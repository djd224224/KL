#!/usr/bin/env python3
"""Unit tests for kalshi_ws.py -- run: python -m unittest test_kalshi_ws

Everything runs against an in-process WebSocket server on 127.0.0.1 (plain
ws://, no TLS, no network): the frame codec, the handshake, timeouts that
must not lose data, and the feed's book / sequence / subscription logic."""

import base64
import json
import socket
import struct
import threading
import time
import unittest

import kalshi_ws as kw


# ----------------------------------------------------------------------------
# A tiny WebSocket server double
# ----------------------------------------------------------------------------

class _ServerConn:
    """One accepted client: reads masked client frames, sends unmasked."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()
        self.headers = {}

    def handshake(self, accept_override=None, status="101 Switching Protocols"):
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        self.buf = bytearray(rest)
        for ln in head.decode().split("\r\n")[1:]:
            k, v = ln.split(":", 1)
            self.headers[k.strip().lower()] = v.strip()
        key = self.headers["sec-websocket-key"]
        acc = accept_override or kw.accept_key(key)
        self.sock.sendall((f"HTTP/1.1 {status}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acc}"
                           f"\r\n\r\n").encode())

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("client gone")
            self.buf += chunk

    def recv_frame(self):
        self._need(2)
        b0, b1 = self.buf[0], self.buf[1]
        n, off = b1 & 0x7F, 2
        if n == 126:
            self._need(4)
            n, off = struct.unpack_from("!H", self.buf, 2)[0], 4
        elif n == 127:
            self._need(10)
            n, off = struct.unpack_from("!Q", self.buf, 2)[0], 10
        assert b1 & 0x80, "client frames must be masked"
        self._need(off + 4 + n)
        key = bytes(self.buf[off:off + 4])
        payload = kw.xor_mask(bytes(self.buf[off + 4:off + 4 + n]), key)
        del self.buf[:off + 4 + n]
        return b0 & 0x0F, payload

    def recv_json(self, timeout=5.0):
        """Next TEXT message from the client (pings are answered)."""
        self.sock.settimeout(timeout)
        while True:
            op, payload = self.recv_frame()
            if op == kw.OP_PING:
                self.send(kw.OP_PONG, payload)
                continue
            if op == kw.OP_TEXT:
                return json.loads(payload)

    def send(self, op, payload: bytes, fin=True):
        self.sock.sendall(kw.encode_frame(op, payload, mask=False, fin=fin))

    def send_json(self, obj):
        self.send(kw.OP_TEXT, json.dumps(obj).encode())

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class _Server:
    """Accepts connections on a background thread; each one is handed to
    `on_conn(conn)` (on that thread)."""

    def __init__(self, on_conn):
        self.lsock = socket.socket()
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(5)
        self.port = self.lsock.getsockname()[1]
        self.on_conn = on_conn
        self.conns = []
        self._stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/trade-api/ws/v2"

    def _loop(self):
        self.lsock.settimeout(0.2)
        while not self._stop:
            try:
                s, _ = self.lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c = _ServerConn(s)
            self.conns.append(c)
            threading.Thread(target=self._run_one, args=(c,), daemon=True).start()

    def _run_one(self, c):
        try:
            self.on_conn(c)
        except Exception:
            pass

    def stop(self):
        self._stop = True
        try:
            self.lsock.close()
        except Exception:
            pass
        for c in self.conns:
            c.close()


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ----------------------------------------------------------------------------
# Codec + connection
# ----------------------------------------------------------------------------

class TestFrameCodec(unittest.TestCase):
    def test_mask_is_its_own_inverse(self):
        key = b"\x01\x02\x03\x04"
        data = bytes(range(256)) * 3
        self.assertEqual(kw.xor_mask(kw.xor_mask(data, key), key), data)
        self.assertEqual(kw.xor_mask(b"", key), b"")

    def test_length_encodings(self):
        for n, hdr in ((5, 2), (125, 2), (126, 4), (65535, 4), (65536, 10)):
            f = kw.encode_frame(kw.OP_TEXT, b"x" * n, mask=False)
            self.assertEqual(len(f), hdr + n, n)
            fm = kw.encode_frame(kw.OP_TEXT, b"x" * n, mask=True,
                                 mask_key=b"abcd")
            self.assertEqual(len(fm), hdr + 4 + n, n)
            self.assertTrue(fm[1] & 0x80)

    def test_accept_key_matches_rfc_example(self):
        # RFC 6455 section 1.3
        self.assertEqual(kw.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


class TestConnection(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def _serve(self, fn):
        s = _Server(fn)
        self.servers.append(s)
        return s

    def test_handshake_headers_text_ping_and_fragments(self):
        got = {}

        def srv(c):
            c.handshake()
            got["headers"] = dict(c.headers)
            got["first"] = c.recv_json()
            c.send(kw.OP_PING, b"hi")                 # client must pong
            op, payload = c.recv_frame()
            got["pong"] = (op, payload)
            # a fragmented text message: "ab" + "cd" + "ef"
            c.send(kw.OP_TEXT, b'{"a":', fin=False)
            c.send(kw.OP_CONT, b'"bcd', fin=False)
            c.send(kw.OP_CONT, b'ef"}', fin=True)
            # a large message (64-bit length)
            big = json.dumps({"x": "y" * 70000}).encode()
            c.send(kw.OP_TEXT, big)
            time.sleep(0.5)

        s = self._serve(srv)
        conn = kw.WSConnection(s.url, headers={"KALSHI-ACCESS-KEY": "k1"})
        conn.connect()
        conn.send_json({"id": 1, "cmd": "subscribe"})
        op, payload = conn.recv_message()
        self.assertEqual(json.loads(payload), {"a": "bcdef"})
        op, payload = conn.recv_message()
        self.assertEqual(len(json.loads(payload)["x"]), 70000)
        self.assertEqual(got["headers"]["kalshi-access-key"], "k1")
        self.assertEqual(got["first"], {"id": 1, "cmd": "subscribe"})
        self.assertEqual(got["pong"], (kw.OP_PONG, b"hi"))
        conn.close()

    def test_timeout_mid_frame_loses_nothing(self):
        frame = kw.encode_frame(kw.OP_TEXT, json.dumps({"k": 1}).encode(),
                                mask=False)

        def srv(c):
            c.handshake()
            c.sock.sendall(frame[:3])          # header + 1 byte of payload
            time.sleep(0.6)                    # > the client's 0.2s timeout
            c.sock.sendall(frame[3:])
            time.sleep(0.5)

        s = self._serve(srv)
        conn = kw.WSConnection(s.url)
        conn.connect()
        conn.settimeout(0.2)
        timeouts = 0
        while True:
            try:
                op, payload = conn.recv_message()
                break
            except socket.timeout:
                timeouts += 1
                self.assertLess(timeouts, 50)
        self.assertGreaterEqual(timeouts, 1)
        self.assertEqual(json.loads(payload), {"k": 1})
        conn.close()

    def test_timeout_between_fragments_keeps_the_partial_message(self):
        def srv(c):
            c.handshake()
            c.send(kw.OP_TEXT, b'{"part":', fin=False)
            time.sleep(0.6)
            c.send(kw.OP_CONT, b'"whole"}', fin=True)
            time.sleep(0.5)

        s = self._serve(srv)
        conn = kw.WSConnection(s.url)
        conn.connect()
        conn.settimeout(0.2)
        for _ in range(50):
            try:
                op, payload = conn.recv_message()
                break
            except socket.timeout:
                continue
        self.assertEqual(json.loads(payload), {"part": "whole"})
        conn.close()

    def test_bad_accept_or_status_rejected(self):
        s1 = self._serve(lambda c: c.handshake(accept_override="nope"))
        with self.assertRaises(kw.WSError):
            kw.WSConnection(s1.url, timeout=2).connect()
        s2 = self._serve(lambda c: c.handshake(status="401 Unauthorized"))
        with self.assertRaises(kw.WSError):
            kw.WSConnection(s2.url, timeout=2).connect()

    def test_close_frame_raises_closed(self):
        def srv(c):
            c.handshake()
            c.send(kw.OP_CLOSE, struct.pack("!H", 1001) + b"bye")
            time.sleep(0.3)

        s = self._serve(srv)
        conn = kw.WSConnection(s.url)
        conn.connect()
        with self.assertRaises(kw.WSClosed):
            conn.recv_message()


# ----------------------------------------------------------------------------
# Feed
# ----------------------------------------------------------------------------

def _snap(sid, seq, t, yes, no):
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq,
            "msg": {"market_ticker": t,
                    "yes_dollars_fp": [[p, q] for p, q in yes],
                    "no_dollars_fp": [[p, q] for p, q in no]}}


def _delta(sid, seq, t, side, p, d):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": t, "price_dollars": p, "delta_fp": d,
                    "side": side}}


class _ScriptedKalshi:
    """A server that answers subscribes like Kalshi and lets the test push
    book messages. One script per connection (reconnect = next script)."""

    def __init__(self, reject_book_subs: int = 0, reject_code: int = 26):
        self.cmds = []
        self.conns = []
        self.ready = threading.Event()
        # answer the first N orderbook subscribes with an error, like Kalshi
        # does instead of `subscribed` (never both)
        self.reject_book_subs = reject_book_subs
        self.reject_code = reject_code
        self.server = _Server(self._on_conn)

    def _on_conn(self, c):
        c.handshake()
        self.conns.append(c)
        self.ready.set()
        while True:
            try:
                m = c.recv_json(timeout=10)
            except Exception:
                return
            self.cmds.append(m)
            if m.get("cmd") == "subscribe":
                ch = m["params"]["channels"][0]
                if ch == "orderbook_delta" and self.reject_book_subs > 0:
                    self.reject_book_subs -= 1
                    c.send_json({"type": "error", "id": m["id"],
                                 "msg": {"code": self.reject_code,
                                         "msg": "Subscription market limit exceeded"}})
                    continue
                sid = {"fill": 900, "order_group_updates": 901}.get(ch, m["id"])
                c.send_json({"type": "subscribed", "id": m["id"],
                             "msg": {"channel": ch, "sid": sid}})

    def stop(self):
        self.server.stop()


class TestFeed(unittest.TestCase):
    def setUp(self):
        self.k = _ScriptedKalshi()
        self.logs = []
        self.feed = kw.KalshiFeed(
            self.k.server.url, auth_headers=lambda: {"KALSHI-ACCESS-KEY": "x"},
            log=self.logs.append,
            fill_filter=lambda f: str(f.get("client_order_id", "")).startswith("imm-"),
            chunk=2, stale_secs=5.0, ping_secs=1.0, timeout=2.0,
            backoff_max=0.5, retry_secs=0.2)

    def tearDown(self):
        self.feed.stop()
        self.k.stop()

    def _start(self, markets):
        self.feed.set_markets(markets)
        self.feed.start()
        self.assertTrue(self.k.ready.wait(5))
        self.assertTrue(_wait(lambda: len([m for m in self.k.cmds
                                           if m.get("cmd") == "subscribe"]) >= 3))
        return self.k.conns[-1]

    def _book_sub(self):
        return [m for m in self.k.cmds if m.get("cmd") == "subscribe"
                and m["params"]["channels"] == ["orderbook_delta"]]

    def test_snapshot_then_deltas_build_the_rest_shaped_book(self):
        c = self._start(["A"])
        sub = self._book_sub()[0]
        sid = sub["id"]
        self.assertEqual(sub["params"]["market_tickers"], ["A"])
        self.assertIsNone(self.feed.book_fp("A"))           # no snapshot yet
        c.send_json(_snap(sid, 1, "A", [("0.4800", "500.00"), ("0.4900", "600.00")],
                          [("0.4900", "1200.00")]))
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is not None))
        c.send_json(_delta(sid, 2, "A", "yes", "0.49", "-600.00"))   # level gone
        c.send_json(_delta(sid, 3, "A", "yes", "0.5000", "10.00"))   # new level
        c.send_json(_delta(sid, 4, "A", "no", "0.4900", "-200.00"))
        self.assertTrue(_wait(lambda: self.feed.status()["deltas"] >= 3))
        self.assertEqual(self.feed.book_fp("A"), {"orderbook_fp": {
            "yes_dollars": [["0.4800", "500.00"], ["0.5000", "10.00"]],
            "no_dollars": [["0.4900", "1000.00"]]}})
        dirty, ev, dropped = self.feed.drain()
        self.assertIn("A", dirty)
        self.assertEqual((ev, dropped), ([], 0))

    def test_ready_counts_the_servable_books_without_copying(self):
        c = self._start(["A", "B"])
        sid = self._book_sub()[0]["id"]
        self.assertEqual(self.feed.ready(["A", "B", "Z"]), 0)
        c.send_json(_snap(sid, 1, "A", [("0.40", "1")], [("0.50", "1")]))
        self.assertTrue(_wait(lambda: self.feed.ready(["A", "B"]) == 1))
        c.send_json(_snap(sid, 2, "B", [("0.10", "1")], [("0.80", "1")]))
        self.assertTrue(_wait(lambda: self.feed.ready(["A", "B"]) == 2))
        self.assertEqual(self.feed.ready(["A", "B", "Z"]), 2)   # Z never wanted
        self.feed.set_markets(["A"])                            # B no longer wanted
        self.assertEqual(self.feed.ready(["A", "B"]), 1)

    def test_gap_invalidates_and_resnapshots_and_ok_counts_in_sequence(self):
        c = self._start(["A", "B"])
        sid = self._book_sub()[0]["id"]
        c.send_json(_snap(sid, 1, "A", [("0.40", "100")], [("0.50", "100")]))
        c.send_json(_snap(sid, 2, "B", [("0.10", "100")], [("0.80", "100")]))
        # an ok on the book sid takes seq 3 -- not a gap
        c.send_json({"type": "ok", "id": 99, "sid": sid, "seq": 3,
                     "msg": {"market_tickers": ["A", "B"]}})
        c.send_json(_delta(sid, 4, "A", "yes", "0.40", "5"))
        self.assertTrue(_wait(lambda: self.feed.status()["deltas"] >= 1))
        self.assertEqual(self.feed.status()["gaps"], 0)
        self.assertIsNotNone(self.feed.book_fp("A"))
        # seq 6 skips 5: every book on the sid goes dark + get_snapshot sent
        c.send_json(_delta(sid, 6, "A", "yes", "0.40", "5"))
        self.assertTrue(_wait(lambda: self.feed.status()["gaps"] == 1))
        self.assertIsNone(self.feed.book_fp("A"))
        self.assertIsNone(self.feed.book_fp("B"))
        self.assertTrue(_wait(lambda: any(
            m.get("params", {}).get("action") == "get_snapshot"
            for m in self.k.cmds)))
        snap_cmd = [m for m in self.k.cmds
                    if m.get("params", {}).get("action") == "get_snapshot"][0]
        self.assertEqual(sorted(snap_cmd["params"]["market_tickers"]), ["A", "B"])
        # deltas before the fresh snapshot are ignored
        c.send_json(_delta(sid, 7, "A", "yes", "0.40", "999"))
        c.send_json(_snap(sid, 8, "A", [("0.41", "7")], [("0.50", "1")]))
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is not None))
        self.assertEqual(self.feed.book_fp("A")["orderbook_fp"]["yes_dollars"],
                         [["0.4100", "7.00"]])
        self.assertIsNone(self.feed.book_fp("B"))          # still awaiting its own

    def test_market_set_changes_add_into_room_then_new_subs_and_delete(self):
        c = self._start(["A"])
        sid = self._book_sub()[0]["id"]
        c.send_json(_snap(sid, 1, "A", [("0.40", "1")], [("0.50", "1")]))
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is not None))
        self.feed.set_markets(["A", "B", "C", "D"])        # chunk=2
        self.assertTrue(_wait(lambda: any(
            m.get("params", {}).get("action") == "add_markets" for m in self.k.cmds)))
        add = [m for m in self.k.cmds
               if m.get("params", {}).get("action") == "add_markets"][0]
        self.assertEqual((add["params"]["sids"], add["params"]["market_tickers"]),
                         ([sid], ["B"]))
        self.assertTrue(_wait(lambda: len(self._book_sub()) == 2))
        self.assertEqual(self._book_sub()[1]["params"]["market_tickers"], ["C", "D"])
        # A leaves: delete_markets on its sid; its book is gone at once
        self.feed.set_markets(["B", "C", "D"])
        self.assertTrue(_wait(lambda: any(
            m.get("params", {}).get("action") == "delete_markets"
            for m in self.k.cmds)))
        self.assertIsNone(self.feed.book_fp("A"))

    def test_fills_filtered_to_ours_and_group_events_surface(self):
        c = self._start(["A"])
        c.send_json({"type": "fill", "sid": 900, "msg": {
            "client_order_id": "imm-run-1", "count_fp": "20.00"}})
        c.send_json({"type": "fill", "sid": 900, "msg": {
            "client_order_id": "crypto-9", "count_fp": "5.00"}})
        c.send_json({"type": "order_group_updates", "sid": 901, "seq": 1,
                     "msg": {"event_type": "triggered", "order_group_id": "og1"}})
        self.assertTrue(_wait(lambda: self.feed.status()["og_events"] == 1))
        self.assertTrue(self.feed.event_flag.is_set())
        _dirty, ev, _ = self.feed.drain()
        self.assertEqual([k for k, _b in ev], ["fill", "order_group"])
        self.assertEqual(ev[0][1]["client_order_id"], "imm-run-1")
        self.assertEqual(ev[1][1]["event_type"], "triggered")
        self.assertFalse(self.feed.event_flag.is_set())

    def test_disconnect_invalidates_books_and_reconnects_and_resubscribes(self):
        c = self._start(["A"])
        sid = self._book_sub()[0]["id"]
        c.send_json(_snap(sid, 1, "A", [("0.40", "1")], [("0.50", "1")]))
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is not None))
        n_before = len(self._book_sub())
        c.close()                                         # server drops us
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is None))
        self.assertTrue(_wait(lambda: self.feed.status()["connects"] >= 2, 10))
        self.assertTrue(_wait(lambda: len(self._book_sub()) > n_before, 10))
        c2 = self.k.conns[-1]
        sid2 = self._book_sub()[-1]["id"]
        c2.send_json(_snap(sid2, 1, "A", [("0.42", "3")], [("0.50", "1")]))
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is not None))
        self.assertEqual(self.feed.book_fp("A")["orderbook_fp"]["yes_dollars"],
                         [["0.4200", "3.00"]])

    def test_resync_distrusts_at_once_then_reconnects_as_a_request(self):
        c = self._start(["A"])
        sid = self._book_sub()[0]["id"]
        c.send_json(_snap(sid, 1, "A", [("0.40", "1")], [("0.50", "1")]))
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is not None))
        n_before = len(self._book_sub())
        self.feed.resync("audit")
        self.assertIsNone(self.feed.book_fp("A"))          # untrusted at once
        self.assertTrue(_wait(lambda: self.feed.status()["connects"] >= 2, 5))
        st = self.feed.status()
        self.assertEqual((st["resyncs"], st["errors"], st["last_error"]),
                         (1, 0, ""))                       # not a failure
        self.assertTrue(any("resync (audit)" in s for s in self.logs))
        self.assertTrue(_wait(lambda: len(self._book_sub()) > n_before, 5))
        c2 = self.k.conns[-1]
        sid2 = self._book_sub()[-1]["id"]
        c2.send_json(_snap(sid2, 1, "A", [("0.43", "2")], [("0.50", "1")]))
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is not None))
        self.assertEqual(self.feed.book_fp("A")["orderbook_fp"]["yes_dollars"],
                         [["0.4300", "2.00"]])
        self.assertEqual(self.feed.status()["resyncs"], 1)  # acted on once

    def test_market_limit_error_halves_chunk_and_retries(self):
        # the server rejects the first book subscribe with code 26
        self.k.reject_book_subs = 1
        self._start(["A", "B"])
        self.assertEqual(self._book_sub()[0]["params"]["market_tickers"], ["A", "B"])
        self.assertTrue(_wait(lambda: self.feed.status()["sub_chunk"] == 1))
        self.assertTrue(_wait(lambda: len(self._book_sub()) >= 3, 5))
        self.assertEqual(sorted(m["params"]["market_tickers"][0]
                                for m in self._book_sub()[1:3]), ["A", "B"])

    def test_unhealthy_when_silent(self):
        self.feed.stale_secs = 0.3
        self.feed.ping_secs = 100.0     # no pings: the server stays silent
        c = self._start(["A"])
        sid = self._book_sub()[0]["id"]
        c.send_json(_snap(sid, 1, "A", [("0.40", "1")], [("0.50", "1")]))
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is not None))
        self.assertTrue(_wait(lambda: self.feed.book_fp("A") is None, 3))


class TestRestShape(unittest.TestCase):
    def test_round_trip_against_rest_levels(self):
        ob = {"orderbook_fp": {"yes_dollars": [["0.48", "500"], ["0.49", "600"]],
                               "no_dollars": [["0.4900", "1200.00"]]}}
        y, n = kw.rest_book_levels(ob)
        self.assertEqual(kw.book_to_rest_shape(y, n), {"orderbook_fp": {
            "yes_dollars": [["0.4800", "500.00"], ["0.4900", "600.00"]],
            "no_dollars": [["0.4900", "1200.00"]]}})


if __name__ == "__main__":
    unittest.main()
