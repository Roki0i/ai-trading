import copy
import json
import tempfile
import unittest
from dataclasses import replace, asdict
from pathlib import Path
from unittest.mock import patch
from test_backtest import SESSIONS, bar, market, config
from ai_trading.backtest import run_backtest
from ai_trading.market import TradingCalendar, membership
from ai_trading.market_fixture import context, reference
from ai_trading.models import timestamp
from ai_trading.statistics import interval, return_difference, prediction_intervals
from ai_trading.storage import canonical, digest, encode_observation


def cfg(**kwargs):
    c = config(**kwargs)
    return replace(c, market=context(c.sessions,c.universe))


def action(kind='stock_split', effective=SESSIONS[2], **values):
    return reference('fixture:A','corporate_actions',effective,
                     dict(kind=kind,effective_date=effective,**values),known=SESSIONS[0])


class CalendarTests(unittest.TestCase):
    def test_holiday_and_next_session(self):
        cal=TradingCalendar(**cfg().market['calendar'])
        self.assertEqual(cal.shift('2025-01-10',1),'2025-01-14')
        self.assertNotIn('2025-01-13',cal.sessions(SESSIONS[0],SESSIONS[-1]))

    def test_non_session_and_gap_rejected(self):
        for ss in (SESSIONS+('2025-01-25',), SESSIONS[:2]+SESSIONS[3:]):
            with self.assertRaises(ValueError): cfg(sessions=ss)

    def test_year_end(self):
        c=TradingCalendar(**context(('2024-12-30','2025-01-06'),('A',))['calendar'])
        self.assertEqual(c.shift('2024-12-30',1),'2025-01-06')

    def test_unknown_coverage(self):
        c=TradingCalendar(**cfg().market['calendar'])
        with self.assertRaises(ValueError): c.shift(SESSIONS[-1],1)


class MarketTests(unittest.TestCase):
    def test_future_listing_not_eligible(self):
        c=cfg(); c.market['historical_universe']=[reference('fixture:A','historical_universe',SESSIONS[5],
             dict(listing_date=SESSIONS[5],delisting_date=None))]
        r=run_backtest(market(),c)
        self.assertTrue(all(o['decision_session']>=SESSIONS[5] for o in r['orders']))

    def test_delisted_stays_in_past_universe_and_survivorship_difference(self):
        c=cfg(universe=('fixture:A','fixture:B'))
        c.market['historical_universe'][1]['payload_json']=canonical(dict(listing_date='2023-01-04',delisting_date='2025-02-01')).decode()
        rows=market()+[bar(d,'B',100-i*5) for i,d in enumerate(SESSIONS)]
        historical=run_backtest(rows,c)
        biased=run_backtest(rows,cfg())
        self.assertIn('fixture:B',historical['snapshots'][1]['positions'])
        self.assertLess(historical['metrics']['cumulative_return'],biased['metrics']['cumulative_return'])

    def test_delisting_position_stops(self):
        c=cfg(); c.market['historical_universe'][0]['payload_json']=canonical(dict(listing_date='2023-01-04',delisting_date=SESSIONS[3])).decode()
        with self.assertRaisesRegex(ValueError,'delisting'): run_backtest(market(),c)

    def test_split_conserves_equity_and_quantity(self):
        c=cfg(strategy='buy_and_hold'); c.market['corporate_actions']=[action(ratio=2)]
        rows=market([100,100]+[50]*9); rows[2]=bar(SESSIONS[2],price=50,adjustment_factor=.5)
        r=run_backtest(rows,c)
        self.assertEqual(r['snapshots'][2]['positions']['fixture:A'],200)
        self.assertEqual(float(r['snapshots'][2]['equity']),10000)

    def test_reverse_split(self):
        c=cfg(strategy='buy_and_hold'); c.market['corporate_actions']=[action('reverse_split',ratio=.5)]
        rows=market([100,100]+[200]*9); rows[2]=bar(SESSIONS[2],price=200,adjustment_factor=2)
        self.assertEqual(run_backtest(rows,c)['snapshots'][2]['positions']['fixture:A'],50)

    def test_fractional_reverse_split_stops(self):
        c=cfg(initial_cash='9900'); c.market['corporate_actions']=[action('reverse_split',ratio=.5)]
        rows=market(); rows[2]=bar(SESSIONS[2],adjustment_factor=2)
        with self.assertRaisesRegex(ValueError,'fractional'): run_backtest(rows,c)

    def test_dividend_receivable_then_cash(self):
        c=cfg(strategy='buy_and_hold'); c.market['corporate_actions']=[action('dividend',cash_per_share=2,payment_date=SESSIONS[4])]
        r=run_backtest(market([100,100]+[98]*9),c)
        self.assertEqual(float(r['snapshots'][2]['equity']),10000)
        self.assertEqual(float(r['snapshots'][4]['cash']),200)

    def test_future_action_does_not_change_past_decisions(self):
        c=cfg(); a=run_backtest(market(),c)
        c.market['corporate_actions']=[action(effective=SESSIONS[-1],ratio=2)]
        rows=market();rows[-1]=bar(SESSIONS[-1],price=50,adjustment_factor=.5)
        b=run_backtest(rows,c)
        self.assertEqual(a['snapshots'][:-1],b['snapshots'][:-1])

    def test_future_revision_invariant(self):
        c=cfg(); rows=market(); later=timestamp('2026-01-01T00:00:00Z')
        correction=replace(bar(SESSIONS[0],price=999),available_at=later,ingested_at=later,revision_id='later')
        self.assertEqual(run_backtest(rows,c),run_backtest(rows+[correction],c))

    def test_missing_held_price_stops(self):
        with self.assertRaisesRegex(ValueError,'missing held price'): run_backtest(market()[:2]+market()[3:],cfg())

    def test_missing_volume_no_execution_and_recorded(self):
        rows=market(); rows[1]=bar(SESSIONS[1],volume=None)
        r=run_backtest(rows,cfg())
        self.assertEqual(r['fills'][0]['session'],SESSIONS[2])
        self.assertIn('missing_volume',[e['reason'] for e in r['missing_data_events']])

    def test_api_gap_recorded(self):
        r=run_backtest(market()[1:],cfg())
        self.assertEqual(r['missing_data_events'][0]['reason'],'api_gap')

    def test_long_halt_stops(self):
        rows=market()[:2]+[bar(d,volume=0) for d in SESSIONS[2:]]
        with self.assertRaisesRegex(ValueError,'long trading halt'): run_backtest(rows,cfg())

    def test_unknown_action_and_factor_stops(self):
        c=cfg(); c.market['corporate_actions']=[action('merger',ratio=2)]
        with self.assertRaises(ValueError): replace(c)
        with self.assertRaises(ValueError): run_backtest([bar(SESSIONS[0],adjustment_factor=None)],cfg())

    def test_costs_monotonically_worse_flat_market(self):
        rs=[run_backtest(market(),cfg(commission_bps=str(x),slippage_bps=str(x)))['metrics']['cumulative_return'] for x in (0,10,50)]
        self.assertGreater(rs[0],rs[1]);self.assertGreater(rs[1],rs[2])

    def test_missing_history_no_momentum_order(self):
        r=run_backtest(market(),cfg(strategy='momentum',lookback=20))
        self.assertFalse(r['orders'])
        self.assertTrue(r['missing_data_events'])

    def test_reproducible_experiment_and_holdout_sealed(self):
        from ai_trading.experiment import initialize, ExperimentStore
        study=dict(periods=dict(development=dict(start=SESSIONS[0],end=SESSIONS[-1]),validation=dict(start='2025-02-01',end='2025-02-28'),holdout=dict(start='2025-03-01',end='2025-03-31')),
                   universe_definition=dict(type='historical_point_in_time',symbols=['fixture:A'],known_at='2023-01-01T00:00:00Z',evidence='fixture'))
        with tempfile.TemporaryDirectory() as tmp:
            initialize(tmp,study); store=ExperimentStore(tmp)
            c=cfg(); c.market['raw_data']=[encode_observation(r) for r in market()]
            c.market['raw_data_hash']=digest(canonical(c.market['raw_data']))
            a=store.run(market(),c);b=store.run(market(),c)
            self.assertEqual(a.status,'completed',a.failure_reason)
            self.assertEqual(a.result_hash,b.result_hash);store.verify(a.experiment_id)
            self.assertIsNotNone(a.market_provenance)
            with self.assertRaises(PermissionError):store.run(market(),cfg(),split='holdout',holdout=True)
            self.assertFalse((Path(tmp)/'holdout').exists())


class StatisticsTests(unittest.TestCase):
    def test_deterministic_blocks(self):
        import statistics
        c=dict(block_length=3,replicates=100,seed=4)
        a=interval(list(range(30)),statistics.mean,c)
        self.assertEqual(a,interval(list(range(30)),statistics.mean,c))
        self.assertLess(a['lower'],a['estimate']);self.assertGreater(a['upper'],a['estimate'])

    def test_short_sample_no_spurious_interval(self):
        self.assertEqual(interval([1,2],sum,dict(block_length=5))['reason'],'insufficient_blocks')

    def test_paired_identical_difference_zero(self):
        r=run_backtest(market(),cfg())
        ci=return_difference(r,r,dict(block_length=2))
        self.assertEqual((ci['lower'],ci['upper']),(0,0))

    def test_prediction_groups_cluster_symbols(self):
        ps=[dict(session=d,symbol=s,probability=.9) for d in SESSIONS for s in ('A','B')]
        ys={(p['session'],p['symbol']):dict(y=1) for p in ps}
        r=prediction_intervals(ps,ys,dict(block_length=2))
        self.assertEqual(r['accuracy']['sessions'],len(SESSIONS));self.assertEqual(r['accuracy']['lower'],1)

    def test_raw_provider_regeneration_offline(self):
        from ai_trading.providers import MarketDataProvider, FixtureTransport
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('network')):
            path=Path('tests/fixtures/jquants_daily.json')
            # Existing transport fixture request dates discovered from its raw request specification.
            path=next(p for p in Path('tests/fixtures').glob('*.json') if isinstance(json.loads(p.read_text()),dict) and
                      'pages' in json.loads(p.read_text()) and 'path' in json.loads(p.read_text())['pages'][0])
            transport=FixtureTransport(path); provider=MarketDataProvider(transport,tmp)
            dates=sorted({p['params']['date'] for p in transport.pages})
            raw=provider.acquire(dates); bundle=json.loads(raw.read_text())
            self.assertEqual(provider.regenerate(bundle),provider.regenerate(bundle))
            bundle['receipts'][0]['body']='{}'
            with self.assertRaisesRegex(ValueError,'hash'):provider.regenerate(bundle)


class HardenedBoundaryTests(unittest.TestCase):
    def test_calendar_available_after_decision_rejected(self):
        c=cfg();c.market['calendar']['available_at']='2026-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'calendar was unavailable'): run_backtest(market(),c)

    def test_raw_hash_tampering_rejected(self):
        c=cfg();c.market['raw_data'].append({})
        with self.assertRaisesRegex(ValueError,'raw data hash'):run_backtest(market(),c)

    def test_inconsistent_split_factor_stops(self):
        c=cfg();c.market['corporate_actions']=[action(ratio=2)]
        with self.assertRaisesRegex(ValueError,'inconsistent'):run_backtest(market(),c)

    def test_late_action_stops(self):
        c=cfg();a=action(ratio=2)
        for k in ('published_at','available_at','ingested_at'):a[k]=SESSIONS[4]+'T17:00:00+09:00'
        c.market['corporate_actions']=[a]
        with self.assertRaisesRegex(ValueError,'late corporate action'):run_backtest(market(),c)

    def test_applied_action_revision_stops(self):
        c=cfg();a=action(ratio=2);b=copy.deepcopy(a)
        for k in ('published_at','available_at','ingested_at'):b[k]=SESSIONS[4]+'T17:00:00+09:00'
        b['payload_json']=canonical(dict(kind='stock_split',effective_date=SESSIONS[2],ratio=3)).decode()
        b['revision_id']='correction';c.market['corporate_actions']=[a,b]
        rows=market();rows[2]=bar(SESSIONS[2],price=50,adjustment_factor=.5)
        with self.assertRaises(ValueError):run_backtest(rows,c)

    def test_action_crossing_features_and_labels_excluded(self):
        from ai_trading.ml import features, labels
        from ai_trading.market_fixture import fixture
        rows,c,_=fixture();d=c.sessions[65]
        c.market['corporate_actions']=[reference('fixture:A','corporate_actions',d,dict(kind='stock_split',effective_date=d,ratio=2))]
        rows=[replace(r,payload_json=canonical(dict(json.loads(r.payload_json),adjustment_factor=.5)).decode())
              if json.loads(r.payload_json)['session_date']==d and r.entity_id=='fixture:A' else r for r in rows]
        fs,_=features(rows,c)
        self.assertFalse([f for f in fs if f['symbol']=='fixture:A' and d<=f['session']<=c.sessions[100]])
        self.assertNotIn((c.sessions[62],'fixture:A'),labels(rows,c,5))

    def test_calendar_provider_preserves_cash_holiday(self):
        from ai_trading.providers import acquire_reference, calendar_from_raw, Response
        class Mock:
            def get(self,path,params):
                return Response(canonical(dict(data=[dict(Date='2025-01-10',HolDiv='1'),
                    dict(Date='2025-01-11',HolDiv='0'),dict(Date='2025-01-12',HolDiv='0'),
                    dict(Date='2025-01-13',HolDiv='3'),dict(Date='2025-01-14',HolDiv='1')])),timestamp('2024-12-01T00:00:00Z'))
        with tempfile.TemporaryDirectory() as tmp:
            path=acquire_reference(Mock(),'/markets/calendar',{'from':'2025-01-10','to':'2025-01-14'},tmp)
            c=calendar_from_raw(json.loads(path.read_text()))
            self.assertEqual(c.shift('2025-01-10',1),'2025-01-14')
            with self.assertRaisesRegex(ValueError,'snapshot date'):
                acquire_reference(Mock(),'/equities/master',{},tmp)

    def test_experiment_raw_lineage_and_holdout_payload_guard(self):
        from ai_trading.experiment import initialize, ExperimentStore
        from ai_trading.market_fixture import fixture
        rows,c,study=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            initialize(tmp,study); store=ExperimentStore(tmp)
            c=replace(c,strategy='equal_weight',ml=None)
            c.market['raw_data']=[];c.market['raw_data_hash']=digest(canonical([]))
            failed=store.run(rows,c)
            self.assertEqual(failed.status,'failed')
            self.assertIn('raw lineage',failed.failure_reason)
            future=encode_observation(bar('2025-07-01'))
            c.market['raw_data']=[future];c.market['raw_data_hash']=digest(canonical([future]))
            before=len(store.list())
            with self.assertRaisesRegex(PermissionError,'boundary'):store.run(rows,c)
            self.assertEqual(before,len(store.list()))
            self.assertFalse((Path(tmp)/'holdout').exists())

    def test_prelisting_prices_cannot_supply_ipo_features(self):
        from ai_trading.ml import features
        from ai_trading.market_fixture import fixture
        rows,c,_=fixture();d=c.sessions[80]
        c.market['historical_universe'][0]=reference('fixture:A','historical_universe',d,
                                                    dict(listing_date=d,delisting_date=None))
        fs,_=features(rows,c)
        self.assertFalse([f for f in fs if f['symbol']=='fixture:A' and f['session']<c.sessions[140]])
