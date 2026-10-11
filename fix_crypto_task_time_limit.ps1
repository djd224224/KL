# Removes the 72h execution time limit from the 15 crypto annual/updown tasks
# ("KL crypto_annual_mm *" x8, "KL crypto_updown_mm *" x7). The touch fleet's
# tasks already have none (PT0S).
# NEEDS ELEVATION - run via fix_crypto_task_time_limit.bat (self-elevates):
# these tasks were registered elevated and are owned by Administrators.
#
# Background (2026-10-11): registered without -ExecutionTimeLimit, the tasks
# got Task Scheduler's 72h default, so every 3 days Task Scheduler ended each
# one by killing its wscript wrapper. The launcher and bot under it ran on,
# orphaned, and the 15-min watchdog started the Ready task again: 4 launchers
# per asset by 10/11, 3 of them relaunching a python every ~30s that the bot's
# singleton lock turned away. crypto_launcher_guard.ps1 makes a relaunch safe;
# this removes the 3-day cycle itself.
#
# Only ExecutionTimeLimit changes: each task's own settings object is edited
# and written back, so the battery flags etc. stay as they are. Like
# fix_updown_battery.ps1, Set-ScheduledTask only updates the definition: the
# running instances keep running.
$tasks = @(Get-ScheduledTask -TaskName "KL crypto_annual_mm *", "KL crypto_updown_mm *" -ErrorAction SilentlyContinue)
foreach ($t in $tasks) {
    $t.Settings.ExecutionTimeLimit = "PT0S"
    try {
        Set-ScheduledTask -TaskName $t.TaskName -Settings $t.Settings -ErrorAction Stop | Out-Null
        Write-Host "patched $($t.TaskName)"
    } catch {
        Write-Host "FAILED $($t.TaskName): $($_.Exception.Message)"
    }
}
Write-Host ""
Get-ScheduledTask -TaskName "KL crypto_annual_mm *", "KL crypto_updown_mm *" | ForEach-Object {
    '{0,-26} {1,-8} timeLimit={2} startOnBattery={3} stopOnBattery={4}' -f $_.TaskName, $_.State,
        $_.Settings.ExecutionTimeLimit, (-not $_.Settings.DisallowStartIfOnBatteries), $_.Settings.StopIfGoingOnBatteries
}
Write-Host ""
Write-Host "Done - all 15 should show timeLimit=PT0S (and State unchanged)."
