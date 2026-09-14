"""Deterministic synthetic integration exercise; never a performance benchmark."""
import json
import math
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from .backtest import BacktestConfig
from .experiment import ExperimentStore, initialize
from .models import Observation, timestamp
from .storage import canonical


def fixture(count=180):
    sessions=[]
    d=date(2024,1,2)
    while len(sessions)<count:
        if d.weekday()<5:
            sessions.append(d.isoformat())
        d+=timedelta(days=1)
    rows=[]
    for i,session in enumerate(sessions):
        for j,code in enumerate(('A','B')):
            price=round(100+0.06*i+9*math.sin(i/9+j*2)+3*math.cos(i/3+j),6)
            payload=dict(session_date=session,code=code,open=price,high=price,low=price,
                         close=price,volume=100000+1000*((i+j)%17),adjustment_factor=1)
            at=timestamp(session+'T17:00:00+09:00')
            rows.append(Observation('daily_bars','fixture:'+code,timestamp(session+'T00:00:00+09:00'),
                                    None,at,at,'original','fixture',canonical(payload).decode()))
    study=dict(periods=dict(development=dict(start=sessions[0],end=sessions[-1]),
                            validation=dict(start='2025-01-01',end='2025-06-30'),
                            holdout=dict(start='2025-07-01',end='2025-12-31')),
               universe_definition=dict(type='static_point_in_time',symbols=['fixture:A','fixture:B'],
                                        known_at='2024-01-01T00:00:00Z',evidence='predeclared synthetic universe'))
    config=BacktestConfig(sessions=sessions,universe=study['universe_definition']['symbols'],
                          strategy='ml',lot_size=1,commission_bps='10',slippage_bps='5',
                          ml=dict(volume=True,seed=7))
    return rows,config,study


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',type=Path,required=True)
    args=parser.parse_args()
    rows,config,study=fixture()
    initialize(args.store,study)
    store=ExperimentStore(args.store)
    models=[store.run(rows,dict(asdict(config),strategy=s),random_seed=7)
            for s in ('buy_and_hold','equal_weight','momentum','ml')]
    for model in models:
        if model.status != 'completed':
            raise RuntimeError(model.failure_reason)
        store.verify(model.experiment_id)
    report=dict(evidence='FIXTURE ONLY: not evidence of AI performance',
                comparison=store.compare([m.experiment_id for m in models]),
                prediction_metrics=store.read(models[-1].experiment_id)['outcome']['result']['ml_research']['prediction_metrics'])
    (args.store/'fixture-report.json').write_bytes(canonical(report))
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
