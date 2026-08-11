# Spatial Arbitrage Engine v4.0

## WINDOWS - Quick Start

### DEMO (No API keys)
1. Extract ZIP to a folder
2. Double-click **RUN_DEMO.bat**
3. Your browser will **open automatically** to the dashboard

### LIVE (Real exchanges)
1. Get API keys from Binance and Kraken
2. Double-click **SET_KEYS.bat**, paste your keys, save
3. Double-click **RUN_LIVE.bat**

## Dashboard

While the engine runs, your browser opens to:
- **http://localhost:8080/status** — Live HTML dashboard with P&L, latency, circuit breaker status
- **http://localhost:8080/metrics** — JSON telemetry
- **http://localhost:8080/health** — Liveness probe

If the browser doesn't open automatically, manually navigate to `http://localhost:8080/status`

## Where to Get API Keys

| Exchange | URL |
|----------|-----|
| Binance  | https://www.binance.com/en/my/settings/api-management |
| Kraken   | https://www.kraken.com/u/security/api |

Enable: **Spot Trading** only (never withdrawals)

## Output Files

Created in the same folder as the engine:
- `arbitrage_signals.csv` — Trade history for Excel/Tableau/Power BI
- `arbitrage_engine.db` — SQLite database with full records

## Requirements

- Python 3.9+ from https://python.org
- Check **"Add Python to PATH"** during install
