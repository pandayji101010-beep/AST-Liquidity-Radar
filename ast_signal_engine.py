from __future__ import annotations

import math
import time
from typing import Any

TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
    "1d": 86_400_000,
}
TREND_WEIGHTS = {
    "5m": 1.0, "15m": 1.5, "30m": 2.0,
    "1h": 3.0, "4h": 4.0, "1d": 5.0,
}
# Pip display convention: BTC $1.00, XAUUSDT $0.01. Verify broker contract specs.
PIP_SIZE = {"BTC": 1.0, "GOLD": 0.01}
MIN_TARGET_PIPS = 90.0
MIN_MOVE_POTENTIAL_PIPS = 85.0
MIN_ALIGNMENT = 52.0
MIN_READY_DIRECTIONAL_FRAMES = 2


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _atr(rows: list[dict[str, Any]], period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    ranges: list[float] = []
    start = max(1, len(rows) - period)
    for index in range(start, len(rows)):
        row = rows[index]
        previous_close = _num(rows[index - 1].get("c"))
        high, low = _num(row.get("h")), _num(row.get("l"))
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(ranges) / len(ranges) if ranges else 0.0


def _closed(rows: Any, timeframe: str, as_of_ms: int) -> list[dict[str, Any]]:
    interval = TF_MS[timeframe]
    result = []
    for row in rows or []:
        stamp = int(_num(row.get("t")))
        if stamp > 0 and stamp + interval <= as_of_ms:
            result.append(row)
    return result


def _trend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) < 55:
        return {"bias": "NEUTRAL", "ready": False, "strength": 0.0, "bars": len(rows)}
    closes = [_num(row.get("c")) for row in rows if _num(row.get("c")) > 0]
    if len(closes) < 55:
        return {"bias": "NEUTRAL", "ready": False, "strength": 0.0, "bars": len(closes)}
    fast = _ema(closes[-100:], 20)
    slow = _ema(closes[-180:], 50)
    fast_previous = _ema(closes[-105:-5], 20)
    atr = _atr(rows[-40:])
    rising = fast > fast_previous
    falling = fast < fast_previous
    if fast > slow and closes[-1] >= fast and rising:
        bias = "BULLISH"
    elif fast < slow and closes[-1] <= fast and falling:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    strength = min(100.0, abs(fast - slow) / atr * 100.0) if atr > 0 else 0.0
    return {
        "bias": bias, "ready": True, "strength": round(strength, 1),
        "ema20": fast, "ema50": slow, "bars": len(closes),
    }


def _setup(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """More responsive 1m continuation/reversal triggers using completed candles only."""
    if len(rows) < 25:
        return None
    last = rows[-1]
    previous = rows[-2]
    short_history = rows[-6:-1]
    sweep_history = rows[-13:-1]
    op, high, low, close = (_num(last.get(k)) for k in ("o", "h", "l", "c"))
    prev_open, prev_high, prev_low, prev_close = (
        _num(previous.get(k)) for k in ("o", "h", "l", "c")
    )
    span = max(high - low, 1e-12)
    body = abs(close - op)
    upper_wick = max(0.0, high - max(op, close))
    lower_wick = max(0.0, min(op, close) - low)
    close_pos = (close - low) / span
    atr = _atr(rows[-30:])
    if atr <= 0:
        return None

    buy_reasons: list[str] = []
    sell_reasons: list[str] = []

    # Recent local sweep/reclaim: 12-candle window rather than a broad 20-candle range.
    prior_high_12 = max(_num(row.get("h")) for row in sweep_history)
    prior_low_12 = min(_num(row.get("l")) for row in sweep_history)
    if low < prior_low_12 and close > prior_low_12 and lower_wick / span >= 0.18 and close_pos >= 0.58:
        buy_reasons.append("LOCAL LOW SWEEP + RECLAIM")
    if high > prior_high_12 and close < prior_high_12 and upper_wick / span >= 0.18 and close_pos <= 0.42:
        sell_reasons.append("LOCAL HIGH SWEEP + REJECTION")

    # Micro break of the last five completed candles, requiring a directional close.
    micro_high = max(_num(row.get("h")) for row in short_history)
    micro_low = min(_num(row.get("l")) for row in short_history)
    if close > micro_high and close > op and body >= atr * 0.12 and close_pos >= 0.58:
        buy_reasons.append("1M MICRO HIGH BREAK")
    if close < micro_low and close < op and body >= atr * 0.12 and close_pos <= 0.42:
        sell_reasons.append("1M MICRO LOW BREAK")

    # A doji / small-body pause followed by a decisive break.
    previous_span = max(prev_high - prev_low, 1e-12)
    previous_body_pct = abs(prev_close - prev_open) / previous_span
    if previous_body_pct <= 0.30 and close > prev_high and close > op and body >= atr * 0.12:
        buy_reasons.append("DOJI / PAUSE HIGH BREAK")
    if previous_body_pct <= 0.30 and close < prev_low and close < op and body >= atr * 0.12:
        sell_reasons.append("DOJI / PAUSE LOW BREAK")

    # EMA pullback reclaim catches continuation moves before a wide-range breakout.
    closes = [_num(row.get("c")) for row in rows[-40:]]
    prev_closes = closes[:-1]
    ema9_now = _ema(closes[-20:], 9)
    ema21_now = _ema(closes[-30:], 21)
    ema9_prev = _ema(prev_closes[-20:], 9) if prev_closes else ema9_now
    if ema9_now > ema21_now and prev_close <= ema9_prev and close > ema9_now and close > op and body >= atr * 0.10:
        buy_reasons.append("EMA PULLBACK RECLAIM")
    if ema9_now < ema21_now and prev_close >= ema9_prev and close < ema9_now and close < op and body >= atr * 0.10:
        sell_reasons.append("EMA PULLBACK RECLAIM")

    # Engulfing still counts, with a moderate impulse threshold for 1m.
    bullish_engulf = (
        prev_close < prev_open and close > op
        and op <= prev_close and close >= prev_open
        and body >= atr * 0.20 and close_pos >= 0.60
    )
    bearish_engulf = (
        prev_close > prev_open and close < op
        and op >= prev_close and close <= prev_open
        and body >= atr * 0.20 and close_pos <= 0.40
    )
    if bullish_engulf:
        buy_reasons.append("BULLISH ENGULFING")
    if bearish_engulf:
        sell_reasons.append("BEARISH ENGULFING")

    if buy_reasons and not sell_reasons:
        return {"side": "BUY", "reason": " + ".join(dict.fromkeys(buy_reasons)), "atr": atr}
    if sell_reasons and not buy_reasons:
        return {"side": "SELL", "reason": " + ".join(dict.fromkeys(sell_reasons)), "atr": atr}
    return None


def _move_potential_pips(rows: list[dict[str, Any]], pip_size: float) -> float:
    """Simple volatility/range-based opportunity filter, not a guaranteed forecast."""
    if len(rows) < 15 or pip_size <= 0:
        return 0.0
    atr_pips = _atr(rows[-30:]) / pip_size
    window = rows[-30:]
    range_pips = (
        max(_num(row.get("h")) for row in window)
        - min(_num(row.get("l")) for row in window)
    ) / pip_size
    return max(atr_pips * 6.0, range_pips * 0.55)


class ASTOneMinuteSignalEngine:
    """Single live signal per market; evaluates new closed 1m candles and refreshes quotes at 10 Hz."""

    def __init__(self) -> None:
        self._memory: dict[str, dict[str, Any]] = {
            key: {"last_bar": None, "active": None, "last_note": "Waiting for a completed 1m setup"}
            for key in ("BTC", "GOLD")
        }

    def evaluate(
        self,
        asset: str,
        candles_by_tf: dict[str, Any],
        live_price: float | None,
        as_of_ms: int | None = None,
    ) -> dict[str, Any]:
        key = str(asset).upper()
        if key not in self._memory:
            raise ValueError(f"Unsupported AST asset: {asset}")
        now = int(as_of_ms or time.time() * 1000)
        pip_size = PIP_SIZE[key]
        candles = {
            tf: _closed(candles_by_tf.get(tf, []), tf, now)
            for tf in TF_MS
        }
        one = candles["1m"]
        price = _num(live_price, _num(one[-1].get("c")) if one else 0.0)
        memory = self._memory[key]

        if len(one) < 60:
            return self._waiting_result(
                key, price, pip_size, "WARMING UP · Need 60 completed 1m candles",
                candles, now,
            )

        latest = one[-1]
        latest_time = int(_num(latest.get("t")))
        local_trend = _trend(one[-240:])
        historical_trend = _trend(one[-1200:])

        frame_trends: dict[str, dict[str, Any]] = {}
        buy_weight = 0.0
        sell_weight = 0.0
        ready_frames = 0
        directional_frames = 0
        for tf, weight in TREND_WEIGHTS.items():
            trend = _trend(candles[tf][-240:])
            frame_trends[tf] = trend
            if not trend["ready"]:
                continue
            ready_frames += 1
            if trend["bias"] == "BULLISH":
                buy_weight += weight
                directional_frames += 1
            elif trend["bias"] == "BEARISH":
                sell_weight += weight
                directional_frames += 1

        directional_weight = buy_weight + sell_weight
        if directional_weight > 0:
            alignment = max(buy_weight, sell_weight) / directional_weight * 100.0
        else:
            alignment = 0.0

        if buy_weight > sell_weight and (
            directional_frames >= MIN_READY_DIRECTIONAL_FRAMES and alignment >= MIN_ALIGNMENT
            or directional_frames == 1 and frame_trends.get("5m", {}).get("bias") == "BULLISH"
        ):
            present_bias = "BUY"
        elif sell_weight > buy_weight and (
            directional_frames >= MIN_READY_DIRECTIONAL_FRAMES and alignment >= MIN_ALIGNMENT
            or directional_frames == 1 and frame_trends.get("5m", {}).get("bias") == "BEARISH"
        ):
            present_bias = "SELL"
        elif local_trend["bias"] in ("BULLISH", "BEARISH") and local_trend["strength"] >= 5.0:
            present_bias = "BUY" if local_trend["bias"] == "BULLISH" else "SELL"
        else:
            present_bias = "NEUTRAL"

        atr = _atr(one[-30:])
        atr_pips = atr / pip_size if pip_size > 0 else 0.0
        move_potential_pips = _move_potential_pips(one, pip_size)
        projection_bias = (
            "UP" if present_bias == "BUY"
            else "DOWN" if present_bias == "SELL"
            else "RANGE / WAIT"
        )
        projection_price = price if price > 0 else _num(latest.get("c"))
        projection_width = atr * 6.0
        projected_low = max(0.0, projection_price - projection_width)
        projected_high = projection_price + projection_width

        # Manage live stop/target touches between 1m candle closes.
        active = memory.get("active")
        if active and price > 0:
            side = active["side"]
            sl = float(active["stop_loss"])
            tp = float(active["take_profit"])
            exit_reason = None
            if side == "BUY":
                if price <= sl:
                    exit_reason = "STOP LOSS HIT"
                elif price >= tp:
                    exit_reason = "TARGET HIT"
            else:
                if price >= sl:
                    exit_reason = "STOP LOSS HIT"
                elif price <= tp:
                    exit_reason = "TARGET HIT"
            if exit_reason:
                memory["active"] = None
                memory["last_note"] = exit_reason
                active = None

        if latest_time > int(memory.get("last_bar") or 0):
            memory["last_bar"] = latest_time
            candidate = _setup(one[-120:])
            if candidate is None:
                if memory.get("active") is None:
                    memory["last_note"] = "Waiting · no closed 1m trigger candle yet"
            elif candidate["side"] != present_bias:
                if memory.get("active") is None:
                    memory["last_note"] = "1m setup found · waiting for trend agreement"
            elif move_potential_pips < MIN_MOVE_POTENTIAL_PIPS:
                if memory.get("active") is None:
                    memory["last_note"] = (
                        f"WAIT · estimated move-space {move_potential_pips:.0f} pips; "
                        f"minimum is {MIN_MOVE_POTENTIAL_PIPS:.0f}"
                    )
            elif atr_pips > 80.0:
                if memory.get("active") is None:
                    memory["last_note"] = (
                        f"WAIT · 1m ATR is {atr_pips:.0f} pips; fixed 30/50-pip SL is too tight"
                    )
            else:
                side = candidate["side"]
                stop_pips = 50 if atr_pips >= 25.0 else 30
                target_pips = max(MIN_TARGET_PIPS, stop_pips * 1.8)
                distance = stop_pips * pip_size
                target_distance = target_pips * pip_size
                entry = _num(latest.get("c"))
                stop = entry - distance if side == "BUY" else entry + distance
                target = entry + target_distance if side == "BUY" else entry - target_distance
                reason = (
                    f"{candidate['reason']} · trend {alignment:.0f}% · "
                    f"move-space {move_potential_pips:.0f} pips"
                )
                memory["active"] = {
                    "side": side, "entry": round(entry, 8),
                    "stop_loss": round(stop, 8), "take_profit": round(target, 8),
                    "sl_pips": stop_pips, "target_pips": round(target_pips, 1),
                    "sl_price_distance": round(distance, 8),
                    "target_price_distance": round(target_distance, 8),
                    "pip_size": pip_size, "risk_reward": round(target_pips / stop_pips, 2),
                    "move_potential_pips": round(move_potential_pips, 1),
                    "signal_time": latest_time, "reason": reason,
                }
                memory["last_note"] = reason
                active = memory["active"]

        active = memory.get("active")
        return {
            "asset": key,
            "action": active["side"] if active else "WAITING",
            "status": "ACTIVE" if active else "WAITING",
            "price": round(price, 8) if price > 0 else None,
            "entry": active.get("entry") if active else None,
            "stop_loss": active.get("stop_loss") if active else None,
            "take_profit": active.get("take_profit") if active else None,
            "sl_pips": active.get("sl_pips") if active else None,
            "target_pips": active.get("target_pips") if active else None,
            "pip_size": pip_size,
            "sl_price_distance": active.get("sl_price_distance") if active else None,
            "target_price_distance": active.get("target_price_distance") if active else None,
            "risk_reward": active.get("risk_reward") if active else None,
            "move_potential_pips": active.get("move_potential_pips") if active else round(move_potential_pips, 1),
            "minimum_move_pips": MIN_MOVE_POTENTIAL_PIPS,
            "signal_time": active.get("signal_time") if active else None,
            "reason": active.get("reason") if active else memory.get("last_note"),
            "historical_bias": historical_trend["bias"],
            "historical_bars": historical_trend["bars"],
            "present_bias": present_bias,
            "projection_bias": projection_bias,
            "projected_low": round(projected_low, 8) if projection_price > 0 else None,
            "projected_high": round(projected_high, 8) if projection_price > 0 else None,
            "atr_1m": round(atr, 8),
            "atr_pips": round(atr_pips, 2),
            "trend_alignment_pct": round(alignment, 1),
            "ready_timeframes": ready_frames,
            "directional_timeframes": directional_frames,
            "timeframe_trends": {tf: trend["bias"] for tf, trend in frame_trends.items()},
            "last_closed_1m": latest_time,
            "scan_interval_ms": 100,
            "forecast_note": "Six-minute ATR/range scenario only; it is not a guaranteed future price.",
        }

    @staticmethod
    def _waiting_result(
        asset: str, price: float, pip_size: float, reason: str,
        candles: dict[str, list[dict[str, Any]]], now: int,
    ) -> dict[str, Any]:
        one = candles.get("1m", [])
        potential = _move_potential_pips(one, pip_size)
        return {
            "asset": asset, "action": "WAITING", "status": "WARMING UP",
            "price": round(price, 8) if price > 0 else None,
            "entry": None, "stop_loss": None, "take_profit": None,
            "sl_pips": None, "target_pips": None, "pip_size": pip_size,
            "sl_price_distance": None, "target_price_distance": None,
            "risk_reward": None, "move_potential_pips": round(potential, 1),
            "minimum_move_pips": MIN_MOVE_POTENTIAL_PIPS,
            "signal_time": None, "reason": reason,
            "historical_bias": "NEUTRAL", "historical_bars": len(one),
            "present_bias": "NEUTRAL", "projection_bias": "RANGE / WAIT",
            "projected_low": None, "projected_high": None,
            "atr_1m": None, "atr_pips": None, "trend_alignment_pct": 0.0,
            "ready_timeframes": 0, "directional_timeframes": 0,
            "timeframe_trends": {},
            "last_closed_1m": int(_num(one[-1].get("t"))) if one else None,
            "scan_interval_ms": 100,
            "forecast_note": "Projection starts when enough completed candle history is available.",
        }
