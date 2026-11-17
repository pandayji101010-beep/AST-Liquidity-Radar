from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

TIMEFRAME_ORDER = ("1y", "1mo", "1w", "1d", "4h", "1h", "30m", "15m", "5m", "1m")
TIMEFRAME_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    "1w": 7 * 86_400_000, "1mo": 31 * 86_400_000, "1y": 366 * 86_400_000,
}
ANALYSIS_TFS = ("1y", "1mo", "1w", "1d", "4h", "1h")
STRUCTURE_TFS = ANALYSIS_TFS
TF_WEIGHT = {"1h": 3.0, "4h": 4.0, "1d": 5.0, "1w": 6.0, "1mo": 7.0, "1y": 8.0}
TIMEFRAME_LOOKBACK = {
    "1m": 900, "5m": 900, "15m": 900, "30m": 900,
    "1h": 3_000, "4h": 2_000, "1d": 5_000, "1w": 520, "1mo": 240, "1y": 30,
}
MAX_LOOKBACK = 5_000
MAX_PIVOTS_PER_TF = 120
MIN_LEVEL_SCORE = 58
MIN_REACTION_RATE = 0.60
MIN_RR = 2.0


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _calendar_close_ms(stamp_ms: int, timeframe: str) -> int:
    """Return the exact UTC close timestamp for calendar-sized candles."""
    dt = datetime.fromtimestamp(stamp_ms / 1000.0, tz=timezone.utc)
    if timeframe == "1w":
        end = dt + timedelta(days=7)
    elif timeframe == "1mo":
        year, month = dt.year, dt.month
        end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
    elif timeframe == "1y":
        end = datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = dt + timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    return int(end.timestamp() * 1000)


def _clean_candles(rows: Any, timeframe: str, now: int) -> list[dict[str, float | int]]:
    cleaned: dict[int, dict[str, float | int]] = {}
    interval = TIMEFRAME_MS[timeframe]
    if rows is None:
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            stamp = int(row.get("t", 0))
            if 0 < stamp < 10_000_000_000:
                stamp *= 1000
            op, high, low, close = (_number(row.get(key), float("nan")) for key in ("o", "h", "l", "c"))
            volume = max(0.0, _number(row.get("v")))
            close_time = _calendar_close_ms(stamp, timeframe) if timeframe in ("1w", "1mo", "1y") else stamp + interval
            if stamp <= 0 or close_time > now:
                continue
            if any(not math.isfinite(x) for x in (op, high, low, close)):
                continue
            if low <= 0 or high < max(op, close, low) or low > min(op, close, high):
                continue
            cleaned[stamp] = {"t": stamp, "o": op, "h": high, "l": low, "c": close, "v": volume}
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
    limit = TIMEFRAME_LOOKBACK.get(timeframe, MAX_LOOKBACK)
    return [cleaned[key] for key in sorted(cleaned)][-limit:]


def _resample_calendar_timeframes(daily: list[dict[str, Any]], now_ms: int) -> dict[str, list[dict[str, Any]]]:
    """Build only completed UTC weekly, monthly and yearly OHLCV bars from closed daily candles."""
    grouped: dict[str, dict[int, list[dict[str, Any]]]] = {
        "1w": defaultdict(list), "1mo": defaultdict(list), "1y": defaultdict(list),
    }
    for candle in daily:
        dt = datetime.fromtimestamp(int(candle["t"]) / 1000.0, tz=timezone.utc)
        week_start = dt - timedelta(days=dt.weekday())
        month_start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        year_start = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        grouped["1w"][int(week_start.timestamp() * 1000)].append(candle)
        grouped["1mo"][int(month_start.timestamp() * 1000)].append(candle)
        grouped["1y"][int(year_start.timestamp() * 1000)].append(candle)

    result: dict[str, list[dict[str, Any]]] = {}
    for timeframe, buckets in grouped.items():
        bars: list[dict[str, Any]] = []
        for start_ms, items in sorted(buckets.items()):
            if _calendar_close_ms(start_ms, timeframe) > now_ms:
                continue
            end_ms = _calendar_close_ms(start_ms, timeframe)
            expected_days = max(1, int((end_ms - start_ms) // 86_400_000))
            # Reject a partially covered archive/listing period rather than fabricating a full candle.
            if len(items) < max(1, int(expected_days * 0.75)):
                continue
            items = sorted(items, key=lambda row: int(row["t"]))
            bars.append({
                "t": start_ms, "o": float(items[0]["o"]),
                "h": max(float(row["h"]) for row in items),
                "l": min(float(row["l"]) for row in items),
                "c": float(items[-1]["c"]),
                "v": sum(float(row.get("v") or 0.0) for row in items),
            })
        result[timeframe] = bars[-TIMEFRAME_LOOKBACK[timeframe]:]
    return result

def _atr(candles: list[dict[str, Any]], end: int | None = None, period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    last = len(candles) - 1 if end is None else min(end, len(candles) - 1)
    start = max(1, last - period + 1)
    true_ranges: list[float] = []
    for index in range(start, last + 1):
        high = float(candles[index]["h"])
        low = float(candles[index]["l"])
        prior_close = float(candles[index - 1]["c"])
        true_ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
    return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0


def _confirmed_pivots(
    candles: list[dict[str, Any]],
    left: int = 3,
    right: int = 2,
    lookback: int = MAX_LOOKBACK,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs = [float(row["h"]) for row in candles]
    lows = [float(row["l"]) for row in candles]
    start = max(left, len(candles) - lookback)
    end = len(candles) - right
    pivot_highs: list[tuple[int, float]] = []
    pivot_lows: list[tuple[int, float]] = []
    for index in range(start, end):
        high_window = highs[index - left:index + right + 1]
        low_window = lows[index - left:index + right + 1]
        if len(high_window) != left + right + 1:
            continue
        if highs[index] == max(high_window) and highs[index] > highs[index - 1] and highs[index] >= highs[index + 1]:
            pivot_highs.append((index, highs[index]))
        if lows[index] == min(low_window) and lows[index] < lows[index - 1] and lows[index] <= lows[index + 1]:
            pivot_lows.append((index, lows[index]))
    return pivot_highs, pivot_lows


def _reaction_stats(
    candles: list[dict[str, Any]],
    pivot_index: int,
    level: float,
    half_width: float,
    atr_at_pivot: float,
    side: str,
) -> dict[str, Any]:
    """Count separated historical tests and the reactions that followed those tests."""
    if not candles or pivot_index >= len(candles) - 2:
        return {"touches": 0, "reactions": 0, "last_touch_index": pivot_index, "broken": False}
    events: list[int] = []
    last_intersection: int | None = None
    for index in range(pivot_index, len(candles) - 2):
        candle = candles[index]
        intersects = float(candle["l"]) <= level + half_width and float(candle["h"]) >= level - half_width
        if intersects and (last_intersection is None or index - last_intersection > 2):
            events.append(index)
        if intersects:
            last_intersection = index

    reactions = 0
    broken = False
    response_distance = max(atr_at_pivot * 0.45, half_width * 1.8)
    for event_index in events:
        end = min(len(candles), event_index + 9)
        reacted = False
        break_streak = 0
        for index in range(event_index + 1, end):
            candle = candles[index]
            close = float(candle["c"])
            broke_here = close < level - half_width * 1.5 if side == "SUPPORT" else close > level + half_width * 1.5
            break_streak = break_streak + 1 if broke_here else 0
            if break_streak >= 2:
                broken = True
                break
            moved_away = (
                float(candle["h"]) >= level + response_distance
                if side == "SUPPORT"
                else float(candle["l"]) <= level - response_distance
            )
            if moved_away:
                reacted = True
                break
        if reacted:
            reactions += 1
    last_touch = events[-1] if events else pivot_index
    return {
        "touches": len(events),
        "reactions": reactions,
        "last_touch_index": last_touch,
        "broken": broken,
    }


def _volume_ratio(candles: list[dict[str, Any]], index: int) -> float:
    current = _number(candles[index].get("v"))
    history = [_number(row.get("v")) for row in candles[max(0, index - 20):index]]
    history = [value for value in history if value > 0]
    if current <= 0 or not history:
        return 1.0
    average = sum(history) / len(history)
    return current / average if average > 0 else 1.0


def _rejection_quality(candle: dict[str, Any], side: str) -> float:
    op, high, low, close = (_number(candle.get(key)) for key in ("o", "h", "l", "c"))
    span = max(high - low, 1e-12)
    wick = min(op, close) - low if side == "SUPPORT" else high - max(op, close)
    close_position = (close - low) / span
    aligned = close_position >= 0.52 if side == "SUPPORT" else close_position <= 0.48
    return max(0.0, wick / span) if aligned else 0.0


def _return_events(candles: list[dict[str, Any]], start_index: int, level: float, half_width: float) -> list[int]:
    """Count distinct returns after price has departed from a level."""
    events: list[int] = []
    in_touch = False
    for index in range(max(0, start_index), len(candles)):
        row = candles[index]
        intersects = _number(row.get("l")) <= level + half_width and _number(row.get("h")) >= level - half_width
        if intersects and not in_touch:
            events.append(index)
        in_touch = intersects
    return events


def _is_broken_after(candles: list[dict[str, Any]], start_index: int, side: str, lower: float, upper: float) -> bool:
    streak = 0
    for row in candles[max(0, start_index):]:
        close = _number(row.get("c"))
        broken = close < lower if side == "SUPPORT" else close > upper
        streak = streak + 1 if broken else 0
        if streak >= 2:
            return True
    return False


def _pivot_candidate(
    candles: list[dict[str, Any]], timeframe: str, index: int,
    side: str, price: float, liquidity_pool: bool = False,
) -> dict[str, Any] | None:
    """Keep strong HTF pivots that are untested or in their current first retest."""
    atr = _atr(candles, index)
    if atr <= 0 or price <= 0 or index >= len(candles) - 2:
        return None
    half_width = max(price * 0.00012, min(atr * 0.16, price * 0.0035))
    lookahead = {"1h": 18, "4h": 12, "1d": 8, "1w": 6, "1mo": 5, "1y": 4}.get(timeframe, 8)
    future = candles[index + 1:min(len(candles), index + lookahead + 1)]
    if not future:
        return None
    excursions = [
        _number(row.get("h")) - price if side == "SUPPORT" else price - _number(row.get("l"))
        for row in future
    ]
    impulse_atr = max(excursions, default=0.0) / atr
    if impulse_atr < 1.0:
        return None

    departure_threshold = max(atr * 0.95, half_width * 3.0)
    departure_index = next(
        (offset for offset, excursion in enumerate(excursions, start=index + 1) if excursion >= departure_threshold),
        None,
    )
    if departure_index is None:
        return None

    lower, upper = price - half_width, price + half_width
    events = _return_events(candles, departure_index + 1, price, half_width)
    if len(events) > 1:
        return None
    pending_age = {"1h": 4, "4h": 3, "1d": 2, "1w": 1, "1mo": 1, "1y": 1}.get(timeframe, 2)
    touch_age = len(candles) - 1 - events[0] if events else 0
    if events and touch_age > pending_age:
        return None
    if _is_broken_after(candles, departure_index, side, lower, upper):
        return None

    origin = candles[index]
    wick_ratio = _rejection_quality(origin, side)
    freshness_span = {"1h": 1_500, "4h": 900, "1d": 900, "1w": 208, "1mo": 96, "1y": 10}.get(timeframe, 180)
    age = max(0, len(candles) - 1 - index)
    freshness = max(0.0, 1.0 - age / freshness_span)
    if freshness < 0.05:
        return None
    return {
        "side": side, "price": price, "half_width": half_width, "timeframe": timeframe,
        "touches": 1 if events else 0, "reactions": 0,
        "touch_state": "FIRST_TOUCH" if events else "UNTESTED",
        "first_touch_time": int(candles[events[0]]["t"]) if events else None,
        "last_touch_time": int(candles[events[0]]["t"]) if events else int(origin["t"]),
        "freshness": freshness, "impulse_atr": impulse_atr, "wick_ratio": wick_ratio,
        "volume_ratio": _volume_ratio(candles, index), "liquidity_pool": liquidity_pool,
        "origin_time": int(origin["t"]), "origin_kind": "SWING",
    }


def _period_extreme_candidates(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Add completed week/month/year highs and lows when the origin candle shows rejection."""
    candidates: list[dict[str, Any]] = []
    daily = data.get("1d") or []
    if len(daily) < 20:
        return candidates
    for timeframe, max_periods in (("1w", 20), ("1mo", 24), ("1y", 10)):
        periods = data.get(timeframe) or []
        if len(periods) < 2:
            continue
        daily_atr = _atr(daily)
        fallback = daily_atr * {"1w": 4.5, "1mo": 18.0, "1y": 180.0}[timeframe]
        for index in range(max(0, len(periods) - max_periods), len(periods)):
            candle = periods[index]
            atr = _atr(periods, index) or fallback
            if atr <= 0:
                continue
            op, high, low, close = (_number(candle.get(key)) for key in ("o", "h", "l", "c"))
            span = max(high - low, 1e-12)
            upper_wick, lower_wick = high - max(op, close), min(op, close) - low
            close_position = (close - low) / span
            period_close = _calendar_close_ms(int(candle["t"]), timeframe)
            first_daily = next((i for i, row in enumerate(daily) if int(row["t"]) >= period_close), len(daily))
            for side, price, wick, reversal_distance in (
                ("RESISTANCE", high, upper_wick, high - close),
                ("SUPPORT", low, lower_wick, close - low),
            ):
                wick_ratio = wick / span
                if wick_ratio < 0.18 and reversal_distance < atr * 0.35:
                    continue
                if side == "RESISTANCE" and close_position > 0.52:
                    continue
                if side == "SUPPORT" and close_position < 0.48:
                    continue
                half_width = max(price * 0.00012, min(atr * 0.10, price * 0.0035))
                lower, upper = price - half_width, price + half_width
                events = _return_events(daily, first_daily, price, half_width)
                if len(events) > 1:
                    continue
                touch_age = len(daily) - 1 - events[0] if events else 0
                if events and touch_age > 5:
                    continue
                if _is_broken_after(daily, first_daily, side, lower, upper):
                    continue
                period_age = max(0, len(periods) - 1 - index)
                freshness = max(0.0, 1.0 - period_age / {"1w": 156, "1mo": 72, "1y": 10}[timeframe])
                if freshness < 0.05:
                    continue
                candidates.append({
                    "side": side, "price": price, "half_width": half_width, "timeframe": timeframe,
                    "touches": 1 if events else 0, "reactions": 0,
                    "touch_state": "FIRST_TOUCH" if events else "UNTESTED",
                    "first_touch_time": int(daily[events[0]]["t"]) if events else None,
                    "last_touch_time": int(daily[events[0]]["t"]) if events else int(candle["t"]),
                    "freshness": freshness, "impulse_atr": max(0.35, reversal_distance / atr),
                    "wick_ratio": wick_ratio, "volume_ratio": _volume_ratio(periods, index),
                    "liquidity_pool": False, "origin_time": int(candle["t"]),
                    "origin_kind": "PERIOD_EXTREME", "period_age": period_age,
                })
    return candidates


def _candidate_levels(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    minimum_bars = {"1h": 48, "4h": 32, "1d": 32, "1w": 12, "1mo": 8, "1y": 5}
    for timeframe in ANALYSIS_TFS:
        candles = data.get(timeframe) or []
        if len(candles) < minimum_bars[timeframe]:
            continue
        pivot_highs, pivot_lows = _confirmed_pivots(candles)
        pivots = [("RESISTANCE", index, price) for index, price in pivot_highs]
        pivots.extend(("SUPPORT", index, price) for index, price in pivot_lows)
        pivots.sort(key=lambda item: item[1])
        for side, index, price in pivots[-MAX_PIVOTS_PER_TF:]:
            atr = _atr(candles, index)
            if atr <= 0:
                continue
            same_side = pivot_highs if side == "RESISTANCE" else pivot_lows
            tolerance = max(atr * 0.25, price * 0.0002)
            liquidity_pool = sum(
                1 for other_index, other_price in same_side[-120:]
                if other_index != index and abs(float(other_price) - price) <= tolerance
            ) >= 1
            candidate = _pivot_candidate(candles, timeframe, index, side, float(price), liquidity_pool)
            if candidate is not None:
                candidates.append(candidate)
    candidates.extend(_period_extreme_candidates(data))
    return candidates


def _merge_levels(candidates: list[dict[str, Any]], data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Cluster aligned HTF levels and rank them by timeframe, displacement and freshness."""
    groups: list[list[dict[str, Any]]] = []
    for candidate in sorted(candidates, key=lambda item: (item["side"], item["price"])):
        match = None
        for group in reversed(groups):
            if group[0]["side"] != candidate["side"]:
                continue
            center = sum(float(item["price"]) for item in group) / len(group)
            tolerance = max(float(candidate["half_width"]), max(float(item["half_width"]) for item in group))
            if abs(float(candidate["price"]) - center) <= tolerance * 1.5:
                match = group
                break
        if match is None:
            groups.append([candidate])
        else:
            match.append(candidate)

    merged: list[dict[str, Any]] = []
    for group in groups:
        weighted = []
        for item in group:
            tf_weight = TF_WEIGHT.get(str(item["timeframe"]), 2.0)
            weight = tf_weight * (1.0 + min(float(item.get("impulse_atr", 0.0)), 3.0) * 0.15)
            weighted.append((float(item["price"]), weight * (0.7 + float(item.get("freshness", 0.0)))))
        total_weight = sum(weight for _, weight in weighted)
        price = sum(value * weight for value, weight in weighted) / total_weight if total_weight else float(group[0]["price"])
        half_width = max(float(item["half_width"]) + abs(float(item["price"]) - price) for item in group)
        half_width = min(half_width, price * 0.0040)
        source_tfs = sorted({str(item["timeframe"]) for item in group}, key=lambda tf: TIMEFRAME_ORDER.index(tf))
        max_tf_weight = max(TF_WEIGHT.get(tf, 2.0) for tf in source_tfs)
        max_impulse = max(float(item.get("impulse_atr", 0.0)) for item in group)
        max_wick = max(float(item.get("wick_ratio", 0.0)) for item in group)
        volume_ratio = sum(float(item.get("volume_ratio", 1.0)) for item in group) / len(group)
        freshness = max(float(item.get("freshness", 0.0)) for item in group)
        liquidity = any(bool(item.get("liquidity_pool")) for item in group)
        touches = max(int(item.get("touches", 0)) for item in group)
        touch_state = "FIRST_TOUCH" if touches else "UNTESTED"

        score = (
            25.0 + (max_tf_weight - 3.0) * 3.0
            + min(max(len(source_tfs) - 1, 0) * 7.0, 21.0)
            + 20.0 * min(max_impulse / 2.5, 1.0)
            + 10.0 * min(max_wick / 0.45, 1.0)
            + 5.0 * min(max(volume_ratio - 1.0, 0.0) / 1.5, 1.0)
            + (5.0 if liquidity else 0.0)
            + 8.0 * freshness
            + (6.0 if touches == 0 else 3.0)
        )
        score = max(0.0, min(100.0, score))
        if score < MIN_LEVEL_SCORE or max_impulse < 1.0:
            continue

        first_touch_times = [int(item["first_touch_time"]) for item in group if item.get("first_touch_time") is not None]
        level = {
            "type": group[0]["side"], "price": price,
            "zone_low": price - half_width, "zone_high": price + half_width,
            "half_width": half_width, "strength_score": int(round(score)),
            "reaction_probability": None,
            "historical_touches": touches, "historical_reactions": 0,
            "source_timeframes": source_tfs, "origin_timeframe": source_tfs[0],
            "timeframe_confluence": len(source_tfs), "freshness": round(freshness, 3),
            "touch_state": touch_state,
            "last_touch_time": max(int(item["last_touch_time"]) for item in group),
            "first_touch_time": min(first_touch_times) if first_touch_times else None,
            "departure_atr": round(max_impulse, 2),
            "volume_ratio": round(volume_ratio, 3),
            "origin_kinds": sorted({str(item.get("origin_kind", "SWING")) for item in group}),
        }
        if _decisively_broken(level, data):
            continue
        merged.append(level)
    return merged


def _decisively_broken(level: dict[str, Any], data: dict[str, list[dict[str, Any]]]) -> bool:
    side = level["type"]
    lower, upper = float(level["zone_low"]), float(level["zone_high"])
    for timeframe in ("1h", "4h", "1d", "1w", "1mo", "1y"):
        candles = data.get(timeframe) or []
        if len(candles) >= 2 and _is_broken_after(candles, 0, side, lower, upper):
            return True
    return False

def _structure_bias(candles: list[dict[str, Any]]) -> str:
    if len(candles) < 25:
        return "NEUTRAL"
    highs, lows = _confirmed_pivots(candles[-180:], left=2, right=2, lookback=180)
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"
    higher_high = highs[-1][1] > highs[-2][1]
    higher_low = lows[-1][1] > lows[-2][1]
    lower_high = highs[-1][1] < highs[-2][1]
    lower_low = lows[-1][1] < lows[-2][1]
    if higher_high and higher_low:
        return "BULLISH"
    if lower_high and lower_low:
        return "BEARISH"
    return "NEUTRAL"


def _higher_timeframe_bias(data: dict[str, list[dict[str, Any]]]) -> tuple[str, dict[str, str]]:
    detail = {timeframe: _structure_bias(data.get(timeframe) or []) for timeframe in STRUCTURE_TFS}
    bullish = sum(value == "BULLISH" for value in detail.values())
    bearish = sum(value == "BEARISH" for value in detail.values())
    if bullish >= 3 and bearish <= 1:
        return "BULLISH", detail
    if bearish >= 3 and bullish <= 1:
        return "BEARISH", detail
    return "MIXED", detail


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _timeframe_bias(candles: list[dict[str, Any]]) -> dict[str, str]:
    """Historical structure, present trend and a momentum-based forward projection."""
    if len(candles) < 25:
        return {
            "historical": "NEUTRAL",
            "present": "NEUTRAL",
            "projected": "NEUTRAL",
            "bias": "NEUTRAL",
        }

    closes = [float(row["c"]) for row in candles]
    historical = _structure_bias(candles[-180:])

    recent = closes[-min(120, len(closes)):]
    ema20 = _ema(recent, 20)
    ema50 = _ema(recent, 50)
    prior_ema20 = _ema(recent[:-5], 20) if len(recent) > 25 else ema20
    slope = ema20 - prior_ema20
    last_close = closes[-1]
    if last_close > ema20 and ema20 > ema50 and slope > 0:
        present = "BULLISH"
    elif last_close < ema20 and ema20 < ema50 and slope < 0:
        present = "BEARISH"
    else:
        present = "NEUTRAL"

    atr = _atr(candles)
    momentum = last_close - closes[-6] if len(closes) >= 6 else 0.0
    threshold = atr * 0.30
    if atr > 0 and momentum > threshold and slope > 0:
        projected = "BULLISH"
    elif atr > 0 and momentum < -threshold and slope < 0:
        projected = "BEARISH"
    else:
        projected = "NEUTRAL"

    weights = {"historical": 0.35, "present": 0.40, "projected": 0.25}
    score = sum(
        weights[key] * (1 if value == "BULLISH" else -1 if value == "BEARISH" else 0)
        for key, value in (
            ("historical", historical),
            ("present", present),
            ("projected", projected),
        )
    )
    overall = "BULLISH" if score >= 0.25 else "BEARISH" if score <= -0.25 else "NEUTRAL"
    return {
        "historical": historical,
        "present": present,
        "projected": projected,
        "bias": overall,
    }


def _candle_shape(candle: dict[str, Any]) -> tuple[float, float, float, float]:
    op, high, low, close = (float(candle[key]) for key in ("o", "h", "l", "c"))
    span = max(high - low, 1e-12)
    body = abs(close - op)
    close_position = (close - low) / span
    upper_wick = high - max(op, close)
    lower_wick = min(op, close) - low
    return body, close_position, upper_wick, lower_wick


def _rejection_and_break(
    candles: list[dict[str, Any]],
    level: dict[str, Any],
    side: str,
    timeframe: str,
    lookback: int,
) -> bool:
    if len(candles) < lookback + 5:
        return False
    atr = _atr(candles)
    if atr <= 0:
        return False
    current = candles[-1]
    previous = candles[-lookback - 1:-1]
    recent_tests = candles[-lookback - 1:-1]
    rejection_found = False
    for candle in recent_tests:
        body, close_position, upper_wick, lower_wick = _candle_shape(candle)
        touched = (
            float(candle["l"]) <= float(level["zone_high"])
            and float(candle["h"]) >= float(level["zone_low"])
        )
        if side == "BUY":
            rejection_found = rejection_found or (
                touched and float(candle["c"]) > float(candle["o"])
                and lower_wick >= max(body * 0.70, atr * 0.05)
                and close_position >= 0.62
            )
        else:
            rejection_found = rejection_found or (
                touched and float(candle["c"]) < float(candle["o"])
                and upper_wick >= max(body * 0.70, atr * 0.05)
                and close_position <= 0.38
            )
    body, close_position, _upper, _lower = _candle_shape(current)
    if side == "BUY":
        structure_break = float(current["c"]) > max(float(row["h"]) for row in previous)
        confirmation = (
            float(current["c"]) > float(current["o"])
            and body >= atr * 0.05 and close_position >= 0.60
        )
        return rejection_found and structure_break and confirmation
    structure_break = float(current["c"]) < min(float(row["l"]) for row in previous)
    confirmation = (
        float(current["c"]) < float(current["o"])
        and body >= atr * 0.05 and close_position <= 0.40
    )
    return rejection_found and structure_break and confirmation


def _entry_confirmation(
    level: dict[str, Any],
    data: dict[str, list[dict[str, Any]]],
    live_price: float,
    htf_bias: str,
    all_levels: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    side = "BUY" if level["type"] == "SUPPORT" else "SELL"
    required_bias = "BULLISH" if side == "BUY" else "BEARISH"
    if htf_bias != required_bias:
        return "WAITING", None
    five_minute = data.get("5m") or []
    one_minute = data.get("1m") or []
    if len(five_minute) < 35 or len(one_minute) < 35:
        return "WAITING", None
    atr5 = _atr(five_minute)
    atr1 = _atr(one_minute)
    if atr5 <= 0 or atr1 <= 0:
        return "WAITING", None
    max_distance = max(float(level["half_width"]) * 4.0, atr5 * 0.9)
    if abs(live_price - float(level["price"])) > max_distance:
        return "WAITING", None
    if not _rejection_and_break(five_minute, level, side, "5m", 4):
        return "WAITING", None
    if not _rejection_and_break(one_minute, level, side, "1m", 4):
        return "WAITING", None

    buffer = max(atr1 * 0.15, live_price * 0.00002)
    if side == "BUY":
        stop = float(level["zone_low"]) - buffer
        risk = live_price - stop
        opposing = [
            item for item in all_levels
            if item["type"] == "RESISTANCE" and float(item["price"]) > live_price
        ]
        nearest = min(opposing, key=lambda item: float(item["price"])) if opposing else None
        target = float(nearest["zone_low"]) if nearest else live_price + MIN_RR * risk
        reward = target - live_price
    else:
        stop = float(level["zone_high"]) + buffer
        risk = stop - live_price
        opposing = [
            item for item in all_levels
            if item["type"] == "SUPPORT" and float(item["price"]) < live_price
        ]
        nearest = max(opposing, key=lambda item: float(item["price"])) if opposing else None
        target = float(nearest["zone_high"]) if nearest else live_price - MIN_RR * risk
        reward = live_price - target

    if risk <= 0 or reward <= 0 or reward / risk < MIN_RR:
        return "WAITING", None
    return side, {
        "entry": live_price,
        "stop_loss": stop,
        "take_profit": target,
        "risk_reward": round(reward / risk, 3),
        "signal_time": int(one_minute[-1]["t"]),
    }


class HighConfidenceMarketLevelsEngine:
    """Strong 1H–1Y swing and period-extreme levels, prioritising first unmitigated retests."""

    def __init__(self, asset: str = "MARKET") -> None:
        self.asset = asset.upper()
        self._signature: tuple[Any, ...] | None = None
        self._levels_cache: list[dict[str, Any]] = []
        self._bias_cache: tuple[str, dict[str, str]] = ("MIXED", {})

    @staticmethod
    def _data_signature(data: dict[str, list[dict[str, Any]]]) -> tuple[Any, ...]:
        result: list[Any] = []
        for timeframe in TIMEFRAME_ORDER:
            candles = data.get(timeframe) or []
            tail = tuple(
                (int(row["t"]), round(float(row["h"]), 8), round(float(row["l"]), 8), round(float(row["c"]), 8))
                for row in candles[-12:]
            )
            result.append((timeframe, len(candles), int(candles[0]["t"]) if candles else 0, tail))
        return tuple(result)

    def _refresh_levels(self, data: dict[str, list[dict[str, Any]]]) -> None:
        candidates = _candidate_levels(data)
        self._levels_cache = _merge_levels(candidates, data)
        self._bias_cache = _higher_timeframe_bias(data)
        self._signature = self._data_signature(data)

    def snapshot(self, candles_by_tf: dict[str, Any], live_price: float | None = None) -> dict[str, Any]:
        now = int(time.time() * 1000)
        data = {
            timeframe: _clean_candles(candles_by_tf.get(timeframe, []), timeframe, now)
            for timeframe in TIMEFRAME_ORDER if timeframe not in ("1w", "1mo", "1y")
        }
        data.update(_resample_calendar_timeframes(data.get("1d") or [], now))
        for timeframe in ("1w", "1mo", "1y"):
            data[timeframe] = _clean_candles(data.get(timeframe, []), timeframe, now)
        signature = self._data_signature(data)
        if self._signature != signature:
            self._refresh_levels(data)

        price = _number(live_price)
        if price <= 0:
            for timeframe in ("1m", "5m", "15m", "1h"):
                rows = data.get(timeframe) or []
                if rows:
                    price = float(rows[-1]["c"])
                    break

        levels: list[dict[str, Any]] = []
        if price > 0:
            supports = [
                item for item in self._levels_cache
                if item["type"] == "SUPPORT" and float(item["price"]) <= price + float(item["half_width"])
            ]
            resistances = [
                item for item in self._levels_cache
                if item["type"] == "RESISTANCE" and float(item["price"]) >= price - float(item["half_width"])
            ]
            # Select the strongest major levels first; show selected levels nearest-first.
            visible_supports = sorted(
                supports, key=lambda item: (-int(item["strength_score"]), abs(price - float(item["price"])))
            )[:3]
            visible_resistances = sorted(
                resistances, key=lambda item: (-int(item["strength_score"]), abs(price - float(item["price"])))
            )[:3]
            chosen = visible_supports + visible_resistances
            htf_bias, structure = self._bias_cache
            confirmations: list[tuple[dict[str, Any], str, dict[str, Any] | None]] = []
            for level in chosen:
                status, plan = _entry_confirmation(level, data, price, htf_bias, self._levels_cache)
                confirmations.append((level, status, plan))
            active = [row for row in confirmations if row[1] in ("BUY", "SELL")]
            selected_signal = None
            if active:
                selected_signal = max(
                    active,
                    key=lambda row: (int(row[0]["strength_score"]), -abs(price - float(row[0]["price"]))),
                )
            display = []
            for level, status, plan in confirmations:
                final_status = status if selected_signal and level is selected_signal[0] else "WAITING"
                distance = abs(price - float(level["price"]))
                signal_time = plan.get("signal_time") if plan else None
                display.append({
                    "type": level["type"],
                    "level_price": round(float(level["price"]), 8),
                    "distance": round(distance, 8),
                    "distance_pct": round(distance / price * 100.0, 4) if price else None,
                    "strength_score": int(level["strength_score"]),
                    "status": final_status,
                    "reaction_probability": level["reaction_probability"],
                    "historical_touches": level["historical_touches"],
                    "historical_reactions": level["historical_reactions"],
                    "timeframe_confluence": level["timeframe_confluence"],
                    "source_timeframes": level["source_timeframes"],
                    "origin_timeframe": level["origin_timeframe"],
                    "touch_state": level["touch_state"],
                    "departure_atr": level["departure_atr"],
                    "first_touch_time": level["first_touch_time"],
                    "signal_time": signal_time,
                })
            display.sort(key=lambda item: (item["distance"], -item["strength_score"]))
            for rank, item in enumerate(display, start=1):
                item["rank"] = rank
            levels = display

        timeframe_bias = {
            timeframe: _timeframe_bias(data.get(timeframe) or [])
            for timeframe in TIMEFRAME_ORDER
        }
        available = sum(len(data.get(tf) or []) for tf in ANALYSIS_TFS)
        # New listings may not yet have a completed annual candle; monthly history is sufficient to scan.
        ready = (
            len(data.get("1h") or []) >= 32
            and len(data.get("4h") or []) >= 32
            and len(data.get("1d") or []) >= 25
            and len(data.get("1w") or []) >= 8
            and len(data.get("1mo") or []) >= 3
        )
        status = "READY" if levels else "NO TRADE" if ready else "WARMING UP"
        return {
            "asset": self.asset,
            "strategy_name": "AST High-Confidence Market Levels Engine",
            "status": status,
            "live_reference_price": round(price, 8) if price > 0 else None,
            "levels": levels,
            "timeframe_data": {timeframe: len(data.get(timeframe) or []) for timeframe in TIMEFRAME_ORDER},
            "timeframe_bias": timeframe_bias,
            "higher_timeframe_structure": self._bias_cache[1],
            "higher_timeframe_bias": self._bias_cache[0],
            "history_ready": ready,
            "analysis_bars": available,
        }
