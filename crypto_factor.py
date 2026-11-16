"""Custom multi-modal factors for crypto assets.

The formulas in :class:`CryptoFactorDL` follow the factor specification supplied
with this project.  They use Qlib expression syntax, exactly as ``Alpha158DL``
does, so Qlib evaluates every expression independently for each instrument and
datetime.

Besides Qlib's standard ``$open``, ``$high``, ``$low``, and ``$close`` fields,
the following daily feature fields must be present in the configured
``provider_uri`` before this handler can be used::

    adx14, di_plus14, di_minus14,
    fed_funds, dxy, cpi_yoy, m2, net_etf_flow, market_cap,
    hashrate, active_addresses, cdd_nvt, sopr_above_one, sopr_below_one,
    sth_supply_profit_loss_ratio, lth_mvrv,
    hv30, hv50, hv100, fg_alt, fg_perc, ssr

``adx14`` and the two DI fields are intentionally inputs rather than being
reimplemented here: standard ADX uses Wilder smoothing, while the supplied
specification defines these factors as already-computed raw ADX/DI values.

Macro and on-chain series are often published after their observation date.
Store them at their *release/availability timestamp* (not merely their
reference-period timestamp) to avoid look-ahead bias.
"""

import re

from qlib.contrib.data.handler import _DEFAULT_LEARN_PROCESSORS, check_transform_proc
from qlib.data.dataset.handler import DataHandlerLP
from qlib.data.dataset.loader import QlibDataLoader


_EPS = "1e-8"
PRICE_FACTOR_NAMES = (
    "7d_return", "60d_return", "20d_trend_slope_norm",
    "close_dist_sma20", "close_dist_sma50", "60d_min_max_position",
)


class CryptoFactorDL(QlibDataLoader):
    """Qlib data loader defining the 30 price, macro, on-chain, volatility and sentiment factors."""

    def __init__(self, config=None, factor_names=None, **kwargs):
        default_config = {"feature": self.get_feature_config(factor_names)}
        if config is not None:
            default_config.update(config)
        super().__init__(config=default_config, **kwargs)

    @staticmethod
    def _get_all_feature_config():
        """Return ``(fields, names)`` in the same format as ``Alpha158DL``.

        All rolling windows are in bars.  With ``freq='day'`` they are trading
        calendar entries (including weekends if supplied); with a different
        frequency they become bars of that frequency.
        """
        eps = _EPS
        fields = [
            # Price / technical factors
            "Log($close/(Ref($close, 7)+%s))" % eps,
            "Log($close/(Ref($close, 60)+%s))" % eps,
            "Slope(Log($close), 20)",
            "($close-Mean($close, 20))/(Mean($close, 20)+%s)" % eps,
            "($close-Mean($close, 50))/(Mean($close, 50)+%s)" % eps,
            "($close-Min($low, 60))/(Max($high, 60)-Min($low, 60)+%s)" % eps,
            "$adx14/100",
            "($di_plus14-$di_minus14)/100",
            # Macro factors
            "$fed_funds-Ref($fed_funds, 60)",
            "($dxy-Mean($dxy, 60))/(Mean($dxy, 60)+%s)" % eps,
            "Log($dxy/(Ref($dxy, 60)+%s))" % eps,
            "$cpi_yoy-Ref($cpi_yoy, 60)",
            "Log(($m2+%s)/(Mean($m2, 60)+%s))" % (eps, eps),
            "Sum($net_etf_flow, 60)/($market_cap+%s)" % eps,
            # On-chain factors
            "Log((Mean($hashrate, 3)+%s)/(Mean(Ref($hashrate, 60), 3)+%s))" % (eps, eps),
            "($active_addresses-Mean($active_addresses, 60))/(Mean($active_addresses, 60)+%s)" % eps,
            "($cdd_nvt-Mean($cdd_nvt, 60))/(Mean($cdd_nvt, 60)+%s)" % eps,
            "If($sopr_above_one>1, $sopr_above_one, $sopr_below_one)-1",
            "Log(($sth_supply_profit_loss_ratio+%s)/(Ref($sth_supply_profit_loss_ratio, 60)+%s))" % (eps, eps),
            "$lth_mvrv-Ref($lth_mvrv, 60)",
            # Volatility factors
            "($hv30-Mean($hv30, 60))/(Mean($hv30, 60)+%s)" % eps,
            "Log(($hv30+%s)/($hv100+%s))" % (eps, eps),
            "Log(($hv50+%s)/(Power(($hv30+%s)*($hv100+%s), 0.5)+%s))" % (eps, eps, eps, eps),
            "Log(($hv50+%s)/(Ref($hv50, 60)+%s))" % (eps, eps),
            # Alternative sentiment and stablecoin-liquidity factors
            "($fg_alt-Mean($fg_alt, 20))/100",
            "Log(($fg_alt+%s)/(Mean($fg_alt, 60)+%s))" % (eps, eps),
            "($fg_perc-Mean($fg_perc, 20))/(Std($fg_perc, 20)+%s)" % eps,
            "($fg_alt-$fg_perc)/100",
            "Log(($ssr+%s)/(Mean($ssr, 20)+%s))" % (eps, eps),
            "Log(($ssr+%s)/(Mean($ssr, 60)+%s))" % (eps, eps),
        ]
        names = [
            "7d_return",
            "60d_return",
            "20d_trend_slope_norm",
            "close_dist_sma20",
            "close_dist_sma50",
            "60d_min_max_position",
            "adx_14",
            "di_spread_14",
            "fed_funds_spread_60d",
            "dxy_dist_sma60",
            "dxy_return_60d",
            "inflation_momentum_60d",
            "m2_momentum_60d",
            "etf_flow_intensity_60d",
            "hash_rate_growth_60d",
            "active_addresses_dist_sma60",
            "nvt_momentum_60d",
            "sopr_dist_one",
            "sth_supply_intensity_60d",
            "lth_mvrv_momentum_60d",
            "hv30_dist_sma60",
            "vol_curve_slope",
            "vol_curve_curvature",
            "hv50_momentum_60d",
            "alt_fg_bound_center",
            "alt_fg_velocity_60d",
            "perc_fg_shock_20d",
            "sentiment_arbitrage_spread",
            "ssr_momentum_20d",
            "ssr_momentum_60d",
        ]
        return fields, names

    @staticmethod
    def get_feature_config(factor_names=None):
        """Select explicitly; the default retains all 30 original formulas."""
        fields, names = CryptoFactorDL._get_all_feature_config()
        if factor_names is None:
            return fields, names
        if isinstance(factor_names, str):
            raise ValueError("factor_names must be a nonempty sequence of names")
        selected = list(factor_names)
        if not selected or len(set(selected)) != len(selected):
            raise ValueError("factor_names must be nonempty and unique")
        unknown = set(selected) - set(names)
        if unknown:
            raise ValueError("Unknown factors: " + ", ".join(sorted(unknown)))
        lookup = dict(zip(names, fields))
        return [lookup[name] for name in selected], selected

    @staticmethod
    def get_factor_inventory():
        fields, names = CryptoFactorDL.get_feature_config()
        return [
            {"name": name, "expression": expression,
             "required_fields": sorted(set(re.findall(r"\$([a-zA-Z_][a-zA-Z_0-9]*)", expression)))}
            for expression, name in zip(fields, names)
        ]


class CryptoFactorHandler(DataHandlerLP):
    """DataHandlerLP wrapper for :class:`CryptoFactorDL`.

    LABEL0 is close(t+2)/close(t+1)-1. Default learning processors normalize it
    across instruments; pass learn_processors=[] to inspect unnormalized labels.
    Pass a custom ``label`` argument to override it.
    """

    def __init__(
        self,
        instruments,
        start_time=None,
        end_time=None,
        freq="day",
        infer_processors=None,
        learn_processors=_DEFAULT_LEARN_PROCESSORS,
        fit_start_time=None,
        fit_end_time=None,
        process_type=DataHandlerLP.PTYPE_A,
        filter_pipe=None,
        inst_processors=None,
        factor_names=None,
        **kwargs,
    ):
        infer_processors = check_transform_proc(
            [] if infer_processors is None else infer_processors, fit_start_time, fit_end_time
        )
        learn_processors = check_transform_proc(learn_processors, fit_start_time, fit_end_time)
        data_loader = {
            "class": CryptoFactorDL,
            "kwargs": {
                "config": {
                    "feature": self.get_feature_config(factor_names),
                    "label": kwargs.pop("label", self.get_label_config()),
                },
                "filter_pipe": filter_pipe,
                "freq": freq,
                "inst_processors": inst_processors,
            },
        }
        super().__init__(
            instruments=instruments,
            start_time=start_time,
            end_time=end_time,
            data_loader=data_loader,
            infer_processors=infer_processors,
            learn_processors=learn_processors,
            process_type=process_type,
            **kwargs,
        )

    @staticmethod
    def get_feature_config(factor_names=None):
        return CryptoFactorDL.get_feature_config(factor_names)

    @staticmethod
    def get_label_config():
        return ["Ref($close, -2)/Ref($close, -1) - 1"], ["LABEL0"]
