import datetime as dt
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from quant_backtest import PortfolioConfig, SessionBar, TargetWeights, TradingSession, simulate_portfolio
from quant_portfolio import (DataReadiness, MomentumCalendar, MomentumRules, UniverseMember,
                             build_momentum_targets, load_momentum_config)
from quant_signals import MomentumObservation, momentum_snapshot

UTC = dt.timezone.utc
ROOT = Path(__file__).resolve().parents[1]


def calendar(weeks=8, start=dt.date(2026, 8, 3), holidays=(), early_closes=()):
    end = start + dt.timedelta(days=weeks*7-1)
    result = []
    for i in range(weeks*7):
        day = start + dt.timedelta(days=i)
        if day.weekday() >= 5 or day in holidays:
            continue
        close = dt.datetime.combine(day, dt.time(17 if day in early_closes else 20), UTC)
        result.append(TradingSession(day, dt.datetime.combine(day, dt.time(13, 30), UTC),
                                     close, close+dt.timedelta(minutes=5)))
    return MomentumCalendar(tuple(result), start, end)


def members(count=3):
    return tuple(UniverseMember('S%02d' % i, 'Sector%d' % (i//5), '%010d' % (i+1)) for i in range(count))


def observations(cal, universe):
    return tuple(MomentumObservation(member.symbol, session.session_date, session.close_available_time,
                                      D(100)+D(i)*D(len(universe)-j), D(100), D(20000000), True, True, True)
                 for i, session in enumerate(cal.sessions) for j, member in enumerate(universe))


def ready():
    return DataReadiness(True, True, True, True, True)


def end_time(cal):
    return cal.sessions[-1].close_time+dt.timedelta(minutes=30)


def run(cal, universe, obs=None, **kwargs):
    return build_momentum_targets(cal, observations(cal, universe) if obs is None else obs,
                                  universe, kwargs.pop('readiness', ready()),
                                  as_of=kwargs.pop('as_of', end_time(cal)), **kwargs)


class MomentumMeasurementTests(unittest.TestCase):
    def test_twenty_returns_require_twenty_one_closes(self):
        cal, universe = calendar(), members(1)
        obs = observations(cal, universe)
        dates = [s.session_date for s in cal.sessions[:21]]
        stats = momentum_snapshot(obs, dates, cal.sessions[20].close_available_time, ['S00'])
        self.assertEqual(stats[0].momentum, D('.2'))
        self.assertIn('turnover_warmup', stats[0].data_reasons)
        short = momentum_snapshot(obs, dates[:-1], cal.sessions[19].close_available_time, ['S00'])
        self.assertIsNone(short[0].momentum)

    def test_missing_date_is_not_compressed_or_forward_filled(self):
        cal, universe = calendar(), members(1)
        obs = observations(cal, universe)
        missing_day = cal.sessions[15].session_date
        cut = [o for o in obs if o.session_date != missing_day]
        stats = momentum_snapshot(cut, [s.session_date for s in cal.sessions[:30]],
                                  cal.sessions[29].close_available_time, ['S00'])[0]
        self.assertIsNone(stats.momentum)
        self.assertIsNone(stats.average_turnover_usd)
        self.assertIn('missing_adjusted_close_history', stats.data_reasons)

    def test_future_revision_is_only_used_after_publication(self):
        cal, universe = calendar(), members(1)
        obs = observations(cal, universe)
        dates = [s.session_date for s in cal.sessions[:30]]
        cutoff = cal.sessions[29].close_available_time
        base = next(o for o in obs if o.session_date == dates[-21])
        revision = replace(base, adjusted_close=D(50), available_at=cutoff+dt.timedelta(hours=1))
        before = momentum_snapshot(obs+(revision,), dates, cutoff, ['S00'])[0]
        self.assertEqual(before.momentum, D(129)/D(109)-1)
        after = momentum_snapshot(obs+(revision,), dates, revision.available_at, ['S00'])[0]
        self.assertEqual(after.momentum, D(129)/D(50)-1)

    def test_duplicate_versions_and_bad_numeric_inputs_are_rejected(self):
        cal, universe = calendar(), members(1)
        obs = observations(cal, universe)
        with self.assertRaises(ValueError):
            momentum_snapshot(obs+(obs[0],), [s.session_date for s in cal.sessions], end_time(cal), ['S00'])
        for kwargs in [dict(adjusted_close=100.0), dict(turnover_usd=D(-1)),
                       dict(adjusted_close=D('NaN')), dict(permission='yes')]:
            with self.assertRaises(ValueError):
                replace(obs[0], **kwargs)

    def test_signal_uses_adjusted_closes_instead_of_raw_split_prices(self):
        cal, universe = calendar(), members(1)
        obs = tuple(replace(o, adjusted_close=D(100),
                            unadjusted_close=D(200) if i < 15 else D(100))
                    for i, o in enumerate(observations(cal, universe)))
        stats = momentum_snapshot(obs, [s.session_date for s in cal.sessions[:30]],
                                  cal.sessions[29].close_available_time, ['S00'])[0]
        self.assertEqual(stats.momentum, D(0))


class WeeklyScheduleTests(unittest.TestCase):
    def test_friday_holiday_uses_thursday(self):
        cal = calendar(1, dt.date(2026, 6, 29), holidays=(dt.date(2026, 7, 3),))
        self.assertEqual([s.session_date for s in cal.weekly_sessions()], [dt.date(2026, 7, 2)])

    def test_partial_calendar_week_never_treats_wednesday_as_week_end(self):
        cal = calendar(1)
        partial = MomentumCalendar(cal.sessions[:3], cal.coverage_start, cal.sessions[2].session_date)
        self.assertEqual(partial.weekly_sessions(), ())
        result = run(cal, members(), as_of=cal.sessions[2].close_time+dt.timedelta(minutes=30))
        self.assertEqual(result['decisions'], ())

    def test_early_close_cutoff_and_delayed_publication(self):
        day = dt.date(2026, 8, 7)
        cal = calendar(1, early_closes=(day,))
        cutoff = cal.sessions[-1].close_time+dt.timedelta(minutes=30)
        self.assertEqual(run(cal, members(), as_of=cutoff-dt.timedelta(seconds=1))['decisions'], ())
        self.assertEqual(run(cal, members(), as_of=cutoff)['decisions'][0]['signal_time'], cutoff)
        delayed = replace(cal.sessions[-1], close_available_time=cutoff+dt.timedelta(minutes=1))
        late = replace(cal, sessions=cal.sessions[:-1]+(delayed,))
        result = run(late, members(), as_of=cutoff+dt.timedelta(days=1))
        self.assertEqual(result['decisions'][0]['status'], 'missed_signal_cutoff')
        self.assertEqual(result['targets'], ())

    def test_activation_does_not_backfill_earlier_weeks(self):
        cal, universe = calendar(), members()
        activation = cal.sessions[32].session_date
        result = run(cal, universe, activation_date=activation)
        self.assertTrue(result['decisions'])
        self.assertTrue(all(d['signal_session'] >= activation for d in result['decisions']))


class MomentumPortfolioTests(unittest.TestCase):
    def test_sector_and_issuer_limits_scan_down_to_twenty(self):
        cal = calendar()
        universe = tuple(UniverseMember('S%02d' % i, 'Sector%d' % (i//8),
                                        '%010d' % (1 if i == 1 else i+1)) for i in range(40))
        result = run(cal, universe)
        target = result['targets'][0]
        self.assertEqual(len(target.weights), 20)
        self.assertEqual(sum(target.weights.values()), D('.80'))
        self.assertNotIn('S01', target.weights)
        decision = next(d for d in result['decisions'] if d['status'] == 'target_ready')
        self.assertTrue(all(w <= D('.20') for w in decision['sector_weights'].values()))
        selected = [m for m in universe if m.symbol in target.weights]
        self.assertEqual(len({m.issuer_cik for m in selected}), 20)
        candidates = {c['symbol']: c for c in decision['candidates']}
        self.assertEqual(candidates['S01']['reasons'], ('issuer_already_selected',))
        self.assertEqual(candidates['S06']['reasons'], ('sector_cap',))
        self.assertEqual(target.buy_priority, decision['selected_symbols'])

    def test_seventeen_stocks_keep_four_percent_each_and_cash(self):
        cal, universe = calendar(), members(17)
        result = run(cal, universe)
        decision = next(d for d in result['decisions'] if d['status'] == 'target_ready')
        self.assertEqual(decision['target_equity_weight'], D('.68'))
        self.assertEqual(decision['target_cash_weight'], D('.32'))
        self.assertTrue(all(w == D('.04') for w in result['targets'][0].weights.values()))

    def test_exact_thresholds_qualify_and_exclusions_have_reasons(self):
        cal, universe = calendar(), members(8)
        obs = []
        for o in observations(cal, universe):
            if o.symbol == 'S00': o = replace(o, unadjusted_close=D(5))
            if o.symbol == 'S01': o = replace(o, unadjusted_close=D('4.99'))
            if o.symbol == 'S02': o = replace(o, turnover_usd=D(19999999))
            if o.symbol == 'S03': o = replace(o, permission=False)
            if o.symbol == 'S04': o = replace(o, tradable=None)
            if o.symbol == 'S05': o = replace(o, corporate_actions_clear=False)
            if o.symbol == 'S06': o = replace(o, turnover_usd=None)
            if o.symbol == 'S07': o = replace(o, available_at=o.available_at+dt.timedelta(hours=1))
            obs.append(o)
        result = run(cal, universe, tuple(obs))
        decision = next(d for d in result['decisions'] if d['status'] == 'target_ready')
        candidates = {c['symbol']: c for c in decision['candidates']}
        self.assertEqual(decision['selected_symbols'], ('S00',))
        self.assertIn('S05', result['targets'][0].unsafe_holding_symbols)
        for symbol, reason in [('S01', 'below_minimum_price'), ('S02', 'below_minimum_turnover'),
                               ('S03', 'not_permission'), ('S04', 'unknown_tradable'),
                               ('S05', 'unverified_corporate_actions'), ('S06', 'missing_turnover_history'),
                               ('S07', 'missing_current_observation')]:
            self.assertIn(reason, candidates[symbol]['reasons'])

    def test_missing_metadata_is_not_an_empty_liquidation_target(self):
        cal, universe = calendar(), members()
        result = run(cal, universe, readiness=DataReadiness())
        self.assertEqual(result['targets'], ())
        self.assertTrue(all(d['status'] == 'data_not_ready' for d in result['decisions']))
        self.assertEqual(len(result['missing_checks']), 5)
        no_records = run(cal, universe, ())
        self.assertEqual(no_records['targets'], ())
        self.assertIn('empty_eligible_pool_with_data_gaps', no_records['decisions'][-1]['reasons'])

    def test_later_data_gaps_preserve_already_held_position(self):
        cal, universe = calendar(), members(1)
        original = observations(cal, universe)
        first = run(cal, universe, original)['targets'][0].signal_session
        obs = tuple(replace(o, turnover_usd=None) if o.session_date > first else o for o in original)
        strategy = run(cal, universe, obs)
        self.assertEqual(len(strategy['targets']), 1)
        account = simulate_portfolio(cal.sessions,
            [SessionBar(o.symbol, o.session_date, o.unadjusted_close, o.unadjusted_close) for o in obs],
            strategy['targets'])
        self.assertEqual(len(account['trades']), 1)
        self.assertEqual(account['summary']['final_shares']['S00'], 40)

    def test_genuinely_ineligible_pool_produces_explicit_cash_target(self):
        cal, universe = calendar(), members()
        obs = tuple(replace(o, unadjusted_close=D(4)) for o in observations(cal, universe))
        result = run(cal, universe, obs)
        self.assertTrue(result['targets'])
        self.assertEqual(dict(result['targets'][0].weights), {})
        self.assertEqual(result['decisions'][-1]['status'], 'cash_target_ready')
        self.assertEqual(result['decisions'][-1]['target_cash_weight'], D(1))

    def test_negative_momentum_and_ties_are_deterministic(self):
        cal, universe = calendar(), members(3)
        obs = []
        for i, session in enumerate(cal.sessions):
            for m in universe:
                obs.append(MomentumObservation(m.symbol, session.session_date, session.close_available_time,
                                               D(100)-i, D(100), D(20000000), True, True, True))
        result = run(cal, universe, tuple(reversed(obs)))
        self.assertEqual(result['targets'][0].buy_priority, ('S00', 'S01', 'S02'))
        self.assertLess(next(d for d in result['decisions'] if d['status']=='target_ready')['candidates'][0]['momentum'], 0)

    def test_future_prices_and_revisions_do_not_change_past_decisions(self):
        cal, universe = calendar(), members()
        obs = observations(cal, universe)
        cut_session = cal.weekly_sessions()[-2]
        cutoff = cut_session.close_time+dt.timedelta(minutes=30)
        prefix = run(cal, universe, tuple(o for o in obs if o.available_at <= cutoff), as_of=cutoff)
        altered = tuple(replace(o, adjusted_close=D(999999)) if o.available_at > cutoff else o for o in obs)
        revision = replace(obs[0], adjusted_close=D(1), available_at=cutoff+dt.timedelta(days=1))
        full = run(cal, universe, altered+(revision,), as_of=cutoff)
        self.assertEqual(prefix['decisions'], full['decisions'])
        self.assertEqual(prefix['targets'], full['targets'])

    def test_target_reaches_account_only_on_next_session_open(self):
        cal, universe = calendar(), members(3)
        obs = observations(cal, universe)
        result = run(cal, universe, obs)
        prices = [SessionBar(o.symbol, o.session_date, o.unadjusted_close, o.unadjusted_close) for o in obs]
        account = simulate_portfolio(cal.sessions, prices, result['targets'], PortfolioConfig())
        first = result['targets'][0]
        index = next(i for i, s in enumerate(cal.sessions) if s.session_date == first.signal_session)
        self.assertEqual(account['trades'][0]['fill_session'], cal.sessions[index+1].session_date)
        self.assertEqual(account['trades'][0]['fill_time'], cal.sessions[index+1].open_time)
        self.assertTrue(all(t['fill_time'] > t['signal_time'] for t in account['trades']))

    def test_unsafe_held_symbol_skips_entire_rebalance(self):
        cal = calendar(1)
        s = cal.sessions
        prices = [SessionBar(symbol, session.session_date, D(10), D(10)) for session in s for symbol in ['A', 'B']]
        targets = [TargetWeights(s[0].session_date, s[0].close_available_time, {'A': D('.5')}),
                   TargetWeights(s[1].session_date, s[1].close_available_time, {'B': D('.5')}, (), ('A',))]
        result = simulate_portfolio(s, prices, targets)
        self.assertEqual(result['rebalances'][1]['status'], 'skipped_unsafe_holding_data')
        self.assertEqual(len(result['trades']), 1)
        self.assertEqual(result['summary']['final_shares']['A'], 5000)

    def test_observation_cannot_precede_its_market_close(self):
        cal, universe = calendar(), members(1)
        obs = observations(cal, universe)
        bad = replace(obs[0], available_at=cal.sessions[0].open_time)
        with self.assertRaises(ValueError):
            run(cal, universe, (bad,)+obs[1:])

    def test_missing_sector_or_issuer_never_gets_selected(self):
        cal = calendar()
        universe = (UniverseMember('S00', None, '1'), UniverseMember('S01', 'Tech', None))
        result = run(cal, universe)
        self.assertEqual(result['targets'], ())
        self.assertEqual(result['decisions'][-1]['status'], 'data_not_ready')

    def test_frozen_configuration_and_snapshot_hash(self):
        rules, universe, cfg = load_momentum_config(ROOT/'configs/momentum_v1.json')
        self.assertEqual(len(universe), 503)
        self.assertEqual(rules, MomentumRules())
        self.assertFalse(cfg['broker_routing_enabled'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            raw = b'{"records": [], "security_count": 0}'
            (path/'universe.json').write_bytes(raw)
            cfg['universe']['snapshot_file'] = 'universe.json'
            cfg['universe']['snapshot_sha256'] = hashlib.sha256(raw).hexdigest()
            (path/'rules.json').write_text(json.dumps(cfg))
            (path/'universe.json').write_bytes(raw+b' ')
            with self.assertRaises(ValueError):
                load_momentum_config(path/'rules.json')


if __name__ == '__main__':
    unittest.main()
