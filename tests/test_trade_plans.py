import unittest

from ast_strategy import ASTStructureStrategy


class TradePlanTests(unittest.TestCase):
    def test_buy_plan_has_entry_stop_and_1_5r_2r_targets(self):
        plan = ASTStructureStrategy._make_trade_plan(
            "BUY", 99.0, 101.0, 97.0, 102.0, "TEST"
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["entry_price"], 100.0)
        self.assertEqual(plan["stop_loss"], 97.0)
        self.assertEqual(plan["take_profit_1"], 104.5)
        self.assertEqual(plan["take_profit_2"], 106.0)
        self.assertEqual(plan["entry_state"], "WAIT FOR PULLBACK")

    def test_sell_plan_has_targets_below_entry(self):
        plan = ASTStructureStrategy._make_trade_plan(
            "SELL", 99.0, 101.0, 103.0, 98.0, "TEST"
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["entry_price"], 100.0)
        self.assertEqual(plan["stop_loss"], 103.0)
        self.assertEqual(plan["take_profit_1"], 95.5)
        self.assertEqual(plan["take_profit_2"], 94.0)
        self.assertEqual(plan["entry_state"], "WAIT FOR PULLBACK")

    def test_invalid_stop_side_does_not_generate_a_plan(self):
        buy = ASTStructureStrategy._make_trade_plan(
            "BUY", 99.0, 101.0, 100.0, 100.0, "TEST"
        )
        sell = ASTStructureStrategy._make_trade_plan(
            "SELL", 99.0, 101.0, 100.0, 100.0, "TEST"
        )
        self.assertIsNone(buy)
        self.assertIsNone(sell)

    def test_price_inside_entry_zone_is_reported(self):
        plan = ASTStructureStrategy._make_trade_plan(
            "BUY", 99.0, 101.0, 97.0, 100.25, "TEST"
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["entry_state"], "PRICE INSIDE ENTRY ZONE")

    def test_normalizes_reversed_entry_bounds(self):
        plan = ASTStructureStrategy._make_trade_plan(
            "BUY", 101.0, 99.0, 97.0, 100.0, "TEST"
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["entry_low"], 99.0)
        self.assertEqual(plan["entry_high"], 101.0)


if __name__ == "__main__":
    unittest.main()
