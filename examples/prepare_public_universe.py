"""Full frozen S&P universe + SPY: optional public fetch, then offline audit."""
import argparse
import datetime as dt
import json
from pathlib import Path

from quant_data.public_universe import initialize_manifest, seed_pilot, fetch_universe, audit_universe, encode

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true', help='Fetch only not-yet-requested symbols; no paid endpoints')
    parser.add_argument('--output', type=Path, default=ROOT/'outputs/public_universe_20261104')
    args = parser.parse_args()
    config = ROOT/'configs/momentum_public_v1.json'
    raw = args.output/'raw'
    manifest = initialize_manifest(raw, config, dt.datetime.now(dt.timezone.utc))
    error = None
    if args.fetch:
        seed_pilot(raw, manifest, ROOT/'outputs/data_acceptance/public_raw')
        try:
            fetch_universe(raw, manifest)
        except (OSError, ValueError, RuntimeError) as exc:
            error = str(exc)
    summary = audit_universe(raw, args.output, config)
    print(json.dumps({k:summary[k] for k in ('inventory_symbols','acquisition_attempted_all','data_status_counts',
        'normalized_bars','corporate_event_counts','public_backtest_ready')},ensure_ascii=False,indent=2,default=encode))
    print('Saved:', args.output/'summary.json', flush=True)
    if error:
        print('Acquisition stopped:', error)
    if error or not summary['acquisition_attempted_all']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
