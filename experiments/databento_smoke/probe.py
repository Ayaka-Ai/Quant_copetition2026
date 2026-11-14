"""Metadata and quote queries only. No market-data download endpoint exists here.

Requires Python 3.8+ and curl with a valid system certificate store.
Credentials are passed to curl on stdin, never argv or report files.
"""
import base64
import datetime
import getpass
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlencode

BASE = "https://hist.databento.com/v0/metadata."
ALLOWED = {"list_datasets", "list_schemas", "get_dataset_range", "get_cost"}


def request(endpoint, params, key):
    if endpoint not in ALLOWED:
        raise ValueError("Only metadata queries are allowed")
    auth = base64.b64encode((key + ":").encode()).decode()
    config = 'header = "Authorization: Basic ' + auth + '"\n'
    url = BASE + endpoint
    args = ["curl", "--silent", "--show-error", "--max-time", "30",
            "--proto", "=https", "--config", "-", "--write-out", "\n%{http_code}"]
    # No redirects, retries, verbose logging or download APIs.
    if endpoint == "get_cost":
        args += ["--data", urlencode(params)]
    elif params:
        url += "?" + urlencode(params)
    result = subprocess.run(args + [url], input=config, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        return {"endpoint": endpoint, "params": params, "status": None,
                "error": "curl transport failure", "curl_exit_code": result.returncode}
    body, status = result.stdout.rsplit("\n", 1)
    body = body.replace(key, "[REDACTED]").replace(auth, "[REDACTED]")
    try:
        data = json.loads(body)
    except ValueError:
        data = {"error": "Non-JSON response; body omitted"}
    return {"endpoint": endpoint, "params": params, "status": int(status), "data": data}


def main():
    key = os.environ.get("DATABENTO_API_KEY") or getpass.getpass("Databento API key (hidden): ")
    if not key or key.strip() != key:
        raise SystemExit("Missing key or unexpected surrounding whitespace")
    report = {"queried_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "market_data_downloaded": False, "free_credits_verified": False,
              "download_entitlement_verified": False, "queries": []}

    def query(endpoint, params):
        row = request(endpoint, params, key)
        report["queries"].append(row)
        print(endpoint, params.get("dataset", ""), "HTTP", row["status"], flush=True)
        return row

    catalog = query("list_datasets", {})
    if catalog["status"] == 200 and isinstance(catalog.get("data"), list):
        for dataset in ["EQUS.MINI", "XNAS.ITCH", "DBEQ.BASIC"]:
            if dataset not in catalog["data"]:
                continue
            schemas = query("list_schemas", {"dataset": dataset})
            ranges = query("get_dataset_range", {"dataset": dataset})
            if any(r["status"] in (401, 403, 429) for r in (schemas, ranges)):
                break
            for label, schema, start, end in [
                ("A_small_daily", "ohlcv-1d", "2026-09-14", "2026-09-19"),
                ("B_quarter_daily", "ohlcv-1d", "2026-07-01", "2026-10-01"),
                ("C_one_day_minute", "ohlcv-1m", "2026-09-14", "2026-09-15"),
            ]:
                if schemas["status"] != 200 or schema not in schemas.get("data", []):
                    continue
                params = {"dataset": dataset, "symbols": "AAPL,MSFT,SPY",
                          "schema": schema, "stype_in": "raw_symbol",
                          "stype_out": "instrument_id", "start": start + "T00:00:00Z",
                          "end": end + "T00:00:00Z"}
                row = query("get_cost", params)
                row["scenario"] = label
                if row["status"] == 200:
                    print("Estimated USD:", row["data"], flush=True)
                if row["status"] in (401, 403, 429):
                    break
    output = Path(__file__).parent / "reports" / "metadata_probe.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("Saved:", output)
    print("No market data requested. Credits and download permissions remain unverified.")


if __name__ == "__main__":
    main()
