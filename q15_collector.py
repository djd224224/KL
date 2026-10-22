#!/usr/bin/env python3
r"""Always-on recorder for the 15-minute shadow test (Q15_SHADOW.md; Jack
2026-10-22: "Yes use a live price feed like Miami temp, with recorder and a
week of testing").

Records Kalshi's paying 15-minute "price up in next 15 mins?" markets -- the
five FX pairs and the crypto race (Jack 2026-10-22: "It's paid, do FX and
crypto": the other series settle on Pyth, which sells its keys; they can be
added back with Q15_SHADOW_SERIES and Q15_PYTH=1) -- together with live
prices of their underlyings, so a shadow sim can replay quoting policies the way the
hourly-temp test did for KXTEMPMIAH (TEMP_SHADOW.md). READ-ONLY: it GETs and
subscribes to market data; it never places, amends or cancels an order, and
never imports incentive_mm.py.

It reuses temp_shadow_collector's WebSocket recorder, raw writer, REST
client, lock and signer, pointed at KL-data\q15-shadow: TEMP_SHADOW_DATA and
friends are set BEFORE that import, so its log, lock, status and raw files
land here, never in the Miami test's folder (main() refuses to run if not).

Threads (each catches everything and keeps going; log -> collector.log):
  ws     temp_shadow_collector.WSRecorder: `orderbook_delta` + `trade` for
         every market of Q15_SERIES from 60 s before its open to 10 min after
         its close -> raw/<UTC day>/ws_<HH>.jsonl, verbatim.
  px-*   the underlyings' live prices -> raw/<UTC day>/px_<HH>.jsonl: once a
         second, one row per source with the symbols that changed.
           kraken  public WS v2 ticker (bbo) for the five FX pairs -- a
                   proxy: the FX markets settle on Pyth
           hl      Hyperliquid's public allMids for BTC ETH SOL XRP HYPE -- a
                   proxy: the crypto race settles on CF Benchmarks (paid)
           pyth    OFF (Q15_PYTH=1 turns it on): the settlement source itself
                   over Hermes' SSE stream, with a key in PYTH_API_KEY or
                   KL-data\q15-shadow\pyth_api_key.txt -- Hermes refuses
                   unkeyed price reads since 2026-08-26, and the keys are paid
  rest   discovery (each series' upcoming markets, every 10 min -- Kalshi
         lists them hours ahead), each quarter-hour's opened markets at
         +20 s (strikes / rules), settlements from close + 3 min (retry every
         2 min, up to 1 h), incentive programs every 5 min -> meta stream and
         settled/<ticker>.json.
  main   heartbeat (collector_status.json every 10 s), flush, gzip of raw
         files >= 2 h old, HALT / Q15_SHADOW_UNTIL exit.

Exit codes as temp_shadow_collector: 0 --duration elapsed, 1 crash, 3 past
Q15_SHADOW_UNTIL or HALT (the launcher stops looping), 4 another live
collector holds collector.lock.

CLI:  python q15_collector.py [--duration S] [--echo]
"""
import os
import sys

DATA = os.environ.get("Q15_SHADOW_DATA", r"C:\Users\jackd\Documents\KL-data\q15-shadow")
START = os.environ.get("Q15_SHADOW_START", "2026-10-22T00:00:00Z")
UNTIL = os.environ.get("Q15_SHADOW_UNTIL", "2026-10-30T12:00:00Z")
# FX and the crypto race only (Jack 2026-10-22: "It's paid, do FX and
# crypto"): Pyth -- the settlement source of FX, the metals, natgas, Brent
# and the yields -- sells its keys, and only FX and the race have free live
# proxies (Kraken, Hyperliquid). Q15_SHADOW_SERIES widens the list again.
SERIES = tuple(s for s in os.environ.get(
    "Q15_SHADOW_SERIES",
    "KXEURUSD15M,KXGBPUSD15M,KXUSDJPY15M,KXAUDUSD15M,KXUSDCAD15M,"
    "KXCRYPTOLEAD15M").split(",") if s)
PYTH_ON = os.environ.get("Q15_PYTH", "0") == "1"      # paid; off unless asked for
# point temp_shadow_common / temp_shadow_collector at this test's folder
os.environ["TEMP_SHADOW_DATA"] = DATA
os.environ["TEMP_SHADOW_SERIES"] = ",".join(SERIES)
os.environ["TEMP_SHADOW_START"] = START
os.environ["TEMP_SHADOW_UNTIL"] = UNTIL

import argparse  # noqa: E402
import collections  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
import ssl  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from typing import Dict, Iterable, List, Optional, Tuple  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kalshi_ws  # noqa: E402
import temp_shadow_common as C  # noqa: E402
import temp_shadow_collector as T  # noqa: E402

# the raw writer's gzip pass knows ws / wx / meta; this test writes px too
T._RAW_NAME = re.compile(r"^(ws|px|wx|meta)_(\d\d)\.jsonl$")
log = T.log

EXIT_OK, EXIT_ERROR, EXIT_DONE, EXIT_LOCKED = T.EXIT_OK, T.EXIT_ERROR, T.EXIT_DONE, T.EXIT_LOCKED

# Pyth price feeds (the settlement sources named in each series' rules), ids
# from Hermes /v2/price_feeds (open without a key) on 2026-10-22.
PYTH_FEEDS: Dict[str, Tuple[str, str]] = {
    "EURUSD": ("FX.EUR/USD", "a995d00bb36a63cef7fd2c287dc105fc8f3d93779f062f09551b0af3e81ec30b"),
    "GBPUSD": ("FX.GBP/USD", "84c2dde9633d93d1bcad84e7dc41c9d56578b7ec52fabedc1f335d673df0a7c1"),
    "USDJPY": ("FX.USD/JPY", "ef2c98c804ba503c6a707e38be4dfbb16683775f195b091252bf24693042fd52"),
    "AUDUSD": ("FX.AUD/USD", "67a6f93030420c1c9e3fe37c1ab6b77966af82f995944a9fefce357a22854a80"),
    "USDCAD": ("FX.USD/CAD", "3112b03a41c910ed446852aacf67118cb1bec67b2cd0b9a214c58cc0eaa2ecca"),
    "BRENT": ("Commodities.Index.BRENT/USD",
              "f33ce961935076ef4dc98be75cf2126046eac1bffdcd7a0fa05ccf18b746fda6"),
    "NATGAS": ("Commodities.Index.NATGAS/USD",
               "45f95717fbe158e896415d1cea33814d73e54169022a8f7bcbb815ebef92f71c"),
    "COPPER": ("Commodities.Index.CU/USD",
               "b2b238aeb6ef5a722c5cf278595bf40434e174cac4f88c8ad5b6f5009b548c59"),
    "PLATINUM": ("Metal.XPT/USD", "398e4bbc7cbf89d6648c21e08019d878967677753b3096799595c78f805a34e5"),
    "PALLADIUM": ("Metal.XPD/USD", "80367e9664197f37d89a07a804dffd2101c479c7c4e8490501bc9d9e1e7f9021"),
    "US2Y": ("InterestRate.US2Y", "7d01ec0cb9d38918cd497f97b24cb6cea5552993f5da05863886e6a2d33ee0aa"),
    "US5Y": ("InterestRate.US5Y", "7d220b081152db0d74a93d3ce383c61d0ec5250c6dd2b2cdb2d1e4b8919e1a6e"),
    "US10Y": ("InterestRate.US10Y", "9c196541230ba421baa2a499214564312a46bb47fb6b61ef63db2f70d3ce34c1"),
    "US30Y": ("InterestRate.US30Y", "424c69939fc52a459f6dde758d80f74a3628c0e9b48bfc142df1270c9b9131be"),
    "BTC": ("Crypto.BTC/USD", "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"),
    "ETH": ("Crypto.ETH/USD", "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace"),
    "SOL": ("Crypto.SOL/USD", "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d"),
    "XRP": ("Crypto.XRP/USD", "ec5d399846a9209f3fe5881d70aae9268c94339ff9817e8d18ff19fa05eea1c8"),
    "HYPE": ("Crypto.HYPE/USD", "4279e31cc369bbcc2faf022b382b080e32a8e689ff20fbc530d2a603eb6cd98b"),
}
PYTH_HOSTS = tuple(h for h in os.environ.get(
    "Q15_PYTH_HOSTS", "https://hermes.pyth.network,https://pyth.dourolabs.app/hermes").split(",") if h)
PYTH_KEY_FILE = os.path.join(DATA, "pyth_api_key.txt")
PYTH_RECHECK_SECS = 300.0
KRAKEN_WS = "wss://ws.kraken.com/v2"
KRAKEN_FX = {"EUR/USD": "EURUSD", "GBP/USD": "GBPUSD", "USD/JPY": "USDJPY",
             "AUD/USD": "AUDUSD", "USD/CAD": "USDCAD"}
HL_WS = "wss://api.hyperliquid.xyz/ws"
HL_COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")

# Unsigned REST shares one per-IP budget (~2 req/s, TEMP_SHADOW notes) with
# the Miami recorder's station poll, so reads are spread out: >= 3 s apart,
# discovery one series at a time (the first pass back to back), programs
# (~8 pages) every 7.5 min -- each 15-min program is seen at least once.
REST_GAP = 3.0
DISCOVERY_SECS = 600.0           # each series re-read this often (one every 100 s)
DISCOVERY_AHEAD_SECS = 3 * 3600.0
# a synced edit of this file restarts the recorder (the launcher relaunches
# it 20 s after a clean exit), so a fix needs no hand kill
SOURCE_CHECK_SECS = 30.0
OPEN_PULL_DELAY = 20.0           # a quarter-hour's opened markets, +20 s (strike / rules)
SETTLE_DELAY, SETTLE_RETRY, SETTLE_WINDOW = 180.0, 120.0, 3600.0
PROGRAMS_SECS = 450.0
SUB_BEFORE_OPEN, KEEP_AFTER_CLOSE = 60.0, 600.0
FORGET_AFTER_CLOSE = 3 * 3600.0


def iso_ts(s) -> Optional[float]:
    return C.parse_iso(s) if s else None


# ---- pure helpers (unit-tested) --------------------------------------------------

def want_tickers(markets: Dict[str, dict], now: float) -> List[str]:
    """Markets to hold WS subscriptions for: from SUB_BEFORE_OPEN before the
    open to KEEP_AFTER_CLOSE after the close."""
    out = []
    for t, m in markets.items():
        o, c = iso_ts(m.get("open_time")), iso_ts(m.get("close_time"))
        if o is None or c is None:
            continue
        if o - SUB_BEFORE_OPEN <= now <= c + KEEP_AFTER_CLOSE:
            out.append(t)
    return sorted(out)


def quarter_start(ts: float) -> float:
    return ts - (ts % 900.0)


def market_sig(m: dict) -> str:
    """What a re-read must change before it is written again."""
    keys = ("status", "floor_strike", "cap_strike", "result", "expiration_value",
            "rules_primary", "open_time", "close_time")
    return json.dumps({k: m.get(k) for k in keys}, sort_keys=True, default=str)


def parse_kraken(msg: dict) -> Dict[str, list]:
    """Kraken v2 ticker snapshot/update -> {EURUSD: [mid, bid, ask]}."""
    if msg.get("channel") != "ticker":
        return {}
    out = {}
    for d in msg.get("data") or []:
        sym = KRAKEN_FX.get(d.get("symbol"))
        try:
            bid, ask = float(d["bid"]), float(d["ask"])
        except (KeyError, TypeError, ValueError):
            continue
        if sym and bid > 0 and ask > 0:
            out[sym] = [round((bid + ask) / 2.0, 8), bid, ask]
    return out


def parse_hl(msg: dict) -> Dict[str, float]:
    """Hyperliquid allMids -> {BTC: mid, ...} for the race coins."""
    if msg.get("channel") != "allMids":
        return {}
    mids = (msg.get("data") or {}).get("mids") or {}
    out = {}
    for c in HL_COINS:
        try:
            out[c] = float(mids[c])
        except (KeyError, TypeError, ValueError):
            continue
    return out


_PYTH_BY_ID = {fid: sym for sym, (_name, fid) in PYTH_FEEDS.items()}


def parse_pyth(js: dict) -> Dict[str, list]:
    """A Hermes SSE update ({"parsed": [{"id", "price": {"price", "conf",
    "expo", "publish_time"}}]}) -> {EURUSD: [price, conf, publish_time]}."""
    out = {}
    for p in js.get("parsed") or []:
        sym = _PYTH_BY_ID.get(str(p.get("id") or "").lower().replace("0x", ""))
        pr = p.get("price") or {}
        try:
            e = int(pr["expo"])
            out[sym] = [int(pr["price"]) * 10.0 ** e, int(pr["conf"]) * 10.0 ** e,
                        int(pr["publish_time"])]
        except (KeyError, TypeError, ValueError):
            continue
    out.pop(None, None)
    return out


def pyth_key() -> str:
    """The Hermes key: PYTH_API_KEY, else the key file; '' when neither.
    Never logged."""
    k = os.environ.get("PYTH_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(PYTH_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


class PxBook:
    """Latest price per (source, symbol) and what changed since the last
    flush; flush() -> one row per source."""

    def __init__(self):
        self._lock = threading.Lock()
        self.latest: Dict[str, Dict[str, list]] = collections.defaultdict(dict)
        self.dirty: Dict[str, set] = collections.defaultdict(set)
        self.updates = collections.Counter()
        self.last_rx: Dict[str, float] = {}

    def put(self, src: str, vals: Dict[str, object], rx: float) -> None:
        if not vals:
            return
        with self._lock:
            for sym, v in vals.items():
                v = v if isinstance(v, list) else [v]
                if self.latest[src].get(sym) != v:
                    self.latest[src][sym] = v
                    self.dirty[src].add(sym)
            self.updates[src] += 1
            self.last_rx[src] = rx

    def flush(self, rx: float) -> List[dict]:
        with self._lock:
            rows = []
            for src, syms in self.dirty.items():
                if syms:
                    rows.append({"rx": rx, "src": src,
                                 "px": {s: self.latest[src][s] for s in sorted(syms)}})
            self.dirty = collections.defaultdict(set)
            return rows


# ---- price feeds -------------------------------------------------------------------

class WSPriceFeed(threading.Thread):
    """One public WebSocket price source (no auth): connect, subscribe,
    parse each frame into the PxBook; ping every ping_secs; reconnect with
    capped backoff."""

    def __init__(self, name: str, url: str, subscribe: dict, parse, book: PxBook,
                 stop_evt: threading.Event, ping: Optional[dict] = None, ping_secs: float = 20.0):
        super().__init__(daemon=True, name=f"px-{name}")
        self.src, self.url, self.subscribe, self.parse = name, url, subscribe, parse
        self.book, self.stop_evt, self.ping_msg, self.ping_secs = book, stop_evt, ping, ping_secs
        self.connected = False
        self.stats = {"connects": 0, "errors": 0, "last_error": ""}

    def status(self) -> dict:
        last = self.book.last_rx.get(self.src)
        return {"connected": self.connected, "updates": self.book.updates[self.src],
                "last_update_age_s": round(time.time() - last, 1) if last else None, **self.stats}

    def run(self) -> None:
        backoff = 2.0
        while not self.stop_evt.is_set():
            conn = None
            try:
                conn = kalshi_ws.WSConnection(self.url, headers={}, timeout=15)
                conn.connect()
                conn.settimeout(2.0)
                conn.send_json(self.subscribe)
                self.connected = True
                self.stats["connects"] += 1
                log(f"{self.src} connected {self.url}")
                last_ping = time.monotonic()
                last_msg = time.monotonic()
                while not self.stop_evt.is_set():
                    if self.ping_msg is not None and time.monotonic() - last_ping >= self.ping_secs:
                        conn.send_json(self.ping_msg)
                        last_ping = time.monotonic()
                    if time.monotonic() - last_msg > 90:
                        raise kalshi_ws.WSError("no message in 90 s")
                    try:
                        op, data = conn.recv_message()
                    except (socket.timeout, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        continue        # as temp_shadow_collector.WSRecorder: a timeout
                    last_msg = time.monotonic()
                    if op != kalshi_ws.OP_TEXT:
                        continue
                    try:
                        msg = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        self.book.put(self.src, self.parse(msg), time.time())
                backoff = 2.0
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
                if not self.stop_evt.is_set():
                    log(f"{self.src} feed ended: {self.stats['last_error']}; retry in {backoff:.0f}s")
            finally:
                self.connected = False
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            if self.stop_evt.wait(backoff):
                break
            backoff = min(backoff * 2.0, 120.0)


class PythFeed(threading.Thread):
    """Hermes SSE stream for every PYTH_FEEDS id, with the key as a Bearer
    header. No key: wait, re-check every PYTH_RECHECK_SECS. 401/403: the key
    is refused -- log once per refusal, re-check later."""

    def __init__(self, book: PxBook, stop_evt: threading.Event):
        super().__init__(daemon=True, name="px-pyth")
        self.book, self.stop_evt = book, stop_evt
        self.connected = False
        self.state = "no key"
        self.stats = {"connects": 0, "errors": 0, "refused": 0, "last_error": "", "host": ""}

    def status(self) -> dict:
        last = self.book.last_rx.get("pyth")
        return {"state": self.state, "connected": self.connected,
                "updates": self.book.updates["pyth"],
                "last_update_age_s": round(time.time() - last, 1) if last else None, **self.stats}

    def run(self) -> None:
        import requests
        while not self.stop_evt.is_set():
            key = pyth_key()
            if not key:
                self.state = "no key (set PYTH_API_KEY or pyth_api_key.txt)"
                if self.stop_evt.wait(PYTH_RECHECK_SECS):
                    break
                continue
            params = [("ids[]", fid) for _n, fid in PYTH_FEEDS.values()] + [
                ("parsed", "true"), ("allow_unordered", "true")]
            refused = True
            for host in PYTH_HOSTS:
                if self.stop_evt.is_set():
                    return
                try:
                    with requests.get(host.rstrip("/") + "/v2/updates/price/stream", params=params,
                                      headers={"Authorization": f"Bearer {key}",
                                               "Accept": "text/event-stream"},
                                      stream=True, timeout=(15, 60)) as r:
                        if r.status_code in (401, 403):
                            self.stats["refused"] += 1
                            self.stats["last_error"] = f"{host}: HTTP {r.status_code}"
                            log(f"pyth: {host} refused the key (HTTP {r.status_code})")
                            continue
                        r.raise_for_status()
                        refused = False
                        self.connected, self.state = True, "streaming"
                        self.stats["connects"] += 1
                        self.stats["host"] = host
                        log(f"pyth: streaming {len(PYTH_FEEDS)} feeds from {host}")
                        for line in r.iter_lines():
                            if self.stop_evt.is_set():
                                return
                            if line and line.startswith(b"data:"):
                                try:
                                    js = json.loads(line[5:])
                                except ValueError:
                                    continue
                                self.book.put("pyth", parse_pyth(js), time.time())
                except Exception as e:
                    self.stats["errors"] += 1
                    self.stats["last_error"] = f"{host}: {type(e).__name__}: {str(e)[:160]}"
                    log(f"pyth: {self.stats['last_error']}")
                finally:
                    self.connected = False
                if not refused:
                    break
            self.state = "key refused" if refused else "reconnecting"
            if self.stop_evt.wait(PYTH_RECHECK_SECS if refused else 5.0):
                break


# ---- REST: discovery, opened markets, settlements, programs -----------------------

class Q15Rest(threading.Thread):
    def __init__(self, coll: "Collector"):
        super().__init__(daemon=True, name="rest")
        self.coll = coll
        self.client = T.RestClient(coll.signer, coll.stop_evt, min_gap=REST_GAP, window_gap=REST_GAP)
        self.markets: Dict[str, dict] = {}
        self._sig: Dict[str, str] = {}
        self.next_discovery = 0.0
        self._disc_i = 0
        self.next_programs = 0.0
        self.open_done: set = set()
        self.settled: set = set()
        self._prog_sig = ""
        self.stats = collections.Counter()

    def status(self) -> dict:
        now = time.time()
        live = [t for t, m in self.markets.items()
                if (iso_ts(m.get("open_time")) or 0) <= now < (iso_ts(m.get("close_time")) or 0)]
        return {"markets_known": len(self.markets), "markets_open": len(live),
                "settled": len(self.settled), "requests": self.client.n,
                "errors": self.client.nerr, "last_error": self.client.last_error, **self.stats}

    def _note(self, m: dict, rx: float, why: str) -> None:
        t = m.get("ticker")
        if not t:
            return
        sig = market_sig(m)
        if self._sig.get(t) == sig:
            return
        self._sig[t] = sig
        self.markets[t] = m
        self.coll.writer.write("meta", {"rx": rx, "kind": "market", "why": why, "market": m})

    def run(self) -> None:
        while not self.coll.stop_evt.is_set():
            try:
                now = time.time()
                if now >= self.next_discovery:
                    s = SERIES[self._disc_i % len(SERIES)]
                    if self.discover(now, s):
                        self._disc_i += 1
                        first_pass = self._disc_i < len(SERIES)
                        self.next_discovery = now + (0.0 if first_pass
                                                     else DISCOVERY_SECS / len(SERIES))
                    else:
                        self.next_discovery = now + 30.0      # the same series again soon
                self.pull_opened(now)
                self.pull_settled(now)
                if now >= self.next_programs:
                    self.programs(now)
                    self.next_programs = now + PROGRAMS_SECS
                self.forget(now)
                self.coll.ws.set_markets(want_tickers(self.markets, time.time()))
            except Exception as e:
                self.coll.error("rest", e)
            if self.coll.stop_evt.wait(5.0):
                break

    def discover(self, now: float, s: str) -> bool:
        """One series' markets closing in the next DISCOVERY_AHEAD_SECS
        (Kalshi lists ~40 windows ahead, newest first: an unbounded first
        page of the crypto race -- 5 coins a window -- held only the far
        ones, 10/22); False when the read failed."""
        try:
            ms = self.client.paginate("/markets", {
                "series_ticker": s, "min_close_ts": int(now) - 60,
                "max_close_ts": int(now + DISCOVERY_AHEAD_SECS), "limit": 200},
                "markets", max_pages=5)
        except Exception:
            self.stats["discovery_fails"] += 1
            return False
        for m in ms:
            self._note(m, time.time(), "discover")
        self.stats["discoveries"] += 1
        return True

    def _batch(self, tickers: List[str], why: str) -> List[dict]:
        out = []
        for i in range(0, len(tickers), 50):
            r = self.client.get("/markets", {"tickers": ",".join(tickers[i:i + 50]), "limit": 200})
            if r.ok:
                for m in r.body.get("markets") or []:
                    self._note(m, time.time(), why)
                    out.append(m)
        return out

    def pull_opened(self, now: float) -> None:
        q = quarter_start(now)
        if now - q < OPEN_PULL_DELAY or q in self.open_done:
            return
        self.open_done.add(q)
        opened = [t for t, m in self.markets.items()
                  if abs((iso_ts(m.get("open_time")) or 0) - q) < 1.0]
        if opened:
            self._batch(opened, "opened")
            self.stats["open_pulls"] += 1

    def pull_settled(self, now: float) -> None:
        due = []
        for t, m in self.markets.items():
            c = iso_ts(m.get("close_time"))
            if c is None or t in self.settled or now < c + SETTLE_DELAY or now > c + SETTLE_WINDOW:
                continue
            if now - float(m.get("_settle_try", 0.0)) < SETTLE_RETRY:
                continue
            due.append(t)
        if not due:
            return
        for t in due:
            self.markets[t]["_settle_try"] = now
        for m in self._batch(due, "settle"):
            if m.get("result") in ("yes", "no"):
                t = m["ticker"]
                self.settled.add(t)
                try:
                    C.write_json_atomic(C.data_path("settled", f"{t}.json"),
                                        {"rx": time.time(), "market": m})
                except Exception as e:
                    self.coll.error("settled", e)

    def programs(self, now: float) -> None:
        try:
            progs = self.client.paginate("/incentive_programs", {"status": "active", "limit": 1000},
                                         "incentive_programs", cursor_key="next_cursor")
        except Exception as e:
            self.coll.error("programs", e)
            return
        mine = sorted((p for p in progs if p.get("market_ticker", "").split("-")[0] in SERIES),
                      key=lambda p: p.get("market_ticker", ""))
        sig = json.dumps([(p.get("id"), p.get("period_reward"), p.get("end_date")) for p in mine])
        if sig != self._prog_sig:
            self._prog_sig = sig
            self.coll.writer.write("meta", {"rx": time.time(), "kind": "programs", "programs": mine})
        self.stats["program_pulls"] += 1

    def forget(self, now: float) -> None:
        for t in [t for t, m in self.markets.items()
                  if (iso_ts(m.get("close_time")) or now) < now - FORGET_AFTER_CLOSE]:
            self.markets.pop(t, None)
            self._sig.pop(t, None)


# ---- the collector -------------------------------------------------------------------

class Collector:
    def __init__(self, duration: Optional[float] = None):
        self.started = time.time()
        self.duration = duration
        self.stop_evt = threading.Event()
        self.writer = T.RawWriter()
        self.signer = T.Signer()
        self.errors = collections.deque(maxlen=20)
        self.n_errors = 0
        self.until = C.parse_iso(UNTIL) or float("inf")
        self.ws = T.WSRecorder(self.writer, lambda: self.signer.headers("GET", T.WS_PATH),
                               self.stop_evt, on_error=lambda w, e: self.error(w, e, log_it=False))
        self.px = PxBook()
        self.feeds = [
            WSPriceFeed("kraken", KRAKEN_WS, {"method": "subscribe", "params": {
                "channel": "ticker", "symbol": sorted(KRAKEN_FX), "event_trigger": "bbo"}},
                parse_kraken, self.px, self.stop_evt, ping={"method": "ping"}),
            WSPriceFeed("hl", HL_WS, {"method": "subscribe", "subscription": {"type": "allMids"}},
                        parse_hl, self.px, self.stop_evt, ping={"method": "ping"})]
        if PYTH_ON:
            self.feeds.append(PythFeed(self.px, self.stop_evt))
        self.rest = Q15Rest(self)
        self.exit_reason = ""

    def error(self, where: str, err, log_it: bool = True) -> None:
        msg = (f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err))[:300]
        self.n_errors += 1
        self.errors.append({"ts": C.iso(time.time()), "where": where, "error": msg})
        if log_it:
            log(f"! {where}: {msg}")

    def threads(self) -> list:
        return [self.ws, self.rest] + self.feeds

    def status(self) -> dict:
        now = time.time()
        return {"pid": os.getpid(), "state": "running" if not self.exit_reason else "stopped",
                "exit_reason": self.exit_reason, "started": C.iso(self.started), "now": C.iso(now),
                "rx": now, "uptime_s": round(now - self.started, 1), "until": UNTIL,
                "series": list(SERIES), "ws": self.ws.status(),
                "px": {f.src if hasattr(f, "src") else "pyth": f.status() for f in self.feeds},
                "rest": self.rest.status(), "raw_lines": dict(self.writer.lines),
                "raw_bytes": dict(self.writer.bytes),
                "errors": {"count": self.n_errors, "recent": list(self.errors)[-10:]},
                "signing_key": "ok" if self.signer._key is not None else
                               (self.signer.error or "not loaded")}

    def write_status(self, meta: bool) -> None:
        st = self.status()
        try:
            C.write_json_atomic(C.STATUS_FILE, st)
        except Exception as e:
            self.error("status", e)
        if meta:
            self.writer.write("meta", {"rx": st["rx"], "kind": "status", "data": st})

    def rotate(self) -> None:
        for p in T.gzip_due_files(C.DATA_ROOT, time.time()):
            try:
                self.writer.close_path(p)
                T.gzip_file(p)
                log(f"gzipped {os.path.relpath(p, C.DATA_ROOT)}")
            except Exception as e:
                self.error("gzip", e)

    def run(self) -> int:
        log(f"q15 collector starting pid {os.getpid()}; data {C.DATA_ROOT}; {len(SERIES)} series; "
            f"until {UNTIL}")
        self.rotate()
        for t in self.threads():
            t.start()
        code = EXIT_OK
        next_flush = next_hb = next_meta = next_px = 0.0
        next_rot = time.time() + 600
        last_hour = int(time.time() // 3600)
        src_mtime = _source_mtime()
        next_src = time.time() + SOURCE_CHECK_SECS
        try:
            while True:
                now = time.time()
                if os.path.exists(C.HALT_FILE):
                    self.exit_reason, code = "HALT file present", EXIT_DONE
                    break
                if now >= next_src:
                    next_src = now + SOURCE_CHECK_SECS
                    if _source_mtime() != src_mtime:
                        self.exit_reason, code = "source file changed (synced fix)", EXIT_OK
                        break
                if now >= self.until:
                    self.exit_reason, code = f"past Q15_SHADOW_UNTIL {UNTIL}", EXIT_DONE
                    break
                if self.duration is not None and now - self.started >= self.duration:
                    self.exit_reason, code = f"--duration {self.duration:.0f}s elapsed", EXIT_OK
                    break
                dead = [t.name for t in self.threads() if not t.is_alive()]
                if dead:
                    self.exit_reason, code = f"thread(s) died: {dead}", EXIT_ERROR
                    break
                if now >= next_px:
                    for row in self.px.flush(now):
                        self.writer.write("px", row)
                    next_px = now + 1.0
                if now >= next_flush:
                    self.writer.flush()
                    next_flush = now + T.FLUSH_SECS
                if now >= next_hb:
                    self.write_status(meta=now >= next_meta)
                    if now >= next_meta:
                        next_meta = now + T.STATUS_META_SECS
                    next_hb = now + T.HEARTBEAT_SECS
                hour = int(now // 3600)
                if hour != last_hour or now >= next_rot:
                    self.writer.close_idle()
                    self.rotate()
                    last_hour = hour
                    next_rot = now + 600
                self.stop_evt.wait(0.25)
        except KeyboardInterrupt:
            self.exit_reason, code = "KeyboardInterrupt", EXIT_OK
        except Exception as e:
            self.exit_reason, code = f"main loop: {type(e).__name__}: {e}", EXIT_ERROR
            log(traceback.format_exc())
        finally:
            self.shutdown()
        log(f"q15 collector exiting ({self.exit_reason}) code {code}")
        return code

    def shutdown(self) -> None:
        self.stop_evt.set()
        self.ws.stop()
        for t in self.threads():
            if t.is_alive():
                t.join(timeout=10)
        try:
            self.write_status(meta=True)
        except Exception:
            pass
        self.writer.flush()
        self.writer.close_all()


def _source_mtime() -> float:
    """This file's and its helpers' mtimes, summed (any synced edit shows)."""
    tot = 0.0
    for p in (os.path.abspath(__file__), T.__file__, C.__file__, kalshi_ws.__file__):
        try:
            tot += os.path.getmtime(p)
        except OSError:
            pass
    return tot


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--duration", type=float, default=None,
                    help="exit (code 0) after this many seconds (smoke tests)")
    ap.add_argument("--echo", action="store_true", help="echo log lines to stdout")
    args = ap.parse_args(argv)
    T._ECHO = args.echo or sys.stdout.isatty()
    if os.path.normcase(os.path.abspath(C.DATA_ROOT)) != os.path.normcase(os.path.abspath(DATA)):
        print(f"refusing: temp_shadow_common was imported before this module "
              f"(DATA_ROOT {C.DATA_ROOT}, not {DATA})")
        return EXIT_ERROR
    os.makedirs(DATA, exist_ok=True)
    if os.path.exists(C.HALT_FILE):
        log("HALT file present; not starting")
        return EXIT_DONE
    if time.time() >= (C.parse_iso(UNTIL) or float("inf")):
        log(f"past Q15_SHADOW_UNTIL {UNTIL}; not starting")
        return EXIT_DONE
    ok, why = T.acquire_lock(C.LOCK_FILE)
    if not ok:
        log(f"not starting: {why}")
        return EXIT_LOCKED
    try:
        return Collector(duration=args.duration).run()
    except Exception:
        log("fatal: " + traceback.format_exc())
        return EXIT_ERROR
    finally:
        T.release_lock(C.LOCK_FILE)


if __name__ == "__main__":
    sys.exit(main())
