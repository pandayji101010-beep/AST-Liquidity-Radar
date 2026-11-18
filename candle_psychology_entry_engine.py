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
    """Use only candles that have fully closed by the decision timestamp."""
    interval = TIMEFRAME_MS[timeframe]
    return [
        row for row in _rows(data, timeframe)
        if int(_num(row.get("t"))) > 0
        and int(_num(row.get("t"))) + interval <= int(as_of_ms)
    ]


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    ranges = []
    for index in range(max(1, len(candles) - period), len(candles)):
        row = candles[index]
        previous_close = _num(candles[index - 1].get("c"))
        ranges.append(max(
            _num(row.get("h")) - _num(row.get("l")),
            abs(_num(row.get("h")) - previous_close),
            abs(_num(row.get("l")) - previous_close),
        ))
    return sum(ranges) / len(ranges) if ranges else 0.0


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
    """Read local candle psychology and level-specific rejection from closed candles."""
    if index < 0 or index >= len(candles):
        return {"patterns": [], "metrics": {}, "volume_reliable": False}

    row = candles[index]
    op, high, low, close = (_num(row.get(key)) for key in ("o", "h", "l", "c"))
    atr = _atr(candles[max(0, index - 14):index + 1])
    metrics = candle_metrics(row, atr)
    body, upper, lower, span = (
        metrics["body"], metrics["upper_wick"], metrics["lower_wick"], metrics["range"]
    )
    close_pos = metrics["close_position"]
    bullish, bearish = close > op, close < op
    patterns: list[str] = []

    bull_rejection = bullish and lower >= max(body * 1.0, span * 0.32) and close_pos >= 0.58
    bear_rejection = bearish and upper >= max(body * 1.0, span * 0.32) and close_pos <= 0.42
    if bull_rejection:
        patterns.extend(["BULLISH_REJECTION", "LONG_LOWER_WICK_REJECTION"])
    if bear_rejection:
        patterns.extend(["BEARISH_REJECTION", "LONG_UPPER_WICK_REJECTION"])
    if lower >= max(body * 2.0, span * 0.50) and upper <= span * 0.25 and close_pos >= 0.58:
        patterns.append("HAMMER")
        if close_pos >= 0.70:
            patterns.append("PIN_BAR_BULLISH")
    if upper >= max(body * 2.0, span * 0.50) and lower <= span * 0.25 and close_pos <= 0.42:
        patterns.append("SHOOTING_STAR")
        if close_pos <= 0.30:
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

    if metrics["body_pct"] >= 60.0 and metrics["relative_strength"] >= 0.55:
        if bullish and close_pos >= 0.72:
            patterns.append("STRONG_BODY_BULLISH")
        elif bearish and close_pos <= 0.28:
            patterns.append("STRONG_BODY_BEARISH")

    volume_reliable = False
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
        for prior in recent:
            prior_low, prior_high = _num(prior.get("l")), _num(prior.get("h"))
            if prior_low > zone_high or prior_high < zone_low:
                continue
            prior_metrics = candle_metrics(prior, atr)
            if level.get("type") == "SUPPORT":
                rejected = prior_metrics["lower_wick_pct"] >= 30 and prior_metrics["close_position"] >= 0.58
            else:
                rejected = prior_metrics["upper_wick_pct"] >= 30 and prior_metrics["close_position"] <= 0.42
            rejection_count += int(rejected)
        if rejection_count >= 2:
            patterns.append(
                "REPEATED_BULLISH_REJECTION" if level.get("type") == "SUPPORT"
                else "REPEATED_BEARISH_REJECTION"
            )

        prior_volumes = [
            _num(prior.get("v"))
            for prior in candles[max(0, index - 20):index]
            if _num(prior.get("v")) > 0
        ]
        current_volume = _num(row.get("v"))
        volume_reliable = current_volume > 0 and len(prior_volumes) >= 10
        volume_ratio = (
            current_volume / (sum(prior_volumes) / len(prior_volumes))
            if volume_reliable else 0.0
        )
        if touched and volume_reliable and volume_ratio >= 1.5 and rejection_count >= 2:
            patterns.append(
                "VOLUME_ABSORPTION_BULLISH_PROXY" if level.get("type") == "SUPPORT"
                else "VOLUME_ABSORPTION_BEARISH_PROXY"
            )
        metrics["volume_ratio"] = volume_ratio

    return {"patterns": patterns, "metrics": metrics, "volume_reliable": volume_reliable}


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


def _level_zone(level: dict[str, Any]) -> tuple[float, float, float, float]:
    price = _num(level.get("price", level.get("level_price")))
    low = _num(level.get("zone_low"), price - _num(level.get("half_width")))
    high = _num(level.get("zone_high"), price + _num(level.get("half_width")))
    if low <= 0 or high < low:
        low = price - _num(level.get("half_width"))
        high = price + _num(level.get("half_width"))
    half_width = max((high - low) / 2.0, _num(level.get("half_width")), price * 0.00001)
    return price, low, high, half_width


def _approach(candles: list[dict[str, Any]], low: float, high: float) -> str:
    """Find the side the latest local test came from."""
    for index in range(len(candles) - 1, max(0, len(candles) - 6), -1):
        row = candles[index]
        touched = _num(row.get("l")) <= high and _num(row.get("h")) >= low
        if not touched:
            continue
        previous_close = _num(candles[index - 1].get("c")) if index else 0.0
        if previous_close > high:
            return "FROM_ABOVE"
        if previous_close < low:
            return "FROM_BELOW"
        close = _num(row.get("c"))
        return "INSIDE_ZONE" if low <= close <= high else ("FROM_ABOVE" if close > high else "FROM_BELOW")
    last_close = _num(candles[-1].get("c")) if candles else 0.0
    return "FROM_ABOVE" if last_close > high else "FROM_BELOW" if last_close < low else "INSIDE_ZONE"


def _waiting(reason: str, approach: str = "UNKNOWN") -> dict[str, Any]:
    return {"action": "WAITING", "reason": reason, "approach": approach}


def _panic_sweep_candidate(
    five_minute: list[dict[str, Any]],
    level: dict[str, Any],
    side: str,
) -> dict[str, Any] | None:
    """Return a recent closed 5m abnormal-urgency sweep/reclaim at this HTF level."""
    price, zone_low, zone_high, _half_width = _level_zone(level)
    for index in range(max(15, len(five_minute) - 8), len(five_minute)):
        row = five_minute[index]
        atr = _atr(five_minute[:index])
        if atr <= 0:
            continue
        metrics = candle_metrics(row, atr)
        range_atr = metrics["range"] / atr
        prior_volumes = [
            _num(item.get("v"))
            for item in five_minute[max(0, index - 20):index]
            if _num(item.get("v")) > 0
        ]
        current_volume = _num(row.get("v"))
        volume_reliable = current_volume > 0 and len(prior_volumes) >= 10
        volume_ratio = (
            current_volume / (sum(prior_volumes) / len(prior_volumes))
            if volume_reliable else 0.0
        )
        # Expansion is the primary panic evidence; volume is an optional, venue-specific boost.
        if range_atr < 1.35 and not (range_atr >= 1.0 and volume_reliable and volume_ratio >= 1.5):
            continue

        op, high, low, close = (_num(row.get(key)) for key in ("o", "h", "l", "c"))
        if side == "BUY":
            sweep_depth = zone_low - low
            min_sweep = max(atr * 0.03, price * 0.00001)
            reclaimed = (
                sweep_depth >= min_sweep
                and close > price
                and metrics["close_position"] >= 0.58
                and metrics["lower_wick_pct"] >= 18.0
            )
        else:
            sweep_depth = high - zone_high
            min_sweep = max(atr * 0.03, price * 0.00001)
            reclaimed = (
                sweep_depth >= min_sweep
                and close < price
                and metrics["close_position"] <= 0.42
                and metrics["upper_wick_pct"] >= 18.0
            )
        if not reclaimed:
            continue

        return {
            "index": index,
            "time": int(_num(row.get("t"))),
            "close_time": int(_num(row.get("t"))) + TIMEFRAME_MS["5m"],
            "panic_extreme": low if side == "BUY" else high,
            "range_atr": range_atr,
            "volume_ratio": volume_ratio,
            "volume_reliable": volume_reliable,
        }
    return None


def _accepted_beyond(
    candles: list[dict[str, Any]],
    start_index: int,
    side: str,
    zone_low: float,
    zone_high: float,
) -> bool:
    """Two consecutive closed 5m closes beyond the zone invalidate the reclaim."""
    streak = 0
    for row in candles[max(0, start_index):]:
        close = _num(row.get("c"))
        beyond = close < zone_low if side == "BUY" else close > zone_high
        streak = streak + 1 if beyond else 0
        if streak >= 2:
            return True
    return False


def evaluate_level(
    level: dict[str, Any],
    active_levels: list[dict[str, Any]],
    candles_by_tf: dict[str, Any],
    live_price: float | None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    """
    PANIC Point entry decision: 5m urgency + sweep/reclaim, then a closed 1m
    opposite-side micro swing break. A level touch or rejection alone is not an entry.
    """
    if as_of_ms is None:
        import time
        as_of_ms = int(time.time() * 1000)

    level_type = str(level.get("type", "")).upper()
    side = "BUY" if level_type == "SUPPORT" else "SELL" if level_type == "RESISTANCE" else None
    if side is None:
        return _waiting("INVALID_LEVEL")
    if level.get("panic_confirmed") is False:
        return _waiting("LEVEL_NOT_PANIC_CONFIRMED")

    one = _closed_rows(candles_by_tf, "1m", as_of_ms)[-40:]
    five = _closed_rows(candles_by_tf, "5m", as_of_ms)[-40:]
    if len(one) < 8 or len(five) < 16:
        return _waiting("WAITING_FOR_1M_5M_CANDLES")

    level_price, zone_low, zone_high, half_width = _level_zone(level)
    current_price = _num(live_price, _num(one[-1].get("c")))
    if level_price <= 0 or current_price <= 0:
        return _waiting("WAITING_FOR_PRICE")

    atr1 = _atr(one)
    atr5 = _atr(five)
    if atr1 <= 0 or atr5 <= 0:
        return _waiting("WAITING_FOR_ATR")

    local_buffer = max(half_width * 3.0, atr5 * 2.5, atr1 * 5.0, level_price * 0.00008)
    distance = (
        0.0 if zone_low <= current_price <= zone_high
        else min(abs(current_price - zone_low), abs(current_price - zone_high))
    )
    if distance > local_buffer:
        return _waiting("PRICE_NOT_NEAR_LEVEL")

    approach = _approach(one, zone_low, zone_high)
    expected_approach = "FROM_ABOVE" if side == "BUY" else "FROM_BELOW"

    last_two = one[-2:]
    if side == "BUY" and all(_num(row.get("c")) < zone_low for row in last_two):
        return _waiting("SUPPORT_BROKEN", approach)
    if side == "SELL" and all(_num(row.get("c")) > zone_high for row in last_two):
        return _waiting("RESISTANCE_BROKEN", approach)
    if approach not in (expected_approach, "INSIDE_ZONE"):
        return _waiting("APPROACH_SIDE_MISMATCH", approach)

    panic = _panic_sweep_candidate(five, level, side)
    if panic is None:
        return _waiting("WAITING_FOR_PANIC_URGENCY_SWEEP_RECLAIM", approach)
    if _accepted_beyond(five, int(panic["index"]), side, zone_low, zone_high):
        return _waiting("PRICE_ACCEPTED_BEYOND_LEVEL", approach)

    # The inverse micro shift must occur after the 5m sweep candle has closed.
    post_event = [
        row for row in one
        if int(_num(row.get("t"))) >= int(panic["close_time"])
    ]
    if len(post_event) < 4:
        return _waiting("WAITING_FOR_POST_PANIC_MICRO_SHIFT", approach)

    latest = post_event[-1]
    prior_micro = post_event[-4:-1]
    op, high, low, close = (_num(latest.get(key)) for key in ("o", "h", "l", "c"))
    latest_metrics = candle_metrics(latest, atr1)
    if side == "BUY":
        micro_reference = max(_num(row.get("h")) for row in prior_micro)
        micro_confirmed = (
            close > micro_reference
            and close > op
            and latest_metrics["relative_strength"] >= 0.08
            and latest_metrics["close_position"] >= 0.60
        )
    else:
        micro_reference = min(_num(row.get("l")) for row in prior_micro)
        micro_confirmed = (
            close < micro_reference
            and close < op
            and latest_metrics["relative_strength"] >= 0.08
            and latest_metrics["close_position"] <= 0.40
        )
    if not micro_confirmed:
        return _waiting("WAITING_FOR_INVERSE_MICRO_STRUCTURE_BREAK", approach)

    entry = close
    stop_buffer = max(atr1 * 0.20, half_width * 0.10, entry * 0.00002)
    if side == "BUY":
        stop = float(panic["panic_extreme"]) - stop_buffer
        risk = entry - stop
        opposing = [
            item for item in active_levels
            if str(item.get("type", "")).upper() == "RESISTANCE"
            and _num(item.get("price")) > entry
        ]
        nearest = min(opposing, key=lambda item: _num(item.get("price")), default=None)
        if nearest is not None:
            opposing_target = _num(nearest.get("zone_low"), _num(nearest.get("price")))
            if opposing_target - entry < MIN_RR * risk:
                return _waiting("NEAREST_RESISTANCE_BLOCKS_1_TO_2_RR", approach)
            target = opposing_target
        else:
            target = entry + MIN_RR * risk
    else:
        stop = float(panic["panic_extreme"]) + stop_buffer
        risk = stop - entry
        opposing = [
            item for item in active_levels
            if str(item.get("type", "")).upper() == "SUPPORT"
            and _num(item.get("price")) < entry
        ]
        nearest = max(opposing, key=lambda item: _num(item.get("price")), default=None)
        if nearest is not None:
            opposing_target = _num(nearest.get("zone_high"), _num(nearest.get("price")))
            if entry - opposing_target < MIN_RR * risk:
                return _waiting("NEAREST_SUPPORT_BLOCKS_1_TO_2_RR", approach)
            target = opposing_target
        else:
            target = entry - MIN_RR * risk

    if risk <= 0 or abs(target - entry) / risk < MIN_RR:
        return _waiting("INVALID_RISK_REWARD", approach)

    pattern_analysis = candle_patterns(one, len(one) - 1, level)
    panic_tag = "PANIC_SWEEP_RECLAIM_BULLISH" if side == "BUY" else "PANIC_SWEEP_RECLAIM_BEARISH"
    return {
        "action": side,
        "reason": "PANIC_CONFIRMED",
        "approach": approach,
        "entry": round(entry, 8),
        "stop_loss": round(stop, 8),
        "take_profit": round(target, 8),
        "risk_reward": round(abs(target - entry) / risk, 3),
        "signal_time": int(_num(latest.get("t"))),
        "panic_time": int(panic["time"]),
        "panic_extreme": round(float(panic["panic_extreme"]), 8),
        "panic_range_atr": round(float(panic["range_atr"]), 3),
        "panic_volume_ratio": round(float(panic["volume_ratio"]), 3),
        "patterns": sorted(set(pattern_analysis["patterns"]) | {panic_tag}),
    }


class CandlePsychologyEntryEngine:
    """Small, independent micro-reaction engine; reads levels without changing them."""

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

        results: dict[str, dict[str, Any]] = {}
        for level in active_levels:
            key = level_key(level)
            previous_signal = self._triggered.get(key)
            if previous_signal is not None:
                signal_age = int(as_of_ms) - (
                    int(previous_signal.get("signal_time", 0)) + TIMEFRAME_MS["1m"]
                )
                if 0 <= signal_age <= SIGNAL_DISPLAY_MS:
                    results[key] = dict(previous_signal)
                else:
                    results[key] = _waiting("LEVEL_SIGNAL_ALREADY_USED")
                continue

            result = evaluate_level(level, active_levels, candles_by_tf, live_price, as_of_ms)
            if result.get("action") in ("BUY", "SELL"):
                self._triggered[key] = dict(result)
            results[key] = result
        return results


def backtest_trades(
    levels_by_step: list[list[dict[str, Any]]],
    candles_by_step: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replay the exact same local-level evaluator at each historical 1m close."""
    engine = CandlePsychologyEntryEngine()
    trades: list[dict[str, Any]] = []
    for index, step in enumerate(candles_by_step):
        timestamp = int(step.get("time", 0))
        levels = levels_by_step[index] if index < len(levels_by_step) else []
        decision_time = timestamp + TIMEFRAME_MS["1m"]
        signals = engine.evaluate(
            levels,
            step.get("candles", {}),
            step.get("price"),
            as_of_ms=decision_time,
        )
        for key, signal in signals.items():
            if signal.get("action") not in ("BUY", "SELL"):
                continue
            if int(signal.get("signal_time", -1)) != timestamp:
                continue
            trades.append({"level_key": key, **signal})
    return trades
