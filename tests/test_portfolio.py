"""実保有台帳の計算・永続化・価格時点・CLI境界を外部通信なしで確認する。"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from ai_trading.portfolio import (
    Transaction, MarketSnapshot, RuleConfig, PortfolioError, PortfolioStore,
    JsonSnapshotProvider, replay, status, alerts,
)
from ai_trading.portfolio.models import instant, canonical, object_from_json
from ai_trading.portfolio.store import SCHEMA

AT = instant("2025-01-03T12:00:00Z")
ROOT = Path(__file__).resolve().parents[1]


def transaction(identifier="buy1", **changes):
    values = dict(id=identifier, symbol="7203", side="buy", quantity="10",
                  price="100", fee="0", currency="JPY",
                  executed_at="2025-01-01", note="", created_at="2025-01-03T00:00:00Z")
    values.update(changes)
    return Transaction(**values)


def snapshot(symbol="7203", price="120", previous_close="100", currency="JPY", **changes):
    values = dict(symbol=symbol, price=price, previous_close=previous_close,
                  currency=currency, as_of=AT, source="fake")
    values.update(changes)
    return MarketSnapshot(**values)


class CalculationTests(unittest.TestCase):
    def test_single_buy(self):
        row = replay([transaction()])[0]
        self.assertEqual((row.quantity, row.average_cost, row.total_cost, row.realized_pnl),
                         (Decimal(10), Decimal(100), Decimal(1000), Decimal(0)))

    def test_multiple_buys_and_fees(self):
        row = replay([transaction(fee="10"), transaction("b2", price="200", fee="30")])[0]
        self.assertEqual((row.quantity, row.average_cost, row.total_cost), (20, 152, 3040))

    def test_partial_sell_fee_and_full_sell(self):
        txs = [transaction(fee="10"), transaction("s1", side="sell", quantity="4",
                price="150", fee="2", executed_at="2025-01-02")]
        row = replay(txs)[0]
        self.assertEqual((row.quantity, row.average_cost, row.total_cost, row.realized_pnl),
                         (6, 101, 606, 194))
        txs.append(transaction("s2", side="sell", quantity="6", price="90", fee="3",
                               executed_at="2025-01-03"))
        row = replay(txs)[0]
        self.assertEqual((row.quantity, row.total_cost, row.realized_pnl), (0, 0, 125))
        self.assertIsNone(row.average_cost)

    def test_rebuy_preserves_realized(self):
        txs = [transaction(), transaction("s", side="sell", price="120",
               executed_at="2025-01-02"), transaction("b", price="200", executed_at="2025-01-03")]
        row = replay(txs)[0]
        self.assertEqual((row.average_cost, row.realized_pnl), (200, 200))

    def test_oversell_at_historical_point(self):
        for txs in ([transaction(side="sell")],
                    [transaction(), transaction("s", side="sell", quantity="11")],
                    [transaction(executed_at="2025-01-02"),
                     transaction("s", side="sell", executed_at="2025-01-01")]):
            with self.subTest(txs=txs), self.assertRaisesRegex(PortfolioError, "oversell"):
                replay(txs)

    def test_execution_order_and_same_time_insertion_order(self):
        buy, sell = transaction(), transaction("s", side="sell", price="120")
        self.assertEqual(replay([buy, sell])[0].quantity, 0)
        with self.assertRaisesRegex(PortfolioError, "oversell"):
            replay([sell, buy])
        later_sell = replace(sell, executed_at=instant("2025-01-02", date_only=True))
        self.assertEqual(replay([later_sell, buy])[0].realized_pnl, 200)

    def test_duplicates_id_and_economic_fields(self):
        original = transaction()
        for other in (original, replace(original, price=Decimal(99)),
                      replace(original, id="another", note="再送")):
            with self.subTest(other=other), self.assertRaisesRegex(PortfolioError, "duplicate"):
                replay([original, other])

    def test_decimal_exactness_independent_of_ambient_precision(self):
        with localcontext() as ctx:
            ctx.prec = 3
            txs = [transaction(quantity="0.1", price="0.2", fee="0.001"),
                   transaction("s", side="sell", quantity="0.1", price="0.3", fee="0.002",
                               executed_at="2025-01-02")]
            self.assertEqual(replay(txs)[0].realized_pnl, Decimal("0.007"))
            self.assertEqual(RuleConfig(loss_warning_pct="-10.123456789012").loss_warning_pct,
                             Decimal("-10.123456789012"))

    def test_repeating_average_full_close_has_no_residual(self):
        txs = [transaction(quantity="3", price="1", fee="1")]
        for i in range(3):
            txs.append(transaction("s"+str(i), side="sell", quantity="1", price=str(i+2),
                                   executed_at="2025-01-02"))
        row = replay(txs)[0]
        self.assertEqual((row.quantity, row.total_cost, row.realized_pnl), (0, 0, 5))

    def test_invalid_transactions(self):
        cases = [dict(quantity=x) for x in ("0", "-1", "NaN", "Infinity", "1e18", "1e-13", True, 0.1)]
        cases += [dict(price="0"), dict(fee="-1"), dict(symbol="x; rm"),
                  dict(currency=[]), dict(currency="BTC"), dict(side="short"),
                  dict(asset_type="crypto"), dict(asset_type="option"),
                  dict(executed_at="2025-01-01T01:00:00"),
                  dict(executed_at="2026-01-01"), dict(note="bad\x00"), dict(id=""),
                  dict(quantity="bad"), dict(created_at=None)]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(PortfolioError):
                transaction(**changes)
        with self.assertRaisesRegex(PortfolioError, "invalid_transaction"):
            replay([{}])

    def test_date_and_timezone_normalization(self):
        self.assertEqual(transaction().to_dict()["executed_at"], "2025-01-01T00:00:00.000000Z")
        tx = transaction(executed_at="2025-01-01T09:00:00+09:00")
        self.assertEqual(tx.executed_at, transaction().executed_at)

    def test_multiple_symbols_currency_totals_weights(self):
        txs = [transaction(), transaction("b2", symbol="6758", quantity="30"),
               transaction("b3", symbol="NVDA", currency="USD")]
        provider = JsonSnapshotProvider([snapshot(), snapshot("6758", "80"),
                                         snapshot("NVDA", "150", currency="USD")])
        report = status(txs, provider, RuleConfig(), AT)
        rows = {r["symbol"]: r for r in report["positions"]}
        self.assertEqual(rows["7203"]["unrealized_pnl"], "200")
        self.assertEqual(rows["7203"]["unrealized_pnl_pct"], "0.2")
        self.assertEqual(rows["NVDA"]["portfolio_weight"], "1")
        self.assertAlmostEqual(float(rows["7203"]["portfolio_weight"]), 1/3)
        totals = {r["currency"]: r for r in report["currency_summaries"]}
        self.assertEqual((totals["JPY"]["total_cost"], totals["JPY"]["market_value"]), ("4000", "3600"))
        self.assertEqual(totals["USD"]["market_value"], "1500")
        self.assertNotIn("grand_total", report)

    def test_missing_price_invalidates_currency_weight_not_other_currency(self):
        txs = [transaction(), transaction("b2", symbol="6758"),
               transaction("b3", symbol="NVDA", currency="USD")]
        report = status(txs, JsonSnapshotProvider([snapshot(), snapshot("NVDA", currency="USD")]),
                        RuleConfig(), AT)
        totals = {r["currency"]: r for r in report["currency_summaries"]}
        self.assertFalse(totals["JPY"]["complete"])
        self.assertIsNone(totals["JPY"]["market_value"])
        self.assertEqual(totals["JPY"]["known_market_value"], "1200")
        for row in report["positions"]:
            if row["currency"] == "JPY":
                self.assertIsNone(row["portfolio_weight"])
        self.assertTrue(totals["USD"]["complete"])

    def test_snapshot_age_boundary_future_and_before_trade(self):
        cases = [(AT-timedelta(seconds=60), "ok"), (AT-timedelta(seconds=61), "stale"),
                 (AT+timedelta(microseconds=1), "future")]
        for at, expected in cases:
            with self.subTest(expected=expected):
                report = status([transaction()], JsonSnapshotProvider([snapshot(as_of=at)]),
                                RuleConfig(max_snapshot_age_seconds=60), AT)
                row = report["positions"][0]
                self.assertEqual(row["valuation_status"], expected)
                if expected != "ok":
                    self.assertIsNone(row["unrealized_pnl"])
        report = status([transaction()], JsonSnapshotProvider([
            snapshot(as_of=instant("2024-12-31T23:59:59Z"))]),
            RuleConfig(max_snapshot_age_seconds=1000000), AT)
        self.assertEqual(report["positions"][0]["valuation_status"], "before_transaction")

    def test_provider_boundary_and_closed_position(self):
        class Fake:
            def __init__(self):
                self.calls = []
            def snapshot(self, symbol, currency):
                self.calls.append((symbol, currency))
                return snapshot()
        fake = Fake()
        report = status([transaction()], fake, RuleConfig(), AT)
        self.assertEqual(fake.calls, [("7203", "JPY")])
        self.assertEqual(report["positions"][0]["current_price"], "120")
        fake.calls.clear()
        closed = status([transaction(), transaction("s", side="sell", price="150")],
                        fake, RuleConfig(), AT)
        self.assertEqual(fake.calls, [])
        self.assertEqual(closed["positions"][0]["valuation_status"], "closed")
        self.assertEqual(closed["currency_summaries"][0]["realized_pnl"], "500")
        self.assertEqual(alerts(closed, RuleConfig(take_profit_pct="20"))["alerts"], [])

    def test_snapshot_identity_mismatch(self):
        class Wrong:
            def snapshot(self, symbol, currency):
                return snapshot("NVDA", currency="USD")
        with self.assertRaisesRegex(PortfolioError, "snapshot_identity_mismatch"):
            status([transaction()], Wrong(), RuleConfig(), AT)

    def test_empty_portfolio_and_historical_valuation(self):
        self.assertEqual(status([], JsonSnapshotProvider(), RuleConfig(), AT)["currency_summaries"], [])
        with self.assertRaisesRegex(PortfolioError, "valuation_precedes_transaction"):
            status([transaction()], JsonSnapshotProvider(), RuleConfig(), "2024-01-01T00:00:00Z")

    def test_take_profit_and_loss_boundaries(self):
        config = RuleConfig(take_profit_pct="20", loss_warning_pct="-10")
        for price, expected in (("119.99", (False, False)), ("120", (True, False)),
                                ("120.01", (True, False)), ("90", (False, True)),
                                ("90.01", (False, False)), ("89.99", (False, True))):
            with self.subTest(price=price):
                report = status([transaction()], JsonSnapshotProvider([snapshot(price=price)]), config, AT)
                self.assertEqual(tuple(a["triggered"] for a in alerts(report, config)["alerts"]), expected)

    def test_daily_move_both_signs_boundary(self):
        config = RuleConfig(daily_move_pct="7")
        for price, expected in (("107", True), ("93", True), ("106.99", False), ("93.01", False)):
            with self.subTest(price=price):
                report = status([transaction()], JsonSnapshotProvider([snapshot(price=price)]), config, AT)
                self.assertEqual(alerts(report, config)["alerts"][0]["triggered"], expected)

    def test_concentration_strict_boundary(self):
        txs = [transaction(quantity="3"), transaction("b", symbol="6758", quantity="7")]
        provider = JsonSnapshotProvider([snapshot(price="100"), snapshot("6758", "100")])
        config = RuleConfig(max_position_weight_pct="30")
        entries = {a["symbol"]: a for a in alerts(status(txs, provider, config, AT), config)["alerts"]}
        self.assertFalse(entries["7203"]["triggered"])
        self.assertTrue(entries["6758"]["triggered"])

    def test_disabled_and_unavailable_alerts(self):
        report = status([transaction()], JsonSnapshotProvider(), RuleConfig(), AT)
        self.assertEqual(alerts(report, RuleConfig())["alerts"], [])
        config = RuleConfig(take_profit_pct="20", loss_warning_pct="-10",
                            daily_move_pct="7", max_position_weight_pct="30")
        for row in alerts(report, config)["alerts"]:
            self.assertIsNone(row["triggered"])
            self.assertEqual(row["evaluation"], "not_evaluable")
        report = status([transaction()], JsonSnapshotProvider([snapshot(previous_close=None)]), config, AT)
        daily = [r for r in alerts(report, config)["alerts"] if r["type"] == "daily_move_threshold"][0]
        self.assertIsNone(daily["triggered"])

    def test_config_validation(self):
        for values in ({"take_profit_pct": 0}, {"loss_warning_pct": "1"}, {"loss_warning_pct": "-101"},
                       {"loss_warning_pct": "NaN"}, {"daily_move_pct": -1},
                       {"max_position_weight_pct": 101}, {"max_snapshot_age_seconds": True},
                       {"max_snapshot_age_seconds": 0}, {"unknown": 1}):
            with self.subTest(values=values), self.assertRaises(PortfolioError):
                RuleConfig.from_dict(values)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/"portfolio.sqlite3"
        self.store = PortfolioStore.initialize(self.path)

    def mutate(self, sql):
        db = sqlite3.connect(self.path)
        try:
            db.executescript(sql)
            db.commit()
        finally:
            db.close()

    def test_reopen_replay_and_decimal_storage(self):
        tx = transaction(quantity="0.1", price="0.2", fee="0.001", note="手入力")
        self.store.add(tx)
        rows, config = PortfolioStore(self.path).read()
        self.assertEqual(rows, [tx])
        self.assertEqual(replay(rows)[0].total_cost, Decimal("0.021"))
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT quantity,typeof(quantity) FROM transactions").fetchone(),
                             ("0.1", "text"))
        finally:
            db.close()
        self.assertEqual(config, RuleConfig())

    def test_config_persistence(self):
        config = RuleConfig(take_profit_pct="20", loss_warning_pct="-10")
        self.store.configure(config)
        self.assertEqual(PortfolioStore(self.path).read()[1], config)

    def test_duplicate_and_oversell_leave_database_unchanged(self):
        self.store.add(transaction())
        before = self.path.read_bytes()
        for tx in (transaction(), transaction("duplicate"), transaction("s", side="sell", quantity="11")):
            with self.assertRaises(PortfolioError):
                self.store.add(tx)
            self.assertEqual(self.path.read_bytes(), before)

    def test_backdated_oversell_rejected(self):
        self.store.add(transaction(executed_at="2025-01-02"))
        with self.assertRaisesRegex(PortfolioError, "oversell"):
            self.store.add(transaction("s", side="sell", executed_at="2025-01-01"))
        self.assertEqual(len(self.store.read()[0]), 1)

    def test_existing_and_research_database_not_reused(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(PortfolioError, "database_already_exists"):
            PortfolioStore.initialize(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        foreign = Path(self.tmp.name)/"research.sqlite3"
        db = sqlite3.connect(foreign)
        db.execute("CREATE TABLE research (id INTEGER)")
        db.close()
        before = foreign.read_bytes()
        with self.assertRaisesRegex(PortfolioError, "unsupported_database"):
            PortfolioStore(foreign).read()
        self.assertEqual(foreign.read_bytes(), before)

    def test_missing_database_and_future_version(self):
        with self.assertRaisesRegex(PortfolioError, "database_not_initialized"):
            PortfolioStore(Path(self.tmp.name)/"missing.db").read()
        self.mutate("PRAGMA user_version=99;")
        with self.assertRaisesRegex(PortfolioError, "unsupported_database"):
            self.store.read()

    def test_append_only_triggers_and_foreign_keys(self):
        self.store.add(transaction())
        db = sqlite3.connect(self.path)
        try:
            for sql in ("DELETE FROM transactions", "UPDATE transactions SET price='2'",
                        "DELETE FROM instruments"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
                db.rollback()
        finally:
            db.close()
        with self.store._connect(write=True) as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_schema_change_detected_without_repair(self):
        self.mutate("DROP TRIGGER transactions_no_update;")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(PortfolioError, "schema_integrity_error"):
            self.store.read()
        self.assertEqual(self.path.read_bytes(), before)

    def test_hash_chain_tampering(self):
        self.store.add(transaction())
        db = sqlite3.connect(self.path)
        try:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='transactions_no_update'").fetchone()[0]
            db.execute("DROP TRIGGER transactions_no_update")
            db.execute("UPDATE transactions SET note='tampered'")
            db.execute(trigger)
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(PortfolioError, "ledger_integrity_error"):
            self.store.read()

    def test_config_corruption(self):
        self.mutate("UPDATE rules SET body='{}';")
        with self.assertRaisesRegex(PortfolioError, "config_integrity_error"):
            self.store.read()

    def test_metadata_truncation_detected(self):
        self.mutate("UPDATE metadata SET entry_count=1;")
        with self.assertRaisesRegex(PortfolioError, "ledger_integrity_error"):
            self.store.read()

    def test_invalid_file_not_repaired(self):
        other = Path(self.tmp.name)/"broken.db"
        other.write_bytes(b"not a database")
        with self.assertRaisesRegex(PortfolioError, "storage_error"):
            PortfolioStore(other).read()
        self.assertEqual(other.read_bytes(), b"not a database")

    def test_concurrent_sells_serialize_and_cannot_oversell(self):
        self.store.add(transaction())
        def sell(i):
            try:
                PortfolioStore(self.path).add(transaction("s"+str(i), side="sell", quantity="6", price=str(120+i)))
                return "ok"
            except PortfolioError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(sell, [1, 2]))
        self.assertCountEqual(results, ["ok", "oversell"])
        self.assertEqual(replay(self.store.read()[0])[0].quantity, 4)

    def test_failure_after_insert_rolls_back_atomically(self):
        from ai_trading.portfolio import store as module
        original_digest = module.digest
        def failed(value):
            if '"asset_type"' in value and '"sequence"' not in value:
                raise RuntimeError("injected")
            return original_digest(value)
        with patch.object(module, "digest", side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.store.add(transaction())
        self.assertEqual(self.store.read()[0], [])
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM instruments").fetchone()[0], 0)
        finally:
            db.close()


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root/"portfolio.sqlite3"

    def cli(self, *args, code=0):
        env = dict(os.environ, PYTHONPATH=str(ROOT/"src"), PYTHONUTF8="1")
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "ai_trading.portfolio",
            "--db", str(self.db), "--json", *args], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, code, result.stderr+result.stdout)
        self.assertEqual(result.stderr, "")
        value = json.loads(result.stdout)
        self.assertEqual(value["schema_version"], 1)
        self.assertIsNotNone(instant(value["generated_at"]))
        self.assertEqual(value["ok"], code == 0)
        return value

    def write_snapshots(self, data):
        path = self.root/"snapshot.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_human_output_is_separate_from_json(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from ai_trading.portfolio.__main__ import main
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--db", str(self.db), "init"])
        self.assertEqual(code, 0)
        self.assertIn("Portfolio DB", output.getvalue())
        self.assertNotIn('"schema_version"', output.getvalue())

    def test_complete_json_report_contract(self):
        self.cli("init")
        for command in ("status", "alerts"):
            result = self.cli(command)
            self.assertEqual(set(result), {"schema_version", "generated_at", "ok", "command", "data"})
            data = result["data"]
            self.assertEqual(set(data), {"as_of", "weight_basis", "portfolio_summary",
                             "currency_summaries", "positions", "alerts", "rules"})
            self.assertEqual(data["portfolio_summary"], dict(transaction_count=0,
                open_position_count=0, closed_position_count=0, currencies=[], valuation_complete=True))
            self.assertEqual(data["currency_summaries"], [])
            self.assertEqual(data["alerts"], [])

    def test_cli_end_to_end_machine_contract(self):
        self.cli("init")
        buy = self.cli("add", "--id", "one", "--symbol", "7203", "--side", "buy",
                      "--quantity", "10", "--price", "100", "--currency", "JPY",
                      "--executed-at", "2025-01-01", "--note", "実保有")
        self.assertEqual(buy["data"]["transaction"]["quantity"], "10")
        config = self.root/"config.json"
        config.write_text('{"take_profit_pct":20}', encoding="utf-8")
        self.cli("config", "--file", str(config))
        file = self.write_snapshots(dict(schema_version=1, snapshots=[snapshot().to_dict()]))
        report = self.cli("status", "--snapshot", str(file), "--as-of", "2025-01-03T12:00:00Z")
        self.assertEqual(report["data"]["positions"][0]["unrealized_pnl"], "200")
        entries = self.cli("alerts", "--snapshot", str(file), "--as-of", "2025-01-03T12:00:00Z")
        self.assertTrue(entries["data"]["alerts"][0]["triggered"])
        self.assertEqual(len(self.cli("transactions")["data"]["transactions"]), 1)
        self.assertEqual(self.cli("config")["data"]["rules"]["take_profit_pct"], "20")

    def test_cli_error_contract_and_no_secret_reflection(self):
        self.assertEqual(self.cli("status", code=2)["error"]["code"], "database_not_initialized")
        self.cli("init")
        result = self.cli("add", "--secret-TOKEN-EXAMPLE", code=2)
        self.assertNotIn("TOKEN", json.dumps(result))
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertEqual(self.cli("init", code=2)["error"]["code"], "database_already_exists")

    def test_json_snapshot_strict_schema_duplicates_limits(self):
        good = dict(schema_version=1, snapshots=[snapshot().to_dict()])
        file = self.write_snapshots(good)
        self.assertEqual(JsonSnapshotProvider.from_file(file).snapshot("7203", "JPY"), snapshot())
        malformed = [[], dict(good, secret="not-accepted"), dict(good, schema_version=True),
                     dict(schema_version=1, snapshots=[dict(snapshot().to_dict(), extra=1)]),
                     dict(schema_version=1, snapshots=[snapshot().to_dict()]*2),
                     dict(schema_version=1, snapshots=[dict(snapshot().to_dict(), price=0)]),
                     dict(schema_version=1, snapshots=[dict(snapshot().to_dict(), currency=[])])]
        for data in malformed:
            with self.subTest(data=data), self.assertRaises(PortfolioError):
                JsonSnapshotProvider.from_file(self.write_snapshots(data))
        file.write_bytes(b" "*(1024*1024+1))
        with self.assertRaisesRegex(PortfolioError, "snapshot_too_large"):
            JsonSnapshotProvider.from_file(file)

    def test_strict_json_and_decimal_numeric_input(self):
        for text in ('{"a":1,"a":2}', '{"a":NaN}', '{'):
            with self.assertRaises(PortfolioError):
                object_from_json(text)
        value = object_from_json('{"price":0.100000000001}')
        self.assertEqual(value["price"], Decimal("0.100000000001"))

    def test_cli_no_market_input_is_explicitly_missing(self):
        self.cli("init")
        self.cli("add", "--symbol", "7203", "--side", "buy", "--quantity", "1",
                 "--price", "100", "--currency", "JPY", "--executed-at", "2025-01-01")
        row = self.cli("status")["data"]["positions"][0]
        self.assertEqual(row["valuation_status"], "missing")
        self.assertIsNone(row["current_price"])

    def test_read_only_valuation_never_persists_snapshot(self):
        store = PortfolioStore.initialize(self.db)
        store.add(transaction())
        before = self.db.read_bytes()
        status(*[store.read()[0], JsonSnapshotProvider([snapshot(source="secret-snapshot-marker")]),
                 store.read()[1], AT])
        self.assertEqual(before, self.db.read_bytes())
        self.assertNotIn(b"secret-snapshot-marker", self.db.read_bytes())

    def test_no_research_llm_broker_or_shell_imports(self):
        import ast
        for file in (ROOT/"src"/"ai_trading"/"portfolio").glob("*.py"):
            tree = ast.parse(file.read_text(encoding="utf-8"))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                if isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            self.assertFalse(set(imports) & {"subprocess", "requests", "urllib", "forward",
                "providers", "experiment", "ollama", "raphael"}, (file, imports))


if __name__ == "__main__":
    unittest.main()
