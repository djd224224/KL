# Registers + starts the 7 crypto_updown_mm weekly-tenor tasks.
# NEEDS ELEVATION — run via register_updown_tasks.bat (self-elevates) or from
# an admin PowerShell. Idempotent: -Force re-registers cleanly, and starting a
# Running task is a no-op (the shim waits, so a healthy task stays Running —
# no duplicate fleets).
$assets = "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"
foreach ($a in $assets) {
    Register-ScheduledTask -TaskName "KL crypto_updown_mm $a" `
        -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument "//B `"C:\Users\jackd\Documents\KL\run_crypto_updown_mm_hidden.vbs`" $a weekly 30") `
        -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Force | Out-Null
    Start-ScheduledTask -TaskName "KL crypto_updown_mm $a"
    Write-Host "registered + started KL crypto_updown_mm $a"
}
Write-Host ""
Get-ScheduledTask -TaskName "KL crypto_updown_mm *" | Format-Table TaskName, State -AutoSize
Write-Host "Done - all 7 should show Running (launchers add 0-45s jitter before the bots quote)."
