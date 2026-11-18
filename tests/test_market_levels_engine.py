from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from market_levels_engine import (
    HighConfidenceMarketLevelsEngine,
    TIMEFRAME_MS,
    TIMEFRAME_ORDER,
    _candidate_levels,
    _confirmed_pivots,
    _decisively_broken,
    _panic_event_candidates,
    _merge_levels,
    _pivot_candidate,
    _resample_calendar_timeframes,
    _structure_bias,
    _timeframe_bias,
)


def candle(stamp, op, high, low, close, volume=100.0):
    return {"t": stamp, "o": op, "h": high, "l": low, "c": close, "v": volume}


def daily_candles(start: date, end_exclusive: date):
    rows = []
    day = start
    index = 0
    while day < end_exclusive:
        stamp = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
        close = 100.0 + (index % 15) * 0.1
        rows.append(candle(stamp, close - 0.05, close + 0.3, close - 0.3, close))
        day += timedelta(days=1)
        index += 1
    return rows


def panic_event_fixture():
    hour = 3_600_000
    five_minute = [
        candle(40 * hour - (35 - index) * 300_000, 100.6, 100.9, 100.3, 100.65, 100.0)
        for index in range(35)
    ]
    event_time = 40 * hour
    # Abnormal 5m selling candle sweeps the 100 support and reclaims it.
    five_minute.append(candle(event_time, 100.4, 100.8, 98.5, 100.6, 240.0))
    five_minute.extend([
        candle(event_time + 5 * 60_000, 100.8, 102.0, 100.7, 101.8, 130.0),
        candle(event_time + 10 * 60_000, 101.8, 102.3, 101.5, 102.0, 110.0),
        candle(event_time + 15 * 60_000, 102.0, 102.4, 101.8, 102.2, 105.0),
    ])

    event_close = event_time + 5 * 60_000
    one_minute = [
        candle(event_close - (11 - index) * 60_000, 100.5, 100.75, 100.4, 100.6, 10.0)
        for index in range(11)
    ]
    one_minute.extend([
        candle(event_close + index * 60_000, 100.5, 100.75 + index * 0.02, 100.4, 100.6, 10.0)
        for index in range(4)
    ])
    one_minute.append(candle(event_close + 4 * 60_000, 100.75, 101.3, 100.7, 101.2, 20.0))

    hourly = []
    for index in range(70):
        stamp = (index + 1) * hour
        if index == 10:
            hourly.append(candle(stamp, 103.0, 104.0, 100.0, 102.0, 500.0))
        else:
            hourly.append(candle(stamp, 110.0, 110.6, 109.2, 110.0, 100.0))

    return {"1h": hourly, "5m": five_minute, "1m": one_minute}


class HighConfidenceMarketLevelsTests(unittest.TestCase):
    def test_empty_history_waits_without_inventing_levels(self):
        engine = HighConfidenceMarketLevelsEngine("BTC")
        result = engine.snapshot({tf: [] for tf in TIMEFRAME_MS}, 100_000.0)
        self.assertEqual(result["status"], "WARMING UP")
        self.assertEqual(result["levels"], [])
        self.assertIn("1w", result["timeframe_data"])
        self.assertIn("1mo", result["timeframe_data"])
        self.assertIn("1y", result["timeframe_data"])

    def test_only_one_hour_and_higher_timeframes_create_levels(self):
        self.assertEqual(set(("1h", "4h", "1d", "1w", "1mo", "1y")), set(
            ("1h", "4h", "1d", "1w", "1mo", "1y")
        ))
        self.assertEqual(TIMEFRAME_ORDER[:6], ("1y", "1mo", "1w", "1d", "4h", "1h"))

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

    def test_calendar_resampling_creates_only_closed_week_month_and_year_bars(self):
        rows = daily_candles(date(2024, 1, 1), date(2024, 3, 1))
        now = int(datetime(2024, 3, 10, tzinfo=timezone.utc).timestamp() * 1000)
        derived = _resample_calendar_timeframes(rows, now)
        self.assertGreater(len(derived["1w"]), 6)
        self.assertEqual(len(derived["1mo"]), 2)
        self.assertEqual(len(derived["1y"]), 0)
        self.assertEqual(
            derived["1mo"][0]["t"],
            int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        )

    def test_yearly_bars_are_built_from_complete_calendar_years(self):
        rows = daily_candles(date(2023, 1, 1), date(2026, 1, 1))
        now = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
        derived = _resample_calendar_timeframes(rows, now)
        self.assertEqual(len(derived["1y"]), 3)
        self.assertEqual(
            derived["1y"][-1]["t"],
            int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        )

    def _strong_support_pivot(self):
        rows = [
            candle((i + 1) * 3_600_000, 105.0, 106.0, 104.0, 105.0)
            for i in range(5)
        ]
        # Clear swing low and then a fast upside displacement exceeding one ATR.
        rows.append(candle(6 * 3_600_000, 101.0, 102.0, 97.0, 100.0))
        rows.extend([
            candle(7 * 3_600_000, 100.0, 103.0, 99.0, 102.0),
            candle(8 * 3_600_000, 102.0, 107.0, 101.0, 106.0),
            candle(9 * 3_600_000, 106.0, 109.0, 105.0, 108.0),
            candle(10 * 3_600_000, 108.0, 110.0, 107.0, 109.0),
        ])
        return rows

    def test_strong_pivot_is_kept_when_it_has_not_been_retested(self):
        rows = self._strong_support_pivot()
        candidate = _pivot_candidate(rows, "1h", 5, "SUPPORT", 97.0)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["touch_state"], "UNTESTED")
        self.assertEqual(candidate["touches"], 0)
        self.assertGreaterEqual(candidate["impulse_atr"], 1.0)

    def test_first_recent_retest_is_kept_but_second_retest_consumes_level(self):
        rows = self._strong_support_pivot()
        rows.append(candle(11 * 3_600_000, 100.0, 101.0, 97.1, 99.5))
        first = _pivot_candidate(rows, "1h", 5, "SUPPORT", 97.0)
        self.assertIsNotNone(first)
        self.assertEqual(first["touch_state"], "FIRST_TOUCH")
        self.assertEqual(first["touches"], 1)

        rows.extend([
            candle(12 * 3_600_000, 100.0, 103.0, 100.0, 102.0),
            candle(13 * 3_600_000, 100.0, 101.0, 97.1, 99.5),
        ])
        second = _pivot_candidate(rows, "1h", 5, "SUPPORT", 97.0)
        self.assertIsNone(second)

    def test_confluence_merges_and_scores_major_unmitigated_levels(self):
        candidates = []
        for timeframe, price in (
            ("1y", 100.00), ("1mo", 100.08), ("1w", 99.96), ("1d", 100.03),
        ):
            candidates.append({
                "side": "SUPPORT",
                "price": price,
                "half_width": 0.2,
                "timeframe": timeframe,
                "touches": 0,
                "reactions": 0,
                "touch_state": "UNTESTED",
                "freshness": 0.9,
                "volume_ratio": 1.7,
                "impulse_atr": 2.4,
                "wick_ratio": 0.4,
                "liquidity_pool": True,
                "last_touch_time": 10_000,
                "origin_time": 9_000,
                "origin_kind": "SWING",
            })
        merged = _merge_levels(candidates, {})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["type"], "SUPPORT")
        self.assertEqual(merged[0]["source_timeframes"], ["1y", "1mo", "1w", "1d"])
        self.assertEqual(merged[0]["touch_state"], "UNTESTED")
        self.assertGreaterEqual(merged[0]["strength_score"], 80)

    def test_old_break_before_level_origin_does_not_invalidate_new_level(self):
        data = {"1h": [
            candle(1 * 3_600_000, 100, 100.2, 98.8, 99.0),
            candle(2 * 3_600_000, 99, 99.2, 98.5, 98.8),
            candle(3 * 3_600_000, 100, 100.5, 99.8, 100.1),
            candle(4 * 3_600_000, 100.1, 100.6, 99.9, 100.2),
        ]}
        level = {
            "type": "SUPPORT",
            "zone_low": 99.5,
            "zone_high": 100.5,
            "origin_time": 3 * 3_600_000,
        }
        self.assertFalse(_decisively_broken(level, data))
        data["1h"].extend([
            candle(5 * 3_600_000, 99.4, 99.5, 99.0, 99.2),
            candle(6 * 3_600_000, 99.2, 99.3, 98.8, 99.0),
        ])
        self.assertTrue(_decisively_broken(level, data))


    def test_plain_htf_swing_does_not_become_a_display_level_without_panic(self):
        rows = [
            candle((i + 1) * 3_600_000, 110.0, 110.6, 109.2, 110.0)
            for i in range(70)
        ]
        rows[10] = candle(11 * 3_600_000, 103.0, 104.0, 100.0, 102.0)
        data = {"1h": rows, "5m": [], "1m": []}
        self.assertEqual(_candidate_levels(data, []), [])

    def test_panic_event_requires_later_opposite_micro_shift(self):
        data = panic_event_fixture()
        events = _panic_event_candidates(data)
        self.assertTrue(events)
        self.assertEqual(events[-1]["side"], "SUPPORT")
        self.assertEqual(events[-1]["micro_shift_timeframe"], "1m")

        without_micro = {"5m": data["5m"], "1m": []}
        self.assertEqual(_panic_event_candidates(without_micro), [])

    def test_htf_support_level_is_kept_only_after_panic_sweep_and_micro_shift(self):
        data = panic_event_fixture()
        candidates = _candidate_levels(data, _panic_event_candidates(data))
        support = [
            item for item in candidates
            if item["side"] == "SUPPORT" and abs(item["price"] - 100.0) < 0.01
        ]
        self.assertTrue(support)
        self.assertTrue(support[0]["panic_confirmed"])
        self.assertEqual(support[0]["micro_shift_timeframe"], "1m")
        self.assertGreaterEqual(support[0]["panic_range_atr"], 1.35)

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
        rising, falling = [], []
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
        bullish, bearish = _timeframe_bias(rising), _timeframe_bias(falling)
        self.assertEqual(set(bullish), {"historical", "present", "projected", "bias"})
        self.assertEqual(bullish["present"], "BULLISH")
        self.assertEqual(bullish["projected"], "BULLISH")
        self.assertEqual(bullish["bias"], "BULLISH")
        self.assertEqual(bearish["present"], "BEARISH")
        self.assertEqual(bearish["projected"], "BEARISH")
        self.assertEqual(bearish["bias"], "BEARISH")


if __name__ == "__main__":
    unittest.main()
