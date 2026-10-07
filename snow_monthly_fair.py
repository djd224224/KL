#!/usr/bin/env python3
"""Monthly snowfall fair values for incentive_mm's snow gate.

Jack 2026-10-04: "build the snow feed". KX<CITY>SNOWM-<YYMON>-<K> ("If the
total snowfall in Chicago (O'Hare) in December 2026 is strictly greater than
8 inches, then the market resolves to Yes") settles on The Weather Company's
Kalshi dashboard, Snowfall tab: "Official daily snowfall from the NWS climate
report, for the city's own local calendar day" -- the CLI's daily snowfall at
the city's primary official station, summed over the month (contract
SNOWOVERTIME; checked on weather.com/kalshi 2026-10-04). The rules name the
city only ("in Boston"; "in Chicago (O'Hare)"), so CITY_CODES maps the place
to the dashboard's station: Boston KBOS, Chicago (O'Hare) KORD, Washington
DC KDCA, Denver KDEN, Detroit KDTW, Milwaukee KMKE, Minneapolis KMSP, New
York City KNYC, Philadelphia KPHL, Pittsburgh KPIT. An unmapped place, or
rules naming another month than the ticker, is no station: stood aside.

The fair is the monthly rain gate's, P(month to date + rest > K), on snow:
  - TO DATE: the latest CLI's month-to-date snowfall (IEM json/cli.py,
    "snow_month"; trace counts 0, as the dashboard's totals do) and the
    report's issue time from its product id. ASOS measures no snowfall, so
    nothing fills the hours after the report: snow observed at the station
    after it was issued (latest_snow_at, kept across writes) makes the event
    STALE until a later report counts it -- "the snow day", failing closed.
    A CLI older than rain_monthly.STALE_CLI_HOURS is stale too.
  - REST: rain_monthly_fair.simulate_period on the station's ACIS daily
    SNOWFALL (1980 on; Denver's airport record starts 2006, so its history
    is the threaded Denver Area record DENthr), whole calendar-aligned
    windows, the NWS grid's snowfallAmount injected over its horizon, and
    the historical part smoothed by a mean-one log-normal kernel
    (KERNEL_SIGMA 0.25; zeros stay zero). Leave-one-out over 1980-2025
    Decembers at the ten stations, the listed strikes (2,688 predictions):
    Brier 0.1827 raw, 0.1823 at sigma 0.15, 0.1820 at 0.25; a coin is 0.25.
  - SNOWING: while the month is open the event stands aside while it snows
    at the station (the latest observation, younger than OBS_MAX_AGE_MIN:
    SN/SG/PL/GS/UP in the present weather, or precipitation at 35F or
    below) and, in incentive_mm, for SNOW_MONTHLY_DRY_MIN after the last
    such observation. Before its month starts an event reads dry with no
    observation: October snow counts nothing toward December.
  - Strikes within BOUNDARY_IN (0.1", the CLI's precision) of the to-date
    stand aside (preliminary vs final reports).
Writes SNOW_MONTHLY_FILE in rain_monthly_fair's shape (markets: p and event;
event_state: wet, last_wet_at, stale_cli, stale_why, mtd, ...), which the
gate in incentive_mm reads with the rain gate's loader and checks.

Standalone:  python snow_monthly_fair.py [--out path]
"""

import argparse
import calendar
import json
import os
import random
import re
import sys
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import pytz

import rain_monthly as rm
import rain_monthly_fair as rmf


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


STATUS_DIR = rmf.STATUS_DIR
ALL_SERIES = ("KXBOSSNOWM,KXCHISNOWM,KXDCSNOWM,KXDENSNOWM,KXDETSNOWM,KXMKESNOWM,"
              "KXMSPSNOWM,KXNYCSNOWM,KXPHILSNOWM,KXPITSNOWM")
SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_SNOW_MONTHLY_SERIES", ALL_SERIES).split(",") if s.strip())
N_SAMPLES = int(_env_float("IMM_SNOW_MONTHLY_SAMPLES", rm.N_SAMPLES))
KERNEL_SIGMA = _env_float("IMM_SNOW_KERNEL_SIGMA", 0.25)
BOUNDARY_IN = _env_float("IMM_SNOW_BOUNDARY_IN", 0.1)
SNOW_TEMP_F = _env_float("IMM_SNOW_TEMP_F", 35.0)
OBS_MAX_AGE_MIN = rmf.OBS_MAX_AGE_MIN
EVENTS_TTL_SECS = rmf.EVENTS_TTL_SECS

# The dashboard's snowfall stations (2026-10-04), each checked live that day
# on IEM currents ({state}_ASOS), IEM's CLI (K + code) and ACIS. "acis" only
# where the airport's own snowfall record is short.
SNOW_STATIONS: Dict[str, dict] = {}
for _code, _net, _name, _lat, _lon, _tz, _acis in (
        ("BOS", "MA_ASOS", "Boston Logan", 42.3606, -71.0097, "US/Eastern", None),
        ("ORD", "IL_ASOS", "Chicago O'Hare", 41.995, -87.934, "US/Central", None),
        ("MDW", "IL_ASOS", "Chicago Midway", 41.786, -87.752, "US/Central", None),
        ("DCA", "VA_ASOS", "Washington Reagan National", 38.8472, -77.0346,
         "US/Eastern", None),
        ("DEN", "CO_ASOS", "Denver Intl", 39.847, -104.656, "US/Mountain", "DENthr"),
        ("DTW", "MI_ASOS", "Detroit Metro", 42.2124, -83.3534, "US/Eastern", None),
        ("MKE", "WI_ASOS", "Milwaukee Mitchell", 42.947, -87.897, "US/Central", None),
        ("MSP", "MN_ASOS", "Minneapolis-St Paul", 44.8831, -93.2289, "US/Central", None),
        ("NYC", "NY_ASOS", "NY Central Park", 40.779, -73.969, "US/Eastern", None),
        ("PHL", "PA_ASOS", "Philadelphia Intl", 39.8721, -75.2411, "US/Eastern", None),
        ("PIT", "PA_ASOS", "Pittsburgh Intl", 40.4915, -80.2329, "US/Eastern", None),
        # Seattle (Jack 2026-10-06: "why isnt KXSEASNOWM-26DEC [quoting]?"):
        # Sea-Tac, checked live that day -- IEM currents WA_ASOS, the KSEA
        # CLI, ACIS snowfall back to 1980
        ("SEA", "WA_ASOS", "Seattle-Tacoma Intl", 47.4447, -122.3144, "US/Pacific", None)):
    SNOW_STATIONS[_code] = {"icao": f"K{_code}", "iem": _code, "net": _net,
                            "name": _name, "lat": _lat, "lon": _lon, "tz": _tz}
    if _acis:
        SNOW_STATIONS[_code]["acis"] = _acis
# the rules' place -> station (the dashboard's names; a bare "Chicago" is
# two stations, so it maps to none)
CITY_CODES = {"Boston": "BOS", "Chicago (O'Hare)": "ORD", "Chicago (Midway)": "MDW",
              "Washington DC": "DCA", "Washington, DC": "DCA", "Denver": "DEN",
              "Detroit": "DTW", "Milwaukee": "MKE", "Minneapolis": "MSP",
              "New York City": "NYC", "Philadelphia": "PHL", "Pittsburgh": "PIT",
              "Seattle": "SEA"}

_RULES_RE = re.compile(r"\btotal snowfall (?:at|in) (.+?) in ([A-Z][a-z]+) (\d{4})\b")
_MONTH_NAMES = {n: i for i, n in enumerate(calendar.month_name) if n}
# present-weather tokens of falling snow, sleet or ice (sleet counts as
# snowfall in the CLI) and the ASOS's unknown precipitation
_SNOW_RE = re.compile(r"(SN|SG|PL|GS|UP)")
_PRODUCT_TS_RE = re.compile(r"^(\d{12})-")


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[snow-monthly-fair] {msg}", flush=True)


def station_code(rules: str, event: str) -> Optional[str]:
    """The station the rules' place names, when the rules' month and year
    are the ticker's (KXCHISNOWM-26DEC -> "in Chicago (O'Hare) in December
    2026" -> ORD). None otherwise."""
    m = _RULES_RE.search(rules or "")
    ym = rm.parse_event_month(event)
    if not m or ym is None:
        return None
    if (int(m.group(3)), _MONTH_NAMES.get(m.group(2))) != ym:
        return None
    return CITY_CODES.get(m.group(1).strip())


def fetch_events(get_json: Optional[Callable] = None,
                 series: Optional[Tuple[str, ...]] = None) -> Dict[str, dict]:
    """Open snow-total events: event -> {"series", "code", "strikes":
    {ticker: K}}; markets that disagree on the station leave no code."""
    out: Dict[str, dict] = {}
    for s in series or SERIES:
        js = rmf._signed_or_public(get_json, {"series_ticker": s, "status": "open",
                                              "limit": 200})
        for m in js.get("markets") or []:
            ev = str(m.get("event_ticker") or "")
            t = str(m.get("ticker") or "")
            try:
                k = float(m.get("floor_strike"))
            except (TypeError, ValueError):
                continue
            if not ev or not t or str(m.get("strike_type") or "greater") != "greater":
                continue
            code = station_code(f"{m.get('rules_primary') or ''}", ev)
            e = out.setdefault(ev, {"series": s, "code": code, "strikes": {}})
            if e["code"] != code:
                e["code"] = None
            e["strikes"][t] = k
    return out


def _issued(product: str) -> Optional[datetime]:
    """A CLI product id's issue time (UTC): 202610042138-KLOT-CDUS43-CLIORD."""
    m = _PRODUCT_TS_RE.match(product or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def cli_state(spec: dict, year: int, month: int) -> dict:
    """The latest CLI of (year, month): {"mtd", "date", "issued", "day"}
    (mtd None when the month has no report yet)."""
    data = rm.cached_get_json(
        f"snowcli_{spec['icao']}_{year}", 1800,
        lambda: rm.http_json(f"https://mesonet.agron.iastate.edu/json/cli.py"
                             f"?station={spec['icao']}&year={year}"))
    rows = [r for r in (data.get("results") or [])
            if str(r.get("valid", "")).startswith(f"{year}-{month:02d}")]
    if not rows:
        return {"mtd": None, "date": None, "issued": None, "day": None}
    last = rows[-1]
    return {"mtd": rm.parse_precip(last.get("snow_month")), "date": str(last.get("valid")),
            "issued": _issued(str(last.get("product") or "")),
            "day": rm.parse_precip(last.get("snow"))}


def snow_to_date(spec: dict, year: int, month: int, now: datetime,
                 last_snow_at: Optional[str] = None) -> dict:
    """The month's snowfall so far and whether it can be trusted: stale when
    the latest report is older than rm.STALE_CLI_HOURS, when snow was seen at
    the station after it was issued (or since the month began, with no
    report yet), or when the report carries no month total."""
    tz = pytz.timezone(spec["tz"])
    cli = cli_state(spec, year, month)
    out = {"mtd": cli["mtd"], "cli_date": cli["date"], "stale": False, "why": None,
           "cli_issued": cli["issued"].isoformat() if cli["issued"] else None,
           "notes": []}
    seen = None
    if last_snow_at:
        try:
            seen = datetime.fromisoformat(str(last_snow_at).replace("Z", "+00:00"))
        except ValueError:
            seen = now                      # unreadable: assume just now
    month_start = tz.localize(datetime(year, month, 1)).astimezone(timezone.utc)
    if cli["date"] is None:
        out["mtd"] = 0.0
        out["notes"].append("no CLI yet this month")
        if seen is not None and seen >= month_start:
            out.update(stale=True, why="snow this month and no climate report yet")
        return out
    if cli["mtd"] is None:
        out.update(mtd=0.0, stale=True, why="the climate report has no month total")
        return out
    age_h = (now - tz.localize(datetime.strptime(cli["date"], "%Y-%m-%d"))
             .astimezone(timezone.utc)).total_seconds() / 3600.0
    if age_h > rm.STALE_CLI_HOURS:
        out.update(stale=True, why=f"the station's CLI is {age_h:.0f}h old")
    elif seen is not None and seen >= month_start and (
            cli["issued"] is None or seen > cli["issued"]):
        out.update(stale=True, why="snow since the last climate report")
    return out


def obs_state(spec: dict, now: datetime) -> dict:
    """rain_monthly_fair.obs_state's shape, where "wet" means snowing: snow,
    sleet or ice in the present weather, or precipitation at SNOW_TEMP_F or
    colder. None = no observation younger than OBS_MAX_AGE_MIN."""
    try:
        data = rm.http_json(f"https://mesonet.agron.iastate.edu/api/1/currents.json"
                            f"?station={spec['iem']}&network={spec['net']}", tries=2)
        row = (data.get("data") or [{}])[0]
    except RuntimeError:
        return {"wet": None, "obs_time": None, "phour": None, "wx": None}
    t_s = str(row.get("utc_valid") or "")
    try:
        t = datetime.fromisoformat(t_s.replace("Z", "+00:00"))
    except ValueError:
        t = None
    phour = rm.parse_precip(row.get("phour"))
    wx = str(row.get("wxcodes") or "")
    if t is None or (now - t).total_seconds() > OBS_MAX_AGE_MIN * 60:
        return {"wet": None, "obs_time": t_s or None, "phour": phour, "wx": wx}
    try:
        cold = float(row.get("tmpf")) <= SNOW_TEMP_F
    except (TypeError, ValueError):
        cold = False
    wet = bool(_SNOW_RE.search(wx) or ((phour or 0.0) > 0.0 and cold))
    return {"wet": wet, "obs_time": t.isoformat(), "phour": phour, "wx": wx}


def event_fair(event: str, info: dict, now: datetime, n_samples: int = N_SAMPLES,
               rng: Optional[random.Random] = None) -> Optional[dict]:
    """Per-market P(month total > K) for one event and its to-date state;
    None when the station or the month is unknown or the month is over."""
    code = info.get("code")
    spec = SNOW_STATIONS.get(code or "")
    ym = rm.parse_event_month(event)
    if spec is None or ym is None or not info.get("strikes"):
        return None
    year, month = ym
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    tz = pytz.timezone(spec["tz"])
    today = now.astimezone(tz).date()
    if today > end:
        return None
    key = f"SNOW{code}"
    rm.STATIONS[key] = dict(spec, series=info.get("series", ""))
    if today < start:
        td = {"mtd": 0.0, "cli_date": None, "cli_issued": None, "stale": False,
              "why": None, "notes": ["month not started"]}
    else:
        td = snow_to_date(spec, year, month, now, info.get("last_wet_at"))
    hist = rmf.period_history(key, now, elem="snow")
    sim = rmf.simulate_period(key, start, end, now, n_samples=n_samples,
                              rng=rng or random.Random(), hist=hist,
                              element="snowfallAmount", kernel_sigma=KERNEL_SIGMA)
    strikes = info["strikes"]
    mtd = float(td["mtd"])
    p = rm.price_rungs(mtd, sim["totals"], sorted(set(strikes.values())))
    return {"p": {t: round(p[k], 4) for t, k in strikes.items()},
            "boundary": sorted(t for t, k in strikes.items()
                               if abs(mtd - k) < BOUNDARY_IN),
            "mtd": mtd, "cli_mtd": td["mtd"], "cli_date": td["cli_date"],
            "cli_issued": td["cli_issued"], "stale_cli": td["stale"],
            "stale_why": td["why"], "pday": None, "notes": td["notes"],
            "horizon_days": sim["horizon_days"], "n_segments": sim["n_segments"],
            "in_period": today >= start}


def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    events: Optional[Dict[str, dict]] = None,
                    fair_fn: Callable = event_fair,
                    obs_fn: Callable = obs_state,
                    extra_series: Tuple[str, ...] = ()) -> Tuple[int, int]:
    """Build and atomically write the snow fair file; returns (events with a
    fair, events without one). Kalshi's open events are re-read hourly, and
    at once when `extra_series` (the bot's feed) names a series the cached
    read lacks; a failed read reuses the previous file's. The last snow
    observation per event survives across writes. An event before its
    month reads dry without an observation."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = rmf._read_json(path)
    ev_cache = old.get("events") or {}
    events_at = old.get("events_at")
    want = tuple(dict.fromkeys(tuple(SERIES) + tuple(extra_series or ())))
    have = old.get("events_series")
    if events is None:
        if ev_cache and ts - float(events_at or 0) < EVENTS_TTL_SECS \
                and have is not None and set(want) <= set(have):
            events = ev_cache
            want = tuple(have)
        else:
            try:
                events = fetch_events(get_json, series=want)
                events_at = ts
            except Exception as e:
                if not ev_cache:
                    raise
                _log(f"! Kalshi event read failed ({type(e).__name__}); "
                     f"reusing {len(ev_cache)} cached")
                events = ev_cache
                want = tuple(have) if have is not None else want
    old_ev = old.get("event_state") or {}
    markets: Dict[str, dict] = {}
    state: Dict[str, dict] = {}
    missing: List[str] = []
    for ev, info in sorted(events.items()):
        spec = SNOW_STATIONS.get(info.get("code") or "")
        st = {"code": info.get("code"), "fetched_at": now.isoformat(),
              "last_wet_at": (old_ev.get(ev) or {}).get("last_wet_at")}
        ym = rm.parse_event_month(ev)
        before = bool(spec and ym and now.astimezone(pytz.timezone(spec["tz"])).date()
                      < date(ym[0], ym[1], 1))
        if before:
            # snow before the month counts for nothing: no stand-aside
            st.update(wet=False, obs_time=None, phour=None, wx=None)
        elif spec is not None:
            ob = obs_fn(spec, now)
            st.update(wet=ob["wet"], obs_time=ob["obs_time"], phour=ob["phour"],
                      wx=ob["wx"])
            if ob["wet"]:
                st["last_wet_at"] = ob["obs_time"]
        try:
            f = fair_fn(ev, dict(info, last_wet_at=st["last_wet_at"]), now) \
                if spec is not None else None
        except Exception as e:
            _log(f"! {ev}: {type(e).__name__}: {str(e)[:120]}")
            f = None
        if f is None:
            missing.append(ev)
            st["error"] = "no station" if spec is None else "no fair"
            state[ev] = st
            continue
        st.update({k: f.get(k) for k in ("mtd", "cli_mtd", "cli_date", "cli_issued",
                                         "stale_cli", "stale_why", "notes",
                                         "horizon_days", "n_segments", "in_period")})
        state[ev] = st
        for t, p in f["p"].items():
            markets[t] = {"event": ev, "p": p, "boundary": t in f["boundary"],
                          "fetched_at": now.isoformat()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": now.isoformat(), "markets": markets,
                   "event_state": state, "missing": missing, "events": events,
                   "events_at": events_at, "events_series": list(want),
                   "model": {"samples": N_SAMPLES, "obs_max_age_min": OBS_MAX_AGE_MIN,
                             "boundary_in": BOUNDARY_IN, "kernel_sigma": KERNEL_SIGMA,
                             "forecast_weight": rm.FORECAST_WEIGHT,
                             "stale_cli_hours": rm.STALE_CLI_HOURS,
                             "snow_temp_f": SNOW_TEMP_F}},
                  fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return len(events) - len(missing), len(missing)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "snow_monthly_fair.json"))
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    _log(f"{ok} events with a fair, {miss} without -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
