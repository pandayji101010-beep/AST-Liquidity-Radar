from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from institutional_campaign_strategy import ASTInstitutionalCampaignStrategy as Kernel


class ASTMarketBehaviourIntelligenceEngine:
    """MBIE: market understanding only. It never creates or approves orders."""

    SCORE_WEIGHTS = {
        "market_regime": 10, "htf_structure": 15, "liquidity": 15,
        "auction_profile": 10, "order_flow": 15, "supply_demand": 10,
        "momentum_volatility": 10, "price_action": 5, "session": 5,
        "fundamental_risk": 5,
    }

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

    def analyze_timeframe(self, bars: list[dict[str, Any]], timeframe: str, include_auction: bool = True) -> dict[str, Any]:
        # The orchestrator supplies timestamp-sorted, validated bars; avoid re-sorting the 1m replay window.
        clean = list(bars or [])
        base = Kernel._frame_snapshot(clean, timeframe)
        if not base.get("ready"):
            return {
                **base, "market_regime": "WARMING UP", "market_regime_status": "UNAVAILABLE",
                "liquidity": {"status": "UNAVAILABLE", "buy_side": [], "sell_side": []},
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
            "liquidity": {
                "status": "CANDLE_DERIVED",
                "buy_side": buy_side[:5], "sell_side": sell_side[:5],
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
        fields = (
            ("prior_high", "PREVIOUS HIGH", "BUY"),
            ("previous_period_high", "PREVIOUS PERIOD HIGH", "BUY"),
            ("equal_high", "EQUAL HIGHS", "BUY"),
            ("swing_high", "SWING HIGH", "BUY"),
            ("range_high", "RANGE HIGH", "BUY"),
            ("prior_low", "PREVIOUS LOW", "SELL"),
            ("previous_period_low", "PREVIOUS PERIOD LOW", "SELL"),
            ("equal_low", "EQUAL LOWS", "SELL"),
            ("swing_low", "SWING LOW", "SELL"),
            ("range_low", "RANGE LOW", "SELL"),
        )
        candidates = []
        for tf, frame in frames.items():
            if not frame.get("ready"):
                continue
            for key, label, direction in fields:
                value = frame.get(key)
                if not isinstance(value, (int, float)):
                    continue
                price = float(value)
                if direction == side and (price > entry if side == "BUY" else price < entry):
                    candidates.append({"price": price, "label": label, "timeframe": tf})
        if not candidates:
            return None
        return min(candidates, key=lambda x: x["price"]) if side == "BUY" else max(candidates, key=lambda x: x["price"])

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
        nearest_buy = self._nearest_htf_liquidity(frames, "BUY", current_price)
        nearest_sell = self._nearest_htf_liquidity(frames, "SELL", current_price)
        if nearest_buy is None:
            buy_targets = micro.get("liquidity", {}).get("buy_side", [])
            nearest_buy = buy_targets[0] if buy_targets else None
        if nearest_sell is None:
            sell_targets = micro.get("liquidity", {}).get("sell_side", [])
            nearest_sell = sell_targets[0] if sell_targets else None
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
