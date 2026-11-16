"""Explicit actual-share-unit events; source labels alone do not verify them."""
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Optional


@dataclass(frozen=True)
class CorporateAction:
    event_id: str
    symbol: str
    effective_session: dt.date
    kind: str
    available_at: dt.datetime
    ratio: Optional[Decimal] = None
    cash_per_share: Optional[Decimal] = None
    verified: bool = False
    evidence: str = ''
    ratio_numerator: Optional[int] = None
    ratio_denominator: Optional[int] = None

    def __post_init__(self):
        if any(not isinstance(s,str) or not s or s!=s.strip() for s in (self.event_id,self.symbol)):
            raise ValueError('Action needs a nonempty event ID and symbol')
        if type(self.effective_session) is not dt.date:
            raise ValueError('Action effective_session must be a date')
        if not isinstance(self.available_at,dt.datetime) or self.available_at.utcoffset() is None:
            raise ValueError('Action availability must be timezone-aware')
        if self.kind not in ('split','cash_dividend','cash_distribution','unsupported','resolve_unheld'):
            raise ValueError('Unsupported action kind; use unsupported to block explicitly')
        if type(self.verified) is not bool or not isinstance(self.evidence,str):
            raise ValueError('Invalid action verification')
        if self.verified and not self.evidence.strip():
            raise ValueError('Verified actions need source/basis evidence')
        for value in (self.ratio,self.cash_per_share):
            if value is not None and (not isinstance(value,Decimal) or not value.is_finite() or value<=0):
                raise ValueError('Action amounts and ratios must be positive finite Decimal')
        pair=(self.ratio_numerator,self.ratio_denominator)
        if any(x is not None for x in pair):
            if self.kind!='split' or any(type(x) is not int or x<=0 for x in pair):
                raise ValueError('Exact split ratios need two positive integers')
            calculated=Decimal(pair[0])/Decimal(pair[1])
            if self.ratio is not None and self.ratio!=calculated:
                raise ValueError('Conflicting Decimal and rational split ratios')
            object.__setattr__(self,'ratio',calculated)
        if self.kind=='split' and (self.ratio is None or self.cash_per_share is not None):
            raise ValueError('A split needs only a share ratio')
        if self.kind in ('cash_dividend','cash_distribution') and (self.cash_per_share is None or self.ratio is not None):
            raise ValueError('Cash events need only actual-share cash amount')
        if self.kind in ('unsupported','resolve_unheld') and (self.ratio is not None or self.cash_per_share is not None):
            raise ValueError('Unsupported actions must not supply guessed cash or shares')

    @property
    def exact_ratio(self):
        if self.kind!='split':
            raise ValueError('Only split events have a share ratio')
        return (Fraction(self.ratio_numerator,self.ratio_denominator) if self.ratio_numerator is not None
                else Fraction(self.ratio))


def action_schedule(actions, session_dates):
    """Deduplicate exact repeats; conflicting versions fail rather than sum."""
    result, identities, split_days = {}, {}, set()
    for action in actions:
        if not isinstance(action,CorporateAction) or action.effective_session not in session_dates:
            raise ValueError('Action must belong to the explicit session schedule')
        key=(action.symbol,action.event_id)
        if key in identities:
            if identities[key]!=action:
                raise ValueError('Conflicting corporate-action version')
            continue
        identities[key]=action
        if action.kind=='split':
            day=(action.symbol,action.effective_session)
            if day in split_days:
                raise ValueError('Multiple same-session split events require explicit consolidation')
            split_days.add(day)
        result.setdefault(action.effective_session,[]).append(action)
    for actions_for_day in result.values():
        priority={'unsupported':0,'split':1,'resolve_unheld':2,'cash_dividend':3,'cash_distribution':3}
        actions_for_day.sort(key=lambda a:(priority[a.kind],a.symbol,a.kind,a.event_id))
    return result
