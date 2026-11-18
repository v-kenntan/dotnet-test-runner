# .NET SDK Test Runner

This app runs .NET SDK tests on Windows VMs. It ships as one portable folder. The target machine does not need Python or Node.js.

## Quick start (end users)

You need Windows 10 or 11 (x64) with a .NET SDK installed.

1. Copy the `dotnet-test-runner` folder to your VM.
2. Run `dotnet-test-runner.exe`.
3. The app opens in a Microsoft Edge app window, which has no tabs and no address bar. If Edge and Chrome are not installed, the app opens in your default browser at http://localhost:5000.

To use the browser instead of the app window, set the environment variable `APP_MODE=web` before you start the app. The default, `APP_MODE=native`, uses the app window.

### Updating

When the app starts, it checks the latest GitHub release. If that release has a different version from yours, a banner shows an **Update** button. To check again at any time, click **Check for updates** in the header. The update downloads the new release, replaces the app files and restarts the app. It keeps your run history (`test_runner.db`) and the SDK zip cache. You cannot update while a run is active. If the file swap fails, the app puts the old files back and shows "The last update failed" after it restarts.

While the repository is private, the check needs a token. Before you start the app, set `GITHUB_TOKEN` to a token with read access to the repo. Without a token, the banner does not show. Versions v1.06 and older have no updater, so download one release by hand to get it.

## Selecting a specific SDK install, such as a zip or an exe

By default, all `dotnet` commands use the SDK on the machine's `PATH`. This is usually the SDK from the `.exe` installer, under `C:\Program Files\dotnet`. The **SDK dropdown** at the top of the app lists the installs that `dotnet --list-sdks` reports. It names each install the same way as the Installed apps page in Windows Settings, with the architecture and the install source:

```
11.0.100-preview.7.26366.102 (x64)
10.0.400-preview.0.25322.101 (x64) from Visual Studio
```

The extra detail comes from the Windows installed-apps registry. If an SDK has no registry entry, the dropdown shows only its version number.

### Pinning a zip SDK or any other folder

You can test an SDK that is not installed. For example, a `.zip` SDK uses the file-based workload install type, so you test it this way. Pick **Enter .zip or folder path…** in the SDK dropdown and paste one of these paths:

- the path to an SDK `.zip`, for example `C:\Downloads\dotnet-sdk-11.0.100-win-x64.zip`
- the path to an SDK folder that is already extracted. This is the folder that contains `dotnet.exe`.

The app extracts a zip one time into `sdk_zips/` next to the exe, and uses that copy on every later run. Only the first run waits for the extraction. If you rebuild the zip at the same path, the app extracts it again. The app checks the path at once and shows the SDK version, so you find problems before you start a run.

The whole run then uses that install. The app sets `DOTNET_ROOT`, puts the folder first on `PATH` and turns off multi-level lookup. So `dotnet --info` shows that folder as the **Base Path**.

### Pinning an SDK on a single test

One test can use a different SDK from the rest of the run. Open the test in the editor and set the **SDK folder (optional)** field, or set an `sdk_path` key in a YAML test definition. This field accepts a `.zip` or a folder, the same as the dropdown, and uses the same extraction cache. Leave it blank to use the run's SDK.

Some tests are useful only with a pinned install, for example the zip install workload cases. These tests have `requires_sdk_path: true` in their YAML. The test list shows the same **SDK folder** box under them, so you can pick the folder without the editor. Both boxes write the same `sdk_path` field, so a change in one box shows in the other. A pinned folder stays after a restart. `sdk_path` is different on each machine, so the built-in re-seed at startup does not change it or `workload_version`.

The app picks the SDK for each test in this order:

1. A valid `sdk_path` on the test.
2. The SDK pinned for the run.
3. The SDK on the machine's `PATH`.

If a pinned path is not valid, the run logs a warning and uses the `PATH` SDK. A path is not valid if it does not exist, or if it has no `dotnet.exe` in the folder or one level below. With this order, the "zip install" workload tests can use a zip SDK while the rest of the run uses the default SDK.

## Screenshots (evidence capture)

A test can use a `screenshot` step to save a screenshot as evidence. This works on Windows only. The step opens a window, captures only that window and saves a PNG. It can open one of two targets.

A folder in File Explorer:

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

A Windows Settings page. The step can also type text into the search box of the page to filter it:

```yaml
- type: screenshot
  label: settings-installed-apps
  settings_uri: ms-settings:appsfeatures   # any ms-settings: URI
  search: .net                             # typed into the page's search box (optional)
  delay: 6
```

The step finds the Settings window with UI Automation, not by its title. So it works in any Windows display language. The step closes the Settings window afterwards only if the step opened it.

The app saves screenshots under `screenshots/<run_id>/` next to its database. You can see them in the **Screenshots** tab of the run detail view.

- **Test 1** captures the installed .NET apps in Settings and the `sdk` folders in Explorer (issue #55). These are two more checkpoints in addition to `dotnet --info`. The Explorer screenshot follows `DOTNET_ROOT`, so it shows the SDK folders of the install that the run pinned.
- **Workload Scenario 1** captures the `installertype` folder (issue #47).
- **Workload Scenario 2** captures the `Microsoft.NET.Runtime.MonoAOTCompiler.Task\<version>\Sdk` pack folder before and after the test empties it.

## NuGet feeds

If a required package source is missing, restore fails with `NU1102`. This shows as a failed test, not as a configuration problem. The **NuGet.Config** button in the SDK banner opens the user-level config (`%APPDATA%\NuGet\NuGet.Config`) in Notepad, so you can paste in a full feed configuration (issue #76). If NuGet has not made the file yet, the app makes an empty one first.

The **Feeds…** button next to it opens a panel. The panel does these things:

- It shows the path of the user-level config and the sources in it.
- It marks each required feed as present or missing. A feed in `<disabledPackageSources>` counts as missing, because restore ignores it.
- It adds the missing feeds with one click.
- It lets you add any other feed by name and URL.

Each machine has only one config file, but the feeds a build needs depend on the SDK. So the panel labels each required feed with its SDK. NuGet makes the file only after an SDK runs once, for example `dotnet --info`. If the file is missing, the app tells you, and makes the file when you add a feed. Before any change, the app writes a copy to `NuGet.Config.bak`.

## Saving results so they stay after the VM is gone

Testers usually run this app on a new VM for each .NET build. When the VM is deleted, all files next to the exe are lost. So the app also exports every finished run to a persistent destination. To export a past run again, open its detail view and click **Save to results folder**.

The destination is only a path. A synced OneDrive folder, a UNC share (`\\server\share\results`) and a mounted drive all work with the same setup. To set it, use the `DTR_RESULTS_DIR` environment variable:

```bat
set DTR_RESULTS_DIR=\\server\share\dotnet-test-results
:: ...or a OneDrive folder
set DTR_RESULTS_DIR=%OneDriveCommercial%\dotnet-test-runner
```

The app picks the destination in this order:

| Order | Source | Result |
|-------|--------|--------|
| 1 | `DTR_RESULTS_DIR` | Used as it is. The app expands `%VARS%` and `~`. |
| 2 | `%OneDriveCommercial%`, `%OneDriveConsumer%`, `%OneDrive%` | `<OneDrive>\dotnet-test-runner`, from the first variable that exists |
| 3 | Nothing set | `results/` next to the database. These files are lost when the VM is deleted. |

If the app uses option 3, the UI and the run log show a warning. So the tester sees it before deleting the VM.

The app writes each run to its own folder, so many VMs can share one destination:

```
<root>/<sdk-version>/<machine>-<timestamp>-<run-id>/
    report.md          # the same Markdown report as the export button
    run.json           # the run, its results and every step, which you can merge across VMs
    test_runner.db     # consistent copy of the whole database
    screenshots/*.png
```

The folder name uses the start time of the run. So when you export a run again, the app writes over its old folder and does not make a copy. To skip the database copy, set `DTR_EXPORT_DB=0`.

### The live database goes there too

The exports above are a record. The app keeps its own run history in `test_runner.db`. That file was next to the exe before, so each rebuild of the app deleted the history, and a re-imaged VM lost it. The app now keeps the database under the same persistent root:

```
<root>/history/test_runner.db
<root>/history/screenshots/<run_id>/*.png
```

So the history and the screenshots stay across rebuilds and across VMs. The Dashboard shows where the database is, next to the results destination.

The app picks the database path in this order:

| Order | Source | Result |
|-------|--------|--------|
| 1 | `DTR_DB_PATH` | Used as it is. It is the full path to the `.db` file. |
| 2 | The results root above, when it is persistent | `<root>\history\test_runner.db` |
| 3 | Nothing set | Next to the exe. The next rebuild deletes it. |

The first time the database moves, the app copies the old history and screenshots to the new place. If the app cannot write to the destination, for example an offline share or a read-only folder, it logs a warning and uses the local file. It does not stop.

The extracted zip SDKs in `sdk_zips/` stay next to the exe. They are hundreds of megabytes, so the app does not sync them.

> **One machine at a time.** One shared database keeps the history of all VMs in one place. But two machines must not write to the same OneDrive folder at the same time, because OneDrive then makes conflict copies. If you need to run more than one machine at the same time, give each machine its own `DTR_DB_PATH`.

> **If you use OneDrive sync.** The app writes the files on the local disk, and OneDrive uploads them in the background. Make sure OneDrive has finished the sync before you delete the VM. A network share has no such delay. The write works or shows an error at once.

## Workload scenarios

The **Workloads** category follows the "Test cases for Workloads" plan:

- **Scenario 1** runs `dotnet workload info`, `list` and `search`. It checks that the MSI `installertype` marker is a 0 KB file.
- **Scenario 2** installs `wasm-tools` and `android` with `--skip-manifest-update`. It publishes a Blazor WASM app two times, first `-o 1p0` without AOT, then `-o 2p0` with `<RunAOTCompilation>true</RunAOTCompilation>`, and checks that the AOT output is larger. Then it deletes the contents of the `Sdk` pack folder of `MonoAOTCompiler.Task`, restores it with `dotnet workload repair`, and uninstalls both workloads.
- **Scenario 3** sets the `VerifySignatures` workload policy (`HKLM\SOFTWARE\Policies\Microsoft\dotnet\Workloads`). It checks that `dotnet workload install wasm-tools --skip-sign-check` fails (issue #79). Then it installs the same two workloads without `--skip-manifest-update`, so the manifests update. Then it uninstalls the .NET SDK and checks that `dotnet` no longer runs. The policy stays set. It only stops `--skip-sign-check` from turning off signature checks.

> **Scenario 3 removes the .NET SDK from the machine.** It runs last, so a "run everything" pass does not break the tests after it. Every later test fails until you install the SDK again. Run it on a VM that you can re-image.

Scenarios 2 and 3 install MSI workloads. Scenario 3 also sets a machine policy and runs the SDK uninstall bundle. So start the runner as administrator. Scenario 2 also deletes files under `C:\Program Files\dotnet`. If the runner is not elevated, that step and the policy step of Scenario 3 show a UAC prompt. You must approve it, or the run cannot continue.

## HTTPS development certificate

Test cases **4a–4c** (C# web, mvc and webapi) and **9a–9c** (F# web, mvc and webapi) host HTTPS sites. These sites need a trusted ASP.NET Core development certificate. If the certificate is missing, Windows shows a security dialog during the run, and the run stops until someone clicks it.

So before a run with these tests, the runner checks the certificate with `dotnet dev-certs https --check --trust`. If the certificate is not trusted, a prompt shows three choices:

- **Trust certificate** runs `dotnet dev-certs https --trust` first. Accept the Windows security dialog, and the run starts after it.
- **Run anyway** starts the run without the trust step. The dialog can then stop the run in the middle.
- **Cancel run** runs nothing.

A run without HTTPS tests skips this check.

## Workload sets: selecting the version

Most Workload Set cases use a specific workload set version. They run `dotnet workload update --version <version>` or `dotnet workload install android --version <version>`, or they pin a `workloadVersion` in `global.json`. These versions exist only on the workloads feed, so the runner cannot find one for you. You pick it:

1. Run **Update to latest workload set**, or any test with `dotnet workload search version`, and note a version from the output.
2. Open the test in the editor and fill in **Workload set version**, for example `8.0.400`.
3. Run the test. Each `{workload_version}` in the steps of that test changes to the value you entered.

**Install workload with specific version** does not need steps 1 and 2. Its YAML has `requires_workload_version: true`, so the test list shows a **Workload set version** dropdown under it. The app fills the dropdown from `dotnet workload search version`, for the SDK selected in the header or the SDK folder of the test. The ↻ button loads the list again. **Other…** lets you type a version that is not in the list. The dropdown writes the same field as the editor.

Each test has its own field. So **Update workload sets from different band** can have a version from another feature band, while the other tests have a version from the current band. For example, `8.0.3xx` on an `8.0.4xx` SDK, `9.0.2xx` on `9.0.3xx`, or `10.0.1xx` on `10.0.2xx`.

If a step uses `{workload_version}` and the field is blank, the test fails at once with a message about it. It does not run `--version` with an empty value. The app keeps the versions you enter after a restart, because the YAML re-seed does not change them.

## Continuous test cases

The app runs tests with `continuous: true` in their YAML as one sequence, not one by one. All **Workload Sets** cases are continuous.

- **Shared working directory.** Continuous tests that come one after another use one temp folder. So a later test can use files from an earlier test, for example the `global.json` that the pin test writes. Each test still starts at the root of the folder, so a `cd` in one test does not change the next test.
- **Stops on failure.** If one test fails, the app marks the other tests in the sequence **Skipped** and does not run them on a half-updated machine. The app keeps the shared folder for debugging and logs its path.

A test that is not continuous ends the sequence. The next continuous test starts a new one. The test list shows a ⛓ badge on continuous tests.

## Usage

1. **Select tests** in the left panel. The panel groups tests by category. The **All**, **CLI** and **Sanity Check** tabs above it show only one suite. **Sanity Check** is the quick pass to run first on a new SDK. **CLI** is the full set, without the sanity-only WinForms/WPF publish and F# solution cases.
2. Click **Run Selected** to start the run.
3. Watch the live logs in the runner view. Click a test name to go to its output.
4. Look at the results, with the counts of passed, failed and skipped tests.
5. To add your own test, click **+ Add Test**.

### Test outcomes

Each test reports one of these outcomes:

- **Passed** means that all steps succeeded with no warnings.
- **Passed with warnings** means that all steps succeeded, but the output had an MSBuild or NuGet warning. An example is `warning NU1903:` for a package with a known vulnerability. The app counts these apart from clean passes.
- **Failed** means that a step returned an unexpected exit code or failed an output check.
- **Skipped** means that you cancelled the test before it ran, or an earlier test in its continuous sequence failed.

### Completion notification

When a run finishes, a Windows message box shows the counts of passed, failed, warning and skipped tests. The box stays on top of other windows. A Notepad or browser window from a test often covers the app, so it was hard to see if the run had finished. Windows does not usually let a background process take focus. So the app forces the box to the front. It clears the foreground lock timeout for a short time and attaches to the input queue of the active window. If Windows still refuses, the taskbar button flashes until you open the box. While the result shows, the title of the app window changes to `✅ Run complete`, `❌ Run complete` or `⛔ Run cancelled`.

## Building from source

### Prerequisites (build machine only)

- **Python 3.12** from [python.org](https://python.org)
- **Node.js 18+** from [nodejs.org](https://nodejs.org), only if you change the frontend

### Build steps

```
build.bat
```

The output is the `dist/dotnet-test-runner/` folder, which contains the standalone executable.

Tag the commit first, for example `git tag v1.07`. `build.bat` writes that tag into the exe as its version, and stops if the commit has no tag. The in-app updater compares this version with the tag of the latest release.

### Rebuilding the frontend

Do this only if you change files in `frontend/src/`:

```
cd frontend
npm install
npm run build
```

The build writes static files to `backend/static/`, and `build.bat` puts them into the executable.

## Test case format

The YAML files in `backend/test_definitions/` define the tests. Each test has these fields:

```yaml
tests:
  - id: my-test-id
    category: "C# Console"
    title: "My Test"
    description: "What this tests"
    machine_mutating: false  # true if it modifies global state
    continuous: false        # true to chain with adjacent continuous tests
    requires_sdk_path: false # true to show the SDK folder picker inline in the test list
    requires_workload_version: false # true to show the workload set version dropdown inline
    workload_version: ""     # optional seed for {workload_version}
    suites: [cli]            # Tests tabs it is listed under besides "All": cli and/or sanity (default: [cli])
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

### Step types

| Type | Fields | Description |
|------|--------|-------------|
| `command` | command, timeout, expected_exit_code, assert_output_contains, assert_output_matches, continue_on_error, long_running, run_timeout, screenshot, label | Runs a CLI command |
| `write_file` | path, content, continue_on_error, screenshot, label | Writes content to a file |
| `screenshot` | label, delay, folder / folder_ps / settings_uri + search, continue_on_error | Opens a folder in Explorer or a Settings page and captures that window. See [Screenshots](#screenshots-evidence-capture). |

Each step gets one Step outcome: passed, warning or failed. A `command` step passes when its exit code matches `expected_exit_code` (default `0`) and all `assert_output_*` checks match. This includes `long_running` and `run_timeout` steps. The outcome is warning when the step passes but the output has an MSBuild or NuGet warning, such as `warning NU1903:`. A failed step stops the test, unless the step sets `continue_on_error: true`. A `command` in the form `cd <folder>` changes the working folder for the next steps. It fails if the folder does not exist. A `write_file` step fails if it cannot write the file. A step with an unknown `type` fails.

Three flags capture evidence without a separate `screenshot` step:

- `run_timeout: 10` on a `command` step runs a GUI app that blocks, such as a WinForms or WPF app. The app window stays open for that number of seconds, then the step closes it and passes. Add `screenshot: true` to capture the window before it closes.
- `screenshot: true` on a plain `command` step captures the console after the command finishes. This keeps the command and its output as a checkpoint. The **MAUI Workload** tests use it on `dotnet workload list` before install, after install and after update (issue #77). **Wasm-tools Workload (zip install)** uses it before and after install (issue #78).
- `screenshot: true` on a `write_file` step writes the file, opens it in Notepad and captures the Notepad window.

All three flags work on Windows only. These tests use them: the C# WinForms, WPF, xUnit, NUnit and MSTest tests, the VB versions of these tests, the **Non-native SDK** tests, and the MAUI and wasm-tools workload tests. The optional `label` sets the name of the PNG. By default, the name is the project folder for `run_timeout`, `console` for a plain `command`, or the file name for `write_file`. If one test captures two files with the same name, set `label` on each step.

### Placeholders

The `command` of a step and the `content` of a `write_file` step support these placeholders:

| Placeholder | Expands to | Example |
|-------------|-----------|---------|
| `{tfm}` | Target framework moniker of the SDK that the run uses | `net11.0` |
| `{rid}` | Runtime identifier from `dotnet --info`, or the machine's runtime identifier if `dotnet --info` has none | `win-x64`, `win-arm64` |
| `{assets}` | Path to the bundled read-only `test_assets` folder | |
| `{workload_version}` | The **Workload set version** field of the test. See [Workload sets](#workload-sets-selecting-the-version). | `8.0.400` |

Placeholders also expand in `assert_output_contains` and `assert_output_matches`. So a step can check for the version that it installed.

`assert_output_matches` takes a list of Python regular expressions (regexes). Each regex must match the output after the app removes the ANSI colour codes. Use it when a step expects a non-zero exit code. For example, the failing-suite steps of xUnit, NUnit and MSTest expect exit code 1. But an aborted test run (`TESTRUNABORT`) or a compile error also exits with 1. So these steps also need the framework to report the failed test: `(?im)\bfailed:\s*1\b|^\s*Failed \S+ \[`.

With `{rid}`, the self-contained publish tests (cases **3** and **16**) work on both x64 and ARM64 VMs with no change. `dotnet publish -r {rid} --sc` publishes `win-arm64` on an ARM64 machine and `win-x64` on an x64 machine. Then `cd bin\Release\{tfm}\{rid}\publish` goes to the output folder.

## Adding tests in the UI

1. Click **+ Add Test** in the nav bar.
2. Fill in the category and the title. The app makes the ID from the title.
3. Write the steps as a JSON array.
4. Click save. The test shows in the test list at once.
5. To delete a custom test, click its **Delete** button.

## Architecture

```
dotnet-test-runner/
├── backend/
│   ├── app.py              # Flask API server and static file serving
│   ├── executor.py         # Runs a run: console window, records each Step outcome, SSE
│   ├── steps.py            # Runs one Step and decides its Step outcome (all Step rules)
│   ├── results_export.py   # Exports runs to a persistent place (OneDrive or UNC share)
│   ├── db_store.py         # Picks where the live database and screenshots are kept
│   ├── sdk_source.py       # Extracts zip SDKs and finds the install root
│   ├── nuget_config.py     # Reads NuGet.Config feeds and adds missing ones
│   ├── updater.py          # In-app update: release check, download, file swap
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

## Security note

This app runs commands on the local machine. By default, it listens only on localhost (127.0.0.1). Do not open it to a network.

## Development

For local development with hot-reload:

```
# Terminal 1: backend
cd backend
pip install -r requirements.txt
python app.py

# Terminal 2: frontend (with hot-reload)
cd frontend
npm install
npm run dev
```

The frontend dev server runs on http://localhost:3000 and sends API calls to port 5000.
