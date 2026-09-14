"""Phase 3 tests use synthetic observations and no services."""
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from ai_trading.experiment import ExperimentStore, initialize
from ai_trading.models import timestamp
from ai_trading.storage import canonical, save_observations
from test_backtest import SESSIONS, bar, config, market


def study():
    return dict(periods={"development": {"start": "2025-01-06", "end": "2025-01-21"},
                         "validation": {"start": "2025-02-01", "end": "2025-02-28"},
                         "holdout": {"start": "2025-03-01", "end": "2025-03-31"}},
                universe_definition={"type": "static_point_in_time", "symbols": ["fixture:A"],
                                     "known_at": "2025-01-01T00:00:00Z", "evidence": "synthetic predeclared universe"})


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        initialize(self.root, study())
        self.store = ExperimentStore(self.root)

    def test_repeat_and_input_order(self):
        a = self.store.run(market(), config())
        b = self.store.run(reversed(market()), config())
        self.assertNotEqual(a.experiment_id, b.experiment_id)
        for field in ("input_data_hash", "config_hash", "result_hash", "metrics"):
            self.assertEqual(getattr(a, field), getattr(b, field))
        self.assertEqual(len(self.store.list()), 2)

    def test_data_hash_changes(self):
        a = self.store.run(market(), config())
        b = self.store.run([bar(SESSIONS[0], price=110)] + market()[1:], config())
        self.assertNotEqual(a.input_data_hash, b.input_data_hash)

    def test_config_and_seed_hash_changes(self):
        a = self.store.run(market(), config())
        for c, seed in ((config(commission_bps="10"), 0), (config(), 42)):
            b = self.store.run(market(), c, random_seed=seed)
            self.assertNotEqual(a.config_hash, b.config_hash)

    def test_git_revision_and_source_snapshot(self):
        a = self.store.run(market(), config())
        expected = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
        self.assertEqual(a.git_commit, expected)
        env = self.store.read(a.experiment_id)["environment"]
        self.assertIn("experiment.py", env["sources"])
        self.assertIsNotNone(env["git_status"])

    def test_saved_reverification_offline(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
            a = self.store.run(market(), config())
            self.assertEqual(self.store.verify(a.experiment_id), asdict(a))

    def test_all_artifacts_detect_tampering(self):
        a = self.store.run(market(), config())
        folder = self.root / "research" / a.experiment_id
        for path in folder.glob("*.json"):
            original = path.read_bytes()
            path.write_bytes(b"{}")
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                self.store.verify(a.experiment_id)
            path.write_bytes(original)

    def test_code_change_refuses_replay(self):
        from ai_trading.experiment import environment
        a = self.store.run(market(), config())
        env = dict(environment(), code_hash="changed")
        with patch("ai_trading.experiment.environment", return_value=env), self.assertRaisesRegex(ValueError, "code_hash"):
            self.store.verify(a.experiment_id)

    def test_failed_execution_saved_and_replayed(self):
        a = self.store.run([bar(SESSIONS[0], price=-1)] + market()[1:], config())
        self.assertEqual(a.status, "failed")
        self.assertIn("ValueError", a.failure_reason)
        self.assertEqual(self.store.verify(a.experiment_id), asdict(a))
        self.assertEqual(len(self.store.compare()), 1)

    def test_invalid_config_saved(self):
        a = self.store.run(market(), dict(asdict(config()), strategy="llm"))
        self.assertEqual(a.status, "failed")
        self.assertEqual(self.store.verify(a.experiment_id), asdict(a))

    def test_holdout_access_denied(self):
        c = config(sessions=("2025-03-03", "2025-03-04"))
        rows = [bar(d) for d in c.sessions]
        with self.assertRaises(PermissionError):
            self.store.run(rows, c, "holdout")
        a = self.store.run(rows, c, "holdout", holdout=True)
        self.assertEqual(a.status, "completed")
        for action in (self.store.read, self.store.verify, lambda eid: self.store.compare([eid])):
            with self.assertRaises(PermissionError):
                action(a.experiment_id)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.verify(a.experiment_id, holdout=True), asdict(a))

    def test_relabel_holdout_sessions_fails_without_archiving_holdout(self):
        a = self.store.run(market() + [bar("2025-03-03")], config(sessions=("2025-03-03",)))
        self.assertEqual(a.status, "failed")
        saved = self.store.read(a.experiment_id)
        self.assertFalse(any("2025-03-03" in r["event_at"] for r in saved["inputs"]))

    def test_future_data_corrections_and_symbols_do_not_change_past(self):
        later = timestamp("2025-03-01T00:00:00Z")
        correction = replace(bar(SESSIONS[0], price=999), revision_id="corrected",
                             available_at=later, ingested_at=later)
        a = self.store.run(market(), config())
        b = self.store.run(market() + [correction, bar("2025-03-03"), bar(SESSIONS[0], "NEW")], config())
        self.assertEqual(a.input_data_hash, b.input_data_hash)
        self.assertEqual(a.result_hash, b.result_hash)

    def test_future_universe_rejected(self):
        s = study()
        s["universe_definition"]["known_at"] = "2025-02-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "future universe"):
            initialize(self.root / "bad", s)

    def test_split_overlap_and_redefinition_rejected(self):
        s = study()
        s["periods"]["validation"]["start"] = "2025-01-20"
        with self.assertRaisesRegex(ValueError, "disjoint"):
            initialize(self.root / "bad", s)
        s = study()
        s["periods"]["holdout"]["end"] = "2025-04-01"
        with self.assertRaisesRegex(ValueError, "locked"):
            initialize(self.root, s)

    def test_three_baselines_and_incomparable_costs(self):
        ids = [self.store.run(market(), config(strategy=s)).experiment_id
               for s in ("buy_and_hold", "equal_weight", "momentum")]
        result = self.store.compare(ids)
        self.assertEqual(len(result), 3)
        self.assertEqual(set(result[0]["metrics"]), {"cumulative_return", "cagr", "max_drawdown",
                                                   "annualized_volatility", "sharpe_ratio", "turnover"})
        other = self.store.run(market(), config(commission_bps="10"))
        with self.assertRaisesRegex(ValueError, "incomparable"):
            self.store.compare(ids + [other.experiment_id])

    def test_historical_mode_forbidden(self):
        a = self.store.run(market(), config(pit_mode="historical", knowledge_at="2026-01-01T00:00:00Z"))
        self.assertEqual(a.status, "failed")

    def test_session_event_mismatch_fails(self):
        row = bar(SESSIONS[0])
        data = json.loads(row.payload_json)
        data["session_date"] = SESSIONS[-1]
        a = self.store.run([replace(row, payload_json=canonical(data).decode())], config())
        self.assertEqual(a.status, "failed")

    def test_trade_history_links_decision_and_fill(self):
        a = self.store.run(market(), config())
        saved = self.store.read(a.experiment_id)
        for trade in saved["trade_history"]:
            self.assertEqual(trade["fill"]["order_id"], trade["order"]["id"])
            self.assertLess(trade["order"]["decision_session"], trade["fill"]["session"])

    def test_cli_baseline_compare_verify_and_failed_exit(self):
        data = save_observations(self.root / "data", market())
        cfg = self.root / "config.json"
        cfg.write_bytes(canonical(asdict(config())))
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "ai_trading.experiment", *args,
                                   "--store", str(self.root)], capture_output=True, text=True)
        r = cli("baseline", "--data", str(data), "--config", str(cfg))
        self.assertEqual(r.returncode, 0, r.stderr)
        ids = [a["experiment_id"] for a in json.loads(r.stdout)]
        self.assertEqual(cli("compare", *ids).returncode, 0)
        self.assertEqual(cli("verify", ids[0]).returncode, 0)
        cfg.write_bytes(canonical(dict(asdict(config()), strategy="llm")))
        r = cli("run", "--data", str(data), "--config", str(cfg))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)[0]["status"], "failed")

    def test_path_traversal_denied(self):
        with self.assertRaises(ValueError):
            self.store.read("../holdout")

    def test_validation_is_separate_and_cannot_be_compared_to_development(self):
        a = self.store.run(market(), config())
        c = config(sessions=("2025-02-03", "2025-02-04"))
        b = self.store.run([bar(d) for d in c.sessions], c, "validation")
        self.assertEqual(b.status, "completed")
        self.assertEqual(self.store.verify(b.experiment_id), asdict(b))
        with self.assertRaisesRegex(ValueError, "incomparable"):
            self.store.compare([a.experiment_id, b.experiment_id])

    def test_empty_data_is_saved_failure(self):
        a = self.store.run([], config())
        self.assertEqual(a.status, "failed")
        self.assertIn("no eligible", a.failure_reason)
        self.store.verify(a.experiment_id)

    def test_future_prices_within_horizon_do_not_change_earlier_decisions(self):
        a = self.store.run(market(), config(strategy="momentum", lookback=2))
        rows = market()[:7] + [bar(d, price=9999) for d in SESSIONS[7:]]
        b = self.store.run(rows, config(strategy="momentum", lookback=2))
        aa, bb = (self.store.read(x.experiment_id) for x in (a, b))
        self.assertEqual(aa["daily_equity"][:7], bb["daily_equity"][:7])
        for artifact in ("fills",):
            self.assertEqual([f for f in aa[artifact] if f["session"] <= SESSIONS[6]],
                             [f for f in bb[artifact] if f["session"] <= SESSIONS[6]])

    def test_later_revision_within_horizon_does_not_rewrite_past(self):
        later = timestamp(SESSIONS[7] + "T17:00:00+09:00")
        correction = replace(bar(SESSIONS[0], price=999), revision_id="correction",
                             available_at=later, ingested_at=later)
        a = self.store.run(market(), config())
        b = self.store.run(market() + [correction], config())
        aa, bb = (self.store.read(x.experiment_id) for x in (a, b))
        self.assertEqual(aa["daily_equity"][:7], bb["daily_equity"][:7])

    def test_runtime_change_refuses_replay(self):
        from ai_trading.experiment import environment
        a = self.store.run(market(), config())
        with patch("ai_trading.experiment.environment", return_value=dict(environment(), python="0.0")):
            with self.assertRaisesRegex(ValueError, "python"):
                self.store.verify(a.experiment_id)
