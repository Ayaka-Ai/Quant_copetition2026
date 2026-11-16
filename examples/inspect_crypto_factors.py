"""Print the formula/field inventory without downloading data."""
import argparse
import json
from pathlib import Path

from crypto_factor import CryptoFactorDL


def build_inventory(available_fields):
    registry = json.loads((Path(__file__).resolve().parents[1] / "configs/crypto_factor_fields.json").read_text(encoding="utf-8"))
    available = set(available_fields)
    factors = []
    for row in CryptoFactorDL.get_factor_inventory():
        missing = sorted(set(row["required_fields"]) - available)
        factors.append(dict(row, missing_fields=missing, compatible_by_field_name=not missing))
    return {
        "provided_field_names": sorted(available),
        "source_mapping_verified": False,
        "note": "Field names alone do not verify source, units, history, availability or quality.",
        "factor_count": len(factors),
        "compatible_by_field_name_count": sum(row["compatible_by_field_name"] for row in factors),
        "fields": registry["fields"], "factors": factors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--available-fields", nargs="*", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    inventory = build_inventory(args.available_fields)
    text = json.dumps(inventory, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
