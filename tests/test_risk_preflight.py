import datetime as dt
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from quant_data.risk_inputs import load_risk_inputs,risk_snapshot
from quant_portfolio.risk_preflight import necessary_conditions,validate_covariance
from examples.run_risk_preflight import check_snapshot

ROOT=Path(__file__).resolve().parents[1]


class RiskPreflightTests(unittest.TestCase):
    def setUp(self):
        self.limits=json.loads((ROOT/'configs/risk_preflight_v1.json').read_text())['limits']

    def test_position_capacity_proof(self):
        for exposure,minimum in [('.95',24),('.85',22)]:
            r=necessary_conditions(exposure,self.limits)
            self.assertEqual(r['minimum_positions_by_security_cap'],minimum)
            self.assertEqual(r['status'],'proven_infeasible')
        self.assertEqual(necessary_conditions('.8',self.limits)['status'],'not_ready')

    def test_asset_lower_bound_conflict_and_mandatory_count(self):
        b={'A':D('.08'),**{'S%d'%i:D('.02') for i in range(46)}}
        r=necessary_conditions('.8',self.limits,benchmark=b)
        self.assertIn('security_bounds:A',r['conflicts'])
        self.assertIn('mandatory_holdings_exceed_position_count',r['conflicts'])

    def test_sector_and_aggregate_bound_conflicts(self):
        b={'S%d'%i:D('.02') for i in range(50)}
        sectors={s:'one_sector' for s in b}
        r=necessary_conditions('.6',self.limits,benchmark=b,sectors=sectors)
        self.assertIn('sector_bounds:one_sector',r['conflicts'])
        self.assertIn('sum_sector_lower_bounds_exceeds_exposure',r['conflicts'])

    def test_beta_envelope_requires_complete_pool(self):
        b={'S%d'%i:D('.02') for i in range(50)};sectors={s:s for s in b}
        r=necessary_conditions('.6',self.limits,benchmark=b,sectors=sectors,betas={s:D(1) for s in b})
        self.assertIn('beta_band_outside_relaxed_envelope',r['conflicts'])
        r=necessary_conditions('.6',self.limits,benchmark=b,sectors=sectors,betas={'S0':D(1)})
        self.assertNotIn('beta_band_outside_relaxed_envelope',r['conflicts'])
        self.assertIn('incomplete_betas_for_benchmark',r['missing_inputs'])

    def test_passing_bounds_is_not_a_feasibility_certificate(self):
        limits=dict(self.limits,security_cap='.2',active_security_cap='.03')
        b={'S%d'%i:D('.1') for i in range(10)}
        r=necessary_conditions('.8',limits,benchmark=b,sectors={s:s for s in b},betas={s:D('1.2') for s in b})
        self.assertEqual(r['status'],'requires_optimizer')
        self.assertFalse(r['feasible_solution_certified'])

    def test_covariance_psd_and_singular_matrices(self):
        self.assertEqual(validate_covariance([['.0001',0],[0,'.0002']])['status'],'PSD_within_tolerance')
        self.assertEqual(validate_covariance([[1,1],[1,1]])['dimension'],2)
        self.assertEqual(validate_covariance([[0,0],[0,0]])['dimension'],2)

    def test_bad_covariance_rejected_even_if_one_portfolio_variance_positive(self):
        for matrix in ([[1,2],[2,1]],[[0,1],[1,0]],[[1,0],[1,1]],[[1,2]]):
            with self.assertRaises(ValueError): validate_covariance(matrix)
        with self.assertRaises(ValueError): validate_covariance([['NaN']])

    def write_inputs(self,root,rows,kind='benchmark'):
        raw=json.dumps(rows).encode();(root/'data.json').write_bytes(raw)
        (root/'manifest.json').write_text(json.dumps(dict(schema_version='risk_inputs_v1',files=[
            dict(kind=kind,file='data.json',sha256=hashlib.sha256(raw).hexdigest())])))

    def test_availability_versions_and_no_previous_day_fill(self):
        rows=[dict(as_of='2026-11-02',available_at='2026-11-02T21:30:00+00:00',source='fixture',
                   units='NAV_fraction',payload={'A':'1'}),
              dict(as_of='2026-11-02',available_at='2026-11-03T21:30:00+00:00',source='fixture',
                   units='NAV_fraction',payload={'B':'1'})]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.write_inputs(root,rows);data=load_risk_inputs(root)
            cutoff=dt.datetime.fromisoformat('2026-11-02T22:00:00+00:00')
            self.assertEqual(risk_snapshot(data,dt.date(2026,11,2),cutoff)['benchmark']['payload'],{'A':'1'})
            self.assertIsNone(risk_snapshot(data,dt.date(2026,11,3),cutoff+dt.timedelta(days=1))['benchmark'])

    def test_hash_timezone_units_and_duplicate_versions_rejected(self):
        row=dict(as_of='2026-11-02',available_at='2026-11-02T21:30:00+00:00',source='fixture',units='NAV_fraction',payload={'A':'1'})
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.write_inputs(root,[row,row])
            with self.assertRaisesRegex(ValueError,'Ambiguous'): load_risk_inputs(root)
            self.write_inputs(root,[dict(row,available_at='2026-11-02T21:30:00')])
            with self.assertRaises(ValueError): load_risk_inputs(root)
            self.write_inputs(root,[dict(row,units='percent')])
            with self.assertRaises(ValueError): load_risk_inputs(root)
            self.write_inputs(root,[row]);(root/'data.json').write_text('[]')
            with self.assertRaisesRegex(ValueError,'fingerprint'): load_risk_inputs(root)

    def test_estimate_cannot_precede_input_close(self):
        row=dict(as_of='2026-11-02',available_at='2026-11-02T14:00:00+00:00',source='fixture',
                 units='dimensionless_SPY_beta',estimation_start='2026-08-01',estimation_end='2026-11-02',payload={'A':'1'})
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.write_inputs(root,[row],'betas')
            with self.assertRaisesRegex(ValueError,'predates'): load_risk_inputs(root)

    def test_missing_manifest_is_missing_data_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            inputs=load_risk_inputs(Path(d))
            self.assertEqual(inputs['status'],'missing_manifest')
            self.assertTrue(all(not rows for rows in inputs['records'].values()))

    def test_complete_snapshot_validation_and_covariance_universe(self):
        row=dict(estimation_start='2025-01-02',estimation_end='2026-10-30')
        snapshot={'benchmark':dict(payload={'A':'.5','B':'.5'}),
            'sectors':dict(payload={'A':'tech','B':'other'}),
            'betas':dict(row,payload={'A':'1','B':'1'}),
            'covariance':dict(row,payload=dict(symbols=['A','B'],matrix=[['.0001',0],[0,'.0001']]))}
        self.assertEqual(check_snapshot(snapshot,dt.date(2026,11,2),{'A','B'})[0],[])
        self.assertIn('incomplete_sectors',check_snapshot(snapshot,dt.date(2026,11,2),{'A','B','C'})[0])
        snapshot['covariance']['payload']['matrix']=[[1,2],[2,1]]
        self.assertTrue(any('PSD' in e for e in check_snapshot(snapshot,dt.date(2026,11,2),{'A','B'})[0]))

    def test_sector_validity_and_publication_cutoff(self):
        row=dict(as_of='2026-10-01',valid_through='2026-11-30',available_at='2026-11-03T21:30:00+00:00',
                 source='fixture',units='GICS_sector',payload={'A':'tech'})
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.write_inputs(root,[row],'sectors');data=load_risk_inputs(root)
            cutoff=dt.datetime.fromisoformat('2026-11-02T22:00:00+00:00')
            self.assertIsNone(risk_snapshot(data,dt.date(2026,11,2),cutoff)['sectors'])
            self.assertIsNotNone(risk_snapshot(data,dt.date(2026,11,3),cutoff+dt.timedelta(days=1))['sectors'])


if __name__=='__main__': unittest.main()
