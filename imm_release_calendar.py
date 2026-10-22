#!/usr/bin/env python3
"""IMM release calendar upkeep (Jack 2026-10-22: "Yes email. Also check daily
for new calendar times for any unresolved ones, and check calendars when
adding new events to this auto-quoting family").

The release pause (incentive_mm: release_sources_from_rules, release_calendar
.json) stands a state series aside around the agency report its rules cite,
but only on the dates the calendar has. Agencies post them late (NASS and BLS
post next year's late in the year) or one at a time (EIA's Electric Power
Monthly and Quarterly Coal Report), and a series whose report has no upcoming
date quotes straight through it. The IMM family watch runs this as its pass 6:

1. LOOKUP, once an ET day: each report the live bot maps a series to
   (incentive_mm.release_pause_map_path()) whose dates end within LOOKUP_DAYS
   is read off its agency's schedule page (LOOKUPS, the NASS month pages); a
   future date not on the calendar goes to incentive_mm
   .release_calendar_found_path(), which the bot merges with the repo file (no
   restart, no edit to the live checkout).
2. ALERT: a report in use with no future date after the lookup is emailed on
   first sight and re-sent every REALERT_HOURS (weekly) while it stays so.
3. NEW SERIES: a series the watch enrolls into the state statistics family
   (graduate rules, econstate) is checked at once against its sample market's
   rules. A report it maps to with no upcoming date is looked up right then;
   rules naming no report the pause knows are emailed once.

  python imm_release_calendar.py      # look up every report in use now, print (no writes)
"""

import html as _html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

import incentive_mm as imm
from incentive_mm import log

UA = "Mozilla/5.0 (KL release-calendar check; contact jackdu224@gmail.com)"
FETCH_TIMEOUT_SECS = 30
FETCH_GAP_SECS = 0.5                       # between two pages of one lookup
LOOKUP_DAYS = float(os.environ.get("IMM_RELEASE_CAL_LOOKUP_DAYS", "60"))
REALERT_HOURS = float(os.environ.get("IMM_RELEASE_CAL_REALERT_HOURS", "168"))
NASS_MONTHS_AHEAD = 14
NASS_URL = ("https://www.nass.usda.gov/Publications/Calendar/reports_by_date.php"
            "?view=l&js=1&month={m:02d}&year={y}")

_MON = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATE_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? "
                      r"(\d{1,2}), (20\d\d)\b")
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d\d)\s*([ap])\.?m", re.I)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECS) as r:
        return r.read().decode("utf-8", "replace")


def _lines(page: str) -> List[str]:
    s = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", page)
    s = re.sub(r"(?i)<br\s*/?>|</tr>|</p>|</li>|</h\d>|</div>|</td>|</th>", "\n", s)
    s = _html.unescape(re.sub(r"<[^>]+>", " ", s))
    return [x for x in (re.sub(r"\s+", " ", ln).strip() for ln in s.splitlines()) if x]


def _rows(page: str) -> List[List[str]]:
    """Each <tr>'s cell texts."""
    return [[re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
             for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
            for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page)]


def _ymd(m: "re.Match") -> date:
    return date(int(m.group(3)), _MON.index(m.group(1)[:3].lower()) + 1, int(m.group(2)))


def _clock(text: str) -> Optional[str]:
    m = _CLOCK_RE.search(text)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return f"{h:02d}:{m.group(2)}"


# ---------------------------------------------------------------- parsers ---
# Each: page HTML -> ["YYYY-MM-DD HH:MM" ET, ...], the calendar file's form.

def parse_bls(page: str) -> List[str]:
    """A BLS news-release schedule (bls.gov/schedule/news_release/<id>.htm):
    Reference Month | Release Date ("Nov. 20, 2026") | Release Time."""
    out = []
    for cells in _rows(page):
        if len(cells) >= 3:
            d, hm = _DATE_RE.search(cells[1]), _clock(cells[2])
            if d and hm:
                out.append(f"{_ymd(d):%Y-%m-%d} {hm}")
    return out


# (source, the report as NASS's calendar lists it)
NASS_REPORTS = (
    ("nass_crop", re.compile(r"Crop Production(?: Annual Summary)?$")),
    ("nass_cof", re.compile(r"Cattle on Feed$")),
    ("nass_cattle", re.compile(r"Cattle$")),
    ("nass_milk", re.compile(r"Milk Production(?:, Disposition, and Income)?$")),
    ("nass_poultry", re.compile(r"Poultry - Production and Value$")),
    ("nass_catfish", re.compile(r"Catfish Production$")),
)
NASS_SOURCES = tuple(s for s, _ in NASS_REPORTS)


def parse_nass(page: str) -> Dict[str, List[str]]:
    """One month of NASS's Reports by Date list: "Tue, 11/10/26", "12:00 pm
    ET", then the reports at that time."""
    out: Dict[str, List[str]] = {}
    day = hm = None
    for x in _lines(page):
        m = re.match(r"\w{3}, (\d\d)/(\d\d)/(\d\d)$", x)
        if m:
            day, hm = date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2))), None
            continue
        if re.match(r"\d{1,2}:\d\d [ap]m ET$", x):
            hm = _clock(x)
            continue
        if day and hm:
            for src, rx in NASS_REPORTS:
                if rx.match(x):
                    out.setdefault(src, []).append(f"{day:%Y-%m-%d} {hm}")
    return out


def parse_eia_next(page: str) -> List[str]:
    """An EIA report page's "Next Release Date: October 23, 2026" ("TBD" is
    none). EIA posts the day, not the hour: 10:00 as on the calendar."""
    m = re.search(r"Next Release(?: Date)?:\s*([A-Z][a-z]+\.? \d{1,2}, 20\d\d)", " ".join(_lines(page)))
    d = _DATE_RE.search(m.group(1)) if m else None
    return [f"{_ymd(d):%Y-%m-%d} 10:00"] if d else []


def parse_steo(page: str) -> List[str]:
    """The STEO release schedule: MM/DD/YYYY rows, released at noon."""
    return [f"{m.group(3)}-{m.group(1)}-{m.group(2)} 12:00"
            for m in (re.match(r"(\d\d)/(\d\d)/(20\d\d)\b", x) for x in _lines(page)) if m]


def parse_census_bps(page: str) -> List[str]:
    """Census's building permits schedule: survey month | New Residential
    Construction (08:30, U.S. and regions) | Revised Building Permits (08:00,
    states and metros); plus the annual release on the first workday of May."""
    out, years = [], set()
    for cells in _rows(page):
        if len(cells) >= 3 and re.fullmatch(r"[A-Z][a-z]+ 20\d\d", cells[0]):
            a, b = _DATE_RE.search(cells[1]), _DATE_RE.search(cells[2])
            if a and b:
                out += [f"{_ymd(a):%Y-%m-%d} 08:30", f"{_ymd(b):%Y-%m-%d} 08:00"]
                years |= {_ymd(a).year, _ymd(b).year}
    for y in sorted(years):
        d = date(y, 5, 1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(f"{d:%Y-%m-%d} 08:00")
    return out


def parse_census_popest(page: str) -> List[str]:
    """Census's population estimates schedule: the national / state row's
    "Date of Next Release" once it names a day ("December 2026" does not)."""
    lines = _lines(page)
    for i, x in enumerate(lines):
        if x.startswith("National and State population"):
            for y in lines[i + 1:i + 3]:
                d = _DATE_RE.search(y)
                if d:
                    return [f"{_ymd(d):%Y-%m-%d} 00:01"]
            return []
    return []


def parse_fhfa(page: str) -> List[str]:
    """FHFA's HPI release dates: a "2027 HPI Release Dates" heading, then
    "Tuesday, January 26" rows; released at 09:00."""
    out, year = [], None
    for x in _lines(page):
        y = re.match(r"(20\d\d) HPI Release Dates$", x)
        if y:
            year = int(y.group(1))
            continue
        m = re.match(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday), ([A-Z][a-z]+) (\d{1,2})$", x)
        if m and year and m.group(1)[:3].lower() in _MON:
            out.append(f"{year:04d}-{_MON.index(m.group(1)[:3].lower()) + 1:02d}-"
                       f"{int(m.group(2)):02d} 09:00")
    return out


LOOKUPS: Dict[str, Tuple[str, Callable[[str], List[str]]]] = {
    "bls_state": ("https://www.bls.gov/schedule/news_release/laus.htm", parse_bls),
    "bls_metro": ("https://www.bls.gov/schedule/news_release/metro.htm", parse_bls),
    "bls_qcew": ("https://www.bls.gov/schedule/news_release/cew.htm", parse_bls),
    "bls_empsit": ("https://www.bls.gov/schedule/news_release/empsit.htm", parse_bls),
    "eia_epm": ("https://www.eia.gov/electricity/monthly/", parse_eia_next),
    "eia_psm": ("https://www.eia.gov/petroleum/supply/monthly/", parse_eia_next),
    "eia_ngm": ("https://www.eia.gov/naturalgas/monthly/", parse_eia_next),
    "eia_qcr": ("https://www.eia.gov/coal/production/quarterly/", parse_eia_next),
    "eia_steo": ("https://www.eia.gov/outlooks/steo/release_schedule.php", parse_steo),
    "census_bps": ("https://www.census.gov/construction/bps/schedule.html", parse_census_bps),
    "census_popest": ("https://www.census.gov/programs-surveys/popest/about/schedule.html",
                      parse_census_popest),
    "fhfa_hpi": ("https://www.fhfa.gov/data/hpi", parse_fhfa),
}


def _host(url: str) -> str:
    return url.split("/")[2]


def lookup(sources, now: datetime, fetcher: Optional[Callable[[str], str]] = None
           ) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """({source: dates read}, {source: what was read, or the failure}). A
    page is read once however many sources it serves."""
    fetcher = fetcher or fetch
    pages: Dict[str, object] = {}

    def page(url: str):
        if url not in pages:
            if pages:
                time.sleep(FETCH_GAP_SECS)
            try:
                pages[url] = fetcher(url)
            except Exception as e:                   # noqa: BLE001 - reported per source
                pages[url] = e
        return pages[url]

    found: Dict[str, List[str]] = {}
    notes: Dict[str, str] = {}
    et = now.astimezone(imm.ET)
    months = [(et.year + (et.month - 1 + k) // 12, (et.month - 1 + k) % 12 + 1)
              for k in range(NASS_MONTHS_AHEAD)]

    def read(src: str, where: str, dates: List[str], err: str = "") -> None:
        found[src] = dates
        n = sum(1 for t in dates if _utc(t) > now)
        notes[src] = (f"{where} on {et:%m/%d}: "
                      + (f"{n} upcoming date(s)" if n else "no upcoming date posted") + err)

    for src in sorted(sources):
        if src in NASS_SOURCES:
            got, errs = [], []
            for y, m in months:
                p = page(NASS_URL.format(y=y, m=m))
                if isinstance(p, Exception):
                    errs.append(f"{y}-{m:02d} {type(p).__name__}")
                else:
                    got += parse_nass(p).get(src, [])
            read(src, f"nass.usda.gov Reports by Date ({months[0][1]}/{months[0][0]}-"
                      f"{months[-1][1]}/{months[-1][0]})", got,
                 f" ({len(errs)} month page(s) failed: {errs[0]})" if errs else "")
        elif src in LOOKUPS:
            url, parser = LOOKUPS[src]
            p = page(url)
            if isinstance(p, Exception):
                notes[src] = f"{_host(url)} failed on {et:%m/%d}: {type(p).__name__}: {str(p)[:120]}"
                continue
            try:
                read(src, _host(url), parser(p))
            except Exception as e:                   # noqa: BLE001 - a page that changed
                notes[src] = f"{_host(url)} unreadable on {et:%m/%d}: {type(e).__name__}: {str(e)[:120]}"
        else:
            notes[src] = "no schedule page known for it"
    return found, notes


# ------------------------------------------------------------ the calendar ---

def _utc(s: str) -> datetime:
    return imm.ET.localize(datetime.strptime(s, "%Y-%m-%d %H:%M")).astimezone(timezone.utc)


def new_dates(found: Dict[str, List[str]], cal: Dict[str, tuple], now: datetime
              ) -> Dict[str, List[str]]:
    """{source: future dates read that the calendar lacks}."""
    out = {}
    for src, times in found.items():
        have = set((cal.get(src) or ("", []))[1])
        new = sorted({t for t in times if _utc(t) > now and _utc(t) not in have})
        if new:
            out[src] = new
    return out


def save_found(added: Dict[str, List[str]], now: datetime) -> None:
    """Merge `added` into the found file (atomic; retried while the bot holds
    it open for a read)."""
    path = imm.release_calendar_found_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f) or {}
    except (OSError, ValueError):
        raw = {}
    srcs = raw.setdefault("sources", {})
    for src, times in added.items():
        rec = srcs.setdefault(src, {"times_et": []})
        rec["times_et"] = sorted(set(rec.get("times_et") or []) | set(times))
    raw["_comment"] = ("Release dates the IMM family watch read off the agencies' schedule "
                       "pages (imm_release_calendar); incentive_mm merges them with "
                       "release_calendar.json.")
    raw["updated"] = now.isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=1, sort_keys=True)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def in_use() -> Dict[str, Set[str]]:
    """{source: series} from the live bot's map ({} before its first write)."""
    try:
        with open(imm.release_pause_map_path(), encoding="utf-8") as f:
            series = (json.load(f) or {}).get("series") or {}
    except (OSError, ValueError):
        return {}
    out: Dict[str, Set[str]] = {}
    for s, srcs in series.items():
        for src in srcs or []:
            out.setdefault(src, set()).add(s)
    return out


def _future(cal: Dict[str, tuple], src: str, now: datetime) -> List[datetime]:
    return [t for t in (cal.get(src) or ("", []))[1] if t > now]


def check_new_series(client, new_series: List[Tuple[str, str]]) -> Dict[str, dict]:
    """{series: {"sample", "sources", "rules"}} for the series the watch just
    enrolled into the state statistics family, from the sample market's
    rules ("sources" None when the market could not be read)."""
    out = {}
    for s, sample in new_series:
        try:
            m = (client.get_market(sample) or {}).get("market") or {}
        except Exception as e:                       # noqa: BLE001 - reported
            out[s] = {"sample": sample, "sources": None, "rules": f"market read failed: {e!r}"}
            continue
        text = " ".join(str(m.get(k) or "") for k in ("rules_primary", "rules_secondary"))
        out[s] = {"sample": sample, "rules": re.sub(r"\s+", " ", text).strip(),
                  "sources": list(imm.release_sources_from_rules(text))}
    return out


def _repo_notes() -> Dict[str, dict]:
    try:
        with open(imm.RELEASE_CALENDAR_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("sources") or {}
    except (OSError, ValueError):
        return {}


def due(state: Dict[str, dict], items: Dict[str, dict], now_ts: float,
        realert_hours: float = REALERT_HOURS) -> Tuple[List[Tuple[str, dict, bool]], dict]:
    """(items to email [(key, item, new)], new state). An item is sent on
    first sight and re-sent every realert_hours while open; a "once" item is
    sent once and remembered. A closed item leaves the state."""
    out, new_state = [], {}
    for k, it in sorted(items.items()):
        st = dict(state.get(k) or {"first": now_ts})
        last = st.get("sent")
        if last is None or (not it.get("once") and now_ts - float(last) >= realert_hours * 3600):
            out.append((k, it, last is None))
            st["sent"] = now_ts
        new_state[k] = st
    for k, st in state.items():                      # a "once" item stays remembered
        if k not in new_state and k.startswith("new:"):
            new_state[k] = st
    return out, new_state


def body(due_items: List[Tuple[str, dict, bool]]) -> Tuple[str, str]:
    srcs = [d for d in due_items if d[0].startswith("src:")]
    news = [d for d in due_items if d[0].startswith("new:")]
    parts = ([f"{len(srcs)} report(s) with no upcoming date"] if srcs else []) + \
            ([f"{len(news)} new state series with no report on the calendar"] if news else [])
    lines = []
    if srcs:
        lines += ["These reports have no upcoming date on the release calendar, so the",
                  "series below quote straight through the next one.", ""]
        for _k, it, new in srcs:
            lines.append(it["name"] + (" [NEW]" if new else ""))
            lines.append(f"  Series:  {', '.join(sorted(it['series']))}")
            lines.append(f"  Looked:  {it.get('looked') or 'not yet'}")
            if it.get("note"):
                lines.append(f"  Note:    {it['note']}")
            lines.append("")
    if news:
        lines += ["New in the state statistics family, with rules naming no report the",
                  "release pause knows, so they trade through their releases:", ""]
        for k, it, _new in news:
            lines.append(f"{k[4:]} (e.g. {it['sample']})")
            lines.append(f"  Rules:   {it['rules'][:240]}")
            lines.append("  Fix:     add its report to release_calendar.json and incentive_mm's "
                         "_AGENCY_RELEASE_RULES, or leave it (no published schedule)")
            lines.append("")
    lines.append("The watch reads each agency's schedule once a day and adds dates as they "
                 "post (run-logs\\incentive-mm\\release_calendar_found.json). Re-sent "
                 f"every {REALERT_HOURS / 24:g} days while open (imm_release_calendar.py, "
                 "IMM family watch pass 6).")
    return "IMM-WATCH release calendar: " + ", ".join(parts), "\n".join(lines)


NEW_SERIES_TRIES = 6                      # watch passes to read a new series' market


def _next_txt(cal: Dict[str, tuple], src: str, now: datetime) -> str:
    nxt = _future(cal, src, now)
    return (f"{src} next {nxt[0].astimezone(imm.ET):%m-%d %H:%M} ET" if nxt
            else f"{src} (no upcoming date)")


def run(client, new_series: List[Tuple[str, str]], st: dict, dry: bool,
        now: Optional[datetime] = None, fetcher: Optional[Callable[[str], str]] = None
        ) -> Tuple[str, Optional[Tuple[str, str]]]:
    """One pass: (summary, (subject, text) to email or None). `st` is the
    watch state's "release_cal" entry, updated in place."""
    now = now or datetime.now(timezone.utc)
    cal = imm.load_release_calendar(force=True)
    use = in_use()
    pending = {s: (sample, n) for s, sample, n in st.get("pending") or []}
    for s, sample in new_series:
        pending.setdefault(s, (sample, 0))
    fresh = check_new_series(client, [(s, v[0]) for s, v in pending.items()]) if pending else {}
    st["pending"] = [[s, pending[s][0], pending[s][1] + 1] for s, r in fresh.items()
                     if r["sources"] is None and pending[s][1] + 1 < NEW_SERIES_TRIES]
    for s, r in fresh.items():
        for src in r["sources"] or []:
            use.setdefault(src, set()).add(s)
        what = (r["rules"] if r["sources"] is None else
                ", ".join(_next_txt(cal, src, now) for src in r["sources"])
                or "no report on the calendar")
        log(f"[WATCH] release calendar: new {s} -> {what}")
    today = now.astimezone(imm.ET).strftime("%Y-%m-%d")
    horizon = now + timedelta(days=LOOKUP_DAYS)
    short = {src for src in use
             if not any(t > horizon for t in (cal.get(src) or ("", []))[1])}
    # a report that just lost its last date (EIA posts the next Electric Power
    # Monthly with this one) or a new series' is read before it is emailed
    known = {k[4:] for k in st.get("items") or {} if k.startswith("src:")}
    urgent = {src for src in use if not _future(cal, src, now)} - known
    targets = short if st.get("looked_day") != today else urgent
    added: Dict[str, List[str]] = {}
    if targets:
        found, notes = lookup(targets, now, fetcher)
        added = new_dates(found, cal, now)
        st["looked_day"] = today
        st["notes"] = dict(st.get("notes") or {}, **notes)
        for src, ts in sorted(added.items()):
            log(f"[WATCH] release calendar: added {src} {', '.join(ts)} ({notes.get(src)})")
        if added and not dry:
            save_found(added, now)
            cal = imm.load_release_calendar(force=True)
    notes = st.get("notes") or {}
    repo = _repo_notes()
    items: Dict[str, dict] = {}
    for src, series in sorted(use.items()):
        if _future(cal, src, now) or src in added:
            continue
        rec = repo.get(src) or {}
        items[f"src:{src}"] = {"name": (cal.get(src) or (src,))[0], "series": sorted(series),
                               "looked": notes.get(src), "note": rec.get("note")}
    for s, r in fresh.items():
        if r["sources"] == []:
            items[f"new:{s}"] = {"sample": r["sample"], "rules": r["rules"], "once": True}
    due_items, st["items"] = due(st.get("items") or {}, items, now.timestamp())
    summary = (f"release calendar: {len(use)} report(s) in use, "
               f"{sum(1 for k in items if k.startswith('src:'))} without an upcoming date"
               + (f", added {sum(len(v) for v in added.values())} date(s)" if added else "")
               + (f", {len(fresh)} new series checked" if fresh else ""))
    return summary, (body(due_items) if due_items else None)


def main(argv=None) -> int:
    now = datetime.now(timezone.utc)
    use = in_use()
    srcs = set(use) or set(LOOKUPS) | set(NASS_SOURCES)
    found, notes = lookup(srcs, now)
    cal = imm.load_release_calendar(force=True)
    for src in sorted(srcs):
        fut = [t for t in found.get(src, []) if _utc(t) > now]
        print(f"{src:14s} {len(fut):3d} upcoming read, {len(_future(cal, src, now)):3d} on the "
              f"calendar; new {new_dates({src: found.get(src, [])}, cal, now).get(src, [])} "
              f"[{notes.get(src)}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
