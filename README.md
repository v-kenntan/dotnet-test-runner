# .NET SDK Test Runner

A standalone application for automating .NET SDK test execution on Windows VMs. Ships as a single portable folder, no Python or Node.js required on the target machine.

## Quick Start (End Users)

**Requirements:** Windows 10/11 (x64) + .NET SDK installed

1. Copy the `dotnet-test-runner` folder to your VM
2. Run `dotnet-test-runner.exe`
3. Opens in a dedicated app window (a chromeless Microsoft Edge window — no tabs, no address bar). Falls back to your default browser at http://localhost:5000 if no Edge/Chrome is installed.

To force browser/web mode, set the environment variable `APP_MODE=web` before launching. `APP_MODE=native` (the default) uses the app window.

## Selecting a specific SDK install (e.g. zip vs. exe)

By default, all `dotnet` commands run against the SDK on the machine's `PATH` (typically the `.exe`/installer SDK under `C:\Program Files\dotnet`). The **SDK dropdown** at the top of the app lists the installs `dotnet --list-sdks` reports, named the way Windows **Settings → Installed apps** names them — architecture and install source included:

```
11.0.100-preview.7.26366.102 (x64)
10.0.400-preview.0.25322.101 (x64) from Visual Studio
```

(The extra detail comes from the Windows installed-apps registry; if an SDK has no registry entry, its plain version number is shown.)

### Pinning a zip SDK or any other folder

To exercise an SDK that isn't installed — for example a `.zip` SDK, which uses the file-based workload install type — pick **📦 Enter .zip or folder path…** in the SDK dropdown and paste either:

- a path to an SDK **`.zip`** (e.g. `C:\Downloads\dotnet-sdk-11.0.100-win-x64.zip`), or
- a path to an already-extracted **SDK folder** — the one containing `dotnet.exe`.

A zip is extracted once into `sdk_zips/` next to the app's database and reused on every later run, so you only pay the extraction cost the first time. Rebuilding the zip at the same path produces a fresh extraction. The app validates the path immediately and shows the resolved version, so you find out before starting a run.

The whole run then executes against that install (`DOTNET_ROOT` is set, the folder is prepended to `PATH`, and multi-level lookup is disabled), so `dotnet --info` reports it as the **Base Path**.

### Pinning an SDK on a single test

An individual test can override the run's SDK. Open it in the editor and set the **SDK folder (optional)** field, or set an `sdk_path` key in a YAML test definition. This field accepts a **`.zip` or a folder**, exactly like the dropdown, and uses the same extraction cache. Leave it blank to use the run's SDK.

Tests that are only meaningful against a pinned install — the **zip install** workload cases — carry `requires_sdk_path: true` in their YAML and show the same **SDK folder** box inline in the test list, so the folder can be picked without opening the editor. Both boxes write the same `sdk_path` field, so whichever one you use, the other shows the new value. A pinned folder survives restarts: `sdk_path` is per machine, so the built-in re-seed on startup deliberately leaves it (and `workload_version`) alone.

Precedence per test:

1. A valid per-test `sdk_path` → that install.
2. Otherwise the SDK pinned for the run.
3. Otherwise the machine's `PATH` SDK.

If a pinned path is invalid (not found, or no `dotnet.exe` in it or one level below), the run logs a warning and falls back to the `PATH` SDK. This lets, say, the "zip install" workload tests target a zip SDK while the rest of the run uses the default.

## Screenshots (evidence capture)

A test can capture a screenshot as evidence using a `screenshot` step (Windows only). It opens a window, captures just that window, and saves a PNG. Two targets are supported.

**A folder in File Explorer:**

```yaml
- type: screenshot
  label: installertype          # used in the filename and log
  delay: 5                      # seconds to wait before capturing (optional)
  folder_ps: >-                 # PowerShell that outputs the folder to open...
    (Get-ChildItem 'C:\Program Files\dotnet\metadata\workloads' -Recurse
    -Filter msi | Where-Object { $_.Directory.Name -eq 'installertype' } |
    Select-Object -First 1).Directory.FullName
  # ...or a literal path instead of folder_ps:
  # folder: 'C:\Program Files\dotnet\metadata\workloads'
```

**A Windows Settings page**, optionally filtered by typing into the page's own search box:

```yaml
- type: screenshot
  label: settings-installed-apps
  settings_uri: ms-settings:appsfeatures   # any ms-settings: URI
  search: .net                             # typed into the page's search box (optional)
  delay: 6
```

The Settings window is located by UI Automation rather than by its title, so it is found regardless of the Windows display language, and it is closed afterwards only if the step opened it.

Screenshots are saved under `screenshots/<run_id>/` next to the app's database and can be browsed in the **Screenshots** tab of a run's detail view. **Test 1** captures the installed .NET apps in Settings and the `sdk` folders in Explorer, as two extra checkpoints beyond `dotnet --info` (per issue #55) — the Explorer one follows `DOTNET_ROOT`, so it shows the SDK folders of whichever install the run pinned. **Workload Scenario 1** captures the `installertype` folder (per issue #47), and **Workload Scenario 2** captures the `Microsoft.NET.Runtime.MonoAOTCompiler.Task\<version>\Sdk` pack folder before and after it is emptied.

## NuGet feeds

Restores fail with `NU1102` when the required package sources are missing, which surfaces as a *failed test* rather than as a configuration problem. The **📦 NuGet.Config** button in the SDK banner opens a panel that:

- shows the user-level config path (`%APPDATA%\NuGet\NuGet.Config`) and the sources already in it,
- flags each required feed as present or missing — including a feed that is configured but sitting in `<disabledPackageSources>`, which restore silently ignores,
- adds the missing ones in one click, and
- lets you add any other feed by name and URL.

There is only one config file per machine but the feeds a build needs depend on the SDK, so each required feed is labelled with the SDK it is for. NuGet only creates the file once an SDK has been used (e.g. `dotnet --info`); if it is missing, the app says so and creates it when you add a feed. `NuGet.Config.bak` is written before any change.

## Saving results so they outlive the VM

Testers typically run this on a throwaway VM per .NET build, where anything written next to the exe is lost when the VM goes away. Every finished run is therefore **also exported to a persistent destination**, and any past run can be re-exported from its detail view with **💾 Save to results folder**.

The destination is just *a path*, so a synced OneDrive folder, a UNC share (`\\server\share\results`) or a mounted drive all work with no configuration difference — OneDrive is simply "a path that happens to sync". Set it with the `DTR_RESULTS_DIR` environment variable:

```bat
set DTR_RESULTS_DIR=\\server\share\dotnet-test-results
:: ...or a OneDrive folder
set DTR_RESULTS_DIR=%OneDriveCommercial%\dotnet-test-runner
```

Resolution order:

| Order | Source | Result |
|-------|--------|--------|
| 1 | `DTR_RESULTS_DIR` | Used as-is (expands `%VARS%` and `~`) |
| 2 | `%OneDriveCommercial%`, `%OneDriveConsumer%`, `%OneDrive%` | `<OneDrive>\dotnet-test-runner` — first one that exists wins |
| 3 | *(nothing configured)* | `results/` next to the database, **which dies with the VM** |

Falling back to (3) is called out in the UI and in the run log, so a tester is warned *before* tearing the VM down rather than after.

Each run is written to a collision-proof folder, so many VMs can share one destination:

```
<root>/<sdk-version>/<machine>-<timestamp>-<run-id>/
    report.md          # the same Markdown report as the export button
    run.json           # run, results and every step — mergeable across VMs
    test_runner.db     # consistent copy of the whole database
    screenshots/*.png
```

The folder name uses the run's *start* time, so re-exporting a run overwrites its own folder instead of creating duplicates. Set `DTR_EXPORT_DB=0` to skip the database copy.

### The live database goes there too

The exports above are a *record*; the app's own run history lives in `test_runner.db`. That file used to sit next to the exe, so **rebuilding the app wiped the history**, and re-imaging the VM lost it entirely. It is now kept under the same persistent root:

```
<root>/history/test_runner.db
<root>/history/screenshots/<run_id>/*.png
```

so history and screenshots carry across rebuilds *and* across VMs. The Dashboard shows exactly where it is, next to the results destination.

Resolution order:

| Order | Source | Result |
|-------|--------|--------|
| 1 | `DTR_DB_PATH` | Used as-is — a full path to the `.db` file |
| 2 | The results root above, when it is persistent | `<root>\history\test_runner.db` |
| 3 | *(nothing configured)* | Next to the exe, **where the next rebuild wipes it** |

The first time the database moves, any existing history and screenshots are copied across automatically. If the destination cannot be written — an offline share, a read-only folder — the app logs a warning and keeps using the local file rather than refusing to start.

Extracted zip SDKs (`sdk_zips/`) deliberately stay next to the exe: they are hundreds of megabytes and have no business being synced.

> **One machine at a time.** A single shared database keeps the history combined across the VM-per-build workflow, but two machines writing the same OneDrive folder simultaneously is not supported — OneDrive would produce conflict copies. Give each concurrent machine its own `DTR_DB_PATH` if you ever need that.

> **If you use OneDrive sync:** the files are only written *locally* and uploaded in the background. Confirm OneDrive has finished syncing before destroying the VM. A network share has no such delay — the write either lands or reports an error immediately.

## Workload scenarios

The **Workloads** category mirrors the "Test cases for Workloads" plan:

- **Scenario 1** — inspect `dotnet workload info` / `list` / `search` and verify the MSI `installertype` marker is a 0 KB file.
- **Scenario 2** — install `wasm-tools` and `android` with `--skip-manifest-update`, publish a Blazor WASM app twice (`-o 1p0` without AOT, then `-o 2p0` with `<RunAOTCompilation>true</RunAOTCompilation>`) and assert the AOT output is larger, then delete the contents of the `MonoAOTCompiler.Task` `Sdk` pack folder, restore it with `dotnet workload repair`, and uninstall both workloads.
- **Scenario 3** — install the same two workloads **without** `--skip-manifest-update` so the manifests are updated, then uninstall the .NET SDK and assert `dotnet` is no longer available.

> **Scenario 3 removes the .NET SDK from the machine.** It is ordered last so that a "run everything" pass does not break the tests behind it, and every later test would fail until the SDK is reinstalled. Run it on a VM you can re-image.

Scenarios 2 and 3 install MSI workloads and (for Scenario 3) run the SDK uninstall bundle, so the runner should be started elevated. Scenario 2 also deletes files under `C:\Program Files\dotnet`; if the runner is not elevated, that step requests administrator permission with a UAC prompt you have to approve for the run to continue.

## HTTPS development certificate

Test cases **4** (C# web/mvc/webapi) and **9** (F# web/mvc/webapi) host HTTPS sites, which requires a trusted ASP.NET Core development certificate. If it is missing, Windows pops a security dialog *while the run is in progress* and the run stalls until someone clicks it.

To avoid that, the runner checks the certificate **before** starting a run that contains those tests (`dotnet dev-certs https --check --trust`). If it isn't trusted, a prompt appears with three choices:

- **Trust certificate** — runs `dotnet dev-certs https --trust` up front; accept the Windows security dialog, and the run starts right after
- **Run anyway** — starts the run without trusting (the dialog may then interrupt it midway)
- **Cancel run** — nothing is executed

Runs without HTTPS tests skip the check entirely.

## Workload sets: selecting the version

Most Workload Set cases act on a **specific workload set version** (`dotnet workload update --version <version>`, `dotnet workload install maui --version <version>`, or a `workloadVersion` pinned in `global.json`). Those versions only exist on the workloads feed, so the runner cannot derive one — you pick it:

1. Run **Update to latest workload set**, or any test containing `dotnet workload search version`, and note a version from the output.
2. Open the test you want in the editor and fill in **Workload set version** (e.g. `8.0.400`).
3. Run it. Every `{workload_version}` in that test's steps expands to what you entered.

The field is per test, so **Update workload sets from different band** can hold a version from another feature band (`8.0.3xx` on an `8.0.4xx` SDK, `9.0.2xx` on `9.0.3xx`, `10.0.1xx` on `10.0.2xx`) while the others hold the current band's.

If a step uses `{workload_version}` and the field is blank, the test fails immediately with a message saying so, rather than running `--version` with an empty value. Versions you enter are kept when the app restarts — the YAML re-seed does not overwrite them.

## Continuous test cases

Tests marked `continuous: true` in YAML (all **Workload Sets** cases) are executed as one sequence instead of in isolation:

- **Shared working directory** — consecutive continuous tests reuse one temp folder, so files from an earlier test (e.g. the `global.json` written by the pin test) are still there for a later one. Each test still starts at the folder's root, so a `cd` inside one test never moves the next.
- **Stops on failure** — if one fails, the remaining tests in the sequence are reported **Skipped** rather than run against a half-updated machine. The shared folder is kept for debugging, and its path is logged.

A non-continuous test ends the sequence; the next continuous test starts a fresh one. Continuous tests show a ⛓ badge in the test list.

## Usage

1. **Select tests** from the left panel (grouped by category)
2. **Click "Run Selected"** to execute
3. **Watch real-time logs** in the runner view (click a test name to scroll to its output)
4. **Review results** with pass/fail/skip counts
5. **Add custom tests** using the "+ Add Test" button

### Test Outcomes

Each test reports one of these outcomes:

- **Passed** ✓ — all steps succeeded with no warnings
- **Passed with warnings** ⚠ — all steps succeeded but the output contained an MSBuild/NuGet-style warning (e.g. `warning NU1903:` for a package with a known vulnerability). Counted separately from clean passes.
- **Failed** ✗ — a step returned an unexpected exit code or failed an output assertion
- **Skipped** — the test was cancelled before it ran, or an earlier test in its continuous sequence failed

### Completion Notification

When a run finishes, a Windows message box pops up in the foreground with the pass/fail/warning/skip counts. It is shown topmost on purpose: a test's Notepad or browser window often ends up covering the app, which made it hard to tell whether the run had finished. Windows normally refuses to give focus to a background process, so the app also forces the box to the front (briefly clearing the foreground lock timeout and attaching to the active window's input queue); if Windows still refuses, the taskbar button blinks until the box is opened. The app window's title also changes to `✅ Run complete` / `❌ Run complete` / `⛔ Run cancelled` while the result is on screen.

## Building from Source

### Prerequisites (build machine only)

- **Python 3.12** — [python.org](https://python.org)
- **Node.js 18+** — [nodejs.org](https://nodejs.org) (only if modifying the frontend)

### Build Steps

```
build.bat
```

Output: `dist/dotnet-test-runner/` folder containing the standalone executable.

### Rebuilding the Frontend

Only needed if you modify files in `frontend/src/`:

```
cd frontend
npm install
npm run build
```

This outputs static files to `backend/static/` which get bundled into the executable.

## Test Case Format

Tests are defined as YAML in `backend/test_definitions/`. Each test has:

```yaml
tests:
  - id: my-test-id
    category: "C# Console"
    title: "My Test"
    description: "What this tests"
    machine_mutating: false  # true if it modifies global state
    continuous: false        # true to chain with adjacent continuous tests
    requires_sdk_path: false # true to show the SDK folder picker inline in the test list
    workload_version: ""     # optional seed for {workload_version}
    steps:
      - type: command
        command: "dotnet new console -o myapp"
        timeout: 60  # seconds (default: 120)
      - type: command
        command: "cd myapp"
      - type: command
        command: "dotnet build"
      - type: command
        command: "dotnet run"
        expected_exit_code: 0  # default
        assert_output_contains: ["Hello, World!"]
      - type: write_file
        path: "Program.cs"
        content: |
          using System;
          Console.WriteLine("Custom code");
```

### Step Types

| Type | Fields | Description |
|------|--------|-------------|
| `command` | command, timeout, expected_exit_code, assert_output_contains, continue_on_error, run_timeout, screenshot, label | Execute a CLI command |
| `write_file` | path, content, screenshot, label | Write content to a file |
| `screenshot` | label, delay, folder / folder_ps / settings_uri + search | Open a folder in Explorer or a Settings page and capture that window (see [Screenshots](#screenshots-evidence-capture)) |

Two flags capture evidence without a dedicated `screenshot` step:

- `run_timeout: 10` on a `command` step runs a blocking GUI app (WinForms/WPF), leaves the window up for that many seconds, then closes it and passes. Adding `screenshot: true` captures the window before it closes.
- `screenshot: true` on a `write_file` step writes the file through Notepad and captures the Notepad window.

Both are Windows-only and are used by the C# WinForms/WPF and xUnit/NUnit/MSTest tests, the VB equivalents, and the **Non-native SDK (x86)** test. An optional `label` names the resulting PNG; it defaults to the project folder (`command`) or the file name (`write_file`), so set it explicitly when one test captures two files with the same name.

### Placeholders

Step `command` and `write_file` `content` support these placeholders:

| Placeholder | Expands to | Example |
|-------------|-----------|---------|
| `{tfm}` | Target framework moniker of the SDK the run resolved | `net11.0` |
| `{rid}` | Runtime identifier reported by `dotnet --info` (falls back to the machine's) | `win-x64`, `win-arm64` |
| `{assets}` | Path to the bundled read-only `test_assets` folder | — |
| `{workload_version}` | The test's **Workload set version** field (see below) | `8.0.400` |

Placeholders also expand inside `assert_output_contains`, so a step can assert on the version it was told to install.

`{rid}` is what makes the self-contained publish tests (cases **3** and **16**) work unchanged on both x64 and ARM64 VMs: `dotnet publish -r {rid} --sc` publishes `win-arm64` on an ARM64 machine and `win-x64` on an x64 one, and `cd bin\Release\{tfm}\{rid}\publish` follows the output there.

## Adding Tests via UI

1. Click "+ Add Test" in the nav bar
2. Fill in category and title (ID is auto-generated from the title)
3. Define steps as JSON array
4. Save, the test appears in the test list immediately
5. Custom tests can be deleted via the Delete button

## Architecture

```
dotnet-test-runner/
├── backend/
│   ├── app.py              # Flask API server + static file serving
│   ├── executor.py         # Test execution engine (Popen + SSE)
│   ├── results_export.py   # Persistent run export (OneDrive / UNC share)
│   ├── db_store.py         # Where the live database and screenshots are kept
│   ├── sdk_source.py       # Zip SDK extraction + install-root resolution
│   ├── nuget_config.py     # NuGet.Config feed inspection and repair
│   ├── test_definitions/   # Built-in YAML test cases
│   ├── static/             # Pre-built React frontend
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.tsx         # Main app with routing
│   │   ├── api.ts          # API client
│   │   └── components/     # React components
│   └── package.json
├── build.bat               # Windows build script
├── build.spec              # PyInstaller configuration
├── run.bat                 # Dev launcher (Windows)
├── run-prod.bat            # Production launcher (Windows)
└── README.md
```

## Security Note

This app executes commands on the local machine. It binds to **localhost only** (127.0.0.1) by default. Do not expose to a network.

## Development

For local development with hot-reload:

```
# Terminal 1 — backend
cd backend
pip install -r requirements.txt
python app.py

# Terminal 2 — frontend (with hot-reload)
cd frontend
npm install
npm run dev
```

Frontend dev server runs on http://localhost:3000 and proxies API calls to port 5000.
