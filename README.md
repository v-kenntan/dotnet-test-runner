# .NET SDK Test Runner

A standalone application for automating .NET SDK test execution on Windows VMs. Ships as a single portable folder, no Python or Node.js required on the target machine.

## Quick Start (End Users)

**Requirements:** Windows 10/11 (x64) + .NET SDK installed

1. Copy the `dotnet-test-runner` folder to your VM
2. Run `dotnet-test-runner.exe`
3. Opens in a dedicated app window (a chromeless Microsoft Edge window — no tabs, no address bar). Falls back to your default browser at http://localhost:5000 if no Edge/Chrome is installed.

To force browser/web mode, set the environment variable `APP_MODE=web` before launching. `APP_MODE=native` (the default) uses the app window.

## Selecting a specific SDK install (e.g. zip vs. exe)

By default, all `dotnet` commands run against the SDK on the machine's `PATH` (typically the `.exe`/installer SDK under `C:\Program Files\dotnet`). To exercise a **different SDK install** — for example a `.zip`-extracted SDK, which uses the file-based workload install type — pin an **SDK folder** on the individual test:

1. Open the test in the editor and set the **SDK folder (optional)** field to the SDK install root — the folder that contains `dotnet.exe` (e.g. `C:\dotnet-zip`). Use **📁 Browse** to pick it, or set an `sdk_path` key in a YAML test definition.
2. Run the test. Its commands execute against that install (`DOTNET_ROOT` is set, the folder is prepended to `PATH`, and multi-level lookup is disabled), so `dotnet --info` reports that folder as its **Base Path**.
3. Leave the field blank to use the default `PATH` SDK.

Precedence per test:

1. A valid per-test `sdk_path` → that install.
2. Otherwise the machine's `PATH` SDK.

If a per-test folder is set but invalid (missing `dotnet`), the run logs a warning and falls back to the `PATH` SDK. This lets, say, the "zip install" workload tests target a zip SDK while the rest of the run uses the default.

## Screenshots (evidence capture)

A test can capture a screenshot as evidence using a `screenshot` step (Windows only). It opens a folder in Explorer and saves a PNG of the primary screen:

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

Screenshots are saved under `screenshots/<run_id>/` next to the app's database and can be browsed in the **Screenshots** tab of a run's detail view. **Workload Scenario 1** uses this to capture the `installertype` folder (per issue #47), and **Workload Scenario 2** captures the `Microsoft.NET.Runtime.MonoAOTCompiler.Task\<version>\Sdk` pack folder before and after it is emptied.

## Workload scenarios

The **Workloads** category mirrors the "Test cases for Workloads" plan:

- **Scenario 1** — inspect `dotnet workload info` / `list` / `search` and verify the MSI `installertype` marker is a 0 KB file.
- **Scenario 2** — install `wasm-tools` and `android` with `--skip-manifest-update`, publish a Blazor WASM app twice (`-o 1p0` without AOT, then `-o 2p0` with `<RunAOTCompilation>true</RunAOTCompilation>`) and assert the AOT output is larger, then delete the contents of the `MonoAOTCompiler.Task` `Sdk` pack folder, restore it with `dotnet workload repair`, and uninstall both workloads.
- **Scenario 3** — install the same two workloads **without** `--skip-manifest-update` so the manifests are updated, then uninstall the .NET SDK and assert `dotnet` is no longer available.

> **Scenario 3 removes the .NET SDK from the machine.** It is ordered last so that a "run everything" pass does not break the tests behind it, and every later test would fail until the SDK is reinstalled. Run it on a VM you can re-image.

Scenarios 2 and 3 install MSI workloads and (for Scenario 3) run the SDK uninstall bundle, so the runner must be started elevated.

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
| `command` | command, timeout, expected_exit_code, assert_output_contains, continue_on_error | Execute a CLI command |
| `write_file` | path, content | Write content to a file |

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
