#!/usr/bin/env python3
"""Core-CPI fair values for incentive_mm's CPI pilot gate.

Jack 2026-10-11: "build the KXRT, CPI, KXRAINWKND models". The CPI pilot
quotes core only (KXCPICORE pilot events and the KXCPICOREYOY family; the
headline series stay blocked), so this prices core:

  KXCPICORE-<YYMON>-T<K>     "If the seasonally adjusted CPI-U All Items less
                             Food and Energy for <month>, as published by the
                             BLS, increases by above K%" -- the FIRST PRINT of
                             the seasonally adjusted one-month change.
  KXCPICOREYOY-<YYMON>-T<K>  the UNADJUSTED 12-month change (never revised).
Both settle on the change rounded to one decimal, computed from the
three-decimal indexes with standard rounding (2.7502 -> 2.8), so a strike
settles YES exactly when the unrounded change X >= K + 0.05:
    P(YES) = 1 - Phi((K + 0.05 - mu) / sigma(days to the release)).
Checked against Kalshi's settled history (research 2026-10-11): YoY matched
the unadjusted BLS change on 45/45 core events, MoM the first-print adjusted
change on 61/62 (the miss: November 2025, which had no official MoM).

MU, from the BLS indexes only (public domain; api.bls.gov v2, no key):
  - MoM: the 12-month average adjusted change through the latest print L,
    as a geometric mean of the index ((SA_L / SA_L-12) ** (1/12) - 1). This
    IS the Cleveland Fed's core nowcast (their FAQ: a 12-month moving
    average of core MoM; rebuilt to within 0.02), so their feed -- whose
    terms allow non-commercial use only -- is not needed.
  - YoY for month T: the last printed unadjusted YoY (month L, unrounded)
    plus, for each month j = L+1..T, the forecast MoM minus the adjusted MoM
    a year earlier (j-12). October 2025 was never published: its year-ago
    term is 0 and November 2025's is the Sep->Nov two-month change.
SIGMA by days to the release, from the 2022-onward backtest of exactly this
construction (151 scored months; RMSE in points): MoM ~0.124-0.136 at every
horizon; YoY 0.116 inside 4 weeks, 0.19-0.21 at 6-8 weeks, 0.25-0.30 at
10-12, 0.34-0.41 at 14-19. Against Kalshi's daily mids (1,051 markets,
2024-26) the market is slightly better within ~15 days of the release, and
the model is better on core MoM beyond 30 days; strikes the market priced
10-25c settled YES 0-1% of the time. incentive_mm therefore uses this only
as a wide band (CPI_FAIR_TOL_CENTS), not as a price.

Writes CPI_FAIR_FILE for incentive_mm: markets {ticker: {p, event,
fetched_at}} (the shape its fair-file loader reads), plus per event mu,
sigma, the month, the latest print and days to the release. The BLS read is
cached in the file and refreshed every BLS_REFRESH_HOURS (25 keyless queries
a day is BLS's limit); Kalshi's open events hourly.

Standalone:  python cpi_fair.py [--out path]
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
BLS_URL = os.environ.get("IMM_CPI_BLS_URL",
                         "https://api.bls.gov/publicAPI/v2/timeseries/data/")
KALSHI_MARKETS_URL = os.environ.get(
    "IMM_CPI_MARKETS_URL", "https://api.elections.kalshi.com/trade-api/v2/markets")
CORE_SA = "CUSR0000SA0L1E"      # core, seasonally adjusted (MoM)
CORE_NSA = "CUUR0000SA0L1E"     # core, unadjusted (YoY)
# series -> the change it settles on
SERIES = {"KXCPICORE": "mom", "KXCPICOREYOY": "yoy"}
# sigma (points) by days to the release: the first bucket at or past the
# horizon; past the table, its last value
SIGMA_TABLE = {
    "mom": ((14, .124), (28, .124), (42, .127), (56, .130), (70, .130),
            (84, .132), (100, .133), (130, .136)),
    "yoy": ((14, .116), (28, .117), (42, .189), (56, .209), (70, .253),
            (84, .295), (100, .339), (130, .413)),
}
ROUND_HALF = 0.05               # one-decimal rounding: YES iff X >= K + 0.05
BLS_REFRESH_HOURS = float(os.environ.get("IMM_CPI_BLS_REFRESH_HOURS", "3"))
EVENTS_TTL_SECS = 3600
HTTP_TIMEOUT = 30
USER_AGENT = "KL-IMM-cpi-fair/1.0"
_MON = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z [cpi-fair] {msg}",
          flush=True)


def month_add(m: str, k: int) -> str:
    """'2026-08' + k months."""
    y, mo = map(int, m.split("-"))
    t = y * 12 + mo - 1 + k
    return f"{t // 12}-{t % 12 + 1:02d}"


def event_month(event_ticker: str) -> Optional[str]:
    """'KXCPICORE-26NOV' -> '2026-11'; None when the segment does not parse."""
    parts = (event_ticker or "").split("-")
    if len(parts) < 2 or len(parts[1]) != 5:
        return None
    seg = parts[1]
    mon = _MON.get(seg[2:5].upper())
    if mon is None or not seg[:2].isdigit():
        return None
    return f"20{seg[:2]}-{mon:02d}"


def sigma(kind: str, h_days: float) -> float:
    table = SIGMA_TABLE[kind]
    for hh, s in table:
        if h_days <= hh:
            return s
    return table[-1][1]


def p_yes(strike: float, mu: float, sd: float) -> float:
    """P(the rounded change > strike) = P(X >= strike + 0.05)."""
    z = (strike + ROUND_HALF - mu) / sd
    return 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def parse_bls(payload: dict) -> Dict[str, Dict[str, float]]:
    """{series id: {'YYYY-MM': index}} from a BLS v2 response (annual
    averages, M13, dropped)."""
    out: Dict[str, Dict[str, float]] = {}
    for s in ((payload or {}).get("Results") or {}).get("series") or []:
        sid = str(s.get("seriesID") or "")
        vals: Dict[str, float] = {}
        for row in s.get("data") or []:
            per = str(row.get("period") or "")
            if not per.startswith("M") or per == "M13":
                continue
            try:
                vals[f"{int(row['year'])}-{int(per[1:]):02d}"] = float(row["value"])
            except (KeyError, TypeError, ValueError):
                continue
        if sid:
            out[sid] = vals
    return out


def fetch_bls(now: datetime) -> Dict[str, Dict[str, float]]:
    """The two core indexes, this year and the three before (one query)."""
    body = {"seriesid": [CORE_SA, CORE_NSA],
            "startyear": str(now.year - 3), "endyear": str(now.year)}
    r = requests.post(BLS_URL, json=body, timeout=HTTP_TIMEOUT,
                      headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    js = r.json() or {}
    if str(js.get("status") or "") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS: {js.get('status')} {js.get('message')}")
    out = parse_bls(js)
    if not out.get(CORE_SA) or not out.get(CORE_NSA):
        raise RuntimeError("BLS: a core series came back empty")
    return out


def latest_print(idx: Dict[str, Dict[str, float]]) -> Optional[str]:
    """The latest month both core indexes carry."""
    both = set(idx.get(CORE_SA) or {}) & set(idx.get(CORE_NSA) or {})
    return max(both) if both else None


def _change(series: Dict[str, float], end: str, start: str) -> Optional[float]:
    a, b = series.get(end), series.get(start)
    return (a / b - 1.0) * 100.0 if a and b else None


def mom_forecast(idx: Dict[str, Dict[str, float]], last: str) -> Optional[float]:
    """The 12-month average adjusted change through `last`, geometric (a
    missing base month borrows the nearest earlier one, scaled)."""
    sa = idx.get(CORE_SA) or {}
    for back in (12, 13, 14):
        base = month_add(last, -back)
        if sa.get(base) and sa.get(last):
            return ((sa[last] / sa[base]) ** (1.0 / back) - 1.0) * 100.0
    return None


def year_ago_mom(idx: Dict[str, Dict[str, float]], month: str) -> Optional[float]:
    """The adjusted MoM of `month`, with the October 2025 gap: a missing
    month's own change is 0 and the month after it takes the two-month
    change from the last month that exists."""
    sa = idx.get(CORE_SA) or {}
    if month not in sa:
        return 0.0 if month_add(month, -1) in sa else None
    prev = month_add(month, -1)
    if prev in sa:
        return _change(sa, month, prev)
    prev2 = month_add(month, -2)
    return _change(sa, month, prev2) if prev2 in sa else None


def event_mu(kind: str, month: str, idx: Dict[str, Dict[str, float]],
             last: str) -> Optional[float]:
    """mu for an unprinted month (month > last); None without the inputs."""
    f = mom_forecast(idx, last)
    if f is None or month <= last:
        return None
    if kind == "mom":
        return f
    nsa = idx.get(CORE_NSA) or {}
    mu = _change(nsa, last, month_add(last, -12))
    if mu is None:
        return None
    j = month_add(last, 1)
    while j <= month:
        b = year_ago_mom(idx, month_add(j, -12))
        if b is None:
            return None
        mu += f - b
        j = month_add(j, 1)
    return mu


def _signed_or_public(get_json: Optional[Callable], params: dict) -> dict:
    if get_json is not None:
        try:
            js = get_json("/markets", dict(params))
            if isinstance(js, dict):
                return js
        except Exception:
            pass
    for attempt in range(3):
        r = requests.get(KALSHI_MARKETS_URL, params=params, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 2:
            time.sleep(3 * (attempt + 1) ** 2)
            continue
        r.raise_for_status()
        return r.json() or {}
    return {}


def fetch_events(get_json: Optional[Callable] = None) -> Dict[str, dict]:
    """Open core-CPI events: event -> {series, kind, month, close (ISO),
    strikes {ticker: K}}. Only 'greater' (above-K) strikes."""
    out: Dict[str, dict] = {}
    for s, kind in SERIES.items():
        js = _signed_or_public(get_json, {"series_ticker": s, "status": "open",
                                          "limit": 200})
        for m in js.get("markets") or []:
            ev = str(m.get("event_ticker") or "")
            t = str(m.get("ticker") or "")
            month = event_month(ev)
            try:
                k = float(m.get("floor_strike"))
            except (TypeError, ValueError):
                continue
            if not ev or not t or month is None \
                    or str(m.get("strike_type") or "greater") != "greater":
                continue
            e = out.setdefault(ev, {"series": s, "kind": kind, "month": month,
                                    "close": m.get("close_time"), "strikes": {}})
            e["strikes"][t] = k
    return out


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def build(now: datetime, idx: Dict[str, Dict[str, float]],
          events: Dict[str, dict]) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """(markets, event info) for the open events that can be priced."""
    stamp = now.isoformat()
    last = latest_print(idx)
    markets: Dict[str, dict] = {}
    info: Dict[str, dict] = {}
    if last is None:
        return markets, info
    for ev, e in events.items():
        close = _iso_ts(e.get("close"))
        if close is None:
            continue
        h = (close - now.timestamp()) / 86400.0
        mu = event_mu(e["kind"], e["month"], idx, last)
        if mu is None or h <= 0:
            continue
        sd = sigma(e["kind"], h)
        info[ev] = {"series": e["series"], "kind": e["kind"], "month": e["month"],
                    "last_print": last, "mu": round(mu, 4), "sigma": sd,
                    "h_days": round(h, 2)}
        for t, k in e["strikes"].items():
            markets[t] = {"p": round(p_yes(k, mu, sd), 4), "event": ev,
                          "strike": k, "fetched_at": stamp}
    return markets, info


def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    events: Optional[Dict[str, dict]] = None,
                    bls_fn: Callable = fetch_bls) -> Tuple[int, int]:
    """Build and atomically write the CPI fair file; returns (events with a
    fair, open events without one). The BLS read (cached in the file) is
    refreshed every BLS_REFRESH_HOURS, Kalshi's open events hourly; a
    failed read reuses the cached one (no cache: raises)."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    idx = old.get("bls") or {}
    bls_at = float(old.get("bls_at") or 0.0)
    if not idx or ts - bls_at >= BLS_REFRESH_HOURS * 3600:
        try:
            idx = bls_fn(now)
            bls_at = ts
        except Exception as e:
            if not idx:
                raise
            _log(f"BLS read failed ({type(e).__name__}: {str(e)[:100]}); "
                 f"keeping the {((ts - bls_at) / 3600):.1f}h-old read")
    ev_cache = old.get("open_events") or {}
    events_at = float(old.get("events_at") or 0.0)
    if events is None:
        if ev_cache and ts - events_at < EVENTS_TTL_SECS:
            events = ev_cache
        else:
            try:
                events = fetch_events(get_json)
                events_at = ts
            except Exception:
                if not ev_cache:
                    raise
                events = ev_cache
    markets, info = build(now, idx, events)
    payload = {"generated_at": now.isoformat(), "bls_at": bls_at, "bls": idx,
               "events_at": events_at, "open_events": events,
               "last_print": latest_print(idx), "events": info,
               "markets": markets}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    os.replace(tmp, path)
    return len(info), max(0, len(events) - len(info))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "cpi_fair.json"))
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    d = _read_json(args.out)
    _log(f"{ok} events priced, {miss} without a fair; latest print "
         f"{d.get('last_print')}")
    for ev, e in sorted((d.get("events") or {}).items()):
        ps = sorted((m["strike"], m["p"]) for m in (d.get("markets") or {}).values()
                    if m["event"] == ev)
        _log(f"  {ev}: mu {e['mu']:.3f} sd {e['sigma']:.3f} ({e['h_days']:.0f}d) "
             + " ".join(f"T{k:g}:{p * 100:.0f}" for k, p in ps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
