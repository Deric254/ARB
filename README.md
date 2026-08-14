# ARB Pro Desktop v6.0

## Professional Spatial Arbitrage Engine

A standalone desktop application for high-frequency cross-exchange cryptocurrency arbitrage. No coding required. No separate installs. Just run the installer and trade.

---

## What You Get After Build

| File | Description |
|------|-------------|
| `ARB Pro Setup.exe` | Windows installer — installs like any professional app |
| `ARB Pro.exe` | Portable executable — runs without installing |

**The installer includes everything:**
- Electron frontend (dark theme, charts, KPIs)
- Bundled Python backend (PyInstaller)
- All Python dependencies (ccxt, FastAPI, etc.)
- All Node.js dependencies
- Your custom logo and branding

**The end user needs NOTHING else.** No Python. No Node. No pip. No npm.

---

## Build Instructions (One Command)

### Prerequisites (Build Machine Only)
- Node.js 18+: https://nodejs.org
- Python 3.9+: https://python.org
- Pillow: `pip install Pillow`

### Build
```bash
cd arb_desktop_pro
npm install
npm run build
```

Wait 5-10 minutes. Done.

**Output:** `dist/ARB Pro Setup.exe`

---

## For End Users (No Build Needed)

1. Download `ARB Pro Setup.exe`
2. Double-click -> Install
3. Launch from desktop
4. Click **Settings** -> paste API keys
5. Select **DEMO Mode** -> click **Start**
6. Watch the dashboard come alive

---

## Features

- **6 Live Charts**: P&L, Latency, Signal Rate, Spreads, Price Divergence, Circuit Breaker
- **5 Interactive Slicers**: Time Range, Min Profit, Exchange Pair, Status, Refresh Rate
- **8 KPI Cards**: Real-time metrics at a glance
- **Trade Log**: Color-coded, filterable, exportable
- **Circuit Breaker**: Auto-halts on loss limits
- **Encrypted API Storage**: Fernet-encrypted SQLite
- **Custom Branding**: Drop logo.png, change name/slogan
- **System Tray**: Minimize to tray, quick access
- **Splash Screen**: Professional loading experience
- **Auto-Update Ready**: electron-updater configured

---

## Trading Modes

| Mode | API Keys | Real Data | Real Trades | Risk |
|------|----------|-----------|-------------|------|
| **DEMO** | Not needed | Simulated | Simulated | Zero |
| **PAPER** | Required | Real | Simulated | Zero |
| **LIVE** | Required | Real | **Real money** | Real |

---

## Security

- API keys encrypted at rest with Fernet
- Keys never transmitted over network
- Only Spot Trading permission required
- IP restriction recommended
- Circuit breaker protects against runaway losses

---

Built for speed. Built for profit. Built to last.
