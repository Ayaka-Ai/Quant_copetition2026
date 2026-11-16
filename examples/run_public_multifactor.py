"""Traditional factors versus matched-pool momentum, entirely from local cache."""
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
from quant_signals.multifactor import TraditionalFactorScorer, load_factor_config
from examples.run_portfolio import write_report
from examples.run_public_momentum import ASSUMPTIONS, verify_account

ROOT = Path(__file__).resolve().parents[1]
MATCHED = 'MOM20_MATCHED_20_EW80'


def report(comparison):
    lines = ['# 传统多因子与共同股票池动量对照', '',
        '规则版本 %s；仅传统因子研究版本。' % comparison['rule_version'], '',
        '共同评估期：%s至%s（右端不含）。' %
        (comparison['evaluation_start'], comparison['evaluation_end_exclusive']), '',
        '原始因子在完整、合格候选的GICS行业大类内去极值、标准化；价量与防御合成分数跨池取并列平均分位。', '',
        '最终分数 = (0.25 × 价量分位 + 0.10 × 防御分位) / 0.35。未使用机器学习、新闻、状态切换或TopK Drop。', '',
        '| 组合 | 累计净收益 | 年化夏普 | 最大回撤 | 日均实际股票仓位 | 双边累计换手 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for name, item in comparison['portfolios'].items():
        m = item['metrics']['full_period']
        if m['status'] != 'valid_exploratory':
            lines.append('| %s | 估值无效 | — | — | — | — |' % name)
            continue
        lines.append('| %s | %.2f%% | %s | %.2f%% | %.2f%% | %.2f倍 |' % (
            name, 100*m['cumulative_net_return'],
            '不可计算' if m['annualized_sharpe'] is None else '%.3f' % m['annualized_sharpe'],
            100*m['maximum_drawdown'], 100*m['mean_equity_exposure'], m['cumulative_two_way_turnover']))
    lines += ['', '动量对照也要求完整61日因子历史，使用相同合格股票池、信号日、资金、约束及成交规则；不能直接与第一步较早起点的累计收益相减。', '',
              '股票池等权基准仍受整数股及最低佣金影响，实际仓位可能远低于80%，不代表相同风险敞口。', '',
              '## 各时间段的多因子减动量净收益差', '']
    for key, value in comparison['net_return_difference_vs_matched_momentum'].items():
        lines.append('- %s：%s。' % (key, '估值无效或无样本' if value is None else '%.2f个百分点' % (100*value)))
    lines += ['', '本次只运行预先固定的一组公式与权重，未按收益挑选参数；上述差异不是风险调整后的alpha。', '',
              '固定多因子在全期的收益和夏普低于共同池动量，最大回撤较小；后段改善不足以证明整体优于动量。保留完整结果，未据此修改权重。', '',
              '## 验证及局限', '',
              '- 所有组合通过逐日现金、股数、净值及次日成交检查。',
              '- 第一版动量入口保持兼容，通用评分不覆盖原动量规则。',
              '- 标准化分组采用现有GICS行业大类，尚无更细行业历史；单成员或零方差组标准化为0。',
              '- 调整后的OHLC仅用于因子，按当日AdjClose/Close统一缩放；跳空使用这一总回报调整口径。',
              '- 所有因子要求同一冻结版本的连续61日历史；缺失不填补，不以剩余因子重新分配权重。', '']
    lines += ['- ' + x for x in ASSUMPTIONS]
    lines += ['', '完整分数与资格见 decisions.json，标准化统计见 normalization.json，子区间与账目见各组合目录。', '']
    return '\n'.join(lines)


def run(input_path, output, config_path):
    cfg = load_factor_config(config_path)
    name = cfg['base_config_file']
    if Path(name).name != name:
        raise ValueError('Base configuration must be a local filename')
    base_path = config_path.parent / name
    if digest(base_path) != cfg['base_config_sha256']:
        raise ValueError('Base configuration fingerprint changed')
    output.mkdir(parents=True, exist_ok=True)
    protocol = dict(registered_at_utc=dt.datetime.now(dt.timezone.utc), config_sha256=digest(config_path),
                    research_protocol=cfg['research_protocol'])
    protocol_path = output / 'run_protocol.json'
    if protocol_path.exists():
        previous = json.loads(protocol_path.read_text())
        if previous['config_sha256'] != protocol['config_sha256']:
            raise ValueError('Output has another registered factor configuration; use another directory')
    else:
        save_json(protocol_path, protocol)
    data = load_public_research(input_path, base_path, ROOT / 'configs/corporate_actions_review_20261104.json')
    calendar = data['calendar']
    scorer = TraditionalFactorScorer(calendar, data['observations'], cfg)
    kwargs = dict(as_of=calendar.sessions[-1].close_available_time, minimum_signal_sessions=61)
    multi = build_public_targets(calendar, data['observations'], data['universe'],
        score_provider=scorer.provider(), strategy_id=cfg['strategy_id'], **kwargs)
    momentum = build_public_targets(calendar, data['observations'], data['universe'],
        score_provider=scorer.provider('momentum20'), strategy_id=MATCHED, **kwargs)
    for benchmark in ('SPY_80_CASH_20', 'ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80'):
        if multi['targets'][benchmark] != momentum['targets'][benchmark]:
            raise AssertionError('Factor and momentum comparison pools/schedules differ')
    targets = {cfg['strategy_id']: multi['targets'][cfg['strategy_id']],
               MATCHED: momentum['targets'][MATCHED],
               'SPY_80_CASH_20': multi['targets']['SPY_80_CASH_20'],
               'ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80': multi['targets']['ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80']}
    index = {s.session_date: i for i, s in enumerate(calendar.sessions)}
    valid = [t for t in targets[cfg['strategy_id']] if index[t.signal_session]+1 < len(calendar.sessions)]
    if not valid:
        raise ValueError('No executable common factor targets')
    start = calendar.sessions[index[valid[0].signal_session]+1].session_date
    end = calendar.coverage_end + dt.timedelta(days=1)
    base = data['config']
    accounting = PortfolioConfig(initial_cash=D(base['account']['research_initial_cash']),
        fee_per_share=D(base['costs']['commission_usd_per_share']),
        minimum_fee=D(base['costs']['minimum_commission_usd_per_filled_order']),
        slippage_bps=D(base['costs']['slippage_bps_per_side']))
    comparison = dict(rule_version=cfg['rule_version'], evaluation_start=start, evaluation_end_exclusive=end,
        assumptions=ASSUMPTIONS, fingerprints=dict(data['fingerprints'], factor_config=digest(config_path)),
        parameter_search_performed=False, independent_out_of_sample=False, broker_routing_enabled=False,
        decision_status_counts=dict(Counter(d['status'] for d in multi['decisions'])), portfolios={})
    save_json(output / 'config_snapshot.json', cfg)
    save_json(output / 'decisions.json', multi['decisions'])
    # Matched decisions need only their selection/ranks; all factor evidence is shared.
    save_json(output / 'matched_momentum_decisions.json', [dict(
        signal_session=d['signal_session'], status=d['status'], selected_symbols=d['selected_symbols'],
        eligible_symbols=[c['symbol'] for c in d['candidates'] if c['eligible']]) for d in momentum['decisions']])
    save_json(output / 'normalization.json', [dict(signal_session=k[0], signal_time=k[1],
              sectors=v['normalization']) for k, v in scorer.cache.items()])
    save_json(output / 'input_inventory.json', data['inventory'])
    save_json(output / 'event_basis_audit.json', data['event_audit'])
    nav = {s.session_date: dict(session_date=s.session_date) for s in calendar.sessions if s.session_date >= start}
    for name, plans in targets.items():
        print('Simulating', name, flush=True)
        symbols = {'SPY'} if name == 'SPY_80_CASH_20' else {m.symbol for m in data['universe']}
        result = simulate_portfolio(calendar.sessions, [b for b in data['bars'] if b.symbol in symbols],
            plans, accounting, corporate_actions=[a for a in data['actions'] if a.symbol in symbols])
        verification = verify_account(result, calendar.sessions, accounting.initial_cash)
        windows = [dict(id='full_period', start=start.isoformat(), end_exclusive=end.isoformat())] + cfg['research_protocol']['evaluation_windows']
        metrics = {w['id']: performance_metrics(result,
            start=max(start, dt.date.fromisoformat(w['start'])),
            end_exclusive=min(end, dt.date.fromisoformat(w['end_exclusive'])), initial_cash=accounting.initial_cash)
            for w in windows}
        write_report(result, accounting, dict(rule_version=cfg['rule_version'], assumptions=ASSUMPTIONS,
            fingerprints=comparison['fingerprints']), output / name,
            purpose='Traditional-only exploratory factors and common-pool controls, not complete CLEAR-US or live evidence.')
        save_json(output / name / 'metrics.json', metrics)
        comparison['portfolios'][name] = dict(metrics=metrics, verification=verification,
            order_status_counts=dict(Counter(o['status'] for o in result['orders'])))
        for row in result['daily']:
            if row['session_date'] in nav:
                nav[row['session_date']][name] = row['equity']/accounting.initial_cash if row['equity'] is not None else None
    differences = {}
    for window, m in comparison['portfolios'][cfg['strategy_id']]['metrics'].items():
        control = comparison['portfolios'][MATCHED]['metrics'][window]
        differences[window] = (m['cumulative_net_return']-control['cumulative_net_return']
            if m['status'] == control['status'] == 'valid_exploratory' else None)
    comparison['net_return_difference_vs_matched_momentum'] = differences
    comparison['implementation_sha256'] = {p: digest(ROOT / p) for p in (
        'quant_data/public_research.py', 'quant_data/action_basis.py', 'quant_signals/scores.py',
        'quant_signals/multifactor.py', 'quant_portfolio/public_momentum.py', 'quant_backtest/portfolio.py',
        'quant_backtest/research_metrics.py', 'examples/run_public_multifactor.py', 'examples/run_public_momentum.py')}
    with (output / 'net_values.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['session_date']+list(targets))
        writer.writeheader()
        writer.writerows(nav.values())
    save_json(output / 'comparison.json', comparison)
    (output / 'report.md').write_text(report(comparison), encoding='utf-8')
    print(json.dumps({k: v['metrics']['full_period'] for k, v in comparison['portfolios'].items()}, indent=2, default=str))
    print('Saved:', output / 'report.md')
    return comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'outputs/public_universe_20261104')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/public_multifactor_20261107')
    args = parser.parse_args()
    run(args.input, args.output, ROOT / 'configs/multifactor_public_v1.json')


if __name__ == '__main__':
    main()
