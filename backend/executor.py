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
import urllib.request
import webbrowser
from datetime import datetime
from queue import Queue, Empty
from typing import Dict, List, Generator

# Read-only fixtures shipped with the app (e.g. sample projects that tests
# copy into their work dir). Resolves under PyInstaller's _MEIPASS when bundled.
ASSETS_DIR = os.path.join(
    getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__)),
    "test_assets",
)


def _render_terminal_output(text: str) -> str:
    """Collapse a terminal-logger animation stream into the final rendered
    screen a real console would show, preserving SGR colors.

    The .NET SDK's modern terminal logger (forced on via MSBUILDTERMINALLOGGER)
    emits cursor-movement escapes to animate progress in place. Captured to a
    file those frames just pile up, so we replay them through a VT emulator and
    read back the final screen.

    only kicks in when cursor-control escapes are present (the
    telltale `\\x1b[?25` show/hide-cursor codes the logger always emits), so
    plain command output passes through untouched and never gets reflowed or
    truncated to the emulator width.
    """
    if "\x1b[?25" not in text:
        return text
    try:
        import pyte
    except ImportError:
        return text

    named = {"black": 0, "red": 1, "green": 2, "brown": 3,
             "blue": 4, "magenta": 5, "cyan": 6, "white": 7}

    def sgr(char) -> str:
        parts = []
        if char.bold:
            parts.append("1")
        if char.fg != "default":
            parts.append(str(30 + named.get(char.fg, 9)))
        if char.bg != "default":
            parts.append(str(40 + named.get(char.bg, 9)))
        return ";".join(parts)

    def render_line(buf) -> str:
        cols = (max(buf) + 1) if buf else 0
        out, prev = "", ""
        for col in range(cols):
            char = buf[col]
            code = sgr(char)
            if code != prev:
                out += "\x1b[0m" + (f"\x1b[{code}m" if code else "")
                prev = code
            out += char.data
        if prev:
            out += "\x1b[0m"
        return out.rstrip()

    screen = pyte.HistoryScreen(200, 50, history=5000)
    # Windows consoles treat LF as CR+LF (move to column 0); pyte
    # defaults to Unix bare LF, which mangles cursor-repositioned output. LNM
    # matches the real console the app is imitating.
    screen.set_mode(pyte.modes.LNM)
    pyte.Stream(screen).feed(text)
    rows = list(screen.history.top) + [screen.buffer[i] for i in range(screen.lines)]
    lines = [render_line(buf) for buf in rows]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _get_db_path():
    if getattr(sys, '_MEIPASS', None):
        return os.path.join(os.path.dirname(sys.executable), "test_runner.db")
    return os.path.join(os.path.dirname(__file__), "test_runner.db")


DB_PATH = _get_db_path()


def _get_screenshots_dir():
    """Root folder for run screenshots, kept next to the DB so it persists in
    both dev and PyInstaller-frozen runs."""
    return os.path.join(os.path.dirname(DB_PATH), "screenshots")


SCREENSHOTS_DIR = _get_screenshots_dir()


# Temp artifacts created per test/run. They are removed as soon as they are no
# longer needed, but a crash or a hard kill can still strand them, and working
# directories for failed tests are deliberately retained for debugging. The
# startup sweep bounds that growth.
TEMP_PREFIXES = ("dotnet_test_", "test_runner_", "notepad_shot_", "sdk_resolve_",
                 "gui_shot_")
TEMP_MAX_AGE_HOURS = 24 * 7


def _rmtree(path: str, retries: int = 3) -> bool:
    """Best-effort recursive delete.

    On Windows a just-terminated dotnet/MSBuild process can briefly keep a
    handle open, so a first attempt may fail with a sharing violation; retry a
    few times before giving up. Never raises — cleanup must not fail a test.
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


# Matches MSBuild/NuGet/Roslyn diagnostic warnings, e.g. "warning NU1903:",
# "warning CS0168:", "warning MSB3277:", "warning NETSDK1138:". These indicate a
# step succeeded but emitted a diagnostic worth surfacing (e.g. a package with a
# known vulnerability), so the test is reported as "passed with warnings".
_WARNING_RE = re.compile(r"\bwarning\s+[A-Za-z]{2,}[0-9]+\s*:", re.IGNORECASE)


def _detect_warnings(text: str) -> bool:
    """Return True if the output contains an MSBuild/NuGet-style warning line."""
    if not text:
        return False
    return bool(_WARNING_RE.search(text))


# Title fragment common to this app's own windows: the Edge app window
# (".NET SDK Test Runner") and the live console (".NET Test Runner - Run <id>").
# Screenshot capture excludes them so a run never photographs the runner itself.
# Matching the full ".NET SDK Test Runner" missed the console, whose title has no
# "SDK", which is how the console ended up in GUI-window screenshots.
OWN_WINDOW_TITLE = "Test Runner"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class TestExecutor:
    def __init__(self):
        self._runs: Dict[str, dict] = {}
        self._event_queues: Dict[str, List[Queue]] = {}
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._console_procs: Dict[str, str] = {}  # run_id -> console_dir path

    def start_run(self, run_id: str, tests: List[dict], sdk_version: str = None, sdk_path: str = None):
        """Start executing tests in a background thread.

        sdk_path, when provided, is the install root (folder containing dotnet.exe)
        of a specific .NET SDK — e.g. a zip-extracted SDK. All dotnet commands for
        the run are executed against that install (via DOTNET_ROOT + PATH), so
        zip/file-based workload installs are exercised instead of the PATH default.
        """
        cancel_flag = threading.Event()
        self._cancel_flags[run_id] = cancel_flag
        sdk_path = (sdk_path or "").strip() or None
        self._runs[run_id] = {"status": "running", "tests": tests, "sdk_version": sdk_version, "sdk_path": sdk_path}
        self._event_queues[run_id] = []

        self._open_console(run_id)

        conn = get_db()
        conn.execute(
            "INSERT INTO test_runs (id, started_at, status, sdk_version, sdk_path) VALUES (?, ?, ?, ?, ?)",
            (run_id, datetime.now().isoformat(), "running", sdk_version, sdk_path),
        )
        conn.commit()
        conn.close()

        thread = threading.Thread(
            target=self._execute_run, args=(run_id, tests, cancel_flag), daemon=True
        )
        thread.start()

    def _dotnet_root(self, run_id: str):
        """Return the SDK install root in effect right now, or None.

        A per-test override (set while a test with its own ``sdk_path`` runs)
        takes precedence over the run-level folder.
        """
        run = self._runs.get(run_id, {}) or {}
        if "_active_sdk_path" in run:
            return run["_active_sdk_path"] or None
        return run.get("sdk_path") or None

    def _dotnet_exe(self, root: str) -> str:
        return os.path.join(root, "dotnet.exe")

    def _valid_dotnet_root(self, root: str) -> bool:
        return bool(root) and os.path.isfile(self._dotnet_exe(root))

    def _sdk_env(self, run_id: str, base: dict = None) -> dict:
        """Copy of the environment with dotnet pointed at the run's SDK install.

        When the run pins an SDK folder, prepend it to PATH, set DOTNET_ROOT, and
        disable multi-level lookup so only that install (its workload install type)
        is used. Otherwise returns the environment unchanged.
        """
        env = dict(base) if base is not None else os.environ.copy()
        scr = self._screenshot_dir(run_id)
        if scr:
            env["SCREENSHOT_DIR"] = scr
        root = self._dotnet_root(run_id)
        if not root:
            return env
        env["DOTNET_ROOT"] = root
        env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
        env["PATH"] = root + ";" + env.get("PATH", "")
        return env

    def _screenshot_dir(self, run_id: str):
        """Per-run folder where screenshots for this run are saved, or None."""
        return (self._runs.get(run_id, {}) or {}).get("_screenshot_dir") or None

    def _powershell_exe(self) -> str:
        """Full path to Windows PowerShell — the spawned console's PATH may lack
        System32, so a bare `powershell` name isn't reliably found."""
        return os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )

    def _capture_screen(self, out_path: str, pid: int = None, title_hint: str = None,
                        foreground_fallback: bool = False, exclude_title: str = None,
                        maximize: bool = False, descendant_of_pid: int = None,
                        window_only: bool = False) -> bool:
        """Capture a screenshot to out_path (PNG). Windows only.

        The window's own pixels are read with PrintWindow + PW_RENDERFULLCONTENT,
        which pulls from the window's DWM buffer. That is occlusion-proof: an
        overlapping window (notably this app's own live console) cannot bleed into
        the shot. A CopyFromScreen of the window rectangle is only a fallback for
        when PrintWindow fails or returns an empty bitmap, since it captures
        whatever happens to be physically on screen.

        When pid is given, capture that process's main window. When the pid's
        window handle can't be resolved (e.g. Windows 11 Notepad launches via a
        stub process, or a browser opens a tab in an already-running process),
        fall back to locating a top-level window whose title contains title_hint.
        When foreground_fallback is set, use the current foreground window if
        neither pid nor title match (useful for browsers, which bring themselves
        to the front). Falls back to a full virtual-screen capture only if no
        window can be resolved and window_only is not set. Best-effort: returns
        True only if the PNG was written; the failure reason is stored in
        self._last_capture_error for the caller to log."""
        self._last_capture_error = None
        if sys.platform != "win32":
            self._last_capture_error = "not supported on non-Windows"
            return False
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
            # PyInstaller build has no valid stdin handle, so leaving it to be
            # inherited makes CreateProcess fail with WinError 6; that is why the
            # screenshot silently never appeared. CREATE_NO_WINDOW keeps the
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
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", "replace").strip()
                self._last_capture_error = err[:500] or f"powershell exit code {r.returncode}"
            ok = os.path.isfile(out_path)
            if not ok and not self._last_capture_error:
                self._last_capture_error = "screenshot file was not created"
            return ok
        except Exception as e:
            self._last_capture_error = f"{type(e).__name__}: {e}"
            return False
        finally:
            if script_path:
                try:
                    os.unlink(script_path)
                except OSError:
                    pass

    def _capture_gui_window(self, run_id, result_id, idx, root_pid, test_name, cwd,
                            write_log, lines, step) -> None:
        """Capture the WinForms/WPF window spawned by a `dotnet run` GUI step and
        log that it was displayed (issue #45). The window belongs to a descendant
        of the launched shell process, so it is resolved by walking the process
        tree. Best-effort: emits a log line on success, a warning on failure."""
        def _slug(s):
            return re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "")).strip("-")
        scr = self._screenshot_dir(run_id)
        shot_path = None
        if scr:
            # Use the project directory (e.g. `w`, `mywpf`) so the WinForms and
            # WPF shots within one test get distinct names.
            label = step.get("label") or os.path.basename(os.path.normpath(cwd or "")) or "gui-window"
            base = "-".join(p for p in (_slug(test_name), _slug(label)) if p) or "gui-window"
            shot_path = os.path.join(scr, f"{base}-{time.strftime('%Y%m%d-%H%M%S')}.png")
        target = shot_path or os.path.join(tempfile.gettempdir(), f"gui_shot_{idx}.png")
        saved = self._capture_screen(
            target, descendant_of_pid=root_pid, exclude_title=OWN_WINDOW_TITLE,
            window_only=True,
        )
        if saved and shot_path and os.path.isfile(shot_path):
            msg = f"🖼️ GUI window displayed; screenshot: {os.path.basename(shot_path)}"
        elif saved:
            msg = "🖼️ GUI window displayed"
        else:
            reason = getattr(self, "_last_capture_error", None) or "window not detected"
            msg = f"[warn] GUI window screenshot failed: {reason}"
        self._emit_event(run_id, {
            "type": "step_output", "result_id": result_id, "step_index": idx, "line": msg,
        })
        write_log("\n" + msg + "\n")
        lines.append("\n" + msg + "\n")

    def _build_screenshot_script(self, label: str, folder: str, folder_ps: str, delay: float, test_name: str = "") -> str:
        """Generate a PowerShell script that opens a folder in Explorer and saves
        a screenshot of all monitors into %SCREENSHOT_DIR%. Written to a .ps1 file
        (not inline) so quoting is unaffected by the cmd console. The saved file is
        named after the test (and label), e.g. `Workload-Scenario-1-installertype-<ts>.png`."""
        def _slug(s):
            return re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "")).strip("-")
        parts = [p for p in (_slug(test_name), _slug(label)) if p]
        base = "-".join(parts) or "screenshot"
        if folder_ps:
            resolve = "$folder = (& { " + folder_ps.strip() + " } | Select-Object -Last 1)"
        else:
            resolve = '$folder = "' + (folder or "").replace('"', '`"') + '"'
        return "\n".join([
            "$ErrorActionPreference = 'Stop'",
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
            "  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }",
            "}",
            '"@',
            "$folder = $null",
            "try {",
            "  " + resolve,
            "} catch { $folder = $null }",
            "if (-not $folder) { Write-Error 'Screenshot: could not resolve target folder'; exit 1 }",
            "if (-not (Test-Path -LiteralPath $folder)) { Write-Error ('Screenshot: folder not found: ' + $folder); exit 1 }",
            "Write-Host ('Opening folder: ' + $folder)",
            "Start-Process explorer.exe -ArgumentList ('\"' + $folder + '\"')",
            f"Start-Sleep -Seconds {float(delay)}",
            "$dir = $env:SCREENSHOT_DIR",
            "if (-not $dir) { $dir = $env:TEMP }",
            "New-Item -ItemType Directory -Force -Path $dir | Out-Null",
            "$target = ([System.IO.Path]::GetFullPath($folder)).TrimEnd('\\')",
            "# Locate the Explorer window we just opened for this folder and grab",
            "# only its pixels (PrintWindow), so the screenshot is cropped to the",
            "# window instead of capturing the whole desktop. Fall back to a full",
            "# virtual-screen capture if the window can't be resolved.",
            "$hwnd = [IntPtr]::Zero",
            "for ($i = 0; $i -lt 25; $i++) {",
            "  try {",
            "    $shell = New-Object -ComObject Shell.Application",
            "    foreach ($w in @($shell.Windows())) {",
            "      try {",
            "        $p = $w.Document.Folder.Self.Path",
            "        if ($p -and ([System.IO.Path]::GetFullPath($p)).TrimEnd('\\') -ieq $target) { $hwnd = [IntPtr]$w.HWND; break }",
            "      } catch {}",
            "    }",
            "  } catch {}",
            "  if ($hwnd -ne [IntPtr]::Zero) { break }",
            "  Start-Sleep -Milliseconds 200",
            "}",
            "$saved = $false",
            f"$file = Join-Path $dir ('{base}-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.png')",
            "if ($hwnd -ne [IntPtr]::Zero) {",
            "  [WinCap]::ShowWindow($hwnd, 9) | Out-Null",
            "  $topmost = New-Object System.IntPtr(-1)",
            "  $notop = New-Object System.IntPtr(-2)",
            "  [WinCap]::SetWindowPos($hwnd, $topmost, 0, 0, 0, 0, 0x0003) | Out-Null",
            "  [WinCap]::SetForegroundWindow($hwnd) | Out-Null",
            "  Start-Sleep -Milliseconds 400",
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
            "exit 0",
        ])

    def _open_console(self, run_id: str):
        """Open a visible console/terminal window that executes commands."""
        try:
            # Create a directory for this run's console communication
            console_dir = os.path.join(tempfile.gettempdir(), f"test_runner_{run_id}")
            os.makedirs(console_dir, exist_ok=True)

            # Command queue file — we write commands here, console reads them
            cmd_file = os.path.join(console_dir, "commands.txt")
            done_file = os.path.join(console_dir, "done.txt")

            # Initialize empty command file
            with open(cmd_file, "w", encoding="utf-8") as f:
                f.write("")

            if sys.platform == "win32":
                # Create a batch script that polls for commands
                script_path = os.path.join(console_dir, "runner.bat")
                exec_file = os.path.join(console_dir, "exec.bat")
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write("@echo off\n")
                    f.write("%SystemRoot%\\System32\\chcp.com 65001 >nul\n")  # full path; PATH may lack System32 in the spawned console
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
            else:
                import shutil
                # Create a bash script that polls for commands
                script_path = os.path.join(console_dir, "runner.sh")
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write("#!/bin/bash\n")
                    # Snapshot the pristine PATH once; each command rebuilds PATH
                    # from this baseline so a per-test SDK folder never leaks into a
                    # later test that uses the default SDK.
                    f.write('export TESTRUNNER_BASE_PATH="$PATH"\n')
                    f.write("echo '========================================'\n")
                    f.write("echo '  .NET SDK Test Runner - Live Console'\n")
                    f.write("echo '========================================'\n")
                    f.write("echo\n")
                    f.write(f'CMD_FILE="{cmd_file}"\n')
                    f.write(f'DONE_FILE="{done_file}"\n')
                    f.write("while true; do\n")
                    f.write('  if [ -s "$CMD_FILE" ]; then\n')
                    f.write('    while IFS= read -r line; do\n')
                    f.write('      echo\n')
                    f.write('      eval "$line"\n')
                    f.write('    done < "$CMD_FILE"\n')
                    f.write('    > "$CMD_FILE"\n')
                    f.write("  fi\n")
                    f.write('  if [ -f "$DONE_FILE" ]; then break; fi\n')
                    f.write("  sleep 0.5\n")
                    f.write("done\n")
                    f.write("echo\n")
                    f.write("echo '========================================'\n")
                    f.write("echo '  Run complete. Press Enter to close.'\n")
                    f.write("echo '========================================'\n")
                    f.write("read\n")
                os.chmod(script_path, 0o755)

                terminals = [
                    ["gnome-terminal", "--title", f"Test Runner - {run_id}", "--"],
                    ["xfce4-terminal", "--title", f"Test Runner - {run_id}", "-e"],
                    ["konsole", "--title", f"Test Runner - {run_id}", "-e"],
                    ["xterm", "-title", f"Test Runner - {run_id}", "-e"],
                ]
                for term in terminals:
                    if shutil.which(term[0]):
                        try:
                            subprocess.Popen(
                                term + [script_path],
                                start_new_session=True,
                            )
                        except Exception:
                            continue
                        break


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
            # Reclaim it on a short delay off the run thread; if the window is
            # left open the delete simply fails and the startup sweep gets it.
            def _reclaim():
                time.sleep(10)
                _rmtree(console_dir)

            threading.Thread(target=_reclaim, daemon=True).start()

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
                    if event is None:  # Sentinel for end
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
        try:
            run_scr = os.path.join(SCREENSHOTS_DIR, run_id)
            os.makedirs(run_scr, exist_ok=True)
            self._runs[run_id]["_screenshot_dir"] = run_scr
        except OSError:
            pass

        # Validate a pinned SDK folder (e.g. a zip-extracted install). If it is
        # invalid, fall back to the PATH default and warn loudly so a "zip install"
        # test isn't silently run against the wrong (exe-installed) SDK.
        root = self._dotnet_root(run_id)
        if root and not self._valid_dotnet_root(root):
            warn = f"[warn] SDK folder not found or missing dotnet executable: {root}. Falling back to PATH dotnet."
            self._runs[run_id]["sdk_path"] = None
            self._emit_event(run_id, {"type": "step_output", "result_id": None, "step_index": -1, "line": warn})
            self._queue_console_cmd(run_id, f"echo {warn}")
            conn.execute("UPDATE test_runs SET sdk_path=NULL WHERE id=?", (run_id,))
            conn.commit()
        elif root:
            msg = f"[info] Using pinned SDK install: {root}"
            self._emit_event(run_id, {"type": "step_output", "result_id": None, "step_index": -1, "line": msg})
            self._queue_console_cmd(run_id, f"echo {msg}")

        # Capture environment info
        env_info = self._capture_environment(run_id)
        conn.execute(
            "UPDATE test_runs SET environment_info=? WHERE id=?",
            (env_info, run_id),
        )
        conn.commit()

        # Record the SDK actually resolved by dotnet (a pinned version may be
        # gone from the machine and silently roll forward). Log what really runs.
        run_pinned = self._runs.get(run_id, {}).get("sdk_version") or None
        actual = self._resolve_sdk_version(run_pinned, run_id)
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

        # Run-level SDK context. Each test defaults to this, but a test may pin its
        # own install folder (test["sdk_path"]) to override it for that test only.
        run_level_root = self._runs[run_id].get("sdk_path") or None
        run_level_version = self._runs[run_id].get("sdk_version") or None

        self._emit_event(run_id, {
            "type": "run_start",
            "run_id": run_id,
            "total_tests": len(tests),
            "environment": env_info,
        })

        for test in tests:
            if cancel_flag.is_set():
                skipped += 1
                continue

            test_case_id = test["id"]
            result_id = str(uuid.uuid4())[:12]
            steps = json.loads(test["steps"]) if isinstance(test["steps"], str) else test["steps"]

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

            # Send test header to console with spacing
            title = test["title"]
            # Title is arbitrary text echoed into a cmd batch; escape the cmd
            # metacharacters that would otherwise split the line (e.g.
            # "& .NET Standard"). Caret first so we don't double-escape the
            # carets we add.
            for ch in "^&<>|()":
                title = title.replace(ch, "^" + ch)
            self._queue_console_cmd(run_id, "echo.")
            self._queue_console_cmd(run_id, f"echo ===== {title} =====")
            self._queue_console_cmd(run_id, "echo.")

            # Resolve this test's SDK context: a valid per-test folder overrides
            # the run-level one for this test only; otherwise the run-level applies.
            test_root = (test.get("sdk_path") or "").strip() or None
            if test_root and not self._valid_dotnet_root(test_root):
                self._emit_event(run_id, {
                    "type": "step_output", "result_id": result_id, "step_index": -1,
                    "line": f"[warn] Test SDK folder not found or missing dotnet: {test_root}. Using run default.",
                })
                self._queue_console_cmd(run_id, f"echo [warn] test SDK folder invalid: {test_root}; using run default")
                test_root = None
            if test_root and test_root != run_level_root:
                self._runs[run_id]["_active_sdk_path"] = test_root
                # Resolve the version from this test's install for tfm + global.json.
                self._runs[run_id]["_active_sdk_version"] = self._resolve_sdk_version(run_pinned, run_id)
                self._emit_event(run_id, {
                    "type": "step_output", "result_id": result_id, "step_index": -1,
                    "line": f"[info] Test pinned to SDK install: {test_root}",
                })
                self._queue_console_cmd(run_id, f"echo [info] test pinned to SDK install: {test_root}")
            else:
                self._runs[run_id]["_active_sdk_path"] = run_level_root
                self._runs[run_id]["_active_sdk_version"] = run_level_version

            test_passed, test_warned = self._execute_test(
                run_id, result_id, steps, cancel_flag, conn, test.get("title") or test_case_id
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

        # Finalize run
        final_status = "completed" if not cancel_flag.is_set() else "cancelled"
        summary = json.dumps({"passed": passed, "failed": failed, "skipped": skipped, "warnings": warned})
        conn.execute(
            "UPDATE test_runs SET status=?, finished_at=?, summary=? WHERE id=?",
            (final_status, datetime.now().isoformat(), summary, run_id),
        )
        conn.commit()
        conn.close()

        self._emit_event(run_id, {
            "type": "run_end",
            "status": final_status,
            "summary": {"passed": passed, "failed": failed, "skipped": skipped, "warnings": warned},
        })
        self._end_stream(run_id)

        # Cleanup
        self._cancel_flags.pop(run_id, None)
        self._close_console(run_id)

    def _execute_test(
        self, run_id: str, result_id: str, steps: List[dict],
        cancel_flag: threading.Event, conn: sqlite3.Connection, test_name: str = ""
    ) -> tuple:
        """Run one test case in a temp working directory, then clean it up.

        The directory is removed when the test passes. It is deliberately kept
        when the test fails so the build output can be inspected, and the path
        is surfaced in the run log; the startup sweep reclaims it later. A
        cancelled test keeps nothing, since its directory is usually empty.
        """
        work_dir = tempfile.mkdtemp(prefix="dotnet_test_")
        try:
            all_passed, had_warnings = self._execute_test_steps(
                run_id, result_id, steps, cancel_flag, conn, test_name, work_dir
            )
        except BaseException:
            _rmtree(work_dir)
            raise

        if all_passed or cancel_flag.is_set():
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
        self, run_id: str, result_id: str, steps: List[dict],
        cancel_flag: threading.Event, conn: sqlite3.Connection, test_name: str,
        work_dir: str,
    ) -> tuple:
        """Execute all steps for a single test case.

        Returns (all_passed, had_warnings): all_passed is True if every step
        passed; had_warnings is True if any passing step emitted an
        MSBuild/NuGet-style warning (e.g. NU1903 vulnerability warning).
        """
        current_dir = work_dir
        all_passed = True
        had_warnings = False

        # Pin SDK version via global.json if specified. Uses this test's active
        # SDK context (a per-test folder override, or the run-level default).
        run = self._runs.get(run_id, {})
        sdk_version = run.get("_active_sdk_version", run.get("sdk_version")) or None
        # {tfm} in step commands/content tracks the selected SDK's target framework
        # moniker (e.g. 11.0.100 -> net11.0). net48 and other literals are untouched.
        tfm = f"net{sdk_version.split('.')[0]}.0" if sdk_version else "net10.0"
        if sdk_version:
            global_json = os.path.join(work_dir, "global.json")
            with open(global_json, "w") as f:
                json.dump({"sdk": {"version": sdk_version, "rollForward": "disable"}}, f)

        for idx, step in enumerate(steps):
            if cancel_flag.is_set():
                return (False, had_warnings)

            step_id = str(uuid.uuid4())[:12]
            step_type = step.get("type", "command")
            timeout = step.get("timeout", 120)
            expected_exit = step.get("expected_exit_code", 0)

            if step_type == "command":
                cmd = step["command"].replace("{tfm}", tfm).replace("{assets}", ASSETS_DIR)
                # Handle cd commands by updating current_dir
                if cmd.strip().startswith("cd "):
                    prev_dir = current_dir
                    target = cmd.strip()[3:].strip()
                    if os.path.isabs(target):
                        current_dir = target
                    else:
                        current_dir = os.path.normpath(os.path.join(current_dir, target))
                    # Ensure global.json is in the new directory too
                    if sdk_version and os.path.isdir(current_dir):
                        gj = os.path.join(current_dir, "global.json")
                        if not os.path.exists(gj):
                            with open(gj, "w") as f:
                                json.dump({"sdk": {"version": sdk_version, "rollForward": "disable"}}, f)
                    conn.execute(
                        """INSERT INTO step_results (id, test_result_id, step_index, step_type, command, exit_code, stdout, status, duration_ms)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (step_id, result_id, idx, "cd", cmd, 0, f"Changed to: {current_dir}", "passed", 0),
                    )
                    conn.commit()
                    self._emit_event(run_id, {
                        "type": "step_output",
                        "result_id": result_id,
                        "step_index": idx,
                        "line": f"$ {cmd}\n  → {current_dir}",
                    })
                    # Send cd to console, echoing the prompt at the dir BEFORE cd so it matches a manual run
                    self._queue_console_cmd(run_id, f"echo {prev_dir}^> {cmd}")
                    self._queue_console_cmd(run_id, f"cd /d {current_dir}")
                    continue

                # Long-running server step (e.g. `dotnet run`): stream output, wait
                # for readiness, optionally verify the hosted site, then terminate.
                # `run_timeout` reuses the same path for blocking GUI apps (WinForms/
                # WPF): run for N seconds so the window shows, then auto-close it.
                if step.get("long_running") or step.get("run_timeout"):
                    start_time = time.time()
                    self._emit_event(run_id, {
                        "type": "step_output",
                        "result_id": result_id,
                        "step_index": idx,
                        "line": f"$ {cmd}",
                        "is_command": True,
                    })
                    stdout_text, exit_code = self._run_long_running(
                        run_id, result_id, idx, cmd, current_dir, step, cancel_flag, timeout,
                        test_name,
                    )
                    if cancel_flag.is_set():
                        return (False, had_warnings)
                    duration_ms = int((time.time() - start_time) * 1000)
                    step_passed = (exit_code == 0)
                    step_warned = step_passed and _detect_warnings(stdout_text)
                    if step_warned:
                        had_warnings = True
                    status = "warning" if step_warned else ("passed" if step_passed else "failed")
                    if not step_passed:
                        all_passed = False
                    conn.execute(
                        """INSERT INTO step_results (id, test_result_id, step_index, step_type, command, exit_code, stdout, stderr, status, duration_ms)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (step_id, result_id, idx, "command", cmd, exit_code,
                         stdout_text[:10000], "", status, duration_ms),
                    )
                    conn.commit()
                    self._emit_event(run_id, {
                        "type": "step_end",
                        "result_id": result_id,
                        "step_index": idx,
                        "status": status,
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                    })
                    if not step_passed and not step.get("continue_on_error", False):
                        break
                    continue

                # Execute the command
                start_time = time.time()

                # Emit the command being run to the app
                self._emit_event(run_id, {
                    "type": "step_output",
                    "result_id": result_id,
                    "step_index": idx,
                    "line": f"$ {cmd}",
                    "is_command": True,
                })

                # Run command in the console window and capture output
                stdout_text, stderr_text, exit_code = self._run_in_console(
                    run_id, cmd, current_dir, timeout, cancel_flag
                )

                if cancel_flag.is_set():
                    return (False, had_warnings)

                # Emit captured output to the in-app runner
                if stdout_text:
                    for line in stdout_text.splitlines():
                        self._emit_event(run_id, {
                            "type": "step_output",
                            "result_id": result_id,
                            "step_index": idx,
                            "line": line,
                        })
                if stderr_text:
                    for line in stderr_text.splitlines():
                        self._emit_event(run_id, {
                            "type": "step_output",
                            "result_id": result_id,
                            "step_index": idx,
                            "line": f"[STDERR] {line}",
                        })

                duration_ms = int((time.time() - start_time) * 1000)

                # Determine pass/fail
                if isinstance(expected_exit, list):
                    step_passed = exit_code in expected_exit
                else:
                    step_passed = exit_code == expected_exit

                # Check output assertions if any
                if step_passed and "assert_output_contains" in step:
                    for pattern in step["assert_output_contains"]:
                        if pattern not in stdout_text:
                            step_passed = False
                            break

                step_warned = step_passed and _detect_warnings(stdout_text)
                if step_warned:
                    had_warnings = True
                status = "warning" if step_warned else ("passed" if step_passed else "failed")
                if not step_passed:
                    all_passed = False

                conn.execute(
                    """INSERT INTO step_results (id, test_result_id, step_index, step_type, command, exit_code, stdout, stderr, status, duration_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (step_id, result_id, idx, "command", cmd, exit_code,
                     stdout_text[:10000], stderr_text[:5000],
                     status, duration_ms),
                )
                conn.commit()

                self._emit_event(run_id, {
                    "type": "step_end",
                    "result_id": result_id,
                    "step_index": idx,
                    "status": status,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                })

                # Stop test on first failure unless continue_on_error
                if not step_passed and not step.get("continue_on_error", False):
                    break

            elif step_type == "screenshot":
                # Open a folder in Explorer and capture a screenshot of the
                # primary screen into the run's screenshot folder. Windows only.
                start_time = time.time()
                label = step.get("label", "screenshot")
                delay = step.get("delay", 4)
                timeout = step.get("timeout", 60)

                if sys.platform != "win32":
                    self._emit_event(run_id, {
                        "type": "step_output", "result_id": result_id, "step_index": idx,
                        "line": "[skip] screenshot step is only supported on Windows",
                    })
                    conn.execute(
                        """INSERT INTO step_results (id, test_result_id, step_index, step_type, command, exit_code, stdout, status, duration_ms)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (step_id, result_id, idx, "screenshot", f"screenshot: {label}", 0,
                         "skipped (non-Windows)", "passed", 0),
                    )
                    conn.commit()
                    continue

                console_dir = self._console_procs.get(run_id)
                script_dir = console_dir if isinstance(console_dir, str) else tempfile.gettempdir()
                script_path = os.path.join(script_dir, f"screenshot_{idx}.ps1")
                try:
                    with open(script_path, "w", encoding="utf-8") as sf:
                        sf.write(self._build_screenshot_script(
                            label, step.get("folder", ""), step.get("folder_ps", ""), delay,
                            test_name,
                        ))
                except OSError as e:
                    self._emit_event(run_id, {
                        "type": "step_output", "result_id": result_id, "step_index": idx,
                        "line": f"[error] could not write screenshot script: {e}",
                    })
                    all_passed = False
                    break

                cmd = f'"{self._powershell_exe()}" -NoProfile -ExecutionPolicy Bypass -File "{script_path}"'
                self._emit_event(run_id, {
                    "type": "step_output", "result_id": result_id, "step_index": idx,
                    "line": f"📸 Capture screenshot ({label})", "is_command": True,
                })

                stdout_text, stderr_text, exit_code = self._run_in_console(
                    run_id, cmd, current_dir, timeout, cancel_flag
                )
                if cancel_flag.is_set():
                    return (False, had_warnings)

                for line in (stdout_text or "").splitlines():
                    self._emit_event(run_id, {
                        "type": "step_output", "result_id": result_id, "step_index": idx, "line": line,
                    })
                for line in (stderr_text or "").splitlines():
                    self._emit_event(run_id, {
                        "type": "step_output", "result_id": result_id, "step_index": idx,
                        "line": f"[STDERR] {line}",
                    })

                duration_ms = int((time.time() - start_time) * 1000)
                step_passed = (exit_code == 0) and ("Screenshot saved:" in (stdout_text or ""))
                status = "passed" if step_passed else "failed"
                if not step_passed:
                    all_passed = False

                conn.execute(
                    """INSERT INTO step_results (id, test_result_id, step_index, step_type, command, exit_code, stdout, stderr, status, duration_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (step_id, result_id, idx, "screenshot", f"screenshot: {label}", exit_code,
                     (stdout_text or "")[:10000], (stderr_text or "")[:5000], status, duration_ms),
                )
                conn.commit()

                self._emit_event(run_id, {
                    "type": "step_end", "result_id": result_id, "step_index": idx,
                    "status": status, "exit_code": exit_code, "duration_ms": duration_ms,
                })

                if not step_passed and not step.get("continue_on_error", False):
                    break

            elif step_type == "write_file":
                filepath = os.path.join(current_dir, step["path"])
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                content = step["content"].replace("{tfm}", tfm).replace("{assets}", ASSETS_DIR)

                start_time = time.time()

                # Optionally capture a checkpoint screenshot of the Notepad window
                # (Windows only) into the run's screenshot folder so it appears in
                # the Screenshots tab.
                shot_path = None
                if step.get("screenshot") and sys.platform == "win32":
                    scr = self._screenshot_dir(run_id)
                    if scr:
                        def _slug(s):
                            return re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "")).strip("-")
                        label = step.get("label") or os.path.basename(step["path"])
                        base = "-".join(p for p in (_slug(test_name), _slug(label)) if p) or "notepad"
                        shot_path = os.path.join(
                            scr, f"{base}-{time.strftime('%Y%m%d-%H%M%S')}.png"
                        )

                wrote_via_notepad = False
                try:
                    wrote_via_notepad = self._write_file_via_notepad(filepath, content, shot_path)
                except Exception:
                    wrote_via_notepad = False

                if not wrote_via_notepad:
                    # Fallback to direct write
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(content)

                duration_ms = int((time.time() - start_time) * 1000)
                method_label = "via Notepad" if wrote_via_notepad else "direct"
                shot_saved = bool(shot_path and os.path.isfile(shot_path))

                stdout_msg = f"Wrote {len(content)} bytes ({method_label})"
                if shot_saved:
                    stdout_msg += f"; screenshot: {os.path.basename(shot_path)}"

                conn.execute(
                    """INSERT INTO step_results (id, test_result_id, step_index, step_type, command, exit_code, stdout, status, duration_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (step_id, result_id, idx, "write_file", step["path"], 0,
                     stdout_msg, "passed", duration_ms),
                )
                conn.commit()

                self._emit_event(run_id, {
                    "type": "step_output",
                    "result_id": result_id,
                    "step_index": idx,
                    "line": f"📝 Wrote file ({method_label}): {step['path']}",
                })
                if shot_path and not shot_saved:
                    reason = (getattr(self, "_last_capture_error", None)
                              or getattr(self, "_last_notepad_error", None)
                              or "unknown error")
                    self._emit_event(run_id, {
                        "type": "step_output",
                        "result_id": result_id,
                        "step_index": idx,
                        "line": f"[warn] Notepad screenshot failed: {reason}",
                    })

        return (all_passed, had_warnings)

    def _write_file_via_notepad(self, filepath: str, content: str, screenshot_path: str = None) -> bool:
        """
        Write file content, open it in Notepad to display, then close.
        If screenshot_path is given, capture the screen (with Notepad in the
        foreground) into that path before closing, as a checkpoint record.

        Uses plain subprocess rather than GUI automation libraries: those are
        unreliable in a windowed PyInstaller build (and may not be bundled),
        which previously made this silently fall back to a direct write with no
        screenshot. Returns True if Notepad was opened, False otherwise; the
        reason for any failure is stored in self._last_notepad_error.
        """
        self._last_notepad_error = None

        # Write the actual content to the file first
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        if sys.platform != "win32":
            self._last_notepad_error = "not supported on non-Windows"
            return False

        notepad_exe = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe"
        )
        proc = None
        try:
            # Redirect all standard streams: a windowed (console=False) build has
            # no valid stdin handle, so an un-redirected launch fails outright.
            proc = subprocess.Popen(
                [notepad_exe, filepath],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._last_notepad_error = f"could not launch notepad: {type(e).__name__}: {e}"
            return False

        try:
            # Give Notepad a moment to spawn; _capture_screen then polls for its
            # window handle and grabs that window directly (robust even if the
            # window never gains foreground focus).
            time.sleep(0.4)
            if screenshot_path:
                self._capture_screen(screenshot_path, pid=proc.pid,
                                     title_hint=os.path.basename(filepath),
                                     maximize=True)
            else:
                # Brief pause so the user can see the file content.
                time.sleep(0.6)
            return True
        except Exception as e:
            self._last_notepad_error = f"{type(e).__name__}: {e}"
            return False
        finally:
            # Close the Notepad window we opened (force kill: the file is already
            # saved, so there is nothing to prompt about).
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

    def _run_in_console(
        self, run_id: str, cmd: str, cwd: str, timeout: int,
        cancel_flag: threading.Event
    ) -> tuple:
        """
        Run a command in the visible console window and capture its output.
        Returns (stdout, stderr, exit_code).
        """
        console_dir = self._console_procs.get(run_id)

        if not console_dir:
            # No console available — run directly (fallback)
            return self._run_direct(cmd, cwd, timeout, cancel_flag, run_id)

        # Temp files for capturing output and exit code
        stdout_file = os.path.join(console_dir, "stdout.tmp")
        exitcode_file = os.path.join(console_dir, "exitcode.tmp")

        # Clean up any old result files
        for f in [stdout_file, exitcode_file]:
            if os.path.exists(f):
                os.unlink(f)

        # Build wrapper commands that:
        # 1. cd to the working directory
        # 2. Run the command (output shows in console)
        # 3. Tee output to a file for the app to read
        # 4. Write exit code to a file
        cmd_file = os.path.join(console_dir, "commands.txt")
        root = self._dotnet_root(run_id)
        scr = self._screenshot_dir(run_id)
        try:
            if sys.platform == "win32":
                # Write a dedicated wrapper batch that captures exit code reliably
                wrapper_file = os.path.join(console_dir, "runcmd.bat")
                with open(wrapper_file, "w", encoding="utf-8") as wf:
                    wf.write("@echo off\n")
                    wf.write("set MSBUILDTERMINALLOGGER=on\n")
                    # Rebuild PATH from the pristine baseline every command so a
                    # per-test SDK folder pinned earlier never lingers for a later
                    # test that uses the default SDK. Fall back to %PATH% if the
                    # baseline wasn't captured (e.g. older console).
                    wf.write('if defined TESTRUNNER_BASE_PATH set "PATH=%TESTRUNNER_BASE_PATH%"\n')
                    # The spawned console's PATH may lack System32, so bare tools
                    # like `powershell` aren't found (same reason chcp uses a full
                    # path). Ensure the core Windows directories are on PATH.
                    wf.write('set "PATH=%PATH%;%SystemRoot%\\System32;%SystemRoot%\\System32\\WindowsPowerShell\\v1.0"\n')
                    if scr:
                        wf.write(f'set "SCREENSHOT_DIR={scr}"\n')
                    if root:
                        # Point dotnet at the pinned SDK install for this run.
                        wf.write(f'set "DOTNET_ROOT={root}"\n')
                        wf.write("set DOTNET_MULTILEVEL_LOOKUP=0\n")
                        wf.write(f'set "PATH={root};%PATH%"\n')
                    else:
                        # Default SDK: clear any DOTNET_ROOT a previous test pinned
                        # so dotnet resolves the machine-wide install.
                        wf.write('set "DOTNET_ROOT="\n')
                        wf.write('set "DOTNET_MULTILEVEL_LOOKUP="\n')
                    wf.write(f"cd /d {cwd}\n")
                    wf.write(f"echo {cwd}^> {cmd}\n")
                    # Capture the real exit code. The previous
                    # `cmd && echo 0 || echo 1` form flattened every failure to
                    # 1, so a step with expected_exit_code: 1 passed on *any*
                    # error (including 9009, command not found). %ERRORLEVEL% is
                    # expanded when this line is read, and each statement is on
                    # its own line rather than in a parenthesised block, so no
                    # delayed expansion is needed. The space before `>` is
                    # required: `echo 1>file` would parse the digit as a stream
                    # number and write "ECHO is off." instead of the code.
                    wf.write(f'{cmd} > "{stdout_file}" 2>&1\n')
                    wf.write(f'echo %ERRORLEVEL% > "{exitcode_file}"\n')
                    wf.write(f'type "{stdout_file}"\n')
                    wf.write("echo.\n")
                # Tell the console to call the wrapper
                with open(cmd_file, "a", encoding="utf-8") as f:
                    f.write(f'call "{wrapper_file}"\n')
            else:
                with open(cmd_file, "a", encoding="utf-8") as f:
                    f.write(f"cd {cwd}\n")
                    f.write(f"echo '{cwd}$ {cmd}'\n")
                    f.write("export MSBUILDTERMINALLOGGER=on\n")
                    if scr:
                        f.write(f'export SCREENSHOT_DIR="{scr}"\n')
                    f.write('[ -n "$TESTRUNNER_BASE_PATH" ] && export PATH="$TESTRUNNER_BASE_PATH"\n')
                    if root:
                        f.write(f'export DOTNET_ROOT="{root}"\n')
                        f.write("export DOTNET_MULTILEVEL_LOOKUP=0\n")
                        f.write(f'export PATH="{root}:$PATH"\n')
                    else:
                        f.write("unset DOTNET_ROOT\n")
                        f.write("unset DOTNET_MULTILEVEL_LOOKUP\n")
                    f.write(f'{cmd} 2>&1 | tee "{stdout_file}"; echo $? > "{exitcode_file}"\n')
                    f.write("echo\n")
        except Exception:
            return self._run_direct(cmd, cwd, timeout, cancel_flag, run_id)

        # Wait for the exit code file to appear (means command finished)
        deadline = time.time() + timeout
        while not os.path.exists(exitcode_file):
            if cancel_flag.is_set():
                return ("", "", -1)
            if time.time() > deadline:
                return ("", "", -1)
            time.sleep(0.5)

        # Small delay to ensure files are fully written
        time.sleep(0.3)

        # Read captured output
        stdout_text = ""
        exit_code = -1

        try:
            if os.path.exists(stdout_file):
                with open(stdout_file, "r", encoding="utf-8", errors="replace") as f:
                    stdout_text = f.read()
            if os.path.exists(exitcode_file):
                with open(exitcode_file, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read().strip()
                    if content:
                        exit_code = int(content)
        except (ValueError, OSError):
            pass

        # Cleanup result files for next command
        for f in [stdout_file, exitcode_file]:
            try:
                if os.path.exists(f):
                    os.unlink(f)
            except OSError:
                pass

        return (_render_terminal_output(stdout_text), "", exit_code)

    def _run_long_running(
        self, run_id: str, result_id: str, idx: int, cmd: str, cwd: str,
        step: dict, cancel_flag: threading.Event, timeout: int, test_name: str = ""
    ) -> tuple:
        """
        Start a long-running server command, stream its output live to BOTH the app
        panel and the popup console, wait for a readiness pattern, optionally
        HTTP-verify the hosted site, then terminate. Returns (stdout_text, exit_code).

        Python owns the process (clean PID kill). Each captured line is written to a
        log file that the popup console tails live via a small PowerShell script, so
        the console streams the same output as the panel in real time.
        """
        ready_patterns = step.get("ready_pattern") or []
        if isinstance(ready_patterns, str):
            ready_patterns = [ready_patterns]
        verify_url = step.get("verify_url")
        contains = step.get("verify_contains")
        if isinstance(contains, str):
            contains = [contains]
        # After a site is confirmed up, optionally open it in the browser and hold
        # the server alive so it can be eyeballed (like the Notepad file preview).
        open_in_browser = step.get("open_in_browser", True)
        hold_seconds = step.get("hold_seconds", 10)

        console_dir = self._console_procs.get(run_id)
        live = bool(console_dir)

        lines = []
        ready = threading.Event()
        log_fh = None
        done_flag = None

        if live:
            srv_log = os.path.join(console_dir, f"srv_{idx}_{uuid.uuid4().hex[:8]}.log")
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
                # full path — the spawned console's PATH may lack System32,
                # so bare `powershell` isn't found (same reason chcp uses a full path).
                ps_exe = os.path.join(
                    os.environ.get("SystemRoot", r"C:\Windows"),
                    "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
                )
                self._queue_console_cmd(
                    run_id,
                    f'"{ps_exe}" -NoProfile -ExecutionPolicy Bypass -File "{tail_ps1}" "{srv_log}" "{done_flag}"',
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
            return (_render_terminal_output("".join(lines)), exit_code)

        env = self._sdk_env(run_id)
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
                self._emit_event(run_id, {
                    "type": "step_output",
                    "result_id": result_id,
                    "step_index": idx,
                    "line": line.rstrip("\n"),
                })
                if not ready.is_set() and any(p in line for p in ready_patterns):
                    ready.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        # Timed run for blocking GUI apps (WinForms/WPF): no readiness pattern —
        # let the window render for `run_timeout` seconds, then auto-close it and
        # pass. An early exit means the app quit on its own: use its exit code so
        # a build/runtime failure (nonzero) is still reported as a failure.
        run_timeout = step.get("run_timeout")
        if run_timeout:
            # Once the GUI window has had a moment to render, capture it once and
            # log that it was displayed (issue #45: the window auto-closes, so
            # otherwise there is no record it ever appeared).
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
                if (not gui_shot_done and sys.platform == "win32"
                        and step.get("screenshot") and time.time() >= gui_shot_at):
                    gui_shot_done = True
                    self._capture_gui_window(
                        run_id, result_id, idx, proc.pid, test_name, cwd,
                        write_log, lines, step,
                    )
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

        # Process died before becoming ready -> failure (build/run error).
        if not ready.is_set():
            t.join(timeout=2)
            return finish(proc.returncode or 1)

        exit_code = 0
        if verify_url:
            ok, status, body, err = self._verify_site(verify_url, contains)
            msg = f"GET {verify_url} -> HTTP {status}" + (f" | {err}" if err else "")
            self._emit_event(run_id, {
                "type": "step_output",
                "result_id": result_id,
                "step_index": idx,
                "line": ("✅ " if ok else "❌ ") + msg,
            })
            log_msg = "\n" + ("[OK] " if ok else "[FAIL] ") + msg + "\n"
            lines.append(log_msg)
            write_log(log_msg)
            if not ok:
                lines.append(body[:5000])
                write_log(body[:5000])
                exit_code = 1

        # Open the live site for visual verification, keeping the server up for a
        # short hold so it can be seen, then continue the automation.
        if verify_url and open_in_browser:
            try:
                webbrowser.open(verify_url)
            except Exception:
                pass
            # Optionally capture a checkpoint screenshot of the browser window
            # showing the hosted site (Windows only) into the run's screenshot
            # folder so it appears in the Screenshots tab. Give the page a moment
            # to render, then grab the browser window (cropped).
            if step.get("screenshot") and sys.platform == "win32":
                shot_deadline = time.time() + 4
                while time.time() < shot_deadline and not cancel_flag.is_set():
                    time.sleep(0.2)
                scr = self._screenshot_dir(run_id)
                if scr:
                    def _slug(s):
                        return re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "")).strip("-")
                    hostport = re.sub(r"^https?://", "", verify_url)
                    label = step.get("label") or hostport
                    base = "-".join(p for p in (_slug(test_name), _slug(label)) if p) or "browser"
                    shot_path = os.path.join(
                        scr, f"{base}-{time.strftime('%Y%m%d-%H%M%S')}.png"
                    )
                    # Match the browser window by its host:port (the tab title for
                    # the minimal web/webapi pages is the URL); fall back to the
                    # foreground window (the browser we just opened) for pages with
                    # a custom title (e.g. the MVC "Home Page").
                    hint = re.sub(r"^https?://", "", verify_url).split("/")[0]
                    saved = self._capture_screen(
                        shot_path, title_hint=hint, foreground_fallback=True,
                        exclude_title=OWN_WINDOW_TITLE,
                    )
                    if saved:
                        msg = f"📸 Browser screenshot saved: {os.path.basename(shot_path)}"
                        lines.append("\n" + msg + "\n")
                        write_log("\n" + msg + "\n")
                    else:
                        reason = getattr(self, "_last_capture_error", None) or "unknown error"
                        self._emit_event(run_id, {
                            "type": "step_output",
                            "result_id": result_id,
                            "step_index": idx,
                            "line": f"[warn] Browser screenshot failed: {reason}",
                        })
            hold_deadline = time.time() + hold_seconds
            while time.time() < hold_deadline and not cancel_flag.is_set():
                time.sleep(0.2)

        self._terminate_proc(proc)
        t.join(timeout=2)
        return finish(exit_code)

    def _write_tail_script(self, console_dir: str) -> str:
        """Write (idempotently) a PowerShell script that live-tails a growing log
        file to the console and exits once a done-flag file appears."""
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
        """One HTTP GET (a couple of tries). Returns (ok, status, body, err)."""
        # dev HTTPS cert is self-signed -> skip TLS verification.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        last_err = ""
        # fixed 2 attempts, no backoff lib; server may need a beat
        # after "Now listening on". Bump the range if that proves flaky.
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
            # taskkill /T reaps the dotnet child that shell=True spawns;
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

    def _run_direct(
        self, cmd: str, cwd: str, timeout: int,
        cancel_flag: threading.Event, run_id: str
    ) -> tuple:
        """Fallback: run command directly when no console is available."""
        stdout_lines = []
        stderr_lines = []
        exit_code = -1

        try:
            env = self._sdk_env(run_id)
            env["DOTNET_CLI_COLORS"] = "1"
            env["DOTNET_SYSTEM_CONSOLE_ALLOW_ANSI_COLOR_REDIRECTION"] = "1"
            env["FORCE_COLOR"] = "1"
            env["TERM"] = "xterm-256color"
            env["MSBUILDTERMINALLOGGER"] = "on"

            proc = subprocess.Popen(
                cmd, shell=True, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=env,
            )

            def read_stdout():
                for line in proc.stdout:
                    stdout_lines.append(line)

            def read_stderr():
                for line in proc.stderr:
                    stderr_lines.append(line)

            t1 = threading.Thread(target=read_stdout, daemon=True)
            t2 = threading.Thread(target=read_stderr, daemon=True)
            t1.start()
            t2.start()

            deadline = time.time() + timeout
            while proc.poll() is None:
                if cancel_flag.is_set():
                    proc.terminate()
                    return ("", "", -1)
                if time.time() > deadline:
                    proc.terminate()
                    return ("", "", -1)
                time.sleep(0.1)

            t1.join(timeout=5)
            t2.join(timeout=5)
            exit_code = proc.returncode if proc.returncode is not None else -1

        except Exception as e:
            stderr_lines.append(str(e))

        return (_render_terminal_output("".join(stdout_lines)), "".join(stderr_lines), exit_code)

    def _resolve_sdk_version(self, pinned: str = None, run_id: str = None) -> str:
        """Return the SDK version dotnet actually resolves for this run.

        Resolves using the same global.json the test steps use so history logs
        the version that really executes, not a stale pinned string. Falls back
        to the unpinned default when the pinned version is no longer installed.
        When the run pins an SDK folder, the version is resolved from that install.
        """
        root = self._dotnet_root(run_id) if run_id else None
        dotnet = self._dotnet_exe(root) if root else "dotnet"
        env = self._sdk_env(run_id) if run_id else None

        def dotnet_version(cwd):
            try:
                r = subprocess.run(
                    [dotnet, "--version"], capture_output=True, text=True,
                    timeout=30, cwd=cwd, env=env,
                )
                return r.stdout.strip() if r.returncode == 0 else None
            except Exception:
                return None

        if not pinned:
            return dotnet_version(None)

        d = tempfile.mkdtemp(prefix="sdk_resolve_")
        try:
            with open(os.path.join(d, "global.json"), "w") as f:
                json.dump({"sdk": {"version": pinned, "rollForward": "disable"}}, f)
            # unpinned fallback = version that actually runs when pinned is gone
            return dotnet_version(d) or dotnet_version(None)
        finally:
            try:
                os.remove(os.path.join(d, "global.json"))
                os.rmdir(d)
            except OSError:
                pass

    def _capture_environment(self, run_id: str = None) -> str:
        root = self._dotnet_root(run_id) if run_id else None
        dotnet = self._dotnet_exe(root) if root else "dotnet"
        env = self._sdk_env(run_id) if run_id else None
        try:
            result = subprocess.run(
                [dotnet, "--info"], capture_output=True, text=True, timeout=30, env=env,
            )
            return result.stdout
        except Exception as e:
            return f"Could not capture environment: {e}"
