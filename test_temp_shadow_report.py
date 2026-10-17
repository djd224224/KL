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
import glob                     # noqa: E402
import json                     # noqa: E402
import math                     # noqa: E402
import random                   # noqa: E402
import shutil                   # noqa: E402
import subprocess               # noqa: E402
import sys                      # noqa: E402
import time                     # noqa: E402
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
    for sub in ("sim", "raw", "reports", "settled", "prereg"):
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


FAV_DEFAULTS = dict(DEFAULT_POLICY, live=True, quote_hours_et=None, q_fav=300)
FAV_VARIANTS = dict(VARIANTS,
                    FAV_night={"live": False, "decided": True, "q_pad": 0,
                               "quote_hours_et": [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7]},
                    FAV_night_agree={"live": False, "decided": True, "q_pad": 0,
                                     "quote_hours_et": [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7],
                                     "fav_market_min_p": 0.95})
FAV_POLICY = R.Policy(FAV_DEFAULTS, FAV_VARIANTS)


def pin_ident(names=None):
    """The registered rule and model a sim file of the tested variants
    carries (temp_shadow_sim SIM_VERSION 8 writes these fields): the
    pre-registered tests count only files that match PREREG_TESTS /
    PREREG_PIN_MODEL."""
    pm = R.PREREG_PIN_MODEL
    tv = {t["variant"]: t for t in R.PREREG_TESTS}
    names = list(tv) if names is None else list(names)
    return json.loads(json.dumps({
        "sim_version": pm["sim_version"],
        "policies": {n: tv[n]["overrides"] for n in names if n in tv},
        "policy_full": {n: tv[n]["policy"] for n in names if n in tv},
        "swp": dict(pm["swp"]),
        "stress": dict(pm["stress"], scope="nolive", variants=names),
        "fair_model": {"id": pm["fair_model_id"], "source": "model_params.json"}}))


def srec(net, cal=None, fav_fills=None, draws=6):
    """A stress (or swp) result: vrec + the Monte Carlo / tails extras."""
    r = vrec(net)
    r.update(draws=draws, net_sd=1.0, net_se=round(1.0 / draws ** 0.5, 6), filler_contracts=0.0,
             draw_net=[net] * draws, net_cal=net + 1.0 if cal is None else cal,
             stress_exact=False, fav_fills=fav_fills if fav_fills is not None else {})
    return r


def write_fav_days(day_nets, base=None, hours=range(24), agree=None, fav_hits=None,
                   missing=(), ident=None, extra=None):
    """Sim files whose events hold FAV_night|stress (and FAV_night_agree|stress)
    with per-event nets: day_nets {day: net per event or [net per hour]};
    agree likewise for FAV_night_agree (default = FAV_night's); base adds
    B_pull|cons / |swp so the window has other variants; missing = events
    written without any stress result. Every file carries the registered
    rule and model (pin_ident); ident(day, hour) -> fields merged over it
    (an edited rule, an older SIM_VERSION ...); extra {variant: net or
    {day: net}} adds more variants' stress / swp results."""
    for day, nets in day_nets.items():
        os.makedirs(os.path.join(DATA, "sim", day), exist_ok=True)
        for i, h in enumerate(hours):
            ev = event_ticker(day, h)
            x = nets[i] if isinstance(nets, (list, tuple)) else nets
            ag = (agree or {}).get(day, x)
            y = ag[i] if isinstance(ag, (list, tuple)) else ag
            variants = {"B_pull|cons": vrec(base if base is not None else 1.0),
                        "B_pull|swp": srec(base if base is not None else 1.0)}
            if ev not in missing:
                variants["FAV_night|stress"] = srec(x)
                variants["FAV_night_agree|stress"] = srec(y)
                variants["FAV_night|swp"] = srec(x)
                variants["FAV_night_agree|swp"] = srec(y)
            for name, xn in (extra or {}).items():
                z = xn.get(day, 0.0) if isinstance(xn, dict) else xn
                variants[f"{name}|stress"] = srec(z)
                variants[f"{name}|swp"] = srec(z)
            rec = erec(ev, variants)
            rec.update(pin_ident())
            if ident is not None:
                rec.update(ident(day, h))
            if fav_hits is not None:
                rec["fav_hits"] = fav_hits
            C.write_json_atomic(os.path.join(DATA, "sim", day, f"{ev}.json"), rec)


def days_from(start, n):
    return [(_dt.date.fromisoformat(start) + _dt.timedelta(days=i)).isoformat()
            for i in range(n)]


class TestWeekSummary(unittest.TestCase):
    """Jack 2026-10-17 ("Yes. And Assume rewards are paid" / "1"): the day-7
    week summary uses the adopted go-live rules -- the pre-registered tests
    decide -- instead of the old GO / NO-GO / EXTEND thresholds on the best
    variant's realized net."""

    def setUp(self):
        clear_data()

    def _ctx(self, day):
        return R.build_context(day, policy=FAV_POLICY, with_health=False, now=NOW)

    def test_day7_test_a_pass_is_pilot_eligible(self):
        # 10/15-10/16 (before the windows) lose big: irrelevant to the tests
        nets = {d: -50.0 for d in days_from("2026-10-15", 2)}
        nets.update({d: 2.0 for d in days_from("2026-10-17", 5)})
        write_fav_days(nets)
        ctx = self._ctx("2026-10-21")
        w = ctx["week"]
        self.assertEqual(w["verdict"], "pilot eligible: FAV_night")
        self.assertEqual(w["eligible"], ["FAV_night"])
        self.assertEqual(w["best"], "FAV_night")
        self.assertEqual(w["config"]["quote_hours_et"], [20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7])
        self.assertIn("1/3 size, night hours only, a $300 daily halt", w["pilot"])
        self.assertIn("Test A (FAV_night, 10/17-10/21): PASS", w["reason"])
        self.assertIn("Test B (FAV_night_agree, 10/18-10/22): in progress (4/5 days)", w["reason"])
        self.assertNotIn("GO_MEAN_USD", dir(R))                 # the old bars are gone
        self.assertTrue(any(r.startswith("Fill model (FAV_night") for r in w["risks"]))
        page = R.render_html(ctx)
        # rule 4: the pilot runs the variant at 1/3 size
        self.assertEqual((w["config"]["q_fav"], w["pilot_config"]["q_fav"]), (300, 100))
        for needle in ("Week summary", "Day 7 of 7", "pilot eligible: FAV_night",
                       "Pilot config (FAV_night, rule 4: 1/3 size)",
                       "100 (1/3 of the variant&#x27;s 300)", "q_fav 300 -&gt; 100",
                       "the old GO / NO-GO thresholds"):
            self.assertIn(needle, page)
        self.assertNotIn("Recommended starting config", page)
        subj = R.subject_line(ctx)
        self.assertTrue(subj.endswith("-- week: pilot eligible: FAV_night"), subj)
        self.assertIn("Test A PASS (stress +$240; after registration PASS +$210), Test B in "
                      "progress 4/5 (stress +$192)", subj)
        txt = R.render_text(ctx)
        self.assertIn("Week verdict: pilot eligible: FAV_night", txt)
        self.assertIn("Pilot config (FAV_night, rule 4, 1/3 size): q_fav 100 (the variant's 300)",
                      txt)
        ctx6 = self._ctx("2026-10-20")
        self.assertIsNone(ctx6["week"])
        self.assertNotIn("Week summary", R.render_html(ctx6))

    def test_day7_in_progress_then_no_go(self):
        nets = {d: (-3.0 if d in ("2026-10-18", "2026-10-19") else 1.0)
                for d in days_from("2026-10-17", 6)}
        write_fav_days(nets)                 # 3 of 5 days positive: A and B fail
        w7 = self._ctx("2026-10-21")["week"]
        self.assertEqual(w7["verdict"], "in progress")        # B still running
        self.assertIsNone(w7["best"])
        page = R.render_html(self._ctx("2026-10-21"))
        self.assertIn("No variant is eligible for a live pilot.", page)
        ctx8 = self._ctx("2026-10-22")
        self.assertEqual(ctx8["week"]["verdict"],
                         "NO-GO (no variant passes the pre-registered tests)")
        self.assertEqual([t["status"] for t in ctx8["tests"]], ["FAIL", "FAIL"])
        self.assertIn("-- week: NO-GO (no variant passes the pre-registered tests)",
                      R.subject_line(ctx8))

    def test_week_needs_both_tests_finished_for_no_go(self):
        self.assertEqual(R.prereg_verdict([{"variant": "a", "status": "FAIL"},
                                           {"variant": "b", "status": "in progress"}]),
                         "in progress")
        self.assertEqual(R.prereg_verdict([{"variant": "a", "status": "FAIL"},
                                           {"variant": "b", "status": "PASS"}]),
                         "pilot eligible: b")
        self.assertEqual(R.prereg_verdict([{"variant": "a", "status": "FAIL"},
                                           {"variant": "b", "status": "FAIL"}]),
                         "NO-GO (no variant passes the pre-registered tests)")
        self.assertEqual(R.prereg_verdict([{"variant": "a", "status": "not started"}]),
                         "in progress")


class TestRenderAndHealth(unittest.TestCase):
    def setUp(self):
        clear_data()

    def test_three_days_html_and_cumulative(self):
        rng = random.Random(9)
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            write_day(day, rng)
        write_day("2026-10-14", rng, hours=range(20, 24))       # partial day 0
        ctx = R.build_context("2026-10-17", policy=POLICY, with_health=True, now=NOW)
        # cons / opt only (older sim output): no stress results, and no
        # fallback to another fill model (review of 338b4a5)
        self.assertEqual((ctx["fill"], ctx["rules_fill"]), ("stress", "cons"))
        self.assertIsNone(ctx["best"])
        self.assertTrue(ctx["headline"]["no_stress"])
        self.assertEqual(len(ctx["events"]), 24)
        self.assertEqual(ctx["day0_events"], 4)
        page = R.render_html(ctx)
        for needle in ("Day 3 of 7", "No variant passes (no stress results)",
                       "No stress results to rank in the window yet.",
                       "72 of 72 event(s) in the window have no stress results",
                       "Pre-registered tests", "Market structure", "Model",
                       "Data health", "Cumulative to date", "d1", "d3", "Day 0 (partial, 4 events)"):
            self.assertIn(needle, page)
        self.assertNotIn("Best cons variant", page)          # no single-day ranking
        self.assertNotIn("Headline variant", page)
        self.assertNotIn("Week summary", page)
        win = ctx["win"]
        self.assertEqual(win.days, ["2026-10-15", "2026-10-16", "2026-10-17"])
        self.assertEqual(len(win.days_with("D_size|cons")), 3)
        subj = R.subject_line(ctx)
        self.assertTrue(subj.startswith("Temp shadow day 3 (2026-10-17): no variant passes (no "
                                        "stress results); Test A in progress 1/5 (stress $0; "
                                        "after registration $0; 24 hours missing), Test B starts "
                                        "10/18"), subj)
        self.assertIn("[72 events without stress]", subj)
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
        # cons / opt only: no stress results, so no variant is named
        self.assertTrue(cmd[2].startswith("Temp shadow day 3 (2026-10-17): no variant passes (no "
                                          "stress results)"), cmd[2])
        self.assertEqual(cmd[3], self.out)
        self.assertIn("Headline: no variant passes (no stress results).", cmd[4])

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
        ctx = R.build_context("2026-10-23", policy=POLICY, with_health=True, now=now)
        w9 = ctx["week"]
        # the week verdict is the pre-registered tests' (their windows end
        # 10/21 and 10/22, before the partial last day); B_pull alone has no
        # results for their variants: every window hour lacks its stress
        # result, so both are INCOMPLETE (never PASS)
        self.assertEqual(w9["verdict"], "undecided (Test A INCOMPLETE; Test B INCOMPLETE)")
        self.assertEqual([t["last_day"] for t in ctx["tests"]], ["2026-10-21", "2026-10-22"])
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

    @unittest.skipUnless(_importable("temp_shadow_sim"), "temp_shadow_sim not importable")
    def test_preregistered_pins_match_the_code(self):
        # the registered rule and model are what the current code runs: the
        # tested variants' overrides and full policies, SIM_VERSION, and the
        # swp / stress identity of the repo tables (this sandbox has no
        # model/ overrides). The fair model's id needs the production
        # model_params.json (checked by hand: 4f5fd78c360d8717, 10/14 22:01).
        import json as _json
        import temp_shadow_sim as sim
        pm = R.PREREG_PIN_MODEL
        self.assertEqual(str(sim.SIM_VERSION), pm["sim_version"])
        full = _json.loads(_json.dumps(sim._resolve_variants([t["variant"]
                                                              for t in R.PREREG_TESTS])))
        for t in R.PREREG_TESTS:
            self.assertEqual(_json.loads(_json.dumps(sim.VARIANTS[t["variant"]])),
                             _json.loads(_json.dumps(t["overrides"])), t["id"])
            self.assertEqual(full[t["variant"]], _json.loads(_json.dumps(t["policy"])), t["id"])
        sim._SWP_CACHE.clear()
        swp = sim.swp_config()
        st = sim.stress_config(swp)
        self.assertEqual({k: swp["ident"].get(k) for k in pm["swp"]}, pm["swp"])
        self.assertEqual({k: st["ident"].get(k) for k in pm["stress"]}, pm["stress"])

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


def write_day_swp(day, rng, hours=range(24), losing_hours=(), stress=False):
    """opt / cons / swp (and with stress=True "|stress" = swp - 1 for every
    variant) per event."""
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
            if stress:
                st = vrec(sw[n] - 1.0)
                st.update(draws=8, net_sd=2.0, net_se=round(2.0 / 8 ** 0.5, 6),
                          filler_contracts=0.0 if n == "G_decidedonly" else 950.0,
                          net_cal=sw[n] + 0.5, stress_exact=False, fav_fills={})
                variants[f"{n}|stress"] = st
        C.write_json_atomic(os.path.join(DATA, "sim", day, f"{ev}.json"),
                            erec(ev, variants))


# G (decided only) quotes no live strikes: its net_cal is evidence; every
# other test variant quotes live strikes (live=True), where it is circular
POLICY_LIVE = R.Policy(dict(DEFAULT_POLICY, live=True),
                       dict(VARIANTS, G_decidedonly={"decided": True, "q_live": 0,
                                                     "live": False}))



class TestSwpHeadline(unittest.TestCase):
    """Jack 2026-10-16, "1": the sweep-aware report column; since 10/17
    (Jack: "1") the stress column beside it, the ranking / tests / verdict
    on stress, the tweak rules still on swp; net_cal shown as "expected
    (calibrated tails)" with its caveat."""

    def setUp(self):
        clear_data()

    def test_four_columns_and_ranking_on_stress(self):
        rng = random.Random(21)
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            write_day_swp(day, rng, stress=True)
        ctx = R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False, now=NOW)
        self.assertEqual((ctx["fill"], ctx["rules_fill"]), ("stress", "swp"))
        self.assertEqual(ctx["best"], "G_decidedonly")
        self.assertTrue(ctx["headline"]["pass"])
        self.assertEqual(ctx["rank"][0]["name"], "G_decidedonly")
        self.assertEqual(ctx["win"].best("swp"), "G_decidedonly")
        self.assertNotIn("best_cons", ctx)                          # no single-day ranking
        page = R.render_html(ctx)
        for needle in ("Headline variant: <b>G_decidedonly</b>", ">opt</th>", ">cons</th>",
                       ">swp (sweep-aware)</th>", ">stress (decision model)</th>",
                       "+/- mc se", "1c fillers", "expected (cal. tails)",
                       "swp = the sweep-aware fill model", "Mean of 8 Monte Carlo draws",
                       "stress = swp plus level-clears at every price",
                       "Expected (calibrated tails) = reward", "an ESTIMATE",
                       "stress robust", "stress sum", "stress /day", "swp sum", "cons sum",
                       "opt sum", "days + and the day columns are stress net",
                       "Pairs use the swp fill model", "Ranked on stress net",
                       "robust stress total", "noise-dominated",
                       "Variant G_decidedonly (stress fills, the decision model; means over 8 "
                       "Monte Carlo draws; the headline variant)",
                       "By ET hour of the event", "By strike class", "By minutes to close",
                       "By side", "Worst 5 events"):
            self.assertIn(needle, page)
        # the variants table: one group of columns per fill model, opt / cons / swp / stress
        self.assertLess(page.index(">opt</th>"), page.index(">cons</th>"))
        self.assertLess(page.index(">cons</th>"), page.index(">swp (sweep-aware)</th>"))
        self.assertLess(page.index(">swp (sweep-aware)</th>"),
                        page.index(">stress (decision model)</th>"))
        subj = R.subject_line(ctx)
        self.assertTrue(subj.startswith("Temp shadow day 3 (2026-10-17): top G_decidedonly "
                                        "robust stress +$"), subj)
        self.assertNotIn("/day on", subj)
        txt = R.render_text(ctx)
        self.assertIn("G_decidedonly on the report day (stress)", txt)
        self.assertIn("Monte Carlo se", txt)
        self.assertIn("expected at calibrated tails", txt)
        self.assertIn("Ranking on robust stress totals", txt)
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
            write_day_swp(day, rng, losing_hours=(2, 3), stress=True)
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

    def test_week_verdict_without_the_test_variants_waits(self):
        # a positive swp-best variant (G, ~$24/day) no longer makes a week
        # verdict: the pre-registered tests decide, and their variants have
        # no stress results here, so the tests wait ("in progress")
        rng = random.Random(25)
        for i in range(7):
            write_day_swp((_dt.date(2026, 10, 15) + _dt.timedelta(days=i)).isoformat(), rng,
                          hours=range(6))
        ctx = R.build_context("2026-10-21", policy=POLICY_LIVE, with_health=False, now=NOW)
        w = ctx["week"]
        # Test A's window is over with every hour missing (00-05 simmed
        # without its stress result, the rest never recorded): INCOMPLETE;
        # Test B still runs
        self.assertEqual(w["verdict"], "in progress (Test A INCOMPLETE)")
        self.assertIsNone(w["best"])
        a = ctx["tests"][0]
        self.assertEqual((a["status"], len(a["missing"])), ("INCOMPLETE", 120))
        self.assertEqual(sum(1 for _e, why in a["missing"]
                             if why.startswith("its sim file has no stress result")), 30)
        self.assertEqual(ctx["tests"][1]["status"], "in progress")
        self.assertIn("-- week: in progress (Test A INCOMPLETE)", R.subject_line(ctx))

    def test_older_output_without_stress_never_falls_back(self):
        # the rules fall back to cons on output without swp; the ranking
        # never leaves stress (review of 338b4a5)
        rng = random.Random(27)
        write_day("2026-10-15", rng)
        ctx = R.build_context("2026-10-15", policy=POLICY, with_health=False, now=NOW)
        self.assertEqual((ctx["fill"], ctx["rules_fill"], ctx["rank"]), ("stress", "cons", []))
        self.assertIsNone(ctx["best"])
        self.assertIn("No swp results in the window", " ".join(ctx["notes"]))
        page = R.render_html(ctx)
        self.assertIn("No variant passes (no stress results)", page)
        self.assertNotIn(">swp (sweep-aware)</th>", page)
        self.assertNotIn(">stress (decision model)</th>", page)
        subj = R.subject_line(ctx)
        self.assertIn("no variant passes (no stress results)", subj)
        self.assertNotIn("robust cons", subj)


class TestSwpFixPass(unittest.TestCase):
    """Fix pass 10/16 (review of the swp build): net_cal only where it is not
    circular; events without swp counted; a failed event whose older file is
    still loaded named as such; the swp tables named and a rejected override
    flagged; the re-sim time budget."""

    def setUp(self):
        clear_data()
        self._model = os.path.join(DATA, "model", "sweep_hazard.json")

    def tearDown(self):
        if os.path.exists(self._model):
            os.remove(self._model)
        try:
            import temp_shadow_sim as sim
            sim._SWP_CACHE.clear()
        except Exception:
            pass

    def _days(self, seed=21, days=("2026-10-15", "2026-10-16", "2026-10-17"), **kw):
        # with stress: the ranking is on stress only (no swp fallback since
        # the review of 338b4a5)
        rng = random.Random(seed)
        kw.setdefault("stress", True)
        for day in days:
            write_day_swp(day, rng, **kw)

    def test_net_cal_only_for_variants_without_live_quotes(self):
        self._days()
        ctx = R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False, now=NOW)
        self.assertEqual(ctx["best"], "G_decidedonly")
        self.assertTrue(R.cal_ok(POLICY_LIVE, "G_decidedonly"))
        self.assertFalse(R.cal_ok(POLICY_LIVE, "D_size"))
        self.assertFalse(R.cal_ok(POLICY, "G_decidedonly"))         # live unknown -> not shown
        self.assertFalse(R.cal_ok(POLICY_LIVE, "Z_unknown"))
        page = R.render_html(ctx)
        top = ctx["rank"][0]
        self.assertEqual(top["name"], "G_decidedonly")
        self.assertAlmostEqual(top["cal"], ctx["win"].total("G_decidedonly|stress", "net_cal"))
        self.assertIn(f"expected (calibrated tails) {R.m_html(top['cal'])}", page)
        self.assertTrue(all(r["cal"] is None for r in ctx["rank"][1:]))   # live-quoting: none
        # the variants table and the cumulative table: G's figure, (circular) for the rest
        rows = R._variant_rows(ctx, ["opt", "cons", "swp"])
        g_row = [r for r in rows if "G_decidedonly" in str(r[0])][0]
        d_row = [r for r in rows if "D_size" in str(r[0])][0]
        self.assertNotIn(R.CIRCULAR, g_row)
        self.assertIn(R.CIRCULAR, d_row)
        cum = R._cumulative_html(ctx)
        self.assertEqual(cum.count(f">{R.CIRCULAR}<"), len(VARIANTS) - 1)     # table cells
        self.assertIn("those cells read", page)                     # the note says why
        txt = R.render_text(ctx)
        self.assertIn("expected at calibrated tails", txt)

    def test_live_quoting_best_shows_no_expectation(self):
        # the review's case: the swp headline is a live-quoting variant
        # (H_noguard_pull on 10/15): its net_cal is circular -> not shown
        self._days()
        pol_ = R.Policy(dict(DEFAULT_POLICY, live=True),
                        dict(VARIANTS, G_decidedonly={"decided": True, "q_live": 0,
                                                      "live": True}))
        ctx = R.build_context("2026-10-17", policy=pol_, with_health=False, now=NOW)
        self.assertEqual(ctx["best"], "G_decidedonly")
        page = R.render_html(ctx)
        self.assertNotIn("expected (calibrated tails) <span", page)
        self.assertIn("no calibrated-tails expectation (it quotes live strikes: circular)", page)
        self.assertIsNone(ctx["rank"][0]["cal"])
        txt = R.render_text(ctx)
        self.assertNotIn("expected at calibrated tails", txt)
        self.assertIn("no calibrated-tails expectation (it quotes live strikes: circular)", txt)
        self.assertEqual(R._cumulative_html(ctx).count(f">{R.CIRCULAR}<"), len(VARIANTS))

    def test_events_without_swp_are_counted(self):
        self._days(hours=range(6))
        d = os.path.join(DATA, "sim", "2026-10-17")
        e1, e2 = event_ticker("2026-10-17", 1), event_ticker("2026-10-17", 2)
        for ev, extra in ((e1, {"swp_error": "swp run failed: RuntimeError: boom\ntrace"}),
                          (e2, {"sim_version": "4"})):
            p = os.path.join(d, f"{ev}.json")
            doc = C.read_json(p)
            doc["variants"] = {k: v for k, v in doc["variants"].items() if not k.endswith("|swp")}
            doc.update(extra)
            C.write_json_atomic(p, doc)
        ctx = R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False, now=NOW)
        self.assertEqual((ctx["fill"], ctx["rules_fill"]), ("stress", "swp"))
        sm = ctx["swp_missing"]
        self.assertEqual((sm["total"], sm["events"]), (2, 18))
        self.assertEqual(sm["by_day"], {"2026-10-17": 2})
        why = dict(sm["day"])
        self.assertEqual(why[e1], "swp failed: swp run failed: RuntimeError: boom")
        self.assertTrue(why[e2].startswith("older sim file (SIM_VERSION 4)")
                        or why[e2] == "no swp in the sim file", why[e2])
        self.assertEqual(ctx["agg"]["G_decidedonly|swp"]["n_events"], 4)
        self.assertEqual(ctx["agg"]["G_decidedonly|cons"]["n_events"], 6)
        page = R.render_html(ctx)
        self.assertIn("2 of 18 event(s) in the window have no swp results", page)
        self.assertIn("RuntimeError: boom", page)
        self.assertIn("[2 events without swp]", R.subject_line(ctx))
        self.assertIn("2 of 18 event(s) in the window have no swp results", R.render_text(ctx))
        # all present: no banner, no flag
        clear_data()
        self._days(hours=range(6))
        ctx = R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False, now=NOW)
        self.assertEqual(ctx["swp_missing"]["total"], 0)
        self.assertNotIn("without swp", R.subject_line(ctx))
        self.assertNotIn("have no swp results", R.render_html(ctx))

    def test_failed_event_with_an_older_file_is_named(self):
        self._days(days=("2026-10-17",), hours=range(4))
        loaded = event_ticker("2026-10-17", 1)
        gone = event_ticker("2026-10-17", 9)
        ss = {"ok": 3, "pending": [], "deferred": [], "window": {},
              "errors": [(loaded, "RuntimeError: x"), (gone, "RuntimeError: y")]}
        ctx = R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False, now=NOW,
                              sim_summary=ss)
        page = R.render_html(ctx)
        self.assertIn("2 event(s) failed in the sim", page)
        self.assertIn("1 of them still show an older sim file in the figures below", page)
        self.assertNotIn("(left out of every figure below)", page)
        self.assertIn(f"1 still show an older sim file: {loaded}", R.render_text(ctx))
        ss["errors"] = [(gone, "RuntimeError: y")]
        page = R.render_html(R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False,
                                             now=NOW, sim_summary=ss))
        self.assertIn("(left out of every figure below)", page)
        ss["errors"] = [(loaded, "RuntimeError: x")]
        page = R.render_html(R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False,
                                             now=NOW, sim_summary=ss))
        self.assertIn("an older sim file of each is still loaded", page)

    def test_swp_tables_named_and_rejected_override_flagged(self):
        import temp_shadow_sim as sim
        repo = sim.swp_config()["ident"]
        rng = random.Random(4)
        write_day_swp("2026-10-17", rng, hours=range(3))
        d = os.path.join(DATA, "sim", "2026-10-17")
        for p in glob.glob(os.path.join(d, "*.json")):
            doc = C.read_json(p)
            doc["swp"] = dict(repo)
            C.write_json_atomic(p, doc)
        ctx = R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False, now=NOW)
        line = R.swp_tables_text(ctx)
        self.assertEqual(line, f"swp tables: hazard {repo['hazard_id']} (repo default), tails "
                               f"{repo['tails_id']} (repo default), {repo['draws']} draws.")
        self.assertIn(line, R.render_html(ctx))
        self.assertIn(line, R.render_text(ctx))
        self.assertEqual(R.swp_rejections(ctx), [])
        self.assertNotIn("swp table problem", R.subject_line(ctx))
        # the review's malformed override: rejected, the repo table used, flagged
        os.makedirs(os.path.dirname(self._model), exist_ok=True)
        with open(self._model, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "built": "x"}, f)
        sim._SWP_CACHE.clear()
        ctx = R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False, now=NOW)
        rej = R.swp_rejections(ctx)
        self.assertEqual(len(rej), 1)
        self.assertIn("no 'hazard' fit", rej[0])
        self.assertEqual(ctx["swp_current"]["hazard_id"], repo["hazard_id"])
        page = R.render_html(ctx)
        self.assertIn("swp table problem", page)
        self.assertIn("[swp table problem]", R.subject_line(ctx))
        self.assertIn("SWP TABLE PROBLEM", R.render_text(ctx))
        self.assertEqual(R.main(["--day", "2026-10-17", "--no-sim", "--out",
                                 os.path.join(DATA, "out", "rej.html")]), 1)
        # files simmed on two different tables are named as such
        p0 = sorted(glob.glob(os.path.join(d, "*.json")))[0]
        doc = C.read_json(p0)
        doc["swp"] = dict(repo, hazard_id="feedfacefeedface", hazard_kind="override",
                          hazard_src=os.path.join(DATA, "model", "sweep_hazard.json"))
        C.write_json_atomic(p0, doc)
        ctx = R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False, now=NOW)
        line = R.swp_tables_text(ctx)
        self.assertTrue(line.startswith("swp tables differ across the window: "), line)
        self.assertIn("hazard feedfacefeedface (model/ override)", line)

    def test_run_sim_time_budget(self):
        calls = []

        def run_day(d, *a, deadline=None, **k):
            calls.append((d, deadline))
            if deadline is not None:
                return [{"event": event_ticker(d, 1), "deferred": True, "settled": True},
                        {"event": event_ticker(d, 2), "variants": {}, "settled": True,
                         "swp_error": "swp run failed: X\n..."}]
            return [{"event": event_ticker(d, 3), "variants": {}, "settled": True}]
        fake_sim = types.SimpleNamespace(run_day=run_day, DEFAULT_POLICY=DEFAULT_POLICY,
                                         VARIANTS=VARIANTS)
        with StubModules(temp_shadow_sim=fake_sim):
            t0 = time.time()
            err, summary = R.run_sim("2026-10-17", budget_min=7.0)
        self.assertIsNone(err)
        # earlier days newest first (10/17: the test windows are the latest days)
        self.assertEqual([c[0] for c in calls], ["2026-10-17", "2026-10-16", "2026-10-15"])
        self.assertIsNone(calls[0][1])                     # the report day: no deadline
        for _, dl in calls[1:]:
            self.assertAlmostEqual(dl, t0 + 420.0, delta=5.0)
        w = summary["window"]
        self.assertEqual(w["deferred"], [event_ticker("2026-10-16", 1),
                                         event_ticker("2026-10-15", 1)])
        self.assertEqual(w["budget_min"], 7.0)
        self.assertEqual(len(w["swp_errors"]), 2)
        self.assertEqual(R.RESIM_BUDGET_MIN, 80.0)
        rng = random.Random(3)
        write_day_swp("2026-10-17", rng, hours=range(3))
        ctx = R.build_context("2026-10-17", policy=POLICY_LIVE, with_health=False, now=NOW,
                              sim_summary=summary)
        page = R.render_html(ctx)
        self.assertIn("2 earlier-day event(s) not re-simmed within the time budget", page)
        self.assertIn("(7 min)", page)
        self.assertIn("[2 re-sims deferred]", R.subject_line(ctx))
        self.assertIn("not re-simmed within the time budget", R.render_text(ctx))

    def test_exit_code_on_swp_failure_not_on_deferral(self):
        out = os.path.join(DATA, "out", "rc.html")

        def rc_with(res_day, res_win):
            fake = types.SimpleNamespace(
                run_day=lambda d, *a, deadline=None, **k: res_win if deadline else res_day,
                DEFAULT_POLICY=DEFAULT_POLICY, VARIANTS=VARIANTS)
            with StubModules(temp_shadow_sim=fake):
                return R.main(["--day", "2026-10-16", "--out", out])
        ok = [{"event": event_ticker("2026-10-16", 1), "variants": {}, "settled": True}]
        swp_bad = [{"event": event_ticker("2026-10-16", 2), "variants": {}, "settled": True,
                    "swp_error": "swp run failed: X"}]
        deferred = [{"event": event_ticker("2026-10-15", 3), "deferred": True, "settled": True}]
        self.assertEqual(rc_with(ok, ok), 0)
        self.assertEqual(rc_with(ok, deferred), 0)          # a deferral is the budget working
        self.assertEqual(rc_with(swp_bad, ok), 1)           # swp failed (opt / cons kept)
        self.assertEqual(rc_with(ok, swp_bad), 1)
        stress_bad = [{"event": event_ticker("2026-10-16", 2), "variants": {}, "settled": True,
                       "stress_error": "stress run failed: X"}]
        self.assertEqual(rc_with(stress_bad, ok), 1)        # stress failed (the rest kept)
        self.assertEqual(rc_with(ok, stress_bad), 1)


def _ev_rec(day, h, variants, **extra):
    rec = erec(event_ticker(day, h), variants)
    rec.update(pin_ident())
    rec.update(extra)
    return rec


def _win_from(nets_by_variant, days=("2026-10-16", "2026-10-17"), fill="stress"):
    """A two-day Window (the headline needs HEADLINE_MIN_DAYS judged days)
    whose i-th event-hour in settle order holds name|fill = nets[i]: the
    first half on days[0] from 00:00, the rest on days[1] from 00:00."""
    n = max(len(v) for v in nets_by_variant.values())
    half = (n + 1) // 2
    de = {d: [] for d in days}
    for i in range(n):
        d, h = (days[0], i) if i < half else (days[1], i - half)
        variants = {f"{name}|{fill}": srec(nets[i]) for name, nets in nets_by_variant.items()
                    if i < len(nets)}
        de[d].append(_ev_rec(d, h, variants))
    return R.Window(de)


class TestRobustRanking(unittest.TestCase):
    """Jack 2026-10-17, "1": the 10/17 email headlined "best I_pads_only net
    +$1,480/day on swp", a fluke of two upset hours. The headline now ranks
    variants on STRESS net over the cumulative window without each one's
    best and worst hour, and names the top one only when that robust total
    is > 0 and P(total > 0) >= 0.8; a single day's net ranks nothing."""

    def test_robust_total_ci_and_flags(self):
        steady = [5.0] * 20
        fluke = [-10.0] * 19 + [600.0]                    # one upset hour carries it
        win = _win_from({"Steady": steady, "Fluke": fluke})
        rows = R.rank_variants(win, FAV_POLICY)
        self.assertEqual([r["name"] for r in rows], ["Steady", "Fluke"])
        s, f = rows
        self.assertEqual((s["total"], s["robust"], s["hours"]), (100.0, 90.0, 20))
        self.assertEqual((s["p_pos"], s["noise"], s["passes"]), (1.0, False, True))
        self.assertEqual((s["ci_lo"], s["ci_hi"]), (100.0, 100.0))
        self.assertEqual(f["total"], 410.0)                # the plain total ranks it first ...
        self.assertEqual(f["robust"], 410.0 - 600.0 + 10.0)   # ... the robust total last
        self.assertEqual((f["best"], f["worst"]), (600.0, -10.0))
        self.assertTrue(f["noise"])                        # 600 + 10 > 410
        self.assertFalse(f["passes"])
        self.assertLess(f["ci_lo"], 0.0)
        self.assertLess(f["ci_lo"], f["ci_hi"])
        # reproducible: the bootstrap has a fixed seed
        self.assertEqual(R.rank_variants(win, FAV_POLICY), rows)
        self.assertEqual(R.boot_total([]), (None, None, None))
        self.assertIsNone(R.robust_total([3.0]))
        self.assertEqual(R.robust_total([3.0, -1.0]), 0.0)
        hl = R.headline_pick(rows)
        self.assertTrue(hl["pass"])
        self.assertIs(hl["pick"], s)
        self.assertEqual(R.headline_phrase(hl, subject=True),
                         "top Steady robust stress +$90 (P>0 1.00)")

    def test_no_variant_passes(self):
        # positive robust total but P(total > 0) < 0.8; and a negative one
        noisy = [40.0, -35.0, 30.0, -28.0, 25.0, -20.0, 22.0, -30.0, 12.0, 9.0]
        win = _win_from({"Noisy": noisy, "Loser": [-2.0] * 10})
        rows = R.rank_variants(win, FAV_POLICY)
        top = rows[0]
        self.assertEqual(top["name"], "Noisy")
        self.assertGreater(top["robust"], 0.0)
        self.assertLess(top["p_pos"], R.HEADLINE_P_MIN)
        hl = R.headline_pick(rows)
        self.assertFalse(hl["pass"])
        self.assertIsNone(hl["pick"])
        self.assertEqual(R.headline_phrase(hl, subject=True), "no variant passes")
        self.assertTrue(R.headline_phrase(hl).startswith(
            "no variant passes (top robust stress total, for information: Noisy +$"))

    def test_a_lower_rank_that_passes_is_named_as_such(self):
        hi_var = [60.0, -55.0, 50.0, -45.0, 52.0, -40.0, 45.0, -50.0, 48.0, -20.0]  # P < 0.8
        steady = [1.0] * 10
        win = _win_from({"Wild": hi_var, "Steady": steady})
        rows = R.rank_variants(win, FAV_POLICY)
        self.assertEqual(rows[0]["name"], "Wild")
        hl = R.headline_pick(rows)
        self.assertFalse(hl["pass"])
        self.assertEqual(hl["pick"]["name"], "Steady")
        self.assertIn("top robust Wild does not pass; Steady does", R.headline_phrase(hl, True))

    def test_headline_judges_each_variant_after_it_was_chosen(self):
        # review of 338b4a5: no variant is ranked on the hours it was chosen
        # from. FAV_night wins its in-sample 10/15-10/16 and loses on 10/17
        # (Test A's start); FAV_night_agree (chosen from 10/16, judged from
        # 10/18) has no judged hour yet; G_decided_only (its 180 s hold chosen
        # 10/16) from 10/17; G0_nohold from the window start
        clear_data()
        write_fav_days({"2026-10-15": 5.0, "2026-10-16": 5.0, "2026-10-17": -1.0},
                       agree={d: 2.0 for d in days_from("2026-10-15", 3)},
                       extra={"G_decided_only": {"2026-10-15": 9.0, "2026-10-16": 9.0,
                                                 "2026-10-17": 1.0},
                              "G0_nohold": 0.2})
        ctx = R.build_context("2026-10-17", policy=FAV_POLICY, with_health=False, now=NOW)
        rows = {r["name"]: r for r in ctx["rank"]}
        fn = rows["FAV_night"]
        self.assertEqual((fn["hours"], fn["total"], fn["in_hours"], fn["in_total"]),
                         (24, -24.0, 48, 240.0))
        self.assertFalse(fn["passes"])
        g = rows["G_decided_only"]
        self.assertEqual((g["hours"], g["total"], g["in_hours"], g["in_total"]),
                         (24, 24.0, 48, 432.0))
        g0 = rows["G0_nohold"]
        self.assertEqual((g0["hours"], g0["in_hours"], g0["judged_from"]), (72, 0, None))
        ag = rows["FAV_night_agree"]
        self.assertEqual((ag["judged"], ag["rank"], ag["in_hours"], ag["in_total"]),
                         (False, None, 72, 144.0))
        self.assertEqual(ctx["rank"][-1]["name"], "FAV_night_agree")   # after the ranked ones
        self.assertEqual([r["name"] for r in ctx["rank"] if r.get("judged")],
                         ["G_decided_only", "G0_nohold", "FAV_night"])
        hl = ctx["headline"]
        self.assertEqual(hl["top"]["name"], "G_decided_only")
        self.assertEqual(sorted(r["name"] for r in hl["in_sample"]),
                         ["FAV_night", "FAV_night_agree", "G_decided_only"])
        self.assertNotIn("FAV_night_agree", R.subject_line(ctx))
        A, B = C.parse_iso("2026-10-17T04:00:00Z"), C.parse_iso("2026-10-18T04:00:00Z")
        for name, want in (("FAV_night", A), ("FAV_only", A), ("FAV_night_q100", A),
                           ("FAV_night_agree", B), ("G_decided_only", A), ("G0_nohold", None),
                           ("I_pads_only", None), ("J_decided_fav100", None)):
            self.assertEqual(R.judged_from_ts(name), want, name)
        page = R.render_html(ctx)
        for needle in ("judged from", "10/17 00:00 ET", "10/18 00:00 ET", "window start",
                       "(no judged hour yet)", "in-sample hours (not ranked)", "48 hours, ",
                       "In-sample hours, before each variant&#x27;s first judged hour",
                       "FAV_night_agree: 72 hours before 10/18 00:00 ET, stress +$144",
                       "moving-block bootstrap of 3 consecutive event-hours"):
            self.assertIn(needle, page)
        txt = R.render_text(ctx)
        self.assertIn("not judged yet: FAV_night_agree (from 10/18 00:00 ET)", txt)
        self.assertIn("in-sample hours, before each variant's first judged hour", txt)

    def test_one_judged_day_is_never_named(self):
        # no single-day ranking: a variant whose judged hours sit on one ET
        # day clears robust > 0 and P >= 0.8 here, but is not named
        self.assertEqual(R.HEADLINE_MIN_DAYS, 2)
        evs = [_ev_rec("2026-10-17", h, {"OneNight|stress": srec(5.0 if h < 8 else 0.0)})
               for h in range(19)]
        rows = R.rank_variants(R.Window({"2026-10-17": evs}), FAV_POLICY)
        r = rows[0]
        self.assertEqual((r["days"], r["thin"], r["passes"]), (1, True, False))
        self.assertGreater(r["robust"], 0)
        self.assertGreaterEqual(r["p_pos"], R.HEADLINE_P_MIN)
        hl = R.headline_pick(rows)
        self.assertEqual((hl["top"]["name"], hl["pick"], hl["pass"]), ("OneNight", None, False))
        self.assertEqual(R.headline_phrase(hl, subject=True), "no variant passes")
        self.assertIn("1 judged day: the headline needs 2", R.headline_phrase(hl))
        # a second judged day makes it eligible
        two = R.Window({"2026-10-16": [_ev_rec("2026-10-16", 23,
                                               {"OneNight|stress": srec(5.0)})],
                        "2026-10-17": evs})
        r = R.rank_variants(two, FAV_POLICY)[0]
        self.assertEqual((r["days"], r["thin"], r["passes"]), (2, False, True))

    def test_block_bootstrap_for_clustered_losses(self):
        # one weather move fails three consecutive hours: blocks of 3 keep
        # them together, so the spread is wider than the hour bootstrap's
        nets = [6.0] * 30 + [-100.0] * 3 + [6.0] * 27
        lo1, hi1, p1 = R.boot_total(nets, block=1)
        lo3, hi3, p3 = R.boot_total(nets)
        self.assertEqual(R.BOOT_BLOCK, 3)
        self.assertGreater(hi3 - lo3, hi1 - lo1)
        self.assertNotEqual(p1, p3)
        self.assertEqual(R.boot_total(nets), (lo3, hi3, p3))          # fixed seed
        self.assertEqual(R.boot_total([5.0] * 7), (35.0, 35.0, 1.0))  # a partial last block
        self.assertEqual(R.boot_total([2.0, -1.0]), (1.0, 1.0, 1.0))  # fewer hours than a block

    def test_noise_flag_in_the_subject(self):
        # the named variant is noise-dominated: the subject says so
        clear_data()
        for day in days_from("2026-10-15", 3):
            os.makedirs(os.path.join(DATA, "sim", day), exist_ok=True)
            for h in range(24):
                x = 1.0
                if (day, h) == ("2026-10-16", 21):
                    x = 120.0
                if (day, h) == ("2026-10-16", 22):
                    x = -60.0
                C.write_json_atomic(os.path.join(DATA, "sim", day, f"{event_ticker(day, h)}.json"),
                                    _ev_rec(day, h, {"G0_nohold|stress": srec(x),
                                                     "G0_nohold|swp": srec(x)}))
        ctx = R.build_context("2026-10-17", policy=FAV_POLICY, with_health=False, now=NOW)
        r = ctx["rank"][0]
        self.assertEqual((r["name"], r["total"], r["robust"]), ("G0_nohold", 130.0, 70.0))
        self.assertTrue(r["noise"] and r["passes"])
        subj = R.subject_line(ctx)
        self.assertTrue(subj.startswith("Temp shadow day 3 (2026-10-17): top G0_nohold robust "
                                        f"stress +$70 (P>0 {r['p_pos']:.2f}) [noise-dominated]"),
                        subj)
        self.assertIn("noise-dominated (|best hour| + |worst hour| &gt; |total|)",
                      R.render_html(ctx))

    def test_no_stress_results_never_fall_back(self):
        # review of 338b4a5: stress failed on every event (swp kept): the
        # headline never falls back to swp
        clear_data()
        for day in days_from("2026-10-15", 3):
            os.makedirs(os.path.join(DATA, "sim", day), exist_ok=True)
            for h in range(24):
                x = 12.0 if h != 3 else 5.0
                C.write_json_atomic(
                    os.path.join(DATA, "sim", day, f"{event_ticker(day, h)}.json"),
                    _ev_rec(day, h, {"I_pads_only|swp": srec(x), "I_pads_only|cons": vrec(x),
                                     "FAV_night|swp": srec(-1.0)},
                            stress_error="stress run failed: KeyError: 'x'"))
        ctx = R.build_context("2026-10-17", policy=FAV_POLICY, with_health=False, now=NOW)
        self.assertEqual((ctx["fill"], ctx["rules_fill"], ctx["rank"]), ("stress", "swp", []))
        self.assertIsNone(ctx["best"])
        self.assertEqual(ctx["stress_missing"]["total"], 72)
        self.assertEqual(R.headline_phrase(ctx["headline"]),
                         "no variant passes (no stress results)")
        subj = R.subject_line(ctx)
        self.assertTrue(subj.startswith("Temp shadow day 3 (2026-10-17): no variant passes (no "
                                        "stress results); Test A in progress 1/5"), subj)
        self.assertIn("[72 events without stress]", subj)
        a = ctx["tests"][0]
        self.assertTrue(a["missing"][0][1].startswith("stress failed: stress run failed"))
        page = R.render_html(ctx)
        self.assertIn("No variant passes (no stress results)", page)
        self.assertIn("No stress results to rank in the window yet.", page)
        self.assertNotIn("Headline variant", page)
        self.assertIn("Headline: no variant passes (no stress results).", R.render_text(ctx))

    def test_report_day_net_never_ranks_the_headline(self):
        # the 10/17 case: on the report day "Fluke" wins big (an upset hour);
        # over the window "Steady" earns every hour. The headline, the subject
        # and the variants table follow the robust ranking.
        clear_data()
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            os.makedirs(os.path.join(DATA, "sim", day), exist_ok=True)
            for h in range(24):
                fl = 1480.0 if (day == "2026-10-17" and h == 4) else -3.0
                variants = {"Steady|stress": srec(2.0), "Fluke|stress": srec(fl),
                            "Steady|swp": srec(2.0), "Fluke|swp": srec(fl)}
                C.write_json_atomic(os.path.join(DATA, "sim", day,
                                                 f"{event_ticker(day, h)}.json"),
                                    _ev_rec(day, h, variants))
        ctx = R.build_context("2026-10-17", policy=FAV_POLICY, with_health=False, now=NOW)
        self.assertGreater(ctx["agg"]["Fluke|stress"]["net"], ctx["agg"]["Steady|stress"]["net"])
        self.assertEqual(ctx["best"], "Steady")
        self.assertEqual([r["name"] for r in ctx["rank"]], ["Steady", "Fluke"])
        subj = R.subject_line(ctx)
        self.assertTrue(subj.startswith("Temp shadow day 3 (2026-10-17): top Steady robust "
                                        "stress +$140 (P>0 1.00)"), subj)
        self.assertNotIn("Fluke", subj)
        page = R.render_html(ctx)
        rows = R._variant_rows(ctx, ["swp", "stress"])
        self.assertIn("Steady", str(rows[0][0]))           # the table in the ranking's order
        self.assertIn("A single day&#x27;s net ranks nothing", page)
        fl = [r for r in ctx["rank"] if r["name"] == "Fluke"][0]
        self.assertTrue(fl["noise"])
        self.assertIn("NOISE", page)


def _fav_hits(rows):
    return [[f"T{i}", b, n, u, sl, 100.0, 0.0] for i, (b, n, u, sl) in enumerate(rows)]


class TestPreregisteredTests(unittest.TestCase):
    """TEMP_SHADOW.md "Pre-registered tests" (eff9cd3): Test A (FAV_night, ET
    10/17-10/21: total > 0, >= 4 of 5 days > 0, calibrated tails > 0) and
    Test B (FAV_night_agree, 10/18-10/22: the same plus the total without
    its best and worst hour > 0); the recent-regime figure beside the
    table-based one. Review of 338b4a5 (Jack 2026-10-17, "1"): Test A's
    start is one constant and the hours known at registration are labelled,
    with the verdict shown both ways; a test never completes with an expected
    hour missing (INCOMPLETE) or before its last hour is due; the registered
    rule and model are pinned (INVALID otherwise); a completed verdict is
    frozen."""

    def setUp(self):
        clear_data()

    def _ctx(self, day, now=NOW, **kw):
        return R.build_context(day, policy=FAV_POLICY, with_health=False, now=now, **kw)

    def test_definitions_are_the_preregistered_ones(self):
        a, b = R.PREREG_TESTS
        self.assertEqual((a["id"], a["variant"], a["start"], a["days"]),
                         ("A", "FAV_night", "2026-10-17T00:00:00-04:00", 5))
        self.assertEqual(R.test_days(a), days_from("2026-10-17", 5))
        self.assertEqual(a["criteria"], ("total", "days", "cal"))
        self.assertIn("robust", a["info"])                 # reported for information
        self.assertEqual((b["id"], b["variant"], b["start"], b["days"]),
                         ("B", "FAV_night_agree", "2026-10-18T00:00:00-04:00", 5))
        self.assertEqual(R.test_days(b), days_from("2026-10-18", 5))
        self.assertEqual(b["criteria"], ("total", "days", "robust", "cal"))
        self.assertIn("recent", b["info"])
        self.assertEqual(R.PREREG_DAYS_POS, 4)
        self.assertEqual(R.PREREG_REGISTERED_AT, "2026-10-17T14:57:35-04:00")
        lo, hi = R.test_bounds(a)
        self.assertEqual((C.iso(lo), C.iso(hi)), ("2026-10-17T04:00:00Z", "2026-10-22T04:00:00Z"))
        # the registered rules: Test B = Test A's rule plus the agreement gate
        self.assertEqual(b["overrides"], dict(a["overrides"], fav_market_min_p=0.95))
        self.assertEqual(b["policy"], dict(a["policy"], fav_market_min_p=0.95))
        self.assertIsNone(a["policy"]["fav_market_min_p"])
        self.assertEqual((a["policy"]["q_fav"], a["policy"]["fav_min_p"], a["policy"]["live"]),
                         (300, 0.97, False))
        self.assertEqual(R.PREREG_PIN_MODEL["sim_version"], "8")
        # each test carries its model pin (the swp / stress identity, the fair model)
        self.assertIs(a["model"], R.PREREG_PIN_MODEL)
        self.assertIs(b["model"], R.PREREG_PIN_MODEL)
        self.assertEqual(sorted(R.PREREG_PIN_MODEL),
                         ["fair_model_id", "sim_version", "stress", "swp"])

    def test_status_progression_and_subject(self):
        write_fav_days({d: -1.0 for d in days_from("2026-10-15", 3)})
        before = self._ctx("2026-10-16")
        self.assertEqual([t["status"] for t in before["tests"]], ["not started"] * 2)
        self.assertEqual([t["text"] for t in before["tests"]], ["starts 10/17", "starts 10/18"])
        ctx = self._ctx("2026-10-17")
        a, b = ctx["tests"]
        self.assertEqual((a["status"], a["text"]), ("in progress", "in progress (1/5 days)"))
        self.assertEqual(b["text"], "starts 10/18")
        self.assertEqual(a["total"], -24.0)
        # 26OCT1700-1714 settled before the registration commit (14:57:35 ET)
        self.assertEqual([e for e, _n in a["known"]],
                         [event_ticker("2026-10-17", h) for h in range(15)])
        self.assertEqual((a["alt"]["total"], a["alt"]["text"]), (-9.0, "in progress (1/5 days)"))
        self.assertEqual(a["alt"]["known"], [])
        self.assertIsNone(b["alt"])
        self.assertEqual((ctx["prereg_verdict"], ctx["prereg_verdict_alt"]),
                         ("in progress", "in progress"))
        subj = R.subject_line(ctx)
        # the task's example: "...: no variant passes; Test A in progress 1/5
        # (stress -$X), Test B starts 10/18" -- with the "after registration"
        # figure while the registered window holds known hours
        self.assertTrue(subj.startswith("Temp shadow day 3 (2026-10-17): no variant passes; "
                                        "Test A in progress 1/5 (stress -$24; after "
                                        "registration -$9), Test B starts 10/18"), subj)
        page = R.render_html(ctx)
        for needle in ("Pre-registered verdict: ", "in progress (1/5 days)",
                       "Test A -- FAV_night (rule 3): ET days 2026-10-17 .. 2026-10-21 (from "
                       "10/17 00:00 ET)", "As registered: ",
                       "Excluding the 15 hours known at registration (settled before 2026-10-17 "
                       "14:57:35 ET (eff9cd3))", "known at registration",
                       "net after registration", "26OCT1700 -$1.00",
                       "Test B -- FAV_night_agree", "the window&#x27;s total stress net &gt; 0",
                       "not met", "so far", "RECENT REGIME", "table-based",
                       "excluding known hours"):
            self.assertIn(needle, page)
        txt = R.render_text(ctx)
        self.assertIn("Test A (FAV_night, ET 2026-10-17 .. 2026-10-21): as registered in "
                      "progress (1/5 days); excluding the 15 hours known at registration", txt)
        self.assertIn("Live reconciliation (rule 4) of the agreement gate: ", txt)

    def test_pass_fail_on_each_criterion(self):
        window = days_from("2026-10-17", 6)
        # A: +24 a day on every day -> PASS; B identical -> PASS
        write_fav_days({d: 1.0 for d in window})
        ctx = self._ctx("2026-10-22")
        a, b = ctx["tests"]
        self.assertEqual((a["status"], b["status"]), ("PASS", "PASS"))
        self.assertEqual((a["pos_days"], a["days_done"], a["hours"]), (5, 5, 120))
        self.assertEqual((a["alt"]["status"], a["alt"]["hours"]), ("PASS", 105))
        self.assertEqual(ctx["prereg_verdict"], "pilot eligible: FAV_night, FAV_night_agree")
        self.assertEqual(ctx["prereg_verdict_alt"], "pilot eligible: FAV_night, FAV_night_agree")
        # total > 0 but only 3 of 5 positive days -> FAIL (A and B)
        clear_data()
        nets = {d: 10.0 if d in ("2026-10-17", "2026-10-20", "2026-10-21", "2026-10-22") else -0.5
                for d in window}
        write_fav_days(nets)
        a, b = self._ctx("2026-10-22")["tests"]
        self.assertGreater(a["total"], 0)
        self.assertEqual(a["pos_days"], 3)
        self.assertEqual(a["status"], "FAIL")
        self.assertFalse(a["crit"]["days"]["met"])
        self.assertEqual(b["pos_days"], 3)
        self.assertEqual(b["status"], "FAIL")
        # B only: the robust criterion -- one huge hour carries the window
        clear_data()
        agree = {d: ([-20.0] * 23 + [500.0] if d == "2026-10-19" else [0.3] * 24)
                 for d in window}
        write_fav_days({d: 1.0 for d in window}, agree=agree)
        a, b = self._ctx("2026-10-22")["tests"]
        self.assertEqual(a["status"], "PASS")
        self.assertGreater(b["total"], 0)
        self.assertEqual(b["pos_days"], 5)
        self.assertLess(b["robust"], 0)                    # without its +500 hour
        self.assertEqual(b["status"], "FAIL")
        self.assertFalse(b["crit"]["robust"]["met"])
        # the calibrated-tails criterion: net_cal <= 0 fails even when the
        # realized nets pass
        clear_data()
        for d in window:
            os.makedirs(os.path.join(DATA, "sim", d), exist_ok=True)
            for h in range(24):
                variants = {"FAV_night|stress": srec(1.0, cal=-0.5),
                            "FAV_night_agree|stress": srec(1.0, cal=0.5)}
                C.write_json_atomic(os.path.join(DATA, "sim", d, f"{event_ticker(d, h)}.json"),
                                    _ev_rec(d, h, variants))
        ctx = self._ctx("2026-10-22")
        a, b = ctx["tests"]
        self.assertEqual(a["cal"], -0.5 * 24 * 5)
        self.assertEqual((a["status"], b["status"]), ("FAIL", "PASS"))
        self.assertEqual(ctx["prereg_verdict"], "pilot eligible: FAV_night_agree")

    def test_known_hours_both_verdicts(self):
        # the hours settled before the registration carry Test A: +40 an hour
        # in 26OCT1700-1714; every later day +0.5 (its 00:00 hour +2.8, the
        # rest -0.1) -> PASS as registered, FAIL without the known hours
        window = days_from("2026-10-17", 6)
        later = [2.8] + [-0.1] * 23
        write_fav_days({d: ([40.0] * 15 + [-1.0] * 9 if d == "2026-10-17" else later)
                        for d in window})
        ctx = self._ctx("2026-10-22")
        a, b = ctx["tests"]
        self.assertEqual(a["status"], "PASS")
        self.assertAlmostEqual(a["total"], 591.0 + 4 * 0.5)
        self.assertEqual(len(a["known"]), 15)
        self.assertEqual(a["alt"]["status"], "FAIL")
        self.assertAlmostEqual(a["alt"]["total"], -9.0 + 4 * 0.5)
        self.assertEqual(a["rows"][0]["known"], 15)
        self.assertAlmostEqual(a["rows"][0]["known_net"], 600.0)
        self.assertEqual(b["status"], "FAIL")              # robust: without its 2.8 hours
        self.assertEqual(ctx["prereg_verdict"], "pilot eligible: FAV_night")
        self.assertEqual(ctx["prereg_verdict_alt"],
                         "NO-GO (no variant passes the pre-registered tests)")
        w = ctx["week"]
        self.assertEqual(w["verdict_after_registration"],
                         "NO-GO (no variant passes the pre-registered tests)")
        subj = R.subject_line(ctx)
        self.assertIn("Test A PASS (stress +$593; after registration FAIL -$7)", subj)
        self.assertTrue(subj.endswith("-- week: pilot eligible: FAV_night (excluding known "
                                      "hours: NO-GO (no variant passes the pre-registered "
                                      "tests))"), subj)
        page = R.render_html(ctx)
        for needle in ("As registered: <b", "Excluding the 15 hours known at registration",
                       "15 hours, ", "26OCT1714 +$40.00",
                       "excluding the hours known at registration: <b"):
            self.assertIn(needle, page)

    def test_missing_hours_make_it_incomplete_never_pass(self):
        # 10/21 21:00-23:00 ET have no sim file (one raised, two never
        # recorded); 10/19 05:00 has a file without its stress result. The
        # criteria pass on the hours that are in -- the test still never passes
        window = days_from("2026-10-17", 6)
        write_fav_days({d: 1.0 for d in window}, missing=(event_ticker("2026-10-19", 5),))
        gone = [event_ticker("2026-10-21", h) for h in (21, 22, 23)]
        for ev in gone:
            os.remove(os.path.join(DATA, "sim", "2026-10-21", f"{ev}.json"))
        summary = {"ok": 0, "pending": [], "errors": [], "deferred": [],
                   "window": {"errors": [(gone[1], "ValueError: boom")], "pending": [],
                              "deferred": []}}
        ctx = self._ctx("2026-10-22", sim_summary=summary)
        a, b = ctx["tests"]
        self.assertEqual((a["status"], b["status"]), ("INCOMPLETE", "INCOMPLETE"))
        self.assertTrue(all(a["crit"][c]["met"] for c in a["criteria"]))
        miss = dict(a["missing"])
        self.assertEqual(sorted(miss), sorted([event_ticker("2026-10-19", 5)] + gone))
        self.assertEqual(miss[gone[1]], "the sim raised: ValueError: boom")
        self.assertEqual(miss[gone[0]], "never seen: the collector was not recording then")
        self.assertEqual(miss[event_ticker("2026-10-19", 5)],
                         "its sim file has no stress result for FAV_night (SIM_VERSION 8)")
        self.assertEqual(a["text"], "INCOMPLETE (4 expected hour(s) without a stress result)")
        self.assertEqual(ctx["prereg_verdict"], "undecided (Test A INCOMPLETE; Test B INCOMPLETE)")
        subj = R.subject_line(ctx)
        self.assertIn("Test A INCOMPLETE (4 hours missing; stress +$116; after registration "
                      "INCOMPLETE +$101)", subj)
        page = R.render_html(ctx)
        self.assertIn("expected hour without a stress result", page)
        self.assertIn(gone[2], page)
        self.assertIn("1 expected hour(s) without stress", page)     # the 10/19 row
        self.assertIn("3 expected hour(s) without stress", page)     # the 10/21 row
        # before the window is over the same gap only says "so far"
        a20 = self._ctx("2026-10-20")["tests"][0]
        self.assertEqual(a20["status"], "in progress")
        self.assertEqual(a20["text"], "in progress (4/5 days; 1 expected hour(s) without a "
                                      "stress result so far)")
        # a settled file without a sim file names the re-sim
        C.write_json_atomic(C.settled_file(gone[0]), {"event_ticker": gone[0]})
        a = self._ctx("2026-10-22")["tests"][0]
        self.assertEqual(dict(a["missing"])[gone[0]], "settled, but no sim file (re-sim the day)")

    def test_never_complete_before_the_last_hour_is_due(self):
        write_fav_days({d: 1.0 for d in days_from("2026-10-17", 5)})
        last = C.parse_event(event_ticker("2026-10-21", 23))[1]
        early = self._ctx("2026-10-21", now=last + R.SETTLE_LAG_S - 60)["tests"][0]
        self.assertEqual((early["status"], early["text"]), ("in progress", "in progress (5/5 days)"))
        self.assertEqual(early["hours"], 120)             # every hour simmed, still running
        done = self._ctx("2026-10-21", now=last + R.SETTLE_LAG_S)["tests"][0]
        self.assertEqual(done["status"], "PASS")
        # an hour not due yet and not simmed keeps it running too
        os.remove(os.path.join(DATA, "sim", "2026-10-21", f"{event_ticker('2026-10-21', 23)}.json"))
        early = self._ctx("2026-10-21", now=last + R.SETTLE_LAG_S - 60)["tests"][0]
        self.assertEqual((early["status"], early["rows"][-1]["later"]), ("in progress", 1))

    def test_hours_kalshi_never_listed_are_not_expected(self):
        window = days_from("2026-10-17", 6)
        write_fav_days({d: 1.0 for d in window})
        ev = event_ticker("2026-10-20", 5)
        os.remove(os.path.join(DATA, "sim", "2026-10-20", f"{ev}.json"))
        S = C.parse_event(ev)[1]
        # the recording began before the windows (recording_start)
        t0 = C.parse_iso("2026-10-14T12:00:00Z")
        C.append_jsonl(C.raw_file("meta", t0, create=True),
                       {"rx": t0, "kind": "poll", "data": {"n": 12, "city": "miami"}})
        # the collector was recording across the hour (a station poll every
        # minute, market discovery writing rows for the hours around it) and
        # never saw this one listed
        for t in range(int(S) - 4 * 3600, int(S) + 2 * 3600, 60):
            C.append_jsonl(C.raw_file("meta", t, create=True),
                           {"rx": float(t), "kind": "poll", "data": {"n": 12, "city": "miami"}})
        for h, t in ((4, S - 3000.0), (6, S + 600.0)):
            C.append_jsonl(C.raw_file("meta", t, create=True),
                           {"rx": t, "kind": "markets",
                            "data": {"event_ticker": event_ticker("2026-10-20", h),
                                     "markets": []}})
        ctx = self._ctx("2026-10-22")
        a, b = ctx["tests"]
        self.assertEqual((a["not_listed"], b["not_listed"]), ([ev], [ev]))
        self.assertEqual((a["missing"], a["status"]), ([], "PASS"))
        self.assertEqual(a["hours"], 119)
        self.assertIn("Not listed by Kalshi", R.render_html(ctx))
        # unknown (the collector was not recording then): expected, missing
        clear_data()
        write_fav_days({d: 1.0 for d in window})
        os.remove(os.path.join(DATA, "sim", "2026-10-20", f"{ev}.json"))
        a = self._ctx("2026-10-22")["tests"][0]
        self.assertEqual((a["status"], a["not_listed"]), ("INCOMPLETE", []))

    def test_missing_reasons_and_listing(self):
        ev = event_ticker("2026-10-19", 4)
        S = C.parse_event(ev)[1]
        none = {"seen": set(), "mk_rx": [], "poll_min": set()}
        rec = {"seen": set(), "mk_rx": [S - 3000.0],
               "poll_min": {int(t // 60) for t in range(int(S) - 4200, int(S), 60)}}
        self.assertEqual(R._missing_reason(ev, none, {"pending": [ev]}),
                         "unsettled when the sim ran (the settlement pull was incomplete)")
        self.assertEqual(R._missing_reason(ev, none, {"window": {"deferred": [ev]}}),
                         "its re-sim was deferred (the time budget)")
        self.assertEqual(R._missing_reason(ev, dict(none, seen={ev}), {}),
                         "listed, but no settled file (the settlement pull failed?)")
        self.assertEqual(R._missing_reason(ev, none, {}),
                         "never seen: the collector was not recording then")
        self.assertEqual(R._missing_reason(ev, dict(rec, mk_rx=[]), {}),
                         "never seen, with no market discovery rows around it: whether Kalshi "
                         "listed it is unknown")
        self.assertIs(R.listed_by_kalshi(ev, rec), False)              # recorded, never seen
        self.assertIsNone(R.listed_by_kalshi(ev, dict(rec, mk_rx=[])))  # discovery silent
        self.assertIsNone(R.listed_by_kalshi(ev, none))                 # not recording
        self.assertIs(R.listed_by_kalshi(ev, dict(none, seen={ev})), True)
        C.write_json_atomic(C.settled_file(ev), {"event_ticker": ev})
        self.assertIs(R.listed_by_kalshi(ev, rec), True)               # a settled file

    def test_a_changed_rule_or_model_is_invalid(self):
        window = days_from("2026-10-17", 6)

        def edited(day, h):
            # from 10/20: FAV_night_agree's gate edited to 0.5 and another
            # stress model version in every file
            if day < "2026-10-20":
                return {}
            ident = pin_ident()
            ident["policies"]["FAV_night_agree"]["fav_market_min_p"] = 0.5
            ident["policy_full"]["FAV_night_agree"]["fav_market_min_p"] = 0.5
            ident["stress"]["stress_version"] = "99"
            return ident
        write_fav_days({d: 1.0 for d in window}, ident=edited)
        ctx = self._ctx("2026-10-22")
        a, b = ctx["tests"]
        self.assertEqual((a["status"], b["status"]), ("INVALID", "INVALID"))
        f = {d["field"]: d for d in b["pin_diffs"]}
        self.assertEqual((f["overrides.fav_market_min_p"]["pinned"],
                          f["overrides.fav_market_min_p"]["found"]), (0.95, 0.5))
        self.assertEqual(f["policy.fav_market_min_p"]["found"], 0.5)
        self.assertEqual((f["stress.stress_version"]["pinned"], f["stress.stress_version"]["found"],
                          f["stress.stress_version"]["files"], f["stress.stress_version"]["first"]),
                         ("1", "99", 72, event_ticker("2026-10-20", 0)))
        self.assertEqual([d["field"] for d in a["pin_diffs"]], ["stress.stress_version"])
        self.assertEqual(a["text"], "INVALID (rule or model changed)")
        self.assertEqual(ctx["prereg_verdict"], "undecided (Test A INVALID; Test B INVALID)")
        subj = R.subject_line(ctx)
        self.assertIn("Test A INVALID (rule or model changed), Test B INVALID (rule or model "
                      "changed)", subj)
        page = R.render_html(ctx)
        for needle in ("overrides.fav_market_min_p", "stress.stress_version", "window files",
                       "the test is INVALID and never passes"):
            self.assertIn(needle, page)
        self.assertIn("INVALID (rule or model changed): overrides.fav_market_min_p registered "
                      "0.95, found 0.5", R.render_text(ctx))
        # a fair-model refit inside the window: the same, on one file
        clear_data()
        write_fav_days({d: 1.0 for d in window},
                       ident=lambda d, h: ({"fair_model": {"id": "0" * 16, "source": "x.json"}}
                                           if (d, h) == ("2026-10-19", 3) else {}))
        a, b = self._ctx("2026-10-22")["tests"]
        self.assertEqual((a["status"], b["status"]), ("INVALID", "INVALID"))
        self.assertEqual([(d["field"], d["files"]) for d in a["pin_diffs"]], [("fair_model.id", 1)])
        # a file without the identity (no policies / policy_full): not the
        # registered rule as far as anyone can tell -> INVALID
        clear_data()
        write_fav_days({d: 1.0 for d in window},
                       ident=lambda d, h: ({"policies": {}, "policy_full": {}}
                                           if (d, h) == ("2026-10-18", 2) else {}))
        a = self._ctx("2026-10-22")["tests"][0]
        self.assertEqual(a["status"], "INVALID")
        self.assertEqual(sorted(d["field"] for d in a["pin_diffs"]), ["overrides", "policy"])

    def test_older_sim_files_wait_for_their_resim(self):
        window = days_from("2026-10-17", 6)
        write_fav_days({d: 1.0 for d in window},
                       ident=lambda d, h: {"sim_version": "7"} if d == "2026-10-18" else {})
        a, b = self._ctx("2026-10-22")["tests"]
        self.assertEqual((a["status"], len(a["missing"]), a["pin_diffs"]), ("INCOMPLETE", 24, []))
        self.assertEqual(a["missing"][0][1],
                         "older sim file (SIM_VERSION 7; the tests read 8): re-sim the day")
        self.assertEqual(a["stale"][0], event_ticker("2026-10-18", 0))
        # a file of the CURRENT code's version that is not the pinned one:
        # the model changed (here the pin says 9, the code and the files 8)
        with mock.patch.dict(R.PREREG_PIN_MODEL, sim_version="9"):
            a = self._ctx("2026-10-22")["tests"][0]
        self.assertEqual(a["status"], "INVALID")
        self.assertIn(("sim_version", "9", "8"),
                      [(d["field"], d["pinned"], d["found"]) for d in a["pin_diffs"]])

    def test_a_completed_verdict_is_frozen(self):
        window = days_from("2026-10-17", 6)
        write_fav_days({d: 1.0 for d in window})
        self._ctx("2026-10-22")                            # freeze=False writes nothing
        self.assertFalse(os.path.exists(R.frozen_path("A")))
        ctx = self._ctx("2026-10-22", freeze=True)
        self.assertTrue(os.path.exists(R.frozen_path("A")))
        self.assertTrue(os.path.exists(R.frozen_path("B")))
        doc = C.read_json(R.frozen_path("A"))
        self.assertEqual((doc["verdict"]["status"], doc["after_registration"]["status"]),
                         ("PASS", "PASS"))
        self.assertEqual(doc["definition"], R.test_definition(R.PREREG_TESTS[0]))
        self.assertEqual((len(doc["verdict"]["hours"]), doc["verdict"]["total"]), (120, 120.0))
        self.assertEqual((doc["frozen_at"], doc["report_day"]), ("2026-10-30T12:00:00Z",
                                                                  "2026-10-22"))
        self.assertIn("Verdict frozen now", R.render_html(ctx))
        # later runs show the frozen verdict
        a = self._ctx("2026-10-22")["tests"][0]
        self.assertEqual((a["status"], a["text"], a["frozen_diffs"]),
                         ("PASS", "PASS (frozen 2026-10-30)", []))
        # a window file changed after the freeze: the frozen verdict stands, flagged
        p = os.path.join(DATA, "sim", "2026-10-19", f"{event_ticker('2026-10-19', 3)}.json")
        rec = C.read_json(p)
        rec["variants"]["FAV_night|stress"]["net"] = -500.0
        C.write_json_atomic(p, rec)
        ctx = self._ctx("2026-10-22", freeze=True)
        a = ctx["tests"][0]
        self.assertEqual((a["status"], a["recomputed"]["status"]), ("PASS", "FAIL"))
        self.assertIn("total", a["frozen_diffs"])
        self.assertEqual(C.read_json(R.frozen_path("A"))["verdict"]["status"], "PASS")
        self.assertIn("Test A PASS (stress +$120; after registration PASS +$105) [frozen "
                      "verdict; a recompute differs]", R.subject_line(ctx))
        self.assertIn("A RECOMPUTE TODAY DIFFERS in", R.render_html(ctx))
        # another definition (Test A's start moved): the frozen file is not used
        spec = dict(R.PREREG_TESTS[0], start="2026-10-18T00:00:00-04:00")
        with mock.patch.object(R, "PREREG_TESTS", (spec, R.PREREG_TESTS[1])):
            a = self._ctx("2026-10-22")["tests"][0]
        self.assertIsNone(a["frozen"])
        self.assertEqual(a["frozen_other"]["status"], "PASS")

    def test_test_a_start_is_one_constant(self):
        # Jack's pending decision: the stricter window is the one line
        # "start": "2026-10-18T00:00:00-04:00"
        window = days_from("2026-10-17", 6)
        write_fav_days({d: 1.0 for d in window})
        spec = dict(R.PREREG_TESTS[0], start="2026-10-18T00:00:00-04:00")
        with mock.patch.object(R, "PREREG_TESTS", (spec, R.PREREG_TESTS[1])):
            self.assertEqual(R.test_days(spec), days_from("2026-10-18", 5))
            self.assertEqual(R.judged_from_ts("FAV_only"), C.parse_iso("2026-10-18T04:00:00Z"))
            ctx = self._ctx("2026-10-22")
        a = ctx["tests"][0]
        self.assertEqual((a["first_day"], a["last_day"], a["status"]),
                         ("2026-10-18", "2026-10-22", "PASS"))
        self.assertEqual((a["alt"], a["known"], ctx["prereg_verdict_alt"]), (None, [], None))
        self.assertNotIn("after registration", R.subject_line(ctx))

    def test_recent_regime_figure(self):
        # the recorded period's favorite-bid hits: >0.995 lost 2 of 40 (5%),
        # 0.97-0.99 lost 1 of 20 (5%)
        hits = _fav_hits([(">0.995", 20, 1, 0.02), (">0.995", 20, 1, 0.02),
                          ("0.97-0.99", 20, 1, 0.4)])
        ff = {">0.995": {"contracts": 300.0, "cost": 294.0, "pnl_cal": 5.7},
              "0.97-0.99": {"contracts": 100.0, "cost": 95.0, "pnl_cal": 3.0}}
        d = "2026-10-17"
        os.makedirs(os.path.join(DATA, "sim", d), exist_ok=True)
        for h in range(2):
            variants = {"FAV_night|stress": srec(1.0, cal=10.0, fav_fills=ff),
                        "FAV_night_agree|stress": srec(1.0, cal=10.0)}
            C.write_json_atomic(os.path.join(DATA, "sim", d, f"{event_ticker(d, h)}.json"),
                                _ev_rec(d, h, variants, fav_hits=hits if h == 0 else []))
        ctx = self._ctx(d)
        rec = ctx["recent"]
        self.assertEqual(rec["hours"], 2)
        self.assertAlmostEqual(rec["buckets"][">0.995"]["rate"], 0.05)
        self.assertAlmostEqual(rec["buckets"]["0.97-0.99"]["rate"], 0.05)
        self.assertAlmostEqual(rec["buckets"][">0.995"]["model"], 0.04 / 40)
        a = ctx["tests"][0]
        # per hour: 10 - 5.7 - 3.0 + 300 x 0.95 - 294 + 100 x 0.95 - 95 = -7.7
        self.assertAlmostEqual(a["recent"], 2 * (10.0 - 5.7 - 3.0 + 285.0 - 294.0 + 95.0 - 95.0))
        self.assertAlmostEqual(a["cal"], 20.0)
        page = R.render_html(ctx)
        self.assertIn("Recent regime (the tests&#x27; second calibrated-tails figure)", page)
        self.assertIn("&gt;0.995: 5.0%", page)
        # without fav_fills (older sim files) the recent figure is n/a
        self.assertIsNone(R.recent_cal([{"net_cal": 1.0}], rec))
        self.assertEqual(R.recent_cal([srec(1.0, cal=2.0)], rec), 2.0)   # no favorite fills

    def test_agreement_gate_share_for_the_live_reconciliation(self):
        window = days_from("2026-10-17", 3)
        write_fav_days({d: 1.0 for d in window},
                       ident=lambda d, h: {"fav_gate": {"FAV_night_agree": {
                           "model_secs": 1000, "gate_secs": 600}}})
        ctx = self._ctx("2026-10-19")
        b = ctx["tests"][1]
        self.assertEqual(b["fav_gate"], {"model_secs": 48000, "gate_secs": 28800, "files": 48})
        self.assertIsNone(ctx["tests"][0]["fav_gate"])
        page = R.render_html(ctx)
        self.assertIn("Agreement gate in the shadow: it let a favorite join through in 60.0% of "
                      "the 48,000 strike-seconds", page)
        self.assertIn("fav_market.ok", page)
        self.assertIn("agreement gate in the shadow: passed 60.0% of 48,000", R.render_text(ctx))


if __name__ == "__main__":
    unittest.main()
