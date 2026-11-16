"""Risk overlay on stateful TopK selection. Failure means no new target."""
from decimal import Decimal as D, ROUND_FLOOR
from quant_backtest import TargetWeights
from .weight_optimizer import optimize_weights,risk_diagnostics


class RiskWeightStrategy:
    def __init__(self,selector,risk_store,sectors,config,*,optimize=False):
        self.selector,self.risk_store,self.sectors,self.config=selector,risk_store,sectors,config
        self.optimize=optimize;self.audit=[]

    def __call__(self,state):
        import numpy as np
        # Match warmup for both variants: neither trades without a dated risk version.
        if state.session_date not in self.selector.decisions:return None
        risk=self.risk_store.snapshot(state.session_date,state.available_at,self.config['risk_mode'])
        if risk is None:
            self.audit.append(dict(signal_session=state.session_date,status='no_risk_version'));return None
        target=self.selector(state)
        if target is None:
            self.audit.append(dict(signal_session=state.session_date,status='selector_skipped'));return None
        required=set(target.weights)|set(state.holdings)
        missing=sorted(required-set(risk['symbols']))
        if missing:
            self.audit.append(dict(signal_session=state.session_date,status='incomplete_risk_coverage',missing_symbols=missing));return None
        audit=dict(signal_session=state.session_date,status='target_ready',actual_holdings=dict(state.holdings),
            original_weights=dict(target.weights),risk_mode=risk['mode'],
            before=risk_diagnostics(target.weights,**{k:risk[k] for k in ('symbols','factors','diagonal','betas')}),
            actual=risk_diagnostics(state.weights,**{k:risk[k] for k in ('symbols','factors','diagonal','betas')}))
        weights=dict(target.weights)
        if self.optimize and weights:
            selected=sorted(weights);indices=[risk['symbols'].index(s) for s in selected]
            f=risk['factors'][indices];d=risk['diagonal'][indices]
            cov=f@f.T+np.diag(d)
            try:
                solution=optimize_weights(cov,[self.sectors[s] for s in selected],
                    exposure=float(sum(weights.values(),D(0))),security_cap=float(self.config['security_cap']),
                    sector_cap=float(self.config['sector_cap']),maximum_positions=self.config['maximum_positions'],
                    reference=[float(weights[s]) for s in selected],anchor_penalty=float(self.config['anchor_penalty']),
                    tolerance=float(self.config['tolerance']),max_iterations=self.config['max_iterations'])
                vector=solution.pop('weights');audit['solver']=solution
                if solution['status']=='unique_capacity_solution':
                    # Preserve exact Decimal targets rather than rounding a known unique solution.
                    weights={s:D(self.config['security_cap']) for s in selected}
                else:
                    weights={s:D(str(v)).quantize(D('1e-12'),rounding=ROUND_FLOOR) for s,v in zip(selected,vector)}
                if (any(w<0 or w>D(self.config['security_cap']) for w in weights.values())
                        or sum(weights.values(),D(0))>sum(target.weights.values(),D(0))
                        or sum(target.weights.values(),D(0))-sum(weights.values(),D(0))>D('1e-9')):
                    raise ValueError('Decimal weights failed exposure/security checks')
                for sector in set(self.sectors[s] for s in weights):
                    if sum((w for s,w in weights.items() if self.sectors[s]==sector),D(0))>D(self.config['sector_cap']):
                        raise ValueError('Decimal weights failed sector check')
            except ValueError as exc:
                audit.update(status='optimization_failed_no_target',error=str(exc));self.audit.append(audit);return None
        audit.update(weights=weights,after=risk_diagnostics(weights,
            **{k:risk[k] for k in ('symbols','factors','diagonal','betas')}))
        self.audit.append(audit)
        priority=tuple(s for s in target.buy_priority if weights.get(s,D(0))>0)
        return TargetWeights(target.signal_session,target.signal_time,weights,priority,target.unsafe_holding_symbols,
                             target.maximum_trade_notional,target.symbol_trade_notionals)
