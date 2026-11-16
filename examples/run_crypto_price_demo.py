"""Offline Qlib smoke test with synthetic prices; no market-data download."""
import argparse
import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd
import qlib
from qlib.data.dataset.handler import DataHandlerLP

from crypto_factor import CryptoFactorHandler, PRICE_FACTOR_NAMES


def write_synthetic_provider(path, periods=96):
    """Write an isolated daily Qlib fixture, including weekends.

    Qlib's FileFeatureStorage format is [start calendar index, float32 values].
    The destination must be empty; this helper never edits an existing dataset.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ValueError("Synthetic provider destination must be empty")
    dates = pd.date_range("2024-01-01", periods=periods, freq="D")
    (path / "calendars").mkdir()
    (path / "instruments").mkdir()
    (path / "calendars/day.txt").write_text(
        "\n".join(dates.strftime("%Y-%m-%d")) + "\n", encoding="utf-8"
    )
    instruments = []
    prices = {}
    t = np.arange(periods, dtype=float)
    for symbol, trend in (("SYNTH_A", 0.4), ("SYNTH_B", -0.15)):
        close = (100 + trend * t + 3 * np.sin(t / 4)).astype("<f4")
        frame = pd.DataFrame({"close": close, "high": close + 2, "low": close - 2}, index=dates)
        prices[symbol] = frame
        feature_path = path / "features" / symbol.lower()
        feature_path.mkdir(parents=True)
        for field in frame:
            np.concatenate(([0], frame[field])).astype("<f4").tofile(feature_path / (field + ".day.bin"))
        instruments.append(f"{symbol}\t{dates[0]:%Y-%m-%d}\t{dates[-1]:%Y-%m-%d}")
    (path / "instruments/all.txt").write_text("\n".join(instruments) + "\n", encoding="utf-8")
    return prices


def init_provider(path):
    # Single process and no Redis/disk expression cache; all inputs are local.
    qlib.init(provider_uri=str(Path(path).resolve()), region="us", kernels=1,
              expression_cache=None, dataset_cache=None, logging_level="ERROR")


def load_price_factors(prices):
    dates = next(iter(prices.values())).index
    handler = CryptoFactorHandler(
        instruments=list(prices), start_time=dates[60], end_time=dates[-1],
        factor_names=PRICE_FACTOR_NAMES, infer_processors=[], learn_processors=[],
    )
    return handler.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_R)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs/crypto_price_demo"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qlib_synthetic_") as folder:
        prices = write_synthetic_provider(folder)
        init_provider(folder)
        result = load_price_factors(prices)
    exported = result.copy()
    exported.columns = [name for _, name in exported.columns]
    exported.to_csv(args.output / "factors.csv")
    summary = {
        "synthetic": True, "qlib_version": qlib.__version__,
        "calendar": "daily including weekends; fixture labels only, no publication-time claim",
        "instruments": list(prices), "input_bars_per_instrument": len(next(iter(prices.values()))),
        "warmup_bars": 60, "rows": len(result), "factor_names": list(PRICE_FACTOR_NAMES),
        "complete_feature_rows": int(result["feature"].notna().all(axis=1).sum()),
        "available_label_rows": int(result["label"].notna().all(axis=1).sum()),
        "label": "close(t+2)/close(t+1)-1; unnormalized",
        "purpose": "Qlib loading and formula validation; no IC or return evidence",
    }
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
