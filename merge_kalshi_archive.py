# -*- coding: utf-8 -*-
"""Merge Kalshi settlement/trade CSVs into a local append-only archive.

Kalshi's portfolio endpoints are rolling windows (~65 days — see
bq_retention_guard.py, born from the 2026-07-07 incident), and the daily
API pulls overwrite their CSVs in full, so history silently rolls off.
Same policy locally as the BQ guard: retention limits are fine, silent
loss is not. run_dashboards.ps1 merges every pull (plus any seed exports
dropped in the repo root) into Kalshi-*-archive.csv, and the dashboard
analyzers read the archives instead of the raw daily pulls.

Handles both settlement CSV dialects:
  - API format (fetch_settlements_csv.py): integer counts, avg prices in
    DOLLARS ("0.48"), Profit_In_Dollars = net profit.
  - Website "Recent Activity" export: fractional counts ("13.71"), avg
    prices in CENTS ("81.98"), Profit_In_Dollars = gross payout (never
    negative). Detected per file, normalized to the API format so the
    analyzers' cost/pay math stays correct.

Dedup: Settlement rows on (ticker, settled-time truncated to seconds) —
one settlement per market per account, and second-precision survives the
sources' sub-second formatting differences. Other rows (Trades) on the
full row tuple — fills carry no id in this CSV format, but every field is
deterministic per fill, so refetches collapse cleanly. First-seen wins
(the archive's existing row is never overwritten). Website-format TRADE
exports are not supported as inputs — trades only ever come from
fetch_trades_csv.py.

Usage: python merge_kalshi_archive.py <archive.csv> <input.csv|glob> [...]
Glob patterns are expanded here (PowerShell passes them through literally);
inputs that match nothing are skipped so seed files stay optional. The
archive is created on first run and written atomically (tmp + replace).
"""

import csv
import glob
import math
import os
import sys
from datetime import datetime

CSV_COLUMNS = [
    "type", "Status", "Amount_In_Dollars", "Original_Date", "Traded_Time",
    "Last_Updated", "Deposit_Type", "Fee_In_Dollars", "Market_Title",
    "Market_Ticker", "Market_Id", "Filled", "Remaining", "Direction",
    "Order_Type", "Price_In_Cents", "No_Contracts_Owned",
    "No_Contracts_Average_Price_In_Cents", "Yes_Contracts_Owned",
    "Yes_Contracts_Average_Price_In_Cents", "Result", "Profit_In_Dollars",
    "Credit_Reason", "Credit_Type", "Introducing_Broker",
]


def _f(row, key):
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0.0


def is_website_export(rows):
    """Website settlement exports have avg prices in cents (values > $1.50
    are impossible in the API's dollar format) and/or fractional contract
    counts. One such row anywhere marks the whole file."""
    for r in rows:
        if r.get("type") != "Settlement":
            continue
        if _f(r, "Yes_Contracts_Average_Price_In_Cents") > 1.5:
            return True
        if _f(r, "No_Contracts_Average_Price_In_Cents") > 1.5:
            return True
        for k in ("Yes_Contracts_Owned", "No_Contracts_Owned"):
            v = _f(r, k)
            if v != math.floor(v):
                return True
    return False


def normalize_website_settlement(r):
    """Website export row -> API format: floor counts, cents -> dollars,
    gross payout -> net profit (payout is reconstructed from the winning
    side for yes/no results; for voids the exported payout is kept, which
    nets refunds to ~0)."""
    out = dict(r)
    yc = int(_f(r, "Yes_Contracts_Owned"))
    nc = int(_f(r, "No_Contracts_Owned"))
    ya = round(_f(r, "Yes_Contracts_Average_Price_In_Cents") / 100.0, 2)
    na = round(_f(r, "No_Contracts_Average_Price_In_Cents") / 100.0, 2)
    cost = yc * ya + nc * na
    res = (r.get("Result") or "").strip().lower()
    if res == "yes":
        pay = yc * 1.0
    elif res == "no":
        pay = nc * 1.0
    else:
        pay = _f(r, "Profit_In_Dollars")
    out["Yes_Contracts_Owned"] = str(yc)
    out["No_Contracts_Owned"] = str(nc)
    out["Yes_Contracts_Average_Price_In_Cents"] = f"{ya:.2f}"
    out["No_Contracts_Average_Price_In_Cents"] = f"{na:.2f}"
    out["Profit_In_Dollars"] = f"{pay - cost:.2f}"
    return out


def row_key(r):
    if r.get("type") == "Settlement":
        return ("S", r.get("Market_Ticker", ""), (r.get("Original_Date") or "")[:19])
    return ("T",) + tuple(r.get(c, "") for c in CSV_COLUMNS)


def load_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = [{c: (r.get(c) or "") for c in CSV_COLUMNS} for r in csv.DictReader(f)]
    if is_website_export(rows):
        rows = [normalize_website_settlement(r) if r.get("type") == "Settlement" else r
                for r in rows]
        print(f"  {os.path.basename(path)}: website-export format detected, normalized")
    return rows


def sort_ts(r):
    s = r.get("Original_Date") or ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    archive_path = sys.argv[1]

    merged, seen = [], set()

    def absorb(rows):
        added = dupes = 0
        for r in rows:
            k = row_key(r)
            if k in seen:
                dupes += 1
                continue
            seen.add(k)
            merged.append(r)
            added += 1
        return added, dupes

    if os.path.exists(archive_path):
        n, _ = absorb(load_rows(archive_path))
        print(f"archive {archive_path}: {n} existing rows")
    else:
        print(f"archive {archive_path}: starting fresh")

    total_added = 0
    for pattern in sys.argv[2:]:
        paths = sorted(glob.glob(pattern)) if any(ch in pattern for ch in "*?[") \
            else ([pattern] if os.path.exists(pattern) else [])
        if not paths:
            print(f"  {pattern}: no match, skipped")
            continue
        for p in paths:
            if os.path.abspath(p) == os.path.abspath(archive_path):
                continue
            added, dupes = absorb(load_rows(p))
            total_added += added
            print(f"  {os.path.basename(p)}: +{added} new, {dupes} already archived")

    merged.sort(key=sort_ts, reverse=True)
    tmp = archive_path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL,
                           lineterminator="\n")
        w.writeheader()
        for r in merged:
            w.writerow(r)
    os.replace(tmp, archive_path)
    print(f"archive now {len(merged)} rows (+{total_added} this run)")


if __name__ == "__main__":
    main()
