"""Databento cache adapter. No downloads, resampling, filling or adjustment."""
import csv
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Optional, Sequence, Tuple

UTC = dt.timezone.utc


class DataContractError(ValueError):
    """Input cannot safely be represented by this data contract."""


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    provider_bar_date: dt.date
    bar_start_utc: dt.datetime
    bar_end_utc: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class DatasetInfo:
    contract_version: str
    source: str
    dataset: str
    schema: str
    currency: str
    price_adjustment: str
    session: str
    volume_scope: str
    availability: str
    source_start_utc: dt.datetime
    source_end_utc: dt.datetime
    source_sha256: str


@dataclass(frozen=True)
class BarDataset:
    info: DatasetInfo
    bars: Tuple[DailyBar, ...]


def _timestamp(value):
    # This adapter supports midnight UTC daily bars only. Validate before
    # parsing so nanoseconds are never silently rounded across a boundary.
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T00:00:00(?:\.0{1,9})?(?:Z|\+00:00)', value):
        raise DataContractError('Expected a midnight UTC daily-bar timestamp: ' + value)
    return dt.datetime.combine(dt.date.fromisoformat(value[:10]), dt.time(), UTC)


def _date(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Filters must use YYYY-MM-DD strings')
    return dt.date.fromisoformat(value)


def load_daily_bars(sample_dir, *, symbols: Optional[Sequence[str]] = None,
                    start: Optional[str] = None, end: Optional[str] = None) -> BarDataset:
    """Load an audited EQUS.MINI cache; filter dates as [start, end).

    Validate the entire source before filtering. Return immutable bars sorted
    by (bar_start_utc, symbol). Missing sessions remain missing. bar_end_utc is
    an interval boundary, NOT a verified publication/availability timestamp.
    """
    lo, hi = _date(start), _date(end)
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError('start must precede end (end is exclusive)')
    if isinstance(symbols, str):
        raise ValueError('symbols must be a sequence, e.g. ["AAPL"]')
    wanted = None if symbols is None else set(symbols)
    if wanted is not None and (not wanted or any(not isinstance(s, str) or not s for s in wanted)):
        raise ValueError('symbols must contain nonempty symbol strings')
    root = Path(sample_dir)
    try:
        manifest = json.loads((root / 'manifest.json').read_text())
        mapping = json.loads((root / 'mapping.json').read_text())
        raw = (root / 'ohlcv.csv').read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != manifest['sha256'] or len(raw) != manifest['bytes']:
            raise DataContractError('Raw cache size or SHA256 differs from manifest')
        p = manifest['params']
        if (p['dataset'], p['schema'], p['stype_in'], p['stype_out']) != (
                'EQUS.MINI', 'ohlcv-1d', 'raw_symbol', 'instrument_id'):
            raise DataContractError('Unsupported dataset/schema/symbology; use a separate adapter')
        source_start, source_end = _timestamp(p['start']), _timestamp(p['end'])
        if source_start >= source_end:
            raise DataContractError('Invalid source interval')
        declared = set(p['symbols'].split(','))
        if wanted is not None and not wanted.issubset(declared):
            raise DataContractError('Unknown requested symbols: ' + str(sorted(wanted - declared)))
        if mapping.get('partial') or mapping.get('not_found'):
            raise DataContractError('Incomplete source symbol mapping')
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8')))
        fields = {'ts_event', 'instrument_id', 'open', 'high', 'low', 'close', 'volume'}
        if not fields.issubset(reader.fieldnames or []):
            raise DataContractError('Missing required CSV fields')
        bars, seen = [], set()
        for line, row in enumerate(reader, 2):
            stamp = _timestamp(row['ts_event'])
            date = stamp.date()
            if not source_start <= stamp < source_end:
                raise DataContractError('Bar outside source range at row ' + str(line))
            matches = [s for s, intervals in mapping['result'].items() for v in intervals
                       if str(v['s']) == row['instrument_id']
                       and v['d0'] <= date.isoformat() < v['d1']]
            if len(matches) != 1 or matches[0] not in declared:
                raise DataContractError('Missing/ambiguous mapping at row ' + str(line))
            symbol = matches[0]
            key = (stamp, symbol)
            if key in seen:
                raise DataContractError('Duplicate bar at row ' + str(line))
            seen.add(key)
            o, h, l, c = [Decimal(row[k]) for k in ['open', 'high', 'low', 'close']]
            if not all(x.is_finite() and x > 0 for x in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
                raise DataContractError('Invalid OHLC at row ' + str(line))
            if not re.fullmatch(r'\d+', row['volume']) or int(row['volume']) <= 0:
                raise DataContractError('Invalid volume at row ' + str(line))
            bars.append(DailyBar(symbol, date, stamp, stamp + dt.timedelta(days=1),
                                 o, h, l, c, int(row['volume'])))
        if not bars or {b.symbol for b in bars} != declared:
            raise DataContractError('Empty source or missing declared symbol')
    except DataContractError:
        raise
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise DataContractError('Invalid local cache: ' + str(exc)) from exc
    selected = tuple(sorted((b for b in bars if
                             (wanted is None or b.symbol in wanted)
                             and (lo is None or b.provider_bar_date >= lo)
                             and (hi is None or b.provider_bar_date < hi)),
                            key=lambda b: (b.bar_start_utc, b.symbol)))
    info = DatasetInfo('1.0', 'Databento', p['dataset'], p['schema'], 'USD',
                       'unadjusted', 'UTC_calendar_day', 'component_venues',
                       'publication_time_unknown; not before bar_end_utc',
                       source_start, source_end, digest)
    return BarDataset(info, selected)
