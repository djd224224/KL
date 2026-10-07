#!/usr/bin/env python3
"""OpenRouter token-usage fair values for incentive_mm's KXTOKENUSE gate.

KXTOKENUSE ("Will OpenRouter total token usage for Sep 21-27, 2026 be above
164T tokens?") and KXTOKENUSEM (the same over a 4-week "month", e.g.
"September 2026 (measured August 31 - September 27)") settle on the token
usage in OpenRouter's AI Model Rankings summed over UTC calendar days,
observed 10:00 AM ET the Monday after. OpenRouter publishes exactly that
data through its official Data API (CC BY 4.0, commercial use with
attribution; any OpenRouter API key; 30 requests/min, 500/day):

    GET https://openrouter.ai/api/v1/datasets/rankings-daily
        ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
    -> 51 rows per completed UTC day (top 50 models + "other")

Verified 2026-09-27 (Jack: "yes build the OpenRouter token usage gate"):
the Monday-Sunday sums reproduce all five settled weeks to Kalshi's
rounding -- 93.38 -> 93.4T, 113.00 -> 113, 115.46 -> 115T, 126.76 -> 127T,
128.90 -> 129T. Only COMPLETED days are published, so the week's last day
lands just after 00:00Z Monday, ~4h before the 03:59Z close.

Model for a window of days (the event's week or month):
    mu    = known days' sum + sum over remaining days of
            base * weekday_factor(day) * (1 + g * OR_TREND_WEIGHT) ** ((h + 3) / 7)
            + OR_BIAS_WEIGHT * BIAS[r] * base
    sigma = OR_SIGMA_MULT * RMSE[r] * base
where (on the days through cap_spikes: none above 1 + OR_SPIKE_CAP x the
median of the 7 before it) base = mean of the last 7 completed days,
weekday_factor = that
weekday's share of its trailing 4 weeks, g = last-7 over prior-7 growth
(clipped -10%..+20%), h = days ahead, r = remaining days. RMSE[r] and
BIAS[r] are MEASURED in units of one day's volume by a backtest over
2026-02..09 (63 week and 30 four-week windows, half trend): one day left
0.09 / +0.05, seven left 0.82 / +0.31, twenty-eight left 7.07 / +3.52 --
usage has been growing, so the plain model runs low and the bias term
corrects it. Strikes are 2T apart; at ~20T/day that is sigma ~1.9T with a
day left and ~16T with a full week left.

Writes OR_FAIR_FILE for incentive_mm (per EVENT ticker: mu, sigma in T,
known / total days, complete) and appends each new completed day to
OR_VINTAGE_FILE. Event windows come from the open markets' own rules text
on Kalshi's markets endpoint (the month is NOT a calendar month), read
SIGNED when incentive_mm passes its reader and publicly otherwise; a weekly
event whose text does not parse falls back to the 7 days before its ticker
date.

The key lives OUTSIDE the repo (it is public): IMM_OR_API_KEY, else
~/.openrouter_key.json {"key": "sk-or-..."}. Never logged.

Standalone:  python openrouter_fair.py [--out path]
"""

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
OR_KEY_FILE = os.environ.get(
    "IMM_OR_KEY_FILE", os.path.join(os.path.expanduser("~"), ".openrouter_key.json"))
OR_API = "https://openrouter.ai/api/v1/datasets/rankings-daily"
KALSHI_MARKETS_URL = os.environ.get(
    "IMM_OR_KALSHI_MARKETS_URL",
    "https://api.elections.kalshi.com/trade-api/v2/markets")
OR_SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_OR_FAIR_SERIES", "KXTOKENUSE,KXTOKENUSEM").split(",") if s.strip())
OR_VINTAGE_FILE = os.environ.get(
    "IMM_OR_VINTAGE_FILE", os.path.join(STATUS_DIR, "openrouter_vintages.jsonl"))
OR_TREND_WEIGHT = _env_float("IMM_OR_TREND_WEIGHT", 0.5)
OR_BIAS_WEIGHT = _env_float("IMM_OR_BIAS_WEIGHT", 1.0)
OR_SIGMA_MULT = _env_float("IMM_OR_SIGMA_MULT", 1.2)
# SPIKE CAP (Jack 2026-10-06: "implement all 3"): in the run-rate inputs --
# base, trend and weekday factors, never the window's own known days, which
# settle at their actual totals -- a day counts at most (1 + OR_SPIKE_CAP) x
# the median of the 7 days before it, so one launch day (8/24-8/26, GLM-5.3-
# Flash: +31-38% on that median) does not run on into the forecast.
# Backtest 8/03-10/04, forecasts at every day of each window: weekly RMSE
# 5.34T -> 4.61T, 4-week 36.2 -> 29.2 (10%: 5.02 / 28.9; 20%: 4.67 / 30.8);
# the mean error goes a little lower (-1.43 -> -2.04T weekly) -- the BIAS
# table below was fit uncapped. 10/05 (+8.1%) is NOT capped at 15%: that
# week's miss was intraday information the completed days do not hold.
# 0 = off.
OR_SPIKE_CAP = _env_float("IMM_OR_SPIKE_CAP", 0.15)
HISTORY_DAYS = 70
HTTP_TIMEOUT = 30
T = 1e12

# Backtest 2026-02..09, units of one day's volume, index = remaining days.
RMSE = [0.0, 0.093, 0.198, 0.308, 0.417, 0.546, 0.66, 0.821, 0.947, 1.139,
        1.327, 1.515, 1.726, 1.9, 2.173, 2.379, 2.649, 2.922, 3.183, 3.497,
        3.803, 4.204, 4.485, 4.858, 5.227, 5.6, 6.055, 6.559, 7.065]
BIAS = [0.0, 0.053, 0.113, 0.17, 0.219, 0.253, 0.28, 0.306, 0.371, 0.475,
        0.555, 0.631, 0.691, 0.763, 0.858, 1.022, 1.214, 1.381, 1.501, 1.607,
        1.699, 1.81, 2.039, 2.325, 2.553, 2.714, 2.885, 3.159, 3.524]

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"])}


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[or-fair] {msg}", flush=True)


# the last signed-read failure, so each distinct one is logged once
_signed_err: Optional[str] = None


def _signed_read(get_json: Optional[Callable[[str, dict], dict]], path: str,
                 params: dict) -> Optional[dict]:
    """A SIGNED Kalshi read through `get_json(path, params)`, or None when
    there is no reader or it failed -- the caller then reads the public
    endpoint as before. incentive_mm passes the reader (2026-09-27): Kalshi
    throttles UNSIGNED /markets list reads from any IP (the window read lost
    about half its 10-minute attempts 06-14Z), while the same read signed
    passed 20/20 in a paired probe. Each distinct failure is logged once."""
    global _signed_err
    if get_json is None:
        return None
    try:
        js = get_json(path, dict(params))
        if not isinstance(js, dict):
            raise ValueError(f"signed read returned {type(js).__name__}")
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:120]}"
        if err != _signed_err:
            _log(f"! signed Kalshi read failed ({err}); reading the public endpoint")
        _signed_err = err
        return None
    if _signed_err is not None:
        _log("signed Kalshi reads back")
        _signed_err = None
    return js


def api_key() -> str:
    """IMM_OR_API_KEY, else OR_KEY_FILE ({"key": ...} or a bare key)."""
    k = os.environ.get("IMM_OR_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(OR_KEY_FILE, encoding="utf-8-sig") as f:
            raw = f.read()
    except OSError:
        return ""
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return str(d.get("key") or d.get("api_key") or "").strip()
    except ValueError:
        pass
    m = re.search(r"sk-or-[A-Za-z0-9_\-]+", raw)
    return m.group(0) if m else ""


def fetch_daily(key: str, start: date, end: date,
                timeout: float = HTTP_TIMEOUT) -> Dict[date, float]:
    """date -> total tokens (all 51 rows summed) for completed UTC days."""
    r = requests.get(OR_API, params={"start_date": start.isoformat(),
                                     "end_date": end.isoformat()},
                     headers={"Authorization": f"Bearer {key}"},
                     timeout=timeout)
    r.raise_for_status()
    out: Dict[date, float] = {}
    for row in (r.json() or {}).get("data") or []:
        try:
            d = date.fromisoformat(str(row["date"])[:10])
            out[d] = out.get(d, 0.0) + float(row["total_tokens"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _month(tok: str) -> Optional[int]:
    return _MONTHS.get(tok.strip().lower()[:3])


def parse_window(text: str, year: int) -> Optional[Tuple[date, date]]:
    """The measured window named in a market's rules: 'measured August 31 -
    September 27', 'for Sep 21-27, 2026' (en dash or hyphen), 'Sep 28-Oct
    4, 2026', or a bracketed range with no year, 'October 2026 (Sep 28-Oct
    25)' -- the October month's wording (2026-09-30: the parser missed it,
    so the event got no window and every KXTOKENUSEM strike stood aside
    fail-closed). `year` is the year of the window's END (the close's year)."""
    t = (text or "").replace("\u2013", "-").replace("\u2014", "-")
    m = re.search(r"measured\s+([A-Za-z]+)\s+(\d{1,2})\s*-\s*([A-Za-z]+)\s+(\d{1,2})", t)
    if m:
        m1, d1, m2, d2 = _month(m.group(1)), int(m.group(2)), _month(m.group(3)), int(m.group(4))
    else:
        m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2})\s*-\s*(?:([A-Za-z]{3,9})\s+)?(\d{1,2}),\s*(\d{4})", t)
        if m:
            year = int(m.group(5))
        else:
            m = re.search(r"\(\s*([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*-\s*"
                          r"(?:([A-Za-z]{3,9})\.?\s+)?(\d{1,2})\s*\)", t)
            if not m:
                return None
        m1, d1 = _month(m.group(1)), int(m.group(2))
        m2 = _month(m.group(3)) if m.group(3) else m1
        d2 = int(m.group(4))
    if not m1 or not m2:
        return None
    try:
        end = date(year, m2, d2)
        start = date(year if m1 <= m2 else year - 1, m1, d1)
    except ValueError:
        return None
    if not (0 <= (end - start).days <= 40):
        return None
    return start, end


def _ticker_date(event_ticker: str) -> Optional[date]:
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m:
        return None
    mo = _month(m.group(2))
    try:
        return date(2000 + int(m.group(1)), mo, int(m.group(3))) if mo else None
    except ValueError:
        return None


def windows_from_markets(markets: List[dict],
                         unparsed: Optional[List[str]] = None) -> Dict[str, dict]:
    """event ticker -> {"series", "start", "end"} from open markets. An event
    whose window does not parse is appended to `unparsed` when a list is
    given, so the fair file can name it instead of dropping it silently."""
    out: Dict[str, dict] = {}
    bad: List[str] = []
    for m in markets or []:
        ev = str(m.get("event_ticker") or "")
        if not ev or ev in out:
            continue
        series = ev.split("-")[0]
        close = str(m.get("close_time") or "")
        year = int(close[:4]) if close[:4].isdigit() else datetime.now(timezone.utc).year
        w = None
        for fld in ("rules_primary", "rules_secondary", "title"):
            w = parse_window(str(m.get(fld) or ""), year)
            if w:
                break
        if w is None and series == "KXTOKENUSE":
            td = _ticker_date(ev)
            if td:
                w = (td - timedelta(days=7), td - timedelta(days=1))
        if w:
            out[ev] = {"series": series, "start": w[0].isoformat(),
                       "end": w[1].isoformat()}
        elif ev not in bad:
            bad.append(ev)
    if unparsed is not None:
        unparsed.extend(e for e in bad if e not in out and e not in unparsed)
    return out


def fetch_windows(series: Tuple[str, ...] = OR_SERIES,
                  timeout: float = HTTP_TIMEOUT,
                  retries: int = 3, backoff: float = 3.0,
                  get_json: Optional[Callable[[str, dict], dict]] = None,
                  unparsed: Optional[List[str]] = None
                  ) -> Dict[str, dict]:
    """Event windows from Kalshi's markets endpoint: signed through
    `get_json` when given (see _signed_read), else -- or when that read
    fails -- the public endpoint, where a 429 or 5xx is retried with a
    growing sleep (3s, 12s, 27s)."""
    markets: List[dict] = []
    for s in series:
        params = {"series_ticker": s, "status": "open", "limit": 200}
        js = _signed_read(get_json, "/markets", params)
        if js is None:
            for attempt in range(retries + 1):
                r = requests.get(KALSHI_MARKETS_URL, params=params,
                                 timeout=timeout)
                if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
                    time.sleep(backoff * (attempt + 1) ** 2)
                    continue
                break
            r.raise_for_status()
            js = r.json() or {}
        markets += js.get("markets") or []
    return windows_from_markets(markets, unparsed)


def weekday_factor(daily: Dict[date, float], d: date, asof: date,
                   weeks: int = 4) -> float:
    vals = []
    for w in range(1, weeks + 1):
        ref = asof - timedelta(days=7 * w)
        wk = [ref - timedelta(days=i) for i in range(1, 8)]
        if not all(x in daily for x in wk):
            continue
        m = sum(daily[x] for x in wk) / 7.0
        if m > 0:
            vals += [daily[x] / m for x in wk if x.weekday() == d.weekday()]
    return statistics.mean(vals) if vals else 1.0


def cap_spikes(daily: Dict[date, float], cap: Optional[float] = None) -> Dict[date, float]:
    """`daily` with each day held to at most (1 + cap) x the median of the 7
    days before it (raw values; days without 7 before them as they are).
    cap <= 0 -> unchanged (OR_SPIKE_CAP when None)."""
    cap = OR_SPIKE_CAP if cap is None else cap
    if cap <= 0:
        return dict(daily)
    out: Dict[date, float] = {}
    for d in sorted(daily):
        prior = [daily[d - timedelta(days=i)] for i in range(1, 8)
                 if d - timedelta(days=i) in daily]
        out[d] = (min(daily[d], (1.0 + cap) * statistics.median(prior))
                  if len(prior) == 7 else daily[d])
    return out


def window_fair(daily: Dict[date, float], start: date, end: date,
                today: date) -> Optional[dict]:
    """N(mu, sigma) in T for the window's total as of `today` (days before
    today are completed). None without 14 completed days of history. The
    known days count at their actual totals; the run rate reads them through
    cap_spikes."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    last7 = [today - timedelta(days=i) for i in range(1, 8)]
    prev7 = [today - timedelta(days=i) for i in range(8, 15)]
    if not all(x in daily for x in last7 + prev7):
        return None
    known = [x for x in days if x < today and x in daily]
    remaining = [x for x in days if x not in known]
    run = cap_spikes({k: v for k, v in daily.items() if k < today})
    capped = sorted(x for x in last7 + prev7 if run[x] < daily[x])
    base = statistics.mean(run[x] for x in last7)
    g = statistics.mean(run[x] for x in last7) / max(
        1.0, statistics.mean(run[x] for x in prev7)) - 1.0
    g = max(-0.10, min(0.20, g)) * OR_TREND_WEIGHT
    mu = sum(daily[x] for x in known)
    for x in remaining:
        h = max(1, (x - today).days + 1)
        mu += base * weekday_factor(run, x, today) * (1.0 + g) ** ((h + 3) / 7.0)
    r = min(len(remaining), len(RMSE) - 1)
    if len(remaining) > len(RMSE) - 1:      # beyond the table: extend linearly
        r_extra = len(remaining) - (len(RMSE) - 1)
        rmse = RMSE[-1] + r_extra * (RMSE[-1] - RMSE[-2])
        bias = BIAS[-1] + r_extra * (BIAS[-1] - BIAS[-2])
    else:
        rmse, bias = RMSE[r], BIAS[r]
    mu += OR_BIAS_WEIGHT * bias * base
    sigma = OR_SIGMA_MULT * rmse * base
    return {"mu": round(mu / T, 4), "sigma": round(sigma / T, 4),
            "known": len(known), "days": len(days),
            "known_sum": round(sum(daily[x] for x in known) / T, 4),
            "base": round(base / T, 4), "complete": not remaining,
            "spike_capped": [x.isoformat() for x in capped]}


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_fair_file(path: str, daily: Optional[Dict[date, float]] = None,
                    windows: Optional[Dict[str, dict]] = None,
                    now: Optional[datetime] = None,
                    vintage_path: Optional[str] = None,
                    get_json: Optional[Callable[[str, dict], dict]] = None
                    ) -> Tuple[int, int]:
    """Fetch (unless given), build and atomically write the fair file.
    Returns (events with an entry, events without one); (0, 0) and no write
    when there is no API key. Raises on fetch errors (the caller logs and
    keeps the previous file, whose entries then age out of the bot's TTL).
    `get_json` is incentive_mm's signed Kalshi reader (fetch_windows)."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    if daily is None:
        key = api_key()
        if not key:
            return 0, 0
        daily = fetch_daily(key, today - timedelta(days=HISTORY_DAYS), today)
    prev = _read_json(path)
    unparsed: List[str] = []
    if windows is None:
        try:
            windows = fetch_windows(get_json=get_json, unparsed=unparsed)
        except Exception as e:
            # an event's window never changes: keep refreshing the totals on
            # the windows already known, and only new events wait for Kalshi
            windows = {ev: {"series": str(e2.get("series") or ev.split("-")[0]),
                            "start": e2["start"], "end": e2["end"]}
                       for ev, e2 in (prev.get("entries") or {}).items()
                       if isinstance(e2, dict) and e2.get("start") and e2.get("end")}
            if not windows:
                raise
            _log(f"! Kalshi window read failed ({type(e).__name__}); "
                 f"reusing {len(windows)} cached event windows")
    entries: Dict[str, dict] = {}
    missing: List[str] = []
    for ev, w in sorted(windows.items()):
        f = window_fair(daily, date.fromisoformat(w["start"]),
                        date.fromisoformat(w["end"]), today)
        if f is None:
            missing.append(ev)
            continue
        entries[ev] = dict(f, series=w["series"], start=w["start"],
                           end=w["end"], fetched_at=now.isoformat())
    # an open event whose window text did not parse has no read -- name it
    missing += [ev for ev in unparsed if ev not in entries and ev not in missing]
    last_day = max(daily).isoformat() if daily else ""
    if last_day and last_day != prev.get("last_day"):
        vp = vintage_path or OR_VINTAGE_FILE
        os.makedirs(os.path.dirname(vp) or ".", exist_ok=True)
        with open(vp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"fetched_at": now.isoformat(),
                                 "day": last_day,
                                 "tokens": daily[max(daily)]}) + "\n")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": now.isoformat(), "last_day": last_day,
                   "entries": entries, "missing": missing,
                   "daily_tail": {d.isoformat(): round(v / T, 4)
                                  for d, v in sorted(daily.items())[-21:]},
                   "model": {"trend_weight": OR_TREND_WEIGHT,
                             "bias_weight": OR_BIAS_WEIGHT,
                             "sigma_mult": OR_SIGMA_MULT, "spike_cap": OR_SPIKE_CAP}},
                  fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return len(entries), len(missing)


def p_above(strike_t: float, mu: float, sigma: float) -> float:
    """P(total > strike), all in T."""
    if sigma <= 0:
        return 1.0 if mu > strike_t else 0.0
    return 0.5 * math.erfc((strike_t - mu) / (sigma * math.sqrt(2.0)))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "openrouter_fair.json"))
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    if (ok, miss) == (0, 0) and not api_key():
        _log(f"no OpenRouter key (IMM_OR_API_KEY or {OR_KEY_FILE}); nothing written")
        return 1
    _log(f"{ok} events with a fair entry, {miss} without -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
