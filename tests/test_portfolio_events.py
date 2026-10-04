"""企業イベントMVPをfixtureだけで検証する。外部通信は禁止する。"""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from ai_trading.portfolio.events import CorporateEvent, EventConfig, JsonEventProvider, enrich_assessment
from ai_trading.portfolio.models import RuleConfig, PortfolioError, canonical
from ai_trading.portfolio.store import PortfolioStore
from ai_trading.portfolio.__main__ import main
from test_portfolio_assessment import AT, report, trade

FIXTURE = Path(__file__).parent/"fixtures/portfolio_events.json"


def event(**changes):
    values = dict(symbol="7203", event_type="earnings", event_date="2025-01-15",
                  announced_at="2025-01-07T00:00:00Z", source="fixture", status="scheduled", metadata={})
    values.update(changes)
    return CorporateEvent(**values)


def enriched(events, config=None):
    return enrich_assessment(report(), events, config)["assessments"][0]


class EventTests(unittest.TestCase):
    def test_earnings_exact_threshold(self):
        row = enriched([event()])
        self.assertEqual(row["event_flags"], ["earnings_soon"])
        self.assertEqual(row["events"][0]["days_until"], 7)
        self.assertTrue(row["events"][0]["is_upcoming"])

    def test_earnings_inside_threshold(self):
        self.assertEqual(enriched([event(event_date="2025-01-09")])["event_flags"], ["earnings_soon"])

    def test_outside_and_custom_threshold(self):
        self.assertEqual(enriched([event()], EventConfig(earnings_soon_days=6))["event_flags"], [])
        self.assertEqual(enriched([event(event_date="2025-01-16")])["event_flags"], [])
        self.assertEqual(enriched([event(event_date="2025-01-16")], EventConfig(earnings_soon_days=8))["event_flags"], ["earnings_soon"])

    def test_dividend(self):
        row = enriched([event(event_type="dividend")])
        self.assertEqual(row["event_flags"], ["dividend_soon"])

    def test_split(self):
        row = enriched([event(event_type="stock_split", event_date="2025-01-22")])
        self.assertEqual(row["event_flags"], ["stock_split_upcoming"])
        self.assertEqual(row["metrics"], report()["assessments"][0]["metrics"])

    def test_past_and_recent_boundary(self):
        for day, recent in (("2025-01-07", True), ("2025-01-01", True), ("2024-12-31", False)):
            with self.subTest(day=day):
                row = enriched([event(event_date=day, status="completed")])
                self.assertFalse(row["events"][0]["is_upcoming"])
                self.assertIs(row["events"][0]["is_recent"], recent)
                self.assertEqual(row["event_flags"], [])
                self.assertLess(row["events"][0]["days_until"], 0)

    def test_today_and_jst_boundary(self):
        base = report()
        base["as_of"] = "2025-01-08T15:00:00Z"
        row = enrich_assessment(base, [event(event_date="2025-01-09")])["assessments"][0]
        self.assertEqual(row["events"][0]["days_until"], 0)
        self.assertFalse(row["events"][0]["is_recent"])
        self.assertEqual(row["event_flags"], ["earnings_soon"])

    def test_stale_suppressed_metadata_retained(self):
        value = event(announced_at="2024-01-01T00:00:00Z")
        row = enriched([value])
        self.assertEqual(row["events"][0]["freshness_status"], "stale")
        self.assertEqual(row["events"][0]["announced_at"], value.announced_at)
        self.assertEqual(row["event_flags"], [])
        self.assertEqual(row["event_reasons"], [])

    def test_freshness_exact_boundary(self):
        for seconds, expected in ((86400, "fresh"), (86401, "stale")):
            row = enriched([event(announced_at=AT-timedelta(seconds=seconds))], EventConfig(max_announcement_age_days=1))
            self.assertEqual(row["events"][0]["freshness_status"], expected)

    def test_unknown_freshness_suppressed(self):
        row = enriched([event(announced_at=None)])
        self.assertEqual(row["events"][0]["freshness_status"], "unknown")
        self.assertEqual(row["event_flags"], [])

    def test_future_announcement_not_visible(self):
        row = enriched([event(announced_at=AT+timedelta(seconds=1))])
        self.assertEqual(row["events"], [])

    def test_cancelled_completed_unknown_not_flagged(self):
        for status in ("cancelled", "completed", "unknown"):
            with self.subTest(status=status):
                row = enriched([event(status=status)])
                self.assertEqual(row["event_flags"], [])
                self.assertFalse(row["events"][0]["is_upcoming"])

    def test_no_event(self):
        row = enriched([])
        self.assertEqual((row["events"], row["event_flags"], row["event_reasons"]), ([], [], []))

    def test_multiple_events_unique_flags_all_reasons(self):
        row = enriched([event(), event(event_date="2025-01-14"), event(event_type="dividend")])
        self.assertEqual(len(row["events"]), 3)
        self.assertEqual(len(row["event_flags"]), 2)
        self.assertEqual(len(row["event_reasons"]), 3)

    def test_multiple_symbols(self):
        base = report(transactions=[trade(), trade("two", symbol="NVDA", currency="USD")])
        result = enrich_assessment(base, [event(), event(symbol="NVDA", event_type="stock_split")])
        rows = {r["symbol"]: r for r in result["assessments"]}
        self.assertEqual(rows["7203"]["event_flags"], ["earnings_soon"])
        self.assertEqual(rows["NVDA"]["event_flags"], ["stock_split_upcoming"])

    def test_reason_structure(self):
        reason = enriched([event()])["event_reasons"][0]
        self.assertEqual((reason["rule"], reason["days_until"], reason["threshold_days"],
                          reason["event_date"], reason["source"]), ("earnings_soon", 7, 7, "2025-01-15", "fixture"))

    def test_config_backward_compatibility(self):
        legacy = RuleConfig().to_dict()
        self.assertNotIn("events", legacy)
        self.assertEqual(RuleConfig.from_dict(legacy).to_dict(), legacy)
        config = RuleConfig.from_dict(dict(legacy, events={"earnings_soon_days": 3}))
        self.assertEqual(config.events.earnings_soon_days, 3)
        self.assertEqual(config.events.dividend_soon_days, 7)
        self.assertEqual(RuleConfig.from_dict(config.to_dict()), config)

    def test_invalid_config(self):
        for value in ({"unknown": 1}, {"earnings_soon_days": -1}, {"recent_days": True}, {"dividend_soon_days": "7"}):
            with self.subTest(value=value), self.assertRaises(PortfolioError):
                RuleConfig(events=value)

    def test_model_validation(self):
        for changes in ({"event_type": "news"}, {"event_date": "2025-02-30"}, {"event_date": "20250115"},
                        {"announced_at": "2025-01-07"}, {"status": "invalid"}, {"symbol": "bad;symbol"},
                        {"metadata": {"ratio": 2}}, {"metadata": {"bad": "x\n"}}):
            with self.subTest(changes=changes), self.assertRaises(PortfolioError):
                event(**changes)

    def test_immutable_inputs_and_existing_fields(self):
        base = report()
        before = copy.deepcopy(base)
        result = enrich_assessment(base, [event()])
        self.assertEqual(base, before)
        for key, value in base["assessments"][0].items():
            self.assertEqual(result["assessments"][0][key], value)
        self.assertEqual(result["portfolio"], base["portfolio"])
        self.assertEqual(result["schema_version"], 1)

    def test_fixture_filter_duplicate_and_limits(self):
        provider = JsonEventProvider.from_file(FIXTURE)
        self.assertEqual(len(provider.events("7203")), 3)
        self.assertEqual(provider.events("NVDA"), [])
        with self.assertRaisesRegex(PortfolioError, "duplicate_event"):
            JsonEventProvider([event(), event()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"bad.json"
            for value in ({"schema_version": True, "events": []}, {"schema_version": 1, "events": [{}]}):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(PortfolioError):
                    JsonEventProvider.from_file(path)
            path.write_bytes(b" "*(1024*1024+1))
            with self.assertRaisesRegex(PortfolioError, "event_fixture_too_large"):
                JsonEventProvider.from_file(path)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/"portfolio.sqlite3"
        self.store = PortfolioStore.initialize(self.db)
        self.store.add(trade())
        self.before = self.db.read_bytes()
        guard = patch("ai_trading.providers.urlopen", side_effect=AssertionError("external API forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def cli(self, *args, machine=True):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(["assess", "--db", str(self.db), "--as-of", "2025-01-08T09:00:00Z",
                         *(["--json"] if machine else []), *args])
        self.assertEqual(code, 0, stream.getvalue())
        self.assertEqual(self.before, self.db.read_bytes())
        return json.loads(stream.getvalue()) if machine else stream.getvalue()

    def test_cli_json_fixture(self):
        result = self.cli("--events-fixture", str(FIXTURE))
        self.assertEqual(result["schema_version"], 1)
        row = result["assessments"][0]
        self.assertEqual(set(row["event_flags"]), {"earnings_soon", "dividend_soon", "stock_split_upcoming"})
        self.assertEqual(row["market_data_status"], "missing")
        self.assertEqual(row["severity"], "warning")

    def test_cli_human(self):
        output = self.cli("--events-fixture", str(FIXTURE), machine=False)
        for expected in ("決算", "配当", "株式分割", "2025-01-15", "7日後", "fresh"):
            self.assertIn(expected, output)

    def test_cli_no_fixture(self):
        self.assertEqual(self.cli()["assessments"][0]["events"], [])

    def test_existing_db_and_config_reopen(self):
        self.assertIsNone(PortfolioStore(self.db).read()[1].events)
        self.store.configure(RuleConfig(events={"earnings_soon_days": 2}))
        self.before = self.db.read_bytes()
        config = PortfolioStore(self.db).read()[1]
        self.assertEqual(config.events.earnings_soon_days, 2)
        row = self.cli("--events-fixture", str(FIXTURE))["assessments"][0]
        self.assertNotIn("earnings_soon", row["event_flags"])
        self.assertIn("dividend_soon", row["event_flags"])


if __name__ == "__main__":
    unittest.main()
