@echo off
:: One-click removal of the 72h time limit from the crypto annual/updown tasks.
:: Pops a UAC prompt, then runs fix_crypto_task_time_limit.ps1 elevated.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','C:\Users\jackd\Documents\KL\fix_crypto_task_time_limit.ps1'"
