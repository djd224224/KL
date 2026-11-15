#!/usr/bin/env python3
"""
fx_event_fair.py -- the LIVE fair value of Kalshi's FX + metals "after economic events"
strikes (KXGOLDEVENT, KXSILVEREVENT, KXCOPPEREVENT, KXUSDJPYEVENT, KXEURUSDEVENT,
KXAUDUSDEVENT, KXUSDMXNEVENT, KXUSDCADEVENT, KXGBPUSDEVENT: "Will gold be above $3,880 one
minute after the Nov 17 release?"), for the IMM's FX event gate. Jack 2026-11-14: "after-economic
events can quote anytime not just when markets are shut, but need a fair value", then "Yes,
build it" to a 15-second live fair in the bot like the crypto strikes' (crypto_event_fair.py).

THE MODEL is wave_fair.py's (the w1113-fxmetals fair since 11/13), refreshed every 15 s
instead of every 15-min pass: "above K at T" = N(ln(S/K) / (vol_d x sqrt(trading days to T +
the Sunday-open gap))), S = the weekend's Friday 5 PM ET close x (the 24/7 source's price now /
its price at that close). The close and the source's own close come from
run-logs/pvd-scan/wave_fair_closes.json, which wave_fair.py (the wave's pass-7 fair_cmd) fills
once a weekend (Kalshi's weekly metals settlements, Yahoo's last FX bar, each checked within
0.5% of its source). No close kept for the current close = no fair for that series: the gate
stands it aside (fails closed). The 24/7 sources (public, no key): Hyperliquid trade.xyz
perps (gold, silver, copper, JPY, EUR, GBP: one call), Kraken spot FX (AUD/USD, USD/CAD: one
call), Bitso (USD/MXN). Pyth, the settlement source, needs a key for live prices (401).

MODES (as wave_fair.py):
  weekend  FX and metals shut (Fri 5 PM - Sun 5 PM ET): time from the close; floor 2c (a bid
           at <= 2c / an ask at >= 98c always passes, as pvd_wave_tune's --fair-file).
  weekday  they trade: time from now, the Sunday gap only across a weekend; no fair within
           CUTOFF_H (12 h) of a strike's measurement (the release it settles one minute after
           outweighs the model's vol-error allowance inside that); floor 0 (no fair = nothing
           rests); and a requote term in the margin (below).
THE MARGIN (c) per strike: the 2c base, every series (Jack 2026-11-14: "tighten to 2c"; copper,
6c until that evening: "make copper and all metals/fx at the 2c margin")
+ 100 x VOL_ERR x phi(z)|z| (how far the fair moves under a 25% vol error, as the tune's), and
on weekdays + 100 x phi(z) x REQUOTE_SD x (the source's move over REQUOTE_MIN / the sigma to
the measurement): a quote rests up to ~3 min between requotes while the market moves. A wave
book (every one of these strikes is a wave book) bids at most fair - margin and asks at least
fair + margin, in whole cents, floors apart: bid_max / ask_min, live every cycle.
JUMP HOLD: a source whose price moved more than JUMP_SD of its series' daily sigma inside
JUMP_WINDOW_SECS (5 min) holds the series JUMP_HOLD_SECS (5 min), as crypto's 1%.
STALE: the gate (incentive_mm.fx_event_gate) stands a series aside once its source's last
good read is FX_EVENT_TTL_SECS (180 s) old.

Dry read (no orders):  python fx_event_fair.py [KXGOLDEVENT,KXUSDJPYEVENT]
"""

import json
import math
import os
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")
CLOSES_FILE = os.environ.get("IMM_FX_EVENT_CLOSES_FILE",
                             os.path.join(HERE, "run-logs", "pvd-scan", "wave_fair_closes.json"))

# wave_fair.py's (keep in step: test_fx_event_fair checks them against it)
SERIES = ("KXUSDJPYEVENT", "KXEURUSDEVENT", "KXAUDUSDEVENT", "KXUSDMXNEVENT", "KXUSDCADEVENT",
          "KXGBPUSDEVENT", "KXGOLDEVENT", "KXSILVEREVENT", "KXCOPPEREVENT")
VOL_D = {"KXUSDJPYEVENT": .0057, "KXEURUSDEVENT": .0044, "KXAUDUSDEVENT": .0063,
         "KXUSDMXNEVENT": .0076, "KXUSDCADEVENT": .0038, "KXGBPUSDEVENT": .0050,
         "KXGOLDEVENT": .0113, "KXSILVEREVENT": .019, "KXCOPPEREVENT": .0139}
PROXY = {"KXGOLDEVENT": ("hl", "xyz:GOLD"), "KXSILVEREVENT": ("hl", "xyz:SILVER"),
         "KXCOPPEREVENT": ("hl", "xyz:COPPER"), "KXUSDJPYEVENT": ("hl", "xyz:JPY"),
         "KXEURUSDEVENT": ("hl", "xyz:EUR"), "KXGBPUSDEVENT": ("hl", "xyz:GBP"),
         "KXAUDUSDEVENT": ("kraken", "AUDUSD"), "KXUSDCADEVENT": ("kraken", "USDCAD"),
         "KXUSDMXNEVENT": ("bitso", "usd_mxn")}
MARGIN = {s: 2.0 for s in SERIES}        # copper too since 11/14 evening (was 6c)
WEEKEND_GAP_DAYS = 0.5
CUTOFF_H = 12.0
WEEKEND_FLOOR, WEEKDAY_FLOOR = 2, 0
# the live fair's own
KRAKEN_KEYS = {"AUDUSD": ("AUDUSD", "ZAUDZUSD"), "USDCAD": ("USDCAD", "ZUSDZCAD")}
META_REFRESH_SECS = 600          # Kalshi market lists
VOL_ERR = 0.25                   # pvd_wave_tune --fair-vol-err
REQUOTE_MIN = 3.0                # how long a quote may rest before the bot requotes it
REQUOTE_SD = 2.0                 # weekday margin: this many sd of the source's move over REQUOTE_MIN
JUMP_SD = 0.35                   # a move > this x the daily sigma inside the window = a jump (BTC 1% ~ 0.36)
# A WIDE BOOK'S MID IS NOT A PRICE (2026-11-14, first live hour): Bitso's weekend USD/MXN book
# flickers -- its spread 0.005% <-> 0.29% as the ask side thins, the mid 0.23% in 3 min -- and
# tripped the jump hold (0.27%) on noise. A read whose spread is wider than MAX_SPREAD is not
# taken; the series keeps its last good price, and a book wide past the gate's TTL stands it aside.
MAX_SPREAD = 0.0012
JUMP_WINDOW_SECS = 300
JUMP_HOLD_SECS = 300


def _get(url: str, body: dict = None, timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "User-Agent": "fx_event_fair"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_mids(get: Callable = None) -> Tuple[Dict[str, float], Dict[str, str]]:
    """({series: the 24/7 source's mid now}, {series: why not}): one Hyperliquid call for the
    six xyz perps, one Kraken ticker for AUD/USD + USD/CAD, one Bitso ticker for USD/MXN."""
    get = get or _get
    out: Dict[str, float] = {}
    err: Dict[str, str] = {}
    by_src: Dict[str, List[str]] = {}
    for s, (src, _sym) in PROXY.items():
        by_src.setdefault(src, []).append(s)
    try:
        meta, ctx = get("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs", "dex": "xyz"})
        mids = {u["name"]: float(c.get("midPx") or c.get("markPx")) for u, c in zip(meta["universe"], ctx)
                if (c.get("midPx") or c.get("markPx"))}
        for s in by_src.get("hl", []):
            if mids.get(PROXY[s][1], 0) > 0:
                out[s] = mids[PROXY[s][1]]
            else:
                err[s] = f"hl:{PROXY[s][1]} not in the list"
    except Exception as e:  # noqa: BLE001
        for s in by_src.get("hl", []):
            err[s] = f"hl: {type(e).__name__}: {str(e)[:80]}"
    try:
        pairs = [PROXY[s][1] for s in by_src.get("kraken", [])]
        res = get("https://api.kraken.com/0/public/Ticker?pair=" + ",".join(pairs))["result"]
        for s in by_src.get("kraken", []):
            pair = PROXY[s][1]
            row = next((res[k] for k in KRAKEN_KEYS.get(pair, (pair,)) if k in res), None)
            if row is None:
                err[s] = f"kraken:{pair} not in the reply"
                continue
            b, a = float(row["b"][0]), float(row["a"][0])
            if not (0 < b <= a):
                err[s] = f"kraken:{pair} bad book {b} / {a}"
                continue
            if (a - b) / ((a + b) / 2) > MAX_SPREAD:
                err[s] = f"kraken:{pair} book {100 * (a - b) / ((a + b) / 2):.2f}% wide"
                continue
            out[s] = (b + a) / 2
    except Exception as e:  # noqa: BLE001
        for s in by_src.get("kraken", []):
            err[s] = f"kraken: {type(e).__name__}: {str(e)[:80]}"
    for s in by_src.get("bitso", []):
        try:
            t = get(f"https://api.bitso.com/v3/ticker/?book={PROXY[s][1]}")["payload"]
            b, a = float(t["bid"]), float(t["ask"])
            if not (0 < b <= a):
                raise ValueError(f"bad book {b} / {a}")
            if (a - b) / ((a + b) / 2) > MAX_SPREAD:
                err[s] = f"bitso:{PROXY[s][1]} book {100 * (a - b) / ((a + b) / 2):.2f}% wide"
                continue
            out[s] = (b + a) / 2
        except Exception as e:  # noqa: BLE001
            err[s] = f"bitso: {type(e).__name__}: {str(e)[:80]}"
    return out, err


def fx_shut_window(now: datetime) -> Tuple[datetime, datetime, bool]:
    """(close, reopen, shut): the latest Friday 5 PM ET close at or before `now`, the Sunday
    5 PM ET reopen after it (UTC), and whether `now` is between them. DST-aware."""
    loc = now.astimezone(ET)
    fri = (loc - timedelta(days=(loc.weekday() - 4) % 7)).date()
    close = datetime(fri.year, fri.month, fri.day, 17, 0, tzinfo=ET)
    if close > loc:
        fri -= timedelta(days=7)
        close = datetime(fri.year, fri.month, fri.day, 17, 0, tzinfo=ET)
    sun = fri + timedelta(days=2)
    c = close.astimezone(timezone.utc)
    r = datetime(sun.year, sun.month, sun.day, 17, 0, tzinfo=ET).astimezone(timezone.utc)
    return c, r, c <= now < r


def _next_reopen(a: datetime) -> datetime:
    _c, r, _s = fx_shut_window(a)
    if r > a:
        return r
    x = a
    for _ in range(8):
        x += timedelta(days=1)
        c2, r2, _s2 = fx_shut_window(x)
        if c2 > a:
            return r2
    raise ValueError("no Friday close within 8 days")


def _shut(x: datetime) -> bool:
    lo = x.astimezone(ET)
    wd, h = lo.weekday(), lo.hour
    return wd == 5 or (wd == 4 and h >= 17) or (wd == 6 and h < 17)


def trading_days(t: datetime, t0: datetime, weekday_from: Optional[datetime] = None) -> float:
    """Open-market days from the close t0 (or weekday_from, a moment while FX trades) to t,
    x 7/5, + the Sunday-open gap (from weekday_from: only if a reopen lies before t)."""
    d, x = 0.0, (weekday_from or t0)
    while x < t:
        step = min(t, x.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        if not _shut(x):
            d += (step - x).total_seconds() / 86400
        x = step
    gap = WEEKEND_GAP_DAYS if weekday_from is None or _next_reopen(weekday_from) <= t else 0.0
    return max(d * 7 / 5, 0.02) + gap


def _phi(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)


def _N(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def strike_fair(series: str, spot: float, strike: float, t_days: float, weekday: bool
                ) -> Tuple[float, float, int, int]:
    """(p 0-1, margin c, bid_max, ask_min) for one "above strike" market."""
    sig = VOL_D[series] * math.sqrt(t_days)
    z = math.log(spot / strike) / sig
    p = _N(z)
    m = MARGIN[series] + 100 * VOL_ERR * _phi(z) * abs(z)
    if weekday:
        sig_rq = VOL_D[series] * math.sqrt(REQUOTE_MIN / 1440 * 7 / 5)
        m += 100 * _phi(z) * REQUOTE_SD * sig_rq / sig
    floor = WEEKDAY_FLOOR if weekday else WEEKEND_FLOOR
    bid_max = max(floor, int(math.floor(100 * p - m + 1e-9)))
    ask_min = min(100 - floor, int(math.ceil(100 * p + m - 1e-9)))
    return p, m, bid_max, ask_min


def _close_dt(market: dict) -> Optional[datetime]:
    s = market.get("close_time") or market.get("expected_expiration_time")
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


class FxEventWatch:
    """The refresher's state: market lists every META_REFRESH_SECS, the kept closes when the
    file changes, the sources' mids every refresh(); `snap` is what the gate reads.

    snap = {"checked_ts", "mode", "close_at", "series": {series: {mid, mid_ts, ref, close,
    spot, hold_until, err}}, "markets": {ticker: {series, strike, close_ts, t_days, p, margin,
    bid_max, ask_min, spot_ts, hold_until} or {series, err[, cut]}}, "errors", "model"}"""

    def __init__(self, get_json: Optional[Callable[[str, dict], dict]],
                 series: Callable[[], Iterable[str]] = lambda: SERIES,
                 closes_path: str = CLOSES_FILE,
                 fetch: Optional[Callable[[], Tuple[Dict[str, float], Dict[str, str]]]] = None):
        self.get_json = get_json
        self.series_fn = series
        self.closes_path = closes_path
        self._fetch = fetch or fetch_mids
        self.meta_at = 0.0
        self.markets: List[dict] = []
        self.kept: dict = {}
        self.kept_mtime: Optional[float] = None
        self.mid: Dict[str, dict] = {}            # series -> {mid, ts}
        self.hist: Dict[str, Deque[Tuple[float, float]]] = {}
        self.hold_until: Dict[str, float] = {}
        self.snap: Optional[dict] = None

    def _series(self) -> List[str]:
        return sorted({s for s in self.series_fn() if s in PROXY})

    def _list_markets(self) -> List[dict]:
        out: List[dict] = []
        for s in self._series():
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

    def _load_kept(self) -> None:
        try:
            mt = os.path.getmtime(self.closes_path)
        except OSError:
            self.kept, self.kept_mtime = {}, None
            return
        if mt != self.kept_mtime:
            try:
                with open(self.closes_path, encoding="utf-8") as fh:
                    k = json.load(fh)
                self.kept, self.kept_mtime = (k if isinstance(k, dict) else {}), mt
            except (OSError, ValueError):
                pass                              # keep the last good read

    def _jump(self, s: str, now_ts: float, px: float) -> None:
        h = self.hist.setdefault(s, deque())
        h.append((now_ts, px))
        while h and now_ts - h[0][0] > JUMP_WINDOW_SECS:
            h.popleft()
        lo, hi = min(x for _t, x in h), max(x for _t, x in h)
        thr = JUMP_SD * VOL_D[s]
        if lo > 0 and (px / lo - 1.0 > thr or 1.0 - px / hi > thr):
            self.hold_until[s] = now_ts + JUMP_HOLD_SECS

    def refresh(self, now_ts: Optional[float] = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        errors: List[str] = []
        if now_ts - self.meta_at >= META_REFRESH_SECS or not self.markets:
            try:
                self.markets = self._list_markets()
                self.meta_at = now_ts
            except Exception as e:                       # keep the last list
                errors.append(f"markets: {type(e).__name__}: {str(e)[:100]}")
        self._load_kept()
        t0, reopen, shut = fx_shut_window(now)
        weekday = not shut
        wk = self.kept.get(t0.strftime("%Y-%m-%dT%H:%MZ")) or {}
        close, refs = wk.get("close") or {}, wk.get("venue_ref") or {}
        try:
            mids, m_err = self._fetch()
        except Exception as e:  # noqa: BLE001
            mids, m_err = {}, {s: f"{type(e).__name__}: {str(e)[:80]}" for s in PROXY}
        for s, px in mids.items():
            if px > 0:
                self.mid[s] = {"mid": px, "ts": now_ts}
                self._jump(s, now_ts, px)
        for s, why in m_err.items():
            errors.append(f"{s}: {why}")
        ser: Dict[str, dict] = {}
        for s in self._series():
            md = self.mid.get(s)
            c, r = close.get(s), refs.get(s)
            e = None
            if not c:
                e = f"no close kept for the {t0:%m/%d %H:%MZ} close yet (wave_fair.py fills it)"
            elif not r:
                e = "no 24/7 price at the close kept"
            elif not md:
                e = m_err.get(s) or "no 24/7 price read yet"
            ser[s] = {"mid": (md or {}).get("mid"), "mid_ts": (md or {}).get("ts"), "ref": r, "close": c,
                      "spot": (float(c) * md["mid"] / float(r)) if not e else None,
                      "hold_until": self.hold_until.get(s), "err": e}
        mk: Dict[str, dict] = {}
        cut = now + timedelta(hours=CUTOFF_H)
        for m in self.markets:
            t = str(m.get("ticker") or "")
            s = t.split("-")[0]
            if s not in ser:
                continue
            if ser[s]["err"]:
                mk[t] = {"series": s, "err": ser[s]["err"]}
                continue
            k, T = m.get("floor_strike"), _close_dt(m)
            if m.get("strike_type") != "greater" or m.get("early_close_condition") or not k or T is None:
                mk[t] = {"series": s, "err": "not an 'above' strike with a close time"}
                continue
            if T <= now:
                mk[t] = {"series": s, "err": "measured"}
                continue
            if weekday and T < cut:
                mk[t] = {"series": s, "err": f"within {CUTOFF_H:g} h of its measurement", "cut": True}
                continue
            try:
                td = trading_days(T, t0, weekday_from=now if weekday else None)
                p, mg, bmax, amin = strike_fair(s, ser[s]["spot"], float(k), td, weekday)
            except Exception as e:  # noqa: BLE001
                mk[t] = {"series": s, "err": f"price: {type(e).__name__}: {str(e)[:80]}"}
                continue
            mk[t] = {"series": s, "strike": float(k), "close_ts": T.timestamp(), "t_days": round(td, 4),
                     "p": round(p, 5), "margin": round(mg, 2), "bid_max": bmax, "ask_min": amin,
                     "spot": ser[s]["spot"], "spot_ts": ser[s]["mid_ts"],
                     "hold_until": self.hold_until.get(s)}
        self.snap = {
            "checked_ts": now_ts, "mode": "weekday" if weekday else "weekend",
            "close_at": t0.strftime("%Y-%m-%dT%H:%MZ"), "reopen": reopen.strftime("%Y-%m-%dT%H:%MZ"),
            "series": ser, "markets": mk, "errors": errors,
            "model": {"vol_err": VOL_ERR, "requote_min": REQUOTE_MIN, "requote_sd": REQUOTE_SD,
                      "max_spread": MAX_SPREAD,
                      "jump_sd": JUMP_SD, "jump_window_s": JUMP_WINDOW_SECS, "jump_hold_s": JUMP_HOLD_SECS,
                      "cutoff_h": CUTOFF_H, "floor": WEEKDAY_FLOOR if weekday else WEEKEND_FLOOR},
        }
        return self.snap


def write_status(path: str, snap: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


if __name__ == "__main__":                               # a dry read, no orders
    import sys
    sys.path.insert(0, HERE)
    from kalshi_reads import kalshi_get
    ser_ = (sys.argv[1].split(",") if len(sys.argv) > 1 else list(SERIES))
    w = FxEventWatch(lambda p, q: kalshi_get(p, q), lambda: ser_)
    snap = w.refresh()
    print(snap["mode"], "close", snap["close_at"], "errors", snap["errors"])
    for s, v in snap["series"].items():
        print(s, v)
    n_ok = sum(1 for e in snap["markets"].values() if "p" in e)
    print(f"{n_ok}/{len(snap['markets'])} strikes priced")
    for t, e in sorted(snap["markets"].items())[:40]:
        if "p" in e:
            print(f"{t:44s} p {100 * e['p']:5.1f}  +-{e['margin']:4.1f}c  limits {e['bid_max']}/{e['ask_min']}  "
                  f"t {e['t_days']:.2f}d")
        else:
            print(t, e)
