"""Real Qlib evaluation against independent arithmetic on synthetic prices."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


HAS_QLIB = importlib.util.find_spec("qlib") is not None


@unittest.skipUnless(HAS_QLIB, "optional: install requirements-qlib.txt")
class CryptoFactorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from examples.run_crypto_price_demo import write_synthetic_provider, init_provider, load_price_factors
        cls.temp = tempfile.TemporaryDirectory(prefix="crypto_factor_test_")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.prices = write_synthetic_provider(cls.temp.name)
        init_provider(cls.temp.name)
        cls.result = load_price_factors(cls.prices)

    def test_six_factors_match_independent_full_window_arithmetic(self):
        import numpy as np
        from crypto_factor import PRICE_FACTOR_NAMES
        self.assertEqual(len(self.result), 72)
        for symbol, frame in self.prices.items():
            close = frame.close.astype(float)
            expected = {
                "7d_return": np.log(close / (close.shift(7) + 1e-8)),
                "60d_return": np.log(close / (close.shift(60) + 1e-8)),
                "20d_trend_slope_norm": np.log(close).rolling(20).apply(
                    lambda values: np.polyfit(np.arange(20), values, 1)[0], raw=True),
                "close_dist_sma20": (close - close.rolling(20).mean()) / (close.rolling(20).mean() + 1e-8),
                "close_dist_sma50": (close - close.rolling(50).mean()) / (close.rolling(50).mean() + 1e-8),
                "60d_min_max_position": (close - frame.low.rolling(60).min()) / (
                    frame.high.rolling(60).max() - frame.low.rolling(60).min() + 1e-8),
            }
            actual = self.result.xs(symbol, level="instrument")["feature"]
            self.assertEqual(list(actual.columns), list(PRICE_FACTOR_NAMES))
            self.assertTrue(np.isfinite(actual.to_numpy()).all())
            for name, values in expected.items():
                np.testing.assert_allclose(actual[name], values.loc[actual.index], rtol=1e-4, atol=1e-6)

    def test_label_is_forward_t_plus_one_to_t_plus_two_with_missing_tail(self):
        import numpy as np
        for symbol, frame in self.prices.items():
            close = frame.close.astype(float)
            actual = self.result.xs(symbol, level="instrument")[("label", "LABEL0")]
            expected = (close.shift(-2) / close.shift(-1) - 1).loc[actual.index]
            np.testing.assert_allclose(actual, expected, rtol=1e-4, atol=1e-6, equal_nan=True)
            self.assertEqual(int(actual.isna().sum()), 2)

    def test_inventory_covers_all_required_fields_and_explicit_selection(self):
        from crypto_factor import CryptoFactorDL
        inventory = CryptoFactorDL.get_factor_inventory()
        self.assertEqual(len(inventory), 30)
        self.assertEqual(len({row["name"] for row in inventory}), 30)
        required = {field for row in inventory for field in row["required_fields"]}
        registry = json.loads((Path(__file__).resolve().parents[1] / "configs/crypto_factor_fields.json").read_text(encoding="utf-8"))
        self.assertEqual(required, {row["field"] for row in registry["fields"]})
        self.assertEqual(len(required), 25)
        from examples.inspect_crypto_factors import build_inventory
        report = build_inventory(["close", "high", "low"])
        self.assertEqual(report["compatible_by_field_name_count"], 6)
        self.assertFalse(report["source_mapping_verified"])
        self.assertEqual(sum(bool(row["missing_fields"]) for row in report["factors"]), 24)
        requested = ["sopr_dist_one", "7d_return"]
        self.assertEqual(CryptoFactorDL.get_feature_config(requested)[1], requested)
        for invalid in ([], ["missing"], ["7d_return", "7d_return"], "7d_return"):
            with self.assertRaises(ValueError):
                CryptoFactorDL.get_feature_config(invalid)

    def test_future_price_change_does_not_change_past_features(self):
        import numpy as np
        from examples.run_crypto_price_demo import write_synthetic_provider, init_provider, load_price_factors
        with tempfile.TemporaryDirectory(prefix="crypto_future_test_") as folder:
            prices = write_synthetic_provider(folder)
            for symbol, frame in prices.items():
                for field in frame:
                    values = frame[field].to_numpy(copy=True)
                    values[85:] *= 2
                    np.concatenate(([0], values)).astype("<f4").tofile(
                        Path(folder) / "features" / symbol.lower() / (field + ".day.bin"))
            init_provider(folder)
            changed = load_price_factors(prices)
        mask = self.result.index.get_level_values("datetime") < next(iter(self.prices.values())).index[85]
        np.testing.assert_allclose(changed.loc[mask, "feature"], self.result.loc[mask, "feature"], equal_nan=True)
