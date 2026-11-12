import unittest

from main import AssetState, rebase_gold_reference_history


class GoldHistoryBasisTests(unittest.TestCase):
    @staticmethod
    def candle(stamp, close):
        return {
            "t": stamp,
            "o": close - 0.4,
            "h": close + 0.8,
            "l": close - 1.0,
            "c": close,
            "v": 25.0,
        }

    def test_gold_reference_history_is_shifted_once_to_live_binance_basis(self):
        state = AssetState(key="GOLD", symbol="XAUUSDT")
        state.gold_history_reference_price = 2351.0
        state.candles["1m"].extend([
            self.candle(1_700_000_000_000, 2350.0),
            self.candle(1_700_000_060_000, 2351.0),
        ])
        state.candles["5m"].append(self.candle(1_700_000_000_000, 2351.0))

        offset = rebase_gold_reference_history(state, 3985.75, 1_800_000_000_000)

        self.assertAlmostEqual(offset, 1634.75)
        self.assertTrue(state.gold_history_basis_applied)
        self.assertAlmostEqual(state.candles["1m"][-1]["c"], 3985.75)
        self.assertAlmostEqual(state.candles["1m"][-1]["h"], 3986.55)
        self.assertAlmostEqual(state.candles["1m"][-1]["l"], 3984.75)
        self.assertEqual(state.candles["1m"][-1]["v"], 25.0)
        self.assertAlmostEqual(state.candles["5m"][-1]["c"], 3985.75)
        self.assertEqual(state.mark_price, 3985.75)
        self.assertEqual(state.mark_ts, 1_800_000_000_000)
        self.assertIn("Binance XAUUSDT", state.live_price_source)

        # A reconnect must not apply the basis offset a second time.
        second = rebase_gold_reference_history(state, 3990.0, 1_800_000_060_000)
        self.assertAlmostEqual(second, offset)
        self.assertAlmostEqual(state.candles["1m"][-1]["c"], 3985.75)

    def test_gold_rebase_does_not_override_native_vantage_candles(self):
        state = AssetState(key="GOLD", symbol="XAUUSD")
        state.broker_candle_feed = True
        state.gold_history_reference_price = 2351.0
        state.candles["1m"].append(self.candle(1_700_000_000_000, 2351.0))

        result = rebase_gold_reference_history(state, 3985.75)

        self.assertIsNone(result)
        self.assertFalse(state.gold_history_basis_applied)
        self.assertEqual(state.candles["1m"][-1]["c"], 2351.0)


if __name__ == "__main__":
    unittest.main()
