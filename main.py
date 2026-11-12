from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ast_strategy import ASTStructureStrategy
from historical_sources import (
    load_btc_um_history,
    load_binance_tradfi_gold_history,
    load_binance_tradfi_gold_live,
)
from ast_campaign_orchestrator import ASTFourEngineOrchestrator

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("AST-Liquidity-Radar")

REST_BASE = "https://fapi.binance.com"
MARKET_WS_BASE = "wss://fstream.binance.com/market/stream"
PUBLIC_WS_BASE = "wss://fstream.binance.com/public/stream"
SYMBOLS = {"BTC": "BTCUSDT", "GOLD": "XAUUSDT"}
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
HISTORY_BARS = max(25_000, int(os.getenv("AST_HISTORY_BARS", "25_000")))
BRIDGE_SECRET = os.getenv("VANTAGE_BRIDGE_SECRET", "replace-this-secret")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "false").lower() == "true"


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError):
        pass
    return default


def median_or_none(values: deque[float] | list[float]) -> float | None:
    return statistics.median(values) if values else None


@dataclass
class AssetState:
    key: str
    symbol: str
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    next_funding_time: int | None = None
    mark_ts: int | None = None
    open_interest: float | None = None
    oi_ts: int | None = None
    oi_history: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=500))
    long_account: float | None = None
    short_account: float | None = None
    long_short_ratio: float | None = None
    lsr_ts: int | None = None
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    book_ts: int | None = None
    book_imbalance: float | None = None
    bid_wall_price: float | None = None
    bid_wall_notional: float | None = None
    ask_wall_price: float | None = None
    ask_wall_notional: float | None = None
    trades: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=30000))
    liquidation_events: deque[tuple[int, str, float, float]] = field(default_factory=lambda: deque(maxlen=5000))
    candles: dict[str, deque[dict[str, float | int]]] = field(
        default_factory=lambda: {tf: deque(maxlen=HISTORY_BARS + 100) for tf in TIMEFRAMES}
    )
    exchange_prices: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=3000))
    vantage_price: float | None = None
    vantage_high: float | None = None
    vantage_low: float | None = None
    vantage_bid: float | None = None
    vantage_ask: float | None = None
    vantage_spread: float | None = None
    vantage_ts: int | None = None
    vantage_ticker: str | None = None
    vantage_tf: str | None = None
    basis_ratios: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    basis_ratio: float | None = None
    basis_offset: float | None = None
    vantage_match_delta_ms: int | None = None
    vantage_match_candidate_price: float | None = None
    vantage_match_ratio: float | None = None
    vantage_match_reason: str = "Waiting for matched exchange tick"
    basis_updated_ts: int | None = None
    latest_analysis: dict[str, Any] = field(default_factory=dict)
    recent_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=100))
    last_message_ts: int | None = None
    last_depth_update: int | None = None
    connection_error: str | None = None
    trend_lock_side: str | None = None
    trend_lock_since: int | None = None
    trend_lock_reason: str = "Waiting for multi-timeframe alignment"
    structure_engine: ASTStructureStrategy = field(default_factory=ASTStructureStrategy)
    campaign_engine: ASTFourEngineOrchestrator = field(default_factory=ASTFourEngineOrchestrator)
    data_source: str | None = None
    broker_candle_feed: bool = False
    broker_candle_ts: int | None = None
    binance_tradfi_stream_active: bool = False
    binance_tradfi_last_update_ts: int | None = None

    def add_trade(self, ts: int, signed_notional: float) -> None:
        self.trades.append((ts, signed_notional))
        cutoff = ts - 15 * 60 * 1000
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()

    def cvd(self, window_ms: int = 5 * 60 * 1000) -> float:
        if not self.trades:
            return 0.0
        cutoff = now_ms() - window_ms
        return sum(v for ts, v in self.trades if ts >= cutoff)

    def liquidation_totals(self, window_ms: int = 5 * 60 * 1000) -> dict[str, float]:
        cutoff = now_ms() - window_ms
        long_liq = 0.0
        short_liq = 0.0
        for ts, forced_side, notional, _price in self.liquidation_events:
            if ts >= cutoff:
                if forced_side == "SELL":
                    long_liq += notional
                elif forced_side == "BUY":
                    short_liq += notional
        return {"long_liquidations_usdt": long_liq, "short_liquidations_usdt": short_liq}

    def liquidation_clusters(self, window_ms: int = 30 * 60 * 1000) -> dict[str, dict[str, float] | None]:
        cutoff = now_ms() - window_ms
        if not self.mark_price:
            return {"long_cluster": None, "short_cluster": None}
        bucket_size = max(self.mark_price * 0.001, 0.01)
        groups: dict[str, dict[int, list[float]]] = {"SELL": {}, "BUY": {}}
        for ts, side, notional, price in self.liquidation_events:
            if ts < cutoff or side not in groups or price <= 0:
                continue
            bucket = int(round(price / bucket_size))
            entry = groups[side].setdefault(bucket, [0.0, 0.0])
            entry[0] += notional
            entry[1] += price * notional
        result: dict[str, dict[str, float] | None] = {"long_cluster": None, "short_cluster": None}
        for side, name in (("SELL", "long_cluster"), ("BUY", "short_cluster")):
            if groups[side]:
                bucket, totals = max(groups[side].items(), key=lambda item: item[1][0])
                notional, weighted = totals
                result[name] = {"price": weighted / notional if notional else bucket * bucket_size, "notional_usdt": notional}
        return result

    def oi_change_pct(self, lookback_ms: int = 5 * 60 * 1000) -> float | None:
        if self.open_interest is None or not self.oi_history:
            return None
        cutoff = now_ms() - lookback_ms
        # Do not label a 30-second sample as a 5-minute OI change.
        if not self.oi_history or self.oi_history[0][0] > cutoff:
            return None
        base = next((oi for ts, oi in self.oi_history if ts >= cutoff), None)
        if base is None or not base:
            return None
        return (self.open_interest / base - 1.0) * 100.0

    def update_book(self, data: dict[str, Any]) -> None:
        bids = data.get("b", [])
        asks = data.get("a", [])
        if not bids or not asks:
            return
        self.bids = {float(p): float(q) for p, q in bids if safe_float(p) and safe_float(q) is not None and float(q) > 0}
        self.asks = {float(p): float(q) for p, q in asks if safe_float(p) and safe_float(q) is not None and float(q) > 0}
        bid_values = [(p, p * q) for p, q in self.bids.items()]
        ask_values = [(p, p * q) for p, q in self.asks.items()]
        bid_total = sum(v for _, v in bid_values)
        ask_total = sum(v for _, v in ask_values)
        denom = bid_total + ask_total
        self.book_imbalance = (bid_total - ask_total) / denom if denom else None
        if bid_values:
            self.bid_wall_price, self.bid_wall_notional = max(bid_values, key=lambda x: x[1])
        if ask_values:
            self.ask_wall_price, self.ask_wall_notional = max(ask_values, key=lambda x: x[1])
        self.book_ts = now_ms()

    def add_candle(self, tf: str, k: dict[str, Any]) -> dict[str, Any] | None:
        if tf not in self.candles or not k.get("x"):
            return None
        candle = {
            "t": int(k["t"]),
            "o": float(k["o"]),
            "h": float(k["h"]),
            "l": float(k["l"]),
            "c": float(k["c"]),
            "v": float(k["v"]),
        }
        candles = self.candles[tf]
        if candles and candles[-1]["t"] == candle["t"]:
            candles[-1] = candle
        else:
            candles.append(candle)
        analysis = self.analyse(tf)
        self.latest_analysis[tf] = analysis
        return analysis

    def analyse(self, tf: str) -> dict[str, Any]:
        candles = list(self.candles[tf])
        if len(candles) < 25:
            return {"ready": False, "bars": len(candles), "message": "Warming up candle history"}

        closes = [float(c["c"]) for c in candles]
        highs = [float(c["h"]) for c in candles]
        lows = [float(c["l"]) for c in candles]
        last = candles[-1]
        prev = candles[-2]

        def ema(values: list[float], period: int) -> float:
            alpha = 2.0 / (period + 1.0)
            result = values[0]
            for x in values[1:]:
                result = alpha * x + (1.0 - alpha) * result
            return result

        ema_fast = ema(closes[-max(60, min(len(closes), 120)):], 20)
        ema_slow = ema(closes[-max(120, min(len(closes), 240)):], 50)
        local_trend = "BULLISH" if closes[-1] > ema_fast > ema_slow else "BEARISH" if closes[-1] < ema_fast < ema_slow else "NEUTRAL"

        tr_values: list[float] = []
        start = max(1, len(candles) - 15)
        for i in range(start, len(candles)):
            tr_values.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        atr = sum(tr_values) / len(tr_values) if tr_values else 0.0

        lookback = min(30, len(candles) - 2)
        prior_high = max(highs[-lookback - 1:-1])
        prior_low = min(lows[-lookback - 1:-1])
        range_width_atr = (prior_high - prior_low) / atr if atr else None
        balance = bool(atr and range_width_atr is not None and range_width_atr <= 5.0)

        body = abs(float(last["c"]) - float(last["o"]))
        full_range = max(float(last["h"]) - float(last["l"]), 1e-12)
        close_pos = (float(last["c"]) - float(last["l"])) / full_range
        bull_displacement = last["c"] > last["o"] and body >= atr * 0.7 and close_pos >= 0.75 if atr else False
        bear_displacement = last["c"] < last["o"] and body >= atr * 0.7 and close_pos <= 0.25 if atr else False

        upper_sweep = float(last["h"]) > prior_high and float(last["c"]) < prior_high
        lower_sweep = float(last["l"]) < prior_low and float(last["c"]) > prior_low
        range_break_down = float(last["c"]) < prior_low and bear_displacement
        range_break_up = float(last["c"]) > prior_high and bull_displacement

        pivot_len = 3
        phs: list[float] = []
        pls: list[float] = []
        if len(candles) >= pivot_len * 2 + 3:
            for i in range(max(pivot_len, len(candles) - 60), len(candles) - pivot_len):
                h = highs[i]
                l = lows[i]
                if h == max(highs[i - pivot_len:i + pivot_len + 1]):
                    phs.append(h)
                if l == min(lows[i - pivot_len:i + pivot_len + 1]):
                    pls.append(l)

        equal_high = None
        equal_low = None
        tolerance = atr * 0.20
        if len(phs) >= 2 and abs(phs[-1] - phs[-2]) <= tolerance:
            equal_high = (phs[-1] + phs[-2]) / 2.0
        if len(pls) >= 2 and abs(pls[-1] - pls[-2]) <= tolerance:
            equal_low = (pls[-1] + pls[-2]) / 2.0

        return {
            "ready": True,
            "timeframe": tf,
            "bars": len(candles),
            "last_close": float(last["c"]),
            "local_trend": local_trend,
            "ema20": ema_fast,
            "ema50": ema_slow,
            "atr": atr,
            "range_high": prior_high,
            "range_low": prior_low,
            "range_width_atr": range_width_atr,
            "balance": balance,
            "equal_high": equal_high,
            "equal_low": equal_low,
            "upper_sweep": upper_sweep,
            "lower_sweep": lower_sweep,
            "bull_displacement": bull_displacement,
            "bear_displacement": bear_displacement,
            "range_break_down": range_break_down,
            "range_break_up": range_break_up,
            "candle_time": int(last["t"]),
        }


states: dict[str, AssetState] = {
    key: AssetState(key=key, symbol=symbol) for key, symbol in SYMBOLS.items()
}

app = FastAPI(title="AST Institutional Liquidity Radar", version="2.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

http_client: httpx.AsyncClient | None = None
background_tasks: list[asyncio.Task] = []
last_alert_sent: dict[str, float] = defaultdict(float)
CAMPAIGN_ALERT_POLL_SECONDS = 2.0
CAMPAIGN_ALERTS_ENABLED = ENABLE_TELEGRAM and bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
campaign_alert_seen: dict[str, set[str]] = defaultdict(set)


async def send_telegram(message: str) -> bool:
    if not ENABLE_TELEGRAM or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    if http_client is None:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = await http_client.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True},
            timeout=8.0,
        )
        payload = response.json()
        if response.status_code >= 400 or not payload.get("ok"):
            log.warning("Telegram send rejected: HTTP %s | %s", response.status_code, response.text[:180])
            return False
        return True
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)
        return False


def build_event_trade_plan(st: AssetState, event_type: str, timeframe: str, event_price: float | None) -> dict[str, Any]:
    """Create transparent, event-based reference levels; never invent levels without valid candle data."""
    side_by_event = {
        "LOWER_LIQUIDITY_SWEEP": "BUY",
        "BULLISH_RANGE_EXPANSION": "BUY",
        "UPPER_LIQUIDITY_SWEEP": "SELL",
        "BEARISH_RANGE_EXPANSION": "SELL",
    }
    side = side_by_event.get(event_type)
    candles = list(st.candles.get(timeframe, []))
    analysis = st.latest_analysis.get(timeframe, {})
    base = {
        "side": side,
        "entry": None,
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "rr1": "1:2",
        "rr2": "1:3",
        "status": "NO VALID SETUP",
        "basis": "Binance event candle close; indicative levels only",
    }
    if not side or not analysis.get("ready") or not analysis.get("atr") or not candles:
        return base

    candle = candles[-1]
    entry = float(event_price if event_price is not None else candle["c"])
    atr = float(analysis["atr"])
    buffer = max(atr * 0.15, entry * 0.0001)
    if side == "BUY":
        stop = float(candle["l"]) - buffer
        risk = entry - stop
        if risk <= 0:
            return base
        tp1, tp2 = entry + risk * 2, entry + risk * 3
    else:
        stop = float(candle["h"]) + buffer
        risk = stop - entry
        if risk <= 0:
            return base
        tp1, tp2 = entry - risk * 2, entry - risk * 3

    return {
        **base,
        "entry": round(entry, 8),
        "stop_loss": round(stop, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "status": "REFERENCE PLAN",
    }


async def publish_event(key: str, event_type: str, detail: str, price: float | None, timeframe: str) -> None:
    st = states[key]
    event = {
        "id": f"{key}-{event_type}-{now_ms()}",
        "asset": key,
        "symbol": st.symbol,
        "type": event_type,
        "detail": detail,
        "price": price,
        "time": now_ms(),
        "timeframe": timeframe,
    }
    st.recent_events.appendleft(event)
    alert_key = f"{key}:{event_type}"
    current = time.time()
    if current - last_alert_sent[alert_key] >= 300:
        last_alert_sent[alert_key] = current
        vantage_price = st.vantage_price
        vprice = f"\nVantage mapped price: {vantage_price:.4f}" if vantage_price else ""
        await send_telegram(
            f"AST LIQUIDITY RADAR | {key}\n{event_type}\n{detail}\nBinance: {price}\nTF: {timeframe}{vprice}\nEvent information only; reaction levels are shown on the dashboard."
        )


def calculate_scores(st: AssetState, tf: str) -> dict[str, Any]:
    analysis = st.latest_analysis.get(tf, {})
    if not analysis.get("ready"):
        return {"bull_pressure": None, "bear_pressure": None, "label": "WARMING UP", "components": {}}

    bull = 0.0
    bear = 0.0
    components: dict[str, Any] = {}

    trend = analysis.get("local_trend", "NEUTRAL")
    if trend == "BULLISH":
        bull += 12
    elif trend == "BEARISH":
        bear += 12
    components["local_trend"] = trend
    higher = st.latest_analysis.get("1h", {})
    higher_trend = higher.get("local_trend", "NEUTRAL") if higher.get("ready") else "NEUTRAL"
    if higher_trend == "BULLISH":
        bull += 8
    elif higher_trend == "BEARISH":
        bear += 8
    components["higher_timeframe_trend_1h"] = higher_trend

    cvd5 = st.cvd(5 * 60 * 1000)
    cvd15 = st.cvd(15 * 60 * 1000)
    if cvd5 > 0:
        bull += 10
    elif cvd5 < 0:
        bear += 10
    if cvd15 > 0:
        bull += 5
    elif cvd15 < 0:
        bear += 5
    components["cvd_5m_usdt"] = cvd5
    components["cvd_15m_usdt"] = cvd15

    obi = st.book_imbalance
    if obi is not None:
        if obi >= 0.15:
            bull += 12
        elif obi <= -0.15:
            bear += 12
    components["orderbook_imbalance"] = obi

    oi_change = st.oi_change_pct()
    price_return = None
    if len(st.exchange_prices) > 2:
        current_price = st.mark_price
        cutoff = now_ms() - 5 * 60 * 1000
        base_price = next((p for ts, p in st.exchange_prices if ts >= cutoff), st.exchange_prices[0][1])
        if current_price and base_price:
            price_return = (current_price / base_price - 1.0) * 100.0
    if oi_change is not None and price_return is not None:
        if oi_change > 0.3 and price_return > 0.1:
            bull += 10
        elif oi_change > 0.3 and price_return < -0.1:
            bear += 10
    components["oi_change_5m_pct"] = oi_change
    components["price_change_5m_pct"] = price_return

    if st.funding_rate is not None:
        if st.funding_rate > 0.0001:
            bear += 6
        elif st.funding_rate < -0.0001:
            bull += 6
    components["funding_rate"] = st.funding_rate

    if st.long_account is not None:
        if st.long_account >= 0.55:
            bear += 7
        elif st.long_account <= 0.45:
            bull += 7
    components["global_long_account"] = st.long_account

    liqs = st.liquidation_totals()
    long_liq = liqs["long_liquidations_usdt"]
    short_liq = liqs["short_liquidations_usdt"]
    if long_liq > short_liq * 1.5 and long_liq > 0:
        bear += 8
    elif short_liq > long_liq * 1.5 and short_liq > 0:
        bull += 8
    components.update(liqs)

    if analysis.get("upper_sweep") or analysis.get("range_break_down"):
        bear += 15
    if analysis.get("lower_sweep") or analysis.get("range_break_up"):
        bull += 15

    # The index is a rule-based pressure score, not a statistical probability.
    bull = round(min(100.0, bull), 1)
    bear = round(min(100.0, bear), 1)
    if bull >= 55 and bull >= bear + 10:
        label = "BULLISH PRESSURE"
    elif bear >= 55 and bear >= bull + 10:
        label = "BEARISH PRESSURE"
    elif max(bull, bear) >= 35:
        label = "MIXED / WAIT"
    else:
        label = "NO CLEAR EDGE"

    return {"bull_pressure": bull, "bear_pressure": bear, "label": label, "components": components}


def adjusted_price(st: AssetState, exchange_price: float | None) -> float | None:
    if exchange_price is None:
        return None
    # Direct Vantage GOLD candles are already in the broker's XAUUSD price
    # scale.
    if st.key == "GOLD" and st.broker_candle_feed:
        if st.broker_candle_ts is None or now_ms() - st.broker_candle_ts > 90_000:
            return None
        if st.mark_ts is None or now_ms() - st.mark_ts > 20_000:
            return None
        return exchange_price
    if st.basis_ratio is None or st.vantage_ts is None:
        return None
    if len(st.basis_ratios) < 3 or st.basis_updated_ts is None:
        return None
    if now_ms() - st.vantage_ts > 180_000 or now_ms() - st.basis_updated_ts > 180_000:
        return None
    if st.mark_ts is None or now_ms() - st.mark_ts > 20_000:
        return None
    return exchange_price * st.basis_ratio



def build_reaction_levels(st: AssetState, timeframe: str) -> list[dict[str, Any]]:
    """Return candidate horizontal reaction prices from closed Binance candles."""
    levels: list[dict[str, Any]] = []
    hourly = list(st.candles.get("1h", []))
    if hourly:
        import datetime as _dt
        current_day = _dt.datetime.fromtimestamp(int(hourly[-1]["t"]) / 1000, tz=_dt.timezone.utc).date()
        daily: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        weekly: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for candle in hourly:
            stamp = _dt.datetime.fromtimestamp(int(candle["t"]) / 1000, tz=_dt.timezone.utc)
            daily[stamp.date()].append(candle)
            iso = stamp.isocalendar()
            weekly[(iso.year, iso.week)].append(candle)
        prior_days = [d for d in daily if d < current_day]
        if prior_days:
            d = max(prior_days)
            group = daily[d]
            levels.extend([
                {"name": "Previous Day High", "price": max(float(c["h"]) for c in group), "kind": "resistance", "source": "1H candles"},
                {"name": "Previous Day Low", "price": min(float(c["l"]) for c in group), "kind": "support", "source": "1H candles"},
            ])
        current_iso = _dt.datetime.fromtimestamp(int(hourly[-1]["t"]) / 1000, tz=_dt.timezone.utc).isocalendar()
        current_week = (current_iso.year, current_iso.week)
        prior_weeks = [w for w in weekly if w < current_week]
        if prior_weeks:
            w = max(prior_weeks)
            group = weekly[w]
            levels.extend([
                {"name": "Previous Week High", "price": max(float(c["h"]) for c in group), "kind": "resistance", "source": "1H candles"},
                {"name": "Previous Week Low", "price": min(float(c["l"]) for c in group), "kind": "support", "source": "1H candles"},
            ])

    candles = list(st.candles.get(timeframe, []))
    if len(candles) >= 9:
        highs = [float(c["h"]) for c in candles]
        lows = [float(c["l"]) for c in candles]
        pivot_highs: list[float] = []
        pivot_lows: list[float] = []
        span = 3
        for i in range(max(span, len(candles) - 240), len(candles) - span):
            if highs[i] == max(highs[i-span:i+span+1]):
                pivot_highs.append(highs[i])
            if lows[i] == min(lows[i-span:i+span+1]):
                pivot_lows.append(lows[i])
        if pivot_highs:
            levels.append({"name": "Recent Swing High", "price": pivot_highs[-1], "kind": "resistance", "source": timeframe})
        if pivot_lows:
            levels.append({"name": "Recent Swing Low", "price": pivot_lows[-1], "kind": "support", "source": timeframe})
        analysis = st.latest_analysis.get(timeframe, {})
        tolerance = float(analysis.get("atr") or 0) * 0.20
        if len(pivot_highs) >= 2 and tolerance and abs(pivot_highs[-1] - pivot_highs[-2]) <= tolerance:
            levels.append({"name": "Equal Highs", "price": (pivot_highs[-1] + pivot_highs[-2]) / 2, "kind": "resistance", "source": timeframe})
        if len(pivot_lows) >= 2 and tolerance and abs(pivot_lows[-1] - pivot_lows[-2]) <= tolerance:
            levels.append({"name": "Equal Lows", "price": (pivot_lows[-1] + pivot_lows[-2]) / 2, "kind": "support", "source": timeframe})

    # Keep one nearest occurrence per label and avoid duplicate prices.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in levels:
        marker = (item["name"], round(float(item["price"]) * 10000))
        if marker not in seen and math.isfinite(float(item["price"])):
            seen.add(marker)
            unique.append(item)
    return unique

def build_structure_snapshot(st: AssetState) -> dict[str, Any]:
    """Return the multi-timeframe structure analytics without the legacy Trap engine."""
    result = st.structure_engine.snapshot(st.candles)
    for setup in result.get("setups", []):
        setup["asset"] = st.key
    return result

def build_a_plus_scanner(st: AssetState) -> list[dict[str, Any]]:
    """Backward-compatible route name; now returns all timeframe strategy setups."""
    return build_structure_snapshot(st).get("setups", [])


def make_snapshot(key: str, tf: str) -> dict[str, Any]:
    st = states[key]
    strategy = build_structure_snapshot(st)
    analysis = st.latest_analysis.get(tf, {"ready": False, "message": "Waiting for candles"})
    scores = calculate_scores(st, tf)
    liqs = st.liquidation_totals()
    liq_clusters = st.liquidation_clusters()
    higher = st.latest_analysis.get("1h", {})
    vantage_fresh = st.vantage_ts is not None and now_ms() - st.vantage_ts <= 180_000
    exchange_fresh = st.mark_ts is not None and now_ms() - st.mark_ts <= 20_000
    mapped = {
        "range_high": adjusted_price(st, analysis.get("range_high")),
        "range_low": adjusted_price(st, analysis.get("range_low")),
        "equal_high": adjusted_price(st, analysis.get("equal_high")),
        "equal_low": adjusted_price(st, analysis.get("equal_low")),
        "bid_wall": adjusted_price(st, st.bid_wall_price),
        "ask_wall": adjusted_price(st, st.ask_wall_price),
        "long_liquidation_cluster": adjusted_price(st, liq_clusters["long_cluster"]["price"]) if liq_clusters["long_cluster"] else None,
        "short_liquidation_cluster": adjusted_price(st, liq_clusters["short_cluster"]["price"]) if liq_clusters["short_cluster"] else None,
    }
    return {
        "asset": key,
        "exchange_symbol": st.symbol,
        "historical_source": st.data_source,
        "binance_tradfi_feed_active": st.binance_tradfi_stream_active,
        "binance_tradfi_feed_age_seconds": round((now_ms() - st.binance_tradfi_last_update_ts) / 1000, 1) if st.binance_tradfi_last_update_ts else None,
        "broker_candle_feed": st.broker_candle_feed,
        "broker_candle_age_seconds": round((now_ms() - st.broker_candle_ts) / 1000, 1) if st.broker_candle_ts else None,
        "exchange_price": st.mark_price,
        "index_price": st.index_price,
        "funding_rate": st.funding_rate,
        "next_funding_time": st.next_funding_time,
        "open_interest": st.open_interest,
        "oi_change_5m_pct": st.oi_change_pct(),
        "long_account": st.long_account,
        "short_account": st.short_account,
        "long_short_ratio": st.long_short_ratio,
        "book_imbalance": st.book_imbalance,
        "bid_wall_price": st.bid_wall_price,
        "bid_wall_notional": st.bid_wall_notional,
        "ask_wall_price": st.ask_wall_price,
        "ask_wall_notional": st.ask_wall_notional,
        "cvd_5m": st.cvd(5 * 60 * 1000),
        "cvd_15m": st.cvd(15 * 60 * 1000),
        **liqs,
        **liq_clusters,
        "higher_timeframe_trend": higher.get("local_trend") if higher.get("ready") else None,
        "vantage_price": st.vantage_price,
        "vantage_bid": st.vantage_bid,
        "vantage_ask": st.vantage_ask,
        "vantage_spread": st.vantage_spread,
        "vantage_ticker": st.vantage_ticker,
        "vantage_timeframe": st.vantage_tf,
        "vantage_age_seconds": round((now_ms() - st.vantage_ts) / 1000, 1) if st.vantage_ts else None,
        "vantage_feed_fresh": vantage_fresh,
        "basis_ratio": st.basis_ratio,
        "basis_offset": st.basis_offset,
        "vantage_match_delta_ms": st.vantage_match_delta_ms,
        "vantage_match_candidate_price": st.vantage_match_candidate_price,
        "vantage_match_ratio": st.vantage_match_ratio,
        "vantage_match_reason": st.vantage_match_reason,
        "broker_candle_feed": st.broker_candle_feed,
        "broker_candle_age_seconds": round((now_ms() - st.broker_candle_ts) / 1000, 1) if st.broker_candle_ts else None,
        "basis_updated_ts": st.basis_updated_ts,
        "basis_samples": len(st.basis_ratios),
        "adjusted_levels": mapped,
        "reaction_levels": build_reaction_levels(st, tf),
        "a_plus_setups": strategy.get("setups", []),
        "structure_timeframes": strategy.get("timeframes", {}),
        "structure_zones": strategy.get("zones", []),
        "liquidity_levels": strategy.get("liquidity", []),
        "structure_events": strategy.get("events", []),
        "exchange_feed_fresh": exchange_fresh,
        "mark_age_seconds": round((now_ms() - st.mark_ts) / 1000, 1) if st.mark_ts else None,
        "book_age_seconds": round((now_ms() - st.book_ts) / 1000, 1) if st.book_ts else None,
        "analysis": analysis,
        "scores": scores,
        "events": list(st.recent_events)[:15],
        "commodity_session": st.latest_analysis.get("commodity_session"),
        "error": st.connection_error,
    }


async def bootstrap_market_data() -> None:
    """Load BTC and Gold history exclusively from Binance public market data."""
    assert http_client is not None

    for key, state in states.items():
        try:
            if key == "BTC":
                loaded = await load_btc_um_history(http_client, state.symbol, HISTORY_BARS)
            else:
                loaded = await load_binance_tradfi_gold_history(http_client, HISTORY_BARS)

            state.data_source = loaded.get("source") or (
                "Binance USD-M BTCUSDT public history" if key == "BTC"
                else "Binance Futures TradFi Perpetual XAUUSDT history"
            )
            state.connection_error = None
            for tf in TIMEFRAMES:
                rows = loaded["candles"].get(tf, [])
                # History first, then live bars so a just-closed websocket candle
                # wins if its timestamp overlaps the archive snapshot.
                merged = {int(item["t"]): item for item in rows}
                for item in state.candles[tf]:
                    merged[int(item["t"])] = item
                ordered = [merged[stamp] for stamp in sorted(merged)][-HISTORY_BARS:]
                state.candles[tf].clear()
                state.candles[tf].extend(ordered)
                state.latest_analysis[tf] = await asyncio.to_thread(state.analyse, tf)
                log.info(
                    "Loaded %s Binance historical/live candles for %s %s from %s",
                    len(state.candles[tf]), key, tf, state.data_source,
                )

            one_minute = list(state.candles.get("1m", []))
            if one_minute:
                latest = one_minute[-1]
                state.mark_price = float(latest["c"])
                state.index_price = state.mark_price if key == "BTC" else None
                state.mark_ts = int(latest["t"]) + 60_000
                state.exchange_prices.append((state.mark_ts, state.mark_price))

        except Exception as exc:
            state.connection_error = f"Binance historical data unavailable: {str(exc)[:160]}"
            if key == "GOLD":
                state.data_source = (
                    "Binance Futures TradFi Perpetual XAUUSDT live stream; "
                    "historical Binance archive unavailable"
                )
            log.exception("Binance historical bootstrap failed for %s", key)

    # Binance Futures open-interest history is only meaningful for BTCUSDT here.
    btc = states.get("BTC")
    if btc:
        try:
            response = await http_client.get(
                f"{REST_BASE}/futures/data/openInterestHist",
                params={"symbol": btc.symbol, "period": "5m", "limit": 30},
                timeout=10.0,
            )
            response.raise_for_status()
            for row in response.json():
                ts = int(row.get("timestamp", 0))
                value = safe_float(row.get("sumOpenInterest"))
                if ts and value is not None:
                    btc.oi_history.append((ts, value))
        except Exception as exc:
            log.warning("BTC OI history bootstrap unavailable: %s", exc)


async def gold_market_worker() -> None:
    """Refresh Gold only from Binance XAUUSDT TradFi Perpetual market data."""
    assert http_client is not None
    state = states["GOLD"]
    while True:
        try:
            if state.broker_candle_feed:
                await asyncio.sleep(30)
                continue

            stream_fresh = (
                state.binance_tradfi_stream_active
                and state.binance_tradfi_last_update_ts is not None
                and now_ms() - state.binance_tradfi_last_update_ts <= 20_000
            )
            if stream_fresh:
                state.connection_error = None
                await asyncio.sleep(10)
                continue

            loaded = await load_binance_tradfi_gold_live(http_client)

            # A WebSocket packet may have arrived while the REST request was in flight.
            if (
                state.binance_tradfi_stream_active
                and state.binance_tradfi_last_update_ts is not None
                and now_ms() - state.binance_tradfi_last_update_ts <= 20_000
            ):
                continue

            data = loaded["candles"]
            for tf, rows in data.items():
                if tf not in state.candles:
                    continue
                merged = {int(item["t"]): item for item in state.candles[tf]}
                for item in rows:
                    merged[int(item["t"])] = item
                ordered = [merged[key] for key in sorted(merged)][-HISTORY_BARS:]
                state.candles[tf].clear()
                state.candles[tf].extend(ordered)
                state.latest_analysis[tf] = await asyncio.to_thread(state.analyse, tf)

            latest = loaded["latest"]
            state.mark_price = float(latest["c"])
            state.index_price = None
            state.mark_ts = int(latest["t"]) + 60_000
            state.exchange_prices.append((state.mark_ts, state.mark_price))
            state.data_source = loaded["source"]
            state.connection_error = None
            state.binance_tradfi_last_update_ts = now_ms()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.connection_error = f"Binance XAUUSDT TradFi feed stale; retaining last Binance candles: {str(exc)[:120]}"
            log.warning("Binance TradFi GOLD refresh failed; keeping last Binance bars: %s", exc)
        await asyncio.sleep(30)


async def market_stream_worker() -> None:
    streams: list[str] = ["!forceOrder@arr"]
    for symbol in (states["BTC"].symbol,):
        s = symbol.lower()
        streams.extend([
            f"{s}@aggTrade",
            f"{s}@markPrice@1s",
            f"{s}@kline_1m",
            f"{s}@kline_5m",
            f"{s}@kline_15m",
            f"{s}@kline_30m",
            f"{s}@kline_1h",
            f"{s}@kline_4h",
            f"{s}@kline_1d",
        ])
    url = MARKET_WS_BASE + "?streams=" + "/".join(streams)

    while True:
        try:
            log.info("Connecting Binance USD-M BTC market streams")
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=8_000_000) as ws:
                states["BTC"].connection_error = None
                async for raw in ws:
                    packet = json.loads(raw)
                    data = packet.get("data", packet)
                    event = data.get("e")
                    if event == "markPriceUpdate":
                        symbol = data.get("s", "")
                        state = next((x for x in states.values() if x.symbol == symbol), None)
                        if state:
                            state.mark_price = safe_float(data.get("p"))
                            state.index_price = safe_float(data.get("i"))
                            state.funding_rate = safe_float(data.get("r"))
                            state.next_funding_time = int(data.get("T", 0)) or None
                            state.mark_ts = int(data.get("E", now_ms()))
                            if state.mark_price:
                                state.exchange_prices.append((state.mark_ts, state.mark_price))
                                state.last_message_ts = now_ms()
                    elif event == "aggTrade":
                        symbol = data.get("s", "")
                        state = next((x for x in states.values() if x.symbol == symbol), None)
                        if state:
                            price = safe_float(data.get("p"), 0.0) or 0.0
                            qty = safe_float(data.get("nq", data.get("q")), 0.0) or 0.0
                            # m=true means buyer was maker, therefore seller was taker.
                            signed = -(price * qty) if bool(data.get("m")) else (price * qty)
                            state.add_trade(int(data.get("T", now_ms())), signed)
                            state.last_message_ts = now_ms()
                    elif event == "kline":
                        symbol = data.get("s", "")
                        state = next((x for x in states.values() if x.symbol == symbol), None)
                        if state:
                            k = data.get("k", {})
                            tf = str(k.get("i", ""))
                            if tf in TIMEFRAMES and k.get("x"):
                                analysis = state.add_candle(tf, k)
                                if analysis and analysis.get("ready"):
                                    if analysis.get("upper_sweep"):
                                        await publish_event(state.key, "UPPER_LIQUIDITY_SWEEP", "Price wicked above recent range high and closed back inside; breakout buyers may be trapped.", analysis.get("last_close"), tf)
                                    if analysis.get("lower_sweep"):
                                        await publish_event(state.key, "LOWER_LIQUIDITY_SWEEP", "Price wicked below recent range low and closed back inside; breakout sellers may be trapped.", analysis.get("last_close"), tf)
                                    if analysis.get("range_break_down"):
                                        await publish_event(state.key, "BEARISH_RANGE_EXPANSION", "Strong candle closed below the prior range; possible long-stop run / downside expansion.", analysis.get("last_close"), tf)
                                    if analysis.get("range_break_up"):
                                        await publish_event(state.key, "BULLISH_RANGE_EXPANSION", "Strong candle closed above the prior range; possible short-stop run / upside expansion.", analysis.get("last_close"), tf)
                    elif event == "forceOrder":
                        order = data.get("o", {})
                        symbol = order.get("s", "")
                        state = next((x for x in states.values() if x.symbol == symbol), None)
                        if state:
                            side = str(order.get("S", ""))
                            qty = safe_float(order.get("z", order.get("q")), 0.0) or 0.0
                            price = safe_float(order.get("ap", order.get("p")), 0.0) or 0.0
                            state.liquidation_events.append((int(order.get("T", now_ms())), side, qty * price, price))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("BTC Binance market stream disconnected: %s", exc)
            states["BTC"].connection_error = str(exc)[:180]
            await asyncio.sleep(3)


async def gold_tradfi_stream_worker() -> None:
    """Subscribe separately to Binance XAUUSDT TradFi perpetual continuous klines."""
    gold_pair = states["GOLD"].symbol.lower()
    streams = [
        f"{gold_pair}_tradifi_perpetual@continuousKline_{timeframe}"
        for timeframe in TIMEFRAMES
    ]
    url = MARKET_WS_BASE + "?streams=" + "/".join(streams)
    state = states["GOLD"]
    logged_timeframes: set[str] = set()

    while True:
        try:
            log.info("Connecting Binance Gold TradFi continuous kline streams: %s", ", ".join(TIMEFRAMES))
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=8_000_000) as ws:
                async for raw in ws:
                    packet = json.loads(raw)
                    data = packet.get("data", packet)
                    if data.get("e") != "continuous_kline":
                        continue

                    pair = str(data.get("ps") or "").upper()
                    contract_type = str(data.get("ct") or "").upper()
                    if pair != state.symbol or contract_type != "TRADIFI_PERPETUAL":
                        continue

                    k = data.get("k") or {}
                    timeframe = str(k.get("i") or "")
                    price = safe_float(k.get("c"))
                    event_time = int(data.get("E") or now_ms())
                    first_binance_packet = not state.binance_tradfi_stream_active
                    state.binance_tradfi_last_update_ts = now_ms()
                    state.last_message_ts = now_ms()
                    if first_binance_packet:
                        log.info(
                            "Received Binance XAUUSDT TradFi continuous-kline packet: timeframe=%s close=%s contractType=%s",
                            timeframe, price, contract_type,
                        )
                    if timeframe not in logged_timeframes:
                        logged_timeframes.add(timeframe)
                        log.info("First Binance Gold %s stream update received: close=%s candle_closed=%s", timeframe, price, bool(k.get("x")))
                    if k.get("x") and timeframe in TIMEFRAMES:
                        log.info("Binance Gold %s closed candle received; current stored bars=%s", timeframe, len(state.candles.get(timeframe, [])) + 1)

                    if state.broker_candle_feed:
                        # Vantage native XAUUSD broker OHLC remains authoritative.
                        state.binance_tradfi_stream_active = True
                        continue

                    if not state.binance_tradfi_stream_active:
                        state.data_source = "Binance Futures TradFi Perpetual XAUUSDT continuousKline stream"

                    state.binance_tradfi_stream_active = True
                    state.connection_error = None
                    if price is not None and price > 0:
                        state.mark_price = price
                        state.index_price = None
                        state.mark_ts = event_time
                        state.exchange_prices.append((event_time, price))

                    if k.get("x") and timeframe in TIMEFRAMES:
                        analysis = state.add_candle(timeframe, k)
                        if analysis and analysis.get("ready"):
                            if analysis.get("upper_sweep"):
                                await publish_event(state.key, "UPPER_LIQUIDITY_SWEEP", "Price wicked above recent range high and closed back inside; breakout buyers may be trapped.", analysis.get("last_close"), timeframe)
                            if analysis.get("lower_sweep"):
                                await publish_event(state.key, "LOWER_LIQUIDITY_SWEEP", "Price wicked below recent range low and closed back inside; breakout sellers may be trapped.", analysis.get("last_close"), timeframe)
                            if analysis.get("range_break_down"):
                                await publish_event(state.key, "BEARISH_RANGE_EXPANSION", "Strong candle closed below the prior range; possible long-stop run / downside expansion.", analysis.get("last_close"), timeframe)
                            if analysis.get("range_break_up"):
                                await publish_event(state.key, "BULLISH_RANGE_EXPANSION", "Strong candle closed above the prior range; possible short-stop run / upside expansion.", analysis.get("last_close"), timeframe)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.binance_tradfi_stream_active = False
            if not state.broker_candle_feed:
                state.connection_error = f"Binance XAUUSDT TradFi stream: {str(exc)[:140]}"
            log.exception("Gold TradFi continuous-kline stream disconnected: %s", exc)
            await asyncio.sleep(5)




async def depth_stream_worker() -> None:
    streams = [f"{states['BTC'].symbol.lower()}@depth20@100ms"]
    url = PUBLIC_WS_BASE + "?streams=" + "/".join(streams)
    while True:
        try:
            log.info("Connecting Binance partial order-book streams")
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=8_000_000) as ws:
                async for raw in ws:
                    packet = json.loads(raw)
                    data = packet.get("data", packet)
                    if data.get("e") != "depthUpdate":
                        continue
                    symbol = data.get("s", "")
                    state = next((x for x in states.values() if x.symbol == symbol), None)
                    if state:
                        state.update_book(data)
                        state.last_depth_update = now_ms()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Depth stream disconnected: %s", exc)
            await asyncio.sleep(3)


async def rest_metrics_worker() -> None:
    assert http_client is not None
    while True:
        for key, state in states.items():
            if key != "BTC":
                continue
            try:
                oi = await http_client.get(f"{REST_BASE}/fapi/v1/openInterest", params={"symbol": state.symbol}, timeout=8.0)
                oi.raise_for_status()
                payload = oi.json()
                value = safe_float(payload.get("openInterest"))
                if value is not None:
                    state.open_interest = value
                    state.oi_ts = int(payload.get("time", now_ms()))
                    state.oi_history.append((state.oi_ts, value))

                lsr = await http_client.get(
                    f"{REST_BASE}/futures/data/globalLongShortAccountRatio",
                    params={"symbol": state.symbol, "period": "5m", "limit": 1},
                    timeout=8.0,
                )
                lsr.raise_for_status()
                rows = lsr.json()
                if rows:
                    row = rows[-1]
                    state.long_short_ratio = safe_float(row.get("longShortRatio"))
                    state.long_account = safe_float(row.get("longAccount"))
                    state.short_account = safe_float(row.get("shortAccount"))
                    state.lsr_ts = int(row.get("timestamp", now_ms()))

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("REST metric update failed for %s: %s", key, exc)
        await asyncio.sleep(30)


def record_vantage_heartbeat(payload: dict[str, Any]) -> dict[str, Any]:
    raw_asset = str(payload.get("asset", "")).upper()
    ticker = str(payload.get("symbol", payload.get("ticker", "")))
    if raw_asset in states:
        key = raw_asset
    else:
        combined = (raw_asset + " " + ticker).upper()
        if "BTC" in combined:
            key = "BTC"
        elif "XAU" in combined or "GOLD" in combined:
            key = "GOLD"
        else:
            raise ValueError("Could not map TradingView ticker to BTC or GOLD. Set the Pine bridge Asset Override.")

    bid = safe_float(payload.get("bid"))
    ask = safe_float(payload.get("ask"))
    price = safe_float(payload.get("price"))
    if price is None:
        price = bid if bid is not None else ((bid + ask) / 2.0 if bid is not None and ask is not None else None)
    if price is None or price <= 0:
        raise ValueError("Invalid Vantage MT5 price")

    state = states[key]
    tick_time = int(payload.get("time_msc", now_ms()))
    if tick_time < 10_000_000_000:
        tick_time *= 1000
    state.vantage_price = price
    state.vantage_bid = bid
    state.vantage_ask = ask
    state.vantage_spread = (ask - bid) if bid is not None and ask is not None else None
    state.vantage_high = None
    state.vantage_low = None
    state.vantage_ts = now_ms()
    state.vantage_ticker = ticker[:120]
    state.vantage_tf = "MT5 TICK"

    if key == "GOLD" and state.broker_candle_feed:
        state.mark_price = price
        state.index_price = price
        state.mark_ts = now_ms()
        state.vantage_match_reason = "Direct Vantage MT5 broker feed"
        return {
            "asset": key,
            "vantage_price": price,
            "vantage_bid": bid,
            "vantage_ask": ask,
            "direct_feed": True,
            "broker_candle_feed": True,
            "matched": True,
            "basis_samples": len(state.basis_ratios),
            "match_reason": state.vantage_match_reason,
        }

    closest = min(state.exchange_prices, key=lambda x: abs(x[0] - tick_time), default=None)
    match_delta_ms = abs(closest[0] - tick_time) if closest else None
    matched = bool(closest and match_delta_ms is not None and match_delta_ms <= 15_000)
    ratio = (price / closest[1]) if closest and closest[1] > 0 else None

    state.vantage_match_delta_ms = match_delta_ms
    state.vantage_match_candidate_price = closest[1] if closest else None
    state.vantage_match_ratio = ratio
    if closest is None:
        state.vantage_match_reason = "No exchange tick available"
    elif not matched:
        state.vantage_match_reason = "Timestamp gap exceeds 15 seconds"
    elif ratio is None or not (0.90 <= ratio <= 1.10):
        state.vantage_match_reason = "Price ratio outside 0.90-1.10 range"
    else:
        state.vantage_match_reason = "Matched"

    if matched and ratio is not None and 0.90 <= ratio <= 1.10:
        state.basis_ratios.append(ratio)
        state.basis_ratio = median_or_none(state.basis_ratios)
        state.basis_offset = price - closest[1]
        state.basis_updated_ts = now_ms()

    return {
        "asset": key,
        "vantage_price": price,
        "vantage_bid": bid,
        "vantage_ask": ask,
        "matched_exchange_price": closest[1] if closest else None,
        "basis_ratio": state.basis_ratio,
        "basis_samples": len(state.basis_ratios),
        "match_delta_ms": match_delta_ms,
        "match_reason": state.vantage_match_reason,
        "matched": matched,
    }



def record_vantage_candles(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace/refresh GOLD analysis candles with native Vantage MT5 XAUUSD OHLC."""
    asset = str(payload.get("asset", "")).upper()
    symbol = str(payload.get("symbol", "")).strip()
    if asset != "GOLD" or "XAU" not in symbol.upper():
        raise ValueError("Native broker candle endpoint accepts GOLD XAU symbols only")

    frames = payload.get("timeframes")
    if not isinstance(frames, dict):
        raise ValueError("timeframes must be an object keyed by supported timeframe")

    state = states["GOLD"]
    replace_existing = bool(payload.get("replace", False))
    if not replace_existing and not state.broker_candle_feed:
        # Render restarts clear in-memory OHLC history while the Windows bridge
        # may remain running and keep sending only its incremental 2-bar sync.
        # Ask the bridge for a full bootstrap instead of treating 2 bars as history.
        return {
            "asset": "GOLD",
            "symbol": symbol,
            "broker_candle_feed": False,
            "needs_bootstrap": True,
            "reason": "Backend has no broker candle history; send full OHLC bootstrap.",
            "bars": {},
        }
    if replace_existing:
        for candles in state.candles.values():
            candles.clear()

    received_counts: dict[str, int] = {}
    for timeframe, rows in frames.items():
        if timeframe not in TIMEFRAMES or not isinstance(rows, list):
            continue

        merged = {} if replace_existing else {int(item["t"]): item for item in state.candles[timeframe]}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                stamp = int(row.get("t", 0))
            except (TypeError, ValueError):
                continue
            if stamp <= 0:
                continue
            if stamp < 10_000_000_000:
                stamp *= 1000

            o = safe_float(row.get("o"))
            h = safe_float(row.get("h"))
            low = safe_float(row.get("l"))
            close = safe_float(row.get("c"))
            volume = safe_float(row.get("v"), 0.0)
            if None in (o, h, low, close) or min(o, h, low, close) <= 0:
                continue
            if h < max(o, close, low) or low > min(o, close, h):
                continue
            merged[stamp] = {
                "t": stamp,
                "o": float(o),
                "h": float(h),
                "l": float(low),
                "c": float(close),
                "v": max(0.0, float(volume or 0.0)),
            }

        ordered = [merged[key] for key in sorted(merged)][-HISTORY_BARS:]
        if ordered:
            state.candles[timeframe].clear()
            state.candles[timeframe].extend(ordered)
            state.latest_analysis[timeframe] = state.analyse(timeframe)
            received_counts[timeframe] = len(ordered)

    if not received_counts:
        raise ValueError("No valid Vantage MT5 OHLC candles were received")

    state.broker_candle_feed = True
    state.broker_candle_ts = now_ms()
    state.data_source = f"Vantage MT5 {symbol} broker OHLC (direct feed)"
    state.connection_error = None
    return {
        "asset": "GOLD",
        "symbol": symbol,
        "broker_candle_feed": True,
        "timeframes_received": sorted(received_counts),
        "bars": received_counts,
        "updated_at": state.broker_candle_ts,
    }


async def initialize_market_data_and_workers() -> None:
    """Connect live market streams immediately, then backfill history in parallel."""
    background_tasks.extend([
        asyncio.create_task(market_stream_worker(), name="binance-market"),
        asyncio.create_task(gold_tradfi_stream_worker(), name="binance-gold-tradfi"),
        asyncio.create_task(depth_stream_worker(), name="binance-depth"),
        asyncio.create_task(rest_metrics_worker(), name="binance-rest-metrics"),
        asyncio.create_task(gold_market_worker(), name="gold-reference-feed"),
    ])
    log.info("AST Liquidity Radar live market workers started; Binance history backfill running in parallel")

    try:
        await bootstrap_market_data()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Historical bootstrap failed; live market streams remain active")

    # Strategy replay runs off the request path. The dashboard API returns the
    # latest completed cache immediately, even while a 25k-bar replay is warming.
    background_tasks.append(
        asyncio.create_task(campaign_snapshot_worker(), name="campaign-snapshot-cache")
    )



@app.on_event("startup")
async def startup() -> None:
    global http_client
    http_client = httpx.AsyncClient(headers={"User-Agent": "AST-Liquidity-Radar/1.0"})
    # Do not block Render's startup health check while archive candles are downloaded.
    bootstrap_task = asyncio.create_task(
        initialize_market_data_and_workers(),
        name="historical-bootstrap",
    )
    background_tasks.append(bootstrap_task)
    if CAMPAIGN_ALERTS_ENABLED:
        alert_task = asyncio.create_task(campaign_alert_worker(), name="campaign-telegram-alerts")
        background_tasks.append(alert_task)
        log.info("Campaign Telegram alerts enabled; monitoring every %.1f seconds", CAMPAIGN_ALERT_POLL_SECONDS)
    else:
        log.info("Campaign Telegram alerts disabled; configure ENABLE_TELEGRAM, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    log.info("AST Liquidity Radar API ready; historical bootstrap running in background")


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in background_tasks:
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    if http_client:
        await http_client.aclose()


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "running",
        "server_time": now_ms(),
        "symbols": SYMBOLS,
        "market_stream_connected": any(s.mark_ts and now_ms() - s.mark_ts < 15_000 for s in states.values()),
        "telegram_enabled": ENABLE_TELEGRAM and bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "campaign_alerts_enabled": CAMPAIGN_ALERTS_ENABLED,
        "campaign_alert_poll_seconds": CAMPAIGN_ALERT_POLL_SECONDS if CAMPAIGN_ALERTS_ENABLED else None,
        "vantage_mt5_bridge_secret_configured": bool(BRIDGE_SECRET and BRIDGE_SECRET != "replace-this-secret"),
        "data_sources": {
            key: {
                "source": state.data_source,
                "bars": {tf: len(state.candles[tf]) for tf in TIMEFRAMES},
                "error": state.connection_error,
                "binance_tradfi_stream_active": state.binance_tradfi_stream_active,
                "binance_tradfi_feed_age_seconds": round((now_ms() - state.binance_tradfi_last_update_ts) / 1000, 1) if state.binance_tradfi_last_update_ts else None,
                "vantage_broker_candles_active": state.broker_candle_feed,
            }
            for key, state in states.items()
        },
    }


@app.get("/api/snapshot")
async def snapshot(timeframe: str = Query("5m", pattern="^(1m|5m|15m|30m|1h|4h|1d)$")) -> dict[str, Any]:
    return {"time": now_ms(), "timeframe": timeframe, "assets": {key: make_snapshot(key, timeframe) for key in states}}


@app.post("/api/vantage/mt5")
async def vantage_mt5_bridge(request: Request) -> JSONResponse:
    if not BRIDGE_SECRET or BRIDGE_SECRET == "replace-this-secret":
        raise HTTPException(status_code=503, detail="Set a unique VANTAGE_BRIDGE_SECRET in the server .env first")
    supplied_secret = request.headers.get("X-AST-Bridge-Secret", "")
    if not hmac.compare_digest(supplied_secret, BRIDGE_SECRET):
        raise HTTPException(status_code=401, detail="Invalid bridge secret")
    try:
        payload = await request.json()
        result = record_vantage_heartbeat(payload)
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Vantage MT5 bridge failed")
        raise HTTPException(status_code=400, detail="Invalid MT5 tick payload") from exc



@app.post("/api/vantage/mt5/candles")
async def vantage_mt5_candles(request: Request) -> JSONResponse:
    if not BRIDGE_SECRET or BRIDGE_SECRET == "replace-this-secret":
        raise HTTPException(status_code=503, detail="Set a unique VANTAGE_BRIDGE_SECRET in the server .env first")
    supplied_secret = request.headers.get("X-AST-Bridge-Secret", "")
    if not hmac.compare_digest(supplied_secret, BRIDGE_SECRET):
        raise HTTPException(status_code=401, detail="Invalid bridge secret")
    try:
        payload = await request.json()
        result = record_vantage_candles(payload)
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Vantage MT5 candle sync failed")
        raise HTTPException(status_code=400, detail="Invalid MT5 candle payload") from exc


_campaign_scanner_cache: dict[str, Any] | None = None
_campaign_scanner_cache_at = 0.0
_campaign_scanner_cache_lock: asyncio.Lock | None = None


async def refresh_campaign_scanner_cache() -> dict[str, Any]:
    """Build point-in-time strategy snapshots in a background worker."""
    global _campaign_scanner_cache, _campaign_scanner_cache_at, _campaign_scanner_cache_lock
    if _campaign_scanner_cache_lock is None:
        _campaign_scanner_cache_lock = asyncio.Lock()

    async with _campaign_scanner_cache_lock:
        loop = asyncio.get_running_loop()
        assets: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for state in states.values():
            result = await asyncio.to_thread(
                state.campaign_engine.snapshot,
                state.candles,
                state.key,
            )
            results.append(result)
            # Yield between assets so HTTP health checks and websocket tasks
            # are serviced between the two CPU-heavy strategy replays.
            await asyncio.sleep(0)
        for state, result in zip(states.values(), results):
            result["data_source"] = state.data_source or "Source not reported"
            result["broker_candle_feed"] = bool(state.broker_candle_feed)
            result["broker_candle_time"] = state.broker_candle_ts
            result["live_reference_price"] = state.mark_price
            assets.append(result)

        _campaign_scanner_cache = {
            "time": now_ms(),
            "strategy_name": "AST Multi-Timeframe Pyramiding Campaign",
            "assets": assets,
        }
        _campaign_scanner_cache_at = loop.time()
        return _campaign_scanner_cache


async def campaign_snapshot_worker() -> None:
    """Keep the heavy replay warm away from browser/API request threads."""
    while True:
        try:
            await refresh_campaign_scanner_cache()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Campaign snapshot cache refresh failed")
        await asyncio.sleep(1.0)


@app.get("/api/campaign")
async def campaign_scanner() -> dict[str, Any]:
    """Return the latest cached campaign state without waiting for a 25k-bar replay."""
    if _campaign_scanner_cache is not None:
        return _campaign_scanner_cache
    return {
        "time": now_ms(),
        "strategy_name": "AST Multi-Timeframe Pyramiding Campaign",
        "assets": [],
        "status": "WARMING UP",
    }


@app.websocket("/ws/prices")
async def live_price_stream(websocket: WebSocket) -> None:
    """Push tiny in-memory BTC/Gold quote snapshots to browsers at up to 10 Hz."""
    await websocket.accept()
    try:
        while True:
            stamp = now_ms()
            payload = {
                "type": "prices",
                "server_time": stamp,
                "assets": {
                    key: {
                        "price": state.mark_price,
                        "mark_ts": state.mark_ts,
                        "source": (
                            "VANTAGE LIVE" if key == "GOLD" and state.broker_candle_feed
                            else "XAUUSDT LIVE" if key == "GOLD"
                            else "BINANCE LIVE"
                        ),
                        "fresh": bool(state.mark_ts and stamp - state.mark_ts <= 15_000),
                    }
                    for key, state in states.items()
                },
            }
            await websocket.send_json(payload)
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("Live price websocket closed", exc_info=True)
        return



def _campaign_alert_price(asset: str, value: Any) -> str:
    if value is None:
        return "—"
    try:
        digits = 1 if asset == "BTC" else 3
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _campaign_alert_quantity(value: Any) -> str:
    if value is None:
        return "NOT CONFIGURED"
    try:
        return f"{float(value):,.5f}"
    except (TypeError, ValueError):
        return "NOT CONFIGURED"


def _campaign_setup_alert_id(asset: str, execution: dict[str, Any]) -> str:
    signal_time = execution.get("signal_time") or execution.get("signal_close") or execution.get("entry_price")
    return f"setup:{signal_time}:{execution.get('kind')}:{execution.get('side')}"


def _seed_campaign_alert_state(asset: str, result: dict[str, Any]) -> None:
    """Avoid sending stale alerts for positions already present when the worker starts."""
    seen = campaign_alert_seen[asset]
    execution = result.get("execution_1m") or {}
    if execution.get("status") == "ENTRY READY · NEXT 1M OPEN":
        seen.add(_campaign_setup_alert_id(asset, execution))
    campaign = result.get("campaign") or {}
    for position in campaign.get("positions", []):
        position_id = str(position.get("id") or "")
        if position_id:
            seen.add(f"fill:{position_id}")
            if position.get("partial_taken"):
                seen.add(f"partial:{position_id}")
    for position in result.get("recent_closed", []):
        position_id = str(position.get("id") or "")
        if position_id:
            seen.add(f"closed:{position_id}")


async def campaign_alert_worker() -> None:
    """Monitor strategy state on the server and send Telegram alerts independent of the dashboard tab."""
    bootstrapped_assets: set[str] = set()
    while True:
        try:
            payload = await campaign_scanner()
            for result in payload.get("assets", []):
                asset = str(result.get("asset") or "MARKET").upper()
                replay = result.get("replay") or {}
                if asset not in bootstrapped_assets:
                    if int(replay.get("bars_available") or 0) > 0:
                        _seed_campaign_alert_state(asset, result)
                        bootstrapped_assets.add(asset)
                    continue

                seen = campaign_alert_seen[asset]
                execution = result.get("execution_1m") or {}
                if execution.get("status") == "ENTRY READY · NEXT 1M OPEN":
                    alert_id = _campaign_setup_alert_id(asset, execution)
                    if alert_id not in seen:
                        side = execution.get("side") or "TRADE"
                        message = (
                            f"AST FOUR-ENGINE CAMPAIGN | {asset}\n"
                            f"{side} {execution.get('kind') or 'ENTRY'} SETUP CONFIRMED · 1M\n"
                            f"Planned Entry: {_campaign_alert_price(asset, execution.get('entry_price'))}\n"
                            f"Stop Loss: {_campaign_alert_price(asset, execution.get('stop_loss'))}\n"
                            f"Partial TP (1R): {_campaign_alert_price(asset, execution.get('partial_take_profit'))}\n"
                            f"Final TP: {_campaign_alert_price(asset, execution.get('take_profit'))}\n"
                            f"Score: {execution.get('score', '—')} | Target: {execution.get('target_r', '—')}R\n"
                            "Status: Waiting for next 1m open validation.\n"
                            "SIMULATED PLAN ONLY — no broker order is placed."
                        )
                        if await send_telegram(message):
                            seen.add(alert_id)

                campaign = result.get("campaign") or {}
                for position in campaign.get("positions", []):
                    position_id = str(position.get("id") or "")
                    if not position_id:
                        continue
                    fill_id = f"fill:{position_id}"
                    if fill_id not in seen:
                        message = (
                            f"AST FOUR-ENGINE CAMPAIGN | {asset}\n"
                            f"{position.get('side', 'TRADE')} {position.get('kind', 'ENTRY')} · SIMULATED FILL CONFIRMED\n"
                            f"Entry: {_campaign_alert_price(asset, position.get('entry_price'))}\n"
                            f"SL: {_campaign_alert_price(asset, position.get('stop_loss'))}\n"
                            f"Partial TP: {_campaign_alert_price(asset, position.get('partial_take_profit'))}\n"
                            f"Final TP: {_campaign_alert_price(asset, position.get('take_profit'))}\n"
                            f"Quantity: {_campaign_alert_quantity(position.get('quantity'))}\n"
                            "SIMULATED FILL ONLY — this dashboard does not place a broker order."
                        )
                        if await send_telegram(message):
                            seen.add(fill_id)

                    partial_id = f"partial:{position_id}"
                    if position.get("partial_taken") and partial_id not in seen:
                        message = (
                            f"AST CAMPAIGN UPDATE | {asset}\n"
                            f"{position.get('side', 'TRADE')} PARTIAL TARGET HIT\n"
                            "50% partial booking simulated at 1R; remaining stop moved to breakeven."
                        )
                        if await send_telegram(message):
                            seen.add(partial_id)

                for position in result.get("recent_closed", []):
                    position_id = str(position.get("id") or "")
                    close_id = f"closed:{position_id}"
                    if not position_id or close_id in seen:
                        continue
                    message = (
                        f"AST CAMPAIGN EXIT | {asset}\n"
                        f"{position.get('side', 'TRADE')} · {position.get('status', 'CLOSED')}\n"
                        f"Entry: {_campaign_alert_price(asset, position.get('entry_price'))}\n"
                        f"Exit: {_campaign_alert_price(asset, position.get('exit_price'))}\n"
                        f"Result: {position.get('result_r', '—')}R\n"
                        "This is a simulated strategy result, not a broker execution."
                    )
                    if await send_telegram(message):
                        seen.add(close_id)

            await asyncio.sleep(CAMPAIGN_ALERT_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Campaign Telegram alert worker failed")
            await asyncio.sleep(max(5.0, CAMPAIGN_ALERT_POLL_SECONDS))


@app.get("/api/events")
async def events() -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for state in states.values():
        combined.extend(list(state.recent_events))
    return sorted(combined, key=lambda x: x["time"], reverse=True)[:50]


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return {
        "symbols": SYMBOLS,
        "timeframes": TIMEFRAMES,
        "historical_bars_per_timeframe": HISTORY_BARS,
        "strategy": "AST Four-Engine Campaign Architecture: MBIE market intelligence -> Multi-Timeframe context -> 1m Main/Add-on Entry Engine -> Campaign risk and position management.",
        "engine_architecture": {
            "MBIE": "Market regime, structure, liquidity, auction profile proxy, order-flow availability, zones, momentum, volatility, scenarios and data coverage. No entry signal generation.",
            "Multi-Timeframe Engine": "Combines completed Monthly/Weekly/Daily/4H/1H/30M/15M/5M context without overwriting individual timeframe readings.",
            "Entry Engine": "Generates 1m MAIN and ADD-ON candidates only; cannot set quantity, stop loss or take profit.",
            "Campaign Engine": "Owns quantity estimation, SL, TP, total campaign risk, add-on protection, partial exits and campaign invalidation.",
        },
        "trend_lock_mode": "independent per asset; lock flips only on a newer displacement-backed MAJOR CHOCH",
        "pivot_confirmation": "major 5-left/5-right and minor 2-left/2-right; pivots are usable only after right bars close",
        "price_mapping": "Rolling median ratio from direct Vantage MT5 tick / nearest Binance mark price",
        "telegram_configured": ENABLE_TELEGRAM and bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "notes": [
            "Structure setups are rule-based and educational, not probabilities or guaranteed signals.",
            "Origin zones are candle-derived candidate supply/demand blocks; retest counts are closed-candle touches, not verified institutional order flow.",
            "Major/minor pivots require 5/2 right-side closed candles respectively, so signals intentionally confirm after the pivot forms.",
            "BTC market data uses Binance USD-M; GOLD historical and live market data use Binance XAUUSDT TRADIFI_PERPETUAL only.",
            "Binance TradFi Gold continuous-kline streams provide OHLCV; OI and order-book metrics remain unavailable unless separately connected.",
            "The Vantage MT5 bridge remains a separate read-only broker feed and takes authority over GOLD candles only after native broker OHLC bootstrap.",
            "The exchange feed does not reveal every trader's stop or exact liquidation price.",
            "Campaign engine replays completed 1m candles sequentially; higher-timeframe candles are exposed only after their own close timestamp.",
            "1W and 1M context are aggregated from completed daily candles; no incomplete calendar period is used for confirmation.",
            "1m plans are emitted after a completed signal candle and simulated at the next 1m open; the backend does not place broker orders.",
            "Campaign defaults: 0.20% risk allocation per position, 1.50% maximum campaign risk, 10 concurrent positions and 2.00% daily loss pause; configure with AST_CAMPAIGN_* environment variables.",
            "Quantity is only estimated when AST_CAMPAIGN_ACCOUNT_EQUITY and the asset-specific AST_CAMPAIGN_BTC_VALUE_PER_PRICE_UNIT or AST_CAMPAIGN_GOLD_VALUE_PER_PRICE_UNIT are configured; otherwise quantity is explicitly unavailable.",
            "Add-on entries require same-side campaign, favorable extension and all existing open stops protected at breakeven or better.",
        ],
    }
