# KL — operating notes for Claude sessions

Kalshi trading bots (KXHIGH/KXLOW weather, IMM incentive market-maker, crypto
one-touch/up-down, gas, rain) plus their analysis and dashboard tooling. This
file exists so a session knows **where it is running and what it can reach**
before it starts work. Public repo: never commit keys, exports, or balances.

## Where things run

| Surface | Kalshi API | BigQuery | Local files (archives, run-logs) |
|---|---|---|---|
| Laptop (Windows, `C:\Users\jackd\Documents\KL`, Task Scheduler) | yes — `Lisa_Kalshi.txt` | yes — `gcloud auth application-default login` | yes |
| GitHub Actions (`.github/workflows/*`) | yes — `secrets.KALSHI_PRIVATE_KEY` | yes — `secrets.GCP_SERVICE_ACCOUNT_KEY` (writes tables) | no |
| Cloud session (claude.ai/code, fresh Ubuntu VM per session) | **only if** the cloud environment's network level is Custom/Full with `api.elections.kalshi.com` allowed AND `KALSHI_PRIVATE_KEY` is set on the environment | **only if** `GCP_SA_KEY` is set on the environment (`.claude/hooks/session-start.sh` materializes it) | no — gitignored files never reach the clone |

`.claude/hooks/session-start.sh` runs on cloud sessions only and prints one
`[session-start] cloud parity: ...` line: whether Python deps are present,
whether BigQuery creds were found, whether a Kalshi key is set, and whether the
Kalshi host is reachable. Read it before promising live-data work.

- Under the default **Trusted** network level, `*.googleapis.com` is reachable
  but `api.elections.kalshi.com` is denied at the egress proxy (403 on CONNECT).
- `CLOUDSDK_AUTH_ACCESS_TOKEN` in a cloud VM is a harness placeholder, not a
  usable Google credential.
- No Kalshi/BigQuery access in the cloud? Use the established fallback: a
  read-only probe script + workflow (pattern: `lowtemp_status.py`,
  `kxtruev_diag.py`, `imm_deploy_probe.py`) triggered by pushing its `.trigger`
  file, then read the job log with the GitHub tools. Print aggregates only —
  Action logs are public.

## Credentials

- Local: `Lisa_Kalshi.txt` (PEM) in the repo root; gcloud ADC.
- All scripts also accept `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY` (base64 PEM
  or raw PEM) and `KALSHI_PRIVATE_KEY_PATH`; BigQuery clients honor
  `GOOGLE_APPLICATION_CREDENTIALS`.
- Cloud environment variables are visible to anyone using the environment.
  Prefer a read-only BigQuery service account for `GCP_SA_KEY`; putting the
  live Kalshi trading key on the environment is a deliberate risk decision.

## Data sources of record

- BigQuery `elite-contact-446323-q7.Kalshi`, prefix `KXHIGH_` (`orders`,
  `fills`, `settlements`, `market_snapshot`, `runs`, `alerts`, `cli_readings`,
  `historical_forecasts`; views in `analysis/kxhigh/sql/`). `KXLOW_` mirrors.
  Kalshi portfolio endpoints only serve ~65 days; BigQuery is the long history.
- `Kalshi-*-archive.csv` (append-only merges of API pulls) exist only on the
  laptop. Cloud sessions analyze from BigQuery instead.
- `settlements.pnl` treats contracts sold before settlement as total losses;
  for realized P&L use the fills cash-flow method in
  `analysis/kxhigh/sql/90_ab_hi_no_report.sql`, attributed by `client_order_id`.

## Conventions

- Bots run from the laptop's Task Scheduler (`run_*.ps1`) or GitHub Actions
  (`run_high_temp_trading.yml` via external cron). Cloud sessions never run
  trading loops; they analyze, edit, and push.
- Handoff docs (`*_HANDOFF.md`, `SESSION_HANDOFF_*.md`, `KXHIGH_PROFITABILITY_REVIEW.md`)
  are the memory across sessions; update them rather than relying on chat.
- Tests: `python -m unittest test_incentive_mm` (large), `test_crypto_*`,
  `test_rain_monthly`, `test_imm_health_alert`, `test_imm_reward_recon`.
