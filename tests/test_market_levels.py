from __future__ import annotations

import unittest

from market_levels import (
    HighConfidenceMarketLevelsEngine,
    _clean_rows,
    _level_is_broken,
    _merge_levels,
    _micro_bos,
)


def candidate(side, timeframe, price=100.0, samples=3, successes=3):
    return {
        "side": side,
        "price": price,
        "width": 0.25,
        "timeframe": timeframe,
        "samples": samples,
        "successes": successes,
        "freshness": 0.9,
        "volume_ratio": 1.5,
        "pivot_time": 1,
    }


def candle(stamp, op, high, low, close, volume=10):
    return {"t": stamp, "o": op, "h": high, "l": low, "c": close, "v": volume}


class HighConfidenceMarketLevelsTests(unittest.TestCase):
    def test_only_fully_closed_candles_are_used(self):
        rows = [
            candle(1_000, 100, 102, 99, 101),
            candle(2_000, 101, 103, 100, 102),
        ]
        result = _clean_rows(rows, "1m", now_ms=62_000)
        self.assertEqual([row["t"] for row in result], [1_000])
        
    def test_overlapping_multitimeframe_levels_merge(self):
        levels = _merge_levels([
            candidate("SUPPORT", "4h", 100.00),
            candidate("SUPPORT", "1h", 100.08),
            candidate("SUPPORT", "15m", 99.95),
        ])
        self.assertEqual(len(levels), 1)
        self.assertEqual(levels[0]["side"], "SUPPORT")
        self.assertEqual(set(levels[0]["timeframes"]), {"4h", "1h", "15m"})
        self.assertGreaterEqual(levels[0]["strength_score"], 65)

    def test_levels_without_higher_timeframe_confluence_are_rejected(self):
        levels = _merge_levels([
            candidate("RESISTANCE", "15m", 110.0),
            candidate("RESISTANCE", "30m", 110.05),
        ])
        self.assertEqual(levels, [])

    def test_low_historical_reaction_rate_is_rejected(self):
        levels = _merge_levels([
            candidate("SUPPORT", "4h", 100.0, samples=5, successes=2),
            candidate("SUPPORT", "1h", 100.05, samples=5, successes=2),
        ])
        self.assertEqual(levels, [])

    def test_decisive_two_candle_break_invalidates_support(self):
        rows = {
            "15m": [
                candle(1, 100, 100.2, 98.8, 98.9),
                candle(2, 98.9, 99.1, 98.5, 98.7),
            ]
        }
        self.assertTrue(_level_is_broken(100.0, 0.25, "SUPPORT", rows, 98.7))

    def test_wick_breach_alone_does_not_invalidate_level(self):
        rows = {
            "15m": [
                candle(1, 100.2, 100.4, 99.0, 100.1),
                candle(2, 100.1, 100.3, 98.8, 100.0),
            ]
        }
        self.assertFalse(_level_is_broken(100.0, 0.25, "SUPPORT", rows, 100.0))

    def test_micro_bos_requires_a_closed_break(self):
        rows = [
            candle(1, 100, 100.4, 99.8, 100.2),
            candle(2, 100.2, 100.5, 100.0, 100.3),
            candle(3, 100.3, 100.4, 99.9, 100.1),
            candle(4, 100.1, 101.0, 100.0, 100.8),
        ]
        self.assertTrue(_micro_bos(rows, "SUPPORT", bars=3))
        rows[-1]["c"] = 100.2
        self.assertFalse(_micro_bos(rows, "SUPPORT", bars=3))

    def test_empty_market_warms_up_without_inventing_levels(self):
        engine = HighConfidenceMarketLevelsEngine("BTC")
        data = {tf: [] for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d")}
        result = engine.snapshot(data, 100.0, now_ms=10_000_000)
        self.assertEqual(result["status"], "WARMING UP")
        self.assertEqual(result["levels"], [])

    def test_display_payload_contains_no_probability_claim(self):
        engine = HighConfidenceMarketLevelsEngine("GOLD")
        data = {tf: [] for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d")}
        result = engine.snapshot(data, 100.0, now_ms=10_000_000)
        self.assertEqual(set(result), {"asset", "price", "status", "levels"})


if __name__ == "__main__":
    unittest.main()
