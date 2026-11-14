"""Resumable public-universe acquisition and offline source-quality inspection.

No credentials, paid data, execution adapter, price repair or trading decisions.
HTTP authentication/rate blocks stop new requests; source errors stay in inventory.
"""
import csv
import datetime as dt
import hashlib
import json
import shutil
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path

from .daily import DataContractError
from .public_daily import parse_yahoo_chart, parse_yahoo_events, audit_public_dataset, load_public_cache, PUBLIC_DATA_GAPS
from .us_calendar import exchange_sessions, SOURCES

UTC = dt.timezone.utc


def encode(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, D):
        return str(value)
    raise TypeError(type(value).__name__)


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, default=encode, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest_completed_session(now):
    if not isinstance(now, dt.datetime) or now.utcoffset() is None:
        raise ValueError('Need aware acquisition timestamp')
    now = now.astimezone(UTC)
    sessions = exchange_sessions(now.date()-dt.timedelta(days=14), now.date()+dt.timedelta(days=1))
    return max(s.session_date for s in sessions if s.close_time <= now)


def profile_spec(path):
    path = Path(path)
    cfg = json.loads(path.read_text())
    if (cfg['schema_version'] != 'public_exploration_rules_1.0'
            or cfg['strategy_id'] != 'US_MOM20_EW_PUBLIC'
            or cfg['broker_routing_enabled'] is not False
            or cfg['scope']['public_no_credential_sources_only'] is not True
            or cfg['scope']['paid_data_requests_allowed'] is not False):
        raise DataContractError('Only the frozen public-only research profile is supported')
    snapshot_path = path.parent / cfg['universe']['snapshot_file']
    if digest(snapshot_path) != cfg['universe']['snapshot_sha256']:
        raise DataContractError('Universe fingerprint mismatch')
    snapshot = json.loads(snapshot_path.read_text())
    members = {r['symbol']: r for r in snapshot['records']}
    if len(members) != snapshot['security_count'] or 'SPY' in members:
        raise DataContractError('Duplicate universe or benchmark in selection pool')
    members['SPY'] = dict(symbol='SPY', company='SPDR S&P 500 ETF Trust', sector='', issuer_cik='')
    start = dt.date.fromisoformat(cfg['research_protocol']['requested_history_start'])
    end = dt.date.fromisoformat(cfg['research_protocol']['requested_history_end_exclusive'])
    exchange_sessions(start, end)
    return cfg, members, start, end, digest(path)


def initialize_manifest(root, config_path, now):
    root = Path(root)
    cfg, members, start, end, config_hash = profile_spec(config_path)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'manifest.json'
    identity = dict(schema_version='yahoo_public_universe_v1', source='Yahoo Finance chart',
                    rule_version=cfg['rule_version'], config_sha256=config_hash,
                    universe_sha256=cfg['universe']['snapshot_sha256'],
                    start=start.isoformat(), end_exclusive=end.isoformat(), symbols=sorted(members))
    if path.exists():
        manifest = json.loads(path.read_text())
        if any(manifest.get(k) != v for k, v in identity.items()):
            raise DataContractError('Existing manifest belongs to another profile/window/universe')
        if set(manifest['entries']) != set(members):
            raise DataContractError('Manifest silently omitted or added a universe symbol')
        anchor = dt.date.fromisoformat(manifest['expected_adjustment_anchor'])
        if manifest['request_end_exclusive'] != max(end, anchor+dt.timedelta(days=1)).isoformat():
            raise DataContractError('Request coverage does not reach the pinned adjustment anchor')
        for symbol, row in manifest['entries'].items():
            if (row.get('symbol') != symbol or row.get('status') not in
                    ('not_requested','received','http_error','transport_error')):
                raise DataContractError('Invalid manifest entry identity/status')
            if row['status'] == 'received' and (row.get('http_status') != 200 or not row.get('file')):
                raise DataContractError('Successful response needs HTTP 200 and a pinned file')
            if row.get('file'):
                raw_response(root, row)
        return manifest
    anchor = latest_completed_session(now)
    if end > anchor+dt.timedelta(days=1):
        raise DataContractError('Requested historical window contains an unfinished session')
    manifest = dict(identity, created_at_utc=now, expected_adjustment_anchor=anchor.isoformat(),
                    anchor_is_assumption_not_verified_source_basis=True,
                    request_end_exclusive=max(end, anchor+dt.timedelta(days=1)).isoformat(),
                    request_policy=dict(max_workers=2, attempts_per_symbol=3,
                        batch_pause_seconds=.5, stop_on_http=[401, 403, 429], paid_requests=0),
                    entries={s: dict(symbol=s, status='not_requested') for s in sorted(members)})
    save_json(path, manifest)
    return manifest


def raw_response(root, row):
    name = row['file']
    if name != row['symbol'].replace('.', '-')+'.json' or Path(name).name != name:
        raise DataContractError('Unexpected response filename')
    try:
        raw = (root / name).read_bytes()
    except OSError as exc:
        raise DataContractError('Missing/unreadable pinned response: '+row['symbol']) from exc
    if hashlib.sha256(raw).hexdigest() != row['sha256'] or len(raw) != row['bytes']:
        raise DataContractError('Response SHA256/size mismatch: '+row['symbol'])
    return raw


def seed_pilot(root, manifest, pilot):
    if not (pilot / 'manifest.json').exists():
        return
    old = json.loads((pilot / 'manifest.json').read_text())
    if (old['start'], old['end_exclusive']) != (manifest['start'], manifest['request_end_exclusive']):
        return
    load_public_cache(pilot)
    for row in old['responses']:
        symbol = row['symbol']
        if symbol not in manifest['entries'] or manifest['entries'][symbol]['status'] != 'not_requested':
            continue
        stamp = dt.datetime.fromisoformat(row['retrieved_at_utc'])
        if latest_completed_session(stamp).isoformat() != manifest['expected_adjustment_anchor']:
            continue
        shutil.copyfile(pilot / row['file'], root / row['file'])
        manifest['entries'][symbol] = dict(row, status='received', http_status=200,
            reused_from=str(pilot), attempts=[], expected_adjustment_anchor=manifest['expected_adjustment_anchor'])
    save_json(root / 'manifest.json', manifest)


def public_request(symbol, start, end, context):
    params = dict(period1=int(dt.datetime.combine(start, dt.time(), UTC).timestamp()),
                  period2=int(dt.datetime.combine(end, dt.time(), UTC).timestamp()), interval='1d',
                  events='div,splits,capitalGains', includeAdjustedClose='true')
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(symbol.replace('.', '-'), safe='')
    url += '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    attempts, raw, http_status = [], None, None
    for attempt in range(3):
        requested = dt.datetime.now(UTC)
        try:
            with urllib.request.urlopen(request, timeout=20, context=context) as response:
                raw, http_status = response.read(), response.status
        except urllib.error.HTTPError as exc:
            http_status, raw = exc.code, exc.read()
        except (OSError, urllib.error.URLError, TimeoutError):
            http_status, raw = None, None
        received = dt.datetime.now(UTC)
        attempts.append(dict(requested_at_utc=requested, received_at_utc=received, http_status=http_status))
        if http_status == 200 or (http_status is not None and http_status not in (500, 502, 503, 504)):
            break
        if attempt < 2:
            time.sleep(attempt+1)
    row = dict(symbol=symbol, status='received' if http_status == 200 else
               'transport_error' if http_status is None else 'http_error',
               http_status=http_status, request_url=url, retrieved_at_utc=received,
               attempts=attempts)
    if raw is not None:
        row.update(file=symbol.replace('.', '-')+'.json', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    return row, raw


def fetch_universe(root, manifest, *, requester=None):
    """Issue at most two ordinary requests at a time; never retry stored results."""
    root = Path(root)
    if latest_completed_session(dt.datetime.now(UTC)).isoformat() != manifest['expected_adjustment_anchor']:
        raise DataContractError('Adjustment anchor advanced; use a new acquisition directory')
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    requester = public_request if requester is None else requester
    start = dt.date.fromisoformat(manifest['start'])
    end = dt.date.fromisoformat(manifest['request_end_exclusive'])
    pending = [s for s in manifest['symbols'] if manifest['entries'][s]['status'] == 'not_requested']
    if any(r.get('http_status') in (401, 403, 429) for r in manifest['entries'].values()):
        raise DataContractError('Stored authentication/rate block; no automatic resume requests')
    for offset in range(0, len(pending), 2):
        if latest_completed_session(dt.datetime.now(UTC)).isoformat() != manifest['expected_adjustment_anchor']:
            manifest['stop_reason'] = 'adjustment_anchor_advanced'
            break
        with ThreadPoolExecutor(max_workers=2) as pool:
            batch = [pool.submit(requester, s, start, end, context) for s in pending[offset:offset+2]]
            for task in batch:
                row, raw = task.result()
                row['expected_adjustment_anchor'] = manifest['expected_adjustment_anchor']
                if raw is not None:
                    (root / row['file']).write_bytes(raw)
                manifest['entries'][row['symbol']] = row
                save_json(root / 'manifest.json', manifest)
                done = sum(r['status'] != 'not_requested' for r in manifest['entries'].values())
                if done % 10 == 0 or row['status'] != 'received':
                    print('Acquired {}/{}; {}: {}'.format(done, len(manifest['entries']), row['symbol'], row['status']), flush=True)
        if any(r.get('http_status') in (401, 403, 429) for r in manifest['entries'].values()):
            manifest['stop_reason'] = 'provider_authentication_or_rate_block'
            break
        if all(r['status'] == 'transport_error' for r in list(manifest['entries'].values()) if r['symbol'] in pending[offset:offset+2]):
            manifest['stop_reason'] = 'entire_batch_transport_failure'
            break
        time.sleep(.5)
    save_json(root / 'manifest.json', manifest)
    return manifest


def inspect_quality(dataset, start, end):
    audit = audit_public_dataset(dataset, start, end)
    audit.pop('missing_checks')
    sessions = exchange_sessions(start, end)
    bars = {b.session_date: b for b in dataset.bars}
    first_window, streak = None, 0
    for session in sessions:
        b = bars.get(session.session_date)
        valid = b and all(getattr(b, k) is not None for k in
            ('yahoo_open','yahoo_high','yahoo_low','yahoo_close','yahoo_adjusted_close','yahoo_volume')) and b.yahoo_volume > 0
        streak = streak+1 if valid else 0
        if streak >= 30 and first_window is None:
            first_window = session.session_date
    audit['first_30_complete_sessions_end'] = first_window
    audit['missing_before_first_record'] = [d for d in audit['missing_sessions'] if d < dataset.bars[0].session_date]
    audit['missing_after_last_record'] = [d for d in audit['missing_sessions'] if d > dataset.bars[-1].session_date]
    audit['missing_inside_observed_span'] = [d for d in audit['missing_sessions']
        if dataset.bars[0].session_date <= d <= dataset.bars[-1].session_date]
    counts = Counter(e['kind'] for e in dataset.events)
    audit['event_counts'] = dict(counts)
    audit['corporate_action_basis_verified'] = False
    audit['price_reconstruction_ready'] = False
    audit['event_review_reasons'] = ['distribution_share_amount_basis_unknown'] if counts['dividends'] or counts['capitalGains'] else []
    if counts['splits']:
        audit['event_review_reasons'].append('split_price_basis_and_event_coverage_require_review')
    groups = Counter((e['kind'], e['provider_event_time'].date()) for e in dataset.events)
    audit['multiple_same_type_event_dates'] = [dict(kind=k, session_date=d, count=n) for (k,d),n in groups.items() if n > 1]
    if audit['multiple_same_type_event_dates']:
        audit['event_review_reasons'].append('multiple_events_require_deduplication_review')
    # Flag unexplained changes in AdjClose/Close; do not repair or delete a price.
    distribution_dates = {e['provider_event_time'].date() for e in dataset.events if e['kind'] != 'splits'}
    changes, previous = [], None
    for b in dataset.bars:
        if b.yahoo_close is None or b.yahoo_adjusted_close is None:
            previous = None
            continue
        ratio = b.yahoo_adjusted_close / b.yahoo_close
        if previous is not None and abs(ratio/previous-1) > D('.00001') and b.session_date not in distribution_dates:
            changes.append(b.session_date)
        previous = ratio
    audit['adjustment_changes_without_distribution_event'] = changes
    if changes:
        audit['event_review_reasons'].append('unexplained_adjustment_factor_change')
    return audit


def annotate_event(event, row, *, price_contract_passed):
    result = dict(event, source_sha256=row['sha256'], retrieved_at_utc=row['retrieved_at_utc'],
        distribution_amount_basis=None, split_price_basis_verified=False,
        daily_price_contract_passed=price_contract_passed,
        legal_action_type_verified=False)
    if event['kind'] == 'splits':
        result['classification'] = 'provider_split_record_not_yet_verified_as_actual_share_split'
        ratio = D(str(event['provider_fields']['numerator'])) / D(str(event['provider_fields']['denominator']))
        result['reported_ratio'] = ratio
        result['non_integer_or_inverse_integer_ratio'] = ratio != ratio.to_integral_value() and 1/ratio != (1/ratio).to_integral_value()
    return result


def audit_universe(root, output, config_path):
    root, output = Path(root), Path(output)
    cfg, members, start, end, config_hash = profile_spec(config_path)
    # Offline inspection uses the original pinned acquisition anchor.
    manifest = initialize_manifest(root, config_path, dt.datetime.now(UTC))
    output.mkdir(parents=True, exist_ok=True)
    expected_dates = [s.session_date for s in exchange_sessions(start,end)]
    inventories, events = [], []
    fieldnames = ['symbol','session_date','provider_timestamp','yahoo_open','yahoo_high','yahoo_low',
        'yahoo_close','yahoo_adjusted_close','yahoo_volume','reported_close_volume_proxy',
        'retrieved_at_utc','source_sha256','expected_adjustment_anchor','historical_publication_time',
        'unadjusted_share_close','distribution_amount_basis_verified']
    with (output / 'public_daily.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for symbol in manifest['symbols']:
            row = manifest['entries'][symbol]
            inventory = dict(symbol=symbol, role='benchmark' if symbol=='SPY' else 'selection_pool',
                company=members[symbol]['company'], sector=members[symbol]['sector'],
                issuer_cik=members[symbol]['issuer_cik'], acquisition_status=row['status'],
                http_status=row.get('http_status'), data_status=row['status'], bars=0,
                expected_sessions=len(expected_dates), missing_sessions=expected_dates,
                price_reconstruction_ready=False, strategy_ready=False)
            inventories.append(inventory)
            if row['status'] != 'received':
                inventory['error'] = 'No successful daily response; symbol retained in inventory'
                continue
            try:
                raw = raw_response(root, row)
                data = parse_yahoo_chart(raw, symbol, retrieved_at=dt.datetime.fromisoformat(row['retrieved_at_utc']))
                request_end = dt.date.fromisoformat(manifest['request_end_exclusive'])
                if any(not start <= b.session_date < request_end for b in data.bars):
                    raise DataContractError('Response outside declared request window')
                audit = inspect_quality(data, start, end)
                inventory.update(audit, bars=len(data.bars),
                    data_status='complete' if audit['structural_pass'] else 'partial_or_null')
                for bar in data.bars:
                    record = asdict(bar)
                    record.update(retrieved_at_utc=row['retrieved_at_utc'], source_sha256=data.sha256,
                        expected_adjustment_anchor=manifest['expected_adjustment_anchor'],
                        reported_close_volume_proxy=bar.yahoo_close*bar.yahoo_volume
                            if bar.yahoo_close is not None and bar.yahoo_volume is not None else None)
                    writer.writerow({k: '' if v is None else str(v) for k,v in record.items()})
                events.extend(annotate_event(e,row,price_contract_passed=True) for e in data.events)
            except DataContractError as exc:
                inventory.update(data_status='invalid_or_unavailable', error=str(exc))
                try:
                    retained_events = parse_yahoo_events(raw_response(root,row), symbol, start,
                        dt.date.fromisoformat(manifest['request_end_exclusive']))
                    events.extend(annotate_event(e,row,price_contract_passed=False) for e in retained_events)
                    inventory['event_counts'] = dict(Counter(e['kind'] for e in retained_events))
                    inventory['event_review_reasons'] = ['daily_price_contract_failed_action_basis_unknown']
                except DataContractError as event_exc:
                    inventory['event_error'] = str(event_exc)
    save_json(output / 'inventory.json', inventories)
    save_json(output / 'corporate_events.json', events)
    csvfields = ['symbol','role','company','sector','issuer_cik','acquisition_status','http_status','data_status',
        'bars','first_date','last_date','expected_sessions','missing_sessions','null_counts',
        'zero_volume_sessions','first_30_complete_sessions_end','event_counts','event_review_reasons',
        'adjustment_changes_without_distribution_event','error','price_reconstruction_ready','strategy_ready']
    with (output / 'inventory.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=csvfields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows({k: json.dumps(v,default=encode,ensure_ascii=False) if isinstance(v,(dict,list))
                          else str(v) if isinstance(v,(dt.date,dt.datetime)) else v for k,v in r.items()} for r in inventories)
    with (output / 'exchange_calendar.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['session_date','open_time','close_time'])
        writer.writeheader()
        writer.writerows(asdict(s) for s in exchange_sessions(start, end))
    statuses = Counter(r['data_status'] for r in inventories)
    summary = dict(rule_version=cfg['rule_version'], checked_at_utc=dt.datetime.now(UTC),
        config_sha256=config_hash, universe_sha256=manifest['universe_sha256'],
        manifest_sha256=digest(root / 'manifest.json'), requested_selection_symbols=len(members)-1,
        requested_benchmark_symbols=1, inventory_symbols=len(inventories), start=start, end_exclusive=end,
        expected_sessions=len(exchange_sessions(start,end)), expected_adjustment_anchor=manifest['expected_adjustment_anchor'],
        anchor_is_source_interpretation_assumption=True, acquisition_status_counts=dict(Counter(r['status'] for r in manifest['entries'].values())),
        acquisition_attempted_all=all(r['status'] not in ('not_requested','transport_error') for r in manifest['entries'].values()),
        data_status_counts=dict(statuses), normalized_bars=sum(r['bars'] for r in inventories),
        corporate_event_counts=dict(Counter(e['kind'] for e in events)),
        events_retained_despite_price_contract_failure=sum(not e['daily_price_contract_passed'] for e in events),
        nonstandard_split_record_symbols=sorted({e['symbol'] for e in events if e.get('non_integer_or_inverse_integer_ratio')}),
        symbols_with_split_events=[r['symbol'] for r in inventories if r.get('event_counts',{}).get('splits')],
        symbols_with_adjustment_review=[r['symbol'] for r in inventories if r.get('adjustment_changes_without_distribution_event')],
        exceptions=[dict(symbol=r['symbol'], status=r['data_status'], error=r.get('error'),
                         missing_sessions=r.get('missing_sessions',[])) for r in inventories if r['data_status']!='complete'],
        public_sources_only=True, paid_requests=0, new_actual_spending_usd='0',
        input_structure_audited=True, corporate_action_basis_verified=False,
        strict_v1_readiness={k:False for k in PUBLIC_DATA_GAPS}, public_backtest_ready=False,
        backtest_run=False, generated_target_count=0, source_limitations=PUBLIC_DATA_GAPS,
        next_requirements=['public_profile_adapter','split_and_distribution_basis_review','corporate_action_cash_share_ledger'],
        calendar_sources=SOURCES,
        output_sha256={name:digest(output/name) for name in
            ('public_daily.csv','inventory.csv','inventory.json','corporate_events.json','exchange_calendar.csv')})
    save_json(output / 'summary.json', summary)
    return summary
