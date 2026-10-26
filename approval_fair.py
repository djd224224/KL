#!/usr/bin/env python3
"""
approval_fair.py -- fair values for the RealClearPolitics Trump approval
markets, for incentive_mm's fair-file gate (Jack 2026-10-25: one fair-value
module for KXTRUMPAPPROVE and KXAPRPOTUS; the gate stands a market aside
without a fresh fair and caps the side whose touch is more than TOL through
it -- a guard against being picked off, not a price we quote at).

THE MARKETS
  KXTRUMPAPPROVE-<YYMONDD>-<U|E|A><K>: the "RCP Average" Approve value at
  exactly 1:00 PM ET on the ticker date. Nine 0.1-point buckets: strike_type
  "less" (below cap_strike), "between" with floor == cap (exactly that
  value) and "greater" (above floor_strike). Listed 10:01 ET the day
  before, closes 12:59 ET.
  KXAPRPOTUS-<YYMONDD>-<x>: the same number at 11:00 AM ET on the ticker
  date (a Friday), weekly, in 0.3-point ranges ("between" floor..cap
  inclusive, "less" than cap, "greater" than floor).
  Each event's read time comes from its rules_primary.

THE DATA
  The settlement page (RCP_PAGE_URL) answers scripts with a DataDome
  challenge (HTTP 403, captcha-delivery.com); this module never touches it.
  The page's own poll table is loaded from RCP_FEED_URL on
  orig.realclearpolitics.com (CloudFront, no challenge; its robots.txt
  allows every agent but two AI-training crawlers): one JSON with both
  Trump terms, each term's "RCP Average" row first, then its polls --
  "poll_rcp_avg" for those in the average -- with field dates, Approve and
  the CMS "updated" stamp. The file is rebuilt when RCP edits the table
  (Last-Modified moves; on 2026-10-25 it sat at Sunday 11:59 CT all
  evening), so a read is conditional (If-None-Match / If-Modified-Since; a
  304 is a good, unchanged read) and at most every RCP_MIN_READ_SECS.
  A read is TRUSTED only if the 2nd-term table (RCP_POLL_ID) is there, its
  Approve is inside VALUE_RANGE, it holds MIN_IN..MAX_IN in-average polls,
  their mean rounds to the Average (|mean - avg| <= 0.05: on all 410
  Wayback copies of the page since 1/2025 the Average is the mean rounded
  half up, 32 exact .x5 ties included, and live on 10/25: 13 polls, mean
  38.46, Average 38.5), and the poll list dates at least MIN_SESSIONS editing
  sessions in RATE_WEEKS. A read that fails a check is a fault: no fairs at
  all until a good one. A network failure keeps the last good read and its
  time (fetched_at), so the gate's TTL runs out on it. A move of more than
  MAX_STEP points between reads waits for a second read to confirm it.
  SETTLEMENT CROSS-CHECK: the value read within SETTLE_READ_WINDOW_SECS of
  each event's read time is compared with the bucket Kalshi settled YES; a
  mismatch stops every fair until a later settlement agrees again.

THE MODEL
  The Average moves only when RCP's editors touch the poll list: every
  value change in the 54 Kalshi-settled days (9/2-10/25) with no CMS
  "editing session" between two settlement reads was zero (9 of 9 one-day
  pairs). A session = the polls' creation times (the running minimum of
  "updated" from the newest id down -- ids are sequential, a later edit only
  raises "updated") and their later edit stamps, merged within
  SESSION_GAP_SECS. So
      V(T) = V(now) + J_1 + ... + J_N,
  N = the sessions before the read time T, each ET hour slot of the week a
  Bernoulli with p = the share of the last RATE_WEEKS weeks that had a
  session in that slot (+ SLOT_PRIOR, + BG_PER_HOUR for moves the stamps
  miss), recomputed from the live feed on every read -- on 10/25 about 2.2
  sessions a weekday (09-10 ET slots 0.4-0.9), 0.4 a Saturday, 1.4 a
  Sunday (a 09-10 ET bump); and J, the move per
  session in tenths (JUMP: 0 21%, +-1 44%, +-2 16%, +-3 9%, a tail to
  +-8), fitted by maximum likelihood on the 191 pairs of Kalshi-settled
  reads up to 3 days apart with the session count between them known.
  The fair of a bucket is P(V(T) in it).

BACKTEST (scratch fair_approval/, walk-forward: J refitted daily on the
  pairs settled before, rates from the trailing 8 weeks, V(t) only where a
  settled read with no session in between pins it)
  * Hourly points through each event's quoting window, Brier vs the last
    trade within the hour: KXTRUMPAPPROVE 0.085 model / 0.079 market (46
    events, 5,462 points; 0.082 / 0.074 at 12-18 h out); KXAPRPOTUS 0.130 /
    0.118 (7 events, 2,533 points). The market is the better forecaster on
    average -- it knows the release calendar (a Fox poll out Wednesday
    night is entered Thursday morning); this model does not.
  * Gate on our 30 days of maker fills (dashboard D.fills, 9/27-10/25),
    the 1,313 of 1,768 daily fills whose V is pinned: at TOL 15 the gate
    blocks 92 fills (3,694 ct, 13 of 29 events) that lost $766 to
    settlement and $23 at 4 h; at TOL 10, 161 fills, -$926 / -$70. With
    the other fills' V interpolated, TOL 15 blocks 158 fills, -$1,394
    (event bootstrap 90%: -$2,579..-$358) / -$233. Mostly asks on the
    bucket AT the current value while it stayed put. The touch sat 15c
    through the fair ~4% of each side's market-hours (the reward cost).
    The weekly's blocked fills WON (+$227 at TOL 15, 9 fills, 3 events;
    +$58 at 35): band it wide or not at all.
GATE (incentive_mm APPROVE_FAIR_*): KXTRUMPAPPROVE only, TOL 15c, TTL 15
  min, refresh every 120 s (a 304 is a fresh read, so quiet nights stay
  fresh; the feed is read at most every RCP_MIN_READ_SECS whatever the
  cadence). KXAPRPOTUS is priced here but not gated: the fills a band would
  have blocked won.
NOT BUILT: polls released but not yet entered by RCP. No single reliable
  public source: RCP's latest-polls pages sit behind the same DataDome,
  pollsters publish on their own sites at their own hours, and Wikipedia's
  tables trail by hours. That is where the market beats this model.

Standalone dry run:  python approval_fair.py [--out path] [--now ISO]
"""
import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    ET = None

SERIES = ("KXTRUMPAPPROVE", "KXAPRPOTUS")
RCP_POLL_ID = "8656"                  # "President Trump 2nd Term Job Approval"
RCP_PAGE_URL = "https://www.realclearpolling.com/polls/approval/donald-trump/approval-rating"
RCP_FEED_URL = ("https://orig.realclearpolitics.com/json/battleground_script/"
                "test_2025/trump_1st_ja_6179_8656_polling_data.json")
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
USER_AGENT = "KL-IMM-approval-fair/1.0"   # says what it is (served: HTTP 200, 10/26)
HTTP_TIMEOUT = 30
RCP_MIN_READ_SECS = 90          # the feed at most this often, whatever the caller's cadence
MARKETS_TTL_SECS = 300          # Kalshi's open markets (a new daily lists 10:01 ET)
PUBLIC_SPACING_SECS = 0.35      # between public (unsigned) Kalshi requests; 429s back off
VALUE_RANGE = (25.0, 75.0)      # a sane Approve average
MIN_IN, MAX_IN = 5, 30          # in-average polls a sane table holds
MAX_STEP = 1.0                  # points; a bigger move between reads waits for a 2nd read
HISTORY_DAYS = 10               # value changes / cross-checks kept in the file
SETTLE_READ_WINDOW_SECS = 900   # reads within this of a read time count for the cross-check
XCHECK_AFTER_SECS = 1800        # ask Kalshi for the result this long after the read time
XCHECK_EVERY_SECS = 300         # ... and again at most this often until it settles
XCHECK_GIVE_UP_SECS = 2 * 86400
DEFAULT_READ_ET = {"KXTRUMPAPPROVE": (13, 0), "KXAPRPOTUS": (11, 0)}
# ---- the model (FIT 2026-10-25, see the docstring) -------------------------------
SESSION_GAP_SECS = 600          # CMS stamps this close are one editing session
RATE_WEEKS = 8                  # session rates from this many trailing weeks
SLOT_PRIOR = 0.02               # pseudo-sessions per hour slot
BG_PER_HOUR = 0.01              # moves the stamps miss (drop-outs), per hour
MIN_SESSIONS = 20               # fewer dated sessions in RATE_WEEKS = a broken feed
MAX_SESSIONS = 80               # the session count's support in one horizon
# a session's move of the Average, in tenths of a point (symmetric)
JUMP: Dict[int, float] = {0: 0.2119}
for _k, _p in ((1, 0.2213), (2, 0.0784), (3, 0.0434), (4, 0.0240), (5, 0.0133),
               (6, 0.0074), (7, 0.0041), (8, 0.0023)):
    JUMP[_k] = JUMP[-_k] = _p

_EVENT_DATE_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})(?:-|$)")
_RULE_TIME_RE = re.compile(
    r"at (?:exactly )?(\d{1,2}):(\d{2}) ?([AP])\.?M\.? ET on ([A-Z][a-z]+)\.? (\d{1,2}), (\d{4})",
    re.I)
_CMS_RE = re.compile(r"(\d{1,2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_last_public = [0.0]


class FeedError(ValueError):
    """The RCP feed was read but fails a check: no fairs until a good read."""


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} [approval] {msg}", flush=True)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            js = json.load(f)
        return js if isinstance(js, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


# ---- Eastern time ----------------------------------------------------------------
def _us_dst(dt_utc: datetime) -> bool:
    """US daylight time (2nd Sunday of March 07:00Z .. 1st Sunday of November
    06:00Z) -- the fallback when there is no tz database."""
    y = dt_utc.year
    mar = date(y, 3, 8) + timedelta(days=(6 - date(y, 3, 8).weekday()) % 7)
    nov = date(y, 11, 1) + timedelta(days=(6 - date(y, 11, 1).weekday()) % 7)
    return (datetime(y, mar.month, mar.day, 7, tzinfo=timezone.utc) <= dt_utc
            < datetime(y, nov.month, nov.day, 6, tzinfo=timezone.utc))


def to_et(dt_utc: datetime) -> datetime:
    if ET is not None:
        return dt_utc.astimezone(ET)
    return dt_utc.astimezone(timezone(timedelta(hours=-4 if _us_dst(dt_utc) else -5)))


def et_instant(d: date, hour: int, minute: int) -> datetime:
    """`hour:minute` ET on `d`, in UTC."""
    if ET is not None:
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=ET).astimezone(timezone.utc)
    edt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc) + timedelta(hours=4)
    return edt if _us_dst(edt) else edt + timedelta(hours=1)


# ---- the RCP feed ----------------------------------------------------------------
def _approve(row: dict) -> Optional[float]:
    for c in row.get("candidate") or []:
        if isinstance(c, dict) and str(c.get("name") or "").strip().lower() == "approve":
            try:
                return float(str(c.get("value")).strip().rstrip("%"))
            except (TypeError, ValueError):
                return None
    return None


def _cms_ts(s) -> Optional[float]:
    """RCP's "Fri, 23 Oct 2026 09:10:09 -0500" as epoch seconds (parsed by
    hand: strptime's %b follows the machine's locale)."""
    m = _CMS_RE.search(str(s or ""))
    mo = _MONTHS.get(m.group(2).lower()) if m else None
    if not mo:
        return None
    d, _, y, hh, mi, ss, off = m.groups()
    sign = -1 if off[0] == "-" else 1
    try:
        tz = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
        return datetime(int(y), mo, int(d), int(hh), int(mi), int(ss), tzinfo=tz).timestamp()
    except ValueError:
        return None


def session_times(entries: List[Tuple[int, float]]) -> List[float]:
    """Editing sessions (epoch s) from the polls' (id, updated): a poll's
    creation is the least "updated" of it and every later id; its own
    stamp counts too when it is a later edit; stamps within
    SESSION_GAP_SECS of the one before are the same session."""
    stamps: List[float] = []
    low = math.inf
    for _, up in sorted(entries, reverse=True):
        low = min(low, up)
        stamps.append(low)
        if up - low > 300:
            stamps.append(up)
    out: List[float] = []
    last = None
    for t in sorted(stamps):
        if last is None or t - last > SESSION_GAP_SECS:
            out.append(t)
        last = t
    return out


def parse_feed(js, now: Optional[datetime] = None) -> dict:
    """The 2nd-term table of the feed: {"value", "dates", "build", "n_in",
    "mean_in", "polls_in", "n_polls", "newest_update", "sessions"}; a
    FeedError when any check fails (see the module docstring)."""
    if not isinstance(js, dict) or not isinstance(js.get("poll"), list):
        raise FeedError("no poll list")
    rows = js["poll"]
    i0 = next((i for i, r in enumerate(rows) if isinstance(r, dict)
               and r.get("type") == "rcp_average" and str(r.get("id")) == RCP_POLL_ID), None)
    if i0 is None:
        raise FeedError(f"no RCP Average row {RCP_POLL_ID}")
    value = _approve(rows[i0])
    if value is None or not VALUE_RANGE[0] <= value <= VALUE_RANGE[1]:
        raise FeedError(f"Average Approve unreadable or out of range: {value!r}")
    block = []
    for r in rows[i0 + 1:]:
        if not isinstance(r, dict) or r.get("type") == "rcp_average":
            break
        block.append(r)
    polls_in = []
    for r in block:
        if r.get("type") != "poll_rcp_avg":
            continue
        a = _approve(r)
        if a is None or not 0.0 < a < 100.0:
            raise FeedError(f"in-average poll {r.get('id')} has no Approve")
        polls_in.append({"id": str(r.get("id")), "pollster": str(r.get("pollster") or ""),
                         "start": r.get("data_start_date"), "end": r.get("data_end_date"),
                         "approve": a, "updated": r.get("updated")})
    if not MIN_IN <= len(polls_in) <= MAX_IN:
        raise FeedError(f"{len(polls_in)} in-average polls")
    mean_in = sum(p["approve"] for p in polls_in) / len(polls_in)
    if abs(mean_in - value) > 0.05 + 1e-9:
        raise FeedError(f"Average {value} is not the mean of its {len(polls_in)} polls "
                        f"({mean_in:.3f})")
    entries = []
    for r in block:
        up = _cms_ts(r.get("updated"))
        try:
            pid = int(r.get("id"))
        except (TypeError, ValueError):
            pid = None
        if up is not None and pid is not None:
            entries.append((pid, up))
    sessions = session_times(entries)
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    recent = [t for t in sessions if now_ts - RATE_WEEKS * 7 * 86400 <= t <= now_ts + 3600]
    if len(recent) < MIN_SESSIONS:
        raise FeedError(f"only {len(recent)} dated editing sessions in {RATE_WEEKS} weeks")
    newest = max((up for _, up in entries), default=None)
    return {"value": value, "dates": str(rows[i0].get("date") or ""),
            "build": str((js.get("moduleInfo") or {}).get("lastBuildDate") or ""),
            "n_in": len(polls_in), "mean_in": round(mean_in, 4), "polls_in": polls_in,
            "n_polls": len(block),
            "newest_update": _iso(datetime.fromtimestamp(newest, timezone.utc)) if newest else None,
            "sessions": [round(t) for t in recent]}


def _http_get(url: str, headers: Optional[dict] = None) -> Tuple[Optional[str], dict]:
    """(body, response headers); body None on 304 Not Modified."""
    h = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    h.update(headers or {})
    r = requests.get(url, headers=h, timeout=HTTP_TIMEOUT)
    if r.status_code == 304:
        return None, dict(r.headers)
    r.raise_for_status()
    return r.text, dict(r.headers)


def fetch_feed(get_text: Optional[Callable] = None, etag: Optional[str] = None,
               last_modified: Optional[str] = None, now: Optional[datetime] = None
               ) -> Tuple[Optional[dict], dict]:
    """(the parsed table, or None when unchanged (304); {"etag",
    "last_modified"}). `get_text(url, headers)` may return the body, None
    (304), or (body, headers). Raises on a network error (the caller keeps
    its last read) and FeedError on a bad table."""
    h = {}
    if etag:
        h["If-None-Match"] = etag
    if last_modified:
        h["If-Modified-Since"] = last_modified
    res = (get_text or _http_get)(RCP_FEED_URL, h)
    text, hdr = res if isinstance(res, tuple) else (res, {})
    if text is None:
        return None, {}
    try:
        js = json.loads(text)
    except ValueError as e:
        raise FeedError(f"not JSON ({e})")
    hdr = {str(k).lower(): v for k, v in (hdr or {}).items()}
    return parse_feed(js, now), {"etag": hdr.get("etag"), "last_modified": hdr.get("last-modified")}


# ---- Kalshi ----------------------------------------------------------------------
def _signed_or_public(get_json: Optional[Callable], params: dict) -> dict:
    if get_json is not None:
        try:
            js = get_json("/markets", dict(params))
            if isinstance(js, dict):
                return js
        except Exception:
            pass
    for attempt in range(3):
        wait = PUBLIC_SPACING_SECS - (time.time() - _last_public[0])
        if wait > 0:
            time.sleep(wait)
        _last_public[0] = time.time()
        r = requests.get(KALSHI_MARKETS_URL, params=params, timeout=HTTP_TIMEOUT)
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 2:
            time.sleep(3 * (attempt + 1) ** 2)
            continue
        r.raise_for_status()
        return r.json() or {}
    return {}


def event_date(event: str) -> Optional[date]:
    """KXTRUMPAPPROVE-26OCT26 -> 2026-10-26."""
    m = _EVENT_DATE_RE.search(event or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%y%b%d").date()
    except ValueError:
        return None


def read_time(m: dict, series: str) -> Optional[datetime]:
    """The settlement read instant (UTC): rules_primary's "at exactly 1:00 PM
    ET on Oct 26, 2026", else the series' usual hour on the ticker date."""
    mt = _RULE_TIME_RE.search(str(m.get("rules_primary") or ""))
    if mt:
        hh, mm, ap, mon, dd, yy = mt.groups()
        mo = _MONTHS.get(mon[:3].lower())
        if mo:
            try:
                return et_instant(date(int(yy), mo, int(dd)),
                                  int(hh) % 12 + (12 if ap.upper() == "P" else 0), int(mm))
            except ValueError:
                pass
    d = event_date(str(m.get("event_ticker") or ""))
    if d is None or series not in DEFAULT_READ_ET:
        return None
    return et_instant(d, *DEFAULT_READ_ET[series])


def bucket(m: dict) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """The YES range in tenths, inclusive (None = unbounded): "less" K ->
    (None, 10K - 1), "greater" K -> (10K + 1, None), "between" a..b ->
    (10a, 10b)."""
    kind = str(m.get("strike_type") or "")

    def tenths(v) -> Optional[int]:
        try:
            return int(round(float(v) * 10))
        except (TypeError, ValueError):
            return None
    lo, hi = tenths(m.get("floor_strike")), tenths(m.get("cap_strike"))
    if kind == "less" and hi is not None:
        return None, hi - 1
    if kind == "greater" and lo is not None:
        return lo + 1, None
    if kind == "between" and lo is not None and hi is not None and lo <= hi:
        return lo, hi
    return None


def _cents(m: dict, key: str) -> Optional[float]:
    for k, scale in ((key + "_dollars", 100.0), (key, 1.0)):
        v = m.get(k)
        if v not in (None, ""):
            try:
                return float(v) * scale
            except (TypeError, ValueError):
                pass
    return None


def fetch_markets(get_json: Optional[Callable] = None) -> Dict[str, dict]:
    """Open markets of both series: ticker -> {"event", "series", "lo", "hi",
    "read_at" (ISO-Z), "bid", "ask" (cents, for the dry run)}."""
    out: Dict[str, dict] = {}
    for s in SERIES:
        cursor = None
        for _ in range(10):
            params = {"series_ticker": s, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            js = _signed_or_public(get_json, params)
            for m in js.get("markets") or []:
                t, ev = str(m.get("ticker") or ""), str(m.get("event_ticker") or "")
                b, rt = bucket(m), read_time(m, s)
                if t and ev and b and rt:
                    out[t] = {"event": ev, "series": s, "lo": b[0], "hi": b[1],
                              "read_at": _iso(rt), "bid": _cents(m, "yes_bid"),
                              "ask": _cents(m, "yes_ask")}
            cursor = js.get("cursor")
            if not cursor:
                break
    return out


# ---- the model -------------------------------------------------------------------
def slot_probs(sessions: List[float], now: datetime) -> Dict[Tuple[int, int], float]:
    """P(a session) in each (ET weekday, ET hour) slot: the share of the
    last RATE_WEEKS weeks with one there (+ SLOT_PRIOR), and BG_PER_HOUR."""
    end = now.timestamp()
    start = end - RATE_WEEKS * 7 * 86400
    hit = set()
    for t in sessions:
        if start <= t < end:
            e = to_et(datetime.fromtimestamp(t, timezone.utc))
            hit.add((e.date(), e.hour))
    cnt: Dict[Tuple[int, int], int] = {}
    for d, h in hit:
        cnt[(d.weekday(), h)] = cnt.get((d.weekday(), h), 0) + 1
    out = {}
    for w in range(7):
        for h in range(24):
            q = (cnt.get((w, h), 0) + SLOT_PRIOR) / (RATE_WEEKS + 2 * SLOT_PRIOR)
            out[(w, h)] = 1.0 - (1.0 - q) * (1.0 - BG_PER_HOUR)
    return out


def session_count_pmf(p: Dict[Tuple[int, int], float], t0: datetime, t1: datetime) -> List[float]:
    """P(N = n) for the sessions in [t0, t1): a Bernoulli per ET hour slot
    (a partial slot scaled by its share of the hour)."""
    dist = [1.0]
    t = t0
    while t < t1:
        e = to_et(t)
        nxt = min(t1, t + timedelta(minutes=60 - e.minute, seconds=-e.second,
                                    microseconds=-e.microsecond))
        if nxt <= t:
            nxt = min(t1, t + timedelta(hours=1))
        q = p[(e.weekday(), e.hour)] * (nxt - t).total_seconds() / 3600.0
        new = [x * (1.0 - q) for x in dist] + [0.0]
        for n, x in enumerate(dist):
            new[n + 1] += x * q
        dist = new[:MAX_SESSIONS + 1]
        t = nxt
    return dist


def delta_pmf(cnt: List[float]) -> Dict[int, float]:
    """P(the Average moves by k tenths) for N ~ cnt, each session a JUMP."""
    out: Dict[int, float] = {}
    cur: Dict[int, float] = {0: 1.0}
    tail = sum(cnt)
    for n, pn in enumerate(cnt):
        if tail < 1e-10:
            break
        tail -= pn
        if n > 0:
            nxt: Dict[int, float] = {}
            for i, a in cur.items():
                for j, b in JUMP.items():
                    nxt[i + j] = nxt.get(i + j, 0.0) + a * b
            cur = {k: v for k, v in nxt.items() if v > 1e-12}
        if pn > 1e-12:
            for k, v in cur.items():
                out[k] = out.get(k, 0.0) + pn * v
    s = sum(out.values()) or 1.0
    return {k: v / s for k, v in out.items()}


def p_bucket(v10: int, pmf: Dict[int, float], lo: Optional[int], hi: Optional[int]) -> float:
    p = sum(q for k, q in pmf.items()
            if (lo is None or v10 + k >= lo) and (hi is None or v10 + k <= hi))
    return max(0.0, min(1.0, p))


# ---- the file -------------------------------------------------------------------
def _value_at(reads: List[list], at: datetime) -> Optional[float]:
    """The value read across `at`: every read within SETTLE_READ_WINDOW_SECS
    must agree (None when they do not, or there are none)."""
    vals = set()
    for ts, v in reads:
        t = _parse_iso(ts)
        if t is not None and abs((t - at).total_seconds()) <= SETTLE_READ_WINDOW_SECS:
            vals.add(float(v))
    return vals.pop() if len(vals) == 1 else None


def _settled_bucket(get_json: Optional[Callable], event: str
                    ) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """The YES bucket Kalshi settled `event` on, None until it has."""
    js = _signed_or_public(get_json, {"event_ticker": event, "limit": 50})
    yes = [m for m in js.get("markets") or [] if str(m.get("result") or "") == "yes"]
    return bucket(yes[0]) if len(yes) == 1 else None


def _read_feed(old: dict, now: datetime, get_text: Optional[Callable]
               ) -> Tuple[dict, list, Optional[str], float]:
    """The RCP step of write_fair_file: (rcp state, history, fault, tried_at)."""
    rcp = dict(old.get("rcp") or {})
    history: List[list] = list(old.get("history") or [])
    fault = old.get("fault")
    tried = float(old.get("rcp_tried_at") or 0)
    if now.timestamp() - tried < RCP_MIN_READ_SECS:
        return rcp, history, fault, tried
    tried = now.timestamp()
    try:
        parsed, hdr = fetch_feed(get_text, None if fault else rcp.get("etag"),
                                 None if fault else rcp.get("last_modified"), now)
    except FeedError as e:
        fault = f"{type(e).__name__}: {str(e)[:160]}"
        _log(f"! RCP feed fails its checks: {fault}; no fairs")
        return rcp, history, fault, tried
    except Exception as e:
        _log(f"! RCP feed read failed ({type(e).__name__}: {str(e)[:80]}); the last read "
             f"stands until the gate's TTL")
        return rcp, history, fault, tried
    if parsed is None:                             # 304: unchanged
        if rcp.get("value") is not None and not fault:
            rcp["read_at"] = _iso(now)
            if rcp.get("unconfirmed") is not None:
                rcp.pop("unconfirmed")             # an unchanged 2nd read confirms it
                rcp["value"] = rcp.pop("pending_value", rcp["value"])
                history.append([_iso(now), rcp["value"]])
                rcp["value_since"] = _iso(now)
        return rcp, history, fault, tried
    prev = rcp.get("value")
    v = parsed["value"]
    rcp.update({k: parsed[k] for k in ("dates", "build", "n_in", "mean_in", "n_polls",
                                       "newest_update", "polls_in", "sessions")})
    rcp["read_at"] = _iso(now)
    rcp["etag"], rcp["last_modified"] = hdr.get("etag"), hdr.get("last_modified")
    big = prev is not None and abs(v - float(prev)) > MAX_STEP + 1e-9
    if big and rcp.get("pending_value") != v:
        rcp["unconfirmed"], rcp["pending_value"] = v, v
        _log(f"! RCP Average {prev} -> {v} waits for a second read")
    else:
        rcp.pop("unconfirmed", None)
        rcp.pop("pending_value", None)
        if prev is None or float(prev) != v:
            history.append([_iso(now), v])
            rcp["value_since"] = _iso(now)
        rcp["value"] = v
    return rcp, history, None, tried


def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    get_text: Optional[Callable] = None,
                    markets: Optional[Dict[str, dict]] = None) -> Tuple[int, int]:
    """One pass: read the RCP feed (conditional, at most every
    RCP_MIN_READ_SECS), Kalshi's open markets (every MARKETS_TTL_SECS; a
    failed read reuses the cache), run the settlement cross-check, price
    every open market and write the file atomically. Returns (markets with a
    fair, events without one)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    rcp, history, fault, tried = _read_feed(old, now, get_text)
    history = [h for h in history
               if (_parse_iso(h[0]) or now) >= now - timedelta(days=HISTORY_DAYS)][-500:]

    # -- Kalshi
    mk_cache = old.get("open_markets") or {}
    markets_at = float(old.get("markets_at") or 0)
    if markets is None:
        if mk_cache and ts - markets_at < MARKETS_TTL_SECS:
            markets = mk_cache
        else:
            try:
                markets = fetch_markets(get_json)
                markets_at = ts
            except Exception as e:
                if not mk_cache:
                    raise
                _log(f"! Kalshi market read failed ({type(e).__name__}); reusing "
                     f"{len(mk_cache)} cached")
                markets = mk_cache

    # -- the reads around each event's read time, then the cross-check
    settle_reads: Dict[str, list] = dict(old.get("settle_reads") or {})
    xcheck: Dict[str, dict] = dict(old.get("xcheck") or {})
    for m in markets.values():
        xcheck.setdefault(m["event"], {"read_at": m["read_at"], "status": "pending"})
    fresh = rcp.get("read_at") == _iso(now) and rcp.get("value") is not None \
        and rcp.get("unconfirmed") is None and not fault
    for ev, x in xcheck.items():
        rt = _parse_iso(x.get("read_at"))
        if fresh and rt is not None and abs(ts - rt.timestamp()) <= SETTLE_READ_WINDOW_SECS:
            lst = settle_reads.setdefault(ev, [])
            lst.append([rcp["read_at"], rcp["value"]])
    for ev, x in sorted(xcheck.items()):
        rt = _parse_iso(x.get("read_at"))
        if (x.get("status") != "pending" or rt is None
                or ts < rt.timestamp() + XCHECK_AFTER_SECS
                or ts - float(x.get("tried") or 0) < XCHECK_EVERY_SECS):
            continue
        x["tried"] = ts
        ours = _value_at(settle_reads.get(ev) or [], rt)
        if ours is None:
            x["status"] = "no reads"
            continue
        try:
            b = _settled_bucket(get_json, ev)
        except Exception:
            b = None
        if b is None:
            if ts > rt.timestamp() + XCHECK_GIVE_UP_SECS:
                x["status"] = "unsettled"
            continue
        v10 = int(round(ours * 10))
        ok = (b[0] is None or v10 >= b[0]) and (b[1] is None or v10 <= b[1])
        x.update(status="ok" if ok else "MISMATCH", ours=ours, kalshi=list(b),
                 checked_at=_iso(now))
        if not ok:
            _log(f"! settlement cross-check FAILED on {ev}: we read {ours}, Kalshi settled "
                 f"{b}; no fairs until a later event agrees")
    done = sorted(((x.get("read_at") or "", x.get("checked_at") or "", ev)
                   for ev, x in xcheck.items() if x.get("status") in ("ok", "MISMATCH")))
    last_check = xcheck[done[-1][2]] if done else None
    keep = now - timedelta(days=HISTORY_DAYS)
    xcheck = {ev: x for ev, x in xcheck.items() if (_parse_iso(x.get("read_at")) or now) >= keep}
    settle_reads = {ev: v for ev, v in settle_reads.items() if ev in xcheck}

    # -- the fairs
    read_at = _parse_iso(rcp.get("read_at"))
    why = None
    if fault:
        why = f"feed fault: {fault}"
    elif rcp.get("value") is None or read_at is None or not rcp.get("sessions"):
        why = "no good read of the RCP feed yet"
    elif rcp.get("unconfirmed") is not None:
        why = f"unconfirmed move to {rcp['unconfirmed']} (from {rcp.get('value')})"
    elif last_check is not None and last_check.get("status") == "MISMATCH":
        why = f"the last settlement cross-check failed ({done[-1][2]})"
    out: Dict[str, dict] = {}
    event_fair: Dict[str, dict] = {}
    pmfs: Dict[str, Dict[int, float]] = {}
    probs = slot_probs(rcp.get("sessions") or [], now) if not why else {}
    for t, m in sorted(markets.items()):
        ev = m["event"]
        rt = _parse_iso(m["read_at"])
        if rt is None or rt <= now:
            event_fair.setdefault(ev, {"why": "past its read time"})
            continue
        if why:
            event_fair.setdefault(ev, {"why": why})
            continue
        if ev not in pmfs:
            cnt = session_count_pmf(probs, now, rt)
            pmfs[ev] = delta_pmf(cnt)
            mean = sum(k * q for k, q in pmfs[ev].items())
            sd = math.sqrt(max(0.0, sum(k * k * q for k, q in pmfs[ev].items()) - mean ** 2))
            event_fair[ev] = {"value": rcp["value"], "read_at": m["read_at"],
                              "hours": round((rt - now).total_seconds() / 3600.0, 2),
                              "sessions": round(sum(n * q for n, q in enumerate(cnt)), 2),
                              "p_unchanged": round(pmfs[ev].get(0, 0.0), 4),
                              "sd": round(sd / 10.0, 3)}
        v10 = int(round(float(rcp["value"]) * 10))
        out[t] = {"p": round(p_bucket(v10, pmfs[ev], m["lo"], m["hi"]), 4), "event": ev,
                  "fetched_at": rcp["read_at"], "lo": m["lo"], "hi": m["hi"],
                  "value": rcp["value"]}
    missing = sum(1 for e in event_fair.values() if "why" in e)
    payload = {"generated_at": _iso(now), "series": list(SERIES), "markets": out,
               "event_fair": event_fair, "fault": fault, "rcp": rcp, "rcp_tried_at": tried,
               "history": history, "settle_reads": settle_reads, "xcheck": xcheck,
               "open_markets": markets, "markets_at": markets_at,
               "model": {"jump": {str(k): v for k, v in sorted(JUMP.items())},
                         "rate_weeks": RATE_WEEKS, "slot_prior": SLOT_PRIOR,
                         "bg_per_hour": BG_PER_HOUR}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return len(out), missing


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(),
                                                  "approval_fair_dryrun.json"),
                    help="the fair file to write (by default never the bot's status dir)")
    ap.add_argument("--now", default=None, help="ISO time to price at (tests, replays)")
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out, now=_parse_iso(args.now) if args.now else None)
    js = _read_json(args.out)
    r = js.get("rcp") or {}
    print(f"RCP Average {r.get('value')} ({r.get('dates')}; {r.get('n_in')} polls, mean "
          f"{r.get('mean_in')}); read {r.get('read_at')}, feed built {r.get('build')}, "
          f"{len(r.get('sessions') or [])} sessions in {RATE_WEEKS} weeks")
    if js.get("fault"):
        print(f"FAULT: {js['fault']}")
    mk = js.get("open_markets") or {}
    for ev, e in sorted((js.get("event_fair") or {}).items()):
        print(f"{ev}: " + (e["why"] if "why" in e else
                           f"read at {e['read_at']} ({e['hours']}h): {e['sessions']} sessions "
                           f"expected, P(unchanged) {100 * e['p_unchanged']:.0f}%, sd {e['sd']}"))
        rows = [(t, m) for t, m in (js.get("markets") or {}).items() if m["event"] == ev]
        for t, m in sorted(rows, key=lambda kv: (kv[1]["lo"] is not None, kv[1]["lo"] or 0)):
            b, a = (mk.get(t) or {}).get("bid"), (mk.get(t) or {}).get("ask")
            book = f"{'-' if b is None else f'{b:.0f}'} x {'-' if a is None else f'{a:.0f}'}"
            print(f"  {t:32s} fair {100 * m['p']:5.1f}c   book {book}")
    print(f"{ok} markets priced, {miss} events without a fair")
    return 0


if __name__ == "__main__":
    sys.exit(main())
