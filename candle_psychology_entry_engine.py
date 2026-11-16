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


def evaluate_level(
    level: dict[str, Any],
    active_levels: list[dict[str, Any]],
    candles_by_tf: dict[str, Any],
    live_price: float | None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    """
    Fast level-local decision. It reads only the last closed 1m candles around
    this active level: no 1H/15M trend gates and no distant-market analysis.
    """
    if as_of_ms is None:
        import time
        as_of_ms = int(time.time() * 1000)

    level_type = str(level.get("type", "")).upper()
    side = "BUY" if level_type == "SUPPORT" else "SELL" if level_type == "RESISTANCE" else None
    if side is None:
        return _waiting("INVALID_LEVEL")

    one = _closed_rows(candles_by_tf, "1m", as_of_ms)[-20:]
    if len(one) < 4:
        return _waiting("WAITING_FOR_LOCAL_CANDLES")

    level_price, zone_low, zone_high, half_width = _level_zone(level)
    current_price = _num(live_price, _num(one[-1].get("c")))
    if level_price <= 0 or current_price <= 0:
        return _waiting("WAITING_FOR_PRICE")

    atr = _atr(one)
    local_buffer = max(half_width * 2.5, atr * 2.0, level_price * 0.00008)
    distance = (
        0.0 if zone_low <= current_price <= zone_high
        else min(abs(current_price - zone_low), abs(current_price - zone_high))
    )
    if distance > local_buffer:
        return _waiting("PRICE_NOT_NEAR_LEVEL")

    approach = _approach(one, zone_low, zone_high)
    expected_approach = "FROM_ABOVE" if side == "BUY" else "FROM_BELOW"

    # Stop analysing a support/resistance after two decisive closed 1m closes
    # through the far side of its zone.
    last_two = one[-2:]
    if side == "BUY" and all(_num(row.get("c")) < zone_low for row in last_two):
        return _waiting("SUPPORT_BROKEN", approach)
    if side == "SELL" and all(_num(row.get("c")) > zone_high for row in last_two):
        return _waiting("RESISTANCE_BROKEN", approach)
    if approach not in (expected_approach, "INSIDE_ZONE"):
        return _waiting("APPROACH_SIDE_MISMATCH", approach)

    accepted = _BULLISH_REACTIONS if side == "BUY" else _BEARISH_REACTIONS
    reaction_index = None
    reaction_tags: list[str] = []
    # Require a level rejection with enough closed candles afterwards to form
    # and confirm a separate micro swing sequence.
    reaction_start = max(1, len(one) - 10)
    for index in range(len(one) - 5, reaction_start - 1, -1):
        row = one[index]
        touched = _num(row.get("l")) <= zone_high and _num(row.get("h")) >= zone_low
        if not touched:
            continue
        analysis = candle_patterns(one, index, level)
        matched = [tag for tag in analysis["patterns"] if tag in accepted]
        close = _num(row.get("c"))
        op = _num(row.get("o"))
        close_pos = analysis["metrics"].get("close_position", 0.5)
        reclaimed = (
            close > level_price and close > op and close_pos >= 0.55
            if side == "BUY"
            else close < level_price and close < op and close_pos <= 0.45
        )
        if matched and reclaimed:
            reaction_index = index
            reaction_tags = matched
            break

    if reaction_index is None:
        return _waiting("WAITING_FOR_LEVEL_REJECTION", approach)

    # BUY: rejection low -> local push high -> higher-low pullback -> close
    # above the push high. SELL is the exact inverse.
    latest = one[-1]
    op, high, low, close = (_num(latest.get(key)) for key in ("o", "h", "l", "c"))
    latest_metrics = candle_metrics(latest, atr)
    micro_buffer = max(atr * 0.03, level_price * 0.000001)
    micro_confirmed = False
    micro_tags: list[str] = []
    micro_swing_price = 0.0
    pullback_price = 0.0

    if side == "BUY":
        reaction_extreme = _num(one[reaction_index].get("l"))
        for pullback_index in range(reaction_index + 3, len(one) - 1):
            pullback_low = _num(one[pullback_index].get("l"))
            left_low = _num(one[pullback_index - 1].get("l"))
            right_low = _num(one[pullback_index + 1].get("l"))
            is_higher_low = (
                pullback_low < left_low
                and pullback_low <= right_low
                and pullback_low > reaction_extreme + micro_buffer
            )
            if not is_higher_low:
                continue
            pivot_highs = []
            for pivot_index in range(reaction_index + 2, pullback_index):
                pivot_high = _num(one[pivot_index].get("h"))
                if (
                    pivot_high > _num(one[pivot_index - 1].get("h"))
                    and pivot_high >= _num(one[pivot_index + 1].get("h"))
                ):
                    pivot_highs.append(pivot_high)
            if not pivot_highs:
                continue
            swing_high = max(pivot_highs)
            if swing_high <= level_price + micro_buffer:
                continue
            if any(
                _num(item.get("l")) <= pullback_low
                for item in one[pullback_index + 1:-1]
            ):
                continue
            if close > swing_high + micro_buffer:
                micro_confirmed = (
                    close > op
                    and latest_metrics["close_position"] >= 0.60
                    and latest_metrics["body"] >= max(atr * 0.08, micro_buffer)
                )
                if micro_confirmed:
                    micro_swing_price = swing_high
                    pullback_price = pullback_low
                    micro_tags = ["MICRO_HIGHER_LOW", "MICRO_BOS_BULLISH"]
                    break
    else:
        reaction_extreme = _num(one[reaction_index].get("h"))
        for pullback_index in range(reaction_index + 3, len(one) - 1):
            pullback_high = _num(one[pullback_index].get("h"))
            left_high = _num(one[pullback_index - 1].get("h"))
            right_high = _num(one[pullback_index + 1].get("h"))
            is_lower_high = (
                pullback_high > left_high
                and pullback_high >= right_high
                and pullback_high < reaction_extreme - micro_buffer
            )
            if not is_lower_high:
                continue
            pivot_lows = []
            for pivot_index in range(reaction_index + 2, pullback_index):
                pivot_low = _num(one[pivot_index].get("l"))
                if (
                    pivot_low < _num(one[pivot_index - 1].get("l"))
                    and pivot_low <= _num(one[pivot_index + 1].get("l"))
                ):
                    pivot_lows.append(pivot_low)
            if not pivot_lows:
                continue
            swing_low = min(pivot_lows)
            if swing_low >= level_price - micro_buffer:
                continue
            if any(
                _num(item.get("h")) >= pullback_high
                for item in one[pullback_index + 1:-1]
            ):
                continue
            if close < swing_low - micro_buffer:
                micro_confirmed = (
                    close < op
                    and latest_metrics["close_position"] <= 0.40
                    and latest_metrics["body"] >= max(atr * 0.08, micro_buffer)
                )
                if micro_confirmed:
                    micro_swing_price = swing_low
                    pullback_price = pullback_high
                    micro_tags = ["MICRO_LOWER_HIGH", "MICRO_BOS_BEARISH"]
                    break

    if not micro_confirmed:
        return _waiting("WAITING_FOR_MICRO_STRUCTURE_SHIFT", approach)

    entry = close
    local_reaction = one[reaction_index:]
    entry = close
    local_reaction = one[max(0, reaction_index - 1):]
    stop_buffer = max(atr * 0.20, half_width * 0.10, entry * 0.00002)
    if side == "BUY":
        rejection_swing = min(_num(row.get("l")) for row in local_reaction)
        stop = rejection_swing - stop_buffer
        risk = entry - stop
        target = entry + MIN_RR * risk
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
        rejection_swing = max(_num(row.get("h")) for row in local_reaction)
        stop = rejection_swing + stop_buffer
        risk = stop - entry
        target = entry - MIN_RR * risk
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

    if risk <= 0:
        return _waiting("INVALID_STOP", approach)

    return {
        "action": side,
        "reason": "CONFIRMED",
        "approach": approach,
        "entry": round(entry, 8),
        "stop_loss": round(stop, 8),
        "take_profit": round(target, 8),
        "risk_reward": round(abs(target - entry) / risk, 3),
        "signal_time": int(_num(latest.get("t"))),
        "patterns": sorted(set(reaction_tags + micro_tags)),
        "micro_swing_price": round(micro_swing_price, 8),
        "micro_pullback_price": round(pullback_price, 8),
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
