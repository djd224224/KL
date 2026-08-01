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
hot-reloads each cycle — no restart); email a summary with paste-ready
`--set` commands for anything unresolved.

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
    occurrence (INTC occ Jul 25 but the real report is Jul 23)."""
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
            hour, label = 16, "time n/a->4pm ET (Nasdaq)"
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
    for ev in events:
        if ev in EVENT_START_OVERRIDES:
            covered.append(ev)
            continue
        series = ev.split("-")[0]
        tkr = (series[len(_EARNINGS_PREFIX):]
               if series.startswith(_EARNINGS_PREFIX) else "")
        rel = (nasdaq_release_datetime(tkr, now, DISCLOSURE_LEAD_DAYS + 3)
               if tkr else None)
        if rel:
            dt_et, label = rel
            iso = dt_et.isoformat()
            file_data[ev] = iso
            resolved.append((ev, iso, f"nasdaq:{tkr}",
                             f"call cutoff = earnings RELEASE [{label}] "
                             f"(safe: call is at/after the release)"))
            log(f"call {ev} = {iso}  (release proxy [{label}])")
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
        m = SHOW_TITLE_RE.search(title)
        d_et = parse_event_date(ev).astimezone(ET).date()
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

    if not args.dry and (resolved or rel_resolved or bc_resolved):
        write_file(file_data)

    # email a summary whenever there is anything actionable
    if not args.dry and (resolved or unresolved or enrolled or review
                         or rel_resolved or rel_unresolved
                         or bc_resolved or bc_unresolved):
        lines = ["Earnings call + release override run", ""]
        if bc_resolved:
            lines.append("BROADCAST mention cutoffs AUTO-RESOLVED (TVmaze; "
                         "written, bot hot-reloads):")
            for ev, iso, net, title in bc_resolved:
                lines.append(f"  {ev} = {iso}   [{net}]")
                lines.append(f"    \"{title}\"")
            lines.append("")
        if bc_unresolved:
            lines.append("BROADCAST mention events UNRESOLVED — the bot will "
                         "NOT quote these until --set (find the air time):")
            for ev, title in bc_unresolved:
                d = parse_event_date(ev)
                hint = (d.astimezone(ET).strftime("%Y-%m-%d")
                        if d else now.astimezone(ET).strftime("%Y-%m-%d"))
                lines.append(f"    # \"{title[:110]}\"")
                lines.append(f'  python imm_earnings_overrides.py --set {ev} '
                             f'"{hint}T20:00:00-04:00"   # VERIFY air time')
            lines.append("")
        if rel_resolved:
            lines.append("RELEASE cutoffs AUTO-RESOLVED (written; bot hot-reloads; "
                         "orders expire 10min before these):")
            for ev, iso, label, url, evidence in rel_resolved:
                lines.append(f"  {ev} = {iso}   [{label}]")
                lines.append(f"    source: {url}")
                lines.append(f"    \"{evidence[:150]}\"")
            lines.append("")
        if rel_unresolved:
            lines.append("RELEASE cutoffs UNRESOLVED — verify the earnings press-"
                         "release datetime and run (after-close ~4pm ET, BMO "
                         "~before open):")
            for ev, occ in rel_unresolved:
                hint = (parse_iso_utc(occ or "") or now).strftime("%Y-%m-%d")
                lines.append(f'  python imm_earnings_overrides.py --set {ev} '
                             f'"{hint}T16:00:00-04:00"   # kalshi occ={occ} VERIFY')
            lines.append("")
        if enrolled:
            lines.append("NEW SERIES AUTO-ENROLLED (bot hot-reloads; remove "
                         "from extra_allow_series.json to veto):")
            for s, why, sample in enrolled:
                lines.append(f"  {s}  [{why}]  e.g. {sample}")
            lines.append("")
        if review:
            lines.append("NEW SERIES NEEDING REVIEW (not enrolled):")
            for s, why, sample in review:
                lines.append(f"  {s}  [{why}]  e.g. {sample}")
            lines.append("")
        if resolved:
            lines.append("RESOLVED (written to overrides file; bot hot-reloads):")
            for ev, iso, url, evidence in resolved:
                lines.append(f"  {ev} = {iso}")
                lines.append(f"    source: {url}")
                lines.append(f"    \"{evidence[:160]}\"")
        if unresolved:
            lines.append("")
            lines.append("UNRESOLVED calls — verify the call date AND time, then "
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
                if rel:
                    rel_et = rel[0].astimezone(ET)
                    # context anchor only — NOT the --set value
                    lines.append(f"    # Nasdaq: {tkr} releases {rel_et:%a %b %d} "
                                 f"[{rel[1]}] — call same-day shortly after OR "
                                 f"next AM; VERIFY the date")
                    # runnable template: same-day call guess (5pm ET after a
                    # close release, 8:30am after a pre-market one), clearly to
                    # be edited if it's a split reporter
                    guess = (rel_et.replace(hour=8, minute=30)
                             if "before open" in rel[1]
                             else rel_et.replace(hour=17, minute=0))
                    hint_iso = guess.isoformat()
                else:
                    d = parse_event_date(ev)
                    hint_iso = ((d.strftime("%Y-%m-%d") if d else "2026-MM-DD")
                                + "T16:30:00-04:00")
                lines.append(f'  python imm_earnings_overrides.py --set {ev} '
                             f'"{hint_iso}"')
            lines.append("(unresolved events fall back to the conservative "
                         "midnight-ET rule and stop quoting the night before)")
        if covered:
            lines.append("")
            lines.append(f"already covered: {', '.join(covered)}")
        alerter = Alerter("IMM-EARNINGS", live=True)
        if alerter.enabled:
            ok = alerter.send_message(
                "\n".join(lines),
                subject=f"IMM overrides: calls {len(resolved)}+/{len(unresolved)}?, "
                        f"releases {len(rel_resolved)}+/{len(rel_unresolved)}?")
            log(f"summary email: {'sent' if ok else 'FAILED'}")
        else:
            log("alert credentials not configured; summary not emailed")
            print("\n".join(lines))
    else:
        log(f"nothing to do: {len(covered)} covered, dry={args.dry}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
