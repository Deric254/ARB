#!/usr/bin/env python3
"""
================================================================================
SPATIAL ARBITRAGE TRADING ENGINE — PRODUCTION v5.1
High-Frequency Cryptocurrency Arbitrage System
================================================================================

ZERO-GAP ARCHITECTURE with REAL-TIME CHART DASHBOARD + SLICERS:
 - Live Chart.js dashboard with auto-refresh
 - KPI cards: P&L, Success Rate, Latency, Signals/min, Trades, Spread
 - Real-time charts: P&L over time, Execution Latency (p50/p99),
   Signal Rate, Exchange Spreads, Price Divergence
 - INTERACTIVE SLICERS: Time Range, Profit Filter, Exchange Pair, Status, Refresh Rate
 - Trade Log Table with live filtering
 - Circuit Breaker, P&L Ledger, Latency Telemetry, Webhook Alerts
 - Synthetic DEMO mode + LIVE WebSocket mode

Usage:
 python arbitrage_engine.py              # DEMO mode, 120s, auto-opens browser
 python arbitrage_engine.py --live       # LIVE mode (requires API keys)
 python arbitrage_engine.py --duration 300 --port 8080

Requirements:
 pip install ccxt aiofiles
"""

import asyncio
import signal
import sys
import time
import sqlite3
import os
import json
import logging
import traceback
import random
import argparse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP, getcontext
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

getcontext().prec = 50

try:
    import ccxt.pro as ccxtpro
except ImportError:
    try:
        import ccxt.async_support as ccxtpro
    except ImportError:
        ccxtpro = None

try:
    import aiofiles
except ImportError:
    raise ImportError("pip install aiofiles")

# =============================================================================
# CONFIGURATION & DATA STRUCTURES
# =============================================================================

class TradeSide(Enum):
    BUY = "buy"
    SELL = "sell"

class ExchangeId(Enum):
    BINANCE = "binance"
    KRAKEN = "kraken"

class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EXECUTION = "execution"

@dataclass(frozen=True)
class ExchangeConfig:
    exchange_id: ExchangeId
    api_key: str
    api_secret: str
    taker_fee_rate: Decimal
    sandbox: bool = True
    def __post_init__(self):
        fee = Decimal(str(self.taker_fee_rate)).quantize(Decimal("0.00000001"))
        object.__setattr__(self, 'taker_fee_rate', fee)

@dataclass(frozen=True)
class ArbitrageConfig:
    symbol: str
    target_volume: Decimal
    min_yield_threshold: Decimal
    estimated_gas_fee: Decimal
    max_slippage_tolerance: Decimal
    vwap_depth_limit: int = 20
    def __post_init__(self):
        object.__setattr__(self, 'target_volume', Decimal(str(self.target_volume)))
        object.__setattr__(self, 'min_yield_threshold', Decimal(str(self.min_yield_threshold)))
        object.__setattr__(self, 'estimated_gas_fee', Decimal(str(self.estimated_gas_fee)))
        object.__setattr__(self, 'max_slippage_tolerance', Decimal(str(self.max_slippage_tolerance)))

@dataclass(frozen=True)
class RiskConfig:
    max_daily_trades: int = 1000
    max_daily_loss: Decimal = Decimal("500.00")
    max_consecutive_failures: int = 5
    cooldown_seconds_after_failure: int = 30
    max_open_position_value: Decimal = Decimal("100000.00")
    circuit_breaker_drawdown_pct: Decimal = Decimal("0.02")

@dataclass
class OrderBookLevel:
    price: Decimal
    volume: Decimal
    def __post_init__(self):
        if isinstance(self.price, (int, float, str)):
            object.__setattr__(self, 'price', Decimal(str(self.price)))
        if isinstance(self.volume, (int, float, str)):
            object.__setattr__(self, 'volume', Decimal(str(self.volume)))

@dataclass
class OrderBookSnapshot:
    exchange_id: ExchangeId
    symbol: str
    timestamp_ns: int
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    @property
    def best_bid(self): return self.bids[0].price if self.bids else None
    @property
    def best_ask(self): return self.asks[0].price if self.asks else None
    @property
    def mid_price(self):
        bb, ba = self.best_bid, self.best_ask
        if bb is not None and ba is not None:
            return ((bb + ba) / Decimal("2")).quantize(Decimal("0.01"))
        return None
    @property
    def spread_bps(self):
        bb, ba = self.best_bid, self.best_ask
        if bb is not None and ba is not None and bb > Decimal("0"):
            return ((ba - bb) / bb * Decimal("10000")).quantize(Decimal("0.01"))
        return None

@dataclass
class VWAPResult:
    vwap: Decimal
    executable_volume: Decimal
    levels_consumed: int
    slippage_from_top: Decimal
    fully_filled: bool

@dataclass
class ArbitrageSignal:
    timestamp_ns: int
    symbol: str
    buy_exchange: ExchangeId
    sell_exchange: ExchangeId
    buy_vwap: Decimal
    sell_vwap: Decimal
    target_volume: Decimal
    gross_spread: Decimal
    buy_fee: Decimal
    sell_fee: Decimal
    gas_fee: Decimal
    net_profit: Decimal
    net_profit_pct: Decimal
    execution_id: str

@dataclass
class ExecutionResult:
    execution_id: str
    timestamp_ns: int
    buy_exchange: ExchangeId
    sell_exchange: ExchangeId
    buy_order_id: Optional[str]
    sell_order_id: Optional[str]
    buy_fill_price: Optional[Decimal]
    sell_fill_price: Optional[Decimal]
    filled_volume: Decimal
    status: str
    latency_us: int
    error_message: Optional[str] = None

@dataclass
class Position:
    exchange_id: ExchangeId
    symbol: str
    base_asset: Decimal = Decimal("0")
    quote_asset: Decimal = Decimal("0")
    avg_entry_price: Optional[Decimal] = None
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    trade_count: int = 0

# =============================================================================
# LOGGING
# =============================================================================

class MicrosecondFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, tz=timezone.utc)
        t = ct.strftime("%Y-%m-%d %H:%M:%S")
        return "%s.%06d" % (t, int(record.msecs * 1000))

def setup_logging(log_level=logging.INFO):
    logger = logging.getLogger("ArbitrageEngine")
    logger.setLevel(log_level)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(log_level)
        h.setFormatter(MicrosecondFormatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"))
        logger.addHandler(h)
    return logger

LOG = setup_logging()

# =============================================================================
# HIGH-PRECISION CALCULATION ENGINE
# =============================================================================

class PrecisionCalculator:
    @staticmethod
    def compute_vwap(levels, target_volume, depth_limit=20):
        if not levels or target_volume <= Decimal("0"):
            return VWAPResult(Decimal("0"), Decimal("0"), 0, Decimal("0"), False)
        remaining = target_volume
        cum_val = Decimal("0")
        cum_vol = Decimal("0")
        top_price = levels[0].price
        levels_consumed = 0
        for level in levels[:depth_limit]:
            if remaining <= Decimal("0"): break
            take = min(level.volume, remaining)
            cum_val += take * level.price
            cum_vol += take
            remaining -= take
            levels_consumed += 1
        if cum_vol <= Decimal("0"):
            return VWAPResult(Decimal("0"), Decimal("0"), 0, Decimal("0"), False)
        vwap = (cum_val / cum_vol).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        slippage = abs(((vwap - top_price) / top_price).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)) if top_price != Decimal("0") else Decimal("0")
        return VWAPResult(vwap, cum_vol, levels_consumed, slippage, remaining <= Decimal("0"))

    @staticmethod
    def calculate_net_spread(buy_vwap, sell_vwap, buy_taker_fee, sell_taker_fee, gas_fee, target_volume):
        buy_cost = buy_vwap * target_volume * (Decimal("1") + buy_taker_fee)
        sell_revenue = sell_vwap * target_volume * (Decimal("1") - sell_taker_fee)
        gross = sell_revenue - buy_cost
        net = gross - gas_fee
        net_pct = (net / buy_cost).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP) if buy_cost > Decimal("0") else Decimal("0")
        return gross, net, net_pct

    @staticmethod
    def validate_signal(buy_book, sell_book, config, buy_fee, sell_fee):
        buy_vwap = PrecisionCalculator.compute_vwap(buy_book.asks, config.target_volume, config.vwap_depth_limit)
        sell_vwap = PrecisionCalculator.compute_vwap(sell_book.bids, config.target_volume, config.vwap_depth_limit)
        if not buy_vwap.fully_filled or not sell_vwap.fully_filled: return None
        if buy_vwap.slippage_from_top > config.max_slippage_tolerance or sell_vwap.slippage_from_top > config.max_slippage_tolerance: return None
        gross, net, net_pct = PrecisionCalculator.calculate_net_spread(buy_vwap.vwap, sell_vwap.vwap, buy_fee, sell_fee, config.estimated_gas_fee, config.target_volume)
        if net <= config.min_yield_threshold: return None
        eid = "ARB-%s" % datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:-3]
        return ArbitrageSignal(
            timestamp_ns=time.time_ns(), symbol=config.symbol,
            buy_exchange=buy_book.exchange_id, sell_exchange=sell_book.exchange_id,
            buy_vwap=buy_vwap.vwap, sell_vwap=sell_vwap.vwap, target_volume=config.target_volume,
            gross_spread=gross.quantize(Decimal("0.00000001")),
            buy_fee=(buy_vwap.vwap * config.target_volume * buy_fee).quantize(Decimal("0.00000001")),
            sell_fee=(sell_vwap.vwap * config.target_volume * sell_fee).quantize(Decimal("0.00000001")),
            gas_fee=config.estimated_gas_fee, net_profit=net.quantize(Decimal("0.00000001")),
            net_profit_pct=net_pct, execution_id=eid)

# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitBreaker:
    def __init__(self, risk_config):
        self.risk_config = risk_config
        self._state = "CLOSED"
        self._consecutive_failures = 0
        self._daily_trades = 0
        self._daily_pnl = Decimal("0")
        self._last_failure_time = 0
        self._lock = asyncio.Lock()
        self._opened_at = None
        self._open_reason = None

    @property
    def state(self): return self._state
    @property
    def open_reason(self): return self._open_reason

    async def can_trade(self):
        async with self._lock:
            if self._state == "OPEN":
                if time.time() - self._last_failure_time > self.risk_config.cooldown_seconds_after_failure:
                    self._state = "CLOSED"; self._consecutive_failures = 0
                return True
            return False
        return True

    async def record_success(self, net_profit):
        async with self._lock:
            self._consecutive_failures = 0
            self._daily_trades += 1
            self._daily_pnl += net_profit

    async def record_failure(self, reason=""):
        async with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            if self._consecutive_failures >= self.risk_config.max_consecutive_failures:
                self._open_circuit("Max consecutive failures: %d" % self._consecutive_failures)

    async def check_daily_limits(self):
        async with self._lock:
            if self._daily_trades >= self.risk_config.max_daily_trades:
                self._open_circuit("Daily trade limit: %d" % self._daily_trades); return False
            if self._daily_pnl <= -self.risk_config.max_daily_loss:
                self._open_circuit("Daily loss limit: %s" % self._daily_pnl); return False
            return True

    def _open_circuit(self, reason):
        self._state = "OPEN"; self._opened_at = datetime.now(timezone.utc); self._open_reason = reason
        LOG.critical("[CIRCUIT] BREAKER OPENED: %s" % reason)

    def get_metrics(self):
        return {"state": self._state, "open_reason": self._open_reason,
                "opened_at": self._opened_at.isoformat() if self._opened_at else None,
                "consecutive_failures": self._consecutive_failures,
                "daily_trades": self._daily_trades, "daily_pnl": str(self._daily_pnl)}

# =============================================================================
# LATENCY TELEMETRY
# =============================================================================

class LatencyTracker:
    def __init__(self, window_size=1000):
        self._detection_us = deque(maxlen=window_size)
        self._execution_us = deque(maxlen=window_size)
        self._lock = asyncio.Lock()

    async def record_detection(self, latency_us):
        async with self._lock: self._detection_us.append(latency_us)
    async def record_execution(self, latency_us):
        async with self._lock: self._execution_us.append(latency_us)

    def _pct(self, data, p):
        if not data: return 0.0
        s = sorted(data); k = (len(s) - 1) * p; f = int(k); c = min(f + 1, len(s) - 1)
        return s[f] + (k - f) * (s[c] - s[f]) if c != f else s[f]

    def _stats(self, data):
        if not data: return {"count": 0, "min": 0, "max": 0, "avg": 0, "p50": 0, "p99": 0}
        return {"count": len(data), "min": min(data), "max": max(data),
                "avg": sum(data) / len(data), "p50": self._pct(data, 0.50), "p99": self._pct(data, 0.99)}

    def get_metrics(self):
        return {"detection_us": self._stats(self._detection_us), "execution_us": self._stats(self._execution_us)}

# =============================================================================
# P&L LEDGER
# =============================================================================

class ProfitLossLedger:
    def __init__(self, symbol):
        self.symbol = symbol
        self.base_asset, self.quote_asset = symbol.split("/")
        self.positions = {}
        self._lock = asyncio.Lock()
        self._total_realized_pnl = Decimal("0")
        self._total_unrealized_pnl = Decimal("0")
        self._trade_history = []

    async def initialize_positions(self, exchange_ids, initial_base=Decimal("1.0"), initial_quote=Decimal("100000.0")):
        async with self._lock:
            for ex_id in exchange_ids:
                self.positions[ex_id] = Position(exchange_id=ex_id, symbol=self.symbol,
                                                 base_asset=initial_base, quote_asset=initial_quote)
            LOG.info("[LEDGER] Positions initialized.")

    async def record_execution(self, result, signal):
        async with self._lock:
            if result.status != "FILLED": return
            buy_pos = self.positions[result.buy_exchange]
            sell_pos = self.positions[result.sell_exchange]
            buy_pos.quote_asset -= signal.buy_vwap * signal.target_volume
            buy_pos.base_asset += signal.target_volume
            buy_pos.trade_count += 1
            sell_pos.base_asset -= signal.target_volume
            sell_pos.quote_asset += signal.sell_vwap * signal.target_volume
            sell_pos.trade_count += 1
            self._total_realized_pnl += signal.net_profit
            self._trade_history.append({"timestamp": datetime.now(timezone.utc).isoformat(),
                                        "execution_id": result.execution_id, "net_profit": str(signal.net_profit),
                                        "latency_us": result.latency_us, "status": result.status,
                                        "buy_exchange": result.buy_exchange.value, "sell_exchange": result.sell_exchange.value,
                                        "buy_vwap": str(signal.buy_vwap), "sell_vwap": str(signal.sell_vwap),
                                        "target_volume": str(signal.target_volume)})
            LOG.info("[LEDGER] P&L: %s | Total: %s" % (signal.net_profit, self._total_realized_pnl))

    async def mark_to_market(self, exchange_books):
        async with self._lock:
            total = Decimal("0")
            for ex_id, pos in self.positions.items():
                book = exchange_books.get(ex_id)
                if book and book.mid_price and pos.base_asset > Decimal("0"):
                    pos.unrealized_pnl = pos.base_asset * book.mid_price
                total += pos.unrealized_pnl
            self._total_unrealized_pnl = total

    def get_metrics(self):
        return {"total_realized_pnl": str(self._total_realized_pnl),
                "total_unrealized_pnl": str(self._total_unrealized_pnl),
                "total_pnl": str(self._total_realized_pnl + self._total_unrealized_pnl),
                "positions": {k.value: {"base": str(v.base_asset), "quote": str(v.quote_asset),
                                        "trades": v.trade_count, "unrealized": str(v.unrealized_pnl)}
                              for k, v in self.positions.items()},
                "trade_count": len(self._trade_history),
                "trade_history": list(self._trade_history)}

# =============================================================================
# METRICS HISTORY
# =============================================================================

class MetricsHistory:
    def __init__(self, max_points=300):
        self.max_points = max_points
        self._timestamps = deque(maxlen=max_points)
        self._realized_pnl = deque(maxlen=max_points)
        self._exec_latency_p50 = deque(maxlen=max_points)
        self._exec_latency_p99 = deque(maxlen=max_points)
        self._detection_latency_avg = deque(maxlen=max_points)
        self._signals_detected = deque(maxlen=max_points)
        self._signals_executed = deque(maxlen=max_points)
        self._binance_spread_bps = deque(maxlen=max_points)
        self._kraken_spread_bps = deque(maxlen=max_points)
        self._binance_mid = deque(maxlen=max_points)
        self._kraken_mid = deque(maxlen=max_points)
        self._circuit_state = deque(maxlen=max_points)
        self._lock = asyncio.Lock()

    async def snapshot(self, engine):
        async with self._lock:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            self._timestamps.append(now)
            lat = engine.latency_tracker.get_metrics()
            pnl = engine.ledger.get_metrics()
            circ = engine.circuit_breaker.get_metrics()
            self._realized_pnl.append(float(pnl["total_realized_pnl"]))
            self._exec_latency_p50.append(lat["execution_us"]["p50"])
            self._exec_latency_p99.append(lat["execution_us"]["p99"])
            self._detection_latency_avg.append(lat["detection_us"]["avg"])
            self._signals_detected.append(engine._signals_detected)
            self._signals_executed.append(engine._signals_executed)
            self._circuit_state.append(1 if circ["state"] == "CLOSED" else 0)
            books = {}
            for ex_id, mgr in engine.exchanges.items():
                try:
                    b = mgr._order_book
                    if b: books[ex_id] = b
                except: pass
            b_spread = float(books.get(ExchangeId.BINANCE, OrderBookSnapshot(ExchangeId.BINANCE, "", 0)).spread_bps or 0)
            k_spread = float(books.get(ExchangeId.KRAKEN, OrderBookSnapshot(ExchangeId.KRAKEN, "", 0)).spread_bps or 0)
            b_mid = float(books.get(ExchangeId.BINANCE, OrderBookSnapshot(ExchangeId.BINANCE, "", 0)).mid_price or 0)
            k_mid = float(books.get(ExchangeId.KRAKEN, OrderBookSnapshot(ExchangeId.KRAKEN, "", 0)).mid_price or 0)
            self._binance_spread_bps.append(b_spread)
            self._kraken_spread_bps.append(k_spread)
            self._binance_mid.append(b_mid)
            self._kraken_mid.append(k_mid)

    def get_chart_data(self):
        return {
            "timestamps": list(self._timestamps),
            "realized_pnl": list(self._realized_pnl),
            "exec_latency_p50": list(self._exec_latency_p50),
            "exec_latency_p99": list(self._exec_latency_p99),
            "detection_latency_avg": list(self._detection_latency_avg),
            "signals_detected": list(self._signals_detected),
            "signals_executed": list(self._signals_executed),
            "binance_spread_bps": list(self._binance_spread_bps),
            "kraken_spread_bps": list(self._kraken_spread_bps),
            "binance_mid": list(self._binance_mid),
            "kraken_mid": list(self._kraken_mid),
            "circuit_state": list(self._circuit_state),
        }

# =============================================================================
# ALERT MANAGER
# =============================================================================

class AlertManager:
    def __init__(self, webhook_url=None):
        self.webhook_url = webhook_url
        self._last_alert_time = {}
        self._rate_limit_seconds = 5.0
        self._lock = asyncio.Lock()

    async def send(self, level, title, message, fields=None):
        if not self.webhook_url: return
        async with self._lock:
            now = time.time()
            if now - self._last_alert_time.get(level, 0) < self._rate_limit_seconds: return
            self._last_alert_time[level] = now
            payload = {"level": level.value, "title": title, "message": message,
                       "timestamp": datetime.now(timezone.utc).isoformat(), "fields": fields or {}}
            try: asyncio.create_task(self._post_webhook(payload))
            except Exception as e: LOG.warning("[ALERT] Queue failed: %s" % e)

    async def _post_webhook(self, payload):
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(self.webhook_url, data=data,
                                         headers={'Content-Type': 'application/json'}, method='POST')
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, urllib.request.urlopen, req, 5)
        except Exception as e: LOG.warning("[ALERT] Delivery failed: %s" % e)

    async def alert_execution(self, signal, result):
        await self.send(AlertLevel.EXECUTION, "Arbitrage: %s" % result.execution_id,
                        "Net: %s" % signal.net_profit,
                        {"Buy": signal.buy_exchange.value, "Sell": signal.sell_exchange.value,
                         "Latency": "%d us" % result.latency_us, "Status": result.status})

    async def alert_circuit_opened(self, reason):
        await self.send(AlertLevel.CRITICAL, "CIRCUIT BREAKER OPENED", reason)

    async def alert_error(self, error):
        await self.send(AlertLevel.WARNING, "Engine Error", error)

# =============================================================================
# HEALTH HTTP SERVER with REAL-TIME CHART DASHBOARD + SLICERS
# =============================================================================


# =============================================================================
# HEALTH HTTP SERVER with REAL-TIME CHART DASHBOARD + SLICERS (v5.2 FIXED)
# =============================================================================

class HealthServer:
    def __init__(self, port=8080):
        self.port = port
        self._engine_ref = None
        self._server = None
        self._task = None

    def attach_engine(self, engine):
        self._engine_ref = engine

    async def start(self):
        self._server = await asyncio.start_server(self._handle_request, '0.0.0.0', self.port)
        LOG.info("[HEALTH] Dashboard: http://0.0.0.0:%d/status" % self.port)
        self._task = asyncio.create_task(self._server.serve_forever())

    async def _handle_request(self, reader, writer):
        try:
            request_line = (await reader.readline()).decode('utf-8').strip()
            if not request_line:
                writer.close()
                await writer.wait_closed()
                return
            parts = request_line.split()
            path = parts[1] if len(parts) > 1 else "/"

            if path == "/health":
                body = json.dumps({"status": "ok", "timestamp": time.time_ns()})
                status = "200 OK"
                ct = "application/json"
            elif path == "/metrics":
                body = json.dumps(self._get_metrics(), indent=2, default=str)
                status = "200 OK"
                ct = "application/json"
            elif path == "/status":
                body = self._get_dashboard_html()
                status = "200 OK"
                ct = "text/html"
            elif path == "/api/trades":
                body = json.dumps(self._get_trade_data(), indent=2, default=str)
                status = "200 OK"
                ct = "application/json"
            else:
                body = "Not Found"
                status = "404 Not Found"
                ct = "text/plain"

            response = (
                "HTTP/1.1 %s\r\n"
                "Content-Type: %s\r\n"
                "Content-Length: %d\r\n"
                "Connection: close\r\n"
                "\r\n"
                "%s"
            ) % (status, ct, len(body.encode('utf-8')), body)
            writer.write(response.encode('utf-8'))
            await writer.drain()
        except Exception as e:
            LOG.warning("[HEALTH] Error: %s" % e)
        finally:
            writer.close()
            await writer.wait_closed()

    def _get_metrics(self):
        if self._engine_ref is None:
            return {"error": "No engine"}
        return {
            "engine": {
                "signals_detected": self._engine_ref._signals_detected,
                "signals_executed": self._engine_ref._signals_executed,
                "success_rate": round(
                    self._engine_ref._signals_executed
                    / max(self._engine_ref._signals_detected, 1) * 100, 2
                ),
                "uptime_sec": (time.time_ns() - self._engine_ref._start_time_ns) / 1e9,
            },
            "circuit": self._engine_ref.circuit_breaker.get_metrics(),
            "latency": self._engine_ref.latency_tracker.get_metrics(),
            "pnl": self._engine_ref.ledger.get_metrics(),
            "exchanges": {
                ex_id.value: {"connected": m.is_connected, "msgs": m.messages_received}
                for ex_id, m in self._engine_ref.exchanges.items()
            },
            "charts": self._engine_ref.metrics_history.get_chart_data()
        }

    def _get_trade_data(self):
        if self._engine_ref is None:
            return {"trades": []}
        pnl = self._engine_ref.ledger.get_metrics()
        return {"trades": pnl.get("trade_history", [])}

    def _get_dashboard_html(self):
        return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ARB v5.2 — Spatial Arbitrage Command Center</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #0b0f1a; --bg-card: rgba(18,25,45,0.9); --border: rgba(56,189,248,0.12);
  --text: #e2e8f0; --text-dim: #94a3b8; --accent: #38bdf8; --accent2: #a78bfa;
  --profit: #34d399; --loss: #f87171; --warn: #fbbf24; --glass: rgba(255,255,255,0.03);
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
  min-height: 100vh;
  background-image:
    radial-gradient(circle at 15% 25%, rgba(56,189,248,0.06) 0%, transparent 45%),
    radial-gradient(circle at 85% 75%, rgba(167,139,250,0.06) 0%, transparent 45%);
}
.header {
  padding: 18px 28px; border-bottom: 1px solid var(--border);
  display: flex; justify-content: space-between; align-items: center;
  background: var(--glass); backdrop-filter: blur(12px);
}
.header h1 { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.3px; }
.header h1 span { color: var(--accent); }
.badge {
  padding: 5px 12px; border-radius: 20px; font-size: 0.72rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.badge.closed { background: rgba(52,211,153,0.12); color: var(--profit); border: 1px solid rgba(52,211,153,0.25); }
.badge.open { background: rgba(248,113,113,0.12); color: var(--loss); border: 1px solid rgba(248,113,113,0.25); }

.slicer-panel {
  margin: 18px 28px; padding: 16px 22px;
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px;
  backdrop-filter: blur(12px); display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end;
}
.slicer-group { display: flex; flex-direction: column; gap: 5px; }
.slicer-group label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); font-weight: 600; }
.slicer-group select, .slicer-group input {
  background: var(--glass); border: 1px solid var(--border); color: var(--text);
  padding: 7px 12px; border-radius: 8px; font-size: 0.82rem; outline: none;
  min-width: 130px; cursor: pointer; transition: all 0.2s;
}
.slicer-group select:hover, .slicer-group input:hover { border-color: var(--accent); }
.slicer-group select:focus, .slicer-group input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(56,189,248,0.08); }
.slicer-btn {
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  border: none; color: #fff; padding: 9px 20px; border-radius: 8px;
  font-weight: 600; font-size: 0.82rem; cursor: pointer; transition: all 0.2s;
}
.slicer-btn:hover { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(56,189,248,0.2); }
.slicer-btn:active { transform: translateY(0); }

.kpi-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 14px; margin: 0 28px 18px;
}
.kpi-card {
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px;
  padding: 18px; backdrop-filter: blur(12px); transition: all 0.25s;
}
.kpi-card:hover { border-color: rgba(56,189,248,0.25); transform: translateY(-1px); }
.kpi-card .kpi-label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); margin-bottom: 6px; }
.kpi-card .kpi-value { font-size: 1.5rem; font-weight: 700; }
.kpi-card .kpi-sub { font-size: 0.78rem; color: var(--text-dim); margin-top: 3px; }
.kpi-profit { color: var(--profit); }
.kpi-loss { color: var(--loss); }

.chart-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
  gap: 14px; margin: 0 28px 18px;
}
.chart-card {
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px;
  padding: 18px; backdrop-filter: blur(12px);
}
.chart-card h3 { font-size: 0.82rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); margin-bottom: 12px; }
.chart-wrapper { position: relative; height: 240px; }

.trade-panel {
  margin: 0 28px 28px;
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px;
  padding: 18px; backdrop-filter: blur(12px);
}
.trade-panel h3 { font-size: 0.82rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); margin-bottom: 12px; }
.trade-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
.trade-table th { text-align: left; padding: 9px 10px; color: var(--text-dim); font-weight: 600; text-transform: uppercase; font-size: 0.68rem; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); }
.trade-table td { padding: 9px 10px; border-bottom: 1px solid rgba(255,255,255,0.025); }
.trade-table tr:hover { background: var(--glass); }
.status-filled { color: var(--profit); font-weight: 600; }
.status-failed { color: var(--loss); font-weight: 600; }
.status-partial { color: var(--warn); font-weight: 600; }

.footer {
  text-align: center; padding: 18px; color: var(--text-dim); font-size: 0.72rem;
  border-top: 1px solid var(--border);
}

@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
.live-dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--profit); margin-right:7px; animation:pulse 2.5s infinite; }
.no-data { text-align:center; color:var(--text-dim); padding:30px; font-size:0.85rem; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1><span>ARB</span> v5.2 &mdash; Spatial Arbitrage Command Center</h1>
    <div style="margin-top:5px;font-size:0.78rem;color:var(--text-dim);">
      <span class="live-dot"></span>Live | Uptime: <span id="uptime">0s</span> | Refresh: <span id="refreshLabel">2s</span>
    </div>
  </div>
  <div class="badge closed" id="circuitBadge">CIRCUIT: CLOSED</div>
</div>

<!-- SLICERS -->
<div class="slicer-panel">
  <div class="slicer-group">
    <label>Time Range</label>
    <select id="timeSlicer">
      <option value="all">All Time</option>
      <option value="60">Last 1 Minute</option>
      <option value="300">Last 5 Minutes</option>
      <option value="900" selected>Last 15 Minutes</option>
      <option value="3600">Last 1 Hour</option>
    </select>
  </div>
  <div class="slicer-group">
    <label>Min Profit ($)</label>
    <input type="number" id="profitSlicer" value="0" min="0" step="0.01" placeholder="0.00">
  </div>
  <div class="slicer-group">
    <label>Exchange Pair</label>
    <select id="pairSlicer">
      <option value="all">All Pairs</option>
      <option value="binance_kraken">Binance &rarr; Kraken</option>
      <option value="kraken_binance">Kraken &rarr; Binance</option>
    </select>
  </div>
  <div class="slicer-group">
    <label>Status Filter</label>
    <select id="statusSlicer">
      <option value="all">All Statuses</option>
      <option value="FILLED">FILLED</option>
      <option value="FAILED">FAILED</option>
      <option value="PARTIAL">PARTIAL</option>
    </select>
  </div>
  <div class="slicer-group">
    <label>Refresh Rate</label>
    <select id="refreshSlicer">
      <option value="1000">1 second</option>
      <option value="2000" selected>2 seconds</option>
      <option value="5000">5 seconds</option>
      <option value="10000">10 seconds</option>
    </select>
  </div>
  <button class="slicer-btn" id="applyBtn">Apply Filters</button>
</div>

<!-- KPI CARDS -->
<div class="kpi-grid">
  <div class="kpi-card">
    <div class="kpi-label">Realized P&amp;L</div>
    <div class="kpi-value" id="kpiPnl">$0.00</div>
    <div class="kpi-sub" id="kpiPnlSub">Total profit</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Success Rate</div>
    <div class="kpi-value" id="kpiRate">0.0%</div>
    <div class="kpi-sub" id="kpiRateSub">0 / 0</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Avg Exec Latency</div>
    <div class="kpi-value" id="kpiLat">0 &mu;s</div>
    <div class="kpi-sub" id="kpiLatSub">p50 execution</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Signals / Min</div>
    <div class="kpi-value" id="kpiSig">0</div>
    <div class="kpi-sub" id="kpiSigSub">Detection rate</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Total Trades</div>
    <div class="kpi-value" id="kpiTrades">0</div>
    <div class="kpi-sub" id="kpiTradesSub">Completed</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Binance Spread</div>
    <div class="kpi-value" id="kpiSpread">0 bps</div>
    <div class="kpi-sub" id="kpiSpreadSub">Top-of-book</div>
  </div>
</div>

<!-- CHARTS -->
<div class="chart-grid">
  <div class="chart-card">
    <h3>P&amp;L Over Time</h3>
    <div class="chart-wrapper"><canvas id="pnlChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Execution Latency (&mu;s)</h3>
    <div class="chart-wrapper"><canvas id="latChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Signal Rate</h3>
    <div class="chart-wrapper"><canvas id="sigChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Exchange Spreads (bps)</h3>
    <div class="chart-wrapper"><canvas id="spreadChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Price Divergence</h3>
    <div class="chart-wrapper"><canvas id="priceChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Circuit Breaker State</h3>
    <div class="chart-wrapper"><canvas id="circuitChart"></canvas></div>
  </div>
</div>

<!-- TRADE LOG -->
<div class="trade-panel">
  <h3>Trade Log <span style="float:right;font-weight:400;color:var(--text-dim);font-size:0.72rem;" id="tradeCount">0 trades</span></h3>
  <div style="overflow-x:auto;">
    <table class="trade-table">
      <thead>
        <tr>
          <th>Time</th><th>ID</th><th>Buy</th><th>Sell</th>
          <th>Buy VWAP</th><th>Sell VWAP</th><th>Volume</th>
          <th>Net Profit</th><th>Latency</th><th>Status</th>
        </tr>
      </thead>
      <tbody id="tradeBody">
        <tr><td colspan="10" class="no-data">Waiting for trades...</td></tr>
      </tbody>
    </table>
  </div>
</div>

<div class="footer">Spatial Arbitrage Engine v5.2 | Zero-Gap Architecture | Built for Profit</div>

<script>
// ===================== CHART CONFIG =====================
Chart.defaults.color = '#64748b';
Chart.defaults.borderColor = 'rgba(255,255,255,0.04)';
Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";
Chart.defaults.font.size = 11;

function makeChart(ctx, label, color, fill) {
  return new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [{ label, data: [], borderColor: color, backgroundColor: color + '18', borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.35, fill: fill }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, interaction: { intersect: false, mode: 'index' }, scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } }, y: { grid: { color: 'rgba(255,255,255,0.03)' } } }, animation: { duration: 400 } }
  });
}

const pnlChart = makeChart(document.getElementById('pnlChart'), 'P&L', '#34d399', true);
const latChart = makeChart(document.getElementById('latChart'), 'p50', '#a78bfa', false);
latChart.data.datasets.push({ label: 'p99', data: [], borderColor: '#f87171', backgroundColor: '#f8717118', borderWidth: 1.5, pointRadius: 0, tension: 0.35, fill: false });
const sigChart = makeChart(document.getElementById('sigChart'), 'Detected', '#38bdf8', true);
sigChart.data.datasets.push({ label: 'Executed', data: [], borderColor: '#34d399', backgroundColor: '#34d39918', borderWidth: 2, pointRadius: 0, tension: 0.35, fill: true });
const spreadChart = makeChart(document.getElementById('spreadChart'), 'Binance', '#38bdf8', false);
spreadChart.data.datasets.push({ label: 'Kraken', data: [], borderColor: '#a78bfa', backgroundColor: '#a78bfa18', borderWidth: 2, pointRadius: 0, tension: 0.35, fill: false });
const priceChart = makeChart(document.getElementById('priceChart'), 'Binance', '#38bdf8', false);
priceChart.data.datasets.push({ label: 'Kraken', data: [], borderColor: '#a78bfa', backgroundColor: '#a78bfa18', borderWidth: 2, pointRadius: 0, tension: 0.35, fill: false });
const circuitChart = new Chart(document.getElementById('circuitChart'), {
  type: 'line',
  data: { labels: [], datasets: [{ label: 'Circuit', data: [], borderColor: '#fbbf24', backgroundColor: '#fbbf2418', borderWidth: 2, pointRadius: 0, tension: 0, fill: true, stepped: true }] },
  options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } }, y: { min: -0.1, max: 1.3, grid: { color: 'rgba(255,255,255,0.03)' }, ticks: { callback: v => v >= 0.5 ? 'CLOSED' : 'OPEN', stepSize: 1 } } }, animation: { duration: 300 } }
});

const charts = [pnlChart, latChart, sigChart, spreadChart, priceChart, circuitChart];

// ===================== STATE =====================
let allData = { timestamps: [], realized_pnl: [], exec_latency_p50: [], exec_latency_p99: [], detection_latency_avg: [], signals_detected: [], signals_executed: [], binance_spread_bps: [], kraken_spread_bps: [], binance_mid: [], kraken_mid: [], circuit_state: [] };
let allTrades = [];
let refreshInterval = 2000;
let timer = null;

// ===================== RENDER =====================
function render() {
  const timeVal = document.getElementById('timeSlicer').value;
  const minProfit = parseFloat(document.getElementById('profitSlicer').value) || 0;
  const pairVal = document.getElementById('pairSlicer').value;
  const statusVal = document.getElementById('statusSlicer').value;

  // Slice chart data by time
  let s = 0;
  if (timeVal !== 'all' && allData.timestamps.length > 0) {
    const secs = parseInt(timeVal);
    const points = Math.ceil(secs / 2);
    s = Math.max(0, allData.timestamps.length - points);
  }
  const e = allData.timestamps.length;

  // Update charts
  updateChart(pnlChart, allData.timestamps.slice(s, e), [allData.realized_pnl.slice(s, e)]);
  updateChart(latChart, allData.timestamps.slice(s, e), [allData.exec_latency_p50.slice(s, e), allData.exec_latency_p99.slice(s, e)]);
  updateChart(sigChart, allData.timestamps.slice(s, e), [allData.signals_detected.slice(s, e), allData.signals_executed.slice(s, e)]);
  updateChart(spreadChart, allData.timestamps.slice(s, e), [allData.binance_spread_bps.slice(s, e), allData.kraken_spread_bps.slice(s, e)]);
  updateChart(priceChart, allData.timestamps.slice(s, e), [allData.binance_mid.slice(s, e), allData.kraken_mid.slice(s, e)]);
  updateChart(circuitChart, allData.timestamps.slice(s, e), [allData.circuit_state.slice(s, e)]);

  // Filter & render trades
  let filtered = allTrades.filter(t => {
    const profit = parseFloat(t.net_profit || 0);
    if (profit < minProfit) return false;
    if (pairVal !== 'all') {
      const pair = (t.buy_exchange || '') + '_' + (t.sell_exchange || '');
      if (pair !== pairVal) return false;
    }
    if (statusVal !== 'all') {
      const st = (t.status || '').toUpperCase();
      if (st === 'PARTIAL_BUY_ONLY' || st === 'PARTIAL_SELL_ONLY') {
        if (statusVal !== 'PARTIAL') return false;
      } else if (st !== statusVal) {
        return false;
      }
    }
    return true;
  });
  renderTrades(filtered);
}

function updateChart(chart, labels, datasets) {
  if (!labels.length) return;
  chart.data.labels = labels;
  datasets.forEach((d, i) => { if (chart.data.datasets[i]) chart.data.datasets[i].data = d; });
  chart.update('none');
}

function renderTrades(trades) {
  const tbody = document.getElementById('tradeBody');
  document.getElementById('tradeCount').textContent = trades.length + ' trades';
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="no-data">No trades match current filters</td></tr>';
    return;
  }
  tbody.innerHTML = trades.slice(0, 50).map(t => {
    const profit = parseFloat(t.net_profit || 0);
    const profitClass = profit >= 0 ? 'kpi-profit' : 'kpi-loss';
    let statusClass = 'status-partial';
    const st = (t.status || '').toUpperCase();
    if (st === 'FILLED') statusClass = 'status-filled';
    else if (st === 'FAILED') statusClass = 'status-failed';
    const ts = t.timestamp ? t.timestamp.split('T')[1].split('.')[0] : '-';
    return '<tr>' +
      '<td>' + ts + '</td>' +
      '<td>' + (t.execution_id ? t.execution_id.slice(-8) : '-') + '</td>' +
      '<td>' + (t.buy_exchange || '-') + '</td>' +
      '<td>' + (t.sell_exchange || '-') + '</td>' +
      '<td>$' + (t.buy_vwap || '0') + '</td>' +
      '<td>$' + (t.sell_vwap || '0') + '</td>' +
      '<td>' + (t.target_volume || '0') + ' BTC</td>' +
      '<td class="' + profitClass + '">$' + profit.toFixed(2) + '</td>' +
      '<td>' + (t.latency_us || 0) + ' &mu;s</td>' +
      '<td class="' + statusClass + '">' + (t.status || '-') + '</td>' +
      '</tr>';
  }).join('');
}

// ===================== FETCH =====================
async function fetchData() {
  try {
    const [mRes, tRes] = await Promise.all([fetch('/metrics'), fetch('/api/trades')]);
    if (!mRes.ok || !tRes.ok) throw new Error('HTTP error');
    const m = await mRes.json();
    const t = await tRes.json();

    // Update KPIs
    const pnl = parseFloat(m.pnl?.total_realized_pnl || 0);
    const pnlEl = document.getElementById('kpiPnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
    pnlEl.className = 'kpi-value ' + (pnl >= 0 ? 'kpi-profit' : 'kpi-loss');

    const rate = m.engine?.success_rate || 0;
    document.getElementById('kpiRate').textContent = rate.toFixed(1) + '%';
    document.getElementById('kpiRateSub').textContent = (m.engine?.signals_executed || 0) + ' / ' + (m.engine?.signals_detected || 0);

    const lat = m.latency?.execution_us?.p50 || 0;
    document.getElementById('kpiLat').textContent = Math.round(lat) + ' μs';

    const sigMin = m.engine?.signals_detected ? Math.round(m.engine.signals_detected / Math.max(m.engine.uptime_sec / 60, 1)) : 0;
    document.getElementById('kpiSig').textContent = sigMin;

    document.getElementById('kpiTrades').textContent = m.pnl?.trade_count || 0;

    const bSpread = m.charts?.binance_spread_bps?.length ? m.charts.binance_spread_bps[m.charts.binance_spread_bps.length - 1] : 0;
    document.getElementById('kpiSpread').textContent = bSpread.toFixed(2) + ' bps';

    // Circuit badge
    const circ = m.circuit?.state || 'CLOSED';
    const badge = document.getElementById('circuitBadge');
    badge.textContent = 'CIRCUIT: ' + circ;
    badge.className = 'badge ' + (circ === 'CLOSED' ? 'closed' : 'open');

    // Uptime
    const up = Math.floor(m.engine?.uptime_sec || 0);
    document.getElementById('uptime').textContent = Math.floor(up / 60) + 'm ' + (up % 60) + 's';

    // Store data
    allData = m.charts || allData;
    allTrades = t.trades || [];

    render();
  } catch (e) {
    console.error('Fetch error:', e);
  }
}

// ===================== EVENTS =====================
document.getElementById('applyBtn').addEventListener('click', render);
document.getElementById('refreshSlicer').addEventListener('change', function() {
  refreshInterval = parseInt(this.value);
  document.getElementById('refreshLabel').textContent = (refreshInterval / 1000) + 's';
  clearInterval(timer);
  timer = setInterval(fetchData, refreshInterval);
});

// ===================== INIT =====================
timer = setInterval(fetchData, refreshInterval);
fetchData();
</script>
</body>
</html>"""

    async def shutdown(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        LOG.info("[HEALTH] Shutdown.")

# =============================================================================
# EXCHANGE WEBSOCKET MANAGER (LIVE)
# =============================================================================

class ExchangeWebSocketManager:
    def __init__(self, config, symbol, reconnect_base_delay_ms=100, reconnect_max_delay_ms=30000):
        self.config = config
        self.symbol = symbol
        self.exchange_id = config.exchange_id
        self._reconnect_base_delay_ms = reconnect_base_delay_ms
        self._reconnect_max_delay_ms = reconnect_max_delay_ms
        self._consecutive_failures = 0
        self._exchange = None
        self._is_connected = False
        self._shutdown_event = asyncio.Event()
        self._order_book = None
        self._lock = asyncio.Lock()
        self._messages_received = 0
        self._last_message_ns = 0

    async def _create_exchange(self):
        if ccxtpro is None:
            raise RuntimeError("ccxt not installed")
        exchange_class = getattr(ccxtpro, self.exchange_id.value, None)
        if exchange_class is None:
            raise RuntimeError("Exchange %s not supported" % self.exchange_id.value)
        instance = exchange_class({
            'apiKey': self.config.api_key,
            'secret': self.config.api_secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        if self.config.sandbox:
            try:
                instance.set_sandbox_mode(True)
            except Exception as e:
                LOG.warning("[%s] Sandbox unavailable: %s" % (self.exchange_id.value, e))
        return instance

    async def connect(self):
        while not self._shutdown_event.is_set():
            try:
                LOG.info("[%s] Connecting..." % self.exchange_id.value)
                self._exchange = await self._create_exchange()
                await self._exchange.load_markets()
                self._is_connected = True
                self._consecutive_failures = 0
                LOG.info("[%s] Connected." % self.exchange_id.value)
                await self._consume_order_book()
            except Exception as e:
                self._consecutive_failures += 1
                self._is_connected = False
                delay_ms = min(
                    self._reconnect_base_delay_ms * (2 ** (self._consecutive_failures - 1)),
                    self._reconnect_max_delay_ms
                )
                LOG.error("[%s] Fail #%d: %s. Reconnect %dms..." % (
                    self.exchange_id.value, self._consecutive_failures, e, delay_ms))
                if self._exchange:
                    try:
                        await self._exchange.close()
                    except:
                        pass
                self._exchange = None
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=delay_ms / 1000.0
                    )
                except asyncio.TimeoutError:
                    pass

    async def _consume_order_book(self):
        if self._exchange is None:
            return
        try:
            while not self._shutdown_event.is_set():
                try:
                    ob = await self._exchange.watch_order_book(self.symbol)
                    now_ns = time.time_ns()
                    self._last_message_ns = now_ns
                    self._messages_received += 1
                    async with self._lock:
                        self._order_book = self._normalize_order_book(ob, now_ns)
                except Exception as e:
                    LOG.error("[%s] Stream: %s" % (self.exchange_id.value, e))
                    raise
        except asyncio.CancelledError:
            LOG.info("[%s] Cancelled." % self.exchange_id.value)
            raise
        except Exception as e:
            LOG.error("[%s] Fatal: %s" % (self.exchange_id.value, e))
            raise

    def _normalize_order_book(self, raw_ob, timestamp_ns):
        bids = [OrderBookLevel(level[0], level[1]) for level in raw_ob.get('bids', [])]
        asks = [OrderBookLevel(level[0], level[1]) for level in raw_ob.get('asks', [])]
        return OrderBookSnapshot(self.exchange_id, self.symbol, timestamp_ns, bids, asks)

    async def get_order_book(self):
        async with self._lock:
            return self._order_book

    @property
    def is_connected(self):
        return self._is_connected

    @property
    def messages_received(self):
        return self._messages_received

    async def shutdown(self):
        LOG.info("[%s] Shutdown." % self.exchange_id.value)
        self._shutdown_event.set()
        self._is_connected = False
        if self._exchange:
            try:
                await self._exchange.close()
            except Exception as e:
                LOG.warning("[%s] Close err: %s" % (self.exchange_id.value, e))
        self._exchange = None


# =============================================================================
# SYNTHETIC EXCHANGE MANAGER
# =============================================================================

class SyntheticExchangeManager:
    def __init__(self, exchange_id, symbol, base_price, volatility=0.002, arb_inject_prob=0.05):
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.base_price = base_price
        self.volatility = volatility
        self.arb_inject_prob = arb_inject_prob
        self._current_price = base_price
        self._order_book = None
        self._lock = asyncio.Lock()
        self._is_connected = True
        self._messages_received = 0
        self._shutdown_event = asyncio.Event()
        self._task = None
        self._arb_bias = 0.0

    async def connect(self):
        LOG.info("[%s] Synthetic generator starting..." % self.exchange_id.value)
        self._task = asyncio.create_task(self._generate_loop())
        try:
            await self._task
        except asyncio.CancelledError:
            LOG.info("[%s] Synthetic stopped." % self.exchange_id.value)

    async def _generate_loop(self):
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=0.1
                )
            except asyncio.TimeoutError:
                await self._generate_snapshot()

    async def _generate_snapshot(self):
        now_ns = time.time_ns()
        drift = (self.base_price - self._current_price) * 0.01
        noise = random.gauss(0, self.volatility * self._current_price)
        if random.random() < self.arb_inject_prob:
            self._arb_bias = random.choice([-0.008, 0.008])
        else:
            self._arb_bias *= 0.95
        self._current_price += drift + noise + (self._arb_bias * self._current_price)
        self._current_price = max(self._current_price, 1000.0)
        price = Decimal(str(round(self._current_price, 2)))
        bids = []
        asks = []
        for i in range(20):
            bp = price * (Decimal("1") - Decimal(str(0.0001 * (i + 1))))
            ap = price * (Decimal("1") + Decimal(str(0.0001 * (i + 1))))
            bv = Decimal(str(round(random.uniform(0.5, 5.0), 4)))
            av = Decimal(str(round(random.uniform(0.5, 5.0), 4)))
            bids.append(OrderBookLevel(bp.quantize(Decimal("0.01")), bv))
            asks.append(OrderBookLevel(ap.quantize(Decimal("0.01")), av))
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        async with self._lock:
            self._order_book = OrderBookSnapshot(
                self.exchange_id, self.symbol, now_ns, bids, asks)
            self._messages_received += 1

    async def get_order_book(self):
        async with self._lock:
            return self._order_book

    @property
    def is_connected(self):
        return self._is_connected and not self._shutdown_event.is_set()

    @property
    def messages_received(self):
        return self._messages_received

    async def shutdown(self):
        LOG.info("[%s] Synthetic shutdown." % self.exchange_id.value)
        self._shutdown_event.set()
        self._is_connected = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


# =============================================================================
# CONCURRENT EXECUTION ROUTER
# =============================================================================

class ExecutionRouter:
    def __init__(self, sandbox=True):
        self.sandbox = sandbox
        self._execution_history = []
        self._lock = asyncio.Lock()

    async def execute_arbitrage(self, signal, exchanges):
        start_ns = time.time_ns()
        eid = signal.execution_id
        LOG.info("[EXEC] %s | BUY %s@%s | SELL %s@%s | Net:%s" % (
            eid, signal.buy_exchange.value, signal.buy_vwap,
            signal.sell_exchange.value, signal.sell_vwap, signal.net_profit))

        buy_task = self._place_order(
            exchanges[signal.buy_exchange], TradeSide.BUY,
            signal.symbol, signal.target_volume, eid)
        sell_task = self._place_order(
            exchanges[signal.sell_exchange], TradeSide.SELL,
            signal.symbol, signal.target_volume, eid)
        buy_res, sell_res = await asyncio.gather(
            buy_task, sell_task, return_exceptions=True)

        end_ns = time.time_ns()
        latency_us = (end_ns - start_ns) // 1000

        buy_ok = not isinstance(buy_res, Exception)
        sell_ok = not isinstance(sell_res, Exception)

        if buy_ok and sell_ok:
            status = "FILLED"
            err = None
        elif buy_ok and not sell_ok:
            status = "PARTIAL_BUY_ONLY"
            err = "Sell failed: %s" % sell_res
        elif not buy_ok and sell_ok:
            status = "PARTIAL_SELL_ONLY"
            err = "Buy failed: %s" % buy_res
        else:
            status = "FAILED"
            err = "Buy:%s | Sell:%s" % (buy_res, sell_res)

        result = ExecutionResult(
            execution_id=eid,
            timestamp_ns=end_ns,
            buy_exchange=signal.buy_exchange,
            sell_exchange=signal.sell_exchange,
            buy_order_id=buy_res.get('id') if buy_ok else None,
            sell_order_id=sell_res.get('id') if sell_ok else None,
            buy_fill_price=Decimal(str(buy_res.get('average', buy_res.get('price', 0)))) if buy_ok else None,
            sell_fill_price=Decimal(str(sell_res.get('average', sell_res.get('price', 0)))) if sell_ok else None,
            filled_volume=signal.target_volume if status == "FILLED" else Decimal("0"),
            status=status,
            latency_us=latency_us,
            error_message=err)

        async with self._lock:
            self._execution_history.append(result)
        LOG.info("[EXEC] %s done in %dus | %s" % (eid, latency_us, status))
        return result

    async def _place_order(self, manager, side, symbol, volume, eid):
        is_synthetic = hasattr(manager, '_generate_snapshot')
        if is_synthetic:
            await asyncio.sleep(0.0001)
            book = await manager.get_order_book()
            if book is None:
                raise RuntimeError("No synthetic book")
            fp = book.best_ask if side == TradeSide.BUY else book.best_bid
            return {
                'id': "SIM-%s-%s" % (eid, side.value.upper()),
                'symbol': symbol,
                'side': side.value,
                'type': 'market',
                'amount': float(volume),
                'price': float(fp),
                'average': float(fp),
                'status': 'closed',
                'filled': float(volume),
                'remaining': 0.0,
                'cost': float(volume * fp),
                'fee': None,
                'trades': []
            }

        exchange = manager._exchange
        if exchange is None or not manager.is_connected:
            raise ConnectionError("[%s] Not connected" % manager.exchange_id.value)

        if self.sandbox:
            await asyncio.sleep(0.0001)
            book = await manager.get_order_book()
            fp = book.best_ask if side == TradeSide.BUY else book.best_bid
            return {
                'id': "SIM-%s-%s" % (eid, side.value.upper()),
                'symbol': symbol,
                'side': side.value,
                'type': 'market',
                'amount': float(volume),
                'price': float(fp),
                'average': float(fp),
                'status': 'closed',
                'filled': float(volume),
                'remaining': 0.0,
                'cost': float(volume * fp),
                'fee': None,
                'trades': []
            }
        else:
            return await exchange.create_order(
                symbol=symbol, type='market', side=side.value, amount=float(volume))


# =============================================================================
# MICROSECOND PERSISTENCE LAYER
# =============================================================================

class DataSink:
    def __init__(self, db_path="arbitrage_engine.db", csv_path="arbitrage_signals.csv", flush_interval_seconds=60):
        self.db_path = db_path
        self.csv_path = csv_path
        self.flush_interval_seconds = flush_interval_seconds
        self._conn = None
        self._cursor = None
        self._flush_task = None
        self._shutdown_event = asyncio.Event()

    def initialize(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._cursor = self._conn.cursor()
        if self.db_path != ":memory:":
            self._cursor.execute("PRAGMA journal_mode=WAL;")
            self._cursor.execute("PRAGMA synchronous=NORMAL;")
            self._cursor.execute("PRAGMA temp_store=MEMORY;")
            self._cursor.execute("""
                CREATE TABLE IF NOT EXISTS arbitrage_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ns INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    buy_exchange TEXT NOT NULL,
                    sell_exchange TEXT NOT NULL,
                    buy_vwap TEXT NOT NULL,
                    sell_vwap TEXT NOT NULL,
                    target_volume TEXT NOT NULL,
                    gross_spread TEXT NOT NULL,
                    buy_fee TEXT NOT NULL,
                    sell_fee TEXT NOT NULL,
                    gas_fee TEXT NOT NULL,
                    net_profit TEXT NOT NULL,
                    net_profit_pct TEXT NOT NULL,
                    execution_id TEXT UNIQUE NOT NULL,
                    executed INTEGER DEFAULT 0,
                    execution_latency_us INTEGER,
                    status TEXT)
            """)
            self._cursor.execute("CREATE INDEX IF NOT EXISTS idx_ts ON arbitrage_signals(timestamp_ns)")
            self._cursor.execute("CREATE INDEX IF NOT EXISTS idx_exec ON arbitrage_signals(execution_id)")
            self._conn.commit()
            LOG.info("[DATASINK] SQLite at '%s'." % self.db_path)

    def persist_signal(self, signal):
        if self._cursor is None:
            raise RuntimeError("Not initialized")
        self._cursor.execute("""
            INSERT OR REPLACE INTO arbitrage_signals (
                timestamp_ns, received_at, symbol, buy_exchange, sell_exchange,
                buy_vwap, sell_vwap, target_volume, gross_spread, buy_fee,
                sell_fee, gas_fee, net_profit, net_profit_pct, execution_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.timestamp_ns,
            datetime.now(timezone.utc).isoformat(),
            signal.symbol,
            signal.buy_exchange.value,
            signal.sell_exchange.value,
            str(signal.buy_vwap),
            str(signal.sell_vwap),
            str(signal.target_volume),
            str(signal.gross_spread),
            str(signal.buy_fee),
            str(signal.sell_fee),
            str(signal.gas_fee),
            str(signal.net_profit),
            str(signal.net_profit_pct),
            signal.execution_id))
        self._conn.commit()

    def update_execution_result(self, result):
        if self._cursor is None:
            return
        self._cursor.execute(
            "UPDATE arbitrage_signals SET executed=1, execution_latency_us=?, status=? WHERE execution_id=?",
            (result.latency_us, result.status, result.execution_id))
        self._conn.commit()

    async def start_background_flush(self):
        self._flush_task = asyncio.create_task(self._background_flush_loop())
        LOG.info("[DATASINK] Background flush started.")

    async def _background_flush_loop(self):
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.flush_interval_seconds)
            except asyncio.TimeoutError:
                await self._flush_to_csv()

    async def _flush_to_csv(self):
        if self._cursor is None:
            return
        try:
            self._cursor.execute("SELECT * FROM arbitrage_signals ORDER BY timestamp_ns DESC")
            rows = self._cursor.fetchall()
            columns = [desc[0] for desc in self._cursor.description]
            if not rows:
                return
            async with aiofiles.open(self.csv_path, mode='w', newline='') as f:
                await f.write(','.join(columns) + '\n')
                for row in rows:
                    escaped = []
                    for val in row:
                        if val is None:
                            escaped.append('')
                        elif isinstance(val, str) and (',' in val or '"' in val or '\n' in val):
                            escaped.append('"' + val.replace('"', '""') + '"')
                        else:
                            escaped.append(str(val))
                    await f.write(','.join(escaped) + '\n')
            LOG.info("[DATASINK] Flushed %d records to %s" % (len(rows), self.csv_path))
        except Exception as e:
            LOG.error("[DATASINK] Flush error: %s" % e)

    async def shutdown(self):
        LOG.info("[DATASINK] Shutdown. Final flush...")
        self._shutdown_event.set()
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_to_csv()
        if self._conn:
            self._conn.close()
            LOG.info("[DATASINK] Closed.")

# =============================================================================
# MAIN ARBITRAGE ENGINE ORCHESTRATOR
# =============================================================================

class ArbitrageEngine:
    def __init__(self, arb_config, exchange_configs, data_sink, execution_router,
                 risk_config=None, poll_interval_ms=10.0, demo_mode=False,
                 webhook_url=None, health_port=8080):
        self.config = arb_config
        self.data_sink = data_sink
        self.execution_router = execution_router
        self.poll_interval_ms = poll_interval_ms
        self.demo_mode = demo_mode
        self.risk_config = risk_config or RiskConfig()

        self.circuit_breaker = CircuitBreaker(self.risk_config)
        self.latency_tracker = LatencyTracker(window_size=1000)
        self.ledger = ProfitLossLedger(arb_config.symbol)
        self.alert_manager = AlertManager(webhook_url)
        self.metrics_history = MetricsHistory(max_points=300)
        self.health_server = HealthServer(port=health_port)
        self.health_server.attach_engine(self)

        self.exchanges = {}
        for ec in exchange_configs:
            if demo_mode:
                bp = 65000.0 if ec.exchange_id == ExchangeId.BINANCE else 65200.0
                self.exchanges[ec.exchange_id] = SyntheticExchangeManager(
                    ec.exchange_id, arb_config.symbol, bp, 0.002, 0.05)
            else:
                self.exchanges[ec.exchange_id] = ExchangeWebSocketManager(ec, arb_config.symbol)

        if len(self.exchanges) != 2:
            raise ValueError("Need exactly 2 exchanges.")

        self._shutdown_event = asyncio.Event()
        self._tasks = []
        self._signals_detected = 0
        self._signals_executed = 0
        self._start_time_ns = time.time_ns()

    async def start(self):
        LOG.info("=" * 70)
        LOG.info("ARBITRAGE ENGINE v5.2 | Mode: %s" % ("DEMO" if self.demo_mode else "LIVE"))
        LOG.info("Symbol: %s | Vol: %s | Threshold: %s" % (
            self.config.symbol, self.config.target_volume, self.config.min_yield_threshold))
        LOG.info("Risk: max_trades=%d | max_loss=%s | cooldown=%ds" % (
            self.risk_config.max_daily_trades,
            self.risk_config.max_daily_loss,
            self.risk_config.cooldown_seconds_after_failure))
        LOG.info("Dashboard: http://0.0.0.0:%d/status" % self.health_server.port)
        LOG.info("=" * 70)

        self.data_sink.initialize()
        await self.data_sink.start_background_flush()
        await self.ledger.initialize_positions(list(self.exchanges.keys()))
        await self.health_server.start()

        self._tasks.extend([
            asyncio.create_task(ex.connect(), name="Conn-%s" % ex.exchange_id.value)
            for ex in self.exchanges.values()
        ])
        await asyncio.sleep(1)

        self._tasks.append(asyncio.create_task(self._detection_loop(), name="Detector"))
        self._tasks.append(asyncio.create_task(self._status_reporter(), name="Reporter"))
        self._tasks.append(asyncio.create_task(self._mark_to_market_loop(), name="MTM"))
        self._tasks.append(asyncio.create_task(self._metrics_snapshot_loop(), name="Metrics"))

        LOG.info("[ENGINE] Online.")
        await self._shutdown_event.wait()

    async def _detection_loop(self):
        ex_list = list(self.exchanges.values())
        ex_a, ex_b = ex_list[0], ex_list[1]
        ex_a_id, ex_b_id = ex_a.exchange_id, ex_b.exchange_id

        fee_map = {}
        for ec in [
            ExchangeConfig(ExchangeId.BINANCE, "", "", Decimal("0.001")),
            ExchangeConfig(ExchangeId.KRAKEN, "", "", Decimal("0.0026"))
        ]:
            if ec.exchange_id in self.exchanges:
                fee_map[ec.exchange_id] = ec.taker_fee_rate

        while not self._shutdown_event.is_set():
            loop_start = time.time_ns()

            if not await self.circuit_breaker.can_trade():
                await asyncio.sleep(1.0)
                continue
            if not await self.circuit_breaker.check_daily_limits():
                await asyncio.sleep(1.0)
                continue

            try:
                book_a = await ex_a.get_order_book()
                book_b = await ex_b.get_order_book()
                if book_a is None or book_b is None:
                    await asyncio.sleep(self.poll_interval_ms / 1000.0)
                    continue

                sig_ab = PrecisionCalculator.validate_signal(
                    book_a, book_b, self.config,
                    fee_map.get(ex_a_id, Decimal("0.001")),
                    fee_map.get(ex_b_id, Decimal("0.0026")))
                if sig_ab:
                    await self.latency_tracker.record_detection(
                        (time.time_ns() - loop_start) // 1000)
                    await self._process_signal(sig_ab)
                    continue

                sig_ba = PrecisionCalculator.validate_signal(
                    book_b, book_a, self.config,
                    fee_map.get(ex_b_id, Decimal("0.0026")),
                    fee_map.get(ex_a_id, Decimal("0.001")))
                if sig_ba:
                    await self.latency_tracker.record_detection(
                        (time.time_ns() - loop_start) // 1000)
                    await self._process_signal(sig_ba)
                    continue

            except Exception as e:
                LOG.error("[DETECTOR] %s\n%s" % (e, traceback.format_exc()))
                await self.circuit_breaker.record_failure(str(e))

            elapsed = time.time_ns() - loop_start
            sleep_ns = max(0, int(self.poll_interval_ms * 1_000_000) - elapsed)
            if sleep_ns > 0:
                await asyncio.sleep(sleep_ns / 1_000_000_000.0)

    async def _process_signal(self, signal):
        self._signals_detected += 1
        self.data_sink.persist_signal(signal)
        LOG.info("[SIGNAL] #%d | %s | Net: %s %s" % (
            self._signals_detected, signal.execution_id,
            signal.net_profit, signal.symbol.split('/')[1]))
        try:
            result = await self.execution_router.execute_arbitrage(signal, self.exchanges)
            await self.latency_tracker.record_execution(result.latency_us)
            self.data_sink.update_execution_result(result)
            await self.ledger.record_execution(result, signal)
            await self.alert_manager.alert_execution(signal, result)

            if result.status == "FILLED":
                await self.circuit_breaker.record_success(signal.net_profit)
                self._signals_executed += 1
                LOG.info("[SIGNAL] #%d EXECUTED in %dus" % (
                    self._signals_detected, result.latency_us))
            else:
                await self.circuit_breaker.record_failure(result.status)
                LOG.warning("[SIGNAL] #%d issue: %s" % (
                    self._signals_detected, result.status))
        except Exception as e:
            LOG.error("[SIGNAL] Exec failed: %s" % e)
            await self.circuit_breaker.record_failure(str(e))
            await self.alert_manager.alert_error(str(e))

    async def _mark_to_market_loop(self):
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                books = {
                    ex_id: await m.get_order_book()
                    for ex_id, m in self.exchanges.items()
                }
                books = {k: v for k, v in books.items() if v}
                if books:
                    await self.ledger.mark_to_market(books)

    async def _metrics_snapshot_loop(self):
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                await self.metrics_history.snapshot(self)

    async def _status_reporter(self):
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                for ex_id, m in self.exchanges.items():
                    LOG.info("[HEALTH] %s: %s | Msgs:%d" % (
                        ex_id.value,
                        "CONN" if m.is_connected else "DISC",
                        m.messages_received))
                rate = self._signals_executed / max(self._signals_detected, 1) * 100
                lat = self.latency_tracker.get_metrics()
                exec_p99 = lat["execution_us"]["p99"]
                LOG.info("[METRICS] Sig:%d | Exec:%d | Rate:%.1f%% | Exec p99:%.0fus | P&L:%s" % (
                    self._signals_detected,
                    self._signals_executed,
                    rate,
                    exec_p99,
                    self.ledger.get_metrics()["total_realized_pnl"]))

    async def shutdown(self):
        LOG.info("[ENGINE] Shutdown...")
        self._shutdown_event.set()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for m in self.exchanges.values():
            await m.shutdown()
        await self.data_sink.shutdown()
        await self.health_server.shutdown()
        LOG.info("[ENGINE] Done.")


# =============================================================================
# SIGNAL HANDLERS & MAIN
# =============================================================================

class SignalHandler:
    def __init__(self, engine):
        self.engine = engine
        self._received = False

    def register(self):
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._handle, sig)
            LOG.info("[SIGNAL] Handlers registered.")
        except NotImplementedError:
            LOG.warning("[SIGNAL] Not supported on this platform.")

    def _handle(self, sig):
        if self._received:
            LOG.warning("[SIGNAL] Forced exit.")
            sys.exit(1)
        self._received = True
        LOG.info("[SIGNAL] %s received. Graceful shutdown..." % signal.Signals(sig).name)
        asyncio.create_task(self.engine.shutdown())


async def main():
    parser = argparse.ArgumentParser(description="Spatial Arbitrage Engine v5.2")
    parser.add_argument("--live", action="store_true", help="LIVE mode (needs API keys)")
    parser.add_argument("--duration", type=int, default=120, help="Demo duration in seconds")
    parser.add_argument("--db", default="arbitrage_engine.db", help="SQLite path")
    parser.add_argument("--csv", default="arbitrage_signals.csv", help="CSV path")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard port")
    parser.add_argument("--webhook", default=None, help="Webhook URL for alerts")
    args = parser.parse_args()

    demo_mode = not args.live

    arb_config = ArbitrageConfig(
        symbol="BTC/USDT",
        target_volume=Decimal("0.01"),
        min_yield_threshold=Decimal("5.00"),
        estimated_gas_fee=Decimal("2.50"),
        max_slippage_tolerance=Decimal("0.005"),
        vwap_depth_limit=20)

    risk_config = RiskConfig(
        max_daily_trades=1000,
        max_daily_loss=Decimal("1000.00"),
        max_consecutive_failures=5,
        cooldown_seconds_after_failure=30,
        circuit_breaker_drawdown_pct=Decimal("0.05"))

    if demo_mode:
        LOG.info("[MAIN] DEMO MODE — no API keys needed.")
        exchange_configs = [
            ExchangeConfig(ExchangeId.BINANCE, "demo", "demo", Decimal("0.001"), True),
            ExchangeConfig(ExchangeId.KRAKEN, "demo", "demo", Decimal("0.0026"), True)]
    else:
        LOG.info("[MAIN] LIVE MODE.")
        exchange_configs = [
            ExchangeConfig(
                ExchangeId.BINANCE,
                os.environ.get("BINANCE_API_KEY", ""),
                os.environ.get("BINANCE_API_SECRET", ""),
                Decimal("0.001"), True),
            ExchangeConfig(
                ExchangeId.KRAKEN,
                os.environ.get("KRAKEN_API_KEY", ""),
                os.environ.get("KRAKEN_API_SECRET", ""),
                Decimal("0.0026"), True)]

    data_sink = DataSink(
        db_path=args.db,
        csv_path=args.csv,
        flush_interval_seconds=10)
    execution_router = ExecutionRouter(sandbox=True)

    engine = ArbitrageEngine(
        arb_config=arb_config,
        exchange_configs=exchange_configs,
        data_sink=data_sink,
        execution_router=execution_router,
        risk_config=risk_config,
        poll_interval_ms=10.0,
        demo_mode=demo_mode,
        webhook_url=args.webhook,
        health_port=args.port)

    SignalHandler(engine).register()

    if demo_mode:
        async def auto_shutdown():
            await asyncio.sleep(args.duration)
            LOG.info("[MAIN] Demo %ds complete." % args.duration)
            await engine.shutdown()
        asyncio.create_task(auto_shutdown())

    try:
        await engine.start()
    except Exception as e:
        LOG.critical("[MAIN] Fatal: %s\n%s" % (e, traceback.format_exc()))
        await engine.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOG.info("[MAIN] Interrupted.")
        sys.exit(0)
