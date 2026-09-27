#!/usr/bin/env python3
"""Kalshi market-data reads for collectors and ad-hoc scripts, SIGNED first.

Kalshi throttles UNSIGNED list reads -- GET /markets?series_ticker= /
event_ticker= / tickers=, /events?... -- from ANY IP, whatever this box
sends (measured 2026-09-27: ~30% of the unsigned list reads that missed
Kalshi's CDN cache got a 429, a second IP got 6/6, and a lone call got one
with nothing else running). The same read signed with the account key
passed 20/20 on the first try, in 25-55 ms against 0.7-1.1 s. Unsigned
single-market and order-book reads are mostly spared, so those can go
public first and leave the signed budget alone.

    import sys; sys.path.insert(0, "C:/Users/jackd/Documents/KL")
    from kalshi_reads import kalshi_get, kalshi_get_all
    js = kalshi_get("/markets", {"series_ticker": "KXFOO", "status": "open"})
    ms = kalshi_get_all("/markets", {"series_ticker": "KXFOO"}, items_key="markets")
    ob = kalshi_get("/markets/KXFOO-26SEP28-T1/orderbook", prefer="public")

CLI, instead of curl against the public host (prints JSON, ASCII-safe). The
leading slash is optional; Git Bash turns "/markets" into a Windows path,
and the CLI undoes that:
    python kalshi_reads.py markets series_ticker=KXFOO status=open limit=1000
    python kalshi_reads.py markets series_ticker=KXFOO --all markets
    python kalshi_reads.py markets/KXFOO-26SEP28-T1/orderbook --public

Signed reads spend the ACCOUNT's token bucket (Advanced: 300 read tokens/s,
10 per read), which every live bot shares. So signed reads are paced to one
per KALSHI_READS_MIN_GAP seconds per process (default 0.25, at most 4/s);
bulk single-market or order-book scans should pass prefer="public". The key
is the bots' key (KALSHI_PRIVATE_KEY / KALSHI_PRIVATE_KEY_PATH /
Lisa_Kalshi.txt), loaded from disk and never printed. Read-only.
"""

import argparse
import base64
import json
import os
import random
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import requests

BASE = os.environ.get("KALSHI_READS_BASE",
                      "https://api.elections.kalshi.com/trade-api/v2")
KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
LOCAL_KEY_DEFAULT = "C:/Users/jackd/Downloads/Lisa_Kalshi.txt"
TIMEOUT = 20.0
SIGNED_MIN_GAP = float(os.environ.get("KALSHI_READS_MIN_GAP", "0.25"))
RETRYABLE = (429, 500, 502, 503, 504)
TERMINAL = (400, 404)                 # the other route would fail the same way

# reads by the route that answered, for a collector's own log line
STATS: Dict[str, int] = {"signed": 0, "public": 0, "fallback": 0, "failed": 0}

_client_lock = threading.Lock()
_pace_lock = threading.Lock()
_client: Any = None
_last_signed = 0.0


class KalshiReadError(RuntimeError):
    """Every route failed; the message carries each route's error."""

    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status


class PublicHTTPError(RuntimeError):
    def __init__(self, status: int, text: str = ""):
        super().__init__(f"HTTP {status} {text[:80]}".strip())
        self.status = status


def load_private_key():
    """The bots' key, found the way incentive_mm.load_private_key finds it."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    pem_b64 = os.environ.get("KALSHI_PRIVATE_KEY")
    if pem_b64:
        try:
            pem = base64.b64decode(pem_b64)
        except Exception:
            pem = pem_b64.encode()
        return serialization.load_pem_private_key(pem, password=None,
                                                  backend=default_backend())
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
    if not os.path.exists(path):
        if not os.path.exists(LOCAL_KEY_DEFAULT):
            raise FileNotFoundError(
                f"no Kalshi private key: set KALSHI_PRIVATE_KEY (b64) or "
                f"place the PEM at {path!r}")
        path = LOCAL_KEY_DEFAULT
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None,
                                                  backend=default_backend())


def signed_client():
    """This process's signed client: the bots' ExchangeClient with its
    keep-alive session, built on first use (no network call to build). It
    retries a 429 / 5xx itself, at 1s and 3s."""
    global _client
    with _client_lock:
        if _client is None:
            os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")
            from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient
            _client = ExchangeClient(exchange_api_base=BASE, key_id=KEY_ID,
                                     private_key=load_private_key())
        return _client


def _pace_signed() -> None:
    global _last_signed
    with _pace_lock:
        wait = _last_signed + SIGNED_MIN_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_signed = time.monotonic()


def _public(path: str, params: dict, timeout: float, retries: int,
            backoff: float = 2.0):
    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(BASE + path, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = PublicHTTPError(r.status_code, r.text or "")
            if r.status_code not in RETRYABLE:
                break
        except requests.RequestException as e:
            last = e
        if attempt < retries:
            time.sleep(backoff * (attempt + 1) + random.uniform(0, backoff))
    raise last if last else RuntimeError(f"GET {path} failed")


def kalshi_get(path: str, params: Optional[dict] = None, prefer: str = "signed",
               timeout: float = TIMEOUT) -> Any:
    """GET BASE + path with query `params`. prefer="signed" (list reads, the
    default): the signed client, then the public endpoint (one retry after a
    jittered pause). prefer="public" (order books, single markets): one
    public try, then signed on a 429 / 5xx / network error. A 400 or 404 is
    final on either route. Raises KalshiReadError when every route fails."""
    if not path.startswith("/"):
        raise ValueError(f"path must start with '/': {path!r}")
    if prefer not in ("signed", "public"):
        raise ValueError(f"prefer must be 'signed' or 'public': {prefer!r}")
    params = dict(params or {})
    routes = ("signed", "public") if prefer == "signed" else ("public", "signed")
    errors: List[str] = []
    status: Optional[int] = None
    for i, route in enumerate(routes):
        try:
            if route == "signed":
                client = signed_client()
                _pace_signed()
                js = client.get(path, dict(params))
            else:
                js = _public(path, dict(params), timeout, retries=0 if i == 0 else 1)
        except Exception as e:
            errors.append(f"{route}: {type(e).__name__}: {str(e)[:120]}")
            s = getattr(e, "status", None)
            status = s if isinstance(s, int) else status
            if status in TERMINAL:
                break
            continue
        STATS[route] += 1
        if i:
            STATS["fallback"] += 1
        return js
    STATS["failed"] += 1
    raise KalshiReadError("; ".join(errors), status=status)


def kalshi_get_all(path: str, params: Optional[dict] = None,
                   items_key: str = "markets", prefer: str = "signed",
                   max_pages: int = 100, pace: float = 0.2) -> List[Any]:
    """Every page of a cursor-paged list (markets, events, series, trades):
    the pages' `items_key` lists, concatenated. Raises on a failed page."""
    out: List[Any] = []
    cursor = None
    for _ in range(max_pages):
        p = dict(params or {})
        if cursor:
            p["cursor"] = cursor
        js = kalshi_get(path, p, prefer=prefer)
        items = js.get(items_key) or []
        out += items
        cursor = js.get("cursor")
        if not cursor or not items:
            break
        time.sleep(pace)
    return out


def cli_path(arg: str) -> str:
    """The API path from a CLI argument: 'markets' or '/markets', or the
    Windows path Git Bash (MSYS) makes of '/markets' ('C:/Program Files/Git/
    markets')."""
    m = re.match(r"^[A-Za-z]:[/\\].*?[/\\]Git([/\\].*)$", arg)
    if m:
        arg = m.group(1)
    arg = arg.replace("\\", "/")
    return arg if arg.startswith("/") else "/" + arg


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Kalshi GET, signed first (the list endpoints' 429s); prints JSON.")
    ap.add_argument("path", type=cli_path,
                    help="path under /trade-api/v2, e.g. markets or markets/<ticker>/orderbook")
    ap.add_argument("params", nargs="*", help="query parameters as key=value")
    ap.add_argument("--public", action="store_true",
                    help="public first, signed on a 429 (order books, single markets)")
    ap.add_argument("--all", metavar="ITEMS_KEY",
                    help="follow the cursor and print the concatenated ITEMS_KEY lists")
    a = ap.parse_args(argv)
    params: Dict[str, str] = {}
    for kv in a.params:
        if "=" not in kv:
            ap.error(f"parameter {kv!r} is not key=value")
        k, v = kv.split("=", 1)
        params[k] = v
    prefer = "public" if a.public else "signed"
    try:
        if a.all:
            out = kalshi_get_all(a.path, params, items_key=a.all, prefer=prefer)
        else:
            out = kalshi_get(a.path, params, prefer=prefer)
    except KalshiReadError as e:
        print(f"kalshi_reads: {e}", file=sys.stderr)
        return 1
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
