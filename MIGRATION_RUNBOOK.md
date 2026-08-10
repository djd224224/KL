# MIGRATION_RUNBOOK — moving the KL bot fleet to a dedicated laptop

*Written 2026-08-09. Target: a spare laptop that stays on AC with the lid open,
replacing the daily-driver laptop whose Modern Standby freezes cost ~8.5h of
quoting on 8/9 alone. Helper scripts: `migrate_export.ps1` (old machine),
`migrate_import_tasks.ps1` (new machine).*

**The one rule that matters: the two machines must NEVER both be live.**
Singleton locks are per-machine files — laptop + laptop is the 8/9
duplicate-generation incident with zero protection. Every step below is
ordered around that.

---

## Phase 0 — decisions (5 min)

- **Create the Windows user `jackd` on the new laptop.** Every launcher, task,
  and config hardcodes `C:\Users\jackd\...`. Same username = zero path edits.
  Different username = grep-and-fix ~15 files; don't.
- Scope: everything in the 34 `KL *` tasks EXCEPT `KL gas snipe` (retired 8/9,
  stays Disabled). `KL sync-kl-main` and `KL kalshi-daily-recap` come last —
  they need git push access and Claude CLI auth respectively.

## Phase 1 — prep the new laptop as a bot host (30 min)

All in an **elevated** PowerShell on the NEW laptop:

```powershell
# never sleep, never hibernate (AC and DC)
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
# lid close = do nothing (AC and DC — it's not a portable anymore)
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
# kill Modern Standby entirely (the thing no setting could fix on the old
# laptop — safe on a dedicated box; takes effect after reboot)
reg add "HKLM\System\CurrentControlSet\Control\Power" /v PlatformAoAcOverride /t REG_DWORD /d 0 /f
# Windows Update must not auto-reboot mid-week
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f
```

- **Auto-logon** (at-logon tasks must fire after any reboot/patch): run
  `netplwiz`, untick "Users must enter a user name and password", enter the
  jackd credentials. (Lock the screen after logon if you want it visually
  secure — tasks keep running under a locked session.)
- Reboot once so PlatformAoAcOverride takes effect; verify with
  `powercfg /a` → "Standby (S0 Low Power Idle)" should be gone.

## Phase 2 — toolchain (30 min)

1. **Python 3.12** — per-user install to the DEFAULT location
   (`C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe`) —
   launchers hardcode that path. Then:
   ```powershell
   & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m pip install requests pytz cryptography numpy pandas scipy
   ```
   (Task Scheduler sessions can't see the Roaming user-site — install into the
   interpreter's own site-packages exactly like this, not `pip --user`.)
2. **Git** — plus push credentials for github.com/djd224224/KL (Git Credential
   Manager sign-in on first push). Needed by `KL sync-kl-main`.
3. **Claude Code** — only for `KL kalshi-daily-recap`; install + `claude` login.
   Defer if you want the trading fleet up first.
4. **gcloud/bq CLIs** — for the BQ-touching jobs (recap, retention guard):
   `gcloud auth login` + `gcloud auth application-default login`, and set the
   user-level `CLOUDSDK_PYTHON` env var (see the export bundle's env list).

## Phase 3 — repo, secrets, and proof-of-life (30 min)

1. `git clone https://github.com/djd224224/KL.git C:\Users\jackd\Documents\KL`
2. On the OLD machine run
   `powershell -ExecutionPolicy Bypass -File migrate_export.ps1` → produces
   `migration_export\` (task XMLs + HKCU env values + the Kalshi PEM). Copy
   that folder to the new laptop (USB — it contains live credentials; don't
   email it, and delete both copies when done).
3. On the NEW machine, from the bundle:
   - PEM → `C:\Users\jackd\Downloads\Lisa_Kalshi.txt` (hardcoded fallback path)
   - Set user-level env vars from `env_values.txt`:
     ```powershell
     [Environment]::SetEnvironmentVariable("ALERT_EMAIL_FROM","<value>","User")
     [Environment]::SetEnvironmentVariable("ALERT_EMAIL_PASSWORD","<value>","User")
     [Environment]::SetEnvironmentVariable("CLOUDSDK_PYTHON","<value>","User")
     ```
4. **Prove the stack** (all read-only / dry-run, safe while the old fleet is
   still live — these place no orders):
   ```powershell
   cd C:\Users\jackd\Documents\KL
   & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m unittest test_crypto_touch_mm test_crypto_touch_mm_weekly test_crypto_updown_mm
   & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" crypto_touch_mm.py --market SOL-MAX --once
   & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" crypto_updown_mm.py --asset BTC --cadence weekly --once
   & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" send_daily_digest.py --test
   ```
   Expect: tests green, dry-run banners, a test digest email arriving.

## Phase 4 — import the tasks, DISABLED (15 min)

On the NEW machine, elevated:
`powershell -ExecutionPolicy Bypass -File migrate_import_tasks.ps1 -BundleDir <path>`.
It imports every exported task XML **in the Disabled state** — nothing can
start trading yet. Verify: `Get-ScheduledTask -TaskName "KL *"` shows ~34
tasks, all Disabled.

## Phase 5 — CUTOVER (the dangerous 20 minutes)

Do this in one sitting, in exactly this order:

1. **OLD machine — stop the world:**
   ```powershell
   Get-ScheduledTask -TaskName "KL *" | Disable-ScheduledTask
   ```
   Then kill every bot chain (the `cleanup_duplicate_bots.ps1` pattern: all
   bot pythons + their cmd/powershell/wscript ancestors) and **verify zero**:
   `@(Get-Process python -ErrorAction SilentlyContinue).Count` → 0.
2. **OLD machine — clear the book** (or wait one TTL: 10 min crypto, 30 IMM):
   ```powershell
   $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
   foreach ($m in (& $py -c "import crypto_touch_mm as m; print(' '.join(sorted(m.MARKETS)))").Split(' ')) { & $py crypto_touch_mm.py --market $m --cancel-all }
   foreach ($a in "BTC","ETH","SOL","XRP","DOGE","BNB","HYPE") { & $py crypto_updown_mm.py --asset $a --cancel-all }
   ```
   (IMM cancels its own orders on clean shutdown; after a hard kill its
   orders TTL out in ≤30 min — fine either way.)
3. **State copy — only NOW, after IMM is dead** (stale-state copies are how
   P&L carry and halt state get corrupted). On the OLD machine:
   `powershell -ExecutionPolicy Bypass -File migrate_export.ps1 -StateOnly` →
   copy the produced `state\` folder to
   the same paths on the new laptop (`run-logs\incentive-mm\`, `gas_data\`).
   Crypto bots are stateless (status/cache files regenerate).
4. **NEW machine — go live:**
   ```powershell
   Get-ScheduledTask -TaskName "KL *" | Where-Object TaskName -ne "KL gas snipe" | Enable-ScheduledTask
   Restart-Computer   # auto-logon fires every at-logon task cleanly
   ```
5. **Verify** (~10 min after reboot): ~24 pythons; every
   `run-logs\crypto-touch\status_*.json`, `crypto-updown\status_*.json`, and
   `incentive-mm\status_incentive_mm.json` fresh with LIVE banners; resting
   orders visible on Kalshi; next morning both digests arrive.

## Phase 6 — decommission the old laptop (permanent)

The old laptop's tasks are Disabled but still registered — a `Start-ScheduledTask`
or any re-enable is a live duplicate fleet. Remove the risk outright:

```powershell
Get-ScheduledTask -TaskName "KL *" | Unregister-ScheduledTask -Confirm:$false
```

Keep the repo checkout there if you like (it's just files); delete
`migration_export\` from both machines and the USB stick.

## Rollback

Reverse of Phase 5: disable+kill on the new machine, cancel-alls, copy the IMM
state back, re-enable on the old (re-import the task XMLs if you already
unregistered). The state bundle is the only thing with direction — everything
else is in git.

## Known per-task follow-ups after cutover

| Task | Needs on the new box |
|---|---|
| `KL kalshi-daily-recap` | Claude CLI installed + authed (PT20M timeout gotcha stands) |
| `KL sync-kl-main` | git push credentials cached |
| `KL imm quote-gaps` | nothing extra — but its launcher-env mirror gotcha means verify the first morning email's numbers |
| `KL gas snipe` | stays Disabled (retired 8/9); `gas_data\HALT` travels in the state copy as a second lock |
| BQ jobs | `gcloud auth` done in Phase 2 |
