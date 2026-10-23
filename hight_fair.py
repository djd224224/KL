#!/usr/bin/env python3
r"""High-temperature fair values for the IMM's KXHIGH family (Jack 2026-10-23:
"Investigate building the same thing for high temp IMM. I know there's a
separate bot quoting it, is it possible to keep it separate?" -> "Yes build
high temp recommendation").

The low-temperature fair (lowt_fair.py) turned around for the daily HIGH,
on lowt_fair's writer (its Kind):
  - the NWS hourly forecast's MAXIMUM over the station's local calendar day,
    averaged with The Weather Company's calendarDayTemperatureMax only when
    WU_API_KEY is set (no key is carried here);
  - the high bot's degenerate-day filter: the forecast temperature at 00:00
    within 4.5F of the day's maximum means the high can print at midnight (a
    front cooling the day from the start), and the day stands aside;
  - a Laplace calibrated on the high bot's own history (KXHIGH_market_snapshot
    x KXHIGH_cli_readings, Sep 1 - Oct 22, 1,041 next-day city-days, the
    first evening run of each): the NWS forecast alone missed the CLI high by
    mean |err| 1.87F (rmse 2.58F, p90 4.0F, bias +0.21F), the NWS + WU mean
    by 1.59F (bias +0.39F). The per-city scale is the city's NWS mean |err|
    shrunk to the pooled 1.89F (Las Vegas 1.11F .. Los Angeles 2.69F), times
    B_MULT for the longer leads the IMM quotes at (10:00-20:45 ET the day
    before; the sample's runs were ~21:00 ET), times TWO_SOURCE_MULT with
    both forecasts.
The market's mid beat the high bot's own fair (Brier 0.112 vs 0.133 on
10,805 next-day books since 9/1), so incentive_mm uses this as a WIDE sanity
gate (hight_gate), never to price.

The IMM's "hight-fair" thread calls write_fair_file every
IMM_HIGHT_FAIR_REFRESH_SECS; the file has lowt_fair's shape.

usage: python hight_fair.py [--out FILE]      (writes and prints the fairs)
"""
import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lowt_fair as L  # noqa: E402  (the writer, the Kalshi / NWS reads, the Laplace)

SERIES_PREFIX = "KXHIGH"
BIAS_NWS_F = 0.21
BIAS_TWO_F = 0.39
B_POOLED = 1.89
B_BY_SERIES = {"KXHIGHAUS": 1.74, "KXHIGHCHI": 1.94, "KXHIGHDEN": 1.80,
               "KXHIGHLAX": 2.69, "KXHIGHMIA": 1.36, "KXHIGHNY": 1.80,
               "KXHIGHPHIL": 2.02, "KXHIGHTATL": 1.84, "KXHIGHTBOS": 2.67,
               "KXHIGHTDAL": 1.65, "KXHIGHTDC": 2.08, "KXHIGHTEWR": 1.86,
               "KXHIGHTHOU": 1.42, "KXHIGHTLV": 1.11, "KXHIGHTMIN": 2.17,
               "KXHIGHTNOLA": 1.75, "KXHIGHTOKC": 1.86, "KXHIGHTPHX": 1.81,
               "KXHIGHTSAN": 2.23, "KXHIGHTSATX": 1.66, "KXHIGHTSDF": 1.96,
               "KXHIGHTSEA": 1.82, "KXHIGHTSFO": 2.46, "KXHIGHTTTN": 1.91}
B_MULT = float(os.environ.get("HIGHT_FAIR_B_MULT", "1.25"))
B_FLOOR = 1.0
# both forecasts: the pooled mean |err| 1.59F vs NWS alone 1.87F
TWO_SOURCE_MULT = 0.85
# the high bot's degenerate-day filter
MIDNIGHT_DELTA_F = 4.5


def nws_profile(periods: List[dict], tzname: str, day: date) -> dict:
    """The hourly profile of `day` (station-LOCAL calendar day): day_max,
    max_hour, t00 (the 00:00 forecast), n_hours."""
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
    out = {"day_max": None, "max_hour": None, "t00": None, "n_hours": len(temps)}
    if temps:
        h = max(temps, key=lambda k: (temps[k], -k))
        out.update(day_max=temps[h], max_hour=h, t00=temps.get(0))
    return out


def shape_skip(prof: dict) -> Optional[str]:
    """The high bot's degenerate-day filter, or None when the high is a
    daytime one."""
    t00, dm = prof.get("t00"), prof.get("day_max")
    if t00 is not None and dm is not None and dm - t00 < MIDNIGHT_DELTA_F:
        return (f"midnight high: {t00:g}F at 00:00 is within {MIDNIGHT_DELTA_F:g}F "
                f"of the day's maximum {dm:g}F")
    return None


def event_fair(series: str, nws: Optional[float], wu: Optional[float],
               prof: dict, markets: List[dict]) -> dict:
    """One event's entry (lowt_fair.event_fair's contract). Fails closed: no
    NWS hourly forecast, a partial day or a midnight high skips the day."""
    out: dict = {"nws": nws, "wu": wu, "shape": prof, "skip": None,
                 "mu": None, "b": None, "markets": {}}
    if nws is None:
        out["skip"] = "no NWS hourly forecast for the day (the shape filter cannot run)"
        return out
    if (prof.get("n_hours") or 0) < 20:
        out["skip"] = f"the NWS hourly forecast covers only {prof.get('n_hours')}h of the day"
        return out
    why = shape_skip(prof)
    if why:
        out["skip"] = why
        return out
    b = max(B_FLOOR, B_BY_SERIES.get(series, B_POOLED)) * B_MULT
    if wu is None:
        mu = nws + BIAS_NWS_F
    else:
        mu = (nws + wu) / 2.0 + BIAS_TWO_F
        b *= TWO_SOURCE_MULT
    out["mu"], out["b"] = round(mu, 2), round(b, 3)
    for m in markets:
        rng = L.market_range(m)
        if rng is not None:
            out["markets"][m["ticker"]] = round(L.range_probability(rng[0], rng[1], mu, b), 4)
    return out


HIGH = L.Kind(SERIES_PREFIX, nws_profile, "day_max", "calendarDayTemperatureMax",
              event_fair, {"bias_nws_f": BIAS_NWS_F, "bias_two_f": BIAS_TWO_F,
                           "b_mult": B_MULT, "b_pooled": B_POOLED})


def write_fair_file(path: str, get_json: Optional[Callable] = None,
                    now_utc: Optional[datetime] = None) -> Tuple[int, int, int]:
    """lowt_fair.write_fair_file for the KXHIGH dailies."""
    return L.write_fair_file(path, get_json=get_json, now_utc=now_utc, kind=HIGH)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="hight_fair.json")
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
