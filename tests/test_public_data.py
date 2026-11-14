import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal as D

from quant_backtest import TradingSession
from quant_data import DataContractError
from quant_data.public_daily import (audit_public_dataset, load_public_cache, parse_yahoo_chart,
                                     public_data_readiness, public_observations)
from quant_data.us_calendar import exchange_sessions
from quant_portfolio import MomentumCalendar, UniverseMember, build_momentum_targets
from quant_signals.momentum import momentum_snapshot

UTC = dt.timezone.utc


class ExchangeCalendarTest(unittest.TestCase):
    def test_published_holidays_extra_closure_and_year_bound(self):
        rows = exchange_sessions(dt.date(2025, 1, 1), dt.date(2025, 1, 11))
        self.assertEqual([r.session_date.day for r in rows], [2, 3, 6, 7, 8, 10])
        with self.assertRaisesRegex(ValueError, '2024--2026'):
            exchange_sessions(dt.date(2023, 12, 31), dt.date(2024, 1, 2))
        with self.assertRaises(ValueError):
            exchange_sessions(dt.date(2026, 1, 1), dt.date(2026, 1, 1))

    def test_early_closes_and_no_july_2026_early_close(self):
        dates = [(dt.date(2025, 7, 3), 17), (dt.date(2025, 11, 28), 18),
                 (dt.date(2026, 12, 24), 18), (dt.date(2026, 7, 2), 20)]
        for day, hour in dates:
            row = exchange_sessions(day, day + dt.timedelta(days=1))[0]
            self.assertEqual(row.close_time.hour, hour)
        self.assertEqual(exchange_sessions(dt.date(2026, 7, 3), dt.date(2026, 7, 4)), ())

    def test_dst_transitions_and_complete_calendar_independent_of_prices(self):
        spring = exchange_sessions(dt.date(2025, 3, 7), dt.date(2025, 3, 11))
        autumn = exchange_sessions(dt.date(2026, 10, 30), dt.date(2026, 11, 3))
        self.assertEqual([r.open_time.hour for r in spring], [14, 13])
        self.assertEqual([r.close_time.hour for r in autumn], [20, 21])
        self.assertEqual([len(exchange_sessions(dt.date(y, 1, 1), dt.date(y+1, 1, 1)))
                          for y in (2024, 2025, 2026)], [252, 250, 251])


class PublicDataTest(unittest.TestCase):
    def setUp(self):
        self.days = [dt.date(2026, 11, d) for d in (2, 3, 4)]
        self.stamps = [int(s.open_time.timestamp()) for s in exchange_sessions(self.days[0], dt.date(2026, 11, 5))]
        self.retrieved = dt.datetime(2026, 11, 5, 1, tzinfo=UTC)
        self.result = dict(meta=dict(symbol='AAA', currency='USD', exchangeTimezoneName='America/New_York', dataGranularity='1d'),
            timestamp=self.stamps, indicators=dict(quote=[dict(open=[10, 11, 12], high=[11, 12, 13],
                low=[9, 10, 11], close=[10.25, 11.25, 12.25], volume=[100, 200, 300])],
                adjclose=[dict(adjclose=[9.75, 10.75, 11.75])]),
            events=dict(dividends={'x': dict(date=self.stamps[1], amount=0.25)}))

    def raw(self):
        return json.dumps(dict(chart=dict(result=[self.result], error=None))).encode()

    def parse(self, **kwargs):
        return parse_yahoo_chart(self.raw(), 'AAA', **kwargs)

    def test_numeric_precision_names_and_events_not_pay_dates(self):
        data = self.parse(retrieved_at=self.retrieved)
        self.assertEqual(data.bars[0].yahoo_close, D('10.25'))
        self.assertEqual(data.bars[0].yahoo_adjusted_close, D('9.75'))
        self.assertEqual(data.events[0]['provider_fields']['amount'], D('.25'))
        self.assertIsNone(data.events[0]['announcement_time'])
        self.assertIsNone(data.events[0]['pay_date'])
        self.assertFalse(hasattr(data.bars[0], 'unadjusted_close'))

    def test_no_filling_and_calendar_gap_detection(self):
        self.result['timestamp'].pop(1)
        arrays = self.result['indicators']['quote'][0]
        for values in arrays.values():
            values.pop(1)
        self.result['indicators']['adjclose'][0]['adjclose'].pop(1)
        data = self.parse()
        audit = audit_public_dataset(data, self.days[0], dt.date(2026, 11, 5))
        self.assertEqual(len(data.bars), 2)
        self.assertEqual(audit['missing_sessions'], [self.days[1]])
        self.assertFalse(audit['structural_pass'])

    def test_nulls_and_zero_volume_are_reported(self):
        self.result['indicators']['adjclose'][0]['adjclose'][1] = None
        self.result['indicators']['quote'][0]['volume'][0] = 0
        data = self.parse()
        audit = audit_public_dataset(data, self.days[0], dt.date(2026, 11, 5))
        self.assertIsNone(data.bars[1].yahoo_adjusted_close)
        self.assertEqual(audit['null_counts']['yahoo_adjusted_close'], 1)
        self.assertEqual(audit['zero_volume_sessions'], [self.days[0]])
        self.assertFalse(audit['structural_pass'])

    def test_unknowns_and_retrieval_time_not_backdated(self):
        data = self.parse(retrieved_at=self.retrieved)
        observations = public_observations(data)
        self.assertTrue(all(o.available_at == self.retrieved for o in observations))
        self.assertTrue(all(o.unadjusted_close is None and o.turnover_usd is None
                            and o.permission is None and o.corporate_actions_clear is None for o in observations))
        before = momentum_snapshot(observations, self.days, self.retrieved-dt.timedelta(seconds=1),
                                   ['AAA'], lookback=2, turnover_window=2)[0]
        self.assertIsNone(before.momentum)
        at = momentum_snapshot(observations, self.days, self.retrieved, ['AAA'], lookback=2, turnover_window=2)[0]
        self.assertEqual(at.momentum, D('11.75') / D('9.75') - 1)
        self.assertIsNone(at.average_turnover_usd)
        with self.assertRaisesRegex(DataContractError, 'Undated'):
            public_observations(self.parse())

    def test_duplicate_length_timezone_symbol_and_unfinished_rejected(self):
        original = copy.deepcopy(self.result)
        mutations = [lambda r: r['timestamp'].__setitem__(1, r['timestamp'][0]),
                     lambda r: r['indicators']['quote'][0]['open'].pop(),
                     lambda r: r['meta'].__setitem__('exchangeTimezoneName', 'UTC'),
                     lambda r: r['meta'].__setitem__('symbol', 'BBB'),
                     lambda r: r['timestamp'].__setitem__(0, r['timestamp'][0]+60)]
        for mutation in mutations:
            self.result = copy.deepcopy(original)
            mutation(self.result)
            with self.assertRaises(DataContractError):
                self.parse()
        self.result = original
        with self.assertRaisesRegex(DataContractError, 'Unfinished'):
            self.parse(retrieved_at=dt.datetime(2026, 11, 4, 20, tzinfo=UTC))

    def test_bad_prices_volume_and_events_rejected(self):
        original = copy.deepcopy(self.result)
        mutations = [lambda r: r['indicators']['quote'][0]['close'].__setitem__(0, float('nan')),
                     lambda r: r['indicators']['quote'][0]['high'].__setitem__(0, 8),
                     lambda r: r['indicators']['quote'][0]['volume'].__setitem__(0, True),
                     lambda r: r['events']['dividends']['x'].__setitem__('amount', -1),
                     lambda r: r['events'].__setitem__('unknown', {})]
        for mutation in mutations:
            self.result = copy.deepcopy(original)
            mutation(self.result)
            with self.assertRaises(DataContractError):
                self.parse()

    def test_api_empty_error_and_missing_arrays_rejected(self):
        for chart in (dict(result=None, error=dict(code='Not Found')), dict(result=[], error=None)):
            with self.assertRaises(DataContractError):
                parse_yahoo_chart(json.dumps(dict(chart=chart)).encode(), 'AAA')
        self.result['indicators'].pop('adjclose')
        with self.assertRaises(DataContractError):
            self.parse()

    def cache(self, root):
        raw = self.raw()
        (root / 'AAA.json').write_bytes(raw)
        m = dict(schema_version='yahoo_public_daily_v1', source='Yahoo Finance chart',
                 start='2026-11-02', end_exclusive='2026-11-05', responses=[dict(symbol='AAA', file='AAA.json',
                     sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), retrieved_at_utc=self.retrieved.isoformat())])
        (root / 'manifest.json').write_text(json.dumps(m))
        return m

    def test_manifest_hash_date_window_and_path_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.cache(root)
            self.assertEqual(len(load_public_cache(root)[0].bars), 3)
            (root / 'AAA.json').write_bytes(self.raw()+b' ')
            with self.assertRaisesRegex(DataContractError, 'SHA256'):
                load_public_cache(root)
            m = self.cache(root)
            m['end_exclusive'] = '2026-11-04'
            (root / 'manifest.json').write_text(json.dumps(m))
            with self.assertRaisesRegex(DataContractError, 'window'):
                load_public_cache(root)
            m = self.cache(root)
            m['responses'][0]['file'] = '../AAA.json'
            (root / 'manifest.json').write_text(json.dumps(m))
            with self.assertRaisesRegex(DataContractError, 'local filename'):
                load_public_cache(root)

    def test_public_data_gate_produces_no_liquidation_or_targets(self):
        data = self.parse(retrieved_at=self.retrieved)
        sessions = tuple(TradingSession(s.session_date, s.open_time, s.close_time,
                         max(s.close_time, self.retrieved))
                         for s in exchange_sessions(dt.date(2026, 11, 2), dt.date(2026, 11, 9)))
        calendar = MomentumCalendar(sessions, dt.date(2026, 11, 2), dt.date(2026, 11, 8))
        result = build_momentum_targets(calendar, public_observations(data),
                 [UniverseMember('AAA', 'Tech', '1')], public_data_readiness(),
                 as_of=dt.datetime(2026, 11, 9, tzinfo=UTC))
        self.assertEqual(result['targets'], ())
        self.assertEqual(result['decisions'][0]['status'], 'data_not_ready')
        self.assertEqual(len(result['missing_checks']), 5)


if __name__ == '__main__':
    unittest.main()
