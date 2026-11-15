from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any

TIMEFRAME_ORDER = ("1d", "4h", "1h", "30m", "15m", "5m", "1m")
TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
ANALYSIS_TFS = ("1d", "4h", "1h", "30m", "15m")
STRUCTURE_TFS = ("1d", "4h", "1h", "30m", "15m")
TF_WEIGHT = {"1d": 5.0, "4h": 4.0, "1h": 3.0, "30m": 2.0, "15m": 1.0}
MAX_LOOKBACK = 900
MAX_PIVOTS_PER_TF = 140
MIN_LEVEL_SCORE = 65
MIN_REACTION_RATE = 0.60
MIN_RR = 2.0


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


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
            if stamp <= 0 or stamp + interval > now:
                continue
            if any(not math.isfinite(x) for x in (op, high, low, close)):
                continue
            if low <= 0 or high < max(op, close, low) or low > min(op, close, high):
                continue
            cleaned[stamp] = {
                "t": stamp, "o": op, "h": high, "l": low, "c": close, "v": volume,
            }
        except (TypeError, ValueError, AttributeError):
            continue
    return [cleaned[key] for key in sorted(cleaned)][-MAX_LOOKBACK:]


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


def _candidate_levels(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    max_age = {"1d": 100, "4h": 150, "1h": 180, "30m": 220, "15m": 260}
    for timeframe in ANALYSIS_TFS:
        candles = data.get(timeframe) or []
        if len(candles) < 32:
            continue
        pivot_highs, pivot_lows = _confirmed_pivots(candles)
        pivots = [("RESISTANCE", index, price) for index, price in pivot_highs]
        pivots.extend(("SUPPORT", index, price) for index, price in pivot_lows)
        pivots.sort(key=lambda item: item[1])
        for side, index, price in pivots[-MAX_PIVOTS_PER_TF:]:
            atr_at_pivot = _atr(candles, index)
            if atr_at_pivot <= 0 or price <= 0:
                continue
            half_width = max(price * 0.00008, min(atr_at_pivot * 0.14, price * 0.0010))
            same_side_pivots = pivot_highs if side == "RESISTANCE" else pivot_lows
            liquidity_tolerance = max(atr_at_pivot * 0.20, half_width * 1.5)
            liquidity_pool = any(
                other_index != index and abs(float(other_price) - price) <= liquidity_tolerance
                for other_index, other_price in same_side_pivots[-120:]
            )
            stats = _reaction_stats(candles, index, price, half_width, atr_at_pivot, side)
            if stats["touches"] < 1 or stats["reactions"] < 1 or stats["broken"]:
                continue
            age = max(0, len(candles) - 1 - int(stats["last_touch_index"]))
            freshness = max(0.0, 1.0 - age / max_age[timeframe])
            if freshness < 0.03:
                continue
            candidates.append({
                "side": side,
                "price": price,
                "half_width": half_width,
                "timeframe": timeframe,
                "touches": int(stats["touches"]),
                "reactions": int(stats["reactions"]),
                "freshness": freshness,
                "volume_ratio": _volume_ratio(candles, index),
                "liquidity_pool": liquidity_pool,
                "last_touch_time": int(candles[int(stats["last_touch_index"])]["t"]),
            })
    return candidates


def _merge_levels(candidates: list[dict[str, Any]], data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for candidate in sorted(candidates, key=lambda item: (item["side"], item["price"])):
        match: list[dict[str, Any]] | None = None
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
            weight = TF_WEIGHT[item["timeframe"]] * (1.0 + min(item["reactions"], 3) * 0.25) * (0.5 + item["freshness"])
            weighted.append((float(item["price"]), weight))
        total_weight = sum(weight for _, weight in weighted)
        price = sum(value * weight for value, weight in weighted) / total_weight if total_weight else float(group[0]["price"])
        half_width = max(float(item["half_width"]) + abs(float(item["price"]) - price) for item in group)
        half_width = min(half_width, price * 0.0018)
        touches = sum(int(item["touches"]) for item in group)
        reactions = sum(int(item["reactions"]) for item in group)
        probability = reactions / touches if touches else 0.0
        source_tfs = sorted({str(item["timeframe"]) for item in group}, key=lambda tf: TIMEFRAME_ORDER.index(tf))
        freshness = max(float(item["freshness"]) for item in group)
        volume_ratio = sum(float(item["volume_ratio"]) for item in group) / len(group)
        if touches >= 12 or (touches >= 8 and probability < 0.65):
            continue
        liquidity_evidence = any(bool(item.get("liquidity_pool")) for item in group)
        reaction_score = 31.0 * probability
        touch_score = 16.0 * min(touches / 4.0, 1.0)
        confluence_score = 20.0 * min(len(source_tfs) / 3.0, 1.0)
        freshness_score = 15.0 * freshness
        volume_score = 10.0 * min(max(volume_ratio - 0.8, 0.0) / 1.2, 1.0)
        liquidity_score = 8.0 if liquidity_evidence else 0.0
        consumption_penalty = min(max(touches - 5, 0) * 4.0, 16.0)
        score = max(0.0, min(100.0, reaction_score + touch_score + confluence_score + freshness_score + volume_score + liquidity_score - consumption_penalty))
        if touches < 2 or reactions < 1 or probability < MIN_REACTION_RATE or score < MIN_LEVEL_SCORE:
            continue
        if len(source_tfs) < 2 and reactions < 3:
            continue
        level = {
            "type": group[0]["side"],
            "price": price,
            "zone_low": price - half_width,
            "zone_high": price + half_width,
            "half_width": half_width,
            "strength_score": int(round(score)),
            "reaction_probability": round(probability * 100.0, 2),
            "historical_touches": touches,
            "historical_reactions": reactions,
            "source_timeframes": source_tfs,
            "timeframe_confluence": len(source_tfs),
            "freshness": round(freshness, 3),
            "last_touch_time": max(int(item["last_touch_time"]) for item in group),
            "volume_ratio": round(volume_ratio, 3),
        }
        if _decisively_broken(level, data):
            continue
        merged.append(level)
    return merged


def _decisively_broken(level: dict[str, Any], data: dict[str, list[dict[str, Any]]]) -> bool:
    side = level["type"]
    lower, upper = float(level["zone_low"]), float(level["zone_high"])
    for timeframe in ("1d", "4h", "1h", "30m", "15m"):
        candles = data.get(timeframe) or []
        if len(candles) < 2:
            continue
        last_two = candles[-2:]
        if side == "SUPPORT" and all(float(row["c"]) < lower for row in last_two):
            return True
        if side == "RESISTANCE" and all(float(row["c"]) > upper for row in last_two):
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
    """Historical-reaction and multi-timeframe structure based support/resistance engine."""

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
            for timeframe in TIMEFRAME_ORDER
        }
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
            visible_supports = sorted(
                supports, key=lambda item: (abs(price - float(item["price"])), -int(item["strength_score"]))
            )[:3]
            visible_resistances = sorted(
                resistances, key=lambda item: (abs(price - float(item["price"])), -int(item["strength_score"]))
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
        ready = all(len(data.get(tf) or []) >= 32 for tf in ("1d", "4h", "1h", "30m", "15m"))
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
