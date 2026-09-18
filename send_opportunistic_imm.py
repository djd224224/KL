#!/usr/bin/env python3
r"""send_opportunistic_imm.py — the daily "Opportunistic IMM" email
(Jack 2026-09-05: "add daily email called 'opportunistic IMM' showing a
table of the events quoted, earning est, P&L, and net").

The "opportunistic" book is THREE tiers:
  finecon   the hand-curated Finance/Economics quiet-print families
            (incentive_mm.FINECON_SERIES) — top-N-by-ROI group walk,
            quote-to-completion, daily over-cap openings
  scan      the OPEN SCAN (Jack 2026-09-05 "extend the opportunistic IMM
            with 15 slots and 5 to scan all markets"): every other live-
            program market, machine-screened for adverse selection
            (incentive_mm SCAN_*; membership persisted as scan_members)
  *CC       the Carbon Arc credit-card family (Jack 2026-09-11: "AmazonCC
            and StarbucksCC arent in the opportunistic daily email
            anymore"). REPORTING ONLY — the family is quoted by the NORMAL
            book and that does not change here. It entered the normal book
            on 2026-09-10 by name pattern (ALLOW_FAMILY_SUFFIXES), which
            took KXAMZNCC out of FINECON_SERIES and KXSBUXCC out of the
            finecon extra file; the two-tier _tier() below then matched
            neither, so all 30 *CC events silently left this email. Quoting
            tier and reporting tier are DIFFERENT questions: Jack's 9/10
            "picked up by the normal IMM ... not the opportunistic" was
            about slots and caps (no finecon walk, no scan screen), and his
            9/11 ask is about this scorecard. Membership is read from
            imm.ALLOW_FAMILY_SUFFIXES, so a future family suffix lands here
            automatically.
This email is that book's own scorecard, separate from the whole-account
digest: a combined headline, then ONE TABLE PER TIER in the same format
(Jack 2026-09-06: "a similarly formatted table for non-finecon
opportunistic bot"), each with its own slot/openings line and TOTAL row,
then the CUMULATIVE table (Jack 2026-09-11: "make sure it shows not just
active, but also cumulative") — see below.

One row per currently-quoted opportunistic EVENT:
  EARN EST   the bot's accrued reward estimate over the markets it is
             tracking NOW (NOT period-to-date: see the EST note below —
             the accrual is not reset at a period end, but it IS deleted
             when a market goes unquoted and flat)
  P&L        trading P&L on the event's markets (realized + settlement +
             open-book MTM) over the digest's attribution window — the cost
             of holding the inventory that earns the reward
  NET        P&L + EARN EST — the position's true economics

The per-tier tables are the ACTIVE book: what is quoted or held right now.
The CUMULATIVE table is the same three tiers measured over every event each
one ever touched, settled-and-gone included, on the most trustworthy basis
available per column:
  CREDITED  actual Kalshi money, all-time, from the recon ledger. Per EVENT
            and back to 2026-03-21, so this column is complete.
  EST       the bot's own accrual estimator, over the markets it is
            tracking RIGHT NOW. Two scopes, one stream: EST and CREDITED
            measure the same earnings and NEITHER CONTAINS THE OTHER, so
            the tables never sum them.
              EST > CREDITED is the normal case — the accrual code never
            resets at a period boundary, so a continuously-quoted market's
            estimate is credited-plus-still-in-flight (KXAMZNCC-26OCT07:
            est $8.70 against $3.79 credited).
              EST < CREDITED happens two ways, both real on 2026-09-11.
            (a) accrued_est is PRUNED with known_tickers, and
            known_tickers &= (managed | positions) drops a market the
            cycle it stops being quoted and is flat — so accrual is
            DELETED and restarts from zero across program periods.
            KXMONSTERPOS-26OCT03-T105 went "-> gone" on 9/07 and 9/08 and
            only resumed quoting 9/11 02:23Z: its $0.44 is a fresh accrual
            while its $1.05 credit was earned in the earlier stretch.
            (b) credits are per EVENT and forever, while a row's EST sums
            only the tickers still in its book. KXCBDECISIONNZ-26OCT27 was
            credited twice ($4.61 + $6.31) but only -HOLD survives in state
            (the one market still carrying inventory), so $4.61 has no EST
            counterpart at all.
              The estimator itself is not the problem: on the market that
            was never pruned, est $6.33 vs credit $6.31.
  REALIZED  the bot's own per-market realized trading P&L, summed from the
            `realized` analytics sink's deltas. That sink only starts
            2026-09-06 (commit ffe4b48), so this column is floored at the
            first sink day and says so in the footer rather than implying
            history it does not have.
  MTM       open-book mark-to-market right now, same own_book()/mids path
            as the active tables.
Tier attribution has to survive the event going away. finecon and *CC are
series-name rules, so they are durable by construction. The scan tier is a
per-TICKER membership the bot prunes (scan_book sheds flat non-members at
each daily roll), so a settled scan event would silently stop being ours:
the roster below folds the `selection_events` sink's is_scan flag into a
durable per-event set cached in opportunistic_roster.json. Measured on
2026-09-11: 7 credited scan events worth $118.40 had already left
scan_book, against $21.83 the two-tier footer was reporting as the book's
whole lifetime.
Numbers come from the SAME validated path as the whole-account digest: this
script imports send_imm_digest and calls its pnl_windows() / own_book() /
credit-ledger helpers, so a row here can never disagree with the digest.

Under the tier tables sits the SCAN PERF block: the open-scan performance
loop's scorecard, read from STATUS_DIR/scan_perf.json (the same file the bot
loads, honouring the same IMM_SCAN_PERF_FILE override). It prints the table's
age, the WEIGHT and BAR actually in force, the cohort table on BOTH bases with
the acting column marked, every non-neutral key, the mark-source mix, and —
required, not optional — THE COST LINE: the seats the loop left empty, the
admissions it barred, and the MODELLED rent that went with them. Forgone rent
is never observed as a loss anywhere, so without that line the two-week review
is structurally biased toward keeping the loop. A missing or garbled table
prints "no table" and changes nothing else in the email.

STRICTLY READ-ONLY. Scheduled daily 7:25 AM ET ("KL imm opportunistic"),
after the 7:10 digest and 7:20 quote-gaps. --test sends now ignoring the
sent-marker; --dry / --print build and print only.
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

# Importing send_imm_digest applies the live launcher env (its $ProbeEnv
# mirror) and the ALERT_EMAIL_* registry fallback at import, BEFORE
# incentive_mm's config is read — same ordering the digest itself relies on.
import send_imm_digest as sd
import incentive_mm as imm
from incentive_mm import log
from send_imm_digest import (TD, TDL, _f, _event_of, _short_event, _pnl_span,
                             load_json, own_book, fetch_own_fills,
                             current_mids, pnl_windows, status_summary,
                             load_credit_ledger, STATE_PATH, STATUS_PATH,
                             FILL_LOOKBACK_HOURS,
                             # the open-scan performance table's reader lives
                             # in the digest so both emails parse it once, the
                             # same way (see that module's SCAN_PERF section)
                             load_scan_perf, scan_perf_header, scan_perf_cost,
                             scan_perf_cost_line, scan_perf_cost_basis_note,
                             SCAN_PERF_PATH, SCAN_PERF_MAX_AGE_H)

# Compact family labels; anything unmatched falls back to the Kalshi event
# title (fetched + cached below), so new Carbon Arc self-extensions read
# fine without a code change here.
_LABEL = {
    "KXSPRLVL": "US Strategic Petroleum Reserve level",
    "KXCBDECISIONNZ": "RBNZ rate decision",
    "KXCBDISRAEL": "Bank of Israel rate decision",
    "KXVENEZCRUDE": "Venezuela crude output",
    "KXAAAGASMINM": "Monthly low US gas price",
    "KXAAAGASMAXM": "Monthly high US gas price",
    "KXBRAZILGDP": "Brazil GDP growth",
    "KXJOLTSOPEN": "US job openings (JOLTS)",
    "KXDATACENTCON": "US data-center construction spend",
    "KXWENBACONATOR": "Wendy's Baconator price",
    "KXTBCRUNCHWRAP": "Taco Bell Crunchwrap price",
    "KXTXOIL": "Texas crude oil production",
    "KXVAPORTTEU": "Port of Virginia container volume",
    "KXDKS": "Dick's quarterly KPI",
    "KXZM": "Zoom quarterly KPI",
    "KXURBN": "Urban Outfitters KPI",
    "KXLOW": "Lowe's KPI",
    "KXDG": "Dollar General KPI",
    "KXAFRM": "Affirm KPI",
    "KXBBY": "Best Buy KPI",
    "KXWSM": "Williams-Sonoma KPI",
    "KXOKTA": "Okta KPI",
    "KXDRPEPPERPOS": "Dr Pepper point-of-sale growth",
    # the two *CC members Jack names (2026-09-11); the other 28 resolve from
    # the live event title, which reads "<Company> Credit Card Spend".
    "KXAMZNCC": "Amazon credit-card spend",
    "KXSBUXCC": "Starbucks credit-card spend",
}
_ADS_SUFFIX = "ADS"
_title_cache: dict = {}


def family_label(series: str) -> str:
    """Deterministic label for a name-pattern family, or "" — the answer when
    the live title cannot be fetched. Without it an un-mapped *CC sibling
    falls back to its raw ticker and the email leaks the ticker scheme for 28
    of the family's 30 events."""
    if series.endswith(_ADS_SUFFIX):
        return "Ad spend (Carbon Arc)"
    if is_family(series):
        return "Credit-card spend (Carbon Arc)"
    return ""


def _family_suffix() -> str:
    """The family suffix this email reports as its own tier. One suffix is
    configured (CC); if a second is ever added, they share the tier and the
    label joins them rather than silently naming only the first."""
    return "/".join(getattr(imm, "ALLOW_FAMILY_SUFFIXES", ()) or ()) or "?"


def _family_event_top_n() -> int:
    """The per-event ROI cut that actually binds the family, read from the
    bot's own EVENT_TOP_N rather than restated here (it is "*CC:3" today)."""
    return max((imm.event_top_n_for("KX" + suf)
                for suf in (getattr(imm, "ALLOW_FAMILY_SUFFIXES", ()) or ())),
               default=0)


def event_label(client, event_ticker: str) -> str:
    series = event_ticker.split("-")[0]
    if series in _LABEL:
        return _LABEL[series]
    if series.endswith(_ADS_SUFFIX):
        return "Ad spend (Carbon Arc)"
    if event_ticker in _title_cache:
        return _title_cache[event_ticker]
    title = ""
    try:
        e = (client.get(f"/events/{event_ticker}") or {}).get("event") or {}
        title = str(e.get("title") or e.get("sub_title") or "").strip()
    except Exception:
        pass
    title = title if len(title) <= 42 else title[:39] + "..."
    _title_cache[event_ticker] = title or family_label(series) or series
    return _title_cache[event_ticker]


def is_family(ticker_or_event: str) -> bool:
    """The *CC Carbon Arc family (Jack 2026-09-11). Read off
    imm.ALLOW_FAMILY_SUFFIXES — the bot's OWN membership rule — so this email
    cannot drift from the allowlist the way it did on 2026-09-10, and a
    future family suffix is covered without a change here."""
    series = ticker_or_event.split("-")[0]
    return any(series.endswith(suf)
               for suf in (getattr(imm, "ALLOW_FAMILY_SUFFIXES", ()) or ()))


def tier_of(ticker_or_event: str, fin, scan_set) -> str:
    """"finecon" | "scan" | "family" | None — which opportunistic tier owns
    this market (or event), or None for the rest of the normal book.

    MODULE LEVEL ON PURPOSE. This lived as a closure inside build_report
    until 2026-09-11, and that is exactly how 30 *CC series left the email
    without a single one of 538 green tests noticing: the tests could only
    reach the pure formatters. It is the one piece of this script with a
    real invariant — every tier the bot quotes must map to a tier this email
    reports — so it has to be callable from a test.

    Precedence is finecon, then scan, then the name-pattern family. finecon
    first because a series in both sets is walked and capped as finecon
    (incentive_mm._allowed checks FINECON_SERIES before ALLOW_FAMILY_SUFFIXES);
    scan before family because scan membership is an explicit per-ticker fact
    the bot persisted, and a member admitted before the family rule existed
    must keep reporting where the bot put it. In practice the three cannot
    overlap at all — an allowlisted market is never scanned
    (scan_universe_reason -> "allowed") — so this order only decides the
    stale-membership edge.

    `scan_set` is tickers for the active book (state["scan_members"]) and
    EVENTS for the cumulative one (the durable roster); both are plain
    membership tests, so one function serves both.
    """
    if ticker_or_event.split("-")[0] in fin:
        return "finecon"
    if ticker_or_event in scan_set:
        return "scan"
    if is_family(ticker_or_event):
        return "family"
    return None


# ---- DURABLE HISTORY (Jack 2026-09-11 "not just active, but also
# cumulative") ---------------------------------------------------------------
# Cumulative cannot be read off the live state: accrued_est is pruned with
# known_tickers (a settled market's accrual is deleted) and scan_book sheds
# flat non-members at each daily roll, so the book's own past disappears from
# imm_state.json market by market. The bot's analytics sinks DO keep it, and
# this cache folds them once per UTC day so the email stays O(one day) as the
# sinks grow (27MB on 2026-09-11, ~4MB/day) and keeps its history if those
# files are ever archived off — the repo has lost data to silent retention
# twice, so the durable copy is the point, not the speed.
ROSTER_PATH = os.path.join(imm.STATUS_DIR, "opportunistic_roster.json")
# NEW-SINCE-LAST-EMAIL (Jack 2026-09-13: "always bold the events that are
# new (werent in the previous email)"). Every SENT email (scheduled or
# --test, never --dry) records the events its tables showed; the next
# build bolds the rows whose event is not in that record. Before the first
# record exists, the previous email is recovered from the task log, which
# has always carried each sent body verbatim ("opportunistic body:" ...
# "opportunistic send: ok").
LAST_SENT_PATH = os.path.join(imm.STATUS_DIR, "opportunistic_last_sent.json")
TASK_LOG_PATH = os.path.join(imm.STATUS_DIR, "opportunistic-task.log")
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z ")
_ROW_EVENT_RE = re.compile(r"^[A-Z0-9]+-[A-Z0-9.-]+$")


def parse_last_sent_body(log_text: str):
    """(sent_at, {short event names}) of the LAST body in the task log that
    was actually sent (its block is followed by 'opportunistic send: ok'
    before the next body), else (None, None). A --dry body is logged too and
    must not count as an email anyone saw. Table rows are recognised by
    their first column: a short event name (the KX prefix dropped) in the
    27-character EVENT field; header, TOTAL and prose lines never match."""
    blocks = []      # [start_line_index, ts, sent_ok]
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        m = _LOG_TS_RE.match(line)
        if not m:
            continue
        if line.endswith("opportunistic body:"):
            blocks.append([i, m.group(1) + "Z", False])
        elif "opportunistic send: ok" in line and blocks:
            blocks[-1][2] = True
    sent = [b for b in blocks if b[2]]
    if not sent:
        return None, None
    start, ts, _ok = sent[-1]
    events = set()
    for line in lines[start + 1:]:
        if _LOG_TS_RE.match(line):
            break
        first = line[:27].strip()
        if first and first not in ("EVENT", "TOTAL", "TIER") \
                and _ROW_EVENT_RE.match(first):
            events.add(first)
    return ts, events


def previous_email_events():
    """(sent_at, {short event names}, source) for the previous SENT email:
    the persisted record first, the task-log body as the fallback, and
    (None, None, 'none') when neither exists -- in which case nothing is
    bolded and the email says so, rather than bolding everything."""
    rec = load_json(LAST_SENT_PATH) or {}
    if rec.get("events") is not None:
        return (str(rec.get("sent_at") or ""),
                {_short_event(str(e)) for e in rec.get("events") or []},
                "record")
    try:
        with open(TASK_LOG_PATH, encoding="utf-8", errors="replace") as f:
            ts, evs = parse_last_sent_body(f.read())
    except OSError:
        ts, evs = None, None
    if evs is None:
        return None, None, "none"
    return ts, evs, "log"


def write_last_sent(now_utc, events, perf_verdicts=None) -> None:
    """Best-effort, after a successful send only.

    `perf_verdicts` ({"series:KXFOO": "down_rank"}) rides in the same record
    so the next email can bold the SCAN PERF rows whose verdict CHANGED. One
    record, one write, one fsync-free os.replace: a second file would be a
    second thing to go stale independently of this one."""
    try:
        tmp = LAST_SENT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sent_at": now_utc.isoformat(),
                       "events": sorted(set(events)),
                       "perf_verdicts": dict(perf_verdicts or {})}, f)
        os.replace(tmp, LAST_SENT_PATH)
    except OSError as e:
        log(f"opportunistic last-sent record not written ({e!r}); the next "
            f"email falls back to the task log")


def account_realized(client) -> tuple:
    """(realized_by_ticker, position_by_ticker) from the account's UNSETTLED
    positions — Kalshi's own `realized_pnl_dollars`, which is per market and
    LIFETIME.

    This is the authoritative answer to "what has this market actually cost
    us", and the email had no access to it: the row P&L was open-book MTM
    plus whatever realized the fill replay could attribute in the past 24h,
    so trades older than a day were simply absent. Measured 2026-09-11 on the
    two events Jack queried: KXAAAGASMINM-26SEP30 printed P&L +$1.30 where
    Kalshi's realized alone is -$5.20, and KXAAAGASMAXM-26SEP30 printed
    -$17.48 against realized -$7.55 on its live strikes.

    CAVEAT, and why the caller still cross-checks: this figure is
    ACCOUNT-level. The key is shared with the crypto fleet and with Jack's
    manual trading, so on a market the bot does not solely own it would
    credit someone else's P&L to this book. The caller therefore takes it
    only where the bot's own position MATCHES the account's, and falls back
    to the bot-attributed sink otherwise.

    Settled markets age out of this endpoint entirely, so it is a union with
    the sink, never a replacement for it."""
    real, pos = {}, {}
    cursor = None
    for _page in range(40):
        resp = client.get_positions(limit=200, cursor=cursor,
                                    settlement_status="unsettled")
        for p in (resp.get("market_positions") or resp.get("positions") or []):
            t = p.get("ticker")
            if not t:
                continue
            real[t] = _f(p.get("realized_pnl_dollars"))
            pos[t] = _f(p.get("position"))
        cursor = resp.get("cursor")
        if not cursor:
            break
    return real, pos


def realized_for(tickers, acct_real, acct_pos, bot_pos, sink) -> tuple:
    """(dollars, n_disputed) — lifetime realized over `tickers`.

    Per market: Kalshi's own realized where the market is still in the
    account AND the bot's position matches it (so the number is ours), the
    bot-attributed sink otherwise. The sink only reaches back to its first
    day, so a pre-sink market the bot no longer holds contributes nothing —
    that is a floor, and the footer says so."""
    tot, disputed = 0.0, 0
    for t in tickers:
        if t in acct_real:
            if abs(_f(acct_pos.get(t)) - _f(bot_pos.get(t))) <= 0.51:
                tot += acct_real[t]
                continue
            disputed += 1                 # shared market: keep OUR attribution
        tot += _f(sink.get(t))
    return tot, disputed


def _sink_days(name: str) -> list:
    """[(utc_date, path)] for STATUS_DIR/<name>_YYYY-MM-DD.jsonl, oldest
    first. The sink names its files by UTC date (incentive_mm._sink), so the
    fold boundary below is UTC too, NOT the ET/CT day this email is keyed on."""
    out = []
    for path in glob.glob(os.path.join(imm.STATUS_DIR, f"{name}_*.jsonl")):
        day = os.path.basename(path)[len(name) + 1:-len(".jsonl")]
        if len(day) == 10 and day[4] == "-":
            out.append((day, path))
    return sorted(out)


def _read_roster() -> dict:
    r = load_json(ROSTER_PATH) or {}
    # SCHEMA 2 keys realized by TICKER, not event: the row P&L unions it
    # per market with Kalshi's own realized_pnl_dollars, which needs the same
    # granularity. A cache written by schema 1 is discarded rather than
    # migrated — it re-folds from the sinks in 0.2s.
    if int(_f(r.get("schema"))) != 2:
        r = {}
    return {"scan_events": set(r.get("scan_events") or []),
            "realized": {str(k): _f(v)
                         for k, v in (r.get("realized") or {}).items()},
            "folded_scan": set(r.get("folded_scan") or []),
            "folded_realized": set(r.get("folded_realized") or []),
            "first_day": str(r.get("first_day") or "")}


def _write_roster(r: dict) -> None:
    """Best-effort. A cache that cannot be written costs a full re-fold next
    run, not a wrong number, so it must never fail the email."""
    try:
        tmp = ROSTER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": 2,
                       "scan_events": sorted(r["scan_events"]),
                       "realized": {k: round(v, 6)
                                    for k, v in r["realized"].items()},
                       "folded_scan": sorted(r["folded_scan"]),
                       "folded_realized": sorted(r["folded_realized"]),
                       "first_day": r["first_day"]}, f)
        os.replace(tmp, ROSTER_PATH)
    except OSError as e:
        log(f"opportunistic roster cache not written ({e!r}); "
            f"next run re-folds from the sinks")


def durable_history(today_utc: str) -> dict:
    """{"scan_events", "realized", "first_day"} — the opportunistic book's
    history, folded out of the bot's analytics sinks.

    scan_events is a SET of event tickers the open-scan tier ever QUOTED.
    The membership test is `is_scan AND decision == "selected"`, which is
    exactly how the bot itself forms scan_members
    (`new_scan = {t for t, m in selected.items() if m.scan}`). The is_scan
    flag ALONE is not membership — the sink logs every decision CHANGE, and
    is_scan rides on the candidate meta, so a market the scan merely looked
    at and rejected carries it too. Measured 2026-09-11: 136 events carried
    is_scan, only 39 were ever selected, and all $118.40 of ledger credit on
    the other 97 belongs to markets the bot never quoted through this tier —
    $84.74 of it on `manual` stand-asides, i.e. Jack's own orders. Attributing
    those to the scan tier is the inverse of the "IMM own fills are not
    manual" mistake and inflated the lifetime footer 4.8x.
    A set folds idempotently, so a partially written day can be re-read
    safely; it is only marked folded once the UTC day is complete.

    realized is TICKER -> cumulative realized trading dollars, summed from
    the `realized` sink's DELTAS. Deltas are NOT idempotent — re-reading today's
    partial file would double-count — so only COMPLETE days are ever cached
    and today's file is summed live on top of the cache every run. (Deltas,
    not realized_total_dollars: the tracker's per-ticker total restarts from
    zero with the process ~20x/day, which is exactly why the sink exists.)

    first_day is the earliest sink day ever seen, persisted so the footer can
    keep stating the floor honestly after old sink files are archived away."""
    r = _read_roster()
    live = {}
    for day, path in _sink_days("selection_events"):
        r["first_day"] = min(r["first_day"] or day, day)
        if day in r["folded_scan"]:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    # substring pre-filter on BOTH halves of the membership
                    # test: the sink writes one long line per decision change
                    # and most carry a reject reason, not "selected"
                    if ('"is_scan": true' not in line
                            or '"decision": "selected"' not in line):
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if (rec.get("decision") != "selected"
                            or not rec.get("is_scan")):
                        continue
                    ev = rec.get("event_ticker")
                    if ev:
                        r["scan_events"].add(str(ev))
        except OSError:
            continue
        if day < today_utc:
            r["folded_scan"].add(day)
    for day, path in _sink_days("realized"):
        r["first_day"] = min(r["first_day"] or day, day)
        complete = day < today_utc
        if complete and day in r["folded_realized"]:
            continue
        # Accumulate into a PER-DAY dict and merge only on a clean read. A
        # part-read that raised (locked file, disk) used to leave its partial
        # deltas in the cache with the day still unfolded, so the next run
        # folded the whole day again on top — a permanent, silent overstate.
        day_sum: dict = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    tk = rec.get("ticker")
                    if tk:
                        day_sum[str(tk)] = (
                            day_sum.get(str(tk), 0.0)
                            + _f(rec.get("realized_delta_dollars")))
        except OSError:
            continue
        into = r["realized"] if complete else live
        for ev, v in day_sum.items():
            into[ev] = into.get(ev, 0.0) + v
        if complete:
            r["folded_realized"].add(day)
    _write_roster(r)
    realized = dict(r["realized"])
    for ev, v in live.items():
        realized[ev] = realized.get(ev, 0.0) + v
    return {"scan_events": r["scan_events"], "realized": realized,
            "first_day": r["first_day"]}


def tier_totals(rows) -> dict:
    earn = sum(r["earn"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    return {"mkts": sum(r["mkts"] for r in rows),
            "held": sum(r.get("held", 0) for r in rows),
            # .get: CRED is per EVENT and arrives from the ledger, which can
            # legitimately hold nothing for a young event
            "cred": sum(r.get("cred", 0.0) for r in rows),
            # None, not 0.0, when NO row in the tier can measure its period
            # yet: a tier-wide "0.00" would read as "earned nothing this
            # period" when it means "no baseline observed yet"
            "period": (sum(r["period"] for r in rows
                           if r.get("period") is not None)
                       if any(r.get("period") is not None for r in rows)
                       else None),
            "earn": earn, "pnl": pnl, "net": earn + pnl}


def text_table(rows, new_events=frozenset(), perf=None) -> list:
    """Plain-text event table + TOTAL row. One call per tier, so the two
    tiers' tables are identical in format by construction.

    QUOTED = markets the bot is quoting now. HELD = markets of the same
    event it has stopped quoting but still holds inventory in, or has
    accrued reward on (Jack 2026-09-07). Their P&L and accrual are in the
    row; they just consume no slot.

    `perf` is an event -> PERF cell map and is passed for the OPEN SCAN tier
    ONLY: the performance loop is scoped on m.scan, so a PERF column on the
    finecon or *CC tables would invent a policy that does not exist there.
    Absent (the default) the column is not rendered at all, so those two
    tables stay byte-identical to what they were."""
    t = tier_totals(rows)
    # EVENT holds 26 so a <SERIES>-<YYMMMDD> label keeps its day-of-month:
    # at 23 the two events of a weekly series printed identically
    # (BABELMANDEBWEEKLY-26SEP13 and -26SEP20 both truncated to ...26SEP).
    L = [f"{'EVENT':<27}{'WHAT IT IS':<29}{'QUOT':>5}{'HELD':>5}"
         f"{'PERIOD$':>9}{'ALL-TIME$':>11}{'CRED$':>9}"
         f"{'P&L$':>10}{'NET$':>10}"
         + (f"{'PERF':>8}" if perf else "")]
    for r in rows:
        # blank, not 0.00 — an event with no credit yet reads as "nothing has
        # landed", which is the fact; the HTML twin blanks it the same way
        cred = r.get("cred") or 0.0
        cred_s = f"{cred:,.2f}" if cred else ""
        # "-" not "0.00": the bot has not seen this market's program roll
        # yet, so its period accrual is UNMEASURABLE, not nil
        per = r.get("period")
        per_s = f"{per:,.2f}" if per is not None else "-"
        # '*' = new since the previous email (the HTML twin bolds the row)
        _name = (("*" if r["event"] in new_events else "")
                 + _short_event(r["event"]))[:26]
        L.append(f"{_name:<27}{r['label'][:28]:<29}"
                 f"{r['mkts']:>5}{(r.get('held') or ''):>5}"
                 f"{per_s:>9}{r['earn']:>11.2f}{cred_s:>9}"
                 f"{r['pnl']:>+10.2f}{r['net']:>+10.2f}"
                 + (f"{(perf.get(r['event']) or ''):>8}" if perf else ""))
    tper = f"{t['period']:,.2f}" if t["period"] is not None else "-"
    L.append(f"{'TOTAL':<27}{'':<29}{t['mkts']:>5}{(t['held'] or ''):>5}"
             f"{tper:>9}{t['earn']:>11.2f}{t['cred']:>9,.2f}"
             f"{t['pnl']:>+10.2f}{t['net']:>+10.2f}"
             + (f"{'':>8}" if perf else ""))
    return L


_NEW_BADGE = ('<span style="background:#ffd54a;color:#333;font-size:10px;'
              'font-weight:700;padding:0 4px;border-radius:3px;'
              'margin-left:5px;vertical-align:middle">NEW</span>')


def html_table(rows, new_events=frozenset(), perf=None) -> str:
    """HTML twin of text_table (same columns, same TOTAL row, same optional
    PERF column). Since 2026-09-13 the EVENT cell is bold ONLY for a row new
    since the previous email (plus a NEW badge); the rest sit in normal weight
    so the new ones stand out."""
    t = tier_totals(rows)
    h = ['<table style="border-collapse:collapse;margin:6px 0">']
    h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
             f'<td style="{TDL}">EVENT</td><td style="{TDL}">WHAT IT IS</td>'
             f'<td style="{TD}">QUOTED</td><td style="{TD}">HELD</td>'
             f'<td style="{TD}">THIS PERIOD$</td>'
             f'<td style="{TD}">ALL-TIME EST$</td>'
             f'<td style="{TD}">CREDITED$</td>'
             f'<td style="{TD}">P&amp;L$</td><td style="{TD}">NET$</td>'
             + (f'<td style="{TD}">PERF</td>' if perf else '') + '</tr>')
    for i, r in enumerate(rows):
        bg = "#fafafa" if i % 2 else "#fff"
        held = r.get("held") or 0
        cred = r.get("cred") or 0.0
        per = r.get("period")
        per_s = f'{per:,.2f}' if per is not None else '&mdash;'
        _new = r["event"] in new_events
        _ev = (f'<b>{_short_event(r["event"])}</b>{_NEW_BADGE}' if _new
               else _short_event(r["event"]))
        h.append(f'<tr style="background:{bg}">'
                 f'<td style="{TDL}">{_ev}</td>'
                 f'<td style="{TDL}">{r["label"]}</td>'
                 f'<td style="{TD}">{r["mkts"]}</td>'
                 f'<td style="{TD};color:#888">{held or ""}</td>'
                 f'<td style="{TD}">{per_s}</td>'
                 f'<td style="{TD};color:#777">{r["earn"]:,.2f}</td>'
                 f'<td style="{TD};color:#0a7">{cred and f"{cred:,.2f}" or ""}'
                 f'</td>'
                 f'<td style="{TD}">{_pnl_span(r["pnl"])}</td>'
                 f'<td style="{TD};font-weight:700">{_pnl_span(r["net"])}</td>'
                 + (f'<td style="{TD};color:#b00">'
                    f'{perf.get(r["event"]) or ""}</td>' if perf else '')
                 + '</tr>')
    tper = f'{t["period"]:,.2f}' if t["period"] is not None else '&mdash;'
    h.append(f'<tr style="background:#f0f0f0;font-weight:700">'
             f'<td style="{TDL}">TOTAL</td><td style="{TDL}"></td>'
             f'<td style="{TD}">{t["mkts"]}</td>'
             f'<td style="{TD}">{t["held"] or ""}</td>'
             f'<td style="{TD}">{tper}</td>'
             f'<td style="{TD};color:#777">{t["earn"]:,.2f}</td>'
             f'<td style="{TD};color:#0a7">{t["cred"]:,.2f}</td>'
             f'<td style="{TD}">{_pnl_span(t["pnl"])}</td>'
             f'<td style="{TD}">{_pnl_span(t["net"])}</td>'
             + (f'<td style="{TD}"></td>' if perf else '') + '</tr>')
    h.append('</table>')
    return "".join(h)


# ---- SCAN PERF (the open-scan performance loop's scorecard) ----------------
# The loop demands extra ROI of a scan candidate whose STRUCTURE (spread, mid,
# days-to-close, pool $/day) the scorer has MEASURED as loss-making, by
# subtracting a requirement `req` from its ROI before the 5%/day admission
# bar. It can only ever subtract — `adj` is clipped at 0.0 on the upside and
# `req = clip(-WEIGHT*adj, 0, MAX_REQ) >= 0` — so there is no code path that
# promotes a market, and this block never needs to explain one.
#
# Everything printed here comes from ONE file (STATUS_DIR/scan_perf.json,
# parsed by send_imm_digest.load_scan_perf). The block is defensive at every
# level: no file prints "no table", and a field the scorer did not write
# renders blank rather than raising. The email must never fail because of
# this block — it is a scorecard bolted onto a report that already carries the
# tier's money.
#
# Two labels are load-bearing and appear on every number:
#   MEASURED  markout from the bot's own fills against the strategy doc's
#             four-tier mark hierarchy; `credited_measured` from the ledger
#   MODELLED  the rent estimate (cycle-log accrual integral, $1.00 floor) and
#             every counterfactual
# MEASURED credits get their OWN column and are never added to the MODELLED
# rent — the same rule the CUMULATIVE table below enforces for EST/CREDITED,
# and for the same reason: they are two scopes on one stream and neither
# contains the other.
_PERF_DIM_ORDER = ("spread", "mid", "dtc", "pool")


def _perf_keys_for(event_ticker: str) -> list:
    """The keys under which this event could be scored, most specific first.

    The table keys series records by SERIES and event records by EVENT ROOT,
    and the root is "the ticker minus the trailing date segment" — which for
    KXAXP-26OCTCARDS is written as the full event ticker in one place of the
    schema and as KXAXP in another. Rather than guess, the lookup tries the
    full event ticker, then the event ticker minus its last segment, then the
    series. All three are plain dict hits and the first one found wins, so
    either keying convention reports correctly and neither can silently
    report nothing — which is exactly how 30 *CC events left this email on
    2026-09-10."""
    series = event_ticker.split("-")[0]
    out = [event_ticker]
    trimmed = event_ticker.rsplit("-", 1)[0]
    if trimmed != event_ticker:
        out.append(trimmed)
    if series not in out:
        out.append(series)
    return out


def perf_record(table, event_ticker: str):
    """(key, unit, record) for an event, or (None, None, None).

    EVENT records are checked before SERIES records: a verdict on the narrower
    unit is the one that describes this row."""
    if not table:
        return None, None, None
    events = table.get("events") if isinstance(table.get("events"), dict) else {}
    series = table.get("series") if isinstance(table.get("series"), dict) else {}
    for key in _perf_keys_for(event_ticker):
        rec = events.get(key)
        if isinstance(rec, dict):
            return key, "event", rec
    for key in _perf_keys_for(event_ticker):
        rec = series.get(key)
        if isinstance(rec, dict):
            return key, "series", rec
    return None, None, None


def perf_req_of(rec, hdr) -> float:
    """The requirement in $/day per $ at risk that this record implies at the
    weight currently in force: `req = clip(-WEIGHT * dev, 0, MAX_REQ)`.

    Recomputed here rather than read from the file because the file carries
    `dev` (the MEASURED deviation) and the bot carries the WEIGHT: printing a
    req from one without the other is how a table of intentions gets read as a
    table of actions. Returns 0.0 for a missing/neutral record, and NEVER a
    negative number — the loop has no promote path and neither does its
    report."""
    if not isinstance(rec, dict):
        return 0.0
    dev = _f(rec.get("dev"))
    w = hdr.get("weight")
    w = _f(w) if w is not None else 0.0
    max_req = _f((hdr.get("max_req") if hdr.get("max_req") is not None
                  else 0.10)) or 0.10
    return max(0.0, min(-w * dev, max_req))


def perf_req_at_full_weight(rec) -> float:
    """What the req WOULD be at WEIGHT=1.0 — the observe-only readout. While
    the loop ships at weight 0 this is the only number in the row that moves,
    and hiding it would make the whole table read as zeros."""
    return perf_req_of(rec, {"weight": 1.0})


def perf_cell(rec, hdr) -> str:
    """The PERF cell on an OPEN SCAN row: "BAR", the applied req, a
    parenthesised would-be req while the loop is observe-only or the table is
    stale, or blank when the row is neutral/unscored.

    Parentheses mean NOT APPLIED. Printing "0.000" for a down-ranked row at
    weight 0 would be true and useless; printing the bare req would be a lie.
    """
    if not isinstance(rec, dict):
        return ""
    verdict = str(rec.get("verdict") or "neutral")
    if verdict == "bar" and hdr.get("bar_on") and not hdr.get("stale"):
        return "BAR"
    if verdict == "bar":
        return "(BAR)"
    if verdict == "neutral":
        return ""
    req = perf_req_of(rec, hdr)
    if req > 0 and not hdr.get("stale"):
        return f"{req:.3f}"
    return f"({perf_req_at_full_weight(rec):.3f})"


def perf_cells_for(table, rows, hdr) -> dict:
    """event -> PERF cell, for the OPEN SCAN rows only."""
    out = {}
    for r in rows:
        _k, _u, rec = perf_record(table, r["event"])
        cell = perf_cell(rec, hdr)
        if cell:
            out[r["event"]] = cell
    return out


def perf_nonneutral(table) -> list:
    """[(unit, key, record)] for every non-neutral record in the table, worst
    dev first. Neutral records are the overwhelming majority and say nothing;
    a bar or a down-rank is the whole point of the file."""
    out = []
    for unit, field in (("series", "series"), ("event", "events")):
        d = table.get(field) if isinstance(table.get(field), dict) else {}
        for key, rec in sorted(d.items()):
            if isinstance(rec, dict) and str(rec.get("verdict") or "neutral") \
                    != "neutral":
                out.append((unit, key, rec))
    out.sort(key=lambda t: (_f(t[2].get("dev")), t[1]))
    return out


def perf_verdict_map(table) -> dict:
    """{"series:KXFOO": "down_rank", ...} — the record this email persists so
    the NEXT one can bold the rows whose verdict CHANGED. Same mechanism as
    the new-event marking (opportunistic_last_sent.json), for the same reason:
    a verdict that flipped overnight is the one thing in a 40-row table that
    a human has to see."""
    return {f"{unit}:{key}": str(rec.get("verdict") or "neutral")
            for unit, key, rec in perf_nonneutral(table)}


def previous_perf_verdicts():
    """{"unit:key": verdict} from the last SENT email, or None when there is
    no record — in which case nothing is marked changed and the block says
    so, rather than marking everything."""
    rec = load_json(LAST_SENT_PATH) or {}
    pv = rec.get("perf_verdicts")
    if not isinstance(pv, dict):
        return None
    return {str(k): str(v) for k, v in pv.items()}


def _perf_rent_c_per_ct(rec) -> str:
    """MODELLED rent in cents per contract for one record, or "" when the
    scorer did not give enough to derive it. Never invented from a
    denominator that is not in the file."""
    if rec.get("rent_cents_per_contract") is not None:
        return f"{_f(rec.get('rent_cents_per_contract')):.2f}"
    ct = _f(rec.get("contracts_matured")) or _f(rec.get("contracts_filled"))
    if ct <= 0:
        return ""
    return f"{_f(rec.get('rent_modelled')) * 100.0 / ct:.2f}"


def _perf_num(v, fmt: str) -> str:
    """Blank for a field the scorer did not write; formatted otherwise. A
    missing number must never print as 0.00 — that is a measurement claim."""
    if v is None:
        return ""
    try:
        return format(float(v), fmt)
    except (TypeError, ValueError):
        return ""


def perf_unmarked_frac(table):
    """The share of fills the mark hierarchy could not mark. Above 0.35 the
    scorer forces a group neutral, so a drift toward it is visible BEFORE it
    changes a verdict."""
    cov = table.get("coverage") if isinstance(table.get("coverage"), dict) else {}
    tier = table.get("tier") if isinstance(table.get("tier"), dict) else {}
    if tier.get("unmarked_frac") is not None:
        return _f(tier.get("unmarked_frac"))
    fills = _f(cov.get("fills"))
    if fills <= 0:
        return None
    return _f(cov.get("fills_unmarked")) / fills


def scan_perf_text_block(table, hdr, cost, prev_verdicts=None) -> list:
    """The SCAN PERF block, plain text. Returns lines; never raises."""
    L = ["SCAN PERF — open-scan performance loop (markout MEASURED, rent "
         "MODELLED)"]
    if not table:
        L.append(f"no table — {SCAN_PERF_PATH} is absent or unreadable, so "
                 f"every scan candidate ranks on gross ROI alone (today's "
                 f"behaviour).")
        L.append(scan_perf_cost_line(cost))
        return L
    age = (f"{hdr['age_h']:.1f}h old" if hdr["age_h"] is not None
           else "age unknown")
    w = hdr["weight"] if hdr["weight"] is not None else "?"
    b = ("on" if hdr["bar_on"] else "off") if hdr["bar_on"] is not None else "?"
    L.append(f"table generated {hdr['generated_at']} ({age})  |  WEIGHT={w}  "
             f"|  BAR {b}   [weight/bar read from the {hdr['source']}]")
    if hdr["stale"]:
        L.append(f"STALE — verdicts not applied. Above "
                 f"{SCAN_PERF_MAX_AGE_H:.0f}h the bot clears its tables and "
                 f"every verdict below goes neutral; this is a picture of an "
                 f"old policy, not the live one.")
    tier = table.get("tier") if isinstance(table.get("tier"), dict) else {}
    cov = table.get("coverage") if isinstance(table.get("coverage"), dict) else {}
    L.append("coverage {} mkts / {} events / {} series | {} fills -> {} "
             "episodes ({} matured) | risk-days {}".format(
                 _perf_num(cov.get("markets"), ",.0f") or "?",
                 _perf_num(cov.get("events"), ",.0f") or "?",
                 _perf_num(cov.get("series"), ",.0f") or "?",
                 _perf_num(cov.get("fills"), ",.0f") or "?",
                 _perf_num(cov.get("episodes"), ",.0f") or "?",
                 _perf_num(cov.get("episodes_matured"), ",.0f") or "?",
                 _perf_num(cov.get("risk_days"), ",.1f") or "?"))
    L.append("tier markout {} c/ct [MEASURED] | rent {} [MODELLED, basis {}] "
             "| mu_trading_only {} | mu_rent_blended {}".format(
                 _perf_num(tier.get("markout_c_per_ct_24h"), "+.2f") or "?",
                 "$" + (_perf_num(tier.get("rent_modelled_dollars"), ",.2f")
                        or "?"),
                 tier.get("rent_basis") or "?",
                 _perf_num(tier.get("mu_trading_only"), "+.5f") or "?",
                 _perf_num(tier.get("mu_rent_blended"), "+.5f") or "?"))
    L.append("MEASURED credits ${} on {} of {} events — its OWN column, never "
             "added to the MODELLED rent above (two scopes on one stream, "
             "neither contains the other).".format(
                 _perf_num(tier.get("credited_measured_dollars"), ",.2f")
                 or "0.00",
                 _perf_num(cov.get("credited_events"), ",.0f") or "0",
                 _perf_num(cov.get("events"), ",.0f") or "?"))
    mix = tier.get("mark_source_mix") if isinstance(
        tier.get("mark_source_mix"), dict) else {}
    uf = perf_unmarked_frac(table)
    L.append("marks: " + (", ".join(
        f"{k} {_f(v):.2f}" for k, v in sorted(mix.items())) or "not reported")
        + ("  |  unmarked {:.2f}".format(uf) if uf is not None
           else "  |  unmarked n/a")
        + "  (a drift toward one-sided/settlement marks shows up here BEFORE "
          "it changes a verdict; above 0.35 unmarked a group is forced "
          "neutral)")

    # ---- cohorts, BOTH bases, acting column marked ------------------------
    basis = hdr.get("basis") or "trading"
    acting = "trading" if basis != "blend" else "blend"
    cohorts = table.get("cohorts") if isinstance(table.get("cohorts"), dict) else {}
    L.append("")
    L.append("COHORTS — both bases; '*' marks the ACTING column "
             f"(score_basis={basis}). dev is MEASURED deviation from the tier "
             f"centre, in $/day per $ at risk.")
    L.append("{:<7}{:<10}{:>6}{:>5}{:>5}{:>11}{:>10}{:>10}{:>10}{:>10}  {}"
             .format("DIM", "BUCKET", "MKTS", "SER", "EVT", "$-DAYS",
                     "RAW_TR" + ("*" if acting == "trading" else ""),
                     "DEV_TR" + ("*" if acting == "trading" else ""),
                     "RAW_BL" + ("*" if acting == "blend" else ""),
                     "DEV_BL" + ("*" if acting == "blend" else ""), "MUTED"))
    dims = [d for d in _PERF_DIM_ORDER if d in cohorts]
    dims += [d for d in sorted(cohorts) if d not in _PERF_DIM_ORDER]
    any_bucket = False
    for dim in dims:
        c = cohorts.get(dim) or {}
        for bkt in (c.get("buckets") or []):
            if not isinstance(bkt, dict):
                continue
            any_bucket = True
            L.append("{:<7}{:<10}{:>6}{:>5}{:>5}{:>11}{:>10}{:>10}{:>10}{:>10}"
                     "  {}".format(
                         dim[:6], str(bkt.get("key") or "")[:9],
                         _perf_num(bkt.get("n_markets"), ",.0f"),
                         _perf_num(bkt.get("n_series"), ",.0f"),
                         _perf_num(bkt.get("n_events"), ",.0f"),
                         _perf_num(bkt.get("risk_days"), ",.1f"),
                         _perf_num(bkt.get("raw_trading"), "+.4f"),
                         _perf_num(bkt.get("dev_trading"), "+.4f"),
                         _perf_num(bkt.get("raw_blend"), "+.4f"),
                         _perf_num(bkt.get("dev_blend"), "+.4f"),
                         "muted" if bkt.get("muted") else ""))
    if not any_bucket:
        L.append("  (no cohort buckets in the table)")

    # ---- non-neutral keys --------------------------------------------------
    nn = perf_nonneutral(table)
    L.append("")
    if not nn:
        L.append("NON-NEUTRAL KEYS: none — every scored series and event root "
                 "is neutral in this table.")
    else:
        L.append("NON-NEUTRAL KEYS — '!' marks a verdict that CHANGED since "
                 "the previous email. REQ is at the weight in force; a "
                 "parenthesised value is what it WOULD be at WEIGHT=1.0 and "
                 "is not applied. CRED$ is MEASURED money and is NOT part of "
                 "RENT.")
        L.append("{:<26}{:<8}{:>4}{:>6}{:>9}{:>10}{:>10}{:>10}  {:<10}{:<12}"
                 "{:>9}".format("KEY", "UNIT", "EP", "MKTS", "MO24c",
                                "RENTc/ct", "DEV", "REQ", "VERDICT", "UNTIL",
                                "CRED$"))
        for unit, key, rec in nn:
            vk = f"{unit}:{key}"
            changed = (prev_verdicts is not None
                       and prev_verdicts.get(vk)
                       != str(rec.get("verdict") or "neutral"))
            req = perf_req_of(rec, hdr)
            req_s = (f"{req:.4f}" if req > 0 and not hdr.get("stale")
                     else f"({perf_req_at_full_weight(rec):.4f})")
            L.append("{:<26}{:<8}{:>4}{:>6}{:>9}{:>10}{:>10}{:>10}  {:<10}"
                     "{:<12}{:>9}".format(
                         (("!" if changed else "") + key)[:25],
                         unit[:7],
                         _perf_num(rec.get("n_episodes"), ",.0f"),
                         _perf_num(rec.get("n_markets"), ",.0f"),
                         _perf_num(rec.get("markout_c_per_ct_24h"), "+.2f"),
                         _perf_rent_c_per_ct(rec),
                         _perf_num(rec.get("dev"), "+.4f"),
                         req_s,
                         str(rec.get("verdict") or "")[:9],
                         str(rec.get("until") or "")[:11],
                         _perf_num(rec.get("credited_measured"), ",.2f")))
    if prev_verdicts is None:
        L.append("  (no previous SCAN PERF record on file, so no verdict is "
                 "marked changed this time)")

    # ---- bars --------------------------------------------------------------
    bars = [(u, k, r) for u, k, r in nn
            if str(r.get("verdict") or "") == "bar"]
    L.append("")
    if bars:
        L.append("ACTIVE BARS ({}), BAR {}:".format(len(bars), b))
        for unit, key, rec in bars:
            L.append(f"  {unit}:{key} until {rec.get('until') or '?'}")
    else:
        L.append("bars: 0 live. The bar fires on nothing in this table"
                 + (" and BAR is off." if b == "off" else "."))
    unmatched = table.get("unmatched_bar_keys") or []
    L.append("unmatched bar keys (a bar whose group produced no scan "
             "candidate in 24h — a dead bar, visible rather than silently "
             "mis-firing): "
             + (", ".join(str(x) for x in unmatched[:12]) if unmatched
                else "none"))

    # ---- the cost line -----------------------------------------------------
    L.append("")
    L.append(scan_perf_cost_line(cost))
    L.append(scan_perf_cost_basis_note(cost))
    for wmsg in (table.get("warnings") or [])[:6]:
        L.append(f"warning: {wmsg}")
    return L


def scan_perf_html_block(table, hdr, cost, prev_verdicts=None) -> str:
    """HTML twin of scan_perf_text_block. Same facts, same order, same
    labels — the two renderers are kept side by side on purpose so a number
    can never appear in one and not the other."""
    h = ['<div style="font-size:15px;font-weight:600;margin:18px 0 2px">'
         'Scan perf <span style="color:#888;font-weight:400">&mdash; '
         'open-scan performance loop (markout MEASURED, rent MODELLED)'
         '</span></div>']
    if not table:
        h.append('<div style="color:#666;font-size:13px">no table &mdash; '
                 '<code>{}</code> is absent or unreadable, so every scan '
                 'candidate ranks on gross ROI alone (today&rsquo;s '
                 'behaviour).</div>'.format(SCAN_PERF_PATH))
        h.append('<div style="color:#555;font-size:13px;margin-top:4px">'
                 '{}</div>'.format(scan_perf_cost_line(cost)))
        return "".join(h)
    age = (f"{hdr['age_h']:.1f}h old" if hdr["age_h"] is not None
           else "age unknown")
    w = hdr["weight"] if hdr["weight"] is not None else "?"
    b = ("on" if hdr["bar_on"] else "off") if hdr["bar_on"] is not None else "?"
    h.append('<div style="color:#555;font-size:13px">table generated '
             '<b>{}</b> ({}) &nbsp;&middot;&nbsp; <b>WEIGHT={}</b> '
             '&nbsp;&middot;&nbsp; <b>BAR {}</b> <span style="color:#999">'
             '(weight/bar read from the {})</span></div>'.format(
                 hdr["generated_at"], age, w, b, hdr["source"]))
    if hdr["stale"]:
        h.append('<div style="color:#b00;font-size:13px;font-weight:700">'
                 'STALE &mdash; verdicts not applied. Above {:.0f}h the bot '
                 'clears its tables and every verdict below goes neutral.'
                 '</div>'.format(SCAN_PERF_MAX_AGE_H))
    # The header lines above are already rendered as HTML; skip exactly those
    # from the text twin (title, generated line, and the STALE line when it is
    # present) so nothing is printed twice.
    skip = 2 + (1 if hdr["stale"] else 0)
    for line in scan_perf_text_block(table, hdr, cost, prev_verdicts)[skip:]:
        if not line.strip():
            continue
        esc = (line.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
        # a verdict that CHANGED since the previous email carries the '!'
        # marker in its first column; the HTML twin bolds the whole row
        if line.startswith("!"):
            esc = f"<b>{esc}</b>"
        h.append('<div style="font-family:Consolas,monospace;font-size:11px;'
                 'color:#333;white-space:pre">{}</div>'.format(esc))
    return "".join(h)


# ---- the cumulative table --------------------------------------------------
# Same twin-renderer discipline as the active tables above: one row builder,
# a text renderer and an HTML renderer that must stay identical in shape.
# Columns and their bases are the docstring's contract — CREDITED is actual
# money, EST is the live accrual, and NEITHER contains the other (accrual is
# deleted and restarts when a market goes unquoted-and-flat), so nothing here
# ever adds the two into one figure.
_CUM_COLS = ("EVENTS", "CREDITED$", "EST$", "REALIZED$", "MTM$", "NET$")


def cum_text_table(rows) -> list:
    L = [f"{'TIER':<16}{'EVENTS':>7}{'CREDITED$':>11}{'EST$':>10}"
         f"{'REALIZED$':>11}{'MTM$':>10}{'NET$':>10}"]
    for r in rows:
        L.append(f"{r['tier'][:15]:<16}{r['events']:>7}{r['cred']:>11.2f}"
                 f"{r['est']:>10.2f}{r['realized']:>+11.2f}{r['mtm']:>+10.2f}"
                 f"{r['net']:>+10.2f}")
    return L


def cum_html_table(rows) -> str:
    h = ['<table style="border-collapse:collapse;margin:6px 0">']
    h.append('<tr style="background:#f0f0f0;font-weight:600">'
             f'<td style="{TDL}">TIER</td>'
             + "".join(f'<td style="{TD}">{c}</td>' for c in _CUM_COLS)
             + '</tr>')
    for i, r in enumerate(rows):
        last = i == len(rows) - 1
        bg = "#f0f0f0" if last else ("#fafafa" if i % 2 else "#fff")
        w = ";font-weight:700" if last else ""
        h.append(f'<tr style="background:{bg}{w}">'
                 f'<td style="{TDL}{w}">{r["tier"]}</td>'
                 f'<td style="{TD}">{r["events"]}</td>'
                 f'<td style="{TD};color:#0a7;font-weight:700">'
                 f'{r["cred"]:,.2f}</td>'
                 f'<td style="{TD}">{r["est"]:,.2f}</td>'
                 f'<td style="{TD}">{_pnl_span(r["realized"])}</td>'
                 f'<td style="{TD}">{_pnl_span(r["mtm"])}</td>'
                 f'<td style="{TD};font-weight:700">{_pnl_span(r["net"])}</td>'
                 f'</tr>')
    h.append('</table>')
    return "".join(h)


def build_report(now_utc):
    """(text, html, subject)."""
    client = sd.build_client()
    # Pick up task-appended Carbon Arc self-extensions so the opportunistic
    # universe here matches what the live bot quotes.
    imm.load_finecon_extra_series()
    fin = imm.FINECON_SERIES

    status = load_json(STATUS_PATH)
    state = load_json(STATE_PATH)
    ss = status_summary(status)
    our_ids = set(state.get("our_order_ids") or {})

    fills = fetch_own_fills(client, our_ids, FILL_LOOKBACK_HOURS)
    pos, avg = own_book(state)
    touched = {f.get("ticker", "") for f in fills} | set(pos)
    mids, results = current_mids(client, touched)
    # pnl_windows keeps this email's basis identical to the digest's; its
    # per-event past-day window is NO LONGER the row P&L's realized term
    # (see realized_for) but the call stays, so the two reports still walk
    # the same validated path.
    w = pnl_windows(client, state, our_ids, fills, mids, results,
                    ss["reward_lifetime"])
    day_ev = w["day"]["events"]
    try:
        acct_real, acct_pos = account_realized(client)
    except Exception as e:                                  # noqa: BLE001
        log(f"opportunistic account realized unavailable ({e!r}); realized "
            f"falls back to the sink alone")
        acct_real, acct_pos = {}, {}

    selected = set(state.get("selected_tickers") or [])
    accrued = state.get("accrued_est") or {}
    # Open-scan tier: membership is persisted by the bot (scan_members);
    # scan_book adds former members still carrying our inventory, so their
    # credits and P&L stay attributed to the opportunistic book.
    scan_members = set(state.get("scan_members") or [])
    scan_book = set(state.get("scan_book") or []) | scan_members
    scan_events = {_event_of(t) for t in scan_book}

    # Durable per-event history for the cumulative table (see the module
    # docstring): the scan tier's lifetime roster and per-event realized.
    # Never fatal: build_report is retried 8x/5min and a failure here costs
    # the whole day's email. Degrading to the live sets loses the departed
    # events from the cumulative table, which the footer's as-of line makes
    # visible — silently sending nothing would not.
    try:
        hist = durable_history(now_utc.strftime("%Y-%m-%d"))
    except Exception as e:                                  # noqa: BLE001
        log(f"opportunistic durable history unavailable ({e!r}); cumulative "
            f"falls back to the live book")
        hist = {"scan_events": set(), "realized": {}, "first_day": ""}
    scan_ever = scan_events | hist["scan_events"]

    def _tier(t):
        """Ticker -> tier for the BOOK. scan_book, not scan_members: a market
        the scan tier stopped quoting but still holds inventory in is exactly
        what the 2026-09-07 "scope to the tier's book" rule is about, and
        scan_members alone would drop it (scan_book = members + everything
        still carrying our inventory). The SLOT count below stays on true
        membership so the tier line cannot inflate."""
        return tier_of(t, fin, scan_book)

    def _event_tier(ev):
        return tier_of(ev, fin, scan_ever)

    def _is_opp_event(ev):
        return _event_tier(ev) is not None

    members = [t for t in selected if _tier(t)]
    fin_members = [t for t in members if _tier(t) == "finecon"]
    fam_members = [t for t in members if _tier(t) == "family"]
    scan_sel = [t for t in members if t in scan_members]
    # THE TIER'S BOOK, not just what it is quoting right now (Jack
    # 2026-09-07: "scope the email's P&L to every market in the tier's book
    # rather than just the selected ones"). A market the bot has stopped
    # quoting keeps its inventory and its period-to-date accrual, and the
    # selected-only sum silently dropped both: KXSBUXCC-26OCT07 reported
    # P&L $0.00 on 9/6 while the three unselected strikes it still held
    # (-50/-52/-50) marked to -$32.72, which is what Kalshi showed. So the
    # row set is every market of the tier we hold inventory in, or have
    # accrued reward on, in ADDITION to the selected ones. `mkts` still
    # counts what is being QUOTED — that is the slot number the tier lines
    # above report — and held-but-unquoted markets are counted separately.
    held = [t for t in pos if _tier(t) and abs(_f(pos.get(t))) > 1e-9]
    earned = [t for t in accrued if _tier(t) and _f(accrued.get(t)) > 0]
    book = sorted(set(members) | set(held) | set(earned))
    quoted = set(members)
    by_event: dict = {}
    for t in book:
        by_event.setdefault(_event_of(t), []).append(t)

    # Actual Kalshi money, per event, all-time. Read BEFORE the row loop so
    # each active row can carry its own credited-to-date figure alongside the
    # estimate (Jack 2026-09-11 "not just active, but also cumulative").
    ledger, _calib = load_credit_ledger()
    cred_by_event: dict = {}
    for _d, _ev, _a in ledger:
        cred_by_event[_ev] = cred_by_event.get(_ev, 0.0) + _a

    realized_by_ticker = hist["realized"]
    # Per-program-period accrual (Jack 2026-09-11). The bot records what
    # accrued_est read when each market's CURRENT program period opened;
    # `accrued - base` is this period's earning, the quantity the exchange's
    # own rewards view shows. A market with no recorded baseline has not been
    # seen roll yet — its period is unmeasurable, NOT zero, and the row
    # renders "-". An event is measurable only if EVERY market in it is, so a
    # partial sum can never masquerade as the event's period total.
    period_base = state.get("period_base") or {}

    def _period_earn(tickers):
        vals = []
        for t in tickers:
            if t not in period_base:
                return None
            vals.append(_f(accrued.get(t)) - _f(period_base.get(t)))
        return sum(vals) if vals else None

    rows = []
    n_disputed = 0
    for ev, tickers in by_event.items():
        earn = sum(_f(accrued.get(t)) for t in tickers)
        # P&L per event: open-book MTM on held inventory (this book almost
        # never sells, so MTM is the P&L; the sum over all held markets
        # equals the digest's lifetime unrealized) + any realized/settlement
        # booked for the event in the past-day window.
        mtm = 0.0
        for t in tickers:
            p = _f(pos.get(t))
            m = mids.get(t)
            if abs(p) > 1e-9 and m is not None:
                mtm += p * (m - _f(avg.get(t))) / 100.0
        # REALIZED IS LIFETIME (Jack 2026-09-11: "P&L of $1.30 is wrong. I
        # see P&L of -$5"). It used to be the past-DAY window only -- the
        # footer even called it 168h, which was wrong twice over: the window
        # is 86400s, and a book that holds multi-week inventory realises most
        # of its P&L outside ANY fill window. KXAAAGASMINM-26SEP30 printed
        # +1.30 (pure MTM) while Kalshi's own realized on the same two
        # markets was -5.20, including -4.00 on a strike the bot had already
        # closed and which therefore contributed nothing at all.
        rlz, disp = realized_for(tickers, acct_real, acct_pos, pos,
                                 realized_by_ticker)
        n_disputed += disp
        pnl = mtm + rlz
        netpos = sum(_f(pos.get(t)) for t in tickers)
        n_quoted = sum(1 for t in tickers if t in quoted)
        rows.append({
            "event": ev, "label": event_label(client, ev),
            # from the EVENT, not tickers[0]: an event whose first ticker
            # has left every live set still belongs to the tier that quoted
            # it, and an untiered row renders in NO table at all — the same
            # silent drop this change exists to fix.
            "tier": _event_tier(ev) or _tier(tickers[0]) or "",
            "mkts": n_quoted, "held": len(tickers) - n_quoted,
            "earn": earn, "period": _period_earn(tickers),
            "cred": cred_by_event.get(ev, 0.0), "pnl": pnl,
            "net": pnl + earn, "pos": netpos})
    rows.sort(key=lambda r: -r["net"])
    fin_rows = [r for r in rows if r["tier"] == "finecon"]
    fam_rows = [r for r in rows if r["tier"] == "family"]
    scan_rows = [r for r in rows if r["tier"] == "scan"]

    tot = tier_totals(rows)
    tot_earn, tot_pnl, tot_net, tot_mkts = (tot["earn"], tot["pnl"],
                                            tot["net"], tot["mkts"])
    tot_held = tot["held"]
    cred_life = sum(a for _d, ev, a in ledger if _is_opp_event(ev))

    # ---- CUMULATIVE: every event each tier ever touched ---------------------
    # The union is deliberately wider than the active book — an event that
    # settled and left state entirely still belongs to the tier that earned
    # its credits. EST comes from the live accrual (so it only exists while
    # the market does); CREDITED and REALIZED are history.
    cum_mtm: dict = {}
    for t, pv in pos.items():
        m = mids.get(t)
        if abs(_f(pv)) > 1e-9 and m is not None:
            ev = _event_of(t)
            cum_mtm[ev] = cum_mtm.get(ev, 0.0) + _f(pv) * (m - _f(avg.get(t))) / 100.0
    cum_est: dict = {}
    for t, av in accrued.items():
        if _f(av) > 0:
            ev = _event_of(t)
            cum_est[ev] = cum_est.get(ev, 0.0) + _f(av)
    # Cumulative REALIZED unions the same two sources per market, so it can
    # never disagree with the rows above it.
    realized_by_event: dict = {}
    for _t in set(realized_by_ticker) | set(acct_real):
        _v, _ = realized_for([_t], acct_real, acct_pos, pos,
                             realized_by_ticker)
        if abs(_v) > 1e-9:
            _e = _event_of(_t)
            realized_by_event[_e] = realized_by_event.get(_e, 0.0) + _v
    cum_universe = (set(cred_by_event) | set(realized_by_event)
                    | set(cum_mtm) | set(cum_est) | {r["event"] for r in rows})
    cum_by_tier: dict = {}
    for ev in cum_universe:
        tier = _event_tier(ev)
        if not tier:
            continue
        b = cum_by_tier.setdefault(tier, {"events": 0, "cred": 0.0, "est": 0.0,
                                          "realized": 0.0, "mtm": 0.0})
        b["events"] += 1
        b["cred"] += cred_by_event.get(ev, 0.0)
        b["est"] += cum_est.get(ev, 0.0)
        b["realized"] += realized_by_event.get(ev, 0.0)
        b["mtm"] += cum_mtm.get(ev, 0.0)
    cum_rows = []
    for key, name in (("finecon", "FINECON"), ("scan", "OPEN SCAN"),
                      ("family", "CARBON ARC *CC")):
        b = cum_by_tier.get(key)
        if not b:
            continue
        # NET is EST + trading, NOT CREDITED + EST + trading: accrued_est is
        # never reset at a period end, so credits already paid are inside EST
        # (KXAMZNCC 2026-09-11: est 8.70, of which 3.79 already credited).
        # Same basis as the active tables' NET, so the two agree.
        cum_rows.append({"tier": name, **b,
                         "net": b["est"] + b["realized"] + b["mtm"]})
    if cum_rows:
        tot_row = {"tier": "TOTAL", "events": sum(r["events"] for r in cum_rows)}
        for k in ("cred", "est", "realized", "mtm", "net"):
            tot_row[k] = sum(r[k] for r in cum_rows)
        cum_rows.append(tot_row)
    cum_since = hist["first_day"] or "—"
    today_et = now_utc.astimezone(imm.ET).date()
    # AS-OF STAMP for the credited columns. reward_credits.csv is a hand-run
    # statement paste (imm_reward_recon.py --statement; no scheduled task), so
    # it runs days behind by construction. Without this stamp a CREDITED$ of
    # 0.00 is indistinguishable from "this event earned nothing" when it
    # really means "the period has not paid, or the ledger has not been
    # pasted". The digest guards the same trap with LEDGER_STALE_DAYS.
    cred_through = max((d for d, _e, _a in ledger), default="")
    try:
        cred_lag = (today_et - datetime.strptime(cred_through,
                                                 "%Y-%m-%d").date()).days
    except ValueError:
        cred_lag = None
    cred_asof = (f"ledger through {cred_through}"
                 + (f", {cred_lag}d behind" if cred_lag else "")
                 if cred_through else "no reward ledger on disk")
    cred_stale = cred_lag is not None and cred_lag > getattr(
        sd, "LEDGER_STALE_DAYS", 4)

    top_n = getattr(imm, "FINECON_TOP_N", 0)
    openings_cap = getattr(imm, "FINECON_DAILY_OPENINGS", 0)
    used = (int(_f(state.get("finecon_admits_today")))
            if state.get("finecon_admit_day") == today_et.isoformat() else 0)
    scan_top_n = getattr(imm, "SCAN_TOP_N", 0)
    scan_openings_cap = getattr(imm, "SCAN_DAILY_OPENINGS", 0)
    scan_used = (int(_f(state.get("scan_admits_today")))
                 if state.get("scan_admit_day") == today_et.isoformat() else 0)
    scan_halted = state.get("scan_halt_day") == today_et.isoformat()
    scan_evicted = len(state.get("scan_evicted_events") or {})

    # ---- the open-scan performance loop ------------------------------------
    # Whole block is best-effort. A scorecard bolted onto this report must
    # never be the reason the report does not go out, so every failure here
    # degrades to "no table" and the rest of the email is byte-identical.
    try:
        perf_table = load_scan_perf()
        perf_hdr = scan_perf_header(perf_table, status, now_utc)
        perf_cost = scan_perf_cost(now_utc)
    except Exception as e:                                  # noqa: BLE001
        log(f"opportunistic scan-perf block unavailable ({e!r}); the SCAN "
            f"PERF section will print 'no table'")
        perf_table, perf_cost = {}, {"window_h": 24, "seats_empty": 0,
                                     "barred": 0, "floored_out": 0,
                                     "forgone_dollars_per_day": 0.0,
                                     "seats_cap": getattr(imm, "SCAN_TOP_N", 0)}
        perf_hdr = {"present": False, "generated_at": "", "age_h": None,
                    "stale": False, "weight": None, "bar_on": None,
                    "source": "unknown", "basis": "", "max_req": None}
    perf_cells = perf_cells_for(perf_table, scan_rows, perf_hdr)
    perf_verdicts = perf_verdict_map(perf_table)
    prev_perf = previous_perf_verdicts()
    # One section per tier, same table format (Jack 2026-09-06).
    tiers = [
        ("FINECON", "curated Finance/Economics quiet prints",
         f"{len(fin_members)}/{top_n} slots, +{used}/{openings_cap} daily "
         f"openings used", fin_rows, None),
        ("OPEN SCAN", "all other markets, machine-screened for adverse "
         "selection",
         f"{len(scan_sel)}/{scan_top_n} slots, "
         + (f"+{scan_used}/{scan_openings_cap} daily openings used"
            if scan_openings_cap > 0
            else f"hard cap {scan_top_n}, no daily openings")
         + (", HALTED today (loss budget)" if scan_halted else "")
         + (f", {scan_evicted} event(s) evicted" if scan_evicted else ""),
         # PERF is the OPEN SCAN tier's column only — the loop is scoped on
         # m.scan and nothing it does touches finecon or the *CC family
         scan_rows, perf_cells),
        # No tier slot budget to report: the family is quoted by the NORMAL
        # book, so its only caps are the per-event ROI cut and the global
        # event ceiling. Saying "N/M slots" here would invent a budget that
        # does not exist, so the line states the caps that DO bind.
        (f"CARBON ARC *{_family_suffix()}",
         "credit-card spend monthlies — quoted by the normal book, "
         "reported here",
         f"{len(fam_rows)} event(s) / {len(fam_members)} markets; no tier "
         f"slot cap — max {_family_event_top_n()} markets per event by ROI, "
         f"inside the global {getattr(imm, 'MAX_MARKETS', 0)}-event ceiling",
         fam_rows, None),
    ]

    # Deliberately does NOT put the estimate and the credited figure side by
    # side as if they were addends — they are two scopes on one stream (see
    # the footer). The estimate and the net it feeds lead; credited money is
    # its own clause.
    subject = (f"Opportunistic IMM {today_et} — est ${tot_earn:,.0f} accrued, "
               f"net ${tot_net:+,.0f} (${cred_life:,.0f} paid to date)")

    # ---- new since the previous email (Jack 2026-09-13) ---------------------
    prev_at, prev_short, prev_src = previous_email_events()
    if prev_short is None:
        new_events = set()
        new_note = ("no previous email on record, so nothing is marked new "
                    "this time")
    else:
        new_events = {r["event"] for r in rows
                      if _short_event(r["event"]) not in prev_short}
        _when = (prev_at or "?")[:16].replace("T", " ")
        new_note = (f"{len(new_events)} new event(s) since the previous email "
                    f"(sent {_when}Z" + (", recovered from the task log"
                                         if prev_src == "log" else "") + ")")
    new_list = sorted(_short_event(e) for e in new_events)
    if len(new_list) > 12:
        new_list = new_list[:12] + [f"+{len(new_events) - 12} more"]

    # ---- plain text ---------------------------------------------------------
    L = [f"Opportunistic IMM — {today_et}", ""]
    L.append(f"ACTIVE — {len(rows)} events / {tot_mkts} markets quoted "
             f"across {len(tiers)} tiers"
             + (f", plus {tot_held} held but no longer quoted." if tot_held
                else "."))
    L.append(f"Est reward accrued ${tot_earn:,.2f}  |  trading P&L "
             f"${tot_pnl:+,.2f}  |  net ${tot_net:+,.2f}.")
    L.append(f"NEW: {new_note}"
             + (": " + ", ".join(new_list) if new_list else "")
             + ". '*' marks a new row below.")
    L.append("")
    for name, desc, slots, trows, tperf in tiers:
        L.append(f"{name} — {desc}")
        L.append(f"{slots}.")
        if trows:
            L.extend(text_table(trows, new_events, tperf))
        else:
            L.append(f"No {name.lower()} events quoted right now.")
        L.append("")
    if cum_rows:
        L.append("CUMULATIVE — CREDITED and REALIZED cover every event each "
                 "tier has ever touched, settled and gone included; EST and "
                 "MTM can only see the live book")
        L.extend(cum_text_table(cum_rows))
        L.append("")
    L.extend(scan_perf_text_block(perf_table, perf_hdr, perf_cost, prev_perf))
    L.append("")
    L.append(f"Kalshi-credited on opportunistic events to date: "
             f"${cred_life:,.2f} (actual money; lands 1-2d after each period "
             f"ends; {cred_asof})."
             + ("  LEDGER STALE — paste a fresh statement through "
                "imm_reward_recon.py --statement; every CREDITED figure "
                "above is a floor until then." if cred_stale else ""))
    L.append("")
    L.append("QUOTED = markets the tier is quoting now; HELD = markets it "
             "stopped quoting but still holds inventory in (or accrued on) "
             "- their P&L and accrual ARE in the row. "
             "EARN EST = the bot's accrual estimator over the markets it "
             "is tracking NOW. CREDITED = actual Kalshi money on the event, "
             "all-time, every market. Same earnings, different scope - "
             "NEITHER CONTAINS THE OTHER, so never add them. EARN EST is "
             "usually the larger (the accrual is not reset at a period end, "
             "so it is credited-plus-in-flight), but it is DELETED and "
             "restarts from zero whenever a market goes unquoted and flat, "
             "and it cannot see markets that have left the book at all - "
             "which is why some rows show CREDITED above EARN EST. EARN EST "
             "also spans EVERY program period since the bot first quoted the "
             "market, not just the current one: these programs re-list weekly "
             "(KXAAAGASMINM-26SEP30 ran 9/02-9/09 and again 9/09-9/16), so "
             "the exchange's own rewards view - which shows the CURRENT "
             "period - reads far lower than this column. P&L = "
             "trading only: LIFETIME realized (Kalshi's own per-market "
             "realized_pnl_dollars where the bot's position matches the "
             "account's, the bot's realized sink otherwise) plus open-book "
             "MTM at the mid. Until 2026-09-11 it was the past 24 HOURS of "
             "realized, which left weeks of a multi-week book's trading out "
             "of the column. A row covers the event's LIVE book, so a strike "
             "that has already settled sits in the CUMULATIVE table below, "
             "not here. NET = P&L + EARN EST, so on the rows where CREDITED "
             "exceeds EARN EST, NET understates.")
    L.append(f"In the CUMULATIVE table REALIZED is the bot's own per-market "
             f"realized trading P&L, which only starts {cum_since} (the "
             f"first analytics-sink day) - anything earlier is not in it. "
             f"MTM is the open book right now. NET = EST + REALIZED + MTM, "
             f"the same estimate basis as the tables above.")
    text = "\n".join(L)

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;'
         'color:#222">']
    h.append(f'<div style="font-size:17px;font-weight:600">Opportunistic IMM'
             f' <span style="color:#888;font-weight:400">— {today_et}</span>'
             f'</div>')
    h.append(f'<div style="font-size:24px;font-weight:800;margin:8px 0 2px">'
             f'Net {_pnl_span(tot_net)}'
             f'<span style="font-size:13px;font-weight:400;color:#999">'
             f' &nbsp;= trading {tot_pnl:+,.2f} + est reward {tot_earn:,.2f}'
             f'</span></div>')
    h.append(f'<div style="color:#555;margin-bottom:6px">'
             f'ACTIVE &nbsp;·&nbsp; <b>{len(rows)}</b> events &nbsp;·&nbsp; '
             f'<b>{tot_mkts}</b> markets quoted across {len(tiers)} tiers'
             + (f' &nbsp;·&nbsp; <b>{tot_held}</b> held, no longer quoted'
                if tot_held else '')
             + (f' &nbsp;·&nbsp; <b style="color:#0a7">${cred_life:,.2f}</b>'
                f' credited to date' if cred_life else '') + '</div>')
    h.append(f'<div style="color:#555;font-size:13px;margin-bottom:6px">'
             f'{_NEW_BADGE} {new_note}'
             + (': <b>' + '</b>, <b>'.join(new_list) + '</b>' if new_list
                else '')
             + '. Bold rows below are new since the previous email.</div>')
    for name, desc, slots, trows, tperf in tiers:
        h.append(f'<div style="font-size:15px;font-weight:600;margin:14px 0 2px">'
                 f'{name} <span style="color:#888;font-weight:400">&mdash; '
                 f'{desc}</span></div>')
        h.append(f'<div style="color:#555;font-size:13px;margin-bottom:4px">'
                 f'{slots}</div>')
        if trows:
            h.append(html_table(trows, new_events, tperf))
        else:
            h.append(f'<div style="color:#666;font-size:13px">No '
                     f'{name.lower()} events quoted right now.</div>')
    if cum_rows:
        h.append('<div style="font-size:15px;font-weight:600;margin:18px 0 2px">'
                 'Cumulative <span style="color:#888;font-weight:400">&mdash; '
                 'CREDITED and REALIZED cover every event each tier has ever '
                 'touched, settled and gone included; EST and MTM can only '
                 'see the live book</span></div>')
        h.append(cum_html_table(cum_rows))
    h.append(scan_perf_html_block(perf_table, perf_hdr, perf_cost, prev_perf))
    h.append(f'<div style="color:#555;font-size:13px;margin-top:12px">'
             f'Kalshi-credited on opportunistic events to date: '
             f'<b>${cred_life:,.2f}</b> <span style="color:#999">(actual '
             f'money; lands 1&ndash;2d after each period ends; '
             f'{cred_asof})</span></div>')
    if cred_stale:
        h.append('<div style="color:#b00;font-size:12px;margin-top:2px">'
                 '<b>LEDGER STALE</b> &mdash; paste a fresh statement through '
                 '<code>imm_reward_recon.py --statement</code>; every CREDITED '
                 'figure above is a floor until then.</div>')
    h.append('<div style="color:#999;font-size:11px;margin-top:10px">'
             'QUOTED = markets the tier is quoting now; HELD = markets it '
             'stopped quoting but still holds inventory in (or accrued on) '
             '&mdash; their P&amp;L and accrual ARE in the row.</div>')
    h.append(f'<div style="color:#999;font-size:11px;margin-top:4px">'
             f'EARN EST = the bot&rsquo;s accrual estimator over the markets '
             f'it is tracking NOW. CREDITED = actual Kalshi money on the '
             f'event, all-time, every market. Same earnings, different scope '
             f'&mdash; <b>neither contains the other, so never add them</b>. '
             f'EARN EST is usually the larger (the accrual is not reset at a '
             f'period end, so it is credited-plus-in-flight), but it is '
             f'DELETED and restarts from zero whenever a market goes unquoted '
             f'and flat, and it cannot see markets that have left the book at '
             f'all &mdash; which is why some rows show CREDITED above EARN '
             f'EST. EARN EST also spans EVERY program period since the bot '
             f'first quoted the market, not just the current one: these '
             f'programs re-list weekly (KXAAAGASMINM-26SEP30 ran 9/02&ndash;'
             f'9/09 and again 9/09&ndash;9/16), so the exchange&rsquo;s own '
             f'rewards view &mdash; which shows the CURRENT period &mdash; '
             f'reads far lower than this column. '
             f'P&amp;L = trading only: LIFETIME realized '
             f'(Kalshi&rsquo;s own per-market realized_pnl_dollars where the '
             f'bot&rsquo;s position matches the account&rsquo;s, the '
             f'bot&rsquo;s realized sink otherwise) plus open-book MTM at the '
             f'mid. Until 2026-09-11 it was the past 24 HOURS of realized, '
             f'which left weeks of a multi-week book&rsquo;s trading out of '
             f'the column. A row covers the event&rsquo;s LIVE book, so a '
             f'strike that has already settled sits in the CUMULATIVE table '
             f'below, not here. NET = P&amp;L + EARN EST, so on the rows where '
             f'CREDITED exceeds EARN EST, NET understates.</div>')
    h.append(f'<div style="color:#999;font-size:11px;margin-top:4px">'
             f'Cumulative REALIZED is the bot&rsquo;s own per-market realized '
             f'trading P&amp;L and only starts <b>{cum_since}</b> (the first '
             f'analytics-sink day) &mdash; anything earlier is not in it. MTM '
             f'is the open book right now. NET = EST + REALIZED + MTM, the '
             f'same estimate basis as the tables above.</div>')
    h.append('</div>')
    return (text, "".join(h), subject, sorted(r["event"] for r in rows),
            perf_verdicts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="alias for --dry")
    args = ap.parse_args(argv)
    dry = args.dry or args.print_only

    now_utc = datetime.now(timezone.utc)
    today_ct = now_utc.astimezone(imm.CT).date()
    marker = os.path.join(imm.STATUS_DIR,
                          f"opportunistic_imm_sent_{today_ct}.marker")
    if not (args.test or dry) and os.path.exists(marker):
        log(f"opportunistic email already sent for {today_ct}; exiting")
        return 0

    attempts = 1 if (args.test or dry) else 8
    text = html = subject = None
    shown_events: list = []
    perf_verdicts: dict = {}
    for attempt in range(1, attempts + 1):
        try:
            (text, html, subject, shown_events,
             perf_verdicts) = build_report(now_utc)
            break
        except Exception as e:
            log(f"opportunistic build attempt {attempt}/{attempts} failed: {e!r}")
            if attempt == attempts:
                log("giving up for today")
                return 1
            time.sleep(300)
    log("opportunistic body:\n" + text)
    if dry:
        return 0

    alerter = imm.Alerter("IMM-OPP", live=True)
    if not alerter.enabled:
        log("cannot send opportunistic email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject, html=html)
        if ok:
            break
        log(f"opportunistic send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"opportunistic send: {'ok' if ok else 'FAILED'}")
    if ok:
        # a --test send is still an email Jack saw: the next one is judged
        # against it
        write_last_sent(now_utc, shown_events, perf_verdicts)
    if ok and not args.test:
        with open(marker, "w") as f:
            f.write(now_utc.isoformat())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
