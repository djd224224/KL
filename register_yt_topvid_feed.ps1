# Registers "KL yt-topvid-feed" -- the global top-video markets' durable
# YouTube Data API feed (yt_topvid_feed.py): one pass every 20 min (:05, :25,
# :45), plus one 2 min after logon so a reboot costs at most that. Safe to
# re-run: -Force replaces the definition.
#
# WHY (Jack 2026-10-14: "set up the feed and run the preview"): the IMM's #2
# pilot (KXYTTOPVIDEOG2D) quotes with no fair, and the only record of its
# candidates' view counts is KL-data\youtube-collect\yt_collect.py, a bare
# process (no task, gone on a reboot) whose --until is 2026-10-18 12:00Z.
# This feed writes yt_topvid_snapshots.jsonl beside it.
#
# WINDOWLESS via run_yt_topvid_feed_hidden.vbs (wscript //B). Interactive,
# like every KL task (it runs while Jack is logged on, as the bot does).
# :05/:25/:45 keeps it off ytw-feed's :00; a pass takes ~15 s.
# StartWhenAvailable reruns a missed slot; the script's own 15-min gap guard
# makes a logon + slot overlap harmless; IgnoreNew stops a slow pass from
# stacking.
#
# Started once on registration so the feed begins now.
# Remove: Unregister-ScheduledTask -TaskName 'KL yt-topvid-feed' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL yt-topvid-feed"
$Vbs = Join-Path $Repo "run_yt_topvid_feed_hidden.vbs"
$Log = "C:\Users\jackd\Documents\KL-data\youtube-collect\yt_topvid_feed.log"
if (-not (Test-Path $Vbs)) {
    Write-Host "Missing $Vbs -- has the sync pulled it onto $Repo yet?"
    exit 1
}

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B `"$Vbs`""
$now = Get-Date
$hour = $now.Date.AddHours($now.Hour)
$first = @(5, 25, 45, 65) | ForEach-Object { $hour.AddMinutes($_) } |
    Where-Object { $_ -gt $now } | Select-Object -First 1
$every = New-ScheduledTaskTrigger -Once -At $first `
    -RepetitionInterval (New-TimeSpan -Minutes 20)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT2M"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$desc = "Global top-video (#1 / #2) YouTube API feed (2026-10-14): viewCount snapshots of " +
        "the candidates every 20 min -> KL-data\youtube-collect\yt_topvid_snapshots.jsonl " +
        "for a #2 fair. Remove: Unregister-ScheduledTask -TaskName 'KL yt-topvid-feed' -Confirm:`$false"

Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Trigger @($every, $logon) `
    -Principal $principal -Settings $settings -Description $desc | Out-Null
Start-ScheduledTask -TaskName $TaskName

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host "Registered: every 20 min from $first + at logon."
Write-Host "Log:   $Log"
Write-Host "State: python $Repo\yt_topvid_feed.py --status"
