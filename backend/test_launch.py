"""Check the GUI launch path (issue #57). Run: python test_launch.py"""
import os
import socket
import threading

import app


def test_wait_for_server_detects_listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert app.wait_for_server(port=port, timeout=2.0) is True
    finally:
        srv.close()


def test_wait_for_server_times_out():
    assert app.wait_for_server(port=59999, timeout=0.5) is False


def test_run_app_window_falls_back_when_no_browser():
    original = app.find_app_host
    app.find_app_host = lambda: None
    try:
        assert app.run_app_window() is False
    finally:
        app.find_app_host = original


def test_run_app_window_launches_app_mode():
    captured = {}
    app.subprocess.Popen = lambda cmd, **kw: captured.setdefault("cmd", cmd)
    app.find_app_host = lambda: "msedge.exe"
    original = app.wait_for_window_close
    app.wait_for_window_close = lambda profile, timeout=15.0: True
    try:
        assert app.run_app_window() is True
        assert f"--app={app.APP_URL}" in captured["cmd"]
    finally:
        app.wait_for_window_close = original


def test_wait_for_window_close_detects_lock_release(tmp=None):
    import tempfile as tf
    profile = tf.mkdtemp()
    assert app.wait_for_window_close(profile, timeout=0.5) is False
    open(os.path.join(profile, "lockfile"), "w").close()
    assert app.wait_for_window_close(profile, timeout=0.5) is True


if __name__ == "__main__":
    test_wait_for_server_detects_listener()
    test_wait_for_server_times_out()
    test_run_app_window_falls_back_when_no_browser()
    test_run_app_window_launches_app_mode()
    test_wait_for_window_close_detects_lock_release()
    print("ok")
