"""Offline J-Quants protocol fixtures; no API key or network."""
import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_trading.collection import (PacedTransport, collect_session, main,
                                   purchaseable_lots, risk_config)
from ai_trading.providers import FixtureTransport, JQuantsError
from ai_trading.storage import canonical, read_verified

DAY = "2025-01-06"


def pages(*, missing=False, volume=100000, ingested=DAY + "T17:00:00+09:00"):
    return dict(synthetic=True, pages=[
        dict(path="/markets/calendar",
             params={"from": DAY, "to": "2025-01-07"}, ingested_at=ingested,
             response=dict(data=[dict(Date=DAY, HolDiv="1"),
                                 dict(Date="2025-01-07", HolDiv="1")])),
        dict(path="/equities/master", params={"date": DAY}, ingested_at=ingested,
             response=dict(data=[dict(Date=DAY, Code=c, ProdCat="011")
                                 for c in ("65010", "72030")])),
        dict(path="/equities/bars/daily", params={"date": DAY}, ingested_at=ingested,
             response=dict(data=[
                 dict(Date=DAY, Code=c, O=p, H=p, L=p, C=p, Vo=volume,
                      AdjFactor=1)
                 for c, p in (("65010", 150), ("72030", 250))
                 if not missing or c != "72030"]))])


def fixture(root, **kwargs):
    path = Path(root) / "pages.json"
    path.write_bytes(canonical(pages(**kwargs)))
    return FixtureTransport(path)


def collect(transport, root, *, refresh=False):
    config = risk_config(DAY, ("65010", "72030"))
    return collect_session(transport, root, DAY, ("6501", "7203"),
                           DAY, "2025-01-07", config, refresh=refresh,
                           provenance="synthetic_test")


class CollectionTests(unittest.TestCase):
    def test_archives_raw_and_reuses_complete_session_without_network(self):
        with tempfile.TemporaryDirectory() as root:
            transport = fixture(root)
            first = collect(transport, root)
            self.assertEqual(first["status"], "complete")
            self.assertEqual(first["temporal_use"], "observed_before_cutoff")
            self.assertEqual(len(transport.calls), 3)
            self.assertEqual(first["one_lot_preview"]["100000"]["symbol_count"], 1)
            self.assertEqual(first["one_lot_preview"]["200000"]["symbol_count"], 2)
            self.assertEqual(first["one_lot_preview"]["300000"]["symbol_count"], 2)
            for relative in first["raw_paths"].values():
                self.assertTrue(read_verified(Path(root) / relative))
            second = collect(transport, root)
            self.assertTrue(second["reused"])
            self.assertEqual(len(transport.calls), 3)
            # A fixture snapshot cannot satisfy an explicit live collection.
            live = collect_session(
                transport, root, DAY, ("6501", "7203"), DAY, "2025-01-07",
                risk_config(DAY, ("65010", "72030")), provenance="live_api")
            self.assertFalse(live["reused"])
            self.assertEqual(len(transport.calls), 6)
            refreshed = collect(transport, root, refresh=True)
            self.assertFalse(refreshed["reused"])
            self.assertEqual(len(transport.calls), 9)

    def test_missing_or_untradable_is_recorded_without_fill(self):
        with tempfile.TemporaryDirectory() as root:
            missing = fixture(root, missing=True)
            report = collect(missing, root)
            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(report["missing_symbols"], ["72030"])
            self.assertEqual(report["one_lot_preview"]["200000"]["symbol_count"], 1)
            collect(missing, root)
            self.assertEqual(len(missing.calls), 6)
        with tempfile.TemporaryDirectory() as root:
            halted = fixture(root, volume=0)
            report = collect(halted, root)
            self.assertEqual(report["unavailable_symbols"], ["65010", "72030"])
            self.assertEqual(report["one_lot_preview"]["300000"]["symbol_count"], 0)

    def test_late_acquisition_is_exploratory(self):
        with tempfile.TemporaryDirectory() as root:
            transport = fixture(root, ingested="2025-01-07T09:00:00+09:00")
            report = collect(transport, root)
            self.assertEqual(report["temporal_use"], "after_cutoff_exploratory")
            self.assertFalse(report["forward_ready"])

    def test_refresh_keeps_prior_receipts_and_integrity_is_checked(self):
        with tempfile.TemporaryDirectory() as root:
            first = collect(fixture(root), root)
            later = collect(fixture(root, ingested="2025-01-07T09:00:00+09:00"),
                            root, refresh=True)
            self.assertNotEqual(first["collection_path"], later["collection_path"])
            self.assertEqual(len(list((Path(root) / "collections").glob("*.json"))), 2)
            self.assertEqual(later["temporal_use"], "after_cutoff_exploratory")
            self.assertTrue(read_verified(Path(first["collection_path"])))
            reused = collect(fixture(root), root)
            self.assertEqual(reused["collection_path"], later["collection_path"])
            for record in (first, later):
                (Path(root) / record["raw_paths"]["daily"]).write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "hash"):
                collect(fixture(root), root)

    def test_one_lot_boundary_includes_costs_and_cash_floor(self):
        capped = risk_config(DAY, ("65010",))
        self.assertEqual(purchaseable_lots({"65010": 200}, capped)["100000"]["symbol_count"], 0)
        self.assertEqual(purchaseable_lots({"65010": 199}, capped)["100000"]["symbol_count"], 1)
        cash_limited = risk_config(DAY, ("65010",), max_position_weight="1")
        self.assertEqual(purchaseable_lots({"65010": 600}, cash_limited)["100000"]["symbol_count"], 0)

    def test_failed_request_keeps_raw_evidence_without_success_record(self):
        with tempfile.TemporaryDirectory() as root:
            source = fixture(root)
            original = source.get

            def fail_daily(path, params):
                if path == "/equities/bars/daily":
                    raise JQuantsError("http", 429)
                return original(path, params)

            source.get = fail_daily
            with self.assertRaises(JQuantsError):
                collect(source, root)
            self.assertFalse((Path(root) / "collections").exists())
            self.assertTrue(list((Path(root) / "jquants_v2" / "calendar" / "bundles").glob("*.json")))

    def test_pacing_and_no_retry_after_rate_limit(self):
        class Rejecting:
            def __init__(self):
                self.calls = 0

            def get(self, path, params):
                self.calls += 1
                raise JQuantsError("http", 429)

        now = [0.0]
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        source = Rejecting()
        paced = PacedTransport(source, clock=lambda: now[0], sleep=sleep)
        for _ in range(2):
            with self.assertRaises(JQuantsError):
                paced.get("/equities/master", {"date": DAY})
        self.assertEqual(source.calls, 2)
        self.assertEqual(waits, [12.5])
        with self.assertRaises(ValueError):
            PacedTransport(source, requests_per_minute=6)

    def test_cli_without_key_never_connects(self):
        env = dict(os.environ, PYTHONPATH="src", PYTHONUTF8="1", JQUANTS_API_KEY="")
        result = subprocess.run(
            [sys.executable, "-m", "ai_trading.collection", "--date", DAY,
             "--symbols", "7203", "--calendar-from", DAY, "--calendar-to", DAY,
             "--raw-root", "unused"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "auth_missing")

    def test_cli_mock_live_probe_uses_get_and_excludes_key(self):
        with tempfile.TemporaryDirectory() as root:
            source = fixture(root)
            output = io.StringIO()
            args = ["collection", "--date", DAY, "--symbols", "6501", "7203",
                    "--calendar-from", DAY, "--calendar-to", "2025-01-07",
                    "--raw-root", str(Path(root) / "raw")]
            with patch.dict(os.environ, JQUANTS_API_KEY="test-secret"), \
                    patch("sys.argv", args), \
                    patch("ai_trading.collection.JQuantsTransport", return_value=source), \
                    patch("ai_trading.collection.PacedTransport", side_effect=lambda source, rate: source), \
                    redirect_stdout(output):
                main()
            self.assertTrue(json.loads(output.getvalue())["ok"])
            self.assertEqual([path for path, _ in source.calls],
                             ["/markets/calendar", "/equities/master", "/equities/bars/daily"])
            self.assertNotIn("test-secret", output.getvalue())
            self.assertTrue(all(b"test-secret" not in path.read_bytes()
                                for path in (Path(root) / "raw").rglob("*.json")))


if __name__ == "__main__":
    unittest.main()
