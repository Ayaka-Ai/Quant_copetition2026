import datetime as dt
import unittest
from dataclasses import replace
from decimal import Decimal as D

from quant_data import DataContractError
from quant_data.action_basis import prepare_action_basis
from quant_data.public_daily import PublicDailyBar,PublicDataset
from quant_data.us_calendar import exchange_sessions


class ActionBasisTests(unittest.TestCase):
    def setUp(self):
        self.sessions=exchange_sessions(dt.date(2026,11,2),dt.date(2026,11,5))
        self.days=[s.session_date for s in self.sessions]
        self.retrieved=dt.datetime(2026,11,5,tzinfo=dt.timezone.utc)
        closes=[D(10),D('9.5'),D('9.5')]; adj=[D('9.5')]*3
        bars=tuple(PublicDailyBar('A',s.session_date,s.open_time,c,c,c,c,a,100)
            for s,c,a in zip(self.sessions,closes,adj))
        self.dataset=PublicDataset('A',bars,(),self.retrieved,'sha')
        self.cash=dict(symbol='A',kind='dividends',source_event_id='cash',
            provider_event_time=self.days[1].isoformat()+'T14:30:00+00:00',
            provider_fields=dict(amount='.5'),source_sha256='sha')
        self.split=dict(symbol='A',kind='splits',source_event_id='split',
            provider_event_time=self.days[2].isoformat()+'T14:30:00+00:00',
            provider_fields=dict(numerator='2',denominator='1'),source_sha256='sha')
        self.review=dict(symbol='A',provider_event_id='split',provider_session=str(self.days[2]),
            provider_source_sha256='sha',provider_numerator='2',provider_denominator='1',
            supported_share_action=True,classification='forward_split',share_numerator='2',share_denominator='1',
            source_url='https://issuer.example/split',legal_distribution_date=None)

    def prepare(self,events=None,reviews=None):
        return prepare_action_basis(self.dataset,events if events is not None else [self.cash,self.split],
            reviews if reviews is not None else [self.review],self.sessions,self.days[-1])

    def test_price_and_cash_undo_only_future_real_share_splits(self):
        r=self.prepare()
        self.assertEqual([b.close for b in r['bars']],[D(20),D(19),D('9.5')])
        cash=next(a for a in r['actions'] if a.kind=='cash_dividend')
        self.assertEqual(cash.cash_per_share,D(1))
        self.assertTrue(cash.verified)
        a=next(a for a in r['event_audit'] if a['kind']=='dividends')
        self.assertLess(a['adjustment_identity_relative_error'],D('.00001'))
        self.assertNotEqual(a['model_available_at'],a['actual_retrieved_at'])
        self.assertFalse(a['independent_amount_verification'])
        self.assertFalse(r['independent_price_basis_verified'])

    def test_complex_distribution_is_never_parent_share_multiplication(self):
        self.review.update(supported_share_action=False,classification='spinoff',legal_distribution_date=str(self.days[1]))
        r=self.prepare()
        self.assertTrue(all(b.close is None for b in r['bars']))
        self.assertFalse(any(a.kind=='split' for a in r['actions']))
        self.assertTrue(any(a.kind=='unsupported' for a in r['actions']))

    def test_record_date_and_legal_date_both_guarded_then_flat_resolution(self):
        self.split['provider_event_time']=self.days[0].isoformat()+'T14:30:00+00:00'
        self.review.update(provider_session=str(self.days[0]),supported_share_action=False,
            classification='spinoff',legal_distribution_date=str(self.days[1]))
        r=self.prepare(events=[self.split])
        self.assertEqual(r['guarded_through'],self.days[1])
        self.assertEqual([b.close for b in r['bars']],[None,None,D('9.5')])
        resolution=next(a for a in r['actions'] if a.kind=='resolve_unheld')
        self.assertEqual(resolution.effective_session,self.days[2])

    def test_wrong_amount_or_missing_adjacent_price_does_not_pass_basis(self):
        self.cash['provider_fields']['amount']='.9'
        r=self.prepare()
        action=next(a for a in r['actions'] if a.kind=='cash_dividend')
        self.assertFalse(action.verified)
        self.assertIn('mismatch',next(a for a in r['event_audit'] if a['kind']=='dividends')['reason'])
        self.cash['provider_event_time']=self.days[0].isoformat()+'T14:30:00+00:00'
        r=self.prepare()
        self.assertEqual(next(a for a in r['event_audit'] if a['kind']=='dividends')['reason'],
                         'missing_adjacent_prices_for_cash_basis')

    def test_source_version_and_duplicate_event_rejections(self):
        self.cash['source_sha256']='different'
        with self.assertRaisesRegex(DataContractError,'different response'):
            self.prepare()
        self.cash['source_sha256']='sha'
        with self.assertRaisesRegex(DataContractError,'Duplicate source'):
            self.prepare(events=[self.cash,self.cash])
        self.review['provider_session']='2026-11-03'
        with self.assertRaisesRegex(DataContractError,'date mismatch'):
            self.prepare()

    def test_unadjusted_prices_not_double_adjusted_when_split_label_exists(self):
        b=list(self.dataset.bars)
        b[-1]=replace(b[-1],yahoo_close=D('4.75'),yahoo_open=D('4.75'))
        self.dataset=replace(self.dataset,bars=tuple(b))
        r=self.prepare()
        self.assertEqual(r['guarded_through'],self.days[-1])
        self.assertFalse(any(a.kind=='split' for a in r['actions']))
        self.assertEqual(r['event_audit'][0]['reason'],'split_price_normalization_ambiguous')


if __name__=='__main__': unittest.main()
