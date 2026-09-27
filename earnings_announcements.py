#!/usr/bin/env python3
r"""earnings_announcements.py -- an earnings call's date and time from the
company's OWN announcement, found the way a person finds it.

Jack 2026-09-27, after KXEARNINGSMENTIONARITZIA-26OCT14 sat undated for two
weeks: "you were able to figure out what the earnings call times were, so make
sure the fallback can do that". The answer each time was the company's own
notice, published 2-4 weeks ahead. Two public, keyless ways to reach it:

  * the IR site's EVENT FEED. Q4-hosted investor sites load their events page
    from /feed/Event.svc/GetEventList, and that JSON is public: "Aritzia Second
    Quarter Fiscal 2027 Earnings Call", 10/08/2026 16:30, EST. The page itself
    is JavaScript-only, which is why the old IR scrape never saw the date.
  * the company's PRESS RELEASES, via Nasdaq's per-symbol feed
    (api.nasdaq.com/api/news/topic/press_release) and nasdaq.com's copy of each
    wire release: "Domino's Announces Q3 2026 Earnings Webcast" (Sep 10) --
    webcast Oct 13 8:30am ET, results distributed 6:05am ET. Domino's own IR
    site answers a script with HTTP 403; Nasdaq's copy does not.

The parser reads only the release BODY, after the wire dateline. The page
header "Published Sep 10, 2026 4:05pm EDT" is what the old parse_call_time()
took for a 4:05pm call. Each time is classified by the nearest keyword before
it -- a conference call / webcast, or results being released / distributed --
archive and replay times ("available through 9:00 p.m. PT, October 29") are
skipped, and PT/CT/MT are converted to ET.

anchor() turns a finding into the bot's override, which is the RELEASE, the
moment the information is public: the stated time when the notice gives one,
16:00 for after the close, else the earlier of 07:00 and two hours before a
morning call.

Nothing here writes anything, and every entry point returns None/[] on any
failure: a source that cannot be read is a source that found nothing.
"""

import html
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytz
import requests

ET = pytz.timezone("US/Eastern")
TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 "
                    "Safari/537.36",
      "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9"}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
DATE_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?\b", re.I)
# "8:30 am ET", "10 am (EDT)", "1:30pm PT", "4:30 p.m. Eastern" (a.m./p.m.
# are normalised to am/pm first)
TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b\s*\(?\s*"
    r"(eastern|pacific|central|mountain|[epcm][sd]?t)\b", re.I)
_ZONES = {"e": "US/Eastern", "p": "US/Pacific", "c": "US/Central",
          "m": "US/Mountain"}
CALL_RE = re.compile(
    r"conference[- ]call|earnings call|webcast|call with (?:analysts|investors)"
    r"|investor call", re.I)
RELEASE_RE = re.compile(r"\b(?:releas|report|distribut|issu|publish)\w*", re.I)
RESULTS_RE = re.compile(r"results|earnings|financial|supplemental", re.I)
AMC_RE = re.compile(
    r"after (?:the )?(?:stock )?markets? clos\w*|after (?:the )?close\b"
    r"|following the close|after the closing bell"
    r"|after (?:regular )?(?:trading|market) hours", re.I)
BMO_RE = re.compile(
    r"before (?:the )?(?:stock )?markets? open\w*|before (?:the )?open\b"
    r"|before the opening bell|prior to (?:the )?(?:stock )?markets? open\w*"
    r"|pre-?market", re.I)
SAME_MORNING_RE = re.compile(r"that morning|earlier that (?:morning|day)", re.I)
ARCHIVE_RE = re.compile(r"archiv\w*|replay|through|until", re.I)
# wire datelines: the body starts after these
DATELINE_RE = re.compile(
    r"/\s*PRNewswire\s*/\s*--|--\s*\(\s*BUSINESS WIRE\s*\)\s*--"
    r"|/\s*CNW\s*/\s*--|\(\s*GLOBE NEWSWIRE\s*\)\s*--|/\s*EINPresswire\S*\s*--"
    r"|\(\s*ACCESSWIRE\s*\)|\(\s*Newsfile Corp\.?\s*\)\s*--", re.I)
BODY_END_RE = re.compile(r"\bAbout [A-Z]|\bSOURCE [A-Z]|View original content")
PUBLISHED_RE = re.compile(
    r"Published\s+\w{3,9}\.?\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s*[ap]m\s+[A-Z]{2,4}",
    re.I)
# a Nasdaq feed title that ANNOUNCES a call, as opposed to reporting results
ANNOUNCE_TITLE_RE = re.compile(
    r"conference call|webcast|earnings call|\bto (?:report|release|announce|"
    r"host|hold)\b|(?:release|report(?:ing)?) date|\bschedules?\b|\bsets?\b.*date",
    re.I)

# How far from now an announced call may sit and still be "the next call".
MAX_AHEAD_DAYS = 150
RECENT_RELEASE_DAYS = 100


def clean_text(page: str) -> str:
    """Visible text of an HTML page, whitespace collapsed, a.m./p.m. -> am/pm,
    and no space before punctuation ("2026 , at" -> "2026, at")."""
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page or "")
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\b([ap])\.\s?m\.", lambda m: m.group(1).lower() + "m", t,
               flags=re.I)
    t = re.sub(r"\s+([,.;:)])", r"\1", t)
    return t.strip()


def release_body(text: str) -> str:
    """The press-release body: from the wire dateline to "About <Company>" /
    "SOURCE". Without a dateline, the whole text minus the page's
    "Published <date> <time>" stamp."""
    m = DATELINE_RE.search(text)
    body = text[m.end():] if m else PUBLISHED_RE.sub(" ", text)
    end = BODY_END_RE.search(body)
    return (body[:end.start()] if end else body).strip()


def _sentences(body: str) -> list:
    return [s for s in re.split(r"(?<=[a-z0-9)\]])[.!?]\s+(?=[A-Z\"])", body)
            if s.strip()]


def _year(month: int, day: int, stated, ref: datetime) -> int:
    """A year printed next to the date wins; else the year that puts the date
    closest to `ref` (the event's own date)."""
    if stated:
        return int(stated)
    if ref.tzinfo is not None:
        ref = ref.astimezone(ET).replace(tzinfo=None)
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            gap = abs((datetime(y, month, day) - ref).total_seconds())
        except ValueError:
            continue
        if best is None or gap < best[0]:
            best = (gap, y)
    return best[1] if best else ref.year


def _to_et(d, hour: int, minute: int, zone: str) -> datetime:
    tz = pytz.timezone(zone)
    local = tz.localize(datetime(d.year, d.month, d.day, hour, minute))
    return local.astimezone(ET)


def _last_keyword(text: str):
    """'call' / 'release' for the LAST keyword in `text`, or None. A release
    keyword only counts next to results/earnings words."""
    best = None
    for m in CALL_RE.finditer(text):
        best = (m.start(), "call")
    for m in RELEASE_RE.finditer(text):
        ctx = text[max(0, m.start() - 60):m.end() + 60]
        if RESULTS_RE.search(ctx) and (best is None or m.start() > best[0]):
            best = (m.start(), "release")
    return best[1] if best else None


def parse_announcement(page_or_text: str, ref: datetime) -> dict:
    """{"call": dt_et|None, "release": dt_et|None, "release_how":
    "stated"|"after close"|"before open"|None, "evidence": [str]} from an
    announcement page (HTML or text). `ref` resolves year-less dates."""
    body = release_body(clean_text(page_or_text))
    out = {"call": None, "release": None, "release_how": None, "evidence": []}
    last_date = None
    for sent in _sentences(body):
        dates = []
        for dm in DATE_RE.finditer(sent):
            month, day = MONTHS[dm.group(1).lower()], int(dm.group(2))
            try:
                d = datetime(_year(month, day, dm.group(3), ref), month, day)
            except ValueError:
                continue
            archived = bool(ARCHIVE_RE.search(sent[max(0, dm.start() - 40):
                                                   dm.start()]))
            dates.append((dm.start(), d, archived))
        live_dates = [x for x in dates if not x[2]]

        def date_near(pos):
            if not live_dates:
                return last_date
            return min(live_dates, key=lambda x: abs(x[0] - pos))[1]

        for tm in TIME_RE.finditer(sent):
            if ARCHIVE_RE.search(sent[max(0, tm.start() - 70):tm.start()]):
                continue                          # an archive / replay window
            kind = _last_keyword(sent[:tm.start()]) or (
                "call" if CALL_RE.search(sent) else
                "release" if RELEASE_RE.search(sent) and RESULTS_RE.search(sent)
                else None)
            d = date_near(tm.start())
            if kind is None or d is None:
                continue
            hour, minute = int(tm.group(1)) % 12, int(tm.group(2) or 0)
            if tm.group(3).lower() == "pm":
                hour += 12
            dt = _to_et(d, hour, minute, _ZONES[tm.group(4)[0].lower()])
            if kind == "call" and out["call"] is None:
                out["call"] = dt
                out["evidence"].append(sent[max(0, tm.start() - 90):tm.end() + 20])
            elif kind == "release" and out["release"] is None:
                out["release"], out["release_how"] = dt, "stated"
                out["evidence"].append(sent[max(0, tm.start() - 90):tm.end() + 40])
        # A release clause with no stated time: after the close / before the
        # open / "released that morning" (Carnival: same day as its call).
        if (out["release"] is None and RELEASE_RE.search(sent)
                and RESULTS_RE.search(sent)):
            how = ("after close" if AMC_RE.search(sent) else
                   "before open" if BMO_RE.search(sent) else
                   "same morning" if SAME_MORNING_RE.search(sent) else None)
            if how == "same morning":
                d = out["call"] and out["call"].replace(tzinfo=None)
            else:
                d = date_near(len(sent)) if how else None
            if how and d is not None:
                hour = 16 if how == "after close" else 7
                out["release"] = ET.localize(datetime(d.year, d.month, d.day,
                                                      hour, 0))
                out["release_how"] = ("after close" if how == "after close"
                                      else "before open")
                out["evidence"].append(sent[:220])
        if live_dates:
            last_date = live_dates[-1][1]
    return out


def anchor(found: dict):
    """The override for a finding: when the information goes public. None
    when there is nothing to anchor on."""
    rel, how, call = found.get("release"), found.get("release_how"), found.get("call")
    if rel is not None and how in ("stated", "after close"):
        return rel
    if rel is not None:                               # before the open
        a = rel
        if call is not None and call.date() == rel.date():
            a = min(a, call - timedelta(hours=2))
        return a
    if call is not None:
        if call.hour >= 16:
            return call.replace(hour=16, minute=0)
        return min(call.replace(hour=7, minute=0), call - timedelta(hours=2))
    return None


def label_for(found: dict, source: str) -> str:
    """Provenance label, <= 80 chars, and free of the guess markers that
    imm_earnings_overrides.provenance_of() treats as a synthesized hour."""
    parts = []
    rel, how, call = found.get("release"), found.get("release_how"), found.get("call")
    if rel is not None:
        parts.append("release {} ET".format(rel.strftime("%H:%M"))
                     if how == "stated" else how)
    if call is not None:
        parts.append("call {} ET".format(call.strftime("%m-%d %H:%M")))
    return ("announced: " + ", ".join(parts) + "; " + source)[:80]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _get(url: str, params=None, accept_json: bool = False):
    headers = dict(UA)
    if accept_json:
        headers["Accept"] = "application/json, text/plain, */*"
    r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def q4_upcoming_call(host: str, now: datetime):
    """The next earnings call on a Q4-hosted IR site's public event feed, as
    {"call": dt_et, "title", "url"}, or None (not a Q4 site, no such event,
    or any failure)."""
    try:
        r = _get("https://{}/feed/Event.svc/GetEventList".format(host), params={
            "LanguageId": 1, "eventSelection": 3, "eventDateFilter": 3,
            "includeFinancialReports": "true", "includePresentations": "true",
            "includePressReleases": "true", "sortOperator": 1, "pageSize": 20,
            "pageNumber": 0, "tagList": "", "includeTags": "true", "year": -1,
            "excludeSelection": 1}, accept_json=True)
        events = (r.json() or {}).get("GetEventListResult") or []
    except Exception:
        return None
    best = None
    for e in events:
        title = str(e.get("Title") or "")
        if not re.search(r"earnings|results|quarter", title, re.I):
            continue
        if re.search(r"annual (?:general )?meeting|investor day|"
                     r"conference(?! call)", title, re.I):
            continue
        zone = str(e.get("TimeZone") or "").strip().upper()
        if not re.fullmatch(r"[EPCM][SD]?T", zone):
            continue                              # no US zone: fail closed
        try:
            local = datetime.strptime(str(e.get("StartDate")), "%m/%d/%Y %H:%M:%S")
        except ValueError:
            continue
        call = _to_et(local, local.hour, local.minute, _ZONES[zone[0].lower()])
        if not (now - timedelta(hours=6) <= call
                <= now + timedelta(days=MAX_AHEAD_DAYS)):
            continue
        if best is None or call < best["call"]:
            best = {"call": call, "title": title,
                    "url": "https://{}{}".format(host, e.get("LinkToDetailPage") or "")}
    return best


def nasdaq_announcements(symbol: str, now: datetime, max_fetch: int = 4) -> list:
    """Announcements of upcoming calls among the symbol's recent press
    releases on Nasdaq, newest first: [{"call", "release", "release_how",
    "evidence", "url", "title"}]. Only releases whose body names the symbol in
    an exchange tag ("(NYSE: CCL)") count -- the feed also carries other
    issuers' releases that merely mention it (index reconstitutions)."""
    sym = (symbol or "").upper()
    if not re.fullmatch(r"[A-Z][A-Z.]{0,5}", sym):
        return []
    try:
        r = _get("https://api.nasdaq.com/api/news/topic/press_release", params={
            "q": "symbol:{}|assetclass:stocks".format(sym.lower()),
            "limit": 25, "offset": 0}, accept_json=True)
        rows = (((r.json() or {}).get("data") or {}).get("rows")) or []
    except Exception:
        return []
    tag = re.compile(r"\((?:NYSE|Nasdaq|NASDAQ|NYSE American|TSX|OTC\w*|"
                     r"Cboe\w*)\s*:\s*{}\)".format(re.escape(sym)))
    out = []
    for row in rows:
        title = str(row.get("title") or "")
        if not ANNOUNCE_TITLE_RE.search(title):
            continue
        try:
            created = datetime.strptime(str(row.get("created")), "%b %d, %Y")
            if (now.replace(tzinfo=None) - created).days > RECENT_RELEASE_DAYS:
                continue
        except ValueError:
            pass                                  # "3 hours ago": recent
        url = "https://www.nasdaq.com" + str(row.get("url") or "")
        try:
            text = clean_text(_get(url).text)
        except Exception:
            continue
        if not tag.search(text):
            continue
        found = parse_announcement(text, now)
        if found["call"] is None and found["release"] is None:
            continue
        found.update(url=url, title=title)
        out.append(found)
        if len(out) >= max_fetch:
            break
    return out


def _upcoming(found: dict, now: datetime) -> bool:
    when = found.get("release") or found.get("call")
    return (when is not None and now - timedelta(hours=12) <= when
            <= now + timedelta(days=MAX_AHEAD_DAYS))


def find_announced(symbol: str, source_urls, now: datetime):
    """The company's own notice of its next call: {"anchor", "call",
    "release", "release_how", "label", "url", "evidence", "conflict"} or
    None. Press releases first (they carry the release time), then the IR
    event feed on the hosts of Kalshi's settlement sources. When the two put
    the event more than a day apart the earlier anchor is kept and "conflict"
    says so: EARLY only forfeits accrual, LATE quotes through the call. (A day
    apart is one event: results after Tuesday's close, call Wednesday.)"""
    picks = []
    for f in nasdaq_announcements(symbol, now):
        if _upcoming(f, now):
            picks.append((f, "press release"))
            break
    hosts = []
    for u in source_urls or []:
        h = urlparse(str(u)).hostname
        if h and h not in hosts:
            hosts.append(h)
    for h in hosts:
        q = q4_upcoming_call(h, now)
        if q is not None:
            picks.append(({"call": q["call"], "release": None,
                           "release_how": None, "url": q["url"],
                           "evidence": [q["title"]]}, "IR event feed"))
            break
    best = None
    days = []
    for f, source in picks:
        a = anchor(f)
        if a is None:
            continue
        days.append((f.get("call") or f.get("release")).date())
        if best is None or a < best["anchor"]:
            best = dict(f, anchor=a, label=label_for(f, source))
    if best is not None:
        best["conflict"] = bool(days) and (max(days) - min(days)).days > 1
    return best


if __name__ == "__main__":                        # manual check
    import sys
    from datetime import timezone
    now_ = datetime.now(timezone.utc)
    res = find_announced(sys.argv[1], sys.argv[2:], now_)
    print(json.dumps(res, default=str, indent=1))
