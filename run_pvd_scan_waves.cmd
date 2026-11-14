@echo off
rem Relight scanner for the NON-election-mode waves (task "KL pvd-scan waves", every 15 min,
rem :00/:15/:30/:45 local, offset from "KL pvd-scan" at :07/:22/:37/:52 -- they share scan.lock).
rem Jack 2026-11-06: "also do 2 and 3" (2 = a relight task for the political and 2028 waves).
rem
rem Pass 0 restarts a wave dashboard that is down or hung (pvd_watch_keepalive.ps1; Jack
rem 2026-11-07: "Yes, add it"): bank-early in pass 4 reads the 8773 one.
rem Each pass adds relit / new programs of its wave by ROI (bar $1.10); listed markets
rem whose period is unchanged are left alone. Never places, cancels or removes.
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
rem KXDPRESPRIMARY, KXRPRESPRIMARY, KXPRESSTATE and KXJEFFRIESFIRST left W28 on 2026-11-11 for
rem registry wave w1111-elec2028 (pass 7, default preset at 200 a side, halts on; Jack: "Yes, at
rem 200 a side", then "start /market-wave skill for all 724"): their 11/11 relight sat dark under
rem UNWIND_ONLY_WAVES. Pass 6 skips them via --exclude-registry. KXTRUMPNOMNUM stays here (off, 11/08).
set W28=KXHOUSERACE,KXPRESDISTRICT,KXPOPVOTE,KXGOVRACE,KXCHINA1,KXDPRESPRIMWIN,KXGOPDELMAJ,KXRPRESPRIMWIN,KXDEMDELMAJ,KXRPRESWINCOUNT,KXPAMINWAGE,KXDPRESWINCOUNT,KXRPRESSWEEP,KXTRUMPFIRSTENDORSE,KXGOPFIRSTWINNOM,KXDEMFIRSTWINNOM,KXWIMEDICAIDEXP,KXDPRESSWEEP,KXTHIRDPARTYSTATEWIN,KXRPRESFRACTURED,KXDPRESFRACTURED,KXTPPRESPCT,KXTRUMPNOMNUM,KXWIRTWREPEAL,KXMINWAGEHOUSE,KXGAMEDICAIDEXP,KXKSMEDICAIDEXP,KXACASUBSIDYHOUSE,KXMEDICAIDWORKHOUSE,KXWIMARIJUANA,KXPUBLICOPTIONHOUSE,KXPAMARIJUANA
set POL=KXDCSTATEHOODHOUSE,KXGOPIMPEACHVOTES,KXGOVTSHUTLENGTH,KXHAGCHAIR,KXHEDUCATIONCHAIR,KXHJUDICIARYRM,KXHOUSEEXPANDLAW,KXITALYEXITTAX,KXJUDGECOUNTYEAR,KXNPVICSTATES,KXNRCCCHAIR,KXOVERSIGHTRM,KXPRSTATEHOUSE,KXROEHOUSE,KXSBANKINGCHAIR,KXSCOTUSEXPANDHOUSE,KXSCOTUSEXPANDLAW,KXSCOTUSTERMHOUSE,KXSHELPRM,KXSJUDICIARYRM,KXTARIFFREPEAL,KXTRUMPGOPCONVICT
cd /d %KL%
echo ===== %date% %time% pass 0: dashboard keepalive (8773 2028 wave, 8771 political wave) >> "%LOG%"
rem NOT ">> %LOG%": a dashboard started here would inherit cmd's handle on the log and hold
rem it open for life, and every later ">> %LOG%" redirect (so every pass) would then fail.
rem The keepalive appends its own lines to the log instead.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%KL%\pvd_watch_keepalive.ps1" > nul 2>&1
rem 1-5. RETIRED 2026-11-11 (Jack: "if the 2028 previously quoted markets get relit, they should be
rem quoted. if new 2028 markets come online, they should be quoted. only the markets with positions
rem should be on unwind mode"; then "yes build all 3. make no mistakes, this is serious"). The 11/11
rem relight listed 724 2028 markets that rested nothing: these passes wrote entries without
rem wave_added, so the 11/08 UNWIND_ONLY_WAVES lock kept them dark, and pass 4 still carried the
rem 11/07 settings (x1.5, cheap sides to x135, --no-halts). Their series are now registry waves
rem (pass 7, default preset, stamped): W28 -> w1111-w28 (the 300 pins kept, KXTRUMPNOMNUM paused)
rem and w1111-elec2028; POL -> w1111-pol. W28 / POL stay defined for pass 6's exclusion and the
rem pick-off watch's labels. The old pass lines: git history, or the session backup.
rem 6. THE 200 FLOOR on the 11/04 election wave and every other listing outside the registry
rem (Jack 2026-11-07), and since 2026-11-11 it STAMPS them (--wave-added): flat ones quote both
rem sides and held ones unwind (the bot's held-markets-only rule, 470df6c); unstamped, the old-wave
rem lock kept them dark.
echo ===== %date% %time% pass 6: 11/04 election wave 200 floor >> "%LOG%"
"%PY%" "%KL%\pvd_wave_tune.py" --series ALL --exclude-series %W28%,%POL% --exclude-registry --since 2000-01-01T00:00:00Z --bank-size 200 --no-drop --bank-usd 1.30 --bank-url http://127.0.0.1:8774/data --side-cap ALL:90:0 --twin-block 200 --wave-added 2026-11-11T13:45:00Z --note "(relight task)" --write >> "%LOG%" 2>&1
rem 7. REGISTRY WAVES (pvd_waves.py, the market-wave playbook): each enabled wave in
rem run-logs\pvd-scan\waves.json gets its scan (relit / new programs) and its tune pass.
rem Adding a wave = "pvd_waves.py add ..."; this file never needs editing for one.
echo ===== %date% %time% pass 7: registry waves >> "%LOG%"
"%PY%" "%KL%\pvd_waves.py" run --write >> "%LOG%" 2>&1
rem 8. PICK-OFF WATCH (Jack 2026-11-08: "the pickoffs yesterday were unacceptable"; the
rem 11/07 House-night baits were found only when Jack asked for a dashboard refresh).
rem Every fill that opened or added to a position on a wave market in the last 6 h, marked
rem to the book with OUR orders taken out, flagged at 5c+ and $25+ against us; 3+ flagged
rem markets in one series = a series trip. --act pins a flagged REGISTRY-wave market (or a
rem tripped series) at the wave's base size and re-tunes it now (legacy waves: reported
rem for a hand edit, Jack's word). Exit 3 = something new -> wave_alert.py texts at $100+
rem new, a series trip or a stand-down, and emails. State: run-logs\pvd-scan\pickoff_state.json.
echo ===== %date% %time% pass 8: pick-off watch >> "%LOG%"
"%PY%" "%KL%\pvd_waves.py" pickoffs all --hours 6 --act --update-state --json "%KL%\run-logs\pvd-scan\pickoffs_last.json" >> "%LOG%" 2>&1
if errorlevel 3 "%PY%" "%KL%\wave_alert.py" pickoff --pickoffs "%KL%\run-logs\pvd-scan\pickoffs_last.json" --send >> "%LOG%" 2>&1
rem 9. DARK CHECK (Jack 2026-11-11: "ELI5 why market-wave didnt autoquote last night, and what needs to
rem be fixed so that never happens again"): every listed wave market 20+ min into its listing is
rem compared with what rests on Kalshi; a dark one gets its reason. --act fixes the old-wave-lock
rem case (re-tunes its registry wave, or adopts the series into a default-preset registry wave
rem after the auto preflight). Exit 3 = new dark -> wave_alert.py texts and emails.
echo ===== %date% %time% pass 9: dark check (listed vs resting on Kalshi) >> "%LOG%"
"%PY%" "%KL%\pvd_waves.py" darkcheck --act --update-state --json "%KL%\run-logs\pvd-scan\darkcheck_last.json" >> "%LOG%" 2>&1
if errorlevel 3 "%PY%" "%KL%\wave_alert.py" dark --dark "%KL%\run-logs\pvd-scan\darkcheck_last.json" --send >> "%LOG%" 2>&1
rem 10. BAIT WATCH (Jack 2026-11-11: "Be extremely careful for baiters, notify me when something
rem suspicious occurs", then "Just cover market wave"): what the bot's own bait defenses logged on
rem market-wave markets since the last run -- a bait-fingerprint stop or a 3rd sweep in 30 min (TEXT),
rem a toxic-side halt, a 2nd sweep, or a price flashed and pulled 3+ times in 60 min (email). Wave
rem fills against us stay pass 8's. Exit 3 = something new -> wave_alert.py bait.
echo ===== %date% %time% pass 10: bait watch (wave markets) >> "%LOG%"
"%PY%" "%KL%\bait_watch.py" --update-state --json "%KL%\run-logs\pvd-scan\bait_watch_last.json" >> "%LOG%" 2>&1
if errorlevel 3 "%PY%" "%KL%\wave_alert.py" bait --bait "%KL%\run-logs\pvd-scan\bait_watch_last.json" --send >> "%LOG%" 2>&1
