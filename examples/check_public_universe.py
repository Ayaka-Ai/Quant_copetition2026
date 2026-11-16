"""Independent raw-to-export and timezone crosscheck (Python 3.9+)."""
import argparse
import csv
import datetime as dt
import hashlib
import json
from collections import Counter
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/public_universe_20261104')
    args=parser.parse_args()
    out=args.output
    summary=json.loads((out/'summary.json').read_text())
    manifest_raw=(out/'raw/manifest.json').read_bytes()
    assert hashlib.sha256(manifest_raw).hexdigest()==summary['manifest_sha256']
    manifest=json.loads(manifest_raw)
    for name,sha in summary['output_sha256'].items():
        assert hashlib.sha256((out/name).read_bytes()).hexdigest()==sha,name
    inventory={r['symbol']:r for r in json.loads((out/'inventory.json').read_text())}
    assert set(manifest['symbols'])==set(manifest['entries'])==set(inventory)
    ny=ZoneInfo('America/New_York')
    calendar=list(csv.DictReader((out/'exchange_calendar.csv').open()))
    expected_dates={r['session_date'] for r in calendar}
    for row in calendar:
        o=dt.datetime.fromisoformat(row['open_time']).astimezone(ny)
        c=dt.datetime.fromisoformat(row['close_time']).astimezone(ny)
        assert o.date().isoformat()==row['session_date'] and (o.hour,o.minute)==(9,30)
        assert c.date()==o.date() and c.hour in (13,16) and c.minute==0
    sources,raw_events={},{}
    for symbol,row in manifest['entries'].items():
        if row['status']!='received':
            continue
        raw=(out/'raw'/row['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==row['sha256'] and len(raw)==row['bytes']
        result=json.loads(raw,parse_float=D)['chart']['result'][0]
        sources[symbol]=result
        for kind,records in result.get('events',{}).items():
            for event_id,event in records.items():
                raw_events[(symbol,kind,event_id)]=event
    seen,counts=set(),Counter()
    lookup={s:{stamp:i for i,stamp in enumerate(r.get('timestamp',[]))} for s,r in sources.items()}
    for row in csv.DictReader((out/'public_daily.csv').open()):
        symbol=row['symbol']; source=sources[symbol]
        stamp=dt.datetime.fromisoformat(row['provider_timestamp'])
        assert (stamp.astimezone(ny).hour,stamp.astimezone(ny).minute)==(9,30)
        assert stamp.date().isoformat()==row['session_date']
        assert row['session_date'] in expected_dates
        key=(symbol,row['session_date']); assert key not in seen; seen.add(key)
        i=lookup[symbol][int(stamp.timestamp())]
        quote=source['indicators']['quote'][0]
        for field in ('open','high','low','close'):
            raw_value=quote[field][i]; exported=row['yahoo_'+field]
            assert (None if exported=='' else D(exported))==(None if raw_value is None else D(str(raw_value))),key
        adj=source['indicators']['adjclose'][0]['adjclose'][i]
        assert (None if row['yahoo_adjusted_close']=='' else D(row['yahoo_adjusted_close']))==(None if adj is None else D(str(adj))),key
        volume=quote['volume'][i]
        assert (None if row['yahoo_volume']=='' else int(row['yahoo_volume']))==volume,key
        if quote['close'][i] is not None and volume is not None:
            assert D(row['reported_close_volume_proxy'])==D(str(quote['close'][i]))*volume,key
        assert not row['historical_publication_time'] and not row['unadjusted_share_close']
        assert row['source_sha256']==manifest['entries'][symbol]['sha256']
        counts[symbol]+=1
    for symbol,row in inventory.items():
        assert counts[symbol]==row['bars'],symbol
        if row['data_status'] in ('complete','partial_or_null'):
            assert counts[symbol]==len(sources[symbol]['timestamp']),symbol
            observed={day for sym,day in seen if sym==symbol}
            assert sorted(expected_dates-observed)==row['missing_sessions'],symbol
    events=json.loads((out/'corporate_events.json').read_text())
    event_keys=set()
    for event in events:
        key=(event['symbol'],event['kind'],event['source_event_id'])
        assert key not in event_keys; event_keys.add(key)
        raw=raw_events[key]
        assert int(dt.datetime.fromisoformat(event['provider_event_time']).timestamp())==raw['date']
        if event['kind']=='splits':
            assert D(event['provider_fields']['numerator'])==D(str(raw['numerator']))
            assert D(event['provider_fields']['denominator'])==D(str(raw['denominator']))
            assert event['legal_action_type_verified'] is False
        else:
            assert D(event['provider_fields']['amount'])==D(str(raw['amount']))
        assert event['distribution_amount_basis'] is None
    assert event_keys==set(raw_events),'Provider events omitted from normalized event inventory'
    assert sum(counts.values())==summary['normalized_bars']
    assert summary['public_backtest_ready'] is False and summary['generated_target_count']==0
    result=dict(status='passed',checked_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        manifest_sha256=summary['manifest_sha256'],source_response_count=len(sources),
        calendar_zoneinfo_sessions=len(calendar),normalized_rows_matched_to_raw=sum(counts.values()),
        normalized_events_matched_to_raw=len(events),selection_and_benchmark_inventory=len(inventory),
        output_sha256=summary['output_sha256'],public_backtest_ready=False)
    (out/'independent_crosschecks.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='output_sha256'},indent=2))


if __name__=='__main__':
    main()
