' KXYTVIEWSW pilot's YouTube API feed with NO console flash ("KL ytw-feed",
' hourly at :00 and at logon; register_ytw_feed.ps1). One pass of
' yt_pilot_feed.py, then exit. The script keeps its own log
' (KL-data\youtube-collect\yt_pilot_feed.log); stdout/stderr -- a traceback --
' land in yt_pilot_feed.stdout.log beside it. The exit code is passed through
' so the task's Last Result shows a failed pass (1 partial, 2 no key / list).
' Added 2026-10-06: the pilot's feed must outlive yt_artists.py's 10/18 --until.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"""" """"C:\Users\jackd\Documents\KL\yt_pilot_feed.py"""" >> """"C:\Users\jackd\Documents\KL-data\youtube-collect\yt_pilot_feed.stdout.log"""" 2>&1""", 0, True)
