"""Read and edit the machine's user-level NuGet.Config (issue #64).

Without the required package sources configured, restores fail with `NU1102`
and tests report a misleading "failed" status, so the app surfaces the file and
can add the missing feeds itself.

There is only one user-level NuGet.Config per machine, but which feeds a build
needs depends on the SDK under test, so every feed carries the SDK it is meant
for and testers can add their own.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

# Feeds the app offers to add. `sdk` is shown in the UI so a shared config file
# still makes clear which SDK each source is there for.
REQUIRED_FEEDS = [
    {
        "key": "azure-default",
        "url": "https://packagefeedproxy.microsoft.io/nuget/v3/index.json",
        "protocol_version": "3",
        "sdk": "All SDKs",
    },
]

EMPTY_CONFIG = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<configuration>\n"
    "  <packageSources>\n"
    "  </packageSources>\n"
    "</configuration>\n"
)


def config_path() -> str:
    """Location of the user-level NuGet.Config.

    NuGet writes it the first time an SDK is used, so it can legitimately be
    missing on a freshly imaged VM.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return os.path.join(base, "NuGet", "NuGet.Config")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.nuget")
    return os.path.join(base, "NuGet", "NuGet.Config")


def open_in_notepad() -> dict:
    """Open the user-level NuGet.Config in Notepad (issue #76).

    Testers paste a complete feed configuration in, so an empty config is
    created first when NuGet has not written one yet; otherwise Notepad would
    offer to create the file in a folder that may not exist.
    """
    path = config_path()
    if sys.platform != "win32":
        return {"ok": False, "path": path, "error": "opening in Notepad is only supported on Windows"}
    try:
        if not os.path.isfile(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(EMPTY_CONFIG)
        notepad = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe")
        # cwd: Notepad outlives the app and would otherwise hold the app's
        # folder open, blocking a rebuild of dist\ (WinError 32).
        subprocess.Popen([notepad, path], cwd=os.path.dirname(path))
    except OSError as e:
        return {"ok": False, "path": path, "error": f"could not open NuGet.Config: {e}"}
    threading.Thread(target=_raise_notepad, daemon=True).start()
    return {"ok": True, "path": path, "error": None}


def _raise_notepad(timeout: float = 5.0):
    """Bring the NuGet.Config Notepad window to the front. Windows only.

    The server is a background process, so Windows opens Notepad behind the app
    window and the click looks like it did nothing. Windows 11 Notepad starts via
    a stub process, so its window is found by class and title rather than by pid.
    """
    import ctypes
    from executor import _force_foreground
    user32 = ctypes.windll.user32
    found = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _visit(hwnd, _lparam):
        # Match the window class too: a browser tab about NuGet.Config and
        # Notepad would otherwise match on title alone.
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if cls.value == "Notepad" and user32.IsWindowVisible(hwnd) and "NuGet.Config" in title.value:
            found.value = hwnd
            return False
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        user32.EnumWindows(_visit, 0)
        if found.value:
            _force_foreground(found.value)
            # _force_foreground pins the window topmost (meant for alerts); an
            # editor must not stay above everything else.
            HWND_NOTOPMOST, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = -2, 0x1, 0x2, 0x10
            user32.SetWindowPos(ctypes.c_void_p(found.value), ctypes.c_void_p(HWND_NOTOPMOST),
                                0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
            return
        time.sleep(0.2)


def _parse(path: str):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != "configuration":
        raise ValueError(f"unexpected root element <{root.tag}>, expected <configuration>")
    return tree, root


def _section(root, name: str, create: bool = False):
    el = root.find(name)
    if el is None and create:
        el = ET.SubElement(root, name)
    return el


def _disabled_keys(root) -> set:
    """Keys listed under <disabledPackageSources> with value 'true'.

    A source can be present and still unused, which looks identical to a missing
    feed from the tester's point of view, so it has to be reported separately.
    """
    out = set()
    section = _section(root, "disabledPackageSources")
    if section is None:
        return out
    for add in section.findall("add"):
        if (add.get("value") or "").strip().lower() == "true":
            out.add((add.get("key") or "").strip())
    return out


def read_state() -> dict:
    """Current package sources plus which required feeds are missing."""
    path = config_path()
    if not os.path.isfile(path):
        return {
            "path": path,
            "exists": False,
            "sources": [],
            "required": [dict(f, present=False, disabled=False) for f in REQUIRED_FEEDS],
            "error": None,
        }
    try:
        _, root = _parse(path)
    except (ET.ParseError, ValueError, OSError) as e:
        return {
            "path": path, "exists": True, "sources": [],
            "required": [dict(f, present=False, disabled=False) for f in REQUIRED_FEEDS],
            "error": f"could not read NuGet.Config: {e}",
        }

    disabled = _disabled_keys(root)
    sources = []
    section = _section(root, "packageSources")
    if section is not None:
        for add in section.findall("add"):
            key = (add.get("key") or "").strip()
            sources.append({
                "key": key,
                "url": (add.get("value") or "").strip(),
                "protocol_version": add.get("protocolVersion") or "",
                "disabled": key in disabled,
            })

    by_url = {s["url"].rstrip("/").lower(): s for s in sources}
    by_key = {s["key"].lower(): s for s in sources}
    required = []
    for feed in REQUIRED_FEEDS:
        # Match on URL first: the same feed added under a different key is still
        # configured, and re-adding it would just create a duplicate source.
        match = by_url.get(feed["url"].rstrip("/").lower()) or by_key.get(feed["key"].lower())
        required.append(dict(
            feed,
            present=match is not None,
            disabled=bool(match and match["disabled"]),
        ))
    return {"path": path, "exists": True, "sources": sources, "required": required, "error": None}


def add_feeds(feeds: list, backup: bool = True) -> dict:
    """Add (or re-enable) package sources, creating the file if NuGet has not yet.

    Returns the same shape as `read_state`, plus `added`/`enabled`/`backup_path`.
    """
    path = config_path()
    added, enabled = [], []
    backup_path = None

    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(EMPTY_CONFIG)
    elif backup:
        backup_path = path + ".bak"
        try:
            shutil.copy2(path, backup_path)
        except OSError:
            backup_path = None

    try:
        tree, root = _parse(path)
    except (ET.ParseError, ValueError, OSError) as e:
        return {"ok": False, "error": f"could not read NuGet.Config: {e}"}

    section = _section(root, "packageSources", create=True)
    existing = {(a.get("value") or "").strip().rstrip("/").lower(): a
                for a in section.findall("add")}
    disabled_section = _section(root, "disabledPackageSources")

    for feed in feeds:
        key = (feed.get("key") or "").strip()
        url = (feed.get("url") or "").strip()
        if not key or not url:
            continue
        node = existing.get(url.rstrip("/").lower())
        if node is None:
            node = ET.SubElement(section, "add")
            node.set("key", key)
            node.set("value", url)
            added.append(key)
        pv = (feed.get("protocol_version") or "").strip()
        if pv:
            node.set("protocolVersion", pv)
        # An explicitly disabled source is silently ignored by restore, so adding
        # it back is only useful if the disable entry goes away too.
        if disabled_section is not None:
            for bad in [a for a in disabled_section.findall("add")
                        if (a.get("key") or "").strip().lower() == (node.get("key") or "").lower()]:
                disabled_section.remove(bad)
                enabled.append(node.get("key"))

    if disabled_section is not None and len(disabled_section) == 0:
        root.remove(disabled_section)

    try:
        ET.indent(tree, space="  ")
    except AttributeError:  # Python < 3.9
        pass
    try:
        tree.write(path, encoding="utf-8", xml_declaration=True)
    except OSError as e:
        return {"ok": False, "error": f"could not write NuGet.Config: {e}"}

    state = read_state()
    state.update({"ok": True, "added": added, "enabled": enabled, "backup_path": backup_path})
    return state
