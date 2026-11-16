"""Bounded, source-listed NYSE cash-equity schedule for 2024--2026.

Calendar boundaries are independent of price observations. No publication time
is inferred here. Update the source table before using any other year.
"""
import datetime as dt
from dataclasses import dataclass

SOURCES = (
    'https://ir.theice.com/press/news-details/2023/NYSE-Group-Announces-2024-2025-and-2026-Holiday-and-Early-Closings-Calendar/default.aspx',
    'https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx',
    'https://www.nyse.com/trade/hours-calendars',
)
HOLIDAYS = {
    2024: '01-01 01-15 02-19 03-29 05-27 06-19 07-04 09-02 11-28 12-25',
    2025: '01-01 01-09 01-20 02-17 04-18 05-26 06-19 07-04 09-01 11-27 12-25',
    2026: '01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25',
}
EARLY_CLOSES = {2024: '07-03 11-29 12-24', 2025: '07-03 11-28 12-24', 2026: '11-27 12-24'}
# Explicit local dates on which US daylight time starts/ends in these years.
DST = {2024: ('03-10', '11-03'), 2025: ('03-09', '11-02'), 2026: ('03-08', '11-01')}


@dataclass(frozen=True)
class ExchangeSession:
    session_date: dt.date
    open_time: dt.datetime
    close_time: dt.datetime


def exchange_sessions(start, end):
    """Return complete scheduled sessions in [start, end), both date objects.

    Times are UTC, incorporating each year's explicitly bounded DST dates.
    These are scheduled exchange hours, not observed feed publication times.
    """
    if type(start) is not dt.date or type(end) is not dt.date or start >= end:
        raise ValueError('Require date objects with start < exclusive end')
    if start < dt.date(2024, 1, 1) or end > dt.date(2027, 1, 1):
        raise ValueError('Calendar only reviewed for 2024--2026')
    result, day = [], start
    while day < end:
        md = day.strftime('%m-%d')
        if day.weekday() < 5 and md not in HOLIDAYS[day.year].split():
            dst_start, dst_end = DST[day.year]
            offset = 4 if dst_start <= md < dst_end else 5
            close_hour = 13 if md in EARLY_CLOSES[day.year].split() else 16
            result.append(ExchangeSession(day,
                dt.datetime.combine(day, dt.time(9 + offset, 30), dt.timezone.utc),
                dt.datetime.combine(day, dt.time(close_hour + offset), dt.timezone.utc)))
        day += dt.timedelta(days=1)
    return tuple(result)
