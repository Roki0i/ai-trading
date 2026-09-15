import copy
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from test_backtest import SESSIONS, bar, market
from test_market import cfg, action
from ai_trading.backtest import run_backtest
from ai_trading.execution import participation_quantity, delisting_policy
from ai_trading.forward import Ledger, freeze
from ai_trading.providers import (Response, JQuantsTransport, FixtureTransport,
                                 MarketDataProvider, acquire_reference, validate_daily_row)
from ai_trading.storage import canonical, digest, encode_observation, put
from ai_trading.validation import coverage, historical_checks, smoke, validate_split


def study():
    return dict(periods=dict(development=dict(start=SESSIONS[0],end=SESSIONS[-1]),
        validation=dict(start='2025-02-01',end='2025-02-28'),holdout=dict(start='2025-03-01',end='2025-03-31')),
        universe_definition=dict(type='historical_point_in_time',symbols=['fixture:A'],
                                 known_at='2023-01-01T00:00:00Z',evidence='synthetic'))


def raw(n):
    return [encode_observation(r) for r in market()[:n]]


class ProviderValidationTests(unittest.TestCase):
    def test_schema_nullable_missing(self):
        validate_daily_row(dict(Date=SESSIONS[0],Code='72030',C=None))

    def test_schema_malformed_numeric(self):
        for v in ('100',True,float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                validate_daily_row(dict(Date=SESSIONS[0],Code='72030',C=v))

    def test_schema_identity(self):
        with self.assertRaises(ValueError): validate_daily_row(dict(Date=SESSIONS[0],Code=72030))

    def test_fixture_pagination_and_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            t=FixtureTransport(Path('tests/fixtures/daily_pages.json'))
            p=MarketDataProvider(t,tmp).acquire([SESSIONS[0]])
            b=json.loads(p.read_bytes())
            self.assertEqual(len(t.calls),2)
            r=coverage(b,SESSIONS[:2],['90001'])['symbols']['90001']
            self.assertEqual(r['missing_sessions'],[SESSIONS[1]])
            self.assertEqual(r['price_coverage'],1)
            self.assertTrue(r['acquired']);self.assertFalse(r['research_complete'])

    def test_duplicate_and_revision_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=json.loads(MarketDataProvider(FixtureTransport(Path('tests/fixtures/daily_pages.json')),tmp).acquire([SESSIONS[0]]).read_bytes())
            b['receipts'].append(copy.deepcopy(b['receipts'][0]))
            changed=copy.deepcopy(b['receipts'][0]);body=json.loads(changed['body']);body['data'][0]['C']=106
            changed['body']=canonical(body).decode();changed['receipt']['body_sha256']=digest(canonical(body))
            b['receipts'].append(changed)
            r=coverage(b,[SESSIONS[0]],['90001'])['symbols']['90001']
            self.assertEqual(r['duplicate_rows'],1);self.assertEqual(r['revisions'],1)

    def test_failure_and_rate_limit_never_log_secret(self):
        secret='test-secret-do-not-store'
        with patch.dict(os.environ,JQUANTS_API_KEY=secret):
            for code in (401,403,429,500):
                with patch('ai_trading.providers.urlopen',side_effect=HTTPError('url',code,secret,{},None)):
                    with self.assertRaises(RuntimeError) as e: JQuantsTransport().get('/equities/bars/daily',{'date':SESSIONS[0]})
                    self.assertNotIn(secret,str(e.exception));self.assertIn(str(code),str(e.exception))

    def test_network_failure(self):
        with patch.dict(os.environ,JQUANTS_API_KEY='secret'),patch('ai_trading.providers.urlopen',side_effect=URLError('secret')):
            with self.assertRaisesRegex(RuntimeError,'network request failed'): JQuantsTransport().get('/equities/master',{'date':SESSIONS[0]})

    def test_secret_reflection_not_archived(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{"data":[],"key":"secret-value"}'
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,JQUANTS_API_KEY='secret-value'),patch('ai_trading.providers.urlopen',return_value=Reply()):
            with self.assertRaisesRegex(RuntimeError,'reflection'):
                MarketDataProvider(JQuantsTransport(),tmp).acquire([SESSIONS[0]])
            self.assertEqual(list(Path(tmp).rglob('*')),[])

    def test_fallback_without_network(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'JQUANTS_API_KEY':''}),patch('ai_trading.providers.urlopen',side_effect=AssertionError('network')):
            r=json.loads(smoke(tmp,[SESSIONS[0]],['90001']).read_bytes())
            self.assertEqual(r['mode'],'fixture_fallback')

    def test_reference_repeated_cursor(self):
        class T:
            def get(self,*args): return Response(canonical(dict(data=[],pagination_key='repeat')),bar(SESSIONS[0]).ingested_at)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'cursor'): acquire_reference(T(),'/equities/master',{'date':SESSIONS[0]},tmp)

    def test_current_master_cannot_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'snapshot date'): acquire_reference(None,'/equities/master',{},tmp)

    def test_incremental_bundle_retains_original_raw(self):
        from ai_trading.providers import merge_daily_bundles
        with tempfile.TemporaryDirectory() as tmp:
            b=json.loads(MarketDataProvider(FixtureTransport(Path('tests/fixtures/daily_pages.json')),tmp).acquire([SESSIONS[0]]).read_bytes())
            self.assertEqual(merge_daily_bundles(b,b),b)
            self.assertEqual(len(MarketDataProvider.regenerate(merge_daily_bundles(b,b))),2)

    def test_historical_checks_before_listing_after_delisting(self):
        c=cfg();r=c.market['historical_universe'][0]
        r['event_at']=SESSIONS[2]+'T00:00:00+09:00'
        r['payload_json']=canonical(dict(listing_date=SESSIONS[2],delisting_date=SESSIONS[5])).decode()
        checks=historical_checks(c,[SESSIONS[0],SESSIONS[3],SESSIONS[6]])
        self.assertEqual([r['universe'] for r in checks],[[],['fixture:A'],[]])

    def test_split_relation_requires_evidence(self):
        row=dict(Date=SESSIONS[2],Code='72030',C=50,AdjC=25,AdjFactor=.5)
        a=dict(kind='stock_split',ratio=2,effective_date=SESSIONS[2],evidence='synthetic announcement')
        r=validate_split(row,a,100)
        self.assertEqual(r['quantity_after'],200);self.assertEqual(r['previous_mark_multiplier'],'0.5')
        a.pop('evidence')
        with self.assertRaises(ValueError):validate_split(row,a,100)


class LiquidityTests(unittest.TestCase):
    def test_volume_limit(self):
        self.assertEqual(participation_quantity(1000,10000,'.01',100),100)

    def test_aggregate_daily_capacity(self):
        self.assertEqual(participation_quantity(1000,10000,'.01',1,used=90),10)

    def test_lot_no_fill(self):
        self.assertEqual(participation_quantity(100,999,'.01',100),0)

    def test_partial_fill(self):
        c=cfg(volume_participation='0.0001')
        r=run_backtest(market(),c)
        self.assertEqual(r['fills'][0]['quantity'],10)
        self.assertEqual(r['orders'][0]['unfilled_quantity'],90)

    def test_no_fill_missing_volume(self):
        rows=[replace(r,payload_json=canonical(dict(json.loads(r.payload_json),volume=None)).decode()) for r in market()]
        self.assertFalse(run_backtest(rows,cfg(volume_participation='.01'))['fills'])

    def test_no_fill_small_capacity(self):
        r=run_backtest(market(),cfg(volume_participation='.000001'))
        self.assertFalse(r['fills']);self.assertIn('last_no_fill',r['orders'][0])

    def test_partial_sale_cannot_exceed_proceeds_with_fixed_fee(self):
        c=cfg(universe=('fixture:A','fixture:B'),initial_cash='100000',commission_fixed='2000',volume_participation='.01')
        rows=[bar(d,price=100 if i<5 else 1000,volume=100000 if i<5 else 100) for i,d in enumerate(SESSIONS)]
        rows += [bar(d,'B') for d in SESSIONS]
        r=run_backtest(rows,c)
        self.assertTrue(any(o['side']=='sell' for o in r['orders']))
        self.assertTrue(all(float(s['cash'])>=0 for s in r['snapshots']))
        self.assertFalse([f for f in r['fills'] if f['side']=='sell'])

    def test_participation_invalid(self):
        for v in ('0','1.1','NaN'):
            with self.assertRaises(ValueError):cfg(volume_participation=v)

    def test_unknown_delisting_stops(self):
        for status in ('value_unknown','delisting','cash_out','merger'):
            with self.assertRaises(ValueError):delisting_policy(status,held=True,evidence='notice')

    def test_statuses_separate(self):
        self.assertTrue(delisting_policy('last_trading_day',evidence='notice')['execution_allowed'])
        self.assertFalse(delisting_policy('trading_halt',evidence='notice')['execution_allowed'])
        with self.assertRaises(ValueError):delisting_policy('tradable')

    def test_evidenced_halt_blocks_execution(self):
        status=dict(symbol='fixture:A',status='trading_halt',effective_date=SESSIONS[1],
                    available_at=SESSIONS[0]+'T17:00:00+09:00',ingested_at=SESSIONS[0]+'T17:00:00+09:00',evidence='exchange notice')
        self.assertFalse(run_backtest(market(),cfg(execution_statuses=(status,)))['fills'])

    def test_evidenced_unknown_position_stops(self):
        status=dict(symbol='fixture:A',status='value_unknown',effective_date=SESSIONS[2],
                    available_at=SESSIONS[0]+'T17:00:00+09:00',ingested_at=SESSIONS[0]+'T17:00:00+09:00')
        with self.assertRaisesRegex(ValueError,'valuation stopped'):
            run_backtest(market(),cfg(execution_statuses=(status,)))


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.c=cfg(volume_participation='.01')
        self.sha=freeze(self.root,self.c,study(),seed=7,mode='fixture')

    def tearDown(self):self.tmp.cleanup()

    def step(self,n):
        d=SESSIONS[n-1]
        return Ledger(self.root).step(d,raw(n),fixture_generated_at=d+'T18:01:00+09:00')

    def test_freeze_immutable(self):
        with self.assertRaises(FileExistsError):freeze(self.root,replace(self.c,commission_bps='20'),study(),seed=7,mode='fixture')
        self.assertEqual(Ledger(self.root).freeze_hash,self.sha)

    def test_manifest_tamper_detected(self):
        (self.root/'artifacts'/(self.sha+'.json')).write_text('{}')
        with self.assertRaises(ValueError):Ledger(self.root)

    def test_manifest_swap_detected(self):
        m=Ledger(self.root).manifest;m['seed']=42
        p=put(self.root/'artifacts',canonical(m));(self.root/'freeze.json').write_bytes(canonical(dict(sha256=p.stem)))
        with self.assertRaisesRegex(ValueError,'freeze changed'):Ledger(self.root)

    def test_append_only(self):
        self.step(1)
        with Ledger(self.root).connect() as db:
            for query in ('DELETE FROM decisions','UPDATE decisions SET hash="x"'):
                with self.assertRaises(sqlite3.IntegrityError):db.execute(query)

    def test_same_session_rejected(self):
        self.step(1)
        with self.assertRaisesRegex(ValueError,'next frozen session'):self.step(1)

    def test_restart_ledger_consistent(self):
        a=self.step(1);self.step(2)
        self.assertEqual(Ledger(self.root).read()[0],a)
        self.assertEqual(len(Ledger(self.root).read()),2)
        self.assertEqual(Ledger(self.root).verify()['verified_entries'],2)

    def test_observed_status_update_survives_restart(self):
        self.step(1)
        status=dict(symbol='fixture:A',status='trading_halt',effective_date=SESSIONS[1],
                    available_at=SESSIONS[1]+'T17:00:00+09:00',ingested_at=SESSIONS[1]+'T17:00:00+09:00',evidence='exchange notice')
        Ledger(self.root).step(SESSIONS[1],raw(2),reference_updates={'execution_statuses':[status]},
                              fixture_generated_at=SESSIONS[1]+'T18:01:00+09:00')
        e=self.step(3)
        self.assertFalse(e['simulated_fills'])
        self.assertEqual(Ledger(self.root).verify()['verified_entries'],3)

    def test_replay_matches_engine(self):
        self.step(1);e=self.step(2)
        r=run_backtest(market()[:2],replace(self.c,sessions=SESSIONS[:2]))
        self.assertEqual(e['portfolio'],r['snapshots'][-1]);self.assertEqual(e['simulated_fills'],r['fills'])

    def test_future_revision_preserves_old_prediction(self):
        a=self.step(1);self.step(2)
        correction=raw(1)[0];correction['revision_id']='revision'
        for k in ('available_at','ingested_at'):correction[k]=SESSIONS[2]+'T17:00:00+09:00'
        correction['payload_json']=canonical(dict(json.loads(correction['payload_json']),close=101,high=101)).decode()
        Ledger(self.root).step(SESSIONS[2],raw(3)+[correction],fixture_generated_at=SESSIONS[2]+'T18:01:00+09:00')
        self.assertEqual(Ledger(self.root).read()[0],a)

    def test_future_input_rejected(self):
        with self.assertRaisesRegex(ValueError,'unavailable'):Ledger(self.root).step(SESSIONS[0],raw(2),fixture_generated_at=SESSIONS[0]+'T18:01:00+09:00')

    def test_backdated_input_rejected(self):
        self.step(1);r=raw(1)[0];r['revision_id']='new'
        with self.assertRaisesRegex(ValueError,'backdates'):Ledger(self.root).step(SESSIONS[1],raw(2)+[r],fixture_generated_at=SESSIONS[1]+'T18:01:00+09:00')

    def test_removed_history_rejected(self):
        self.step(1)
        with self.assertRaisesRegex(ValueError,'removed'):Ledger(self.root).step(SESSIONS[1],raw(2)[1:],fixture_generated_at=SESSIONS[1]+'T18:01:00+09:00')

    def test_holdout_input_blocked(self):
        with self.assertRaises(PermissionError):Ledger(self.root).step(SESSIONS[0],[encode_observation(bar('2025-03-03'))])
        self.assertFalse((self.root/'holdout').exists());self.assertEqual(Ledger(self.root).read(),[])

    def test_short_report(self):
        self.step(1);self.step(2)
        report=Ledger(self.root).report()
        self.assertEqual(report['statistical_conclusion'],'insufficient_forward_history')
        self.assertIsNone(report['mean_return_ci']['lower'])
        self.assertIn('benchmark_relative_return',report)

    def test_real_paper_cannot_backdate_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'before its first'):freeze(tmp,self.c,study(),seed=7)

    def test_paper_cannot_override_generated_time(self):
        c=copy.deepcopy(self.c)
        c.market['data_provider']='jquants_v2'
        c.market['raw_data']=dict(provider='jquants_v2',schema_version=1,receipts=[])
        c.market['raw_data_hash']=digest(canonical(c.market['raw_data']))
        selection=dict(split='development',selected='equal_weight',candidates=[dict(strategy=s) for s in ('buy_and_hold','equal_weight','momentum','ml')])
        from ai_trading.models import timestamp
        with tempfile.TemporaryDirectory() as tmp,patch('ai_trading.forward.datetime') as clock:
            clock.now.return_value=timestamp('2024-12-01T00:00:00Z')
            freeze(tmp,c,study(),seed=7,selection=selection)
            with self.assertRaisesRegex(ValueError,'cannot be overridden'):
                Ledger(tmp).step(SESSIONS[0],{},fixture_generated_at=SESSIONS[0]+'T18:01:00+09:00')

    def test_atomic_failure_no_entry(self):
        with patch('ai_trading.forward.run_backtest',side_effect=ValueError('data gap')):
            with self.assertRaises(ValueError):self.step(1)
        self.assertEqual(Ledger(self.root).read(),[])

    def test_code_change_requires_new_series(self):
        with patch('ai_trading.forward.environment',return_value={'code_hash':'changed'}):
            with self.assertRaisesRegex(ValueError,'code changed'):self.step(1)

    def test_mutating_loaded_manifest_cannot_change_fee(self):
        ledger=Ledger(self.root);ledger.manifest['config']['commission_bps']='500'
        ledger.step(SESSIONS[0],raw(1),fixture_generated_at=SESSIONS[0]+'T18:01:00+09:00')
        e=self.step(2)
        self.assertEqual(e['costs'],0)

    def test_real_ml_revision_preserves_recorded_prediction(self):
        from ai_trading.market_fixture import fixture
        from ai_trading.ml import MLConfig
        rows,c,st=fixture()
        c=replace(c,strategy='ml',volume_participation='.01',ml=dict(c.ml,penalties=[.01],epochs=3,seed=7))
        first=MLConfig(**c.ml).first_prediction
        c=replace(c,sessions=c.sessions[:first+2]);c.market['raw_data']=[];c.market['raw_data_hash']=digest(canonical([]))
        with tempfile.TemporaryDirectory() as tmp:
            freeze(tmp,c,st,seed=7,mode='fixture')
            data=[encode_observation(r) for r in rows if json.loads(r.payload_json)['session_date']<=c.sessions[first]]
            a=Ledger(tmp).step(c.sessions[first],data,fixture_generated_at=c.sessions[first]+'T18:01:00+09:00')
            self.assertTrue(a['prediction'])
            data=[encode_observation(r) for r in rows if json.loads(r.payload_json)['session_date']<=c.sessions[-1]]
            revision=copy.deepcopy(next(r for r in data if json.loads(r['payload_json'])['session_date']==c.sessions[first-20]))
            revision['revision_id']='late'
            revision['payload_json']=canonical(dict(json.loads(revision['payload_json']),open=999,high=999,low=999,close=999)).decode()
            for k in ('available_at','ingested_at'):revision[k]=c.sessions[-1]+'T17:00:00+09:00'
            data.append(revision)
            Ledger(tmp).step(c.sessions[-1],data,fixture_generated_at=c.sessions[-1]+'T18:01:00+09:00')
            self.assertEqual(Ledger(tmp).read()[0],a)
