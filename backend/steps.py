"""Run one Step and judge its Step outcome.

run_step(step, ctx, shell, desktop) is the only entry point. All the Step rules
are here: the exit code check, the output assertions, warning detection, the
cd and write_file rules, and continue_on_error. The caller (the orchestrator in
executor.py) records each outcome and decides nothing about pass or fail.

Two seams keep this module testable:
  * Shell runs a command line. The adapters are ConsoleShell (the visible
    console window), DirectShell (a hidden process) and a fake in the tests.
  * Desktop does the work that needs a real Windows desktop: Notepad, screen
    capture, the screenshot script, and long-running Steps. TestExecutor
    provides the production adapter.
"""

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, NamedTuple, Optional, Protocol

import sdk_source

PASSED = "passed"
WARNING = "warning"
FAILED = "failed"
# Never recorded. The orchestrator stops the Test case when it sees this.
CANCELLED = "cancelled"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Matches MSBuild/NuGet/Roslyn diagnostic warnings, for example "warning NU1903:",
# "warning CS0168:", "warning MSB3277:", "warning NETSDK1138:". These mean that a
# step succeeded but emitted a diagnostic worth showing (for example, a package with
# a known vulnerability), so the test is reported as "passed with warnings".
_WARNING_RE = re.compile(r"\bwarning\s+[A-Za-z]{2,}[0-9]+\s*:", re.IGNORECASE)


def detect_warnings(text: str) -> bool:
    """Return True if the output contains an MSBuild/NuGet-style warning line."""
    return bool(text) and bool(_WARNING_RE.search(text))


def render_terminal_output(text: str) -> str:
    """Collapse a terminal-logger animation stream into the final rendered
    screen a real console would show. SGR colors are kept.

    The .NET SDK's modern terminal logger (forced on via MSBUILDTERMINALLOGGER)
    emits cursor-movement escapes to animate progress in place. When the output
    is captured to a file, those frames pile up. So we replay them through a VT
    emulator and read back the final screen.

    This only runs when cursor-control escapes are present (the
    `\\x1b[?25` show/hide-cursor codes that the logger always emits). Plain
    command output passes through unchanged. It is never reflowed or
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
    # Windows consoles treat LF as CR+LF (move to column 0). pyte defaults
    # to a Unix bare LF, which breaks output that moves the cursor. LNM mode
    # makes pyte match the real console that the app imitates.
    screen.set_mode(pyte.modes.LNM)
    pyte.Stream(screen).feed(text)
    rows = list(screen.history.top) + [screen.buffer[i] for i in range(screen.lines)]
    lines = [render_line(buf) for buf in rows]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def screenshot_base(test_name: str, label: str, fallback: str) -> str:
    """File name stem for every screenshot: `<test name>-<label>`, made safe."""
    def slug(s):
        return re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "")).strip("-")
    return "-".join(p for p in (slug(test_name), slug(label)) if p) or fallback


def _no_log(line: str, is_command: bool = False) -> None:
    pass


@dataclass(frozen=True)
class StepContext:
    """Everything a Step needs from its Test case. It is frozen, so a cd Step
    returns its new folder in the outcome, and the orchestrator makes the next
    context."""
    cwd: str
    sdk: sdk_source.SdkContext = field(default_factory=sdk_source.SdkContext)
    test_name: str = ""
    rid: str = ""
    workload_version: str = ""
    assets_dir: str = ""
    screenshot_dir: Optional[str] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    # log(line, is_command=False) sends one live line to the in-app log.
    log: Callable[..., None] = _no_log

    def expand(self, text: str) -> str:
        """Fill in {tfm}, {rid}, {assets} and {workload_version}."""
        return (text.replace("{tfm}", self.sdk.tfm).replace("{rid}", self.rid)
                    .replace("{assets}", self.assets_dir)
                    .replace("{workload_version}", self.workload_version))

    def env(self) -> dict:
        """Environment for a process that a Step starts directly."""
        env = self.sdk.env()
        if self.screenshot_dir:
            env["SCREENSHOT_DIR"] = self.screenshot_dir
        return env

    def screenshot_path(self, label: str, fallback: str) -> Optional[str]:
        """Where to save a screenshot now, or None when the run has no folder."""
        if not self.screenshot_dir:
            return None
        base = screenshot_base(self.test_name, label, fallback)
        return os.path.join(self.screenshot_dir, f"{base}-{time.strftime('%Y%m%d-%H%M%S')}.png")


@dataclass(frozen=True)
class StepOutcome:
    status: str
    step_type: str  # command, cd, screenshot or write_file
    label: str      # the command text, "screenshot: <label>", or the file path
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    new_cwd: Optional[str] = None  # cd only
    stops_test: bool = False


class ShellResult(NamedTuple):
    stdout: str
    stderr: str
    exit_code: int


class Shell(Protocol):
    def run(self, command: str, cwd: str, timeout: int) -> ShellResult: ...
    def show(self, line: str) -> None: ...


class Desktop(Protocol):
    """Each method returns error text, or None when it worked."""
    def show_in_notepad(self, path: str, screenshot_path: Optional[str]) -> Optional[str]: ...
    def capture_console(self, screenshot_path: str) -> Optional[str]: ...
    # Writes the screenshot script and returns the command that runs it.
    # Raises OSError when the script cannot be written.
    def screenshot_command(self, step: dict, ctx: StepContext) -> str: ...
    # Returns (output, exit_code). Streams its own output through ctx.log.
    def run_long_running(self, command: str, step: dict, ctx: StepContext) -> tuple: ...


def run_step(step: dict, ctx: StepContext, shell: Shell, desktop: Desktop) -> StepOutcome:
    start = time.time()
    kind = step.get("type", "command")
    if kind == "command":
        command = ctx.expand(step["command"])
        if command.strip().lower().startswith("cd "):
            outcome = _cd(command, ctx, shell)
        elif step.get("long_running") or step.get("run_timeout"):
            outcome = _long_running(command, step, ctx, desktop)
        else:
            outcome = _command(command, step, ctx, shell, desktop)
    elif kind == "write_file":
        outcome = _write_file(step, ctx, desktop)
    elif kind == "screenshot":
        outcome = _screenshot(step, ctx, shell, desktop)
    else:
        msg = f"Unknown step type: {kind}"
        ctx.log(f"[error] {msg}")
        outcome = StepOutcome(FAILED, kind, kind, exit_code=-1, stdout=msg)
    return replace(
        outcome,
        duration_ms=int((time.time() - start) * 1000),
        stops_test=outcome.status == FAILED and not step.get("continue_on_error", False),
    )


def _judge(step: dict, ctx: StepContext, exit_code: int, stdout: str) -> tuple:
    """The rules for command and long-running Steps. Returns (status, stdout)."""
    expected = step.get("expected_exit_code", 0)
    ok = exit_code in expected if isinstance(expected, list) else exit_code == expected
    if ok:
        for pattern in step.get("assert_output_contains") or []:
            if ctx.expand(pattern) not in stdout:
                ok = False
                break
    # Regex assertions run on colour-stripped output, because SGR codes can
    # appear in the middle of a phrase (for example, "\x1b[31;1mfailed: 1\x1b[m").
    if ok and step.get("assert_output_matches"):
        plain = ANSI_RE.sub("", stdout)
        for pattern in step["assert_output_matches"]:
            pattern = ctx.expand(pattern)
            if not re.search(pattern, plain):
                ok = False
                note = f"Assertion failed: output did not match /{pattern}/"
                stdout = f"{stdout}\n{note}"
                ctx.log(note)
                break
    if not ok:
        return FAILED, stdout
    return (WARNING if detect_warnings(stdout) else PASSED), stdout


def _log_output(ctx: StepContext, stdout: str, stderr: str) -> None:
    for line in (stdout or "").splitlines():
        ctx.log(line)
    for line in (stderr or "").splitlines():
        ctx.log(f"[STDERR] {line}")


def _cancelled(kind: str, label: str) -> StepOutcome:
    return StepOutcome(CANCELLED, kind, label, exit_code=-1)


def _command(command, step, ctx, shell, desktop) -> StepOutcome:
    ctx.log(f"$ {command}", is_command=True)
    out = shell.run(command, ctx.cwd, step.get("timeout", 120))
    if ctx.cancel.is_set():
        return _cancelled("command", command)
    if step.get("screenshot"):
        shot = ctx.screenshot_path(step.get("label") or "console", "console")
        if shot:
            err = desktop.capture_console(shot)
            ctx.log(f"[warn] Console screenshot failed: {err}" if err
                    else f"🖼️ Console screenshot: {os.path.basename(shot)}")
    _log_output(ctx, out.stdout, out.stderr)
    status, stdout = _judge(step, ctx, out.exit_code, out.stdout or "")
    return StepOutcome(status, "command", command, out.exit_code, stdout, out.stderr or "")


def _long_running(command, step, ctx, desktop) -> StepOutcome:
    """A server (`dotnet run` with ready_pattern) or a GUI app (run_timeout).
    The runner streams its own output, so this function only applies the rules."""
    ctx.log(f"$ {command}", is_command=True)
    output, exit_code = desktop.run_long_running(command, step, ctx)
    if ctx.cancel.is_set():
        return _cancelled("command", command)
    status, output = _judge(step, ctx, exit_code, output or "")
    return StepOutcome(status, "command", command, exit_code, output)


def _cd(command, ctx, shell) -> StepOutcome:
    target = command.strip()[3:].strip()
    if target.lower().startswith("/d "):
        target = target[3:].strip()
    target = target.strip('"')
    new_cwd = os.path.normpath(target if os.path.isabs(target) else os.path.join(ctx.cwd, target))
    shell.show(f"{ctx.cwd}> {command}")
    if not os.path.isdir(new_cwd):
        msg = f"Folder not found: {new_cwd}"
        ctx.log(f"$ {command}\n  [error] {msg}")
        return StepOutcome(FAILED, "cd", command, exit_code=1, stdout=msg)
    # Pin the SDK in the new folder too, but keep a global.json that a Step made.
    if not os.path.exists(os.path.join(new_cwd, "global.json")):
        ctx.sdk.write_global_json(new_cwd)
    ctx.log(f"$ {command}\n  → {new_cwd}")
    return StepOutcome(PASSED, "cd", command, stdout=f"Changed to: {new_cwd}", new_cwd=new_cwd)


def _write_file(step, ctx, desktop) -> StepOutcome:
    path = os.path.join(ctx.cwd, step["path"])
    content = ctx.expand(step["content"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        msg = f"Could not write file: {e}"
        ctx.log(f"[error] {msg}")
        return StepOutcome(FAILED, "write_file", step["path"], exit_code=1, stdout=msg)

    shot = None
    if step.get("screenshot"):
        shot = ctx.screenshot_path(step.get("label") or os.path.basename(step["path"]), "notepad")
    err = desktop.show_in_notepad(path, shot)
    stdout = f"Wrote {len(content)} bytes"
    if shot and os.path.isfile(shot):
        stdout += f"; screenshot: {os.path.basename(shot)}"
    ctx.log(f"📝 Wrote file: {step['path']}")
    if err:
        ctx.log(f"[warn] Notepad preview failed: {err}")
    return StepOutcome(PASSED, "write_file", step["path"], stdout=stdout)


def _screenshot(step, ctx, shell, desktop) -> StepOutcome:
    """Open a folder or a Settings page and save a screenshot of it."""
    label = step.get("label", "screenshot")
    record_label = f"screenshot: {label}"
    try:
        command = desktop.screenshot_command(step, ctx)
    except OSError as e:
        msg = f"could not write screenshot script: {e}"
        ctx.log(f"[error] {msg}")
        return StepOutcome(FAILED, "screenshot", record_label, exit_code=-1, stdout=msg)
    ctx.log(f"📸 Capture screenshot ({label})", is_command=True)
    out = shell.run(command, ctx.cwd, step.get("timeout", 60))
    if ctx.cancel.is_set():
        return _cancelled("screenshot", record_label)
    _log_output(ctx, out.stdout, out.stderr)
    ok = out.exit_code == 0 and "Screenshot saved:" in (out.stdout or "")
    return StepOutcome(PASSED if ok else FAILED, "screenshot", record_label,
                       out.exit_code, out.stdout or "", out.stderr or "")


class DirectShell:
    """Runs a command as a hidden process. Used when there is no console window."""

    def __init__(self, ctx: StepContext):
        self._ctx = ctx

    def show(self, line: str) -> None:
        pass

    def run(self, command: str, cwd: str, timeout: int) -> ShellResult:
        stdout_lines, stderr_lines = [], []
        exit_code = -1
        try:
            env = self._ctx.env()
            env["DOTNET_CLI_COLORS"] = "1"
            env["DOTNET_SYSTEM_CONSOLE_ALLOW_ANSI_COLOR_REDIRECTION"] = "1"
            env["FORCE_COLOR"] = "1"
            env["TERM"] = "xterm-256color"
            env["MSBUILDTERMINALLOGGER"] = "on"

            proc = subprocess.Popen(
                command, shell=True, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=env,
            )

            def read(stream, into):
                for line in stream:
                    into.append(line)

            readers = [threading.Thread(target=read, args=(proc.stdout, stdout_lines), daemon=True),
                       threading.Thread(target=read, args=(proc.stderr, stderr_lines), daemon=True)]
            for t in readers:
                t.start()

            deadline = time.time() + timeout
            while proc.poll() is None:
                if self._ctx.cancel.is_set():
                    proc.terminate()
                    return ShellResult("", "", -1)
                if time.time() > deadline:
                    proc.terminate()
                    return ShellResult(
                        render_terminal_output("".join(stdout_lines)),
                        "".join(stderr_lines) + f"Timed out after {timeout}s. "
                        f"Stopped the shell (PID {proc.pid}); child processes "
                        "it started may still be running.", -1)
                time.sleep(0.1)

            for t in readers:
                t.join(timeout=5)
            exit_code = proc.returncode if proc.returncode is not None else -1
        except Exception as e:
            stderr_lines.append(str(e))

        return ShellResult(render_terminal_output("".join(stdout_lines)), "".join(stderr_lines), exit_code)


class ConsoleShell:
    """Runs a command in the visible console window and reads its output back.

    The console polls commands.txt in console_dir. Each command goes through a
    wrapper runcmd.bat that saves the output and the exit code to files.
    """

    def __init__(self, console_dir: str, ctx: StepContext, fallback: Shell):
        self._dir = console_dir
        self._ctx = ctx
        self._fallback = fallback

    def _queue(self, line: str) -> None:
        with open(os.path.join(self._dir, "commands.txt"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def show(self, line: str) -> None:
        for ch in "^&<>|()":
            line = line.replace(ch, "^" + ch)
        try:
            self._queue(f"echo {line}")
        except OSError:
            pass

    def run(self, command: str, cwd: str, timeout: int) -> ShellResult:
        stdout_file = os.path.join(self._dir, "stdout.tmp")
        exitcode_file = os.path.join(self._dir, "exitcode.tmp")
        for f in (stdout_file, exitcode_file):
            if os.path.exists(f):
                os.unlink(f)

        try:
            wrapper_file = os.path.join(self._dir, "runcmd.bat")
            with open(wrapper_file, "w", encoding="utf-8") as wf:
                wf.write("@echo off\n")
                wf.write("set MSBUILDTERMINALLOGGER=on\n")
                # Rebuild PATH from the pristine baseline for every command, so a
                # per-test SDK folder pinned earlier is not on PATH for a later
                # test that uses the default SDK. Fall back to %PATH% if the
                # baseline wasn't captured (for example, an older console).
                wf.write('if defined TESTRUNNER_BASE_PATH set "PATH=%TESTRUNNER_BASE_PATH%"\n')
                # The spawned console's PATH may lack System32, so bare tools
                # like `powershell` aren't found (same reason chcp uses a full
                # path). Add the core Windows directories to PATH.
                wf.write('set "PATH=%PATH%;%SystemRoot%\\System32;%SystemRoot%\\System32\\WindowsPowerShell\\v1.0"\n')
                if self._ctx.screenshot_dir:
                    wf.write(f'set "SCREENSHOT_DIR={self._ctx.screenshot_dir}"\n')
                wf.write(self._ctx.sdk.bat_prelude())
                wf.write(f"cd /d {cwd}\n")
                wf.write(f"echo {cwd}^> {command}\n")
                # Capture the real exit code. The previous
                # `cmd && echo 0 || echo 1` form changed every failure to
                # 1, so a step with expected_exit_code: 1 passed on any
                # error (including 9009, command not found). %ERRORLEVEL% is
                # expanded when this line is read, and each statement is on
                # its own line rather than in a parenthesised block, so no
                # delayed expansion is needed. The space before `>` is
                # required: `echo 1>file` would parse the digit as a stream
                # number and write "ECHO is off." instead of the code.
                wf.write(f'{command} > "{stdout_file}" 2>&1\n')
                wf.write(f'echo %ERRORLEVEL% > "{exitcode_file}"\n')
                wf.write(f'type "{stdout_file}"\n')
                wf.write("echo.\n")
            self._queue(f'call "{wrapper_file}"')
        except OSError:
            return self._fallback.run(command, cwd, timeout)

        # The exit code file appears when the command has finished.
        deadline = time.time() + timeout
        while not os.path.exists(exitcode_file):
            if self._ctx.cancel.is_set():
                return ShellResult("", "", -1)
            if time.time() > deadline:
                # Keep what the command printed so far, so a slow step is not
                # mistaken for a hang (for example, `dotnet workload install maui`).
                partial = ""
                try:
                    with open(stdout_file, "r", encoding="utf-8", errors="replace") as f:
                        partial = f.read()
                except OSError:
                    pass
                return ShellResult(
                    render_terminal_output(partial),
                    f"Timed out after {timeout}s. The command is still running in "
                    "the test console window; wait for it or press Ctrl+C there "
                    "before the next run.", -1)
            time.sleep(0.5)

        # Wait a short time so the console finishes writing the files.
        time.sleep(0.3)

        stdout_text = ""
        exit_code = -1
        try:
            if os.path.exists(stdout_file):
                with open(stdout_file, "r", encoding="utf-8", errors="replace") as f:
                    stdout_text = f.read()
            with open(exitcode_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().strip()
                if content:
                    exit_code = int(content)
        except (ValueError, OSError):
            pass

        for f in (stdout_file, exitcode_file):
            try:
                if os.path.exists(f):
                    os.unlink(f)
            except OSError:
                pass

        return ShellResult(render_terminal_output(stdout_text), "", exit_code)
