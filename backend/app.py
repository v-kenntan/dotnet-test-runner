import os
import sys
import json
import sqlite3
import uuid
import webbrowser
import threading
from datetime import datetime
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
import yaml
from executor import TestExecutor

try:
    import webview
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False


def get_base_dir():
    """Return the base directory for bundled resources (PyInstaller or dev)."""
    if getattr(sys, '_MEIPASS', None):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
CORS(app)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_runner.db")
if getattr(sys, '_MEIPASS', None):
    # When bundled, put the DB next to the exe so it persists across runs
    DB_PATH = os.path.join(os.path.dirname(sys.executable), "test_runner.db")
DEFINITIONS_DIR = os.path.join(BASE_DIR, "test_definitions")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS test_cases (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            steps TEXT NOT NULL,
            is_builtin INTEGER DEFAULT 0,
            is_machine_mutating INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 999,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS test_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            status TEXT DEFAULT 'pending',
            environment_info TEXT,
            summary TEXT
        );
        CREATE TABLE IF NOT EXISTS test_results (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            test_case_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            started_at TEXT,
            finished_at TEXT,
            log_output TEXT,
            FOREIGN KEY (run_id) REFERENCES test_runs(id),
            FOREIGN KEY (test_case_id) REFERENCES test_cases(id)
        );
        CREATE TABLE IF NOT EXISTS step_results (
            id TEXT PRIMARY KEY,
            test_result_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            step_type TEXT,
            command TEXT,
            exit_code INTEGER,
            stdout TEXT,
            stderr TEXT,
            status TEXT DEFAULT 'pending',
            duration_ms INTEGER,
            FOREIGN KEY (test_result_id) REFERENCES test_results(id)
        );
    """)
    conn.commit()
    conn.close()


def load_builtin_definitions():
    """Load YAML test definitions into DB if not already present."""
    conn = get_db()
    existing = set(
        row[0] for row in conn.execute(
            "SELECT id FROM test_cases WHERE is_builtin = 1"
        ).fetchall()
    )

    if not os.path.isdir(DEFINITIONS_DIR):
        conn.close()
        return

    for filename in sorted(os.listdir(DEFINITIONS_DIR)):
        if not filename.endswith((".yaml", ".yml")):
            continue
        filepath = os.path.join(DEFINITIONS_DIR, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or "tests" not in data:
            continue

        for test in data["tests"]:
            test_id = test["id"]
            if test_id in existing:
                continue
            conn.execute(
                """INSERT INTO test_cases (id, category, title, description, steps, is_builtin, is_machine_mutating, sort_order)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    test_id,
                    test.get("category", "General"),
                    test["title"],
                    test.get("description", ""),
                    json.dumps(test["steps"]),
                    1 if test.get("machine_mutating", False) else 0,
                    test.get("sort_order", 999),
                ),
            )
    conn.commit()
    conn.close()


# Global executor instance
executor = TestExecutor()


# --- API Routes ---

@app.route("/api/tests", methods=["GET"])
def list_tests():
    conn = get_db()
    rows = conn.execute("SELECT * FROM test_cases ORDER BY sort_order, category, title").fetchall()
    conn.close()
    tests = []
    for row in rows:
        tests.append({
            "id": row["id"],
            "category": row["category"],
            "title": row["title"],
            "description": row["description"],
            "steps": json.loads(row["steps"]),
            "is_builtin": bool(row["is_builtin"]),
            "is_machine_mutating": bool(row["is_machine_mutating"]),
            "sort_order": row["sort_order"],
        })
    return jsonify(tests)


@app.route("/api/tests", methods=["POST"])
def create_test():
    data = request.json
    test_id = data.get("id", str(uuid.uuid4())[:8])
    conn = get_db()
    conn.execute(
        """INSERT INTO test_cases (id, category, title, description, steps, is_builtin, is_machine_mutating)
           VALUES (?, ?, ?, ?, ?, 0, ?)""",
        (
            test_id,
            data["category"],
            data["title"],
            data.get("description", ""),
            json.dumps(data["steps"]),
            1 if data.get("is_machine_mutating", False) else 0,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"id": test_id}), 201


@app.route("/api/tests/<test_id>", methods=["PUT"])
def update_test(test_id):
    data = request.json
    conn = get_db()
    conn.execute(
        """UPDATE test_cases SET category=?, title=?, description=?, steps=?,
           is_machine_mutating=?, updated_at=datetime('now')
           WHERE id=?""",
        (
            data["category"],
            data["title"],
            data.get("description", ""),
            json.dumps(data["steps"]),
            1 if data.get("is_machine_mutating", False) else 0,
            test_id,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "updated"})


@app.route("/api/tests/<test_id>", methods=["DELETE"])
def delete_test(test_id):
    conn = get_db()
    conn.execute("DELETE FROM test_cases WHERE id=? AND is_builtin=0", (test_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


@app.route("/api/runs", methods=["GET"])
def list_runs():
    conn = get_db()
    rows = conn.execute("SELECT * FROM test_runs ORDER BY started_at DESC LIMIT 50").fetchall()
    conn.close()
    runs = [dict(row) for row in rows]
    return jsonify(runs)


@app.route("/api/runs/<run_id>", methods=["GET"])
def get_run(run_id):
    conn = get_db()
    run = conn.execute("SELECT * FROM test_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    results = conn.execute(
        "SELECT * FROM test_results WHERE run_id=? ORDER BY started_at", (run_id,)
    ).fetchall()
    conn.close()
    return jsonify({
        "run": dict(run),
        "results": [dict(r) for r in results],
    })


@app.route("/api/runs/<run_id>/results/<result_id>/steps", methods=["GET"])
def get_step_results(run_id, result_id):
    conn = get_db()
    steps = conn.execute(
        "SELECT * FROM step_results WHERE test_result_id=? ORDER BY step_index",
        (result_id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(s) for s in steps])


@app.route("/api/execute", methods=["POST"])
def start_execution():
    """Start executing selected test cases. Returns a run_id."""
    data = request.json
    test_ids = data.get("test_ids", [])
    if not test_ids:
        return jsonify({"error": "No tests selected"}), 400

    conn = get_db()
    tests = conn.execute(
        f"SELECT * FROM test_cases WHERE id IN ({','.join('?' * len(test_ids))})",
        test_ids,
    ).fetchall()
    conn.close()

    run_id = str(uuid.uuid4())[:12]
    open_console = data.get("open_console", True)
    sdk_version = data.get("sdk_version", None)
    # Debug log to file (since exe has no console)
    import tempfile
    with open(os.path.join(tempfile.gettempdir(), "test_runner_debug.log"), "a") as dbg:
        dbg.write(f"[Execute] run_id={run_id}, sdk_version={sdk_version}\n")
    executor.start_run(run_id, [dict(t) for t in tests], open_console=open_console, sdk_version=sdk_version)
    return jsonify({"run_id": run_id})


@app.route("/api/execute/<run_id>/cancel", methods=["POST"])
def cancel_execution(run_id):
    executor.cancel_run(run_id)
    return jsonify({"status": "cancelled"})


@app.route("/api/execute/<run_id>/stream")
def stream_execution(run_id):
    """SSE endpoint for real-time log streaming."""
    def generate():
        for event in executor.stream_events(run_id):
            yield f"data: {json.dumps(event)}\n\n"
    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/environment", methods=["GET"])
def get_environment():
    """Get current dotnet environment info."""
    import subprocess
    try:
        result = subprocess.run(
            ["dotnet", "--info"], capture_output=True, text=True, timeout=30
        )
        return jsonify({"output": result.stdout, "exit_code": result.returncode})
    except FileNotFoundError:
        return jsonify({"output": "dotnet not found in PATH", "exit_code": -1})
    except Exception as e:
        return jsonify({"output": str(e), "exit_code": -1})


@app.route("/api/sdks", methods=["GET"])
def list_sdks():
    """List all installed .NET SDKs."""
    import subprocess
    try:
        result = subprocess.run(
            ["dotnet", "--list-sdks"], capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return jsonify({"sdks": [], "error": result.stderr})
        sdks = []
        for line in result.stdout.strip().splitlines():
            # Format: "8.0.100 [C:\Program Files\dotnet\sdk]"
            parts = line.split(" [")
            if parts:
                version = parts[0].strip()
                path = parts[1].rstrip("]") if len(parts) > 1 else ""
                sdks.append({"version": version, "path": path})
        return jsonify({"sdks": sdks})
    except FileNotFoundError:
        return jsonify({"sdks": [], "error": "dotnet not found in PATH"})
    except Exception as e:
        return jsonify({"sdks": [], "error": str(e)})


# --- Serve React SPA ---

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path):
    """Serve the React frontend (static files or index.html for SPA routing)."""
    if path and os.path.exists(os.path.join(STATIC_DIR, path)):
        return send_from_directory(STATIC_DIR, path)
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/save-file", methods=["POST"])
def save_file():
    """Show a native save dialog and write content to the chosen path."""
    data = request.json
    content = data.get("content", "")
    default_name = data.get("filename", "export.md")

    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        filepath = filedialog.asksaveasfilename(
            defaultextension=".md",
            initialfile=default_name,
            filetypes=[("Markdown files", "*.md"), ("All files", "*.*")],
        )
        root.destroy()

        if not filepath:
            return jsonify({"saved": False, "reason": "cancelled"})

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return jsonify({"saved": True, "path": filepath})
    except Exception as e:
        return jsonify({"saved": False, "reason": str(e)}), 500


def start_server():
    """Start the Flask server in a background thread."""
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)


def open_browser():
    """Fallback: open in browser if pywebview is unavailable."""
    import time
    time.sleep(1)
    webbrowser.open("http://localhost:5000")


def can_use_webview():
    """Check if pywebview can initialize without actually starting it."""
    if not HAS_WEBVIEW:
        return False
    try:
        from webview.guilib import initialize
        initialize()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    init_db()
    load_builtin_definitions()

    if can_use_webview():
        print("Starting .NET SDK Test Runner (native window)...")
        server_thread = threading.Thread(target=start_server, daemon=True)
        server_thread.start()

        webview.create_window(
            ".NET SDK Test Runner",
            "http://127.0.0.1:5000",
            width=1280,
            height=800,
            min_size=(900, 600),
        )
        webview.start()
    else:
        print("Starting .NET SDK Test Runner on http://localhost:5000")
        threading.Thread(target=open_browser, daemon=True).start()
        app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
