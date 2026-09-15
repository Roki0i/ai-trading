"""Phase 5 synthetic, explicit-calendar integration; never opens holdout."""
import json
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from .models import Observation, timestamp
from .storage import canonical, digest, encode_observation
from .market import TradingCalendar

POLICY = dict(version='strict_v1', held_missing_price='stop', missing_volume='skip',
              api_gap='record_skip', insufficient_history='skip', max_halt_sessions=5)


def reference(symbol, dataset, event, payload, known='2023-12-01'):
    at = timestamp(known+'T17:00:00+09:00')
    return encode_observation(Observation(dataset,symbol,timestamp(event+'T00:00:00+09:00'),
                              at,at,at,'original','fixture',canonical(payload).decode()))


def context(sessions, symbols):
    # Explicit fixture holiday list; not a calendar extrapolation service.
    holidays = {'2024-01-08','2024-02-12','2024-02-23','2024-03-20','2024-04-29',
                '2024-05-03','2024-05-06','2024-07-15','2024-08-12','2024-09-16',
                '2024-09-23','2024-10-14','2024-11-04','2025-01-13'}
    d = date.fromisoformat(sessions[0]); end = date.fromisoformat(sessions[-1]); days = {}
    while d <= end:
        days[str(d)] = ('weekend' if d.weekday() >= 5 else 'new_year_closure' if (d.month,d.day) in
                        ((1,1),(1,2),(1,3),(12,31)) else 'holiday' if str(d) in holidays else 'open')
        d += timedelta(days=1)
    return dict(data_provider='synthetic_jquants_v2', raw_data_hash=digest(canonical([])), raw_data=[],
                calendar=dict(version='JP-cash-fixture-2024-2025-v1',source='explicit synthetic fixture',days=days,available_at='2023-12-01T00:00:00Z'),
                historical_universe=[reference(s,'historical_universe','2023-01-04',
                    dict(listing_date='2023-01-04', delisting_date=None)) for s in symbols],
                corporate_actions=[], missing_data_policy=dict(POLICY), cost_scenario='base',
                statistical_evaluation=dict(block_length=5,replicates=200,confidence=.95,seed=7))


def fixture():
    from .ml_fixture import fixture as old_fixture
    rows, cfg, study = old_fixture(210)
    m = context(('2024-01-04','2024-11-29'), cfg.universe)
    calendar = TradingCalendar(**m['calendar'])
    sessions = calendar.sessions('2024-01-04','2024-11-29')[:210]
    mapping = dict(zip(cfg.sessions, sessions))
    converted = []
    for r in rows:
        p = json.loads(r.payload_json); d = mapping[p['session_date']]; p['session_date'] = d
        at = timestamp(d+'T17:00:00+09:00')
        converted.append(replace(r,event_at=timestamp(d+'T00:00:00+09:00'),available_at=at,ingested_at=at,
                                 payload_json=canonical(p).decode()))
    m['raw_data'] = [encode_observation(r) for r in converted]
    m['raw_data_hash'] = digest(canonical(m['raw_data']))
    cfg = replace(cfg, sessions=sessions,market=m)
    study['periods']['development'] = dict(start=sessions[0],end=sessions[-1])
    study['universe_definition']['type'] = 'historical_point_in_time'
    return converted,cfg,study


def main():
    import argparse
    from .experiment import initialize, ExperimentStore
    from .sensitivity import compare_costs
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',type=Path,required=True)
    args=parser.parse_args()
    rows,cfg,study=fixture(); initialize(args.store,study)
    store=ExperimentStore(args.store)
    report = {s:compare_costs(store,rows,replace(cfg,strategy=s),seed=7)
              for s in ('equal_weight','momentum','ml')}
    (args.store/'cost-report.json').write_bytes(canonical(report))
    print(json.dumps(report,indent=2))

if __name__ == '__main__':
    main()
