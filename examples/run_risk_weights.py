"""Matched actual-holdings replay of equal and risk-optimized 80% targets."""
import argparse
import csv
import datetime as dt
import json
from collections import Counter
from pathlib import Path
from quant_data.compact_risk import CompactRiskStore
from quant_data.public_research import load_public_research
from quant_data.public_universe import digest,save_json
from quant_portfolio.dropout import TopKDropStrategy
from quant_portfolio.public_momentum import build_public_targets
from quant_portfolio.risk_weight_strategy import RiskWeightStrategy
from quant_signals.multifactor import load_factor_config,TraditionalFactorScorer
from quant_backtest import PortfolioConfig,simulate_portfolio
from quant_backtest.research_metrics import performance_metrics
from examples.run_public_momentum import verify_account,ASSUMPTIONS
from examples.run_portfolio import write_report

ROOT=Path(__file__).resolve().parents[1]


def run(output):
    cfg_path=ROOT/'configs/weight_optimization_public_v1.json';cfg=json.loads(cfg_path.read_text())
    if (cfg.get('schema_version')!='risk_weight_optimization_v1' or cfg.get('equity_weight')!='0.80'
            or cfg.get('maximum_positions')!=20 or cfg.get('security_cap')!='0.04'
            or cfg.get('sector_cap')!='0.20' or cfg.get('risk_mode')!='exploratory_model_time'
            or cfg.get('broker_routing_enabled') is not False or cfg.get('full_index_enhancement') is not False):
        raise ValueError('This replay supports only the frozen 80%/20-name/4% research profile')
    if digest(ROOT/'configs'/cfg['risk_config_file'])!=cfg['risk_config_sha256']:raise ValueError('Risk configuration changed')
    risk_cfg=json.loads((ROOT/'configs'/cfg['risk_config_file']).read_text())
    preflight_path=ROOT/'configs'/risk_cfg['parent_config_file']
    if digest(preflight_path)!=risk_cfg['parent_config_sha256']:raise ValueError('Parent preflight policy changed')
    preflight_cfg=json.loads(preflight_path.read_text())
    drop_path=ROOT/'configs'/preflight_cfg['parent_config_file']
    if digest(drop_path)!=preflight_cfg['parent_config_sha256']:raise ValueError('Parent dropout policy changed')
    drop_cfg=json.loads(drop_path.read_text());factor_path=ROOT/'configs'/drop_cfg['factor_config_file']
    if digest(factor_path)!=drop_cfg['factor_config_sha256']:raise ValueError('Frozen factor scores configuration changed')
    store=CompactRiskStore(ROOT/'outputs/risk_models_20261109',cfg['risk_artifact_sha256'])
    output.mkdir(parents=True,exist_ok=True)
    protocol=dict(config_sha256=digest(cfg_path),registered_at_utc=dt.datetime.now(dt.timezone.utc),
                  interpretation='Exploratory risk-model clock, not historical PIT evidence',parameter_search_performed=False)
    pp=output/'run_protocol.json'
    if pp.exists():
        if json.loads(pp.read_text())['config_sha256']!=protocol['config_sha256']:raise ValueError('Use a new output directory')
    else:save_json(pp,protocol)
    data=load_public_research(ROOT/'outputs/public_universe_20261104',ROOT/'configs/momentum_public_backtest_v1.json',
                              ROOT/'configs/corporate_actions_review_20261104.json')
    if data['fingerprints']!=store.summary['input_fingerprints']:raise ValueError('Risk matrices and replay inputs differ')
    fcfg=load_factor_config(factor_path);cal=data['calendar']
    scorer=TraditionalFactorScorer(cal,data['observations'],fcfg)
    built=build_public_targets(cal,data['observations'],data['universe'],as_of=cal.sessions[-1].close_available_time,
        minimum_signal_sessions=61,score_provider=scorer.provider(),strategy_id='FACTORS')
    sectors={m.symbol:m.sector for m in data['universe']}
    first_risk=min(dt.date.fromisoformat(d) for d in store.rows)
    i=next(i for i,s in enumerate(cal.sessions) if s.session_date==first_risk)
    start=cal.sessions[i+1].session_date;end=cal.coverage_end+dt.timedelta(days=1)
    summary=dict(rule_version=cfg['rule_version'],evaluation_start=start,evaluation_end_exclusive=end,
        risk_mode=cfg['risk_mode'],historical_point_in_time=False,full_index_enhancement=False,
        structural_result='80pct_with_20_names_at_4pct_cap_has_unique_weights',portfolios={},
        config_sha256=digest(cfg_path),factor_config_sha256=digest(factor_path),
        risk_artifacts=cfg['risk_artifact_sha256'],input_fingerprints=data['fingerprints'])
    nav={s.session_date:dict(session_date=s.session_date) for s in cal.sessions if s.session_date>=start}
    for name,optimize in [('EQUAL_TOPK_RISK_COVERAGE',False),('OPTIMIZED_TOPK_RISK_COVERAGE',True)]:
        selector=TopKDropStrategy(built['decisions'])
        strategy=RiskWeightStrategy(selector,store,sectors,cfg,optimize=optimize)
        print('Simulating',name,flush=True)
        result=simulate_portfolio(cal.sessions,data['bars'],config=PortfolioConfig(),strategy=strategy,corporate_actions=data['actions'])
        verification=verify_account(result,cal.sessions,PortfolioConfig().initial_cash)
        metrics=performance_metrics(result,start=start,end_exclusive=end,initial_cash=PortfolioConfig().initial_cash)
        write_report(result,PortfolioConfig(),dict(risk_mode=cfg['risk_mode'],assumptions=ASSUMPTIONS,
            risk_fingerprints=cfg['risk_artifact_sha256']),output/name,
            purpose='80% long-only risk allocation interface experiment; not full index enhancement or a live strategy.')
        save_json(output/name/'risk_audit.json',strategy.audit)
        save_json(output/name/'selection_audit.json',selector.audit)
        ready=[r for r in strategy.audit if r['status']=='target_ready']
        for row in ready:
            for key in ('before','after','actual'):
                d=row[key]
                if d['status']=='diagnostic_only' and abs(sum(d['annual_variance_contributions'].values())-d['annual_variance'])>1e-10:
                    raise AssertionError('Risk contributions do not sum to variance')
        summary['portfolios'][name]=dict(metrics=metrics,verification=verification,
            status_counts=dict(Counter(r['status'] for r in strategy.audit)),
            solver_status_counts=dict(Counter(r['solver']['status'] for r in ready if 'solver' in r)),
            mean_target_predicted_volatility=sum(r['after']['annual_volatility'] for r in ready)/len(ready) if ready else None)
        for row in result['daily']:
            if row['session_date'] in nav:nav[row['session_date']][name]=row['net_value']
    for row in nav.values():
        if row['EQUAL_TOPK_RISK_COVERAGE']!=row['OPTIMIZED_TOPK_RISK_COVERAGE']:
            raise AssertionError('A unique feasible allocation unexpectedly changed the account')
    summary['matched_daily_nav_identical']=True
    summary['strict_visible_covariance_dates']=sum(store.snapshot(dt.date.fromisoformat(day),
        dt.datetime.fromisoformat(row['model_available_at']),'strict') is not None for day,row in store.rows.items())
    if summary['strict_visible_covariance_dates']:raise AssertionError('Actual availability was bypassed')
    summary['implementation_sha256']={p:digest(ROOT/p) for p in (
        'quant_data/compact_risk.py','quant_portfolio/weight_optimizer.py','quant_portfolio/risk_weight_strategy.py',
        'examples/run_risk_weights.py','quant_backtest/portfolio.py')}
    save_json(output/'comparison.json',summary);save_json(output/'config_snapshot.json',cfg)
    with (output/'net_values.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['session_date','EQUAL_TOPK_RISK_COVERAGE','OPTIMIZED_TOPK_RISK_COVERAGE'])
        writer.writeheader();writer.writerows(nav.values())
    print(json.dumps(summary,default=str,indent=2));return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/risk_weights_20261109')
    args=parser.parse_args();run(args.output)
