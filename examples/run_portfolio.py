"""Shared-account fixtures: python3 -m examples.run_portfolio [--sample-b]."""
import argparse
import csv
import datetime as dt
import json
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path

from quant_backtest import PortfolioConfig, SessionBar, TargetWeights, TradingSession, simulate_portfolio
from quant_data import load_daily_bars

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {
    'orders': ['order_id', 'symbol', 'side', 'signal_session', 'signal_time', 'signal_equity',
               'signal_close', 'target_weight', 'target_shares', 'requested_shares', 'filled_shares',
               'buy_priority', 'execution_session', 'attempt_session', 'attempt_time', 'fill_session', 'status',
               'signal_target_shares','signal_requested_shares','split_ratio_applied'],
    'trades': ['order_id', 'symbol', 'side', 'signal_session', 'signal_time', 'fill_session', 'fill_time',
               'reference_price', 'fill_price', 'shares', 'notional', 'commission', 'slippage_cost',
               'cash_delta', 'portfolio_cash_after', 'shares_after', 'cost_basis_after'],
    'daily': ['session_date', 'cash', 'market_value', 'equity', 'estimated_market_value', 'estimated_equity',
              'net_value', 'daily_return', 'drawdown', 'valuation_valid', 'stale_symbols', 'realized_pnl',
              'unrealized_pnl', 'equity_exposure', 'cumulative_commission', 'cumulative_slippage',
              'cumulative_dividend_income','blocked_action_symbols'],
    'positions': ['session_date', 'symbol', 'shares', 'mark_price', 'mark_session', 'valuation_valid',
                  'estimated_market_value', 'cost_basis', 'realized_pnl', 'estimated_unrealized_pnl'],
    'rebalances': ['signal_session', 'signal_time', 'weights', 'target_equity_weight',
                   'signal_equity', 'status', 'reason', 'order_count'],
    'corporate_actions': ['event_id','symbol','kind','effective_session','event_time','status','reason',
        'shares_before','shares_after','ratio','ratio_numerator','ratio_denominator','cash_per_share','entitled_shares','cash_delta',
        'cost_basis_removed','realized_pnl_delta','portfolio_cash_after','basis_after'],
}


def encode(value):
    if isinstance(value, D):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    raise TypeError('Cannot serialize ' + type(value).__name__)


def synthetic_fixture():
    utc = dt.timezone.utc
    dates = [dt.date(2026, 11, day) for day in (2, 3, 4)]
    schedule = [TradingSession(day, dt.datetime.combine(day, dt.time(14, 30), utc),
                               dt.datetime.combine(day, dt.time(21), utc),
                               dt.datetime.combine(day, dt.time(21, 30), utc)) for day in dates]
    prices = [SessionBar(symbol, day, price, price) for day in dates
              for symbol, price in [('AAA', D(10)), ('ZZZ', D(20))]]
    targets = [TargetWeights(dates[0], schedule[0].close_available_time, {'AAA': D('.4'), 'ZZZ': D('.4')}),
               TargetWeights(dates[1], schedule[1].close_available_time, {'ZZZ': D('.8')})]
    return schedule, prices, targets, dict(kind='synthetic_fixed_prices',
        description='Invented symbols/prices; 40%+40% then 80% validates accounting, not the V1 selection rules.')


def local_b_fixture():
    data = load_daily_bars(ROOT / 'experiments/databento_smoke/data/b')
    grouped = {}
    for bar in data.bars:
        grouped.setdefault(bar.provider_bar_date, []).append(bar)
    schedule = []
    for day, group in sorted(grouped.items()):
        intervals = {(b.bar_start_utc, b.bar_end_utc) for b in group}
        if len(intervals) != 1:
            raise ValueError('Inconsistent UTC day intervals')
        start, end = intervals.pop()
        schedule.append(TradingSession(day, start, end, end, 'utc_calendar_day_proxy'))
    prices = [SessionBar(b.symbol, b.provider_bar_date, None, b.close) for b in data.bars]
    lookup = {s.session_date: s for s in schedule}
    # Fixed fixture dates, not dates selected from future prices or sample length.
    first, second = dt.date(2026, 7, 1), dt.date(2026, 8, 3)
    if first not in lookup or second not in lookup:
        raise ValueError('Local B fixture requires July 1 and August 3, 2026')
    targets = [TargetWeights(first, lookup[first].close_available_time,
                            {'AAPL': D('.4'), 'MSFT': D('.4')}),
               TargetWeights(second, lookup[second].close_available_time, {'SPY': D('.8')})]
    return schedule, prices, targets, dict(kind='local_B_UTC_close_proxy', data_info=asdict(data.info),
        description='Fixed demonstration targets; publication at bar end and tradability assumed, not verified.')


def write_report(result, config, source, output, *, purpose=None):
    output.mkdir(parents=True, exist_ok=True)
    for name, headers in HEADERS.items():
        with (output / (name + '.csv')).open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for row in result[name]:
                writer.writerow({key: ('' if value is None else
                    json.dumps(value, default=encode, ensure_ascii=False) if isinstance(value, (dict, tuple, list))
                    else encode(value) if isinstance(value, (D, dt.date, dt.datetime)) else value)
                    for key, value in row.items()})
    report = dict(config=asdict(config), source=source, **result['summary'], limitations=[
        purpose or 'Accounting fixture, not a momentum strategy backtest or verified executable performance.',
        'Only explicitly supplied evidenced actions are applied; no interest, tax, settlement delay or volume/market-impact model.',
        'Cash distributions use ex-date cash and split fractions use open-price cash-in-lieu proxies, not broker payment schedules.',
        'Prices and close availability must match the declared session; UTC close proxy is a separate mode.',
        'Slippage is already in fill prices; its attribution must not be deducted again.',
    ])
    (output / 'summary.json').write_text(json.dumps(report, default=encode, ensure_ascii=False, indent=2) + '\n')
    (output / 'targets.json').write_text(json.dumps(result['rebalances'], default=encode, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample-b', action='store_true', help='Use cached B UTC close proxy instead of invented prices')
    args = parser.parse_args()
    frozen = json.loads((ROOT / 'configs/momentum_v1.json').read_text())
    cfg = PortfolioConfig(initial_cash=D(frozen['account']['research_initial_cash']),
                          fee_per_share=D(frozen['costs']['commission_usd_per_share']),
                          minimum_fee=D(frozen['costs']['minimum_commission_usd_per_filled_order']),
                          slippage_bps=D(frozen['costs']['slippage_bps_per_side']),
                          execution_mode='next_session_close_proxy' if args.sample_b else 'next_session_open')
    schedule, prices, targets, source = local_b_fixture() if args.sample_b else synthetic_fixture()
    result = simulate_portfolio(schedule, prices, targets, cfg)
    output = ROOT / 'outputs' / ('portfolio_b' if args.sample_b else 'portfolio_synthetic')
    write_report(result, cfg, source, output)
    print(json.dumps(result['summary'], default=encode, ensure_ascii=False, indent=2))
    print('Saved:', output)


if __name__ == '__main__':
    main()
