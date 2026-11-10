import unittest

from ast_strategy import ASTStructureStrategy


class ASTStructureStrategyTests(unittest.TestCase):
    @staticmethod
    def make_bars(count=160):
        rows = []
        for index in range(count):
            center = 100.0 + 0.04 * index + 4.0 * __import__("math").sin(index / 5.0)
            open_price = center - 0.25
            close_price = center + 0.25
            rows.append({
                "t": index * 60_000,
                "o": open_price,
                "h": max(open_price, close_price) + 0.8,
                "l": min(open_price, close_price) - 0.8,
                "c": close_price,
                "v": 10.0,
            })
        return rows

    def test_snapshot_matches_dashboard_structure_contract(self):
        candles = {tf: self.make_bars() for tf in ASTStructureStrategy.TIMEFRAMES}
        result = ASTStructureStrategy().snapshot(candles)

        self.assertEqual(set(result["timeframes"]), set(ASTStructureStrategy.TIMEFRAMES))
        self.assertIsInstance(result["direction_lock"], dict)
        self.assertIn("side", result["direction_lock"])
        self.assertTrue(result["setups"])

        for tf, state in result["timeframes"].items():
            self.assertTrue(state["ready"], tf)
            self.assertIn("major_trend", state)
            self.assertIn("minor_trend", state)
            self.assertIn("major_event", state)
            self.assertIn("minor_event", state)
            self.assertIn("setup", state)
            self.assertIsInstance(state["sweeps"], list)

        for event in result["events"]:
            self.assertTrue({"timeframe", "structure", "event", "side", "level"}.issubset(event))

        for zone in result["zones"]:
            self.assertTrue({"side", "asset_timeframe", "low", "high", "retests", "retest_stage", "status", "strength_atr"}.issubset(zone))

        for level in result["liquidity"]:
            self.assertTrue({"timeframe", "name", "price"}.issubset(level))

    def test_structure_break_must_match_sweep_direction_and_follow_displacement(self):
        bars = self.make_bars(30)
        sweeps = [{
            "bar_index": 20, "time": bars[20]["t"],
            "type": "LOWER_SWEEP", "side": "BUY", "level": 99.0,
        }]
        displacement = [False] * len(bars)
        displacement[22] = True

        matching_event = [{
            "structure": "MINOR", "event": "BOS", "side": "BUY",
            "level": 105.0, "time": bars[24]["t"], "bar_index": 24,
            "displacement": False,
        }]
        valid = ASTStructureStrategy._setup_for_timeframe(
            "5m", bars, sweeps, displacement, matching_event, [],
            "BULLISH", "BULLISH", "BUY",
        )
        self.assertEqual(valid["status"], "ENTRY CONFIRMED")
        self.assertTrue(valid["checks"]["structure_break"])
        self.assertEqual(valid["score"], 75)

        opposite_event = [dict(matching_event[0], side="SELL")]
        invalid = ASTStructureStrategy._setup_for_timeframe(
            "5m", bars, sweeps, displacement, opposite_event, [],
            "BULLISH", "BULLISH", "BUY",
        )
        self.assertEqual(invalid["status"], "SETUP FORMING")
        self.assertFalse(invalid["checks"]["structure_break"])

        early_event = [dict(matching_event[0], bar_index=21)]
        early = ASTStructureStrategy._setup_for_timeframe(
            "5m", bars, sweeps, displacement, early_event, [],
            "BULLISH", "BULLISH", "BUY",
        )
        self.assertFalse(early["checks"]["structure_break"])

    def test_origin_zone_retests_count_visits_not_each_candle(self):
        bars = [
            {"t": i * 60_000, "o": 10.0, "h": 10.5, "l": 9.5, "c": 10.2, "v": 1.0}
            for i in range(20)
        ]
        bars[9] = {"t": 9 * 60_000, "o": 10.0, "h": 10.4, "l": 8.5, "c": 9.0, "v": 1.0}
        bars[13] = {"t": 13 * 60_000, "o": 9.8, "h": 10.2, "l": 9.0, "c": 10.0, "v": 1.0}
        bars[14] = {"t": 14 * 60_000, "o": 9.9, "h": 10.1, "l": 9.2, "c": 10.0, "v": 1.0}
        bars[15] = {"t": 15 * 60_000, "o": 10.4, "h": 10.8, "l": 10.2, "c": 10.6, "v": 1.0}
        bars[16] = {"t": 16 * 60_000, "o": 10.3, "h": 10.5, "l": 9.4, "c": 10.0, "v": 1.0}

        displacement = [False] * len(bars)
        displacement[12] = True
        event = [{
            "timeframe": "5m", "structure": "MAJOR", "event": "BOS",
            "side": "BUY", "level": 11.0, "time": bars[12]["t"],
            "bar_index": 12, "displacement": False,
        }]
        displacement = [False] * len(bars)
        displacement[11] = True
        zones = ASTStructureStrategy._build_zones("5m", bars, event, [1.0] * len(bars), displacement)
        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0]["retests"], 2)
        self.assertEqual(zones[0]["retest_stage"], "2 RETEST")
        self.assertEqual(zones[0]["status"], "ACTIVE")

    def test_direction_locks_are_per_engine_instance(self):
        first = ASTStructureStrategy()
        second = ASTStructureStrategy()
        timeframes = {
            "1d": {"major_trend": "NEUTRAL"},
            "4h": {"major_trend": "NEUTRAL"},
        }
        reversal = [{
            "structure": "MAJOR", "event": "CHOCH", "side": "SELL",
            "timeframe": "4h", "time": 100, "displacement": True,
        }]
        first_lock = first._update_direction_lock(timeframes, reversal)
        second_lock = second._update_direction_lock(
            {"1d": {"major_trend": "BULLISH"}}, []
        )
        self.assertEqual(first_lock["side"], "SELL")
        self.assertEqual(second_lock["side"], "BUY")


if __name__ == "__main__":
    unittest.main()
