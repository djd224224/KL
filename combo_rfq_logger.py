#!/usr/bin/env python3
r"""combo_rfq_logger.py -- a READ-ONLY logger of Kalshi's combo RFQ flow, the
data side of the combo maker shadow (Jack 2026-11-05: "Yes, build it", on
"Build a read-only combo RFQ shadow logger and scorer first?").

Kalshi routes every combo (multivariate / parlay) order through an RFQ: the
requester asks, makers answer privately, the requester accepts, the maker
confirms within 3 s, and the execution prints on the public trade tape. This
logger watches two public WebSocket channels on ONE connection and sends
nothing else:

  communications  every RFQ Kalshi broadcasts. Measured 11/05 (Thursday night,
                  TNF + NBA + college): ~2,700 rfq_created a second, ~4.7 MB/s,
                  nearly all quote previews deleted within ~35 s. Subscribed
                  SHARDED server-side to 1/SHARD_FACTOR (default 50, a 2%
                  random slice: ~48 RFQs a second, ~90 KB/s), so the feed
                  costs the box's Wi-Fi little beside the bot.
  trade           every public print on every market (~170 a second, ~65 KB/s).
                  The combo (KXMVE*) prints, ~43 a second that night, are the
                  executions.

Kept in memory: the slice's combo RFQs for RFQ_TTL_S (legs, size, creation
time), by combo market. A combo print on a market one of them asked for, from
2 s before to MATCH_WINDOW_S after its creation, is an EXECUTION: logged with
the RFQ's legs (11/05: prints land 0.6-7 s after creation, RFQs live 7 s
median, 35 s at most). imm combo_rfq_score.py prices the legs from 1-minute
candles and scores a shadow maker against the print and the settlement.

Files: <OUT>\combo_<UTC date>.jsonl, one JSON object a line, by "k":
  exec   a matched execution: the print (ts_ms, tid, mt, px = YES price, ct,
         side = the taker's outcome side, book) and its RFQ (rfq, rfq_ms,
         legs = [[market, side], ...], tc = target cost $ or cn = contracts,
         cr = the requester id's first 8 characters, coll, n = the print's
         number on that RFQ)
  rfq    1 in RFQ_SAMPLE of the slice's combo RFQs (executed or not), for
         the fill-rate context
  stat   a minute's counts: the slice's RFQs (combo / single / batch, leg
         count mix, deletes, lifetimes), every combo print on the tape (n,
         contracts, taker premium $), matched / unmatched, seq gaps
  start / conn / disc / err / stop  the run's own record
A closed day's file is gzip'd at the UTC day roll.

READ-ONLY: subscribe commands only; no order, quote, RFQ or REST write. One
REST read at start (build_client's exchange status). Runs at below-normal
priority, one instance at a time (a lock file in OUT). Stops on Ctrl+C,
--minutes, or the STOP file <OUT>\combo_rfq_logger.stop (it also refuses to
start while that file exists; delete it to allow runs again).

ALWAYS ON (Jack 2026-11-05: "Register the logger as an always-on Windows task
so it captures every night"): the "KL combo-rfq logger" task
(register_combo_rfq_logger.ps1) runs run_combo_rfq_logger.ps1's restart loop
through run_combo_rfq_logger_hidden.vbs. Exit codes for that loop: 3 = the
STOP file (stop looping), 4 = another logger holds the lock (stop looping;
that one runs on), anything else = restart after 20 s.

  python combo_rfq_logger.py [--minutes N] [--out DIR] [--shard-factor 50]
"""
import argparse
import gzip
import json
import os
import shutil
import socket
import sys
import threading
import time
import zlib
from collections import Counter, OrderedDict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import kalshi_ws  # noqa: E402

WS_URL = os.environ.get("COMBO_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
WS_PATH = "/trade-api/ws/v2"
DEFAULT_OUT = os.environ.get("COMBO_RFQ_DIR", os.path.join(HERE, "run-logs", "combo-rfq"))
SHARD_FACTOR = int(os.environ.get("COMBO_SHARD_FACTOR", "50"))   # 2% of RFQs
SHARD_KEY = int(os.environ.get("COMBO_SHARD_KEY", "0"))
RFQ_TTL_S = 90.0          # an RFQ lives <= ~35 s (11/05 p99); kept this long for matching
MATCH_WINDOW_S = 60.0     # a print this long after the RFQ's creation still matches it
MATCH_EARLY_S = 2.0       # ...or this much before it (clock slack between the channels)
RFQ_SAMPLE = 20           # 1 in N of the slice's combo RFQs logged as "rfq"
STAT_SECS = 60.0
STALE_SECS = 30.0         # Kalshi pings every 10 s: no frame this long = reconnect
BACKOFF_MAX = 60.0
MAX_INFLIGHT = 200_000    # hard cap on the RFQ map (normally ~48/s x 90 s = 4-5k)
LEG_BUCKETS = 12          # leg counts above this share the top bucket
COMBO_PREFIX = "KXMVE"
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
STOP_NAME = "combo_rfq_logger.stop"
LOCK_NAME = "combo_rfq_logger.lock"
EXIT_STOP = 3             # the STOP file: the launcher stops looping
EXIT_LOCKED = 4           # another logger holds the lock: the launcher stops looping


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} [COMBO] {msg}", flush=True)


def iso_ms(s):
    """'2026-11-05T23:11:23.050513Z' -> epoch ms (int), or None."""
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def legs_of(raw) -> list:
    """mve_selected_legs -> [[market_ticker, side] (+ yes settlement value)]."""
    out = []
    for leg in raw or []:
        mt, side = leg.get("market_ticker"), leg.get("side")
        if not mt or side not in ("yes", "no"):
            continue
        row = [mt, side]
        if leg.get("yes_settlement_value_dollars") is not None:
            row.append(leg["yes_settlement_value_dollars"])
        out.append(row)
    return out


def sampled(key: str, n: int) -> bool:
    """A deterministic 1-in-n pick by crc32 of the key."""
    return n <= 1 or zlib.crc32(str(key).encode("utf-8")) % n == 0


def _num(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(p * len(s)))]


class DayWriter:
    """Appends JSON lines to <out>\\combo_<UTC date>.jsonl; at the UTC day roll
    it closes the day's file and gzips it on a background thread."""

    def __init__(self, out_dir: str, clock=time.time):
        self.out_dir = out_dir
        self.clock = clock
        os.makedirs(out_dir, exist_ok=True)
        self.day = None
        self.f = None
        self.lines = 0
        self.gz_threads = []

    def path_of(self, day: str) -> str:
        return os.path.join(self.out_dir, f"combo_{day}.jsonl")

    def _open(self, day: str) -> None:
        if self.f is not None:
            self.f.close()
            th = threading.Thread(target=gzip_file, args=(self.path_of(self.day),), daemon=True)
            th.start()
            self.gz_threads.append(th)
        self.day = day
        self.f = open(self.path_of(day), "a", encoding="utf-8")

    def write(self, obj: dict) -> None:
        day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")
        if day != self.day:
            self._open(day)
        self.f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.lines += 1

    def flush(self) -> None:
        if self.f is not None:
            self.f.flush()

    def close(self) -> None:
        if self.f is not None:
            self.f.close()
            self.f = None
        for th in self.gz_threads:          # a day being gzip'd finishes first
            th.join(timeout=120)


def gzip_file(path: str) -> None:
    """path -> path.gz (the plain file removed once the copy is whole)."""
    try:
        if not os.path.exists(path) or os.path.exists(path + ".gz"):
            return
        tmp = path + ".gz.tmp"
        with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp, path + ".gz")
        os.remove(path)
    except OSError as e:
        log(f"! gzip of {os.path.basename(path)} failed: {e!r}")


class ComboLogger:
    """The message side, free of the network: handle(msg) for each WebSocket
    message, upkeep(now) about once a second. Records go to `writer.write`."""

    def __init__(self, writer, rfq_sample: int = RFQ_SAMPLE, ttl_s: float = RFQ_TTL_S,
                 match_s: float = MATCH_WINDOW_S, clock=time.time, shard=(SHARD_FACTOR, SHARD_KEY)):
        self.w = writer
        self.rfq_sample = rfq_sample
        self.ttl_ms = ttl_s * 1000.0
        self.match_ms = match_s * 1000.0
        self.clock = clock
        self.shard = list(shard)
        self.rfqs = OrderedDict()      # combo market ticker -> the latest RFQ on it
        self.ids = OrderedDict()       # rfq id -> created ms (lifetimes)
        self.seq = {}                  # sid -> last seq
        self.totals = Counter()
        self._reset_stats(clock())

    # ---- stats ----------------------------------------------------------------

    def _reset_stats(self, now: float) -> None:
        self.st = Counter()
        self.legs = Counter()
        self.size_kind = Counter()
        self.life = []
        self.st_since = now

    def stat_record(self, now: float) -> dict:
        st = self.st
        rec = {"k": "stat", "t": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
               "secs": round(now - self.st_since, 1), "shard": self.shard,
               "rfq": st["rfq"], "rfq_combo": st["rfq_combo"], "rfq_single": st["rfq_single"],
               "batch": st["batch"], "del": st["del"],
               "legs": {str(k): v for k, v in sorted(self.legs.items())},
               "size": dict(self.size_kind),
               "life_p50_ms": _pct(self.life, 0.5), "life_p90_ms": _pct(self.life, 0.9),
               "prints": st["prints"], "mve_prints": st["mve_prints"],
               "mve_ct": round(st["mve_ct"], 2), "mve_usd": round(st["mve_usd"], 2),
               "matched": st["matched"], "unmatched": st["unmatched"],
               "logged_rfq": st["logged_rfq"], "gaps": st["gaps"], "errors": st["errors"],
               "inflight": len(self.rfqs)}
        return rec

    # ---- messages -------------------------------------------------------------

    def reset_connection(self) -> None:
        """A new connection: new sids and seqs."""
        self.seq.clear()

    def handle(self, m: dict) -> None:
        if not isinstance(m, dict):
            return
        typ, msg = m.get("type"), m.get("msg") or {}
        sid, seq = m.get("sid"), m.get("seq")
        if sid is not None and isinstance(seq, int):
            last = self.seq.get(sid)
            if last is not None and seq != last + 1:
                self.st["gaps"] += 1
                self.totals["gaps"] += 1
            self.seq[sid] = seq
        if typ == "rfq_created":
            self._rfq(msg)
        elif typ == "rfq_batch_created":
            self._rfq_batch(msg)
        elif typ == "rfq_deleted":
            self._deleted(msg.get("id"), msg.get("deleted_ts"))
        elif typ == "rfq_batch_deleted":
            for rid in msg.get("rfq_ids") or []:
                self._deleted(rid, msg.get("deleted_ts"))
        elif typ == "trade":
            self._trade(msg)
        elif typ == "error":
            self.st["errors"] += 1
            self.totals["errors"] += 1
            self.w.write({"k": "err", "t": self._now_iso(), "msg": msg})

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")

    def _rfq(self, msg: dict, legs=None, created_ts=None, creator=None) -> None:
        legs = legs_of(msg.get("mve_selected_legs")) if legs is None else legs
        rid, mt = msg.get("id"), msg.get("market_ticker")
        cms = iso_ms(created_ts or msg.get("created_ts")) or int(self.clock() * 1000)
        self.st["rfq"] += 1
        self.totals["rfq"] += 1
        if not legs or not rid or not mt:
            self.st["rfq_single"] += 1
            return
        self.st["rfq_combo"] += 1
        self.legs[min(len(legs), LEG_BUCKETS)] += 1
        tc = msg.get("target_cost_dollars")
        cn = msg.get("contracts_fp")
        if _num(tc) > 0:
            cn = None
            self.size_kind["tc"] += 1
        else:
            tc = None
            self.size_kind["cn"] += 1
        rec = {"id": rid, "ms": cms, "mt": mt, "legs": legs, "tc": tc, "cn": cn,
               "cr": str(creator or msg.get("creator_id") or "")[:8],
               "coll": msg.get("mve_collection_ticker"), "prints": 0}
        if mt in self.rfqs:
            del self.rfqs[mt]               # keep insertion order = creation order
        self.rfqs[mt] = rec
        self.ids[rid] = cms
        if sampled(rid, self.rfq_sample):
            self.st["logged_rfq"] += 1
            self.w.write({"k": "rfq", "ms": cms, "id": rid, "mt": mt, "legs": legs, "tc": tc,
                          "cn": cn, "cr": rec["cr"], "coll": rec["coll"]})

    def _rfq_batch(self, msg: dict) -> None:
        self.st["batch"] += 1
        legs = msg.get("legs") or []
        for child in msg.get("rfqs") or []:
            try:
                picked = [legs[i] for i in child.get("leg_indices") or []]
            except (IndexError, TypeError):
                continue
            self._rfq(child, legs=legs_of(picked), created_ts=msg.get("created_ts"),
                      creator=msg.get("creator_id"))

    def _deleted(self, rid, deleted_ts) -> None:
        self.st["del"] += 1
        cms = self.ids.pop(rid, None) if rid else None
        dms = iso_ms(deleted_ts)
        if cms is not None and dms is not None and len(self.life) < 100_000:
            self.life.append(dms - cms)

    def _trade(self, msg: dict) -> None:
        self.st["prints"] += 1
        mt = msg.get("market_ticker") or ""
        if not mt.startswith(COMBO_PREFIX):
            return
        ts_ms = msg.get("ts_ms") or int(_num(msg.get("ts")) * 1000)
        px, ct = _num(msg.get("yes_price_dollars")), _num(msg.get("count_fp"))
        side = msg.get("taker_outcome_side") or msg.get("taker_side")
        self.st["mve_prints"] += 1
        self.st["mve_ct"] += ct
        self.st["mve_usd"] += ct * (px if side == "yes" else 1.0 - px)
        self.totals["mve_prints"] += 1
        rec = self.rfqs.get(mt)
        if rec is None or not (-MATCH_EARLY_S * 1000 <= ts_ms - rec["ms"] <= self.match_ms):
            self.st["unmatched"] += 1
            return
        rec["prints"] += 1
        self.st["matched"] += 1
        self.totals["matched"] += 1
        self.w.write({"k": "exec", "ts_ms": ts_ms, "tid": msg.get("trade_id"), "mt": mt,
                      "px": msg.get("yes_price_dollars"), "ct": msg.get("count_fp"),
                      "side": side, "book": msg.get("taker_book_side"),
                      "blk": bool(msg.get("is_block_trade")), "rfq": rec["id"],
                      "rfq_ms": rec["ms"], "n": rec["prints"], "legs": rec["legs"],
                      "tc": rec["tc"], "cn": rec["cn"], "cr": rec["cr"], "coll": rec["coll"]})

    # ---- upkeep ---------------------------------------------------------------

    def upkeep(self, now: float, stat_secs: float = STAT_SECS) -> None:
        """Forget RFQs past their TTL (and over the cap); write the minute's stat."""
        cut = now * 1000.0 - self.ttl_ms
        while self.rfqs:
            mt, rec = next(iter(self.rfqs.items()))
            if rec["ms"] >= cut and len(self.rfqs) <= MAX_INFLIGHT:
                break
            self.rfqs.popitem(last=False)
        while self.ids:
            rid, cms = next(iter(self.ids.items()))
            if cms >= cut and len(self.ids) <= MAX_INFLIGHT:
                break
            self.ids.popitem(last=False)
        if now - self.st_since >= stat_secs:
            self.w.write(self.stat_record(now))
            self._reset_stats(now)


# ---- the network side ---------------------------------------------------------

def below_normal() -> None:
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def single_instance(out_dir: str):
    """An exclusive lock on <out>\\combo_rfq_logger.lock, or None if another
    logger holds it. Keep the returned handle open for the run."""
    path = os.path.join(out_dir, LOCK_NAME)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def signer():
    """(headers() for the WebSocket handshake), from the bots' own key."""
    from crypto_touch_mm import KEY_ID, build_client
    client = build_client()
    key_id = getattr(client, "key_id", None) or KEY_ID

    def headers():
        ts = str(int(time.time() * 1000))
        return {"KALSHI-ACCESS-KEY": key_id,
                "KALSHI-ACCESS-SIGNATURE": client.sign_pss_text(ts + "GET" + WS_PATH),
                "KALSHI-ACCESS-TIMESTAMP": ts}
    return headers


def subscribe_cmds(shard_factor: int, shard_key: int) -> list:
    comm = {"channels": ["communications"]}
    if shard_factor > 1:
        comm.update(shard_factor=shard_factor, shard_key=shard_key)
    return [{"id": 1, "cmd": "subscribe", "params": comm},
            {"id": 2, "cmd": "subscribe", "params": {"channels": ["trade"]}}]


def run(out_dir: str, minutes: float, shard_factor: int, shard_key: int, url: str = WS_URL) -> int:
    os.makedirs(out_dir, exist_ok=True)
    stop_path = os.path.join(out_dir, STOP_NAME)
    if os.path.exists(stop_path):
        log(f"stop file present ({stop_path}); not starting")
        return EXIT_STOP
    lock = single_instance(out_dir)
    if lock is None:
        log("another combo RFQ logger is running (lock held); exiting")
        return EXIT_LOCKED
    below_normal()
    headers = signer()
    writer = DayWriter(out_dir)
    lg = ComboLogger(writer, shard=(shard_factor, shard_key))
    deadline = time.time() + minutes * 60 if minutes else None
    writer.write({"k": "start", "t": lg._now_iso(), "pid": os.getpid(), "url": url,
                  "shard": [shard_factor, shard_key], "minutes": minutes,
                  "rfq_sample": RFQ_SAMPLE, "ttl_s": RFQ_TTL_S, "match_s": MATCH_WINDOW_S})
    log(f"start: shard 1/{shard_factor} (key {shard_key}), out {out_dir}"
        + (f", for {minutes:g} min" if minutes else ""))
    backoff, why = 1.0, "done"
    try:
        while True:
            if deadline and time.time() >= deadline:
                why = "deadline"
                break
            if os.path.exists(stop_path):
                why = "stop file"
                break
            conn = None
            t_conn = time.monotonic()
            try:
                conn = kalshi_ws.WSConnection(url, headers=headers(), timeout=10.0)
                conn.connect()
                conn.settimeout(1.0)
                for cmd in subscribe_cmds(shard_factor, shard_key):
                    conn.send_json(cmd)
                lg.reset_connection()
                writer.write({"k": "conn", "t": lg._now_iso()})
                log("connected; subscribed communications + trade")
                last_up = 0.0
                while True:
                    now = time.time()
                    if now - last_up >= 1.0:
                        last_up = now
                        lg.upkeep(now)
                        writer.flush()
                        if deadline and now >= deadline:
                            raise StopIteration("deadline")
                        if os.path.exists(stop_path):
                            raise StopIteration("stop file")
                        if time.monotonic() - conn.last_rx > STALE_SECS:
                            raise kalshi_ws.WSError(f"no frames for {STALE_SECS:.0f}s")
                    try:
                        op, payload = conn.recv_message()
                    except socket.timeout:
                        continue
                    if op != kalshi_ws.OP_TEXT:
                        continue
                    try:
                        lg.handle(json.loads(payload.decode("utf-8")))
                    except (ValueError, UnicodeDecodeError):
                        lg.st["errors"] += 1
            except StopIteration as e:
                why = str(e)
                break
            except Exception as e:
                writer.write({"k": "disc", "t": lg._now_iso(), "err": f"{type(e).__name__}: {str(e)[:200]}"})
                log(f"! {type(e).__name__}: {str(e)[:200]}; reconnecting in ~{backoff:.0f}s")
                if time.monotonic() - t_conn > 300:
                    backoff = 1.0
                time.sleep(backoff)
                backoff = min(backoff * 2.0, BACKOFF_MAX)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
    except KeyboardInterrupt:
        why = "interrupted"
    finally:
        lg.upkeep(time.time(), stat_secs=1.0)       # the last partial minute, if any
        writer.write({"k": "stop", "t": lg._now_iso(), "why": why, "totals": dict(lg.totals)})
        writer.close()
        lock.close()
        log(f"stop ({why}): {dict(lg.totals)}")
    return EXIT_STOP if why == "stop file" else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default run-logs\\combo-rfq)")
    ap.add_argument("--minutes", type=float, default=0.0, help="stop after this long (0 = until stopped)")
    ap.add_argument("--shard-factor", type=int, default=SHARD_FACTOR,
                    help="take 1 in N RFQs, server-side (1 = all of them: ~4.7 MB/s)")
    ap.add_argument("--shard-key", type=int, default=SHARD_KEY)
    a = ap.parse_args(argv)
    if not (1 <= a.shard_factor <= 100 and 0 <= a.shard_key < a.shard_factor):
        ap.error("shard factor 1-100, key 0 <= key < factor")
    return run(a.out, a.minutes, a.shard_factor, a.shard_key)


if __name__ == "__main__":
    sys.exit(main())
