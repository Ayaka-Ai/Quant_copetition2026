import unittest
import datetime as dt
from dataclasses import replace
from decimal import Decimal as D
try:
    import numpy as np
except ImportError:
    np=None

from quant_signals.risk_model import estimate_beta,shrink_covariance,portfolio_variance,trailing_panel
from quant_data.public_research import PublicResearchObservation
from quant_backtest import TradingSession
from quant_portfolio import MomentumCalendar


@unittest.skipIf(np is None,'Risk estimator tests require NumPy; use the bundled runtime')
class RiskModelTests(unittest.TestCase):
    def test_beta_known_slope_and_intercept(self):
        spy=np.array([-.03,.02,.01,-.01,.04])
        assets=np.column_stack([2*spy+.001,-spy+.002,np.zeros(5)])
        np.testing.assert_allclose(estimate_beta(assets,spy),[2,-1,0],atol=1e-14)

    def test_beta_zero_variance_and_invalid_inputs(self):
        for assets,spy in [([[1],[2]],[1,1]),([[1],[float('nan')]],[1,2]),([[1]],[1,2])]:
            with self.assertRaises(ValueError): estimate_beta(assets,spy)

    def test_unbiased_covariance_and_fixed_diagonal_shrinkage(self):
        f,d,meta=shrink_covariance([[1,2],[2,0],[3,4]],.1)
        np.testing.assert_allclose(f@f.T+np.diag(d),[[1,.9],[.9,4]],atol=1e-14)
        self.assertEqual(meta['shrinkage'],.1)
        self.assertTrue(meta['strictly_positive_diagonal_loading'])

    def test_psd_even_with_more_assets_than_observations(self):
        x=np.array([[1,2,3,4],[2,4,5,8],[3,6,7,12]],dtype=float)
        f,d,meta=shrink_covariance(x,.1)
        self.assertGreater(np.linalg.eigvalsh(f@f.T+np.diag(d)).min(),0)
        self.assertGreater(meta['conservative_condition_number_upper_bound'],1)

    def test_zero_variance_assets_not_silently_repaired(self):
        f,d,meta=shrink_covariance([[1,1],[1,2],[1,3]],.1)
        self.assertEqual(meta['zero_sample_variance_assets'],1)
        self.assertIsNone(meta['conservative_condition_number_upper_bound'])
        self.assertEqual(d[0],0)
        self.assertAlmostEqual(portfolio_variance(f,d,[1,0]),0)

    def test_portfolio_variance_matches_dense_quadratic_form(self):
        f,d,_=shrink_covariance([[1,2],[2,0],[3,4]])
        w=np.array([.4,.4])
        self.assertAlmostEqual(portfolio_variance(f,d,w),float(w@(f@f.T+np.diag(d))@w))
        with self.assertRaises(ValueError): portfolio_variance(f,[-1,1],w)

    def test_invalid_covariance_inputs(self):
        for matrix in ([[1]],[[float('inf')],[2]],[]):
            with self.assertRaises(ValueError): shrink_covariance(matrix)
        with self.assertRaises(ValueError): shrink_covariance([[1],[2]],1.1)

    def fixture(self):
        sessions=[];lookup={};utc=dt.timezone.utc
        start=dt.date(2026,10,1)
        for i in range(8):
            day=start+dt.timedelta(days=i)
            close=dt.datetime.combine(day,dt.time(20),utc)
            s=TradingSession(day,close-dt.timedelta(hours=6),close,close+dt.timedelta(minutes=30))
            sessions.append(s)
            lookup['A',day]=PublicResearchObservation('A',day,s.close_available_time,
                close+dt.timedelta(days=30),D(100+i),D(100+i),D(20000000),True,True,'fixture')
        return MomentumCalendar(tuple(sessions),start,sessions[-1].session_date),lookup

    def test_trailing_exact_window_and_future_append_invariance(self):
        cal,lookup=self.fixture();day=cal.sessions[5].session_date;cutoff=cal.sessions[5].close_available_time
        panel,names,_=trailing_panel(cal,lookup,('A',),day,cutoff,3)
        np.testing.assert_allclose(panel[:,0],[103/102-1,104/103-1,105/104-1])
        short={k:o for k,o in lookup.items() if o.model_available_at<=cutoff}
        np.testing.assert_array_equal(panel,trailing_panel(cal,short,('A',),day,cutoff,3)[0])
        self.assertEqual(names,('A',))

    def test_missing_guarded_and_late_sessions_not_compressed(self):
        cal,lookup=self.fixture();day=cal.sessions[5].session_date;cutoff=cal.sessions[5].close_available_time
        key=('A',cal.sessions[3].session_date);original=lookup[key]
        for replacement in (None,replace(original,action_basis_clear=False),
                            replace(original,model_available_at=cutoff+dt.timedelta(seconds=1))):
            copy=dict(lookup)
            if replacement is None: del copy[key]
            else: copy[key]=replacement
            p,names,reasons=trailing_panel(cal,copy,('A',),day,cutoff,3)
            self.assertEqual(p.shape,(3,0));self.assertFalse(names);self.assertIn('A',reasons)


if __name__=='__main__': unittest.main()
