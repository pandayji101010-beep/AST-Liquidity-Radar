from __future__ import annotations

import copy
import math
from typing import Any


class ASTStructureStrategy:
    """Closed-candle major/minor structure engine with independent timeframe signals."""

    TIMEFRAMES = ("1d", "4h", "1h", "30m", "15m", "5m", "1m")
    SEQUENCE_BARS = 12
    MOMENTUM_LOOKBACK = 20
    MOMENTUM_RETEST_BARS = 12
    MOMENTUM_ACTIVE_BARS = 24
    MOMENTUM_ATR_MULTIPLE = 1.0
    MOMENTUM_BODY_RATIO = 0.50
    MOMENTUM_RETEST_ATR = 0.25

    def __init__(self) -> None:
        self._cache_signature: tuple[Any, ...] | None = None
        self._cache_result: dict[str, Any] | None = None

    @staticmethod
    def _number(candle: dict[str, Any], key: str) -> float | None:
        try:
            value = float(candle[key])
            return value if math.isfinite(value) else None
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def _clean_bars(cls, candles: Any) -> list[dict[str, Any]]:
        unique: dict[Any, dict[str, Any]] = {}
        for index, candle in enumerate(list(candles or [])):
            o = cls._number(candle, "o")
            h = cls._number(candle, "h")
            low = cls._number(candle, "l")
            close = cls._number(candle, "c")
            if None in (o, h, low, close) or h < low:
                continue
            stamp = candle.get("t")
            key = stamp if stamp is not None else ("index", index)
            unique[key] = {
                "t": stamp, "o": o, "h": h, "l": low, "c": close,
                "v": cls._number(candle, "v") or 0.0,
            }
        rows = list(unique.values())
        rows.sort(key=lambda row: row["t"] if isinstance(row["t"], (int, float)) else -1)
        return rows

    @staticmethod
    def _atr_values(bars: list[dict[str, Any]], period: int = 14) -> list[float]:
        true_ranges: list[float] = []
        for index, bar in enumerate(bars):
            previous_close = bars[index - 1]["c"] if index else bar["c"]
            true_ranges.append(max(
                bar["h"] - bar["l"],
                abs(bar["h"] - previous_close),
                abs(bar["l"] - previous_close),
                0.0,
            ))
        result: list[float] = []
        for index in range(len(bars)):
            sample = true_ranges[max(0, index - period):index]
            result.append(sum(sample) / len(sample) if sample else 0.0)
        return result

    @staticmethod
    def _displacement_flags(bars: list[dict[str, Any]], atr: list[float]) -> list[bool]:
        result = []
        for index, bar in enumerate(bars):
            average = atr[index]
            candle_range = bar["h"] - bar["l"]
            body = abs(bar["c"] - bar["o"])
            ratio = body / candle_range if candle_range > 0 else 0.0
            result.append(average > 0 and body >= average * 1.25 and ratio >= 0.55)
        return result

    @staticmethod
    def _find_sweeps(bars: list[dict[str, Any]], lookback: int = 20) -> list[dict[str, Any]]:
        sweeps = []
        for index in range(lookback, len(bars)):
            bar = bars[index]
            prior = bars[index - lookback:index]
            prior_high = max(item["h"] for item in prior)
            prior_low = min(item["l"] for item in prior)
            if bar["l"] < prior_low and bar["c"] > prior_low:
                sweeps.append({
                    "bar_index": index, "time": bar["t"], "type": "LOWER_SWEEP",
                    "side": "BUY", "level": prior_low,
                })
            elif bar["h"] > prior_high and bar["c"] < prior_high:
                sweeps.append({
                    "bar_index": index, "time": bar["t"], "type": "UPPER_SWEEP",
                    "side": "SELL", "level": prior_high,
                })
        return sweeps

    @staticmethod
    def _build_tier(
        bars: list[dict[str, Any]],
        displacement: list[bool],
        left: int,
        right: int,
        structure_name: str,
    ) -> dict[str, Any]:
        highs: list[dict[str, Any]] = []
        lows: list[dict[str, Any]] = []
        active_high = None
        active_low = None
        trend = "NEUTRAL"
        events = []

        for current in range(left + right, len(bars)):
            pivot_index = current - right
            pivot = bars[pivot_index]

            left_highs = [x["h"] for x in bars[pivot_index-left:pivot_index]]
            right_highs = [x["h"] for x in bars[pivot_index+1:pivot_index+right+1]]
            left_lows = [x["l"] for x in bars[pivot_index-left:pivot_index]]
            right_lows = [x["l"] for x in bars[pivot_index+1:pivot_index+right+1]]

            if left_highs and right_highs and pivot["h"] > max(left_highs) and pivot["h"] >= max(right_highs):
                active_high = {
                    "bar_index": pivot_index, "confirmed_at": current,
                    "time": pivot["t"], "price": pivot["h"], "broken": False,
                }
                highs.append(active_high)

            if left_lows and right_lows and pivot["l"] < min(left_lows) and pivot["l"] <= min(right_lows):
                active_low = {
                    "bar_index": pivot_index, "confirmed_at": current,
                    "time": pivot["t"], "price": pivot["l"], "broken": False,
                }
                lows.append(active_low)

            if trend == "NEUTRAL" and len(highs) >= 2 and len(lows) >= 2:
                hh = highs[-1]["price"] > highs[-2]["price"]
                hl = lows[-1]["price"] > lows[-2]["price"]
                lh = highs[-1]["price"] < highs[-2]["price"]
                ll = lows[-1]["price"] < lows[-2]["price"]
                if hh and hl:
                    trend = "BULLISH"
                elif lh and ll:
                    trend = "BEARISH"

            previous_close = bars[current - 1]["c"]
            close = bars[current]["c"]

            if (
                active_high is not None and not active_high["broken"]
                and current > active_high["confirmed_at"]
                and previous_close <= active_high["price"] < close
            ):
                event_type = "CHOCH" if trend == "BEARISH" else "BOS"
                events.append({
                    "structure": structure_name, "event": event_type, "side": "BUY",
                    "level": active_high["price"], "time": bars[current]["t"],
                    "bar_index": current, "displacement": displacement[current],
                })
                active_high["broken"] = True
                trend = "BULLISH"

            if (
                active_low is not None and not active_low["broken"]
                and current > active_low["confirmed_at"]
                and previous_close >= active_low["price"] > close
            ):
                event_type = "CHOCH" if trend == "BULLISH" else "BOS"
                events.append({
                    "structure": structure_name, "event": event_type, "side": "SELL",
                    "level": active_low["price"], "time": bars[current]["t"],
                    "bar_index": current, "displacement": displacement[current],
                })
                active_low["broken"] = True
                trend = "BEARISH"

        return {
            "trend": trend, "highs": highs, "lows": lows,
            "events": events[-30:], "latest_event": events[-1] if events else None,
        }

    @staticmethod
    def _liquidity_levels(timeframe: str, major: dict[str, Any], minor: dict[str, Any], atr_value: float) -> list[dict[str, Any]]:
        result = []
        for structure, tier in (("MAJOR", major), ("MINOR", minor)):
            highs, lows = tier["highs"], tier["lows"]
            if highs:
                result.append({
                    "timeframe": timeframe, "name": f"{structure} BSL · Swing High",
                    "price": highs[-1]["price"], "side": "BUY_SIDE",
                })
            if lows:
                result.append({
                    "timeframe": timeframe, "name": f"{structure} SSL · Swing Low",
                    "price": lows[-1]["price"], "side": "SELL_SIDE",
                })
            if len(highs) >= 2 and atr_value > 0:
                a, b = highs[-2]["price"], highs[-1]["price"]
                if abs(a - b) <= atr_value * 0.20:
                    result.append({
                        "timeframe": timeframe, "name": f"{structure} Equal Highs · BSL",
                        "price": (a + b) / 2, "side": "BUY_SIDE",
                    })
            if len(lows) >= 2 and atr_value > 0:
                a, b = lows[-2]["price"], lows[-1]["price"]
                if abs(a - b) <= atr_value * 0.20:
                    result.append({
                        "timeframe": timeframe, "name": f"{structure} Equal Lows · SSL",
                        "price": (a + b) / 2, "side": "SELL_SIDE",
                    })
        return result[-12:]

    @staticmethod
    def _build_zones(
        timeframe: str,
        bars: list[dict[str, Any]],
        events: list[dict[str, Any]],
        atr: list[float],
        displacements: list[bool],
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        zones = []
        seen = set()
        for event in sorted(events, key=lambda item: item["bar_index"]):
            event_index = int(event["bar_index"])
            side = event["side"]

            # The origin belongs to the impulse preceding the structure break.
            impulse_index = event_index if event.get("displacement") else None
            if impulse_index is None:
                for index in range(event_index - 1, max(-1, event_index - 9), -1):
                    aligned = bars[index]["c"] > bars[index]["o"] if side == "BUY" else bars[index]["c"] < bars[index]["o"]
                    if displacements[index] and aligned:
                        impulse_index = index
                        break
            if impulse_index is None:
                continue

            origin_index = None
            for index in range(impulse_index - 1, max(-1, impulse_index - 9), -1):
                bar = bars[index]
                opposite = bar["c"] < bar["o"] if side == "BUY" else bar["c"] > bar["o"]
                if opposite:
                    origin_index = index
                    break
            if origin_index is None:
                continue

            origin = bars[origin_index]
            low, high = (origin["l"], origin["o"]) if side == "BUY" else (origin["o"], origin["h"])
            if high <= low:
                continue
            key = (side, origin["t"], round(low, 10), round(high, 10))
            if key in seen:
                continue
            seen.add(key)

            retests = 0
            was_inside = False
            invalidated = False
            for later in bars[event_index + 1:]:
                intersects = later["l"] <= high and later["h"] >= low
                if intersects and not was_inside:
                    retests += 1
                was_inside = intersects
                if (side == "BUY" and later["c"] < low) or (side == "SELL" and later["c"] > high):
                    invalidated = True
                    break

            status = "INVALIDATED" if invalidated else "WEAKENED" if retests >= 3 else "ACTIVE"
            stage = (
                "INVALIDATED" if invalidated else
                "3RD+ RETEST" if retests >= 3 else
                f"{retests} RETEST" if retests else "FRESH"
            )
            average = atr[impulse_index] if impulse_index < len(atr) else 0.0
            body = abs(bars[impulse_index]["c"] - bars[impulse_index]["o"])
            zones.append({
                "side": side, "asset_timeframe": timeframe,
                "low": low, "high": high, "origin_time": origin["t"],
                "created_time": event.get("time"), "retests": retests,
                "retest_stage": stage, "status": status,
                "strength_atr": round(body / average, 2) if average > 0 else 0.0,
            })
        return zones[-limit:]

    @staticmethod
    def _setup_for_timeframe(
        timeframe: str,
        bars: list[dict[str, Any]],
        sweeps: list[dict[str, Any]],
        displacements: list[bool],
        structure_events: list[dict[str, Any]],
        zones: list[dict[str, Any]],
        major_trend: str,
        minor_trend: str,
    ) -> dict[str, Any]:
        """Evaluate one timeframe in isolation; no other timeframe can block it."""
        current_price = bars[-1]["c"] if bars else None
        start = max(0, len(bars) - ASTStructureStrategy.SEQUENCE_BARS)
        candidates = [item for item in sweeps if item["bar_index"] >= start]
        sweep = candidates[-1] if candidates else None
        side = sweep["side"] if sweep else None
        displacement_index = None
        matching_event = None

        if sweep:
            sweep_index = int(sweep["bar_index"])
            for index in range(sweep_index + 1, min(len(bars), sweep_index + 9)):
                aligned = bars[index]["c"] > bars[index]["o"] if side == "BUY" else bars[index]["c"] < bars[index]["o"]
                if displacements[index] and aligned:
                    displacement_index = index
                    break
            if displacement_index is not None:
                candidates_after = [
                    event for event in structure_events
                    if event["side"] == side
                    and displacement_index < event["bar_index"] <= displacement_index + 8
                ]
                if candidates_after:
                    matching_event = candidates_after[-1]

        sweep_ok = sweep is not None
        displacement_ok = displacement_index is not None
        structure_ok = matching_event is not None
        zone_ok = False
        if side is not None and current_price is not None:
            zone_ok = any(
                zone["side"] == side and zone["status"] != "INVALIDATED"
                and zone["low"] <= current_price <= zone["high"]
                for zone in zones
            )

        # Show this chart's own direction. A fresh sweep takes priority, then
        # the latest confirmed local structure event, then minor/major trend.
        latest_event = max(
            structure_events,
            key=lambda event: event.get("time") or 0,
            default=None,
        )
        trend = minor_trend if minor_trend not in (None, "NEUTRAL") else major_trend
        trend_side = "BUY" if trend == "BULLISH" else "SELL" if trend == "BEARISH" else None
        direction = side or (latest_event.get("side") if latest_event else None) or trend_side

        checks = {
            "liquidity_sweep": sweep_ok,
            "displacement": displacement_ok,
            "structure_break": structure_ok,
            "zone_confluence": zone_ok,
        }

        if structure_ok:
            status = "ENTRY CONFIRMED"
            reason = "This timeframe confirmed its own liquidity sweep -> directional displacement -> matching BOS/CHOCH on closed candles."
            if zone_ok:
                reason += " Price is also inside an active origin zone (extra confluence)."
        elif displacement_ok:
            status = "SETUP FORMING"
            reason = "This timeframe has its own sweep and directional displacement; waiting for its matching BOS/CHOCH."
        elif sweep_ok:
            status = "LIQUIDITY SWEPT"
            reason = "This timeframe swept liquidity; waiting for its own directional displacement and structure confirmation."
        else:
            status = "WAITING"
            reason = "No complete local sweep sequence yet. Direction follows this timeframe's own confirmed structure."

        entry_side = side if structure_ok else None
        candidate_side = side if side is not None and not structure_ok else None
        score = sum(bool(checks[key]) for key in (
            "liquidity_sweep", "displacement", "structure_break", "zone_confluence"
        )) * 25
        return {
            "entry_timeframe": timeframe,
            "status": status,
            "side": entry_side,
            "candidate_side": candidate_side,
            "direction": direction,
            "score": score,
            "major_trend": major_trend,
            "minor_trend": minor_trend,
            "price": current_price,
            "checks": checks,
            "sweep_level": sweep["level"] if sweep else None,
            "reason": reason,
        }
    @staticmethod
    def _momentum_continuation_for_timeframe(
        timeframe: str,
        bars: list[dict[str, Any]],
        atr: list[float],
        major_trend: str,
        minor_trend: str,
    ) -> dict[str, Any]:
        """Detect a strong 20-bar range break and a later retest rejection.

        This engine is independent of liquidity sweeps. A range breakout is
        only an alert; it becomes an entry candidate after price retests the
        broken boundary and closes back in the breakout direction.
        """
        current_price = bars[-1]["c"] if bars else None
        trend = minor_trend if minor_trend not in (None, "NEUTRAL") else major_trend
        trend_side = "BUY" if trend == "BULLISH" else "SELL" if trend == "BEARISH" else None

        empty = {
            "engine": "MOMENTUM CONTINUATION",
            "entry_timeframe": timeframe,
            "status": "WAITING",
            "side": None,
            "candidate_side": None,
            "direction": trend_side,
            "score": 0,
            "major_trend": major_trend,
            "minor_trend": minor_trend,
            "price": current_price,
            "breakout_level": None,
            "breakout_time": None,
            "retest_zone_low": None,
            "retest_zone_high": None,
            "retest_age_bars": None,
            "checks": {
                "range_breakout": False,
                "momentum_displacement": False,
                "retest_touch": False,
                "retest_rejection": False,
            },
            "reason": "Waiting for a strong close beyond the prior 20-candle range. A liquidity sweep is not required for this engine.",
        }
        lookback = ASTStructureStrategy.MOMENTUM_LOOKBACK
        if len(bars) <= lookback or len(atr) != len(bars):
            empty["reason"] = f"Need more than {lookback} candles to evaluate a momentum range break."
            return empty

        breakouts: list[dict[str, Any]] = []
        first_index = max(lookback, len(bars) - ASTStructureStrategy.MOMENTUM_ACTIVE_BARS)
        for index in range(first_index, len(bars)):
            candle = bars[index]
            average = atr[index]
            candle_range = candle["h"] - candle["l"]
            body = abs(candle["c"] - candle["o"])
            body_ratio = body / candle_range if candle_range > 0 else 0.0
            if average <= 0 or body < average * ASTStructureStrategy.MOMENTUM_ATR_MULTIPLE:
                continue
            if body_ratio < ASTStructureStrategy.MOMENTUM_BODY_RATIO:
                continue

            previous = bars[index - lookback:index]
            range_high = max(item["h"] for item in previous)
            range_low = min(item["l"] for item in previous)

            if candle["c"] > range_high and candle["c"] > candle["o"]:
                breakouts.append({
                    "bar_index": index, "side": "BUY", "level": range_high,
                    "time": candle.get("t"), "atr": average,
                })
            elif candle["c"] < range_low and candle["c"] < candle["o"]:
                breakouts.append({
                    "bar_index": index, "side": "SELL", "level": range_low,
                    "time": candle.get("t"), "atr": average,
                })

        if not breakouts:
            return empty

        breakout = breakouts[-1]
        breakout_index = int(breakout["bar_index"])
        side = str(breakout["side"])
        level = float(breakout["level"])
        breakout_atr = max(float(breakout["atr"]), 1e-12)
        tolerance = breakout_atr * ASTStructureStrategy.MOMENTUM_RETEST_ATR
        age = len(bars) - 1 - breakout_index

        if age > ASTStructureStrategy.MOMENTUM_RETEST_BARS:
            result = dict(empty)
            result["reason"] = "The latest range breakout is older than the retest window; waiting for a fresh continuation setup."
            return result

        checks = {
            "range_breakout": True,
            "momentum_displacement": True,
            "retest_touch": False,
            "retest_rejection": False,
        }
        invalidated = False

        for index in range(breakout_index + 1, len(bars)):
            candle = bars[index]
            if side == "BUY":
                if candle["c"] < level - breakout_atr * 0.50:
                    invalidated = True
                    break
                touched = candle["l"] <= level + tolerance and candle["h"] >= level - tolerance
                rejected = touched and candle["c"] >= level and candle["c"] > candle["o"]
            else:
                if candle["c"] > level + breakout_atr * 0.50:
                    invalidated = True
                    break
                touched = candle["h"] >= level - tolerance and candle["l"] <= level + tolerance
                rejected = touched and candle["c"] <= level and candle["c"] < candle["o"]

            if touched:
                checks["retest_touch"] = True
                if rejected:
                    checks["retest_rejection"] = True
                break

        score = round(sum(checks.values()) / len(checks) * 100)
        common = {
            "engine": "MOMENTUM CONTINUATION",
            "entry_timeframe": timeframe,
            "side": side if checks["retest_rejection"] else None,
            "candidate_side": side if not checks["retest_rejection"] and not invalidated else None,
            "direction": side,
            "score": score,
            "major_trend": major_trend,
            "minor_trend": minor_trend,
            "price": current_price,
            "breakout_level": level,
            "breakout_time": breakout.get("time"),
            "retest_zone_low": level - tolerance,
            "retest_zone_high": level + tolerance,
            "retest_age_bars": age,
            "checks": checks,
        }

        if invalidated:
            return {
                **common,
                "side": None,
                "candidate_side": None,
                "status": "INVALIDATED",
                "reason": "Price closed decisively back through the broken range boundary; this continuation candidate is invalid.",
            }
        if checks["retest_rejection"]:
            return {
                **common,
                "status": "ENTRY CONFIRMED",
                "reason": "A strong 20-candle range breakout was followed by a retest of the broken boundary and a directional rejection close. No liquidity sweep was required.",
            }
        if checks["retest_touch"]:
            return {
                **common,
                "status": "RETEST TESTING",
                "reason": "The breakout boundary has been retested, but a directional rejection close is not confirmed yet.",
            }
        status = "BREAKOUT WATCH" if age == 0 else "RETEST PENDING"
        return {
            **common,
            "status": status,
            "reason": "A strong close broke the prior 20-candle range. Wait for price to retest the broken boundary; do not chase the impulse candle.",
        }

    def snapshot(self, candles: dict[str, Any]) -> dict[str, Any]:
        cleaned = {tf: self._clean_bars(candles.get(tf, [])) for tf in self.TIMEFRAMES}
        signature = tuple(
            (tf, len(rows), rows[-1].get("t"), rows[-1].get("c")) if rows else (tf, 0, None, None)
            for tf, rows in cleaned.items()
        )
        if signature == self._cache_signature and self._cache_result is not None:
            return copy.deepcopy(self._cache_result)

        timeframe_results = {}
        internals = {}
        all_events = []
        all_zones = []
        all_liquidity = []

        for tf in self.TIMEFRAMES:
            bars = cleaned[tf]
            if len(bars) < 30:
                timeframe_results[tf] = {
                    "ready": False, "status": "WARMING UP", "bars": len(bars),
                    "message": "Need at least 30 valid closed candles",
                    "major_trend": "NEUTRAL", "minor_trend": "NEUTRAL",
                    "major_event": None, "minor_event": None,
                    "setup": {"status": "WAITING", "side": None},
                    "momentum_setup": {"status": "WAITING", "side": None, "candidate_side": None, "direction": None, "score": 0, "reason": "Need at least 30 valid candles."},
                    "sweeps": [],
                }
                internals[tf] = None
                continue

            atr = self._atr_values(bars)
            displacement = self._displacement_flags(bars, atr)
            sweeps = self._find_sweeps(bars)
            major = self._build_tier(bars, displacement, 5, 5, "MAJOR")
            minor = self._build_tier(bars, displacement, 2, 2, "MINOR")

            events = []
            for item in major["events"] + minor["events"]:
                enriched = dict(item)
                enriched["timeframe"] = tf
                events.append(enriched)
            all_events.extend(events)

            liquidity = self._liquidity_levels(tf, major, minor, atr[-1] if atr else 0.0)
            all_liquidity.extend(liquidity)
            zones = self._build_zones(tf, bars, events, atr, displacement)
            all_zones.extend(zones)

            setup = self._setup_for_timeframe(
                tf, bars, sweeps, displacement, events, zones,
                major["trend"], minor["trend"],
            )
            momentum_setup = self._momentum_continuation_for_timeframe(
                tf, bars, atr, major["trend"], minor["trend"],
            )

            def ui_event(event):
                if not event:
                    return None
                return {
                    "event": event["event"], "side": event["side"],
                    "level": event["level"], "time": event.get("time"),
                }

            timeframe_results[tf] = {
                "ready": True, "status": "READY", "bars": len(bars), "message": None,
                "major_trend": major["trend"], "minor_trend": minor["trend"],
                "major_event": ui_event(major["latest_event"]),
                "minor_event": ui_event(minor["latest_event"]),
                "setup": {"status": setup["status"], "side": setup["side"] or setup["candidate_side"], "direction": setup["direction"], "score": setup["score"]},
                "momentum_setup": {
                    "status": momentum_setup["status"],
                    "side": momentum_setup["side"] or momentum_setup["candidate_side"],
                    "direction": momentum_setup["direction"],
                    "score": momentum_setup["score"],
                    "breakout_level": momentum_setup["breakout_level"],
                },
                "sweeps": [
                    {"type": item["type"], "side": item["side"], "level": item["level"], "time": item["time"]}
                    for item in sweeps[-5:]
                ],
            }
            internals[tf] = {
                "bars": bars, "atr": atr, "displacement": displacement,
                "sweeps": sweeps, "events": events, "major": major, "minor": minor,
                "zones": zones, "setup": setup, "momentum_setup": momentum_setup,
            }

        # Each timeframe keeps the setup calculated from its own candles.
        # No asset-wide direction lock or cross-timeframe voting is applied.
        setups = []
        momentum_setups = []
        for tf in self.TIMEFRAMES:
            data = internals[tf]
            if data is None:
                continue
            setup = data["setup"]
            momentum_setup = data["momentum_setup"]
            setups.append(setup)
            momentum_setups.append(momentum_setup)
            timeframe_results[tf]["setup"] = {
                "status": setup["status"],
                "side": setup["side"] or setup["candidate_side"],
                "direction": setup["direction"],
                "score": setup["score"],
            }
            timeframe_results[tf]["momentum_setup"] = {
                "status": momentum_setup["status"],
                "side": momentum_setup["side"] or momentum_setup["candidate_side"],
                "direction": momentum_setup["direction"],
                "score": momentum_setup["score"],
                "breakout_level": momentum_setup["breakout_level"],
            }
        result = {
            "timeframes": timeframe_results,
            "zones": sorted(all_zones, key=lambda item: item.get("created_time") or 0, reverse=True)[:20],
            "liquidity": all_liquidity[-80:],
            "events": sorted(all_events, key=lambda item: (item.get("time") or 0, item["timeframe"]), reverse=True)[:60],
            "setups": setups,
            "momentum_setups": momentum_setups,
        }
        self._cache_signature = signature
        self._cache_result = copy.deepcopy(result)
        return result
