"""Tests for temp_shadow_report.py (the hourly-temp shadow daily report).

Sandboxed: TEMP_SHADOW_DATA points at a fresh temp dir BEFORE any
temp_shadow module is imported, so nothing here touches the live data root.
No email is ever sent (the subprocess runner is mocked) and the real
temp_shadow_sim / temp_shadow_model are replaced by stubs where needed.
"""
import os
import tempfile

os.environ["TEMP_SHADOW_DATA"] = tempfile.mkdtemp(prefix="tsr_test_")

import datetime as _dt          # noqa: E402
import math                     # noqa: E402
import random                   # noqa: E402
import shutil                   # noqa: E402
import subprocess               # noqa: E402
import sys                      # noqa: E402
import types                    # noqa: E402
import unittest                 # noqa: E402
from unittest import mock       # noqa: E402

import temp_shadow_common as C  # noqa: E402
import temp_shadow_report as R  # noqa: E402

DATA = os.environ["TEMP_SHADOW_DATA"]
assert C.DATA_ROOT == DATA and R.C.DATA_ROOT == DATA

NAMES = ["A_base", "B_pull", "C_back", "D_size", "E_decided", "F_full",
         "G_decidedonly", "H_noguard"]
DEFAULT_POLICY = {"q_live": 500, "live_stop_min": 3, "guard_k": 1.0, "win_mode": "pull",
                  "win_lo": 20, "win_hi": 45, "decided": False, "q_pad": 1000,
                  "guard": True}
VARIANTS = {"A_base": {"win_mode": "none"}, "B_pull": {}, "C_back": {"win_mode": "back"},
            "D_size": {"q_live": 1000}, "E_decided": {"decided": True},
            "F_full": {"q_live": 1000, "decided": True, "win_mode": "back"},
            "G_decidedonly": {"decided": True, "q_live": 0},
            "H_noguard": {"guard": False}}
POLICY = R.Policy(DEFAULT_POLICY, VARIANTS)
NOW = C.parse_iso("2026-10-30T12:00:00Z")


def event_ticker(day: str, hour: int) -> str:
    d = _dt.date.fromisoformat(day)
    mon = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
           "NOV", "DEC")[d.month - 1]
    return f"KXTEMPMIAH-{d.year % 100:02d}{mon}{d.day:02d}{hour:02d}"


def vrec(net, reward=None, contracts=100.0, by_tau=None, share=0.1):
    reward = (abs(net) + 5.0) if reward is None else reward
    trading = net - reward
    tau = by_tau or {b: {"reward": reward / 5, "contracts": contracts / 5, "pnl": trading / 5}
                     for b in R.TAU_BUCKETS}
    return {"reward": reward, "fills_n": 3, "contracts": contracts, "trading_pnl": trading,
            "fees": 0.0, "net": net, "avg_share": share, "quoted_secs": 3000,
            "max_abs_pos": 50.0, "worst_case_loss": 40.0,
            "by_class": {"live": {"reward": reward * 0.6, "contracts": contracts * 0.7,
                                  "pnl": trading * 0.8},
                         "decided": {"reward": reward * 0.4, "contracts": contracts * 0.3,
                                     "pnl": trading * 0.2}},
            "by_tau": tau,
            "by_side": {"yes_bid": {"contracts": contracts / 2, "pnl": trading / 2},
                        "no_bid": {"contracts": contracts / 2, "pnl": trading / 2}},
            "fills_after_wx": {"contracts": 5.0, "pnl": -1.0},
            "markouts": {"m60": -0.5, "m300": -1.0}}


def erec(event, variants, settled=True, brier_model=0.08, brier_mid=0.10,
         paying=0.15, model_n=10):
    s = C.parse_event(event)[1]
    return {"event": event, "open_ts": s - 3600, "close_ts": s, "settle_ts": s,
            "settled": settled, "settlement_value": 81.68 if settled else None,
            "results": {},
            "coverage": {"book_trusted_frac": 0.97, "wx_rows": 300, "trades": 400,
                         "trades_src": "ws", "reconnects": 0, "gaps": 0, "program_secs": 3480},
            "market_stats": {"strike_secs": 34800, "paying_frac_without_us": paying,
                             "locked99_frac": 0.3, "live_frac": 0.25, "decided_frac": 0.75,
                             "depth3c_median": 1200.0},
            "model_eval": {"n": model_n, "brier_model": brier_model, "brier_mid": brier_mid,
                           "logloss_model": 0.3, "logloss_mid": 0.32,
                           "by_tau": {"0-5": {"n": model_n / 2, "brier_model": brier_model,
                                              "brier_mid": brier_mid},
                                      "40-60": {"n": model_n / 2, "brier_model": brier_model,
                                                "brier_mid": brier_mid}}},
            "variants": variants}


def scenario_nets(rng, hour, losing_hours=(), opt_mult=None):
    """Per-event cons nets with designed deltas:
    B-A +3, C-B ~0 (noise), D-B +2, E-B -1.5, F-B -0.5, G ~-0.1, H = B-2."""
    a = 10.0 + rng.gauss(0, 1.0)
    b = a + 3.0 + rng.gauss(0, 0.3)
    nets = {"A_base": a, "B_pull": b,
            "C_back": b + rng.gauss(0, 0.2),
            "D_size": b + 2.0 + rng.gauss(0, 0.3),
            "E_decided": b - 1.5 + rng.gauss(0, 0.3),
            "F_full": b - 0.5 + rng.gauss(0, 0.3),
            "G_decidedonly": -0.1 + rng.gauss(0, 0.05),
            "H_noguard": b - 2.0 + rng.gauss(0, 0.3)}
    if hour in losing_hours:
        nets = {k: v - 30.0 for k, v in nets.items()}
    return nets


def write_day(day, rng, losing_hours=(), opt_mult=None, d_tau=None, hours=range(24),
              **ekw):
    os.makedirs(os.path.join(DATA, "sim", day), exist_ok=True)
    for h in hours:
        ev = event_ticker(day, h)
        nets = scenario_nets(rng, h, losing_hours)
        variants = {}
        for n, x in nets.items():
            variants[f"{n}|cons"] = vrec(x, by_tau=d_tau if n == "D_size" else None)
            ox = x * opt_mult if opt_mult else x + 1.0
            variants[f"{n}|opt"] = vrec(ox)
        C.write_json_atomic(os.path.join(DATA, "sim", day, f"{ev}.json"),
                            erec(ev, variants, **ekw))


def clear_data():
    for sub in ("sim", "raw", "reports", "settled"):
        shutil.rmtree(os.path.join(DATA, sub), ignore_errors=True)
    for f in (C.STATUS_FILE,):
        if os.path.exists(f):
            os.remove(f)


def by_id(suggestions):
    return {s["id"]: s for s in suggestions}


class StubModules:
    """Temporarily replace temp_shadow_sim / temp_shadow_model in sys.modules."""

    def __init__(self, **mods):
        self.mods = mods
        self.saved = {}

    def __enter__(self):
        for k, m in self.mods.items():
            self.saved[k] = sys.modules.get(k)
            sys.modules[k] = m
        return self

    def __exit__(self, *exc):
        for k, m in self.saved.items():
            if m is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = m


def _spec_replica(stations_f, offsets_c, b_city_c):
    xs = [(C.f_to_c(f) - offsets_c.get(s, 0.0) + b_city_c) for s, f in stations_f.items()]
    if len(xs) < 4:
        return None
    if len(xs) == 5:
        return round(32 + 0.36 * sum(round(C.f_to_c(f)) for f in stations_f.values()), 2)
    return round(32 + 1.8 * sum(xs) / len(xs), 2)


# --------------------------------------------------------------------------


class TestHelpers(unittest.TestCase):
    def test_days_and_labels(self):
        self.assertEqual(R.day_number("2026-10-15"), 1)
        self.assertEqual(R.day_number("2026-10-14"), 0)
        self.assertEqual(R.day_label("2026-10-21"), "Day 7 of 7")
        self.assertIn("partial", R.day_label("2026-10-14"))
        self.assertEqual(R.window_days("2026-10-17"), ["2026-10-15", "2026-10-16", "2026-10-17"])
        self.assertEqual(R.window_days("2026-10-14"), ["2026-10-14"])
        # 2026-10-16 03:00Z = 10/15 23:00 ET -> yesterday = 10/14
        self.assertEqual(R.default_day(C.parse_iso("2026-10-16T03:00:00Z")), "2026-10-14")
        self.assertEqual(R.default_day(C.parse_iso("2026-10-16T13:00:00Z")), "2026-10-15")

    def test_money_and_resolve(self):
        self.assertEqual(R.money(1234.4), "+$1,234")
        self.assertEqual(R.money(-5.5), "-$5.50")
        self.assertEqual(R.money(0), "$0.00")
        self.assertEqual(R.resolve_variant("B", NAMES), "B_pull")
        self.assertEqual(R.resolve_variant("B", ["B", "B2"]), "B")
        self.assertIsNone(R.resolve_variant("B", ["B2", "BX"]))
        self.assertEqual(R.event_hour("KXTEMPMIAH-26OCT1509"), 9)

    def test_gate_suppresses_noise(self):
        st = R._stats([0.1, 0.1, 0.1], 1, 0.3)
        self.assertTrue(any("under $5" in w for w in R.gate(st)))
        st = R._stats([10.0, 0.0, 0.0, 0.0], 1, 10.0)      # 1 differing event
        self.assertTrue(any("differing" in w for w in R.gate(st)))
        st = R._stats([40.0, -35.0, 30.0, -28.0, 9.0], 1, 16.0)   # +16/day but SE ~ 34
        self.assertTrue(any("SE" in w for w in R.gate(st)))
        st = R._stats([3.0] * 24, 1, 72.0)
        self.assertEqual(R.gate(st), [])
        self.assertEqual(st["z"], float("inf"))

    def test_policy_diff(self):
        self.assertEqual(POLICY.diff_text("B_pull", "D_size"), "q_live 500 -> 1000")
        self.assertEqual(POLICY.params("D_size")["q_live"], 1000)
        self.assertEqual(POLICY.desc("C_back"), "win_mode=back")
        self.assertEqual(POLICY.get("D_size", "Q_LIVE"), 1000)


class TestRuleEngine(unittest.TestCase):
    def setUp(self):
        clear_data()

    def _ctx(self, day="2026-10-17", **kw):
        return R.build_context(day, policy=POLICY, with_health=False, now=NOW, **kw)

    def test_pair_rules_fire_and_suppress(self):
        rng = random.Random(7)
        d_tau = {"0-5": {"reward": 1.0, "contracts": 30, "pnl": 0.0},
                 "5-10": {"reward": 1.0, "contracts": 20, "pnl": -4.0},
                 "10-20": {"reward": 3.0, "contracts": 20, "pnl": 1.0},
                 "20-40": {"reward": 4.0, "contracts": 20, "pnl": 1.0},
                 "40-60": {"reward": 4.0, "contracts": 10, "pnl": 1.0}}
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            write_day(day, rng, losing_hours=(2, 3), d_tau=d_tau,
                      brier_model=0.15, brier_mid=0.10)
        ctx = self._ctx()
        s = by_id(ctx["suggestions"])
        # window B-A = +3/event -> ~+72/day, keep it
        self.assertFalse(s["window"]["suppressed"])
        self.assertAlmostEqual(s["window"]["usd"], 72.0, delta=8)
        self.assertIn("keep", s["window"]["title"])
        self.assertIn("win_mode none -> pull", s["window"]["change"])
        # back vs pull ~0 -> suppressed
        self.assertTrue(s["back"]["suppressed"])
        # size D-B = +2/event -> +48/day over 500 extra contracts
        self.assertFalse(s["size"]["suppressed"])
        self.assertIn("per extra contract", s["size"]["evidence"])
        self.assertIn("raise q_live 500 -> 1000", s["size"]["change"])
        self.assertAlmostEqual(s["size"]["usd"], 48.0, delta=8)
        # decided E-B = -1.5/event -> leave off
        self.assertFalse(s["decided"]["suppressed"])
        self.assertLess(s["decided"]["usd"], -25)
        self.assertIn("leave them off", s["decided"]["title"])
        self.assertIn("G_decidedonly alone", s["decided"]["evidence"])
        # full: D is best; F trails it by ~2.5/event
        self.assertEqual(ctx["win"].best("cons"), "D_size")
        self.assertFalse(s["full"]["suppressed"])
        self.assertIn("trails D_size", s["full"]["title"])
        # guard B-H = +2/event
        self.assertFalse(s["guard"]["suppressed"])
        self.assertGreater(s["guard"]["usd"], 30)
        # fill gap +1/event on ~15/event -> under 50% -> suppressed
        self.assertTrue(s["fillgap"]["suppressed"])
        # tau 5-10 on D loses 3/event -> raise live_stop_min 3 -> 10; the
        # 0-5 bucket straddles live_stop_min 3 and stays out of the $ evidence
        self.assertFalse(s["tau"]["suppressed"])
        self.assertIn("live_stop_min 3 -> 10", s["tau"]["change"])
        self.assertAlmostEqual(s["tau"]["usd"], -72.0, delta=1)
        self.assertIn("tau 3-5 min (inside the 0-5 bucket) is not priced", s["tau"]["evidence"])
        # hours 02, 03 lose on every day
        self.assertFalse(s["hours"]["suppressed"])
        self.assertIn("02, 03", s["hours"]["title"])
        self.assertIn("q_live 1000 -> 500", s["hours"]["change"])
        # model worse than the mid
        self.assertFalse(s["model"]["suppressed"])
        self.assertIn("guard_k 1 -> 1.5", s["model"]["change"])
        # paying fraction 15% -> note
        self.assertEqual(s["paying"]["kind"], "note")
        # ordering: unsuppressed policy first (changes before "keep"s), suppressed last
        kinds = [(x["suppressed"], x["kind"], not x["action"]) for x in ctx["suggestions"]]
        self.assertEqual(kinds, sorted(kinds, key=lambda k: (k[0], {"policy": 0, "risk": 1,
                                                                    "ops": 2, "note": 3}[k[1]],
                                                             k[2])))
        self.assertFalse(s["window"]["action"])           # "keep B's window"
        self.assertTrue(s["tau"]["action"])
        top = [x["id"] for x in ctx["suggestions"] if not x["suppressed"]][:4]
        self.assertEqual(set(top), {"tau", "full", "size", "hours"})
        # every unsuppressed suggestion carries $ evidence and a change
        for x in ctx["suggestions"]:
            self.assertIn("$", x["evidence"])
            self.assertTrue(x["change"])
        html_page = R.render_html(ctx)
        self.assertIn("Suggested tweaks", html_page)
        self.assertIn("Below noise", html_page)
        self.assertIn("raise live_stop_min 3 -&gt; 10", html_page)

    def test_one_day_suppresses_hour_rule_and_good_model_is_quiet(self):
        rng = random.Random(3)
        write_day("2026-10-15", rng, losing_hours=(2, 3), paying=0.6)
        ctx = self._ctx("2026-10-15")
        s = by_id(ctx["suggestions"])
        self.assertNotIn("hours", s)          # one event per hour is not evidence
        self.assertNotIn("model", s)          # model beats the mid
        self.assertNotIn("paying", s)         # 60% pays without us
        self.assertIn("window", s)

    def test_fill_model_gap_fires(self):
        rng = random.Random(11)
        for day in ("2026-10-15", "2026-10-16"):
            write_day(day, rng, opt_mult=3.0)
        s = by_id(self._ctx("2026-10-16")["suggestions"])
        self.assertFalse(s["fillgap"]["suppressed"])
        self.assertEqual(s["fillgap"]["kind"], "risk")
        self.assertIn("q_live 1000 -> 500", s["fillgap"]["change"])

    def test_small_deltas_are_suppressed(self):
        rng = random.Random(5)
        os.makedirs(os.path.join(DATA, "sim", "2026-10-15"), exist_ok=True)
        for h in range(24):
            ev = event_ticker("2026-10-15", h)
            base = 10 + rng.gauss(0, 2)
            variants = {f"{n}|{f}": vrec(base + 0.01 * i + rng.gauss(0, 0.1))
                        for i, n in enumerate(NAMES) for f in ("cons", "opt")}
            C.write_json_atomic(os.path.join(DATA, "sim", "2026-10-15", f"{ev}.json"),
                                erec(ev, variants, paying=0.5))
        ctx = self._ctx("2026-10-15")
        for sid in ("window", "back", "size", "guard"):
            self.assertTrue(by_id(ctx["suggestions"])[sid]["suppressed"], sid)

    def test_window_position_rule(self):
        rng = random.Random(12)
        write_day("2026-10-15", rng)
        win = R.Window({"2026-10-15": R.load_sim_day("2026-10-15")})
        mk = lambda secs: {"wx": {"land_all": secs}}   # noqa: E731
        early = [13.0 + (i % 5) for i in range(60)]          # readings land :13-:17
        sugg, _ = R.suggest(win, "2026-10-15", POLICY, {}, {}, mk(early))
        s = by_id(sugg)["winpos"]
        self.assertIn("win_lo 20 -> 12, win_hi 45 -> 21", s["change"])
        self.assertIn("window value as configured (B-A)", s["evidence"])
        self.assertIn("0% of 60 station readings", s["evidence"])
        inside = [25.0 + (i % 10) for i in range(60)]
        sugg, _ = R.suggest(win, "2026-10-15", POLICY, {}, {}, mk(inside))
        self.assertNotIn("winpos", by_id(sugg))
        sugg, _ = R.suggest(win, "2026-10-15", POLICY, {}, {}, mk(early[:10]))   # too few
        self.assertNotIn("winpos", by_id(sugg))

    def test_missing_letters_are_noted(self):
        rng = random.Random(1)
        os.makedirs(os.path.join(DATA, "sim", "2026-10-15"), exist_ok=True)
        for h in range(3):
            ev = event_ticker("2026-10-15", h)
            variants = {"base|cons": vrec(rng.gauss(5, 1)), "base|opt": vrec(6.0)}
            C.write_json_atomic(os.path.join(DATA, "sim", "2026-10-15", f"{ev}.json"),
                                erec(ev, variants, paying=0.5))
        ctx = self._ctx("2026-10-15")
        self.assertTrue(any("Pair rules skipped" in n for n in ctx["notes"]))
        R.render_html(ctx)     # renders without the letter variants


class TestWeekSummary(unittest.TestCase):
    def _win(self, nets):
        days = {}
        for i, x in enumerate(nets):
            day = (_dt.date(2026, 10, 15) + _dt.timedelta(days=i)).isoformat()
            evs = []
            for h in (9, 10):
                ev = event_ticker(day, h)
                evs.append(erec(ev, {"B_pull|cons": vrec(x / 2), "B_pull|opt": vrec(x / 2 + 5),
                                     "A_base|cons": vrec(x / 2 - 50)}))
            days[day] = evs
        return R.Window(days)

    def _verdict(self, nets):
        win = self._win(nets)
        return R.week_summary(win, POLICY, R.agg_model(win.all_events))

    def test_go(self):
        w = self._verdict([600, 700, 800, 650, 900, -100, 700])
        self.assertEqual(w["verdict"], "GO")
        self.assertEqual(w["best"], "B_pull")
        self.assertEqual(w["pos"], 6)
        self.assertEqual(w["config"]["q_live"], 500)
        self.assertTrue(any("Fill model" in r for r in w["risks"]))

    def test_no_go(self):
        self.assertEqual(self._verdict([-100, -200, 50, -300, 20, -10, -5])["verdict"], "NO-GO")

    def test_extend_low_mean(self):
        w = self._verdict([300, 200, 400, 300, 200, 100, 300])
        self.assertEqual(w["verdict"], "EXTEND")
        self.assertIn("mean", w["reason"])

    def test_extend_too_few_positive_days(self):
        w = self._verdict([2000, 2000, 2000, 2000, -10, -10, -10])
        self.assertEqual(w["verdict"], "EXTEND")
        self.assertIn("4 of 7", w["reason"])

    def test_week_section_rendered_on_day_seven(self):
        clear_data()
        rng = random.Random(2)
        for i in range(7):
            write_day((_dt.date(2026, 10, 15) + _dt.timedelta(days=i)).isoformat(), rng,
                      hours=range(6))
        ctx = R.build_context("2026-10-21", policy=POLICY, with_health=False, now=NOW)
        self.assertIsNotNone(ctx["week"])
        page = R.render_html(ctx)
        self.assertIn("Week summary", page)
        self.assertIn("Recommended starting config", page)
        self.assertIn("Day 7 of 7", page)
        self.assertIn("week:", R.subject_line(ctx))
        ctx6 = R.build_context("2026-10-20", policy=POLICY, with_health=False, now=NOW)
        self.assertIsNone(ctx6["week"])
        self.assertNotIn("Week summary", R.render_html(ctx6))


class TestRenderAndHealth(unittest.TestCase):
    def setUp(self):
        clear_data()

    def test_three_days_html_and_cumulative(self):
        rng = random.Random(9)
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            write_day(day, rng)
        write_day("2026-10-14", rng, hours=range(20, 24))       # partial day 0
        ctx = R.build_context("2026-10-17", policy=POLICY, with_health=True, now=NOW)
        self.assertEqual(ctx["best_cons"], "D_size")
        self.assertEqual(len(ctx["events"]), 24)
        self.assertEqual(ctx["day0_events"], 4)
        page = R.render_html(ctx)
        for needle in ("Day 3 of 7", "Best cons variant <b>D_size</b>", "Best opt variant",
                       "By ET hour of the event", "By strike class", "By minutes to close",
                       "By side", "Worst 5 events", "Market structure", "Model",
                       "Data health", "Cumulative to date", "d1", "d3", "Day 0 (partial, 4 events)"):
            self.assertIn(needle, page)
        self.assertNotIn("Week summary", page)
        win = ctx["win"]
        self.assertEqual(win.days, ["2026-10-15", "2026-10-16", "2026-10-17"])
        self.assertEqual(len(win.days_with("D_size|cons")), 3)
        subj = R.subject_line(ctx)
        self.assertTrue(subj.startswith("Temp shadow day 3 (2026-10-17): best D_size net +$"), subj)
        self.assertIn("reward", subj)
        self.assertIn("fills", subj)
        # agg sanity: sum over events equals the day's figure
        a = ctx["agg"]["D_size|cons"]
        evs = ctx["events"]
        self.assertAlmostEqual(a["net"], sum(e["variants"]["D_size|cons"]["net"] for e in evs))
        self.assertEqual(a["n_events"], 24)
        self.assertAlmostEqual(a["avg_share"], 0.1)
        self.assertAlmostEqual(ctx["market"]["paying_frac_without_us"], 0.15)

    def test_empty_day_renders_with_reason(self):
        ctx = R.build_context("2026-10-15", policy=POLICY, with_health=True, now=NOW)
        page = R.render_html(ctx)
        self.assertIn("No sim results for 2026-10-15", page)
        self.assertIn("collector down", page)
        self.assertIn("no sim results", R.subject_line(ctx))
        self.assertEqual(len(ctx["health"]["events"]["missing"]), 24)
        R.render_text(ctx)

    def test_empty_day_with_recording_says_not_settled(self):
        t0, _ = C.et_day_bounds("2026-10-15")
        C.append_jsonl(C.raw_file("wx", t0 + 100), {"rx": t0 + 100, "city": "miami",
                                                    "t": int(t0 * 1000), "status": "incomplete",
                                                    "v": None, "stations": []})
        ctx = R.build_context("2026-10-15", policy=POLICY, with_health=True, now=NOW)
        self.assertIn("not settled yet", R.render_html(ctx))

    def test_sim_error_banner(self):
        ctx = R.build_context("2026-10-15", sim_error="Traceback...\nRuntimeError: boom",
                              sim_ran=True, policy=POLICY, with_health=False, now=NOW)
        page = R.render_html(ctx)
        self.assertIn("THE SIM FAILED", page)
        self.assertIn("RuntimeError: boom", page)
        self.assertIn("[SIM FAILED]", R.subject_line(ctx))

    def test_collector_health_from_raw(self):
        day = "2026-10-15"
        t0, t1 = C.et_day_bounds(day)
        # ws: one row a minute, disconnected minutes 600..699
        for k in range(1440):
            rx = t0 + 60 * k + 5
            if 600 <= k < 700:
                continue
            C.append_jsonl(C.raw_file("ws", rx), {"rx": rx, "m": {"type": "orderbook_delta",
                                                                 "sid": 1, "seq": k}})
        C.append_jsonl(C.raw_file("ws", t0 + 600 * 60 + 1),
                       {"rx": t0 + 600 * 60 + 1, "marker": "disconnect", "detail": "eof"})
        C.append_jsonl(C.raw_file("ws", t0 + 700 * 60 + 1),
                       {"rx": t0 + 700 * 60 + 1, "marker": "connect", "detail": ""})
        C.append_jsonl(C.raw_file("ws", t0 + 800 * 60 + 2),
                       {"rx": t0 + 800 * 60 + 2, "marker": "gap", "detail": "sid 1"})
        # meta: a poll row a minute, a 429 every 50th minute
        for k in range(1440):
            rx = t0 + 60 * k + 59
            C.append_jsonl(C.raw_file("meta", rx), {"rx": rx, "kind": "poll", "data": {
                "n": 40, "n429": 1 if k % 50 == 0 else 0, "nerr": 0, "lat_ms_p50": 80,
                "lat_ms_max": 300, "signed": False}})
        # wx: three minutes; the third is a replica mismatch
        stations = [("KMIA1M", 82.4), ("KOPF1M", 82.4), ("KFLL1M", 80.6), ("KFXE1M", 80.6),
                    ("KPMP1M", 80.6)]
        sum_c = sum(round(C.f_to_c(f)) for _, f in stations)
        v_ok = C.index_from_sum(sum_c)
        # live shape (verified 10/15 00:4xZ): a received reading stays code
        # "pending" until the official v lands, then every station turns "ok"
        for j, v in enumerate((v_ok, v_ok, v_ok + 0.36)):
            t = int(t0 * 1000) + 60000 * (100 + j)
            allp = [{"station_id": s, "temp_f": f, "code": "pending", "source": "hf_asos",
                     "received_at_ms": t + (158000 if i == 0 else 140000)}
                    for i, (s, f) in enumerate(stations)]
            four = allp[1:]                                   # KMIA not in yet
            final = [dict(x, code="ok") for x in allp]
            base = {"city": "miami", "t": t, "contributors": 5}
            C.append_jsonl(C.raw_file("wx", t / 1000 + 150), dict(
                base, rx=t / 1000 + 150, status="incomplete", v=None, stations=four))
            C.append_jsonl(C.raw_file("wx", t / 1000 + 160), dict(
                base, rx=t / 1000 + 160, status="incomplete", v=None, stations=allp))
            C.append_jsonl(C.raw_file("wx", t / 1000 + 310), dict(
                base, rx=t / 1000 + 310, status="normal", v=v, stations=final))
        # a minute already complete at first sight (collector restart): no lag sample
        t = int(t0 * 1000) + 60000 * 200
        late = [{"station_id": s, "temp_f": f, "code": "ok", "source": "hf_asos",
                 "received_at_ms": t + 140000} for s, f in stations]
        C.append_jsonl(C.raw_file("wx", t / 1000 + 290), {
            "rx": t / 1000 + 290, "city": "miami", "t": t, "contributors": 5,
            "status": "incomplete", "v": None, "stations": late})
        C.write_json_atomic(C.STATUS_FILE, {"rx": NOW - 30, "pid": 1234, "ws": "up"})
        # settled file without a sim output -> fixable
        C.write_json_atomic(os.path.join(DATA, "settled", f"{event_ticker(day, 5)}.json"),
                            {"event_ticker": event_ticker(day, 5)})
        stub_model = types.SimpleNamespace(replica_index=_spec_replica)
        with StubModules(temp_shadow_model=stub_model):
            h = R.collector_health(day, [], now=NOW)
        ws = h["ws"]
        self.assertEqual(ws["connects"], 1)
        self.assertEqual(ws["disconnects"], 1)
        self.assertEqual(ws["gaps"], 1)
        self.assertAlmostEqual(ws["uptime"], 1340 / 1440, places=3)
        self.assertEqual(h["poll"]["n429"], 29)
        self.assertEqual(h["poll"]["n"], 40 * 1440)
        self.assertAlmostEqual(h["poll"]["coverage"], 1.0)
        self.assertEqual(h["wx"]["minutes_finalized"], 3)
        self.assertAlmostEqual(h["wx"]["obs_lag_median"], 2.0, places=3)
        self.assertEqual(h["wx"]["obs_lag_n"], 3)
        self.assertEqual(h["wx"]["minutes_seen"], 4)
        self.assertAlmostEqual(h["wx"]["land_sec_p10"], 38.0, places=3)     # t+158 s
        self.assertAlmostEqual(h["wx"]["station_lag_median"], 10.0, places=3)
        self.assertAlmostEqual(h["wx"]["official_lag_median"], 310.0, places=3)
        self.assertEqual(h["replica"]["n"], 3)
        self.assertEqual(h["replica"]["exact"], 2)
        self.assertEqual(len(h["replica"]["mismatches"]), 1)
        miss = {m["event"]: m for m in h["events"]["missing"]}
        self.assertTrue(miss[event_ticker(day, 5)]["fixable"])
        self.assertFalse(miss[event_ticker(day, 6)]["fixable"])
        # ops rules fire on that health
        rng = random.Random(4)
        write_day(day, rng, hours=range(5))
        win = R.Window({day: R.load_sim_day(day)})
        sugg, _ = R.suggest(win, day, POLICY, R.agg_market(win.all_events),
                            R.agg_model(win.all_events), h)
        s = by_id(sugg)
        self.assertNotIn("ops429", s)       # 29 of 57,600 polls (0.05%) is no signal
        self.assertIn("opsws", s)
        self.assertIn("opsmiss", s)
        self.assertNotIn("opslag", s)
        ctx = R.build_context(day, policy=POLICY, with_health=False, now=NOW)
        ctx["health"] = h
        page = R.render_html(ctx)
        self.assertIn("93.1%", page)
        self.assertIn("2 of 3 finalized minutes exact", page)

    def test_replica_degrades_without_model(self):
        broken = types.ModuleType("temp_shadow_model")    # no replica_index
        with StubModules(temp_shadow_model=broken):
            rep = R.replica_check({1: [{"rx": 1, "v": 80.0, "status": "normal",
                                        "stations": []}]})
        self.assertIn("error", rep)


class TestMain(unittest.TestCase):
    def setUp(self):
        clear_data()
        rng = random.Random(21)
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            write_day(day, rng, hours=range(8))
        self.out = os.path.join(DATA, "out", "r.html")

    def _patches(self):
        return [mock.patch.object(R, "load_policy_defs",
                                  return_value=(DEFAULT_POLICY, VARIANTS, None)),
                mock.patch.object(R, "_env_from_registry", return_value=""),
                mock.patch.object(R.time, "sleep")]

    def test_send_mocked(self):
        done = subprocess.CompletedProcess([], 0, "email sent", "")
        ps = self._patches()
        for p in ps:
            p.start()
        try:
            with mock.patch.object(R.subprocess, "run", return_value=done) as run:
                rc = R.main(["--day", "2026-10-17", "--no-sim", "--send", "--out", self.out])
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.out))
        run.assert_called_once()
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("send_html_email.py"))
        self.assertTrue(cmd[2].startswith("Temp shadow day 3 (2026-10-17): best D_size net"))
        self.assertEqual(cmd[3], self.out)
        self.assertIn("D_size", cmd[4])

    def test_send_failure_exit_2(self):
        bad = subprocess.CompletedProcess([], 1, "", "smtp down")
        ps = self._patches()
        for p in ps:
            p.start()
        try:
            with mock.patch.object(R.subprocess, "run", return_value=bad) as run:
                rc = R.main(["--day", "2026-10-17", "--no-sim", "--send", "--out", self.out])
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(rc, 2)
        self.assertEqual(run.call_count, 3)

    def test_default_path_and_sim_failure(self):
        def boom(day, *a, **k):
            raise RuntimeError("sim exploded on " + day)
        fake_sim = types.SimpleNamespace(run_day=boom, DEFAULT_POLICY=DEFAULT_POLICY,
                                         VARIANTS=VARIANTS)
        with StubModules(temp_shadow_sim=fake_sim), \
                mock.patch.object(R.subprocess, "run") as run:
            rc = R.main(["--day", "2026-10-17"])
        self.assertEqual(rc, 1)
        run.assert_not_called()               # no --send, no email
        path = os.path.join(DATA, "reports", "2026-10-17.html")
        with open(path, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("THE SIM FAILED", page)
        self.assertIn("sim exploded on 2026-10-17", page)
        self.assertIn("D_size", page)          # earlier sim output still reported

    def test_sim_success_called_with_day(self):
        # the report day first, then every earlier day of the cumulative
        # window (review fix 10/16: a SIM_VERSION bump left 10/15 at v4)
        calls = []
        fake_sim = types.SimpleNamespace(run_day=lambda d, *a, **k: calls.append(d) or [],
                                         DEFAULT_POLICY=DEFAULT_POLICY, VARIANTS=VARIANTS)
        with StubModules(temp_shadow_sim=fake_sim):
            rc = R.main(["--day", "2026-10-16", "--out", self.out])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["2026-10-16", "2026-10-15"])
        calls.clear()
        with StubModules(temp_shadow_sim=fake_sim):
            R.main(["--day", "2026-10-14", "--out", self.out])     # day 0: alone
        self.assertEqual(calls, ["2026-10-14"])


class TestSimIntegration(unittest.TestCase):
    def setUp(self):
        clear_data()

    def test_per_event_sim_errors_and_pending(self):
        rng = random.Random(8)
        write_day("2026-10-15", rng, hours=range(4))
        ok_ev = [{"event": event_ticker("2026-10-15", h)} for h in range(4)]
        res = ok_ev + [{"event": event_ticker("2026-10-15", 4), "error": "KeyError: 'x'"},
                       {"event": event_ticker("2026-10-15", 5), "pending": True,
                        "settled": False}]
        fake_sim = types.SimpleNamespace(run_day=lambda d, *a, **k: res,
                                         DEFAULT_POLICY=DEFAULT_POLICY, VARIANTS=VARIANTS)
        out = os.path.join(DATA, "out", "e.html")
        with StubModules(temp_shadow_sim=fake_sim), \
                mock.patch.object(C, "utcnow", return_value=NOW):
            err, summary = R.run_sim("2026-10-15")
            rc = R.main(["--day", "2026-10-15", "--out", out])
        self.assertIsNone(err)
        self.assertEqual(summary["ok"], 4)
        self.assertEqual(summary["pending"], [event_ticker("2026-10-15", 5)])
        self.assertEqual(rc, 1)
        with open(out, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("1 event(s) failed in the sim", page)
        self.assertIn("the sim raised: KeyError", page)
        self.assertIn("the sim found it unsettled", page)
        self.assertIn("4 ok, 1 pending (unsettled), 1 failed", page)

    def test_markouts_contract_weighted(self):
        e1, e2 = event_ticker("2026-10-15", 1), event_ticker("2026-10-15", 2)
        v1, v2 = vrec(5.0, contracts=100.0), vrec(5.0, contracts=300.0)
        v1["markouts"] = {"m60": -2.0, "m300": -4.0}
        v2["markouts"] = {"m60": 2.0, "m300": 0.0}
        agg = R.agg_variants([erec(e1, {"B_pull|cons": v1}), erec(e2, {"B_pull|cons": v2})])
        mo = agg["B_pull|cons"]["markouts"]
        self.assertAlmostEqual(mo["m60"], 1.0)
        self.assertAlmostEqual(mo["m300"], -1.0)

    def test_recorded_policies_fill_missing_variants(self):
        ev = event_ticker("2026-10-15", 9)
        rec = erec(ev, {"Z_new|cons": vrec(5.0), "Z_new|opt": vrec(6.0)})
        rec["policies"] = {"Z_new": {"q_live": 900}}
        C.write_json_atomic(os.path.join(DATA, "sim", "2026-10-15", f"{ev}.json"), rec)
        pol = R.Policy(dict(DEFAULT_POLICY), dict(VARIANTS))
        ctx = R.build_context("2026-10-15", policy=pol, with_health=False, now=NOW)
        self.assertEqual(ctx["policy"].params("Z_new")["q_live"], 900)
        self.assertIn("q_live=900", R.render_html(ctx))


class TestWindowResim(unittest.TestCase):
    """Review fix 10/16 (finding 1): the SIM_VERSION 5 bump re-simmed only the
    report day, so the cumulative window kept 10/15 at v4 -- G_decided_only
    there was the old G (no pad hold) and G0_nohold did not exist. run_sim now
    runs run_day for every window day, and files still older than the
    current SIM_VERSION are flagged."""

    OLD_G, NEW_G, G0 = 3.0, 2.5, 3.0     # per event: v4 G, v5 G (held), v5 G0

    def setUp(self):
        clear_data()

    def _write(self, day, version, hours=range(6)):
        os.makedirs(os.path.join(DATA, "sim", day), exist_ok=True)
        for h in hours:
            ev = event_ticker(day, h)
            if version == "4":
                variants = {"G_decided_only|cons": vrec(self.OLD_G),
                            "G_decided_only|opt": vrec(self.OLD_G),
                            "B_pull|cons": vrec(10.0), "B_pull|opt": vrec(11.0)}
                pols = {"G_decided_only": {"live": False, "decided": True}, "B_pull": {}}
            else:
                variants = {"G_decided_only|cons": vrec(self.NEW_G),
                            "G_decided_only|opt": vrec(self.NEW_G),
                            "G0_nohold|cons": vrec(self.G0), "G0_nohold|opt": vrec(self.G0),
                            "B_pull|cons": vrec(10.0), "B_pull|opt": vrec(11.0)}
                pols = {"G_decided_only": {"live": False, "decided": True,
                                           "pad_hold_open_s": 180.0},
                        "G0_nohold": {"live": False, "decided": True}, "B_pull": {}}
            rec = erec(ev, variants)
            rec["sim_version"], rec["policies"] = version, pols
            C.write_json_atomic(os.path.join(DATA, "sim", day, f"{ev}.json"), rec)

    def _fake_sim(self, calls, fail_day=None):
        """run_day(d) re-sims files from an older version to v5 (what the
        real run_day does through _cached_ok)."""
        def run_day(d, *a, **k):
            calls.append(d)
            if d == fail_day:
                raise RuntimeError("disk full on " + d)
            out = []
            for rec in R.load_sim_day(d):
                if str(rec.get("sim_version")) != "5":
                    self._write(d, "5", hours=[int(rec["event"][-2:])])
                out.append({"event": rec["event"]})
            return out
        return types.SimpleNamespace(
            run_day=run_day, SIM_VERSION="5",
            DEFAULT_POLICY={"live": True, "decided": False, "pad_hold_open_s": 0.0},
            VARIANTS={"B_pull": {},
                      "G_decided_only": {"live": False, "decided": True,
                                         "pad_hold_open_s": 180.0},
                      "G0_nohold": {"live": False, "decided": True}})

    def test_report_resims_every_window_day(self):
        self._write("2026-10-15", "4")
        self._write("2026-10-16", "4")
        calls = []
        with StubModules(temp_shadow_sim=self._fake_sim(calls)), \
                mock.patch.object(C, "utcnow", return_value=NOW):
            err, summary = R.run_sim("2026-10-16")
            self.assertIsNone(err)
            self.assertEqual(calls, ["2026-10-16", "2026-10-15"])
            self.assertEqual(summary["ok"], 6)
            self.assertEqual(summary["window"]["days"], ["2026-10-15"])
            self.assertEqual(summary["window"]["ok"], 6)
            self.assertEqual(summary["window"]["errors"], [])
            ctx = R.build_context("2026-10-16", with_health=False, now=NOW,
                                  sim_summary=summary)
        win = ctx["win"]
        # every window day at v5: G0 on both days, G is the held G on both
        self.assertEqual(win.days_with("G0_nohold|cons"), ["2026-10-15", "2026-10-16"])
        self.assertAlmostEqual(win.total("G_decided_only|cons"), 12 * self.NEW_G)
        self.assertAlmostEqual(win.total("G0_nohold|cons"), 12 * self.G0)
        self.assertEqual(ctx["stale_sim"], {})
        self.assertEqual(ctx["sim_version"], "5")
        for d in ("2026-10-15", "2026-10-16"):
            self.assertEqual({e["sim_version"] for e in R.load_sim_day(d)}, {"5"})
        page = R.render_html(ctx)
        self.assertNotIn("older SIM_VERSION", page)
        self.assertNotIn("stale", R.subject_line(ctx))
        # the pad-hold rule pairs all 12 events of both days
        s = by_id(ctx["suggestions"])["padhold"]
        self.assertIn("over 12 events / 2 days", s["evidence"])

    def test_before_the_fix_window_mixes_versions(self):
        # what the 10/17 report computed before the fix: 10/15 v4 + 10/16 v5
        self._write("2026-10-15", "4")
        self._write("2026-10-16", "5")
        days = R.window_days("2026-10-16")
        win = R.Window({d: R.load_sim_day(d) for d in days})
        self.assertAlmostEqual(win.total("G_decided_only|cons"),
                               6 * self.OLD_G + 6 * self.NEW_G)      # mixed
        self.assertEqual(win.days_with("G0_nohold|cons"), ["2026-10-16"])
        # ... and a report that does not re-sim (--no-sim) now flags it
        with StubModules(temp_shadow_sim=self._fake_sim([])):
            ctx = R.build_context("2026-10-16", with_health=False, now=NOW)
        self.assertEqual(ctx["stale_sim"], {"2026-10-15": 6})
        page = R.render_html(ctx)
        self.assertIn("6 sim file(s) in the cumulative window are from an older SIM_VERSION "
                      "than 5", page)
        self.assertIn("2026-10-15 x6", page)
        self.assertIn("[6 stale sim files]", R.subject_line(ctx))
        self.assertIn("older SIM_VERSION than 5 (2026-10-15 x6)", R.render_text(ctx))

    def test_window_day_failure_is_reported_and_exits_1(self):
        self._write("2026-10-15", "4")
        self._write("2026-10-16", "4")
        calls = []
        out = os.path.join(DATA, "out", "f.html")
        with StubModules(temp_shadow_sim=self._fake_sim(calls, fail_day="2026-10-15")), \
                mock.patch.object(C, "utcnow", return_value=NOW), \
                mock.patch.object(R.subprocess, "run") as run:
            rc = R.main(["--day", "2026-10-16", "--out", out])
        run.assert_not_called()
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ["2026-10-16", "2026-10-15"])
        with open(out, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("1 re-sim failure(s) on earlier window days", page)
        self.assertIn("run_day raised RuntimeError: disk full on 2026-10-15", page)
        self.assertIn("2026-10-15 x6", page)          # still v4: flagged as stale
        self.assertIn("6 ok, 0 pending (unsettled), 0 failed", page)    # the report day
        self.assertIn("sim run (earlier window days)", page)
        self.assertIn("0 ok, 0 pending (unsettled), 1 failed over 1 day(s)", page)

    def test_files_without_a_version_are_not_judged(self):
        self.assertEqual(R.stale_sim_days({"d": [{"event": "x"}, {"sim_version": "5"}]}, "5"),
                         {})
        self.assertEqual(R.stale_sim_days({"d": [{"sim_version": 4}]}, "5"), {"d": 1})
        self.assertEqual(R.stale_sim_days({"d": [{"sim_version": "4"}]}, None), {})


HOLD_DEFAULTS = dict(DEFAULT_POLICY, pad_hold_open_s=0.0)
HOLD_VARIANTS = {"A_base": {"win_mode": "none"}, "B_pull": {}, "C_back": {"win_mode": "back"},
                 "D_size": {"q_live": 1000}, "E_decided": {"decided": True},
                 "F_full": {"q_live": 1000, "decided": True, "win_mode": "back"},
                 "G_decided_only": {"decided": True, "q_live": 0, "pad_hold_open_s": 180.0},
                 "G0_nohold": {"decided": True, "q_live": 0},
                 "H_noguard": {"guard": False}}


class TestPadHoldRules(unittest.TestCase):
    """Review fix 10/16 (finding 2): letter G resolves to the held
    G_decided_only, so "decided strikes alone" mixed in the 180 s pad hold
    and no rule tracked what the hold is worth."""

    def setUp(self):
        clear_data()

    def _write(self, g0, g_minus_g0, with_g0=True, with_e=True, e_minus_b=0.0, days=3):
        rng = random.Random(17)
        for i in range(days):
            day = (_dt.date(2026, 10, 15) + _dt.timedelta(days=i)).isoformat()
            os.makedirs(os.path.join(DATA, "sim", day), exist_ok=True)
            for h in range(24):
                ev = event_ticker(day, h)
                b = 13.0 + rng.gauss(0, 1.0)
                x0 = g0 + rng.gauss(0, 0.3)
                nets = {"A_base": b - 3.0, "B_pull": b, "C_back": b, "D_size": b,
                        "F_full": b, "G_decided_only": x0 + g_minus_g0 + rng.gauss(0, 0.1),
                        "H_noguard": b - 2.0}
                if with_e:
                    nets["E_decided"] = b + e_minus_b + rng.gauss(0, 0.02)
                if with_g0:
                    nets["G0_nohold"] = x0
                variants = {}
                for n, x in nets.items():
                    variants[f"{n}|cons"] = vrec(x)
                    variants[f"{n}|opt"] = vrec(x + 1.0)
                C.write_json_atomic(os.path.join(DATA, "sim", day, f"{ev}.json"),
                                    erec(ev, variants, paying=0.5))

    def _ctx(self, variants=None):
        variants = HOLD_VARIANTS if variants is None else variants
        return R.build_context("2026-10-17", policy=R.Policy(HOLD_DEFAULTS, dict(variants)),
                               with_health=False, now=NOW)

    def test_resolve_g_and_g0(self):
        names = ["G0_nohold", "G_decided_only"]
        self.assertEqual(R.resolve_variant("G", names), "G_decided_only")
        self.assertEqual(R.resolve_variant("G0", names), "G0_nohold")
        self.assertIsNone(R.resolve_variant("G0", ["G_decided_only"]))

    def test_decided_alone_uses_g0_and_hold_rule_fires(self):
        # E-B is noise; G0 earns +5/event -> decided alone on G0; G = G0 - 0.5
        self._write(g0=5.0, g_minus_g0=-0.5)
        ctx = self._ctx()
        s = by_id(ctx["suggestions"])
        dec = s["decided"]
        self.assertIn("G0_nohold alone (decided only)", dec["evidence"])
        self.assertNotIn("pad hold", dec["evidence"])
        self.assertTrue(dec["title"].startswith("Decided strikes alone earn $1"), dec["title"])
        self.assertAlmostEqual(dec["usd"], 120.0, delta=6)          # G0's +5 x 24
        self.assertIn("decided False -> True", dec["change"])
        self.assertNotIn("pad_hold_open_s", dec["change"])           # src = E
        hold = s["padhold"]
        self.assertIn("Pad hold value (G-G0)", hold["evidence"])
        self.assertIn("over 72 events / 3 days", hold["evidence"])
        self.assertAlmostEqual(hold["usd"], -12.0, delta=1)          # -0.5 x 24
        self.assertIn("first 180 s after the open costs", hold["title"])
        self.assertEqual(hold["change"], "pad_hold_open_s 180 -> 0 (G0_nohold's setting)")
        self.assertTrue(hold["action"])
        self.assertFalse(hold["suppressed"])
        self.assertFalse(any("G0" in n for n in ctx["notes"]))      # never a "missing" letter
        self.assertIn("Pad hold value (G-G0)", R.render_html(ctx))

    def test_hold_that_saves_says_keep(self):
        self._write(g0=1.0, g_minus_g0=+0.8)
        hold = by_id(self._ctx()["suggestions"])["padhold"]
        self.assertAlmostEqual(hold["usd"], 19.2, delta=1)
        self.assertIn("saves", hold["title"])
        self.assertEqual(hold["change"],
                         "keep pad_hold_open_s=180 (G_decided_only, not G0_nohold)")
        self.assertFalse(hold["action"])

    def test_without_g0_the_hold_is_named(self):
        self._write(g0=5.0, g_minus_g0=0.0, with_g0=False)
        variants = {k: v for k, v in HOLD_VARIANTS.items() if k != "G0_nohold"}
        s = by_id(self._ctx(variants)["suggestions"])
        self.assertNotIn("padhold", s)
        dec = s["decided"]
        self.assertIn("G_decided_only alone (decided only + 180 s pad hold)", dec["evidence"])
        self.assertTrue(dec["title"].startswith(
            "Decided strikes alone with the 180 s pad hold earn"), dec["title"])
        self.assertIn("decided False -> True", dec["change"])        # src = E

    def test_without_e_or_g0_the_change_names_the_hold(self):
        self._write(g0=5.0, g_minus_g0=0.0, with_g0=False, with_e=False)
        variants = {k: v for k, v in HOLD_VARIANTS.items()
                    if k not in ("G0_nohold", "E_decided")}
        dec = by_id(self._ctx(variants)["suggestions"])["decided"]
        self.assertIn("with the 180 s pad hold", dec["title"])
        self.assertIn("pad_hold_open_s 0 -> 180", dec["change"])

    def test_no_hold_difference_no_rule(self):
        self._write(g0=5.0, g_minus_g0=0.0)
        variants = dict(HOLD_VARIANTS, G_decided_only={"decided": True, "q_live": 0})
        s = by_id(self._ctx(variants)["suggestions"])
        self.assertNotIn("padhold", s)
        self.assertIn("G0_nohold alone (decided only)", s["decided"]["evidence"])


UNTIL = "2026-10-23T12:00:00Z"        # the collector stops 10/23 08:00 ET


def flat_day(day, net_per_day, hours=range(24), name="B_pull"):
    """24 events (or `hours`) each earning net_per_day / 24 on name|cons."""
    os.makedirs(os.path.join(DATA, "sim", day), exist_ok=True)
    for h in hours:
        ev = event_ticker(day, h)
        C.write_json_atomic(os.path.join(DATA, "sim", day, f"{ev}.json"),
                            erec(ev, {f"{name}|cons": vrec(net_per_day / 24.0)}))


class TestPartialLastDay(unittest.TestCase):
    """Review fix: the shadow's last ET day (10/23, collector stops at 08:00
    ET) must not dilute the verdict, and its post-shadow hours are not
    missing events."""

    def setUp(self):
        clear_data()
        self.p = mock.patch.object(C, "SHADOW_UNTIL_UTC", UNTIL)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _write(self):
        for i in range(8):                                    # 10/15 .. 10/22
            flat_day((_dt.date(2026, 10, 15) + _dt.timedelta(days=i)).isoformat(), 520.0)
        flat_day("2026-10-23", 520.0, hours=range(8))         # events 00..07

    def test_partial_day_helpers(self):
        self.assertTrue(R.is_partial_end("2026-10-23"))
        self.assertFalse(R.is_partial_end("2026-10-22"))
        self.assertAlmostEqual(R.day_weight("2026-10-23"), 8 / 24)
        self.assertEqual(R.day_weight("2026-10-22"), 1.0)
        self.assertIn("partial: the shadow ended 08:00 ET", R.day_label("2026-10-23"))
        due, later = R.expected_events("2026-10-23", C.parse_iso("2026-10-24T12:30:00Z"),
                                       until=C.parse_iso(UNTIL))
        self.assertEqual(len(due), 8)                         # hour 08 settles AT the stop
        self.assertEqual(later, [])
        self.assertEqual(due[-1], event_ticker("2026-10-23", 7))

    def test_verdict_ignores_partial_last_day(self):
        self._write()
        now = C.parse_iso("2026-10-24T12:30:00Z")
        w8 = R.build_context("2026-10-22", policy=POLICY, with_health=False, now=now)["week"]
        self.assertEqual((w8["verdict"], w8["days"], w8["pos"]), ("GO", 8, 8))
        self.assertAlmostEqual(w8["mean"], 520.0, places=6)
        ctx = R.build_context("2026-10-23", policy=POLICY, with_health=True, now=now)
        w9 = ctx["week"]
        # before the fix: EXTEND, mean ~$484, 9 days, need 7
        self.assertEqual(w9["verdict"], "GO")
        self.assertEqual((w9["days"], w9["pos"], w9["need"]), (8, 8, 6))
        self.assertAlmostEqual(w9["mean"], 520.0, places=6)
        self.assertEqual(w9["partial_days"], ["2026-10-23"])
        self.assertIn("partial last day 2026-10-23 (8 events", w9["reason"])
        win = ctx["win"]
        self.assertEqual(win.partial, {"2026-10-23"})
        self.assertAlmostEqual(win.per_day("B_pull|cons"), 520.0, places=6)   # weighted 8/24
        ev = ctx["health"]["events"]
        self.assertEqual((ev["due"], ev["simmed"], ev["outside"]), (8, 8, 16))
        self.assertEqual(ev["missing"], [])                   # was 15 x "collector down"
        self.assertTrue(ctx["health"]["shadow_ended"])
        page = R.render_html(ctx)
        self.assertIn("d9*", page)
        self.assertIn("is partial (the shadow ended inside it; 8 of 24 event-hours)", page)
        self.assertIn("16 hour(s) outside the shadow", page)
        self.assertNotIn("Missing events", page)
        self.assertIn("8/8", page)                             # days + over full days


class TestRuleFixes(unittest.TestCase):
    """Review fixes in the losing-hours and tau rules."""

    def _noise_win(self, seed, days=3, mu=10.0, sd=30.0):
        rng = random.Random(seed)
        dd = {}
        for i in range(days):
            day = (_dt.date(2026, 10, 15) + _dt.timedelta(days=i)).isoformat()
            dd[day] = [erec(event_ticker(day, h), {"B_pull|cons": vrec(rng.gauss(mu, sd))})
                       for h in range(24)]
        return R.Window(dd)

    def test_hour_rule_quiet_on_noise(self):
        fired = 0
        for seed in range(200):
            out = []
            R._hour_rule(self._noise_win(seed), "2026-10-17", POLICY, "B_pull", out)
            fired += any(not s["suppressed"] for s in out)
            for s in out:
                if s["suppressed"]:
                    self.assertIn("no hour passes per-hour z", s["why"])
                    self.assertEqual(s["change"], "no change")
        self.assertLessEqual(fired, 5)     # before the fix: 179 of 200

    def test_hour_rule_needs_three_days(self):
        rng = random.Random(4)
        dd = {}
        for i in range(2):
            day = (_dt.date(2026, 10, 15) + _dt.timedelta(days=i)).isoformat()
            dd[day] = [erec(event_ticker(day, h), {"B_pull|cons": vrec(
                (-40.0 if h == 5 else 10.0) + rng.gauss(0, 1))}) for h in range(24)]
        out = []
        R._hour_rule(R.Window(dd), "2026-10-16", POLICY, "B_pull", out)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["suppressed"])

    def _tau_win(self, bt, names=("B_pull",)):
        dd = {}
        for i in range(2):
            day = (_dt.date(2026, 10, 15) + _dt.timedelta(days=i)).isoformat()
            dd[day] = [erec(event_ticker(day, h), {f"{n}|cons": vrec(-2.0, by_tau=bt)
                                                   for n in names}) for h in range(24)]
        return R.Window(dd)

    def test_tau_rule_ignores_buckets_below_live_stop(self):
        # live_stop_min 5: 0-5 loses ~-$84/day (pads / decided), 5-10 EARNS
        bt = {"0-5": {"reward": 0.5, "pnl": -4.0}, "5-10": {"reward": 1.0, "pnl": 0.5}}
        pol = R.Policy(dict(DEFAULT_POLICY, live_stop_min=5), {"B_pull": {}})
        out = []
        R._tau_rule(self._tau_win(bt), "2026-10-16", pol, "B_pull", out)
        self.assertEqual(out, [])          # before the fix: 'raise live_stop_min 5 -> 10'

    def test_tau_rule_skips_variants_without_live_quotes(self):
        bt = {"5-10": {"reward": 0.5, "pnl": -6.0}}
        pol = R.Policy(dict(DEFAULT_POLICY, live=True), {"G_only": {"live": False}})
        out = []
        R._tau_rule(self._tau_win(bt, ("G_only",)), "2026-10-16", pol, "G_only", out)
        self.assertEqual(out, [])

    def test_tau_rule_straddling_bucket_is_flagged_not_acted_on(self):
        bt = {"0-5": {"reward": 0.5, "pnl": -4.0}, "5-10": {"reward": 1.0, "pnl": 0.5}}
        out = []
        R._tau_rule(self._tau_win(bt), "2026-10-16", POLICY, "B_pull", out)   # stop 3
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["suppressed"])
        self.assertIn("mixes tau < 3 min", out[0]["why"])
        self.assertIn("$", out[0]["evidence"])


class TestHealthFixes(unittest.TestCase):
    def setUp(self):
        clear_data()

    def test_ws_restart_without_disconnect_keeps_uptime(self):
        day = "2026-10-15"
        t0, t1 = C.et_day_bounds(day)
        for k in range(2880):                       # a ws row every 30 s all day
            rx = t0 + 30 * k + 1
            C.append_jsonl(C.raw_file("ws", rx), {"rx": rx, "m": {"type": "orderbook_delta"}})
        kill = t0 + 15 * 3600 + 60                  # hard kill + relaunch: one 'connect'
        C.append_jsonl(C.raw_file("ws", kill), {"rx": kill, "marker": "connect", "detail": ""})
        ws = R.ws_health(t0, t1)
        self.assertEqual(ws["connects"], 1)
        self.assertAlmostEqual(ws["uptime"], 1.0)   # before the fix: 0.375

    def test_ws_overnight_disconnect_still_counts_down(self):
        day = "2026-10-15"
        t0, t1 = C.et_day_bounds(day)
        back = t0 + 120 * 60                         # reconnects at 02:00 ET, no data before
        C.append_jsonl(C.raw_file("ws", back), {"rx": back, "marker": "connect", "detail": ""})
        rx = back + 1
        while rx < t1:
            C.append_jsonl(C.raw_file("ws", rx), {"rx": rx, "m": {"type": "orderbook_delta"}})
            rx += 30
        ws = R.ws_health(t0, t1)
        self.assertAlmostEqual(ws["uptime"], (1440 - 120) / 1440, delta=2 / 1440)

    def test_day0_health_starts_at_recording(self):
        start = C.parse_iso("2026-10-15T00:39:20Z")   # 10/14 20:39 ET
        end = C.parse_iso("2026-10-15T04:00:00Z")     # 10/15 00:00 ET
        rx = start
        while rx < end:
            C.append_jsonl(C.raw_file("ws", rx), {"rx": rx, "m": {"type": "orderbook_delta"}})
            rx += 30
        m = math_floor_min(start) + 59
        while m < end:
            C.append_jsonl(C.raw_file("meta", m), {"rx": m, "kind": "poll", "data": {
                "n": 40, "n429": 0, "nerr": 0, "lat_ms_p50": 80, "lat_ms_max": 200,
                "signed": False}})
            m += 60
        self.assertAlmostEqual(R.recording_start(), start)
        h = R.collector_health("2026-10-14", [], now=C.parse_iso("2026-10-15T12:30:00Z"))
        self.assertTrue(h["started_late"])
        self.assertEqual(h["ws"]["minutes"], 201)                 # 20:39 .. 24:00 ET
        self.assertGreater(h["ws"]["uptime"], 0.99)               # before the fix: ~0.14
        self.assertGreater(h["poll"]["coverage"], 0.99)
        self.assertEqual(h["events"]["due"], 3)                   # hours 21, 22, 23
        self.assertEqual(h["events"]["outside"], 21)
        rng = random.Random(6)
        write_day("2026-10-14", rng, hours=range(21, 24))
        win = R.Window({"2026-10-14": R.load_sim_day("2026-10-14")})
        sugg, _ = R.suggest(win, "2026-10-14", POLICY, {}, {}, h)
        self.assertNotIn("opsws", by_id(sugg))
        self.assertNotIn("opspoll", by_id(sugg))
        ctx = R.build_context("2026-10-14", policy=POLICY, with_health=False,
                              now=C.parse_iso("2026-10-15T12:30:00Z"))
        ctx["health"] = h
        self.assertIn("Health covers 20:39-00:00 ET only (the recording began then)",
                      R.render_html(ctx))

    def test_empty_reason_names_sim_errors(self):
        day = "2026-10-16"
        t0, _ = C.et_day_bounds(day)
        C.append_jsonl(C.raw_file("wx", t0 + 100), {"rx": t0 + 100, "city": "miami",
                                                    "t": int(t0 * 1000), "status": "incomplete",
                                                    "v": None, "stations": []})
        for hh in range(24):
            ev = event_ticker(day, hh)
            C.write_json_atomic(C.settled_file(ev), {"event_ticker": ev})
        summ = {"ok": 0, "pending": [],
                "errors": [(event_ticker(day, hh), "KeyError: 'x'") for hh in range(24)]}
        ctx = R.build_context(day, sim_ran=True, policy=POLICY, now=NOW, sim_summary=summ)
        reason = R._empty_reason(ctx)
        self.assertIn("every due event raised in the sim", reason)
        self.assertNotIn("settlement pull", reason)   # before the fix: blamed the pull
        self.assertIn("every due event raised in the sim", R.render_text(ctx))
        self.assertIn("every due event raised in the sim", R.render_html(ctx))


def math_floor_min(ts):
    return (int(ts) // 60) * 60


class TestCollectorSettingsAndOps(unittest.TestCase):
    SRC = ('import os\n'
           'WX_LO = float(os.environ.get("TEMP_SHADOW_WX_LO", "13"))\n'
           'WX_HI = float(os.environ.get("TEMP_SHADOW_WX_HI", "50"))\n'
           'WX_FAST, WX_SLOW = 1.0, 5.0\n'
           'WX_MIN_GAP = 0.8\n'
           'SIGN_AFTER_429 = 3\n'
           'SIGN_ON_429 = os.environ.get("TEMP_SHADOW_SIGN_ON_429", "1") != "0"\n'
           'WX_YIELD_FAST = 2.0\n')

    def setUp(self):
        self.path = os.path.join(DATA, "fake_collector.py")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.SRC)
        self.cfg = R.collector_settings(self.path, env={})
        rng = random.Random(3)
        clear_data()
        write_day("2026-10-15", rng, hours=range(4))
        self.win = R.Window({"2026-10-15": R.load_sim_day("2026-10-15")})

    def test_settings_read_with_env_overrides(self):
        self.assertEqual(self.cfg["WX_LO"], 13.0)
        self.assertEqual((self.cfg["WX_FAST"], self.cfg["WX_SLOW"]), (1.0, 5.0))
        self.assertIs(self.cfg["SIGN_ON_429"], True)
        self.assertEqual(self.cfg["_source"], "fake_collector.py")
        env = {"TEMP_SHADOW_WX_LO": "11", "TEMP_SHADOW_SIGN_ON_429": "0"}
        cfg = R.collector_settings(self.path, env=env)
        self.assertEqual(cfg["WX_LO"], 11.0)
        self.assertIs(cfg["SIGN_ON_429"], False)
        miss = R.collector_settings(os.path.join(DATA, "nope.py"), env={})
        self.assertEqual(miss["WX_LO"], 18.0)

    def _ops(self, wx=None, poll=None):
        h = {"wx": wx or {}, "poll": poll or {}, "collector_cfg": self.cfg}
        out = []
        R._ops_rules(h, self.win, "2026-10-15", "B_pull", out)
        return by_id(out)

    def test_lag_rule_never_raises_wx_lo(self):
        wx = {"obs_lag_p90": 3.4, "obs_lag_median": 2.0, "obs_lag_n": 10,
              "land_sec_p10": 16.0, "land_sec_p90": 30.0}
        s = self._ops(wx=wx)["opslag"]
        self.assertNotIn("WX_LO", s["change"])      # before the fix: 'WX_LO 18 -> 14'
        self.assertIn("WX_FAST 1 -> 0.8", s["change"])
        s = self._ops(wx=dict(wx, land_sec_p10=10.5))["opslag"]
        self.assertIn("WX_LO 13 -> 8", s["change"])
        self.assertIn(":13-:50", s["change"])

    def test_429_rule_is_a_rate_and_never_says_sign(self):
        self.assertNotIn("ops429", self._ops(poll={"n": 60000, "n429": 1000}))   # 1.7%
        s = self._ops(poll={"n": 1046, "n429": 24, "signed_minutes": 0})["ops429"]
        self.assertIn("back off", s["change"])
        self.assertIn("WX_SLOW 5 -> 10", s["change"])
        self.assertIn(":13-:50", s["change"])
        self.assertNotIn("SIGN_AFTER_429", s["change"])
        self.assertNotIn(":18", s["change"])
        s = self._ops(poll={"n": 60000, "n429": 30, "signed_minutes": 4})["ops429"]
        self.assertIn("TEMP_SHADOW_SIGN_ON_429=0", s["change"])
        self.assertIn("IMM", s["change"])


def _importable(name):
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001
        return False


class TestRealModules(unittest.TestCase):
    """Contract checks against the sibling modules when they import."""

    @unittest.skipUnless(_importable("temp_shadow_sim"), "temp_shadow_sim not importable")
    def test_real_variant_grid_resolves_letters(self):
        defaults, variants, err = R.load_policy_defs()
        self.assertIsNone(err)
        names = sorted(variants)
        for c in "ABCDEFGH":
            self.assertIsNotNone(R.resolve_variant(c, names), (c, names))
        pol = R.Policy(defaults, variants)
        b, d = R.resolve_variant("B", names), R.resolve_variant("D", names)
        self.assertIsNotNone(R._opt_num(pol.get(b, "q_live")))
        self.assertNotEqual(pol.get(b, "q_live"), pol.get(d, "q_live"))
        self.assertIsNotNone(pol.get(b, "live_stop_min"))
        self.assertIsNotNone(pol.get(b, "guard_k"))
        # G0 (G without the pad hold) resolves; the pad-hold rule's pair
        # differs by the hold alone
        g, g0 = R.resolve_variant("G", names), R.resolve_variant("G0", names)
        self.assertIsNotNone(g0)
        self.assertNotEqual(g, g0)
        self.assertEqual(pol.diff_text(g0, g), "pad_hold_open_s 0 -> 180")
        self.assertEqual((R._hold_s(pol, g), R._hold_s(pol, g0)), (180.0, 0.0))

    @unittest.skipUnless(_importable("temp_shadow_sim"), "temp_shadow_sim not importable")
    def test_real_sim_version_is_read(self):
        import temp_shadow_sim as sim
        self.assertEqual(R.current_sim_version(), str(sim.SIM_VERSION))

    @unittest.skipUnless(_importable("temp_shadow_model"), "temp_shadow_model not importable")
    def test_real_replica_matches_lattice(self):
        stations = [("KMIA1M", 82.4), ("KOPF1M", 82.4), ("KFLL1M", 80.6), ("KFXE1M", 80.6),
                    ("KPMP1M", 80.6)]
        v = C.index_from_sum(sum(round(C.f_to_c(f)) for _, f in stations))
        t = int(C.parse_iso("2026-10-15T14:00:00Z") * 1000)
        row = {"rx": t / 1000 + 310, "t": t, "status": "normal", "v": v,
               "stations": [{"station_id": s, "temp_f": f, "code": "ok"} for s, f in stations]}
        bad = dict(row, v=v + 0.36)
        rep = R.replica_check({t: [row], t + 60000: [dict(bad, t=t + 60000)]})
        self.assertNotIn("error", rep)
        self.assertEqual((rep["n"], rep["exact"]), (2, 1))


def tearDownModule():
    shutil.rmtree(DATA, ignore_errors=True)


def swp_nets(rng, nets):
    """swp nets for scenario_nets' cons nets: sweepers take the 1c fillers
    of every variant but the decided-only one (G), which has none here --
    best on cons is D_size, best on swp G_decidedonly; B-A stays +3, D-B
    turns to -10."""
    out = {n: x - 20.0 for n, x in nets.items()}
    out["D_size"] = nets["D_size"] - 32.0
    out["G_decidedonly"] = 4.0 + rng.gauss(0, 0.5)
    return out


def write_day_swp(day, rng, hours=range(24), losing_hours=()):
    os.makedirs(os.path.join(DATA, "sim", day), exist_ok=True)
    for h in hours:
        ev = event_ticker(day, h)
        nets = scenario_nets(rng, h)
        sw = swp_nets(rng, nets)
        if h in losing_hours:
            sw = {k: v - 30.0 for k, v in sw.items()}
        variants = {}
        for n, x in nets.items():
            variants[f"{n}|opt"] = vrec(x + 1.0)
            variants[f"{n}|cons"] = vrec(x)
            r = vrec(sw[n])
            r.update(draws=8, net_sd=2.0, net_se=round(2.0 / 8 ** 0.5, 6),
                     filler_contracts=0.0 if n == "G_decidedonly" else 900.0,
                     net_cal=sw[n] + 1.5, swp_exact=n == "G_decidedonly")
            variants[f"{n}|swp"] = r
        C.write_json_atomic(os.path.join(DATA, "sim", day, f"{ev}.json"),
                            erec(ev, variants))


class TestSwpHeadline(unittest.TestCase):
    """Jack 2026-10-16, "1": the sweep-aware report column. Three fill-model
    columns; the headline best variant, the rules, the cumulative days + and
    the week verdict on swp; the subject says swp; net_cal shown as
    "expected (calibrated tails)" with its caveat."""

    def setUp(self):
        clear_data()

    def test_three_columns_and_best_on_swp(self):
        rng = random.Random(21)
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            write_day_swp(day, rng)
        ctx = R.build_context("2026-10-17", policy=POLICY, with_health=False, now=NOW)
        self.assertEqual(ctx["fill"], "swp")
        self.assertEqual(ctx["best"], "G_decidedonly")
        self.assertEqual(ctx["best_swp"], "G_decidedonly")
        self.assertEqual(ctx["best_cons"], "D_size")               # cons would have said D
        self.assertEqual(ctx["win"].best("swp"), "G_decidedonly")
        page = R.render_html(ctx)
        for needle in ("Best swp variant <b>G_decidedonly</b>", "Monte Carlo se",
                       "Chosen on swp net", ">opt</th>", ">cons</th>", ">swp (sweep-aware)</th>",
                       "+/- mc se", "1c fillers", "expected (cal. tails)",
                       "swp = the sweep-aware fill model", "Mean of 8 Monte Carlo draws",
                       "Expected (calibrated tails) = reward", "an ESTIMATE",
                       "Best cons variant D_size", "swp sum", "swp /day", "cons sum", "opt sum",
                       "days + and the day columns are swp net", "Pairs use the swp fill model",
                       "Variant G_decidedonly (swp fills, the sweep-aware model; means over 8 Monte Carlo"):
            self.assertIn(needle, page)
        # the variants table: one group of columns per fill model, opt / cons / swp
        self.assertLess(page.index(">opt</th>"), page.index(">cons</th>"))
        self.assertLess(page.index(">cons</th>"), page.index(">swp (sweep-aware)</th>"))
        subj = R.subject_line(ctx)
        self.assertTrue(subj.startswith("Temp shadow day 3 (2026-10-17): best G_decidedonly net +$"),
                        subj)
        self.assertIn("/day on swp (reward", subj)
        txt = R.render_text(ctx)
        self.assertIn("G_decidedonly (swp)", txt)
        self.assertIn("Monte Carlo se", txt)
        self.assertIn("expected at calibrated tails", txt)
        # aggregates: Monte Carlo se adds in quadrature, net_cal and fillers sum
        a = ctx["agg"]["G_decidedonly|swp"]
        self.assertAlmostEqual(a["net_se"], math.sqrt(24) * 2.0 / math.sqrt(8), places=4)
        self.assertAlmostEqual(a["net_cal"], a["net"] + 24 * 1.5, places=4)
        self.assertEqual(ctx["agg"]["D_size|swp"]["filler_contracts"], 24 * 900.0)
        self.assertIsNone(ctx["agg"]["D_size|cons"]["net_se"])
        self.assertAlmostEqual(ctx["win"].mc_se("G_decidedonly|swp"),
                               math.sqrt(72) * 2.0 / math.sqrt(8), places=4)

    def test_rules_compare_on_swp(self):
        rng = random.Random(23)
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            write_day_swp(day, rng)
        ctx = R.build_context("2026-10-17", policy=POLICY, with_health=False, now=NOW)
        s = by_id(ctx["suggestions"])
        # B-A is +3 on swp as on cons: keep the window
        self.assertIn("keep", s["window"]["title"])
        self.assertAlmostEqual(s["window"]["usd"], 72.0, delta=8)
        # D-B: +2 an event on cons, -10 on swp -> the rule says it loses
        self.assertIn("loses", s["size"]["title"])
        self.assertLess(s["size"]["usd"], -200)
        self.assertFalse(s["size"]["suppressed"])
        # the sweep gap on the swp-best variant (G has no fillers: cons ~ -0.1, swp ~ +4)
        self.assertIn("Sweep risk on G_decidedonly", s["fillgap"]["title"])
        self.assertIn("cons - swp on G_decidedonly", s["fillgap"]["evidence"])
        # F trails the swp-best G
        self.assertIn("trails G_decidedonly", s["full"]["title"])
        self.assertFalse([n for n in ctx["notes"] if "No swp results" in n])

    def test_hour_rule_on_a_decided_only_best_names_quote_hours(self):
        rng = random.Random(31)
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            write_day_swp(day, rng, losing_hours=(2, 3))
        pol_ = R.Policy(dict(DEFAULT_POLICY, live=True, quote_hours_et=None),
                        dict(VARIANTS, G_decidedonly={"decided": True, "q_live": 0,
                                                      "live": False}))
        ctx = R.build_context("2026-10-17", policy=pol_, with_health=False, now=NOW)
        self.assertEqual(ctx["best"], "G_decidedonly")
        s = by_id(ctx["suggestions"])
        self.assertFalse(s["hours"]["suppressed"])
        self.assertIn("02, 03", s["hours"]["title"])
        keep = [h for h in range(24) if h not in (2, 3)]
        self.assertEqual(s["hours"]["change"],
                         "stop quoting those settle hours: quote_hours_et all -> "
                         + "[" + ",".join(str(h) for h in keep) + "]")

    def test_week_verdict_on_swp(self):
        rng = random.Random(25)
        for i in range(7):
            write_day_swp((_dt.date(2026, 10, 15) + _dt.timedelta(days=i)).isoformat(), rng,
                          hours=range(6))
        ctx = R.build_context("2026-10-21", policy=POLICY, with_health=False, now=NOW)
        w = ctx["week"]
        self.assertEqual(w["fill"], "swp")
        self.assertEqual(w["best"], "G_decidedonly")
        self.assertEqual(w["verdict"], "EXTEND")      # positive every day, ~$24/day < $500
        self.assertEqual(w["pos"], 7)
        self.assertTrue(any(r.startswith("Fill model: swp") and "Monte Carlo se" in r
                            and "expected at calibrated tails" in r for r in w["risks"]),
                        w["risks"])
        self.assertIn("-- week: EXTEND", R.subject_line(ctx))

    def test_older_output_without_swp_falls_back_to_cons(self):
        rng = random.Random(27)
        write_day("2026-10-15", rng)
        ctx = R.build_context("2026-10-15", policy=POLICY, with_health=False, now=NOW)
        self.assertEqual(ctx["fill"], "cons")
        self.assertEqual(ctx["best"], ctx["best_cons"])
        self.assertIn("No swp results in the window", " ".join(ctx["notes"]))
        page = R.render_html(ctx)
        self.assertIn("Best cons variant", page)
        self.assertIn("the headline uses cons", page)
        self.assertNotIn(">swp (sweep-aware)</th>", page)
        self.assertIn("/day on cons (reward", R.subject_line(ctx))

if __name__ == "__main__":
    unittest.main()
