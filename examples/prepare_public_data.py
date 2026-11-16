"""Audit local data; optionally obtain a three-symbol public historical pilot.

python3 -m examples.prepare_public_data
python3 -m examples.prepare_public_data --fetch-public-pilot

No credentials, paid endpoints, broker routes, targets or simulated returns.
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path

from examples.run_portfolio import encode
from quant_data import DataContractError, load_daily_bars
from quant_data.public_daily import (PUBLIC_DATA_GAPS, audit_public_dataset, load_public_cache,
                                     parse_yahoo_chart, public_data_readiness)
from quant_data.us_calendar import SOURCES, exchange_sessions
from quant_portfolio import load_momentum_config

ROOT = Path(__file__).resolve().parents[1]
START, END = dt.date(2024, 11, 4), dt.date(2026, 11, 4)
PILOT = ('AAPL', 'MSFT', 'SPY')


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=encode) + '\n', encoding='utf-8')


def fetch_public_pilot(directory):
    """Small fixed public request, resumable per successful hash-pinned response.

    A successful existing cache is reused. No full-universe acquisition until
    the pilot's source capabilities support the frozen rules.
    """
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / 'manifest.json'
    if manifest_path.exists():
        datasets = load_public_cache(directory)
        m = json.loads(manifest_path.read_text())
        if (m['start'], m['end_exclusive'], tuple(d.symbol for d in datasets)) != (
                START.isoformat(), END.isoformat(), tuple(sorted(PILOT))):
            raise DataContractError('Existing cache is not this fixed pilot')
        return
    # Retain successful responses across an interrupted or rate-limited run.
    checkpoint = directory / 'checkpoint.json'
    manifest = dict(schema_version='yahoo_public_daily_v1', source='Yahoo Finance chart',
                    start=START.isoformat(), end_exclusive=END.isoformat(), responses=[])
    if checkpoint.exists():
        old = json.loads(checkpoint.read_text())
        if (old['schema_version'], old['start'], old['end_exclusive']) != (
                manifest['schema_version'], manifest['start'], manifest['end_exclusive']):
            raise DataContractError('Incompatible public pilot checkpoint')
        manifest = old
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    for symbol in PILOT:
        saved = next((r for r in manifest['responses'] if r['symbol'] == symbol), None)
        if saved:
            raw = (directory / saved['file']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != saved['sha256'] or len(raw) != saved['bytes']:
                raise DataContractError('Changed checkpoint response')
            parse_yahoo_chart(raw, symbol, retrieved_at=dt.datetime.fromisoformat(saved['retrieved_at_utc']))
            continue
        params = dict(period1=int(dt.datetime.combine(START, dt.time(), dt.timezone.utc).timestamp()),
                      period2=int(dt.datetime.combine(END, dt.time(), dt.timezone.utc).timestamp()),
                      interval='1d', events='div,splits,capitalGains', includeAdjustedClose='true')
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + symbol + '?' + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, context=context, timeout=20) as response:
                    raw = response.read()
                retrieved = dt.datetime.now(dt.timezone.utc)
                data = parse_yahoo_chart(raw, symbol, retrieved_at=retrieved)
                if any(not START <= b.session_date < END for b in data.bars):
                    raise DataContractError('Provider returned data outside fixed request dates')
                break
            except urllib.error.HTTPError as exc:
                # Respect authentication/rate blocks; do not try other hosts or accounts.
                if exc.code not in (500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError('Public source HTTP ' + str(exc.code)) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise RuntimeError('Public source transport failed after three attempts') from None
            time.sleep(attempt + 1)
        filename = symbol + '.json'
        (directory / filename).write_bytes(raw)
        manifest['responses'].append(dict(symbol=symbol, file=filename, request_url=url,
            retrieved_at_utc=retrieved.isoformat(), sha256=data.sha256, bytes=len(raw)))
        save_json(checkpoint, manifest)
        print('Public pilot cached:', symbol, len(data.bars), 'sessions', flush=True)
    save_json(manifest_path, manifest)
    load_public_cache(directory)


def audit_local_b():
    path = ROOT / 'experiments/databento_smoke/data/b'
    if not path.exists():
        return dict(status='not_present')
    try:
        data = load_daily_bars(path)
        return dict(status='structural_pass_strategy_blocked', info=asdict(data.info),
                    rows=len(data.bars), symbols=sorted({b.symbol for b in data.bars}),
                    first_date=min(b.provider_bar_date for b in data.bars),
                    last_date=max(b.provider_bar_date for b in data.bars),
                    strategy_ready=False, reasons=['UTC daily bars cannot supply regular-session open/close',
                        'Component venues do not establish consolidated dollar turnover',
                        'No point-in-time adjustments, corporate actions or publication times'])
    except DataContractError as exc:
        return dict(status='invalid', error=str(exc), strategy_ready=False)


def audit_legacy_ranking(directory, universe):
    """Inspect original cached responses; do not invent retrieval timestamps."""
    if not directory.exists():
        return dict(status='not_present')
    rows = []
    for symbol in sorted({m.symbol for m in universe} | {'SPY'}):
        filename = directory / (symbol.replace('.', '-') + '.json')
        if not filename.exists():
            rows.append(dict(symbol=symbol, status='missing_file'))
            continue
        try:
            data = parse_yahoo_chart(filename.read_bytes(), symbol)
            row = audit_public_dataset(data, dt.date(2026, 9, 1), dt.date(2026, 11, 3))
            row.pop('missing_checks')  # Shared source limitations recorded once below.
            row['status'] = 'structural_pass' if row['structural_pass'] else 'data_gaps'
            rows.append(row)
        except DataContractError as exc:
            rows.append(dict(symbol=symbol, status='invalid_or_unavailable', error=str(exc),
                             sha256=hashlib.sha256(filename.read_bytes()).hexdigest()))
    return dict(status='audited', requested_symbols=len(rows),
                structural_pass_count=sum(r['status'] == 'structural_pass' for r in rows),
                rows=rows, strategy_ready=False, missing_checks=PUBLIC_DATA_GAPS,
                note='Legacy responses lack per-response acquisition manifests; filesystem mtime is not a publication time.')


def write_pilot_exports(output, datasets):
    fields = ['symbol', 'session_date', 'provider_timestamp', 'yahoo_open', 'yahoo_high', 'yahoo_low',
              'yahoo_close', 'yahoo_adjusted_close', 'yahoo_volume', 'retrieved_at_utc',
              'historical_publication_time', 'unadjusted_share_close', 'consolidated_turnover_usd']
    with (output / 'public_daily.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for data in datasets:
            for bar in data.bars:
                row = asdict(bar)
                row['retrieved_at_utc'] = data.retrieved_at
                writer.writerow({k: '' if v is None else str(v) for k, v in row.items()})
    save_json(output / 'corporate_events.json', [event for data in datasets for event in data.events])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch-public-pilot', action='store_true', help='Fetch fixed AAPL/MSFT/SPY two-year public sample')
    parser.add_argument('--cache', type=Path, default=ROOT / 'outputs/data_acceptance/public_raw')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/data_acceptance')
    parser.add_argument('--legacy-cache', type=Path, default=ROOT.parent / 'work/us-momentum-20261103/raw')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fetch_error = None
    if args.fetch_public_pilot:
        try:
            fetch_public_pilot(args.cache)
        except (OSError, ValueError, RuntimeError) as exc:
            fetch_error = str(exc)
    _, universe, config = load_momentum_config(ROOT / 'configs/momentum_v1.json')
    pilot = dict(status='not_present', strategy_ready=False)
    normalized_exports = {}
    if (args.cache / 'manifest.json').exists():
        try:
            datasets = load_public_cache(args.cache)
            write_pilot_exports(args.output, datasets)
            normalized_exports = {name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
                                  for name in ('public_daily.csv', 'corporate_events.json')}
            manifest = json.loads((args.cache / 'manifest.json').read_text())
            lo, hi = [dt.date.fromisoformat(manifest[k]) for k in ('start', 'end_exclusive')]
            pilot = dict(status='audited', rows=sum(len(d.bars) for d in datasets),
                         symbols=[d.symbol for d in datasets], start=lo, end_exclusive=hi,
                         audit=[audit_public_dataset(d, lo, hi) for d in datasets],
                         strategy_ready=False)
        except DataContractError as exc:
            pilot = dict(status='invalid', error=str(exc), strategy_ready=False)
    report = dict(checked_at_utc=dt.datetime.now(dt.timezone.utc), purpose='data_acceptance_not_strategy_performance',
                  rule_version=config['rule_version'], public_sources_only=True, credentials_required=False,
                  paid_market_data_requests=0, new_actual_spending_usd='0',
                  frozen_universe_securities=len(universe), required_history_months='24--36',
                  public_pilot=pilot, normalized_export_sha256=normalized_exports,
                  fetch_error=fetch_error, local_b=audit_local_b(),
                  legacy_ranking=audit_legacy_ranking(args.legacy_cache, universe),
                  readiness=asdict(public_data_readiness()), source_gaps=PUBLIC_DATA_GAPS,
                  strategy_ready=False, generated_target_count=0, backtest_run=False,
                  additional_blockers=['Account corporate-action cash/share ledger is not implemented',
                      'Public pilot is three symbols; full frozen universe and permissions are not covered'],
                  decision='Retain public data for exploratory price/signal checks. Frozen V1 historical performance remains blocked.',
                  calendar_sources=SOURCES)
    save_json(args.output / 'data_acceptance.json', report)
    with (args.output / 'exchange_calendar.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['session_date', 'open_time', 'close_time'])
        writer.writeheader()
        writer.writerows(asdict(s) for s in exchange_sessions(START, END))
    print(json.dumps(dict(public_pilot_status=pilot['status'], public_pilot_rows=pilot.get('rows', 0),
                          legacy_structural_pass=report['legacy_ranking'].get('structural_pass_count', 0),
                          strategy_ready=False, generated_target_count=0, fetch_error=fetch_error,
                          report=str(args.output / 'data_acceptance.json')), ensure_ascii=False, indent=2))
    # A valid audit can succeed with known capability gaps. Transport/cache corruption cannot.
    if fetch_error or pilot['status'] == 'invalid' or report['local_b']['status'] == 'invalid':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
