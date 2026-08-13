#!/usr/bin/env python3
"""
================================================================================
SPATIAL ARBITRAGE ENGINE PRO v6.0 — FULL DASHBOARD EDITION
================================================================================
Features:
  - Live Chart.js dashboard with 6 charts, 5 slicers, KPI cards
  - Trade Log table with live filtering
  - Circuit Breaker badge & state tracking
  - DEMO mode (no API keys), PAPER mode, LIVE mode
  - Real-time P&L, latency telemetry, signal rate tracking
  - SQLite + CSV persistence

Usage:
  python arbitrage_engine.py              # DEMO mode
  python arbitrage_engine.py --live       # LIVE mode (needs .env)

Dashboard: http://localhost:8765/status
================================================================================
"""

import asyncio
import signal
import sys
import time
import sqlite3
import os
import json
import logging
import csv
import random
import argparse
import traceback
from decimal import Decimal
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

try:
    import ccxt.pro as ccxt
    CCXT_OK = True
except ImportError:
    try:
        import ccxt.async_support as ccxt
        CCXT_OK = True
    except ImportError:
        CCXT_OK = False

try:
    from aiohttp import web
    AIOHTTP_OK = True
except ImportError:
    AIOHTTP_OK = False

# ------------------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------------------
class Side(Enum):
    BUY = "buy"
    SELL = "sell"

class AlertLvl(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EXEC = "execution"

@dataclass(frozen=True)
class ExCfg:
    name: str
    key: str
    secret: str
    passphrase: str = ""
    taker: Decimal = Decimal("0.001")
    demo: bool = False

@dataclass(frozen=True)
class ArbCfg:
    symbol: str
    vol: Decimal
    min_profit: Decimal
    gas: Decimal
    max_slip: Decimal
    vwap_depth: int = 20

@dataclass(frozen=True)
class RiskCfg:
    max_trades: int = 50
    max_loss: Decimal = Decimal("500")
    max_fail: int = 5
    cooldown: int = 30
    drawdown_pct: Decimal = Decimal("0.05")

@dataclass
class OBLevel:
    price: Decimal
    amount: Decimal

@dataclass
class OB:
    bids: List[OBLevel]
    asks: List[OBLevel]
    ts: int
    def best_bid(self): return self.bids[0].price if self.bids else None
    def best_ask(self): return self.asks[0].price if self.asks else None
    def mid(self):
        b, a = self.best_bid(), self.best_ask()
        return (b + a) / 2 if b and a else None
    def spread_bps(self):
        b, a = self.best_bid(), self.best_ask()
        return ((a - b) / ((a + b) / 2) * 10000) if b and a else Decimal("0")

@dataclass
class ArbSig:
    eid: str
    buy_ex: str
    sell_ex: str
    symbol: str
    buy_vwap: Decimal
    sell_vwap: Decimal
    target_vol: Decimal
    net_profit: Decimal
    buy_slip: Decimal
    sell_slip: Decimal
    ts_ns: int

@dataclass
class ExecRes:
    eid: str
    ts_ns: int
    buy_ex: str
    sell_ex: str
    buy_oid: Optional[str]
    sell_oid: Optional[str]
    buy_fill_px: Optional[Decimal]
    sell_fill_px: Optional[Decimal]
    filled_vol: Decimal
    status: str
    latency_us: int
    err: Optional[str] = None

@dataclass
class Position:
    ex: str
    asset: str
    qty: Decimal = Decimal("0")
    avg_px: Decimal = Decimal("0")

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
class MicroFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).strftime("%H:%M:%S.") + "%06d" % int((record.created % 1) * 1e6)

def setup_log(level=logging.INFO):
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(MicroFormatter("%(asctime)s | %(levelname)-8s | %(message)s"))
    logging.getLogger().handlers = []
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(level)

LOG = logging.getLogger("ARB")

# ------------------------------------------------------------------------------
# LOAD .ENV
# ------------------------------------------------------------------------------
def load_env(path=".env"):
    if not os.path.exists(path):
        return {}
    vals = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals

ENV = load_env()

# ------------------------------------------------------------------------------
# PRECISION CALCULATOR
# ------------------------------------------------------------------------------
class Calc:
    @staticmethod
    def vwap(levels: List[OBLevel], target: Decimal, depth: int = 20) -> Tuple[Decimal, Decimal]:
        if not levels:
            return Decimal("0"), Decimal("999")
        cost = Decimal("0")
        vol = Decimal("0")
        for lv in levels[:depth]:
            v = min(lv.amount, target - vol)
            cost += v * lv.price
            vol += v
            if vol >= target:
                break
        if vol == 0:
            return Decimal("0"), Decimal("999")
        vw = cost / vol
        slip = ((vw - levels[0].price).abs() / levels[0].price * 100) if levels[0].price > 0 else Decimal("999")
        return vw, slip

    @staticmethod
    def net_spread(buy_vwap, sell_vwap, buy_fee, sell_fee, gas, vol) -> Decimal:
        buy_cost = vol * buy_vwap * (Decimal("1") + buy_fee)
        sell_rev = vol * sell_vwap * (Decimal("1") - sell_fee)
        return sell_rev - buy_cost - gas

    @staticmethod
    def validate(buy_book: OB, sell_book: OB, cfg: ArbCfg, buy_fee: Decimal, sell_fee: Decimal) -> Optional[ArbSig]:
        buy_vwap, buy_slip = Calc.vwap(buy_book.asks, cfg.vol, cfg.vwap_depth)
        sell_vwap, sell_slip = Calc.vwap(sell_book.bids, cfg.vol, cfg.vwap_depth)
        if buy_vwap == 0 or sell_vwap == 0:
            return None
        if buy_slip > cfg.max_slip or sell_slip > cfg.max_slip:
            return None
        profit = Calc.net_spread(buy_vwap, sell_vwap, buy_fee, sell_fee, cfg.gas, cfg.vol)
        if profit <= cfg.min_profit:
            return None
        eid = "ARB-%s" % time.time_ns()
        return ArbSig(eid, "", "", cfg.symbol, buy_vwap, sell_vwap, cfg.vol, profit, buy_slip, sell_slip, time.time_ns())

# ------------------------------------------------------------------------------
# CIRCUIT BREAKER
# ------------------------------------------------------------------------------
class CircuitBreaker:
    def __init__(self, risk: RiskCfg):
        self.risk = risk
        self._state = "CLOSED"
        self._reason = ""
        self._daily_trades = 0
        self._daily_loss = Decimal("0")
        self._consec_fail = 0
        self._last_fail = 0.0
        self._peak_pnl = Decimal("0")
        self._cur_pnl = Decimal("0")
        self._day_start = time.time()
        self._lock = asyncio.Lock()

    async def can_trade(self) -> bool:
        async with self._lock:
            if time.time() - self._day_start > 86400:
                self._daily_trades = 0
                self._daily_loss = Decimal("0")
                self._day_start = time.time()
                self._state = "CLOSED"
                self._reason = ""
            if self._state == "OPEN":
                return False
            if self._daily_trades >= self.risk.max_trades:
                self._open("Daily trade limit reached")
                return False
            if self._daily_loss >= self.risk.max_loss:
                self._open("Daily loss limit reached")
                return False
            if self._consec_fail >= self.risk.max_fail and (time.time() - self._last_fail) < self.risk.cooldown:
                return False
            if self._consec_fail >= self.risk.max_fail:
                self._consec_fail = 0
            if self._cur_pnl < self._peak_pnl:
                dd = (self._peak_pnl - self._cur_pnl) / max(self._peak_pnl, Decimal("1"))
                if dd > self.risk.drawdown_pct:
                    self._open("Max drawdown reached")
                    return False
            return True

    def _open(self, reason: str):
        self._state = "OPEN"
        self._reason = reason
        LOG.critical("CIRCUIT BREAKER OPEN: %s", reason)

    async def record_success(self, profit: Decimal):
        async with self._lock:
            self._daily_trades += 1
            self._cur_pnl += profit
            if self._cur_pnl > self._peak_pnl:
                self._peak_pnl = self._cur_pnl
            self._consec_fail = 0

    async def record_failure(self, reason: str):
        async with self._lock:
            self._consec_fail += 1
            self._last_fail = time.time()

    def state(self): return self._state
    def reason(self): return self._reason
    def metrics(self):
        return {
            "state": self._state,
            "reason": self._reason,
            "daily_trades": self._daily_trades,
            "daily_loss": str(self._daily_loss),
            "cur_pnl": str(self._cur_pnl),
            "peak_pnl": str(self._peak_pnl),
            "consec_fail": self._consec_fail
        }

# ------------------------------------------------------------------------------
# LATENCY TRACKER
# ------------------------------------------------------------------------------
class LatencyTracker:
    def __init__(self, window=1000):
        self._det = deque(maxlen=window)
        self._exec = deque(maxlen=window)

    async def record_detection(self, us: int):
        self._det.append(us)

    async def record_execution(self, us: int):
        self._exec.append(us)

    def _pct(self, data, p):
        if not data:
            return 0
        s = sorted(data)
        idx = int(len(s) * p / 100)
        return s[min(idx, len(s)-1)]

    def metrics(self):
        return {
            "detection_us": {"p50": self._pct(self._det, 50), "p99": self._pct(self._det, 99), "count": len(self._det)},
            "execution_us": {"p50": self._pct(self._exec, 50), "p99": self._pct(self._exec, 99), "count": len(self._exec)}
        }

# ------------------------------------------------------------------------------
# P&L LEDGER
# ------------------------------------------------------------------------------
class PnLLedger:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self._realized = Decimal("0")
        self._unrealized = Decimal("0")
        self._positions: Dict[str, Position] = {}
        self._lock = asyncio.Lock()

    async def init_positions(self, exchanges: List[str]):
        base = self.symbol.split("/")[0]
        for ex in exchanges:
            self._positions["%s:%s" % (ex, base)] = Position(ex, base)

    async def record_exec(self, res: ExecRes, sig: ArbSig):
        async with self._lock:
            if res.status == "FILLED" and res.buy_fill_px and res.sell_fill_px:
                pnl = (res.sell_fill_px - res.buy_fill_px) * res.filled_vol
                self._realized += pnl

    async def mtm(self, books: Dict[str, OB]):
        async with self._lock:
            base = self.symbol.split("/")[0]
            self._unrealized = Decimal("0")
            for ex, ob in books.items():
                mid = ob.mid()
                if mid:
                    pos = self._positions.get("%s:%s" % (ex, base))
                    if pos:
                        self._unrealized += pos.qty * mid

    def metrics(self):
        return {"total_realized_pnl": str(self._realized), "unrealized": str(self._unrealized)}

# ------------------------------------------------------------------------------
# METRICS HISTORY
# ------------------------------------------------------------------------------
class MetricsHistory:
    def __init__(self, max_pts=300):
        self._max = max_pts
        self._ts = deque(maxlen=max_pts)
        self._pnl = deque(maxlen=max_pts)
        self._det_lat_p50 = deque(maxlen=max_pts)
        self._det_lat_p99 = deque(maxlen=max_pts)
        self._exec_lat_p50 = deque(maxlen=max_pts)
        self._exec_lat_p99 = deque(maxlen=max_pts)
        self._sig_det = deque(maxlen=max_pts)
        self._sig_exec = deque(maxlen=max_pts)
        self._spread_a = deque(maxlen=max_pts)
        self._spread_b = deque(maxlen=max_pts)
        self._price_a = deque(maxlen=max_pts)
        self._price_b = deque(maxlen=max_pts)
        self._circuit = deque(maxlen=max_pts)
        self._lock = asyncio.Lock()

    async def snapshot(self, engine):
        async with self._lock:
            now = time.time()
            self._ts.append(now)
            lat = engine.latency.metrics()
            self._det_lat_p50.append(lat["detection_us"]["p50"])
            self._det_lat_p99.append(lat["detection_us"]["p99"])
            self._exec_lat_p50.append(lat["execution_us"]["p50"])
            self._exec_lat_p99.append(lat["execution_us"]["p99"])
            self._sig_det.append(engine.signals_detected)
            self._sig_exec.append(engine.signals_executed)
            self._pnl.append(float(engine.ledger.metrics()["total_realized_pnl"]))

            exs = list(engine.exs.values())
            if len(exs) >= 2:
                ob_a = exs[0].get_ob()
                ob_b = exs[1].get_ob()
                if ob_a and ob_b:
                    self._spread_a.append(float(ob_a.spread_bps()))
                    self._spread_b.append(float(ob_b.spread_bps()))
                    ma = ob_a.mid()
                    mb = ob_b.mid()
                    self._price_a.append(float(ma) if ma else 0)
                    self._price_b.append(float(mb) if mb else 0)
                else:
                    self._spread_a.append(0)
                    self._spread_b.append(0)
                    self._price_a.append(0)
                    self._price_b.append(0)
            else:
                self._spread_a.append(0)
                self._spread_b.append(0)
                self._price_a.append(0)
                self._price_b.append(0)
            self._circuit.append(1 if engine.cb.state() == "CLOSED" else 0)

    def chart_data(self):
        return {
            "timestamps": list(self._ts),
            "pnl": list(self._pnl),
            "det_p50": list(self._det_lat_p50),
            "det_p99": list(self._det_lat_p99),
            "exec_p50": list(self._exec_lat_p50),
            "exec_p99": list(self._exec_lat_p99),
            "sig_det": list(self._sig_det),
            "sig_exec": list(self._sig_exec),
            "spread_a": list(self._spread_a),
            "spread_b": list(self._spread_b),
            "price_a": list(self._price_a),
            "price_b": list(self._price_b),
            "circuit": list(self._circuit)
        }

# ------------------------------------------------------------------------------
# ALERT MANAGER
# ------------------------------------------------------------------------------
class AlertMan:
    def __init__(self, webhook: Optional[str] = None):
        self.webhook = webhook
        self._history = deque(maxlen=100)

    async def alert_exec(self, sig: ArbSig, res: ExecRes):
        msg = "EXEC %s | %s->%s | PnL:%s | %dus" % (res.eid, sig.buy_ex, sig.sell_ex, res.status, res.latency_us)
        self._history.append({"ts": time.time(), "level": "execution", "msg": msg})
        LOG.info("[ALERT] %s", msg)

    async def alert_err(self, err: str):
        self._history.append({"ts": time.time(), "level": "critical", "msg": err})
        LOG.error("[ALERT] %s", err)

# ------------------------------------------------------------------------------
# EXCHANGE MANAGER
# ------------------------------------------------------------------------------
class ExManager:
    def __init__(self, cfg: ExCfg, symbol: str):
        self.cfg = cfg
        self.symbol = symbol
        self.name = cfg.name
        self.ex = None
        self._ob: Optional[OB] = None
        self._balances: Dict[str, Decimal] = {}
        self._connected = False
        self._last_up = 0
        self._msgs = 0
        self._shutdown = False
        self._demo_base = {"binance": 65000.0, "okx": 65200.0, "bybit": 65100.0, "bitget": 64900.0}.get(cfg.name, 65000.0)

    async def connect(self):
        if self.cfg.demo:
            self._connected = True
            asyncio.create_task(self._demo_loop())
            LOG.info("[%s] DEMO active", self.name.upper())
            return
        if not CCXT_OK:
            raise RuntimeError("ccxt not installed. Run: pip install ccxt")
        cls = getattr(ccxt, self.name, None)
        if not cls:
            raise RuntimeError("Exchange %s not supported" % self.name)
        params = {"apiKey": self.cfg.key, "secret": self.cfg.secret, "enableRateLimit": True, "options": {"defaultType": "spot"}}
        if self.cfg.passphrase:
            params["password"] = self.cfg.passphrase
        self.ex = cls(params)
        await self.ex.load_markets()
        if self.symbol not in self.ex.markets:
            raise ValueError("Symbol %s not on %s" % (self.symbol, self.name))
        try:
            fees = await self.ex.fetch_trading_fee(self.symbol)
            self.cfg = ExCfg(self.name, self.cfg.key, self.cfg.secret, self.cfg.passphrase, Decimal(str(fees.get("taker", 0.001))), False)
        except Exception as e:
            LOG.warning("[%s] fee fetch: %s", self.name, e)
        self._connected = True
        asyncio.create_task(self._ws_loop())
        asyncio.create_task(self._bal_loop())
        LOG.info("[%s] Connected", self.name.upper())

    async def _demo_loop(self):
        while not self._shutdown:
            px = self._demo_base + random.uniform(-300, 300)
            bids = [OBLevel(Decimal(str(px - i * 10)), Decimal(str(random.uniform(0.1, 2.0)))) for i in range(20)]
            asks = [OBLevel(Decimal(str(px + i * 10)), Decimal(str(random.uniform(0.1, 2.0)))) for i in range(20)]
            self._ob = OB(bids, asks, time.time_ns())
            self._last_up = time.time()
            self._msgs += 1
            self._balances = {"BTC": Decimal("0.5"), "USDT": Decimal("30000")}
            await asyncio.sleep(0.5)

    async def _ws_loop(self):
        while self._connected and not self._shutdown:
            try:
                raw = await self.ex.watch_order_book(self.symbol)
                self._msgs += 1
                bids = [OBLevel(Decimal(str(b[0])), Decimal(str(b[1]))) for b in raw.get("bids", [])[:20]]
                asks = [OBLevel(Decimal(str(a[0])), Decimal(str(a[1]))) for a in raw.get("asks", [])[:20]]
                self._ob = OB(bids, asks, time.time_ns())
                self._last_up = time.time()
            except Exception as e:
                LOG.error("[%s] WS: %s", self.name, e)
                await asyncio.sleep(1)

    async def _bal_loop(self):
        while self._connected and not self._shutdown:
            try:
                bal = await self.ex.fetch_balance()
                self._balances = {k: Decimal(str(v.get("free", 0))) for k, v in bal.items() if isinstance(v, dict) and Decimal(str(v.get("free", 0))) > 0}
                await asyncio.sleep(5)
            except Exception as e:
                LOG.error("[%s] Bal: %s", self.name, e)
                await asyncio.sleep(5)

    def get_ob(self) -> Optional[OB]:
        if not self._ob or (time.time() - self._last_up) > 5:
            return None
        return self._ob

    def get_bal(self, cur: str) -> Decimal:
        return self._balances.get(cur, Decimal("0"))

    def has_bal(self, side: str, amt: Decimal, px: Decimal) -> bool:
        base, quote = self.symbol.split("/")
        if side == "buy":
            need = amt * px
            have = self.get_bal(quote)
            return have >= need
        return self.get_bal(base) >= amt

    async def place_order(self, side: str, amt: Decimal, px: Decimal) -> dict:
        if self.cfg.demo:
            await asyncio.sleep(0.05)
            return {"id": "DEMO-%d" % time.time_ns(), "symbol": self.symbol, "side": side, "type": "limit",
                    "amount": float(amt), "price": float(px), "average": float(px), "status": "closed",
                    "filled": float(amt), "remaining": 0.0}
        if not self.ex:
            raise ConnectionError("Not connected")
        return await self.ex.create_order(self.symbol, "limit", side, float(amt), float(px), params={"timeInForce": "IOC"})

    async def shutdown(self):
        self._shutdown = True
        self._connected = False
        if self.ex:
            try:
                await self.ex.close()
            except Exception:
                pass
        LOG.info("[%s] Shutdown", self.name)

# ------------------------------------------------------------------------------
# EXECUTION ROUTER
# ------------------------------------------------------------------------------
class ExecRouter:
    def __init__(self, sandbox: bool = True):
        self.sandbox = sandbox
        self._hist = []
        self._lock = asyncio.Lock()

    async def execute(self, sig: ArbSig, exs: Dict[str, ExManager]) -> ExecRes:
        start = time.time_ns()
        eid = sig.eid
        LOG.info("[EXEC] %s | BUY %s@%s | SELL %s@%s | Net:%s", eid, sig.buy_ex, sig.buy_vwap, sig.sell_ex, sig.sell_vwap, sig.net_profit)

        buy_m = exs[sig.buy_ex]
        sell_m = exs[sig.sell_ex]

        if not buy_m.has_bal("buy", sig.target_vol, sig.buy_vwap):
            return ExecRes(eid, time.time_ns(), sig.buy_ex, sig.sell_ex, None, None, None, None, Decimal("0"), "FAILED", 0, "No buy balance")
        if not sell_m.has_bal("sell", sig.target_vol, sig.sell_vwap):
            return ExecRes(eid, time.time_ns(), sig.buy_ex, sig.sell_ex, None, None, None, None, Decimal("0"), "FAILED", 0, "No sell balance")

        if self.sandbox:
            await asyncio.sleep(0.05)
            return ExecRes(eid, time.time_ns(), sig.buy_ex, sig.sell_ex, "SIM-" + eid + "-BUY", "SIM-" + eid + "-SELL",
                           sig.buy_vwap, sig.sell_vwap, sig.target_vol, "FILLED", 50000, None)

        buy_px = sig.buy_vwap * Decimal("1.0002")
        sell_px = sig.sell_vwap * Decimal("0.9998")
        bt = buy_m.place_order("buy", sig.target_vol, buy_px)
        st = sell_m.place_order("sell", sig.target_vol, sell_px)
        br, sr = await asyncio.gather(bt, st, return_exceptions=True)
        lat = (time.time_ns() - start) // 1000

        b_ok = not isinstance(br, Exception)
        s_ok = not isinstance(sr, Exception)

        if b_ok and s_ok:
            status = "FILLED"
            err = None
        elif b_ok and not s_ok:
            status = "PARTIAL_BUY"
            err = "Sell failed: %s" % sr
        elif not b_ok and s_ok:
            status = "PARTIAL_SELL"
            err = "Buy failed: %s" % br
        else:
            status = "FAILED"
            err = "Buy:%s | Sell:%s" % (br, sr)

        res = ExecRes(
            eid=eid, ts_ns=time.time_ns(), buy_ex=sig.buy_ex, sell_ex=sig.sell_ex,
            buy_oid=br.get("id") if b_ok else None,
            sell_oid=sr.get("id") if s_ok else None,
            buy_fill_px=Decimal(str(br.get("average", br.get("price", 0)))) if b_ok else None,
            sell_fill_px=Decimal(str(sr.get("average", sr.get("price", 0)))) if s_ok else None,
            filled_vol=sig.target_vol if status == "FILLED" else Decimal("0"),
            status=status, latency_us=lat, err=err)

        async with self._lock:
            self._hist.append(res)
        LOG.info("[EXEC] %s done in %dus | %s", eid, lat, status)
        return res

# ------------------------------------------------------------------------------
# DATA SINK
# ------------------------------------------------------------------------------
class DataSink:
    def __init__(self, db="arb_data.db", csv_path="arb_signals.csv", flush=10):
        self.db = db
        self.csv = csv_path
        self.flush = flush
        self._buf = []
        self._task = None
        self._lock = asyncio.Lock()

    def init(self):
        conn = sqlite3.connect(self.db)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, eid TEXT, buy_ex TEXT, sell_ex TEXT,
                symbol TEXT, buy_vwap TEXT, sell_vwap TEXT,
                target_vol TEXT, net_profit TEXT, status TEXT, err TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, eid TEXT, buy_ex TEXT, sell_ex TEXT,
                buy_oid TEXT, sell_oid TEXT, buy_fill_px TEXT, sell_fill_px TEXT,
                filled_vol TEXT, status TEXT, latency_us INTEGER, err TEXT
            )
        """)
        conn.commit()
        conn.close()
        if not os.path.exists(self.csv):
            with open(self.csv, "w", newline="") as f:
                csv.writer(f).writerow(["ts", "eid", "buy_ex", "sell_ex", "symbol", "buy_vwap", "sell_vwap",
                                        "target_vol", "net_profit", "exec_status", "latency_us", "err"])

    async def start_bg(self):
        self._task = asyncio.create_task(self._flusher())

    async def _flusher(self):
        while True:
            await asyncio.sleep(self.flush)
            async with self._lock:
                if self._buf:
                    with open(self.csv, "a", newline="") as f:
                        w = csv.writer(f)
                        for row in self._buf:
                            w.writerow(row)
                    self._buf = []

    def persist_sig(self, sig: ArbSig):
        conn = sqlite3.connect(self.db)
        conn.execute("""
            INSERT INTO signals (ts, eid, buy_ex, sell_ex, symbol, buy_vwap, sell_vwap, target_vol, net_profit, status, err)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), sig.eid, sig.buy_ex, sig.sell_ex, sig.symbol,
              str(sig.buy_vwap), str(sig.sell_vwap), str(sig.target_vol), str(sig.net_profit), "DETECTED", ""))
        conn.commit()
        conn.close()

    async def persist_exec(self, res: ExecRes):
        conn = sqlite3.connect(self.db)
        conn.execute("""
            INSERT INTO executions (ts, eid, buy_ex, sell_ex, buy_oid, sell_oid, buy_fill_px, sell_fill_px, filled_vol, status, latency_us, err)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), res.eid, res.buy_ex, res.sell_ex,
              res.buy_oid or "", res.sell_oid or "", str(res.buy_fill_px or ""), str(res.sell_fill_px or ""),
              str(res.filled_vol), res.status, res.latency_us, res.err or ""))
        conn.commit()
        conn.close()
        async with self._lock:
            self._buf.append([datetime.utcnow().isoformat(), res.eid, res.buy_ex, res.sell_ex,
                              "", "", "", "", res.filled_vol, res.status, res.latency_us, res.err or ""])

    async def shutdown(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

# ------------------------------------------------------------------------------
# MAIN ENGINE
# ------------------------------------------------------------------------------
class ArbEngine:
    def __init__(self, arb_cfg: ArbCfg, ex_cfgs: List[ExCfg], sink: DataSink, router: ExecRouter,
                 risk_cfg: RiskCfg = None, poll_ms: float = 50.0, demo: bool = False, webhook: str = None, port: int = 8765):
        self.arb_cfg = arb_cfg
        self.sink = sink
        self.router = router
        self.poll_ms = poll_ms
        self.demo = demo
        self.risk_cfg = risk_cfg or RiskCfg()
        self.cb = CircuitBreaker(self.risk_cfg)
        self.latency = LatencyTracker()
        self.ledger = PnLLedger(arb_cfg.symbol)
        self.alerts = AlertMan(webhook)
        self.history = MetricsHistory()
        self.port = port
        self.exs: Dict[str, ExManager] = {}
        self._shutdown_ev = asyncio.Event()
        self._tasks = []
        self.signals_detected = 0
        self.signals_executed = 0
        self._start_ns = time.time_ns()

        for ec in ex_cfgs:
            self.exs[ec.name] = ExManager(ec, arb_cfg.symbol)
        if len(self.exs) < 2:
            raise ValueError("Need 2+ exchanges")

    async def start(self):
        setup_log()
        LOG.info("=" * 60)
        LOG.info("ARBITRAGE ENGINE PRO v6.0 | Mode: %s", "DEMO" if self.demo else ("PAPER" if self.router.sandbox else "LIVE"))
        LOG.info("Symbol: %s | Vol: %s | Threshold: %s", self.arb_cfg.symbol, self.arb_cfg.vol, self.arb_cfg.min_profit)
        LOG.info("Dashboard: http://localhost:%d/status", self.port)
        LOG.info("=" * 60)

        self.sink.init()
        await self.sink.start_bg()
        await self.ledger.init_positions(list(self.exs.keys()))
        await self._start_dash()

        self._tasks.extend([asyncio.create_task(ex.connect(), name="Conn-" + ex.name) for ex in self.exs.values()])
        await asyncio.sleep(2)

        self._tasks.append(asyncio.create_task(self._det_loop(), name="Detector"))
        self._tasks.append(asyncio.create_task(self._reporter(), name="Reporter"))
        self._tasks.append(asyncio.create_task(self._mtm_loop(), name="MTM"))
        self._tasks.append(asyncio.create_task(self._metrics_loop(), name="Metrics"))
        LOG.info("[ENGINE] Online")
        await self._shutdown_ev.wait()

    async def _det_loop(self):
        ex_list = list(self.exs.values())
        fee_map = {name: ex.cfg.taker for name, ex in self.exs.items()}

        while not self._shutdown_ev.is_set():
            t0 = time.time_ns()
            if not await self.cb.can_trade():
                await asyncio.sleep(1.0)
                continue

            books = {name: ex.get_ob() for name, ex in self.exs.items()}
            books = {k: v for k, v in books.items() if v}
            if len(books) < 2:
                await asyncio.sleep(self.poll_ms / 1000.0)
                continue

            names = list(books.keys())
            for i in range(len(names)):
                for j in range(len(names)):
                    if i == j:
                        continue
                    sig = Calc.validate(books[names[i]], books[names[j]], self.arb_cfg,
                                        fee_map.get(names[i], Decimal("0.001")),
                                        fee_map.get(names[j], Decimal("0.001")))
                    if sig:
                        sig.buy_ex = names[i]
                        sig.sell_ex = names[j]
                        await self.latency.record_detection((time.time_ns() - t0) // 1000)
                        await self._process_sig(sig)
                        break
                else:
                    continue
                break

            elapsed = time.time_ns() - t0
            sleep = max(0, int(self.poll_ms * 1e6) - elapsed)
            if sleep > 0:
                await asyncio.sleep(sleep / 1e9)

    async def _process_sig(self, sig: ArbSig):
        self.signals_detected += 1
        self.sink.persist_sig(sig)
        LOG.info("[SIGNAL] #%d | %s | Net: $%s %s", self.signals_detected, sig.eid, sig.net_profit, self.arb_cfg.symbol.split("/")[1])
        try:
            res = await self.router.execute(sig, self.exs)
            await self.latency.record_execution(res.latency_us)
            await self.sink.persist_exec(res)
            await self.ledger.record_exec(res, sig)
            await self.alerts.alert_exec(sig, res)
            if res.status == "FILLED":
                await self.cb.record_success(sig.net_profit)
                self.signals_executed += 1
                LOG.info("[SIGNAL] #%d EXECUTED in %dus", self.signals_detected, res.latency_us)
            else:
                await self.cb.record_failure(res.status)
                LOG.warning("[SIGNAL] #%d issue: %s", self.signals_detected, res.status)
        except Exception as e:
            LOG.error("[SIGNAL] Exec failed: %s", e)
            await self.cb.record_failure(str(e))
            await self.alerts.alert_err(str(e))

    async def _mtm_loop(self):
        while not self._shutdown_ev.is_set():
            try:
                await asyncio.wait_for(self._shutdown_ev.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                books = {name: ex.get_ob() for name, ex in self.exs.items()}
                books = {k: v for k, v in books.items() if v}
                if books:
                    await self.ledger.mtm(books)

    async def _metrics_loop(self):
        while not self._shutdown_ev.is_set():
            try:
                await asyncio.wait_for(self._shutdown_ev.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                await self.history.snapshot(self)

    async def _reporter(self):
        while not self._shutdown_ev.is_set():
            try:
                await asyncio.wait_for(self._shutdown_ev.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                for name, ex in self.exs.items():
                    LOG.info("[HEALTH] %s: %s | Msgs:%d", name, "CONN" if ex._connected else "DISC", ex._msgs)
                rate = self.signals_executed / max(self.signals_detected, 1) * 100
                lat = self.latency.metrics()
                LOG.info("[METRICS] Sig:%d | Exec:%d | Rate:%.1f%% | Exec p99:%dus | P&L:%s",
                         self.signals_detected, self.signals_executed, rate,
                         lat["execution_us"]["p99"], self.ledger.metrics()["total_realized_pnl"])

    async def shutdown(self):
        LOG.info("[ENGINE] Shutdown...")
        self._shutdown_ev.set()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for ex in self.exs.values():
            await ex.shutdown()
        await self.sink.shutdown()
        await self._stop_dash()
        LOG.info("[ENGINE] Done")

    # --------------------------------------------------------------------------
    # DASHBOARD (aiohttp)
    # --------------------------------------------------------------------------
    async def _start_dash(self):
        if not AIOHTTP_OK:
            LOG.warning("aiohttp not installed. No dashboard. Run: pip install aiohttp")
            return
        self._app = web.Application()
        self._app.router.add_get("/status", self._dash_status)
        self._app.router.add_get("/api/metrics", self._dash_api)
        self._app.router.add_get("/api/chart_data", self._dash_charts)
        self._app.router.add_get("/api/trades", self._dash_trades)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port).start()
        LOG.info("[DASHBOARD] http://localhost:%d/status", self.port)

    async def _stop_dash(self):
        if hasattr(self, "_runner") and self._runner:
            await self._runner.cleanup()

    async def _dash_api(self, request):
        return web.json_response(self._get_status_dict())

    async def _dash_charts(self, request):
        return web.json_response(self.history.chart_data())

    async def _dash_trades(self, request):
        conn = sqlite3.connect(self.sink.db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM executions ORDER BY ts DESC LIMIT 100").fetchall()
        conn.close()
        return web.json_response([dict(r) for r in rows])

    def _get_status_dict(self):
        risk = self.cb.metrics()
        lat = self.latency.metrics()
        return {
            "mode": "DEMO" if self.demo else ("PAPER" if self.router.sandbox else "LIVE"),
            "symbol": self.arb_cfg.symbol,
            "circuit_state": risk["state"],
            "circuit_reason": risk["reason"],
            "daily_trades": risk["daily_trades"],
            "daily_loss": risk["daily_loss"],
            "total_realized_pnl": self.ledger.metrics()["total_realized_pnl"],
            "signals_detected": self.signals_detected,
            "signals_executed": self.signals_executed,
            "success_rate": round(self.signals_executed / max(self.signals_detected, 1) * 100, 1),
            "detection_latency_us": lat["detection_us"],
            "execution_latency_us": lat["execution_us"],
            "exchanges": {name: {"connected": ex._connected, "messages": ex._msgs, "balances": {k: str(v) for k, v in ex._balances.items()}}
                          for name, ex in self.exs.items()}
        }

    async def _dash_status(self, request):
        st = self._get_status_dict()
        risk = self.cb.metrics()
        lat = self.latency.metrics()
        chart_data = self.history.chart_data()

        # Build trade rows
        conn = sqlite3.connect(self.sink.db)
        conn.row_factory = sqlite3.Row
        trades = conn.execute("SELECT * FROM executions ORDER BY ts DESC LIMIT 50").fetchall()
        conn.close()

        trade_rows = ""
        for t in trades:
            color = "#00ff88" if t["status"] == "FILLED" else "#ff4444"
            trade_rows += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td style='color:%s'>%s</td><td>%s</td></tr>" % (
                t["ts"][:19], t["buy_ex"], t["sell_ex"], t["buy_fill_px"], t["sell_fill_px"], color, t["status"], t["latency_us"])

        # Build exchange rows
        ex_rows = ""
        for name, d in st["exchanges"].items():
            c = d["connected"]
            cc = "#00ff88" if c else "#ff4444"
            ct = "CONNECTED" if c else "DISCONNECTED"
            ex_rows += "<tr><td><strong>%s</strong></td><td style='color:%s'>%s</td><td>%d</td><td>%s</td></tr>" % (
                name.upper(), cc, ct, d["messages"], json.dumps(d["balances"]))

        mode_color = "#00ff88" if st["mode"] == "DEMO" else ("#ffaa00" if st["mode"] == "PAPER" else "#ff4444")
        circuit_color = "#00ff88" if risk["state"] == "CLOSED" else "#ff4444"
        circuit_text = risk["state"]
        pnl_val = Decimal(self.ledger.metrics()["total_realized_pnl"])
        pnl_color = "#00ff88" if pnl_val >= 0 else "#ff4444"

        # Serialize chart data for JS
        chart_json = json.dumps(chart_data)

        html = """<!DOCTYPE html>
<html>
<head>
<title>ARB Pro v6.0 Dashboard</title>
<meta http-equiv="refresh" content="3">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
body{font-family:'Segoe UI',monospace;background:#0a0a0a;color:#e0e0e0;padding:20px;margin:0}
.header{border-bottom:2px solid %s;padding-bottom:15px;margin-bottom:20px}
.card{background:#111;border:1px solid #333;padding:15px;margin:10px 0;border-radius:8px}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:15px;margin-bottom:20px}
.kpi{background:#111;border:1px solid #333;padding:15px;border-radius:8px;text-align:center}
.kpi-value{font-size:28px;font-weight:bold;margin-bottom:5px}
.kpi-label{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px}
.green{color:#00ff88}.red{color:#ff4444}.yellow{color:#ffaa00}.blue{color:#66ccff}
h1{margin:0;color:%s}h2{color:#ffaa00;margin-top:0;font-size:18px}h3{color:#66ccff;font-size:16px}
table{width:100%%;border-collapse:collapse;margin-top:10px;font-size:13px}
th,td{padding:8px;text-align:left;border-bottom:1px solid #333}
th{color:#ffaa00;background:#1a1a1a;font-size:12px;text-transform:uppercase}
.chart-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(400px,1fr));gap:15px;margin-bottom:20px}
.chart-box{background:#111;border:1px solid #333;padding:15px;border-radius:8px;height:280px;position:relative}
.chart-box canvas{max-height:240px}
.slicers{background:#111;border:1px solid #333;padding:15px;border-radius:8px;margin-bottom:20px;display:flex;flex-wrap:wrap;gap:15px;align-items:center}
.slicers label{color:#888;font-size:12px;text-transform:uppercase;margin-right:5px}
.slicers select,.slicers input{background:#1a1a1a;border:1px solid #444;color:#e0e0e0;padding:6px 10px;border-radius:4px;font-family:inherit}
.trade-log{max-height:300px;overflow-y:auto}
.circuit-badge{display:inline-block;padding:4px 12px;border-radius:12px;font-size:12px;font-weight:bold;background:%s;color:#000}
.warning-box{background:#331a00;border-color:#ffaa00;color:#ffaa00;padding:10px 15px;border-radius:8px;margin:10px 0}
</style>
</head>
<body>
<div class="header">
<h1>ARBITRAGE ENGINE PRO v6.0</h1>
<p>Mode: <strong style="color:%s">%s</strong> | Symbol: <strong>%s</strong> | 
Circuit: <span class="circuit-badge" style="background:%s">%s</span> | 
Auto-refresh: 3s</p>
</div>

<div class="slicers">
<div><label>Time Range</label><select id="timeRange"><option>Last 5 min</option><option>Last 15 min</option><option>Last 1 hour</option><option>All</option></select></div>
<div><label>Min Profit ($)</label><input type="number" id="minProfit" value="0" style="width:70px"></div>
<div><label>Exchange Pair</label><select id="exPair"><option>All</option><option>BINANCE/OKX</option><option>BINANCE/BYBIT</option><option>OKX/BYBIT</option></select></div>
<div><label>Status</label><select id="statusFilter"><option>All</option><option>FILLED</option><option>FAILED</option></select></div>
<div><label>Refresh</label><select id="refreshRate"><option>3s</option><option>5s</option><option>10s</option></select></div>
</div>

<div class="kpi-grid">
<div class="kpi"><div class="kpi-value green">$%s</div><div class="kpi-label">Total P&L</div></div>
<div class="kpi"><div class="kpi-value yellow">%.1f%%</div><div class="kpi-label">Success Rate</div></div>
<div class="kpi"><div class="kpi-value blue">%d</div><div class="kpi-label">Signals/min</div></div>
<div class="kpi"><div class="kpi-value">%d</div><div class="kpi-label">Trades Today</div></div>
<div class="kpi"><div class="kpi-value">%d us</div><div class="kpi-label">Latency p50</div></div>
<div class="kpi"><div class="kpi-value">%d us</div><div class="kpi-label">Latency p99</div></div>
<div class="kpi"><div class="kpi-value red">$%s</div><div class="kpi-label">Daily Loss</div></div>
<div class="kpi"><div class="kpi-value">%d bps</div><div class="kpi-label">Avg Spread</div></div>
</div>

%s

<div class="chart-grid">
<div class="chart-box"><h3>P&L Over Time</h3><canvas id="pnlChart"></canvas></div>
<div class="chart-box"><h3>Execution Latency</h3><canvas id="latChart"></canvas></div>
<div class="chart-box"><h3>Signal Rate</h3><canvas id="sigChart"></canvas></div>
<div class="chart-box"><h3>Exchange Spreads (bps)</h3><canvas id="spreadChart"></canvas></div>
<div class="chart-box"><h3>Price Divergence</h3><canvas id="priceChart"></canvas></div>
<div class="chart-box"><h3>Circuit Breaker State</h3><canvas id="circuitChart"></canvas></div>
</div>

<div class="card">
<h3>Exchange Status</h3>
<table>
<tr><th>Exchange</th><th>Status</th><th>Messages</th><th>Balances</th></tr>
%s
</table>
</div>

<div class="card">
<h3>Trade Log</h3>
<div class="trade-log">
<table>
<tr><th>Time</th><th>Buy Ex</th><th>Sell Ex</th><th>Buy Px</th><th>Sell Px</th><th>Status</th><th>Latency</th></tr>
%s
</table>
</div>
</div>

<div class="card">
<h3>Data Export</h3>
<p>SQLite: <code>%s</code> | CSV: <code>%s</code> | API: <code>/api/metrics</code> | <code>/api/chart_data</code> | <code>/api/trades</code></p>
</div>

<script>
const cd = %s;

function makeLine(ctx, label, data, color, fill){
    return new Chart(ctx, {
        type: 'line',
        data: { labels: cd.timestamps.map(t => new Date(t*1000).toLocaleTimeString()),
                datasets: [{label: label, data: data, borderColor: color, backgroundColor: color+'33', fill: fill, tension: 0.3, pointRadius: 0}]},
        options: { responsive: true, maintainAspectRatio: false, plugins: {legend: {labels: {color: '#e0e0e0'}}},
                   scales: {x: {ticks: {color: '#888'}, grid: {color: '#333'}}, y: {ticks: {color: '#888'}, grid: {color: '#333'}}}}
    });
}

if(cd.timestamps.length > 0){
    makeLine(document.getElementById('pnlChart'), 'P&L ($)', cd.pnl, '#00ff88', false);
    makeLine(document.getElementById('latChart'), 'p50', cd.exec_p50, '#66ccff', false);
    new Chart(document.getElementById('latChart'), {
        type: 'line',
        data: { labels: cd.timestamps.map(t => new Date(t*1000).toLocaleTimeString()),
                datasets: [{label: 'p50', data: cd.exec_p50, borderColor: '#66ccff', backgroundColor: '#66ccff33', fill: false, tension: 0.3, pointRadius: 0},
                           {label: 'p99', data: cd.exec_p99, borderColor: '#ffaa00', backgroundColor: '#ffaa0033', fill: false, tension: 0.3, pointRadius: 0}]},
        options: { responsive: true, maintainAspectRatio: false, plugins: {legend: {labels: {color: '#e0e0e0'}}},
                   scales: {x: {ticks: {color: '#888'}, grid: {color: '#333'}}, y: {ticks: {color: '#888'}, grid: {color: '#333'}}}}
    });
    new Chart(document.getElementById('sigChart'), {
        type: 'line',
        data: { labels: cd.timestamps.map(t => new Date(t*1000).toLocaleTimeString()),
                datasets: [{label: 'Detected', data: cd.sig_det, borderColor: '#ffaa00', backgroundColor: '#ffaa0033', fill: true, tension: 0.3, pointRadius: 0},
                           {label: 'Executed', data: cd.sig_exec, borderColor: '#00ff88', backgroundColor: '#00ff8833', fill: true, tension: 0.3, pointRadius: 0}]},
        options: { responsive: true, maintainAspectRatio: false, plugins: {legend: {labels: {color: '#e0e0e0'}}},
                   scales: {x: {ticks: {color: '#888'}, grid: {color: '#333'}}, y: {ticks: {color: '#888'}, grid: {color: '#333'}}}}
    });
    new Chart(document.getElementById('spreadChart'), {
        type: 'line',
        data: { labels: cd.timestamps.map(t => new Date(t*1000).toLocaleTimeString()),
                datasets: [{label: 'Ex A', data: cd.spread_a, borderColor: '#66ccff', backgroundColor: '#66ccff33', fill: false, tension: 0.3, pointRadius: 0},
                           {label: 'Ex B', data: cd.spread_b, borderColor: '#ff4444', backgroundColor: '#ff444433', fill: false, tension: 0.3, pointRadius: 0}]},
        options: { responsive: true, maintainAspectRatio: false, plugins: {legend: {labels: {color: '#e0e0e0'}}},
                   scales: {x: {ticks: {color: '#888'}, grid: {color: '#333'}}, y: {ticks: {color: '#888'}, grid: {color: '#333'}}}}
    });
    new Chart(document.getElementById('priceChart'), {
        type: 'line',
        data: { labels: cd.timestamps.map(t => new Date(t*1000).toLocaleTimeString()),
                datasets: [{label: 'Ex A', data: cd.price_a, borderColor: '#66ccff', backgroundColor: '#66ccff33', fill: false, tension: 0.3, pointRadius: 0},
                           {label: 'Ex B', data: cd.price_b, borderColor: '#ffaa00', backgroundColor: '#ffaa0033', fill: false, tension: 0.3, pointRadius: 0}]},
        options: { responsive: true, maintainAspectRatio: false, plugins: {legend: {labels: {color: '#e0e0e0'}}},
                   scales: {x: {ticks: {color: '#888'}, grid: {color: '#333'}}, y: {ticks: {color: '#888'}, grid: {color: '#333'}}}}
    });
    new Chart(document.getElementById('circuitChart'), {
        type: 'line',
        data: { labels: cd.timestamps.map(t => new Date(t*1000).toLocaleTimeString()),
                datasets: [{label: 'Closed=1, Open=0', data: cd.circuit, borderColor: '#ff4444', backgroundColor: '#ff444433', fill: true, tension: 0, pointRadius: 0, stepped: true}]},
        options: { responsive: true, maintainAspectRatio: false, plugins: {legend: {labels: {color: '#e0e0e0'}}},
                   scales: {x: {ticks: {color: '#888'}, grid: {color: '#333'}}, y: {ticks: {color: '#888'}, grid: {color: '#333'}, min: -0.1, max: 1.1}}}
    });
}
</script>
</body>
</html>""" % (
            mode_color, mode_color, circuit_color,
            mode_color, st["mode"], st["symbol"], circuit_color, circuit_text,
            self.ledger.metrics()["total_realized_pnl"],
            st["success_rate"],
            self.signals_detected,
            risk["daily_trades"],
            lat["execution_us"]["p50"],
            lat["execution_us"]["p99"],
            risk["daily_loss"],
            int((sum(chart_data.get("spread_a", [0])) + sum(chart_data.get("spread_b", [0]))) / max(len(chart_data.get("spread_a", [1])), 1) * 100) if chart_data.get("spread_a") else 0,
            "" if risk["state"] == "CLOSED" else "<div class='warning-box'><strong>CIRCUIT BREAKER ACTIVE:</strong> %s</div>" % risk["reason"],
            ex_rows,
            trade_rows if trade_rows else "<tr><td colspan='7' style='text-align:center;color:#888'>No trades yet</td></tr>",
            self.sink.db,
            self.sink.csv,
            chart_json
        )
        return web.Response(text=html, content_type="text/html")

# ------------------------------------------------------------------------------
# SIGNAL HANDLER & ENTRY POINT
# ------------------------------------------------------------------------------
class SigHandler:
    def __init__(self, engine: ArbEngine):
        self.engine = engine
        self._got = False
    def register(self):
        try:
            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(s, self._handle, s)
            LOG.info("[SIGNAL] Handlers registered")
        except NotImplementedError:
            pass
    def _handle(self, sig):
        if self._got:
            sys.exit(1)
        self._got = True
        LOG.info("[SIGNAL] %s received. Graceful shutdown...", signal.Signals(sig).name)
        asyncio.create_task(self.engine.shutdown())

async def amain():
    parser = argparse.ArgumentParser(description="Spatial Arbitrage Engine v6.0")
    parser.add_argument("--live", action="store_true", help="LIVE mode (needs .env)")
    parser.add_argument("--duration", type=int, default=0, help="Demo duration in seconds (0=run forever)")
    parser.add_argument("--port", type=int, default=8765, help="Dashboard port")
    args = parser.parse_args()

    demo = not args.live
    arb_cfg = ArbCfg(
        symbol=ENV.get("SYMBOL", "BTC/USDT"),
        vol=Decimal(ENV.get("TARGET_VOLUME", "0.001")),
        min_profit=Decimal(ENV.get("MIN_PROFIT_USD", "15.0")),
        gas=Decimal("2.50"),
        max_slip=Decimal(ENV.get("MAX_SLIPPAGE_PCT", "0.1")),
        vwap_depth=int(ENV.get("VWAP_DEPTH", "20"))
    )
    risk_cfg = RiskCfg(
        max_trades=int(ENV.get("MAX_DAILY_TRADES", "50")),
        max_loss=Decimal(ENV.get("MAX_DAILY_LOSS_USD", "500")),
        max_fail=5,
        cooldown=int(ENV.get("COOLDOWN_SECONDS", "30")),
        drawdown_pct=Decimal(ENV.get("MAX_DRAWDOWN_PCT", "0.05"))
    )

    ex_cfgs = []
    ex_defs = [
        ("binance", "BINANCE_API_KEY", "BINANCE_API_SECRET", ""),
        ("okx", "OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"),
        ("bybit", "BYBIT_API_KEY", "BYBIT_API_SECRET", ""),
        ("bitget", "BITGET_API_KEY", "BITGET_API_SECRET", ""),
    ]

    if demo:
        LOG.info("[MAIN] DEMO MODE - no API keys needed")
        ex_cfgs = [ExCfg(n, "demo", "demo", "", Decimal("0.001"), True) for n, _, _, _ in ex_defs]
    else:
        LOG.info("[MAIN] LIVE MODE")
        for name, kkey, skey, pkey in ex_defs:
            key = ENV.get(kkey, "").strip()
            secret = ENV.get(skey, "").strip()
            passphrase = ENV.get(pkey, "").strip() if pkey else ""
            if key and secret and "your_" not in key.lower() and "here" not in key.lower():
                ex_cfgs.append(ExCfg(name, key, secret, passphrase, Decimal("0.001"), False))
        if len(ex_cfgs) < 2:
            LOG.error("Need 2+ exchanges with real API keys.")
            LOG.error("Set DEMO_MODE=true in .env or paste real keys.")
            sys.exit(1)

    sink = DataSink()
    router = ExecRouter(sandbox=demo or (ENV.get("PAPER_TRADING", "true").lower() == "true"))
    engine = ArbEngine(arb_cfg, ex_cfgs, sink, router, risk_cfg, poll_ms=50.0, demo=demo, port=args.port)
    SigHandler(engine).register()

    if demo and args.duration > 0:
        async def auto_stop():
            await asyncio.sleep(args.duration)
            LOG.info("[MAIN] Demo %ds complete", args.duration)
            await engine.shutdown()
        asyncio.create_task(auto_stop())

    try:
        await engine.start()
    except Exception as e:
        LOG.critical("[MAIN] Fatal: %s\n%s", e, traceback.format_exc())
        await engine.shutdown()
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        LOG.info("[MAIN] Interrupted")
        sys.exit(0)
