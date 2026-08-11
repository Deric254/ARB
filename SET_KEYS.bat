@echo off
title ARB v5.1 — Set API Keys
color 0B
echo.
echo  ╔══════════════════════════════════════════════════════════════════╗
echo  ║              CONFIGURE EXCHANGE API KEYS                          ║
echo  ╚══════════════════════════════════════════════════════════════════╝
echo.
echo  Get your keys from:
echo    Binance: https://www.binance.com/en/my/settings/api-management
echo    Kraken:  https://www.kraken.com/u/security/api
echo.
echo  IMPORTANT: Enable SPOT TRADING only. NEVER enable withdrawals.
echo.
set /p B_KEY="Binance API Key    : "
set /p B_SEC="Binance API Secret : "
set /p K_KEY="Kraken API Key     : "
set /p K_SEC="Kraken API Secret  : "
(
echo BINANCE_API_KEY=%B_KEY%
echo BINANCE_API_SECRET=%B_SEC%
echo KRAKEN_API_KEY=%K_KEY%
echo KRAKEN_API_SECRET=%K_SEC%
) > .env
echo.
echo  [+] API keys saved to .env
echo  [+] Run RUN_LIVE.bat to start live trading.
pause
