"""Weekly constrained momentum targets with explicit calendar and data gates."""
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, fields
from decimal import Decimal as D
from pathlib import Path
from typing import Optional, Tuple

from quant_backtest.portfolio import TargetWeights, TradingSession
from quant_signals.momentum import MomentumObservation, momentum_snapshot


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    sector: Optional[str]
    issuer_cik: Optional[str]

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.strip():
            raise ValueError('Invalid universe symbol')
        if any(v is not None and (not isinstance(v, str) or not v.strip())
               for v in (self.sector, self.issuer_cik)):
            raise ValueError('Sector/issuer must be nonempty strings or None')


@dataclass(frozen=True)
class DataReadiness:
    regular_session_verified: bool = False
    point_in_time_adjustment_verified: bool = False
    unadjusted_share_prices_verified: bool = False
    consolidated_turnover_verified: bool = False
    publication_times_verified: bool = False

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not bool for f in fields(self)):
            raise ValueError('Readiness checks require explicit bool values')

    @property
    def missing_checks(self):
        return tuple(f.name for f in fields(self) if not getattr(self, f.name))


@dataclass(frozen=True)
class MomentumCalendar:
    sessions: Tuple[TradingSession, ...]
    coverage_start: dt.date
    coverage_end: dt.date

    def __post_init__(self):
        sessions = tuple(self.sessions)
        if (type(self.coverage_start) is not dt.date or type(self.coverage_end) is not dt.date
                or self.coverage_start > self.coverage_end or not sessions):
            raise ValueError('Calendar needs explicit complete coverage dates and sessions')
        if any(not isinstance(s, TradingSession) or s.kind != 'regular' for s in sessions):
            raise ValueError('Momentum requires regular-session calendar semantics')
        if any(not self.coverage_start <= s.session_date <= self.coverage_end for s in sessions):
            raise ValueError('Session outside calendar coverage')
        if any(a.session_date >= b.session_date or a.close_time > b.open_time
               for a, b in zip(sessions, sessions[1:])):
            raise ValueError('Calendar must be unique, ordered and nonoverlapping')
        object.__setattr__(self, 'sessions', sessions)

    def weekly_sessions(self):
        weeks = {}
        for session in self.sessions:
            monday = session.session_date - dt.timedelta(days=session.session_date.weekday())
            if self.coverage_start <= monday and monday + dt.timedelta(days=6) <= self.coverage_end:
                weeks[monday] = session
        return tuple(weeks[key] for key in sorted(weeks))


@dataclass(frozen=True)
class MomentumRules:
    lookback: int = 20
    turnover_window: int = 30
    minimum_close: D = D('5')
    minimum_turnover: D = D('20000000')
    maximum_positions: int = 20
    security_weight: D = D('.04')
    sector_cap: D = D('.20')
    total_cap: D = D('.80')
    cutoff_minutes: int = 30

    def __post_init__(self):
        if any(type(x) is not int or x < 1 for x in
               (self.lookback, self.turnover_window, self.maximum_positions)):
            raise ValueError('Lookbacks and position count must be positive integers')
        if type(self.cutoff_minutes) is not int or self.cutoff_minutes < 0:
            raise ValueError('Invalid signal cutoff')
        if any(not isinstance(x, D) or not x.is_finite() or x <= 0 for x in
               (self.minimum_close, self.minimum_turnover, self.security_weight, self.sector_cap, self.total_cap)):
            raise ValueError('Rule values must be positive finite Decimal values')
        if not self.security_weight <= self.sector_cap <= self.total_cap <= 1:
            raise ValueError('Require security <= sector <= total cap <= 1')
        if self.security_weight * self.maximum_positions > self.total_cap:
            raise ValueError('Position count times weight exceeds total cap')


def load_momentum_config(path):
    """Load supported V1 rules and fingerprint-checked frozen membership."""
    path = Path(path)
    cfg = json.loads(path.read_text())
    if cfg.get('schema_version') != '1.0' or cfg.get('strategy_id') != 'US_MOM20_EW':
        raise ValueError('Only strict US_MOM20_EW V1 configuration is supported here; '
                         'public exploration rules require a separate adapter')
    supported = (
        cfg['schema_version'] == '1.0', cfg['universe']['mode'] == 'frozen_snapshot',
        cfg['universe']['maximum_share_classes_per_issuer'] == 1,
        cfg['universe']['manual_exclusions'] == [],
        cfg['signal']['type'] == 'cross_sectional_momentum',
        cfg['signal']['sort'] == ['momentum_descending_unrounded', 'symbol_ascending_ASCII'],
        cfg['signal']['require_positive_momentum'] is False,
        cfg['schedule']['frequency'] == 'weekly', cfg['data']['session'] == 'US_regular_session',
        cfg['schedule']['signal_session'] == 'last_exchange_session_of_each_Monday_to_Sunday_week',
        cfg['schedule']['missed_signal_cutoff'] == 'skip_week_no_late_catchup',
        cfg['eligibility']['require_all_30_turnover_observations'] is True,
        cfg['eligibility']['require_all_21_signal_closes'] is True,
        cfg['eligibility']['require_known_sector_and_issuer'] is True,
        cfg['eligibility']['require_current_tradability_and_permission'] is True,
        cfg['data']['require_data_available_by_signal_cutoff'] is True,
        cfg['data']['allow_forward_filling_for_eligibility_or_signals'] is False,
        cfg['portfolio']['selection_algorithm'] == 'scan_ranked_candidates_skip_ineligible_issuer_or_sector_limit_until_20_or_exhausted',
        cfg['execution']['reference_price'] == 'unadjusted_next_regular_session_open',
        all(v is False for v in cfg['optional_features'].values()),
    )
    if not all(supported):
        raise ValueError('Configuration contains unsupported strategy semantics')
    universe_path = path.parent / cfg['universe']['snapshot_file']
    raw = universe_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != cfg['universe']['snapshot_sha256']:
        raise ValueError('Frozen universe fingerprint mismatch')
    snapshot = json.loads(raw)
    universe = tuple(UniverseMember(r['symbol'], r['sector'], r['issuer_cik']) for r in snapshot['records'])
    if len(universe) != snapshot['security_count'] or len({m.symbol for m in universe}) != len(universe):
        raise ValueError('Invalid frozen universe count or duplicate symbols')
    rules = MomentumRules(lookback=cfg['signal']['lookback_return_intervals'],
                          turnover_window=cfg['eligibility']['turnover_lookback_sessions'],
                          minimum_close=D(cfg['eligibility']['minimum_unadjusted_close_usd_inclusive']),
                          minimum_turnover=D(cfg['eligibility']['minimum_average_daily_turnover_usd_inclusive']),
                          maximum_positions=cfg['portfolio']['maximum_positions'],
                          security_weight=D(cfg['portfolio']['target_weight_per_selected_security']),
                          sector_cap=D(cfg['portfolio']['maximum_target_sector_weight']),
                          total_cap=D(cfg['portfolio']['maximum_target_equity_weight']),
                          cutoff_minutes=cfg['schedule']['signal_cutoff_minutes_after_session_close'])
    if cfg['signal']['required_close_observations'] != rules.lookback + 1:
        raise ValueError('Momentum requires lookback+1 closes')
    return rules, universe, cfg


def build_momentum_targets(calendar, observations, universe, readiness, *, as_of, rules=MomentumRules(),
                           activation_date=None):
    """Generate weekly decisions and executable targets only when data is ready.

    Calendar coverage is independent of the observed price sample. No target
    event means retain positions, whereas a genuinely empty eligible pool can
    produce an explicit empty target. Skipped/data-blocked decisions never do.
    """
    if not isinstance(calendar, MomentumCalendar) or not isinstance(readiness, DataReadiness):
        raise ValueError('Explicit momentum calendar and data readiness are required')
    if not isinstance(rules, MomentumRules):
        raise ValueError('rules must be MomentumRules')
    if not isinstance(as_of, dt.datetime) or as_of.utcoffset() is None:
        raise ValueError('as_of must be timezone-aware')
    activation_date = calendar.coverage_start if activation_date is None else activation_date
    if type(activation_date) is not dt.date:
        raise ValueError('activation_date must be a date')
    universe, observations = tuple(universe), tuple(observations)
    if not universe or any(not isinstance(m, UniverseMember) for m in universe):
        raise ValueError('Nonempty explicit universe required')
    if len({m.symbol for m in universe}) != len(universe):
        raise ValueError('Duplicate universe symbol')
    members = {m.symbol: m for m in universe}
    session_indices = {s.session_date: i for i, s in enumerate(calendar.sessions)}
    for observation in observations:
        if not isinstance(observation, MomentumObservation):
            raise ValueError('Expected MomentumObservation objects')
        if observation.session_date in session_indices:
            session = calendar.sessions[session_indices[observation.session_date]]
            if observation.available_at < session.close_time:
                raise ValueError('A session close observation cannot be available before its close')
    targets, decisions = [], []
    for session in calendar.weekly_sessions():
        cutoff = session.close_time + dt.timedelta(minutes=rules.cutoff_minutes)
        if session.session_date < activation_date or cutoff > as_of:
            continue
        i = session_indices[session.session_date]
        decision = dict(signal_session=session.session_date, signal_time=cutoff, status='',
                        reasons=(), candidates=[], selected_symbols=(), sector_weights={},
                        target_equity_weight=D(0), target_cash_weight=None)
        decisions.append(decision)
        if readiness.missing_checks:
            decision.update(status='data_not_ready', reasons=readiness.missing_checks)
            continue
        if session.close_available_time > cutoff:
            decision.update(status='missed_signal_cutoff', reasons=('close_published_after_cutoff',))
            continue
        history = tuple(s.session_date for s in calendar.sessions[:i+1])
        if len(history) < max(rules.lookback+1, rules.turnover_window):
            decision.update(status='warmup', reasons=('insufficient_calendar_history',))
            continue
        if i+1 < len(calendar.sessions) and cutoff >= calendar.sessions[i+1].open_time:
            raise ValueError('Signal cutoff must precede next regular session open')
        stats = momentum_snapshot(observations, history, cutoff, tuple(members),
                                  lookback=rules.lookback, turnover_window=rules.turnover_window)
        unsafe_holdings, candidates, data_failures = [], [], False
        for value in stats:
            member, current = members[value.symbol], value.current
            data_reasons = list(value.data_reasons)
            rule_reasons = []
            if not member.sector:
                data_reasons.append('missing_sector')
            if not member.issuer_cik:
                data_reasons.append('missing_issuer')
            if current is None or current.unadjusted_close is None:
                data_reasons.append('missing_current_unadjusted_close')
            if current is None or current.corporate_actions_clear is not True:
                data_reasons.append('unverified_corporate_actions')
            if current is None or current.unadjusted_close is None or current.corporate_actions_clear is not True:
                unsafe_holdings.append(value.symbol)
            for flag in ('tradable', 'permission'):
                v = getattr(current, flag) if current else None
                if v is None:
                    data_reasons.append('unknown_' + flag)
                elif v is False:
                    rule_reasons.append('not_' + flag)
            if current is not None and current.unadjusted_close is not None and current.unadjusted_close < rules.minimum_close:
                rule_reasons.append('below_minimum_price')
            if value.average_turnover_usd is not None and value.average_turnover_usd < rules.minimum_turnover:
                rule_reasons.append('below_minimum_turnover')
            data_failures = data_failures or bool(data_reasons)
            candidate = dict(symbol=value.symbol, sector=member.sector, issuer_cik=member.issuer_cik,
                             momentum=value.momentum, average_turnover_usd=value.average_turnover_usd,
                             unadjusted_close=current.unadjusted_close if current else None,
                             eligible=not data_reasons and not rule_reasons, rank=None, selected=False,
                             target_weight=D(0), reasons=tuple(data_reasons + rule_reasons))
            candidates.append(candidate)
        ranked = sorted((c for c in candidates if c['eligible']), key=lambda c: (-c['momentum'], c['symbol']))
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
        # Keep eligible ranks first and retain every excluded symbol with reasons.
        decision['candidates'] = ranked + sorted((c for c in candidates if not c['eligible']), key=lambda c: c['symbol'])
        if not ranked and data_failures:
            decision.update(status='data_not_ready', reasons=('empty_eligible_pool_with_data_gaps',))
            continue
        weights = {symbol: rules.security_weight for symbol in selected}
        target = TargetWeights(session.session_date, cutoff, weights, tuple(selected), tuple(unsafe_holdings))
        targets.append(target)
        equity_weight = sum(weights.values(), D(0))
        decision.update(status='target_ready' if selected else 'cash_target_ready',
                        selected_symbols=tuple(selected), sector_weights=sector_weights,
                        target_equity_weight=equity_weight, target_cash_weight=1-equity_weight)
    return dict(targets=tuple(targets), decisions=tuple(decisions),
                missing_checks=readiness.missing_checks,
                calendar_coverage=(calendar.coverage_start, calendar.coverage_end))
