' Global top-video YouTube API feed with NO console flash ("KL yt-topvid-feed",
' every 20 min and at logon; register_yt_topvid_feed.ps1). One pass of
' yt_topvid_feed.py, then exit. The script keeps its own log
' (KL-data\youtube-collect\yt_topvid_feed.log); stdout/stderr -- a traceback --
' land in yt_topvid_feed.stdout.log beside it. The exit code is passed through
' so the task's Last Result shows a failed pass (1 partial, 2 no key).
' Added 2026-10-14: the #2 candidates' views must outlive yt_collect.py's 10/18 --until.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"""" """"C:\Users\jackd\Documents\KL\yt_topvid_feed.py"""" >> """"C:\Users\jackd\Documents\KL-data\youtube-collect\yt_topvid_feed.stdout.log"""" 2>&1""", 0, True)
