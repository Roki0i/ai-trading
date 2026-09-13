import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from ai_trading.cli import run
from ai_trading.config import load_config
from ai_trading.models import Observation, as_of, timestamp
from ai_trading.providers import (FixtureTransport, JQuantsTransport, Response,
                                  acquire_daily, normalize_daily)
from ai_trading.quality import inspect_daily
from ai_trading.replay import replay
from ai_trading.storage import (canonical, load_observations, put, read_verified,
                                save_observations)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/daily_pages.json"


def observation(**kwargs):
    values = dict(dataset="financials", entity_id="test:90001",
                  event_at=timestamp("2024-12-31T00:00:00+09:00"),
                  published_at=timestamp("2025-02-01T15:00:00+09:00"),
                  available_at=timestamp("2025-02-01T15:01:00+09:00"),
                  ingested_at=timestamp("2025-02-01T15:01:00+09:00"),
                  revision_id="original", source="test", payload_json='{"profit":100}')
    values.update(kwargs)
    return Observation(**values)


class PointInTimeTests(unittest.TestCase):
    def test_rejects_naive_timestamps(self):
        with self.assertRaises(ValueError):
            observation(event_at=datetime(2025, 1, 1))

    def test_rejects_prepublication_availability(self):
        with self.assertRaises(ValueError):
            observation(available_at=timestamp("2025-01-01T00:00:00Z"))

    def test_observed_cannot_backdate_ingestion(self):
        with self.assertRaises(ValueError):
            observation(ingested_at=timestamp("2026-01-01T00:00:00Z"))

    def test_strict_boundary_and_timezone_equivalence(self):
        row = observation()
        self.assertEqual(as_of([row], timestamp("2025-02-01T06:01:00Z")), [])
        self.assertEqual(as_of([row], timestamp("2025-02-01T06:01:01Z")), [row])

    def test_financial_period_end_does_not_mean_published(self):
        self.assertEqual(as_of([observation()], timestamp("2025-01-15T00:00:00Z")), [])

    def test_future_revision_cannot_change_past(self):
        original = observation()
        revision = replace(original, revision_id="correction", payload_json='{"profit":50}',
                           published_at=timestamp("2025-03-01T00:00:00Z"),
                           available_at=timestamp("2025-03-01T00:01:00Z"),
                           ingested_at=timestamp("2025-03-01T00:01:00Z"))
        cutoff = timestamp("2025-02-10T00:00:00Z")
        for rows in ([original, revision], [revision, original]):
            self.assertEqual(as_of(rows, cutoff), as_of([original], cutoff))
            self.assertEqual(as_of(rows, timestamp("2025-04-01T00:00:00Z")), [revision])

    def test_historical_requires_explicit_evidence_and_knowledge_cutoff(self):
        with self.assertRaises(ValueError):
            observation(availability_basis="documented")
        row = observation(availability_basis="documented", availability_evidence="filing:1",
                          ingested_at=timestamp("2026-01-01T00:00:00Z"))
        decision = timestamp("2025-02-02T00:00:00Z")
        self.assertEqual(as_of([row], decision), [])
        with self.assertRaises(ValueError):
            as_of([row], decision, mode="historical")
        self.assertEqual(as_of([row], decision, mode="historical", knowledge_at=row.ingested_at), [row])
        self.assertEqual(as_of([row], decision, mode="historical", knowledge_at=decision), [])

    def test_estimates_excluded_by_default(self):
        row = observation(availability_basis="estimated", availability_evidence="policy-v1")
        decision = timestamp("2025-02-02T00:00:00Z")
        self.assertEqual(as_of([row], decision), [])
        self.assertEqual(as_of([row], decision, allow_estimated=True), [row])

    def test_ambiguous_revisions_fail_closed(self):
        row = observation()
        with self.assertRaises(ValueError):
            as_of([row, replace(row, revision_id="other")], timestamp("2025-02-02T00:00:00Z"))

    def test_announced_future_event_is_not_automatically_a_leak(self):
        row = observation(event_at=timestamp("2025-05-01T00:00:00Z"))
        self.assertEqual(as_of([row], timestamp("2025-02-02T00:00:00Z")), [row])

    def test_ambiguous_old_revision_rejected_regardless_of_order(self):
        original = observation()
        conflicting = replace(original, revision_id="conflicting")
        latest = replace(original, available_at=timestamp("2025-02-03T00:00:00Z"))
        for rows in ([latest, original, conflicting], [original, conflicting, latest]):
            with self.assertRaises(ValueError):
                as_of(rows, timestamp("2025-03-01T00:00:00Z"))


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transport = FixtureTransport(FIXTURE)
        self.pages, self.receipts = acquire_daily(self.transport, "2025-01-06", self.root / "raw")
        self.rows = normalize_daily(self.pages, source="fixture_jquants_v2")

    def codes(self, rows, **kwargs):
        return {i.code for i in inspect_daily(rows, **kwargs)}

    def changed(self, **updates):
        data = json.loads(self.rows[0].payload_json)
        data.update(updates)
        return replace(self.rows[0], payload_json=json.dumps(data))

    def test_pagination_and_missing_publication(self):
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(len(self.rows), 2)
        self.assertIsNone(self.rows[0].published_at)
        self.assertEqual(as_of(self.rows, timestamp("2025-01-06T23:59:59+09:00")), [])

    def test_roundtrip_and_order_independent_content_hash(self):
        path = save_observations(self.root / "processed", self.rows)
        other = save_observations(self.root / "processed", list(reversed(self.rows)))
        self.assertEqual(path, other)
        self.assertEqual(set(load_observations(path)), set(self.rows))

    def test_original_bodies_and_receipts_are_verified(self):
        receipt = json.loads(read_verified(Path(self.receipts[0])))
        body = self.root / "raw/jquants_v2/daily_bars/bodies" / (receipt["body_sha256"] + ".json")
        self.assertEqual(json.loads(read_verified(body))["data"], self.pages[0]["rows"])

    def test_tamper_detected_and_not_overwritten(self):
        path = put(self.root, b"original")
        path.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            read_verified(path)
        with self.assertRaises(ValueError):
            put(self.root, b"original")
        self.assertEqual(path.read_bytes(), b"tampered")

    def test_quality_valid_and_coverage_explicit(self):
        self.assertEqual(self.codes(self.rows), {"coverage_unverified"})
        expected = {(row.entity_id, "2025-01-06") for row in self.rows}
        self.assertEqual(self.codes(self.rows, expected_keys=expected), set())
        expected.add(("fixture_jquants_v2:missing", "2025-01-06"))
        self.assertIn("missing_session", self.codes(self.rows, expected_keys=expected))

    def test_invalid_prices_volume_and_adjustment(self):
        for updates, code in [({"close": 999}, "ohlc_order"),
                              ({"open": float("nan")}, "invalid_price"),
                              ({"close": -1}, "invalid_price"),
                              ({"volume": -1}, "invalid_volume"),
                              ({"adjustment_factor": 0}, "invalid_adjustment_factor"),
                              ({"open": None}, "partial_quotes")]:
            with self.subTest(updates=updates):
                self.assertIn(code, self.codes([self.changed(**updates)]))

    def test_missing_quotes_not_filled(self):
        row = self.changed(open=None, high=None, low=None, close=None, volume=0)
        issues = inspect_daily([row])
        self.assertIn("missing_quotes", {i.code for i in issues})
        self.assertFalse(any(i.severity == "error" for i in issues))
        self.assertIsNone(json.loads(row.payload_json)["close"])

    def test_duplicate_and_empty_data_rejected(self):
        self.assertIn("duplicate_revision", self.codes([self.rows[0], self.rows[0]]))
        self.assertIn("empty_dataset", self.codes([]))

    def test_future_daily_bar_rejected(self):
        row = replace(self.rows[0], event_at=timestamp("2025-02-01T00:00:00+09:00"),
                      payload_json=json.dumps({**json.loads(self.rows[0].payload_json), "session_date": "2025-02-01"}))
        self.assertIn("future_bar", self.codes([row]))

    def test_cursor_loop_rejected(self):
        response = Response(canonical({"data": [], "pagination_key": "loop"}), self.rows[0].ingested_at)
        with patch.object(self.transport, "get", return_value=response):
            with self.assertRaisesRegex(ValueError, "cursor"):
                acquire_daily(self.transport, "2025-01-06", self.root / "loop")

    def test_malformed_response_preserved_as_raw(self):
        with patch.object(self.transport, "get", return_value=Response(b'{"error":1}', self.rows[0].ingested_at)):
            with self.assertRaises(ValueError):
                acquire_daily(self.transport, "2025-01-06", self.root / "bad")
        self.assertEqual(len(list((self.root / "bad").rglob("*.json"))), 2)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = json.loads((ROOT / "config/research.json").read_text())
        self.config["storage"] = {key: str(self.root / key) for key in ("raw", "processed", "experiments")}
        self.config["provider"]["fixture"] = str(FIXTURE)
        self.path = self.root / "config.json"

    def save(self):
        self.path.write_bytes(canonical(self.config))

    def test_offline_pipeline_reproducible_without_api_key(self):
        self.save()
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
            first, second = run(self.path), run(self.path)
        self.assertEqual(first, second)
        manifest = json.loads(read_verified(Path(first["manifest"])))
        self.assertTrue(manifest["synthetic"])
        self.assertEqual(len(load_observations(Path(manifest["processed"]))), 2)
        self.assertEqual(first["warnings"], 1)

    def test_scope_and_credential_config_rejected(self):
        self.config["research"]["leverage"] = 2
        self.save()
        with self.assertRaises(ValueError):
            load_config(self.path)
        self.config["research"]["leverage"] = 1
        self.config["provider"]["api_key"] = "do-not-store"
        self.save()
        with self.assertRaises(ValueError):
            load_config(self.path)

    def test_replay_from_raw_and_detect_corruption(self):
        self.save()
        result = run(self.path)
        manifest_path = Path(result["manifest"])
        manifest = json.loads(read_verified(manifest_path))
        rebuilt = replay(manifest_path, self.root / "rebuilt")
        self.assertEqual(rebuilt.stem, manifest["processed_sha256"])
        with patch("ai_trading.replay.code_fingerprint", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                replay(manifest_path, self.root / "rebuilt")
        receipt_path = Path(manifest["raw_receipts"][0])
        receipt = json.loads(read_verified(receipt_path))
        body = receipt_path.parent.parent / "bodies" / (receipt["body_sha256"] + ".json")
        body.write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "hash"):
            replay(manifest_path, self.root / "rebuilt")
    def test_bad_quality_never_publishes_processed(self):
        fixture = json.loads(FIXTURE.read_text())
        fixture["pages"][0]["response"]["data"][0]["C"] = -1
        file = self.root / "bad.json"
        file.write_bytes(canonical(fixture))
        self.config["provider"]["fixture"] = str(file)
        self.save()
        result = run(self.path)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / "processed").exists())
        self.assertIsNone(json.loads(read_verified(Path(result["manifest"])))["processed"])

    def test_partial_fetch_never_publishes_processed(self):
        self.save()
        with patch.object(FixtureTransport, "get", side_effect=[Response(canonical({"data": [], "pagination_key": "next"}), timestamp("2025-01-07T00:00:00Z")), RuntimeError("interrupted")]):
            with self.assertRaises(RuntimeError):
                run(self.path)
        self.assertFalse((self.root / "processed").exists())
        self.assertTrue((self.root / "raw").exists())

    def test_live_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "JQUANTS_API_KEY"):
                JQuantsTransport()

    def test_live_http_contract_and_errors_without_network(self):
        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-secret"}):
            client = JQuantsTransport()
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data":[]}'
        with patch("ai_trading.providers.urlopen", return_value=response) as opened:
            result = client.get("/equities/bars/daily", {"date": "2025-01-06"})
            request = opened.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.jquants.com/v2/equities/bars/daily?date=2025-01-06")
            self.assertEqual(request.get_header("X-api-key"), "test-secret")
            self.assertEqual(result.body, b'{"data":[]}')
        for error in (HTTPError("url", 429, "test-secret", {}, None), URLError("test-secret")):
            with patch("ai_trading.providers.urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    client.get("/equities/bars/daily", {})
                self.assertNotIn("test-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
