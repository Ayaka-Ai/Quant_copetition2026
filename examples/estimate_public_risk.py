"""Estimate post-hoc PUBLIC beta/covariance artifacts, without certifying history."""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path
from decimal import Decimal as D

from quant_data.public_research import load_public_research
from quant_data.public_universe import digest,save_json
from quant_data.risk_inputs import load_risk_inputs,risk_snapshot
from quant_signals.risk_model import estimate_beta,shrink_covariance,trailing_panel,portfolio_variance

ROOT=Path(__file__).resolve().parents[1]


def run(output):
    import numpy as np
    cfg_path=ROOT/'configs/risk_model_research_v1.json';cfg=json.loads(cfg_path.read_text())
    if digest(ROOT/'configs'/cfg['parent_config_file'])!=cfg['parent_config_sha256']:
        raise ValueError('Parent risk policy changed')
    output.mkdir(parents=True,exist_ok=True);protocol_path=output/'run_protocol.json'
    data=load_public_research(ROOT/'outputs/public_universe_20261104',ROOT/'configs/momentum_public_backtest_v1.json',
                              ROOT/'configs/corporate_actions_review_20261104.json')
    identity=dict(config_sha256=digest(cfg_path),input_fingerprints=data['fingerprints'],numpy_version=np.__version__)
    if protocol_path.exists():
        protocol=json.loads(protocol_path.read_text())
        if protocol['identity']!=identity: raise ValueError('Use a new output directory for different inputs/config/runtime')
    else:
        protocol=dict(identity=identity,first_produced_at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        save_json(protocol_path,protocol)
    produced=dt.datetime.fromisoformat(protocol['first_produced_at_utc'])
    lookup={(o.symbol,o.session_date):o for o in data['observations']}
    symbols=tuple(sorted(m.symbol for m in data['universe']))
    last_retrieved=max(o.actual_retrieved_at for o in data['observations'])
    actual_available=max(produced,last_retrieved)
    decision_path=ROOT/'outputs/public_multifactor_20261107/decisions.json'
    decisions=[d for d in json.loads(decision_path.read_text()) if d['status']=='target_ready']
    index={s.session_date:i for i,s in enumerate(data['calendar'].sessions)}
    coverage=[];beta_records=[];covariance_records=[];exclusions=[];npz_hashes={}
    portfolio_diagnostics=[]
    for decision in decisions:
        day=dt.date.fromisoformat(decision['signal_session']);cutoff=dt.datetime.fromisoformat(decision['signal_time'])
        record=dict(signal_session=day,model_cutoff=cutoff,beta_assets=0,covariance_assets=0,
                    beta_status='unavailable',covariance_status='unavailable',historical_point_in_time=False)
        for kind,n in [('betas',cfg['beta_returns']),('covariance',cfg['covariance_returns'])]:
            panel,accepted,excluded=trailing_panel(data['calendar'],lookup,symbols,day,cutoff,n)
            exclusions.append(dict(signal_session=day,kind=kind,excluded_symbols=excluded))
            if panel is None or not accepted: continue
            start=data['calendar'].sessions[index[day]-n].session_date
            common=dict(as_of=day,available_at=actual_available,model_available_at=cutoff,
                source='posthoc_public_cache:'+data['fingerprints']['manifest'],
                estimation_start=start,estimation_end=day,first_produced_at_utc=produced,
                latest_input_retrieved_at=last_retrieved,historical_point_in_time=False)
            if kind=='betas':
                spy,spy_symbols,_=trailing_panel(data['calendar'],lookup,('SPY',),day,cutoff,n)
                if not spy_symbols: continue
                try: values=estimate_beta(panel,spy[:,0])
                except ValueError as exc: record['beta_status']=str(exc);continue
                beta_records.append(dict(common,units='dimensionless_SPY_beta',
                    payload={s:format(float(v),'.17g') for s,v in zip(accepted,values)}))
                record.update(beta_status='estimated_posthoc',beta_assets=len(accepted))
                continue
            factors,diagonal,diagnostics=shrink_covariance(panel,float(cfg['covariance_shrinkage']))
            filename='covariance_'+day.isoformat()+'.npz'
            np.savez_compressed(output/filename,factors=factors,diagonal=diagonal,symbols=np.array(accepted))
            npz_hashes[filename]=digest(output/filename)
            covariance_records.append(dict(common,units='daily_return_covariance',file=filename,
                sha256=npz_hashes[filename],representation='factors_times_transpose_plus_diagonal',diagnostics=diagnostics))
            record.update(covariance_status='estimated_posthoc',covariance_assets=len(accepted))
            weights={c['symbol']:D(c['target_weight']) for c in decision['candidates'] if c['selected']}
            missing=sorted(set(weights)-set(accepted))
            p=dict(signal_session=day,missing_selected_symbols=missing,status='incomplete_selected_coverage')
            if not missing:
                vector=np.array([float(weights.get(s,D(0))) for s in accepted])
                p.update(status='diagnostic_only',predicted_annual_volatility=(portfolio_variance(factors,diagonal,vector)*252)**.5)
            portfolio_diagnostics.append(p)
        coverage.append(record)
    save_json(output/'betas.json',beta_records);save_json(output/'covariance_index.json',covariance_records)
    save_json(output/'coverage.json',coverage);save_json(output/'exclusions.json',exclusions)
    save_json(output/'portfolio_diagnostics.json',portfolio_diagnostics);save_json(output/'config_snapshot.json',cfg)
    # Honest actual availability: no historical signal may see these post-hoc outputs.
    strict=output/'strict_export';strict.mkdir(exist_ok=True)
    save_json(strict/'betas.json',beta_records)
    save_json(strict/'manifest.json',dict(schema_version='risk_inputs_v1',files=[
        dict(kind='betas',file='betas.json',sha256=digest(strict/'betas.json'))]))
    loaded=load_risk_inputs(strict)
    visible=sum(risk_snapshot(loaded,dt.date.fromisoformat(d['signal_session']),dt.datetime.fromisoformat(d['signal_time']))['betas'] is not None for d in decisions)
    if visible: raise AssertionError('Post-hoc outputs leaked into historical risk availability')
    summary=dict(rule_version=cfg['rule_version'],signal_dates=len(decisions),beta_versions=len(beta_records),
        covariance_versions=len(covariance_records),historically_visible_strict_beta_versions=visible,
        historical_point_in_time=False,full_index_enhancement_ready=False,
        beta_asset_count_range=[min(r['beta_assets'] for r in coverage),max(r['beta_assets'] for r in coverage)],
        covariance_asset_count_range=[min(r['covariance_assets'] for r in coverage if r['covariance_assets']),max(r['covariance_assets'] for r in coverage)],
        actual_available_at=actual_available,first_produced_at_utc=produced,
        model_basis='latest_frozen_adjusted_close_with_snapshot_action_guards',
        missing=['historical_index_weights','historical_sector_versions','true_historical_price_and_estimate_availability'],
        input_fingerprints=data['fingerprints'],config_sha256=digest(cfg_path),decision_sha256=digest(decision_path),
        numpy_version=np.__version__,covariance_sha256=npz_hashes,
        implementation_sha256={p:digest(ROOT/p) for p in ('quant_signals/risk_model.py','examples/estimate_public_risk.py')})
    save_json(output/'summary.json',summary)
    with (output/'coverage.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(coverage[0]));writer.writeheader();writer.writerows(coverage)
    print(json.dumps({k:v for k,v in summary.items() if k!='covariance_sha256'},default=str,indent=2))
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/risk_models_20261109')
    args=parser.parse_args();run(args.output)
