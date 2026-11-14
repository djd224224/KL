"""--size-mult-file tests for the scratch pvd_wave_tune.py (temp list, no network)."""
import importlib.util, io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name="pvd_wave_tune"):
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


A, B = "KXGOLDEVENT-17NOV260930-T388000", "KXGOLDEVENT-17NOV260930-T390000"


def entry(**kw):
    e = {"bid_max": 40, "ask_min": 60, "size": 200, "bid_size": 2000, "stop": "2099-01-01T00:00:00Z",
         "period_start": "2026-11-13T18:01:49Z", "follow_book": True}
    e.update(kw)
    return e


class TickerMultTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lst = os.path.join(self.d, "list.json")
        json.dump({"markets": {A: entry(), B: entry()}}, open(self.lst, "w"))

    def run_tune(self, mult=None, extra=(), mod_name="pvd_wave_tune"):
        mod = load(mod_name)
        mod.LIST, mod.LOCK = self.lst, os.path.join(self.d, "scan.lock")
        mod.read_touches = lambda ts_: ({}, 0)
        args = ["x", "--series", "KXGOLDEVENT", "--since", "2026-11-13T17:30:00Z", "--write", *extra]
        if mult is not None:
            mf = os.path.join(self.d, "mult.json")
            with open(mf, "w") as fh:
                fh.write(mult if isinstance(mult, str) else json.dumps(mult))
            args += ["--size-mult-file", mf]
        old, sys.argv = sys.argv, args
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self.assertEqual(mod.main(), 0)
        finally:
            sys.argv = old
        with open(self.lst) as fh:
            return json.load(fh)["markets"], buf.getvalue()

    def test_scales_only_the_listed_ticker_once(self):
        L, out = self.run_tune({A: 2.0})
        self.assertEqual((L[A]["size"], L[A]["bid_size"], L[A]["size_mult"]), (400, 4000, 2.0))
        self.assertEqual((L[B]["size"], L[B]["bid_size"]), (200, 2000))
        self.assertNotIn("size_mult", L[B])
        self.assertIn("1 markets with their own multiple", out)
        L, _ = self.run_tune({A: 2.0})                          # idempotent
        self.assertEqual((L[A]["size"], L[A]["bid_size"]), (400, 4000))

    def test_dropping_a_ticker_scales_it_back(self):
        self.run_tune({A: 2.5})
        L, _ = self.run_tune({})
        self.assertEqual((L[A]["size"], L[A]["bid_size"]), (200, 2000))
        self.assertNotIn("size_mult", L[A])

    def test_combines_with_the_wave_multiple(self):
        L, _ = self.run_tune({A: 2.0}, extra=("--size-mult", "1.5"))
        self.assertEqual((L[A]["size"], L[A]["size_mult"]), (600, 3.0))
        self.assertEqual((L[B]["size"], L[B]["size_mult"]), (300, 1.5))

    def test_zero_is_off(self):
        L, out = self.run_tune({A: 0, B: 2.0})
        self.assertEqual(L[A]["size"], 0)
        # both sides 0: the bot reads "size": 0 alone as its default 40 a side
        self.assertEqual((L[A]["bid_size"], L[A]["ask_size"]), (0, 0))
        self.assertEqual(L[B]["size"], 400)
        L, _ = self.run_tune({A: 0, B: 2.0})                    # idempotent
        self.assertEqual((L[A]["size"], L[A]["bid_size"], L[A]["ask_size"]), (0, 0, 0))

    def test_an_old_size_only_off_gets_both_sides_zeroed(self):
        # an entry pinned off before the fix: "size": 0, no side sizes, sizes in "pre_off"
        L, _ = self.run_tune({A: 0})
        for k in ("bid_size", "ask_size", "cheap_sides"):
            L[A].pop(k, None)
        json.dump({"markets": L}, open(self.lst, "w"))
        L, _ = self.run_tune({A: 0})
        self.assertEqual((L[A]["size"], L[A]["bid_size"], L[A]["ask_size"]), (0, 0, 0))
        L, _ = self.run_tune({A: 1.0})                          # lifting still restores
        self.assertEqual((L[A]["size"], L[A]["bid_size"]), (200, 2000))
        self.assertNotIn("ask_size", L[A])

    def test_zero_then_back_on_restores_the_sizes(self):
        self.run_tune({A: 2.0})
        L, _ = self.run_tune({A: 0})
        self.assertEqual(L[A]["size"], 0)
        L, _ = self.run_tune({A: 5.0})
        self.assertEqual((L[A]["size"], L[A]["bid_size"], L[A]["size_mult"]), (1000, 10000, 5.0))
        self.assertNotIn("pinned", L[A])
        self.assertNotIn("ask_size", L[A])

    def test_unreadable_file_is_x1(self):
        L, out = self.run_tune("{not json")
        self.assertEqual(L[A]["size"], 200)



if __name__ == "__main__":
    unittest.main()
