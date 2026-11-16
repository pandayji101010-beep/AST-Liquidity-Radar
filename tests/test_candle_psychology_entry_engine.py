from __future__ import annotations

import unittest

from candle_psychology_entry_engine import (
    CandlePsychologyEntryEngine,
    backtest_trades,
    candle_metrics,
    candle_patterns,
    evaluate_level,
    level_key,
)


def candle(stamp, op, high, low, close, volume=100.0):
    return {"t": stamp, "o": op, "h": high, "l": low, "c": close, "v": volume}


END_TIME = 1_700_000_000_000


def rising_rows(count, interval, start=90.0, step=0.25, end_time=END_TIME):
    rows = []
    first_stamp = end_time - count * interval
    for index in range(count):
        close = start + index * step
        rows.append(candle(
            first_stamp + index * interval,
            close - 0.08,
            close + 0.12,
            close - 0.15,
            close,
        ))
    return rows


def bullish_setup():
    one_hour = rising_rows(60, 3_600_000, 80.0, 0.5)
    fifteen = rising_rows(60, 900_000, 90.0, 0.2)
    five_start = END_TIME - 40 * 300_000
    five = [
        candle(five_start + index * 300_000, 100.0, 100.2, 99.8, 100.02)
        for index in range(39)
    ]
    # Closed 5m support rejection + reclaim / liquidity sweep.
    five.append(candle(END_TIME - 300_000, 100.0, 100.5, 98.8, 100.4))
    one_start = END_TIME - 200 * 60_000
    one = [
        candle(one_start + index * 60_000, 100.0, 100.25, 99.8, 100.05)
        for index in range(199)
    ]
    # The final closed 1m candle breaks the preceding five highs.
    one.append(candle(END_TIME - 60_000, 100.7, 102.0, 100.5, 101.8))
    level = {
        "type": "SUPPORT",
        "price": 100.0,
        "zone_low": 99.5,
        "zone_high": 100.5,
        "half_width": 0.5,
        "strength_score": 80,
    }
    data = {"1h": one_hour, "15m": fifteen, "5m": five, "1m": one}
    return level, data, 101.8


class CandlePsychologyEntryEngineTests(unittest.TestCase):
    def test_metrics_report_body_wicks_close_position_and_relative_strength(self):
        row = candle(60_000, 10.0, 12.0, 8.0, 11.0)
        result = candle_metrics(row, atr=2.0)
        self.assertEqual(result["body_pct"], 25.0)
        self.assertEqual(result["upper_wick_pct"], 25.0)
        self.assertEqual(result["lower_wick_pct"], 50.0)
        self.assertEqual(result["close_position"], 0.75)
        self.assertEqual(result["relative_strength"], 0.5)

    def test_pattern_detector_identifies_hammer_and_engulfing(self):
        rows = [
            candle(60_000, 100.4, 100.5, 100.0, 100.1),
            candle(120_000, 100.0, 100.3, 98.8, 100.2),
        ]
        hammer = candle_patterns(rows, 1)
        self.assertIn("HAMMER", hammer["patterns"])
        self.assertIn("BULLISH_REJECTION", hammer["patterns"])

        engulfing_rows = [
            candle(60_000, 100.8, 101.0, 99.8, 100.0),
            candle(120_000, 99.9, 101.2, 99.7, 101.1),
        ]
        self.assertIn("BULLISH_ENGULFING", candle_patterns(engulfing_rows, 1)["patterns"])

    def test_inside_and_outside_bars_are_labelled(self):
        inside = [
            candle(60_000, 10, 12, 8, 11),
            candle(120_000, 10.5, 11.5, 8.5, 11),
        ]
        outside = [
            candle(60_000, 10, 12, 8, 11),
            candle(120_000, 11, 13, 7, 12),
        ]
        self.assertIn("INSIDE_BAR", candle_patterns(inside, 1)["patterns"])
        self.assertIn("OUTSIDE_BAR", candle_patterns(outside, 1)["patterns"])

    def test_entry_requires_reaction_reclaim_mtf_alignment_and_1m_bos(self):
        level, data, price = bullish_setup()
        result = evaluate_level(level, [level], data, price)
        self.assertEqual(result["action"], "BUY")
        self.assertEqual(result["reason"], "CONFIRMED")
        self.assertGreaterEqual(result["risk_reward"], 2.0)
        self.assertLess(result["stop_loss"], result["entry"])
        self.assertGreater(result["take_profit"], result["entry"])
        self.assertIn("HAMMER", result["patterns"])
        self.assertEqual(result["approach"], "FROM_ABOVE")

    def test_touch_without_candle_confirmation_stays_waiting(self):
        level, data, price = bullish_setup()
        data["5m"][-1] = candle(39 * 300_000, 100.0, 100.1, 99.9, 100.02)
        result = evaluate_level(level, [level], data, price)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "5M_REACTION_OR_RECLAIM_MISSING")

    def test_conflicting_15m_direction_blocks_entry(self):
        level, data, price = bullish_setup()
        data["15m"] = rising_rows(60, 900_000, 160.0, -0.2)
        result = evaluate_level(level, [level], data, price)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "TIMEFRAME_CONFLICT_OR_NOT_ALIGNED")

    def test_level_break_before_entry_cancels_setup(self):
        level, data, price = bullish_setup()
        data["5m"][-2] = candle(38 * 300_000, 99.0, 99.2, 98.5, 98.8)
        data["5m"][-1] = candle(39 * 300_000, 98.8, 99.0, 98.0, 98.4)
        result = evaluate_level(level, [level], data, 98.4)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "LEVEL_BROKEN")

    def test_live_and_replay_use_the_same_evaluator_and_signal_time(self):
        level, data, price = bullish_setup()
        live = evaluate_level(level, [level], data, price, as_of_ms=END_TIME)
        replay = backtest_trades(
            [[level]],
            [{"time": data["1m"][-1]["t"], "candles": data, "price": price}],
        )
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["action"], live["action"])
        self.assertEqual(replay[0]["signal_time"], live["signal_time"])
        self.assertEqual(replay[0]["entry"], live["entry"])

    def test_same_active_level_is_not_triggered_twice(self):
        level, data, price = bullish_setup()
        engine = CandlePsychologyEntryEngine("BTC")
        first = engine.evaluate([level], data, price)
        second = engine.evaluate([level], data, price)
        self.assertEqual(first[level_key(level)]["action"], "BUY")
        self.assertEqual(second[level_key(level)]["action"], "BUY")
        self.assertEqual(len(engine._triggered), 1)


    def test_bearish_rejection_shooting_star_and_sell_confirmation(self):
        level = {
            "type": "RESISTANCE", "price": 100.0,
            "zone_low": 99.5, "zone_high": 100.5, "half_width": 0.5,
        }
        one_hour = [
            candle(END_TIME - 60 * 3_600_000 + i * 3_600_000,
                   128.08 - i * 0.5, 128.15 - i * 0.5,
                   127.85 - i * 0.5, 128.0 - i * 0.5)
            for i in range(60)
        ]
        # 15m bearish context reaches and reacts at the resistance zone.
        fifteen = [
            candle(END_TIME - 60 * 900_000 + i * 900_000,
                   (111.3 - i * 0.2) + 0.08,
                   (111.3 - i * 0.2) + 0.15,
                   (111.3 - i * 0.2) - 0.05,
                   111.3 - i * 0.2)
            for i in range(60)
        ]
        five_start = END_TIME - 40 * 300_000
        five = [
            candle(five_start + i * 300_000, 100.0, 100.2, 99.8, 100.02)
            for i in range(39)
        ]
        five.append(candle(END_TIME - 300_000, 100.0, 101.2, 99.5, 99.6))
        one_start = END_TIME - 200 * 60_000
        one = [
            candle(one_start + i * 60_000, 100.0, 100.2, 99.8, 100.05)
            for i in range(199)
        ]
        one.append(candle(END_TIME - 60_000, 99.8, 100.0, 98.0, 98.3))
        data = {"1h": one_hour, "15m": fifteen, "5m": five, "1m": one}

        patterns = candle_patterns(five, len(five) - 1, level)["patterns"]
        self.assertIn("SHOOTING_STAR", patterns)
        self.assertIn("BEARISH_REJECTION", patterns)
        self.assertIn("LIQUIDITY_SWEEP_RECLAIM_BEARISH", patterns)

        result = evaluate_level(level, [level], data, 98.3)
        self.assertEqual(result["action"], "SELL")
        self.assertEqual(result["reason"], "CONFIRMED")
        self.assertGreaterEqual(result["risk_reward"], 2.0)
        self.assertGreater(result["stop_loss"], result["entry"])
        self.assertLess(result["take_profit"], result["entry"])

    def test_volume_absorption_is_only_tagged_with_reliable_ohlcv_volume(self):
        level = {
            "type": "SUPPORT", "price": 100.0,
            "zone_low": 99.5, "zone_high": 100.5,
        }
        rows = [
            candle((i + 1) * 60_000, 100.0, 100.4, 99.2, 100.3, 100.0)
            for i in range(14)
        ]
        rows.append(candle(15 * 60_000, 100.0, 100.4, 99.2, 100.3, 220.0))
        result = candle_patterns(rows, len(rows) - 1, level)
        self.assertTrue(result["volume_reliable"])
        self.assertIn("REPEATED_BULLISH_REJECTION", result["patterns"])
        self.assertIn("VOLUME_ABSORPTION_BULLISH_PROXY", result["patterns"])


    def test_open_candles_are_excluded_from_entry_decisions(self):
        level, data, price = bullish_setup()
        live = evaluate_level(level, [level], data, price, as_of_ms=END_TIME)
        self.assertEqual(live["action"], "BUY")

        # Append an unfinished 1m candle with a hypothetical even higher close.
        data["1m"].append(candle(END_TIME, 101.8, 110.0, 101.5, 109.0))
        replay = evaluate_level(level, [level], data, price, as_of_ms=END_TIME)
        self.assertEqual(replay["action"], "BUY")
        self.assertEqual(replay["signal_time"], live["signal_time"])

    def test_wrong_approach_side_blocks_a_support_buy(self):
        level, data, _price = bullish_setup()
        result = evaluate_level(level, [level], data, 99.0, as_of_ms=END_TIME)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "APPROACH_SIDE_MISMATCH")

    def test_15m_must_show_a_reaction_at_the_same_level(self):
        level, data, price = bullish_setup()
        for index in range(len(data["15m"]) - 12, len(data["15m"])):
            stamp = data["15m"][index]["t"]
            close = 105.0 + (index - (len(data["15m"]) - 12)) * 0.2
            data["15m"][index] = candle(stamp, close - 0.08, close + 0.12, close - 0.15, close)
        result = evaluate_level(level, [level], data, price, as_of_ms=END_TIME)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "15M_LEVEL_REACTION_MISSING")

    def test_price_far_from_level_does_not_activate_reaction_entry(self):
        level, data, _price = bullish_setup()
        result = evaluate_level(level, [level], data, 120.0)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "PRICE_NOT_NEAR_LEVEL")


if __name__ == "__main__":
    unittest.main()
