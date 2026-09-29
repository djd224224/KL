"""Dry-run high_temp_trading.py against LIVE market data without trading.

The bot has no dry-run mode, so this runs the real script with every
mutation stubbed:
  - ExchangeClient.create_order / cancel_order are recorded, never sent
  - BigQuery load jobs are recorded, never sent (queries still run, unless
    the clock is shifted -- then the BQ client is stubbed wholesale, because
    google-auth would see every token as expired)
  - smtplib is blocked and the ALERT_EMAIL_* env vars are blanked
Kalshi and weather READS are real and use the account key.

--at shifts the SCRIPT's clock (not the Kalshi client's, so signed reads
still work) to exercise any run slot at any time of day:

    python analysis/kxhigh/python/dryrun_high_temp.py                        # as if run now
    python analysis/kxhigh/python/dryrun_high_temp.py --at "2026-09-29 20:02" # evening slot
    python analysis/kxhigh/python/dryrun_high_temp.py --at "2026-09-29 09:07" \
        --env KXHIGH_RUN_SLOT=west_late                                       # the late run

Run it from a scratch directory: the script writes run_summary.md to the cwd.
Prints one line per order it would have placed and writes a JSON summary
(--out). A full 24-city slot takes ~10-15 min locally (vs ~5 on Actions).
Built 2026-09-29 for the west late-morning test; see test_high_temp_run_slot.py
for the unit-level slot/cutoff checks.
"""
import argparse
import json
import os
import runpy
import sys
from datetime import datetime as REAL_DT, timedelta

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
LOCAL_KEY = "C:/Users/jackd/Downloads/Lisa_Kalshi.txt"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="", help='script clock, "YYYY-MM-DD HH:MM" US/Central')
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE for the script")
    ap.add_argument("--out", default="dryrun_summary.json")
    ap.add_argument("--repo", default=REPO, help="checkout whose high_temp_trading.py runs")
    args = ap.parse_args()

    sys.path.insert(0, args.repo)
    os.environ["KALSHI_PRIVATE_KEY"] = ""
    os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", LOCAL_KEY)
    for k in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD", "ALERT_EMAIL_TO"):
        os.environ[k] = ""
    for kv in args.env:
        k, _, v = kv.partition("=")
        os.environ[k] = v

    import pytz
    import pandas as pd
    import smtplib
    import KalshiClientsBaseV2ApiKey_FIXED as K  # binds the REAL datetime first
    from google.cloud import bigquery

    ct = pytz.timezone("US/Central")
    creates, cancels, loads = [], [], []

    def fake_create(self, **kw):
        creates.append(kw)
        return {"order": {"order_id": f"DRYRUN-{len(creates)}"}}

    def fake_cancel(self, **kw):
        cancels.append(kw)
        return {}

    K.ExchangeClient.create_order = fake_create
    K.ExchangeClient.cancel_order = fake_cancel

    class _Job:
        def result(self):
            return None

    def fake_load(self, df, table_id, job_config=None, **kw):
        loads.append((table_id, df.copy()))
        return _Job()

    bigquery.Client.load_table_from_dataframe = fake_load

    def _no_email(*a, **k):
        raise RuntimeError("email blocked in dry run")

    smtplib.SMTP_SSL = _no_email

    offset = timedelta(0)
    if args.at:
        offset = ct.localize(REAL_DT.strptime(args.at, "%Y-%m-%d %H:%M")) - REAL_DT.now(pytz.UTC)

        class _Empty:
            def to_dataframe(self):
                return pd.DataFrame()

            def result(self):
                return []

        class _Tbl:
            num_rows = -1

        class FakeBQ:
            def __init__(self, *a, **k):
                pass

            def query(self, *a, **k):
                return _Empty()

            def load_table_from_dataframe(self, df, table_id, job_config=None, **kw):
                loads.append((table_id, df.copy()))
                return _Job()

            def get_table(self, *a, **k):
                return _Tbl()

        bigquery.Client = FakeBQ

        import datetime as dtmod

        class FakeDT(dtmod.datetime):
            @classmethod
            def now(cls, tz=None):
                return REAL_DT.now(tz) + offset

            @classmethod
            def utcnow(cls):
                return REAL_DT.utcnow() + offset

        dtmod.datetime = FakeDT
        print(f"[dryrun] script clock {args.at} CT (offset {offset})")

    exit_code = 0
    try:
        runpy.run_path(os.path.join(args.repo, "high_temp_trading.py"), run_name="__main__")
    except SystemExit as e:
        exit_code = e.code or 0

    now_ts = (REAL_DT.now(pytz.UTC) + offset).timestamp()
    rows = []
    print(f"\n[dryrun] would place {len(creates)} orders, cancel {len(cancels)}:")
    for c in creates:
        exp = REAL_DT.fromtimestamp(c["expiration_ts"], pytz.UTC).astimezone(ct)
        rows.append({"ticker": c["ticker"], "count": c["count"], "no_price": c["no_price"],
                     "side": c.get("side"), "action": c.get("action"),
                     "post_only": c.get("post_only"),
                     "expiry_ct": exp.strftime("%Y-%m-%d %H:%M"),
                     "expiry_hours_from_now": round((c["expiration_ts"] - now_ts) / 3600, 2)})
        print(f"  {c['action']} {c['side']} {c['ticker']:30s} {c['count']:>4} @ {c['no_price']:>2}c"
              f"  expires {exp.strftime('%m-%d %H:%M')} CT")
    tables = {}
    for tid, df in loads:
        t = tables.setdefault(tid.split(".")[-1], {"rows": 0})
        t["rows"] += len(df)
        for col in ("run_slot", "exit_status", "city"):
            if col in df.columns:
                t.setdefault(col, sorted(set(map(str, df[col].dropna()))))
    with open(args.out, "w") as f:
        json.dump({"at": args.at or "now", "exit_code": exit_code, "orders": rows,
                   "n_cancels": len(cancels), "bq_writes": tables}, f, indent=1)
    print(f"[dryrun] exit={exit_code}; BigQuery writes (not sent): "
          f"{ {k: v['rows'] for k, v in tables.items()} }; summary -> {args.out}")


if __name__ == "__main__":
    main()
