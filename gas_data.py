#!/usr/bin/env python3
"""
gas_data.py — AAA national-average data layer for the KXAAAGAS* trading bot.

The settlement source for Kalshi's gas markets (KXAAAGASD/W/M) is the AAA
national average regular gasoline price shown on https://gasprices.aaa.com/
("Today's AAA National Average $4.0990 ... Price as of 7/28/26"). It updates
once per day, roughly 3-4am ET.

This module maintains gas_data/aaa_history.csv — one row per as-of date:
    date,value,lo,hi,source
where `value` is the exact published average when known (live scrape or
Wayback snapshot), and lo/hi is the half-cent settlement bracket implied by
finalized Kalshi daily markets (strikes are 0.5c apart; result yes/no on
"strictly greater" strikes bounds the print) when only that is known.

Sources, best-first: live > wayback > kalshi_mid (bracket midpoint).
A later-run better source overwrites a worse row for the same date.

CLI:
    python gas_data.py live                 # scrape AAA now, append/print
    python gas_data.py backfill-wayback --from 2026-04-01
    python gas_data.py backfill-kalshi      # settled D/W/M brackets
    python gas_data.py validate             # exact values vs Kalshi brackets
    python gas_data.py show [--days 30]
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_DIR, "gas_data")
HISTORY_CSV = os.path.join(DATA_DIR, "aaa_history.csv")

AAA_URL = "https://gasprices.aaa.com/"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gas-data/1.0"

# "Today's AAA National Average $4.0990" (apostrophe may be curly or mangled
# by encoding; don't anchor on it) + "Price as of 7/28/26"
PRICE_RE = re.compile(r"National\s+Average\s*\$\s*([2-9]\.\d{2,4})")
ASOF_RE = re.compile(r"Price\s+as\s+of\s+(\d{1,2}/\d{1,2}/\d{2,4})")

SOURCE_RANK = {"live": 3, "wayback": 2, "kalshi_mid": 1}


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_aaa_html(html: str) -> Tuple[float, date]:
    """Extract (national_average, as_of_date) from gasprices.aaa.com HTML.
    Raises ValueError if either is missing — callers must treat that as
    'no reading', never as zero."""
    pm = PRICE_RE.search(html)
    dm = ASOF_RE.search(html)
    if not pm or not dm:
        raise ValueError("AAA page: national average or as-of date not found")
    value = float(pm.group(1))
    mm, dd, yy = (int(x) for x in dm.group(1).split("/"))
    if yy < 100:
        yy += 2000
    asof = date(yy, mm, dd)
    if not (1.5 <= value <= 9.0):
        raise ValueError(f"AAA page: implausible national average {value}")
    return value, asof


def fetch_live() -> Tuple[float, date]:
    html = _http_get(AAA_URL).decode("utf-8", errors="ignore")
    return parse_aaa_html(html)


# ----------------------------------------------------------------------------
# History CSV
# ----------------------------------------------------------------------------

def load_history() -> Dict[str, dict]:
    rows: Dict[str, dict] = {}
    if not os.path.exists(HISTORY_CSV):
        return rows
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows[r["date"]] = {
                "date": r["date"],
                "value": float(r["value"]) if r["value"] else None,
                "lo": float(r["lo"]) if r["lo"] else None,
                "hi": float(r["hi"]) if r["hi"] else None,
                "source": r["source"],
            }
    return rows


def save_history(rows: Dict[str, dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HISTORY_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "value", "lo", "hi", "source"])
        for k in sorted(rows):
            r = rows[k]
            w.writerow([r["date"],
                        "" if r["value"] is None else f"{r['value']:.4f}",
                        "" if r["lo"] is None else f"{r['lo']:.4f}",
                        "" if r["hi"] is None else f"{r['hi']:.4f}",
                        r["source"]])
    os.replace(tmp, HISTORY_CSV)


def upsert(rows: Dict[str, dict], day: date, value: Optional[float],
           lo: Optional[float], hi: Optional[float], source: str) -> bool:
    """Insert/overwrite a row if the new source outranks the existing one.
    Returns True if the table changed."""
    key = day.isoformat()
    old = rows.get(key)
    if old and SOURCE_RANK.get(old["source"], 0) >= SOURCE_RANK.get(source, 0):
        return False
    rows[key] = {"date": key, "value": value, "lo": lo, "hi": hi, "source": source}
    return True


def series_values(rows: Dict[str, dict]) -> List[Tuple[date, float]]:
    """(date, best-estimate value) ascending. Exact value preferred; bracket
    midpoint otherwise."""
    out = []
    for k in sorted(rows):
        r = rows[k]
        v = r["value"]
        if v is None and r["lo"] is not None and r["hi"] is not None:
            v = (r["lo"] + r["hi"]) / 2.0
        if v is not None:
            out.append((date.fromisoformat(k), v))
    return out


# ----------------------------------------------------------------------------
# Backfill: Wayback Machine (exact values)
# ----------------------------------------------------------------------------

def _get_with_retries(url: str, timeout: float, attempts: int = 4) -> bytes:
    """archive.org is slow and flaky; retry timeouts/resets with backoff."""
    last = None
    for i in range(attempts):
        if i:
            time.sleep(5.0 * i)
        try:
            return _http_get(url, timeout=timeout)
        except Exception as e:
            last = e
            log(f"  retry {i+1}/{attempts} after: {e}")
    raise last


def backfill_wayback(rows: Dict[str, dict], from_date: date, to_date: date) -> int:
    cdx = ("http://web.archive.org/cdx/search/cdx?url=gasprices.aaa.com"
           f"&from={from_date.strftime('%Y%m%d')}&to={to_date.strftime('%Y%m%d')}"
           "&output=json&filter=statuscode:200&collapse=timestamp:8")
    snaps = json.loads(_get_with_retries(cdx, timeout=120))
    stamps = [row[1] for row in snaps[1:]]  # row 0 is the header
    log(f"wayback: {len(stamps)} daily snapshots {from_date} .. {to_date}")
    added = 0
    for i, ts in enumerate(stamps):
        # `id_` = raw archived bytes, no wayback rewriting
        url = f"http://web.archive.org/web/{ts}id_/https://gasprices.aaa.com/"
        try:
            html = _get_with_retries(url, timeout=90, attempts=3).decode("utf-8", errors="ignore")
            value, asof = parse_aaa_html(html)
        except Exception as e:
            log(f"  {ts}: skip ({e})")
            continue
        # Key by the IN-PAGE as-of date: a snapshot taken 2am ET still shows
        # yesterday's print. Same as-of seen twice -> keep latest (identical).
        if upsert(rows, asof, value, None, None, "wayback"):
            added += 1
        if i % 20 == 0:
            log(f"  {i+1}/{len(stamps)} ... {asof} = {value}")
        time.sleep(0.5)   # be polite to archive.org
    return added


# ----------------------------------------------------------------------------
# Backfill: Kalshi settled markets (half-cent brackets)
# ----------------------------------------------------------------------------

_last_kalshi_call = [0.0]


def _kalshi_get(path: str) -> dict:
    """Throttled (~3/s) + 429/5xx retry — these are unauthenticated reads
    sharing the public IP-level rate budget."""
    for attempt in range(5):
        gap = 0.35 - (time.time() - _last_kalshi_call[0])
        if gap > 0:
            time.sleep(gap)
        _last_kalshi_call[0] = time.time()
        try:
            return json.loads(_http_get(KALSHI_BASE + path, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 4:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise


def _event_bracket(event_ticker: str) -> Optional[Tuple[date, float, float, Optional[float]]]:
    """(settle_date, lo, hi, exp_value) from one finalized event's markets.
    Value is in (lo, hi]: lo = highest 'greater' strike that resolved YES,
    hi = lowest that resolved NO."""
    d = _kalshi_get(f"/markets?event_ticker={event_ticker}&limit=100")
    ms = [m for m in d.get("markets", []) if m.get("status") == "finalized"]
    if not ms:
        return None
    strike_date = ms[0].get("expected_expiration_time") or ms[0].get("expiration_time")
    settle_day = datetime.fromisoformat(strike_date.replace("Z", "+00:00")).date()
    lo, hi = None, None
    exp_value = None
    for m in ms:
        if m.get("strike_type") != "greater" or m.get("floor_strike") is None:
            continue
        try:
            exp_value = float(m.get("expiration_value"))
        except (TypeError, ValueError):
            pass
        k = float(m["floor_strike"])
        if m.get("result") == "yes":
            lo = k if lo is None else max(lo, k)
        elif m.get("result") == "no":
            hi = k if hi is None else min(hi, k)
    if lo is None or hi is None or hi < lo:
        return None
    return settle_day, lo, hi, exp_value


def backfill_kalshi(rows: Dict[str, dict]) -> int:
    added = 0
    for series in ("KXAAAGASD", "KXAAAGASW", "KXAAAGASM"):
        cursor = ""
        tickers: List[str] = []
        while True:
            path = f"/events?series_ticker={series}&limit=200"
            if cursor:
                path += f"&cursor={cursor}"
            d = _kalshi_get(path)
            evs = d.get("events", [])
            tickers += [e["event_ticker"] for e in evs]
            cursor = d.get("cursor")
            if not cursor or not evs:
                break
        log(f"kalshi {series}: {len(tickers)} events")
        for t in tickers:
            try:
                br = _event_bracket(t)
            except Exception as e:
                log(f"  {t}: skip ({e})")
                continue
            if br is None:
                continue
            settle_day, lo, hi, _ = br
            if upsert(rows, settle_day, None, lo, hi, "kalshi_mid"):
                added += 1
    return added


# ----------------------------------------------------------------------------
# Validation: exact values must land inside Kalshi brackets
# ----------------------------------------------------------------------------

def validate(rows: Dict[str, dict]) -> int:
    """Re-derive Kalshi brackets and check every exact-value row against its
    bracket. Returns count of violations."""
    brackets: Dict[str, Tuple[float, float]] = {}
    tmp: Dict[str, dict] = {}
    backfill_kalshi(tmp)
    for k, r in tmp.items():
        brackets[k] = (r["lo"], r["hi"])
    bad = 0
    checked = 0
    for k, r in rows.items():
        if r["value"] is None or k not in brackets:
            continue
        lo, hi = brackets[k]
        checked += 1
        if not (lo < r["value"] <= hi + 1e-9):
            bad += 1
            log(f"VIOLATION {k}: exact {r['value']:.4f} outside ({lo:.3f}, {hi:.3f}]")
    log(f"validate: {checked} exact-value days checked against Kalshi brackets, "
        f"{bad} violations")
    return bad


# ----------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("live")
    bw = sub.add_parser("backfill-wayback")
    bw.add_argument("--from", dest="from_date", default="2026-04-01")
    bw.add_argument("--to", dest="to_date", default=None)
    sub.add_parser("backfill-kalshi")
    sub.add_parser("validate")
    sh = sub.add_parser("show")
    sh.add_argument("--days", type=int, default=30)
    args = ap.parse_args(argv)

    rows = load_history()
    if args.cmd == "live":
        value, asof = fetch_live()
        changed = upsert(rows, asof, value, None, None, "live")
        log(f"AAA national average {value:.4f} as of {asof} "
            f"({'saved' if changed else 'already recorded'})")
        if changed:
            save_history(rows)
    elif args.cmd == "backfill-wayback":
        frm = date.fromisoformat(args.from_date)
        to = date.fromisoformat(args.to_date) if args.to_date else date.today()
        added = backfill_wayback(rows, frm, to)
        save_history(rows)
        log(f"wayback backfill: {added} rows added/updated; "
            f"history now {len(rows)} days")
    elif args.cmd == "backfill-kalshi":
        added = backfill_kalshi(rows)
        save_history(rows)
        log(f"kalshi backfill: {added} rows added; history now {len(rows)} days")
    elif args.cmd == "validate":
        return 1 if validate(rows) else 0
    elif args.cmd == "show":
        vals = series_values(rows)[-args.days:]
        prev = None
        for d, v in vals:
            src = rows[d.isoformat()]["source"]
            delta = "" if prev is None else f"{(v - prev) * 100:+.2f}c"
            print(f"{d}  {v:.4f}  {delta:>8}  [{src}]")
            prev = v
    return 0


if __name__ == "__main__":
    sys.exit(main())
