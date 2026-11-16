"""Offline risk coverage, necessary-bound proofs and entry-path acceptance."""
import argparse
import csv
import datetime as dt
import json
from collections import Counter
from decimal import Decimal as D
from pathlib import Path

from quant_data.risk_inputs import load_risk_inputs,risk_snapshot,KINDS
from quant_data.public_universe import digest,save_json
from quant_data.us_calendar import exchange_sessions
from quant_portfolio.risk_preflight import necessary_conditions,validate_covariance

ROOT=Path(__file__).resolve().parents[1]


def check_snapshot(snapshot,day,required_symbols=()):
    errors=[]; values={}
    for kind,row in snapshot.items():
        if row is None: errors.append('missing:'+kind); continue
        values[kind]=row['payload']
        if kind in ('betas','covariance'):
            start=dt.date.fromisoformat(row['estimation_start']); end=dt.date.fromisoformat(row['estimation_end'])
            needed=61 if kind=='betas' else 253
            if len(exchange_sessions(start,end+dt.timedelta(days=1)))<needed:
                errors.append('short_estimation_window:'+kind)
    if not errors:
        symbols=set(values['benchmark'])|set(required_symbols)
        if any(not isinstance(v,str) or not v for v in values['sectors'].values()): errors.append('invalid_sector')
        if not symbols.issubset(values['sectors']): errors.append('incomplete_sectors')
        beta={s:D(str(v)) for s,v in values['betas'].items()}
        if not symbols.issubset(beta) or any(not v.is_finite() for v in beta.values()): errors.append('incomplete_or_invalid_betas')
        cov=values['covariance']; order=cov['symbols']
        if len(order)!=len(set(order)) or not symbols.issubset(order) or len(order)!=len(cov['matrix']):
            errors.append('incomplete_covariance_universe')
        if not errors:
            try: validate_covariance(cov['matrix'])
            except ValueError as exc: errors.append('covariance:'+str(exc))
    return errors,values


def run(input_root,output):
    cfg_path=ROOT/'configs/risk_preflight_v1.json'; cfg=json.loads(cfg_path.read_text())
    if digest(ROOT/'configs'/cfg['parent_config_file'])!=cfg['parent_config_sha256']:
        raise ValueError('Parent rules changed')
    output.mkdir(parents=True,exist_ok=True)
    inputs=load_risk_inputs(input_root)
    decision_path=ROOT/'outputs/public_multifactor_20261107/decisions.json'
    decisions=[d for d in json.loads(decision_path.read_text()) if d['status']=='target_ready']
    cache_calendar=exchange_sessions(dt.date(2024,11,4),dt.date(2026,11,4))
    calendar_index={s.session_date:i for i,s in enumerate(cache_calendar)}
    cov_window_dates=[d['signal_session'] for d in decisions
                      if calendar_index[dt.date.fromisoformat(d['signal_session'])]>=252]
    coverage=[]; preflight=[]
    for row in decisions:
        day=dt.date.fromisoformat(row['signal_session']); cutoff=dt.datetime.fromisoformat(row['signal_time'])
        snap=risk_snapshot(inputs,day,cutoff)
        required={c['symbol'] for c in row['candidates'] if c['eligible']}
        errors,values=check_snapshot(snap,day,required)
        ready_kinds={k:snap[k] is not None for k in KINDS}
        coverage.append(dict(signal_session=day,signal_time=cutoff,**ready_kinds,
            validation_errors=errors,source_versions={k:None if r is None else dict(as_of=r['as_of'],available_at=r['available_at'],source=r['source']) for k,r in snap.items()}))
        for name,exposure in cfg['exposure_profiles'].items():
            try:
                proof=necessary_conditions(exposure,cfg['limits'],benchmark=values.get('benchmark'),
                                           sectors=values.get('sectors'),betas=values.get('betas'))
            except (ValueError,KeyError) as exc:
                proof=dict(status='invalid_input',conflicts=[],missing_inputs=[],feasible_solution_certified=False,error=str(exc))
            preflight.append(dict(signal_session=day,profile=name,**proof,
                data_validation_errors=errors,
                ready_for_optimizer=not errors and proof['status']=='requires_optimizer'))
    # Inventory existing local evidence without relabeling exploration outputs as risk inputs.
    snapshot_path=ROOT/'configs/universe/sp500_2026-11-03.json'
    frozen=json.loads(snapshot_path.read_text())
    inventory=dict(search_scope=['configs','outputs','parent work and relatedDoc filename inventory'],
        declared_risk_input_directory=str(input_root),manifest_status=inputs['status'],accepted_files=inputs['files'],
        frozen_membership_and_sector_snapshot=dict(path=str(snapshot_path),sha256=digest(snapshot_path),
            securities=frozen['security_count'],snapshot_date=frozen['snapshot_date'],
            historical_point_in_time=False,aware_publication_time_available=False),
        daily_cache=dict(path=str(ROOT/'outputs/public_universe_20261104/raw/manifest.json'),
            sha256=digest(ROOT/'outputs/public_universe_20261104/raw/manifest.json'),
            interpretation='Prices can support future research estimators; they are not already validated beta/covariance inputs.'),
        readiness_diagnostics_are_inputs=False,
        remaining=['daily historical benchmark weights','historical effective sectors and membership',
            'versioned 60-return SPY beta estimates','versioned 252-return covariance and shrinkage/PSD validation'])
    # Reconstruct the observed low-exposure path, including real limited trades.
    result_path=ROOT/'outputs/public_dropout_20261109/comparison.json'
    previous=json.loads(result_path.read_text()); start=previous['evaluation_start']
    data_dir=ROOT/'outputs/public_dropout_20261109/FACTORS_DROP_EXECUTION_CAPS'
    daily=[r for r in csv.DictReader((data_dir/'daily.csv').open()) if r['session_date']>=start]
    low=[r for r in daily if r['valuation_valid']=='True' and D(r['equity_exposure'])<D('.5')]
    path_rows=daily[:20]
    with (output/'entry_path.csv').open('w',newline='') as f:
        headers=['session_date','cash','equity','equity_exposure','valuation_valid']
        writer=csv.DictWriter(f,fieldnames=headers);writer.writeheader();writer.writerows({k:r[k] for k in headers} for r in path_rows)
    summary=dict(rule_version=cfg['rule_version'],signal_dates=len(decisions),
        available_versions_by_kind={k:sum(r[k] for r in coverage) for k in KINDS},
        validated_complete_dates=sum(not r['validation_errors'] for r in coverage),
        optimizer_ready_profile_dates=sum(r['ready_for_optimizer'] for r in preflight),
        calendar_only_price_window_capacity=dict(
            interpretation='Calendar length only; does not certify symbol coverage, action basis or point-in-time data.',
            beta61_price_signal_dates=sum(calendar_index[dt.date.fromisoformat(d['signal_session'])]>=60 for d in decisions),
            covariance253_price_signal_dates=len(cov_window_dates),
            first_covariance_length_eligible_signal=cov_window_dates[0] if cov_window_dates else None),
        status_counts=dict(Counter(r['status'] for r in preflight)),
        exposure_capacity_proofs={name:necessary_conditions(e,cfg['limits']) for name,e in cfg['exposure_profiles'].items()},
        entry_path=dict(evaluation_sessions=len(daily),sessions_below_50pct=len(low),dates=[r['session_date'] for r in low],
            interpretation='Internal research exposure statistic, not official qualification adjudication.',
            first_entry_exempt=False,minimum_budget_only_execution_events_to_80pct=4,
            caveat='Four is an idealized fixed-NAV budget-only lower bound, not a guarantee; sells, fees, rounding and prices can lengthen entry.'),
        policy=cfg['conflict_policy'],optimizer_enabled=False,broker_routing_enabled=False,
        fingerprints=dict(config=digest(cfg_path),source_decisions=digest(decision_path),prior_comparison=digest(result_path),
                          entry_daily=digest(data_dir/'daily.csv')))
    save_json(output/'config_snapshot.json',cfg);save_json(output/'input_inventory.json',inventory)
    save_json(output/'coverage.json',coverage);save_json(output/'preflight.json',preflight)
    summary['implementation_sha256']={name:digest(ROOT/name) for name in (
        'quant_portfolio/risk_preflight.py','quant_data/risk_inputs.py','examples/run_risk_preflight.py')}
    save_json(output/'summary.json',summary)
    with (output/'coverage.csv').open('w',newline='') as f:
        headers=['signal_session','signal_time']+list(KINDS)+['validation_errors']
        writer=csv.DictWriter(f,fieldnames=headers);writer.writeheader()
        writer.writerows({k:str(r[k]) for k in headers} for r in coverage)
    print(json.dumps(summary,default=str,indent=2))
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--risk-input',type=Path,default=ROOT/'risk_inputs')
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/risk_preflight_20261109')
    args=parser.parse_args();run(args.risk_input,args.output)
