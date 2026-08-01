' Fleet watchdog with NO console flash (runs every 15 min): starts any
' "KL crypto_touch_mm *-M*" task sitting in Ready (= bot died / was closed).
' Disabled tasks are untouched — Disable-ScheduledTask is the deliberate
' pause switch.
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -Command ""Get-ScheduledTask -TaskName 'KL crypto_touch_mm *-M*' | Where-Object State -eq 'Ready' | Start-ScheduledTask""", 0, True
