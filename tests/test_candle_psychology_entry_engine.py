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
SUPPORT = {
    "type": "SUPPORT",
    "price": 100.0,
    "zone_low": 99.8,
    "zone_high": 100.2,
    "half_width": 0.2,
    "strength_score": 80,
}


def bullish_setup():
    level = SUPPORT.copy()
    event_start = END_TIME - 9 * 60_000
    five = [
        candle(event_start - (19 - index) * 5 * 60_000, 100.65, 100.95, 100.35, 100.70)
        for index in range(19)
    ]
    # Abnormal 5m selling extension sweeps support and closes back above it.
    five.append(candle(event_start, 100.60, 100.80, 99.20, 100.55, 220.0))

    event_close = event_start + 5 * 60_000
    rows = [
        candle(event_close - (11 - index) * 60_000, 100.45, 100.75, 100.30, 100.60)
        for index in range(11)
    ]
    # Four closed 1m bars after the 5m event; the last one breaks the micro swing high.
    rows.extend([
        candle(event_close, 100.55, 100.80, 100.45, 100.65),
        candle(event_close + 60_000, 100.65, 100.90, 100.55, 100.72),
        candle(event_close + 120_000, 100.72, 100.85, 100.62, 100.68),
        candle(event_close + 180_000, 100.75, 101.25, 100.70, 101.20),
    ])
    return level, {"1m": rows, "5m": five}, 101.20


def bearish_setup():
    level = {
        "type": "RESISTANCE",
        "price": 100.0,
        "zone_low": 99.8,
        "zone_high": 100.2,
        "half_width": 0.2,
        "strength_score": 80,
    }
    event_start = END_TIME - 9 * 60_000
    five = [
        candle(event_start - (19 - index) * 5 * 60_000, 99.55, 99.85, 99.25, 99.50)
        for index in range(19)
    ]
    # Abnormal 5m buying extension sweeps resistance and closes back below it.
    five.append(candle(event_start, 99.60, 100.80, 99.25, 99.45, 220.0))

    event_close = event_start + 5 * 60_000
    rows = [
        candle(event_close - (11 - index) * 60_000, 99.55, 99.70, 99.25, 99.40)
        for index in range(11)
    ]
    # Four closed 1m bars after the 5m event; the last one breaks the micro swing low.
    rows.extend([
        candle(event_close, 99.45, 99.65, 99.10, 99.30),
        candle(event_close + 60_000, 99.30, 99.50, 99.05, 99.20),
        candle(event_close + 120_000, 99.20, 99.40, 99.10, 99.25),
        candle(event_close + 180_000, 99.15, 99.20, 98.70, 98.80),
    ])
    return level, {"1m": rows, "5m": five}, 98.80


class CandlePsychologyEntryEngineTests(unittest.TestCase):
    def test_candle_metrics_report_body_wicks_and_close_position(self):
        row = candle(60_000, 10.0, 12.0, 8.0, 11.0)
        result = candle_metrics(row, atr=2.0)
        self.assertEqual(result["body_pct"], 25.0)
        self.assertEqual(result["upper_wick_pct"], 25.0)
        self.assertEqual(result["lower_wick_pct"], 50.0)
        self.assertEqual(result["close_position"], 0.75)
        self.assertEqual(result["relative_strength"], 0.5)

    def test_hammer_and_bullish_engulfing_are_detected(self):
        rows = [
            candle(60_000, 100.4, 100.5, 100.0, 100.1),
            candle(120_000, 100.0, 100.3, 98.8, 100.2),
        ]
        self.assertIn("HAMMER", candle_patterns(rows, 1)["patterns"])
        engulfing = [
            candle(60_000, 100.8, 101.0, 99.8, 100.0),
            candle(120_000, 99.9, 101.2, 99.7, 101.1),
        ]
        self.assertIn("BULLISH_ENGULFING", candle_patterns(engulfing, 1)["patterns"])

    def test_inside_and_outside_bars_are_detected(self):
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

    def test_support_sweep_reclaim_gives_local_buy_without_htf_data(self):
        level, data, price = bullish_setup()
        original = dict(level)
        result = evaluate_level(level, [level], data, price, as_of_ms=END_TIME)
        self.assertEqual(result["action"], "BUY")
        self.assertEqual(result["reason"], "PANIC_CONFIRMED")
        self.assertEqual(result["approach"], "FROM_ABOVE")
        self.assertIn("PANIC_SWEEP_RECLAIM_BULLISH", result["patterns"])
        self.assertGreaterEqual(result["risk_reward"], 2.0)
        self.assertLess(result["stop_loss"], result["entry"])
        self.assertGreater(result["take_profit"], result["entry"])
        self.assertEqual(level, original)

    def test_resistance_sweep_reclaim_gives_local_sell_without_htf_data(self):
        level, data, price = bearish_setup()
        result = evaluate_level(level, [level], data, price, as_of_ms=END_TIME)
        self.assertEqual(result["action"], "SELL")
        self.assertEqual(result["reason"], "PANIC_CONFIRMED")
        self.assertEqual(result["approach"], "FROM_BELOW")
        self.assertIn("PANIC_SWEEP_RECLAIM_BEARISH", result["patterns"])
        self.assertGreaterEqual(result["risk_reward"], 2.0)
        self.assertGreater(result["stop_loss"], result["entry"])
        self.assertLess(result["take_profit"], result["entry"])

    def test_level_touch_without_rejection_stays_waiting(self):
        level, data, price = bullish_setup()
        event = data["5m"][-1]
        data["5m"][-1] = candle(event["t"], 100.4, 100.55, 99.9, 100.0, event["v"])
        result = evaluate_level(level, [level], data, price, as_of_ms=END_TIME)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "WAITING_FOR_PANIC_URGENCY_SWEEP_RECLAIM")

    def test_support_approached_from_below_does_not_give_buy(self):
        level, data, _price = bullish_setup()
        timestamp = data["1m"][-6]["t"]
        data["1m"][-6] = candle(timestamp, 99.6, 99.7, 99.4, 99.5)
        timestamp = data["1m"][-5]["t"]
        data["1m"][-5] = candle(timestamp, 99.6, 100.1, 99.4, 99.5)
        result = evaluate_level(level, [level], data, 101.20, as_of_ms=END_TIME)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "APPROACH_SIDE_MISMATCH")

    def test_decisively_broken_support_cancels_setup(self):
        level, data, _price = bullish_setup()
        data["1m"][-2] = candle(END_TIME - 120_000, 99.7, 99.75, 99.3, 99.4)
        data["1m"][-1] = candle(END_TIME - 60_000, 99.4, 99.55, 99.1, 99.2)
        result = evaluate_level(level, [level], data, 99.2, as_of_ms=END_TIME)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "SUPPORT_BROKEN")

    def test_open_one_minute_candle_is_ignored(self):
        level, data, price = bullish_setup()
        base = evaluate_level(level, [level], data, price, as_of_ms=END_TIME)
        data["1m"].append(candle(END_TIME, 100.5, 110.0, 100.4, 109.0))
        with_open = evaluate_level(level, [level], data, price, as_of_ms=END_TIME)
        self.assertEqual(with_open["action"], base["action"])
        self.assertEqual(with_open["signal_time"], base["signal_time"])

    def test_price_far_from_existing_level_stays_waiting(self):
        level, data, _price = bullish_setup()
        result = evaluate_level(level, [level], data, 120.0, as_of_ms=END_TIME)
        self.assertEqual(result["action"], "WAITING")
        self.assertEqual(result["reason"], "PRICE_NOT_NEAR_LEVEL")

    def test_same_active_level_is_not_triggered_twice(self):
        level, data, price = bullish_setup()
        engine = CandlePsychologyEntryEngine("BTC")
        first = engine.evaluate([level], data, price, as_of_ms=END_TIME)
        second = engine.evaluate([level], data, price, as_of_ms=END_TIME + 60_000)
        self.assertEqual(first[level_key(level)]["action"], "BUY")
        self.assertEqual(second[level_key(level)]["action"], "BUY")
        self.assertEqual(len(engine._triggered), 1)

    def test_signal_lock_clears_when_level_lifecycle_ends(self):
        level, data, price = bullish_setup()
        engine = CandlePsychologyEntryEngine("BTC")
        engine.evaluate([level], data, price, as_of_ms=END_TIME)
        result = engine.evaluate([], data, price, as_of_ms=END_TIME + 60_000)
        self.assertEqual(result, {})
        self.assertEqual(engine._triggered, {})

    def test_live_and_historical_replay_share_same_local_evaluator(self):
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

    def test_volume_absorption_requires_reliable_ohlcv_volume(self):
        level = SUPPORT.copy()
        rows = [
            candle((index + 1) * 60_000, 100.0, 100.4, 99.2, 100.3, 100.0)
            for index in range(14)
        ]
        rows.append(candle(15 * 60_000, 100.0, 100.4, 99.2, 100.3, 220.0))
        result = candle_patterns(rows, len(rows) - 1, level)
        self.assertTrue(result["volume_reliable"])
        self.assertIn("REPEATED_BULLISH_REJECTION", result["patterns"])
        self.assertIn("VOLUME_ABSORPTION_BULLISH_PROXY", result["patterns"])


if __name__ == "__main__":
    unittest.main()
