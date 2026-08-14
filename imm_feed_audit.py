"""Read-only audit: every event with ACTIVE incentive programs that
incentive_mm would exclude, with the exclusion reason.

Run after any resolver/cutoff/allowlist change, or when an event "should be
quoting but isn't". Catches the whole class of miss at once instead of waiting
for one to be noticed (2026-07-19: CONNPHX team-code mismatch and postponed
NYDAL were both invisible in the bot's logs — no-program and pre-dropped
markets never reach the skips dict).

Usage:  python imm_feed_audit.py
Output: one line per excluded-but-paying event; silence below the header means
        nothing is being missed.
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import incentive_mm as imm


def main() -> int:
    client = imm.build_client()
    now = datetime.now(timezone.utc)

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
    print(f"active program markets: {len(progs)}")

    by_event = defaultdict(list)
    for t in progs:
        by_event[t.rsplit("-", 1)[0]].append(t)

    res = imm.EventStartResolver()
    bot = imm.IncentiveMarketMaker(client=None, live=False)  # _allowed only

    # The candidate cap is ANOTHER silent exclusion (sorted by pool $/day,
    # truncated, no skip count): warn when the allowed universe exceeds it.
    n_allowed = sum(1 for t in progs if bot._allowed(t))
    if n_allowed > imm.MAX_CANDIDATE_BOOKS:
        print(f"\n!! WARNING: {n_allowed} allowed program markets > "
              f"MAX_CANDIDATE_BOOKS={imm.MAX_CANDIDATE_BOOKS} — the lowest-"
              f"$/day {n_allowed - imm.MAX_CANDIDATE_BOOKS} are being "
              f"silently truncated (bit the company-metric class 2026-07-22)")

    print("\nevents with programs the bot excludes (blank = no misses):")
    misses = 0
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
        if cutoff is not None and cutoff <= now:
            if mention_family and resolved is None:
                # a passed ticker date on a mention event may be a LISTING
                # date; the bot's real cutoff needs expected_expiration
                status.append("ticker date passed (mention window — "
                              "expiration governs, see listing-date rule)")
            else:
                status.append(f"cutoff passed ({cutoff:%m-%d %H:%MZ} "
                              f"{'resolved' if resolved else 'midnight-fallback'})")
        if status:
            misses += 1
            print(f"  {ev_t} ({len(tickers)} mkts, {series}): {'; '.join(status)}")
    if not misses:
        print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
