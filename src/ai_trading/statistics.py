"""Seeded circular moving-block bootstrap; paired dates and clustered symbols."""
import math
import random
import statistics as st
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class BootstrapConfig:
    block_length: int = 5
    replicates: int = 500
    confidence: float = .95
    seed: int = 7

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (self.block_length, self.replicates)):
            raise ValueError('positive bootstrap sizes required')
        if not 0 < self.confidence < 1 or type(self.seed) is not int:
            raise ValueError('invalid bootstrap confidence/seed')


def interval(groups, statistic, config):
    cfg = BootstrapConfig(**config)
    n = len(groups)
    if n < 2 * cfg.block_length:
        return dict(estimate=statistic(groups) if groups else None, lower=None, upper=None,
                    reason='insufficient_blocks', sessions=n, config=asdict(cfg))
    rng = random.Random(cfg.seed)
    values = []
    for _ in range(cfg.replicates):
        indices = []
        while len(indices) < n:
            start = rng.randrange(n)
            indices.extend((start+j) % n for j in range(cfg.block_length))
        values.append(statistic([groups[i] for i in indices[:n]]))
    values.sort()
    alpha = (1-cfg.confidence)/2
    return dict(estimate=statistic(groups), lower=values[int(alpha*(len(values)-1))],
                upper=values[math.ceil((1-alpha)*(len(values)-1))], sessions=n, config=asdict(cfg))


def returns(result):
    previous = float(result['config']['initial_cash'])
    out = []
    for s in result['snapshots']:
        equity = float(s['equity'])
        out.append((s['session'], equity/previous-1))
        previous = equity
    return out


def return_difference(left, right, config):
    a, b = returns(left), returns(right)
    if [d for d,v in a] != [d for d,v in b]:
        raise ValueError('paired return dates differ')
    return interval([x-y for (_,x),(_,y) in zip(a,b)], st.mean, config)


def prediction_intervals(predictions, labels, config):
    grouped = {}
    for p in predictions:
        key = p['session'], p['symbol']
        if key in labels:
            grouped.setdefault(p['session'], []).append((p['probability'], labels[key]['y']))
    groups = [grouped[d] for d in sorted(grouped)]
    def score(gs, name):
        pairs = [pair for g in gs for pair in g]
        return st.mean((int((p >= .5) == bool(y)) if name == 'accuracy' else (p-y)**2) for p,y in pairs)
    return {name: interval(groups, lambda gs: score(gs, name), config) for name in ('accuracy','brier_score')}
