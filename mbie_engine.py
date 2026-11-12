from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from institutional_campaign_strategy import ASTInstitutionalCampaignStrategy as Kernel


class ASTMarketBehaviourIntelligenceEngine:
    """MBIE: market understanding only. It never creates or approves orders."""

    SCALP_MIN_DISTANCE_PIPS = 90.0
    SCALP_MAX_DISTANCE_PIPS: float | None = None
    SCALP_PIVOT_LOOKBACK = 48

    SCORE_WEIGHTS = {
        "market_regime": 10, "htf_structure": 15, "liquidity": 15,
        "auction_profile": 10, "order_flow": 15, "supply_demand": 10,
        "momentum_volatility": 10, "price_action": 5, "session": 5,
        "fundamental_risk": 5,
    }

    def __init__(self) -> None:
        # Independent active pools for every candle timeframe. The orchestrator
        # resets and rebuilds these in timestamp order on each point-in-time replay.
        self._liquidity_trackers: dict[str, dict[str, Any]] = {}

    def reset_liquidity_trackers(self) -> None:
        self._liquidity_trackers = {}

    @staticmethod
    def _liquidity_candidates(base: dict[str, Any], timeframe: str, close: float) -> dict[str, list[dict[str, Any]]]:
        specs = (
            ("BUY_SIDE", "equal_high", "equal_high_time", "EQUAL HIGHS"),
            ("BUY_SIDE", "swing_high", "pivot_high_time", "SWING HIGH"),
            ("BUY_SIDE", "prior_high", "prior_high_time", "PREVIOUS RANGE HIGH"),
            ("SELL_SIDE", "equal_low", "equal_low_time", "EQUAL LOWS"),
            ("SELL_SIDE", "swing_low", "pivot_low_time", "SWING LOW"),
            ("SELL_SIDE", "prior_low", "prior_low_time", "PREVIOUS RANGE LOW"),
        )
        found: dict[str, dict[str, dict[str, Any]]] = {"BUY_SIDE": {}, "SELL_SIDE": {}}
        for side, price_key, time_key, label in specs:
            raw_price = base.get(price_key)
            raw_time = base.get(time_key)
            if not isinstance(raw_price, (int, float)) or not math.isfinite(float(raw_price)):
                continue
            price = float(raw_price)
            if side == "BUY_SIDE" and price <= close:
                continue
            if side == "SELL_SIDE" and price >= close:
                continue
            source_time = int(raw_time if raw_time is not None else base.get("candle_time") or 0)
            if source_time <= 0:
                continue
            level_id = f"{timeframe}:{side}:{source_time}:{price:.10f}"
            found[side].setdefault(level_id, {
                "id": level_id,
                "price": price,
                "label": label,
                "side": side,
                "timeframe": timeframe,
                "source_time": source_time,
                "formed_at": source_time,
                "status": "ACTIVE",
            })
        return {
            "BUY_SIDE": sorted(found["BUY_SIDE"].values(), key=lambda x: x["price"]),
            "SELL_SIDE": sorted(found["SELL_SIDE"].values(), key=lambda x: x["price"], reverse=True),
        }

    @staticmethod
    def _pip_size_for_asset(asset: str | None) -> float:
        """Dashboard pip convention: BTC = $1; GOLD/XAUUSD = $0.01."""
        return 0.01 if str(asset or "BTC").upper() in ("GOLD", "XAUUSD", "GC=F") else 1.0

    @staticmethod
    def _scalping_liquidity_candidates(
        bars: list[dict[str, Any]],
        timeframe: str,
        base: dict[str, Any],
        close: float,
        pip_size: float,
        min_pips: float = 90.0,
        max_pips: float | None = None,
        excluded_prices: dict[str, list[float]] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Find confirmed local highs/lows inside the scalp-distance band."""
        found: dict[str, list[dict[str, Any]]] = {"BUY_SIDE": [], "SELL_SIDE": []}
        if len(bars) < 5 or not math.isfinite(pip_size) or pip_size <= 0 or close <= 0:
            return found

        rows = bars[-64:]
        pivots: dict[str, list[tuple[float, int]]] = {"BUY_SIDE": [], "SELL_SIDE": []}
        start = max(1, len(rows) - ASTMarketBehaviourIntelligenceEngine.SCALP_PIVOT_LOOKBACK)
        for i in range(start, len(rows) - 1):
            high = float(rows[i]["h"])
            low = float(rows[i]["l"])
            if high > float(rows[i - 1]["h"]) and high >= float(rows[i + 1]["h"]):
                pivots["BUY_SIDE"].append((high, int(rows[i]["t"])))
            if low < float(rows[i - 1]["l"]) and low <= float(rows[i + 1]["l"]):
                pivots["SELL_SIDE"].append((low, int(rows[i]["t"])))

        atr = float(base.get("atr") or 0.0)
        equal_tolerance = max(atr * 0.10, pip_size * 2.0, 1e-8)

        def add(side: str, price: float, source_time: int, label: str) -> None:
            if not math.isfinite(price) or source_time <= 0:
                return
            if side == "BUY_SIDE" and price <= close:
                return
            if side == "SELL_SIDE" and price >= close:
                return
            distance_points = price - close if side == "BUY_SIDE" else close - price
            distance_pips = distance_points / pip_size
            if distance_pips < min_pips or (max_pips is not None and distance_pips > max_pips):
                return
            # Never reuse the same structural price already displayed as swing liquidity.
            exclusion_tolerance = max(pip_size * 2.0, abs(price) * 1e-7, 1e-8)
            if any(
                isinstance(level, (int, float))
                and abs(float(level) - price) <= exclusion_tolerance
                for level in (excluded_prices or {}).get(side, [])
            ):
                return
            if any(abs(float(item["price"]) - price) <= pip_size * 0.25 for item in found[side]):
                return
            found[side].append({
                "id": f"{timeframe}:SCALP:{side}:{source_time}:{price:.10f}",
                "price": price,
                "label": label,
                "side": side,
                "timeframe": timeframe,
                "source_time": source_time,
                "formed_at": source_time,
                "status": "ACTIVE",
                "distance_points": round(distance_points, 10),
                "distance_pips": round(distance_pips, 1),
            })

        for side, label in (("BUY_SIDE", "MICRO EQUAL HIGH"), ("SELL_SIDE", "MICRO EQUAL LOW")):
            side_pivots = pivots[side]
            if len(side_pivots) >= 2:
                first, second = side_pivots[-2], side_pivots[-1]
                if abs(first[0] - second[0]) <= equal_tolerance:
                    add(side, (first[0] + second[0]) / 2.0, max(first[1], second[1]), label)

        for price, source_time in reversed(pivots["BUY_SIDE"]):
            add("BUY_SIDE", price, source_time, "MICRO SWING HIGH")
        for price, source_time in reversed(pivots["SELL_SIDE"]):
            add("SELL_SIDE", price, source_time, "MICRO SWING LOW")

        prior_window = rows[-9:-1]
        if prior_window:
            high_bar = max(prior_window, key=lambda item: float(item["h"]))
            low_bar = min(prior_window, key=lambda item: float(item["l"]))
            add("BUY_SIDE", float(high_bar["h"]), int(high_bar["t"]), "RECENT MICRO RANGE HIGH")
            add("SELL_SIDE", float(low_bar["l"]), int(low_bar["t"]), "RECENT MICRO RANGE LOW")

        found["BUY_SIDE"].sort(key=lambda item: item["price"])
        found["SELL_SIDE"].sort(key=lambda item: item["price"], reverse=True)
        return found

    @staticmethod
    def _candidate_was_consumed(state: dict[str, Any], side: str, candidate: dict[str, Any], base: dict[str, Any]) -> bool:
        price = float(candidate["price"])
        tolerance = max(float(base.get("atr") or 0.0) * 0.05, abs(price) * 0.00001, 1e-8)
        for cleared in state["consumed_levels"].get(side, []):
            # A same-price pool is new only when its source pivot/range candle formed
            # after the prior pool was actually swept or broken.
            if int(candidate["source_time"]) <= int(cleared["time"]) and abs(price - float(cleared["price"])) <= tolerance:
                return True
        return False

    @staticmethod
    def _liquidity_cross(level: dict[str, Any], candle: dict[str, Any]) -> str | None:
        price = float(level["price"])
        side = level["side"]
        high, low, close = float(candle["h"]), float(candle["l"]), float(candle["c"])
        if side == "BUY_SIDE" and high >= price:
            return "BROKEN" if close >= price else "SWEPT"
        if side == "SELL_SIDE" and low <= price:
            return "BROKEN" if close <= price else "SWEPT"
        return None

    def _liquidity_snapshot(self, state: dict[str, Any], timeframe: str, close: float) -> dict[str, Any]:
        def decorate(level: dict[str, Any] | None, side: str) -> dict[str, Any] | None:
            if not level:
                return None
            result = dict(level)
            distance = float(result["price"]) - close if side == "BUY_SIDE" else close - float(result["price"])
            result["distance_points"] = round(max(0.0, distance), 10)
            result["distance_pct"] = round(max(0.0, distance) / close * 100.0, 5) if close else None
            return result
        buy = decorate(state["active"].get("BUY_SIDE"), "BUY_SIDE")
        sell = decorate(state["active"].get("SELL_SIDE"), "SELL_SIDE")
        return {
            "timeframe": timeframe,
            "status": "TRACKING" if buy or sell else "WAITING FOR UNTOUCHED POOL",
            "buy_side": buy,
            "sell_side": sell,
            "last_cleared": {
                "buy_side": dict(state["last_cleared"].get("BUY_SIDE")) if state["last_cleared"].get("BUY_SIDE") else None,
                "sell_side": dict(state["last_cleared"].get("SELL_SIDE")) if state["last_cleared"].get("SELL_SIDE") else None,
            },
            "rule": "A level stays fixed until a candle of this same timeframe trades through it. Wick rejection = SWEPT; close through = BROKEN. The next untouched pool is selected from the following timeframe candle.",
        }

    def _update_persistent_liquidity(
        self,
        bars: list[dict[str, Any]],
        timeframe: str,
        base: dict[str, Any],
        candidate_builder: Any = None,
        state_key: str | None = None,
        max_active_distance_pips: float | None = None,
        pip_size: float | None = None,
    ) -> dict[str, Any]:
        if not bars:
            return {
                "timeframe": timeframe, "status": "WAITING FOR DATA",
                "buy_side": None, "sell_side": None,
                "last_cleared": {"buy_side": None, "sell_side": None},
            }

        tracker_key = state_key or timeframe
        state = self._liquidity_trackers.setdefault(tracker_key, {
            "last_bar_time": None,
            "active": {"BUY_SIDE": None, "SELL_SIDE": None},
            "consumed_ids": set(),
            "consumed_levels": {"BUY_SIDE": [], "SELL_SIDE": []},
            "last_cleared": {"BUY_SIDE": None, "SELL_SIDE": None},
        })

        def candidates_for(frame_bars, frame_base, frame_close):
            if candidate_builder is not None:
                return candidate_builder(frame_bars, frame_base, frame_close)
            return self._liquidity_candidates(frame_base, timeframe, frame_close)

        last_time = int(bars[-1]["t"])
        close = float(bars[-1]["c"])

        if state["last_bar_time"] is None:
            candidates = candidates_for(bars, base, close)
            for side in ("BUY_SIDE", "SELL_SIDE"):
                for candidate in candidates[side]:
                    crossed = None
                    for historical_bar in bars:
                        if int(historical_bar["t"]) <= int(candidate["source_time"]):
                            continue
                        crossed = self._liquidity_cross(candidate, historical_bar)
                        if crossed:
                            break
                    if crossed:
                        state["consumed_ids"].add(candidate["id"])
                        clear_time = int(historical_bar["t"])
                        state["consumed_levels"][side].append({"price": candidate["price"], "time": clear_time})
                        state["last_cleared"][side] = {
                            "price": candidate["price"], "label": candidate["label"],
                            "event": crossed, "time": clear_time, "timeframe": timeframe,
                        }
                        continue
                    state["active"][side] = candidate
                    break
            state["last_bar_time"] = last_time
            return self._liquidity_snapshot(state, timeframe, close)

        prior_time = int(state["last_bar_time"])
        unseen_indices = [i for i, candle in enumerate(bars) if int(candle["t"]) > prior_time]
        for new_index in unseen_indices:
            candle = bars[new_index]
            candle_time = int(candle["t"])
            frame_bars = bars[:new_index + 1]
            frame_base = base if new_index == len(bars) - 1 else Kernel._frame_snapshot(frame_bars, timeframe)
            consumed_now: set[str] = set()

            for side in ("BUY_SIDE", "SELL_SIDE"):
                active = state["active"].get(side)
                if active:
                    event = self._liquidity_cross(active, candle)
                    if event:
                        state["consumed_ids"].add(active["id"])
                        state["consumed_levels"][side].append({"price": active["price"], "time": candle_time})
                        state["last_cleared"][side] = {
                            "price": active["price"], "label": active["label"],
                            "event": event, "time": candle_time, "timeframe": timeframe,
                        }
                        state["active"][side] = None
                        consumed_now.add(side)
                        continue

                    # Scalping pools are only useful while they remain within the
                    # configured maximum distance. Unlike swing pools, stale scalp
                    # pools expire instead of drifting thousands of pips away.
                    if (
                        candidate_builder is not None
                        and max_active_distance_pips is not None
                        and isinstance(pip_size, (int, float))
                        and float(pip_size) > 0
                    ):
                        frame_close_now = float(candle["c"])
                        gap_points = (
                            float(active["price"]) - frame_close_now
                            if side == "BUY_SIDE"
                            else frame_close_now - float(active["price"])
                        )
                        gap_pips = gap_points / float(pip_size)
                        if gap_pips > float(max_active_distance_pips):
                            state["last_cleared"][side] = {
                                "price": active["price"], "label": active["label"],
                                "event": "EXPIRED_OUTSIDE_SCALP_WINDOW",
                                "distance_pips": round(gap_pips, 1),
                                "time": candle_time, "timeframe": timeframe,
                            }
                            state["active"][side] = None
                            consumed_now.add(side)

            frame_close = float(candle["c"])
            candidates = candidates_for(frame_bars, frame_base, frame_close)
            for side in ("BUY_SIDE", "SELL_SIDE"):
                if state["active"].get(side) is not None or side in consumed_now:
                    continue
                for candidate in candidates[side]:
                    if candidate["id"] in state["consumed_ids"] or self._candidate_was_consumed(state, side, candidate, frame_base):
                        continue
                    event = self._liquidity_cross(candidate, candle)
                    if event:
                        state["consumed_ids"].add(candidate["id"])
                        state["consumed_levels"][side].append({"price": candidate["price"], "time": candle_time})
                        state["last_cleared"][side] = {
                            "price": candidate["price"], "label": candidate["label"],
                            "event": event, "time": candle_time, "timeframe": timeframe,
                        }
                        continue
                    state["active"][side] = candidate
                    break
            state["last_bar_time"] = candle_time

        return self._liquidity_snapshot(state, timeframe, close)

    @staticmethod
    def _rsi(closes: list[float], period: int = 14) -> float | None:
        if len(closes) <= period:
            return None
        gains, losses = 0.0, 0.0
        for i in range(len(closes) - period, len(closes)):
            change = closes[i] - closes[i - 1]
            gains += max(change, 0.0)
            losses += max(-change, 0.0)
        avg_gain, avg_loss = gains / period, losses / period
        if avg_loss <= 0:
            return 100.0 if avg_gain > 0 else 50.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    @staticmethod
    def _adx(bars: list[dict[str, Any]], period: int = 14) -> float | None:
        """Average directional index from rolling OHLC directional-movement windows."""
        if len(bars) < period * 2 + 1:
            return None
        dx_values = []
        first_end = max(period + 1, len(bars) - period)
        for end in range(first_end, len(bars) + 1):
            window = bars[end - period - 1:end]
            tr_values, plus_values, minus_values = [], [], []
            for i in range(1, len(window)):
                current, previous = window[i], window[i - 1]
                up = float(current["h"]) - float(previous["h"])
                down = float(previous["l"]) - float(current["l"])
                plus_values.append(up if up > down and up > 0 else 0.0)
                minus_values.append(down if down > up and down > 0 else 0.0)
                tr_values.append(max(
                    float(current["h"]) - float(current["l"]),
                    abs(float(current["h"]) - float(previous["c"])),
                    abs(float(current["l"]) - float(previous["c"])),
                ))
            total_tr = sum(tr_values)
            if total_tr <= 0:
                dx_values.append(0.0)
                continue
            plus_di = sum(plus_values) / total_tr * 100.0
            minus_di = sum(minus_values) / total_tr * 100.0
            denominator = plus_di + minus_di
            dx_values.append(abs(plus_di - minus_di) / denominator * 100.0 if denominator > 0 else 0.0)
        return round(sum(dx_values) / len(dx_values), 2) if dx_values else None

    @staticmethod
    def _volume_profile(bars: list[dict[str, Any]], bins: int = 24) -> dict[str, Any]:
        """OHLCV typical-price approximation; explicitly not exchange trade-level profile."""
        rows = bars[-120:]
        if len(rows) < 10:
            return {"status": "UNAVAILABLE", "basis": "OHLCV typical-price proxy"}
        low = min(float(x["l"]) for x in rows)
        high = max(float(x["h"]) for x in rows)
        if high <= low:
            return {"status": "UNAVAILABLE", "basis": "OHLCV typical-price proxy"}
        width = (high - low) / bins
        histogram = [0.0] * bins
        for row in rows:
            typical = (float(row["h"]) + float(row["l"]) + float(row["c"])) / 3.0
            idx = min(bins - 1, max(0, int((typical - low) / width)))
            histogram[idx] += max(0.0, float(row.get("v") or 0.0))
        total = sum(histogram)
        if total <= 0:
            return {"status": "UNAVAILABLE", "basis": "OHLCV typical-price proxy"}
        poc_idx = max(range(bins), key=lambda i: histogram[i])
        selected = {poc_idx}
        accumulated = histogram[poc_idx]
        left, right = poc_idx - 1, poc_idx + 1
        while accumulated < total * 0.70 and (left >= 0 or right < bins):
            left_vol = histogram[left] if left >= 0 else -1.0
            right_vol = histogram[right] if right < bins else -1.0
            if right_vol > left_vol:
                selected.add(right)
                accumulated += histogram[right]
                right += 1
            elif left >= 0:
                selected.add(left)
                accumulated += histogram[left]
                left -= 1
            else:
                break
        return {
            "status": "PROXY",
            "basis": "OHLCV typical-price x candle-volume approximation; not bid/ask footprint",
            "poc": low + (poc_idx + 0.5) * width,
            "vah": low + (max(selected) + 1) * width,
            "val": low + min(selected) * width,
            "bins": bins,
            "sample_bars": len(rows),
        }

    @staticmethod
    def _session(timestamp_ms: int) -> dict[str, Any]:
        hour = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).hour
        # UTC windows are context hints only; DST-adjusted exchange calendars are not connected.
        active = []
        if 0 <= hour < 9:
            active.append("ASIA")
        if 7 <= hour < 16:
            active.append("LONDON")
        if 12 <= hour < 21:
            active.append("NEW YORK")
        overlap = "LONDON-NEW YORK OVERLAP" if 12 <= hour < 16 else None
        return {
            "active": active or ["OFF-PEAK"],
            "overlap": overlap,
            "status": "UTC_WINDOW_PROXY",
            "timezone": "UTC; daylight-saving exchange calendars unavailable",
        }

    def analyze_timeframe(self, bars: list[dict[str, Any]], timeframe: str, include_auction: bool = True, asset: str | None = None) -> dict[str, Any]:
        # The orchestrator supplies timestamp-sorted, validated bars; avoid re-sorting the 1m replay window.
        clean = list(bars or [])
        base = Kernel._frame_snapshot(clean, timeframe)
        if not base.get("ready"):
            return {
                **base, "market_regime": "WARMING UP", "market_regime_status": "UNAVAILABLE",
                "liquidity": {"status": "UNAVAILABLE", "buy_side": [], "sell_side": []},
                "persistent_liquidity": {"timeframe": timeframe, "status": "WARMING UP", "buy_side": None, "sell_side": None},
                "scalping_liquidity": {
                    "timeframe": timeframe, "status": "WARMING UP",
                    "buy_side": None, "sell_side": None,
                    "target_window_pips": {"min": self.SCALP_MIN_DISTANCE_PIPS, "max": self.SCALP_MAX_DISTANCE_PIPS},
                },
                "zones": {"status": "UNAVAILABLE", "supply": [], "demand": [], "fvg": None, "order_block": None},
                "auction_profile": {"status": "UNAVAILABLE"},
                "order_flow": {"status": "UNAVAILABLE", "reason": "No verified bid/ask historical feed in this replay."},
                "news_risk": {"status": "UNAVAILABLE", "reason": "No verified economic calendar/news feed connected."},
                "data_coverage_pct": 0.0,
            }

        last = clean[-1]
        closes = [float(x["c"]) for x in clean]
        rsi = self._rsi(closes)
        adx = self._adx(clean)
        roc = ((closes[-1] / closes[-min(len(closes), 11)] - 1.0) * 100.0) if len(closes) >= 2 else 0.0
        atr = float(base.get("atr") or 0.0)
        ema20, ema50 = base.get("ema20"), base.get("ema50")
        ema_gap_atr = abs(float(ema20) - float(ema50)) / atr if ema20 is not None and ema50 is not None and atr > 0 else 0.0
        efficiency = float(base.get("efficiency_ratio") or 0.0)
        side = base.get("side")
        structure_event = base.get("structure_event")
        if structure_event == "CHOCH" or (base.get("bos_side") and base.get("displacement_side") and base["bos_side"] != base["displacement_side"]):
            regime = "TREND TRANSITION"
        elif base.get("choppy") or side is None:
            regime = "RANGING"
        elif side in ("BUY", "SELL") and (efficiency >= 0.20 or ema_gap_atr >= 0.25):
            regime = "TRENDING"
        else:
            regime = "DEVELOPING"

        relative_volume = base.get("relative_volume")
        repeated_range = bool(base.get("choppy") and relative_volume is not None and relative_volume >= 1.20)
        phase = "RANGE_ROTATION"
        if repeated_range and base.get("lower_sweep"):
            phase = "POSSIBLE_ACCUMULATION"
        elif repeated_range and base.get("upper_sweep"):
            phase = "POSSIBLE_DISTRIBUTION"
        phase_status = "DEVELOPING" if phase != "RANGE_ROTATION" else "UNCLASSIFIED"
        auction = self._volume_profile(clean) if include_auction else {"status": "DEFERRED_DURING_REPLAY", "basis": "Calculated on final dashboard snapshot"}
        prior_high = base.get("prior_high")
        prior_low = base.get("prior_low")
        levels = []
        for label, value, direction in (
            ("PREVIOUS RANGE HIGH", prior_high, "BUY_SIDE"),
            ("PREVIOUS RANGE LOW", prior_low, "SELL_SIDE"),
            ("SWING HIGH", base.get("swing_high"), "BUY_SIDE"),
            ("SWING LOW", base.get("swing_low"), "SELL_SIDE"),
            ("EQUAL HIGHS", base.get("equal_high"), "BUY_SIDE"),
            ("EQUAL LOWS", base.get("equal_low"), "SELL_SIDE"),
        ):
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                levels.append({"name": label, "price": float(value), "side": direction})
        price = float(last["c"])
        buy_side = sorted((x for x in levels if x["side"] == "BUY_SIDE" and x["price"] > price), key=lambda x: x["price"])
        sell_side = sorted((x for x in levels if x["side"] == "SELL_SIDE" and x["price"] < price), key=lambda x: x["price"], reverse=True)
        persistent_liquidity = self._update_persistent_liquidity(clean, timeframe, base)
        pip_size = self._pip_size_for_asset(asset)
        def scalp_builder(frame_bars, frame_base, frame_close):
            exclusions = {"BUY_SIDE": [], "SELL_SIDE": []}
            for side, keys in (
                ("BUY_SIDE", ("prior_high", "swing_high", "equal_high")),
                ("SELL_SIDE", ("prior_low", "swing_low", "equal_low")),
            ):
                for key in keys:
                    value = frame_base.get(key)
                    if isinstance(value, (int, float)) and math.isfinite(float(value)):
                        exclusions[side].append(float(value))

            # Also exclude the currently locked swing pool, even if it originated
            # before the current frame's rolling candidate window.
            swing_state = self._liquidity_trackers.get(timeframe, {})
            swing_active = swing_state.get("active", {})
            for side in ("BUY_SIDE", "SELL_SIDE"):
                active_swing = swing_active.get(side)
                if active_swing and isinstance(active_swing.get("price"), (int, float)):
                    exclusions[side].append(float(active_swing["price"]))

            return self._scalping_liquidity_candidates(
                frame_bars, timeframe, frame_base, frame_close, pip_size,
                self.SCALP_MIN_DISTANCE_PIPS, self.SCALP_MAX_DISTANCE_PIPS,
                excluded_prices=exclusions,
            )

        scalping_liquidity = self._update_persistent_liquidity(
            clean, timeframe, base, candidate_builder=scalp_builder,
            state_key=f"{timeframe}:SCALP",
            max_active_distance_pips=None,
            pip_size=pip_size,
        )
        scalping_liquidity["pip_size"] = pip_size
        scalping_liquidity["pip_definition"] = "BTC: $1 per pip; GOLD/XAUUSD: $0.01 per pip"
        scalping_liquidity["target_window_pips"] = {
            "min": self.SCALP_MIN_DISTANCE_PIPS,
            "max": self.SCALP_MAX_DISTANCE_PIPS,
        }
        for scalp_level in (scalping_liquidity.get("buy_side"), scalping_liquidity.get("sell_side")):
            if scalp_level:
                scalp_level["distance_pips"] = round(
                    float(scalp_level.get("distance_points") or 0.0) / pip_size, 1
                )

        fvg = base.get("fvg")
        order_block = base.get("order_block")
        zones = {
            "status": "CANDLE_DERIVED_CANDIDATES",
            "supply": [order_block] if order_block and order_block.get("side") == "SELL" else [],
            "demand": [order_block] if order_block and order_block.get("side") == "BUY" else [],
            "fvg": fvg,
            "order_block": order_block,
            "note": "Candidate zones are OHLC-derived; not verified institutional orders.",
        }
        displacement = base.get("displacement_side")
        momentum = "BULLISH" if displacement == "BUY" or (rsi is not None and rsi >= 55 and roc > 0) else "BEARISH" if displacement == "SELL" or (rsi is not None and rsi <= 45 and roc < 0) else "NEUTRAL"
        volatility = "EXPANSION" if atr > 0 and base.get("volatility_ok") and relative_volume is not None and relative_volume >= 1.2 else "NORMAL" if base.get("volatility_ok") else "CONTRACTION_OR_EXTREME"
        body = abs(float(last["c"]) - float(last["o"]))
        candle_range = max(float(last["h"]) - float(last["l"]), 1e-12)
        price_action = {
            "body_pct": round(body / candle_range * 100.0, 1),
            "upper_wick_pct": round((float(last["h"]) - max(float(last["o"]), float(last["c"]))) / candle_range * 100.0, 1),
            "lower_wick_pct": round((min(float(last["o"]), float(last["c"])) - float(last["l"])) / candle_range * 100.0, 1),
            "close_location_pct": round((float(last["c"]) - float(last["l"])) / candle_range * 100.0, 1),
            "displacement": displacement,
        }
        session = self._session(int(last["t"]))
        return {
            **base,
            "market_regime": regime,
            "market_phase": phase,
            "market_phase_status": phase_status,
            "market_regime_status": "CONFIRMED" if regime in ("TRENDING", "RANGING") else "DEVELOPING",
            "trend_strength": round(min(100.0, efficiency * 55.0 + ema_gap_atr * 20.0 + (10.0 if base.get("volume_confirmed") else 0.0)), 1),
            "momentum": {"status": "LIVE", "direction": momentum, "rsi": round(rsi, 2) if rsi is not None else None, "adx": round(adx, 2) if adx is not None else None, "roc_pct": round(roc, 3)},
            "volatility": {"status": "LIVE", "state": volatility, "atr": atr, "atr_pct": base.get("atr_pct"), "efficiency_ratio": round(efficiency, 3)},
            "persistent_liquidity": persistent_liquidity,
            "scalping_liquidity": scalping_liquidity,
            "liquidity": {
                "status": "CANDLE_DERIVED",
                "buy_side": buy_side[:5], "sell_side": sell_side[:5],
                "persistent": persistent_liquidity,
                "scalping": scalping_liquidity,
                "sweep": "LOWER_SWEEP" if base.get("lower_sweep") else "UPPER_SWEEP" if base.get("upper_sweep") else None,
                "acceptance": "ACCEPTED_ABOVE" if prior_high is not None and price > prior_high else "ACCEPTED_BELOW" if prior_low is not None and price < prior_low else "INSIDE_OR_RECLAIMED",
            },
            "zones": zones,
            "auction_profile": auction,
            "volume_behaviour": {
                "status": "OHLCV_PROXY",
                "relative_volume": relative_volume,
                "volume_confirmed": bool(base.get("volume_confirmed")),
                "possible_range_absorption": repeated_range,
                "note": "Volume on candles does not prove bid/ask absorption.",
            },
            "order_flow": {
                "status": "UNAVAILABLE",
                "reason": "Replay candles do not contain verified historical bid/ask, footprint, DOM or time-and-sales data.",
                "cvd": None, "absorption": None, "footprint_imbalance": None,
            },
            "session": session,
            "news_risk": {"status": "UNAVAILABLE", "reason": "No verified live/historical economic calendar feed is connected."},
            "structure_status": "CONFIRMED" if structure_event or base.get("bos_side") else "DEVELOPING",
        }


    @staticmethod
    def _nearest_htf_liquidity(frames: dict[str, dict[str, Any]], side: str, entry: float) -> dict[str, Any] | None:
        key = "buy_side" if side == "BUY" else "sell_side"
        candidates = []
        for timeframe, frame in frames.items():
            persistent = frame.get("persistent_liquidity") or {}
            level = persistent.get(key)
            if not isinstance(level, dict):
                continue
            price = float(level.get("price") or 0.0)
            if price <= 0 or (price <= entry if side == "BUY" else price >= entry):
                continue
            candidates.append({
                "price": price,
                "label": level.get("label", "ACTIVE LIQUIDITY"),
                "timeframe": timeframe,
                "level_id": level.get("id"),
                "distance_points": round(price - entry if side == "BUY" else entry - price, 10),
            })
        if not candidates:
            return None
        return min(candidates, key=lambda item: item["distance_points"])


    def summarize(
        self,
        frames: dict[str, dict[str, Any]],
        micro: dict[str, Any],
        mtf: dict[str, Any],
    ) -> dict[str, Any]:
        weights = self.SCORE_WEIGHTS
        checks: dict[str, dict[str, Any]] = {}
        ready = [x for x in frames.values() if x.get("ready")]
        regime_ok = bool(micro.get("market_regime") not in (None, "WARMING UP"))
        checks["market_regime"] = {"available": regime_ok, "score": 10 if regime_ok else None, "max": weights["market_regime"], "status": micro.get("market_regime_status", "UNAVAILABLE")}
        structure_frames = [frames.get("1d", {}), frames.get("4h", {}), frames.get("1h", {}), frames.get("15m", {}), frames.get("5m", {})]
        structure_ready = [x for x in structure_frames if x.get("ready")]
        structure_agreement = sum(1 for x in structure_ready if x.get("side") == mtf.get("candidate_side"))
        structure_score = (weights["htf_structure"] * structure_agreement / len(structure_ready)) if structure_ready and mtf.get("candidate_side") else 0.0
        checks["htf_structure"] = {"available": bool(structure_ready), "score": round(structure_score, 1) if structure_ready else None, "max": weights["htf_structure"], "status": "LIVE" if structure_ready else "UNAVAILABLE"}
        liquidity_ready = bool(micro.get("liquidity", {}).get("status") == "CANDLE_DERIVED")
        liquidity_score = 15 if micro.get("liquidity", {}).get("sweep") else 8 if micro.get("liquidity", {}).get("buy_side") or micro.get("liquidity", {}).get("sell_side") else 0
        checks["liquidity"] = {"available": liquidity_ready, "score": liquidity_score if liquidity_ready else None, "max": weights["liquidity"], "status": "LIVE" if liquidity_ready else "UNAVAILABLE"}
        auction_status = micro.get("auction_profile", {}).get("status", "UNAVAILABLE")
        checks["auction_profile"] = {"available": auction_status == "PROXY", "score": 5 if auction_status == "PROXY" else None, "max": weights["auction_profile"], "status": auction_status}
        checks["order_flow"] = {"available": False, "score": None, "max": weights["order_flow"], "status": "UNAVAILABLE"}
        zones_ready = micro.get("zones", {}).get("status") == "CANDLE_DERIVED_CANDIDATES"
        checks["supply_demand"] = {"available": zones_ready, "score": 10 if (micro.get("zones", {}).get("fvg") or micro.get("zones", {}).get("order_block")) else 0, "max": weights["supply_demand"], "status": "CANDLE_DERIVED" if zones_ready else "UNAVAILABLE"}
        momentum_ready = micro.get("volatility", {}).get("status") == "LIVE"
        momentum_score = 10 if micro.get("displacement_side") else 5 if micro.get("momentum", {}).get("direction") != "NEUTRAL" else 0
        checks["momentum_volatility"] = {"available": momentum_ready, "score": momentum_score if momentum_ready else None, "max": weights["momentum_volatility"], "status": "LIVE" if momentum_ready else "UNAVAILABLE"}
        checks["price_action"] = {"available": bool(micro.get("ready")), "score": 5 if micro.get("displacement_side") else 3, "max": weights["price_action"], "status": "LIVE" if micro.get("ready") else "UNAVAILABLE"}
        checks["session"] = {"available": micro.get("session", {}).get("status") == "UTC_WINDOW_PROXY", "score": 3, "max": weights["session"], "status": micro.get("session", {}).get("status", "UNAVAILABLE")}
        checks["fundamental_risk"] = {"available": False, "score": None, "max": weights["fundamental_risk"], "status": "UNAVAILABLE"}

        available_max = sum(int(v["max"]) for v in checks.values() if v["available"])
        earned = sum(float(v["score"] or 0.0) for v in checks.values() if v["available"])
        score = round(earned / available_max * 100.0, 1) if available_max else None
        coverage = round(available_max, 1)
        scenarios = {
            "bullish_continuation": {"status": "DEVELOPING", "evidence": [], "opposing": []},
            "bearish_continuation": {"status": "DEVELOPING", "evidence": [], "opposing": []},
            "reversal_transition": {"status": "DEVELOPING", "evidence": [], "opposing": []},
        }
        for key, side in (("bullish_continuation", "BUY"), ("bearish_continuation", "SELL")):
            scenario = scenarios[key]
            if mtf.get("side") == side:
                scenario["evidence"].append("Confirmed weighted multi-timeframe direction")
            if micro.get("side") == side:
                scenario["evidence"].append("1m trend context agrees")
            if micro.get("displacement_side") == side:
                scenario["evidence"].append("1m displacement agrees")
            if micro.get("liquidity", {}).get("sweep") and ((side == "BUY" and micro.get("lower_sweep")) or (side == "SELL" and micro.get("upper_sweep"))):
                scenario["evidence"].append("Opposite-side liquidity sweep observed")
            if mtf.get("side") in ("BUY", "SELL") and mtf.get("side") != side:
                scenario["opposing"].append("Confirmed multi-timeframe direction points the other way")
            scenario["status"] = "SUPPORTED" if len(scenario["evidence"]) >= 3 and not scenario["opposing"] else "DEVELOPING"
        if micro.get("structure_event") == "CHOCH" or micro.get("bos_side") and micro.get("bos_side") != mtf.get("side"):
            scenarios["reversal_transition"]["evidence"].append("Opposing structure break / CHOCH")
        if micro.get("displacement_side") and micro.get("displacement_side") != mtf.get("side"):
            scenarios["reversal_transition"]["evidence"].append("Opposite displacement")
        if micro.get("liquidity", {}).get("sweep"):
            scenarios["reversal_transition"]["evidence"].append("Liquidity sweep observed")
        scenarios["reversal_transition"]["status"] = "SUPPORTED" if len(scenarios["reversal_transition"]["evidence"]) >= 2 else "DEVELOPING"

        current_price = float(micro.get("close") or 0.0)
        liquidity_frames = {**frames, "1m": micro}
        nearest_buy = self._nearest_htf_liquidity(liquidity_frames, "BUY", current_price)
        nearest_sell = self._nearest_htf_liquidity(liquidity_frames, "SELL", current_price)
        return {
            "market_regime": micro.get("market_regime", "WARMING UP"),
            "market_phase": micro.get("market_phase", "UNCLASSIFIED"),
            "market_phase_status": micro.get("market_phase_status", "UNAVAILABLE"),
            "market_bias": micro.get("trend", "NEUTRAL"),
            "trend_strength": micro.get("trend_strength"),
            "market_context_score": score,
            "score_meaning": "Market-context quality only; not win probability.",
            "data_coverage_pct": coverage,
            "score_components": checks,
            "structure_event": micro.get("structure_event") or ("BOS" if micro.get("bos_side") else None),
            "liquidity": micro.get("liquidity", {}),
            "persistent_liquidity": micro.get("persistent_liquidity", {}),
            "scalping_liquidity": micro.get("scalping_liquidity", {}),
            "nearest_buy_side_liquidity": nearest_buy,
            "nearest_sell_side_liquidity": nearest_sell,
            "active_zones": micro.get("zones", {}),
            "auction_profile": micro.get("auction_profile", {}),
            "volume_behaviour": micro.get("volume_behaviour", {}),
            "order_flow": micro.get("order_flow", {"status": "UNAVAILABLE"}),
            "momentum": micro.get("momentum", {}),
            "volatility": micro.get("volatility", {}),
            "session": micro.get("session", {}),
            "news_risk": micro.get("news_risk", {"status": "UNAVAILABLE"}),
            "scenarios": scenarios,
            "next_confirmation_required": "1m sweep/rejection → MSS/BOS → displacement → FVG/OB retest, with confirmed MTF direction.",
            "data_notes": [
                "Auction POC/VAH/VAL are an OHLCV typical-price proxy, not exchange trade-level volume profile.",
                "Historical bid/ask order flow, DOM, footprint and absorption are unavailable and are not invented.",
                "News/fundamental risk is unavailable until a verified calendar/news source is connected.",
                "Session labels are UTC-window approximations and do not yet apply exchange-calendar daylight-saving rules.",
                "Supply/demand and order-block zones are candle-derived candidates, not verified institutional orders.",
            ],
        }
