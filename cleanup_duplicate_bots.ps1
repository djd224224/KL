# Kills every duplicate/stacked crypto-bot process chain found on 2026-08-09.
# Background: three generations of the monthly one-touch fleet (started 8/5,
# 8/6, 8/7 wake-respawns) and two generations of the updown fleet were running
# CONCURRENTLY - stacked ladders on the same markets. The v2.5 singleton stops
# newcomers but never evicts an incumbent, so takeovers accumulated bots.
#
# This kills ALL bot chains (pythons + their cmd/powershell/wscript ancestors),
# including the currently-task-owned ones. Recovery is automatic and needs no
# manual start: tasks flip to Ready and the WATCHDOG (runs every 15 min,
# covers 'KL crypto_touch_mm *-M*' AND 'KL crypto_updown_mm *') restarts one
# clean chain per market; the singleton then keeps it at one. Order TTL (600s)
# flushes all stacked resting quotes during the gap.
#
# Deliberately NOT killed: the IMM bot chain (restarted healthy 16:24Z today)
# and this session's own ancestry. Two pids adjacent to IMM (16128, 2200,
# 31516's parents) are left alone out of caution.
# No elevation needed - these are your own processes.

$kill = @(
    10156,12812,39048,6500, 10440,29892,10128, 10612,15176,30312,33500,
    11428,19412,33384, 12744,18640,26048, 12756,29400,12128,31708,
    13628,25668,21076,13576, 14392,2208,38640, 14656,7988,22472,
    15168,12484,5100, 1528,20380,17768, 1600,33588,22340,
    16396,36944,32964, 16988,30248,24484, 17920,32216,19184,
    18708,9792,25120,33292, 19160,14412,22544,5716, 19852,10716,13400,
    20848,31800,25888, 21636,28852,26396, 24000,35560,21672,39748,
    24428,24280,9624,7472, 27324,32140,33208, 27620,28724,25172,31392,
    28080,31268,5892,30808, 28216,30532,32992,17880, 28764,25116,16296,27440,
    28956,24308,32592, 29896,15496,33068, 30012,28596,32552,31324,
    30184,18732,8292,15052, 30284,31232,11408, 31096,30308,16448,12732,
    31404,13504,524,33748, 31516, 31924,9744,32756, 32192,36508,2552,
    32460,20068,15396,26640, 32988,24188,32420, 34296,6556,24556,5564,
    34360,37664,9396, 3532,39028,9564,31064, 36112,12920,13344,
    36488,34328,29792, 36748,37548,30928, 36820,34768,4804,
    38844,37684,38664, 4628,34116,20284, 6384,19868,9808,14040,
    7380,1008,3892, 8188,132,39628,23032, 8744,5028,13836
)

# Only ever kill these process types - if a pid was recycled to something
# else since the survey, skip it.
$allowed = "python", "cmd", "powershell", "wscript"

$before = @(Get-Process python -ErrorAction SilentlyContinue).Count
$killed = 0; $skipped = 0
foreach ($p in $kill) {
    $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    if ($allowed -notcontains $proc.ProcessName) { $skipped++; continue }
    try { Stop-Process -Id $p -Force -ErrorAction Stop; $killed++ } catch {}
}
Start-Sleep -Seconds 5
$after = @(Get-Process python -ErrorAction SilentlyContinue).Count
Write-Host ""
Write-Host "pythons before: $before   killed: $killed   name-mismatch skipped: $skipped   pythons now: $after"
Write-Host "(expect ~1-2 remaining: the IMM bot, plus any transient)"
Write-Host ""
Get-ScheduledTask -TaskName "KL crypto_touch_mm *","KL crypto_updown_mm *" -ErrorAction SilentlyContinue |
    Group-Object State | ForEach-Object { Write-Host ("tasks {0}: {1}" -f $_.Name, $_.Count) }
Write-Host ""
Write-Host "Done. The watchdog restarts every Ready bot task within 15 minutes"
Write-Host "(one clean bot per market); stacked resting orders TTL out within 10."
