"""Public research unit reconstruction with explicit, evidence-scoped checks.

Internal consistency is not independent verification or point-in-time evidence.
Complex-action price history remains unusable until after its guarded interval.
"""
import datetime as dt
from decimal import Decimal as D
from fractions import Fraction

from quant_backtest import CorporateAction,SessionBar
from .daily import DataContractError


def _rational(review):
    n,d=D(review['share_numerator']),D(review['share_denominator'])
    if n!=n.to_integral_value() or d!=d.to_integral_value() or n<=0 or d<=0:
        raise DataContractError('Legal share ratio needs positive integer numerator/denominator')
    return Fraction(int(n),int(d))


def _scale(value,factor):
    return None if value is None else value*D(factor.numerator)/D(factor.denominator)


def prepare_action_basis(dataset,source_events,reviews,sessions,anchor):
    """Return account prices/events for the declared PUBLIC research assumptions.

    Event available_at is explicitly a modeled ex-open time. Original retrieval
    and source hashes remain in audit rows and are never replaced by that model.
    No strict V1 readiness flag is changed. No targets are generated here.
    """
    schedule={s.session_date:s for s in sessions}
    dates=tuple(sorted(schedule))
    previous=dict(zip(dates[1:],dates))
    following=dict(zip(dates,dates[1:]))
    quotes={b.session_date:b for b in dataset.bars}
    if not dates or any(b.session_date not in schedule or b.session_date>anchor for b in dataset.bars):
        raise DataContractError('Price history outside declared calendar/adjustment anchor')
    events=[e for e in source_events if e['symbol']==dataset.symbol]
    if any(e['source_sha256']!=dataset.sha256 for e in events):
        raise DataContractError('Events and prices belong to different response versions')
    ids=[(e['kind'],e['source_event_id']) for e in events]
    if len(ids)!=len(set(ids)):
        raise DataContractError('Duplicate source event identities')
    legal={(r['symbol'],r['provider_event_id']):r for r in reviews}
    if len(legal)!=len(reviews):
        raise DataContractError('Duplicate review identities')
    split_checks,guarded=[],[]
    for e in events:
        if e['kind']!='splits': continue
        r=legal.get((dataset.symbol,e['source_event_id']))
        day=dt.date.fromisoformat(e['provider_event_time'][:10])
        if r is None or r['provider_source_sha256']!=dataset.sha256:
            raise DataContractError('Missing or mismatched split classification')
        if r['provider_session']!=day.isoformat():
            raise DataContractError('Review/source action date mismatch')
        if (D(r['provider_numerator'])!=D(e['provider_fields']['numerator']) or
                D(r['provider_denominator'])!=D(e['provider_fields']['denominator'])):
            raise DataContractError('Review/source split ratio mismatch')
        accepted=False; reason='complex_share_distribution_not_supported'; ratio=None
        if r['supported_share_action']:
            ratio=_rational(r)
            pre=quotes.get(previous.get(day)); post=quotes.get(day)
            if pre and post and pre.yahoo_close and post.yahoo_close:
                observed=post.yahoo_close/pre.yahoo_close
                adjusted_distance=abs(observed-1)
                raw_distance=abs(_scale(observed,ratio)-1)
                accepted=adjusted_distance<raw_distance
                reason='primary_ratio_and_split_normalization_consistent' if accepted else 'split_price_normalization_ambiguous'
            else:
                reason='missing_split_boundary_prices'
        if not accepted:
            legal_day=dt.date.fromisoformat(r.get('legal_distribution_date') or day.isoformat())
            guarded.append(max(day,legal_day))
        split_checks.append(dict(event=e,review=r,day=day,ratio=ratio,accepted=accepted,reason=reason))
    guard=max(guarded) if guarded else None

    def factor(day):
        value=Fraction(1)
        for check in split_checks:
            if check['accepted'] and day<check['day']<=anchor:
                value*=check['ratio']
        return value

    bars,price_rows,actions,audit=[],[],[],[]
    for b in dataset.bars:
        valid=guard is None or b.session_date>guard
        f=factor(b.session_date)
        opening=_scale(b.yahoo_open,f) if valid else None
        close=_scale(b.yahoo_close,f) if valid else None
        bars.append(SessionBar(b.symbol,b.session_date,opening,close,
                               tradable_at_execution=opening is not None))
        price_rows.append(dict(symbol=b.symbol,session_date=b.session_date,
            yahoo_open=b.yahoo_open,yahoo_close=b.yahoo_close,
            reconstructed_open=opening,reconstructed_close=close,
            factor_numerator=f.numerator,factor_denominator=f.denominator,
            basis_status='public_source_interpretation_internally_checked' if valid else 'blocked_complex_or_unresolved_action_history',
            independently_verified=False,source_sha256=dataset.sha256))
    for check in split_checks:
        e,r,day=check['event'],check['review'],check['day']
        if day not in schedule:
            raise DataContractError('Corporate action outside explicit exchange schedule')
        accepted=check['accepted']
        kwargs=dict(event_id='splits:'+e['source_event_id'],symbol=dataset.symbol,
            effective_session=day,available_at=schedule[day].open_time,
            kind='split' if accepted else 'unsupported',verified=accepted,
            evidence=r['source_url']+'; '+check['reason']+'; availability modeled at ex-open for public research')
        if accepted:
            kwargs.update(ratio_numerator=check['ratio'].numerator,ratio_denominator=check['ratio'].denominator)
        actions.append(CorporateAction(**kwargs))
        audit.append(dict(symbol=dataset.symbol,kind='splits',source_event_id=e['source_event_id'],
            session_date=day,status='accepted_share_split' if accepted else 'blocked_complex_or_unresolved',
            reason=check['reason'],classification=r['classification'],source_url=r['source_url'],
            independently_verified_price_basis=False,model_available_at=schedule[day].open_time,
            actual_retrieved_at=dataset.retrieved_at,source_sha256=dataset.sha256))
    if guard is not None:
        # Only resolve when flat, after the last guarded legal/provider date.
        after=next((d for d in dates if d>guard and d in quotes),None)
        if after is not None:
            actions.append(CorporateAction('resolution:'+guard.isoformat(),dataset.symbol,after,
                'resolve_unheld',schedule[after].open_time,verified=True,
                evidence='No held entitlement may be discarded; post-guard source units checked under PUBLIC assumptions'))
    cash_groups={}
    for e in events:
        if e['kind'] in ('dividends','capitalGains'):
            cash_groups.setdefault(e['provider_event_time'][:10],[]).append(e)
    for day_text,group in sorted(cash_groups.items()):
        day=dt.date.fromisoformat(day_text)
        pre=quotes.get(previous.get(day)); post=quotes.get(day)
        for e in group:
            amount=D(e['provider_fields']['amount']); error=None
            reason='source_adjustment_identity_consistent'; accepted=True
            if len(group)!=1:
                reason='multiple_distribution_components_need_evidence'; accepted=False
            elif pre is None or post is None or any(v is None for v in
                    (pre.yahoo_close,pre.yahoo_adjusted_close,post.yahoo_close,post.yahoo_adjusted_close)):
                reason='missing_adjacent_prices_for_cash_basis'; accepted=False
            elif pre.yahoo_close<=amount:
                reason='distribution_not_smaller_than_pre_close'; accepted=False
            else:
                observed=(post.yahoo_adjusted_close/post.yahoo_close)/(pre.yahoo_adjusted_close/pre.yahoo_close)
                expected=pre.yahoo_close/(pre.yahoo_close-amount)
                error=abs(observed/expected-1)
                if error>D('.00001'):
                    reason='distribution_adjustment_identity_mismatch'; accepted=False
            if guard is not None and day<=guard:
                reason='complex_price_or_cash_amount_basis'; accepted=False
            raw_amount=_scale(amount,factor(day)) if accepted else None
            audit.append(dict(symbol=dataset.symbol,kind=e['kind'],source_event_id=e['source_event_id'],
                session_date=day,status='accepted_public_research_basis' if accepted else 'unresolved_cash_basis',
                reason=reason,reported_amount=amount,reconstructed_actual_share_amount=raw_amount,
                amount_factor_numerator=factor(day).numerator if accepted else None,
                amount_factor_denominator=factor(day).denominator if accepted else None,
                adjustment_identity_relative_error=error,independent_amount_verification=False,
                source_adjustment_identity_consistent=error is not None and error<=D('.00001'),
                model_available_at=schedule[day].open_time,actual_retrieved_at=dataset.retrieved_at,
                source_sha256=dataset.sha256))
            actions.append(CorporateAction(e['kind']+':'+e['source_event_id'],dataset.symbol,day,
                'cash_dividend' if e['kind']=='dividends' else 'cash_distribution',schedule[day].open_time,
                cash_per_share=raw_amount if accepted else amount,verified=accepted,
                evidence=reason+'; reconstructed share units accepted only under PUBLIC research assumptions'))
    return dict(symbol=dataset.symbol,bars=tuple(bars),actions=tuple(actions),price_rows=price_rows,
        event_audit=audit,guarded_through=guard,strict_v1_readiness_changed=False,
        independent_price_basis_verified=False)
