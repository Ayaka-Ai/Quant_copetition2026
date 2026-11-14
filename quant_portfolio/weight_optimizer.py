"""Convex weight allocation on a preselected set, without changing stock selection."""


def project_weights(values,exposure,security_cap,sectors,sector_cap,tolerance=1e-13):
    import numpy as np
    v=np.asarray(values,dtype=float);n=len(v)
    if (v.ndim!=1 or not n or len(sectors)!=n or not np.isfinite(v).all()
            or any(not isinstance(s,str) or not s for s in sectors)): raise ValueError('Invalid projection input')
    if not 0<=exposure<=1 or not 0<security_cap<=1 or not 0<sector_cap<=1: raise ValueError('Invalid allocation limits')
    groups=[np.array([i for i,s in enumerate(sectors) if s==sector]) for sector in sorted(set(sectors))]
    capacity=sum(min(len(g)*security_cap,sector_cap) for g in groups)
    if exposure>capacity+tolerance: raise ValueError('Infeasible exposure capacity')
    if exposure==0: return np.zeros(n)
    sector_levels=[]
    for g in groups:
        if len(g)*security_cap<=sector_cap:
            sector_levels.append(float('-inf'));continue
        lo,hi=float(v[g].min()-security_cap),float(v[g].max())
        for _ in range(80):
            mid=(lo+hi)/2
            if np.clip(v[g]-mid,0,security_cap).sum()>sector_cap: lo=mid
            else: hi=mid
        sector_levels.append(hi)
    def allocation(tau):
        result=np.zeros(n)
        for g,level in zip(groups,sector_levels): result[g]=np.clip(v[g]-max(tau,level),0,security_cap)
        return result
    lo,hi=float(v.min()-security_cap-1),float(v.max()+1)
    for _ in range(100):
        mid=(lo+hi)/2
        if allocation(mid).sum()>exposure: lo=mid
        else: hi=mid
    result=allocation((lo+hi)/2)
    if abs(result.sum()-exposure)>1e-10: raise ValueError('Projection did not reach required exposure')
    return result


def optimize_weights(covariance,sectors,*,exposure=.8,security_cap=.04,sector_cap=.2,
                     maximum_positions=20,reference=None,anchor_penalty=.01,tolerance=1e-10,max_iterations=10000):
    import numpy as np
    c=np.asarray(covariance,dtype=float);n=len(sectors)
    if (n>maximum_positions or c.shape!=(n,n) or n==0 or not np.isfinite(c).all()
            or anchor_penalty<0 or not np.isfinite(anchor_penalty) or tolerance<=0 or max_iterations<1):
        raise ValueError('Invalid optimizer inputs/preselected cardinality')
    if not np.allclose(c,c.T,atol=1e-14,rtol=0): raise ValueError('Asymmetric risk matrix')
    eigen=np.linalg.eigvalsh(c)
    if eigen[0]<-1e-12: raise ValueError('Risk matrix is not PSD')
    ref=np.full(n,exposure/n) if reference is None else np.asarray(reference,dtype=float)
    if ref.shape!=(n,) or not np.isfinite(ref).all(): raise ValueError('Invalid anchor weights')
    project=lambda v:project_weights(v,exposure,security_cap,sectors,sector_cap)
    w=project(ref)
    def objective(x): return float(252*(x@c@x)+anchor_penalty*np.sum((x-ref)**2))
    initial=objective(w)
    if abs(n*security_cap-exposure)<=1e-12:
        return dict(weights=w,status='unique_capacity_solution',iterations=0,objective=initial,
                    initial_objective=initial,projected_gradient_residual=0.,max_constraint_violation=abs(w.sum()-exposure))
    lipschitz=2*(252*max(float(eigen[-1]),0)+anchor_penalty)
    if lipschitz==0:
        return dict(weights=w,status='flat_objective',iterations=0,objective=initial,initial_objective=initial,
                    projected_gradient_residual=0.,max_constraint_violation=abs(w.sum()-exposure))
    for iteration in range(1,max_iterations+1):
        gradient=2*252*c@w+2*anchor_penalty*(w-ref)
        nxt=project(w-gradient/lipschitz)
        residual=float(np.max(np.abs(nxt-w)));w=nxt
        if residual<=tolerance: break
    else: raise ValueError('Optimizer failed to converge')
    violation=max(abs(float(w.sum())-exposure),max(0,float(w.max())-security_cap),max(0,-float(w.min())),
        max(max(0,float(w[np.array(sectors)==s].sum())-sector_cap) for s in set(sectors)))
    final=objective(w)
    if violation>1e-9 or final>initial+1e-10: raise ValueError('Optimizer verification failed')
    return dict(weights=w,status='converged',iterations=iteration,objective=final,initial_objective=initial,
                projected_gradient_residual=residual,max_constraint_violation=violation)


def risk_diagnostics(weights,symbols,factors,diagonal,betas):
    import numpy as np
    from quant_signals.risk_model import portfolio_variance
    absent=sorted(s for s,w in weights.items() if w and s not in symbols)
    if absent:return dict(status='incomplete_covariance',missing_symbols=absent)
    lookup={s:i for i,s in enumerate(symbols)};v=np.zeros(len(symbols))
    for s,w in weights.items():
        if w:v[lookup[s]]=float(w)
    marginal=factors@(factors.T@v)+diagonal*v
    variance=portfolio_variance(factors,diagonal,v)
    contributions=v*marginal*252
    missing_beta=sorted(s for s,w in weights.items() if w and s not in betas)
    return dict(status='diagnostic_only',annual_variance=variance*252,
        annual_volatility=(variance*252)**.5,estimated_NAV_beta=None if missing_beta else
            sum(float(w)*float(betas[s]) for s,w in weights.items() if w),missing_beta_symbols=missing_beta,
        annual_variance_contributions={s:float(contributions[i]) for s,i in lookup.items() if v[i]},
        tracking_error=None,benchmark_active_risk_status='missing_historical_benchmark_weights')
