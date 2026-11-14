import datetime as dt
import unittest
from decimal import Decimal as D

from quant_backtest import PortfolioConfig, SessionBar, TargetWeights, TradingSession, simulate_portfolio

UTC = dt.timezone.utc


def sessions(count=4, kind='regular'):
    dates = [dt.date(2026, 11, 2), dt.date(2026, 11, 3),
             dt.date(2026, 11, 4), dt.date(2026, 11, 5), dt.date(2026, 11, 6)]
    return [TradingSession(date, dt.datetime.combine(date, dt.time(14, 30), UTC),
                           dt.datetime.combine(date, dt.time(21), UTC),
                           dt.datetime.combine(date, dt.time(21, 30), UTC), kind)
            for date in dates[:count]]


def bars(schedule, prices, opens=None):
    result = []
    for symbol, values in prices.items():
        for i, close in enumerate(values):
            opening = opens[symbol][i] if opens and symbol in opens else close
            result.append(SessionBar(symbol, schedule[i].session_date,
                                     None if opening is None else D(str(opening)),
                                     None if close is None else D(str(close))))
    return result


def target(session, weights, priority=()):
    return TargetWeights(session.session_date, session.close_available_time,
                         {s: D(str(w)) for s, w in weights.items()}, priority)


def config(**overrides):
    values = dict(initial_cash=D('1000'), fee_per_share=D('0'), minimum_fee=D('0'), slippage_bps=D('0'))
    values.update(overrides)
    return PortfolioConfig(**values)


class PortfolioTests(unittest.TestCase):
    def test_hand_computed_shared_account_and_full_target_replacement(self):
        s = sessions(3)
        b = bars(s, {'A': [10, 10, 10], 'B': [20, 20, 20]})
        r = simulate_portfolio(s, b, [target(s[0], {'A': '.4', 'B': '.4'}),
                                      target(s[1], {'B': '.8'})], config(minimum_fee=D(1)))
        self.assertEqual([t['shares'] for t in r['trades']], [40, 20, 40, 19])
        self.assertEqual([t['side'] for t in r['trades']], ['BUY', 'BUY', 'SELL', 'BUY'])
        self.assertEqual(r['daily'][0]['equity'], D(1000))
        self.assertEqual(r['daily'][1]['cash'], D(198))
        self.assertEqual(r['summary']['final_cash'], D(216))
        self.assertEqual(r['summary']['final_equity'], D(996))
        self.assertEqual(r['summary']['commissions'], D(4))
        self.assertEqual(r['summary']['final_shares'], {'A': 0, 'B': 39})

    def test_sell_proceeds_fund_buy_before_alphabetical_symbol(self):
        s = sessions(3)
        r = simulate_portfolio(s, bars(s, {'AAA': [20]*3, 'ZZZ': [10]*3}),
                               [target(s[0], {'ZZZ': 1}), target(s[1], {'AAA': 1})], config())
        self.assertEqual([(t['symbol'], t['side'], t['shares']) for t in r['trades']],
                         [('ZZZ', 'BUY', 100), ('ZZZ', 'SELL', 100), ('AAA', 'BUY', 50)])
        self.assertEqual(r['summary']['final_cash'], D(0))

    def test_cash_shortfall_obeys_priority_and_never_resizes_from_future_upwards(self):
        s = sessions(2)
        t = target(s[0], {'A': '.5', 'B': '.5'}, ('B', 'A'))
        for price, expected_cash, expected_fills in [(20, 0, [('B', 50)]),
                                                       (5, 500, [('B', 50), ('A', 50)])]:
            with self.subTest(open_price=price):
                r = simulate_portfolio(s, bars(s, {'A': [10, price], 'B': [10, price]}), [t], config())
                self.assertEqual([o['requested_shares'] for o in r['orders']], [50, 50])
                self.assertEqual([(x['symbol'], x['shares']) for x in r['trades']], expected_fills)
                self.assertEqual(r['summary']['final_cash'], D(expected_cash))

    def test_partial_fill_remainder_expires_and_zero_fill_has_no_commission(self):
        s = sessions(3)
        r = simulate_portfolio(s, bars(s, {'A': [10, 20, 1]}), [target(s[0], {'A': 1})],
                               config(minimum_fee=D(1)))
        self.assertEqual(r['orders'][0]['requested_shares'], 100)
        self.assertEqual(r['orders'][0]['filled_shares'], 49)
        self.assertEqual(r['orders'][0]['status'], 'partial_remainder_cancelled')
        self.assertEqual(len(r['trades']), 1)
        self.assertEqual(r['summary']['final_cash'], D(19))
        zero = simulate_portfolio(s[:2], bars(s[:2], {'A': [10, 20]}), [target(s[0], {'A': 1})],
                                  config(initial_cash=D(1), minimum_fee=D(1)))
        self.assertEqual(zero['trades'], [])
        self.assertEqual(zero['summary']['commissions'], D(0))

    def test_slippage_and_fees_reconcile_one_round_trip(self):
        s = sessions(3)
        r = simulate_portfolio(s, bars(s, {'A': [10]*3}),
                               [target(s[0], {'A': '.1'}), target(s[1], {})],
                               config(minimum_fee=D(1), slippage_bps=D(100)))
        buy, sell = r['trades']
        self.assertEqual(buy['fill_price'], D('10.1'))
        self.assertEqual(buy['cost_basis_after'], D(102))
        self.assertEqual(sell['fill_price'], D('9.9'))
        self.assertEqual(r['summary']['commissions'], D(2))
        self.assertEqual(r['summary']['slippage_cost'], D(2))
        self.assertEqual(r['summary']['final_equity'], D(996))
        self.assertEqual(r['positions'][-1]['realized_pnl'], D(-4))
        self.assertEqual(r['positions'][-1]['cost_basis'], D(0))

    def test_unchanged_weight_rebalances_drift_only_on_target_event(self):
        s = sessions(4)
        b = bars(s, {'A': [10, 20, 20, 40]}, {'A': [10, 10, 20, 20]})
        t = [target(s[0], {'A': '.5'}), target(s[1], {'A': '.5'})]
        r = simulate_portfolio(s, b, t, config())
        self.assertEqual([(x['side'], x['shares']) for x in r['trades']], [('BUY', 50), ('SELL', 13)])
        self.assertEqual(r['summary']['final_shares']['A'], 37)
        self.assertEqual(r['summary']['final_cash'], D(760))

    def test_missing_next_session_bar_cancels_without_waiting_for_later_bar(self):
        s = sessions(3)
        b = [SessionBar('A', s[0].session_date, D(10), D(10)),
             SessionBar('A', s[2].session_date, D(1), D(1))]
        r = simulate_portfolio(s, b, [target(s[0], {'A': 1})], config())
        self.assertEqual(r['trades'], [])
        self.assertEqual(r['orders'][0]['attempt_session'], s[1].session_date)
        self.assertEqual(r['orders'][0]['status'], 'cancelled_missing_execution_price')
        self.assertEqual(r['summary']['final_equity'], D(1000))

    def test_untradable_and_missing_open_do_not_fill(self):
        s = sessions(2)
        for opening, tradable, status in [(None, True, 'cancelled_missing_execution_price'),
                                           (D(10), False, 'cancelled_not_tradable')]:
            b = [SessionBar('A', s[0].session_date, D(10), D(10)),
                 SessionBar('A', s[1].session_date, opening, D(10), tradable)]
            r = simulate_portfolio(s, b, [target(s[0], {'A': '.5'})], config())
            self.assertEqual(r['trades'], [])
            self.assertEqual(r['orders'][0]['status'], status)

    def test_missing_held_close_exposes_invalid_nav_and_skips_rebalance(self):
        s = sessions(4)
        b = bars(s, {'A': [10, None, 12, 12]}, {'A': [10, 10, 12, 12]})
        r = simulate_portfolio(s, b, [target(s[0], {'A': '.5'}), target(s[1], {})], config())
        self.assertIsNone(r['daily'][1]['equity'])
        self.assertIsNone(r['daily'][1]['net_value'])
        self.assertIsNone(r['daily'][1]['daily_return'])
        self.assertEqual(r['daily'][1]['estimated_equity'], D(1000))
        self.assertEqual(r['rebalances'][1]['status'], 'skipped_missing_signal_prices')
        self.assertEqual(r['daily'][2]['equity'], D(1100))
        self.assertIsNone(r['daily'][2]['daily_return'])
        self.assertEqual(r['daily'][3]['daily_return'], D(0))
        self.assertFalse(r['summary']['performance_complete'])
        self.assertIsNone(r['summary']['max_drawdown_pct'])
        self.assertEqual(len(r['trades']), 1)

    def test_unknown_signal_price_does_not_use_future_price(self):
        s = sessions(2)
        b = [SessionBar('B', s[0].session_date, D(10), D(10)),
             SessionBar('A', s[1].session_date, D(10), D(10))]
        r = simulate_portfolio(s, b, [target(s[0], {'A': '.5'})], config())
        self.assertEqual(r['orders'], [])
        self.assertEqual(r['summary']['skipped_rebalances'], 1)

    def test_future_prices_and_targets_cannot_change_past_ledgers(self):
        s = sessions(4)
        ts = [target(s[0], {'A': '.5'}), target(s[2], {})]
        b = bars(s, {'A': [10, 11, 12, 13]}, {'A': [10, 10, 11, 12]})
        future_ts = ts + [target(s[3], {'A': '.8'})]
        full = simulate_portfolio(s, b, future_ts, config(minimum_fee=D(1)))
        changed = simulate_portfolio(s, bars(s, {'A': [10, 11, 12, 999]},
                                            {'A': [10, 10, 11, 999]}), future_ts, config(minimum_fee=D(1)))
        prefix = simulate_portfolio(s[:3], b[:3], ts, config(minimum_fee=D(1)))
        self.assertEqual(full['daily'][:3], prefix['daily'])
        self.assertEqual([p for p in full['positions'] if p['session_date'] <= s[2].session_date],
                         prefix['positions'])
        self.assertEqual(full['daily'][:3], changed['daily'][:3])
        self.assertEqual(full['orders'][1]['requested_shares'], changed['orders'][1]['requested_shares'])
        self.assertEqual(prefix['orders'][-1]['status'], 'pending_end_of_sample')
        self.assertEqual(full['trades'][0], prefix['trades'][0])

    def test_end_of_sample_does_not_invent_fill_or_liquidation(self):
        s = sessions(2)
        first = simulate_portfolio(s[:1], bars(s[:1], {'A': [10]}), [target(s[0], {'A': '.5'})], config())
        self.assertEqual(first['trades'], [])
        self.assertEqual(first['summary']['pending_orders'], 1)
        second = simulate_portfolio(s, bars(s, {'A': [10, 10]}), [target(s[0], {'A': '.5'})], config())
        self.assertEqual(second['summary']['final_shares']['A'], 50)
        self.assertEqual(len(second['trades']), 1)

    def test_cash_and_shares_can_be_reconstructed_from_trade_log(self):
        s = sessions(5)
        b = bars(s, {'A': [10, 12, 11, 15, 16], 'B': [20, 18, 19, 20, 21]})
        ts = [target(s[0], {'A': '.4', 'B': '.4'}), target(s[2], {'B': '.8'}), target(s[3], {})]
        cfg = config(minimum_fee=D(1), slippage_bps=D(5))
        r = simulate_portfolio(s, b, ts, cfg)
        cash, shares = cfg.initial_cash, {'A': 0, 'B': 0}
        quotes = {(x.session_date, x.symbol): x.close for x in b}
        for day in r['daily']:
            for trade in r['trades']:
                if trade['fill_session'] == day['session_date']:
                    qty = trade['shares']
                    buy = trade['side'] == 'BUY'
                    fee = max(cfg.minimum_fee, cfg.fee_per_share * qty)
                    cash += (-1 if buy else 1) * qty * trade['fill_price'] - fee
                    shares[trade['symbol']] += qty if buy else -qty
            market_value = sum((q * quotes[day['session_date'], symbol] for symbol, q in shares.items()), D(0))
            self.assertEqual(day['cash'], cash)
            self.assertEqual(day['equity'], cash + market_value)
            self.assertTrue(cash >= 0 and all(q >= 0 for q in shares.values()))

    def test_sell_fee_cannot_create_negative_cash_and_zero_equity_can_be_reported(self):
        s = sessions(4)
        r = simulate_portfolio(s, bars(s, {'A': [1, 1, '.01', '.01']}),
                               [target(s[0], {'A': 1}), target(s[1], {})],
                               config(initial_cash=D(2), minimum_fee=D(1)))
        self.assertEqual(r['orders'][1]['status'], 'cancelled_insufficient_cash')
        self.assertEqual(r['summary']['final_cash'], D(0))
        bankrupt = simulate_portfolio(s, bars(s, {'A': [1]*4}),
                                      [target(s[0], {'A': 1}), target(s[1], {})],
                                      config(initial_cash=D(2), minimum_fee=D(1)))
        self.assertEqual(bankrupt['summary']['final_equity'], D(0))
        self.assertEqual(bankrupt['summary']['return_pct'], D(-100))
        self.assertIsNone(bankrupt['daily'][-1]['daily_return'])

    def test_target_input_is_copied_and_invalid_weights_are_rejected(self):
        s = sessions(1)[0]
        weights = {'A': D('.4')}
        t = TargetWeights(s.session_date, s.close_available_time, weights)
        weights['A'] = D(1)
        self.assertEqual(t.weights['A'], D('.4'))
        for bad in [{'A': .4}, {'A': D('NaN')}, {'A': D('-.1')},
                    {'A': D('.6'), 'B': D('.6')}]:
            with self.assertRaises(ValueError):
                TargetWeights(s.session_date, s.close_available_time, bad)
        with self.assertRaises(ValueError):
            target(s, {'A': '.4', 'B': '.4'}, ('A',))

    def test_time_calendar_and_price_contracts(self):
        s = sessions(2)
        b = bars(s, {'A': [10, 10]})
        with self.assertRaises(ValueError):
            simulate_portfolio(s, b, [TargetWeights(s[0].session_date, s[0].close_time, {'A': D('.5')})], config())
        with self.assertRaises(ValueError):
            simulate_portfolio(s, b, [TargetWeights(s[0].session_date, s[1].open_time, {'A': D('.5')})], config())
        with self.assertRaises(ValueError):
            simulate_portfolio(s[::-1], b, [], config())
        with self.assertRaises(ValueError):
            simulate_portfolio(s, b+b[:1], [], config())
        with self.assertRaises(ValueError):
            simulate_portfolio(s, b, [target(s[0], {}), target(s[0], {})], config())
        with self.assertRaises(ValueError):
            SessionBar('A', s[0].session_date, 10.0, D(10))
        with self.assertRaises(ValueError):
            simulate_portfolio(sessions(2, 'utc_calendar_day_proxy'), b, [], config())

    def test_explicit_close_proxy_uses_close_instead_of_open(self):
        s = sessions(2, 'utc_calendar_day_proxy')
        r = simulate_portfolio(s, bars(s, {'A': [10, 12]}, {'A': [10, 1]}),
                               [target(s[0], {'A': '.5'})], config(execution_mode='next_session_close_proxy'))
        self.assertEqual(r['trades'][0]['reference_price'], D(12))
        self.assertEqual(r['trades'][0]['fill_time'], s[1].close_time)
        self.assertEqual(r['summary']['session_kind'], 'utc_calendar_day_proxy')


if __name__ == '__main__':
    unittest.main()
