#!/usr/bin/env python3
"""imm_ca_shadow_report.py -- score the Carbon Arc SHADOW rules before they go
live (Jack 2026-10-16: "build B2, C and D in shadow mode for the November
array listings").

  * C (empty-book rule) and D (1-99 band within the fair): incentive_mm logs
    what each WOULD quote to run-logs/incentive-mm/ca_shadow_<UTC date>.jsonl
    (CA_SHADOW). Each line holds until the market's next line, capped at
    HOLD_CAP_H, and contributes its modelled $/day over that time.
  * B2 (shadow array fairs): carbon_arc_fair writes each sector array's
    composite read to carbon_arc_array_shadow.jsonl. Once an array settles,
    the last read of its month is set beside Kalshi's settled value
    (expiration_value), for each composite variant -- which variant (if any)
    IS the array decides whether C can be trusted on the arrays.

READ-ONLY (Kalshi reads are public market data via kalshi_reads).

USAGE:
  python imm_ca_shadow_report.py                  # last 7 days
  python imm_ca_shadow_report.py --days 30
  python imm_ca_shadow_report.py --no-settle      # skip the Kalshi reads
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

KL = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = os.environ.get("IMM_STATUS_DIR",
                            os.path.join(KL, "run-logs", "incentive-mm"))
HOLD_CAP_H = 2.0          # a line stands for at most this long without a successor


def _ts(s: str) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def load_lines(status_dir: str, since: datetime) -> list:
    rows = []
    for path in sorted(glob.glob(os.path.join(status_dir, "ca_shadow_*.jsonl"))):
        day = os.path.basename(path)[len("ca_shadow_"):-len(".jsonl")]
        if day < (since - timedelta(days=1)).strftime("%Y-%m-%d"):
            continue
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if _ts(r["ts"]) >= since.timestamp():
                    rows.append(r)
    return sorted(rows, key=lambda r: (r["ticker"], r["ts"]))


def summarize(rows: list, now_ts: float) -> dict:
    """{"C": {size: $}, "D": $, "D_eligible": $, "series": {series: {...}}}:
    modelled reward dollars over the window, each line held until the next
    line of its market (capped at HOLD_CAP_H) or now."""
    out = {"C": defaultdict(float), "D": 0.0, "D_eligible": 0.0,
           "series": defaultdict(lambda: defaultdict(float)), "markets": set()}
    by_t = defaultdict(list)
    for r in rows:
        by_t[r["ticker"]].append(r)
    for t, rs in by_t.items():
        for i, r in enumerate(rs):
            start = _ts(r["ts"])
            end = _ts(rs[i + 1]["ts"]) if i + 1 < len(rs) else now_ts
            days = max(0.0, min(end - start, HOLD_CAP_H * 3600)) / 86400.0
            s = t.split("-")[0]
            out["markets"].add(t)
            for size, v in ((r.get("C") or {}).get("by_size") or {}).items():
                out["C"][size] += v["usd_day"] * days
                out["series"][s][f"C{size}"] += v["usd_day"] * days
            d = r.get("D")
            if d:
                out["D"] += d["usd_day"] * days
                out["series"][s]["D"] += d["usd_day"] * days
                if d.get("eligible"):
                    out["D_eligible"] += d["usd_day"] * days
    return out


def array_vintages(status_dir: str) -> dict:
    """series -> month -> the last composite read of that month."""
    path = os.path.join(status_dir, "carbon_arc_array_shadow.jsonl")
    last = defaultdict(dict)
    if not os.path.exists(path):
        return last
    with open(path, encoding="utf-8") as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            last[r["series"]][r["month"]] = r
    return last


def settled_values(series: str, get_all) -> dict:
    """measurement month 'YYYY-MM' -> settled array value, from the series'
    finalized markets (every strike carries the same expiration_value)."""
    out = {}
    for m in get_all("/markets", {"series_ticker": series, "limit": 1000,
                                  "status": "settled"}, "markets"):
        v = m.get("expiration_value")
        if v in (None, ""):
            continue
        try:
            when = datetime.fromisoformat(str(m["close_time"]).replace("Z", "+00:00"))
            prev = (when.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            out[prev] = float(v)
        except (KeyError, ValueError):
            continue
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--no-settle", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    rows = load_lines(STATUS_DIR, now - timedelta(days=a.days))
    s = summarize(rows, now.timestamp())
    print(f"Carbon Arc shadow, last {a.days:g} days: {len(rows)} lines, "
          f"{len(s['markets'])} markets")
    for size in sorted(s["C"], key=int):
        print(f"  C (empty-book, {size} lots): ${s['C'][size]:,.2f} modelled "
              f"(${s['C'][size] / a.days:,.2f}/day)")
    print(f"  D (1-99 band): ${s['D']:,.2f} modelled, ${s['D_eligible']:,.2f} on "
          f"eligible programs")
    for ser, v in sorted(s["series"].items(), key=lambda kv: -sum(kv[1].values()))[:15]:
        print(f"    {ser:20s} " + "  ".join(f"{k} ${x:,.2f}" for k, x in sorted(v.items())))
    vint = array_vintages(STATUS_DIR)
    if vint:
        print("Array shadow fairs (last read per month; settled value when known):")
        get_all = None
        if not a.no_settle:
            sys.path.insert(0, KL)
            from kalshi_reads import kalshi_get_all as get_all
        for ser in sorted(vint):
            settled = settled_values(ser, get_all) if get_all else {}
            for month, r in sorted(vint[ser].items()):
                var = "  ".join(f"{k} {v:.2f}" for k, v in sorted((r.get("variants") or {}).items()))
                actual = settled.get(month)
                print(f"  {ser:16s} {month}  data through {r.get('data_through')}  "
                      f"mu {r['mu']:.2f} +-{r['sigma']:.2f}  [{var}]"
                      + (f"  SETTLED {actual:.2f}" if actual is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
