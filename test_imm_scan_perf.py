#!/usr/bin/env python3
"""Unit tests for imm_scan_perf.py — run: python -m unittest test_imm_scan_perf

Every test builds SYNTHETIC sinks in a tempdir. Nothing here ever reads or
writes the live ``run-logs/incentive-mm`` — the live bot and four daily tasks
own that directory, and the 2026-07-28 incident (gate-test dry takes landed in
the LIVE rain_directional_ledger.csv) is why every path is redirected.

Docstrings quote the SPEC section and the MEASURED incident the rule exists
for, per house convention.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import imm_scan_perf as M

UTC = timezone.utc
T0 = datetime(2026, 9, 6, 0, 0, 0, tzinfo=UTC)
ASOF = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)

_HDR = M._CYCLE_HDR
_NCOL = len(M._CYCLE_COLS)


def _tiny_replay(fills, settles):
    """A deliberately SEPARATE avg-cost implementation used only to generate
    consistent synthetic ``realized`` sink rows. The scorer has its own; the
    point of SPEC 4.4 is that the two must agree to the cent."""
    pos, avg, out = defaultdict(float), defaultdict(float), []
    stream = ([(float(f["ts"]), "f", f) for f in fills] +
              [(M.ts_of(s["ts"]), "s", s) for s in settles])
    stream.sort(key=lambda x: x[0])

    def book(t, signed, px):
        p, a = pos[t], avg[t]
        if p * signed >= 0:
            new = p + signed
            if abs(new) > 1e-9:
                avg[t] = (abs(p) * a + abs(signed) * px) / abs(new)
            pos[t] = new
            return 0.0
        closed = min(abs(signed), abs(p))
        per = (px - a) if p > 0 else (a - px)
        d = closed * per / 100.0
        new = p + signed
        pos[t] = new
        if p * new < 0:
            avg[t] = px
        elif abs(new) < 1e-9:
            avg[t] = 0.0
        return d

    for ts, kind, rec in stream:
        t = rec["ticker"]
        if kind == "f":
            side, action = rec["side"], rec["action"]
            if side == "no":
                action = "sell" if action == "buy" else "buy"
            signed = rec["count"] if action == "buy" else -rec["count"]
            d = book(t, signed, rec["yes_price_cents"])
        else:
            if rec.get("result") not in ("yes", "no"):
                continue
            p = pos[t]
            d = book(t, -p, rec["settle_price_cents"])
        if abs(d) > 1e-12:
            out.append((ts, t, d, pos[t], avg[t]))
    return out


class Sinks:
    """Synthetic STATUS_DIR."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.rows = defaultdict(list)          # day -> list[list[str]]
        self.fills = []
        self.settles = []
        self.marks = []
        self.selection = []
        self.extra_realized = []
        self.state = {"scan_book": [], "scan_members": [], "own_pos": {},
                      "own_avg": {}, "own_filled": {}, "scan_series_meta": {},
                      "accrued_est": {}}
        self.roster = {"schema": 2, "scan_events": [], "realized": {}}
        self.programs = {}
        self.credits = []                      # (credit_date, event, amount)
        self.auto_realized = True

    # ---- cycle_log ------------------------------------------------------
    def cycle(self, ts, ticker, *, bid=40, ask=42, est_frac=0.01, sides=2,
              own_pos=0.0, pool=30.0, own_bid_ct=0, own_ask_ct=0, is_scan=1,
              cols=_NCOL):
        row = [""] * cols
        row[M.C_TS] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        row[M.C_TKR] = ticker
        row[M.C_BID] = "" if bid is None else str(bid)
        row[M.C_ASK] = "" if ask is None else str(ask)
        row[M.C_EST_FRAC] = str(est_frac)
        row[M.C_SIDES] = str(sides)
        if cols > M.C_OWN_POS:
            row[M.C_OWN_POS] = str(own_pos)
        row[M.C_POOL] = str(pool)
        if cols > M.C_OWN_ASK_CT:
            row[M.C_OWN_BID_CT] = str(own_bid_ct)
            row[M.C_OWN_ASK_CT] = str(own_ask_ct)
        if cols > M.C_IS_SCAN:
            row[M.C_IS_SCAN] = str(is_scan)
        self.rows[ts.strftime("%Y-%m-%d")].append(row)
        return self

    def quotes(self, ticker, start, n, step=300, **kw):
        for i in range(n):
            self.cycle(start + timedelta(seconds=i * step), ticker, **kw)
        return self

    # ---- other sinks ----------------------------------------------------
    def fill(self, ts, ticker, count, *, side="yes", action="buy", px=40.0,
             pos_before=0.0, pos_after=None, avg_before=0.0, fill_id=None):
        if pos_after is None:
            signed = count if (action == "buy") == (side == "yes") else -count
            pos_after = pos_before + signed
        rec = {"ts": ts.timestamp(), "fill_id": fill_id or f"f{len(self.fills)}",
               "ticker": ticker, "series": M.ser_of(ticker),
               "event_ticker": M.ev_of(ticker), "side": side, "action": action,
               "count": float(count), "yes_price_cents": float(px),
               "pos_before": float(pos_before), "pos_after": float(pos_after),
               "avg_before": float(avg_before), "is_scan": True}
        self.fills.append(rec)
        return self

    def settle(self, ts, ticker, price, result=None):
        self.settles.append({
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "ticker": ticker,
            "series": M.ser_of(ticker), "event_ticker": M.ev_of(ticker),
            "result": result or ("yes" if price >= 50 else "no"),
            "settle_price_cents": float(price)})
        return self

    def mark(self, ts, ticker, mark_cents, pos=0.0, avg=0.0):
        self.marks.append({"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "ticker": ticker, "pos": pos, "avg_cents": avg,
                           "mark_cents": float(mark_cents)})
        return self

    def admit(self, ts, ticker, *, per_day=1.0, collateral=10.0, mid=41.0,
              spread=2, pool=30.0, event=None):
        ev = event or M.ev_of(ticker)
        self.selection.append({
            "ticker": ticker, "decision": "selected", "prev": None,
            "series": M.ser_of(ticker), "event_ticker": ev,
            "est_dollars_per_day": per_day, "est_collateral_dollars": collateral,
            "dollars_per_day": pool, "mid_cents": mid, "spread_cents": spread,
            "is_scan": True, "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")})
        self.state["scan_book"].append(ticker)
        if ev not in self.roster["scan_events"]:
            self.roster["scan_events"].append(ev)
        return self

    def program(self, ticker, start, end):
        self.programs[ticker] = {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "paid": True, "reward": 100000}
        return self

    def credit(self, date, event, amount):
        self.credits.append((date, event, float(amount)))
        return self

    # ---- writing --------------------------------------------------------
    def write(self):
        for day, rows in self.rows.items():
            rows.sort(key=lambda r: (r[M.C_TS], r[M.C_TKR]))
            with open(os.path.join(self.root, f"cycle_log_{day}.csv"), "w",
                      encoding="utf-8", newline="") as f:
                f.write(_HDR + "\n")
                for r in rows:
                    f.write(",".join(r) + "\n")
        self._jsonl("fills", self.fills, lambda r: datetime.fromtimestamp(
            r["ts"], UTC).strftime("%Y-%m-%d"))
        self._jsonl("settlements", self.settles, lambda r: r["ts"][:10])
        self._jsonl("marks", self.marks, lambda r: r["ts"][:10])
        self._jsonl("selection_events", self.selection, lambda r: r["ts"][:10])
        real = []
        if self.auto_realized:
            for ts, t, d, pos, avg in _tiny_replay(self.fills, self.settles):
                real.append({"ts": datetime.fromtimestamp(ts, UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"), "ticker": t, "series": M.ser_of(t),
                    "event_ticker": M.ev_of(t), "realized_delta_dollars": d,
                    "realized_total_dollars": d, "pos_after": pos, "avg_after": avg})
        real.extend(self.extra_realized)
        self._jsonl("realized", real, lambda r: r["ts"][:10])
        with open(os.path.join(self.root, "imm_state.json"), "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        with open(os.path.join(self.root, "opportunistic_roster.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.roster, f)
        with open(os.path.join(self.root, "reward_programs.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.programs, f)
        with open(os.path.join(self.root, "reward_credits.csv"), "w",
                  encoding="utf-8", newline="") as f:
            f.write("credit_date,event_ticker,amount,kind\n")
            for d, e, a in self.credits:
                f.write(f"{d},{e},{a:.2f},Liquidity\n")
        return self

    def _jsonl(self, name, recs, daykey):
        by_day = defaultdict(list)
        for r in recs:
            by_day[daykey(r)].append(r)
        for day, rows in by_day.items():
            with open(os.path.join(self.root, f"{name}_{day}.jsonl"), "a",
                      encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="scanperf_test_")
        self.sd = os.path.join(self.tmp, "sinks")
        self.wd = os.path.join(self.tmp, "work")
        os.makedirs(self.sd, exist_ok=True)
        os.makedirs(self.wd, exist_ok=True)
        self.s = Sinks(self.sd)
        self._old = (M.STATUS_DIR, M.WORK_DIR, M.MIN_RISK_DAYS, M.BLOCK_ROI)

    def tearDown(self):
        (M.STATUS_DIR, M.WORK_DIR, M.MIN_RISK_DAYS, M.BLOCK_ROI) = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def build(self, *, asof=ASOF, use_cache=False, **kw):
        self.s.write()
        M.STATUS_DIR = self.sd
        M.WORK_DIR = self.wd
        sc = M.ScanPerfScorer(self.sd, self.wd, asof=asof, use_cache=use_cache, **kw)
        sc.load()
        sc.build()
        return sc

    def read_table(self):
        with open(os.path.join(self.wd, M.TABLE_NAME), encoding="utf-8") as f:
            return json.load(f)

    def run_cli(self, *extra, asof=ASOF, write=True, write_sinks=True):
        if write_sinks:
            self.s.write()
        argv = ["--status-dir", self.sd, "--work-dir", self.wd,
                "--asof", asof.strftime("%Y-%m-%dT%H:%M:%SZ")]
        argv += ["--now"] if write else ["--dry"]
        argv += list(extra)
        buf = io.StringIO()
        rc = M.run(argv, out=buf)
        return rc, buf.getvalue()

    def simple_market(self, ticker, *, start=None, hours=8, bid=40, ask=42,
                      own_bid_ct=20, pool=30.0, event=None, **kw):
        start = start or T0
        self.s.admit(start, ticker, pool=pool, mid=(bid + ask) / 2.0,
                     spread=ask - bid, event=event)
        self.s.quotes(ticker, start, int(hours * 12), bid=bid, ask=ask,
                      own_bid_ct=own_bid_ct, pool=pool, **kw)
        return ticker


# ===================================================================== 27-31

class TestMarkHierarchy(Base):

    def test_mark_hierarchy_settlement_then_two_sided_then_one_sided_then_unmarked(self):
        """SPEC 2.4: four tiers and NO fifth carry-forward tier.

        Judge must_fix J1-7: a market that went flat and was dropped has a
        dead book; manufacturing a mark from the last live quote and feeding
        it to a verdict is exactly what PnlTracker.unrealized already
        refuses to do."""
        t_fill = T0 + timedelta(hours=1)
        t_mark = t_fill + timedelta(hours=24)
        # (a) settlement wins
        self.simple_market("KXSET-26SEP20-T1", hours=2)
        self.s.fill(t_fill, "KXSET-26SEP20-T1", 10, px=40)
        self.s.settle(t_fill + timedelta(hours=3), "KXSET-26SEP20-T1", 100)
        # (b) two-sided quote at the horizon
        self.simple_market("KXTWO-26SEP20-T1", hours=2)
        self.s.quotes("KXTWO-26SEP20-T1", t_mark - timedelta(seconds=60), 2,
                      bid=60, ask=64)
        self.s.fill(t_fill, "KXTWO-26SEP20-T1", 10, px=40)
        # (c) one side only at the horizon
        self.simple_market("KXONE-26SEP20-T1", hours=2)
        self.s.quotes("KXONE-26SEP20-T1", t_mark - timedelta(seconds=60), 2,
                      bid=50, ask=None, sides=1)
        self.s.fill(t_fill, "KXONE-26SEP20-T1", 10, px=40)
        # (d) nothing within tolerance -> UNMARKED (no carry-forward)
        self.simple_market("KXNIL-26SEP20-T1", hours=2)
        self.s.fill(t_fill, "KXNIL-26SEP20-T1", 10, px=40)

        sc = self.build()
        src = {r["ticker"]: r for r in sc.fill_rows}
        self.assertEqual(src["KXSET-26SEP20-T1"]["source"], "settlement")
        self.assertAlmostEqual(src["KXSET-26SEP20-T1"]["mark"], 100.0)
        # the SUB-source: the live cycle book, distinct from the marks-sink
        # fallback that shares tier 2 with it
        self.assertEqual(src["KXTWO-26SEP20-T1"]["source"], "cycle_two_sided")
        self.assertAlmostEqual(src["KXTWO-26SEP20-T1"]["mark"], 62.0)
        self.assertEqual(src["KXONE-26SEP20-T1"]["source"], "one_sided")
        self.assertEqual(src["KXNIL-26SEP20-T1"]["source"], "unmarked")
        self.assertIsNone(src["KXNIL-26SEP20-T1"]["mo"])
        self.assertEqual(sc.market_mo.get("KXNIL-26SEP20-T1", 0.0), 0.0)
        # the SPEC's three-key mix still folds both tier-2 sinks together
        self.assertEqual(set(sc.mark_source_mix),
                         {"two_sided", "one_sided", "settlement"})
        self.assertAlmostEqual(sum(sc.mark_source_mix.values()), 1.0, places=3)

    def test_marks_sink_fallback_is_reported_as_its_own_sub_source(self):
        """F1: the marks-sink fallback shares tier 2 with the live cycle book
        but is NOT the same thing -- its value is state.last_mark, which the
        bot fills from a bulk get_markets mid, falls back to last_price (a
        trade print) when bid/ask are missing, and leaves in place on an API
        failure ("stale marks stand"). Reporting it as `two_sided` blinded
        mark_source_mix, the one detector SPEC 2.4 added so a drift toward
        staler marks is visible BEFORE it changes a verdict. MEASURED on the
        live window: 22.66% of scored contracts rode this channel."""
        t_fill = T0 + timedelta(hours=1)
        t_mark = t_fill + timedelta(hours=24)
        tkr = "KXSINK-26SEP20-T1"
        self.simple_market(tkr, hours=2)
        # NO cycle row anywhere near the horizon; only a marks_*.jsonl row
        self.s.mark(t_mark - timedelta(seconds=120), tkr, 70.0)
        self.s.fill(t_fill, tkr, 10, px=40)
        sc = self.build()
        row = next(r for r in sc.fill_rows if r["ticker"] == tkr)
        self.assertEqual(row["source"], "sink_mark")
        self.assertAlmostEqual(row["mark"], 70.0)
        # still counts as two_sided in the SPEC's three-key mix ...
        self.assertAlmostEqual(sc.mark_source_mix["two_sided"], 1.0)
        # ... and is broken out in the detail, which is the honest detector
        self.assertAlmostEqual(sc.mark_source_detail["sink_mark"], 1.0)
        self.assertAlmostEqual(sc.mark_source_detail["cycle_two_sided"], 0.0)
        self.assertEqual(sc.mark_source_detail["sink_mark_fills"], 1)

    def test_one_sided_mark_uses_the_trailing_24h_median_half_spread(self):
        """SPEC 2.4 tier 3: MEDIAN over the trailing 24 h, never the
        instantaneous spread — a book that blows out for one cycle must not
        widen the mark that scores a fill."""
        t_fill = T0 + timedelta(hours=1)
        t_mark = t_fill + timedelta(hours=24)
        tkr = "KXMED-26SEP20-T1"
        self.simple_market(tkr, start=T0, hours=2)
        # trailing two-sided history: mostly 2c, one 30c blow-out
        self.s.quotes(tkr, t_mark - timedelta(hours=6), 40, bid=49, ask=51)
        # The blow-out sits OUTSIDE the +/-900s mark tolerance on purpose: it
        # must feed the trailing-24h median and nothing else. Inside the
        # tolerance it is a legitimate tier-2 two-sided row and tier 3 never
        # runs -- which is what the old nearest-3-neighbours search in
        # _cycle_near was accidentally hiding (F11).
        self.s.cycle(t_mark - timedelta(hours=2), tkr, bid=35, ask=65)
        self.s.cycle(t_mark - timedelta(seconds=30), tkr, bid=50, ask=None, sides=1)
        self.s.fill(t_fill, tkr, 10, px=40)
        sc = self.build()
        row = next(r for r in sc.fill_rows if r["ticker"] == tkr)
        self.assertEqual(row["source"], "one_sided")
        self.assertAlmostEqual(row["mark"], 51.0, places=6)   # 50 + median(2)/2

    def test_episode_collapses_a_multi_rung_sweep_into_one_observation(self):
        """SPEC 2.2, MEASURED: 21 fills of >=40 contracts carry 910 of 2,685
        contracts (34%). A multi-rung ladder sweep inside one cycle is ONE
        price event; treating the rungs as independent triples the sample."""
        tkr = "KXEP-26SEP20-T1"
        t_fill = T0 + timedelta(hours=1)
        self.simple_market(tkr, hours=2)
        self.s.quotes(tkr, t_fill + timedelta(hours=24) - timedelta(seconds=30), 2,
                      bid=29, ask=31)
        pos = 0
        for i, (ct, px) in enumerate([(10, 40), (20, 41), (30, 42), (40, 43)]):
            self.s.fill(t_fill + timedelta(seconds=i * 60), tkr, ct, px=px,
                        pos_before=pos)
            pos += ct
        sc = self.build()
        self.assertEqual(len(sc.episodes), 1)
        ep = list(sc.episodes.values())[0]
        self.assertEqual(ep["fills"], 4)
        self.assertEqual(ep["ct"], 100)
        want = sum(c * (30 - p) for c, p in [(10, 40), (20, 41), (30, 42), (40, 43)]) / 100.0
        self.assertAlmostEqual(ep["mo"], want, places=6)

    def test_markout_sign_follows_position_delta_not_the_side_field(self):
        """SPEC 2.3 — the Price_In_Cents trap class (memory
        reference_kalshi_csv_price_convention): a sell-YES and a buy-NO at the
        same effective YES price MUST get the same sign."""
        t_fill = T0 + timedelta(hours=1)
        t_mark = t_fill + timedelta(hours=24)
        for tkr, side, action in (("KXSY-26SEP20-T1", "yes", "sell"),
                                  ("KXBN-26SEP20-T1", "no", "buy")):
            self.simple_market(tkr, hours=2)
            self.s.quotes(tkr, t_mark - timedelta(seconds=30), 2, bid=59, ask=61)
            self.s.fill(t_fill, tkr, 10, side=side, action=action, px=40,
                        pos_before=50.0, pos_after=40.0)
        sc = self.build()
        rows = {r["ticker"]: r for r in sc.fill_rows}
        self.assertEqual(rows["KXSY-26SEP20-T1"]["d"], -1.0)
        self.assertEqual(rows["KXBN-26SEP20-T1"]["d"], -1.0)
        self.assertAlmostEqual(rows["KXSY-26SEP20-T1"]["mo"],
                               rows["KXBN-26SEP20-T1"]["mo"])
        self.assertLess(rows["KXSY-26SEP20-T1"]["mo"], 0.0)

    def test_immature_fills_are_pending_not_zero(self):
        """SPEC 2.4: a fill enters the sample only once now >= t + H + 15 min.
        Earlier fills are `pending` — never scored as a zero markout."""
        tkr = "KXPEND-26SEP20-T1"
        self.simple_market(tkr, start=ASOF - timedelta(hours=6), hours=1)
        self.s.fill(ASOF - timedelta(hours=1), tkr, 10, px=40)
        sc = self.build()
        self.assertEqual(sc.cov["fills_pending"], 1)
        self.assertEqual(sc.cov["fills_matured"], 0)
        # `episodes` counts EVERY fill collapsed to (ticker, UTC hour) and
        # `episodes_matured` the marked-and-matured subset, so the file can
        # express SPEC 2.2's "106 fills -> 85 episodes (63 matured)" shape.
        # Emitting len(self.episodes) for both made them identical by
        # construction and the distinction unfalsifiable.
        self.assertEqual(sc.cov["episodes"], 1)
        self.assertEqual(sc.cov["episodes_matured"], 0)
        self.assertEqual(sc.market_mo.get(tkr, 0.0), 0.0)
        row = next(r for r in sc.fill_rows if r["ticker"] == tkr)
        self.assertEqual(row["status"], "pending")


# ===================================================================== 32-36

class TestGroupsAndExposure(Base):


    def test_adopted_position_is_capped_at_own_fill_contracts(self):
        """SPEC 2.5 adopted-position rule. MEASURED: KXAALA-27JANPLF-83 held
        +30 @ 85.67 with ZERO bot fills (an inherited account position) and
        -$15.05 MTM; it must contribute no inventory $-days and no verdict."""
        adopted = "KXADOPT-26SEP20-T1"
        self.simple_market(adopted, hours=4, own_bid_ct=0)
        self.s.quotes(adopted, T0 + timedelta(hours=4), 48, own_pos=30.0,
                      own_bid_ct=0)
        partial = "KXPART-26SEP20-T1"
        self.simple_market(partial, hours=4, own_bid_ct=0)
        self.s.quotes(partial, T0 + timedelta(hours=4), 48, own_pos=50.0,
                      own_bid_ct=0)
        self.s.fill(T0 + timedelta(hours=1), partial, 20, px=50)
        sc = self.build()
        self.assertEqual(sc.markets[adopted]["inv_days"], 0.0)
        self.assertTrue(sc.markets[adopted]["adopted_capped"])
        self.assertIn(adopted, sc.adopted_capped)
        # partial: 20 of 50 counted -> exactly 2/5 of the uncapped integral
        full = sc.markets[partial]["inv_days"] * 50.0 / 20.0
        self.assertGreater(sc.markets[partial]["inv_days"], 0.0)
        self.assertAlmostEqual(sc.markets[partial]["inv_days"] / full, 0.4, places=6)
        self.assertTrue(sc.markets[partial]["adopted_capped"])

    def test_rent_floor_zeroes_a_sub_dollar_market_period(self):
        """SPEC 2.6: a MEASURED exchange rule — the minimum Liquidity credit
        is exactly $1.00 across 7,264 ledger rows and 39% of quoted markets
        never clear it."""
        tiny = "KXTINY-26SEP20-T1"
        self.simple_market(tiny, hours=6, pool=0.5)          # ~ $0.00x of est
        big = "KXBIG-26SEP20-T1"
        self.simple_market(big, hours=6, pool=5000.0)
        sc = self.build()
        self.assertGreater(sc.market_rent_raw[tiny], 0.0)
        self.assertLess(sc.market_rent_raw[tiny], M.PAYOUT_FLOOR)
        self.assertEqual(sc.market_rent[tiny], 0.0)
        self.assertGreater(sc.market_rent[big], M.PAYOUT_FLOOR)

    def test_est_and_credited_are_never_summed_for_the_same_period(self):
        """SPEC 2.6 rent basis + memory
        reference_imm_accrued_est_never_resets: EST and CREDITED never
        contain each other and are never summed."""
        tkr = "KXCRED-26SEP20-T1"
        self.simple_market(tkr, hours=10, pool=5000.0)
        self.s.program(tkr, T0 - timedelta(days=3), T0 + timedelta(hours=5))
        self.s.credit("2026-09-07", M.ev_of(tkr), 7.25)
        self.s.credit("2026-09-09", "KXOTHER-26SEP20", 3.0)   # newest date
        sc = self.build()
        periods = sc.market_rent_periods[tkr]
        bases = [p["basis"] for p in periods]
        self.assertIn("credited", bases)
        # a ledger row is consumed by exactly ONE event period, never by two
        self.assertAlmostEqual(sc.event_rent_measured[M.ev_of(tkr)], 7.25)
        # the credited period's own floored estimate is NOT added to the credit
        est_side = sum(p["floored"] for p in periods if p["basis"] != "credited")
        self.assertAlmostEqual(sc.event_rent[M.ev_of(tkr)], 7.25 + est_side)
        credited_est = sum(p["floored"] for p in periods if p["basis"] == "credited")
        self.assertGreater(credited_est, 0.0)
        self.assertNotAlmostEqual(sc.event_rent[M.ev_of(tkr)],
                                  7.25 + est_side + credited_est)
        rec = sc.series_recs["KXCRED"]
        self.assertGreater(rec["rent_measured_dollars"], 0.0)
        self.assertIn(rec["rent_basis"], ("credited", "mixed_by_period"))


# ===================================================================== 37-42

class TestIdempotenceAndGuards(Base):

    def _corpus(self):
        for i in range(3):
            self.simple_market(f"KXA{i}-26SEP20-T1", hours=6, bid=40, ask=42)
        self.s.fill(T0 + timedelta(hours=1), "KXA0-26SEP20-T1", 10, px=40)
        self.s.quotes("KXA0-26SEP20-T1", T0 + timedelta(hours=25)
                      - timedelta(seconds=30), 2, bid=29, ask=31)

    def test_partial_day_is_recomputed_not_folded_twice(self):
        """SPEC 4.2: only COMPLETE UTC day files are cached; today's partial
        day is recomputed from scratch every run (the
        send_opportunistic_imm.durable_history convention — summing a partial
        day twice double-counts)."""
        self._corpus()
        today = ASOF.replace(hour=0, minute=0, second=0)
        self.s.quotes("KXA0-26SEP20-T1", today + timedelta(hours=1), 6)
        self.s.write()
        M.STATUS_DIR, M.WORK_DIR = self.sd, self.wd
        a = M.ScanPerfScorer(self.sd, self.wd, asof=ASOF, use_cache=True)
        a.load(); a.build(); a.cache.save()
        # today's file grows
        self.s.rows.clear()
        self.s.quotes("KXA0-26SEP20-T1", today + timedelta(hours=3), 6)
        self.s.write()
        b = M.ScanPerfScorer(self.sd, self.wd, asof=ASOF, use_cache=True)
        b.load(); b.build()
        c = M.ScanPerfScorer(self.sd, self.wd, asof=ASOF, use_cache=False)
        c.load(); c.build()
        self.assertAlmostEqual(b.risk_total, c.risk_total, places=6)
        self.assertAlmostEqual(sum(b.market_rent_raw.values()),
                               sum(c.market_rent_raw.values()), places=9)
        self.assertEqual(b.cov["episodes"], c.cov["episodes"])
        self.assertGreater(a.cache.misses, 0)
        self.assertGreater(b.cache.hits, 0)

    def test_scorer_is_idempotent_on_rerun(self):
        """SPEC 4.2: two runs in the same minute produce a byte-identical
        table modulo the run-to-run deltas scrubbed below.

        `generated_at` and the whole `window` block are NOT among them:
        run_cli always passes an explicit --asof, so all four are derived
        from a constant and compared as-is. (Without that pin they would be
        stamped from datetime.now() and two runs straddling a UTC second
        boundary would fail -- which is why the --asof is load-bearing here
        and not decoration.)"""
        self._corpus()
        rc1, _ = self.run_cli()
        with open(os.path.join(self.wd, M.TABLE_NAME), encoding="utf-8") as f:
            first = f.read()
        rc2, _ = self.run_cli()
        with open(os.path.join(self.wd, M.TABLE_NAME), encoding="utf-8") as f:
            second = f.read()
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertEqual(json.loads(first)["generated_at"],
                         json.loads(second)["generated_at"])
        self.assertEqual(json.loads(first)["window"],
                         json.loads(second)["window"])

        def scrub(txt):
            d = json.loads(txt)
            d["audit"]["diagnostics"].pop("timings", None)
            d["audit"]["diagnostics"].pop("cache_hits", None)
            d["audit"]["diagnostics"].pop("cache_misses", None)
            # run-to-run deltas by construction, not part of the computation
            d["tier"].pop("risk_days_dod_change", None)
            d["notes"] = [n for n in d["notes"] if "previous scan_perf.json" not in n]
            return json.dumps(d, sort_keys=True)
        self.assertEqual(scrub(first), scrub(second))

    def test_scorer_refuses_to_write_when_reconciliation_fails(self):
        """SPEC 4.4 guard 1 (de-circularised, judge must_fix J2-2): the
        INDEPENDENT avg-cost replay must equal the realized-sink deltas over
        complete UTC days to the cent."""
        self._corpus()
        self.s.extra_realized.append({
            "ts": (T0 + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ticker": "KXA0-26SEP20-T1", "series": "KXA0",
            "event_ticker": "KXA0-26SEP20", "realized_delta_dollars": -2.0,
            "realized_total_dollars": -2.0, "pos_after": 0.0, "avg_after": 0.0})
        rc, txt = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("RECONCILIATION FAILED", txt)
        self.assertFalse(os.path.exists(os.path.join(self.wd, M.TABLE_NAME)))

    def test_scorer_refuses_to_write_on_a_thirty_percent_exposure_drop(self):
        """SPEC 4.4 guard 2: _sink self-mutes after ONE failure and can go
        dark mid-day; a silently shrunken denominator inflates every dev and
        is the most likely way a bad table reaches the bot."""
        self._corpus()
        self.s.write()
        M.WORK_DIR = self.wd
        M.append_jsonl(os.path.join(self.wd, M.HISTORY_NAME),
                       {"generated_at": "2026-09-09T07:55:00Z", "risk_days": 1e6})
        rc, txt = self.run_cli()
        self.assertEqual(rc, 3)
        self.assertIn("EXPOSURE DROP", txt)
        self.assertFalse(os.path.exists(os.path.join(self.wd, M.TABLE_NAME)))

    def test_a_backward_asof_rescore_announces_the_skipped_exposure_guard(self):
        """A deliberate --asof re-score covers an earlier window than the
        history row it is compared against, so a smaller denominator is
        arithmetic, not the sink going dark. The skip is PRINTED — 'a model is
        not a measurement' cuts both ways: a silently skipped guard is worse
        than a noisy one."""
        self._corpus()
        self.s.write()
        M.WORK_DIR = self.wd
        M.append_jsonl(os.path.join(self.wd, M.HISTORY_NAME),
                       {"generated_at": "2026-09-30T07:55:00Z", "risk_days": 1e6})
        rc, txt = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("exposure-drop guard skipped", txt)
        self.assertIn("BACKWARD re-score", txt)


    def test_scorer_does_not_write_reward_est_cache(self):
        """SPEC 2.6 / judge must_fix J2-10: rebuild_estimates() rewrites the
        LIVE reward_est_cache.json unconditionally and would fight the 07:50
        `KL imm program-history` job."""
        self._corpus()
        cache = os.path.join(self.sd, "reward_est_cache.json")
        with open(cache, "w", encoding="utf-8") as f:
            f.write("{}")
        os.utime(cache, (1_700_000_000, 1_700_000_000))
        before = os.stat(cache).st_mtime
        rc, _ = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertEqual(os.stat(cache).st_mtime, before)

    def test_scorer_is_read_only_on_status_dir(self):
        """Jack: the live data dir is owned by the bot and four daily tasks.
        Every write goes through check_write_path, so a stray open() under
        STATUS_DIR is impossible when --work-dir points elsewhere."""
        self._corpus()
        self.s.write()
        opened = []
        real_open = open

        def spy(path, mode="r", *a, **kw):
            if any(c in str(mode) for c in "wax+"):
                opened.append(os.path.abspath(str(path)))
            return real_open(path, mode, *a, **kw)
        import builtins
        builtins.open = spy
        try:
            rc, _ = self.run_cli(write_sinks=False)
        finally:
            builtins.open = real_open
        self.assertEqual(rc, 0)
        self.assertTrue(opened)
        for p in opened:
            self.assertEqual(os.path.dirname(p), os.path.abspath(self.wd),
                             f"wrote outside the work dir: {p}")
            self.assertIn(os.path.basename(p).replace(".tmp", ""),
                          M._ALLOWED_WRITE)
        with self.assertRaises(M.WriteGuardError):
            M.check_write_path(os.path.join(self.sd, "imm_state.json"))


class TestFileShape(Base):


    def test_empty_window_is_handled_without_complaint(self):
        """The sinks start 2026-09-06; a 45-day window necessarily begins
        before they exist and must produce a neutral, empty table rather than
        an error."""
        rc, txt = self.run_cli(asof=datetime(2026, 9, 7, tzinfo=UTC))
        self.assertEqual(rc, 0)
        tbl = self.read_table()
        self.assertEqual(tbl["coverage"]["markets"], 0)
        self.assertEqual(tbl["series"], {})
        self.assertEqual(tbl["limits"]["n_units"], len(M.FAMILY_GROUPS))

    def test_explain_prints_a_fill_by_fill_table(self):
        """SPEC 4.3 --explain: each fill's mark, mark source, horizon and
        markout, so a verdict can be argued with rather than believed."""
        tkr = "KXEXP-26SEP20-T1"
        self.simple_market(tkr, hours=6)
        self.s.fill(T0 + timedelta(hours=1), tkr, 10, px=40)
        self.s.quotes(tkr, T0 + timedelta(hours=25) - timedelta(seconds=30), 2,
                      bid=29, ask=31)
        self.s.write()
        buf = io.StringIO()
        rc = M.run(["--status-dir", self.sd, "--work-dir", self.wd,
                    "--asof", ASOF.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "--explain", "KXEXP"], out=buf)
        self.assertEqual(rc, 0)
        txt = buf.getvalue()
        self.assertIn("KXEXP-26SEP20-T1", txt)
        self.assertIn("two_sided", txt)
        self.assertIn("roi_hist", txt)
        self.assertFalse(os.path.exists(os.path.join(self.wd, M.TABLE_NAME)))
    def test_written_table_matches_the_v2_shape(self):
        """The bot's loader validates against this schema (version 2), so the
        field names and types are the contract: every unit record carries the
        MEASURED/MODELLED components of its roi_hist, a verdict from the
        three-word vocabulary, and a TTL exactly when it blocks."""
        for i in range(3):
            self.simple_market(f"KXS{i}-26SEP20-T1", hours=6)
        self.s.fill(T0 + timedelta(hours=1), "KXS0-26SEP20-T1", 10, px=40)
        self.s.quotes("KXS0-26SEP20-T1", T0 + timedelta(hours=25)
                      - timedelta(seconds=30), 2, bid=29, ask=31)
        rc, _ = self.run_cli()
        self.assertEqual(rc, 0)
        tbl = self.read_table()
        self.assertEqual(tbl["version"], 2)
        for k in ("generated_at", "generated_by", "window", "params", "coverage",
                  "tier", "events", "series", "families", "markets", "limits",
                  "audit", "unmatched_block_keys", "warnings", "notes"):
            self.assertIn(k, tbl)
        for k in ("min_risk_days", "block_roi", "block_ttl_hours", "roi_clip_lo",
                  "roi_clip_hi", "family_groups"):
            self.assertIn(k, tbl["params"])
        for blk in ("events", "series", "families"):
            for key, rec in tbl[blk].items():
                self.assertIn(rec["verdict"], ("block", "allow", "insufficient"))
                self.assertEqual("until" in rec, rec["verdict"] == "block", f"{blk}:{key}")
                for f in ("kind", "key", "risk_days", "roi_hist", "roi_hist_trading",
                          "roi_hist_measured", "rent_used", "rent_modelled",
                          "rent_measured_dollars", "rent_basis", "credited_measured",
                          "realized_dollars", "mtm_dollars", "net_dollars", "reason",
                          "n_markets", "n_events"):
                    self.assertIn(f, rec, f"{blk}:{key} lacks {f}")
        for name, rec in tbl["families"].items():
            self.assertIn("members", rec)
            self.assertIn("match", rec)
        self.assertEqual(set(tbl["families"]), {g["name"] for g in M.FAMILY_GROUPS})
        for k in ("blocked_events", "blocked_series", "blocked_families", "n_units"):
            self.assertIn(k, tbl["limits"])
        self.assertEqual(tbl["audit"]["counterfactual_label"],
                         "IN-SAMPLE, MODELLED COUNTERFACTUAL")
        self.assertTrue(os.path.exists(os.path.join(self.wd, M.HISTORY_NAME)))
        with open(os.path.join(self.wd, M.HISTORY_NAME), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        for k in ("generated_at", "risk_days", "risk_days_at50c", "roi_hist",
                  "roi_hist_trading", "n_block_series", "n_units", "exit_code"):
            self.assertIn(k, rows[-1])

    def test_counterfactual_lines_carry_the_in_sample_label(self):
        """Every counterfactual carries the literal prefix at every point of
        use (Jack: a model is not a measurement), and every printed money
        figure says whether it is MEASURED or MODELLED."""
        for i in range(3):
            self.simple_market(f"KXC{i}-26SEP20-T1", hours=6)
        rc, txt = self.run_cli(write=False)
        self.assertEqual(rc, 0)
        self.assertIn("[IN-SAMPLE, MODELLED COUNTERFACTUAL]", txt)
        self.assertIn("[MEASURED]", txt)
        self.assertIn("[MODELLED mark]", txt)
        self.assertIn("units (event ROOT -> series -> family", txt)
        self.assertIn("rule: block a unit under", txt)


class TestBotLoaderRoundTrip(Base):
    """SPEC test 47 — the integration seam between the two units.

    The scorer and the bot loader were built by different agents against the
    same SPEC section 5, so nothing but a real round trip proves they agree on
    field names, on ``hi: null`` meaning +infinity, on bucket ``centre``, on
    ``params.dim_clip``, on ``edges_refit``, on ``until`` being a bar's TTL,
    and on ``events`` being keyed by EVENT ROOT rather than by dated event.
    The loader is ALL-OR-NOTHING (SPEC 6.2 step 4): one disagreed field does
    not degrade the table, it discards the whole thing and the tier silently
    keeps the previous verdicts — which is exactly the "a frozen verdict
    outliving its evidence" failure the age check exists to prevent. Jack,
    9/12, on the half-patched file that ran six minutes in prod: verify
    through the real path, not by reading two files side by side.

    Two invariants are pinned here on purpose, because the writer and the
    reader count them over DIFFERENT sets and the writer must never be the
    laxer of the two (both aligned at integration, 2026-09-18):
      * the clamp-storm fraction — the loader counts every bucket
        ``dev_trading`` plus each series/event ``dev``, never ``dev_blend``;
      * the record count — the loader counts series + events + BUCKETS and
        never reads the ``markets`` block at all.
    """

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _bot():
        try:
            import incentive_mm as imm
        except Exception as e:                       # pragma: no cover
            raise unittest.SkipTest(f"incentive_mm not importable here: {e}")
        if not hasattr(imm, "load_scan_perf"):
            raise unittest.SkipTest("incentive_mm has no load_scan_perf yet "
                                    "(SPEC test 47 lands with the bot unit)")
        return imm
    def _load_into_bot(self, imm, path):
        """Run the REAL loader against ``path``, restoring every scrap of bot
        module state afterwards. test_incentive_mm imports the same module
        object in the same process, so a leaked table would be a live verdict
        table sitting under another suite's tests."""
        saved = (getattr(imm, "SCAN_PERF_FILE", None),
                 dict(getattr(imm, "_scan_perf_state", {}) or {}),
                 dict(imm.SCAN_PERF_EVENTS), dict(imm.SCAN_PERF_SERIES),
                 dict(imm.SCAN_PERF_FAMILIES), imm._scan_perf_fiscal_lookup)
        self.addCleanup(self._restore_bot, imm, saved)
        imm.SCAN_PERF_FILE = path
        imm._scan_perf_state["mtime"] = 0.0
        imm.set_scan_perf_fiscal_lookup(None)
        n = imm.load_scan_perf()
        return n, (dict(imm.SCAN_PERF_EVENTS), dict(imm.SCAN_PERF_SERIES),
                   dict(imm.SCAN_PERF_FAMILIES))

    @staticmethod
    def _restore_bot(imm, saved):
        f, state, evt, ser, fam, hook = saved
        if f is not None:
            imm.SCAN_PERF_FILE = f
        imm._scan_perf_state.clear()
        imm._scan_perf_state.update(state)
        for live, old in ((imm.SCAN_PERF_EVENTS, evt),
                          (imm.SCAN_PERF_SERIES, ser),
                          (imm.SCAN_PERF_FAMILIES, fam)):
            live.clear()
            live.update(old)
        imm.set_scan_perf_fiscal_lookup(hook)

    def _assert_zero_rejects(self, imm, table, loaded, n, *, where):
        """ZERO REJECTS: every unit record in the file is present in the
        bot's in-memory tables afterwards, every family match rule compiled,
        and every key resolves under the bot's own rules."""
        events, series, families = loaded
        self.assertTrue(events or series or families,
                        f"{where}: bot loader refused the table whole "
                        f"(load_scan_perf returned {n})")
        n_block = sum(1 for blk in ("events", "series", "families")
                      for r in table[blk].values() if r["verdict"] == "block")
        self.assertLessEqual(n_block, imm.SCAN_PERF_MAX_BLOCKED,
                             f"{where}: over the reader cap, which truncates")
        self.assertEqual(set(events), set(table["events"]), f"{where}: event roots dropped")
        self.assertEqual(set(series), set(table["series"]), f"{where}: series dropped")
        self.assertEqual(set(families), set(table["families"]), f"{where}: families dropped")
        self.assertEqual(n, len(events) + len(series) + len(families))
        self.assertFalse(imm._scan_perf_state["stale"])
        for blk, live in (("events", events), ("series", series), ("families", families)):
            for k, rec in table[blk].items():
                self.assertEqual(live[k]["verdict"], rec["verdict"], f"{where}: {blk}:{k}")
                self.assertAlmostEqual(live[k]["roi_hist"], rec["roi_hist"], places=6)
                self.assertAlmostEqual(live[k]["n"], rec["risk_days"], places=3)
        # the event ROOT: derived the same way by both units
        for k, rec in table["events"].items():
            for dated in rec.get("events", []):
                self.assertEqual(imm.scan_perf_event_root(dated), k,
                                 f"{where}: {dated} -> "
                                 f"{imm.scan_perf_event_root(dated)} != {k}")
        # family match rules: every emitted member resolves to its family
        for name, rec in table["families"].items():
            if rec.get("match", {}).get("regex"):
                self.assertIsNotNone(families[name]["regex_c"])
            for member in rec.get("members", []):
                self.assertEqual(imm.scan_perf_family_of(member), name,
                                 f"{where}: {member} does not resolve to {name}")

    def test_written_file_validates_against_the_bot_loader(self):
        """A table the scorer has just written from synthetic sinks is
        accepted by imm.load_scan_perf() with ZERO rejects, and a unit the
        scorer blocked is a unit the bot blocks."""
        imm = self._bot()
        # The loader ages a table out at 48 h, so the fixture must sit in the
        # real present, not on the synthetic 2026-09-06 timeline.
        now = datetime.now(UTC).replace(microsecond=0)
        start = now - timedelta(days=2)
        for i in range(3):
            self.simple_market(f"KXR{i}-26SEP20-T1", start=start, hours=6,
                               own_bid_ct=2000, pool=5000.0)
        loser = "KXLOSE-26SEP20-T1"
        self.simple_market(loser, start=start, hours=6, own_bid_ct=2000, pool=30.0)
        self.s.fill(start + timedelta(hours=1), loser, 10, px=40)
        self.s.quotes(loser, start + timedelta(hours=6), 2, bid=9, ask=11,
                      own_bid_ct=2000)
        rc, _ = self.run_cli(asof=now)
        self.assertEqual(rc, 0)
        path = os.path.join(self.wd, M.TABLE_NAME)
        with open(path, encoding="utf-8") as f:
            table = json.load(f)
        self.assertEqual(table["series"]["KXLOSE"]["verdict"], "block")
        self.assertEqual(table["series"]["KXR0"]["verdict"], "allow")
        n, loaded = self._load_into_bot(imm, path)
        self._assert_zero_rejects(imm, table, loaded, n, where="synthetic")
        self.assertEqual(imm.scan_perf_blocked("KXLOSE", "KXLOSE-26SEP27"),
                         "event:KXLOSE")
        self.assertIsNone(imm.scan_perf_blocked("KXR0", "KXR0-26SEP27"))

    def test_a_real_scorer_output_validates_against_the_bot_loader(self):
        """LIVE half. Synthetic sinks cannot produce a hundred series, a
        family with a dozen members, or an event root with several dated
        siblings at once — every one of those is a place the two units could
        disagree. Point IMM_SCAN_PERF_TEST_TABLE at a table the scorer
        produced from the real sinks:

            IMM_SCAN_PERF_TEST_TABLE=<dir>/scan_perf.json \\
                python -m unittest test_imm_scan_perf

        Skipped when unset, so the suite stays hermetic: no test in this
        module ever reads run-logs/incentive-mm. ``generated_at`` is
        re-stamped to now in a COPY before loading (age is a wall-clock
        property, not a schema one; the staleness path has its own test)."""
        imm = self._bot()
        src = os.environ.get("IMM_SCAN_PERF_TEST_TABLE", "")
        if not src or not os.path.exists(src):
            self.skipTest("IMM_SCAN_PERF_TEST_TABLE unset or missing — see "
                          "the docstring for the one-liner")
        with open(src, encoding="utf-8") as f:
            table = json.load(f)
        table["generated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for blk in ("events", "series", "families"):
            for rec in table[blk].values():
                if rec.get("verdict") == "block":
                    rec["until"] = (datetime.now(UTC) + timedelta(hours=60)
                                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = os.path.join(self.wd, M.TABLE_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(table, f)
        n, loaded = self._load_into_bot(imm, path)
        self._assert_zero_rejects(imm, table, loaded, n, where="live")
        self.assertEqual(table["version"], 2)
        self.assertTrue(any(len(r.get("members") or []) > 1
                            for r in table["families"].values()),
                        "no multi-member family in this artefact")


    # -- LEAD RULING R5: the two integration guard fixes, pinned -----------


# ================================================ fixer-added invariants


class TestScorerInvariants(Base):

    def test_event_root_matches_the_bots_rule_exactly(self):
        """The scorer EMITS the `events` block and the bot LOOKS UP in it, so
        the two must derive the key by the same rule or the scorer publishes
        a key the bot can never find. rsplit('-', 1) agreed on the easy cases
        and disagreed on KXVOTEGENERAL-HOUSECO3-26CBRO (574 of 1,455 scan
        candidates over the last 20 selection_events files)."""
        try:
            import incentive_mm as imm
        except Exception as e:                       # pragma: no cover
            raise unittest.SkipTest(f"incentive_mm not importable: {e}")
        for ev in ("KXCPIYOY-26NOV", "KXAXP-26OCTCARDS",
                   "KXHYPEMINMON-HYPE-26JUL31", "KXVOTEGENERAL-HOUSECO3-26CBRO",
                   "KXMLBPLAYOFFS", "KXNOVEL-99DEC31", "KXWEEKSNUM1-26SEP21",
                   "KXBNBMAXMON-BNB-26JUL31"):
            self.assertEqual(M.root_of(ev), imm.scan_perf_event_root(ev), ev)

    def test_event_ticker_comes_from_the_sink_not_a_string_guess(self):
        """The fills and selection_events sinks both carry the bot's own
        `event_ticker`; ev_of's two-segment guess is the fallback only. A
        wrong event here misses that event's ledger rows AND publishes an
        `events` key under the wrong root."""
        tkr = "KXVOTEGENERAL-HOUSECO3-26CBRO-7"
        self.simple_market(tkr, hours=6, event="KXVOTEGENERAL-HOUSECO3-26CBRO")
        sc = self.build()
        self.assertEqual(sc.ev(tkr), "KXVOTEGENERAL-HOUSECO3-26CBRO")
        self.assertNotEqual(sc.ev(tkr), M.ev_of(tkr))
        self.assertEqual(M.root_of(sc.ev(tkr)), "KXVOTEGENERAL-HOUSECO3")
        # and an unseen ticker still falls back rather than raising
        self.assertEqual(sc.ev("KXNEVERSEEN-26SEP20-T1"),
                         "KXNEVERSEEN-26SEP20")


    def test_a_qualified_period_with_no_ledger_row_stays_modelled(self):
        """$0.00 is NOT evidence of a credit. A period that qualifies on age
        but whose ledger rows fall outside its window has no MEASURED term;
        stamping it `credited` and zeroing the estimate produced records
        reading `rent_basis: mixed_by_period` next to `rent_measured_frac:
        0.0` next to a non-zero `credited_measured` -- three statements about
        one event that cannot all be true, and an invitation to add EST and
        CREDITED."""
        tkr = "KXNOCR-26SEP20-T1"
        self.simple_market(tkr, hours=10, pool=5000.0)
        self.s.program(tkr, T0 - timedelta(days=3), T0 + timedelta(hours=5))
        # a credit for a DIFFERENT event, dated late enough that `newest`
        # makes this market's period age-qualify
        self.s.credit("2026-09-09", "KXOTHER-26SEP20", 3.0)
        sc = self.build()
        self.assertEqual({p["basis"] for p in sc.market_rent_periods[tkr]},
                         {"est_floored"})
        rec = sc.series_recs["KXNOCR"]
        self.assertEqual(rec["rent_basis"], "est_floored")
        self.assertEqual(rec["rent_measured_dollars"], 0.0)

    def test_no_record_claims_a_credited_basis_with_a_zero_measured_term(self):
        """The invariant the three contradictory fields above violated: a
        basis other than `est_floored` means a MEASURED credit actually
        replaced an estimate, so rent_measured_dollars must be non-zero."""
        tkr = "KXCR2-26SEP20-T1"
        self.simple_market(tkr, hours=10, pool=5000.0)
        self.s.program(tkr, T0 - timedelta(days=3), T0 + timedelta(hours=5))
        self.s.credit("2026-09-07", M.ev_of(tkr), 7.25)
        self.s.credit("2026-09-09", "KXOTHER-26SEP20", 3.0)
        sc = self.build()
        tbl = sc.table()
        recs = list(tbl["series"].values()) + list(tbl["events"].values())
        self.assertTrue(recs)
        for r in recs:
            if r["rent_basis"] != "est_floored":
                self.assertGreater(
                    r["rent_measured_dollars"], 0.0,
                    f"rent_basis {r['rent_basis']} with a $0 measured term")
        # the pseudo-period before the program start can never be credited
        for p in sc.market_rent_periods[tkr]:
            if p["basis"] == "credited":
                self.assertTrue(p["lo"] > -1e308)

    def test_a_flat_through_fill_is_not_counted_as_unmarked(self):
        """`unmarked/n_fills` is the input to the data_thin rule and to bar
        condition 4. A fill whose position did not move carries no direction
        -- there is nothing to mark -- so booking it as `unmarked` put the
        one thing that CAN raise that ratio into a denominator that names
        something else."""
        tkr = "KXFLAT-26SEP20-T1"
        t_fill = T0 + timedelta(hours=1)
        self.simple_market(tkr, hours=6)
        self.s.quotes(tkr, t_fill + timedelta(hours=24) - timedelta(seconds=30),
                      2, bid=29, ask=31)
        self.s.fill(t_fill, tkr, 10, px=40, pos_before=5.0, pos_after=5.0)
        sc = self.build()
        self.assertEqual(sc.cov["fills_unmarked"], 0)
        self.assertEqual(sc.cov["fills_flat_through"], 1)
        row = next(r for r in sc.fill_rows if r["ticker"] == tkr)
        self.assertEqual(row["status"], "flat_through")

    def test_each_markout_horizon_has_its_own_maturity_and_sample_size(self):
        """SPEC 10.3 pre-registers 1h/24h/72h baselines, so the three numbers
        printed on one line labelled MEASURED must be the SAME statistic at
        three horizons. The first cut computed 1h and 72h inside the 24h
        loop, conditioning both on the fill already having matured AND been
        markable at 24h, and winsorised/episode-collapsed neither."""
        tkr = "KXHZ-26SEP20-T1"
        t_fill = T0 + timedelta(hours=1)
        self.simple_market(tkr, hours=6)
        for h in (1, 24, 72):
            self.s.quotes(tkr, t_fill + timedelta(hours=h)
                          - timedelta(seconds=30), 2, bid=49 + h, ask=51 + h)
        self.s.fill(t_fill, tkr, 10, px=40)
        # a SECOND fill that has matured at 1h but not at 72h
        late = ASOF - timedelta(hours=30)
        self.s.quotes(tkr, late + timedelta(hours=1) - timedelta(seconds=30),
                      2, bid=39, ask=41)
        self.s.fill(late, tkr, 10, px=40, pos_before=10.0)
        sc = self.build()
        hz = sc.markout_horizons
        self.assertEqual(set(hz), {1, 24, 72})
        self.assertGreater(hz[1]["n_fills"], hz[72]["n_fills"],
                           "the 1h sample must not be conditioned on 72h "
                           "maturity")
        for h in (1, 24, 72):
            self.assertLessEqual(abs(hz[h]["c_per_ct"]), M.WINSOR_CENTS + 1e-9)
            self.assertGreaterEqual(hz[h]["n_episodes"], 0)
        # the 24h entry reproduces the acting figure by the same path
        tbl = sc.table()
        self.assertAlmostEqual(hz[24]["c_per_ct"],
                               tbl["tier"]["markout_c_per_ct_24h"], places=3)


class TestGuardsLeaveTheLastGoodTable(Base):
    """Every abort path prints 'NO FILE WRITTEN'. The three guard tests start
    from an EMPTY work dir and assert the table was never created, which a
    write-then-validate refactor would also satisfy. What actually matters on
    the box is that the PREVIOUS GOOD TABLE is still there, byte for byte,
    and that no history row was appended."""

    def _corpus(self):
        for i in range(3):
            self.simple_market(f"KXA{i}-26SEP20-T1", hours=6, bid=40, ask=42)
        self.s.fill(T0 + timedelta(hours=1), "KXA0-26SEP20-T1", 10, px=40)
        self.s.quotes("KXA0-26SEP20-T1", T0 + timedelta(hours=25)
                      - timedelta(seconds=30), 2, bid=29, ask=31)

    def _snapshot(self):
        with open(os.path.join(self.wd, M.TABLE_NAME), "rb") as f:
            tbl = f.read()
        with open(os.path.join(self.wd, M.HISTORY_NAME), "rb") as f:
            hist = f.read()
        return tbl, hist

    def _assert_unchanged(self, before):
        self.assertEqual(self._snapshot(), before,
                         "an aborted run changed the last good table or "
                         "appended a history row")

    def test_reconciliation_abort_leaves_the_previous_table_untouched(self):
        self._corpus()
        rc, _ = self.run_cli()
        self.assertEqual(rc, 0)
        before = self._snapshot()
        self.s.extra_realized.append({
            "ts": (T0 + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ticker": "KXA0-26SEP20-T1", "series": "KXA0",
            "event_ticker": "KXA0-26SEP20", "realized_delta_dollars": -2.0,
            "realized_total_dollars": -2.0, "pos_after": 0.0, "avg_after": 0.0})
        rc2, txt = self.run_cli()
        self.assertEqual(rc2, 2)
        self.assertIn("NO FILE WRITTEN", txt)
        self._assert_unchanged(before)

    def test_exposure_abort_leaves_the_previous_table_untouched(self):
        self._corpus()
        rc, _ = self.run_cli()
        self.assertEqual(rc, 0)
        before = self._snapshot()
        M.WORK_DIR = self.wd
        M.append_jsonl(os.path.join(self.wd, M.HISTORY_NAME),
                       {"generated_at": "2026-09-09T07:55:00Z", "risk_days": 1e6})
        after_row = self._snapshot()
        rc2, txt = self.run_cli()
        self.assertEqual(rc2, 3)
        self.assertIn("NO FILE WRITTEN", txt)
        self._assert_unchanged(after_row)


# =============================================================== the rule

class TestUnitVerdicts(Base):
    """Jack 2026-09-18: "make open scan pick up new markets or not based on
    historical ROI of those market families." Jack 2026-09-20: "If it's a
    new event listed then use the family historical ROI but if it's the same
    event that is just relisted for new dates (eg gas dailies, AI token
    share, etc) then use the specific ROI of that event, not the family
    historical ROI." These pin the scorer's half of that rule: the three
    nested units, their verdicts, and the arithmetic behind roi_hist."""

    def _heavy(self, ticker, *, hours=8, pool=5000.0, event=None, start=None,
               losing=False, own_bid_ct=2000):
        """A market with well over MIN_RISK_DAYS of history ($800 resting
        for 8h = ~267 $-days). `losing` adds a 10-lot fill at 40 marked at
        10 afterwards (-$3 of MTM against ~$0 of floored rent)."""
        start = start or T0
        self.simple_market(ticker, start=start, hours=hours, own_bid_ct=own_bid_ct,
                           pool=pool, event=event)
        if losing:
            self.s.fill(start + timedelta(hours=1), ticker, 10, px=40)
            self.s.quotes(ticker, start + timedelta(hours=hours), 2, bid=9, ask=11,
                          own_bid_ct=own_bid_ct, pool=pool)
        return ticker

    def test_verdict_thresholds_insufficient_block_allow(self):
        """Under MIN_RISK_DAYS -> insufficient (the bot treats the unit as
        unknown); at or above it, roi_hist < BLOCK_ROI -> block (with a TTL),
        else allow."""
        self._heavy("KXPOS-26SEP20-T1")                       # earns rent, no fills
        self._heavy("KXNEG-26SEP20-T1", pool=30.0, losing=True)   # loses, no rent
        self.simple_market("KXTHIN-26SEP20-T1", hours=1, own_bid_ct=20)
        sc = self.build()
        pos, neg, thin = (sc.series_recs["KXPOS"], sc.series_recs["KXNEG"],
                          sc.series_recs["KXTHIN"])
        self.assertGreaterEqual(pos["risk_days"], M.MIN_RISK_DAYS)
        self.assertEqual(pos["verdict"], "allow")
        self.assertGreater(pos["roi_hist"], 0.0)
        self.assertNotIn("until", pos)
        self.assertEqual(neg["verdict"], "block")
        self.assertLess(neg["roi_hist"], M.BLOCK_ROI)
        self.assertIn("until", neg)
        self.assertLess(M.parse_iso(neg["until"]) - ASOF,
                        timedelta(hours=M.BLOCK_TTL_HOURS + 1))
        self.assertIn("roi_hist", neg["reason"])
        self.assertLess(thin["risk_days"], M.MIN_RISK_DAYS)
        self.assertEqual(thin["verdict"], "insufficient")
        self.assertNotIn("until", thin)
        # the arithmetic is the one the docstring states, on the record itself
        for r in (pos, neg):
            self.assertAlmostEqual(
                r["roi_hist"],
                M.clip((r["rent_used"] + r["realized_dollars"] + r["mtm_dollars"])
                       / r["risk_days"], M.ROI_CLIP_LO, M.ROI_CLIP_HI), places=5)
            self.assertAlmostEqual(
                r["roi_hist_trading"],
                M.clip((r["realized_dollars"] + r["mtm_dollars"]) / r["risk_days"],
                       M.ROI_CLIP_LO, M.ROI_CLIP_HI), places=5)

    def test_relisted_dates_share_one_event_root_and_new_roots_split(self):
        """KXGAS-26SEP07 and KXGAS-26SEP08 are the same event re-listed: ONE
        root record carrying both dated events. KXV-NORTH-26OCT01 and
        KXV-SOUTH-26OCT01 are two roots under one series."""
        self._heavy("KXGAS-26SEP07-T1", event="KXGAS-26SEP07")
        self._heavy("KXGAS-26SEP08-T1", event="KXGAS-26SEP08",
                    start=T0 + timedelta(days=1))
        self._heavy("KXV-NORTH-26OCT01-T1", event="KXV-NORTH-26OCT01")
        self._heavy("KXV-SOUTH-26OCT01-T1", event="KXV-SOUTH-26OCT01")
        sc = self.build()
        gas = sc.event_recs["KXGAS"]
        self.assertEqual(gas["n_events"], 2)
        self.assertEqual(gas["events"], ["KXGAS-26SEP07", "KXGAS-26SEP08"])
        self.assertEqual(gas["n_markets"], 2)
        self.assertEqual(sc.series_recs["KXGAS"]["risk_days"], gas["risk_days"])
        self.assertEqual(sc.series_recs["KXGAS"]["roots"], ["KXGAS"])
        self.assertIn("KXV-NORTH", sc.event_recs)
        self.assertIn("KXV-SOUTH", sc.event_recs)
        self.assertEqual(sc.series_recs["KXV"]["n_markets"], 2)
        self.assertEqual(sc.series_recs["KXV"]["roots"], ["KXV-NORTH", "KXV-SOUTH"])
        self.assertEqual(sc.event_recs["KXV-NORTH"]["series"], "KXV")

    def test_family_grouping_by_regex_members_and_fiscal_flag(self):
        """FAMILY_GROUPS: the CPI prints group by name, the Fiscal.ai KPIs by
        the persisted `fiscal` flag; everything else is its own family. The
        match rules ride in the file so the bot classifies a never-seen
        series the same way."""
        self._heavy("KXCPI-26OCT-T1")
        self._heavy("KXCPIYOY-26NOV-T1", start=T0 + timedelta(days=1))
        self._heavy("KXAXP-26OCTCARDS-T1")
        self._heavy("KXAAAGASDTX-26SEP20-T1")
        self._heavy("KXOTHER-26SEP20-T1")
        self.s.state["scan_series_meta"]["KXAXP"] = {"ok": True, "fiscal": True}
        sc = self.build()
        self.assertEqual(sc.family_of("KXCPI"), "CPI")
        self.assertEqual(sc.family_of("KXCPIYOY"), "CPI")
        self.assertEqual(sc.family_of("KXCPICORE"), "CPI")
        self.assertEqual(sc.family_of("KXAXP"), "FISCAL_KPI")
        self.assertEqual(sc.family_of("KXAAAGASDTX"), "AAAGAS_STATE_DAILY")
        self.assertEqual(sc.family_of("KXAAAGASD"), "KXAAAGASD")     # the national daily is not a state
        self.assertEqual(sc.family_of("KXOTHER"), "KXOTHER")
        cpi = sc.family_recs["CPI"]
        self.assertEqual(cpi["members"], ["KXCPI", "KXCPIYOY"])
        self.assertEqual(cpi["n_markets"], 2)
        self.assertEqual(cpi["match"], {"regex": r"^KXCPI(CORE)?(YOY)?$"})
        self.assertEqual(sc.family_recs["FISCAL_KPI"]["members"], ["KXAXP"])
        self.assertEqual(sc.family_recs["FISCAL_KPI"]["match"], {"fiscal": True})
        self.assertNotIn("KXOTHER", {m for r in sc.family_recs.values()
                                      for m in r["members"]})

    def test_lookup_unit_prefers_the_most_specific_unit_with_history(self):
        """Root with history -> the root. Thin root under a series with
        history -> the series. Thin series inside a family with history ->
        the family. Nothing with history -> nothing."""
        self._heavy("KXV-NORTH-26OCT01-T1", event="KXV-NORTH-26OCT01")
        self.simple_market("KXV-SOUTH-26OCT01-T1", hours=1, own_bid_ct=20,
                           event="KXV-SOUTH-26OCT01")
        self._heavy("KXCPIYOY-26NOV-T1")
        self.simple_market("KXCPI-26OCT-T1", hours=1, own_bid_ct=20)
        self.simple_market("KXLONE-26SEP20-T1", hours=1, own_bid_ct=20)
        sc = self.build()
        self.assertEqual(sc.lookup_unit("KXV-NORTH-26OCT01-T1")[:2], ("event", "KXV-NORTH"))
        self.assertEqual(sc.lookup_unit("KXV-SOUTH-26OCT01-T1")[:2], ("series", "KXV"))
        self.assertEqual(sc.lookup_unit("KXCPI-26OCT-T1")[:2], ("family", "CPI"))
        self.assertEqual(sc.lookup_unit("KXLONE-26SEP20-T1"), (None, None, None))

    def test_roi_hist_uses_the_credited_rent_where_the_ledger_covers_the_period(self):
        """rent_used is ONE basis per period: the MEASURED credit replaces the
        floored estimate for a covered period and is never added to it; the
        replaced amount is published beside the basis."""
        tkr = "KXCRED-26SEP20-T1"
        self.simple_market(tkr, hours=10, pool=5000.0, own_bid_ct=2000)
        self.s.program(tkr, T0 - timedelta(days=3), T0 + timedelta(hours=5))
        self.s.credit("2026-09-07", M.ev_of(tkr), 7.25)
        self.s.credit("2026-09-09", "KXOTHER-26SEP20", 3.0)
        sc = self.build()
        rec = sc.series_recs["KXCRED"]
        periods = sc.market_rent_periods[tkr]
        est_side = sum(p["floored"] for p in periods if p["basis"] != "credited")
        self.assertAlmostEqual(rec["rent_measured_dollars"], 7.25)
        self.assertAlmostEqual(rec["rent_used"], 7.25 + est_side, places=4)
        self.assertGreater(rec["rent_modelled"], rec["rent_used"] - 7.25)
        self.assertIn(rec["rent_basis"], ("credited", "mixed_by_period"))
        self.assertAlmostEqual(rec["roi_hist_measured"],
                               M.clip((7.25 + rec["realized_dollars"]) / rec["risk_days"],
                                      M.ROI_CLIP_LO, M.ROI_CLIP_HI), places=5)

    def test_report_lists_blocks_once_with_their_ttl_and_the_counterfactual(self):
        """The printed report: a root that IS its series prints once (as the
        series), every block shows its `until`, the blocks line counts each
        kind, and the counterfactual carries its label."""
        self._heavy("KXNEG-26SEP20-T1", pool=30.0, losing=True)
        self._heavy("KXPOS-26SEP20-T1")
        rc, txt = self.run_cli(write=False)
        self.assertEqual(rc, 0)
        neg_rows = [ln for ln in txt.splitlines()
                    if ln.lstrip().startswith(("event ", "series ", "family "))
                    and "KXNEG" in ln]
        self.assertEqual(len(neg_rows), 1, neg_rows)
        self.assertIn("until", neg_rows[0])
        self.assertIn("series  KXNEG", neg_rows[0])
        self.assertIn("blocks   : 1 event root(s), 1 series, 0 family(ies)", txt)
        self.assertIn("counterfactual [IN-SAMPLE, MODELLED COUNTERFACTUAL]: 1 of 2 "
                      "markets", txt)

    def test_insufficient_records_are_shed_first_when_over_max_records(self):
        """The bot refuses a file over its record cap WHOLE, so the writer
        bounds the count by shedding `insufficient` records — no-ops for the
        bot — smallest history first, and says so."""
        for i in range(5):
            self.simple_market(f"KXT{i}-26SEP20-T1", hours=1, own_bid_ct=20)
        old = M.MAX_RECORDS
        self.addCleanup(setattr, M, "MAX_RECORDS", old)
        M.MAX_RECORDS = 4
        rc, txt = self.run_cli()
        self.assertEqual(rc, 0)
        tbl = self.read_table()
        self.assertLessEqual(tbl["limits"]["n_units"], 4)
        self.assertTrue(any("MAX_RECORDS" in w for w in tbl["warnings"]), tbl["warnings"])
        self.assertEqual(set(tbl["families"]), {g["name"] for g in M.FAMILY_GROUPS})

    def test_explain_resolves_a_root_a_series_a_family_or_a_ticker(self):
        self._heavy("KXCPIYOY-26NOV-T1")
        self.s.write()
        for key in ("KXCPIYOY", "CPI", "KXCPIYOY-26NOV-T1"):
            buf = io.StringIO()
            rc = M.run(["--status-dir", self.sd, "--work-dir", self.wd,
                        "--asof", ASOF.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "--explain", key], out=buf)
            self.assertEqual(rc, 0, key)
            self.assertIn("roi_hist", buf.getvalue(), key)
            self.assertIn("KXCPIYOY-26NOV-T1", buf.getvalue(), key)
        self.assertFalse(os.path.exists(os.path.join(self.wd, M.TABLE_NAME)))


if __name__ == "__main__":
    unittest.main()
