@echo off
title ARBITRAGE ENGINE PRO v6.0
color 0A
cls
echo ================================================================
echo   SPATIAL ARBITRAGE ENGINE PRO v6.0
echo ================================================================
echo.
if not exist .env (
    echo [ERROR] .env not found!
    echo Copy .env.example to .env and edit it.
    pause
    exit /b 1
)
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org
    pause
    exit /b 1
)
echo [+] Installing dependencies...
pip install -q -r requirements.txt
echo [+] Starting engine...
echo [+] Dashboard: http://localhost:8765/status
echo [+] Press Ctrl+C to stop
echo.
start "" "http://localhost:8765/status"
python arbitrage_engine.py
echo.
echo [+] Engine stopped.
pause
