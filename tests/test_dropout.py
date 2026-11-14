import datetime as dt
import unittest
from decimal import Decimal as D
from types import MappingProxyType

from quant_backtest.portfolio import AccountSnapshot
from quant_backtest import TradingSession, SessionBar, TargetWeights, PortfolioConfig, simulate_portfolio
from quant_portfolio.dropout import TopKDropStrategy
from quant_portfolio.risk_checks import audit_target_risk


def fixture():
    utc=dt.timezone.utc
    days=[dt.date(2026,11,d) for d in (2,3,4)]
    sessions=[TradingSession(d,dt.datetime.combine(d,dt.time(14,30),utc),
        dt.datetime.combine(d,dt.time(21),utc),dt.datetime.combine(d,dt.time(21,30),utc)) for d in days]
    return days,sessions


def decision(day,time,count=30):
    return dict(signal_session=day,signal_time=time,status='target_ready',candidates=[
        dict(symbol='S%02d'%i,score=D(i),eligible=True,data_reasons=(),
             sector='sector%d'%(i%6),issuer_cik=str(i)) for i in range(count)])


class DropoutTests(unittest.TestCase):
    def setUp(self):
        self.days,self.sessions=fixture()

    def state(self,holdings,valid=True):
        return AccountSnapshot(self.days[0],self.sessions[0].close_available_time,D(20000),D(100000),
            MappingProxyType(holdings),MappingProxyType({}),D(0),valid)

    def test_actual_holdings_cap_and_complete_portfolio(self):
        d=decision(self.days[0],self.sessions[0].close_available_time)
        strategy=TopKDropStrategy([d])
        held={'S%02d'%i:10 for i in range(20)}
        target=strategy(self.state(held))
        audit=strategy.audit[-1]
        self.assertEqual(len(audit['replacements']),4)
        self.assertEqual(len(target.weights),20)
        self.assertEqual(len(set(target.weights).intersection(held)),16)
        self.assertEqual(sum(target.weights.values()),D('.8'))
        self.assertEqual(audit['actual_holdings'],held)

    def test_ties_do_not_replace_and_small_account_rounds_down(self):
        d=decision(self.days[0],self.sessions[0].close_available_time)
        for c in d['candidates']: c['score']=D(1)
        strategy=TopKDropStrategy([d])
        strategy(self.state({'S%02d'%i:1 for i in range(20)}))
        self.assertEqual(strategy.audit[-1]['replacements'],[])
        strategy(self.state({'S00':1,'S01':1,'S02':1}))
        self.assertEqual(strategy.audit[-1]['maximum_regular_replacements'],0)

    def test_initial_and_partial_fill_use_vacancy_buys(self):
        strategy=TopKDropStrategy([decision(self.days[0],self.sessions[0].close_available_time)])
        target=strategy(self.state({}))
        self.assertEqual(len(target.weights),20)
        self.assertEqual(len(strategy.audit[-1]['vacancy_buys']),20)
        self.assertEqual(strategy.audit[-1]['replacements'],[])
        strategy(self.state({'S29':1}))
        self.assertEqual(len(strategy.audit[-1]['vacancy_buys']),19)

    def test_forced_exit_and_unknown_holding_data(self):
        d=decision(self.days[0],self.sessions[0].close_available_time)
        d['candidates'][0].update(eligible=False,data_reasons=('missing_factor',))
        strategy=TopKDropStrategy([d])
        target=strategy(self.state({'S00':5,'S29':1}))
        self.assertNotIn('S00',target.weights)
        self.assertEqual(strategy.audit[-1]['forced_exits'],['S00'])
        d['candidates'][0]['data_reasons']=('unresolved_current_price_or_action_basis',)
        self.assertIsNone(strategy(self.state({'S00':5})))

    def test_industry_and_issuer_constraints_survive_replacements(self):
        d=decision(self.days[0],self.sessions[0].close_available_time)
        d['candidates'][-1]['issuer_cik']=d['candidates'][-2]['issuer_cik']
        strategy=TopKDropStrategy([d]); target=strategy(self.state({}))
        picked=[c for c in d['candidates'] if c['symbol'] in target.weights]
        self.assertEqual(len({c['issuer_cik'] for c in picked}),len(picked))
        self.assertTrue(all(sum(c['sector']==s for c in picked)<=5 for s in {c['sector'] for c in picked}))

    def test_margin_and_future_score_guards(self):
        d=decision(self.days[0],self.sessions[0].close_available_time)
        strategy=TopKDropStrategy([d],score_margin_std=D(100))
        strategy(self.state({'S%02d'%i:1 for i in range(20)}))
        self.assertEqual(strategy.audit[-1]['replacements'],[])
        d['signal_time']+=dt.timedelta(seconds=1)
        self.assertIsNone(strategy(self.state({})))

    def test_callback_observes_cancelled_fills_and_readonly_snapshot(self):
        seen=[]
        def callback(state):
            seen.append(state)
            with self.assertRaises(TypeError): state.holdings['X']=99
            if state.session_date==self.days[0]:
                return TargetWeights(self.days[0],state.available_at,{'A':D('.4')})
            if state.session_date==self.days[1]:
                self.assertEqual(dict(state.holdings),{})
                return TargetWeights(self.days[1],state.available_at,{'B':D('.4')})
        bars=[SessionBar(s,d,None if s=='A' and d==self.days[1] else D(10),D(10))
              for d in self.days for s in ('A','B')]
        result=simulate_portfolio(self.sessions,bars,strategy=callback)
        self.assertEqual(result['orders'][0]['status'],'cancelled_missing_execution_price')
        self.assertIn('B',seen[-1].holdings)
        self.assertNotIn('A',seen[-1].holdings)

    def test_static_and_callback_accounting_are_identical(self):
        bars=[SessionBar('A',d,D(10),D(11)) for d in self.days]
        t=TargetWeights(self.days[0],self.sessions[0].close_available_time,{'A':D('.8')})
        static=simulate_portfolio(self.sessions,bars,[t])
        dynamic=simulate_portfolio(self.sessions,bars,strategy=lambda s:t if s.session_date==self.days[0] else None)
        self.assertEqual(static,dynamic)
        with self.assertRaises(ValueError): simulate_portfolio(self.sessions,bars,[t],strategy=lambda s:None)

    def test_execution_caps_use_fill_price_and_combined_buy_sell_amounts(self):
        bars=[SessionBar(s,d,D(10),D(10)) for d in self.days for s in ('A','B')]
        cfg=PortfolioConfig(initial_cash=D(1000),slippage_bps=D(0))
        t=TargetWeights(self.days[0],self.sessions[0].close_available_time,{'A':D('.4'),'B':D('.4')},
                        maximum_trade_notional=D(250),symbol_trade_notionals={'A':D(100),'B':D(200)})
        result=simulate_portfolio(self.sessions,bars,[t],cfg)
        trades=result['trades']
        self.assertEqual(sum(t['notional'] for t in trades),250)
        self.assertEqual([t['notional'] for t in trades],[D(100),D(150)])
        self.assertEqual(len(result['execution_limits']),2)
        missing=TargetWeights(self.days[0],self.sessions[0].close_available_time,{'B':D('.4')},
                              symbol_trade_notionals={'A':D(100)})
        blocked=simulate_portfolio(self.sessions,bars,[missing],cfg)
        self.assertEqual(blocked['orders'][0]['status'],'cancelled_trade_budget')

    def test_risk_inputs_are_required_and_incompatible_bounds_reported(self):
        r=audit_target_risk({'A':D('.04')},{'A':'tech'})
        self.assertEqual(r['status'],'not_ready')
        self.assertIn('historical_benchmark_weights',r['missing_inputs'])
        r=audit_target_risk({'A':D('.04')},{'A':'tech'},benchmark={'A':D(1)})
        self.assertIn('benchmark_security_bounds:A',r['infeasible_constraints'])
        self.assertIn('active_security_cap:A',r['violations'])

    def test_beta_and_tracking_error_diagnostics(self):
        r=audit_target_risk({'A':D('.4'),'B':D('.4')},{'A':'tech','B':'other'},
            benchmark={'A':D('.5'),'B':D('.5')},betas={'A':D(1),'B':D(1)},
            daily_covariance={('A','A'):D('.0001'),('B','B'):D('.0001'),('A','B'):D(0),('B','A'):D(0)},
            security_cap=D('.5'),sector_cap=D('.5'),active_cap=D('.2'),sector_active_cap=D('.2'),beta_lower=D('.75'))
        self.assertEqual(r['estimated_beta'],D('.8'))
        self.assertEqual(r['predicted_annual_tracking_error'],D('.000504').sqrt())
        self.assertEqual(r['status'],'within_limits')

    def test_sell_and_buy_share_the_same_notional_budget(self):
        bars=[SessionBar(s,d,D(10),D(10)) for d in self.days for s in ('A','B')]
        a=TargetWeights(self.days[0],self.sessions[0].close_available_time,{'A':D('.4')})
        b=TargetWeights(self.days[1],self.sessions[1].close_available_time,{'B':D('.4')},maximum_trade_notional=D(250))
        r=simulate_portfolio(self.sessions,bars,[a,b],PortfolioConfig(initial_cash=D(1000),slippage_bps=D(0)))
        trades=[t for t in r['trades'] if t['signal_session']==self.days[1]]
        self.assertEqual(sum(t['notional'] for t in trades),250)
        self.assertTrue(all(t['side']=='SELL' for t in trades))


if __name__=='__main__': unittest.main()
