import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io

from quant_data import DataContractError
from quant_data.public_daily import parse_yahoo_chart, parse_yahoo_events
from quant_data.public_universe import (initialize_manifest, fetch_universe, audit_universe,
    latest_completed_session, inspect_quality, save_json, raw_response)
from quant_data.us_calendar import exchange_sessions

ROOT=Path(__file__).resolve().parents[1]
CONFIG=ROOT/'configs/momentum_public_v1.json'
UTC=dt.timezone.utc


class PublicUniverseTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'raw'
        self.now=dt.datetime(2026,11,4,7,tzinfo=UTC)
        self.manifest=initialize_manifest(self.root,CONFIG,self.now)

    def raw(self,symbol,days):
        schedule=exchange_sessions(days[0],days[-1]+dt.timedelta(days=1))
        stamps=[int(s.open_time.timestamp()) for s in schedule if s.session_date in days]
        n=len(stamps)
        result=dict(meta=dict(symbol=symbol.replace('.','-'), currency='USD',
            exchangeTimezoneName='America/New_York',dataGranularity='1d'),timestamp=stamps,
            indicators=dict(quote=[dict(open=[10]*n,high=[11]*n,low=[9]*n,close=[10]*n,volume=[100]*n)],
                adjclose=[dict(adjclose=[10]*n)]),events={})
        return json.dumps(dict(chart=dict(result=[result],error=None))).encode()

    def put(self,symbol,raw,status='received',http=200):
        name=symbol.replace('.','-')+'.json'; (self.root/name).write_bytes(raw)
        row=dict(symbol=symbol,status=status,http_status=http,file=name,
            bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),retrieved_at_utc=self.now.isoformat())
        self.manifest['entries'][symbol]=row
        save_json(self.root/'manifest.json',self.manifest)
        return row

    def test_full_membership_and_no_silent_symbol_removal(self):
        self.assertEqual(len(self.manifest['symbols']),504)
        self.assertIn('SPY',self.manifest['symbols'])
        self.assertIn('KVUE',self.manifest['symbols'])
        self.assertEqual(self.manifest['expected_adjustment_anchor'],'2026-11-03')
        self.manifest['entries'].pop('KVUE')
        save_json(self.root/'manifest.json',self.manifest)
        with self.assertRaisesRegex(DataContractError,'omitted'):
            initialize_manifest(self.root,CONFIG,self.now)

    def test_all_stored_files_checked_before_reuse(self):
        row=self.put('AAPL',self.raw('AAPL',[dt.date(2026,11,2)]))
        (self.root/row['file']).write_bytes(b'changed')
        with self.assertRaisesRegex(DataContractError,'SHA256'):
            initialize_manifest(self.root,CONFIG,self.now)

    def test_path_traversal_and_window_anchor_rejected(self):
        with self.assertRaisesRegex(DataContractError,'filename'):
            raw_response(self.root,dict(symbol='AAA',file='../AAA.json'))
        self.manifest['expected_adjustment_anchor']='2026-11-05'
        save_json(self.root/'manifest.json',self.manifest)
        with self.assertRaisesRegex(DataContractError,'anchor'):
            initialize_manifest(self.root,CONFIG,self.now)

    def test_response_errors_and_absent_symbols_stay_in_inventory(self):
        self.put('AAPL',self.raw('AAPL',[dt.date(2026,11,2),dt.date(2026,11,3)]))
        self.put('KVUE',b'{"chart":{"result":null,"error":{"code":"Not Found"}}}')
        out=Path(self.tmp.name)/'audit'
        result=audit_universe(self.root,out,CONFIG)
        inventory={r['symbol']:r for r in json.loads((out/'inventory.json').read_text())}
        self.assertEqual(len(inventory),504)
        self.assertEqual(inventory['AAPL']['data_status'],'partial_or_null')
        self.assertEqual(inventory['KVUE']['data_status'],'invalid_or_unavailable')
        self.assertEqual(inventory['SPY']['data_status'],'not_requested')
        self.assertFalse(result['acquisition_attempted_all'])
        self.assertFalse(result['public_backtest_ready'])
        self.assertEqual(result['normalized_bars'],2)
        self.assertEqual(result['generated_target_count'],0)

    def test_complete_window_does_not_imply_reconstruction_ready(self):
        start,end=dt.date(2026,9,1),dt.date(2026,11,4)
        days=[s.session_date for s in exchange_sessions(start,end)]
        data=parse_yahoo_chart(self.raw('AAA',days),'AAA',retrieved_at=self.now)
        audit=inspect_quality(data,start,end)
        self.assertTrue(audit['structural_pass'])
        self.assertEqual(audit['first_30_complete_sessions_end'],days[29])
        self.assertFalse(audit['price_reconstruction_ready'])
        self.assertFalse(audit['corporate_action_basis_verified'])

    def test_authentication_or_rate_block_stops_new_batches(self):
        calls=[]
        def request(symbol,start,end,context):
            calls.append(symbol)
            return dict(symbol=symbol,status='http_error',http_status=429,
                        retrieved_at_utc=self.now.isoformat(),attempts=[]),None
        with patch('quant_data.public_universe.latest_completed_session',return_value=dt.date(2026,11,3)), patch('quant_data.public_universe.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
            fetch_universe(self.root,self.manifest,requester=request)
            self.assertEqual(len(calls),2)
            self.assertEqual(self.manifest['stop_reason'],'provider_authentication_or_rate_block')
            with self.assertRaisesRegex(DataContractError,'rate block'):
                fetch_universe(self.root,self.manifest,requester=request)
        self.assertEqual(len(calls),2)

    def test_known_response_is_reused_and_only_pending_requested(self):
        first=self.manifest['symbols'][0]
        self.put(first,self.raw(first,[dt.date(2026,11,3)]))
        calls=[]
        def request(symbol,start,end,context):
            calls.append(symbol)
            return dict(symbol=symbol,status='http_error',http_status=429,
                        retrieved_at_utc=self.now.isoformat(),attempts=[]),None
        with patch('quant_data.public_universe.latest_completed_session',return_value=dt.date(2026,11,3)), patch('quant_data.public_universe.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
            fetch_universe(self.root,self.manifest,requester=request)
        self.assertNotIn(first,calls)
        self.assertEqual(self.manifest['entries'][first]['status'],'received')

    def test_completed_anchor_handles_early_close_and_closed_day(self):
        # Friday Nov 27 2026 closes at 18:00 UTC; the previous session is Wednesday.
        self.assertEqual(latest_completed_session(dt.datetime(2026,11,27,17,tzinfo=UTC)),dt.date(2026,11,25))
        self.assertEqual(latest_completed_session(dt.datetime(2026,11,27,18,tzinfo=UTC)),dt.date(2026,11,27))

    def test_actions_retained_when_prices_fail_without_approving_prices(self):
        raw=json.loads(self.raw('KVUE',[dt.date(2026,11,3)]))
        result=raw['chart']['result'][0]
        result['timestamp'][0]+=30
        date=int(exchange_sessions(dt.date(2026,10,1),dt.date(2026,10,2))[0].open_time.timestamp())
        result['events']={'dividends':{'event-1':{'date':date,'amount':.25}}}
        encoded=json.dumps(raw).encode()
        self.put('KVUE',encoded)
        self.assertEqual(parse_yahoo_events(encoded,'KVUE',dt.date(2024,11,4),dt.date(2026,11,4))[0]['source_event_id'],'event-1')
        out=Path(self.tmp.name)/'audit'
        summary=audit_universe(self.root,out,CONFIG)
        self.assertEqual(summary['events_retained_despite_price_contract_failure'],1)
        self.assertEqual(summary['normalized_bars'],0)
        self.assertFalse(summary['public_backtest_ready'])
        events=json.loads((out/'corporate_events.json').read_text())
        self.assertFalse(events[0]['daily_price_contract_passed'])
        self.assertIsNone(events[0]['distribution_amount_basis'])


if __name__=='__main__':
    unittest.main()
