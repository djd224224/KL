@echo off
:: One-click go-live for the crypto_updown_mm weekly fleet.
:: Pops a UAC prompt, then runs register_updown_tasks.ps1 elevated.
:: The window stays open so you can see the 7 tasks report Running.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','C:\Users\jackd\Documents\KL\register_updown_tasks.ps1'"
