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

echo [1/3] Installing backend dependencies...
pip install -r backend\requirements.txt
if %errorlevel% neq 0 goto :pip_fail

echo.
echo [2/3] Installing PyInstaller...
pip install pyinstaller
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
