import datetime as dt
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from quant_backtest import PortfolioConfig, SessionBar, simulate_portfolio
from quant_backtest.research_metrics import performance_metrics
from quant_data.public_daily import PublicDailyBar, PublicDataset, public_data_readiness
from quant_data.public_research import (PublicResearchObservation, adapt_public_dataset,
                                       load_public_research_config)
from quant_portfolio import MomentumCalendar, MomentumRules, UniverseMember, load_momentum_config
from quant_portfolio.public_momentum import build_public_targets
from quant_data.us_calendar import exchange_sessions
from quant_backtest import TradingSession
from examples.run_public_momentum import verify_account

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    start, end = dt.date(2026, 8, 3), dt.date(2026, 10, 5)
    sessions = tuple(TradingSession(s.session_date, s.open_time, s.close_time,
                                   s.close_time+dt.timedelta(minutes=30)) for s in exchange_sessions(start, end))
    calendar = MomentumCalendar(sessions, start, end-dt.timedelta(days=1))
    members = tuple(UniverseMember(s, 'sector', s) for s in ('AAA', 'BBB', 'CCC'))
    retrieved = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
    observations = tuple(PublicResearchObservation(symbol, s.session_date, s.close_available_time,
        retrieved, D(100)+D(i)*slope, D(100), D(20000000), True, True, 'sha')
        for i, s in enumerate(sessions) for symbol, slope in
        [('AAA', D(2)), ('BBB', D(1)), ('CCC', D('-1')), ('SPY', D(0))])
    return calendar, members, observations


class PublicResearchTests(unittest.TestCase):
    def setUp(self):
        self.calendar, self.members, self.obs = fixture()

    def build(self, obs=None, **kwargs):
        return build_public_targets(self.calendar, self.obs if obs is None else obs, self.members,
                                    as_of=kwargs.pop('as_of', self.calendar.sessions[-1].close_available_time),
                                    **kwargs)

    def test_rank_weights_and_common_benchmark_dates(self):
        built = self.build()
        plans = built['targets']
        first = plans['MOM20_20_EW80'][0]
        self.assertEqual(first.buy_priority, ('AAA', 'BBB', 'CCC'))
        self.assertEqual(dict(first.weights), dict.fromkeys(('AAA', 'BBB', 'CCC'), D('.04')))
        self.assertEqual(len(built['decisions'][0]['candidates']), 0)
        dates = [t.signal_session for t in plans['MOM20_20_EW80']]
        self.assertTrue(all([t.signal_session for t in ts] == dates for ts in plans.values()))
        equal = plans['ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80'][0]
        self.assertEqual(sum(equal.weights.values()), D('.8'))
        self.assertEqual(dict(plans['SPY_80_CASH_20'][0].weights), {'SPY': D('.8')})
        # Negative momentum remains eligible, unlike an unannounced timing filter.
        self.assertIn('CCC', first.weights)

    def test_missing_session_not_compressed_and_late_row_not_used(self):
        day = self.calendar.sessions[15].session_date
        obs = tuple(o for o in self.obs if not (o.symbol == 'AAA' and o.session_date == day))
        first = self.build(obs)['targets']['MOM20_20_EW80'][0]
        self.assertNotIn('AAA', first.weights)
        obs = tuple(replace(o, model_available_at=o.actual_retrieved_at)
                    if o.symbol == 'AAA' else o for o in self.obs)
        self.assertTrue(all('AAA' not in t.weights for t in self.build(obs)['targets']['MOM20_20_EW80']))

    def test_future_observations_do_not_change_earlier_targets(self):
        cutoff = self.calendar.sessions[34].close_available_time
        short = tuple(o for o in self.obs if o.model_available_at <= cutoff)
        self.assertEqual(self.build(short, as_of=cutoff), self.build(as_of=cutoff))

    def test_all_missing_data_holds_instead_of_liquidating(self):
        built = self.build(())
        self.assertTrue(all(not ts for ts in built['targets'].values()))
        self.assertIn('data_not_ready', {d['status'] for d in built['decisions']})

    def test_missing_spy_skips_all_three_schedules(self):
        built = self.build(tuple(o for o in self.obs if o.symbol != 'SPY'))
        self.assertTrue(all(not ts for ts in built['targets'].values()))
        self.assertIn('benchmark_data_not_ready', {d['status'] for d in built['decisions']})

    def test_equal_benchmark_does_not_inherit_sector_selection_cap(self):
        rules = MomentumRules(maximum_positions=1, sector_cap=D('.04'))
        plans = self.build(rules=rules)['targets']
        self.assertEqual(set(plans['MOM20_20_EW80'][0].weights), {'AAA'})
        self.assertEqual(set(plans['ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80'][0].weights), {'AAA', 'BBB', 'CCC'})

    def test_duplicate_frozen_versions_rejected(self):
        with self.assertRaisesRegex(ValueError, 'one frozen version'):
            self.build(self.obs + (self.obs[0],))

    def test_adapter_keeps_actual_time_and_proxy_outside_strict_v1(self):
        s = self.calendar.sessions[0]
        source = PublicDailyBar('AAA', s.session_date, s.open_time,
            D(10), D(11), D(9), D(10), D(8), 200)
        data = PublicDataset('AAA', (source,), (), self.obs[0].actual_retrieved_at, 'sha')
        prepared = {'bars': (SessionBar('AAA', s.session_date, D(20), D(20)),), 'actions': ()}
        o = adapt_public_dataset(data, prepared, self.calendar.sessions)[0]
        self.assertEqual(o.reported_close_volume_proxy, D(2000))
        self.assertEqual(o.actual_share_close, D(20))
        self.assertEqual(o.model_available_at, s.close_available_time)
        self.assertEqual(o.actual_retrieved_at, data.retrieved_at)
        self.assertNotEqual(o.actual_retrieved_at, o.model_available_at)
        self.assertEqual(len(public_data_readiness().missing_checks), 5)
        prepared['bars'] = (SessionBar('AAA', s.session_date, None, None),)
        blocked = adapt_public_dataset(data, prepared, self.calendar.sessions)[0]
        self.assertIsNone(blocked.adjusted_close)
        self.assertFalse(blocked.action_basis_clear)

    def test_execution_revision_does_not_change_frozen_rules(self):
        cfg, _ = load_public_research_config(ROOT / 'configs/momentum_public_backtest_v1.json')
        self.assertTrue(cfg['historical_backtest_enabled'])
        with self.assertRaises(ValueError):
            load_momentum_config(ROOT / 'configs/momentum_public_backtest_v1.json')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'momentum_public_v1.json').write_bytes((ROOT / 'configs/momentum_public_v1.json').read_bytes())
            cfg['portfolio']['maximum_target_equity_weight'] = '.95'
            path = root / 'revision.json'
            path.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, 'changed frozen'):
                load_public_research_config(path)

    def test_integration_fill_timing_costs_and_metrics_window(self):
        built = self.build()
        plans = built['targets']['MOM20_20_EW80']
        bars = tuple(SessionBar(o.symbol, o.session_date, D(100), D(100))
                     for o in self.obs if o.symbol != 'SPY')
        cfg = PortfolioConfig()
        result = simulate_portfolio(self.calendar.sessions, bars, plans, cfg)
        self.assertEqual(verify_account(result, self.calendar.sessions, cfg.initial_cash)['status'], 'passed')
        i = next(i for i, s in enumerate(self.calendar.sessions) if s.session_date == plans[0].signal_session)
        start = self.calendar.sessions[i+1].session_date
        first = [t for t in result['trades'] if t['fill_session'] == start]
        expected = sum(t['commission']+t['slippage_cost'] for t in result['trades'])
        self.assertEqual(len(first), 3)
        self.assertAlmostEqual(float(result['summary']['final_equity']), float(cfg.initial_cash-expected))
        metrics = performance_metrics(result, start=start, end_exclusive=self.calendar.coverage_end+dt.timedelta(days=1), initial_cash=cfg.initial_cash)
        self.assertEqual(metrics['trading_sessions'], len(self.calendar.sessions)-i-1)
        self.assertAlmostEqual(metrics['cumulative_net_return'], float(-expected/cfg.initial_cash))
        # Missing valuations invalidate the result rather than removing losing days.
        result['daily'][i+1]['valuation_valid'] = False
        self.assertEqual(performance_metrics(result, start=start,
            end_exclusive=self.calendar.coverage_end+dt.timedelta(days=1), initial_cash=cfg.initial_cash)['status'], 'invalid_valuation')


if __name__ == '__main__':
    unittest.main()
