"""Export custom factors from a local price panel."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from factors import FACTORS

# Descriptive export names avoid collisions with Qlib Alpha158's definitions.
EXPORT_NAMES = {"ROC20": "custom_momentum20", "STD20": "custom_lowvol20", "REV3": "custom_reversal3"}


def demo_prices():
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2025-01-01", periods=65)
    rows = []
    for symbol in ("DEMO_A", "DEMO_B", "DEMO_C"):
        prices = 100 * np.exp(np.cumsum(rng.normal(.0005, .012, len(dates))))
        rows.extend({"date": d, "symbol": symbol, "close": float(p)} for d,p in zip(dates,prices))
    return pd.DataFrame(rows)


def build_scores(frame):
    required = {"date", "symbol", "close"}
    if not required.issubset(frame):
        raise ValueError("Input requires date, symbol, close")
    df = frame[list(sorted(required))].copy()
    df["date"] = pd.to_datetime(df.date, errors="raise")
    if df.date.isna().any() or df.date.dt.tz is not None or (df.date != df.date.dt.normalize()).any():
        raise ValueError("date must be a non-missing timezone-free session DATE, without time")
    if df.symbol.isna().any():
        raise ValueError("symbol cannot be missing")
    df["symbol"] = df.symbol.astype(str).str.strip()
    if (df.symbol == "").any() or df.duplicated(["date","symbol"]).any():
        raise ValueError("Empty symbol or duplicate date-symbol")
    df["close"] = pd.to_numeric(df.close, errors="raise")
    if not np.isfinite(df.close).all() or (df.close <= 0).any():
        raise ValueError("Observed close must be finite and positive; do not fill missing prices with zero")
    close = df.pivot(index="date",columns="symbol",values="close").sort_index().sort_index(axis=1)
    parts = []
    for old, fn in FACTORS.items():
        wide = fn(close)
        # Preserve NaNs and every date-symbol pair; never drop warmup observations silently.
        part = (wide.rename_axis(index='date').reset_index().melt(id_vars='date',var_name='symbol',value_name=EXPORT_NAMES[old]).set_index(['date','symbol'])[EXPORT_NAMES[old]])
        parts.append(part)
    scores = pd.concat(parts,axis=1).reset_index().sort_values(["date","symbol"]).reset_index(drop=True)
    scores["ready_all"] = scores[list(EXPORT_NAMES.values())].notna().all(axis=1)
    return scores


def main():
    parser = argparse.ArgumentParser(description="从本地价格表导出 custom factors")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo",action="store_true",help="使用固定 seed 的虚构价格")
    mode.add_argument("--input",type=Path,help="本地 CSV / Parquet，字段 date, symbol, close")
    parser.add_argument("--price-basis",help="使用 --input 时必填：复权与 session 口径")
    parser.add_argument("--output-dir",type=Path,default=Path(__file__).resolve().parent.parent/'data/output')
    args = parser.parse_args()
    if args.input and not args.price_basis:
        parser.error("使用 --input 时必须提供 --price-basis")
    if args.demo:
        frame = demo_prices(); source = 'synthetic_demo_seed_42'; basis='synthetic only, weekdays are not an exchange calendar'
    else:
        if args.input.suffix.lower() not in {'.csv','.parquet'}:
            parser.error("输入文件须为 CSV 或 Parquet")
        frame = pd.read_csv(args.input) if args.input.suffix.lower()=='.csv' else pd.read_parquet(args.input)
        source = args.input.name; basis=args.price_basis
    scores=build_scores(frame)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    output=args.output_dir/'factor_scores.csv'
    scores.to_csv(output,index=False,float_format='%.12g')
    if args.demo:
        frame.to_csv(args.output_dir/'demo_prices.csv',index=False,float_format='%.12g')
    metadata={'source':source,'price_basis':basis,'rows':len(scores),'ready_rows':int(scores.ready_all.sum()),
              'input_sha256':hashlib.sha256(args.input.read_bytes()).hexdigest() if args.input else None,
              'export_names':EXPORT_NAMES,'price_adjustment_performed':False,'calendar_validated':False,
              'factors_sha256':hashlib.sha256(Path(__file__).with_name('factors.py').read_bytes()).hexdigest(),
              'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
              'pandas_version':pd.__version__,'numpy_version':np.__version__,
              'interpretation':'Scores only; no orders or forecast of guaranteed returns'}
    (args.output_dir/'run_info.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"Saved {output}: {len(scores)} rows, {metadata['ready_rows']} ready rows")
    print(scores[scores.ready_all].tail(3).to_string(index=False))


if __name__=='__main__': main()
