"""Phase 9の条件評価とCLIを架空データだけで検証する。"""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from ai_trading.portfolio import Transaction, MarketSnapshot, RuleConfig, PortfolioError, PortfolioStore
from ai_trading.portfolio.engine import status, replay
from ai_trading.portfolio.market import JsonSnapshotProvider
from ai_trading.portfolio.models import instant
from ai_trading.portfolio.assessment import assess
from ai_trading.portfolio.assessment_market import collect_snapshots
from ai_trading.portfolio.__main__ import main
from ai_trading.portfolio.jquants import prepare_provider
from test_portfolio_market import FakeTransport

AT = instant("2025-01-08T09:00:00Z")
FIXTURE = Path(__file__).resolve().parent/"fixtures/portfolio_market.json"


def trade(identifier="one", **changes):
    values = dict(id=identifier, symbol="7203", side="buy", quantity="10", price="100",
                  fee="0", currency="JPY", executed_at="2025-01-01", note="", created_at=AT)
    values.update(changes)
    return Transaction(**values)


def snapshot(**changes):
    values = dict(symbol="7203", price="120", previous_close="100", currency="JPY", as_of=AT,
                  source="fixture", market="TSE", data_date="2025-01-08", ingested_at=AT)
    values.update(changes)
    return MarketSnapshot(**values)


def report(config=None, snapshots=None, transactions=None, blocked=None):
    state = status(transactions if transactions is not None else [trade()],
                   JsonSnapshotProvider(snapshots if snapshots is not None else [snapshot()]),
                   config or RuleConfig(), AT)
    return assess(state, generated_at=AT, blocked=blocked)


class AssessmentTests(unittest.TestCase):
    def test_no_flags(self):
        row = report()["assessments"][0]
        self.assertEqual(row["assessment_flags"], [])
        self.assertEqual(row["triggered_rules"], [])
        self.assertEqual(row["severity"], "info")
        self.assertEqual(row["metrics"]["unrealized_pnl_pct"], "0.2")

    def test_take_profit_only(self):
        row = report(RuleConfig(take_profit_pct="20"))["assessments"][0]
        self.assertEqual(row["assessment_flags"], ["take_profit_threshold"])
        self.assertEqual(row["severity"], "info")

    def test_loss_only(self):
        row = report(RuleConfig(loss_warning_pct="-20"), [snapshot(price="80")])["assessments"][0]
        self.assertEqual(row["assessment_flags"], ["loss_warning"])
        self.assertEqual(row["severity"], "warning")

    def test_daily_only(self):
        row = report(RuleConfig(daily_move_pct="20"))["assessments"][0]
        self.assertEqual(row["assessment_flags"], ["daily_move_warning"])
        self.assertEqual(row["metrics"]["daily_move_pct"], "0.2")

    def test_concentration_only(self):
        row = report(RuleConfig(max_position_weight_pct="30"))["assessments"][0]
        self.assertEqual(row["assessment_flags"], ["concentration_warning"])
        self.assertEqual(row["metrics"]["portfolio_weight"], "1")

    def test_multiple_rules_all_retained(self):
        row = report(RuleConfig(take_profit_pct="20", daily_move_pct="20", max_position_weight_pct="30"))["assessments"][0]
        self.assertEqual(row["assessment_flags"], ["take_profit_threshold", "daily_move_warning", "concentration_warning"])
        self.assertEqual(len(row["triggered_rules"]), 3)
        self.assertEqual(row["severity"], "warning")

    def test_profit_exact_below_above(self):
        for price, triggered in (("119.99", False), ("120", True), ("120.01", True)):
            with self.subTest(price=price):
                row = report(RuleConfig(take_profit_pct="20"), [snapshot(price=price)])["assessments"][0]
                self.assertIs(row["reasons"][0]["triggered"], triggered)

    def test_loss_exact_below_above(self):
        for price, triggered in (("79.99", True), ("80", True), ("80.01", False)):
            with self.subTest(price=price):
                row = report(RuleConfig(loss_warning_pct="-20"), [snapshot(price=price)])["assessments"][0]
                self.assertIs(row["reasons"][0]["triggered"], triggered)

    def test_daily_signed_boundary(self):
        for price, triggered in (("80", True), ("80.01", False), ("119.99", False), ("120", True)):
            with self.subTest(price=price):
                row = report(RuleConfig(daily_move_pct="20"), [snapshot(price=price)])["assessments"][0]
                self.assertIs(row["reasons"][0]["triggered"], triggered)
                self.assertEqual(row["reasons"][0]["comparison"], "abs>=")

    def test_concentration_strict_boundary(self):
        txs = [trade(), trade("two", symbol="130A")]
        prices = [snapshot(price="100"), snapshot(symbol="130A", price="100")]
        for threshold, triggered in (("49.99", True), ("50", False), ("50.01", False)):
            with self.subTest(threshold=threshold):
                rows = report(RuleConfig(max_position_weight_pct=threshold), prices, txs)["assessments"]
                self.assertTrue(all(r["reasons"][0]["triggered"] is triggered for r in rows))
                self.assertEqual(rows[0]["reasons"][0]["comparison"], ">")

    def test_stale_preserves_metadata_suppresses_values(self):
        result = report(RuleConfig(take_profit_pct="1"), [snapshot(as_of=AT-timedelta(days=2), data_date="2025-01-06")])
        row = result["assessments"][0]
        self.assertEqual(row["market_data_status"], "stale")
        self.assertEqual(row["market_snapshot"]["data_date"], "2025-01-06")
        self.assertEqual(row["market_snapshot"]["ingested_at"], "2025-01-08T09:00:00.000000Z")
        self.assertEqual(row["reasons"][0]["code"], "stale")
        self.assertIsNone(row["metrics"]["current_price"])
        self.assertIsNone(row["reasons"][1]["triggered"])
        self.assertEqual(result["portfolio"]["currencies"][0]["stale_position_count"], 1)

    def test_stale_threshold_exact(self):
        prices = [snapshot(as_of=AT-timedelta(seconds=60))]
        self.assertEqual(report(RuleConfig(max_snapshot_age_seconds=60), prices)["assessments"][0]["market_data_status"], "fresh")
        self.assertEqual(report(RuleConfig(max_snapshot_age_seconds=59), prices)["assessments"][0]["market_data_status"], "stale")

    def test_missing_null_not_zero(self):
        result = report(RuleConfig(take_profit_pct="1", loss_warning_pct="-1"), [])
        row = result["assessments"][0]
        self.assertEqual(row["market_data_status"], "missing")
        for field in ("current_price", "market_value", "unrealized_pnl", "unrealized_pnl_pct", "daily_move_pct", "portfolio_weight"):
            self.assertIsNone(row["metrics"][field])
        self.assertEqual(row["assessment_flags"], [])
        self.assertEqual(row["severity"], "warning")
        self.assertEqual(result["portfolio"]["currencies"][0]["missing_price_count"], 1)

    def test_blocked_corporate_action(self):
        result = report(RuleConfig(take_profit_pct="1"), [], blocked={("7203", "JPY"): "market_corporate_action_requires_review"})
        row = result["assessments"][0]
        self.assertEqual(row["market_data_status"], "blocked")
        self.assertEqual(row["severity"], "critical")
        self.assertEqual(row["reasons"][0]["code"], "market_corporate_action_requires_review")
        self.assertEqual(row["assessment_flags"], [])
        group = result["portfolio"]["currencies"][0]
        self.assertEqual((group["missing_price_count"], group["blocked_position_count"]), (0, 1))
        self.assertIsNone(group["market_value"])

    def test_future_and_before_transaction_blocked(self):
        for value in (snapshot(as_of=AT+timedelta(seconds=1), ingested_at=AT+timedelta(seconds=1)),
                      snapshot(ingested_at=AT+timedelta(seconds=1))):
            row = report(snapshots=[value])["assessments"][0]
            self.assertEqual(row["market_data_status"], "blocked")
            self.assertEqual(row["reasons"][0]["code"], "future")
        row = report(snapshots=[snapshot(as_of=AT-timedelta(hours=2))],
                     transactions=[trade(executed_at=AT-timedelta(hours=1))])["assessments"][0]
        self.assertEqual(row["reasons"][0]["code"], "before_transaction")
        self.assertEqual(row["market_data_status"], "blocked")

    def test_missing_previous_close_only_suppresses_daily(self):
        row = report(RuleConfig(take_profit_pct="20", daily_move_pct="1"), [snapshot(previous_close=None)])["assessments"][0]
        self.assertEqual(row["assessment_flags"], ["take_profit_threshold"])
        self.assertIsNone(row["metrics"]["daily_move_pct"])
        self.assertEqual(row["reasons"][1]["code"], "previous_close_unavailable")
        self.assertEqual(row["market_data_status"], "fresh")
        self.assertEqual(row["severity"], "warning")

    def test_multi_currency_summary_is_separate(self):
        txs = [trade(), trade("usd", symbol="NVDA", currency="USD")]
        prices = [snapshot(), snapshot(symbol="NVDA", currency="USD", price="80")]
        result = report(RuleConfig(max_position_weight_pct="30"), prices, txs)
        groups = result["portfolio"]["currencies"]
        self.assertEqual([g["currency"] for g in groups], ["JPY", "USD"])
        self.assertEqual([g["market_value"] for g in groups], ["1200", "800"])
        self.assertEqual([g["unrealized_pnl"] for g in groups], ["200", "-200"])
        self.assertEqual([g["concentration_flags"] for g in groups], [["7203"], ["NVDA"]])
        self.assertTrue(all(g["number_of_positions"] == 1 for g in groups))

    def test_incomplete_currency_suppresses_weight_not_other_profit(self):
        result = report(RuleConfig(take_profit_pct="20", max_position_weight_pct="30"),
                        [snapshot()], [trade(), trade("two", symbol="130A")])
        row = next(r for r in result["assessments"] if r["symbol"] == "7203")
        self.assertEqual(row["assessment_flags"], ["take_profit_threshold"])
        self.assertIsNone(row["metrics"]["portfolio_weight"])
        self.assertEqual(row["reasons"][1]["code"], "currency_valuation_incomplete")
        self.assertIsNone(result["portfolio"]["currencies"][0]["market_value"])

    def test_structured_reason_decimal_contract(self):
        row = report(RuleConfig(take_profit_pct="20"))["assessments"][0]
        reason = row["reasons"][0]
        self.assertEqual({k: reason[k] for k in ("rule", "value", "threshold", "comparison", "data_date", "source")},
                         dict(rule="take_profit_threshold", value="0.2", threshold="0.2", comparison=">=", data_date="2025-01-08", source="fixture"))

    def test_closed_position_excluded_realized_retained(self):
        txs = [trade(), trade("sell", side="sell", price="120", executed_at="2025-01-02")]
        result = report(transactions=txs, snapshots=[])
        self.assertEqual(result["assessments"], [])
        group = result["portfolio"]["currencies"][0]
        self.assertEqual((group["number_of_positions"], group["realized_pnl"], group["market_value"]), (0, "200", "0"))
        self.assertEqual(group["missing_price_count"], 0)

    def test_partial_sell_fees_are_reused(self):
        txs = [trade(fee="10"), trade("sell", side="sell", quantity="4", price="150", fee="2", executed_at="2025-01-02")]
        metrics = report(transactions=txs)["assessments"][0]["metrics"]
        self.assertEqual((metrics["quantity"], metrics["average_cost"], metrics["realized_pnl"], metrics["unrealized_pnl"]), ("6", "101", "194", "114"))

    def test_mixed_data_quality_counts_and_currency_isolation(self):
        txs = [trade(), trade("b", symbol="130A"), trade("c", symbol="6758"),
               trade("d", symbol="8306"), trade("usd", symbol="NVDA", currency="USD")]
        prices = [snapshot(), snapshot(symbol="130A", as_of=AT-timedelta(days=2)),
                  snapshot(symbol="NVDA", currency="USD")]
        result = report(RuleConfig(max_position_weight_pct="30"), prices, txs,
                        blocked={("8306", "JPY"): "market_adjustment_unknown"})
        jpy, usd = result["portfolio"]["currencies"]
        self.assertEqual((jpy["number_of_positions"], jpy["stale_position_count"],
                          jpy["missing_price_count"], jpy["blocked_position_count"]), (4, 1, 1, 1))
        self.assertIsNone(jpy["market_value"])
        self.assertEqual(jpy["concentration_flags"], [])
        self.assertEqual(usd["market_value"], "1200")
        self.assertEqual(usd["concentration_flags"], ["NVDA"])

    def test_empty_portfolio(self):
        result = report(transactions=[])
        self.assertEqual(result["assessments"], [])
        self.assertEqual(result["portfolio"]["currencies"], [])

    def test_pure_deterministic_no_input_mutation(self):
        state = status([trade()], JsonSnapshotProvider([snapshot()]), RuleConfig(take_profit_pct="20"), AT)
        original = copy.deepcopy(state)
        with localcontext() as context:
            context.prec = 3
            first = assess(state, generated_at=AT)
        self.assertEqual(first, assess(state, generated_at=AT))
        self.assertEqual(state, original)
        self.assertEqual(json.loads(json.dumps(first)), first)

    def test_invalid_block_cannot_mask_valid_price_or_unknown_error(self):
        for blocked in ({("7203", "JPY"): "market_corporate_action_requires_review"},
                        {("UNKNOWN", "JPY"): "market_adjustment_unknown"}):
            with self.assertRaisesRegex(PortfolioError, "invalid_assessment_block"):
                report(blocked=blocked)
        with self.assertRaisesRegex(PortfolioError, "invalid_assessment_block"):
            report(snapshots=[], blocked={("7203", "JPY"): "market_provider_error"})


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("ai_trading.providers.urlopen", side_effect=AssertionError("real API forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/"portfolio.sqlite3"
        store = PortfolioStore.initialize(self.db)
        store.add(trade(price="2500", quantity="100"))
        store.configure(RuleConfig(take_profit_pct="0.5", daily_move_pct="7"))
        self.before = self.db.read_bytes()
        self.args = ["--market-provider", "jquants-fixture", "--market-fixture", str(FIXTURE),
                     "--market-lookback-days", "7", "--as-of", "2025-01-08T09:00:00Z"]

    def cli(self, *args, command="assess", machine=True, expected=0):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main([command, "--db", str(self.db), *(["--json"] if machine else []), *args])
        self.assertEqual(code, expected, stream.getvalue())
        self.assertEqual(self.db.read_bytes(), self.before)
        return json.loads(stream.getvalue()) if machine else stream.getvalue()

    def test_fixture_json_schema_and_db_reopen(self):
        result = self.cli(*self.args)
        self.assertEqual(set(result), {"schema_version", "generated_at", "as_of", "ok", "command", "portfolio", "assessments"})
        self.assertEqual(result["schema_version"], 1)
        row = result["assessments"][0]
        self.assertEqual(row["metrics"]["current_price"], "2520")
        self.assertEqual(row["assessment_flags"], ["take_profit_threshold"])
        self.assertEqual(row["generated_at"], result["generated_at"])
        self.assertTrue(row["market_snapshot"]["source"].startswith("fixture_jquants_v2:"))
        txs, config = PortfolioStore(self.db).read()
        self.assertEqual((len(txs), config.take_profit_pct), (1, Decimal("0.5")))
        self.assertEqual(self.cli(*self.args)["assessments"][0]["metrics"], row["metrics"])

    def test_human_output(self):
        text = self.cli(*self.args, machine=False)
        for expected in ("7203", "2,520", "+0.80%", "設定した利益閾値に到達", "日足終値", "リアルタイムではありません"):
            self.assertIn(expected, text)
        self.assertNotIn('"assessments":', text)

    def test_manual_default_missing_no_auth(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.cli()
        self.assertEqual(result["assessments"][0]["market_data_status"], "missing")

    def test_auth_missing_explicit_no_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.cli("--market-provider", "jquants", expected=2)
        self.assertEqual(result["error"]["code"], "market_auth_missing")
        self.assertNotIn("assessments", result)

    def test_fake_live_transport(self):
        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-only-not-a-credential"}), patch(
                "ai_trading.portfolio.jquants.JQuantsTransport", return_value=FakeTransport()):
            result = self.cli("--market-provider", "jquants", "--market-lookback-days", "7", "--as-of", "2025-01-08T09:00:00Z")
        self.assertTrue(result["assessments"][0]["market_snapshot"]["source"].startswith("jquants_v2:"))
        self.assertNotIn("test-only-not-a-credential", json.dumps(result))

    def test_corporate_action_fixture_blocks_without_adjustment(self):
        fake = FakeTransport()
        fake.data["pages"][2]["response"]["data"][-1]["AdjFactor"] = 0.5
        with patch("ai_trading.portfolio.jquants.FixtureTransport", return_value=fake):
            row = self.cli(*self.args)["assessments"][0]
        self.assertEqual(row["market_data_status"], "blocked")
        self.assertEqual(row["severity"], "critical")
        self.assertIsNone(row["metrics"]["current_price"])
        self.assertEqual(row["reasons"][0]["code"], "market_corporate_action_requires_review")
        self.assertEqual(len(fake.calls), 3)

    def test_blocked_symbol_does_not_stop_healthy_symbol(self):
        PortfolioStore(self.db).add(trade("two", symbol="130A"))
        self.before = self.db.read_bytes()
        fake = FakeTransport()
        fake.data["pages"][2]["response"]["data"][-1]["AdjFactor"] = 0.5
        with patch("ai_trading.portfolio.jquants.FixtureTransport", return_value=fake):
            result = self.cli(*self.args)
        rows = {r["symbol"]: r for r in result["assessments"]}
        self.assertEqual(rows["7203"]["market_data_status"], "blocked")
        self.assertEqual(rows["130A"]["market_data_status"], "fresh")
        self.assertEqual(rows["130A"]["metrics"]["current_price"], "120")
        self.assertIsNone(rows["130A"]["metrics"]["portfolio_weight"])
        self.assertEqual(len(fake.calls), 5)

    def test_invalid_fixture_combinations(self):
        for args in (("--market-provider", "jquants-fixture"), ("--market-fixture", str(FIXTURE)),
                     ("--market-provider", "jquants", "--market-fixture", str(FIXTURE))):
            with self.subTest(args=args):
                self.assertEqual(self.cli(*args, expected=2)["error"]["code"], "invalid_arguments")

    def test_manual_snapshot_cli(self):
        path = Path(self.tmp.name)/"snapshot.json"
        path.write_text(json.dumps(dict(schema_version=1, snapshots=[snapshot(price="3000").to_dict()])), encoding="utf-8")
        result = self.cli("--snapshot", str(path), "--as-of", "2025-01-08T09:00:00Z")
        self.assertEqual(result["assessments"][0]["metrics"]["unrealized_pnl_pct"], "0.2")

    def test_unknown_adjustment_blocks(self):
        fake = FakeTransport()
        fake.data["pages"][2]["response"]["data"][-1].pop("AdjFactor")
        with patch("ai_trading.portfolio.jquants.FixtureTransport", return_value=fake):
            row = self.cli(*self.args)["assessments"][0]
        self.assertEqual(row["reasons"][0]["code"], "market_adjustment_unknown")

    def test_other_provider_errors_not_swallowed(self):
        fake = FakeTransport()
        fake.data["pages"][2]["response"]["data"][-1]["C"] = -1
        with patch("ai_trading.portfolio.jquants.FixtureTransport", return_value=fake):
            result = self.cli(*self.args, expected=2)
        self.assertEqual(result["error"]["code"], "market_daily_invalid")

    def test_status_alerts_corporate_error_unchanged(self):
        for command in ("status", "alerts"):
            fake = FakeTransport()
            fake.data["pages"][2]["response"]["data"][-1]["AdjFactor"] = 0.5
            with patch("ai_trading.portfolio.jquants.FixtureTransport", return_value=fake):
                result = self.cli(*self.args, command=command, expected=2)
            self.assertEqual(result["error"]["code"], "market_corporate_action_requires_review")

    def test_status_alerts_json_unchanged(self):
        for command in ("status", "alerts"):
            result = self.cli(*self.args, command=command)
            self.assertIn("data", result)
            self.assertNotIn("assessments", result)
            self.assertEqual(result["data"]["positions"][0]["current_price"], "2520")

    def test_config_override_read_only(self):
        result = self.cli(*self.args, "--max-price-age-seconds", "60")
        self.assertEqual(result["assessments"][0]["market_data_status"], "stale")
        self.assertEqual(PortfolioStore(self.db).read()[1].max_snapshot_age_seconds, 86400)

    def test_identity_mismatch_still_fails(self):
        class WrongProvider:
            def snapshot(self, symbol, currency):
                return snapshot(symbol="130A")
        with self.assertRaisesRegex(PortfolioError, "snapshot_identity_mismatch"):
            collect_snapshots(replay([trade()]), WrongProvider())

    def test_prefetch_default_keeps_phase8_failure(self):
        fake = FakeTransport()
        fake.data["pages"][2]["response"]["data"][-1]["AdjFactor"] = 0.5
        with patch("ai_trading.portfolio.jquants.FixtureTransport", return_value=fake):
            with self.assertRaisesRegex(PortfolioError, "market_corporate_action_requires_review"):
                prepare_provider(replay([trade()]), at=AT, fixture_path=FIXTURE, lookback_days=7)


if __name__ == "__main__":
    unittest.main()
