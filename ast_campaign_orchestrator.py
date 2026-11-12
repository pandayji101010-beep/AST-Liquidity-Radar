from __future__ import annotations

import bisect
from typing import Any

from campaign_engine import ASTCampaignEngine
from entry_engine import ASTEntryEngine
from institutional_campaign_strategy import ASTInstitutionalCampaignStrategy as Kernel, FRAME_MS
from mbie_engine import ASTMarketBehaviourIntelligenceEngine
from multi_timeframe_engine import ASTMultiTimeframeEngine, HTF_ORDER


class ASTFourEngineOrchestrator:
    """Coordinates MBIE -> MTF -> Entry -> Campaign with one-way data contracts."""

    REPLAY_LIMIT = Kernel.REPLAY_LIMIT

    def __init__(self, asset: str = "MARKET") -> None:
        self.asset = asset.upper()
        self.mbie = ASTMarketBehaviourIntelligenceEngine()
        self.mtf_engine = ASTMultiTimeframeEngine()
        self.entry_engine = ASTEntryEngine()
        self.campaign_engine = ASTCampaignEngine(self.asset)
        self._cache_key: tuple[Any, ...] | None = None
        self._cache_value: dict[str, Any] | None = None

    @staticmethod
    def _cache_signature(candles: dict[str, Any], asset: str) -> tuple[Any, ...]:
        signature: list[Any] = [asset]
        for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d"):
            rows = candles.get(tf) or []
            if rows:
                signature.append((tf, len(rows), int(rows[0]["t"]), int(rows[-1]["t"]), float(rows[-1]["c"])))
            else:
                signature.append((tf, 0, None, None, None))
        return tuple(signature)

    def snapshot(self, candles: dict[str, Any], asset: str | None = None) -> dict[str, Any]:
        asset_name = (asset or self.asset or "MARKET").upper()
        if self.asset != asset_name:
            self.asset = asset_name
            # Load the correct BTC / GOLD contract-value environment setting for this asset.
            self.campaign_engine = ASTCampaignEngine(asset_name)
        key = self._cache_signature(candles, asset_name)
        if self._cache_key == key and self._cache_value is not None:
            return self._cache_value
        result = self._replay(candles, asset_name)
        self._cache_key, self._cache_value = key, result
        return result

    def _empty(self, asset: str) -> dict[str, Any]:
        return {
            "asset": asset, "strategy_name": "AST Campaign · MBIE + MTF + Entry + Campaign",
            "status": "WAITING FOR 1M DATA", "reason": "No completed 1m candles are available.",
            "higher_timeframes": {},
            "alignment": {"status": "WAITING", "side": None, "candidate_side": None, "agreement_pct": 0.0},
            "market_intelligence": {"market_regime": "WARMING UP", "data_coverage_pct": 0.0, "market_context_score": None},
            "entry_engine": {"status": "WAITING", "candidate": None},
            "execution_1m": None,
            "campaign": {
                "status": "NO CAMPAIGN", "positions": [], "open_positions": 0,
                "risk_pct": 0.0, "max_risk_pct": self.campaign_engine.max_campaign_risk_pct,
                "can_add_position": False,
            },
            "recent_closed": [],
            "performance": {"closed_positions": 0, "win_rate_pct": None, "profit_factor": None, "net_r": 0.0, "max_drawdown_pct_of_equity": 0.0},
            "replay": {"bars_processed": 0, "bars_available": 0, "lookahead_safe": True},
            "data_notes": ["No 1m candles available; all engines are waiting."],
            "engine_contract": self.engine_contract(),
        }

    @staticmethod
    def engine_contract() -> dict[str, str]:
        return {
            "MBIE": "Market regime, structure, liquidity, auction proxy, zones, volume behaviour, momentum, volatility, scenarios and data coverage. No entry signals.",
            "Multi-Timeframe Engine": "Combines completed timeframe context and publishes directional agreement/conflicts. Does not overwrite MBIE timeframe readings.",
            "Entry Engine": "Creates MAIN and ADD-ON 1m candidates from the MTF-approved context. Does not calculate quantity, stop loss or take profit.",
            "Campaign Engine": "Sole owner of quantity estimation, SL, TP, per-position risk, total campaign exposure, add-on protection, partial exits and campaign invalidation.",
        }

    def _replay(self, candles: dict[str, Any], asset: str) -> dict[str, Any]:
        self.mbie.reset_liquidity_trackers()
        one = Kernel._clean_bars(candles.get("1m"))
        if not one:
            self.entry_engine.reset()
            self.campaign_engine.reset()
            return self._empty(asset)
        one = one[-self.REPLAY_LIMIT:]

        source_frames: dict[str, list[dict[str, Any]]] = {
            tf: Kernel._clean_bars(candles.get(tf)) for tf in FRAME_MS if tf != "1m"
        }
        source_frames["1w"] = Kernel._aggregate_calendar(source_frames.get("1d", []), "1w")
        source_frames["1M"] = Kernel._aggregate_calendar(source_frames.get("1d", []), "1M")
        source_frames["1m"] = one

        close_times: dict[str, list[int]] = {}
        for tf, rows in source_frames.items():
            if tf in ("1w", "1M"):
                close_times[tf] = [int(x["_close_t"]) for x in rows]
            else:
                close_times[tf] = [int(x["t"]) + FRAME_MS[tf] for x in rows]

        cached_count = {tf: -1 for tf in source_frames}
        cached_frame: dict[str, dict[str, Any]] = {}
        self.entry_engine.reset()
        self.campaign_engine.reset()
        processed = 0
        latest_micro: dict[str, Any] = {}
        latest_mbie: dict[str, Any] = {}
        latest_mtf: dict[str, Any] = {"status": "WAITING", "side": None, "candidate_side": None, "agreement_pct": 0.0}
        last_candidate: dict[str, Any] | None = None
        last_candidate_state = "WAITING FOR 1M SETUP"

        for index, bar in enumerate(one):
            asof = int(bar["t"]) + FRAME_MS["1m"]
            day_key = ASTCampaignEngine._day_pnl_key(int(bar["t"]))

            # MBIE analyses each newly completed timeframe snapshot independently.
            for tf, rows in source_frames.items():
                if tf == "1m":
                    continue
                count = bisect.bisect_right(close_times[tf], asof)
                if count != cached_count[tf]:
                    available = rows[max(0, count - 320):count]
                    if tf in ("1w", "1M"):
                        available = [x for x in available if int(x["_close_t"]) <= asof]
                    else:
                        available = [x for x in available if int(x["t"]) + FRAME_MS[tf] <= asof]
                    cached_frame[tf] = self.mbie.analyze_timeframe(available, tf, asset=asset)
                    cached_count[tf] = count

            higher = {
                tf: cached_frame.get(tf, {"ready": False, "side": None, "market_regime": "WARMING UP"})
                for tf in HTF_ORDER
            }
            micro_bars = one[max(0, index - 320):index + 1]
            micro = self.mbie.analyze_timeframe(micro_bars, "1m", include_auction=False, asset=asset)
            higher_with_execution = {**higher, "1m": micro}
            latest_mtf = self.mtf_engine.combine(higher_with_execution)
            latest_micro = micro
            if not micro.get("ready"):
                self.campaign_engine.process_bar(bar, asof, micro, latest_mtf)
                processed += 1
                continue

            latest_mbie = self.mbie.summarize(higher, micro, latest_mtf)

            # Campaign state is managed before a fresh entry candidate is considered.
            self.campaign_engine.process_bar(bar, asof, micro, latest_mtf)
            campaign_context = self.campaign_engine.entry_context(micro)
            candidate = self.entry_engine.evaluate(
                index=index, bar=bar, micro=micro, frames=higher,
                mtf=latest_mtf, mbie=latest_mbie, campaign_context=campaign_context,
            )
            if candidate:
                decision = self.campaign_engine.queue_candidate(candidate)
                last_candidate = candidate
                last_candidate_state = "CANDIDATE ACCEPTED" if decision.get("accepted") else "RISK GATE REJECTED"
                if not decision.get("accepted"):
                    self.campaign_engine.last_rejection = decision.get("reason")
            processed += 1

        latest_asof = int(one[-1]["t"]) + FRAME_MS["1m"]
        final_frames: dict[str, dict[str, Any]] = {}
        for tf, rows in source_frames.items():
            count = bisect.bisect_right(close_times[tf], latest_asof) if tf != "1m" else len(one)
            available = rows[max(0, count - 320):count]
            if tf in ("1w", "1M"):
                available = [x for x in available if int(x["_close_t"]) <= latest_asof]
            else:
                available = [x for x in available if int(x["t"]) + FRAME_MS[tf] <= latest_asof]
            final_frames[tf] = self.mbie.analyze_timeframe(available, tf, asset=asset)

        higher = {tf: final_frames.get(tf, {"ready": False, "side": None, "market_regime": "WARMING UP"}) for tf in HTF_ORDER}
        final_micro_bars = one[-min(len(one), 320):]
        final_micro = self.mbie.analyze_timeframe(final_micro_bars, "1m", asset=asset)
        alignment = self.mtf_engine.combine({**higher, "1m": final_micro})
        market_summary = self.mbie.summarize(higher, final_micro, alignment) if final_micro.get("ready") else latest_mbie
        campaign_state = self.campaign_engine.snapshot()

        pending = campaign_state.get("pending")
        positions = campaign_state["campaign"].get("positions", [])
        if pending:
            execution = {
                **pending,
                "signal_close": pending.get("signal_close"),
                "status": "ENTRY READY · NEXT 1M OPEN",
                "execution": "Signal formed on completed 1m candle; fill is simulated at the next 1m open.",
            }
        elif positions:
            latest_position = positions[-1]
            execution = {
                "asset": asset, "timeframe": "1m", "status": "CAMPAIGN ACTIVE",
                "side": campaign_state["campaign"].get("side"), "kind": "MAIN + ADD-ONS",
                "entry_price": latest_position.get("entry_price"), "stop_loss": latest_position.get("stop_loss"),
                "partial_take_profit": latest_position.get("partial_take_profit"),
                "take_profit": latest_position.get("take_profit"),
                "open_positions": len(positions), "campaign_risk_pct": campaign_state["campaign"].get("risk_pct"),
                "reason": "Campaign Engine manages each open position's SL, partial and final target.",
            }
        else:
            execution = None

        if execution:
            status = execution["status"]
            reason = execution.get("reason", "")
        elif alignment.get("status") != "CONFIRMED":
            status = "WAITING FOR HTF CONFIRMATION"
            reason = alignment.get("reason", "")
        elif final_micro.get("market_regime") == "RANGING":
            status = "NO TRADE · RANGING MARKET"
            reason = "MBIE classifies the current 1m regime as ranging; wait for fresh directional behaviour."
        elif campaign_state.get("last_rejection"):
            status = "ENTRY BLOCKED · CAMPAIGN RISK"
            reason = campaign_state["last_rejection"]
        else:
            status = "WAITING FOR 1M ENTRY"
            reason = "MBIE context is ready; waiting for the Entry Engine's complete 1m sequence."

        entry_state = "WAITING"
        if pending:
            entry_state = "PENDING NEXT 1M OPEN"
        elif last_candidate_state != "WAITING FOR 1M SETUP":
            entry_state = last_candidate_state
        elif campaign_state.get("last_rejection"):
            entry_state = "BLOCKED BY CAMPAIGN ENGINE"

        notes = list(market_summary.get("data_notes", [])) if market_summary else []
        notes.extend([
            "The four engines communicate through explicit data contracts; Entry Engine cannot override MTF direction and Campaign Engine owns all position-risk decisions.",
            "Replay uses completed candles only. Signals are analytical and simulated; this service does not place broker orders.",
            "Fees and slippage are excluded from replay results.",
        ])
        return {
            "asset": asset,
            "strategy_name": "AST Campaign · MBIE + Multi-Timeframe + Entry + Campaign",
            "status": status,
            "reason": reason,
            "higher_timeframes": higher,
            "alignment": alignment,
            "market_intelligence": market_summary or {},
            "entry_engine": {
                "status": entry_state,
                "last_candidate": last_candidate,
                "candidate": pending,
                "block_reason": self.entry_engine.last_block_reason,
                "thresholds": {
                    "minimum_entry_score": self.entry_engine.MIN_SCORE,
                    "minimum_context_score": self.entry_engine.MIN_CONTEXT_SCORE,
                    "minimum_data_coverage_pct": self.entry_engine.MIN_CONTEXT_COVERAGE,
                    "maximum_setup_age_bars": self.entry_engine.MAX_SETUP_AGE,
                    "maximum_bos_displacement_gap_bars": self.entry_engine.MAX_BOS_DISPLACEMENT_GAP,
                },
                "responsibility": "MAIN / ADD-ON candidate generation only; no quantity, SL or TP.",
            },
            "execution_1m": execution,
            "campaign": campaign_state["campaign"],
            "recent_closed": campaign_state["recent_closed"],
            "performance": campaign_state["performance"],
            "replay": {
                "bars_processed": processed,
                "bars_available": len(one),
                "first_bar_time": int(one[0]["t"]),
                "last_bar_close_time": latest_asof,
                "lookahead_safe": True,
                "execution_model": "Entry candidate on completed 1m candle; simulated fill at next 1m open; within-candle SL is evaluated before target.",
            },
            "data_notes": notes,
            "engine_contract": self.engine_contract(),
        }
