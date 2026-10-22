#!/usr/bin/env python3
"""Ski-resort season snowfall fair values for incentive_mm's resort snow gate.

Jack 2026-10-22: "No way to fair value price it?" -> "Do it best you can and
quote it". KX<RESORT>SNOW-24DEC26-GT<K> ("If the total snowfall recorded at
Beaver Creek Resort October 1, 2026 through December 24, 2026 (value
recorded at 4:00 PM ET on December 24) is above 50 inches, then the market
resolves to Yes") settles on the resort's own recorded total. No public
archive of a resort's daily reports exists, so each resort borrows the
longest nearby NOAA snowfall record (ACIS) that still reports daily, scaled
to the resort's level:

  scale   the resort's average season (OnTheSnow's multi-season average of
          the resort's own reports, read 10/22) / the proxy's average season
          over the same recent seasons (SCALE_FROM_SEASON on), x the
          resort's LEVEL (RESORTS: the launch-day book's median)
  TO DATE the proxy's daily snowfall from the window's first day through its
          latest report, x scale -- an hourly ACIS read through today (the
          season history's daily read ends yesterday, so a storm's report
          would wait a day)
  REST    rain_monthly_fair.simulate_period on the proxy's history (whole
          calendar-aligned windows, x scale) with the NWS grid's
          snowfallAmount AT THE RESORT injected over its horizon, smoothed
          by the snow gate's log-normal kernel
  ANCHOR  the model alone does not have the book's shape -- on launch day
          its tails ran 10-14c fatter (Beaver Creek over 115": model 28c,
          book 16/17) -- and the market sees the resort's own reports, which
          we cannot. So each strike's fair is the model's P shifted in
          log-odds by an offset that starts at the book's mid on the
          strike's first calm read and follows the mid with an
          ANCHOR_TAU_HOURS time constant (one step per hourly book read, at
          most ANCHOR_MAX_STEP_HOURS of it), frozen while the event is wet,
          drying or stale. The fair moves with the proxy's reports and the
          forecast; a book that runs away from it faster than the anchor
          follows is what the bot's band catches. A strike without a usable
          book (one-sided, or wider than ANCHOR_MAX_SPREAD_C) borrows its
          neighbours' offsets; an event with no anchored strike has no fair
          (the bot stands it aside).
  STORMS  any precipitation at the resort's nearest airport (the IEM's
          current observation) reads "snowing" -- a mountain gets snow when
          its valley gets rain -- and the bot stands the event aside while it
          lasts and DRY_MIN after; precipitation after the latest report's
          morning observation (REPORT_HOUR local: COOP and CoCoRaHS days end
          then) makes the event STALE until a later report counts it, as
          does a proxy more than MAX_REPORT_LAG_DAYS behind

Proxies (ACIS, 10/22), resort season averages (OnTheSnow):
  Beaver Creek  DILLON 1 E   (9,081 ft, since 1893)  226"  airport EGE
  Keystone      DILLON 1 E                            208"  airport LXV
  Northstar     TAHOE CITY   (6,230 ft, since 1904)  340"  airport TRK
  Park City     ALTA         (8,710 ft, since 1905)  281"  airport HCR
  Mammoth       LEE VINING   (6,797 ft, since 1988)  331"  airport MMH  WEAK
  Stowe         UNDERHILL 4.4 NNE (936 ft, 1999)     225"  airport MVL  WEAK
(Vail, Beaver Creek's own valley, reports to ACIS only monthly; no ACIS
station near Mammoth Lakes reports snowfall.) The weak two -- an east-side
rain shadow, a valley gauge -- get a wider band in the bot
(RESORT_SNOW_WEAK_SERIES).

NEW RESORTS (Jack 2026-10-22: "Make sure new markets like this family will
auto quote"): every KX<RESORT>SNOW series (SERIES_RE) in the bot's program
feed comes in as `extra_series`, read at once. A resort whose rules name a
KNOWN_RESORTS entry is mapped once and kept in the file: its nearest ACIS
gauge with AUTO_PROXY_MIN_SEASONS+ complete seasons (within ~50 km), its
nearest active ASOS (the IEM's network for its state), and -- with no
OnTheSnow average or launch-day LEVEL -- a scale fitted per event so the
model's median season total sits on the book's 50% point (fit_scale, on
the first usable book). The anchor does the rest, as for the six. An
unknown resort, or one with no gauge, has no fair ("unmapped" in the file)
and the bot leaves it out; the file's "mapped" list is what it enrolls.

Writes RESORT_SNOW_FILE in snow_monthly_fair's shape (markets: p, event,
boundary, fetched_at, plus p_model / offset / anchored; event_state: wet,
last_wet_at, stale_cli, stale_why, mtd, ...; anchors), read by the bot's
weather-fair loader.

Standalone:  python resort_snow_fair.py [--out path]
"""

import argparse
import json
import math
import os
import random
import re
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import pytz

import rain_monthly as rm
import rain_monthly_fair as rmf
import snow_monthly_fair as smf

STATUS_DIR = rmf.STATUS_DIR
N_SAMPLES = smf.N_SAMPLES
KERNEL_SIGMA = smf.KERNEL_SIGMA
EVENTS_TTL_SECS = rmf.EVENTS_TTL_SECS
OBS_MAX_AGE_MIN = rmf.OBS_MAX_AGE_MIN
SCALE_FROM_SEASON = int(os.environ.get("IMM_RESORT_SNOW_SCALE_FROM", "2012"))
MAX_REPORT_LAG_DAYS = float(os.environ.get("IMM_RESORT_SNOW_MAX_LAG_DAYS", "3"))
MIN_SEASONS = 5          # fewer complete proxy seasons since SCALE_FROM_SEASON: use all
RECENT_TTL_SECS = 3600   # the to-date's ACIS read through today
REPORT_HOUR = 7          # a COOP / CoCoRaHS report's day ends at this local hour
# the bot's RESORT_SNOW_DRY_MIN: no anchor step this soon after precipitation
DRY_MIN = float(os.environ.get("IMM_RESORT_SNOW_DRY_MIN", "60"))
ANCHOR_TAU_HOURS = float(os.environ.get("IMM_RESORT_SNOW_ANCHOR_TAU_HOURS", "24"))
ANCHOR_MAX_STEP_HOURS = 2.0
ANCHOR_MAX_SPREAD_C = int(os.environ.get("IMM_RESORT_SNOW_ANCHOR_MAX_SPREAD", "10"))
ANCHOR_MAX_OFFSET = 3.0  # log-odds
P_FLOOR = 0.002          # the model's P clipped to [P_FLOOR, 1 - P_FLOOR] for log-odds

# series -> the rules' resort name, the resort (lat, lon, tz), the ACIS proxy
# (sid, name), the resort's average season (inches), its nearest airport
# (IEM id, network), and its LEVEL: the multiplier that put the model's
# median for 10/1-12/24 on the book's 50% point on launch day (10/22 19:45Z).
# The season ratio alone read the three high resorts at about half the
# market (Beaver Creek 46" vs 85", Keystone 42" vs 65", Mammoth 48" vs 88")
# and the other three within ~10% (Northstar 72 vs 82, Park City 76 vs 70,
# Stowe 57 vs 55): the proxies sit far below the high resorts, where October
# and November snow that the valley gauges miss counts toward the window,
# and OnTheSnow's averages (from opening day: Beaver Creek Nov 23", Dec 47")
# leave out that pre-opening snow. So the LEVEL comes from the market once,
# and the ANCHOR (above) takes the shape from it.
RESORTS: Dict[str, dict] = {}
for _s, _name, _lat, _lon, _tz, _sid, _pname, _avg, _apt, _level in (
        ("KXBEAVERSNOW", "Beaver Creek Resort", 39.6042, -106.5165, "US/Mountain",
         "052281", "DILLON 1 E", 226.0, ("EGE", "CO_ASOS"), 1.86),
        ("KXKEYSTONESNOW", "Keystone Resort", 39.6045, -105.9548, "US/Mountain",
         "052281", "DILLON 1 E", 208.0, ("LXV", "CO_ASOS"), 1.54),
        ("KXNORTHSTARSNOW", "Northstar California Resort", 39.2746, -120.1211, "US/Pacific",
         "048758", "TAHOE CITY", 340.0, ("TRK", "CA_ASOS"), 1.13),
        ("KXPARKCSNOW", "Park City Mountain", 40.6514, -111.5080, "US/Mountain",
         "420072", "ALTA", 281.0, ("HCR", "UT_ASOS"), 0.92),
        ("KXMAMMOTHSNOW", "Mammoth Mountain", 37.6308, -119.0326, "US/Pacific",
         "044881", "LEE VINING", 331.0, ("MMH", "CA_ASOS"), 1.84),
        ("KXSTOWESNOW", "Stowe Mountain Resort", 44.5303, -72.7814, "US/Eastern",
         "US1VTCH0004", "UNDERHILL 4.4 NNE", 225.0, ("MVL", "VT_ASOS"), 0.95)):
    RESORTS[_s] = {"place": _name, "lat": _lat, "lon": _lon, "tz": _tz, "acis": _sid,
                   "icao": _sid, "proxy": _pname, "season_avg": _avg,
                   "iem": _apt[0], "net": _apt[1], "name": _name, "level": _level}
SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_RESORT_SNOW_SERIES", ",".join(RESORTS)).split(",") if s.strip())
# the shape a new resort's series takes (the bot's RESORT_SNOW_SERIES_RE)
SERIES_RE = re.compile(os.environ.get("IMM_RESORT_SNOW_SERIES_RE", r"KX[A-Z]{2,20}SNOW"))
AUTO_MAP_RETRY_SECS = 6 * 3600      # an unmapped resort is tried again after this
AUTO_PROXY_MIN_SEASONS = 6          # complete seasons since 2010 a new gauge needs
AUTO_PROXY_CANDIDATES = 30          # nearest gauges whose history is read
AUTO_AIRPORT_MAX_KM = 100.0
FIT_LO, FIT_HI, FIT_STEPS, FIT_SAMPLES, FIT_SEED = 0.05, 50.0, 16, 4000, 7

# KNOWN RESORTS a new series may name: (resort, aliases, lat, lon, state,
# tz). The rules' name is matched on its words with the generic ones
# (resort, ski, area, mountain, ...) dropped and "Mt." read as "mount"; an
# alias found as whole words in it, or ("=") the whole of it -- a one-word
# name another area shares (Mt. Brighton, MI; Powder Ridge, CT).
KNOWN_RESORTS = (
    ("Vail", ("vail",), 39.6200, -106.3700, "CO", "US/Mountain"),
    ("Beaver Creek", ("beaver creek",), 39.6042, -106.5165, "CO", "US/Mountain"),
    ("Breckenridge", ("breckenridge",), 39.4800, -106.0667, "CO", "US/Mountain"),
    ("Keystone", ("keystone",), 39.6045, -105.9548, "CO", "US/Mountain"),
    ("Copper Mountain", ("=copper",), 39.5013, -106.1516, "CO", "US/Mountain"),
    ("Arapahoe Basin", ("arapahoe basin", "a basin"), 39.6425, -105.8719, "CO", "US/Mountain"),
    ("Winter Park", ("winter park",), 39.8868, -105.7625, "CO", "US/Mountain"),
    ("Steamboat", ("steamboat",), 40.4572, -106.8045, "CO", "US/Mountain"),
    ("Snowmass", ("snowmass", "aspen snowmass"), 39.2084, -106.9490, "CO", "US/Mountain"),
    ("Aspen Mountain", ("aspen",), 39.1862, -106.8182, "CO", "US/Mountain"),
    ("Aspen Highlands", ("aspen highlands",), 39.1824, -106.8556, "CO", "US/Mountain"),
    ("Crested Butte", ("crested butte",), 38.8991, -106.9653, "CO", "US/Mountain"),
    ("Telluride", ("telluride",), 37.9375, -107.8123, "CO", "US/Mountain"),
    ("Wolf Creek", ("wolf creek",), 37.4747, -106.7936, "CO", "US/Mountain"),
    ("Eldora", ("eldora",), 39.9372, -105.5827, "CO", "US/Mountain"),
    ("Loveland", ("loveland",), 39.6800, -105.8979, "CO", "US/Mountain"),
    ("Purgatory", ("purgatory", "durango"), 37.6303, -107.8140, "CO", "US/Mountain"),
    ("Monarch", ("monarch",), 38.5121, -106.3325, "CO", "US/Mountain"),
    ("Park City", ("park city",), 40.6514, -111.5080, "UT", "US/Mountain"),
    ("Deer Valley", ("deer valley",), 40.6374, -111.4783, "UT", "US/Mountain"),
    ("Alta", ("alta",), 40.5884, -111.6386, "UT", "US/Mountain"),
    ("Snowbird", ("snowbird",), 40.5830, -111.6508, "UT", "US/Mountain"),
    ("Brighton", ("=brighton",), 40.5980, -111.5832, "UT", "US/Mountain"),
    ("Solitude", ("solitude",), 40.6199, -111.5919, "UT", "US/Mountain"),
    ("Snowbasin", ("snowbasin",), 41.2160, -111.8569, "UT", "US/Mountain"),
    ("Powder Mountain", ("=powder",), 41.3790, -111.7807, "UT", "US/Mountain"),
    ("Brian Head", ("brian head",), 37.7021, -112.8499, "UT", "US/Mountain"),
    ("Sundance", ("sundance",), 40.3929, -111.5885, "UT", "US/Mountain"),
    ("Jackson Hole", ("jackson hole",), 43.5875, -110.8279, "WY", "US/Mountain"),
    ("Grand Targhee", ("grand targhee", "targhee"), 43.7903, -110.9580, "WY", "US/Mountain"),
    ("Big Sky", ("big sky",), 45.2857, -111.4012, "MT", "US/Mountain"),
    ("Whitefish", ("whitefish",), 48.4823, -114.3550, "MT", "US/Mountain"),
    ("Bridger Bowl", ("bridger bowl", "bridger"), 45.8174, -110.8966, "MT", "US/Mountain"),
    ("Sun Valley", ("sun valley",), 43.6971, -114.3517, "ID", "US/Mountain"),
    ("Bogus Basin", ("bogus basin",), 43.7640, -116.1020, "ID", "US/Mountain"),
    ("Schweitzer", ("schweitzer",), 48.3678, -116.6228, "ID", "US/Pacific"),
    ("Taos", ("taos",), 36.5960, -105.4545, "NM", "US/Mountain"),
    ("Ski Santa Fe", ("santa fe",), 35.7960, -105.8040, "NM", "US/Mountain"),
    ("Mammoth Mountain", ("mammoth",), 37.6308, -119.0326, "CA", "US/Pacific"),
    ("June Mountain", ("=june",), 37.7678, -119.0907, "CA", "US/Pacific"),
    ("Northstar", ("northstar",), 39.2746, -120.1211, "CA", "US/Pacific"),
    ("Heavenly", ("heavenly",), 38.9353, -119.9400, "CA", "US/Pacific"),
    ("Kirkwood", ("kirkwood",), 38.6850, -120.0652, "CA", "US/Pacific"),
    ("Palisades Tahoe", ("palisades tahoe", "palisades", "squaw valley", "alpine meadows"),
     39.1970, -120.2357, "CA", "US/Pacific"),
    ("Sugar Bowl", ("sugar bowl",), 39.3044, -120.3343, "CA", "US/Pacific"),
    ("Sierra-at-Tahoe", ("sierra tahoe",), 38.7993, -120.0806, "CA", "US/Pacific"),
    ("Big Bear", ("big bear", "snow summit"), 34.2264, -116.8600, "CA", "US/Pacific"),
    ("Mt. Rose", ("mount rose",), 39.3285, -119.8853, "NV", "US/Pacific"),
    ("Diamond Peak", ("diamond peak",), 39.2540, -119.9230, "NV", "US/Pacific"),
    ("Mt. Bachelor", ("mount bachelor",), 43.9792, -121.6886, "OR", "US/Pacific"),
    ("Mt. Hood Meadows", ("mount hood meadows", "hood meadows"), 45.3311, -121.6648, "OR",
     "US/Pacific"),
    ("Crystal Mountain", ("=crystal",), 46.9282, -121.5045, "WA", "US/Pacific"),
    ("Stevens Pass", ("stevens pass",), 47.7448, -121.0890, "WA", "US/Pacific"),
    ("Mt. Baker", ("mount baker",), 48.8570, -121.6650, "WA", "US/Pacific"),
    ("Snoqualmie", ("snoqualmie",), 47.4245, -121.4130, "WA", "US/Pacific"),
    ("Stowe", ("stowe",), 44.5303, -72.7814, "VT", "US/Eastern"),
    ("Killington", ("killington",), 43.6045, -72.8201, "VT", "US/Eastern"),
    ("Okemo", ("okemo",), 43.4017, -72.7170, "VT", "US/Eastern"),
    ("Mount Snow", ("mount snow",), 42.9602, -72.9204, "VT", "US/Eastern"),
    ("Stratton", ("stratton",), 43.1134, -72.9081, "VT", "US/Eastern"),
    ("Sugarbush", ("sugarbush",), 44.1359, -72.8944, "VT", "US/Eastern"),
    ("Jay Peak", ("jay peak",), 44.9379, -72.5045, "VT", "US/Eastern"),
    ("Smugglers' Notch", ("smugglers notch",), 44.5886, -72.7900, "VT", "US/Eastern"),
    ("Loon", ("loon",), 44.0364, -71.6214, "NH", "US/Eastern"),
    ("Bretton Woods", ("bretton woods",), 44.2580, -71.4410, "NH", "US/Eastern"),
    ("Cannon", ("cannon",), 44.1564, -71.6987, "NH", "US/Eastern"),
    ("Waterville Valley", ("waterville valley",), 43.9655, -71.5280, "NH",
     "US/Eastern"),
    ("Attitash", ("attitash",), 44.0826, -71.2290, "NH", "US/Eastern"),
    ("Wildcat", ("wildcat",), 44.2640, -71.2390, "NH", "US/Eastern"),
    ("Sunday River", ("sunday river",), 44.4734, -70.8566, "ME", "US/Eastern"),
    ("Sugarloaf", ("sugarloaf",), 45.0314, -70.3131, "ME", "US/Eastern"),
    ("Whiteface", ("whiteface",), 44.3659, -73.9026, "NY", "US/Eastern"),
    ("Hunter Mountain", ("=hunter",), 42.2020, -74.2250, "NY", "US/Eastern"),
    ("Gore Mountain", ("=gore",), 43.6730, -74.0060, "NY", "US/Eastern"),
    ("Snowshoe", ("snowshoe",), 38.4110, -79.9940, "WV", "US/Eastern"),
    ("Alyeska", ("alyeska",), 60.9700, -149.0981, "AK", "US/Alaska"),
)
_GENERIC = frozenset(("resort", "resorts", "ski", "area", "mountain", "mountains", "the",
                      "at", "and", "california", "colorado", "utah", "vermont"))
_ALIASES: Dict[str, tuple] = {a: row for row in KNOWN_RESORTS for a in row[1]}

_RULES_RE = re.compile(r"total snowfall recorded at (.+?) ([A-Z][a-z]+ \d{1,2}, \d{4}) "
                       r"through ([A-Z][a-z]+ \d{1,2}, \d{4})")
_WET_RE = re.compile(r"(RA|DZ|SN|SG|PL|GR|GS|UP|TS|SH)")


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[resort-snow-fair] {msg}", flush=True)


def norm_place(name: str) -> str:
    """A resort name's words, lower case, without punctuation or the generic
    words, "mt" read as "mount": "Mt. Bachelor Resort" -> "mount bachelor"."""
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("&", " "))
    words = ["mount" if w == "mt" else w for w in s.split()]
    return " ".join(w for w in words if w not in _GENERIC)


def lookup_resort(place: str) -> Optional[dict]:
    """The KNOWN_RESORTS entry the rules' resort name names: the longest
    alias found as whole words in it, None when none is or two resorts tie."""
    name = norm_place(place)
    words = name.split()
    hits = []
    for alias, row in _ALIASES.items():
        if alias.startswith("="):
            if name == alias[1:]:
                hits.append((len(words), row))
            continue
        a = alias.split()
        if any(words[i:i + len(a)] == a for i in range(len(words) - len(a) + 1)):
            hits.append((len(a), row))
    if not hits:
        return None
    top = max(n for n, _ in hits)
    rows = {row for n, row in hits if n == top}
    if len(rows) != 1:
        return None
    resort, _a, lat, lon, state, tz = rows.pop()
    return {"resort": resort, "lat": lat, "lon": lon, "state": state, "tz": tz}


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180.0
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742.0 * math.asin(math.sqrt(a))


def _acis_sid(sids: List[str]) -> str:
    """The station's COOP id, else its GHCN, WBAN or FAA id ("052281 2" ->
    "052281"): DILLON 1 E lists its WBAN 03005 first."""
    by_type: Dict[str, str] = {}
    for s in sids:
        parts = str(s).split()
        if len(parts) == 2:
            by_type.setdefault(parts[1], parts[0])
    for t in ("2", "6", "1", "3"):
        if t in by_type:
            return by_type[t]
    return str(sids[0]).split()[0]


def auto_proxy(lat: float, lon: float, now: datetime) -> dict:
    """The nearest ACIS snowfall gauge (within ~0.6 deg lon x 0.45 deg lat)
    with AUTO_PROXY_MIN_SEASONS+ complete seasons since 2010 -- reporting
    within MAX_REPORT_LAG_DAYS with a record from 2010 first, then
    reporting, then the rest; ValueError when none."""
    today = now.date()
    js = rm.http_json("https://data.rcc-acis.org/StnMeta", post_body={
        "bbox": f"{lon - 0.6:.3f},{lat - 0.45:.3f},{lon + 0.6:.3f},{lat + 0.45:.3f}",
        "elems": "snow", "meta": "name,sids,ll,elev,valid_daterange",
        "sdate": (today - timedelta(days=30)).isoformat(), "edate": today.isoformat()})
    cands = []
    for s in js.get("meta") or []:
        try:
            first, last = (date.fromisoformat(d) for d in s["valid_daterange"][0])
            sid = _acis_sid(s["sids"])
            slon, slat = (float(v) for v in s["ll"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if (today - first).days < 365 * AUTO_PROXY_MIN_SEASONS:
            continue
        tier = (2 if (today - last).days > MAX_REPORT_LAG_DAYS else 0) \
            + (first > date(2010, 7, 1))
        cands.append((tier, _km(lat, lon, slat, slon), sid, str(s.get("name") or sid),
                      s.get("elev")))
    for _tier, km, sid, name, elev in sorted(cands)[:AUTO_PROXY_CANDIDATES]:
        data = rm.http_json("https://data.rcc-acis.org/StnData", post_body={
            "sid": sid, "sdate": "2010-07-01",
            "edate": (today - timedelta(days=1)).isoformat(), "elems": [{"name": "snow"}]})
        hist = {str(d): rm.parse_precip(v) for d, v in data.get("data") or []}
        n = len(season_totals(hist))
        if n >= AUTO_PROXY_MIN_SEASONS:
            return {"acis": sid, "proxy": name, "proxy_km": round(km, 1),
                    "proxy_elev": elev, "proxy_seasons": n}
    raise ValueError(f"no ACIS snowfall gauge nearby with {AUTO_PROXY_MIN_SEASONS}+ "
                     f"complete seasons ({len(cands)} candidates)")


def auto_airport(lat: float, lon: float, state: str,
                 now: Optional[datetime] = None) -> Tuple[str, str, float]:
    """(station, IEM network, km): the nearest station of the state's ASOS
    network (AWOS included) within AUTO_AIRPORT_MAX_KM whose current
    observation is fresh -- the gate reads it every pass."""
    net = f"{state}_ASOS"
    js = rm.cached_get_json(f"iem_network_{net}", 7 * 86400, lambda: rm.http_json(
        f"https://mesonet.agron.iastate.edu/geojson/network/{net}.geojson"))
    near = []
    for f in js.get("features") or []:
        try:
            if (f.get("properties") or {}).get("archive_end"):
                continue                                  # retired
            slon, slat = (float(v) for v in f["geometry"]["coordinates"][:2])
            sid = str(f["id"])
        except (KeyError, TypeError, ValueError):
            continue
        km = _km(lat, lon, slat, slon)
        if km <= AUTO_AIRPORT_MAX_KM:
            near.append((km, sid))
    for km, sid in sorted(near)[:5]:
        ob = obs_state({"iem": sid, "net": net}, now or datetime.now(timezone.utc))
        if ob["wet"] is not None:
            return sid, net, round(km, 1)
    raise ValueError(f"no {net} station within {AUTO_AIRPORT_MAX_KM:g} km reports now "
                     f"({len(near)} near)")


def auto_spec(series: str, place: str, now: datetime) -> dict:
    """A RESORTS-shaped spec for a new resort (no season average or LEVEL:
    its scale is fitted per event); ValueError when it cannot be mapped."""
    r = lookup_resort(place)
    if r is None:
        raise ValueError(f"{place!r} is not in KNOWN_RESORTS")
    px = auto_proxy(r["lat"], r["lon"], now)
    iem, net, akm = auto_airport(r["lat"], r["lon"], r["state"], now)
    return {"place": place, "name": place, "resort": r["resort"], "lat": r["lat"],
            "lon": r["lon"], "tz": r["tz"], "acis": px["acis"], "icao": px["acis"],
            "proxy": px["proxy"], "proxy_km": px["proxy_km"],
            "proxy_seasons": px["proxy_seasons"], "iem": iem, "net": net,
            "airport_km": akm, "auto": True}


def book_median(strikes: Dict[str, float], books: Dict[str, List[int]]) -> Optional[float]:
    """The total at which the book's YES mids cross 50% (linear between the
    two strikes around it, usable books only); None off the ladder."""
    pts = sorted((k, (books[t][0] + books[t][1]) / 200.0) for t, k in strikes.items()
                 if books.get(t) and books[t][1] - books[t][0] <= ANCHOR_MAX_SPREAD_C)
    for (k1, m1), (k2, m2) in zip(pts, pts[1:]):
        if m1 >= 0.5 > m2 and k2 > k1:
            return k1 + (m1 - 0.5) / (m1 - m2) * (k2 - k1)
    return None


def fit_scale(key: str, start: date, end: date, now: datetime,
              hist: Dict[str, Optional[float]], raw_to_date: float, target: float) -> float:
    """The proxy scale that puts the model's median season total on `target`:
    geometric bisection on [FIT_LO, FIT_HI] over FIT_SAMPLES draws on one
    seed (each draw's total rises with the scale, so its median does);
    ValueError when no scale in the range lands within 10%."""
    def median_at(s: float) -> float:
        t = rmf.simulate_period(key, start, end, now, n_samples=FIT_SAMPLES,
                                rng=random.Random(FIT_SEED), hist=hist,
                                element="snowfallAmount", kernel_sigma=KERNEL_SIGMA,
                                hist_scale=s)["totals"]
        return raw_to_date * s + (t[len(t) // 2] if t else 0.0)
    lo, hi = FIT_LO, FIT_HI
    for _ in range(FIT_STEPS):
        mid = math.sqrt(lo * hi)
        if median_at(mid) < target:
            lo = mid
        else:
            hi = mid
    s = math.sqrt(lo * hi)
    got = median_at(s)
    if abs(got - target) > 0.1 * max(target, 1.0):
        raise ValueError(f"no scale in [{FIT_LO:g}, {FIT_HI:g}] puts the median on "
                         f"{target:.1f}\" (got {got:.1f}\")")
    return s


def parse_rules(rules: str) -> Optional[Tuple[str, date, date]]:
    """(resort, first day, last day) from a market's rules, else None."""
    m = _RULES_RE.search(rules or "")
    if not m:
        return None
    try:
        a = datetime.strptime(m.group(2), "%B %d, %Y").date()
        b = datetime.strptime(m.group(3), "%B %d, %Y").date()
    except ValueError:
        return None
    return m.group(1).strip(), a, b


def book_cents(m: dict) -> Optional[List[int]]:
    """[YES bid, YES ask] in cents from a Kalshi market, or None for a
    one-sided, empty or crossed book."""
    out = []
    for kd, kc in (("yes_bid_dollars", "yes_bid"), ("yes_ask_dollars", "yes_ask")):
        v = m.get(kd)
        try:
            out.append(int(round(float(v) * 100)) if v is not None else int(m.get(kc)))
        except (TypeError, ValueError):
            return None
    return out if 0 < out[0] < out[1] < 100 else None


def fetch_events(get_json: Optional[Callable] = None,
                 series: Optional[Tuple[str, ...]] = None) -> Dict[str, dict]:
    """Open resort events: event -> {"series", "code" (the series when its
    rules read as a resort window -- for one of the six, ITS resort -- else
    None), "place" (the rules' resort), "start", "end", "strikes": {ticker:
    K}, "books": {ticker: [bid, ask]}}; markets that disagree on the resort
    or the window leave no code."""
    out: Dict[str, dict] = {}
    for i, s in enumerate(series or SERIES):
        if i:
            # six reads back to back drew a 429 on the public endpoint (10/22)
            time.sleep(0.3 if get_json is not None else 1.5)
        js = rmf._signed_or_public(get_json, {"series_ticker": s, "status": "open",
                                              "limit": 200})
        for m in js.get("markets") or []:
            ev, t = str(m.get("event_ticker") or ""), str(m.get("ticker") or "")
            try:
                k = float(m.get("floor_strike"))
            except (TypeError, ValueError):
                continue
            if not ev or not t or str(m.get("strike_type") or "greater") != "greater":
                continue
            r = parse_rules(str(m.get("rules_primary") or ""))
            spec = RESORTS.get(s)
            ok = bool(r and (r[0] == spec["place"] if spec else True))
            e = out.setdefault(ev, {"series": s, "code": s if ok else None,
                                    "place": r[0] if r else None,
                                    "start": r[1].isoformat() if r else None,
                                    "end": r[2].isoformat() if r else None,
                                    "strikes": {}, "books": {}})
            if not ok or e["place"] != (r[0] if r else None) \
                    or e["start"] != (r[1].isoformat() if r else None) \
                    or e["end"] != (r[2].isoformat() if r else None):
                e["code"] = None
            e["strikes"][t] = k
            bk = book_cents(m)
            if bk:
                e["books"][t] = bk
    return out


def season_totals(hist: Dict[str, Optional[float]]) -> Dict[int, float]:
    """Proxy season totals (Jul 1 - Jun 30, keyed by the starting year) over
    complete seasons: at most 5% of the days missing."""
    sums: Dict[int, List[float]] = {}
    miss: Dict[int, int] = {}
    for d, v in hist.items():
        y, mo = int(d[:4]), int(d[5:7])
        season = y if mo >= 7 else y - 1
        sums.setdefault(season, [])
        if v is None:
            miss[season] = miss.get(season, 0) + 1
        else:
            sums[season].append(v)
    out = {}
    for season, vals in sums.items():
        days = len(vals) + miss.get(season, 0)
        if days >= 360 and miss.get(season, 0) <= 0.05 * days:
            out[season] = sum(vals)
    return out


def proxy_scale(spec: dict, hist: Dict[str, Optional[float]]) -> Tuple[float, float, int]:
    """(scale, the proxy's average season, seasons used): the resort's
    average season over the proxy's, the proxy averaged over complete seasons
    since SCALE_FROM_SEASON (all of them when fewer than MIN_SEASONS), x the
    resort's LEVEL."""
    tot = season_totals(hist)
    recent = {s: v for s, v in tot.items() if s >= SCALE_FROM_SEASON}
    use = recent if len(recent) >= MIN_SEASONS else tot
    if not use:
        raise ValueError("no complete proxy season")
    avg = sum(use.values()) / len(use)
    if avg <= 0:
        raise ValueError("proxy average season is zero")
    return spec["season_avg"] / avg * float(spec.get("level") or 1.0), avg, len(use)


def recent_reports(key: str, start: date, today: date) -> Dict[str, Optional[float]]:
    """The proxy's daily snowfall from `start` through `today` (ACIS),
    re-read every RECENT_TTL_SECS."""
    st = rm.STATIONS[key]
    data = rm.cached_get_json(
        f"acis_snowrecent_{key}", RECENT_TTL_SECS,
        lambda: rm.http_json("https://data.rcc-acis.org/StnData", post_body={
            "sid": st.get("acis") or st["icao"], "sdate": start.isoformat(),
            "edate": today.isoformat(), "elems": [{"name": "snow"}]}))
    return {str(d): rm.parse_precip(v) for d, v in data.get("data") or []}


def proxy_to_date(hist: Dict[str, Optional[float]], start: date, now: datetime,
                  tz) -> Tuple[float, Optional[str], int]:
    """(proxy snowfall from `start` through its latest report -- today's
    counts once it is in -- that report's day, the past days with no
    report)."""
    today = now.astimezone(tz).date()
    total, latest, missing = 0.0, None, 0
    d = start
    while d <= today:
        v = hist.get(d.isoformat())
        if v is None:
            missing += d < today
        else:
            total += v
            latest = d.isoformat()
        d += timedelta(days=1)
    return total, latest, missing


def obs_state(spec: dict, now: datetime) -> dict:
    """The airport's current observation, "wet" = ANY precipitation (the
    present weather or the hour's total): rain in the valley is snow up the
    mountain. None = no observation younger than OBS_MAX_AGE_MIN."""
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
    wet = bool(_WET_RE.search(wx) or (phour or 0.0) > 0.0)
    return {"wet": wet, "obs_time": t.isoformat(), "phour": phour, "wx": wx}


def _utc(iso: Optional[str]) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def event_fair(event: str, info: dict, now: datetime, n_samples: int = N_SAMPLES,
               rng: Optional[random.Random] = None) -> Optional[dict]:
    """The model's per-market P(season total > K) ("p", before the anchor)
    and the to-date state; None when the resort, the window or the strikes
    are unknown, or the window is over. The resort is info["spec"] (a new
    resort's auto_spec) or the six's RESORTS row; a spec without a season
    average takes its scale from info["fit"], else fits one on the event's
    books (fit_scale) and returns it as "fit" -- ValueError without a book
    that straddles 50%."""
    spec = info.get("spec") or RESORTS.get(info.get("code") or "")
    try:
        start = date.fromisoformat(str(info["start"]))
        end = date.fromisoformat(str(info["end"]))
    except (KeyError, TypeError, ValueError):
        return None
    if spec is None or not info.get("strikes"):
        return None
    tz = pytz.timezone(spec["tz"])
    today = now.astimezone(tz).date()
    if today > end:
        return None
    key = f"RSNOW{info['code']}"
    rm.STATIONS[key] = dict(spec, series=info.get("series", ""))
    hist = rmf.period_history(key, now, elem="snow")
    notes: List[str] = []
    stale, why = False, None
    raw, latest, missing = 0.0, None, 0
    if today < start:
        notes.append("window not started")
    else:
        seen = dict(hist)
        try:
            seen.update((d, v) for d, v in recent_reports(key, start, today).items()
                        if v is not None)
        except RuntimeError:
            notes.append(f"{spec['proxy']}'s recent reports unreadable; daily history only")
        raw, latest, missing = proxy_to_date(seen, start, now, tz)
    fit = info.get("fit")
    if spec.get("season_avg"):
        scale, proxy_avg, n_seasons = proxy_scale(spec, hist)
    else:
        if not fit:
            target = book_median(info["strikes"], info.get("books") or {})
            if target is None:
                raise ValueError("no usable book straddles 50% to fit the scale on")
            fit = {"scale": round(fit_scale(key, start, end, now, hist, raw, target), 4),
                   "book_median": round(target, 1), "at": now.isoformat()}
            notes.append(f"scale fitted to the book's median {target:.1f}\"")
        scale = float(fit["scale"])
        tot = season_totals(hist)
        proxy_avg, n_seasons = (sum(tot.values()) / len(tot) if tot else 0.0), len(tot)
    mtd = raw * scale
    if today >= start:
        if latest is None:
            stale, why = True, f"no {spec['proxy']} report since the window opened"
        else:
            lag = (today - date.fromisoformat(latest)).days
            if lag > MAX_REPORT_LAG_DAYS:
                stale, why = True, f"{spec['proxy']}'s latest report is {lag} days old"
            wet_at = _utc(info.get("last_wet_at")) if info.get("last_wet_at") else None
            cover = tz.localize(datetime.combine(date.fromisoformat(latest),
                                                 dtime(REPORT_HOUR)))
            if not stale and wet_at is not None and wet_at > cover:
                stale, why = True, (
                    f"precipitation at {spec['iem']} "
                    f"{wet_at.astimezone(tz).strftime('%m/%d %H:%M')} after "
                    f"{spec['proxy']}'s latest report ({latest}, through "
                    f"~{REPORT_HOUR}:00)")
        if missing:
            notes.append(f"{missing} day(s) without a {spec['proxy']} report, counted 0")
    sim = rmf.simulate_period(key, start, end, now, n_samples=n_samples,
                              rng=rng or random.Random(), hist=hist,
                              element="snowfallAmount", kernel_sigma=KERNEL_SIGMA,
                              hist_scale=scale)
    strikes = info["strikes"]
    p = rm.price_rungs(mtd, sim["totals"], sorted(set(strikes.values())))
    totals = sim["totals"]
    med = totals[len(totals) // 2] + mtd if totals else mtd
    return {"p": {t: round(p[k], 4) for t, k in strikes.items()}, "boundary": [],
            "mtd": round(mtd, 2), "cli_mtd": None, "cli_date": latest,
            "cli_issued": None, "stale_cli": stale, "stale_why": why, "pday": None,
            "notes": notes, "horizon_days": sim["horizon_days"],
            "n_segments": sim["n_segments"], "in_period": today >= start,
            "scale": round(scale, 3), "proxy_avg": round(proxy_avg, 1),
            "scale_seasons": n_seasons, "median_total": round(med, 1), "fit": fit}


def _logit(p: float) -> float:
    p = min(1.0 - P_FLOOR, max(P_FLOOR, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def anchor_offsets(strikes: Dict[str, float], p_model: Dict[str, float],
                   books: Dict[str, List[int]], old: Dict[str, dict], ts: float,
                   calm: bool) -> Tuple[Dict[str, float], Dict[str, dict]]:
    """(log-odds offset per strike for the fair, the anchors to keep). A
    strike with a usable book while `calm` steps its anchor toward
    logit(mid) - logit(model): all the way on its first read, else by
    1 - exp(-dt / ANCHOR_TAU_HOURS), dt the hours since its last step capped
    at ANCHOR_MAX_STEP_HOURS. Any other strike keeps its anchor. A strike
    still without one borrows its neighbours' offsets (linear in the strike
    between the nearest two, the nearest one past either end); no anchored
    strike -> no offsets."""
    keep: Dict[str, dict] = {}
    for t, p in p_model.items():
        a = old.get(t)
        bk = books.get(t) if calm else None
        if bk and bk[1] - bk[0] <= ANCHOR_MAX_SPREAD_C:
            mid = (bk[0] + bk[1]) / 200.0
            target = max(-ANCHOR_MAX_OFFSET,
                         min(ANCHOR_MAX_OFFSET, _logit(mid) - _logit(p)))
            if a is None:
                off = target
            else:
                dt = min(ANCHOR_MAX_STEP_HOURS, max(0.0, (ts - float(a["ts"])) / 3600.0))
                off = float(a["off"]) + (1.0 - math.exp(-dt / ANCHOR_TAU_HOURS)) * (
                    target - float(a["off"]))
            a = {"off": round(off, 4), "ts": ts, "mid": round(mid * 100, 1),
                 "p_model": round(p, 4)}
        if a is not None:
            keep[t] = a
    known = sorted((strikes[t], float(a["off"])) for t, a in keep.items() if t in strikes)
    offs: Dict[str, float] = {}
    if not known:
        return offs, keep
    for t, k in strikes.items():
        if t in keep:
            offs[t] = float(keep[t]["off"])
            continue
        lo = [x for x in known if x[0] < k]
        hi = [x for x in known if x[0] > k]
        if lo and hi:
            (k0, o0), (k1, o1) = lo[-1], hi[0]
            offs[t] = o0 + (o1 - o0) * (k - k0) / (k1 - k0)
        else:
            offs[t] = (lo[-1] if lo else hi[0])[1]
    return offs, keep


def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    events: Optional[Dict[str, dict]] = None,
                    fair_fn: Callable = event_fair,
                    obs_fn: Callable = obs_state,
                    extra_series: Tuple[str, ...] = (),
                    map_fn: Callable = auto_spec) -> Tuple[int, int]:
    """Build and atomically write the resort fair file; returns (events with
    a fair, events without one). Kalshi's open events and their books are
    re-read hourly, and at once when `extra_series` (the bot's feed) names a
    series the last read did not (a failed read reuses the previous file's
    events; only a fresh read -- or `events` handed in -- steps the
    anchors). A series outside the six is mapped by its rules' resort
    (map_fn: auto_spec), once, kept in the file's "auto_map" (a failure is
    tried again after AUTO_MAP_RETRY_SECS); each such event's fitted scale
    rides in "fits". The last wet observation per event and every strike's
    anchor survive across writes. An event before its window reads dry.
    "mapped" lists the series with a resort -- what the bot enrolls."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = rmf._read_json(path)
    ev_cache = old.get("events") or {}
    events_at = old.get("events_at")
    series_all = tuple(SERIES) + tuple(s for s in sorted(set(extra_series))
                                       if s not in SERIES and SERIES_RE.fullmatch(s)
                                       and "MENTION" not in s)
    read_series = old.get("events_series") or list(SERIES)
    fresh = events is not None
    if events is None:
        if ev_cache and ts - float(events_at or 0) < EVENTS_TTL_SECS \
                and not set(series_all) - set(read_series):
            events = ev_cache
        else:
            try:
                events = fetch_events(get_json, series=series_all)
                events_at = ts
                read_series = list(series_all)
                fresh = True
            except Exception as e:
                if not ev_cache:
                    raise
                _log(f"! Kalshi event read failed ({type(e).__name__}); "
                     f"reusing {len(ev_cache)} cached")
                events = ev_cache
    old_ev = old.get("event_state") or {}
    old_anchors = old.get("anchors") or {}
    old_auto = old.get("auto_map") or {}
    old_fits = old.get("fits") or {}
    auto_map: Dict[str, dict] = {}
    fits: Dict[str, dict] = {}
    anchors: Dict[str, dict] = {}
    markets: Dict[str, dict] = {}
    state: Dict[str, dict] = {}
    missing: List[str] = []
    mapped: Dict[str, dict] = {s: {"place": r["place"], "proxy": r["proxy"], "iem": r["iem"],
                                   "auto": False} for s, r in RESORTS.items()}
    unmapped: Dict[str, str] = {}

    def resort_spec(code: Optional[str], place: Optional[str]) -> Optional[dict]:
        if not code:
            return None
        if code in RESORTS:
            return RESORTS[code]
        a = auto_map.get(code) or old_auto.get(code)
        if not (a and a.get("place") == place
                and (a.get("spec") or ts - float(a.get("at") or 0) < AUTO_MAP_RETRY_SECS)):
            try:
                a = {"place": place, "at": ts, "spec": map_fn(code, str(place), now)}
                sp = a["spec"]
                _log(f"{code}: mapped {place} -> proxy {sp['proxy']} ({sp.get('proxy_km')} km, "
                     f"{sp.get('proxy_seasons')} seasons), airport {sp['iem']} "
                     f"({sp.get('airport_km')} km)")
            except Exception as e:
                a = {"place": place, "at": ts, "error": f"{type(e).__name__}: {str(e)[:160]}"}
                _log(f"! {code}: {place} not mapped -- {a['error']}")
        auto_map[code] = a
        if a.get("spec"):
            mapped[code] = {"place": place, "proxy": a["spec"]["proxy"],
                            "iem": a["spec"]["iem"], "auto": True}
        else:
            unmapped[code] = f"{place}: {a.get('error')}"
        return a.get("spec")

    for ev, info in sorted(events.items()):
        spec = resort_spec(info.get("code"), info.get("place"))
        st = {"code": info.get("code"), "fetched_at": now.isoformat(),
              "last_wet_at": (old_ev.get(ev) or {}).get("last_wet_at")}
        try:
            before = bool(spec) and now.astimezone(pytz.timezone(spec["tz"])).date() \
                < date.fromisoformat(str(info.get("start")))
        except ValueError:
            before = False
        if before:
            st.update(wet=False, obs_time=None, phour=None, wx=None)
        elif spec is not None:
            ob = obs_fn(spec, now)
            st.update(wet=ob["wet"], obs_time=ob["obs_time"], phour=ob["phour"], wx=ob["wx"])
            if ob["wet"]:
                st["last_wet_at"] = ob["obs_time"]
        try:
            f = fair_fn(ev, dict(info, last_wet_at=st["last_wet_at"], spec=spec,
                                 fit=old_fits.get(ev)), now) \
                if spec is not None else None
        except Exception as e:
            _log(f"! {ev}: {type(e).__name__}: {str(e)[:120]}")
            st["why"] = f"{type(e).__name__}: {str(e)[:160]}"
            f = None
        if f is None:
            # its anchors and fit wait for the fair to come back
            anchors.update((t, a) for t, a in old_anchors.items()
                           if t in (info.get("strikes") or {}))
            if ev in old_fits:
                fits[ev] = old_fits[ev]
            missing.append(ev)
            st["error"] = "no resort" if spec is None else "no fair"
            state[ev] = st
            continue
        if f.get("fit"):
            fits[ev] = f["fit"]
        st.update({k: f.get(k) for k in ("mtd", "cli_date", "stale_cli", "stale_why",
                                         "notes", "horizon_days", "n_segments",
                                         "in_period", "scale", "proxy_avg",
                                         "scale_seasons", "median_total")})
        lw = _utc(st["last_wet_at"]) if st["last_wet_at"] else None
        calm = (st.get("wet") is False and not f.get("stale_cli")
                and (lw is None or (now - lw).total_seconds() >= DRY_MIN * 60))
        offs, keep = anchor_offsets(info["strikes"], f["p"],
                                    (info.get("books") or {}) if fresh else {},
                                    old_anchors, ts, calm)
        anchors.update(keep)
        st["anchored"] = len(keep)
        if not offs:
            missing.append(ev)
            st["error"] = "no anchor yet"
            state[ev] = st
            continue
        state[ev] = st
        for t, p in f["p"].items():
            if t not in offs:
                continue
            markets[t] = {"event": ev, "p": round(_sigmoid(_logit(p) + offs[t]), 4),
                          "p_model": p, "offset": round(offs[t], 3),
                          "anchored": t in keep, "boundary": False,
                          "fetched_at": now.isoformat()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": now.isoformat(), "markets": markets,
                   "event_state": state, "missing": missing, "events": events,
                   "events_at": events_at, "events_series": read_series,
                   "anchors": anchors, "auto_map": auto_map, "fits": fits,
                   "mapped": mapped, "unmapped": unmapped,
                   "model": {"samples": N_SAMPLES, "kernel_sigma": KERNEL_SIGMA,
                             "scale_from_season": SCALE_FROM_SEASON,
                             "max_report_lag_days": MAX_REPORT_LAG_DAYS,
                             "report_hour": REPORT_HOUR,
                             "forecast_weight": rm.FORECAST_WEIGHT,
                             "anchor_tau_hours": ANCHOR_TAU_HOURS,
                             "anchor_max_spread_c": ANCHOR_MAX_SPREAD_C,
                             "resorts": {s: {k: r[k] for k in ("place", "proxy", "acis",
                                                               "season_avg", "level", "iem")}
                                         for s, r in RESORTS.items()}}},
                  fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return len(events) - len(missing), len(missing)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "resort_snow_fair.json"))
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    _log(f"{ok} events with a fair, {miss} without -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
