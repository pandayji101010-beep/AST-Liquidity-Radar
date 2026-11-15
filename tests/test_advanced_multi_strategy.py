from __future__ import annotations

import unittest

from advanced_multi_strategy import (
    AdvancedMultiStrategyEngine,
    COMBINATIONS,
    MIN_BARS,
    TIMEFRAME_MS,
    TIMEFRAME_ORDER,
    _clean_candles,
    _confirmed_swings,
    _summary,
    _trend_state,
)


def candles(count: int, interval: int = 60_000, drift: float = 1.0):
    rows = []
    price = 100.0
    for index in range(count):
        op = price
        close = price + drift
        rows.append({
            "t": index * interval,
            "o": op,
            "h": max(op, close) + 0.15,
            "l": min(op, close) - 0.15,
            "c": close,
            "v": 100.0,
        })
        price = close
    return rows


class AST915EMATests(unittest.TestCase):
    def setUp(self):
        self.engine = AdvancedMultiStrategyEngine("BTC")

    def test_warmup_reports_insufficient_history(self):
        state = _trend_state(candles(MIN_BARS - 1))
        self.assertFalse(state["ready"])
        self.assertEqual(state["direction"], "INSUFFICIENT DATA")

    def test_strong_rising_trend_is_bullish(self):
        state = _trend_state(candles(200, drift=1.0))
        self.assertTrue(state["ready"])
        self.assertEqual(state["direction"], "BULLISH")
        self.assertIn(state["strength"], ("STRONG", "MODERATE"))

    def test_strong_falling_trend_is_bearish(self):
        state = _trend_state(candles(200, drift=-0.5))
        self.assertTrue(state["ready"])
        self.assertEqual(state["direction"], "BEARISH")

    def test_flat_market_is_not_a_directional_trend(self):
        rows = []
        for index in range(200):
            price = 100 + (0.02 if index % 2 else -0.02)
            rows.append({"t": index * 60_000, "o": price, "h": 100.10, "l": 99.90, "c": price, "v": 100})
        state = _trend_state(rows)
        self.assertEqual(state["direction"], "FLAT")
        self.assertEqual(state["strength"], "WEAK")

    def test_all_three_requested_combinations_exist(self):
        self.assertEqual(set(COMBINATIONS), {"A", "B", "C"})
        self.assertEqual(COMBINATIONS["A"]["higher"], ("4h", "1h"))
        self.assertEqual(COMBINATIONS["B"]["higher"], ("1h",))
        self.assertEqual(COMBINATIONS["C"]["higher"], ("15m",))
        self.assertEqual(TIMEFRAME_ORDER, ("1m", "5m", "15m", "30m", "1h", "4h", "1d"))

    def test_asof_excludes_incomplete_higher_timeframe_bar(self):
        data = {
            "1m": candles(5, TIMEFRAME_MS["1m"]),
            "4h": candles(3, TIMEFRAME_MS["4h"]),
        }
        opens = {tf: [int(row["t"]) for row in rows] for tf, rows in data.items()}
        just_before_close = self.engine._asof(data, opens, TIMEFRAME_MS["4h"] - 1)
        at_close = self.engine._asof(data, opens, TIMEFRAME_MS["4h"])
        self.assertEqual(len(just_before_close["4h"]), 0)
        self.assertEqual(len(at_close["4h"]), 1)
        self.assertEqual(len(at_close["1m"]), 5)

    def test_confirmed_swings_do_not_use_unclosed_right_side_bars(self):
        rows = [
            {"t": i * 60_000, "o": 10, "h": high, "l": low, "c": 10, "v": 1}
            for i, (high, low) in enumerate([
                (11, 9), (12, 8), (15, 7), (12, 8), (11, 9),
                (10, 10), (11, 9),
            ])
        ]
        highs, lows = _confirmed_swings(rows, left=2, right=2)
        self.assertTrue(any(index == 2 and value == 15 for index, value in highs))
        before_right_confirmation = _confirmed_swings(rows[:4], left=2, right=2)[0]
        self.assertFalse(any(index == 2 for index, _value in before_right_confirmation))

    def test_same_candle_stop_and_target_assumes_stop_first(self):
        bars = [{"t": 60_000, "o": 100.0, "h": 103.0, "l": 98.0, "c": 101.0, "v": 10.0}]
        trade = self.engine._simulate_trade(bars, 0, "BUY", 100.0, 99.0, 0, "A")
        self.assertIsNotNone(trade)
        self.assertEqual(trade["result"], "LOSS")
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertEqual(trade["result_r"], -1.0)

    def test_2r_target_is_used_for_winning_trade(self):
        bars = [
            {"t": 60_000, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.2, "v": 10.0},
            {"t": 120_000, "o": 100.2, "h": 102.1, "l": 100.1, "c": 102.0, "v": 10.0},
        ]
        trade = self.engine._simulate_trade(bars, 0, "BUY", 100.0, 99.0, 0, "B")
        self.assertEqual(trade["result"], "WIN")
        self.assertEqual(trade["take_profit"], 102.0)
        self.assertEqual(trade["result_r"], 2.0)

    def test_summary_reports_win_rate_rr_and_net_r(self):
        trades = [
            {"result": "WIN", "result_r": 2.0, "net_result_r": 2.0},
            {"result": "WIN", "result_r": 2.0, "net_result_r": 2.0},
            {"result": "LOSS", "result_r": -1.0, "net_result_r": -1.0},
        ]
        result = _summary(trades, 100)
        self.assertEqual(result["total_trades"], 3)
        self.assertAlmostEqual(result["win_rate_pct"], 66.67, places=2)
        self.assertEqual(result["actual_average_rr"], 2.0)
        self.assertEqual(result["gross_net_r"], 3.0)
        self.assertEqual(result["net_profit_loss_r"], 3.0)
        self.assertEqual(result["break_even_trades"], 0)

    def test_trade_log_route_accepts_only_a_b_c(self):
        self.assertEqual(self.engine.historical_trades("A")["combination"], "A")
        self.assertEqual(self.engine.historical_trades("1m")["status"], "INSUFFICIENT DATA")

    def test_live_and_historical_share_same_combination_evaluator(self):
        data = {tf: [] for tf in TIMEFRAME_ORDER}
        result = self.engine._evaluate_combination(data, "A")
        self.assertEqual(result["combination"], "A")
        self.assertIsNone(result["side"])
        self.assertEqual(result["status"], "INSUFFICIENT DATA")

    def test_clean_candles_sort_and_deduplicate_timestamp(self):
        rows = candles(3)
        rows.append({**rows[-1], "c": rows[-1]["c"] + 0.1})
        clean = _clean_candles(rows)
        self.assertEqual(len(clean), 3)
        self.assertAlmostEqual(clean[-1]["c"], rows[-1]["c"] + 0.1)


if __name__ == "__main__":
    unittest.main()
