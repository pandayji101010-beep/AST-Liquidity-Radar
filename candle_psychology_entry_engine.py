from __future__ import annotations

import math
from typing import Any

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
MIN_RR = 2.0
SIGNAL_DISPLAY_MS = 5 * 60_000


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _rows(data: dict[str, Any], timeframe: str) -> list[dict[str, Any]]:
    value = data.get(timeframe) or []
    return value if isinstance(value, list) else list(value)


def _closed_rows(data: dict[str, Any], timeframe: str, as_of_ms: int) -> list[dict[str, Any]]:
    """Exclude every candle whose scheduled close is later than the decision time."""
    interval = TIMEFRAME_MS[timeframe]
    return [
        row for row in _rows(data, timeframe)
        if int(_num(row.get("t"))) > 0
        and int(_num(row.get("t"))) + interval <= int(as_of_ms)
    ]


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    start = max(1, len(candles) - period)
    ranges = []
    for index in range(start, len(candles)):
        row = candles[index]
        previous_close = _num(candles[index - 1].get("c"))
        ranges.append(max(
            _num(row.get("h")) - _num(row.get("l")),
            abs(_num(row.get("h")) - previous_close),
            abs(_num(row.get("l")) - previous_close),
        ))
    return sum(ranges) / len(ranges) if ranges else 0.0


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _market_bias(candles: list[dict[str, Any]]) -> str:
    """Closed-candle structure with an EMA fallback when pivots are sparse."""
    if len(candles) < 30:
        return "NEUTRAL"
    highs = [_num(row.get("h")) for row in candles[-120:]]
    lows = [_num(row.get("l")) for row in candles[-120:]]
    pivot_highs: list[float] = []
    pivot_lows: list[float] = []
    for index in range(2, len(highs) - 2):
        high_window = highs[index - 2:index + 3]
        low_window = lows[index - 2:index + 3]
        if highs[index] == max(high_window) and highs[index] > highs[index - 1]:
            pivot_highs.append(highs[index])
        if lows[index] == min(low_window) and lows[index] < lows[index - 1]:
            pivot_lows.append(lows[index])
    if len(pivot_highs) >= 2 and len(pivot_lows) >= 2:
        if pivot_highs[-1] > pivot_highs[-2] and pivot_lows[-1] > pivot_lows[-2]:
            return "BULLISH"
        if pivot_highs[-1] < pivot_highs[-2] and pivot_lows[-1] < pivot_lows[-2]:
            return "BEARISH"
    closes = [_num(row.get("c")) for row in candles[-120:]]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    prior_ema20 = _ema(closes[:-5], 20)
    if closes[-1] > ema20 > ema50 and ema20 > prior_ema20:
        return "BULLISH"
    if closes[-1] < ema20 < ema50 and ema20 < prior_ema20:
        return "BEARISH"
    return "NEUTRAL"


def candle_metrics(candle: dict[str, Any], atr: float = 0.0) -> dict[str, float]:
    op, high, low, close = (_num(candle.get(key)) for key in ("o", "h", "l", "c"))
    span = max(high - low, 1e-12)
    body = abs(close - op)
    upper_wick = max(0.0, high - max(op, close))
    lower_wick = max(0.0, min(op, close) - low)
    return {
        "body_pct": body / span * 100.0,
        "upper_wick_pct": upper_wick / span * 100.0,
        "lower_wick_pct": lower_wick / span * 100.0,
        "close_position": (close - low) / span,
        "relative_strength": body / atr if atr > 0 else 0.0,
        "range": span,
        "body": body,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
    }


def candle_patterns(
    candles: list[dict[str, Any]],
    index: int,
    level: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe candle psychology using only this candle and already-closed predecessors."""
    if index < 0 or index >= len(candles):
        return {"patterns": [], "metrics": {}, "volume_reliable": False}
    candle = candles[index]
    op, high, low, close = (_num(candle.get(key)) for key in ("o", "h", "l", "c"))
    atr = _atr(candles[max(0, index - 14):index + 1])
    metrics = candle_metrics(candle, atr)
    body = metrics["body"]
    upper = metrics["upper_wick"]
    lower = metrics["lower_wick"]
    span = metrics["range"]
    close_pos = metrics["close_position"]
    bullish = close > op
    bearish = close < op
    patterns: list[str] = []

    bull_rejection = bullish and lower >= max(body * 1.0, span * 0.32) and close_pos >= 0.62
    bear_rejection = bearish and upper >= max(body * 1.0, span * 0.32) and close_pos <= 0.38
    if bull_rejection:
        patterns.extend(["BULLISH_REJECTION", "LONG_LOWER_WICK_REJECTION"])
    if bear_rejection:
        patterns.extend(["BEARISH_REJECTION", "LONG_UPPER_WICK_REJECTION"])
    if lower >= max(body * 2.0, span * 0.50) and upper <= span * 0.25 and close_pos >= 0.62:
        patterns.append("HAMMER")
        if close_pos >= 0.72:
            patterns.append("PIN_BAR_BULLISH")
    if upper >= max(body * 2.0, span * 0.50) and lower <= span * 0.25 and close_pos <= 0.38:
        patterns.append("SHOOTING_STAR")
        if close_pos <= 0.28:
            patterns.append("PIN_BAR_BEARISH")

    if index > 0:
        previous = candles[index - 1]
        prev_open, prev_close = _num(previous.get("o")), _num(previous.get("c"))
        prev_high, prev_low = _num(previous.get("h")), _num(previous.get("l"))
        if prev_close < prev_open and bullish and op <= prev_close and close >= prev_open:
            patterns.append("BULLISH_ENGULFING")
        if prev_close > prev_open and bearish and op >= prev_close and close <= prev_open:
            patterns.append("BEARISH_ENGULFING")
        if high <= prev_high and low >= prev_low:
            patterns.append("INSIDE_BAR")
        if high >= prev_high and low <= prev_low and (high > prev_high or low < prev_low):
            patterns.append("OUTSIDE_BAR")

    if metrics["body_pct"] >= 65.0 and metrics["relative_strength"] >= 0.70:
        if bullish and close_pos >= 0.75:
            patterns.append("STRONG_BODY_BULLISH")
        elif bearish and close_pos <= 0.25:
            patterns.append("STRONG_BODY_BEARISH")

    if level is not None:
        level_price = _num(level.get("price"))
        zone_low = _num(level.get("zone_low"), level_price)
        zone_high = _num(level.get("zone_high"), level_price)
        touched = low <= zone_high and high >= zone_low
        if index > 0:
            previous_close = _num(candles[index - 1].get("c"))
            if level.get("type") == "SUPPORT" and previous_close < zone_low and close > level_price:
                patterns.append("FAILED_BREAKOUT_BULLISH")
            elif level.get("type") == "RESISTANCE" and previous_close > zone_high and close < level_price:
                patterns.append("FAILED_BREAKOUT_BEARISH")
        if level.get("type") == "SUPPORT" and low < zone_low and close > level_price:
            patterns.append("LIQUIDITY_SWEEP_RECLAIM_BULLISH")
        elif level.get("type") == "RESISTANCE" and high > zone_high and close < level_price:
            patterns.append("LIQUIDITY_SWEEP_RECLAIM_BEARISH")

        recent = candles[max(0, index - 4):index + 1]
        rejection_count = 0
        for row in recent:
            row_low, row_high = _num(row.get("l")), _num(row.get("h"))
            if row_low > zone_high or row_high < zone_low:
                continue
            row_metrics = candle_metrics(row, atr)
            if level.get("type") == "SUPPORT":
                rejected = row_metrics["lower_wick_pct"] >= 32 and row_metrics["close_position"] >= 0.60
            else:
                rejected = row_metrics["upper_wick_pct"] >= 32 and row_metrics["close_position"] <= 0.40
            rejection_count += int(rejected)
        if rejection_count >= 2:
            patterns.append(
                "REPEATED_BULLISH_REJECTION" if level.get("type") == "SUPPORT"
                else "REPEATED_BEARISH_REJECTION"
            )

        prior_volumes = [_num(row.get("v")) for row in candles[max(0, index - 20):index]]
        prior_volumes = [value for value in prior_volumes if value > 0]
        current_volume = _num(candle.get("v"))
        volume_reliable = current_volume > 0 and len(prior_volumes) >= 10
        volume_ratio = current_volume / (sum(prior_volumes) / len(prior_volumes)) if volume_reliable else 0.0
        if touched and volume_reliable and volume_ratio >= 1.4 and rejection_count >= 2:
            patterns.append(
                "VOLUME_ABSORPTION_BULLISH_PROXY" if level.get("type") == "SUPPORT"
                else "VOLUME_ABSORPTION_BEARISH_PROXY"
            )
        metrics["volume_ratio"] = volume_ratio
    else:
        volume_reliable = False
    return {
        "patterns": patterns,
        "metrics": metrics,
        "volume_reliable": volume_reliable,
    }


_BULLISH_REACTIONS = {
    "BULLISH_REJECTION", "LONG_LOWER_WICK_REJECTION", "HAMMER", "PIN_BAR_BULLISH",
    "BULLISH_ENGULFING", "STRONG_BODY_BULLISH", "FAILED_BREAKOUT_BULLISH",
    "LIQUIDITY_SWEEP_RECLAIM_BULLISH", "REPEATED_BULLISH_REJECTION",
    "VOLUME_ABSORPTION_BULLISH_PROXY",
}
_BEARISH_REACTIONS = {
    "BEARISH_REJECTION", "LONG_UPPER_WICK_REJECTION", "SHOOTING_STAR", "PIN_BAR_BEARISH",
    "BEARISH_ENGULFING", "STRONG_BODY_BEARISH", "FAILED_BREAKOUT_BEARISH",
    "LIQUIDITY_SWEEP_RECLAIM_BEARISH", "REPEATED_BEARISH_REJECTION",
    "VOLUME_ABSORPTION_BEARISH_PROXY",
}


def level_key(level: dict[str, Any]) -> str:
    return f"{str(level.get('type', '')).upper()}:{_num(level.get('price')):.8f}"


def _broken_before_entry(
    side: str,
    level: dict[str, Any],
    five_minute: list[dict[str, Any]],
    one_minute: list[dict[str, Any]],
    atr1: float,
) -> bool:
    lower = _num(level.get("zone_low"), _num(level.get("price")))
    upper = _num(level.get("zone_high"), _num(level.get("price")))
    if len(five_minute) >= 2:
        last_two = five_minute[-2:]
        if side == "BUY" and all(_num(row.get("c")) < lower for row in last_two):
            return True
        if side == "SELL" and all(_num(row.get("c")) > upper for row in last_two):
            return True
    if one_minute:
        close = _num(one_minute[-1].get("c"))
        buffer = max(atr1 * 0.25, _num(level.get("half_width")) * 0.5)
        if side == "BUY" and close < lower - buffer:
            return True
        if side == "SELL" and close > upper + buffer:
            return True
    return False


def _has_15m_level_reaction(
    candles: list[dict[str, Any]],
    level: dict[str, Any],
    side: str,
    lookback: int = 12,
) -> tuple[bool, list[str]]:
    """Require a recent directional 15m close after that timeframe tests this level."""
    if len(candles) < 3:
        return False, []
    level_price = _num(level.get("price"))
    lower = _num(level.get("zone_low"), level_price)
    upper = _num(level.get("zone_high"), level_price)
    accepted = _BULLISH_REACTIONS if side == "BUY" else _BEARISH_REACTIONS
    start = max(0, len(candles) - lookback)
    for index in range(start, len(candles)):
        candle = candles[index]
        if _num(candle.get("l")) > upper or _num(candle.get("h")) < lower:
            continue
        analysis = candle_patterns(candles, index, level)
        matched = [tag for tag in analysis["patterns"] if tag in accepted]
        op, high, low, close = (
            _num(candle.get(key)) for key in ("o", "h", "l", "c")
        )
        close_position = (close - low) / max(high - low, 1e-12)
        directional_close = (
            close > op and close >= level_price and close_position >= 0.55
            if side == "BUY"
            else close < op and close <= level_price and close_position <= 0.45
        )
        if matched or directional_close:
            return True, matched or (["15M_BULLISH_LEVEL_CONTEXT"] if side == "BUY"
                                     else ["15M_BEARISH_LEVEL_CONTEXT"])
    return False, []


def evaluate_level(
    level: dict[str, Any],
    active_levels: list[dict[str, Any]],
    candles_by_tf: dict[str, Any],
    live_price: float | None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    """One deterministic confirmation path shared by live evaluation and historical replay."""
    level_type = str(level.get("type", "")).upper()
    side = "BUY" if level_type == "SUPPORT" else "SELL" if level_type == "RESISTANCE" else None
    waiting = {"action": "WAITING", "reason": "NO_CANDLE_CONFIRMATION"}
    if side is None:
        return waiting
    if as_of_ms is None:
        import time
        as_of_ms = int(time.time() * 1000)

    # Live and replay both make decisions from candles that have fully closed
    # by the same explicit decision timestamp.
    one_minute = _closed_rows(candles_by_tf, "1m", as_of_ms)
    five_minute = _closed_rows(candles_by_tf, "5m", as_of_ms)
    fifteen_minute = _closed_rows(candles_by_tf, "15m", as_of_ms)
    one_hour = _closed_rows(candles_by_tf, "1h", as_of_ms)
    price = _num(live_price, _num(one_minute[-1].get("c")) if one_minute else 0.0)
    if price <= 0:
        return {**waiting, "reason": "NO_LIVE_PRICE"}
    if min(len(one_minute), len(five_minute), len(fifteen_minute), len(one_hour)) < 30:
        return {**waiting, "reason": "TIMEFRAME_WARMING_UP"}

    level_price = _num(level.get("price"))
    lower = _num(level.get("zone_low"), level_price)
    upper = _num(level.get("zone_high"), level_price)
    half_width = max(_num(level.get("half_width")), (upper - lower) / 2.0, level_price * 0.00001)
    atr5, atr1 = _atr(five_minute), _atr(one_minute)
    if atr5 <= 0 or atr1 <= 0:
        return {**waiting, "reason": "VOLATILITY_NOT_READY"}

    if price > upper:
        approach = "FROM_ABOVE"
        distance_to_zone = price - upper
    elif price < lower:
        approach = "FROM_BELOW"
        distance_to_zone = lower - price
    else:
        approach = "INSIDE_ZONE"
        distance_to_zone = 0.0
    approach_limit = max(half_width * 4.0, atr5 * 1.5, price * 0.00015)
    if distance_to_zone > approach_limit:
        return {**waiting, "reason": "PRICE_NOT_NEAR_LEVEL", "approach": approach}
    expected_approach = "FROM_ABOVE" if side == "BUY" else "FROM_BELOW"
    if approach not in (expected_approach, "INSIDE_ZONE"):
        return {**waiting, "reason": "APPROACH_SIDE_MISMATCH", "approach": approach}
    if _broken_before_entry(side, level, five_minute, one_minute, atr1):
        return {**waiting, "reason": "LEVEL_BROKEN", "approach": approach}

    required_bias = "BULLISH" if side == "BUY" else "BEARISH"
    bias_1h = _market_bias(one_hour)
    bias_15m = _market_bias(fifteen_minute)
    if bias_1h != bias_15m or bias_1h != required_bias:
        return {
            **waiting,
            "reason": "TIMEFRAME_CONFLICT_OR_NOT_ALIGNED",
            "approach": approach,
            "bias_1h": bias_1h,
            "bias_15m": bias_15m,
        }

    m15_reacted, m15_tags = _has_15m_level_reaction(fifteen_minute, level, side)
    if not m15_reacted:
        return {
            **waiting,
            "reason": "15M_LEVEL_REACTION_MISSING",
            "approach": approach,
            "bias_1h": bias_1h,
            "bias_15m": bias_15m,
        }

    reaction_tags: list[str] = []
    reaction_candles: list[dict[str, Any]] = []
    reaction_start = max(0, len(five_minute) - 5)
    accepted_patterns = _BULLISH_REACTIONS if side == "BUY" else _BEARISH_REACTIONS
    for index in range(max(1, reaction_start), len(five_minute)):
        candle = five_minute[index]
        touched = _num(candle.get("l")) <= upper and _num(candle.get("h")) >= lower
        if not touched:
            continue
        analysis = candle_patterns(five_minute, index, level)
        matched = [tag for tag in analysis["patterns"] if tag in accepted_patterns]
        if matched:
            reaction_tags.extend(matched)
            reaction_candles.append(candle)

    last_five_close = _num(five_minute[-1].get("c"))
    reclaim = last_five_close > level_price if side == "BUY" else last_five_close < level_price
    if not reaction_candles or not reclaim:
        return {
            **waiting,
            "reason": "5M_REACTION_OR_RECLAIM_MISSING",
            "approach": approach,
            "bias_1h": bias_1h,
            "bias_15m": bias_15m,
        }

    five_close_time = int(five_minute[-1].get("t", 0)) + TIMEFRAME_MS["5m"]
    one_close_time = int(one_minute[-1].get("t", 0)) + TIMEFRAME_MS["1m"]
    if one_close_time < five_close_time or len(one_minute) < 6:
        return {**waiting, "reason": "WAITING_FOR_1M_CLOSE", "approach": approach}

    current = one_minute[-1]
    prior = one_minute[-6:-1]
    body_atr = candle_metrics(current, atr1)
    close_pos = body_atr["close_position"]
    body = body_atr["body"]
    if side == "BUY":
        micro_bos = _num(current.get("c")) > max(_num(row.get("h")) for row in prior)
        displacement = _num(current.get("c")) > _num(current.get("o")) and body >= atr1 * 0.15 and close_pos >= 0.65
    else:
        micro_bos = _num(current.get("c")) < min(_num(row.get("l")) for row in prior)
        displacement = _num(current.get("c")) < _num(current.get("o")) and body >= atr1 * 0.15 and close_pos <= 0.35
    if not micro_bos or not displacement:
        return {**waiting, "reason": "1M_STRUCTURE_SHIFT_MISSING", "approach": approach}

    entry = _num(current.get("c"))
    rejection_swing = (
        min(_num(row.get("l")) for row in reaction_candles + five_minute[-2:] + one_minute[-6:])
        if side == "BUY"
        else max(_num(row.get("h")) for row in reaction_candles + five_minute[-2:] + one_minute[-6:])
    )
    buffer = max(atr1 * 0.15, half_width * 0.10, entry * 0.00002)
    stop = rejection_swing - buffer if side == "BUY" else rejection_swing + buffer
    risk = entry - stop if side == "BUY" else stop - entry
    if risk <= 0:
        return {**waiting, "reason": "INVALID_STOP", "approach": approach}

    opposing = []
    for other in active_levels:
        if str(other.get("type", "")).upper() == ("RESISTANCE" if side == "BUY" else "SUPPORT"):
            other_price = _num(other.get("price"))
            if (side == "BUY" and other_price > entry) or (side == "SELL" and other_price < entry):
                opposing.append(other)
    if opposing:
        nearest = min(opposing, key=lambda item: _num(item.get("price"))) if side == "BUY" else max(opposing, key=lambda item: _num(item.get("price")))
        target = _num(nearest.get("zone_low"), _num(nearest.get("price"))) if side == "BUY" else _num(nearest.get("zone_high"), _num(nearest.get("price")))
    else:
        target = entry + MIN_RR * risk if side == "BUY" else entry - MIN_RR * risk
    reward = target - entry if side == "BUY" else entry - target
    rr = reward / risk if risk > 0 else 0.0
    if reward <= 0 or rr < MIN_RR:
        return {**waiting, "reason": "MINIMUM_1_TO_2_RR_NOT_AVAILABLE", "approach": approach}

    last_pattern = candle_patterns(five_minute, len(five_minute) - 1, level)
    return {
        "action": side,
        "reason": "CONFIRMED",
        "approach": approach,
        "entry": round(entry, 8),
        "stop_loss": round(stop, 8),
        "take_profit": round(target, 8),
        "risk_reward": round(rr, 3),
        "signal_time": int(current.get("t", 0)),
        "patterns": sorted(set(m15_tags + reaction_tags + last_pattern["patterns"])),
        "bias_1h": bias_1h,
        "bias_15m": bias_15m,
    }


class CandlePsychologyEntryEngine:
    """Independent level-reaction entry state; never changes source level data."""

    def __init__(self, asset: str = "MARKET") -> None:
        self.asset = asset.upper()
        self._triggered: dict[str, dict[str, Any]] = {}

    def evaluate(
        self,
        active_levels: list[dict[str, Any]],
        candles_by_tf: dict[str, Any],
        live_price: float | None,
        as_of_ms: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        if as_of_ms is None:
            import time
            as_of_ms = int(time.time() * 1000)
        active_keys = {level_key(level) for level in active_levels}
        for stale_key in list(self._triggered):
            if stale_key not in active_keys:
                self._triggered.pop(stale_key, None)

        # Materialize the shared candle deques once per refresh, not once per level.
        candle_data = {
            timeframe: _closed_rows(candles_by_tf, timeframe, as_of_ms)
            for timeframe in ("1m", "5m", "15m", "1h")
        }
        one_minute = candle_data["1m"]
        current_time = int(one_minute[-1].get("t", 0)) if one_minute else 0
        results: dict[str, dict[str, Any]] = {}
        for level in active_levels:
            key = level_key(level)
            previous_signal = self._triggered.get(key)
            if previous_signal is not None:
                signal_age = current_time - int(previous_signal.get("signal_time", current_time))
                if 0 <= signal_age <= SIGNAL_DISPLAY_MS:
                    results[key] = dict(previous_signal)
                else:
                    results[key] = {
                        "action": "WAITING",
                        "reason": "LEVEL_ALREADY_TRIGGERED",
                    }
                continue

            result = evaluate_level(level, active_levels, candle_data, live_price, as_of_ms)
            if result.get("action") in ("BUY", "SELL"):
                self._triggered[key] = dict(result)
            results[key] = result
        return results


def backtest_trades(
    levels_by_step: list[list[dict[str, Any]]],
    candles_by_step: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate pre-built point-in-time replay steps with the exact live evaluator."""
    engine = CandlePsychologyEntryEngine()
    trades: list[dict[str, Any]] = []
    for index, step in enumerate(candles_by_step):
        timestamp = int(step.get("time", 0))
        levels = levels_by_step[index] if index < len(levels_by_step) else []
        decision_time = timestamp + TIMEFRAME_MS["1m"]
        signals = engine.evaluate(
            levels, step.get("candles", {}), step.get("price"), as_of_ms=decision_time
        )
        for key, signal in signals.items():
            if signal.get("action") not in ("BUY", "SELL") or int(signal.get("signal_time", -1)) != timestamp:
                continue
            trades.append({"level_key": key, **signal})
    return trades
