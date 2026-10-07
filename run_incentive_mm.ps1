# Launcher for the Kalshi incentive-rewards market maker (incentive_mm.py).
# Mirrors run_crypto_touch_mm.ps1: runs under Windows Task Scheduler (at
# logon), restarts the bot forever if it exits, logs to
# run-logs\incentive-mm\incentive-mm-YYYY-MM-DD.log.
#
# NOT REGISTERED AS A TASK YET (deliberate — see INCENTIVE_MM_HANDOFF.md for
# the go-live checklist and the Register-ScheduledTask command).
#
# No secrets live in this file: alert credentials are read from the
# user-level environment (HKCU\Environment).

param(
    # 90 -> 60 -> 30 (Jack 2026-07-21) -> 10 (Jack 2026-08-02 "do A"): the old
    # <15s contention warning predates the Advanced API tier (read 300/s
    # sustained) + the 25ms client throttle; measured cycle WORK is ~22s at
    # the ~360-market universe, so poll 10 = ~32s effective cadence at ~12
    # req/s (~4% of the shared read budget). Watch for 429s in the log after
    # any further cut. The KXTEMP fast-lane (IMM_FAST_LANE_SECS, in-code)
    # additionally re-quotes temp books between full cycles.
    [string]$PollSecs = "10",
    # Micro-probe mode (go-live phase 1): 1/5-size ladders on ~10 markets,
    # ~$200 collateral. Run one full PAID period this way and reconcile
    # Kalshi's actual credits against the estimator before scaling up.
    [switch]$Probe
)

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"

# Transcript: Task Scheduler swallows console errors; this file is the only
# way to see why the launcher died before its first loop iteration.
try { Start-Transcript -Path (Join-Path $Repo "run-logs\incentive-mm\launcher-transcript.log") -Append | Out-Null } catch {}

Set-Location $Repo
New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\incentive-mm") | Out-Null

$env:PYTHONIOENCODING = "utf-8"
# The rich HTML morning digest (send_imm_digest.py) is a section of the 7:00
# portfolio email since 2026-10-02. Suppress the bot's own plain-text
# one-liner email so there's no duplicate; it still STORES the daily summary
# in status_incentive_mm.json (the digest reads activity figures from it).
$env:IMM_SUMMARY_EMAIL = "0"
# Task Scheduler sessions can lack APPDATA (hides pip --user installs).
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
# The task session may not inherit user env vars — pull alert creds from HKCU.
foreach ($v in "ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD") {
    if (-not (Get-Item "env:$v" -ErrorAction SilentlyContinue)) {
        $val = [Environment]::GetEnvironmentVariable($v, "User")
        if ($val) { Set-Item "env:$v" $val }
    }
}

# Spread startup vs the crypto fleet's at-logon herd (shared account API).
Start-Sleep -Seconds (Get-Random -Maximum 45)

$ProbeEnv = ""
if ($Probe) {
    # TTL/refresh raised vs defaults: with the crypto fleet sharing the
    # account's API throughput, order writes run ~5s each — a 60-order book
    # takes ~5 min to rewrite, so a 420s refresh would full-churn forever
    # (the fleet's hard-learned lesson). 1800/1500 = ~28% rewrite duty cycle;
    # cutoff-capped expirations still bound event risk exactly.
    # KALSHI_RATE_LIMIT_MS 100->25 (40 calls/s, was 10) and placements/cycle
    # 120->250 (Jack 2026-07-23): account is on Advanced (write 600 burst /
    # 300/s sustained), so 40/s is ~13% of budget and the ~30ms warm round-trip
    # (not the throttle) becomes the real floor = quote as fast as the wire
    # allows. Env-scoped: the 16 crypto bots keep 100ms and don't starve the
    # shared write budget.
    # placements/cycle 250 -> 1000 (Jack 2026-10-01, "both"), now PACED in
    # code at IMM_PLACE_RATE_PER_SEC (12 writes/s; amends share the cap and
    # the pace). A capped cycle used to fire its 250 in ~10s, and both
    # account-wide 429 storms of 10/1 -- 13:25Z (a renewal wave) and 15:14Z
    # (the restart rebuild) -- landed in exactly those seconds, with
    # crypto-touch / crypto-annual / updown rejected alongside the IMM; the
    # "~13% of budget" above did not hold in practice. A full 1000-order cycle
    # now spreads over ~85s at half the old peak rate.
    # Quiet-hours ladder boost (Jack 2026-07-25): non-KXTEMP rungs x2 during
    # ET 3-7am — 1/3.3 the traded flow, half the fill turnover, ~breakeven
    # non-temp fill P&L (KXTEMP excluded in code: IMM_HOUR_MULT_EXCLUDE).
    # Ladder 5/5/5 -> 8/0/0 (Jack 2026-07-25, from the shape sim on 198 live
    # books): reward weight halves per tick behind the touch and 55/198 books
    # walk-truncate deeper rungs to ZERO score, while touch fills are the
    # cheapest per contract (-0.9c vs -5.3c at depth 2) — all-at-the-touch
    # dominates; total 15 -> 8 was Jack's exposure trim.
    # 0:30 -> 0:20 global (Jack 2026-08-01 night: "move down everything to 20");
    # IMM_TEMP_LEVELS=0:20 RESTORED 8/2: dropping it fell back to the temp override's 5/2/2 code default (NOT the global), whose multi-rung shape + atref collapse tripped level_cap and killed all temp quoting overnight. Mention
    # x1.5 off (code default 1.0). Caps pinned EXPLICITLY (Jack same night):
    # IMM_MAX_POSITION=150 + IMM_MAX_EVENT=1000 for ALL series -- replaces the
    # old mult-derived mention-only 150/1000; base was 100/500, then /667.
    # IMM_MAX_TOTAL_RESTING 2000 -> 4000 (Jack 2026-08-02): the 2000 cap was
    # sized for the ~263-market universe; post-explosion (~360 quoted, pads
    # included) peak books approached it and the cap silently blocks
    # placements when hit. 610 resting at raise time; alerts still fire.
    # KXTEMP re-allowed + IMM_TEMP_LEVELS=0:20 (Jack 2026-08-01 pm: "reallow the
    # hourly temp markets... contract size 20 instead of 30"). IMM_MAX_EVENT
    # 500 -> 667 => mention-family event cap 750 -> ~1000 (Jack: "increase caps
    # to 150/1000"; per-market mention cap already 150). At-ref rungs are BAND-
    # EXEMPT both sides (Jack: no arbitrary 5c/90c pin when the reference is
    # deeper) — safe: at-ref bids only rest at/below touch, asks at/above.
    # 0:10 -> 0:30 + IMM_LADDER_MODE=atref (Jack 2026-08-01): amended program
    # rules (eff 7/30) score the whole top-of-book band at full weight, so
    # per-contract share is ~size/band (~/200) not touch-multiplied; shape sim
    # v2 (imm_shape_sim_v2.py): at-ref = same est reward as all-at-touch at
    # ~half the fill exposure and ~2/3 collateral. 30/side rebuilds the share
    # the band dilution took. Rain override removed (Jack 2026-08-01 pm): rain runs the global 30/side at-ref.
    # 0:8 -> 0:10 (Jack 2026-07-25 pm). Gas trackers blocklisted same day
    # (Jack: stop quoting KXAAAGASW/M/D + KXUSGASCPI). KXRAIN prefix added
    # 2026-07-26 pm (Jack: "remove all rain markets from the allowlist" —
    # covers the daily KXRAIN + all KXRAIN<CITY>M monthlies). Blocklist =
    # FROZEN: no orders at all, positions ride to settlement.
    # Gas trackers UN-blocklisted (Jack 2026-08-02 "reconsider gas trackers"):
    # re-enter under the code-level re-entry guards ($2/day rate floor +
    # safe-join). No sniper collision: day-dated tickers stop quoting at
    # midnight ET before print day; the 3:20am snipe finds IMM already out.
    # IMM_FORCE_EVENTS: per-event floor/hopeless bypass for Kalshi data-bug
    # events (2026-08-03 TRUMPMENTION: program period stamped to Aug 18 on a
    # same-day event). REMOVE entries once the event settles.
    # IMM_COLLATERAL_BUDGET 20000 -> 50000 (Jack 2026-08-04). At 20k the budget
    # was over-subscribed ($16,325 ladder + $5,866 inventory reserve = $22,191)
    # and, because sticky retention is seeded BEFORE the yield ranking, the
    # incumbent earnings-mention book (450 markets / 36 events) held all of it.
    # Hourly KXTEMP relists every hour and can never be an incumbent, so all 50
    # temp markets were rejected at 11:02Z — 28 extreme_mid, 4 one_sided and 18
    # on budget alone — despite temp being 67% of lifetime credited reward.
    # KXRAIN-26AUG05 (the top-ranked unquoted event, ~$143/day) was blocked the
    # same way. NOTE the budget is a MODELLED reservation (x0.65 realization),
    # not cash: account cash was $9,470 when this was raised, so past ~$10k the
    # real governor is Kalshi rejecting orders for insufficient balance, with
    # the account-value floor (IMM_ACCOUNT_DROP_HALT, $2,000/day in code since
    # 2026-10-03; the cash IMM_BALANCE_DROP_HALT=5000 it replaced is gone)
    # and the daily-loss halt underneath.
    # 50000 -> 100000 (Jack 2026-10-01 "raise budget to $100k"), the same day
    # IMM_MAX_MARKETS went 200 -> 1000: with the event cap out of play the
    # budget is the breadth governor, and ~$45k of the 50k was spoken for
    # ($26.2k ladder + $18.8k inventory reserve at 50c/contract). The same
    # caveat holds: account cash was $8,077 (equity $25.5k) at the change.
    # KXTRUMPMENTION BLOCKED 2026-08-05 (Jack "block KXTRUMPMENTION markets").
    # Blocklist = FROZEN: zero new orders, existing resting quotes cancelled on
    # the next cycle, positions ride to settlement. At the time of blocking the
    # bot held 33 markets / 1,025 contracts (net -618 on KXTRUMPMENTION-26AUG05,
    # which settles 4:30pm ET today) and had 49 resting orders / 848 contracts.
    # PREFIX match, so KXTRUMPMENTIONB is caught too — narrow to an exact
    # series list if only the base family is meant.
    # IMM_FORCE_EVENTS emptied in the same edit: it held KXTRUMPMENTION-26AUG05
    # (the Aug 18 program-period data bug), which the block makes moot — and a
    # force entry for a blocked series reads like a contradiction later.
    # KXMAMDANIMENTION BLOCKED 2026-08-05 pm (Jack: "yes blocklist this").
    # THE POOL IS FINE — the CLOCK is broken. Do not read this as a bad-pool
    # block. MEASURED: 14 programs, one per market, $100.00/market over
    # 2026-08-05T19:00Z -> 2026-08-27T14:00Z (21.79d) = $4.59/day/market,
    # $1,400 event pool; cycle_log_2026-08-05.csv carries pool_per_day=4.59 in
    # all 1,856 MAMDANI rows. That is a healthy pool, mid-pack on yield.
    # THE DEFECT: trade_cutoff_utc()/parse_event_date() (incentive_mm.py:1939,
    # :1685) read the "26AUG06" ticker segment as the EVENT date and cut the bot
    # out at 2026-08-06T04:00Z. MEASURED: Kalshi's own close_time and
    # expiration_time for these strikes are 2026-08-27T14:00:00Z. For these
    # political-mention series the ticker date is a LISTING date, not a
    # resolution date, so the bot plays ~9h of a 22-day program. Note
    # expected_expiration cannot rescue it: it is only consulted inside the
    # event_day_cutoff_et override branch, and min() can only pull the cutoff
    # EARLIER — a series with no override gets the raw ticker date as a ceiling.
    # CONSEQUENCE: est_peak projects $0.37-$0.87/market, all under the $1.00
    # per-market Kalshi floor => expected credit $0.00 x 14 (modelled).
    # So the block is correct WHILE THE CUTOFF BUG EXISTS, and should be
    # revisited the moment it is fixed. This heuristic was right until ~late
    # July: of 276 historical MENTION events the median program is 1.05d and
    # only 11 exceed 5d. Kalshi started issuing 16-24d mention programs.
    # SAME BUG, OPPOSITE SIGN — currently costing us money in the other
    # direction: KXTRUMPMENTIONB-26AUG04 ($2,500 pool, ~20.7d left) and
    # KXTRUMPMENTION-26AUG05 ($3,300, ~14.7d left) are LIVE programs the bot
    # cannot touch because their ticker cutoff already passed. ~$5,800 of pool
    # sitting idle. Fixing the cutoff is worth more than any blocklist entry.
    # IMM_BENCH_COOLDOWN 4h -> 1h (Jack 2026-08-06: "bench only 1hr instead of
    # 4hr going fwd"). The bench fires on 30 consecutive ZERO-reward-share
    # cycles (:4905), which measures OUR resting size — so it cannot tell "this
    # book can't earn" from "we had no orders up". MEASURED that morning:
    # Kalshi went down for maintenance ~07:16-09:00Z (19,949 503s in hour 07Z,
    # 18,737 in 08Z, 13 in 09Z); the failsafe cancelled every order at
    # 07:18:44Z after 4 consecutive cycle errors; with nothing resting, all 307
    # bench events fired 07:45:59-08:13:29Z — exactly 30 cycles later at the
    # degraded ~54s/cycle. That benched 293 of 471 candidates and cut selected
    # 390 -> 31 and est reward ~$404 -> ~$159/day for four hours, none of it a
    # statement about the markets. 1h caps the blast radius of any future
    # outage at ~1/4 the lost quoting time. NOT the root fix: the real bug is
    # that zero-share strikes accrue while the bot has no orders up through no
    # fault of the book. bench_until is in-memory only (:2707/:4190/:4905, not
    # in _save_persist), so a restart also clears the whole bench instantly.
    # KXEARNINGSMENTIONAC blocked 2026-08-06: imm_earnings_overrides.py reports
    # it UNRESOLVED, so the event has no call-time guard and falls back to
    # midnight-ET-of-ticker-date (26AUG12). That fallback is NOT safe -- a call
    # KXEARNINGSMENTIONAC unblocked 2026-08-07 (Jack "yes"): call time found in
    # Air Canada's own media advisory — analyst call 8:00 AM ET Wed Aug 12 —
    # and set as an event_start_override, so the stand-down fires 07:50 ET.
    # The AC scraper gap is structural (TSX listing, Nasdaq-derived calendar):
    # any non-US name will come up UNRESOLVED and needs a manual --set.
    # KXMAMDANIMENTION unblocked same day (Jack "allowlist KXMAMDANIMENTION").
    # Its announcement times are BROADCAST UNRESOLVED, so the call-window
    # freeze cannot protect those events — quoting runs on ticker-date cutoff
    # alone, and the OPEN parse_event_date listing-date bug is unpatched again.
    # FORCE_EVENTS (Jack 2026-08-07 "should also be quoted across markets"):
    # NCLH/FSLR held only reduce-only orphan positions, which do NOT count as
    # sticky membership — so their events ranked as NEW and the $2/day
    # re-entry rate floor excluded every sibling strike (books are 6k-26k deep
    # vs target 1000; our 20-contract share estimates in pennies/day).
    # Forcing bypasses the floors + hopeless exit ONLY — cutoff, bands, caps
    # and budget still apply. DKNG entry is self-limiting (stand-down 08:20
    # ET 8/7 = call 08:30 minus the 10-min override buffer, settles same
    # day); prune it on the next touch.
    # IMM_MAX_MARKETS (the distinct-EVENTS cap) 75 -> 100 (Jack 2026-09-01
    # "also increase event threshold to 100"): the 9/1 family enrollment
    # (FT/APP/state gas) pushed selection to 70/75 events — the cap was about
    # to become the silent breadth governor instead of the collateral budget.
    # 100 -> 150 (Jack 2026-09-10 pm "increase 100 event cap to 150"): the
    # *CC credit-card family (30 events, 3 strikes each after the ROI cap)
    # took the book to 100/100 the moment it was allowed in, with three CC
    # events left out as not_ranked. 150 -> 200 (Jack 2026-09-22, 8707d5e):
    # the *ADS/*POS family events were not_ranked behind a full 150.
    # 200 -> 1000 (Jack 2026-10-01 "increase event cap to 1000"): the October
    # Carbon Arc programs opened 00:00 ET 10/1 into a full 200/200 book; 12
    # of their events (41 strikes: TGTCC, WMTCC, LULUCC, DKSCC, VELOPOS,
    # REDBULLPOS, MOUNTAINDEWPOS, ROGUEPOS, STREAMINGADS, SPORTSBOOKADS,
    # TEENCLOTHADS, NFLXAPP) sat not_ranked -- silently skipped, logged as
    # "gone" in selection_events. 1000 takes the cap out of play; the
    # collateral budget below is the breadth governor again (~$45k of $50k
    # in use at the change, ladder + inventory reserve). Launcher env change
    # => task-level restart (restart_imm.ps1 -Task), a python kill keeps the
    # stale env.
    # KXAAAGASW paused (Jack 2026-08-31 "pause KXAAAGASW": lifetime net
    # -$298, negative in every window; dailies + diesel stay live). The
    # entry was lost once on 9/1 when the pause sat uncommitted through a
    # main sync — it is committed now; prefix-matched, catches state
    # weeklies too. Open weekly positions ride to the 9/7 settlement.
    # KXDIESELW blocked (Jack 2026-10-06 "Block diesel weeklies"): since 9/06
    # -$311 settled/marked fill P&L vs $34 reward credits, every week net
    # negative (SEP07 week -$300 vs $28). Prefix-matched: the weekly only --
    # KXDIESELD dailies and the KXDIESELMAXY/MINY/YE annuals stay live. The
    # open OCT12 position (177 YES on $6.22-6.26) rides to settlement.
    # NQE STRIKE-SUFFIX BLOCK (Jack 2026-09-11 pm "implement the suffix
    # block"): the mention family's "Event does not qualify" leg is frozen
    # IN CODE by market-ticker suffix (incentive_mm.MARKET_BLOCK_SUFFIXES,
    # default NQE; env IMM_BLOCK_MARKET_SUFFIXES overrides, empty disables).
    # Not an IMM_BLOCKLIST entry: that list is series-PREFIX matched and
    # would need one entry per event. Word legs beside it are untouched.
    # Same freeze semantics: no orders, resting NQE quotes cancelled on the
    # next cycle, open NQE positions ride to settlement (SEP12 -10, SEP22
    # +45 at the time). Code-only change, so a python kill reloads it; the
    # env knob needs the task-level restart like every other env change.
    # SATURDAY x1.5 (Jack 2026-09-12 "Saturday multiplier of 1.5x, only on
    # long-dated families"): IMM_SAT_SIZE_MULT=1.5 scales every rung on the
    # ET Saturday calendar day for all series EXCEPT the dailies + temp
    # (code default IMM_SAT_MULT_EXCLUDE=KXAAAGAS,KXDIESEL,KXRAIN,KXTEMP).
    # From the 8/8-9/11 weekday-vs-weekend study: Saturday fills per resting
    # contract 0.20 vs 0.64 weekday at the same rent per contract and the
    # same loss per fill (net +1.17c vs +0.44c per resting contract-day,
    # +5.8c vs +0.7c per FILLED contract); Sunday is the worst day (net
    # -0.61c/ct-day) and gets nothing; the dailies fill at the same rate
    # every day and are excluded. Composes with the 3-7am x2 (Sat 3-7am =
    # x3 on the global ladder; TOTAL_SIZE_MULT_CAP still bounds hour x ref).
    # Env knob => task-level restart (restart_imm.ps1 -Task). Re-measured
    # every Monday 07:40 ET by "KL imm saturday-tracker"
    # (imm_saturday_tracker.py): rent per filled contract, turnover, loss
    # per fill by day type since 9/12 vs the baseline. 4 Saturdays of
    # evidence at deploy — a hypothesis under test.
    # QUIET HOURS 3-7 -> 0-9 ET, LONG-DATED ONLY (Jack 2026-09-12 "extend to
    # midnight to 10am ET for longdated. ensure future daily families are
    # excluded"): the post-temp hour study (8/8-9/11 weekdays, fills scored
    # to settlement) put the turnover cliff at the US open, not at 8am —
    # long-dated fills per resting contract-hour 0.010 (0-2), 0.005 (3-7 at
    # x2), 0.009 (8-9) vs 0.036+ from 10am; net 7.1c / 11.1c / 5.8c per
    # fill. The 8am jump in the overall numbers was the gas dailies (0.093
    # per contract-hour, 3.2c lost per fill at the AAA print) and rain
    # (11.8c), so dailies now take NO global window at all: incentive_mm
    # is_daily_series() = prefix floor (KXAAAGAS,KXDIESEL,KXRAIN,KXTEMP,
    # IMM_DAILY_PREFIXES) + a structural class refreshed from the live
    # program feed every universe refresh (dated tickers, >=2 live event
    # dates, p75 program window <= 30h; persisted to daily_series.json), so
    # future daily families are excluded without a launcher edit. Their own
    # per-series windows (16-1 / 19-1 halvings) still apply. Expected
    # +$30/weekday modelled from doubling 0-2 and 8-9 on the long-dated
    # book. Env knob => task-level restart.
    # IMM_FORCE_EVENTS EMPTIED (Jack 2026-09-27, ROI scan: "fix these"). The
    # 8/14 entries had outlived their reasons: KXEARNINGSMENTIONDKNG-26AUG07
    # settled 8/7 ("prune it on the next touch") and KXNCLH-26OCTPAX has had
    # no live program since at least 9/20 -- both dead text. The force on
    # KXFSLR-26OCTMWSOLD (added so the $2/day re-entry rate bar would not
    # shut out sibling strikes of an event held only as orphaned inventory)
    # also switched off the hopeless exit, so 11 selected strikes projecting
    # under the $1.00 cliff for the period ending 9/28 03:02Z kept quoting
    # for zero payout -- four of them (4800/5000/5100/5200) at $0.02-0.06.
    # Without the force they face the ordinary rules: near-cliff holds 4100/
    # 4200 ($0.92 projected) to completion, 4500/4600 are over $1, the rest
    # leave after the 30-minute clock (positions ride). The rate bar only
    # binds an event with no quoting member left, which is the right answer
    # for strikes earning ~$0.04-0.17/day. Env knob => task-level restart.
    # EVERY monthly rain city OFF the blocklist (Jack 2026-10-01: "quote these
    # monthly rain markets, with an algorithm like how you quote the dailies
    # KXRAINCHIM-26OCT, KXRAINAUSM-26OCT", then "yes add all the cities"):
    # they quote only through the code's RAIN_MONTHLY_* gate (fail closed,
    # out while it rains at the station, cutoff 22:00 ET the day before the
    # month's last day). Takes effect on a task-level restart
    # (restart_imm.ps1 -Task).
    # MONDAY 10/5 STEP, SCHEDULED (Jack 2026-10-02: "make both changes for
    # monday"): from 00:00 ET 2026-10-05 the quiet hours become 0-8 ET x3
    # (hour 9 back to x1) and the earnings family x2 (was x1.5). The _NEXT /
    # _FROM pairs switch on the clock inside the bot, so Monday needs no
    # restart; IMM_HOUR_SIZE_MULT=0-9:2.0 stays in force until then. Weekdays
    # 9/15-10/1, long-dated ex-ladders, rent minus 24h mark-out per 1k
    # resting contract-hours: 0-8 ET at x2 +23c (positive 11 of 13 days),
    # 10-23 ET at x1 +17c, hour 9 at x2 -25c (worst mark-out of the night,
    # mostly KXRT and econ). Composes with Saturday x2: Saturday 0-8 ET runs
    # x6 from 10/10. Earnings: +$81/day net, positive 13 of 14 days, net per
    # contract-hour up since the x1.5. Revert either by deleting its pair +
    # restart_imm.ps1 -Task. imm_saturday_tracker reads the window per day.
    # CAPACITY CAPS DOUBLED SO THEY STOP BINDING (Jack 2026-10-03: "Double collateral
    # budget, slot caps, resting order cap, and candidate book cap. I don't
    # want the caps to trip"). The 7:00 email's new risk-controls table had
    # these binding or close over the 24h to 09:40 ET:
    # - COLLATERAL_BUDGET 100000 -> 300000, not 200000 (Jack picked $300k).
    #   Sticky members alone reserved ~$224k at the Saturday-night x4, so
    #   $200k would still have refused every newcomer (79 markets, ~$209/day
    #   modelled). Still a modelled reservation, not cash (~$7.6k cash,
    #   ~$28k account value). Note the hour/Saturday multipliers inflate
    #   the reservation: from 10/10 Saturday 0-8 ET runs x6, which can push
    #   it past $300k on those nights, so the budget may bind there again.
    # - MAX_TOTAL_RESTING 4000 -> 8000: peaked at 3,743 (94%). Untested
    #   above 4,000 resting on Kalshi.
    # - MAX_CANDIDATE_BOOKS 5000 -> 10000 (was the code default): peaked at
    #   4,647. More books read per refresh only once the universe grows.
    # - Slot caps were doubled the same afternoon, then REVERTED (Jack
    #   2026-10-03: "revert Open-scan slots, finecon slots, per-event strike
    #   caps"): open scan back to 60 slots / 3 per event, finecon 25 / 3,
    #   per-event strike caps back to the code spec (IMM_EVENT_TOP_N_MULT
    #   unset = 1). Strikes admitted past 3 per event are trimmed back by the
    #   lifetime ledger (earliest 3 keep their slots, positions ride); scan
    #   and finecon members admitted above the cap ride to completion and
    #   nothing new enters until attrition brings the tier under it.
    # - NOT changed: IMM_MAX_PLACEMENTS_PER_CYCLE stays 1000 (Jack: leave
    #   it). Placements are paced at 12/s for the shared API budget, so a
    #   bigger book defers more placements a cycle instead.
    # Env change => task-level restart (restart_imm.ps1 -Task).
    # DAILY LOSS HALT 1200 -> 2000 (Jack 2026-10-03 "Increase daily halt to
    # $2000"; was the code default since 7/21). Halts everything until the
    # 5am-CT roll at IMM P&L today (realized + marked) <= -$2,000. At the
    # change: -$826 today, 69% of the old limit. imm_dashboard reads this
    # var from here (its fallback is 1200), so set it here, not in code.
    # OPEN SCAN OFF (Jack 2026-10-03: "yes do 1 - 4 but also keep Jobs,
    # travel and energy and any other markets that are near-breakeven"):
    # IMM_SCAN_TOP_N=0. Over 9/06-10/03 the scan-only series netted +$234
    # (rewards at the paid rate + trading marked to market): the 83 that
    # moved to the normal book the same day as open-scan graduates
    # (incentive_mm SCAN_GRADUATE_SERIES) made +$501, the other 46 lost $267.
    # Members leave at the next refresh (quotes cancelled, positions ride).
    # WEBSOCKET BOOKS -- ON (Jack 2026-10-04: "turn on the websocket books
    # now"). The quote loop reads each managed book from the kalshi_ws.py
    # feed whenever the feed trusts it, and from REST otherwise.
    # REST AUDIT: 1 read in 20 is also read from REST and traded on. Too many
    # mismatches trip every read back to REST, reconnect the feed and email
    # ALERT ws_audit; it re-arms after 15 min clean.
    # WHY: on 10/4 a plain cycle spent ~60 of its ~137s on REST book reads.
    # Shadow from 10/4 17:51Z: 99.7-100% of books equal to REST, 0 gaps.
    # Status latency.ws (books_used, rest_fallback, audit) and latency.cycle
    # (reads_s) show it working.
    # STILL OFF: IMM_WS_FAST=1 (fast stale-quote cancels; the check runs dry).
    # The event sweep breaker stays dry (code default).
    # BACK OUT: IMM_WS=shadow (or off) here, then restart_imm.ps1 -Task.
    # EVENT SWEEP BREAKER -- ON (Jack 2026-10-04: "do Whole event, both sides,
    # 2 min. but monitor to make sure it's effective and net positive").
    # Trigger: one of our maker orders filled for all that was left of it.
    # Then every quote we have in that event (every market, both sides) is
    # pulled for IMM_SWEEP_HOLD_SECS=120.
    # Backtest 9/6-10/4 for this setting: +$21/day at 5m mark-outs and +$30 at
    # 30m, net of ~$4/day of reward.
    # IMM_SWEEP_HOLDOUT=0.2: a random 20% of trips stay a DRY CONTROL. Once
    # live, a pull hides the losses it prevents, and the control trips show
    # them. ws_stale_score.py nets the live trips against them ("LIVE,
    # netted against its control trips"), and a daily task reports it. Set
    # the holdout to 0 once it has proven out.
    # BACK OUT: IMM_SWEEP_BREAKER=dry here, then restart_imm.ps1 -Task.
    $ProbeEnv = "set IMM_SCAN_TOP_N=0&& set IMM_FORCE_EVENTS=&&set IMM_BLOCKLIST=KXCRYPTOSTRUCTURE,KXAAAGASW,KXDIESELW&& set IMM_LEVELS=0:20&& set IMM_TEMP_LEVELS=0:20&& set IMM_MAX_POSITION=150&& set IMM_MAX_TOTAL_RESTING=8000&& set IMM_MAX_EVENT=1000&& set IMM_LADDER_MODE=atref&& set IMM_MAX_MARKETS=1000&& set IMM_COLLATERAL_BUDGET=300000&& set IMM_ORDER_TTL_SECS=1800&& set IMM_ORDER_REFRESH_SECS=1500&& set KALSHI_RATE_LIMIT_MS=25&& set IMM_MAX_PLACEMENTS_PER_CYCLE=1000&& set IMM_HOUR_SIZE_MULT=0-9:2.0&& set IMM_HOUR_SIZE_MULT_NEXT=0-8:3.0&& set IMM_HOUR_SIZE_MULT_FROM=2026-10-05&& set IMM_EARNINGS_SIZE_MULT_NEXT=2.0&& set IMM_EARNINGS_SIZE_MULT_FROM=2026-10-05&& set IMM_SAT_SIZE_MULT=1.5&& set IMM_BENCH_COOLDOWN=3600&& set IMM_MAX_CANDIDATE_BOOKS=10000&& set IMM_DAILY_LOSS_LIMIT=2000&& set IMM_WS=on&& set IMM_SWEEP_BREAKER=on&& set IMM_SWEEP_HOLD_SECS=120&& set IMM_SWEEP_HOLDOUT=0.2&&"
}

while ($true) {
    $log = Join-Path $Repo ("run-logs\incentive-mm\incentive-mm-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    Add-Content $log "$(Get-Date -Format u) launcher: starting incentive_mm live$(if ($Probe) { ' [MICRO-PROBE]' })"
    # cmd-level redirection appends raw utf-8 bytes; PowerShell's *>> would
    # write UTF-16 and wrap stderr lines in NativeCommandError noise.
    & cmd.exe /c "set PYTHONPATH=$UserSite&& set IMM_POLL_SECS=$PollSecs&& $ProbeEnv`"$Python`" `"$Repo\incentive_mm.py`" --live >> `"$log`" 2>&1"
    Add-Content $log "$(Get-Date -Format u) launcher: bot exited (code $LASTEXITCODE); restarting in 30s"
    Start-Sleep -Seconds 30
}
