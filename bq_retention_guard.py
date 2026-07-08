"""BigQuery retention guard + fills archiver. Runs daily via GitHub Actions.

Born out of the 2026-07-07 incident: the Kalshi dataset carried a 60-day
default table expiration that silently deleted KXHIGH_market_snapshot,
KXHIGH_orders and KXNBAMENTION_bankroll_log (and, on 2026-05-08 / 05-24,
two months of KXHIGH model calls and most of the MLB bot's order history).
Policy after the cleanup: retention limits are FINE when chosen on purpose,
but nothing may disappear silently. This script enforces that:

  1. ARCHIVE  — Kalshi's fills endpoint only returns ~65 days, and the
     nightly monitoring jobs TRUNCATE + full-re-pull the *_fills tables,
     so fill history silently rolls off. Each run MERGEs the current
     *_fills tables into append-only *_fills_archive tables (keyed on
     fill_id, which is verified 100% populated + unique).
  2. EXPIRATION AUDIT — alert if the dataset default expirations come
     back, or if any table/view carries an expiration that isn't
     declared in ALLOWED_EXPIRATION_RE.
  3. GENERATION GUARD — alert if a base table is dropped + recreated
     (creation_time changed = the old generation's data is gone) or
     disappears entirely. Tracked in bq_guard_table_registry.

On violations: prints a report, best-effort email via the same Gmail SMTP
path the trading bots use, and exits 1 so the GitHub Actions failure
notification acts as the backstop alert channel.

To retire data ON PURPOSE: set the expiration / drop the table, then add
the name to ALLOWED_EXPIRATION_RE or EXPECTED_RECREATE below — the guard
alerts exactly once on anything it wasn't told about.
"""
from __future__ import annotations

import os
import re
import smtplib
import sys
from email.mime.text import MIMEText

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

PROJECT = os.environ.get("BQ_PROJECT", "elite-contact-446323-q7")
DATASET = os.environ.get("BQ_DATASET", "Kalshi")
LOCATION = "northamerica-northeast1"

ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")

# source fills table -> append-only archive (schema mirrors source + archived_at)
FILLS_ARCHIVES = {
    "KXHIGH_fills": "KXHIGH_fills_archive",
    "KXMLBMENTION_fills": "KXMLBMENTION_fills_archive",
    "KXNBAMENTION_fills": "KXNBAMENTION_fills_archive",
}

# Tables that are dropped + rebuilt from source on a schedule by design
# (high_temp_monitoring recreates these nightly from the Kalshi API).
# A creation_time change on these is normal, not data loss.
EXPECTED_RECREATE = {"KXHIGH_fills", "KXHIGH_settlements"}

# Objects allowed to carry an expiration: deliberate self-cleaning copies.
# The 2026-07-07 restore backups match this on purpose — they self-delete
# in September and must not page anyone when they do (they're also excluded
# from the generation registry for the same reason).
ALLOWED_EXPIRATION_RE = re.compile(r"(_pre_expiry_|_backup_)")

REGISTRY_TABLE = "bq_guard_table_registry"


def _q(client: bigquery.Client, sql: str) -> bigquery.table.RowIterator:
    return client.query(sql, location=LOCATION).result()


def archive_fills(client: bigquery.Client, violations: list) -> None:
    for src, dst in FILLS_ARCHIVES.items():
        src_id = f"{PROJECT}.{DATASET}.{src}"
        dst_id = f"{PROJECT}.{DATASET}.{dst}"
        try:
            src_schema = client.get_table(src_id).schema
        except NotFound:
            violations.append(f"ARCHIVE: source table {src} not found")
            continue
        try:
            dst_table = client.get_table(dst_id)
        except NotFound:
            # First run on a fresh setup: seed the archive from the source.
            _q(client, f"CREATE TABLE `{dst_id}` AS "
                       f"SELECT *, CURRENT_TIMESTAMP() AS archived_at FROM `{src_id}`")
            n = list(_q(client, f"SELECT COUNT(*) n FROM `{dst_id}`"))[0].n
            print(f"  ARCHIVE: created {dst} seeded with {n} rows")
            continue

        # Self-heal schema drift: bots add columns via ALLOW_FIELD_ADDITION,
        # so mirror any new source column onto the archive before merging.
        dst_names = {f.name for f in dst_table.schema}
        missing = [f for f in src_schema if f.name not in dst_names]
        if missing:
            dst_table.schema = list(dst_table.schema) + [
                bigquery.SchemaField(f.name, f.field_type, mode="NULLABLE") for f in missing
            ]
            client.update_table(dst_table, ["schema"])
            print(f"  ARCHIVE: {dst} schema extended with {[f.name for f in missing]}")

        cols = ", ".join(f"`{f.name}`" for f in src_schema)
        src_cols = ", ".join(f"S.`{f.name}`" for f in src_schema)
        job = client.query(f"""
            MERGE `{dst_id}` T
            USING (
              SELECT * FROM `{src_id}`
              WHERE fill_id IS NOT NULL AND fill_id != ''
              QUALIFY ROW_NUMBER() OVER (PARTITION BY fill_id) = 1
            ) S
            ON T.fill_id = S.fill_id
            WHEN NOT MATCHED THEN
              INSERT ({cols}, archived_at) VALUES ({src_cols}, CURRENT_TIMESTAMP())
        """, location=LOCATION)
        job.result()
        print(f"  ARCHIVE: {src} -> {dst}: +{job.num_dml_affected_rows} new fills")

        gap = list(_q(client, f"""
            SELECT COUNT(*) n FROM `{src_id}` s
            LEFT JOIN `{dst_id}` d USING (fill_id)
            WHERE d.fill_id IS NULL AND s.fill_id IS NOT NULL AND s.fill_id != ''
        """))[0].n
        if gap:
            violations.append(f"ARCHIVE: {gap} fills in {src} missing from {dst} after merge")


def check_dataset_defaults(client: bigquery.Client, violations: list) -> None:
    ds = client.get_dataset(f"{PROJECT}.{DATASET}")
    if ds.default_table_expiration_ms:
        violations.append(
            f"DATASET: default_table_expiration_ms is set "
            f"({ds.default_table_expiration_ms}) — this is what caused the "
            f"2026-07-07 data loss; clear it or update the guard on purpose")
    if ds.default_partition_expiration_ms:
        violations.append(
            f"DATASET: default_partition_expiration_ms is set "
            f"({ds.default_partition_expiration_ms})")


def check_expirations(client: bigquery.Client, violations: list) -> None:
    rows = _q(client, f"""
        SELECT table_name, option_name, option_value
        FROM `{PROJECT}.{DATASET}.INFORMATION_SCHEMA.TABLE_OPTIONS`
        WHERE option_name IN ('expiration_timestamp', 'partition_expiration_days')
    """)
    for r in rows:
        if not ALLOWED_EXPIRATION_RE.search(r.table_name):
            violations.append(
                f"EXPIRATION: {r.table_name} has undeclared {r.option_name} = "
                f"{r.option_value} (data will silently self-delete)")


def check_generations(client: bigquery.Client, violations: list) -> None:
    reg_id = f"{PROJECT}.{DATASET}.{REGISTRY_TABLE}"
    _q(client, f"""
        CREATE TABLE IF NOT EXISTS `{reg_id}` (
          table_name STRING NOT NULL,
          creation_time TIMESTAMP,
          first_seen TIMESTAMP,
          last_seen TIMESTAMP
        )
    """)
    current_sql = f"""
        SELECT table_name, creation_time
        FROM `{PROJECT}.{DATASET}.INFORMATION_SCHEMA.TABLES`
        WHERE table_type = 'BASE TABLE'
          AND NOT REGEXP_CONTAINS(table_name, r'(_pre_expiry_|_backup_)')
    """
    diffs = _q(client, f"""
        WITH cur AS ({current_sql}),
             reg AS (SELECT table_name, creation_time FROM `{reg_id}`)
        SELECT
          COALESCE(c.table_name, r.table_name) AS table_name,
          CASE
            WHEN r.table_name IS NULL THEN 'new'
            WHEN c.table_name IS NULL THEN 'deleted'
            WHEN c.creation_time != r.creation_time THEN 'recreated'
          END AS status,
          r.creation_time AS old_ct,
          c.creation_time AS new_ct
        FROM cur c
        FULL OUTER JOIN reg r USING (table_name)
        WHERE c.table_name IS NULL OR r.table_name IS NULL
           OR c.creation_time != r.creation_time
    """)
    for d in diffs:
        if d.status == "new":
            print(f"  REGISTRY: new table {d.table_name} registered")
        elif d.status == "deleted":
            violations.append(
                f"GENERATION: {d.table_name} disappeared (was created {d.old_ct}) — "
                f"if deliberate, this alert fires once; the registry forgets it now")
        elif d.status == "recreated" and d.table_name not in EXPECTED_RECREATE:
            violations.append(
                f"GENERATION: {d.table_name} was dropped + recreated "
                f"({d.old_ct} -> {d.new_ct}) — the previous generation's data is "
                f"gone; recover via time travel NOW if unintended (7-day window)")

    # Alert-once semantics: sync the registry to reality regardless of
    # violations, so each event pages exactly one daily run.
    _q(client, f"""
        MERGE `{reg_id}` T
        USING ({current_sql}) S
        ON T.table_name = S.table_name
        WHEN MATCHED THEN UPDATE SET
          creation_time = S.creation_time, last_seen = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT (table_name, creation_time, first_seen, last_seen)
          VALUES (S.table_name, S.creation_time, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
        WHEN NOT MATCHED BY SOURCE THEN DELETE
    """)


def send_alert_email(violations: list) -> None:
    if not ALERT_EMAIL_FROM or not ALERT_EMAIL_PASSWORD or not ALERT_EMAIL_TO:
        print("  (email not configured; relying on workflow failure notification)")
        return
    body = "BQ retention guard violations:\n\n" + "\n".join(f"- {v}" for v in violations)
    for recipient in [r.strip() for r in ALERT_EMAIL_TO.split(",") if r.strip()]:
        try:
            msg = MIMEText(body)
            msg["Subject"] = f"BQ Guard: {len(violations)} violation(s)"
            msg["From"] = ALERT_EMAIL_FROM
            msg["To"] = recipient
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
                server.sendmail(ALERT_EMAIL_FROM, [recipient], msg.as_string())
            print(f"  alert email sent to {recipient}")
        except Exception as e:
            print(f"  WARN: alert email to {recipient} failed: {e}")


def main() -> None:
    client = bigquery.Client(project=PROJECT)
    violations: list = []

    print("1/4 archiving fills (Kalshi API only retains ~65 days)...")
    archive_fills(client, violations)
    print("2/4 checking dataset default expirations...")
    check_dataset_defaults(client, violations)
    print("3/4 checking per-table expirations...")
    check_expirations(client, violations)
    print("4/4 checking table generations (drop/recreate detection)...")
    check_generations(client, violations)

    if violations:
        print(f"\nFAIL: {len(violations)} violation(s):")
        for v in violations:
            print(f"  - {v}")
        send_alert_email(violations)
        sys.exit(1)
    print("\nOK: retention guard clean")


if __name__ == "__main__":
    main()
