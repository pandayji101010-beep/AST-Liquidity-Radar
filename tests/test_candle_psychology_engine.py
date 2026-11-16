from __future__ import annotations

import unittest

from candle_psychology_engine import (
    CandlePsychologyEntryEngine,
    TIMEFRAME_MS,
    _closed_frames,
    backtest_candle_entries,
    candle_psychology,
    evaluate_level_entry,
    level_key,
)


def candle(stamp, op, high, low, close, volume=100.0):
    return {"t": stamp, "o": op, "h": high, "l": low, "c": close, "v": volume}


def trending_rows(timeframe, direction, count=70, start=1_700_000_000_000):
    interval = TIMEFRAME_MS[timeframe]
    rows = []
    previous = 100.0
    for index in range(count):
        close = 100.0 + index * 0.2 if direction == "BULLISH" else 120.0 - index * 0.2
        op = previous
        rows.append(candle(start + index * interval, op, max(op, close) + 0.05,
                           min(op, close) - 0.05, close))
        previous = close
    return rows


class CandlePsychologyEntryTests(unittest.TestCase):
    def test_shape_percentages_and_pin_bar_are_reported(self):
        profile = candle_psychology(
            candle(1_700_000_000_000, 100.0, 102.0, 97.5, 101.0),
            None, 1.0, [100.0] * 12,
        )
        self.assertAlmostEqual(profile["body_pct"], 22.22, places=1)
        self.assertAlmostEqual(profile["lower_wick_pct"], 55.56, places=1)
        self.assertIn("BULLISH_PIN_BAR", profile["patterns"])
        self.assertGreater(profile["close_position_pct"], 75.0)

    def test_engulfing_inside_and_outside_patterns_are_detected(self):
        previous = candle(60_000, 101.0, 101.2, 99.8, 100.0)
        engulfing = candle(120_000, 99.8, 101.4, 99.6, 101.3)
        profile = candle_psychology(engulfing, previous, 1.0, [100.0] * 12)
        self.assertTrue(profile["bullish_engulfing"])
        inside = candle(180_000, 100.5, 101.0, 100.0, 100.7)
        inside_profile = candle_psychology(inside, engulfing, 1.0)
        self.assertIn("INSIDE_BAR", inside_profile["patterns"])
        outside = candle(240_000, 100.0, 102.0, 99.0, 101.0)
        outside_profile = candle_psychology(outside, inside, 1.0)
        self.assertIn("OUTSIDE_BAR", outside_profile["patterns"])

    def test_absorption_is_not_labelled_without_reliable_volume_history(self):
        current = candle(120_000, 100.0, 101.0, 97.0, 100.8, 500.0)
        without_history = candle_psychology(current, None, 1.0, [0.0] * 20)
        with_history = candle_psychology(current, None, 1.0, [100.0] * 20)
        self.assertNotIn("VOLUME_ABSORPTION", without_history["patterns"])
        self.assertIn("VOLUME_ABSORPTION", with_history["patterns"])

    def test_open_timeframe_candles_are_excluded(self):
        start = 1_700_000_000_000
        as_of = start + 60_000
        frames = {"1m": [
            candle(start, 100, 101, 99, 100.5),
            candle(start + 60_000, 100.5, 102, 100, 101.5),
        ]}
        closed = _closed_frames(frames, as_of)
        self.assertEqual(len(closed["1m"]), 1)
        self.assertEqual(closed["1m"][-1]["t"], start)

    def test_conflicting_one_hour_and_fifteen_minute_directions_wait(self):
        data = {
            "1h": trending_rows("1h", "BULLISH"),
            "15m": trending_rows("15m", "BEARISH"),
            "5m": trending_rows("5m", "BULLISH"),
            "1m": trending_rows("1m", "BULLISH"),
        }
        as_of = max(
            rows[-1]["t"] + TIMEFRAME_MS[tf]
            for tf, rows in data.items()
        )
        level = {"type": "SUPPORT", "price": 100.0, "zone_low": 99.8,
                 "zone_high": 100.2, "half_width": 0.2}
        result = evaluate_level_entry(level, data, 100.0, [level], as_of)
        self.assertEqual(result["status"], "WAITING")
        self.assertEqual(result["reason"], "HIGHER_TIMEFRAME_CONFLICT_OR_NEUTRAL")

    def test_backtest_requires_time_indexed_level_snapshots(self):
        data = {"1m": trending_rows("1m", "BULLISH")}
        self.assertEqual(backtest_candle_entries([], data), [])

    def test_level_key_is_stable_for_dashboard_matching(self):
        level = {"type": "SUPPORT", "price": 67123.45678901}
        self.assertEqual(level_key(level), "SUPPORT:67123.45678901")

    def test_engine_does_not_mutate_the_source_levels(self):
        data = {tf: [] for tf in TIMEFRAME_MS}
        level = {"type": "SUPPORT", "price": 100.0, "zone_low": 99.8,
                 "zone_high": 100.2, "half_width": 0.2, "strength_score": 88}
        original = dict(level)
        engine = CandlePsychologyEntryEngine("BTC")
        result = engine.snapshot(data, [level], 100.0, 1_800_000_000_000)
        self.assertEqual(level, original)
        self.assertEqual(result[level_key(level)]["status"], "WAITING")


if __name__ == "__main__":
    unittest.main()
