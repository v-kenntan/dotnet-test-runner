import os
import sys
import json
import uuid
import subprocess
import tempfile
import threading
import time
import sqlite3
from datetime import datetime
from queue import Queue, Empty
from typing import Dict, List, Any, Generator


def _get_db_path():
    if getattr(sys, '_MEIPASS', None):
        return os.path.join(os.path.dirname(sys.executable), "test_runner.db")
    return os.path.join(os.path.dirname(__file__), "test_runner.db")


DB_PATH = _get_db_path()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class TestExecutor:
    def __init__(self):
        self._runs: Dict[str, dict] = {}
        self._event_queues: Dict[str, List[Queue]] = {}
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._console_procs: Dict[str, Any] = {}  # run_id -> console_dir path

    def start_run(self, run_id: str, tests: List[dict], open_console: bool = True, sdk_version: str = None):
        """Start executing tests in a background thread."""
        cancel_flag = threading.Event()
        self._cancel_flags[run_id] = cancel_flag
        self._runs[run_id] = {"status": "running", "tests": tests, "sdk_version": sdk_version}
        self._event_queues[run_id] = []

        # Open a separate console window if requested
        if open_console:
            self._open_console(run_id)

        conn = get_db()
        conn.execute(
            "INSERT INTO test_runs (id, started_at, status, sdk_version) VALUES (?, ?, ?, ?)",
            (run_id, datetime.now().isoformat(), "running", sdk_version),
        )
        conn.commit()
        conn.close()

        thread = threading.Thread(
            target=self._execute_run, args=(run_id, tests, cancel_flag), daemon=True
        )
        thread.start()

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
                    f.write(f"@echo off\n")
                    f.write(f"title .NET Test Runner - Run {run_id}\n")
                    f.write(f"echo ========================================\n")
                    f.write(f"echo   .NET SDK Test Runner - Live Console\n")
                    f.write(f"echo ========================================\n")
                    f.write(f"echo.\n")
                    f.write(f":loop\n")
                    f.write(f'if exist "{done_file}" goto end\n')
                    # Check if commands file has content (size > 0)
                    f.write(f'for %%A in ("{cmd_file}") do if %%~zA==0 goto wait\n')
                    # Copy commands to exec file and clear the queue
                    f.write(f'copy /y "{cmd_file}" "{exec_file}" >nul 2>&1\n')
                    f.write(f'type nul > "{cmd_file}"\n')
                    # Execute the commands
                    f.write(f'call "{exec_file}"\n')
                    f.write(f":wait\n")
                    f.write(f"timeout /t 1 /nobreak >nul 2>&1\n")
                    f.write(f"goto loop\n")
                    f.write(f":end\n")
                    f.write(f"echo.\n")
                    f.write(f"echo ========================================\n")
                    f.write(f"echo   Run complete. You may close this window.\n")
                    f.write(f"echo ========================================\n")
                    f.write(f"pause\n")

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
            self._console_procs[run_id] = None

    def _close_console(self, run_id: str):
        """Signal the console that the run is complete."""
        console_dir = self._console_procs.pop(run_id, None)
        if console_dir and isinstance(console_dir, str):
            try:
                done_file = os.path.join(console_dir, "done.txt")
                with open(done_file, "w") as f:
                    f.write("done")
            except Exception:
                pass

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

        # Capture environment info
        env_info = self._capture_environment()
        conn.execute(
            "UPDATE test_runs SET environment_info=? WHERE id=?",
            (env_info, run_id),
        )
        conn.commit()

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
            blank = "echo." if sys.platform == "win32" else "echo"
            self._queue_console_cmd(run_id, blank)
            self._queue_console_cmd(run_id, f"echo ===== {test['title']} =====")
            self._queue_console_cmd(run_id, blank)

            test_passed = self._execute_test(
                run_id, result_id, steps, cancel_flag, conn
            )

            status = "passed" if test_passed else "failed"
            if cancel_flag.is_set():
                status = "cancelled"
                skipped += 1
            elif test_passed:
                passed += 1
            else:
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
        summary = json.dumps({"passed": passed, "failed": failed, "skipped": skipped})
        conn.execute(
            "UPDATE test_runs SET status=?, finished_at=?, summary=? WHERE id=?",
            (final_status, datetime.now().isoformat(), summary, run_id),
        )
        conn.commit()
        conn.close()

        self._emit_event(run_id, {
            "type": "run_end",
            "status": final_status,
            "summary": {"passed": passed, "failed": failed, "skipped": skipped},
        })
        self._end_stream(run_id)

        # Cleanup
        self._cancel_flags.pop(run_id, None)
        self._close_console(run_id)

    def _execute_test(
        self, run_id: str, result_id: str, steps: List[dict],
        cancel_flag: threading.Event, conn: sqlite3.Connection
    ) -> bool:
        """Execute all steps for a single test case. Returns True if all pass."""
        # Create a temp working directory for this test
        work_dir = tempfile.mkdtemp(prefix="dotnet_test_")
        current_dir = work_dir
        all_passed = True

        # Pin SDK version via global.json if specified
        sdk_version = self._runs.get(run_id, {}).get("sdk_version") or None
        if sdk_version:
            global_json = os.path.join(work_dir, "global.json")
            with open(global_json, "w") as f:
                json.dump({"sdk": {"version": sdk_version, "rollForward": "disable"}}, f)

        for idx, step in enumerate(steps):
            if cancel_flag.is_set():
                return False

            step_id = str(uuid.uuid4())[:12]
            step_type = step.get("type", "command")
            timeout = step.get("timeout", 120)
            expected_exit = step.get("expected_exit_code", 0)

            if step_type == "command":
                cmd = step["command"]
                # Handle cd commands by updating current_dir
                if cmd.strip().startswith("cd "):
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
                    # Send cd to console
                    cd_cmd = f"cd /d {current_dir}" if sys.platform == "win32" else f"cd {current_dir}"
                    self._queue_console_cmd(run_id, cd_cmd)
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
                    return False

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

                status = "passed" if step_passed else "failed"
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

            elif step_type == "write_file":
                filepath = os.path.join(current_dir, step["path"])
                os.makedirs(os.path.dirname(filepath), exist_ok=True)

                start_time = time.time()
                wrote_via_notepad = False

                if sys.platform == "win32":
                    try:
                        wrote_via_notepad = self._write_file_via_notepad(filepath, step["content"])
                    except Exception:
                        wrote_via_notepad = False

                if not wrote_via_notepad:
                    # Fallback to direct write
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(step["content"])

                duration_ms = int((time.time() - start_time) * 1000)
                method_label = "via Notepad" if wrote_via_notepad else "direct"

                conn.execute(
                    """INSERT INTO step_results (id, test_result_id, step_index, step_type, command, exit_code, stdout, status, duration_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (step_id, result_id, idx, "write_file", step["path"], 0,
                     f"Wrote {len(step['content'])} bytes ({method_label})", "passed", duration_ms),
                )
                conn.commit()

                self._emit_event(run_id, {
                    "type": "step_output",
                    "result_id": result_id,
                    "step_index": idx,
                    "line": f"📝 Wrote file ({method_label}): {step['path']}",
                })

        return all_passed

    def _write_file_via_notepad(self, filepath: str, content: str) -> bool:
        """
        Write file content, open it in Notepad to display, then close.
        Returns True if successful, False otherwise.
        """
        from pywinauto import Application

        # Write the actual content to the file first
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        # Open Notepad with the file so the user can see the content
        app = Application(backend="win32").start(f'notepad.exe "{filepath}"')

        try:
            # Wait for the Notepad window to be ready
            notepad_window = app.window(title_re=".*Notepad.*|.*" + os.path.basename(filepath) + ".*")
            notepad_window.wait("ready", timeout=3)

            # Brief pause so the user can see the file content
            time.sleep(0.5)

            # Close the tab (Ctrl+W)
            notepad_window.type_keys("^w", pause=0.05)
            time.sleep(0.2)

            # If Notepad asks to save, dismiss with Don't Save
            try:
                save_dialog = app.window(title_re=".*Save.*|.*Notepad.*")
                if save_dialog.exists(timeout=0.2):
                    save_dialog.type_keys("%n", pause=0.05)
            except Exception:
                pass

            return True

        except Exception:
            try:
                app.kill()
            except Exception:
                pass
            return False

    def _queue_console_cmd(self, run_id: str, command: str):
        """Write a command to the console's command queue file."""
        console_dir = self._console_procs.get(run_id)
        if not console_dir or not isinstance(console_dir, str):
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

        if not console_dir or not isinstance(console_dir, str):
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
        try:
            if sys.platform == "win32":
                # Write a dedicated wrapper batch that captures exit code reliably
                wrapper_file = os.path.join(console_dir, "runcmd.bat")
                with open(wrapper_file, "w", encoding="utf-8") as wf:
                    wf.write("@echo off\n")
                    wf.write(f"cd /d {cwd}\n")
                    wf.write(f"echo {cwd}^> {cmd}\n")
                    # Use && and || to reliably capture success/failure
                    wf.write(f'{cmd} > "{stdout_file}" 2>&1 && (echo 0 > "{exitcode_file}") || (echo 1 > "{exitcode_file}")\n')
                    wf.write(f'type "{stdout_file}"\n')
                    wf.write("echo.\n")
                # Tell the console to call the wrapper
                with open(cmd_file, "a", encoding="utf-8") as f:
                    f.write(f'call "{wrapper_file}"\n')
            else:
                with open(cmd_file, "a", encoding="utf-8") as f:
                    f.write(f"cd {cwd}\n")
                    f.write(f"echo '{cwd}$ {cmd}'\n")
                    f.write(f'{cmd} 2>&1 | tee "{stdout_file}"; echo $? > "{exitcode_file}"\n')
                    f.write(f"echo\n")
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

        return (stdout_text, "", exit_code)

    def _run_direct(
        self, cmd: str, cwd: str, timeout: int,
        cancel_flag: threading.Event, run_id: str
    ) -> tuple:
        """Fallback: run command directly when no console is available."""
        stdout_lines = []
        stderr_lines = []
        exit_code = -1

        try:
            env = os.environ.copy()
            env["DOTNET_CLI_COLORS"] = "1"
            env["DOTNET_SYSTEM_CONSOLE_ALLOW_ANSI_COLOR_REDIRECTION"] = "1"
            env["FORCE_COLOR"] = "1"
            env["TERM"] = "xterm-256color"

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

        return ("".join(stdout_lines), "".join(stderr_lines), exit_code)

    def _capture_environment(self) -> str:
        try:
            result = subprocess.run(
                ["dotnet", "--info"], capture_output=True, text=True, timeout=30
            )
            return result.stdout
        except Exception as e:
            return f"Could not capture environment: {e}"
