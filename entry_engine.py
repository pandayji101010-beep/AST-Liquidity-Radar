from __future__ import annotations

from typing import Any


class ASTEntryEngine:
    """Creates main/add-on entry candidates only; it does not size trades or set SL/TP."""

    MIN_SCORE = 70.0
    MIN_CONTEXT_SCORE = 55.0
    MIN_CONTEXT_COVERAGE = 45.0
    MAX_SETUP_AGE = 18

    def __init__(self) -> None:
        self.setup: dict[str, Any] | None = None
        self.last_candidate_time: int | None = None

    def reset(self) -> None:
        self.setup = None
        self.last_candidate_time = None

    @staticmethod
    def _liquidity_targets(frames: dict[str, dict[str, Any]], side: str, entry: float) -> list[dict[str, Any]]:
        candidates: dict[tuple[int, str], dict[str, Any]] = {}
        fields = (
            ("prior_high", "PREVIOUS RANGE HIGH"), ("previous_period_high", "PREVIOUS PERIOD HIGH"),
            ("equal_high", "EQUAL HIGHS"), ("swing_high", "SWING HIGH"), ("range_high", "RANGE HIGH"),
            ("prior_low", "PREVIOUS RANGE LOW"), ("previous_period_low", "PREVIOUS PERIOD LOW"),
            ("equal_low", "EQUAL LOWS"), ("swing_low", "SWING LOW"), ("range_low", "RANGE LOW"),
        )
        for tf, data in frames.items():
            if not data.get("ready"):
                continue
            for field, label in fields:
                value = data.get(field)
                if not isinstance(value, (int, float)):
                    continue
                price = float(value)
                eligible = price > entry if side == "BUY" else price < entry
                if eligible:
                    candidates[(round(price * 100000), tf + ":" + label)] = {
                        "price": price, "timeframe": tf, "label": label,
                    }
            liquidity = data.get("liquidity", {})
            pools = liquidity.get("buy_side", []) if side == "BUY" else liquidity.get("sell_side", [])
            for pool in pools:
                price = float(pool.get("price", 0.0))
                if price and (price > entry if side == "BUY" else price < entry):
                    candidates[(round(price * 100000), tf + ":" + str(pool.get("name")))] = {
                        "price": price, "timeframe": tf, "label": pool.get("name", "LIQUIDITY"),
                    }
        ordered = sorted(candidates.values(), key=lambda x: x["price"], reverse=side == "SELL")
        return ordered[:30]

    def evaluate(
        self,
        index: int,
        bar: dict[str, Any],
        micro: dict[str, Any],
        frames: dict[str, dict[str, Any]],
        mtf: dict[str, Any],
        mbie: dict[str, Any],
        campaign_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not micro.get("ready"):
            return None
        side_bias = mtf.get("side")
        if micro.get("market_regime") == "RANGING" or micro.get("choppy"):
            self.setup = None
            return None
        if not side_bias:
            self.setup = None
            return None

        body = abs(float(bar["c"]) - float(bar["o"]))
        full_range = max(float(bar["h"]) - float(bar["l"]), 1e-12)
        close_pos = (float(bar["c"]) - float(bar["l"])) / full_range
        lower_wick = min(float(bar["o"]), float(bar["c"])) - float(bar["l"])
        upper_wick = float(bar["h"]) - max(float(bar["o"]), float(bar["c"]))
        atr = float(micro.get("atr") or 0.0)
        bullish_rejection = body > 0 and lower_wick >= body * 1.25 and close_pos >= 0.65
        bearish_rejection = body > 0 and upper_wick >= body * 1.25 and close_pos <= 0.35
        lower_sweep = bool(micro.get("lower_sweep"))
        upper_sweep = bool(micro.get("upper_sweep"))
        sweep_side = "BUY" if lower_sweep or bullish_rejection else "SELL" if upper_sweep or bearish_rejection else None

        close_time = int(bar["t"]) + 60_000
        if sweep_side == side_bias:
            self.setup = {
                "side": sweep_side,
                "sweep_time": close_time,
                "sweep_price": float(bar["l"] if sweep_side == "BUY" else bar["h"]),
                "start_index": index,
                "bos_index": None,
                "displacement_index": None,
                "zone": None,
                "consumed": False,
            }

        setup = self.setup
        if setup is None:
            return None
        age = index - int(setup["start_index"])
        if age > self.MAX_SETUP_AGE or setup["side"] != side_bias:
            self.setup = None
            return None
        side = setup["side"]

        if setup["bos_index"] is None and micro.get("bos_side") == side:
            setup["bos_index"] = index
        if setup["displacement_index"] is None and micro.get("displacement_side") == side:
            setup["displacement_index"] = index
        if setup["bos_index"] is not None and setup["displacement_index"] is not None:
            if abs(int(setup["bos_index"]) - int(setup["displacement_index"])) <= 4 and setup["zone"] is None:
                zone = micro.get("fvg")
                if not zone or zone.get("side") != side:
                    zone = micro.get("order_block")
                if zone and zone.get("side") == side:
                    setup["zone"] = dict(zone)

        zone = setup.get("zone")
        after_confirm = (
            setup["bos_index"] is not None and setup["displacement_index"] is not None
            and index > max(int(setup["bos_index"]), int(setup["displacement_index"]))
        )
        if not zone or not after_confirm or setup.get("consumed"):
            return None

        low, high = float(zone["low"]), float(zone["high"])
        intersects = float(bar["l"]) <= high and float(bar["h"]) >= low
        midpoint = (low + high) / 2.0
        rejection = (
            intersects and float(bar["c"]) > midpoint and float(bar["c"]) > float(bar["o"])
            if side == "BUY" else
            intersects and float(bar["c"]) < midpoint and float(bar["c"]) < float(bar["o"])
        )
        if not rejection or mtf.get("status") != "CONFIRMED":
            return None

        campaign_side = campaign_context.get("side")
        is_main = not campaign_context.get("active")
        if is_main:
            kind = "MAIN"
        elif campaign_side == side and campaign_context.get("can_add_position"):
            kind = "ADD-ON"
        else:
            return None

        context_score = float(mbie.get("market_context_score") or 0.0)
        context_coverage = float(mbie.get("data_coverage_pct") or 0.0)
        if context_score < self.MIN_CONTEXT_SCORE or context_coverage < self.MIN_CONTEXT_COVERAGE:
            return None
        htf_score = min(20.0, float(mtf.get("agreement_pct") or 0.0) * 0.20)
        checks = {
            "market_context": round(context_score * 0.15, 1),
            "htf_alignment": round(htf_score, 1),
            "liquidity_sweep_or_rejection": 15.0,
            "mss_or_bos": 15.0 if setup["bos_index"] is not None else 0.0,
            "displacement": 15.0 if setup["displacement_index"] is not None else 0.0,
            "fvg_or_order_block_retest": 10.0,
            "relative_volume": 5.0 if micro.get("volume_confirmed") else 0.0,
            "premium_discount_location": 5.0 if location_ok else 0.0,
        }
        score = round(sum(checks.values()), 1)
        if score < self.MIN_SCORE:
            return None

        premium_discount = micro.get("premium_discount")
        location_ok = premium_discount == ("DISCOUNT" if side == "BUY" else "PREMIUM")
        targets = self._liquidity_targets(frames, side, float(bar["c"]))
        candidate = {
            "kind": kind,
            "side": side,
            "timeframe": "1m",
            "signal_time": close_time,
            "signal_price": float(bar["c"]),
            "sweep_price": float(setup["sweep_price"]),
            "atr": atr,
            "zone": dict(zone),
            "liquidity_targets": targets,
            "score": score,
            "grade": "HIGHEST QUALITY" if score >= 90 else "STRONG" if score >= 80 else "QUALIFIED",
            "score_checks": checks,
            "market_context_score": mbie.get("market_context_score"),
            "market_context_coverage_pct": context_coverage,
            "htf_agreement_pct": mtf.get("agreement_pct"),
            "location": premium_discount,
            "location_aligned": location_ok,
            "scenario": "BULLISH CONTINUATION" if side == "BUY" else "BEARISH CONTINUATION",
            "reason": "MBIE context + confirmed MTF direction + 1m sweep/rejection + BOS/MSS + displacement + FVG/OB retest.",
            "execution": "Candidate is formed on a completed 1m candle; Campaign Engine may simulate fill at the next 1m open.",
        }
        setup["consumed"] = True
        self.last_candidate_time = close_time
        return candidate
