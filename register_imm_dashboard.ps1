# Registers "KL imm dashboard" -- rebuilds the IMM command center page
# (imm_dashboard.py -> run-logs\incentive-mm\dashboard\imm_dashboard.html)
# every 10 minutes. Safe to re-run: -Force replaces the definition.
#
# WINDOWLESS: runs pythonw.exe directly (the vercel-logger pattern), so no
# console flashes every 10 minutes; the script writes its own log via --log.
# The Python path is taken from the sibling "KL imm quote-gaps" task when it
# exists, with pythonw.exe swapped in for python.exe; the literal path is the
# fallback.
#
# CADENCE: a warm build is ~2 s from the local logs; every 30 min it also
# re-reads Kalshi's incentive-program feed, and every 60 min it re-runs the
# 7:20 email's gap estimator and the pick-off scan (~30-45 s). A cold start
# (no cache) parses 30 days of logs, ~2 min, once. IgnoreNew stops a slow
# run from stacking.
#
# Started once on registration so the page exists immediately.
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL imm dashboard"
$Script = Join-Path $Repo "imm_dashboard.py"
$Log = Join-Path $Repo "run-logs\incentive-mm\dashboard\dashboard-task.log"

$pyw = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\pythonw.exe"
$sib = Get-ScheduledTask -TaskName "KL imm quote-gaps" -ErrorAction SilentlyContinue
if ($sib) {
    $a = ($sib.Actions | Select-Object -First 1).Arguments
    if ($a -match '"([^"]*\\python\.exe)"') {
        $cand = $Matches[1] -replace 'python\.exe$', 'pythonw.exe'
        if (Test-Path $cand) { $pyw = $cand }
    }
}
if (-not (Test-Path $pyw)) {
    Write-Host "pythonw.exe not found at $pyw -- edit this script's `$pyw and re-run."
    exit 1
}

New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null

$action = New-ScheduledTaskAction -Execute $pyw `
    -Argument "`"$Script`" --log `"$Log`"" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $TaskName

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host "Registered: every 10 minutes with $pyw."
Write-Host "Page: $Repo\run-logs\incentive-mm\dashboard\imm_dashboard.html"
Write-Host "Log:  $Log"
