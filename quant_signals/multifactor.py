"""Frozen traditional CLEAR factors, calculated only from a cutoff's history."""
import json
import statistics
from decimal import Decimal as D
from pathlib import Path

from .scores import SignalScore

FEATURES = ('mom5', 'mom20', 'mom60', 'ma20_deviation', 'ma60_deviation',
            'reversal1', 'reversal3', 'close_position', 'volume_confirmation',
            'neg_vol20', 'neg_downside20', 'neg_gap5', 'log_liquidity20', 'neg_liquidity_cv20')
GROUPS = {
    'trend': FEATURES[:5], 'reversal': ('reversal1', 'reversal3'),
    'intraday_quality': ('close_position',), 'volume_confirmation': ('volume_confirmation',),
    'low_volatility': ('neg_vol20', 'neg_downside20', 'neg_gap5'),
    'liquidity_stability': ('log_liquidity20', 'neg_liquidity_cv20'),
}


def mean(values):
    return sum(values, D(0)) / len(values)


def population_std(values):
    average = mean(values)
    return mean([(v-average)**2 for v in values]).sqrt()


def sector_zscores(values):
    """Median +/- 3 MAD clipping, then population z-score. Constants map to 0."""
    median = statistics.median(values.values())
    mad = statistics.median([abs(v-median) for v in values.values()])
    clipped = {s: min(median+3*mad, max(median-3*mad, v)) if mad else v for s, v in values.items()}
    average = mean(list(clipped.values()))
    std = population_std(list(clipped.values()))
    return ({s: (v-average)/std if std else D(0) for s, v in clipped.items()},
            dict(count=len(values), median=median, mad=mad, clipped_mean=average, population_std=std))


def percentile_ranks(values):
    """Percentile in [0,1], average rank for ties, singleton/constant neutral .5."""
    ordered = sorted(values, key=lambda s: (values[s], s))
    output, i, n = {}, 0, len(ordered)
    while i < n:
        j = i + 1
        while j < n and values[ordered[j]] == values[ordered[i]]:
            j += 1
        rank = D(i+j-1)/2/(n-1) if n > 1 else D('.5')
        output.update({s: rank for s in ordered[i:j]})
        i = j
    return output


def raw_features(history):
    """61 consecutive session observations. None means an unavailable session."""
    if len(history) != 61 or any(o is None or o.adjusted_close is None or not o.action_basis_clear for o in history):
        return None, ('missing_or_guarded_61_session_history',)
    closes = [o.adjusted_close for o in history]
    proxies = [o.reported_close_volume_proxy for o in history[-20:]]
    if any(v is None or v <= 0 for v in proxies):
        return None, ('missing_positive_20_session_liquidity_proxy',)
    if any(o.adjusted_open is None for o in history[-5:]):
        return None, ('missing_adjusted_open_history',)
    current = history[-1]
    if current.adjusted_high is None or current.adjusted_low is None:
        return None, ('missing_adjusted_intraday_range',)
    if current.adjusted_high < current.adjusted_low:
        return None, ('invalid_intraday_range',)
    returns = [b/a-1 for a, b in zip(closes[-21:-1], closes[-20:])]
    gap = mean([abs(history[i].adjusted_open/closes[i-1]-1) for i in range(56, 61)])
    liquidity = mean(proxies)
    span = current.adjusted_high-current.adjusted_low
    position = (min(D(1), max(D(0), (closes[-1]-current.adjusted_low)/span)) if span else D('.5'))
    momentum = closes[-1]/closes[-21]-1
    return dict(
        mom5=closes[-1]/closes[-6]-1, mom20=momentum, mom60=closes[-1]/closes[0]-1,
        ma20_deviation=closes[-1]/mean(closes[-20:])-1,
        ma60_deviation=closes[-1]/mean(closes[-60:])-1,
        reversal1=1-closes[-1]/closes[-2], reversal3=1-closes[-1]/closes[-4],
        close_position=position, volume_confirmation=momentum*proxies[-1]/liquidity,
        neg_vol20=-population_std(returns),
        neg_downside20=-mean([min(v, D(0))**2 for v in returns]).sqrt(),
        neg_gap5=-gap, log_liquidity20=liquidity.ln(),
        neg_liquidity_cv20=-population_std(proxies)/liquidity), ()


def load_factor_config(path):
    cfg = json.loads(Path(path).read_text())
    if (cfg.get('schema_version') != 'traditional_factors_public_1.0'
            or cfg.get('strategy_id') != 'CLEAR_FACTORS_20_EW80'
            or cfg.get('rule_version') != 'FACTORS-PUBLIC-1.0.0'
            or cfg.get('comparison') != 'momentum20_on_identical_complete_factor_pool_and_same_signal_dates'
            or cfg.get('feature_version') != 'clear_traditional_daily_v1'
            or cfg.get('minimum_signal_sessions') != 61
            or cfg.get('broker_routing_enabled') is not False
            or any(cfg['optional_features'].values())
            or cfg.get('normalization') != {
                'group': 'frozen_GICS_sector',
                'winsorization': 'median_plus_minus_3_MAD_skip_if_MAD_zero',
                'zscore': 'population_std_zero_if_constant',
                'ranking': 'pooled_percentile_average_ties_singleton_0.5'}):
        raise ValueError('Unsupported traditional factor semantics')
    for key, expected in (
        ('price_volume_weights', {'trend', 'reversal', 'intraday_quality', 'volume_confirmation'}),
        ('defensive_weights', {'low_volatility', 'liquidity_stability'}),
        ('final_weights_before_normalization', {'price_volume', 'defensive'})):
        values = cfg[key]
        if set(values) != expected or any(not D(v).is_finite() or D(v) < 0 for v in values.values()):
            raise ValueError('Invalid factor weights')
        if sum(map(D, values.values()), D(0)) <= 0:
            raise ValueError('Factor weights must have positive total')
    return cfg


class TraditionalFactorScorer:
    def __init__(self, calendar, observations, config):
        self.sessions, self.config = calendar.sessions, config
        self.indices = {s.session_date: i for i, s in enumerate(self.sessions)}
        self.lookup = {}
        for o in observations:
            key = (o.symbol, o.session_date)
            if key in self.lookup:
                raise ValueError('Duplicate factor observation')
            self.lookup[key] = o
        self.cache = {}

    def _snapshot(self, day, cutoff, candidates):
        identity = (day, cutoff, tuple(sorted((c['symbol'], c['sector']) for c in candidates)))
        if identity in self.cache:
            return self.cache[identity]
        i = self.indices[day]
        raw, missing = {}, {}
        for c in candidates:
            history = []
            for session in self.sessions[max(0, i-60):i+1]:
                o = self.lookup.get((c['symbol'], session.session_date))
                history.append(o if o is not None and o.model_available_at <= cutoff else None)
            features, reasons = raw_features(history)
            if features is None:
                missing[c['symbol']] = reasons
            else:
                raw[c['symbol']] = features
        zscores, normalization = {s: {} for s in raw}, {}
        sectors = {c['sector'] for c in candidates if c['symbol'] in raw}
        for sector in sorted(sectors):
            symbols = [c['symbol'] for c in candidates if c['sector'] == sector and c['symbol'] in raw]
            normalization[sector] = {}
            for feature in FEATURES:
                transformed, audit = sector_zscores({s: raw[s][feature] for s in symbols})
                normalization[sector][feature] = audit
                for s, value in transformed.items():
                    zscores[s][feature] = value
        groups = {s: {name: mean([zscores[s][f] for f in features])
                      for name, features in GROUPS.items()} for s in raw}
        def weighted(s, key):
            weights = {k: D(v) for k, v in self.config[key].items()}
            return sum((groups[s][k]*v for k, v in weights.items()), D(0))/sum(weights.values(), D(0))
        price_volume = {s: weighted(s, 'price_volume_weights') for s in raw}
        defensive = {s: weighted(s, 'defensive_weights') for s in raw}
        ranks = dict(price_volume=percentile_ranks(price_volume), defensive=percentile_ranks(defensive))
        weights = {k: D(v) for k, v in self.config['final_weights_before_normalization'].items()}
        scores = {s: sum((ranks[k][s]*v for k, v in weights.items()), D(0))/sum(weights.values(), D(0)) for s in raw}
        result = dict(raw=raw, missing=missing, zscores=zscores, groups=groups, scores=scores,
                      price_volume=price_volume, defensive=defensive, normalization=normalization,
                      ranks=ranks)
        self.cache[identity] = result
        return result

    def provider(self, mode='traditional'):
        if mode not in ('traditional', 'momentum20'):
            raise ValueError('Unsupported score mode')
        def score(day, cutoff, candidates):
            snapshot = self._snapshot(day, cutoff, candidates)
            output = {}
            for c in candidates:
                symbol = c['symbol']
                if symbol in snapshot['missing']:
                    output[symbol] = SignalScore(symbol, day, cutoff, None,
                                                reasons=snapshot['missing'][symbol])
                    continue
                details = dict(raw_features=snapshot['raw'][symbol], sector_zscores=snapshot['zscores'][symbol],
                    group_scores=snapshot['groups'][symbol], price_volume=snapshot['price_volume'][symbol],
                    defensive=snapshot['defensive'][symbol],
                    price_volume_rank=snapshot['ranks']['price_volume'][symbol],
                    defensive_rank=snapshot['ranks']['defensive'][symbol], mode=mode)
                value = snapshot['scores'][symbol] if mode == 'traditional' else snapshot['raw'][symbol]['mom20']
                output[symbol] = SignalScore(symbol, day, cutoff, value, details)
            return output
        return score
