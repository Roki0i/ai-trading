"""Versioned cash-equity calendar and observed-PIT market reference data.

No inferred historical availability, present-day membership backfill, or globally
adjusted prices. Unknown coverage and unsupported entitlements fail closed.
"""
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .models import as_of, timestamp
from .storage import canonical, digest


@dataclass(frozen=True)
class TradingCalendar:
    version: str
    source: str
    days: dict  # EVERY calendar date -> open / closure reason
    available_at: str

    def __post_init__(self):
        if not self.version or not self.source or not self.days:
            raise ValueError('calendar version/source/coverage required')
        timestamp(self.available_at)
        for day in self.days:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("calendar requires ISO dates")
        dates = sorted(self.days)
        d = date.fromisoformat(dates[0])
        while d.isoformat() <= dates[-1]:
            reason = self.days.get(d.isoformat())
            if not isinstance(reason, str) or not reason:
                raise ValueError('calendar coverage gap')
            if reason == 'open' and (d.weekday() >= 5 or (d.month, d.day) in ((1,1),(1,2),(1,3),(12,31))):
                raise ValueError('invalid Japanese cash-equity session')
            d += timedelta(days=1)

    def sessions(self, start, end):
        if start not in self.days or end not in self.days or start > end:
            raise ValueError('calendar outside coverage')
        return tuple(d for d in sorted(self.days) if start <= d <= end and self.days[d] == 'open')

    def shift(self, session, count):
        sessions = self.sessions(min(self.days), max(self.days))
        i = sessions.index(session) + count
        if not 0 <= i < len(sessions):
            raise ValueError('calendar shift outside coverage')
        return sessions[i]


def reference_rows(market, dataset):
    from .experiment import decode_rows
    return decode_rows(market[dataset])


def known(market, dataset, cutoff):
    return as_of(reference_rows(market, dataset), cutoff)


def membership(config, session, cutoff):
    if config.market is None:
        return config.universe
    result = []
    for row in known(config.market, 'historical_universe', cutoff):
        p = json.loads(row.payload_json)
        if p['listing_date'] <= session and (p['delisting_date'] is None or session < p['delisting_date']):
            result.append(row.entity_id)
    if len(result) != len(set(result)):
        raise ValueError('ambiguous historical universe')
    return tuple(sorted(set(result) & set(config.universe)))


def actions(config, cutoff):
    selected = [] if config.market is None else known(config.market, 'corporate_actions', cutoff)
    keys = [(r.entity_id, r.event_at) for r in selected]
    if len(set(keys)) != len(keys):
        raise ValueError('ambiguous simultaneous corporate actions')
    return selected


def crosses_action(config, symbol, start, end, cutoff):
    return any(r.entity_id == symbol and start < json.loads(r.payload_json)['effective_date'] <= end
               for r in actions(config, cutoff))


def validate_market(market, sessions, universe):
    required = {'data_provider', 'raw_data_hash', 'raw_data', 'calendar', 'historical_universe', 'corporate_actions',
                'missing_data_policy', 'cost_scenario', 'statistical_evaluation'}
    if set(market) != required:
        raise ValueError('incomplete market context')
    if not market['data_provider'] or len(market['raw_data_hash']) != 64:
        raise ValueError('provider/raw hash required')
    if digest(canonical(market['raw_data'])) != market['raw_data_hash']:
        raise ValueError('raw data hash mismatch')
    calendar = TradingCalendar(**market['calendar'])
    if timestamp(calendar.available_at) >= timestamp(sessions[0]+'T18:00:00+09:00'):
        raise ValueError('calendar was unavailable at first decision')
    if tuple(sessions) != calendar.sessions(sessions[0], sessions[-1]):
        raise ValueError('sessions must equal complete trading calendar interval')
    if market['missing_data_policy'] != {'version': 'strict_v1', 'held_missing_price': 'stop',
                                         'missing_volume': 'skip', 'api_gap': 'record_skip',
                                         'insufficient_history': 'skip', 'max_halt_sessions': 5}:
        raise ValueError('unsupported missing-data policy')
    from .statistics import BootstrapConfig
    BootstrapConfig(**market['statistical_evaluation'])
    for r in reference_rows(market, 'historical_universe'):
        p = json.loads(r.payload_json)
        if r.dataset != 'historical_universe' or r.entity_id not in universe:
            raise ValueError('invalid universe observation')
        date.fromisoformat(p['listing_date'])
        if p['delisting_date'] is not None:
            date.fromisoformat(p['delisting_date'])
        if p['delisting_date'] is not None and p['delisting_date'] <= p['listing_date']:
            raise ValueError('invalid listing interval')
        if r.event_at != timestamp(p['listing_date'] + 'T00:00:00+09:00'):
            raise ValueError('listing identity must remain stable across revisions')
    for r in reference_rows(market, 'corporate_actions'):
        p = json.loads(r.payload_json)
        if r.dataset != 'corporate_actions' or r.entity_id not in universe:
            raise ValueError('invalid action observation')
        if p['kind'] not in ('stock_split', 'reverse_split', 'dividend'):
            raise ValueError('unknown corporate action')
        if p['effective_date'] not in calendar.days or calendar.days[p['effective_date']] != 'open':
            raise ValueError('action outside trading calendar')
        value = Decimal(str(p['ratio'] if p['kind'] != 'dividend' else p['cash_per_share']))
        if not value.is_finite() or value <= 0:
            raise ValueError('invalid corporate action value')
        if p['kind'] == 'stock_split' and value <= 1 or p['kind'] == 'reverse_split' and value >= 1:
            raise ValueError('inconsistent split ratio')
        if r.event_at != timestamp(p['effective_date']+'T00:00:00+09:00'):
            raise ValueError('action event/effective date mismatch')
        if p['kind'] == 'dividend':
            date.fromisoformat(p['payment_date'])
        if p['kind'] == 'dividend' and p['payment_date'] < p['effective_date']:
            raise ValueError('dividend payment before entitlement')


def provenance(market):
    return dict(data_provider=market['data_provider'], raw_data_hash=market['raw_data_hash'],
                trading_calendar_version=market['calendar']['version'],
                trading_calendar_hash=digest(canonical(market['calendar'])),
                historical_universe_hash=digest(canonical(market['historical_universe'])),
                corporate_action_data_hash=digest(canonical(market['corporate_actions'])),
                missing_data_policy=market['missing_data_policy'], cost_scenario=market['cost_scenario'],
                statistical_evaluation_config=market['statistical_evaluation'])


def raw_observations(market):
    if market['data_provider'] == 'synthetic_jquants_v2':
        from .experiment import decode_rows
        return decode_rows(market['raw_data'])
    if market['data_provider'] == 'jquants_v2':
        from .providers import MarketDataProvider
        return MarketDataProvider.regenerate(market['raw_data'])
    raise ValueError('unsupported data provider')
