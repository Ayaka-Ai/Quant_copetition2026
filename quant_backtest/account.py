"""Offline long/flat sleeve accounts using next observed bar-close proxies."""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from quant_signals import moving_average_signals

D = Decimal


@dataclass(frozen=True)
class SimulationConfig:
    initial_cash: Decimal = D('100000')
    fee_per_share: Decimal = D('0.005')
    minimum_fee: Decimal = D('1')
    slippage_bps: Decimal = D('5')
    short_window: int = 5
    long_window: int = 20


def simulate(bars, config=SimulationConfig()):
    """Fixed initial equal cash sleeves; trade target changes, never rebalance daily.

    Whole-share requested quantities use signal-time prices. At next bar-close
    proxy fill, cash may limit quantity; unfilled remainder expires. No borrowing,
    dividends, splits, interest, settlement delay, liquidity model or forced exit.
    """
    for value in (config.initial_cash, config.fee_per_share, config.minimum_fee, config.slippage_bps):
        if not isinstance(value, D) or not value.is_finite() or value < 0:
            raise ValueError('Cash/cost assumptions must be finite nonnegative Decimal values')
    if config.initial_cash <= 0 or config.slippage_bps >= 10000:
        raise ValueError('Invalid initial cash or slippage')
    bars = tuple(bars)
    if not bars:
        raise ValueError('No bars')
    signals = moving_average_signals(bars, short_window=config.short_window, long_window=config.long_window)
    lookup = {(s.symbol, s.provider_bar_date): s for s in signals}
    symbols = sorted({b.symbol for b in bars})
    by_day = defaultdict(dict)
    for b in bars:
        by_day[b.provider_bar_date][b.symbol] = b
    # This demo requires a complete shared calendar; fail rather than invent marks.
    if any(set(day) != set(symbols) for day in by_day.values()):
        raise ValueError('Unequal symbol calendars; missing-price policy required')
    piece = (config.initial_cash / len(symbols)).quantize(D('.01'), rounding=ROUND_DOWN)
    accounts = {s: dict(cash=piece if i < len(symbols)-1 else config.initial_cash-piece*(len(symbols)-1),
                        shares=0, basis=D(0), realized=D(0), target=None, pending=None)
                for i, s in enumerate(symbols)}
    allocations = {s: a['cash'] for s, a in accounts.items()}
    slip = config.slippage_bps / D(10000)
    orders, trades, daily, positions = [], [], [], []
    fees = slippage = D(0)
    peak = config.initial_cash

    def fee(qty):
        return max(config.minimum_fee, config.fee_per_share*qty) if qty else D(0)

    def affordable(cash, price, cap=None):
        # Binary search accounts for commission without using a future price
        # when first sizing an order. At fill this is only a cash constraint.
        low, high = 0, int(cash / price)
        if cap is not None:
            high = min(high, cap)
        while low < high:
            mid = (low+high+1)//2
            if mid*price+fee(mid) <= cash:
                low = mid
            else:
                high = mid-1
        return low

    for date, day in sorted(by_day.items()):
        for symbol in symbols:
            bar, a = day[symbol], accounts[symbol]
            order = a['pending']
            if order is not None:
                if not bar.bar_end_utc > order['signal_not_before_utc']:
                    raise ValueError('Fill must follow signal interval')
                buy = order['side'] == 'BUY'
                price = bar.close*(1+slip if buy else 1-slip)
                quantity = (affordable(a['cash'], price, order['requested_shares']) if buy
                            else min(a['shares'], order['requested_shares']))
                commission = fee(quantity)
                notional = quantity*price
                cash_delta = -notional-commission if buy else notional-commission
                if not buy and a['cash']+cash_delta < 0:
                    quantity = 0; commission = notional = cash_delta = D(0)
                quantity = int(quantity)
                if quantity:
                    if buy:
                        a['basis'] += notional+commission
                        a['shares'] += quantity
                    else:
                        removed_basis = a['basis']*quantity/a['shares']
                        a['realized'] += notional-commission-removed_basis
                        a['basis'] -= removed_basis
                        a['shares'] -= quantity
                    a['cash'] += cash_delta
                    cost = abs(price-bar.close)*quantity
                    fees += commission; slippage += cost
                    trades.append(dict(order_id=order['order_id'], symbol=symbol, side=order['side'],
                                       signal_date=order['signal_date'], fill_bar_date=date,
                                       fill_proxy_time_utc=bar.bar_end_utc, reference_close=bar.close,
                                       fill_price=price, shares=quantity, notional=notional,
                                       commission=commission, slippage_cost=cost,
                                       cash_delta=cash_delta, sleeve_cash_after=a['cash'],
                                       sleeve_shares_after=a['shares']))
                order.update(status='filled' if quantity==order['requested_shares'] else
                             'partial_remainder_cancelled' if quantity else 'cancelled_insufficient_cash',
                             filled_shares=quantity, fill_bar_date=date)
                a['pending'] = None
            signal = lookup[(symbol,date)]
            if signal.target_position is not None and signal.target_position != a['target']:
                buy = signal.target_position == 1
                qty = affordable(a['cash'], bar.close*(1+slip)) if buy else a['shares']
                order = dict(order_id=len(orders)+1, symbol=symbol, signal_date=date,
                             signal_not_before_utc=signal.signal_not_before_utc,
                             side='BUY' if buy else 'SELL', requested_shares=qty, filled_shares=0,
                             status='pending_end_of_sample' if qty else 'no_quantity', fill_bar_date=None)
                orders.append(order)
                if qty:
                    a['pending'] = order
                a['target'] = signal.target_position
            value = a['shares']*bar.close
            if a['cash'] < 0 or a['shares'] < 0:
                raise AssertionError('Negative cash/shares')
            positions.append(dict(date=date, symbol=symbol, shares=a['shares'], close=bar.close,
                                  market_value=value, cost_basis=a['basis'], cash=a['cash'],
                                  realized_pnl=a['realized'], unrealized_pnl=value-a['basis']))
        cash = sum((a['cash'] for a in accounts.values()), D(0))
        market_value = sum((accounts[s]['shares']*day[s].close for s in symbols), D(0))
        realized = sum((a['realized'] for a in accounts.values()), D(0))
        unrealized = market_value-sum((a['basis'] for a in accounts.values()), D(0))
        equity = cash+market_value
        if abs(equity-config.initial_cash-realized-unrealized) > D('0.00000001'):
            raise AssertionError('PnL reconciliation failed')
        if abs(cash-config.initial_cash-sum((t['cash_delta'] for t in trades),D(0))) > D('0.00000001'):
            raise AssertionError('Cash ledger reconciliation failed')
        peak = max(peak,equity)
        daily.append(dict(date=date, cash=cash, market_value=market_value, equity=equity,
                          net_value=equity/config.initial_cash, realized_pnl=realized,
                          unrealized_pnl=unrealized, cumulative_commission=fees,
                          cumulative_slippage=slippage, drawdown=equity/peak-1))
    return dict(orders=orders, trades=trades, daily=daily, positions=positions,
                summary=dict(initial_cash=config.initial_cash, initial_allocations=allocations,
                             final_equity=daily[-1]['equity'], final_cash=daily[-1]['cash'],
                             final_market_value=daily[-1]['market_value'],
                             net_pnl=daily[-1]['equity']-config.initial_cash,
                             return_pct=(daily[-1]['net_value']-1)*100,
                             max_drawdown_pct=-min(r['drawdown'] for r in daily)*100,
                             commissions=fees, slippage_cost=slippage, filled_orders=len(trades),
                             pending_orders=sum(a['pending'] is not None for a in accounts.values()),
                             final_shares={s:a['shares'] for s,a in accounts.items()},
                             accounting_checks_passed=True))
