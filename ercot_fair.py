#!/usr/bin/env python3
"""
ercot_fair.py -- fair values for KXTXERCOTPEAKD, ERCOT's daily peak demand
(Kalshi's "Texas ERCOT peak electricity demand": 19 strikes x $50/day
liquidity programs = $950/day; the 9/02 scan rejected it as "realtime grid
data" and the IMM has never quoted it).

THE MARKET. KXTXERCOTPEAKD-<YYMONDD>-T<K>: "If the highest hourly total
system load reported for the ERCOT grid on <D> is above K MW". The ticker's
date is D + 1; the module reads D from the rules. Settlement: the highest
TOTAL in the first complete "Actual System Load by Forecast Zone" CSV
(NP6-346-CD: hour-ending averages in MW, posted ~05:50 CT on D+1);
5-minute / instantaneous readings do not count. NP6-346's daily max equals
29 of the last 32 settlements to the cent (Kalshi entered the other 3
truncated to whole MW). "greater" strikes: 19 x 500 MW since 9/23 (10-16 x
1,000 MW in Aug-Sep, 31 on 10/01). An event lists at 04:55Z (23:55 CDT on
D-1; 10/01 listed at 19:50Z) and closes 24 h later, as the next one lists;
the programs run the same 24 h.

THE DATA. SWD_URL, ERCOT's public dashboard feed (no key; the website's
"load forecast vs actual" chart): previousDay / currentDay / nextDay x 24
hour-ending rows of currentLoadForecast (ERCOT's latest hourly Seven-Day
Load Forecast run = NP3-560-CD's SystemTotal), dayAheadForecast and
systemLoad (each finished hour's actual = NP6-346-CD's TOTAL within 0.01
MW). It re-posts hourly at ~:07-:15 (lastUpdated), each run reaching it
before MIS posts that run's CSV at :30; the module reads it every call.

THE MODEL. The day's peak is max(the hours already run, the rest), and
    log(rest-of-day peak) = log(F) + BIAS + t4 * scale,
    scale = S0 * (1 + K_SWING * |swing|) * LEAD_MULT
F = the latest run's top hour among those not yet run; swing = log(F / D-1's
actual peak) -- front days miss wider and lower; t4 = Student-t, 4 dof (the
misses are fat-tailed: the day-ahead missed 10/01/2026 by -8.9% and 10/16
by -5.9%; 10/16 peaked in HE01). Fitted by ML on 1,209 days of ERCOT's
day-ahead forecast vs actual (EIA-930 ERCO, 7/2023-10/2026; EIA's daily max
vs the 58 settlements: sd 15 MW, worst 70): S0 1.42%, K_SWING 9.8 (scale
1.4% on a calm day, 2.4% at a 7% swing; the t4 sd is 1.41x the scale,
1,250-2,100 MW at 62 GW). BIAS: the 3-year location is -0.8% (ERCOT runs
high), the last 60 days ~0; -0.4% splits it (flattest event PIT over 365
days). The live run sits 6-17 h before the peak in the window, the
day-ahead ~27 h; ERCOT's backcasts put the 6-hour-ahead MAPE at ~0.88x the
day-ahead's, so LEAD_MULT 1 keeps the day-ahead width -- wide on purpose.
A strike's fair: 1 if an hour already ran above K, else
1 - T4((ln K - mu) / scale).

THE WINDOW. Priced from listing until CUTOFF_HOUR_LOCAL (10:00 Central) on
D, and never later than PEAK_GAP_HOURS before the forecast's peak hour (or
before a morning hour within NEAR_PEAK_FRAC of it: Dec-Feb days peak by
HE10 on 35-63% of days), nor once the hours already run reach
REALIZED_STOP_FRAC of what is left of the forecast. Then the market is
absent from "markets" and the gate stands it aside. Why there: the takers'
markout to settlement on 5-95c trades is +1.8c/contract 00-10h, -0.9c
10-12h, +3.7c 12-14h, +8.7c 14-17h (all 58 settled days); the realized load
says little before the ramp (the day-ahead miss's residual sd after the
last two hours: 2.98% at midnight, 2.71% at 10:00, 2.55% at noon, 2.00% at
15:00); the peak is HE16-18 on 80% of Sep-Nov days.

FAILS CLOSED: no fair when the feed's lastUpdated is older than
FEED_MAX_AGE_MIN (or from the future), the day is missing from it, the
rules do not name the hourly total system load and one parsable day, or a
strike type is unknown. A failed read reuses the file's last good feed
under its own fetched_at (the gate's TTL then ages it out) -- never a
fresh-looking fair.

BACKTEST (scratch walkforward3.py / candles_analysis.py: this module's own
event_fair + market_p on feed rows rebuilt from history, every settled event
8/28-10/24/2026, against Kalshi's hourly bid/ask candles):
  the live run this reads (ERCOT's hourly postings: 7 events 10/18-24, all
    the public archive holds): Brier 0.055-0.061 over all strikes; on the
    strike-hours with a two-sided book, fair 0.033-0.057 vs the market mid
    0.034-0.055 at every hour 01:00-10:00, |mid - fair| median 2-3c, p90
    8-18c; the mid pulls away from 15:00 (0.077 vs 0.100);
  the day-ahead alone (58 events, 954 strike-outcomes): Brier 0.087 at the
    open, 0.081 at 09:54 (log loss 0.28 / 0.27); it trails the mid all
    morning (03:00-10:00: 0.089-0.097 vs 0.076-0.094) -- hence the live run;
  BIAS 0 / -0.004 / -0.008: day-ahead Brier 0.086 / 0.087 / 0.091.
  Books: morning spreads median 13-15c (<= 2c 3-5% of the time); 5-7
  strikes two-sided with mids 5-95c in each hour 01:00-10:00.

The file is bway_fair's shape for the bot's loader (markets: p / event /
fetched_at, plus forecast_peak_mw / sd_mw diagnostics; event_fair says why
an event has no fair). Each refresh also records the hour's forecast peak
and, once a day is over, its actual peak into a sibling
<out>_history.json, so the model can be re-fitted on the live forecast.

usage: python ercot_fair.py [--out FILE]   (dry run: fairs vs Kalshi's book)
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
from datetime import time as dtime
from typing import Callable, Dict, List, Optional, Tuple

import requests

SERIES = "KXTXERCOTPEAKD"
SWD_URL = "https://www.ercot.com/api/1/services/read/dashboards/system-wide-demand.json"
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
USER_AGENT = "KL-IMM-ercot-fair/1.0"
HTTP_TIMEOUT = 20
EVENTS_TTL_SECS = 900            # Kalshi's open events (a new day lists at 04:55Z)
FEED_MAX_AGE_MIN = 100           # the feed re-posts hourly; one missed post is tolerated
CUTOFF_HOUR_LOCAL = 10.0         # Central time on the operating day
PEAK_GAP_HOURS = 6.0             # ... and no later than this before the forecast peak hour
MORNING_HE_MAX = 12              # ... or before a morning hour this close to it:
NEAR_PEAK_FRAC = 0.97
REALIZED_STOP_FRAC = 0.97        # the day's actual max this close to the forecast peak: stop
T_DOF = 4                        # the closed form in t4_cdf is for 4 dof
BIAS = -0.004                    # log(actual peak / forecast peak), t4 location
S0 = 0.0142                      # t4 scale on a no-swing day
K_SWING = 9.8                    # scale x (1 + K_SWING * |swing|)
SWING_CAP = 0.20
SWING_UNKNOWN = 0.06             # no previous-day actual peak: price it as a swing day
LEAD_MULT = 1.0                  # current run vs the day-ahead fit (kept wide)
P_MIN, P_MAX = 0.005, 0.995
MW_MIN, MW_MAX = 20000.0, 150000.0   # a value outside is a broken read
HISTORY_DAYS = 365
DRY_OUT_DEFAULT = os.path.join(tempfile.gettempdir(), "ercot_fair_dry.json")
_RULES_RE = re.compile(r"highest hourly total system load reported for the ERCOT grid on "
                       r"([A-Z][a-z]+ \d{1,2}, \d{4})", re.I)
_EVENT_DATE_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})$")
_GREATER = ("greater", "greater_or_equal")
_LESS = ("less", "less_or_equal")


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} [ercot] {msg}", flush=True)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            js = json.load(f)
        return js if isinstance(js, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _iso_z(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(s: str) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


# ---- Central time (US rules since 2007; stdlib only) ------------------------------
def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))


def central_offset_hours(t_utc: datetime) -> int:
    """-5 (CDT) or -6 (CST) at the UTC instant t_utc."""
    y = t_utc.year
    start = datetime.combine(_nth_sunday(y, 3, 2), dtime(8), tzinfo=timezone.utc)
    end = datetime.combine(_nth_sunday(y, 11, 1), dtime(7), tzinfo=timezone.utc)
    return -5 if start <= t_utc.astimezone(timezone.utc) < end else -6


def local_to_utc(day: date, hour: float) -> datetime:
    """Central wall-clock `hour` (may exceed 24 or be negative) on `day`, in UTC."""
    naive = datetime.combine(day, dtime(0)) + timedelta(hours=hour)
    for off in (-5, -6):
        t = (naive - timedelta(hours=off)).replace(tzinfo=timezone.utc)
        if central_offset_hours(t) == off:
            return t
    return (naive + timedelta(hours=6)).replace(tzinfo=timezone.utc)   # the skipped hour


def central_date(t_utc: datetime) -> date:
    return (t_utc.astimezone(timezone.utc) + timedelta(hours=central_offset_hours(t_utc))).date()


# ---- ERCOT's dashboard feed -----------------------------------------------------------
def _http_text(url: str, headers: Optional[dict] = None) -> str:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    r = requests.get(url, headers=h, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_feed(js) -> dict:
    """{"updated": ISO-Z or None, "days": {ISO day: [[he, dst, forecast, day_ahead,
    actual], ...]}} from SWD_URL's JSON. A day block with fewer than 23 rows or
    a forecast outside MW_MIN..MW_MAX is dropped (a partial or broken post)."""
    out: Dict[str, object] = {"updated": None, "days": {}}
    if not isinstance(js, dict):
        return out
    up = str(js.get("lastUpdated") or "")
    try:
        out["updated"] = _iso_z(datetime.strptime(up, "%Y-%m-%d %H:%M:%S%z"))
    except ValueError:
        pass
    days: Dict[str, list] = {}
    for key in ("previousDay", "currentDay", "nextDay"):
        blk = js.get(key)
        if not isinstance(blk, dict):
            continue
        day = str(blk.get("dayDate") or "")[:10]
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        rows = []
        for r in blk.get("data") or []:
            if not isinstance(r, dict):
                continue
            he = _num(r.get("hourEnding"))
            fc = _num(r.get("currentLoadForecast"))
            if he is None or fc is None:
                continue
            act = _num(r.get("systemLoad"))
            rows.append([int(he), str(r.get("dstFlag") or "N"), fc,
                         _num(r.get("dayAheadForecast")), act])
        if len(rows) < 23 or not all(MW_MIN <= r[2] <= MW_MAX for r in rows):
            continue
        rows.sort(key=lambda r: (r[0], r[1] == "Y"))
        days[day] = rows
    out["days"] = days
    return out


def fetch_feed(get_text: Optional[Callable] = None) -> dict:
    return parse_feed(json.loads((get_text or _http_text)(SWD_URL, None)))


def feed_fresh(feed: dict, now: datetime) -> bool:
    """lastUpdated within FEED_MAX_AGE_MIN (and not from the future: a clock or
    zone slip reads as a stale feed)."""
    up = _parse_utc(feed.get("updated") or "")
    return bool(up and feed.get("days")
                and -1800 <= (now - up).total_seconds() <= FEED_MAX_AGE_MIN * 60)


# ---- Kalshi ---------------------------------------------------------------------------
def _signed_or_public(get_json: Optional[Callable], params: dict) -> dict:
    if get_json is not None:
        try:
            js = get_json("/markets", dict(params))
            if isinstance(js, dict):
                return js
        except Exception:
            pass
    for attempt in range(3):
        r = requests.get(KALSHI_MARKETS_URL, params=params, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 2:
            time.sleep(3 * (attempt + 1) ** 2)
            continue
        r.raise_for_status()
        return r.json() or {}
    return {}


def rules_day(rules: str) -> Optional[str]:
    """'If the highest hourly total system load reported for the ERCOT grid on
    October 25, 2026 is above ...' -> '2026-10-25'; None for other rules."""
    m = _RULES_RE.search(" ".join(str(rules or "").split()))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def ticker_day(event: str) -> Optional[str]:
    """KXTXERCOTPEAKD-26OCT27 -> '2026-10-26' (the ticker names D + 1)."""
    m = _EVENT_DATE_RE.search(event or "")
    if not m:
        return None
    try:
        return (datetime.strptime(m.group(1), "%y%b%d").date() - timedelta(days=1)).isoformat()
    except ValueError:
        return None


def fetch_events(get_json: Optional[Callable] = None) -> Dict[str, dict]:
    """Open events (see parse_events), from the signed reader or the public URL."""
    return parse_events(_signed_or_public(get_json, {"series_ticker": SERIES,
                                                     "status": "open", "limit": 200}))


def parse_events(js: dict) -> Dict[str, dict]:
    """/markets JSON -> event -> {"day": operating day (ISO) or None, "why": why
    not priceable or "", "close": ISO-Z, "markets": {ticker: {"type", "floor",
    "cap"}}}."""
    out: Dict[str, dict] = {}
    for m in js.get("markets") or []:
        ev, t = str(m.get("event_ticker") or ""), str(m.get("ticker") or "")
        if not ev or not t:
            continue
        e = out.setdefault(ev, {"day": None, "why": "", "close": None, "markets": {},
                                "days": set()})
        e["days"].add(rules_day(m.get("rules_primary") or ""))
        close = _parse_utc(m.get("close_time") or "")
        if close is not None and (e["close"] is None or _iso_z(close) < e["close"]):
            e["close"] = _iso_z(close)
        e["markets"][t] = {"type": str(m.get("strike_type") or ""),
                           "floor": _num(m.get("floor_strike")),
                           "cap": _num(m.get("cap_strike"))}
    for ev, e in out.items():
        days = e.pop("days")
        if len(days) != 1 or None in days:
            e["why"] = "rules do not name one operating day for the hourly total system load"
            continue
        e["day"] = days.pop()
        if ticker_day(ev) not in (None, e["day"]):
            _log(f"{ev}: rules say {e['day']}, the ticker {ticker_day(ev)} -- the rules rule")
    return out


def events_stale(ev_cache: Dict[str, dict], now: datetime) -> bool:
    """A cached event has closed: the next day has just listed (each event
    closes as the next one opens)."""
    for e in ev_cache.values():
        c = _parse_utc(e.get("close") or "")
        if c is not None and c <= now:
            return True
    return False


# ---- the model ---------------------------------------------------------------------------
def t4_cdf(x: float) -> float:
    """Student-t CDF, 4 degrees of freedom (exact closed form)."""
    u = x * x / 4.0
    return 0.5 + 0.375 * (x / math.sqrt(1.0 + u)) * (1.0 - u / (3.0 * (1.0 + u)))


def scale_for(swing: Optional[float]) -> float:
    sw = SWING_UNKNOWN if swing is None else min(abs(swing), SWING_CAP)
    return S0 * (1.0 + K_SWING * sw) * LEAD_MULT


def p_above(mu: float, scale: float, k: float) -> float:
    """P(peak > k) for log(peak) ~ mu + t4 * scale."""
    if k <= 0:
        return 1.0
    return 1.0 - t4_cdf((math.log(k) - mu) / scale)


def market_p(mk: dict, mu: float, scale: float,
             realized: Optional[float] = None) -> Optional[float]:
    """A strike's P(YES) when the day's peak is max(realized, the rest of the
    day), the rest ~ mu + t4 * scale in logs; None for a structure this does
    not price. An hour already above a "greater" strike has settled it YES."""
    typ, lo, hi = mk.get("type"), mk.get("floor"), mk.get("cap")
    r = realized or 0.0
    if typ in _GREATER and lo is not None:
        p = 1.0 if r > lo else p_above(mu, scale, lo)
    elif typ in _LESS and hi is not None:
        p = 0.0 if r >= hi else 1.0 - p_above(mu, scale, hi)
    elif typ == "between" and lo is not None and hi is not None and hi > lo:
        if r > hi:
            p = 0.0
        elif r >= lo:
            p = 1.0 - p_above(mu, scale, hi)
        else:
            p = p_above(mu, scale, lo) - p_above(mu, scale, hi)
    else:
        return None
    return min(P_MAX, max(P_MIN, p))


def day_stats(rows: List[list]) -> dict:
    """The forecast's peak over the whole day and over the hours not yet run
    (+ its hour ending), the day-ahead peak, the actual max so far and how many
    hours it covers."""
    pk = max(rows, key=lambda r: r[2])
    rest = [r for r in rows if r[4] is None]
    rk = max(rest, key=lambda r: r[2]) if rest else None
    acts = [r[4] for r in rows if r[4] is not None]
    das = [r[3] for r in rows if r[3] is not None]
    return {"f_peak": pk[2], "peak_he": pk[0],
            "f_rest": rk[2] if rk else None, "rest_he": rk[0] if rk else None,
            "da_peak": max(das) if das else None,
            "act_max": max(acts) if acts else None, "act_hours": len(acts)}


def gap_he(rows: List[list]) -> int:
    """The hour ending PEAK_GAP_HOURS counts back from: the forecast's peak hour,
    or an earlier morning hour (HE <= MORNING_HE_MAX) within NEAR_PEAK_FRAC of
    it -- a winter day's morning hump can set the day's max (Dec-Feb days
    peak by HE10 on 35-63% of days)."""
    pk = max(rows, key=lambda r: r[2])
    first = pk[0]
    for r in rows:
        if r[0] <= MORNING_HE_MAX and r[2] >= NEAR_PEAK_FRAC * pk[2]:
            first = min(first, r[0])
    return first


def quote_until(day: date, he: int) -> datetime:
    """The earlier of CUTOFF_HOUR_LOCAL on the day and PEAK_GAP_HOURS before the
    start of hour ending `he` (see gap_he)."""
    return min(local_to_utc(day, CUTOFF_HOUR_LOCAL),
               local_to_utc(day, he - 1 - PEAK_GAP_HOURS))


def event_fair(day_iso: str, feed_days: Dict[str, list], now: datetime) -> dict:
    """The event's model inputs and either {"mu", "sc", "realized"} or {"why"}.
    The peak is max(the hours already run, the rest of the day); the rest is
    the forecast over the hours without an actual."""
    rows = feed_days.get(day_iso)
    if not rows:
        return {"why": "no ERCOT forecast for the day in the feed"}
    d = date.fromisoformat(day_iso)
    s = day_stats(rows)
    prev = feed_days.get((d - timedelta(days=1)).isoformat())
    ps = day_stats(prev) if prev else None
    y_prev = ps["act_max"] if ps and ps["act_hours"] >= 20 else None
    out = {"forecast_peak_mw": round(s["f_peak"]), "peak_he": s["peak_he"],
           "rest_peak_mw": round(s["f_rest"]) if s["f_rest"] else None,
           "rest_peak_he": s["rest_he"],
           "da_peak_mw": round(s["da_peak"]) if s["da_peak"] else None,
           "realized_max_mw": round(s["act_max"]) if s["act_max"] else None,
           "prev_peak_mw": round(y_prev) if y_prev else None}
    if s["f_rest"] is None:
        out["why"] = "every hour of the day has run"
        return out
    f_day = max(s["f_rest"], s["act_max"] or 0.0)
    swing = math.log(f_day / y_prev) if y_prev else None
    g = gap_he(rows)
    until = quote_until(d, g)
    out.update(swing=round(swing, 4) if swing is not None else None, gap_he=g,
               quote_until=_iso_z(until))
    if now >= until:
        out["why"] = "past the quoting cutoff (the day's load is showing)"
        return out
    if s["act_max"] and s["act_max"] >= REALIZED_STOP_FRAC * s["f_rest"]:
        out["why"] = "the day's realized load is near what is left of the forecast"
        return out
    mu = math.log(s["f_rest"]) + BIAS
    sc = scale_for(swing)
    out.update(mu_mw=round(math.exp(mu)), scale=round(sc, 5),
               sd_mw=round(math.sqrt(T_DOF / (T_DOF - 2.0)) * sc * s["f_rest"]))
    out["mu"], out["sc"], out["realized"] = mu, sc, s["act_max"]
    return out


# ---- the history -----------------------------------------------------------------------------
def record_history(hist: dict, feed: dict, now: datetime) -> bool:
    """Fold the feed into {day: {"da": peak, "fc": {hour-Z: peak}, "y": actual
    peak}}: the first forecast read of each UTC hour from 18:00 Central the day
    before to 18:00 on the day, the day-ahead peak once, a complete day's
    actual peak once it is over. Returns whether it changed."""
    changed = False
    hour = now.strftime("%Y-%m-%dT%H:00Z")
    today = central_date(now)
    for day, rows in (feed.get("days") or {}).items():
        d = date.fromisoformat(day)
        s = day_stats(rows)
        h = hist.setdefault(day, {"fc": {}})
        if s["da_peak"] and h.get("da") is None:
            h["da"], changed = round(s["da_peak"], 1), True
        if local_to_utc(d, -6) <= now < local_to_utc(d, 18) and hour not in h["fc"]:
            h["fc"][hour], changed = round(s["f_peak"], 1), True
        if d < today and s["act_hours"] >= 23 and h.get("y") is None:
            h["y"], changed = round(s["act_max"], 2), True
    cut = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    for day in [d for d in hist if d < cut]:
        hist.pop(day)
        changed = True
    return changed


def history_path_for(path: str) -> str:
    return os.path.splitext(path)[0] + "_history.json"


# ---- the file ----------------------------------------------------------------------------------
def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    get_text: Optional[Callable] = None,
                    events: Optional[Dict[str, dict]] = None,
                    history_path: Optional[str] = None) -> Tuple[int, int]:
    """Build and atomically write the fair file; returns (markets with a fair,
    open events without one). ERCOT's feed is read every call (a failed or
    stale read falls back to the file's last good copy, under that copy's read
    time); Kalshi's open events every EVENTS_TTL_SECS, sooner once a cached
    event's day is over."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    feed, feed_at = None, None
    try:
        f = fetch_feed(get_text)
        if not feed_fresh(f, now):
            raise ValueError(f"feed lastUpdated {f.get('updated')} with "
                             f"{len(f.get('days') or {})} day(s)")
        feed, feed_at = f, ts
    except Exception as e:
        cached = old.get("feed") or {}
        if cached and feed_fresh(cached, now):
            feed, feed_at = cached, float(old.get("feed_at") or 0)
        _log(f"! ERCOT feed read failed ({type(e).__name__}: {str(e)[:100]})"
             + ("; the file's copy stands in" if feed else "; no fairs"))
    ev_cache = old.get("events") or {}
    events_at = float(old.get("events_at") or 0)
    if events is None:
        if ev_cache and ts - events_at < EVENTS_TTL_SECS and not events_stale(ev_cache, now):
            events = ev_cache
        else:
            try:
                events = fetch_events(get_json)
                events_at = ts
            except Exception as e:
                _log(f"! Kalshi event read failed ({type(e).__name__}); reusing "
                     f"{len(ev_cache)} cached")
                events = ev_cache
    fetched = _iso_z(datetime.fromtimestamp(feed_at, timezone.utc)) if feed_at else None
    markets: Dict[str, dict] = {}
    ev_out: Dict[str, dict] = {}
    missing = 0
    for ev, e in sorted(events.items()):
        if e.get("why") or not e.get("day"):
            ev_out[ev] = {"day": e.get("day"), "why": e.get("why") or "no operating day"}
            missing += 1
            continue
        if feed is None:
            ev_out[ev] = {"day": e["day"], "why": "no fresh ERCOT feed"}
            missing += 1
            continue
        info = event_fair(e["day"], feed["days"], now)
        mu, sc, realized = info.pop("mu", None), info.pop("sc", None), info.pop("realized", None)
        info["day"] = e["day"]
        ev_out[ev] = info
        if mu is None:
            missing += 1
            continue
        n = 0
        for t, mk in sorted(e["markets"].items()):
            p = market_p(mk, mu, sc, realized)
            if p is None:
                continue
            markets[t] = {"p": round(p, 4), "event": ev, "fetched_at": fetched,
                          "strike": mk.get("floor") if mk.get("type") in _GREATER
                          else mk.get("cap"), "type": mk.get("type"),
                          "forecast_peak_mw": info["rest_peak_mw"],
                          "sd_mw": info["sd_mw"], "day": e["day"]}
            n += 1
        if not n:
            info["why"] = "no strike structure this prices"
            missing += 1
    out = {"generated_at": _iso_z(now), "series": SERIES,
           "model": {"bias": BIAS, "s0": S0, "k_swing": K_SWING, "t_dof": T_DOF,
                     "lead_mult": LEAD_MULT, "cutoff_hour_local": CUTOFF_HOUR_LOCAL,
                     "peak_gap_hours": PEAK_GAP_HOURS,
                     "realized_stop_frac": REALIZED_STOP_FRAC},
           "markets": markets, "event_fair": ev_out,
           "events": events, "events_at": events_at,
           "feed": feed or old.get("feed") or {}, "feed_at": feed_at
           if feed is not None else float(old.get("feed_at") or 0)}
    _write_json(path, out)
    if feed is not None and feed_at == ts:
        hp = history_path or history_path_for(path)
        hist = _read_json(hp)
        if record_history(hist, feed, now):
            try:
                _write_json(hp, hist)
            except OSError as e:
                _log(f"! history write failed ({e})")
    return len(markets), missing


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DRY_OUT_DEFAULT,
                    help="the fair file to write (default: a temp-dir dry-run file)")
    args = ap.parse_args(argv)
    # one public read of the open markets serves both the events and the books
    mjs = _signed_or_public(None, {"series_ticker": SERIES, "status": "open", "limit": 200})
    ok, miss = write_fair_file(args.out, events=parse_events(mjs))
    js = _read_json(args.out)
    fa = js.get("feed_at") or 0
    print(f"ERCOT feed updated {(js.get('feed') or {}).get('updated')}, read "
          + (f"{datetime.fromtimestamp(fa, timezone.utc):%Y-%m-%d %H:%M}Z" if fa else "never"))
    for ev, e in sorted((js.get("event_fair") or {}).items()):
        head = (f"{ev} (day {e.get('day')}): forecast peak {e.get('forecast_peak_mw')} MW "
                f"HE{e.get('peak_he')} (rest of day {e.get('rest_peak_mw')}), day-ahead "
                f"{e.get('da_peak_mw')}, prev day {e.get('prev_peak_mw')}, swing "
                f"{e.get('swing')}, so far {e.get('realized_max_mw')}; quote until "
                f"{e.get('quote_until')}")
        print(head + (f" -- NO FAIR: {e['why']}" if e.get("why") else
                      f"; center {e.get('mu_mw')} MW, sd {e.get('sd_mw')} MW"))
    fair = js.get("markets") or {}
    for m in sorted(mjs.get("markets") or [], key=lambda m: (m.get("event_ticker") or "",
                                                              _num(m.get("floor_strike")) or 0)):
        t = m.get("ticker")
        bid, ask = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
        f = f"{100 * fair[t]['p']:5.1f}c" if t in fair else "    --"
        print(f"  {t}: fair {f}   book {'-' if bid is None else round(100 * bid)} x "
              f"{'-' if ask is None else round(100 * ask)}")
    print(f"{ok} markets priced, {miss} events without a fair; wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
