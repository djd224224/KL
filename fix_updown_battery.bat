@echo off
:: One-click battery-settings fix for the crypto_updown_mm tasks.
:: Pops a UAC prompt, then runs fix_updown_battery.ps1 elevated.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','C:\Users\jackd\Documents\KL\fix_updown_battery.ps1'"
