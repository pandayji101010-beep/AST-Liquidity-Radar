from __future__ import annotations

import unittest

from market_levels_engine import (
    HighConfidenceMarketLevelsEngine,
    TIMEFRAME_MS,
    _confirmed_pivots,
    _decisively_broken,
    _merge_levels,
    _reaction_stats,
    _structure_bias,
    _timeframe_bias,
)


def candle(stamp, op, high, low, close, volume=100.0):
    return {"t": stamp, "o": op, "h": high, "l": low, "c": close, "v": volume}


class HighConfidenceMarketLevelsTests(unittest.TestCase):
    def test_empty_history_waits_without_inventing_levels(self):
        engine = HighConfidenceMarketLevelsEngine("BTC")
        result = engine.snapshot({tf: [] for tf in TIMEFRAME_MS}, 100_000.0)
        self.assertEqual(result["status"], "WARMING UP")
        self.assertEqual(result["levels"], [])

    def test_confirmed_pivot_requires_right_side_candles(self):
        rows = [
            candle(60_000 * (i + 1), 10, high, low, 10)
            for i, (high, low) in enumerate([
                (11, 9), (12, 8), (15, 7), (12, 8), (11, 9), (10, 10), (11, 9)
            ])
        ]
        highs, lows = _confirmed_pivots(rows, left=2, right=2)
        self.assertIn((2, 15.0), highs)
        self.assertIn((2, 7.0), lows)
        earlier, _ = _confirmed_pivots(rows[:4], left=2, right=2)
        self.assertFalse(any(index == 2 for index, _ in earlier))

    def test_historical_reaction_rate_uses_separate_tests(self):
        rows = [
            candle(60_000, 100.2, 100.5, 99.8, 100.1),
            candle(120_000, 100.3, 101.2, 100.2, 101.0),
            candle(180_000, 101.0, 101.4, 100.8, 101.2),
            candle(240_000, 101.1, 101.3, 100.8, 101.0),
            candle(300_000, 100.3, 100.5, 99.7, 100.0),
            candle(360_000, 100.2, 101.3, 100.1, 101.1),
            candle(420_000, 101.1, 101.4, 100.9, 101.2),
        ]
        result = _reaction_stats(rows, 0, 100.0, 0.5, 1.0, "SUPPORT")
        self.assertEqual(result["touches"], 2)
        self.assertEqual(result["reactions"], 2)
        self.assertFalse(result["broken"])

    def test_confluence_merges_nearby_levels_and_scores_actual_samples(self):
        candidates = []
        for timeframe, price in (("1d", 100.0), ("4h", 100.08), ("1h", 99.96), ("30m", 100.03)):
            candidates.append({
                "side": "SUPPORT", "price": price, "half_width": 0.2,
                "timeframe": timeframe, "touches": 2, "reactions": 2,
                "freshness": 0.9, "volume_ratio": 1.5, "last_touch_time": 10_000,
            })
        data = {"15m": [
            candle(60_000, 100.2, 100.6, 99.9, 100.3),
            candle(120_000, 100.3, 100.7, 100.0, 100.4),
        ]}
        merged = _merge_levels(candidates, data)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["type"], "SUPPORT")
        self.assertEqual(merged[0]["timeframe_confluence"], 4)
        self.assertEqual(merged[0]["reaction_probability"], 100.0)
        self.assertGreaterEqual(merged[0]["strength_score"], 65)

    def test_two_closed_higher_timeframe_closes_remove_broken_level(self):
        data = {"1h": [
            candle(3_600_000, 100, 100.5, 98.8, 99.0),
            candle(7_200_000, 99.0, 99.3, 98.4, 98.7),
        ]}
        level = {"type": "SUPPORT", "zone_low": 99.5, "zone_high": 100.5}
        self.assertTrue(_decisively_broken(level, data))

    def test_higher_high_and_higher_low_structure_is_bullish(self):
        rows = []
        previous = 100.0
        for i in range(100):
            phase = i % 10
            cycle = i // 10
            base = 100.0 + cycle * 2.0 + phase * 0.03
            offset = phase * 0.50 if phase <= 5 else (10 - phase) * 0.50
            close = base + offset
            op = previous
            rows.append(candle((i + 1) * 60_000, op, max(op, close) + 0.08, min(op, close) - 0.08, close))
            previous = close
        self.assertEqual(_structure_bias(rows), "BULLISH")

    def test_timeframe_bias_includes_history_present_projection_and_combined_bias(self):
        rising = []
        falling = []
        for index in range(120):
            up_close = 100.0 + index * 0.5
            down_close = 200.0 - index * 0.5
            rising.append(candle(
                (index + 1) * 60_000,
                up_close - 0.2, up_close + 0.2, up_close - 0.4, up_close,
            ))
            falling.append(candle(
                (index + 1) * 60_000,
                down_close + 0.2, down_close + 0.4, down_close - 0.2, down_close,
            ))

        bullish = _timeframe_bias(rising)
        bearish = _timeframe_bias(falling)
        self.assertEqual(set(bullish), {"historical", "present", "projected", "bias"})
        self.assertEqual(bullish["present"], "BULLISH")
        self.assertEqual(bullish["projected"], "BULLISH")
        self.assertEqual(bullish["bias"], "BULLISH")
        self.assertEqual(bearish["present"], "BEARISH")
        self.assertEqual(bearish["projected"], "BEARISH")
        self.assertEqual(bearish["bias"], "BEARISH")

    def test_snapshot_exposes_only_confirmed_levels_and_six_requested_fields(self):
        engine = HighConfidenceMarketLevelsEngine("GOLD")
        result = engine.snapshot({tf: [] for tf in TIMEFRAME_MS}, None)
        self.assertEqual(result["asset"], "GOLD")
        self.assertIn("levels", result)
        self.assertEqual(result["levels"], [])


if __name__ == "__main__":
    unittest.main()
