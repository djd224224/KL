@echo off
rem Relight scanner for the NON-election-mode waves (task "KL pvd-scan waves", every 15 min,
rem :00/:15/:30/:45 local, offset from "KL pvd-scan" at :07/:22/:37/:52 -- they share scan.lock).
rem Jack 2026-11-06: "also do 2 and 3" (2 = a relight task for the political and 2028 waves).
rem
rem Each pass adds relit / new programs of its wave by ROI (bar $1.10); listed markets
rem whose period is unchanged are left alone. Never places, cancels or removes.
rem   1. political wave 11/06 (22 series), default sizes 80 live / 120 dead
rem   2. 2028 election wave 11/06, 80 a side first...
rem   3. ...then 200 a side for what can't clear the bar at 80 (Jack: "Yes, list at 200")
rem   4. re-apply the 2028 wave's 3c room + 2-98c band (a relit entry is rebuilt from scratch),
rem      and BANK EARLY: 200 a side until the 8773 dashboard shows $1.30 banked this period,
rem      then 80 (Jack 2026-11-07: "200 a side until $1.30 is banked, then back down").
rem      The 8773 dashboard (imm_pvd_watch, PVD_WATCH_DIR=run-logs\pvd-watch-elec2028) must
rem      be running: if its history.json is over 30 min old, sizes are left alone.
rem PVD_SCAN_BUDGET is the cap on ALL scan-added entries still live (both waves are ~$181k).
rem Remove: Unregister-ScheduledTask -TaskName 'KL pvd-scan waves' -Confirm:$false
set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages
set PY=C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe
set KL=C:\Users\jackd\Documents\KL
set LOG=%KL%\run-logs\pvd-scan\waves-task.log
set PVD_SCAN_BUDGET=300000
set W28=KXHOUSERACE,KXDPRESPRIMARY,KXRPRESPRIMARY,KXPRESSTATE,KXPRESDISTRICT,KXPOPVOTE,KXGOVRACE,KXCHINA1
cd /d %KL%
echo ===== %date% %time% pass 1: political wave 11/06 >> "%LOG%"
"%PY%" "%KL%\pvd_scan.py" --series KXDCSTATEHOODHOUSE,KXGOPIMPEACHVOTES,KXGOVTSHUTLENGTH,KXHAGCHAIR,KXHEDUCATIONCHAIR,KXHJUDICIARYRM,KXHOUSEEXPANDLAW,KXITALYEXITTAX,KXJUDGECOUNTYEAR,KXNPVICSTATES,KXNRCCCHAIR,KXOVERSIGHTRM,KXPRSTATEHOUSE,KXROEHOUSE,KXSBANKINGCHAIR,KXSCOTUSEXPANDHOUSE,KXSCOTUSEXPANDLAW,KXSCOTUSTERMHOUSE,KXSHELPRM,KXSJUDICIARYRM,KXTARIFFREPEAL,KXTRUMPGOPCONVICT --no-vote-check --new-since 2026-11-05T18:00:00Z --write >> "%LOG%" 2>&1
echo ===== %date% %time% pass 2: 2028 elections at 80 >> "%LOG%"
"%PY%" "%KL%\pvd_scan.py" --series %W28% --no-vote-check --new-since 2026-11-06T22:30:00Z --write >> "%LOG%" 2>&1
echo ===== %date% %time% pass 3: 2028 elections at 200 >> "%LOG%"
set PVD_SCAN_LIVE_SIZE=200
set PVD_SCAN_DEAD_SIZE=200
"%PY%" "%KL%\pvd_scan.py" --series %W28% --no-vote-check --new-since 2026-11-06T22:30:00Z --write >> "%LOG%" 2>&1
set PVD_SCAN_LIVE_SIZE=
set PVD_SCAN_DEAD_SIZE=
echo ===== %date% %time% pass 4: 2028 room + band >> "%LOG%"
"%PY%" "%KL%\pvd_wave_tune.py" --series %W28% --since 2026-11-06T22:30:00Z --room 3 --price-min 2 --price-max 98 --bank-size 200 --base-size 80 --bank-usd 1.30 --bank-hist "%KL%\run-logs\pvd-watch-elec2028\history.json" --note "(relight task)" --write >> "%LOG%" 2>&1
