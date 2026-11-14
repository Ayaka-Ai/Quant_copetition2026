"""Point-in-time momentum and turnover measurements; no portfolio or orders."""
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple


@dataclass(frozen=True)
class MomentumObservation:
    symbol: str
    session_date: dt.date
    available_at: dt.datetime
    adjusted_close: Optional[Decimal]
    unadjusted_close: Optional[Decimal]
    turnover_usd: Optional[Decimal]
    tradable: Optional[bool] = None
    permission: Optional[bool] = None
    corporate_actions_clear: Optional[bool] = None

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.strip():
            raise ValueError('Invalid observation symbol')
        if type(self.session_date) is not dt.date:
            raise ValueError('Observation session_date must be a date')
        if not isinstance(self.available_at, dt.datetime) or self.available_at.utcoffset() is None:
            raise ValueError('available_at must be timezone-aware')
        for value in (self.adjusted_close, self.unadjusted_close):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value <= 0):
                raise ValueError('Closes must be positive finite Decimal values or None')
        value = self.turnover_usd
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
            raise ValueError('Turnover must be nonnegative finite Decimal or None')
        if any(v is not None and type(v) is not bool for v in
               (self.tradable, self.permission, self.corporate_actions_clear)):
            raise ValueError('Observation flags must be bool or None')


@dataclass(frozen=True)
class MomentumStats:
    symbol: str
    momentum: Optional[Decimal]
    average_turnover_usd: Optional[Decimal]
    current: Optional[MomentumObservation]
    data_reasons: Tuple[str, ...]


def momentum_snapshot(observations, history_dates, signal_time, symbols, *, lookback=20, turnover_window=30):
    """Use the latest version published by signal_time for each symbol/date.

    history_dates is the complete trading calendar through the signal session.
    Missing sessions are never compressed away or forward-filled. Adjusted
    historical versions must already be on a common basis at this cutoff.
    """
    if type(lookback) is not int or lookback < 1 or type(turnover_window) is not int or turnover_window < 1:
        raise ValueError('Lookbacks must be positive integers')
    dates, symbols = tuple(history_dates), tuple(symbols)
    if not dates or any(type(d) is not dt.date for d in dates) or any(a >= b for a, b in zip(dates, dates[1:])):
        raise ValueError('history_dates must be unique, ordered session dates')
    if len(set(symbols)) != len(symbols) or any(not isinstance(s, str) or not s for s in symbols):
        raise ValueError('symbols must be unique nonempty strings')
    if not isinstance(signal_time, dt.datetime) or signal_time.utcoffset() is None:
        raise ValueError('signal_time must be timezone-aware')
    wanted, allowed, latest, versions = set(symbols), set(dates), {}, set()
    for observation in observations:
        if not isinstance(observation, MomentumObservation):
            raise ValueError('Expected MomentumObservation objects')
        version = (observation.symbol, observation.session_date, observation.available_at)
        if version in versions:
            raise ValueError('Duplicate symbol/date/availability version')
        versions.add(version)
        if (observation.symbol not in wanted or observation.session_date not in allowed
                or observation.available_at > signal_time):
            continue
        key = (observation.symbol, observation.session_date)
        previous = latest.get(key)
        if previous is None or observation.available_at > previous.available_at:
            latest[key] = observation
    output = []
    for symbol in sorted(symbols):
        reasons = []
        momentum = turnover = None
        if len(dates) < lookback + 1:
            reasons.append('price_warmup')
        else:
            window = [latest.get((symbol, day)) for day in dates[-lookback-1:]]
            if any(o is None or o.adjusted_close is None for o in window):
                reasons.append('missing_adjusted_close_history')
            else:
                momentum = window[-1].adjusted_close / window[0].adjusted_close - 1
        if len(dates) < turnover_window:
            reasons.append('turnover_warmup')
        else:
            window = [latest.get((symbol, day)) for day in dates[-turnover_window:]]
            if any(o is None or o.turnover_usd is None for o in window):
                reasons.append('missing_turnover_history')
            else:
                turnover = sum((o.turnover_usd for o in window), Decimal(0)) / turnover_window
        current = latest.get((symbol, dates[-1]))
        if current is None:
            reasons.append('missing_current_observation')
        output.append(MomentumStats(symbol, momentum, turnover, current, tuple(reasons)))
    return tuple(output)
