import unittest

from ast_strategy import ASTStructureStrategy


class TrapSetupMappingTests(unittest.TestCase):
    def setUp(self):
        self.engine = ASTStructureStrategy()

    def make_setup(self, side="BUY", status="ENTRY CONFIRMED", plan=None):
        confirmed = status == "ENTRY CONFIRMED"
        return {
            "status": status,
            "side": side if confirmed else None,
            "candidate_side": side if not confirmed else None,
            "price": 100.0,
            "major_trend": "BULLISH" if side == "BUY" else "BEARISH",
            "minor_trend": "BULLISH" if side == "BUY" else "BEARISH",
            "sweep_level": 99.0 if side == "BUY" else 101.0,
            "trade_plan": plan,
            "checks": {
                "liquidity_sweep": True,
                "displacement": status in ("SETUP FORMING", "ENTRY CONFIRMED"),
                "structure_break": confirmed,
            },
        }

    @staticmethod
    def plan(side):
        return {
            "side": side,
            "entry_low": 99.8,
            "entry_high": 100.2,
            "stop_loss": 98.5 if side == "BUY" else 101.5,
            "take_profit_1": 102.0 if side == "BUY" else 98.0,
            "take_profit_2": 103.0 if side == "BUY" else 97.0,
        }

    def test_lower_ssl_sweep_maps_to_confirmed_bear_trap_buy(self):
        setup = self.make_setup("BUY", "ENTRY CONFIRMED", self.plan("BUY"))
        trap = self.engine._trap_setup_from_reversal_setup("5m", setup)

        self.assertEqual(trap["status"], "TRAP CONFIRMED")
        self.assertEqual(trap["trap_type"], "BEAR TRAP")
        self.assertEqual(trap["side"], "BUY")
        self.assertEqual(trap["sweep_type"], "SSL / SELL-SIDE LIQUIDITY")
        self.assertEqual(trap["trade_plan"]["side"], "BUY")
        self.assertTrue(trap["checks"]["structure_confirmation"])

    def test_upper_bsl_sweep_maps_to_confirmed_bull_trap_sell(self):
        setup = self.make_setup("SELL", "ENTRY CONFIRMED", self.plan("SELL"))
        trap = self.engine._trap_setup_from_reversal_setup("15m", setup)

        self.assertEqual(trap["status"], "TRAP CONFIRMED")
        self.assertEqual(trap["trap_type"], "BULL TRAP")
        self.assertEqual(trap["side"], "SELL")
        self.assertEqual(trap["sweep_type"], "BSL / BUY-SIDE LIQUIDITY")
        self.assertEqual(trap["trade_plan"]["side"], "SELL")

    def test_sweep_without_displacement_is_detected_not_confirmed(self):
        setup = self.make_setup("SELL", "LIQUIDITY SWEPT")
        trap = self.engine._trap_setup_from_reversal_setup("1h", setup)

        self.assertEqual(trap["status"], "SWEEP DETECTED")
        self.assertEqual(trap["trap_type"], "BULL TRAP")
        self.assertIsNone(trap["trade_plan"])
        self.assertFalse(trap["checks"]["directional_displacement"])

    def test_displacement_without_structure_is_trap_forming(self):
        setup = self.make_setup("BUY", "SETUP FORMING")
        trap = self.engine._trap_setup_from_reversal_setup("4h", setup)

        self.assertEqual(trap["status"], "TRAP FORMING")
        self.assertEqual(trap["trap_type"], "BEAR TRAP")
        self.assertIsNone(trap["trade_plan"])
        self.assertTrue(trap["checks"]["directional_displacement"])
        self.assertFalse(trap["checks"]["structure_confirmation"])

    def test_no_sweep_remains_waiting_without_direction(self):
        setup = {
            "status": "WAITING",
            "side": None,
            "candidate_side": None,
            "price": 100.0,
            "checks": {
                "liquidity_sweep": False,
                "displacement": False,
                "structure_break": False,
            },
        }
        trap = self.engine._trap_setup_from_reversal_setup("1m", setup)

        self.assertEqual(trap["status"], "WAITING")
        self.assertIsNone(trap["trap_type"])
        self.assertIsNone(trap["side"])
        self.assertIsNone(trap["trade_plan"])

    def test_structure_confirmation_without_valid_plan_is_not_actionable(self):
        setup = self.make_setup("BUY", "ENTRY CONFIRMED", None)
        trap = self.engine._trap_setup_from_reversal_setup("1d", setup)

        self.assertEqual(trap["status"], "RISK PLAN UNAVAILABLE")
        self.assertEqual(trap["trap_type"], "BEAR TRAP")
        self.assertIsNone(trap["trade_plan"])


if __name__ == "__main__":
    unittest.main()
