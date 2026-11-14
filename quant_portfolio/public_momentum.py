"""PUBLIC-only weekly momentum and matched baseline targets, without V1 gates."""
import datetime as dt
from decimal import Decimal as D

from quant_backtest import TargetWeights
from quant_signals.scores import SignalScore
from .momentum import MomentumRules


def build_public_targets(calendar, observations, universe, *, as_of, rules=MomentumRules(),
                         score_provider=None, strategy_id='MOM20_20_EW80', minimum_signal_sessions=0):
    """Proxy fields and modeled time are confined to this research entry point.

    All three portfolios share the strategy's decision dates, execution engine,
    data snapshot and costs. Equal weight has no sector/issuer selection cap.
    """
    if not isinstance(as_of, dt.datetime) or as_of.utcoffset() is None:
        raise ValueError('as_of must be timezone-aware')
    if type(minimum_signal_sessions) is not int or minimum_signal_sessions < 0:
        raise ValueError('Invalid minimum signal history')
    if strategy_id in ('SPY_80_CASH_20', 'ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80'):
        raise ValueError('Strategy ID collides with benchmark')
    members = {m.symbol: m for m in universe}
    if len(members) != len(universe) or 'SPY' in members:
        raise ValueError('Need unique explicit members excluding SPY')
    sessions = calendar.sessions
    session_map = {s.session_date: s for s in sessions}
    lookup = {}
    for o in observations:
        key = (o.symbol, o.session_date)
        if key in lookup:
            raise ValueError('PUBLIC run requires one frozen version per symbol/date')
        if o.session_date not in session_map or o.model_available_at < session_map[o.session_date].close_time:
            raise ValueError('Observation date/time violates calendar')
        lookup[key] = o
    indices = {s.session_date: i for i, s in enumerate(sessions)}
    plans = {k: [] for k in (strategy_id, 'SPY_80_CASH_20', 'ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80')}
    decisions = []
    for session in calendar.weekly_sessions():
        cutoff = session.close_time + dt.timedelta(minutes=rules.cutoff_minutes)
        if cutoff > as_of:
            continue
        i = indices[session.session_date]
        decision = dict(signal_session=session.session_date, signal_time=cutoff,
                        status='warmup', candidates=[], selected_symbols=(), reasons=())
        decisions.append(decision)
        if i + 1 < max(rules.lookback + 1, rules.turnover_window, minimum_signal_sessions):
            continue
        def get(symbol, day):
            o = lookup.get((symbol, day))
            return o if o is not None and o.model_available_at <= cutoff else None
        candidates, unsafe = [], []
        for symbol, member in sorted(members.items()):
            current = get(symbol, session.session_date)
            price_history = [get(symbol, s.session_date) for s in sessions[i-rules.lookback:i+1]]
            liquidity_history = [get(symbol, s.session_date) for s in sessions[i-rules.turnover_window+1:i+1]]
            data, economic = [], []
            momentum = average_proxy = None
            if any(o is None or o.adjusted_close is None for o in price_history):
                data.append('missing_or_guarded_signal_history')
            else:
                momentum = price_history[-1].adjusted_close / price_history[0].adjusted_close - 1
            if any(o is None or o.reported_close_volume_proxy is None for o in liquidity_history):
                data.append('missing_proxy_history')
            else:
                average_proxy = sum((o.reported_close_volume_proxy for o in liquidity_history), D(0)) / rules.turnover_window
            if current is None or current.actual_share_close is None or not current.action_basis_clear:
                data.append('unresolved_current_price_or_action_basis')
                unsafe.append(symbol)
            if not member.sector or not member.issuer_cik:
                data.append('missing_sector_or_issuer')
            if current is not None:
                if not current.research_tradable:
                    economic.append('not_research_tradable')
                if current.actual_share_close is not None and current.actual_share_close < rules.minimum_close:
                    economic.append('below_minimum_price')
            if average_proxy is not None and average_proxy < rules.minimum_turnover:
                economic.append('below_minimum_liquidity_proxy')
            candidates.append(dict(symbol=symbol, sector=member.sector, issuer_cik=member.issuer_cik,
                momentum=momentum, average_reported_close_volume_proxy=average_proxy,
                actual_share_close=current.actual_share_close if current else None,
                model_available_at=current.model_available_at if current else None,
                actual_retrieved_at=current.actual_retrieved_at if current else None,
                source_sha256=current.source_sha256 if current else None,
                eligible=not data and not economic, data_reasons=tuple(data),
                reasons=tuple(data + economic), rank=None, selected=False, target_weight=D(0)))
        if score_provider is not None:
            eligible = tuple(c for c in candidates if c['eligible'])
            scores = score_provider(session.session_date, cutoff, eligible)
            if set(scores) != {c['symbol'] for c in eligible}:
                raise ValueError('Score provider must account for every eligible candidate exactly')
            for c in eligible:
                score = scores[c['symbol']]
                if (not isinstance(score, SignalScore) or score.symbol != c['symbol']
                        or score.signal_session != session.session_date or score.available_at > cutoff):
                    raise ValueError('Score identity/session/availability mismatch')
                c.update(score=score.value, score_details=score.details)
                if score.value is None:
                    c.update(eligible=False, data_reasons=c['data_reasons'] + score.reasons,
                             reasons=c['reasons'] + score.reasons)
        rank_field = 'score' if score_provider is not None else 'momentum'
        ranked = sorted((c for c in candidates if c['eligible']), key=lambda c: (-c[rank_field], c['symbol']))
        selected, issuers, sector_weights = [], set(), {}
        for rank, candidate in enumerate(ranked, 1):
            candidate['rank'] = rank
            reason = None
            if candidate['issuer_cik'] in issuers:
                reason = 'issuer_already_selected'
            elif sector_weights.get(candidate['sector'], D(0)) + rules.security_weight > rules.sector_cap:
                reason = 'sector_cap'
            elif len(selected) >= rules.maximum_positions:
                reason = 'position_limit'
            if reason:
                candidate['reasons'] = (reason,)
                continue
            candidate.update(selected=True, target_weight=rules.security_weight)
            selected.append(candidate['symbol'])
            issuers.add(candidate['issuer_cik'])
            sector_weights[candidate['sector']] = sector_weights.get(candidate['sector'], D(0)) + rules.security_weight
        decision.update(candidates=ranked + [c for c in candidates if not c['eligible']],
                        selected_symbols=tuple(selected), sector_weights=sector_weights)
        if not ranked and any(c['data_reasons'] for c in candidates):
            decision.update(status='data_not_ready', reasons=('empty_pool_with_data_gaps',))
            continue
        spy = get('SPY', session.session_date)
        if spy is None or spy.actual_share_close is None or not spy.action_basis_clear or not spy.research_tradable:
            # Skip all three, rather than comparing different decision schedules.
            decision.update(status='benchmark_data_not_ready', reasons=('SPY_current_data_unavailable',))
            continue
        weights = {s: rules.security_weight for s in selected}
        plans[strategy_id].append(TargetWeights(session.session_date, cutoff, weights,
                                                  tuple(selected), tuple(unsafe)))
        plans['SPY_80_CASH_20'].append(TargetWeights(session.session_date, cutoff, {'SPY': rules.total_cap}))
        equal = {c['symbol']: rules.total_cap / len(ranked) for c in ranked} if ranked else {}
        # Decimal division is finite precision: never cross the cap on rounding.
        if equal:
            first = min(equal)
            equal[first] += rules.total_cap - sum(equal.values(), D(0))
        plans['ELIGIBLE_UNIVERSE_EQUAL_WEIGHT_80'].append(
            TargetWeights(session.session_date, cutoff, equal, tuple(sorted(equal)), tuple(unsafe)))
        decision.update(status='target_ready' if selected else 'cash_target_ready',
                        target_equity_weight=sum(weights.values(), D(0)), eligible_count=len(ranked))
    return dict(targets={k: tuple(v) for k, v in plans.items()}, decisions=decisions)
