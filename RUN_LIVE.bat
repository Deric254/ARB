@echo off
chcp 65001 >nul
title ARB v5.1 — Spatial Arbitrage Engine [LIVE MODE]
color 0C
echo.
echo  ╔══════════════════════════════════════════════════════════════════╗
echo  ║         SPATIAL ARBITRAGE ENGINE v5.1 — LIVE MODE               ║
echo  ║         REAL MONEY ON THE LINE — PROCEED WITH CAUTION            ║
echo  ╚══════════════════════════════════════════════════════════════════╝
echo.
python --version >nul 2>&1
if errorlevel 1 (
    echo  [!] Python not found. Install from https://python.org
    pause
    exit /b 1
)

if not exist .env (
    echo  [!] API keys not set. Run SET_KEYS.bat first.
    pause
    exit /b 1
)

for /f "tokens=1,2 delims==" %%a in (.env) do (
    if "%%a"=="BINANCE_API_KEY" set BINANCE_API_KEY=%%b
    if "%%a"=="BINANCE_API_SECRET" set BINANCE_API_SECRET=%%b
    if "%%a"=="KRAKEN_API_KEY" set KRAKEN_API_KEY=%%b
    if "%%a"=="KRAKEN_API_SECRET" set KRAKEN_API_SECRET=%%b
)

echo  [+] API keys loaded
echo  [+] Installing dependencies...
pip install -q ccxt aiofiles
echo  [+] Starting LIVE engine...
echo  [+] Dashboard: http://localhost:8080/status
echo.
start "" "http://localhost:8080/status"
python arbitrage_engine.py --live --port 8080
echo.
pause
