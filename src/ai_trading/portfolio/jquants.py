"""日足終値専用adapter。研究のTransportと検証を再利用し、原本は永続化しない。"""
import json
import os
import re
from datetime import date, timedelta, timezone
from decimal import Decimal

from ..providers import (JQuantsTransport, JQuantsError, FixtureTransport, validate_daily_row,
                         calendar_from_raw, normalize_daily)
from ..quality import inspect_daily
from ..validation import coverage
from ..storage import digest, canonical as research_json
from .models import MarketSnapshot, PortfolioError, instant, stamp, object_from_json, decimal

JST = timezone(timedelta(hours=9))
MAX_PAGES = 4
MAX_BODY = 4 * 1024 * 1024


def code_for(symbol, currency):
    """4桁は普通株式の5桁コードへ変換。台帳のsymbolは変更しない。"""
    if currency != "JPY":
        raise PortfolioError("market_currency_unsupported")
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9][0-9A-Z]{3}0?", symbol):
        raise PortfolioError("market_symbol_invalid")
    return symbol + "0" if len(symbol) == 4 else symbol


class JQuantsSnapshotProvider:
    """銘柄ごとにmaster/日足、全銘柄共通でcalendarを取得する呼出し単位のcache。"""
    def __init__(self, transport, *, end_date, lookback_days=90, fixture=False):
        if type(lookback_days) is not int or not 2 <= lookback_days <= 366:
            raise PortfolioError("market_lookback_invalid")
        try:
            self.end = date.fromisoformat(end_date)
            if self.end.isoformat() != end_date:
                raise ValueError()
            self.start = self.end - timedelta(days=lookback_days - 1)
        except (ValueError, TypeError, OverflowError):
            raise PortfolioError("market_date_invalid") from None
        self.transport = transport
        self.fixture = fixture
        self.cache = {}
        self.calendar = None
        self.calendar_bundle = None

    def _fetch(self, endpoint, params):
        records, rows, seen = [], [], set()
        query = dict(params)
        for _ in range(MAX_PAGES):
            try:
                response = self.transport.get(endpoint, query)
            except JQuantsError as exc:
                # 固定codeだけを公開し、429を含む全失敗で再試行せず停止する。
                if exc.kind == "http":
                    code = {401: "market_auth_failed", 403: "market_forbidden",
                            429: "market_rate_limited"}.get(exc.http_status, "market_http_error")
                else:
                    code = "market_timeout" if exc.kind == "timeout" else "market_network_error"
                raise PortfolioError(code) from None
            except Exception:
                # Provider例外に含まれるbody/header/credentialを外部へ反射しない。
                raise PortfolioError("market_provider_error") from None
            try:
                if not isinstance(response.body, bytes) or len(response.body) > MAX_BODY:
                    raise PortfolioError("market_response_too_large")
                ingested = stamp(response.ingested_at)
                payload = object_from_json(response.body)
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError()
                if any(not isinstance(row, dict) for row in payload["data"]):
                    raise ValueError()
                rows.extend(payload["data"])
                if len(rows) > 10000:
                    raise PortfolioError("market_response_too_large")
                receipt = dict(schema_version=1, source="jquants_v2", endpoint=endpoint,
                               params=dict(query), ingested_at=ingested, body_sha256=digest(response.body))
                records.append(dict(receipt=receipt, body=response.body.decode("utf-8")))
                cursor = payload.get("pagination_key")
                if cursor is None or cursor == "":
                    return rows, dict(schema_version=1, provider="jquants_v2", receipts=records)
                if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen:
                    raise ValueError()
                seen.add(cursor)
                query = dict(params, pagination_key=cursor)
            except PortfolioError as exc:
                if str(exc) == "market_response_too_large":
                    raise
                raise PortfolioError("market_response_invalid") from None
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
                raise PortfolioError("market_response_invalid") from None
        raise PortfolioError("market_pagination_limit")

    def _calendar(self):
        if self.calendar is None:
            _, bundle = self._fetch("/markets/calendar",
                                    {"from": self.start.isoformat(), "to": self.end.isoformat()})
            try:
                calendar = calendar_from_raw(bundle)
                if set(calendar.days) != {(self.start + timedelta(days=i)).isoformat()
                                         for i in range((self.end-self.start).days+1)}:
                    raise ValueError()
                self.sessions = calendar.sessions(self.start.isoformat(), self.end.isoformat())
                if not self.sessions:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, AttributeError):
                raise PortfolioError("market_calendar_invalid") from None
            self.calendar, self.calendar_bundle = calendar, bundle

    def snapshot(self, symbol, currency):
        code = code_for(symbol, currency)
        key = (symbol, currency)
        if key in self.cache:
            return self.cache[key]
        self._calendar()
        master_date = self.sessions[-1]
        masters, master_bundle = self._fetch("/equities/master", {"code": code, "date": master_date})
        try:
            # 既存のmaster日付・code・原本hash検証をそのまま利用する。
            empty = dict(provider="jquants_v2", schema_version=1, receipts=[])
            report = coverage(empty, [master_date], [code], [master_bundle])
            if any(row.get("Code") != code for row in masters) or len(masters) > 1:
                raise ValueError()
            listed = report["symbols"][code]["listing_snapshot_coverage"]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PortfolioError("market_master_invalid") from None
        if not listed:
            raise PortfolioError("market_symbol_not_found")
        if masters[0].get("ProdCat") != "011":
            raise PortfolioError("market_instrument_unsupported")
        rows, daily_bundle = self._fetch("/equities/bars/daily",
            {"code": code, "from": self.start.isoformat(), "to": self.end.isoformat()})
        by_date = {}
        try:
            # 既存numeric schema検証は標準JSON型で行い、計算には元のDecimalを用いる。
            for record in daily_bundle["receipts"]:
                for row in json.loads(record["body"])["data"]:
                    validate_daily_row(row)
            if rows:
                pages = [dict(receipt=record["receipt"], rows=json.loads(record["body"])["data"])
                         for record in daily_bundle["receipts"]]
                if any(issue.severity == "error" for issue in inspect_daily(normalize_daily(pages))):
                    raise ValueError()
            for row in rows:
                day = row["Date"]
                if row["Code"] != code or day not in self.sessions or day in by_date:
                    raise ValueError()
                for field in ("C", "AdjFactor"):
                    if row.get(field) is not None:
                        decimal(row[field])
                by_date[day] = row
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PortfolioError("market_daily_invalid") from None
        if not by_date:
            self.cache[key] = None
            return None
        # 最新行の欠測を以前の終値で埋めず、missingとして返す。
        latest = by_date[max(by_date)]
        if latest.get("C") is None:
            self.cache[key] = None
            return None
        # 原価・数量への企業行動反映はMVP範囲外。調整済価格でごまかさない。
        if any(row.get("AdjFactor") not in (None, Decimal(1)) for row in rows):
            raise PortfolioError("market_corporate_action_requires_review")
        if any(row.get("AdjFactor") is None for row in rows):
            raise PortfolioError("market_adjustment_unknown")
        index = self.sessions.index(latest["Date"])
        previous = by_date.get(self.sessions[index-1]) if index else None
        previous_close = previous.get("C") if previous else None
        bundles = [self.calendar_bundle, master_bundle, daily_bundle]
        ingested = max(instant(record["receipt"]["ingested_at"])
                       for bundle in bundles for record in bundle["receipts"])
        source = ("fixture_jquants_v2" if self.fixture else "jquants_v2") + ":daily_close:"
        evidence = digest(research_json(bundles))
        result = MarketSnapshot(symbol=symbol, currency=currency, price=latest["C"],
            previous_close=previous_close, as_of=latest["Date"]+"T00:00:00+09:00",
            source=source+evidence, market="TSE", data_date=latest["Date"], ingested_at=ingested)
        self.cache[key] = result
        return result


def prepare_provider(positions, *, at, lookback_days=90, fixture_path=None, prefetch=True):
    """全銘柄の通貨/codeを通信前に検証し、明示したlive/fixtureだけを選ぶ。"""
    held = [p for p in positions if p.quantity]
    if len(held) > 100:
        raise PortfolioError("market_position_limit")
    for position in held:
        code_for(position.symbol, position.currency)
    if fixture_path is None:
        if not os.environ.get("JQUANTS_API_KEY", "").strip():
            raise PortfolioError("market_auth_missing")
        try:
            transport = JQuantsTransport()
        except Exception:
            raise PortfolioError("market_auth_invalid") from None
    else:
        try:
            transport = FixtureTransport(fixture_path)
        except Exception:
            raise PortfolioError("market_fixture_invalid") from None
    provider = JQuantsSnapshotProvider(transport, end_date=instant(at).astimezone(JST).date().isoformat(),
                                       lookback_days=lookback_days, fixture=fixture_path is not None)
    # assessは取得境界で停止理由を収集する。既存呼出しは従来どおり先に取得する。
    if prefetch:
        for position in held:
            provider.snapshot(position.symbol, position.currency)
    return provider
