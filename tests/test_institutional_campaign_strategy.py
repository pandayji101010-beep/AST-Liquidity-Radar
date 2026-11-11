from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from institutional_campaign_strategy import ASTInstitutionalCampaignStrategy


class InstitutionalCampaignStrategyTests(unittest.TestCase):
    @staticmethod
    def daily_bars(count: int = 90):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(count):
            price = 100.0 + i * 0.25
            rows.append({
                "t": int((start + timedelta(days=i)).timestamp() * 1000),
                "o": price, "h": price + 1, "l": price - 1,
                "c": price + 0.25, "v": 1000 + i,
            })
        return rows

    @staticmethod
    def minute_bars(count: int = 80):
        start = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)
        rows = []
        for i in range(count):
            price = 100.0 + i * 0.01
            rows.append({
                "t": start + i * 60_000,
                "o": price, "h": price + 0.08, "l": price - 0.08,
                "c": price + 0.01, "v": 100,
            })
        return rows

    def test_calendar_frames_have_future_close_timestamp_until_period_ends(self):
        daily = self.daily_bars(90)
        monthly = ASTInstitutionalCampaignStrategy._aggregate_calendar(daily, "1M")
        weekly = ASTInstitutionalCampaignStrategy._aggregate_calendar(daily, "1w")
        self.assertGreater(len(monthly), 2)
        self.assertGreater(len(weekly), 10)
        january_end = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(monthly[0]["_close_t"], january_end)

    def test_htf_alignment_requires_15m_and_5m_to_agree(self):
        frames = {
            tf: {"ready": True, "side": "BUY"}
            for tf in ("1M", "1w", "1d", "4h", "1h", "30m", "15m", "5m")
        }
        aligned = ASTInstitutionalCampaignStrategy._alignment(frames)
        self.assertEqual(aligned["status"], "CONFIRMED")
        frames["5m"] = {"ready": True, "side": "SELL"}
        conflicted = ASTInstitutionalCampaignStrategy._alignment(frames)
        self.assertNotEqual(conflicted["status"], "CONFIRMED")
        self.assertIsNone(conflicted["side"])

    def test_liquidity_target_must_be_at_least_minimum_rr(self):
        frames = {
            "5m": {"ready": True, "prior_high": 104.0, "range_high": 105.0},
            "15m": {"ready": True, "swing_high": 107.0},
        }
        target, rr = ASTInstitutionalCampaignStrategy._target_for("BUY", 100.0, 98.0, frames)
        self.assertEqual(target, 104.0)
        self.assertEqual(rr, 2.0)

    def test_replay_contract_is_1m_and_marks_point_in_time_mode(self):
        engine = ASTInstitutionalCampaignStrategy()
        result = engine.snapshot({"1m": self.minute_bars()}, "BTC")
        self.assertEqual(result["asset"], "BTC")
        self.assertIn("execution_1m", result)
        self.assertTrue(result["replay"]["lookahead_safe"])
        self.assertEqual(result["replay"]["bars_processed"], len(self.minute_bars()))
        self.assertIn("profit_factor", result["performance"])
        self.assertIn("max_drawdown_pct_of_equity", result["performance"])

    def test_future_incomplete_daily_bar_is_not_used_for_current_intraday_context(self):
        engine = ASTInstitutionalCampaignStrategy()
        minutes = self.minute_bars(80)
        # A daily candle whose close is after the final 1m close must not be available.
        daily = [{
            "t": minutes[-1]["t"],
            "o": 100, "h": 110, "l": 90, "c": 109, "v": 5000,
        }]
        result = engine.snapshot({"1m": minutes, "1d": daily}, "BTC")
        self.assertFalse(result["higher_timeframes"]["1d"]["ready"])
        self.assertTrue(result["replay"]["lookahead_safe"])

    def test_flat_1m_range_is_classified_as_choppy(self):
        start = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)
        bars = []
        for i in range(40):
            bars.append({
                "t": start + i * 60_000,
                "o": 100.0, "h": 100.2, "l": 99.8, "c": 100.0, "v": 100,
            })
        frame = ASTInstitutionalCampaignStrategy._frame_snapshot(bars, "1m")
        self.assertTrue(frame["choppy"])
        self.assertAlmostEqual(frame["previous_period_high"], 100.2)
        self.assertAlmostEqual(frame["previous_period_low"], 99.8)

    def test_smt_is_marked_unavailable_not_invented(self):
        score = ASTInstitutionalCampaignStrategy._setup_score(
            {"side": "BUY"}, {"sweep_side": "BUY", "bos_side": "BUY",
            "displacement_side": "BUY", "volume_confirmed": True,
            "session_ok": True, "volatility_ok": True},
            "BUY", True, True,
        )
        self.assertIsNone(score["checks"]["smt_divergence"])
        self.assertEqual(score["available_points"], 90)


if __name__ == "__main__":
    unittest.main()
