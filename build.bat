@echo off
title .NET SDK Test Runner - Build
cd /d "%~dp0"

echo ============================================
echo  Building .NET SDK Test Runner Executable
echo ============================================
echo.

:: Check that frontend is pre-built
if not exist "backend\static\index.html" goto :no_frontend

:: Check Python
python --version >nul 2>nul
if %errorlevel% neq 0 goto :no_python

:: The in-app update compares this tag with the latest GitHub Release (issue #89).
git describe --tags --exact-match > backend\version.txt 2>nul
if %errorlevel% neq 0 goto :no_tag

echo [1/3] Installing backend dependencies...
pip install -r backend\requirements.txt
if %errorlevel% neq 0 goto :pip_fail

echo.
echo [2/3] Installing PyInstaller...
:: 6.22+ is needed for Python 3.14's Tcl/Tk 9 (library embedded as zipfs);
:: older versions build an exe that dies on "Tcl data directory ... not found".
pip install --upgrade "pyinstaller>=6.22"
if %errorlevel% neq 0 goto :pip_fail

echo.
echo [3/3] Bundling into standalone executable...
python -m PyInstaller build.spec --distpath dist --workpath build_temp --clean --noconfirm
if %errorlevel% neq 0 goto :build_fail

:: Cleanup
rd /s /q build_temp 2>nul

echo.
echo ============================================
echo  Build complete!
echo  Output: dist\dotnet-test-runner\dotnet-test-runner.exe
echo ============================================
echo.
echo Copy the entire dist\dotnet-test-runner\ folder to your target VM and run dotnet-test-runner.exe
echo The application window will open automatically.
echo.
pause
exit /b 0

:no_frontend
echo ERROR: Frontend not built.
echo Run "npm run build" in frontend\ first.
pause
exit /b 1

:no_python
echo ERROR: Python is required. Install from https://python.org
pause
exit /b 1

:no_tag
del backend\version.txt 2>nul
echo ERROR: HEAD has no git tag. Tag the release first, e.g. "git tag v1.07".
pause
exit /b 1

:pip_fail
echo.
echo ERROR: pip install failed
pause
exit /b 1

:build_fail
echo.
echo ERROR: PyInstaller build failed
pause
exit /b 1
