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
   gaps for pass 3.
3. RESEARCH (2026-10-08, Jack: "yes switch" -- in place of a 2-hourly
   routine) -- what only research can fix (an election event with no
   verified voting day, an awards event with no hand row, a broadcast mention
   event the WH / RNC / TVmaze resolvers cannot place) goes to one headless
   Claude Code run on first sight, retried on a backoff; see research_pass.
4. ALERT -- one email per run for what has stayed dark >= GAP_ALERT_AFTER_MIN
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
import shutil
import subprocess
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


# Events are keyed with imm.event_ticker_of, not a bare rsplit: a one-market
# event's market ticker IS its event ticker (KXTUREKOUTPERFORMRCP-26NOV03, the
# 10/08 02:02Z RCP-outperform batch), and the rsplit made it the SERIES -- so
# the dating pass found no markets and skipped it unlogged, and research and
# the alert both read a $0 pool while nine of them paid ~$56/market/day.
def _by_event(progs: Dict[str, dict]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for t in progs:
        out[imm.event_ticker_of(t)].append(t)
    return out


def event_pools(progs: Dict[str, dict]) -> Dict[str, float]:
    pools: Dict[str, float] = defaultdict(float)
    for t, p in progs.items():
        pools[imm.event_ticker_of(t)] += ifa._dollars_per_day(p)
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
        if not it.get("hold") and it["est"] >= min_dpd and dark_min >= after_min and (
                alerted is None or (not key.startswith("new:")
                                    and now_ts - float(alerted) >= realert_hours * 3600)):
            due.append((key, it, dark_min))
            rec["alerted"] = now_ts
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


def gap_kind(msg: str) -> str:
    """'election' / 'award' for the config gaps a researched row fixes, else
    '' (a ticker shape, a release guard -- code, not research)."""
    m = msg or ""
    if "election day" in m:
        return "election"
    if "hand-table row" in m or "no usable date" in m:
        return "award"
    return ""


def research_targets(client, gaps: dict, now: datetime, dry: bool) -> Dict[str, dict]:
    """{key: {kind, event, series, msg}} for what only research can fix.
    Broadcast mention events the automatic resolvers CAN place are written
    here and now (as the overrides task's Phase 4 would), not researched."""
    targets: Dict[str, dict] = {}
    imm.load_file_event_overrides()       # a --set since import is not a target
    imm.load_election_dates_extra()       # nor a row written since the bot's
    imm.load_awards_dates_extra()         # last config_gaps.json (the dating pass)
    for ev, g in sorted((gaps or {}).items()):
        kind = gap_kind(g.get("msg") or "")
        if kind == "election" and imm.election_cutoff_utc(ev) is not None:
            continue
        if kind == "award" and imm.awards_event_start(ev, None, dates_only=True) is not None:
            continue
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
            wait = RESEARCH_BACKOFF_H[min(n - 1, len(RESEARCH_BACKOFF_H) - 1)] * 3600
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
        "- Election targets: only a market decided by a VOTE gets a row (an "
        "election, referendum, chamber control, vote share). A rally, debate, "
        "court case, retirement, appointment or announcement is NOT a vote: "
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
                           timeout=RESEARCH_TIMEOUT_SECS)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", 0.0, f"{type(e).__name__}: {e}"
    try:
        js = json.loads(p.stdout or "{}")
    except ValueError:
        return p.stdout or "", 0.0, f"exit {p.returncode}: non-JSON output"
    return (js.get("result") or ""), float(js.get("total_cost_usd") or 0.0), (
        "" if p.returncode == 0 else f"exit {p.returncode}: {(p.stderr or '')[:200]}")


def research_pass(client, gaps: dict, state: dict, dry: bool) -> Tuple[Dict[str, dict], List[str]]:
    """Collect targets, research the due ones, update state["research"].
    Returns (targets, summary lines)."""
    now_ts = time.time()
    targets = research_targets(client, gaps, datetime.now(timezone.utc), dry)
    rstate = {k: v for k, v in (state.get("research") or {}).items() if k in targets}
    pools = event_pools(ifa.fetch_active_programs(client)) if targets else {}
    est = {k: pools.get(t["event"], 0.0) * GAP_CAPTURE for k, t in targets.items()}
    due = research_due(rstate, targets, now_ts, est)
    notes = []
    if due and RESEARCH_ENABLE and not dry:
        items = [(k, targets[k], imm_rows.lookup(targets[k]["event"]).replace("\n", " / ")[:900])
                 for k in due]
        text, cost, err = run_research(research_prompt(items))
        res = parse_research(text)
        for k in due:
            r = rstate.setdefault(k, {})
            r["attempts"] = int(r.get("attempts") or 0) + 1
            r["last"] = now_ts
            got = res.get(k) or {}
            r["status"] = got.get("status") if got.get("status") in (
                "written", "judgment", "unresolved") else "unresolved"
            r["note"] = (got.get("note") or err or "no result")[:300]
            notes.append(f"{k}: {r['status']} -- {r['note']}")
        notes.append(f"research: {len(due)} target(s), ${cost:.2f}" + (f", {err}" if err else ""))
    elif due:
        notes.append(f"research due ({'dry' if dry else 'disabled'}): {', '.join(due)}")
    state["research"] = rstate
    return targets, notes


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
    state = _load_state()
    try:
        with open(imm.CONFIG_GAPS_FILE, encoding="utf-8") as f:
            gaps = (json.load(f) or {}).get("gaps") or {}
    except (OSError, ValueError):
        gaps = {}
    targets: Dict[str, dict] = {}
    if not args.no_research:
        try:
            targets, notes = research_pass(client, gaps, state, args.dry)
            for n in notes:
                log(f"[WATCH] {n}")
            summary.append(f"research targets {len(targets)}")
        except Exception as e:
            log(f"[WATCH] ! research pass failed: {e!r}")
    if not args.no_alert:
        try:
            audit = ifa.run_audit(client, progs)
            if "baseline_not_allowed" not in state:      # first run: today's
                state["baseline_not_allowed"] = sorted(audit["not_allowed"])
                state["baseline_at"] = datetime.now(timezone.utc).isoformat()
            baseline = set(state["baseline_not_allowed"])
            items = gap_items(progs, gaps, audit["not_allowed"], baseline, targets)
            hold_researched(items, targets, state, time.time())
            due, state["items"] = due_alerts(state.get("items") or {}, items, time.time())
            if due:
                subject, body = alert_body(due)
                log("[WATCH] alert:\n" + body)
                if not args.dry:
                    imm.Alerter("IMM-WATCH", live=True).send_message(body, subject)
                    # a new family is sent once, then joins the baseline
                    state["baseline_not_allowed"] = sorted(
                        baseline | {k[4:] for k, _, _ in due if k.startswith("new:")})
            summary.append(f"tracking {len(items)}, alerted {len(due)}")
        except Exception as e:
            log(f"[WATCH] ! alert pass failed: {e!r}")
    if not args.dry:
        _save_state(state)
    log(f"[WATCH] {', '.join(summary)} ({time.time() - t0:.0f}s"
        f"{', dry' if args.dry else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
