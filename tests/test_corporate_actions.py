import datetime as dt
import unittest
from decimal import Decimal as D

from quant_backtest import CorporateAction, simulate_portfolio
from test_portfolio import sessions,bars,target,config


def event(session,kind='split',symbol='A',ratio='2',cash=None,event_id='e',**extra):
    values=dict(event_id=event_id,symbol=symbol,effective_session=session.session_date,kind=kind,
        available_at=session.open_time,ratio=D(ratio) if kind=='split' else None,
        cash_per_share=D(cash) if cash is not None else None,verified=True,evidence='hand-checked fixture units')
    values.update(extra)
    return CorporateAction(**values)


class CorporateActionTests(unittest.TestCase):
    def test_dividend_before_sale_and_no_entitlement_for_exdate_buyer(self):
        s=sessions(4); b=bars(s,{'A':[10,10,9,9]})
        e=event(s[2],'cash_dividend',cash='1')
        r=simulate_portfolio(s,b,[target(s[0],{'A':1}),target(s[1],{})],config(),corporate_actions=[e])
        self.assertEqual(r['corporate_actions'][0]['entitled_shares'],D(100))
        self.assertEqual(r['summary']['dividend_income'],D(100))
        self.assertEqual(r['summary']['final_cash'],D(1000))
        self.assertEqual(r['summary']['final_equity'],D(1000))
        buyer=simulate_portfolio(s,b,[target(s[1],{'A':'.9'})],config(),corporate_actions=[e])
        self.assertEqual(buyer['summary']['dividend_income'],D(0))
        self.assertEqual(buyer['summary']['final_shares']['A'],90)

    def test_split_before_buy_adjusts_pending_target_without_future_price_resize(self):
        s=sessions(2)
        r=simulate_portfolio(s,bars(s,{'A':[100,40]}),[target(s[0],{'A':'.5'})],config(),
            corporate_actions=[event(s[1])])
        order=r['orders'][0]
        self.assertEqual((order['signal_target_shares'],order['target_shares'],order['requested_shares']),(5,10,10))
        self.assertEqual(r['summary']['final_shares']['A'],10)
        self.assertEqual(r['summary']['final_cash'],D(600))

    def test_split_holdings_then_sells_use_restated_shares_and_basis(self):
        s=sessions(3)
        r=simulate_portfolio(s,bars(s,{'A':[100,100,50]}),
            [target(s[0],{'A':1}),target(s[1],{})],config(),corporate_actions=[event(s[2])])
        self.assertEqual([t['shares'] for t in r['trades']],[10,20])
        self.assertEqual(r['summary']['final_cash'],D(1000))
        self.assertEqual(r['positions'][-1]['cost_basis'],D(0))

    def test_reverse_split_fraction_cost_and_cash_hand_reconcile(self):
        s=sessions(3)
        r=simulate_portfolio(s,bars(s,{'A':[10,10,40]}),[target(s[0],{'A':1})],
            config(initial_cash=D(30)),corporate_actions=[event(s[2],ratio='.5')])
        # 3 original shares -> 1.5; half-share cash 20, its basis 10.
        self.assertEqual(r['summary']['final_shares']['A'],1)
        self.assertEqual(r['summary']['final_cash'],D(20))
        self.assertEqual(r['positions'][-1]['cost_basis'],D(20))
        self.assertEqual(r['positions'][-1]['realized_pnl'],D(10))
        self.assertEqual(r['summary']['final_equity'],D(60))
        self.assertEqual([x['kind'] for x in r['corporate_actions']],['split','cash_in_lieu'])

    def test_same_day_split_dividend_fraction_sequence(self):
        s=sessions(3)
        es=[event(s[2],'cash_dividend',cash='2',event_id='d'),event(s[2],ratio='.5')]
        r=simulate_portfolio(s,bars(s,{'A':[10,10,18]}),[target(s[0],{'A':1})],
            config(initial_cash=D(30)),corporate_actions=es)
        self.assertEqual([x['kind'] for x in r['corporate_actions']],['split','cash_dividend','cash_in_lieu'])
        self.assertEqual(r['summary']['dividend_income'],D(3))
        self.assertEqual(r['summary']['final_cash'],D(12))
        self.assertEqual(r['summary']['final_equity'],D(30))

    def test_new_dividend_cash_does_not_increase_requested_shares(self):
        s=sessions(3)
        r=simulate_portfolio(s,bars(s,{'A':[10,10,10],'B':[10,10,10]}),
            [target(s[0],{'A':'.5'}),target(s[1],{'A':'.5','B':'.5'})],config(),
            corporate_actions=[event(s[2],'cash_dividend',cash='2')])
        self.assertEqual(r['summary']['final_shares'],{'A':50,'B':50})
        self.assertEqual(r['summary']['final_cash'],D(100))

    def test_duplicate_event_deduplicated_conflict_rejected(self):
        s=sessions(3); b=bars(s,{'A':[10,10,9]}); e=event(s[2],'cash_dividend',cash='1')
        r=simulate_portfolio(s,b,[target(s[0],{'A':1})],config(),corporate_actions=[e,e])
        self.assertEqual(r['summary']['dividend_income'],D(100))
        self.assertEqual(len(r['corporate_actions']),1)
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            simulate_portfolio(s,b,[],config(),corporate_actions=[e,event(s[2],'cash_dividend',cash='2')])
        with self.assertRaisesRegex(ValueError,'Multiple'):
            simulate_portfolio(s,b,[],config(),corporate_actions=[event(s[2]),event(s[2],event_id='other')])

    def test_unsupported_held_event_blocks_all_orders_and_performance(self):
        s=sessions(4)
        r=simulate_portfolio(s,bars(s,{'A':[10]*4,'B':[10]*4}),
            [target(s[0],{'A':'.5'}),target(s[1],{'B':'.5'}),target(s[2],{})],config(),
            corporate_actions=[event(s[2],'unsupported')])
        self.assertEqual(len(r['trades']),1)
        self.assertTrue(all(o['status']=='cancelled_unresolved_corporate_action' for o in r['orders'][1:]))
        self.assertIsNone(r['daily'][2]['equity'])
        self.assertIsNone(r['daily'][3]['equity'])
        self.assertEqual(r['summary']['final_shares']['A'],50)
        self.assertFalse(r['summary']['performance_complete'])
        self.assertEqual(r['rebalances'][-1]['status'],'skipped_unresolved_corporate_action')

    def test_unsupported_unheld_event_cancels_only_affected_buy(self):
        s=sessions(2)
        r=simulate_portfolio(s,bars(s,{'A':[10]*2,'B':[10]*2}),
            [target(s[0],{'A':'.4','B':'.4'})],config(),corporate_actions=[event(s[1],'unsupported')])
        self.assertEqual([t['symbol'] for t in r['trades']],['B'])
        self.assertTrue(r['summary']['performance_complete'])

    def test_unknown_basis_or_late_event_never_gets_cash_or_share_credit(self):
        s=sessions(3); b=bars(s,{'A':[10]*3}); ts=[target(s[0],{'A':1})]
        for e in (event(s[2],'cash_dividend',cash='1',verified=False),
                  event(s[2],'cash_dividend',cash='1',available_at=s[2].close_time)):
            r=simulate_portfolio(s,b,ts,config(),corporate_actions=[e])
            self.assertEqual(r['summary']['dividend_income'],D(0))
            self.assertFalse(r['summary']['performance_complete'])

    def test_missing_fraction_open_blocks_instead_of_fabricating_cash(self):
        s=sessions(3)
        r=simulate_portfolio(s,bars(s,{'A':[10,10,20]},{'A':[10,10,None]}),
            [target(s[0],{'A':1})],config(initial_cash=D(30)),corporate_actions=[event(s[2],ratio='.5')])
        self.assertEqual(r['summary']['final_cash'],D(0))
        self.assertIsNone(r['summary']['final_equity'])
        self.assertEqual(r['corporate_actions'][0]['reason'],'missing_fractional_cash_in_lieu_open')

    def test_known_split_restates_stale_price_without_filling_missing_close(self):
        s=sessions(3)
        r=simulate_portfolio(s,bars(s,{'A':[10,10,None]},{'A':[10,10,5]}),
            [target(s[0],{'A':1})],config(),corporate_actions=[event(s[2])])
        self.assertEqual(r['positions'][-1]['mark_price'],D(5))
        self.assertEqual(r['daily'][-1]['estimated_equity'],D(1000))
        self.assertIsNone(r['daily'][-1]['equity'])

    def test_appending_future_actions_does_not_change_past_cash_or_positions(self):
        s=sessions(4); b=bars(s,{'A':[10,10,10,5]}); ts=[target(s[0],{'A':1})]
        a=simulate_portfolio(s,b,ts,config(),corporate_actions=[event(s[3])])
        b=simulate_portfolio(s[:3],bars(s[:3],{'A':[10]*3}),ts,config())
        self.assertEqual(a['daily'][:3],b['daily'])
        self.assertEqual(a['positions'][:2],b['positions'])

    def test_contract_and_proxy_rejections(self):
        s=sessions(2)
        with self.assertRaises(ValueError): event(s[1],ratio='NaN')
        with self.assertRaises(ValueError): event(s[1],evidence='')
        with self.assertRaises(ValueError):
            simulate_portfolio(sessions(2,'utc_calendar_day_proxy'),bars(s,{'A':[10]*2}),[],
                config(execution_mode='next_session_close_proxy'),corporate_actions=[event(s[1])])

    def test_exact_one_for_three_reverse_split_has_no_rounding_phantom_fraction(self):
        s=sessions(3)
        e=CorporateAction('exact','A',s[2].session_date,'split',s[2].open_time,
            verified=True,evidence='exact 1:3 source ratio',ratio_numerator=1,ratio_denominator=3)
        r=simulate_portfolio(s,bars(s,{'A':[10,10,30]}),[target(s[0],{'A':1})],
            config(initial_cash=D(30)),corporate_actions=[e])
        self.assertEqual(r['summary']['final_shares']['A'],1)
        self.assertEqual(r['summary']['final_cash'],D(0))
        self.assertEqual(len(r['corporate_actions']),1)
        self.assertEqual(r['summary']['final_equity'],D(30))

    def test_pending_reverse_split_target_uses_exact_rational_floor(self):
        s=sessions(2)
        e=CorporateAction('exact','A',s[1].session_date,'split',s[1].open_time,
            verified=True,evidence='exact 1:3 source ratio',ratio_numerator=1,ratio_denominator=3)
        r=simulate_portfolio(s,bars(s,{'A':[10,30]}),[target(s[0],{'A':1})],
            config(initial_cash=D(30)),corporate_actions=[e])
        self.assertEqual(r['orders'][0]['target_shares'],1)
        self.assertEqual(r['summary']['final_shares']['A'],1)

    def test_unknown_cash_amount_without_entitlement_does_not_block_new_exdate_buyer(self):
        s=sessions(2)
        r=simulate_portfolio(s,bars(s,{'A':[10,9]}),[target(s[0],{'A':'.5'})],config(),
            corporate_actions=[event(s[1],'cash_dividend',cash='1',verified=False)])
        self.assertEqual(len(r['trades']),1)
        self.assertEqual(r['summary']['dividend_income'],D(0))

    def test_explicit_flat_resolution_allows_later_entry_but_cannot_clear_held_unknown(self):
        s=sessions(4)
        es=[event(s[1],'unsupported'),event(s[2],'resolve_unheld',event_id='resolved')]
        flat=simulate_portfolio(s,bars(s,{'A':[10]*4}),[target(s[2],{'A':'.5'})],config(),corporate_actions=es)
        self.assertEqual(flat['summary']['final_shares']['A'],50)
        # Buy before the unknown event; even an explicit flat-only resolution cannot erase it.
        held=simulate_portfolio(s,bars(s,{'A':[10]*4}),[target(s[0],{'A':'.5'})],config(),
            corporate_actions=[event(s[2],'unsupported'),event(s[3],'resolve_unheld',event_id='resolved')])
        self.assertIsNone(held['summary']['final_equity'])
        self.assertEqual(held['summary']['final_shares']['A'],50)

    def test_same_day_unsupported_action_prevents_cash_with_ambiguous_entitlement(self):
        s=sessions(3)
        r=simulate_portfolio(s,bars(s,{'A':[10]*3}),[target(s[0],{'A':1})],config(),
            corporate_actions=[event(s[2],'cash_dividend',cash='1'),event(s[2],'unsupported',event_id='unknown')])
        self.assertEqual(r['summary']['dividend_income'],D(0))
        self.assertFalse(r['summary']['performance_complete'])


if __name__=='__main__': unittest.main()
