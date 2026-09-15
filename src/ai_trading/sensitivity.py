"""Paired cost experiments retain each full configuration and Experiment ID."""
from dataclasses import asdict
from .statistics import return_difference

SCENARIOS = dict(optimistic=dict(commission_bps='0', slippage_bps='0'),
                 base=dict(commission_bps='10', slippage_bps='5'),
                 conservative=dict(commission_bps='50', slippage_bps='25'))


def compare_costs(store, rows, config, split='development', seed=7):
    if split == 'holdout':
        raise PermissionError('cost selection cannot access holdout')
    results, report = {}, {}
    for name, costs in SCENARIOS.items():
        c = asdict(config)
        c.update(costs)
        c['market']['cost_scenario'] = name
        model = store.run(rows, c, split, seed)
        if model.status != 'completed':
            raise ValueError(model.failure_reason)
        store.verify(model.experiment_id)
        results[name] = store.read(model.experiment_id)['outcome']['result']
        report[name] = dict(experiment_id=model.experiment_id, metrics=model.metrics)
    for name in ('base', 'conservative'):
        report[name]['daily_return_difference_vs_optimistic'] = return_difference(
            results[name], results['optimistic'], config.market['statistical_evaluation'])
    return report
