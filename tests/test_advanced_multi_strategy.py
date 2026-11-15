from __future__ import annotations

import random
import unittest

from advanced_multi_strategy import AdvancedMultiStrategyEngine, MIN_BARS, TIMEFRAME_ORDER


def bars(count: int = 900):
    rng = random.Random(7)
    rows = []
    price = 30_000.0
    for i in range(count):
        drift = 0.12 if (i // 120) % 2 == 0 else -0.12
        op = price
        close = max(100.0, price + drift + rng.uniform(-7.0, 7.0))
        high = max(op, close) + rng.uniform(0.5, 4.0)
        low = min(op, close) - rng.uniform(0.5, 4.0)
        rows.append({"t": i * 60_000, "o": op, "h": high, "l": low, "c": close, "v": 100 + rng.uniform(0, 100)})
        price = close
    return rows


class AdvancedMultiStrategyTests(unittest.TestCase):
    def setUp(self):
        self.engine = AdvancedMultiStrategyEngine("BTC")
        self.data = bars()

    def test_warmup_does_not_emit_signal(self):
        result = self.engine.evaluate(self.data[:MIN_BARS - 1])
        self.assertIsNone(result.side)
        self.assertEqual(result.status, "WARMING UP")

    def test_all_six_engines_are_reported(self):
        result = self.engine.evaluate(self.data)
        self.assertEqual(set(result.engines), {"trend", "liquidity", "breakout", "range", "momentum", "price_action"})

    def test_future_candles_do_not_change_past_evaluation(self):
        index = 600
        past_only = self.engine.evaluate(self.data[:index + 1])
        using_full_series = self.engine.evaluate(self.data, index)
        self.assertEqual(past_only.side, using_full_series.side)
        self.assertEqual(past_only.engines, using_full_series.engines)
        self.assertEqual(past_only.stop_anchor, using_full_series.stop_anchor)

    def test_same_candle_stop_and_target_assumes_stop_first(self):
        candle = [{"t": 60_000, "o": 100.0, "h": 103.0, "l": 98.0, "c": 101.0, "v": 10.0}]
        trade = self.engine._simulate_trade(candle, 0, "BUY", 100.0, 99.0, 0, "1m", "BTC")
        self.assertIsNotNone(trade)
        self.assertEqual(trade["result"], "LOSS")
        self.assertEqual(trade["exit_reason"], "SL_FIRST_SAME_CANDLE")
        self.assertEqual(trade["result_r"], -1.0)

    def test_live_plan_uses_two_r_target(self):
        plan = self.engine._plan("BUY", 100.0, 99.0, 123, "1m")
        self.assertEqual(plan["risk_reward"], 2.0)
        self.assertEqual(plan["take_profit"], 102.0)

    def test_backtest_is_repeatable_and_reports_required_metrics(self):
        first = self.engine.backtest(self.data, "1m")
        second = self.engine.backtest(self.data, "1m")
        self.assertEqual(first["trades"], second["trades"])
        for field in (
            "total_historical_trades", "total_winning_trades", "total_losing_trades",
            "win_rate_pct", "loss_rate_pct", "actual_risk_reward_ratio",
            "total_winning_r", "total_losing_r", "net_profit_loss_r",
            "maximum_drawdown_r", "maximum_consecutive_wins",
            "maximum_consecutive_losses", "profit_factor", "costs",
        ):
            self.assertIn(field, first)
        ids = [trade["trade_id"] for trade in first["trades"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_supported_timeframes_are_the_requested_seven(self):
        self.assertEqual(TIMEFRAME_ORDER, ("1m", "5m", "15m", "30m", "1h", "4h", "1d"))


if __name__ == "__main__":
    unittest.main()
