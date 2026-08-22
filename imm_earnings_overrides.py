#!/usr/bin/env python3
r"""imm_earnings_overrides.py — daily earnings CALL-TIME override maintainer.

WHY: KXEARNINGSMENTION markets resolve on what is said DURING the earnings
call, so incentive_mm's cutoff must be the CALL start — but no automated feed
gives call times (Kalshi ticker dates / occurrence and Nasdaq AMC/BMO all
track the RELEASE, which can be a different day). Without a per-event
override the bot falls back to midnight-ET-of-ticker-date and the event dies
the night before its call (bit GOOGL/TSLA/ALK overnight 7/21->7/22).

WHAT: for every ACTIVE KXEARNINGSMENTION event with no override yet,
best-effort scrape the market's own settlement-source / IR pages for a
"conference call ... <time> ET on <date>" pattern; write resolved times to
run-logs/incentive-mm/event_start_overrides.json (which incentive_mm
hot-reloads each cycle — no restart). Emails on its own ONLY when something
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
    except OSError:
        return ""


for _v in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD"):
    if not os.environ.get(_v):
        _val = _env_from_registry(_v)
        if _val:
            os.environ[_v] = _val

import incentive_mm as imm  # noqa: E402
from incentive_mm import (Alerter, ET, EVENT_OVERRIDES_FILE,  # noqa: E402
                          EVENT_START_OVERRIDES, EXTRA_ALLOW_FILE,
                          _EARNINGS_PREFIX, build_client,
                          load_extra_allow_series, load_file_event_overrides,
                          log, parse_event_date, parse_iso_utc)

# Per-run summary consumed by the 7:20 combined email (imm_quote_gaps.py).
SUMMARY_FILE = os.path.join(os.path.dirname(EVENT_OVERRIDES_FILE),
                            "overrides_last_runs.json")

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
DATE_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(\d{1,2})", re.I)
KEYWORD_RE = re.compile(r"conference call|webcast|earnings call", re.I)


def parse_call_time(page_text: str, year: int = 2026):
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
            dt_et = ET.localize(datetime(year, month, day, hour, minute))
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


def parse_release_time(page_text: str, year: int = 2026):
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
            dt_et = ET.localize(datetime(year, month, day, hour, minute))
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
        verdict, why = classify_series(s, sample)
        if verdict == "enroll":
            additions.append(s)
            enrolled.append((s, why, sample))
        elif verdict == "review":
            review.append((s, why, sample))
    if additions and not dry:
        try:
            with open(EXTRA_ALLOW_FILE, encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            data = {}
        cur = set(data.get("series") or [])
        cur.update(additions)
        os.makedirs(os.path.dirname(EXTRA_ALLOW_FILE), exist_ok=True)
        tmp = EXTRA_ALLOW_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"series": sorted(cur)}, f, indent=1)
        os.replace(tmp, EXTRA_ALLOW_FILE)
        log(f"enrolled {len(additions)} new series -> {EXTRA_ALLOW_FILE}")
    return enrolled, review


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
    "KXDDCOLDBREW", "KXBKNUGGETS", "KXAMSAVO"}
COMPANY_DISCLOSURE_SERIES = {
    s for s in imm._DEFAULT_COMPANY_SERIES.split(",")
    if s and s not in _CONSUMER_PRICE_SERIES}
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
# paste-ready --set for the rest. Same safe direction as everything here: no
# override -> the bot just keeps NOT quoting.
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


def wh_schedule_start(title: str, date_et):
    """(datetime ET, matched schedule details) from the Factbase WH calendar:
    the UNIQUE best keyword match on that date with >=2 shared content words
    and a concrete time, else None."""
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
    want = _wh_words(title)
    scored = []
    for it in _wh_cache["entries"]:
        if str(it.get("date")) != date_et.isoformat() or not it.get("time"):
            continue
        sc = len(want & _wh_words(str(it.get("details") or "")))
        if sc >= 2:
            scored.append((sc, it))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None                              # ambiguous match
    it = scored[0][1]
    try:
        hh, mm = str(it["time"]).split(":")[:2]
        dt = ET.localize(datetime(date_et.year, date_et.month, date_et.day,
                                  int(hh), int(mm)))
    except (ValueError, KeyError):
        return None
    return dt, str(it.get("details") or "")


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
        out.append((t[0], t[1], "broadcast schedule"))
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
        yr = occ.year if occ else now.year
        found = None
        # (a) precise IR/press page, if Kalshi's settlement source is a real one
        for url in source_urls(client, ev):
            try:
                page = requests.get(url, headers=UA, timeout=15).text
            except Exception as e:
                log(f"! fetch failed {url}: {e}")
                continue
            hit = parse_release_time(page, year=yr)
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
    # the release. Falls back to the IR call-time scrape then --set on a miss.
    events = discover_events(client)
    log(f"active earnings events: {len(events)}")

    resolved, unresolved, covered = [], [], []
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
        if ev in provisional:
            # ONLY a measured flag may replace a fail-safe guess. Anything else
            # (Nasdaq still has no time, or has dropped the row) leaves the
            # early cutoff exactly where it is — a re-check may push the bot's
            # stand-down LATER only on evidence, never on a second guess.
            if rel and provenance_of(rel[1]) == "read":
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
            hit = parse_call_time(page)
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
        if rel:
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
    for ev, series in discover_broadcast_mention_events(client, now):
        if ev in EVENT_START_OVERRIDES or ev in file_data:
            continue
        title = ""
        try:
            title = (((client.get_event(ev) or {}).get("event") or {})
                     .get("title")) or ""
        except Exception as e:
            log(f"! event fetch failed {ev}: {e}")
        d_et = parse_event_date(ev).astimezone(ET).date()
        # WH-schedule series (KXTRUMPMENTION*): try the Factbase calendar
        # before TVmaze — these events are appearances, not shows.
        if series.startswith(WH_SCHEDULE_SERIES):
            wh = wh_schedule_start(title, d_et)
            if wh:
                dt_et, det = wh
                iso = dt_et.isoformat()
                file_data[ev] = iso
                bc_resolved.append((ev, iso, "WH schedule",
                                    f"{title[:60]} => {det[:60]}"))
                log(f"broadcast {ev} = {iso}  [WH schedule]  {det[:70]}")
                continue
        m = SHOW_TITLE_RE.search(title)
        hit = tvmaze_airtime(m.group(1), d_et) if m else None
        if hit:
            dt_et, net = hit
            iso = dt_et.isoformat()
            file_data[ev] = iso
            bc_resolved.append((ev, iso, net, title[:90]))
            log(f"broadcast {ev} = {iso}  [tvmaze {net}]  {title[:70]}")
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
        act.append("BROADCAST mention events UNRESOLVED — the bot will "
                   "NOT quote these until --set (find the air time):")
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
    # `review` (unclassified/blocked series, 242 as of 8/15) is deliberately
    # NOT in the email at all (Jack 8/15): it is chronic, and any such series
    # with real money on it already shows in the quote-gaps table as a
    # "not in allowlist" row with its ROI. Log-only (see enroll_new_series).
    if resolved:
        for ev, iso, url, evidence in resolved:
            inf.append(f"call resolved: {ev} = {iso}")
    if unresolved:
        act.append("UNRESOLVED calls — verify the call date AND time, then "
                   "--set. The Nasdaq line is the RELEASE (anchor only): "
                   "the call is usually the same day shortly after, but "
                   "split reporters (e.g. airlines) call the next morning "
                   "— so confirm the date, don't assume it:")
        for ev in unresolved:
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
        act.append("(unresolved events fall back to the conservative "
                   "midnight-ET rule and stop quoting the night before)")

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

    tallies = (f"calls {len(resolved)}+/{len(unresolved)}?, "
               f"releases {len(rel_resolved)}+/{len(rel_unresolved)}?, "
               f"broadcast {len(bc_resolved)}+/{len(bc_unresolved)}?"
               + (f", {len(enrolled)} enrolled" if enrolled else "")
               + (f", {len(stale_fixed)} stale fixed" if stale_fixed else "")
               + (f", {len(stale_open)} STALE OPEN" if stale_open else "")
               + (f", audit {len(fa_act)} new" if fa_act else ""))

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
    # auto-handled runs surface in the morning combined email instead.
    if not args.dry and act:
        lines = ["Earnings call + release override run — ACTION NEEDED", ""]
        lines += act
        if inf:
            lines.append("auto-handled this run:")
            lines += [f"  {s}" for s in inf]
            lines.append("")
        if covered:
            lines.append(f"already covered: {', '.join(covered)}")
        alerter = Alerter("IMM-EARNINGS", live=True)
        if alerter.enabled:
            ok = alerter.send_message("\n".join(lines),
                                      subject=f"IMM overrides ACTION: {tallies}")
            log(f"action email: {'sent' if ok else 'FAILED'}")
        else:
            log("alert credentials not configured; action summary not emailed")
            print("\n".join(lines))
    else:
        log(f"no action needed ({tallies}); {len(covered)} covered, "
            f"{len(provisional_open)} provisional, dry={args.dry}; "
            f"summary saved for the morning combined email")

    return 0


if __name__ == "__main__":
    sys.exit(main())
