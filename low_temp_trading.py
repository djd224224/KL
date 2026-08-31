# -*- coding: utf-8 -*-
"""low_temp_trading.py — KXLOW (daily minimum temperature) trading bot.

Adapted from high_temp_trading.py for Kalshi's KXLOW<CITY> series
(e.g. KXLOWTSATX-26JUL20, "Lowest temperature in San Antonio on Jul 20").
Settlement: NWS Climatological Report (Daily) minimum temperature —
midnight-to-midnight LOCAL calendar day, same CLI product the KXHIGH
markets settle on.

Key differences vs the high-temp bot (deliberate, do not "fix" back):

1. FORECAST FIELDS. WU's `temperatureMin[i]` is the traditional
   "overnight low" for the NIGHT period (evening i → morning i+1) — it
   crosses the midnight boundary and does NOT match CLI settlement.
   This bot uses `calendarDayTemperatureMin` (midnight-to-midnight),
   index-matched via `validTimeLocal` dates. The NWS number is the MIN
   over the target local calendar day of the HOURLY forecast (the daily
   "Tonight" period low has the same two-calendar-day problem).

2. OUTCOME TIMING IS INVERTED. A daily high prints mid-afternoon; a
   daily low prints near sunrise (~05-07 local), with the day's obs
   starting to reveal it from local midnight. At midnight the temp is
   typically only ~3-6°F above the eventual low (vs ~15°F below the
   eventual high), so resting quotes after local midnight are far more
   toxic than for highs. Therefore:
     - evening runs (trading tomorrow) expire at LOCAL 23:59 of the
       night before the target day (i.e., 1 minute before the target
       day's obs begin);
     - day-of runs (00:00–07:59 CT) expire at LOCAL 02:59 of the target
       day (~3h before a typical sunrise min);
     - expiries are NEVER rolled forward a day; if a city's expiry is
       already in the past, the city is skipped.

3. MIRRORED RISK FILTERS.
     - late-day cold-drop filter (mirror of the high bot's midnight-delta
       filter): if the target day's 18-23h local forecast comes within
       4.5°F of the 00-12h forecast min, the low is not "morning-set"
       (a front can reset it at 23:59) → skip city.
     - midnight-print filter: if temp@00:00 local is within 1.5°F of the
       daily min, the low likely prints AT midnight (temps rising all
       night) → outcome known exactly when the day starts → skip city.
     - min-hour filter (day-of runs): expected min before ~04 local →
       outcome prints before/at our quote stop → skip city.
     - per-bucket obs filter (day-of runs): current obs ≤ bucket top →
       bucket active or still reachable downward → skip bucket
       (mirror of the high bot's obs ≥ bucket bottom).
     - forecast-busted (day-of runs): obs already ≤ forecast min − 2 →
       skip city.

4. NO TAIL MARKETS in v1. The high bot prices its warm tail from an
   empirical actual-vs-forecast error distribution built on months of
   high-temp data. No such distribution exists for lows yet, and the
   dangerous tail here is the COLD one (radiational-cooling busts under
   clear/calm skies are the classic fat tail for minima). Both T markets
   are skipped until a low-temp error distribution is built
   (TRADE_TAILS env flag exists but defaults false).

5. WIDER SIGMA FLOORS. Vendor Tmin error runs a few tenths °F wider
   than Tmax error (radiational nights, cold pools, late-day fronts),
   so city floors are the high bot's floors +0.3°F with a 1.5 default.

6. STRIKE PARSING from strike_type/floor_strike/cap_strike (the API
   provides them; KXLOW events have TWO tail markets so the high bot's
   positional B→T trick would mis-parse if ordering ever changes).

7. BIAS CORRECTION reads KXLOW_market_snapshot (earliest run per
   city/day) joined to KXHIGH_cli_readings.low_temp_f. Off until
   snapshot history accumulates — returns no rows gracefully; a missing
   table (first runs) logs quietly instead of alerting.

BQ tables: KXLOW_market_snapshot / KXLOW_orders / KXLOW_runs /
KXLOW_alerts in the same dataset as the KXHIGH tables.

Env knobs: LOW_DRY_RUN (default false) — full run but no order
cancels/creates and no BQ writes; LOW_STARTING_CONTRACTS (default 2 →
2/rung day-of, 4/rung evening via the x2 night multiplier);
LOW_MAX_CONTRACTS (default 50); TRADE_TAILS (default false);
BIAS_CORRECTION_ENABLED and the BIAS_* knobs (same as the high bot).
"""

from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient
import pytz
import time
import json
import uuid
import re
import sys
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

import os
import hashlib
import base64

# Windows consoles/pipes default to cp1252, which can't encode the ✓/🔽/⚡
# glyphs in the log output (the high bot only ever runs on GH Actions'
# UTF-8 runners, so it never hit this). Reconfigure instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Kill switch (Jack, 2026-08-31): while low_temp.paused exists next to this
# script, exit before ANY setup — no cancels, no orders, no BQ writes. The
# launcher checks it too; this copy also covers the GH workflow_dispatch
# backup. Delete the file from main to resume.
_PAUSE_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "low_temp.paused")
if os.path.exists(_PAUSE_FLAG):
    print(f"PAUSED — {_PAUSE_FLAG} present; exiting without trading.")
    sys.exit(0)

DRY_RUN = os.environ.get("LOW_DRY_RUN", "false").lower() == "true"
# Model-vs-market divergence gate (2026-08-12 settlement study, n=54 traded):
# when fair_NO exceeded the market's NO mid by >12c, NO won only 28-40% —
# the market was right and the model wrong (winner's curse). Retro: gating
# fair-mid>12c kept 19/54 trades for +$21.89 vs -$27.89 ungated. 0 disables.
MAX_DIVERGENCE_CENTS = float(os.environ.get("LOW_MAX_DIVERGENCE_CENTS", "12"))
# Forecast-std multiplier, default OFF (1.0). The 960-market calibration
# study found global widening improves unconditional log-loss (k~2-3) but
# WORSENS the traded-set calibration: wider dist => lower P(band) => HIGHER
# fair_NO on exactly the bets the bot loses, and every trade then trips the
# divergence gate. Knob exists for experiments; do not enable without a new
# study. (True error shape is a mixture — near-exact forecasts plus a fat
# miss cluster — which no single scale factor fits.)
STD_MULT = float(os.environ.get("LOW_STD_MULT", "1.0"))


def load_private_key(b64_key="", file_path=""):
    """Load Kalshi RSA key from file or env var (same as high-temp bot)."""
    if file_path and os.path.exists(file_path):
        with open(file_path, "rb") as f: pem = f.read()
    elif b64_key:
        try: pem = base64.b64decode(b64_key)
        except Exception: pem = b64_key.encode()
    else:
        raise FileNotFoundError(f"No private key. Set KALSHI_PRIVATE_KEY or place at '{file_path}'.")
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())

prod_key_id = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
prod_private_key = load_private_key(
    b64_key=os.environ.get("KALSHI_PRIVATE_KEY", ""),
    file_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
)

# Same per-process HTTP keep-alive opt-in as incentive_mm (client commit
# 1037903): one pooled TLS connection (~30ms/call warm) instead of a fresh
# ~1s handshake on every call. Retries on the pooled session are
# CONNECT-ONLY, so a retry can never double-place an order. This run makes
# hundreds of GETs (orderbooks, per-rung cap checks), so it shortens the
# cancel-sweep → re-quote gap materially. setdefault: an explicit
# KALSHI_HTTP_KEEPALIVE=0 in the environment still wins. Must be set
# BEFORE ExchangeClient is constructed (the session is built in __init__).
os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")

prod_api_base = "https://api.elections.kalshi.com/trade-api/v2"
exchange_client = ExchangeClient(exchange_api_base=prod_api_base, key_id=prod_key_id, private_key=prod_private_key)
print(exchange_client.get_exchange_status())
if DRY_RUN:
    print("\n*** LOW_DRY_RUN=true — no orders will be cancelled/placed, no BQ writes ***\n")

###### BIGQUERY SETUP

from google.cloud import bigquery
import smtplib
from email.mime.text import MIMEText

# =========================
# ALERTING
# =========================
_ALERTS = []
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")


def alert(category: str, message: str, context: dict = None):
    entry = {
        "timestamp": datetime.now(pytz.timezone('US/Central')).strftime('%Y-%m-%d %H:%M:%S'),
        "category": category,
        "message": message,
        **(context or {}),
    }
    _ALERTS.append(entry)
    ctx_str = f" | {context}" if context else ""
    print(f"  ⚡ ALERT [{category}]: {message}{ctx_str}")


def send_alert_notification():
    if not ALERT_EMAIL_FROM or not ALERT_EMAIL_PASSWORD or not ALERT_EMAIL_TO:
        return
    if len(_ALERTS) == 0:
        return
    cats = {}
    for a in _ALERTS:
        c = a["category"]
        cats[c] = cats.get(c, 0) + 1
    lines = [f"KXLOW Alerts: {len(_ALERTS)} total"]
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count}")
    lines.append("")
    for a in _ALERTS[:8]:
        lines.append(f"[{a['category']}] {a['message']}")
    if len(_ALERTS) > 8:
        lines.append(f"... +{len(_ALERTS) - 8} more")
    body = "\n".join(lines)

    SMS_GATEWAYS = ("tmomail.net", "vtext.com", "txt.att.net", "messaging.sprintpcs.com", "msg.fi.google.com")
    recipients = [r.strip() for r in ALERT_EMAIL_TO.split(",") if r.strip()]

    for recipient in recipients:
        try:
            is_sms = any(gw in recipient.lower() for gw in SMS_GATEWAYS)
            send_body = body[:300] if is_sms else body

            msg = MIMEText(send_body)
            msg["Subject"] = f"LOW TEMP Bot: {len(_ALERTS)} alerts" if not is_sms else ""
            msg["From"] = ALERT_EMAIL_FROM
            msg["To"] = recipient

            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
                server.sendmail(ALERT_EMAIL_FROM, [recipient], msg.as_string())
        except Exception as e:
            print(f"  ⚠️ Alert to {recipient} failed: {e}")

    print(f"  ✓ Alert notification sent to {len(recipients)} recipient(s)")


BQ_PROJECT = os.environ.get("GCP_PROJECT_ID", "elite-contact-446323-q7")
BQ_DATASET = os.environ.get("GCP_DATASET_ID", "Kalshi")
BQ_TABLE_PREFIX = "KXLOW_"
# cli_readings is shared with the high bot (the CLI product carries both the
# daily max and min) — the fetcher writes it under the KXHIGH_ prefix.
CLI_READINGS_TABLE = "KXHIGH_cli_readings"

if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    if os.path.exists("google_credentials.json") and os.path.getsize("google_credentials.json") > 10:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google_credentials.json"

try:
    bq_client = bigquery.Client(project=BQ_PROJECT)
    print(f"BigQuery connected: {BQ_PROJECT}.{BQ_DATASET}")
except Exception as e:
    alert("BIGQUERY_INIT_FAILED", f"BQ init failed (non-fatal): {e}")
    print(f"BigQuery init failed (non-fatal): {e}")
    bq_client = None


def upload_alerts_to_bq():
    if bq_client is None or len(_ALERTS) == 0 or DRY_RUN:
        return
    # Same fixed schema convention as KXHIGH_alerts: context keys flatten
    # into `error` so load_table_from_dataframe never hits a schema mismatch.
    _CORE = {"timestamp", "category", "message", "error"}
    flat = []
    for a in _ALERTS:
        extras = {k: v for k, v in a.items() if k not in _CORE}
        error_str = str(a.get("error", "") or "")
        if extras:
            extra_str = ", ".join(f"{k}={v}" for k, v in extras.items())
            error_str = f"{error_str} | {extra_str}" if error_str else extra_str
        flat.append({
            "timestamp": a.get("timestamp"),
            "category":  a.get("category"),
            "message":   a.get("message"),
            "error":     error_str,
        })
    try:
        df_alerts = pd.DataFrame(flat)
        table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE_PREFIX}alerts"
        job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND", autodetect=True)
        job = bq_client.load_table_from_dataframe(df_alerts, table_id, job_config=job_config)
        job.result()
        print(f"  ✓ Uploaded {len(df_alerts)} alerts to {table_id}")
    except Exception as e:
        print(f"  ⚠️ Alert upload failed: {e}")


def flush_alerts(checkpoint: str = ""):
    """Upload accumulated _ALERTS to BQ and CLEAR the list (mid-run checkpoint)."""
    if not _ALERTS:
        return
    tag = f" @ {checkpoint}" if checkpoint else ""
    print(f"  ⚡ Flushing {len(_ALERTS)} alerts to BigQuery{tag}")
    upload_alerts_to_bq()
    _ALERTS.clear()


def write_to_bq(df, table_name, write_disposition="WRITE_APPEND", schema=None):
    """Upload DataFrame to BigQuery. Non-fatal on failure (alerts on error).
    Same semantics as the high bot: explicit schema pins types on table
    creation; ALLOW_FIELD_ADDITION lets appends add new nullable columns."""
    if DRY_RUN:
        print(f"  BQ SKIP (dry run): {table_name} — {0 if df is None else len(df)} rows")
        return
    if bq_client is None:
        print(f"  BQ SKIP: {table_name} — bq_client is None")
        return
    if df is None or len(df) == 0:
        print(f"  BQ SKIP: {table_name} — df empty (rows=0)")
        return
    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE_PREFIX}{table_name}"
    try:
        job_config = bigquery.LoadJobConfig(
            write_disposition=write_disposition,
            autodetect=schema is None,
            schema=schema,
            schema_update_options=["ALLOW_FIELD_ADDITION"] if write_disposition == "WRITE_APPEND" else None,
        )
        job = bq_client.load_table_from_dataframe(df, table_id, job_config=job_config)
        job.result()
        print(f"  BQ: {table_id} ← {len(df)} rows (table total: {bq_client.get_table(table_id).num_rows})")
    except Exception as e:
        print(f"  BQ ERROR: {table_id} write failed: {e}")
        try:
            alert("BQ_WRITE_FAIL", f"{table_id}: {e}", {"table": table_name, "rows": len(df)})
        except Exception:
            pass


# Collector for orders placed during this run
all_order_records = []

# ====================================================================
# Per-run identity + tracking (KXLOW_runs; same convention as KXHIGH_runs)
# ====================================================================
RUN_ID = str(uuid.uuid4())
RUN_STARTED_AT = datetime.now(pytz.UTC)


# Explicit schema for KXLOW_runs. Unlike the high bot (whose KXHIGH_runs
# already exists with correct types, so its schema-less appends adopt them),
# every KXLOW_ table is created by THIS bot's first live write — and BQ
# autodetect on a fresh table mis-infers datetime64[ns] as INT64 nanoseconds
# and chokes on all-None columns (the documented 2026-05-08 KXHIGH incident
# pattern). Adding a new field to write_run_row requires adding it here too.
_RUNS_SCHEMA = [
    bigquery.SchemaField("run_id", "STRING"),
    bigquery.SchemaField("event", "STRING"),
    bigquery.SchemaField("event_at", "TIMESTAMP"),
    bigquery.SchemaField("started_at", "TIMESTAMP"),
    bigquery.SchemaField("script_name", "STRING"),
    bigquery.SchemaField("workflow_name", "STRING"),
    bigquery.SchemaField("github_run_id", "STRING"),
    bigquery.SchemaField("github_run_attempt", "STRING"),
    bigquery.SchemaField("runner_os", "STRING"),
    bigquery.SchemaField("variable", "INTEGER"),
    bigquery.SchemaField("night_size_mult", "FLOAT"),
    bigquery.SchemaField("central_time_hour", "INTEGER"),
    bigquery.SchemaField("starting_contracts", "INTEGER"),
    bigquery.SchemaField("trade_tails", "BOOLEAN"),
    bigquery.SchemaField("safety_margin_cents", "INTEGER"),
    bigquery.SchemaField("dry_run", "BOOLEAN"),
    bigquery.SchemaField("n_markets_in_table", "INTEGER"),
    bigquery.SchemaField("finished_at", "TIMESTAMP"),
    bigquery.SchemaField("duration_seconds", "FLOAT"),
    bigquery.SchemaField("n_orders_placed", "INTEGER"),
    bigquery.SchemaField("n_orders_in_records", "INTEGER"),
    bigquery.SchemaField("n_alerts_emitted", "INTEGER"),
    bigquery.SchemaField("exit_status", "STRING"),
]
_RUNS_COLS = [f.name for f in _RUNS_SCHEMA]
_RUNS_TS_COLS = ["event_at", "started_at", "finished_at"]
_RUNS_INT_COLS = ["variable", "central_time_hour", "starting_contracts",
                  "safety_margin_cents", "n_markets_in_table",
                  "n_orders_placed", "n_orders_in_records", "n_alerts_emitted"]


def write_run_row(event, **fields):
    if bq_client is None:
        print(f"  RUNS SKIP ({event}): bq_client is None")
        return
    base = {
        "run_id": RUN_ID,
        "event": event,
        "event_at": datetime.now(pytz.UTC),
        "started_at": RUN_STARTED_AT,
        "script_name": os.path.basename(sys.argv[0]) if sys.argv else "low_temp_trading.py",
        "workflow_name": os.environ.get("GITHUB_WORKFLOW"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "runner_os": os.environ.get("RUNNER_OS"),
    }
    base.update(fields)
    _unknown = [k for k in base if k not in _RUNS_COLS]
    if _unknown:
        print(f"  RUNS ({event}): dropping fields not in _RUNS_SCHEMA: {_unknown}")
    try:
        df = pd.DataFrame([base])
        for col in _RUNS_COLS:
            if col not in df.columns:
                df[col] = None
        df = df[_RUNS_COLS]
        for col in _RUNS_TS_COLS:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
        for col in _RUNS_INT_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        write_to_bq(df, "runs", "WRITE_APPEND", schema=_RUNS_SCHEMA)
    except Exception as e:
        print(f"  RUNS WRITE FAIL ({event}): {e}")


# ====================================================================
# Rolling forecast-bias correction (lows)
# --------------------------------------------------------------------
# Same design as the high bot's: mean(actual − forecast) per city over a
# trailing window, shrunk toward the global mean, capped at ±2°F.
#   - forecasts: KXLOW_market_snapshot (earliest run per city/forecast_date)
#   - actuals  : KXHIGH_cli_readings.low_temp_f
# Until KXLOW_market_snapshot accumulates history the join returns no
# rows and the correction is a no-op. A missing table (first-ever runs)
# logs quietly instead of alerting.
# ====================================================================
BIAS_CORRECTION_ENABLED = os.environ.get("BIAS_CORRECTION_ENABLED", "true").lower() == "true"
BIAS_LOOKBACK_DAYS      = int(os.environ.get("BIAS_LOOKBACK_DAYS", "14"))
BIAS_MIN_SAMPLE         = int(os.environ.get("BIAS_MIN_SAMPLE", "5"))
BIAS_SHRINKAGE_N        = int(os.environ.get("BIAS_SHRINKAGE_N", "10"))
BIAS_MAX_CORRECTION_F   = float(os.environ.get("BIAS_MAX_CORRECTION_F", "2.0"))

ROLLING_BIAS_BY_CITY = None
ROLLING_BIAS_GLOBAL  = 0.0


def compute_rolling_bias():
    """Per-city mean(actual_low − forecast_avg) over BIAS_LOOKBACK_DAYS.
    Returns ({city: bias_F}, global_bias_F); ({}, 0.0) on any failure.

    Preliminary-CLI filter: same heuristic as the high bot (pre-dawn
    high_time + diurnal range ≤ 7°F = partial early-morning report). For
    LOWS a preliminary report is doubly untrustworthy: the morning low may
    not be final yet AND a late-day drop can still lower it, so those rows
    are excluded the same way."""
    if not BIAS_CORRECTION_ENABLED:
        print("  BIAS: BIAS_CORRECTION_ENABLED=false, skipping")
        return {}, 0.0
    if bq_client is None:
        print("  BIAS: bq_client None, skipping correction")
        return {}, 0.0

    sql = f"""
    WITH city_map AS (
      SELECT 'NY' AS abv, 'New York City' AS name UNION ALL
      SELECT 'CHI', 'Chicago' UNION ALL SELECT 'MIA', 'Miami' UNION ALL
      SELECT 'LAX', 'Los Angeles' UNION ALL SELECT 'DEN', 'Denver' UNION ALL
      SELECT 'PHIL', 'Philadelphia' UNION ALL SELECT 'AUS', 'Austin' UNION ALL
      SELECT 'THOU', 'Houston' UNION ALL SELECT 'TATL', 'Atlanta' UNION ALL
      SELECT 'TDC', 'Washington DC' UNION ALL SELECT 'TPHX', 'Phoenix' UNION ALL
      SELECT 'TDAL', 'Dallas' UNION ALL SELECT 'TLV', 'Las Vegas' UNION ALL
      SELECT 'TOKC', 'Oklahoma City' UNION ALL SELECT 'TSEA', 'Seattle' UNION ALL
      SELECT 'TSFO', 'San Francisco' UNION ALL SELECT 'TSATX', 'San Antonio' UNION ALL
      SELECT 'TMIN', 'Minneapolis' UNION ALL SELECT 'TNOLA', 'New Orleans' UNION ALL
      SELECT 'TBOS', 'Boston'
    ),
    first_snap AS (
      SELECT city, forecast_date, forecast_avg,
        ROW_NUMBER() OVER (PARTITION BY city, forecast_date ORDER BY run_date) AS rn
      FROM `{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE_PREFIX}market_snapshot`
      WHERE forecast_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL {BIAS_LOOKBACK_DAYS + 1} DAY)
                              AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
        AND forecast_avg IS NOT NULL
    ),
    fcst AS (
      SELECT city, forecast_date, AVG(forecast_avg) AS forecast_avg
      FROM first_snap WHERE rn = 1
      GROUP BY city, forecast_date
    ),
    cli_parsed AS (
      SELECT city_abv, event_date, high_temp_f, low_temp_f,
        CASE
          WHEN ENDS_WITH(high_time, 'AM') AND SAFE_CAST(REGEXP_EXTRACT(high_time, r'^(\\d{{1,2}})') AS INT64) = 12 THEN 0
          WHEN ENDS_WITH(high_time, 'AM') THEN SAFE_CAST(REGEXP_EXTRACT(high_time, r'^(\\d{{1,2}})') AS INT64)
          WHEN SAFE_CAST(REGEXP_EXTRACT(high_time, r'^(\\d{{1,2}})') AS INT64) = 12 THEN 12
          ELSE SAFE_CAST(REGEXP_EXTRACT(high_time, r'^(\\d{{1,2}})') AS INT64) + 12
        END AS h24
      FROM `{BQ_PROJECT}.{BQ_DATASET}.{CLI_READINGS_TABLE}`
      WHERE event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL {BIAS_LOOKBACK_DAYS + 1} DAY)
                           AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
        AND low_temp_f IS NOT NULL
    ),
    actuals AS (
      SELECT city_abv, event_date, low_temp_f AS actual_low
      FROM cli_parsed
      WHERE NOT (h24 < 6 AND (high_temp_f - low_temp_f) <= 7)
    )
    SELECT cm.name AS city, COUNT(*) AS n,
           AVG(a.actual_low - f.forecast_avg) AS bias
    FROM actuals a
    JOIN city_map cm ON a.city_abv = cm.abv
    JOIN fcst f ON f.city = cm.name AND f.forecast_date = a.event_date
    GROUP BY cm.name
    """

    try:
        df = bq_client.query(sql).to_dataframe()
    except Exception as e:
        if "Not found" in str(e):
            # First runs ever — KXLOW_market_snapshot doesn't exist yet.
            print("  BIAS: KXLOW_market_snapshot not found yet (first runs); skipping correction")
        else:
            alert("BIAS_QUERY_FAILED", f"Rolling-bias query failed: {e}")
            print(f"  BIAS: query failed ({e}); skipping correction")
        return {}, 0.0

    if df.empty:
        print("  BIAS: no rows returned (no snapshot history yet); skipping correction")
        return {}, 0.0

    total_n = float(df["n"].sum())
    global_bias = float((df["bias"] * df["n"]).sum() / total_n) if total_n > 0 else 0.0

    out = {}
    for _, row in df.iterrows():
        city, n, bias = row["city"], int(row["n"]), float(row["bias"])
        if n < BIAS_MIN_SAMPLE:
            corr = global_bias
        else:
            w = n / (n + BIAS_SHRINKAGE_N)
            corr = w * bias + (1.0 - w) * global_bias
        corr = max(-BIAS_MAX_CORRECTION_F, min(BIAS_MAX_CORRECTION_F, corr))
        out[city] = corr

    capped_global = max(-BIAS_MAX_CORRECTION_F, min(BIAS_MAX_CORRECTION_F, global_bias))

    print(f"  BIAS: window={BIAS_LOOKBACK_DAYS}d, global={global_bias:+.2f}°F "
          f"(capped {capped_global:+.2f}, n={int(total_n)} city-days):")
    for city, b in sorted(out.items()):
        raw_row = df[df["city"] == city]
        raw = float(raw_row["bias"].iloc[0]) if not raw_row.empty else 0.0
        n_c = int(raw_row["n"].iloc[0]) if not raw_row.empty else 0
        print(f"    {city:<16} raw={raw:+.2f} n={n_c:>2} applied={b:+.2f}°F")

    if abs(capped_global) >= 0.5:
        alert("BIAS_LARGE_GLOBAL",
              f"Rolling low-temp forecast bias is {capped_global:+.2f}°F over {BIAS_LOOKBACK_DAYS}d "
              f"(n={int(total_n)}). Vendor MOS may be lagging the regime.",
              {"global_bias_F": round(capped_global, 2), "n_city_days": int(total_n),
               "lookback_days": BIAS_LOOKBACK_DAYS})

    return out, capped_global


# ====================================================================
# RUN MODE — which day's low are we trading?
# --------------------------------------------------------------------
# `variable` = day offset from today (CT), same meaning as the high bot
# but with a DIFFERENT boundary. A daily low prints near sunrise, so by
# 08:00 CT today's low is essentially set for every city — any run from
# 08:00 CT onward trades TOMORROW's event. Runs between 00:00 and 07:59
# CT are "day-of" runs on TODAY's event and go through the pre-trade
# observation filters (the mirror of the high bot's variable==0 path).
# ====================================================================
central_tz = pytz.timezone('US/Central')
central_time = datetime.now(central_tz)
variable = 1 if central_time.hour >= 8 else 0
is_day_of_run = variable == 0
target_date = (central_time + timedelta(days=variable)).date()
print(f"\nRun mode: {'DAY-OF (today)' if is_day_of_run else 'EVENING (tomorrow)'} — "
      f"target_date={target_date}, now={central_time.strftime('%Y-%m-%d %H:%M CT')}")

# Station-exact coordinates — same Kalshi settlement stations as KXHIGH
# (the KXLOW series settle on the same CLI product per city).
cities = {
    "Austin":         (30.18304, -97.67987),   # KAUS
    "Miami":          (25.79056, -80.31639),   # KMIA
    "Houston":        (29.63750, -95.28250),   # KHOU Hobby
    "Denver":         (39.84658, -104.65622),  # KDEN
    "New York City":  (40.78333, -73.96667),   # KNYC Central Park
    "Philadelphia":   (39.87327, -75.22678),   # KPHL
    "Chicago":        (41.78417, -87.75528),   # KMDW Midway
    "Los Angeles":    (33.93806, -118.38889),  # KLAX
    "Atlanta":        (33.64028, -84.42694),   # KATL
    "Washington DC":  (38.84833, -77.03417),   # KDCA Reagan
    "Phoenix":        (33.42780, -112.00347),  # KPHX Sky Harbor
    "Dallas":         (32.89743, -97.02196),   # KDFW
    "Las Vegas":      (36.07188, -115.16340),  # KLAS Harry Reid
    "Oklahoma City":  (35.38861, -97.60028),   # KOKC
    "Seattle":        (47.44472, -122.31361),  # KSEA Sea-Tac
    "San Francisco":  (37.61961, -122.36558),  # KSFO
    "San Antonio":    (29.53278, -98.46361),   # KSAT
    "Minneapolis":    (44.88306, -93.22889),   # KMSP
    "New Orleans":    (29.99278, -90.25083),   # KMSY
    "Boston":         (42.36056, -71.01056),   # KBOS Logan
}

CITY_TO_KALSHI_STATION = {
    "New York City":  "KNYC",
    "Chicago":        "KMDW",
    "Miami":          "KMIA",
    "Los Angeles":    "KLAX",
    "Denver":         "KDEN",
    "Philadelphia":   "KPHL",
    "Austin":         "KAUS",
    "Houston":        "KHOU",
    "Atlanta":        "KATL",
    "Washington DC":  "KDCA",
    "Phoenix":        "KPHX",
    "Dallas":         "KDFW",
    "Las Vegas":      "KLAS",
    "Oklahoma City":  "KOKC",
    "Seattle":        "KSEA",
    "San Francisco":  "KSFO",
    "San Antonio":    "KSAT",
    "Minneapolis":    "KMSP",
    "New Orleans":    "KMSY",
    "Boston":         "KBOS",
}

# IANA timezone per city — drives local-midnight order expiry and the
# local-hour risk filters. Phoenix is America/Phoenix (no DST), NOT
# US/Mountain.
CITY_TZ = {
    "New York City": "America/New_York",
    "Philadelphia":  "America/New_York",
    "Miami":         "America/New_York",
    "Atlanta":       "America/New_York",
    "Washington DC": "America/New_York",
    "Boston":        "America/New_York",
    "Chicago":       "America/Chicago",
    "Austin":        "America/Chicago",
    "Houston":       "America/Chicago",
    "Dallas":        "America/Chicago",
    "Oklahoma City": "America/Chicago",
    "San Antonio":   "America/Chicago",
    "Minneapolis":   "America/Chicago",
    "New Orleans":   "America/Chicago",
    "Denver":        "America/Denver",
    "Phoenix":       "America/Phoenix",
    "Las Vegas":     "America/Los_Angeles",
    "Seattle":       "America/Los_Angeles",
    "San Francisco": "America/Los_Angeles",
    "Los Angeles":   "America/Los_Angeles",
}

# Day-of quote stop: LOCAL 02:59 of the target day (~3h before a typical
# sunrise min). Min-hour filter (below) skips cities whose expected min is
# before ~04 local. Evening runs stop at LOCAL 23:59 the night before.
DAY_OF_STOP_LOCAL_HOUR = 3        # day-of orders expire at 02:59 local
MIN_HOUR_SKIP_BEFORE = 4          # day-of: skip city if expected min < 04 local


def compute_expiry_ts(city, now_utc_dt):
    """Order expiration for `city`, or None if the city must be skipped.

    Evening runs: LOCAL 23:59:00 of the night before target_date (one
    minute before the target day's observations begin at local midnight).
    Day-of runs: LOCAL 02:59:00 of target_date (~3h before a typical
    sunrise min).

    NEVER rolls forward a day (the high bot's roll-forward would leave a
    low-market order resting through the entire outcome window). If the
    expiry is already within 5 minutes, returns None → skip city.

    Both stops are derived from LOCAL MIDNIGHT of the target day via
    timedelta arithmetic (midnight always exists — US DST transitions
    happen at 02:00). Building "02:59" with tz.localize directly would
    silently land on 03:59 wall time on the spring-forward night, resting
    orders an extra hour inside the pre-dawn reveal window.
    """
    tz = pytz.timezone(CITY_TZ[city])
    midnight_local = tz.localize(datetime(target_date.year, target_date.month,
                                          target_date.day, 0, 0, 0))
    if is_day_of_run:
        # 2h59m of real time after the day's obs begin (≈02:59 local)
        target_local = midnight_local + timedelta(hours=DAY_OF_STOP_LOCAL_HOUR) - timedelta(minutes=1)
    else:
        # one minute before the target day's obs begin (23:59 local eve)
        target_local = midnight_local - timedelta(minutes=1)
    if (target_local - now_utc_dt).total_seconds() < 300:
        return None
    return int(target_local.timestamp())


NWS_HEADERS = {"User-Agent": "KXLOW-bot/1.0 (contact: jack)"}


def _nws_get(url, retries=3, timeout=10):
    """GET with retry — NWS API is flaky about timeouts."""
    last = None
    for _ in range(retries):
        try:
            r = requests.get(url, headers=NWS_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1)
    raise last


def get_nws_meta(lat, lon):
    """Fetch hourly + daily forecast URLs for a coordinate."""
    try:
        data = _nws_get(f"https://api.weather.gov/points/{lat},{lon}")
        props = data['properties']
        return {
            'hourly_url': props['forecastHourly'],
            'forecast_url': props['forecast'],
        }
    except Exception as e:
        alert("NWS_META_FAILED", f"NWS points failed for {lat},{lon}: {e}",
              {"source": "nws_meta"})
        return None


def get_nws_hourly_profile(meta, city, target_date_local, now_local=None):
    """Hourly-forecast profile of target_date (city-LOCAL calendar day).

    Returns dict (any field may be None):
      day_min_f          min forecast temp over the target local day. On
                         day-of runs the already-elapsed hours are missing
                         from the feed, so this is the min over REMAINING
                         hours (the pre-dawn min window is what matters).
      min_hour_local     local hour of that min
      temp_at_00_f       forecast temp at 00:00 local of target day
                         (None on day-of runs — hour already elapsed)
      morning_min_f      min over local hours 00-12
      late_evening_min_f min over local hours 18-23
      n_hours            how many target-day hours the feed covered
      forecast_update_ts NWS issuance time (ISO8601)
      forecast_temp_at_run_f  forecast temp for the hour containing now
    """
    result = {"day_min_f": None, "min_hour_local": None, "temp_at_00_f": None,
              "morning_min_f": None, "late_evening_min_f": None, "n_hours": 0,
              "forecast_update_ts": None, "forecast_temp_at_run_f": None}
    if meta is None:
        return result
    try:
        data = _nws_get(meta['hourly_url'])
        props = data.get('properties', {})
        result["forecast_update_ts"] = props.get('updateTime')
        periods = props.get('periods', [])
        tz = pytz.timezone(CITY_TZ[city])
        now_hr = None
        if now_local is not None:
            now_local = now_local.astimezone(tz)
            now_hr = (now_local.date(), now_local.hour)
        day_temps = {}   # local hour -> temp
        for p in periods:
            st = datetime.fromisoformat(p['startTime']).astimezone(tz)
            if st.date() == target_date_local:
                day_temps[st.hour] = p['temperature']
            if now_hr is not None and (st.date(), st.hour) == now_hr:
                result["forecast_temp_at_run_f"] = p['temperature']
        if day_temps:
            result["n_hours"] = len(day_temps)
            min_hr = min(day_temps, key=lambda h: (day_temps[h], h))
            result["min_hour_local"] = min_hr
            result["day_min_f"] = day_temps[min_hr]
            result["temp_at_00_f"] = day_temps.get(0)
            morning = [t for h, t in day_temps.items() if h <= 12]
            late = [t for h, t in day_temps.items() if h >= 18]
            result["morning_min_f"] = min(morning) if morning else None
            result["late_evening_min_f"] = min(late) if late else None
        return result
    except Exception as e:
        alert("NWS_HOURLY_FAILED", f"NWS hourly failed for {city}: {e}",
              {"source": "nws_hourly"})
        return result


def get_nws_conditions(meta, city, target_date_local):
    """Short/detailed conditions text for the overnight period of the target
    day — the daily-forecast period containing 04:00 local (the pre-dawn
    window the low usually prints in). Returns (detailed, short)."""
    if meta is None:
        return None, None
    try:
        data = _nws_get(meta['forecast_url'])
        tz = pytz.timezone(CITY_TZ[city])
        probe = tz.localize(datetime(target_date_local.year, target_date_local.month,
                                     target_date_local.day, 4, 0))
        for period in data['properties']['periods']:
            st = datetime.fromisoformat(period['startTime'])
            en = datetime.fromisoformat(period['endTime'])
            if st <= probe < en:
                return period.get('detailedForecast'), period.get('shortForecast')
        return None, None
    except Exception as e:
        print(f"  ⚠️ NWS conditions failed for {city}: {e}")
        return None, None


def get_nws_current_observation(city_name):
    """Latest METAR observation from the EXACT Kalshi settlement station.
    Returns (station_id, temp_f) or (None/station, None)."""
    station_id = CITY_TO_KALSHI_STATION.get(city_name)
    if not station_id:
        return None, None
    try:
        data = _nws_get(f"https://api.weather.gov/stations/{station_id}/observations/latest")
        temp_c = data.get('properties', {}).get('temperature', {}).get('value')
        if temp_c is None:
            return station_id, None
        return station_id, temp_c * 9 / 5 + 32
    except Exception as e:
        alert("NWS_OBS_FAILED", f"NWS observation failed for {station_id}: {e}",
              {"source": "nws_obs", "station": station_id})
        return station_id, None


def get_weatherunderground_low(city, coords, target_date_local):
    """WU calendar-day minimum for the target LOCAL calendar day.

    Uses `calendarDayTemperatureMin` (midnight-to-midnight — matches CLI
    settlement), NOT `temperatureMin` (the traditional overnight low for
    the NIGHT period, which crosses the midnight boundary and belongs
    mostly to the NEXT calendar morning). The index is matched via
    validTimeLocal dates rather than assumed, so day-boundary rolls in
    WU's array can't cause an off-by-one.

    Returns float or 'N/A' after exhausting retries."""
    API_KEY = "a828c2a178844147a8c2a17884a147a5"
    lat, lon = coords
    URL = (f"https://api.weather.com/v3/wx/forecast/daily/5day"
           f"?apiKey={API_KEY}&geocode={lat},{lon}&format=json&units=e&language=en-US")

    _last_err = None
    _last_status = None
    _last_body = None
    for _attempt in range(3):
        try:
            response = requests.get(URL, timeout=10)
            _last_status = response.status_code
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception as _je:
                    _last_err = f"json parse: {_je}"
                    _last_body = response.text[:200] if response.text else ""
                    time.sleep(1.5 * (_attempt + 1))
                    continue
                mins = data.get("calendarDayTemperatureMin")
                valid = data.get("validTimeLocal")
                if isinstance(mins, list) and isinstance(valid, list):
                    idx = None
                    want = target_date_local.strftime('%Y-%m-%d')
                    for i, vt in enumerate(valid):
                        if isinstance(vt, str) and vt.startswith(want):
                            idx = i
                            break
                    if idx is None:
                        _last_err = f"target date {want} not in validTimeLocal"
                        _last_body = str(valid[:3])[:200]
                        break
                    if idx < len(mins) and mins[idx] is not None:
                        return mins[idx]
                    _last_err = f"calendarDayTemperatureMin[{idx}] is null"
                    _last_body = str(mins)[:200]
                else:
                    _last_err = "calendarDayTemperatureMin missing from response"
                    _last_body = str(list(data.keys()))[:200]
                break
            elif response.status_code in (429, 500, 502, 503, 504):
                _last_err = f"HTTP {response.status_code}"
                _last_body = response.text[:200] if response.text else ""
                time.sleep(1.5 * (_attempt + 1))
                continue
            else:
                _last_err = f"HTTP {response.status_code}"
                _last_body = response.text[:200] if response.text else ""
                break
        except requests.exceptions.Timeout as _te:
            _last_err = f"timeout: {_te}"
            time.sleep(1.5 * (_attempt + 1))
            continue
        except requests.exceptions.RequestException as _re:
            _last_err = f"request exc: {_re}"
            time.sleep(1.5 * (_attempt + 1))
            continue

    alert("FORECAST_FAILED",
          f"WU {_last_err} for {city}" + (f" (status={_last_status})" if _last_status else ""),
          {"source": "weather_underground", "status_code": _last_status,
           "err": _last_err, "body": _last_body})
    print(f"  ⚠️ WU FAIL {city} after 3 attempts: {_last_err}"
          + (f" | status={_last_status}" if _last_status else "")
          + (f" | body={_last_body}" if _last_body else ""))
    return "N/A"


# ====================================================================
# FORECAST PULLS — one pass per city: NWS meta, hourly profile,
# conditions, WU calendar-day min. The NWS "forecast" value used for
# pricing is the hourly-forecast min over the target local day (exactly
# settlement-aligned); WU's is calendarDayTemperatureMin.
# ====================================================================
run_date = central_time.strftime('%Y-%m-%d %H:%M:%S')
now_utc = datetime.now(pytz.UTC)

print("\n========== FORECAST PULLS ==========")
forecast_data = []
CITY_HOURLY = {}   # city -> hourly profile dict (reused by risk filters)
CITY_META = {}
EXPIRY_PAST_SKIP = set()   # cities whose quote stop has already passed

for city, coords in cities.items():
    # Early guard: if this city's order expiry is already unreachable
    # (e.g. a 07:25 CT day-of run — every city is past its 02:59-local
    # stop, and the "forecast" at that hour would be a remaining-hours
    # min, not a real daily min), skip it entirely: no forecast pull, no
    # snapshot row (keeps KXLOW_market_snapshot clean for bias analysis),
    # no orders. The cancel sweep below still covers its markets.
    if compute_expiry_ts(city, datetime.now(pytz.UTC)) is None:
        EXPIRY_PAST_SKIP.add(city)
        print(f"  {city:<16} SKIP — quote stop already passed "
              f"({'02:59 local day-of' if is_day_of_run else '23:59 local eve'})")
        continue
    meta = get_nws_meta(*coords)
    CITY_META[city] = meta
    profile = get_nws_hourly_profile(meta, city, target_date, now_local=now_utc)
    CITY_HOURLY[city] = profile
    detailed, short = get_nws_conditions(meta, city, target_date)
    wu_low = get_weatherunderground_low(city, coords, target_date)
    nws_low = profile["day_min_f"] if profile["day_min_f"] is not None else "N/A"

    _mh = profile['min_hour_local']
    print(f"  {city:<16} NWS(hourly min)={nws_low} WU(cal-day min)={wu_low} "
          f"min_hr={_mh if _mh is not None else '?'} "
          f"t00={profile['temp_at_00_f']} late_min={profile['late_evening_min_f']} "
          f"({profile['n_hours']}h) {short or ''}")

    forecast_data.append({
        "City": city,
        "Forecast Date": target_date.strftime('%Y-%m-%d'),
        "Run Date": run_date,
        "Weather Underground": wu_low,
        "NWS": nws_low,
        "NWS Detailed Conditions": detailed,
        "NWS Short Conditions": short,
        "Min Hour Local": profile["min_hour_local"],
        "Temp At 00 Local": profile["temp_at_00_f"],
        "Morning Min": profile["morning_min_f"],
        "Late Evening Min": profile["late_evening_min_f"],
        "NWS Hourly Coverage": profile["n_hours"],
        "nws_forecast_update_ts": profile["forecast_update_ts"],
        "forecast_temp_at_run_hour_f": profile["forecast_temp_at_run_f"],
    })
    time.sleep(0.1)

# Explicit columns so an all-cities-skipped run (e.g. late-morning day-of
# invocation) yields an EMPTY-but-well-formed frame instead of a KeyError —
# the run must still reach the cancel sweep below.
_FORECAST_COLS = ["City", "Forecast Date", "Run Date", "Weather Underground", "NWS",
                  "NWS Detailed Conditions", "NWS Short Conditions",
                  "Min Hour Local", "Temp At 00 Local", "Morning Min",
                  "Late Evening Min", "NWS Hourly Coverage",
                  "nws_forecast_update_ts", "forecast_temp_at_run_hour_f"]
forecast_table = pd.DataFrame(forecast_data, columns=_FORECAST_COLS)
forecast_table[["Weather Underground", "NWS"]] = forecast_table[["Weather Underground", "NWS"]].apply(pd.to_numeric, errors='coerce')
forecast_table["Average"] = forecast_table[["Weather Underground", "NWS"]].mean(axis=1)
forecast_table["Standard Deviation"] = forecast_table[["Weather Underground", "NWS"]].std(axis=1)
forecast_table["Highest Minus Lowest"] = forecast_table[["Weather Underground", "NWS"]].max(axis=1) - forecast_table[["Weather Underground", "NWS"]].min(axis=1)

# ====================================================================
# CITY-LEVEL RISK FILTERS (forecast-shape) — both run modes.
#
# 1. LATE-DAY COLD-DROP (mirror of the high bot's midnight-delta filter):
#    the daily low normally prints pre-dawn, but a front can reset it at
#    23:59. If the forecast's 18-23h local min comes within 4.5°F of the
#    00-12h morning min, the low is NOT safely morning-set → skip city.
#    (On day-of runs morning hours may be partially elapsed; the check
#    uses whatever morning hours remain, and the min-hour filter covers
#    the rest.)
#
# 2. MIDNIGHT-PRINT: if temp@00:00 local is within 1.5°F of the day min,
#    the low likely prints AT local midnight (temps rising overnight) —
#    the outcome is effectively tonight's late-evening temp, revealed
#    while evening orders rest → skip city. (Evening runs only; on
#    day-of runs midnight already happened and the obs filters govern.)
# ====================================================================
LATE_DROP_MIN_DELTA_F = 4.5
MIDNIGHT_PRINT_MIN_DELTA_F = 1.5

print("\n========== FORECAST-SHAPE RISK FILTERS ==========")
FORECAST_SHAPE_SKIP = {}   # city -> reason
for _, r in forecast_table.iterrows():
    c = r['City']
    morning_min = r['Morning Min']
    late_min = r['Late Evening Min']
    t00 = r['Temp At 00 Local']
    day_min = CITY_HOURLY[c]['day_min_f']
    cover = r['NWS Hourly Coverage']

    if cover == 0:
        # No hourly data — can't shape-check. Leave the city in (forecast
        # avg may still exist via WU); obs filters still apply day-of.
        print(f"  ?  {c}: no NWS hourly coverage — shape filters not applied")
        continue
    if morning_min is not None and late_min is not None and \
            (late_min - morning_min) < LATE_DROP_MIN_DELTA_F:
        FORECAST_SHAPE_SKIP[c] = (f"late_day_drop (late_min {late_min}°F within "
                                  f"{LATE_DROP_MIN_DELTA_F}°F of morning_min {morning_min}°F)")
        print(f"  🔽 SKIP {c}: {FORECAST_SHAPE_SKIP[c]}")
        alert("PRE_TRADE_SKIP_LATE_DROP",
              f"{c}: late-evening min {late_min} vs morning min {morning_min}",
              {"city": c, "late_evening_min_f": late_min, "morning_min_f": morning_min})
        continue
    if (not is_day_of_run) and t00 is not None and day_min is not None and \
            (t00 - day_min) < MIDNIGHT_PRINT_MIN_DELTA_F:
        FORECAST_SHAPE_SKIP[c] = (f"midnight_print (temp@00 {t00}°F within "
                                  f"{MIDNIGHT_PRINT_MIN_DELTA_F}°F of day min {day_min}°F)")
        print(f"  🔽 SKIP {c}: {FORECAST_SHAPE_SKIP[c]}")
        alert("PRE_TRADE_SKIP_MIDNIGHT_PRINT",
              f"{c}: temp@00 {t00} vs day min {day_min}",
              {"city": c, "temp_at_00_f": t00, "day_min_f": day_min})
        continue
    print(f"  ✓  {c}: morning_min={morning_min} late_min={late_min} t00={t00} day_min={day_min}")

forecast_table = forecast_table[~forecast_table['City'].isin(FORECAST_SHAPE_SKIP)].reset_index(drop=True)
print(f"==========> {len(FORECAST_SHAPE_SKIP)} city/cities dropped by shape filters; "
      f"{len(forecast_table)} remain")

########### EVENT TICKERS
# Series = KXLOW + same city code as KXHIGH. hi_no_price is the top of the
# NO ladder in cents — uniform 50c to start (no per-city low-temp
# calibration yet); the fair-NO cap below is the binding guard.

month = target_date.strftime("%b").upper()
day = target_date.strftime("%d")

DEFAULT_HI_NO = 50
CITY_ABV = {
    "Chicago": "CHI", "New York City": "NY", "Denver": "DEN",
    "Philadelphia": "PHIL", "Austin": "AUS", "Miami": "MIA",
    "Houston": "THOU", "Los Angeles": "LAX", "Atlanta": "TATL",
    "Washington DC": "TDC", "Phoenix": "TPHX", "Dallas": "TDAL",
    "Las Vegas": "TLV", "Oklahoma City": "TOKC", "Seattle": "TSEA",
    "San Francisco": "TSFO", "San Antonio": "TSATX", "Minneapolis": "TMIN",
    "New Orleans": "TNOLA", "Boston": "TBOS",
}
CITY_HI_NO = {c: DEFAULT_HI_NO for c in CITY_ABV}

# Year derived from target_date, NOT hardcoded — a '26' literal would go
# silently dead on the first 2027 run (the quiet-404 path below would make
# it look like the benign no-event case). The high bot has this hardcode;
# don't copy it back.
yy = target_date.strftime("%y")
all_event_tickers = [[f"KXLOW{abv}-{yy}{month}{day}", city, CITY_HI_NO[city]]
                     for city, abv in CITY_ABV.items()]
event_tickers = pd.DataFrame(all_event_tickers, columns=['Ticker', 'City', 'hi_no_price'])

############ PULL MARKETS
# Strike parsing uses the API's own strike_type/floor_strike/cap_strike
# (NOT the high bot's positional B→T ordering trick — KXLOW events carry
# BOTH a cold tail (strike_type='less') and a warm tail ('greater')).
# Band semantics: 'between' floor=69 cap=70 settles YES on integer CLI
# minima 69 or 70 → continuous band (68.5, 70.5) for the normal CDF.
# Tails: 'less' cap=67 → (−∞, 66.5]; 'greater' floor=74 → [74.5, ∞).
# LOW_RANGE_FLOOR/HIGH_RANGE_CAP are numeric stand-ins for ±∞.
LOW_RANGE_FLOOR = -60.0
HIGH_RANGE_CAP = 150.0

print("\n========== PULL MARKETS ==========")
markets_rows = []
for _, ev in event_tickers.iterrows():
    event_ticker = ev['Ticker']
    print(f"  {event_ticker}", end="")
    try:
        event_response = exchange_client.get_event(event_ticker=event_ticker)
    except Exception as e:
        if '404' in str(e):
            # Expected for cities with no low event listed: as of Jul 2026
            # only the 13 T-prefix cities carry KXLOW events — the classic
            # 7 series (NY/CHI/MIA/LAX/DEN/PHIL/AUS) exist but have never
            # listed one. Keep them configured (they'd start trading here
            # automatically if Kalshi activates them) without alert spam.
            print(f" — no event listed (404)")
        else:
            print(f" — FAIL ({e})")
            alert("EVENT_FETCH_FAILED", f"{event_ticker}: {e}", {"event_ticker": event_ticker})
        continue
    mkts = event_response.get('markets') or []
    n_ok = 0
    for market in mkts:
        st = market.get('strike_type')
        floor = market.get('floor_strike')
        cap = market.get('cap_strike')
        if st == 'between' and floor is not None and cap is not None:
            low_range, high_range = float(floor) - 0.5, float(cap) + 0.5
            kind = 'band'
        elif st == 'less' and cap is not None:
            low_range, high_range = LOW_RANGE_FLOOR, float(cap) - 0.5
            kind = 'cold_tail'
        elif st == 'greater' and floor is not None:
            low_range, high_range = float(floor) + 0.5, HIGH_RANGE_CAP
            kind = 'warm_tail'
        else:
            print(f"\n    ⚠️ unparsed strike on {market.get('ticker')} "
                  f"(strike_type={st}, floor={floor}, cap={cap})")
            alert("STRIKE_PARSE_FAILED", f"{market.get('ticker')}: strike_type={st}",
                  {"ticker": market.get('ticker')})
            continue
        markets_rows.append({
            'event_ticker': event_ticker,
            'market_ticker': market['ticker'],
            'City': ev['City'],
            'strike_kind': kind,
            'low_range': low_range,
            'high_range': high_range,
            'hi_no_price': ev['hi_no_price'],
        })
        n_ok += 1
    print(f" — {n_ok} markets")

markets_table = pd.DataFrame(markets_rows,
                             columns=['event_ticker', 'market_ticker', 'City', 'strike_kind',
                                      'low_range', 'high_range', 'hi_no_price'])

##### COMBINE FORECAST AND MARKET TABLES, CALCULATE PROBABILITIES
from scipy.stats import norm

combined_table = pd.merge(forecast_table, markets_table, on='City', how='inner')
combined_table['Average'] = pd.to_numeric(combined_table['Average'], errors='coerce')
combined_table['Standard Deviation'] = pd.to_numeric(combined_table['Standard Deviation'], errors='coerce')

# ---- Rolling forecast-bias correction (per-city, shrunken, capped) ----------
# Average stays the raw vendor mean (so KXLOW_market_snapshot.forecast_avg
# tracks vendor skill); Average_corrected feeds yes_probability.
print("\n========== ROLLING BIAS CORRECTION ==========")
ROLLING_BIAS_BY_CITY, ROLLING_BIAS_GLOBAL = compute_rolling_bias()
combined_table['bias_correction_F'] = combined_table['City'].map(
    lambda c: ROLLING_BIAS_BY_CITY.get(c, ROLLING_BIAS_GLOBAL)
).fillna(0.0).astype(float)
combined_table['Average_corrected'] = combined_table['Average'] + combined_table['bias_correction_F']
_n_corr = (combined_table['bias_correction_F'].abs() > 0.01).sum()
print(f"  applied to {_n_corr}/{len(combined_table)} rows; "
      f"max |correction|={combined_table['bias_correction_F'].abs().max():.2f}°F"
      if len(combined_table) else "  (no rows)")
# -----------------------------------------------------------------------------

# City floor σ for LOWS: high-bot floors (calibrated on 1816 high-temp
# observations) + 0.3°F, 1.5 default. Vendor Tmin error runs wider than
# Tmax (radiational-cooling nights, cold pools, frontal timing), and we
# have no low-temp calibration yet — err wide until the KXLOW history
# supports tightening.
CITY_FLOOR_STD = {
    "Austin": 1.5, "Miami": 1.3, "Houston": 2.2, "Denver": 1.7,
    "New York City": 1.5, "Philadelphia": 1.6, "Chicago": 1.6, "Los Angeles": 1.5,
    "Atlanta": 1.5, "Washington DC": 1.6, "Phoenix": 1.4, "Dallas": 1.5,
    "Las Vegas": 1.4, "Oklahoma City": 1.7, "Seattle": 1.5, "San Francisco": 1.5,
    "San Antonio": 1.5, "Minneapolis": 1.7, "New Orleans": 1.5, "Boston": 1.6,
}
combined_table['City Floor Std'] = combined_table['City'].map(CITY_FLOOR_STD).fillna(1.5)
combined_table['Standard Deviation'] = combined_table[['Standard Deviation', 'City Floor Std']].max(axis=1)
combined_table['Standard Deviation'] = combined_table['Standard Deviation'].replace({0: 1.5, np.nan: 1.5})
if STD_MULT != 1.0:
    combined_table['Standard Deviation'] = combined_table['Standard Deviation'] * STD_MULT
    print(f"  STD_MULT={STD_MULT:g}x applied to forecast std (calibration experiment)")

combined_table['high_range'] = pd.to_numeric(combined_table['high_range'], errors='coerce')
combined_table['low_range'] = pd.to_numeric(combined_table['low_range'], errors='coerce')

combined_table['yes_probability'] = norm.cdf(combined_table['high_range'], loc=combined_table['Average_corrected'], scale=combined_table['Standard Deviation']) - norm.cdf(combined_table['low_range'], loc=combined_table['Average_corrected'], scale=combined_table['Standard Deviation'])
combined_table['yes_probability'] = combined_table['yes_probability'].round(2)
combined_table['fair_no_price'] = 1 - combined_table['yes_probability']

#### PULL ORDER BOOK OF MARKETS

combined_table['no_highest_bid'] = ''
combined_table['no_lowest_offer'] = ''
combined_table['no_orderbook'] = ''
combined_table['yes_orderbook'] = ''

for index, row in combined_table.iterrows():
    market_ticker = row['market_ticker']
    try:
        orderbook_response = exchange_client.get_orderbook(ticker=market_ticker, depth=3)
    except Exception as e:
        alert("ORDERBOOK_ERROR", f"Fetch failed for {market_ticker}: {e}",
              {"ticker": market_ticker})
        continue

    # Handle both old (cents) and V2 fp (dollar-string) orderbook formats
    no_levels = []
    yes_levels = []
    if 'orderbook_fp' in orderbook_response:
        ob_fp = orderbook_response['orderbook_fp']
        for level in ob_fp.get('no_dollars', []) or []:
            no_levels.append([int(round(float(level[0]) * 100)), int(float(level[1]))])
        for level in ob_fp.get('yes_dollars', []) or []:
            yes_levels.append([int(round(float(level[0]) * 100)), int(float(level[1]))])
    elif 'orderbook' in orderbook_response:
        ob = orderbook_response['orderbook'] or {}
        no_levels = ob.get('no') or []
        yes_levels = ob.get('yes') or []
    else:
        no_levels = orderbook_response.get('no') or []
        yes_levels = orderbook_response.get('yes') or []

    if no_levels and len(no_levels) > 0:
        combined_table.loc[index, 'no_highest_bid'] = str(no_levels[-1][0])
    if yes_levels and len(yes_levels) > 0:
        combined_table.loc[index, 'no_lowest_offer'] = str(100 - yes_levels[-1][0])
    combined_table.loc[index, 'no_orderbook'] = str(no_levels)
    combined_table.loc[index, 'yes_orderbook'] = str(yes_levels)

# =====================================================================
# PRE-TRADE OBSERVATION FILTERS — day-of runs only (the target day has
# started; obs are flowing). Mirror of the high bot's day-run block:
#   (1) expected min prints before/at our quote stop → skip city
#   (2) obs already at/below forecast min − 2 → forecast busted cold →
#       skip city
#   (3) per-bucket: obs ≤ bucket high_range → bucket active or still
#       reachable downward → skip bucket (applied in the order loop)
# =====================================================================
PRE_TRADE_SKIP_CITIES = set(FORECAST_SHAPE_SKIP)   # shape skips already applied upstream
PRE_TRADE_OBSERVED = {}
PRE_TRADE_STATE = {}

if is_day_of_run:
    print("\n========== PRE-TRADE OBS CHECKS (day-of run) ==========")
    _forecast_min_by_city = dict(zip(combined_table['City'], combined_table['Average']))
    for _city in list(dict.fromkeys(combined_table['City'])):
        if _city in PRE_TRADE_SKIP_CITIES:
            continue
        _profile = CITY_HOURLY.get(_city) or {}
        _min_hr = _profile.get('min_hour_local')
        _station, _obs = get_nws_current_observation(_city)
        _fmin = _forecast_min_by_city.get(_city)
        _f_at_run = _profile.get('forecast_temp_at_run_f')
        _obs_minus_forecast = (_obs - _f_at_run) if (_obs is not None and _f_at_run is not None) else None
        _skip_reason = None

        _obs_status = f"{_obs:.1f}F@{_station}" if _obs is not None else f"FAIL@{_station}"
        if _min_hr is not None and _min_hr < MIN_HOUR_SKIP_BEFORE:
            _skip_reason = f"min_before_stop (expected min {_min_hr:02d}:00 local < {MIN_HOUR_SKIP_BEFORE:02d}:00)"
            print(f"  SKIP {_city}: {_skip_reason}")
            alert("PRE_TRADE_SKIP_MIN_HOUR", f"{_city}: min hr {_min_hr} < {MIN_HOUR_SKIP_BEFORE}",
                  {"city": _city, "min_hour_local": _min_hr})
            PRE_TRADE_SKIP_CITIES.add(_city)
        elif _obs is not None and _fmin is not None and not pd.isna(_fmin) and _obs < _fmin - 2:
            _skip_reason = f"forecast_busted (obs {_obs:.0f}F < forecast min {_fmin:.0f}F - 2F)"
            print(f"  SKIP {_city}: {_skip_reason}")
            alert("PRE_TRADE_SKIP_BUSTED",
                  f"{_city}: obs {_obs:.1f} < forecast min {_fmin:.1f} - 2",
                  {"city": _city, "observed_f": _obs, "forecast_min_f": _fmin})
            PRE_TRADE_SKIP_CITIES.add(_city)
        else:
            if _obs is not None:
                PRE_TRADE_OBSERVED[_city] = _obs
            _drift_str = (f"drift {_obs_minus_forecast:+.1f}F"
                          if _obs_minus_forecast is not None else "drift n/a")
            _min_str = f"{_min_hr:02d}:00" if _min_hr is not None else "?"
            print(f"  OK  {_city}: expected min {_min_str} local, obs {_obs_status}, "
                  f"forecast_min {_fmin if _fmin is not None else '?'}, {_drift_str}")

        PRE_TRADE_STATE[_city] = {
            "observed_temp_f": _obs,
            "observed_station": _station,
            "pre_trade_skip_reason": _skip_reason,
            "obs_minus_forecast_at_run_f": _obs_minus_forecast,
        }
        time.sleep(0.1)
    print(f"==========> {len(PRE_TRADE_SKIP_CITIES)} city/cities skipped total; "
          f"{len(PRE_TRADE_OBSERVED)} have observations for per-bucket check\n")
    flush_alerts("after pre-trade filters")

# Stamp per-city pre-trade/observation state onto combined_table
for _col in ["observed_temp_f", "observed_station", "pre_trade_skip_reason",
             "obs_minus_forecast_at_run_f"]:
    combined_table[_col] = combined_table['City'].map(
        lambda c, col=_col: (PRE_TRADE_STATE.get(c) or {}).get(col)
    )
# Shape-skip reasons for cities dropped before the merge never reach
# combined_table (their forecast rows were filtered) — that's fine; they
# are recorded in KXLOW_alerts.

####### WRITE MARKET SNAPSHOT TO BIGQUERY

print("\nWriting market snapshot to BigQuery...")

_SNAPSHOT_COL_MAP = {
    "City": "city",
    "Forecast Date": "forecast_date",
    "Run Date": "run_date",
    "Weather Underground": "weather_underground",
    "NWS": "nws",
    "Average": "forecast_avg",
    "Standard Deviation": "forecast_std",
    "Highest Minus Lowest": "forecast_range",
    "NWS Detailed Conditions": "nws_detailed_conditions",
    "NWS Short Conditions": "nws_short_conditions",
    "Min Hour Local": "min_hour_local",
    "Temp At 00 Local": "temp_at_00_f",
    "Morning Min": "morning_min_f",
    "Late Evening Min": "late_evening_min_f",
    "bias_correction_F": "bias_correction_f",
    "strike_kind": "strike_kind",
}
_SNAPSHOT_BQ_COLS = [
    "city", "forecast_date", "run_date",
    "weather_underground", "nws",
    "forecast_avg", "forecast_std", "forecast_range",
    "bias_correction_f",
    "nws_detailed_conditions", "nws_short_conditions",
    "min_hour_local", "temp_at_00_f", "morning_min_f", "late_evening_min_f",
    "event_ticker", "market_ticker", "strike_kind",
    "low_range", "high_range", "hi_no_price",
    "yes_probability", "fair_no_price",
    "no_highest_bid", "no_lowest_offer",
    "no_orderbook", "yes_orderbook",
    "position",
    "observed_temp_f", "observed_station",
    "pre_trade_skip_reason",
    "nws_forecast_update_ts",
    "forecast_temp_at_run_hour_f",
    "obs_minus_forecast_at_run_f",
]
_SNAPSHOT_NUMERIC_COLS = [
    "weather_underground", "nws",
    "forecast_avg", "forecast_std", "forecast_range", "bias_correction_f",
    "temp_at_00_f", "morning_min_f", "late_evening_min_f",
    "low_range", "high_range", "hi_no_price",
    "yes_probability", "fair_no_price",
    "no_highest_bid", "no_lowest_offer",
    "observed_temp_f",
    "forecast_temp_at_run_hour_f", "obs_minus_forecast_at_run_f",
]
_SNAPSHOT_SCHEMA = [
    bigquery.SchemaField("city", "STRING"),
    bigquery.SchemaField("forecast_date", "DATE"),
    bigquery.SchemaField("run_date", "TIMESTAMP"),
    bigquery.SchemaField("weather_underground", "FLOAT"),
    bigquery.SchemaField("nws", "FLOAT"),
    bigquery.SchemaField("forecast_avg", "FLOAT"),
    bigquery.SchemaField("forecast_std", "FLOAT"),
    bigquery.SchemaField("forecast_range", "FLOAT"),
    bigquery.SchemaField("bias_correction_f", "FLOAT"),
    bigquery.SchemaField("nws_detailed_conditions", "STRING"),
    bigquery.SchemaField("nws_short_conditions", "STRING"),
    bigquery.SchemaField("min_hour_local", "INTEGER"),
    bigquery.SchemaField("temp_at_00_f", "FLOAT"),
    bigquery.SchemaField("morning_min_f", "FLOAT"),
    bigquery.SchemaField("late_evening_min_f", "FLOAT"),
    bigquery.SchemaField("event_ticker", "STRING"),
    bigquery.SchemaField("market_ticker", "STRING"),
    bigquery.SchemaField("strike_kind", "STRING"),
    bigquery.SchemaField("low_range", "FLOAT"),
    bigquery.SchemaField("high_range", "FLOAT"),
    bigquery.SchemaField("hi_no_price", "FLOAT"),
    bigquery.SchemaField("yes_probability", "FLOAT"),
    bigquery.SchemaField("fair_no_price", "FLOAT"),
    bigquery.SchemaField("no_highest_bid", "INTEGER"),
    bigquery.SchemaField("no_lowest_offer", "FLOAT"),
    bigquery.SchemaField("no_orderbook", "STRING"),
    bigquery.SchemaField("yes_orderbook", "STRING"),
    bigquery.SchemaField("position", "INTEGER"),
    bigquery.SchemaField("observed_temp_f", "FLOAT"),
    bigquery.SchemaField("observed_station", "STRING"),
    bigquery.SchemaField("pre_trade_skip_reason", "STRING"),
    bigquery.SchemaField("nws_forecast_update_ts", "TIMESTAMP"),
    bigquery.SchemaField("forecast_temp_at_run_hour_f", "FLOAT"),
    bigquery.SchemaField("obs_minus_forecast_at_run_f", "FLOAT"),
]

snapshot_df = combined_table.rename(columns=_SNAPSHOT_COL_MAP).copy()
for col in _SNAPSHOT_BQ_COLS:
    if col not in snapshot_df.columns:
        snapshot_df[col] = None
snapshot_df = snapshot_df[_SNAPSHOT_BQ_COLS]
for col in _SNAPSHOT_NUMERIC_COLS:
    snapshot_df[col] = pd.to_numeric(snapshot_df[col], errors="coerce")
snapshot_df["forecast_date"] = pd.to_datetime(snapshot_df["forecast_date"], errors="coerce").dt.date
snapshot_df["run_date"] = pd.to_datetime(snapshot_df["run_date"], errors="coerce")
snapshot_df["position"] = pd.to_numeric(snapshot_df["position"], errors="coerce").astype("Int64")
snapshot_df["min_hour_local"] = pd.to_numeric(snapshot_df["min_hour_local"], errors="coerce").astype("Int64")
snapshot_df["nws_forecast_update_ts"] = pd.to_datetime(
    snapshot_df["nws_forecast_update_ts"], errors="coerce", utc=True
)

write_to_bq(snapshot_df, "market_snapshot", "WRITE_APPEND", schema=_SNAPSHOT_SCHEMA)
flush_alerts("after snapshot write")

combined_table = combined_table.fillna("")


def fetch_all_orders(ticker, max_pages=10):
    """Every order on a ticker, following the pagination cursor."""
    orders = []
    cursor = None
    for _ in range(max_pages):
        resp = exchange_client.get_orders(ticker=ticker, limit=200, cursor=cursor)
        if not isinstance(resp, dict):
            break
        orders.extend(resp.get('orders') or [])
        cursor = resp.get('cursor') or None
        if not cursor:
            break
    if cursor:
        print(f"  ⚠️ get_orders page cap ({max_pages} pages) hit for {ticker}; "
              f"resting exposure may be undercounted")
    return orders


####### DELETE EXISTING ORDERS
# Sweep over markets_table (ALL discovered markets), not combined_table:
# a city that got shape/obs-skipped THIS run may still carry resting
# orders from a previous run, and those should die now, not at expiry.

if DRY_RUN:
    print("\n(dry run: skipping cancel sweep)")
else:
    for market_ticker in markets_table['market_ticker']:
        open_orders = fetch_all_orders(market_ticker)
        for i in range(len(open_orders)):
            order_id = open_orders[i]['order_id']
            status = open_orders[i]['status']
            _rem = open_orders[i].get('remaining_count') or 0
            order_id = {'order_id': order_id}
            if _rem > 0 or status in ('resting', 'partial_filled', 'partially_filled', 'pending'):
                try:
                    exchange_client.cancel_order(**order_id)
                except Exception as _ce:
                    print(f"  cancel failed for {order_id}: {_ce}")

############## PULL CURRENT POSITIONS
combined_table['position'] = 0
for market_ticker in combined_table['market_ticker']:
    try:
        current_position = exchange_client.get_positions(ticker=market_ticker)
    except Exception as _pe:
        print(f"  ⚠️ get_positions failed for {market_ticker}: {_pe}")
        continue
    if 'market_positions' in current_position and current_position['market_positions']:
        positions = pd.DataFrame(current_position['market_positions'])
        if 'position' in positions.columns:
            live_positions = positions[positions['position'] != 0]
            if not live_positions.empty:
                row_index = combined_table.index[combined_table['market_ticker'] == market_ticker].tolist()[0]
                combined_table.loc[row_index, 'position'] = abs(live_positions['position'].iloc[0])
# float, not int: V2 contract quantities are fractional (e.g. 196.74)
combined_table['position'] = combined_table['position'].fillna(0).astype(float)
combined_table['resting_order_count'] = 0.0

##### PLACE ORDERS

######### BETTING INPUTS
increment = 2
# Testing size (Jack 2026-07-24): 2/rung day-of, 4/rung evening (x2 night)
# — raised from 1/2, vs the high bot's 15/30. Bump via LOW_STARTING_CONTRACTS
# after real KXLOW P&L.
starting_contracts = int(os.environ.get("LOW_STARTING_CONTRACTS", "2"))
price_count = list(range(0, 8))

# Tail markets are OFF in v1 — no low-temp actual-vs-forecast error
# distribution exists yet, and the cold tail (radiational-cooling busts)
# is the fat one. Flip TRADE_TAILS=true only after building the
# distribution (see analysis/kxhigh/python/build_forecast_error_dist.py
# for the high-temp template).
TRADE_TAILS = os.environ.get("TRADE_TAILS", "false").lower() == "true"

# Night-run size multiplier: evening runs (trading tomorrow) rest longest
# and get 2x, matching the high bot's overnight-size logic.
is_night_run = variable == 1
night_size_mult = 2 if is_night_run else 1.0

# Per-city knobs — flat until KXLOW P&L data justifies tilts.
CITY_SIZE_MULT = {}
CITY_MIN_NO_PRICE = {}
CITY_MAX_CONTRACTS = {}
max_contracts = int(os.environ.get("LOW_MAX_CONTRACTS", "50"))

market_cutoff_probability = .2

# Always-on fair-NO cap (the KXHIGH A/B "treatment" rule, hard-applied):
# top of ladder = min(hi_no_config, 100·P(no) − margin). On a brand-new
# model this is the main guard against overbidding confident buckets.
SAFETY_MARGIN_CENTS = int(os.environ.get("LOW_SAFETY_MARGIN_CENTS", "3"))

#################################################### ORDER EXPIRY
# compute_expiry_ts is defined up top (the forecast loop needs it for the
# early skip); the per-market Filter 4 below re-checks it right before
# placement in case time passed mid-run.

print(f"\n[CONFIG] starting_contracts={starting_contracts} night_mult={night_size_mult:g} "
      f"max_contracts={max_contracts} trade_tails={TRADE_TAILS} "
      f"divergence_gate={MAX_DIVERGENCE_CENTS:g}c std_mult={STD_MULT:g} "
      f"safety_margin={SAFETY_MARGIN_CENTS}c dry_run={DRY_RUN}")

write_run_row(
    "start",
    variable=int(variable),
    night_size_mult=float(night_size_mult),
    central_time_hour=int(central_time.hour),
    starting_contracts=int(starting_contracts),
    trade_tails=bool(TRADE_TAILS),
    safety_margin_cents=int(SAFETY_MARGIN_CENTS),
    dry_run=bool(DRY_RUN),
    n_markets_in_table=int(len(combined_table)),
)

def _fmt(v, unit='', prec=1, none_str='N/A'):
    """Tolerantly format a numeric field (handles None, '', NaN, strings)."""
    try:
        import math as _math
        if v is None: return none_str
        if isinstance(v, str) and v.strip() == '': return none_str
        f = float(v)
        if _math.isnan(f): return none_str
        return f"{f:.{prec}f}{unit}"
    except Exception:
        return none_str


def _missing(v):
    """True for None, empty string (post-fillna('')), or NaN."""
    if v is None: return True
    if isinstance(v, str) and v.strip() == '': return True
    try:
        import math as _math
        if isinstance(v, float) and _math.isnan(v): return True
    except Exception:
        pass
    return False


# ====================================================================
# Exchange-pause handling. Kalshi pauses trading during its overnight
# maintenance/settlement window: observed 2026-07-23 ~02:17–03:47 CT, when
# every still-open market returned 409 trading_is_paused / 503
# service_unavailable. The bot placed 0 orders but hammered all 8 rungs of
# every market and burned the shared rate budget into a 429 cascade (one
# run: 25 trading_is_paused + 3 service_unavailable + 19 too_many_requests
# = ~40–101 alerts). The SAME markets placed cleanly in the evening runs,
# so this is purely a time-window state, not a per-market condition.
#
# Two responses:
#   1. Per-market short-circuit: on a pause/unavailable error (placement OR
#      cap-check), stop laddering THIS market immediately — the remaining
#      rungs would all fail the same way.
#   2. Global early-out: once EXCHANGE_PAUSE_ABORT_THRESHOLD distinct
#      markets have paused, the whole exchange is down for the window —
#      abort the run (the next scheduled run re-quotes once it clears). A
#      genuine single-market pause (1–2 markets) still just skips those and
#      continues.
# One EXCHANGE_PAUSED alert per market replaces the per-rung storm.
# ====================================================================
EXCHANGE_PAUSE_ABORT_THRESHOLD = int(os.environ.get("LOW_PAUSE_ABORT_THRESHOLD", "3"))
_PAUSED_MARKETS = set()


def _is_pause_error(s):
    """True if an error string is an exchange pause / unavailability, not a
    per-order rejection. Matches the Kalshi error codes and the HttpError
    status prefixes precisely (not a bare '409'/'503' substring, which
    could appear in a price or ticker)."""
    s = str(s).lower()
    return ("trading_is_paused" in s or "service_unavailable" in s
            or "httperror(409" in s or "httperror(503" in s)


orders_placed = 0
_diag_printed = False
for index, row in combined_table.iterrows():
  ticker = row['market_ticker']
  is_tail = row['strike_kind'] in ('cold_tail', 'warm_tail')
  yes_prob = row['yes_probability']
  no_offer = row['no_lowest_offer']
  no_bid = row['no_highest_bid']
  hi_no = row['hi_no_price']

  # Global early-out: exchange is in its pause window (many markets down).
  if len(_PAUSED_MARKETS) >= EXCHANGE_PAUSE_ABORT_THRESHOLD:
    print(f"\n  ⏸ EXCHANGE PAUSED across {len(_PAUSED_MARKETS)} markets "
          f"(≥{EXCHANGE_PAUSE_ABORT_THRESHOLD}) — aborting remaining markets; "
          f"the next scheduled run will re-quote once trading resumes.")
    alert("EXCHANGE_PAUSE_ABORT",
          f"Aborted run: {len(_PAUSED_MARKETS)} markets paused "
          f"(likely Kalshi maintenance window). {orders_placed} orders placed before abort.",
          {"paused_markets": len(_PAUSED_MARKETS), "orders_before_abort": orders_placed})
    break

  print(f"\n  {ticker} [{row['City']}] ({row['strike_kind']}):")

  # Filter 0: market still open?
  try:
    _mkt_resp = exchange_client.get_market(ticker=ticker)
    _mkt_status = _mkt_resp.get('market', {}).get('status', '')
    if _mkt_status not in ('open', 'active', ''):
      print(f"    SKIP: market status '{_mkt_status}' (closed/settled)")
      continue
  except:
    pass

  # Refresh position for THIS ticker (bulk fetch may be stale)
  try:
    _pos_resp = exchange_client.get_positions(ticker=ticker)
    _mps = _pos_resp.get('market_positions', []) if isinstance(_pos_resp, dict) else []
    _live = [p for p in _mps if p.get('ticker') == ticker and p.get('position', 0) != 0]
    _fresh_pos = abs(_live[0].get('position', 0)) if _live else 0
    if _fresh_pos != row['position']:
      print(f"    📊 position refresh {row['position']} → {_fresh_pos}")
      row['position'] = _fresh_pos
  except Exception as _pe:
    pass

  # Refresh resting-order count (cancel is async; partials survive sweeps)
  try:
    _orders = fetch_all_orders(ticker)
    _live_resting = 0.0
    for _o in _orders:
      _rem = _o.get('remaining_count')
      if _rem is None:
        _status = _o.get('status', '')
        if _status not in ('resting', 'partial_filled', 'partially_filled', 'pending'):
          continue
        try:
          _rem = max(0.0, float(_o.get('count', 0)) - float(_o.get('filled_count', 0)))
        except (TypeError, ValueError):
          _rem = 0.0
      _live_resting += float(_rem)
    if _live_resting != float(row['resting_order_count']):
      print(f"    📊 resting refresh {row['resting_order_count']} → {_live_resting:g}")
      row['resting_order_count'] = _live_resting
  except Exception as _re:
    pass

  # Filter 0b: city-level pre-trade skip (shape or obs filters)
  if row['City'] in PRE_TRADE_SKIP_CITIES:
    print(f"    SKIP: city-level pre-trade filter active for {row['City']}")
    continue

  # Filter 1: tails off in v1 (gate BEFORE the per-bucket obs filter so
  # tail sentinels never interact with it)
  if is_tail and not TRADE_TAILS:
    print(f"    SKIP: tail market ({row['strike_kind']}, TRADE_TAILS=false)")
    continue

  # Filter 0c (day-of, BANDS only): obs already at/below bucket top — the
  # low will be ≤ obs, so this bucket is active now or still reachable
  # downward; the model's yes_probability didn't condition on the obs →
  # adverse. Mirror of the high bot's obs ≥ low_range skip. Bands only:
  # the warm tail's high_range=150 sentinel would make `obs <= 150`
  # unconditionally true, and tail semantics differ anyway.
  _obs_now = PRE_TRADE_OBSERVED.get(row['City'])
  if row['strike_kind'] == 'band' and _obs_now is not None and _obs_now <= row['high_range']:
    print(f"    SKIP: obs {_obs_now:.0f}F <= bucket high_range {row['high_range']:.0f}F "
          f"(bucket active or still reachable downward)")
    continue

  # Filter 2: probability cutoff. Guard first: if both forecast sources
  # failed, yes_probability is NaN → '' after fillna(''), and '' > 0.2
  # would raise TypeError mid-loop, aborting every remaining city's
  # ladder (latent in the high bot too).
  if _missing(yes_prob):
    print(f"    SKIP: yes_probability missing (both forecast sources failed?)")
    continue
  if not (yes_prob > market_cutoff_probability or (is_tail and TRADE_TAILS)):
    print(f"    SKIP: P(yes)={yes_prob:.2f} <= {market_cutoff_probability} cutoff")
    continue

  # Filter 3: orderbook data present
  if no_offer == '' or no_bid == '':
    print(f"    SKIP: no orderbook data")
    continue

  # Filter 4: expiry computable and not in the past
  _exp_ts = compute_expiry_ts(row['City'], now_utc_dt=datetime.now(pytz.UTC))
  if _exp_ts is None:
    print(f"    SKIP: expiry already past for {row['City']} "
          f"({'day-of 02:59 local' if is_day_of_run else 'local midnight'})")
    continue

  # Fair-NO cap (always on)
  _fair_no_cents = 100.0 * (1.0 - float(yes_prob))

  # Filter 5 (2026-08-12): model-vs-market divergence gate. When our fair NO
  # is far above what the market pays, history says the MARKET is right
  # (fair-paid >15c: NO won 28%, -$32 of the bot's -$28 total). Same
  # humility rule as the crypto MMs' MAX_FAIR_DIVERGENCE: big disagreement
  # means we'd be taking a view, not providing liquidity.
  _no_mid = (float(no_bid) + float(no_offer)) / 2.0
  _div = _fair_no_cents - _no_mid
  if MAX_DIVERGENCE_CENTS > 0 and _div > MAX_DIVERGENCE_CENTS:
    print(f"    SKIP: divergence {_div:.0f}c (fair_NO {_fair_no_cents:.0f}c vs "
          f"market NO mid {_no_mid:.0f}c) > {MAX_DIVERGENCE_CENTS:g}c gate")
    continue

  _hi_no_config = float(hi_no)
  _effective_hi_no = min(_hi_no_config, _fair_no_cents - SAFETY_MARGIN_CENTS)
  if _effective_hi_no < 2:
    print(f"    SKIP: effective_hi_no={_effective_hi_no:.1f}c "
          f"(fair_NO={_fair_no_cents:.1f}c, margin={SAFETY_MARGIN_CENTS}c, "
          f"hi_no_config={_hi_no_config:.0f}c)")
    continue

  # ==================================================================
  # Detail block (markets that passed all pre-filters)
  # ==================================================================
  _nws_f = row.get('NWS')
  _wu_f = row.get('Weather Underground')
  _avg_f = row.get('Average')
  _avg_corr = row.get('Average_corrected')
  _eff_sigma = row.get('Standard Deviation')
  _city_floor = row.get('City Floor Std')
  _hi_lo = row.get('Highest Minus Lowest')

  try:
    _raw_vals = []
    for _v in [_nws_f, _wu_f]:
      try:
        _vf = float(_v)
        if not np.isnan(_vf): _raw_vals.append(_vf)
      except Exception:
        pass
    _iss = float(np.std(_raw_vals, ddof=1)) if len(_raw_vals) >= 2 else None
  except Exception:
    _iss = None

  print(f"    Forecasts:  NWS(hourly min)={_fmt(_nws_f, '°F', 0)} | "
        f"WU(cal-day min)={_fmt(_wu_f, '°F', 0)} | "
        f"μ={_fmt(_avg_f, '°F', 1)} (corr {_fmt(_avg_corr, '°F', 1)}) | "
        f"spread={_fmt(_hi_lo, '°F', 1)}")
  _floor_tag = ''
  try:
    if _eff_sigma is not None and _city_floor is not None:
      _ef = float(_eff_sigma); _cf = float(_city_floor)
      _is_v = _iss if _iss is not None else 0.0
      if _ef >= _cf - 1e-9 and _ef > _is_v + 1e-9:
        _floor_tag = ' (FLOOR ACTIVE — sources agree / too close)'
  except Exception:
    pass
  print(f"    σ:          inter-source={_fmt(_iss, '°F', 2)} | "
        f"city_floor={_fmt(_city_floor, '°F', 2)} ({row['City']}) | "
        f"effective={_fmt(_eff_sigma, '°F', 2)}{_floor_tag}")

  _short = row.get('NWS Short Conditions') or 'N/A'
  _det = row.get('NWS Detailed Conditions') or ''
  if isinstance(_det, str) and len(_det) > 80:
    _det = _det[:77] + '...'
  _cond_line = f"    Conditions: {_short}"
  if _det and _det != _short:
    _cond_line += f' — "{_det}"'
  print(_cond_line)

  _min_hr = row.get('Min Hour Local')
  _t00 = row.get('Temp At 00 Local')
  _late = row.get('Late Evening Min')
  _obs_t = row.get('observed_temp_f')
  _obs_st = row.get('observed_station')
  _min_part = (f"expected min {int(_min_hr):02d}:00 local" if not _missing(_min_hr) else "expected min N/A")
  _obs_part = (f"obs {_obs_st}={_fmt(_obs_t, '°F', 0)} at run" if not _missing(_obs_t) else "obs n/a (evening run)")
  print(f"    Timing:     {_min_part} | t00={_fmt(_t00, '°F', 0)} | "
        f"late_min={_fmt(_late, '°F', 0)} | {_obs_part}")

  _lo_r = float(row['low_range']); _hi_r = float(row['high_range'])
  _suf = ticker.split('-')[-1]
  if row['strike_kind'] == 'band':
    _band_str = f"[{_lo_r:g}, {_hi_r:g}]°F"
  elif row['strike_kind'] == 'cold_tail':
    _band_str = f"[≤{_hi_r:g}]°F (cold tail)"
  else:
    _band_str = f"[≥{_lo_r:g}]°F (warm tail)"
  print(f"    Band:       {_suf} = {_band_str}")
  try:
    _mu = float(_avg_corr); _s = float(_eff_sigma)
    if _s > 0:
      _z_hi = (_hi_r - _mu) / _s; _z_lo = (_lo_r - _mu) / _s
      _p_hi = float(norm.cdf(_z_hi)); _p_lo = float(norm.cdf(_z_lo))
      print(f"                P(yes) = Φ({_z_hi:+.2f}) − Φ({_z_lo:+.2f}) "
            f"= {_p_hi:.3f} − {_p_lo:.3f} = {yes_prob:.2f}")
  except Exception:
    pass

  _fair_no_c = round(100 * (1 - yes_prob))
  print(f"    Model:      hi_no config = {int(_hi_no_config)}c | fair NO ≈ {_fair_no_c}c | "
        f"effective_hi_no={_effective_hi_no:.0f}c (margin={SAFETY_MARGIN_CENTS}c) | "
        f"P(no)={1-yes_prob:.2f}")

  try:
    _nb_i = int(no_bid); _no_i = int(no_offer)
    print(f"    Orderbook:  no_bid={_nb_i}c, no_offer={_no_i}c | spread={_no_i - _nb_i}c")
  except Exception:
    _nb_i = None; _no_i = None
    print(f"    Orderbook:  no_bid={no_bid}, no_offer={no_offer} (parse issue)")

  _city_mult = CITY_SIZE_MULT.get(row['City'], 1.0)
  _base_size = max(1, int(round(starting_contracts * night_size_mult * _city_mult)))
  print(f"    Sizing:     base={starting_contracts} × night={night_size_mult:g}x × "
        f"city={_city_mult:g}x → {_base_size}/rung (increment={increment}c)")

  _cap = CITY_MAX_CONTRACTS.get(row['City'], max_contracts)
  _headroom = _cap - float(row['position']) - float(row['resting_order_count'])
  print(f"    Position:   held={row['position']}, resting={row['resting_order_count']}, "
        f"cap={_cap} (headroom={_headroom:g})")
  _exp_dt_local = datetime.fromtimestamp(_exp_ts, pytz.timezone(CITY_TZ[row['City']]))
  print(f"    Expiry:     {_exp_dt_local.strftime('%Y-%m-%d %H:%M %Z')} "
        f"({'02:59-local day-of stop' if is_day_of_run else 'local midnight of target day'})")

  print(f"    Ladder (maker-only post_only; filters: bid<no_offer AND bid<no_bid−3):")

  # ==================================================================
  # Ladder loop — same cap-safety architecture as the high bot: local
  # placed-counter + live re-query, exposure = max(live, local).
  # ==================================================================
  level_orders = 0
  _rungs = []
  _placed_rungs = []
  _skip_cnt = {"below_city_min": 0, "bid>=no_offer": 0, "bid>=no_bid-3": 0, "position_cap": 0, "order_failed": 0}
  _initial_position = float(row['position'])
  _initial_resting = float(row['resting_order_count'])
  _run_placed_contracts = 0

  for i in price_count:
    bid_price = max(_effective_hi_no - i * increment, 1)
    city_mult = CITY_SIZE_MULT.get(row['City'], 1.0)
    contracts = max(1, int(round(starting_contracts * night_size_mult * city_mult)))
    edge = (1.0 - yes_prob) - (bid_price / 100.0)

    _city_min = CITY_MIN_NO_PRICE.get(row['City'])
    if _city_min is not None and bid_price < _city_min:
      _rungs.append((i, bid_price, contracts, edge, 'SKIP',
                     f'bid<city_min({_city_min}c, {row["City"]})'))
      _skip_cnt["below_city_min"] += 1
      continue
    if not (bid_price < int(no_offer)):
      _rungs.append((i, bid_price, contracts, edge, 'SKIP',
                     f'bid≥no_offer({int(no_offer)})'))
      _skip_cnt["bid>=no_offer"] += 1
      continue
    if not (bid_price < int(no_bid) - 3):
      _rungs.append((i, bid_price, contracts, edge, 'SKIP',
                     f'bid≥no_bid−3({int(no_bid) - 3})'))
      _skip_cnt["bid>=no_bid-3"] += 1
      continue
    if not (_cap >= row['position'] + row['resting_order_count'] + contracts):
      _rungs.append((i, bid_price, contracts, edge, 'SKIP',
                     f'position cap (would exceed {_cap})'))
      _skip_cnt["position_cap"] += 1
      continue

    client_oid = str(uuid.uuid4())
    order_params = {'ticker': row['market_ticker'],
                    'client_order_id': client_oid,
                    'type': 'limit',
                    'action': 'buy',
                    'side': 'no',
                    'count': contracts,
                    'no_price': int(bid_price),
                    'expiration_ts': _exp_ts,
                    'post_only': True}

    if DRY_RUN:
      _rungs.append((i, bid_price, contracts, edge, '✓', 'DRY RUN (not placed)'))
      _placed_rungs.append((int(bid_price), contracts, edge))
      _run_placed_contracts += contracts
      row['resting_order_count'] = row['resting_order_count'] + contracts
      continue

    # Server-side cap check right before placement (fail closed on error)
    try:
      _live_pos_resp = exchange_client.get_positions(ticker=row['market_ticker'])
      _live_mps = _live_pos_resp.get('market_positions', []) if isinstance(_live_pos_resp, dict) else []
      _live_active = [p for p in _live_mps if p.get('ticker') == row['market_ticker'] and p.get('position', 0) != 0]
      _live_pos = abs(_live_active[0].get('position', 0)) if _live_active else 0

      _live_orders = fetch_all_orders(row['market_ticker'])
      _live_resting = 0.0
      for _lo in _live_orders:
        _rem = _lo.get('remaining_count')
        if _rem is None:
          if _lo.get('status', '') not in ('resting', 'partial_filled', 'partially_filled', 'pending'):
            continue
          try:
            _rem = max(0.0, float(_lo.get('count', 0)) - float(_lo.get('filled_count', 0)))
          except (TypeError, ValueError):
            _rem = 0.0
        _live_resting += float(_rem)

      _signal_live = _live_pos + _live_resting
      _signal_local = _initial_position + _initial_resting + _run_placed_contracts
      _effective_total = max(_signal_live, _signal_local)

      row['position'] = _live_pos
      row['resting_order_count'] = max(_effective_total - _live_pos, 0)

      if _effective_total + contracts > _cap:
        _rungs.append((i, bid_price, contracts, edge, 'SKIP',
                       f'live cap (live={_signal_live}'
                       f' [pos={_live_pos}+resting={_live_resting}],'
                       f' local={_signal_local}'
                       f' [init_pos={_initial_position}+init_rest={_initial_resting}'
                       f'+run={_run_placed_contracts}],'
                       f' eff={_effective_total}+new={contracts}>{_cap})'))
        _skip_cnt["position_cap"] += 1
        continue
    except Exception as _ce:
      # If the cap-check reads fail because the exchange is paused/
      # unavailable (the same maintenance window that blocks placement),
      # short-circuit this market instead of failing the check 8× per
      # market (94 CAP_CHECK_FAILED alerts in the 2026-07-23 02:17 run).
      if _is_pause_error(str(_ce)):
          if row['market_ticker'] not in _PAUSED_MARKETS:
              alert("EXCHANGE_PAUSED",
                    f"{row['market_ticker']}: cap-check reads paused/unavailable — market skipped",
                    {"market": row['market_ticker'], "detail": str(_ce)[:160]})
          _PAUSED_MARKETS.add(row['market_ticker'])
          _rungs.append((i, bid_price, contracts, edge, 'SKIP', 'EXCHANGE_PAUSED (cap-check reads down)'))
          _skip_cnt["position_cap"] += 1
          break
      alert("CAP_CHECK_FAILED",
            f"Pre-placement cap check failed on {row['market_ticker']}; skipping rung to avoid leak",
            {"error": str(_ce)[:200], "rung_contracts": int(contracts), "rung_price": int(bid_price)})
      _rungs.append((i, bid_price, contracts, edge, 'SKIP', 'cap check API error (skipped)'))
      _skip_cnt["position_cap"] += 1
      continue

    try:
      _create_resp = exchange_client.create_order(**order_params)
      _kalshi_oid = ''
      if isinstance(_create_resp, dict):
          _kalshi_oid = (_create_resp.get('order') or {}).get('order_id', '') or ''
      time.sleep(0.1)
      all_order_records.append({
          'city': row['City'], 'forecast_date': row['Forecast Date'], 'run_date': row['Run Date'],
          'market_ticker': row['market_ticker'], 'contracts': contracts, 'no_price': int(bid_price),
          'city_abv': CITY_ABV.get(row['City'], ''), 'client_order_id': client_oid,
          'kalshi_order_id': _kalshi_oid,
          'expiration_ts': _exp_ts,
          'created_at': datetime.now(central_tz).strftime('%Y-%m-%d %H:%M:%S'),
          'run_id': RUN_ID,
          'strike_kind': row['strike_kind'],
          'effective_hi_no': float(_effective_hi_no),
          'hi_no_config': float(_hi_no_config),
          'fair_no_cents': round(float(_fair_no_cents), 2),
          'yes_prob': round(float(yes_prob), 4),
          'bias_correction_f': float(row.get('bias_correction_F') or 0.0),
      })
      row['resting_order_count'] = row['resting_order_count'] + contracts
      _run_placed_contracts += contracts
      orders_placed += 1
      level_orders += 1
      _rungs.append((i, bid_price, contracts, edge, '✓', 'placed'))
      _placed_rungs.append((int(bid_price), contracts, edge))
    except Exception as e:
      resp_body = ""
      if hasattr(e, 'response') and e.response is not None:
          try: resp_body = e.response.text[:300]
          except: pass
      if not resp_body and hasattr(e, 'args') and len(e.args) > 1:
          resp_body = str(e.args[1])[:300] if e.args[1] else ""
      if not resp_body:
          for attr in ('body', 'detail', 'message', 'reason'):
              val = getattr(e, attr, None)
              if val:
                  resp_body = str(val)[:300]
                  break
      if not resp_body and repr(e) != str(e):
          resp_body = repr(e)[:300]
      _err_str = f"{e} | {resp_body}" if resp_body else str(e)
      # Exchange pause / unavailable — stop laddering this market (every
      # remaining rung fails identically). One alert per market, not per rung.
      if _is_pause_error(_err_str):
          if row['market_ticker'] not in _PAUSED_MARKETS:
              alert("EXCHANGE_PAUSED",
                    f"{row['market_ticker']}: trading paused/unavailable — ladder short-circuited",
                    {"market": row['market_ticker'], "detail": _err_str[:160]})
          _PAUSED_MARKETS.add(row['market_ticker'])
          _rungs.append((i, bid_price, contracts, edge, '✗', 'EXCHANGE_PAUSED (ladder short-circuited)'))
          _skip_cnt["order_failed"] += 1
          break
      if '400' in str(e) and not _diag_printed:
          _diag_printed = True
          print(f"    [DIAG] Exception type: {type(e).__name__}, attrs: {[a for a in dir(e) if not a.startswith('_')]}")
          print(f"    [DIAG] args: {e.args}")
          print(f"    [DIAG] repr: {repr(e)[:500]}")
      if '400' in str(e):
          _fail_tag = f'ORDER_400: {_err_str[:80]}'
      elif '429' in str(e):
          # Rate limited — usually downstream of a pause-driven retry storm.
          # Back off THIS market's ladder too rather than deepening it.
          alert("ORDER_429", f"Rate limited on {row['market_ticker']} — ladder backed off",
                {"error": _err_str[:300]})
          _rungs.append((i, bid_price, contracts, edge, '✗', 'ORDER_429: rate limited (ladder backed off)'))
          _skip_cnt["order_failed"] += 1
          time.sleep(0.5)
          break
      else:
          alert("ORDER_FAILED", f"{row['market_ticker']} @{int(bid_price)}c: {_err_str[:200]}")
          _fail_tag = f'ORDER_FAILED: {_err_str[:60]}'
      _rungs.append((i, bid_price, contracts, edge, '✗', _fail_tag))
      _skip_cnt["order_failed"] += 1
      time.sleep(0.2)
    if bid_price <= 1:
      break

  # Render ladder rows
  for _r_i, _r_bid, _r_c, _r_edge, _r_st, _r_rs in _rungs:
    if _r_st == '✓':
      _status_fmt = f'✓ {_r_rs}' if _r_rs != 'placed' else '✓ placed'
    elif _r_st == '✗':
      _status_fmt = f'✗ {_r_rs}'
    else:
      _status_fmt = f'SKIP {_r_rs}'
    print(f"      i={_r_i}  {int(_r_bid):>3d}c × {_r_c:>4d}  "
          f"edge={_r_edge*100:+5.1f}%  — {_status_fmt}")

  if _placed_rungs:
    _top5 = _placed_rungs[:5]
    _formatted = [f"{p}c({c},ev={e*100:+.0f}%)" for p, c, e in _top5]
    if len(_placed_rungs) > 5:
      _formatted.append(f"... +{len(_placed_rungs)-5} more")
    _sum_placed = sum(c for _, c, _ in _placed_rungs)
    _n_skipped = len(_rungs) - len(_placed_rungs)
    print(f"    → NO: {_formatted} (placed: {len(_placed_rungs)} rungs / "
          f"{_sum_placed} contracts, skipped: {_n_skipped})")
  else:
    if len(_rungs) == 0:
      _reason = "ladder never entered (unexpected)"
    elif _skip_cnt["bid>=no_offer"] == len(_rungs):
      _reason = "entire ladder crosses the offer (book entirely above ladder top)"
    elif _skip_cnt["bid>=no_bid-3"] == len(_rungs):
      _reason = "no rung is ≥4c below current NO bid (market too tight for maker)"
    elif _skip_cnt["bid>=no_offer"] + _skip_cnt["bid>=no_bid-3"] == len(_rungs):
      _reason = "every rung either crosses offer or fails the 4c maker buffer"
    elif _skip_cnt["position_cap"] > 0 and _skip_cnt["order_failed"] == 0:
      _reason = "position cap reached"
    elif _skip_cnt["order_failed"] > 0:
      _reason = f"all attempts failed ({_skip_cnt})"
    else:
      _reason = f"mixed skips: {_skip_cnt}"
    print(f"    → NO: [no orders placed — {_reason}]")

print(f"\n{'='*60}")
print(f"TOTAL ORDERS PLACED: {orders_placed}" + (" (DRY RUN — none sent)" if DRY_RUN else ""))
if _PAUSED_MARKETS:
    _aborted = len(_PAUSED_MARKETS) >= EXCHANGE_PAUSE_ABORT_THRESHOLD
    print(f"⏸ EXCHANGE PAUSE: {len(_PAUSED_MARKETS)} market(s) paused/unavailable"
          + (" — run ABORTED (likely Kalshi maintenance window)" if _aborted
             else " — those markets skipped, rest quoted normally"))
print(f"{'='*60}")

# Write all orders to BigQuery. Explicit schema for the same reason as
# _RUNS_SCHEMA: the first live write CREATES KXLOW_orders, and autodetect
# would type the datetime64 columns as INT64 nanoseconds (2026-05-08
# incident pattern) — silently, since the load itself succeeds.
_ORDERS_SCHEMA = [
    bigquery.SchemaField("city", "STRING"),
    bigquery.SchemaField("forecast_date", "DATE"),
    bigquery.SchemaField("run_date", "TIMESTAMP"),
    bigquery.SchemaField("market_ticker", "STRING"),
    bigquery.SchemaField("contracts", "INTEGER"),
    bigquery.SchemaField("no_price", "INTEGER"),
    bigquery.SchemaField("city_abv", "STRING"),
    bigquery.SchemaField("client_order_id", "STRING"),
    bigquery.SchemaField("kalshi_order_id", "STRING"),
    bigquery.SchemaField("expiration_ts", "INTEGER"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("run_id", "STRING"),
    bigquery.SchemaField("strike_kind", "STRING"),
    bigquery.SchemaField("effective_hi_no", "FLOAT"),
    bigquery.SchemaField("hi_no_config", "FLOAT"),
    bigquery.SchemaField("fair_no_cents", "FLOAT"),
    bigquery.SchemaField("yes_prob", "FLOAT"),
    bigquery.SchemaField("bias_correction_f", "FLOAT"),
]
if all_order_records:
    df_orders = pd.DataFrame(all_order_records)
    df_orders['forecast_date'] = pd.to_datetime(df_orders['forecast_date'], errors="coerce").dt.date
    df_orders['run_date'] = pd.to_datetime(df_orders['run_date'], errors="coerce")
    df_orders['created_at'] = pd.to_datetime(df_orders['created_at'], errors="coerce")
    df_orders = df_orders[[f.name for f in _ORDERS_SCHEMA]]
    write_to_bq(df_orders, "orders", "WRITE_APPEND", schema=_ORDERS_SCHEMA)

print("\nTrading run complete.")

# =====================================================================
# END-OF-RUN ALERT SUMMARY
# =====================================================================
if _ALERTS:
    print(f"\n{'='*60}")
    print(f"⚡ ALERT SUMMARY: {len(_ALERTS)} alerts")
    print(f"{'='*60}")
    _cats = {}
    for _a in _ALERTS:
        _c = _a['category']
        _cats[_c] = _cats.get(_c, 0) + 1
    for _cat, _count in sorted(_cats.items(), key=lambda x: -x[1]):
        print(f"  {_cat}: {_count}")
    upload_alerts_to_bq()
    send_alert_notification()
    print(f"{'='*60}\n")
else:
    print("\n✓ No alerts — clean run")

# =====================================================================
# RUN-END marker for KXLOW_runs
# =====================================================================
try:
    _finished_at = datetime.now(pytz.UTC)
    _duration_s = (_finished_at - RUN_STARTED_AT).total_seconds()
    write_run_row(
        "end",
        finished_at=_finished_at,
        duration_seconds=float(_duration_s),
        n_orders_placed=int(orders_placed),
        n_orders_in_records=int(len(all_order_records)),
        n_alerts_emitted=int(len(_ALERTS)),
        exit_status="success",
    )
except Exception as _re:
    print(f"  RUNS end-row write failed: {_re}")

# =====================================================================
# RUN SUMMARY (markdown, for GitHub Actions step summary)
# =====================================================================
try:
    import datetime as _dt
    _lines = ["# Low-Temp Trading Run", ""]
    _lines.append(f"- Finished: `{_dt.datetime.utcnow().isoformat(timespec='seconds')}Z`")
    _lines.append(f"- Mode: **{'DAY-OF' if is_day_of_run else 'EVENING'}** → target {target_date}")
    _lines.append(f"- Orders placed: **{orders_placed}**" + (" _(dry run)_" if DRY_RUN else ""))
    _order_recs = locals().get('all_order_records') or []
    if _order_recs:
        _by_city = {}
        for _r in _order_recs:
            _k = _r.get('city') or '?'
            _by_city[_k] = _by_city.get(_k, 0) + 1
        _lines.append("")
        _lines.append("## Orders by city")
        for _c, _n in sorted(_by_city.items(), key=lambda x: -x[1]):
            _lines.append(f"- {_c}: {_n}")
    if FORECAST_SHAPE_SKIP or PRE_TRADE_SKIP_CITIES or EXPIRY_PAST_SKIP:
        _lines.append("")
        _lines.append("## Cities skipped")
        for _c in sorted(EXPIRY_PAST_SKIP):
            _lines.append(f"- {_c}: quote stop already passed")
        for _c in sorted(PRE_TRADE_SKIP_CITIES):
            _lines.append(f"- {_c}: {FORECAST_SHAPE_SKIP.get(_c, 'obs filter')}")
    if _PAUSED_MARKETS:
        _lines.append("")
        _aborted = len(_PAUSED_MARKETS) >= EXCHANGE_PAUSE_ABORT_THRESHOLD
        _lines.append(f"## Exchange pause — {len(_PAUSED_MARKETS)} market(s)"
                      + (" (run ABORTED)" if _aborted else ""))
        _lines.append("Likely Kalshi overnight maintenance window; the next scheduled run re-quotes.")
        for _m in sorted(_PAUSED_MARKETS):
            _lines.append(f"- {_m}")
    _lines.append("")
    _lines.append("## Alerts")
    if _ALERTS:
        _cats2 = {}
        for _a in _ALERTS:
            _cats2[_a['category']] = _cats2.get(_a['category'], 0) + 1
        for _cat, _count in sorted(_cats2.items(), key=lambda x: -x[1]):
            _lines.append(f"- `{_cat}`: {_count}")
    else:
        _lines.append("- None — clean run")
    with open("run_summary.md", "w", encoding="utf-8") as _f:
        _f.write("\n".join(_lines) + "\n")
    print("\n[SUMMARY] Wrote run_summary.md")
except Exception as _e:
    print(f"[SUMMARY] Failed to write run_summary.md: {_e}")
