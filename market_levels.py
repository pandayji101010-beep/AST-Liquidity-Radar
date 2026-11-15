from __future__ import annotations

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
LEVEL_TIMEFRAMES = ("1d", "4h", "1h", "30m", "15m")
TIMEFRAME_WEIGHT = {"1d": 30, "4h": 26, "1h": 22, "30m": 14, "15m": 10}
MAX_AGE_BARS = {"1d": 260, "4h": 520, "1h": 900, "30m": 1_200, "15m": 1_800}
MIN_LEVEL_SCORE = 65
MIN_REACTION_SAMPLES = 3
MIN_REACTION_RATE = 0.62


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _clean_rows(rows: Any, timeframe: str, now_ms: int) -> list[dict[str, float | int]]:
    interval = TIMEFRAME_MS[timeframe]
    by_time: dict[int, dict[str, float | int]] = {}
    for row in rows or []:
        try:
            stamp = int(row.get("t", 0))
            op, high, low, close = (_num(row.get(key), float("nan")) for key in ("o", "h", "l", "c"))
            volume = max(0.0, _num(row.get("v")))
            if stamp <= 0 or stamp + interval > now_ms:
                continue
            if any(x != x or abs(x) == float("inf") for x in (op, high, low, close)):
                continue
            if low <= 0 or high < max(op, close, low) or low > min(op, close, high):
                continue
            by_time[stamp] = {"t": stamp, "o": op, "h": high, "l": low, "c": close, "v": volume}
        except (AttributeError, TypeError, ValueError):
            continue
    return [by_time[key] for key in sorted(by_time)]


def _atr(rows: list[dict[str, Any]], period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    start = max(1, len(rows) - period)
    ranges = []
    for index in range(start, len(rows)):
        candle = rows[index]
        previous_close = float(rows[index - 1]["c"])
        ranges.append(max(
            float(candle["h"]) - float(candle["l"]),
            abs(float(candle["h"]) - previous_close),
            abs(float(candle["l"]) - previous_close),
        ))
    return sum(ranges) / len(ranges) if ranges else 0.0


def _pivots(rows: list[dict[str, Any]], left: int = 3, right: int = 2) -> tuple[list[int], list[int]]:
    highs = [float(row["h"]) for row in rows]
    lows = [float(row["l"]) for row in rows]
    pivot_highs: list[int] = []
    pivot_lows: list[int] = []
    start = max(left, len(rows) - 1_900 - right)
    end = len(rows) - right
    for index in range(start, end):
        high_window = highs[index - left:index + right + 1]
        low_window = lows[index - left:index + right + 1]
        if len(high_window) != left + right + 1:
            continue
        if highs[index] == max(high_window) and highs[index] > highs[index - 1] and highs[index] >= highs[index + 1]:
            pivot_highs.append(index)
        if lows[index] == min(low_window) and lows[index] < lows[index - 1] and lows[index] <= lows[index + 1]:
            pivot_lows.append(index)
    return pivot_highs, pivot_lows


def _volume_ratio(rows: list[dict[str, Any]], index: int) -> float:
    history = [float(row.get("v", 0.0)) for row in rows[max(0, index - 20):index]]
    positive = [value for value in history if value > 0]
    current = float(rows[index].get("v", 0.0))
    if not positive or current <= 0:
        return 1.0
    average = sum(positive) / len(positive)
    return max(0.25, min(4.0, current / average)) if average else 1.0


def _level_is_broken(
    level: float,
    width: float,
    side: str,
    candles_by_tf: dict[str, list[dict[str, Any]]],
    current_price: float,
) -> bool:
    threshold = width * 1.35
    if side == "SUPPORT" and current_price >= level - threshold:
        return False
    if side == "RESISTANCE" and current_price <= level + threshold:
        return False
    for timeframe in ("15m", "30m", "1h", "4h", "1d"):
        rows = candles_by_tf.get(timeframe, [])
        if len(rows) < 2:
            continue
        first, second = float(rows[-2]["c"]), float(rows[-1]["c"])
        if side == "SUPPORT" and first < level - threshold and second < level - threshold:
            return True
        if side == "RESISTANCE" and first > level + threshold and second > level + threshold:
            return True
    return False


def _candidate_reaction_stats(
    rows: list[dict[str, Any]],
    pivot_index: int,
    price: float,
    width: float,
    side: str,
    atr: float,
) -> tuple[int, int]:
    # The confirmed pivot itself is the first observed reaction. Later retests
    # count once per separated visit; movement is checked only on future bars.
    samples = successes = 1
    last_touch = pivot_index
    break_margin = width * 1.35
    required_move = max(atr * 0.45, width * 2.0)
    for index in range(pivot_index + 3, len(rows) - 5):
        candle = rows[index]
        intersects = float(candle["l"]) <= price + width and float(candle["h"]) >= price - width
        if not intersects or index - last_touch < 4:
            continue
        future = rows[index + 1:index + 6]
        if len(future) < 5:
            continue
        samples += 1
        last_touch = index
        if side == "SUPPORT":
            broken = any(float(item["c"]) < price - break_margin for item in future)
            excursion = max(float(item["h"]) for item in future) - price
        else:
            broken = any(float(item["c"]) > price + break_margin for item in future)
            excursion = price - min(float(item["l"]) for item in future)
        if not broken and excursion >= required_move:
            successes += 1
    return samples, successes


def _detect_candidates(candles_by_tf: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for timeframe in LEVEL_TIMEFRAMES:
        rows = candles_by_tf.get(timeframe, [])
        if len(rows) < 60:
            continue
        atr = _atr(rows)
        if atr <= 0:
            continue
        highs, lows = _pivots(rows)
        max_age = MAX_AGE_BARS[timeframe]
        for side, indices, key in (
            ("RESISTANCE", highs, "h"),
            ("SUPPORT", lows, "l"),
        ):
            for index in indices:
                age = len(rows) - 1 - index
                if age > max_age:
                    continue
                price = float(rows[index][key])
                width = max(atr * 0.18, price * 0.00012)
                samples, successes = _candidate_reaction_stats(rows, index, price, width, side, atr)
                candidates.append({
                    "side": side,
                    "price": price,
                    "width": width,
                    "timeframe": timeframe,
                    "samples": samples,
                    "successes": successes,
                    "freshness": max(0.0, 1.0 - age / max_age),
                    "volume_ratio": _volume_ratio(rows, index),
                    "pivot_time": int(rows[index]["t"]),
                })
    return candidates


def _merge_levels(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for side in ("SUPPORT", "RESISTANCE"):
        remaining = sorted(
            (candidate for candidate in candidates if candidate["side"] == side),
            key=lambda candidate: candidate["price"],
        )
        groups: list[list[dict[str, Any]]] = []
        for candidate in remaining:
            low = float(candidate["price"]) - float(candidate["width"])
            high = float(candidate["price"]) + float(candidate["width"])
            match = None
            for group in groups:
                group_low = min(float(item["price"]) - float(item["width"]) for item in group)
                group_high = max(float(item["price"]) + float(item["width"]) for item in group)
                if low <= group_high and high >= group_low:
                    match = group
                    break
            if match is None:
                groups.append([candidate])
            else:
                match.append(candidate)

        for group in groups:
            timeframes = {str(item["timeframe"]) for item in group}
            if len(timeframes) < 2 or not (timeframes & {"1d", "4h", "1h"}):
                continue
            samples = sum(int(item["samples"]) for item in group)
            successes = sum(int(item["successes"]) for item in group)
            if samples < MIN_REACTION_SAMPLES:
                continue
            reaction_rate = successes / samples if samples else 0.0
            if reaction_rate < MIN_REACTION_RATE:
                continue

            weights = [
                TIMEFRAME_WEIGHT[str(item["timeframe"])]
                * max(0.2, float(item["freshness"]))
                * max(0.2, float(item["successes"]) / max(1, int(item["samples"])))
                for item in group
            ]
            total_weight = sum(weights)
            price = sum(float(item["price"]) * weight for item, weight in zip(group, weights)) / total_weight
            width = max(float(item["width"]) for item in group)
            confluence = min(100, sum(TIMEFRAME_WEIGHT[tf] for tf in timeframes))
            sample_quality = min(1.0, samples / 8.0)
            freshness = sum(float(item["freshness"]) for item in group) / len(group)
            volume_quality = sum(min(1.0, float(item["volume_ratio"]) / 2.0) for item in group) / len(group)
            strength = round(
                45.0 * reaction_rate
                + 25.0 * (confluence / 100.0)
                + 15.0 * sample_quality
                + 10.0 * freshness
                + 5.0 * volume_quality
            )
            if strength < MIN_LEVEL_SCORE:
                continue
            merged.append({
                "side": side,
                "price": price,
                "width": width,
                "strength_score": max(0, min(100, strength)),
                "reaction_rate": reaction_rate,
                "reaction_samples": samples,
                "timeframes": sorted(timeframes, key=lambda tf: LEVEL_TIMEFRAMES.index(tf)),
                "last_pivot_time": max(int(item["pivot_time"]) for item in group),
            })
    return merged


def _rejection(candle: dict[str, Any], side: str, atr: float) -> bool:
    op, high, low, close = (float(candle[key]) for key in ("o", "h", "l", "c"))
    span = max(high - low, 1e-12)
    body = abs(close - op)
    if atr <= 0 or body < atr * 0.06:
        return False
    upper_wick = high - max(op, close)
    lower_wick = min(op, close) - low
    close_position = (close - low) / span
    if side == "SUPPORT":
        return close > op and lower_wick >= max(body * 0.65, atr * 0.08) and close_position >= 0.64
    return close < op and upper_wick >= max(body * 0.65, atr * 0.08) and close_position <= 0.36


def _micro_bos(rows: list[dict[str, Any]], side: str, bars: int) -> bool:
    if len(rows) < bars + 2:
        return False
    previous = rows[-(bars + 1):-1]
    last_close = float(rows[-1]["c"])
    if side == "SUPPORT":
        return last_close > max(float(item["h"]) for item in previous)
    return last_close < min(float(item["l"]) for item in previous)


def _entry_status(
    level: dict[str, Any],
    candles_by_tf: dict[str, list[dict[str, Any]]],
    current_price: float,
    opposing_levels: list[dict[str, Any]],
) -> str:
    side = str(level["side"])
    one = candles_by_tf.get("1m", [])
    five = candles_by_tf.get("5m", [])
    if len(one) < 12 or len(five) < 12:
        return "WAITING"
    atr_one, atr_five = _atr(one), _atr(five)
    if atr_one <= 0 or atr_five <= 0:
        return "WAITING"

    width = float(level["width"])
    price = float(level["price"])
    zone_low, zone_high = price - width, price + width
    near_limit = max(width * 1.5, atr_one * 0.75)
    if abs(current_price - price) > near_limit:
        return "WAITING"

    recent_one = one[-4:-1]
    recent_five = five[-3:-1]
    one_rejection = any(
        _rejection(candle, side, atr_one)
        and float(candle["l"]) <= zone_high
        and float(candle["h"]) >= zone_low
        for candle in recent_one
    )
    five_rejection = any(
        _rejection(candle, side, atr_five)
        and float(candle["l"]) <= zone_high
        and float(candle["h"]) >= zone_low
        for candle in recent_five
    )
    one_bos = _micro_bos(one, side, 3)
    five_bos = _micro_bos(five, side, 4)
    if not (one_rejection and five_rejection and one_bos and five_bos):
        return "WAITING"

    entry = float(one[-1]["c"])
    if side == "SUPPORT":
        stop = zone_low - max(atr_one * 0.35, width * 0.20)
        targets = [float(item["price"]) for item in opposing_levels
                   if item["side"] == "RESISTANCE" and float(item["price"]) > entry]
        if not targets:
            return "WAITING"
        risk = entry - stop
        reward = min(targets) - entry
        return "BUY" if risk > 0 and reward / risk >= 2.0 else "WAITING"

    stop = zone_high + max(atr_one * 0.35, width * 0.20)
    targets = [float(item["price"]) for item in opposing_levels
               if item["side"] == "SUPPORT" and float(item["price"]) < entry]
    if not targets:
        return "WAITING"
    risk = stop - entry
    reward = entry - max(targets)
    return "SELL" if risk > 0 and reward / risk >= 2.0 else "WAITING"


class HighConfidenceMarketLevelsEngine:
    """Historical-reaction and multi-timeframe support/resistance level engine."""

    def __init__(self, asset: str = "MARKET") -> None:
        self.asset = asset.upper()

    def snapshot(
        self,
        candles: dict[str, Any],
        current_price: float | None,
        now_ms: int,
    ) -> dict[str, Any]:
        closed = {
            timeframe: _clean_rows(candles.get(timeframe, []), timeframe, now_ms)
            for timeframe in TIMEFRAME_MS
        }
        price = _num(current_price)
        if price <= 0 and closed["1m"]:
            price = float(closed["1m"][-1]["c"])
        if price <= 0:
            return {"asset": self.asset, "price": None, "status": "WARMING UP", "levels": []}

        candidates = _detect_candidates(closed)
        merged = _merge_levels(candidates)
        valid = [
            level for level in merged
            if not _level_is_broken(
                float(level["price"]), float(level["width"]), str(level["side"]), closed, price
            )
        ]
        eligible = [
            level for level in valid
            if (
                level["side"] == "SUPPORT" and float(level["price"]) <= price + float(level["width"])
            ) or (
                level["side"] == "RESISTANCE" and float(level["price"]) >= price - float(level["width"])
            )
        ]
        supports = sorted(
            (level for level in eligible if level["side"] == "SUPPORT"),
            key=lambda level: (abs(price - float(level["price"])), -int(level["strength_score"])),
        )[:3]
        resistances = sorted(
            (level for level in eligible if level["side"] == "RESISTANCE"),
            key=lambda level: (abs(price - float(level["price"])), -int(level["strength_score"])),
        )[:3]

        selected: list[dict[str, Any]] = []
        for level in supports + resistances:
            status = _entry_status(level, closed, price, valid)
            selected.append({
                "side": level["side"],
                "price": round(float(level["price"]), 8),
                "distance": round(abs(price - float(level["price"])), 8),
                "distance_pct": round(abs(price - float(level["price"])) / price * 100.0, 4),
                "strength_score": int(level["strength_score"]),
                "status": status,
            })
        selected.sort(key=lambda level: (
            level["distance_pct"] - level["strength_score"] / 10_000.0,
            -level["strength_score"],
        ))
        for rank, level in enumerate(selected, start=1):
            level["rank"] = rank

        has_level_history = any(len(closed[tf]) >= 60 for tf in LEVEL_TIMEFRAMES)
        status = "READY" if selected else "NO TRADE" if has_level_history else "WARMING UP"
        return {
            "asset": self.asset,
            "price": round(price, 8),
            "status": status,
            "levels": selected,
        }
