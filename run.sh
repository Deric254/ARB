#!/bin/bash
echo "ARB v5.1 — Spatial Arbitrage Engine"
echo "====================================="
if [ "$1" = "--live" ]; then
    echo "[+] LIVE MODE"
    python3 arbitrage_engine.py --live --port 8080
else
    echo "[+] DEMO MODE (120s)"
    python3 arbitrage_engine.py --duration 120 --port 8080
fi
