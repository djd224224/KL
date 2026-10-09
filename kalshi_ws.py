#!/usr/bin/env python3
"""Kalshi WebSocket market-data feed for incentive_mm.py -- stdlib only.

Why this exists (2026-10-02): the IMM read every managed order book over REST,
one call per market, inside its quote cycle. At ~1,500 markets that read phase
alone took 80-165s and the full cycle 4.5-9 minutes, so a resting quote could
sit minutes behind a moved market. Kalshi pushes every book change over its
WebSocket API (`orderbook_delta`: one snapshot per market, then deltas), plus
our own fills (`fill`) and order-group events (`order_group_updates`). This
module keeps those books current in memory so the quote loop can read them in
microseconds and react between cycles.

Design rules -- the same contract as the bot's other refresher threads:

* Everything network-facing runs on ONE daemon thread. The trading thread only
  reads snapshots through a lock (`book_fp`, `drain`) and never blocks on I/O.
* A book is served only while it is TRUSTWORTHY: a snapshot arrived on the
  live connection, no sequence gap has been seen since, and the connection
  has delivered a frame recently. Anything else -- disconnected,
  reconnecting, gapped, never snapshotted -- returns None and the caller
  falls back to the REST read it always did. Correctness never depends on
  this feed; it is an accelerator.
* No third-party dependency: the trading box's Python has no websocket
  library, and a pip install on the machine every bot shares is a risk this
  does not need. RFC 6455 client side is small: one HTTP upgrade, masked
  client frames, unmasked server frames, fragmentation, ping/pong/close.
* Every failure reconnects with capped exponential backoff and invalidates
  every book first. The thread never raises out of run().

Message formats (docs.kalshi.com, verified live read-only 2026-10-02 on
wss://api.elections.kalshi.com/trade-api/ws/v2): prices are dollar strings
("0.9600"), sizes fixed-point strings ("300.00"). A snapshot carries
`yes_dollars_fp` / `no_dollars_fp` as [[price, size], ...]; a delta carries
`price_dollars`, `delta_fp`, `side`. Book messages carry `sid` and a
per-subscription `seq` that increases by one per message -- and the `ok`
answering an update_subscription on that sid takes a seq in the SAME
sequence (live: 34 deltas + 8 snapshots + 2 oks on sid 1, zero gaps only
when the oks are counted). `subscribed` carries no seq; the first snapshot
is seq 1. A get_snapshot request is answered by the snapshot alone (no ok).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import ssl
import struct
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA

# A single message larger than this is a protocol error, not a book: guards
# the reader against a corrupted length field allocating gigabytes.
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class WSError(Exception):
    """Any WebSocket protocol / transport failure."""


class WSClosed(WSError):
    """The peer closed the connection (or the socket hit EOF)."""


class _Resync(Exception):
    """resync() asked for a fresh connection -- a request, not a failure."""


def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept for a Sec-WebSocket-Key (RFC 6455 4.2.2)."""
    return base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")


def xor_mask(payload: bytes, key: bytes) -> bytes:
    """RFC 6455 masking (its own inverse)."""
    if not payload:
        return b""
    n = len(payload)
    reps = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big")
            ^ int.from_bytes(reps, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes, mask: bool = True,
                 mask_key: Optional[bytes] = None, fin: bool = True) -> bytes:
    """One RFC 6455 frame. Clients MUST mask (mask=True); a server (the test
    double) sends unmasked frames with mask=False."""
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    n = len(payload)
    mbit = 0x80 if mask else 0x00
    if n < 126:
        header = struct.pack("!BB", b0, mbit | n)
    elif n < (1 << 16):
        header = struct.pack("!BBH", b0, mbit | 126, n)
    else:
        header = struct.pack("!BBQ", b0, mbit | 127, n)
    if not mask:
        return header + payload
    key = mask_key if mask_key is not None else os.urandom(4)
    return header + key + xor_mask(payload, key)


class WSConnection:
    """Blocking client connection.

    `recv_message` returns one complete (opcode, payload) data message,
    answering pings and assembling fragments on the way. Socket timeouts
    propagate as `socket.timeout` so the caller can do upkeep between
    messages -- and a timeout NEVER loses data: a frame is consumed from the
    buffer only once it is complete, and a partly assembled fragmented
    message is kept on the instance."""

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 10.0,
                 ssl_context: Optional[ssl.SSLContext] = None):
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self.ssl_context = ssl_context
        self.sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._frags: List[bytes] = []
        self._frag_op: Optional[int] = None
        self._frag_len = 0
        self._send_lock = threading.Lock()
        self.last_rx = 0.0          # monotonic time of the last frame received
        self.pongs = 0

    # ---- connection ---------------------------------------------------------

    def connect(self) -> None:
        u = urlparse(self.url)
        if u.scheme not in ("ws", "wss"):
            raise WSError(f"unsupported scheme {u.scheme!r}")
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        raw = socket.create_connection((host, port), timeout=self.timeout)
        sock = raw
        try:
            if u.scheme == "wss":
                ctx = self.ssl_context or ssl.create_default_context()
                sock = ctx.wrap_socket(raw, server_hostname=host)
            sock.settimeout(self.timeout)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_hdr = host if u.port is None else f"{host}:{port}"
            lines = [f"GET {path} HTTP/1.1", f"Host: {host_hdr}",
                     "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            lines += [f"{k}: {v}" for k, v in self.headers.items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
            resp = bytearray()
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WSClosed("connection closed during handshake")
                resp += chunk
                if len(resp) > 65536:
                    raise WSError("handshake response too large")
            head, _, rest = bytes(resp).partition(b"\r\n\r\n")
            status_line, *hdr_lines = head.decode("latin-1").split("\r\n")
            parts = status_line.split(" ", 2)
            if len(parts) < 2 or parts[1] != "101":
                raise WSError(f"handshake rejected: {status_line[:200]}")
            hdrs = {}
            for ln in hdr_lines:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
            if hdrs.get("sec-websocket-accept") != accept_key(key):
                raise WSError("handshake: bad Sec-WebSocket-Accept")
            self.sock = sock
            self._buf = bytearray(rest)
            self.last_rx = time.monotonic()
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise

    def settimeout(self, secs: float) -> None:
        if self.sock is not None:
            self.sock.settimeout(secs)

    def close(self, code: int = 1000) -> None:
        s = self.sock
        self.sock = None
        if s is None:
            return
        try:
            with self._send_lock:
                s.sendall(encode_frame(OP_CLOSE, struct.pack("!H", code)))
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass

    # ---- send ---------------------------------------------------------------

    def _send(self, opcode: int, payload: bytes) -> None:
        s = self.sock
        if s is None:
            raise WSClosed("not connected")
        frame = encode_frame(opcode, payload)
        with self._send_lock:
            s.sendall(frame)

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def send_json(self, obj: dict) -> None:
        self.send_text(json.dumps(obj, separators=(",", ":")))

    def ping(self, payload: bytes = b"") -> None:
        self._send(OP_PING, payload)

    # ---- receive ------------------------------------------------------------

    def _fill(self, n: int) -> None:
        """Buffer at least n bytes. May raise socket.timeout; never consumes."""
        while len(self._buf) < n:
            s = self.sock
            if s is None:
                raise WSClosed("not connected")
            chunk = s.recv(65536)
            if not chunk:
                raise WSClosed("socket EOF")
            self._buf += chunk

    def _read_frame(self) -> Tuple[bool, int, bytes]:
        self._fill(2)
        b0, b1 = self._buf[0], self._buf[1]
        n = b1 & 0x7F
        off = 2
        if n == 126:
            self._fill(4)
            n = struct.unpack_from("!H", self._buf, 2)[0]
            off = 4
        elif n == 127:
            self._fill(10)
            n = struct.unpack_from("!Q", self._buf, 2)[0]
            off = 10
        if n > MAX_MESSAGE_BYTES:
            raise WSError(f"frame too large ({n} bytes)")
        masked = bool(b1 & 0x80)
        if masked:
            off += 4
        self._fill(off + n)
        key = bytes(self._buf[off - 4:off]) if masked else None
        payload = bytes(self._buf[off:off + n])
        del self._buf[:off + n]
        if key is not None:
            payload = xor_mask(payload, key)
        self.last_rx = time.monotonic()
        return bool(b0 & 0x80), b0 & 0x0F, payload

    def recv_message(self) -> Tuple[int, bytes]:
        while True:
            fin, op, payload = self._read_frame()
            if op == OP_PING:
                try:
                    self._send(OP_PONG, payload)
                except Exception:
                    pass
                continue
            if op == OP_PONG:
                self.pongs += 1
                continue
            if op == OP_CLOSE:
                code = (struct.unpack("!H", payload[:2])[0]
                        if len(payload) >= 2 else 1005)
                reason = payload[2:].decode("utf-8", "replace")
                try:
                    self._send(OP_CLOSE, payload[:2])
                except Exception:
                    pass
                raise WSClosed(f"closed by peer ({code} {reason[:120]})")
            if op in (OP_TEXT, OP_BINARY):
                if self._frag_op is not None:
                    raise WSError("new data frame inside a fragmented message")
                self._frag_op = op
            elif op == OP_CONT:
                if self._frag_op is None:
                    raise WSError("continuation frame with no message")
            else:
                raise WSError(f"unknown opcode {op}")
            self._frags.append(payload)
            self._frag_len += len(payload)
            if self._frag_len > MAX_MESSAGE_BYTES:
                raise WSError("message too large")
            if fin:
                out_op, out = self._frag_op, b"".join(self._frags)
                self._frags, self._frag_op, self._frag_len = [], None, 0
                return out_op, out


# ----------------------------------------------------------------------------
# Order-book helpers
# ----------------------------------------------------------------------------

def px_key(p) -> float:
    """Dollar price -> a float key exact to the 0.0001 grid ("0.96" and
    "0.9600" are one level)."""
    return round(float(p), 4)


def book_to_rest_shape(yes: Dict[float, float], no: Dict[float, float]) -> dict:
    """The REST get_orderbook body (`orderbook_fp`, ascending dollar levels)
    the quote loop already parses -- so a WS book and a REST book take the
    same code path downstream."""
    def side(levels: Dict[float, float]) -> List[List[str]]:
        return [[f"{p:.4f}", f"{q:.2f}"] for p, q in sorted(levels.items())
                if q > 1e-9]
    return {"orderbook_fp": {"yes_dollars": side(yes), "no_dollars": side(no)}}


def rest_book_levels(ob: dict) -> Tuple[Dict[float, float], Dict[float, float]]:
    """A REST `orderbook_fp` body -> ({px: qty} yes, {px: qty} no), the same
    keys the feed stores -- for the shadow-mode comparison."""
    fp = (ob or {}).get("orderbook_fp") or {}

    def side(rows) -> Dict[float, float]:
        out: Dict[float, float] = {}
        for p, q in rows or []:
            k = px_key(p)
            out[k] = out.get(k, 0.0) + float(q)
        return {k: v for k, v in out.items() if v > 1e-9}
    return side(fp.get("yes_dollars")), side(fp.get("no_dollars"))


class _Book:
    __slots__ = ("yes", "no", "sid", "ok", "updated")

    def __init__(self, sid: Optional[int] = None):
        self.yes: Dict[float, float] = {}
        self.no: Dict[float, float] = {}
        self.sid = sid
        self.ok = False          # snapshot received on the live subscription
        self.updated = 0.0       # monotonic time of the last snapshot/delta


# ----------------------------------------------------------------------------
# The feed thread
# ----------------------------------------------------------------------------

class KalshiFeed(threading.Thread):
    """Maintains order books for a caller-chosen market set, and surfaces our
    fills and order-group events, over one Kalshi WebSocket connection.

    Thread contract: the main thread calls `set_markets`, `book_fp`,
    `book_levels`, `ready`, `drain`, `healthy`, `status`, `stop`; everything else
    runs on this thread. Network sends happen outside the state lock."""

    def __init__(self, url: str,
                 auth_headers: Callable[[], Dict[str, str]],
                 log: Callable[[str], None] = print,
                 fill_filter: Optional[Callable[[dict], bool]] = None,
                 want_fills: bool = True,
                 want_order_groups: bool = True,
                 chunk: int = 100,
                 stale_secs: float = 30.0,
                 ping_secs: float = 10.0,
                 timeout: float = 10.0,
                 backoff_max: float = 60.0,
                 retry_secs: float = 60.0,
                 ssl_context: Optional[ssl.SSLContext] = None,
                 connect_factory: Optional[Callable[[], WSConnection]] = None):
        super().__init__(daemon=True, name="kalshi-ws")
        self.url = url
        self.auth_headers = auth_headers
        self.log = log
        self.fill_filter = fill_filter
        self.want_fills = want_fills
        self.want_order_groups = want_order_groups
        self.chunk = max(1, int(chunk))
        self.stale_secs = float(stale_secs)
        self.ping_secs = float(ping_secs)
        self.timeout = float(timeout)
        self.backoff_max = float(backoff_max)
        self.retry_secs = float(retry_secs)
        self.ssl_context = ssl_context
        self._connect_factory = connect_factory
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        # set whenever there is something for drain(): a book changed, a fill
        # or an order-group event arrived
        self.event_flag = threading.Event()
        self._want: Set[str] = set()
        self._want_ver = 0
        self._applied_ver = -1
        self._apply_after = 0.0          # retry throttle after a failed command
        self._books: Dict[str, _Book] = {}
        self._subs: Dict[int, Set[str]] = {}        # book sid -> markets
        self._sid_seq: Dict[int, int] = {}          # any sid -> last seq
        self._market_sid: Dict[str, int] = {}
        # cmd id -> (kind, markets); kind in subscribe/add/delete/snapshot/base
        self._pending: Dict[int, Tuple[str, List[str]]] = {}
        self._next_id = 1
        self._outbox: List[dict] = []    # commands queued under the lock
        self._dirty: Set[str] = set()
        self._events: Deque[Tuple[str, dict]] = deque(maxlen=5000)
        self._events_dropped = 0
        self._conn: Optional[WSConnection] = None
        self._connected = False
        self._sub_chunk = self.chunk
        self._resync_req = ""            # resync(): reason, until acted on
        self.stats = {"connects": 0, "disconnects": 0, "msgs": 0,
                      "snapshots": 0, "deltas": 0, "gaps": 0, "fills": 0,
                      "og_events": 0, "errors": 0, "last_error": "",
                      "resnapshots": 0, "resyncs": 0}

    # ---- main-thread API ----------------------------------------------------

    def set_markets(self, tickers: Iterable[str]) -> None:
        new = {t for t in tickers if t}
        with self._lock:
            if new != self._want:
                self._want = new
                self._want_ver += 1

    def healthy(self) -> bool:
        conn = self._conn
        return bool(self._connected and conn is not None
                    and time.monotonic() - conn.last_rx <= self.stale_secs)

    def book_levels(self, ticker: str
                    ) -> Optional[Tuple[Dict[float, float], Dict[float, float]]]:
        """Copies of ({px: qty} yes, {px: qty} no) when trustworthy."""
        if not self.healthy():
            return None
        with self._lock:
            b = self._books.get(ticker)
            if b is None or not b.ok or ticker not in self._want:
                return None
            return dict(b.yes), dict(b.no)

    def book_fp(self, ticker: str) -> Optional[dict]:
        """The REST-shaped book for `ticker` when trustworthy, else None
        (the caller falls back to REST)."""
        lv = self.book_levels(ticker)
        return None if lv is None else book_to_rest_shape(*lv)

    def ready(self, tickers: Iterable[str]) -> int:
        """How many of `tickers` book_levels would serve right now -- without
        copying any book (a caller waiting for a batch of new snapshots)."""
        if not self.healthy():
            return 0
        with self._lock:
            n = 0
            for t in tickers:
                b = self._books.get(t)
                if b is not None and b.ok and t in self._want:
                    n += 1
            return n

    def drain(self) -> Tuple[Set[str], List[Tuple[str, dict]], int]:
        """(tickers whose book changed, [(kind, msg)], dropped-event count)
        since the last drain. kind is 'fill' or 'order_group'."""
        with self._lock:
            dirty, self._dirty = self._dirty, set()
            ev = list(self._events)
            self._events.clear()
            dropped, self._events_dropped = self._events_dropped, 0
            self.event_flag.clear()
        return dirty, ev, dropped

    def status(self) -> dict:
        with self._lock:
            out = dict(self.stats)
            out.update(connected=self._connected, healthy=self.healthy(),
                       want=len(self._want),
                       books_ok=sum(1 for b in self._books.values() if b.ok),
                       subs=len(self._subs), sub_chunk=self._sub_chunk)
        return out

    def resync(self, reason: str = "") -> None:
        """Rebuild every book from fresh snapshots on a new connection, for a
        caller with reason to distrust them (the bot's REST audit). The books
        go untrusted at once (book_levels -> None until their new snapshot);
        the feed thread reconnects within ~1s, without the failure backoff."""
        with self._lock:
            self._resync_req = (reason or "requested")[:120]
            for b in self._books.values():
                b.ok = False

    def stop(self) -> None:
        self._stop_evt.set()
        c = self._conn
        if c is not None:
            c.close()

    # ---- thread -------------------------------------------------------------

    def run(self) -> None:
        backoff = 1.0
        while not self._stop_evt.is_set():
            started = time.monotonic()
            resync = False
            try:
                self._session()
            except _Resync as e:     # asked for, not a failure: no backoff
                resync = True
                self.log(f"[WS] resync ({e}): reconnecting for fresh snapshots")
            except Exception as e:   # every failure: invalidate + reconnect
                with self._lock:
                    self.stats["errors"] += 1
                    self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
                if not self._stop_evt.is_set():
                    self.log(f"[WS] ! {type(e).__name__}: {str(e)[:200]}; "
                             f"reconnecting in ~{backoff:.0f}s")
            finally:
                self._teardown()
            if time.monotonic() - started > 300:
                backoff = 1.0            # a long healthy session resets backoff
            if resync:
                if self._stop_evt.wait(0.2):
                    break
                continue
            if self._stop_evt.wait(backoff * (0.8 + 0.4 * random.random())):
                break
            backoff = min(backoff * 2.0, self.backoff_max)

    def _teardown(self) -> None:
        with self._lock:
            was = self._connected
            self._connected = False
            conn, self._conn = self._conn, None
            for b in self._books.values():
                b.ok = False
            self._subs.clear()
            self._sid_seq.clear()
            self._market_sid.clear()
            self._pending.clear()
            self._outbox.clear()
            self._applied_ver = -1
            self._apply_after = 0.0
            if was:
                self.stats["disconnects"] += 1
        if conn is not None:
            conn.close()

    def _new_conn(self) -> WSConnection:
        if self._connect_factory is not None:
            return self._connect_factory()
        c = WSConnection(self.url, headers=self.auth_headers(),
                         timeout=self.timeout, ssl_context=self.ssl_context)
        c.connect()
        return c

    def _session(self) -> None:
        conn = self._new_conn()
        with self._lock:
            self._conn = conn
            self._connected = True
            self.stats["connects"] += 1
            self._resync_req = ""       # a new connection IS the resync
            # account channels: no market list = every market (docs)
            if self.want_fills:
                self._queue_cmd("subscribe", {"channels": ["fill"]}, "base", [])
            if self.want_order_groups:
                self._queue_cmd("subscribe",
                                {"channels": ["order_group_updates"]}, "base", [])
        self.log(f"[WS] connected {self.url}")
        conn.settimeout(1.0)            # wake every second for upkeep
        last_ping = time.monotonic()
        while not self._stop_evt.is_set():
            if self._resync_req:
                with self._lock:
                    why, self._resync_req = self._resync_req, ""
                    self.stats["resyncs"] += 1
                raise _Resync(why)
            self._apply_market_set()
            self._flush_outbox(conn)
            now = time.monotonic()
            if now - conn.last_rx > self.stale_secs:
                raise WSError(f"no frames for {now - conn.last_rx:.0f}s")
            if now - last_ping >= self.ping_secs:
                conn.ping(b"imm")
                last_ping = now
            try:
                op, payload = conn.recv_message()
            except socket.timeout:
                continue
            if op != OP_TEXT:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                with self._lock:
                    self.stats["errors"] += 1
                continue
            if isinstance(msg, dict):
                self._handle(msg)

    # ---- commands (queue under the lock, send outside it) -------------------

    def _queue_cmd(self, cmd: str, params: dict, kind: str,
                   markets: List[str]) -> int:
        # caller holds the lock. A get_snapshot ("snapshot") is answered by
        # the snapshots alone -- never an ok -- so it is not tracked as
        # pending (it would never be popped).
        cid = self._next_id
        self._next_id += 1
        if kind != "snapshot":
            self._pending[cid] = (kind, list(markets))
        self._outbox.append({"id": cid, "cmd": cmd, "params": params})
        return cid

    def _flush_outbox(self, conn: WSConnection) -> None:
        with self._lock:
            out, self._outbox = self._outbox, []
        for obj in out:
            conn.send_json(obj)

    def _apply_market_set(self) -> None:
        """Bring the orderbook_delta subscriptions in line with the wanted
        market set: delete_markets for markets no longer wanted, add_markets
        into subscriptions with room, new subscriptions for the rest."""
        with self._lock:
            if self._applied_ver == self._want_ver \
                    or time.monotonic() < self._apply_after:
                return
            want = set(self._want)
            pending_mk: Set[str] = set()
            for kind, mk in self._pending.values():
                if kind in ("subscribe", "add"):
                    pending_mk.update(mk)
            have = set(self._market_sid)
            by_sid: Dict[int, List[str]] = {}
            for t in sorted(have - want):
                sid = self._market_sid.pop(t)
                self._books.pop(t, None)
                self._subs.get(sid, set()).discard(t)
                by_sid.setdefault(sid, []).append(t)
            for sid, mk in by_sid.items():
                self._queue_cmd("update_subscription",
                                {"sids": [sid], "market_tickers": mk,
                                 "action": "delete_markets"}, "delete", mk)
            for t in list(self._books):
                if t not in want:
                    self._books.pop(t, None)
            add = sorted(want - have - pending_mk)
            i = 0
            for sid, mk in sorted(self._subs.items()):
                room = self._sub_chunk - len(mk)
                if room <= 0 or i >= len(add):
                    continue
                part = add[i:i + room]
                i += len(part)
                self._queue_cmd("update_subscription",
                                {"sids": [sid], "market_tickers": part,
                                 "action": "add_markets"}, "add", part)
            while i < len(add):
                part = add[i:i + self._sub_chunk]
                i += len(part)
                self._queue_cmd("subscribe",
                                {"channels": ["orderbook_delta"],
                                 "market_tickers": part}, "subscribe", part)
            for t in add:
                self._books.setdefault(t, _Book())
            self._applied_ver = self._want_ver

    # ---- message handling (feed thread) -------------------------------------

    def _handle(self, msg: dict) -> None:
        typ = msg.get("type")
        with self._lock:
            self.stats["msgs"] += 1
            if typ in ("orderbook_snapshot", "orderbook_delta"):
                self._on_book(msg, typ == "orderbook_snapshot")
            elif typ == "fill":
                self._on_fill(msg)
            elif typ == "order_group_updates":
                self._seq_check(msg)
                self.stats["og_events"] += 1
                self._push_event("order_group", msg.get("msg") or {})
            elif typ == "subscribed":
                self._on_subscribed(msg)
            elif typ == "ok":
                self._pending.pop(msg.get("id"), None)
                # an ok on a book subscription takes a seq in its sequence
                if not self._seq_check(msg) and msg.get("sid") is not None \
                        and int(msg["sid"]) in self._subs:
                    self._resnapshot(int(msg["sid"]))
            elif typ == "error":
                self._on_error(msg)
            elif typ == "unsubscribed":
                self._pending.pop(msg.get("id"), None)

    def _push_event(self, kind: str, body: dict) -> None:
        # caller holds the lock
        if len(self._events) == self._events.maxlen:
            self._events_dropped += 1
        self._events.append((kind, body))
        self.event_flag.set()

    def _on_subscribed(self, msg: dict) -> None:
        body = msg.get("msg") or {}
        kind, mk = self._pending.pop(msg.get("id"), ("", []))
        sid = body.get("sid")
        if body.get("channel") == "orderbook_delta" and sid is not None:
            s = self._subs.setdefault(int(sid), set())
            for t in mk:
                if t in self._want:
                    s.add(t)
                    self._market_sid[t] = int(sid)

    def _on_error(self, msg: dict) -> None:
        body = msg.get("msg") or {}
        code = body.get("code")
        kind, mk = self._pending.pop(msg.get("id"), ("", []))
        self.stats["errors"] += 1
        self.stats["last_error"] = f"code {code}: {str(body.get('msg'))[:120]}"
        if kind in ("subscribe", "add") and mk:
            for t in mk:
                b = self._books.get(t)
                if b is not None and not b.ok:
                    self._books.pop(t, None)
            if code == 26 and self._sub_chunk > 1:
                # per-subscription market limit: halve the chunk
                self._sub_chunk = max(1, self._sub_chunk // 2)
            # let a later pass re-place them, throttled
            self._applied_ver = -1
            self._apply_after = time.monotonic() + (
                1.0 if code == 26 else self.retry_secs)
        self.log(f"[WS] ! error on cmd {msg.get('id')} ({kind}, {len(mk)} "
                 f"mkts): code {code} {str(body.get('msg'))[:160]}")

    def _seq_check(self, msg: dict) -> bool:
        """Track the per-sid seq. True when in order (or untracked); False on
        a gap. Caller holds the lock."""
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is None or seq is None:
            return True
        sid, seq = int(sid), int(seq)
        last = self._sid_seq.get(sid)
        self._sid_seq[sid] = seq
        if last is None or seq == last + 1:
            return True
        self.stats["gaps"] += 1
        return False

    def _resnapshot(self, sid: int) -> None:
        """A gap on a book subscription: every book on it is suspect until
        a fresh snapshot lands. get_snapshot returns snapshots without
        touching the subscription (docs). Caller holds the lock."""
        markets = sorted(self._subs.get(sid, set()))
        for t in markets:
            b = self._books.get(t)
            if b is not None:
                b.ok = False
        if markets:
            self.stats["resnapshots"] += 1
            self._queue_cmd("update_subscription",
                            {"sids": [sid], "market_tickers": markets,
                             "action": "get_snapshot"}, "snapshot", markets)

    def _on_book(self, msg: dict, snapshot: bool) -> None:
        # caller holds the lock
        body = msg.get("msg") or {}
        t = body.get("market_ticker")
        sid = msg.get("sid")
        in_order = self._seq_check(msg)
        if not in_order and sid is not None:
            self._resnapshot(int(sid))
        if not t or t not in self._want:
            return          # a market we no longer (or never) wanted
        b = self._books.get(t)
        if b is None:
            b = self._books[t] = _Book()
        if sid is not None:
            # bookkeeping from the message itself, so a snapshot that beats
            # its subscribe/ok acknowledgement is still adopted
            sid = int(sid)
            b.sid = sid
            self._subs.setdefault(sid, set()).add(t)
            self._market_sid[t] = sid
        if snapshot:
            b.yes = {px_key(p): float(q) for p, q in
                     (body.get("yes_dollars_fp") or body.get("yes_dollars") or [])
                     if float(q) > 1e-9}
            b.no = {px_key(p): float(q) for p, q in
                    (body.get("no_dollars_fp") or body.get("no_dollars") or [])
                    if float(q) > 1e-9}
            b.ok = True
            self.stats["snapshots"] += 1
        else:
            self.stats["deltas"] += 1
            if not b.ok:
                return      # gapped / never snapshotted: wait for a snapshot
            side = b.yes if body.get("side") == "yes" else b.no
            p = px_key(body.get("price_dollars"))
            q = side.get(p, 0.0) + float(body.get("delta_fp") or 0.0)
            if q > 1e-9:
                side[p] = q
            else:
                side.pop(p, None)
        b.updated = time.monotonic()
        self._dirty.add(t)
        self.event_flag.set()

    def _on_fill(self, msg: dict) -> None:
        # caller holds the lock
        body = msg.get("msg") or {}
        if self.fill_filter is not None:
            try:
                if not self.fill_filter(body):
                    return
            except Exception:
                return
        self.stats["fills"] += 1
        self._push_event("fill", body)
