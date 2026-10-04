#!/usr/bin/env python3
"""Player-history fair values for incentive_mm's NFL player-prop gate.

Jack 2026-10-03: "how to avoid getting blown up on the NFL markets like
KXNFLESCALATORREC-26OCT05ATLNO? can you model what it should be, based on
past data of the player to protect from the market going super out of wack
like the 400-lot overnight fill: the ATL@NO Robinson escalator."

WHAT HAPPENED (orders / fills logs, 2026-10-03). The Bijan Robinson
receptions escalator (KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7) listed at
00:24Z and traded 3-6c until 05:02Z, when the touch jumped from 6 / 97 to
45 / 60. The quote loop follows the touch, so the bid went 6 -> 8 -> 12 -> 40
(05:15Z, amended to the 40c touch, 400 lots in the Saturday quiet hours) and
filled 400 at 40c at 05:36Z. By 10:14Z the book was 6 / 7 again (6.8c at
23:52Z): -$134 on one fill. The same evening his receptions LADDER ran 17 ->
44 -> 24 and filled 200 at 42 (-$44 against fair). Nothing in the bot had a
view of what the contract was worth; it joined whatever touch existed, and
the reward estimator ranks exactly those thin, dislocated books highest.

THE MARKETS (custom_strike, verified against all 596 settlements to 9/24-10/1):
  KXNFLLADDERREC        $0.05 per reception, cap 20       = min(s,20)/20
  KXNFLLADDERRECYDS     $0.0025 per receiving yard, cap 400
  KXNFLLADDERRSHYDS     $0.0025 per rushing yard, cap 400 (negative -> 0)
  KXNFLFFPTSLADDER      $0.01 per Sleeper PPR point, rounded half up, cap 100
  KXNFLESCALATORREC     floor(1e4 * (min(s,14)/14)^3) / 1e4
  KXNFLESCALATORRECYDS  floor(1e4 * (min(y10,200)/200)^3) / 1e4, y10 = yards
  KXNFLESCALATORRSHYDS    floored to 10 (60-69 yards pays 0.0270)
One market per player per game; settled markets carry the stat itself in
expiration_value (the payout, for FFPTS).

THE MODEL (fit 2026-10-03 on nflverse weekly stats, out of sample: every
2025 and 2026 regular-season game of a QB/WR/TE/RB predicted from that
player's earlier games only; "starters" = prior mean over 2.5 catches /
25 yards / 6 PPR, ~2,200 player-games per stat):
  - mean: an exponentially weighted average of the player's games, half-life
    HALF_LIFE_GAMES (8) games, earlier seasons at PRIOR_SEASON_WEIGHT (0.5) --
    the best of half-lives 2-1000 x season weights 1 / 0.7 / 0.5 on all four
    stats -- then shrunk toward the mean, mu = a * ewma + b (receptions 0.902
    / 0.066, receiving yards 0.867 / 2.94, rushing yards 0.833 / 6.59, PPR
    0.894 / 0.63): the raw average over-predicts starters by 5-8%.
    Targets / carries add nothing over the stat's own history (MSE 4.55 vs
    4.55 for receptions).
  - shape: every stat's variance is ~proportional to its mean at every level
    (var / mu: receptions 1.3, receiving yards 23, rushing yards 21, PPR ~5),
    so receptions are NB1 (var = 1.3 mu) and yards / points a gamma with
    var = phi * mu. Calibration of the escalator payouts, predicted vs paid:
    receptions 4.71c vs 4.63c, receiving yards 3.23c vs 3.14c, rushing yards
    4.02c vs 4.10c, by quintile within ~0.5c; ladders and PPR within 0.2c.
  - on the 368 settled Kalshi props with a pre-game book, the model's error
    equals the market's own pre-game mid (MSE 59.5 vs 59.1 cents^2, the two
    correlated 0.89-0.95): a real fair, not just a sanity bound.
  - band: fair_lo / fair_hi = the payout at mu x BAND_LO (0.7) / x BAND_HI
    (1.5). On 517k logged tight-book cycles (spread <= 6c) the touch sat
    above fair_hi + 1c 1.3% of the time and never below fair_lo. Replaying
    the 273 prop fills 9/30-10/3: the 32 outside the band lost $384 at
    settlement / fair (Robinson 40c, Diggs 51c, Johnston 48c ...), the 241
    inside made $97.
  - TEAMMATES (2026-10-04, see TEAMMATE_ALPHA): a designated teammate's
    share of the team's last 4 games' targets / carries, weighted by his
    chance to sit (P_MISS), moves the player's mean by alpha x V / (1 - V)
    -- a key pass-catcher out adds ~+7-16% to his teammates' catches, a lead
    back out ~+37% to his backup's rushing, a starting QB out -5% to the
    receivers. Entries carry mu_base, team_mult, teammates and the team's
    news / news_at, which the gate's news hold reads.
  - INJURIES: ESPN's team roster carries each player's designation
    (Questionable / Doubtful / Out / IR); the gate stands any designated
    player aside. 10/3: McLaurin went Doubtful at 14:42Z and the bot bought
    his yards ladder at 12c at 16:43Z (book 3 / 4 by evening).

DATA: nflverse's weekly player stats (github.com/nflverse/nflverse-data,
release `stats_player`, one CSV per season, refreshed after each game day),
cached under CACHE_DIR; the current season re-downloads every
NFLV_REFRESH_SECS (6h), finished seasons weekly. Kalshi's yes_sub_title
("Bijan Robinson") matches nflverse's player_display_name for all 1,113
markets listed to date; the ticker's team code breaks name ties. ESPN rosters
(site.web.api.espn.com, the host the kickoff resolver uses) per team with an
open market every ROSTER_REFRESH_SECS; the player is found by jersey (the
ticker's trailing number) and last name.

    python nfl_prop_fair.py                     # every open prop vs its book
    python nfl_prop_fair.py --event KXNFLESCALATORREC-26OCT05ATLNO
    python nfl_prop_fair.py --ticker KXNFLESCALATORREC-26OCT05ATLNO-ATLBROBINSON7
"""

import argparse
import csv
import functools
import io
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# series -> (stat, kind); the stat keys index STAT_MODEL
SERIES: Dict[str, Tuple[str, str]] = {
    "KXNFLLADDERREC": ("rec", "ladder"),
    "KXNFLLADDERRECYDS": ("recyds", "ladder"),
    "KXNFLLADDERRSHYDS": ("rshyds", "ladder"),
    "KXNFLFFPTSLADDER": ("ffpts", "ladder"),
    "KXNFLESCALATORREC": ("rec", "escalator"),
    "KXNFLESCALATORRECYDS": ("recyds", "escalator"),
    "KXNFLESCALATORRSHYDS": ("rshyds", "escalator"),
}
# stat -> nflverse column, shrink (mu = a * ewma + b), dispersion phi (var /
# mean), discrete (NB1) or continuous (gamma, integer bins)
STAT_MODEL: Dict[str, dict] = {
    "rec": {"col": "receptions", "a": 0.902, "b": 0.066, "phi": 1.3,
            "discrete": True},
    "recyds": {"col": "receiving_yards", "a": 0.867, "b": 2.94, "phi": 23.0,
               "discrete": False},
    "rshyds": {"col": "rushing_yards", "a": 0.833, "b": 6.59, "phi": 21.0,
               "discrete": False},
    "ffpts": {"col": "fantasy_points_ppr", "a": 0.894, "b": 0.63, "phi": 5.0,
              "discrete": False},
}
# Kalshi's default payout geometry per (stat, kind), used when a market's
# custom_strike is missing a field (it never was on the 1,113 to date)
DEFAULT_SPEC: Dict[Tuple[str, str], dict] = {
    ("rec", "ladder"): {"cap": 20.0, "floor": 0.0},
    ("recyds", "ladder"): {"cap": 400.0, "floor": 0.0},
    ("rshyds", "ladder"): {"cap": 400.0, "floor": 0.0},
    ("ffpts", "ladder"): {"cap": 100.0, "floor": 0.0},
    ("rec", "escalator"): {"cap": 14.0, "floor": 0.0, "exponent": 3.0,
                           "step": 1.0},
    ("recyds", "escalator"): {"cap": 200.0, "floor": 0.0, "exponent": 3.0,
                              "step": 10.0},
    ("rshyds", "escalator"): {"cap": 200.0, "floor": 0.0, "exponent": 3.0,
                              "step": 10.0},
}

HALF_LIFE_GAMES = _env_float("IMM_NFL_HALF_LIFE_GAMES", 8.0)
PRIOR_SEASON_WEIGHT = _env_float("IMM_NFL_PRIOR_SEASON_WEIGHT", 0.5)
BAND_LO = _env_float("IMM_NFL_BAND_LO", 0.7)
BAND_HI = _env_float("IMM_NFL_BAND_HI", 1.5)
# fewer games than this in the window -> no fair (a rookie's first weeks)
MIN_GAMES = int(_env_float("IMM_NFL_MIN_GAMES", 3))
# under this many games the band widens to WIDE_LO / WIDE_HI
FULL_GAMES = int(_env_float("IMM_NFL_FULL_GAMES", 8))
WIDE_LO = _env_float("IMM_NFL_WIDE_LO", 0.5)
# RECENT FORM (Jack 2026-10-04: "yes add it and ship", on Chuba Hubbard's
# DET@CAR fantasy ladder): the long EWMA is slow to see a role that changed
# -- Hubbard's model mean was 12.6 PPR, his 2026 games 23.7 / 14.4 / 15.0
# (late-2025 games of 3-5 points weighed it down), and the IMM's bid cap
# (band top + 1c = 19c) sat behind 3,510 contracts at the 20c touch. Each
# entry also carries the same band on the mean of the player's last
# RECENT_GAMES games (fair_recent / _lo / _hi, the teammate multiplier
# applied); the IMM gate quotes inside the WIDER of the two. 0 = off.
RECENT_GAMES = int(_env_float("IMM_NFL_RECENT_GAMES", 4))
WIDE_HI = _env_float("IMM_NFL_WIDE_HI", 2.0)
SEASONS_BACK = int(_env_float("IMM_NFL_SEASONS_BACK", 2))
NFLV_REFRESH_SECS = _env_float("IMM_NFL_NFLV_REFRESH_SECS", 6 * 3600)
NFLV_OLD_REFRESH_SECS = _env_float("IMM_NFL_NFLV_OLD_REFRESH_SECS", 7 * 86400)
# a failed season download is retried after this (the cached file, if any,
# keeps serving meanwhile)
NFLV_RETRY_SECS = _env_float("IMM_NFL_NFLV_RETRY_SECS", 600)
ROSTER_REFRESH_SECS = _env_float("IMM_NFL_ROSTER_REFRESH_SECS", 600)
META_REFRESH_SECS = _env_float("IMM_NFL_META_REFRESH_SECS", 900)
CACHE_DIR = os.environ.get(
    "IMM_NFL_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-logs",
                 "incentive-mm", "nfl_stats"))
NFLV_URL = os.environ.get(
    "IMM_NFL_NFLV_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.csv")
ESPN_ROSTER = ("https://site.web.api.espn.com/apis/site/v2/sports/football/"
               "nfl/teams/{team}/roster")
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HTTP_TIMEOUT = 30.0
POSITIONS = frozenset({"QB", "RB", "FB", "WR", "TE"})
# Kalshi's team codes -> nflverse's / ESPN's (the rest agree, ESPN lowercase)
KALSHI_TO_NFLV = {"JAC": "JAX", "LAR": "LA", "WSH": "WAS"}
NFLV_TO_ESPN = {"WAS": "wsh", "JAX": "jax", "LA": "lar"}
TEAM_CODES = frozenset(
    "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAC JAX KC LAC "
    "LAR LA LV MIA MIN NE NO NYG NYJ PHI PIT SEA SF TB TEN WAS WSH".split())
# an ESPN designation that is NOT one: anything else stands the player aside
CLEAR_STATUSES = frozenset({"", "active"})

# TEAMMATE NEWS (Jack 2026-10-04: "build the teammate adjustment and news
# hold"). Fit on nflverse 2024-26 the way it runs here: a teammate's share
# is his targets (carries) / the team's over its last TEAM_WINDOW_GAMES
# games, games he missed counted as zero -- so one already out for weeks
# has little share left and his absence is not counted twice (the EWMA has
# absorbed it); "absent" = played in one of the team's last 2 games and has
# no line in this one. With V the absent teammates' summed share, a
# remaining player's stat runs x (1 + alpha x V / (1 - V)) of his own
# history's prediction (weighted least squares, weights mu; bootstrap 90%):
#   receptions          targets  alpha 0.18 [0.13, 0.24]  (V > 0.3: +20%)
#   receiving yards     targets  alpha 0.14 [0.08, 0.21]
#   PPR, WR / TE        targets  alpha 0.08 [0.01, 0.17]
#   rushing yards, RB   carries  alpha 0.33 [0.23, 0.48]  (lead back out: +37%)
#   PPR, RB             carries  alpha 0.23 [0.14, 0.35]
# i.e. ~30% of a proportional hand-out of the vacated targets: the rest goes
# to the replacement and elsewhere. A starting QB out (>= 70% of the
# window's attempts; 339 receiver-games): receptions -5%, yards -2.5%, RB
# rushing -9%. ESPN's designation sets the chance he misses (P_MISS).
TEAMMATE_ENABLE = os.environ.get("IMM_NFL_TEAMMATE_ENABLE", "1") == "1"
TEAM_WINDOW_GAMES = int(_env_float("IMM_NFL_TEAM_WINDOW_GAMES", 4))
TEAMMATE_ALPHA: Dict[Tuple[str, str], float] = {
    ("rec", "t"): 0.18, ("recyds", "t"): 0.14, ("ffpts", "t"): 0.08,
    ("rshyds", "c"): 0.33, ("ffpts", "c"): 0.23}
QB_OUT_MULT = {"rec": 0.95, "recyds": 0.975, "rshyds": 0.91, "ffpts": 0.93}
QB_STARTER_SHARE = 0.7
MAX_VACATED = 0.6           # V is clamped here (V / (1 - V) explodes near 1)
PASS_CATCHERS = frozenset({"WR", "TE", "RB", "FB"})
RUSHERS = frozenset({"RB", "FB"})
ESPN_TO_NFLV = {v: k for k, v in NFLV_TO_ESPN.items()}
# a player whose news starts the gate's team hold: this much of the team's
# targets / carries / pass attempts over the window
NEWS_MIN_TARGET_SHARE = _env_float("IMM_NFL_NEWS_MIN_TARGET_SHARE", 0.08)
NEWS_MIN_CARRY_SHARE = _env_float("IMM_NFL_NEWS_MIN_CARRY_SHARE", 0.15)
NEWS_MIN_ATTEMPT_SHARE = _env_float("IMM_NFL_NEWS_MIN_ATTEMPT_SHARE", 0.5)


def material(share: dict) -> bool:
    """A contributor whose news moves the team's props (team_shares row)."""
    return (share.get("t", 0.0) >= NEWS_MIN_TARGET_SHARE
            or share.get("c", 0.0) >= NEWS_MIN_CARRY_SHARE
            or share.get("a", 0.0) >= NEWS_MIN_ATTEMPT_SHARE)


def p_miss(status: str) -> float:
    """The chance a designated player sits, by ESPN's designation."""
    s = (status or "").strip().lower()
    if s in CLEAR_STATUSES:
        return 0.0
    if "doubtful" in s:
        return 0.8
    if "questionable" in s:
        return 0.25
    if "day-to-day" in s or "day to day" in s:
        return 0.1
    if ("out" in s or "reserve" in s or "suspen" in s or "physically" in s
            or "non-football" in s):
        return 1.0
    return 0.5

_NAME_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?")
_SUFFIX_RE = re.compile(r"^(?P<team>[A-Z]{2,3}?)(?P<name>[A-Z]+?)(?P<jersey>\d*)$")


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%SZ')}] nfl-fair: "
          f"{msg}", flush=True)


def norm_name(s: str) -> str:
    """'Michael Pittman Jr.' -> 'michaelpittman'; 'Jaxon Smith-Njigba' ->
    'jaxonsmithnjigba'; 'Amon-Ra St. Brown' -> 'amonrastbrown'."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore")
    s = s.decode().lower()
    s = _NAME_SUFFIX_RE.sub("", s)
    return re.sub(r"[^a-z]", "", s)


def split_event_teams(event_ticker: str) -> Optional[Tuple[str, str]]:
    """'KXNFLESCALATORREC-26OCT05ATLNO' -> ('ATL', 'NO') (away, home)."""
    parts = event_ticker.split("-")
    if len(parts) < 2 or len(parts[1]) < 11:
        return None
    code = parts[1][7:]
    for i in (2, 3):
        a, b = code[:i], code[i:]
        if a in TEAM_CODES and b in TEAM_CODES:
            return a, b
    return None


def parse_player_suffix(ticker: str, teams: Optional[Tuple[str, str]]
                        ) -> Tuple[Optional[str], Optional[int]]:
    """(Kalshi team code, jersey) from 'KX...-26OCT05ATLNO-ATLBROBINSON7' ->
    ('ATL', 7). The team is whichever of the event's two the suffix starts
    with (the longer code first, so 'NYG' never reads as 'NY' + 'G...')."""
    suf = ticker.rsplit("-", 1)[-1]
    m = re.search(r"(\d+)$", suf)
    jersey = int(m.group(1)) if m else None
    team = None
    for t in sorted(teams or (), key=len, reverse=True):
        if suf.startswith(t):
            team = t
            break
    return team, jersey


# ---------------------------------------------------------------- payouts

def payout_spec(m: dict, stat: str, kind: str) -> dict:
    """The market's payout geometry from custom_strike, defaults filled."""
    spec = dict(DEFAULT_SPEC[(stat, kind)])
    cs = m.get("custom_strike") or {}
    for key, field in (("cap", "scalar_cap"), ("floor", "scalar_floor"),
                       ("exponent", "exponent"), ("step", "scalar_step")):
        v = cs.get(field)
        if v in (None, ""):
            continue
        try:
            spec[key] = float(v)
        except (TypeError, ValueError):
            pass
    spec["kind"] = kind
    spec["stat"] = stat
    return spec


def payout(spec: dict, s: float) -> float:
    """YES payout in dollars for stat value s (an integer count / yard / the
    rounded point total). The +1e-6 absorbs float error on exact table rows:
    10000 * (60/200)^3 is 269.99999999999997, and Kalshi pays 0.0270."""
    cap, flo = spec["cap"], spec.get("floor", 0.0)
    x = min(max(s, flo), cap)
    if spec["kind"] == "ladder":
        return (x - flo) / (cap - flo)
    step = spec.get("step") or 1.0
    x = math.floor(x / step + 1e-9) * step
    frac = (x - flo) / (cap - flo)
    return math.floor(10000.0 * frac ** spec.get("exponent", 3.0) + 1e-6) / 10000.0


# ---------------------------------------------------------- distributions

def _gammp(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x) (Numerical Recipes)."""
    if x <= 0.0:
        return 0.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        ap, s = a, 1.0 / a
        d = s
        for _ in range(1000):
            ap += 1.0
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-13:
                break
        return min(1.0, s * math.exp(-x + a * math.log(x) - gln))
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-13:
            break
    return max(0.0, 1.0 - math.exp(-x + a * math.log(x) - gln) * h)


def stat_pmf(stat: str, mu: float, kmax: int) -> List[float]:
    """P(stat = k), k = 0..kmax, the last cell holding the upper tail.
    Discrete stats: NB1 with var = phi * mu. Continuous ones: a gamma with
    mean mu and var phi * mu, binned to the nearest integer (yards are
    whole; Kalshi rounds PPR points half up), everything under 0.5 at 0."""
    sm = STAT_MODEL[stat]
    phi = sm["phi"]
    mu = max(mu, 1e-6)
    out = [0.0] * (kmax + 1)
    if sm["discrete"]:
        if phi <= 1.0 + 1e-9:                       # Poisson
            lp = -mu
            for k in range(kmax + 1):
                out[k] = math.exp(lp)
                lp += math.log(mu) - math.log(k + 1)
        else:
            p = 1.0 / phi
            r = mu * p / (1.0 - p)
            base = math.lgamma(r)
            for k in range(kmax + 1):
                out[k] = math.exp(math.lgamma(k + r) - base - math.lgamma(k + 1)
                                  + r * math.log(p) + k * math.log1p(-p))
        tail = max(0.0, 1.0 - sum(out[:kmax]))
        out[kmax] = tail
        return out
    shape, scale = mu / phi, phi
    prev = 0.0
    for k in range(kmax):
        c = _gammp(shape, (k + 0.5) / scale)
        out[k] = max(0.0, c - prev)
        prev = c
        if 1.0 - c < 1e-10:
            break
    out[kmax] = max(0.0, 1.0 - prev)
    return out


@functools.lru_cache(maxsize=8192)
def _pmf_cached(stat: str, mu: float, kmax: int) -> Tuple[float, ...]:
    """stat_pmf, shared by a player's ladder and escalator on one stat (the
    yards gamma is ~10 ms of pure Python a call)."""
    return tuple(stat_pmf(stat, mu, kmax))


def expected_payout(spec: dict, mu: float) -> float:
    """E[YES payout] in dollars for a player whose stat has mean mu."""
    kmax = int(spec["cap"]) if spec["stat"] in ("rec", "ffpts") else 400
    kmax = max(kmax, int(spec["cap"]))
    pm = _pmf_cached(spec["stat"], round(mu, 6), kmax)
    return sum(p * payout(spec, k) for k, p in enumerate(pm) if p > 0.0)


# ------------------------------------------------------------ player data

def predict_mu(games: List[dict], stat: str, season: int
               ) -> Optional[Tuple[float, float, int, int]]:
    """(mu, raw ewma, games, games this season) from the player's games in
    time order, or None with none. Negative yards count as 0 (Kalshi pays
    nothing below zero, and the calibration fit the clipped stat)."""
    sm = STAT_MODEL[stat]
    clip = not sm["discrete"] and stat != "ffpts"
    vals = []
    for g in games:
        v = g.get(sm["col"])
        if v is None:
            continue
        v = float(v)
        vals.append((max(0.0, v) if clip else v, int(g["season"])))
    n = len(vals)
    if n == 0:
        return None
    num = den = 0.0
    for i, (v, s) in enumerate(vals):
        ago = n - 1 - i
        w = 0.5 ** (ago / HALF_LIFE_GAMES)
        if s != season:
            w *= PRIOR_SEASON_WEIGHT
        num += w * v
        den += w
    raw = num / den
    mu = sm["a"] * raw + sm["b"]
    return mu, raw, n, sum(1 for _v, s in vals if s == season)


def recent_mean(games: List[dict], stat: str, k: int = 4) -> Optional[float]:
    """The stat's mean over the player's last k games (negative yards as 0,
    like predict_mu); None under 3 games carrying it."""
    sm = STAT_MODEL[stat]
    clip = not sm["discrete"] and stat != "ffpts"
    vals = []
    for g in games[-k:]:
        v = g.get(sm["col"])
        if v is None or v == "":
            continue
        v = float(v)
        vals.append(max(0.0, v) if clip else v)
    return sum(vals) / len(vals) if len(vals) >= 3 else None


def fair_band(spec: dict, mu: float, n_games: int) -> Tuple[float, float, float]:
    """(fair, fair_lo, fair_hi) in dollars; a short history widens the band."""
    lo, hi = (BAND_LO, BAND_HI) if n_games >= FULL_GAMES else (WIDE_LO, WIDE_HI)
    return (expected_payout(spec, mu), expected_payout(spec, mu * lo),
            expected_payout(spec, mu * hi))


_NFLV_COLS = ("player_id", "player_display_name", "position", "season", "week",
              "season_type", "team", "receptions", "receiving_yards",
              "rushing_yards", "fantasy_points_ppr", "targets", "carries",
              "attempts")


def parse_nflverse(text: str) -> List[dict]:
    """Rows of one stats_player_week CSV, skill positions only."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("position") not in POSITIONS:
            continue
        try:
            row = {"player_id": r["player_id"],
                   "name": r.get("player_display_name") or "",
                   "position": r["position"],
                   "season": int(r["season"]), "week": int(r["week"]),
                   "season_type": r.get("season_type") or "REG",
                   "team": r.get("team") or ""}
        except (KeyError, TypeError, ValueError):
            continue
        for c in _NFLV_COLS[7:]:
            v = r.get(c)
            try:
                row[c] = float(v) if v not in (None, "", "NA") else 0.0
            except ValueError:
                row[c] = 0.0
        out.append(row)
    return out


class PlayerIndex:
    """nflverse rows grouped by player, plus a name -> ids lookup."""

    def __init__(self, rows: List[dict]):
        self.games: Dict[str, List[dict]] = {}
        # team -> (season, week) -> that game's rows, for the teammate shares
        self.team_games: Dict[str, Dict[Tuple[int, int], List[dict]]] = {}
        for r in rows:
            self.games.setdefault(r["player_id"], []).append(r)
            self.team_games.setdefault(r.get("team") or "", {}).setdefault(
                (r["season"], r["week"]), []).append(r)
        self.by_name: Dict[str, List[str]] = {}
        self.position: Dict[str, str] = {}
        for pid, gs in self.games.items():
            # POST weeks are numbered after REG in nflverse (19-22)
            gs.sort(key=lambda g: (g["season"], g["week"]))
            key = norm_name(gs[-1]["name"])
            self.by_name.setdefault(key, []).append(pid)
            self.position[pid] = gs[-1].get("position") or ""

    def team_shares(self, team: str, k: int = TEAM_WINDOW_GAMES
                    ) -> Dict[str, dict]:
        """pid -> {t, c, a: his share of the team's targets / carries / pass
        attempts over its last k games (a missed game counts as zero),
        recent: he has a line in one of the last 2} -- nflverse team code."""
        games = self.team_games.get(team) or {}
        keys = sorted(games)[-k:]
        tot = {"targets": 0.0, "carries": 0.0, "attempts": 0.0}
        per: Dict[str, Dict[str, float]] = {}
        for key in keys:
            for r in games[key]:
                d = per.setdefault(r["player_id"], dict.fromkeys(tot, 0.0))
                for c in tot:
                    v = float(r.get(c) or 0.0)
                    d[c] += v
                    tot[c] += v
        recent = {r["player_id"] for key in keys[-2:] for r in games[key]}
        return {pid: {"t": d["targets"] / tot["targets"] if tot["targets"] else 0.0,
                      "c": d["carries"] / tot["carries"] if tot["carries"] else 0.0,
                      "a": d["attempts"] / tot["attempts"] if tot["attempts"] else 0.0,
                      "recent": pid in recent}
                for pid, d in per.items()}

    def find(self, name: str, team: Optional[str]) -> Tuple[Optional[str], str]:
        """(player_id, '') or (None, why). Ties break on the latest team."""
        ids = self.by_name.get(norm_name(name)) or []
        if not ids:
            return None, f"no nflverse games for {name!r}"
        if len(ids) == 1:
            return ids[0], ""
        nt = KALSHI_TO_NFLV.get(team or "", team or "")
        on_team = [p for p in ids if self.games[p][-1]["team"] == nt]
        if len(on_team) == 1:
            return on_team[0], ""
        return None, f"{len(ids)} nflverse players named {name!r}"


# ---------------------------------------------------------------- the watch

def _kalshi_read(get_json, path: str, params: dict) -> dict:
    if get_json is not None:
        try:
            js = get_json(path, dict(params))
            if isinstance(js, dict):
                return js
        except Exception as e:                       # noqa: BLE001
            _log(f"signed read failed ({type(e).__name__}: {str(e)[:80]}); "
                 f"public endpoint")
    last = None
    for attempt in range(3):
        try:
            r = requests.get(KALSHI_BASE + path, params=params,
                             timeout=HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(2.0 * (attempt + 1) ** 2)
    raise RuntimeError(f"Kalshi {path} failed: {last}")


def open_markets(get_json=None) -> List[dict]:
    out: List[dict] = []
    for series in SERIES:
        cursor = None
        for _ in range(10):
            p = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            js = _kalshi_read(get_json, "/markets", p)
            out.extend(js.get("markets") or [])
            cursor = js.get("cursor")
            if not cursor:
                break
    return out


def _espn_ts(s) -> Optional[float]:
    """'2026-10-03T14:42Z' -> epoch seconds (None when unreadable)."""
    try:
        return datetime.strptime(str(s)[:16], "%Y-%m-%dT%H:%M").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def roster_status(js: dict) -> Dict[int, List[dict]]:
    """jersey -> [{name, key, status, roster, position, injury_date}] from an
    ESPN team roster (injury_date: when the designation was set, epoch)."""
    out: Dict[int, List[dict]] = {}
    for grp in js.get("athletes") or []:
        for a in grp.get("items") or []:
            try:
                jersey = int(a.get("jersey"))
            except (TypeError, ValueError):
                continue
            inj = a.get("injuries") or []
            first = (inj[0] or {}) if inj else {}
            status = str(first.get("status") or "")
            out.setdefault(jersey, []).append({
                "name": a.get("fullName") or a.get("displayName") or "",
                "key": norm_name(a.get("fullName") or ""),
                "status": status,
                "roster": str(((a.get("status") or {}).get("type")) or ""),
                "position": str(((a.get("position") or {}).get("abbreviation"))
                                or ""),
                "injury_date": _espn_ts(first.get("date")) if inj else None})
    return out


def team_context(espn_team: str, ros: Optional[dict],
                 index: Optional[PlayerIndex],
                 changed_at: Optional[Dict[Tuple[str, str], float]] = None
                 ) -> dict:
    """A team's designated contributors and latest news, from its ESPN roster
    read ({ts, roster}) and nflverse: absent = [{pid, name, position,
    status, p (= P_MISS), t, c, a (window shares)}] for every QB / WR / TE /
    RB with a designation who has a share in the team's window; news_at =
    the latest designation time or observed status change (a player cleared
    counts too); news = what it was."""
    ctx: dict = {"absent": [], "news_at": None, "news": None}
    if ros is None or index is None:
        return ctx
    nt = ESPN_TO_NFLV.get(espn_team, espn_team.upper())
    shares = index.team_shares(nt)
    changed_at = changed_at or {}
    for entries in (ros.get("roster") or {}).values():
        for a in entries:
            pos = a.get("position") or ""
            if pos not in POSITIONS:
                continue
            st = (a.get("status") or "").strip()
            designated = st.lower() not in CLEAR_STATUSES
            pid, _why = index.find(a["name"], nt)
            sh = shares.get(pid) if pid else None
            if sh is not None and material(sh):
                # news on a contributor holds the team; a fringe player's
                # (no share, or under NEWS_MIN_*) does not
                stamps = [changed_at.get((espn_team, a["key"]))]
                if designated:
                    stamps.append(a.get("injury_date"))
                stamps = [x for x in stamps if x is not None]
                if stamps and (ctx["news_at"] is None or max(stamps) > ctx["news_at"]):
                    ctx["news_at"] = max(stamps)
                    ctx["news"] = f"{a['name']} {pos} {st or 'cleared'}"
            if not designated or sh is None or not sh["recent"]:
                # absent = played in one of the team's last 2 games (the
                # fit's definition): one out longer is already in the EWMAs
                continue
            ctx["absent"].append({"pid": pid, "name": a["name"], "position": pos,
                                  "status": st, "p": p_miss(st),
                                  "t": round(sh["t"], 4), "c": round(sh["c"], 4),
                                  "a": round(sh["a"], 4)})
    return ctx


def teammate_mult(stat: str, pos: str, pid: Optional[str], ctx: Optional[dict]
                  ) -> Tuple[float, dict]:
    """(multiplier on the player's mean, detail) from his teammates' absences
    (the player's own designation is the gate's business, not this). QBs and
    unknown positions are left alone (the fit covered WR / TE / RB)."""
    if not TEAMMATE_ENABLE or not ctx or pos not in PASS_CATCHERS:
        return 1.0, {}
    others = [x for x in ctx.get("absent") or [] if x["pid"] != pid]
    if not others:
        return 1.0, {}
    v_t = min(MAX_VACATED, sum(x["p"] * x["t"] for x in others
                               if x["position"] in PASS_CATCHERS))
    v_c = min(MAX_VACATED, sum(x["p"] * x["c"] for x in others
                               if x["position"] in RUSHERS))
    qb = max((x["p"] for x in others
              if x["position"] == "QB" and x["a"] >= QB_STARTER_SHARE), default=0.0)
    if stat == "rshyds":
        chan, v = ("c", v_c) if pos in RUSHERS else (None, 0.0)
    elif stat == "ffpts":
        chan, v = ("c", v_c) if pos in RUSHERS else ("t", v_t)
    else:
        chan, v = "t", v_t
    m = 1.0
    if chan is not None and v > 0:
        m *= 1.0 + TEAMMATE_ALPHA[(stat, chan)] * v / (1.0 - v)
    if qb > 0:
        m *= 1.0 - qb * (1.0 - QB_OUT_MULT[stat])
    detail = {"v_t": round(v_t, 4), "v_c": round(v_c, 4), "qb_out": qb,
              "absent": [f"{x['name']} {x['position']} {x['status']} "
                         f"(t {x['t']:.0%}, c {x['c']:.0%})" for x in others]}
    return m, detail


def last_name_key(name: str) -> str:
    """Everything after the first name: 'Amon-Ra St. Brown' -> 'stbrown',
    'Michael Pittman Jr.' -> 'pittman'."""
    parts = (name or "").split()
    return norm_name(" ".join(parts[1:])) if len(parts) > 1 else norm_name(name)


def find_on_roster(roster: Dict[int, List[dict]], name: str,
                   jersey: Optional[int]) -> Optional[dict]:
    """The roster entry for this player: the ticker's jersey with the full or
    the last name (ESPN 'Hollywood Brown' vs Kalshi 'Marquise Brown'), else
    the one athlete anywhere on the roster with the full name."""
    key, last = norm_name(name), last_name_key(name)
    if jersey is not None:
        same = roster.get(jersey, [])
        for a in same:
            if a["key"] == key:
                return a
        for a in same:
            if last and last_name_key(a["name"]) == last:
                return a
    hits = [a for entries in roster.values() for a in entries if a["key"] == key]
    return hits[0] if len(hits) == 1 else None


class NflPropWatch:
    """The refresher's state: open markets every META_REFRESH_SECS, nflverse
    seasons on their own clocks, ESPN rosters every ROSTER_REFRESH_SECS;
    `snap` is what the gate reads."""

    def __init__(self, get_json: Optional[Callable[[str, dict], dict]] = None,
                 session=None, cache_dir: str = CACHE_DIR,
                 season: Optional[int] = None):
        self.get_json = get_json
        self.session = session if session is not None else requests.Session()
        self.session.headers.setdefault("User-Agent", "Mozilla/5.0")
        self.cache_dir = cache_dir
        self.season = season
        self.meta_at = 0.0
        self.family: List[dict] = []
        self.nflv_at: Dict[int, float] = {}
        self.nflv_retry_at: Dict[int, float] = {}
        self.nflv_rows: Dict[int, List[dict]] = {}
        self.index: Optional[PlayerIndex] = None
        self.rosters: Dict[str, dict] = {}       # espn team -> {ts, roster}
        # (espn team, player key) -> last status seen / when it last changed
        self.status_seen: Dict[Tuple[str, str], str] = {}
        self.status_changed_at: Dict[Tuple[str, str], float] = {}
        self.fair_cache: Dict[tuple, Tuple[float, float, float]] = {}
        self.snap: Optional[dict] = None

    def current_season(self, now: datetime) -> int:
        if self.season:
            return self.season
        # the NFL season is named for the year it starts (Sep -> Feb)
        return now.year if now.month >= 6 else now.year - 1

    # -- nflverse
    def _cache_path(self, season: int) -> str:
        return os.path.join(self.cache_dir, f"stats_player_week_{season}.csv")

    def _load_season(self, season: int, max_age: float, now_ts: float,
                     errors: List[str]) -> bool:
        """Re-read one season when due; True when its rows changed."""
        if now_ts < self.nflv_retry_at.get(season, 0.0):
            return False
        if season in self.nflv_rows and now_ts - self.nflv_at.get(season, 0) < max_age:
            return False
        path = self._cache_path(season)
        text = None
        try:
            fresh = os.path.exists(path) and now_ts - os.path.getmtime(path) < max_age
        except OSError:
            fresh = False
        if not fresh:
            try:
                r = self.session.get(NFLV_URL.format(season=season),
                                     timeout=HTTP_TIMEOUT)
                if r.status_code == 404:
                    errors.append(f"nflverse {season}: not published")
                    self.nflv_retry_at[season] = now_ts + NFLV_RETRY_SECS
                else:
                    r.raise_for_status()
                    text = r.text
                    os.makedirs(self.cache_dir, exist_ok=True)
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8", newline="") as f:
                        f.write(text)
                    os.replace(tmp, path)
            except Exception as e:                   # noqa: BLE001
                errors.append(f"nflverse {season}: {type(e).__name__}: "
                              f"{str(e)[:80]}")
                self.nflv_retry_at[season] = now_ts + NFLV_RETRY_SECS
        if text is None and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError as e:
                errors.append(f"nflverse {season} cache: {e}")
        if text is None:
            return False
        rows = parse_nflverse(text)
        self.nflv_at[season] = now_ts
        if rows == self.nflv_rows.get(season):
            return False
        self.nflv_rows[season] = rows
        return True

    def _refresh_history(self, now_ts: float, season: int,
                         errors: List[str]) -> None:
        changed = False
        for s in range(season - SEASONS_BACK, season + 1):
            age = NFLV_REFRESH_SECS if s == season else NFLV_OLD_REFRESH_SECS
            changed |= self._load_season(s, age, now_ts, errors)
        if changed or self.index is None:
            rows = [r for s in sorted(self.nflv_rows) for r in self.nflv_rows[s]]
            self.index = PlayerIndex(rows)
            self.fair_cache.clear()
            _log(f"history: {len(rows)} player-games, seasons "
                 f"{sorted(self.nflv_rows)}")

    # -- rosters
    def _refresh_rosters(self, teams: List[str], now_ts: float,
                         errors: List[str]) -> None:
        for t in sorted(set(teams)):
            ent = self.rosters.get(t)
            if ent and now_ts - ent["ts"] < ROSTER_REFRESH_SECS:
                continue
            try:
                r = self.session.get(ESPN_ROSTER.format(team=t), timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                ros = roster_status(r.json())
                self._track_status(t, ros, now_ts)
                self.rosters[t] = {"ts": now_ts, "roster": ros}
            except Exception as e:                   # noqa: BLE001
                # the last good read stays; its ts ages out in the gate
                errors.append(f"roster {t}: {type(e).__name__}: {str(e)[:80]}")

    def _track_status(self, team: str, roster: Dict[int, List[dict]],
                      now_ts: float) -> None:
        """Stamp a QB / WR / TE / RB whose designation changed since the last
        read (ESPN's own injury date can lag or miss a clearing); the first
        sighting of a player stamps nothing."""
        for entries in roster.values():
            for a in entries:
                if a.get("position") not in POSITIONS:
                    continue
                k = (team, a["key"])
                cur = (a.get("status") or "").strip().lower()
                prev = self.status_seen.get(k)
                if prev is not None and prev != cur:
                    self.status_changed_at[k] = now_ts
                    _log(f"{team}: {a['name']} {prev or 'clear'} -> "
                         f"{cur or 'clear'}")
                self.status_seen[k] = cur

    def refresh(self, now_ts: Optional[float] = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        season = self.current_season(now)
        errors: List[str] = []
        if now_ts - self.meta_at >= META_REFRESH_SECS or not self.family:
            try:
                self.family = open_markets(self.get_json)
                self.meta_at = now_ts
            except Exception as e:                   # noqa: BLE001
                errors.append(f"meta: {type(e).__name__}: {str(e)[:100]}")
        self._refresh_history(now_ts, season, errors)
        teams = []
        for m in self.family:
            tm = split_event_teams(m.get("event_ticker") or "")
            team, _j = parse_player_suffix(m.get("ticker") or "", tm)
            if team:
                nt = KALSHI_TO_NFLV.get(team, team)
                teams.append(NFLV_TO_ESPN.get(nt, nt.lower()))
        self._refresh_rosters(teams, now_ts, errors)
        self.snap = build_snapshot(now_ts, season, self.family, self.index,
                                   self.rosters, self.fair_cache, errors,
                                   changed_at=self.status_changed_at)
        return self.snap


def market_entry(m: dict, season: int, index: Optional[PlayerIndex],
                 rosters: Dict[str, dict], fair_cache: Dict[tuple, tuple],
                 ctx: Optional[dict] = None) -> dict:
    t = m.get("ticker") or ""
    series = t.split("-", 1)[0]
    stat, kind = SERIES[series]
    teams = split_event_teams(m.get("event_ticker") or t.rsplit("-", 1)[0])
    team, jersey = parse_player_suffix(t, teams)
    name = str(m.get("yes_sub_title") or "").strip()
    e = {"stat": stat, "kind": kind, "player": name, "team": team,
         "jersey": jersey}
    if not name or team is None:
        e["err"] = f"cannot read the player from {t}"
        return e
    nt = KALSHI_TO_NFLV.get(team, team)
    et = NFLV_TO_ESPN.get(nt, nt.lower())
    ros = rosters.get(et)
    if ctx and ctx.get("news_at") is not None:
        e["news_at"], e["news"] = ctx["news_at"], ctx["news"]
    if ros is not None:
        e["roster_ts"] = ros["ts"]
        a = find_on_roster(ros["roster"], name, jersey)
        if a is None:
            e["injury"] = "not on roster"
        else:
            st = a["status"].strip()
            e["injury"] = None if st.lower() in CLEAR_STATUSES else st
    if index is None:
        e["err"] = "no nflverse history loaded"
        return e
    pid, why = index.find(name, team)
    if pid is None:
        e["err"] = why
        return e
    games = index.games[pid]
    e["pid"] = pid
    pred = predict_mu(games, stat, season)
    if pred is None:
        e["err"] = "no games"
        return e
    mu, raw, n, n_cur = pred
    # teammates' absences scale the player's own mean (TEAMMATE_ALPHA)
    tm, detail = teammate_mult(stat, index.position.get(pid, ""), pid, ctx)
    e.update(mu=round(mu * tm, 4), mu_base=round(mu, 4), ewma=round(raw, 4),
             n_games=n, n_season=n_cur,
             last_game=f"{games[-1]['season']}w{games[-1]['week']}")
    mu = mu * tm
    if tm != 1.0:
        e["team_mult"] = round(tm, 4)
        e["teammates"] = detail
    if n < MIN_GAMES:
        e["err"] = f"only {n} games of history (min {MIN_GAMES})"
        return e
    spec = payout_spec(m, stat, kind)
    ck = (series, spec["cap"], spec.get("exponent"), spec.get("step"),
          round(mu, 4), n >= FULL_GAMES)
    fb = fair_cache.get(ck)
    if fb is None:
        fb = fair_band(spec, mu, n)
        fair_cache[ck] = fb
    e["fair"], e["fair_lo"], e["fair_hi"] = (round(x, 6) for x in fb)
    # the same band on the last RECENT_GAMES games (see RECENT_GAMES)
    rm = recent_mean(games, stat, RECENT_GAMES) if RECENT_GAMES > 0 else None
    if rm is not None:
        rmu = rm * tm
        rk = (series, spec["cap"], spec.get("exponent"), spec.get("step"),
              round(rmu, 4), n >= FULL_GAMES)
        rb = fair_cache.get(rk)
        if rb is None:
            rb = fair_band(spec, rmu, n)
            fair_cache[rk] = rb
        e["recent_mean"] = round(rm, 4)
        e["fair_recent"], e["fair_recent_lo"], e["fair_recent_hi"] = (
            round(x, 6) for x in rb)
    return e


def build_snapshot(now_ts: float, season: int, family: List[dict],
                   index: Optional[PlayerIndex], rosters: Dict[str, dict],
                   fair_cache: Dict[tuple, tuple],
                   errors: Optional[List[str]] = None,
                   changed_at: Optional[Dict[Tuple[str, str], float]] = None
                   ) -> dict:
    entries: Dict[str, dict] = {}
    contexts: Dict[str, dict] = {}
    for m in family:
        t = m.get("ticker")
        if not t or t.split("-", 1)[0] not in SERIES:
            continue
        try:
            team, _j = parse_player_suffix(t, split_event_teams(
                m.get("event_ticker") or t.rsplit("-", 1)[0]))
            ctx = None
            if team:
                nt = KALSHI_TO_NFLV.get(team, team)
                et = NFLV_TO_ESPN.get(nt, nt.lower())
                if et not in contexts:
                    contexts[et] = team_context(et, rosters.get(et), index,
                                                changed_at)
                ctx = contexts[et]
            entries[t] = market_entry(m, season, index, rosters, fair_cache, ctx)
        except Exception as e:                       # noqa: BLE001
            entries[t] = {"err": f"{type(e).__name__}: {str(e)[:120]}"}
    return {
        "ts": now_ts,
        "fetched_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "season": season,
        "markets": entries,
        "rosters": {k: v["ts"] for k, v in sorted(rosters.items())},
        "teams": {k: v for k, v in sorted(contexts.items())
                  if v["absent"] or v["news_at"] is not None},
        "errors": list(errors or []),
        "model": {"half_life_games": HALF_LIFE_GAMES,
                  "prior_season_weight": PRIOR_SEASON_WEIGHT,
                  "band": [BAND_LO, BAND_HI], "wide_band": [WIDE_LO, WIDE_HI],
                  "min_games": MIN_GAMES, "full_games": FULL_GAMES,
                  "recent_games": RECENT_GAMES,
                  "stats": STAT_MODEL,
                  "teammates": {"enable": TEAMMATE_ENABLE,
                                "window_games": TEAM_WINDOW_GAMES,
                                "alpha": {f"{k[0]}/{k[1]}": v
                                          for k, v in TEAMMATE_ALPHA.items()},
                                "qb_out": QB_OUT_MULT,
                                "max_vacated": MAX_VACATED}},
    }


def write_status(path: str, snap: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


# -------------------------------------------------------------------- CLI

def _book(m: dict) -> Tuple[Optional[float], Optional[float]]:
    def c(k):
        try:
            v = float(m.get(k))
        except (TypeError, ValueError):
            return None
        return v * 100.0 if 0.0 < v < 1.0 else None
    return c("yes_bid_dollars"), c("yes_ask_dollars")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", help="one market (prints the distribution)")
    ap.add_argument("--event", help="one event ticker")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    w = NflPropWatch()
    snap = w.refresh()
    books = {m["ticker"]: _book(m) for m in w.family}
    rows = sorted(snap["markets"].items())
    if args.ticker:
        rows = [(t, e) for t, e in rows if t == args.ticker]
    elif args.event:
        rows = [(t, e) for t, e in rows if t.startswith(args.event)]
    if args.json:
        print(json.dumps({t: dict(e, book=books.get(t)) for t, e in rows},
                         indent=1, default=str))
        return 0
    print(f"{'ticker':58s} {'player':22s} {'mu':>6s} {'n':>3s} {'fair':>6s} "
          f"{'band':>13s} {'book':>13s}  flag")
    for t, e in rows:
        b, a = books.get(t, (None, None))
        f, lo, hi = (e.get(k) for k in ("fair", "fair_lo", "fair_hi"))
        flag = e.get("err") or (f"INJURY {e['injury']}" if e.get("injury") else "")
        if f is not None and not flag:
            if b is not None and b > hi * 100 + 1:
                flag = "BID ABOVE BAND"
            elif a is not None and a < lo * 100 - 1:
                flag = "ASK BELOW BAND"
        if e.get("team_mult"):
            flag += f" team x{e['team_mult']:.3f}"
        if e.get("news_at") and time.time() - e["news_at"] < 3600:
            flag += (f" NEWS {e.get('news')} "
                     f"{(time.time() - e['news_at']) / 60:.0f}m ago")
        fs = f"{f * 100:6.2f}" if f is not None else "     -"
        bs = f"{lo * 100:5.2f}-{hi * 100:5.2f}" if f is not None else ""
        bk = (f"{b:5.2f}/{a:5.2f}" if b is not None and a is not None
              else f"{b}/{a}")
        mu = e.get("mu")
        print(f"{t:58s} {e.get('player', '')[:22]:22s} "
              f"{(f'{mu:6.2f}' if mu is not None else '     -')} "
              f"{e.get('n_games', 0):3d} {fs} {bs:>13s} {bk:>13s}  {flag}")
    if args.ticker and rows and rows[0][1].get("fair") is not None:
        t, e = rows[0]
        m = next(x for x in w.family if x["ticker"] == t)
        spec = payout_spec(m, e["stat"], e["kind"])
        yards = e["stat"] in ("recyds", "rshyds")
        pm = stat_pmf(e["stat"], e["mu"], 400 if yards else int(spec["cap"]))
        print(f"\n{t}: mu {e['mu']:.2f} ({e['n_games']} games, last "
              f"{e['last_game']}), fair {e['fair'] * 100:.2f}c")
        width = 10 if yards else 1
        for k0 in range(0, len(pm), width):
            p = sum(pm[k0:k0 + width])
            if p < 5e-4:
                continue
            lbl = f"{k0}-{k0 + width - 1}" if width > 1 else f"{k0}"
            print(f"  {lbl:>8s}: P {p:6.3f}  pays {payout(spec, k0):.4f}")
    if snap["errors"]:
        print("errors:", "; ".join(snap["errors"])[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
