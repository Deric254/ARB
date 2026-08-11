@echo off
title Spatial Arbitrage Engine v5.0 - DEMO

echo ==========================================
echo   SPATIAL ARBITRAGE ENGINE v5.0
echo   DEMO MODE - No API keys needed
echo ==========================================
echo.
set PYTHON_CMD=python
python --version >nul 2>&1
if errorlevel 1 (
    set PYTHON_CMD=py
    py --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found. Install from https://python.org
        pause
        exit /b 1
    )
)

echo [OK] Python found.
echo.
%PYTHON_CMD% -c "import ccxt, aiofiles" >nul 2>&1
if errorlevel 1 (
    echo [INSTALL] Installing dependencies...
    %PYTHON_CMD% -m pip install -r requirements.txt
)

echo [OK] Dependencies ready.
echo.
echo [INFO] Starting engine for 120 seconds...
echo [INFO] Browser will open automatically in 4 seconds.
echo [INFO] Press Ctrl+C to stop early.
echo.

REM Auto-open browser after 4 seconds
start "" /B %PYTHON_CMD% -c "import time,os;time.sleep(4);os.system('start http://localhost:8080/status')"

%PYTHON_CMD% arbitrage_engine.py --duration 120 --port 8080

echo.
echo [INFO] Engine stopped.
echo [INFO] Output files in this folder:
echo        - arbitrage_signals.csv
echo        - arbitrage_engine.db
echo.
pause
