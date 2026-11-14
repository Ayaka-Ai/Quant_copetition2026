import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal as D
try:
    import numpy as np
except ImportError:
    np=None

from quant_data.compact_risk import CompactRiskStore
from quant_portfolio.weight_optimizer import optimize_weights,project_weights,risk_diagnostics
from quant_portfolio.risk_weight_strategy import RiskWeightStrategy
from quant_backtest import TargetWeights
from quant_backtest.portfolio import AccountSnapshot


@unittest.skipIf(np is None,'Requires NumPy; use bundled runtime')
class WeightOptimizerTests(unittest.TestCase):
    def test_current_80pct_20_stock_constraints_have_unique_solution(self):
        result=optimize_weights(np.eye(20)*.001,['sector%d'%(i//5) for i in range(20)])
        self.assertEqual(result['status'],'unique_capacity_solution')
        np.testing.assert_allclose(result['weights'],np.full(20,.04),atol=1e-12)
        self.assertEqual(result['iterations'],0)

    def test_general_interface_matches_closed_form_minimum_variance(self):
        result=optimize_weights(np.diag([1.,4.]),['a','b'],exposure=.8,security_cap=.8,
            sector_cap=1.,anchor_penalty=0,tolerance=1e-12)
        np.testing.assert_allclose(result['weights'],[.64,.16],atol=1e-8)
        self.assertLess(result['objective'],result['initial_objective'])
        self.assertLess(result['max_constraint_violation'],1e-10)

    def test_binding_sector_constraint(self):
        result=optimize_weights(np.diag([1.,2.,2.]),['a','b','c'],exposure=.8,
            security_cap=.8,sector_cap=.3,anchor_penalty=0)
        np.testing.assert_allclose(result['weights'],[.3,.25,.25],atol=1e-7)

    def test_infeasible_and_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):optimize_weights(np.eye(19),['a']*19)
        with self.assertRaises(ValueError):optimize_weights(np.eye(21),[str(i) for i in range(21)])
        with self.assertRaises(ValueError):optimize_weights(np.array([[1,2],[2,1]]),['a','b'])
        with self.assertRaises(ValueError):optimize_weights(np.array([[1,0],[1,1]]),['a','b'])
        with self.assertRaises(ValueError):project_weights([],0,.04,[],.2)

    def test_nonconvergence_returns_no_silent_solution(self):
        with self.assertRaisesRegex(ValueError,'converge'):
            optimize_weights(np.diag([1.,4.]),['a','b'],exposure=.8,security_cap=.8,
                             sector_cap=1.,anchor_penalty=0,max_iterations=1,tolerance=1e-15)

    def test_projection_preserves_feasibility_and_is_idempotent(self):
        x=np.array([.4,.4,0.,0.]);sectors=['a','a','b','b']
        p=project_weights(x,.6,.3,sectors,.4)
        self.assertAlmostEqual(float(p.sum()),.6)
        self.assertLessEqual(float(p[:2].sum()),.4+1e-12)
        np.testing.assert_allclose(project_weights(p,.6,.3,sectors,.4),p,atol=1e-12)

    def test_risk_contributions_and_beta_use_NAV_weights(self):
        f=np.eye(2)*.01;d=np.array([.0001,.0002]);w={'A':D('.4'),'B':D('.4')}
        r=risk_diagnostics(w,('A','B'),f,d,{'A':1,'B':1})
        self.assertAlmostEqual(r['estimated_NAV_beta'],.8)
        self.assertAlmostEqual(sum(r['annual_variance_contributions'].values()),r['annual_variance'])
        self.assertIsNone(r['tracking_error'])
        self.assertIsNone(risk_diagnostics(w,('A','B'),f,d,{'A':1})['estimated_NAV_beta'])
        self.assertEqual(risk_diagnostics({'C':.1},('A','B'),f,d,{})['status'],'incomplete_covariance')

    def store_fixture(self,root):
        name='covariance.npz';np.savez(root/name,factors=np.eye(2)*.01,diagonal=np.array([.0001,.0001]),symbols=np.array(['A','B']))
        sha=hashlib.sha256((root/name).read_bytes()).hexdigest()
        metadata=dict(as_of='2026-11-02',estimation_end='2026-11-02',available_at='2026-11-09T22:00:00+00:00',
                      model_available_at='2026-11-02T21:30:00+00:00')
        (root/'summary.json').write_text(json.dumps(dict(covariance_sha256={name:sha})))
        (root/'covariance_index.json').write_text(json.dumps([dict(metadata,units='daily_return_covariance',file=name,sha256=sha)]))
        (root/'betas.json').write_text(json.dumps([dict(metadata,units='dimensionless_SPY_beta',payload={'A':'1','B':'1'})]))
        return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in ('summary.json','betas.json','covariance_index.json')}

    def test_strict_clock_rejects_posthoc_model_and_arrays_readonly(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);hashes=self.store_fixture(root);store=CompactRiskStore(root,hashes)
            day=dt.date(2026,11,2);cutoff=dt.datetime.fromisoformat('2026-11-02T21:30:00+00:00')
            self.assertIsNone(store.snapshot(day,cutoff,'strict'))
            research=store.snapshot(day,cutoff,'exploratory_model_time')
            self.assertEqual(research['symbols'],('A','B'))
            with self.assertRaises(ValueError):research['factors'][0,0]=9
            self.assertIsNone(store.snapshot(day,cutoff-dt.timedelta(seconds=1),'exploratory_model_time'))

    def test_metadata_and_cached_matrix_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);hashes=self.store_fixture(root)
            store=CompactRiskStore(root,hashes);day=dt.date(2026,11,2)
            cutoff=dt.datetime.fromisoformat('2026-11-02T21:30:00+00:00')
            store.snapshot(day,cutoff,'exploratory_model_time')
            (root/'covariance.npz').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'fingerprint'):store.snapshot(day,cutoff,'exploratory_model_time')
            (root/'betas.json').write_text('[]')
            with self.assertRaisesRegex(ValueError,'metadata'):CompactRiskStore(root,hashes)

    def overlay_fixture(self):
        day=dt.date(2026,11,2);cutoff=dt.datetime.fromisoformat('2026-11-02T21:30:00+00:00')
        class Selector:
            decisions={day:{}}
            def __call__(self,state):return TargetWeights(day,cutoff,{'A':D('.3'),'B':D('.3')})
        class Store:
            def snapshot(self,*args):return dict(symbols=('A','B'),factors=np.diag([1.,2.]),
                diagonal=np.zeros(2),betas={'A':1,'B':1},mode='exploratory_model_time')
        config=dict(risk_mode='exploratory_model_time',security_cap='.8',sector_cap='1',
                    maximum_positions=20,anchor_penalty='0',tolerance='1e-12',max_iterations=10000)
        state=AccountSnapshot(day,cutoff,D(1000),D(1000),{}, {},D(0),True)
        return Selector(),Store(),config,state

    def test_overlay_nonunique_solution_produces_checked_decimal_targets(self):
        selector,store,config,state=self.overlay_fixture()
        overlay=RiskWeightStrategy(selector,store,{'A':'a','B':'b'},config,optimize=True)
        target=overlay(state)
        self.assertAlmostEqual(float(target.weights['A']),.48,places=8)
        self.assertAlmostEqual(float(target.weights['B']),.12,places=8)
        self.assertLessEqual(sum(target.weights.values()),D('.6'))
        self.assertGreaterEqual(sum(target.weights.values()),D('.599999999'))

    def test_overlay_failure_and_unknown_actual_holding_produce_no_target(self):
        selector,store,config,state=self.overlay_fixture()
        config['max_iterations']=1
        overlay=RiskWeightStrategy(selector,store,{'A':'a','B':'b'},config,optimize=True)
        self.assertIsNone(overlay(state))
        self.assertEqual(overlay.audit[-1]['status'],'optimization_failed_no_target')
        state=AccountSnapshot(state.session_date,state.available_at,D(900),D(1000),{'C':1},{'C':D('.1')},D(0),True)
        self.assertIsNone(overlay(state))
        self.assertEqual(overlay.audit[-1]['missing_symbols'],['C'])


if __name__=='__main__':unittest.main()
