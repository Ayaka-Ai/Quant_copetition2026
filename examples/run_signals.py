"""Offline B-sample demonstration: python3 -m examples.run_signals"""
import argparse
from dataclasses import asdict
import csv
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path

from quant_data import load_daily_bars
from quant_signals import moving_average_signals

ROOT = Path(__file__).resolve().parents[1]


def encode(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample-dir', type=Path, default=ROOT/'experiments/databento_smoke/data/b')
    parser.add_argument('--output-dir', type=Path, default=ROOT/'outputs/ma_signals_b')
    args = parser.parse_args()
    data = load_daily_bars(args.sample_dir)
    signals = moving_average_signals(data.bars)
    if not signals:
        raise SystemExit('No bars; no outputs written')
    summaries = {}
    for symbol in sorted({s.symbol for s in signals}):
        rows = [s for s in signals if s.symbol == symbol]
        ready = [s for s in rows if s.status == 'ready']
        summaries[symbol] = dict(rows=len(rows), warmup=sum(s.status == 'warmup' for s in rows),
                                 ready=len(ready), long=sum(s.target_position == 1 for s in ready),
                                 flat=sum(s.target_position == 0 for s in ready),
                                 first_ready_date=ready[0].provider_bar_date if ready else None,
                                 target_changes=sum(a.target_position != b.target_position
                                                    for a, b in zip(ready, ready[1:])))
    report = dict(strategy='trailing_sma_long_flat', short_window=5, long_window=20,
                  rule='short_ma > long_ma => 1; otherwise 0; warmup => null',
                  source=asdict(data.info), rows=len(signals), by_symbol=summaries,
                  limitations=['Targets are long/flat intentions, not trades or portfolio weights.',
                               'Publication time unknown; signal_not_before_utc is only a lower bound.',
                               'Observed bars, no gap filling; source is unadjusted UTC daily data.',
                               'No execution, transaction costs, cash accounting or performance calculation.'])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir/'signals.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(signals[0])))
        writer.writeheader()
        for signal in signals:
            writer.writerow({k: '' if v is None else encode(v) if isinstance(v, (dt.date, Decimal)) else v
                             for k, v in asdict(signal).items()})
    (args.output_dir/'summary.json').write_text(json.dumps(report, default=encode, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summaries, default=encode, ensure_ascii=False, indent=2))
    print('Saved:', args.output_dir/'signals.csv')
    print('Saved:', args.output_dir/'summary.json')


if __name__ == '__main__':
    main()
