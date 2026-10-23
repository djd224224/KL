#!/usr/bin/env python3
r"""Low-temperature fair values for the IMM's KXLOWT family (Jack 2026-10-23:
"Build low temp fair value as part of IMM, only quote the next day never the
same day (I should have the bones already as part of the low temp bot)").

THE BONES are low_temp_trading.py's (paused since 8/31). A city's daily low
comes from two forecasts, averaged:
  - the NWS hourly forecast's minimum over the station's LOCAL calendar day;
  - The Weather Company's calendarDayTemperatureMin for that day (Kalshi
    settles on The Weather Company's reading at the CLI<code> station named
    in the rules) -- only when WU_API_KEY is set in the environment; this
    module carries no key, and without one the NWS forecast prices alone.
Its two forecast-shape filters stand the city-day aside:
  - a late-evening cold drop: the 18-23h minimum within 4.5F of the 00-12h
    one, so a front can set the low at 23:59;
  - a midnight print: the 00:00 temperature within 1.5F of the day's
    minimum, so the low prints at midnight while evening quotes rest.

WHAT CHANGED, from the low bot's own history (KXLOW_market_snapshot x
KXHIGH_cli_readings, Jul 22 - Aug 31, 390 next-day city-days, 1,134 runs):
the error of the two-source mean is fat-tailed (median |err| 1.0F, p90
3.0F, rmse 2.09F, mean +0.3F), so the day's low is a LAPLACE around the
mean + BIAS_F with a per-city scale. The scale is the city's mean |err|
shrunk to the pooled 1.43F (Houston 0.90F .. Phoenix 2.30F), widened by
B_MULT for the longer leads the IMM quotes at (from 10 AM ET the day before;
the sample's runs were 20:17 / 23:17 ET) and the cold months the summer
never saw. Even so the market's own mid out-predicted it (Brier 0.103 vs
0.112 on 5,562 two-sided books; the mid was closer at every disagreement
threshold), so incentive_mm uses this fair as a WIDE sanity gate and never
to price (lowt_gate).

Only series whose rules name an NWS CLI product (US stations) are modelled;
the ICAO-coded international series have no NWS forecast and land in
"unmapped" (their markets fail closed in the gate).

The IMM's refresher thread ("lowt-fair") calls write_fair_file every
IMM_LOWT_FAIR_REFRESH_SECS; the bot hot-reloads the file by mtime:
  {"generated_at", "events": {event: {"date", "series", "cli", "fetched_at",
   "nws", "wu", "mu", "b", "skip", "shape", "markets": {ticker: p}}},
   "stations": {cli: row}, "grid_urls": {cli: url}, "series": {series: cli},
   "unmapped": {series: why}, "discovered_at", "errors"}

usage: python lowt_fair.py [--out FILE]      (writes and prints the fairs)
"""
import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import pytz
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rain_fair  # noqa: E402  (NWS station records and the hourly-URL lookup)

SERIES_PREFIX = "KXLOWT"
SERIES_CATEGORY = "Climate and Weather"
KALSHI_API = os.environ.get("LOWT_FAIR_KALSHI_API",
                            "https://api.elections.kalshi.com/trade-api/v2")
UA = {"User-Agent": "KL lowt_fair (jackdu224@gmail.com)"}
TIMEOUT = 20
DISCOVER_SECS = float(os.environ.get("LOWT_FAIR_DISCOVER_SECS", str(6 * 3600)))
# The Weather Company's 5-day daily forecast, read only when WU_API_KEY is
# set in the environment (no key is carried here); without it the fair is
# the NWS forecast alone, widened by ONE_SOURCE_MULT
WU_URL = "https://api.weather.com/v3/wx/forecast/daily/5day"
WU_API_KEY = os.environ.get("WU_API_KEY", "").strip()

# the calibration (see the module docstring)
BIAS_F = float(os.environ.get("LOWT_FAIR_BIAS_F", "0.3"))
B_POOLED = 1.43
B_BY_SERIES = {"KXLOWTATL": 1.27, "KXLOWTBOS": 1.20, "KXLOWTDAL": 1.03,
               "KXLOWTDC": 1.45, "KXLOWTHOU": 0.90, "KXLOWTLV": 1.80,
               "KXLOWTMIN": 1.33, "KXLOWTNOLA": 1.35, "KXLOWTOKC": 1.88,
               "KXLOWTPHX": 2.30, "KXLOWTSATX": 1.24, "KXLOWTSEA": 1.23,
               "KXLOWTSFO": 1.33}
B_MULT = float(os.environ.get("LOWT_FAIR_B_MULT", "1.25"))
B_FLOOR = 1.0
# one forecast instead of two: WU alone missed by 2.25F rmse, NWS by 2.32F,
# their mean by 2.09F
ONE_SOURCE_MULT = 1.1
# the low bot's forecast-shape filters
LATE_DROP_MIN_DELTA_F = 4.5
MIDNIGHT_PRINT_MIN_DELTA_F = 1.5
_CLI_RE = re.compile(r"\bCLI([A-Z0-9]{3})\b")


def _get_json(url: str, params: Optional[dict] = None, retries: int = 3) -> dict:
    last: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            if r.status_code == 429:                 # Kalshi's public limit: back off
                last = RuntimeError("429 Too Many Requests")
                time.sleep(3.0 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:                       # noqa: BLE001 - caller logs
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url}: {last}")


def _kalshi(path: str, params: dict, get_json: Optional[Callable] = None) -> dict:
    """One Kalshi read: the bot's signed reader when it handed one over, else
    (or on its failure) the public endpoint."""
    if get_json is not None:
        try:
            js = get_json(path, dict(params))
            if isinstance(js, dict):
                return js
        except Exception:                            # noqa: BLE001 - public fallback
            pass
    return _get_json(KALSHI_API + path, params) or {}


def event_date(event_ticker: str) -> Optional[date]:
    """KXLOWTEWR-26OCT24 -> 2026-10-24 (the station's local calendar day)."""
    try:
        return datetime.strptime(event_ticker.split("-")[1], "%y%b%d").date()
    except (IndexError, ValueError):
        return None


def market_range(m: dict) -> Optional[Tuple[float, float]]:
    """The continuous range of integer lows a market pays on (the low bot's
    parse): between floor..cap -> (floor-0.5, cap+0.5); less than cap ->
    (-inf, cap-0.5); greater than floor -> (floor+0.5, +inf)."""
    st, fl, cp = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
    try:
        if st == "between" and fl is not None and cp is not None:
            return float(fl) - 0.5, float(cp) + 0.5
        if st == "less" and cp is not None:
            return -math.inf, float(cp) - 0.5
        if st == "greater" and fl is not None:
            return float(fl) + 0.5, math.inf
    except (TypeError, ValueError):
        return None
    return None


def laplace_cdf(x: float, mu: float, b: float) -> float:
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    z = (x - mu) / b
    return 0.5 * math.exp(z) if z < 0 else 1.0 - 0.5 * math.exp(-z)


def range_probability(lo: float, hi: float, mu: float, b: float) -> float:
    return max(0.0, laplace_cdf(hi, mu, b) - laplace_cdf(lo, mu, b))


def nws_profile(periods: List[dict], tzname: str, day: date) -> dict:
    """The low bot's hourly profile of `day` (station-LOCAL calendar day):
    day_min, min_hour, t00, morning_min (00-12h), late_min (18-23h), n_hours."""
    tz = pytz.timezone(tzname)
    temps: Dict[int, float] = {}
    for p in periods or []:
        try:
            st = datetime.fromisoformat(p["startTime"]).astimezone(tz)
            t = float(p["temperature"])
        except (KeyError, TypeError, ValueError):
            continue
        if st.date() == day:
            temps[st.hour] = t
    out = {"day_min": None, "min_hour": None, "t00": None, "morning_min": None,
           "late_min": None, "n_hours": len(temps)}
    if temps:
        h = min(temps, key=lambda k: (temps[k], k))
        morning = [t for k, t in temps.items() if k <= 12]
        late = [t for k, t in temps.items() if k >= 18]
        out.update(day_min=temps[h], min_hour=h, t00=temps.get(0),
                   morning_min=min(morning) if morning else None,
                   late_min=min(late) if late else None)
    return out


def shape_skip(prof: dict) -> Optional[str]:
    """The low bot's two forecast-shape filters, or None when the low is
    safely a pre-dawn one."""
    mm, lm, t00, dm = (prof.get("morning_min"), prof.get("late_min"),
                       prof.get("t00"), prof.get("day_min"))
    if mm is not None and lm is not None and lm - mm < LATE_DROP_MIN_DELTA_F:
        return (f"late-day drop: the 18-23h minimum {lm:g}F is within "
                f"{LATE_DROP_MIN_DELTA_F:g}F of the morning's {mm:g}F")
    if t00 is not None and dm is not None and t00 - dm < MIDNIGHT_PRINT_MIN_DELTA_F:
        return (f"midnight print: {t00:g}F at 00:00 is within "
                f"{MIDNIGHT_PRINT_MIN_DELTA_F:g}F of the day's minimum {dm:g}F")
    return None


def wu_day_value(js: dict, day: date, field: str) -> Optional[float]:
    """The Weather Company's calendar-day `field` for `day`, matched by
    validTimeLocal rather than by index."""
    vals, valid = js.get(field), js.get("validTimeLocal")
    if not isinstance(vals, list) or not isinstance(valid, list):
        return None
    want = day.isoformat()
    for i, vt in enumerate(valid):
        if isinstance(vt, str) and vt.startswith(want):
            v = vals[i] if i < len(vals) else None
            return float(v) if v is not None else None
    return None


def wu_day_min(js: dict, day: date) -> Optional[float]:
    """calendarDayTemperatureMin for `day` (midnight-to-midnight local, the
    settlement day; NOT temperatureMin, the night period that crosses
    midnight)."""
    return wu_day_value(js, day, "calendarDayTemperatureMin")


def event_fair(series: str, nws: Optional[float], wu: Optional[float],
               prof: dict, markets: List[dict]) -> dict:
    """One event's entry: mu, b, the skip reason (None when quotable) and
    P(YES) per market. Fails closed: with no NWS hourly forecast the shape
    filters cannot run, so the day is skipped; WU alone never prices."""
    out: dict = {"nws": nws, "wu": wu, "shape": prof, "skip": None,
                 "mu": None, "b": None, "markets": {}}
    if nws is None:
        out["skip"] = "no NWS hourly forecast for the day (shape filters cannot run)"
        return out
    if (prof.get("n_hours") or 0) < 20:
        out["skip"] = f"the NWS hourly forecast covers only {prof.get('n_hours')}h of the day"
        return out
    why = shape_skip(prof)
    if why:
        out["skip"] = why
        return out
    srcs = [v for v in (nws, wu) if v is not None]
    mu = sum(srcs) / len(srcs) + BIAS_F
    b = max(B_FLOOR, B_BY_SERIES.get(series, B_POOLED)) * B_MULT
    if len(srcs) == 1:
        b *= ONE_SOURCE_MULT
    out["mu"], out["b"] = round(mu, 2), round(b, 3)
    for m in markets:
        rng = market_range(m)
        if rng is not None:
            out["markets"][m["ticker"]] = round(range_probability(rng[0], rng[1], mu, b), 4)
    return out


class Kind(NamedTuple):
    """What differs between the daily-low family and the daily-high one
    (hight_fair.py): the series prefix, the NWS hourly profile, the profile
    field that is the day's forecast, The Weather Company's field, the
    event's fair, and the model constants recorded in the file."""
    prefix: str
    profile: Callable
    nws_key: str
    wu_field: str
    fair: Callable
    model: dict


def discover(old: dict, now_ts: float, get_json: Optional[Callable] = None,
             prefix: str = SERIES_PREFIX
             ) -> Tuple[Dict[str, Optional[str]], dict, float, List[str]]:
    """{series: CLI product or None} for every `prefix` series, re-read from
    Kalshi's category listing and each series' rules every DISCOVER_SECS;
    else the cached map. Returns (series map, unmapped, discovered_at, errors)."""
    cached = dict(old.get("series") or {})
    at = float(old.get("discovered_at") or 0.0)
    if cached and now_ts - at < DISCOVER_SECS:
        return cached, dict(old.get("unmapped") or {}), at, []
    errors: List[str] = []
    js = _kalshi("/series", {"category": SERIES_CATEGORY}, get_json)
    names = sorted(s["ticker"] for s in js.get("series") or []
                   if str(s.get("ticker", "")).startswith(prefix))
    smap: Dict[str, Optional[str]] = {}
    unmapped: dict = {}
    for s in names:
        try:
            mj = _kalshi("/markets", {"series_ticker": s, "status": "open", "limit": 50}, get_json)
        except Exception as e:                       # noqa: BLE001
            errors.append(f"{s}: {e}")
            smap[s] = cached.get(s)
            continue
        cli = None
        for m in mj.get("markets") or []:
            hit = _CLI_RE.search(str(m.get("rules_primary") or ""))
            if hit:
                cli = hit.group(0)
                break
        smap[s] = cli
        if cli is None:
            unmapped[s] = ("no open market" if not mj.get("markets")
                           else "rules name no NWS CLI station (not a US station)")
        time.sleep(0.3)
    return smap, unmapped, now_ts, errors


LOW = Kind(SERIES_PREFIX, nws_profile, "day_min", "calendarDayTemperatureMin",
           event_fair, {"bias_f": BIAS_F, "b_mult": B_MULT, "b_pooled": B_POOLED})


def write_fair_file(path: str, get_json: Optional[Callable] = None,
                    now_utc: Optional[datetime] = None,
                    kind: Kind = LOW) -> Tuple[int, int, int]:
    """Refresh every open `kind` event (KXLOWT by default) dated after its
    station's local today (the IMM quotes the day before only). An event
    whose read fails this round keeps its old entry and old fetched_at (the
    gate's TTL governs). Returns (events priced, events skipped, stations
    failed)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    now_ts = now_utc.timestamp()
    old: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f) or {}
    except (OSError, ValueError):
        pass
    smap, unmapped, disc_at, errors = discover(old, now_ts, get_json, kind.prefix)
    stations: Dict[str, dict] = dict(old.get("stations") or {})
    for row in rain_fair.STATIONS.values():
        stations.setdefault(row["cli"], dict(row))
    grid_urls: Dict[str, str] = dict(old.get("grid_urls") or {})
    old_events: dict = dict(old.get("events") or {})
    events: dict = {}
    priced = skipped = failed = 0
    for series, cli in sorted(smap.items()):
        if not cli:
            continue
        try:
            st = stations.get(cli)
            if st is None:
                st = rain_fair.resolve_station(cli)
                stations[cli] = st
            local_today = now_utc.astimezone(pytz.timezone(st["tz"])).date()
            mj = _kalshi("/markets", {"series_ticker": series, "status": "open",
                                      "limit": 200}, get_json)
            by_event: Dict[str, List[dict]] = {}
            for m in mj.get("markets") or []:
                ev = m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0]
                d = event_date(ev)
                if d is not None and d > local_today:
                    by_event.setdefault(ev, []).append(m)
            if not by_event:
                continue
            url = grid_urls.get(cli)
            if not url:
                url = rain_fair.resolve_hourly_url(cli, st)
                grid_urls[cli] = url
            try:
                periods = (_get_json(url).get("properties") or {}).get("periods") or []
            except RuntimeError:
                url = rain_fair.resolve_hourly_url(cli, st)   # moved gridpoint
                grid_urls[cli] = url
                periods = (_get_json(url).get("properties") or {}).get("periods") or []
            wu_js: dict = {}
            if WU_API_KEY:
                try:
                    wu_js = _get_json(WU_URL, {"apiKey": WU_API_KEY,
                                               "geocode": f"{st['lat']},{st['lon']}",
                                               "format": "json", "units": "e",
                                               "language": "en-US"})
                except RuntimeError as e:
                    # the key rides in the URL: never log the exception text
                    errors.append(f"{series} WU: read failed ({type(e).__name__})")
            for ev, mkts in sorted(by_event.items()):
                d = event_date(ev)
                prof = kind.profile(periods, st["tz"], d)
                entry = kind.fair(series, prof.get(kind.nws_key),
                                  wu_day_value(wu_js, d, kind.wu_field), prof, mkts)
                entry.update(date=d.isoformat(), series=series, cli=cli,
                             fetched_at=now_utc.isoformat())
                events[ev] = entry
                if entry["skip"]:
                    skipped += 1
                else:
                    priced += 1
        except Exception as e:                       # noqa: BLE001
            failed += 1
            errors.append(f"{series}: {str(e)[:160]}")
            for ev, e_old in old_events.items():     # keep its last read
                if e_old.get("series") == series:
                    events.setdefault(ev, e_old)
        time.sleep(0.2)                              # be polite to the feeds
    out = {"generated_at": now_utc.isoformat(), "events": events,
           "stations": stations, "grid_urls": grid_urls, "series": smap,
           "unmapped": unmapped, "discovered_at": disc_at,
           "errors": errors[:20], "model": kind.model}
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return priced, skipped, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="lowt_fair.json")
    args = ap.parse_args()
    ok, skip, fail = write_fair_file(args.out)
    with open(args.out, encoding="utf-8") as f:
        js = json.load(f)
    print(f"{ok} events priced, {skip} skipped, {fail} stations failed; "
          f"unmapped {len(js.get('unmapped') or {})}")
    for ev, e in sorted((js.get("events") or {}).items()):
        if e.get("skip"):
            print(f"  {ev:24s} SKIP {e['skip']}")
            continue
        fairs = " ".join(f"{t.rsplit('-', 1)[1]}={p * 100:.0f}"
                         for t, p in sorted(e["markets"].items()))
        print(f"  {ev:24s} nws {e['nws']} wu {e['wu']} -> mu {e['mu']} b {e['b']}: {fairs}")
    for e in js.get("errors") or []:
        print("  !", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
