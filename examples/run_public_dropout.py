"""Stateful drop and execution-budget comparisons from the frozen factor data."""
import csv
import datetime as dt
import json
from decimal import Decimal as D
from pathlib import Path
from collections import defaultdict
from quant_backtest import PortfolioConfig, simulate_portfolio
from quant_backtest.research_metrics import performance_metrics
from quant_data.public_research import load_public_research
from quant_data.public_universe import digest, save_json
from quant_portfolio.public_momentum import build_public_targets
from quant_portfolio.dropout import TopKDropStrategy
from quant_portfolio.risk_checks import audit_target_risk
from quant_signals.multifactor import load_factor_config, TraditionalFactorScorer
from examples.run_public_momentum import verify_account, ASSUMPTIONS
from examples.run_portfolio import write_report

ROOT = Path(__file__).resolve().parents[1]


def run(output=None):
    output = Path(output) if output else ROOT/'outputs/public_dropout_20261109'
    output.mkdir(parents=True, exist_ok=True)
    path = ROOT/'configs/dropout_public_v1.json'
    cfg = json.loads(path.read_text())
    fp = ROOT/'configs'/cfg['factor_config_file']
    if digest(fp) != cfg['factor_config_sha256']:
        raise ValueError('Frozen factor configuration changed')
    protocol = dict(config_sha256=digest(path), registered_at_utc=dt.datetime.now(dt.timezone.utc),
        parameter_search_performed=False, independent_out_of_sample=False)
    pp = output/'run_protocol.json'
    if pp.exists() and json.loads(pp.read_text())['config_sha256'] != protocol['config_sha256']:
        raise ValueError('Use a new directory for a changed configuration')
    if not pp.exists(): save_json(pp, protocol)
    factors = load_factor_config(fp)
    base = ROOT/'configs'/factors['base_config_file']
    if digest(base) != factors['base_config_sha256']: raise ValueError('Base configuration changed')
    data = load_public_research(ROOT/'outputs/public_universe_20261104', base,
                                ROOT/'configs/corporate_actions_review_20261104.json')
    cal = data['calendar']
    scorer = TraditionalFactorScorer(cal, data['observations'], factors)
    built = build_public_targets(cal, data['observations'], data['universe'],
        as_of=cal.sessions[-1].close_available_time, minimum_signal_sessions=61,
        score_provider=scorer.provider(), strategy_id='FACTORS')
    decisions = built['decisions']
    kwargs = dict(maximum_positions=cfg['maximum_positions'], weight=D(cfg['security_weight']),
        sector_cap=D(cfg['sector_cap']), replacement_fraction=D(cfg['replacement_fraction']),
        score_margin_std=D(cfg['score_margin_population_std']))
    drop = TopKDropStrategy(decisions, **kwargs)
    # Twenty previous/current signal sessions only; never use execution-day volume.
    obs = {(o.symbol,o.session_date):o for o in data['observations']}
    indices = {s.session_date:i for i,s in enumerate(cal.sessions)}
    caps = {}
    for decision in decisions:
        day, cutoff = decision['signal_session'], decision['signal_time']
        i = indices[day]; caps[day] = {}
        if i < 19: continue
        for member in data['universe']:
            history = [obs.get((member.symbol,s.session_date)) for s in cal.sessions[i-19:i+1]]
            if all(o is not None and o.model_available_at <= cutoff and o.reported_close_volume_proxy is not None for o in history):
                caps[day][member.symbol] = sum((o.reported_close_volume_proxy for o in history),D(0))/20*D(cfg['risk_revision']['symbol_fraction_of_mean20_close_volume_proxy'])
    risk = TopKDropStrategy(decisions, **kwargs,
        trade_fraction=D(cfg['risk_revision']['maximum_two_way_trade_fraction_of_signal_NAV']), liquidity_caps=caps)
    accounting = PortfolioConfig()
    first = built['targets']['FACTORS'][0]
    start = cal.sessions[indices[first.signal_session]+1].session_date
    end = cal.coverage_end+dt.timedelta(days=1)
    variants = [('FACTORS_NO_BUFFER',built['targets']['FACTORS'],None),
                ('FACTORS_TOPK_DROP',(),drop),('FACTORS_DROP_EXECUTION_CAPS',(),risk),
                ('SPY_80_CASH_20',built['targets']['SPY_80_CASH_20'],None)]
    comparison = dict(rule_version=cfg['rule_version'], evaluation_start=start, evaluation_end_exclusive=end,
        assumptions=ASSUMPTIONS, fingerprints=dict(data['fingerprints'],factor_config=digest(fp),drop_config=digest(path)),
        benchmark_active_risk_status=cfg['risk_revision']['benchmark_active_risk_status'], portfolios={})
    save_json(output/'config_snapshot.json',cfg)
    nav = {s.session_date:dict(session_date=s.session_date) for s in cal.sessions if s.session_date>=start}
    for name, plans, strategy in variants:
        print('Simulating',name,flush=True)
        symbols = {'SPY'} if name.startswith('SPY') else {m.symbol for m in data['universe']}
        result = simulate_portfolio(cal.sessions,[b for b in data['bars'] if b.symbol in symbols],plans,accounting,
            corporate_actions=[a for a in data['actions'] if a.symbol in symbols],strategy=strategy)
        checks = verify_account(result,cal.sessions,accounting.initial_cash)
        metrics = performance_metrics(result,start=start,end_exclusive=end,initial_cash=accounting.initial_cash)
        windows = {w['id']:performance_metrics(result,start=max(start,dt.date.fromisoformat(w['start'])),
            end_exclusive=min(end,dt.date.fromisoformat(w['end_exclusive'])),initial_cash=accounting.initial_cash)
            for w in factors['research_protocol']['evaluation_windows']}
        budget_checks = 0
        if strategy is not None:
            for row in strategy.audit:
                assert len(row['replacements']) <= row.get('maximum_regular_replacements',0)
        write_report(result,accounting,dict(fingerprints=comparison['fingerprints'],assumptions=ASSUMPTIONS),output/name,
            purpose='Offline stateful dropout comparison with actual filled holdings; not live execution or full index enhancement.')
        if strategy is not None: save_json(output/name/'strategy_audit.json',strategy.audit)
        if strategy is not None:
            sectors={m.symbol:m.sector for m in data['universe']}
            risk_checks=[dict(signal_session=a['signal_session'],**audit_target_risk(
                {s:D(cfg['security_weight']) for s in a['selected_symbols']},sectors))
                for a in strategy.audit if a['status']=='target_ready']
            save_json(output/name/'index_risk_readiness.json',risk_checks)
            assert all(not r['violations'] for r in risk_checks)
        save_json(output/name/'execution_limits.json',result['execution_limits'])
        if strategy is risk:
            audit = {a['signal_session']:a for a in risk.audit if a['status']=='target_ready'}
            totals = defaultdict(lambda:D(0)); singles = defaultdict(lambda:D(0))
            for t in result['trades']:
                totals[t['signal_session']] += t['notional']
                singles[t['signal_session'],t['symbol']] += t['notional']
            for day,value in totals.items():
                assert value <= audit[day]['maximum_trade_notional']
                budget_checks += 1
            for (day,symbol),value in singles.items():
                assert value <= audit[day]['symbol_trade_notionals'][symbol]
                budget_checks += 1
        comparison['portfolios'][name] = dict(metrics=metrics,windows=windows,verification=checks,
            execution_budget_checks=budget_checks,limited_orders=len(result['execution_limits']))
        for row in result['daily']:
            if row['session_date'] in nav:
                nav[row['session_date']][name] = row['net_value']
    with (output/'net_values.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['session_date']+[v[0] for v in variants]);writer.writeheader();writer.writerows(nav.values())
    comparison['implementation_sha256'] = {p:digest(ROOT/p) for p in (
        'quant_backtest/portfolio.py','quant_portfolio/dropout.py','examples/run_public_dropout.py',
        'quant_portfolio/public_momentum.py','quant_signals/multifactor.py','quant_data/public_research.py')}
    save_json(output/'comparison.json',comparison)
    print(json.dumps(comparison['portfolios'],default=str,indent=2))
    return comparison


if __name__=='__main__': run()
