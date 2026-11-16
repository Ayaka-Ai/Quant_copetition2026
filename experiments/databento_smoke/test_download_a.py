import datetime as dt
import unittest
from download_a import parse_bar_timestamp


class TimestampTests(unittest.TestCase):
    def test_vendor_nanoseconds(self):
        self.assertEqual(parse_bar_timestamp('2026-09-14T00:00:00.000000000Z'),
                         dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc))

    def test_no_rounding_into_next_day(self):
        result = parse_bar_timestamp('2026-09-14T23:59:59.999999999Z')
        self.assertEqual(result.date(), dt.date(2026, 9, 14))
        self.assertEqual(result.microsecond, 999999)

    def test_offsets_and_fractions(self):
        for fraction in ['', '.1', '.123', '.123456', '.123456789']:
            result = parse_bar_timestamp('2026-09-14T23:30:00' + fraction + '-04:00')
            self.assertEqual(result.date(), dt.date(2026, 9, 15))
            self.assertEqual(result.hour, 3)

    def test_epoch_nanoseconds(self):
        self.assertEqual(parse_bar_timestamp('1000000001'),
                         dt.datetime(1970, 1, 1, 0, 0, 1, tzinfo=dt.timezone.utc))

    def test_reject_naive_timestamp(self):
        with self.assertRaises(ValueError):
            parse_bar_timestamp('2026-09-14T00:00:00')


if __name__ == '__main__':
    unittest.main()
