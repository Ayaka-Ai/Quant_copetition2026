from collections import defaultdict, deque
from dataclasses import dataclass
import datetime as dt
from decimal import Decimal, localcontext
from typing import Iterable, Optional, Tuple

from quant_data import DailyBar


@dataclass(frozen=True)
class MovingAverageSignal:
    symbol: str
    provider_bar_date: dt.date
    bar_end_utc: dt.datetime
    signal_not_before_utc: dt.datetime
    close: Decimal
    short_ma: Optional[Decimal]
    long_ma: Optional[Decimal]
    status: str
    target_position: Optional[int]


def moving_average_signals(bars: Iterable[DailyBar], *, short_window: int = 5,
                           long_window: int = 20) -> Tuple[MovingAverageSignal, ...]:
    """Trailing observed-bar means per symbol; no execution or portfolio sizing.

    target_position is a per-symbol long/flat state, NOT a portfolio weight.
    Warmup yields None; equality yields flat. Missing bars are not filled.
    signal_not_before_utc is a lower bound, not verified publication time.
    """
    if (type(short_window) is not int or type(long_window) is not int
            or not 1 <= short_window < long_window):
        raise ValueError('Require integer windows: 1 <= short_window < long_window')
    histories = defaultdict(lambda: deque(maxlen=long_window))
    previous = {}
    signals = []
    with localcontext() as context:
        context.prec = 28
        for bar in bars:
            if (bar.bar_start_utc.tzinfo is None or bar.bar_end_utc.tzinfo is None
                    or bar.bar_end_utc <= bar.bar_start_utc):
                raise ValueError('Invalid timezone-aware bar interval')
            if bar.symbol in previous and bar.bar_start_utc < previous[bar.symbol]:
                raise ValueError('Duplicate, overlapping or unordered bars for ' + bar.symbol)
            if not isinstance(bar.close, Decimal) or not bar.close.is_finite() or bar.close <= 0:
                raise ValueError('Expected a positive finite Decimal close')
            previous[bar.symbol] = bar.bar_end_utc
            history = histories[bar.symbol]
            history.append(bar.close)
            values = list(history)
            short = sum(values[-short_window:], Decimal(0)) / short_window if len(values) >= short_window else None
            long = sum(values, Decimal(0)) / long_window if len(values) >= long_window else None
            target = int(short > long) if long is not None else None
            signals.append(MovingAverageSignal(
                bar.symbol, bar.provider_bar_date, bar.bar_end_utc, bar.bar_end_utc,
                bar.close, short, long, 'ready' if target is not None else 'warmup', target))
    return tuple(sorted(signals, key=lambda s: (s.bar_end_utc, s.symbol)))
