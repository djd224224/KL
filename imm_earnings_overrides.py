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
from datetime import datetime, timezone

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
        return "enroll", "company/consumer metric shape"
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

    # Phase 3: company-disclosure events cut off at the earnings RELEASE, which
    # Kalshi's occurrence doesn't reliably give -> flag imminent ones for --set.
    now = datetime.now(timezone.utc)
    disclosure = discover_company_disclosure(client, now)
    for ev, occ in disclosure:
        log(f"disclosure NEEDS RELEASE cutoff: {ev} (kalshi occ={occ})")

    # Phase 2: earnings call-time overrides.
    events = discover_events(client)
    log(f"active earnings events: {len(events)}")

    resolved, unresolved, covered = [], [], []
    for ev in events:
        if ev in EVENT_START_OVERRIDES:
            covered.append(ev)
            continue
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

    if not args.dry and (resolved or True):
        if resolved:
            write_file(file_data)

    # email a summary whenever there is anything actionable
    if not args.dry and (resolved or unresolved or enrolled or review or disclosure):
        lines = ["Earnings call-time override run", ""]
        if disclosure:
            lines.append("COMPANY-DISCLOSURE events need a RELEASE-time cutoff "
                         "(numbers drop in the earnings PRESS RELEASE, not the "
                         "call). Verify each company's release datetime and run "
                         "(after-close reporters ~4pm ET, BMO ~before open):")
            for ev, occ in disclosure:
                d = parse_event_date(ev)
                # placeholder date = the event's own month token if parseable,
                # else today; user MUST verify (Kalshi occ is unreliable)
                hint = d.strftime("%Y-%m-%d") if d else \
                    (parse_iso_utc(occ or "") or now).strftime("%Y-%m-%d")
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
            lines.append("UNRESOLVED — verify the call time and run:")
            for ev in unresolved:
                d = parse_event_date(ev)
                hint = d.strftime("%Y-%m-%d") if d else "2026-MM-DD"
                lines.append(f'  python imm_earnings_overrides.py --set {ev} '
                             f'"{hint}T16:30:00-04:00"')
            lines.append("(unresolved events fall back to the conservative "
                         "midnight-ET rule and stop quoting the night before)")
        if covered:
            lines.append("")
            lines.append(f"already covered: {', '.join(covered)}")
        alerter = Alerter("IMM-EARNINGS", live=True)
        if alerter.enabled:
            ok = alerter.send_message(
                "\n".join(lines),
                subject=f"IMM earnings overrides: {len(resolved)} resolved, "
                        f"{len(unresolved)} need input")
            log(f"summary email: {'sent' if ok else 'FAILED'}")
        else:
            log("alert credentials not configured; summary not emailed")
            print("\n".join(lines))
    else:
        log(f"nothing to do: {len(covered)} covered, dry={args.dry}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
