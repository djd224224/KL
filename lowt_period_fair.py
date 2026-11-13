#!/usr/bin/env python3
r"""Period low-temperature fair values for the IMM's KXLOWTPERIOD family
(Jack 2026-11-13: "daily new incentive program said KXLOWTPERIOD-26NOV15PVD,
KXLOWTPERIOD-26NOV15BOS are quoted but they arent. build a fair value and
quote", then "this family").

THE CONTRACT. KXLOWTPERIOD-26NOV15EWR-T40 = "Will Newark fall below 40F from
Nov 12 to 15, 2026?": YES when the lowest of The Weather Company's official
daily minimums at the station the rules name ("(KEWR)") over the period's
local calendar dates (both ends inclusive) is under the strike. One series,
one event per city (the event suffix), "less" strikes only so far. The
period is already running when its program starts, so -- unlike the daily
KXLOWT<CITY>, quoted only the day before -- every quotable hour is inside
the measurement window, and the fair has to carry what has been observed.

THE FAIR. The period low M = min(O, L_1 .. L_n):
  - O, OBSERVED SO FAR: the period's days up to the station's local today,
    in two readings -- the METAR minimum (IEM's daily summary and currents,
    what The Weather Company's history shows) and the NWS CLI low for the
    finished days (continuous ASOS data, which can sit 1-2F under the
    METARs: KPVD 11/12 read 44F on the METARs, 42F on the CLI). Whole
    degrees, rounded half up. A strike both readings are under is DECIDED;
    one only the CLI is under counts half.
  - L_d, EACH REMAINING LOCAL DAY: the NWS hourly forecast's minimum over
    that day's hours still to come, SHIFTED by the latest observation's miss
    (obs - the forecast for its hour, clamped to SHIFT_MAX_F) with the
    shift fading linearly to nothing over SHIFT_DECAY_H -- the grid lags a
    front: on 11/13 at 07:51 ET the hourly said NYC 45F, EWR 44F, BOS 43F
    while the stations read 38F, 39F and 37F. Each day's low is a Laplace
    around that minimum + lowt_fair.BIAS_F, scale = the city's lowt_fair
    scale x a lead factor (0.75 + 0.025/h of lead, in [0.75, 2.0]: 1.0 at
    the ~10h lead lowt_fair's calibration ran at, 1.25 = its B_MULT at
    ~20h), x lowt_fair.ONE_SOURCE_MULT (NWS alone), floored at B_FLOOR.
    Days independent.
  P(M < x) = 1 - P(O >= x) * prod_d P(L_d >= x).

Fails CLOSED (the event gets a skip reason, so the gate stands it aside):
no station in the rules, no period, no current observation within
OBS_MAX_AGE_MIN, no NWS hourly forecast reaching the period's last hour.

Each entry also lists, besides P(YES) per market:
  - "decided": strikes the observed low is already under (both readings);
  - "near": strikes the latest observation is within NEAR_F of deciding --
    the book moves on every five-minute reading there, faster than any
    refresh of this file.
incentive_mm.lowt_period_gate stands both kinds aside.

The IMM's refresher thread ("lowt-period-fair") calls write_fair_file every
IMM_LOWT_PERIOD_REFRESH_SECS; the bot hot-reloads the file by mtime:
  {"generated_at", "events": {event: {"date", "series", "station", "period",
   "fetched_at", "obs", "shift", "days", "mu", "b", "nws", "wu", "skip",
   "markets": {ticker: p}, "decided": [ticker], "near": [ticker]}},
   "stations": {icao: row}, "grid_urls": {icao: url}, "errors", "model"}

usage: python lowt_period_fair.py [--out FILE]   (writes and prints the fairs)
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

import pytz
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lowt_fair as LF  # noqa: E402  (Laplace, strike ranges, the city scales)
import rain_fair  # noqa: E402       (NWS station records and the hourly-URL lookup)

SERIES = tuple(s.strip() for s in os.environ.get(
    "LOWT_PERIOD_SERIES", "KXLOWTPERIOD").split(",") if s.strip())
IEM = "https://mesonet.agron.iastate.edu"
OBS_MAX_AGE_MIN = float(os.environ.get("LOWT_PERIOD_OBS_MAX_AGE_MIN", "90"))
SHIFT_DECAY_H = float(os.environ.get("LOWT_PERIOD_SHIFT_DECAY_H", "12"))
SHIFT_MAX_F = float(os.environ.get("LOWT_PERIOD_SHIFT_MAX_F", "10"))
SHIFT_WINDOW_H = 3.0
LEAD_B0, LEAD_B_SLOPE, LEAD_B_MIN, LEAD_B_MAX = 0.75, 0.025, 0.75, 2.0
NEAR_F = float(os.environ.get("LOWT_PERIOD_NEAR_F", "2"))
P_DECIDED = 0.999
UA = {"User-Agent": "KL lowt_period_fair"}
TIMEOUT = 20

_RULES_RE = re.compile(
    r"\((K[A-Z0-9]{3})\)\s+for\s+([A-Z][a-z]+)\.?\s+(\d{1,2})\s+to\s+"
    r"(?:([A-Z][a-z]+)\.?\s+)?(\d{1,2}),\s+(\d{4})")
_TZ_RE = re.compile(r"inclusive,\s+([A-Za-z_]+/[A-Za-z_]+)\s+local")
_ZONE_RE = re.compile(r"([A-Z]{2})[CZ]\d{3}")
_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
# event suffix -> lowt_fair's KXLOWT<CITY> scale key where they differ
_SCALE_ALIAS = {"PHL": "PHIL"}


def parse_rules(text: str) -> Optional[dict]:
    """{"icao", "start", "end", "tz"} from a market's rules_primary, else
    None. 'at Newark (KEWR) for Nov 12 to 15, 2026 (inclusive,
    America/New_York local calendar dates)'; the end may name its own month
    ('Nov 28 to Dec 2, 2026'), and a start month after the end's is the
    year before."""
    m = _RULES_RE.search(text or "")
    if not m:
        return None
    icao, mon1, d1, mon2, d2, yyyy = m.groups()
    m1 = _MONTHS.get(mon1[:3].upper())
    m2 = _MONTHS.get((mon2 or mon1)[:3].upper())
    if m1 is None or m2 is None:
        return None
    try:
        end = date(int(yyyy), m2, int(d2))
        start = date(int(yyyy) - (1 if m1 > m2 else 0), m1, int(d1))
    except ValueError:
        return None
    if start > end or (end - start).days > 62:
        return None
    tz = _TZ_RE.search(text or "")
    return {"icao": icao, "start": start, "end": end, "tz": tz.group(1) if tz else None}


def whole_f(v) -> Optional[int]:
    """A reading in whole degrees F, rounded half up (The Weather Company
    shows whole degrees); None for a missing one."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x):
        return None
    return int(math.floor(x + 0.5))


def lead_factor(lead_h: float) -> float:
    return min(LEAD_B_MAX, max(LEAD_B_MIN, LEAD_B0 + LEAD_B_SLOPE * max(0.0, lead_h)))


def city_scale(event: str) -> float:
    suffix = re.sub(r"^\d{2}[A-Z]{3}\d{2}", "", event.split("-")[1]) if "-" in event else ""
    key = "KXLOWT" + _SCALE_ALIAS.get(suffix, suffix)
    return LF.B_BY_SERIES.get(key, LF.B_POOLED)


def remaining_days(periods: List[dict], tzname: str, obs_temp: Optional[float],
                   obs_at: datetime, start: date, end: date, b_city: float) -> Tuple[List[dict], float, Optional[str]]:
    """([{"day", "f", "lead_h", "b", "mu"}], shift, skip) for the forecast
    hours after the observation through the period's last local day."""
    tz = pytz.timezone(tzname)
    hours: List[Tuple[datetime, float]] = []
    for p in periods or []:
        try:
            st = datetime.fromisoformat(p["startTime"])
            hours.append((st, float(p["temperature"])))
        except (KeyError, TypeError, ValueError):
            continue
    hours.sort()
    shift = 0.0
    if obs_temp is not None:
        # the miss is a LEVEL miss only beyond the range the forecast spans
        # from the reading's hour to SHIFT_WINDOW_H after it: a reading inside
        # that range is a front arriving early or late (BOS 11/13 07:54 ET:
        # 37F with the hourly at 43F to 08:00 and 36F from 09:00), and
        # shifting the post-front hours by it would count the front twice.
        # The hourly list rolls on the hour, so the reading's own hour may be
        # gone already: the window opens an hour before it.
        win = [t for st, t in hours
               if obs_at - timedelta(hours=1) <= st <= obs_at + timedelta(hours=SHIFT_WINDOW_H)]
        if win:
            lo, hi = min(win), max(win)
            miss = obs_temp - lo if obs_temp < lo else obs_temp - hi if obs_temp > hi else 0.0
            shift = max(-SHIFT_MAX_F, min(SHIFT_MAX_F, miss))
    last_needed = tz.localize(datetime(end.year, end.month, end.day, 23))
    if obs_at.astimezone(tz).date() <= end and \
            (not hours or hours[-1][0] < last_needed):
        return [], shift, (f"the NWS hourly forecast ends before the period's last hour "
                           f"({end.isoformat()} 23:00)")
    by_day: Dict[date, Tuple[float, float]] = {}
    for st, t in hours:
        if st <= obs_at:
            continue
        d = st.astimezone(tz).date()
        if d < start or d > end:
            continue
        lead = (st - obs_at).total_seconds() / 3600.0
        adj = t + shift * max(0.0, 1.0 - lead / SHIFT_DECAY_H)
        if d not in by_day or adj < by_day[d][0]:
            by_day[d] = (adj, lead)
    days = []
    for d in sorted(by_day):
        f, lead = by_day[d]
        b = max(LF.B_FLOOR, b_city * lead_factor(lead)) * LF.ONE_SOURCE_MULT
        days.append({"day": d.isoformat(), "f": round(f, 2), "lead_h": round(lead, 1),
                     "b": round(b, 3), "mu": round(f + LF.BIAS_F, 2)})
    return days, shift, None


def p_below(x: float, obs: dict, days: List[dict]) -> float:
    """P(period low < x): the observed part, then each remaining day."""
    hi, lo = obs.get("min_hi"), obs.get("min_lo")
    if hi is not None and hi < x:
        return 1.0
    p_obs_above = 0.5 if (lo is not None and lo < x) else 1.0
    stay = p_obs_above
    for d in days:
        stay *= 1.0 - LF.laplace_cdf(x, d["mu"], d["b"])
    return 1.0 - stay


def market_p(m: dict, obs: dict, days: List[dict]) -> Optional[float]:
    rng = LF.market_range(m)
    if rng is None:
        return None
    lo, hi = rng
    p = (1.0 if hi == math.inf else p_below(hi, obs, days)) - \
        (0.0 if lo == -math.inf else p_below(lo, obs, days))
    return min(P_DECIDED, max(0.0, p))


def decided(m: dict, obs: dict) -> bool:
    """A strike the observed low is already under on both readings: a 'less'
    YES, a 'greater' NO, a band whose top it is under NO."""
    rng = LF.market_range(m)
    hi_obs = obs.get("min_hi")
    if rng is None or hi_obs is None:
        return False
    lo, hi = rng
    return (m.get("strike_type") == "less" and hi_obs < hi) or \
        (lo != -math.inf and hi_obs < lo)


def near(m: dict, obs: dict) -> bool:
    """The latest reading within NEAR_F of deciding the strike (and not yet
    deciding it): the cold edge of the market's range is that close."""
    now_f = obs.get("now_f")
    rng = LF.market_range(m)
    if rng is None or now_f is None:
        return False
    edge = rng[1] if m.get("strike_type") == "less" else rng[0]
    if edge in (math.inf, -math.inf):
        return False
    return edge < now_f <= edge + NEAR_F


def http_json(url: str, retries: int = 2) -> dict:
    last: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:                       # noqa: BLE001 - caller logs
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url}: {last}")


def _iem(path: str, get: Callable) -> dict:
    return get(IEM + path) or {}


def iem_network(icao: str, get: Callable) -> str:
    """{state}_ASOS from the NWS record's county zone (KEWR -> NJC013 -> NJ)."""
    props = (get(f"https://api.weather.gov/stations/{icao}") or {}).get("properties") or {}
    zone = str(props.get("county") or props.get("forecast") or "").rstrip("/").rsplit("/", 1)[-1]
    m = _ZONE_RE.fullmatch(zone)
    if not m:
        raise RuntimeError(f"{icao}: no state in the NWS zone {zone!r}")
    return f"{m.group(1)}_ASOS"


def station_row(icao: str, rows: Dict[str, dict], get: Callable) -> dict:
    """The station's row (cli, tz, lat, lon, iem id, IEM network), cached in
    the file: rain_fair.STATIONS by CLI product, else the NWS record."""
    row = rows.get(icao)
    if row and row.get("net"):
        return row
    cli = "CLI" + icao[1:]
    base = next((dict(r) for r in rain_fair.STATIONS.values() if r["cli"] == cli), None)
    if base is None:
        base = rain_fair.resolve_station(cli)
    base.update(iem=icao[1:], icao=icao, net=iem_network(icao, get))
    rows[icao] = base
    return base


def observed(st: dict, start: date, end: date, now_utc: datetime, get: Callable) -> dict:
    """The period's low so far on both readings, and the latest observation.
    Raises RuntimeError when there is no current observation."""
    tz = pytz.timezone(st["tz"])
    cur = (_iem(f"/api/1/currents.json?station={st['iem']}&network={st['net']}", get)
           .get("data") or [{}])[0]
    try:
        at = datetime.fromisoformat(str(cur.get("utc_valid") or "").replace("Z", "+00:00"))
    except ValueError:
        at = None
    now_f = whole_f(cur.get("tmpf"))
    if at is None or now_f is None or \
            (now_utc - at).total_seconds() > OBS_MAX_AGE_MIN * 60:
        raise RuntimeError(f"no {st['iem']} observation within {OBS_MAX_AGE_MIN:g}m "
                           f"({cur.get('utc_valid')!r})")
    today = now_utc.astimezone(tz).date()
    last = min(end, today)
    metar: Dict[str, Optional[int]] = {}
    cli: Dict[str, Optional[int]] = {}
    if start <= last:
        months = sorted({(d.year, d.month) for d in
                         (start + timedelta(days=i) for i in range((last - start).days + 1))})
        for y, mo in months:
            for r in _iem(f"/api/1/daily.json?station={st['iem']}&network={st['net']}"
                          f"&year={y}&month={mo}", get).get("data") or []:
                ds = str(r.get("date") or "")
                if start.isoformat() <= ds <= last.isoformat():
                    metar[ds] = whole_f(r.get("min_tmpf"))
        for y in sorted({y for y, _ in months}):
            for r in _iem(f"/json/cli.py?station={st['icao']}&year={y}", get).get("results") or []:
                ds = str(r.get("valid") or "")
                if start.isoformat() <= ds <= last.isoformat() and ds < today.isoformat():
                    cli[ds] = whole_f(r.get("low"))
        if today <= end and today >= start:
            t_min = whole_f(cur.get("min_tmpf"))
            vals = [v for v in (metar.get(today.isoformat()), t_min, now_f) if v is not None]
            metar[today.isoformat()] = min(vals) if vals else None
        for i in range((last - start).days + 1):
            ds = (start + timedelta(days=i)).isoformat()
            if metar.get(ds) is None and cli.get(ds) is None:
                raise RuntimeError(f"no {st['iem']} reading for {ds}")
    his = [v for v in metar.values() if v is not None]
    los = his + [v for v in cli.values() if v is not None]
    return {"now_f": now_f, "now_at": at.isoformat(),
            "min_hi": min(his) if his else None, "min_lo": min(los) if los else None,
            "days": {ds: {"metar": metar.get(ds), "cli": cli.get(ds)}
                     for ds in sorted(set(metar) | set(cli))}}


def event_fair(event: str, info: dict, st: dict, periods: List[dict],
               markets: List[dict], obs: dict, now_utc: datetime) -> dict:
    """One event's entry (see the module docstring)."""
    out: dict = {"skip": None, "markets": {}, "decided": [], "near": [], "obs": obs,
                 "mu": None, "b": None, "nws": None, "wu": None, "shift": None, "days": []}
    at = datetime.fromisoformat(obs["now_at"])
    tzname = info.get("tz") or st["tz"]
    if at.astimezone(pytz.timezone(tzname)).date() > info["end"]:
        days, shift, why = [], 0.0, None                 # the period is over
    else:
        days, shift, why = remaining_days(periods, tzname, obs.get("now_f"), at,
                                          info["start"], info["end"], city_scale(event))
    out.update(days=days, shift=round(shift, 2))
    if why:
        out["skip"] = why
        return out
    if days:
        coldest = min(days, key=lambda d: d["mu"])
        out.update(mu=coldest["mu"], b=coldest["b"], nws=round(coldest["f"] - shift
                   * max(0.0, 1.0 - coldest["lead_h"] / SHIFT_DECAY_H), 2))
    for m in markets:
        p = market_p(m, obs, days)
        if p is None:
            continue
        out["markets"][m["ticker"]] = round(p, 4)
        if decided(m, obs):
            out["decided"].append(m["ticker"])
        elif near(m, obs):
            out["near"].append(m["ticker"])
    return out


def write_fair_file(path: str, get_json: Optional[Callable] = None,
                    now_utc: Optional[datetime] = None,
                    http_get: Callable = http_json) -> Tuple[int, int, int]:
    """Refresh every open period event. An event whose read fails this round
    keeps its old entry and fetched_at (the gate's TTL governs). Returns
    (events priced, events skipped, events failed)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    old: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f) or {}
    except (OSError, ValueError):
        pass
    stations: Dict[str, dict] = dict(old.get("stations") or {})
    grid_urls: Dict[str, str] = dict(old.get("grid_urls") or {})
    old_events: dict = dict(old.get("events") or {})
    events: dict = {}
    errors: List[str] = []
    priced = skipped = failed = 0
    by_event: Dict[str, List[dict]] = {}
    for series in SERIES:
        try:
            mj = LF._kalshi("/markets", {"series_ticker": series, "status": "open",
                                         "limit": 200}, get_json)
        except Exception as e:                       # noqa: BLE001
            errors.append(f"{series}: {str(e)[:160]}")
            for ev, e_old in old_events.items():
                if e_old.get("series") == series:
                    events.setdefault(ev, e_old)
            continue
        for m in mj.get("markets") or []:
            ev = m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0]
            by_event.setdefault(ev, []).append(dict(m, _series=series))
    periods_by_icao: Dict[str, List[dict]] = {}
    for ev, mkts in sorted(by_event.items()):
        series = mkts[0]["_series"]
        info = next((i for i in (parse_rules(str(m.get("rules_primary") or ""))
                                 for m in mkts) if i), None)
        base = {"series": series, "fetched_at": now_utc.isoformat()}
        if info is None:
            events[ev] = dict(base, skip="no station or period in the rules",
                              markets={}, decided=[], near=[])
            skipped += 1
            continue
        base.update(date=info["end"].isoformat(), station=info["icao"],
                    period=[info["start"].isoformat(), info["end"].isoformat()])
        try:
            st = station_row(info["icao"], stations, http_get)
            obs = observed(st, info["start"], info["end"], now_utc, http_get)
            icao = info["icao"]
            if icao not in periods_by_icao:
                url = grid_urls.get(icao) or rain_fair.resolve_hourly_url(icao, st)
                grid_urls[icao] = url
                try:
                    js = http_get(url)
                except RuntimeError:
                    url = rain_fair.resolve_hourly_url(icao, st)   # moved gridpoint
                    grid_urls[icao] = url
                    js = http_get(url)
                periods_by_icao[icao] = (js.get("properties") or {}).get("periods") or []
            entry = event_fair(ev, info, st, periods_by_icao[icao], mkts, obs, now_utc)
        except Exception as e:                       # noqa: BLE001
            failed += 1
            errors.append(f"{ev}: {str(e)[:160]}")
            if ev in old_events:
                events[ev] = old_events[ev]           # keep its last read
            continue
        entry.update(base)
        events[ev] = entry
        if entry["skip"]:
            skipped += 1
        else:
            priced += 1
        time.sleep(0.2)                              # be polite to the feeds
    out = {"generated_at": now_utc.isoformat(), "events": events,
           "stations": stations, "grid_urls": grid_urls, "errors": errors[:20],
           "model": {"bias_f": LF.BIAS_F, "shift_decay_h": SHIFT_DECAY_H,
                     "shift_max_f": SHIFT_MAX_F, "near_f": NEAR_F,
                     "lead_b": [LEAD_B0, LEAD_B_SLOPE, LEAD_B_MIN, LEAD_B_MAX]}}
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return priced, skipped, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="lowt_period_fair.json")
    args = ap.parse_args()
    ok, skip, fail = write_fair_file(args.out)
    with open(args.out, encoding="utf-8") as f:
        js = json.load(f)
    print(f"{ok} events priced, {skip} skipped, {fail} failed")
    for ev, e in sorted((js.get("events") or {}).items()):
        if e.get("skip"):
            print(f"  {ev:28s} SKIP {e['skip']}")
            continue
        o = e.get("obs") or {}
        print(f"  {ev:28s} now {o.get('now_f')}F low so far {o.get('min_hi')}/{o.get('min_lo')}F "
              f"shift {e.get('shift')}F; days "
              + ", ".join(f"{d['day'][5:]} {d['mu']}+-{d['b']}" for d in e.get("days") or []))
        print("    " + " ".join(
            f"{t.rsplit('-', 1)[1]}={p * 100:.0f}"
            + ("D" if t in e.get("decided", []) else "N" if t in e.get("near", []) else "")
            for t, p in sorted(e["markets"].items())))
    for e in js.get("errors") or []:
        print("  !", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
