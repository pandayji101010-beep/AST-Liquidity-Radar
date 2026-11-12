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
        self.assertIn("scalping_liquidity", summary)
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

    def test_scalping_liquidity_accepts_micro_pools_inside_60_to_999_pip_band(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        t0 = 1_700_000_000_000
        highs = [1110, 1120, 1300, 1250, 1200, 1150, 1100, 1050, 1040]
        lows = [890, 880, 700, 740, 780, 820, 860, 900, 910]
        bars = [
            {"t": t0 + i * 60_000, "o": 1000, "h": highs[i], "l": lows[i], "c": 1000, "v": 100}
            for i in range(len(highs))
        ]
        candidates = mbie._scalping_liquidity_candidates(
            bars, "1m", {"atr": 20.0}, 1000.0, pip_size=1.0,
        )
        self.assertEqual(candidates["BUY_SIDE"][0]["price"], 1300.0)
        self.assertEqual(candidates["SELL_SIDE"][0]["price"], 700.0)
        self.assertEqual(candidates["BUY_SIDE"][0]["distance_pips"], 300.0)
        self.assertEqual(candidates["SELL_SIDE"][0]["distance_pips"], 300.0)
        self.assertEqual(mbie._pip_size_for_asset("BTC"), 1.0)
        self.assertEqual(mbie._pip_size_for_asset("GOLD"), 0.01)

    def test_scalping_candidates_accept_700_pips_but_reject_1000_plus(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        t0 = 1_700_000_000_000
        highs = [1110, 1120, 1700, 1250, 1200, 1150, 1100, 1050, 1040]
        lows = [890, 880, 300, 740, 780, 820, 860, 900, 910]
        bars = [
            {"t": t0 + i * 60_000, "o": 1000, "h": highs[i], "l": lows[i], "c": 1000, "v": 100}
            for i in range(len(highs))
        ]
        candidates = mbie._scalping_liquidity_candidates(
            bars, "1m", {"atr": 20.0}, 1000.0,
            pip_size=1.0, min_pips=60.0, max_pips=999.0,
        )
        self.assertEqual(candidates["BUY_SIDE"][0]["price"], 1700.0)
        self.assertEqual(candidates["SELL_SIDE"][0]["price"], 300.0)
        self.assertEqual(candidates["BUY_SIDE"][0]["distance_pips"], 700.0)
        self.assertEqual(candidates["SELL_SIDE"][0]["distance_pips"], 700.0)
        self.assertEqual(mbie.SCALP_MAX_DISTANCE_PIPS, 999.0)
        self.assertEqual(mbie.SCALP_MIN_DISTANCE_PIPS, 60.0)

    def test_scalping_candidates_reject_levels_at_1000_pips_and_above(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        t0 = 1_700_000_000_000
        highs = [3110, 3120, 4010, 3250, 3200, 3150, 3100, 3050, 3040]
        lows = [2890, 2880, 1990, 2750, 2800, 2850, 2900, 2950, 2960]
        bars = [
            {"t": t0 + i * 60_000, "o": 3000, "h": highs[i], "l": lows[i], "c": 3000, "v": 100}
            for i in range(len(highs))
        ]
        candidates = mbie._scalping_liquidity_candidates(
            bars, "1m", {"atr": 20.0}, 3000.0,
            pip_size=1.0, min_pips=60.0, max_pips=999.0,
        )
        self.assertEqual(candidates["BUY_SIDE"], [])
        self.assertEqual(candidates["SELL_SIDE"], [])

    def test_scalping_pool_has_an_independent_lock_and_clears_on_its_own_timeframe_sweep(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        t0 = 1_700_000_000_000
        highs = [1110, 1120, 1300, 1250, 1200, 1150, 1100, 1050, 1040]
        lows = [890, 880, 700, 740, 780, 820, 860, 900, 910]
        bars = [
            {"t": t0 + i * 60_000, "o": 1000, "h": highs[i], "l": lows[i], "c": 1000, "v": 100}
            for i in range(len(highs))
        ]
        # Seed the swing layer separately; a scalp sweep must not consume it.
        swing = mbie._update_persistent_liquidity(bars, "5m", {
            "prior_high": 1600.0, "prior_high_time": t0 - 60_000,
            "prior_low": 400.0, "prior_low_time": t0 - 60_000,
        })
        self.assertEqual(swing["buy_side"]["price"], 1600.0)
        builder = lambda rows, frame, close: mbie._scalping_liquidity_candidates(
            rows, "5m", frame, close, pip_size=1.0,
        )
        seeded = mbie._update_persistent_liquidity(
            bars, "5m", {}, candidate_builder=builder, state_key="5m:SCALP",
        )
        self.assertEqual(seeded["buy_side"]["price"], 1300.0)
        self.assertEqual(seeded["sell_side"]["price"], 700.0)
        self.assertIn("5m:SCALP", mbie._liquidity_trackers)
        self.assertIn("5m", mbie._liquidity_trackers)

        bars.append({
            "t": t0 + len(bars) * 60_000, "o": 1280, "h": 1310,
            "l": 990, "c": 1290, "v": 100,
        })
        swept = mbie._update_persistent_liquidity(
            bars, "5m", {}, candidate_builder=builder, state_key="5m:SCALP",
        )
        self.assertIsNone(swept["buy_side"])
        self.assertEqual(swept["last_cleared"]["buy_side"]["event"], "SWEPT")
        self.assertIsNotNone(mbie._liquidity_trackers["5m"]["active"]["BUY_SIDE"])

    def test_scalping_candidates_exclude_prices_already_used_by_swing_liquidity(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        t0 = 1_700_000_000_000
        highs = [1110, 1120, 1300, 1250, 1200, 1150, 1100, 1050, 1040]
        lows = [890, 880, 700, 740, 780, 820, 860, 900, 910]
        bars = [
            {"t": t0 + i * 60_000, "o": 1000, "h": highs[i], "l": lows[i], "c": 1000, "v": 100}
            for i in range(len(highs))
        ]
        candidates = mbie._scalping_liquidity_candidates(
            bars, "1m", {"atr": 20.0}, 1000.0, pip_size=1.0,
            excluded_prices={"BUY_SIDE": [1300.0], "SELL_SIDE": [700.0]},
        )
        self.assertTrue(all(item["price"] != 1300.0 for item in candidates["BUY_SIDE"]))
        self.assertTrue(all(item["price"] != 700.0 for item in candidates["SELL_SIDE"]))

    def test_scalping_pool_expires_only_after_it_moves_beyond_999_pips(self):
        mbie = ASTMarketBehaviourIntelligenceEngine()
        t0 = 1_700_000_000_000
        highs = [1110, 1120, 1300, 1250, 1200, 1150, 1100, 1050, 1040]
        lows = [890, 880, 700, 740, 780, 820, 860, 900, 910]
        bars = [
            {"t": t0 + i * 60_000, "o": 1000, "h": highs[i], "l": lows[i], "c": 1000, "v": 100}
            for i in range(len(highs))
        ]
        builder = lambda rows, frame, close: mbie._scalping_liquidity_candidates(
            rows, "5m", frame, close, pip_size=1.0,
        )
        seeded = mbie._update_persistent_liquidity(
            bars, "5m", {}, candidate_builder=builder, state_key="5m:SCALP",
            max_active_distance_pips=999, pip_size=1.0,
        )
        self.assertEqual(seeded["buy_side"]["price"], 1300.0)

        # 510 pips away is still inside the permitted 60–999 band.
        bars.append({
            "t": t0 + len(bars) * 60_000,
            "o": 800.0, "h": 850.0, "l": 770.0, "c": 790.0, "v": 100,
        })
        still_locked = mbie._update_persistent_liquidity(
            bars, "5m", {}, candidate_builder=builder, state_key="5m:SCALP",
            max_active_distance_pips=999, pip_size=1.0,
        )
        self.assertEqual(still_locked["buy_side"]["price"], 1300.0)
        self.assertEqual(still_locked["buy_side"]["distance_points"], 510.0)
        self.assertIsNone(still_locked["last_cleared"]["buy_side"])

        # More than 999 pips away expires the scalp pool, leaving the swing layer to cover distant liquidity.
        bars.append({
            "t": t0 + len(bars) * 60_000,
            "o": 290.0, "h": 300.0, "l": 280.0, "c": 290.0, "v": 100,
        })
        expired = mbie._update_persistent_liquidity(
            bars, "5m", {}, candidate_builder=builder, state_key="5m:SCALP",
            max_active_distance_pips=999, pip_size=1.0,
        )
        self.assertIsNone(expired["buy_side"])
        self.assertEqual(
            expired["last_cleared"]["buy_side"]["event"],
            "EXPIRED_OUTSIDE_SCALP_WINDOW",
        )
        self.assertEqual(expired["last_cleared"]["buy_side"]["distance_pips"], 1010.0)

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
