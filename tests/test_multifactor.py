import datetime as dt
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from quant_backtest import TradingSession
from quant_data.public_daily import PublicDailyBar, PublicDataset
from quant_data.public_research import PublicResearchObservation, adapt_public_dataset
from quant_backtest import SessionBar
from quant_data.us_calendar import exchange_sessions
from quant_portfolio import MomentumCalendar, MomentumRules, UniverseMember
from quant_portfolio.public_momentum import build_public_targets
from quant_signals.multifactor import (TraditionalFactorScorer, load_factor_config, raw_features,
                                      sector_zscores, percentile_ranks)
from quant_signals.scores import SignalScore

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    start, end = dt.date(2026, 6, 1), dt.date(2026, 10, 5)
    sessions = tuple(TradingSession(s.session_date, s.open_time, s.close_time,
        s.close_time+dt.timedelta(minutes=30)) for s in exchange_sessions(start, end))
    calendar = MomentumCalendar(sessions, start, end-dt.timedelta(days=1))
    members = tuple(UniverseMember(s, 'sector', s) for s in ('AAA', 'BBB', 'CCC'))
    retrieved = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
    obs = []
    for i, session in enumerate(sessions):
        for symbol, slope in [('AAA', D(2)), ('BBB', D(1)), ('CCC', D(0)), ('SPY', D(0))]:
            close = D(100)+i*slope
            obs.append(PublicResearchObservation(symbol, session.session_date, session.close_available_time,
                retrieved, close, close, D(20000000), True, True, 'sha',
                D(100)+max(0, i-1)*slope, close+1, close-1))
    return calendar, members, tuple(obs)


class MultifactorTests(unittest.TestCase):
    def setUp(self):
        self.calendar, self.members, self.obs = fixture()
        self.config = load_factor_config(ROOT / 'configs/multifactor_public_v1.json')

    def history(self, symbol='BBB'):
        return [o for o in self.obs if o.symbol == symbol][:61]

    def build(self, **kwargs):
        return build_public_targets(self.calendar, self.obs, self.members,
                                    as_of=self.calendar.sessions[-1].close_available_time, **kwargs)

    def test_raw_factor_windows_and_orientations(self):
        raw, reasons = raw_features(self.history())
        self.assertFalse(reasons)
        self.assertEqual(raw['mom5'], D(160)/155-1)
        self.assertEqual(raw['mom20'], D(160)/140-1)
        self.assertEqual(raw['mom60'], D('.6'))
        self.assertEqual(raw['ma20_deviation'], D(160)/D('150.5')-1)
        self.assertEqual(raw['ma60_deviation'], D(160)/D('130.5')-1)
        self.assertEqual(raw['reversal1'], 1-D(160)/159)
        self.assertEqual(raw['reversal3'], 1-D(160)/157)
        self.assertEqual(raw['close_position'], D('.5'))
        self.assertEqual(raw['volume_confirmation'], raw['mom20'])
        self.assertEqual(raw['neg_gap5'], 0)
        self.assertEqual(raw['neg_downside20'], 0)
        self.assertEqual(raw['neg_liquidity_cv20'], 0)
        self.assertLess(raw['neg_vol20'], 0)

    def test_constant_prices_and_zero_range_are_neutral_and_finite(self):
        history = [replace(o, adjusted_high=o.adjusted_close, adjusted_low=o.adjusted_close)
                   for o in self.history('CCC')]
        raw, _ = raw_features(history)
        self.assertEqual(raw['close_position'], D('.5'))
        self.assertEqual(raw['neg_vol20'], 0)
        self.assertTrue(all(v.is_finite() for v in raw.values()))

    def test_missing_session_or_guard_never_compresses_window(self):
        h = self.history()
        for bad in (h[:-1], h[:20]+[None]+h[21:],
                    h[:20]+[replace(h[20], action_basis_clear=False)]+h[21:]):
            raw, reasons = raw_features(bad)
            self.assertIsNone(raw)
            self.assertIn('missing_or_guarded_61_session_history', reasons)
        h[-2] = replace(h[-2], adjusted_open=None)
        self.assertIn('missing_adjusted_open_history', raw_features(h)[1])

    def test_feature_prices_do_not_use_actual_share_accounting_units(self):
        s = self.calendar.sessions[0]
        source = PublicDailyBar('AAA', s.session_date, s.open_time, D(9), D(11), D(8), D(10), D(5), 200)
        data = PublicDataset('AAA', (source,), (), self.obs[0].actual_retrieved_at, 'sha')
        prepared = dict(bars=(SessionBar('AAA', s.session_date, D(18), D(20)),), actions=())
        o = adapt_public_dataset(data, prepared, self.calendar.sessions)[0]
        self.assertEqual((o.adjusted_open, o.adjusted_high, o.adjusted_low), (D('4.5'), D('5.5'), D(4)))
        self.assertEqual(o.actual_share_close, D(20))

    def test_mad_clipping_and_population_standardization(self):
        z, audit = sector_zscores(dict(zip('abcde', map(D, (0, 1, 2, 3, 100)))))
        self.assertEqual(audit['median'], 2)
        self.assertEqual(audit['mad'], 1)
        self.assertEqual(audit['clipped_mean'], D('2.2'))
        self.assertLess(abs(sum(z.values())), D('1e-25'))
        self.assertLess(abs(sum(v*v for v in z.values())/5-1), D('1e-25'))
        self.assertEqual(sector_zscores({'a': D(100)})[0], {'a': D(0)})
        self.assertEqual(sector_zscores({'a': D(1), 'b': D(1)})[0], {'a': D(0), 'b': D(0)})

    def test_percentile_ties_and_singleton(self):
        self.assertEqual(percentile_ranks({'a': D(1), 'b': D(1), 'c': D(3)}),
                         {'a': D('.25'), 'b': D('.25'), 'c': D(1)})
        self.assertEqual(percentile_ranks({'a': D(7)}), {'a': D('.5')})
        self.assertEqual(percentile_ranks({'a': D(7), 'b': D(7)}), {'a': D('.5'), 'b': D('.5')})

    def test_generic_score_changes_selection_without_changing_risk_rules(self):
        def inverted(day, cutoff, candidates):
            return {c['symbol']: SignalScore(c['symbol'], day, cutoff, -c['momentum']) for c in candidates}
        built = self.build(score_provider=inverted, strategy_id='INVERTED',
                           rules=MomentumRules(maximum_positions=1))
        self.assertEqual(set(built['targets']['INVERTED'][0].weights), {'CCC'})
        self.assertEqual(sum(built['targets']['INVERTED'][0].weights.values()), D('.04'))

    def test_future_scores_and_incomplete_provider_are_rejected(self):
        def future(day, cutoff, candidates):
            return {c['symbol']: SignalScore(c['symbol'], day, cutoff+dt.timedelta(seconds=1), D(1)) for c in candidates}
        with self.assertRaisesRegex(ValueError, 'availability mismatch'):
            self.build(score_provider=future)
        with self.assertRaisesRegex(ValueError, 'every eligible'):
            self.build(score_provider=lambda *args: {})

    def test_unavailable_scores_do_not_liquidate(self):
        def missing(day, cutoff, candidates):
            return {c['symbol']: SignalScore(c['symbol'], day, cutoff, None, reasons=('missing_factor',)) for c in candidates}
        built = self.build(score_provider=missing)
        self.assertTrue(all(not ts for ts in built['targets'].values()))

    def test_common_factor_pool_and_schedule_for_momentum_control(self):
        scorer = TraditionalFactorScorer(self.calendar, self.obs, self.config)
        multi = self.build(score_provider=scorer.provider(), strategy_id='FACTORS', minimum_signal_sessions=61)
        control = self.build(score_provider=scorer.provider('momentum20'), strategy_id='CONTROL', minimum_signal_sessions=61)
        self.assertEqual(multi['targets']['SPY_80_CASH_20'], control['targets']['SPY_80_CASH_20'])
        self.assertEqual(multi['targets']['ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80'],
                         control['targets']['ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80'])
        self.assertEqual([t.signal_session for t in multi['targets']['FACTORS']],
                         [t.signal_session for t in control['targets']['CONTROL']])
        for decision in multi['decisions']:
            for c in decision['candidates']:
                if c['eligible']:
                    self.assertTrue(D(0) <= c['score'] <= D(1))

    def test_future_observations_do_not_change_factor_scores(self):
        day = self.calendar.sessions[64].session_date
        cutoff = self.calendar.sessions[64].close_available_time
        candidates = [dict(symbol=m.symbol, sector=m.sector) for m in self.members]
        full = TraditionalFactorScorer(self.calendar, self.obs, self.config)
        short = TraditionalFactorScorer(self.calendar, [o for o in self.obs if o.model_available_at <= cutoff], self.config)
        self.assertEqual(full.provider()(day, cutoff, candidates), short.provider()(day, cutoff, candidates))
        late = [replace(o, model_available_at=o.actual_retrieved_at) if o.symbol == 'BBB' and o.session_date == day else o for o in self.obs]
        scorer = TraditionalFactorScorer(self.calendar, late, self.config)
        self.assertIsNone(scorer.provider()(day, cutoff, candidates)['BBB'].value)

    def test_invalid_numeric_scores_rejected(self):
        with self.assertRaises(ValueError):
            SignalScore('A', self.calendar.sessions[0].session_date, self.calendar.sessions[0].close_available_time, D('NaN'))
        with self.assertRaises(ValueError):
            SignalScore('A', self.calendar.sessions[0].session_date, self.calendar.sessions[0].close_available_time, None)

    def test_unsupported_normalization_is_not_silently_ignored(self):
        self.config['normalization']['group'] = 'all_stocks'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(self.config))
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                load_factor_config(path)


if __name__ == '__main__':
    unittest.main()
