import unittest

from abhay_patil_trap_strategy import AbhayPatilBlackBoxTrapStrategy


def candle(index, opened, high, low, close):
    return {
        "t": index * 60_000,
        "o": float(opened),
        "h": float(high),
        "l": float(low),
        "c": float(close),
        "v": 1.0,
    }


def bear_trap_history():
    rows = [candle(i, 100, 105, 101, 103) for i in range(41)]
    # The 20th candle is a pivot low. It becomes usable only at bar 40
    # after the full 20 candles on its right have closed.
    rows[20] = candle(20, 100, 102, 90, 101)
    rows[40] = candle(40, 100, 104, 98, 100)
    # Close under support arms the bear-trap alert.
    rows.append(candle(41, 91, 92, 88, 89))
    # Green candle reclaims support; high becomes BUY STOP, low becomes SL.
    rows.append(candle(42, 89, 94, 88, 92))
    # Later bar triggers the pending BUY STOP.
    rows.append(candle(43, 92, 95, 91, 94))
    # One 2R target is reached.
    rows.append(candle(44, 94, 106, 93, 105))
    return rows


def bull_trap_history():
    rows = [candle(i, 98, 100, 95, 97) for i in range(41)]
    # The 20th candle is a pivot high confirmed at bar 40.
    rows[20] = candle(20, 100, 110, 95, 99)
    rows[40] = candle(40, 98, 104, 96, 100)
    # Close above resistance arms the bull-trap alert.
    rows.append(candle(41, 109, 112, 108, 111))
    # Red candle rejects resistance; low becomes SELL STOP, high becomes SL.
    rows.append(candle(42, 111, 112, 107, 108))
    # Later bar triggers the SELL STOP.
    rows.append(candle(43, 108, 110, 106, 107))
    # Single 2R target is reached.
    rows.append(candle(44, 107, 108, 97, 98))
    return rows


class AbhayPatilBlackBoxTrapTests(unittest.TestCase):
    def setUp(self):
        self.engine = AbhayPatilBlackBoxTrapStrategy()

    def test_bear_trap_uses_support_close_break_green_reclaim_and_buy_stop(self):
        result = self.engine.replay_timeframe("1m", bear_trap_history())
        self.assertEqual(result["status"], "LAST TRADE CLOSED")
        self.assertEqual(result["history"]["traps_detected"], 1)
        self.assertEqual(result["history"]["orders_filled"], 1)
        self.assertEqual(result["history"]["wins"], 1)
        self.assertEqual(result["history"]["losses"], 0)
        self.assertEqual(result["history"]["win_rate_pct"], 100.0)
        self.assertEqual(result["last_trade"]["trap_type"], "BEAR TRAP")
        self.assertEqual(result["last_trade"]["side"], "BUY")
        self.assertEqual(result["last_trade"]["entry_trigger"], 94.0)
        self.assertEqual(result["last_trade"]["stop_loss"], 88.0)
        self.assertEqual(result["last_trade"]["take_profit"], 106.0)
        self.assertEqual(result["last_trade"]["outcome"], "TP HIT")
        self.assertEqual(result["last_trade"]["result_r"], 2.0)

    def test_bull_trap_uses_resistance_close_break_red_reclaim_and_sell_stop(self):
        result = self.engine.replay_timeframe("5m", bull_trap_history())
        self.assertEqual(result["history"]["traps_detected"], 1)
        self.assertEqual(result["history"]["orders_filled"], 1)
        self.assertEqual(result["history"]["wins"], 1)
        self.assertEqual(result["last_trade"]["trap_type"], "BULL TRAP")
        self.assertEqual(result["last_trade"]["side"], "SELL")
        self.assertEqual(result["last_trade"]["entry_trigger"], 107.0)
        self.assertEqual(result["last_trade"]["stop_loss"], 112.0)
        self.assertEqual(result["last_trade"]["take_profit"], 97.0)
        self.assertEqual(result["last_trade"]["outcome"], "TP HIT")
        self.assertEqual(result["last_trade"]["result_r"], 2.0)

    def test_reclaim_bar_does_not_fill_its_own_stop_order(self):
        rows = bear_trap_history()[:43]  # ends on the reclaim candle at index 42
        result = self.engine.replay_timeframe("1m", rows)
        self.assertEqual(result["status"], "ENTRY PENDING")
        self.assertEqual(result["history"]["traps_detected"], 1)
        self.assertEqual(result["history"]["orders_filled"], 0)
        self.assertEqual(result["history"]["pending_orders"], 1)
        self.assertEqual(result["current_signal"]["trade_plan"]["order_type"], "BUY STOP")
        self.assertEqual(result["current_signal"]["trade_plan"]["entry_price"], 94.0)

    def test_pending_stop_order_has_no_time_expiry_in_source(self):
        rows = bear_trap_history()[:43]
        for index in range(43, 55):
            rows.append(candle(index, 90, 93, 89, 91))
        result = self.engine.replay_timeframe("1m", rows)
        self.assertEqual(result["status"], "ENTRY PENDING")
        self.assertEqual(result["history"]["pending_orders"], 1)
        self.assertEqual(result["history"]["orders_filled"], 0)

    def test_close_break_is_required_wick_only_break_does_not_arm_trap(self):
        rows = [candle(i, 100, 105, 101, 103) for i in range(41)]
        rows[20] = candle(20, 100, 102, 90, 101)
        rows[40] = candle(40, 100, 104, 98, 100)
        # Price wicks below support but closes above it: no Pine crossunder.
        rows.append(candle(41, 93, 95, 88, 92))
        rows.append(candle(42, 91, 94, 90, 93))
        result = self.engine.replay_timeframe("1m", rows)
        self.assertEqual(result["history"]["fake_breaks_detected"], 0)
        self.assertEqual(result["history"]["traps_detected"], 0)
        self.assertEqual(result["status"], "WAITING")

    def test_bear_trap_alert_persists_until_a_later_green_reclaim(self):
        rows = [candle(i, 100, 105, 101, 103) for i in range(41)]
        rows[20] = candle(20, 100, 102, 90, 101)
        rows[40] = candle(40, 100, 104, 98, 100)
        rows.append(candle(41, 91, 92, 88, 89))   # close below support
        rows.append(candle(42, 90, 92, 88, 89))   # still below: alert persists
        rows.append(candle(43, 89, 94, 88, 92))   # green reclaim
        result = self.engine.replay_timeframe("15m", rows)
        self.assertEqual(result["history"]["traps_detected"], 1)
        self.assertEqual(result["status"], "ENTRY PENDING")
        self.assertEqual(result["current_signal"]["signal_time"], rows[43]["t"])

    def test_stop_loss_is_counted_when_sl_and_tp_touch_same_bar(self):
        rows = bear_trap_history()[:44]
        rows.append(candle(44, 94, 106, 87, 100))  # both 2R TP and SL inside same candle
        result = self.engine.replay_timeframe("1m", rows)
        self.assertEqual(result["history"]["ambiguous_exit_bars"], 1)
        self.assertEqual(result["history"]["losses"], 1)
        self.assertEqual(result["last_trade"]["outcome"], "SL HIT")
        self.assertEqual(result["last_trade"]["result_r"], -1.0)

    def test_history_replay_reports_actual_bar_span_and_counts(self):
        rows = bear_trap_history()
        result = self.engine.replay_timeframe("4h", rows)
        self.assertEqual(result["bars_scanned"], len(rows))
        self.assertEqual(result["first_bar_time"], rows[0]["t"])
        self.assertEqual(result["last_bar_time"], rows[-1]["t"])
        self.assertEqual(result["history"]["bars_scanned"], len(rows))
        self.assertEqual(result["lookback"], 20)
        self.assertEqual(result["rr_ratio"], 2.0)

    def test_all_timeframe_snapshot_is_returned(self):
        candles = {tf: bear_trap_history() for tf in self.engine.TIMEFRAMES}
        result = self.engine.snapshot(candles)
        self.assertEqual(set(result["timeframes"]), set(self.engine.TIMEFRAMES))
        for tf in self.engine.TIMEFRAMES:
            self.assertEqual(result["timeframes"][tf]["timeframe"], tf)
            self.assertEqual(result["timeframes"][tf]["status"], "LAST TRADE CLOSED")


if __name__ == "__main__":
    unittest.main()
