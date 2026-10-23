import os
import sys
import json
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import uuid
import webbrowser
import threading
from flask import Flask, request, jsonify, Response, send_from_directory
import yaml
from executor import TestExecutor, sweep_temp_artifacts
import results_export
import db_store
import sdk_source
import nuget_config


def get_base_dir():
    """Return the base directory for bundled resources (PyInstaller or dev)."""
    if getattr(sys, '_MEIPASS', None):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
app.json.sort_keys = False

DB_PATH = db_store.db_path()
DEFINITIONS_DIR = os.path.join(BASE_DIR, "test_definitions")

# The app's own folder. Stays local even when the DB is kept in OneDrive: it is
# the base for the extracted-SDK cache, which is far too big to sync.
APP_DIR = db_store.default_dir()

# Screenshots captured during runs live next to the DB (persistent), one folder
# per run: <db_dir>/screenshots/<run_id>/*.png
SCREENSHOTS_DIR = db_store.screenshots_dir()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # The DB may sit in a synced folder, where a sync client can hold the file
    # for a moment. Wait rather than failing the request outright.
    conn.execute("PRAGMA busy_timeout=10000")
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
            sdk_path TEXT,
            workload_version TEXT,
            is_continuous INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS test_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            status TEXT DEFAULT 'pending',
            environment_info TEXT,
            summary TEXT,
            sdk_version TEXT,
            sdk_path TEXT
        );
        CREATE TABLE IF NOT EXISTS test_results (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            test_case_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            started_at TEXT,
            finished_at TEXT,
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
    # Migrate: add sdk_version column if missing (for existing DBs)
    try:
        conn.execute("ALTER TABLE test_runs ADD COLUMN sdk_version TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add sdk_path column (pinned SDK install folder, e.g. zip install)
    try:
        conn.execute("ALTER TABLE test_runs ADD COLUMN sdk_path TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add per-test sdk_path column to test_cases
    try:
        conn.execute("ALTER TABLE test_cases ADD COLUMN sdk_path TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add the workload set version a test pins via {workload_version}
    try:
        conn.execute("ALTER TABLE test_cases ADD COLUMN workload_version TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add the continuous-chain flag (Workload Sets run as one sequence)
    try:
        conn.execute("ALTER TABLE test_cases ADD COLUMN is_continuous INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add the flag marking tests that need an SDK folder pinned before
    # they are meaningful (the zip-install workload cases), so the test list can
    # offer the picker inline instead of hiding it in the editor.
    try:
        conn.execute("ALTER TABLE test_cases ADD COLUMN requires_sdk_path INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: same idea for tests that need a workload set version picked inline.
    try:
        conn.execute("ALTER TABLE test_cases ADD COLUMN requires_workload_version INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists
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

    yaml_ids = set()

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
            yaml_ids.add(test_id)
            if test_id in existing:
                # Already seeded: refresh the yaml-owned fields so edits to
                # built-in tests (renames, new steps, re-ordering) reach
                # existing DBs too. Only built-ins are yaml-owned; user
                # created tests (is_builtin = 0) are left untouched.
                # workload_version is deliberately NOT refreshed: the tester
                # types it in the editor before a run, so re-seeding it on every
                # startup would wipe the version they just selected. sdk_path is
                # excluded for the same reason - it is pinned per machine, not
                # owned by the yaml.
                conn.execute(
                    """UPDATE test_cases
                          SET category = ?, title = ?, description = ?, steps = ?,
                              is_machine_mutating = ?, sort_order = ?,
                              is_continuous = ?, requires_sdk_path = ?,
                              requires_workload_version = ?
                        WHERE id = ? AND is_builtin = 1""",
                    (
                        test.get("category", "General"),
                        test["title"],
                        test.get("description", ""),
                        json.dumps(test["steps"]),
                        1 if test.get("machine_mutating", False) else 0,
                        test.get("sort_order", 999),
                        1 if test.get("continuous", False) else 0,
                        1 if test.get("requires_sdk_path", False) else 0,
                        1 if test.get("requires_workload_version", False) else 0,
                        test_id,
                    ),
                )
                continue
            conn.execute(
                """INSERT INTO test_cases (id, category, title, description, steps, is_builtin, is_machine_mutating, sort_order, sdk_path, workload_version, is_continuous, requires_sdk_path, requires_workload_version)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    test_id,
                    test.get("category", "General"),
                    test["title"],
                    test.get("description", ""),
                    json.dumps(test["steps"]),
                    1 if test.get("machine_mutating", False) else 0,
                    test.get("sort_order", 999),
                    (test.get("sdk_path") or None),
                    (test.get("workload_version") or None),
                    1 if test.get("continuous", False) else 0,
                    1 if test.get("requires_sdk_path", False) else 0,
                    1 if test.get("requires_workload_version", False) else 0,
                ),
            )
    # Prune built-ins whose YAML definition has been removed, so deleting a
    # definition file actually takes effect on DBs that already seeded it.
    # Guarded on yaml_ids being non-empty so a missing or unreadable
    # definitions directory can never wipe every built-in test. Past run
    # results are intentionally left alone to preserve history.
    stale = existing - yaml_ids
    if yaml_ids and stale:
        conn.executemany(
            "DELETE FROM test_cases WHERE id = ? AND is_builtin = 1",
            [(test_id,) for test_id in stale],
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
            "sdk_path": row["sdk_path"],
            "workload_version": row["workload_version"],
            "is_continuous": bool(row["is_continuous"]),
            "requires_sdk_path": bool(row["requires_sdk_path"]),
            "requires_workload_version": bool(row["requires_workload_version"]),
        })
    return jsonify(tests)


@app.route("/api/tests", methods=["POST"])
def create_test():
    data = request.json
    test_id = data.get("id", str(uuid.uuid4())[:8])
    conn = get_db()
    conn.execute(
        """INSERT INTO test_cases (id, category, title, description, steps, is_builtin, is_machine_mutating, sdk_path, workload_version, is_continuous)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
        (
            test_id,
            data["category"],
            data["title"],
            data.get("description", ""),
            json.dumps(data["steps"]),
            1 if data.get("is_machine_mutating", False) else 0,
            (data.get("sdk_path") or "").strip() or None,
            (data.get("workload_version") or "").strip() or None,
            1 if data.get("is_continuous", False) else 0,
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
           is_machine_mutating=?, sdk_path=?, workload_version=?, is_continuous=?,
           updated_at=datetime('now')
           WHERE id=?""",
        (
            data["category"],
            data["title"],
            data.get("description", ""),
            json.dumps(data["steps"]),
            1 if data.get("is_machine_mutating", False) else 0,
            (data.get("sdk_path") or "").strip() or None,
            (data.get("workload_version") or "").strip() or None,
            1 if data.get("is_continuous", False) else 0,
            test_id,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "updated"})


_INLINE_FIELDS = {"sdk-path": "sdk_path", "workload-version": "workload_version"}


@app.route("/api/tests/<test_id>/<field>", methods=["PUT"])
def set_test_field(test_id, field):
    """Set (or clear) one test's SDK folder or workload set version without
    rewriting the whole test.

    The test list offers these inline, so it must not touch the steps/category
    the editor owns.
    """
    column = _INLINE_FIELDS.get(field)
    if not column:
        return jsonify({"error": f"Unknown field {field}"}), 404
    value = ((request.json or {}).get(column) or "").strip() or None
    conn = get_db()
    cur = conn.execute(
        f"UPDATE test_cases SET {column}=?, updated_at=datetime('now') WHERE id=?",
        (value, test_id),
    )
    conn.commit()
    updated = cur.rowcount
    conn.close()
    if not updated:
        return jsonify({"error": f"Unknown test {test_id}"}), 404
    return jsonify({"status": "updated", column: value})


@app.route("/api/workload-versions", methods=["GET"])
def list_workload_versions():
    """Workload set versions available for the run's SDK band, for the inline picker.

    Runs `dotnet workload search version` under a global.json pinning the run's
    SDK version (same as the test steps do), so the list matches its feature band.
    """
    _refresh_path()
    dotnet = _dotnet_exe(request.args.get("sdk_path") or None)
    sdk_version = request.args.get("sdk_version") or None
    cmd = [dotnet, "workload", "search", "version", "--take", "20", "--format", "json"]
    with tempfile.TemporaryDirectory(prefix="wl_versions_") as d:
        if sdk_version:
            with open(os.path.join(d, "global.json"), "w") as f:
                json.dump({"sdk": {"version": sdk_version, "rollForward": "disable"}}, f)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=d,
                               creationflags=NO_WINDOW)
        except FileNotFoundError:
            return jsonify({"versions": [], "error": "dotnet not found in PATH"})
        except subprocess.TimeoutExpired:
            return jsonify({"versions": [], "error": "timed out running dotnet workload search version"})
    try:
        # Output may be preceded by first-run/telemetry banners; JSON starts at '['.
        out = r.stdout
        versions = [v["workloadVersion"] for v in json.loads(out[out.index("["):])]
    except (ValueError, KeyError, TypeError):
        return jsonify({"versions": [], "error": (r.stderr or r.stdout or "").strip()[:500]})
    return jsonify({"versions": versions})


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
    # environment_info holds the full `dotnet --info` output (several KB per run)
    # and the history list never renders it, so it is excluded here. That keeps
    # each row small enough for the client to hold every run and paginate/filter
    # locally instead of round-tripping per page.
    rows = conn.execute(
        """SELECT id, started_at, finished_at, status, summary, sdk_version, sdk_path
           FROM test_runs ORDER BY started_at DESC"""
    ).fetchall()
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


@app.route("/api/runs/<run_id>/screenshots", methods=["GET"])
def list_screenshots(run_id):
    """List screenshots captured during a run (PNG files in the run's folder)."""
    run_dir = os.path.join(SCREENSHOTS_DIR, run_id)
    shots = []
    if os.path.isdir(run_dir):
        for name in os.listdir(run_dir):
            if not name.lower().endswith(".png"):
                continue
            fp = os.path.join(run_dir, name)
            if not os.path.isfile(fp):
                continue
            shots.append({
                "name": name,
                "url": f"/api/runs/{run_id}/screenshots/{name}",
                "modified": os.path.getmtime(fp),
                "size": os.path.getsize(fp),
            })
    shots.sort(key=lambda s: s["modified"])
    return jsonify(shots)


@app.route("/api/runs/<run_id>/screenshots/<path:filename>", methods=["GET"])
def get_screenshot(run_id, filename):
    """Serve a single screenshot PNG. Guarded against path traversal."""
    run_dir = os.path.abspath(os.path.join(SCREENSHOTS_DIR, run_id))
    target = os.path.abspath(os.path.join(run_dir, filename))
    if not target.startswith(run_dir + os.sep) or not os.path.isfile(target):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(run_dir, os.path.basename(target))


@app.route("/api/results-destination", methods=["GET"])
def results_destination():
    """Where finished runs are exported to, and whether that outlives this VM."""
    root, source = results_export.resolve_results_root(APP_DIR)
    return jsonify({
        "root": root,
        "source": source,
        "persistent": results_export.is_persistent(source),
        "env_var": results_export.ENV_ROOT,
        "db_path": DB_PATH,
        "db_source": db_store.db_source(),
        "db_persistent": results_export.is_persistent(db_store.db_source()),
        "db_env_var": db_store.ENV_DB,
    })


@app.route("/api/runs/<run_id>/export", methods=["POST"])
def export_run(run_id):
    """Re-export a past run to the results folder. Overwrites its own folder."""
    result = results_export.export_run(run_id, DB_PATH, SCREENSHOTS_DIR)
    return jsonify(result), (200 if result.get("exported") else 500)


@app.route("/api/execute", methods=["POST"])
def start_execution():
    """Start executing selected test cases. Returns a run_id."""
    data = request.json
    test_ids = data.get("test_ids", [])
    if not test_ids:
        return jsonify({"error": "No tests selected"}), 400

    conn = get_db()
    # ORDER BY sort_order so tests always execute in their defined sequence.
    # Without it SQLite returns rows in arbitrary (rowid) order, so a run could
    # e.g. install a workload after the test that depends on it.
    tests = conn.execute(
        f"SELECT * FROM test_cases WHERE id IN ({','.join('?' * len(test_ids))})"
        " ORDER BY sort_order, category, title",
        test_ids,
    ).fetchall()
    conn.close()

    run_id = str(uuid.uuid4())[:12]
    sdk_version = data.get("sdk_version", None)
    sdk_path = data.get("sdk_path", None)
    executor.start_run(run_id, [dict(t) for t in tests], sdk_version=sdk_version, sdk_path=sdk_path)
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


# The packaged exe has no console, so each dotnet call would flash its own window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _refresh_path():
    """Refresh PATH from the registry so newly installed tools are found."""
    machine_path = os.environ.get("PATH", "")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
            machine_path = winreg.QueryValueEx(key, "Path")[0]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            user_path = winreg.QueryValueEx(key, "Path")[0]
        os.environ["PATH"] = machine_path + ";" + user_path
    except Exception:
        pass


@app.route("/api/environment", methods=["GET"])
def get_environment():
    """Get current dotnet environment info."""
    _refresh_path()
    try:
        result = subprocess.run(
            ["dotnet", "--info"], capture_output=True, text=True, timeout=30,
            creationflags=NO_WINDOW,
        )
        # Return stdout even on non-zero exit code; preview SDKs may return
        # non-zero while still printing valid info.
        output = result.stdout or result.stderr
        return jsonify({"output": output, "exit_code": 0 if result.stdout.strip() else result.returncode})
    except FileNotFoundError:
        return jsonify({"output": "dotnet not found in PATH", "exit_code": -1})
    except Exception as e:
        return jsonify({"output": str(e), "exit_code": -1})


@app.route("/api/sdks", methods=["GET"])
def list_sdks():
    """List all installed .NET SDKs."""
    _refresh_path()
    try:
        result = subprocess.run(
            ["dotnet", "--list-sdks"], capture_output=True, text=True, timeout=30,
            creationflags=NO_WINDOW,
        )
        # Parse stdout even on non-zero exit code; preview SDKs may return
        # non-zero while still printing valid SDK list.
        sdks = []
        output = result.stdout.strip()
        if not output and result.returncode != 0:
            return jsonify({"sdks": [], "error": result.stderr})
        # Settings-style names ("11.0.100-preview.7.26381.103 (x64)") come from
        # the installed-apps registry; `dotnet --list-sdks` has no architecture
        # or install-source information of its own (issue #74).
        by_version = sdk_source.installed_sdk_labels()
        for line in output.splitlines():
            # Format: "8.0.100 [C:\Program Files\dotnet\sdk]"
            parts = line.split(" [")
            if parts:
                version = parts[0].strip()
                path = parts[1].rstrip("]") if len(parts) > 1 else ""
                sdks.append({
                    "version": version,
                    "path": path,
                    "label": sdk_source.label_for(version, path, by_version),
                })
        return jsonify({"sdks": sdks})
    except FileNotFoundError:
        return jsonify({"sdks": [], "error": "dotnet not found in PATH"})
    except Exception as e:
        return jsonify({"sdks": [], "error": str(e)})


def _dotnet_exe(sdk_path=None):
    """dotnet executable to use: a pinned SDK install root, else the PATH one."""
    if sdk_path:
        exe = os.path.join(sdk_path, "dotnet.exe")
        if os.path.isfile(exe):
            return exe
    return "dotnet"


def _dev_certs(args, sdk_path=None, timeout=60):
    """Run `dotnet dev-certs https <args>` and report whether the cert is trusted."""
    _refresh_path()
    try:
        result = subprocess.run(
            [_dotnet_exe(sdk_path), "dev-certs", "https"] + args,
            capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
        )
        return jsonify({
            "trusted": result.returncode == 0,
            "exit_code": result.returncode,
            "output": (result.stdout or result.stderr or "").strip(),
        })
    except FileNotFoundError:
        return jsonify({"trusted": False, "exit_code": -1, "output": "dotnet not found in PATH"})
    except subprocess.TimeoutExpired:
        return jsonify({"trusted": False, "exit_code": -1,
                        "output": "Timed out waiting for dotnet dev-certs (was the security dialog left open?)"})
    except Exception as e:
        return jsonify({"trusted": False, "exit_code": -1, "output": str(e)})


@app.route("/api/dev-certs", methods=["GET"])
def check_dev_certs():
    """Report whether a trusted ASP.NET Core HTTPS development certificate exists.

    Tests that host HTTPS sites (test cases 4 and 9) trigger an interactive
    Windows trust dialog when the certificate is missing or untrusted, which
    stalls a run midway. The UI checks this before starting a run instead.
    """
    return _dev_certs(["--check", "--trust"], request.args.get("sdk_path") or None)


@app.route("/api/dev-certs/trust", methods=["POST"])
def trust_dev_certs():
    """Run `dotnet dev-certs https --trust` up front, before any test starts.

    Windows shows a security dialog the user has to accept, hence the long
    timeout — the call blocks until they answer it.
    """
    sdk_path = (request.json or {}).get("sdk_path") or None
    return _dev_certs(["--trust"], sdk_path, timeout=300)


@app.route("/api/sdk-path/resolve", methods=["POST"])
def resolve_sdk_path():
    """Turn a typed SDK path into an install root the runner can use.

    Accepts an install folder or a ``.zip`` SDK, which is extracted on first use
    so testers can point straight at a downloaded zip (issue #74). Extraction of
    a real SDK zip takes a while, hence the generous timeout on the caller side.
    """
    raw = (request.json or {}).get("path") or ""
    root, error = sdk_source.resolve_sdk_root(raw, APP_DIR)
    if error:
        return jsonify({"ok": False, "error": error})
    version = sdk_source.sdk_version(root)
    if not version:
        return jsonify({
            "ok": False,
            "error": f"dotnet in {root} did not report a version; is it a complete SDK?",
        })
    # Zip SDKs are not registered as installed apps, so they get no Settings-style
    # name; mark the source instead so it is distinguishable in the picker.
    is_zip = raw.strip().strip('"').strip("'").lower().endswith(".zip")
    return jsonify({
        "ok": True,
        "root": root,
        "version": version,
        "label": f"{version} ({'zip' if is_zip else 'folder'})",
    })


@app.route("/api/nuget-config", methods=["GET"])
def get_nuget_config():
    """Current user-level NuGet.Config sources and which required feeds are missing.

    Missing feeds make restores fail with NU1102, which surfaces as a failed test
    rather than a configuration problem, so the app reports them up front (issue #64).
    """
    return jsonify(nuget_config.read_state())


@app.route("/api/nuget-config/add", methods=["POST"])
def add_nuget_feeds():
    """Add the selected package sources to the user-level NuGet.Config."""
    body = request.json or {}
    feeds = body.get("feeds") or []
    if not isinstance(feeds, list) or not feeds:
        return jsonify({"ok": False, "error": "no feeds given"})
    result = nuget_config.add_feeds(feeds, backup=bool(body.get("backup", True)))
    return jsonify(result)


@app.route("/api/nuget-config/open", methods=["POST"])
def open_nuget_config():
    """Open the user-level NuGet.Config in Notepad so testers can paste a full config (issue #76)."""
    return jsonify(nuget_config.open_in_notepad())


@app.route("/api/pick-folder", methods=["POST"])
def pick_folder():
    """Show a native folder-picker so the user can choose an SDK install folder.

    Returns the chosen folder and whether it contains a dotnet executable, so the
    UI can pin a specific SDK install (e.g. a zip-extracted SDK) for a run.
    """
    def has_dotnet(folder):
        return bool(folder) and os.path.isfile(os.path.join(folder, "dotnet.exe"))

    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Select .NET SDK install folder (contains dotnet.exe)")
        root.destroy()

        if not folder:
            return jsonify({"picked": False, "reason": "cancelled"})
        folder = os.path.normpath(folder)
        return jsonify({"picked": True, "path": folder, "has_dotnet": has_dotnet(folder)})
    except Exception as e:
        return jsonify({"picked": False, "reason": str(e)}), 500


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


APP_URL = "http://127.0.0.1:5000"

# Chromium "--app" mode gives a chromeless window: no tabs, no address bar.
# ponytail: Edge ships with Windows, so this needs no CLR, no pythonnet and no
# extra dependency — which is exactly what made issue #57 possible.
BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_app_host():
    """Path to a Chromium binary that supports --app, or None."""
    for path in BROWSER_CANDIDATES:
        if os.path.exists(path):
            return path
    return shutil.which("msedge") or shutil.which("chrome")


def wait_for_server(port=5000, timeout=20.0):
    """Block until the Flask server accepts connections. False on timeout."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def wait_for_window_close(profile, timeout=15.0):
    """Block while the app window is open.

    Chromium holds an exclusive handle on <profile>/lockfile for its lifetime,
    so a failed rename means "still running". ponytail: cheaper and more
    portable than walking the process table for our --user-data-dir.
    Returns False if the lock never appeared (nothing to wait on).
    """
    import time
    lock = os.path.join(profile, "lockfile")
    deadline = time.monotonic() + timeout
    while not os.path.exists(lock):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.25)
    while True:
        if not os.path.exists(lock):
            return True  # Chromium removes the lock on a clean exit
        try:
            os.rename(lock, lock + ".probe")
            os.rename(lock + ".probe", lock)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(1.0)


def run_app_window():
    """Open the GUI window and block until the user closes it. False if unavailable."""
    host = find_app_host()
    if not host:
        print("No Edge/Chrome found for app-window mode; falling back to browser.")
        return False
    # A dedicated profile keeps this window out of the user's normal browser
    # session and gives us a lockfile to wait on.
    profile = os.path.join(tempfile.gettempdir(), "dotnet-test-runner-window")
    try:
        subprocess.Popen([
            host,
            f"--app={APP_URL}",
            f"--user-data-dir={profile}",
            "--window-size=1280,800",
            "--no-first-run",
            "--no-default-browser-check",
        ], cwd=tempfile.gettempdir())  # don't pin the app's folder open (see nuget_config)
    except OSError as e:
        print(f"App window unavailable ({e}); falling back to browser.")
        return False
    if not wait_for_window_close(profile):
        # Window is up but unobservable — keep serving rather than exiting.
        threading.Event().wait()
    return True


if __name__ == "__main__":
    for note in db_store.notes():
        print(note)
    print(f"Database: {DB_PATH}")
    init_db()
    load_builtin_definitions()

    # Reclaim temp dirs stranded by earlier crashes, plus retained working
    # directories from failed tests that are now older than the cutoff.
    # Backgrounded so a large sweep never delays startup.
    threading.Thread(target=sweep_temp_artifacts, daemon=True).start()

    # APP_MODE=native|web (default: native). native = chromeless GUI window.
    mode = os.environ.get("APP_MODE", "native").strip().lower()
    if mode not in ("native", "web"):
        print(f"Unknown APP_MODE={mode!r}, falling back to 'native'.")
        mode = "native"

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    wait_for_server()

    if mode == "native":
        print("Starting .NET SDK Test Runner (app window)...")
        if run_app_window():
            sys.exit(0)

    print(f"Starting .NET SDK Test Runner on {APP_URL}")
    webbrowser.open(APP_URL)
    server_thread.join()
