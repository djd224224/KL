#!/usr/bin/env python3
r"""imm_earnings_overrides.py — daily earnings CALL-TIME override maintainer.

WHY: KXEARNINGSMENTION markets resolve on what is said DURING the earnings
call, so incentive_mm's cutoff must be the CALL start — but no automated feed
gives call times (Kalshi ticker dates / occurrence and Nasdaq AMC/BMO all
track the RELEASE, which can be a different day). Without a per-event
override the bot falls back to midnight-ET-of-ticker-date and the event dies
the night before its call (bit GOOGL/TSLA/ALK overnight 7/21->7/22).

WHAT: for every ACTIVE KXEARNINGSMENTION event with no override yet, find
the company's own announcement of the call (earnings_announcements: the IR
site's event feed, then its press releases via Nasdaq), else the Nasdaq
earnings calendar, else scrape the settlement-source / IR page for a
"conference call ... <time> ET on <date>" pattern; write resolved times to
run-logs/incentive-mm/event_start_overrides.json (which incentive_mm
hot-reloads each cycle — no restart). An event nothing can date stops
quoting IMM_EARNINGS_UNDATED_LEAD_DAYS before its ticker date. Emails on its own ONLY when something
needs Jack (paste-ready `--set` commands); every run also appends a summary
to overrides_last_runs.json, which the 7:20 combined "IMM quotes and
overrides" email (imm_quote_gaps.py) folds in each morning.

USAGE:
  python imm_earnings_overrides.py            # daily run (scheduled 6:45am)
  python imm_earnings_overrides.py --dry      # no write, no email
  python imm_earnings_overrides.py --set KXEARNINGSMENTIONXYZ-26AUG05 \
         "2026-08-05T17:00:00-04:00"          # manual entry (the fallback)

Precedence: env/code overrides in incentive_mm win over this file; file
entries may be re-written by later runs of this script or --set.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests


def _env_from_registry(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError):     # ImportError: non-Windows (tests)
        return ""


for _v in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD"):
    if not os.environ.get(_v):
        _val = _env_from_registry(_v)
        if _val:
            os.environ[_v] = _val

import earnings_announcements  # noqa: E402
import incentive_mm as imm  # noqa: E402
import imm_pickoff  # noqa: E402
from incentive_mm import (Alerter, ET, EVENT_OVERRIDES_FILE,  # noqa: E402
                          EVENT_START_OVERRIDES, EXTRA_ALLOW_FILE,
                          _EARNINGS_PREFIX, build_client,
                          load_extra_allow_series, load_file_event_overrides,
                          log, parse_event_date, parse_iso_utc)

# Per-run summary consumed by the 7:20 combined email (imm_quote_gaps.py).
SUMMARY_FILE = os.path.join(os.path.dirname(EVENT_OVERRIDES_FILE),
                            "overrides_last_runs.json")
# Pick-off windows this task has already emailed (imm_pickoff.new_rows).
PICKOFF_SEEN_FILE = os.path.join(os.path.dirname(EVENT_OVERRIDES_FILE),
                                 "pickoff_seen.json")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
# "conference call/webcast ... at 4:30 p.m. Eastern/ET" with an optional
# nearby "July 22" date. Windows are searched around call/webcast keywords.
TIME_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\s*"
    r"(?:eastern\b|edt\b|est\b|et\b)", re.I)
# Group 3 is a year printed right after the date ("January 28, 2027"), when the
# page states one. Groups 1-2 are unchanged.
DATE_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?", re.I)
KEYWORD_RE = re.compile(r"conference call|webcast|earnings call", re.I)


def _year_for(month, day, stated=None, year=None, ref=None):
    """Year for a 'Month DD' read off an IR / press page.

    Order of evidence: a year printed next to the date on the page, then an
    explicit caller year, then the year that puts Month DD CLOSEST to `ref` --
    the event's own date (its ticker date or Kalshi occurrence), else now.

    This replaces a hardcoded default of 2026. From ~mid-December 2026 that
    default stamped every IR-resolved January call a year EARLY; the override
    file is write-once for resolved entries, so the event would then have been
    stood down for its whole life. Taking the year from the wall clock instead
    would fail the other way across the Dec/Jan boundary."""
    if stated:
        return int(stated)
    if year is not None:
        return int(year)
    if ref is None:
        ref = datetime.now(timezone.utc)
    if not isinstance(ref, datetime):            # a plain date
        ref = datetime(ref.year, ref.month, ref.day)
    if ref.tzinfo is not None:
        ref = ref.astimezone(ET).replace(tzinfo=None)
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            gap = abs((datetime(y, month, day) - ref).total_seconds())
        except ValueError:                        # Feb 29 in a non-leap year
            continue
        if best is None or gap < best[0]:
            best = (gap, y)
    return best[1] if best else ref.year


# IR-PAGE LATE GUARD (2026-09-26). The direction of an override error decides
# whether it costs money: an EARLY override only stands the bot down early
# (safe); a LATE one quotes straight through the call (loses money). DATE_RE
# takes the FIRST date in a 600-char window, so a page can be mis-parsed
# ("Senior Notes due March 15, 2031 ... conference call on December 3" reads
# March 15, 2031). Resolving a year-less date to the year nearest the event
# made some mis-parses land LATE where the old hardcoded 2026 landed them
# early. So an IR-page date more than this many days AFTER the event's own
# date is NOT written: the event stays UNRESOLVED -- keeping its default,
# earlier cutoff -- and goes to the ACTION email for a --set. Fail closed;
# never pick another year. IR-page resolutions have been rare (none from
# 2026-07-23 to 2026-09-26), so the cost of caution is a manual --set.
IR_LATE_GUARD_DAYS = int(os.environ.get("IMM_IR_LATE_GUARD_DAYS", "21"))


def ir_date_too_late(dt_et, ref) -> bool:
    """True if an IR-page datetime is implausibly LATE versus the event's own
    date `ref`. Any comparison failure counts as too late (fail closed)."""
    try:
        return (dt_et - ref).total_seconds() > IR_LATE_GUARD_DAYS * 86400
    except Exception:
        return True


def parse_call_time(page_text: str, year=None, ref=None):
    """Best-effort (datetime_ET, evidence) from an IR/press page; None if the
    page doesn't contain BOTH a keyword-adjacent ET time and a nearby date."""
    text = re.sub(r"\s+", " ", page_text)
    for kw in KEYWORD_RE.finditer(text):
        window = text[max(0, kw.start() - 300):kw.end() + 300]
        tm = TIME_RE.search(window)
        dm = DATE_RE.search(window)
        if not tm or not dm:
            continue
        hour = int(tm.group(1))
        minute = int(tm.group(2) or 0)
        if "p" in tm.group(3).lower() and hour != 12:
            hour += 12
        if "a" in tm.group(3).lower() and hour == 12:
            hour = 0
        month = MONTHS[dm.group(1).lower()]
        day = int(dm.group(2))
        try:
            dt_et = ET.localize(datetime(_year_for(month, day, dm.group(3), year, ref),
                                         month, day, hour, minute))
        except ValueError:
            continue
        evidence = window[max(0, tm.start() - 60):tm.end() + 60].strip()
        return dt_et, evidence
    return None


# Earnings RELEASE (the press release / 8-K where a disclosed metric lands) —
# distinct from the CALL. Usually "report ... results ... after market close"
# (-> 4pm ET) or "before market open" (-> ~7am ET), occasionally a stated ET
# time. This is what company-disclosure cutoffs anchor to.
AMC_RE = re.compile(
    r"after\s+(?:the\s+)?(?:market|markets)\s+close|"
    r"after\s+(?:the\s+)?close\s+of\s+(?:the\s+)?markets?|"
    r"after\s+(?:the\s+)?closing\s+bell|post[-\s]?market|after[-\s]?hours", re.I)
BMO_RE = re.compile(
    r"before\s+(?:the\s+)?(?:market|markets)\s+open|"
    r"before\s+(?:the\s+)?(?:market\s+)?opens|"
    r"before\s+(?:the\s+)?opening\s+bell|pre[-\s]?market|premarket", re.I)
REPORT_RE = re.compile(r"report|announce|release|publish", re.I)


# Direct earnings-date lookup by stock ticker, to OVERRIDE Kalshi's often-
# useless settlement source (e.g. fiscal.ai, a data-aggregator homepage). The
# Nasdaq earnings calendar gives the report date + an after-hours/pre-market
# flag == the RELEASE timing (after-hours -> 4pm ET, pre-market -> ~7am ET).
# (A prior Nasdaq resolver was removed for CALL times, where AMC/BMO is the
# release not the call — but for RELEASE cutoffs that is exactly right.)
COMPANY_TICKERS = {
    "KXINTC": "INTC", "KXAMZN": "AMZN", "KXMETA": "META", "KXHOOD": "HOOD",
    "KXHOODA": "HOOD", "KXGOOG": "GOOGL", "KXSCHW": "SCHW", "KXCMG": "CMG",
    "KXCVNA": "CVNA", "KXDPZ": "DPZ", "KXSBUX": "SBUX", "KXRBLX": "RBLX",
    "KXNCLH": "NCLH", "KXLUV": "LUV", "KXWH": "WH", "KXPM": "PM",
    "KXRACE": "RACE", "KXTLN": "TLN", "KXTLNA": "TLN", "KXWING": "WING",
    "KXWINGA": "WING", "KXFSLR": "FSLR", "KXFSLRA": "FSLR", "KXYOU": "YOU",
    "KXBA": "BA", "KXRDDT": "RDDT", "KXCOINBASE": "COIN",
    # Finecon KPI sweep (Jack 2026-09-04 "yes company-KPI set should be in
    # the scan"): these live in incentive_mm._FINECON_KPI_SERIES (the
    # finecon top-N group), not _DEFAULT_COMPANY_SERIES, but their release
    # guard is THIS map + the disclosure sweep below.
    "KXDKS": "DKS", "KXZM": "ZM", "KXURBN": "URBN", "KXLOW": "LOW",
    "KXDG": "DG", "KXAFRM": "AFRM", "KXBBY": "BBY", "KXWSM": "WSM",
    "KXOKTA": "OKTA",
}
_nasdaq_cache: dict = {}


def nasdaq_earnings_for_date(date_iso: str) -> dict:
    """{ticker: time_flag} for a Nasdaq calendar date (cached per run)."""
    if date_iso in _nasdaq_cache:
        return _nasdaq_cache[date_iso]
    out = {}
    try:
        r = requests.get(
            f"https://api.nasdaq.com/api/calendar/earnings?date={date_iso}",
            headers=UA, timeout=15)
        for row in ((r.json() or {}).get("data") or {}).get("rows") or []:
            sym = (row.get("symbol") or "").upper()
            if sym:
                out[sym] = row.get("time") or ""
    except Exception as e:
        log(f"! nasdaq fetch {date_iso} failed: {e}")
    _nasdaq_cache[date_iso] = out
    return out


def nasdaq_release_datetime(ticker: str, now, days: int):
    """Scan the Nasdaq calendar forward `days` days for `ticker`'s next
    earnings -> (datetime_ET, label) with after-hours->4pm ET / pre-market->
    7am ET, or None. Scans from NOW so it's robust to Kalshi's wrong
    occurrence (INTC occ Jul 25 but the real report is Jul 23).

    When Nasdaq has the DATE but no time flag the fallback is 7am ET, NOT
    4pm — see the comment on the else-branch. The label carries a
    provenance_of() guess marker in that case so the sidecar and the digest
    can tell a synthesized hour from a measured one."""
    from datetime import timedelta
    tkr = ticker.upper()
    for i in range(days + 1):
        d = (now + timedelta(days=i)).astimezone(ET).date()
        flag = nasdaq_earnings_for_date(d.isoformat()).get(tkr)
        if flag is None:
            continue
        if "after" in flag:
            hour, label = 16, "after close (4pm ET, Nasdaq)"
        elif "pre" in flag or "before" in flag:
            hour, label = 7, "before open (~7am ET, Nasdaq)"
        else:
            # FAIL SAFE = FAIL EARLY. Jack 2026-08-06, after
            # KXEARNINGSMENTIONCELH-26AUG06. Nasdaq's calendar carried
            # "time-not-supplied" for CELH on 2026-08-02, this branch read 16
            # and wrote "2026-08-06T16:00:00-04:00", and Celsius in fact
            # reported BEFORE the open with an 8:00am ET call — so the cutoff
            # had not passed, the bot quoted 15 markets straight through a live
            # call with the Q2 results already public, and Jack had to stop it
            # by hand at 12:31Z.
            #
            # The two ways of being wrong are NOT symmetric and this branch is
            # the only place that choice is made:
            #   guess AMC, truth is BMO -> the bot market-makes through a
            #       morning call against people who have read the print. LOSES
            #       MONEY, and the loss scales with how good the pool is.
            #   guess BMO, truth is AMC -> the bot stands down at 06:50 ET and
            #       forfeits a day of reward accrual. RISKS NOTHING.
            # Same asymmetry as the override-vs-ticker rule established
            # 2026-08-05 on KXEARNINGSMENTIONLLY-26AUG07: EARLY is conservative,
            # LATE is exposure. An unknown hour must therefore land EARLY.
            #
            # 7am (not 6am, not midnight) to stay on the same anchor the
            # measured pre-market branch above already uses in production — a
            # guess should not invent a new number, and going earlier would
            # forfeit accrual on evidence nobody has.
            #
            # "time-not-supplied" is not rare: 106 of 550 rows (19%) on the
            # 2026-08-06 calendar. It is also PROVISIONAL — Nasdaq filled CELH
            # in as "time-pre-market" days later — which is what the
            # provisional re-check in main() exists to pick up, since this
            # value would otherwise be frozen forever by the `covered`
            # short-circuit. Keep a _GUESS_MARKERS substring in the label
            # (below: "time n/a", "fail-safe") or the provenance sidecar
            # silently reclassifies these as measurements.
            hour, label = 7, "time n/a->7am ET BMO assumed (Nasdaq, fail-safe)"
        return ET.localize(datetime(d.year, d.month, d.day, hour, 0)), label
    return None


def parse_release_time(page_text: str, year=None, ref=None):
    """(datetime_ET, label, evidence) for the earnings RELEASE, or None. A
    report+results context with a date, plus after-close (->4pm ET) /
    before-open (->7am ET) / a stated ET time not next to 'call'/'webcast'."""
    text = re.sub(r"\s+", " ", page_text)
    for kw in REPORT_RE.finditer(text):
        window = text[max(0, kw.start() - 60):kw.end() + 320]
        if not re.search(r"result|earnings|quarter|financial", window, re.I):
            continue                              # not an earnings-report context
        dm = DATE_RE.search(window)
        if not dm:
            continue
        month = MONTHS[dm.group(1).lower()]
        day = int(dm.group(2))
        amc, bmo = AMC_RE.search(window), BMO_RE.search(window)
        tm = TIME_RE.search(window)
        near_call = bool(tm) and bool(re.search(
            r"call|webcast", window[max(0, tm.start() - 45):tm.end() + 45], re.I))
        if tm and not near_call:
            hour = int(tm.group(1))
            minute = int(tm.group(2) or 0)
            if "p" in tm.group(3).lower() and hour != 12:
                hour += 12
            if "a" in tm.group(3).lower() and hour == 12:
                hour = 0
            label = "stated ET time"
        elif amc:
            hour, minute, label = 16, 0, "after close (4pm ET)"
        elif bmo:
            hour, minute, label = 7, 0, "before open (~7am ET)"
        else:
            continue
        try:
            dt_et = ET.localize(datetime(_year_for(month, day, dm.group(3), year, ref),
                                         month, day, hour, minute))
        except ValueError:
            continue
        span = amc or bmo or tm
        evidence = window[max(0, span.start() - 55):span.end() + 40].strip()
        return dt_et, label, evidence
    return None


def discover_events(client):
    """Active KXEARNINGSMENTION events from the incentive program feed."""
    events = set()
    cursor = None
    while True:
        params = {"status": "active", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/incentive_programs", params=params)
        for p in resp.get("incentive_programs") or []:
            t = p.get("market_ticker", "")
            if t.startswith(_EARNINGS_PREFIX):
                events.add(t.rsplit("-", 1)[0])
        cursor = resp.get("next_cursor")
        if not cursor:
            break
    return sorted(events)


# ---------------------------------------------------------------------------
# STALE-TICKER TRAP autofix (Jack 2026-08-03, after KXEARNINGSMENTIONPGR-26JUL15
# sat dead with 12 markets x $28.67/day of pool). Kalshi stamps "next earnings
# call" events with a ticker date that goes STALE once the quarter moves on: the
# ticker says 26JUL15, the market closes Dec 31, and the bot's midnight-ET
# ticker-date rule therefore sets a cutoff three weeks in the PAST -> _screen
# returns 'cutoff' forever and the event silently never quotes.
#
# Signature: earnings-mention event + ticker date passed + market still OPEN +
# live incentive programs + no override. Those cannot coexist legitimately.
# Nasdaq is retried with a much longer horizon than the normal path (a stale
# event's real call can be a full quarter out), and whatever is left is
# reported with its POOL COST so it cannot be lost in the noise.
STALE_LOOKAHEAD_DAYS = int(os.environ.get("IMM_STALE_LOOKAHEAD_DAYS", "120"))


def discover_stale_ticker_events(client, now):
    """[(event, n_markets, pool_per_day, close_time)] for earnings-mention
    events whose ticker date has passed while the market is still open and
    paying — the trap class that never self-heals."""
    pools, samples = {}, {}
    cursor = None
    while True:
        params = {"status": "active", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/incentive_programs", params=params)
        batch = resp.get("incentive_programs") or []
        for p in batch:
            t = p.get("market_ticker", "")
            if not t.startswith(_EARNINGS_PREFIX) or p.get("paid_out"):
                continue
            ev = t.rsplit("-", 1)[0]
            start = imm.parse_iso_utc(p.get("start_date", ""))
            end = imm.parse_iso_utc(p.get("end_date", ""))
            if not start or not end or not (start <= now < end):
                continue
            days = max((end - start).total_seconds() / 86400.0, 1.0 / 24)
            dpd = (p.get("period_reward") or 0) / 10000.0 / days
            cur = pools.setdefault(ev, [0, 0.0])
            cur[0] += 1
            cur[1] += dpd
            samples.setdefault(ev, t)
        cursor = resp.get("next_cursor")
        if not cursor or not batch:
            break

    out = []
    for ev, (n, dpd) in sorted(pools.items()):
        if ev in EVENT_START_OVERRIDES:
            continue
        d = parse_event_date(ev)
        if d is None or d > now:
            continue                       # ticker date still ahead: normal path
        try:
            m = ((client.get_market(samples[ev]) or {}).get("market")) or {}
        except Exception as e:
            log(f"! stale-check market read failed {ev}: {e}")
            continue
        if m.get("status") not in ("active", "open"):
            continue                       # genuinely over, not a trap
        close = imm.parse_iso_utc(m.get("close_time", ""))
        if close is None or close <= now:
            continue
        out.append((ev, n, dpd, close))
    return out


def source_urls(client, event: str):
    """Candidate pages: the series' settlement sources (usually the IR page)."""
    series = event.split("-")[0]
    urls = []
    try:
        resp = client.get(f"/series/{series}")
        for s in (resp.get("series") or {}).get("settlement_sources") or []:
            u = s.get("url")
            if u:
                urls.append(u)
    except Exception as e:
        log(f"! series read failed for {series}: {e}")
    return urls[:3]


# THE COMPANY'S OWN ANNOUNCEMENT (Jack 2026-09-27: "you were able to figure
# out what the earnings call times were, so make sure the fallback can do
# that"). ARITZIA and DPZ sat UNRESOLVED on 41 straight runs while both
# companies had published their dates: Aritzia is on no Nasdaq calendar and its
# IR page is JavaScript-only; Domino's IR site answers scripts with 403 and its
# Oct 13 report sat one day past the Nasdaq window. earnings_announcements
# reads the same notices a person finds -- the IR site's event feed and the
# press releases -- and runs FIRST, because it also carries the real release
# time (Domino's 6:05am, where the Nasdaq pre-market proxy says 7:00).
# An announced date more than this many days past the ticker date is not
# written (a different quarter's notice, or a misparse): LATE is the direction
# that quotes through a call, so it goes to the ACTION email instead.
ANNOUNCED_MAX_LATE_DAYS = int(os.environ.get("IMM_ANNOUNCED_MAX_LATE_DAYS", "45"))


def announced_release(client, ev: str, now, rel=None):
    """(datetime_ET, label, url, note) from the company's own notice of the
    event's call, merged with Nasdaq's calendar reading `rel` when there is
    one, or None. The EARLIER of the two wins; `note` is set when sources put
    the event more than a day apart, for the ACTION email. Never raises."""
    series = ev.split("-")[0]
    sym = (series[len(_EARNINGS_PREFIX):]
           if series.startswith(_EARNINGS_PREFIX) else "")
    try:
        found = earnings_announcements.find_announced(
            sym, source_urls(client, ev), now)
    except Exception as e:                       # noqa: BLE001 -- never fatal
        log(f"! announcement lookup failed for {ev}: {e}")
        return None
    if not found:
        return None
    dt, label, url = found["anchor"], found["label"], found.get("url") or ""
    note = ("the company's press release and its IR event feed disagree"
            if found.get("conflict") else None)
    td = parse_event_date(ev)
    if td is not None and (dt - td).days > ANNOUNCED_MAX_LATE_DAYS:
        log(f"! announced {dt.isoformat()} for {ev} is more than "
            f"{ANNOUNCED_MAX_LATE_DAYS}d past its ticker date; not written "
            f"[{label}] {url}")
        return None
    if rel is not None:
        if abs((rel[0].date() - dt.date()).days) > 1:
            note = f"Nasdaq's calendar says {rel[0]:%a %b %d} [{rel[1]}]"
        dt = min(dt, rel[0])
    return dt, label, url, note


# ---- daily series auto-enrollment (Jack 2026-07-22) -------------------------
# Classify newly-programmed series against the strategy families and enroll
# matches into EXTRA_ALLOW_FILE (hot-reloaded by the bot). Never touches the
# fleet's monthly crypto or anything blocklisted; leftovers go to the email
# as a REVIEW list.
# month must be a real month token ("26FAUSTO" is a hurricane, not a metric);
# requires a letter metric tail directly after the month (26JULDELIV) with NO
# day — real company metrics are month-scoped, while day+tail is the person/
# event pattern (26OCT02JALVAREZ, a boxer — caught as a false positive in the
# first dry run). Bare day-dated events land in the review email instead.
COMPANY_EVENT_RE = re.compile(
    r"^\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]{2,10}$")
CRYPTO_YEARLY_RE = re.compile(r"^KX[A-Z0-9]{2,8}(MINY|MAXY)$")
# Day-dated event segment (26OCT08) — the dated-observation consumer shape.
DAY_DATED_EVENT_RE = re.compile(
    r"^\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}$")


def classify_series(series: str, sample_ticker: str):
    """-> ('enroll', reason) | ('review', hint) | ('skip', reason)."""
    if series.endswith("MAXMON") or series.endswith("MINMON"):
        return "skip", "fleet monthly crypto"
    if any(series.startswith(p) for p in imm.SERIES_BLOCKLIST_PREFIXES):
        return "skip", "blocklisted (cloud fleet / manual)"
    if "MENTION" in series:
        return "enroll", "mention family (tailed variant)"
    if CRYPTO_YEARLY_RE.match(series):
        return "enroll", "crypto yearly min/max"
    if series.startswith("KXTEMP"):
        return "review", "new temp city — needs IMM_TEMP_SERIES override"
    parts = sample_ticker.split("-")
    # Dated-observation consumer families (Jack allowlisted the foot-traffic
    # and app-chart families wholesale 2026-08-31; 2026-09-01 "fix this
    # going forward" after Kalshi added 12 new *APP series overnight): new
    # members are pre-approved by family, so ENROLL them — the no-new
    # company rule below is about earnings-release companies, not these.
    # The day-dated event + T-threshold strike shape keeps unrelated
    # *FT/*APP-ending series (KXNFLDRAFT-26APR30-<person>) in review; the
    # bot clones family guards via FAMILY_OVERRIDE_PARENTS at refresh.
    if (series.endswith(_CONSUMER_OBS_SUFFIXES) and len(parts) >= 3
            and DAY_DATED_EVENT_RE.match(parts[1])
            and parts[2].startswith("T")):
        return "enroll", "dated-observation consumer family (FT/APP)"
    if len(parts) >= 2 and COMPANY_EVENT_RE.match(parts[1]) and len(series) <= 14:
        # NO-NEW company rule (Jack 2026-07-28): the bot no longer admits
        # fresh company markets, so the classifier must not ALLOW new company
        # series either — surface in REVIEW for visibility instead. (The
        # bot-side NO_NEW_SERIES gate covers already-allowed series; this
        # closes the front door for brand-new tickers.)
        return "review", "company/consumer shape — NOT enrolled (no-new company rule 7/28)"
    return "review", "unclassified"


def enroll_new_series(client, dry: bool):
    """Returns (enrolled, review) lists for the email."""
    load_extra_allow_series()
    seen: dict = {}
    cursor = None
    while True:
        params = {"status": "active", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/incentive_programs", params=params)
        for p in resp.get("incentive_programs") or []:
            t = p.get("market_ticker", "")
            s = t.split("-")[0]
            if s and s not in seen:
                seen[s] = t
        cursor = resp.get("next_cursor")
        if not cursor:
            break
    enrolled, review = [], []
    additions = []
    for s, sample in sorted(seen.items()):
        if imm.IncentiveMarketMaker._allowed(sample):
            continue                      # already covered somewhere
        # A family-suffix series the bot has not source-verified yet
        # (2026-09-22, incentive_mm.ALLOW_FAMILY_SUFFIXES): the bot's own
        # refresh admits or rejects it within the hour; classifying it here
        # could only write a Carbon Arc sibling into the finecon file, where
        # the loader now refuses it anyway. A verified NON-member (the
        # KXAMAZONADS lawsuit binary) falls through and is classified like
        # any other series.
        if imm.series_family_suffix(s) and imm.family_verdict(s) is None:
            continue
        verdict, why = classify_series(s, sample)
        if verdict == "enroll":
            additions.append(s)
            enrolled.append((s, why, sample))
        elif verdict == "review":
            review.append((s, why, sample))
    # Carbon Arc self-extension (Jack 2026-09-05 "yes self-extend carbon
    # arc"): a REVIEW series whose Kalshi settlement source is Carbon Arc
    # (NOT the *CC / *ADS / *POS name-pattern families: since 2026-09-10
    # (CC) and 2026-09-22 (ADS, POS) the bot allows those into the NORMAL
    # book by suffix + its own Carbon Arc source verdict --
    # incentive_mm.ALLOW_FAMILY_SUFFIXES -- so `_allowed` above skips the
    # verified members and the pending-verdict skip holds the rest back;
    # the bot's loader also refuses family-suffix names from the finecon
    # file, so nothing written here could re-absorb them.)
    # is a dated-observation vendor print — the finecon class — so it
    # joins the finecon group file (hot-reloaded by the bot into
    # FINECON_SERIES: group walk, caps, guards, allowance) instead of
    # waiting in the review email. One /series read per NOVEL series per
    # run; everything already allowed was skipped above, so the steady
    # state is zero reads.
    # Elections by Kalshi category (Jack 2026-10-05, after the 21:02Z batch
    # left 13 of its 15 Elections series unquoted): a REVIEW series Kalshi
    # files under "Elections" joins the election family through
    # incentive_mm.ELECTION_EXTRA_FILE (hot-reloaded), less the 9/28
    # carve-outs (ELECTION_EXCLUDE). It still needs a verified ELECTION_DATES
    # row to quote -- the email's ACTION list names it. Same /series read as
    # the Carbon Arc check below, so still one read per novel series.
    fin_additions = []
    elec_additions = []
    still_review = []
    for s, why, sample in review:
        se = series_meta(client, s)
        if s not in imm.ELECTION_EXCLUDE and se.get("category") == "Elections":
            elec_additions.append(s)
            enrolled.append((s, ELECTION_ENROLL_WHY, sample))
        elif imm.series_is_carbon_arc(se):
            fin_additions.append(s)
            enrolled.append((s, "Carbon Arc source -> finecon group", sample))
        else:
            still_review.append((s, why, sample))
    review = still_review
    if elec_additions and not dry:
        _merge_series_file(imm.election_extra_path(), elec_additions)
        log(f"election-enrolled {len(elec_additions)} Kalshi Elections "
            f"series -> {imm.election_extra_path()}")
    if fin_additions and not dry:
        _merge_series_file(imm.FINECON_EXTRA_FILE, fin_additions)
        log(f"finecon-extended {len(fin_additions)} Carbon Arc series -> "
            f"{imm.FINECON_EXTRA_FILE}")
    if additions and not dry:
        _merge_series_file(EXTRA_ALLOW_FILE, additions)
        log(f"enrolled {len(additions)} new series -> {EXTRA_ALLOW_FILE}")
    return enrolled, review


ELECTION_ENROLL_WHY = "Kalshi Elections category -> election family"


def series_meta(client, series: str) -> dict:
    """Kalshi's /series record ({} when the read fails)."""
    try:
        return (client.get(f"/series/{series}") or {}).get("series") or {}
    except Exception as e:
        log(f"! series read failed for {series}: {e}")
        return {}


def carbon_arc_series(client, series: str) -> bool:
    """True when the series' Kalshi settlement sources name Carbon Arc."""
    try:
        return imm.series_is_carbon_arc(series_meta(client, series))
    except Exception as e:
        log(f"! series source read failed for {series}: {e}")
        return False


def _merge_series_file(path: str, additions) -> None:
    """Merge series into a {"series": [...]} json file, atomically."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        data = {}
    cur = set(data.get("series") or [])
    cur.update(additions)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"series": sorted(cur)}, f, indent=1)
    os.replace(tmp, path)


# ---- company-disclosure release-time cutoffs (Jack 2026-07-23) --------------
# Company operating-metric markets (headcount, DAU, funded accounts, comps...)
# resolve on numbers disclosed in the earnings PRESS RELEASE — earlier than the
# call, and Kalshi's occurrence_datetime does NOT reliably give it (INTC was
# stamped Jul 25 but Intel released Jul 23; others are midnight-ET placeholders
# hours after a real ~4pm release). So these need an explicit release-time
# override in the SAME file the bot hot-reloads. The consumer-price trackers
# are excluded — their menu-price observation is a fixed dated event handled by
# the ticker/occurrence already.
_CONSUMER_PRICE_SERIES = {
    "KXSBUXSAR", "KXCFACHICKSAND", "KXPOPCHICKSAND", "KXCHIPBURRITO",
    "KXDDCOLDBREW", "KXBKNUGGETS", "KXAMSAVO",
    # Foot-traffic + app-chart series (swept 2026-08-31 with their
    # allowlisting; KXBKFT/KXYUMTBFT belonged here since 8/3): dated
    # observations like the price trackers — there is no press release to
    # find a time for, so surfacing them as disclosure events would only
    # nag for overrides that cannot exist. Membership is by suffix so the
    # family additions (12 new *APP series on 9/1 alone) never regress
    # this; the comprehension below applies it.
    "KXBKFT", "KXYUMTBFT"}
_CONSUMER_OBS_SUFFIXES = ("FT", "APP")
# The finecon KPI series (enrolled 2026-09-04 via the finecon group, not
# the company allowlist) settle on earnings press-release numbers exactly
# like the rest of this set, so they get the same release-time cutoffs —
# without this, a program period spanning a report week would quote
# straight through the release (no day in tickers like KXZM-26NOVCUST100K,
# so the midnight-ET rule never fires for them).
COMPANY_DISCLOSURE_SERIES = {
    s for s in (imm._DEFAULT_COMPANY_SERIES + ","
                + getattr(imm, "_FINECON_KPI_SERIES", "")).split(",")
    if s and s not in _CONSUMER_PRICE_SERIES
    and not s.endswith(_CONSUMER_OBS_SUFFIXES)}
# Only surface events whose (unreliable) occurrence is within this many days,
# so the daily email flags them a bit ahead without spamming months-out ones.
DISCLOSURE_LEAD_DAYS = int(os.environ.get("IMM_DISCLOSURE_LEAD_DAYS", "12"))


def discover_company_disclosure(client, now):
    """Active company-disclosure events lacking a release override, whose
    Kalshi occurrence is within DISCLOSURE_LEAD_DAYS. -> [(event, occ_iso)]."""
    from datetime import timedelta
    seen = {}
    cursor = None
    while True:
        params = {"status": "active", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/incentive_programs", params=params)
        for p in resp.get("incentive_programs") or []:
            t = p.get("market_ticker", "")
            if t.split("-")[0] in COMPANY_DISCLOSURE_SERIES:
                seen.setdefault(t.rsplit("-", 1)[0], t)
        cursor = resp.get("next_cursor")
        if not cursor:
            break
    horizon = now + timedelta(days=DISCLOSURE_LEAD_DAYS)
    out = []
    for ev, sample in sorted(seen.items()):
        if ev in EVENT_START_OVERRIDES:
            continue
        m = (client.get_market(sample) or {}).get("market") or {}
        occ_iso = m.get("occurrence_datetime")
        occ = parse_iso_utc(occ_iso or "")
        # imminent (or undated -> surface it, cutoff source is unknown)
        if occ is None or occ <= horizon:
            out.append((ev, occ_iso))
    return out


# ---------------------------------------------------------------------------
# Phase 4: scheduled-broadcast mention events (Jack 2026-08-01, after the
# KXFOXNEWSMENTION-26AUG01 miss: a NEW mention series has no resolver source,
# so its same-day event dies at the midnight-of-ticker-date fallback silently
# — the programs went live at 16:19Z for a 9pm show and the bot never looked).
# Sweep: every active-program non-earnings MENTION event with a near ticker
# date that the bot's own EventStartResolver cannot place. Best-effort air
# time from the TVmaze US schedule (exact show-name match only); email a
# paste-ready --set for the rest.
# CORRECTION 2026-09-08: this sweep's original premise — "no override -> the
# bot just keeps NOT quoting" — was never a rule. A dated ticker gives the
# midnight-ET fallback, which IS a cutoff, so the bot quotes right up to
# air; it only looked safe because short listing windows died at the $1
# payout floor. KXWORLDNEWSMENTION-26SEP08 listed at 17:45Z between two
# runs of this task, cleared the floor on ten hours of window, and quoted
# 16 markets through the 6:30pm show. The bot now handles the class itself
# (incentive_mm MENTION_NO_CUTOFF_GATE): an unresolved mention event puts
# its series on the live-event depth gate — pads off, whole event stands
# down on thin depth — until an override lands. So a --set from this email
# is what UN-gates the event and lets it quote normally to the real start.
# ---------------------------------------------------------------------------

BROADCAST_LOOKAHEAD_DAYS = int(os.environ.get("IMM_BCAST_LOOKAHEAD_DAYS", "3"))
TVMAZE_SCHED = "https://api.tvmaze.com/schedule?country=US&date={date}"
SHOW_TITLE_RE = re.compile(r"during\s+(?:fox news:\s*)?([^?]+?)\s*\??\s*$", re.I)


def _norm_show(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def discover_broadcast_mention_events(client, now):
    """[(event, series)] for active-program non-earnings MENTION events with
    a parseable ticker date within the lookahead that the bot's resolver
    cannot place — the population that silently dies at the midnight rule."""
    events = {}
    cursor = None
    for _page in range(20):
        params = {"limit": 1000, "status": "active"}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/incentive_programs", params=params)
        batch = resp.get("incentive_programs") or []
        for p in batch:
            t = p.get("market_ticker") or ""
            series = t.split("-")[0]
            if not series.endswith("MENTION") or series.startswith(_EARNINGS_PREFIX):
                continue
            events.setdefault("-".join(t.split("-")[:2]), series)
        cursor = resp.get("next_cursor")
        if not cursor or not batch:
            break
    out = []
    resolver = imm.EventStartResolver()
    today_et = now.astimezone(ET).date()
    for ev, series in sorted(events.items()):
        d = parse_event_date(ev)
        if d is None:
            continue                      # no ticker date -> midnight rule N/A
        days_out = (d.astimezone(ET).date() - today_et).days
        if not (0 <= days_out <= BROADCAST_LOOKAHEAD_DAYS):
            continue
        if resolver.resolve(series, ev) is not None:
            continue                      # bot already derives a real start
        out.append((ev, series))
    return out


def tvmaze_airtime(show_title: str, date_et):
    """(datetime ET, network) for an exact normalized show-name match on the
    US schedule that day, else None. Exact match only — a wrong air time is
    worse than an email."""
    try:
        r = requests.get(TVMAZE_SCHED.format(date=date_et.isoformat()),
                         headers=UA, timeout=15)
        r.raise_for_status()
        entries = r.json()
    except Exception as e:
        log(f"! tvmaze fetch failed: {e}")
        return None
    want = _norm_show(show_title)
    if not want:
        return None
    for e in entries:
        show = ((e.get("show") or {}).get("name")) or ""
        if _norm_show(show) == want and e.get("airstamp"):
            dt = parse_iso_utc(e["airstamp"])
            if dt:
                net = (((e.get("show") or {}).get("network") or {})
                       .get("name")) or "?"
                return dt.astimezone(ET), net
    return None


# White-House schedule auto-resolve (Jack 2026-08-03, KXTRUMPMENTION-26AUG03
# "add to the auto-resolver to find a start time": an Executive Order signing
# has no TVmaze entry; Roll Call Factbase publishes the WH daily schedule as
# JSON). Validated live 2026-08-03: the feed carried "13:30:00 / The President
# signs an Executive Order" matching the hand-set override exactly. Anything
# ambiguous stays UNRESOLVED — a wrong start time is worse than the email.
WH_SCHEDULE_JSON = os.environ.get(
    "IMM_WH_SCHEDULE_JSON",
    "https://media-cdn.factba.se/rss/json/trump/calendar-full.json")
WH_SCHEDULE_SERIES = tuple(s for s in os.environ.get(
    "IMM_WH_SCHEDULE_SERIES", "KXTRUMPMENTION").split(",") if s)
_WH_STOP = frozenset(
    "what will trump say during the a an of at in on to his her president "
    "participates and or".split())
_wh_cache: dict = {}


def _wh_words(s: str) -> set:
    out = set()
    for w in re.findall(r"[a-z0-9]+", s.lower()):
        if w in _WH_STOP:
            continue
        out.add(w[:-3] if w.endswith("ing") else (w[:-1] if w.endswith("s") else w))
    return out


# PLACE FALLBACK (2026-10-05, KXTRUMPMENTION-26OCT05 "his rally in Nebraska"):
# the timed entry was "The President delivers Remarks [6:00 PM Local]" at
# "Pinnacle Bank Expo Center, Grand Island, NE" -- not one word in common with
# the title, so the event went UNRESOLVED and the bot ran it on the ticker-day
# live gate (halted twice, 21h and 9h before a 7pm speech). Kalshi's sub_title
# names the place ("Midterm Rally in Grand Island, Nebraska") and the entry's
# LOCATION carries it. Only when the title-vs-details match finds nothing or
# ties (AUG05 Las Vegas: nine entries naming the city tied at 2 words, the
# remarks scored 1; the fallback picks them 3-to-2, the hand-set 16:30):
# (title + sub_title) vs (details + location), trailing state code spelled out
# for one-word states, numbers and the generic venue words every White House
# entry shares dropped -- the same >=2-word, unique-best rule, so a tie (the
# remarks AND a timed departure to the same city) still resolves to nothing.
_WH_PLACE_STOP = frozenset(_wh_words(
    "donald originally scheduled for white house oval office room center "
    "joint base andrews january february march april may june july august "
    "september october november december"))
_WH_STATES = dict(s.split("=") for s in (
    "AL=alabama AK=alaska AZ=arizona AR=arkansas CA=california CO=colorado "
    "CT=connecticut DE=delaware FL=florida GA=georgia HI=hawaii ID=idaho "
    "IL=illinois IN=indiana IA=iowa KS=kansas KY=kentucky LA=louisiana "
    "ME=maine MD=maryland MA=massachusetts MI=michigan MN=minnesota "
    "MS=mississippi MO=missouri MT=montana NE=nebraska NV=nevada OH=ohio "
    "OK=oklahoma OR=oregon PA=pennsylvania TN=tennessee TX=texas UT=utah "
    "VT=vermont VA=virginia WA=washington WI=wisconsin WY=wyoming").split())


def _wh_place_words(s: str) -> set:
    s = s.strip()
    m = re.search(r",\s*([A-Z]{2})$", s)
    if m and m.group(1) in _WH_STATES:
        s = s[:m.start()] + " " + _WH_STATES[m.group(1)]
    return {w for w in _wh_words(s) if w not in _WH_PLACE_STOP and not w.isdigit()}


def _wh_unique_best(entries, score):
    """The single entry with the top score (>= 2 shared words), or None when
    nothing scores or the top score is tied (ambiguous match)."""
    scored = sorted(((score(e), e) for e in entries), key=lambda x: -x[0])
    scored = [x for x in scored if x[0] >= 2]
    if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1]


def wh_schedule_start(title: str, date_et, sub_title: str = ""):
    """(datetime ET, matched schedule details) from the Factbase WH calendar:
    the UNIQUE best keyword match on that date with >=2 shared content words
    and a concrete time, else None. Title vs details first; the place
    fallback (see _WH_PLACE_STOP) when that finds nothing or ties; then a
    title word exactly one timed entry of the day carries."""
    if "entries" not in _wh_cache:
        try:
            r = requests.get(WH_SCHEDULE_JSON, headers=UA, timeout=20)
            r.raise_for_status()
            d = r.json()
            _wh_cache["entries"] = d if isinstance(d, list) else (
                d.get("data") or d.get("items") or [])
        except Exception as e:
            log(f"! WH schedule fetch failed: {e}")
            _wh_cache["entries"] = []
    day = [it for it in _wh_cache["entries"]
           if str(it.get("date")) == date_et.isoformat() and it.get("time")]
    want = _wh_words(title)
    it = _wh_unique_best(day, lambda e: len(
        want & _wh_words(str(e.get("details") or ""))))
    if it is None and sub_title:
        place = _wh_place_words(f"{title} {sub_title}")
        it = _wh_unique_best(day, lambda e: len(place & _wh_place_words(
            f"{e.get('details') or ''} {e.get('location') or ''}")))
    if it is None and len(want) == 1:
        # SINGLE WORD, UNIQUE (Jack 2026-10-06, KXTRUMPMENTIONB-26OCT07: "his
        # announcement?" vs "13:00 The President makes an Announcement" --
        # a title with ONE content word can never reach the >=2 bar): that
        # word in exactly ONE timed entry of the day. Two entries with it (the
        # two 10/07 Policy Meetings) resolve to nothing, and travel entries
        # never count. Titles with more words stay on the >=2 rules: "his
        # rally in Nebraska" would otherwise pick the one timed entry naming
        # Nebraska -- the 8:30pm DEPARTURE (test_place_tie_resolves_to_nothing).
        hits = [e for e in day
                if want & _wh_words(str(e.get("details") or ""))
                and not re.search(r"\b(departs|arrives)\b", str(e.get("details") or ""), re.I)]
        if len(hits) == 1:
            it = hits[0]
    if it is None:
        return None
    try:
        hh, mm = str(it["time"]).split(":")[:2]
        dt = ET.localize(datetime(date_et.year, date_et.month, date_et.day,
                                  int(hh), int(mm)))
    except (ValueError, KeyError):
        return None
    return dt, str(it.get("details") or "")


# RNC EVENTS PAGE (Jack 2026-10-06: "Rally start times: use the RNC events
# page as a source", after KXTRUMPMENTION-26OCT07 -- the San Antonio rally,
# events.gop.com "Wed, October 07, 2026 - 06:00 pm (US/Central)" -- went
# UNRESOLVED at the 4:45pm run the day before: the WH schedule lists a rally
# only a day ahead, so the event ran on the ticker-day live gate and a routine
# 8:41pm ET fill halted it). The site has no index or API ("Powered by
# Nucleus", /events and /sitemap.xml 404), but a rally page's slug is its
# name + "-president-donald-j-trump", and Kalshi's event sub_title carries the
# name ("Donald Trump - Midterm Rally in San Antonio, Texas originally
# scheduled for October 7, 2026"): verified on San Antonio (10/07) and Grand
# Island (10/05). Rallies only (the RNC site does not list Oval Office or
# White House events), tried AFTER the WH schedule, and a page counts only
# when it names the city and its own date is the event's date -- a wrong
# start time is worse than the UNRESOLVED email.
RNC_EVENTS_BASE = os.environ.get("IMM_RNC_EVENTS_BASE", "https://events.gop.com/events/")
RNC_SLUG_SUFFIXES = ("-president-donald-j-trump", "-featuring-president-donald-j-trump",
                     "-with-president-donald-j-trump", "")
_RNC_WHEN_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})"
    r"\s*-\s*(\d{1,2}):(\d{2})\s*([ap]m)\s*\(\s*([A-Za-z_]+/[A-Za-z_]+)\s*\)", re.I)
# SPEAKER TIME (Jack 2026-10-08: "Use the speaker time for things like trump
# remarks"). The page's header time is the PROGRAM start, not Trump's: the
# Syracuse page (10/09) reads "05:30 pm (US/Eastern)" over an Event Schedule
# of "3:00 PM EST: Doors Open / 5:30 PM EST: Program Begins / 7:00 PM EST:
# Remarks Begin". A remarks line on the header's day, at or up to
# RNC_REMARKS_MAX_AFTER_H after the header time, wins; its wall time is read
# in the header's zone (the pages write "EST" year-round).
_RNC_REMARKS_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\s*(?:[A-Z]{1,4})?\s*[:-]\s*"
    r"(?:(?:President\s+)?(?:Donald\s+J\.?\s+)?Trump(?:'s)?\s+)?Remarks\b", re.I)
RNC_REMARKS_MAX_AFTER_H = 6


def rnc_event_name(sub_title: str):
    """'Midterm Rally in San Antonio, Texas' out of Kalshi's sub_title, or
    None when it is not a rally."""
    name = re.sub(r"^\s*Donald Trump\s*-\s*", "", sub_title or "")
    name = re.sub(r"\s+originally scheduled for .*$", "", name).strip(" ?")
    return name if re.search(r"\brally\b", name, re.I) and " in " in name else None


def _rnc_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# The slug spells the state out (San Antonio "-texas", Grand Island
# "-nebraska") or abbreviates it (Syracuse 10/09: "midterm-rally-in-syracuse-
# ny-president-donald-j-trump", which the full-name slug missed and research
# had to find). Full name first, then the USPS code.
_US_STATE_ABBR = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc"}


def _rnc_names(name: str) -> list:
    """The rally name as Kalshi gives it, then with its state abbreviated."""
    head, _, state = name.rpartition(", ")
    abbr = _US_STATE_ABBR.get(state.strip().lower()) if head else None
    return [name] + ([f"{head} {abbr}"] if abbr else [])


def rnc_rally_start(sub_title: str, date_et, get=None):
    """(datetime ET, page url) for a Trump rally from its RNC events page, or
    None: the slug from Kalshi's sub_title, the page's own date/time/zone
    line, accepted only when the page names the city and its date is
    `date_et`."""
    import pytz
    name = rnc_event_name(sub_title)
    if not name:
        return None
    city = name.rsplit(" in ", 1)[1].split(",")[0].strip().lower()
    get = get or (lambda u: requests.get(u, headers=UA, timeout=20))
    for nm, suf in [(n, x) for n in _rnc_names(name) for x in RNC_SLUG_SUFFIXES]:
        url = RNC_EVENTS_BASE + _rnc_slug(nm) + suf
        try:
            r = get(url)
        except Exception as e:
            log(f"! RNC events fetch failed {url}: {e}")
            continue
        if getattr(r, "status_code", 0) != 200:
            continue
        html = r.text or ""
        m = _RNC_WHEN_RE.search(html)
        if not m or city not in html.lower():
            continue
        try:
            mon = datetime.strptime(m.group(1)[:3], "%b").month
            hh = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "pm" else 0)
            tz = pytz.timezone(m.group(7))
            local = tz.localize(datetime(
                int(m.group(3)), mon, int(m.group(2)), hh, int(m.group(5))))
        except (ValueError, pytz.UnknownTimeZoneError):
            continue
        # the speaker's own time when the page lists it (see _RNC_REMARKS_RE)
        rm = _RNC_REMARKS_RE.search(re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " "))
        if rm:
            try:
                rh = int(rm.group(1)) % 12 + (12 if rm.group(3).lower() == "p" else 0)
                said = tz.localize(datetime(local.year, local.month, local.day,
                                            rh, int(rm.group(2))))
            except ValueError:
                said = None
            if said is not None and timedelta(0) <= said - local <= timedelta(
                    hours=RNC_REMARKS_MAX_AFTER_H):
                local = said
        dt_et = local.astimezone(ET)
        if dt_et.date() != date_et and local.date() != date_et:
            continue                       # a page for another date
        return dt_et, url
    return None


def auto_broadcast_start(client, ev: str, series: str):
    """(iso, source, detail, title) for a broadcast mention event from the
    automatic resolvers -- for the WH-schedule series (KXTRUMPMENTION*) the
    Factbase calendar, then the RNC events page; then TVmaze for shows -- or
    (None, None, None, title) when none places it. Phase 4 below and the
    family watch (imm_family_watch.research_targets) share it."""
    title = sub_title = ""
    try:
        _e = ((client.get_event(ev) or {}).get("event") or {})
        title = _e.get("title") or ""
        sub_title = _e.get("sub_title") or ""
    except Exception as e:
        log(f"! event fetch failed {ev}: {e}")
    d = parse_event_date(ev)
    if d is None:
        return None, None, None, title
    d_et = d.astimezone(ET).date()
    # WH-schedule series: try the Factbase calendar before TVmaze -- these
    # events are appearances, not shows
    if series.startswith(WH_SCHEDULE_SERIES):
        wh = wh_schedule_start(title, d_et, sub_title)
        if wh:
            dt_et, det = wh
            return dt_et.isoformat(), "WH schedule", f"{title[:60]} => {det[:60]}", title
        # a rally the WH schedule does not list yet: its RNC events page
        rnc = rnc_rally_start(sub_title, d_et)
        if rnc:
            dt_et, url = rnc
            return dt_et.isoformat(), "RNC events", f"{sub_title[:60]} => {url[-60:]}", title
    m = SHOW_TITLE_RE.search(title)
    hit = tvmaze_airtime(m.group(1), d_et) if m else None
    if hit:
        dt_et, net = hit
        return dt_et.isoformat(), net, title[:90], title
    return None, None, None, title


def load_file() -> dict:
    try:
        with open(EVENT_OVERRIDES_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def write_file(data: dict) -> None:
    os.makedirs(os.path.dirname(EVENT_OVERRIDES_FILE), exist_ok=True)
    tmp = EVENT_OVERRIDES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, EVENT_OVERRIDES_FILE)


# ---------------------------------------------------------------------------
# PROVENANCE SIDECAR (Jack 2026-08-06, after KXEARNINGSMENTIONCELH-26AUG06).
#
# Nasdaq's calendar carried NO time flag for CELH, nasdaq_release_datetime()
# fell through to its else-branch and wrote 16:00 ET, and the bot quoted the
# event straight through an 8:00am call whose results were already public. The
# written value is INDISTINGUISHABLE from a real after-close reading — both are
# exactly "T16:00:00-04:00" — so nothing downstream could tell a measurement
# from a guess, and the digest's cutoff audit (which compares DATES) saw a
# perfect match and said nothing.
#
# This records HOW each value was obtained, next to the file rather than in it:
# incentive_mm.load_file_event_overrides() (:1481) does
# `parse_iso_utc(str(iso).strip())` on every value and DROPS the entry when that
# returns None, so a richer value shape would silently delete the cutoff and
# hand the event back to the midnight-ET ticker rule — the exact failure this is
# meant to prevent. The overrides file keeps its {event: ISO8601} contract
# untouched; this file is advisory and every reader must work without it.
#
# The recorded ISO is stored ALONGSIDE the confidence so a reader can tell
# whether the provenance still describes the live value. Jack's --set of CELH to
# 07:00 supersedes the guess; without that check the digest would keep flagging
# a value a human had already fixed, which is how this kind of section turns
# into noise and gets skipped.
OVERRIDE_META_FILE = os.path.join(os.path.dirname(EVENT_OVERRIDES_FILE),
                                  "event_start_overrides_meta.json")

# Substrings that mark a resolver label as a GUESS rather than a reading. Kept
# as a marker list, not an equality test on today's label text, so the fail-safe
# default can be re-worded (or flipped from 4pm to 7am) without silently
# reclassifying every guess as a measurement. Deliberately high-specificity:
# these are matched against RESOLVER LABELS, but a mis-plumbed call site could
# hand this scraped IR page text, and a generic marker like "assumed" or
# "default" would then mark a real reading as a guess. One false "guess" a week
# is what turns the digest section it feeds into something Jack skips.
_GUESS_MARKERS = ("time n/a", "time unknown", "no time", "fail-safe")


def provenance_of(label: str) -> str:
    """"guess" when the resolver had no time and synthesized one, else "read"
    (a Nasdaq AMC/BMO flag, a scraped IR time, a broadcast schedule time)."""
    low = str(label or "").lower()
    return "guess" if any(k in low for k in _GUESS_MARKERS) else "read"


def provenance_batch(resolved, rel_resolved, stale_fixed, bc_resolved) -> list:
    """[(event, iso, label)] from the phase result lists.

    All four shapes are decoded HERE rather than by editing the phases, so the
    phases keep their existing tuples and this stays a single-site change. Both
    Nasdaq paths bracket their label into the evidence string; rel_resolved
    carries it bare in position 2."""
    out = []
    for t in resolved:                    # (ev, iso, src, evidence["[label]"])
        m = re.search(r"\[([^\]]{0,80})\]", str(t[3]) if len(t) > 3 else "")
        out.append((t[0], t[1], m.group(1) if m else "IR page call time"))
    for t in rel_resolved:                # (ev, iso, label, url, evidence)
        out.append((t[0], t[1], str(t[2]) if len(t) > 2 else ""))
    for t in stale_fixed:                 # (ev, iso, n, dpd, "nasdaq:X [label]")
        m = re.search(r"\[([^\]]{0,80})\]", str(t[4]) if len(t) > 4 else "")
        out.append((t[0], t[1], m.group(1) if m else "stale-ticker autofix"))
    for t in bc_resolved:                 # (ev, iso, network, title)
        # an RNC rally page is named so the WH schedule can replace it later
        out.append((t[0], t[1], "broadcast schedule [RNC events]"
                    if len(t) > 2 and t[2] == "RNC events" else "broadcast schedule"))
    return out


def record_meta(resolutions, file_data: dict) -> None:
    """Merge {event: provenance} for this run into the sidecar and prune keys
    the overrides file no longer has. `resolutions` is [(event, iso, label)].

    Never raises: the sidecar is advisory and must never be able to take down
    the scheduled task that writes the real overrides."""
    try:
        meta = load_meta()
        stamp = datetime.now(timezone.utc).isoformat()
        for t in resolutions:
            try:
                ev, iso, label = str(t[0]), str(t[1]), str(t[2])
            except (IndexError, TypeError):
                continue
            meta[ev] = {"iso": iso, "confidence": provenance_of(label),
                        "label": label, "asof": stamp}
        for ev in [e for e in meta if e not in file_data]:
            meta.pop(ev, None)
        os.makedirs(os.path.dirname(OVERRIDE_META_FILE), exist_ok=True)
        tmp = OVERRIDE_META_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1, sort_keys=True)
        os.replace(tmp, OVERRIDE_META_FILE)
    except Exception as e:                       # advisory only — never fatal
        log(f"! override provenance sidecar not written: {e}")


def load_meta() -> dict:
    try:
        with open(OVERRIDE_META_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def provisional_events(file_data: dict) -> set:
    """Events whose override is a FAIL-SAFE GUESS that is still standing.

    The `covered` short-circuits (:288/:447/:706/:773) skip anything already in
    the overrides file, so a value written once is never re-derived. That is
    correct for a MEASURED time and wrong for a guessed one: on 2026-08-06
    Nasdaq had already corrected CELH from "time-not-supplied" to
    "time-pre-market", the right answer sat in the same endpoint for four days,
    and the thirteen scheduled runs in between each skipped the event without
    emitting a single log line (`covered` is emailed, never logged). Only a
    guess is re-opened; a measurement, once taken, stays taken.

    Two guards, both load-bearing:
      * confidence must be "guess" — a hand `--set` records as "read" (see
        main()), so a re-check can never overwrite a human's answer;
      * the sidecar's recorded ISO must still equal the live file value — if
        anything has changed the value since, the record no longer describes it
        and the entry is somebody else's, not ours to move.
    Together these mean the re-check can only ever move a value that this
    resolver itself synthesized and that nobody has touched since."""
    out = set()
    for ev, rec in (load_meta() or {}).items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("confidence")) != "guess":
            continue
        if str(rec.get("iso") or "") != str(file_data.get(ev) or ""):
            continue
        out.add(ev)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry", action="store_true", help="no write, no email")
    ap.add_argument("--set", nargs=2, metavar=("EVENT", "ISO8601"),
                    help="manually set one override and exit")
    args = ap.parse_args(argv)

    if args.set:
        ev, iso = args.set[0].strip(), args.set[1].strip()
        if parse_iso_utc(iso) is None:
            print(f"unparseable ISO datetime: {iso}")
            return 2
        data = load_file()
        data[ev] = iso
        write_file(data)
        # A --set SUPERSEDES whatever the resolver guessed. Recording it is what
        # stops the digest's unverified section from going on flagging a value a
        # human has already checked (CELH was --set to 07:00 the same morning).
        record_meta([(ev, iso, "hand-set by operator")], data)
        print(f"wrote {ev} = {iso} -> {EVENT_OVERRIDES_FILE} "
              f"(bot hot-reloads within a cycle)")
        return 0

    client = build_client()
    load_file_event_overrides()          # bring file entries into the dict
    file_data = load_file()

    # Phase 1: enroll newly-programmed series that fit the strategy families.
    enrolled, review = enroll_new_series(client, dry=args.dry)
    for s, why, sample in enrolled:
        log(f"ENROLLED {s} ({why}) e.g. {sample}")
    for s, why, sample in review:
        log(f"review: {s} ({why}) e.g. {sample}")

    # Phase 3: company-disclosure RELEASE-time cutoffs. Parity with the call
    # check — scrape the settlement-source pages for the report date + after-
    # close/before-open timing, write the release override, flag the rest.
    now = datetime.now(timezone.utc)
    disclosure = discover_company_disclosure(client, now)
    rel_resolved, rel_unresolved = [], []
    for ev, occ_iso in disclosure:
        occ = parse_iso_utc(occ_iso or "")
        # Anchor for a year-less page date. Was `occ.year if occ else now.year`,
        # which lands a year LATE when the page date and the anchor fall either
        # side of Dec/Jan (a Dec 30 report read against a Jan occurrence).
        release_ref = occ or parse_event_date(ev) or now
        found = None
        # (a) precise IR/press page, if Kalshi's settlement source is a real one
        for url in source_urls(client, ev):
            try:
                page = requests.get(url, headers=UA, timeout=15).text
            except Exception as e:
                log(f"! fetch failed {url}: {e}")
                continue
            hit = parse_release_time(page, ref=release_ref)
            if hit and ir_date_too_late(hit[0], release_ref):
                log(f"! IR release date {hit[0].isoformat()} for {ev} is more than "
                    f"{IR_LATE_GUARD_DAYS}d after the event date; not written "
                    f"(fail closed) [{url}]")
                hit = None
            if hit:
                found = (url, *hit)
                break
        # (b) fall back to the Nasdaq earnings calendar by ticker (overrides a
        # useless fiscal.ai-style settlement source with the real report date)
        if not found:
            ticker = COMPANY_TICKERS.get(ev.split("-")[0])
            if ticker:
                hit = nasdaq_release_datetime(ticker, now, DISCLOSURE_LEAD_DAYS + 3)
                if hit:
                    dt_et, label = hit
                    found = (f"nasdaq:{ticker}", dt_et, label,
                             f"Nasdaq earnings calendar ({ticker})")
        if found:
            url, dt_et, label, evidence = found
            iso = dt_et.isoformat()
            file_data[ev] = iso
            rel_resolved.append((ev, iso, label, url, evidence))
            log(f"RELEASE resolved {ev} = {iso} [{label}]  [{url}]")
        else:
            rel_unresolved.append((ev, occ_iso))
            log(f"RELEASE UNRESOLVED: {ev} (kalshi occ={occ_iso})")

    # Phase 2: earnings-CALL cutoffs. Jack 2026-07-23: set the call cutoff to
    # the earnings RELEASE time (Nasdaq-resolved), not the call itself. Safe by
    # construction — the call is always at/after the release, so being out
    # before the release is out before the call — and actually BETTER, because
    # the release can pre-move the mention market (a "10k layoffs" release makes
    # the layoffs-mention market gap before the call). Also makes calls fully
    # automated: the exact call time has no reliable machine source (Kalshi
    # points these series at bloomberg.com, not the IR page), but Nasdaq gives
    # the release. Since 2026-09-27 the company's own announcement is read
    # FIRST (announced_release) and merged with Nasdaq, earlier wins; then the
    # IR call-time scrape, then --set on a miss.
    events = discover_events(client)
    log(f"active earnings events: {len(events)}")

    resolved, unresolved, covered = [], [], []
    conflicts = []           # (event, announced_release tuple) for the ACTION email
    # PROVISIONAL RE-CHECK (Jack 2026-08-06, CELH). A fail-safe 7am guess is an
    # ADMISSION that nobody knows the time, so it is the one class of override
    # that must not be frozen by the `covered` short-circuit below. Everything
    # else keeps the old write-once behaviour.
    provisional = provisional_events(file_data)
    upgraded, provisional_open = [], []
    for ev in events:
        if ev in EVENT_START_OVERRIDES and ev not in provisional:
            covered.append(ev)
            continue
        series = ev.split("-")[0]
        tkr = (series[len(_EARNINGS_PREFIX):]
               if series.startswith(_EARNINGS_PREFIX) else "")
        rel = (nasdaq_release_datetime(tkr, now, DISCLOSURE_LEAD_DAYS + 3)
               if tkr else None)
        # The company's own notice first (see announced_release): it dates
        # what Nasdaq cannot and carries the real release time.
        ann = announced_release(client, ev, now, rel)
        if ann and ann[3]:
            conflicts.append((ev, ann))
        if ev in provisional:
            # ONLY a measured flag may replace a fail-safe guess. Anything else
            # (Nasdaq still has no time, or has dropped the row) leaves the
            # early cutoff exactly where it is — a re-check may push the bot's
            # stand-down LATER only on evidence, never on a second guess. The
            # company's own announcement is such evidence.
            if ann:
                was = file_data.get(ev)
                iso = ann[0].isoformat()
                file_data[ev] = iso
                upgraded.append((ev, was, iso, ann[1]))
                resolved.append((ev, iso, ann[2],
                                 f"fail-safe guess {was} REPLACED by the "
                                 f"company's announcement [{ann[1]}]"))
                log(f"PROVISIONAL UPGRADED {ev}: {was} -> {iso}  [{ann[1]}]  "
                    f"{ann[2]}")
            elif rel and provenance_of(rel[1]) == "read":
                was = file_data.get(ev)
                iso = rel[0].isoformat()
                file_data[ev] = iso
                upgraded.append((ev, was, iso, rel[1]))
                resolved.append((ev, iso, f"nasdaq:{tkr}",
                                 f"fail-safe guess {was} REPLACED by a measured "
                                 f"Nasdaq flag [{rel[1]}]"))
                log(f"PROVISIONAL UPGRADED {ev}: {was} -> {iso}  [{rel[1]}]")
            else:
                provisional_open.append((ev, file_data.get(ev)))
                # Logged on EVERY run, deliberately. The worst detail of the
                # CELH post-mortem is not that the guess was wrong, it is that
                # thirteen consecutive runs looked straight at it and said
                # nothing, because `covered` is emailed and never logged.
                log(f"provisional (still unmeasured) {ev} = {file_data.get(ev)}"
                    f"  [Nasdaq has no time flag; fail-safe cutoff stands]")
            continue
        if ann:
            iso = ann[0].isoformat()
            file_data[ev] = iso
            resolved.append((ev, iso, ann[2],
                             f"call cutoff = earnings RELEASE per the company's "
                             f"announcement [{ann[1]}]"))
            log(f"call {ev} = {iso}  (company announcement [{ann[1]}]  {ann[2]})")
            continue
        if rel:
            dt_et, label = rel
            iso = dt_et.isoformat()
            file_data[ev] = iso
            resolved.append((ev, iso, f"nasdaq:{tkr}",
                             f"call cutoff = earnings RELEASE [{label}] "
                             f"(safe: call is at/after the release)"))
            log(f"call {ev} = {iso}  (release proxy [{label}])")
            # A brand-new guess belongs in the unverified section of TODAY's
            # email, not only in tomorrow's digest: the morning it is written is
            # the cheapest moment for a human to look the call time up.
            if provenance_of(label) == "guess":
                provisional_open.append((ev, iso))
            continue
        # Nasdaq had nothing -> try the IR page for the exact call time, else flag
        found = None
        for url in source_urls(client, ev):
            try:
                page = requests.get(url, headers=UA, timeout=15).text
            except Exception as e:
                log(f"! fetch failed {url}: {e}")
                continue
            call_ref = parse_event_date(ev) or now
            hit = parse_call_time(page, ref=call_ref)
            if hit and ir_date_too_late(hit[0], call_ref):
                log(f"! IR call date {hit[0].isoformat()} for {ev} is more than "
                    f"{IR_LATE_GUARD_DAYS}d after the event date; not written "
                    f"(fail closed) [{url}]")
                hit = None
            if hit:
                found = (url, *hit)
                break
        if found:
            url, dt_et, evidence = found
            iso = dt_et.isoformat()
            file_data[ev] = iso
            resolved.append((ev, iso, url, evidence))
            log(f"resolved {ev} = {iso}  [{url}]")
        else:
            unresolved.append(ev)
            log(f"UNRESOLVED: {ev}")

    # Phase 2b: STALE-TICKER TRAP autofix. These are already dead to the bot
    # (cutoff in the past) and will never self-heal, so they get a longer
    # Nasdaq horizon than the normal path and are reported with their cost.
    stale_fixed, stale_open = [], []
    for ev, n_mkts, dpd, close in discover_stale_ticker_events(client, now):
        if ev in file_data:
            continue
        series = ev.split("-")[0]
        tkr = (series[len(_EARNINGS_PREFIX):]
               if series.startswith(_EARNINGS_PREFIX) else "")
        rel = (nasdaq_release_datetime(tkr, now, STALE_LOOKAHEAD_DAYS)
               if tkr else None)
        ann = announced_release(client, ev, now, rel)
        if ann and ann[3]:
            conflicts.append((ev, ann))
        if ann:
            iso = ann[0].isoformat()
            file_data[ev] = iso
            stale_fixed.append((ev, iso, n_mkts, dpd, f"{ann[2]} [{ann[1]}]"))
            log(f"STALE-TICKER FIXED {ev} = {iso}  ({n_mkts} mkts, "
                f"${dpd:,.0f}/day pool)  [{ann[1]}]  {ann[2]}")
        elif rel:
            dt_et, label = rel
            iso = dt_et.isoformat()
            file_data[ev] = iso
            stale_fixed.append((ev, iso, n_mkts, dpd, f"nasdaq:{tkr} [{label}]"))
            log(f"STALE-TICKER FIXED {ev} = {iso}  ({n_mkts} mkts, "
                f"${dpd:,.0f}/day pool)  [nasdaq {tkr} {label}]")
        else:
            stale_open.append((ev, n_mkts, dpd, close))
            log(f"STALE-TICKER UNRESOLVED {ev}: {n_mkts} mkts, ${dpd:,.0f}/day "
                f"pool earning $0 (ticker date passed, market open until "
                f"{close:%Y-%m-%d})")

    # Phase 4: scheduled-broadcast mention events the bot cannot window.
    bc_resolved, bc_unresolved = [], []
    # RNC -> WH upgrade (2026-10-06): an RNC rally page gives the EVENT's start
    # (Grand Island: 4:30pm local), the WH schedule -- once it lists the rally,
    # usually the day of -- Trump's remarks (6:00pm local), the time a mention
    # market turns on. A start the RNC page set (and nothing has changed since)
    # gives way to a WH match; hand-set values are never touched.
    meta = load_meta()
    for ev, iso in sorted(file_data.items()):
        m_ev = meta.get(ev) or {}
        if not ev.startswith(WH_SCHEDULE_SERIES) \
                or "[RNC events]" not in str(m_ev.get("label") or "") \
                or m_ev.get("iso") != iso:
            continue
        d = parse_event_date(ev)
        if d is None or d.astimezone(ET).date() < now.astimezone(ET).date():
            continue
        try:
            _e = ((client.get_event(ev) or {}).get("event") or {})
        except Exception as e:
            log(f"! event fetch failed {ev}: {e}")
            continue
        wh = wh_schedule_start(_e.get("title") or "", d.astimezone(ET).date(),
                               _e.get("sub_title") or "")
        if wh and wh[0].isoformat() != iso:
            new = wh[0].isoformat()
            file_data[ev] = new
            bc_resolved.append((ev, new, "WH schedule",
                                f"{wh[1][:60]} (was {iso} from the RNC page)"))
            log(f"broadcast {ev} = {new}  [WH schedule over RNC {iso}]  {wh[1][:70]}")
    for ev, series in discover_broadcast_mention_events(client, now):
        if ev in EVENT_START_OVERRIDES or ev in file_data:
            continue
        iso, src, detail, title = auto_broadcast_start(client, ev, series)
        if iso:
            file_data[ev] = iso
            bc_resolved.append((ev, iso, src, detail))
            log(f"broadcast {ev} = {iso}  [{src}]  {detail[:70]}")
        else:
            bc_unresolved.append((ev, title))
            log(f"BROADCAST UNRESOLVED: {ev}  {title[:90]}")

    if not args.dry and (resolved or rel_resolved or bc_resolved or stale_fixed):
        write_file(file_data)
        record_meta(provenance_batch(resolved, rel_resolved, stale_fixed,
                                     bc_resolved), file_data)

    # Split the run's findings into ACTION (needs Jack; the only thing that
    # can trigger this task's own email) and INFO (auto-handled; carried to
    # the 7:20 combined "IMM quotes and overrides" email via the run-summary
    # file, never emailed alone).
    act = []
    inf = []
    if stale_open:
        lost = sum(d for _e, _n, d, _c in stale_open)
        act.append(f"!! STALE-TICKER TRAP — {len(stale_open)} event(s) "
                   f"earning $0 on ${lost:,.0f}/day of live pool. Kalshi's "
                   f"ticker date has passed but the market is still open, "
                   f"so the bot's cutoff sits in the PAST and it will "
                   f"NEVER quote these without an override:")
        for ev, n, dpd, close in stale_open:
            act.append(f"  {ev}  —  {n} markets, ${dpd:,.0f}/day pool, "
                       f"open until {close:%Y-%m-%d}")
            act.append(f'  python imm_earnings_overrides.py --set {ev} '
                       f'"YYYY-MM-DDTHH:MM:00-04:00"   # the REAL call time')
        act.append("")
    if bc_unresolved:
        act.append("BROADCAST mention events UNRESOLVED — quoting GATED "
                   "(no pads; whole event stands down on thin depth) until "
                   "--set gives the air time:")
        for ev, title in bc_unresolved:
            d = parse_event_date(ev)
            hint = (d.astimezone(ET).strftime("%Y-%m-%d")
                    if d else now.astimezone(ET).strftime("%Y-%m-%d"))
            act.append(f"    # \"{title[:110]}\"")
            act.append(f'  python imm_earnings_overrides.py --set {ev} '
                       f'"{hint}T20:00:00-04:00"   # VERIFY air time')
        act.append("")
    if stale_fixed:
        for ev, iso, n, dpd, src in stale_fixed:
            inf.append(f"stale-ticker auto-fixed: {ev} = {iso}  "
                       f"({n} mkts, ${dpd:,.0f}/day) [{src}]")
    if upgraded:
        for ev, was, iso, label in upgraded:
            inf.append(f"7am guess -> measured: {ev} = {iso}  [{label}]")
    if provisional_open:
        # Safe by construction (the bot stands down at 06:50 ET) and
        # auto-upgrades when Nasdaq publishes a time — info only, now
        # structurally unable to trigger an email.
        for ev, iso in provisional_open:
            inf.append(f"on 7am fail-safe guess (safe, auto-upgrades): {ev}")
    if bc_resolved:
        for ev, iso, net, title in bc_resolved:
            inf.append(f"broadcast resolved: {ev} = {iso}  [{net}]")
    if rel_resolved:
        for ev, iso, label, url, evidence in rel_resolved:
            inf.append(f"release resolved: {ev} = {iso}  [{label}]")
    if rel_unresolved:
        act.append("RELEASE cutoffs UNRESOLVED — verify the earnings press-"
                   "release datetime and run (after-close ~4pm ET, BMO "
                   "~before open). The template below is the SAFE default, "
                   "not a guess at the answer:")
        for ev, occ in rel_unresolved:
            hint = (parse_iso_utc(occ or "") or now).strftime("%Y-%m-%d")
            # 07:00, not 16:00. Jack 2026-08-06: a paste-ready template IS a
            # default — pasted unedited it becomes the override. A 4pm
            # template hands the operator the exact value that had the bot
            # quoting through CELH's 8am call, and it is the one an
            # interrupted human is most likely to run without checking.
            # 7am only costs a day of accrual if it is wrong.
            act.append(f'  python imm_earnings_overrides.py --set {ev} '
                       f'"{hint}T07:00:00-04:00"   # kalshi occ={occ} '
                       f'VERIFY; 7am = stand-down default, edit if AMC')
        act.append("")
    if enrolled:
        for s, why, sample in enrolled:
            inf.append(f"series enrolled: {s}  [{why}]  e.g. {sample}")
    elec = [(s, sample) for s, why, sample in enrolled if why == ELECTION_ENROLL_WHY]
    if elec:
        act.append("ELECTION series enrolled by Kalshi category -- each "
                   "STANDS DOWN until it has a verified voting-day row in "
                   "incentive_mm._ELECTION_DATES_DEFAULT (verify the date "
                   "yourself; Kalshi's ticker dates are not trusted):")
        for s, sample in elec:
            act.append(f"  {s}  e.g. {sample}")
        act.append("")
    # `review` (unclassified/blocked series, 242 as of 8/15) is deliberately
    # NOT in the email at all (Jack 8/15): it is chronic, and any such series
    # with real money on it already shows in the quote-gaps table as a
    # "not in allowlist" row with its ROI. Log-only (see enroll_new_series).
    if resolved:
        for ev, iso, url, evidence in resolved:
            inf.append(f"call resolved: {ev} = {iso}")
    if conflicts:
        act.append("SOURCES DISAGREE on the date -- the EARLIER was written "
                   "(standing down early only forfeits accrual). Check the "
                   "company's IR page; --set if the later one is right:")
        for ev, ann in conflicts:
            act.append(f"  {ev}: wrote {ann[0]:%a %b %d %H:%M} ET "
                       f"[{ann[1]}]; {ann[3]}")
            if ann[2]:
                act.append(f"    {ann[2]}")
        act.append("")
    if unresolved:
        act.append("UNRESOLVED calls — no company announcement found (IR "
                   "event feed, press releases) and nothing on Nasdaq's "
                   "calendar within reach. Verify the call date AND time, "
                   "then --set. The Nasdaq line is the RELEASE (anchor only): "
                   "the call is usually the same day shortly after, but "
                   "split reporters (e.g. airlines) call the next morning "
                   "— so confirm the date, don't assume it:")
        for ev in unresolved:
            # What the bot is doing meanwhile: the undated-call fallback in
            # incentive_mm.trade_cutoff_utc (ticker date - lead days).
            cut = imm.trade_cutoff_utc(ev, None, None)
            if cut is not None:
                act.append(f"    # bot {'stood' if cut <= now else 'stands'} "
                           f"down {cut.astimezone(ET):%a %b %d} (ticker date - "
                           f"{imm.EARNINGS_UNDATED_LEAD_DAYS:g}d) until dated")
            series = ev.split("-")[0]
            tkr = (series[len(_EARNINGS_PREFIX):]
                   if series.startswith(_EARNINGS_PREFIX) else "")
            rel = (nasdaq_release_datetime(tkr, now, DISCLOSURE_LEAD_DAYS + 3)
                   if tkr else None)
            if rel and provenance_of(rel[1]) == "read":
                rel_et = rel[0].astimezone(ET)
                # context anchor only — NOT the --set value
                act.append(f"    # Nasdaq: {tkr} releases {rel_et:%a %b %d} "
                           f"[{rel[1]}] — call same-day shortly after OR "
                           f"next AM; VERIFY the date")
                # The template is the RELEASE time itself, not release+1h.
                # Jack 2026-08-06: the old version offered 5:00pm after a
                # 4pm release and 8:30am after a 7am one — i.e. an hour of
                # quoting after the print is already public, which is the
                # CELH failure in miniature. Phase 2 above deliberately
                # cuts off at the RELEASE ("safe: call is at/after the
                # release"); the hint a human pastes has to agree with it.
                hint_iso = rel_et.isoformat()
            else:
                # Nasdaq had nothing, or had only a guess. Unknown -> the
                # stand-down default, never 4:30pm (which is what this line
                # used to offer). Standing down early forfeits accrual;
                # standing down late is how the bot ends up making markets
                # into a print.
                d = parse_event_date(ev)
                hint_iso = ((d.strftime("%Y-%m-%d") if d else "2026-MM-DD")
                            + "T07:00:00-04:00")
            act.append(f'  python imm_earnings_overrides.py --set {ev} '
                       f'"{hint_iso}"')
        act.append(f"(an undated earnings event stops quoting "
                   f"{imm.EARNINGS_UNDATED_LEAD_DAYS:g} days before Kalshi's "
                   f"ticker date -- IMM_EARNINGS_UNDATED_LEAD_DAYS. Until "
                   f"2026-09-27 it quoted to the market's Dec 31 expiry.)")

    # Feed-audit fold (Jack 2026-08-22 "isnt there a daily sweeper? fold into
    # that"): NEW findings — unearnable series (the KXTEMPMIAH class), an
    # enrolled family's programs leaving the feed (the Aug-4 class), a new
    # paying family outside the allowlist (the KXAVGT class) — ride this
    # task's ACTION email; the one-line current state rides the run summary
    # into the 7:20 combined email. Deltas come from feed_audit_state.json,
    # so a standing finding pings once, not 3x/day. Never let an audit hiccup
    # sink the overrides run — the failure surfaces in the morning email.
    try:
        import imm_feed_audit
        fa_act, fa_inf = imm_feed_audit.delta_for_email(
            client=client, save_state=not args.dry)
    except Exception as e:               # noqa: BLE001 — task must not die
        fa_act, fa_inf = [], [f"feed-audit FAILED this run: {e}"]
        log(f"! feed audit failed: {e}")
    act += fa_act
    inf += fa_inf

    # PICK-OFF WINDOWS (Jack 2026-09-27): Kalshi's event start LATER than the
    # real event, so orders set to expire "at event start" are still resting
    # while it happens. The 7:10 digest and 7:20 email list every open window;
    # THIS run emails only NEW ones, because a window found at 12:45 for a 5pm
    # call is no use in tomorrow's digest. Kept out of action_lines: the 7:20
    # email renders its own live block and would otherwise print it twice.
    # Runs after this run's writes, so a fresh override is already its input.
    # Marked seen only once the email has gone (below): a failed send must
    # retry at the next run, not go quiet until tomorrow's digest.
    pick = imm_pickoff.scan(client, now, meta=load_meta(),
                            nasdaq_for_date=nasdaq_earnings_for_date)
    pick_new = imm_pickoff.new_rows(pick, now, PICKOFF_SEEN_FILE, save=False)
    pick_act = (imm_pickoff.text_lines({"rows": pick_new}, now) + [""]
                if pick_new else [])
    for r in pick.get("rows") or []:
        log(f"pick-off window: {r['event']} real {r['real'].isoformat()} "
            f"kalshi {r['kalshi_start'].isoformat()}"
            f"{'' if r in pick_new else ' (already emailed)'}")
    if pick.get("error"):
        inf.append(imm_pickoff.error_text(pick))

    tallies = (f"calls {len(resolved)}+/{len(unresolved)}?, "
               f"releases {len(rel_resolved)}+/{len(rel_unresolved)}?, "
               f"broadcast {len(bc_resolved)}+/{len(bc_unresolved)}?"
               + (f", {len(enrolled)} enrolled" if enrolled else "")
               + (f", {len(stale_fixed)} stale fixed" if stale_fixed else "")
               + (f", {len(stale_open)} STALE OPEN" if stale_open else "")
               + (f", audit {len(fa_act)} new" if fa_act else "")
               + (f", {len(conflicts)} DATE CONFLICT" if conflicts else "")
               + (f", {len(pick_new)} NEW PICK-OFF" if pick_new else ""))

    # Run summary for the 7:20 combined email (kept for the last 8 runs, so
    # info from the midday/afternoon runs still reaches the next morning).
    if not args.dry:
        runs = []
        try:
            if os.path.exists(SUMMARY_FILE):
                with open(SUMMARY_FILE, encoding="utf-8") as f:
                    runs = json.load(f)
                if not isinstance(runs, list):
                    runs = []
        except Exception:
            runs = []
        runs.append({"ts": now.isoformat(), "tallies": tallies,
                     "action_lines": act, "info": inf,
                     "covered": len(covered)})
        with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
            json.dump(runs[-8:], f, indent=1)

    # This task emails on its own ONLY when something needs Jack; routine
    # auto-handled runs surface in the morning combined email instead. A new
    # pick-off window leads the email AND the subject: it is the time-critical
    # item, and "calls 0+/0?, ..." would bury it at the end of the line.
    if not args.dry and (act or pick_act):
        lines = list(pick_act)
        if act:
            lines += ["Earnings call + release override run — ACTION NEEDED",
                      ""]
            lines += act
        if inf:
            lines.append("auto-handled this run:")
            lines += [f"  {s}" for s in inf]
            lines.append("")
        if covered:
            lines.append(f"already covered: {', '.join(covered)}")
        subject = f"IMM overrides ACTION: {tallies}"
        if pick_new:
            subject = "IMM {}: {}{}".format(
                imm_pickoff.HEADER,
                ", ".join(r["title"] or r["event"] for r in pick_new),
                f" + overrides ACTION ({tallies})" if act else "")
        alerter = Alerter("IMM-EARNINGS", live=True)
        if alerter.enabled:
            ok = alerter.send_message("\n".join(lines), subject=subject)
            log(f"action email: {'sent' if ok else 'FAILED'}")
            if ok and pick_new:
                imm_pickoff.new_rows(pick, now, PICKOFF_SEEN_FILE, save=True)
        else:
            log("alert credentials not configured; action summary not emailed")
            print("\n".join(lines))
    else:
        log(f"no action needed ({tallies}); {len(covered)} covered, "
            f"{len(provisional_open)} provisional, "
            f"{len(pick.get('rows') or [])} pick-off window(s) "
            f"({len(pick_new)} new), dry={args.dry}; "
            f"summary saved for the morning combined email")

    return 0


if __name__ == "__main__":
    sys.exit(main())
