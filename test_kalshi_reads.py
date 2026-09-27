"""kalshi_reads: signed-first Kalshi GETs with a public fallback (2026-09-27).
No network: the signed client and requests.get are mocked."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import kalshi_reads as kr


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = {} if body is None else body
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _Base(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.dict(kr.STATS, {k: 0 for k in kr.STATS}),
                  mock.patch.object(kr, "_last_signed", 0.0)):
            p.start()
            self.addCleanup(p.stop)
        s = mock.patch.object(kr.time, "sleep")
        self.sleep = s.start()
        self.addCleanup(s.stop)
        self.client = mock.Mock()
        c = mock.patch.object(kr, "signed_client", return_value=self.client)
        c.start()
        self.addCleanup(c.stop)


class TestRoutes(_Base):
    def test_signed_first_and_no_public_call(self):
        self.client.get.return_value = {"markets": [1]}
        with mock.patch.object(kr.requests, "get", side_effect=AssertionError("public")):
            self.assertEqual(kr.kalshi_get("/markets", {"series_ticker": "KXFOO"}),
                             {"markets": [1]})
        self.client.get.assert_called_once_with("/markets", {"series_ticker": "KXFOO"})
        self.assertEqual((kr.STATS["signed"], kr.STATS["fallback"]), (1, 0))

    def test_a_signed_failure_falls_back_to_public(self):
        self.client.get.side_effect = RuntimeError("HttpError(429 Too Many Requests)")
        with mock.patch.object(kr.requests, "get", return_value=_Resp(200, {"ok": 1})) as g:
            self.assertEqual(kr.kalshi_get("/markets", {"a": "b"}), {"ok": 1})
        g.assert_called_once()
        self.assertEqual(g.call_args[0][0], kr.BASE + "/markets")
        self.assertEqual(g.call_args[1]["params"], {"a": "b"})
        self.assertEqual((kr.STATS["public"], kr.STATS["fallback"]), (1, 1))

    def test_public_first_then_signed_on_a_429_without_sleeping(self):
        self.client.get.return_value = {"orderbook_fp": {}}
        with mock.patch.object(kr.requests, "get", return_value=_Resp(429)) as g:
            self.assertEqual(kr.kalshi_get("/markets/X/orderbook", prefer="public"),
                             {"orderbook_fp": {}})
        g.assert_called_once()                       # one public try
        self.sleep.assert_not_called()               # no sleep-retry storm
        self.client.get.assert_called_once()

    def test_public_first_success_never_signs(self):
        with mock.patch.object(kr.requests, "get", return_value=_Resp(200, {"ok": 1})):
            kr.kalshi_get("/markets/X/orderbook", prefer="public")
        self.client.get.assert_not_called()

    def test_a_404_is_final(self):
        with mock.patch.object(kr.requests, "get", return_value=_Resp(404)):
            with self.assertRaises(kr.KalshiReadError) as cm:
                kr.kalshi_get("/markets/NOPE", prefer="public")
        self.client.get.assert_not_called()
        self.assertEqual(cm.exception.status, 404)

    def test_every_route_failing_raises_with_both_errors(self):
        self.client.get.side_effect = OSError("reset")
        with mock.patch.object(kr.requests, "get", return_value=_Resp(429)) as g:
            with self.assertRaises(kr.KalshiReadError) as cm:
                kr.kalshi_get("/markets", {"x": 1})
        self.assertEqual(g.call_count, 2)            # the public fallback retries once
        self.assertIn("signed: OSError", str(cm.exception))
        self.assertIn("public: PublicHTTPError", str(cm.exception))
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(kr.STATS["failed"], 1)

    def test_caller_params_are_copied(self):
        self.client.get.return_value = {}
        params = {"a": 1}
        kr.kalshi_get("/markets", params)
        passed = self.client.get.call_args[0][1]
        self.assertEqual(passed, {"a": 1})
        self.assertIsNot(passed, params)

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            kr.kalshi_get("markets")
        with self.assertRaises(ValueError):
            kr.kalshi_get("/markets", prefer="both")

    def test_signed_reads_are_paced(self):
        self.client.get.return_value = {}
        with mock.patch.object(kr, "SIGNED_MIN_GAP", 0.25):
            kr.kalshi_get("/markets")
            kr.kalshi_get("/markets")
        waits = [c[0][0] for c in self.sleep.call_args_list]
        self.assertEqual(len(waits), 1)              # the second read waited
        self.assertTrue(0 < waits[0] <= 0.25)


class TestPaging(_Base):
    def test_follows_the_cursor_to_the_end(self):
        self.client.get.side_effect = [{"markets": [1, 2], "cursor": "c1"},
                                       {"markets": [3], "cursor": "c2"},
                                       {"markets": [], "cursor": "c3"}]
        self.assertEqual(kr.kalshi_get_all("/markets", {"series_ticker": "S"}), [1, 2, 3])
        cursors = [c[0][1].get("cursor") for c in self.client.get.call_args_list]
        self.assertEqual(cursors, [None, "c1", "c2"])

    def test_an_empty_cursor_is_one_page(self):
        self.client.get.return_value = {"events": [1], "cursor": ""}
        self.assertEqual(kr.kalshi_get_all("/events", items_key="events"), [1])
        self.client.get.assert_called_once()


class TestClientAndKey(unittest.TestCase):
    def test_the_client_is_built_once_on_first_use(self):
        with mock.patch.object(kr, "_client", None), \
                mock.patch.object(kr, "load_private_key", return_value="KEY") as lk, \
                mock.patch("KalshiClientsBaseV2ApiKey_FIXED.ExchangeClient") as ec, \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KALSHI_HTTP_KEEPALIVE", None)
            a, b = kr.signed_client(), kr.signed_client()
            self.assertIs(a, b)
            ec.assert_called_once_with(exchange_api_base=kr.BASE, key_id=kr.KEY_ID,
                                       private_key="KEY")
            lk.assert_called_once()
            self.assertEqual(os.environ.get("KALSHI_HTTP_KEEPALIVE"), "1")

    def test_key_from_a_path_and_a_missing_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        d = tempfile.mkdtemp(prefix="kr_key_")
        path = os.path.join(d, "k.pem")
        with open(path, "wb") as f:
            f.write(pem)
        with mock.patch.dict(os.environ, {"KALSHI_PRIVATE_KEY_PATH": path}):
            os.environ.pop("KALSHI_PRIVATE_KEY", None)
            self.assertEqual(kr.load_private_key().key_size, 2048)
        with mock.patch.dict(os.environ, {"KALSHI_PRIVATE_KEY_PATH":
                                          os.path.join(d, "missing.pem")}), \
                mock.patch.object(kr, "LOCAL_KEY_DEFAULT", os.path.join(d, "nope.pem")):
            os.environ.pop("KALSHI_PRIVATE_KEY", None)
            with self.assertRaises(FileNotFoundError):
                kr.load_private_key()


class TestCli(_Base):
    def test_params_and_public_flag(self):
        with mock.patch.object(kr, "kalshi_get", return_value={"markets": []}) as kg:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kr.main(["/markets", "series_ticker=KXFOO", "limit=5", "--public"])
        self.assertEqual(rc, 0)
        kg.assert_called_once_with("/markets", {"series_ticker": "KXFOO", "limit": "5"},
                                   prefer="public")
        self.assertEqual(json.loads(buf.getvalue()), {"markets": []})

    def test_path_forms_including_the_git_bash_rewrite(self):
        self.assertEqual(kr.cli_path("markets"), "/markets")
        self.assertEqual(kr.cli_path("/markets"), "/markets")
        self.assertEqual(kr.cli_path("C:/Program Files/Git/markets/KX-1/orderbook"),
                         "/markets/KX-1/orderbook")
        self.assertEqual(kr.cli_path("markets\\KX-1\\orderbook"), "/markets/KX-1/orderbook")
        with mock.patch.object(kr, "kalshi_get", return_value={}) as kg, \
                redirect_stdout(io.StringIO()):
            kr.main(["C:/Program Files/Git/markets", "status=open"])
        kg.assert_called_once_with("/markets", {"status": "open"}, prefer="signed")

    def test_all_pages_and_a_failure(self):
        with mock.patch.object(kr, "kalshi_get_all", return_value=[1]) as ga, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(kr.main(["/markets", "--all", "markets"]), 0)
        ga.assert_called_once_with("/markets", {}, items_key="markets", prefer="signed")
        with mock.patch.object(kr, "kalshi_get", side_effect=kr.KalshiReadError("x")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(kr.main(["/markets"]), 1)


if __name__ == "__main__":
    unittest.main()
