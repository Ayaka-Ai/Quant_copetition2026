import datetime as dt
from decimal import Decimal as D
import unittest
from quant_data import DailyBar
from quant_backtest import simulate, SimulationConfig


def bars(prices, symbol='AAPL'):
    result=[]
    for i,p in enumerate(prices):
        start=dt.datetime(2026,7,1,tzinfo=dt.timezone.utc)+dt.timedelta(days=i)
        p=D(str(p))
        result.append(DailyBar(symbol,start.date(),start,start+dt.timedelta(days=1),p,p,p,p,10000))
    return result


def config(**kwargs):
    values=dict(initial_cash=D('1000'),fee_per_share=D('0'),minimum_fee=D('1'),
                slippage_bps=D('0'),short_window=1,long_window=2)
    values.update(kwargs)
    return SimulationConfig(**values)


class AccountTests(unittest.TestCase):
    def test_hand_calculated_roundtrip_and_next_bar(self):
        r=simulate(bars([10,20,25,15,12]),config())
        # Day 2 requests 49 shares; day 3 cash permits only 39 at $25 + $1.
        # Day 4 signals flat; day 5 sells 39 at $12 less $1.
        buy,sell=r['trades']
        self.assertEqual(buy['shares'],39)
        self.assertEqual(buy['fill_bar_date'],dt.date(2026,7,3))
        self.assertEqual(buy['sleeve_cash_after'],D(24))
        self.assertEqual(r['orders'][0]['requested_shares'],49)
        self.assertEqual(r['orders'][0]['status'],'partial_remainder_cancelled')
        self.assertEqual(sell['shares'],39)
        self.assertEqual(r['summary']['final_equity'],D(491))
        self.assertEqual(r['daily'][-1]['realized_pnl'],D(-509))
        self.assertEqual(r['daily'][-1]['unrealized_pnl'],D(0))

    def test_slippage_charged_once_and_fee_in_basis(self):
        r=simulate(bars([10,20,20]),config(slippage_bps=D(100)))
        fill=r['trades'][0]
        self.assertEqual(fill['shares'],49)
        self.assertEqual(fill['fill_price'],D('20.2'))
        self.assertEqual(r['summary']['final_equity'],D('989.2'))
        self.assertEqual(r['summary']['slippage_cost'],D('9.8'))

    def test_signal_day_fill_and_final_forced_exit_absent(self):
        r=simulate(bars([10,20]),config())
        self.assertEqual(r['trades'],[])
        self.assertEqual(r['summary']['pending_orders'],1)
        r=simulate(bars([10,20,21]),config())
        self.assertGreater(r['summary']['final_shares']['AAPL'],0)
        self.assertEqual(len(r['trades']),1)

    def test_future_data_does_not_change_past_ledger(self):
        sample=bars([10,20,25,15,12,30,11])
        full=simulate(sample,config())
        prefix=simulate(sample[:5],config())
        altered=simulate(bars([10,20,25,15,12,999,999]),config())
        self.assertEqual(prefix['daily'],full['daily'][:5])
        self.assertEqual(altered['daily'][:5],full['daily'][:5])
        self.assertEqual(prefix['trades'],[t for t in full['trades'] if t['fill_bar_date']<=dt.date(2026,7,5)])

    def test_empty_insufficient_cash_and_missing_calendar(self):
        with self.assertRaises(ValueError):
            simulate([],config())
        r=simulate(bars([10,20,100]),config(initial_cash=D(1)))
        self.assertEqual(r['trades'],[])
        a,b=bars([10,20]),bars([10],'MSFT')
        with self.assertRaises(ValueError):
            simulate(sorted(a+b,key=lambda x:(x.bar_start_utc,x.symbol)),config())

    def test_equal_sleeves_independent_and_cash_nonnegative(self):
        a,b=bars([10,20,25,15,12]),bars([10,10,10,10,10],'MSFT')
        r=simulate(sorted(a+b,key=lambda x:(x.bar_start_utc,x.symbol)),config())
        self.assertEqual(r['summary']['initial_allocations'],{'AAPL':D(500),'MSFT':D(500)})
        self.assertTrue(all(p['cash']>=0 and p['shares']>=0 for p in r['positions']))
        self.assertEqual([p for p in r['positions'] if p['symbol']=='MSFT'][-1]['cash'],D(500))


if __name__=='__main__':
    unittest.main()
