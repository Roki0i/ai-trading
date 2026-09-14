"""Synthetic daily market fixtures: no credentials, clock or network required."""
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from ai_trading.backtest import BacktestConfig, run_backtest, save_experiment
from ai_trading.metrics import performance
from ai_trading.models import Observation, timestamp
from ai_trading.storage import canonical, load_observations, read_verified
from ai_trading.strategies import target_weights

SESSIONS = ("2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10",
            "2025-01-14", "2025-01-15", "2025-01-16", "2025-01-17", "2025-01-20", "2025-01-21")


def bar(session, code="A", price=100, **payload):
    data = dict(session_date=session, code=code, open=price, high=price, low=price,
                close=price, volume=100000, adjustment_factor=1)
    data.update(payload)
    return Observation(dataset="daily_bars", entity_id="fixture:" + code,
                       event_at=timestamp(session + "T00:00:00+09:00"), published_at=None,
                       available_at=timestamp(session + "T17:00:00+09:00"),
                       ingested_at=timestamp(session + "T17:00:00+09:00"),
                       revision_id="original", source="fixture", payload_json=canonical(data).decode())


def config(**kwargs):
    return BacktestConfig(**dict(dict(sessions=SESSIONS, universe=("fixture:A",),
                                     initial_cash="10000", lot_size=1), **kwargs))


def market(prices=None):
    return [bar(d, price=p) for d, p in zip(SESSIONS, prices or [100] * len(SESSIONS))]


class ExecutionTests(unittest.TestCase):
    def test_same_close_cannot_fill_and_gap_uses_next_close(self):
        result = run_backtest(market([100, 200] + [200] * 9), config(strategy="buy_and_hold"))
        self.assertEqual(result["snapshots"][0]["positions"], {})
        fill = result["fills"][0]
        self.assertEqual(fill["session"], SESSIONS[1])
        self.assertEqual(Decimal(fill["price"]), 200)
        self.assertEqual(fill["quantity"], 50)
        self.assertEqual(result["orders"][0]["status"], "partial_cancelled")
        for f in result["fills"]:
            self.assertLess(result["orders"][f["order_id"] - 1]["decision_session"], f["session"])

    def test_no_overspending_with_gap_fees_slippage_and_lots(self):
        rows = market([100, 300] + [300] * 9) + [bar(d, "B", 200) for d in SESSIONS]
        r = run_backtest(rows, config(universe=("fixture:A", "fixture:B"), lot_size=10,
                                     commission_fixed="51", commission_bps="50", slippage_bps="100"))
        for s in r["snapshots"]:
            self.assertGreaterEqual(Decimal(s["cash"]), 0)
            self.assertTrue(all(q > 0 and q % 10 == 0 for q in s["positions"].values()))
        self.assertTrue(all(Decimal(f["cash_after"]) >= 0 for f in r["fills"]))

    def test_costs_exactly_reduce_equity(self):
        r = run_backtest(market(), config(strategy="buy_and_hold", commission_fixed="2",
                                         commission_bps="100", slippage_bps="100"))
        f = r["fills"][0]
        self.assertEqual(f["quantity"], 98)
        self.assertEqual(Decimal(f["price"]), Decimal("101"))
        self.assertEqual(Decimal(f["commission"]), Decimal("100.98"))
        self.assertEqual(Decimal(r["snapshots"][-1]["equity"]), Decimal("9801.02"))

    def test_weekly_holiday_schedule(self):
        r = run_backtest(market([100] * 5 + [200] * 6) + [bar(d, "B") for d in SESSIONS],
                         config(universe=("fixture:A", "fixture:B")))
        decisions = {o["decision_session"] for o in r["orders"]}
        self.assertIn("2025-01-14", decisions)
        self.assertTrue(decisions <= {"2025-01-06", "2025-01-14", "2025-01-20"})

    def test_sell_before_buy_and_roundtrip_ledger(self):
        rows = market([100] * 5 + [200] * 6) + [bar(d, "B") for d in SESSIONS]
        c = config(universe=("fixture:A", "fixture:B"), commission_bps="10", commission_fixed="1", slippage_bps="50")
        r = run_backtest(rows, c)
        self.assertIn("sell", [f["side"] for f in r["fills"]])
        cash, holdings = Decimal(c.initial_cash), {}
        for f in r["fills"]:
            sign = 1 if f["side"] == "buy" else -1
            notional = Decimal(f["price"]) * f["quantity"]
            cash -= sign * notional + Decimal(f["commission"])
            holdings[f["symbol"]] = holdings.get(f["symbol"], 0) + sign * f["quantity"]
            reference = Decimal(f["reference_price"])
            self.assertEqual(Decimal(f["price"]), reference * (Decimal("1.005") if sign == 1 else Decimal("0.995")))
            self.assertEqual(Decimal(f["commission"]), notional * Decimal("0.001") + 1)
            self.assertEqual(cash, Decimal(f["cash_after"]))
        last = r["snapshots"][-1]
        equity = cash + sum(q * Decimal(last["marks"][s]) for s, q in holdings.items() if q)
        self.assertEqual(equity, Decimal(last["equity"]))
        self.assertAlmostEqual(float(equity / Decimal(c.initial_cash) - 1), r["metrics"]["cumulative_return"])

    def test_missing_and_zero_volume_defer_fill(self):
        rows = market()
        rows[1] = bar(SESSIONS[1], volume=0)
        rows = [r for r in rows if r != rows[2]]
        r = run_backtest(rows, config(strategy="buy_and_hold"))
        self.assertEqual(r["fills"][0]["session"], SESSIONS[3])

    def test_missing_held_price_is_explicitly_stale(self):
        rows = [r for r in market() if r.event_at != timestamp(SESSIONS[2] + "T00:00:00+09:00")]
        r = run_backtest(rows, config(strategy="buy_and_hold"))
        self.assertEqual(r["snapshots"][2]["stale_marks"], ["fixture:A"])

    def test_too_small_cash_rejected_without_negative_balance(self):
        r = run_backtest(market(), config(initial_cash="100", commission_fixed="200"))
        self.assertFalse(r["fills"])
        self.assertEqual(r["orders"][0]["status"], "rejected")
        self.assertEqual(r["metrics"]["cumulative_return"], 0)

    def test_last_session_order_remains_pending(self):
        r = run_backtest(market(), config(sessions=SESSIONS[:1]))
        self.assertFalse(r["fills"])
        self.assertEqual(r["orders"][0]["status"], "pending")


class PITBacktestTests(unittest.TestCase):
    def test_future_rows_and_revisions_do_not_change_past_result(self):
        rows = market()
        later = timestamp("2025-02-01T17:00:00+09:00")
        correction = replace(bar(SESSIONS[0], price=999), revision_id="correction",
                             available_at=later, ingested_at=later)
        future = bar("2025-02-03", price=999)
        self.assertEqual(run_backtest(rows, config()), run_backtest(rows + [correction, future], config()))

    def test_extending_horizon_preserves_past_equity_and_fills(self):
        short = run_backtest(market()[:7], config(sessions=SESSIONS[:7]))
        long = run_backtest(market(), config())
        self.assertEqual(short["snapshots"], long["snapshots"][:7])
        self.assertEqual(short["fills"], [f for f in long["fills"] if f["session"] <= SESSIONS[6]])

    def test_exact_availability_boundary_excluded(self):
        rows = market()
        t = timestamp(SESSIONS[0] + "T18:00:00+09:00")
        rows[0] = replace(rows[0], available_at=t, ingested_at=t)
        r = run_backtest(rows, config())
        self.assertTrue(all(o["decision_session"] != SESSIONS[0] for o in r["orders"]))

    def test_delayed_ingestion_cannot_be_used_for_execution(self):
        rows = market()
        t = timestamp(SESSIONS[2] + "T17:00:00+09:00")
        rows[1] = replace(rows[1], available_at=t, ingested_at=t)
        self.assertEqual(run_backtest(rows, config())["fills"][0]["session"], SESSIONS[2])

    def test_historical_mode_needs_evidence_and_frozen_cutoff(self):
        later = timestamp("2026-01-01T00:00:00Z")
        rows = [replace(r, published_at=r.available_at, availability_basis="documented",
                        availability_evidence="synthetic documented fixture", ingested_at=later) for r in market()]
        self.assertFalse(run_backtest(rows, config())["fills"])
        self.assertTrue(run_backtest(rows, config(pit_mode="historical", knowledge_at=later.isoformat()))["fills"])
        self.assertFalse(run_backtest(rows, config(pit_mode="historical", knowledge_at="2025-01-01T00:00:00Z"))["fills"])

    def test_corporate_actions_and_invalid_prices_fail_closed(self):
        for row in (bar(SESSIONS[0], adjustment_factor=0.5), bar(SESSIONS[0], price=-1)):
            with self.subTest(row=row), self.assertRaises(ValueError):
                run_backtest([row] + market()[1:], config())


class StrategyTests(unittest.TestCase):
    def test_buy_hold_does_not_rebalance(self):
        r = run_backtest(market([100] * 5 + [200] * 6), config(strategy="buy_and_hold"))
        self.assertEqual(len(r["orders"]), 1)
        self.assertEqual(len(r["fills"]), 1)
        self.assertEqual(r["metrics"]["cumulative_return"], 1)

    def test_equal_weight_allocates_equally(self):
        r = run_backtest(market() + [bar(d, "B", 200) for d in SESSIONS], config(universe=("fixture:B", "fixture:A")))
        self.assertEqual(r["snapshots"][1]["positions"], {"fixture:A": 50, "fixture:B": 25})

    def test_momentum_requires_history_and_selects_positive_winner(self):
        rows = market([100, 101, 102, 103, 104, 110, 111, 112, 113, 114, 115])
        rows += [bar(d, "B", 100 - i) for i, d in enumerate(SESSIONS)]
        r = run_backtest(rows, config(strategy="momentum", lookback=2, top_n=1, universe=("fixture:A", "fixture:B")))
        self.assertEqual(r["orders"][0]["decision_session"], SESSIONS[5])
        self.assertEqual(r["fills"][0]["symbol"], "fixture:A")

    def test_momentum_ties_and_nonpositive_scores(self):
        h = {s: [Decimal(100), Decimal(110)] for s in ("B", "A")}
        self.assertEqual(target_weights("momentum", h, ("B", "A"), 1, 1), {"A": Decimal(1)})
        self.assertEqual(target_weights("momentum", {"A": [Decimal(100), Decimal(99)]}, ("A",), 1, 1), {})

    def test_momentum_missing_history_excluded(self):
        rows = market([100, 101, 102, 103, 104, 110, 111, 112, 113, 114, 115])
        del rows[4]
        r = run_backtest(rows, config(strategy="momentum", lookback=2))
        self.assertFalse([o for o in r["orders"] if o["decision_session"] == SESSIONS[5]])


class ReproducibilityAndMetricsTests(unittest.TestCase):
    def test_reproducible_artifacts_input_order_and_offline_replay(self):
        with tempfile.TemporaryDirectory() as directory, patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
            root = Path(directory)
            first = save_experiment(market(), config(), root)
            second = save_experiment(reversed(market()), config(), root)
            self.assertEqual(first, second)
            manifest = json.loads(read_verified(first))
            rows = load_observations(root / "inputs" / (manifest["input_sha256"] + ".jsonl"))
            saved = read_verified(root / "results" / (manifest["result_sha256"] + ".json"))
            self.assertEqual(canonical(run_backtest(rows, BacktestConfig(**manifest["config"]))), saved)

    def test_decimal_context_does_not_affect_results(self):
        expected = run_backtest(market(), config(slippage_bps="13"))
        with localcontext() as context:
            context.prec = 7
            self.assertEqual(run_backtest(market(), config(slippage_bps="13")), expected)

    def test_metrics_known_path(self):
        snapshots = [dict(session=d, equity=v) for d, v in
                     (("2024-01-01", "100"), ("2024-07-01", "120"), ("2025-01-01", "90"))]
        m = performance("100", snapshots, [{"notional": "50"}])
        self.assertAlmostEqual(m["cumulative_return"], -0.1)
        self.assertAlmostEqual(m["cagr"], 0.9 ** (365.25 / 366) - 1)
        self.assertAlmostEqual(m["max_drawdown"], 0.25)
        import statistics
        import math
        std = statistics.stdev([0, 0.2, -0.25])
        self.assertAlmostEqual(m["annualized_volatility"], std * math.sqrt(252))
        self.assertAlmostEqual(m["sharpe_ratio"], statistics.mean([0, 0.2, -0.25]) / std * math.sqrt(252))
        self.assertAlmostEqual(m["turnover"], 50 / (310 / 3))

    def test_flat_single_session_metrics_are_defined(self):
        r = run_backtest(market(), config(sessions=SESSIONS[:1]))
        self.assertEqual(r["metrics"]["annualized_volatility"], 0)
        self.assertIsNone(r["metrics"]["sharpe_ratio"])
        self.assertIsNone(r["metrics"]["cagr"])

    def test_invalid_config_rejected(self):
        for values in (dict(initial_cash="0"), dict(slippage_bps="10000"), dict(commission_bps="-1"),
                       dict(initial_cash="NaN"), dict(lot_size=0), dict(lot_size=True),
                       dict(sessions=SESSIONS[::-1]), dict(universe=("A", "A")), dict(strategy="llm"),
                       dict(pit_mode="historical"), dict(knowledge_at="2025-01-01T00:00:00")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                config(**values)

    def test_checked_in_fixture_and_manifest_replay(self):
        from ai_trading.backtest import replay_experiment
        from ai_trading.providers import normalize_daily
        root = Path(__file__).resolve().parents[1]
        fixture = json.loads((root / "tests/fixtures/backtest_pages.json").read_text())
        rows = normalize_daily(fixture["pages"], source="fixture")
        c = BacktestConfig(**json.loads((root / "config/backtest.json").read_text()))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            manifest = save_experiment(rows, c, output)
            self.assertEqual(canonical(replay_experiment(manifest)), canonical(run_backtest(rows, c)))
            with patch("ai_trading.backtest.code_fingerprint", return_value="changed"):
                with self.assertRaisesRegex(ValueError, "fingerprint"):
                    replay_experiment(manifest)
            data = next((output / "inputs").glob("*.jsonl"))
            data.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "hash"):
                replay_experiment(manifest)
