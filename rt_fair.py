#!/usr/bin/env python3
"""Rotten Tomatoes fair values for incentive_mm's KXRT gate.

Jack 2026-10-11: "build the KXRT, CPI, KXRAINWKND models"; 2026-10-12: "yes
fandango is authorized" (Rotten Tomatoes' terms, section 10, bar automated
access without Fandango's written authorization -- Jack has it).

KXRT-<FILM>-<K>: "If <film> has a Tomatometer score of above K on <Monday>
at 10:00 AM ET" -- the Monday after wide release (the market's close_time).
The score is the page's All Critics Tomatometer: round-half-up(100 L / N)
over L fresh of N reviews (108 of 108 Wayback reads, every .5 rounded up),
so YES iff 100 (L + X) / (N + M) >= K + 0.5 at the close, X fresh among the
M reviews still to come.

The page (rottentomatoes.com/m/<slug>, the event's settlement source; two
events name only the site root and resolve through SLUG_OVERRIDES or RT's
search, exact title and year) carries the counts in its media-scorecard-json
script. The model, fitted on Wayback trajectories of the settled titles
(see FIT below; it does not beat the market, so the bot's band is off and
the feed's job is the jump hold below):
    M ~ NegBin(mean N (1/g - 1), size R_NB)      g = share of the final
                                                 reviews in by now (days)
    X | M ~ BetaBinomial(M, k p, k (1 - p))      p = (L + PRIOR_W PRIOR_P) /
                                                 (N + PRIOR_W) + drift(days)
Late reviews run below the early ones (festival and junket critics are
kinder), and a later review's fresh rate scatters around the current one by
far more than a binomial would (k).

A film with fewer than MIN_REVIEWS reviews gets no price (the market's own
prior stands; the bot stands a film with 1 to MIN_REVIEWS - 1 of them aside,
see incentive_mm.RT_FAIR_FEW_REVIEWS). A jump in the review count -- the
embargo lift, MIN_JUMP+ reviews and +50% since the last read, or a film's
FIRST reviews whatever their number -- stamps the event `jump_at`: the gate
stands aside HOLD minutes from then while the market re-prices. 17 of the 66 settled titles passed 10 reviews
more than 7 days before the close -- inside the bot's quoting window -- and
their near-money strikes moved a median 14c (mean 20c) across that first
batch, nearly always up (festival critics are kind): resting asks are what
gets lifted.

Writes RT_FAIR_FILE for incentive_mm: markets {ticker: {p (None without a
price), event, fetched_at, n, jump_at}}, films {event: counts, slug,
first_reviews_at}, plus the cached open events.

Standalone:  python rt_fair.py [--out path]
"""

import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
SERIES = "KXRT"
KALSHI_EVENTS_URL = os.environ.get(
    "IMM_RT_EVENTS_URL", "https://api.elections.kalshi.com/trade-api/v2/events")
RT_BASE = os.environ.get("IMM_RT_BASE", "https://www.rottentomatoes.com")
# events whose settlement source names only the site root (2026-10-12)
SLUG_OVERRIDES: Dict[str, str] = {
    "KXRT-EBE": "ebenezer_2026",
    "KXRT-HOWT": "how_to_rob_a_bank_2026",
}

# ---- model -------------------------------------------------------------------
# Fit 2026-10-12 on 66 settled titles' Wayback trajectories (502 snapshots,
# 42 days to 0.3 before close; finals = Kalshi's expiration values) and
# scored against Kalshi's hourly mids at the same moments (1,196 markets):
# g = the median share of the final review count in by then; the drift and
# k are half / four times the raw late-review estimates (a grid on Brier).
# The market is the better forecaster at every horizon -- Brier 0.084 vs
# 0.091 more than 7 days out, 0.040 vs 0.045 inside -- and where the two
# disagreed by 10c+ the outcome sided with the market on the bid side, so
# incentive_mm uses this price only behind IMM_RT_FAIR_BAND (off).
# (days before close upper bound, g, drift in fresh-rate units, k)
FIT = ((1.0, 0.98, 0.000, 1600.0),
       (2.0, 0.96, 0.000, 800.0),
       (3.0, 0.89, -0.004, 240.0),
       (4.0, 0.67, -0.022, 100.0),
       (7.0, 0.42, -0.0175, 60.0),
       (14.0, 0.33, -0.0175, 48.0),
       (1e9, 0.24, -0.0245, 40.0))
R_NB = 4.0                 # negative binomial size of the reviews to come
PRIOR_W, PRIOR_P = 2.0, 0.67
MIN_REVIEWS = int(os.environ.get("IMM_RT_MIN_REVIEWS", "10"))
MIN_JUMP = int(os.environ.get("IMM_RT_MIN_JUMP", "15"))
QUANTILES = 80             # M's distribution in this many equal-mass points
EXACT_M_MAX = 250          # ... unless its support is shorter than this

# ---- polling ---------------------------------------------------------------
EVENTS_TTL_SECS = 3600
POLL_EMBARGO_SECS = int(os.environ.get("IMM_RT_POLL_EMBARGO_SECS", "120"))
POLL_REVIEWED_SECS = int(os.environ.get("IMM_RT_POLL_REVIEWED_SECS", "300"))
POLL_FAR_SECS = int(os.environ.get("IMM_RT_POLL_FAR_SECS", "600"))
POLL_RELEASE_WEEK_SECS = int(os.environ.get("IMM_RT_POLL_RELEASE_WEEK_SECS", "1800"))
EMBARGO_WATCH_DAYS = 21.0  # no reviews and closing within this: poll fast
RELEASE_WEEK_DAYS = 7.0    # the bot's stand-down (KXRT_CUTOFF_BEFORE_CLOSE_DAYS)
HTTP_TIMEOUT = 30
USER_AGENT = "KL-IMM-rt-fair/1.0"
_SCORECARD_RE = re.compile(
    r'<script[^>]*id="media-scorecard-json"[^>]*>(.*?)</script>', re.S)
_SLUG_RE = re.compile(r"rottentomatoes\.com/m/([A-Za-z0-9_\-]+)")


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z [rt-fair] {msg}",
          flush=True)


# ---------------------------------------------------------------------------
# model
def fit_row(days: float) -> Tuple[float, float, float]:
    """(g, drift, k) for `days` before the close."""
    for hi, g, d, k in FIT:
        if days < hi:
            return g, d, k
    return FIT[-1][1:]


def _nb_points(mu: float, r: float) -> List[Tuple[int, float]]:
    """M ~ NegBin(mean mu, size r) as [(m, prob)]: exact when its support
    is short, else QUANTILES equal-mass points (each bin's mean m)."""
    if mu <= 1e-9:
        return [(0, 1.0)]
    q = mu / (r + mu)
    pm = (r / (r + mu)) ** r
    pts, cum, m = [], 0.0, 0
    while cum < 0.9995 and m < 20000:
        pts.append((m, pm))
        cum += pm
        pm *= (m + r) / (m + 1) * q
        m += 1
    tot = sum(p for _, p in pts)
    pts = [(m, p / tot) for m, p in pts]
    if len(pts) <= EXACT_M_MAX:
        return pts
    out, acc, wsum, step = [], 0.0, 0.0, 1.0 / QUANTILES
    for m, p in pts:
        acc += p
        wsum += p * m
        if acc >= step - 1e-12:
            out.append((int(round(wsum / acc)), acc))
            acc, wsum = 0.0, 0.0
    if acc > 0:
        out.append((int(round(wsum / acc)), acc))
    return out


def _bb_tail(M: int, a: float, b: float, thresholds: List[int]) -> List[float]:
    """P(X >= t) for each t, X ~ BetaBinomial(M, a, b)."""
    lp0 = (math.lgamma(a + b) - math.lgamma(b) + math.lgamma(M + b)
           - math.lgamma(M + a + b))
    pmf = [0.0] * (M + 1)
    pmf[0] = math.exp(lp0)
    for x in range(M):
        pmf[x + 1] = pmf[x] * (M - x) / (x + 1) * (x + a) / (M - x - 1 + b)
    tail = [0.0] * (M + 2)
    for x in range(M, -1, -1):
        tail[x] = tail[x + 1] + pmf[x]
    s = tail[0] or 1.0
    return [1.0 if t <= 0 else (0.0 if t > M else tail[t] / s) for t in thresholds]


def yes_threshold(liked: int, total: int, strike: float) -> int:
    """The fewest fresh reviews X among the new ones for a score above
    `strike` with `total` reviews in all: round-half-up(100 (L + X) /
    total) > K  <=>  200 (L + X) >= (2K + 1) total (integers, no float edge)."""
    num = int(round(2 * strike + 1)) * total - 200 * liked
    return -((-num) // 200)


def p_above(liked: int, n: int, strikes: List[float], days: float
            ) -> Dict[float, float]:
    """P(score > K at the close) for each strike K."""
    g, drift, k = fit_row(days)
    p = (liked + PRIOR_W * PRIOR_P) / (n + PRIOR_W) + drift
    p = min(max(p, 0.002), 0.998)
    mu = n * (1.0 / g - 1.0)
    a, b = k * p, k * (1.0 - p)
    out = {K: 0.0 for K in strikes}
    for M, w in _nb_points(mu, R_NB):
        th = [yes_threshold(liked, n + M, K) for K in strikes]
        if M == 0:
            tails = [1.0 if t <= 0 else 0.0 for t in th]
        else:
            tails = _bb_tail(M, a, b, th)
        for K, t in zip(strikes, tails):
            out[K] += w * t
    return {K: min(max(v, 0.0), 1.0) for K, v in out.items()}


# ---------------------------------------------------------------------------
# Rotten Tomatoes
def parse_scorecard(html: str) -> Optional[dict]:
    """{'liked', 'n', 'score'} from a film page's media-scorecard-json (the
    All Critics overlay, which is the headline Tomatometer), else None."""
    m = _SCORECARD_RE.search(html or "")
    if not m:
        return None
    try:
        sc = json.loads(m.group(1))
    except ValueError:
        return None
    ca = (sc.get("overlay") or {}).get("criticsAll") or sc.get("criticsScore") or {}
    try:
        liked = int(ca.get("likedCount") or 0)
        n = int(ca.get("reviewCount") or 0)
    except (TypeError, ValueError):
        return None
    if n < 0 or not 0 <= liked <= n:
        return None
    score = ca.get("score")
    try:
        score = int(score) if score not in (None, "") else None
    except (TypeError, ValueError):
        score = None
    return {"liked": liked, "n": n, "score": score}


def fetch_film(slug: str) -> Optional[dict]:
    r = requests.get(f"{RT_BASE}/m/{slug}", timeout=HTTP_TIMEOUT,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return parse_scorecard(r.text)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def search_slug(title: str, year: int) -> Optional[str]:
    """RT's search: the one film titled exactly `title` released in `year`
    or the year before, else None."""
    r = requests.get(f"{RT_BASE}/search", params={"search": title},
                     timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    hits = []
    for attrs, body in re.findall(
            r"<search-page-media-row([^>]*)>(.*?)</search-page-media-row>", r.text, re.S):
        yr = re.search(r'release-year="(\d{4})"', attrs)
        link = _SLUG_RE.search(body)
        name = re.search(r'data-qa="info-name"[^>]*>\s*([^<]+?)\s*<', body)
        if yr and link and name and _norm(name.group(1)) == _norm(title) \
                and int(yr.group(1)) in (year, year - 1):
            hits.append(link.group(1))
    return hits[0] if len(set(hits)) == 1 else None


# ---------------------------------------------------------------------------
# Kalshi
def _signed_or_public(get_json: Optional[Callable], params: dict) -> dict:
    if get_json is not None:
        try:
            js = get_json("/events", dict(params))
            if isinstance(js, dict):
                return js
        except Exception:
            pass
    for attempt in range(3):
        r = requests.get(KALSHI_EVENTS_URL, params=params, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 2:
            time.sleep(3 * (attempt + 1) ** 2)
            continue
        r.raise_for_status()
        return r.json() or {}
    return {}


def fetch_events(get_json: Optional[Callable] = None) -> Dict[str, dict]:
    """Open KXRT events: event -> {'title', 'url', 'close' (ISO), 'strikes':
    {ticker: K}} (strike_type 'greater' only)."""
    out: Dict[str, dict] = {}
    cursor = None
    for _ in range(10):
        params = {"series_ticker": SERIES, "status": "open",
                  "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        js = _signed_or_public(get_json, params)
        for e in js.get("events") or []:
            ev = str(e.get("event_ticker") or "")
            src = [str(s.get("url") or "") for s in (e.get("settlement_sources") or [])]
            strikes, closes = {}, set()
            for m in e.get("markets") or []:
                if m.get("strike_type") != "greater" or m.get("floor_strike") is None:
                    continue
                strikes[str(m["ticker"])] = float(m["floor_strike"])
                closes.add(str(m.get("close_time") or ""))
            if ev and strikes and len(closes) == 1:
                out[ev] = {"title": str(e.get("sub_title") or e.get("title") or ""),
                           "url": src[0] if src else "", "close": closes.pop(),
                           "strikes": strikes}
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


# ---------------------------------------------------------------------------
def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _ts(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def poll_every(days: float, n: int) -> int:
    """Seconds between page reads for a film `days` from its close with `n`
    reviews: fast while an embargo can lift on our quotes."""
    if days <= RELEASE_WEEK_DAYS:
        return POLL_RELEASE_WEEK_SECS
    if n > 0:
        return POLL_REVIEWED_SECS
    return POLL_EMBARGO_SECS if days <= EMBARGO_WATCH_DAYS else POLL_FAR_SECS


def is_jump(old_n: Optional[int], new_n: int) -> bool:
    """MIN_JUMP+ new reviews and +50% since the last read, or the first
    reviews at all (0 -> any): Klara and the Sun's first 8 (1 fresh) showed
    at 20:28:22Z on 10/15, the book crashed at 20:30:01 and the bot, with no
    jump and no price, kept quoting through it (Jack 2026-10-15: "ship 1 - 4")."""
    if old_n is None:
        return False                      # first read ever: no baseline
    if old_n == 0 and new_n > 0:
        return True
    return new_n - old_n >= max(MIN_JUMP, 0.5 * old_n)


def resolve_slug(ev: str, e: dict, film: dict, search: Callable) -> Optional[str]:
    if ev in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[ev]
    m = _SLUG_RE.search(e.get("url") or "")
    if m:
        return m.group(1)
    if film.get("slug"):
        return film["slug"]
    if film.get("search_failed"):
        return None
    close = _ts(e.get("close"))
    try:
        slug = search(e.get("title") or "", close.year if close else datetime.now().year)
    except Exception:
        slug = None
    if not slug:
        film["search_failed"] = True
    return slug


def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    events: Optional[Dict[str, dict]] = None,
                    film_fn: Callable = fetch_film,
                    search_fn: Callable = search_slug) -> Tuple[int, int]:
    """One pass: re-read Kalshi's open events hourly (every pass while none
    is open; a failed read reuses the cache), read each film page that is
    due (poll_every), price every strike from the film's latest counts, and
    write the file atomically. Returns (markets priced, open markets
    without a price)."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    ev_cache = old.get("open_events") or {}
    events_at = float(old.get("events_at") or 0.0)
    if events is None:
        if ev_cache and ts - events_at < EVENTS_TTL_SECS:
            events = ev_cache
        else:
            try:
                events = fetch_events(get_json)
                events_at = ts
            except Exception:
                if not ev_cache:
                    raise
                events = ev_cache
    old_films = old.get("films") or {}
    films: Dict[str, dict] = {}
    markets: Dict[str, dict] = {}
    missing: Dict[str, str] = {}
    failed: Dict[str, str] = {}
    for ev, e in sorted(events.items()):
        film = dict(old_films.get(ev) or {})
        close = _ts(e.get("close"))
        slug = resolve_slug(ev, e, film, search_fn)
        if close is None or not slug:
            for t in e["strikes"]:
                missing[t] = "no RT page for this event" if close else "no close time"
            films[ev] = film
            continue
        film["slug"] = slug
        days = (close - now).total_seconds() / 86400.0
        last = _ts(film.get("read_at"))
        due = last is None or (now - last).total_seconds() >= \
            poll_every(days, int(film.get("n") or 0)) - 5
        if due and days > -0.5:
            try:
                sc = film_fn(slug)
                if sc is None:
                    raise ValueError("no scorecard on the page")
            except Exception as ex:
                err = f"{type(ex).__name__}: {str(ex)[:80]}"
                if not film.get("read_error"):
                    failed[ev] = err          # newly failing: logged once
                film["read_error"] = err
            else:
                film.pop("read_error", None)
                old_n = film.get("n")
                if is_jump(old_n, sc["n"]):
                    film["jump_at"] = now.isoformat()
                    film["jump_from"] = old_n
                if sc["n"] > 0 and not old_n and old_n is not None:
                    film["first_reviews_at"] = now.isoformat()
                film.update(sc, read_at=now.isoformat())
        films[ev] = film
        if film.get("read_at") is None:
            for t in e["strikes"]:
                missing[t] = "page not read yet"
            continue
        n, liked = int(film.get("n") or 0), int(film.get("liked") or 0)
        priced = n >= MIN_REVIEWS and days > 0
        ps = p_above(liked, n, sorted(set(e["strikes"].values())), days) if priced else {}
        for t, K in e["strikes"].items():
            markets[t] = {"p": round(ps[K], 4) if priced else None, "event": ev,
                          "fetched_at": film["read_at"], "n": n,
                          "jump_at": film.get("jump_at")}
            if not priced:
                missing[t] = f"{n} reviews" if days > 0 else "closed"
    if failed:
        ev, err = sorted(failed.items())[0]
        _log(f"page read failed for {len(failed)} film(s) ({ev}: {err}); "
             "their last counts stand until the gate's TTL")
    payload = {"generated_at": now.isoformat(), "events_at": events_at,
               "open_events": events, "films": films, "markets": markets,
               "missing": missing,
               "model": {"fit": FIT, "r_nb": R_NB, "prior": [PRIOR_W, PRIOR_P],
                         "min_reviews": MIN_REVIEWS, "min_jump": MIN_JUMP}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    os.replace(tmp, path)
    priced = sum(1 for m in markets.values() if m["p"] is not None)
    return priced, len(missing)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR, "rt_fair.json"))
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    d = _read_json(args.out)
    _log(f"{ok} markets priced, {miss} without a price")
    for ev, f in sorted((d.get("films") or {}).items()):
        _log(f"  {ev}: {f.get('slug')} {f.get('liked')}/{f.get('n')} "
             f"(score {f.get('score')})")
    for t, m in sorted((d.get("markets") or {}).items()):
        if m["p"] is not None:
            _log(f"  {t}: {m['p'] * 100:.0f}c")
    return 0


if __name__ == "__main__":
    sys.exit(main())
