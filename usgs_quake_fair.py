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
under the model's fair value.

Fair value, for a strike not yet crossed:
    P(day max >= K) = 1 - exp(-lam(K) * W / 24)
lam(K) = -ln(1 - P_DAY(K)), P_DAY the empirical 10-year frequency of UTC
days whose maximum reached K (USGS ComCat 2016-2026, 3,650 days --
aftershock clustering included). W = hours of the day still able to add a
qualifying quake that is not on the feed yet: the rest of the day PLUS the
unpublished tail (USGS publishes a median 17.4 min after origin for M5.5+,
GFZ a median 5.3 min), clipped to the day. Before 00:00Z it is the whole day.
The live ladder sat within 1c of this model on 5.6-6.4 on 9/27.

Feeds (both official, polled from incentive_mm's refresher thread):
  USGS  earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson
        (public domain; regenerated every minute, cached up to 60 s)
  GFZ   geofon.gfz.de/fdsnws/event/1/query (the GEOFON FDSN event service;
        earthquake products CC BY 4.0 -- source: GEOFON data centre, GFZ
        Helmholtz Centre for Geosciences)

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


def lam_eff(k: float) -> Optional[float]:
    """Effective daily rate of days reaching magnitude k: exact at the table
    strikes, log-linear between them, the end segment's slope for up to
    EXTRAP_MAG past either end, None beyond (no model)."""
    try:
        k = float(k)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(k):
        return None
    lams = [-math.log(1.0 - p) for p in P_DAY]
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
        out.append({"id": str(f.get("id") or p.get("code") or ""),
                    "mag": mag, "mag_type": str(p.get("magType") or ""),
                    "net": str(p.get("net") or "").lower(), "time": t,
                    "place": str(p.get("place") or "")[:80]})
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


def fetch_usgs(timeout: float = HTTP_TIMEOUT) -> Tuple[Optional[float], List[dict]]:
    r = requests.get(USGS_URL, headers={"User-Agent": USER_AGENT},
                     timeout=timeout)
    r.raise_for_status()
    return parse_usgs(r.json())


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
        self.created = time.time()

    # -- updates -----------------------------------------------------------
    def update_usgs(self, generated: Optional[float], events: List[dict],
                    now_ts: float) -> None:
        with self._lock:
            fresh: Dict[str, dict] = {}
            for e in events:
                old = self.usgs.get(e["id"])
                rec = dict(e)
                rec["first_seen"] = old["first_seen"] if old else now_ts
                same = old is not None and old["mag"] == e["mag"] \
                    and old["net"] == e["net"]
                rec["stable_since"] = old["stable_since"] if same else now_ts
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
            fair = p_yes(k, day_start_ts, now_ts, lag)
            if fair is None:
                return {"action": "stand", "why": f"no rate model for M{k:g}",
                        "reason": "no_model"}
            cap_c = int(math.floor(fair * 100.0 - margin_cents + 1e-9))
            base = {"fair": round(fair * 100.0, 2), "cap_c": cap_c,
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
            return {
                "at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
                "usgs_ok_age_s": None if self.usgs_ok is None else round(now_ts - self.usgs_ok),
                "usgs_generated_age_s": (None if self.usgs_generated is None
                                         else round(now_ts - self.usgs_generated)),
                "gfz_ok_age_s": None if self.gfz_ok is None else round(now_ts - self.gfz_ok),
                "usgs_events": len(self.usgs), "gfz_events": len(self.gfz),
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
                          "gfz_stale_secs": GFZ_STALE_SECS},
            }


def write_status(path: str, watch: QuakeWatch,
                 now_ts: Optional[float] = None) -> None:
    now_ts = time.time() if now_ts is None else now_ts
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(watch.snapshot(now_ts), f, indent=1)
    os.replace(tmp, path)
