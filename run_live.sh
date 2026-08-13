#!/bin/bash
set -e
echo "================================================================"
echo "  SPATIAL ARBITRAGE ENGINE PRO v6.0"
echo "================================================================"
if [ ! -f .env ]; then
    echo "[ERROR] .env not found! Copy .env.example to .env"
    exit 1
fi
python3 --version > /dev/null 2>&1 || { echo "Python 3 not found"; exit 1; }
echo "[+] Installing dependencies..."
pip3 install -q -r requirements.txt
echo "[+] Starting engine..."
echo "[+] Dashboard: http://localhost:8765/status"
echo "[+] Press Ctrl+C to stop"
echo ""
python3 arbitrage_engine.py
echo "[+] Engine stopped."
