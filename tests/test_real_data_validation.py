"""実TransportからAssessmentまでの経路をmock HTTPで検証する。実通信は禁止。"""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch

from ai_trading.providers import JQuantsTransport, JQuantsError
from ai_trading.portfolio.__main__ import main
from ai_trading.portfolio.store import PortfolioStore
from ai_trading.portfolio.models import RuleConfig
from test_portfolio_market import AT, FIXTURE, trade

TOKEN = "unit-test-placeholder-not-real-credential"


class RealPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/"portfolio.sqlite3"
        store = PortfolioStore.initialize(self.db)
        store.add(trade())
        store.configure(RuleConfig(take_profit_pct="0.5"))
        self.before = self.db.read_bytes()
        self.pages = json.loads(FIXTURE.read_text(encoding="utf-8"))["pages"]
        self.replies = []
        self.calls = []
        env = patch.dict(os.environ, {"JQUANTS_API_KEY": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        clock = patch("ai_trading.providers.datetime")
        clock.start().now.return_value = AT
        self.addCleanup(clock.stop)
        network = patch("ai_trading.providers.urlopen", side_effect=self.http)
        self.opened = network.start()
        self.addCleanup(network.stop)

    def http(self, request, timeout):
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 30)
        self.assertEqual(request.get_header("X-api-key"), TOKEN)
        self.assertNotIn(TOKEN, request.full_url)
        url = urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.netloc), ("https", "api.jquants.com"))
        path = url.path.removeprefix("/v2")
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((path, params))
        for page in self.pages:
            if (page["path"], page["params"]) == (path, params):
                reply = io.BytesIO(json.dumps(page["response"]).encode())
                self.replies.append(reply)
                return reply
        raise AssertionError("unexpected fixture request")

    def cli(self, *, expected=0, command="assess", extra=(), fixture=False):
        args = [command, "--db", str(self.db), "--json", "--market-provider",
                "jquants-fixture" if fixture else "jquants", "--market-lookback-days", "7",
                "--as-of", "2025-01-08T09:00:00Z", *extra]
        if fixture:
            args += ["--market-fixture", str(FIXTURE)]
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(args)
        self.assertEqual(code, expected, stream.getvalue())
        self.assertNotIn(TOKEN, stream.getvalue())
        self.assertEqual(self.db.read_bytes(), self.before)
        self.assertTrue(all(r.closed for r in self.replies))
        return json.loads(stream.getvalue())

    def test_real_transport_parsing_and_provenance(self):
        result = self.cli()
        row = result["assessments"][0]
        self.assertEqual(row["metrics"]["current_price"], "2520")
        self.assertEqual(row["market_data_status"], "fresh")
        metadata = row["market_snapshot"]
        self.assertEqual(metadata["data_date"], "2025-01-08")
        self.assertEqual(metadata["as_of"], "2025-01-07T15:00:00.000000Z")
        self.assertEqual(metadata["ingested_at"], "2025-01-08T09:00:00.000000Z")
        self.assertTrue(metadata["source"].startswith("jquants_v2:daily_close:"))
        self.assertEqual(len(metadata["source"].rsplit(":", 1)[1]), 64)
        self.assertEqual(row["currency"], "JPY")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(row["events"], [])
        self.assertEqual(result["schema_version"], 1)

    def test_previous_close_and_latest_unsorted_bars(self):
        self.pages[2]["response"]["data"].reverse()
        result = self.cli(command="status")
        row = result["data"]["positions"][0]
        self.assertEqual(row["snapshot"]["previous_close"], "2510")
        self.assertEqual(row["current_price"], "2520")

    def test_auth_missing_zero_requests(self):
        with patch.dict(os.environ, {"JQUANTS_API_KEY": ""}):
            result = self.cli(expected=2)
        self.assertEqual(result["error"]["code"], "market_auth_missing")
        self.opened.assert_not_called()

    def http_failure(self, status, expected):
        response = io.BytesIO(TOKEN.encode())
        self.opened.side_effect = HTTPError("https://example.invalid/"+TOKEN, status, TOKEN,
                                            {"sensitive": TOKEN}, response)
        result = self.cli(expected=2)
        self.assertEqual(result["error"]["code"], expected)
        self.assertEqual(self.opened.call_count, 1)
        self.assertTrue(response.closed)
        self.assertNotIn("assessments", result)

    def test_401(self):
        self.http_failure(401, "market_auth_failed")

    def test_403(self):
        self.http_failure(403, "market_forbidden")

    def test_429_stops_without_retry(self):
        self.http_failure(429, "market_rate_limited")

    def test_other_http_error(self):
        self.http_failure(500, "market_http_error")

    def test_timeout(self):
        self.opened.side_effect = TimeoutError(TOKEN)
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_timeout")
        self.assertEqual(self.opened.call_count, 1)

    def test_wrapped_timeout(self):
        self.opened.side_effect = URLError(TimeoutError(TOKEN))
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_timeout")

    def test_network_failure(self):
        self.opened.side_effect = URLError(TOKEN)
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_network_error")

    def test_reset_and_incomplete_read(self):
        for exc in (ConnectionResetError(TOKEN), IncompleteRead(TOKEN.encode())):
            with self.subTest(kind=type(exc).__name__):
                self.opened.side_effect = exc
                self.assertEqual(self.cli(expected=2)["error"]["code"], "market_network_error")

    def test_read_failure_closes_reply(self):
        class Broken(io.BytesIO):
            def read(self, *args):
                raise TimeoutError(TOKEN)
        reply = Broken()
        self.opened.side_effect = None
        self.opened.return_value = reply
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_timeout")
        self.assertTrue(reply.closed)

    def test_malformed_json(self):
        self.opened.side_effect = lambda *a, **k: io.BytesIO(b'not-json')
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_response_invalid")

    def test_malformed_schema(self):
        self.pages[0]["response"] = {"data": [None]}
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_response_invalid")

    def test_master_mismatch(self):
        self.pages[1]["response"]["data"][0]["Code"] = "99990"
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_master_invalid")

    def test_incomplete_master(self):
        self.pages[1]["response"]["data"][0].pop("Date")
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_master_invalid")

    def test_unknown_symbol(self):
        self.pages[1]["response"]["data"] = []
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_symbol_not_found")
        self.assertEqual(len(self.calls), 2)

    def test_calendar_gap(self):
        self.pages[0]["response"]["data"].pop()
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_calendar_invalid")

    def test_missing_daily(self):
        self.pages[2]["response"]["data"] = []
        row = self.cli()["assessments"][0]
        self.assertEqual(row["market_data_status"], "missing")
        self.assertIsNone(row["metrics"]["current_price"])

    def test_missing_latest_never_backfills(self):
        self.pages[2]["response"]["data"][-1].update(O=None,H=None,L=None,C=None,Vo=None)
        row = self.cli()["assessments"][0]
        self.assertEqual(row["market_data_status"], "missing")

    def test_stale_data_retains_ingestion_and_suppresses_flags(self):
        self.pages[2]["response"]["data"] = self.pages[2]["response"]["data"][:1]
        row = self.cli()["assessments"][0]
        self.assertEqual(row["market_data_status"], "stale")
        self.assertEqual(row["market_snapshot"]["data_date"], "2025-01-06")
        self.assertEqual(row["market_snapshot"]["ingested_at"], "2025-01-08T09:00:00.000000Z")
        self.assertEqual(row["assessment_flags"], [])

    def test_live_fixture_json_contract_same(self):
        live, fixture = self.cli(), self.cli(fixture=True)
        self.assertEqual(set(live), set(fixture))
        a,b = live["assessments"][0],fixture["assessments"][0]
        self.assertEqual(set(a), set(b))
        self.assertEqual(a["metrics"], b["metrics"])
        self.assertEqual(set(a["market_snapshot"]), set(b["market_snapshot"]))
        self.assertEqual(a["event_flags"], b["event_flags"])

    def test_status_alerts_kept(self):
        for command in ("status", "alerts"):
            self.assertIn("data", self.cli(command=command))

    def test_endpoint_allowlist_unchanged(self):
        with self.assertRaises(ValueError):
            JQuantsTransport().get("/fins/dividend", {"code": "72030"})
        self.opened.assert_not_called()

    def test_credential_reflection_rejected(self):
        self.pages[0]["response"]["unexpected"] = TOKEN
        self.assertEqual(self.cli(expected=2)["error"]["code"], "market_provider_error")


if __name__ == "__main__":
    unittest.main()
