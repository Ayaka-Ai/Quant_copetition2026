"""Download only the approved A sample; reuse cache on subsequent runs."""
import base64
import csv
import datetime as dt
from decimal import Decimal
import getpass
import hashlib
import io
import json
import os
import re
from pathlib import Path
import subprocess
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'a'
PARAMS = dict(dataset='EQUS.MINI', symbols='AAPL,MSFT,SPY', schema='ohlcv-1d',
              stype_in='raw_symbol', stype_out='instrument_id',
              start='2026-09-14T00:00:00Z', end='2026-09-19T00:00:00Z')
# Stop on any price increase over the saved quote (small rounding tolerance).
QUOTE_LIMIT = Decimal('0.00002346933')


def parse_bar_timestamp(value):
    """UTC datetime for date checks; original nanosecond text stays in CSV.

    Python 3.8 datetime accepts microseconds, not nine-digit fractions.
    Truncate (never round) only the parsing copy, preserving date boundaries.
    """
    if value.isdigit():
        seconds, nanos = divmod(int(value), 10**9)
        return (dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
                + dt.timedelta(seconds=seconds, microseconds=nanos // 1000))
    match = re.fullmatch(
        r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})',
        value)
    if not match:
        raise ValueError('Expected timezone-aware ISO timestamp or epoch nanoseconds')
    base, fraction, zone = match.groups()
    fraction = '.' + fraction[:6].ljust(6, '0') if fraction else ''
    zone = '+00:00' if zone == 'Z' else zone
    return dt.datetime.fromisoformat(base + fraction + zone).astimezone(dt.timezone.utc)


def post(endpoint, params, key):
    if endpoint not in {'metadata.get_cost', 'symbology.resolve', 'timeseries.get_range'}:
        raise ValueError('Endpoint not allowed')
    auth = base64.b64encode((key + ':').encode()).decode()
    result = subprocess.run(['curl', '--silent', '--show-error', '--max-time', '45',
        '--proto', '=https', '--config', '-', '--data', urlencode(params),
        '--write-out', '\n%{http_code}', 'https://hist.databento.com/v0/' + endpoint],
        input='header = "Authorization: Basic ' + auth + '"\n',
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Transport failure; do not retry automatically. curl=' + str(result.returncode))
    body, code = result.stdout.rsplit('\n', 1)
    body = body.replace(key, '[REDACTED]').replace(auth, '[REDACTED]')
    if code != '200':
        raise RuntimeError('HTTP ' + code + ': ' + body[:1500])
    return body


def validate(raw, mapping):
    rows = list(csv.DictReader(io.StringIO(raw)))
    required = {'ts_event','instrument_id','open','high','low','close','volume'}
    if not rows or not required.issubset(rows[0]):
        raise ValueError('Missing CSV rows or fields')
    errors, seen, counts = [], set(), {s: 0 for s in ['AAPL','MSFT','SPY']}
    expected = {f'2026-09-{d:02d}' for d in range(14,19)}
    normalized = []
    for r in rows:
        ts = r['ts_event']
        stamp = parse_bar_timestamp(ts)
        date = stamp.date().isoformat()
        matches = [s for s, intervals in mapping['result'].items() for interval in intervals
                   if str(interval['s']) == r['instrument_id'] and interval['d0'] <= date < interval['d1']]
        symbol = matches[0] if len(matches)==1 else 'UNRESOLVED'
        identity = (symbol, date)
        if identity in seen: errors.append('duplicate: ' + str(identity))
        seen.add(identity)
        if symbol not in counts or date not in expected: errors.append('unexpected: ' + str(identity))
        else: counts[symbol] += 1
        # Explicit pretty_px=true request means CSV prices are decimal dollars.
        o,h,l,c = [Decimal(r[k]) for k in ['open','high','low','close']]
        if not all(x.is_finite() and x > 0 for x in [o,h,l,c]):
            errors.append('invalid price: ' + str(identity))
        elif not l <= min(o,c) <= max(o,c) <= h:
            errors.append('OHLC ordering: ' + str(identity))
        if int(r['volume']) <= 0: errors.append('nonpositive volume: ' + str(identity))
        normalized.append(dict(symbol=symbol, bar_start_utc=ts, provider_bar_date=date,
                               **{k:r[k] for k in ['instrument_id','open','high','low','close','volume']}))
    missing = sorted((s,d) for s in counts for d in expected if (s,d) not in seen)
    if missing: errors.append('missing symbol-days: ' + str(missing))
    report = dict(rows=len(rows), counts=counts, errors=errors, structural_pass=not errors,
        checks=['schema','symbol mapping','five UTC bar dates per symbol','duplicates','positive prices','OHLC bounds','positive volume'],
        limitations=['Provider UTC daily bars are not verified regular-session bars.',
                     'Corporate actions, external price reconciliation and full-market volume not verified.',
                     'Five days are for ingestion validation, not evidence of strategy performance.'])
    if normalized:
        with (DATA/'normalized.csv').open('w') as f:
            w=csv.DictWriter(f,fieldnames=list(normalized[0])); w.writeheader(); w.writerows(normalized)
    path=ROOT/'reports'/'a_validation.json'; path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


def main():
    DATA.mkdir(parents=True,exist_ok=True)
    rawpath=DATA/'ohlcv.csv'; mappath=DATA/'mapping.json'
    if rawpath.exists() and mappath.exists():
        print('Using local cache; no API request.')
    else:
        if rawpath.exists() or (DATA/'download_started.json').exists():
            raise SystemExit('Prior/partial download found. Inspect it before any retry.')
        key=os.environ.get('DATABENTO_API_KEY') or getpass.getpass('Databento API key (hidden): ')
        quote=Decimal(post('metadata.get_cost',PARAMS,key).strip())
        print('Current estimate USD:',quote,flush=True)
        if not quote.is_finite() or quote < 0 or quote > QUOTE_LIMIT:
            raise SystemExit('Quote invalid or above previously reviewed A quote; download stopped.')
        mp={k:PARAMS[k] for k in ['dataset','symbols','stype_in','stype_out']}
        mp.update(start_date='2026-09-14',end_date='2026-09-19')
        mapping=json.loads(post('symbology.resolve',mp,key))
        if any(not mapping.get('result',{}).get(s) for s in ['AAPL','MSFT','SPY']):
            raise SystemExit('Incomplete mapping; download stopped.')
        mappath.write_text(json.dumps(mapping,indent=2)+'\n')
        manifest=dict(params=PARAMS,estimated_usd=str(quote),actual_charge_verified=False,
                      started_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        (DATA/'download_started.json').write_text(json.dumps(manifest,indent=2)+'\n')
        payload=dict(PARAMS,encoding='csv',compression='none',pretty_px='true',pretty_ts='true')
        raw=post('timeseries.get_range',payload,key)
        rawpath.write_text(raw)
        manifest.update(sha256=hashlib.sha256(raw.encode()).hexdigest(),bytes=len(raw.encode()),http_status=200)
        (DATA/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    validate(rawpath.read_text(),json.loads(mappath.read_text()))


if __name__=='__main__':
    main()
