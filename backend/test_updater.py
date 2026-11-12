"""Run the real Updater script on a fake App folder (issue #89). Run: python test_updater.py"""
import os
import shutil
import subprocess
import sys
import tempfile

import updater

# Any small exe works as the "app": the script starts it at the end.
FAKE_EXE = os.path.join(os.environ["SystemRoot"], "System32", "hostname.exe")
DEAD_PID = 99999996


def make_app():
    app_dir = tempfile.mkdtemp(prefix="dtr-app-")
    os.makedirs(os.path.join(app_dir, "_internal"))
    open(os.path.join(app_dir, "_internal", "old.txt"), "w").close()
    shutil.copy(FAKE_EXE, os.path.join(app_dir, updater.EXE))
    with open(os.path.join(app_dir, "test_runner.db"), "w") as f:
        f.write("history")
    new_dir = os.path.join(app_dir, updater.STAGING, "dotnet-test-runner")
    os.makedirs(os.path.join(new_dir, "_internal"))
    open(os.path.join(new_dir, "_internal", "new.txt"), "w").close()
    shutil.copy(FAKE_EXE, os.path.join(new_dir, updater.EXE))
    with open(os.path.join(new_dir, updater.EXE), "ab") as f:
        f.write(b"new")  # so the old and new exe differ
    return app_dir, new_dir


def run_updater(app_dir, new_dir, pid=DEAD_PID, env=None):
    script = os.path.join(tempfile.mkdtemp(), "u.cmd")
    with open(script, "w", encoding="oem") as f:
        f.write(updater.updater_script(app_dir, new_dir, pid))
    return subprocess.run(["cmd", "/c", script], capture_output=True, timeout=60, env=env).returncode


def exe_is_new(app_dir):
    with open(os.path.join(app_dir, updater.EXE), "rb") as f:
        return f.read().endswith(b"new")


def test_swap_replaces_zip_items_and_keeps_user_data():
    app_dir, new_dir = make_app()
    assert run_updater(app_dir, new_dir) == 0
    assert os.listdir(os.path.join(app_dir, "_internal")) == ["new.txt"]
    assert exe_is_new(app_dir)
    assert open(os.path.join(app_dir, "test_runner.db")).read() == "history"
    assert not os.path.exists(os.path.join(app_dir, updater.STAGING))
    assert os.path.exists(os.path.join(app_dir, "_internal.old"))
    updater.cleanup(app_dir)
    assert sorted(os.listdir(app_dir)) == ["_internal", updater.EXE, "test_runner.db"]


def test_waits_for_app_even_with_broken_path():
    # The app's PATH can hold an unexpanded %SystemRoot%\system32 (from the registry).
    app_dir, new_dir = make_app()
    app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    env = dict(os.environ, PATH=r"%SystemRoot%\system32")
    assert run_updater(app_dir, new_dir, app.pid, env) == 0
    assert app.poll() is not None  # it swapped only after the app exited
    assert exe_is_new(app_dir)


def test_failed_move_restores_old_files():
    app_dir, new_dir = make_app()
    # An open handle blocks renaming the exe, after _internal was already swapped.
    with open(os.path.join(app_dir, updater.EXE), "rb"):
        assert run_updater(app_dir, new_dir) == 1
    assert os.listdir(os.path.join(app_dir, "_internal")) == ["old.txt"]
    assert not exe_is_new(app_dir)
    assert not os.path.exists(os.path.join(app_dir, "_internal.old"))
    assert updater.take_failure(app_dir)
    assert not updater.take_failure(app_dir)  # reported once

if __name__ == "__main__":
    test_swap_replaces_zip_items_and_keeps_user_data()
    test_waits_for_app_even_with_broken_path()
    test_failed_move_restores_old_files()
    print("ok")
