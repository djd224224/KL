' Fleet watchdog with NO console flash (runs every 15 min): starts any
' crypto bot task sitting in Ready (= bot died / was closed). Covers the
' monthly one-touch fleet ("KL crypto_touch_mm *-M*"), the above/below
' fleet ("KL crypto_updown_mm *") — the updown pilot died 2026-08-05 with no
' watchdog coverage and stayed dead for 3 days — AND the annual fleet
' ("KL crypto_annual_mm *", added 2026-08-13). -ErrorAction keeps the
' sweep working even when a fleet's tasks are not registered yet.
' Disabled tasks are untouched — Disable-ScheduledTask is the deliberate
' pause switch.
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -Command ""Get-ScheduledTask -TaskName 'KL crypto_touch_mm *-M*','KL crypto_updown_mm *','KL crypto_annual_mm *' -ErrorAction SilentlyContinue | Where-Object State -eq 'Ready' | Start-ScheduledTask""", 0, True

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
'
' Ready-only is NOT sufficient here, which is why this goes through a guard
' script instead of an inline Start-ScheduledTask (2026-08-06). Unlike the
' crypto tasks, run_incentive_mm.ps1 is a blocking supervisor that relaunches
' python forever -- so Ready means the LAUNCHER died, and its python child may
' still be alive and trading. Starting the task then puts a SECOND bot on the
' same live account. At 12:41:59Z that state was real (task Ready, pid 32644
' quoting) and this watchdog was 12 minutes from doing exactly that.
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\jackd\Documents\KL\watchdog_imm_guard.ps1""", 0, True
