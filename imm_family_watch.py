#!/usr/bin/env python3
"""IMM family watch: every 30 minutes (Windows task "KL imm family-watch").

Jack 2026-10-07: "how to ensure new events get quoted when they should fall
under existing quote families e.g. elections, award ceremonies, rain, etc. dont
want to manually catch this going fwd" -> "yes build all 5. $5/day is the right
threshold". Three passes, each fail-safe on its own:

1. ENROLL -- the overrides task's new-series pass
   (imm_earnings_overrides.enroll_new_series: Kalshi's Elections category,
   mention families, the FT/APP consumer families, Carbon Arc sources). It ran
   at 6:45am / 12:45pm / 4:45pm ET only, so an evening batch waited half a day
   (KXSTATELEG, 28 state chambers, lit 22:31Z 10/07).
2. DATE -- an election-family event with no row that is a US race of the 2026
   general election (us_general_vote_ok: Kalshi's Elections category, a US tag,
   rules naming 2026 with vote language and no rally / debate / court / primary
   / runoff / special-election words, expiring Nov 3 2026 - Jan 2027) gets its
   Nov 3 row in election_dates_extra.json: the statute fixes the day (2 U.S.C.
   7 and each state's general-election law). Anything else stays in config
   gaps for the gap-fixer routine.
3. ALERT -- one email per run for what has stayed dark >= GAP_ALERT_AFTER_MIN
   (30 min) and is worth >= GAP_ALERT_MIN_DPD ($5/day of estimated reward =
   pool $/day x GAP_CAPTURE, the median share our quoted markets earn): the live
   bot's config gaps (re-sent daily while open), and series that newly appear
   paying but not allowed -- a new family, which is Jack's call (sent once).

Imports imm_quote_gaps first: it mirrors the launcher's env (blocklist, allow
lists, pilots) before incentive_mm is imported, so "allowed" means what the live
bot means.

  python imm_family_watch.py            # the scheduled run
  python imm_family_watch.py --dry      # report only: no writes, no email
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple

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


def _by_event(progs: Dict[str, dict]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for t in progs:
        out[t.rsplit("-", 1)[0]].append(t)
    return out


def event_pools(progs: Dict[str, dict]) -> Dict[str, float]:
    pools: Dict[str, float] = defaultdict(float)
    for t, p in progs.items():
        pools[t.rsplit("-", 1)[0]] += ifa._dollars_per_day(p)
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
              baseline: set) -> Dict[str, dict]:
    """What is dark for want of admission or a row, keyed for the alert state:
    gap:<event> for the live bot's config gaps, new:<series> for a paying
    not-allowed series outside the baseline (a new family)."""
    pools = event_pools(progs)
    items: Dict[str, dict] = {}
    for ev, g in sorted((gaps or {}).items()):
        items[f"gap:{ev}"] = {
            "est": round(pools.get(ev, 0.0) * GAP_CAPTURE, 2),
            "what": f"{ev}: {g.get('msg') or 'config gap'}",
            "fix": "the gap-fixer routine researches a row; by hand: "
                   "python imm_rows.py election|award add EVENT YYYY-MM-DD "
                   "--source A --source B"}
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
    stays open -- except new:<series>, which is sent once. Items no longer
    present drop out of the state (fixed, or gone)."""
    after_min = GAP_ALERT_AFTER_MIN if after_min is None else after_min
    min_dpd = GAP_ALERT_MIN_DPD if min_dpd is None else min_dpd
    realert_hours = GAP_REALERT_HOURS if realert_hours is None else realert_hours
    due, fresh = [], {}
    for key, it in items.items():
        prev = state_items.get(key) or {}
        first = float(prev.get("first") or now_ts)
        alerted = prev.get("alerted")
        rec = {"first": first, "alerted": alerted, "est": it["est"]}
        dark_min = (now_ts - first) / 60.0
        if it["est"] >= min_dpd and dark_min >= after_min and (
                alerted is None or (not key.startswith("new:")
                                    and now_ts - float(alerted) >= realert_hours * 3600)):
            due.append((key, it, dark_min))
            rec["alerted"] = now_ts
        fresh[key] = rec
    return due, fresh


def alert_body(due: List[Tuple[str, dict, float]]) -> Tuple[str, str]:
    total = sum(it["est"] for _, it, _ in due)
    subject = (f"IMM watch: {len(due)} dark item(s) worth ~${total:,.0f}/day "
               f"need a row or a call")
    lines = [subject, ""]
    for key, it, dark in sorted(due, key=lambda d: -d[1]["est"]):
        lines.append(f"- ~${it['est']:,.2f}/day, dark {dark / 60:.1f}h  {it['what']}")
        lines.append(f"    fix: {it['fix']}")
    lines += ["", f"(est = pool $/day x {GAP_CAPTURE:.3f}, the median share our "
                  f"quoted markets earn; threshold ${GAP_ALERT_MIN_DPD:g}/day, "
                  f"dark >= {GAP_ALERT_AFTER_MIN:g} min)"]
    return subject, "\n".join(lines)


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
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args(argv)
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
    if not args.no_date:
        try:
            dated = date_us_general(client, progs, args.dry)
            for ev, added, msg in dated:
                log(f"[WATCH] {'dated' if added else 'undated'} {ev}: {msg}")
            summary.append(f"dated {sum(1 for _, a, _ in dated if a)}")
        except Exception as e:
            log(f"[WATCH] ! dating pass failed: {e!r}")
    if not args.no_alert:
        try:
            audit = ifa.run_audit(client, progs)
            try:
                with open(imm.CONFIG_GAPS_FILE, encoding="utf-8") as f:
                    gaps = (json.load(f) or {}).get("gaps") or {}
            except (OSError, ValueError):
                gaps = {}
            state = _load_state()
            if "baseline_not_allowed" not in state:      # first run: today's
                state["baseline_not_allowed"] = sorted(audit["not_allowed"])
                state["baseline_at"] = datetime.now(timezone.utc).isoformat()
            baseline = set(state["baseline_not_allowed"])
            items = gap_items(progs, gaps, audit["not_allowed"], baseline)
            due, state["items"] = due_alerts(state.get("items") or {}, items, time.time())
            if due:
                subject, body = alert_body(due)
                log("[WATCH] alert:\n" + body)
                if not args.dry:
                    imm.Alerter("IMM-WATCH", live=True).send_message(body, subject)
                    # a new family is sent once, then joins the baseline
                    state["baseline_not_allowed"] = sorted(
                        baseline | {k[4:] for k, _, _ in due if k.startswith("new:")})
            if not args.dry:
                _save_state(state)
            summary.append(f"tracking {len(items)}, alerted {len(due)}")
        except Exception as e:
            log(f"[WATCH] ! alert pass failed: {e!r}")
    log(f"[WATCH] {', '.join(summary)} ({time.time() - t0:.0f}s"
        f"{', dry' if args.dry else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
