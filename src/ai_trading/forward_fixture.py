"""Reproducible Phase 6 development selection and explicitly synthetic ledger."""
import argparse
import json
from dataclasses import replace
from pathlib import Path

from .experiment import ExperimentStore, initialize
from .forward import Ledger, freeze
from .market_fixture import fixture
from .storage import canonical, digest, encode_observation, put


def select_development(store, rows, config, seed):
    """Predeclared net-return ranking on identical development data; all four kept."""
    candidates = []
    for strategy in ('buy_and_hold','equal_weight','momentum','ml'):
        e = store.run(rows,replace(config,strategy=strategy),split='development',random_seed=seed)
        if e.status != 'completed':
            raise ValueError('development candidate failed: '+str(e.failure_reason))
        candidates.append(dict(strategy=strategy,experiment_id=e.experiment_id,metrics=e.metrics))
    store.compare([c['experiment_id'] for c in candidates])
    winner = min(candidates,key=lambda c:(-c['metrics']['cumulative_return'],c['strategy']))
    return dict(split='development',holdout_accessed=False,
                selection_rule='maximum net cumulative return; ties by strategy name',
                candidates=candidates,selected=winner['strategy'],
                limitation='development selection is optimistic; fixture ranking is not real-market evidence')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--store',type=Path,required=True)
    a = p.parse_args()
    rows,cfg,study = fixture()
    # A single predeclared penalty is compared on development, then frozen.
    cfg = replace(cfg,commission_bps='10',slippage_bps='5',volume_participation='0.01',
                  ml=dict(cfg.ml,penalties=[0.01],seed=7))
    initialize(a.store/'development',study)
    selection = select_development(ExperimentStore(a.store/'development'),rows,cfg,7)
    put(a.store/'selection',canonical(selection))
    # This exercises mechanics on synthetic development sessions only.
    # It must never be presented as an out-of-sample forward performance result.
    from .ml import MLConfig
    first = MLConfig(**cfg.ml).first_prediction
    cfg = replace(cfg,strategy=selection['selected'],sessions=cfg.sessions[:first+5])
    cfg.market['raw_data']=[]
    cfg.market['raw_data_hash']=digest(canonical([]))
    root = a.store/'fixture-forward'
    freeze(root,cfg,study,seed=7,mode='fixture',selection=selection)
    for session in cfg.sessions[first:]:
        raw = [encode_observation(r) for r in rows if json.loads(r.payload_json)['session_date']<=session]
        Ledger(root).step(session,raw,fixture_generated_at=session+'T18:01:00+09:00')
    report = Ledger(root).report()
    path = put(a.store/'reports',canonical(report))
    print(json.dumps(dict(selection=selection,report=str(path),freeze_hash=Ledger(root).freeze_hash),indent=2))


if __name__ == '__main__':
    main()
