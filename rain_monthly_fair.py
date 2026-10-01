#!/usr/bin/env python3
"""Monthly rain fair values for incentive_mm's monthly rain gate.

KXRAIN<CITY>M-<YYMON>-<K> ("If the total precipitation at CLIORD in Chicago
in Oct 2026 is strictly greater than 3 inches, then the market resolves to
Yes") settles on the station's NWS climate report (CLI) month total. Jack
2026-10-01: "quote these monthly rain markets, with an algorithm like how you
quote the dailies KXRAINCHIM-26OCT, KXRAINAUSM-26OCT". The dailies' algorithm:
a fair value from the NWS forecast decides WHETHER the bot joins the touch,
never WHERE (it stands aside when the touch fights the fair by more than the
tolerance), and the bot never quotes the day the rain is measured. The
monthly version, on rain_monthly.py's calibrated model:
  - fair = P(month-to-date + rest of month > K): the month-to-date from the
    CLI, IEM daily obs and today's running total
    (rain_monthly.effective_mtd); the rest by Monte Carlo over 28-46 years
    of the station's history with the NWS gridpoint forecast injected over
    its horizon (rain_monthly.simulate_remaining) -- Brier skill 62% over
    43,080 walk-forward predictions on the climatology backbone;
  - the station is read from each event's own rules ("at CLIORD"), never a
    fixed map: October's KXRAINCHIM settles at O'Hare (CLIORD), not the
    Midway rain_monthly.py was verified on in July;
  - a monthly is measured on every day of its month, so "never the rain
    day" becomes: the event stands aside while it rains at the station (the
    latest observation, younger than OBS_MAX_AGE_MIN, shows precipitation
    in the last hour or a precipitation weather code) and for DRY_MIN after
    the last wet observation; and it stops for good at 22:00 ET the day
    before the month's last day (the cutoff, set in incentive_mm);
  - rungs within BOUNDARY_IN of the month-to-date stand aside (obs vs CLI
    rounding), and every rung of an event whose last CLI is older than
    rain_monthly.STALE_CLI_HOURS.
Writes RAIN_MONTHLY_FILE: per market p and its event; per event the
month-to-date, CLI date, station, wet state, last wet observation and
staleness. The gate in incentive_mm fails CLOSED on all of it.

Standalone:  python rain_monthly_fair.py [--out path]
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

import rain_monthly as rm


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
KALSHI_MARKETS_URL = os.environ.get(
    "IMM_RAIN_MONTHLY_MARKETS_URL",
    "https://api.elections.kalshi.com/trade-api/v2/markets")
SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_RAIN_MONTHLY_SERIES", "KXRAINCHIM,KXRAINAUSM").split(",") if s.strip())
N_SAMPLES = int(_env_float("IMM_RAIN_MONTHLY_SAMPLES", rm.N_SAMPLES))
OBS_MAX_AGE_MIN = _env_float("IMM_RAIN_MONTHLY_OBS_MAX_AGE_MIN", 90)
BOUNDARY_IN = _env_float("IMM_RAIN_MONTHLY_BOUNDARY_IN", rm.BOUNDARY_IN)
EVENTS_TTL_SECS = 3600
HTTP_TIMEOUT = 30

# CLI code (the rules' "at CLIxxx") -> station spec in rain_monthly.STATIONS'
# shape. Seeded from rain_monthly's verified stations, plus O'Hare (CLIORD,
# KXRAINCHIM from Oct 2026). A code missing here leaves its event without a
# fair -- stood aside, never guessed.
CLI_STATIONS: Dict[str, dict] = {
    v["iem"]: {k: v[k] for k in ("icao", "iem", "net", "name", "lat", "lon", "tz")}
    for v in rm.STATIONS.values()}
CLI_STATIONS["ORD"] = {"icao": "KORD", "iem": "ORD", "net": "IL_ASOS",
                       "name": "Chicago O'Hare", "lat": 41.995, "lon": -87.934,
                       "tz": "US/Central"}

# METAR present-weather tokens that mean precipitation at or near the station
_WET_RE = re.compile(r"(RA|DZ|SN|SG|PL|GR|GS|UP|TS|SH)")
_CLI_RE = re.compile(r"\bat\s+CLI([A-Z]{3})\b")


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[rain-monthly-fair] {msg}", flush=True)


def station_code(rules: str) -> Optional[str]:
    """The settlement station's CLI code from a market's rules text."""
    m = _CLI_RE.search(rules or "")
    return m.group(1) if m else None


def _signed_or_public(get_json: Optional[Callable], params: dict) -> dict:
    if get_json is not None:
        try:
            js = get_json("/markets", dict(params))
            if isinstance(js, dict):
                return js
        except Exception:
            pass
    for attempt in range(3):
        r = requests.get(KALSHI_MARKETS_URL, params=params, timeout=HTTP_TIMEOUT)
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 2:
            time.sleep(3 * (attempt + 1) ** 2)
            continue
        r.raise_for_status()
        return r.json() or {}
    return {}


def fetch_events(get_json: Optional[Callable] = None) -> Dict[str, dict]:
    """Open monthly rain events: event -> {"series", "code", "strikes":
    {ticker: K}}. The code comes from the rules; an event whose markets
    disagree on it gets no code (stood aside)."""
    out: Dict[str, dict] = {}
    for s in SERIES:
        js = _signed_or_public(get_json, {"series_ticker": s, "status": "open", "limit": 200})
        for m in js.get("markets") or []:
            ev = str(m.get("event_ticker") or "")
            t = str(m.get("ticker") or "")
            try:
                k = float(m.get("floor_strike"))
            except (TypeError, ValueError):
                continue
            if not ev or not t or str(m.get("strike_type") or "greater") != "greater":
                continue
            code = station_code(f"{m.get('rules_primary') or ''}")
            e = out.setdefault(ev, {"series": s, "code": code, "strikes": {}})
            if e["code"] != code:
                e["code"] = None
            e["strikes"][t] = k
    return out


def obs_state(spec: dict, now: datetime) -> dict:
    """The station's latest observation: {"wet": True/False/None, "obs_time",
    "phour", "wx"}. None = no observation younger than OBS_MAX_AGE_MIN."""
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
    wet = bool((phour or 0.0) > 0.0 or _WET_RE.search(wx))
    return {"wet": wet, "obs_time": t.isoformat(), "phour": phour, "wx": wx}


def event_fair(event: str, info: dict, now: datetime,
               n_samples: int = N_SAMPLES,
               rng: Optional[random.Random] = None) -> Optional[dict]:
    """Per-market fair for one event and its month-to-date state; None when
    the station or the month is unknown (the gate then stands aside)."""
    code = info.get("code")
    spec = CLI_STATIONS.get(code or "")
    ym = rm.parse_event_month(event)
    if spec is None or ym is None or not info.get("strikes"):
        return None
    key = f"CLI{code}"
    rm.STATIONS[key] = dict(spec, series=info.get("series", ""))
    year, month = ym
    mtd = rm.effective_mtd(key, year, month, now)
    if mtd.get("mtd") is None:
        return None
    sim = rm.simulate_remaining(key, year, month, now, n_samples=n_samples,
                                rng=rng or random.Random())
    strikes = info["strikes"]
    p = rm.price_rungs(float(mtd["mtd"]), sim["totals"], sorted(set(strikes.values())))
    return {"p": {t: round(p[k], 4) for t, k in strikes.items()},
            "boundary": sorted(t for t, k in strikes.items()
                               if abs(float(mtd["mtd"]) - k) < BOUNDARY_IN),
            "mtd": mtd["mtd"], "cli_mtd": mtd.get("cli_mtd"),
            "cli_date": mtd.get("cli_date"), "stale_cli": bool(mtd.get("stale")),
            "pday": mtd.get("pday"), "notes": mtd.get("notes") or [],
            "horizon_days": sim.get("horizon_days"),
            "n_segments": sim.get("n_segments")}


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    events: Optional[Dict[str, dict]] = None,
                    fair_fn: Callable = event_fair,
                    obs_fn: Callable = obs_state) -> Tuple[int, int]:
    """Build and atomically write the fair file. Returns (events with a
    fair, events without one). Kalshi's open events are re-read hourly; a
    failed read reuses the previous file's. The last wet observation per
    event survives across writes (the DRY_MIN cool-down)."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    ev_cache = old.get("events") or {}
    events_at = old.get("events_at")
    if events is None:
        if ev_cache and ts - float(events_at or 0) < EVENTS_TTL_SECS:
            events = ev_cache
        else:
            try:
                events = fetch_events(get_json)
                events_at = ts
            except Exception as e:
                if not ev_cache:
                    raise
                _log(f"! Kalshi event read failed ({type(e).__name__}); "
                     f"reusing {len(ev_cache)} cached")
                events = ev_cache
    old_ev = old.get("event_state") or {}
    markets: Dict[str, dict] = {}
    state: Dict[str, dict] = {}
    missing: List[str] = []
    for ev, info in sorted(events.items()):
        spec = CLI_STATIONS.get(info.get("code") or "")
        st = {"code": info.get("code"), "fetched_at": now.isoformat(),
              "last_wet_at": (old_ev.get(ev) or {}).get("last_wet_at")}
        if spec is not None:
            ob = obs_fn(spec, now)
            st.update(wet=ob["wet"], obs_time=ob["obs_time"], phour=ob["phour"],
                      wx=ob["wx"])
            if ob["wet"]:
                st["last_wet_at"] = ob["obs_time"]
        try:
            f = fair_fn(ev, info, now) if spec is not None else None
        except Exception as e:
            _log(f"! {ev}: {type(e).__name__}: {str(e)[:120]}")
            f = None
        if f is None:
            missing.append(ev)
            st["error"] = "no station" if spec is None else "no fair"
            state[ev] = st
            continue
        st.update({k: f[k] for k in ("mtd", "cli_mtd", "cli_date", "stale_cli",
                                     "pday", "notes", "horizon_days", "n_segments")})
        state[ev] = st
        for t, p in f["p"].items():
            markets[t] = {"event": ev, "p": p, "boundary": t in f["boundary"],
                          "fetched_at": now.isoformat()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": now.isoformat(), "markets": markets,
                   "event_state": state, "missing": missing, "events": events,
                   "events_at": events_at,
                   "model": {"samples": N_SAMPLES, "obs_max_age_min": OBS_MAX_AGE_MIN,
                             "boundary_in": BOUNDARY_IN,
                             "forecast_weight": rm.FORECAST_WEIGHT,
                             "stale_cli_hours": rm.STALE_CLI_HOURS}},
                  fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return len(events) - len(missing), len(missing)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "rain_monthly_fair.json"))
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    _log(f"{ok} events with a fair, {miss} without -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
