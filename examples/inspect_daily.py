"""Run from repository root: python3 -m examples.inspect_daily"""
from pathlib import Path
from quant_data import load_daily_bars


def main():
    sample = Path(__file__).resolve().parents[1] / 'experiments/databento_smoke/data/b'
    data = load_daily_bars(sample)
    print(data.info)
    for symbol in sorted({b.symbol for b in data.bars}):
        bars = [b for b in data.bars if b.symbol == symbol]
        print(symbol, len(bars), bars[0].provider_bar_date, bars[-1].provider_bar_date)
    print('First bar:', data.bars[0])
    print('AAPL September:', len(load_daily_bars(sample, symbols=['AAPL'],
                                                start='2026-09-01', end='2026-10-01').bars))


if __name__ == '__main__':
    main()
