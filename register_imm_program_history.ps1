# Register "KL imm program-history": a daily pure-python job that refreshes the
# Kalshi incentive-program cache and appends one per-series row per day to
# run-logs/incentive-mm/program_history.jsonl.
#
# WHY THIS EXISTS
# reward_programs.json is fully OVERWRITTEN on every run and its paid flags
# mutate in place, so there has never been a time series of reward-program
# SUPPLY. That made the largest regime break in this bot's history invisible in
# data: when Kalshi pulled the hourly KXTEMP pools around 2026-08-07, the family
# that had been ~67% of August credits vanished, and the only record of it is
# prose in a memory file. The roll-up shipped 2026-09-06 but had no scheduler,
# so by 2026-09-13 it had exactly two samples (09-07 and 09-12), both from
# manual runs.
#
# SAFETY (traced before scheduling)
# With ONLY --refresh-programs, main() runs one branch: refresh_programs().
# It never reads or writes reward_credits.csv (the hand-maintained credit
# ledger), never rewrites reward_calibration.json or reward_est_cache.json,
# and never scans the ~20 GB of cycle logs. It writes exactly two files, both
# atomically via tmp + os.replace: reward_programs.json and
# program_history.jsonl. It is idempotent per UTC day.
#
# API load: ~170 paged GETs against /incentive_programs in ~40s, capped at
# 10 calls/s by the client's default throttle — ~1.6% of the account's read
# budget. It shares the key with the LIVE incentive_mm bot, which is why the
# slot is AFTER the morning email lineup rather than inside it.
#
# TIMING: 07:50 local. The box is on Eastern (tzutil /g -> "Eastern Standard
# Time"), so local == ET. This sits one step past the last IMM morning task
# (07:40 saturday-tracker), clear of the 06:45/12:45/16:45 overrides task, the
# 07:00 stack, and the 07:10/07:20/07:25/07:30 IMM emails. A MORNING ET slot
# also matters for correctness: _append_program_history keys rows on the UTC
# date, so an evening ET run (past 20:00) stamps the NEXT UTC day. The existing
# 2026-09-12 row came from a 2026-09-11 21:07 ET run for exactly that reason.
#
# NOTE the header of register_portfolio_digest.ps1 claims "local (Central) =
# ET" — that is stale for this machine. Do not copy that conversion.
#
# Idempotent: re-running replaces the task definition.

$ErrorActionPreference = "Stop"

$repo    = "C:\Users\jackd\Documents\KL"
$python  = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$pypath  = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
$logFile = "$repo\run-logs\incentive-mm\reward-recon-task.log"
$taskName = "KL imm program-history"

# PYTHONPATH is load-bearing: Task Scheduler hands the process a stripped
# environment that does not resolve the user site-packages where cryptography,
# pytz and requests live. Without it the task "succeeds" (exit 0) having done
# nothing. Same fix as the quote-gaps and portfolio-digest tasks.
# The `>> log 2>&1` redirect must stay LAST on the line — anything after it is
# swallowed into the filename, and imm_reward_recon.py with no flags is a no-op.
$arg = '/c "set PYTHONPATH=' + $pypath + '&& "' + $python + '" "' + $repo + '\imm_reward_recon.py" --refresh-programs >> "' + $logFile + '" 2>&1"'

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $arg -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -Daily -At 7:50am

# AllowStartIfOnBatteries + DontStopIfGoingOnBatteries are NOT the defaults of
# New-ScheduledTaskSettingsSet (DisallowStartIfOnBatteries defaults to $true).
# Omit them and the task silently never fires while the laptop is unplugged,
# with no error recorded anywhere. All 48 existing "KL *" tasks set both.
# StartWhenAvailable makes a missed run (Modern Standby) produce a late row
# rather than a hole.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily refresh of the Kalshi incentive-program cache; appends one per-series supply row per day to program_history.jsonl. Pure python, no LLM dependency." `
    -Force | Out-Null

Write-Host "Registered '$taskName' -> daily 07:50 local"
Get-ScheduledTask -TaskName $taskName |
    Select-Object TaskName, State,
        @{n = "Trigger"; e = { $_.Triggers[0].StartBoundary } } |
    Format-List
