@echo off
rem Relight scanner for the NON-election-mode waves (task "KL pvd-scan waves", every 15 min,
rem :00/:15/:30/:45 local, offset from "KL pvd-scan" at :07/:22/:37/:52 -- they share scan.lock).
rem Jack 2026-11-06: "also do 2 and 3" (2 = a relight task for the political and 2028 waves).
rem
rem Pass 0 restarts a wave dashboard that is down or hung (pvd_watch_keepalive.ps1; Jack
rem 2026-11-07: "Yes, add it"): bank-early in pass 4 reads the 8773 one.
rem Each pass adds relit / new programs of its wave by ROI (bar $1.10); listed markets
rem whose period is unchanged are left alone. Never places, cancels or removes.
rem   1. political wave 11/06 (22 series), default sizes 80 live / 120 dead
rem   2. 2028 election wave 11/06, 80 a side first...
rem   3. ...then 200 a side for what can't clear the bar at 80 (Jack: "Yes, list at 200")
rem   4. re-apply the 2028 wave's 3c room + 1-99c band (a relit entry is rebuilt from scratch;
rem      2-98c until Jack 2026-11-07: "expand range to 1-99c"),
rem      and BANK EARLY: 200 a side until the 8773 dashboard shows $1.30 banked this period,
rem      then 80 (Jack 2026-11-07: "200 a side until $1.30 is banked, then back down").
rem      300 a side where 200 won't bank $1.30 by 6 hours before the stop (Jack 2026-11-07:
rem      "want to bank $1.30 sooner. go up to 300 a side where 200 wont bank $1.30 by 6 hours
rem      before period end"), from the 8773 dashboard's /data rows.
rem      FOLLOW THE BOOK (Jack 2026-11-07: "Yes, follow the book"): cushion 0, and a market
rem      with no trade gets its limits re-anchored to the touch +-3c on every run.
rem      The 8773 dashboard (imm_pvd_watch, PVD_WATCH_DIR=run-logs\pvd-watch-elec2028) must
rem      be running: if its data is over 30 min old, sizes are left alone.
rem PVD_SCAN_BUDGET is the cap on ALL scan-added entries still live (both waves are ~$181k).
rem Remove: Unregister-ScheduledTask -TaskName 'KL pvd-scan waves' -Confirm:$false
set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages
set PY=C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe
set KL=C:\Users\jackd\Documents\KL
set LOG=%KL%\run-logs\pvd-scan\waves-task.log
set PVD_SCAN_BUDGET=300000
rem W28 = the 2028 election wave (11/06 23:01Z) + the 2028 election-structure launches of 11/07
rem 02:01Z (Jack: "several new elections events just launched at 9:01 ... bring the relevant ones in"),
rem and the 00:11Z / 01:00Z ones first missed (Jack: "why wasnt KXTPPRESPCT-28NOV07 quoted")
set W28=KXHOUSERACE,KXDPRESPRIMARY,KXRPRESPRIMARY,KXPRESSTATE,KXPRESDISTRICT,KXPOPVOTE,KXGOVRACE,KXCHINA1,KXDPRESPRIMWIN,KXGOPDELMAJ,KXRPRESPRIMWIN,KXDEMDELMAJ,KXRPRESWINCOUNT,KXPAMINWAGE,KXDPRESWINCOUNT,KXRPRESSWEEP,KXTRUMPFIRSTENDORSE,KXGOPFIRSTWINNOM,KXJEFFRIESFIRST,KXDEMFIRSTWINNOM,KXWIMEDICAIDEXP,KXDPRESSWEEP,KXTHIRDPARTYSTATEWIN,KXRPRESFRACTURED,KXDPRESFRACTURED,KXTPPRESPCT,KXTRUMPNOMNUM,KXWIRTWREPEAL,KXMINWAGEHOUSE,KXGAMEDICAIDEXP,KXKSMEDICAIDEXP,KXACASUBSIDYHOUSE,KXMEDICAIDWORKHOUSE,KXWIMARIJUANA,KXPUBLICOPTIONHOUSE,KXPAMARIJUANA
set POL=KXDCSTATEHOODHOUSE,KXGOPIMPEACHVOTES,KXGOVTSHUTLENGTH,KXHAGCHAIR,KXHEDUCATIONCHAIR,KXHJUDICIARYRM,KXHOUSEEXPANDLAW,KXITALYEXITTAX,KXJUDGECOUNTYEAR,KXNPVICSTATES,KXNRCCCHAIR,KXOVERSIGHTRM,KXPRSTATEHOUSE,KXROEHOUSE,KXSBANKINGCHAIR,KXSCOTUSEXPANDHOUSE,KXSCOTUSEXPANDLAW,KXSCOTUSTERMHOUSE,KXSHELPRM,KXSJUDICIARYRM,KXTARIFFREPEAL,KXTRUMPGOPCONVICT
cd /d %KL%
echo ===== %date% %time% pass 0: dashboard keepalive (8773 2028 wave, 8771 political wave) >> "%LOG%"
rem NOT ">> %LOG%": a dashboard started here would inherit cmd's handle on the log and hold
rem it open for life, and every later ">> %LOG%" redirect (so every pass) would then fail.
rem The keepalive appends its own lines to the log instead.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%KL%\pvd_watch_keepalive.ps1" > nul 2>&1
echo ===== %date% %time% pass 1: political wave 11/06 >> "%LOG%"
"%PY%" "%KL%\pvd_scan.py" --series %POL% --no-vote-check --new-since 2026-11-05T18:00:00Z --write >> "%LOG%" 2>&1
echo ===== %date% %time% pass 2: 2028 elections at 80 >> "%LOG%"
"%PY%" "%KL%\pvd_scan.py" --series %W28% --no-vote-check --new-since 2026-11-06T22:30:00Z --write >> "%LOG%" 2>&1
echo ===== %date% %time% pass 3: 2028 elections at 200 >> "%LOG%"
set PVD_SCAN_LIVE_SIZE=200
set PVD_SCAN_DEAD_SIZE=200
"%PY%" "%KL%\pvd_scan.py" --series %W28% --no-vote-check --new-since 2026-11-06T22:30:00Z --write >> "%LOG%" 2>&1
set PVD_SCAN_LIVE_SIZE=
set PVD_SCAN_DEAD_SIZE=
echo ===== %date% %time% pass 4: 2028 band, follow the book, bank early >> "%LOG%"
"%PY%" "%KL%\pvd_wave_tune.py" --series %W28% --since 2026-11-06T22:30:00Z --room 3 --price-min 1 --price-max 99 --flip-through --no-halts --follow-book --cushion 0 --reanchor 3 --series-tier KXDPRESPRIMARY:bid:2:45 --series-tier KXDPRESPRIMARY:bid:5:15 --series-tier KXDPRESPRIMARY:bid:15:11.25 --series-tier KXDPRESPRIMARY:bid:30:8.4375 --series-tier KXHOUSERACE:bid:2:45 --series-tier KXHOUSERACE:bid:5:22.5 --series-tier KXHOUSERACE:bid:15:16.875 --series-tier KXHOUSERACE:bid:30:10.125 --series-tier KXHOUSERACE:bid:50:5.0625 --series-tier KXHOUSERACE:bid:70:3.375 --series-tier KXHOUSERACE:ask:2:45 --series-tier KXHOUSERACE:ask:5:22.5 --series-tier KXHOUSERACE:ask:15:16.875 --series-tier KXHOUSERACE:ask:30:10.125 --series-tier KXHOUSERACE:ask:50:5.0625 --series-tier KXHOUSERACE:ask:70:3.375 --cheap-tiers 2:45,5:15,15:11.25,30:6.75,50:5.0625,70:2.25 --bank-size 200 --bank-max 300 --bank-by-hrs 6 --base-size 80 --no-drop --hold-max --bank-usd 1.30 --bank-url http://127.0.0.1:8773/data --bank-hist "%KL%\run-logs\pvd-watch-elec2028\history.json" --size-mult 1.5 --pin KXJEFFRIESFIRST-27FEB01-AL1:300:flat --pin-series KXTRUMPNOMNUM:300 --pin-series KXCHINA1:300 --pin-series KXACASUBSIDYHOUSE:300 --pin-series KXMINWAGEHOUSE:300 --pin-series KXPUBLICOPTIONHOUSE:300 --pin-series KXMEDICAIDWORKHOUSE:300 --pin-series KXWIRTWREPEAL:300 --pin-series KXWIMARIJUANA:300 --pin-series KXPAMARIJUANA:300 --side-cap ALL:90:1 --note "(relight task)" --write >> "%LOG%" 2>&1
rem 5 + 6. THE 200 FLOOR on the other waves (Jack 2026-11-07: "dont go lower than 200 on any market
rem right now until hitting $1.30 threshold"): 200 a side until the wave's dashboard shows $1.30
rem banked this period, then back to the entry's own size (pre_bank). Nothing else changes there.
echo ===== %date% %time% pass 5: political wave 200 floor >> "%LOG%"
"%PY%" "%KL%\pvd_wave_tune.py" --series %POL% --since 2026-11-05T18:00:00Z --bank-size 200 --no-drop --bank-usd 1.30 --bank-url http://127.0.0.1:8771/data --side-cap ALL:90:1 --note "(relight task)" --write >> "%LOG%" 2>&1
echo ===== %date% %time% pass 6: 11/04 election wave 200 floor >> "%LOG%"
"%PY%" "%KL%\pvd_wave_tune.py" --series ALL --exclude-series %W28%,%POL% --since 2000-01-01T00:00:00Z --bank-size 200 --no-drop --bank-usd 1.30 --bank-url http://127.0.0.1:8774/data --side-cap ALL:90:1 --note "(relight task)" --write >> "%LOG%" 2>&1
