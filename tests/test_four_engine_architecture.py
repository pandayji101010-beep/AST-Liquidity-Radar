from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from ast_campaign_orchestrator import ASTFourEngineOrchestrator
from campaign_engine import ASTCampaignEngine
from entry_engine import ASTEntryEngine
from mbie_engine import ASTMarketBehaviourIntelligenceEngine
from multi_timeframe_engine import ASTMultiTimeframeEngine


class FourEngineArchitectureTests(unittest.TestCase):
    @staticmethod
    def minute_bars(count: int = 100):
        start = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)
        rows = []
        for i in range(count):
            base = 100.0 + i * 0.03
            rows.append({
                "t": start + i * 60_000,
                "o": base, "h": base + 0.14, "l": base - 0.12,
                "c": base + 0.05, "v": 100 + (i % 7) * 5,
            })
        return rows

    def test_mbie_reports_context_and_never_emits_an_order(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        frame = mbie.analyze_timeframe(self.minute_bars(), "1m")
        self.assertTrue(frame["ready"])
        self.assertIn(frame["market_regime"], ("TRENDING", "RANGING", "TREND TRANSITION", "DEVELOPING"))
        summary = mbie.summarize({}, frame, {"status": "WAITING", "side": None, "candidate_side": None})
        self.assertIn("market_context_score", summary)
        self.assertIn("data_coverage_pct", summary)
        self.assertEqual(summary["order_flow"]["status"], "UNAVAILABLE")
        self.assertEqual(summary["news_risk"]["status"], "UNAVAILABLE")
        self.assertNotIn("entry", summary)
        self.assertNotIn("stop_loss", summary)
        self.assertNotIn("take_profit", summary)

    def test_multi_timeframe_engine_preserves_context_and_reports_conflicts(self):
        engine = ASTMultiTimeframeEngine()
        frames = {
            tf: {"ready": True, "side": "BUY", "market_regime": "TRENDING"}
            for tf in ("1M", "1w", "1d", "4h", "1h", "30m", "15m", "5m")
        }
        aligned = engine.combine(frames)
        self.assertEqual(engine.combine(frames)["required_agreement_pct"], 60.0)
        self.assertEqual(aligned["status"], "CONFIRMED")
        self.assertEqual(aligned["side"], "BUY")
        self.assertEqual(frames["4h"]["side"], "BUY")
        frames_with_1m = {**frames, "1m": {"ready": True, "side": "SELL", "market_regime": "TRENDING"}}
        with_execution = engine.combine(frames_with_1m)
        self.assertEqual(with_execution["side"], "BUY")
        self.assertEqual(with_execution["execution_context"]["status"], "COUNTER-TREND / PULLBACK")
        self.assertIn("1m", with_execution["timeframe_context"])
        frames["5m"] = {"ready": True, "side": "SELL", "market_regime": "RANGING"}
        conflicted = engine.combine(frames)
        self.assertNotEqual(conflicted["status"], "CONFIRMED")
        self.assertTrue(conflicted["conflicting_timeframes"])

    def test_entry_engine_returns_candidate_without_campaign_owned_levels(self):
        entry = ASTEntryEngine()
        mtf = {"status": "CONFIRMED", "side": "BUY", "agreement_pct": 90.0}
        frames = {
            "5m": {"ready": True, "prior_high": 110.0, "swing_high": 112.0},
            "15m": {"ready": True, "prior_high": 115.0},
        }
        mbie = {"market_context_score": 85.0, "data_coverage_pct": 75.0}
        campaign_context = {"active": False, "side": None, "can_add_position": False}
        zone = {"side": "BUY", "low": 99.5, "high": 100.5}
        micro = {
            "ready": True, "market_regime": "TRENDING", "choppy": False, "atr": 1.0,
            "lower_sweep": True, "upper_sweep": False, "bos_side": "BUY",
            "displacement_side": "BUY", "fvg": zone, "order_block": None,
            "volume_confirmed": True, "premium_discount": "DISCOUNT",
        }
        first = {"t": 1_700_000_000_000, "o": 100.0, "h": 101.0, "l": 98.0, "c": 100.2}
        self.assertIsNone(entry.evaluate(0, first, micro, frames, mtf, mbie, campaign_context))
        micro["lower_sweep"] = False
        second = {"t": first["t"] + 60_000, "o": 100.0, "h": 101.0, "l": 99.7, "c": 100.8}
        candidate = entry.evaluate(1, second, micro, frames, mtf, mbie, campaign_context)
        self.assertIsNotNone(candidate)
        self.assertIn("qualified", entry.last_block_reason)
        self.assertEqual(candidate["kind"], "MAIN")
        self.assertEqual(entry.MIN_SCORE, 65.0)
        self.assertEqual(entry.MIN_CONTEXT_SCORE, 50.0)
        self.assertEqual(entry.MIN_CONTEXT_COVERAGE, 40.0)
        self.assertEqual(entry.MAX_SETUP_AGE, 24)
        self.assertEqual(candidate["side"], "BUY")
        self.assertIn("liquidity_targets", candidate)
        self.assertNotIn("stop_loss", candidate)
        self.assertNotIn("take_profit", candidate)
        self.assertNotIn("quantity", candidate)

    def test_campaign_engine_owns_sl_tp_quantity_and_only_unblocks_addons_after_protection(self):
        engine = ASTCampaignEngine("BTC")
        candidate = {
            "kind": "MAIN", "side": "BUY", "signal_time": 1_700_000_000_000,
            "signal_price": 100.0, "sweep_price": 98.0, "atr": 1.0,
            "liquidity_targets": [{"price": 106.0, "timeframe": "5m", "label": "SWING HIGH"}],
            "score": 90.0, "grade": "STRONG", "reason": "test",
        }
        queued = engine.queue_candidate(candidate)
        self.assertTrue(queued["accepted"])
        self.assertLess(queued["plan"]["stop_loss"], candidate["signal_price"])
        self.assertGreater(queued["plan"]["take_profit"], candidate["signal_price"])
        self.assertIsNone(queued["plan"]["quantity"])
        self.assertIn("UNAVAILABLE", queued["plan"]["quantity_status"])
        self.assertNotIn("stop_loss", candidate)

        fill_bar = {"t": candidate["signal_time"] + 60_000, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.2}
        micro = {"atr": 1.0}
        mtf = {"status": "CONFIRMED", "side": "BUY"}
        engine.process_bar(fill_bar, fill_bar["t"] + 60_000, micro, mtf)
        before_partial = engine.entry_context(micro)
        self.assertTrue(before_partial["active"])
        self.assertFalse(before_partial["can_add_position"])

        partial_bar = {"t": fill_bar["t"] + 60_000, "o": 100.2, "h": 103.0, "l": 100.0, "c": 102.5}
        engine.process_bar(partial_bar, partial_bar["t"] + 60_000, micro, mtf)
        after_partial = engine.entry_context(micro)
        self.assertTrue(after_partial["existing_positions_protected"])
        self.assertTrue(after_partial["favorable_extension_confirmed"])
        self.assertTrue(after_partial["can_add_position"])

        addon = {
            **candidate, "kind": "ADD-ON", "signal_time": partial_bar["t"] + 60_000,
            "signal_price": 102.5, "sweep_price": 101.0,
            "liquidity_targets": [{"price": 108.0, "timeframe": "15m", "label": "EQUAL HIGH"}],
        }
        self.assertTrue(engine.queue_candidate(addon)["accepted"])
        next_bar = {"t": addon["signal_time"] + 60_000, "o": 102.5, "h": 103.2, "l": 102.2, "c": 103.0}
        engine.process_bar(next_bar, next_bar["t"] + 60_000, micro, mtf)
        self.assertEqual(engine.snapshot()["campaign"]["open_positions"], 2)

    def test_persistent_liquidity_level_stays_fixed_until_swept_then_rolls_forward(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        t0 = 1_700_000_000_000
        bars = [
            {"t": t0, "o": 100.0, "h": 108.0, "l": 99.0, "c": 105.0, "v": 100},
            {"t": t0 + 300_000, "o": 105.0, "h": 109.0, "l": 104.0, "c": 108.0, "v": 100},
        ]
        first_base = {
            "prior_high": 110.0, "prior_high_time": t0,
            "prior_low": 95.0, "prior_low_time": t0,
        }
        first = mbie._update_persistent_liquidity(bars, "5m", first_base)
        self.assertEqual(first["buy_side"]["price"], 110.0)
        self.assertEqual(first["sell_side"]["price"], 95.0)

        # A new closer high forms, but the live 110 pool must not be overwritten.
        bars.append({"t": t0 + 600_000, "o": 108.0, "h": 109.5, "l": 106.0, "c": 109.0, "v": 100})
        closer_base = {
            "prior_high": 109.5, "prior_high_time": t0 + 600_000,
            "swing_high": 109.5, "pivot_high_time": t0 + 600_000,
            "prior_low": 95.0, "prior_low_time": t0,
        }
        unchanged = mbie._update_persistent_liquidity(bars, "5m", closer_base)
        self.assertEqual(unchanged["buy_side"]["price"], 110.0)

        # A wick through 110 that closes back below consumes it as a sweep.
        bars.append({"t": t0 + 900_000, "o": 109.0, "h": 111.0, "l": 108.5, "c": 109.2, "v": 100})
        swept = mbie._update_persistent_liquidity(bars, "5m", {
            "prior_high": 111.0, "prior_high_time": t0 + 900_000,
            "prior_low": 95.0, "prior_low_time": t0,
        })
        self.assertIsNone(swept["buy_side"])
        self.assertEqual(swept["last_cleared"]["buy_side"]["event"], "SWEPT")

        # Only on the following 5m candle can the next untouched level activate.
        # The swept 110 price is deliberately presented again under an alternate
        # source label/time; it must not reactivate as if it were fresh liquidity.
        bars.append({"t": t0 + 1_200_000, "o": 109.2, "h": 110.0, "l": 108.8, "c": 109.5, "v": 100})
        next_pool = mbie._update_persistent_liquidity(bars, "5m", {
            "equal_high": 110.0, "equal_high_time": t0 + 600_000,
            "swing_high": 112.0, "pivot_high_time": t0 + 1_200_000,
            "prior_low": 95.0, "prior_low_time": t0,
        })
        self.assertEqual(next_pool["buy_side"]["price"], 112.0)

    def test_persistent_liquidity_state_is_separate_for_each_timeframe(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        t0 = 1_700_000_000_000
        five_min = [{"t": t0, "o": 100, "h": 105, "l": 95, "c": 101, "v": 100}]
        fifteen_min = [{"t": t0, "o": 100, "h": 106, "l": 94, "c": 101, "v": 100}]
        mbie._update_persistent_liquidity(five_min, "5m", {
            "prior_high": 110, "prior_high_time": t0 - 300_000,
            "prior_low": 90, "prior_low_time": t0 - 300_000,
        })
        mbie._update_persistent_liquidity(fifteen_min, "15m", {
            "prior_high": 120, "prior_high_time": t0 - 900_000,
            "prior_low": 80, "prior_low_time": t0 - 900_000,
        })
        five_min.append({"t": t0 + 300_000, "o": 101, "h": 111, "l": 99, "c": 109, "v": 100})
        mbie._update_persistent_liquidity(five_min, "5m", {
            "prior_high": 111, "prior_high_time": t0 + 300_000,
            "prior_low": 90, "prior_low_time": t0 - 300_000,
        })
        fifteen_snapshot = mbie._liquidity_snapshot(mbie._liquidity_trackers["15m"], "15m", 101)
        self.assertEqual(fifteen_snapshot["buy_side"]["price"], 120)
        self.assertEqual(fifteen_snapshot["sell_side"]["price"], 80)

    def test_orchestrator_runs_four_engines_and_excludes_incomplete_daily_candle(self):
        bars = self.minute_bars(80)
        future_daily = [{
            "t": bars[-1]["t"], "o": 100, "h": 120, "l": 90, "c": 118, "v": 10000,
        }]
        engine = ASTFourEngineOrchestrator("BTC")
        result = engine.snapshot({"1m": bars, "1d": future_daily}, "BTC")
        self.assertTrue(result["replay"]["lookahead_safe"])
        self.assertEqual(result["asset"], "BTC")
        self.assertFalse(result["higher_timeframes"]["1d"]["ready"])
        self.assertIn("market_intelligence", result)
        self.assertIn("entry_engine", result)
        self.assertIn("campaign", result)
        self.assertEqual(set(result["engine_contract"]), {"MBIE", "Multi-Timeframe Engine", "Entry Engine", "Campaign Engine"})


    def test_campaign_rejects_a_stop_on_the_wrong_side_of_entry(self):
        engine = ASTCampaignEngine("BTC")
        candidate = {
            "kind": "MAIN", "side": "BUY", "signal_time": 1_700_000_000_000,
            "signal_price": 100.0, "sweep_price": 102.0, "atr": 1.0,
            "liquidity_targets": [{"price": 108.0, "timeframe": "5m", "label": "SWING HIGH"}],
            "score": 90.0,
        }
        decision = engine.queue_candidate(candidate)
        self.assertFalse(decision["accepted"])
        self.assertIn("wrong side", decision["reason"])
        self.assertIsNone(engine.snapshot()["pending"])

    def test_campaign_cancels_pending_fill_when_mtf_direction_is_no_longer_confirmed(self):
        engine = ASTCampaignEngine("BTC")
        candidate = {
            "kind": "MAIN", "side": "BUY", "signal_time": 1_700_000_000_000,
            "signal_price": 100.0, "sweep_price": 98.0, "atr": 1.0,
            "liquidity_targets": [{"price": 106.0, "timeframe": "5m", "label": "SWING HIGH"}],
            "score": 90.0,
        }
        self.assertTrue(engine.queue_candidate(candidate)["accepted"])
        next_bar = {
            "t": candidate["signal_time"] + 60_000,
            "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5,
        }
        engine.process_bar(
            next_bar, next_bar["t"] + 60_000, {"atr": 1.0},
            {"status": "CONFLICT", "side": None},
        )
        state = engine.snapshot()
        self.assertIsNone(state["pending"])
        self.assertEqual(state["campaign"]["open_positions"], 0)
        self.assertIn("MTF direction", engine.last_rejection)

    def test_orchestrator_loads_asset_specific_contract_value_settings(self):
        bars = self.minute_bars(80)
        settings = {
            "AST_CAMPAIGN_ACCOUNT_EQUITY": "10000",
            "AST_CAMPAIGN_BTC_VALUE_PER_PRICE_UNIT": "2",
            "AST_CAMPAIGN_GOLD_VALUE_PER_PRICE_UNIT": "100",
        }
        with patch.dict(os.environ, settings):
            engine = ASTFourEngineOrchestrator()
            engine.snapshot({"1m": bars}, "BTC")
            self.assertEqual(engine.campaign_engine.contract_value_per_price_unit, 2.0)
            engine.snapshot({"1m": bars}, "GOLD")
            self.assertEqual(engine.campaign_engine.contract_value_per_price_unit, 100.0)

    def test_nearest_liquidity_uses_active_unconsumed_pools_only(self):
        frames = {
            "1h": {"persistent_liquidity": {"buy_side": {"price": 110, "label": "LOCKED HIGH"}, "sell_side": {"price": 90, "label": "LOCKED LOW"}}},
            "5m": {"persistent_liquidity": {"buy_side": {"price": 105, "label": "CLOSER HIGH"}, "sell_side": None}},
        }
        nearest_buy = ASTMarketBehaviourIntelligenceEngine._nearest_htf_liquidity(frames, "BUY", 100)
        nearest_sell = ASTMarketBehaviourIntelligenceEngine._nearest_htf_liquidity(frames, "SELL", 100)
        self.assertEqual(nearest_buy["price"], 105)
        self.assertEqual(nearest_sell["price"], 90)

    def test_mbie_exposes_adx_phase_and_honest_auction_coverage(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        bars = self.minute_bars(100)
        replay_frame = mbie.analyze_timeframe(bars, "1m", include_auction=False)
        self.assertIsNotNone(replay_frame["momentum"]["adx"])
        self.assertIn("market_phase", replay_frame)
        self.assertEqual(replay_frame["auction_profile"]["status"], "DEFERRED_DURING_REPLAY")
        dashboard_frame = mbie.analyze_timeframe(bars, "1m")
        self.assertEqual(dashboard_frame["auction_profile"]["status"], "PROXY")
        self.assertIn("typical-price", dashboard_frame["auction_profile"]["basis"])


if __name__ == "__main__":
    unittest.main()
