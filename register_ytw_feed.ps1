# Registers "KL ytw-feed" -- the KXYTVIEWSW pilot's durable YouTube Data API
# feed (yt_pilot_feed.py): one pass hourly at :00, with the 3-hourly
# new-upload scan inside it, plus one 2 min after logon so a reboot costs at
# most that. Safe to re-run: -Force replaces the definition.
#
# WHY (2026-10-06): the pilot's fair (yt_weekly_fair.py) read only the
# snapshots of KL-data\youtube-collect\yt_artists.py, a bare process (no
# task, gone on a reboot) whose --until is 2026-10-18 12:00Z; the 26OCT18
# week trades to ~10/20. This feed writes yt_pilot_snapshots.jsonl beside it,
# which the fair merges with the collector's file.
#
# WINDOWLESS via run_ytw_feed_hidden.vbs (wscript //B). Interactive, like
# every KL task (it runs while Jack is logged on, as the bot does). :00
# because the fair's chart-day windows open and close at 15:00Z on the hour;
# a pass takes ~15 s. StartWhenAvailable reruns a missed slot; the script's
# own 40-min gap guard makes a logon + :00 overlap harmless; IgnoreNew stops
# a slow pass from stacking.
#
# Started once on registration so the feed begins now.
# Remove: Unregister-ScheduledTask -TaskName 'KL ytw-feed' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL ytw-feed"
$Vbs = Join-Path $Repo "run_ytw_feed_hidden.vbs"
$Log = "C:\Users\jackd\Documents\KL-data\youtube-collect\yt_pilot_feed.log"
if (-not (Test-Path $Vbs)) {
    Write-Host "Missing $Vbs -- has the sync pulled it onto $Repo yet?"
    exit 1
}

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B `"$Vbs`""
$now = Get-Date
$hourly = New-ScheduledTaskTrigger -Once -At $now.Date.AddHours($now.Hour + 1) `
    -RepetitionInterval (New-TimeSpan -Hours 1)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT2M"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$desc = "KXYTVIEWSW pilot YouTube API feed (2026-10-06): hourly viewCount snapshots of " +
        "the pilot artists' videos -> KL-data\youtube-collect\yt_pilot_snapshots.jsonl " +
        "for yt_weekly_fair. Remove: Unregister-ScheduledTask -TaskName 'KL ytw-feed' -Confirm:`$false"

Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Trigger @($hourly, $logon) `
    -Principal $principal -Settings $settings -Description $desc | Out-Null
Start-ScheduledTask -TaskName $TaskName

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host "Registered: hourly at :00 from $($now.Date.AddHours($now.Hour + 1)) + at logon."
Write-Host "Log:   $Log"
Write-Host "State: python $Repo\yt_pilot_feed.py --status"
