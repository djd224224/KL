"""pvd_waves.py pick-off gates (Jack 2026-11-08: "the pickoffs yesterday were
unacceptable", "Ensure this doesn't happen again"): preflight, add --auto,
pickoffs, setarg --unset VALUE. No network: kalshi_reads and incentive_mm are
stand-ins; every path points into a temp dir."""
import argparse
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
import contextlib
from contextlib import redirect_stdout
from datetime import datetime, timezone

TMP = tempfile.mkdtemp(prefix="pvdw_test_")
os.environ["PVD_WAVES_REGISTRY"] = os.path.join(TMP, "waves.json")
os.environ["PVD_PICKOFF_STATE"] = os.path.join(TMP, "pickoff_state.json")
import pvd_waves as pw  # noqa: E402

INC = os.path.join(TMP, "inc")
os.makedirs(INC, exist_ok=True)
pw.INC = INC
pw.STATE_FILE = os.path.join(INC, "imm_state.json")
pw.LIST = os.path.join(INC, "post_vote_dead_books.json")
pw.LAUNCHER = os.path.join(TMP, "run_incentive_mm.ps1")
pw.TASK_CMD = os.path.join(TMP, "run_pvd_scan_waves.cmd")

BOOKS = {}          # ticker -> orderbook response
ORDERS = []         # resting orders
BAL = {"balance_breakdown": [{"exchange_index": 0, "balance": 9000.0}], "balance_dollars": "9500"}


def _fake_reads():
    m = types.ModuleType("kalshi_reads")
    m.read = lambda path, params=None, timeout=None: BOOKS[path.split("/")[2]]
    m.kalshi_get = lambda path, params=None, prefer="signed", timeout=None: BAL
    m.kalshi_get_all = lambda path, params=None, items_key="markets", **k: list(ORDERS)
    return m


sys.modules["kalshi_reads"] = _fake_reads()
_imm = types.ModuleType("incentive_mm")
_imm.company_kpi_series = lambda s: s in ("KXCCLA",)
_imm.series_blocklisted = lambda s: s in ("KXCCLA", "KXBLOCKED")
sys.modules["incentive_mm"] = _imm

NOW = datetime.now(timezone.utc)
STAMP = NOW.strftime("%Y-%m-%d %H:%M:%SZ")
GOOD_START = [
    f"{STAMP} [IMM] account balance $10,000.00 cash, $70,000.00 account value",
    f"{STAMP} event sweep breaker (2026-10-04): on -- a maker fill that clears our order pulls ...",
    f"{STAMP} tape trigger (2026-11-01): ab -- THE LIVE A/B ...",
    f"{STAMP} bait-fingerprint stop (2026-10-22): on -- a fill that takes our whole rung ...",
    f"{STAMP} join wait (2026-11-02): on -- an at-ref rung joins ...; penny and sub-penny books "
    f"(2026-11-08); a side with no rung starts from where it last rested (24 h memory)",
    f"{STAMP} company KPIs (2026-11-08): OFF -- every series Kalshi tags KPIs ...",
    f"{STAMP} net halt (2026-11-08): off (IMM_NET_LOSS_LIMIT=0)",
    f"{STAMP} off-usual size (2026-11-08): on -- a listed market's side ...",
    f"{STAMP} close clamp (2026-11-08): on -- the side that reduces a position ...",
    f"{STAMP} lone-reference guard on the wave list (2026-11-08): on -- a listed wave market runs ...",
    f"{STAMP} unwind-only waves scope (2026-11-08): existing waves -- UNWIND_ONLY_WAVES covers ...",
    f"{STAMP} [IMM] 1036/1204 mkts quoted, 1815 resting, 8 amend",
]


def write_bot(lines=GOOD_START, halted_until=0.0, own_pos=None):
    with open(os.path.join(INC, "incentive-mm-2026-11-08.log"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(pw.STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"halted_until": halted_until, "own_pos": own_pos or {}}, fh)
    for fn in ("HALT", "UNWIND_ONLY", "UNWIND_ONLY_WAVES", "UNWIND_ONLY_EXCEPT_WAVES"):
        p = os.path.join(INC, fn)
        if os.path.exists(p):
            os.remove(p)


def detect(**over):
    rows = {"KXGOOD": ("FIT", 8), "KXNEAR": ("NEAR", 6), "KXONE": ("FEW", 1), "KXCCLA": ("FIT", 1),
            "KXTV": ("CHECK", 9)}
    rows.update(over)
    return {"series": [{"series": s, "verdict": v, "events": n, "markets": n * 3, "pool_day": 100.0,
                        "why": [], "category": "Politics"} for s, (v, n) in rows.items()]}


def statuses(checks, prefix):
    return [c[0] for c in checks if c[1].startswith(prefix)]


class PreflightTests(unittest.TestCase):
    def setUp(self):
        write_bot()
        with open(pw.LAUNCHER, "w", encoding="utf-8") as fh:
            fh.write('$ProbeEnv = ""\n    $ProbeEnv = "set IMM_DAILY_LOSS_LIMIT=5000&& set IMM_SWEEP_BREAKER=on&&"\n')

    def test_a_healthy_bot_passes(self):
        cs = pw.guard_checks()
        self.assertFalse([c for c in cs if c[0] == pw.FAIL], cs)
        for name in ("join wait", "off-usual size", "sweep breaker", "tape trigger", "company KPIs off",
                     "bot cycling", "shard-0 cash"):
            self.assertEqual(statuses(cs, name), [pw.PASS], name)

    def test_switch_files_halt_and_a_missing_guard_fail(self):
        # UNWIND_ONLY_WAVES: a bot that scopes it to the existing waves lets a new one quote
        open(os.path.join(INC, "UNWIND_ONLY_WAVES"), "w").close()
        self.assertEqual(statuses(pw.guard_checks(), "switch UNWIND_ONLY_WAVES"), [pw.INFO])
        write_bot([ln for ln in GOOD_START if "unwind-only waves scope" not in ln])
        open(os.path.join(INC, "UNWIND_ONLY_WAVES"), "w").close()
        self.assertEqual(statuses(pw.guard_checks(), "switch UNWIND_ONLY_WAVES"), [pw.FAIL])
        # the close clamp and the lone-ref guard on the wave list are guards too
        write_bot([ln for ln in GOOD_START if "close clamp" not in ln])
        self.assertEqual(statuses(pw.guard_checks(), "close clamp"), [pw.FAIL])
        write_bot([ln.replace("wave list (2026-11-08): on", "wave list (2026-11-08): dry") for ln in GOOD_START])
        self.assertEqual(statuses(pw.guard_checks(), "lone-ref guard on waves"), [pw.FAIL])
        write_bot(halted_until=time.time() + 3600)
        self.assertEqual(statuses(pw.guard_checks(), "bot halted"), [pw.FAIL])
        # the join wait without sub-penny books (11/07's gap) or without rest memory
        write_bot([ln.replace("penny and sub-penny books", "penny books only") for ln in GOOD_START])
        self.assertEqual(statuses(pw.guard_checks(), "join wait"), [pw.FAIL])
        write_bot([ln for ln in GOOD_START if "off-usual" not in ln])
        self.assertEqual(statuses(pw.guard_checks(), "off-usual size"), [pw.FAIL])

    def test_low_shard0_cash_fails(self):
        BAL["balance_breakdown"][0]["balance"] = 1500.0
        try:
            self.assertEqual(statuses(pw.guard_checks(), "shard-0 cash"), [pw.FAIL])
        finally:
            BAL["balance_breakdown"][0]["balance"] = 9000.0

    def test_wave_args(self):
        w = {"series": ["KXGOOD"], "tune": list(pw.PRESETS["default"]), "near": [], "near_tune": pw.PRESETS["near"]}
        self.assertFalse([c for c in pw.wave_checks("w", w, auto=True) if c[0] == pw.FAIL])
        w28 = dict(w, tune=list(pw.PRESETS["2028"]))
        fails = [c[1] for c in pw.wave_checks("w", w28, auto=True) if c[0] == pw.FAIL]
        self.assertTrue(any("--no-halts" in f for f in fails) and any("--size-mult" in f for f in fails))
        self.assertTrue(any("cheap tiers" in f for f in fails))
        # manual mode: the same are WARNs (Jack's word), not FAILs
        self.assertFalse([c for c in pw.wave_checks("w", w28) if c[0] == pw.FAIL])
        # the old default's mid-price tiers are now above the default
        old = dict(w, tune=[x if x != "2:10,5:5" else "2:10,5:5,15:3,30:2,70:1.5" for x in pw.PRESETS["default"]])
        self.assertEqual(statuses(pw.wave_checks("w", old, auto=True), "w main cheap tiers"), [pw.FAIL])
        # an uncapped re-anchor always fails
        unc = dict(w, tune=list(pw.PRESETS["default"]) + ["--reanchor-step", "0"])
        self.assertEqual(statuses(pw.wave_checks("w", unc), "w main re-anchor cap"), [pw.FAIL])

    def test_90c_side_cap(self):
        """Jack 2026-11-08: "dont preset that" -- no preset caps 90c+ sides; a cap
        that rests a probe-sized lot there (1-9, under first fit) still fails, the
        1-lot the 11/07 bait sweeps took ("why even have a probe?")"""
        w = {"series": ["KXGOOD"], "tune": list(pw.PRESETS["default"]), "near": [], "near_tune": pw.PRESETS["near"]}
        self.assertFalse(any("--side-cap" in pw.PRESETS[k] for k in ("default", "near", "2028")))
        self.assertTrue(all("--twin-block" in pw.PRESETS[k] for k in ("default", "near", "2028")))
        cap = lambda *v: dict(w, tune=[x for c in v for x in ("--side-cap", c)] + list(pw.PRESETS["default"]))
        st = lambda ww, auto=True: statuses(pw.wave_checks("w", ww, auto=auto), "w main 90c+ side cap")
        self.assertEqual(st(w), [pw.PASS])                       # no cap: full size
        self.assertEqual(st(cap("ALL:90:1")), [pw.FAIL])
        self.assertEqual(st(cap("ALL:90:1"), auto=False), [pw.WARN])
        # first fit: a 95c cap at 1 listed ahead of the 90c one still leaves 95-99c a 1-lot
        self.assertEqual(st(cap("ALL:95:1", "ALL:90:0")), [pw.FAIL])
        self.assertEqual(st(cap("ALL:90:0")), [pw.PASS])          # off
        self.assertEqual(st(cap("ALL:90:50")), [pw.PASS])         # a real size

    def test_tune_stamps_the_wave_added_and_the_report_lists_every_lever(self):
        """the bot's UNWIND_ONLY_WAVES covers only waves added before it (the tune
        writes wave_added); Step 7 (Jack 2026-11-08: "make sure to provide a table of
        the presets of all the parameters to tune, be thorough")"""
        w = {"series": ["KXGOOD"], "near": ["KXNEAR"], "since": "2026-11-09T00:00:00Z", "port": 8790,
             "dir": "pvd-watch-w", "scan_size": 200, "budget": 50000, "stop_cap": None, "preset": "default",
             "tune": list(pw.PRESETS["default"]) + ["--pin", "KXGOOD-X:200"], "near_tune": list(pw.PRESETS["near"]),
             "near_scan": {"live": 80, "dead": 120, "decided": 300}, "enabled": True,
             "added": "2026-11-09T01:00:00Z"}
        cmd = pw.tune_cmd("w", w, False)
        self.assertEqual(cmd[cmd.index("--wave-added") + 1], "2026-11-09T01:00:00Z")
        self.assertNotIn("--wave-added", pw.tune_cmd("w", dict(w, added=None), False))
        txt = "\n".join(pw.param_table("w", w))
        for flag, *_r in pw.LEVERS:
            self.assertEqual(txt.count(f"| `{flag}` |"), 2, flag)    # main and near
        self.assertIn("| `--side-cap` | not set: none: sides tying up 90c+ quote like any other |", txt)
        self.assertIn("| `--pin` | KXGOOD-X:200 |", txt)
        self.assertIn("| `--cheap-tiers` | 2:10,5:5 |", txt)
        for nm, _mk in pw.BOT_PARAM_LINES:
            self.assertIn(f"| {nm} |", txt)
        self.assertIn("| close clamp | on -- the side that reduces a position ... |", txt)
        self.assertIn("| daily-loss halt ($ of P&L today) | 5000 |", txt)
        self.assertIn("| switch HALT | off |", txt)

    def test_scope(self):
        cs = pw.scope_checks(["KXGOOD", "KXONE", "KXCCLA", "KXTV", "KXNEW"], ["KXNEAR"], detect(), auto=True)
        by = {c[1]: c[0] for c in cs}
        self.assertEqual(by["scope KXGOOD"], pw.PASS)
        self.assertEqual(by["scope KXNEAR"], pw.PASS)
        self.assertEqual(by["scope KXONE"], pw.FAIL)        # 1 event: not a wave
        self.assertEqual(by["scope KXCCLA"], pw.FAIL)       # company KPI
        self.assertEqual(by["scope KXTV"], pw.FAIL)         # CHECK: Jack's call
        self.assertEqual(by["scope KXNEW"], pw.FAIL)        # not in the detector output

    def test_legacy_passes_are_reported(self):
        with open(pw.TASK_CMD, "w", encoding="utf-8") as fh:
            fh.write('echo ===== %date% %time% pass 4: 2028 band >> "%LOG%"\n'
                     '"%PY%" "%KL%\\pvd_wave_tune.py" --series %W28% --no-halts --size-mult 1.5 '
                     '--series-tier KXDPRESPRIMARY:bid:2:135 --cheap-tiers 2:20 --write >> "%LOG%" 2>&1\n')
        (row,) = pw.legacy_checks()
        self.assertIn("--no-halts", row[2])
        self.assertIn("x135", row[2])


class ReportEmailTests(unittest.TestCase):
    """report --email (Jack 2026-11-08: "send me a test email report for wave-tool")."""

    TEXT = "\n".join([
        "# Wave `w` -- parameters (2026-11-09 02:30Z)",
        "",
        "## Listing",
        "- Series (2): KXA, KXB",
        "  - pacing 2h: 3",
        "- listed now **4** markets",
        "",
        "| status | check | detail |",
        "|---|---|---|",
        "| PASS | sweep breaker | on <fast> |",
        "| FAIL | 90c+ side cap | `--side-cap ALL:90:1` |",
        "",
        "1. Size: `--size-mult`",
        "2. Breakers",
    ])

    def test_html(self):
        h = pw.md_to_html(self.TEXT, banner="TEST -- by hand")
        self.assertIn("TEST -- by hand", h)
        self.assertIn(">Wave <code", h)
        self.assertIn("<h2 ", h)
        self.assertIn("<b>4</b>", h)
        self.assertIn("margin-left:16px", h)                     # the nested item
        self.assertEqual(h.count("<tr>"), 3)                       # head + 2 rows, no separator row
        self.assertIn("on &lt;fast&gt;", h)                        # escaped
        self.assertIn("color:#b91c1c", h)                          # FAIL in red
        self.assertIn("--side-cap ALL:90:1</code>", h)
        self.assertEqual(h.count("<ol "), 1)
        self.assertEqual(h.count("<ul "), 1)

    def test_subject(self):
        now = datetime(2026, 11, 9, 2, 30, tzinfo=timezone.utc)
        s = pw.report_subject("w", {"preflight": "PASS", "quoted": 3, "listed": 5, "rate": 120.4,
                                    "pickoffs": 1, "pickoffs_usd": 40}, now, test=True)
        self.assertEqual(s, "[TEST] Wave w report 11/08 9:30 PM ET: preflight PASS, quoting 3 of 5, "
                            "est $120/day, 1 pick-off flagged ($40)")
        s = pw.report_subject("w", {"preflight": "FAIL", "pickoffs": 0}, now)
        self.assertEqual(s, "Wave w report 11/08 9:30 PM ET: preflight FAIL, dashboard down, 0 pick-offs flagged")

    def test_send(self):
        sent = []

        class FakeAlerter:
            enabled = True

            def send_message(self, body, subject="", html=None):
                sent.append((body, subject, html))
                return True
        old = pw._report_alerter
        pw._report_alerter = lambda: FakeAlerter()
        try:
            with redirect_stdout(io.StringIO()):
                rc = pw.send_report_email("w", self.TEXT, {"preflight": "PASS"}, test=True)
            self.assertEqual(rc, 0)
            (body, subj, html), = sent
            self.assertEqual(body, self.TEXT)
            self.assertTrue(subj.startswith("[TEST] Wave w report "))
            self.assertIn("<table", html)
            self.assertIn("sent by hand", html)
            FakeAlerter.enabled = False
            with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(pw.send_report_email("w", self.TEXT, {}), 1)
            self.assertEqual(len(sent), 1)                           # nothing more sent
        finally:
            pw._report_alerter = old


class AutoAddTests(unittest.TestCase):
    def setUp(self):
        write_bot()
        with open(os.environ["PVD_WAVES_REGISTRY"], "w", encoding="utf-8") as fh:
            json.dump({"waves": {}}, fh)
        self.det = os.path.join(TMP, "detect.json")
        with open(self.det, "w", encoding="utf-8") as fh:
            json.dump(detect(), fh)

    def add(self, *argv):
        ap = argparse.Namespace(name="w1108-x", series="", since="2026-11-08T04:30:00Z", go_live=None, port=8790,
                                budget=50000.0, scan_size=200, stop_cap=None, preset="default", tune_extra=None,
                                near="", near_live=80, near_dead=120, note="auto", replace=False, auto=True,
                                detect=self.det)
        for k, v in argv:
            setattr(ap, k, v)
        with redirect_stdout(io.StringIO()) as out:
            rc = pw.cmd_add(ap)
        return rc, out.getvalue(), pw.load()["waves"]

    def test_a_clean_wave_is_added(self):
        rc, out, waves = self.add(("series", "KXGOOD"), ("near", "KXNEAR"))
        self.assertEqual(rc, 0, out)
        self.assertIn("w1108-x", waves)

    def test_a_failed_gate_writes_nothing(self):
        for argv in ([("series", "KXGOOD,KXONE")],                 # a 1-event series
                     [("series", "KXGOOD"), ("preset", "2028")],   # Jack-only preset
                     [("series", "KXGOOD"), ("budget", 90000.0)],
                     [("series", "KXGOOD"), ("tune_extra", "--no-halts")]):
            rc, out, waves = self.add(*argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn("NOT ADDED", out)
            self.assertEqual(waves, {}, argv)
        open(os.path.join(INC, "HALT"), "w").close()
        rc, out, waves = self.add(("series", "KXGOOD"))
        self.assertEqual((rc, waves), (1, {}))


def fill(t, side, n, px, pb, ts_off=600, fid=None):
    pa = pb + n if side == "bid" else pb - n
    return {"ts": time.time() - ts_off, "ticker": t, "our_book_side": side, "count": n, "yes_price_cents": px,
            "pos_before": pb, "pos_after": pa, "fill_id": fid or f"{t}-{side}-{px}-{n}"}


def book(bids, asks):
    """bids / asks in YES cents -> an orderbook_fp response (asks as NO bids)."""
    return {"orderbook_fp": {"yes_dollars": [[f"{p / 100:.4f}", str(q)] for p, q in bids],
                             "no_dollars": [[f"{(100 - p) / 100:.4f}", str(q)] for p, q in asks]}}


class PickoffTests(unittest.TestCase):
    def setUp(self):
        write_bot(own_pos={"KXHOUSERACE-IN04-28-D": 1273})
        reg = {"waves": {"wtest": {"series": ["KXWAVE"], "since": "2026-11-08T00:00:00Z", "go_live": "2026-11-08T00:00:00Z",
                                   "port": 8790, "dir": "pvd-watch-wtest", "scan_size": 200, "budget": 0,
                                   "stop_cap": None, "preset": "default", "tune": list(pw.PRESETS["default"]),
                                   "near": [], "near_tune": list(pw.PRESETS["near"]), "enabled": True}}}
        with open(os.environ["PVD_WAVES_REGISTRY"], "w", encoding="utf-8") as fh:
            json.dump(reg, fh)
        with open(pw.LIST, "w", encoding="utf-8") as fh:
            json.dump({"markets": {"KXHOUSERACE-IN04-28-D": {"event": "KXHOUSERACE-IN04-28", "stop": "2026-12-01T00:00:00Z"}}}, fh)
        with open(pw.TASK_CMD, "w", encoding="utf-8") as fh:
            fh.write("set W28=KXHOUSERACE,KXDPRESPRIMARY\n")
        day = datetime.now(timezone.utc).date().isoformat()
        fills = [fill("KXHOUSERACE-IN04-28-D", "bid", 1273, 40.0, 0),          # baited: book back at 4-7
                 fill("KXWAVE-A-1", "ask", 300, 60.0, 0),                       # 30c against us
                 fill("KXWAVE-B-1", "bid", 200, 10.0, 0),                       # fine: 1c against
                 fill("KXWAVE-A-1", "bid", 300, 50.0, -300, ts_off=300),        # REDUCING: not counted
                 fill("KXOTHER-X", "bid", 500, 50.0, 0)]                        # not a wave market
        with open(os.path.join(INC, f"fills_{day}.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(f) for f in fills) + "\n")
        BOOKS.clear()
        BOOKS["KXHOUSERACE-IN04-28-D"] = book([(3.9, 500), (40.0, 1)], [(7.1, 400)])   # our 40c... and a 1-lot
        BOOKS["KXWAVE-A-1"] = book([(89, 100)], [(91, 100)])
        BOOKS["KXWAVE-B-1"] = book([(8.5, 100)], [(9.5, 100)])
        ORDERS[:] = [{"ticker": "KXHOUSERACE-IN04-28-D", "side": "yes", "action": "buy",
                      "yes_price_dollars": "0.4000", "remaining_count_fp": "1"}]     # OUR 40c bid: taken out
        if os.path.exists(os.environ["PVD_PICKOFF_STATE"]):
            os.remove(os.environ["PVD_PICKOFF_STATE"])

    def args(self, **kw):
        a = argparse.Namespace(name="all", hours=6.0, min_c=5.0, min_usd=25.0, series_trip=3, act=False,
                               update_state=True, top=10, json=os.path.join(TMP, "po.json"))
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_flags_adding_fills_marked_without_our_orders(self):
        with redirect_stdout(io.StringIO()):
            rc = pw.cmd_pickoffs(self.args())
        self.assertEqual(rc, 3)
        res = json.load(open(os.path.join(TMP, "po.json")))
        got = {g["ticker"]: g for g in res["rows"]}
        self.assertNotIn("KXOTHER-X", got)
        hr = got["KXHOUSERACE-IN04-28-D"]
        self.assertEqual(hr["ext_bid"], 3.9)              # our 40c bid is not the market
        self.assertAlmostEqual(hr["mark_c"], 5.5)
        self.assertTrue(hr["flag"])
        self.assertAlmostEqual(hr["adverse_usd"], 1273 * 34.5 / 100, places=2)
        self.assertEqual(hr["wave"], "2028 election wave (pass 4)")
        self.assertFalse(hr["registry"])
        a = got["KXWAVE-A-1"]
        self.assertEqual((a["contracts"], a["flag"]), (300, True))   # the reducing buy is left out
        self.assertFalse(got["KXWAVE-B-1"]["flag"])
        # the state remembers them: a second run has nothing new
        with redirect_stdout(io.StringIO()):
            self.assertEqual(pw.cmd_pickoffs(self.args()), 0)

    def test_act_pins_a_registry_market_at_base_only(self):
        pw.run_one = lambda cmd, env=None, tail=4: 0        # no real re-tune
        with redirect_stdout(io.StringIO()):
            pw.cmd_pickoffs(self.args(act=True))
        tune = pw.load()["waves"]["wtest"]["tune"]
        self.assertIn("KXWAVE-A-1:200", pw._vals(tune, "--pin"))
        self.assertNotIn("KXHOUSERACE-IN04-28-D:200", " ".join(tune))     # legacy: hand edit only
        res = json.load(open(os.path.join(TMP, "po.json")))
        self.assertEqual([x["ticker"] for x in res["acted"]], ["KXWAVE-A-1"])

    def test_external_touch_and_adding_count(self):
        self.assertEqual(pw.external_touch([[5.9, 100.0], [6.0, 30.0]], [[93.9, 50.0], [90.0, 10.0]],
                                           [("bid", 6.0, 30.0)]), (5.9, 6.1))
        self.assertEqual(pw.adding_count({"pos_before": 0, "pos_after": 200}), 200)
        self.assertEqual(pw.adding_count({"pos_before": -100, "pos_after": 150}), 150)
        self.assertEqual(pw.adding_count({"pos_before": 300, "pos_after": 100}), 0)


class SetargTests(unittest.TestCase):
    def test_unset_one_pin_of_several(self):
        with open(os.environ["PVD_WAVES_REGISTRY"], "w", encoding="utf-8") as fh:
            json.dump({"waves": {"w": {"series": ["S"], "tune": ["--pin", "A:200", "--pin", "B:200"]}}}, fh)
        with redirect_stdout(io.StringIO()):
            pw.cmd_setarg(argparse.Namespace(name="w", flag="pin", value="A:200", unset=True, group="main"))
        self.assertEqual(pw.load()["waves"]["w"]["tune"], ["--pin", "B:200"])


class StopTests(unittest.TestCase):
    def test_stop_moves_live_entries_and_disables(self):
        pw.SCAN_LOCK = os.path.join(TMP, "scan.lock")
        with open(os.environ["PVD_WAVES_REGISTRY"], "w", encoding="utf-8") as fh:
            json.dump({"waves": {"w": {"series": ["KXA"], "near": ["KXN"], "enabled": True}}}, fh)
        with open(pw.LIST, "w", encoding="utf-8") as fh:
            json.dump({"markets": {"KXA-1-X": {"stop": "2099-01-01T00:00:00Z"},
                                   "KXN-1-X": {"stop": "2099-01-01T00:00:00Z"},
                                   "KXA-2-X": {"stop": "2001-01-01T00:00:00Z"},
                                   "KXB-1-X": {"stop": "2099-01-01T00:00:00Z"}}}, fh)
        write_bot(own_pos={"KXA-1-X": 50})
        with redirect_stdout(io.StringIO()) as out:
            pw.cmd_stop(argparse.Namespace(name="w", reason="test", dry_run=False))
        d = json.load(open(pw.LIST, encoding="utf-8"))
        self.assertEqual(sorted(d["markets"]), ["KXA-2-X", "KXB-1-X"])
        self.assertEqual(sorted(d["stopped"]), ["KXA-1-X", "KXN-1-X"])
        self.assertIn("1 held position(s) ride", out.getvalue())
        self.assertFalse(pw.load()["waves"]["w"]["enabled"])
        self.assertFalse(os.path.exists(pw.SCAN_LOCK))


class DarkcheckTests(unittest.TestCase):
    """Jack 2026-11-11: "ELI5 why market-wave didnt autoquote last night, and what
    needs to be fixed so that never happens again" -> "yes build all 3. make no
    mistakes, this is serious". The 11/11 relight listed 724 markets that the
    old-wave lock kept dark; darkcheck compares the list with Kalshi."""

    def setUp(self):
        write_bot()
        self.now = datetime.now(timezone.utc)
        z = lambda h: (self.now - __import__("datetime").timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.z = z
        sw = os.path.join(INC, "UNWIND_ONLY_WAVES")
        open(sw, "w").close()
        t_sw = self.now.timestamp() - 3 * 86400
        os.utime(sw, (t_sw, t_sw))
        stop = z(-48)
        L = {
            "KXAAA-28-A": {"stop": stop, "period_start": z(2)},                           # old wave, flat: LOCK
            "KXAAA-28-B": {"stop": stop, "period_start": z(2), "wave_added": z(1)},       # stamped, two-sided
            "KXCCC-28-C": {"stop": stop, "period_start": z(2), "wave_added": z(1)},       # stamped, dark: selection
            "KXAAA-28-D": {"stop": stop, "period_start": z(2), "price_min": 99, "price_max": 1,
                           "tpause": {"mode": "off", "pc": 0}},                             # paused: on purpose
            "KXBBB-28-E": {"stop": stop, "period_start": z(2)},                           # old wave, held
            "KXBBB-28-F": {"stop": stop, "period_start": z(0.05)},                        # 3 min old: too new
            "KXBBB-28-G": {"stop": z(1), "period_start": z(30)},                          # ended
            "KXBBB-28-H": {"stop": stop, "period_start": z(2), "wave_added": z(1)},       # one side
            "KXBBB-28-I": {"stop": stop, "period_start": z(2)},                           # old wave, flat: LOCK
        }
        with open(pw.LIST, "w", encoding="utf-8") as fh:
            json.dump({"markets": L}, fh)
        with open(os.environ["PVD_WAVES_REGISTRY"], "w", encoding="utf-8") as fh:
            json.dump({"waves": {"wtest": {"series": ["KXAAA"], "near": [], "since": z(30), "enabled": True,
                                           "tune": list(pw.PRESETS["default"]), "port": 8790,
                                           "dir": "pvd-watch-wtest", "added": z(1)}}}, fh)
        self.resting = {"KXAAA-28-B": {"bid", "ask"}, "KXBBB-28-H": {"bid"}}
        self.bot = {"started": self.now.timestamp() - 3600, "cycled": self.now.timestamp() - 30}

    def tearDown(self):
        sw = os.path.join(INC, "UNWIND_ONLY_WAVES")
        if os.path.exists(sw):
            os.remove(sw)

    def scan(self, **kw):
        a = argparse.Namespace(min_age=20.0)
        args = dict(now=self.now, resting=self.resting, own_pos={"KXBBB-28-E": 40.0},
                    selection={"KXCCC-28-C": "payout_floor"}, bot=self.bot, guards={}, statuses={},
                    holds={})
        args.update(kw)
        return pw.dark_scan(a, pw.load(), **args)

    def test_it_sorts_every_listed_market_and_names_the_reason(self):
        res = self.scan()
        self.assertIsNone(res["deferred"])
        self.assertEqual((res["listed"], res["checked"], res["too_new"]), (8, 7, 1))
        self.assertEqual((res["two_sided"], res["one_sided"], res["dark"]), (1, 1, 5))
        kinds = {r["ticker"]: r["kind"] for r in res["dark_rows"]}
        self.assertEqual(kinds, {"KXAAA-28-A": "lock", "KXBBB-28-I": "lock", "KXCCC-28-C": "bot",
                                 "KXAAA-28-D": "off", "KXBBB-28-E": "held"})
        why = {r["ticker"]: r["reason"] for r in res["dark_rows"]}
        self.assertTrue(why["KXAAA-28-A"].startswith(pw.LOCK_REASON))
        self.assertTrue(why["KXCCC-28-C"].startswith("under the payout floor"))
        # a guard standing it aside (guard_skips) is the bot's own reason; selected with
        # no guard on record, or no record at all, is unexplained
        os.remove(os.path.join(INC, "UNWIND_ONLY_WAVES"))
        res = self.scan(selection={"KXAAA-28-A": "selected", "KXBBB-28-I": "selected"},
                        guards={"KXAAA-28-A": "cannot_qualify"})
        got = {r["ticker"]: (r["kind"], r["reason"]) for r in res["dark_rows"]}
        self.assertEqual(got["KXAAA-28-A"][0], "bot")
        self.assertTrue(got["KXAAA-28-A"][1].startswith("the bot's depth gate"))
        self.assertEqual(got["KXBBB-28-I"][0], "unexplained")
        self.assertEqual(self.scan(selection={})["dark_rows"][0]["kind"], "unexplained")
        # a market closed on Kalshi (settled, still listed until its old stop): nothing to
        # quote -- never an alert (11/11 14:17Z: 17 settled KXSTATELEG / KXPASEN texted)
        res = self.scan(selection={}, statuses={"KXAAA-28-A": "finalized", "KXBBB-28-I": "active"})
        got = {r["ticker"]: (r["kind"], r["reason"]) for r in res["dark_rows"]}
        self.assertEqual(got["KXAAA-28-A"], ("off", "closed on Kalshi (finalized): nothing to quote"))
        self.assertEqual(got["KXBBB-28-I"][0], "unexplained")

    def test_a_breaker_hold_on_the_event_is_the_bots_reason(self):
        """11/11 15:21Z: the sweep breaker held KXDPRESPRIMARY-28KY 600 s after ABES's
        200 ask filled; the 15:31Z check, seconds after the hold ended, texted 10 dark"""
        nts = self.now.timestamp()
        st = {"sweep_esc": {"KXCCC-28": {"n": 1, "hold_until": nts - 30, "reset_at": nts + 1800}},
              "sweep_until": {"KXOLD-1": nts - 3600},
              "toxic_halt_until": {"KXDDD-28-X|bid": nts + 600}}
        holds = pw.active_holds(st, nts)
        self.assertEqual(sorted(holds), ["KXCCC-28", "KXDDD-28-X|bid"])     # an hour-old one is gone
        self.assertEqual(pw.hold_for("KXCCC-28-C", holds)[0], "sweep_esc")
        self.assertEqual(pw.hold_for("KXDDD-28-X", holds)[0], "toxic_halt_until")
        self.assertIsNone(pw.hold_for("KXEEE-28-A", holds))
        res = self.scan(selection={"KXCCC-28-C": "selected"}, holds=holds)
        got = {r["ticker"]: (r["kind"], r["reason"]) for r in res["dark_rows"]}
        self.assertEqual(got["KXCCC-28-C"][0], "bot")
        self.assertTrue(got["KXCCC-28-C"][1].startswith("the bot's sweep_esc hold on its event"))

    def test_no_verdict_right_after_a_restart_or_with_the_bot_down(self):
        res = self.scan(bot={"started": self.now.timestamp() - 120, "cycled": self.now.timestamp() - 10})
        self.assertIn("restarted", res["deferred"])
        self.assertEqual(res["dark_rows"], [])
        res = self.scan(bot={"started": self.now.timestamp() - 9000, "cycled": self.now.timestamp() - 1200})
        self.assertIn("not logged a cycle", res["deferred"])

    def test_act_retunes_a_registry_series_and_adopts_the_rest(self):
        res = self.scan()
        calls = []
        orig = pw.run_one
        pw.run_one = lambda cmd, env=None, tail=4: calls.append(cmd) or 0
        try:
            with redirect_stdout(io.StringIO()):
                dry = pw.dark_act(res, pw.load(), write=False)
                self.assertEqual(calls, [])
                done = pw.dark_act(res, pw.load(), write=True)
        finally:
            pw.run_one = orig
        self.assertEqual([(x["action"], x["series"]) for x in dry],
                         [("retuned", ["KXAAA"]), ("adopted", ["KXBBB"])])
        self.assertEqual([(x["action"], x["wave"], x["series"], x["markets"]) for x in done],
                         [("retuned", "wtest", ["KXAAA"], 1), ("adopted", done[1]["wave"], ["KXBBB"], 1)])
        # the retune runs the wave's own tune, on the dark series only, stamping wave_added
        self.assertEqual(len(calls), 2)
        self.assertIn("--wave-added", calls[0])
        self.assertEqual(calls[0][calls[0].index("--series") + 1], "KXAAA")
        # the adoption is a registry wave on the default preset, stamped by its tune
        w = pw.load()["waves"][done[1]["wave"]]
        self.assertTrue(done[1]["wave"].endswith("-adopt"))
        self.assertEqual((w["series"], w["preset"], w["tune"], w["enabled"]),
                         (["KXBBB"], "default", list(pw.PRESETS["default"]), True))
        self.assertEqual(calls[1][calls[1].index("--series") + 1], "KXBBB")
        self.assertIn("--wave-added", calls[1])

    def test_a_failed_preflight_adopts_nothing(self):
        res = self.scan()
        open(os.path.join(INC, "HALT"), "w").close()
        orig = pw.run_one
        pw.run_one = lambda cmd, env=None, tail=4: 0
        try:
            with redirect_stdout(io.StringIO()):
                done = pw.dark_act(res, pw.load(), write=True)
        finally:
            pw.run_one = orig
            os.remove(os.path.join(INC, "HALT"))
        self.assertEqual([x["action"] for x in done], ["retuned", "not adopted"])
        self.assertNotIn("adopt", " ".join(pw.load()["waves"]))

    def _twice(self, res1, res2=None, gap_min=20.0):
        """cmd_darkcheck on res1, then -- the state's previous check moved gap_min
        back -- on res2 (default res1); returns (rc1, rc2, output)."""
        st_path = os.path.join(TMP, "darkcheck_state.json")
        orig_scan, orig_state = pw.dark_scan, pw.DARK_STATE
        pw.DARK_STATE = st_path
        rcs = []
        try:
            a = argparse.Namespace(min_age=20.0, act=False, update_state=True, json=None)
            with redirect_stdout(io.StringIO()) as out:
                for i, res in enumerate((res1, res2 or res1)):
                    if i == 1:
                        st = json.load(open(st_path, encoding="utf-8"))
                        st["prev_at"] = datetime.fromtimestamp(time.time() - gap_min * 60, timezone.utc
                                                               ).strftime("%Y-%m-%dT%H:%M:%SZ")
                        json.dump(st, open(st_path, "w", encoding="utf-8"))
                    pw.dark_scan = lambda a_, reg, _r=res: json.loads(json.dumps(_r))
                    rcs.append(pw.cmd_darkcheck(a))
        finally:
            pw.dark_scan, pw.DARK_STATE = orig_scan, orig_state
            if os.path.exists(st_path):
                os.remove(st_path)
        return rcs[0], rcs[1], out.getvalue()

    def test_the_command_alerts_only_on_darkness_seen_twice(self):
        """Jack 2026-11-11: "stop false alarming" -- the first sight never alerts (--act
        gets its try); the old-wave lock still there at the next check does, once"""
        res = self.scan()
        rc1, rc2, out = self._twice(res)
        self.assertEqual((rc1, rc2), (0, 3), out)
        self.assertIn("ALERT 2", out)                 # the two lock markets, not the payout-floor one
        # ...and once only: a third look at the same markets says nothing new
        st_path = os.path.join(TMP, "darkcheck_state.json")
        orig_scan, orig_state = pw.dark_scan, pw.DARK_STATE
        pw.DARK_STATE = st_path
        try:
            a = argparse.Namespace(min_age=20.0, act=False, update_state=True, json=None)
            pw.dark_scan = lambda a_, reg: json.loads(json.dumps(res))
            ago = lambda m: datetime.fromtimestamp(time.time() - m * 60, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            with redirect_stdout(io.StringIO()):
                pw.cmd_darkcheck(a)
                for _ in range(2):
                    st = json.load(open(st_path, encoding="utf-8"))
                    st["prev_at"] = ago(20)
                    json.dump(st, open(st_path, "w", encoding="utf-8"))
                    rc = pw.cmd_darkcheck(a)
            self.assertEqual(rc, 0)
        finally:
            pw.dark_scan, pw.DARK_STATE = orig_scan, orig_state
            if os.path.exists(st_path):
                os.remove(st_path)
        # a previous check too old (or too recent) does not count: no alert
        self.assertEqual(self._twice(res, gap_min=90)[1], 0)
        self.assertEqual(self._twice(res, gap_min=2)[1], 0)
        # fixed by --act between the checks: no alert
        fixed = json.loads(json.dumps(res))
        fixed["dark_rows"] = [r for r in fixed["dark_rows"] if r["kind"] != "lock"]
        self.assertEqual(self._twice(res, fixed)[1], 0)

    def test_only_wave_wide_darkness_alerts_whatever_the_reason(self):
        def res(rows, checked=774):
            return {"at": "2026-11-11T13:00:00Z", "deferred": None, "listed": checked, "checked": checked,
                    "too_new": 0, "two_sided": checked - len(rows), "one_sided": 0, "dark": len(rows),
                    "by_reason": {}, "dark_rows": rows, "one_rows": [], "new": [], "acted": []}
        row = lambda i, k: {"ticker": f"KXX-E{i % 7}-{i}", "series": "KXX", "wave": None, "registry": False,
                            "reason": k, "kind": k, "pos": 0.0, "listed_at": None}
        quiet = [
            [row(i, "bot") for i in range(33)],                 # 33 dead books
            [row(i, "unexplained") for i in range(10)],         # one primary under a breaker hold
            [row(i, "unexplained") for i in range(60)],         # 60 across events: still under 100
            [row(i, "bot") for i in range(150)],                # 150 < a quarter of 774
            [dict(row(i, "off"), kind="off") for i in range(400)],     # settled / paused
        ]
        for rows in quiet:
            self.assertEqual(self._twice(res(rows))[1], 0, (len(rows), rows[0]["kind"]))
        # 11/11 05:00Z-12:13Z: 723 of 724 listed dark -- whatever the reason, twice = alert
        big = [row(i, "unexplained") for i in range(723)]
        rc1, rc2, out = self._twice(res(big, 724))
        self.assertEqual((rc1, rc2), (0, 3), out)
        self.assertIn("WAVE-WIDE", out)
        # ...but not when the second check found them quoting
        self.assertEqual(self._twice(res(big, 724), res([], 724))[1], 0)


class ScanEnvTests(unittest.TestCase):
    """Jack 2026-11-11: "should quote larger size if <=5c, otherwise if still under the
    $1.1 bar then yes dont quote it" -- a wave's scan judges markets at the group's own
    cheap tiers, band, cushion and follow-the-book; pins take no tiers; NEAR has none."""

    def test_the_wave_args_reach_the_scan(self):
        w = {"series": ["KXA"], "near": ["KXN"], "since": "2026-11-11T00:00:00Z", "scan_size": 200,
             "budget": 1000, "tune": list(pw.PRESETS["default"]) + ["--pin-series", "KXP:300",
                                                                     "--pin", "KXQ-1-A:200:flat"],
             "near_tune": list(pw.PRESETS["near"])}
        _cmd, env = pw.scan_cmd(w, False)
        self.assertEqual({k: env.get(k) for k in ("PVD_SCAN_CHEAP_TIERS", "PVD_SCAN_PRICE_MIN",
                                                  "PVD_SCAN_PRICE_MAX", "PVD_SCAN_CUSHION",
                                                  "PVD_SCAN_FOLLOW_BOOK", "PVD_SCAN_TIER_SKIP")},
                         {"PVD_SCAN_CHEAP_TIERS": "2:10,5:5", "PVD_SCAN_PRICE_MIN": "1",
                          "PVD_SCAN_PRICE_MAX": "99", "PVD_SCAN_CUSHION": "0",
                          "PVD_SCAN_FOLLOW_BOOK": "1", "PVD_SCAN_TIER_SKIP": "KXQ-1-A,KXP"})
        os.environ["PVD_SCAN_CHEAP_TIERS"] = "2:99"          # never inherited
        try:
            _cmd, env = pw.scan_cmd(w, False, group="near")
        finally:
            del os.environ["PVD_SCAN_CHEAP_TIERS"]
        self.assertNotIn("PVD_SCAN_CHEAP_TIERS", env)
        self.assertNotIn("PVD_SCAN_FOLLOW_BOOK", env)
        self.assertEqual(env["PVD_SCAN_LIVE_SIZE"], "80")

if __name__ == "__main__":
    unittest.main()
