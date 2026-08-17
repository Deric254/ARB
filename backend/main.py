#!/usr/bin/env python3
"""
ARB Pro Backend v6.0
Bundled with PyInstaller for desktop deployment.
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
import traceback
from decimal import Decimal
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from collections import deque

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
import uvicorn

try:
    import ccxt.pro as ccxt
    CCXT_OK = True
except ImportError:
    try:
        import ccxt.async_support as ccxt
        CCXT_OK = True
    except ImportError:
        CCXT_OK = False

from config_manager import ConfigManager

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-8s | %(message)s')
LOG = logging.getLogger("ARB-BACKEND")

# Detect if running from PyInstaller bundle
BUNDLE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

# ------------------------------------------------------------------
# DATA STRUCTURES
# ------------------------------------------------------------------
@dataclass
class OBLevel:
    price: Decimal
    amount: Decimal

@dataclass
class OrderBook:
    bids: List[OBLevel]
    asks: List[OBLevel]
    ts: int
    def best_bid(self): return self.bids[0].price if self.bids else None
    def best_ask(self): return self.asks[0].price if self.asks else None
    def mid(self):
        b, a = self.best_bid(), self.best_ask()
        return (b + a) / 2 if b and a else None

@dataclass
class ArbSig:
    eid: str
    buy_ex: str
    sell_ex: str
    symbol: str
    buy_vwap: Decimal
    sell_vwap: Decimal
    volume: Decimal
    profit: Decimal
    buy_slip: Decimal
    sell_slip: Decimal

@dataclass
class ExecRes:
    ok: bool
    buy_fill: Decimal
    sell_fill: Decimal
    buy_px: Decimal
    sell_px: Decimal
    pnl: Decimal
    latency_ms: int
    error: str = ""

# ------------------------------------------------------------------
# CALCULATOR
# ------------------------------------------------------------------
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
        slip = (abs(vw - levels[0].price) / levels[0].price * 100) if levels[0].price > 0 else Decimal("999")
        return vw, slip

    @staticmethod
    def validate(buy_book: OrderBook, sell_book: OrderBook, vol: Decimal, min_profit: Decimal, max_slip: Decimal,
                 buy_fee: Decimal, sell_fee: Decimal, gas: Decimal, symbol: str = "BTC/USDT", depth: int = 20,
                 exec_buffer_pct: Decimal = Decimal("0.0002")) -> Optional[ArbSig]:
        buy_vwap, buy_slip = Calc.vwap(buy_book.asks, vol, depth)
        sell_vwap, sell_slip = Calc.vwap(sell_book.bids, vol, depth)
        if buy_vwap == 0 or sell_vwap == 0:
            return None
        if buy_slip > max_slip or sell_slip > max_slip:
            return None
        # Actual order placement pads the limit price by exec_buffer_pct on
        # each leg to improve fill odds (see buy_px/sell_px in _process_sig).
        # That's real worst-case cost that must be projected here too —
        # otherwise a trade can look profitable on paper and still lose
        # purely from its own execution buffer, with zero adverse market move.
        worst_buy_px = buy_vwap * (Decimal("1") + exec_buffer_pct)
        worst_sell_px = sell_vwap * (Decimal("1") - exec_buffer_pct)
        buy_cost = vol * worst_buy_px * (Decimal("1") + buy_fee)
        sell_rev = vol * worst_sell_px * (Decimal("1") - sell_fee)
        profit = sell_rev - buy_cost - gas
        if profit <= min_profit:
            return None
        return ArbSig("ARB-%d" % time.time_ns(), "", "", symbol, buy_vwap, sell_vwap, vol, profit, buy_slip, sell_slip)

# ------------------------------------------------------------------
# EXCHANGE MANAGER
# ------------------------------------------------------------------
class ExMan:
    def __init__(self, name: str, symbol: str, key: str, secret: str, passphrase: str = "", demo: bool = False):
        self.name = name
        self.symbol = symbol
        self.key = key
        self.secret = secret
        self.passphrase = passphrase
        self.demo = demo
        self.ex = None
        self._ob: Optional[OrderBook] = None
        self._balances: Dict[str, Decimal] = {}
        self.connected = False
        self.last_up = 0
        self.msgs = 0
        self.taker_fee = Decimal("0.001")
        self._shutdown = False
        self._demo_base = {"binance": 65000.0, "okx": 65200.0, "bybit": 65100.0, "bitget": 64900.0, "mexc": 65050.0}.get(name, 65000.0)
        # Portfolio-wide USDT valuation — separate from trading balances above.
        # "How much money do I actually have here, in USDT terms" regardless
        # of what asset it's currently sitting in.
        self.usdt_value = Decimal("0")
        self._last_valuation = 0.0

    async def connect(self):
        if self.demo:
            self.connected = True
            asyncio.create_task(self._demo_loop())
            LOG.info("[%s] DEMO active", self.name.upper())
            return
        if not CCXT_OK:
            raise RuntimeError("ccxt not installed")
        cls = getattr(ccxt, self.name, None)
        if not cls:
            raise RuntimeError("Exchange %s not supported" % self.name)
        params = {"apiKey": self.key, "secret": self.secret, "enableRateLimit": True, "options": {"defaultType": "spot"}}
        if self.passphrase:
            params["password"] = self.passphrase
        self.ex = cls(params)
        await self.ex.load_markets()
        if self.symbol not in self.ex.markets:
            raise ValueError("Symbol %s not on %s" % (self.symbol, self.name))
        try:
            fees = await self.ex.fetch_trading_fee(self.symbol)
            self.taker_fee = Decimal(str(fees.get("taker", 0.001)))
        except Exception as e:
            LOG.warning("[%s] fee fetch: %s", self.name, e)
        self.connected = True
        asyncio.create_task(self._ws_loop())
        asyncio.create_task(self._bal_loop())
        LOG.info("[%s] Connected", self.name.upper())

    async def _demo_loop(self):
        while not self._shutdown:
            px = self._demo_base + random.uniform(-300, 300)
            bids = [OBLevel(Decimal(str(px - i * 10)), Decimal(str(random.uniform(0.1, 2.0)))) for i in range(20)]
            asks = [OBLevel(Decimal(str(px + i * 10)), Decimal(str(random.uniform(0.1, 2.0)))) for i in range(20)]
            self._ob = OrderBook(bids, asks, time.time_ns())
            self.last_up = time.time()
            self.msgs += 1
            self._balances = {"BTC": Decimal("0.5"), "USDT": Decimal("30000")}
            self.usdt_value = self._balances["USDT"] + self._balances["BTC"] * Decimal(str(px))
            await asyncio.sleep(0.5)

    async def _ws_loop(self):
        # Not every exchange ccxt supports offers WebSocket order book streaming
        # (MEXC doesn't in this build, for example). Fall back to REST polling
        # for those rather than silently never populating an order book.
        use_ws = bool(self.ex.has.get('watchOrderBook'))
        if not use_ws:
            LOG.info("[%s] No WebSocket order book support — using REST polling instead", self.name)
        while self.connected and not self._shutdown:
            try:
                if use_ws:
                    raw = await self.ex.watch_order_book(self.symbol)
                else:
                    raw = await self.ex.fetch_order_book(self.symbol)
                self.msgs += 1
                bids = [OBLevel(Decimal(str(b[0])), Decimal(str(b[1]))) for b in raw.get("bids", [])[:20]]
                asks = [OBLevel(Decimal(str(a[0])), Decimal(str(a[1]))) for a in raw.get("asks", [])[:20]]
                self._ob = OrderBook(bids, asks, time.time_ns())
                self.last_up = time.time()
                if not use_ws:
                    await asyncio.sleep(max(0.2, self.ex.rateLimit / 1000))
            except Exception as e:
                LOG.error("[%s] Order book fetch: %s", self.name, e)
                await asyncio.sleep(1)

    async def _bal_loop(self):
        cycle = 0
        while self.connected and not self._shutdown:
            try:
                bal = await self.ex.fetch_balance()
                self._balances = {k: Decimal(str(v.get("free", 0))) for k, v in bal.items() if isinstance(v, dict) and Decimal(str(v.get("free", 0))) > 0}
                # Full portfolio value (free + locked-in-orders) refreshed less
                # often than trading balances — it's for visibility/budget
                # awareness, not for gating trades, so it doesn't need to be
                # as fresh and shouldn't burn extra API calls every 5s.
                if cycle % 6 == 0:
                    total_bal = {k: Decimal(str(v.get("total", 0))) for k, v in bal.items() if isinstance(v, dict) and Decimal(str(v.get("total", 0))) > 0}
                    await self._refresh_usdt_value(total_bal)
                cycle += 1
                await asyncio.sleep(5)
            except Exception as e:
                LOG.error("[%s] Bal: %s", self.name, e)
                await asyncio.sleep(5)

    async def _refresh_usdt_value(self, balances: Dict[str, Decimal]):
        """Value every asset held on this exchange in USDT terms, so 'how much
        money do I actually have here' is answerable regardless of what it's
        currently parked in. Doesn't move any funds — valuation only."""
        total = Decimal("0")
        needed = [a for a in balances if a not in ("USDT", "USD", "USDC")]
        prices: Dict[str, Decimal] = {}
        if needed:
            try:
                if self.ex.has.get('fetchTickers'):
                    symbols = [f"{a}/USDT" for a in needed if f"{a}/USDT" in self.ex.markets]
                    if symbols:
                        tickers = await self.ex.fetch_tickers(symbols)
                        for sym, t in tickers.items():
                            asset = sym.split("/")[0]
                            last = t.get('last') or t.get('close')
                            if last:
                                prices[asset] = Decimal(str(last))
                else:
                    for a in needed:
                        sym = f"{a}/USDT"
                        if sym in self.ex.markets:
                            t = await self.ex.fetch_ticker(sym)
                            last = t.get('last') or t.get('close')
                            if last:
                                prices[a] = Decimal(str(last))
            except Exception as e:
                LOG.warning("[%s] USDT valuation price fetch: %s", self.name, e)

        for asset, amt in balances.items():
            if asset in ("USDT", "USD", "USDC"):
                total += amt
            elif asset in prices:
                total += amt * prices[asset]
            # Assets with no discoverable USDT pair are left out of the total
            # rather than guessed at — an incomplete-but-honest number beats
            # a fabricated one.
        self.usdt_value = total

    def get_ob(self):
        if not self._ob or (time.time() - self.last_up) > 5:
            return None
        return self._ob

    def get_bal(self, cur: str) -> Decimal:
        return self._balances.get(cur, Decimal("0"))

    def has_bal(self, side: str, amt: Decimal, px: Decimal) -> bool:
        base, quote = self.symbol.split("/")
        if side == "buy":
            return self.get_bal(quote) >= amt * px
        return self.get_bal(base) >= amt

    async def place_order(self, side: str, amt: Decimal, px: Decimal):
        if self.demo:
            await asyncio.sleep(0.05)
            return {"id": "DEMO-%d" % time.time_ns(), "symbol": self.symbol, "side": side, "type": "limit",
                    "amount": float(amt), "price": float(px), "average": float(px), "status": "closed",
                    "filled": float(amt), "remaining": 0.0}
        if not self.ex:
            raise ConnectionError("Not connected")
        return await self.ex.create_order(self.symbol, "limit", side, float(amt), float(px), params={"timeInForce": "IOC"})

    async def shutdown(self):
        self._shutdown = True
        self.connected = False
        if self.ex:
            try:
                await self.ex.close()
            except Exception:
                pass

# ------------------------------------------------------------------
# ENGINE
# ------------------------------------------------------------------
class ArbEngine:
    def __init__(self, cfg_mgr: ConfigManager):
        self.cfg_mgr = cfg_mgr
        self.exs: Dict[str, ExMan] = {}
        self.running = False
        self.detected = 0
        self.executed = 0
        self._shutdown = asyncio.Event()
        self._tasks = []
        self._status = {"mode": "STOPPED", "message": "Engine not started"}
        self._history = deque(maxlen=300)
        self._trades_db = os.path.join(os.path.dirname(__file__), 'trades.db')
        self._init_db()

        # --- Circuit breaker / risk state ---
        self._circuit_open = False
        self._circuit_reason = ""
        self._circuit_auto_clearable = False  # True for daily/drawdown limits, False for naked-position/unknown-state trips that need manual review
        self._daily_date = datetime.utcnow().date()
        self._daily_trade_count = 0
        self._daily_pnl = Decimal("0")
        self._peak_pnl = Decimal("0")
        self._last_trade_ts = 0.0
        self._risk_cfg = {}

    def _init_db(self):
        conn = sqlite3.connect(self._trades_db)
        conn.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, eid TEXT, buy_ex TEXT, sell_ex TEXT, buy_px TEXT, sell_px TEXT, vol TEXT, pnl TEXT, status TEXT, latency_ms INTEGER)")
        # Every signal that cleared the profit bar, whether it got executed or
        # not — this is the actual record of "what opportunities existed",
        # separate from "what we captured". Needed to honestly answer
        # questions like "are we missing profitable trades, and why?"
        conn.execute("""CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, eid TEXT, symbol TEXT,
            buy_ex TEXT, sell_ex TEXT, buy_vwap TEXT, sell_vwap TEXT, volume TEXT,
            projected_profit TEXT, buy_slip TEXT, sell_slip TEXT,
            selected INTEGER, outcome TEXT, detail TEXT
        )""")
        conn.commit()
        conn.close()

    def _log_opportunity(self, sig: 'ArbSig', selected: bool, outcome: str, detail: str = ""):
        conn = sqlite3.connect(self._trades_db)
        conn.execute("""INSERT INTO opportunities
            (ts, eid, symbol, buy_ex, sell_ex, buy_vwap, sell_vwap, volume, projected_profit, buy_slip, sell_slip, selected, outcome, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.utcnow().isoformat(), sig.eid, sig.symbol, sig.buy_ex, sig.sell_ex,
             str(sig.buy_vwap), str(sig.sell_vwap), str(sig.volume), str(sig.profit),
             str(sig.buy_slip), str(sig.sell_slip), 1 if selected else 0, outcome, detail))
        conn.commit()
        conn.close()

    def _reset_daily_if_needed(self):
        today = datetime.utcnow().date()
        if today != self._daily_date:
            self._daily_date = today
            self._daily_trade_count = 0
            self._daily_pnl = Decimal("0")
            self._peak_pnl = Decimal("0")
            if self._circuit_open and self._circuit_auto_clearable:
                self._circuit_open = False
                self._circuit_reason = ""
                self._circuit_auto_clearable = False
                LOG.info("Daily risk counters reset — circuit breaker re-closed")

    def trip_circuit(self, reason: str, auto_clearable: bool = False):
        if not self._circuit_open:
            LOG.error("CIRCUIT BREAKER OPEN: %s", reason)
        self._circuit_open = True
        self._circuit_reason = reason
        self._circuit_auto_clearable = auto_clearable

    def reset_circuit(self):
        self._circuit_open = False
        self._circuit_reason = ""
        self._circuit_auto_clearable = False
        LOG.info("Circuit breaker manually reset")

    async def start(self, mode: str = "demo"):
        tc = self.cfg_mgr.get_trading_config()
        symbol = tc.get('symbol', 'BTC/USDT')
        vol = Decimal(tc.get('target_volume', '0.001'))
        min_profit = Decimal(tc.get('min_profit_usd', '15.0'))
        max_slip = Decimal(tc.get('max_slippage_pct', '0.1'))
        depth = int(tc.get('vwap_depth', 20) or 20)
        poll_ms = int(tc.get('poll_interval_ms', 50) or 50)
        sizing_mode = tc.get('position_sizing_mode', 'fixed')
        size_pct = Decimal(str(tc.get('position_size_pct', '0.1')))
        fixed_cost = Decimal(str(tc.get('fixed_cost_usd', '2.50')))
        demo = mode == "demo"
        paper = mode == "paper"

        # Risk config used by the circuit breaker — applies in every mode so
        # paper/demo behave the same way live will.
        self._risk_cfg = {
            "max_daily_trades": int(tc.get('max_daily_trades', 50) or 0),
            "max_daily_loss_usd": Decimal(str(tc.get('max_daily_loss_usd', '500'))),
            "cooldown_seconds": float(tc.get('cooldown_seconds', 30) or 0),
            "max_drawdown_pct": Decimal(str(tc.get('max_drawdown_pct', '0.05'))),
        }
        self._reset_daily_if_needed()
        self.reset_circuit()

        ex_cfgs = []
        ex_defs = [("binance", "BINANCE"), ("okx", "OKX"), ("bybit", "BYBIT"), ("bitget", "BITGET"), ("mexc", "MEXC")]

        if demo:
            ex_cfgs = [(n, "demo", "demo", "") for n, _ in ex_defs]
            self._status = {"mode": "DEMO", "message": "Running with simulated data"}
        else:
            for name, prefix in ex_defs:
                keys = self.cfg_mgr.get_exchange_keys(name)
                if keys['api_key'] and keys['api_secret']:
                    ex_cfgs.append((name, keys['api_key'], keys['api_secret'], keys.get('passphrase', '')))
            if len(ex_cfgs) < 2:
                self._status = {"mode": "ERROR", "message": "Need 2+ exchanges with API keys"}
                return False
            self._status = {"mode": "PAPER" if paper else "LIVE", "message": "Connected to real exchanges"}

        self.exs = {}
        for name, key, secret, passphrase in ex_cfgs:
            try:
                ex = ExMan(name, symbol, key, secret, passphrase, demo=demo)
                await ex.connect()
                self.exs[name] = ex
            except Exception as e:
                LOG.error("Failed to init %s: %s", name, e)

        if len(self.exs) < 2:
            self._status = {"mode": "ERROR", "message": "Only %d exchange(s) connected" % len(self.exs)}
            return False

        self.running = True
        self._shutdown.clear()
        self._tasks.append(asyncio.create_task(self._det_loop(vol, min_profit, max_slip, paper or demo, poll_ms, live=not (demo or paper), symbol=symbol, depth=depth, sizing_mode=sizing_mode, size_pct=size_pct, fixed_cost=fixed_cost)))
        self._tasks.append(asyncio.create_task(self._metrics_loop()))
        LOG.info("ENGINE ONLINE")
        return True

    async def _det_loop(self, vol: Decimal, min_profit: Decimal, max_slip: Decimal, paper: bool, poll_ms: int, live: bool, symbol: str, depth: int, sizing_mode: str = 'fixed', size_pct: Decimal = Decimal("0.1"), fixed_cost: Decimal = Decimal("2.50")):
        fee_map = {name: ex.taker_fee for name, ex in self.exs.items()}
        gas = fixed_cost
        cycle_ns = max(1, poll_ms) * 1_000_000
        base_ccy, quote_ccy = symbol.split("/")

        while not self._shutdown.is_set():
            t0 = time.time_ns()
            try:
                self._reset_daily_if_needed()

                if self._circuit_open:
                    await asyncio.sleep(0.5)
                    continue

                if time.time() - self._last_trade_ts < self._risk_cfg.get("cooldown_seconds", 0):
                    await asyncio.sleep(0.05)
                    continue

                books = {n: e.get_ob() for n, e in self.exs.items() if e.get_ob()}
                if len(books) < 2:
                    await asyncio.sleep(0.1)
                    continue

                names = list(books.keys())
                candidates = []
                for i in range(len(names)):
                    for j in range(len(names)):
                        if i == j: continue

                        pair_vol = vol
                        if sizing_mode == 'pct_of_balance':
                            # Size this trade off what's actually available on both
                            # legs right now, not a number picked once and left
                            # static — so trades scale up automatically as your
                            # balance grows instead of staying capped forever.
                            buy_ex, sell_ex = self.exs[names[i]], self.exs[names[j]]
                            best_ask = books[names[i]].best_ask()
                            if not best_ask or best_ask <= 0:
                                continue
                            quote_avail = buy_ex.get_bal(quote_ccy)
                            base_avail = sell_ex.get_bal(base_ccy)
                            max_by_buy = quote_avail / best_ask
                            max_by_sell = base_avail
                            pair_vol = min(max_by_buy, max_by_sell) * size_pct
                            if pair_vol <= 0:
                                continue

                        sig = Calc.validate(books[names[i]], books[names[j]], pair_vol, min_profit, max_slip,
                                            fee_map.get(names[i], Decimal("0.001")),
                                            fee_map.get(names[j], Decimal("0.001")), gas, symbol, depth)
                        if sig:
                            sig.buy_ex = names[i]
                            sig.sell_ex = names[j]
                            candidates.append(sig)

                if candidates:
                    # Always take the single most profitable opportunity available
                    # this cycle, not just the first pair combo that happened to
                    # qualify — with 4 exchanges there can be several valid signals
                    # in the same instant, and they're rarely equally profitable.
                    candidates.sort(key=lambda s: s.profit, reverse=True)
                    best = candidates[0]
                    for runner_up in candidates[1:]:
                        self._log_opportunity(runner_up, selected=False, outcome="SKIPPED_LOWER_PROFIT",
                                               detail="Best available this cycle: %s->%s $%.2f" % (best.buy_ex, best.sell_ex, best.profit))
                    await self._process_sig(best, paper, live, fee_map)
            except Exception:
                LOG.error("Detection loop error:\n%s", traceback.format_exc())

            elapsed = time.time_ns() - t0
            sleep = max(0, cycle_ns - elapsed)
            if sleep > 0:
                await asyncio.sleep(sleep / 1e9)

    @staticmethod
    def actual_fee_cost(order: dict, fill_price: Decimal, fill_qty: Decimal, fallback_rate: Decimal,
                         base_ccy: str, quote_ccy: str) -> Decimal:
        """Best-effort extraction of the REAL fee paid on a fill, in quote-currency
        terms, from the exchange's own order response. Falls back to an estimate
        (fallback_rate * notional) only if the exchange didn't report fee data —
        estimates should never be presented as if they were the real number, but
        having *some* deduction beats silently charging zero fee against PnL."""
        fee_info = order.get('fee') or (order.get('fees') or [None])[0]
        if fee_info and fee_info.get('cost') is not None:
            try:
                cost = Decimal(str(fee_info['cost']))
                currency = (fee_info.get('currency') or '').upper()
                if currency == quote_ccy.upper():
                    return cost
                if currency == base_ccy.upper() and fill_price > 0:
                    return cost * fill_price
                # Fee paid in a third currency (e.g. BNB) — we don't have a
                # live conversion rate for it here, so fall through to the
                # rate-based estimate rather than guess at a conversion.
            except Exception:
                pass
        return fill_qty * fill_price * fallback_rate

    async def _process_sig(self, sig: ArbSig, paper: bool, live: bool, fee_map: dict = None):
        fee_map = fee_map or {}
        self.detected += 1
        buy_m = self.exs[sig.buy_ex]
        sell_m = self.exs[sig.sell_ex]

        if not buy_m.has_bal("buy", sig.volume, sig.buy_vwap) or not sell_m.has_bal("sell", sig.volume, sig.sell_vwap):
            self._log_opportunity(sig, selected=True, outcome="SKIPPED_INSUFFICIENT_BALANCE")
            return

        # Live mode only: re-check balance right before committing capital.
        # The cached balance used above can be up to 5s stale (see ExMan._bal_loop);
        # a fresh check here trades a little latency for a lot of correctness.
        if live:
            try:
                fresh_bal = await buy_m.ex.fetch_balance()
                fresh_sell_bal = await sell_m.ex.fetch_balance()
                base, quote = sig.symbol.split("/")
                buy_quote_free = Decimal(str(fresh_bal.get(quote, {}).get('free', 0)))
                sell_base_free = Decimal(str(fresh_sell_bal.get(base, {}).get('free', 0)))
                if buy_quote_free < sig.volume * sig.buy_vwap or sell_base_free < sig.volume:
                    LOG.warning("Skipping %s: fresh balance check failed (stale cache)", sig.eid)
                    self._log_opportunity(sig, selected=True, outcome="SKIPPED_STALE_BALANCE")
                    return
            except Exception as e:
                LOG.error("Fresh balance check failed for %s: %s — skipping trade", sig.eid, e)
                self._log_opportunity(sig, selected=True, outcome="SKIPPED_BALANCE_CHECK_ERROR", detail=str(e))
                return

        start = time.time_ns()
        status = "FAILED"
        if paper:
            await asyncio.sleep(0.05)
            res = ExecRes(True, sig.volume, sig.volume, sig.buy_vwap, sig.sell_vwap, sig.profit, 50)
            status = "FILLED"
        else:
            try:
                buy_px = sig.buy_vwap * Decimal("1.0002")
                sell_px = sig.sell_vwap * Decimal("0.9998")
                br, sr = await asyncio.gather(
                    buy_m.place_order("buy", sig.volume, buy_px),
                    sell_m.place_order("sell", sig.volume, sell_px),
                    return_exceptions=True
                )
                lat = (time.time_ns() - start) // 1_000_000
                b_ok = not isinstance(br, Exception)
                s_ok = not isinstance(sr, Exception)

                if b_ok and s_ok:
                    bf = Decimal(str(br.get("filled", 0)))
                    sf = Decimal(str(sr.get("filled", 0)))
                    bavg = Decimal(str(br.get("average", br.get("price", 0))))
                    savg = Decimal(str(sr.get("average", sr.get("price", 0))))
                    matched = min(bf, sf)
                    pnl = (savg - bavg) * matched
                    base_ccy, quote_ccy = sig.symbol.split("/")
                    buy_fee_actual = self.actual_fee_cost(br, bavg, bf, fee_map.get(sig.buy_ex, Decimal("0.001")), base_ccy, quote_ccy)
                    sell_fee_actual = self.actual_fee_cost(sr, savg, sf, fee_map.get(sig.sell_ex, Decimal("0.001")), base_ccy, quote_ccy)
                    pnl -= (buy_fee_actual + sell_fee_actual)

                    # IOC orders can partially fill even with no exception raised
                    # at all — if the two legs filled different amounts, the
                    # unmatched leftover is a real, untracked naked position on
                    # whichever side over-filled. Flatten it immediately rather
                    # than silently carrying inventory drift forward.
                    leftover = bf - sf
                    unwind_note = ""
                    if abs(leftover) > 0:
                        if leftover > 0:
                            # Bought more than we sold — flatten the excess long on buy_m.
                            extra_pnl, unwound = await self._unwind_leg(buy_m, "sell", leftover, bavg)
                        else:
                            # Sold more than we bought — buy back the excess on sell_m.
                            extra_pnl, unwound = await self._unwind_leg(sell_m, "buy", abs(leftover), savg)
                        if unwound:
                            pnl += extra_pnl
                            unwind_note = "Partial-fill mismatch (buy=%s sell=%s) — leftover %s unwound" % (bf, sf, leftover)
                            LOG.warning("%s: %s", sig.eid, unwind_note)
                        else:
                            unwind_note = "Partial-fill mismatch (buy=%s sell=%s) — leftover %s UNWIND FAILED" % (bf, sf, leftover)
                            LOG.error("%s: %s", sig.eid, unwind_note)
                            self.trip_circuit("Unhedged leftover position after partial-fill mismatch on %s/%s — manual check required" % (sig.buy_ex, sig.sell_ex))

                    res = ExecRes(True, bf, sf, bavg, savg, pnl, lat, unwind_note)
                    status = "FILLED"

                elif b_ok and not s_ok:
                    # Buy leg filled, sell leg failed: we're holding an unhedged
                    # long position on buy_m. Try to flatten it immediately.
                    bf = Decimal(str(br.get("filled", 0)))
                    bavg = Decimal(str(br.get("average", br.get("price", 0))))
                    LOG.error("LEG FAILURE %s: buy filled, sell failed (%s). Attempting unwind on %s.",
                              sig.eid, sr, sig.buy_ex)
                    unwind_pnl, unwound = await self._unwind_leg(buy_m, "sell", bf, bavg)
                    if unwound:
                        res = ExecRes(True, bf, bf, bavg, Decimal("0"), unwind_pnl, lat, "Unwound after sell-leg failure")
                        status = "UNWOUND"
                    else:
                        res = ExecRes(False, bf, Decimal("0"), bavg, Decimal("0"), Decimal("0"), lat, "NAKED POSITION: unwind failed")
                        status = "NAKED_POSITION"
                        self.trip_circuit("Naked position on %s after failed unwind — manual intervention required" % sig.buy_ex)

                elif s_ok and not b_ok:
                    # Sell leg filled, buy leg failed: our inventory on sell_m is
                    # now short by sig.volume relative to what we intended. Buy it back.
                    sf = Decimal(str(sr.get("filled", 0)))
                    savg = Decimal(str(sr.get("average", sr.get("price", 0))))
                    LOG.error("LEG FAILURE %s: sell filled, buy failed (%s). Attempting unwind on %s.",
                              sig.eid, br, sig.sell_ex)
                    unwind_pnl, unwound = await self._unwind_leg(sell_m, "buy", sf, savg)
                    if unwound:
                        res = ExecRes(True, Decimal("0"), sf, Decimal("0"), savg, unwind_pnl, lat, "Unwound after buy-leg failure")
                        status = "UNWOUND"
                    else:
                        res = ExecRes(False, Decimal("0"), sf, Decimal("0"), savg, Decimal("0"), lat, "NAKED POSITION: unwind failed")
                        status = "NAKED_POSITION"
                        self.trip_circuit("Naked position on %s after failed unwind — manual intervention required" % sig.sell_ex)

                else:
                    # Both legs failed. We assume (but cannot fully confirm from
                    # here without exchange-side order reconciliation) that no
                    # position was taken. Treat as a straightforward miss.
                    res = ExecRes(False, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), lat, "Both legs failed")
                    status = "FAILED"

            except Exception as e:
                # Unexpected error outside the normal gather/handle flow — we
                # genuinely don't know what state either exchange is in.
                # Fail safe: halt the engine rather than silently continuing.
                LOG.error("UNKNOWN EXECUTION STATE for %s: %s\n%s", sig.eid, e, traceback.format_exc())
                res = ExecRes(False, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), 0, str(e))
                status = "UNKNOWN"
                self.trip_circuit("Unknown execution state on signal %s — verify exchange balances manually before resuming" % sig.eid)

        if status in ("FILLED", "UNWOUND"):
            self.executed += 1
            self._daily_trade_count += 1
            self._daily_pnl += res.pnl
            self._peak_pnl = max(self._peak_pnl, self._daily_pnl)
            self._last_trade_ts = time.time()
            LOG.info("%s %s | PnL: $%.2f", status, sig.eid, res.pnl)

            # Circuit breaker checks
            rc = self._risk_cfg
            if rc.get("max_daily_trades") and self._daily_trade_count >= rc["max_daily_trades"]:
                self.trip_circuit("Max daily trades reached (%d)" % rc["max_daily_trades"], auto_clearable=True)
            if rc.get("max_daily_loss_usd") and self._daily_pnl <= -rc["max_daily_loss_usd"]:
                self.trip_circuit("Max daily loss reached ($%.2f)" % float(-self._daily_pnl), auto_clearable=True)
            if rc.get("max_drawdown_pct") and self._peak_pnl > 0:
                drawdown = (self._peak_pnl - self._daily_pnl) / self._peak_pnl
                if drawdown >= rc["max_drawdown_pct"]:
                    self.trip_circuit("Max drawdown reached (%.1f%%)" % float(drawdown * 100), auto_clearable=True)

        conn = sqlite3.connect(self._trades_db)
        conn.execute("INSERT INTO trades (ts, eid, buy_ex, sell_ex, buy_px, sell_px, vol, pnl, status, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (datetime.utcnow().isoformat(), sig.eid, sig.buy_ex, sig.sell_ex, str(res.buy_px), str(res.sell_px),
                      str(sig.volume), str(res.pnl), status, res.latency_ms))
        conn.commit()
        conn.close()
        self._log_opportunity(sig, selected=True, outcome=status, detail=getattr(res, 'error', '') or '')

    async def _unwind_leg(self, ex_mgr: 'ExMan', side: str, amount: Decimal, ref_price: Decimal) -> Tuple[Decimal, bool]:
        """Flatten a stray position after a leg failure or fill mismatch —
        aggressively, and with retries. The whole point of this function is
        'never sit exposed waiting' — a single failed attempt must not leave
        a naked position just sitting there. Each retry accepts a worse price
        than the last; getting flat matters far more than getting a good exit
        price once something has already gone wrong.
        Returns (realized_pnl, fully_exited: bool)."""
        remaining = amount
        total_pnl = Decimal("0")
        # Escalating price concessions: 0.5% -> 1% -> 2% -> 4% away from
        # reference. If even a 4%-away IOC order can't get filled, the
        # problem isn't the price — it's genuine market/connectivity failure
        # that no amount of further automated retrying will fix.
        buffers = [Decimal("0.005"), Decimal("0.01"), Decimal("0.02"), Decimal("0.04")]

        for attempt, buf in enumerate(buffers, start=1):
            if remaining <= 0:
                break
            try:
                px = ref_price * (Decimal("1") - buf) if side == "sell" else ref_price * (Decimal("1") + buf)
                r = await ex_mgr.place_order(side, remaining, px)
                filled = Decimal(str(r.get("filled", 0)))
                avg = Decimal(str(r.get("average", r.get("price", 0))))
                if filled > 0:
                    leg_pnl = (avg - ref_price) * filled if side == "sell" else (ref_price - avg) * filled
                    total_pnl += leg_pnl
                    remaining -= filled
                    LOG.warning("Unwind attempt %d/%d on %s: filled %s/%s at %s%% off ref",
                                attempt, len(buffers), ex_mgr.name, filled, amount, float(buf * 100))
                if remaining <= 0:
                    return total_pnl, True
            except Exception as e:
                LOG.error("Unwind attempt %d/%d failed on %s: %s", attempt, len(buffers), ex_mgr.name, e)
            if remaining > 0 and attempt < len(buffers):
                await asyncio.sleep(0.3)

        if remaining < amount:
            # Partially unwound — better than nothing, but still a real gap.
            LOG.error("Unwind on %s only partially succeeded: %s of %s still unhedged after %d attempts",
                       ex_mgr.name, remaining, amount, len(buffers))
        return total_pnl, remaining <= 0

    async def _metrics_loop(self):
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                snapshot = {
                    "ts": time.time(),
                    "pnl": float(self._get_total_pnl()),
                    "detected": self.detected,
                    "executed": self.executed,
                    "exchanges": {n: {"connected": e.connected, "msgs": e.msgs, "balances": {k: str(v) for k, v in e._balances.items()}} for n, e in self.exs.items()}
                }
                self._history.append(snapshot)

    def _get_total_pnl(self):
        conn = sqlite3.connect(self._trades_db)
        row = conn.execute("SELECT SUM(CAST(pnl AS REAL)) FROM trades WHERE status = 'FILLED'").fetchone()
        conn.close()
        return Decimal(str(row[0] or 0))

    async def stop(self):
        LOG.info("Stopping engine...")
        self._shutdown.set()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for ex in self.exs.values():
            await ex.shutdown()
        self.running = False
        self._status = {"mode": "STOPPED", "message": "Engine stopped"}

    def get_status(self):
        portfolio_usdt = sum((e.usdt_value for e in self.exs.values()), Decimal("0"))
        return {
            "mode": self._status.get("mode", "STOPPED"),
            "message": self._status.get("message", ""),
            "running": self.running,
            "detected": self.detected,
            "executed": self.executed,
            "success_rate": round(self.executed / max(self.detected, 1) * 100, 1),
            "total_pnl": str(self._get_total_pnl()),
            "exchanges": {n: {"connected": e.connected, "msgs": e.msgs, "balances": {k: str(v) for k, v in e._balances.items()}, "usdt_value": str(e.usdt_value)} for n, e in self.exs.items()},
            "portfolio_usdt_value": str(portfolio_usdt),
            "history": list(self._history),
            "circuit_state": "OPEN" if self._circuit_open else "CLOSED",
            "circuit_reason": self._circuit_reason,
            "circuit_auto_clearable": self._circuit_auto_clearable,
            "daily_trades": self._daily_trade_count,
            "daily_pnl": str(self._daily_pnl),
        }

    def get_trades(self, limit: int = 100):
        conn = sqlite3.connect(self._trades_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_opportunities(self, limit: int = 200):
        conn = sqlite3.connect(self._trades_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM opportunities ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_analytics(self):
        """Aggregate stats for analytics/presentation — real numbers pulled
        straight from the trades/opportunities tables, not estimates."""
        conn = sqlite3.connect(self._trades_db)
        conn.row_factory = sqlite3.Row

        trades = [dict(r) for r in conn.execute("SELECT * FROM trades").fetchall()]
        opps = [dict(r) for r in conn.execute("SELECT * FROM opportunities").fetchall()]
        conn.close()

        filled = [t for t in trades if t['status'] in ('FILLED', 'UNWOUND')]
        pnls = [Decimal(t['pnl']) for t in filled]
        total_pnl = sum(pnls) if pnls else Decimal("0")
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        # Which exchange pair has actually been most profitable, on average —
        # this is the real answer to "which opportunities should we favor",
        # grounded in what actually happened rather than a guess.
        pair_stats = {}
        for t in filled:
            key = f"{t['buy_ex']}->{t['sell_ex']}"
            pair_stats.setdefault(key, {"count": 0, "total_pnl": Decimal("0")})
            pair_stats[key]["count"] += 1
            pair_stats[key]["total_pnl"] += Decimal(t['pnl'])
        pair_summary = sorted(
            [{"pair": k, "trades": v["count"], "total_pnl": str(v["total_pnl"]),
              "avg_pnl": str(v["total_pnl"] / v["count"])} for k, v in pair_stats.items()],
            key=lambda x: Decimal(x["total_pnl"]), reverse=True
        )

        skip_reasons = {}
        for o in opps:
            if o['outcome'] not in ('FILLED', 'UNWOUND'):
                skip_reasons[o['outcome']] = skip_reasons.get(o['outcome'], 0) + 1

        selected_opps = [o for o in opps if o['selected']]
        captured = len([o for o in selected_opps if o['outcome'] in ('FILLED', 'UNWOUND')])

        return {
            "total_trades": len(filled),
            "total_pnl": str(total_pnl),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate_pct": round(len(wins) / len(filled) * 100, 1) if filled else 0.0,
            "avg_win": str(sum(wins) / len(wins)) if wins else "0",
            "avg_loss": str(sum(losses) / len(losses)) if losses else "0",
            "best_trade": str(max(pnls)) if pnls else "0",
            "worst_trade": str(min(pnls)) if pnls else "0",
            "opportunities_seen": len(selected_opps),
            "opportunities_captured": captured,
            "capture_rate_pct": round(captured / len(selected_opps) * 100, 1) if selected_opps else 0.0,
            "skip_reasons": skip_reasons,
            "pair_performance": pair_summary,
        }

# ------------------------------------------------------------------
# FASTAPI APP
# ------------------------------------------------------------------
app = FastAPI(title="ARB Pro Backend", version="6.0.0")
cfg_mgr = ConfigManager()
engine = ArbEngine(cfg_mgr)

@app.get("/")
def root():
    return {"status": "ARB Pro Backend v6.0", "mode": engine._status.get("mode", "STOPPED")}

@app.get("/api/status")
def get_status():
    return engine.get_status()

@app.get("/api/trades")
def get_trades(limit: int = 100):
    return engine.get_trades(limit)

@app.get("/api/opportunities")
def get_opportunities(limit: int = 200):
    return engine.get_opportunities(limit)

@app.get("/api/analytics")
def get_analytics():
    return engine.get_analytics()

@app.get("/api/analytics/export")
def export_analytics_csv():
    import io
    trades = engine.get_trades(100000)
    buf = io.StringIO()
    if trades:
        writer = csv.DictWriter(buf, fieldnames=list(trades[0].keys()))
        writer.writeheader()
        writer.writerows(trades)
    return Response(content=buf.getvalue(), media_type="text/csv",
                     headers={"Content-Disposition": "attachment; filename=arb_pro_trades.csv"})

@app.get("/api/config")
def get_config():
    tc = cfg_mgr.get_trading_config()
    branding = cfg_mgr.get_branding()
    return {"trading": tc, "branding": branding}

@app.post("/api/config/trading")
async def save_trading_config(data: dict):
    cfg_mgr.save_trading_config(data)
    return {"ok": True}

@app.post("/api/config/keys/{exchange}")
async def save_keys(exchange: str, data: dict):
    cfg_mgr.save_exchange_keys(exchange, data.get("api_key", ""), data.get("api_secret", ""), data.get("passphrase", ""))
    return {"ok": True}

@app.post("/api/config/branding")
async def save_branding(data: dict):
    cfg_mgr.save_branding(data.get("name", "ARB Pro"), data.get("slogan", ""))
    return {"ok": True}

@app.post("/api/engine/start")
async def start_engine(data: dict):
    mode = data.get("mode", "demo")
    ok = await engine.start(mode)
    return {"ok": ok, "mode": mode}

@app.post("/api/engine/stop")
async def stop_engine():
    await engine.stop()
    return {"ok": True}

@app.post("/api/engine/reset-circuit")
async def reset_circuit():
    engine.reset_circuit()
    return {"ok": True}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(engine.get_status())
            await asyncio.sleep(2)
    except Exception:
        pass

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("ARB_PORT", 8765))
    LOG.info("Starting ARB Pro Backend on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
