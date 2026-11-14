"""Audit source action bases and replay fixed-target accounting fixtures offline."""
import argparse
import csv
import datetime as dt
import hashlib
import json
from collections import Counter
from decimal import Decimal as D
from fractions import Fraction
from pathlib import Path

from quant_backtest import PortfolioConfig,TargetWeights,TradingSession,simulate_portfolio
from quant_data import DataContractError
from quant_data.action_basis import prepare_action_basis
from quant_data.public_daily import parse_yahoo_chart
from quant_data.public_universe import initialize_manifest,raw_response,save_json,digest
from quant_data.us_calendar import exchange_sessions
from examples.run_portfolio import write_report

ROOT=Path(__file__).resolve().parents[1]


def fixture_check(result,prepared,source_events,reviews,cfg):
    """Rebuild a single initial purchase and subsequent actions from source rows."""
    assert len(result['trades'])==1
    trade=result['trades'][0]; symbol=trade['symbol']
    shares=0; cash=cfg.initial_cash; income=D(0); basis=D(0); fractional_realized=D(0)
    legal=[r for r in reviews if r['symbol']==symbol and r['supported_share_action']]
    marks={b.session_date:b.close for b in prepared['bars']}
    openings={b.session_date:b.open for b in prepared['bars']}
    rows={r['session_date']:r for r in result['daily']}
    positions={r['session_date']:r for r in result['positions']}
    daily_events={}
    for e in source_events:
        if e['symbol']==symbol:
            daily_events.setdefault(dt.date.fromisoformat(e['provider_event_time'][:10]),[]).append(e)
    for day, row in sorted(rows.items()):
        fractional_quantity=Fraction(0)
        for e in sorted(daily_events.get(day,[]),key=lambda e:e['kind']!='splits'):
            if e['kind']=='splits':
                legal_event=next(r for r in legal if r['provider_event_id']==e['source_event_id'])
                ratio=Fraction(int(D(legal_event['share_numerator'])),int(D(legal_event['share_denominator'])))
                shares=Fraction(shares)*ratio
                fractional_quantity=shares-(shares.numerator//shares.denominator)
            elif e['kind']=='dividends':
                factor=Fraction(1)
                for r in legal:
                    if day.isoformat()<r['provider_session']:
                        factor*=Fraction(int(D(r['share_numerator'])),int(D(r['share_denominator'])))
                amount=D(e['provider_fields']['amount'])*D(factor.numerator)/D(factor.denominator)
                q=Fraction(shares)
                payout=D(q.numerator)/D(q.denominator)*amount; cash+=payout; income+=payout
        if fractional_quantity:
            q=Fraction(shares)
            fraction=D(fractional_quantity.numerator)/D(fractional_quantity.denominator)
            total=D(q.numerator)/D(q.denominator)
            whole=q.numerator//q.denominator
            removed=basis if whole==0 else basis*fraction/total
            cash+=fraction*openings[day]; basis-=removed
            fractional_realized+=fraction*openings[day]-removed
            shares=whole
        elif isinstance(shares,Fraction):
            assert shares.denominator==1
            shares=shares.numerator
        if day==trade['fill_session']:
            shares+=trade['shares']; cash-=trade['notional']+trade['commission']
            basis+=trade['notional']+trade['commission']
        assert abs(row['cash']-cash)<D('.00000001')
        assert abs(row['equity']-cash-shares*marks[day])<D('.00000001')
        if shares:
            assert positions[day]['shares']==shares
            assert abs(positions[day]['cost_basis']-basis)<D('.00000001')
            assert abs(positions[day]['realized_pnl']-fractional_realized)<D('.00000001')
    assert result['summary']['dividend_income']==income
    assert result['summary']['final_shares'][symbol]==shares
    return dict(status='passed',daily_cash_and_share_checks=len(rows),final_shares=shares,
                source_rebuilt_dividend_income=income,ordinary_trades=1,
                fractional_cash_in_lieu_rows=sum(a['kind']=='cash_in_lieu' for a in result['corporate_actions']),
                purpose='fixed_target_accounting_fixture_not_momentum_performance')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'outputs/public_universe_20261104')
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/corporate_actions_20261104')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    cfg_path=ROOT/'configs/momentum_public_v1.json'
    cfg=json.loads(cfg_path.read_text())
    review_path=ROOT/'configs/corporate_actions_review_20261104.json'
    review=json.loads(review_path.read_text())
    events_path=args.input/'corporate_events.json'
    if digest(events_path)!=review['source_events_sha256']:
        raise DataContractError('Reviewed source-event fingerprint changed')
    events=json.loads(events_path.read_text()); reviews=review['split_records']
    if len({(r['symbol'],r['provider_event_id']) for r in reviews})!=len(reviews):
        raise DataContractError('Duplicate classified event')
    actual={(e['symbol'],e['source_event_id']) for e in events if e['kind']=='splits'}
    if actual!={(r['symbol'],r['provider_event_id']) for r in reviews}:
        raise DataContractError('Review does not cover exactly the returned split records')
    manifest=initialize_manifest(args.input/'raw',cfg_path,dt.datetime.now(dt.timezone.utc))
    start=dt.date.fromisoformat(manifest['start']); end=dt.date.fromisoformat(manifest['end_exclusive'])
    anchor=dt.date.fromisoformat(manifest['expected_adjustment_anchor'])
    schedule=exchange_sessions(start,end)
    sessions=tuple(TradingSession(s.session_date,s.open_time,s.close_time,s.close_time+dt.timedelta(minutes=30)) for s in schedule)
    inventory,audits,fixture_inputs=[],[],{}
    fields=['symbol','session_date','yahoo_open','yahoo_close','reconstructed_open','reconstructed_close',
        'factor_numerator','factor_denominator','basis_status','independently_verified','source_sha256']
    with (args.output/'reconstructed_prices.csv').open('w',newline='',encoding='utf-8') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
        for symbol,row in manifest['entries'].items():
            if row['status']!='received':
                inventory.append(dict(symbol=symbol,status='source_not_received'))
                continue
            try:
                dataset=parse_yahoo_chart(raw_response(args.input/'raw',row),symbol,
                    retrieved_at=dt.datetime.fromisoformat(row['retrieved_at_utc']))
                prepared=prepare_action_basis(dataset,events,reviews,schedule,anchor)
                audits.extend(prepared['event_audit'])
                writer.writerows({k:'' if v is None else str(v) for k,v in r.items()} for r in prepared['price_rows'])
                inventory.append(dict(symbol=symbol,status='prepared_with_public_assumptions',
                    rows=len(prepared['bars']),guarded_through=prepared['guarded_through'],
                    blocked_price_rows=sum(r['reconstructed_close'] is None for r in prepared['price_rows']),
                    accepted_cash_events=sum(a['status']=='accepted_public_research_basis' for a in prepared['event_audit']),
                    unresolved_cash_events=sum(a['status']=='unresolved_cash_basis' for a in prepared['event_audit']),
                    independent_price_basis_verified=False))
                if symbol in ('AMCR','ANET','APH','TSCO','SPY'):
                    fixture_inputs[symbol]=prepared
            except DataContractError as exc:
                inventory.append(dict(symbol=symbol,status='source_contract_failed',error=str(exc)))
                for e in events:
                    if e['symbol']==symbol:
                        audits.append(dict(symbol=symbol,kind=e['kind'],source_event_id=e['source_event_id'],
                            session_date=e['provider_event_time'][:10],status='source_price_contract_failed',
                            reason=str(exc),source_sha256=e['source_sha256']))
    samples=[]
    for sample in review['dividend_amount_samples']:
        match=next(a for a in audits if a['symbol']==sample['symbol'] and str(a['session_date'])==sample['provider_session'] and a['kind']=='dividends')
        assert match['reconstructed_actual_share_amount']==D(sample['actual_share_dividend']),sample
        samples.append(dict(sample,status='amount_matches_issuer_sample',
                            reconstructed_amount=match['reconstructed_actual_share_amount']))
    accounting=PortfolioConfig(initial_cash=D(cfg['account']['research_initial_cash']),
        fee_per_share=D(cfg['costs']['commission_usd_per_share']),
        minimum_fee=D(cfg['costs']['minimum_commission_usd_per_filled_order']),
        slippage_bps=D(cfg['costs']['slippage_bps_per_side']))
    fixtures={}
    for symbol,prepared in fixture_inputs.items():
        first=sessions[0]
        targets=[TargetWeights(first.session_date,first.close_available_time,{symbol:D('.04')})]
        result=simulate_portfolio(sessions,prepared['bars'],targets,accounting,corporate_actions=prepared['actions'])
        fixtures[symbol]=fixture_check(result,prepared,events,reviews,accounting)
        write_report(result,accounting,dict(kind='public_actual_price_fixed_target_corporate_action_fixture',
            symbol=symbol,historical_availability_is_assumed=True,rule_version='PUBLIC-1.0.0'),
            args.output/'fixtures'/symbol,
            purpose='One fixed 4% allocation to test action accounting; no momentum selection or strategy comparison.')
    save_json(args.output/'action_review.json',review)
    save_json(args.output/'event_basis_audit.json',audits)
    save_json(args.output/'symbol_basis_inventory.json',inventory)
    summary=dict(checked_at_utc=dt.datetime.now(dt.timezone.utc),source_events_sha256=digest(events_path),
        raw_manifest_sha256=digest(args.input/'raw/manifest.json'),review_sha256=digest(review_path),
        frozen_public_config_sha256=digest(cfg_path),classified_provider_split_records=len(reviews),
        supported_share_splits=sum(r['supported_share_action'] for r in reviews),
        unsupported_complex_distributions=sum(not r['supported_share_action'] for r in reviews),
        source_events_audited=len(audits),event_status_counts=dict(Counter(a['status'] for a in audits)),
        source_cash_identity_consistent=sum(a.get('source_adjustment_identity_consistent',False) for a in audits),
        guarded_symbols=[r for r in inventory if r.get('guarded_through') is not None],
        source_price_failures=[r for r in inventory if r['status']=='source_contract_failed'],
        issuer_cash_amount_samples=samples,fixed_target_fixture_checks=fixtures,
        strict_v1_readiness_unchanged=True,independent_basis_verification_complete=False,
        corporate_action_coverage_complete=False,full_strategy_backtest_run=False,
        paid_data_requests=0,new_actual_spending_usd='0',
        limitations=['Share-event classification covers the 29 returned split records, not all possible corporate actions.',
            'Internal dividend adjustment identities do not constitute independent per-event verification.',
            'Historical event availability and ex-date cash credit are explicit PUBLIC research assumptions.',
            'Complex-action prehistory and unknown cash bases remain flagged, never silently repaired.'],
        output_sha256={name:digest(args.output/name) for name in
            ('action_review.json','event_basis_audit.json','symbol_basis_inventory.json','reconstructed_prices.csv')},
        implementation_sha256={name:digest(ROOT/name) for name in
            ('quant_backtest/portfolio.py','quant_backtest/corporate_actions.py',
             'quant_data/action_basis.py','examples/review_corporate_actions.py','examples/run_portfolio.py')})
    save_json(args.output/'summary.json',summary)
    print(json.dumps({k:summary[k] for k in ('classified_provider_split_records','supported_share_splits',
        'unsupported_complex_distributions','source_events_audited','event_status_counts','full_strategy_backtest_run')},indent=2))
    print('Fixed-target accounting fixtures:',','.join(sorted(fixtures)))
    print('Saved:',args.output/'summary.json')


if __name__=='__main__': main()
