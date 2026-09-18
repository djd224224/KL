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
              spread=2, pool=30.0):
        self.selection.append({
            "ticker": ticker, "decision": "selected", "prev": None,
            "series": M.ser_of(ticker), "event_ticker": M.ev_of(ticker),
            "est_dollars_per_day": per_day, "est_collateral_dollars": collateral,
            "dollars_per_day": pool, "mid_cents": mid, "spread_cents": spread,
            "is_scan": True, "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")})
        self.state["scan_book"].append(ticker)
        ev = M.ev_of(ticker)
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
        self._old = (M.STATUS_DIR, M.WORK_DIR, M.DIM_CLIP, M.BAR_MIN_EPISODES)

    def tearDown(self):
        (M.STATUS_DIR, M.WORK_DIR, M.DIM_CLIP, M.BAR_MIN_EPISODES) = self._old
        os.environ.pop("IMM_SCAN_PERF_BAR", None)
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
                      own_bid_ct=20, pool=30.0, **kw):
        start = start or T0
        self.s.admit(start, ticker, pool=pool, mid=(bid + ask) / 2.0,
                     spread=ask - bid)
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
        self.assertEqual(src["KXTWO-26SEP20-T1"]["source"], "two_sided")
        self.assertAlmostEqual(src["KXTWO-26SEP20-T1"]["mark"], 62.0)
        self.assertEqual(src["KXONE-26SEP20-T1"]["source"], "one_sided")
        self.assertEqual(src["KXNIL-26SEP20-T1"]["source"], "unmarked")
        self.assertIsNone(src["KXNIL-26SEP20-T1"]["mo"])
        self.assertEqual(sc.market_mo.get("KXNIL-26SEP20-T1", 0.0), 0.0)

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
        self.s.cycle(t_mark - timedelta(seconds=600), tkr, bid=35, ask=65)
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
        self.assertEqual(sc.cov["episodes"], 0)
        self.assertEqual(sc.market_mo.get(tkr, 0.0), 0.0)
        row = next(r for r in sc.fill_rows if r["ticker"] == tkr)
        self.assertEqual(row["status"], "pending")


# ===================================================================== 32-36

class TestGroupsAndExposure(Base):

    def _thin_series(self):
        """3 markets, 4 matured fills, 3 of them UNMARKED (75% > 35%)."""
        t_fill = T0 + timedelta(hours=1)
        for i in range(3):
            tkr = f"KXTHIN-26SEP2{i}-T1"
            self.simple_market(tkr, hours=2)
            self.s.fill(t_fill, tkr, 10, px=40)
        # only the first gets a mark at the horizon
        self.s.quotes("KXTHIN-26SEP20-T1", t_fill + timedelta(hours=24)
                      - timedelta(seconds=30), 2, bid=9, ask=11)
        self.s.fill(t_fill + timedelta(hours=2), "KXTHIN-26SEP20-T1", 10, px=40,
                    pos_before=10.0)

    def test_data_thin_group_is_forced_neutral_and_can_never_be_barred(self):
        """SPEC 2.4: any group over 35% unmarked is forced to
        verdict "neutral" / cohort "data_thin" and can never be barred —
        66% of the acting signal must not be a mark on a book the tier itself
        measures as dead."""
        self._thin_series()
        sc = self.build()
        rec = sc.series_recs["KXTHIN"]
        self.assertEqual(rec["cohort"], "data_thin")
        self.assertEqual(rec["verdict"], "neutral")
        self.assertEqual(rec["dev"], 0.0)
        self.assertNotIn("until", rec)

    def test_bucket_is_muted_below_five_series_or_three_events(self):
        """SPEC 3.4 mute rules: < 5 distinct series or < 3 distinct events."""
        for i in range(6):                       # 6 series, 6 events, 1c books
            self.simple_market(f"KXW{i}-26SEP20-T1", bid=40, ask=41, hours=3)
        for i in range(2):                       # 2 series only -> muted
            self.simple_market(f"KXN{i}-26SEP20-T1", bid=30, ask=45, hours=3)
        sc = self.build()
        by_key = {b["key"]: b for b in sc.cohorts["spread"]["buckets"]}
        self.assertFalse(by_key["1c"]["muted"])
        self.assertGreaterEqual(by_key["1c"]["n_series"], 5)
        self.assertTrue(by_key["10-19c"]["muted"])
        self.assertEqual(by_key["10-19c"]["dev_trading"], 0.0)

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
        self.assertGreater(rec["rent_measured_frac"], 0.0)
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
        table modulo generated_at."""
        self._corpus()
        rc1, _ = self.run_cli()
        with open(os.path.join(self.wd, M.TABLE_NAME), encoding="utf-8") as f:
            first = f.read()
        rc2, _ = self.run_cli()
        with open(os.path.join(self.wd, M.TABLE_NAME), encoding="utf-8") as f:
            second = f.read()
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)

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

    def test_scorer_refuses_to_write_on_a_clamp_storm(self):
        """SPEC 4.4 guard 3: a uniformly negated file is individually in
        range and passes every NaN/schema check — the clamp storm is the
        sign-flip detector."""
        # TWO groups of 6 series each, in different spread/mid/dtc/pool
        # buckets and with opposite markouts, so every unmuted bucket has a
        # real (non-zero) deviation to be clamped.
        t_fill = T0 + timedelta(hours=1)
        t_mark = t_fill + timedelta(hours=24)
        groups = [("A", 40, 41, 40.0, 70, "26SEP20", 30.0),
                  ("B", 15, 22, 18.0, 5, "26DEC20", 10.0)]
        for tag, bid, ask, px, mark, dt, pool in groups:
            for i in range(6):
                tkr = f"KX{tag}{i}-{dt}-T1"
                self.simple_market(tkr, hours=6, bid=bid, ask=ask, pool=pool)
                self.s.fill(t_fill, tkr, 10, px=px)
                self.s.quotes(tkr, t_mark - timedelta(seconds=30), 2,
                              bid=mark - 1, ask=mark + 1)
        M.DIM_CLIP = 1e-4          # force every dev onto a clamp
        rc, txt = self.run_cli()
        self.assertEqual(rc, 4)
        self.assertIn("CLAMP STORM", txt)
        self.assertFalse(os.path.exists(os.path.join(self.wd, M.TABLE_NAME)))

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


# ===================================================================== 43-48

class TestVerdictsAndBases(Base):

    def _bar_corpus(self, n_events=4, n_strikes=3, mark=20, spread_events=True):
        """>=30 episodes over >=3 markets and >=2 days, all adverse."""
        for e in range(n_events):
            for k in range(n_strikes):
                tkr = f"KXBAR-26SEP2{e}-T{k}"
                self.simple_market(tkr, hours=6, bid=40, ask=42)
                for h in range(3):
                    day = T0 + timedelta(days=h % 2)
                    t_fill = day + timedelta(hours=1 + h)
                    self.s.fill(t_fill, tkr, 10, px=40,
                                pos_before=10.0 * h)
                    self.s.quotes(tkr, t_fill + timedelta(hours=24)
                                  - timedelta(seconds=30), 2,
                                  bid=mark - 1, ask=mark + 1)

    def test_bar_requires_thirty_episodes_three_markets_and_a_negative_ci(self):
        """SPEC 3.6: n_episodes >= 30 (the ratified section-6 floor,
        re-expressed in EPISODE units — a TIGHTENING, recorded as a
        reinterpretation), n_markets >= 3, n_days >= 2, a negative one-sided
        90% bootstrap upper bound and the same verdict on the previous run."""
        os.environ["IMM_SCAN_PERF_BAR"] = "1"
        self._bar_corpus()
        sc = self.build()
        rec = sc.series_recs["KXBAR"]
        self.assertGreaterEqual(rec["n_episodes"], 30)
        self.assertGreaterEqual(rec["n_markets"], 3)
        self.assertGreaterEqual(rec["n_days"], 2)
        self.assertIsNotNone(rec["ci_hi"])
        self.assertLess(rec["ci_hi"], 0.0)
        self.assertTrue(rec["bar_eligible"])
        # run 1: eligible but not sustained -> down_rank only
        self.assertEqual(rec["verdict"], "down_rank")
        rc1, _ = self.run_cli()
        self.assertEqual(rc1, 0)
        rc2, _ = self.run_cli()
        self.assertEqual(rc2, 0)
        tbl = self.read_table()
        self.assertEqual(tbl["series"]["KXBAR"]["verdict"], "bar")
        self.assertIn("until", tbl["series"]["KXBAR"])

    def test_a_ci_straddling_zero_never_bars(self):
        """SPEC 3.6 condition 3: 40 episodes are not enough if the interval
        covers 0. 'do not claim an effect the CI does not support'."""
        os.environ["IMM_SCAN_PERF_BAR"] = "1"
        # alternate favourable / adverse marks -> the CI straddles zero
        for e in range(4):
            for k in range(3):
                tkr = f"KXMIX-26SEP2{e}-T{k}"
                self.simple_market(tkr, hours=6, bid=40, ask=42)
                mark = 20 if (e + k) % 2 else 60
                for h in range(3):
                    day = T0 + timedelta(days=h % 2)
                    t_fill = day + timedelta(hours=1 + h)
                    self.s.fill(t_fill, tkr, 10, px=40, pos_before=10.0 * h)
                    self.s.quotes(tkr, t_fill + timedelta(hours=24)
                                  - timedelta(seconds=30), 2,
                                  bid=mark - 1, ask=mark + 1)
        sc = self.build()
        rec = sc.series_recs["KXMIX"]
        self.assertGreaterEqual(rec["n_episodes"], 30)
        self.assertFalse(rec["bar_eligible"])
        self.assertNotEqual(rec["verdict"], "bar")

    def test_bootstrap_is_clustered_by_market(self):
        """SPEC test 44: three strikes of one event are ONE draw.

        DEVIATION, recorded: SPEC 3.6's prose says "clustered by market" while
        this test defines the cluster as the EVENT; both cannot hold. The
        event is taken because three strikes reprice on the same print
        (KXCPIYOY's three strikes all moved on the same September CPI) and the
        coarser cluster gives the WIDER interval, i.e. it bars LESS often."""
        self._bar_corpus(n_events=1, n_strikes=3)
        sc = self.build()
        tickers = [t for t in sc.markets if t.startswith("KXBAR")]
        self.assertEqual(len({M.ev_of(t) for t in tickers}), 1)
        # one cluster -> every resample is the same draw -> a degenerate CI
        ci = sc._bootstrap_ci_hi(tickers, 0.0, "t")
        self.assertIsNone(ci)
        self._bar_corpus(n_events=3, n_strikes=3)
        sc2 = self.build()
        t2 = [t for t in sc2.markets if t.startswith("KXBAR")]
        self.assertIsNotNone(sc2._bootstrap_ci_hi(t2, 0.0, "t"))

    def test_ratchet_eases_by_at_most_one_cent_per_run_but_deepens_immediately(self):
        """SPEC 3.7: tightening on realized pain stays immediate and
        asymmetric; relaxing is bounded at RATCHET_EASE_PER_RUN per run."""
        self._bar_corpus(n_events=3, n_strikes=3)
        rc, _ = self.run_cli()
        self.assertEqual(rc, 0)
        prev = self.read_table()
        dim = "spread"
        b = next(x for x in prev["cohorts"][dim]["buckets"] if x["key"] == "2-4c")
        deep = -0.035
        b["dev_trading"] = deep
        prev["series"]["KXBAR"]["dev"] = deep
        M.WORK_DIR = self.wd
        M.atomic_write_json(os.path.join(self.wd, M.TABLE_NAME), prev)
        sc = self.build()
        got = next(x for x in sc.cohorts[dim]["buckets"] if x["key"] == "2-4c")
        self.assertLessEqual(got["dev_trading"], deep + M.RATCHET_EASE_PER_RUN + 1e-9)
        self.assertGreater(sc.ratchet_eased, 0)
        self.assertLessEqual(sc.series_recs["KXBAR"]["dev"],
                             deep + M.RATCHET_EASE_PER_RUN + 1e-9)
        # deepening is immediate: a shallow previous value does not hold it up
        prev["cohorts"][dim]["buckets"] = [
            dict(x, dev_trading=0.0) for x in prev["cohorts"][dim]["buckets"]]
        M.atomic_write_json(os.path.join(self.wd, M.TABLE_NAME), prev)
        sc2 = self.build()
        got2 = next(x for x in sc2.cohorts[dim]["buckets"] if x["key"] == "2-4c")
        self.assertAlmostEqual(got2["dev_trading"],
                               next(x for x in self.build().cohorts[dim]["buckets"]
                                    if x["key"] == "2-4c")["dev_trading"])

    def test_both_bases_are_emitted_and_trading_is_the_acting_one(self):
        """SPEC 3.2 / judge must_fix J1-6: scoring NET softens the largest
        loss cohort (mid 30-70c is -150.7 trading but only -9.8 net) and the
        tier's realization factor is MEASURED on 2 of 83 events. Both bases
        are printed every run; only `trading` acts."""
        self._bar_corpus(n_events=3, n_strikes=3)
        rc, txt = self.run_cli(write=False)
        self.assertEqual(rc, 0)
        self.assertIn("acting column = trading", txt)
        sc = self.build()
        tbl = sc.table()
        self.assertEqual(tbl["params"]["score_basis"], "trading")
        for dim in M.DIM_ORDER:
            for b in tbl["cohorts"][dim]["buckets"]:
                for f in ("raw_trading", "dev_trading", "raw_blend", "dev_blend"):
                    self.assertIn(f, b)
                    self.assertIsInstance(b[f], float)
        self.assertIn("mu_trading_only", tbl["tier"])
        self.assertIn("mu_rent_blended", tbl["tier"])
        sc_t = self.build(score_basis="trading")
        sc_b = self.build(score_basis="blend")
        tk = sorted(sc_t.markets)[0]
        self.assertEqual(sc_t.adj_struct(tk)[0], sc_t.adj_struct(tk, "trading")[0])
        self.assertEqual(sc_b.score_basis, "blend")

    def test_frozen_edges_hash_matches_the_constant(self):
        """SPEC 3.3 / judge must_fix J1-3: the bucket edges are PRE-REGISTERED
        and FROZEN. A silent edit is specification search after the fact."""
        self.assertEqual(M.edges_sha256(), M.FROZEN_EDGES_SHA256)
        for dim in M.DIM_ORDER:
            self.assertIn(dim, M.COHORT_EDGES)
            self.assertEqual(len(M.COHORT_EDGES[dim]["buckets"]), 5)
        mutated = json.loads(json.dumps(M.COHORT_EDGES))
        mutated["spread"]["buckets"][0] = ["1c", 0, 2]
        self.assertNotEqual(M.edges_sha256(mutated), M.FROZEN_EDGES_SHA256)

    def test_refit_edges_stamps_the_flag_and_a_mutated_constant_is_refused(self):
        """SPEC 4.3: --refit-edges stamps edges_refit:true (the loader refuses
        it while IMM_SCAN_PERF_REQUIRE_FROZEN_EDGES=1); without the flag a
        changed edge table aborts the run."""
        self._bar_corpus(n_events=3, n_strikes=3)
        rc, _ = self.run_cli("--refit-edges")
        self.assertEqual(rc, 0)
        tbl = self.read_table()
        self.assertTrue(tbl["params"]["edges_refit"])
        old = M.FROZEN_EDGES_SHA256
        try:
            M.FROZEN_EDGES_SHA256 = "deadbeef"
            rc2, txt = self.run_cli()
            self.assertEqual(rc2, 1)
            self.assertIn("pre-registered edges were modified", txt)
        finally:
            M.FROZEN_EDGES_SHA256 = old


class TestFileShape(Base):

    def test_written_table_matches_the_spec_section_5_shape(self):
        """SPEC 5: the bot's loader validates against this schema, so the
        field names and types are the contract."""
        for i in range(3):
            self.simple_market(f"KXS{i}-26SEP20-T1", hours=6)
        self.s.fill(T0 + timedelta(hours=1), "KXS0-26SEP20-T1", 10, px=40)
        self.s.quotes("KXS0-26SEP20-T1", T0 + timedelta(hours=25)
                      - timedelta(seconds=30), 2, bid=29, ask=31)
        rc, _ = self.run_cli()
        self.assertEqual(rc, 0)
        tbl = self.read_table()
        self.assertEqual(tbl["version"], 1)
        for k in ("generated_at", "generated_by", "window", "params", "coverage",
                  "tier", "cohorts", "series", "events", "markets", "limits",
                  "audit", "unmatched_bar_keys", "warnings", "notes"):
            self.assertIn(k, tbl)
        for k in ("score_basis", "dim_clip", "adj_clip", "max_req",
                  "cohort_edges_sha256", "edges_refit"):
            self.assertIn(k, tbl["params"])
        for dim in M.DIM_ORDER:
            blk = tbl["cohorts"][dim]
            self.assertIn("field", blk)
            self.assertIn("assign", blk)
            los = [b["lo"] for b in blk["buckets"]]
            self.assertEqual(los, sorted(los))
            for b in blk["buckets"]:
                self.assertIsInstance(b["centre"], float)
                self.assertLessEqual(abs(b["dev_trading"]), tbl["params"]["dim_clip"] + 1e-12)
                self.assertLessEqual(abs(b["dev_blend"]), tbl["params"]["dim_clip"] + 1e-12)
        for rec in list(tbl["series"].values()) + list(tbl["events"].values()):
            self.assertIn(rec["verdict"], ("neutral", "down_rank", "bar"))
            self.assertEqual("until" in rec, rec["verdict"] == "bar")
        self.assertLessEqual(tbl["limits"]["barred_series"] +
                             tbl["limits"]["barred_events"], M.MAX_BARRED_FILE)
        self.assertLessEqual(tbl["limits"]["bar_frac_universe"], 0.25)
        self.assertEqual(tbl["audit"]["counterfactual_label"],
                         "IN-SAMPLE, MODELLED COUNTERFACTUAL")
        self.assertTrue(os.path.exists(os.path.join(self.wd, M.HISTORY_NAME)))
        with open(os.path.join(self.wd, M.HISTORY_NAME), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        for k in ("generated_at", "risk_days", "risk_days_at50c", "mo_dollars",
                  "mu_trading_only", "mu_rent_blended", "exit_code"):
            self.assertIn(k, rows[-1])

    def test_counterfactual_lines_carry_the_in_sample_label(self):
        """SPEC 4.5 / judge must_fix J1-4: every counterfactual carries the
        literal prefix at every point of use — the '41% of loss' figure was an
        IN-SAMPLE replay stated as a measurement."""
        for i in range(3):
            self.simple_market(f"KXC{i}-26SEP20-T1", hours=6)
        rc, txt = self.run_cli(write=False)
        self.assertEqual(rc, 0)
        for line in txt.splitlines():
            if "counterfactual" in line.lower() and "would have blocked" in line:
                self.assertIn("[IN-SAMPLE, MODELLED COUNTERFACTUAL]", line)
        self.assertIn("[IN-SAMPLE, MODELLED COUNTERFACTUAL]", txt)
        self.assertIn("WARNING ONLY, never acted on", txt)
        self.assertIn("The bar fires on nothing.", txt)

    def test_empty_window_is_handled_without_complaint(self):
        """The sinks start 2026-09-06; a 45-day window necessarily begins
        before they exist and must produce a neutral, empty table rather than
        an error."""
        rc, txt = self.run_cli(asof=datetime(2026, 9, 7, tzinfo=UTC))
        self.assertEqual(rc, 0)
        tbl = self.read_table()
        self.assertEqual(tbl["coverage"]["markets"], 0)
        self.assertEqual(tbl["series"], {})
        self.assertEqual(tbl["limits"]["down_ranked"], 0)

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
        self.assertIn("adj_struct", txt)
        self.assertFalse(os.path.exists(os.path.join(self.wd, M.TABLE_NAME)))


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
                 dict(imm.SCAN_PERF_COHORTS), dict(imm.SCAN_PERF_SERIES),
                 dict(imm.SCAN_PERF_EVENTS))
        self.addCleanup(self._restore_bot, imm, saved)
        imm.SCAN_PERF_FILE = path
        imm._scan_perf_state["mtime"] = 0.0
        n = imm.load_scan_perf()
        return n, (dict(imm.SCAN_PERF_COHORTS), dict(imm.SCAN_PERF_SERIES),
                   dict(imm.SCAN_PERF_EVENTS))

    @staticmethod
    def _restore_bot(imm, saved):
        f, state, coh, ser, evt = saved
        if f is not None:
            imm.SCAN_PERF_FILE = f
        imm._scan_perf_state.clear()
        imm._scan_perf_state.update(state)
        for live, old in ((imm.SCAN_PERF_COHORTS, coh),
                          (imm.SCAN_PERF_SERIES, ser),
                          (imm.SCAN_PERF_EVENTS, evt)):
            live.clear()
            live.update(old)

    def _assert_zero_rejects(self, imm, table, loaded, n, *, where):
        """ZERO REJECTS: every dimension, every bucket and every record in the
        file is present in the bot's in-memory table afterwards."""
        cohorts, series, events = loaded
        self.assertTrue(cohorts, f"{where}: bot loader refused the table "
                                 f"whole (load_scan_perf returned {n})")
        self.assertEqual(set(cohorts), set(table["cohorts"]),
                         f"{where}: dimensions dropped")
        for dim, ent in table["cohorts"].items():
            self.assertEqual(len(cohorts[dim]["buckets"]), len(ent["buckets"]),
                             f"{where}: buckets dropped from {dim}")
            self.assertEqual(cohorts[dim]["field"], ent["field"])
            for got, want in zip(cohorts[dim]["buckets"], ent["buckets"]):
                self.assertEqual(got["centre"], want["centre"])
                # SPEC 3.2: `trading` is the acting column, normalised to
                # `dev` by the loader. A reader that silently acted on
                # `dev_blend` would soften the largest loss cohort (mid
                # 30-70c is -150.7 trading but only -9.8 net) on a rent side
                # that is 99% MODELLED until the first statement paste.
                self.assertEqual(got["dev"], want["dev_trading"])
                self.assertEqual(got["hi"],
                                 float("inf") if want["hi"] is None
                                 else float(want["hi"]),
                                 f"{where}: 'hi': null must read as +infinity")
        # The reader-side blast-radius caps (SPEC 6.2 step 7) legitimately
        # drop records; that is truncation, not a reject. Assert the file is
        # under them first, then demand exact key equality.
        n_down = sum(1 for blk in ("series", "events")
                     for r in table[blk].values()
                     if r["verdict"] == "down_rank")
        n_bar = sum(1 for blk in ("series", "events")
                    for r in table[blk].values() if r["verdict"] == "bar")
        self.assertLessEqual(n_down, imm.SCAN_PERF_MAX_PENALIZED)
        self.assertLessEqual(n_bar, imm.SCAN_PERF_MAX_BARRED)
        self.assertEqual(set(series), set(table["series"]),
                         f"{where}: series records dropped")
        self.assertEqual(set(events), set(table["events"]),
                         f"{where}: event records dropped")
        self.assertEqual(n, len(cohorts) + len(series) + len(events))
        self.assertFalse(imm._scan_perf_state["stale"])
        for k, rec in table["series"].items():
            self.assertEqual(series[k]["dev"], rec["dev"])
            self.assertEqual(series[k]["verdict"], rec["verdict"])
        # SPEC 6.1/6.3: `events` is keyed by EVENT ROOT, and the two units
        # must derive that root the same way or a bar never matches the
        # candidate it was meant to stop.
        for k, rec in table["events"].items():
            self.assertEqual(rec.get("root", k), k)
            for dated in rec.get("events", []):
                self.assertEqual(imm.scan_perf_event_root(dated), k,
                                 f"{where}: {dated} -> "
                                 f"{imm.scan_perf_event_root(dated)} != {k}")

    def _assert_writer_is_not_laxer(self, imm, table, *, where):
        """The two guards whose denominators differ between the units."""
        dim_clip = float(table["params"]["dim_clip"])
        acting = [b["dev_trading"] for e in table["cohorts"].values()
                  for b in e["buckets"]]
        acting += [float(r.get("dev") or 0.0) for blk in ("series", "events")
                   for r in table[blk].values()]
        clamped = sum(1 for d in acting if abs(abs(d) - dim_clip) <= 1e-9)
        self.assertLessEqual(clamped, 0.10 * len(acting),
                             f"{where}: clamp storm on the ACTING set "
                             f"({clamped}/{len(acting)}) — the loader would "
                             f"refuse this file whole")
        n_loader = (len(table["series"]) + len(table["events"])
                    + sum(len(e["buckets"])
                          for e in table["cohorts"].values()))
        self.assertLessEqual(n_loader, imm.SCAN_PERF_MAX_RECORDS,
                             f"{where}: {n_loader} loader-counted records "
                             f"(series+events+buckets) over the cap; the "
                             f"`markets` trim does not bound this")

    # -- the tests --------------------------------------------------------
    def test_written_file_validates_against_the_bot_loader(self):
        """SPEC test 47, synthetic half: a table the scorer has just written
        from synthetic sinks is accepted by imm.load_scan_perf() with ZERO
        rejects."""
        imm = self._bot()
        # The loader ages a table out at 48 h, so the fixture must sit in the
        # real present, not on the synthetic 2026-09-06 timeline.
        now = datetime.now(UTC).replace(microsecond=0)
        start = now - timedelta(days=2)
        for i in range(3):
            self.simple_market(f"KXR{i}-26SEP20-T1", start=start, hours=6)
        rc, _ = self.run_cli(asof=now)
        self.assertEqual(rc, 0)
        path = os.path.join(self.wd, M.TABLE_NAME)
        with open(path, encoding="utf-8") as f:
            table = json.load(f)
        n, loaded = self._load_into_bot(imm, path)
        self._assert_zero_rejects(imm, table, loaded, n, where="synthetic")
        self._assert_writer_is_not_laxer(imm, table, where="synthetic")
        self.assertEqual(len(loaded[0]), len(M.DIM_ORDER))
        # and the table that just loaded is inert at the shipped weight
        self.assertEqual(imm.SCAN_PERF_WEIGHT, 0.0)

    def test_a_real_scorer_output_validates_against_the_bot_loader(self):
        """SPEC test 47, LIVE half. Synthetic sinks cannot produce a muted
        bucket, an open-ended top bucket with a risk_days-weighted centre, an
        ``events`` record carrying several dated siblings, or 89 series
        records at once — and every one of those is a place the two units
        could have disagreed. Point IMM_SCAN_PERF_TEST_TABLE at a table the
        scorer produced from the real sinks:

            IMM_SCAN_PERF_TEST_TABLE=<dir>/scan_perf.json \\
                python -m unittest test_imm_scan_perf

        Skipped when unset, so the suite stays hermetic: no test in this
        module ever reads run-logs/incentive-mm, which the live bot and four
        daily tasks own (2026-07-28: gate-test dry takes landed in the LIVE
        ledger).

        ``generated_at`` is re-stamped to now in a COPY before loading. Age
        is a wall-clock property, not a schema one — an artefact from last
        week is still the same shape — and the staleness path has its own
        test."""
        imm = self._bot()
        src = os.environ.get("IMM_SCAN_PERF_TEST_TABLE", "")
        if not src or not os.path.exists(src):
            self.skipTest("IMM_SCAN_PERF_TEST_TABLE unset or missing — see "
                          "the docstring for the one-liner")
        with open(src, encoding="utf-8") as f:
            table = json.load(f)
        table["generated_at"] = datetime.now(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        path = os.path.join(self.wd, M.TABLE_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(table, f)
        n, loaded = self._load_into_bot(imm, path)
        self._assert_zero_rejects(imm, table, loaded, n, where="live")
        self._assert_writer_is_not_laxer(imm, table, where="live")
        # the shapes synthetic fixtures cannot reach
        self.assertTrue(any(b["hi"] is None
                            for e in table["cohorts"].values()
                            for b in e["buckets"]),
                        "no open-ended top bucket in this artefact")
        self.assertEqual(table["params"]["edges_refit"], False)
        self.assertEqual(table["version"], 1)


if __name__ == "__main__":
    unittest.main()
