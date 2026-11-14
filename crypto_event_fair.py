#!/usr/bin/env python3
"""
crypto_event_fair.py -- the fair value of Kalshi's crypto "price after
economic events" strikes (KXBTCEVENT, KXETHEVENT, KXSOLEVENT, KXHYPEEVENT:
"Will Bitcoin be above $78000 on Dec 9 at 3:30pm ET?"), for the IMM's crypto
event gate (Jack 2026-11-13: "you can quote both sides on these, built around
fair value").

THE CONTRACT. Each strike settles on the simple average of the CF Benchmarks
real-time index (BRTI, ETHUSD_RTI, ...) over the 60 seconds immediately
before the event's observation time (= the market's close_time). It is a
TERMINAL contract -- no early close, nothing about touching the strike before
-- so it is priced like crypto_annual_mm's KX<ASSET>Y rungs, never like the
touch markets (KXBTCHITBY, KXSOLABOVEBY: early_close_condition set; those
stay NO side only). terminal_shape() refuses anything else.

THE MODEL is the crypto fleet's: driftless GBM (Black-Scholes N(d2), r=0,
crypto_updown_mm.terminal_prob's convention), spot from Kraken with the
Coinbase fallback and jump cross-check (crypto_touch_mm.fetch_live_price, the
fleet's shared 10 s cache), and the monthly fleet's daily sigma: the 0.6 EWMA
/ 0.4 90-day blend over Kraken daily candles, variance-shrunk 25% toward its
own long-run median (crypto_touch_mm.shrunk_daily_vol, CMM_VOL_SHRINK_W), the
in-progress UTC day dropped.

THE BAND. Each market carries p (the model at spot and sigma) and [p_lo,
p_hi], the extremes over spot x (1 +- spot_band) and sigma x vol_lo / vol_hi:
the 1% covers the spot moving while a quote rests (the IMM requotes a market
every ~1.5-2.5 min), the vol factors the realized-vol model's own error and
the event day's extra variance. The gate bids at most p_lo - tol and asks at
least p_hi + tol.

JUMP HOLD. An asset whose spot moved more than jump_pct inside jump_window_s
(from any sample in the window to now) holds every one of its markets for
jump_hold_s: the gate stands them aside, as a release hold does.

Dry read (no orders):  python crypto_event_fair.py KXBTCEVENT,KXETHEVENT
"""

import json
import math
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

import crypto_touch_mm as ctm

ASSET_PAIRS: Dict[str, Tuple[str, str]] = dict(ctm._PAIRS)   # asset -> (kraken, coinbase)
SERIES_RE = re.compile(r"^KX([A-Z0-9]+?)EVENT$")

META_REFRESH_SECS = 600          # Kalshi market lists
VOL_REFRESH_SECS = 3600          # daily candles -> sigma
VOL_SHRINK_W = ctm.VOL_SHRINK_W  # the monthly fleet's 0.25
SPOT_BAND = 0.01
VOL_LO = 0.8
VOL_HI = 1.25
JUMP_PCT = 0.01
JUMP_WINDOW_SECS = 300
JUMP_HOLD_SECS = 600


def series_asset(series: str) -> Optional[str]:
    """KXBTCEVENT -> BTC: the asset of a KX<ASSET>EVENT series the fleet can
    price (its Kraken / Coinbase pair is known), else None."""
    m = SERIES_RE.match(series or "")
    if not m or m.group(1) not in ASSET_PAIRS:
        return None
    return m.group(1)


def _f(market: dict, key: str) -> Optional[float]:
    try:
        v = float(market[key])
    except (KeyError, TypeError, ValueError):
        return None
    return v if v > 0 else None


def terminal_shape(market: dict) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """(kind, lo, hi) for a terminal strike -- kind 'greater' (lo), 'less'
    (hi) or 'between' (lo, hi) -- else (None, None, None): an early close
    condition (a touch market), an unknown strike type, or rules that do not
    name a simple average of a CF Benchmarks index immediately before the
    observation."""
    if market.get("early_close_condition"):
        return None, None, None
    rules = (market.get("rules_primary") or "") + " " + (market.get("rules_secondary") or "")
    # "CF Benchmarks' Bitcoin Real-Time Index (BRTI)", "... CME CF Ether-Dollar
    # Real Time Index (ETHUSD_RTI)", "... CF Hyperliquid-Dollar Spot Rate
    # (HYPEUSD_RTI)": the wording varies by asset, the shape does not
    if not re.search(r"simple average of CF Benchmarks", rules, re.I) \
            or not re.search(r"immediately before", rules, re.I):
        return None, None, None
    st = market.get("strike_type")
    if st == "greater":
        k = _f(market, "floor_strike")
        return ("greater", k, None) if k else (None, None, None)
    if st == "less":
        k = _f(market, "cap_strike")
        return ("less", None, k) if k else (None, None, None)
    if st == "between":
        lo, hi = _f(market, "floor_strike"), _f(market, "cap_strike")
        return ("between", lo, hi) if lo and hi and hi > lo else (None, None, None)
    return None, None, None


def terminal_prob(spot: float, strike: float, sigma_daily: float, t_days: float) -> float:
    """P(S_T >= strike), driftless in price (crypto_updown_mm.terminal_prob)."""
    if spot <= 0 or strike <= 0:
        raise ValueError(f"bad spot/strike: {spot}/{strike}")
    if t_days <= 0 or sigma_daily <= 0:
        return 1.0 if spot >= strike else 0.0
    s = sigma_daily * math.sqrt(t_days)
    d2 = (math.log(spot / strike) - 0.5 * s * s) / s
    return min(max(0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0))), 0.0), 1.0)


def shape_prob(kind: str, lo: Optional[float], hi: Optional[float], spot: float,
               sigma: float, t_days: float) -> float:
    if kind == "greater":
        return terminal_prob(spot, lo, sigma, t_days)
    if kind == "less":
        return 1.0 - terminal_prob(spot, hi, sigma, t_days)
    return max(0.0, terminal_prob(spot, lo, sigma, t_days)
               - terminal_prob(spot, hi, sigma, t_days))


def price_band(kind: str, lo: Optional[float], hi: Optional[float], spot: float,
               sigma: float, t_days: float, spot_band: float = SPOT_BAND,
               vol_lo: float = VOL_LO, vol_hi: float = VOL_HI
               ) -> Tuple[float, float, float]:
    """(p, p_lo, p_hi): the model, and its extremes over spot x (1 +-
    spot_band) and sigma x {vol_lo, 1, vol_hi}. A 'between' bucket peaks
    inside the grid, so the grid includes the middle points."""
    p = shape_prob(kind, lo, hi, spot, sigma, t_days)
    ps = [shape_prob(kind, lo, hi, spot * (1.0 + ds), sigma * vm, t_days)
          for ds in (-spot_band, 0.0, spot_band) for vm in (vol_lo, 1.0, vol_hi)]
    return p, min(ps), max(ps)


def _close_ts(market: dict) -> Optional[float]:
    s = market.get("close_time") or market.get("expected_expiration_time")
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class CryptoEventWatch:
    """The refresher's state: market lists every META_REFRESH_SECS, each
    asset's sigma every VOL_REFRESH_SECS, spots every refresh(); `snap` is
    what the gate reads.

    snap = {"checked_ts", "assets": {asset: {spot, spot_ts, sigma, ...,
    hold_until, err}}, "markets": {ticker: {asset, kind, lo, hi, close_ts,
    t_days, p, p_lo, p_hi, spot, sigma, spot_ts, hold_until} or {err}},
    "errors": [...], "model": {...}}"""

    def __init__(self, get_json: Optional[Callable[[str, dict], dict]],
                 series: Callable[[], Iterable[str]],
                 spot_band: float = SPOT_BAND, vol_lo: float = VOL_LO,
                 vol_hi: float = VOL_HI, jump_pct: float = JUMP_PCT,
                 jump_window_s: float = JUMP_WINDOW_SECS,
                 jump_hold_s: float = JUMP_HOLD_SECS,
                 fetch_price: Optional[Callable[[str, Optional[float]], float]] = None,
                 fetch_closes: Optional[Callable[[str, float], List[float]]] = None):
        self.get_json = get_json
        self.series_fn = series
        self.spot_band, self.vol_lo, self.vol_hi = spot_band, vol_lo, vol_hi
        self.jump_pct, self.jump_window_s, self.jump_hold_s = jump_pct, jump_window_s, jump_hold_s
        self._fetch_price = fetch_price or self._kraken_price
        self._fetch_closes = fetch_closes or self._kraken_closes
        self.meta_at = 0.0
        self.markets: List[dict] = []
        self.vol: Dict[str, dict] = {}           # asset -> {sigma, raw, med, ts}
        self.spot: Dict[str, dict] = {}          # asset -> {spot, ts}
        self.hist: Dict[str, Deque[Tuple[float, float]]] = {}
        self.hold_until: Dict[str, float] = {}
        self.snap: Optional[dict] = None

    # --- feeds (the fleet's) ---------------------------------------------
    @staticmethod
    def _cfg(asset: str):
        kr, cb = ASSET_PAIRS[asset]
        return ctm.MarketConfig(f"{asset}-EVENT", asset, f"KX{asset}EVENT", "max", kr, cb)

    def _kraken_price(self, asset: str, prev: Optional[float]) -> float:
        return ctm.fetch_live_price(self._cfg(asset), prev)

    def _kraken_closes(self, asset: str, now_ts: float) -> List[float]:
        candles = ctm.fetch_ohlc(self._cfg(asset), 1440)
        closes = [c for _ts, c, _h, _l in candles]
        day0 = int(now_ts // 86400 * 86400)
        if candles and candles[-1][0] >= day0:
            closes = closes[:-1]                 # the in-progress UTC day
        return closes

    # --- refresh ------------------------------------------------------------
    def _list_markets(self) -> List[dict]:
        out: List[dict] = []
        for s in sorted({x for x in self.series_fn() if series_asset(x)}):
            cursor = None
            for _ in range(20):
                params = {"series_ticker": s, "status": "open", "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                body = self.get_json("/markets", params) if self.get_json else {}
                out += [m for m in (body or {}).get("markets") or [] if isinstance(m, dict)]
                cursor = (body or {}).get("cursor")
                if not cursor:
                    break
        return out

    def _refresh_vol(self, asset: str, now_ts: float) -> None:
        v = self.vol.get(asset)
        if v and now_ts - v["ts"] < VOL_REFRESH_SECS:
            return
        closes = self._fetch_closes(asset, now_ts)
        sigma, raw, med = ctm.shrunk_daily_vol(closes, VOL_SHRINK_W)
        if not (0.001 <= sigma <= 0.5):
            raise ValueError(f"{asset} daily vol {sigma:.4f} outside sanity range")
        self.vol[asset] = {"sigma": sigma, "raw": raw, "med": med, "ts": now_ts,
                           "n": len(closes)}

    def _refresh_spot(self, asset: str, now_ts: float) -> None:
        prev = (self.spot.get(asset) or {}).get("spot")
        px = float(self._fetch_price(asset, prev))
        if px <= 0:
            raise ValueError(f"{asset} bad spot {px}")
        self.spot[asset] = {"spot": px, "ts": now_ts}
        h = self.hist.setdefault(asset, deque())
        h.append((now_ts, px))
        while h and now_ts - h[0][0] > self.jump_window_s:
            h.popleft()
        lo = min(x for _t, x in h)
        hi = max(x for _t, x in h)
        if lo > 0 and (px / lo - 1.0 > self.jump_pct or 1.0 - px / hi > self.jump_pct):
            self.hold_until[asset] = now_ts + self.jump_hold_s

    def refresh(self, now_ts: Optional[float] = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        errors: List[str] = []
        if now_ts - self.meta_at >= META_REFRESH_SECS or not self.markets:
            try:
                self.markets = self._list_markets()
                self.meta_at = now_ts
            except Exception as e:                       # keep the last list
                errors.append(f"markets: {type(e).__name__}: {str(e)[:100]}")
        assets = sorted({a for a in (series_asset(str(m.get("event_ticker") or m.get("ticker") or "").split("-")[0])
                                     for m in self.markets) if a})
        a_err: Dict[str, str] = {}
        for a in assets:
            try:
                self._refresh_vol(a, now_ts)
            except Exception as e:
                if a not in self.vol:
                    a_err[a] = f"vol: {type(e).__name__}: {str(e)[:100]}"
                errors.append(f"{a} vol: {type(e).__name__}: {str(e)[:80]}")
            try:
                self._refresh_spot(a, now_ts)
            except Exception as e:                       # the spot ages; the gate's TTL fails closed
                errors.append(f"{a} spot: {type(e).__name__}: {str(e)[:80]}")
                if a not in self.spot:
                    a_err.setdefault(a, f"spot: {type(e).__name__}: {str(e)[:100]}")
        mk: Dict[str, dict] = {}
        for m in self.markets:
            t = str(m.get("ticker") or "")
            a = series_asset(t.split("-")[0])
            if not t or not a:
                continue
            if a in a_err:
                mk[t] = {"asset": a, "err": a_err[a]}
                continue
            kind, lo, hi = terminal_shape(m)
            if kind is None:
                mk[t] = {"asset": a, "err": "not a terminal index-average strike"}
                continue
            ct = _close_ts(m)
            if ct is None:
                mk[t] = {"asset": a, "err": "no close time"}
                continue
            sp, vo = self.spot[a], self.vol[a]
            t_days = max(0.0, (ct - sp["ts"]) / 86400.0)
            try:
                p, p_lo, p_hi = price_band(kind, lo, hi, sp["spot"], vo["sigma"], t_days,
                                           self.spot_band, self.vol_lo, self.vol_hi)
            except Exception as e:
                mk[t] = {"asset": a, "err": f"price: {type(e).__name__}: {str(e)[:80]}"}
                continue
            mk[t] = {"asset": a, "kind": kind, "lo": lo, "hi": hi, "close_ts": ct,
                     "t_days": round(t_days, 4), "p": round(p, 5),
                     "p_lo": round(p_lo, 5), "p_hi": round(p_hi, 5),
                     "spot": sp["spot"], "spot_ts": sp["ts"], "sigma": round(vo["sigma"], 6),
                     "hold_until": self.hold_until.get(a)}
        self.snap = {
            "checked_ts": now_ts,
            "assets": {a: {"spot": (self.spot.get(a) or {}).get("spot"),
                           "spot_ts": (self.spot.get(a) or {}).get("ts"),
                           "sigma": (self.vol.get(a) or {}).get("sigma"),
                           "sigma_raw": (self.vol.get(a) or {}).get("raw"),
                           "sigma_med": (self.vol.get(a) or {}).get("med"),
                           "vol_ts": (self.vol.get(a) or {}).get("ts"),
                           "hold_until": self.hold_until.get(a),
                           "err": a_err.get(a)} for a in assets},
            "markets": mk,
            "errors": errors,
            "model": {"spot_band": self.spot_band, "vol_lo": self.vol_lo,
                      "vol_hi": self.vol_hi, "vol_shrink_w": VOL_SHRINK_W,
                      "jump_pct": self.jump_pct, "jump_window_s": self.jump_window_s,
                      "jump_hold_s": self.jump_hold_s},
        }
        return self.snap


def write_status(path: str, snap: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


if __name__ == "__main__":                               # a dry read, no orders
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from kalshi_reads import kalshi_get
    ser = (sys.argv[1] if len(sys.argv) > 1 else
           "KXBTCEVENT,KXETHEVENT,KXSOLEVENT,KXHYPEEVENT").split(",")
    w = CryptoEventWatch(lambda p, q: kalshi_get(p, q), lambda: ser)
    snap = w.refresh()
    for a, v in snap["assets"].items():
        print(a, v)
    for t, e in sorted(snap["markets"].items()):
        if "p" in e:
            print(f"{t:42s} p {100 * e['p']:5.1f}  band {100 * e['p_lo']:5.1f}-{100 * e['p_hi']:5.1f}  t {e['t_days']:.2f}d")
        else:
            print(t, e)
    print(snap["errors"])
