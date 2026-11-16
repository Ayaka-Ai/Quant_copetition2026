"""Offline portfolio accounting for externally supplied target weights.

An explicit session schedule determines order expiry. Prices must describe the
declared session and actual share units. Optional, evidenced events implement
the public research profile's ex-date cash and split/cash-in-lieu conventions.
"""
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR
from types import MappingProxyType
from fractions import Fraction
from typing import Optional, Tuple
from .corporate_actions import action_schedule

D = Decimal
ZERO = D('0')
TOLERANCE = D('0.00000001')


def _aware(value):
    return isinstance(value, dt.datetime) and value.utcoffset() is not None


def _decimal(value, *, positive=False):
    return (isinstance(value, D) and value.is_finite()
            and (value > 0 if positive else value >= 0))


@dataclass(frozen=True)
class TradingSession:
    session_date: dt.date
    open_time: dt.datetime
    close_time: dt.datetime
    close_available_time: dt.datetime
    kind: str = 'regular'

    def __post_init__(self):
        if type(self.session_date) is not dt.date:
            raise ValueError('session_date must be a date')
        if not all(_aware(x) for x in (self.open_time, self.close_time, self.close_available_time)):
            raise ValueError('Session times must be timezone-aware')
        if not self.open_time < self.close_time <= self.close_available_time:
            raise ValueError('Require open < close <= close availability')
        if self.kind not in ('regular', 'utc_calendar_day_proxy'):
            raise ValueError('Unknown session kind')


@dataclass(frozen=True)
class SessionBar:
    symbol: str
    session_date: dt.date
    open: Optional[Decimal]
    close: Optional[Decimal]
    tradable_at_execution: bool = True

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.strip():
            raise ValueError('Expected nonempty symbol without surrounding whitespace')
        if type(self.session_date) is not dt.date or type(self.tradable_at_execution) is not bool:
            raise ValueError('Invalid session date or tradability flag')
        if any(x is not None and not _decimal(x, positive=True) for x in (self.open, self.close)):
            raise ValueError('Prices must be positive finite Decimal values or None')


@dataclass(frozen=True)
class TargetWeights:
    signal_session: dt.date
    signal_time: dt.datetime
    weights: Mapping
    buy_priority: Tuple[str, ...] = ()
    unsafe_holding_symbols: Tuple[str, ...] = ()
    maximum_trade_notional: Optional[Decimal] = None
    symbol_trade_notionals: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if type(self.signal_session) is not dt.date or not _aware(self.signal_time):
            raise ValueError('Target requires a session date and timezone-aware signal time')
        if not isinstance(self.weights, Mapping):
            raise ValueError('weights must map symbols to Decimal NAV fractions')
        weights = dict(self.weights)
        if any(not isinstance(s, str) or not s or s != s.strip() for s in weights):
            raise ValueError('Invalid weight symbol')
        if any(not _decimal(w) or w > 1 for w in weights.values()) or sum(weights.values(), ZERO) > 1:
            raise ValueError('Weights must be finite Decimal values >= 0, summing to <= 1')
        positive = {s for s, w in weights.items() if w > 0}
        priority = tuple(self.buy_priority) if self.buy_priority else tuple(sorted(positive))
        if len(priority) != len(set(priority)) or set(priority) != positive:
            raise ValueError('buy_priority must list every positive-weight symbol exactly once')
        object.__setattr__(self, 'weights', MappingProxyType(weights))
        object.__setattr__(self, 'buy_priority', priority)
        unsafe = tuple(self.unsafe_holding_symbols)
        if len(unsafe) != len(set(unsafe)) or any(not isinstance(s, str) or not s for s in unsafe):
            raise ValueError('Invalid unsafe_holding_symbols')
        object.__setattr__(self, 'unsafe_holding_symbols', unsafe)
        if self.maximum_trade_notional is not None and not _decimal(self.maximum_trade_notional):
            raise ValueError('Invalid total trade budget')
        caps = dict(self.symbol_trade_notionals)
        if any(not isinstance(s, str) or not _decimal(v) for s, v in caps.items()):
            raise ValueError('Invalid symbol trade budgets')
        object.__setattr__(self, 'symbol_trade_notionals', MappingProxyType(caps))


@dataclass(frozen=True)
class AccountSnapshot:
    session_date: dt.date
    available_at: dt.datetime
    cash: Decimal
    equity: Optional[Decimal]
    holdings: Mapping
    weights: Mapping
    drawdown: Optional[Decimal]
    valuation_valid: bool


@dataclass(frozen=True)
class PortfolioConfig:
    initial_cash: Decimal = D('100000')
    fee_per_share: Decimal = D('0.005')
    minimum_fee: Decimal = D('1')
    slippage_bps: Decimal = D('5')
    execution_mode: str = 'next_session_open'

    def __post_init__(self):
        if not _decimal(self.initial_cash, positive=True):
            raise ValueError('initial_cash must be positive finite Decimal')
        if any(not _decimal(x) for x in (self.fee_per_share, self.minimum_fee, self.slippage_bps)):
            raise ValueError('Costs must be nonnegative finite Decimal values')
        if self.slippage_bps >= 10000:
            raise ValueError('slippage_bps must be below 10000')
        if self.execution_mode not in ('next_session_open', 'next_session_close_proxy'):
            raise ValueError('Unsupported execution_mode')


@dataclass
class _Position:
    shares: int = 0
    basis: Decimal = ZERO
    realized: Decimal = ZERO


def simulate_portfolio(sessions, bars, targets=(), config=PortfolioConfig(), *, corporate_actions=(), strategy=None):
    """Replay target-weight plans with one shared cash account, without signals.

    The caller supplies the complete ordered trading-session schedule, even
    when a symbol has no bar on a session. Missing execution prices cancel that
    session's order; they never defer it to a later observed bar. A target is a
    complete portfolio: omitted holdings have target zero; no target means hold.

    Cash distributions use the explicitly requested ex-date proxy, not actual
    payment dates. Unsupported/unverified held events invalidate performance
    and stop rebalancing; they never invent liquidation or a zero position.
    """
    if not isinstance(config, PortfolioConfig):
        raise ValueError('config must be PortfolioConfig')
    sessions, bars, targets = tuple(sessions), tuple(bars), tuple(targets)
    if strategy is not None and (not callable(strategy) or targets):
        raise ValueError('Use either static targets or a callable strategy')
    if not sessions or not bars:
        raise ValueError('Explicit sessions and nonempty price data are required')
    if any(not isinstance(s, TradingSession) for s in sessions):
        raise ValueError('sessions must contain TradingSession objects')
    if any(a.session_date >= b.session_date or a.close_time > b.open_time
           for a, b in zip(sessions, sessions[1:])):
        raise ValueError('Session schedule must be unique, chronological and nonoverlapping')
    if len({s.kind for s in sessions}) != 1:
        raise ValueError('Do not mix regular sessions and UTC-day proxies')
    if config.execution_mode == 'next_session_open' and sessions[0].kind != 'regular':
        raise ValueError('UTC daily open cannot represent regular-session open')
    indices = {s.session_date: i for i, s in enumerate(sessions)}
    actions_by_day = action_schedule(tuple(corporate_actions), indices)
    if actions_by_day and (config.execution_mode!='next_session_open' or sessions[0].kind!='regular'):
        raise ValueError('Corporate actions require regular-session open execution')
    by_day = {s.session_date: {} for s in sessions}
    for bar in bars:
        if not isinstance(bar, SessionBar) or bar.session_date not in indices:
            raise ValueError('Price bar must belong to the explicit session schedule')
        day = by_day[bar.session_date]
        if bar.symbol in day:
            raise ValueError('Duplicate symbol/session price')
        day[bar.symbol] = bar
    target_by_day = {}
    def validate_target(target):
        if not isinstance(target, TargetWeights) or target.signal_session not in indices:
            raise ValueError('Target session must belong to the explicit session schedule')
        session_index = indices[target.signal_session]
        session = sessions[session_index]
        if target.signal_time < session.close_available_time:
            raise ValueError('Signal precedes availability of its session close')
        if session_index + 1 < len(sessions):
            following = sessions[session_index + 1]
            fill_time = (following.open_time if config.execution_mode == 'next_session_open'
                         else following.close_time)
            if target.signal_time >= fill_time:
                raise ValueError('Signal must precede the next scheduled execution time')
    for target in targets:
        validate_target(target)
        if target.signal_session in target_by_day:
            raise ValueError('At most one portfolio target per signal session')
        target_by_day[target.signal_session] = target

    cash = config.initial_cash
    positions, last_closes = {}, {}
    orders, trades, daily, position_rows, rebalances = [], [], [], [], []
    action_rows, blocked_actions = [], set()
    dividend_income = ZERO
    pending = []
    pending_split_factors = {}
    pending_budget, pending_symbol_budgets = None, {}
    limit_audit = []
    fees = slippage = ZERO
    peak = previous_valid_equity = config.initial_cash
    invalid_dates = []
    observed_max_drawdown = ZERO
    slip = config.slippage_bps / D(10000)

    def commission(qty):
        return max(config.minimum_fee, config.fee_per_share * qty) if qty else ZERO

    def affordable(price, cap):
        lo, hi = 0, min(cap, int(cash / price))
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if price * mid + commission(mid) <= cash:
                lo = mid
            else:
                hi = mid - 1
        return lo

    for i, session in enumerate(sessions):
        day = by_day[session.session_date]
        execution_time = (session.open_time if config.execution_mode == 'next_session_open'
                          else session.close_time)
        fractional_settlements = []
        for action in actions_by_day.get(session.session_date,()):
            symbol=action.symbol
            pos=positions.get(symbol)
            shares=pos.shares if pos else 0
            row=dict(event_id=action.event_id,symbol=symbol,kind=action.kind,
                effective_session=session.session_date,event_time=session.open_time,
                status='applied',reason='',shares_before=shares,shares_after=shares,
                ratio=action.ratio,cash_per_share=action.cash_per_share,entitled_shares=ZERO,
                ratio_numerator=action.exact_ratio.numerator if action.kind=='split' else None,
                ratio_denominator=action.exact_ratio.denominator if action.kind=='split' else None,
                cash_delta=ZERO,cost_basis_removed=ZERO,realized_pnl_delta=ZERO,
                portfolio_cash_after=cash,basis_after=pos.basis if pos else ZERO)
            action_rows.append(row)
            if action.kind=='resolve_unheld' and action.verified and action.available_at<=session.open_time and not shares:
                blocked_actions.discard(symbol)
                row.update(status='resolved_unheld',reason='explicit_checked_resolution_no_held_entitlement')
                continue
            if action.kind in ('cash_dividend','cash_distribution') and not shares:
                row.update(status='not_applicable_zero_entitlement',reason='no_previous_close_ownership')
                continue
            if (symbol in blocked_actions or action.kind=='unsupported' or not action.verified
                    or action.available_at>session.open_time or action.kind=='resolve_unheld'):
                blocked_actions.add(symbol)
                row.update(status='blocked',reason='unresolved_or_unsupported_action_or_basis' if
                    action.available_at<=session.open_time else 'event_not_available_at_open')
                continue
            if action.kind=='split':
                exact_shares=Fraction(shares)*action.exact_ratio
                whole=exact_shares.numerator//exact_shares.denominator
                new_shares=D(exact_shares.numerator)/D(exact_shares.denominator)
                fraction=new_shares-whole
                bar=day.get(symbol)
                if fraction and (bar is None or bar.open is None or not bar.tradable_at_execution):
                    blocked_actions.add(symbol)
                    row.update(status='blocked',reason='missing_fractional_cash_in_lieu_open')
                    continue
                if pos:
                    pos.shares=new_shares
                if symbol in last_closes:
                    mark,mark_date=last_closes[symbol]
                    ratio=action.exact_ratio
                    last_closes[symbol]=(mark*D(ratio.denominator)/D(ratio.numerator),mark_date)
                row['shares_after']=new_shares
                if fraction:
                    fractional_settlements.append((action,pos,whole,fraction,bar.open))
                elif pos:
                    pos.shares=whole
                for order in pending:
                    if order['symbol']==symbol:
                        factor=pending_split_factors[order['order_id']]*action.exact_ratio
                        pending_split_factors[order['order_id']]=factor
                        order['split_ratio_applied']=D(factor.numerator)/D(factor.denominator)
                        exact_target=order['signal_target_shares']*factor
                        order['target_shares']=exact_target.numerator//exact_target.denominator
                        # Fractions will be cashed before ordinary orders.
                        delta=order['target_shares']-whole
                        order['requested_shares']=abs(delta)
                        if not delta:
                            order['status']='cancelled_split_rounding'
                        elif (delta>0)!=(order['side']=='BUY'):
                            raise AssertionError('A mechanical split reversed an order')
            else:
                entitled=D(shares)
                amount=entitled*action.cash_per_share
                cash+=amount
                dividend_income+=amount
                row.update(entitled_shares=entitled,cash_delta=amount,portfolio_cash_after=cash)
        for action,pos,whole,fraction,reference in fractional_settlements:
            old_shares=pos.shares
            removed=pos.basis if whole==0 else pos.basis*fraction/old_shares
            proceeds=fraction*reference
            pos.shares=whole
            pos.basis-=removed
            pos.realized+=proceeds-removed
            cash+=proceeds
            action_rows.append(dict(event_id=action.event_id+':cash_in_lieu',symbol=action.symbol,
                kind='cash_in_lieu',effective_session=session.session_date,event_time=session.open_time,
                status='applied',reason='research_open_price_cash_in_lieu_proxy',
                shares_before=old_shares,shares_after=whole,ratio=None,cash_per_share=reference,
                entitled_shares=fraction,cash_delta=proceeds,cost_basis_removed=removed,
                realized_pnl_delta=proceeds-removed,portfolio_cash_after=cash,basis_after=pos.basis))
        blocked_holdings=sorted(s for s in blocked_actions if s in positions and positions[s].shares)
        # Each order gets exactly this attempt. Sorting is independent of bar order.
        for order in sorted(pending, key=lambda o: (
                o['side'] == 'BUY', o['buy_priority'] if o['side'] == 'BUY' else 0, o['symbol'])):
            order['attempt_session'] = session.session_date
            order['attempt_time'] = execution_time
            if order['status']=='cancelled_split_rounding':
                continue
            if blocked_holdings or order['symbol'] in blocked_actions:
                order['status']='cancelled_unresolved_corporate_action'
                continue
            bar = day.get(order['symbol'])
            reference = None if bar is None else (bar.open if config.execution_mode == 'next_session_open'
                                                   else bar.close)
            if bar is None or reference is None:
                order['status'] = 'cancelled_missing_execution_price'
                continue
            if not bar.tradable_at_execution:
                order['status'] = 'cancelled_not_tradable'
                continue
            if not execution_time > order['signal_time']:
                raise AssertionError('Execution must follow signal availability')
            buy = order['side'] == 'BUY'
            price = reference * (1 + slip if buy else 1 - slip)
            pos = positions.get(order['symbol'], _Position())
            qty = (affordable(price, order['requested_shares']) if buy
                   else min(pos.shares, order['requested_shares']))
            unconstrained_qty = qty
            if pending_budget is not None:
                qty = min(qty, int(pending_budget / price))
            if pending_symbol_budgets:
                qty = min(qty, int(pending_symbol_budgets.get(order['symbol'], ZERO) / price))
            budget_constrained = qty < unconstrained_qty
            if budget_constrained:
                limit_audit.append(dict(session_date=session.session_date, order_id=order['order_id'],
                    symbol=order['symbol'], requested_after_cash=unconstrained_qty, allowed_shares=qty,
                    reason='execution_notional_budget', total_budget_before=pending_budget))
            fee = commission(qty)
            notional = price * qty
            cash_delta = -notional - fee if buy else notional - fee
            if cash + cash_delta < 0:
                qty, fee, notional, cash_delta = 0, ZERO, ZERO, ZERO
            if not qty:
                order['status'] = 'cancelled_trade_budget' if budget_constrained and unconstrained_qty else 'cancelled_insufficient_cash'
                continue
            if buy:
                pos.shares += qty
                pos.basis += notional + fee
            else:
                removed_basis = pos.basis if qty == pos.shares else pos.basis * qty / pos.shares
                pos.basis -= removed_basis
                pos.realized += notional - fee - removed_basis
                pos.shares -= qty
            positions[order['symbol']] = pos
            cash += cash_delta
            if pending_budget is not None:
                pending_budget -= notional
            if pending_symbol_budgets:
                pending_symbol_budgets[order['symbol']] -= notional
            cost = abs(price - reference) * qty
            fees += fee
            slippage += cost
            order.update(filled_shares=qty, fill_session=session.session_date,
                         status='filled' if qty == order['requested_shares']
                         else 'partial_remainder_cancelled')
            trades.append(dict(order_id=order['order_id'], symbol=order['symbol'], side=order['side'],
                               signal_session=order['signal_session'], signal_time=order['signal_time'],
                               fill_session=session.session_date, fill_time=execution_time,
                               reference_price=reference, fill_price=price, shares=qty,
                               notional=notional, commission=fee, slippage_cost=cost,
                               cash_delta=cash_delta, portfolio_cash_after=cash,
                               shares_after=pos.shares, cost_basis_after=pos.basis))
        pending = []

        for symbol, bar in day.items():
            if bar.close is not None:
                last_closes[symbol] = (bar.close, session.session_date)
        stale = sorted(s for s, p in positions.items() if p.shares and (
            s not in day or day[s].close is None or s in blocked_actions))
        estimated_market_value = ZERO
        for symbol, pos in sorted(positions.items()):
            mark, mark_date = last_closes[symbol]
            value = pos.shares * mark
            estimated_market_value += value
            position_rows.append(dict(session_date=session.session_date, symbol=symbol,
                                      shares=pos.shares, mark_price=mark, mark_session=mark_date,
                                      valuation_valid=not pos.shares or symbol not in stale,
                                      estimated_market_value=value, cost_basis=pos.basis,
                                      realized_pnl=pos.realized, estimated_unrealized_pnl=value-pos.basis))
        estimated_equity = cash + estimated_market_value
        realized = sum((p.realized for p in positions.values()), ZERO)
        basis = sum((p.basis for p in positions.values()), ZERO)
        if cash < 0 or any(p.shares < 0 or p.basis < 0 for p in positions.values()):
            raise AssertionError('Negative cash, shares or cost basis')
        if abs(cash - config.initial_cash - sum((t['cash_delta'] for t in trades), ZERO)
               -sum((r['cash_delta'] for r in action_rows),ZERO)) > TOLERANCE:
            raise AssertionError('Cash ledger reconciliation failed')
        if abs(estimated_equity-config.initial_cash-realized-dividend_income-(estimated_market_value-basis)) > TOLERANCE:
            raise AssertionError('PnL reconciliation failed')
        valid = not stale
        equity = estimated_equity if valid else None
        day_return = drawdown = None
        if valid:
            day_return = equity / previous_valid_equity - 1 if previous_valid_equity is not None and previous_valid_equity > 0 else None
            peak = max(peak, equity)
            drawdown = equity / peak - 1
            observed_max_drawdown = max(observed_max_drawdown, -drawdown)
        else:
            invalid_dates.append(session.session_date)
        previous_valid_equity = equity
        daily.append(dict(session_date=session.session_date, cash=cash,
                          market_value=estimated_market_value if valid else None, equity=equity,
                          estimated_market_value=estimated_market_value, estimated_equity=estimated_equity,
                          net_value=equity/config.initial_cash if valid else None,
                          daily_return=day_return, drawdown=drawdown, valuation_valid=valid,
                          stale_symbols=tuple(stale), realized_pnl=realized,
                          unrealized_pnl=estimated_market_value-basis if valid else None,
                          equity_exposure=estimated_market_value/equity if valid and equity > 0 else None,
                          cumulative_commission=fees, cumulative_slippage=slippage,
                          cumulative_dividend_income=dividend_income,
                          blocked_action_symbols=tuple(blocked_holdings)))

        target = target_by_day.get(session.session_date)
        if strategy is not None:
            held = {s: p.shares for s, p in positions.items() if p.shares}
            snapshot = AccountSnapshot(session.session_date, session.close_available_time, cash, equity,
                MappingProxyType(held), MappingProxyType({s: held[s]*last_closes[s][0]/equity
                    for s in held} if equity is not None and equity > 0 else {}), drawdown, valid)
            target = strategy(snapshot)
            if target is not None:
                validate_target(target)
                if target.signal_session != session.session_date:
                    raise ValueError('Dynamic target must belong to the current session')
        if target is None:
            continue
        missing = sorted(s for s, weight in target.weights.items() if weight > 0 and (
            s not in day or day[s].close is None))
        rebalance = dict(signal_session=session.session_date, signal_time=target.signal_time,
                         weights=dict(target.weights), target_equity_weight=sum(target.weights.values(), ZERO),
                         signal_equity=equity, status='planned', reason='', order_count=0)
        rebalances.append(rebalance)
        if blocked_holdings or any(target.weights.get(s,ZERO)>0 for s in blocked_actions):
            rebalance.update(status='skipped_unresolved_corporate_action',reason=','.join(sorted(blocked_actions)))
            continue
        guarded = sorted(s for s in target.unsafe_holding_symbols if s in positions and positions[s].shares)
        if guarded:
            rebalance.update(status='skipped_unsafe_holding_data', reason='Unverified holding data: ' + ','.join(guarded))
            continue
        if not valid or missing:
            rebalance.update(status='skipped_missing_signal_prices',
                             reason='Missing current closes: ' + ','.join(sorted(set(stale + missing))))
            continue
        next_session = sessions[i+1] if i+1 < len(sessions) else None
        pending_budget = target.maximum_trade_notional
        pending_symbol_budgets = dict(target.symbol_trade_notionals)
        priority = {s: j+1 for j, s in enumerate(target.buy_priority)}
        desired = set(target.weights) | {s for s, p in positions.items() if p.shares}
        for symbol in sorted(desired):
            weight = target.weights.get(symbol, ZERO)
            if weight > 0:
                close = day[symbol].close
                target_shares = int((weight * equity / close).to_integral_value(rounding=ROUND_FLOOR))
            else:
                target_shares = 0
                close = day[symbol].close if symbol in day else None
            old_shares = positions[symbol].shares if symbol in positions else 0
            change = target_shares - old_shares
            if not change:
                continue
            buy = change > 0
            order = dict(order_id=len(orders)+1, symbol=symbol, side='BUY' if buy else 'SELL',
                         signal_session=session.session_date, signal_time=target.signal_time,
                         signal_equity=equity, signal_close=close, target_weight=weight,
                         target_shares=target_shares, requested_shares=abs(change), filled_shares=0,
                         signal_target_shares=target_shares,signal_requested_shares=abs(change),split_ratio_applied=D(1),
                         buy_priority=priority.get(symbol) if buy else None,
                         execution_session=next_session.session_date if next_session else None,
                         attempt_session=None, attempt_time=None, fill_session=None,
                         status='pending_end_of_sample')
            orders.append(order)
            pending_split_factors[order['order_id']]=Fraction(1)
            pending.append(order)
            rebalance['order_count'] += 1

    final = daily[-1]
    final_equity = final['equity']
    return dict(orders=orders, trades=trades, daily=daily, positions=position_rows, rebalances=rebalances,
                corporate_actions=action_rows, execution_limits=limit_audit,
                summary=dict(initial_cash=config.initial_cash, final_cash=cash,
                             final_market_value=final['market_value'], final_equity=final_equity,
                             estimated_final_equity=final['estimated_equity'],
                             net_pnl=final_equity-config.initial_cash if final_equity is not None else None,
                             return_pct=(final_equity/config.initial_cash-1)*100 if final_equity is not None else None,
                             max_drawdown_pct=observed_max_drawdown*100 if not invalid_dates else None,
                             observed_max_drawdown_pct=observed_max_drawdown*100,
                             commissions=fees, slippage_cost=slippage, filled_orders=len(trades),
                             cancelled_orders=sum(o['status'].startswith('cancelled_') for o in orders),
                             pending_orders=sum(o['status']=='pending_end_of_sample' for o in orders),
                             skipped_rebalances=sum(r['status']!='planned' for r in rebalances),
                             final_shares={s: p.shares for s, p in sorted(positions.items())},
                             performance_complete=not invalid_dates, invalid_valuation_sessions=tuple(invalid_dates),
                             accounting_checks_passed=True, execution_mode=config.execution_mode,
                             session_kind=sessions[0].kind, corporate_actions_supported=True,
                             corporate_action_events_supplied=sum(len(a) for a in actions_by_day.values()),
                             supported_corporate_action_kinds=('split','cash_dividend','cash_distribution','resolve_unheld'),
                             corporate_action_ledger_rows=len(action_rows),dividend_income=dividend_income,
                             corporate_action_cash_delta=sum((r['cash_delta'] for r in action_rows),ZERO),
                             blocked_corporate_action_symbols=tuple(sorted(blocked_actions)),
                             corporate_action_cash_policy='gross_ex_date_cash_and_open_cash_in_lieu_research_proxy'))
