"""Offline V1 signal/target fixture or local data check; no downloads/orders."""
import argparse
import csv
import datetime as dt
import json
from collections import Counter
from dataclasses import asdict
from decimal import Decimal as D

from examples.run_portfolio import ROOT, encode, write_report
from quant_backtest import PortfolioConfig, SessionBar, TradingSession, simulate_portfolio
from quant_data import load_daily_bars
from quant_portfolio import (DataReadiness, MomentumCalendar, UniverseMember,
                             build_momentum_targets, load_momentum_config)
from quant_signals import MomentumObservation


def synthetic_fixture():
    """Fictional prices/universe on an explicit July-September test calendar."""
    start, coverage_end, data_end = dt.date(2026, 7, 1), dt.date(2026, 10, 4), dt.date(2026, 9, 30)
    holidays = {dt.date(2026, 7, 3), dt.date(2026, 9, 7)}
    utc = dt.timezone.utc
    sessions = []
    for i in range((coverage_end-start).days+1):
        day = start+dt.timedelta(days=i)
        if day.weekday() < 5 and day not in holidays:
            close = dt.datetime.combine(day, dt.time(20), utc)
            sessions.append(TradingSession(day, dt.datetime.combine(day, dt.time(13, 30), utc),
                                            close, close+dt.timedelta(minutes=5)))
    calendar = MomentumCalendar(tuple(sessions), start, coverage_end)
    sectors = ['Information Technology', 'Industrials', 'Health Care', 'Financials', 'Consumer Staples']
    universe = tuple(UniverseMember('DEMO%02d' % j, sectors[j//8],
                                    '%010d' % (1 if j == 1 else j+1)) for j in range(40))
    data_sessions = tuple(s for s in sessions if s.session_date <= data_end)
    last = {m.symbol: D(100) for m in universe}
    observations, prices = [], []
    for i, session in enumerate(data_sessions):
        for j, member in enumerate(universe):
            opening = last[member.symbol]
            rate = D(40-j if i < 35 else j+1)/10000
            close = opening*(1+rate)
            last[member.symbol] = close
            observations.append(MomentumObservation(member.symbol, session.session_date,
                session.close_available_time, close, close, D(50000000), True, True, True))
            prices.append(SessionBar(member.symbol, session.session_date, opening, close))
    return calendar, data_sessions, tuple(prices), tuple(observations), universe


def check_local_b():
    data = load_daily_bars(ROOT/'experiments/databento_smoke/data/b')
    readiness = DataReadiness()
    reasons = {
        'regular_session_verified': '现有柱按 UTC 自然日聚合，非已验证的常规时段日线。',
        'point_in_time_adjustment_verified': '未复权，缺少按当时可用版本保存的复权序列。',
        'unadjusted_share_prices_verified': '字段声明未复权，但公司行动及外部价格对账尚未完成。',
        'consolidated_turnover_verified': '仅有组成场所成交量，缺少全市场常规时段美元成交额。',
        'publication_times_verified': '实际发布时刻未知，柱终点只是最早可能完整的边界。',
    }
    report = dict(status='data_not_ready', source=asdict(data.info), target_event_count=0,
                  action='保留已有仓位；不生成空权重清仓指令。',
                  missing_checks=[dict(check=k, reason=reasons[k]) for k in readiness.missing_checks])
    output = ROOT/'outputs/momentum_b_readiness'
    output.mkdir(parents=True, exist_ok=True)
    (output/'data_readiness.json').write_text(json.dumps(report, default=encode, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, default=encode, ensure_ascii=False, indent=2))
    print('Saved:', output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-local-b', action='store_true', help='Report known B-data gaps; do not generate trading targets')
    args = parser.parse_args()
    rules, frozen_universe, cfg = load_momentum_config(ROOT/'configs/momentum_v1.json')
    if args.check_local_b:
        check_local_b()
        return
    calendar, sessions, prices, observations, universe = synthetic_fixture()
    readiness = DataReadiness(True, True, True, True, True)
    as_of = sessions[-1].close_time+dt.timedelta(minutes=rules.cutoff_minutes)
    strategy = build_momentum_targets(calendar, observations, universe, readiness, as_of=as_of, rules=rules)
    account_cfg = PortfolioConfig(initial_cash=D(cfg['account']['research_initial_cash']),
        fee_per_share=D(cfg['costs']['commission_usd_per_share']),
        minimum_fee=D(cfg['costs']['minimum_commission_usd_per_filled_order']),
        slippage_bps=D(cfg['costs']['slippage_bps_per_side']))
    result = simulate_portfolio(sessions, prices, strategy['targets'], account_cfg)
    output = ROOT/'outputs/momentum_synthetic'
    source = dict(kind='synthetic_momentum_pipeline', strategy_id=cfg['strategy_id'], rule_version=cfg['rule_version'],
        description='40 fictional symbols and invented prices/turnover, with no corporate actions; not market performance.',
        universe_override='synthetic_40_not_the_frozen_sp500_snapshot',
        frozen_universe_verified_count=len(frozen_universe), readiness=asdict(readiness),
        calendar_coverage=[calendar.coverage_start, calendar.coverage_end], as_of=as_of)
    write_report(result, account_cfg, source, output,
                 purpose='Synthetic momentum selection/account integration; not historical market performance.')
    audit = dict(source=source, decisions=strategy['decisions'])
    (output/'strategy_audit.json').write_text(json.dumps(audit, default=encode, ensure_ascii=False, indent=2)+'\n')
    planned = [dict(signal_session=t.signal_session, signal_time=t.signal_time, weights=dict(t.weights),
                    buy_priority=t.buy_priority, unsafe_holding_symbols=t.unsafe_holding_symbols)
               for t in strategy['targets']]
    (output/'planned_targets.json').write_text(json.dumps(planned, default=encode, ensure_ascii=False, indent=2)+'\n')
    headers = ['signal_session', 'signal_time', 'rank', 'symbol', 'sector', 'issuer_cik', 'momentum',
               'average_turnover_usd', 'target_weight', 'portfolio_equity_weight', 'portfolio_cash_weight']
    with (output/'weekly_targets.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for decision in strategy['decisions']:
            for candidate in decision['candidates']:
                if candidate['selected']:
                    row = {k: candidate[k] for k in headers if k in candidate}
                    row.update(signal_session=decision['signal_session'], signal_time=decision['signal_time'],
                               portfolio_equity_weight=decision['target_equity_weight'],
                               portfolio_cash_weight=decision['target_cash_weight'])
                    writer.writerow(row)
    summary = dict(status='synthetic_pipeline_complete', price_sessions=len(sessions),
                   universe_size=len(universe), decision_statuses=dict(Counter(d['status'] for d in strategy['decisions'])),
                   target_events=len(strategy['targets']), selected_counts=[len(t.weights) for t in strategy['targets']],
                   filled_orders=result['summary']['filled_orders'],
                   accounting_checks_passed=result['summary']['accounting_checks_passed'])
    print(json.dumps(summary, default=encode, ensure_ascii=False, indent=2))
    print('Saved:', output)


if __name__ == '__main__':
    main()
