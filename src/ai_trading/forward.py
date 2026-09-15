"""Frozen observed-PIT paper runs. No broker, holdout reader or backdating API.

SQLite transactions make decisions atomic across restart. Content-addressed input
artifacts and a hash chain detect mutation; this is not externally notarized WORM.
Replay only verifies the paper engine; recorded decisions are never replaced.
"""
import json
import sqlite3
import statistics
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .backtest import BacktestConfig, run_backtest
from .experiment import decode_rows, environment, validate_study
from .market import raw_observations
from .models import timestamp
from .storage import canonical, digest, encode_observation, put, read_verified


def reject_holdout(rows, study):
    h = study['periods']['holdout']
    for r in rows:
        d = json.loads(r.payload_json).get('session_date', r.event_at.date().isoformat())
        if h['start'] <= d <= h['end']:
            raise PermissionError('holdout remains sealed')


def freeze(root, config, study, *, seed, mode='paper', selection=None):
    """Create a new series; the manifest contains executable settings, not labels."""
    from .ml import FEATURES
    validate_study(study)
    config = BacktestConfig(**asdict(config))
    if mode not in ('paper','fixture') or config.market is None or config.volume_participation is None:
        raise ValueError('market context and liquidity constraint required')
    if config.pit_mode != 'observed' or type(seed) is not int:
        raise ValueError('observed PIT and integer seed required')
    if config.ml and (config.ml['seed'] != seed or len(config.ml['penalties']) != 1):
        raise ValueError('freeze one selected ML penalty and matching seed')
    if config.market['statistical_evaluation'].get('seed',7) != seed:
        raise ValueError('statistical seed must match freeze seed')
    h = study['periods']['holdout']
    if any(h['start'] <= d <= h['end'] for d in config.sessions):
        raise PermissionError('holdout remains sealed')
    raw = raw_observations(config.market)
    reject_holdout(raw,study)
    now = datetime.now(timezone.utc)
    first = 0
    if config.ml:
        from .ml import MLConfig
        first = MLConfig(**config.ml).first_prediction
        if first >= len(config.sessions):
            raise ValueError('insufficient forward warmup')
    if mode == 'paper':
        if timestamp(config.sessions[first]+'T18:00:00+09:00') <= now:
            raise ValueError('paper series must be frozen before its first session')
        if config.market['data_provider'] != 'jquants_v2':
            raise ValueError('paper mode requires real provider provenance')
        if not selection or selection.get('split') != 'development' or selection.get('selected') != config.strategy or {c['strategy'] for c in selection.get('candidates',[])} != {'buy_and_hold','equal_weight','momentum','ml'}:
            raise ValueError('development comparison of all four candidates required')
        if any(max(r.available_at,r.ingested_at) > now for r in raw):
            raise ValueError('future input at freeze')
    env = environment()
    manifest = dict(schema_version=1, kind='frozen_forward', mode=mode, created_at=now.isoformat(),
        config=asdict(config), study=study, universe_policy='observed_lifecycle_half_open_v1',
        feature_definitions=dict(FEATURES, **({'volume_change_5':'volume[t]/volume[t-5]-1'} if config.ml and config.ml['volume'] else {})) if config.strategy == 'ml' else
            dict(history='raw contiguous closes',lookback=config.lookback,top_n=config.top_n),
        model_type=config.strategy, hyperparameters=config.ml if config.strategy == 'ml' else dict(lookback=config.lookback,top_n=config.top_n),
        retraining_cadence=config.ml['step'] if config.strategy == 'ml' else 'none',
        decision_time='18:00:00+09:00', execution_rule='next_session_close_partial_cancel_v1',
        fee=dict(bps=config.commission_bps,fixed=config.commission_fixed), slippage=config.slippage_bps,
        liquidity_constraints=dict(volume_participation=config.volume_participation),
        missing_data_policy=config.market['missing_data_policy'], seed=seed,
        statistical_evaluation=config.market['statistical_evaluation'], selection=selection,
        environment=env)
    root = Path(root)
    root.mkdir(parents=True,exist_ok=True)
    manifest_path = put(root/'artifacts',canonical(manifest))
    with (root/'freeze.json').open('xb') as f:
        f.write(canonical(dict(sha256=manifest_path.stem)))
    Ledger(root)  # initialize transactional append-only storage
    return manifest_path.stem


class Ledger:
    def __init__(self, root):
        self.root = Path(root)
        self.freeze_hash = json.loads((self.root/'freeze.json').read_bytes())['sha256']
        self.manifest = self.artifact(self.freeze_hash)
        self.db_path = self.root/'ledger.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS series (hash TEXT PRIMARY KEY)')
            hashes = db.execute('SELECT hash FROM series').fetchall()
            if not hashes:
                db.execute('INSERT INTO series VALUES (?)',(self.freeze_hash,))
            elif hashes != [(self.freeze_hash,)]:
                raise ValueError('series freeze changed')
            db.execute("CREATE TRIGGER IF NOT EXISTS series_no_update BEFORE UPDATE ON series BEGIN SELECT RAISE(ABORT, 'frozen'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS series_no_delete BEFORE DELETE ON series BEGIN SELECT RAISE(ABORT, 'frozen'); END")
            db.execute('CREATE TABLE IF NOT EXISTS decisions (sequence INTEGER PRIMARY KEY, session TEXT UNIQUE NOT NULL, body BLOB NOT NULL, hash TEXT NOT NULL)')
            db.execute("CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON decisions BEGIN SELECT RAISE(ABORT, 'append only'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON decisions BEGIN SELECT RAISE(ABORT, 'append only'); END")

    def connect(self):
        db = sqlite3.connect(self.db_path,timeout=30)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def artifact(self, sha):
        if len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            raise ValueError('invalid artifact hash')
        return json.loads(read_verified(self.root/'artifacts'/(sha+'.json')))

    def save(self, value):
        return put(self.root/'artifacts',canonical(value)).stem

    def check_environment(self):
        expected = self.artifact(self.freeze_hash)['environment']
        current = environment()
        for key in ('code_hash','python','implementation','machine','platform'):
            if current.get(key) != expected[key]:
                raise ValueError('code changed or runtime mismatch: freeze a new series; '+key)

    def read(self):
        with self.connect() as db:
            records = db.execute('SELECT sequence,session,body,hash FROM decisions ORDER BY sequence').fetchall()
        result, previous = [], self.freeze_hash
        for i,(seq,session,body,sha) in enumerate(records):
            entry = json.loads(body)
            if seq != i or digest(body) != sha or entry['previous_hash'] != previous or entry['freeze_hash'] != self.freeze_hash or entry['session'] != session:
                raise ValueError('ledger chain mismatch')
            for name in ('raw_input_hash','feature_hash','model_hash','result_hash','reference_hash'):
                self.artifact(entry[name])
            previous = sha
            result.append(dict(entry,entry_hash=sha))
        return result

    def step(self, session, raw_snapshot, *, reference_updates=None, fixture_generated_at=None):
        manifest = self.artifact(self.freeze_hash)
        self.check_environment()
        if fixture_generated_at is not None and manifest['mode'] != 'fixture':
            raise ValueError('paper timestamp cannot be overridden')
        generated = timestamp(fixture_generated_at) if fixture_generated_at else datetime.now(timezone.utc)
        cutoff = timestamp(session+'T18:00:00+09:00')
        if manifest['mode'] == 'paper' and generated.date() != cutoff.date():
            raise ValueError('missed decision: historical paper backfill forbidden')
        if generated < cutoff:
            raise ValueError('decision session has not closed')
        old = self.read()
        base = BacktestConfig(**manifest['config'])
        offset = 0
        if base.ml:
            from .ml import MLConfig
            offset = MLConfig(**base.ml).first_prediction
        expected = base.sessions[offset+len(old):offset+len(old)+1]
        if not expected or session != expected[0]:
            raise ValueError('append must use the next frozen session')
        if old and generated <= timestamp(old[-1]['generated_at']):
            raise ValueError('generated_at must increase')
        previous_cutoff = timestamp(old[-1]['decision_at']) if old else None
        market = json.loads(canonical(base.market))
        if market['data_provider'] == 'jquants_v2':
            from .providers import merge_daily_bundles
            previous_raw = self.artifact(old[-1]['raw_input_hash'])['snapshot'] if old else market['raw_data']
            raw_snapshot = merge_daily_bundles(previous_raw,raw_snapshot)
        market['raw_data'] = raw_snapshot
        market['raw_data_hash'] = digest(canonical(raw_snapshot))
        rows = raw_observations(market)
        reject_holdout(rows,manifest['study'])
        if any(r.event_at >= cutoff or r.available_at >= cutoff or r.ingested_at >= cutoff for r in rows):
            raise ValueError('input unavailable at decision; archive revision for a later decision')
        old_rows = decode_rows(self.artifact(old[-1]['raw_input_hash'])['observations']) if old else raw_observations(base.market)
        old_encoded = {canonical(encode_observation(r)) for r in old_rows}
        encoded = {canonical(encode_observation(r)) for r in rows}
        if not old_encoded <= encoded:
            raise ValueError('raw history cannot be removed or replaced')
        if previous_cutoff and any(r.ingested_at < previous_cutoff and canonical(encode_observation(r)) not in old_encoded for r in rows):
            raise ValueError('new input backdates observation history')
        status_rows = list(base.execution_statuses)
        if old:
            refs = self.artifact(old[-1]['reference_hash'])
            market.update({k:v for k,v in refs.items() if k != 'execution_statuses'})
            status_rows = refs['execution_statuses']
        for dataset,new in (reference_updates or {}).items():
            if dataset == 'execution_statuses':
                for r in new:
                    if max(timestamp(r['available_at']),timestamp(r['ingested_at'])) >= cutoff or (previous_cutoff and timestamp(r['ingested_at']) < previous_cutoff):
                        raise ValueError('status update violates observation timing')
                status_rows.extend(new)
                continue
            if dataset not in ('historical_universe','corporate_actions'):
                raise ValueError('unsupported reference update')
            for r in decode_rows(new):
                if max(r.available_at,r.ingested_at) >= cutoff or (previous_cutoff and r.ingested_at < previous_cutoff):
                    raise ValueError('reference update violates observation timing')
            market[dataset].extend(new)
        cfg = replace(base,sessions=base.sessions[:base.sessions.index(session)+1],market=market,execution_statuses=tuple(status_rows))
        result = run_backtest(rows,cfg)
        # Replayed old states are checked, never used to replace the original ledger.
        for entry in old:
            snap = next(s for s in result['snapshots'] if s['session'] == entry['session'])
            if snap != entry['portfolio']:
                raise ValueError('past portfolio changed; reconstruction must use a separate research run')
            predictions = [p for p in result.get('ml_research',{}).get('predictions',[]) if p['session']==entry['session']] if base.strategy == 'ml' else []
            if predictions != entry['prediction']:
                raise ValueError('past prediction changed')
        research = result.get('ml_research',{})
        decision = next((d for d in result['decisions'] if d['session']==session),None)
        features = [f for f in research.get('features',[]) if f['session']==session] if base.strategy=='ml' else (decision or {})
        models = research.get('folds',[])
        model = models[-1]['model'] if models and base.strategy == 'ml' else dict(strategy=base.strategy,parameters=manifest['hyperparameters'])
        fills = [f for f in result['fills'] if f['session']==session]
        available_times = [max(r.available_at,r.ingested_at) for r in rows]
        for dataset in ('historical_universe','corporate_actions'):
            available_times.extend(max(r.available_at,r.ingested_at) for r in decode_rows(market[dataset])
                                   if max(r.available_at,r.ingested_at) < cutoff)
        available_times.append(timestamp(market['calendar']['available_at']))
        available_times.extend(max(timestamp(r['available_at']),timestamp(r['ingested_at'])) for r in status_rows
                               if max(timestamp(r['available_at']),timestamp(r['ingested_at'])) < cutoff)
        record = dict(freeze_hash=self.freeze_hash,previous_hash=old[-1]['entry_hash'] if old else self.freeze_hash,
            session=session,generated_at=generated.isoformat(),decision_at=cutoff.isoformat(),
            data_available_at=max(available_times).isoformat(),
            raw_input_hash=self.save(dict(provider=market['data_provider'],snapshot=raw_snapshot,
                                        observations=[encode_observation(r) for r in rows])),
            reference_hash=self.save(dict({k:market[k] for k in ('historical_universe','corporate_actions','calendar')},execution_statuses=status_rows)),
            feature_hash=self.save(features),model_hash=self.save(model),result_hash=self.save(result),
            prediction=[p for p in research.get('predictions',[]) if p['session']==session] if base.strategy == 'ml' else [],
            target_weights=decision['target_weights'] if decision else None,
            proposed_orders=[o for o in result['orders'] if o['decision_session']==session],
            orders=result['orders'],simulated_fills=fills,
            costs=sum(float(f['commission'])+float(f['slippage_cost']) for f in fills),
            portfolio=result['snapshots'][-1],resulting_positions=result['snapshots'][-1]['positions'])
        body = canonical(record)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0] != len(old):
                raise ValueError('concurrent append; retry from ledger')
            db.execute('INSERT INTO decisions VALUES (?,?,?,?)',(len(old),session,body,digest(body)))
        return dict(record,entry_hash=digest(body))

    def verify(self):
        """Regenerate raw and replay each committed prefix using its own snapshot."""
        self.check_environment()
        entries = self.read()
        for entry in entries:
            expected = self.artifact(entry['result_hash'])
            cfg = BacktestConfig(**expected['config'])
            raw = self.artifact(entry['raw_input_hash'])
            rows = raw_observations(cfg.market)
            if [encode_observation(r) for r in rows] != raw['observations'] or cfg.market['raw_data'] != raw['snapshot']:
                raise ValueError('raw regeneration mismatch')
            if canonical(run_backtest(rows,cfg)) != canonical(expected):
                raise ValueError('forward replay mismatch')
            if entry['portfolio'] != expected['snapshots'][-1]:
                raise ValueError('paper state mismatch')
        return dict(freeze_hash=self.freeze_hash,verified_entries=len(entries),
                    ledger_head=entries[-1]['entry_hash'] if entries else self.freeze_hash)

    def report(self):
        from .statistics import interval, returns, return_difference
        self.check_environment()
        entries = self.read()
        if not entries:
            return dict(status='not_started',statistical_conclusion='insufficient_forward_history')
        result = self.artifact(entries[-1]['result_hash'])
        cfg = BacktestConfig(**result['config'])
        rows = decode_rows(self.artifact(entries[-1]['raw_input_hash'])['observations'])
        benchmark = run_backtest(rows,replace(cfg,strategy='buy_and_hold'))
        manifest = self.artifact(self.freeze_hash)
        stats = manifest['statistical_evaluation']
        n = len(entries)
        return dict(mode=manifest['mode'],freeze_hash=self.freeze_hash,ledger_head=entries[-1]['entry_hash'],
            sessions=n,metrics=result['metrics'],
            benchmark_relative_return=result['metrics']['cumulative_return']-benchmark['metrics']['cumulative_return'],
            benchmark='buy_and_hold_same_universe_costs_liquidity_and_dates',cost=sum(e['costs'] for e in entries),
            mean_return_ci=interval([v for d,v in returns(result)],statistics.mean,stats),
            benchmark_relative_ci=return_difference(result,benchmark,stats),
            prediction_metrics=result['ml_research']['prediction_metrics'] if cfg.strategy=='ml' else {'status':'not_applicable_to_nonprobabilistic_strategy'},
            prediction_confidence_intervals=result.get('ml_research',{}).get('prediction_confidence_intervals') if cfg.strategy=='ml' else None,
            statistical_conclusion='insufficient_forward_history' if n < max(60,2*stats['block_length']) else 'descriptive_only; selection uncertainty not included')


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command',required=True)
    for name in ('freeze','step','report','verify'):
        s = sub.add_parser(name)
        s.add_argument('--root',type=Path,required=True)
        if name == 'freeze':
            s.add_argument('--config',type=Path,required=True)
            s.add_argument('--study',type=Path,required=True)
            s.add_argument('--seed',type=int,required=True)
            s.add_argument('--selection',type=Path,required=True)
        if name == 'step':
            s.add_argument('--session',required=True)
            s.add_argument('--raw-snapshot',type=Path,required=True)
            s.add_argument('--reference-updates',type=Path)
    a = p.parse_args()
    if a.command == 'freeze':
        result = freeze(a.root,BacktestConfig(**json.loads(a.config.read_bytes())),
                        json.loads(a.study.read_bytes()),seed=a.seed,selection=json.loads(read_verified(a.selection)))
    elif a.command == 'step':
        result = Ledger(a.root).step(a.session,json.loads(read_verified(a.raw_snapshot)),
                    reference_updates=json.loads(read_verified(a.reference_updates)) if a.reference_updates else None)
    elif a.command == 'verify':
        result = Ledger(a.root).verify()
    else:
        result = Ledger(a.root).report()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
