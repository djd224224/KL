"""Read-only audit: every event with ACTIVE incentive programs that
incentive_mm would exclude, with the exclusion reason.

Run after any resolver/cutoff/allowlist change, or when an event "should be
quoting but isn't". Catches the whole class of miss at once instead of waiting
for one to be noticed (2026-07-19: CONNPHX team-code mismatch and postponed
NYDAL were both invisible in the bot's logs — no-program and pre-dropped
markets never reach the skips dict).

Two checks added 2026-08-22 (the KXTEMPMIAH incident, Jack "add a check into
sweeper to pickup other markets like this"):
  * UNEARNABLE — a program whose ENTIRE reward period starts after the bot's
    midnight-fallback cutoff. The bot cannot earn a cent of it at any point in
    the market's life; the generic "cutoff passed" line made this read like a
    normal late-day daily. Signature of an hourly/close-anchored series
    missing its per-series override (Kalshi relaunched hourly temp as
    Miami-only 2026-08-15; KXTEMPMIAH wasn't in IMM_TEMP_SERIES, so the
    ticker date parsed as midnight ET and every market arrived pre-dead,
    invisibly, for a week).
  * absent-family footer — enrolled close-anchored series (IMM_TEMP_SERIES /
    IMM_AQI_SERIES families) with NO active programs in the feed. Kalshi
    ended the 5-city hourly-temp programs after Aug 4 and the bot went
    silently dark on its top reward family for 16 days; nothing alerted
    because gaps tooling only ranks markets that HAVE programs.
Plus a not-allowed leaderboard (same day): top paying families the allowlist
excludes — deliberate exclusions, but listed so a NEW family is visible the
day Kalshi lights it up (2026-08-20: 11 KXAVGT weekly-average-temp stations
appeared at ~$2,600/day of pool and nothing surfaced them).

SCHEDULED FOLD (Jack 2026-08-22 "isnt there a daily sweeper? fold into
that"): imm_earnings_overrides.py (the 6:45a/12:45p/4:45p ET task) calls
delta_for_email() each run — NEW findings versus feed_audit_state.json become
ACTION lines in that task's email; a one-line current-state summary rides the
run-summary file into the 7:20 combined email. The state file holds the
CURRENT finding sets, so a fixed item drops out and a regression re-pings;
not-allowed families ping once when they first cross NEW_FAMILY_MIN_DPD.

Usage:  python imm_feed_audit.py           # full report to stdout
Output: one line per excluded-but-paying event; silence below the header means
        nothing is being missed.
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import incentive_mm as imm

STATE_FILE = os.path.join(imm.STATUS_DIR, "feed_audit_state.json")
# A newly-seen not-allowed SERIES only pings the sweeper email when its pool
# is at least this many $/day — scraps stay in the CLI leaderboard only. 324
# series cleared $50/day on 2026-08-22, so the bar sits at $300: KXAVGT's
# $500-tier stations (~$745/day each) ping, the $200-tier (~$298) just miss —
# any one ping surfaces its whole family, and the leaderboard shows the rest.
NEW_FAMILY_MIN_DPD = float(os.environ.get("IMM_AUDIT_NEW_FAMILY_MIN_DPD", "300"))


def fetch_active_programs(client) -> dict:
    """market_ticker -> raw program row, for every ACTIVE incentive program."""
    progs = {}
    cursor = None
    while True:
        params = {"status": "active", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/incentive_programs", params=params)
        for p in resp.get("incentive_programs") or []:
            progs[p.get("market_ticker", "")] = p
        cursor = resp.get("next_cursor")
        if not cursor:
            break
    return progs


def _dollars_per_day(p) -> float:
    """Mirror of incentive_mm's ranking math (period_reward is centi-cents)."""
    start = imm.parse_iso_utc(p.get("start_date", ""))
    end = imm.parse_iso_utc(p.get("end_date", ""))
    if not start or not end:
        return 0.0
    days = max((end - start).total_seconds() / 86400.0, 1.0 / 24)
    return (p.get("period_reward") or 0) / 10000.0 / days


def run_audit(client=None, progs=None) -> dict:
    """Collect every check without printing. Returns:
      n_programs      int
      cap_warning     str | None      (MAX_CANDIDATE_BOOKS truncation)
      miss_lines      [str]           (per excluded-but-paying event)
      unearnable      set[str]        (series with an UNEARNABLE event)
      absent          [str]           (enrolled close-anchored, no programs)
      not_allowed     {series: $/day} (every not-allowed series, unfiltered)
    """
    now = datetime.now(timezone.utc)
    if progs is None:
        progs = fetch_active_programs(client or imm.build_client())

    res = imm.EventStartResolver()
    bot = imm.IncentiveMarketMaker(client=None, live=False)  # _allowed only
    # The bot hot-loads extra_allow_series.json inside refresh_universe, which
    # this audit never calls — without loading it here, extra-enrolled
    # families (KXRAIN!) read as not-allowed: skipped by the misses loop AND
    # false-positived in the not-allowed footer (caught 2026-08-22).
    imm.load_extra_allow_series()

    by_event = defaultdict(list)
    for t in progs:
        by_event[t.rsplit("-", 1)[0]].append(t)

    # The candidate cap is ANOTHER silent exclusion (sorted by pool $/day,
    # truncated, no skip count): warn when the allowed universe exceeds it.
    cap_warning = None
    n_allowed = sum(1 for t in progs if bot._allowed(t))
    if n_allowed > imm.MAX_CANDIDATE_BOOKS:
        cap_warning = (f"!! WARNING: {n_allowed} allowed program markets > "
                       f"MAX_CANDIDATE_BOOKS={imm.MAX_CANDIDATE_BOOKS} — the "
                       f"lowest-$/day {n_allowed - imm.MAX_CANDIDATE_BOOKS} "
                       f"are being silently truncated (bit the company-metric "
                       f"class 2026-07-22)")

    miss_lines = []
    unearnable = set()
    for ev_t in sorted(by_event):
        tickers = by_event[ev_t]
        t0 = tickers[0]
        series = imm.series_of(t0)
        if not bot._allowed(t0):
            continue        # deliberate exclusion (fleet blocklists etc.)
        ov = imm.series_override(series)
        if ov and ov.cutoff_from_close_min is not None:
            continue        # close-anchored (hourly weather/AQI): close_time
                            # governs, ticker date meaningless — not auditable
                            # from the program feed alone
        status = []
        # replicate refresh_universe's ticker pre-filter
        mention_family = any(series.endswith(suf)
                             for suf in imm.ALLOW_SERIES_SUFFIXES)
        pre_exempt = (series.startswith(imm._EARNINGS_PREFIX)
                      or ev_t in imm.EVENT_START_OVERRIDES
                      or series in imm.SCHEDULE_RESOLVED_SERIES
                      # 2026-08-14 listing-date fix: mention-family tickers
                      # always hydrate; trade_cutoff_utc decides from the
                      # market's expiration (not knowable from the feed here)
                      or mention_family)
        td = imm.parse_event_date(t0)
        if not pre_exempt and td is not None and now >= td + timedelta(hours=24):
            status.append("PRE-DROPPED(24h ticker rule)")
        resolved = res.resolve(series, ev_t)
        cutoff = (resolved - timedelta(minutes=imm.EVENT_START_BUFFER_MIN)
                  if resolved is not None else td)
        # UNEARNABLE: the program's whole period lies at/after the cutoff, so
        # the bot can never earn any of it. Scoped to the midnight-FALLBACK
        # cutoff only — a RESOLVED cutoff preceding a program period is a
        # deliberate stand-down (earnings call-time rule), not a miss — and
        # mention-family events keep their softer listing-date message below.
        pstart = imm.parse_iso_utc((progs.get(t0) or {}).get("start_date", ""))
        if (cutoff is not None and resolved is None and not mention_family
                and pstart is not None and pstart >= cutoff):
            unearnable.add(series)
            status.append(
                f"UNEARNABLE(program {pstart:%m-%d %H:%MZ}+ entirely after "
                f"midnight-fallback cutoff {cutoff:%m-%d %H:%MZ} — "
                f"close-anchored series missing its override? "
                f"IMM_TEMP_SERIES/IMM_AQI_SERIES class)")
        elif cutoff is not None and cutoff <= now:
            if mention_family and resolved is None:
                # a passed ticker date on a mention event may be a LISTING
                # date; the bot's real cutoff needs expected_expiration
                status.append("ticker date passed (mention window — "
                              "expiration governs, see listing-date rule)")
            else:
                status.append(f"cutoff passed ({cutoff:%m-%d %H:%MZ} "
                              f"{'resolved' if resolved else 'midnight-fallback'})")
        if status:
            miss_lines.append(
                f"{ev_t} ({len(tickers)} mkts, {series}): {'; '.join(status)}")

    # Enrolled close-anchored families (the per-city override lists) with no
    # ACTIVE programs — the 2026-08-04 class: Kalshi ends a family's programs
    # and the bot goes silently dark on it (selection is program-driven, so
    # nothing errors and nothing logs). Hourly programs exist only during
    # their hour (published ~hh:11), so one absent series can be a timing
    # artifact; a family absent across reruns has genuinely lost its rewards.
    prog_series = {imm.series_of(t) for t in progs}
    absent = sorted(s for s, ov in imm.SERIES_OVERRIDES.items()
                    if ov.cutoff_from_close_min is not None
                    and s not in prog_series)

    # NOT-ALLOWED families by pool $/day — deliberate exclusions, listed so a
    # NEW paying family is visible the day Kalshi lights it up.
    not_allowed = defaultdict(float)
    for t, p in progs.items():
        if not bot._allowed(t):
            not_allowed[imm.series_of(t)] += _dollars_per_day(p)

    return {"n_programs": len(progs), "cap_warning": cap_warning,
            "miss_lines": miss_lines, "unearnable": unearnable,
            "absent": absent, "not_allowed": dict(not_allowed)}


def render(a: dict) -> list:
    """The CLI report, one string per line."""
    out = [f"active program markets: {a['n_programs']}"]
    if a["cap_warning"]:
        out += ["", a["cap_warning"]]
    out += ["", "events with programs the bot excludes (blank = no misses):"]
    out += [f"  {l}" for l in a["miss_lines"]] or ["  (none)"]
    if a["absent"]:
        out += ["", "enrolled close-anchored series with NO active programs "
                    "right now (rerun mid-hour before alarming; persistent "
                    "absence = family's rewards ended):"]
        out += [f"  {s}" for s in a["absent"]]
    top = sorted(a["not_allowed"].items(), key=lambda kv: -kv[1])[:10]
    if top:
        out += ["", f"not-allowed series with active programs, top {len(top)} "
                    f"of {len(a['not_allowed'])} by pool $/day (deliberate "
                    f"exclusions — scan for NEW families worth enrolling):"]
        out += [f"  {s:24s} ${d:,.0f}/day" for s, d in top]
    return out


def delta_for_email(state_path=STATE_FILE, client=None, progs=None,
                    save_state=True):
    """(action_lines, info_lines) for the sweeper task's email machinery:
    action = findings NEW since the saved state (needs Jack), info = one-line
    current state for the 7:20 combined email. State stores the CURRENT sets,
    so fixes drop out and regressions re-ping; a --dry caller passes
    save_state=False so it doesn't consume the new-ness edge."""
    a = run_audit(client=client, progs=progs)
    try:
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f) or {}
    except (OSError, ValueError):
        st = {}

    new_unearn = sorted(a["unearnable"] - set(st.get("unearnable", [])))
    new_absent = [s for s in a["absent"] if s not in set(st.get("absent", []))]
    over = {s: d for s, d in a["not_allowed"].items()
            if d >= NEW_FAMILY_MIN_DPD}
    new_fams = sorted(set(over) - set(st.get("not_allowed", [])),
                      key=lambda s: -over[s])

    act = []
    if new_unearn:
        act.append(f"!! FEED AUDIT — UNEARNABLE series: {', '.join(new_unearn)}. "
                   f"Programs pay only inside a window the bot's midnight-"
                   f"fallback cutoff can never touch (the KXTEMPMIAH class): "
                   f"likely a close-anchored series missing its override — "
                   f"add to IMM_TEMP_SERIES / IMM_AQI_SERIES / IMM_AVGT_SERIES "
                   f"or give it a SeriesOverride, then restart.")
        for l in a["miss_lines"]:
            if "UNEARNABLE" in l:
                act.append(f"  {l}")
        act.append("")
    if new_absent:
        act.append(f"FEED AUDIT — enrolled family's programs LEFT the feed "
                   f"(the Aug-4 hourly-temp class; bot goes silently dark on "
                   f"it): {', '.join(new_absent)}. Transient for hourly series "
                   f"is possible — python imm_feed_audit.py to recheck.")
        act.append("")
    if new_fams:
        act.append(f"FEED AUDIT — new paying famil{'ies' if len(new_fams) > 1 else 'y'} "
                   f"NOT in the allowlist (deliberate until you enroll them):")
        for s in new_fams:
            act.append(f"  {s:24s} ${over[s]:,.0f}/day pool")
        act.append("")

    info = [f"feed-audit: {len(a['unearnable'])} unearnable / "
            f"{len(a['absent'])} enrolled-absent / {len(over)} not-allowed "
            f"families >= ${NEW_FAMILY_MIN_DPD:,.0f}/day"
            + (f" (top: {max(over, key=over.get)} "
               f"${over[max(over, key=over.get)]:,.0f}/day)" if over else "")]

    if save_state:
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"ts": datetime.now(timezone.utc).isoformat(),
                           "unearnable": sorted(a["unearnable"]),
                           "absent": a["absent"],
                           "not_allowed": sorted(over)}, f, indent=1)
        except OSError as e:
            info.append(f"feed-audit: state save FAILED ({e}) — "
                        f"next run will re-ping the same findings")
    return act, info


def main() -> int:
    print("\n".join(render(run_audit())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
