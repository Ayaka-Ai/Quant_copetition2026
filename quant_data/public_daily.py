"""Read-only Yahoo chart normalization. Never certifies V1 trading readiness.

Provider field names are retained: Yahoo OHLC must not be relabelled historical
unadjusted share prices. Retrieval time is the first local evidence of knowledge,
not a historical publication timestamp. Nulls stay null; no filling or repair.
"""
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path
from typing import Optional, Tuple

from .daily import DataContractError
from .us_calendar import exchange_sessions
from quant_portfolio.momentum import DataReadiness
from quant_signals.momentum import MomentumObservation


@dataclass(frozen=True)
class PublicDailyBar:
    symbol: str
    session_date: dt.date
    provider_timestamp: dt.datetime
    yahoo_open: Optional[D]
    yahoo_high: Optional[D]
    yahoo_low: Optional[D]
    yahoo_close: Optional[D]
    yahoo_adjusted_close: Optional[D]
    yahoo_volume: Optional[int]


@dataclass(frozen=True)
class PublicDataset:
    symbol: str
    bars: Tuple[PublicDailyBar, ...]
    events: tuple
    retrieved_at: Optional[dt.datetime]
    sha256: str


PUBLIC_DATA_GAPS = {
    'regular_session_verified': 'Daily chart timestamps match exchange dates, but exact OHLC trade-condition/session construction is not independently verified.',
    'point_in_time_adjustment_verified': 'Latest adjusted history has no historical revision/announcement archive; event dates are not publication dates.',
    'unadjusted_share_prices_verified': 'Yahoo OHLC may be split adjusted; historical actual-share-unit prices have not been verified.',
    'consolidated_turnover_verified': 'Response has daily volume, not exact consolidated regular-session sum(price * shares). close * volume is not substituted.',
    'publication_times_verified': 'Only local retrieval is recorded. Historical availability by close + 30 minutes is unknown.',
}


def public_data_readiness():
    """Source capabilities cannot be promoted by successful structural checks."""
    return DataReadiness()


def _price(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise DataContractError('Boolean price')
    result = D(str(value))
    if not result.is_finite() or result <= 0:
        raise DataContractError('Expected positive finite price or null')
    return result


def _stamp(value):
    if type(value) is not int:
        raise DataContractError('Expected integer Unix timestamp')
    return dt.datetime.fromtimestamp(value, dt.timezone.utc)


def _events(result, symbol, sessions):
    events = []
    for kind, records in result.get('events', {}).items():
        if kind not in ('dividends', 'splits', 'capitalGains'):
            raise DataContractError('Unsupported corporate action type')
        for event_id, record in records.items():
            event_stamp = _stamp(record['date'])
            if event_stamp.date() not in sessions:
                raise DataContractError('Corporate action outside response session range')
            if kind == 'splits':
                if _price(record['numerator']) is None or _price(record['denominator']) is None:
                    raise DataContractError('Missing split ratio')
            elif _price(record['amount']) is None:
                raise DataContractError('Missing distribution amount')
            events.append(dict(symbol=symbol, kind=kind, source_event_id=str(event_id),
                provider_event_time=event_stamp, provider_fields=record, announcement_time=None, pay_date=None))
    events.sort(key=lambda e: (e['provider_event_time'], e['kind']))
    return tuple(events)


def parse_yahoo_events(raw, symbol, start, end):
    """Validate event records independently when the daily-price contract fails.

    Event dates must fit the explicit request calendar, not the observed bar
    span. Successful extraction never certifies price data or action semantics.
    """
    try:
        chart = json.loads(raw, parse_float=D)['chart']
        if chart.get('error') is not None or not chart.get('result') or len(chart['result']) != 1:
            raise DataContractError('No single Yahoo result for corporate events')
        result = chart['result'][0]
        meta = result['meta']
        if (meta['symbol'] != symbol.replace('.', '-') or meta['currency'] != 'USD'
                or meta['exchangeTimezoneName'] != 'America/New_York'):
            raise DataContractError('Unexpected corporate-event symbol/currency/timezone')
        return _events(result, symbol, {s.session_date for s in exchange_sessions(start,end)})
    except DataContractError:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, OSError) as exc:
        raise DataContractError('Invalid Yahoo event response: '+str(exc)) from exc


def parse_yahoo_chart(raw, symbol, *, retrieved_at=None):
    """Validate a complete provider response before selecting any date window.

    This bounded adapter supports 2024--2026 histories only. Missing OHLC,
    adjusted prices and volume are preserved for audit. API errors are failures.
    """
    try:
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
            raise DataContractError('Invalid symbol')
        if retrieved_at is not None and (not isinstance(retrieved_at, dt.datetime)
                                         or retrieved_at.utcoffset() is None):
            raise DataContractError('Retrieval time must be timezone-aware')
        chart = json.loads(raw, parse_float=D)['chart']
        if chart.get('error') is not None or not chart.get('result'):
            raise DataContractError('Yahoo returned an error or empty result')
        if len(chart['result']) != 1:
            raise DataContractError('Expected exactly one result')
        result = chart['result'][0]
        meta = result['meta']
        if (meta['symbol'] != symbol.replace('.', '-') or meta['currency'] != 'USD'
                or meta['exchangeTimezoneName'] != 'America/New_York'
                or meta['dataGranularity'] != '1d'):
            raise DataContractError('Unexpected symbol/currency/timezone/granularity')
        stamps = result.get('timestamp') or []
        if not stamps:
            raise DataContractError('No historical timestamps')
        times = [_stamp(s) for s in stamps]
        if any(a >= b for a, b in zip(times, times[1:])):
            raise DataContractError('Duplicate or unordered timestamps')
        sessions = {s.session_date: s for s in exchange_sessions(times[0].date(), times[-1].date()+dt.timedelta(days=1))}
        indicators = result['indicators']
        if len(indicators['quote']) != 1 or len(indicators['adjclose']) != 1:
            raise DataContractError('Expected one quote/adjusted-price array')
        arrays = dict(indicators['quote'][0], adjclose=indicators['adjclose'][0]['adjclose'])
        if any(len(arrays[k]) != len(times) for k in ('open', 'high', 'low', 'close', 'volume', 'adjclose')):
            raise DataContractError('Unequal column lengths')
        bars = []
        for i, stamp in enumerate(times):
            session = sessions.get(stamp.date())
            if session is None or stamp != session.open_time:
                raise DataContractError('Daily timestamp does not match reviewed exchange session open')
            if retrieved_at is not None and session.close_time > retrieved_at:
                raise DataContractError('Unfinished session in historical cache')
            prices = [_price(arrays[k][i]) for k in ('open', 'high', 'low', 'close', 'adjclose')]
            o, h, l, c, _ = prices
            if all(p is not None for p in (o, h, l, c)) and not l <= min(o, c) <= max(o, c) <= h:
                raise DataContractError('Inconsistent OHLC bounds')
            volume = arrays['volume'][i]
            if volume is not None and (type(volume) is not int or volume < 0):
                raise DataContractError('Invalid volume')
            bars.append(PublicDailyBar(symbol, stamp.date(), stamp, *prices, volume))
        return PublicDataset(symbol, tuple(bars), _events(result, symbol, sessions),
                             retrieved_at, hashlib.sha256(raw).hexdigest())
    except DataContractError:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, OSError) as exc:
        raise DataContractError('Invalid Yahoo daily response: ' + str(exc)) from exc


def load_public_cache(directory):
    """Require hash-pinned, dated manifest; reject missing/changed response files."""
    root = Path(directory)
    try:
        manifest = json.loads((root / 'manifest.json').read_text())
        if manifest['schema_version'] != 'yahoo_public_daily_v1' or manifest['source'] != 'Yahoo Finance chart':
            raise DataContractError('Unsupported public cache manifest')
        start, end = [dt.date.fromisoformat(manifest[k]) for k in ('start', 'end_exclusive')]
        exchange_sessions(start, end)  # Validate bounded coverage independently of rows.
        symbols, result = set(), []
        if not manifest['responses']:
            raise DataContractError('Empty cache manifest')
        for row in manifest['responses']:
            symbol = row['symbol']
            if symbol in symbols:
                raise DataContractError('Duplicate manifest symbol')
            symbols.add(symbol)
            filename = row['file']
            if Path(filename).name != filename or not filename.endswith('.json'):
                raise DataContractError('Response must be a local filename')
            raw = (root / filename).read_bytes()
            if hashlib.sha256(raw).hexdigest() != row['sha256'] or len(raw) != row['bytes']:
                raise DataContractError('Public cache SHA256 or size mismatch')
            stamp = dt.datetime.fromisoformat(row['retrieved_at_utc'])
            data = parse_yahoo_chart(raw, symbol, retrieved_at=stamp)
            if any(not start <= b.session_date < end for b in data.bars):
                raise DataContractError('Bar outside declared request window')
            result.append(data)
        return tuple(sorted(result, key=lambda d: d.symbol))
    except DataContractError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise DataContractError('Invalid public cache: ' + str(exc)) from exc


def public_observations(dataset):
    """Expose only what is known at the actual retrieval time, with gaps explicit.

    These observations cannot produce V1 targets: raw share prices, exact
    turnover, trading permissions and corporate-action clearance remain unknown.
    """
    if dataset.retrieved_at is None:
        raise DataContractError('Undated legacy cache cannot establish availability')
    return tuple(MomentumObservation(b.symbol, b.session_date, dataset.retrieved_at,
                 b.yahoo_adjusted_close, None, None) for b in dataset.bars)


def audit_public_dataset(dataset, start, end):
    expected = {s.session_date for s in exchange_sessions(start, end)}
    actual = {b.session_date for b in dataset.bars if start <= b.session_date < end}
    fields = ('yahoo_open', 'yahoo_high', 'yahoo_low', 'yahoo_close', 'yahoo_adjusted_close', 'yahoo_volume')
    nulls = {k: sum(getattr(b, k) is None for b in dataset.bars) for k in fields}
    zero_volume = [b.session_date for b in dataset.bars if b.yahoo_volume == 0]
    return dict(symbol=dataset.symbol, rows=len(dataset.bars), first_date=dataset.bars[0].session_date,
                last_date=dataset.bars[-1].session_date, expected_sessions=len(expected),
                missing_sessions=sorted(expected - actual), unexpected_sessions=sorted(actual - expected),
                null_counts=nulls, zero_volume_sessions=zero_volume,
                corporate_events=len(dataset.events), retrieved_at=dataset.retrieved_at, sha256=dataset.sha256,
                structural_pass=not (expected - actual) and not any(nulls.values()) and not zero_volume,
                strategy_ready=False, missing_checks=PUBLIC_DATA_GAPS)
