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
    $ProbeEnv = "set IMM_BLOCKLIST=KXRAINAUSM,KXRAINCHIM,KXRAINDALM,KXRAINDENM,KXRAINHOUM,KXRAINMIAM,KXRAINNYCM,KXRAINSEAM,KXRAINSTPM&& set IMM_LEVELS=0:20&& set IMM_TEMP_LEVELS=0:20&& set IMM_MAX_POSITION=150&& set IMM_MAX_TOTAL_RESTING=4000&& set IMM_MAX_EVENT=1000&& set IMM_LADDER_MODE=atref&& set IMM_MAX_MARKETS=50&& set IMM_COLLATERAL_BUDGET=13000&& set IMM_ORDER_TTL_SECS=1800&& set IMM_ORDER_REFRESH_SECS=1500&& set KALSHI_RATE_LIMIT_MS=25&& set IMM_MAX_PLACEMENTS_PER_CYCLE=250&& set IMM_HOUR_SIZE_MULT=3-7:2.0&& set IMM_BALANCE_DROP_HALT=5000&&"
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
