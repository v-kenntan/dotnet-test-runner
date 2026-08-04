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
- **Skipped** — the test was cancelled before it ran

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
