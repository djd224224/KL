#!/usr/bin/env python3
r"""imm_upcoming.py -- the UPCOMING incentive programs: what Kalshi has
scheduled but not started yet, ranked by what the bot would earn there, and
whether the bot expects to quote each one when it starts (Jack 2026-11-12:
"yes add 'upcoming incentive markets' to the morning new-programs email,
ranked by attractiveness and whether we are expecting to quote them or not").

SOURCE: GET /incentive_programs?status=upcoming (the 11/12 finding: Kalshi
lists programs before they start, with start time, reward and target). Per
market only its NEXT program counts (IncentiveMarketMaker.upcoming_by_market:
earliest start, programs sharing it summed). A market that is paying NOW and
only has its next period listed is a RENEWAL: counted, not listed -- the
email's other tables already cover it.

ONE ROW PER SERIES (an FX family lists 30 events of 21 strikes each), with
its event count; a one-event series shows the event ticker.

WHAT "EXPECT TO QUOTE" MEANS: each new market walks the checks the 7:20
quote-gaps email runs on live programs (imm_quote_gaps.classify_and_estimate),
evaluated AS OF ITS PROGRAM START: blocklist, allowlist (a family-suffix
series still awaiting the bot's Carbon Arc check reads "at start"), the bot's
own MarketMeta and _screen (cutoff, closing, the book screens), then the
estimator on today's book and the payout / rate floors over the program's
quotable days.
  yes    allowed, the screens pass and the projection clears the payout bar
  maybe  allowed, but only the start can settle it: the book is wide /
         one-sided / thin today (books fill when a program starts), the
         market is not open yet, a Carbon Arc check at start, the open-scan
         tier's screens, or the book budget did not reach it
  no     blocked, not allowlisted, a cutoff at or before the start, an
         extreme mid, under the payout or rate floor, the no-new gate
The gates that live in the quote loop (the fair gates, sweep holds, slots and
the collateral budget) are not modelled: "yes" is admission, not a promise.

ATTRACTIVENESS = EST $: the bot's estimator on today's book -- day-size $/day
over the market's quotable days from its start (the payout bar's
projection) -- for quotable and unquotable markets alike, so a "no" row says
what fixing it would be worth. Books are read biggest series pools first,
MAX_BOOKS in all and MAX_PER_SERIES per series (the markets the bot may quote
first, the "no" rows with what is left); a series' unread markets take its
read markets' capture of pool ("~"), and a series with none read takes its
pool x PROXY_CAPTURE (the family watch's median capture, "proxy"). Pre-start
books are thinner than they will be once the program draws other makers, so
the estimate leans high: it ranks, it does not promise.

STRICTLY READ-ONLY: GETs only, an offline bot (live=False) that never runs a
cycle or saves state, no files written.
"""

import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import imm_quote_gaps as gaps          # applies the launcher env first
import incentive_mm as imm
from incentive_mm import log

HORIZON_DAYS = float(os.environ.get("IMM_UPCOMING_EMAIL_DAYS", "14"))
MAX_DETAILS = int(os.environ.get("IMM_UPCOMING_MAX_DETAILS", "6000"))
MAX_BOOKS = int(os.environ.get("IMM_UPCOMING_MAX_BOOKS", "250"))
MAX_PER_SERIES = int(os.environ.get("IMM_UPCOMING_MAX_PER_SERIES", "6"))
PROXY_CAPTURE = float(os.environ.get("IMM_GAP_CAPTURE", "0.022"))
PAGE_LIMIT = 1000
MAX_PAGES = int(os.environ.get("IMM_UPCOMING_MAX_PAGES", "10"))

# _screen verdicts a program start can change: books fill when one starts
BOOK_NOW = frozenset({"wide", "one_sided", "no_volume"})
# a screen no config fixes: an extreme mid is a decided-looking market
STRUCTURAL = frozenset({"extreme_mid"})
# ...and the ones a start cannot change either
DEAD_AT_START = {"cutoff": "cutoff at or before the start",
                 "no_event_window": "no event date (config gap)",
                 "closing": "closes too soon after the start",
                 "program_over": "program over", "no_target": "no target"}


def fetch_upcoming_raw(client) -> list:
    """Every upcoming program row (paged). Raises on a failed read."""
    rows, cursor = [], None
    for _ in range(MAX_PAGES):
        params = {"limit": PAGE_LIMIT, "status": "upcoming"}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/incentive_programs", params=params) or {}
        batch = resp.get("incentive_programs") or []
        rows.extend(batch)
        cursor = resp.get("next_cursor")
        if not cursor or not batch:
            break
    return rows


# the bot's hot-reloaded admission files, as refresh_universe loads them: the
# allowlist verdicts here must be the running bot's
_RELOADS = ("load_file_event_overrides", "load_extra_allow_series",
            "load_finecon_extra_series", "load_vercel_series", "load_crypto_open_series",
            "load_election_extra_series",
            "load_pvd_markets", "load_nfl_auto_series", "load_league_auto_series",
            "load_awards_extra_series", "load_graduate_auto_series",
            "load_mention_auto_series", "load_politics_auto_series",
            "load_election_dates_extra", "load_awards_dates_extra",
            "load_family_verdicts")


def hot_reload() -> None:
    for name in _RELOADS:
        fn = getattr(imm, name, None)
        if fn is None:
            continue
        try:
            fn()
        except Exception as e:
            log(f"! upcoming: {name} failed ({e!r:.80}); verdicts may be narrow")


def _days(info) -> float:
    return max((info["end"] - info["start"]).total_seconds() / 86400.0, 1.0 / 24)


def _static_verdict(bot, t: str):
    """(kind, why) from config alone, or None when the market is allowed."""
    series = imm.series_of(t)
    if bot._blocked(t):
        return ("no", "frozen" if series in imm.FREEZE_SERIES else "blocked (config)")
    if bot._allowed(t):
        return None
    if imm.series_family_suffix(series) and imm.family_verdict(series) is None:
        return ("maybe", "Carbon Arc check at start")
    su = imm.scan_universe_reason(t)
    if su is None:
        return ("maybe", "open-scan tier screens")
    if su == "excluded_family":
        return ("no", "excluded family")
    return ("no", "not allowlisted")


def _floor_verdict(meta, est_day: float, est_total: float, at: datetime):
    """The admission floors on an estimated, allowed, screen-passing market."""
    series = meta.series
    curated = imm.curated_event(meta.event_ticker, series, at)
    if series in imm.NO_NEW_SERIES:
        return "no", "no-new gate"
    if meta.yield_per_contract <= 0:
        return "no", "zero est yield"
    if not curated and est_day < imm.series_min_est_rate(series):
        return "no", "under rate floor"
    if not curated and est_total < imm.floor_bar_dollars(series, False):
        return "no", "under payout floor"
    return "yes", ""


def assess(client, bot, raw_rows: list, active_markets: set,
           now_utc: datetime) -> dict:
    """{"rows": [series rows, best first], "renewals": (n markets, $ pool),
    "n_new_markets", "n_events", "books", "horizon_days"}. See the module
    docstring."""
    hot_reload()
    programs = bot.upcoming_by_market(raw_rows, now_utc)
    horizon = now_utc + timedelta(days=HORIZON_DAYS)
    renew_n, renew_pool = 0, 0.0
    new: Dict[str, dict] = {}
    for t, info in programs.items():
        if info is None or info["start"] > horizon:
            continue
        if t in active_markets:
            renew_n += 1
            renew_pool += info["dollars_per_day"] * _days(info)
            continue
        new[t] = info

    # ---- per-market verdicts ------------------------------------------------
    rows: Dict[str, dict] = {}
    for t, info in new.items():
        sv = _static_verdict(bot, t)
        rows[t] = {"ticker": t, "event": gaps._event_of(t), "series": imm.series_of(t),
                   "info": info, "pool": info["dollars_per_day"] * _days(info),
                   "meta": None, "est_day": None, "est_total": None, "dead": False,
                   "kind": sv[0] if sv else None, "why": sv[1] if sv else ""}
    by_series: Dict[str, List[str]] = {}
    for t, r in rows.items():
        by_series.setdefault(r["series"], []).append(t)
    series_pool = {s: sum(rows[t]["pool"] for t in ts) for s, ts in by_series.items()}
    series_order = sorted(by_series, key=lambda s: (-series_pool[s], s))
    # details for the biggest series first (cheap: 50 a read)
    want = [t for s in series_order
            for t in sorted(by_series[s], key=lambda t: -rows[t]["pool"])]
    details = gaps.bulk_market_details(client, want[:MAX_DETAILS])

    for t, r in rows.items():
        m = details.get(t)
        at = max(r["info"]["start"], now_utc)
        if m is None:
            if r["kind"] is None:
                r["kind"], r["why"] = "maybe", "not read (budget)"
            continue
        status = str(m.get("status") or "").lower()
        if status not in ("active", "open"):
            if r["kind"] is None:
                r["kind"], r["why"] = "maybe", f"market not open yet ({status or '?'})"
            continue
        try:
            imm.ensure_family_override(r["series"])
            r["meta"] = meta = gaps.build_meta(bot, t, r["info"], m, at)
        except Exception as e:
            log(f"! upcoming meta {t} failed ({e!r:.80})")
            if r["kind"] is None:
                r["kind"], r["why"] = "maybe", "unreadable market"
            continue
        sr = bot._screen(meta, at)
        # structurally unearnable whatever the config: no estimate, and no
        # share of its series' extrapolated capture
        r["dead"] = sr in DEAD_AT_START or sr in STRUCTURAL
        if r["kind"] is not None:
            continue                          # known from config; estimated below
        if sr in DEAD_AT_START:
            r["kind"], r["why"] = "no", DEAD_AT_START[sr]
        elif sr in BOOK_NOW:
            r["kind"], r["why"] = "maybe", f"book {sr.replace('_', '-')} today"
        elif sr:
            r["kind"], r["why"] = "no", sr.replace("_", " ")

    # ---- estimates: biggest series first, quotable markets before "no" -------
    books = 0
    per_series: Counter = Counter()
    queue = [t for no_pass in (False, True) for s in series_order
             for t in sorted(by_series[s], key=lambda t: -rows[t]["pool"])
             if (rows[t]["kind"] == "no") == no_pass]
    for t in queue:
        if books >= MAX_BOOKS:
            break
        r = rows[t]
        meta = r["meta"]
        if meta is None or r["dead"] or per_series[r["series"]] >= MAX_PER_SERIES:
            continue
        books += 1
        per_series[r["series"]] += 1
        try:
            ok = bot._estimate_candidate_yield(meta, [])
        except Exception as e:
            log(f"! upcoming estimate {t} failed ({e!r:.80})")
            ok = False
        if not ok:
            continue
        at = max(r["info"]["start"], now_utc)
        r["est_day"] = float(meta.est_dollars_per_day or 0.0)
        r["est_total"] = float(meta.floor_dollars_per_day or 0.0) * imm._quotable_days(meta, at)
        if r["kind"] is None:
            r["kind"], r["why"] = _floor_verdict(meta, r["est_day"], r["est_total"], at)
    for r in rows.values():
        if r["kind"] is None:
            r["kind"], r["why"] = "maybe", "not estimated (book budget)"

    # ---- series rollup --------------------------------------------------------
    out = []
    for s in series_order:
        sr = [rows[t] for t in by_series[s]]
        est = [r for r in sr if r["est_total"] is not None]
        pool = series_pool[s]
        if est:
            read_pool = sum(r["pool"] for r in est) or 1e-9
            cap_total = sum(r["est_total"] for r in est) / read_pool
            cap_day = sum(r["est_day"] for r in est) / read_pool
            unread = sum(r["pool"] for r in sr
                         if r["est_total"] is None and not r["dead"])
            est_total = sum(r["est_total"] for r in est) + cap_total * unread
            est_day = sum(r["est_day"] for r in est) + cap_day * unread
            src = "~" if unread > 0 else ""
        else:
            # never read: the proxy on the markets that can earn at all
            live_pool = sum(r["pool"] for r in sr if not r["dead"])
            est_total, est_day = live_pool * PROXY_CAPTURE, None
            src = "proxy" if live_pool > 0 else ""
        kinds = Counter(r["kind"] for r in sr)
        why = {k: ", ".join(w for w, _n in Counter(
            r["why"] for r in sr if r["kind"] == k and r["why"]).most_common(2))
            for k in ("maybe", "no")}
        events = sorted({r["event"] for r in sr})
        out.append({
            "series": s, "events": events,
            "label": events[0] if len(events) == 1 else f"{s} x{len(events)} events",
            "n": len(sr), "n_yes": kinds.get("yes", 0), "n_maybe": kinds.get("maybe", 0),
            "n_no": kinds.get("no", 0),
            "kind": ("yes" if kinds.get("yes") else
                     "maybe" if kinds.get("maybe") else "no"),
            "why_maybe": why["maybe"], "why_no": why["no"],
            "pool": pool,
            "pool_day": sum(r["info"]["dollars_per_day"] for r in sr),
            "start": min(r["info"]["start"] for r in sr),
            "end": max(r["info"]["end"] for r in sr),
            "est_total": est_total, "est_day": est_day, "est_src": src,
            "n_read": len(est),
        })
    out.sort(key=lambda e: (-e["est_total"], -e["pool"], e["series"]))
    return {"rows": out, "renewals": (renew_n, renew_pool),
            "n_new_markets": len(new), "n_events": len({r["event"] for r in rows.values()}),
            "books": books, "horizon_days": HORIZON_DAYS}


def quote_cell(e: dict) -> str:
    """"yes 5/5" / "yes 2/11; 9 no (extreme mid)" / "maybe 3/3 (book wide
    today)" / "no (not allowlisted)": the series' markets by verdict, the
    commonest reasons beside each."""
    def part(kind, n, lead):
        why = e.get(f"why_{kind}") or ""
        return (f"{lead}" + (f" ({why})" if why else "")) if n else ""
    n = e["n"]
    if e["kind"] == "no":
        return part("no", e["n_no"], "no")
    parts = [f"yes {e['n_yes']}/{n}"] if e["kind"] == "yes" else []
    if e["kind"] == "maybe":
        parts.append(part("maybe", e["n_maybe"], f"maybe {e['n_maybe']}/{n}"))
    elif e["n_maybe"]:
        parts.append(part("maybe", e["n_maybe"], f"{e['n_maybe']} maybe"))
    if e["n_no"]:
        parts.append(part("no", e["n_no"], f"{e['n_no']} no"))
    return "; ".join(x for x in parts if x)


def est_cell(e: dict) -> str:
    v = f"{e['est_total']:,.2f}"
    return {"~": "~" + v, "proxy": v + " proxy"}.get(e["est_src"], v)


def day_cell(e: dict) -> str:
    return "-" if e["est_day"] is None else (
        ("~" if e["est_src"] == "~" else "") + f"{e['est_day']:,.2f}")


def summary(up: dict) -> Dict[str, tuple]:
    """kind -> (series rows, $ est, $ pool, markets)."""
    out = {}
    for kind in ("yes", "maybe", "no"):
        rs = [e for e in up["rows"] if e["kind"] == kind]
        out[kind] = (len(rs), sum(e["est_total"] for e in rs),
                     sum(e["pool"] for e in rs), sum(e["n"] for e in rs))
    return out


def build(client, active_markets: set, now_utc: datetime, bot=None) -> dict:
    """The email's entry point: an offline bot (unless given), the feed read
    and the assessment."""
    if bot is None:
        bot = imm.IncentiveMarketMaker(client, live=False)
        bot.alerter.enabled = False
    return assess(client, bot, fetch_upcoming_raw(client), active_markets, now_utc)


if __name__ == "__main__":                      # a dry read, no email
    import sys
    client = imm.build_client()
    bot = imm.IncentiveMarketMaker(client, live=False)
    bot.alerter.enabled = False
    now = datetime.now(timezone.utc)
    active = set(bot.fetch_programs() or {})
    up = build(client, active, now, bot=bot)
    print(f"{len(up['rows'])} series, {up['n_events']} events, {up['n_new_markets']} new "
          f"markets, {up['books']} books; renewals {up['renewals'][0]} "
          f"(${up['renewals'][1]:,.0f})")
    for k, (n, est, pool, mk) in summary(up).items():
        print(f"  {k:5} {n:4} series {mk:5} markets est ${est:,.2f} pool ${pool:,.0f}")
    for e in up["rows"][:int(sys.argv[1]) if len(sys.argv) > 1 else 40]:
        print(f"  {e['start']:%m-%d %H:%MZ} {e['label']:40} n {e['n']:4} pool "
              f"${e['pool']:8,.0f} est {est_cell(e):>16} /day {day_cell(e):>8}  {quote_cell(e)}")
