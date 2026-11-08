# Registers "KL sms-inbox" -- text replies to Claude (sms_inbox.py): one pass
# every minute, plus one 1 min after logon. Safe to re-run: -Force replaces it.
#
# WHY (Jack 2026-11-08: "can i set it up so if i text back and it goes to
# Claude?", "Yes, build it", then "can you just allow texts in general? not so
# rigid?"): a reply to an alert text comes back to Gmail from
# +19729713381@tmomail.net; this checks T-Mobile's DKIM + SPF, runs claude -p on
# it (the token file, like the other claude -p tasks) and texts the answer
# back. Any question is answered; stops and refreshes run at once; any other
# change (a plan, or un-halt / resume / unwind off / restart / size / wave-set)
# waits for a "YES <code>" reply. Never money or credentials.
#
# WINDOWLESS via run_sms_inbox_hidden.vbs (wscript //B). Interactive, like
# every KL task. A pass with no new text is one IMAP search (~1-2 s, no Claude).
# IgnoreNew + the script's own lock keep a long Claude run (<= 8 min) from
# stacking; the 45-min limit kills a hung pass (answer <= 12 min + a plan run <= 20).
# Remove: Unregister-ScheduledTask -TaskName 'KL sms-inbox' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL sms-inbox"
$Vbs = Join-Path $Repo "run_sms_inbox_hidden.vbs"
if (-not (Test-Path $Vbs)) {
    Write-Host "Missing $Vbs"
    exit 1
}

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B `"$Vbs`""
$first = (Get-Date).AddMinutes(1)
$first = $first.Date.AddHours($first.Hour).AddMinutes($first.Minute)
$every = New-ScheduledTaskTrigger -Once -At $first -RepetitionInterval (New-TimeSpan -Minutes 1)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT1M"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$desc = "Text replies to Claude (2026-11-08): every minute, Gmail -> texts from Jack's number " +
        "(T-Mobile DKIM+SPF checked) -> claude -p (answers anything, changes nothing) -> text back; stops at once, any other change behind a YES code. " +
        "Log run-logs\sms_inbox.log. Remove: Unregister-ScheduledTask -TaskName 'KL sms-inbox' -Confirm:`$false"

Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Trigger @($every, $logon) `
    -Principal $principal -Settings $settings -Description $desc | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host "Registered: every minute from $first + at logon."
Write-Host "Log:    $Repo\run-logs\sms_inbox.log"
Write-Host "Status: python $Repo\sms_inbox.py --status"
