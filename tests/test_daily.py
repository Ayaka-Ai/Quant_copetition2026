import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from quant_data import DataContractError, load_daily_bars


class DailyContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rows = [dict(ts_event=d+'T00:00:00.000000000Z', instrument_id='38',
                          open='10.125', high='12', low='9', close='11', volume='100')
                     for d in ['2026-09-16', '2026-09-14']]
        self.mapping = {'result': {'AAPL': [{'s': '38', 'd0': '2026-09-14', 'd1': '2026-09-17'}]}}
        self.save()

    def save(self):
        f = io.StringIO()
        w = csv.DictWriter(f, fieldnames=list(self.rows[0]))
        w.writeheader(); w.writerows(self.rows)
        raw = f.getvalue().encode()
        (self.root/'ohlcv.csv').write_bytes(raw)
        (self.root/'mapping.json').write_text(json.dumps(self.mapping))
        manifest = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                        params=dict(dataset='EQUS.MINI', schema='ohlcv-1d', symbols='AAPL',
                                    stype_in='raw_symbol', stype_out='instrument_id',
                                    start='2026-09-14T00:00:00Z', end='2026-09-17T00:00:00Z'))
        (self.root/'manifest.json').write_text(json.dumps(manifest))

    def test_contract_sort_precision_and_no_gap_fill(self):
        data = load_daily_bars(self.root)
        self.assertEqual([str(b.provider_bar_date) for b in data.bars], ['2026-09-14', '2026-09-16'])
        bar = data.bars[0]
        self.assertEqual(bar.open, Decimal('10.125'))
        self.assertEqual(str(bar.bar_end_utc), '2026-09-15 00:00:00+00:00')
        self.assertEqual(data.info.session, 'UTC_calendar_day')
        self.assertEqual(data.info.price_adjustment, 'unadjusted')

    def test_exclusive_end_and_empty_selection(self):
        self.assertEqual(len(load_daily_bars(self.root, start='2026-09-14', end='2026-09-16').bars), 1)
        self.assertEqual(load_daily_bars(self.root, start='2026-10-01').bars, ())

    def test_unknown_symbol_rejected(self):
        with self.assertRaises(DataContractError):
            load_daily_bars(self.root, symbols=['SPY'])

    def test_tamper_rejected(self):
        with (self.root/'ohlcv.csv').open('ab') as f:
            f.write(b'\n')
        with self.assertRaisesRegex(DataContractError, 'SHA256'):
            load_daily_bars(self.root)

    def test_duplicate_rejected_even_outside_filter(self):
        self.rows.append(self.rows[0].copy()); self.save()
        with self.assertRaisesRegex(DataContractError, 'Duplicate'):
            load_daily_bars(self.root, end='2026-09-15')

    def test_mapping_end_exclusive(self):
        self.mapping['result']['AAPL'][0]['d1'] = '2026-09-16'; self.save()
        with self.assertRaisesRegex(DataContractError, 'mapping'):
            load_daily_bars(self.root)

    def test_invalid_prices_and_timestamps(self):
        for field, value in [('high', '8'), ('close', 'NaN'), ('volume', '-1'),
                             ('ts_event', '2026-09-16T00:00:00.000000001Z')]:
            with self.subTest(field=field):
                old = self.rows[0][field]
                self.rows[0][field] = value; self.save()
                with self.assertRaises(DataContractError):
                    load_daily_bars(self.root)
                self.rows[0][field] = old


if __name__ == '__main__':
    unittest.main()
