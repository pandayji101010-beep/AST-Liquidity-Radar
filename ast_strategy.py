from __future__ import annotations

import copy
import math
from typing import Any


class ASTStructureStrategy:
    """Closed-candle major/minor structure engine with independent asset locks."""

    TIMEFRAMES = ("1d", "4h", "1h", "30m", "15m", "5m", "1m")
    SEQUENCE_BARS = 12

    def __init__(self) -> None:
        self._lock_side: str | None = None
        self._lock_reason = "Waiting for confirmed major structure"
        self._lock_timeframe: str | None = None
        self._last_lock_event_key: tuple[Any, ...] | None = None
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
        lock_side: str,
    ) -> dict[str, Any]:
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

        normalised_lock = lock_side if lock_side in ("BUY", "SELL") else "NEUTRAL"
        lock_ok = side is None or normalised_lock == "NEUTRAL" or side == normalised_lock
        blocked_by_lock = bool(structure_ok and not lock_ok)
        checks = {
            "liquidity_sweep": sweep_ok,
            "displacement": displacement_ok,
            "structure_break": structure_ok,
            "zone_confluence": zone_ok,
            "direction_lock": lock_ok if side is not None else None,
        }

        if blocked_by_lock:
            status = f"BLOCKED BY {normalised_lock} LOCK"
            reason = (
                f"{side} structure sequence is complete, but the current {normalised_lock} "
                "direction lock blocks the opposite entry. Wait for a displacement-backed "
                "major CHOCH before the lock can reverse."
            )
        elif structure_ok:
            status = "ENTRY CONFIRMED"
            reason = "Directional liquidity sweep -> displacement -> matching BOS/CHOCH confirmed on closed candles and aligned with the asset direction lock."
        elif displacement_ok:
            status = "SETUP FORMING"
            reason = "Sweep and directional displacement confirmed; waiting for matching BOS/CHOCH."
        elif sweep_ok:
            status = "LIQUIDITY SWEPT"
            reason = "Liquidity swept; waiting for directional displacement and structure confirmation."
        else:
            status = "WAITING"
            reason = "Waiting for a confirmed liquidity sweep."

        entry_side = side if structure_ok and not blocked_by_lock else None
        candidate_side = side if side is not None and entry_side is None else None
        score = sum(bool(checks[key]) for key in (
            "liquidity_sweep", "displacement", "structure_break", "zone_confluence"
        )) * 25
        return {
            "entry_timeframe": timeframe,
            "status": status,
            "side": entry_side,
            "candidate_side": candidate_side,
            "score": score,
            "trend_lock": normalised_lock,
            "major_trend": major_trend,
            "minor_trend": minor_trend,
            "price": current_price,
            "checks": checks,
            "sweep_level": sweep["level"] if sweep else None,
            "reason": reason,
        }
    def _update_direction_lock(
        self,
        timeframe_results: dict[str, dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        reversals = [
            item for item in events
            if item["structure"] == "MAJOR"
            and item["event"] == "CHOCH"
            and item.get("displacement")
        ]
        latest = max(reversals, key=lambda item: (item.get("time") or 0, item["timeframe"]), default=None)

        if self._lock_side is None:
            if latest:
                self._lock_side = latest["side"]
                self._lock_timeframe = latest["timeframe"]
                self._lock_reason = "Initialized from latest displacement-backed major CHOCH"
                self._last_lock_event_key = (latest["timeframe"], latest.get("time"), latest["side"])
            else:
                for tf in self.TIMEFRAMES:
                    trend = timeframe_results.get(tf, {}).get("major_trend")
                    if trend in ("BULLISH", "BEARISH"):
                        self._lock_side = "BUY" if trend == "BULLISH" else "SELL"
                        self._lock_timeframe = tf
                        self._lock_reason = f"Initial bias from confirmed {tf.upper()} major structure"
                        break

        if latest:
            key = (latest["timeframe"], latest.get("time"), latest["side"])
            if key != self._last_lock_event_key:
                if self._lock_side != latest["side"]:
                    self._lock_side = latest["side"]
                    self._lock_timeframe = latest["timeframe"]
                    self._lock_reason = "Direction changed only after displacement-backed major CHOCH"
                self._last_lock_event_key = key

        return {
            "side": self._lock_side or "NEUTRAL",
            "reason": self._lock_reason,
            "timeframe": self._lock_timeframe,
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
                    "setup": {"status": "WAITING", "side": None}, "sweeps": [],
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
                major["trend"], minor["trend"], self._lock_side or "NEUTRAL",
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
                "setup": {"status": setup["status"], "side": setup["side"] or setup["candidate_side"], "score": setup["score"]},
                "sweeps": [
                    {"type": item["type"], "side": item["side"], "level": item["level"], "time": item["time"]}
                    for item in sweeps[-5:]
                ],
            }
            internals[tf] = {
                "bars": bars, "atr": atr, "displacement": displacement,
                "sweeps": sweeps, "events": events, "major": major, "minor": minor,
            }

        lock = self._update_direction_lock(timeframe_results, all_events)
        setups = []
        for tf in self.TIMEFRAMES:
            data = internals[tf]
            if data is None:
                continue
            setup = self._setup_for_timeframe(
                tf, data["bars"], data["sweeps"], data["displacement"],
                data["events"], [z for z in all_zones if z["asset_timeframe"] == tf],
                data["major"]["trend"], data["minor"]["trend"], lock["side"],
            )
            setups.append(setup)
            timeframe_results[tf]["setup"] = {
                "status": setup["status"],
                "side": setup["side"] or setup["candidate_side"],
                "score": setup["score"],
            }

        result = {
            "direction_lock": lock,
            "timeframes": timeframe_results,
            "zones": sorted(all_zones, key=lambda item: item.get("created_time") or 0, reverse=True)[:20],
            "liquidity": all_liquidity[-80:],
            "events": sorted(all_events, key=lambda item: (item.get("time") or 0, item["timeframe"]), reverse=True)[:60],
            "setups": setups,
        }
        self._cache_signature = signature
        self._cache_result = copy.deepcopy(result)
        return result
