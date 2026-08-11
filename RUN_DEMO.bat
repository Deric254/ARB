@echo off
title Spatial Arbitrage Engine v4.0 - DEMO

echo ==========================================
echo   SPATIAL ARBITRAGE ENGINE v4.0
echo   DEMO MODE - No API keys needed
echo ==========================================
echo.
set PYTHON_CMD=python
python --version >nul 2>&1
if errorlevel 1 (
    set PYTHON_CMD=py
    py --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found.
        echo Install from https://python.org
        pause
        exit /b 1
    )
)

echo [OK] Python found.
echo.
echo [CHECK] Checking dependencies...
%PYTHON_CMD% -c "import ccxt, aiofiles" >nul 2>&1
if errorlevel 1 (
    echo [INSTALL] Installing ccxt and aiofiles...
    %PYTHON_CMD% -m pip install -r requirements.txt
)

echo [OK] Dependencies ready.
echo.
echo [INFO] Starting DEMO mode for 120 seconds...
echo [INFO] Dashboard: http://localhost:8080/status
echo [INFO] Press Ctrl+C to stop
echo.
%PYTHON_CMD% arbitrage_engine.py --duration 120 --port 8080

echo.
echo [INFO] Engine stopped.
echo [INFO] Check these files in this folder:
echo        - arbitrage_signals.csv
echo        - arbitrage_engine.db
echo.
pause
