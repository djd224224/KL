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
    price only under 20 min old.

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
CLOSE = CLOSE_1113                   # the series this fair covers (and the 11/13 values)
VOL_D = {"KXUSDJPYEVENT": .0057, "KXEURUSDEVENT": .0044, "KXAUDUSDEVENT": .0063,
         "KXUSDMXNEVENT": .0076, "KXUSDCADEVENT": .0038, "KXGBPUSDEVENT": .0050,
         "KXGOLDEVENT": .0113, "KXSILVEREVENT": .019, "KXCOPPEREVENT": .0139}
PROXY = {"KXGOLDEVENT": ("hl", "xyz:GOLD"), "KXSILVEREVENT": ("hl", "xyz:SILVER"),
         "KXCOPPEREVENT": ("hl", "xyz:COPPER"), "KXUSDJPYEVENT": ("hl", "xyz:JPY"),
         "KXEURUSDEVENT": ("hl", "xyz:EUR"), "KXGBPUSDEVENT": ("hl", "xyz:GBP"),
         "KXAUDUSDEVENT": ("kraken", "AUDUSD"), "KXUSDCADEVENT": ("kraken", "USDCAD"),
         "KXUSDMXNEVENT": ("bitso", "usd_mxn")}
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


def fetch_hl(coin: str, starts: list = None, need_ref: bool = True, _cache={}) -> tuple:
    starts = starts or REF_STARTS
    if "mids" not in _cache:
        meta, ctx = _get("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs", "dex": "xyz"})
        _cache["mids"] = {u["name"]: float(c.get("midPx") or c.get("markPx")) for u, c in zip(meta["universe"], ctx)
                          if (c.get("midPx") or c.get("markPx"))}
    ref = None
    if need_ref:
        bars = _get("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "5m", "startTime": _ms(starts[0]), "endTime": _ms(starts[-1]) + 60_000}})
        ref = _ref({datetime.fromtimestamp(int(b["t"]) / 1000, timezone.utc): float(b["c"]) for b in bars}, starts)
    return ref, _cache["mids"][coin]


def fetch_kraken(pair: str, starts: list = None, need_ref: bool = True) -> tuple:
    starts = starts or REF_STARTS
    ref = None
    if need_ref:
        o = _get(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=5&since={int(starts[0].timestamp()) - 300}")
        rows = next(v for k, v in o["result"].items() if k != "last")
        ref = _ref({datetime.fromtimestamp(int(r[0]), timezone.utc): float(r[4]) for r in rows}, starts)
    t = next(iter(_get(f"https://api.kraken.com/0/public/Ticker?pair={pair}")["result"].values()))
    return ref, (float(t["b"][0]) + float(t["a"][0])) / 2


def fetch_bitso(book: str, starts: list = None, need_ref: bool = True) -> tuple:
    starts = starts or REF_STARTS
    ref = None
    if need_ref:
        o = _get(f"https://api.bitso.com/api/v3/ohlc?book={book}&time_bucket=300"
                 f"&start={_ms(starts[0])}&end={_ms(starts[-1]) + 300_000}")
        ref = _ref({datetime.fromtimestamp(int(b["bucket_start_time"]) / 1000, timezone.utc): float(b["last_rate"])
                    for b in o["payload"]}, starts)
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
            if p and same_close and prev_at is not None and now - prev_at < timedelta(minutes=reuse_min):
                out[s] = dict(p, from_prev=str(prev.get("at")))
            else:
                failed[s] = f"{src}:{sym}: {ex!r:.100}"
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
    if t0 == T0 and not wk["close"]:
        wk = dict(wk, close=dict(CLOSE_1113), source={s: "entered by hand 11/13" for s in CLOSE_1113})
    close, src, failed, added = dict(wk["close"]), dict(wk["source"]), {}, False
    refs = dict(wk.get("venue_ref") or {})
    for s, v in (venue_ref or {}).items():
        if v and s not in refs:
            refs[s], added = float(v), True
    for s in CLOSE:
        if s in close:
            continue
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
        book[key] = {"close": close, "source": src, "venue_ref": refs,
                     "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
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
        live, failed = live_moves(prev, now, t0=t0, refs=kept_refs(t0), reuse_min=reuse)
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
            if not K or m.get("strike_type") != "greater" or not m.get("close_time"):
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
