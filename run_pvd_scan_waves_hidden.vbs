' Wave relight scanner with NO console flash (task "KL pvd-scan waves", every 15 min).
' Runs run_pvd_scan_waves.cmd: the political wave 11/06 and the 2028 election wave 11/06,
' relit / new programs -> post_vote_dead_books.json, then the 2028 wave's room + band.
' Added 2026-11-06 (Jack: "also do 2 and 3").
' Remove: Unregister-ScheduledTask -TaskName 'KL pvd-scan waves' -Confirm:$false
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c """"C:\Users\jackd\Documents\KL\run_pvd_scan_waves.cmd""""", 0, True
