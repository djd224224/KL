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
# The rich HTML morning email is sent by the separate "KL incentive_mm DIGEST"
# task (send_imm_digest.py), mirroring the crypto fleet. Suppress the bot's own
# plain-text one-liner email so there's no duplicate; it still STORES the daily
# summary in status_incentive_mm.json (the digest reads reward figures from it).
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
    # IMM_BALANCE_DROP_HALT=5000 and the daily-loss halt underneath.
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
    # KXAAAGASW paused (Jack 2026-08-31 "pause KXAAAGASW": lifetime net
    # -$298, negative in every window; dailies + diesel stay live). The
    # entry was lost once on 9/1 when the pause sat uncommitted through a
    # main sync — it is committed now; prefix-matched, catches state
    # weeklies too. Open weekly positions ride to the 9/7 settlement.
    $ProbeEnv = "set IMM_FORCE_EVENTS=KXNCLH-26OCTPAX,KXFSLR-26OCTMWSOLD,KXEARNINGSMENTIONDKNG-26AUG07&& set IMM_BLOCKLIST=KXCRYPTOSTRUCTURE,KXRAINAUSM,KXRAINCHIM,KXRAINDALM,KXRAINDENM,KXRAINHOUM,KXRAINMIAM,KXRAINNYCM,KXRAINSEAM,KXRAINSTPM,KXAAAGASW&& set IMM_LEVELS=0:20&& set IMM_TEMP_LEVELS=0:20&& set IMM_MAX_POSITION=150&& set IMM_MAX_TOTAL_RESTING=4000&& set IMM_MAX_EVENT=1000&& set IMM_LADDER_MODE=atref&& set IMM_MAX_MARKETS=100&& set IMM_COLLATERAL_BUDGET=50000&& set IMM_ORDER_TTL_SECS=1800&& set IMM_ORDER_REFRESH_SECS=1500&& set KALSHI_RATE_LIMIT_MS=25&& set IMM_MAX_PLACEMENTS_PER_CYCLE=250&& set IMM_HOUR_SIZE_MULT=3-7:2.0&& set IMM_BALANCE_DROP_HALT=5000&& set IMM_BENCH_COOLDOWN=3600&&"
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
