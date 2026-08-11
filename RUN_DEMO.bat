@echo off
chcp 65001 >nul
title ARB v5.1 — Spatial Arbitrage Engine [DEMO MODE]
color 0A
echo.
echo  ╔══════════════════════════════════════════════════════════════════╗
echo  ║         SPATIAL ARBITRAGE ENGINE v5.1 — DEMO MODE               ║
echo  ║         Zero-Gap Architecture ^| Slicer Dashboard                ║
echo  ╚══════════════════════════════════════════════════════════════════╝
echo.
echo  [+] Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo  [!] Python not found. Install from https://python.org
    pause
    exit /b 1
)
echo  [+] Python OK
echo  [+] Installing dependencies...
pip install -q ccxt aiofiles
echo  [+] Dependencies ready
echo  [+] Starting DEMO engine (120 seconds)...
echo  [+] Dashboard will open at http://localhost:8080/status
echo.
start "" "http://localhost:8080/status"
python arbitrage_engine.py --duration 120 --port 8080
echo.
echo  [+] Demo complete. Press any key to exit.
pause >nul
