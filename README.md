# Spatial Arbitrage Engine v5.0

## WINDOWS - Quick Start

### DEMO (No API keys, auto-opens browser)
1. Extract ZIP to a folder
2. Double-click **RUN_DEMO.bat**
3. Your browser opens automatically to the live dashboard

### LIVE (Real exchanges)
1. Get API keys from Binance and Kraken
2. Double-click **SET_KEYS.bat**, paste your keys, save
3. Double-click **RUN_LIVE.bat**

## Dashboard

Auto-opens at: **http://localhost:8080/status**

Features:
- **6 live charts** auto-refreshing every 2 seconds
- **KPI cards**: P&L, Success Rate, Latency, Signals/min, Trades, Spread
- **P&L Over Time** — cumulative profit line chart
- **Execution Latency** — p50 and p99 trend lines
- **Signal Rate** — detected vs executed over time
- **Exchange Spreads** — Binance vs Kraken bid-ask spread (bps)
- **Price Divergence** — mid-price of both exchanges
- **Circuit Breaker State** — closed/open status over time

## Where to Get API Keys

| Exchange | URL |
|----------|-----|
| Binance  | https://www.binance.com/en/my/settings/api-management |
| Kraken   | https://www.kraken.com/u/security/api |

Enable: **Spot Trading** only (never withdrawals)

## Output Files

- `arbitrage_signals.csv` — Trade history for Excel/Tableau
- `arbitrage_engine.db` — SQLite database

## Requirements

- Python 3.9+ from https://python.org
- Check **"Add Python to PATH"** during install
