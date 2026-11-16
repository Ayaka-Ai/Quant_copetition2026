"""Necessary-condition proofs and PSD validation, not a portfolio solver."""
from decimal import Decimal as D, ROUND_CEILING


def validate_covariance(matrix, tolerance=D('1e-18')):
    n=len(matrix)
    if not n or any(len(row)!=n for row in matrix): raise ValueError('Covariance must be square and nonempty')
    a=[[D(str(x)) for x in row] for row in matrix]
    if any(not x.is_finite() for row in a for x in row): raise ValueError('Nonfinite covariance')
    if any(abs(a[i][j]-a[j][i])>tolerance for i in range(n) for j in range(n)):
        raise ValueError('Asymmetric covariance')
    # Semidefinite Cholesky with zero-pivot consistency check; no eigenvalue repair.
    lower=[[D(0)]*n for _ in range(n)]
    for j in range(n):
        pivot=a[j][j]-sum((lower[j][k]**2 for k in range(j)),D(0))
        if pivot < -tolerance: raise ValueError('Covariance is not PSD')
        lower[j][j]=max(pivot,D(0)).sqrt() if pivot>tolerance else D(0)
        for i in range(j+1,n):
            residual=a[i][j]-sum((lower[i][k]*lower[j][k] for k in range(j)),D(0))
            if lower[j][j]: lower[i][j]=residual/lower[j][j]
            elif abs(residual)>tolerance: raise ValueError('Covariance zero pivot is inconsistent; not PSD')
    return dict(status='PSD_within_tolerance', dimension=n,tolerance=tolerance,
                units='daily_return_covariance',annualization=252)


def necessary_conditions(exposure, limits, *, benchmark=None, sectors=None, betas=None):
    exposure=D(exposure); cap=D(limits['security_cap']); sector_cap=D(limits['sector_cap'])
    active=D(limits['active_security_cap']); sector_active=D(limits['active_sector_cap'])
    max_positions=limits['maximum_positions']; conflicts=[]; missing=[]
    if not D(0)<=exposure<=1: raise ValueError('Exposure must be in [0,1]')
    if not D(0)<cap<=sector_cap<=1 or max_positions<1: raise ValueError('Invalid absolute limits')
    required=int((exposure/cap).to_integral_value(rounding=ROUND_CEILING))
    if required>max_positions: conflicts.append('position_count_times_security_cap_below_exposure')
    result=dict(exposure=exposure,minimum_positions_by_security_cap=required,
        required_mean_stock_beta_lower=D(limits['beta_lower'])/exposure if exposure else None,
        proof_scope='Necessary conditions only; passing does not certify an optimizer solution')
    if benchmark is None:
        missing.append('historical_benchmark_weights')
    else:
        b={s:D(str(v)) for s,v in benchmark.items()}
        if any(not v.is_finite() or v<0 for v in b.values()) or abs(sum(b.values(),D(0))-1)>D('1e-12'):
            raise ValueError('Benchmark weights must sum to one')
        symbols=set(b)|set(sectors or {})|set(betas or {})
        lower={s:max(D(0),b.get(s,D(0))-active) for s in symbols}
        upper={s:min(cap,b.get(s,D(0))+active) for s in symbols}
        for s in sorted(symbols):
            if lower[s]>upper[s]: conflicts.append('security_bounds:'+s)
        if sum(v>0 for v in lower.values())>max_positions: conflicts.append('mandatory_holdings_exceed_position_count')
        if sum(lower.values(),D(0))>exposure: conflicts.append('sum_security_lower_bounds_exceeds_exposure')
        if sum(upper.values(),D(0))<exposure: conflicts.append('sum_security_upper_bounds_below_exposure')
        result.update(sum_security_lower_bounds=sum(lower.values(),D(0)),sum_security_upper_bounds=sum(upper.values(),D(0)))
        if sectors is None or any(s not in sectors for s in symbols): missing.append('historical_sector_mapping')
        else:
            sector_lower={}; sector_upper={}
            for sector in sorted(set(sectors[s] for s in symbols)):
                group=[s for s in symbols if sectors[s]==sector]
                weight=sum((b.get(s,D(0)) for s in group),D(0))
                lo=max(D(0),weight-sector_active,sum((lower[s] for s in group),D(0)))
                hi=min(sector_cap,weight+sector_active,sum((upper[s] for s in group),D(0)))
                sector_lower[sector],sector_upper[sector]=lo,hi
                if lo>hi: conflicts.append('sector_bounds:'+sector)
            if sum(sector_lower.values(),D(0))>exposure: conflicts.append('sum_sector_lower_bounds_exceeds_exposure')
            if sum(sector_upper.values(),D(0))<exposure: conflicts.append('sum_sector_upper_bounds_below_exposure')
            result.update(sector_lower_bounds=sector_lower,sector_upper_bounds=sector_upper)
    if betas is None or not betas:
        missing.append('historical_asset_betas')
    else:
        values=[D(str(v)) for v in betas.values()]
        if any(not x.is_finite() for x in values): raise ValueError('Invalid betas')
        # Broad long-only envelope; it deliberately relaxes all allocation limits.
        # Thus an outside band proves infeasibility, an overlap proves nothing.
        lo,hi=exposure*min(values),exposure*max(values)
        result['relaxed_beta_envelope']=(lo,hi)
        if hi<D(limits['beta_lower']) or lo>D(limits['beta_upper']): conflicts.append('beta_band_outside_relaxed_envelope')
        known=set(benchmark or {})|set(sectors or {})
        if not known or any(s not in betas for s in known):
            missing.append('incomplete_betas_for_benchmark')
            # Incomplete beta coverage must not be used for a proof about the full pool.
            conflicts=[c for c in conflicts if c!='beta_band_outside_relaxed_envelope']
    return dict(result,conflicts=conflicts,missing_inputs=missing,
        status='proven_infeasible' if conflicts else 'not_ready' if missing else 'requires_optimizer',
        feasible_solution_certified=False)
