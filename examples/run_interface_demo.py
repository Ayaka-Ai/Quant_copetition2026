"""Replay the existing risk strategy, then export two inspectable decision traces.

Run from the project root with the NumPy runtime. No market-data download or
broker connection is performed. --inspect-only reads an already completed run.
"""
import argparse
import csv
import datetime as dt
import json
from collections import Counter
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from examples.run_risk_weights import ROOT, run
from quant_data.compact_risk import CompactRiskStore
from quant_data.public_research import load_public_research
from quant_data.public_universe import digest, save_json
from quant_portfolio.public_momentum import build_public_targets
from quant_signals.multifactor import TraditionalFactorScorer, load_factor_config


def read_json(path):
    return json.loads(path.read_text())


def read_csv(path):
    with path.open(newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def export_trace(output):
    account = output / 'OPTIMIZED_TOPK_RISK_COVERAGE'
    comparison = read_json(output / 'comparison.json')
    cfg = read_json(output / 'config_snapshot.json')
    if cfg != read_json(ROOT / 'configs/weight_optimization_public_v1.json'):
        raise ValueError('Demo configuration does not match the frozen strategy')
    for name, expected in comparison['implementation_sha256'].items():
        if digest(ROOT / name) != expected:
            raise ValueError('Replay implementation changed: ' + name)
    risk_audit = read_json(account / 'risk_audit.json')
    ready = [r for r in risk_audit if r['status'] == 'target_ready']
    # Deterministic choice: first two ready signals, not selected by profit.
    signal_days = [dt.date.fromisoformat(r['signal_session']) for r in ready[:2]]
    if len(signal_days) != 2:
        raise ValueError('Two completed decision dates are required')
    data = load_public_research(
        ROOT / 'outputs/public_universe_20261104',
        ROOT / 'configs/momentum_public_backtest_v1.json',
        ROOT / 'configs/corporate_actions_review_20261104.json')
    if data['fingerprints'] != comparison['input_fingerprints']:
        raise ValueError('Trace inputs differ from the completed replay')
    calendar = data['calendar']
    factor_path = ROOT / 'configs/multifactor_public_v1.json'
    if digest(factor_path) != comparison['factor_config_sha256']:
        raise ValueError('Factor configuration changed')
    scorer = TraditionalFactorScorer(calendar, data['observations'], load_factor_config(factor_path))
    # Keep the full history/calendar; calculate only the two displayed decisions.
    focus = SimpleNamespace(sessions=calendar.sessions,
        weekly_sessions=lambda: tuple(s for s in calendar.weekly_sessions() if s.session_date in signal_days))
    decisions = build_public_targets(focus, data['observations'], data['universe'],
        as_of=calendar.sessions[-1].close_available_time, minimum_signal_sessions=61,
        score_provider=scorer.provider(), strategy_id='FACTORS')['decisions']
    store = CompactRiskStore(ROOT / 'outputs/risk_models_20261109', cfg['risk_artifact_sha256'])
    selection = {r['signal_session']: r for r in read_json(account / 'selection_audit.json')}
    risk_rows = {r['signal_session']: r for r in ready}
    orders, trades, daily, positions, actions = (
        read_csv(account / (name + '.csv'))
        for name in ('orders', 'trades', 'daily', 'positions', 'corporate_actions'))
    daily_by_day = {r['session_date']: r for r in daily}
    observations = {(o.symbol, o.session_date): o for o in data['observations']}
    indices = {s.session_date: i for i, s in enumerate(calendar.sessions)}
    traces = []
    for decision in decisions:
        day = decision['signal_session']; key = day.isoformat()
        selected, risk = selection[key], risk_rows[key]
        candidates = {c['symbol']: c for c in decision['candidates']}
        chosen = selected['selected_symbols']
        assert set(chosen) == set(risk['weights'])
        assert all(candidates[s]['eligible'] for s in chosen)
        eligible_scores = [c['score'] for c in candidates.values() if c['eligible']]
        mean = sum(eligible_scores, D(0)) / len(eligible_scores)
        std = (sum(((v-mean)**2 for v in eligible_scores), D(0)) / len(eligible_scores)).sqrt()
        assert abs(std-D(selected['score_population_std'])) < D('1e-25')
        for replacement in selected['replacements']:
            advantage = candidates[replacement['buy']]['score'] - candidates[replacement['sell']]['score']
            assert abs(advantage-D(replacement['score_advantage'])) < D('1e-25')
            assert advantage >= D(selected['score_threshold'])
        if not selected['actual_holdings']:
            assert chosen == list(decision['selected_symbols'])
        sector_weights = {}
        for symbol, weight in risk['weights'].items():
            sector = candidates[symbol]['sector']
            sector_weights[sector] = sector_weights.get(sector, D(0)) + D(weight)
        assert max(sector_weights.values()) <= D(cfg['sector_cap'])
        assert sum(map(D, risk['weights'].values()), D(0)) == D('.80')
        session = calendar.sessions[indices[day]]
        execution_day = calendar.sessions[indices[day]+1].session_date.isoformat()
        day_orders = [r for r in orders if r['signal_session'] == key]
        day_trades = [r for r in trades if r['signal_session'] == key]
        assert len(day_orders) == len(day_trades)
        assert all(r['fill_session'] == execution_day for r in day_trades)
        prior_cash = D(daily_by_day[key]['cash'])
        # Includes any supplied actions between signal and next execution close.
        action_cash = sum((D(r['cash_delta']) for r in actions
            if key < r['effective_session'] <= execution_day), D(0))
        expected_cash = prior_cash + action_cash + sum((D(r['cash_delta']) for r in day_trades), D(0))
        assert expected_cash == D(daily_by_day[execution_day]['cash'])
        matrix = store.snapshot(day, session.close_available_time, cfg['risk_mode'])
        assert matrix is not None
        strict_visible = store.snapshot(day, session.close_available_time, 'strict') is not None
        assert not strict_visible
        order_symbols = {r['symbol'] for r in day_orders}
        relevant_symbols = set(chosen) | set(selected['actual_holdings']) | order_symbols
        displayed_candidates = []
        for symbol in sorted(relevant_symbols):
            candidate = dict(candidates[symbol])
            candidate['unbuffered_selected'] = candidate.pop('selected')
            candidate['unbuffered_target_weight'] = candidate.pop('target_weight')
            candidate['unbuffered_selection_reasons'] = candidate.pop('reasons')
            candidate['final_selected'] = symbol in chosen
            candidate['final_target_weight'] = risk['weights'].get(symbol, '0')
            displayed_candidates.append(candidate)
        traces.append(dict(signal_session=day, signal_time=session.close_available_time,
            execution_session=execution_day, eligible_count=decision['eligible_count'],
            candidate_count=len(candidates), selection=selected, risk=risk,
            sector_target_weights=sector_weights,
            candidate_details=displayed_candidates,
            input_observations=[asdict(observations[(s, day)]) for s in sorted(relevant_symbols)],
            covariance_metadata=matrix['metadata'], beta_metadata=matrix['beta_metadata'],
            covariance_shape=[len(matrix['symbols']), len(matrix['symbols'])],
            strict_visible_at_historical_cutoff=strict_visible,
            signal_account=daily_by_day[key], orders=day_orders, trades=day_trades,
            execution_day_account=daily_by_day[execution_day],
            execution_day_positions=[r for r in positions if r['session_date'] == execution_day and int(r['shares'])],
            execution_day_actions=[r for r in actions if r['effective_session'] == execution_day],
            cash_reconciliation=dict(prior_cash=prior_cash, action_cash=action_cash,
                trade_cash_delta=sum((D(r['cash_delta']) for r in day_trades), D(0)),
                calculated_end_cash=expected_cash, passed=True)))
    save_json(output / 'walkthrough.json', dict(
        schema_version='interface_demo_walkthrough_v1', generated_at_utc=dt.datetime.now(dt.timezone.utc),
        data_coverage_start=calendar.coverage_start, data_coverage_end=calendar.coverage_end,
        universe_count=len(data['universe']), observation_count=len(data['observations']),
        session_count=len(calendar.sessions), input_fingerprints=data['fingerprints'],
        scope='Local historical research replay; no external orders',
        selection_rule='first_two_target_ready_signals_not_profit_selected', traces=traces))
    # A tiny normalized input example: 61 observations for the first selected name.
    sample_symbol = traces[0]['selection']['selected_symbols'][0]
    first_day = signal_days[0]; last_index = indices[first_day]
    sample = [asdict(observations[(sample_symbol, s.session_date)])
        for s in calendar.sessions[last_index-60:last_index+1]]
    with (output / 'input_observations_sample.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(sample[0])); writer.writeheader(); writer.writerows(sample)
    baseline = ROOT / 'outputs/risk_weights_20261109'
    compared = ['net_values.csv'] + [name+'/'+filename for name in
        ('EQUAL_TOPK_RISK_COVERAGE', 'OPTIMIZED_TOPK_RISK_COVERAGE') for filename in
        ('orders.csv', 'trades.csv', 'daily.csv', 'positions.csv', 'corporate_actions.csv',
         'rebalances.csv', 'risk_audit.json', 'selection_audit.json')]
    checks = {name: digest(output/name) == digest(baseline/name) for name in compared}
    if not all(checks.values()):
        raise AssertionError('Replay differs from previous frozen run')
    proof = dict(trace_checks_passed=True, compared_with='outputs/risk_weights_20261109',
        identical_artifacts=checks, artifact_sha256={name: digest(output/name) for name in compared},
        risk_status_counts=dict(Counter(r['status'] for r in risk_audit)),
        historical_strict_visibility_checked=True, broker_routing_enabled=cfg['broker_routing_enabled'],
        walkthrough_sha256=digest(output/'walkthrough.json'),
        demo_implementation_sha256=digest(Path(__file__)))
    save_json(output / 'demo_verification.json', proof)
    print(json.dumps(dict(trace_checks_passed=True, identical_artifacts=len(checks),
        signal_days=signal_days, sample_symbol=sample_symbol, output=str(output)), default=str, indent=2))
    return traces


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/interface_demo_20261112')
    parser.add_argument('--inspect-only', action='store_true', help='Inspect an already completed replay')
    args = parser.parse_args()
    if not args.inspect_only:
        run(args.output)
    export_trace(args.output)


if __name__ == '__main__':
    main()
