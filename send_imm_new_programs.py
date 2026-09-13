#!/usr/bin/env python3
r"""send_imm_new_programs.py — the daily "new incentive programs" email
(Jack 2026-09-13: "new daily morning email, table of all the new incentive
rewards events that started in the past day").

WHAT IT IS: one table, one row per EVENT whose liquidity-incentive programs
STARTED inside the window (normally the 24h since yesterday's email) —
what the event is in plain English, how many markets carry programs, the
pool in $/day and in total-period $, the program window in ET, the target
depth, and what the bot is doing about it (quoting / allowed / blocked /
not allowlisted, plus a cutoff flag when the bot provably cannot earn the
pool). Sorted biggest pool first, TOTAL row across EVERY new event (not
just the rows shown).

WHY IT EXISTS: reward supply is the bot's raw material and it moves before
P&L does, but nothing in the morning lineup reports the SUPPLY SIDE. The
7:10 digest is the bot's own book, the 7:20 quote-gaps email ranks markets
that already pay and are not quoted, and the 6:45 sweeper only pings a new
family once it crosses $300/day (IMM_AUDIT_NEW_FAMILY_MIN_DPD). Every
expensive surprise in this bot's history was a supply event seen late or
not at all: Kalshi lit 11 KXAVGT stations at ~$2,600/day on 2026-08-20 and
nothing surfaced them; KXTEMPMIAH relaunched 2026-08-15 as a Miami-only
hourly program and the bot sat dark on its top reward family for 16 days.
This email is the standing daily record of what the exchange newly put on
the table, whether or not the bot is allowed to touch it.

HOW "NEW" IS DECIDED — two independent conditions, both required:
  1. the event is NOT in this script's own seen-file
     (run-logs/incentive-mm/imm_new_programs_seen.json, one entry per event
     ever reported or recorded), and
  2. the EARLIEST program start across the event's markets is at or after
     the window start.
Condition 1 alone would flood the first run with the whole live feed;
condition 2 alone would re-report an old event the day a second program
period opens on it after the first one ended (the roll case incentive_mm's
fetch_programs is careful about). Together they mean "this event was not
paying before, and its pool switched on inside the window".
The window start is the LAST SUCCESSFUL SEND (the seen-file's watermark),
not a fixed 24h, so a day the task does not run or the send fails is picked
up by the next email instead of falling in a hole — capped at
IMM_NEWPROG_MAX_LOOKBACK_HOURS (168h) so a long outage cannot produce a
thousand-row email. With no watermark on record the window is the plain
IMM_NEWPROG_LOOKBACK_HOURS (24h).

An event first seen this run whose programs started BEFORE the window
(a backfilled or long-running program that only now entered the feed, and
every event alive on the very first run) is NOT a new event: those are
recorded in the seen-file, counted in the footer, and — once the file has
been seeded — listed in a short LATE ARRIVALS table so a quietly backdated
program is still visible.

COVERAGE BOUNDARY, stated because it decides what this email can and
cannot catch: the feed read is `status=active`, so a program that both
started AND ended between two runs is invisible to it. Hourly families are
NOT in that hole (an hourly series always has a current-hour program
active, published ~hh:11), but a one-off sub-24h program that expired
before the email would be missed. `--include-ended` additionally pages the
non-active statuses for the same window; it is off by default because the
size and ordering of the historical feed (~76k rows) is not something a
7:30 AM email should discover for the first time.

SOURCES AND SAFETY: the program feed is Kalshi's
GET /trade-api/v2/incentive_programs; `period_reward` is CENTI-CENTS
(1,000,000 = $100), converted exactly as incentive_mm.fetch_programs does.
Allowlist verdicts come from the bot's own IncentiveMarketMaker._allowed /
_blocked with the extra-allow and finecon extension files hot-loaded, and
"quoting" is selected_tickers from the bot's persisted imm_state.json.
Config parity with the live bot comes from importing imm_quote_gaps FIRST,
which mirrors run_incentive_mm.ps1's $ProbeEnv into the environment before
incentive_mm's module-level config is read (it logs "[GAPS] mirrored N
launcher env vars"; if that says 0, the allowlist columns ran on defaults).
STRICTLY READ-ONLY against the live system: GETs only, no cycle, no
orders, and the only file written is this script's own seen-file.

USAGE:
  python send_imm_new_programs.py              # the scheduled daily run
  python send_imm_new_programs.py --test       # send now, ignore the marker
  python send_imm_new_programs.py --dry        # build + print, send nothing
  python send_imm_new_programs.py --since 72   # widen the window by hand
Scheduled daily 7:30 AM ET ("KL imm new-programs"), after the 7:10 digest,
7:20 quote-gaps and 7:25 opportunistic emails. Idempotent via a daily
sent-marker; Modern-Standby retries (8x5min); credentials from
ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD with the HKCU registry fallback.
"""

import argparse
import glob
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape as _esc

# Import imm_quote_gaps BEFORE incentive_mm: it applies the live launcher's
# $ProbeEnv and the ALERT_EMAIL_* registry fallback at import time, which
# incentive_mm's config reads depend on (same ordering send_opportunistic_imm
# gets via send_imm_digest). It also carries describe_event()/event_title(),
# so an event reads identically here and in the 7:20 email.
import imm_quote_gaps as gaps
import incentive_mm as imm
from incentive_mm import log

SEEN_PATH = os.path.join(imm.STATUS_DIR, "imm_new_programs_seen.json")
STATE_PATH = os.path.join(imm.STATUS_DIR, "imm_state.json")
SEEN_VERSION = 1

# Window: normally "since the last email" (the seen-file watermark). The cap
# bounds a catch-up after an outage; the default applies only when there is
# no watermark at all.
LOOKBACK_HOURS = float(os.environ.get("IMM_NEWPROG_LOOKBACK_HOURS", "24"))
MAX_LOOKBACK_HOURS = float(os.environ.get("IMM_NEWPROG_MAX_LOOKBACK_HOURS", "168"))
# Row caps: the TOTAL row and the headline always cover every new event; only
# the printed rows are capped, and the overflow is stated with its pool.
MAX_ROWS = int(os.environ.get("IMM_NEWPROG_MAX_ROWS", "75"))
MAX_LATE_ROWS = int(os.environ.get("IMM_NEWPROG_MAX_LATE_ROWS", "10"))
# A series absent from the feed this long and back again is a RELIT family —
# the 2026-08-15 KXTEMPMIAH class, worth flagging as loudly as a new one.
RELIT_DAYS = float(os.environ.get("IMM_NEWPROG_RELIT_DAYS", "7"))
# Seen-file retention. Must comfortably exceed the longest gap a legitimate
# recurring event leaves between listings, or a normal weekly would re-report
# as new; 120 days is ~4x the longest program window observed (Kalshi's
# quarterly earnings-mention relistings are new EVENT tickers anyway).
KEEP_DAYS = float(os.environ.get("IMM_NEWPROG_KEEP_DAYS", "120"))
# A feed that collapses is an API problem, not a supply event: below this
# fraction of the last run's program count the watermark is NOT advanced
# (so nothing is skipped once the feed returns) and the email says so.
FEED_SHRINK_FRAC = float(os.environ.get("IMM_NEWPROG_FEED_SHRINK", "0.25"))
PAGE_LIMIT = int(os.environ.get("IMM_NEWPROG_PAGE_LIMIT", "1000"))
MAX_PAGES = int(os.environ.get("IMM_NEWPROG_MAX_PAGES", "40"))
# One GET per event for Kalshi's own title; biggest pools first, bounded.
TITLE_BUDGET = int(os.environ.get("IMM_NEWPROG_TITLES", "60"))
ENDED_STATUSES = tuple(s for s in os.environ.get(
    "IMM_NEWPROG_ENDED_STATUSES", "settled,closed").split(",") if s)

TD = 'padding:5px 10px;border:1px solid #ddd;text-align:right;'
TDL = 'padding:5px 10px;border:1px solid #ddd;text-align:left;'


# ----------------------------------------------------------------------------
# Feed
# ----------------------------------------------------------------------------

def _dollars(period_reward) -> float:
    """Kalshi's period_reward is CENTI-CENTS (1,000,000 = $100) — the same
    /10000 incentive_mm.fetch_programs uses. Keep in sync."""
    try:
        return float(period_reward or 0) / 10000.0
    except (TypeError, ValueError):
        return 0.0


def _program_days(start: datetime, end: datetime) -> float:
    """Program length in days, floored at one hour so an hourly program's
    $/day is a rate and not a division blow-up (mirrors fetch_programs)."""
    return max((end - start).total_seconds() / 86400.0, 1.0 / 24)


def fetch_programs_raw(client, statuses=("active",)) -> list:
    """Every program row, UNAGGREGATED — this email needs per-program start
    dates, which incentive_mm.fetch_programs() folds away into one union
    window per market.

    Deduped across statuses by (id, market, window) so an --include-ended
    run cannot double-count a row that appears under two filters.

    A read failure on the FIRST status (the live feed this email is built on)
    propagates — a half-paged feed emailed as the truth is worse than a late
    email, and the caller's retry loop exists for exactly this. The opt-in
    extra statuses are best-effort: one of them erroring (an endpoint that
    does not take that filter) must not cost the whole email."""
    rows, seen_keys = [], set()
    for i, status in enumerate(statuses):
        cursor, pages = None, 0
        while pages < MAX_PAGES:
            pages += 1
            params = {"limit": PAGE_LIMIT}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            try:
                resp = client.get("/incentive_programs", params=params) or {}
            except Exception as e:
                if i == 0:
                    raise
                log(f"! program read failed on status={status!r} ({e!r}); "
                    f"continuing without it")
                break
            batch = resp.get("incentive_programs") or []
            for p in batch:
                key = (str(p.get("id") or ""), str(p.get("market_ticker") or ""),
                       str(p.get("start_date") or ""), str(p.get("end_date") or ""))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                rows.append(p)
            cursor = resp.get("next_cursor")
            if not cursor or not batch:
                break
        else:
            log(f"! page budget ({MAX_PAGES}) hit on status={status!r} — that "
                f"slice of the feed is truncated")
    return rows


def roll_up_events(rows, now: datetime):
    """Program rows -> {event_ticker: record}, plus a Counter of the
    incentive_type values skipped (anything but `liquidity`: volume programs
    are taker-side and not this bot's business, but a non-zero count is a
    venue change worth a footnote)."""
    events, skipped = {}, Counter()
    for p in rows:
        if p.get("incentive_type") != "liquidity":
            skipped[str(p.get("incentive_type"))] += 1
            continue
        ticker = p.get("market_ticker") or ""
        start = imm.parse_iso_utc(p.get("start_date", ""))
        end = imm.parse_iso_utc(p.get("end_date", ""))
        if not ticker or not start or not end or end <= start:
            continue
        ev = gaps._event_of(ticker)
        rec = events.get(ev)
        if rec is None:
            rec = events[ev] = {
                "event": ev, "series": imm.series_of(ticker), "tickers": set(),
                "live_tickers": set(), "pool_day": 0.0, "pool_total": 0.0,
                "start": start, "end": end, "target": 0.0, "dfs": set(),
                "n_programs": 0,
            }
        rec["tickers"].add(ticker)
        rec["n_programs"] += 1
        dollars = _dollars(p.get("period_reward"))
        rec["pool_total"] += dollars
        rec["pool_day"] += dollars / _program_days(start, end)
        rec["start"] = min(rec["start"], start)
        rec["end"] = max(rec["end"], end)
        try:
            rec["target"] = max(rec["target"], float(p.get("target_size_fp") or 0))
        except (TypeError, ValueError):
            pass
        rec["dfs"].add((p.get("discount_factor_bps") or 5000) / 10000.0)
        if start <= now < end and not p.get("paid_out"):
            rec["live_tickers"].add(ticker)
    return events, skipped


# ----------------------------------------------------------------------------
# Seen-file: what this email has already reported / recorded
# ----------------------------------------------------------------------------

def _entry(rec) -> dict:
    """One seen-file entry, defensively: a hand-edited or half-written file
    must degrade to "never seen", never raise inside the morning email."""
    return rec if isinstance(rec, dict) else {}


def load_seen() -> dict:
    try:
        with open(SEEN_PATH, encoding="utf-8") as f:
            rec = json.load(f)
        if isinstance(rec, dict):
            for key in ("events", "series"):
                if not isinstance(rec.get(key), dict):
                    rec[key] = {}
            return rec
    except (OSError, ValueError) as e:
        log(f"! seen-file unreadable ({e!r}) — treating this as a first run")
    return {"version": SEEN_VERSION, "events": {}, "series": {}}


def save_seen(rec: dict) -> None:
    """Atomic, best-effort. A failed write costs a day of duplicate rows, not
    correctness, so it must never fail the send that already happened."""
    try:
        os.makedirs(imm.STATUS_DIR, exist_ok=True)
        tmp = SEEN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, SEEN_PATH)
    except OSError as e:
        log(f"! seen-file not written ({e!r}) — tomorrow's email may repeat "
            f"today's rows")


def window_start_for(seen: dict, now: datetime, since_hours=None):
    """(window_start, how_it_was_chosen). See the module docstring."""
    if since_hours is not None:
        return now - timedelta(hours=since_hours), f"--since {since_hours:g}h"
    watermark = imm.parse_iso_utc(seen.get("watermark") or "")
    floor = now - timedelta(hours=MAX_LOOKBACK_HOURS)
    if watermark is None:
        return (now - timedelta(hours=LOOKBACK_HOURS),
                f"no previous email on record - {LOOKBACK_HOURS:g}h default")
    if watermark < floor:
        return floor, (f"last email {_et(watermark)} ET - window capped at "
                       f"{MAX_LOOKBACK_HOURS:g}h")
    if watermark > now:
        return now - timedelta(hours=LOOKBACK_HOURS), "watermark in the future"
    return watermark, "since the last email"


def next_seen(seen: dict, events: dict, now: datetime, feed_ok: bool) -> dict:
    """The seen-file to write after a successful send: every event in this
    run's feed recorded, stale entries pruned, the supply snapshot updated.

    The watermark only advances on a HEALTHY feed — on a collapsed one the
    old watermark rides so the next email still covers this run's window."""
    held = imm.parse_iso_utc(seen.get("watermark") or "") or now
    out = {"version": SEEN_VERSION,
           "watermark": (now if feed_ok else held).isoformat(),
           "last_send": now.isoformat(),
           "feed_programs": sum(r["n_programs"] for r in events.values()),
           "feed_events": len(events),
           "feed_pool_day": round(sum(r["pool_day"] for r in events.values()), 2),
           "feed_ok": feed_ok,
           "events": {}, "series": {}}
    now_iso = now.isoformat()
    cutoff = now - timedelta(days=KEEP_DAYS)
    # Prune anything not seen in KEEP_DAYS: the file carries one entry per
    # event the feed has ever shown, so without a horizon it grows forever.
    for key in ("events", "series"):
        for name, rec in (seen.get(key) or {}).items():
            last = imm.parse_iso_utc(_entry(rec).get("last_seen") or "")
            if last is not None and last >= cutoff:
                out[key][name] = rec
    # Two timestamps per entry and nothing else: the feed shows thousands of
    # events a day and this file keeps four months of them, so every extra
    # field is megabytes. first_seen answers "is this new", last_seen answers
    # "how long was this series dark".
    for ev, rec in events.items():
        prev = _entry(out["events"].get(ev))
        out["events"][ev] = {"first_seen": prev.get("first_seen") or now_iso,
                             "last_seen": now_iso}
        sprev = _entry(out["series"].get(rec["series"]))
        out["series"][rec["series"]] = {
            "first_seen": sprev.get("first_seen") or now_iso,
            "last_seen": now_iso}
    return out


# ----------------------------------------------------------------------------
# What the bot is doing about each new event
# ----------------------------------------------------------------------------

def load_json(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def cutoff_flag(rec: dict, resolver, now: datetime) -> str:
    """"" | "cutoff passed" | "UNEARNABLE" — whether the bot's own stand-down
    cutoff already kills this brand-new pool.

    Mirror of imm_feed_audit.run_audit's guards (keep in sync): a
    close-anchored series (hourly weather / AQI overrides) is governed by
    close_time and is not auditable from the program feed, and a mention
    event's real cutoff comes from its expiration, not its ticker date. Only
    the midnight-ET FALLBACK cutoff can make a program UNEARNABLE — a
    RESOLVED cutoff that precedes a program period is a deliberate
    stand-down (the earnings call-time rule), not a miss."""
    series = rec["series"]
    override = imm.series_override(series)
    if override is not None and override.cutoff_from_close_min is not None:
        return ""
    if any(series.endswith(suf) for suf in imm.ALLOW_SERIES_SUFFIXES):
        return ""
    try:
        resolved = resolver.resolve(series, rec["event"])
    except Exception:
        resolved = None
    ticker_date = imm.parse_event_date(rec["event"])
    cutoff = (resolved - timedelta(minutes=imm.EVENT_START_BUFFER_MIN)
              if resolved is not None else ticker_date)
    if cutoff is None:
        return ""
    if resolved is None and rec["start"] >= cutoff:
        return "UNEARNABLE"
    if cutoff <= now:
        return "cutoff passed"
    return ""


def bot_status(rec: dict, quoted: set, resolver, now: datetime) -> str:
    """One cell: ALLOWLIST policy plus whether the bot is already quoting the
    event, and the cutoff flag when its pool is provably unreachable.

    Deliberately not the full admission chain — the no-new gate, ROI, payout
    floor, slots and capacity are the 7:20 quote-gaps email's job, on the same
    events, with the book reads that question needs. "allowed, not quoting" on
    a brand-new event usually just means the bot has not refreshed its
    universe into it yet."""
    tickers = sorted(rec["tickers"])
    n_quoted = sum(1 for t in tickers if t in quoted)
    n_allowed = sum(1 for t in tickers if imm.IncentiveMarketMaker._allowed(t))
    if n_quoted:
        label = f"quoting {n_quoted}/{len(tickers)}"
    elif n_allowed == 0:
        blocked = any(imm.IncentiveMarketMaker._blocked(t) for t in tickers)
        return "blocked (config)" if blocked else "not allowlisted"
    elif n_allowed < len(tickers):
        label = f"allowed {n_allowed}/{len(tickers)}"
    else:
        label = "allowed, not quoting"
    flag = cutoff_flag(rec, resolver, now)
    return f"{label} - {flag}" if flag else label


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------

def _et(dt: datetime) -> str:
    """Compact ET stamp, built by hand: %-m/%-I are glibc-only and the live
    box is Windows (where they raise), so no strftime shortcut here."""
    local = dt.astimezone(imm.ET)
    hour = local.hour % 12 or 12
    return (f"{local.month}/{local.day} {hour}:{local.minute:02d}"
            f"{'am' if local.hour < 12 else 'pm'}")


def fmt_window(rec: dict, now: datetime) -> str:
    """"9/12 8:11pm->9/15 12:00am 2.1d" — the paying window and what is left
    of it, or "ended" when no program on the event is still paying (a short
    program that expired between the start of the window and this email)."""
    if not rec["live_tickers"]:
        left = "ended"
    else:
        hours = (rec["end"] - now).total_seconds() / 3600.0
        if hours < 1:
            left = f"{max(hours * 60, 0):.0f}m"   # hourly programs live here
        elif hours < 48:
            left = f"{hours:.0f}h"
        else:
            left = f"{hours / 24:.1f}d"
    return f"{_et(rec['start'])}->{_et(rec['end'])} {left}"


def _clip(s: str, n: int) -> str:
    """ASCII-only ellipsis: the plain-text part of the email goes out as
    us-ascii where it can, and SMS-gateway recipients get it raw."""
    s = s or ""
    return s if len(s) <= n else s[:max(n - 3, 1)] + "..."


def _short(ev: str) -> str:
    """Drop the KX prefix — every ticker carries it and the column is tight."""
    return ev[2:] if ev.startswith("KX") else ev


def enrich_titles(client, recs) -> None:
    """Kalshi's own event title for the biggest pools (one GET each, bounded);
    everything else falls back to describe_event's pattern table alone."""
    for i, rec in enumerate(recs):
        title = ""
        if i < TITLE_BUDGET:
            try:
                title = gaps.event_title(client, rec["event"])
            except Exception:
                title = ""
        rec["what"] = gaps.describe_event(rec["event"], title)


def build_report(now_utc: datetime, since_hours=None, include_ended=False):
    """(text, html, subject, events, seen_next). Raises on an empty feed so
    the caller's retry loop treats it as the API glitch it is."""
    imm.load_file_event_overrides()
    imm.load_extra_allow_series()
    try:
        imm.load_finecon_extra_series()
    except Exception as e:            # a missing extension file is not fatal
        log(f"! finecon extension load failed ({e!r}); allowlist may be narrow")

    client = imm.build_client()
    statuses = ("active",) + (ENDED_STATUSES if include_ended else ())
    rows = fetch_programs_raw(client, statuses)
    if not rows:
        raise RuntimeError("/incentive_programs returned nothing (API glitch?)")
    events, skipped_types = roll_up_events(rows, now_utc)
    if not events:
        raise RuntimeError(f"{len(rows)} program rows but no liquidity programs")

    seen = load_seen()
    seeding = not (seen.get("events") or {})
    window_start, window_why = window_start_for(seen, now_utc, since_hours)
    known = seen.get("events") or {}
    series_seen = seen.get("series") or {}

    new_recs, late_recs = [], []
    for ev, rec in events.items():
        if ev in known:
            continue
        (new_recs if rec["start"] >= window_start else late_recs).append(rec)
    new_recs.sort(key=lambda r: (-r["pool_day"], -r["pool_total"], r["event"]))
    late_recs.sort(key=lambda r: (-r["pool_day"], -r["pool_total"], r["event"]))

    # Brand-new and relit SERIES — the family-level signal, which is where the
    # money has actually been (KXAVGT 2026-08-20, KXTEMPMIAH 2026-08-15).
    series_pool, series_new, series_relit = {}, set(), {}
    for rec in new_recs:
        s = rec["series"]
        series_pool[s] = series_pool.get(s, 0.0) + rec["pool_day"]
        prev = _entry(series_seen.get(s))
        last = imm.parse_iso_utc(prev.get("last_seen") or "")
        if not prev:
            if not seeding:
                series_new.add(s)
        elif last is not None:
            dark_days = (now_utc - last).total_seconds() / 86400.0
            if dark_days >= RELIT_DAYS:
                series_relit[s] = dark_days

    quoted = set(load_json(STATE_PATH).get("selected_tickers") or [])
    resolver = imm.EventStartResolver()
    enrich_titles(client, new_recs[:MAX_ROWS] + late_recs[:MAX_LATE_ROWS])
    for rec in new_recs + late_recs:
        rec.setdefault("what", gaps.describe_event(rec["event"]))
        rec["bot"] = bot_status(rec, quoted, resolver, now_utc)

    # Feed-health guard: a collapsed feed must not advance the watermark.
    n_programs = sum(r["n_programs"] for r in events.values())
    prev_programs = int(seen.get("feed_programs") or 0)
    feed_ok = not (prev_programs and n_programs < FEED_SHRINK_FRAC * prev_programs)
    banner = "" if feed_ok else (
        f"FEED SHRANK: {n_programs:,} active programs vs {prev_programs:,} at "
        f"the last email - treating this as an API problem, NOT a supply "
        f"event: the window is NOT advanced, so anything hidden today is "
        f"reported tomorrow.")

    seen_next = next_seen(seen, events, now_utc, feed_ok)

    ctx = {
        "now": now_utc, "window_start": window_start, "window_why": window_why,
        "new": new_recs, "late": late_recs, "seeding": seeding,
        "n_known": len(known), "events": events, "n_programs": n_programs,
        "feed_pool_day": sum(r["pool_day"] for r in events.values()),
        "prev_pool_day": float(seen.get("feed_pool_day") or 0.0),
        "prev_events": int(seen.get("feed_events") or 0),
        "series_new": series_new, "series_relit": series_relit,
        "series_pool": series_pool, "skipped_types": skipped_types,
        "banner": banner, "statuses": statuses,
    }
    text = render_text(ctx)
    html = render_html(ctx)
    pool_day = sum(r["pool_day"] for r in new_recs)
    today_et = now_utc.astimezone(imm.ET).date()
    subject = (f"New Kalshi incentive programs {today_et} - "
               f"{len(new_recs)} event{'' if len(new_recs) == 1 else 's'}, "
               f"${pool_day:,.0f}/day")
    return text, html, subject, events, seen_next


def _headline(ctx) -> list:
    """The numbers that decide whether the table is worth reading."""
    new = ctx["new"]
    pool_day = sum(r["pool_day"] for r in new)
    pool_total = sum(r["pool_total"] for r in new)
    markets = sum(len(r["tickers"]) for r in new)
    allowed = [r for r in new if not r["bot"].startswith(("not allowlisted",
                                                          "blocked"))]
    quoting = [r for r in new if r["bot"].startswith("quoting")]
    # POOL and RATE are different questions and the two diverge hard on short
    # programs — four $20 hourly markets are $80 of pool at a $1,920/day rate.
    # Lead with the money actually on the table, then the rate the table is
    # paid at (which is what the bot ranks on).
    lines = [f"{len(new)} new event{'' if len(new) == 1 else 's'} - "
             f"{markets} market{'' if markets == 1 else 's'} - "
             f"${pool_total:,.2f} of new pool, ${pool_day:,.2f}/day "
             f"while it runs"]
    if new:
        lines.append(
            f"  bot-eligible: {len(allowed)} event(s), "
            f"${sum(r['pool_day'] for r in allowed):,.2f}/day - "
            f"already quoting: {len(quoting)} event(s), "
            f"${sum(r['pool_day'] for r in quoting):,.2f}/day")
    dead = [r for r in new if "UNEARNABLE" in r["bot"] or "cutoff passed" in r["bot"]]
    if dead:
        lines.append(f"  unearnable at the bot's cutoff: {len(dead)} event(s), "
                     f"${sum(r['pool_day'] for r in dead):,.2f}/day")
    pool_of = ctx["series_pool"]
    if ctx["series_new"]:
        ranked = sorted(ctx["series_new"], key=lambda s: -pool_of.get(s, 0))
        lines.append("  NEW SERIES: " + ", ".join(
            f"{s} (${pool_of.get(s, 0):,.0f}/day)" for s in ranked[:8])
            + (f", +{len(ranked) - 8} more" if len(ranked) > 8 else ""))
    if ctx["series_relit"]:
        ranked = sorted(ctx["series_relit"].items(),
                        key=lambda kv: -pool_of.get(kv[0], 0))
        lines.append("  RELIT SERIES: " + ", ".join(
            f"{s} (dark {d:.0f}d, ${pool_of.get(s, 0):,.0f}/day)"
            for s, d in ranked[:8])
            + (f", +{len(ranked) - 8} more" if len(ranked) > 8 else ""))
    return lines


def _footer_lines(ctx) -> list:
    """Provenance + the two facts that decide how to read a thin email."""
    delta = ctx["feed_pool_day"] - ctx["prev_pool_day"]
    feed = (f"Feed now: {ctx['n_programs']:,} active liquidity programs over "
            f"{len(ctx['events']):,} events, ${ctx['feed_pool_day']:,.0f}/day "
            f"of open pool")
    if ctx["prev_pool_day"]:
        feed += (f" ({delta:+,.0f}/day, {len(ctx['events']) - ctx['prev_events']:+d} "
                 f"events vs the last email)")
    out = [feed]
    if ctx["seeding"]:
        out.append(f"First run: {ctx['n_known']}->{len(ctx['events']):,} events "
                   f"recorded as already-known; only events starting inside the "
                   f"window could be new.")
    elif ctx["late"]:
        out.append(f"{len(ctx['late'])} event(s) entered the feed this run with a "
                   f"start BEFORE the window "
                   f"(${sum(r['pool_day'] for r in ctx['late']):,.2f}/day) - "
                   f"listed as late arrivals, not new.")
    if ctx["skipped_types"]:
        out.append("Non-liquidity programs in the feed (ignored): " + ", ".join(
            f"{k}x{v}" for k, v in sorted(ctx["skipped_types"].items())))
    # The whole ladder shape is built on 0.5^(ticks behind best); a program
    # priced off a different discount factor is a rules change, not a detail.
    odd_df = sorted({df for r in ctx["new"] + ctx["late"] for df in r["dfs"]
                     if abs(df - 0.5) > 1e-9})
    if odd_df:
        out.append("NON-STANDARD discount factor on new programs: "
                   + ", ".join(f"{d:.4g}" for d in odd_df)
                   + " (the ladder assumes 0.5 per tick behind best)")
    out.append(f"Source: GET /incentive_programs status={'+'.join(ctx['statuses'])}; "
               f"period_reward centi-cents/10000. New = not seen before AND "
               f"earliest program start >= window start ({ctx['window_why']}).")
    return out


# BOT is last and left-aligned, so its padding costs nothing once the line is
# rstripped; the fixed part of a row is ~117 chars, in line with the 7:20
# quote-gaps table. The HTML table is the one to read — this is the fallback.
_COLS = (("EVENT", 26, "<"), ("WHAT IT IS", 26, "<"), ("MKTS", 4, ">"),
         ("$/DAY", 8, ">"), ("POOL$", 9, ">"), ("PROGRAM WINDOW (ET)", 33, "<"),
         ("TGT", 5, ">"), ("BOT", 22, "<"))


def _text_table(recs, now, total_recs=None) -> list:
    head = "  ".join(f"{name:{align}{width}}" for name, width, align in _COLS)
    lines = [head.rstrip()]
    widths = [w for _n, w, _a in _COLS]
    for rec in recs:
        cells = (_short(rec["event"]), rec.get("what", ""),
                 str(len(rec["tickers"])), f"{rec['pool_day']:,.2f}",
                 f"{rec['pool_total']:,.2f}", fmt_window(rec, now),
                 f"{rec['target']:,.0f}" if rec["target"] else "-",
                 rec.get("bot", ""))
        lines.append("  ".join(
            f"{_clip(c, w):{align}{w}}"
            for c, w, (_n, _w, align) in zip(cells, widths, _COLS)
        ).rstrip())
    rows = total_recs if total_recs is not None else recs
    if rows:
        total = ("TOTAL", "", str(sum(len(r["tickers"]) for r in rows)),
                 f"{sum(r['pool_day'] for r in rows):,.2f}",
                 f"{sum(r['pool_total'] for r in rows):,.2f}", "", "", "")
        lines.append("  ".join(
            f"{c:{align}{width}}" for c, (_n, width, align) in zip(total, _COLS)
        ).rstrip())
    return lines


def render_text(ctx) -> str:
    now = ctx["now"]
    out = [f"New Kalshi incentive programs - {now.astimezone(imm.ET):%Y-%m-%d}",
           f"Window: {_et(ctx['window_start'])} -> {_et(now)} ET "
           f"({(now - ctx['window_start']).total_seconds() / 3600:.1f}h, "
           f"{ctx['window_why']})", ""]
    if ctx["banner"]:
        out += ["!! " + ctx["banner"], ""]
    out += _headline(ctx) + [""]
    if ctx["new"]:
        shown = ctx["new"][:MAX_ROWS]
        out += _text_table(shown, now, total_recs=ctx["new"])
        if len(ctx["new"]) > len(shown):
            rest = ctx["new"][MAX_ROWS:]
            out.append(f"... and {len(rest)} smaller event(s), "
                       f"${sum(r['pool_day'] for r in rest):,.2f}/day "
                       f"(TOTAL above covers all of them)")
    else:
        out.append("No new incentive-reward events started in the window.")
    if ctx["late"] and not ctx["seeding"]:
        out += ["", "LATE ARRIVALS - first seen now, started before the window",
                *_text_table(ctx["late"][:MAX_LATE_ROWS], now,
                             total_recs=ctx["late"])]
    out += [""] + _footer_lines(ctx)
    return "\n".join(out)


def _row_html(rec, now, bg) -> str:
    # Kalshi's event title lands in the WHAT cell, so every cell is escaped:
    # one "&" or "<" in an exchange title would otherwise eat the table.
    bot = rec.get("bot", "")
    if bot.startswith("quoting"):
        bot_style = TDL + "color:#1e8449;font-weight:600;"
    elif "UNEARNABLE" in bot or "cutoff passed" in bot:
        bot_style = TDL + "color:#c0392b;font-weight:600;"
    else:
        bot_style = TDL
    mono = 'font-family:Consolas,Menlo,monospace'
    return ('<tr style="background:{bg}">'
            '<td style="{tdl}"><span style="{mono}">{ev}</span></td>'
            '<td style="{tdl}">{what}</td><td style="{td}">{mkts}</td>'
            '<td style="{td};font-weight:700">{dpd}</td>'
            '<td style="{td}">{pool}</td>'
            '<td style="{tdl};white-space:nowrap">{win}</td>'
            '<td style="{td}">{tgt}</td>'
            '<td style="{bot_style}">{bot}</td></tr>').format(
        bg=bg, tdl=TDL, td=TD, mono=mono, ev=_esc(_short(rec["event"])),
        what=_esc(_clip(rec.get("what", ""), 60)), mkts=len(rec["tickers"]),
        dpd=f"{rec['pool_day']:,.2f}", pool=f"{rec['pool_total']:,.2f}",
        win=_esc(fmt_window(rec, now)).replace("-&gt;", "&rarr;"),
        tgt=f"{rec['target']:,.0f}" if rec["target"] else "&mdash;",
        bot_style=bot_style, bot=_esc(bot))


def _table_html(recs, now, total_recs=None) -> str:
    h = ['<table style="border-collapse:collapse;margin:8px 0;font-size:13px">',
         '<tr style="background:#f0f0f0;font-weight:600">'
         '<td style="{0}">EVENT</td><td style="{0}">WHAT IT IS</td>'
         '<td style="{1}">MKTS</td><td style="{1}">$/DAY</td>'
         '<td style="{1}">POOL$</td><td style="{0}">PROGRAM WINDOW (ET)</td>'
         '<td style="{1}">TGT</td><td style="{0}">BOT</td></tr>'.format(TDL, TD)]
    for i, rec in enumerate(recs):
        h.append(_row_html(rec, now, "#fafafa" if i % 2 else "#fff"))
    rows = total_recs if total_recs is not None else recs
    if rows:
        h.append('<tr style="background:#f0f0f0;font-weight:700">'
                 '<td style="{0}">TOTAL</td><td style="{0}"></td>'
                 '<td style="{1}">{2}</td><td style="{1}">{3:,.2f}</td>'
                 '<td style="{1}">{4:,.2f}</td><td style="{0}"></td>'
                 '<td style="{1}"></td><td style="{0}"></td></tr>'.format(
                     TDL, TD, sum(len(r["tickers"]) for r in rows),
                     sum(r["pool_day"] for r in rows),
                     sum(r["pool_total"] for r in rows)))
    h.append("</table>")
    return "".join(h)


def render_html(ctx) -> str:
    now = ctx["now"]
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">']
    h.append('<div style="font-size:17px;font-weight:600">New Kalshi incentive '
             'programs <span style="color:#888;font-weight:400">&mdash; {}</span>'
             '</div>'.format(now.astimezone(imm.ET).strftime("%Y-%m-%d")))
    h.append('<div style="color:#888;font-size:12px;margin:2px 0 8px">Window {} '
             '&rarr; {} ET ({:.1f}h, {})</div>'.format(
                 _et(ctx["window_start"]), _et(now),
                 (now - ctx["window_start"]).total_seconds() / 3600,
                 _esc(ctx["window_why"])))
    if ctx["banner"]:
        h.append('<div style="background:#fdecea;border-left:4px solid #c0392b;'
                 'color:#8e2b21;padding:8px 10px;margin:8px 0;font-weight:600">'
                 '{}</div>'.format(_esc(ctx["banner"])))
    head = _headline(ctx)
    h.append('<div style="font-size:20px;font-weight:800;margin:6px 0 2px">{}</div>'
             .format(_esc(head[0])))
    for line in head[1:]:
        emph = line.strip().startswith(("NEW SERIES", "RELIT SERIES"))
        h.append('<div style="font-size:13px;margin:1px 0;{}">{}</div>'.format(
            "color:#b9770e;font-weight:600;" if emph else "color:#555;",
            _esc(line.strip())))
    if ctx["new"]:
        h.append(_table_html(ctx["new"][:MAX_ROWS], now, total_recs=ctx["new"]))
        if len(ctx["new"]) > MAX_ROWS:
            rest = ctx["new"][MAX_ROWS:]
            h.append('<div style="color:#888;font-size:12px">&hellip; and {} '
                     'smaller event(s), ${:,.2f}/day &mdash; the TOTAL row covers '
                     'all of them</div>'.format(len(rest),
                                                sum(r["pool_day"] for r in rest)))
    else:
        h.append('<div style="margin:10px 0;color:#555">No new incentive-reward '
                 'events started in the window.</div>')
    if ctx["late"] and not ctx["seeding"]:
        h.append('<div style="font-size:15px;font-weight:600;margin:14px 0 2px">'
                 'Late arrivals <span style="color:#888;font-weight:400">'
                 '&mdash; first seen now, started before the window</span></div>')
        h.append(_table_html(ctx["late"][:MAX_LATE_ROWS], now,
                             total_recs=ctx["late"]))
    h.append('<div style="color:#888;font-size:12px;margin-top:10px;'
             'border-top:1px solid #eee;padding-top:8px">{}</div>'.format(
                 "<br>".join(_esc(ln) for ln in _footer_lines(ctx))))
    h.append("</div>")
    return "".join(h)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no state written")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="alias for --dry")
    ap.add_argument("--since", type=float, metavar="HOURS",
                    help="override the window (default: since the last email)")
    ap.add_argument("--include-ended", action="store_true",
                    help=f"also page status={'/'.join(ENDED_STATUSES)} so programs "
                         f"that started AND ended inside the window are caught")
    ap.add_argument("--html-out", help="with --dry, also write the HTML here")
    args = ap.parse_args(argv)
    dry = args.dry or args.print_only

    now_utc = datetime.now(timezone.utc)
    today_ct = now_utc.astimezone(imm.CT).date()
    marker = os.path.join(imm.STATUS_DIR, f"imm_new_programs_sent_{today_ct}.marker")
    if not (args.test or dry) and os.path.exists(marker):
        log(f"new-programs email already sent for {today_ct}; exiting")
        return 0

    # The 7:30 trigger can fire while the laptop is still in Modern Standby
    # with the radio off — same 8x5min patience as the other morning emails.
    attempts = 1 if (args.test or dry) else 8
    text = html = subject = seen_next = None
    for attempt in range(1, attempts + 1):
        try:
            text, html, subject, _events, seen_next = build_report(
                now_utc, since_hours=args.since, include_ended=args.include_ended)
            break
        except Exception as e:
            log(f"new-programs build attempt {attempt}/{attempts} failed: {e!r}")
            if attempt == attempts:
                log("giving up for today")
                return 1
            time.sleep(300)
    log("new-programs body:\n" + text)
    if dry:
        if args.html_out:
            with open(args.html_out, "w", encoding="utf-8") as f:
                f.write(html)
            log(f"wrote {args.html_out}")
        return 0

    alerter = imm.Alerter("IMM-NEWPROG", live=True)
    if not alerter.enabled:
        log("cannot send new-programs email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject, html=html)
        if ok:
            break
        log(f"new-programs send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"new-programs send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    # Only a SENT email may move the watermark: an unsent day's events have to
    # stay new so tomorrow's email still reports them. A --test send counts —
    # it is an email Jack saw.
    save_seen(seen_next)
    if not args.test:
        # Post-send bookkeeping: never turn a delivered email into a non-zero
        # exit (and a red task) because a marker could not be written.
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would "
                f"send again")
        cutoff = today_ct - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR,
                                          "imm_new_programs_sent_*.marker")):
            name = os.path.basename(old)[len("imm_new_programs_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
