"""In-app update from the latest GitHub Release (issue #89).

While the repo is private, GitHub answers 404, so the launch check finds nothing
and the banner stays hidden. When the repo goes public it starts working by
itself. Set GITHUB_TOKEN to try it before then.

The running exe cannot replace itself, so start() hands off to the Updater: a
.cmd script that waits for this process to exit, swaps the files and restarts.
"""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

REPO = "xianyoong/CLI-Automation-Code"
ASSET = "dotnet-test-runner.zip"
EXE = "dotnet-test-runner.exe"
# Inside the App folder, so the Updater's moves are renames on one volume
# (cmd's `move` cannot move a folder to another drive).
STAGING = "_update"
# The Updater leaves this in the App folder when it had to restore the old files.
FAILED_MARKER = "update_failed.txt"


def current_version(base_dir):
    """The Release tag this build was made from, or 'dev' (never updates)."""
    if not getattr(sys, "_MEIPASS", None):
        return "dev"
    try:
        with open(os.path.join(base_dir, "version.txt"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


def _open(url, accept):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "dotnet-test-runner"})
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        # Unredirected header. The asset download redirects to storage that
        # rejects a second auth.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=30)


def check(version):
    """{'tag', 'asset_url'} when the latest Release is not `version`, else None.
    Raises when GitHub cannot be asked (404 while private, offline VM, rate limit)."""
    if version == "dev":
        return None
    with _open(f"https://api.github.com/repos/{REPO}/releases/latest", "application/vnd.github+json") as r:
        release = json.load(r)
    tag = release.get("tag_name")
    asset = next((a for a in release.get("assets") or [] if a.get("name") == ASSET), None)
    # Update when the tag is different, not only when it is higher. So if an older
    # Release is marked as Latest, everyone rolls back to it.
    if not tag or tag == version or not asset:
        return None
    return {"tag": tag, "asset_url": asset["url"]}


def stage(asset_url, app_dir):
    """Download and extract the Release into <app_dir>/_update. Returns the new app's folder."""
    staging = os.path.join(app_dir, STAGING)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    zip_path = os.path.join(staging, ASSET)
    with _open(asset_url, "application/octet-stream") as r, open(zip_path, "wb") as f:
        shutil.copyfileobj(r, f)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(staging)
    os.remove(zip_path)
    new_dir = os.path.join(staging, "dotnet-test-runner")
    if not os.path.isfile(os.path.join(new_dir, EXE)):
        raise RuntimeError(f"the release ZIP has no dotnet-test-runner\\{EXE}")
    return new_dir


def updater_script(app_dir, new_dir, pid):
    """The Updater. Swaps only the items in the ZIP, so user data in the App folder stays."""
    lines = [
        "@echo off",
        # Do not trust the app's PATH. The Updater needs tasklist and ping.
        'set "PATH=%SystemRoot%\\System32;%PATH%"',
        # %% so a '%' in a folder name is not read as a variable.
        f'set "APP={app_dir.replace("%", "%%")}"',
        f'set "NEW={new_dir.replace("%", "%%")}"',
        ":wait",
        # ping is used as the sleep, because `timeout` fails when there is no console input.
        f'tasklist /NH /FI "PID eq {pid}" | find " {pid} " >nul && (ping -n 2 127.0.0.1 >nul & goto wait)',
        "ping -n 3 127.0.0.1 >nul",  # let antivirus/indexers drop their handles
    ]
    items = sorted(os.listdir(new_dir))
    for name in items:
        lines += [
            f'call :remove "%APP%\\{name}.old"',
            f'if exist "%APP%\\{name}" move /y "%APP%\\{name}" "%APP%\\{name}.old" >nul || goto rollback',
            f'move /y "%NEW%\\{name}" "%APP%\\{name}" >nul || goto rollback',
        ]
    lines += [
        f'call :remove "%APP%\\{STAGING}"',
        f'start "" /d "%APP%" "%APP%\\{EXE}"',
        "exit /b 0",
        ":rollback",
    ]
    for name in items:
        lines.append(
            f'if exist "%APP%\\{name}.old" (call :remove "%APP%\\{name}" & move /y "%APP%\\{name}.old" "%APP%\\{name}" >nul)'
        )
    lines += [
        f'type nul > "%APP%\\{FAILED_MARKER}"',
        f'start "" /d "%APP%" "%APP%\\{EXE}"',
        "exit /b 1",
        ":remove",
        'if exist "%~1\\*" (rd /s /q "%~1") else if exist "%~1" del /f /q "%~1"',
        "exit /b 0",
    ]
    return "\n".join(lines) + "\n"  # text mode writes \r\n


def start(asset_url, app_dir, pid):
    """Stage the Release and launch the Updater. The caller must exit right after."""
    new_dir = stage(asset_url, app_dir)
    # In %TEMP%, not _update: the script deletes _update and cmd re-reads the
    # script file line by line.
    fd, script = tempfile.mkstemp(prefix="dtr-update-", suffix=".cmd")
    # cmd reads batch files in the OEM code page.
    with os.fdopen(fd, "w", encoding="oem") as f:
        f.write(updater_script(app_dir, new_dir, pid))
    # Started by this (elevated) app, so the Updater and the restarted app keep admin rights.
    # Its output goes to a .log next to the script, to diagnose a failed update.
    with open(script[:-4] + ".log", "w") as log:
        subprocess.Popen(
            ["cmd", "/c", script],
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )


def take_failure(app_dir):
    """True once if the last update was rolled back. Deletes the marker."""
    try:
        os.remove(os.path.join(app_dir, FAILED_MARKER))
        return True
    except OSError:
        return False


def cleanup(app_dir):
    """Delete what an update left behind. Call only after the app started fine."""
    for name in os.listdir(app_dir):
        if name.endswith(".old") or name == STAGING:
            path = os.path.join(app_dir, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    os.remove(path)
