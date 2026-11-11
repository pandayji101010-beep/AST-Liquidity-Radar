import unittest

from ast_strategy import ASTStructureStrategy


def base_candles(count=25):
    return [
        {"t": index * 60_000, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0}
        for index in range(count)
    ]


class MomentumContinuationTests(unittest.TestCase):
    def setUp(self):
        self.engine = ASTStructureStrategy()

    def evaluate(self, bars, major="NEUTRAL", minor="NEUTRAL"):
        atr = self.engine._atr_values(bars)
        return self.engine._momentum_continuation_for_timeframe(
            "15m", bars, atr, major, minor
        )

    def test_bullish_range_break_and_retest_confirms_without_sweep(self):
        bars = base_candles()
        bars.append({"t": 25 * 60_000, "o": 100.5, "h": 104.2, "l": 100.4, "c": 104.0, "v": 2.0})
        bars.append({"t": 26 * 60_000, "o": 100.9, "h": 102.3, "l": 100.5, "c": 102.0, "v": 2.0})
        setup = self.evaluate(bars, "BULLISH", "BULLISH")
        self.assertEqual(setup["status"], "ENTRY CONFIRMED")
        self.assertEqual(setup["side"], "BUY")
        self.assertTrue(setup["checks"]["range_breakout"])
        self.assertTrue(setup["checks"]["momentum_displacement"])
        self.assertTrue(setup["checks"]["retest_touch"])
        self.assertTrue(setup["checks"]["retest_rejection"])

    def test_bearish_range_break_and_retest_confirms_without_sweep(self):
        bars = base_candles()
        bars.append({"t": 25 * 60_000, "o": 99.5, "h": 99.6, "l": 95.8, "c": 96.0, "v": 2.0})
        bars.append({"t": 26 * 60_000, "o": 99.1, "h": 99.5, "l": 97.7, "c": 98.0, "v": 2.0})
        setup = self.evaluate(bars, "BEARISH", "BEARISH")
        self.assertEqual(setup["status"], "ENTRY CONFIRMED")
        self.assertEqual(setup["side"], "SELL")
        self.assertTrue(setup["checks"]["retest_rejection"])

    def test_breakout_without_retest_is_watch_not_entry(self):
        bars = base_candles()
        bars.append({"t": 25 * 60_000, "o": 100.5, "h": 104.2, "l": 100.4, "c": 104.0, "v": 2.0})
        bars.append({"t": 26 * 60_000, "o": 104.0, "h": 105.0, "l": 103.5, "c": 104.7, "v": 2.0})
        setup = self.evaluate(bars)
        self.assertEqual(setup["status"], "RETEST PENDING")
        self.assertIsNone(setup["side"])
        self.assertEqual(setup["candidate_side"], "BUY")
        self.assertFalse(setup["checks"]["retest_touch"])

    def test_fresh_breakout_is_watch_not_entry(self):
        bars = base_candles()
        bars.append({"t": 25 * 60_000, "o": 100.5, "h": 104.2, "l": 100.4, "c": 104.0, "v": 2.0})
        setup = self.evaluate(bars)
        self.assertEqual(setup["status"], "BREAKOUT WATCH")
        self.assertIsNone(setup["side"])
        self.assertEqual(setup["candidate_side"], "BUY")

    def test_breakout_that_closes_back_through_boundary_is_invalidated(self):
        bars = base_candles()
        bars.append({"t": 25 * 60_000, "o": 100.5, "h": 104.2, "l": 100.4, "c": 104.0, "v": 2.0})
        bars.append({"t": 26 * 60_000, "o": 104.0, "h": 104.5, "l": 99.0, "c": 99.5, "v": 2.0})
        setup = self.evaluate(bars)
        self.assertEqual(setup["status"], "INVALIDATED")
        self.assertIsNone(setup["candidate_side"])

    def test_weak_candle_does_not_trigger_momentum_breakout(self):
        bars = base_candles()
        bars.append({"t": 25 * 60_000, "o": 100.0, "h": 101.1, "l": 99.9, "c": 100.8, "v": 2.0})
        setup = self.evaluate(bars)
        self.assertEqual(setup["status"], "WAITING")
        self.assertIsNone(setup["candidate_side"])


if __name__ == "__main__":
    unittest.main()
