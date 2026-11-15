#!/usr/bin/env python3
"""Snow-day count fair values for incentive_mm's KXSNOWDAYS gate.

Jack 2026-11-14: "KXSNOWDAYS-01DEC26-01APR27, KXRAINTHANKSGIVING-26NOV26 (and
rain/snow markets generally) should have fair value calculations and
auto-quoted when they come online. these come online later today".

THE CONTRACT (SNOWDAYSSCALAR). KXSNOWDAYS-01DEC26-01APR27-<CITY>, one market
per city, is a SCALAR: "Each Yes contract pays according to a Directional
linear payout from $0.00 at 0 qualifying days to $1.00 at 4 days, capped at
$0.00-$1.00 and rounded down once to $0.01 based on the number of days
between December 1, 2026 and April 1, 2027 with at least 0.1 inches of
official daily snowfall in Atlanta (Hartsfield-Jackson Atlanta International
Airport, KATL)". The days are local STANDARD time ("Days run midnight-midnight
in EST (UTC-05:00), fixed standard time; dates are inclusive"), a trace is
zero, NO pays $1 minus YES. The range differs by city (custom_strike's
scalar_floor / scalar_cap: Atlanta 0-4, Boston 11-31); prices tick in tenths
of a cent (deci_cent), which the bot's whole cents fit.

THE FAIR, before the season: the station's CLIMATOLOGY. ACIS daily snowfall
at the station the rules name (its ICAO; the threaded city record "<code>thr"
when the airport's own is short, as snow_monthly_fair does for Denver),
counted over the same window in each of the last HIST_SEASONS seasons (a
season with more than MAX_MISSING_DAYS missing days is dropped, the rest of
its missing days read as no snow). Fewer than MIN_SEASONS complete -> no fair
(the gate stands it aside). Each (station, window, threshold) history is read
once and kept in the file's "hist" for HIST_TTL_DAYS.

THE FAIR = the mean payout over the last FAIR_SEASONS (10) of those seasons
(Jack 2026-11-15: "Yes, last 10"). At the 11/15 program start the 40-season
mean sat a median 11c over the other maker's book mid (Boston +20, New York
+18): snow days have thinned, and the last 10 seasons matched the book
(median +1.6c, mean gap 6.7c). The window is calendar seasons: a dropped one
is not replaced by an older one, and fewer than FAIR_MIN_SEASONS complete of
the last FAIR_SEASONS -> no fair. Each market row keeps the full-history
mean as "p_all" / "seasons_all" for reference.

IN SEASON (from the window's first local day) there is no fair YET: a
snowstorm moves a 0-4 day city 25c in a day, and the observed layer (the CLI's
daily snowfall to date, a snowing guard) is not built. Such a market goes to
"missing" ("the season started") and the gate stands it aside; the bot's
cutoff is 00:00 ET of the first day anyway (parse_event_date's snow-day rule).

AUTO-ENROLL (Jack 2026-10-31's "make sure new markets with 'RAIN', 'TEMP', etc
are always considered for auto-quoting"). Every DISCOVER_SECS Kalshi's weather
category is listed and each SNOW-named series no other snow family claims
(KX<CITY>SNOWM monthlies, KX<RESORT>SNOW resorts, Big Sky) is read on its open
markets: one whose every market parses as this contract (parse_market) joins
("series" in the file, which enrolls it in the bot); one that does not waits
RETRY_SECS ("rejected", with why).

Writes SNOW_DAYS_FILE for incentive_mm in the weather loaders' shape: markets
{ticker: {p, event, fetched_at, ...}}, missing {ticker: why}.

Standalone:  python snow_days_fair.py [--out path]
"""

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
SNOW_DAYS_FILE = os.path.join(STATUS_DIR, "snow_days_fair.json")
SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_SNOW_DAYS_SERIES", "KXSNOWDAYS").split(",") if s.strip())
AUTO_ENABLE = os.environ.get("IMM_SNOW_DAYS_AUTO", "1") == "1"
KALSHI_API = os.environ.get("IMM_SNOW_DAYS_KALSHI_URL",
                            "https://api.elections.kalshi.com/trade-api/v2")
ACIS_URL = "https://data.rcc-acis.org/StnData"
SERIES_CATEGORY = "Climate and Weather"
HIST_SEASONS = int(os.environ.get("IMM_SNOW_DAYS_SEASONS", "40"))
MIN_SEASONS = 25
FAIR_SEASONS = int(os.environ.get("IMM_SNOW_DAYS_FAIR_SEASONS", "10"))
FAIR_MIN_SEASONS = int(os.environ.get("IMM_SNOW_DAYS_FAIR_MIN", "8"))
MAX_MISSING_DAYS = 3
HIST_TTL_DAYS = 7.0
HIST_RETRY_SECS = 6 * 3600
MAX_HIST_READS = 40          # new ACIS reads a pass
EVENTS_TTL_SECS = 3600
DISCOVER_SECS = 3600
RETRY_SECS = 6 * 3600
HTTP_TIMEOUT = 30
USER_AGENT = "KL-IMM-snow-days-fair/1.0"
# the other snow families' names (theirs, not this one's)
_OTHER_SNOW_RE = re.compile(r"KX[A-Z]{2,6}SNOWM|KX[A-Z]{2,20}SNOW|KXTSNOWFALL[A-Z]*")

_PAY_RE = re.compile(r"from \$(\d+(?:\.\d+)?) at (\d+(?:\.\d+)?) qualifying days? to "
                     r"\$(\d+(?:\.\d+)?) at (\d+(?:\.\d+)?) days?")
_PERIOD_RE = re.compile(
    r"number of days between ([A-Z][a-z]+ \d{1,2}, \d{4}) and ([A-Z][a-z]+ \d{1,2}, \d{4}) "
    r"with at least (\d+(?:\.\d+)?) inch(?:es)? of official daily snowfall in ([^(]+?) "
    r"\(([^()]*)\)")
_ICAO_RE = re.compile(r"\b(K[A-Z]{3})\b")
_TZ_RE = re.compile(r"midnight\W+midnight in [A-Z]{2,4} \(UTC([+-])(\d{2}):(\d{2})\)")


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z [snow-days] {msg}",
          flush=True)


def _day(s: str) -> Optional[date]:
    try:
        return datetime.strptime(s, "%B %d, %Y").date()
    except ValueError:
        return None


def parse_market(m: dict) -> Tuple[Optional[dict], str]:
    """(spec, '') for a snow-day scalar market, else (None, why). spec:
    {floor, cap, y0, y1, start, end (ISO), thr (inches), station (ICAO),
    city, utc_off (hours, local standard time = UTC + utc_off)}."""
    t = str(m.get("ticker") or "")
    rules = str(m.get("rules_primary") or "")
    sec = str(m.get("rules_secondary") or "")
    pay, per = _PAY_RE.search(rules), _PERIOD_RE.search(rules)
    if not pay or not per:
        return None, f"{t}: not a 'linear payout ... days with at least ... snowfall' scalar"
    y0, n0, y1, n1 = (float(x) for x in pay.groups())
    cs = m.get("custom_strike") or {}
    try:
        floor, cap = float(cs.get("scalar_floor")), float(cs.get("scalar_cap"))
    except (TypeError, ValueError):
        floor, cap = n0, n1
    if (floor, cap) != (n0, n1) or not cap > floor or not 0 <= min(y0, y1) <= max(y0, y1) <= 1:
        return None, f"{t}: payout range {n0:g}-{n1:g} vs custom strike {floor:g}-{cap:g}"
    start, end = _day(per.group(1)), _day(per.group(2))
    if start is None or end is None or not start < end or (end - start).days > 200:
        return None, f"{t}: window {per.group(1)} .. {per.group(2)}"
    st = _ICAO_RE.findall(per.group(5))
    if not st:
        return None, f"{t}: no K-station in '({per.group(5)})'"
    tz = _TZ_RE.search(sec)
    off = None
    if tz:
        off = (1 if tz.group(1) == "+" else -1) * (int(tz.group(2)) + int(tz.group(3)) / 60.0)
    if off is None:
        return None, f"{t}: no 'Days run midnight-midnight in XST (UTC-hh:mm)' in its rules"
    return {"floor": floor, "cap": cap, "y0": y0, "y1": y1, "start": start.isoformat(),
            "end": end.isoformat(), "thr": float(per.group(3)), "station": st[-1],
            "city": per.group(4).strip(), "utc_off": off}, ""


def payout(spec: dict, n: float) -> float:
    """The YES payout at `n` qualifying days: linear between the range's ends,
    capped, rounded DOWN to the cent once (the rules)."""
    x = min(max((n - spec["floor"]) / (spec["cap"] - spec["floor"]), 0.0), 1.0)
    y = spec["y0"] + (spec["y1"] - spec["y0"]) * x
    return math.floor(y * 100.0 + 1e-9) / 100.0


def parse_snow(v) -> Optional[float]:
    """An ACIS daily snowfall -> inches: 'T' 0.0, 'M' / 'S' / an accumulated
    'A' total / anything unreadable None (missing)."""
    s = str(v if v is not None else "").strip()
    if s in ("T", "t"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def season_counts(daily: Dict[str, Optional[float]], start: date, end: date, thr: float,
                  seasons: int = HIST_SEASONS) -> List[Tuple[int, int]]:
    """[(season's first year, qualifying days)] over the same window in each
    of the `seasons` seasons before the target, newest first; a season with
    more than MAX_MISSING_DAYS missing (or out of the record) is dropped."""
    out = []

    def shift(d: date, back: int) -> date:
        try:
            return d.replace(year=d.year - back)
        except ValueError:                       # Feb 29 in a common year
            return date(d.year - back, 2, 28)
    for back in range(1, seasons + 1):
        s0, e0 = shift(start, back), shift(end, back)
        miss = n = 0
        for k in range((e0 - s0).days + 1):
            v = daily.get((s0 + timedelta(days=k)).isoformat())
            if v is None:
                miss += 1
            elif v >= thr - 1e-9:
                n += 1
        if miss <= MAX_MISSING_DAYS:
            out.append((s0.year, n))
    return out


def fetch_history(sid: str, start: date, end: date) -> Dict[str, Optional[float]]:
    """ACIS daily snowfall at `sid` over the HIST_SEASONS seasons' windows."""
    s = date(start.year - HIST_SEASONS - 1, 1, 1)
    e = min(end.replace(year=end.year - 1) + timedelta(days=1),
            datetime.now(timezone.utc).date() - timedelta(days=1))
    r = requests.post(ACIS_URL, json={"sid": sid, "sdate": s.isoformat(),
                                      "edate": e.isoformat(), "elems": [{"name": "snow"}]},
                      timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    js = r.json()
    if js.get("error"):
        raise RuntimeError(f"ACIS {sid}: {str(js['error'])[:120]}")
    return {str(d): parse_snow(v) for d, v in js.get("data") or []}


# a station whose own snowfall record is short and has no threaded record of
# its own name -> the nearest long one: Houston Hobby (KHOU) reports snowfall
# on ~20 of the last 40 winters, and ACIS has no HOUthr; "Houston Area"
# (IAHthr, Bush Intercontinental, ~20 miles north) is complete
PROXY_SIDS = {"KHOU": "IAHthr"}


def station_seasons(icao: str, start: date, end: date, thr: float,
                    fetch: Callable = fetch_history) -> Tuple[str, List[Tuple[int, int]]]:
    """(the ACIS id used, season counts): the airport's own record, else the
    threaded city record (<code>thr) when that has more complete seasons."""
    best: Tuple[str, List[Tuple[int, int]]] = (icao, [])
    errs = []
    for sid in (icao, PROXY_SIDS.get(icao) or icao[1:] + "thr"):
        try:
            got = season_counts(fetch(sid, start, end), start, end, thr)
        except Exception as ex:
            errs.append(f"{sid}: {type(ex).__name__}: {str(ex)[:80]}")
            continue
        if len(got) > len(best[1]):
            best = (sid, got)
        if len(got) >= HIST_SEASONS - MAX_MISSING_DAYS:
            break
    if len(best[1]) < MIN_SEASONS:
        raise RuntimeError(f"{icao}: {len(best[1])} complete seasons (need {MIN_SEASONS})"
                           + (f"; {'; '.join(errs)}" if errs else ""))
    return best


def fair_from_counts(spec: dict, counts: List[Tuple[int, int]]) -> dict:
    """{'p', 'seasons', 'n_med', 'n_p10', 'n_p90', 'mean_n'} from the season
    counts: the mean payout."""
    ns = sorted(n for _y, n in counts)
    pays = [payout(spec, n) for _y, n in counts]

    def q(f):
        return ns[min(len(ns) - 1, max(0, int(round(f * (len(ns) - 1)))))]
    return {"p": round(min(max(sum(pays) / len(pays), 0.0), 1.0), 4), "seasons": len(ns),
            "n_med": q(0.5), "n_p10": q(0.1), "n_p90": q(0.9),
            "mean_n": round(sum(ns) / len(ns), 2)}


def recent_counts(counts: List[Tuple[int, int]], start: date,
                  seasons: int = FAIR_SEASONS) -> List[Tuple[int, int]]:
    """The counts of the last `seasons` calendar seasons before the window
    starting `start` (a dropped season is not replaced by an older one)."""
    return [(y, n) for y, n in counts if start.year - seasons <= y < start.year]


def _get(get_json: Optional[Callable], path: str, params: dict) -> dict:
    if get_json is not None:
        try:
            js = get_json(path, dict(params))
            if isinstance(js, dict):
                return js
        except Exception:
            pass
    for attempt in range(3):
        r = requests.get(KALSHI_API + path, params=params, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 2:
            time.sleep(3 * (attempt + 1) ** 2)
            continue
        r.raise_for_status()
        return r.json() or {}
    return {}


def _open_markets(get_json: Optional[Callable], series: str) -> List[dict]:
    out, cursor = [], None
    for _ in range(10):
        p = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        js = _get(get_json, "/markets", p)
        out += js.get("markets") or []
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


def classify(markets: List[dict]) -> Tuple[Optional[dict], str]:
    """({'sample'}, '') when every open market is the snow-day scalar, else
    (None, why)."""
    if not markets:
        return None, "no open market"
    for m in markets:
        spec, why = parse_market(m)
        if spec is None:
            return None, why
    return {"sample": str(markets[0].get("ticker") or "")}, ""


def discover(old: dict, now_ts: float, get_json: Optional[Callable] = None
             ) -> Tuple[Dict[str, dict], Dict[str, dict], float]:
    """(joined {series: {sample, at}}, rejected {series: {at, why}}, listed
    at): the file's until DISCOVER_SECS has passed, then the weather category
    re-listed and each unclaimed SNOW series re-read once its verdict is
    RETRY_SECS old (AUTO-ENROLL)."""
    found = dict(old.get("series") or {})
    rejected = dict(old.get("rejected") or {})
    at = float(old.get("discovered_at") or 0.0)
    if not AUTO_ENABLE:
        return {}, rejected, at
    if now_ts - at < DISCOVER_SECS:
        return found, rejected, at
    js = _get(get_json, "/series", {"category": SERIES_CATEGORY})
    names = sorted({str(s.get("ticker") or "") for s in js.get("series") or []})
    out: Dict[str, dict] = {}
    for s in names:
        if "SNOW" not in s or s in SERIES or _OTHER_SNOW_RE.fullmatch(s):
            continue
        last = found.get(s) or rejected.get(s) or {}
        if last and now_ts - float(last.get("at") or 0.0) < RETRY_SECS:
            if s in found:
                out[s] = found[s]
            continue
        try:
            got, why = classify(_open_markets(get_json, s))
        except Exception:
            if s in found:
                out[s] = found[s]
            continue
        if got is None:
            rejected[s] = {"at": now_ts, "why": why}
            out.pop(s, None)
            continue
        rejected.pop(s, None)
        out[s] = dict(got, at=now_ts)
        if s not in found:
            _log(f"series {s} joins the family: e.g. {got['sample']}")
    return out, rejected, now_ts


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def season_started(spec: dict, now: datetime) -> bool:
    """Has the window's first local (standard-time) day begun?"""
    s = date.fromisoformat(spec["start"])
    first = datetime(s.year, s.month, s.day, tzinfo=timezone.utc) - timedelta(hours=spec["utc_off"])
    return now >= first


def write_fair_file(path: str = SNOW_DAYS_FILE, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    markets_in: Optional[Dict[str, List[dict]]] = None,
                    fetch: Callable = fetch_history) -> Tuple[int, int]:
    """Build and atomically write the snow-day fair file; returns (markets
    with a fair, open markets without one). Kalshi's open markets are re-read
    every EVENTS_TTL_SECS (`markets_in`: {series: [market]} for tests); a
    failed read reuses the file's."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    joined, rejected, disc_at = dict(old.get("series") or {}), dict(old.get("rejected") or {}), \
        float(old.get("discovered_at") or 0.0)
    specs: Dict[str, dict] = {k: v for k, v in (old.get("specs") or {}).items()
                              if isinstance(v, dict)}
    specs_at = float(old.get("specs_at") or 0.0)
    missing: Dict[str, str] = {}
    if markets_in is None:
        try:
            joined, rejected, disc_at = discover(old, ts, get_json)
        except Exception as ex:
            _log(f"weather category read failed ({type(ex).__name__}: {str(ex)[:120]}); "
                 f"the file's series stand")
        if ts - specs_at >= EVENTS_TTL_SECS or not specs or disc_at == ts:
            try:
                markets_in = {s: _open_markets(get_json, s)
                              for s in sorted(set(SERIES) | set(joined))}
            except Exception as ex:
                _log(f"markets read failed ({type(ex).__name__}: {str(ex)[:120]}); "
                     f"the file's markets stand")
    if markets_in is not None:
        specs = {}
        for s, ms in markets_in.items():
            for m in ms:
                t = str(m.get("ticker") or "")
                spec, why = parse_market(m)
                if spec is None:
                    missing[t] = why
                    continue
                specs[t] = dict(spec, event=str(m.get("event_ticker") or ""), series=s)
        specs_at = ts
    hist: Dict[str, dict] = {k: v for k, v in (old.get("hist") or {}).items()
                             if isinstance(v, dict)
                             and ts - float(v.get("at") or 0) < HIST_TTL_DAYS * 86400}
    stamp = now.isoformat()
    out: Dict[str, dict] = {}
    reads = 0
    for t, spec in sorted(specs.items()):
        if season_started(spec, now):
            missing[t] = "the season started (no in-season layer: stands aside)"
            continue
        key = f"{spec['station']}|{spec['start']}|{spec['end']}|{spec['thr']:g}"
        h = hist.get(key) or {}
        if h.get("counts") is None and not (
                h.get("why") and ts - float(h.get("at") or 0) < HIST_RETRY_SECS) \
                and reads < MAX_HIST_READS:
            reads += 1
            try:
                sid, counts = station_seasons(spec["station"], date.fromisoformat(spec["start"]),
                                              date.fromisoformat(spec["end"]), spec["thr"],
                                              fetch=fetch)
                h = hist[key] = {"sid": sid, "counts": counts, "at": ts}
                ns = sorted(n for _y, n in counts)
                _log(f"history {key}: {sid}, {len(counts)} seasons, median "
                     f"{ns[len(ns) // 2]} days")
            except Exception as ex:
                h = hist[key] = {"at": ts, "why": f"{type(ex).__name__}: {str(ex)[:160]}"}
                _log(f"! history {key} failed: {h['why']}")
        if h.get("counts") is None:
            missing[t] = "no history" + (f" ({h['why']})" if h.get("why") else "")
            continue
        allc = [tuple(c) for c in h["counts"]]
        rc = recent_counts(allc, date.fromisoformat(spec["start"]))
        if len(rc) < FAIR_MIN_SEASONS:
            missing[t] = (f"{len(rc)} of the last {FAIR_SEASONS} seasons complete "
                          f"(need {FAIR_MIN_SEASONS})")
            continue
        f = fair_from_counts(spec, rc)
        out[t] = dict(f, event=spec["event"], fetched_at=stamp, station=spec["station"],
                      sid=h.get("sid"), floor=spec["floor"], cap=spec["cap"],
                      window=[spec["start"], spec["end"]], thr=spec["thr"],
                      p_all=fair_from_counts(spec, allc)["p"], seasons_all=len(allc))
    payload = {"generated_at": stamp, "markets": out, "missing": missing,
               "series": joined, "rejected": rejected, "discovered_at": disc_at,
               "specs": specs, "specs_at": specs_at, "hist": hist,
               "model": {"hist_seasons": HIST_SEASONS, "min_seasons": MIN_SEASONS,
                         "fair_seasons": FAIR_SEASONS, "fair_min_seasons": FAIR_MIN_SEASONS,
                         "max_missing_days": MAX_MISSING_DAYS}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fo:
        json.dump(payload, fo, indent=1)
    os.replace(tmp, path)
    return len(out), len(missing)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=SNOW_DAYS_FILE)
    a = ap.parse_args(argv)
    ok, miss = write_fair_file(a.out)
    print(f"{ok} markets priced, {miss} without a fair -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
