"""Read-only J-Quants daily collection with immutable local evidence."""

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import BacktestConfig, decimal
from .portfolio.jquants import code_for
from .providers import (JQuantsError, JQuantsTransport, MarketDataProvider,
                        acquire_reference, calendar_from_raw)
from .models import timestamp
from .quality import inspect_daily
from .storage import canonical, digest, put, read_verified

CAPITALS = ("100000", "200000", "300000")
PLANS = ("unknown", "free", "light", "standard", "premium")


def _today_jst():
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()


def _check_access(session, plan):
    day = date.fromisoformat(session)
    if day.isoformat() != session or plan not in PLANS:
        raise ValueError("invalid session or declared plan")
    # The declared plan is user input, not proof of the account's entitlement.
    if plan == "free" and day > _today_jst() - timedelta(weeks=12):
        raise ValueError("Free plan cannot access the latest 12 weeks")


def _temporal_use(session, latest):
    cutoff = timestamp(session + "T18:00:00+09:00")
    if date.fromisoformat(session) != _today_jst():
        return "historical_exploratory"
    return ("same_day_observation_unverified" if latest < cutoff else
            "after_cutoff_exploratory")


class PacedTransport:
    """Conservative single-process pacing; never automatically retries a 429."""

    def __init__(self, transport, requests_per_minute=5, *, clock=time.monotonic,
                 sleep=time.sleep):
        if type(requests_per_minute) is not int or not 1 <= requests_per_minute <= 5:
            raise ValueError("requests_per_minute must be 1..5")
        self.transport, self.clock, self.sleep = transport, clock, sleep
        self.interval = 60 / requests_per_minute + 0.5
        self.last_request = None

    def get(self, path, params):
        if self.last_request is not None:
            wait = self.interval - (self.clock() - self.last_request)
            if wait > 0:
                self.sleep(wait)
        self.last_request = self.clock()
        return self.transport.get(path, params)


def risk_config(session, codes, *, max_position_weight="0.20",
                min_cash_reserve_weight="0.40", commission_bps="10",
                commission_fixed="0", slippage_bps="5"):
    if not codes:
        raise ValueError("at least one symbol required")
    return BacktestConfig(
        sessions=(session,), universe=tuple("jquants_v2:" + code for code in codes),
        initial_cash="200000", lot_size=100,
        max_position_weight=max_position_weight,
        min_cash_reserve_weight=min_cash_reserve_weight,
        commission_bps=commission_bps, commission_fixed=commission_fixed,
        slippage_bps=slippage_bps)


def purchaseable_lots(quotes, config):
    """Maximum count of distinct one-lot positions from an empty cash account.

    With a common lot and fee schedule, ascending prices minimize both cash
    spent and the largest position value for each candidate count.
    """
    rate = decimal(config.commission_bps) / 10000
    slip = decimal(config.slippage_bps) / 10000
    fixed = decimal(config.commission_fixed)
    limit = decimal(config.max_position_weight)
    reserve = decimal(config.min_cash_reserve_weight)
    candidates = sorted(((decimal(price), code) for code, price in quotes.items()))
    result = {}
    for capital_text in CAPITALS:
        capital = Decimal(capital_text)
        spent = loss = Decimal(0)
        chosen = []
        for close, code in candidates:
            fill_price = close * (1 + slip)
            notional = fill_price * config.lot_size
            fee = notional * rate + fixed
            next_spent = spent + notional + fee
            next_loss = loss + (fill_price - close) * config.lot_size + fee
            post_equity = capital - next_loss
            if (next_spent > capital * (1 - reserve) or
                    close * config.lot_size > limit * post_equity):
                break
            spent, loss = next_spent, next_loss
            chosen.append(code)
        result[capital_text] = dict(symbol_count=len(chosen), symbols=chosen,
                                    cash_after=str(capital - spent),
                                    equity_after_cost=str(capital - loss))
    return result


def _bundle(path):
    return json.loads(read_verified(path))


def _prior(root, identity):
    folder = root / "collections"
    matches = []
    for path in sorted(folder.glob("*.json")):
        record = _bundle(path)
        if record.get("identity") == identity and record.get("status") == "complete":
            matches.append((timestamp(record["latest_ingested_at"]), path.name, record, path))
    if not matches:
        return None, None
    _, _, record, path = max(matches)
    for relative in record["raw_paths"].values():
        read_verified(root / relative)
    return record, path


def collect_session(transport, raw_root, session, symbols, calendar_start,
                    calendar_end, config, *, refresh=False, provenance="live_api",
                    plan="unknown"):
    """Archive one session; incomplete input is recorded but never filled in."""
    root = Path(raw_root)
    if provenance not in ("live_api", "synthetic_test"):
        raise ValueError("unknown collection provenance")
    _check_access(session, plan)
    requested = tuple(sorted(set(code_for(s, "JPY") for s in symbols)))
    if not requested or config.universe != tuple("jquants_v2:" + s for s in requested):
        raise ValueError("risk config and requested symbols differ")
    day = date.fromisoformat(session)
    start, end = date.fromisoformat(calendar_start), date.fromisoformat(calendar_end)
    if (day.isoformat() != session or start.isoformat() != calendar_start or
            end.isoformat() != calendar_end or not start <= day <= end):
        raise ValueError("session outside calendar request")
    identity = dict(session=session, symbols=list(requested), provenance=provenance,
                    declared_plan=plan,
                    calendar=[calendar_start, calendar_end],
                    risk={k: getattr(config, k) for k in (
                        "lot_size", "max_position_weight", "min_cash_reserve_weight",
                        "commission_bps", "commission_fixed", "slippage_bps")})
    if not refresh:
        prior, path = _prior(root, identity)
        if prior is not None:
            return dict(prior, collection_path=str(path), reused=True)

    cal_path = acquire_reference(transport, "/markets/calendar",
                                 {"from": calendar_start, "to": calendar_end}, root)
    calendar = calendar_from_raw(_bundle(cal_path))
    if calendar.days.get(session) != "open":
        raise ValueError("requested session is not an open cash-equity day")
    master_path = acquire_reference(transport, "/equities/master", {"date": session}, root)
    master = _bundle(master_path)
    listed, seen_master = set(), set()
    for record in master["receipts"]:
        receipt = record["receipt"]
        if receipt["params"]["date"] != session:
            raise ValueError("master request date mismatch")
        if digest(record["body"].encode("utf-8")) != receipt["body_sha256"]:
            raise ValueError("master body hash mismatch")
        for row in json.loads(record["body"])["data"]:
            if row.get("Date") != session or not isinstance(row.get("Code"), str):
                raise ValueError("master date/code mismatch")
            if row["Code"] in requested:
                if row["Code"] in seen_master:
                    raise ValueError("duplicate requested master code")
                seen_master.add(row["Code"])
                if row.get("ProdCat") == "011":
                    listed.add(row["Code"])
    daily_path = MarketDataProvider(transport, root).acquire((session,))
    daily = _bundle(daily_path)
    observations = MarketDataProvider.regenerate(daily)
    selected = [r for r in observations if r.entity_id in config.universe]
    errors = [i for i in inspect_daily(selected) if i.severity == "error"] if selected else []
    if errors:
        raise ValueError("invalid requested daily data: " + errors[0].code)
    rows = {}
    for row in selected:
        code = row.entity_id.removeprefix("jquants_v2:")
        if code in rows:
            raise ValueError("ambiguous requested daily row")
        rows[code] = json.loads(row.payload_json)
    missing = sorted(set(requested) - listed | (set(requested) - set(rows)))
    unavailable = sorted(code for code, row in rows.items()
                         if row["close"] is None or row["volume"] is None or
                         row["volume"] <= 0 or row["adjustment_factor"] != 1)
    quotes = {code: row["close"] for code, row in rows.items()
              if code in listed and code not in unavailable}
    receipts = [r["receipt"] for bundle in (master, daily, _bundle(cal_path))
                for r in bundle["receipts"]]
    latest = max(timestamp(r["ingested_at"]) for r in receipts)
    paths = dict(calendar=str(cal_path.relative_to(root)),
                 master=str(master_path.relative_to(root)),
                 daily=str(daily_path.relative_to(root)))
    record = dict(schema_version=1, provenance=provenance, identity=identity,
                  status="complete" if not missing and not unavailable else "incomplete",
                  request_count=len(receipts),
                  raw_paths=paths,
                  raw_sha256={key: path.stem for key, path in (
                      ("calendar", cal_path), ("master", master_path), ("daily", daily_path))},
                  latest_ingested_at=latest.isoformat(),
                  temporal_use=_temporal_use(session, latest),
                  decision_cutoff_relation=("before" if latest < timestamp(
                      session + "T18:00:00+09:00") else "at_or_after"),
                  listed_symbols=sorted(listed), missing_symbols=missing,
                  unavailable_symbols=unavailable,
                  one_lot_preview=purchaseable_lots(quotes, config),
                  research_complete=False, forward_ready=False)
    path = put(root / "collections", canonical(record))
    return dict(record, collection_path=str(path), reused=False)


def probe_session(transport, raw_root, session, symbol, *, plan="unknown"):
    """Bounded three-endpoint GET check; archive real responses without a trade."""
    _check_access(session, plan)
    root = Path(raw_root)
    code = code_for(symbol, "JPY")
    cal_path = acquire_reference(transport, "/markets/calendar",
                                 {"from": session, "to": session}, root)
    calendar = calendar_from_raw(_bundle(cal_path))
    if calendar.days.get(session) != "open":
        raise ValueError("probe date is not an open cash-equity day")
    master_path = acquire_reference(transport, "/equities/master",
                                    {"date": session, "code": code}, root)
    master = _bundle(master_path)
    master_rows = []
    for page in master["receipts"]:
        if page["receipt"]["params"].get("code") != code:
            raise ValueError("master probe code mismatch")
        for row in json.loads(page["body"])["data"]:
            if row.get("Date") != session or row.get("Code") != code:
                raise ValueError("master probe row mismatch")
            master_rows.append(row)
    daily_path = MarketDataProvider(transport, root).acquire((session,), code=code)
    daily = _bundle(daily_path)
    observations = MarketDataProvider.regenerate(daily)
    if len(observations) > 1 or any(r.entity_id != "jquants_v2:" + code
                                    for r in observations):
        raise ValueError("ambiguous daily probe row")
    if observations:
        errors = [i for i in inspect_daily(observations) if i.severity == "error"]
        if errors:
            raise ValueError("invalid daily probe data: " + errors[0].code)
    receipts = [page["receipt"] for bundle in (master, daily, _bundle(cal_path))
                for page in bundle["receipts"]]
    latest = max(timestamp(r["ingested_at"]) for r in receipts)
    row = json.loads(observations[0].payload_json) if observations else None
    unavailable = (row is None or row["close"] is None or row["volume"] is None or
                   row["volume"] <= 0)
    paths = dict(calendar=str(cal_path.relative_to(root)),
                 master=str(master_path.relative_to(root)),
                 daily=str(daily_path.relative_to(root)))
    report = dict(schema_version=1, purpose="connectivity_check",
                  declared_plan=plan, contract_verified=False, session=session,
                  symbol=code, status=("complete" if len(master_rows) == 1 and
                                      not unavailable else "incomplete"),
                  request_count=len(receipts), raw_paths=paths,
                  raw_sha256={key: path.stem for key, path in (
                      ("calendar", cal_path), ("master", master_path),
                      ("daily", daily_path))},
                  master_rows=len(master_rows), daily_rows=len(observations),
                  daily_unavailable=unavailable,
                  daily_fields=next((sorted(item) for page in daily["receipts"]
                                     for item in json.loads(page["body"])["data"]), []),
                  latest_ingested_at=latest.isoformat(),
                  temporal_use=_temporal_use(session, latest),
                  forward_ready=False)
    path = put(root / "probes", canonical(report))
    return dict(report, probe_path=str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--calendar-from", required=True)
    parser.add_argument("--calendar-to", required=True)
    parser.add_argument("--raw-root", required=True, type=Path)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--probe", action="store_true",
                        help="one symbol and one day; three read-only endpoints")
    parser.add_argument("--plan", choices=PLANS, default="unknown",
                        help="declared plan only; does not verify the contract")
    parser.add_argument("--requests-per-minute", type=int, default=5)
    parser.add_argument("--max-position-weight", default="0.20")
    parser.add_argument("--min-cash-reserve-weight", default="0.40")
    parser.add_argument("--commission-bps", default="10")
    parser.add_argument("--commission-fixed", default="0")
    parser.add_argument("--slippage-bps", default="5")
    args = parser.parse_args()
    try:
        if not os.environ.get("JQUANTS_API_KEY", "").strip():
            raise ValueError("auth_missing")
        codes = tuple(sorted(set(code_for(s, "JPY") for s in args.symbols)))
        transport = PacedTransport(JQuantsTransport(), args.requests_per_minute)
        if args.probe:
            if (len(codes) != 1 or args.calendar_from != args.date or
                    args.calendar_to != args.date or args.refresh):
                raise ValueError("probe requires one symbol and one calendar day")
            report = probe_session(transport, args.raw_root, args.date,
                                   args.symbols[0], plan=args.plan)
        else:
            config = risk_config(args.date, codes,
                                 max_position_weight=args.max_position_weight,
                                 min_cash_reserve_weight=args.min_cash_reserve_weight,
                                 commission_bps=args.commission_bps,
                                 commission_fixed=args.commission_fixed,
                                 slippage_bps=args.slippage_bps)
            report = collect_session(transport, args.raw_root, args.date, args.symbols,
                                     args.calendar_from, args.calendar_to, config,
                                     refresh=args.refresh, plan=args.plan)
        print(json.dumps(dict(ok=True, **report), ensure_ascii=False))
    except JQuantsError as exc:
        print(json.dumps(dict(ok=False, error=("http_" + str(exc.http_status) if exc.kind == "http"
                                               else exc.kind))))
        raise SystemExit(2) from None
    except Exception as exc:
        error = "auth_missing" if str(exc) == "auth_missing" else type(exc).__name__
        print(json.dumps(dict(ok=False, error=error)))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
