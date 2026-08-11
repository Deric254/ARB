@echo off
title Spatial Arbitrage Engine v4.0 - DEMO

echo ==========================================
echo   SPATIAL ARBITRAGE ENGINE v4.0
echo   DEMO MODE - No API keys needed
echo ==========================================
echo.

REM Find Python
set PYTHON_CMD=python
python --version >nul 2>&1
if errorlevel 1 (
    set PYTHON_CMD=py
    py --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found. Install from https://python.org
        echo Make sure to check 'Add Python to PATH' during install.
        pause
        exit /b 1
    )
)

echo [OK] Python found.
echo.

REM Install deps if missing
%PYTHON_CMD% -c "import ccxt, aiofiles" >nul 2>&1
if errorlevel 1 (
    echo [INSTALL] Installing ccxt and aiofiles...
    %PYTHON_CMD% -m pip install -r requirements.txt
)

echo [OK] Dependencies ready.
echo.
echo [INFO] Starting engine for 120 seconds...
echo [INFO] Dashboard will open automatically in 3 seconds.
echo [INFO] Press Ctrl+C to stop early.
echo.

REM Auto-open browser after 3 seconds (background task)
start /B "" %PYTHON_CMD% -c "import time, os; time.sleep(3); os.system('start http://localhost:8080/status')"

REM Run the engine (blocks until done)
%PYTHON_CMD% arbitrage_engine.py --duration 120 --port 8080

echo.
echo [INFO] Engine stopped.
echo [INFO] Output files in this folder:
echo        - arbitrage_signals.csv
echo        - arbitrage_engine.db
echo.
pause
