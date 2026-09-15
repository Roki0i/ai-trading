"""J-Quants v2 read-only transport and offline, paginated fixtures."""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Observation, timestamp, utc
from .storage import canonical, digest, put


@dataclass(frozen=True)
class Response:
    body: bytes
    ingested_at: datetime


class Transport(Protocol):
    def get(self, path: str, params: dict) -> Response: ...


class FixtureTransport:
    def __init__(self, path: Path):
        self.pages = json.loads(path.read_text(encoding="utf-8"))["pages"]
        self.calls = []

    def get(self, path: str, params: dict) -> Response:
        self.calls.append((path, dict(params)))
        for page in self.pages:
            if page["path"] == path and page["params"] == params:
                return Response(canonical(page["response"]), timestamp(page["ingested_at"]))
        raise ValueError("fixture has no matching request")


class JQuantsTransport:
    BASE = "https://api.jquants.com/v2"

    def __init__(self):
        self._api_key = os.environ.get("JQUANTS_API_KEY", "")
        if not self._api_key:
            raise ValueError("JQUANTS_API_KEY is required for live mode")

    def get(self, path: str, params: dict) -> Response:
        if path not in ("/equities/bars/daily", "/markets/calendar", "/equities/master"):
            raise ValueError("endpoint not enabled for research")
        request = Request(self.BASE + path + "?" + urlencode(params),
                          headers={"x-api-key": self._api_key}, method="GET")
        try:
            with urlopen(request, timeout=30) as result:
                body = result.read()
        except HTTPError as exc:
            # Do not echo headers, response bodies or credentials into logs.
            raise RuntimeError("J-Quants HTTP status " + str(exc.code)) from None
        except URLError:
            raise RuntimeError("J-Quants network request failed") from None
        return Response(body, datetime.now(timezone.utc))


def acquire_daily(transport: Transport, date: str, raw_root: Path) -> tuple[list[dict], list[str]]:
    path = "/equities/bars/daily"
    params = {"date": date}
    pages, receipt_paths, seen = [], [], set()
    while True:
        response = transport.get(path, params)
        ingested = utc(response.ingested_at)
        body_path = put(raw_root / "jquants_v2" / "daily_bars" / "bodies", response.body)
        receipt = {"schema_version": 1, "source": "jquants_v2", "endpoint": path,
                   "params": params, "ingested_at": ingested.isoformat(),
                   "body_sha256": body_path.stem}
        receipt_path = put(raw_root / "jquants_v2" / "daily_bars" / "receipts", canonical(receipt))
        receipt_paths.append(str(receipt_path))
        payload = json.loads(response.body)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("invalid J-Quants response: data array required")
        for item in payload["data"]:
            if not isinstance(item, dict) or item.get("Date") != date:
                raise ValueError("unexpected row or date in daily response")
        pages.append({"receipt": receipt, "rows": payload["data"]})
        cursor = payload.get("pagination_key")
        if cursor is None or cursor == "":
            break
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError("invalid or repeated pagination cursor")
        seen.add(cursor)
        if len(pages) >= 1000:
            raise ValueError("pagination limit exceeded")
        params = {"date": date, "pagination_key": cursor}
    return pages, receipt_paths


def normalize_daily(pages: list[dict], source: str = "jquants_v2") -> list[Observation]:
    rows = []
    for page in pages:
        receipt = page["receipt"]
        ingested = timestamp(receipt["ingested_at"])
        for item in page["rows"]:
            # No undocumented market-close or publication timestamps are fabricated.
            # event_at represents the session DATE at JST midnight, not availability.
            event = timestamp(item["Date"] + "T00:00:00+09:00")
            payload = {"session_date": item["Date"], "code": item["Code"],
                       "open": item.get("O"), "high": item.get("H"),
                       "low": item.get("L"), "close": item.get("C"),
                       "volume": item.get("Vo"),
                       "adjustment_factor": item.get("AdjFactor")}
            rows.append(Observation(
                dataset="daily_bars", entity_id=source + ":" + item["Code"],
                event_at=event, published_at=None, available_at=ingested,
                ingested_at=ingested, revision_id=digest(canonical(item)),
                source=source, payload_json=canonical(payload).decode("utf-8"),
                availability_basis="observed",
                availability_evidence="raw-body-sha256:" + receipt["body_sha256"],
            ))
    return rows


class MarketDataProvider:
    """Same read-only daily API for fixture and live; portable immutable raw bundle."""
    def __init__(self, transport, raw_root):
        self.transport, self.raw_root = transport, Path(raw_root)

    def acquire(self, sessions):
        from .storage import read_verified
        receipts = []
        for session in sessions:
            _, paths = acquire_daily(self.transport, session, self.raw_root)
            for path in paths:
                path = Path(path)
                receipt = json.loads(read_verified(path))
                body = read_verified(path.parent.parent / 'bodies' / (receipt['body_sha256']+'.json'))
                receipts.append(dict(receipt=receipt, body=body.decode('utf-8')))
        bundle = dict(provider='jquants_v2', schema_version=1, receipts=receipts)
        return put(self.raw_root / 'bundles', canonical(bundle))

    @staticmethod
    def regenerate(bundle, source='jquants_v2'):
        pages = []
        if bundle['provider'] != 'jquants_v2' or bundle['schema_version'] != 1:
            raise ValueError('unsupported raw bundle')
        for record in bundle['receipts']:
            receipt, body = record['receipt'], record['body'].encode('utf-8')
            if digest(body) != receipt['body_sha256']:
                raise ValueError('raw body hash mismatch')
            if receipt['endpoint'] != '/equities/bars/daily':
                raise ValueError('unsupported raw endpoint')
            payload = json.loads(body)
            if any(r['Date'] != receipt['params']['date'] for r in payload['data']):
                raise ValueError('raw date mismatch')
            pages.append(dict(receipt=receipt, rows=payload['data']))
        return normalize_daily(pages, source=source)


def acquire_reference(transport, path, params, raw_root):
    """Archive versioned reference responses; never infer listing/delisting dates.

    Calendar requires a bounded range; master requires an explicit snapshot date.
    Current master data must not become historical membership automatically.
    """
    if path not in ('/markets/calendar', '/equities/master'):
        raise ValueError('unsupported reference endpoint')
    if path == '/equities/master' and not params.get('date'):
        raise ValueError('explicit master snapshot date required')
    if path == '/markets/calendar' and not all(params.get(k) for k in ('from','to')):
        raise ValueError('bounded calendar range required')
    records, seen = [], set()
    query = dict(params)
    for _ in range(1000):
        response = transport.get(path, query)
        root = Path(raw_root)/'jquants_v2'/path.rsplit('/',1)[-1]
        body = put(root/'bodies', response.body)
        receipt = dict(schema_version=1, source='jquants_v2', endpoint=path,params=dict(query),
                       ingested_at=utc(response.ingested_at).isoformat(),body_sha256=body.stem)
        put(root/'receipts',canonical(receipt))
        records.append(dict(receipt=receipt,body=response.body.decode('utf-8')))
        payload = json.loads(response.body)
        if not isinstance(payload.get('data'),list):
            raise ValueError('reference data array required')
        cursor=payload.get('pagination_key')
        if not cursor:
            return put(root/'bundles',canonical(dict(provider='jquants_v2',schema_version=1,receipts=records)))
        if not isinstance(cursor,str) or cursor in seen:
            raise ValueError('invalid/repeated reference cursor')
        seen.add(cursor); query=dict(params,pagination_key=cursor)
    raise ValueError('reference pagination limit exceeded')


def calendar_from_raw(bundle):
    """Cash equities: OSE holiday trading is NOT a TSE trading session."""
    from .market import TradingCalendar
    days, ingestions = {}, []
    for record in bundle['receipts']:
        receipt,body=record['receipt'],record['body'].encode('utf-8')
        if receipt['endpoint'] != '/markets/calendar' or digest(body) != receipt['body_sha256']:
            raise ValueError('invalid calendar raw lineage')
        ingestions.append(receipt['ingested_at'])
        for row in json.loads(body)['data']:
            if row['Date'] in days or str(row['HolDiv']) not in ('0','1','2','3'):
                raise ValueError('duplicate/unknown calendar classification')
            days[row['Date']] = 'open' if str(row['HolDiv']) in ('1','2') else 'exchange_closed_'+str(row['HolDiv'])
    if not days or any(r['receipt']['params']['from'] not in days or r['receipt']['params']['to'] not in days
                       for r in bundle['receipts']):
        raise ValueError('calendar response coverage incomplete')
    return TradingCalendar(version=digest(canonical(bundle)),source='jquants_v2:markets/calendar',
                           days=days,available_at=max(ingestions,key=timestamp))
