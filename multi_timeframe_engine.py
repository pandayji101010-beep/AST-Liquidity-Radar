from __future__ import annotations

from typing import Any


HTF_ORDER = ("1M", "1w", "1d", "4h", "1h", "30m", "15m", "5m")
CONTEXT_ORDER = (*HTF_ORDER, "1m")
MIN_DIRECTIONAL_AGREEMENT = 60.0
HTF_WEIGHTS = {
    "1M": 1.0, "1w": 1.5, "1d": 2.0, "4h": 2.0,
    "1h": 2.5, "30m": 2.5, "15m": 3.0, "5m": 3.0,
}


class ASTMultiTimeframeEngine:
    """Combines MBIE timeframes; never changes an individual timeframe's analysis."""

    @staticmethod
    def combine(frames: dict[str, dict[str, Any]]) -> dict[str, Any]:
        usable = {
            tf: frames.get(tf, {}) for tf in HTF_ORDER
            if frames.get(tf, {}).get("ready") and frames.get(tf, {}).get("side") in ("BUY", "SELL")
        }
        buy_weight = sum(HTF_WEIGHTS[tf] for tf, data in usable.items() if data["side"] == "BUY")
        sell_weight = sum(HTF_WEIGHTS[tf] for tf, data in usable.items() if data["side"] == "SELL")
        total = buy_weight + sell_weight
        candidate = "BUY" if buy_weight > sell_weight else "SELL" if sell_weight > buy_weight else None
        agreement = max(buy_weight, sell_weight) / total if total else 0.0
        core_ready = all(frames.get(tf, {}).get("ready") for tf in ("1d", "4h", "1h", "15m", "5m"))
        near_term = bool(candidate and frames.get("15m", {}).get("side") == candidate and frames.get("5m", {}).get("side") == candidate)
        confirmed = bool(core_ready and near_term and agreement >= MIN_DIRECTIONAL_AGREEMENT / 100.0)
        conflict = bool(candidate and not near_term)
        conflicts = [
            {"timeframe": tf, "side": frames[tf].get("side")}
            for tf in HTF_ORDER
            if frames.get(tf, {}).get("ready") and frames[tf].get("side") in ("BUY", "SELL") and candidate and frames[tf].get("side") != candidate
        ]
        return {
            "status": "CONFIRMED" if confirmed else "CONFLICT" if conflict else "WAITING",
            "side": candidate if confirmed else None,
            "candidate_side": candidate,
            "agreement_pct": round(agreement * 100.0, 1),
            "required_agreement_pct": MIN_DIRECTIONAL_AGREEMENT,
            "buy_weight": round(buy_weight, 2),
            "sell_weight": round(sell_weight, 2),
            "ready_timeframes": [tf for tf in CONTEXT_ORDER if frames.get(tf, {}).get("ready")],
            "conflicting_timeframes": conflicts,
            "timeframe_context": {
                tf: {
                    "ready": bool(frames.get(tf, {}).get("ready")),
                    "side": frames.get(tf, {}).get("side"),
                    "regime": frames.get(tf, {}).get("market_regime", "UNAVAILABLE"),
                    "structure_event": frames.get(tf, {}).get("structure_event"),
                }
                for tf in CONTEXT_ORDER
            },
            "execution_context": {
                "ready": bool(frames.get("1m", {}).get("ready")),
                "side": frames.get("1m", {}).get("side"),
                "status": (
                    "UNAVAILABLE" if not frames.get("1m", {}).get("ready") else
                    "ALIGNED" if candidate and frames.get("1m", {}).get("side") == candidate else
                    "COUNTER-TREND / PULLBACK" if candidate and frames.get("1m", {}).get("side") in ("BUY", "SELL") else
                    "NEUTRAL"
                ),
                "higher_timeframe_side": candidate,
                "note": "1m is displayed as execution context; it never overrides weighted higher-timeframe direction.",
            },
            "reason": (
                f"Weighted higher-timeframe bias confirmed; 15m and 5m agree and weighted support is at least {MIN_DIRECTIONAL_AGREEMENT:.0f}%."
                if confirmed else
                f"Timeframe directions conflict; 15m/5m agreement and {MIN_DIRECTIONAL_AGREEMENT:.0f}% weighted directional support are required."
                if conflict else
                "Waiting for sufficient completed higher-timeframe context."
            ),
        }
