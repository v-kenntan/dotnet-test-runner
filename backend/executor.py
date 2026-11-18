import os
import re
import sys
import json
import shutil
import uuid
import subprocess
import tempfile
import threading
import time
import sqlite3
import ssl
import ctypes
import platform
import urllib.request
import webbrowser
from dataclasses import replace
from datetime import datetime
from queue import Queue, Empty
from typing import Dict, List, Generator, Optional

import results_export
import db_store
import sdk_source
from steps import (
    CANCELLED, FAILED, WARNING, ConsoleShell, DirectShell, StepContext, StepOutcome,
    render_terminal_output, run_step, screenshot_base,
)

# Read-only fixtures shipped with the app (for example, sample projects that tests
# copy into their work dir). Resolves under PyInstaller's _MEIPASS when bundled.
ASSETS_DIR = os.path.join(
    getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__)),
    "test_assets",
)


def _source_mtimes() -> dict:
    """mtime of each backend .py and test YAML, so a run can tell the app is stale."""
    base = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(base, n) for n in os.listdir(base) if n.endswith(".py")]
    defs = os.path.join(base, "test_definitions")
    if os.path.isdir(defs):
        paths += [os.path.join(defs, n) for n in os.listdir(defs) if n.endswith(".yaml")]
    return {p: os.path.getmtime(p) for p in paths}


# Take the snapshot at import time. This is for dev only, because a bundled exe
# cannot change while it runs.
_LOADED_MTIMES = {} if getattr(sys, "_MEIPASS", None) else _source_mtimes()


def _stale_sources() -> list:
    """Names of source files edited since the app started (dev mode only)."""
    if not _LOADED_MTIMES:
        return []
    now = _source_mtimes()
    return sorted(os.path.basename(p) for p, m in now.items()
                  if _LOADED_MTIMES.get(p) != m)


def _get_db_path():
    return db_store.db_path()


DB_PATH = _get_db_path()

# The app's own folder, which stays local even when the DB lives in OneDrive.
APP_DIR = db_store.default_dir()


def _get_screenshots_dir():
    """Root folder for run screenshots, kept next to the DB so it persists in
    both dev and PyInstaller-frozen runs."""
    return db_store.screenshots_dir()


SCREENSHOTS_DIR = _get_screenshots_dir()


# Temp artifacts created per test/run. The app deletes them as soon as it no
# longer needs them, but a crash or a hard kill can still leave them behind. The
# app also keeps the working directories of failed tests on purpose, for
# debugging. The startup sweep limits that growth.
TEMP_PREFIXES = ("dotnet_test_", "test_runner_", "notepad_shot_", "sdk_resolve_",
                 "gui_shot_")
TEMP_MAX_AGE_HOURS = 24 * 7


def _rmtree(path: str, retries: int = 3) -> bool:
    """Best-effort recursive delete.

    On Windows a just-terminated dotnet/MSBuild process can briefly keep a
    handle open, so a first attempt may fail with a sharing violation. Retry a
    few times before giving up. Never raises, because cleanup must not fail a test.
    """
    if not path or not os.path.isdir(path):
        return True
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return True
        except OSError:
            if attempt == retries - 1:
                return False
            time.sleep(0.5)
    return False


def sweep_temp_artifacts(max_age_hours: int = TEMP_MAX_AGE_HOURS) -> int:
    """Delete stranded temp artifacts older than max_age_hours.

    Only touches entries this app creates (TEMP_PREFIXES) and only when they are
    older than the cutoff, so a concurrently running test's directory can never
    be removed. Returns the number of entries deleted.
    """
    root = tempfile.gettempdir()
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        if not name.startswith(TEMP_PREFIXES):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            if os.path.isdir(path):
                if _rmtree(path):
                    removed += 1
            else:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed


def _force_foreground(hwnd: int) -> bool:
    """Bring a window in front of the window that has focus now. Windows only.

    SetForegroundWindow normally refuses a background process (this app while
    Edge/Notepad has focus), so an alert only flashes in the taskbar instead of
    popping up. Setting the foreground lock timeout to zero and briefly
    attaching to the foreground window's input queue removes that restriction.
    The completion alert needs this (issue #67).
    """
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    SW_SHOW, SW_RESTORE, HWND_TOPMOST = 5, 9, -1
    SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x1, 0x2, 0x40
    SPI_GETFOREGROUNDLOCKTIMEOUT, SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2000, 0x2001
    SPIF_SENDCHANGE = 0x2

    previous = ctypes.c_uint(0)
    locked = bool(user32.SystemParametersInfoW(
        SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(previous), 0))
    if locked:
        user32.SystemParametersInfoW(
            SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), SPIF_SENDCHANGE)

    foreground = user32.GetForegroundWindow()
    this_thread = kernel32.GetCurrentThreadId()
    other_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached = bool(other_thread) and other_thread != this_thread and bool(
        user32.AttachThreadInput(this_thread, other_thread, True))
    try:
        user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
        user32.SetActiveWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(this_thread, other_thread, False)
        if locked:
            user32.SystemParametersInfoW(
                SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                ctypes.c_void_p(previous.value), SPIF_SENDCHANGE)
    if not ok:
        _flash_until_opened(hwnd)
    return ok


def _flash_until_opened(hwnd: int):
    """Blink a window's taskbar button until the user opens it. Windows only.

    Use this as a last resort when Windows still refuses to give focus. A
    steady blink is easier to notice than FlashWindow's single toggle.
    """
    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                    ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                    ("dwTimeout", ctypes.c_uint)]

    FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
    info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), ctypes.c_void_p(hwnd),
                      FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
    try:
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def _own_dialog_hwnd() -> int:
    """Handle of a visible dialog (class #32770) owned by this process, or 0."""
    user32 = ctypes.windll.user32
    found = ctypes.c_void_p(0)
    my_pid = os.getpid()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _visit(hwnd, _lparam):
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != my_pid or not user32.IsWindowVisible(hwnd):
            return True
        name = ctypes.create_unicode_buffer(32)
        user32.GetClassNameW(hwnd, name, 32)
        if name.value != "#32770":
            return True
        found.value = hwnd
        return False

    user32.EnumWindows(_visit, 0)
    return found.value or 0


# Runtime identifier used by `{rid}` in test steps, for example "win-x64" on an x64 VM
# and "win-arm64" on an ARM64 one, so self-contained publish tests target the
# machine they run on instead of a hard-coded architecture (issue #69).
_RID_RE = re.compile(r"^\s*RID:\s+(\S+)", re.MULTILINE)

_ARCH_ALIASES = {
    "amd64": "x64", "x86_64": "x64", "aarch64": "arm64",
    "i386": "x86", "i686": "x86",
}


_IMAGE_FILE_MACHINE = {0x014C: "x86", 0x8664: "x64", 0x01C4: "arm", 0xAA64: "arm64"}


def machine_arch() -> str:
    """Native CPU architecture of this machine: x64, arm64, x86, ...

    This does not depend on the SDK. An x86 SDK on an x64 machine still reports x64.
    On Windows this asks IsWow64Process2 for the native machine, because an
    x64 build of the app running emulated on Arm64 sees itself as x64.
    """
    if sys.platform == "win32":
        try:
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.IsWow64Process2.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT),
            ]
            process_machine, native_machine = wintypes.USHORT(), wintypes.USHORT()
            if k32.IsWow64Process2(k32.GetCurrentProcess(),
                                   ctypes.byref(process_machine), ctypes.byref(native_machine)):
                arch = _IMAGE_FILE_MACHINE.get(native_machine.value)
                if arch:
                    return arch
        except (AttributeError, OSError):
            pass  # IsWow64Process2 needs Windows 10 1511+
    arch = (platform.machine() or "").lower()
    return _ARCH_ALIASES.get(arch, arch or "x64")


def _host_rid() -> str:
    """This machine's runtime identifier, for example win-x64 or win-arm64."""
    arch = machine_arch()
    os_part = {"win32": "win", "darwin": "osx"}.get(sys.platform, "linux")
    return f"{os_part}-{arch}"


def _parse_rid(dotnet_info: str) -> str:
    """RID reported by `dotnet --info`, falling back to this machine's RID.

    dotnet's own value is correct for a non-native SDK. For example, an x86 SDK
    on an x64 machine reports win-x86, which is what its publishes should target.
    """
    match = _RID_RE.search(dotnet_info or "")
    return match.group(1) if match else _host_rid()


# Title fragment common to this app's own windows: the Edge app window
# (".NET SDK Test Runner") and the live console (".NET Test Runner - Run <id>").
# Screenshot capture excludes them so a run never photographs the runner itself.
# A match on the full ".NET SDK Test Runner" missed the console, because its title
# has no "SDK". That is how the console got into GUI-window screenshots.
OWN_WINDOW_TITLE = "Test Runner"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # The DB may sit in a synced folder, where a sync client can hold the file
    # for a moment. Wait instead of failing the step write at once.
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class _RunDesktop:
    """The Desktop adapter for one run: TestExecutor's Windows helpers, bound
    to the run's console window."""

    def __init__(self, executor: "TestExecutor", run_id: str):
        self._ex = executor
        self._run_id = run_id

    def show_in_notepad(self, path, screenshot_path):
        return self._ex._show_in_notepad(path, screenshot_path)

    def capture_console(self, screenshot_path):
        """Capture the live console after a `screenshot: true` command step, so
        the command and its output stay as a checkpoint (issue #77). The code
        finds the console by its `title ... - Run <run_id>`."""
        if not self._ex._console_procs.get(self._run_id):
            return "no console window"
        return self._ex._capture_screen(
            screenshot_path, title_hint=f"Run {self._run_id}", window_only=True)

    def screenshot_command(self, step, ctx):
        return self._ex._screenshot_command(self._run_id, step, ctx)

    def run_long_running(self, command, step, ctx):
        return self._ex._run_long_running(self._run_id, command, step, ctx)


class TestExecutor:
    def __init__(self):
        self._runs: Dict[str, dict] = {}
        self._event_queues: Dict[str, List[Queue]] = {}
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._console_procs: Dict[str, str] = {}  # run_id -> console_dir path

    def has_active_run(self) -> bool:
        return bool(self._cancel_flags)

    def start_run(self, run_id: str, tests: List[dict], sdk_version: str = None, sdk_path: str = None):
        """Start executing tests in a background thread.

        sdk_path, when provided, is the install root (folder containing dotnet.exe)
        of a specific .NET SDK, for example a zip-extracted SDK. All dotnet commands
        for the run use that install (through DOTNET_ROOT and PATH). This lets the
        run test zip/file-based workload installs instead of the SDK on PATH.
        """
        cancel_flag = threading.Event()
        self._cancel_flags[run_id] = cancel_flag
        sdk_path = (sdk_path or "").strip() or None
        self._runs[run_id] = {"status": "running", "tests": tests, "sdk_version": sdk_version, "sdk_path": sdk_path}
        self._event_queues[run_id] = []

        self._open_console(run_id)

        conn = get_db()
        conn.execute(
            "INSERT INTO test_runs (id, started_at, status, sdk_version, sdk_path, machine_arch) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, datetime.now().isoformat(), "running", sdk_version, sdk_path, machine_arch()),
        )
        conn.commit()
        conn.close()

        thread = threading.Thread(
            target=self._execute_run, args=(run_id, tests, cancel_flag), daemon=True
        )
        thread.start()

    def _resolve_pinned_root(self, run_id: str, raw: str, what: str):
        """Resolve a pinned SDK path to an install root, or None if unusable.

        Returns a plain folder as-is. Extracts a ``.zip`` SDK on first use,
        so the user can pin a zip directly instead of unzipping it by hand
        (issue #74). Logs progress and failures to the run, because extracting
        a real SDK zip is slow. Without the log, it can look like a hang.
        """
        raw = (raw or "").strip()
        if not raw:
            return None

        def note(line):
            self._emit_event(run_id, {
                "type": "step_output", "result_id": None, "step_index": -1, "line": f"[info] {line}",
            })
            self._queue_console_cmd(run_id, f"echo [info] {line}")

        # A folder that is already valid needs no work and no logging.
        if sdk_source.is_sdk_root(raw):
            return raw
        root, error = sdk_source.resolve_sdk_root(raw, APP_DIR, log=note)
        if error:
            warn = f"[warn] {what}: {error}"
            self._emit_event(run_id, {
                "type": "step_output", "result_id": None, "step_index": -1, "line": warn,
            })
            self._queue_console_cmd(run_id, f"echo {warn}")
            return None
        return root

    def _powershell_exe(self) -> str:
        """Full path to Windows PowerShell. The spawned console's PATH may lack
        System32, so a bare `powershell` name is not always found."""
        return os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )

    def _capture_screen(self, out_path: str, pid: int = None, title_hint: str = None,
                        foreground_fallback: bool = False, exclude_title: str = None,
                        maximize: bool = False, descendant_of_pid: int = None,
                        window_only: bool = False) -> Optional[str]:
        """Capture a screenshot to out_path (PNG). Windows only.

        The code reads the window's own pixels with PrintWindow + PW_RENDERFULLCONTENT,
        which reads from the window's DWM buffer. Because of this, an overlapping
        window (for example, this app's own live console) cannot appear in the
        shot. A CopyFromScreen of the window rectangle is only a fallback for
        when PrintWindow fails or returns an empty bitmap, because it captures
        whatever is on screen.

        When pid is given, capture that process's main window. When the pid's
        window handle can't be resolved (for example, Windows 11 Notepad launches
        through a stub process, or a browser opens a tab in an already-running
        process), fall back to a top-level window whose title contains title_hint.
        When foreground_fallback is set, use the current foreground window if
        neither pid nor title match (useful for browsers, which bring themselves
        to the front). Falls back to a full virtual-screen capture only if no
        window can be resolved and window_only is not set. Best-effort. Returns
        None when the PNG was written, else the failure reason."""
        script_path = None
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            safe_path = out_path.replace("'", "''")
            script = "\n".join([
                "$ErrorActionPreference = 'Stop'",
                f"$path = '{safe_path}'",
                f"$procId = {int(pid) if pid else 0}",
                f"$rootPid = {int(descendant_of_pid) if descendant_of_pid else 0}",
                f"$titleHint = '{(title_hint or '').replace(chr(39), chr(39) + chr(39))}'",
                f"$excludeTitle = '{(exclude_title or '').replace(chr(39), chr(39) + chr(39))}'",
                f"$useForeground = ${'true' if foreground_fallback else 'false'}",
                f"$maximize = ${'true' if maximize else 'false'}",
                f"$windowOnly = ${'true' if window_only else 'false'}",
                "Add-Type -AssemblyName System.Windows.Forms",
                "Add-Type -AssemblyName System.Drawing",
                'Add-Type @"',
                "using System;",
                "using System.Runtime.InteropServices;",
                "public class WinCap {",
                '  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);',
                '  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);',
                '  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);',
                '  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);',
                '  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);',
                '  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();',
                '  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);',
                '  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);',
                "  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }",
                "}",
                '"@',
                "# PrintWindow leaves the bitmap untouched (transparent black) when it",
                "# cannot render the window. Detecting that exactly - rather than",
                "# 'every pixel is the same colour' - matters because a freshly created",
                "# WinForms/WPF window is legitimately a flat white/grey rectangle.",
                "function Test-EmptyShot($bitmap) {",
                "  $stepX = [Math]::Max(1, [int]($bitmap.Width / 16))",
                "  $stepY = [Math]::Max(1, [int]($bitmap.Height / 16))",
                "  for ($y = 0; $y -lt $bitmap.Height; $y += $stepY) {",
                "    for ($x = 0; $x -lt $bitmap.Width; $x += $stepX) {",
                "      $c = $bitmap.GetPixel($x, $y)",
                "      if ($c.A -ne 0 -and ($c.R -ne 0 -or $c.G -ne 0 -or $c.B -ne 0)) { return $false }",
                "    }",
                "  }",
                "  return $true",
                "}",
                "$hwnd = [IntPtr]::Zero",
                "if ($procId -ne 0) {",
                "  try {",
                "    $p = Get-Process -Id $procId -ErrorAction Stop",
                "    for ($i = 0; $i -lt 25; $i++) {",
                "      $p.Refresh()",
                "      if ($p.MainWindowHandle -ne [IntPtr]::Zero) { $hwnd = $p.MainWindowHandle; break }",
                "      Start-Sleep -Milliseconds 200",
                "    }",
                "  } catch { $hwnd = [IntPtr]::Zero }",
                "}",
                "if ($hwnd -eq [IntPtr]::Zero -and $rootPid -ne 0) {",
                "  # The GUI app window (WinForms/WPF) belongs to a descendant of the",
                "  # launched shell/`dotnet run` process, not the process we started.",
                "  # Walk the process tree from rootPid and grab the first descendant",
                "  # that has a visible top-level window. Console hosts are skipped:",
                "  # `shell=True` puts cmd.exe/conhost.exe in that same tree and their",
                "  # window would otherwise win the race against the app's own window.",
                "  $consoleHosts = @('conhost','cmd','openconsole','windowsterminal','wt','powershell','pwsh')",
                "  for ($i = 0; $i -lt 25; $i++) {",
                "    try {",
                "      $all = Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object ProcessId, ParentProcessId",
                "      $desc = New-Object 'System.Collections.Generic.HashSet[int]'",
                "      $queue = New-Object 'System.Collections.Generic.Queue[int]'",
                "      $queue.Enqueue([int]$rootPid) | Out-Null",
                "      while ($queue.Count -gt 0) {",
                "        $cur = $queue.Dequeue()",
                "        foreach ($pr in $all) { if ([int]$pr.ParentProcessId -eq $cur -and -not $desc.Contains([int]$pr.ProcessId)) { $desc.Add([int]$pr.ProcessId) | Out-Null; $queue.Enqueue([int]$pr.ProcessId) | Out-Null } }",
                "      }",
                "      $cand = Get-Process -ErrorAction SilentlyContinue | Where-Object { $desc.Contains($_.Id) -and $_.MainWindowHandle -ne [IntPtr]::Zero -and -not ($consoleHosts -contains $_.ProcessName.ToLower()) -and ($excludeTitle -eq '' -or $_.MainWindowTitle -notlike ('*' + $excludeTitle + '*')) } | Select-Object -First 1",
                "      if ($cand) { $hwnd = $cand.MainWindowHandle; break }",
                "    } catch {}",
                "    Start-Sleep -Milliseconds 200",
                "  }",
                "}",
                "if ($hwnd -eq [IntPtr]::Zero -and $titleHint -ne '') {",
                "  for ($i = 0; $i -lt 25; $i++) {",
                "    $wp = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero -and $_.MainWindowTitle -like ('*' + $titleHint + '*') -and ($excludeTitle -eq '' -or $_.MainWindowTitle -notlike ('*' + $excludeTitle + '*')) } | Select-Object -First 1",
                "    if ($wp -and $wp.MainWindowHandle -ne [IntPtr]::Zero) { $hwnd = $wp.MainWindowHandle; break }",
                "    Start-Sleep -Milliseconds 200",
                "  }",
                "}",
                "if ($hwnd -eq [IntPtr]::Zero -and $useForeground) {",
                "  $browsers = @('msedge','chrome','firefox','brave','opera','vivaldi','chromium','iexplore')",
                "  # Prefer the foreground window, but only if it actually belongs to",
                "  # a browser: otherwise it may be the test's terminal/console, which",
                "  # would capture the wrong thing.",
                "  $fg = [WinCap]::GetForegroundWindow()",
                "  if ($fg -ne [IntPtr]::Zero) {",
                "    $fpid = 0",
                "    [WinCap]::GetWindowThreadProcessId($fg, [ref]$fpid) | Out-Null",
                "    $fproc = Get-Process -Id $fpid -ErrorAction SilentlyContinue",
                "    if ($fproc -and ($browsers -contains $fproc.ProcessName.ToLower()) -and ($excludeTitle -eq '' -or $fproc.MainWindowTitle -notlike ('*' + $excludeTitle + '*'))) { $hwnd = $fg }",
                "  }",
                "  # Otherwise locate the active browser window directly (its main",
                "  # window reflects the tab we just opened).",
                "  if ($hwnd -eq [IntPtr]::Zero) {",
                "    for ($i = 0; $i -lt 15; $i++) {",
                "      $bp = Get-Process -ErrorAction SilentlyContinue | Where-Object { ($browsers -contains $_.ProcessName.ToLower()) -and $_.MainWindowHandle -ne [IntPtr]::Zero -and $_.MainWindowTitle -ne '' -and ($excludeTitle -eq '' -or $_.MainWindowTitle -notlike ('*' + $excludeTitle + '*')) } | Select-Object -First 1",
                "      if ($bp) { $hwnd = $bp.MainWindowHandle; break }",
                "      Start-Sleep -Milliseconds 200",
                "    }",
                "  }",
                "}",
                "$saved = $false",
                "if ($hwnd -ne [IntPtr]::Zero) {",
                "  if ($maximize) { [WinCap]::ShowWindow($hwnd, 3) | Out-Null } else { [WinCap]::ShowWindow($hwnd, 9) | Out-Null }",
                "  $topmost = New-Object System.IntPtr(-1)",
                "  $notop = New-Object System.IntPtr(-2)",
                "  [WinCap]::SetWindowPos($hwnd, $topmost, 0, 0, 0, 0, 0x0003) | Out-Null",
                "  [WinCap]::SetForegroundWindow($hwnd) | Out-Null",
                "  Start-Sleep -Milliseconds 1200",
                "  $r = New-Object WinCap+RECT",
                "  $dr = New-Object WinCap+RECT",
                "  $hr = [WinCap]::DwmGetWindowAttribute($hwnd, 9, [ref]$dr, 16)",
                "  if ($hr -eq 0 -and ($dr.Right - $dr.Left) -gt 0 -and ($dr.Bottom - $dr.Top) -gt 0) { $r = $dr } else { [WinCap]::GetWindowRect($hwnd, [ref]$r) | Out-Null }",
                "  $w = $r.Right - $r.Left; $h = $r.Bottom - $r.Top",
                "  if ($w -gt 0 -and $h -gt 0) {",
                "    $bmp = New-Object System.Drawing.Bitmap $w, $h",
                "    $g = [System.Drawing.Graphics]::FromImage($bmp)",
                "    # PW_RENDERFULLCONTENT (0x2) reads the window's own DWM buffer, so",
                "    # GPU-composited WPF/WinForms content renders AND an overlapping",
                "    # window (e.g. this app's live console) cannot bleed into the shot.",
                "    $hdc = $g.GetHdc()",
                "    $ok = [WinCap]::PrintWindow($hwnd, $hdc, 2)",
                "    $g.ReleaseHdc($hdc)",
                "    # CopyFromScreen is a last resort only: it captures whatever is",
                "    # physically on screen, so it can pick up an occluding window.",
                "    if ((-not $ok) -or (Test-EmptyShot $bmp)) {",
                "      $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $h)))",
                "    }",
                "    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)",
                "    $g.Dispose(); $bmp.Dispose()",
                "    $saved = $true",
                "  }",
                "  [WinCap]::SetWindowPos($hwnd, $notop, 0, 0, 0, 0, 0x0003) | Out-Null",
                "}",
                "if (-not $saved -and -not $windowOnly) {",
                "  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen",
                "  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height",
                "  $g = [System.Drawing.Graphics]::FromImage($bmp)",
                "  $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)",
                "  $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)",
                "  $g.Dispose(); $bmp.Dispose()",
                "}",
                "if (-not $saved -and $windowOnly) { Write-Error 'target window not found' }",
            ])
            fd, script_path = tempfile.mkstemp(suffix=".ps1", prefix="notepad_shot_")
            with os.fdopen(fd, "w", encoding="utf-8") as sf:
                sf.write(script)
            # Redirect all three standard streams. A windowed (console=False)
            # PyInstaller build has no valid stdin handle. If the child inherits
            # it, CreateProcess fails with WinError 6. That is why the screenshot
            # never appeared and no error was shown. CREATE_NO_WINDOW keeps the
            # helper PowerShell from flashing a console.
            r = subprocess.run(
                [self._powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script_path],
                timeout=30,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if os.path.isfile(out_path):
                return None
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", "replace").strip()
                return err[:500] or f"powershell exit code {r.returncode}"
            return "screenshot file was not created"
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        finally:
            if script_path:
                try:
                    os.unlink(script_path)
                except OSError:
                    pass

    def _capture_gui_window(self, ctx: StepContext, root_pid: int, write_log, lines, step) -> None:
        """Capture the WinForms/WPF window spawned by a `dotnet run` GUI step and
        log that it was displayed (issue #45). The window belongs to a descendant
        of the launched shell process, so the code finds it by walking the process
        tree. Best-effort. Logs a line on success and a warning on failure."""
        # Use the project directory (for example, `w` or `mywpf`) so the WinForms and
        # WPF shots within one test get distinct names.
        label = step.get("label") or os.path.basename(os.path.normpath(ctx.cwd or ""))
        shot_path = ctx.screenshot_path(label, "gui-window")
        target = shot_path or os.path.join(tempfile.gettempdir(), f"gui_shot_{uuid.uuid4().hex[:8]}.png")
        err = self._capture_screen(
            target, descendant_of_pid=root_pid, exclude_title=OWN_WINDOW_TITLE,
            window_only=True,
        )
        if not err and shot_path:
            msg = f"🖼️ GUI window displayed; screenshot: {os.path.basename(shot_path)}"
        elif not err:
            msg = "🖼️ GUI window displayed"
        else:
            msg = f"[warn] GUI window screenshot failed: {err}"
        ctx.log(msg)
        write_log("\n" + msg + "\n")
        lines.append("\n" + msg + "\n")

    def _screenshot_command(self, run_id: str, step: dict, ctx: StepContext) -> str:
        """Write the screenshot script for a `screenshot` step and return the
        command that runs it. Raises OSError when the script cannot be written."""
        console_dir = self._console_procs.get(run_id)
        script_path = os.path.join(console_dir or tempfile.gettempdir(),
                                   f"screenshot_{uuid.uuid4().hex[:8]}.ps1")
        with open(script_path, "w", encoding="utf-8") as sf:
            sf.write(self._build_screenshot_script(
                step.get("label", "screenshot"), step.get("folder", ""),
                step.get("folder_ps", ""), step.get("delay", 4), ctx.test_name,
                step.get("settings_uri", ""), step.get("search", ""),
            ))
        return f'"{self._powershell_exe()}" -NoProfile -ExecutionPolicy Bypass -File "{script_path}"'

    def _build_screenshot_script(self, label: str, folder: str, folder_ps: str, delay: float, test_name: str = "",
                                 settings_uri: str = "", search: str = "") -> str:
        """Generate a PowerShell script that opens a window and saves a screenshot
        of it into %SCREENSHOT_DIR%. The code writes it to a .ps1 file, not inline,
        so the cmd console does not change its quoting. The saved file has the
        test name (and label), for example `Workload-Scenario-1-installertype-<ts>.png`.

        The script supports two targets:
          * a folder opened in File Explorer (`folder` / `folder_ps`), and
          * a Windows Settings page (`settings_uri`), optionally filtered by
            typing `search` into the page's search box."""
        base = screenshot_base(test_name, label, "screenshot")
        if folder_ps:
            resolve = "$folder = (& { " + folder_ps.strip() + " } | Select-Object -Last 1)"
        else:
            resolve = '$folder = "' + (folder or "").replace('"', '`"') + '"'
        head = [
            "$ErrorActionPreference = 'Stop'",
            "Add-Type -AssemblyName System.Windows.Forms",
            "Add-Type -AssemblyName System.Drawing",
            'Add-Type @"',
            "using System;",
            "using System.Text;",
            "using System.Collections.Generic;",
            "using System.Runtime.InteropServices;",
            "public class WinCap {",
            '  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);',
            '  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);',
            '  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);',
            '  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);',
            '  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);',
            '  [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr h, uint f);',
            '  [DllImport("user32.dll")] public static extern bool GetMonitorInfo(IntPtr m, ref MONITORINFO mi);',
            '  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();',
            '  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);',
            '  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);',
            '  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);',
            '  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);',
            "  public delegate bool EnumProc(IntPtr h, IntPtr l);",
            "  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }",
            "  [StructLayout(LayoutKind.Sequential)] public struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }",
            "  // Settings is hosted by ApplicationFrameHost, so its own process has no",
            "  // MainWindowHandle; the frame has to be found by walking top-level windows.",
            "  public static IntPtr[] ByClass(string cls) {",
            "    var o = new List<IntPtr>();",
            "    EnumWindows((h, l) => {",
            "      if (!IsWindowVisible(h)) return true;",
            "      var c = new StringBuilder(256); GetClassNameW(h, c, 256);",
            "      if (c.ToString() == cls) o.Add(h);",
            "      return true;",
            "    }, IntPtr.Zero);",
            "    return o.ToArray();",
            "  }",
            "}",
            '"@',
            "# The window is measured in physical pixels, so this process must be DPI",
            "# aware. Without this, a scaled display (laptops commonly run at 125-150%)",
            "# reports virtualised sizes and PrintWindow renders past the bitmap,",
            "# cropping the right-hand columns out of the screenshot.",
            "try { [WinCap]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null } catch {}",
            "try { [WinCap]::SetProcessDPIAware() | Out-Null } catch {}",
            "$dir = $env:SCREENSHOT_DIR",
            "if (-not $dir) { $dir = $env:TEMP }",
            "New-Item -ItemType Directory -Force -Path $dir | Out-Null",
        ]
        if settings_uri:
            locate = self._settings_locate_lines(settings_uri, search, delay)
        else:
            locate = self._explorer_locate_lines(resolve, delay)
        capture = [
            "$saved = $false",
            f"$file = Join-Path $dir ('{base}-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.png')",
            "if ($hwnd -ne [IntPtr]::Zero) {",
            "  [WinCap]::ShowWindow($hwnd, 9) | Out-Null",
            "  # Fill the monitor's work area. Explorer otherwise reopens at its",
            "  # last-remembered size, and on a smaller laptop screen that window is",
            "  # narrow enough to push the Size column outside the captured window.",
            "  $mi = New-Object WinCap+MONITORINFO",
            "  $mi.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf($mi)",
            "  $mon = [WinCap]::MonitorFromWindow($hwnd, 2)",
            "  if ($mon -ne [IntPtr]::Zero -and [WinCap]::GetMonitorInfo($mon, [ref]$mi)) {",
            "    $ww = $mi.rcWork.Right - $mi.rcWork.Left",
            "    $wh = $mi.rcWork.Bottom - $mi.rcWork.Top",
            "    [WinCap]::SetWindowPos($hwnd, [IntPtr]::Zero, $mi.rcWork.Left, $mi.rcWork.Top, $ww, $wh, 0x0004) | Out-Null",
            "  } else {",
            "    [WinCap]::ShowWindow($hwnd, 3) | Out-Null",
            "  }",
            "  # Details view (FVM_DETAILS), otherwise a folder remembered in icon or",
            "  # tile view renders no Size column for the screenshot to show.",
            "  if ($win) { try { $win.Document.CurrentViewMode = 4 } catch {} }",
            "  $topmost = New-Object System.IntPtr(-1)",
            "  $notop = New-Object System.IntPtr(-2)",
            "  [WinCap]::SetWindowPos($hwnd, $topmost, 0, 0, 0, 0, 0x0003) | Out-Null",
            "  [WinCap]::SetForegroundWindow($hwnd) | Out-Null",
            "  Start-Sleep -Milliseconds 800",
            "  $r = New-Object WinCap+RECT",
            "  [WinCap]::GetWindowRect($hwnd, [ref]$r) | Out-Null",
            "  $w2 = $r.Right - $r.Left; $h2 = $r.Bottom - $r.Top",
            "  if ($w2 -gt 0 -and $h2 -gt 0) {",
            "    $bmp = New-Object System.Drawing.Bitmap $w2, $h2",
            "    $g = [System.Drawing.Graphics]::FromImage($bmp)",
            "    $hdc = $g.GetHdc()",
            "    $ok = [WinCap]::PrintWindow($hwnd, $hdc, 2)",
            "    $g.ReleaseHdc($hdc)",
            "    if (-not $ok) { $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w2, $h2))) }",
            "    $bmp.Save($file, [System.Drawing.Imaging.ImageFormat]::Png)",
            "    $g.Dispose(); $bmp.Dispose()",
            "    $saved = $true",
            "  }",
            "  [WinCap]::SetWindowPos($hwnd, $notop, 0, 0, 0, 0, 0x0003) | Out-Null",
            "}",
            "if (-not $saved) {",
            "  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen",
            "  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height",
            "  $g = [System.Drawing.Graphics]::FromImage($bmp)",
            "  $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)",
            "  $bmp.Save($file, [System.Drawing.Imaging.ImageFormat]::Png)",
            "  $g.Dispose(); $bmp.Dispose()",
            "}",
            "Write-Host ('Screenshot saved: ' + $file)",
        ]
        if settings_uri:
            cleanup = [
                "# Close Settings only if this script opened it, leaving a window the",
                "# user already had open untouched. Best-effort.",
                "if ($weOpenedSettings) {",
                "  try { Get-Process -Name SystemSettings -ErrorAction SilentlyContinue | ForEach-Object { $_.CloseMainWindow() | Out-Null } } catch {}",
                "}",
            ]
        else:
            cleanup = [
                "# Close only the Explorer window we opened for this folder, leaving",
                "# any other windows the user has open untouched. Best-effort.",
                "try {",
                "  $shell = New-Object -ComObject Shell.Application",
                "  foreach ($w in @($shell.Windows())) {",
                "    try {",
                "      $p = $w.Document.Folder.Self.Path",
                "      if ($p -and ([System.IO.Path]::GetFullPath($p)).TrimEnd('\\') -ieq $target) { $w.Quit() }",
                "    } catch {}",
                "  }",
                "} catch {}",
            ]
        return "\n".join(head + locate + capture + cleanup + ["exit 0"])

    def _explorer_locate_lines(self, resolve: str, delay: float) -> list:
        """PowerShell that opens a folder in Explorer and resolves its window into
        `$hwnd` (plus `$win`, the Shell window object, and `$target`, the path)."""
        return [
            "$folder = $null",
            "try {",
            "  " + resolve,
            "} catch { $folder = $null }",
            "if (-not $folder) { Write-Error 'Screenshot: could not resolve target folder'; exit 1 }",
            "if (-not (Test-Path -LiteralPath $folder)) { Write-Error ('Screenshot: folder not found: ' + $folder); exit 1 }",
            "Write-Host ('Opening folder: ' + $folder)",
            "Start-Process explorer.exe -ArgumentList ('\"' + $folder + '\"')",
            f"Start-Sleep -Seconds {float(delay)}",
            "$target = ([System.IO.Path]::GetFullPath($folder)).TrimEnd('\\')",
            "# Locate the Explorer window we just opened for this folder and grab",
            "# only its pixels (PrintWindow), so the screenshot is cropped to the",
            "# window instead of capturing the whole desktop. Fall back to a full",
            "# virtual-screen capture if the window can't be resolved.",
            "$hwnd = [IntPtr]::Zero",
            "$win = $null",
            "for ($i = 0; $i -lt 25; $i++) {",
            "  try {",
            "    $shell = New-Object -ComObject Shell.Application",
            "    foreach ($w in @($shell.Windows())) {",
            "      try {",
            "        $p = $w.Document.Folder.Self.Path",
            "        if ($p -and ([System.IO.Path]::GetFullPath($p)).TrimEnd('\\') -ieq $target) { $hwnd = [IntPtr]$w.HWND; $win = $w; break }",
            "      } catch {}",
            "    }",
            "  } catch {}",
            "  if ($hwnd -ne [IntPtr]::Zero) { break }",
            "  Start-Sleep -Milliseconds 200",
            "}",
        ]

    def _settings_locate_lines(self, uri: str, search: str, delay: float) -> list:
        """PowerShell that opens a Windows Settings page, optionally filters it by
        typing into the page's search box, and resolves its window into `$hwnd`."""
        uri_lit = "'" + (uri or "").replace("'", "''") + "'"
        search_lit = "'" + (search or "").replace("'", "''") + "'"
        return [
            "Add-Type -AssemblyName UIAutomationClient",
            "Add-Type -AssemblyName UIAutomationTypes",
            "$win = $null",
            "$target = $null",
            "$weOpenedSettings = -not (Get-Process -Name SystemSettings -ErrorAction SilentlyContinue)",
            f"Write-Host ('Opening Settings: ' + {uri_lit})",
            f"Start-Process {uri_lit}",
            f"Start-Sleep -Seconds {float(delay)}",
            "# Settings runs inside ApplicationFrameHost, so its frame window is found",
            "# by class and then confirmed by the 'Find a setting' box, whose",
            "# AutomationId is stable across Windows display languages.",
            "$hwnd = [IntPtr]::Zero",
            "$root = $null",
            "for ($i = 0; $i -lt 30; $i++) {",
            "  foreach ($h in [WinCap]::ByClass('ApplicationFrameWindow')) {",
            "    try {",
            "      $e = [System.Windows.Automation.AutomationElement]::FromHandle($h)",
            "      $c = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'CommandSearchTextBox')",
            "      if ($e.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $c)) { $hwnd = $h; $root = $e; break }",
            "    } catch {}",
            "  }",
            "  if ($hwnd -ne [IntPtr]::Zero) { break }",
            "  Start-Sleep -Milliseconds 400",
            "}",
            "if ($hwnd -eq [IntPtr]::Zero) { Write-Error 'Screenshot: Settings window not found'; exit 1 }",
            f"$q = {search_lit}",
            "if ($q -and $root) {",
            "  # Filter the page's own list. Any Edit other than the global search box",
            "  # is the in-page filter, so the same step works on other Settings pages.",
            "  $box = $null",
            "  for ($i = 0; $i -lt 20; $i++) {",
            "    try {",
            "      $ec = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)",
            "      foreach ($x in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $ec)) {",
            "        if ($x.Current.AutomationId -ne 'CommandSearchTextBox') { $box = $x; break }",
            "      }",
            "    } catch {}",
            "    if ($box) { break }",
            "    Start-Sleep -Milliseconds 400",
            "  }",
            "  if ($box) {",
            "    try {",
            "      $vp = $box.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)",
            "      $vp.SetValue($q)",
            "      Write-Host ('Filtered Settings by: ' + $q)",
            "      Start-Sleep -Seconds 3",
            "    } catch { Write-Host ('Could not fill the Settings search box: ' + $_.Exception.Message) }",
            "  } else {",
            "    Write-Host 'Settings search box not found; capturing the unfiltered page'",
            "  }",
            "}",
        ]

    def _open_console(self, run_id: str):
        """Open a visible console/terminal window that executes commands."""
        try:
            # Create a directory for this run's console communication
            console_dir = os.path.join(tempfile.gettempdir(), f"test_runner_{run_id}")
            os.makedirs(console_dir, exist_ok=True)

            # Command queue file. The app writes commands here and the console reads them.
            cmd_file = os.path.join(console_dir, "commands.txt")
            done_file = os.path.join(console_dir, "done.txt")

            # Initialize empty command file
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write("")

            # Create a batch script that polls for commands
            script_path = os.path.join(console_dir, "runner.bat")
            exec_file = os.path.join(console_dir, "exec.bat")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write("@echo off\n")
                f.write("%SystemRoot%\\System32\\chcp.com 65001 >nul\n")  # full path, because PATH may lack System32 in the spawned console
                # Snapshot the pristine PATH once. Each command wrapper rebuilds
                # PATH from this baseline so a per-test SDK folder pinned in one
                # test never leaks into a later test that uses the default SDK.
                f.write('set "TESTRUNNER_BASE_PATH=%PATH%"\n')
                f.write(f"title .NET Test Runner - Run {run_id}\n")
                f.write("echo ========================================\n")
                f.write("echo   .NET SDK Test Runner - Live Console\n")
                f.write("echo ========================================\n")
                f.write("echo.\n")
                f.write(":loop\n")
                f.write(f'if exist "{done_file}" goto end\n')
                # Check if commands file has content (size > 0)
                f.write(f'for %%A in ("{cmd_file}") do if %%~zA==0 goto wait\n')
                # Copy commands to exec file and clear the queue
                f.write(f'copy /y "{cmd_file}" "{exec_file}" >nul 2>&1\n')
                f.write(f'type nul > "{cmd_file}"\n')
                # Execute the commands
                f.write(f'call "{exec_file}"\n')
                f.write(":wait\n")
                f.write("timeout /t 1 /nobreak >nul 2>&1\n")
                f.write("goto loop\n")
                f.write(":end\n")
                f.write("echo.\n")
                f.write("echo ========================================\n")
                f.write("echo   Run complete. You may close this window.\n")
                f.write("echo ========================================\n")
                f.write("pause\n")

            # Launch the batch script in a new console window
            subprocess.Popen(
                ["cmd.exe", "/c", script_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )

            # Store the console directory path for communication
            self._console_procs[run_id] = console_dir
        except Exception:
            self._console_procs.pop(run_id, None)

    def _close_console(self, run_id: str):
        """Signal the console that the run is complete, then reclaim its dir."""
        console_dir = self._console_procs.pop(run_id, None)
        if console_dir:
            try:
                done_file = os.path.join(console_dir, "done.txt")
                with open(done_file, "w") as f:
                    f.write("done")
            except Exception:
                pass

            # The console batch is still polling inside this directory and holds
            # runner.bat open, so deleting immediately would break its exit path.
            # Delete it after a short delay, on a thread separate from the run
            # thread. If the window is left open, the delete fails and the
            # startup sweep removes the directory later.
            def _reclaim():
                time.sleep(10)
                _rmtree(console_dir)

            threading.Thread(target=_reclaim, daemon=True).start()

    def _export_results(self, run_id: str):
        """Copy the finished run to the persistent results folder.

        Best-effort. If the share cannot be reached, the log reports it. This
        must never fail the run, because its results are already committed to
        the DB.
        """
        def _line(text):
            self._emit_event(run_id, {
                "type": "step_output", "result_id": None, "step_index": -1, "line": text,
            })

        # Copying to a network share or a cold OneDrive folder is not instant,
        # so log what is happening before blocking on it.
        _line("\n💾 Saving results…")
        # Merge the WAL back into the DB file first. When the DB itself is in
        # OneDrive, the copy that syncs must be self-contained.
        db_store.checkpoint()
        try:
            result = results_export.export_run(run_id, DB_PATH, SCREENSHOTS_DIR)
        except Exception as exc:
            _line(f"[warn] results export failed: {exc}")
            return

        if not result.get("exported"):
            _line(f"[warn] results export failed: {result.get('reason')}")
            return

        _line(f"📁 Results saved to: {result['path']}")
        if not result.get("persistent"):
            _line(
                "[warn] results were saved locally and will be lost with this machine. "
                f"Set {results_export.ENV_ROOT} to a OneDrive folder or network share."
            )

    def _notify_run_complete(self, status: str, counts: dict):
        """Pop a foreground message box when a run finishes (Windows only).

        A test's Notepad or browser window is often left sitting on top of the
        app, so the in-app "Run complete" line and the console are easy to miss
        (issue #67). MB_SETFOREGROUND|MB_TOPMOST puts the alert above whatever
        is on screen, and the icon plays the system sound. This is best-effort
        and runs off the run thread. The box waits for a click, and a
        notification failure must never affect the run's result.
        """
        if sys.platform != "win32":
            return
        done = "cancelled" if status == "cancelled" else "complete"
        text = "\n".join([
            f"Test run {done}.",
            "",
            f"Passed: {counts.get('passed', 0)}",
            f"Passed with warnings: {counts.get('warnings', 0)}",
            f"Failed: {counts.get('failed', 0)}",
            f"Skipped: {counts.get('skipped', 0)}",
        ])
        MB_OK, MB_ICONWARNING, MB_ICONINFORMATION = 0x0, 0x30, 0x40
        MB_SETFOREGROUND, MB_TOPMOST = 0x10000, 0x40000
        icon = MB_ICONWARNING if (counts.get("failed") or status == "cancelled") else MB_ICONINFORMATION
        flags = MB_OK | icon | MB_SETFOREGROUND | MB_TOPMOST

        def _raise_box():
            """Bring the box to the front after it exists.

            MB_SETFOREGROUND alone is not enough when another app (Edge,
            Notepad, a test's browser window) has focus, because of the Windows
            foreground lock. The box then only blinks in the taskbar instead of
            popping up. Poll for the dialog this process just created, force it
            to the front, and confirm that it is the foreground window. The
            first attempt can lose a race with a window that is still settling.
            """
            user32 = ctypes.windll.user32
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    hwnd = _own_dialog_hwnd()
                    if hwnd:
                        _force_foreground(hwnd)
                        if user32.GetForegroundWindow() == hwnd:
                            time.sleep(0.5)
                            if user32.GetForegroundWindow() == hwnd:
                                return
                except Exception:
                    return
                time.sleep(0.25)

        def _show():
            try:
                threading.Thread(target=_raise_box, daemon=True).start()
                ctypes.windll.user32.MessageBoxW(0, text, ".NET SDK Test Runner", flags)
            except Exception:
                pass

        threading.Thread(target=_show, daemon=True).start()

    def cancel_run(self, run_id: str):
        if run_id in self._cancel_flags:
            self._cancel_flags[run_id].set()

    def stream_events(self, run_id: str) -> Generator[dict, None, None]:
        """Yield events for SSE streaming."""
        queue = Queue()
        if run_id not in self._event_queues:
            self._event_queues[run_id] = []
        self._event_queues[run_id].append(queue)

        try:
            while True:
                try:
                    event = queue.get(timeout=30)
                    if event is None:  # None marks the end of the stream
                        break
                    yield event
                except Empty:
                    yield {"type": "heartbeat"}
        finally:
            if run_id in self._event_queues:
                self._event_queues[run_id].remove(queue)

    def _emit_event(self, run_id: str, event: dict):
        if run_id in self._event_queues:
            for queue in self._event_queues[run_id]:
                queue.put(event)

    def _end_stream(self, run_id: str):
        if run_id in self._event_queues:
            for queue in self._event_queues[run_id]:
                queue.put(None)

    def _execute_run(self, run_id: str, tests: List[dict], cancel_flag: threading.Event):
        """Execute all tests in sequence."""
        conn = get_db()
        passed = 0
        failed = 0
        skipped = 0
        warned = 0

        # Per-run folder for screenshots captured by `screenshot` steps, kept
        # next to the DB so results persist and can be browsed per run.
        screenshot_dir = None
        try:
            run_scr = os.path.join(SCREENSHOTS_DIR, run_id)
            os.makedirs(run_scr, exist_ok=True)
            screenshot_dir = run_scr
        except OSError:
            pass

        # Validate a pinned SDK folder (for example, a zip-extracted install) or
        # extract a pinned .zip SDK. If it is unusable, fall back to the PATH
        # default and log a warning. Otherwise a "zip install" test could
        # run against the wrong (exe-installed) SDK without any notice.
        raw_root = self._runs[run_id].get("sdk_path") or None
        root = self._resolve_pinned_root(run_id, raw_root, "SDK folder") if raw_root else None
        if raw_root and not root:
            warn = f"[warn] SDK folder not found or missing dotnet executable: {raw_root}. Falling back to PATH dotnet."
            self._runs[run_id]["sdk_path"] = None
            self._emit_event(run_id, {"type": "step_output", "result_id": None, "step_index": -1, "line": warn})
            self._queue_console_cmd(run_id, f"echo {warn}")
            conn.execute("UPDATE test_runs SET sdk_path=NULL WHERE id=?", (run_id,))
            conn.commit()
        elif root:
            # An extracted zip resolves to a different folder than the user gave.
            if root != raw_root:
                self._runs[run_id]["sdk_path"] = root
                conn.execute("UPDATE test_runs SET sdk_path=? WHERE id=?", (root, run_id))
                conn.commit()
            msg = f"[info] Using pinned SDK install: {root}"
            self._emit_event(run_id, {"type": "step_output", "result_id": None, "step_index": -1, "line": msg})
            self._queue_console_cmd(run_id, f"echo {msg}")

        # Run-level SDK install. Each test defaults to it, but a test may pin its
        # own install folder (test["sdk_path"]) to override it for that test only.
        run_level_root = self._runs[run_id].get("sdk_path") or None

        # Capture environment info
        env_info = self._capture_environment(sdk_source.SdkContext(run_level_root))
        # RID reported by this run's SDK, used by `{rid}` in publish steps.
        rid = _parse_rid(env_info) or _host_rid()
        conn.execute(
            "UPDATE test_runs SET environment_info=? WHERE id=?",
            (env_info, run_id),
        )
        conn.commit()

        # Record the SDK that dotnet really resolves. A pinned version may be
        # gone from the machine, and dotnet then rolls forward with no message.
        # Log what really runs.
        run_pinned = self._runs.get(run_id, {}).get("sdk_version") or None
        actual = sdk_source.resolve_version(run_level_root, run_pinned)
        if actual:
            self._runs[run_id]["sdk_version"] = actual
            conn.execute(
                "UPDATE test_runs SET sdk_version=? WHERE id=?", (actual, run_id)
            )
            conn.commit()
            if run_pinned and actual != run_pinned:
                self._queue_console_cmd(
                    run_id,
                    f"echo [warn] pinned SDK {run_pinned} not installed; running {actual}",
                )

        run_level = sdk_source.SdkContext(run_level_root, self._runs[run_id].get("sdk_version") or None)

        self._emit_event(run_id, {
            "type": "run_start",
            "run_id": run_id,
            "total_tests": len(tests),
            "environment": env_info,
        })
        stale = _stale_sources()
        if stale:
            self._emit_event(run_id, {
                "type": "step_output", "result_id": None, "step_index": -1,
                "line": f"[warn] Changed since the app started: {', '.join(stale)}. "
                        "Restart the app to run the new code.",
            })

        # Continuous chains (issue #49). Tests marked `continuous` that run
        # back-to-back form one chain. They share a single working directory so
        # state carries over (for example, the global.json written by the pin
        # test). If one test fails, the run skips the rest of the chain instead
        # of running it against a half-updated machine. Each test still starts at
        # the chain root, so a `cd` inside one test never moves the next one.
        chain = None

        def close_chain():
            """Finish the current chain and reclaim its shared directory."""
            nonlocal chain
            if not chain:
                return
            if chain["failed"] and not cancel_flag.is_set():
                self._emit_event(run_id, {
                    "type": "step_output", "result_id": None, "step_index": -1,
                    "line": f"[info] Continuous working directory kept for debugging: {chain['dir']}",
                })
            else:
                _rmtree(chain["dir"])
            chain = None

        for test in tests:
            if cancel_flag.is_set():
                skipped += 1
                continue

            test_case_id = test["id"]
            result_id = str(uuid.uuid4())[:12]
            steps = json.loads(test["steps"]) if isinstance(test["steps"], str) else test["steps"]
            continuous = bool(test.get("is_continuous"))

            if not continuous:
                close_chain()
            elif chain is None:
                chain = {"dir": tempfile.mkdtemp(prefix="dotnet_test_"), "failed": False}

            conn.execute(
                "INSERT INTO test_results (id, run_id, test_case_id, status, started_at) VALUES (?, ?, ?, ?, ?)",
                (result_id, run_id, test_case_id, "running", datetime.now().isoformat()),
            )
            conn.commit()

            self._emit_event(run_id, {
                "type": "test_start",
                "test_case_id": test_case_id,
                "title": test["title"],
                "result_id": result_id,
            })

            # An earlier test in this chain failed. The machine never reached the
            # state that the rest of the chain depends on, so skip those tests
            # instead of reporting cascading failures.
            if chain and chain["failed"]:
                msg = "[skip] Earlier test in this continuous sequence failed."
                self._emit_event(run_id, {
                    "type": "step_output", "result_id": result_id, "step_index": -1,
                    "line": msg,
                })
                self._queue_console_cmd(run_id, f"echo {msg}")
                conn.execute(
                    "UPDATE test_results SET status=?, finished_at=? WHERE id=?",
                    ("skipped", datetime.now().isoformat(), result_id),
                )
                conn.commit()
                skipped += 1
                self._emit_event(run_id, {
                    "type": "test_end",
                    "test_case_id": test_case_id,
                    "result_id": result_id,
                    "status": "skipped",
                })
                continue

            # Send test header to console with spacing
            title = test["title"]
            # Title is arbitrary text echoed into a cmd batch. Escape the cmd
            # metacharacters that would otherwise split the line (for example,
            # "& .NET Standard"). Escape the caret first so we don't double-escape
            # the carets we add.
            for ch in "^&<>|()":
                title = title.replace(ch, "^" + ch)
            self._queue_console_cmd(run_id, "echo.")
            self._queue_console_cmd(run_id, f"echo ===== {title} =====")
            self._queue_console_cmd(run_id, "echo.")

            # Resolve this test's SDK context. A valid per-test folder overrides
            # the run-level one for this test only. Otherwise, the run-level one applies.
            test_root = (test.get("sdk_path") or "").strip() or None
            if test_root:
                resolved = self._resolve_pinned_root(run_id, test_root, "Test SDK folder")
                if not resolved:
                    self._emit_event(run_id, {
                        "type": "step_output", "result_id": result_id, "step_index": -1,
                        "line": f"[warn] Test SDK folder not found or missing dotnet: {test_root}. Using run default.",
                    })
                    self._queue_console_cmd(run_id, f"echo [warn] test SDK folder invalid: {test_root}; using run default")
                test_root = resolved
            if test_root and test_root != run_level.root:
                # Resolve the version from this test's install, for {tfm} and global.json.
                test_sdk = sdk_source.SdkContext(
                    test_root, sdk_source.resolve_version(test_root, run_pinned))
                self._emit_event(run_id, {
                    "type": "step_output", "result_id": result_id, "step_index": -1,
                    "line": f"[info] Test pinned to SDK install: {test_root}",
                })
                self._queue_console_cmd(run_id, f"echo [info] test pinned to SDK install: {test_root}")
            else:
                test_sdk = run_level

            ctx = StepContext(
                cwd="", sdk=test_sdk, test_name=test.get("title") or test_case_id, rid=rid,
                # The workload set version that the tester picked for this test in the
                # editor (issue #49). The versions exist only on the workloads feed, so
                # the code cannot derive the value.
                workload_version=(test.get("workload_version") or "").strip(),
                assets_dir=ASSETS_DIR, screenshot_dir=screenshot_dir, cancel=cancel_flag,
            )
            test_passed, test_warned = self._execute_test(
                run_id, result_id, steps, ctx, conn,
                shared_dir=chain["dir"] if chain else None,
            )

            if cancel_flag.is_set():
                status = "cancelled"
                skipped += 1
            elif test_passed and test_warned:
                status = "passed_with_warnings"
                warned += 1
            elif test_passed:
                status = "passed"
                passed += 1
            else:
                status = "failed"
                failed += 1

            if chain and status == "failed":
                chain["failed"] = True

            conn.execute(
                "UPDATE test_results SET status=?, finished_at=? WHERE id=?",
                (status, datetime.now().isoformat(), result_id),
            )
            conn.commit()

            self._emit_event(run_id, {
                "type": "test_end",
                "test_case_id": test_case_id,
                "result_id": result_id,
                "status": status,
            })

        close_chain()

        # Finalize run
        final_status = "completed" if not cancel_flag.is_set() else "cancelled"
        summary = json.dumps({"passed": passed, "failed": failed, "skipped": skipped, "warnings": warned})
        conn.execute(
            "UPDATE test_runs SET status=?, finished_at=?, summary=? WHERE id=?",
            (final_status, datetime.now().isoformat(), summary, run_id),
        )
        conn.commit()
        conn.close()

        # Export before the run_end event. The client closes the SSE stream as
        # soon as run_end arrives, so anything emitted after it never renders.
        # The app usually runs on a VM that is destroyed after the build, so
        # this is the only copy of the results that survives.
        self._export_results(run_id)

        self._emit_event(run_id, {
            "type": "run_end",
            "status": final_status,
            "summary": {"passed": passed, "failed": failed, "skipped": skipped, "warnings": warned},
        })
        self._end_stream(run_id)

        # Cleanup
        self._cancel_flags.pop(run_id, None)
        self._close_console(run_id)
        self._notify_run_complete(
            final_status,
            {"passed": passed, "failed": failed, "skipped": skipped, "warnings": warned},
        )

    def _execute_test(
        self, run_id: str, result_id: str, steps: List[dict], ctx: StepContext,
        conn: sqlite3.Connection, shared_dir: str = None
    ) -> tuple:
        """Run one test case in a temp working directory, then clean it up.

        This method removes the directory when the test passes. When the test
        fails, it keeps the directory on purpose so you can inspect the build
        output, and the run log shows the path. The startup sweep reclaims it
        later. A cancelled test keeps nothing, because its directory is usually
        empty.

        shared_dir, when given, is a continuous chain's working directory. Every
        test in the chain reuses it and the caller owns it, so this test
        neither creates nor removes it.
        """
        work_dir = shared_dir or tempfile.mkdtemp(prefix="dotnet_test_")
        try:
            all_passed, had_warnings = self._execute_test_steps(
                run_id, result_id, steps, replace(ctx, cwd=work_dir), conn
            )
        except BaseException:
            if not shared_dir:
                _rmtree(work_dir)
            raise

        if shared_dir:
            return (all_passed, had_warnings)

        if all_passed or ctx.cancel.is_set():
            _rmtree(work_dir)
        else:
            self._emit_event(run_id, {
                "type": "step_output",
                "result_id": result_id,
                "step_index": -1,
                "line": f"[info] Working directory kept for debugging: {work_dir}",
            })
        return (all_passed, had_warnings)

    def _execute_test_steps(
        self, run_id: str, result_id: str, steps: List[dict], ctx: StepContext,
        conn: sqlite3.Connection,
    ) -> tuple:
        """Run the Steps of one Test case and record each Step outcome once.

        ctx.cwd is the Test case's working folder. A cd Step moves it for the
        Steps after it. Returns (all_passed, had_warnings). had_warnings is True
        if any Step passed with an MSBuild/NuGet-style warning (for example,
        the NU1903 vulnerability warning).
        """
        def record(idx: int, outcome: StepOutcome):
            conn.execute(
                """INSERT INTO step_results (id, test_result_id, step_index, step_type, command, exit_code, stdout, stderr, status, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4())[:12], result_id, idx, outcome.step_type, outcome.label,
                 outcome.exit_code, outcome.stdout[:10000], outcome.stderr[:5000],
                 outcome.status, outcome.duration_ms),
            )
            conn.commit()
            self._emit_event(run_id, {
                "type": "step_end", "result_id": result_id, "step_index": idx,
                "status": outcome.status, "exit_code": outcome.exit_code,
                "duration_ms": outcome.duration_ms,
            })

        # Fail fast when a step needs a workload set version that was never set.
        # An empty string would run, for example, `dotnet workload update
        # --version ` and fail with an unrelated CLI parse error.
        if not ctx.workload_version and any(
            "{workload_version}" in json.dumps(step) for step in steps
        ):
            msg = ("[error] This test needs a workload set version. Open it in the "
                   "editor, set \"Workload set version\" (e.g. from `dotnet workload "
                   "search version`), then run it again.")
            self._step_log(run_id, result_id, -1)(msg)
            record(0, StepOutcome(FAILED, "command", "workload set version required",
                                  exit_code=-1, stdout=msg))
            return (False, False)

        ctx.sdk.write_global_json(ctx.cwd)

        direct = DirectShell(ctx)
        console_dir = self._console_procs.get(run_id)
        shell = ConsoleShell(console_dir, ctx, fallback=direct) if console_dir else direct
        desktop = _RunDesktop(self, run_id)

        all_passed = True
        had_warnings = False
        for idx, step in enumerate(steps):
            if ctx.cancel.is_set():
                return (False, had_warnings)
            outcome = run_step(step, replace(ctx, log=self._step_log(run_id, result_id, idx)),
                               shell, desktop)
            if outcome.status == CANCELLED:
                return (False, had_warnings)
            record(idx, outcome)
            if outcome.new_cwd:
                ctx = replace(ctx, cwd=outcome.new_cwd)
            if outcome.status == FAILED:
                all_passed = False
            elif outcome.status == WARNING:
                had_warnings = True
            if outcome.stops_test:
                break
        return (all_passed, had_warnings)

    def _step_log(self, run_id: str, result_id: str, idx: int):
        """The log callback for one Step: each line becomes a step_output event."""
        def log(line: str, is_command: bool = False):
            event = {"type": "step_output", "result_id": result_id, "step_index": idx, "line": line}
            if is_command:
                event["is_command"] = True
            self._emit_event(run_id, event)
        return log

    def _show_in_notepad(self, filepath: str, screenshot_path: Optional[str]) -> Optional[str]:
        """Open a written file in Notepad so it shows, then close Notepad.
        If screenshot_path is given, capture the Notepad window into that path
        before closing, as a checkpoint record.

        Uses plain subprocess instead of GUI automation libraries. Those libraries
        are unreliable in a windowed PyInstaller build (and may not be bundled).
        Returns None when it worked, else the failure reason.
        """
        notepad_exe = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe"
        )
        try:
            # Redirect all standard streams. A windowed (console=False) build has
            # no valid stdin handle, so an un-redirected launch fails outright.
            proc = subprocess.Popen(
                [notepad_exe, filepath],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            return f"could not launch notepad: {type(e).__name__}: {e}"

        try:
            # Give Notepad a moment to start. _capture_screen then polls for its
            # window handle and captures that window directly. This works even if
            # the window never gets foreground focus.
            time.sleep(0.4)
            if screenshot_path:
                return self._capture_screen(screenshot_path, pid=proc.pid,
                                            title_hint=os.path.basename(filepath),
                                            maximize=True)
            # Brief pause so the user can see the file content.
            time.sleep(0.6)
            return None
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        finally:
            # Close the Notepad window that this code opened. The file is
            # already saved, so Notepad has nothing to ask about.
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _queue_console_cmd(self, run_id: str, command: str):
        """Write a command to the console's command queue file."""
        console_dir = self._console_procs.get(run_id)
        if not console_dir:
            return
        try:
            cmd_file = os.path.join(console_dir, "commands.txt")
            with open(cmd_file, "a", encoding="utf-8") as f:
                f.write(command + "\n")
        except Exception:
            pass

    def _run_long_running(self, run_id: str, cmd: str, step: dict, ctx: StepContext) -> tuple:
        """
        Start a long-running server command, stream its output live to both the app
        panel and the popup console, wait for a readiness pattern, optionally
        HTTP-verify the hosted site, then terminate. Returns (stdout_text, exit_code).

        This code starts the process, so it can kill it by PID. It writes each
        captured line to a log file. The popup console tails that file live with a small
        PowerShell script, so the console streams the same output as the panel in
        real time.
        """
        cwd = ctx.cwd
        cancel_flag = ctx.cancel
        timeout = step.get("timeout", 120)
        ready_patterns = step.get("ready_pattern") or []
        if isinstance(ready_patterns, str):
            ready_patterns = [ready_patterns]
        verify_url = step.get("verify_url")
        contains = step.get("verify_contains")
        if isinstance(contains, str):
            contains = [contains]
        # After a site is confirmed up, optionally open it in the browser and keep
        # the server running so a person can look at it (like the Notepad file preview).
        open_in_browser = step.get("open_in_browser", True)
        hold_seconds = step.get("hold_seconds", 10)

        console_dir = self._console_procs.get(run_id)
        live = bool(console_dir)

        lines = []
        ready = threading.Event()
        log_fh = None
        done_flag = None

        if live:
            srv_log = os.path.join(console_dir, f"srv_{uuid.uuid4().hex[:8]}.log")
            done_flag = srv_log + ".done"
            for p in (srv_log, done_flag):
                if os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            try:
                log_fh = open(srv_log, "a", encoding="utf-8")
            except OSError:
                log_fh = None
            if log_fh:
                tail_ps1 = self._write_tail_script(console_dir)
                self._queue_console_cmd(
                    run_id,
                    f'"{self._powershell_exe()}" -NoProfile -ExecutionPolicy Bypass -File "{tail_ps1}" "{srv_log}" "{done_flag}"',
                )

        def write_log(text):
            if log_fh:
                try:
                    log_fh.write(text)
                    log_fh.flush()
                except (OSError, ValueError):
                    pass

        write_log(f"\n{cwd}> {cmd}\n")

        def finish(exit_code):
            if log_fh:
                try:
                    log_fh.close()
                except OSError:
                    pass
            if done_flag:
                # Signals the console tailer to flush the rest and exit.
                try:
                    open(done_flag, "w").close()
                except OSError:
                    pass
            return (render_terminal_output("".join(lines)), exit_code)

        env = ctx.env()
        env["DOTNET_CLI_COLORS"] = "1"
        env["FORCE_COLOR"] = "1"

        try:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env,
            )
        except Exception as e:
            lines.append(str(e))
            write_log(str(e))
            return finish(1)

        def reader():
            for line in proc.stdout:
                lines.append(line)
                write_log(line)
                ctx.log(line.rstrip("\n"))
                if not ready.is_set() and any(p in line for p in ready_patterns):
                    ready.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        # Timed run for blocking GUI apps (WinForms/WPF). There is no readiness
        # pattern. Let the window render for `run_timeout` seconds, then auto-close
        # it and pass. An early exit means the app quit on its own. In that case,
        # use its exit code so a build/runtime failure (nonzero) is still reported
        # as a failure.
        run_timeout = step.get("run_timeout")
        if run_timeout:
            # Once the GUI window has had a moment to render, capture it once and
            # log that it was displayed (issue #45). The window closes by itself,
            # so without this there is no record that it appeared.
            gui_shot_done = False
            gui_shot_at = time.time() + min(5.0, max(1.0, run_timeout - 1))
            deadline = time.time() + run_timeout
            while time.time() < deadline:
                if cancel_flag.is_set():
                    self._terminate_proc(proc)
                    t.join(timeout=2)
                    return finish(-1)
                if proc.poll() is not None:
                    t.join(timeout=2)
                    return finish(proc.returncode or 0)
                if (not gui_shot_done and step.get("screenshot")
                        and time.time() >= gui_shot_at):
                    gui_shot_done = True
                    self._capture_gui_window(ctx, proc.pid, write_log, lines, step)
                time.sleep(0.2)
            self._terminate_proc(proc)
            t.join(timeout=2)
            return finish(0)

        # Wait for readiness, process exit, timeout, or cancel.
        deadline = time.time() + timeout
        while not ready.is_set() and proc.poll() is None:
            if cancel_flag.is_set():
                self._terminate_proc(proc)
                t.join(timeout=2)
                return finish(-1)
            if time.time() > deadline:
                self._terminate_proc(proc)
                t.join(timeout=2)
                msg = "\n[timeout waiting for readiness]\n"
                lines.append(msg)
                write_log(msg)
                return finish(1)
            time.sleep(0.2)

        # If the process dies before it is ready, the step fails (build or run error).
        if not ready.is_set():
            t.join(timeout=2)
            return finish(proc.returncode or 1)

        exit_code = 0
        if verify_url:
            ok, status, body, err = self._verify_site(verify_url, contains)
            msg = f"GET {verify_url} -> HTTP {status}" + (f" | {err}" if err else "")
            ctx.log(("✅ " if ok else "❌ ") + msg)
            log_msg = "\n" + ("[OK] " if ok else "[FAIL] ") + msg + "\n"
            lines.append(log_msg)
            write_log(log_msg)
            if not ok:
                lines.append(body[:5000])
                write_log(body[:5000])
                exit_code = 1

        # Open the live site for visual verification. Keep the server up for a
        # short hold so a person can see it, then continue the automation.
        if verify_url and open_in_browser:
            try:
                webbrowser.open(verify_url)
            except Exception:
                pass
            # Optionally capture a checkpoint screenshot of the browser window
            # showing the hosted site (Windows only) into the run's screenshot
            # folder so it appears in the Screenshots tab. Give the page a moment
            # to render, then grab the browser window (cropped).
            if step.get("screenshot"):
                shot_deadline = time.time() + 4
                while time.time() < shot_deadline and not cancel_flag.is_set():
                    time.sleep(0.2)
                hostport = re.sub(r"^https?://", "", verify_url)
                shot_path = ctx.screenshot_path(step.get("label") or hostport, "browser")
                if shot_path:
                    # Match the browser window by its host:port (the tab title for
                    # the minimal web/webapi pages is the URL). For pages with a
                    # custom title (for example, the MVC "Home Page"), fall back to
                    # the foreground window (the browser we just opened).
                    hint = hostport.split("/")[0]
                    err = self._capture_screen(
                        shot_path, title_hint=hint, foreground_fallback=True,
                        exclude_title=OWN_WINDOW_TITLE,
                    )
                    if not err:
                        msg = f"📸 Browser screenshot saved: {os.path.basename(shot_path)}"
                        lines.append("\n" + msg + "\n")
                        write_log("\n" + msg + "\n")
                    else:
                        ctx.log(f"[warn] Browser screenshot failed: {err}")
            hold_deadline = time.time() + hold_seconds
            while time.time() < hold_deadline and not cancel_flag.is_set():
                time.sleep(0.2)

        self._terminate_proc(proc)
        t.join(timeout=2)
        return finish(exit_code)

    def _write_tail_script(self, console_dir: str) -> str:
        """Write a PowerShell script that live-tails a growing log file to the
        console and exits once a done-flag file appears. Writing it again is safe."""
        path = os.path.join(console_dir, "tail.ps1")
        script = (
            'param([string]$Path, [string]$DoneFlag)\n'
            '$pos = 0\n'
            'while ($true) {\n'
            '  if (Test-Path -LiteralPath $Path) {\n'
            '    try { $c = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop } catch { $c = $null }\n'
            '    if ($c -and $c.Length -gt $pos) { [Console]::Out.Write($c.Substring($pos)); $pos = $c.Length }\n'
            '  }\n'
            '  if (Test-Path -LiteralPath $DoneFlag) { break }\n'
            '  Start-Sleep -Milliseconds 150\n'
            '}\n'
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
        except OSError:
            pass
        return path

    def _verify_site(self, url: str, contains) -> tuple:
        """One HTTP GET, with up to 2 tries. Returns (ok, status, body, err)."""
        # The dev HTTPS cert is self-signed, so skip TLS verification.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        last_err = ""
        # Try twice, 1 second apart. The server may need a short time after
        # "Now listening on". If this check fails often, increase the range.
        for attempt in range(2):
            try:
                with urllib.request.urlopen(url, timeout=10, context=ctx) as resp:
                    status = getattr(resp, "status", resp.getcode())
                    body = resp.read().decode("utf-8", "replace")
                ok = 200 <= status < 300
                missing = [s for s in (contains or []) if s not in body]
                if ok and not missing:
                    return (True, status, body, "")
                err = f"missing {missing}" if missing else f"unexpected status {status}"
                return (False, status, body, err)
            except Exception as e:
                last_err = str(e)
                time.sleep(1)
        return (False, 0, "", last_err)

    def _terminate_proc(self, proc: subprocess.Popen):
        """Kill the process and its children."""
        try:
            # taskkill /T kills the dotnet child that shell=True spawns.
            # proc.terminate() alone leaves the server listening.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _capture_environment(self, sdk: sdk_source.SdkContext) -> str:
        try:
            result = subprocess.run(
                [sdk.dotnet_exe(), "--info"], capture_output=True, text=True, timeout=30,
                env=sdk.env(),
            )
            return result.stdout
        except Exception as e:
            return f"Could not capture environment: {e}"
