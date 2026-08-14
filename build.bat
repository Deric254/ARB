@echo off
setlocal enabledelayedexpansion
title ARB Pro - Build
cd /d "%~dp0"

echo ========================================
echo   ARB Pro Desktop - Production Build
echo ========================================
echo.

REM --- Check Node.js ---
where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js not found. Install it from https://nodejs.org
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('node --version') do echo [OK] Node.js %%v found

REM --- Check Python ---
set PYTHON_CMD=
where python >nul 2>nul
if not errorlevel 1 (
    set PYTHON_CMD=python
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set PYTHON_CMD=py
    )
)
if "%PYTHON_CMD%"=="" (
    echo [ERROR] Python not found. Install Python 3.9+ from https://python.org
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('%PYTHON_CMD% --version') do echo [OK] Python %%v found

REM --- Check Pillow (needed for icon generation from logo.png) ---
%PYTHON_CMD% -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo     Pillow not found, installing...
    %PYTHON_CMD% -m pip install Pillow
)
echo.

if not exist "logo.png" (
    echo [!] No logo.png found in project root. Build will use the default Electron icon.
    echo     Drop a logo.png here and re-run to brand the installer.
    echo.
)

echo Starting full build ^(this can take 5-10 minutes^)...
echo   1. Generate icons from logo.png
echo   2. Install Node dependencies
echo   3. Bundle Python backend with PyInstaller
echo   4. Package with electron-builder
echo.

call npm run build
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See output above for details.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   BUILD COMPLETE
echo ========================================
echo   Installer: dist\ARB Pro Setup.exe
echo   Portable:  dist\win-unpacked\ARB Pro.exe
echo ========================================
pause

endlocal
