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
  EARN EST   the bot's period-to-date accrued reward estimate (what we
             expect Kalshi to credit at the program's period end)
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
  EST       the bot's own accrual estimator. NOTE accrued_est is a per-
            market counter that is NEVER reset at a program-period boundary
            (incentive_mm BotState.accrued_est, "ticker -> lifetime est"),
            so EST is credited-plus-still-in-flight: KXAMZNCC-26OCT07 reads
            est $8.70 against $3.79 already credited. CREDITED is therefore
            a SUBSET of EST, not an addend — the tables never sum the two.
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

STRICTLY READ-ONLY. Scheduled daily 7:25 AM ET ("KL imm opportunistic"),
after the 7:10 digest and 7:20 quote-gaps. --test sends now ignoring the
sent-marker; --dry / --print build and print only.
"""
import argparse
import glob
import json
import os
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
                             FILL_LOOKBACK_HOURS)

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
            json.dump({"scan_events": sorted(r["scan_events"]),
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

    scan_events is a SET of event tickers the open-scan tier ever admitted
    (`selection_events` sink, is_scan flag). A set folds idempotently, so a
    partially written day can be re-read safely; it is only marked folded
    once the UTC day is complete.

    realized is event -> cumulative realized trading dollars, summed from the
    `realized` sink's DELTAS. Deltas are NOT idempotent — re-reading today's
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
                    # substring pre-filter: the sink writes one long line per
                    # decision change and only a few carry the scan flag
                    if '"is_scan": true' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
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
        into = r["realized"] if complete else live
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
                    ev = rec.get("event_ticker")
                    if ev:
                        into[str(ev)] = (into.get(str(ev), 0.0)
                                         + _f(rec.get("realized_delta_dollars")))
        except OSError:
            continue
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
            "earn": earn, "pnl": pnl, "net": earn + pnl}


def text_table(rows) -> list:
    """Plain-text event table + TOTAL row. One call per tier, so the two
    tiers' tables are identical in format by construction.

    QUOTED = markets the bot is quoting now. HELD = markets of the same
    event it has stopped quoting but still holds inventory in, or has
    accrued reward on (Jack 2026-09-07). Their P&L and accrual are in the
    row; they just consume no slot."""
    t = tier_totals(rows)
    L = [f"{'EVENT':<24}{'WHAT IT IS':<32}{'QUOT':>5}{'HELD':>5}"
         f"{'EARN EST$':>11}{'CRED$':>9}{'P&L$':>10}{'NET$':>10}"]
    for r in rows:
        # blank, not 0.00 — an event with no credit yet reads as "nothing has
        # landed", which is the fact; the HTML twin blanks it the same way
        cred = r.get("cred") or 0.0
        cred_s = f"{cred:,.2f}" if cred else ""
        L.append(f"{_short_event(r['event'])[:23]:<24}{r['label'][:31]:<32}"
                 f"{r['mkts']:>5}{(r.get('held') or ''):>5}"
                 f"{r['earn']:>11.2f}{cred_s:>9}"
                 f"{r['pnl']:>+10.2f}{r['net']:>+10.2f}")
    L.append(f"{'TOTAL':<24}{'':<32}{t['mkts']:>5}{(t['held'] or ''):>5}"
             f"{t['earn']:>11.2f}{t['cred']:>9,.2f}{t['pnl']:>+10.2f}"
             f"{t['net']:>+10.2f}")
    return L


def html_table(rows) -> str:
    """HTML twin of text_table (same columns, same TOTAL row)."""
    t = tier_totals(rows)
    h = ['<table style="border-collapse:collapse;margin:6px 0">']
    h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
             f'<td style="{TDL}">EVENT</td><td style="{TDL}">WHAT IT IS</td>'
             f'<td style="{TD}">QUOTED</td><td style="{TD}">HELD</td>'
             f'<td style="{TD}">EARN EST$</td><td style="{TD}">CREDITED$</td>'
             f'<td style="{TD}">P&amp;L$</td><td style="{TD}">NET$</td></tr>')
    for i, r in enumerate(rows):
        bg = "#fafafa" if i % 2 else "#fff"
        held = r.get("held") or 0
        cred = r.get("cred") or 0.0
        h.append(f'<tr style="background:{bg}">'
                 f'<td style="{TDL}"><b>{_short_event(r["event"])}</b></td>'
                 f'<td style="{TDL}">{r["label"]}</td>'
                 f'<td style="{TD}">{r["mkts"]}</td>'
                 f'<td style="{TD};color:#888">{held or ""}</td>'
                 f'<td style="{TD}">{r["earn"]:,.2f}</td>'
                 f'<td style="{TD};color:#0a7">{cred and f"{cred:,.2f}" or ""}'
                 f'</td>'
                 f'<td style="{TD}">{_pnl_span(r["pnl"])}</td>'
                 f'<td style="{TD};font-weight:700">{_pnl_span(r["net"])}</td>'
                 f'</tr>')
    h.append(f'<tr style="background:#f0f0f0;font-weight:700">'
             f'<td style="{TDL}">TOTAL</td><td style="{TDL}"></td>'
             f'<td style="{TD}">{t["mkts"]}</td>'
             f'<td style="{TD}">{t["held"] or ""}</td>'
             f'<td style="{TD}">{t["earn"]:,.2f}</td>'
             f'<td style="{TD};color:#0a7">{t["cred"]:,.2f}</td>'
             f'<td style="{TD}">{_pnl_span(t["pnl"])}</td>'
             f'<td style="{TD}">{_pnl_span(t["net"])}</td></tr>')
    h.append('</table>')
    return "".join(h)


# ---- the cumulative table --------------------------------------------------
# Same twin-renderer discipline as the active tables above: one row builder,
# a text renderer and an HTML renderer that must stay identical in shape.
# Columns and their bases are the docstring's contract — CREDITED is actual
# money and is a SUBSET of EST, so nothing here ever adds the two.
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
    # pnl_windows only to keep this email's basis identical to the digest's
    # (and to book the past-day per-event realized/settlement).
    w = pnl_windows(client, state, our_ids, fills, mids, results,
                    ss["reward_lifetime"])
    day_ev = w["day"]["events"]

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

    rows = []
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
        de = day_ev.get(ev) or {}
        pnl = mtm + _f(de.get("realized")) + _f(de.get("settle"))
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
            "earn": earn, "cred": cred_by_event.get(ev, 0.0), "pnl": pnl,
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
    realized_by_event = hist["realized"]
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
    # One section per tier, same table format (Jack 2026-09-06).
    tiers = [
        ("FINECON", "curated Finance/Economics quiet prints",
         f"{len(fin_members)}/{top_n} slots, +{used}/{openings_cap} daily "
         f"openings used", fin_rows),
        ("OPEN SCAN", "all other markets, machine-screened for adverse "
         "selection",
         f"{len(scan_sel)}/{scan_top_n} slots, +{scan_used}/"
         f"{scan_openings_cap} daily openings used"
         + (", HALTED today (loss budget)" if scan_halted else "")
         + (f", {scan_evicted} event(s) evicted" if scan_evicted else ""),
         scan_rows),
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
         fam_rows),
    ]

    subject = (f"Opportunistic IMM {today_et} — est ${tot_earn:,.0f} in "
               f"flight, net ${tot_net:+,.0f}, ${cred_life:,.0f} credited "
               f"to date")

    # ---- plain text ---------------------------------------------------------
    L = [f"Opportunistic IMM — {today_et}", ""]
    L.append(f"ACTIVE — {len(rows)} events / {tot_mkts} markets quoted "
             f"across {len(tiers)} tiers"
             + (f", plus {tot_held} held but no longer quoted." if tot_held
                else "."))
    L.append(f"Est reward accrued ${tot_earn:,.2f}  |  trading P&L "
             f"${tot_pnl:+,.2f}  |  net ${tot_net:+,.2f}.")
    L.append("")
    for name, desc, slots, trows in tiers:
        L.append(f"{name} — {desc}")
        L.append(f"{slots}.")
        if trows:
            L.extend(text_table(trows))
        else:
            L.append(f"No {name.lower()} events quoted right now.")
        L.append("")
    if cum_rows:
        L.append("CUMULATIVE — every event each tier has ever touched, "
                 "settled and gone included")
        L.extend(cum_text_table(cum_rows))
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
             "EARN EST = the bot's own accrual estimator, running since it "
             "first quoted the market and NOT reset at a program-period end, "
             "so it is credited-plus-still-in-flight. CREDITED = actual "
             "Kalshi money on that event to date, all-time - a SUBSET of "
             "EARN EST, never an addend. P&L = trading only (realized + "
             "settlement + open-book MTM), same windowed attribution as the "
             f"digest ({FILL_LOOKBACK_HOURS}h). NET = P&L + EARN EST.")
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
    for name, desc, slots, trows in tiers:
        h.append(f'<div style="font-size:15px;font-weight:600;margin:14px 0 2px">'
                 f'{name} <span style="color:#888;font-weight:400">&mdash; '
                 f'{desc}</span></div>')
        h.append(f'<div style="color:#555;font-size:13px;margin-bottom:4px">'
                 f'{slots}</div>')
        if trows:
            h.append(html_table(trows))
        else:
            h.append(f'<div style="color:#666;font-size:13px">No '
                     f'{name.lower()} events quoted right now.</div>')
    if cum_rows:
        h.append('<div style="font-size:15px;font-weight:600;margin:18px 0 2px">'
                 'Cumulative <span style="color:#888;font-weight:400">&mdash; '
                 'every event each tier has ever touched, settled and gone '
                 'included</span></div>')
        h.append(cum_html_table(cum_rows))
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
    h.append(f'<div style="color:#999;font-size:11px;margin-top:10px">'
             f'EARN EST = the bot&rsquo;s own accrual estimator, running since '
             f'it first quoted the market and NOT reset at a program-period '
             f'end &mdash; so it is credited-plus-still-in-flight. CREDITED = '
             f'actual Kalshi money on that event to date, all-time: a SUBSET '
             f'of EARN EST, never an addend. P&amp;L = trading only (realized '
             f'+ settlement + open-book MTM), same windowed attribution as '
             f'the digest ({FILL_LOOKBACK_HOURS}h). NET = P&amp;L + EARN EST.'
             f'</div>')
    h.append(f'<div style="color:#999;font-size:11px;margin-top:4px">'
             f'Cumulative REALIZED is the bot&rsquo;s own per-market realized '
             f'trading P&amp;L and only starts <b>{cum_since}</b> (the first '
             f'analytics-sink day) &mdash; anything earlier is not in it. MTM '
             f'is the open book right now. NET = EST + REALIZED + MTM, the '
             f'same estimate basis as the tables above.</div>')
    h.append('</div>')
    return text, "".join(h), subject


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
    for attempt in range(1, attempts + 1):
        try:
            text, html, subject = build_report(now_utc)
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
    if ok and not args.test:
        with open(marker, "w") as f:
            f.write(now_utc.isoformat())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
