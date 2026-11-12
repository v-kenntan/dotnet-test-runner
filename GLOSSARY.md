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
