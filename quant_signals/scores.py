"""Common score contract, independent of portfolio selection and accounting."""
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class SignalScore:
    symbol: str
    signal_session: dt.date
    available_at: dt.datetime
    value: Optional[Decimal]
    details: dict = field(default_factory=dict)
    reasons: tuple = ()

    def __post_init__(self):
        if not self.symbol or type(self.signal_session) is not dt.date:
            raise ValueError('Score requires a symbol and session date')
        if not isinstance(self.available_at, dt.datetime) or self.available_at.utcoffset() is None:
            raise ValueError('Score availability must be timezone-aware')
        if self.value is not None and (not isinstance(self.value, Decimal) or not self.value.is_finite()):
            raise ValueError('Score must be a finite Decimal or None')
        if self.value is None and not self.reasons:
            raise ValueError('Unavailable scores require explicit reasons')
