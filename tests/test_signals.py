import datetime as dt
from decimal import Decimal
import unittest
from quant_data import DailyBar
from quant_signals import moving_average_signals


def bars(prices, symbol='AAPL'):
    result = []
    for i, price in enumerate(prices):
        start = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)+dt.timedelta(days=i)
        p = Decimal(str(price))
        result.append(DailyBar(symbol, start.date(), start, start+dt.timedelta(days=1), p,p,p,p,100))
    return result


class SignalTests(unittest.TestCase):
    def test_hand_calculation_warmup_and_flat(self):
        result = moving_average_signals(bars([1,2,3,1,1]), short_window=2, long_window=3)
        self.assertEqual([r.target_position for r in result], [None,None,1,0,0])
        self.assertIsNone(result[0].short_ma)
        self.assertEqual(result[1].short_ma, Decimal('1.5'))
        self.assertEqual(result[2].long_ma, Decimal('2'))
        self.assertEqual(result[2].signal_not_before_utc, result[2].bar_end_utc)

    def test_equal_means_are_flat(self):
        self.assertEqual(moving_average_signals(bars([10]*20))[-1].target_position, 0)

    def test_future_append_and_changes_preserve_past(self):
        base = bars(range(1,41))
        changed = bars(list(range(1,26))+[999]*15)
        full = moving_average_signals(base)
        self.assertEqual(moving_average_signals(base[:25]), full[:25])
        self.assertEqual(moving_average_signals(changed)[:25], full[:25])

    def test_symbols_do_not_share_history(self):
        a, b = bars(range(1,26)), bars(range(100,75,-1), 'MSFT')
        mixed = sorted(a+b, key=lambda r: (r.bar_start_utc,r.symbol))
        result = moving_average_signals(mixed)
        self.assertEqual(tuple(r for r in result if r.symbol=='AAPL'), moving_average_signals(a))
        self.assertEqual(tuple(r for r in result if r.symbol=='MSFT'), moving_average_signals(b))
        self.assertEqual(result[-1].target_position, 0)

    def test_reject_order_duplicate_and_bad_windows(self):
        sample = bars([1,2,3])
        for sequence in [sample[::-1], sample+[sample[-1]]]:
            with self.assertRaises(ValueError):
                moving_average_signals(sequence)
        for short,long in [(0,20),(20,20),(5,2),(True,20),(2,3.5)]:
            with self.assertRaises(ValueError):
                moving_average_signals(sample, short_window=short, long_window=long)

    def test_gaps_not_filled(self):
        result = moving_average_signals(bars(range(1,22))[::2])
        self.assertEqual(len(result),11)
        self.assertTrue(all(r.target_position is None for r in result))


if __name__ == '__main__':
    unittest.main()
