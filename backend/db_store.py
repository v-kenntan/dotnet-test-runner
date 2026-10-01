"""Where the live database (and the screenshots beside it) is kept.

Testers get a fresh VM per .NET build and rebuild the app often, so a database
sitting next to the exe is wiped by the next build and dies with the VM. When a
persistent destination is available - OneDrive, a UNC share, or an explicit
``DTR_RESULTS_DIR`` - the live DB is kept there instead, under ``history/``, so
run history carries across rebuilds and across VMs.

One shared DB, not one per machine: a VM is created per test pass, so a
per-machine file would start every pass with an empty history. That is safe
because only one machine runs at a time. Two machines writing the same OneDrive
folder concurrently is *not* supported - OneDrive would make conflict copies.

``DTR_DB_PATH`` overrides everything with an explicit file path. If the chosen
destination cannot be written (offline share, read-only folder) the DB falls
back to its old place next to the exe, so the app always starts.
"""

import os
import shutil
import sqlite3
import sys

import results_export

ENV_DB = "DTR_DB_PATH"
DB_NAME = "test_runner.db"
SCREENSHOTS_DIRNAME = "screenshots"

# Subfolder of the results root. Per-run exports use <root>/<sdk-version>/...,
# so this name only has to avoid colliding with an SDK version.
HISTORY_DIRNAME = "history"


def default_dir():
    """The app's own folder: next to the exe when frozen, else the source dir.

    This stays local even when the DB moves, because it is also the base for
    the extracted-SDK cache, which is far too big to sync.
    """
    if getattr(sys, "_MEIPASS", None):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resolve_db_path(fallback_dir):
    """Return (path, source) for the live database file, without touching disk."""
    configured = (os.environ.get(ENV_DB) or "").strip()
    if configured:
        expanded = os.path.expandvars(os.path.expanduser(configured))
        return os.path.abspath(expanded), ENV_DB

    root, source = results_export.resolve_results_root(fallback_dir)
    if results_export.is_persistent(source):
        return os.path.join(root, HISTORY_DIRNAME, DB_NAME), source

    return os.path.join(os.path.abspath(fallback_dir), DB_NAME), "local"


def _writable(directory):
    probe = os.path.join(directory, ".dtr-write-test")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


def _migrate(old_db, new_db, notes):
    """Carry an existing local history over the first time the DB moves."""
    if os.path.exists(new_db) or not os.path.exists(old_db):
        return
    try:
        results_export.copy_db(old_db, new_db)
    except (OSError, sqlite3.Error) as exc:
        notes.append(f"[warn] could not move the existing history: {exc}")
        return
    notes.append(f"Moved the existing run history to {new_db}")

    old_shots = os.path.join(os.path.dirname(old_db), SCREENSHOTS_DIRNAME)
    if os.path.isdir(old_shots):
        try:
            shutil.copytree(
                old_shots,
                os.path.join(os.path.dirname(new_db), SCREENSHOTS_DIRNAME),
                dirs_exist_ok=True,
            )
        except OSError as exc:
            notes.append(f"[warn] could not move the existing screenshots: {exc}")


def _prepare():
    local = os.path.join(default_dir(), DB_NAME)
    path, source = resolve_db_path(default_dir())
    if os.path.abspath(path) == os.path.abspath(local):
        return local, "local", []

    notes = []
    if not _writable(os.path.dirname(path)):
        notes.append(
            f"[warn] {os.path.dirname(path)} is not writable - keeping the database "
            f"next to the app, where a rebuild will wipe it."
        )
        return local, "local", notes

    _migrate(local, path, notes)
    return path, source, notes


_resolved = None


def _resolve_once():
    global _resolved
    if _resolved is None:
        _resolved = _prepare()
    return _resolved


def db_path():
    """Path to the live database. Resolved once, shared by every module."""
    return _resolve_once()[0]


def db_source():
    """Which rule chose the database location (an env var name, or "local")."""
    return _resolve_once()[1]


def notes():
    """Startup messages about the database location, if any."""
    return list(_resolve_once()[2])


def screenshots_dir():
    """Run screenshots live beside the DB, so they persist with the history."""
    return os.path.join(os.path.dirname(db_path()), SCREENSHOTS_DIRNAME)


def checkpoint():
    """Fold the write-ahead log back into the DB file.

    A synced folder copies each file independently, so a run must not finish
    with committed pages left in the -wal: OneDrive could upload the .db and
    the -wal at different times and hand the next VM an inconsistent pair.
    """
    try:
        conn = sqlite3.connect(db_path())
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # Never let housekeeping fail a finished run.
