' Fleet watchdog with NO console flash (runs every 15 min): starts any
' "KL crypto_touch_mm *-M*" task sitting in Ready (= bot died / was closed).
' Disabled tasks are untouched — Disable-ScheduledTask is the deliberate
' pause switch.
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -Command ""Get-ScheduledTask -TaskName 'KL crypto_touch_mm *-M*' | Where-Object State -eq 'Ready' | Start-ScheduledTask""", 0, True

' KL incentive_mm (added 2026-08-05, Jack "turn on automatic recovery for
' IMM"). It was NOT covered here, and that is what turned a python crash at
' 13:00:37 into a 23-minute silent outage: the process died, the launcher
' exited, the task fell back to Ready, and nothing was watching it. The crypto
' fleet has had this cover for months; the incentive bot never did.
'
' EXACT name, not a wildcard: "KL incentive_mm DIGEST" and the other "KL imm *"
' helpers are one-shot tasks that live in Ready by design, and starting those
' on a 15-minute timer would fire the digest email four times an hour.
'
' Same Ready-only rule, so Disable-ScheduledTask remains the deliberate pause
' switch for this bot too.
sh.Run "powershell.exe -NoProfile -Command ""Get-ScheduledTask -TaskName 'KL incentive_mm' | Where-Object State -eq 'Ready' | Start-ScheduledTask""", 0, True
