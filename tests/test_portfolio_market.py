"""Phase 8の日足adapterを架空レスポンスで検証する。実APIは常に禁止する。"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from ai_trading.providers import Response
from ai_trading.portfolio import (Transaction, PortfolioStore, MarketSnapshot, RuleConfig,
                                 PortfolioError, replay, status, alerts, JsonSnapshotProvider)
from ai_trading.portfolio.models import instant
from ai_trading.portfolio.jquants import JQuantsSnapshotProvider, prepare_provider

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT/"tests/fixtures/portfolio_market.json"
AT = instant("2025-01-08T09:00:00Z")


class FakeTransport:
    def __init__(self):
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.calls = []

    def get(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        for page in self.data["pages"]:
            if page["path"] == endpoint and page["params"] == params:
                return Response(json.dumps(page["response"], ensure_ascii=False).encode("utf-8"),
                                instant(page["ingested_at"]))
        raise RuntimeError("unexpected fixture request")


def trade(identifier="one", symbol="7203", currency="JPY"):
    return Transaction(identifier, symbol, "buy", "100", "2500", "0", currency,
                       "2025-01-01", "", AT)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("ai_trading.providers.urlopen", side_effect=AssertionError("real API forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.fake = FakeTransport()
        self.provider = JQuantsSnapshotProvider(self.fake, end_date="2025-01-08",
                                                lookback_days=7, fixture=True)

    def snapshot(self):
        return self.provider.snapshot("7203", "JPY")

    def test_valid_snapshot_metadata_and_previous_close(self):
        value = self.snapshot()
        self.assertEqual(value.price, Decimal(2520))
        self.assertEqual(value.previous_close, Decimal(2510))
        self.assertEqual(value.symbol, "7203")
        self.assertEqual(value.as_of, instant("2025-01-08T00:00:00+09:00"))
        self.assertEqual(value.data_date, "2025-01-08")
        self.assertEqual(value.market, "TSE")
        self.assertEqual(value.ingested_at, instant("2025-01-08T08:00:00Z"))
        self.assertTrue(value.source.startswith("fixture_jquants_v2:daily_close:"))
        self.assertEqual(len(value.source.rsplit(":",1)[1]), 64)
        self.assertEqual(len(self.fake.calls), 3)

    def test_multi_symbol_japanese_alphanumeric_and_cache(self):
        self.snapshot()
        value = self.provider.snapshot("130A", "JPY")
        self.assertEqual(value.price, 120)
        self.assertEqual(value.symbol, "130A")
        self.assertEqual(len(self.fake.calls), 5)
        self.snapshot()
        self.assertEqual(len(self.fake.calls), 5)

    def test_five_digit_symbol(self):
        value = self.provider.snapshot("72030", "JPY")
        self.assertEqual(value.symbol, "72030")
        self.assertEqual(value.price, 2520)

    def test_wrong_currency_and_invalid_symbol_do_not_call_transport(self):
        for symbol, currency in (("NVDA","USD"),("7203","USD"),("not;code","JPY"),("トヨタ","JPY")):
            with self.subTest(symbol=symbol,currency=currency), self.assertRaises(PortfolioError):
                self.provider.snapshot(symbol,currency)
        self.assertEqual(self.fake.calls, [])

    def test_missing_symbol_is_explicit_error(self):
        self.fake.data["pages"][1]["response"]["data"] = []
        with self.assertRaisesRegex(PortfolioError,"market_symbol_not_found"):
            self.snapshot()
        self.assertEqual(len(self.fake.calls), 2)

    def test_wrong_master_date_code_or_product_rejected(self):
        original = copy.deepcopy(self.fake.data)
        for field, value, expected in (("Date","2025-01-09","market_master_invalid"),
                ("Code","99990","market_master_invalid"),("ProdCat","999","market_instrument_unsupported"),
                ("ProdCat",None,"market_instrument_unsupported")):
            with self.subTest(field=field):
                self.fake.data = copy.deepcopy(original)
                self.fake.data["pages"][1]["response"]["data"][0][field] = value
                with self.assertRaisesRegex(PortfolioError,expected):
                    self.snapshot()

    def test_latest_missing_price_is_not_backfilled(self):
        self.fake.data["pages"][2]["response"]["data"][-1].update(O=None,H=None,L=None,C=None,Vo=0)
        self.assertIsNone(self.snapshot())
        report = status([trade()], self.provider, RuleConfig(), AT)
        self.assertEqual(report["positions"][0]["valuation_status"],"missing")

    def test_empty_daily_is_missing_not_successful_price(self):
        self.fake.data["pages"][2]["response"]["data"] = []
        self.assertIsNone(self.snapshot())

    def test_latest_available_daily_date_and_stale_alert(self):
        self.fake.data["pages"][2]["response"]["data"] = self.fake.data["pages"][2]["response"]["data"][:1]
        value = self.snapshot()
        self.assertEqual(value.data_date,"2025-01-06")
        config = RuleConfig(take_profit_pct="20", daily_move_pct="7")
        report = status([trade()],self.provider,config,AT)
        row = report["positions"][0]
        self.assertEqual(row["valuation_status"],"stale")
        self.assertTrue(row["stale"])
        self.assertIsNone(row["unrealized_pnl"])
        self.assertTrue(all(a["stale"] and a["triggered"] is None for a in alerts(report,config)["alerts"]))

    def test_stale_threshold_boundary(self):
        seconds = int((AT-self.snapshot().as_of).total_seconds())
        for threshold, expected in ((seconds,"ok"),(seconds-1,"stale")):
            with self.subTest(threshold=threshold):
                report = status([trade()],self.provider,RuleConfig(max_snapshot_age_seconds=threshold),AT)
                self.assertEqual(report["positions"][0]["valuation_status"],expected)

    def test_recent_ingestion_does_not_refresh_old_data_date(self):
        self.fake.data["pages"][2]["response"]["data"] = self.fake.data["pages"][2]["response"]["data"][:1]
        self.fake.data["pages"][2]["ingested_at"] = "2025-01-08T08:59:59Z"
        row = status([trade()],self.provider,RuleConfig(),AT)["positions"][0]
        self.assertEqual(row["valuation_status"],"stale")

    def test_future_ingestion_not_available_for_historical_evaluation(self):
        self.fake.data["pages"][2]["ingested_at"] = "2025-01-08T09:01:00Z"
        row = status([trade()],self.provider,RuleConfig(),AT)["positions"][0]
        self.assertEqual(row["valuation_status"],"future")
        self.assertIsNone(row["current_price"])

    def test_missing_previous_session_not_replaced_with_older_close(self):
        self.fake.data["pages"][2]["response"]["data"].pop(1)
        self.assertIsNone(self.snapshot().previous_close)

    def test_null_previous_close_is_unavailable(self):
        self.fake.data["pages"][2]["response"]["data"][-2].update(O=None,H=None,L=None,C=None,Vo=0)
        self.assertIsNone(self.snapshot().previous_close)

    def test_corporate_action_requires_review_no_adjusted_price_substitution(self):
        self.fake.data["pages"][2]["response"]["data"][-1].update(AdjFactor=0.5,AdjC=1260)
        with self.assertRaisesRegex(PortfolioError,"market_corporate_action_requires_review"):
            self.snapshot()

    def test_missing_adjustment_metadata_is_explicit(self):
        self.fake.data["pages"][2]["response"]["data"][-1].pop("AdjFactor")
        with self.assertRaisesRegex(PortfolioError,"market_adjustment_unknown"):
            self.snapshot()

    def test_malformed_daily_rows_rejected(self):
        original = copy.deepcopy(self.fake.data)
        for field,value in (("C","2520"),("C",-1),("C",True),("C",0),
                            ("Date","2025-01-09"),("Date","2025-01-05"),("Code","99990")):
            with self.subTest(field=field,value=value):
                self.fake.data = copy.deepcopy(original)
                self.fake.data["pages"][2]["response"]["data"][-1][field] = value
                with self.assertRaisesRegex(PortfolioError,"market_daily_invalid"):
                    self.snapshot()

    def test_existing_ohlc_and_volume_quality_checks(self):
        original = copy.deepcopy(self.fake.data)
        for field, value in (("H",1),("L",9999),("Vo",-1),("O",None)):
            with self.subTest(field=field):
                self.fake.data = copy.deepcopy(original)
                self.fake.data["pages"][2]["response"]["data"][-1][field] = value
                with self.assertRaisesRegex(PortfolioError,"market_daily_invalid"):
                    self.snapshot()

    def test_previous_session_across_weekend(self):
        from datetime import date
        fake = FakeTransport()
        days = [(date(2025,1,14)+timedelta(days=i)) for i in range(7)]
        fake.data["pages"][0]["params"] = {"from":"2025-01-14","to":"2025-01-20"}
        fake.data["pages"][0]["response"]["data"] = [
            dict(Date=d.isoformat(),HolDiv="1" if d.weekday()<5 else "0") for d in days]
        fake.data["pages"][1]["params"]["date"] = "2025-01-20"
        fake.data["pages"][1]["response"]["data"][0]["Date"] = "2025-01-20"
        fake.data["pages"][2]["params"].update({"from":"2025-01-14","to":"2025-01-20"})
        for row, day in zip(fake.data["pages"][2]["response"]["data"],
                            ("2025-01-16","2025-01-17","2025-01-20")):
            row["Date"] = day
        for page in fake.data["pages"]:
            page["ingested_at"] = "2025-01-20T08:00:00Z"
        value = JQuantsSnapshotProvider(fake,end_date="2025-01-20",lookback_days=7).snapshot("7203","JPY")
        self.assertEqual(value.previous_close,2510)
        self.assertEqual(value.data_date,"2025-01-20")

    def test_duplicate_daily_is_not_silently_selected(self):
        self.fake.data["pages"][2]["response"]["data"].append(
            copy.deepcopy(self.fake.data["pages"][2]["response"]["data"][-1]))
        with self.assertRaisesRegex(PortfolioError,"market_daily_invalid"):
            self.snapshot()

    def test_provider_exception_not_reflected(self):
        self.fake.get = lambda *args: (_ for _ in ()).throw(RuntimeError("secret-key-EXAMPLE"))
        with self.assertRaises(PortfolioError) as caught:
            self.snapshot()
        self.assertEqual(str(caught.exception),"market_provider_error")

    def test_invalid_json_and_response_shape(self):
        for body in (b"not-json", b"[]", b'{"data":[null]}', b'{"data":[],"pagination_key":true}',
                     b'{"data":[],"data":[]}'):
            with self.subTest(body=body):
                self.fake.get = lambda *args: Response(body,AT)
                with self.assertRaisesRegex(PortfolioError,"market_response_invalid"):
                    self.snapshot()

    def test_response_size_limit(self):
        self.fake.get = lambda *args: Response(b" "*(4*1024*1024+1),AT)
        with self.assertRaisesRegex(PortfolioError,"market_response_too_large"):
            self.snapshot()

    def test_calendar_coverage_and_duplicate_day(self):
        for mode in ("gap","duplicate"):
            fake = FakeTransport()
            rows = fake.data["pages"][0]["response"]["data"]
            rows.pop() if mode=="gap" else rows.append(copy.deepcopy(rows[-1]))
            provider = JQuantsSnapshotProvider(fake,end_date="2025-01-08",lookback_days=7)
            with self.subTest(mode=mode),self.assertRaisesRegex(PortfolioError,"market_calendar_invalid"):
                provider.snapshot("7203","JPY")

    def test_pagination_and_repeated_cursor(self):
        page = self.fake.data["pages"][2]
        row = page["response"]["data"].pop()
        page["response"]["pagination_key"] = "next"
        extra = copy.deepcopy(page)
        extra["params"]["pagination_key"] = "next"
        extra["response"] = {"data":[row]}
        self.fake.data["pages"].append(extra)
        self.assertEqual(self.snapshot().price,2520)
        self.assertEqual(len(self.fake.calls),4)
        self.provider.cache.clear()
        extra["response"]["pagination_key"] = "next"
        with self.assertRaisesRegex(PortfolioError,"market_response_invalid"):
            self.snapshot()

    def test_live_auth_missing_no_network_or_implicit_fixture(self):
        with patch.dict(os.environ,{},clear=True), patch(
                "ai_trading.portfolio.jquants.JQuantsTransport") as factory:
            with self.assertRaisesRegex(PortfolioError,"market_auth_missing"):
                prepare_provider(replay([trade()]),at=AT)
            factory.assert_not_called()

    def test_all_currencies_preflight_before_any_request(self):
        with patch("ai_trading.portfolio.jquants.JQuantsTransport") as factory:
            with self.assertRaisesRegex(PortfolioError,"market_currency_unsupported"):
                prepare_provider(replay([trade(),trade("two","NVDA","USD")]),at=AT)
            factory.assert_not_called()

    def test_fake_live_transport_uses_same_validation(self):
        with patch.dict(os.environ,{"JQUANTS_API_KEY":"not-a-real-key"}), patch(
                "ai_trading.portfolio.jquants.JQuantsTransport",return_value=self.fake):
            provider = prepare_provider(replay([trade()]),at=AT,lookback_days=7)
        self.assertTrue(provider.snapshot("7203","JPY").source.startswith("jquants_v2:daily_close:"))

    def test_legacy_and_extended_snapshot_json_roundtrip(self):
        value = self.snapshot()
        self.assertEqual(MarketSnapshot(**value.to_dict()),value)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"snap.json"
            path.write_text(json.dumps({"schema_version":1,"snapshots":[value.to_dict()]}),encoding="utf-8")
            self.assertEqual(JsonSnapshotProvider.from_file(path).snapshot("7203","JPY"),value)
        legacy=MarketSnapshot("7203","100",None,"JPY",AT,"manual")
        self.assertEqual(set(legacy.to_dict()),{"symbol","price","previous_close","currency","as_of","source"})


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/"portfolio.sqlite3"
        store=PortfolioStore.initialize(self.db)
        store.add(trade())
        store.configure(RuleConfig(take_profit_pct="0.5",daily_move_pct="7"))
        self.before=self.db.read_bytes()

    def cli(self,command,*args,code=0):
        env=dict(os.environ,PYTHONPATH=str(ROOT/"src"),PYTHONUTF8="1")
        env.pop("JQUANTS_API_KEY",None)
        result=subprocess.run([sys.executable,"-X","utf8","-m","ai_trading.portfolio",
            "--db",str(self.db),"--json",command,*args],cwd=ROOT,env=env,capture_output=True,
            text=True,encoding="utf-8",timeout=20)
        self.assertEqual(result.returncode,code,result.stdout+result.stderr)
        self.assertEqual(result.stderr,"")
        self.assertEqual(self.db.read_bytes(),self.before)
        return json.loads(result.stdout)

    def test_status_and_alerts_fixture_json_contract(self):
        args=("--market-provider","jquants-fixture","--market-fixture",str(FIXTURE),
              "--market-lookback-days","7","--as-of","2025-01-08T09:00:00Z")
        for command in ("status","alerts"):
            report=self.cli(command,*args)
            self.assertTrue(report["ok"])
            self.assertEqual(report["schema_version"],1)
            self.assertIn("generated_at",report)
            row=report["data"]["positions"][0]
            self.assertEqual(row["current_price"],"2520")
            self.assertEqual(row["unrealized_pnl"],"2000")
            self.assertEqual(row["snapshot"]["data_date"],"2025-01-08")
            self.assertTrue(report["data"]["alerts"][0]["triggered"])

    def test_auth_missing_json_error(self):
        result=self.cli("status","--market-provider","jquants",code=2)
        self.assertEqual(result["error"]["code"],"market_auth_missing")

    def test_invalid_provider_combinations(self):
        for args in (("--market-fixture",str(FIXTURE)),
                     ("--market-provider","jquants-fixture"),
                     ("--market-provider","jquants","--market-fixture",str(FIXTURE)),
                     ("--market-provider","jquants","--snapshot",str(FIXTURE))):
            with self.subTest(args=args):
                self.assertEqual(self.cli("status",*args,code=2)["error"]["code"],"invalid_arguments")

    def test_cli_threshold_override_does_not_write_config(self):
        report=self.cli("alerts","--market-provider","jquants-fixture","--market-fixture",str(FIXTURE),
              "--market-lookback-days","7","--as-of","2025-01-08T09:00:00Z","--max-price-age-seconds","60")
        self.assertEqual(report["data"]["positions"][0]["valuation_status"],"stale")
        self.assertTrue(report["data"]["alerts"][0]["stale"])
        self.assertEqual(PortfolioStore(self.db).read()[1].max_snapshot_age_seconds,86400)


if __name__ == "__main__":
    unittest.main()
