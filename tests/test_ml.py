"""Offline leakage, temporal isolation and execution integration tests."""
import json
import statistics
import tempfile
import unittest
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from ai_trading.backtest import run_backtest
from ai_trading.experiment import ExperimentStore, initialize
from ai_trading.ml import MLConfig, features, fit, labels, prediction_metrics, walk_forward
from ai_trading.ml_fixture import fixture
from ai_trading.models import timestamp
from ai_trading.storage import canonical, digest


class MLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows,cls.config,cls.study=fixture(155)
        cls.cfg=MLConfig(**dict(cls.config.ml,epochs=20))
        cls.config=replace(cls.config,ml=asdict(cls.cfg))
        cls.result=walk_forward(cls.rows,cls.config,cls.cfg)

    def test_feature_formula_and_lineage(self):
        f=self.result['features'][0]
        prices=[json.loads(r.payload_json)['close'] for r in self.rows if r.entity_id==f['symbol']][:61]
        self.assertAlmostEqual(f['values']['return_60'],prices[-1]/prices[0]-1)
        self.assertAlmostEqual(f['values']['ma_ratio_20'],prices[-1]/statistics.mean(prices[-20:])-1)
        for feature in self.result['features']:
            for lineage in feature['lineage'].values():
                self.assertLess(timestamp(lineage['available_at']),timestamp(feature['decision_at']))
                self.assertTrue(all(s['session']<=feature['session'] for s in lineage['sources']))

    def changed_future(self):
        cutoff=self.config.sessions[140]
        output=[]
        for r in self.rows:
            d=json.loads(r.payload_json)
            if d['session_date']>=cutoff:
                for k in ('open','high','low','close'):
                    d[k]*=3
                r=replace(r,payload_json=canonical(d).decode())
            output.append(r)
        return output,cutoff

    def test_future_price_does_not_change_past_features(self):
        rows,cutoff=self.changed_future()
        fs,_=features(rows,self.config,True)
        self.assertEqual([f for f in fs if f['session']<cutoff],
                         [f for f in self.result['features'] if f['session']<cutoff])

    def test_future_prices_do_not_change_past_predictions(self):
        rows,cutoff=self.changed_future()
        r=walk_forward(rows,self.config,self.cfg)
        self.assertEqual([p for p in r['predictions'] if p['session']<cutoff],
                         [p for p in self.result['predictions'] if p['session']<cutoff])

    def test_future_rows_and_sessions_do_not_change_past_predictions(self):
        rows,config,_=fixture(165)
        r=walk_forward(rows,replace(config,ml=asdict(self.cfg)),self.cfg)
        self.assertEqual([p for p in r['predictions'] if p['session']<=self.config.sessions[-1]],self.result['predictions'])

    def test_later_revision_does_not_rewrite_features(self):
        later=timestamp(self.config.sessions[140]+'T17:30:00+09:00')
        original=self.rows[0];d=json.loads(original.payload_json)
        for k in ('open','high','low','close'): d[k]*=2
        revision=replace(original,revision_id='later',available_at=later,ingested_at=later,payload_json=canonical(d).decode())
        fs,_=features(self.rows+[revision],self.config,True)
        self.assertEqual([f for f in fs if timestamp(f['decision_at'])<later],
                         [f for f in self.result['features'] if timestamp(f['decision_at'])<later])
        r=walk_forward(self.rows+[revision],self.config,self.cfg)
        self.assertEqual([p for p in r['predictions'] if p['session']<self.config.sessions[140]],
                         [p for p in self.result['predictions'] if p['session']<self.config.sessions[140]])

    def test_scaler_train_only(self):
        for fold in self.result['folds']:
            keys=fold['train_keys'];model=fold['model']
            train=[f for f in self.result['features'] if [f['session'],f['symbol']] in keys]
            self.assertEqual(model['mean'],[statistics.mean(f['values'][k] for f in train) for k in model['names']])
            self.assertEqual(model['scale'],[statistics.pstdev(f['values'][k] for f in train) or 1 for k in model['names']])
            self.assertTrue(set(map(tuple,keys)).isdisjoint(map(tuple,fold['validation_keys'])))

    def test_validation_perturbation_leaves_candidate_fit_unchanged(self):
        fold=self.result['folds'][0];rows=[]
        for r in self.rows:
            d=json.loads(r.payload_json)
            if fold['validation_period'][0]<=d['session_date']<=fold['validation_period'][1]:
                for k in ('open','high','low','close'): d[k]*=5
                r=replace(r,payload_json=canonical(d).decode())
            rows.append(r)
        r=walk_forward(rows,self.config,self.cfg)
        for k in ('mean','scale'):
            self.assertEqual(fold['model'][k],r['folds'][0]['model'][k])

    def test_label_separation_and_horizon(self):
        ys=labels(self.rows,self.config,self.cfg.horizon)
        for f in self.result['features']:
            self.assertEqual(set(f['values']),set(self.result['feature_definition']))
            self.assertNotIn('y',f)
        key=(self.config.sessions[60],'fixture:A')
        prices=[json.loads(r.payload_json)['close'] for r in self.rows if r.entity_id==key[1]]
        self.assertEqual(ys[key]['y'],int(prices[65]>prices[60]))
        self.assertEqual(ys[key]['end'],self.config.sessions[65])

    def test_purged_order_no_shuffle(self):
        for fold in self.result['folds']:
            self.assertEqual(fold['train_keys'],sorted(fold['train_keys']))
            self.assertEqual(fold['validation_keys'],sorted(fold['validation_keys']))
            self.assertLess(fold['train_period'][1],fold['validation_period'][0])
            self.assertLess(timestamp(fold['train_label_max_available_at']),timestamp(fold['validation_period'][0]+'T18:00:00+09:00'))
            self.assertLess(timestamp(fold['validation_label_max_available_at']),timestamp(fold['prediction_period'][0]+'T18:00:00+09:00'))

    def test_reproducible_seed_input_order(self):
        self.assertEqual(self.result,walk_forward(list(reversed(self.rows)),self.config,self.cfg))

    def test_artifact_hash(self):
        for f in self.result['folds']:
            self.assertEqual(f['model_artifact_hash'],digest(canonical(f['model'])))

    def test_metrics_known_and_degenerate(self):
        m=prediction_metrics([(.1,0),(.9,1)])
        self.assertEqual(m['accuracy'],1);self.assertEqual(m['roc_auc'],1)
        self.assertAlmostEqual(m['brier_score'],.01)
        self.assertEqual(prediction_metrics([(.5,0),(.5,1)])['roc_auc'],.5)
        self.assertIsNone(prediction_metrics([(.1,0)])['recall'])
        self.assertEqual(prediction_metrics([]),{'count':0})

    def test_optional_volume(self):
        fs,names=features(self.rows,self.config)
        self.assertNotIn('volume_change_5',names)
        self.assertEqual(len(fs[0]['values']),5)

    def test_missing_prices_skip_features(self):
        rows=[r for r in self.rows if r!=self.rows[120]]
        fs,_=features(rows,self.config,True)
        self.assertFalse(any(f['session']==self.config.sessions[60] and f['symbol']=='fixture:A' for f in fs))

    def test_insufficient_history_rejected(self):
        with self.assertRaisesRegex(ValueError,'insufficient'):
            walk_forward(self.rows,replace(self.config,sessions=self.config.sessions[:100]),self.cfg)

    def test_unknown_features_and_shuffle_rejected(self):
        for config in ({'features':['future_close']},{'shuffle':True},{'fit':'full_period'},{'epochs':0}):
            with self.assertRaises((TypeError,ValueError)): MLConfig(**config)
        with self.assertRaises(ValueError): replace(self.config,sessions=list(reversed(self.config.sessions)))

    def test_execution_constraints(self):
        r=run_backtest(self.rows,self.config)
        self.assertTrue(r['fills'])
        self.assertTrue(all(Decimal(s['cash'])>=0 for s in r['snapshots']))
        for f in r['fills']:
            self.assertLess(r['orders'][f['order_id']-1]['decision_session'],f['session'])
            self.assertGreater(Decimal(f['commission']),0)
            self.assertGreater(Decimal(f['slippage_cost']),0)
        self.assertEqual(r['snapshots'][0]['session'],self.config.sessions[self.cfg.first_prediction])

    def test_experiment_comparison_and_replay_offline(self):
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('network')):
            initialize(tmp,self.study);store=ExperimentStore(tmp)
            models=[store.run(self.rows,replace(self.config,strategy=s),random_seed=7)
                    for s in ('buy_and_hold','equal_weight','momentum','ml')]
            self.assertTrue(all(m.status=='completed' for m in models))
            self.assertEqual(len(store.compare([m.experiment_id for m in models])),4)
            m=models[-1];self.assertEqual(store.verify(m.experiment_id),asdict(m))
            a=store.read(m.experiment_id)
            self.assertEqual(a['outcome']['result']['ml_research'],self.result)
            self.assertEqual(a['experiment']['strategy_parameters']['ml']['seed'],7)

    def test_holdout_model_selection_forbidden_even_with_access_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            initialize(tmp,self.study);store=ExperimentStore(tmp)
            for flag in (False,True):
                with self.assertRaises(PermissionError): store.run(self.rows,self.config,'holdout',7,holdout=flag)
            self.assertEqual(store.list(),[])

    def test_seed_mismatch_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            initialize(tmp,self.study)
            with self.assertRaisesRegex(ValueError,'seed'): ExperimentStore(tmp).run(self.rows,self.config)
