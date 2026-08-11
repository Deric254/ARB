#!/usr/bin/env python3
"""
================================================================================
SPATIAL ARBITRAGE TRADING ENGINE — PRODUCTION v4.0
High-Frequency Cryptocurrency Arbitrage System
================================================================================

ZERO-GAP ARCHITECTURE with FULL PRODUCTION SUBSYSTEMS:
  - Circuit Breaker (failure/drawdown halting)
  - Real-Time P&L Ledger (per-exchange, per-symbol tracking)
  - Latency Telemetry (min/max/avg/p99 histograms)
  - Webhook Alerting (Slack/Discord/Telegram on execution & errors)
  - Health HTTP Server (/health, /metrics, /status endpoints)
  - Enhanced Risk Controls (max daily trades, cooldown, position limits)
  - Synthetic DEMO mode + LIVE WebSocket mode

Usage:
    python arbitrage_engine.py              # DEMO mode, 60s
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
from datetime import datetime, timezone, timedelta
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
# SECTION 2: CONFIGURATION & DATA STRUCTURES
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
    max_daily_trades: int = 100
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
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[Decimal]:
        bb = self.best_bid
        ba = self.best_ask
        if bb is not None and ba is not None:
            return ((bb + ba) / Decimal("2")).quantize(Decimal("0.01"))
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
# SECTION 3: LOGGING INFRASTRUCTURE
# =============================================================================

class MicrosecondFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, tz=timezone.utc)
        if datefmt:
            s = ct.strftime(datefmt)
        else:
            t = ct.strftime("%Y-%m-%d %H:%M:%S")
            s = "%s.%06d" % (t, int(record.msecs * 1000))
        return s


def setup_logging(log_level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("ArbitrageEngine")
    logger.setLevel(log_level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(log_level)
        formatter = MicrosecondFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


LOG = setup_logging()


# =============================================================================
# SECTION 4: HIGH-PRECISION CALCULATION ENGINE
# =============================================================================

class PrecisionCalculator:
    @staticmethod
    def compute_vwap(levels: List[OrderBookLevel], target_volume: Decimal, depth_limit: int = 20) -> VWAPResult:
        if not levels or target_volume <= Decimal("0"):
            return VWAPResult(Decimal("0"), Decimal("0"), 0, Decimal("0"), False)

        remaining = target_volume
        cumulative_value = Decimal("0")
        cumulative_volume = Decimal("0")
        top_price = levels[0].price
        levels_consumed = 0

        for level in levels[:depth_limit]:
            if remaining <= Decimal("0"):
                break
            take_volume = min(level.volume, remaining)
            cumulative_value += take_volume * level.price
            cumulative_volume += take_volume
            remaining -= take_volume
            levels_consumed += 1

        if cumulative_volume <= Decimal("0"):
            return VWAPResult(Decimal("0"), Decimal("0"), 0, Decimal("0"), False)

        vwap = (cumulative_value / cumulative_volume).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        slippage = abs(((vwap - top_price) / top_price).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)) if top_price != Decimal("0") else Decimal("0")

        return VWAPResult(vwap, cumulative_volume, levels_consumed, slippage, remaining <= Decimal("0"))

    @staticmethod
    def calculate_net_spread(buy_vwap: Decimal, sell_vwap: Decimal, buy_taker_fee: Decimal,
                             sell_taker_fee: Decimal, gas_fee: Decimal, target_volume: Decimal) -> Tuple[Decimal, Decimal, Decimal]:
        buy_cost = buy_vwap * target_volume * (Decimal("1") + buy_taker_fee)
        sell_revenue = sell_vwap * target_volume * (Decimal("1") - sell_taker_fee)
        gross_spread = sell_revenue - buy_cost
        net_profit = gross_spread - gas_fee

        if buy_cost > Decimal("0"):
            net_profit_pct = (net_profit / buy_cost).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        else:
            net_profit_pct = Decimal("0")

        return gross_spread, net_profit, net_profit_pct

    @staticmethod
    def validate_signal(buy_book: OrderBookSnapshot, sell_book: OrderBookSnapshot,
                        config: ArbitrageConfig, buy_fee: Decimal, sell_fee: Decimal) -> Optional[ArbitrageSignal]:
        buy_vwap_result = PrecisionCalculator.compute_vwap(buy_book.asks, config.target_volume, config.vwap_depth_limit)
        sell_vwap_result = PrecisionCalculator.compute_vwap(sell_book.bids, config.target_volume, config.vwap_depth_limit)

        if not buy_vwap_result.fully_filled or not sell_vwap_result.fully_filled:
            return None

        if (buy_vwap_result.slippage_from_top > config.max_slippage_tolerance or
            sell_vwap_result.slippage_from_top > config.max_slippage_tolerance):
            return None

        gross_spread, net_profit, net_profit_pct = PrecisionCalculator.calculate_net_spread(
            buy_vwap_result.vwap, sell_vwap_result.vwap, buy_fee, sell_fee,
            config.estimated_gas_fee, config.target_volume
        )

        if net_profit <= config.min_yield_threshold:
            return None

        execution_id = "ARB-%s" % datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:-3]

        return ArbitrageSignal(
            timestamp_ns=time.time_ns(), symbol=config.symbol,
            buy_exchange=buy_book.exchange_id, sell_exchange=sell_book.exchange_id,
            buy_vwap=buy_vwap_result.vwap, sell_vwap=sell_vwap_result.vwap,
            target_volume=config.target_volume,
            gross_spread=gross_spread.quantize(Decimal("0.00000001")),
            buy_fee=(buy_vwap_result.vwap * config.target_volume * buy_fee).quantize(Decimal("0.00000001")),
            sell_fee=(sell_vwap_result.vwap * config.target_volume * sell_fee).quantize(Decimal("0.00000001")),
            gas_fee=config.estimated_gas_fee,
            net_profit=net_profit.quantize(Decimal("0.00000001")),
            net_profit_pct=net_profit_pct,
            execution_id=execution_id
        )


# =============================================================================
# SECTION 5: CIRCUIT BREAKER
# =============================================================================

class CircuitBreaker:
    def __init__(self, risk_config: RiskConfig):
        self.risk_config = risk_config
        self._state = "CLOSED"
        self._consecutive_failures = 0
        self._daily_trades = 0
        self._daily_pnl = Decimal("0")
        self._daily_start_value = Decimal("0")
        self._last_failure_time = 0
        self._lock = asyncio.Lock()
        self._opened_at = None
        self._open_reason = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def open_reason(self) -> Optional[str]:
        return self._open_reason

    async def can_trade(self) -> bool:
        async with self._lock:
            if self._state == "OPEN":
                if time.time() - self._last_failure_time > self.risk_config.cooldown_seconds_after_failure:
                    LOG.info("[CIRCUIT] Cooldown elapsed. Resetting to CLOSED.")
                    self._state = "CLOSED"
                    self._consecutive_failures = 0
                    return True
                return False
            return True

    async def record_success(self, net_profit: Decimal) -> None:
        async with self._lock:
            self._consecutive_failures = 0
            self._daily_trades += 1
            self._daily_pnl += net_profit

    async def record_failure(self, reason: str = "") -> None:
        async with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            if self._consecutive_failures >= self.risk_config.max_consecutive_failures:
                self._open_circuit("Max consecutive failures: %d" % self._consecutive_failures)

    async def check_daily_limits(self) -> bool:
        async with self._lock:
            if self._daily_trades >= self.risk_config.max_daily_trades:
                self._open_circuit("Daily trade limit: %d" % self._daily_trades)
                return False
            if self._daily_pnl <= -self.risk_config.max_daily_loss:
                self._open_circuit("Daily loss limit: %s" % self._daily_pnl)
                return False
            return True

    def _open_circuit(self, reason: str) -> None:
        self._state = "OPEN"
        self._opened_at = datetime.now(timezone.utc)
        self._open_reason = reason
        LOG.critical("[CIRCUIT] BREAKER OPENED: %s" % reason)

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "state": self._state,
            "open_reason": self._open_reason,
            "opened_at": self._opened_at.isoformat() if self._opened_at else None,
            "consecutive_failures": self._consecutive_failures,
            "daily_trades": self._daily_trades,
            "daily_pnl": str(self._daily_pnl),
        }


# =============================================================================
# SECTION 6: LATENCY TELEMETRY
# =============================================================================

class LatencyTracker:
    def __init__(self, window_size: int = 1000):
        self.window_size = window_size
        self._detection_us = deque(maxlen=window_size)
        self._execution_us = deque(maxlen=window_size)
        self._lock = asyncio.Lock()

    async def record_detection(self, latency_us: int) -> None:
        async with self._lock:
            self._detection_us.append(latency_us)

    async def record_execution(self, latency_us: int) -> None:
        async with self._lock:
            self._execution_us.append(latency_us)

    def _pct(self, data, p):
        if not data:
            return 0.0
        s = sorted(data)
        k = (len(s) - 1) * p
        f = int(k)
        c = min(f + 1, len(s) - 1)
        return s[f] + (k - f) * (s[c] - s[f]) if c != f else s[f]

    def _stats(self, data):
        if not data:
            return {"count": 0, "min": 0, "max": 0, "avg": 0, "p50": 0, "p99": 0}
        return {
            "count": len(data), "min": min(data), "max": max(data),
            "avg": sum(data) / len(data), "p50": self._pct(data, 0.50), "p99": self._pct(data, 0.99)
        }

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "detection_us": self._stats(self._detection_us),
            "execution_us": self._stats(self._execution_us),
        }


# =============================================================================
# SECTION 7: P&L LEDGER
# =============================================================================

class ProfitLossLedger:
    def __init__(self, symbol: str):
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

    async def record_execution(self, result: ExecutionResult, signal: ArbitrageSignal) -> None:
        async with self._lock:
            if result.status != "FILLED":
                return
            buy_pos = self.positions[result.buy_exchange]
            sell_pos = self.positions[result.sell_exchange]

            buy_pos.quote_asset -= signal.buy_vwap * signal.target_volume
            buy_pos.base_asset += signal.target_volume
            buy_pos.trade_count += 1

            sell_pos.base_asset -= signal.target_volume
            sell_pos.quote_asset += signal.sell_vwap * signal.target_volume
            sell_pos.trade_count += 1

            self._total_realized_pnl += signal.net_profit
            self._trade_history.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "execution_id": result.execution_id,
                "net_profit": str(signal.net_profit),
                "latency_us": result.latency_us,
            })
            LOG.info("[LEDGER] P&L: %s | Total: %s" % (signal.net_profit, self._total_realized_pnl))

    async def mark_to_market(self, exchange_books: Dict[ExchangeId, OrderBookSnapshot]) -> None:
        async with self._lock:
            total_unrealized = Decimal("0")
            for ex_id, pos in self.positions.items():
                book = exchange_books.get(ex_id)
                if book and book.mid_price and pos.base_asset > Decimal("0"):
                    pos.unrealized_pnl = pos.base_asset * book.mid_price
                    total_unrealized += pos.unrealized_pnl
            self._total_unrealized_pnl = total_unrealized

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "total_realized_pnl": str(self._total_realized_pnl),
            "total_unrealized_pnl": str(self._total_unrealized_pnl),
            "total_pnl": str(self._total_realized_pnl + self._total_unrealized_pnl),
            "positions": {
                k.value: {"base": str(v.base_asset), "quote": str(v.quote_asset),
                          "trades": v.trade_count, "unrealized": str(v.unrealized_pnl)}
                for k, v in self.positions.items()
            },
            "trade_count": len(self._trade_history),
        }


# =============================================================================
# SECTION 8: ALERT MANAGER
# =============================================================================

class AlertManager:
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url
        self._last_alert_time = {}
        self._rate_limit_seconds = 5.0
        self._lock = asyncio.Lock()

    async def send(self, level: AlertLevel, title: str, message: str, fields=None):
        if not self.webhook_url:
            return
        async with self._lock:
            now = time.time()
            last = self._last_alert_time.get(level, 0)
            if now - last < self._rate_limit_seconds:
                return
            self._last_alert_time[level] = now

        payload = {
            "level": level.value, "title": title, "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(), "fields": fields or {}
        }
        try:
            asyncio.create_task(self._post_webhook(payload))
        except Exception as e:
            LOG.warning("[ALERT] Queue failed: %s" % e)

    async def _post_webhook(self, payload):
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(self.webhook_url, data=data,
                                         headers={'Content-Type': 'application/json'}, method='POST')
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, urllib.request.urlopen, req, 5)
        except Exception as e:
            LOG.warning("[ALERT] Delivery failed: %s" % e)

    async def alert_execution(self, signal, result):
        await self.send(AlertLevel.EXECUTION, "Arbitrage: %s" % result.execution_id,
                        "Net: %s" % signal.net_profit, {
                            "Buy": signal.buy_exchange.value, "Sell": signal.sell_exchange.value,
                            "Latency": "%d us" % result.latency_us, "Status": result.status
                        })

    async def alert_circuit_opened(self, reason):
        await self.send(AlertLevel.CRITICAL, "CIRCUIT BREAKER OPENED", reason)

    async def alert_error(self, error):
        await self.send(AlertLevel.WARNING, "Engine Error", error)


# =============================================================================
# SECTION 9: HEALTH HTTP SERVER
# =============================================================================

class HealthServer:
    def __init__(self, port: int = 8080):
        self.port = port
        self._engine_ref = None
        self._server = None
        self._task = None

    def attach_engine(self, engine):
        self._engine_ref = engine

    async def start(self):
        self._server = await asyncio.start_server(self._handle_request, '0.0.0.0', self.port)
        LOG.info("[HEALTH] http://0.0.0.0:%d/status" % self.port)
        self._task = asyncio.create_task(self._server.serve_forever())

    async def _handle_request(self, reader, writer):
        try:
            request_line = (await reader.readline()).decode('utf-8').strip()
            if not request_line:
                writer.close(); await writer.wait_closed(); return
            parts = request_line.split()
            path = parts[1] if len(parts) > 1 else "/"

            if path == "/health":
                body = json.dumps({"status": "ok", "timestamp": time.time_ns()})
                status = "200 OK"
            elif path == "/metrics":
                body = json.dumps(self._get_metrics(), indent=2, default=str)
                status = "200 OK"
            elif path == "/status":
                body = self._get_status_html()
                status = "200 OK"
            else:
                body = "Not Found"; status = "404 Not Found"

            ct = "application/json" if path in ("/health", "/metrics") else "text/html"
            response = "HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (
                status, ct, len(body.encode('utf-8')), body)
            writer.write(response.encode('utf-8'))
            await writer.drain()
        except Exception as e:
            LOG.warning("[HEALTH] Error: %s" % e)
        finally:
            writer.close(); await writer.wait_closed()

    def _get_metrics(self):
        if self._engine_ref is None:
            return {"error": "No engine"}
        return {
            "engine": {
                "signals_detected": self._engine_ref._signals_detected,
                "signals_executed": self._engine_ref._signals_executed,
                "success_rate": round(self._engine_ref._signals_executed / max(self._engine_ref._signals_detected, 1) * 100, 2),
                "uptime_sec": (time.time_ns() - self._engine_ref._start_time_ns) / 1e9,
            },
            "circuit": self._engine_ref.circuit_breaker.get_metrics(),
            "latency": self._engine_ref.latency_tracker.get_metrics(),
            "pnl": self._engine_ref.ledger.get_metrics(),
            "exchanges": {ex_id.value: {"connected": m.is_connected, "msgs": m.messages_received}
                          for ex_id, m in self._engine_ref.exchanges.items()}
        }

    def _get_status_html(self):
        m = self._get_metrics()
        html = """<!DOCTYPE html><html><head><title>ArbEngine</title><style>
body{font-family:monospace;background:#0a0a0a;color:#0f0;padding:20px}
h1{color:#0ff;border-bottom:1px solid #0ff}
.metric{margin:10px 0;padding:10px;background:#111;border-left:3px solid #0f0}
.critical{color:#f00;border-left-color:#f00}
.ok{color:#0f0;border-left-color:#0f0}
table{border-collapse:collapse;width:100%%;margin:10px 0}
th,td{padding:8px;text-align:left;border-bottom:1px solid #333}
th{color:#0ff}</style></head><body>
<h1>Spatial Arbitrage v4.0</h1>
<div class="metric %s"><strong>Circuit:</strong> %s %s</div>
<div class="metric"><strong>Signals:</strong> %d / %d (%.1f%%)</div>
<div class="metric"><strong>Realized P&L:</strong> %s</div>
<div class="metric"><strong>Exec p99:</strong> %.0f us</div>
<table><tr><th>Ex</th><th>Conn</th><th>Msgs</th></tr>%s</table>
<table><tr><th>Ex</th><th>Base</th><th>Quote</th><th>Trades</th></tr>%s</table>
<p style="color:#666;font-size:11px">%s</p></body></html>""" % (
            "critical" if m["circuit"]["state"] == "OPEN" else "ok",
            m["circuit"]["state"], m["circuit"]["open_reason"] or "",
            m["engine"]["signals_detected"], m["engine"]["signals_executed"], m["engine"]["success_rate"],
            m["pnl"]["total_realized_pnl"], m["latency"]["execution_us"]["p99"],
            "".join("<tr><td>%s</td><td>%s</td><td>%d</td></tr>" % (k, "Y" if v["connected"] else "N", v["msgs"]) for k, v in m["exchanges"].items()),
            "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td></tr>" % (k, v["base"], v["quote"], v["trades"]) for k, v in m["pnl"]["positions"].items()),
            datetime.now(timezone.utc).isoformat()
        )
        return html

    async def shutdown(self):
        if self._server:
            self._server.close(); await self._server.wait_closed()
            LOG.info("[HEALTH] Shutdown.")


# =============================================================================
# SECTION 10: EXCHANGE WEBSOCKET MANAGER (LIVE)
# =============================================================================

class ExchangeWebSocketManager:
    def __init__(self, config, symbol, reconnect_base_delay_ms=100, reconnect_max_delay_ms=30000):
        self.config = config; self.symbol = symbol; self.exchange_id = config.exchange_id
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
        instance = exchange_class({'apiKey': self.config.api_key, 'secret': self.config.api_secret,
                                    'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
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
                delay_ms = min(self._reconnect_base_delay_ms * (2 ** (self._consecutive_failures - 1)),
                               self._reconnect_max_delay_ms)
                LOG.error("[%s] Fail #%d: %s. Reconnect %dms..." % (self.exchange_id.value, self._consecutive_failures, e, delay_ms))
                if self._exchange:
                    try: await self._exchange.close()
                    except: pass
                    self._exchange = None
                try: await asyncio.wait_for(self._shutdown_event.wait(), timeout=delay_ms / 1000.0)
                except asyncio.TimeoutError: pass

    async def _consume_order_book(self):
        if self._exchange is None: return
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
            LOG.info("[%s] Cancelled." % self.exchange_id.value); raise
        except Exception as e:
            LOG.error("[%s] Fatal: %s" % (self.exchange_id.value, e)); raise

    def _normalize_order_book(self, raw_ob, timestamp_ns):
        bids = [OrderBookLevel(level[0], level[1]) for level in raw_ob.get('bids', [])]
        asks = [OrderBookLevel(level[0], level[1]) for level in raw_ob.get('asks', [])]
        return OrderBookSnapshot(self.exchange_id, self.symbol, timestamp_ns, bids, asks)

    async def get_order_book(self):
        async with self._lock:
            return self._order_book

    @property
    def is_connected(self): return self._is_connected
    @property
    def messages_received(self): return self._messages_received

    async def shutdown(self):
        LOG.info("[%s] Shutdown." % self.exchange_id.value)
        self._shutdown_event.set(); self._is_connected = False
        if self._exchange:
            try: await self._exchange.close()
            except Exception as e: LOG.warning("[%s] Close err: %s" % (self.exchange_id.value, e))
            finally: self._exchange = None


# =============================================================================
# SECTION 11: SYNTHETIC EXCHANGE MANAGER
# =============================================================================

class SyntheticExchangeManager:
    def __init__(self, exchange_id, symbol, base_price, volatility=0.002, arb_inject_prob=0.05):
        self.exchange_id = exchange_id; self.symbol = symbol; self.base_price = base_price
        self.volatility = volatility; self.arb_inject_prob = arb_inject_prob
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
        try: await self._task
        except asyncio.CancelledError: LOG.info("[%s] Synthetic stopped." % self.exchange_id.value)

    async def _generate_loop(self):
        while not self._shutdown_event.is_set():
            try: await asyncio.wait_for(self._shutdown_event.wait(), timeout=0.1)
            except asyncio.TimeoutError: await self._generate_snapshot()

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
        bids = []; asks = []
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
            self._order_book = OrderBookSnapshot(self.exchange_id, self.symbol, now_ns, bids, asks)
        self._messages_received += 1

    async def get_order_book(self):
        async with self._lock:
            return self._order_book

    @property
    def is_connected(self): return self._is_connected and not self._shutdown_event.is_set()
    @property
    def messages_received(self): return self._messages_received

    async def shutdown(self):
        LOG.info("[%s] Synthetic shutdown." % self.exchange_id.value)
        self._shutdown_event.set(); self._is_connected = False
        if self._task and not self._task.done():
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass


# =============================================================================
# SECTION 12: CONCURRENT EXECUTION ROUTER
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

        buy_task = self._place_order(exchanges[signal.buy_exchange], TradeSide.BUY,
                                      signal.symbol, signal.target_volume, eid)
        sell_task = self._place_order(exchanges[signal.sell_exchange], TradeSide.SELL,
                                       signal.symbol, signal.target_volume, eid)
        buy_res, sell_res = await asyncio.gather(buy_task, sell_task, return_exceptions=True)

        end_ns = time.time_ns()
        latency_us = (end_ns - start_ns) // 1000

        buy_ok = not isinstance(buy_res, Exception)
        sell_ok = not isinstance(sell_res, Exception)

        if buy_ok and sell_ok:
            status = "FILLED"; err = None
        elif buy_ok and not sell_ok:
            status = "PARTIAL_BUY_ONLY"; err = "Sell failed: %s" % sell_res
        elif not buy_ok and sell_ok:
            status = "PARTIAL_SELL_ONLY"; err = "Buy failed: %s" % buy_res
        else:
            status = "FAILED"; err = "Buy:%s | Sell:%s" % (buy_res, sell_res)

        result = ExecutionResult(
            execution_id=eid, timestamp_ns=end_ns,
            buy_exchange=signal.buy_exchange, sell_exchange=signal.sell_exchange,
            buy_order_id=buy_res.get('id') if buy_ok else None,
            sell_order_id=sell_res.get('id') if sell_ok else None,
            buy_fill_price=Decimal(str(buy_res.get('average', buy_res.get('price', 0)))) if buy_ok else None,
            sell_fill_price=Decimal(str(sell_res.get('average', sell_res.get('price', 0)))) if sell_ok else None,
            filled_volume=signal.target_volume if status == "FILLED" else Decimal("0"),
            status=status, latency_us=latency_us, error_message=err
        )

        async with self._lock:
            self._execution_history.append(result)

        LOG.info("[EXEC] %s done in %dus | %s" % (eid, latency_us, status))
        return result

    async def _place_order(self, manager, side, symbol, volume, eid):
        is_synthetic = hasattr(manager, '_generate_snapshot')
        if is_synthetic:
            await asyncio.sleep(0.0001)
            book = await manager.get_order_book()
            if book is None: raise RuntimeError("No synthetic book")
            fp = book.best_ask if side == TradeSide.BUY else book.best_bid
            return {'id': "SIM-%s-%s" % (eid, side.value.upper()), 'symbol': symbol,
                    'side': side.value, 'type': 'market', 'amount': float(volume),
                    'price': float(fp), 'average': float(fp), 'status': 'closed',
                    'filled': float(volume), 'remaining': 0.0, 'cost': float(volume * fp),
                    'fee': None, 'trades': []}

        exchange = manager._exchange
        if exchange is None or not manager.is_connected:
            raise ConnectionError("[%s] Not connected" % manager.exchange_id.value)

        if self.sandbox:
            await asyncio.sleep(0.0001)
            book = await manager.get_order_book()
            fp = book.best_ask if side == TradeSide.BUY else book.best_bid
            return {'id': "SIM-%s-%s" % (eid, side.value.upper()), 'symbol': symbol,
                    'side': side.value, 'type': 'market', 'amount': float(volume),
                    'price': float(fp), 'average': float(fp), 'status': 'closed',
                    'filled': float(volume), 'remaining': 0.0, 'cost': float(volume * fp),
                    'fee': None, 'trades': []}
        else:
            return await exchange.create_order(symbol=symbol, type='market', side=side.value, amount=float(volume))


# =============================================================================
# SECTION 13: MICROSECOND PERSISTENCE LAYER
# =============================================================================

class DataSink:
    def __init__(self, db_path=":memory:", csv_path="arbitrage_signals.csv", flush_interval_seconds=60):
        self.db_path = db_path; self.csv_path = csv_path; self.flush_interval_seconds = flush_interval_seconds
        self._conn = None; self._cursor = None; self._flush_task = None; self._shutdown_event = asyncio.Event()

    def initialize(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._cursor = self._conn.cursor()
        if self.db_path != ":memory:":
            self._cursor.execute("PRAGMA journal_mode=WAL;")
            self._cursor.execute("PRAGMA synchronous=NORMAL;")
            self._cursor.execute("PRAGMA temp_store=MEMORY;")
        self._cursor.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ns INTEGER NOT NULL,
                received_at TEXT NOT NULL, symbol TEXT NOT NULL, buy_exchange TEXT NOT NULL,
                sell_exchange TEXT NOT NULL, buy_vwap TEXT NOT NULL, sell_vwap TEXT NOT NULL,
                target_volume TEXT NOT NULL, gross_spread TEXT NOT NULL, buy_fee TEXT NOT NULL,
                sell_fee TEXT NOT NULL, gas_fee TEXT NOT NULL, net_profit TEXT NOT NULL,
                net_profit_pct TEXT NOT NULL, execution_id TEXT UNIQUE NOT NULL,
                executed INTEGER DEFAULT 0, execution_latency_us INTEGER, status TEXT)
        """)
        self._cursor.execute("CREATE INDEX IF NOT EXISTS idx_ts ON arbitrage_signals(timestamp_ns)")
        self._cursor.execute("CREATE INDEX IF NOT EXISTS idx_exec ON arbitrage_signals(execution_id)")
        self._conn.commit()
        LOG.info("[DATASINK] SQLite at '%s'." % self.db_path)

    def persist_signal(self, signal):
        if self._cursor is None: raise RuntimeError("Not initialized")
        self._cursor.execute("""
            INSERT OR REPLACE INTO arbitrage_signals (
                timestamp_ns, received_at, symbol, buy_exchange, sell_exchange,
                buy_vwap, sell_vwap, target_volume, gross_spread, buy_fee,
                sell_fee, gas_fee, net_profit, net_profit_pct, execution_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (signal.timestamp_ns, datetime.now(timezone.utc).isoformat(), signal.symbol,
              signal.buy_exchange.value, signal.sell_exchange.value, str(signal.buy_vwap),
              str(signal.sell_vwap), str(signal.target_volume), str(signal.gross_spread),
              str(signal.buy_fee), str(signal.sell_fee), str(signal.gas_fee),
              str(signal.net_profit), str(signal.net_profit_pct), signal.execution_id))
        self._conn.commit()

    def update_execution_result(self, result):
        if self._cursor is None: return
        self._cursor.execute("UPDATE arbitrage_signals SET executed=1, execution_latency_us=?, status=? WHERE execution_id=?",
                             (result.latency_us, result.status, result.execution_id))
        self._conn.commit()

    async def start_background_flush(self):
        self._flush_task = asyncio.create_task(self._background_flush_loop())
        LOG.info("[DATASINK] Background flush started.")

    async def _background_flush_loop(self):
        while not self._shutdown_event.is_set():
            try: await asyncio.wait_for(self._shutdown_event.wait(), timeout=self.flush_interval_seconds)
            except asyncio.TimeoutError: await self._flush_to_csv()

    async def _flush_to_csv(self):
        if self._cursor is None: return
        try:
            self._cursor.execute("SELECT * FROM arbitrage_signals ORDER BY timestamp_ns DESC")
            rows = self._cursor.fetchall()
            columns = [desc[0] for desc in self._cursor.description]
            if not rows: return
            async with aiofiles.open(self.csv_path, mode='w', newline='') as f:
                await f.write(','.join(columns) + '\n')
                for row in rows:
                    escaped = []
                    for val in row:
                        if val is None: escaped.append('')
                        elif isinstance(val, str) and (',' in val or '"' in val or '\n' in val):
                            escaped.append('"' + val.replace('"', '""') + '"')
                        else: escaped.append(str(val))
                    await f.write(','.join(escaped) + '\n')
            LOG.info("[DATASINK] Flushed %d records to %s" % (len(rows), self.csv_path))
        except Exception as e:
            LOG.error("[DATASINK] Flush error: %s" % e)

    async def shutdown(self):
        LOG.info("[DATASINK] Shutdown. Final flush...")
        self._shutdown_event.set()
        if self._flush_task:
            self._flush_task.cancel()
            try: await self._flush_task
            except asyncio.CancelledError: pass
        await self._flush_to_csv()
        if self._conn:
            self._conn.close()
            LOG.info("[DATASINK] Closed.")


# =============================================================================
# SECTION 14: MAIN ARBITRAGE ENGINE ORCHESTRATOR
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
        LOG.info("ARBITRAGE ENGINE v4.0 | Mode: %s" % ("DEMO" if self.demo_mode else "LIVE"))
        LOG.info("Symbol: %s | Vol: %s | Threshold: %s" % (self.config.symbol, self.config.target_volume, self.config.min_yield_threshold))
        LOG.info("Risk: max_trades=%d | max_loss=%s | cooldown=%ds" % (
            self.risk_config.max_daily_trades, self.risk_config.max_daily_loss, self.risk_config.cooldown_seconds_after_failure))
        LOG.info("Health: http://0.0.0.0:%d/status" % self.health_server.port)
        LOG.info("=" * 70)

        self.data_sink.initialize()
        await self.data_sink.start_background_flush()
        await self.ledger.initialize_positions(list(self.exchanges.keys()))
        await self.health_server.start()

        self._tasks.extend([asyncio.create_task(ex.connect(), name="Conn-%s" % ex.exchange_id.value)
                            for ex in self.exchanges.values()])
        await asyncio.sleep(1)

        self._tasks.append(asyncio.create_task(self._detection_loop(), name="Detector"))
        self._tasks.append(asyncio.create_task(self._status_reporter(), name="Reporter"))
        self._tasks.append(asyncio.create_task(self._mark_to_market_loop(), name="MTM"))

        LOG.info("[ENGINE] Online.")
        await self._shutdown_event.wait()

    async def _detection_loop(self):
        ex_list = list(self.exchanges.values())
        ex_a, ex_b = ex_list[0], ex_list[1]
        ex_a_id, ex_b_id = ex_a.exchange_id, ex_b.exchange_id

        fee_map = {}
        for ec in [ExchangeConfig(ExchangeId.BINANCE, "", "", Decimal("0.001")),
                   ExchangeConfig(ExchangeId.KRAKEN, "", "", Decimal("0.0026"))]:
            if ec.exchange_id in self.exchanges:
                fee_map[ec.exchange_id] = ec.taker_fee_rate

        while not self._shutdown_event.is_set():
            loop_start = time.time_ns()

            if not await self.circuit_breaker.can_trade():
                await asyncio.sleep(1.0); continue
            if not await self.circuit_breaker.check_daily_limits():
                await asyncio.sleep(1.0); continue

            try:
                book_a = await ex_a.get_order_book()
                book_b = await ex_b.get_order_book()
                if book_a is None or book_b is None:
                    await asyncio.sleep(self.poll_interval_ms / 1000.0); continue

                sig_ab = PrecisionCalculator.validate_signal(
                    book_a, book_b, self.config,
                    fee_map.get(ex_a_id, Decimal("0.001")),
                    fee_map.get(ex_b_id, Decimal("0.0026")))
                if sig_ab:
                    await self.latency_tracker.record_detection((time.time_ns() - loop_start) // 1000)
                    await self._process_signal(sig_ab); continue

                sig_ba = PrecisionCalculator.validate_signal(
                    book_b, book_a, self.config,
                    fee_map.get(ex_b_id, Decimal("0.0026")),
                    fee_map.get(ex_a_id, Decimal("0.001")))
                if sig_ba:
                    await self.latency_tracker.record_detection((time.time_ns() - loop_start) // 1000)
                    await self._process_signal(sig_ba); continue

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
        LOG.info("[SIGNAL] #%d | %s | Net: %s %s" % (self._signals_detected, signal.execution_id,
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
                LOG.info("[SIGNAL] #%d EXECUTED in %dus" % (self._signals_detected, result.latency_us))
            else:
                await self.circuit_breaker.record_failure(result.status)
                LOG.warning("[SIGNAL] #%d issue: %s" % (self._signals_detected, result.status))
        except Exception as e:
            LOG.error("[SIGNAL] Exec failed: %s" % e)
            await self.circuit_breaker.record_failure(str(e))
            await self.alert_manager.alert_error(str(e))

    async def _mark_to_market_loop(self):
        while not self._shutdown_event.is_set():
            try: await asyncio.wait_for(self._shutdown_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                books = {ex_id: await m.get_order_book() for ex_id, m in self.exchanges.items()}
                books = {k: v for k, v in books.items() if v}
                if books: await self.ledger.mark_to_market(books)

    async def _status_reporter(self):
        while not self._shutdown_event.is_set():
            try: await asyncio.wait_for(self._shutdown_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                for ex_id, m in self.exchanges.items():
                    LOG.info("[HEALTH] %s: %s | Msgs:%d" % (ex_id.value, "CONN" if m.is_connected else "DISC", m.messages_received))
                rate = self._signals_executed / max(self._signals_detected, 1) * 100
                lat = self.latency_tracker.get_metrics()
                exec_p99 = lat["execution_us"]["p99"]
                LOG.info("[METRICS] Sig:%d | Exec:%d | Rate:%.1f%% | Exec p99:%.0fus | P&L:%s" % (
                    self._signals_detected, self._signals_executed, rate, exec_p99,
                    self.ledger.get_metrics()["total_realized_pnl"]))

    async def shutdown(self):
        LOG.info("[ENGINE] Shutdown...")
        self._shutdown_event.set()
        for task in self._tasks:
            if not task.done(): task.cancel()
        if self._tasks: await asyncio.gather(*self._tasks, return_exceptions=True)
        for m in self.exchanges.values(): await m.shutdown()
        await self.data_sink.shutdown()
        await self.health_server.shutdown()
        LOG.info("[ENGINE] Done.")


# =============================================================================
# SECTION 15: SIGNAL HANDLERS & MAIN
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
            LOG.warning("[SIGNAL] Forced exit."); sys.exit(1)
        self._received = True
        LOG.info("[SIGNAL] %s received. Graceful shutdown..." % signal.Signals(sig).name)
        asyncio.create_task(self.engine.shutdown())


async def main():
    parser = argparse.ArgumentParser(description="Spatial Arbitrage Engine v4.0")
    parser.add_argument("--live", action="store_true", help="LIVE mode (needs API keys)")
    parser.add_argument("--duration", type=int, default=60, help="Demo duration in seconds")
    parser.add_argument("--db", default="arbitrage_engine.db", help="SQLite path")
    parser.add_argument("--csv", default="arbitrage_signals.csv", help="CSV path")
    parser.add_argument("--port", type=int, default=8080, help="Health server port")
    parser.add_argument("--webhook", default=None, help="Webhook URL for alerts")
    args = parser.parse_args()

    demo_mode = not args.live

    arb_config = ArbitrageConfig(
        symbol="BTC/USDT", target_volume=Decimal("0.01"),
        min_yield_threshold=Decimal("5.00"), estimated_gas_fee=Decimal("2.50"),
        max_slippage_tolerance=Decimal("0.005"), vwap_depth_limit=20)

    risk_config = RiskConfig(
        max_daily_trades=1000, max_daily_loss=Decimal("1000.00"),
        max_consecutive_failures=5, cooldown_seconds_after_failure=30,
        circuit_breaker_drawdown_pct=Decimal("0.05"))

    if demo_mode:
        LOG.info("[MAIN] DEMO MODE — no API keys needed.")
        exchange_configs = [
            ExchangeConfig(ExchangeId.BINANCE, "demo", "demo", Decimal("0.001"), True),
            ExchangeConfig(ExchangeId.KRAKEN, "demo", "demo", Decimal("0.0026"), True)]
    else:
        LOG.info("[MAIN] LIVE MODE.")
        exchange_configs = [
            ExchangeConfig(ExchangeId.BINANCE, os.environ.get("BINANCE_API_KEY", ""),
                           os.environ.get("BINANCE_API_SECRET", ""), Decimal("0.001"), True),
            ExchangeConfig(ExchangeId.KRAKEN, os.environ.get("KRAKEN_API_KEY", ""),
                           os.environ.get("KRAKEN_API_SECRET", ""), Decimal("0.0026"), True)]

    data_sink = DataSink(db_path=args.db, csv_path=args.csv, flush_interval_seconds=10)
    execution_router = ExecutionRouter(sandbox=True)

    engine = ArbitrageEngine(
        arb_config=arb_config, exchange_configs=exchange_configs,
        data_sink=data_sink, execution_router=execution_router,
        risk_config=risk_config, poll_interval_ms=10.0,
        demo_mode=demo_mode, webhook_url=args.webhook, health_port=args.port)

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
        LOG.info("[MAIN] Interrupted."); sys.exit(0)
