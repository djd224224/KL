# Registers "KL imm fair-marks": every 15 minutes, imm_fair_marks.py logs every
# fair model's view beside the market (fair_marks_<UTC date>.jsonl), archives
# the odd-shaped fair files raw, and looks up the results of markets that left
# their model's file (Jack 2026-11-26: "Yes, build it", the fair-model
# scorecard). The review scores the models daily (imm_fair_scorecard.py,
# evidence section 13). Safe to re-run: -Force replaces the definition.
#
# WINDOWLESS: runs pythonw.exe directly (the dashboard's pattern), so no console
# flashes every 15 minutes; the script appends a one-line summary to --log.
# A run takes ~1 s, plus ~12 s once an hour for Kalshi's top of book on the
# markets the bot is not cycling (signed reads, paced by kalshi_reads).
# IgnoreNew stops a slow run from stacking. IMM_FAIR_MARKS=0 turns it off.
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL imm fair-marks"
$Script = Join-Path $Repo "imm_fair_marks.py"
$Log = Join-Path $Repo "run-logs\incentive-mm\fair-marks-task.log"

$pyw = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\pythonw.exe"
$sib = Get-ScheduledTask -TaskName "KL imm dashboard" -ErrorAction SilentlyContinue
if ($sib) {
    $exe = ($sib.Actions | Select-Object -First 1).Execute
    if ($exe -and (Test-Path $exe)) { $pyw = $exe }
}
if (-not (Test-Path $pyw)) {
    Write-Host "pythonw.exe not found at $pyw -- edit this script's `$pyw and re-run."
    exit 1
}

$action = New-ScheduledTaskAction -Execute $pyw `
    -Argument "`"$Script`" run --log `"$Log`"" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $TaskName

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host "Registered: every 15 minutes with $pyw."
Write-Host "Log: $Log"
