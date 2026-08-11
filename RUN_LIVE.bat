@echo off
title Spatial Arbitrage Engine v4.0 - LIVE

echo ==========================================
echo   SPATIAL ARBITRAGE ENGINE v4.0
echo   LIVE MODE - REAL CAPITAL AT RISK
echo ==========================================
echo.
if "%BINANCE_API_KEY%"=="" goto MISSING
if "%BINANCE_API_SECRET%"=="" goto MISSING
if "%KRAKEN_API_KEY%"=="" goto MISSING
if "%KRAKEN_API_SECRET%"=="" goto MISSING
goto FOUND

:MISSING
echo ERROR: API keys not set!
echo.
echo Where to get API keys:
echo   BINANCE: https://www.binance.com/en/my/settings/api-management
echo   KRAKEN:  https://www.kraken.com/u/security/api
echo.
echo Set them BEFORE running this file:
echo   set BINANCE_API_KEY=your_key_here
echo   set BINANCE_API_SECRET=your_secret_here
echo   set KRAKEN_API_KEY=your_key_here
echo   set KRAKEN_API_SECRET=your_secret_here
echo.
echo Or run SET_KEYS.bat first, then run this file.
echo.
pause
exit /b 1

:FOUND
echo [OK] API keys detected.

set PYTHON_CMD=python
python --version >nul 2>&1
if errorlevel 1 (
    set PYTHON_CMD=py
    py --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found.
        pause
        exit /b 1
    )
)

%PYTHON_CMD% -c "import ccxt, aiofiles" >nul 2>&1
if errorlevel 1 %PYTHON_CMD% -m pip install -r requirements.txt

echo [OK] Starting LIVE mode...
echo [INFO] Dashboard: http://localhost:8080/status
echo [WARN] Sandbox safety is ON by default.
echo.
%PYTHON_CMD% arbitrage_engine.py --live --port 8080

echo.
pause
