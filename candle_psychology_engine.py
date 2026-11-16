from __future__ import annotations

from bisect import bisect_right
from typing import Any
import math

TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
ENTRY_TFS = ("1h", "15m", "5m", "1m")
MIN_RR = 2.0
SIGNAL_DISPLAY_TTL_MS = 60_000


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def level_key(level: dict[str, Any]) -> str:
    """Stable key shared by the live level row and its independent candle entry."""
    side = str(level.get("type", "")).upper()
    price = _num(level.get("price", level.get("level_price")))
    return f"{side}:{price:.8f}"


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    ranges = []
    for index in range(max(1, len(candles) - period), len(candles)):
        high = _num(candles[index].get("h"))
        low = _num(candles[index].get("l"))
        previous_close = _num(candles[index - 1].get("c"))
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(ranges) / len(ranges) if ranges else 0.0


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _trend_direction(candles: list[dict[str, Any]]) -> str:
    """Closed-candle market direction; neutral/insufficient history blocks entries."""
    if len(candles) < 55:
        return "NEUTRAL"
    closes = [_num(row.get("c")) for row in candles[-100:]]
    fast = _ema(closes, 20)
    slow = _ema(closes, 50)
    previous_fast = _ema(closes[:-5], 20)
    last = closes[-1]
    if last > fast > slow and fast > previous_fast:
        return "BULLISH"
    if last < fast < slow and fast < previous_fast:
        return "BEARISH"
    return "NEUTRAL"


def candle_psychology(candle: dict[str, Any], previous: dict[str, Any] | None,
                      atr: float, volume_history: list[float] | None = None) -> dict[str, Any]:
    """Return candle shape percentages and named patterns without using an open candle."""
    op, high, low, close = (_num(candle.get(key)) for key in ("o", "h", "l", "c"))
    span = max(high - low, 1e-12)
    body = abs(close - op)
    upper = max(0.0, high - max(op, close))
    lower = max(0.0, min(op, close) - low)
    close_position = (close - low) / span
    body_pct = body / span * 100.0
    upper_pct = upper / span * 100.0
    lower_pct = lower / span * 100.0
    bullish = close > op
    bearish = close < op

    prev_op = _num(previous.get("o")) if previous else 0.0
    prev_close = _num(previous.get("c")) if previous else 0.0
    prev_body_high = max(prev_op, prev_close)
    prev_body_low = min(prev_op, prev_close)
    body_high = max(op, close)
    body_low = min(op, close)
    bullish_engulfing = bool(previous and bullish and prev_close < prev_op and
                              body_high >= prev_body_high and body_low <= prev_body_low)
    bearish_engulfing = bool(previous and bearish and prev_close > prev_op and
                              body_high >= prev_body_high and body_low <= prev_body_low)
    inside_bar = bool(previous and high <= _num(previous.get("h")) and low >= _num(previous.get("l")))
    outside_bar = bool(previous and high >= _num(previous.get("h")) and low <= _num(previous.get("l")))

    bullish_rejection = bullish and lower >= max(body * 1.15, span * 0.32) and close_position >= 0.62
    bearish_rejection = bearish and upper >= max(body * 1.15, span * 0.32) and close_position <= 0.38
    bullish_pin = body_pct <= 38.0 and lower_pct >= 52.0 and close_position >= 0.62
    bearish_pin = body_pct <= 38.0 and upper_pct >= 52.0 and close_position <= 0.38
    strong_bullish = bullish and body_pct >= 60.0 and close_position >= 0.75 and (not atr or body >= atr * 0.45)
    strong_bearish = bearish and body_pct >= 60.0 and close_position <= 0.25 and (not atr or body >= atr * 0.45)
    long_lower_wick = lower_pct >= 55.0 and close_position >= 0.58
    long_upper_wick = upper_pct >= 55.0 and close_position <= 0.42

    prior_volumes = [value for value in (volume_history or []) if value > 0]
    volume = _num(candle.get("v"))
    volume_ratio = None
    absorption = False
    if volume > 0 and len(prior_volumes) >= 10:
        average = sum(prior_volumes[-20:]) / len(prior_volumes[-20:])
        if average > 0:
            volume_ratio = volume / average
            absorption = volume_ratio >= 1.8 and body_pct <= 45.0 and (
                (long_lower_wick and bullish) or (long_upper_wick and bearish)
            )

    patterns = []
    if bullish_rejection:
        patterns.append("BULLISH_REJECTION")
    if bearish_rejection:
        patterns.append("BEARISH_REJECTION")
    if bullish_pin:
        patterns.append("BULLISH_PIN_BAR")
    if bearish_pin:
        patterns.append("BEARISH_PIN_BAR")
    if bullish_engulfing:
        patterns.append("BULLISH_ENGULFING")
    if bearish_engulfing:
        patterns.append("BEARISH_ENGULFING")
    if inside_bar:
        patterns.append("INSIDE_BAR")
    if outside_bar:
        patterns.append("OUTSIDE_BAR")
    if strong_bullish:
        patterns.append("STRONG_BULLISH_BODY")
    if strong_bearish:
        patterns.append("STRONG_BEARISH_BODY")
    if long_lower_wick:
        patterns.append("LONG_LOWER_WICK")
    if long_upper_wick:
        patterns.append("LONG_UPPER_WICK")
    if absorption:
        patterns.append("VOLUME_ABSORPTION")

    return {
        "body_pct": round(body_pct, 2),
        "upper_wick_pct": round(upper_pct, 2),
        "lower_wick_pct": round(lower_pct, 2),
        "close_position_pct": round(close_position * 100.0, 2),
        "relative_strength": round(body / atr, 3) if atr > 0 else None,
        "volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None,
        "patterns": patterns,
        "bullish_rejection": bullish_rejection or bullish_pin or long_lower_wick,
        "bearish_rejection": bearish_rejection or bearish_pin or long_upper_wick,
        "bullish_engulfing": bullish_engulfing,
        "bearish_engulfing": bearish_engulfing,
        "strong_bullish": strong_bullish,
        "strong_bearish": strong_bearish,
        "absorption": absorption,
    }


def _closed_frames(candles_by_tf: dict[str, Any], as_of_ms: int) -> dict[str, list[dict[str, Any]]]:
    """Use only candles whose close timestamp is at or before the evaluation time."""
    result: dict[str, list[dict[str, Any]]] = {}
    limits = {"1h": 120, "15m": 120, "5m": 80, "1m": 100}
    for timeframe in ENTRY_TFS:
        interval = TIMEFRAME_MS[timeframe]
        rows = candles_by_tf.get(timeframe) or []
        closed = []
        for row in reversed(rows):
            stamp = int(_num(row.get("t")))
            if stamp + interval <= as_of_ms:
                closed.append(row)
                if len(closed) >= limits[timeframe]:
                    break
        result[timeframe] = list(reversed(closed))
    return result


def _level_bounds(level: dict[str, Any]) -> tuple[float, float, float]:
    price = _num(level.get("price", level.get("level_price")))
    half_width = _num(level.get("half_width"))
    lower = _num(level.get("zone_low"), price - half_width)
    upper = _num(level.get("zone_high"), price + half_width)
    if lower <= 0 or upper < lower:
        lower, upper = price - half_width, price + half_width
    return price, lower, upper


def _approach_from(candles: list[dict[str, Any]], lower: float, upper: float) -> str:
    """Infer the side from which the latest level test was approached."""
    if not candles:
        return "UNKNOWN"
    start = max(0, len(candles) - 12)
    for index in range(len(candles) - 1, start - 1, -1):
        candle = candles[index]
        touched = _num(candle.get("l")) <= upper and _num(candle.get("h")) >= lower
        if not touched:
            continue
        if index > start:
            prior_close = _num(candles[index - 1].get("c"))
            if prior_close > upper:
                return "ABOVE"
            if prior_close < lower:
                return "BELOW"
        close = _num(candle.get("c"))
        return "ABOVE" if close >= (lower + upper) / 2.0 else "BELOW"
    prior_close = _num(candles[-1].get("c"))
    return "ABOVE" if prior_close > upper else "BELOW" if prior_close < lower else "UNKNOWN"


def _pattern_for_level(candle: dict[str, Any], previous: dict[str, Any] | None,
                       atr: float, side: str, lower: float, upper: float,
                       volume_history: list[float]) -> tuple[bool, list[str], dict[str, Any]]:
    profile = candle_psychology(candle, previous, atr, volume_history)
    low, high, close = (_num(candle.get(key)) for key in ("l", "h", "c"))
    support_sweep_reclaim = side == "BUY" and low < lower and close > (lower + upper) / 2.0
    resistance_sweep_reclaim = side == "SELL" and high > upper and close < (lower + upper) / 2.0
    sweep = support_sweep_reclaim or resistance_sweep_reclaim
    patterns = list(profile["patterns"])
    if sweep:
        patterns.append("LIQUIDITY_SWEEP_RECLAIM")
    directional_rejection = profile["bullish_rejection"] if side == "BUY" else profile["bearish_rejection"]
    directional_engulf = profile["bullish_engulfing"] if side == "BUY" else profile["bearish_engulfing"]
    directional_strong = profile["strong_bullish"] if side == "BUY" else profile["strong_bearish"]
    direction_close = close > _num(candle.get("o")) if side == "BUY" else close < _num(candle.get("o"))
    absorption = profile["absorption"] and direction_close
    return directional_rejection or directional_engulf or directional_strong or sweep or absorption, patterns, profile


def _has_recent_reaction(candles: list[dict[str, Any]], side: str, lower: float,
                         upper: float, lookback: int = 8) -> tuple[bool, int | None, list[str]]:
    if len(candles) < 4:
        return False, None, []
    atr = _atr(candles)
    start = max(0, len(candles) - lookback)
    touch_count = 0
    selected_index = None
    selected_patterns: list[str] = []
    for index in range(start, len(candles)):
        candle = candles[index]
        if _num(candle.get("l")) > upper or _num(candle.get("h")) < lower:
            continue
        touch_count += 1
        history = [_num(row.get("v")) for row in candles[max(0, index - 20):index]]
        previous = candles[index - 1] if index else None
        valid, patterns, profile = _pattern_for_level(
            candle, previous, atr, side, lower, upper, history
        )
        close = _num(candle.get("c"))
        reclaimed = close > (lower + upper) / 2.0 if side == "BUY" else close < (lower + upper) / 2.0
        if valid and reclaimed:
            selected_index = index
            selected_patterns = patterns
    repeated = touch_count >= 2 and selected_index is not None
    return selected_index is not None or repeated, selected_index, selected_patterns


def _waiting(reason: str, approach: str = "UNKNOWN") -> dict[str, Any]:
    return {"status": "WAITING", "approach_from": approach, "reason": reason}


def evaluate_level_entry(level: dict[str, Any], candles_by_tf: dict[str, Any],
                         live_price: float | None, all_levels: list[dict[str, Any]] | None = None,
                         as_of_ms: int | None = None) -> dict[str, Any]:
    """
    Pure live/replay evaluator. as_of_ms prevents any candle closing after that
    instant from participating. The live service and historical replay both call
    this exact function.
    """
    if as_of_ms is None:
        import time
        as_of_ms = int(time.time() * 1000)
    side_type = str(level.get("type", "")).upper()
    if side_type not in ("SUPPORT", "RESISTANCE"):
        return _waiting("INVALID_LEVEL")
    side = "BUY" if side_type == "SUPPORT" else "SELL"
    price, lower, upper = _level_bounds(level)
    if price <= 0 or upper <= lower:
        return _waiting("INVALID_LEVEL_ZONE")

    data = _closed_frames(candles_by_tf, int(as_of_ms))
    hourly, fifteen, five, one = (data[tf] for tf in ENTRY_TFS)
    if min(len(hourly), len(fifteen), len(five), len(one)) < 55:
        return _waiting("INSUFFICIENT_CLOSED_CANDLES")

    atr1, atr5 = _atr(one), _atr(five)
    if atr1 <= 0 or atr5 <= 0:
        return _waiting("ATR_NOT_READY")

    required_direction = "BULLISH" if side == "BUY" else "BEARISH"
    h1_direction = _trend_direction(hourly)
    m15_direction = _trend_direction(fifteen)
    if h1_direction != required_direction or m15_direction != required_direction:
        return _waiting("HIGHER_TIMEFRAME_CONFLICT_OR_NEUTRAL")

    current_price = _num(live_price)
    if current_price <= 0:
        current_price = _num(one[-1].get("c"))
    approach = _approach_from(one, lower, upper)
    expected_approach = "ABOVE" if side == "BUY" else "BELOW"
    recent_1m_touch = any(
        _num(row.get("l")) <= upper and _num(row.get("h")) >= lower
        for row in one[-12:]
    )
    distance = 0.0 if lower <= current_price <= upper else min(abs(current_price - lower), abs(current_price - upper))
    approach_buffer = max((upper - lower) * 1.5, atr1 * 0.75, price * 0.00003)
    if distance > approach_buffer and not recent_1m_touch:
        return _waiting("PRICE_NOT_NEAR_LEVEL", approach)
    if approach != expected_approach:
        return _waiting("APPROACH_SIDE_MISMATCH", approach)

    # Two decisive closed 5m closes beyond the far edge invalidate this setup.
    if side == "BUY" and all(_num(row.get("c")) < lower for row in five[-2:]):
        return _waiting("SUPPORT_DECISIVELY_BROKEN", approach)
    if side == "SELL" and all(_num(row.get("c")) > upper for row in five[-2:]):
        return _waiting("RESISTANCE_DECISIVELY_BROKEN", approach)

    # 15m must show an actual reaction at this same level, not trend direction alone.
    m15_reacted, _m15_index, _m15_patterns = _has_recent_reaction(fifteen, side, lower, upper, 8)
    if not m15_reacted:
        return _waiting("NO_15M_LEVEL_REACTION", approach)

    # 5m is the main reaction timeframe: rejection/sweep followed by a closed
    # directional candle that reclaims the zone and breaks micro structure.
    m5_reacted, reaction_index, reaction_patterns = _has_recent_reaction(five, side, lower, upper, 8)
    if not m5_reacted or reaction_index is None:
        return _waiting("NO_5M_REJECTION", approach)

    last5 = five[-1]
    prior5 = five[-4:-1]
    if len(prior5) < 3:
        return _waiting("NO_5M_STRUCTURE_WINDOW", approach)
    high5 = _num(last5.get("h"))
    low5 = _num(last5.get("l"))
    close5 = _num(last5.get("c"))
    open5 = _num(last5.get("o"))
    position5 = (close5 - low5) / max(high5 - low5, 1e-12)
    volume_history5 = [_num(row.get("v")) for row in five[max(0, len(five) - 21):-1]]
    pattern5 = candle_psychology(last5, five[-2], atr5, volume_history5)
    if side == "BUY":
        bos5 = close5 > max(_num(row.get("h")) for row in prior5)
        reclaim5 = close5 > upper and close5 > open5 and position5 >= 0.62
        displacement5 = pattern5["strong_bullish"] or pattern5["bullish_engulfing"]
        directional_rejection5 = pattern5["bullish_rejection"]
    else:
        bos5 = close5 < min(_num(row.get("l")) for row in prior5)
        reclaim5 = close5 < lower and close5 < open5 and position5 <= 0.38
        displacement5 = pattern5["strong_bearish"] or pattern5["bearish_engulfing"]
        directional_rejection5 = pattern5["bearish_rejection"]
    if not (bos5 and reclaim5 and (displacement5 or directional_rejection5)):
        return _waiting("NO_5M_CLOSED_BOS_OR_RECLAIM", approach)

    # 1m is the final entry trigger and must close at/after the 5m confirmation.
    last1 = one[-1]
    prior1 = one[-4:-1]
    if len(prior1) < 3:
        return _waiting("NO_1M_STRUCTURE_WINDOW", approach)
    close_time5 = int(_num(last5.get("t"))) + TIMEFRAME_MS["5m"]
    close_time1 = int(_num(last1.get("t"))) + TIMEFRAME_MS["1m"]
    if close_time1 < close_time5:
        return _waiting("WAITING_FOR_1M_AFTER_5M_CLOSE", approach)

    high1 = _num(last1.get("h"))
    low1 = _num(last1.get("l"))
    close1 = _num(last1.get("c"))
    open1 = _num(last1.get("o"))
    position1 = (close1 - low1) / max(high1 - low1, 1e-12)
    volume_history1 = [_num(row.get("v")) for row in one[max(0, len(one) - 21):-1]]
    pattern1 = candle_psychology(last1, one[-2], atr1, volume_history1)
    if side == "BUY":
        bos1 = close1 > max(_num(row.get("h")) for row in prior1)
        trigger1 = close1 > open1 and position1 >= 0.62 and (
            pattern1["bullish_rejection"] or pattern1["bullish_engulfing"] or pattern1["strong_bullish"]
        )
    else:
        bos1 = close1 < min(_num(row.get("l")) for row in prior1)
        trigger1 = close1 < open1 and position1 <= 0.38 and (
            pattern1["bearish_rejection"] or pattern1["bearish_engulfing"] or pattern1["strong_bearish"]
        )
    if not (bos1 and trigger1):
        return _waiting("NO_1M_CLOSED_BOS_OR_CANDLE_TRIGGER", approach)

    # Stop is beyond the actual rejection swing, with a volatility buffer.
    rejection_5m = five[max(reaction_index, len(five) - 8):]
    rejection_1m = one[-12:]
    buffer = max(atr1 * 0.20, price * 0.00002)
    entry = close1
    if side == "BUY":
        swing = min([lower] + [_num(row.get("l")) for row in rejection_5m + rejection_1m])
        stop = swing - buffer
        risk = entry - stop
        target = entry + MIN_RR * risk
        opposing = [
            item for item in (all_levels or [])
            if str(item.get("type", "")).upper() == "RESISTANCE"
            and _num(item.get("price", item.get("level_price"))) > entry
        ]
        nearest = min(opposing, key=lambda item: _num(item.get("price", item.get("level_price"))), default=None)
        if nearest is not None and _num(nearest.get("zone_low"), _num(nearest.get("price"))) < target:
            return _waiting("OPPOSING_LEVEL_BLOCKS_MINIMUM_1_TO_2_RR", approach)
    else:
        swing = max([upper] + [_num(row.get("h")) for row in rejection_5m + rejection_1m])
        stop = swing + buffer
        risk = stop - entry
        target = entry - MIN_RR * risk
        opposing = [
            item for item in (all_levels or [])
            if str(item.get("type", "")).upper() == "SUPPORT"
            and _num(item.get("price", item.get("level_price"))) < entry
        ]
        nearest = max(opposing, key=lambda item: _num(item.get("price", item.get("level_price"))), default=None)
        if nearest is not None and _num(nearest.get("zone_high"), _num(nearest.get("price"))) > target:
            return _waiting("OPPOSING_LEVEL_BLOCKS_MINIMUM_1_TO_2_RR", approach)

    if risk <= 0:
        return _waiting("INVALID_STOP_DISTANCE", approach)
    return {
        "status": side,
        "approach_from": approach,
        "entry": round(entry, 8),
        "stop_loss": round(stop, 8),
        "take_profit": round(target, 8),
        "risk_reward": round(abs(target - entry) / risk, 3),
        "signal_time": close_time1,
        "patterns": sorted(set(reaction_patterns + pattern5["patterns"] + pattern1["patterns"])),
    }


def backtest_candle_entries(level_snapshots: list[dict[str, Any]],
                            candles_by_tf: dict[str, Any],
                            max_bars: int = 5_000) -> list[dict[str, Any]]:
    """
    Historical replay for the entry layer. Each level snapshot is
    {"time": milliseconds, "levels": [level, ...]} and must be captured from
    the unchanged Market Levels Engine at that historical time. The replay
    calls evaluate_level_entry(), the same pure evaluator used live, using only
    bars closed at each 1m decision time. Time-indexed snapshots avoid look-ahead
    from applying today's levels to yesterday's candles.
    """
    if not level_snapshots:
        return []
    one_minute = list(candles_by_tf.get("1m") or [])
    if not one_minute:
        return []
    snapshots = sorted(level_snapshots, key=lambda item: int(_num(item.get("time"))))
    snapshot_times = [int(_num(item.get("time"))) for item in snapshots]
    frame_rows: dict[str, list[dict[str, Any]]] = {
        timeframe: list(candles_by_tf.get(timeframe) or []) for timeframe in ENTRY_TFS
    }
    close_times = {
        timeframe: [int(_num(row.get("t"))) + TIMEFRAME_MS[timeframe] for row in rows]
        for timeframe, rows in frame_rows.items()
    }
    consumed: set[str] = set()
    trades: list[dict[str, Any]] = []
    for one_bar in one_minute[-max(1, max_bars):]:
        decision_time = int(_num(one_bar.get("t"))) + TIMEFRAME_MS["1m"]
        snapshot_index = bisect_right(snapshot_times, decision_time) - 1
        if snapshot_index < 0:
            continue
        active_levels = snapshots[snapshot_index].get("levels") or []
        if not active_levels:
            continue
        historical_frames = {}
        for timeframe in ENTRY_TFS:
            end = bisect_right(close_times[timeframe], decision_time)
            historical_frames[timeframe] = frame_rows[timeframe][max(0, end - 120):end]
        for level in active_levels:
            key = level_key(level)
            if key in consumed:
                continue
            result = evaluate_level_entry(
                level, historical_frames, _num(one_bar.get("c")),
                active_levels, decision_time,
            )
            if result["status"] in ("BUY", "SELL"):
                consumed.add(key)
                trades.append({
                    "level_key": key,
                    "decision_time": decision_time,
                    **result,
                })
    return trades


class CandlePsychologyEntryEngine:
    """Independent candle-reaction engine; reads levels and candles, never mutates them."""

    def __init__(self, asset: str = "MARKET") -> None:
        self.asset = asset.upper()
        self._consumed_levels: set[str] = set()
        self._last_signal: dict[str, dict[str, Any]] = {}

    def snapshot(self, candles_by_tf: dict[str, Any], active_levels: list[dict[str, Any]],
                 live_price: float | None, as_of_ms: int) -> dict[str, dict[str, Any]]:
        active_keys = {level_key(level) for level in active_levels}
        # A level lifecycle ending in the source engine releases its duplicate lock.
        self._consumed_levels.intersection_update(active_keys)
        self._last_signal = {
            key: value for key, value in self._last_signal.items() if key in active_keys
        }
        results: dict[str, dict[str, Any]] = {}
        for level in active_levels:
            key = level_key(level)
            if key in self._consumed_levels:
                previous = self._last_signal.get(key)
                if previous and int(as_of_ms) - int(previous.get("signal_time", 0)) <= SIGNAL_DISPLAY_TTL_MS:
                    results[key] = previous
                else:
                    results[key] = _waiting("LEVEL_ENTRY_ALREADY_EMITTED")
                continue

            result = evaluate_level_entry(level, candles_by_tf, live_price, active_levels, as_of_ms)
            if result["status"] in ("BUY", "SELL"):
                self._consumed_levels.add(key)
                self._last_signal[key] = result
            results[key] = result
        return results
