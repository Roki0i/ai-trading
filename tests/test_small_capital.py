"""Small-capital risk controls, using the existing synthetic daily bars only."""
import unittest
from decimal import Decimal

from ai_trading.backtest import run_backtest
from test_backtest import SESSIONS, bar, config, market


class SmallCapitalTests(unittest.TestCase):
    def test_three_positions_and_cash_reserve_at_each_capital_level(self):
        rows = [bar(day, symbol) for day in SESSIONS for symbol in ("A", "B", "C")]
        for capital in ("100000", "200000", "300000"):
            with self.subTest(capital=capital):
                r = run_backtest(rows, config(
                    universe=("fixture:A", "fixture:B", "fixture:C"),
                    initial_cash=capital, lot_size=100,
                    max_position_weight="0.20", min_cash_reserve_weight="0.40"))
                snap = r["snapshots"][1]
                expected_quantity = int(Decimal(capital) * Decimal("0.20") / 100 / 100) * 100
                self.assertEqual(snap["positions"], {
                    "fixture:A": expected_quantity,
                    "fixture:B": expected_quantity,
                    "fixture:C": expected_quantity,
                })
                self.assertEqual(Decimal(snap["cash"]), Decimal(capital) * Decimal("0.40"))
                for day in r["snapshots"]:
                    self.assertGreaterEqual(Decimal(day["cash"]), Decimal(capital) * Decimal("0.40"))

    def test_cash_reserve_includes_fee_and_slippage(self):
        r = run_backtest(market(), config(
            initial_cash="200000", lot_size=100, min_cash_reserve_weight="0.40",
            commission_fixed="100", commission_bps="20", slippage_bps="50",
            strategy="buy_and_hold"))
        self.assertTrue(r["fills"])
        self.assertTrue(all(
            Decimal(s["cash"]) >= Decimal("80000") for s in r["snapshots"]))
        self.assertTrue(all(fill["quantity"] % 100 == 0 for fill in r["fills"]))

    def test_gap_up_cannot_overfill_position_cap(self):
        prices = [100, 200] + [200] * (len(SESSIONS) - 2)
        r = run_backtest(market(prices), config(
            initial_cash="200000", lot_size=100, max_position_weight="0.20",
            commission_bps="10", slippage_bps="5", strategy="buy_and_hold"))
        self.assertEqual(r["orders"][0]["quantity"], 400)
        self.assertEqual(r["fills"][0]["quantity"], 100)
        self.assertEqual(r["orders"][0]["status"], "partial_cancelled")
        first_fill = r["snapshots"][1]
        exposure = Decimal("200") * first_fill["positions"]["fixture:A"]
        self.assertLessEqual(exposure / Decimal(first_fill["equity"]), Decimal("0.20"))

    def test_unaffordable_standard_lot_stays_in_cash(self):
        rows = [bar(day, price=500) for day in SESSIONS]
        standard = run_backtest(rows, config(
            initial_cash="200000", lot_size=100, max_position_weight="0.20"))
        self.assertFalse(standard["orders"])
        self.assertFalse(standard["fills"])
        self.assertEqual(standard["snapshots"][-1]["cash"], "200000")
        # lot_size=1 models fractional-lot arithmetic, not a broker guarantee.
        odd_lot = run_backtest(rows, config(
            initial_cash="200000", lot_size=1, max_position_weight="0.20"))
        self.assertEqual(odd_lot["fills"][0]["quantity"], 80)

    def test_invalid_limits_rejected(self):
        for values in (
            {"max_position_weight": "0"},
            {"max_position_weight": "-0.1"},
            {"max_position_weight": "1.01"},
            {"max_position_weight": "NaN"},
            {"min_cash_reserve_weight": "-0.01"},
            {"min_cash_reserve_weight": "1"},
            {"min_cash_reserve_weight": "Infinity"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                config(**values)

    def test_explicit_disabled_limits_match_defaults(self):
        expected = run_backtest(market(), config())
        actual = run_backtest(market(), config(
            max_position_weight="1", min_cash_reserve_weight="0"))
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
