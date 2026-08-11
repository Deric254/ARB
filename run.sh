#!/bin/bash
set -e
echo "=========================================="
echo "  SPATIAL ARBITRAGE ENGINE v5.0"
echo "=========================================="
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] python3 not found. Install Python 3.9+"
    exit 1
fi
if ! python3 -c "import ccxt, aiofiles" 2>/dev/null; then
    echo "[INSTALL] Installing dependencies..."
    pip3 install -r requirements.txt
fi
MODE="${1:-demo}"
PORT="${2:-8080}"
URL="http://localhost:$PORT/status"
if [ "$MODE" == "live" ]; then
    if [ -z "$BINANCE_API_KEY" ] || [ -z "$KRAKEN_API_KEY" ]; then
        echo "[ERROR] Set BINANCE_API_KEY and KRAKEN_API_KEY"
        echo "  BINANCE: https://www.binance.com/en/my/settings/api-management"
        echo "  KRAKEN:  https://www.kraken.com/u/security/api"
        exit 1
    fi
    echo "[MODE] LIVE"
    (sleep 4 && xdg-open "$URL" 2>/dev/null || open "$URL" 2>/dev/null || echo "[INFO] Open $URL manually") &
    python3 arbitrage_engine.py --live --port "$PORT"
else
    echo "[MODE] DEMO"
    echo "[INFO] Opening $URL in 4 seconds..."
    (sleep 4 && xdg-open "$URL" 2>/dev/null || open "$URL" 2>/dev/null || echo "[INFO] Open $URL manually") &
    python3 arbitrage_engine.py --duration 120 --port "$PORT"
fi
