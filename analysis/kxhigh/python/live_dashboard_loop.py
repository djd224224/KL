"""Run the live KXHIGH dashboard build on a loop.

Each iteration: re-queries BigQuery and rewrites
`analysis/kxhigh/output/live_dashboard.html`. The HTML's meta-refresh tag
makes any open browser tab pick up the regenerated file.

Pairs with the GitHub-Actions orderbook-logger cron (every 30 min) — the
loop re-renders shortly after each upstream poll lands, so a typical lag
from "Kalshi orderbook moved" → "you see it" is interval_seconds + a few
seconds of BQ latency.

Usage
-----
    python analysis/kxhigh/python/live_dashboard_loop.py
    python analysis/kxhigh/python/live_dashboard_loop.py --interval-seconds 60
    python analysis/kxhigh/python/live_dashboard_loop.py --no-open

Stop with Ctrl+C. Transient BigQuery errors don't kill the loop; the next
tick retries.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

# Windows default console codec (cp1252) chokes on the unicode chars our
# build-script logs (e.g., "…"). Force UTF-8 on stdout/stderr so the loop
# stays alive across rebuilds.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root for orderbook_logger
import live_dashboard as L  # noqa: E402

# Optional data-ingest hooks. Imported lazily so the loop still runs on
# machines without Kalshi auth set up — render-only mode works fine.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    import kxhigh_orderbook_logger as KX  # noqa: E402
except Exception as _e:
    KX = None
    print(f"[loop] orderbook_logger not importable ({_e}); ingest disabled", flush=True)


def _build_kalshi_client():
    """Try to construct a Kalshi ExchangeClient. Returns None if no auth."""
    if KX is None:
        return None
    # Default to the documented PEM path on this machine if the env var
    # isn't set. Avoids the user needing to babysit env between sessions.
    if not os.environ.get("KALSHI_PRIVATE_KEY_PATH") and not os.environ.get("KALSHI_PRIVATE_KEY"):
        default_pem = Path("C:/Users/jackd/Downloads/Lisa_Kalshi.txt")
        if default_pem.exists():
            os.environ["KALSHI_PRIVATE_KEY_PATH"] = str(default_pem)
    try:
        from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient  # noqa: E402
        pk = KX._load_private_key()
        return ExchangeClient(
            exchange_api_base=KX.KALSHI_API_BASE,
            key_id=KX.KEY_ID,
            private_key=pk,
        )
    except Exception as e:
        print(f"[loop] Kalshi client unavailable ({e}); positions polling disabled", flush=True)
        return None


def _with_timeout(fn, *args, timeout_s: int = 90, label: str = "fn"):
    """Run `fn(*args)` on a worker thread, kill it if it doesn't finish in
    `timeout_s`. ExchangeClient's HTTP calls don't propagate timeouts, so a
    flaky Kalshi response can hang the loop indefinitely (this happened
    once — single iteration took 4.5 hours). Wrapping every ingest helper
    here puts a hard ceiling on each call so the loop keeps moving.
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, *args)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            print(f"[loop] {label} TIMED OUT after {timeout_s}s", flush=True)
            return None


def _ingest(bq, ex, *, do_forecast: bool) -> None:
    """One ingest tick: NWS obs (always), positions (if Kalshi auth),
    hourly forecast (only every other tick — NWS updates hourly, our cron
    is 30 min, so polling forecast every tick wastes API calls).

    Each helper runs under a hard timeout so a hung HTTP call doesn't take
    the whole loop down.
    """
    if KX is None:
        return
    poll_ts = datetime.now(timezone.utc)
    try:
        _with_timeout(KX.poll_and_write_nws_obs, bq, poll_ts,
                      timeout_s=60, label="nws-obs")
    except Exception as e:
        print(f"[loop] obs poll failed: {e}", flush=True)
    if ex is not None:
        try:
            _with_timeout(KX.poll_and_write_positions, bq, ex, poll_ts,
                          timeout_s=60, label="positions")
        except Exception as e:
            print(f"[loop] positions poll failed: {e}", flush=True)
        try:
            _with_timeout(KX.poll_and_write_fills, bq, ex, poll_ts,
                          timeout_s=90, label="fills")
        except Exception as e:
            print(f"[loop] fills poll failed: {e}", flush=True)
    if do_forecast:
        try:
            _with_timeout(KX.poll_and_write_hourly_forecast, bq, poll_ts,
                          timeout_s=180, label="hourly-forecast")
        except Exception as e:
            print(f"[loop] hourly-forecast poll failed: {e}", flush=True)


def main() -> None:
    # Default cadence matches the upstream cron in run_kxhigh_orderbook_logger.yml
    # (every 20 min). All four live tables — orderbook, NWS obs, NWS hourly
    # forecast, positions — refresh on that cron, so rebuilding faster than
    # 20 min just re-renders the same data. The trading-bot snapshot updates
    # only 4×/day (02/10/11/23 UTC) — slower still, but its values are baked
    # into the snapshot table that the orderbook cron reads regardless.
    DEFAULT_INTERVAL = 20 * 60

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL,
                    help=f"seconds to sleep between rebuilds (default: {DEFAULT_INTERVAL} = 20 min, matching upstream cron)")
    ap.add_argument("--no-open", action="store_true",
                    help="skip the initial browser launch")
    args = ap.parse_args()

    print(
        f"[loop] starting; rebuilding every {args.interval_seconds}s. "
        f"Ctrl+C to stop.", flush=True,
    )

    # Build BQ + Kalshi clients once; reuse across ticks. If Kalshi auth
    # isn't available, we still ingest obs (public NWS API) and render.
    bq = ex = None
    if KX is not None:
        try:
            from google.cloud import bigquery
            bq = bigquery.Client(project=KX.PROJECT)
        except Exception as e:
            print(f"[loop] BQ client init failed ({e}); ingest disabled", flush=True)
        ex = _build_kalshi_client() if bq is not None else None

    first = True
    iteration = 0
    while True:
        t0 = datetime.now()
        if bq is not None:
            # Forecast polling is hourly-meaningful; do it every other tick.
            _ingest(bq, ex, do_forecast=(iteration % 2 == 0))
        try:
            output_path = L.main()
        except KeyboardInterrupt:
            print("[loop] stopping", flush=True)
            return
        except Exception as e:
            print(f"[loop] {t0:%H:%M:%S} build failed: {e}", flush=True)
            traceback.print_exc()
        else:
            elapsed = (datetime.now() - t0).total_seconds()
            print(
                f"[loop] {t0:%H:%M:%S} ok ({elapsed:.1f}s) -> {output_path}",
                flush=True,
            )
            if first and not args.no_open:
                webbrowser.open(f"file:///{output_path.as_posix()}")
                first = False
        iteration += 1
        try:
            time.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            print("[loop] stopping", flush=True)
            return


if __name__ == "__main__":
    main()
