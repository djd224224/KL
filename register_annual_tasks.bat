@echo off
rem Registers + starts the 8 "KL crypto_annual_mm *" scheduled tasks.
rem Self-elevates, then runs register_annual_tasks.ps1.
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\jackd\Documents\KL\register_annual_tasks.ps1"
pause
