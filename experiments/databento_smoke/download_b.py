"""Download the authorized B sample once, then validate locally (Python 3.8+)."""
import csv
import datetime as dt
from decimal import Decimal
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
from download_a import post, parse_bar_timestamp

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'b'
SYMBOLS = ['AAPL', 'MSFT', 'SPY']
PARAMS = dict(dataset='EQUS.MINI', symbols=','.join(SYMBOLS), schema='ohlcv-1d',
              stype_in='raw_symbol', stype_out='instrument_id',
              start='2026-07-01T00:00:00Z', end='2026-10-01T00:00:00Z')
QUOTE_LIMIT = Decimal('0.00030040741')
# Fixed sample only; official calendar: https://nasdaqtrader.com/Trader.aspx?id=Calendar
HOLIDAYS = {'2026-07-03', '2026-09-07'}
EXPECTED = {(dt.date(2026, 7, 1) + dt.timedelta(days=i)).isoformat()
            for i in range(92)
            if (dt.date(2026, 7, 1) + dt.timedelta(days=i)).weekday() < 5
            and (dt.date(2026, 7, 1) + dt.timedelta(days=i)).isoformat() not in HOLIDAYS}


def validate(raw, mapping):
    reader = csv.DictReader(io.StringIO(raw))
    required = {'ts_event', 'instrument_id', 'open', 'high', 'low', 'close', 'volume'}
    errors, warnings, normalized, seen = [], [], [], set()
    counts = dict.fromkeys(SYMBOLS, 0)
    if not required.issubset(reader.fieldnames or []):
        errors.append('Missing required fields')
    rows = list(reader)
    for i, r in enumerate(rows, 2):
        try:
            stamp = parse_bar_timestamp(r['ts_event'])
            date = stamp.date().isoformat()
            if stamp.time() != dt.time():
                errors.append('Non-midnight UTC bar at row ' + str(i))
            matches = [s for s, intervals in mapping['result'].items() for v in intervals
                       if str(v['s']) == r['instrument_id'] and v['d0'] <= date < v['d1']]
            if len(matches) != 1 or matches[0] not in SYMBOLS:
                raise ValueError('Ambiguous or missing symbol mapping')
            symbol = matches[0]
            identity = (symbol, date)
            if identity in seen:
                errors.append('Duplicate: ' + str(identity))
            seen.add(identity)
            counts[symbol] += 1
            if not '2026-07-01' <= date < '2026-10-01':
                errors.append('Out of request range: ' + str(identity))
            elif date not in EXPECTED:
                warnings.append('Non-session UTC date; investigate venue coverage: ' + str(identity))
            o, h, l, c = [Decimal(r[k]) for k in ['open', 'high', 'low', 'close']]
            if not all(x.is_finite() and x > 0 for x in (o, h, l, c)):
                errors.append('Invalid price: ' + str(identity))
            elif not l <= min(o, c) <= max(o, c) <= h:
                errors.append('OHLC ordering: ' + str(identity))
            if int(r['volume']) <= 0:
                errors.append('Nonpositive volume: ' + str(identity))
            normalized.append(dict(symbol=symbol, bar_start_utc=r['ts_event'], provider_bar_date=date,
                                   **{k: r[k] for k in ['instrument_id', 'open', 'high', 'low', 'close', 'volume']}))
        except (ValueError, KeyError, ArithmeticError, TypeError) as exc:
            errors.append('Row %s: %s' % (i, type(exc).__name__))
    missing = sorted((s, d) for s in SYMBOLS for d in EXPECTED if (s, d) not in seen)
    if missing:
        errors.append('Missing expected symbol-days: ' + str(missing))
    overlap_checked, overlap_differences = 0, []
    a_path = ROOT / 'data/a/normalized.csv'
    indexed = {(r['symbol'], r['provider_bar_date']): r for r in normalized}
    if a_path.exists():
        for a in csv.DictReader(io.StringIO(a_path.read_text())):
            key = (a['symbol'], a['provider_bar_date'])
            b = indexed.get(key)
            overlap_checked += 1
            if b is None or any(Decimal(a[k]) != Decimal(b[k]) for k in ['open', 'high', 'low', 'close', 'volume']):
                overlap_differences.append(key)
        if overlap_differences:
            warnings.append('A/B overlap differs; investigate provider revisions before use')
    else:
        warnings.append('A cache unavailable; overlap check skipped')
    report = dict(params=PARAMS, rows=len(rows), counts=counts,
                  expected_session_dates_per_symbol=len(EXPECTED), missing=missing,
                  errors=errors, warnings=warnings, structural_pass=not errors,
                  acceptance_pass=not errors and not warnings,
                  overlap_checked=overlap_checked, overlap_differences=overlap_differences,
                  sha256=hashlib.sha256(raw.encode()).hexdigest(),
                  limitations=['UTC daily bars, not regular-session OHLC.',
                               'Unadjusted; corporate actions and external prices not reconciled.',
                               'Volume covers component venues, not full market.',
                               'Calendar coverage check is sample-specific, not proof of session semantics.'])
    DATA.mkdir(parents=True, exist_ok=True)
    if normalized:
        with (DATA / 'normalized.csv').open('w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(normalized[0]))
            w.writeheader(); w.writerows(normalized)
    out = ROOT / 'reports/b_validation.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    rawpath, mappath, manifestpath = [DATA / n for n in ['ohlcv.csv', 'mapping.json', 'manifest.json']]
    if all(p.exists() for p in [rawpath, mappath, manifestpath]):
        manifest = json.loads(manifestpath.read_text())
        if manifest['params'] != PARAMS or hashlib.sha256(rawpath.read_bytes()).hexdigest() != manifest['sha256']:
            raise SystemExit('Cache integrity mismatch; stopped without API requests.')
        print('Using local cache; no API request.')
    else:
        if rawpath.exists() or (DATA / 'download_started.json').exists():
            raise SystemExit('Prior/partial download found; inspect before retrying.')
        key = os.environ.get('DATABENTO_API_KEY') or getpass.getpass('Databento API key (hidden): ')
        if not key or key != key.strip():
            raise SystemExit('Missing key or surrounding whitespace')
        quote = Decimal(post('metadata.get_cost', PARAMS, key).strip())
        print('Current estimate USD:', quote, flush=True)
        if not quote.is_finite() or quote < 0 or quote > QUOTE_LIMIT:
            raise SystemExit('Quote invalid or above reviewed B quote; stopped.')
        mp = {k: PARAMS[k] for k in ['dataset', 'symbols', 'stype_in', 'stype_out']}
        mp.update(start_date='2026-07-01', end_date='2026-10-01')
        mapping = json.loads(post('symbology.resolve', mp, key))
        if any(not mapping.get('result', {}).get(s) for s in SYMBOLS):
            raise SystemExit('Incomplete symbol mapping; stopped.')
        mappath.write_text(json.dumps(mapping, indent=2) + '\n')
        manifest = dict(params=PARAMS, estimated_usd=str(quote), actual_charge_verified=False,
                        started_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        (DATA / 'download_started.json').write_text(json.dumps(manifest, indent=2) + '\n')
        raw = post('timeseries.get_range', dict(PARAMS, encoding='csv', compression='none',
                   pretty_px='true', pretty_ts='true'), key)
        rawpath.write_text(raw)
        manifest.update(sha256=hashlib.sha256(rawpath.read_bytes()).hexdigest(),
                        bytes=rawpath.stat().st_size, http_status=200)
        manifestpath.write_text(json.dumps(manifest, indent=2) + '\n')
    report = validate(rawpath.read_text(), json.loads(mappath.read_text()))
    if not report['acceptance_pass']:
        raise SystemExit('Validation requires review; see reports/b_validation.json')


if __name__ == '__main__':
    main()
