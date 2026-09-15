"""Acquisition completeness is distinct from observed-PIT research readiness."""
import json
from collections import Counter
from pathlib import Path

from .models import timestamp
from .providers import (FixtureTransport, JQuantsTransport, MarketDataProvider,
                        acquire_reference, validate_daily_row)
from .storage import canonical, digest, put, read_verified


def coverage(bundle, sessions, symbols, master_bundles=()):
    observations = MarketDataProvider.regenerate(bundle)  # verify immutable lineage and schema
    from .quality import inspect_daily
    issues = inspect_daily(observations)
    rows = [r for page in bundle['receipts'] for r in json.loads(page['body'])['data']]
    masters = {}
    for b in master_bundles:
        for page in b['receipts']:
            rec = page['receipt']
            if digest(page['body'].encode()) != rec['body_sha256'] or rec['endpoint'] != '/equities/master':
                raise ValueError('invalid master lineage')
            requested = rec['params']['date']
            for r in json.loads(page['body'])['data']:
                if r.get('Date') != requested or not isinstance(r.get('Code'), str):
                    raise ValueError('master snapshot date/code mismatch')
                masters.setdefault(requested, set()).add(r['Code'])
    report = {}
    for symbol in symbols:
        selected = [r for r in rows if r['Code'] == symbol and r['Date'] in sessions]
        by_date = {d: [r for r in selected if r['Date'] == d] for d in sessions}
        unavailable = Counter(k for r in selected for k in ('O','H','L','C','Vo','AdjFactor','AdjC') if r.get(k) is None)
        report[symbol] = dict(period=[sessions[0],sessions[-1]], expected_sessions=len(sessions),
            acquired_rows=len(selected), acquired=bool(selected),
            price_coverage=sum(any(all(r.get(k) is not None for k in ('O','H','L','C')) for r in rs) for rs in by_date.values()),
            volume_coverage=sum(any(r.get('Vo') is not None for r in rs) for rs in by_date.values()),
            missing_sessions=[d for d,rs in by_date.items() if not rs],
            duplicate_rows=sum(n-1 for n in Counter(canonical(r) for r in selected).values()),
            revisions=sum(max(0,len({canonical(r) for r in rs})-1) for rs in by_date.values()),
            listing_snapshot_coverage=sum(symbol in masters.get(d,set()) for d in sessions),
            listing_delisting_coverage='unknown: dated master does not establish lifecycle or publication history',
            corporate_action_coverage='unknown: AdjFactor is not an entitlement record',
            adjustment_events=[dict(date=r['Date'],raw_close=r.get('C'),adjusted_close=r.get('AdjC'),
                                    factor=r.get('AdjFactor'),status='requires_independent_action_evidence')
                               for r in selected if r.get('AdjFactor') not in (None,1)],
            unavailable_fields=dict(unavailable), published_timing='unavailable',
            quality_issues=[i.to_dict() for i in issues if i.entity_id == 'jquants_v2:'+symbol],
            available_timing='observed at ingestion; cannot backdate historical acquisition',
            research_complete=False)
    return dict(symbols=report, research_complete=False,
                reason='lifecycle, corporate-action completeness and historical observed timing require independent evidence')


def validate_split(raw_row, action, quantity):
    from decimal import Decimal
    validate_daily_row(raw_row)
    if not action.get('evidence') or action.get('kind') not in ('stock_split','reverse_split'):
        raise ValueError('independent split evidence required')
    ratio = Decimal(str(action['ratio']))
    if ratio <= 0 or raw_row['Date'] != action['effective_date'] or Decimal(str(raw_row['AdjFactor'])) * ratio != 1:
        raise ValueError('split factor/effective date mismatch')
    new_quantity = Decimal(quantity) * ratio
    if new_quantity != int(new_quantity):
        raise ValueError('fractional entitlement unsupported')
    return dict(raw_price=raw_row['C'], vendor_adjusted_price=raw_row.get('AdjC'),
                vendor_adjusted_price_used_for_execution=False, quantity_before=quantity,
                quantity_after=int(new_quantity), previous_mark_multiplier=str(1/ratio),
                evidence=action['evidence'])


def historical_checks(config, dates):
    from .market import membership
    return [dict(date=d, universe=list(membership(config,d,timestamp(d+'T18:00:00+09:00'))),
                 policy='observed lifecycle PIT; listing inclusive, delisting exclusive') for d in dates]


def smoke(raw_root, sessions, symbols, fixture_path=None):
    import os
    live = bool(os.environ.get('JQUANTS_API_KEY'))
    transport = JQuantsTransport() if live else FixtureTransport(fixture_path or Path('tests/fixtures/daily_pages.json'))
    bundle_path = MarketDataProvider(transport,raw_root).acquire(sessions)
    bundle = json.loads(read_verified(bundle_path))
    masters = []
    if live:
        for d in sessions:
            p = acquire_reference(transport,'/equities/master',dict(date=d),raw_root)
            masters.append(json.loads(read_verified(p)))
        acquire_reference(transport,'/markets/calendar',{'from':sessions[0],'to':sessions[-1]},raw_root)
    result = dict(mode='real_api' if live else 'fixture_fallback', raw_snapshot=bundle_path.stem,
                  coverage=coverage(bundle,sessions,symbols,masters),
                  live_api_verified=live, secrets_saved=False)
    return put(Path(raw_root)/'validation',canonical(result))


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--raw-root', type=Path, required=True)
    p.add_argument('--sessions', nargs='+', required=True)
    p.add_argument('--symbols', nargs='+', required=True)
    p.add_argument('--fixture', type=Path)
    a = p.parse_args()
    print(smoke(a.raw_root,a.sessions,a.symbols,a.fixture))


if __name__ == '__main__':
    main()
