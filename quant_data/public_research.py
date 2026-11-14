"""Offline PUBLIC research adapter. Assumed availability never certifies V1."""
import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path
from typing import Optional

from quant_backtest import TradingSession
from quant_portfolio.momentum import MomentumCalendar, UniverseMember
from .action_basis import prepare_action_basis
from .daily import DataContractError
from .public_daily import parse_yahoo_chart, parse_yahoo_events
from .public_universe import digest, initialize_manifest, raw_response
from .us_calendar import exchange_sessions


@dataclass(frozen=True)
class PublicResearchObservation:
    symbol: str
    session_date: dt.date
    model_available_at: dt.datetime
    actual_retrieved_at: dt.datetime
    adjusted_close: Optional[D]
    actual_share_close: Optional[D]
    reported_close_volume_proxy: Optional[D]
    research_tradable: bool
    action_basis_clear: bool
    source_sha256: str
    adjusted_open: Optional[D] = None
    adjusted_high: Optional[D] = None
    adjusted_low: Optional[D] = None

    def __post_init__(self):
        if (type(self.session_date) is not dt.date or any(
                not isinstance(x, dt.datetime) or x.utcoffset() is None
                for x in (self.model_available_at, self.actual_retrieved_at))):
            raise ValueError('Research observations require a date and aware timestamps')
        prices = (self.adjusted_close, self.actual_share_close,
                  self.adjusted_open, self.adjusted_high, self.adjusted_low)
        for value in prices + (self.reported_close_volume_proxy,):
            if value is not None and (not isinstance(value, D) or not value.is_finite() or value < 0):
                raise ValueError('Invalid public research numeric field')
        if any(x is not None and x == 0 for x in prices):
            raise ValueError('Research prices must be positive')


def load_public_research_config(path):
    """Accept only the recorded execution revision of the frozen public rules."""
    path = Path(path)
    cfg = json.loads(path.read_text())
    parent = cfg.get('acquisition_parent', {})
    name = parent.get('config_file', '')
    if not name or Path(name).name != name:
        raise DataContractError('Need a local acquisition-parent configuration')
    parent_path = path.parent / name
    if digest(parent_path) != parent.get('config_sha256'):
        raise DataContractError('Acquisition-parent fingerprint changed')
    original = json.loads(parent_path.read_text())
    allowed = {'rule_version', 'implementation_status', 'implementation_updated_on_hkt',
               'historical_backtest_enabled', 'allowed_run_mode', 'acquisition_parent', 'research_protocol'}
    if ({k: v for k, v in cfg.items() if k not in allowed}
            != {k: v for k, v in original.items() if k not in allowed}):
        raise DataContractError('Backtest revision changed frozen economic/data rules')
    if any(cfg['research_protocol'].get(k) != v for k, v in original['research_protocol'].items()):
        raise DataContractError('Backtest revision changed the acquisition research protocol')
    if (cfg.get('rule_version') != 'PUBLIC-1.0.1' or cfg.get('historical_backtest_enabled') is not True
            or cfg.get('allowed_run_mode') != 'offline_historical_research'
            or cfg.get('broker_routing_enabled') is not False):
        raise DataContractError('Unsupported public execution revision')
    return cfg, parent_path


def adapt_public_dataset(dataset, prepared, sessions):
    """Keep retrieval evidence and a separately named close+30m model clock."""
    schedule = {s.session_date: s for s in sessions}
    bars = {b.session_date: b for b in prepared['bars']}
    unresolved_days = {a.effective_session for a in prepared['actions']
                       if a.kind not in ('split', 'resolve_unheld') and not a.verified}
    if dataset.retrieved_at is None:
        raise DataContractError('Actual retrieval evidence is required')
    result = []
    for source in dataset.bars:
        bar = bars[source.session_date]
        basis_clear = bar.close is not None and source.session_date not in unresolved_days
        complete = all(x is not None and x > 0 for x in
                       (source.yahoo_open, source.yahoo_high, source.yahoo_low, source.yahoo_close,
                        source.yahoo_volume))
        proxy = (source.yahoo_close * source.yahoo_volume
                 if source.yahoo_close is not None and source.yahoo_volume is not None else None)
        # One within-session adjustment factor for feature OHLC only. These
        # total-return-adjusted fields never enter execution or share accounting.
        factor = (source.yahoo_adjusted_close / source.yahoo_close
                  if basis_clear and source.yahoo_adjusted_close is not None and source.yahoo_close is not None else None)
        adjusted = tuple(x * factor if x is not None and factor is not None else None
                         for x in (source.yahoo_open, source.yahoo_high, source.yahoo_low))
        result.append(PublicResearchObservation(
            source.symbol, source.session_date,
            schedule[source.session_date].close_time + dt.timedelta(minutes=30),
            dataset.retrieved_at,
            source.yahoo_adjusted_close if basis_clear else None,
            bar.close, proxy, complete, basis_clear, dataset.sha256, *adjusted))
    return tuple(result)


def load_public_research(input_path, config_path, review_path):
    """Rebuild from hash-pinned raw responses, not previously derived prices."""
    root, config_path, review_path = map(Path, (input_path, config_path, review_path))
    cfg, parent_path = load_public_research_config(config_path)
    if not (root / 'raw/manifest.json').is_file():
        raise DataContractError('Offline cache manifest is missing; no acquisition is performed')
    manifest = initialize_manifest(root / 'raw', parent_path, dt.datetime.now(dt.timezone.utc))
    review = json.loads(review_path.read_text())
    event_path = root / 'corporate_events.json'
    if digest(event_path) != review['source_events_sha256']:
        raise DataContractError('Reviewed event fingerprint changed')
    events, reviews = json.loads(event_path.read_text()), review['split_records']
    identities = {(r['symbol'], r['provider_event_id']) for r in reviews}
    if (len(identities) != len(reviews) or identities !=
            {(e['symbol'], e['source_event_id']) for e in events if e['kind'] == 'splits'}):
        raise DataContractError('Review must cover exactly the pinned split events')
    start, end = (dt.date.fromisoformat(manifest[k]) for k in ('start', 'end_exclusive'))
    anchor = dt.date.fromisoformat(manifest['expected_adjustment_anchor'])
    # This version deliberately requires event/price coverage through the anchor.
    if anchor != end - dt.timedelta(days=1):
        raise DataContractError('This adapter requires cache end at its pinned adjustment anchor')
    exchange = exchange_sessions(start, end)
    sessions = tuple(TradingSession(s.session_date, s.open_time, s.close_time,
                                   s.close_time + dt.timedelta(minutes=30)) for s in exchange)
    calendar = MomentumCalendar(sessions, start, end - dt.timedelta(days=1))
    snapshot = json.loads((config_path.parent / cfg['universe']['snapshot_file']).read_text())
    universe = tuple(UniverseMember(r['symbol'], r['sector'], r['issuer_cik']) for r in snapshot['records'])
    bars, actions, observations, inventory, event_audit = [], [], [], [], []
    by_symbol = {}
    for event in events:
        by_symbol.setdefault(event['symbol'], []).append(event)
    for symbol, entry in sorted(manifest['entries'].items()):
        if entry['status'] != 'received':
            raise DataContractError('Incomplete frozen acquisition: ' + symbol)
        raw = raw_response(root / 'raw', entry)
        # Also verify the reviewed export is an exact rendering of raw events.
        parsed_events = parse_yahoo_events(raw, symbol, start, end)
        # JSON-normalized scalar types can differ (Decimal versus JSON float).
        def normalized(e):
            return (e['kind'], e['source_event_id'],
                    dt.datetime.fromisoformat(str(e['provider_event_time'])).isoformat(),
                    {k: str(D(str(v))) if isinstance(v, (int, float, D)) else v
                     for k, v in e['provider_fields'].items()})
        exported = by_symbol.get(symbol, [])
        if (sorted((normalized(e) for e in parsed_events), key=lambda e: e[:2]) !=
                sorted((normalized(e) for e in exported), key=lambda e: e[:2])
                or any(e['source_sha256'] != entry['sha256'] for e in exported)):
            raise DataContractError('Event export differs from pinned raw response: ' + symbol)
        try:
            dataset = parse_yahoo_chart(raw, symbol,
                         retrieved_at=dt.datetime.fromisoformat(entry['retrieved_at_utc']))
        except DataContractError as exc:
            inventory.append(dict(symbol=symbol, status='source_contract_failed', reason=str(exc)))
            continue
        prepared = prepare_action_basis(dataset, events, reviews, exchange, anchor)
        observations.extend(adapt_public_dataset(dataset, prepared, exchange))
        bars.extend(prepared['bars'])
        actions.extend(prepared['actions'])
        event_audit.extend(prepared['event_audit'])
        inventory.append(dict(symbol=symbol, status='prepared_public_assumptions',
                              rows=len(dataset.bars), guarded_through=prepared['guarded_through'],
                              blocked_price_rows=sum(b.close is None for b in prepared['bars']),
                              actual_retrieved_at=dataset.retrieved_at, source_sha256=dataset.sha256))
    return dict(config=cfg, calendar=calendar, universe=universe, bars=tuple(bars),
                actions=tuple(actions), observations=tuple(observations), inventory=inventory,
                event_audit=event_audit,
                fingerprints=dict(config=digest(config_path), acquisition_config=digest(parent_path),
                    manifest=digest(root / 'raw/manifest.json'), events=digest(event_path),
                    review=digest(review_path), universe=cfg['universe']['snapshot_sha256']))
