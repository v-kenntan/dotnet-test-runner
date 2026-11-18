# .NET SDK Test Runner

A Windows app that runs .NET SDK test cases on test VMs and records the results.

## Language

**App folder**:
The folder that holds `dotnet-test-runner.exe`, made when the user extracts the release ZIP. It can also hold user data.
_Avoid_: Install folder, install dir

**Release**:
A tagged GitHub release that has `dotnet-test-runner.zip` attached.
_Avoid_: Build, package

**Updater**:
A small helper that runs after the app exits, replaces the app's files in the App folder with the new Release, and starts the app again.
_Avoid_: Installer, upgrader

**Test case**:
One scenario that a tester runs, such as "WPF app (self-contained publish)". It is an ordered list of Steps.
_Avoid_: Test, test definition

**Step**:
One action in a Test case: run a command, write a file, or take a screenshot. A command that changes the folder (`cd`) is also a Step. Steps run in order. A failed Step stops its Test case, unless the Step allows the Test case to continue.
_Avoid_: Action, task

**Step outcome**:
The result of one Step. It is passed, warning (the Step passed, but dotnet printed a warning such as NU1903), or failed. A Step that the tester cancels has no outcome.
_Avoid_: Step status, step verdict

**Pinned SDK**:
A .NET SDK that the tester chooses for a run or for one test case, by version or by install folder (a folder or an SDK zip). When no SDK is pinned, dotnet uses the machine-wide install.
_Avoid_: Selected SDK, custom SDK

**SDK context**:
The .NET SDK install and version that one test case runs against. It is the test case's Pinned SDK if it has one, else the run's Pinned SDK, else the machine-wide install.
_Avoid_: SDK settings, SDK environment
