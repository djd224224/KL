#!/usr/bin/env python3
"""IMM family watch: every 30 minutes (Windows task "KL imm family-watch").

Jack 2026-10-07: "how to ensure new events get quoted when they should fall
under existing quote families e.g. elections, award ceremonies, rain, etc. dont
want to manually catch this going fwd" -> "yes build all 5. $5/day is the right
threshold". Three passes, each fail-safe on its own:

1. ENROLL -- the overrides task's new-series pass
   (imm_earnings_overrides.enroll_new_series: Kalshi's Elections category,
   mention families, the FT/APP consumer families, Carbon Arc sources, the
   NFL auto families, and since 2026-10-12 the award shows by Kalshi tag --
   a new awards member stands down until pass 3 dates its event). It ran
   at 6:45am / 12:45pm / 4:45pm ET only, so an evening batch waited half a day
   (KXSTATELEG, 28 state chambers, lit 22:31Z 10/07).
2. DATE -- an election-family event with no row that is a US race of the 2026
   general election (us_general_vote_ok: Kalshi's Elections category, a US tag,
   rules naming 2026 with vote language and no rally / debate / court / primary
   / runoff / special-election words, expiring Nov 3 2026 - Jan 2027) gets its
   Nov 3 row in election_dates_extra.json: the statute fixes the day (2 U.S.C.
   7 and each state's general-election law). Anything else stays in config
   gaps for pass 3.
3. RESEARCH (2026-10-08, Jack: "yes switch" -- in place of a 2-hourly
   routine) -- what only research can fix (an election event with no
   verified voting day, an awards event with no hand row, a broadcast mention
   event the WH / RNC / TVmaze resolvers cannot place) goes to one headless
   Claude Code run on first sight, retried on a backoff; see research_pass.
   A run that dies on the Claude Code login is emailed at once (LOGIN FAILURES).
   With a `claude setup-token` token in RESEARCH_TOKEN_FILE the runs use that
   one-year login instead of the interactive one (RESEARCH TOKEN).
4. ALERT -- one email per run for what has stayed dark >= GAP_ALERT_AFTER_MIN
   (30 min) and is worth >= GAP_ALERT_MIN_DPD ($5/day of estimated reward: the
   bot's own estimator on the live books since 2026-10-09, see REAL ESTIMATES;
   pool $/day x GAP_CAPTURE where no book was read): the live
   bot's config gaps (re-sent daily while open), and series that newly appear
   paying but not allowed -- a new family, which is Jack's call (sent once).
5. MODEL COVERAGE (2026-10-09, Jack: "when new markets are added onto an event
   that is modeled separately like cities in rain, they should automatically
   be modeled generally....ensure i get alerted if this is not the case") --
   imm_model_coverage checks every paying market of a per-entity-modeled
   family (daily rain cities, Carbon Arc entities, monthly rain / snow
   stations, Pokemon items, NFL players, Treasury shapes, Vercel labs, data
   center states, the YouTube pilot's artists, non-NFL sports ladders) against
   its model and emails what is not modeled: on first sight, then daily while
   open (a by-design exclusion once). A failing pass is emailed too (daily).

Imports imm_quote_gaps first: it mirrors the launcher's env (blocklist, allow
lists, pilots) before incentive_mm is imported, so "allowed" means what the live
bot means.

  python imm_family_watch.py            # the scheduled run
  python imm_family_watch.py --dry      # report only: no writes, no email
  python imm_family_watch.py --check-login   # test the research login, nothing else
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import imm_quote_gaps  # noqa: F401  (launcher env, then incentive_mm)
import incentive_mm as imm
import imm_earnings_overrides as ieo
import imm_feed_audit as ifa
import imm_rows
from incentive_mm import log

GAP_ALERT_MIN_DPD = float(os.environ.get("IMM_GAP_ALERT_MIN_DPD", "5"))
GAP_ALERT_AFTER_MIN = float(os.environ.get("IMM_GAP_ALERT_AFTER_MIN", "30"))
GAP_REALERT_HOURS = float(os.environ.get("IMM_GAP_REALERT_HOURS", "24"))
# median est_frac of the quoted rows of 10/07's cycle log (p25 0.010, p75 0.042)
GAP_CAPTURE = float(os.environ.get("IMM_GAP_CAPTURE", "0.022"))
STATE_FILE = os.path.join(imm.STATUS_DIR, "family_watch_state.json")
# how long an alert / research record outlives its item's absence from the
# bot's gaps file (2026-10-10: a bot restart empties the file for a refresh
# or two; the 02:09Z run then dropped every record, and 02:20Z re-researched
# two judgment calls and would have re-alerted them as new)
STATE_GRACE_HOURS = float(os.environ.get("IMM_WATCH_STATE_GRACE_H", "6"))

US_ELECTION_TAGS = frozenset({"US Elections", "Other US Elections"})
US_GENERAL_2026 = "2026-11-03"
_US_EXP_LO = datetime(2026, 11, 3, tzinfo=timezone.utc)
_US_EXP_HI = datetime(2027, 1, 31, 23, 59, tzinfo=timezone.utc)
_VOTE_RE = re.compile(r"\b(win|wins|won|winner|elected|elections?|seats?|majority|"
                      r"control|votes?)\b", re.I)
_NOT_VOTE_RE = re.compile(r"\b(rally|rallies|debates?|attends?|endorse\w*|courts?|"
                          r"invalidat\w*|announce\w*|primary|primaries|runoffs?|"
                          r"special election|recall|nominat\w*|resign\w*|impeach\w*|"
                          r"indict\w*|appoint\w*)\b", re.I)


def us_general_vote_ok(series: dict, market: dict) -> Tuple[bool, str]:
    """(True, why) when the market is a US race decided by the Nov 3 2026
    general election, else (False, the first test it fails). Reads the
    market's own terms (rules_primary + title), not the boilerplate."""
    series = series or {}
    if series.get("category") != "Elections":
        return False, "not Kalshi's Elections category"
    tags = set(series.get("tags") or [])
    if "International elections" in tags or not tags & US_ELECTION_TAGS:
        return False, "not tagged as a US election"
    text = f"{market.get('rules_primary') or ''} {market.get('title') or ''}"
    # the year: the terms name 2026, or the event segment ends in 26
    # (KXBEXARCOUNTYJUDGE-26, KXSTATELEG-TXSENA26) -- 28 of the 59 verified
    # Nov 3 events say only "wins the ... election"
    seg = str(market.get("event_ticker") or "").partition("-")[2]
    if not re.search(r"\b2026\b", text) and not re.fullmatch(r"[A-Z0-9]*?26", seg):
        return False, "neither its terms nor its ticker name 2026"
    m = _NOT_VOTE_RE.search(text)
    if m:
        return False, f"not a general-election result ({m.group(0)!r})"
    if not _VOTE_RE.search(text):
        return False, "no vote language in its terms"
    exp = imm.parse_iso_utc(market.get("expected_expiration_time")
                            or market.get("close_time") or "")
    if exp is None or not (_US_EXP_LO <= exp <= _US_EXP_HI):
        return False, "expiry outside Nov 3 2026 - Jan 2027"
    return True, "US race of the Nov 3 2026 general election"


# Events are keyed with event_key, not a bare rsplit: a one-market event's
# market ticker IS its event ticker (KXTUREKOUTPERFORMRCP-26NOV03, the 10/08
# 02:02Z RCP-outperform batch), and the rsplit made it the SERIES -- so the
# dating pass found no markets and skipped it unlogged, and research and the
# alert both read a $0 pool while nine of them paid ~$56/market/day. And a
# strike can carry a dash (2026-10-09: KXSENGOPLEADRUN-26-27JAN01-JTHU is
# event KXSENGOPLEADRUN-26, which imm.event_ticker_of reads as
# ...-26-27JAN01; 187 of 6,238 paying markets have 3+ dashes): those are read
# from the market data once (load_event_map) and cached in the watch state.
_EVENT_OF: Dict[str, str] = {}


def event_key(t: str) -> str:
    """The event of a paying market ticker: the market data's event_ticker
    when load_event_map read it, else imm.event_ticker_of."""
    return _EVENT_OF.get(t) or imm.event_ticker_of(t)


def load_event_map(client, progs: Dict[str, dict], state: dict) -> int:
    """Fill _EVENT_OF for the paying markets whose ticker is ambiguous (3+
    dashes), from state["event_of"] and a bulk market read for the new ones.
    Returns how many were read now."""
    cached = state.get("event_of") or {}
    ambiguous = [t for t in progs if t.count("-") >= 3]
    need = [t for t in ambiguous if t not in cached]
    got = imm_quote_gaps.bulk_market_details(client, need) if need else {}
    for t in need:
        ev = (got.get(t) or {}).get("event_ticker")
        if ev:
            cached[t] = ev
    state["event_of"] = {t: cached[t] for t in ambiguous if t in cached}
    _EVENT_OF.clear()
    _EVENT_OF.update(state["event_of"])
    return len(need)


def _by_event(progs: Dict[str, dict]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for t in progs:
        out[event_key(t)].append(t)
    return out


def event_pools(progs: Dict[str, dict]) -> Dict[str, float]:
    pools: Dict[str, float] = defaultdict(float)
    for t, p in progs.items():
        pools[event_key(t)] += ifa._dollars_per_day(p)
    return pools


def date_us_general(client, progs: Dict[str, dict], dry: bool) -> List[Tuple[str, bool, str]]:
    """Give every undated election-family event that passes us_general_vote_ok
    its Nov 3 2026 row. [(event, added, message)] for the log."""
    imm.load_election_extra_series()
    imm.load_election_dates_extra()
    cache: Dict[str, dict] = {}
    out = []
    for ev, tickers in sorted(_by_event(progs).items()):
        series = imm.series_of(tickers[0])
        if not imm.election_series(series) or imm.election_cutoff_utc(ev) is not None:
            continue
        if series not in cache:
            cache[series] = ieo.series_meta(client, series)
        try:
            mk = (client.get("/markets", params={"event_ticker": ev}) or {}).get("markets") or []
        except Exception as e:
            out.append((ev, False, f"market read failed: {e}"))
            continue
        if not mk:
            out.append((ev, False, "no markets listed for the event"))
            continue
        ok, why = us_general_vote_ok(cache[series], mk[0])
        if not ok:
            out.append((ev, False, why))
            continue
        if dry:
            out.append((ev, False, f"would date {US_GENERAL_2026} ({why})"))
            continue
        sources = [
            "Tue 2026-11-03, the 2026 US general election (2 U.S.C. 7 for "
            "Congress; each state's general-election statute for its offices)",
            f"Kalshi: series {series} category Elections, tags "
            f"{sorted(cache[series].get('tags') or [])}; terms name 2026; "
            f"expected expiration {mk[0].get('expected_expiration_time')}"]
        added, msg = imm_rows.add_row("election", ev, US_GENERAL_2026, sources,
                                      by="family-watch:us-general-2026", note=why)
        out.append((ev, added, msg))
    return out


def gap_items(progs: Dict[str, dict], gaps: dict, not_allowed: Dict[str, float],
              baseline: set, starts: Dict[str, dict] = None) -> Dict[str, dict]:
    """What is dark for want of admission or a row, keyed for the alert state:
    gap:<event> for the live bot's config gaps, start:<event> for a broadcast
    mention event with no start time (`starts`, from research_targets),
    new:<series> for a paying not-allowed series outside the baseline (a new
    family)."""
    pools = event_pools(progs)
    items: Dict[str, dict] = {}
    for key, t in sorted((starts or {}).items()):
        if key.startswith("start:"):
            items[key] = {"est": round(pools.get(t["event"], 0.0) * GAP_CAPTURE, 2),
                          "what": f"{t['event']}: {t['msg']}",
                          "fix": "python imm_earnings_overrides.py --set EVENT "
                                 "\"YYYY-MM-DDTHH:MM:00-04:00\""}
    for ev, g in sorted((gaps or {}).items()):
        items[f"gap:{ev}"] = {
            "est": round(pools.get(ev, 0.0) * GAP_CAPTURE, 2),
            "what": f"{ev}: {g.get('msg') or 'config gap'}",
            "fix": "by hand: python imm_rows.py election|award add EVENT "
                   "YYYY-MM-DD --source A --source B"}
    for s, dpd in sorted(not_allowed.items()):
        if s in baseline:
            continue
        items[f"new:{s}"] = {
            "est": round(dpd * GAP_CAPTURE, 2),
            "what": f"{s}: newly paying (${dpd:,.0f}/day pool) and not on the allowlist",
            "fix": "your call: enroll the family, or leave it out"}
    return items


def due_alerts(state_items: Dict[str, dict], items: Dict[str, dict], now_ts: float,
               after_min: float = None, min_dpd: float = None,
               realert_hours: float = None) -> Tuple[List[Tuple[str, dict, float]], Dict[str, dict]]:
    """(the items to email now [(key, item, minutes dark)], the new state).
    An item is tracked from first sight; it is due once it has been dark
    after_min and is worth min_dpd, then again every realert_hours while it
    stays open -- except new:<series>, which is sent once. An item no
    longer present keeps its record for STATE_GRACE_HOURS (a bot restart
    empties config_gaps.json until its first refresh re-meets the gaps:
    2026-10-10 02:09Z), so one that comes back is not new; then it drops
    out (fixed, or gone)."""
    after_min = GAP_ALERT_AFTER_MIN if after_min is None else after_min
    min_dpd = GAP_ALERT_MIN_DPD if min_dpd is None else min_dpd
    realert_hours = GAP_REALERT_HOURS if realert_hours is None else realert_hours
    due, fresh = [], {}
    for key, it in items.items():
        prev = state_items.get(key) or {}
        first = float(prev.get("first") or now_ts)
        alerted = prev.get("alerted")
        rec = {"first": first, "alerted": alerted, "est": it["est"], "seen": now_ts}
        dark_min = (now_ts - first) / 60.0
        if not it.get("hold") and it["est"] >= min_dpd and dark_min >= after_min and (
                alerted is None or (not key.startswith("new:")
                                    and now_ts - float(alerted) >= realert_hours * 3600)):
            due.append((key, it, dark_min))
            rec["alerted"] = now_ts
        fresh[key] = rec
    for key, rec in state_items.items():
        if key not in fresh and now_ts - float(rec.get("seen") or rec.get("first") or 0) \
                < STATE_GRACE_HOURS * 3600:
            fresh[key] = rec
    return due, fresh


def hold_researched(items: Dict[str, dict], targets: Dict[str, dict], state: dict,
                    now_ts: float) -> None:
    """Mark the researchable items research has not given up on as `hold`
    (tracked, not alerted) and add research's last word to each. Research has
    given up on a judgment call, on two unresolved tries, or on the attempt
    cap; and anything dark ALERT_STALE_HOURS is alerted regardless."""
    rstate = state.get("research") or {}
    seen = state.get("items") or {}
    for key, it in items.items():
        if key not in targets:
            continue
        r = rstate.get(key) or {}
        n = int(r.get("attempts") or 0)
        gave_up = (r.get("status") == "judgment"
                   or (r.get("status") == "unresolved" and n >= 2)
                   or n >= RESEARCH_MAX_ATTEMPTS)
        first = float((seen.get(key) or {}).get("first") or now_ts)
        it["hold"] = not gave_up and (now_ts - first) < ALERT_STALE_HOURS * 3600
        if r.get("status"):
            it["fix"] = f"research ({n} tr{'y' if n == 1 else 'ies'}): {r['status']} -- {r.get('note', '')}"


def _fmt_end(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d %H:%MZ")
    except (TypeError, ValueError):
        return "?"


def describe(client, due: List[Tuple[str, dict, float]]) -> None:
    """Give each alerted item a plain description, `title` (Jack 2026-10-11:
    "make sure the description of the event is listed, otherwise i dont know
    what 'KXTSAD' is"): new:<series> -> the series' title, category and
    cadence from Kalshi's /series record ("Daily TSA passenger screenings
    (Economics, daily)"); gap:/start:<event> -> the event's title and
    subtitle from /events. A failed read just leaves the item without one."""
    for key, it, _dark in due:
        if it.get("title"):
            continue
        kind, _, tick = key.partition(":")
        try:
            if kind == "new":
                s = ieo.series_meta(client, tick)
                desc = str(s.get("title") or "").strip()
                extra = ", ".join(str(x) for x in (s.get("category"), s.get("frequency")) if x)
                if desc and extra:
                    desc += f" ({extra})"
            else:
                ev = (client.get(f"/events/{tick}") or {}).get("event") or {}
                title = str(ev.get("title") or "").strip()
                sub = str(ev.get("sub_title") or "").strip()
                desc = title + (f" -- {sub}" if sub and sub not in title else "")
        except Exception as e:
            log(f"[WATCH] ! no description for {key}: {e!r}")
            continue
        if desc:
            it["title"] = desc


# QUOTED OR NOT (Jack 2026-10-17: '"Dark" email should be clear on if the
# event isn't quoted vs if it's missing an end date'). A start: item is a
# mention event with no start time, and the bot still quotes those (to the
# market's close, behind the live gates): "dark 30.0h" read as unquoted while
# KXTRUMPMENTIONB-26OCT19 rested on every market. Each line now leads with
# what the bot is doing -- QUOTED on N of M markets / NOT QUOTED -- read off
# the IMM dashboard's data object (rebuilt every 10 min), then what is missing.
def load_quote_state(path: Optional[str] = None) -> Dict[str, dict]:
    """"ev:<event>" / "ser:<series>" -> {"q": markets with a side quoted, "n":
    markets, "dec": Counter of the bot's decisions on the unquoted ones,
    "cutoff": the earliest cutoff (epoch) among the quoted ones}, from the
    IMM dashboard's data object. {} when it cannot be read."""
    path = path or os.path.join(imm.STATUS_DIR, "dashboard", "imm_dashboard.html")
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
        i = html.index("const D = ") + len("const D = ")
        D, _ = json.JSONDecoder().raw_decode(html, i)
    except (OSError, ValueError) as e:
        log(f"[WATCH] ! no dashboard quote state ({e!r})")
        return {}
    out: Dict[str, dict] = {}
    for _t, m in (D.get("markets") or {}).items():
        quoted = ((m.get("cur") or {}).get("q") or 0) > 0
        for key in ("ev:" + str(m.get("ev") or ""), "ser:" + str(m.get("ser") or "")):
            st = out.setdefault(key, {"q": 0, "n": 0, "dec": Counter(), "cutoff": None})
            st["n"] += 1
            if quoted:
                st["q"] += 1
                c = m.get("cutoff")
                if c and (st["cutoff"] is None or c < st["cutoff"]):
                    st["cutoff"] = c
            elif m.get("dec"):
                st["dec"][m["dec"]] += 1
    return out


def missing_of(key: str, it: dict) -> str:
    """What the item lacks, in words."""
    kind = key.partition(":")[0]
    if kind == "start":
        return "its start time"
    if kind == "new":
        return "a place on the allowlist (a new family: your call)"
    g = gap_kind(it.get("what") or "")
    return {"election": "its election day", "award": "its date row"}.get(g, "a config fix")


def quote_label(key: str, qs: Optional[Dict[str, dict]]) -> Tuple[str, Optional[dict]]:
    """("QUOTED on N of M markets" / "NOT QUOTED (...)" / "QUOTE STATE
    UNKNOWN", the state) for an item, from load_quote_state."""
    kind, _, tick = key.partition(":")
    if not qs:
        return "QUOTE STATE UNKNOWN", None
    st = qs.get(("ser:" if kind == "new" else "ev:") + tick)
    if st is None:
        return "NOT QUOTED (not in the bot's book)", None
    if st["q"]:
        return f"QUOTED on {st['q']} of {st['n']} markets", st
    why = st["dec"].most_common(1)[0][0] if st["dec"] else ""
    return (f"NOT QUOTED (0 of {st['n']} markets" + (f"; bot: {why}" if why else "") + ")",
            st)


def _fmt_et(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(
            imm.ET).strftime("%b %d %I:%M %p ET")
    except (TypeError, ValueError, OverflowError):
        return "?"


def alert_body(due: List[Tuple[str, dict, float]],
               qs: Optional[Dict[str, dict]] = None) -> Tuple[str, str]:
    labels = {key: quote_label(key, qs) for key, _, _ in due}
    quoted = [it for key, it, _ in due if labels[key][0].startswith("QUOTED")]
    unknown = [it for key, it, _ in due if labels[key][0].startswith("QUOTE STATE")]
    dark = [it for key, it, _ in due if labels[key][0].startswith("NOT QUOTED")]
    whole = sum(it.get("est_total") or 0.0 for _, it, _ in due)
    parts = []
    if dark:
        parts.append(f"{len(dark)} not quoted (~${sum(i['est'] for i in dark):,.0f}/day)")
    if quoted:
        parts.append(f"{len(quoted)} quoted but missing a start time or date "
                     f"(~${sum(i['est'] for i in quoted):,.0f}/day)")
    if unknown:
        parts.append(f"{len(unknown)} quote state unknown "
                     f"(~${sum(i['est'] for i in unknown):,.0f}/day)")
    subject = ("IMM watch: " + "; ".join(parts)
               + (f" (~${whole:,.0f} over their programs)" if whole else ""))
    lines = [subject, ""]
    for key, it, dark_s in sorted(due, key=lambda d: -d[1]["est"]):
        src = it.get("est_src") or "pool proxy"
        evs = it.get("est_events") or []
        worth = f"~${it['est']:,.2f}/day"
        if evs and it.get("est_total") is not None:
            worth += (f", ~${it['est_total']:,.2f} over the rest of its program ("
                      f"{max(e[3] for e in evs):.1f} of {max(e[4] for e in evs):.1f} "
                      f"days, ends {_fmt_end(max(e[5] for e in evs))})")
        label, st = labels[key]
        lines.append(f"- {label} -- missing {missing_of(key, it)}. {worth} ({src}); "
                     f"flagged {dark_s / 60:.1f}h ago  {it['what']}")
        if st is not None and st["q"] and key.startswith("start:"):
            lines.append("    quoting now" + (f", until {_fmt_et(st['cutoff'])}" if st["cutoff"]
                                              else "") +
                         ", behind the live gates; a start time makes it stop 10 min "
                         "before the event")
        if it.get("title"):
            lines.append(f"    what it is: {it['title']}")
        if len(evs) > 1:
            per = "; ".join(f"{e[0]} ~${e[1]:,.2f}/day = ~${e[2]:,.2f} "
                            f"({e[3]:.1f}d left)" for e in evs[:4])
            lines.append(f"    per event: {per}"
                         + (f"; +{len(evs) - 4} more" if len(evs) > 4 else ""))
        lines.append(f"    fix: {it['fix']}")
    lines += ["", "(QUOTED / NOT QUOTED = what the bot was doing at the last dashboard "
                  "build, every 10 min; for a QUOTED item the $ is about what it earns "
                  "now, and the missing date decides when it stops. "
                  "est = the bot's own reward estimator, as if the gap were "
                  "fixed: its standard ladder against each market's live book, "
                  "the forward $/day over its size schedule, summed over the "
                  "item's paying markets -- no $1/market floor applied. The "
                  "program total is that $/day over the days each market can "
                  "still accrue (to the program end or its close), per event. "
                  f"'pool proxy' = pool $/day x {GAP_CAPTURE:.3f} where no book "
                  f"was read. Threshold ${GAP_ALERT_MIN_DPD:g}/day, dark >= "
                  f"{GAP_ALERT_AFTER_MIN:g} min)"]
    return subject, "\n".join(lines)

# REAL ESTIMATES (Jack 2026-10-09, of the dark email's "$41/day" for five
# Israeli minister events -- 102 markets paying $40 each per 2-day program,
# which the bot projected at <= ~$0.53/day a market: "in the dark email, try
# to apply what the actual rewards would be not just a 2% proxy"). Each
# paying item the alert pass sees gets the bot's own estimate: an offline,
# log-only IncentiveMarketMaker (the imm_quote_gaps path) builds each market's
# meta, clears the cutoff (the "if fixed" view: a gap's cutoff is the
# fail-closed sentinel) and runs _estimate_candidate_yield on the live book;
# the item's est is the sum of floor_dollars_per_day -- the forward $/day the
# bot projects over its size schedule (hour / quiet / Saturday multipliers),
# its admission figure -- with NO $1 floor ("dont need to apply the $1 per
# market floor"). Cached per item for EST_CACHE_HOURS, at most EST_MAX_BOOKS
# book reads a run (highest pool first); an unread market keeps the proxy and
# the label says how many. Research still ranks on the pool proxy (it runs
# before any book is read).
EST_MAX_BOOKS = int(os.environ.get("IMM_WATCH_EST_MAX_BOOKS", "200"))
EST_CACHE_HOURS = float(os.environ.get("IMM_WATCH_EST_CACHE_H", "2"))


def _estimator(client):
    """An offline bot for the estimator: never live, alerts log only."""
    bot = imm.IncentiveMarketMaker(client, live=False)
    bot.alerter.enabled = False
    return bot


def _item_markets(key: str, progs: Dict[str, dict]) -> List[str]:
    """The paying market tickers behind an alert item."""
    kind, _, name = key.partition(":")
    if kind == "new":
        return sorted(t for t in progs if imm.series_of(t) == name)
    return sorted(t for t in progs if event_key(t) == name)


def _program_days(p: dict, now: datetime) -> Tuple[float, float, str]:
    """(days left, program length in days, end ISO) of a raw program."""
    start = imm.parse_iso_utc(p.get("start_date", ""))
    end = imm.parse_iso_utc(p.get("end_date", ""))
    if end is None:
        return 1.0, 1.0, ""
    left = max((end - now).total_seconds() / 86400.0, 0.0)
    length = max((end - start).total_seconds() / 86400.0, left) if start else left
    return left, length, end.isoformat()


def real_estimates(client, items: Dict[str, dict], progs: Dict[str, dict],
                   state: dict, now_ts: float, max_books: int = None,
                   make_bot=None) -> List[str]:
    """Replace the pool-proxy est of each paying, un-held item with the bot's
    own estimate (see REAL ESTIMATES), and add what it is worth over the rest
    of its program, per event (Jack 2026-10-09: "it should also show $ per
    event (for the whole duration)"): each market's $/day times the days it
    can still accrue -- the bot's own _quotable_days, to the program end or
    the market's close -- the same total its $1-floor test reads. Sets
    est, est_src, est_total and est_events ([event, $/day, $ total, days
    left, program days, end ISO], largest total first). Mutates items;
    caches in state["est_cache"]; returns log lines."""
    max_books = EST_MAX_BOOKS if max_books is None else max_books
    make_bot = make_bot or _estimator
    cache = {k: v for k, v in (state.get("est_cache") or {}).items() if k in items}
    notes: List[str] = []
    bot = programs = None
    details: Dict[str, dict] = {}
    books = 0
    now = datetime.fromtimestamp(now_ts, timezone.utc)
    for key, it in sorted(items.items(), key=lambda kv: -kv[1]["est"]):
        markets = _item_markets(key, progs)
        if it.get("hold") or not markets:
            continue
        c = cache.get(key)
        if c and now_ts - float(c.get("at") or 0) < EST_CACHE_HOURS * 3600 \
                and int(c.get("n") or 0) == len(markets) and "total" in c:
            it.update(est=c["est"], est_src=c["src"], est_total=c["total"],
                      est_events=c["events"])
            continue
        if bot is None:
            bot = make_bot(client)
            programs = bot.fetch_programs() or {}
        rate, best, read, proxied = 0.0, 0.0, 0, 0
        evs: Dict[str, dict] = {}
        for t in sorted(markets, key=lambda t: -ifa._dollars_per_day(progs[t])):
            left, length, end = _program_days(progs[t], now)
            v = ifa._dollars_per_day(progs[t]) * GAP_CAPTURE     # the fallback
            days, ok = left, False
            info = programs.get(t)
            ev = event_key(t)
            if info is not None and books < max_books:
                if ev not in details:
                    try:
                        ms = (client.get("/markets", params={"event_ticker": ev,
                                                             "limit": 1000})
                              or {}).get("markets") or []
                    except Exception:
                        ms = []
                    details[ev] = {m.get("ticker"): m for m in ms}
                m = details[ev].get(t)
                if m is not None:
                    books += 1
                    try:
                        meta = imm_quote_gaps.build_meta(bot, t, info, m, now)
                        meta.cutoff = None            # the "if fixed" view
                        ok = bot._estimate_candidate_yield(meta, [])
                    except Exception as e:
                        notes.append(f"estimate {t} failed: {e!r}")
                        ok = False
                    if ok:
                        v = float(meta.floor_dollars_per_day or 0.0)
                        days = imm._quotable_days(meta, now)
            if ok:
                read, best = read + 1, max(best, v)
            else:
                proxied += 1
            rate += v
            e = evs.setdefault(ev, {"rate": 0.0, "total": 0.0, "left": 0.0,
                                    "length": 0.0, "end": ""})
            e["rate"] += v
            e["total"] += v * days
            e["left"] = max(e["left"], days)
            e["length"] = max(e["length"], length)
            e["end"] = max(e["end"], end)
        src = (f"bot estimate, {read} market{'s' if read != 1 else ''}"
               + (f", best ${best:,.2f}/day" if read > 1 else "")
               + (f"; {proxied} at pool proxy" if proxied else ""))
        if not read:
            src = "pool proxy"
        it["est"], it["est_src"] = round(rate, 2), src
        it["est_total"] = round(sum(e["total"] for e in evs.values()), 2)
        it["est_events"] = sorted(
            ([ev, round(e["rate"], 2), round(e["total"], 2), round(e["left"], 2),
              round(e["length"], 2), e["end"]] for ev, e in evs.items()),
            key=lambda r: -r[2])
        cache[key] = {"at": now_ts, "n": len(markets), "est": it["est"], "src": src,
                      "total": it["est_total"], "events": it["est_events"]}
        notes.append(f"est {key}: ${it['est']:,.2f}/day, ${it['est_total']:,.2f} "
                     f"over the rest of its program ({src})")
    state["est_cache"] = cache
    return notes


# RESEARCH ON ARRIVAL (Jack 2026-10-07: "instead of separate gap-fixer
# routine, why not just research when a new series enrolls?" -> "yes switch").
# Each run collects what only research can fix -- an election-family event
# with no verified voting day, an awards event with no hand row, a broadcast
# mention event none of the automatic resolvers (WH schedule, RNC events,
# TVmaze) can place -- and hands the due ones to ONE headless Claude Code run
# (`claude -p --restricted`) with a fixed tool allowlist: imm_rows.py (add /
# lookup), imm_earnings_overrides.py --set, web search and fetch. Anything
# else is denied, not prompted (the imm-gap-fixer routine's first run sat on
# its first command's prompt), so a run cannot stall or touch the bot.
# A target is researched on first sight, then again RESEARCH_BACKOFF_H after
# each unresolved attempt (answers are often not published yet: a rally page,
# the WH schedule a day ahead), at most RESEARCH_MAX_ATTEMPTS times; a
# judgment call (not a vote, a new family) is not retried.
RESEARCH_ENABLE = os.environ.get("IMM_WATCH_RESEARCH", "1") == "1"
RESEARCH_MAX_TARGETS = int(os.environ.get("IMM_WATCH_RESEARCH_MAX", "8"))
RESEARCH_TIMEOUT_SECS = int(os.environ.get("IMM_WATCH_RESEARCH_TIMEOUT", "720"))
RESEARCH_BUDGET_USD = float(os.environ.get("IMM_WATCH_RESEARCH_BUDGET_USD", "3"))
RESEARCH_BACKOFF_H = (2.0, 4.0, 8.0)        # after attempt 1, 2, 3+
# a run that returned no result for a target (no RESULT_JSON entry, and no
# row landed) is a failed run, not "not published yet": retry sooner
RESEARCH_ERROR_RETRY_H = float(os.environ.get("IMM_WATCH_RESEARCH_ERROR_RETRY_H", "0.5"))
RESEARCH_MAX_ATTEMPTS = int(os.environ.get("IMM_WATCH_RESEARCH_ATTEMPTS", "6"))
# alert on a researchable item only once research gave up on it (a judgment
# call, or unresolved twice) -- or when it has simply been dark this long
ALERT_STALE_HOURS = float(os.environ.get("IMM_GAP_ALERT_STALE_HOURS", "6"))
KL_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDE_FALLBACK = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "Microsoft", "WinGet", "Packages",
    "Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe", "claude.exe")
RESEARCH_TOOLS = ("Bash", "WebSearch", "WebFetch")
RESEARCH_ALLOWED = ("Bash(python imm_rows.py:*)",
                    "Bash(python imm_earnings_overrides.py --set:*)",
                    "WebSearch", "WebFetch")
# LOGIN FAILURES (Jack 2026-10-12: "send an email right away when research
# fails on a login error, instead of waiting until items give up"). From 17:50Z
# that day every run ended "Failed to authenticate: OAuth session expired and
# could not be refreshed". apply_results filed each target "error" and spent a
# try, and hold_researched kept the items quiet until research gave up (6 tries
# 30 min apart) or they had been dark ALERT_STALE_HOURS -- longer than the 5.5h
# KXWORLDNEWSMENTION-26OCT12 program it left without a start. Now a run that
# dies on the login emails at once, again every RESEARCH_LOGIN_REALERT_H while
# it lasts, and costs no target a try (none was researched).
RESEARCH_LOGIN_RE = re.compile(r"failed to authenticate|oauth (?:session|token)|"
                               r"not logged in|run /login|invalid api key|"
                               r"authentication_error", re.I)
RESEARCH_LOGIN_REALERT_H = float(os.environ.get("IMM_WATCH_LOGIN_REALERT_H", "6"))
# RESEARCH TOKEN (Jack 2026-10-12: "do it for me" -- a login the interactive
# one cannot take down). `claude setup-token` signs in once and prints a
# one-year OAuth token. Saved in RESEARCH_TOKEN_FILE (Jack's profile, outside
# the repo), it reaches the research runs ONLY, as CLAUDE_CODE_OAUTH_TOKEN in
# the subprocess env, where it outranks the stored /login credentials that
# expired on 10/12. A user-wide variable would put every Claude session on it.
# ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN outrank it and bill the API per
# token, so a run with the token drops them. No file, or no token in it = the
# stored login, as before. The token itself is never logged.
RESEARCH_TOKEN_FILE = os.environ.get(
    "IMM_WATCH_CLAUDE_TOKEN_FILE",
    os.path.join(os.path.expanduser("~"), ".claude", "imm-research-token.txt"))
_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")
# the token lasts a year from when it was made, which the file's mtime stands
# in for: remind from day RESEARCH_TOKEN_RENEW_DAYS, weekly, until replaced
RESEARCH_TOKEN_RENEW_DAYS = float(os.environ.get("IMM_WATCH_TOKEN_RENEW_DAYS", "350"))
RESEARCH_TOKEN_REMIND_DAYS = 7.0


def gap_kind(msg: str) -> str:
    """'election' / 'award' for the config gaps a researched row fixes, else
    '' (a ticker shape, a release guard -- code, not research)."""
    m = msg or ""
    if "election day" in m:
        return "election"
    if "hand-table row" in m or "no usable date" in m:
        return "award"
    return ""


def live_gaps(gaps: dict) -> dict:
    """The bot's config gaps minus the ones nothing should act on: an election
    / award gap a row written since the bot's last config_gaps.json write
    already covers (the dating pass, research, a hand fix -- the bot drops it
    at its next refresh, 2026-10-09), and a series carved out of the election
    family (2026-10-08, Jack on KXPUBLICTALARICO: "should not be quoted"; it
    is never dated). Both research and the alert read this."""
    imm.load_election_dates_extra()
    imm.load_awards_dates_extra()
    out = {}
    for ev, g in (gaps or {}).items():
        kind = gap_kind(g.get("msg") or "")
        if kind == "election" and imm.election_cutoff_utc(ev) is not None:
            continue
        if kind == "election" and \
                (g.get("series") or imm.series_of(ev)) in imm.ELECTION_EXCLUDE:
            continue
        if kind == "award" and imm.awards_event_start(ev, None, dates_only=True) is not None:
            continue
        out[ev] = g
    return out


def research_targets(client, gaps: dict, now: datetime, dry: bool) -> Dict[str, dict]:
    """{key: {kind, event, series, msg}} for what only research can fix.
    Broadcast mention events the automatic resolvers CAN place are written
    here and now (as the overrides task's Phase 4 would), not researched."""
    targets: Dict[str, dict] = {}
    imm.load_file_event_overrides()       # a --set since import is not a target
    for ev, g in sorted(live_gaps(gaps).items()):   # nor a fixed / excluded gap
        kind = gap_kind(g.get("msg") or "")
        if kind:
            targets[f"gap:{ev}"] = {"kind": kind, "event": ev,
                                    "series": g.get("series") or imm.series_of(ev),
                                    "msg": g.get("msg") or ""}
    resolved = []
    for ev, series in ieo.discover_broadcast_mention_events(client, now):
        if ev in imm.EVENT_START_OVERRIDES:
            continue
        iso, src, detail, title = ieo.auto_broadcast_start(client, ev, series)
        if iso:
            resolved.append((ev, iso, src, detail))
            log(f"[WATCH] start {ev} = {iso} [{src}] {detail[:70]}")
            continue
        targets[f"start:{ev}"] = {"kind": "start", "event": ev, "series": series,
                                  "msg": f"no start time ({title})"}
    if resolved and not dry:
        data = ieo.load_file()
        for ev, iso, _src, _det in resolved:
            data[ev] = iso
        ieo.write_file(data)
        ieo.record_meta(ieo.provenance_batch([], [], [], resolved), data)
    return targets


def research_due(rstate: Dict[str, dict], targets: Dict[str, dict], now_ts: float,
                 est: Dict[str, float] = None) -> List[str]:
    """The target keys to research now: never tried, or the backoff after the
    last unresolved try has run out; not judgment calls, not past
    RESEARCH_MAX_ATTEMPTS; and paying -- with `est`, a target with no active
    program ($0) waits until one lights (the bot lists unpaid election
    events as gaps too: the 02:20Z 10/08 KX*OUTPERFORMRCP batch). Highest
    estimated value first, at most RESEARCH_MAX_TARGETS."""
    due = []
    for key in targets:
        if est is not None and est.get(key, 0.0) <= 0:
            continue
        r = rstate.get(key) or {}
        n = int(r.get("attempts") or 0)
        if r.get("status") == "judgment" or n >= RESEARCH_MAX_ATTEMPTS:
            continue
        if n:
            wait = (RESEARCH_ERROR_RETRY_H if r.get("status") == "error" else
                    RESEARCH_BACKOFF_H[min(n - 1, len(RESEARCH_BACKOFF_H) - 1)]) * 3600
            if now_ts - float(r.get("last") or 0) < wait:
                continue
        due.append(key)
    due.sort(key=lambda k: -(est or {}).get(k, 0.0))
    return due[:RESEARCH_MAX_TARGETS]


_HOW = {
    "election": ("the VOTING DAY", "python imm_rows.py election add {ev} YYYY-MM-DD "
                 "[--zone IANA/Zone] --source \"...\" --source \"...\" --by family-watch-research "
                 "--note \"what the vote is\"   (--zone only for a jurisdiction EAST of US "
                 "Eastern time, e.g. Asia/Jerusalem, Europe/Madrid)"),
    "award": ("the FIRST narrowing of the field for that edition (nominations, "
              "shortlist or finalists announcement -- not the ceremony)",
              "python imm_rows.py award add {ev} YYYY-MM-DD --source \"...\" "
              "--source \"...\" --by family-watch-research"),
    "start": ("when the SPEAKER the market is about starts speaking -- for a rally or "
              "speech the scheduled remarks time ('7:00 PM: Remarks Begin'), NOT doors "
              "or program start; for a show, its air time (if sources give the speaker "
              "only a window or disagree by under an hour, the EARLIEST credible time -- "
              "the bot stands down at the start, so late is the costly error)",
              "python imm_earnings_overrides.py --set {ev} \"YYYY-MM-DDTHH:MM:00-04:00\"   "
              "(-04:00 through Oct 31 2026, -05:00 from Nov 1 2026)"),
}


def research_prompt(items: List[Tuple[str, dict, str]]) -> str:
    """The headless run's instructions for [(key, target, Kalshi context)]."""
    lines = [
        "You fill missing dates and start times for Jack's Kalshi incentive "
        "market-maker bot. For each target below, research the correct value "
        "and write it with the exact command shown, or classify it.",
        "",
        "Rules:",
        "- TWO independent sources you actually fetched (official authority -- "
        "election commission, secretary of state, government, the award body, the "
        "White House schedule at https://media-cdn.factba.se/rss/json/trump/"
        "calendar-full.json, events.gop.com -- or reputable press / Wikipedia / "
        "Ballotpedia). Put the URLs in --source. If sources disagree or you have "
        "fewer than two, do not write: status unresolved.",
        # 2026-10-09 (Jack, of five Israeli minister markets research had
        # filed as judgment: "why are these not able to quote?"): the row is
        # the day the bot must be out by, so a market decided by WHAT A VOTE
        # DECIDES (who becomes a minister after the 10/27 Knesset election)
        # takes that vote's day too; "appointment" alone had read as no vote
        "- Election targets: the row is the day of the scheduled VOTE the "
        "market hinges on -- the bot stops quoting from that day. Write it "
        "when the market is decided by a vote (an election, referendum, "
        "chamber control, vote share) OR by what a named upcoming vote "
        "decides: who becomes prime minister or a minister, or forms the "
        "government, after that election -- its day is the information "
        "event. A rally, debate, court case, retirement, or an appointment "
        "or announcement that hinges on no scheduled vote is NOT a vote: "
        "write nothing, status judgment.",
        "- The only commands you may run are the ones shown, plus "
        "`python imm_rows.py election lookup EVENT` (read-only: Kalshi's own "
        "wording). If a command prints REFUSED, do not work around it.",
        "",
        "Targets:",
    ]
    for i, (key, t, ctx) in enumerate(items, 1):
        what, cmd = _HOW[t["kind"]]
        lines += [f"{i}. KEY {key} -- {t['kind']}: find {what}.",
                  f"   bot says: {t['msg']}",
                  f"   Kalshi: {ctx}",
                  f"   write with: {cmd.format(ev=t['event'])}"]
    lines += [
        "",
        "Finish with ONE line, nothing after it:",
        "RESULT_JSON: {\"<KEY>\": {\"status\": \"written\"|\"judgment\"|"
        "\"unresolved\", \"note\": \"value + sources, or why\"}, ...}",
        "covering every KEY above.",
    ]
    return "\n".join(lines)


def parse_research(text: str) -> Dict[str, dict]:
    """{key: {status, note}} from the run's RESULT_JSON line ({} if absent)."""
    for line in reversed((text or "").splitlines()):
        line = line.strip().strip("`")
        if line.startswith("RESULT_JSON:"):
            try:
                out = json.loads(line[len("RESULT_JSON:"):].strip())
            except ValueError:
                return {}
            return {k: v for k, v in out.items() if isinstance(v, dict)}
    return {}


def research_token() -> str:
    """The research token in RESEARCH_TOKEN_FILE, or '' (no file, unreadable,
    or no token in it). The first sk-ant-... run counts, so a paste of
    setup-token's whole output works as well as the bare token."""
    try:
        with open(RESEARCH_TOKEN_FILE, encoding="utf-8-sig") as f:
            raw = f.read()
    except (OSError, ValueError):
        return ""
    m = _TOKEN_RE.search(raw)
    if m:
        return m.group(0)
    raw = raw.strip()
    return raw if raw and not any(c.isspace() for c in raw) else ""


def research_auth() -> str:
    """Which login the research runs use, for the log and the emails."""
    if research_token():
        return "token file"
    if os.path.exists(RESEARCH_TOKEN_FILE):
        return "stored login (no token in the token file)"
    return "stored login"


def research_env() -> Dict[str, str]:
    """The research subprocess's env: the token when the file holds one."""
    env = dict(os.environ)
    token = research_token()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def run_research(prompt: str) -> Tuple[str, float, str]:
    """(result text, cost USD, error) from one headless Claude Code run."""
    exe = shutil.which("claude") or CLAUDE_FALLBACK
    # --restricted: no user hooks / settings, only the tools --tools names;
    # --strict-mcp-config: no MCP servers. Probed 10/08: a python write and a
    # non-allowlisted python command are denied (read-only git still runs)
    cmd = [exe, "-p", prompt, "--output-format", "json", "--no-session-persistence",
           "--max-budget-usd", f"{RESEARCH_BUDGET_USD:g}", "--restricted",
           "--strict-mcp-config",
           "--tools", *RESEARCH_TOOLS, "--allowedTools", *RESEARCH_ALLOWED]
    try:
        p = subprocess.run(cmd, cwd=KL_DIR, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=RESEARCH_TIMEOUT_SECS, env=research_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", 0.0, f"{type(e).__name__}: {e}"
    try:
        js = json.loads(p.stdout or "{}")
    except ValueError:
        return p.stdout or "", 0.0, f"exit {p.returncode}: non-JSON output"
    return (js.get("result") or ""), float(js.get("total_cost_usd") or 0.0), (
        "" if p.returncode == 0 else f"exit {p.returncode}: {(p.stderr or '')[:200]}")


def research_login_failure(text: str, err: str) -> str:
    """The login error a FAILED research run died on, else ''. A clean exit
    is never a login failure, whatever its text says."""
    if not err:
        return ""
    for line in reversed(f"{text}\n{err}".splitlines()):
        if RESEARCH_LOGIN_RE.search(line):
            return line.strip()[:200]
    return ""


def alert_login_failure(state: dict, why: str, due: List[str], est: Dict[str, float],
                        now_ts: float) -> bool:
    """Email a research login failure: on the first failed run, then every
    RESEARCH_LOGIN_REALERT_H while it lasts (state["research_login"]; a send
    that fails is retried next run). True when an email went out."""
    rec = state.setdefault("research_login", {"first": now_ts})
    last = rec.get("alerted")
    if last is not None and now_ts - float(last) < RESEARCH_LOGIN_REALERT_H * 3600:
        return False
    since = datetime.fromtimestamp(float(rec["first"]), timezone.utc)
    lines = [f"The family watch's research run cannot log in to Claude Code "
             f"(failing since {since:%Y-%m-%d %H:%M}Z):",
             f"  {why}",
             "",
             f"Nothing gets researched until the login works. Waiting ({len(due)}):"]
    lines += [f"  - {k}  (~${est.get(k, 0.0):.2f}/day, pool proxy)" for k in due]
    if research_token():
        fix = ("Fix: the research token in " + RESEARCH_TOKEN_FILE + " was rejected "
               "(expired or revoked). In a terminal run `claude setup-token`, sign "
               "in, and paste the new token over the file's contents.")
    else:
        fix = ("Fix: open a terminal, run `claude`, and sign in again with /login. "
               "Sturdier: run `claude setup-token` and paste its one-year token into "
               + RESEARCH_TOKEN_FILE + " -- the research runs then use that instead.")
    lines += ["", fix,
              "The watch runs the WinGet claude.exe as this Windows user; its next run "
              "(every 30 min) retries on its own. Test any time: python "
              "imm_family_watch.py --check-login",
              f"This repeats every {RESEARCH_LOGIN_REALERT_H:g}h while the login "
              f"keeps failing. Log: run-logs\\incentive-mm\\family-watch.log"]
    if not imm.Alerter("IMM-WATCH", live=True).send_message(
            "\n".join(lines), "IMM-WATCH research DOWN: Claude Code login failed"):
        return False
    rec["alerted"] = now_ts
    return True


def token_renewal_due(state: dict, now_ts: float) -> Optional[float]:
    """The token file's age in days when a renewal reminder is due, else None."""
    if not research_token():
        return None
    try:
        age = (now_ts - os.path.getmtime(RESEARCH_TOKEN_FILE)) / 86400
    except OSError:
        return None
    last = state.get("token_reminded")
    if age < RESEARCH_TOKEN_RENEW_DAYS or (
            last is not None and now_ts - float(last) < RESEARCH_TOKEN_REMIND_DAYS * 86400):
        return None
    return age


def remind_token_renewal(state: dict, now_ts: float, dry: bool) -> bool:
    """Email the renewal reminder when due. True when an email went out."""
    age = token_renewal_due(state, now_ts)
    if age is None:
        return False
    body = (f"The family watch's research token ({RESEARCH_TOKEN_FILE}) was saved "
            f"{age:.0f} days ago, and `claude setup-token` tokens last a year. Renew it "
            f"before it lapses: in a terminal run `claude setup-token`, sign in, and "
            f"paste the new token over the file's contents. Then test it: python "
            f"imm_family_watch.py --check-login\n"
            f"This repeats weekly until the file is replaced.")
    log("[WATCH] research token renewal due:\n" + body)
    if dry or not imm.Alerter("IMM-WATCH", live=True).send_message(
            body, "IMM-WATCH renew the research token"):
        return False
    state["token_reminded"] = now_ts
    return True


def check_login() -> int:
    """--check-login: one tiny run on the research login, so a new token can
    be tested without waiting for a target. Prints OK or the error; 0 = OK."""
    auth = research_auth()
    text, cost, err = run_research("Reply with the single word OK. Use no tools.")
    if err or "OK" not in (text or ""):
        why = research_login_failure(text, err) or err or (text or "")[-200:]
        print(f"research login FAILED via {auth}: {why}")
        return 1
    print(f"research login OK via {auth} (${cost:.2f})")
    return 0


def prune_research(rstate: Dict[str, dict], targets: Dict[str, dict],
                   now_ts: float) -> Dict[str, dict]:
    """The research records to keep: every current target's (stamped seen
    now), and an absent one's for STATE_GRACE_HOURS after it was last seen --
    a judgment call must not be re-researched because a bot restart hid its
    gap for a run."""
    out = {}
    for k, v in rstate.items():
        if k in targets:
            out[k] = dict(v, seen=now_ts)
        elif now_ts - float(v.get("seen") or v.get("last") or 0) < STATE_GRACE_HOURS * 3600:
            out[k] = v
    return out


def target_fixed(t: dict) -> bool:
    """Whether a research target's row / start time is now in place."""
    ev = t["event"]
    if t["kind"] == "election":
        return imm.election_cutoff_utc(ev) is not None
    if t["kind"] == "award":
        return imm.awards_event_start(ev, None, dates_only=True) is not None
    if t["kind"] == "start":
        return ev in imm.EVENT_START_OVERRIDES
    return False


def apply_results(rstate: Dict[str, dict], due: List[str], targets: Dict[str, dict],
                  res: Dict[str, dict], err: str, now_ts: float) -> List[str]:
    """Record one research run for each due target. A target the RESULT_JSON
    line does not cover is checked against the rows themselves (2026-10-10
    02:24Z: the run wrote KXALBERTAREFS and KXCZECHSENATE but printed no
    result line, and all 8 were filed "unresolved -- no result"): a row in
    place is "written"; otherwise the run failed for it -- "error", retried
    after RESEARCH_ERROR_RETRY_H."""
    imm.load_election_dates_extra()
    imm.load_awards_dates_extra()
    imm.load_file_event_overrides()
    notes = []
    for k in due:
        r = rstate.setdefault(k, {})
        r["attempts"] = int(r.get("attempts") or 0) + 1
        r["last"] = r["seen"] = now_ts
        got = res.get(k) or {}
        status, note = got.get("status"), got.get("note") or ""
        if status not in ("written", "judgment", "unresolved"):
            if target_fixed(targets[k]):
                status, note = "written", "row in place after the run (no result line)"
            else:
                status, note = "error", (err or "no result line for this target")
        r["status"], r["note"] = status, note[:300]
        notes.append(f"{k}: {status} -- {r['note']}")
    return notes


def research_pass(client, gaps: dict, state: dict, dry: bool) -> Tuple[Dict[str, dict], List[str]]:
    """Collect targets, research the due ones, update state["research"].
    Returns (targets, summary lines)."""
    now_ts = time.time()
    targets = research_targets(client, gaps, datetime.now(timezone.utc), dry)
    rstate = prune_research(state.get("research") or {}, targets, now_ts)
    pools = event_pools(ifa.fetch_active_programs(client)) if targets else {}
    est = {k: pools.get(t["event"], 0.0) * GAP_CAPTURE for k, t in targets.items()}
    due = research_due(rstate, targets, now_ts, est)
    notes = []
    if due and RESEARCH_ENABLE and not dry:
        items = [(k, targets[k], imm_rows.lookup(targets[k]["event"]).replace("\n", " / ")[:900])
                 for k in due]
        auth = research_auth()
        text, cost, err = run_research(research_prompt(items))
        res = parse_research(text)
        login = "" if res else research_login_failure(text, err)
        if login:
            # nothing was researched: no target spends a try, all stay due
            notes.append(f"research: LOGIN FAILED via {auth} ({login}); "
                         f"{len(due)} target(s) wait")
            if alert_login_failure(state, login, due, est, now_ts):
                notes.append("research: login failure emailed")
        else:
            state.pop("research_login", None)
            if not res:
                notes.append(f"research: no RESULT_JSON line; ends {(text or err)[-240:]!r}")
            notes += apply_results(rstate, due, targets, res, err, now_ts)
            notes.append(f"research: {len(due)} target(s), ${cost:.2f}"
                         + (f", {err}" if err else "") + f" [{auth}]")
    elif due:
        notes.append(f"research due ({'dry' if dry else 'disabled'}): {', '.join(due)}")
    state["research"] = rstate
    return targets, notes


MODEL_PASS_ERR_HOURS = 24.0


def model_coverage_pass(progs: Dict[str, dict], state: dict, dry: bool,
                        summary: List[str]) -> None:
    """Pass 5 (Jack 2026-10-09): every paying market in a per-entity-modeled
    family must be modeled (imm_model_coverage). A gap is emailed on first
    sight and re-sent daily while open; the pass's own failure is emailed
    too (at most daily), so a broken check can never go quiet."""
    import imm_model_coverage as mcov
    now_ts = time.time()
    try:
        gaps = mcov.find_gaps(progs, now_ts)
        first = "model_gaps" not in state
        due_m, state["model_gaps"] = mcov.due(state.get("model_gaps") or {}, gaps,
                                              now_ts, baseline_once=first)
        if due_m:
            subject, text = mcov.body(due_m)
            log("[WATCH] model coverage alert:\n" + text)
            if not dry:
                imm.Alerter("IMM-WATCH", live=True).send_message(text, subject)
        summary.append(f"model gaps {len(gaps)}, alerted {len(due_m)}")
    except Exception as e:                           # noqa: BLE001 - emailed below
        log(f"[WATCH] ! model coverage pass failed: {e!r}")
        last = state.get("model_pass_err_sent") or 0
        if not dry and now_ts - float(last) >= MODEL_PASS_ERR_HOURS * 3600:
            imm.Alerter("IMM-WATCH", live=True).send_message(
                f"The IMM family watch's model coverage pass failed: {e!r}\n"
                f"Until it is fixed, new markets that are not modeled are NOT "
                f"being reported. Log: run-logs\\incentive-mm\\family-watch.log",
                "IMM-WATCH model coverage pass FAILED")
            state["model_pass_err_sent"] = now_ts


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry", action="store_true", help="report only: no writes, no email")
    ap.add_argument("--no-enroll", action="store_true")
    ap.add_argument("--no-date", action="store_true")
    ap.add_argument("--no-research", action="store_true")
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--no-model", action="store_true", help="skip pass 5 (model coverage)")
    ap.add_argument("--check-login", action="store_true",
                    help="test the research login with one tiny run, then exit")
    args = ap.parse_args(argv)
    if args.check_login:
        return check_login()
    t0 = time.time()
    client = ieo.build_client()
    summary = []
    if not args.no_enroll:
        try:
            enrolled, review = ieo.enroll_new_series(client, dry=args.dry)
            for s, why, sample in enrolled:
                log(f"[WATCH] enrolled {s} [{why}] e.g. {sample}")
            summary.append(f"enrolled {len(enrolled)}")
        except Exception as e:
            log(f"[WATCH] ! enroll pass failed: {e!r}")
    progs = ifa.fetch_active_programs(client)
    state = _load_state()
    try:
        n = load_event_map(client, progs, state)
        if n:
            log(f"[WATCH] event map: read {n} ambiguous market ticker(s)")
    except Exception as e:
        log(f"[WATCH] ! event map read failed ({e!r}); rsplit keys kept")
    if not args.no_date:
        try:
            dated = date_us_general(client, progs, args.dry)
            for ev, added, msg in dated:
                log(f"[WATCH] {'dated' if added else 'undated'} {ev}: {msg}")
            summary.append(f"dated {sum(1 for _, a, _ in dated if a)}")
        except Exception as e:
            log(f"[WATCH] ! dating pass failed: {e!r}")
    try:
        with open(imm.CONFIG_GAPS_FILE, encoding="utf-8") as f:
            gaps = (json.load(f) or {}).get("gaps") or {}
    except (OSError, ValueError):
        gaps = {}
    gaps = live_gaps(gaps)
    targets: Dict[str, dict] = {}
    if not args.no_research:
        try:
            targets, notes = research_pass(client, gaps, state, args.dry)
            for n in notes:
                log(f"[WATCH] {n}")
            summary.append(f"research targets {len(targets)}")
        except Exception as e:
            log(f"[WATCH] ! research pass failed: {e!r}")
        try:
            if remind_token_renewal(state, time.time(), args.dry):
                summary.append("token renewal reminded")
        except Exception as e:
            log(f"[WATCH] ! token renewal check failed: {e!r}")
    if not args.no_alert:
        try:
            audit = ifa.run_audit(client, progs)
            if "baseline_not_allowed" not in state:      # first run: today's
                state["baseline_not_allowed"] = sorted(audit["not_allowed"])
                state["baseline_at"] = datetime.now(timezone.utc).isoformat()
            baseline = set(state["baseline_not_allowed"])
            items = gap_items(progs, gaps, audit["not_allowed"], baseline, targets)
            hold_researched(items, targets, state, time.time())
            try:
                for n in real_estimates(client, items, progs, state, time.time()):
                    log(f"[WATCH] {n}")
            except Exception as e:
                log(f"[WATCH] ! real estimates failed ({e!r}); pool proxy kept")
            due, state["items"] = due_alerts(state.get("items") or {}, items, time.time())
            if due:
                describe(client, due)
                subject, body = alert_body(due, load_quote_state())
                log("[WATCH] alert:\n" + body)
                if not args.dry:
                    imm.Alerter("IMM-WATCH", live=True).send_message(body, subject)
                    # a new family is sent once, then joins the baseline
                    state["baseline_not_allowed"] = sorted(
                        baseline | {k[4:] for k, _, _ in due if k.startswith("new:")})
            summary.append(f"tracking {len(items)}, alerted {len(due)}")
        except Exception as e:
            log(f"[WATCH] ! alert pass failed: {e!r}")
    if not args.no_model:
        model_coverage_pass(progs, state, args.dry, summary)
    if not args.dry:
        _save_state(state)
    log(f"[WATCH] {', '.join(summary)} ({time.time() - t0:.0f}s"
        f"{', dry' if args.dry else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
