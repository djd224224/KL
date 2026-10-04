#!/usr/bin/env python3
"""Tests for nfl_prop_fair (the NFL player-prop gate's fair, 2026-10-03)."""

import json
import math
import os
import tempfile
import unittest
from unittest import mock

import nfl_prop_fair as nf

ESC_REC_TABLE = [0.0, 0.0003, 0.0029, 0.0098, 0.0233, 0.0455, 0.0787, 0.1250,
                 0.1865, 0.2656, 0.3644, 0.4850, 0.6297, 0.8006, 1.0]
# "0-9 yards, $0.0000; 10-19, $0.0001; ... 190-199, $0.8573; 200 or more, $1"
ESC_YDS_TABLE = [0.0, 0.0001, 0.0010, 0.0033, 0.0080, 0.0156, 0.0270, 0.0428,
                 0.0640, 0.0911, 0.1250, 0.1663, 0.2160, 0.2746, 0.3430,
                 0.4218, 0.5120, 0.6141, 0.7290, 0.8573, 1.0]


def market(ticker, name, **cs):
    series = ticker.split("-")[0]
    stat, kind = nf.SERIES[series]
    strike = {"scalar_cap": str(nf.DEFAULT_SPEC[(stat, kind)]["cap"]),
              "scalar_floor": "0"}
    if kind == "escalator":
        strike.update(exponent="3", scalar_step=str(
            nf.DEFAULT_SPEC[(stat, kind)]["step"]))
    strike.update(cs)
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0],
            "yes_sub_title": name, "custom_strike": strike,
            "yes_bid_dollars": "0.0678", "yes_ask_dollars": "0.0679"}


def spec_of(series):
    stat, kind = nf.SERIES[series]
    return dict(nf.DEFAULT_SPEC[(stat, kind)], stat=stat, kind=kind)


def rows(pid, name, team, season, stats, pos="RB"):
    """nflverse-shaped rows: stats = [(receptions, rec yds, rush yds, ppr)]."""
    out = []
    for w, (r, ry, sy, pts) in enumerate(stats, start=1):
        out.append({"player_id": pid, "name": name, "position": pos,
                    "season": season, "week": w, "season_type": "REG",
                    "team": team, "receptions": float(r),
                    "receiving_yards": float(ry), "rushing_yards": float(sy),
                    "fantasy_points_ppr": float(pts)})
    return out


class TestPayouts(unittest.TestCase):
    """The payout is Kalshi's published schedule exactly (custom_strike's
    'Payout Per Unit'); checked against all 596 settled props 9/24-10/1 on
    2026-10-03 (none differed)."""

    def test_escalator_tables(self):
        rec = spec_of("KXNFLESCALATORREC")
        self.assertEqual([nf.payout(rec, k) for k in range(15)], ESC_REC_TABLE)
        self.assertEqual(nf.payout(rec, 19), 1.0)             # capped at 14
        yds = spec_of("KXNFLESCALATORRECYDS")
        self.assertEqual([nf.payout(yds, y) for y in range(0, 201, 10)],
                         ESC_YDS_TABLE)
        # floored to the 10-yard step: 60-69 all pay 0.0270 (the float trap:
        # 10000 * 0.3**3 is 269.99999999999997)
        self.assertEqual({nf.payout(yds, y) for y in range(60, 70)}, {0.027})
        self.assertEqual(nf.payout(yds, -4), 0.0)
        self.assertEqual(nf.payout(spec_of("KXNFLESCALATORRSHYDS"), 128), 0.216)

    def test_ladders(self):
        self.assertAlmostEqual(nf.payout(spec_of("KXNFLLADDERREC"), 7), 0.35)
        self.assertEqual(nf.payout(spec_of("KXNFLLADDERREC"), 25), 1.0)
        self.assertAlmostEqual(nf.payout(spec_of("KXNFLLADDERRECYDS"), 86), 0.215)
        self.assertEqual(nf.payout(spec_of("KXNFLLADDERRSHYDS"), -3), 0.0)
        self.assertAlmostEqual(nf.payout(spec_of("KXNFLFFPTSLADDER"), 31), 0.31)

    def test_spec_reads_custom_strike(self):
        m = market("KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7",
                   "Bijan Robinson", scalar_cap="12")
        sp = nf.payout_spec(m, "rec", "escalator")
        self.assertEqual((sp["cap"], sp["exponent"], sp["step"]), (12.0, 3.0, 1.0))
        self.assertEqual(nf.payout(sp, 12), 1.0)
        bare = dict(m, custom_strike={})
        self.assertEqual(nf.payout_spec(bare, "rec", "escalator")["cap"], 14.0)


class TestDistributions(unittest.TestCase):

    def test_pmfs_are_distributions_with_the_right_mean(self):
        for stat, mu, kmax in (("rec", 4.6, 40), ("recyds", 55.0, 400),
                               ("rshyds", 80.0, 400), ("ffpts", 15.0, 100)):
            pm = nf.stat_pmf(stat, mu, kmax)
            self.assertAlmostEqual(sum(pm), 1.0, places=9, msg=stat)
            mean = sum(k * p for k, p in enumerate(pm))
            self.assertAlmostEqual(mean, mu, delta=0.02 * mu + 0.05, msg=stat)
            var = sum((k - mean) ** 2 * p for k, p in enumerate(pm))
            # var = phi * mu (the binning adds ~1/12)
            self.assertAlmostEqual(var / mu, nf.STAT_MODEL[stat]["phi"],
                                   delta=0.06 * nf.STAT_MODEL[stat]["phi"] + 0.05,
                                   msg=stat)

    def test_incomplete_gamma(self):
        # P(1, x) = 1 - e^-x; P(a, x) for integer a is the Poisson tail
        for x in (0.1, 1.0, 3.0, 12.0):
            self.assertAlmostEqual(nf._gammp(1.0, x), 1 - math.exp(-x), places=10)
        p3 = 1 - math.exp(-4.0) * (1 + 4.0 + 8.0)
        self.assertAlmostEqual(nf._gammp(3.0, 4.0), p3, places=10)
        self.assertEqual(nf._gammp(2.0, 0.0), 0.0)

    def test_robinson_escalator_fair(self):
        # mu 3.95 receptions (his 37 games to 10/3): 4.75c, the book 6.78 /
        # 6.79 inside the band, the 40c fill nowhere near it
        spec = spec_of("KXNFLESCALATORREC")
        fair, lo, hi = nf.fair_band(spec, 3.951941, 37)
        self.assertAlmostEqual(fair * 100, 4.753, places=2)
        self.assertLess(lo * 100, 6.78)
        self.assertGreater(hi * 100, 6.79)
        self.assertLess(hi * 100 + 3, 40.0)
        # monotone in mu, and a short history widens the band
        self.assertLess(nf.expected_payout(spec, 3.0), nf.expected_payout(spec, 4.0))
        _f, wlo, whi = nf.fair_band(spec, 3.951941, 5)
        self.assertLess(wlo, lo)
        self.assertGreater(whi, hi)


class TestPrediction(unittest.TestCase):

    def test_ewma_with_season_weight_and_shrink(self):
        games = rows("p1", "A B", "ATL", 2025, [(2, 0, 0, 0)] * 3) + \
            rows("p1", "A B", "ATL", 2026, [(6, 0, 0, 0)])
        mu, raw, n, ncur = nf.predict_mu(games, "rec", 2026)
        hl = nf.HALF_LIFE_GAMES
        w = [0.5 ** (3 / hl) * 0.5, 0.5 ** (2 / hl) * 0.5, 0.5 ** (1 / hl) * 0.5, 1.0]
        want = (2 * sum(w[:3]) + 6 * w[3]) / sum(w)
        self.assertAlmostEqual(raw, want, places=9)
        self.assertAlmostEqual(mu, 0.902 * want + 0.066, places=9)
        self.assertEqual((n, ncur), (4, 1))
        self.assertIsNone(nf.predict_mu([], "rec", 2026))

    def test_negative_yards_clip_but_points_do_not(self):
        games = rows("p1", "A B", "ATL", 2026, [(0, 0, -5, -1), (0, 0, 15, 3)])
        _mu, raw, _n, _c = nf.predict_mu(games, "rshyds", 2026)
        self.assertGreater(raw, 7.5)                 # -5 counted as 0
        _mu, raw_p, _n, _c = nf.predict_mu(games, "ffpts", 2026)
        w_old = 0.5 ** (1 / nf.HALF_LIFE_GAMES)
        self.assertAlmostEqual(raw_p, (3 - w_old) / (1 + w_old))   # -1 kept


class TestPlayers(unittest.TestCase):

    def test_tickers(self):
        self.assertEqual(nf.split_event_teams("KXNFLESCALATORREC-26OCT05ATLNO"),
                         ("ATL", "NO"))
        for code, want in (("NYGLAR", ("NYG", "LAR")), ("LACSEA", ("LAC", "SEA")),
                           ("LARPHI", ("LAR", "PHI")), ("NEBUF", ("NE", "BUF")),
                           ("TENNYG", ("TEN", "NYG")), ("GBTB", ("GB", "TB"))):
            self.assertEqual(nf.split_event_teams(f"KXNFLLADDERREC-26OCT04{code}"),
                             want, code)
        self.assertIsNone(nf.split_event_teams("KXNFLLADDERREC-26OCT04"))
        tm = ("ATL", "NO")
        self.assertEqual(nf.parse_player_suffix(
            "KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7", tm), ("ATL", 7))
        self.assertEqual(nf.parse_player_suffix(
            "KXNFLLADDERREC-26OCT05ATLNO-NOCOLAVE12", tm), ("NO", 12))
        self.assertEqual(nf.parse_player_suffix(
            "KXNFLLADDERREC-26OCT04TENNYG-NYGCSKATTEBO44", ("TEN", "NYG")),
            ("NYG", 44))
        self.assertEqual(nf.norm_name("Michael Pittman Jr."), "michaelpittman")
        self.assertEqual(nf.norm_name("Amon-Ra St. Brown"), "amonrastbrown")

    def test_index_finds_and_breaks_ties_on_team(self):
        idx = nf.PlayerIndex(
            rows("a", "Bijan Robinson", "ATL", 2026, [(5, 40, 80, 20)])
            + rows("b", "Mike Williams", "NYJ", 2025, [(3, 40, 0, 8)])
            + rows("c", "Mike Williams", "PIT", 2026, [(2, 20, 0, 4)], pos="WR"))
        self.assertEqual(idx.find("Bijan Robinson", "ATL"), ("a", ""))
        self.assertEqual(idx.find("Mike Williams", "PIT")[0], "c")
        pid, why = idx.find("Mike Williams", "DAL")
        self.assertIsNone(pid)
        self.assertIn("2 nflverse players", why)
        self.assertIn("no nflverse games", idx.find("Nobody Here", "ATL")[1])
        # JAC / LAR / WSH are Kalshi's codes for nflverse's JAX / LA / WAS
        idx2 = nf.PlayerIndex(rows("x", "T J", "JAX", 2026, [(1, 1, 1, 1)])
                              + rows("y", "T J", "LA", 2026, [(1, 1, 1, 1)]))
        self.assertEqual(idx2.find("T J", "JAC")[0], "x")
        self.assertEqual(idx2.find("T J", "LAR")[0], "y")

    def test_roster_injuries(self):
        js = {"athletes": [{"items": [
            {"fullName": "Terry McLaurin", "jersey": "17",
             "injuries": [{"status": "Doubtful", "date": "2026-10-03T14:42Z"}],
             "status": {"type": "active"}},
            {"fullName": "Hollywood Brown", "jersey": "5", "injuries": [],
             "status": {"type": "active"}},
            {"fullName": "Stefon Diggs", "jersey": "3", "injuries": [],
             "status": {"type": "active"}},
            {"fullName": "No Number", "injuries": []}]}]}
        ros = nf.roster_status(js)
        self.assertEqual(nf.find_on_roster(ros, "Terry McLaurin", 17)["status"],
                         "Doubtful")
        self.assertEqual(nf.find_on_roster(ros, "Stefon Diggs", 3)["status"], "")
        # ESPN's nickname, the ticker's jersey + last name
        self.assertEqual(nf.find_on_roster(ros, "Marquise Brown", 5)["name"],
                         "Hollywood Brown")
        # a wrong jersey still finds a unique full name
        self.assertEqual(nf.find_on_roster(ros, "Stefon Diggs", 14)["name"],
                         "Stefon Diggs")
        self.assertIsNone(nf.find_on_roster(ros, "Somebody Else", 99))


class TestSnapshot(unittest.TestCase):

    def _world(self, robinson_games=37):
        hist = rows("p1", "Bijan Robinson", "ATL", 2025,
                    [(5, 40, 80, 20)] * (robinson_games - 3)) + \
            rows("p1", "Bijan Robinson", "ATL", 2026,
                 [(8, 90, 83, 31.3), (3, 9, 72, 11.1), (2, 19, 194, 35.3)])
        hist += rows("p2", "Kid Rookie", "NO", 2026, [(1, 10, 0, 3)] * 2, pos="WR")
        idx = nf.PlayerIndex(hist)
        fam = [market("KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7", "Bijan Robinson"),
               market("KXNFLLADDERREC-26OCT05ATLNO-ATLBROBINSON7", "Bijan Robinson"),
               market("KXNFLESCALATORREC-26OCT05ATLNO-NOKROOKIE1", "Kid Rookie"),
               market("KXNFLESCALATORREC-26OCT05ATLNO-NOGHOST2", "Ghost Player"),
               market("KXNFLLADDERREC-26OCT05ATLNO-ATLDLONDON5", "Drake London"),
               {"ticker": "KXOTHER-26OCT05-X"}]
        ros = {"atl": {"ts": 1000.0, "roster": nf.roster_status({"athletes": [{"items": [
            {"fullName": "Bijan Robinson", "jersey": "7", "injuries": []},
            {"fullName": "Drake London", "jersey": "5",
             "injuries": [{"status": "Questionable"}]}]}]})}}
        return idx, fam, ros

    def test_entries(self):
        idx, fam, ros = self._world()
        snap = nf.build_snapshot(2000.0, 2026, fam, idx, ros, {}, ["x"])
        m = snap["markets"]
        self.assertNotIn("KXOTHER-26OCT05-X", m)
        e = m["KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7"]
        self.assertIsNone(e.get("err"))
        self.assertIsNone(e["injury"])
        self.assertEqual((e["team"], e["jersey"], e["n_games"], e["n_season"]),
                         ("ATL", 7, 37, 3))
        self.assertLess(e["fair_lo"], e["fair"])
        self.assertLess(e["fair"], e["fair_hi"])
        self.assertEqual(e["roster_ts"], 1000.0)
        lad = m["KXNFLLADDERREC-26OCT05ATLNO-ATLBROBINSON7"]
        self.assertAlmostEqual(lad["fair"], e["mu"] / 20.0, places=4)   # linear
        self.assertIn("only 2 games", m["KXNFLESCALATORREC-26OCT05ATLNO-NOKROOKIE1"]["err"])
        self.assertIn("no nflverse games",
                      m["KXNFLESCALATORREC-26OCT05ATLNO-NOGHOST2"]["err"])
        self.assertEqual(m["KXNFLLADDERREC-26OCT05ATLNO-ATLDLONDON5"]["injury"],
                         "Questionable")
        # no NO roster read: no roster_ts (the gate fails closed on it)
        self.assertNotIn("roster_ts", m["KXNFLESCALATORREC-26OCT05ATLNO-NOKROOKIE1"])
        self.assertEqual(snap["errors"], ["x"])
        json.dumps(snap)                                 # the status file

    def test_not_on_roster_reads_as_a_designation(self):
        idx, fam, ros = self._world()
        ros["atl"]["roster"] = {}
        snap = nf.build_snapshot(2000.0, 2026, fam[:1], idx, ros, {})
        self.assertEqual(
            snap["markets"]["KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7"]["injury"],
            "not on roster")


def team_rows(team, season, games):
    """nflverse-shaped rows for one team: games = [{pid: (pos, targets,
    carries, attempts)}] in week order; every stat column filled."""
    out = []
    for w, g in enumerate(games, start=1):
        for pid, (pos, tg, ca, at) in g.items():
            out.append({"player_id": pid, "name": f"P {pid}", "position": pos,
                        "season": season, "week": w, "season_type": "REG",
                        "team": team, "receptions": tg * 0.6,
                        "receiving_yards": tg * 7.0, "rushing_yards": ca * 4.0,
                        "fantasy_points_ppr": tg + ca * 0.5, "targets": float(tg),
                        "carries": float(ca), "attempts": float(at)})
    return out


def roster_js(*athletes):
    """ESPN roster JSON: athletes = (name, jersey, pos, status or "", date)."""
    items = []
    for name, jersey, pos, status, date in athletes:
        items.append({"fullName": name, "jersey": str(jersey),
                      "position": {"abbreviation": pos},
                      "injuries": ([{"status": status, "date": date}]
                                   if status else []),
                      "status": {"type": "active"}})
    return {"athletes": [{"items": items}]}


class TestTeammates(unittest.TestCase):
    """The teammate adjustment (Jack 2026-10-04): absent teammates' window
    shares x P(miss) move a player's mean by alpha x V / (1 - V)."""

    # IND-like: WR1 a (30% of targets), WR2 b, TE c, RB d (all the carries),
    # QB q; WR e left after week 1 (already absorbed); 5 games, window 4
    G = {"a": ("WR", 9, 0, 0), "b": ("WR", 6, 0, 0), "c": ("TE", 6, 0, 0),
         "d": ("RB", 9, 20, 0), "q": ("QB", 0, 2, 30)}

    def _index(self, week5_missing=()):
        games = [dict(self.G, e=("WR", 10, 0, 0))]
        for w in range(2, 6):
            g = dict(self.G)
            if w == 5:
                for pid in week5_missing:
                    g.pop(pid)
            games.append(g)
        rows = team_rows("IND", 2026, games)
        for r in rows:            # names the roster can find
            r["name"] = {"a": "Keenan Allen", "b": "Josh Downs", "c": "Tyler Warren",
                         "d": "Jonathan Taylor", "q": "Daniel Jones",
                         "e": "Old Guy"}[r["player_id"]]
        return nf.PlayerIndex(rows)

    def test_p_miss(self):
        self.assertEqual([nf.p_miss(s) for s in ("Out", "Injured Reserve",
                          "Doubtful", "Questionable", "", "Active",
                          "Day-To-Day", "Suspension", "Weird")],
                         [1.0, 1.0, 0.8, 0.25, 0.0, 0.0, 0.1, 1.0, 0.5])

    def test_team_shares_count_misses_as_zero(self):
        idx = self._index()
        sh = idx.team_shares("IND", 4)          # weeks 2-5
        self.assertAlmostEqual(sh["a"]["t"], 9 / 30)
        self.assertAlmostEqual(sh["d"]["c"], 20 / 22)
        self.assertAlmostEqual(sh["q"]["a"], 1.0)
        self.assertNotIn("e", sh)               # outside the window
        self.assertTrue(sh["a"]["recent"])
        # a player missing week 5 still has his share, counted with a zero
        sh2 = self._index(week5_missing=("a",)).team_shares("IND", 4)
        self.assertAlmostEqual(sh2["a"]["t"], 27 / (3 * 30 + 21))
        self.assertTrue(sh2["a"]["recent"])     # played week 4

    def test_context_and_multipliers(self):
        idx = self._index()
        ros = {"ts": 1.0, "roster": nf.roster_status(roster_js(
            ("Keenan Allen", 10, "WR", "Out", "2026-10-03T14:37Z"),
            ("Josh Downs", 1, "WR", "", None),
            ("Tyler Warren", 84, "TE", "Questionable", "2026-10-01T21:25Z"),
            ("Jonathan Taylor", 28, "RB", "", None),
            ("Daniel Jones", 17, "QB", "", None),
            ("Old Guy", 80, "WR", "Injured Reserve", "2026-09-01T00:00Z"),
            ("Kicker Guy", 3, "K", "Out", "2026-10-03T20:00Z")))}
        ctx = nf.team_context("ind", ros, idx)
        names = sorted(x["name"] for x in ctx["absent"])
        self.assertEqual(names, ["Keenan Allen", "Tyler Warren"])  # not Old Guy
        self.assertEqual(ctx["news"], "Keenan Allen WR Out")       # latest, material
        self.assertEqual(ctx["news_at"], nf._espn_ts("2026-10-03T14:37Z"))
        # Downs: V = 1.0 x 0.30 + 0.25 x 0.20
        v = 9 / 30 + 0.25 * 6 / 30
        m, d = nf.teammate_mult("rec", "WR", "b", ctx)
        self.assertAlmostEqual(m, 1 + 0.18 * v / (1 - v))
        self.assertAlmostEqual(d["v_t"], round(v, 4))
        self.assertAlmostEqual(nf.teammate_mult("recyds", "WR", "b", ctx)[0],
                               1 + 0.14 * v / (1 - v))
        # Allen's own markets: his own designation is the gate's, not this
        m_a, _ = nf.teammate_mult("rec", "WR", "a", ctx)
        va = 0.25 * 6 / 30
        self.assertAlmostEqual(m_a, 1 + 0.18 * va / (1 - va))
        # the RB's rushing: no rusher out -> 1; QBs untouched
        self.assertEqual(nf.teammate_mult("rshyds", "RB", "d", ctx)[0], 1.0)
        self.assertEqual(nf.teammate_mult("ffpts", "QB", "q", ctx)[0], 1.0)
        with mock.patch.object(nf, "TEAMMATE_ENABLE", False):
            self.assertEqual(nf.teammate_mult("rec", "WR", "b", ctx)[0], 1.0)

    def test_lead_back_and_quarterback_out(self):
        idx = self._index()
        ros = {"ts": 1.0, "roster": nf.roster_status(roster_js(
            ("Jonathan Taylor", 28, "RB", "Out", "2026-10-03T10:00Z"),
            ("Daniel Jones", 17, "QB", "Out", "2026-10-03T11:00Z")))}
        ctx = nf.team_context("ind", ros, idx)
        self.assertEqual(ctx["news"], "Daniel Jones QB Out")
        # the clamp: Taylor's 20/22 of the carries -> V capped at MAX_VACATED
        backup = {"pid": "z", "position": "RB"}
        m, d = nf.teammate_mult("rshyds", "RB", backup["pid"], ctx)
        vc = nf.MAX_VACATED
        self.assertAlmostEqual(d["v_c"], vc)
        self.assertAlmostEqual(m, (1 + 0.33 * vc / (1 - vc)) * 0.91)
        # a WR: Taylor's 9/30 of the targets, then the QB
        m_w, _ = nf.teammate_mult("rec", "WR", "b", ctx)
        vt = 9 / 30
        self.assertAlmostEqual(m_w, (1 + 0.18 * vt / (1 - vt)) * 0.95)

    def test_news_needs_a_contributor_and_tracks_changes(self):
        idx = self._index()
        js = roster_js(("Keenan Allen", 10, "WR", "Questionable", "2026-10-02T12:00Z"),
                       ("Nobody", 99, "WR", "Out", "2026-10-03T23:00Z"))
        w = nf.NflPropWatch(session=mock.Mock(), cache_dir=tempfile.mkdtemp())
        ros1 = nf.roster_status(js)
        w._track_status("ind", ros1, 1000.0)
        self.assertEqual(w.status_changed_at, {})       # first sighting
        js2 = roster_js(("Keenan Allen", 10, "WR", "", None),
                        ("Nobody", 99, "WR", "Out", "2026-10-03T23:00Z"))
        ros2 = nf.roster_status(js2)
        w._track_status("ind", ros2, 2000.0)            # Allen cleared
        self.assertEqual(w.status_changed_at, {("ind", "keenanallen"): 2000.0})
        ctx = nf.team_context("ind", {"ts": 2000.0, "roster": ros2}, idx,
                              w.status_changed_at)
        # the cleared contributor is the news; Nobody (no share) is not
        self.assertEqual((ctx["news_at"], ctx["news"]),
                         (2000.0, "Keenan Allen WR cleared"))
        self.assertEqual(ctx["absent"], [])

    def test_snapshot_carries_the_adjustment(self):
        idx = self._index()
        ros = {"ind": {"ts": 5.0, "roster": nf.roster_status(roster_js(
            ("Keenan Allen", 10, "WR", "Out", "2026-10-03T14:37Z"),
            ("Josh Downs", 1, "WR", "", None)))}}
        fam = [market("KXNFLLADDERREC-26OCT04INDWAS-INDJDOWNS1", "Josh Downs")]
        snap = nf.build_snapshot(10.0, 2026, fam, idx, ros, {})
        e = snap["markets"]["KXNFLLADDERREC-26OCT04INDWAS-INDJDOWNS1"]
        v = 9 / 30
        self.assertAlmostEqual(e["team_mult"], round(1 + 0.18 * v / (1 - v), 4))
        self.assertAlmostEqual(e["mu"], round(e["mu_base"] * e["team_mult"], 3),
                               places=3)
        self.assertAlmostEqual(e["fair"], e["mu"] / 20.0, places=4)
        self.assertEqual(e["news"], "Keenan Allen WR Out")
        self.assertIn("ind", snap["teams"])
        self.assertIn("Keenan Allen WR Out", e["teammates"]["absent"][0])


class FakeResp:
    def __init__(self, status=200, text="", js=None):
        self.status_code, self.text, self._js = status, text, js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._js


class FakeSession:
    def __init__(self, csv_by_season, rosters):
        self.csv, self.rosters, self.calls = csv_by_season, rosters, []
        self.headers = {}

    def get(self, url, timeout=None):
        self.calls.append(url)
        if "nflverse" in url:
            season = int(url.rsplit("_", 1)[1].split(".")[0])
            if season not in self.csv:
                return FakeResp(404)
            return FakeResp(text=self.csv[season])
        team = url.split("/teams/")[1].split("/")[0]
        return FakeResp(js=self.rosters.get(team, {"athletes": []}))


def nflverse_csv(game_rows):
    cols = ["player_id", "player_display_name", "position", "season", "week",
            "season_type", "team", "receptions", "receiving_yards",
            "rushing_yards", "fantasy_points_ppr", "extra"]
    lines = [",".join(cols)]
    for r in game_rows:
        lines.append(",".join(str(x) for x in (
            r["player_id"], r["name"], r["position"], r["season"], r["week"],
            r["season_type"], r["team"], r["receptions"], r["receiving_yards"],
            r["rushing_yards"], r["fantasy_points_ppr"], "NA")))
    return "\n".join(lines) + "\n"


class TestWatch(unittest.TestCase):

    def test_refresh_end_to_end_and_caching(self):
        tmp = tempfile.mkdtemp(prefix="nfl_fair_")
        csvs = {2025: nflverse_csv(rows("p1", "Bijan Robinson", "ATL", 2025,
                                        [(5, 40, 80, 20)] * 17)),
                2026: nflverse_csv(rows("p1", "Bijan Robinson", "ATL", 2026,
                                        [(4, 30, 90, 18)] * 3)
                                   + rows("q", "Kicker Guy", "ATL", 2026,
                                          [(0, 0, 0, 9)], pos="K"))}
        roster = {"athletes": [{"items": [
            {"fullName": "Bijan Robinson", "jersey": "7", "injuries": []}]}]}
        sess = FakeSession(csvs, {"atl": roster})
        fam = [market("KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7", "Bijan Robinson")]
        reads = []

        def get_json(path, params):
            reads.append(params["series_ticker"])
            return {"markets": fam if params["series_ticker"] == "KXNFLESCALATORREC" else [],
                    "cursor": None}
        w = nf.NflPropWatch(get_json, session=sess, cache_dir=tmp, season=2026)
        snap = w.refresh(now_ts=10_000.0)
        e = snap["markets"]["KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7"]
        self.assertEqual(e["n_games"], 20)
        self.assertIsNotNone(e["fair"])
        self.assertIsNone(e["injury"])
        self.assertEqual(sorted(reads), sorted(nf.SERIES))
        # 2024 is not published here: an error line, the rest still prices
        self.assertTrue(any("2024" in x for x in snap["errors"]))
        self.assertTrue(os.path.exists(os.path.join(tmp, "stats_player_week_2026.csv")))
        self.assertNotIn("q", w.index.games)                   # skill positions only
        # a minute later nothing is re-downloaded; the roster is cached too
        n_calls = len(sess.calls)
        w.refresh(now_ts=10_060.0)
        self.assertEqual(len(sess.calls), n_calls)
        # past the roster clock the roster is read again, and the missing
        # 2024 file once more (NFLV_RETRY_SECS); 2025 / 2026 stay cached
        w.refresh(now_ts=10_000.0 + max(nf.ROSTER_REFRESH_SECS,
                                         nf.NFLV_RETRY_SECS) + 1)
        self.assertEqual(sorted(sess.calls[n_calls:]),
                         sorted([nf.ESPN_ROSTER.format(team="atl"),
                                 nf.NFLV_URL.format(season=2024)]))
        # a failed roster read keeps the last good one (its ts ages out in
        # the gate)
        sess.rosters = None
        snap = w.refresh(now_ts=10_000.0 + 2 * nf.ROSTER_REFRESH_SECS + 2)
        self.assertTrue(any("roster atl" in x for x in snap["errors"]))
        self.assertEqual(snap["markets"]["KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7"]
                         ["roster_ts"], 10_000.0 + max(nf.ROSTER_REFRESH_SECS,
                                                       nf.NFLV_RETRY_SECS) + 1)


if __name__ == "__main__":
    unittest.main()
