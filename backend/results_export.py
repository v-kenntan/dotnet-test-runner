"""Export finished runs to a persistent results folder.

Testers run this app on a throwaway VM per .NET build, so anything written next
to the exe dies with the VM. Every finished run is therefore also written to a
configurable destination that outlives the machine.

The destination is deliberately just *a path*: a synced OneDrive folder, a UNC
share (\\\\server\\share\\results) or a mounted drive all work with no code
difference. OneDrive is simply "a path that happens to sync", which is why this
needs no Graph API, no token and no extra dependency.

Layout, which is collision-proof because many VMs write into one destination:

    <root>/<sdk-version>/<machine>-<timestamp>-<run-id>/
        report.md
        run.json
        test_runner.db
        screenshots/*.png

The folder name is derived from the run's *start* time, so re-exporting a run
overwrites its own folder instead of piling up duplicates.
"""

import json
import os
import re
import shutil
import socket
import sqlite3

ENV_ROOT = "DTR_RESULTS_DIR"
ENV_INCLUDE_DB = "DTR_EXPORT_DB"

# Checked in order. OneDrive exports these itself when the sync client is set
# up; the commercial (work/school) one wins because that is where test evidence
# belongs.
_ONEDRIVE_VARS = ("OneDriveCommercial", "OneDriveConsumer", "OneDrive")


def resolve_results_root(fallback_dir):
    """Return (root, source) for the folder run exports are written under.

    `source` names which rule won, so the UI can tell a tester whether results
    are actually leaving the VM or just landing next to the exe again.
    """
    configured = (os.environ.get(ENV_ROOT) or "").strip()
    if configured:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(configured))), ENV_ROOT

    for var in _ONEDRIVE_VARS:
        value = (os.environ.get(var) or "").strip()
        if value and os.path.isdir(value):
            return os.path.join(os.path.abspath(value), "dotnet-test-runner"), var

    return os.path.join(os.path.abspath(fallback_dir), "results"), "local"


def is_persistent(source):
    """True when exports land somewhere that survives the VM being destroyed."""
    return source != "local"


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value, default):
    cleaned = _UNSAFE.sub("-", str(value or "").strip()).strip("-.")
    return cleaned or default


def _stamp(iso):
    """Compact sortable timestamp from an ISO date, for use in a folder name."""
    digits = re.sub(r"[^0-9]", "", str(iso or ""))
    if len(digits) >= 14:
        return f"{digits[:8]}-{digits[8:14]}"
    return "no-date"


# --- Markdown report -------------------------------------------------------
# Ported from frontend/src/report.ts: auto-export happens in the backend with
# no browser attached, so the report cannot be generated in the UI. Keep the
# two in sync when either changes.

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_MD_ICONS = {
    "passed": "✅",
    "passed_with_warnings": "⚠️",
    "failed": "❌",
    "skipped": "⏭️",
    "cancelled": "⚠️",
}

_RESULT_LINES = {
    "passed": "  ✅ PASSED",
    "passed_with_warnings": "  ⚠️ PASSED (warnings)",
    "skipped": "  ⏭️ SKIPPED",
    "cancelled": "  🚫 CANCELLED",
}


def strip_ansi(text):
    return _ANSI.sub("", text or "")


def _md_icon(status):
    return _MD_ICONS.get(status, "○")


def _format_time(iso):
    if not iso:
        return "-"
    return str(iso).replace("T", " ")[:19]


def reconstruct_logs(conn, run_id, results, summary_json):
    """Rebuild the run log from persisted step results."""
    logs = [f"▶ Run started ({len(results)} tests)"]

    for result in results:
        logs.append(f"\n━━━ {result['title']} ━━━")
        steps = conn.execute(
            "SELECT command, stdout, stderr FROM step_results"
            " WHERE test_result_id=? ORDER BY step_index",
            (result["id"],),
        ).fetchall()
        for step in steps:
            if step["command"]:
                logs.append(f"$ {step['command']}")
            if step["stdout"]:
                logs.extend(line for line in step["stdout"].split("\n") if line)
            if step["stderr"]:
                logs.extend(
                    f"[STDERR] {line}" for line in step["stderr"].split("\n") if line
                )
        logs.append(_RESULT_LINES.get(result["status"], "  ❌ FAILED"))

    if summary_json:
        logs.append(f"\n✅ Run complete: {summary_json}")
    return logs


def generate_markdown(meta, summary, rows, logs):
    """Build the Markdown report. Mirrors generateMarkdown() in report.ts."""
    md = "# .NET SDK Test Run Report\n\n"
    for key, value in meta:
        md += f"**{key}:** {value}\n"
    md += "\n"

    if summary:
        total = (
            summary.get("passed", 0)
            + summary.get("failed", 0)
            + summary.get("skipped", 0)
            + summary.get("warnings", 0)
        )
        md += "## Summary\n\n"
        md += "| Total | Passed | Warnings | Failed | Skipped |\n"
        md += "|-------|--------|----------|--------|--------|\n"
        md += (
            f"| {total} | ✅ {summary.get('passed', 0)} | ⚠️ {summary.get('warnings', 0)}"
            f" | ❌ {summary.get('failed', 0)} | ⏭️ {summary.get('skipped', 0)} |\n\n"
        )

    md += "## Test Results\n\n"
    md += "| # | Test | Category | Status |\n|---|------|----------|--------|\n"
    for i, row in enumerate(rows, start=1):
        status = row["status"]
        md += f"| {i} | {row['title']} | {row['category'] or ''} | {_md_icon(status)} {status} |\n"

    md += "\n## Full Log Output\n\n```console\n"
    for line in logs:
        md += strip_ansi(line) + "\n"
    md += "```\n"
    return md


# --- Export ----------------------------------------------------------------


def copy_db(db_path, dest):
    """Consistent copy of the SQLite DB.

    The live DB runs in WAL mode, so a plain file copy can miss committed pages
    still sitting in the -wal file. The backup API takes care of that.
    """
    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def export_run(run_id, db_path, screenshots_dir, root=None):
    """Write a run's report, structured data, screenshots and DB copy.

    Always returns a dict rather than raising: a failure to reach a network
    share must never take down a finished test run.
    """
    try:
        root_dir, source = (root, "explicit") if root else resolve_results_root(
            os.path.dirname(db_path)
        )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            run = conn.execute(
                "SELECT * FROM test_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run:
                return {"exported": False, "reason": f"run {run_id} not found"}

            results = conn.execute(
                """SELECT r.id, r.test_case_id, r.status, r.started_at, r.finished_at,
                          COALESCE(t.title, r.test_case_id) AS title,
                          COALESCE(t.category, '') AS category
                   FROM test_results r
                   LEFT JOIN test_cases t ON t.id = r.test_case_id
                   WHERE r.run_id=? ORDER BY r.started_at""",
                (run_id,),
            ).fetchall()

            summary = json.loads(run["summary"]) if run["summary"] else None
            logs = reconstruct_logs(conn, run_id, results, run["summary"])

            steps_by_result = {
                result["id"]: [
                    dict(s)
                    for s in conn.execute(
                        "SELECT * FROM step_results WHERE test_result_id=?"
                        " ORDER BY step_index",
                        (result["id"],),
                    ).fetchall()
                ]
                for result in results
            }
        finally:
            conn.close()

        machine = _slug(socket.gethostname(), "machine")
        sdk = _slug(run["sdk_version"], "unknown-sdk")
        folder = f"{machine}-{_stamp(run['started_at'])}-{_slug(run_id, 'run')}"
        out_dir = os.path.join(root_dir, sdk, folder)
        os.makedirs(out_dir, exist_ok=True)

        meta = [("Run ID", run["id"]), ("Machine", socket.gethostname())]
        if "machine_arch" in run.keys() and run["machine_arch"]:
            meta.append(("Machine Architecture", run["machine_arch"]))
        if run["sdk_version"]:
            meta.append(("SDK Version", run["sdk_version"]))
        if run["sdk_path"]:
            meta.append(("SDK Folder", run["sdk_path"]))
        meta.append(("Status", run["status"] or "unknown"))
        meta.append(("Started", _format_time(run["started_at"])))
        if run["finished_at"]:
            meta.append(("Finished", _format_time(run["finished_at"])))

        report = generate_markdown(meta, summary, results, logs)
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(report)

        payload = {
            "run": dict(run),
            "machine": socket.gethostname(),
            "summary": summary,
            "results": [
                dict(result, steps=steps_by_result.get(result["id"], []))
                for result in results
            ],
        }
        with open(os.path.join(out_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        shots = 0
        run_shots = os.path.join(screenshots_dir, run_id)
        if os.path.isdir(run_shots):
            dest_shots = os.path.join(out_dir, "screenshots")
            os.makedirs(dest_shots, exist_ok=True)
            for name in os.listdir(run_shots):
                src = os.path.join(run_shots, name)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(dest_shots, name))
                    shots += 1

        db_copied = False
        if os.environ.get(ENV_INCLUDE_DB, "1") != "0":
            copy_db(db_path, os.path.join(out_dir, "test_runner.db"))
            db_copied = True

        return {
            "exported": True,
            "path": out_dir,
            "root": root_dir,
            "source": source,
            "persistent": is_persistent(source),
            "screenshots": shots,
            "db_copied": db_copied,
        }
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {"exported": False, "reason": str(exc)}
