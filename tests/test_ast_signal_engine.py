import unittest

from ast_signal_engine import (
    ASTOneMinuteSignalEngine,
    MIN_MOVE_POTENTIAL_PIPS,
    PIP_SIZE,
    TF_MS,
    _closed,
    _move_potential_pips,
    _setup,
)


def make_candle(stamp, close, step=10.0):
    return {
        "t": stamp,
        "o": close - step * 0.5,
        "h": close + step * 0.2,
        "l": close - step * 0.7,
        "c": close,
        "v": 100.0,
    }


def rising_series(timeframe, count=100, step=10.0):
    interval = TF_MS[timeframe]
    base = 1_700_000_000_000
    rows = [
        make_candle(base + i * interval, 20_000.0 + i * step, step)
        for i in range(count)
    ]
    previous = rows[-1]
    prior_close = previous["c"]
    # A confirmed one-minute continuation bar that clears the previous five-bar high.
    rows.append({
        "t": base + count * interval,
        "o": prior_close + step * 0.30,
        "h": prior_close + step * 1.35,
        "l": prior_close + step * 0.20,
        "c": prior_close + step * 1.20,
        "v": 140.0,
    })
    return rows


class ASTSignalEngineTests(unittest.TestCase):
    def test_micro_break_can_create_buy_setup(self):
        rows = []
        base = 1_700_000_000_000
        for i in range(30):
            rows.append({
                "t": base + i * 60_000,
                "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 10.0,
            })
        rows.append({
            "t": base + 30 * 60_000,
            "o": 101.0, "h": 104.0, "l": 100.5, "c": 103.5, "v": 20.0,
        })
        setup = _setup(rows)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["side"], "BUY")

    def test_low_volatility_does_not_pass_85_pip_filter(self):
        rows = []
        base = 1_700_000_000_000
        for i in range(40):
            rows.append({
                "t": base + i * 60_000,
                "o": 100.0, "h": 100.05, "l": 99.95, "c": 100.0, "v": 10.0,
            })
        self.assertLess(_move_potential_pips(rows, 1.0), MIN_MOVE_POTENTIAL_PIPS)

    def test_1m_engine_emits_signal_when_trend_and_move_space_align(self):
        frames = {tf: rising_series(tf) for tf in TF_MS}
        last_stamp = frames["1m"][-1]["t"]
        as_of = last_stamp + TF_MS["1m"]
        engine = ASTOneMinuteSignalEngine()
        result = engine.evaluate("BTC", frames, frames["1m"][-1]["c"], as_of_ms=as_of)
        self.assertEqual(result["action"], "BUY")
        self.assertGreaterEqual(result["move_potential_pips"], MIN_MOVE_POTENTIAL_PIPS)
        self.assertGreaterEqual(result["target_pips"], 90.0)
        self.assertIn(result["sl_pips"], (30, 50))

    def test_incomplete_one_minute_candle_is_not_used(self):
        base = 1_700_000_000_000
        rows = [{"t": base, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}]
        closed = _closed(rows, "1m", base + 59_999)
        self.assertEqual(closed, [])

    def test_gold_pip_convention_is_one_cent(self):
        self.assertEqual(PIP_SIZE["GOLD"], 0.01)


if __name__ == "__main__":
    unittest.main()
