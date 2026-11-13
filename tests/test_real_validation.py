"""Synthetic protocol fixtures only; these are not real market results."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path

from ai_trading.backtest import BacktestConfig
from ai_trading.market_fixture import POLICY, fixture as market_fixture
from ai_trading.models import Observation, timestamp
from ai_trading.providers import MarketDataProvider, calendar_from_raw
from ai_trading.real_validation import compare, preflight
from ai_trading.storage import canonical, digest, encode_observation, save_observations


def raw_bundle(path, params, data, ingested_at):
    body = canonical(dict(data=data))
    return dict(provider="jquants_v2", schema_version=1, receipts=[dict(
        receipt=dict(schema_version=1, source="jquants_v2", endpoint=path,
                     params=params, ingested_at=ingested_at,
                     body_sha256=digest(body)),
        body=body.decode("utf-8"))])


def inputs():
    synthetic, source_config, study = market_fixture()
    sessions = source_config.sessions[:70]
    synthetic = [r for r in synthetic if json.loads(r.payload_json)["session_date"] in sessions]
    study["periods"]["development"]["end"] = sessions[-1]
    raw = dict(provider="jquants_v2", schema_version=1, receipts=[])
    masters = []
    for day in sessions:
        at = day + "T17:00:00+09:00"
        data = []
        for row in synthetic:
            payload = json.loads(row.payload_json)
            if payload["session_date"] == day:
                data.append(dict(Date=day, Code=payload["code"], O=payload["open"],
                                 H=payload["high"], L=payload["low"], C=payload["close"],
                                 Vo=payload["volume"], AdjFactor=1))
        raw["receipts"] += raw_bundle("/equities/bars/daily", dict(date=day), data, at)["receipts"]
        masters.append(raw_bundle("/equities/master", dict(date=day),
                                  [dict(Date=day, Code=code, ProdCat="011")
                                   for code in ("A", "B")], at))
    rows = MarketDataProvider.regenerate(raw)
    days = []
    current = date.fromisoformat(sessions[0])
    while current <= date.fromisoformat(sessions[-1]):
        days.append(dict(Date=str(current), HolDiv="1" if str(current) in sessions else "0"))
        current += timedelta(days=1)
    calendar_bundle = raw_bundle("/markets/calendar",
                                 dict(from_=sessions[0], to=sessions[-1]), days,
                                 "2024-01-01T00:00:00Z")
    calendar_bundle["receipts"][0]["receipt"]["params"] = dict(
        **{"from": sessions[0]}, to=sessions[-1])
    calendar = calendar_from_raw(calendar_bundle)
    refs = []
    for code in ("A", "B"):
        known = timestamp("2024-01-01T00:00:00Z")
        refs.append(encode_observation(Observation(
            "historical_universe", "jquants_v2:" + code,
            timestamp("2023-01-04T00:00:00+09:00"), known, known, known,
            "original", "exchange_archive",
            canonical(dict(listing_date="2023-01-04", delisting_date=None)).decode())))
    market = dict(data_provider="jquants_v2", raw_data=raw,
                  raw_data_hash=digest(canonical(raw)), calendar=calendar.__dict__,
                  historical_universe=refs, corporate_actions=[],
                  missing_data_policy=POLICY, cost_scenario="base",
                  statistical_evaluation=dict(block_length=5, replicates=20,
                                              confidence=.95, seed=7))
    study["universe_definition"] = dict(
        type="historical_point_in_time", symbols=["jquants_v2:A", "jquants_v2:B"],
        known_at="2024-01-01T00:00:00Z", evidence="synthetic predeclared candidates")
    config = BacktestConfig(sessions=sessions, universe=study["universe_definition"]["symbols"],
                            initial_cash="200000", lot_size=100, commission_bps="10",
                            slippage_bps="5", max_position_weight="0.20",
                            min_cash_reserve_weight="0.40", volume_participation="0.1",
                            lookback=2, top_n=1, ml=dict(horizon=1, train_sessions=2,
                                                         validation_sessions=1, step=1,
                                                         epochs=5, seed=7),
                            market=market)
    evidence = dict(schema_version=1, period=[sessions[0], sessions[-1]],
                    universe=list(config.universe), calendar_bundle=calendar_bundle,
                    master_bundles=masters, lifecycle_source="synthetic exchange archive",
                    corporate_action_source="synthetic event coverage review",
                    trading_unit_source="synthetic 100-share schedule",
                    calendar_bundle_sha256=digest(canonical(calendar_bundle)),
                    master_bundles_sha256=digest(canonical(masters)),
                    historical_universe_sha256=digest(canonical(refs)),
                    corporate_actions_sha256=digest(canonical([])))
    return rows, config, study, evidence


class RealValidationTests(unittest.TestCase):
    def test_nine_condition_exploratory_comparison(self):
        rows, config, study, evidence = inputs()
        with tempfile.TemporaryDirectory() as root:
            report = compare(rows, config, study, evidence, root)
            self.assertEqual(set(report["results"]), {"100000", "200000", "300000"})
            self.assertTrue(all(len(group) == 3 for group in report["results"].values()))
            self.assertEqual(report["label"], "exploratory_observed_PIT")
            self.assertEqual(len(list((Path(root) / "comparisons").glob("*.json"))), 1)
            self.assertFalse((Path(root) / "holdout").exists())

    def test_late_ingestion_and_master_gap_fail_before_store_creation(self):
        rows, config, study, evidence = inputs()
        late = replace(rows[0], available_at=timestamp("2025-01-01T00:00:00Z"),
                       ingested_at=timestamp("2025-01-01T00:00:00Z"))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                compare([late] + rows[1:], config, study, evidence, Path(root) / "store")
            self.assertFalse((Path(root) / "store").exists())
        evidence["master_bundles"].pop()
        evidence["master_bundles_sha256"] = digest(canonical(evidence["master_bundles"]))
        with self.assertRaisesRegex(ValueError, "master snapshots"):
            preflight(rows, config, study, evidence)

    def test_rejects_fixture_provider_and_holdout(self):
        rows, config, study, evidence = inputs()
        with self.assertRaisesRegex(ValueError, "holdout"):
            preflight(rows, config, study, evidence, "holdout")
        config.market["data_provider"] = "synthetic_jquants_v2"
        with self.assertRaisesRegex(ValueError, "archived J-Quants"):
            preflight(rows, config, study, evidence)

    def test_unvetted_free_sources_cannot_create_jquants_comparison(self):
        for provider in ("yfinance", "stooq", "jpx_public"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as root:
                rows, config, study, evidence = inputs()
                config.market["data_provider"] = provider
                store = Path(root) / "store"
                with self.assertRaisesRegex(ValueError, "archived J-Quants"):
                    compare(rows, config, study, evidence, store)
                self.assertFalse(store.exists())

    def test_master_observed_after_decision_rejected(self):
        rows, config, study, evidence = inputs()
        evidence["master_bundles"][0]["receipts"][0]["receipt"]["ingested_at"] = (
            config.sessions[0] + "T18:00:00+09:00")
        evidence["master_bundles_sha256"] = digest(canonical(evidence["master_bundles"]))
        with self.assertRaisesRegex(ValueError, "master acquired after PIT decision"):
            preflight(rows, config, study, evidence)

    def test_cli_check_only_does_not_create_store(self):
        rows, config, study, evidence = inputs()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            data = save_observations(root / "inputs", rows)
            for name, value in (("config", asdict(config)), ("study", study),
                                ("evidence", evidence)):
                (root / (name + ".json")).write_bytes(canonical(value))
            env = dict(os.environ, PYTHONPATH="src", PYTHONUTF8="1")
            result = subprocess.run(
                [sys.executable, "-m", "ai_trading.real_validation",
                 "--config", str(root / "config.json"), "--study", str(root / "study.json"),
                 "--data", str(data), "--evidence", str(root / "evidence.json"),
                 "--check-only"], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "ready_for_exploratory_comparison")
            self.assertFalse((root / "comparisons").exists())


if __name__ == "__main__":
    unittest.main()
