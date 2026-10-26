#!/usr/bin/env python3
"""USGS + GFZ earthquake feeds for incentive_mm's KXBIGGESTQUAKE bid-only gate.

KXBIGGESTQUAKE-<DDMMMYY>-<K> ("If the highest USGS-reported earthquake
magnitude worldwide during Sep 27, 2026 from 12:00:00 AM through 11:59:59 PM
UTC is 5.2 or higher, then the market resolves to Yes") -- ten strikes K =
5.2 .. 7.0 in 0.2 steps, one event per UTC day, open 20:00Z the day before,
close 23:59:59Z. Measured 2026-09-27 over every settled market (204): the
outcome is the USGS-DISPLAYED daily maximum when Kalshi checks (a crossed
strike closes at the next :14/:29/:44/:59 check and settles YES at once).

Why BID-ONLY (Jack 2026-09-27: "yes build the USGS bid-only gate"): a quake
can only make YES worth more, so a resting YES bid is never on the wrong
side of the news, while resting YES asks were lifted within ~12 s of USGS
publishing on 24 of 62 crossed strikes (and before USGS, off faster feeds,
on 22 more). The bot rests bids only, priced at most QUAKE_MARGIN_CENTS
under the model's fair value -- except on the far strikes (Jack 2026-10-11,
IMM_QUAKE_ASK_STRIKES), where a small ask may also rest while
QuakeWatch.ask_verdict allows it (see ASK_MARGIN_CENTS).

Fair value, for a strike not yet crossed:
    P(day max >= K) = 1 - exp(-lam(K) * W / 24)
lam(K) = -ln(1 - P_DAY(K)), P_DAY the empirical 10-year frequency of UTC
days whose maximum reached K (USGS ComCat 2016-2026, 3,650 days --
aftershock clustering included). W = hours of the day still able to add a
qualifying quake that is not on the feed yet: the rest of the day PLUS the
unpublished tail (USGS publishes a median 17.4 min after origin for M5.5+,
GFZ a median 5.3 min), clipped to the day. Before 00:00Z it is the whole day.
The live ladder sat within 1c of this model on 5.6-6.4 on 9/27.
Since 2026-10-26 the bids price max(that fair, an aftershock fair) -- a
Reasenberg-Jones term over the last year's M5.5+ quakes (AS_MODE, raise-only).

Feeds (both official, polled from incentive_mm's refresher thread):
  USGS  earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson
        (public domain; regenerated every minute, cached up to 60 s)
  GFZ   geofon.gfz.de/fdsnws/event/1/query (the GEOFON FDSN event service;
        earthquake products CC BY 4.0 -- source: GEOFON data centre, GFZ
        Helmholtz Centre for Geosciences)
  EMSC  wss://www.seismicportal.eu/standing_order/websocket (pushed, read on
        its own thread; far-strike asks only -- see EMSC_MIN_MAG)

The EARLY WARNING (Jack 2026-09-27: "also use GFZ data"): a new M >= 4.8
detection on either feed FREEZES the quake markets -- resting bids are held
as they are, never raised on the news -- until USGS shows the quake with
NEIC's own solution for QUAKE_CONFIRM_SECS (or, for a regional network that
stays authoritative, a stable magnitude once the quake is NEIC_WAIT_MIN
old), for at most FREEZE_MAX_MIN per detection. A stale GFZ feed freezes the
same way (bounded by FREEZE_MAX_MIN, then USGS-only pricing). A stale USGS
feed stands every quake market down (the gate fails closed).
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

USGS_URL = os.environ.get(
    "IMM_QUAKE_USGS_URL",
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson")
GFZ_URL = os.environ.get(
    "IMM_QUAKE_GFZ_URL", "https://geofon.gfz.de/fdsnws/event/1/query")
# EMSC's realtime push feed (seismicportal.eu): one JSON message per new or
# revised event, the moment EMSC publishes it -- often minutes before GFZ.
EMSC_WS_URL = os.environ.get(
    "IMM_QUAKE_EMSC_URL", "wss://www.seismicportal.eu/standing_order/websocket")
HTTP_TIMEOUT = 20
USER_AGENT = "KL-IMM-quake-gate/1.0"

# P(UTC-day maximum >= K), USGS ComCat 2016-09-27 .. 2026-09-26 (3,650 days)
KS = (5.2, 5.4, 5.6, 5.8, 6.0, 6.2, 6.4, 6.6, 6.8, 7.0)
P_DAY = (0.890411, 0.743562, 0.570411, 0.411507, 0.283836, 0.184384,
         0.121918, 0.080548, 0.056164, 0.034521)
# how far past either end of the table lam(K) is extrapolated (G-R slope of
# the end segment); farther strikes get no fair -> the gate stands them down
EXTRAP_MAG = 0.4


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# unpublished tail, hours: USGS alone vs a fresh GFZ feed in front of it
USGS_LAG_H = _env_float("IMM_QUAKE_USGS_LAG_H", 0.3)
GFZ_LAG_H = _env_float("IMM_QUAKE_GFZ_LAG_H", 0.15)
FREEZE_MIN_MAG = _env_float("IMM_QUAKE_FREEZE_MIN_MAG", 4.8)
CONFIRM_SECS = _env_float("IMM_QUAKE_CONFIRM_SECS", 120)
NEIC_WAIT_MIN = _env_float("IMM_QUAKE_NEIC_WAIT_MIN", 25)
FREEZE_MAX_MIN = _env_float("IMM_QUAKE_FREEZE_MAX_MIN", 45)
# a GFZ event and a USGS event are the same quake when their origins sit
# within this many seconds (GFZ vs USGS origin time: a few seconds apart)
MATCH_SECS = _env_float("IMM_QUAKE_MATCH_SECS", 90)
USGS_STALE_SECS = _env_float("IMM_QUAKE_USGS_STALE_SECS", 180)
# the feed's own generation stamp (a CDN serving an old copy)
USGS_GEN_STALE_SECS = _env_float("IMM_QUAKE_USGS_GEN_STALE_SECS", 300)
GFZ_STALE_SECS = _env_float("IMM_QUAKE_GFZ_STALE_SECS", 120)
# GFZ query window (and how long a detection is remembered)
GFZ_LOOKBACK_MIN = _env_float("IMM_QUAKE_GFZ_LOOKBACK_MIN", 180)
# a detection is NEWS only while the quake itself is this recent (so a
# restart does not freeze on the past day's feed)
NEWS_MAX_AGE_MIN = _env_float("IMM_QUAKE_NEWS_MAX_AGE_MIN", 105)
# FREEZE CLOCK (Jack 2026-10-07, after KXBIGGESTQUAKE-07OCT26 sat frozen on
# an unconfirmed GFZ M5.08): FREEZE_MAX_MIN runs from when the watch FIRST
# SAW a detection, and that lives in memory -- the 00:46Z and 00:56Z IMM
# restarts each re-saw gfz2026tpui (origin 00:30Z) as new and pushed the
# freeze from ~01:15Z to ~01:42Z. The clock now starts no later than
# FIRST_SEEN_MAX_LAG_MIN after the quake's origin (GFZ and USGS publish
# well inside it), so a live process is unchanged and a restart cannot
# extend a freeze past origin + FIRST_SEEN_MAX_LAG_MIN + FREEZE_MAX_MIN.
FIRST_SEEN_MAX_LAG_MIN = _env_float("IMM_QUAKE_FIRST_SEEN_MAX_LAG_MIN", 20)
# ASKS ON THE FAR STRIKES (Jack 2026-10-11: "yes GFZ-triggered cancel", "yes
# start at the far strikes"). A resting YES ask (a NO bid) is the order a
# quake picks off, so QuakeWatch.ask_verdict lets one rest only while BOTH
# feeds are fresh and no quake near the strike is in sight. Per strike K,
# asks are OFF while:
#   - USGS or GFZ is stale (no GFZ = no early warning; that IS the trigger);
#   - today's USGS maximum, or a GFZ event inside the day, reached
#     K - ASK_DAYMAX_GAP_MAG (a later magnitude revision could cross K);
#   - a quake of K - ASK_PULL_BELOW_MAG or more (either feed) was first seen
#     within ASK_COOLOFF_MIN, or is still waiting for NEIC's solution (first
#     magnitudes run low, and a foreshock can come first).
# Otherwise an ask may rest no lower than the fair + ASK_MARGIN_CENTS, the
# fair taken with GFZ's unpublished tail (GFZ_LAG_H). The bot's quake
# refresher pulls resting asks the moment the verdict turns off -- every
# poll and every second while it stays off, off the trading thread.
ASK_MARGIN_CENTS = _env_float("IMM_QUAKE_ASK_MARGIN_CENTS", 1.0)
ASK_PULL_BELOW_MAG = _env_float("IMM_QUAKE_ASK_PULL_BELOW_MAG", 0.8)
ASK_DAYMAX_GAP_MAG = _env_float("IMM_QUAKE_ASK_DAYMAX_GAP_MAG", 0.4)
ASK_COOLOFF_MIN = _env_float("IMM_QUAKE_ASK_COOLOFF_MIN", 30)
# EMSC, THE THIRD TRIGGER (Jack 2026-10-11: "poll GFZ every 10s instead of
# 20s, or add the faster EMSC feed" -> "implement"). EMSC's realtime
# websocket pushes each event as EMSC publishes it (update_emsc); its quakes
# count in ask_verdict exactly like GFZ's (detection / pending / near the day
# max). It is an ACCELERATOR only: a dead EMSC connection never turns an ask
# off (GFZ + USGS stay the requirement) and it never holds the bids. Events
# under EMSC_MIN_MAG are not kept; kept ones are dropped EMSC_KEEP_MIN after
# their origin.
EMSC_MIN_MAG = _env_float("IMM_QUAKE_EMSC_MIN_MAG", 4.8)
EMSC_KEEP_MIN = _env_float("IMM_QUAKE_EMSC_KEEP_MIN", 180)
# THE AFTERSHOCK TERM, RAISE-ONLY (Jack 2026-10-26: "Yes build and then go live
# and monitor it", on the 10/15-16 backtest, quake_aftershock/REPORT.md in the
# side-session notes). Since ~10/08 other makers have bid YES 3-16c over the
# 10-year table on every strike after each big quake (10/26: two M7.0 Bonin,
# an M7.0 China, an M6.9 Vanuatu in four days), so our fair - 1c bids sat
# outside the scored walk and earned $0. The backtest's Reasenberg-Jones term
#     N = 10^(a + b(Ms - K)) * integral of (t + c)^-p over the rest of the day
# summed over every M >= AS_MMIN quake of the last AS_WINDOW_D days
# (aftershocks are sources too), each counted AS_SRC_LAG_H after its origin,
# on a background LAM_BG re-solved so the 10-year mean still matches the
# table: fair_as = 1 - exp(-(lam_bg(K) * w + N)). Fitted 1990-2016, tested
# 2016-2026: +342 nats over the table (P(gain) 97%), the gain on post-big-quake
# days, where the table under-forecasts 5.8-6.0 by ~10-30 points. Used RAISE-
# ONLY -- the bid's fair is max(table, aftershock), so quiet days are exactly
# as before (a full swap cost ~$9/day there) -- it put our cap at the reward
# reference on 37% of 10/10-10/15's live strike-hours instead of 14%: about
# +$4.8/day in that sequence at 120 lots, ~$0.5/day over 2016-2026, fills at
# the raised cap still +0.6 to +3.4c of edge. A catalog older than
# AS_STALE_SECS (or none yet) falls back to the table: never fails upward.
# IMM_QUAKE_AFTERSHOCK = off | shadow (computed and logged, not used) | raise.
AS_MODE = os.environ.get("IMM_QUAKE_AFTERSHOCK", "raise").strip().lower()
AS_B = 0.9                       # alpha = b: the textbook form
AS_P = 1.08
AS_C = 0.05                      # days
AS_MMIN = _env_float("IMM_QUAKE_AS_MMIN", 5.5)
AS_WINDOW_D = _env_float("IMM_QUAKE_AS_WINDOW_D", 365.0)
AS_SRC_LAG_H = _env_float("IMM_QUAKE_AS_SRC_LAG_H", 0.3)
AS_STALE_SECS = _env_float("IMM_QUAKE_AS_STALE_SECS", 3 * 3600)
# one quake under two catalog ids counts once: sources whose origins sit
# within AS_DEDUP_SECS merge, the larger magnitude kept (ComCat listed the
# 10/24 02:09:00Z Bonin M7.0 as pt26297050 AND pt26298050 -- same second,
# place and depth -- which would double its term)
AS_DEDUP_SECS = _env_float("IMM_QUAKE_AS_DEDUP_SECS", 60)
# lam_bg per strike (KS order), solved with the term on over 2016-09-27 ..
# 2026-09-26 -- one table per validated a (the report's section 6): -2.35 the
# fit, -2.55 the cautious end. IMM_QUAKE_AS_A picks one; any other value
# falls back to -2.35.
LAM_BG_BY_A = {
    -2.35: (1.9657, 1.1973, 0.7375, 0.4578, 0.2857, 0.1716,
            0.1088, 0.0699, 0.0484, 0.0289),
    -2.55: (2.0482, 1.2536, 0.7756, 0.4834, 0.3029, 0.1832,
            0.1165, 0.0750, 0.0519, 0.0312)}
AS_A = _env_float("IMM_QUAKE_AS_A", -2.35)
if AS_A not in LAM_BG_BY_A:
    AS_A = -2.35
LAM_BG = LAM_BG_BY_A[AS_A]
# the year of source quakes: USGS ComCat's FDSN event service
AS_CATALOG_URL = os.environ.get(
    "IMM_QUAKE_AS_CATALOG_URL", "https://earthquake.usgs.gov/fdsnws/event/1/query")


def _loglin(k: float, lams) -> Optional[float]:
    """A daily rate at magnitude k off a per-strike table (KS order): exact at
    the strikes, log-linear between them, the end segment's slope for up to
    EXTRAP_MAG past either end, None beyond (no model)."""
    try:
        k = float(k)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(k):
        return None
    if k < KS[0] - EXTRAP_MAG - 1e-9 or k > KS[-1] + EXTRAP_MAG + 1e-9:
        return None
    if k <= KS[0]:
        i = 0
    elif k >= KS[-1]:
        i = len(KS) - 2
    else:
        i = max(j for j in range(len(KS) - 1) if KS[j] <= k + 1e-12)
        i = min(i, len(KS) - 2)
    x0, x1 = KS[i], KS[i + 1]
    y0, y1 = math.log(lams[i]), math.log(lams[i + 1])
    return math.exp(y0 + (y1 - y0) * (k - x0) / (x1 - x0))


def lam_eff(k: float) -> Optional[float]:
    """Effective daily rate of days reaching magnitude k, off the table
    (_loglin over P_DAY)."""
    return _loglin(k, [-math.log(1.0 - p) for p in P_DAY])


def lam_bg_eff(k: float) -> Optional[float]:
    """The aftershock model's background rate at magnitude k (_loglin over
    LAM_BG)."""
    return _loglin(k, LAM_BG)


def p_yes(k: float, day_start_ts: float, now_ts: float,
          lag_h: float) -> Optional[float]:
    """P(the UTC day [day_start, day_start+24h) reaches magnitude k), given
    nothing on the feed has reached it yet. None when k has no model."""
    lam = lam_eff(k)
    if lam is None:
        return None
    day_end = day_start_ts + 86400.0
    start = max(day_start_ts, now_ts - max(0.0, lag_h) * 3600.0)
    w_h = max(0.0, day_end - start) / 3600.0
    return 1.0 - math.exp(-lam * min(w_h, 24.0) / 24.0)


def _omori(t1: float, t2: float) -> float:
    """Integral of (t + AS_C)^-AS_P dt over [t1, t2], t in days (t1, t2 >= 0)."""
    t1 = max(0.0, t1)
    t2 = max(t1, t2)
    if abs(AS_P - 1.0) < 1e-9:
        return math.log((t2 + AS_C) / (t1 + AS_C))
    return ((t2 + AS_C) ** (1.0 - AS_P) - (t1 + AS_C) ** (1.0 - AS_P)) / (1.0 - AS_P)


def dedup_sources(sources: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Sources with origins within AS_DEDUP_SECS of the previous one kept are
    one quake: the larger magnitude stays (time-sorted)."""
    out: List[Tuple[float, float]] = []
    for ts, m in sorted(sources):
        if out and ts - out[-1][0] <= AS_DEDUP_SECS:
            if m > out[-1][1]:
                out[-1] = (out[-1][0], m)
            continue
        out.append((ts, m))
    return out


def aftershock_n(k: float, start_ts: float, end_ts: float, now_ts: float,
                 sources: List[Tuple[float, float]]) -> float:
    """Expected count of M >= k quakes in [start, end) from the sources
    [(origin epoch, magnitude)]: 10^(a + b(M - k)) times the Omori integral,
    a source counted AS_SRC_LAG_H after its origin and at most AS_WINDOW_D
    old."""
    n = 0.0
    for ts, m in sources:
        if ts > now_ts - AS_SRC_LAG_H * 3600.0 \
                or start_ts - ts > AS_WINDOW_D * 86400.0:
            continue
        n += 10.0 ** (AS_A + AS_B * (m - k)) * _omori(
            (start_ts - ts) / 86400.0, (end_ts - ts) / 86400.0)
    return n


def p_yes_as(k: float, day_start_ts: float, now_ts: float, lag_h: float,
             sources: Optional[List[Tuple[float, float]]]
             ) -> Tuple[Optional[float], Optional[float]]:
    """(table fair, aftershock fair) for strike k over the rest of the UTC day
    plus the unpublished tail, as p_yes; the aftershock fair is None without a
    source list or a background rate."""
    base = p_yes(k, day_start_ts, now_ts, lag_h)
    lam = lam_bg_eff(k)
    if base is None or lam is None or sources is None:
        return base, None
    day_end = day_start_ts + 86400.0
    start = max(day_start_ts, now_ts - max(0.0, lag_h) * 3600.0)
    w = min(max(0.0, day_end - start) / 86400.0, 1.0)
    n = aftershock_n(k, start, day_end, now_ts, sources)
    return base, 1.0 - math.exp(-(lam * w + n))


def _iso_ts(s: str) -> Optional[float]:
    s = (s or "").strip().replace("Z", "")
    if not s:
        return None
    if "." in s:
        a, b = s.split(".", 1)
        s = a + "." + (b + "000000")[:6]
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def parse_usgs(payload: dict) -> Tuple[Optional[float], List[dict]]:
    """(feed generation epoch or None, events) from a USGS summary GeoJSON."""
    gen = ((payload or {}).get("metadata") or {}).get("generated")
    out: List[dict] = []
    for f in (payload or {}).get("features") or []:
        p = (f or {}).get("properties") or {}
        try:
            mag = float(p["mag"])
            t = float(p["time"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(mag) and math.isfinite(t)):
            continue
        ev = {"id": str(f.get("id") or p.get("code") or ""),
              "mag": mag, "mag_type": str(p.get("magType") or ""),
              "net": str(p.get("net") or "").lower(), "time": t,
              "place": str(p.get("place") or "")[:80]}
        try:
            upd = float(p["updated"]) / 1000.0
            if math.isfinite(upd):
                ev["updated"] = upd
        except (KeyError, TypeError, ValueError):
            pass
        out.append(ev)
    try:
        gen_ts = float(gen) / 1000.0 if gen is not None else None
    except (TypeError, ValueError):
        gen_ts = None
    return gen_ts, out


def parse_gfz(text: str) -> List[dict]:
    """Events from a GEOFON FDSN text response (format=text):
    EventID|Time|Latitude|Longitude|Depth/km|Author|Catalog|Contributor|
    ContributorID|MagType|Magnitude|MagAuthor|EventLocationName|EventType"""
    out: List[dict] = []
    for line in (text or "").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 11:
            continue
        t = _iso_ts(parts[1])
        try:
            mag = float(parts[10])
        except ValueError:
            continue
        if t is None or not math.isfinite(mag):
            continue
        out.append({"id": parts[0].strip(), "mag": mag,
                    "mag_type": parts[9].strip(), "time": t,
                    "place": (parts[12].strip() if len(parts) > 12 else "")[:80]})
    return out


def parse_emsc(text) -> Optional[Tuple[str, dict]]:
    """(action, event) from one EMSC realtime message --
    {"action": "create"|"update", "data": {"properties": {"unid", "time",
    "mag", "magtype", "flynn_region", ...}}} -- or None when it is not a
    usable event (no id / time / finite magnitude, or not JSON)."""
    try:
        msg = json.loads(text)
        data = msg.get("data") or {}
        p = data.get("properties") or {}
        eid = str(p.get("unid") or data.get("id") or "").strip()
        mag = float(p["mag"])
        t = _iso_ts(str(p.get("time") or ""))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if not eid or t is None or not math.isfinite(mag):
        return None
    return (str(msg.get("action") or ""),
            {"id": eid, "mag": mag, "mag_type": str(p.get("magtype") or ""),
             "time": t, "place": str(p.get("flynn_region") or "")[:80]})


def fetch_usgs(timeout: float = HTTP_TIMEOUT) -> Tuple[Optional[float], List[dict]]:
    r = requests.get(USGS_URL, headers={"User-Agent": USER_AGENT},
                     timeout=timeout)
    r.raise_for_status()
    return parse_usgs(r.json())


def fetch_as_catalog(now_ts: Optional[float] = None,
                     timeout: float = 60.0) -> List[dict]:
    """The aftershock term's sources: every ComCat earthquake of M >=
    AS_MMIN in the last AS_WINDOW_D days (~500-600 events), parse_usgs's
    shape."""
    now_ts = time.time() if now_ts is None else now_ts
    start = datetime.fromtimestamp(now_ts - AS_WINDOW_D * 86400.0, timezone.utc)
    r = requests.get(AS_CATALOG_URL, params={
        "format": "geojson", "eventtype": "earthquake", "minmagnitude": AS_MMIN,
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"), "orderby": "time"},
        headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    return parse_usgs(r.json())[1]


def fetch_gfz(now_ts: Optional[float] = None,
              timeout: float = HTTP_TIMEOUT) -> List[dict]:
    now_ts = time.time() if now_ts is None else now_ts
    start = datetime.fromtimestamp(now_ts - GFZ_LOOKBACK_MIN * 60, timezone.utc)
    r = requests.get(GFZ_URL, params={
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "minmagnitude": FREEZE_MIN_MAG, "format": "text", "orderby": "time"},
        headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if r.status_code == 204:          # FDSN: no events in the window
        return []
    r.raise_for_status()
    return parse_gfz(r.text)


class QuakeWatch:
    """What the two feeds currently show, and the verdict for one strike.
    Updated by the refresher thread, read by the quote loop (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.usgs: Dict[str, dict] = {}
        self.gfz: Dict[str, dict] = {}
        self.usgs_ok: Optional[float] = None       # last successful USGS read
        self.usgs_generated: Optional[float] = None
        self.gfz_ok: Optional[float] = None        # last successful GFZ read
        self.emsc: Dict[str, dict] = {}
        self.emsc_ok: Optional[float] = None       # last EMSC frame (or pong)
        # the aftershock term's year of sources: id -> (origin, magnitude)
        self.as_catalog: Dict[str, Tuple[float, float]] = {}
        self.as_ok: Optional[float] = None         # last successful catalog read
        self.created = time.time()

    # -- updates -----------------------------------------------------------
    @staticmethod
    def _settled_since(e: dict, generated: Optional[float],
                       now_ts: float) -> float:
        """stable_since for a solution on the watch's FIRST USGS read (Jack
        2026-10-07: the 02:33Z restart re-froze KXBIGGESTQUAKE-07OCT26 on
        gfz2026tpwq, which the old process had already matched to NEIC's
        settled M4.6 us6000u0nb -- the CONFIRM_SECS clock had restarted with
        the process). USGS bumps an event's 'updated' stamp on every
        revision, so the feed copy shows the solution unchanged from
        'updated' to its own generation time; only that span is credited.
        No stamp (or one past the copy) -> now, the old clock."""
        u = e.get("updated")
        if u is None:
            return now_ts
        seen_to = now_ts if generated is None else min(generated, now_ts)
        return now_ts - max(0.0, seen_to - u)

    def update_usgs(self, generated: Optional[float], events: List[dict],
                    now_ts: float) -> None:
        with self._lock:
            startup = self.usgs_ok is None         # this process's first read
            fresh: Dict[str, dict] = {}
            for e in events:
                old = self.usgs.get(e["id"])
                rec = dict(e)
                rec["first_seen"] = old["first_seen"] if old else now_ts
                same = old is not None and old["mag"] == e["mag"] \
                    and old["net"] == e["net"]
                if same:
                    rec["stable_since"] = old["stable_since"]
                elif old is None and startup:
                    rec["stable_since"] = self._settled_since(e, generated, now_ts)
                else:
                    rec["stable_since"] = now_ts
                fresh[e["id"]] = rec
            self.usgs = fresh
            self.usgs_ok = now_ts
            self.usgs_generated = generated

    def update_gfz(self, events: List[dict], now_ts: float) -> None:
        with self._lock:
            fresh: Dict[str, dict] = {}
            for e in events:
                old = self.gfz.get(e["id"])
                rec = dict(e)
                rec["first_seen"] = old["first_seen"] if old else now_ts
                fresh[e["id"]] = rec
            self.gfz = fresh
            self.gfz_ok = now_ts

    def update_emsc(self, event: dict, now_ts: float) -> bool:
        """One pushed EMSC event (parse_emsc). A revision replaces the record
        (first sighting kept), so a downgrade counts as soon as it lands; a
        new event under EMSC_MIN_MAG is not kept. Prunes events older than
        EMSC_KEEP_MIN. True when the event is (now) kept."""
        with self._lock:
            old = self.emsc.get(event["id"])
            self.emsc_ok = now_ts
            for eid in [i for i, e in self.emsc.items()
                        if now_ts - e["time"] > EMSC_KEEP_MIN * 60]:
                del self.emsc[eid]
            if old is None and (event["mag"] < EMSC_MIN_MAG
                                or now_ts - event["time"] > EMSC_KEEP_MIN * 60):
                return False
            rec = dict(event)
            rec["first_seen"] = old["first_seen"] if old else now_ts
            self.emsc[event["id"]] = rec
            return True

    def emsc_alive(self, now_ts: float) -> None:
        """The EMSC connection delivered a frame (a message or a pong)."""
        with self._lock:
            self.emsc_ok = now_ts

    def update_as_catalog(self, events: List[dict], now_ts: float) -> None:
        """A fresh read of the aftershock sources (fetch_as_catalog)."""
        with self._lock:
            self.as_catalog = {e["id"]: (e["time"], e["mag"]) for e in events
                               if e["mag"] >= AS_MMIN - 1e-9}
            self.as_ok = now_ts

    # -- reads (callers hold the lock) --------------------------------------
    def _usgs_fresh(self, now_ts: float) -> bool:
        if self.usgs_ok is None or now_ts - self.usgs_ok > USGS_STALE_SECS:
            return False
        if self.usgs_generated is not None \
                and now_ts - self.usgs_generated > USGS_GEN_STALE_SECS:
            return False
        return True

    def _gfz_fresh(self, now_ts: float) -> bool:
        return self.gfz_ok is not None and now_ts - self.gfz_ok <= GFZ_STALE_SECS

    def _gfz_stale_since(self) -> float:
        """When GFZ went stale: its last good read + GFZ_STALE_SECS, or the
        watch's creation when it has never answered."""
        return (self.created if self.gfz_ok is None
                else self.gfz_ok + GFZ_STALE_SECS)

    def _day_max(self, day_start_ts: float) -> Optional[dict]:
        best = None
        for e in self.usgs.values():
            if day_start_ts <= e["time"] < day_start_ts + 86400.0:
                if best is None or e["mag"] > best["mag"]:
                    best = e
        return best

    def _confirmed(self, cp: Optional[dict], now_ts: float) -> bool:
        """A USGS event counts as confirmed once NEIC's own solution has
        shown for CONFIRM_SECS, or -- a regional network that stays
        authoritative -- its magnitude has held that long and the quake is
        NEIC_WAIT_MIN old."""
        if cp is None:
            return False
        held = now_ts - cp["stable_since"] >= CONFIRM_SECS
        if cp["net"] == "us":
            return held
        return held and now_ts - cp["time"] >= NEIC_WAIT_MIN * 60

    @staticmethod
    def _freeze_start(e: dict) -> float:
        """When a detection's FREEZE_MAX_MIN clock starts: first seen, but
        never later than FIRST_SEEN_MAX_LAG_MIN after the quake's origin (a
        restart re-sees every detection as new)."""
        return min(e["first_seen"], e["time"] + FIRST_SEEN_MAX_LAG_MIN * 60)

    def _as_sources(self, now_ts: float) -> Optional[List[Tuple[float, float]]]:
        """The aftershock term's sources, or None (the term is off) while the
        catalog is missing or older than AS_STALE_SECS: the catalog plus the
        live feed's NEIC-confirmed M >= AS_MMIN quakes (the feed's magnitude
        wins for a quake in both; an unconfirmed one already holds the book)."""
        if self.as_ok is None or now_ts - self.as_ok > AS_STALE_SECS:
            return None
        src = dict(self.as_catalog)
        for e in self.usgs.values():
            if e["mag"] >= AS_MMIN - 1e-9 and self._confirmed(e, now_ts):
                src[e["id"]] = (e["time"], e["mag"])
        return dedup_sources(list(src.values()))

    def _fairs(self, k: float, day_start_ts: float, now_ts: float,
               lag_h: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """(fair used, table fair, aftershock fair): the used one is the
        table's, or with AS_MODE raise the higher of the two (raise-only)."""
        if AS_MODE in ("shadow", "raise"):
            table, adj = p_yes_as(k, day_start_ts, now_ts, lag_h,
                                  self._as_sources(now_ts))
        else:
            table, adj = p_yes(k, day_start_ts, now_ts, lag_h), None
        use = table
        if AS_MODE == "raise" and table is not None and adj is not None:
            use = max(table, adj)
        return use, table, adj

    def _match_usgs(self, t: float) -> Optional[dict]:
        best, gap = None, None
        for e in self.usgs.values():
            d = abs(e["time"] - t)
            if d <= MATCH_SECS and (gap is None or d < gap):
                best, gap = e, d
        return best

    def _open_detections(self, now_ts: float) -> List[dict]:
        """Detections that still freeze the book: M >= FREEZE_MIN_MAG, first
        seen (_freeze_start) within FREEZE_MAX_MIN, not yet confirmed on
        USGS."""
        out = []
        limit = FREEZE_MAX_MIN * 60
        news = NEWS_MAX_AGE_MIN * 60
        for e in self.usgs.values():
            if e["mag"] >= FREEZE_MIN_MAG and now_ts - self._freeze_start(e) <= limit \
                    and now_ts - e["time"] <= news \
                    and not self._confirmed(e, now_ts):
                out.append({"src": "usgs", "id": e["id"], "mag": e["mag"],
                            "net": e["net"], "time": e["time"]})
        for g in self.gfz.values():
            if g["mag"] < FREEZE_MIN_MAG or now_ts - self._freeze_start(g) > limit \
                    or now_ts - g["time"] > news:
                continue
            if not self._confirmed(self._match_usgs(g["time"]), now_ts):
                out.append({"src": "gfz", "id": g["id"], "mag": g["mag"],
                            "net": "gfz", "time": g["time"]})
        return out

    def verdict(self, k: float, day_start_ts: float, now_ts: float,
                margin_cents: float = 1.0) -> dict:
        """{'action': 'quote'|'hold'|'stand', 'why', 'cap_c', 'fair', ...}:
        stand = cancel and stay out; hold = leave resting bids exactly as
        they are (freeze); quote = bid at most cap_c cents."""
        with self._lock:
            if not self._usgs_fresh(now_ts):
                age = None if self.usgs_ok is None else round(now_ts - self.usgs_ok)
                return {"action": "stand", "why": "USGS feed stale or missing",
                        "reason": "usgs_stale", "usgs_age_s": age}
            top = self._day_max(day_start_ts)
            if top is not None and top["mag"] >= float(k) - 1e-9:
                return {"action": "stand",
                        "why": (f"crossed: USGS shows M{top['mag']:g} "
                                f"({top['net']} {top['id']}) >= {k:g}"),
                        "reason": "crossed", "day_max": top["mag"],
                        "quake": top["id"]}
            gfz_fresh = self._gfz_fresh(now_ts)
            lag = GFZ_LAG_H if gfz_fresh else USGS_LAG_H
            fair, f_table, f_as = self._fairs(k, day_start_ts, now_ts, lag)
            if fair is None:
                return {"action": "stand", "why": f"no rate model for M{k:g}",
                        "reason": "no_model"}
            cap_c = int(math.floor(fair * 100.0 - margin_cents + 1e-9))
            base = {"fair": round(fair * 100.0, 2), "cap_c": cap_c,
                    "fair_table": round(f_table * 100.0, 2),
                    "fair_as": None if f_as is None else round(f_as * 100.0, 2),
                    "lag_h": lag, "gfz_fresh": gfz_fresh,
                    "day_max": None if top is None else top["mag"]}
            if cap_c < 1:
                return dict(base, action="stand", reason="fair_floor",
                            why=f"fair {fair * 100:.1f}c leaves no bid")
            det = self._open_detections(now_ts)
            if det:
                d = max(det, key=lambda x: x["mag"])
                return dict(base, action="hold", reason="detection",
                            why=(f"frozen: M{d['mag']:g} {d['src']} detection "
                                 f"{d['id']} not confirmed by NEIC yet"),
                            detections=len(det))
            if not gfz_fresh \
                    and now_ts - self._gfz_stale_since() <= FREEZE_MAX_MIN * 60:
                return dict(base, action="hold", reason="gfz_stale",
                            why="frozen: GFZ feed stale")
            return dict(base, action="quote", reason="ok", why="")

    def ask_verdict(self, k: float, day_start_ts: float, now_ts: float,
                    margin_cents: Optional[float] = None) -> dict:
        """{'ok', 'floor_c', 'reason', 'why', ...} for a YES ask on strike k
        (see ASK_MARGIN_CENTS): ok=False -> no ask may rest (pull any that
        do); ok=True -> an ask may rest at floor_c cents or higher."""
        margin = ASK_MARGIN_CENTS if margin_cents is None else margin_cents
        k = float(k)

        def off(reason: str, why: str, **extra) -> dict:
            return dict({"ok": False, "floor_c": None, "reason": reason,
                         "why": why}, **extra)

        with self._lock:
            if not self._usgs_fresh(now_ts):
                return off("usgs_stale", "USGS feed stale or missing")
            if not self._gfz_fresh(now_ts):
                return off("gfz_stale", "GFZ feed stale or missing")
            near = k - ASK_DAYMAX_GAP_MAG - 1e-9
            top = self._day_max(day_start_ts)
            if top is not None and top["mag"] >= near:
                return off("near_daymax",
                           f"today's USGS max M{top['mag']:g} ({top['id']}) is "
                           f"within {ASK_DAYMAX_GAP_MAG:g} of {k:g}",
                           quake=top["id"], mag=top["mag"])
            thr = k - ASK_PULL_BELOW_MAG - 1e-9
            cool = ASK_COOLOFF_MIN * 60
            news = NEWS_MAX_AGE_MIN * 60
            for src, evs in (("usgs", self.usgs), ("gfz", self.gfz),
                             ("emsc", self.emsc)):
                for e in evs.values():
                    if e["mag"] < thr:
                        continue
                    tag = (f"M{e['mag']:g} {src} {e['id']} "
                           f"(>= {k:g} - {ASK_PULL_BELOW_MAG:g})")
                    if src != "usgs" and e["mag"] >= near \
                            and day_start_ts <= e["time"] < day_start_ts + 86400.0:
                        return off(f"{src}_near",
                                   f"{src.upper()} shows {tag} inside the day",
                                   quake=e["id"], mag=e["mag"], src=src)
                    if now_ts - self._freeze_start(e) <= cool:
                        return off("detection", f"{tag} seen under "
                                   f"{ASK_COOLOFF_MIN:g} min ago",
                                   quake=e["id"], mag=e["mag"], src=src)
                    cp = e if src == "usgs" else self._match_usgs(e["time"])
                    if now_ts - e["time"] <= news and not self._confirmed(cp, now_ts):
                        return off("pending", f"{tag} not confirmed by NEIC yet",
                                   quake=e["id"], mag=e["mag"], src=src)
            # floored at the fair the bids use (raise-only: never under it)
            fair = self._fairs(k, day_start_ts, now_ts, GFZ_LAG_H)[0]
            if fair is None:
                return off("no_model", f"no rate model for M{k:g}")
            floor_c = max(1, int(math.ceil(fair * 100.0 + margin - 1e-9)))
            if floor_c > 99:
                return off("fair_ceiling", f"fair {fair * 100:.1f}c leaves no ask",
                           fair=round(fair * 100.0, 2))
            return {"ok": True, "floor_c": floor_c, "reason": "ok", "why": "",
                    "fair": round(fair * 100.0, 2)}

    def open_detections(self, now_ts: float) -> List[dict]:
        """The detections currently holding the quake markets."""
        with self._lock:
            return self._open_detections(now_ts)

    def snapshot(self, now_ts: float) -> dict:
        """Status for the file the refresher writes (observability only)."""
        with self._lock:
            day0 = math.floor(now_ts / 86400.0) * 86400.0
            top = self._day_max(day0)
            det = self._open_detections(now_ts)
            src = self._as_sources(now_ts)
            as_fairs = {}
            for k in (5.8, 6.0, 6.2, 6.6):
                t_f, a_f = p_yes_as(k, day0, now_ts, GFZ_LAG_H, src)
                as_fairs[f"{k:g}"] = [None if t_f is None else round(t_f * 100.0, 2),
                                      None if a_f is None else round(a_f * 100.0, 2)]
            return {
                "aftershock": {
                    "mode": AS_MODE, "a": AS_A,
                    "catalog_events": len(self.as_catalog),
                    "catalog_age_s": (None if self.as_ok is None
                                      else round(now_ts - self.as_ok)),
                    "sources": None if src is None else len(src),
                    "n_as_6": (None if src is None else round(aftershock_n(
                        6.0, now_ts, day0 + 86400.0, now_ts, src), 4)),
                    # strike -> [table fair, aftershock fair], cents, now
                    "fairs": as_fairs},
                "at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
                "usgs_ok_age_s": None if self.usgs_ok is None else round(now_ts - self.usgs_ok),
                "usgs_generated_age_s": (None if self.usgs_generated is None
                                         else round(now_ts - self.usgs_generated)),
                "gfz_ok_age_s": None if self.gfz_ok is None else round(now_ts - self.gfz_ok),
                "emsc_ok_age_s": None if self.emsc_ok is None else round(now_ts - self.emsc_ok),
                "usgs_events": len(self.usgs), "gfz_events": len(self.gfz),
                "emsc_events": len(self.emsc),
                "emsc_max": max((e["mag"] for e in self.emsc.values()), default=None),
                "today_max": None if top is None else {
                    k: top[k] for k in ("id", "mag", "mag_type", "net", "place")},
                "open_detections": det,
                "knobs": {"usgs_lag_h": USGS_LAG_H, "gfz_lag_h": GFZ_LAG_H,
                          "freeze_min_mag": FREEZE_MIN_MAG,
                          "confirm_secs": CONFIRM_SECS,
                          "neic_wait_min": NEIC_WAIT_MIN,
                          "freeze_max_min": FREEZE_MAX_MIN,
                          "first_seen_max_lag_min": FIRST_SEEN_MAX_LAG_MIN,
                          "news_max_age_min": NEWS_MAX_AGE_MIN,
                          "match_secs": MATCH_SECS,
                          "usgs_stale_secs": USGS_STALE_SECS,
                          "usgs_gen_stale_secs": USGS_GEN_STALE_SECS,
                          "gfz_stale_secs": GFZ_STALE_SECS,
                          "ask_margin_cents": ASK_MARGIN_CENTS,
                          "ask_pull_below_mag": ASK_PULL_BELOW_MAG,
                          "ask_daymax_gap_mag": ASK_DAYMAX_GAP_MAG,
                          "ask_cooloff_min": ASK_COOLOFF_MIN,
                          "emsc_min_mag": EMSC_MIN_MAG,
                          "emsc_keep_min": EMSC_KEEP_MIN,
                          "as_mode": AS_MODE, "as_a": AS_A, "as_b": AS_B,
                          "as_p": AS_P, "as_c": AS_C, "as_mmin": AS_MMIN,
                          "as_window_d": AS_WINDOW_D,
                          "as_src_lag_h": AS_SRC_LAG_H,
                          "as_stale_secs": AS_STALE_SECS},
            }


def write_status(path: str, watch: QuakeWatch,
                 now_ts: Optional[float] = None) -> None:
    now_ts = time.time() if now_ts is None else now_ts
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(watch.snapshot(now_ts), f, indent=1)
    os.replace(tmp, path)
