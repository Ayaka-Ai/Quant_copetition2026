"""Run from repository root: python3 -m examples.run_account"""
import csv
from dataclasses import asdict
import json
from pathlib import Path
from examples.run_signals import encode
from quant_data import load_daily_bars
from quant_backtest import simulate, SimulationConfig

ROOT = Path(__file__).resolve().parents[1]


def main():
    data = load_daily_bars(ROOT/'experiments/databento_smoke/data/b')
    config = SimulationConfig()
    result = simulate(data.bars, config)
    output = ROOT/'outputs/account_b'
    output.mkdir(parents=True, exist_ok=True)
    for name in ['orders','trades','daily','positions']:
        rows=result[name]
        # Headers remain meaningful even if no fills occur in a different sample.
        if rows:
            with (output/(name+'.csv')).open('w', newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader()
                for row in rows:
                    writer.writerow({k:'' if v is None else str(v) for k,v in row.items()})
        else:
            (output/(name+'.csv')).write_text('')
    report=dict(config=asdict(config), source=asdict(data.info), **result['summary'],
                limitations=['Next observed UTC daily close is an execution proxy, not an official auction fill.',
                             'Publication delay unknown; execution is simulated at the following bar end.',
                             'No corporate actions, dividends, interest, tax, regulatory fees or settlement delay.',
                             'Fixed initial equal cash sleeves; no leverage, daily rebalancing or forced final exit.',
                             'No liquidity/partial-volume model; residual cash-limited order quantity is cancelled.',
                             'Slippage already included in fill price; do not subtract it again.',
                             'Demonstration accounting results, not validated strategy performance.'])
    (output/'summary.json').write_text(json.dumps(report,default=encode,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result['summary'],default=encode,ensure_ascii=False,indent=2))
    print('Saved:',output)


if __name__=='__main__':
    main()
