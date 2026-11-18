"""run_step: one set of Step rules for every Step kind. Run: python test_steps.py"""
import os
import tempfile

from steps import FAILED, PASSED, WARNING, ShellResult, StepContext, run_step


class FakeShell:
    def __init__(self, stdout="", exit_code=0):
        self.result = ShellResult(stdout, "", exit_code)
        self.commands = []

    def run(self, command, cwd, timeout):
        self.commands.append(command)
        return self.result

    def show(self, line):
        pass


class FakeDesktop:
    def __init__(self, output="", exit_code=0):
        self.long_running = (output, exit_code)

    def show_in_notepad(self, path, screenshot_path):
        return None

    def capture_console(self, screenshot_path):
        return None

    def screenshot_command(self, step, ctx):
        return "screenshot.ps1"

    def run_long_running(self, command, step, ctx):
        return self.long_running


def run(step, cwd, shell=None, desktop=None):
    return run_step(step, StepContext(cwd=cwd), shell or FakeShell(), desktop or FakeDesktop())


def test_long_running_uses_expected_exit_code_and_asserts(d):
    step = {"command": "dotnet run", "long_running": True, "expected_exit_code": 1}
    assert run(step, d, desktop=FakeDesktop("boom", 1)).status == PASSED
    # Before, a long-running Step passed on exit 0 and ignored the asserts.
    step = {"command": "dotnet run", "long_running": True, "assert_output_contains": ["Hello"]}
    assert run(step, d, desktop=FakeDesktop("Goodbye", 0)).status == FAILED


def test_write_file_error_is_failed(d):
    open(os.path.join(d, "blocker"), "w").close()
    outcome = run({"type": "write_file", "path": "blocker/x.txt", "content": "x"}, d)
    assert outcome.status == FAILED and outcome.stops_test


def test_cd(d):
    os.mkdir(os.path.join(d, "app"))
    outcome = run({"command": "cd app"}, d)
    assert (outcome.status, outcome.step_type, outcome.new_cwd) == (PASSED, "cd", os.path.join(d, "app"))
    missing = run({"command": "cd nope"}, d)
    assert missing.status == FAILED and missing.new_cwd is None


def test_warning_detection(d):
    shell = FakeShell("warning NU1903: Package has a known vulnerability")
    assert run({"command": "dotnet restore"}, d, shell=shell).status == WARNING


def test_continue_on_error(d):
    shell = FakeShell(exit_code=1)
    assert run({"command": "dotnet build"}, d, shell=shell).stops_test
    assert not run({"command": "dotnet build", "continue_on_error": True}, d, shell=shell).stops_test


if __name__ == "__main__":
    for test in (test_long_running_uses_expected_exit_code_and_asserts, test_write_file_error_is_failed,
                 test_cd, test_warning_detection, test_continue_on_error):
        with tempfile.TemporaryDirectory() as d:
            test(d)
    print("ok")
