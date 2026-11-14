"""Offline real-cache momentum plus two matched benchmarks; no network or broker."""
import argparse
import csv
import datetime as dt
import json
from collections import Counter
from decimal import Decimal as D
from pathlib import Path

from quant_backtest import PortfolioConfig, simulate_portfolio
from quant_backtest.research_metrics import performance_metrics
from quant_data.public_research import load_public_research
from quant_data.public_universe import digest, save_json
from quant_portfolio.public_momentum import build_public_targets
from examples.run_portfolio import write_report

ROOT = Path(__file__).resolve().parents[1]
ASSUMPTIONS = [
    '使用2026-11-03冻结成分及行业快照回看历史，存在选择和幸存者偏差。',
    '历史数据假定收盘后30分钟可用；真实下载时间单独保留，不代表历史真实发布时间。',
    '信号采用冻结的最新复权历史，不是逐时点复权版本回放。',
    '流动性采用来源收盘价乘成交量代理，不是精确全市场美元成交额。',
    '日线及次日开盘加滑点只是研究成交代理，不能重现开盘后5–20分钟的限价单。',
    '价格、股数及分红金额基准经过PUBLIC假设下的内部检查，尚未逐事件独立核实。',
    '沿用复杂公司行动的整段历史保护区间，使用后来已知事件排除更早候选，额外引入研究选择偏差。',
    '分配按除息日税前现金入账，拆股碎股按开盘价补偿；未模拟税收、利息和结算延迟。',
    '采用整数股和简化佣金；等权组合受向下取整和最低佣金影响，实际仓位可能远低于80%。',
    '期末持仓按收盘价估值，没有强制清仓及期末退出成本。',
    '时间子区间仅作描述，不是独立样本外测试；本次未执行参数搜索。周正收益比例包含边界的不完整周。',
]


def verify_account(result, sessions, initial_cash):
    """Independent source-ledger reconciliation plus next-session timing checks."""
    cash_delta, share_delta = {}, {}
    for t in result['trades']:
        amount = t['fill_price'] * t['shares']
        fee = t['commission']
        change = -amount-fee if t['side'] == 'BUY' else amount-fee
        if change != t['cash_delta']:
            raise AssertionError('Fill amount/cash mismatch')
        cash_delta[t['fill_session']] = cash_delta.get(t['fill_session'], D(0)) + change
        key = (t['fill_session'], t['symbol'])
        share_delta[key] = share_delta.get(key, D(0)) + (t['shares'] if t['side'] == 'BUY' else -t['shares'])
    for action in result['corporate_actions']:
        day = action['effective_session']
        cash_delta[day] = cash_delta.get(day, D(0)) + action['cash_delta']
        key = (day, action['symbol'])
        share_delta[key] = share_delta.get(key, D(0)) + action['shares_after']-action['shares_before']
    values, reported_shares = {}, {}
    for p in result['positions']:
        day = p['session_date']
        values[day] = values.get(day, D(0)) + p['shares'] * p['mark_price']
        reported_shares.setdefault(day, {})[p['symbol']] = p['shares']
        if p['shares'] < 0:
            raise AssertionError('Negative position')
    cash, holdings = initial_cash, {}
    deltas_by_day = {}
    for (day, symbol), delta in share_delta.items():
        deltas_by_day.setdefault(day, {})[symbol] = delta
    for row in result['daily']:
        day = row['session_date']
        cash += cash_delta.get(day, D(0))
        for symbol, delta in deltas_by_day.get(day, {}).items():
            holdings[symbol] = holdings.get(symbol, D(0)) + delta
        reported = reported_shares.get(day, {})
        if any(holdings.get(s, D(0)) != reported.get(s, D(0)) for s in holdings.keys() | reported.keys()):
            raise AssertionError('Independent daily share reconstruction failed')
        if (cash < 0 or abs(cash-row['cash']) > D('.00000001') or
                abs(cash+values.get(row['session_date'], D(0))-row['estimated_equity']) > D('.00000001')):
            raise AssertionError('Independent daily cash/NAV reconstruction failed')
    indices = {s.session_date: i for i, s in enumerate(sessions)}
    for t in result['trades']:
        next_session = sessions[indices[t['signal_session']]+1]
        if t['fill_session'] != next_session.session_date or not t['signal_time'] < t['fill_time']:
            raise AssertionError('Signal/execution timing mismatch')
    return dict(status='passed', daily_cash_and_NAV_rows=len(result['daily']),
                daily_shares_reconstructed=True,
                next_session_fills_checked=len(result['trades']))


def markdown_report(comparison):
    lines = ['# 公开数据动量基线与基准比较', '',
        '规则版本 PUBLIC-1.0.1；离线公开数据探索研究。未接入券商。', '',
        '评估起点：%s；截止：%s（不含）。预热期不计入以下指标。' %
        (comparison['evaluation_start'], comparison['evaluation_end_exclusive']), '',
        '| 组合 | 净收益 | 年化夏普 | 最大回撤 | 日均股票仓位 | 佣金（美元） |',
        '|---|---:|---:|---:|---:|---:|']
    for name, item in comparison['portfolios'].items():
        m = item['metrics']['full_period']
        if m['status'] != 'valid_exploratory':
            lines.append('| %s | 估值无效，见审计记录 | — | — | — | — |' % name)
            continue
        lines.append('| %s | %.2f%% | %s | %.2f%% | %.2f%% | %.2f |' %
            (name, m['cumulative_net_return']*100,
             '不可计算' if m['annualized_sharpe'] is None else '%.3f' % m['annualized_sharpe'],
             m['maximum_drawdown']*100, m['mean_equity_exposure']*100, float(m['commission_usd'])))
    lines += ['', '收益已扣佣金，滑点已包含在成交价中；滑点归因不可再次扣减。', '',
              '目标80%不代表实际80%。应结合实际仓位比较收益与回撤；股票池等权组合若明显低配，不能视为同风险敞口的对照。', '',
              '## 数据与核对', '',
              '- 股票池：%s个代码；全部保留在候选审计中。' % comparison['universe_security_count'],
              '- 信号状态：`%s`。' % comparison['decision_status_counts'],
              '- 各组合逐日现金、净值与信号后次日成交核对通过。', '',
              '## 研究局限', '']
    lines.extend('- ' + text for text in ASSUMPTIONS)
    lines += ['', '具体指标、子区间及基准收益差见 comparison.json；完整交易和账目见各组合子目录。', '']
    return '\n'.join(lines)


def run(input_path, output, config_path, review_path):
    # Record windows/config before reading any full-strategy return result.
    output.mkdir(parents=True, exist_ok=True)
    config = json.loads(config_path.read_text())
    protocol = dict(
        registered_at_utc=dt.datetime.now(dt.timezone.utc), config_sha256=digest(config_path),
        research_protocol=config['research_protocol'], assumptions=ASSUMPTIONS,
        parameter_search_performed=False, independent_out_of_sample=False)
    protocol_path = output / 'run_protocol.json'
    if protocol_path.exists():
        previous_protocol = json.loads(protocol_path.read_text())
        if (previous_protocol['config_sha256'] != protocol['config_sha256'] or
                previous_protocol['research_protocol'] != protocol['research_protocol']):
            raise ValueError('Output already has another registered configuration; use a new output directory')
    else:
        save_json(protocol_path, protocol)
    data = load_public_research(input_path, config_path, review_path)
    cfg, calendar = data['config'], data['calendar']
    built = build_public_targets(calendar, data['observations'], data['universe'],
                                as_of=calendar.sessions[-1].close_available_time)
    targets = built['targets']
    strategy_targets = targets['MOM20_20_EW80']
    index = {s.session_date: i for i, s in enumerate(calendar.sessions)}
    executable = [t for t in strategy_targets if index[t.signal_session]+1 < len(calendar.sessions)]
    if not executable:
        raise ValueError('No executable common target after warmup; see input gaps')
    start = calendar.sessions[index[executable[0].signal_session]+1].session_date
    end = calendar.coverage_end + dt.timedelta(days=1)
    accounting = PortfolioConfig(initial_cash=D(cfg['account']['research_initial_cash']),
        fee_per_share=D(cfg['costs']['commission_usd_per_share']),
        minimum_fee=D(cfg['costs']['minimum_commission_usd_per_filled_order']),
        slippage_bps=D(cfg['costs']['slippage_bps_per_side']))
    comparison = dict(rule_version=cfg['rule_version'], strategy_id=cfg['strategy_id'],
        evaluation_start=start, evaluation_end_exclusive=end,
        universe_security_count=len(data['universe']), source_price_rows=len(data['bars']),
        decision_status_counts=dict(Counter(d['status'] for d in built['decisions'])),
        fingerprints=data['fingerprints'], assumptions=ASSUMPTIONS,
        strict_v1_readiness_unchanged=True, broker_routing_enabled=False,
        independent_out_of_sample=False, paid_data_requests=0, portfolios={})
    save_json(output / 'config_snapshot.json', cfg)
    save_json(output / 'decisions.json', built['decisions'])
    save_json(output / 'input_inventory.json', data['inventory'])
    save_json(output / 'event_basis_audit.json', data['event_audit'])
    # Explicit time evidence is kept for every source observation, not relabeled.
    with (output / 'observation_times.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['symbol', 'session_date', 'model_available_at', 'actual_retrieved_at', 'source_sha256'])
        writer.writerows((o.symbol, o.session_date, o.model_available_at.isoformat(),
                          o.actual_retrieved_at.isoformat(), o.source_sha256) for o in data['observations'])
    nav = {r.session_date: dict(session_date=r.session_date) for r in calendar.sessions if r.session_date >= start}
    for name, plans in targets.items():
        symbols = {'SPY'} if name == 'SPY_80_CASH_20' else {m.symbol for m in data['universe']}
        print('Simulating', name, flush=True)
        result = simulate_portfolio(calendar.sessions, [b for b in data['bars'] if b.symbol in symbols],
            plans, accounting, corporate_actions=[a for a in data['actions'] if a.symbol in symbols])
        verification = verify_account(result, calendar.sessions, accounting.initial_cash)
        metrics = {'full_period': performance_metrics(result, start=start, end_exclusive=end,
                                                      initial_cash=accounting.initial_cash)}
        for window in cfg['research_protocol']['evaluation_windows']:
            metrics[window['id']] = performance_metrics(result,
                start=max(start, dt.date.fromisoformat(window['start'])),
                end_exclusive=min(end, dt.date.fromisoformat(window['end_exclusive'])),
                initial_cash=accounting.initial_cash)
        write_report(result, accounting, dict(rule_version=cfg['rule_version'],
                     assumptions=ASSUMPTIONS, fingerprints=data['fingerprints']), output / name,
                     purpose='Offline exploratory momentum/baseline comparison, not strict V1 or live executable evidence.')
        save_json(output / name / 'metrics.json', metrics)
        comparison['portfolios'][name] = dict(metrics=metrics, verification=verification,
            order_status_counts=dict(Counter(o['status'] for o in result['orders'])),
            rebalance_status_counts=dict(Counter(r['status'] for r in result['rebalances'])))
        for row in result['daily']:
            if row['session_date'] in nav:
                nav[row['session_date']][name] = (row['equity'] / accounting.initial_cash
                                                  if row['equity'] is not None else None)
    strategy = comparison['portfolios']['MOM20_20_EW80']['metrics']['full_period']
    comparison['net_return_difference_vs_benchmarks'] = {}
    comparison['exposure_comparability'] = {}
    for name in ('SPY_80_CASH_20', 'ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80'):
        baseline = comparison['portfolios'][name]['metrics']['full_period']
        comparison['net_return_difference_vs_benchmarks'][name] = (
            strategy['cumulative_net_return'] - baseline['cumulative_net_return']
            if strategy['status'] == baseline['status'] == 'valid_exploratory' else None)
        comparison['exposure_comparability'][name] = dict(
            interpretation='Return difference only, not risk-adjusted alpha; inspect actual exposure and market risk.',
            mean_equity_exposure_gap=(strategy['mean_equity_exposure']-baseline['mean_equity_exposure']
                if strategy['status'] == baseline['status'] == 'valid_exploratory' else None))
    with (output / 'net_values.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['session_date'] + list(targets))
        writer.writeheader()
        writer.writerows(nav.values())
    comparison['implementation_sha256'] = {name: digest(ROOT / name) for name in (
        'quant_data/public_research.py', 'quant_data/action_basis.py',
        'quant_portfolio/public_momentum.py', 'quant_backtest/portfolio.py',
        'quant_backtest/research_metrics.py', 'examples/run_public_momentum.py')}
    save_json(output / 'comparison.json', comparison)
    (output / 'report.md').write_text(markdown_report(comparison), encoding='utf-8')
    print(json.dumps({k: v['metrics']['full_period'] for k, v in comparison['portfolios'].items()}, indent=2, default=str))
    print('Saved:', output / 'report.md')
    return comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'outputs/public_universe_20261104')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/public_momentum_20261107')
    args = parser.parse_args()
    run(args.input, args.output, ROOT / 'configs/momentum_public_backtest_v1.json',
        ROOT / 'configs/corporate_actions_review_20261104.json')


if __name__ == '__main__':
    main()
