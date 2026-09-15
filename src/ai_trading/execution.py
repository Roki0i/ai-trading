"""Shared daily-volume execution limit; no order book or liquidation inference."""
from decimal import Decimal


def participation_quantity(requested, volume, participation, lot_size, used=0):
    if volume is None:
        return 0
    v, p = Decimal(str(volume)), Decimal(str(participation))
    if not v.is_finite() or v < 0 or not p.is_finite() or not 0 < p <= 1:
        raise ValueError('invalid volume/participation')
    capacity = max(0, int(v * p) - used) // lot_size * lot_size
    return min(requested, capacity)


def delisting_policy(status, *, evidence=None, held=False):
    """Classification is supplied by evidence, never inferred from missing prices."""
    states = {'tradable', 'last_trading_day', 'trading_halt', 'delisting',
              'cash_out', 'merger', 'value_unknown'}
    if status not in states:
        raise ValueError('unknown market status')
    if status != 'value_unknown' and not evidence:
        raise ValueError('market status evidence required')
    if held and status in {'delisting', 'cash_out', 'merger', 'value_unknown'}:
        raise ValueError('valuation stopped: settlement value/entitlement unsupported')
    return dict(status=status, evidence=evidence,
                execution_allowed=status in {'tradable', 'last_trading_day'},
                valuation='stop' if status in {'delisting', 'cash_out', 'merger', 'value_unknown'} else 'observed_price')
