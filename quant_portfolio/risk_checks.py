"""Target risk diagnostics; missing benchmark inputs never imply compliance."""
from decimal import Decimal as D


def audit_target_risk(weights, sectors, *, benchmark=None, betas=None, daily_covariance=None,
                      security_cap=D('.04'), sector_cap=D('.20'), active_cap=D('.015'),
                      sector_active_cap=D('.03'), beta_lower=D('.85'), beta_upper=D('1.05'),
                      tracking_error_cap=D('.08')):
    violations, missing, infeasible = [], [], []
    if any(not isinstance(v,D) or not v.is_finite() or v<0 for v in weights.values()):
        raise ValueError('Risk weights require nonnegative finite Decimals')
    if sum(weights.values(),D(0))>1: violations.append('total_weight_exceeds_one')
    totals={}
    for s,w in weights.items():
        if w>security_cap: violations.append('security_cap:'+s)
        if s not in sectors: missing.append('sector:'+s)
        else: totals[sectors[s]]=totals.get(sectors[s],D(0))+w
    for sector,w in totals.items():
        if w>sector_cap: violations.append('sector_cap:'+sector)
    result=dict(target_equity_weight=sum(weights.values(),D(0)),sector_weights=totals,
                estimated_beta=None,predicted_annual_tracking_error=None)
    if benchmark is None:
        missing.append('historical_benchmark_weights')
    else:
        if (any(not isinstance(v,D) or not v.is_finite() or v<0 for v in benchmark.values())
                or abs(sum(benchmark.values(),D(0))-1)>D('1e-12')):
            raise ValueError('Benchmark weights must be finite, nonnegative and sum to one')
        symbols=set(weights)|set(benchmark); active={s:weights.get(s,D(0))-benchmark.get(s,D(0)) for s in symbols}
        reference_sectors={}
        for s,b in benchmark.items():
            if b-active_cap>security_cap: infeasible.append('benchmark_security_bounds:'+s)
            if s not in sectors: missing.append('benchmark_sector:'+s)
            else: reference_sectors[sectors[s]]=reference_sectors.get(sectors[s],D(0))+b
        for s,a in active.items():
            if abs(a)>active_cap: violations.append('active_security_cap:'+s)
        for sector in set(reference_sectors)|set(totals):
            if reference_sectors.get(sector,D(0))-sector_active_cap>sector_cap:
                infeasible.append('benchmark_sector_bounds:'+sector)
            if abs(totals.get(sector,D(0))-reference_sectors.get(sector,D(0)))>sector_active_cap:
                violations.append('active_sector_cap:'+sector)
        if daily_covariance is None:
            missing.append('daily_covariance')
        else:
            keys=[(a,b) for a in symbols for b in symbols]
            if any(k not in daily_covariance for k in keys): missing.append('incomplete_daily_covariance')
            elif any(not isinstance(daily_covariance[k],D) or not daily_covariance[k].is_finite() for k in keys):
                raise ValueError('Invalid covariance entries')
            elif any(daily_covariance[a,b]!=daily_covariance[b,a] for a,b in keys):
                raise ValueError('Covariance must be symmetric')
            else:
                variance=sum((active[a]*daily_covariance[a,b]*active[b] for a,b in keys),D(0))
                if variance<0: raise ValueError('Negative active portfolio variance')
                te=(variance*252).sqrt(); result['predicted_annual_tracking_error']=te
                if te>tracking_error_cap: violations.append('tracking_error_cap')
    if betas is None or any(s not in betas for s in weights): missing.append('historical_asset_betas')
    else:
        if any(not isinstance(betas[s],D) or not betas[s].is_finite() for s in weights):
            raise ValueError('Invalid beta estimates')
        beta=sum((w*betas[s] for s,w in weights.items()),D(0)); result['estimated_beta']=beta
        if not beta_lower<=beta<=beta_upper: violations.append('beta_range')
    result.update(violations=violations,missing_inputs=missing,infeasible_constraints=infeasible,
                  status='not_ready' if missing else 'violations' if violations or infeasible else 'within_limits')
    return result
