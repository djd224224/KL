@echo off
rem Switches the live "KL crypto_updown_mm *" fleet to cadence daily,weekly.
rem Self-elevates, then runs enable_updown_daily.ps1 (stop -> kill chains ->
rem re-register -> start, per the handoff restart recipe).
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\jackd\Documents\KL\enable_updown_daily.ps1"
pause
