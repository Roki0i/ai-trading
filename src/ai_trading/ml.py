"""Offline fixed-feature logistic baseline. Labels never enter feature dictionaries."""
import math
import json
import random
import statistics as st
from dataclasses import asdict, dataclass

from .models import as_of, timestamp
from .storage import canonical, digest, encode_observation

FEATURES = dict(return_5='close[t]/close[t-5]-1', return_20='close[t]/close[t-20]-1',
                return_60='close[t]/close[t-60]-1', volatility_20='sample std of 20 daily returns',
                ma_ratio_20='close[t]/mean(close[t-19:t+1])-1')


@dataclass(frozen=True)
class MLConfig:
    horizon: int = 5
    train_sessions: int = 40
    validation_sessions: int = 20
    step: int = 20
    epochs: int = 120
    learning_rate: float = 0.1
    penalties: tuple = (0.01, 0.1)
    volume: bool = False
    seed: int = 0

    def __post_init__(self):
        for k in ('horizon', 'train_sessions', 'validation_sessions', 'step', 'epochs'):
            if type(getattr(self, k)) is not int or getattr(self, k) < 1:
                raise ValueError('positive integer required: ' + k)
        if type(self.seed) is not int or type(self.volume) is not bool:
            raise ValueError('invalid seed/volume')
        if not math.isfinite(self.learning_rate) or self.learning_rate <= 0:
            raise ValueError('invalid learning rate')
        object.__setattr__(self, 'penalties', tuple(self.penalties))
        if not self.penalties or any(not math.isfinite(p) or p < 0 for p in self.penalties):
            raise ValueError('invalid penalties')

    @property
    def first_prediction(self):
        return 60 + self.train_sessions + self.validation_sessions + 2 * self.horizon


def features(rows, config, volume=False):
    from .backtest import _bars
    output = []
    names = dict(FEATURES)
    if volume:
        names['volume_change_5'] = 'volume[t]/volume[t-5]-1'
    for i, session in enumerate(config.sessions):
        if i < 60:
            continue
        cutoff = timestamp(session + 'T18:00:00+09:00')
        bars = _bars(rows, cutoff, config)
        selected = as_of(rows, cutoff)
        sources = {(r.entity_id, json.loads(r.payload_json)['session_date']): r
                   for r in selected if r.dataset == 'daily_bars' and r.event_at < cutoff}
        from .market import membership, crosses_action
        for symbol in membership(config, session, cutoff):
            dates = config.sessions[i-60:i+1]
            if (symbol not in membership(config, dates[0], timestamp(dates[0]+'T18:00:00+09:00'))
                    or crosses_action(config, symbol, dates[0], session, cutoff)):
                continue
            history = [bars.get((symbol, d)) for d in dates]
            if any(b is None or b['close'] is None for b in history):
                continue
            prices = [float(b['close']) for b in history]
            values = {f'return_{n}': prices[-1]/prices[-n-1]-1 for n in (5, 20, 60)}
            values.update(volatility_20=st.stdev([b/a-1 for a,b in zip(prices[-21:-1], prices[-20:])]),
                          ma_ratio_20=prices[-1]/st.mean(prices[-20:])-1)
            if volume:
                a,b = history[-6]['volume'],history[-1]['volume']
                if a is None or b is None or a <= 0:
                    continue
                values['volume_change_5'] = b/a-1
            lineage = {}
            for name in names:
                n = int(name.split('_')[-1]) if name.startswith(('return_', 'volume_change_')) else 20
                used_dates = dates[-n-1:] if name != 'ma_ratio_20' else dates[-20:]
                used = [sources[symbol,d] for d in used_dates]
                lineage[name] = dict(available_at=max(max(r.available_at,r.ingested_at) for r in used).isoformat(),
                                     sources=[dict(session=d, revision_id=r.revision_id,
                                                   observation_hash=digest(canonical(encode_observation(r))))
                                              for d,r in zip(used_dates,used)])
            output.append(dict(session=session, symbol=symbol, decision_at=cutoff.isoformat(),
                               values=values, lineage=lineage))
    return output, names


def labels(rows, config, horizon):
    # Freeze each label at its endpoint decision; later revisions cannot rewrite it.
    from .backtest import _bars
    result = {}
    for i in range(len(config.sessions)-horizon):
        start,end = config.sessions[i],config.sessions[i+horizon]
        cutoff = timestamp(end+'T18:00:00+09:00')
        bars = _bars(rows, cutoff, config)
        from .market import crosses_action, membership
        for symbol in config.universe:
            if (symbol not in membership(config, start, timestamp(start+'T18:00:00+09:00'))
                    or symbol not in membership(config, end, cutoff)):
                continue
            if crosses_action(config, symbol, start, end, cutoff):
                continue
            a,b = bars.get((symbol,start),{}).get('close'),bars.get((symbol,end),{}).get('close')
            if a is not None and b is not None:
                ret = b/a-1
                result[start,symbol] = dict(y=int(ret>0), forward_return=ret, available_at=cutoff.isoformat(), end=end)
    return result


def sigmoid(x):
    return 1/(1+math.exp(-max(-35,min(35,x))))


def predict(model, values):
    z = [(values[k]-m)/s for k,m,s in zip(model['names'],model['mean'],model['scale'])]
    return sigmoid(model['intercept']+sum(w*x for w,x in zip(model['weights'],z)))


def fit(samples, names, cfg, penalty):
    xs = [[f['values'][k] for k in names] for f,y in samples]
    means = [st.mean(c) for c in zip(*xs)]
    scales = [st.pstdev(c) or 1.0 for c in zip(*xs)]
    zs = [[(x-m)/s for x,m,s in zip(row,means,scales)] for row in xs]
    rng = random.Random(cfg.seed)
    w,b = [rng.uniform(-0.001,0.001) for _ in names],0.0
    for _ in range(cfg.epochs):
        errors = [sigmoid(b+sum(a*x for a,x in zip(w,z)))-y['y'] for z,(_,y) in zip(zs,samples)]
        b -= cfg.learning_rate*st.mean(errors)
        w = [a-cfg.learning_rate*(sum(e*z[j] for e,z in zip(errors,zs))/len(zs)+penalty*a) for j,a in enumerate(w)]
    return dict(type='logistic_regression_batch_gradient_v1', names=names, mean=means, scale=scales,
                weights=w, intercept=b, penalty=penalty, seed=cfg.seed)


def prediction_metrics(pairs):
    if not pairs:
        return dict(count=0)
    ps,ys = zip(*pairs)
    tp=sum(p>=0.5 and y==1 for p,y in pairs); fp=sum(p>=0.5 and y==0 for p,y in pairs)
    positives=sum(ys); negatives=len(ys)-positives
    bins=[]
    for i in range(10):
        group=[(p,y) for p,y in pairs if i/10 <= p < (i+1)/10 or (i==9 and p==1)]
        bins.append(dict(lower=i/10, count=len(group), mean_probability=st.mean(p for p,y in group) if group else None,
                         observed_frequency=st.mean(y for p,y in group) if group else None))
    return dict(count=len(pairs), accuracy=st.mean(int((p>=.5)==bool(y)) for p,y in pairs),
                precision=tp/(tp+fp) if tp+fp else None, recall=tp/positives if positives else None,
                roc_auc=sum((a>b)+.5*(a==b) for a,y in pairs if y for b,t in pairs if not t)/(positives*negatives) if positives*negatives else None,
                brier_score=st.mean((p-y)**2 for p,y in pairs),
                log_loss=-st.mean(y*math.log(max(p,1e-15))+(1-y)*math.log(max(1-p,1e-15)) for p,y in pairs),
                expected_calibration_error=sum(b['count']*abs(b['mean_probability']-b['observed_frequency']) for b in bins if b['count'])/len(pairs),
                calibration=bins)


def walk_forward(rows, config, cfg):
    if config.pit_mode != 'observed':
        raise ValueError('ML requires observed PIT')
    if len(config.sessions) <= cfg.first_prediction:
        raise ValueError('insufficient sessions for warmup/train/validation/purge/prediction')
    fs,definitions=features(rows,config,cfg.volume)
    ys=labels(rows,config,cfg.horizon)
    predictions,folds=[],[]
    for start in range(cfg.first_prediction,len(config.sessions),cfg.step):
        v_end=start-cfg.horizon; v_start=v_end-cfg.validation_sessions
        t_end=v_start-cfg.horizon; t_start=t_end-cfg.train_sessions
        train_dates=config.sessions[t_start:t_end]; val_dates=config.sessions[v_start:v_end]
        def samples(dates, cutoff):
            return [(f,ys[f['session'],f['symbol']]) for f in fs if f['session'] in dates and
                    (f['session'],f['symbol']) in ys and timestamp(ys[f['session'],f['symbol']]['available_at']) < timestamp(cutoff)]
        train=samples(train_dates, config.sessions[v_start]+'T18:00:00+09:00')
        val=samples(val_dates, config.sessions[start]+'T18:00:00+09:00')
        if not train or not val:
            raise ValueError('empty train/validation window')
        candidates=[]
        for penalty in cfg.penalties:
            model=fit(train,list(definitions),cfg,penalty)
            score=prediction_metrics([(predict(model,f['values']),y['y']) for f,y in val])['log_loss']
            candidates.append((score,penalty,model))
        score,_,model=min(candidates,key=lambda x:(x[0],x[1]))
        fold=dict(train_period=[train_dates[0],train_dates[-1]], validation_period=[val_dates[0],val_dates[-1]],
                  prediction_period=[config.sessions[start],config.sessions[min(start+cfg.step,len(config.sessions))-1]],
                  train_keys=[[f['session'],f['symbol']] for f,y in train],
                  validation_keys=[[f['session'],f['symbol']] for f,y in val],
                  train_label_max_available_at=max(y['available_at'] for f,y in train),
                  validation_label_max_available_at=max(y['available_at'] for f,y in val),
                  model=model, model_artifact_hash=digest(canonical(model)), validation_log_loss=score,
                  candidate_scores=[dict(penalty=p,log_loss=s) for s,p,m in candidates])
        folds.append(fold)
        for f in fs:
            if f['session'] in config.sessions[start:start+cfg.step]:
                predictions.append(dict(session=f['session'],symbol=f['symbol'],probability=predict(model,f['values']),fold=len(folds)-1))
    scored=[(p['probability'],ys[p['session'],p['symbol']]['y']) for p in predictions if (p['session'],p['symbol']) in ys]
    result = dict(feature_definition=definitions, features=fs, target_definition=f'close[t+{cfg.horizon}]/close[t]-1 > 0; endpoint PIT; label only',
                evaluation_period=[config.sessions[cfg.first_prediction],config.sessions[-1]],
                config=json.loads(canonical(asdict(cfg))), preprocessing=dict(type='standard_scaler',fit='train_only',ddof=0,shuffle=False),
                folds=folds,predictions=predictions,prediction_metrics=prediction_metrics(scored),
                constant_probability_reference=prediction_metrics([(.5,y) for p,y in scored]),
                unscored_predictions=len(predictions)-len(scored), evidence='synthetic fixture results are not evidence of AI performance')

    if config.market is not None:
        from .statistics import prediction_intervals
        result['prediction_confidence_intervals'] = prediction_intervals(predictions, ys, config.market['statistical_evaluation'])
    return result
