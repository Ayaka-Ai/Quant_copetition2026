"""Trailing risk estimators. NumPy is required only when these functions run."""


def estimate_beta(asset_returns, benchmark_returns):
    import numpy as np
    x=np.asarray(asset_returns,dtype=np.float64); y=np.asarray(benchmark_returns,dtype=np.float64)
    if x.ndim!=2 or y.ndim!=1 or x.shape[0]!=len(y) or len(y)<2 or x.shape[1]<1:
        raise ValueError('Expected aligned time by asset returns and one benchmark series')
    if not np.isfinite(x).all() or not np.isfinite(y).all(): raise ValueError('Nonfinite risk returns')
    xc=x-x.mean(axis=0); yc=y-y.mean(); variance=yc@yc
    if variance<=0: raise ValueError('Zero benchmark variance')
    return (xc.T@yc)/variance


def shrink_covariance(returns, shrinkage=.10):
    """S=(X-mean)'(X-mean)/(T-1); C=(1-a)S+a*diag(S).

    Save C as F F' + diag(d). Nonnegative d proves PSD without clipping eigenvalues.
    """
    import numpy as np
    x=np.asarray(returns,dtype=np.float64)
    if x.ndim!=2 or x.shape[0]<2 or x.shape[1]<1 or not np.isfinite(x).all():
        raise ValueError('Expected finite time by asset returns')
    if not 0<=shrinkage<=1: raise ValueError('Shrinkage must be in [0,1]')
    centered=x-x.mean(axis=0); variance=np.sum(centered*centered,axis=0)/(len(x)-1)
    factors=centered.T*((1-shrinkage)/(len(x)-1))**.5
    diagonal=shrinkage*variance
    if not np.isfinite(factors).all() or not np.isfinite(diagonal).all() or np.any(diagonal<0):
        raise ValueError('Invalid covariance representation')
    lower=float(diagonal.min()); trace=float(variance.sum())
    diagnostics=dict(psd_status='PSD_by_factor_plus_nonnegative_diagonal_construction',
        sample_observations=len(x),assets=x.shape[1],shrinkage=shrinkage,
        minimum_diagonal_loading=lower,trace=trace,
        conservative_condition_number_upper_bound=trace/lower if lower>0 else None,
        strictly_positive_diagonal_loading=lower>0,zero_sample_variance_assets=int(np.sum(variance==0)),
        annualization=252,units='daily_return_covariance')
    return factors,diagonal,diagnostics


def portfolio_variance(factors,diagonal,weights):
    import numpy as np
    f=np.asarray(factors,dtype=float);d=np.asarray(diagonal,dtype=float);w=np.asarray(weights,dtype=float)
    if (f.ndim!=2 or d.shape!=w.shape or w.ndim!=1 or f.shape[0]!=len(w)
            or not np.isfinite(f).all() or not np.isfinite(d).all() or not np.isfinite(w).all() or np.any(d<0)):
        raise ValueError('Invalid compact covariance or portfolio weights')
    return float(np.sum((f.T@w)**2)+np.sum(d*w*w))


def trailing_panel(calendar,lookup,symbols,day,cutoff,returns):
    """Exact calendar windows; no forward fill or compression of missing sessions."""
    import numpy as np
    index={s.session_date:i for i,s in enumerate(calendar.sessions)};i=index[day]
    if i<returns: return None,(),dict.fromkeys(symbols,'insufficient_calendar_history')
    sessions=calendar.sessions[i-returns:i+1];columns=[];accepted=[];excluded={}
    for symbol in symbols:
        rows=[lookup.get((symbol,s.session_date)) for s in sessions]
        if any(o is None or o.model_available_at>cutoff or o.adjusted_close is None or not o.action_basis_clear for o in rows):
            excluded[symbol]='missing_late_or_guarded_price_history';continue
        prices=np.array([float(o.adjusted_close) for o in rows])
        if not np.isfinite(prices).all() or np.any(prices<=0):
            excluded[symbol]='invalid_numeric_price';continue
        columns.append(prices[1:]/prices[:-1]-1);accepted.append(symbol)
    panel=np.column_stack(columns) if columns else np.empty((returns,0))
    return panel,tuple(accepted),excluded
