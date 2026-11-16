"""Net daily research statistics; invalid observations never become zero returns."""
import datetime as dt
import math
import statistics
from decimal import Decimal as D


def performance_metrics(result, *, start, end_exclusive, initial_cash):
    daily = result['daily']
    indices = [i for i, r in enumerate(daily) if start <= r['session_date'] < end_exclusive]
    if not indices:
        return dict(status='no_observations')
    rows = [daily[i] for i in indices]
    invalid = [r['session_date'] for r in rows if not r['valuation_valid']]
    before = daily[indices[0]-1]['equity'] if indices[0] else initial_cash
    if invalid or before is None or any(r['daily_return'] is None for r in rows):
        return dict(status='invalid_valuation', invalid_dates=invalid,
                    first_session=rows[0]['session_date'], last_session=rows[-1]['session_date'])
    returns = [float(r['daily_return']) for r in rows]
    std = statistics.stdev(returns) if len(returns) > 1 else 0
    total = float(rows[-1]['equity'] / before - 1)
    peak, max_dd = before, D(0)
    for row in rows:
        peak = max(peak, row['equity'])
        max_dd = max(max_dd, 1-row['equity']/peak)
    turnovers = []
    previous = {r['session_date']: daily[i-1]['equity'] if i else initial_cash for i, r in enumerate(daily)}
    trades = [t for t in result['trades'] if start <= t['fill_session'] < end_exclusive]
    notionals = {}
    for t in trades:
        notionals[t['fill_session']] = notionals.get(t['fill_session'], D(0)) + t['notional']
    for row in rows:
        turnovers.append(float(notionals.get(row['session_date'], D(0)) / previous[row['session_date']]))
    weekly = {}
    for row in rows:
        monday = row['session_date'] - dt.timedelta(days=row['session_date'].weekday())
        weekly[monday] = weekly.get(monday, D(1)) * (1 + row['daily_return'])
    return dict(status='valid_exploratory', first_session=rows[0]['session_date'],
        last_session=rows[-1]['session_date'], trading_sessions=len(rows),
        cumulative_net_return=total,
        annualized_return=(1+total)**(252/len(rows))-1,
        annualized_sharpe=statistics.mean(returns)/std*math.sqrt(252) if std else None,
        maximum_drawdown=float(max_dd), annualized_volatility=std*math.sqrt(252),
        positive_week_fraction=sum(x > 1 for x in weekly.values())/len(weekly),
        mean_daily_two_way_turnover=statistics.mean(turnovers),
        cumulative_two_way_turnover=sum(turnovers),
        commission_usd=str(sum((t['commission'] for t in trades), D(0))),
        slippage_attribution_usd=str(sum((t['slippage_cost'] for t in trades), D(0))),
        mean_equity_exposure=statistics.mean(float(r['equity_exposure']) for r in rows),
        flat_sessions=sum(r['market_value'] == 0 for r in rows),
        sessions_below_50pct_exposure=sum(r['equity_exposure'] < D('.5') for r in rows),
        filled_orders=len(trades))
