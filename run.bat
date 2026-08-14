@echo off
setlocal enabledelayedexpansion
title ARB Pro - Dev Run
cd /d "%~dp0"

echo ========================================
echo   ARB Pro Desktop - Dev Run
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
echo.

REM --- Install Node dependencies if missing ---
if not exist "node_modules" (
    echo [Step 1/3] Installing Node dependencies ^(first run only^)...
    call npm install
    if errorlevel 1 (
        echo [ERROR] npm install failed.
        pause
        exit /b 1
    )
) else (
    echo [Step 1/3] Node dependencies already installed. Skipping.
)
echo.

REM --- Install Python backend dependencies if missing ---
echo [Step 2/3] Checking Python backend dependencies...
%PYTHON_CMD% -c "import fastapi, uvicorn, ccxt, cryptography" >nul 2>nul
if errorlevel 1 (
    echo     Installing Python dependencies...
    %PYTHON_CMD% -m pip install -r backend\requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install Python dependencies.
        pause
        exit /b 1
    )
) else (
    echo     Python dependencies already satisfied. Skipping.
)
echo.

REM --- Launch app in dev mode (Electron spawns backend\main.py via system Python) ---
echo [Step 3/3] Launching ARB Pro ^(dev mode^)...
echo     Backend: Python ^(backend\main.py^)
echo     Frontend: Electron
echo.
call npm start

endlocal
