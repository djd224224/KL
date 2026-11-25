"""Weekend fair values for Kalshi's 'after economic events' FX + metals ladders (w1113-fxmetals).

Jack 2026-11-13: "Are there Fair value calculated on these markets?" -> "1" (stand down the
wrong-side quotes, build a fair before raising the budget), then "can you use the 24/7
prices, to build fair value?" and "Can you get the 24/7 prices on these markets to
calculate fair?". The bot has none for these series (SCAN_EXCLUDE_PREFIXES: live price
feeds), so each market's quotes followed its own book, and some books sit far from where
the underlying is.

Model: "above K at time T" = N(ln(S/K) / (vol_d * sqrt(trading days to T + gap))).
  S       this weekend's Friday 5 PM ET close (metals: Kalshi's own weekly settlements
          KXGOLDW / KXSILVERW / KXCOPPERW, settled ~80 min after the close; FX: the public
          Yahoo chart's last 5-min bar before it), moved by --live: S x (24/7 price now /
          the same source at Friday's close), which drops each source's own basis. 24/7
          sources (all public, no key):
            Hyperliquid trade.xyz perps  gold, silver, copper, JPY, EUR, GBP
            Kraken spot FX               AUD/USD, USD/CAD
            Bitso                        USD/MXN
          "At Friday's close" = the median close of the source's four 5-min bars ending at
          the close (one stray print doesn't move it); "now" = its bid/ask mid.
  time    trading days only (FX and metals are shut Fri 5 PM - Sun 5 PM ET), + a Sunday-open
          gap worth WEEKEND_GAP_DAYS of variance.

THE WEEKEND ROLLS BY ITSELF (2026-11-14, Jack: "default to running the wave for elections,
after economic events, and things of similar shapes to what's been run on waves so far"):
the close, its time and the reopen come from fx_shut_window(now), DST-aware, and each
weekend's closes are fetched once and kept in run-logs/pvd-scan/wave_fair_closes.json. A
close is used only when it sits within CLOSE_CHECK_PCT of the 24/7 source's own price at
the close (11/13: every series within 0.1%); a close not out yet (metals before Kalshi
settles) or refused leaves that series with no fair (floor sides only) until a later run
gets it. The 11/13 weekend keeps the closes entered by hand that night (CLOSE_1113; Kalshi's
settlements and Yahoo's bars reproduce them within 0.03%). Each 24/7 source's own price at
the close is kept there too ("venue_ref": Kraken serves 5-min bars only ~60 h back).

WEEKDAYS TOO (Jack 2026-11-14: "after-economic events can quote anytime not just when markets
are shut, but need a fair value"). While FX and metals trade (Sun 5 PM - Fri 5 PM ET) the
same model prices from now -- the last Friday close moved by the 24/7 source (which trades
alongside the market), open-market time from now to the measurement, the Sunday gap only if
a weekend lies between -- with what a fair refreshed only every 15 min needs on a market that
moves:
  * margin_extra (per ticker): STALE_SD sd of the fair's move over REFRESH_MIN of trading,
    100 x phi(z) x the 18-min sigma / the sigma to the measurement: ~8c at the money a day
    out, ~3c a week out, ~0 deep in the tails;
  * no fair within WEEKDAY_CUTOFF_H (12 h) of a strike's measurement: the release jump it
    settles one minute after outweighs the model's 25% vol-error allowance inside ~12 h;
  * "floor": 0 -- a ticker with no fair rests NOTHING (no 2c / 98c floor sides): a fair is
    required (the weekend keeps the 2c floors);
  * valid 20 min (one missed refresh stands it aside), and a failed source reuses its last
    price only under 20 min old;
  * THE FX PAIRS RE-ANCHOR EACH RUN (2026-11-23, fx_anchors): their last complete Yahoo 5-min
    bars and the venue's at the same minutes replace the Friday close, so a venue whose book fell
    apart at the close (Kraken USD/CAD 11/20) costs the weekend, not the week.

Writes {"at", "mode", "close_at", "reopen", "close", "spot", "live", "failed", "vol_d",
"margin", "margin_extra", "floor", "valid_until", "fair"} to OUT atomically; pvd_wave_tune
--fair-file reads it. Weekend: valid_until = 45 min after a --live refresh (the wave's pass-7
run refreshes it every 15 min right before its tune), never past 30 min before the reopen;
past it the tune lets only floor sides rest. A source that fails keeps its series' last live
price from the previous file while that is under 45 min old (20 on weekdays); past that its
series gets no fair. Every source failing writes nothing.

usage: python wave_fair.py OUT.json [--live]
"""
import json, math, os, statistics, sys, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KL = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")
# the 11/13 weekend's closes, entered by hand that night (the first weekend's fair is unchanged)
CLOSE_1113 = {"KXUSDJPYEVENT": 159.686, "KXEURUSDEVENT": 1.1028, "KXAUDUSDEVENT": 0.6774,
              "KXUSDMXNEVENT": 18.523, "KXUSDCADEVENT": 1.4267, "KXGBPUSDEVENT": 1.3209,
              "KXGOLDEVENT": 3884.10, "KXSILVEREVENT": 48.75, "KXCOPPEREVENT": 6.15}
# the series this fair covers: the 11/13 values, and None for a SESSION_ANCHOR series
CLOSE = dict(CLOSE_1113, KXINXEVENT=None)
VOL_D = {"KXUSDJPYEVENT": .0057, "KXEURUSDEVENT": .0044, "KXAUDUSDEVENT": .0063,
         "KXUSDMXNEVENT": .0076, "KXUSDCADEVENT": .0038, "KXGBPUSDEVENT": .0050,
         "KXGOLDEVENT": .0113, "KXSILVEREVENT": .019, "KXCOPPEREVENT": .0139,
         "KXINXEVENT": .0125}
PROXY = {"KXGOLDEVENT": ("hl", "xyz:GOLD"), "KXSILVEREVENT": ("hl", "xyz:SILVER"),
         "KXCOPPEREVENT": ("hl", "xyz:COPPER"), "KXUSDJPYEVENT": ("hl", "xyz:JPY"),
         "KXEURUSDEVENT": ("hl", "xyz:EUR"), "KXGBPUSDEVENT": ("hl", "xyz:GBP"),
         "KXAUDUSDEVENT": ("kraken", "AUDUSD"), "KXUSDCADEVENT": ("kraken", "USDCAD"),
         "KXUSDMXNEVENT": ("bitso", "usd_mxn"), "KXINXEVENT": ("hl", "xyz:SP500")}
# THE S&P 500 (KXINXEVENT, Jack 2026-11-19: "Build a fair, then list"): "S&P 500 at least 7,300
# after Michigan sentiment?" = the index at 10:14:59 AM ET on the release day. Priced like the
# FX strikes off its 24/7 source (Hyperliquid's trade.xyz SP500 perp), but its kept "close" is a
# SESSION ANCHOR, not the Friday 5 PM close: each run takes the latest regular-hours ^GSPC 5-min
# bar (9:30-16:00 ET, complete) and the perp's 5-min bar starting the same minute, so S = the
# index at that bar x (the perp now / the perp then). The perp's basis to the index moved -0.15%
# to +0.06% between the 11/13-11/18 closes (-0.048, -0.083, -0.149, +0.056%): a week-old Friday
# anchor would carry it, ~3c at the money two days out. The anchor is kept under the week's key
# in CLOSES_FILE (the bot's fx_event_fair reads it there), checked within CLOSE_CHECK_PCT of the
# perp; a failed read keeps the last anchor kept for the week; none = no fair. vol_d 1.25% = VIX
# 24.6 on 11/18 over sqrt(390) of this model's time units a year (20-day realized 22.3%).
SESSION_ANCHOR = {"KXINXEVENT": "%5EGSPC"}          # Yahoo chart symbol (^GSPC, URL-encoded)
SESSION_FIRST_BAR, SESSION_LAST_BAR = (9, 30), (15, 55)    # ET start of the first / last 5-min bar
# "above K" strikes: FX / metals "greater"; the S&P's "greater_or_equal" (the same on a continuum)
ABOVE_STRIKE_TYPES = ("greater", "greater_or_equal")
# where each weekend's close comes from (what was read by hand on 11/13)
CLOSE_SRC = {"KXGOLDEVENT": ("kalshi", "KXGOLDW"), "KXSILVEREVENT": ("kalshi", "KXSILVERW"),
             "KXCOPPEREVENT": ("kalshi", "KXCOPPERW"), "KXUSDJPYEVENT": ("yahoo", "JPY=X"),
             "KXEURUSDEVENT": ("yahoo", "EURUSD=X"), "KXAUDUSDEVENT": ("yahoo", "AUDUSD=X"),
             "KXUSDMXNEVENT": ("yahoo", "MXN=X"), "KXUSDCADEVENT": ("yahoo", "CAD=X"),
             "KXGBPUSDEVENT": ("yahoo", "GBPUSD=X")}
CLOSES_FILE = os.path.join(KL, "run-logs", "pvd-scan", "wave_fair_closes.json")
CLOSE_CHECK_PCT = 0.5                # a close this far (%) from the 24/7 source's own close is refused
T0 = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)      # the 11/13 close (CLOSE_1113's)
WEEKEND_GAP_DAYS = 0.5
# base margin c for the tune, per series (the file's wins over --fair-margin): 2c since
# 2026-11-14 04:33Z (Jack: "tighten to 2c"); copper too since that evening (it was 6c, as its CU/USD
# feed runs 24/7; Jack: "make copper and all metals/fx at the 2c margin")
MARGIN = {s: 2.0 for s in CLOSE}
STALE_MIN = 45
# WEEKDAYS (FX and metals trading; see the docstring)
WEEKDAY_VALID_MIN = 20       # one missed 15-min refresh and the fair stands aside
WEEKDAY_CUTOFF_H = 12.0      # no fair (nothing rests) within this of a strike's measurement
REFRESH_MIN = 18.0           # how old the fair gets: the 15-min pass + its run
STALE_SD = 2.0               # margin_extra = this many sd of the fair's move over REFRESH_MIN
WEEKEND_FLOOR, WEEKDAY_FLOOR = 2, 0   # c: what a ticker with no fair may still rest (0 = nothing)


def fx_shut_window(now: datetime) -> tuple:
    """(close, reopen, shut): the latest Friday 5 PM ET close at or before `now`, the Sunday
    5 PM ET reopen after it (both UTC), and whether `now` falls between them (FX and metals
    shut). DST-aware: 22:00Z in winter, 21:00Z in summer."""
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


def fx_next_window(now: datetime) -> tuple:
    """(close, reopen) of the shut window `now` is in, else the next one."""
    c, r, shut = fx_shut_window(now)
    return (c, r) if shut else _next_after(now)


def _next_after(now: datetime) -> tuple:
    """The first shut window whose close is after `now`."""
    x = now
    for _ in range(8):
        x += timedelta(days=1)
        c, r, _s = fx_shut_window(x)
        if c > now:
            return c, r
    raise ValueError("no Friday close within 8 days")


def ref_starts(t0: datetime) -> list:
    """The four 5-min bars ending at the close (their start times)."""
    return [t0 - timedelta(minutes=m) for m in (20, 15, 10, 5)]


REF_STARTS = ref_starts(T0)


def _get(url: str, body: dict = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "User-Agent": "wave_fair"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _ref(closes_by_start: dict, starts: list = None) -> float:
    xs = [closes_by_start[t] for t in (starts or REF_STARTS) if t in closes_by_start]
    if len(xs) < 2:
        raise ValueError(f"only {len(xs)} of the 4 bars at Friday's close")
    return statistics.median(xs)


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def bars_hl(coin: str, starts: list) -> dict:
    """{bar start: close} of the Hyperliquid perp's 5-min bars from starts[0] to starts[-1]."""
    bars = _get("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "5m", "startTime": _ms(starts[0]), "endTime": _ms(starts[-1]) + 60_000}})
    return {datetime.fromtimestamp(int(b["t"]) / 1000, timezone.utc): float(b["c"]) for b in bars or []}


def bars_kraken(pair: str, starts: list) -> dict:
    """{bar start: close} of Kraken's 5-min bars from starts[0] (it serves ~60 h back)."""
    o = _get(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=5&since={int(starts[0].timestamp()) - 300}")
    rows = next(v for k, v in o["result"].items() if k != "last")
    return {datetime.fromtimestamp(int(r[0]), timezone.utc): float(r[4]) for r in rows}


def bars_bitso(book: str, starts: list) -> dict:
    """{bar start: close} of Bitso's 5-min buckets from starts[0] to starts[-1]."""
    o = _get(f"https://api.bitso.com/api/v3/ohlc?book={book}&time_bucket=300"
             f"&start={_ms(starts[0])}&end={_ms(starts[-1]) + 300_000}")
    return {datetime.fromtimestamp(int(b["bucket_start_time"]) / 1000, timezone.utc): float(b["last_rate"])
            for b in o["payload"]}


VENUE_BARS = {"hl": bars_hl, "kraken": bars_kraken, "bitso": bars_bitso}


def venue_bars(src: str, sym: str, starts: list) -> dict:
    return VENUE_BARS[src](sym, starts)


def fetch_hl(coin: str, starts: list = None, need_ref: bool = True, _cache={}) -> tuple:
    starts = starts or REF_STARTS
    if "mids" not in _cache:
        meta, ctx = _get("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs", "dex": "xyz"})
        _cache["mids"] = {u["name"]: float(c.get("midPx") or c.get("markPx")) for u, c in zip(meta["universe"], ctx)
                          if (c.get("midPx") or c.get("markPx"))}
    ref = _ref(bars_hl(coin, starts), starts) if need_ref else None
    return ref, _cache["mids"][coin]


def fetch_kraken(pair: str, starts: list = None, need_ref: bool = True) -> tuple:
    starts = starts or REF_STARTS
    ref = _ref(bars_kraken(pair, starts), starts) if need_ref else None
    t = next(iter(_get(f"https://api.kraken.com/0/public/Ticker?pair={pair}")["result"].values()))
    return ref, (float(t["b"][0]) + float(t["a"][0])) / 2


def fetch_bitso(book: str, starts: list = None, need_ref: bool = True) -> tuple:
    starts = starts or REF_STARTS
    ref = _ref(bars_bitso(book, starts), starts) if need_ref else None
    t = _get(f"https://api.bitso.com/v3/ticker/?book={book}")["payload"]
    return ref, (float(t["bid"]) + float(t["ask"])) / 2


FETCH = {"hl": fetch_hl, "kraken": fetch_kraken, "bitso": fetch_bitso}


def live_moves(prev: dict, now: datetime, fetch=None, t0: datetime = None, refs: dict = None,
               reuse_min: float = STALE_MIN) -> tuple:
    """({series: {"src", "ref", "now", "ratio"[, "from_prev"]}}, {series: error}). `refs`: each
    source's price at the close already kept for this close (then only its price now is read).
    A source that fails reuses the previous file's entry while that file is under reuse_min old
    (and was built on the same close)."""
    fetch = fetch or FETCH
    t0 = t0 or T0
    refs = refs or {}
    starts = ref_starts(t0)
    out, failed = {}, {}
    prev_live = prev.get("live") or {}
    try:
        prev_at = datetime.fromisoformat(str(prev.get("at")).replace("Z", "+00:00"))
    except ValueError:
        prev_at = None
    same_close = str(prev.get("close_at") or t0.strftime("%Y-%m-%dT%H:%MZ")) == t0.strftime("%Y-%m-%dT%H:%MZ")
    for s, (src, sym) in PROXY.items():
        if s in SESSION_ANCHOR and not refs.get(s):
            continue          # no session anchor kept: session_anchors() reports why (no Friday bars)
        try:
            if refs.get(s):
                ref, cur = float(refs[s]), fetch[src](sym, starts, need_ref=False)[1]
            else:
                ref, cur = fetch[src](sym, starts)
            if not (ref > 0 and cur > 0):
                raise ValueError(f"bad prices {ref} / {cur}")
            out[s] = {"src": f"{src}:{sym}", "ref": ref, "now": cur, "ratio": cur / ref}
        except Exception as ex:  # noqa: BLE001
            p = prev_live.get(s)
            # an anchored series reuses its last price only against the same anchor
            same_ref = s not in SESSION_ANCHOR or (p or {}).get("ref") == refs.get(s)
            if p and same_close and same_ref and prev_at is not None \
                    and now - prev_at < timedelta(minutes=reuse_min):
                out[s] = dict(p, from_prev=str(prev.get("at")))
            else:
                failed[s] = f"{src}:{sym}: {ex!r:.100}"
    return out, failed


def fetch_yahoo_bars(sym: str, now: datetime, get=None) -> list:
    """[(bar start UTC, close)] of the public Yahoo chart's 5-min bars over the 5 days to now."""
    get = get or _get
    p2 = int(now.timestamp())
    r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=5m"
            f"&period1={p2 - 5 * 86400}&period2={p2}")["chart"]["result"][0]
    return [(datetime.fromtimestamp(int(t), timezone.utc), float(c))
            for t, c in zip(r.get("timestamp") or [], r["indicators"]["quote"][0].get("close") or [])
            if c is not None]


def fetch_hl_bar(coin: str, start: datetime, get=None) -> float:
    """The close of the Hyperliquid perp's 5-min bar starting at `start`."""
    get = get or _get
    bars = get("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "5m", "startTime": _ms(start), "endTime": _ms(start) + 60_000}})
    for b in bars or []:
        if int(b["t"]) == _ms(start):
            return float(b["c"])
    raise ValueError(f"no {coin} bar at {start:%Y-%m-%dT%H:%MZ}")


ANCHOR_FETCH = {"yahoo_bars": fetch_yahoo_bars, "hl_bar": fetch_hl_bar, "venue_bars": venue_bars}


def _session_bar(t: datetime, now: datetime) -> bool:
    """A complete regular-hours 5-min bar: it starts 9:30-15:55 ET on a weekday and ended
    a minute or more before now (Yahoo's trailing point at 16:00 / the bar in progress are
    not bars)."""
    lo = t.astimezone(ET)
    hm = (lo.hour, lo.minute)
    return (lo.weekday() < 5 and SESSION_FIRST_BAR <= hm <= SESSION_LAST_BAR
            and t + timedelta(minutes=6) <= now)


def session_anchors(t0: datetime, now: datetime, fetch=None, path: str = None) -> tuple:
    """({series: (index, perp, source)} read now, {series: why no anchor}) for SESSION_ANCHOR
    series; each new anchor is kept in CLOSES_FILE under t0's key as the series' "close" and
    "venue_ref". A failed read with an anchor already kept for the week is not a failure."""
    fetch = ANCHOR_FETCH if fetch is None else fetch
    path = path or CLOSES_FILE
    key = t0.strftime("%Y-%m-%dT%H:%MZ")
    out, failed = {}, {}
    for s, sym in SESSION_ANCHOR.items():
        coin = PROXY[s][1]
        try:
            bars = [(t, c) for t, c in fetch["yahoo_bars"](sym, now) if _session_bar(t, now)]
            if not bars:
                raise ValueError("no regular-hours bar in the last 5 days")
            t, c = bars[-1]
            ref = float(fetch["hl_bar"](coin, t))
            if not (c > 0 and ref > 0):
                raise ValueError(f"bad prices {c} / {ref}")
            off = 100 * (ref / c - 1)
            if abs(off) > CLOSE_CHECK_PCT:
                raise ValueError(f"the perp {ref:g} is {off:+.2f}% off the index {c:g} at {t:%m-%d %H:%MZ}")
            out[s] = (float(c), ref, f"{sym.replace('%5E', '^')} 5m bar {t:%Y-%m-%dT%H:%MZ} / hl:{coin}")
        except Exception as ex:  # noqa: BLE001
            failed[s] = f"session anchor {sym.replace('%5E', '^')}/{coin}: {str(ex)[:120]}"
    try:
        with open(path, encoding="utf-8") as fh:
            book = json.load(fh)
    except (OSError, ValueError):
        book = {}
    wk = dict(book.get(key) or {})
    close, src, refs = (dict(wk.get("close") or {}), dict(wk.get("source") or {}),
                        dict(wk.get("venue_ref") or {}))
    changed = False
    for s, (c, r, why) in out.items():
        if close.get(s) != c or refs.get(s) != r:
            close[s], refs[s], src[s], changed = c, r, why, True
    if changed:
        book[key] = dict(wk, close=close, source=src, venue_ref=refs,
                         at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        book = {k: v for k, v in sorted(book.items())[-8:]}      # the last 8 weekends
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(book, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    for s in list(failed):
        if close.get(s) and refs.get(s):
            del failed[s]                     # the week's last anchor stands
    return out, failed


# THE WEEKDAY FX ANCHOR (Jack 2026-11-23, "Yes, build it"). The weekend close is only as good as
# the 24/7 venue's price at it, and some venues' books fall apart as FX shuts: on 11/20 Kraken's
# USD/CAD went 1.431 -> 1.380 (20Z) -> 1.270 (21Z) -> 1.245 (22Z) -> 1.043 (23Z). The median of
# its four bars to the close, 1.39166, sat 3.04% under Yahoo's 1.4339, so the close was refused
# -- right for the weekend -- and with the Friday close the only anchor, USD/CAD had no fair all
# week, though Kraken traded normally again from Sunday (1.4359 vs Yahoo 1.4368 on 11/23). AUD
# wicked too (0.6439 at 22Z): its kept venue close sat 0.146% under Yahoo's, so the weekday spot
# read 0.08% high (~1c at the money a month out). So while FX trades, each run re-anchors every FX
# pair: its last FX_ANCHOR_BARS complete Yahoo 5-min bars since the Sunday reopen (below), the
# venue's 5-min bars at the same minutes, and the median venue / Yahoo ratio over them --
# refused, as the Friday close, past CLOSE_CHECK_PCT. Kept under the week's key in CLOSES_FILE
# as the pair's "close" (Yahoo's last bar) and "venue_ref" (that x the ratio), which the bot's
# fx_event_fair reads when the file changes: spot = the venue now / the ratio. The Friday values
# are kept under "friday". A refused or failed anchor keeps the week's last one (or the Friday
# close); none at all = no fair, as before. Weekends are unchanged; the metals keep Kalshi's weekly
# settlement (no Yahoo spot price); the S&P has its own session anchor. PVD_FX_ANCHOR=0 = off.
FX_ANCHOR = {s: sym for s, (kind, sym) in CLOSE_SRC.items() if kind == "yahoo"}   # the six FX pairs
# Measured 11/23 19:50-22:10Z, venue / Yahoo per 5-min bar: within ~+-0.03% of each pair's level
# most of the hour, single bars 0.05-0.1% off around the 5 PM ET roll (AUD -0.127%, MXN +0.104%,
# CAD +0.043% at 22:00-22:10Z), and Hyperliquid's GBP perp skips bars it didn't trade and stepped
# -0.10% -> +0.035% at 21:35Z. So: the median of the last 6 complete bars (30 min: a stray bar or
# two don't move it, a step shows within ~15 min), at least 3 the venue has, none starting in
# the daily roll (4:55-5:15 PM ET, Sunday's open too), 90 min back at most.
FX_ANCHOR_BARS = 6
FX_ANCHOR_MIN_BARS = 3
FX_ANCHOR_LOOKBACK_MIN = 90
FX_ROLL_ET = ((16, 55), (17, 15))           # [from, to) ET start of a bar left out
FX_ANCHOR_ON = os.environ.get("PVD_FX_ANCHOR", "1") != "0"
FX_ANCHOR_TAG = "weekday anchor"


def _in_roll(t: datetime) -> bool:
    lo = t.astimezone(ET)
    return FX_ROLL_ET[0] <= (lo.hour, lo.minute) < FX_ROLL_ET[1]


def fx_anchors(t0: datetime, now: datetime, fetch=None, path: str = None) -> tuple:
    """WEEKDAY FX ANCHOR: ({series: (close, venue_ref, source)} kept this run, {series: why not}).
    Nothing on a weekend, with the switch off, or without the Yahoo / venue bar readers."""
    fetch = ANCHOR_FETCH if fetch is None else fetch
    path = path or CLOSES_FILE
    if not FX_ANCHOR_ON or _shut(now) or "yahoo_bars" not in fetch or "venue_bars" not in fetch:
        return {}, {}
    _c, reopen, _s = fx_shut_window(now)

    def ok(t):
        return (t >= reopen and not _shut(t) and not _in_roll(t) and t + timedelta(minutes=6) <= now
                and now - t <= timedelta(minutes=FX_ANCHOR_LOOKBACK_MIN))
    out, failed = {}, {}
    for s, sym in FX_ANCHOR.items():
        src, vsym = PROXY[s]
        try:
            ys = [(t, c) for t, c in fetch["yahoo_bars"](sym, now) if ok(t) and c > 0][-FX_ANCHOR_BARS:]
            if len(ys) < FX_ANCHOR_MIN_BARS:
                raise ValueError(f"{len(ys)} complete Yahoo 5-min bar(s) in the last {FX_ANCHOR_LOOKBACK_MIN} min "
                                 f"outside the 5 PM ET roll")
            vb = fetch["venue_bars"](src, vsym, [t for t, _c in ys])
            pairs = [(t, c, float(vb[t])) for t, c in ys if vb.get(t)]
            if len(pairs) < FX_ANCHOR_MIN_BARS:
                raise ValueError(f"the venue has {len(pairs)} of those {len(ys)} bars")
            ratio = statistics.median(v / c for _t, c, v in pairs)
            off = 100 * (ratio - 1)
            if abs(off) > CLOSE_CHECK_PCT:
                raise ValueError(f"{src}:{vsym} is {off:+.2f}% off yahoo:{sym} over {len(pairs)} bars to "
                                 f"{pairs[-1][0]:%m-%d %H:%MZ}")
            c_last = ys[-1][1]
            out[s] = (float(c_last), float(c_last) * ratio,
                      f"{FX_ANCHOR_TAG}: yahoo:{sym} 5m bar {ys[-1][0]:%Y-%m-%dT%H:%MZ}, {src}:{vsym} "
                      f"{off:+.3f}% (median of {len(pairs)} bars)")
        except Exception as ex:  # noqa: BLE001
            failed[s] = f"{FX_ANCHOR_TAG} yahoo:{sym}/{src}:{vsym}: {str(ex)[:120]}"
    if out:
        key = t0.strftime("%Y-%m-%dT%H:%MZ")
        try:
            with open(path, encoding="utf-8") as fh:
                book = json.load(fh)
        except (OSError, ValueError):
            book = {}
        wk = dict(book.get(key) or {})
        close, src_, refs = (dict(wk.get("close") or {}), dict(wk.get("source") or {}),
                             dict(wk.get("venue_ref") or {}))
        fri = {k: dict(v) for k, v in (wk.get("friday") or {"close": {}, "venue_ref": {}, "source": {}}).items()}
        for s, (c, r, why) in out.items():
            if s in close and s not in fri["close"] and not str(src_.get(s, "")).startswith(FX_ANCHOR_TAG):
                fri["close"][s], fri["venue_ref"][s], fri["source"][s] = close[s], refs.get(s), src_.get(s)
            close[s], refs[s], src_[s] = c, r, why
        book[key] = dict(wk, close=close, source=src_, venue_ref=refs, friday=fri,
                         at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        book = {k: v for k, v in sorted(book.items())[-8:]}      # the last 8 weekends
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(book, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    return out, failed


def fetch_kalshi_weekly(series_w: str, t0: datetime, get=None) -> float:
    """Kalshi's weekly settlement of series_w (KXGOLDW ...) for the week closing at t0: its
    event's expiration value (11/13: 3884.10 at 23:20Z for a 22:00Z close)."""
    if get is None:
        sys.path.insert(0, KL)
        from kalshi_reads import kalshi_get as get   # noqa: E402
    for e in (get("/events", {"series_ticker": series_w, "status": "settled", "limit": 10}) or {}).get("events") or []:
        try:
            at = datetime.fromisoformat(str(e.get("strike_date")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at == t0:
            for m in (get("/markets", {"event_ticker": e["event_ticker"], "limit": 5}) or {}).get("markets") or []:
                v = m.get("expiration_value")
                if v not in (None, ""):
                    return float(v)
    raise ValueError(f"no settled {series_w} event at {t0:%Y-%m-%dT%H:%MZ} yet")


def fetch_yahoo_close(sym: str, t0: datetime, get=None) -> float:
    """The public Yahoo chart's last 5-min bar before t0 (what was read by hand on 11/13:
    JPY=X 159.686)."""
    get = get or _get
    p1, p2 = int(t0.timestamp()) - 3600, int(t0.timestamp()) + 600
    r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=5m&period1={p1}&period2={p2}"
            )["chart"]["result"][0]
    bars = [(t, c) for t, c in zip(r.get("timestamp") or [], r["indicators"]["quote"][0].get("close") or [])
            if c is not None and t < t0.timestamp()]
    if not bars:
        raise ValueError("no bar before the close")
    t, c = bars[-1]
    if t0.timestamp() - t > 30 * 60:
        raise ValueError(f"the last bar is {(t0.timestamp() - t) / 60:.0f} min before the close")
    return float(c)


CLOSE_FETCH = {"kalshi": fetch_kalshi_weekly, "yahoo": fetch_yahoo_close}


def kept_refs(t0: datetime, path: str = None) -> dict:
    """Each 24/7 source's price at the close t0, as kept in CLOSES_FILE ({} if none yet)."""
    try:
        with open(path or CLOSES_FILE, encoding="utf-8") as fh:
            return dict(((json.load(fh) or {}).get(t0.strftime("%Y-%m-%dT%H:%MZ")) or {}).get("venue_ref") or {})
    except (OSError, ValueError, AttributeError):
        return {}


def weekend_closes(t0: datetime, venue_ref: dict, fetch=None, path: str = None) -> tuple:
    """({series: close}, {series: source}, {series: why not}) for the weekend closing at t0.
    Kept per weekend in CLOSES_FILE once checked against the 24/7 source's own close
    (venue_ref, from live_moves, kept there too); the 11/13 weekend is CLOSE_1113. A close
    not out yet, or more than CLOSE_CHECK_PCT off its venue's, leaves its series with no fair
    this run."""
    fetch = fetch or CLOSE_FETCH
    path = path or CLOSES_FILE
    key = t0.strftime("%Y-%m-%dT%H:%MZ")
    try:
        with open(path, encoding="utf-8") as fh:
            book = json.load(fh)
    except (OSError, ValueError):
        book = {}
    wk = book.get(key) or {"close": {}, "source": {}}
    wk = dict(wk, close=dict(wk.get("close") or {}), source=dict(wk.get("source") or {}))
    if t0 == T0 and not any(s in wk["close"] for s in CLOSE_1113):
        # (a session anchor kept before them is not one of the 11/13 closes)
        wk["close"].update(CLOSE_1113)
        wk["source"].update({s: "entered by hand 11/13" for s in CLOSE_1113})
    close, src, failed, added = dict(wk["close"]), dict(wk["source"]), {}, False
    refs = dict(wk.get("venue_ref") or {})
    for s, v in (venue_ref or {}).items():
        if v and s not in refs:
            refs[s], added = float(v), True
    for s in CLOSE:
        if s in close or s in SESSION_ANCHOR:
            continue                     # (an anchored series' close is session_anchors')
        kind, sym = CLOSE_SRC[s]
        try:
            v = float(fetch[kind](sym, t0))
            ref = venue_ref.get(s)
            if not ref:
                raise ValueError("no 24/7 price at the close to check it against")
            off = 100 * (v / ref - 1)
            if abs(off) > CLOSE_CHECK_PCT:
                raise ValueError(f"{v:g} is {off:+.2f}% off the 24/7 source's {ref:g} at the close")
            close[s], src[s], added = v, f"{kind}:{sym}", True
        except Exception as ex:  # noqa: BLE001
            failed[s] = f"close {kind}:{sym}: {str(ex)[:120]}"
    if added:
        # (dict(wk, ...): the week's other keys -- the weekday anchors' "friday" record -- stay)
        book[key] = dict(wk, close=close, source=src, venue_ref=refs,
                         at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        book = {k: v for k, v in sorted(book.items())[-8:]}      # the last 8 weekends
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(book, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    return close, src, failed


def _shut(x: datetime) -> bool:
    lo = x.astimezone(ET)
    wd, h = lo.weekday(), lo.hour
    return wd == 5 or (wd == 4 and h >= 17) or (wd == 6 and h < 17)


def next_reopen(a: datetime) -> datetime:
    """The first Sunday 5 PM ET reopen after `a`."""
    _c, r, _s = fx_shut_window(a)
    return r if r > a else _next_after(a)[1]


def trading_days(t: datetime, t0: datetime = None, weekday_from: datetime = None) -> float:
    """Open-market days from the close t0 to t (Sun 5 PM - Fri 5 PM ET), + the Sunday-open gap.
    weekday_from (a moment while FX trades): from it instead, with the gap only if a reopen
    lies between it and t."""
    d, x = 0.0, (weekday_from or t0 or T0)
    while x < t:
        # to the next whole hour (ET's offsets are whole hours, so the open and close fall on one)
        step = min(t, x.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        if not _shut(x):
            d += (step - x).total_seconds() / 86400
        x = step
    gap = WEEKEND_GAP_DAYS if weekday_from is None or next_reopen(weekday_from) <= t else 0.0
    return max(d * 7 / 5, 0.02) + gap


def N(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _write(out_path: str, doc: dict) -> None:
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=0, sort_keys=True)
    os.replace(tmp, out_path)


def main(out_path: str, live_on: bool, now: datetime = None, list_markets=None) -> int:
    """Weekend (FX and metals shut): the Friday close moved by the 24/7 sources, as since 11/13.
    Weekday (they trade; Jack 2026-11-14: "after-economic events can quote anytime not just when
    markets are shut, but need a fair value"): the same from now, with margin_extra, the
    12-h cutoff, floor 0 and a 20-min life (the docstring)."""
    if list_markets is None:
        sys.path.insert(0, KL)
        from kalshi_reads import kalshi_get_all   # noqa: E402
        list_markets = lambda s: kalshi_get_all("/markets", {"series_ticker": s, "status": "open"},  # noqa: E731
                                                items_key="markets") or []
    now = now or datetime.now(timezone.utc)
    t0, reopen, shut = fx_shut_window(now)
    weekday = not shut
    z = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")    # noqa: E731
    reuse = WEEKDAY_VALID_MIN if weekday else STALE_MIN
    live, failed = {}, {}
    if live_on:
        try:
            with open(out_path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except (OSError, ValueError):
            prev = {}
        _anc, a_fail = session_anchors(t0, now)       # the S&P's anchor first: kept_refs reads it
        # ...and on weekdays every FX pair's (a refused one keeps the week's last: not a failure)
        fx_anc, fx_fail = fx_anchors(t0, now) if weekday else ({}, {})
        if fx_anc or fx_fail:
            print("weekday FX anchors: "
                  + ", ".join(f"{s.replace('KX', '').replace('EVENT', '')} {100 * (r / c - 1):+.3f}%"
                              for s, (c, r, _w) in sorted(fx_anc.items()))
                  + "".join(f"; KEPT LAST ({why})" for _s, why in sorted(fx_fail.items())))
        live, failed = live_moves(prev, now, t0=t0, refs=kept_refs(t0), reuse_min=reuse)
        for s, why in a_fail.items():
            failed.setdefault(s, why)
        if not live:
            print(f"! every 24/7 source failed ({failed}): {out_path} left as it is (it runs out at its valid_until)")
            return 1
    elif weekday:
        print("! a weekday fair needs --live (the 24/7 prices): nothing written")
        return 1
    close, csrc, cfail = weekend_closes(t0, {s: v["ref"] for s, v in live.items()})
    for s, why in cfail.items():
        failed.setdefault(s, why)
    spot = {s: close[s] * (live[s]["ratio"] if live_on else 1.0) for s in close
            if s not in failed and (s in live or not live_on)}
    fair, extra, skipped, cut = {}, {}, 0, 0
    sig_r = {s: v * math.sqrt(REFRESH_MIN / 1440 * 7 / 5) for s, v in VOL_D.items()}
    for s in CLOSE:
        if s not in spot:
            continue                     # no close or no live price: no fair
        for m in list_markets(s):
            K = float(m.get("floor_strike") or 0)
            if not K or m.get("strike_type") not in ABOVE_STRIKE_TYPES or not m.get("close_time"):
                skipped += 1
                continue
            T = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            if weekday and T - now < timedelta(hours=WEEKDAY_CUTOFF_H):
                cut += 1                 # too close to its measurement: no fair, nothing rests
                continue
            sig = VOL_D[s] * math.sqrt(trading_days(T, t0, weekday_from=now if weekday else None))
            zz = math.log(spot[s] / K) / sig
            fair[m["ticker"]] = round(100 * N(zz), 2)
            if weekday:
                # how far the fair can move before the next refresh: STALE_SD sd of REFRESH_MIN
                extra[m["ticker"]] = round(STALE_SD * 100 * math.exp(-zz * zz / 2) / math.sqrt(2 * math.pi)
                                           * sig_r[s] / sig, 2)
    if weekday:
        valid = now + timedelta(minutes=WEEKDAY_VALID_MIN) if live_on else now
    else:
        valid = min(now + timedelta(minutes=STALE_MIN), reopen - timedelta(minutes=30)) if live_on             else reopen - timedelta(minutes=30)
    doc = {"at": z(now), "mode": "weekday" if weekday else "weekend",
           "close_at": t0.strftime("%Y-%m-%dT%H:%MZ"), "reopen": z(reopen),
           "close": close, "close_src": csrc, "spot": {k: round(v, 6) for k, v in spot.items()}, "live": live,
           "failed": failed, "vol_d": VOL_D, "weekend_gap_days": WEEKEND_GAP_DAYS, "margin": MARGIN,
           "margin_extra": extra, "floor": WEEKDAY_FLOOR if weekday else WEEKEND_FLOOR,
           "cutoff_h": WEEKDAY_CUTOFF_H if weekday else None,
           "valid_until": z(valid), "fair": fair}
    _write(out_path, doc)
    moves = ", ".join(f"{s.replace('KX', '').replace('EVENT', '')} {100 * (v['ratio'] - 1):+.2f}%"
                      + (" (last good)" if v.get("from_prev") else "") for s, v in live.items())
    ex = sorted(extra.values())
    print(f"{doc['mode']}: wrote {len(fair)} fairs ({skipped} skipped"
          + (f", {cut} within {WEEKDAY_CUTOFF_H:g} h of their measurement: nothing rests" if weekday else "")
          + f") valid to {doc['valid_until']}, close {doc['close_at']}"
          + (f", staleness margin median {ex[len(ex) // 2]:.1f}c max {ex[-1]:.1f}c" if ex else "")
          + (f"; 24/7 moves since Fri close: {moves}" if live else "; Friday close only")
          + (f"; NO FAIR ({'nothing rests' if weekday else 'floor sides only'}): {', '.join(failed)}"
             if failed else "") + f" -> {out_path}")
    return 0

if __name__ == "__main__":
    sys.exit(main(os.path.abspath(sys.argv[1]), "--live" in sys.argv))
