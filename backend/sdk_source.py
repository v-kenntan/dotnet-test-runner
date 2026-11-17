"""Discovery and resolution of .NET SDK installs (issue #74).

Two jobs, both Windows-centric:

* :func:`installed_sdk_labels` reads the Windows "Installed apps" registry. The
  SDK picker uses it to show SDKs the same way Settings > Apps > Installed apps
  shows them, with the architecture and, for SDKs that ship with Visual Studio,
  the install source. ``dotnet --list-sdks`` alone cannot tell those apart.
* :func:`resolve_sdk_root` turns the user's input into an SDK install root. It
  extracts a ``.zip`` SDK on demand, so a tester can point straight at the
  downloaded zip and does not have to unzip it by hand first.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass

# Registry DisplayName looks like:
#   "Microsoft .NET SDK 11.0.100-preview.7.26381.103 (x64)"
#   "Microsoft .NET SDK 10.0.400-preview.0.26322.102 (x64) from Visual Studio"
_DISPLAY_PREFIX = "Microsoft .NET SDK "

# Folder under the app's data directory where zip SDKs are unpacked.
ZIP_CACHE_DIRNAME = "sdk_zips"


def dotnet_exe(root):
    """Path to the dotnet executable inside an SDK install root."""
    return os.path.join(root or "", "dotnet.exe" if sys.platform == "win32" else "dotnet")


def is_sdk_root(root):
    return bool(root) and os.path.isfile(dotnet_exe(root))


@dataclass(frozen=True)
class SdkContext:
    """The SDK install and version one test runs against (see GLOSSARY.md).

    `root` is None for the machine-wide install. `version` is None when there
    is no global.json pin. All code that points dotnet at an SDK uses this
    class, so the global.json shape and the DOTNET_* rules live in one place.
    """
    root: str = None
    version: str = None

    @property
    def tfm(self):
        """Target framework moniker for {tfm}, for example 11.0.100-preview.7 -> net11.0."""
        # net10.0 when unpinned, not the machine default. Resolve it if that matters.
        return f"net{self.version.split('.')[0]}.0" if self.version else "net10.0"

    def dotnet_exe(self):
        """The dotnet in the pinned install, or else the one on PATH."""
        return dotnet_exe(self.root) if is_sdk_root(self.root) else "dotnet"

    def env(self, base=None):
        """Copy of `base` (default os.environ) with dotnet pointed at `root`."""
        env = dict(base) if base is not None else os.environ.copy()
        if self.root:
            env["DOTNET_ROOT"] = self.root
            env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
            env["PATH"] = self.root + ";" + env.get("PATH", "")
        return env

    def bat_prelude(self):
        """cmd lines that do what env() does. When unpinned they clear the
        variables an earlier pinned test set in the same console."""
        if self.root:
            return (f'set "DOTNET_ROOT={self.root}"\n'
                    "set DOTNET_MULTILEVEL_LOOKUP=0\n"
                    f'set "PATH={self.root};%PATH%"\n')
        return 'set "DOTNET_ROOT="\nset "DOTNET_MULTILEVEL_LOOKUP="\n'

    def write_global_json(self, directory):
        """Pin `version` in `directory`/global.json. No-op when unpinned."""
        if not self.version:
            return
        with open(os.path.join(directory, "global.json"), "w") as f:
            json.dump({"sdk": {"version": self.version, "rollForward": "disable"}}, f)


def _registry_sdk_displays():
    """Every "Microsoft .NET SDK ..." DisplayName in the uninstall registry.

    It reads both the 64-bit and 32-bit views, because an x86 SDK registers
    under WOW6432Node. Returns a list of the strings with the common prefix stripped.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    displays = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            root_key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_READ | view
            )
        except OSError:
            continue
        with root_key:
            count = winreg.QueryInfoKey(root_key)[0]
            for i in range(count):
                try:
                    name = winreg.EnumKey(root_key, i)
                    with winreg.OpenKey(root_key, name) as sub:
                        display = winreg.QueryValueEx(sub, "DisplayName")[0]
                except OSError:
                    continue
                if isinstance(display, str) and display.startswith(_DISPLAY_PREFIX):
                    rest = display[len(_DISPLAY_PREFIX):].strip()
                    if rest and rest not in displays:
                        displays.append(rest)
    return displays


def installed_sdk_labels():
    """Map SDK version -> list of Settings-style labels for that version.

    A version can appear twice (an x64 and an x86 install). The caller then
    uses the SDK's path to pick one. See :func:`label_for`.
    """
    by_version = {}
    for rest in _registry_sdk_displays():
        version = rest.split(" ", 1)[0]
        if version:
            by_version.setdefault(version, []).append(rest)
    return by_version


def label_for(version, path, by_version):
    """Best Settings-style label for one `dotnet --list-sdks` entry.

    Falls back to the bare version when the SDK has no registry entry. This is
    the normal case for a zip SDK, because zips are not registered as installed
    apps.
    """
    candidates = by_version.get(version) or []
    if not candidates:
        return version
    if len(candidates) == 1:
        return candidates[0]
    # Several installs share this version. Pick the one whose architecture
    # matches the folder this SDK is in.
    wanted = "(x86)" if "program files (x86)" in (path or "").replace("/", "\\").lower() else None
    for c in candidates:
        if wanted and wanted in c:
            return c
        if not wanted and "(x86)" not in c:
            return c
    return candidates[0]


def _find_root(folder):
    """The SDK install root at or just below `folder`.

    Zip SDKs usually unpack with dotnet.exe at the top, but some wrap everything
    in a single directory. So this also checks one level down.
    """
    if is_sdk_root(folder):
        return folder
    try:
        entries = [os.path.join(folder, e) for e in os.listdir(folder)]
    except OSError:
        return None
    for entry in sorted(entries):
        if os.path.isdir(entry) and is_sdk_root(entry):
            return entry
    return None


def _zip_fingerprint(zip_path):
    """Identity of a zip file, so re-selecting the same zip reuses the extract
    but a rebuilt zip at the same path does not."""
    st = os.stat(zip_path)
    key = f"{os.path.abspath(zip_path)}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _long_path(path):
    """`path` in Windows extended-length form (``\\\\?\\``), elsewhere unchanged.

    SDK zips contain deeply nested targets files. An extract under a deep app
    folder (for example ``Downloads\\dotnet-test-runner (1)\\...``) exceeds MAX_PATH.
    It then fails with "No such file or directory" unless long paths are enabled
    machine-wide.
    """
    if sys.platform != "win32":
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _extract_zip(zip_path, cache_base, log=None):
    """Extract an SDK zip into the cache and return (root, error).

    Skips extraction when the same zip is already unpacked.
    """
    cache_dir = os.path.join(cache_base, ZIP_CACHE_DIRNAME)
    stem = os.path.splitext(os.path.basename(zip_path))[0]
    dest = os.path.join(cache_dir, f"{stem}-{_zip_fingerprint(zip_path)}")

    done_marker = os.path.join(dest, ".extracted")
    if os.path.isfile(done_marker):
        root = _find_root(dest)
        if root:
            if log:
                log(f"Reusing previously extracted SDK: {root}")
            return root, None
        # The marker exists but the folder has no usable SDK. Start over.
        shutil.rmtree(_long_path(dest), ignore_errors=True)

    if log:
        log(f"Extracting SDK zip to {dest} (this can take a while)...")
    # Do not trust a partial extract from an earlier failure.
    shutil.rmtree(_long_path(dest), ignore_errors=True)
    try:
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            # extractall sanitises member paths (absolute paths and "..") itself.
            zf.extractall(_long_path(dest))
    except (OSError, zipfile.BadZipFile) as e:
        shutil.rmtree(_long_path(dest), ignore_errors=True)
        return None, f"could not extract {os.path.basename(zip_path)}: {e}"

    root = _find_root(dest)
    if not root:
        shutil.rmtree(_long_path(dest), ignore_errors=True)
        return None, (
            f"{os.path.basename(zip_path)} does not look like a .NET SDK zip "
            f"(no {os.path.basename(dotnet_exe(''))} inside)"
        )
    try:
        with open(done_marker, "w", encoding="utf-8") as f:
            f.write(os.path.abspath(zip_path))
    except OSError:
        pass
    return root, None


def resolve_sdk_root(path, cache_base, log=None):
    """Resolve user input to an SDK install root. Returns (root, error).

    Accepts an install folder (returned as-is) or a ``.zip`` SDK, which is
    extracted into `cache_base` on first use and reused afterwards.
    """
    path = (path or "").strip().strip('"').strip("'")
    if not path:
        return None, "no path given"
    path = os.path.normpath(os.path.expandvars(os.path.expanduser(path)))

    if os.path.isdir(path):
        root = _find_root(path)
        if root:
            return root, None
        return None, f"folder has no {os.path.basename(dotnet_exe(''))}: {path}"

    if os.path.isfile(path):
        if path.lower().endswith(".zip"):
            return _extract_zip(path, cache_base, log)
        return None, f"not a folder or .zip file: {path}"

    return None, f"path does not exist: {path}"


def sdk_version(root, timeout=30):
    """`dotnet --version` for an SDK install root, or None."""
    exe = dotnet_exe(root)
    if not os.path.isfile(exe):
        return None
    try:
        result = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=timeout,
            cwd=root,
        )
        version = (result.stdout or "").strip().splitlines()
        return version[-1].strip() if version else None
    except Exception:
        return None


def resolve_version(root, pinned):
    """The SDK version dotnet really runs for `root` when `pinned` is asked for.

    Uses the same global.json the test steps use, so history logs the version
    that really runs, not a stale pinned string. Falls back to the unpinned
    default when the pinned version is no longer installed.
    """
    sdk = SdkContext(root, pinned)

    def run(cwd):
        try:
            r = subprocess.run(
                [sdk.dotnet_exe(), "--version"], capture_output=True, text=True,
                timeout=30, cwd=cwd, env=sdk.env(),
            )
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    if not pinned:
        return run(None)
    with tempfile.TemporaryDirectory(prefix="sdk_resolve_", ignore_cleanup_errors=True) as d:
        sdk.write_global_json(d)
        return run(d) or run(None)
