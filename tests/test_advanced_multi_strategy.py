from __future__ import annotations

import unittest

from advanced_multi_strategy import (
    AdvancedMultiStrategyEngine, COMBINATIONS, MIN_BARS, TIMEFRAME_MS,
    TIMEFRAME_ORDER, _clean_candles, _confirmed_swings, _summary, _trend_state,
)


def candles(count, interval=60_000, drift=1.0):
    rows = []
    price = 100.0
    for i in range(count):
        op, close = price, price + drift
        rows.append({
            "t": (i + 1) * interval, "o": op, "h": max(op, close) + 0.15,
            "l": min(op, close) - 0.15, "c": close, "v": 100.0,
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

    def test_strong_rising_and_falling_trends(self):
        self.assertEqual(_trend_state(candles(200, drift=1.0))["direction"], "BULLISH")
        self.assertEqual(_trend_state(candles(200, drift=-0.5))["direction"], "BEARISH")

    def test_flat_market_is_not_directional(self):
        rows = []
        for i in range(200):
            close = 100 + (0.02 if i % 2 else -0.02)
            rows.append({"t": i * 60_000, "o": close, "h": 100.10, "l": 99.90, "c": close, "v": 100})
        state = _trend_state(rows)
        self.assertEqual(state["direction"], "FLAT")
        self.assertEqual(state["strength"], "WEAK")

    def test_three_required_combinations_exist(self):
        self.assertEqual(set(COMBINATIONS), {"A", "B", "C"})
        self.assertEqual(COMBINATIONS["A"]["higher"], ("4h", "1h"))
        self.assertEqual(COMBINATIONS["B"]["higher"], ("1h",))
        self.assertEqual(COMBINATIONS["C"]["higher"], ("15m",))
        self.assertEqual(TIMEFRAME_ORDER, ("1m", "5m", "15m", "30m", "1h", "4h", "1d"))

    def test_asof_excludes_unclosed_higher_timeframe_bar(self):
        data = {
            "1m": candles(5),
            "4h": [
                {"t": i * TIMEFRAME_MS["4h"], "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100.5 + i, "v": 1}
                for i in range(3)
            ],
        }
        opens = {tf: [int(x["t"]) for x in rows] for tf, rows in data.items()}
        before = self.engine._asof(data, opens, TIMEFRAME_MS["4h"] - 1)
        closed = self.engine._asof(data, opens, TIMEFRAME_MS["4h"])
        self.assertEqual(len(before["4h"]), 0)
        self.assertEqual(len(closed["4h"]), 1)
        self.assertEqual(len(closed["1m"]), 5)

    def test_swings_require_right_side_confirmation_bars(self):
        values = [(11, 9), (12, 8), (15, 7), (12, 8), (11, 9), (10, 10), (11, 9)]
        rows = [{"t": i * 60_000, "o": 10, "h": h, "l": low, "c": 10, "v": 1} for i, (h, low) in enumerate(values)]
        highs, _ = _confirmed_swings(rows, left=2, right=2)
        self.assertIn((2, 15), highs)
        earlier, _ = _confirmed_swings(rows[:4], left=2, right=2)
        self.assertFalse(any(index == 2 for index, _ in earlier))

    def test_same_candle_stop_and_target_counts_as_loss(self):
        bars = [{"t": 60_000, "o": 100, "h": 103, "l": 98, "c": 101, "v": 10}]
        trade = self.engine._simulate_trade(bars, 0, "BUY", 100, 99, 0, "A")
        self.assertEqual(trade["result"], "LOSS")
        self.assertEqual(trade["result_r"], -1.0)

    def test_take_profit_is_two_r(self):
        bars = [
            {"t": 60_000, "o": 100, "h": 100.5, "l": 99.5, "c": 100.2, "v": 10},
            {"t": 120_000, "o": 100.2, "h": 102.1, "l": 100.1, "c": 102, "v": 10},
        ]
        trade = self.engine._simulate_trade(bars, 0, "BUY", 100, 99, 0, "B")
        self.assertEqual(trade["result"], "WIN")
        self.assertEqual(trade["take_profit"], 102.0)
        self.assertEqual(trade["result_r"], 2.0)

    def test_summary_calculates_win_rate_rr_and_net_r(self):
        trades = [
            {"result": "WIN", "result_r": 2, "net_result_r": 2},
            {"result": "WIN", "result_r": 2, "net_result_r": 2},
            {"result": "LOSS", "result_r": -1, "net_result_r": -1},
        ]
        result = _summary(trades, 100)
        self.assertEqual(result["total_trades"], 3)
        self.assertAlmostEqual(result["win_rate_pct"], 66.67, places=2)
        self.assertEqual(result["actual_average_rr"], 2.0)
        self.assertEqual(result["gross_net_r"], 3.0)
        self.assertEqual(result["net_profit_loss_r"], 3.0)
        self.assertEqual(result["break_even_trades"], 0)

    def test_backtest_ids_are_limited_to_combinations(self):
        self.assertEqual(self.engine.historical_trades("A")["combination"], "A")
        self.assertEqual(self.engine.historical_trades("1m")["status"], "INSUFFICIENT DATA")

    def test_empty_data_returns_insufficient_status(self):
        result = self.engine._evaluate_combination({tf: [] for tf in TIMEFRAME_ORDER}, "A")
        self.assertEqual(result["status"], "INSUFFICIENT DATA")
        self.assertIsNone(result["side"])

    def test_candle_cleanup_keeps_latest_duplicate_timestamp(self):
        rows = candles(3)
        rows.append({**rows[-1], "h": rows[-1]["h"] + 0.1, "c": rows[-1]["c"] + 0.1})
        cleaned = _clean_candles(rows)
        self.assertEqual(len(cleaned), 3)
        self.assertAlmostEqual(cleaned[-1]["c"], rows[-1]["c"] + 0.1)


if __name__ == "__main__":
    unittest.main()
