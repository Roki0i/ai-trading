"""Offline J-Quants protocol fixtures; no API key or network."""
import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from ai_trading.collection import (PacedTransport, _check_access, _today_jst,
                                   collect_session, main, probe_session,
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
            self.assertEqual(first["temporal_use"], "historical_exploratory")
            self.assertEqual(first["decision_cutoff_relation"], "before")
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
            self.assertEqual(report["temporal_use"], "historical_exploratory")
            self.assertEqual(report["decision_cutoff_relation"], "at_or_after")
            self.assertFalse(report["forward_ready"])

    def test_refresh_keeps_prior_receipts_and_integrity_is_checked(self):
        with tempfile.TemporaryDirectory() as root:
            first = collect(fixture(root), root)
            later = collect(fixture(root, ingested="2025-01-07T09:00:00+09:00"),
                            root, refresh=True)
            self.assertNotEqual(first["collection_path"], later["collection_path"])
            self.assertEqual(len(list((Path(root) / "collections").glob("*.json"))), 2)
            self.assertEqual(later["temporal_use"], "historical_exploratory")
            self.assertEqual(later["decision_cutoff_relation"], "at_or_after")
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

    def test_free_plan_delay_rejects_recent_date_before_network(self):
        recent = (_today_jst() - timedelta(weeks=12) + timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "latest 12 weeks"):
            _check_access(recent, "free")
        with tempfile.TemporaryDirectory() as root:
            source = fixture(root)
            with self.assertRaisesRegex(ValueError, "latest 12 weeks"):
                probe_session(source, root, recent, "7203", plan="free")
            self.assertEqual(source.calls, [])
            self.assertFalse((Path(root) / "probes").exists())

    def test_bounded_probe_archives_three_responses_without_key(self):
        with tempfile.TemporaryDirectory() as root:
            spec = pages()
            spec["pages"][0]["params"] = {"from": DAY, "to": DAY}
            spec["pages"][0]["response"]["data"] = [dict(Date=DAY, HolDiv="1")]
            spec["pages"][1]["params"]["code"] = "72030"
            spec["pages"][1]["response"]["data"] = [dict(
                Date=DAY, Code="72030", ProdCat="011")]
            spec["pages"][2]["params"]["code"] = "72030"
            spec["pages"][2]["response"]["data"] = [r for r in
                spec["pages"][2]["response"]["data"] if r["Code"] == "72030"]
            path = Path(root) / "probe-pages.json"
            path.write_bytes(canonical(spec))
            source = FixtureTransport(path)
            report = probe_session(source, Path(root) / "raw", DAY, "7203", plan="free")
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["request_count"], 3)
            self.assertEqual(report["temporal_use"], "historical_exploratory")
            self.assertFalse(report["contract_verified"])
            self.assertEqual([p for p, _ in source.calls], [
                "/markets/calendar", "/equities/master", "/equities/bars/daily"])
            self.assertTrue(all(q.get("code") == "72030" for _, q in source.calls[1:]))
            self.assertTrue(all(read_verified(Path(root) / "raw" / p)
                                for p in report["raw_paths"].values()))
            self.assertTrue(read_verified(Path(report["probe_path"])))

    def test_bounded_probe_missing_data_is_incomplete(self):
        with tempfile.TemporaryDirectory() as root:
            spec = pages()
            spec["pages"][0]["params"] = {"from": DAY, "to": DAY}
            spec["pages"][0]["response"]["data"] = [dict(Date=DAY, HolDiv="1")]
            for page in spec["pages"][1:]:
                page["params"]["code"] = "72030"
                page["response"]["data"] = []
            path = Path(root) / "probe-pages.json"
            path.write_bytes(canonical(spec))
            report = probe_session(FixtureTransport(path), Path(root) / "raw",
                                   DAY, "7203", plan="unknown")
            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(report["master_rows"], 0)
            self.assertEqual(report["daily_rows"], 0)
            self.assertFalse(report["forward_ready"])

    def test_cli_bounded_probe_uses_get_and_never_prints_key(self):
        with tempfile.TemporaryDirectory() as root:
            spec = pages()
            spec["pages"][0]["params"] = {"from": DAY, "to": DAY}
            spec["pages"][0]["response"]["data"] = [dict(Date=DAY, HolDiv="1")]
            for page in spec["pages"][1:]:
                page["params"]["code"] = "72030"
                page["response"]["data"] = [r for r in page["response"]["data"]
                                            if r["Code"] == "72030"]
            path = Path(root) / "probe-pages.json"
            path.write_bytes(canonical(spec))
            source = FixtureTransport(path)
            output = io.StringIO()
            args = ["collection", "--probe", "--plan", "free", "--date", DAY,
                    "--symbols", "7203", "--calendar-from", DAY, "--calendar-to", DAY,
                    "--raw-root", str(Path(root) / "raw")]
            with patch.dict(os.environ, JQUANTS_API_KEY="test-secret"), \
                    patch("sys.argv", args), \
                    patch("ai_trading.collection.JQuantsTransport", return_value=source), \
                    patch("ai_trading.collection.PacedTransport", side_effect=lambda source, rate: source), \
                    redirect_stdout(output):
                main()
            self.assertEqual(json.loads(output.getvalue())["status"], "complete")
            self.assertEqual(len(source.calls), 3)
            self.assertNotIn("test-secret", output.getvalue())
            self.assertTrue(all(b"test-secret" not in p.read_bytes()
                                for p in (Path(root) / "raw").rglob("*.json")))

    def test_probe_rate_limit_has_no_success_summary(self):
        with tempfile.TemporaryDirectory() as root:
            source = fixture(root)
            source.get = lambda path, params: (_ for _ in ()).throw(JQuantsError("http", 429))
            with self.assertRaises(JQuantsError):
                probe_session(source, Path(root) / "raw", DAY, "7203")
            self.assertFalse((Path(root) / "raw" / "probes").exists())


if __name__ == "__main__":
    unittest.main()
