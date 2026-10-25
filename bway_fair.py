#!/usr/bin/env python3
"""
bway_fair.py -- fair values for KXBWAYATTENDANCEW, Broadway's weekly total
attendance (Jack 2026-10-25: "Can you build a fair value for this?" on the
watch's item, then "Yes enroll it").

Each event is one Monday-Sunday week; each strike asks whether the Broadway
League's Total Attendance for it (Weekly Totals on LEAGUE_URL) is above K.
The League posts a week on the Monday after it and the market closes the
Sunday night before, so the number is never public while it trades: the
risk is a better forecaster, not a leak.

THE MODEL, bottom-up from the League's per-show table (it sums to the
Total Attendance exactly: 259,164 for the week ending 10/18/2026):
  * a show that played the last published week L plays its usual
    performances (the mode of its last 4 weeks of 6 or more; 8 without) at
    L's attendance per performance, scaled by the seasonal drift of each
    week from L to the target -- the median per-performance change of
    continuing shows in the same calendar week (+-MATCH_DAYS of its date;
    Halloween and Thanksgiving fall on dates) of the seasons since 2022,
    the reopening season left out: Halloween week runs about -3.5%,
    mid-November +5%;
  * a new show (the League's opening-night calendar) plays one performance
    on its first-preview day and then the Tue-Sun 8-show week, at its
    theatre's seats (from history) x FIRST_FILL its first week, LATER_FILL
    after;
  * a show stops after its closing date: the calendar's for this season's
    shows, CLOSINGS (by hand, from the closing notices) for the older ones.
Walk-forward backtest over 6/2022-10/2026 (new shows and closings known in
advance, as they are): log error sd 3.7% one week ahead, 4.0% two, bias
about 0 -- about +-10k on a 260k week, with strikes 10k apart. A strike's
fair is P(total > K) for a lognormal around the forecast with that sd
(SIGMA_BY_H, wider each week further out, x HOLIDAY_SIGMA_MULT through the
Thanksgiving-to-New-Year weeks).

The file is the rain writers' shape for the bot's loader (markets: p /
event / fetched_at), and carries its own memory: the last KEEP_WEEKS
published weeks' per-show rows, the drift table and the seats -- seeded
from SEED_FILE, each new League week appended (and its drift with it) when
it posts.
"""
import argparse
import html
import json
import math
import os
import re
import statistics as st
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

SERIES = ("KXBWAYATTENDANCEW",)
LEAGUE_URL = "https://www.broadwayleague.com/research/grosses-broadway-nyc/"
CALENDAR_URL = "https://www.broadwayleague.com/calendar/data/"
CALENDAR_REFERER = "https://www.broadwayleague.com/press/opening-night/"
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
HTTP_TIMEOUT = 30
LEAGUE_TTL_SECS = 1800          # the League page at most every 30 minutes
CALENDAR_TTL_SECS = 6 * 3600    # the opening-night calendar
EVENTS_TTL_SECS = 3600          # Kalshi's open events
KEEP_WEEKS = 10                 # per-show weeks kept (the perf-count mode needs 4)
MAX_H = 4                       # weeks past the last published one we price
SIGMA_BY_H = {1: 0.037, 2: 0.040}
SIGMA_STEP = 0.005              # each week beyond SIGMA_BY_H
HOLIDAY_SIGMA_MULT = 1.5        # a week from Thanksgiving through New Year's Day
FIRST_FILL, LATER_FILL = 0.96, 0.91   # new shows' median capacity, first week / after
MATCH_DAYS = 3                  # "the same week" in other seasons, by calendar date
DRIFT_DAYS_BACK = 300           # the drift reads only seasons before the target's
MIN_DRIFT_SHOWS = 8             # continuing shows needed to call a week's drift
DEFAULT_SEATS = 1000.0
# Mon..Sun performances of the usual 8-show week
SCHEDULE = {0: 0, 1: 1, 2: 2, 3: 1, 4: 1, 5: 2, 6: 1}
# final performances of shows the opening-night calendar does not list
# (Playbill's "Last Chance" list, 2026-10-25); a later notice goes here
CLOSINGS = {
    "TWO STRANGERS (CARRY A CAKE ACROSS NEW YORK)": "2026-11-29",
    "STRANGER THINGS: THE FIRST SHADOW": "2027-01-03",
}
SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bway_seed.json")
_EVENT_DATE_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})$")


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} [bway] {msg}", flush=True)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            js = json.load(f)
        return js if isinstance(js, dict) else {}
    except (OSError, ValueError):
        return {}


def _norm(title: str) -> str:
    """A show's name, comparable across the grosses ("A FEW GOOD MEN 2026"
    -- a revival carries its year) and the calendar ("A Few Good Men")."""
    t = re.sub(r"\s+(19|20)\d\d$", "", (title or "").upper().strip())
    t = re.sub(r"^THE\s+", "", t)
    return re.sub(r"[^A-Z0-9]", "", t)


def _theatre(name: str) -> str:
    """The calendar's "Booth Theatre" as the grosses name it: "Booth"."""
    return re.sub(r"\s+(Theatre|Theater)$", "", (name or "").strip())


def _iso(d: date) -> str:
    return d.isoformat()


def _day(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# ---- the League's pages ------------------------------------------------------
def parse_rows(page: str) -> Dict[str, List[dict]]:
    """The grosses table: week ending (ISO) -> [{"show", "sid", "theatre",
    "n" (previews + performances), "attend", "cap" (% capacity)}]."""
    i = page.find('<table class="display shows"')
    if i < 0:
        return {}
    j = page.find("</table>", i)
    body = re.sub(r"<!--.*?-->", "", page[i:j], flags=re.S)   # a commented-out cell
    out: Dict[str, List[dict]] = {}

    def num(s: str) -> float:
        return float(re.sub(r"[^0-9.\-]", "", s) or 0)
    for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 11:
            continue
        txt = [html.unescape(re.sub(r"<[^>]+>", "", t)).strip() for t in tds]
        try:
            wk = datetime.strptime(txt[0], "%m/%d/%Y").date()
        except ValueError:
            continue
        sid = re.search(r"/research/grosses-broadway-nyc/(\d+)/", tds[1])
        out.setdefault(_iso(wk), []).append({
            "show": txt[1], "sid": sid.group(1) if sid else "", "theatre": txt[3],
            "n": int(num(txt[4]) + num(txt[5])), "attend": int(num(txt[8])),
            "cap": num(txt[10])})
    return out


def parse_calendar(js) -> List[dict]:
    """The opening-night calendar: [{"title", "theatre", "preview", "closing"
    (ISO or None), "sid" (its grosses id, when it has grosses)}]."""
    out: List[dict] = []

    def day_of(v) -> Optional[str]:
        try:
            return _iso(datetime.strptime(str(v or ""), "%B %d, %Y").date())
        except ValueError:
            return None
    for e in js if isinstance(js, list) else []:
        if not isinstance(e, dict):
            continue
        gid = re.search(r"/(\d+)/?$", str(e.get("grosses") or ""))
        out.append({"title": str(e.get("title") or ""),
                    "theatre": _theatre(str(e.get("theatre") or "")),
                    "preview": day_of(e.get("preview")), "closing": day_of(e.get("closing")),
                    "sid": gid.group(1) if gid else ""})
    return out


def _http_text(url: str, headers: Optional[dict] = None) -> str:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    r = requests.get(url, headers=h, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


def fetch_league(get_text: Optional[Callable] = None) -> Dict[str, List[dict]]:
    """The League page's per-show rows (its latest week)."""
    return parse_rows((get_text or _http_text)(LEAGUE_URL, None))


def fetch_calendar(get_text: Optional[Callable] = None) -> List[dict]:
    """The opening-night calendar's feed (it answers [] without the XHR
    headers)."""
    raw = (get_text or _http_text)(CALENDAR_URL, {
        "X-Requested-With": "XMLHttpRequest", "Referer": CALENDAR_REFERER,
        "Accept": "application/json, text/javascript, */*; q=0.01"})
    return parse_calendar(json.loads(raw))


# ---- Kalshi --------------------------------------------------------------------
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


def event_week_end(event: str) -> Optional[str]:
    """KXBWAYATTENDANCEW-26NOV01 -> "2026-11-01" (the Sunday that ends it)."""
    m = _EVENT_DATE_RE.search(event or "")
    if not m:
        return None
    try:
        return _iso(datetime.strptime(m.group(1), "%y%b%d").date())
    except ValueError:
        return None


def fetch_events(get_json: Optional[Callable] = None) -> Dict[str, dict]:
    """Open events: event -> {"week_end", "strikes": {ticker: K}}."""
    out: Dict[str, dict] = {}
    for s in SERIES:
        js = _signed_or_public(get_json, {"series_ticker": s, "status": "open", "limit": 200})
        for m in js.get("markets") or []:
            ev, t = str(m.get("event_ticker") or ""), str(m.get("ticker") or "")
            wk = event_week_end(ev)
            try:
                k = float(m.get("floor_strike"))
            except (TypeError, ValueError):
                continue
            if not (ev and t and wk) or str(m.get("strike_type") or "greater") != "greater":
                continue
            out.setdefault(ev, {"week_end": wk, "strikes": {}})["strikes"][t] = k
    return out


# ---- the model -------------------------------------------------------------------
def week_drift(prev: List[dict], cur: List[dict]) -> Optional[float]:
    """The median log change in attendance per performance of the shows
    that played both weeks with 6 performances or more (None under
    MIN_DRIFT_SHOWS of them)."""
    before = {r["show"]: r for r in prev if r["n"] >= 6 and r["attend"] > 0}
    lr = [math.log((r["attend"] / r["n"]) / (before[r["show"]]["attend"] / before[r["show"]]["n"]))
          for r in cur if r["show"] in before and r["n"] >= 6 and r["attend"] > 0]
    return st.median(lr) if len(lr) >= MIN_DRIFT_SHOWS else None


def season_drift(drift: Dict[str, float], week_end: date) -> float:
    """The mean drift of the same calendar week (+-MATCH_DAYS) in the
    seasons before the target's."""
    vals: List[float] = []
    for b_iso, v in drift.items():
        b = _day(b_iso)
        if b > week_end - timedelta(days=DRIFT_DAYS_BACK):
            continue
        for y in (week_end.year - 1, week_end.year, week_end.year + 1):
            try:
                same = b.replace(year=y)
            except ValueError:                      # Feb 29
                same = b.replace(year=y, day=28)
            if abs((same - week_end).days) <= MATCH_DAYS:
                vals.append(float(v))
                break
    return sum(vals) / len(vals) if vals else 0.0


def expected_perfs(weeks: Dict[str, List[dict]], show: str, upto: str) -> int:
    """The mode of the show's last 4 weeks of 6 performances or more (8
    without one): a solo play's 7, a holiday week's 9 do not stick."""
    ns = [r["n"] for wk in sorted(weeks) if wk <= upto
          for r in weeks[wk] if r["show"] == show and r["n"] >= 6][-4:]
    if not ns:
        return 8
    try:
        return st.mode(ns)
    except st.StatisticsError:                      # a tie, before Python 3.8
        return ns[-1]


def seats_for(seats: Dict[str, float], theatre: str) -> float:
    """The theatre's seats: by name, else by the name inside the other
    ("Samuel J. Friedman" is the grosses' "Friedman"), else DEFAULT_SEATS."""
    if theatre in seats:
        return float(seats[theatre])
    low = theatre.lower()
    for k, v in seats.items():
        if k and (k.lower() in low or low in k.lower()):
            return float(v)
    return DEFAULT_SEATS


def first_week_perfs(first_preview: date, week_end: date) -> int:
    """One performance on the first-preview day, then the 8-show week."""
    n, d = 1, first_preview + timedelta(days=1)
    while d <= week_end:
        n += SCHEDULE[d.weekday()]
        d += timedelta(days=1)
    return n


def closing_perfs(closing: date, week_end: date, usual: int) -> int:
    """Performances in a closing show's final week: its schedule from the
    Monday through the closing day, at most its usual week."""
    n, d = 0, week_end - timedelta(days=6)
    while d <= closing:
        n += SCHEDULE[d.weekday()]
        d += timedelta(days=1)
    return min(n, usual)


def holiday_week(week_end: date) -> bool:
    """The week holds Thanksgiving (the 4th Thursday of November) or a day
    from Dec 20 through Jan 3."""
    start = week_end - timedelta(days=6)
    d = start
    while d <= week_end:
        if (d.month == 12 and d.day >= 20) or (d.month == 1 and d.day <= 3):
            return True
        if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
            return True
        d += timedelta(days=1)
    return False


def sigma_for(h: int, week_end: date) -> float:
    s = SIGMA_BY_H.get(h) or (SIGMA_BY_H[max(SIGMA_BY_H)]
                              + SIGMA_STEP * (h - max(SIGMA_BY_H)))
    return s * (HOLIDAY_SIGMA_MULT if holiday_week(week_end) else 1.0)


def forecast(weeks: Dict[str, List[dict]], drift: Dict[str, float],
             seats: Dict[str, float], calendar: List[dict], week_end: str,
             base: str, closings: Optional[Dict[str, str]] = None
             ) -> Tuple[float, Dict[str, float]]:
    """(total, {show: attendance}) for the week ending `week_end` from the
    published week `base` (see the module docstring)."""
    W, L = _day(week_end), _day(base)
    f, d = 0.0, L + timedelta(days=7)
    while d <= W:
        f += season_drift(drift, d)
        d += timedelta(days=7)
    factor = math.exp(f)
    close_by: Dict[str, date] = {}
    for show, c in (closings if closings is not None else CLOSINGS).items():
        close_by[_norm(show)] = _day(c)
    known_sids = {r["sid"] for rows in weeks.values() for r in rows if r.get("sid")}
    known_names = {_norm(r["show"]) for rows in weeks.values() for r in rows}
    for e in calendar:
        if e.get("closing"):
            close_by.setdefault(_norm(e["title"]), _day(e["closing"]))
    parts: Dict[str, float] = {}
    for r in weeks[base]:
        c = close_by.get(_norm(r["show"]))
        if c is not None and c < W - timedelta(days=6):
            continue                                # closed before the week
        n = expected_perfs(weeks, r["show"], base)
        if c is not None and c < W:
            n = closing_perfs(c, W, n)
        apf = r["attend"] / r["n"] if r["n"] >= 3 else \
            seats_for(seats, r["theatre"]) * LATER_FILL   # a sliver of a week
        parts[r["show"]] = n * apf * factor
    for e in calendar:
        if not e.get("preview") or (e.get("sid") and e["sid"] in known_sids) \
                or _norm(e["title"]) in known_names:
            continue                                # not new, or no date
        fp = _day(e["preview"])
        if fp > W or fp <= L:
            continue                                # later, or should have played L
        c = _day(e["closing"]) if e.get("closing") else None
        if c is not None and c < W - timedelta(days=6):
            continue
        cap = seats_for(seats, e["theatre"])
        if fp >= W - timedelta(days=6):             # its first week
            n, fill = first_week_perfs(fp, W), FIRST_FILL
        else:
            n, fill = 8, LATER_FILL
        if c is not None and c < W:
            n = closing_perfs(c, W, n)
        parts[e["title"].upper()] = n * cap * fill
    return sum(parts.values()), parts


def p_above(total: float, sigma: float, strike: float) -> float:
    """P(attendance > strike) for a lognormal around `total`."""
    if total <= 0 or strike <= 0 or sigma <= 0:
        return 1.0 if total > strike else 0.0
    z = (math.log(strike) - math.log(total)) / sigma
    return max(0.0, min(1.0, 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))))


# ---- the file ----------------------------------------------------------------------
def _absorb(weeks: Dict[str, List[dict]], drift: Dict[str, float],
            seats: Dict[str, float], new_weeks: Dict[str, List[dict]]) -> List[str]:
    """Fold freshly read weeks into the memory: their rows, each one's drift
    from the week before it (when held), the seats of a theatre not yet
    known. Returns the weeks added."""
    added = []
    for wk in sorted(new_weeks):
        rows = new_weeks[wk]
        if not rows or wk in weeks:
            continue
        weeks[wk] = rows
        added.append(wk)
        prev = _iso(_day(wk) - timedelta(days=7))
        if prev in weeks:
            v = week_drift(weeks[prev], rows)
            if v is not None:
                drift[wk] = round(v, 5)
        for r in rows:
            if r["theatre"] not in seats and r["n"] >= 6 and r["cap"] and 30 <= r["cap"] <= 105:
                seats[r["theatre"]] = round(r["attend"] / r["n"] / (r["cap"] / 100.0))
    return added


def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    get_text: Optional[Callable] = None,
                    events: Optional[Dict[str, dict]] = None,
                    closings: Optional[Dict[str, str]] = None) -> Tuple[int, int]:
    """Build and atomically write the fair file; returns (markets with a
    fair, events without one). The League page is read every
    LEAGUE_TTL_SECS, its calendar every CALENDAR_TTL_SECS and Kalshi's open
    events every EVENTS_TTL_SECS (a failed read reuses the file's copy). An
    event is priced from the latest published week before it, at most MAX_H
    weeks back; with no calendar yet (never read) nothing is priced -- a new
    show missed would read the week low."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    weeks: Dict[str, List[dict]] = dict(old.get("weeks") or {})
    drift: Dict[str, float] = dict(old.get("drift") or {})
    seats: Dict[str, float] = dict(old.get("seats") or {})
    if not (weeks and drift and seats):
        seed = _read_json(SEED_FILE)
        weeks = weeks or dict(seed.get("weeks") or {})
        drift = drift or dict(seed.get("drift") or {})
        seats = seats or dict(seed.get("seats") or {})
    league_at = float(old.get("league_at") or 0)
    if ts - league_at >= LEAGUE_TTL_SECS:
        try:
            added = _absorb(weeks, drift, seats, fetch_league(get_text))
            league_at = ts
            if added:
                _log(f"League week(s) {', '.join(added)} read")
        except Exception as e:
            _log(f"! League page read failed ({type(e).__name__}: {str(e)[:80]}); "
                 f"keeping {len(weeks)} weeks")
    calendar = old.get("calendar")
    calendar_at = float(old.get("calendar_at") or 0)
    if calendar is None or ts - calendar_at >= CALENDAR_TTL_SECS:
        try:
            calendar = fetch_calendar(get_text)
            calendar_at = ts
        except Exception as e:
            _log(f"! calendar read failed ({type(e).__name__}: {str(e)[:80]})"
                 + ("; reusing the last" if calendar is not None else ""))
    ev_cache = old.get("events") or {}
    events_at = float(old.get("events_at") or 0)
    if events is None:
        if ev_cache and ts - events_at < EVENTS_TTL_SECS:
            events = ev_cache
        else:
            try:
                events = fetch_events(get_json)
                events_at = ts
            except Exception as e:
                if not ev_cache:
                    raise
                _log(f"! Kalshi event read failed ({type(e).__name__}); reusing "
                     f"{len(ev_cache)} cached")
                events = ev_cache
    for wk in sorted(weeks)[:-KEEP_WEEKS]:
        weeks.pop(wk)
    fetched = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    markets: Dict[str, dict] = {}
    event_fair: Dict[str, dict] = {}
    missing = 0
    for ev, e in sorted(events.items()):
        W = _day(e["week_end"])
        base = max((wk for wk in weeks if _day(wk) <= W - timedelta(days=7)), default=None)
        h = (W - _day(base)).days // 7 if base else 0
        if calendar is None or base is None or not 1 <= h <= MAX_H or e["week_end"] in weeks:
            missing += 1
            event_fair[ev] = {"week_end": e["week_end"], "base_week": base, "h": h,
                              "why": ("no calendar yet" if calendar is None else
                                      "the week is published" if e["week_end"] in weeks else
                                      "no published week to start from")}
            continue
        total, parts = forecast(weeks, drift, seats, calendar, e["week_end"], base,
                                closings)
        sigma = sigma_for(h, W)
        new = {s: round(a) for s, a in parts.items()
               if s not in {r["show"] for r in weeks[base]}}
        event_fair[ev] = {"week_end": e["week_end"], "base_week": base, "h": h,
                          "forecast": round(total), "sigma": round(sigma, 4),
                          "base_total": sum(r["attend"] for r in weeks[base]),
                          "new_shows": new}
        for t, k in sorted(e["strikes"].items()):
            markets[t] = {"p": round(p_above(total, sigma, k), 4), "event": ev,
                          "fetched_at": fetched, "strike": k}
    out = {"generated_at": fetched, "series": list(SERIES),
           "model": {"sigma_by_h": SIGMA_BY_H, "sigma_step": SIGMA_STEP,
                     "holiday_sigma_mult": HOLIDAY_SIGMA_MULT, "first_fill": FIRST_FILL,
                     "later_fill": LATER_FILL, "match_days": MATCH_DAYS},
           "markets": markets, "event_fair": event_fair,
           "events": events, "events_at": events_at,
           "calendar": calendar, "calendar_at": calendar_at, "league_at": league_at,
           "weeks": weeks, "drift": drift, "seats": seats}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, path)
    return len(markets), missing


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="the fair file to write")
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    js = _read_json(args.out)
    for ev, e in sorted((js.get("event_fair") or {}).items()):
        print(f"{ev}: " + (f"{e['forecast']:,} from week {e['base_week']} (h={e['h']}, "
                           f"sd {100 * e['sigma']:.1f}%), new {e['new_shows']}"
                           if "forecast" in e else e.get("why", "")))
    for t, m in sorted((js.get("markets") or {}).items(), key=lambda kv: kv[1]["strike"]):
        print(f"  {t}: {100 * m['p']:.1f}c")
    print(f"{ok} markets priced, {miss} events without a fair")
    return 0


if __name__ == "__main__":
    sys.exit(main())
