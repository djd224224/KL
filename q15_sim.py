#!/usr/bin/env python3
r"""Shadow sim of the 15-minute test (Q15_SHADOW.md, Q15_PREREG.md; Jack
2026-10-22: "Yes use a live price feed like Miami temp, with recorder and a
week of testing" / "It's paid, do FX and crypto").

ONE pass over an ET day of q15_collector's recording (KL-data\q15-shadow):
  books   every market's order book rebuilt from the ws stream
          (temp_shadow_scoring's snapshot / delta parsers), trusted only after
          a snapshot and untrusted again by every connect / disconnect / gap
          marker; evaluated each second as of rx <= s + 0.5
  fair    FX: P(S_T >= S_0) of a driftless walk on the log of Kraken's mid
          (EWMA sigma, half-life 10 min, floored at 0.2 x the running median
          sigma; a half-tick for "at least"). The race: P(coin leads) by 2,000
          Monte Carlo draws of the remaining 5-coin returns (EWMA covariance
          of 5-s log returns of Hyperliquid's mids), every 5 s.
  quotes  the pre-registered variants (VARIANTS) each second, never crossing
          the book; 100 per side; |position| capped at POS_CAP per market
  reward  our quotes overlaid on the book, scored by temp_shadow_scoring.score
          (the IMM's exact scorer) at the program's target / discount, x the
          program's pool per second
  fills   STRESS: a trade at our price or through it fills our whole resting
          size at our price (the side re-quotes next second); marked to
          Kalshi's result (0 / 100c)
  checks  Brier of the fair vs the market mid at fixed times to close; the
          proxy's own up / down call against Kalshi's FX results
-> KL-data\q15-shadow\sim\<ET day>\q15_day.json. Read-only; never trades.

usage: python q15_sim.py --day YYYY-MM-DD [--echo]
"""
import os
import sys

DATA = os.environ.get("Q15_SHADOW_DATA", r"C:\Users\jackd\Documents\KL-data\q15-shadow")
os.environ["TEMP_SHADOW_DATA"] = DATA

import argparse  # noqa: E402
import bisect  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import time  # noqa: E402
from collections import defaultdict  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from typing import Dict, List, Optional, Tuple  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402
import pytz  # noqa: E402
import temp_shadow_common as C  # noqa: E402
import temp_shadow_scoring as S  # noqa: E402

ET = pytz.timezone("US/Eastern")
FX_SERIES = {"KXEURUSD15M": "EURUSD", "KXGBPUSD15M": "GBPUSD", "KXUSDJPY15M": "USDJPY",
             "KXAUDUSD15M": "AUDUSD", "KXUSDCAD15M": "USDCAD"}
FX_TICK = {"EURUSD": 1e-5, "GBPUSD": 1e-5, "AUDUSD": 1e-5, "USDCAD": 1e-5, "USDJPY": 1e-3}
RACE_SERIES = "KXCRYPTOLEAD15M"
RACE_COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")
SIZE = 100.0
POS_CAP = 300.0
MIN_TAU = 60.0
FX_STALE, RACE_STALE = 5.0, 15.0
SIGMA_HALF_LIFE = 600.0
RACE_EVERY = 5
RACE_DRAWS = 2000
BRIER_TAUS = (600, 300, 120, 60, 30)
NIGHT_HOURS = set(range(20, 24)) | set(range(0, 7))     # ET, 20:00-07:00

# id -> (kind, margin cents, night only)  -- Q15_PREREG.md, fixed before data
VARIANTS = {"FAIR4": ("fair", 4, False), "FAIR4_night": ("fair", 4, True),
            "FAIR8": ("fair", 8, False), "FAIR8_night": ("fair", 8, True),
            "JOIN": ("join", 6, False), "FAV_night": ("fav", 0, True)}


def day_window(day: date) -> Tuple[float, float]:
    a = ET.localize(datetime(day.year, day.month, day.day))
    b = ET.localize(datetime(day.year, day.month, day.day) + timedelta(days=1))
    return a.timestamp(), b.timestamp()


def phi(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def fx_p_up(s_t: float, s_0: float, sigma: float, tau: float, tick: float) -> float:
    """P(S_T >= S_0): ties resolve Yes, so half a tick of room."""
    if s_t <= 0 or s_0 <= 0:
        return 0.5
    if tau <= 0 or sigma <= 0:
        return 1.0 if s_t >= s_0 else 0.0
    z = (math.log(s_t / s_0) + 0.5 * tick / s_0) / (sigma * math.sqrt(tau))
    return phi(z)


def race_probs(done: np.ndarray, cov5: np.ndarray, tau: float, rng: np.random.Generator,
               draws: int = RACE_DRAWS) -> np.ndarray:
    """P(each coin finishes with the highest return): `done` the returns since
    the open, `cov5` the covariance of 5-s log returns, tau seconds left."""
    n = len(done)
    if tau <= 0:
        out = np.zeros(n)
        out[int(np.argmax(done))] = 1.0
        return out
    cov = cov5 * (tau / 5.0)
    try:
        L = np.linalg.cholesky(cov + np.eye(n) * 1e-14)
    except np.linalg.LinAlgError:
        L = np.diag(np.sqrt(np.maximum(np.diag(cov), 1e-14)))
    final = done[None, :] + rng.standard_normal((draws, n)) @ L.T
    win = np.bincount(np.argmax(final, axis=1), minlength=n)
    return win / float(draws)


# ---- inputs -----------------------------------------------------------------------

def load_meta(a: float, b: float):
    """The day's markets (close in (a, b]), their results and programs."""
    markets: Dict[str, dict] = {}
    programs: Dict[str, dict] = {}
    for rec in C.iter_raw("meta", a - 6 * 3600, b + 2 * 3600):
        k = rec.get("kind")
        if k == "market":
            m = rec.get("market") or {}
            if m.get("ticker"):
                markets[m["ticker"]] = {**markets.get(m["ticker"], {}), **m}
        elif k == "programs":
            for p in rec.get("programs") or []:
                programs[p.get("market_ticker")] = p
    keep = {}
    for t, m in markets.items():
        c = C.parse_iso(m.get("close_time")) or 0
        o = C.parse_iso(m.get("open_time")) or 0
        if a < c <= b and o:
            sf = os.path.join(DATA, "settled", f"{t}.json")
            res = m.get("result")
            js = C.read_json(sf)
            if js and (js.get("market") or {}).get("result"):
                res = js["market"]["result"]
            keep[t] = {"ticker": t, "series": t.split("-")[0], "event": t.rsplit("-", 1)[0],
                       "open": o, "close": c, "result": res if res in ("yes", "no") else None,
                       "program": programs.get(t)}
    return keep


def load_px(a: float, b: float) -> Dict[str, Tuple[List[float], List[float]]]:
    """symbol -> (rx list, mid list) from the px stream (kraken FX, hl coins)."""
    out: Dict[str, Tuple[List[float], List[float]]] = defaultdict(lambda: ([], []))
    for rec in C.iter_raw("px", a, b):
        src = rec.get("src")
        if src not in ("kraken", "hl"):
            continue
        rx = float(rec["rx"])
        for sym, v in (rec.get("px") or {}).items():
            try:
                mid = float(v[0])
            except (TypeError, ValueError, IndexError):
                continue
            ts, xs = out[sym]
            ts.append(rx)
            xs.append(mid)
    return dict(out)


class Series:
    """A forward-filled per-second price with its age."""

    def __init__(self, ts: List[float], xs: List[float]):
        self.ts, self.xs = ts, xs

    def at(self, t: float) -> Tuple[Optional[float], float]:
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0:
            return None, float("inf")
        return self.xs[i], t - self.ts[i]


class SigmaTrack:
    """EWMA sigma of 1-s log changes (per sqrt second), floored at 0.2 x the
    running median of the minute samples."""

    def __init__(self, half_life: float = SIGMA_HALF_LIFE):
        self.lam = 0.5 ** (1.0 / half_life)
        self.var: Optional[float] = None
        self.last: Optional[float] = None
        self.samples: List[float] = []
        self.n = 0

    def step(self, price: Optional[float]) -> None:
        if price is None or price <= 0:
            return
        if self.last is not None:
            r2 = math.log(price / self.last) ** 2
            self.var = r2 if self.var is None else self.lam * self.var + (1 - self.lam) * r2
        self.last = price
        self.n += 1
        if self.var is not None and self.n % 60 == 0:
            bisect.insort(self.samples, math.sqrt(self.var))

    def sigma(self) -> Optional[float]:
        if self.var is None or self.n < 120:
            return None
        s = math.sqrt(self.var)
        if self.samples:
            s = max(s, 0.2 * self.samples[len(self.samples) // 2])
        return s if s > 0 else None


# ---- the variants ------------------------------------------------------------------

def best_yes_bid(book_yes) -> Optional[int]:
    return S.best_price(book_yes)


def best_yes_ask(book_no) -> Optional[int]:
    nb = S.best_price(book_no)
    return None if nb is None else 100 - nb


def desired(kind: str, margin: int, fair: float, tau: float, book_yes, book_no
            ) -> Tuple[Optional[int], Optional[int]]:
    """(YES bid cents or None, YES ask cents or None) for one variant."""
    f = 100.0 * fair
    bid = ask = None
    if kind == "fair":
        bid, ask = int(math.floor(f)) - margin, int(math.ceil(f)) + margin
    elif kind == "join":
        bb, ba = best_yes_bid(book_yes), best_yes_ask(book_no)
        if bb is not None and bb <= f - margin:
            bid = bb
        if ba is not None and ba >= f + margin:
            ask = ba
    elif kind == "fav":
        if tau <= 300:
            if fair >= 0.995:
                bid = 97
            elif fair <= 0.005:
                ask = 3
    if bid is not None and not 2 <= bid <= 98:
        bid = None
    if ask is not None and not 2 <= ask <= 98:
        ask = None
    if bid is not None and S.would_cross(book_yes, book_no, "yes", bid):
        bid = None
    if ask is not None and S.would_cross(book_yes, book_no, "no", 100 - ask):
        ask = None
    return bid, ask


class VState:
    __slots__ = ("bid", "ask", "pos", "fills", "reward", "reward_secs")

    def __init__(self):
        self.bid: Optional[int] = None
        self.ask: Optional[int] = None
        self.pos = 0.0
        self.fills: List[Tuple[float, str, int, float]] = []   # (ts, 'bid'|'ask', cents, n)
        self.reward = 0.0
        self.reward_secs = 0


def fill_pnl(fills, result: Optional[str]) -> Optional[float]:
    if result not in ("yes", "no"):
        return None
    y = 100.0 if result == "yes" else 0.0
    return sum(n * ((y - px) if side == "bid" else (px - y)) / 100.0 for _t, side, px, n in fills)


# ---- the day -----------------------------------------------------------------------

def simulate_day(day: date, echo: bool = False) -> dict:
    a, b = day_window(day)
    t_start = time.time()
    markets = load_meta(a, b)
    px = load_px(a - 3700, b + 60)
    series = {sym: Series(*v) for sym, v in px.items()}
    sig = {sym: SigmaTrack() for sym in FX_TICK}
    # per-second state for the sigma tracks: warm on the hour before the day
    by_open = defaultdict(list)
    for t, m in markets.items():
        by_open[int(m["open"])].append(t)
    active: Dict[str, dict] = {}
    vstate: Dict[str, Dict[str, VState]] = {}
    books: Dict[str, list] = {}           # ticker -> [yes, no, trusted]
    race_cache: Dict[str, Tuple[int, np.ndarray]] = {}
    race_ret5 = {c: [] for c in RACE_COINS}       # 5-s log returns (for covariance)
    race_cov = None
    brier = defaultdict(lambda: {"n": 0, "fair": 0.0, "mid": 0.0})
    fidelity = {"n": 0, "agree": 0}
    cover = {"ws_rows": 0, "snapshots": 0, "deltas": 0, "trades": 0, "untrusts": 0,
             "eval_secs": 0, "no_fair": 0, "no_book": 0}
    rng_seed = int(a)
    s0: Dict[str, object] = {}
    wanted = set(markets)

    def fair_of(m: dict, s: int) -> Optional[float]:
        t = m["ticker"]
        tau = m["close"] - s
        if m["series"] in FX_SERIES:
            sym = FX_SERIES[m["series"]]
            ser = series.get(sym)
            if ser is None:
                return None
            p, age = ser.at(s)
            sg = sig[sym].sigma()
            if p is None or age > FX_STALE or sg is None or s0.get(t) is None:
                return None
            return fx_p_up(p, s0[t], sg, tau, FX_TICK[sym])
        if m["series"] == RACE_SERIES:
            ev = m["event"]
            coin = t.rsplit("-", 1)[1]
            if coin not in RACE_COINS or race_cov is None:
                return None
            cached = race_cache.get(ev)
            if cached is None or s - cached[0] >= RACE_EVERY:
                base = s0.get(ev)
                if base is None:
                    return None
                now = []
                for c in RACE_COINS:
                    ser = series.get(c)
                    p, age = (None, float("inf")) if ser is None else ser.at(s)
                    if p is None or age > RACE_STALE or base.get(c) in (None, 0):
                        return None
                    now.append(math.log(p / base[c]))
                rng = np.random.default_rng(rng_seed + int(m["open"]) // 900)
                probs = race_probs(np.array(now), race_cov, tau, rng)
                race_cache[ev] = (s, probs)
                cached = race_cache[ev]
            return float(cached[1][RACE_COINS.index(coin)])
        return None

    def evaluate(s: int) -> None:
        nonlocal race_cov
        # price tracks advance once per second
        for sym, tr in sig.items():
            ser = series.get(sym)
            if ser is not None:
                tr.step(ser.at(s)[0])
        if s % 5 == 0:
            vals = []
            for c in RACE_COINS:
                ser = series.get(c)
                vals.append(None if ser is None else ser.at(s)[0])
            prev = getattr(evaluate, "_race_prev", None)
            if prev is not None and all(v is not None for v in vals) and all(v is not None for v in prev):
                for i, c in enumerate(RACE_COINS):
                    race_ret5[c].append(math.log(vals[i] / prev[i]))
                    if len(race_ret5[c]) > 720:
                        race_ret5[c].pop(0)
                if len(race_ret5[RACE_COINS[0]]) >= 60:
                    X = np.array([race_ret5[c] for c in RACE_COINS])
                    w = 0.5 ** (np.arange(X.shape[1])[::-1] / (SIGMA_HALF_LIFE / 5.0))
                    w = w / w.sum()
                    Xc = X - (X * w).sum(axis=1, keepdims=True)
                    race_cov = (Xc * w) @ Xc.T
            evaluate._race_prev = vals
        if s < a or s >= b:
            return
        # markets opening now: their reference prices
        for t in by_open.get(s, ()):
            m = markets[t]
            active[t] = m
            vstate[t] = {v: VState() for v in VARIANTS}
            if m["series"] in FX_SERIES:
                ser = series.get(FX_SERIES[m["series"]])
                p, age = (None, 0) if ser is None else ser.at(s)
                s0[t] = p if (p is not None and age <= FX_STALE) else None
            elif m["series"] == RACE_SERIES and m["event"] not in s0:
                base = {}
                for c in RACE_COINS:
                    ser = series.get(c)
                    p, age = (None, 0) if ser is None else ser.at(s)
                    base[c] = p if (p is not None and age <= RACE_STALE) else None
                s0[m["event"]] = base if all(base.values()) else None
        hour = datetime.fromtimestamp(s, ET).hour
        night = hour in NIGHT_HOURS
        cover["eval_secs"] += 1
        for t in list(active):
            m = active[t]
            tau = m["close"] - s
            if tau <= 0:
                # the close: FX fidelity (the proxy's own call vs Kalshi's)
                if m["series"] in FX_SERIES and m["result"] and s0.get(t):
                    ser = series.get(FX_SERIES[m["series"]])
                    p, age = (None, 0) if ser is None else ser.at(m["close"])
                    if p is not None and age <= FX_STALE:
                        fidelity["n"] += 1
                        fidelity["agree"] += (p >= s0[t]) == (m["result"] == "yes")
                del active[t]
                continue
            bk = books.get(t)
            vs = vstate[t]
            prog = m["program"]
            if bk is None or not bk[2]:
                cover["no_book"] += 1
                for st in vs.values():
                    st.bid = st.ask = None
                continue
            fair = fair_of(m, s)
            if fair is None:
                cover["no_fair"] += 1
            # Brier at fixed times to close
            if int(tau) in BRIER_TAUS and fair is not None and m["result"]:
                bb, ba = best_yes_bid(bk[0]), best_yes_ask(bk[1])
                if bb is not None and ba is not None:
                    y = 1.0 if m["result"] == "yes" else 0.0
                    key = f"{m['series']}|{int(tau)}"
                    brier[key]["n"] += 1
                    brier[key]["fair"] += (fair - y) ** 2
                    brier[key]["mid"] += ((bb + ba) / 200.0 - y) ** 2
            pool = 0.0
            target, df = 300.0, 0.5
            if prog:
                try:
                    p_a, p_b = C.parse_iso(prog["start_date"]), C.parse_iso(prog["end_date"])
                    if p_a <= s < p_b:
                        pool = float(prog["period_reward"]) / 1e4 / max(p_b - p_a, 1.0)
                    target = float(prog.get("target_size_fp") or 300)
                    df = float(prog.get("discount_factor_bps") or 5000) / 10000.0
                except (KeyError, TypeError, ValueError):
                    pool = 0.0
            for vid, (kind, margin, night_only) in VARIANTS.items():
                st = vs[vid]
                if fair is None or tau < MIN_TAU or (night_only and not night):
                    st.bid = st.ask = None
                    continue
                bid, ask = desired(kind, margin, fair, tau, bk[0], bk[1])
                if bid is not None and st.pos + SIZE > POS_CAP:
                    bid = None
                if ask is not None and st.pos - SIZE < -POS_CAP:
                    ask = None
                st.bid, st.ask = bid, ask
                if pool > 0 and (bid is not None or ask is not None):
                    own_yes = {bid: SIZE} if bid is not None else {}
                    own_no = {100 - ask: SIZE} if ask is not None else {}
                    frac = S.score(bk[0], bk[1], own_yes, own_no, target, df)[0]
                    st.reward += frac * pool
                    st.reward_secs += 1

    def on_trade(tr) -> None:
        vs = vstate.get(tr.ticker)
        if vs is None or tr.ticker not in active:
            return
        cover["trades"] += 1
        for st in vs.values():
            if tr.taker_side == "no" and st.bid is not None and tr.yes_cents <= st.bid:
                n = min(SIZE, POS_CAP - st.pos)
                if n > 0:
                    st.fills.append((tr.ts, "bid", st.bid, n))
                    st.pos += n
                st.bid = None                     # filled: re-quoted next second
            elif tr.taker_side == "yes" and st.ask is not None and tr.yes_cents >= st.ask:
                n = min(SIZE, POS_CAP + st.pos)
                if n > 0:
                    st.fills.append((tr.ts, "ask", st.ask, n))
                    st.pos -= n
                st.ask = None

    next_eval = int(a - 3600)
    for rec in C.iter_raw("ws", a - 3600, b + 60):
        rx = float(rec.get("rx") or 0)
        while next_eval + 0.5 <= rx:
            evaluate(next_eval)
            next_eval += 1
        cover["ws_rows"] += 1
        if "marker" in rec:
            if rec["marker"] in ("connect", "disconnect", "gap", "resubscribe"):
                for bk in books.values():
                    bk[2] = False
                cover["untrusts"] += 1
            continue
        msg = rec.get("m") or {}
        typ = msg.get("type")
        body = msg.get("msg") or {}
        tk = body.get("market_ticker")
        if typ == "orderbook_snapshot" and tk in wanted:
            y, n = S.snapshot_levels(body)
            books[tk] = [y, n, True]
            cover["snapshots"] += 1
        elif typ == "orderbook_delta" and tk in wanted:
            bk = books.get(tk)
            d = S.delta_fields(body)
            if bk is not None and bk[2] and d is not None:
                is_yes, c, delta = d
                side = bk[0] if is_yes else bk[1]
                q = round(side.get(c, 0.0) + delta, S.SIZE_DP)
                if q > S.EPS:
                    side[c] = q
                else:
                    side.pop(c, None)
            cover["deltas"] += 1
        elif typ == "trade" and tk in wanted:
            parsed = S.parse_ws_trade(body, rx)
            if parsed is not None:
                on_trade(parsed[0])
    while next_eval < b + 1:
        evaluate(next_eval)
        next_eval += 1

    # ---- results ----------------------------------------------------------------
    out = {"day": day.isoformat(), "generated": C.iso(time.time()), "runtime_s": 0.0,
           "markets": len(markets), "settled": sum(1 for m in markets.values() if m["result"]),
           "with_program": sum(1 for m in markets.values() if m["program"]),
           "coverage": cover, "fidelity": fidelity,
           "brier": {k: {"n": v["n"], "fair": v["fair"] / v["n"], "mid": v["mid"] / v["n"]}
                     for k, v in brier.items() if v["n"]},
           "variants": {}}
    for vid in VARIANTS:
        agg = {"reward": 0.0, "pnl": 0.0, "fills": 0, "contracts": 0.0, "unsettled": 0,
               "by_series": defaultdict(lambda: {"reward": 0.0, "pnl": 0.0, "fills": 0}),
               "by_hour": defaultdict(float), "night": 0.0, "day": 0.0}
        for t, m in markets.items():
            st = (vstate.get(t) or {}).get(vid)
            if st is None:
                continue
            pnl = fill_pnl(st.fills, m["result"])
            if pnl is None:
                agg["unsettled"] += 1
                pnl = 0.0
            net = st.reward + pnl
            agg["reward"] += st.reward
            agg["pnl"] += pnl
            agg["fills"] += len(st.fills)
            agg["contracts"] += sum(f[3] for f in st.fills)
            bs = agg["by_series"][m["series"]]
            bs["reward"] += st.reward
            bs["pnl"] += pnl
            bs["fills"] += len(st.fills)
            h = datetime.fromtimestamp(m["open"], ET).hour
            agg["by_hour"][h] += net
            agg["night" if h in NIGHT_HOURS else "day"] += net
        agg["net"] = agg["reward"] + agg["pnl"]
        agg["by_series"] = {k: dict(v) for k, v in agg["by_series"].items()}
        agg["by_hour"] = {str(k): v for k, v in sorted(agg["by_hour"].items())}
        out["variants"][vid] = agg
    out["runtime_s"] = round(time.time() - t_start, 1)
    path = os.path.join(DATA, "sim", day.isoformat(), "q15_day.json")
    C.write_json_atomic(path, out)
    if echo:
        print(f"{day}: {out['markets']} markets ({out['settled']} settled, {out['with_program']} paid), "
              f"{cover['ws_rows']:,} ws rows, {out['runtime_s']}s")
        for vid, v in out["variants"].items():
            print(f"  {vid:12s} net ${v['net']:9.2f} = reward ${v['reward']:8.2f} + fills ${v['pnl']:9.2f} "
                  f"({v['fills']} fills, {v['contracts']:.0f} ct)  night ${v['night']:8.2f}")
        if fidelity["n"]:
            print(f"  FX proxy call agrees with Kalshi on {fidelity['agree']}/{fidelity['n']} closes")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", required=True, help="ET day YYYY-MM-DD")
    ap.add_argument("--echo", action="store_true")
    args = ap.parse_args(argv)
    simulate_day(datetime.strptime(args.day, "%Y-%m-%d").date(), echo=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
