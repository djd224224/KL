"""Weekend fair values for Kalshi's 'after economic events' FX + metals ladders (w1113-fxmetals).

Jack 2026-11-13: "Are there Fair value calculated on these markets?" -> "1" (stand down the
wrong-side quotes, build a fair before raising the budget), then "can you use the 24/7
prices, to build fair value?" and "Can you get the 24/7 prices on these markets to
calculate fair?". The bot has none for these series (SCAN_EXCLUDE_PREFIXES: live price
feeds), so each market's quotes followed its own book, and some books sit far from where
the underlying is.

Model: "above K at time T" = N(ln(S/K) / (vol_d * sqrt(trading days to T + gap))).
  S       Friday 11/13 5 PM ET close (metals: Kalshi's own weekly settlements KXGOLDW /
          KXSILVERW / KXCOPPERW at 22:00Z; FX: the public Yahoo chart's last bar), moved by
          --live: S x (24/7 price now / the same source at Friday's close), which drops
          each source's own basis. 24/7 sources (all public, no key):
            Hyperliquid trade.xyz perps  gold, silver, copper, JPY, EUR, GBP
            Kraken spot FX               AUD/USD, USD/CAD
            Bitso                        USD/MXN
          "At Friday's close" = the median close of the source's four 5-min bars ending
          21:45-22:00Z (one stray print doesn't move it); "now" = its bid/ask mid.
  time    trading days only (FX and metals are shut Fri 22:00Z - Sun 22:00Z), + a Sunday-open
          gap worth WEEKEND_GAP_DAYS of variance.
Writes {"at", "spot", "live", "failed", "vol_d", "margin", "valid_until", "fair"} to OUT
atomically; pvd_wave_tune --fair-file reads it. valid_until = 45 min after a --live refresh
(the wave's pass-7 run refreshes it every 15 min right before its tune), never past Sunday
21:30Z; past it the tune lets only floor sides rest. A source that fails keeps its series'
last live price from the previous file while that is under 45 min old; past that its series
gets no fair (floor sides only). Every source failing writes nothing.

usage: python wave_fair.py OUT.json [--live]
"""
import json, math, os, statistics, sys, urllib.request
from datetime import datetime, timedelta, timezone

KL = os.path.dirname(os.path.abspath(__file__))
CLOSE = {"KXUSDJPYEVENT": 159.686, "KXEURUSDEVENT": 1.1028, "KXAUDUSDEVENT": 0.6774,
         "KXUSDMXNEVENT": 18.523, "KXUSDCADEVENT": 1.4267, "KXGBPUSDEVENT": 1.3209,
         "KXGOLDEVENT": 3884.10, "KXSILVEREVENT": 48.75, "KXCOPPEREVENT": 6.15}
VOL_D = {"KXUSDJPYEVENT": .0057, "KXEURUSDEVENT": .0044, "KXAUDUSDEVENT": .0063,
         "KXUSDMXNEVENT": .0076, "KXUSDCADEVENT": .0038, "KXGBPUSDEVENT": .0050,
         "KXGOLDEVENT": .0113, "KXSILVEREVENT": .019, "KXCOPPEREVENT": .0139}
PROXY = {"KXGOLDEVENT": ("hl", "xyz:GOLD"), "KXSILVEREVENT": ("hl", "xyz:SILVER"),
         "KXCOPPEREVENT": ("hl", "xyz:COPPER"), "KXUSDJPYEVENT": ("hl", "xyz:JPY"),
         "KXEURUSDEVENT": ("hl", "xyz:EUR"), "KXGBPUSDEVENT": ("hl", "xyz:GBP"),
         "KXAUDUSDEVENT": ("kraken", "AUDUSD"), "KXUSDCADEVENT": ("kraken", "USDCAD"),
         "KXUSDMXNEVENT": ("bitso", "usd_mxn")}
T0 = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)      # the close CLOSE is from
REOPEN = datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc)  # FX / metals back: a static fair is stale
REF_STARTS = [T0 - timedelta(minutes=m) for m in (20, 15, 10, 5)]   # 5-min bars ending 21:45-22:00Z
WEEKEND_GAP_DAYS = 0.5
# base margin c for the tune, per series (the file's wins over --fair-margin): 2c since
# 2026-11-14 04:33Z (Jack: "tighten to 2c"); copper 6c (its CU/USD feed runs 24/7)
MARGIN = {s: 2.0 for s in CLOSE}
MARGIN["KXCOPPEREVENT"] = 6.0
STALE_MIN = 45


def _get(url: str, body: dict = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "User-Agent": "wave_fair"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _ref(closes_by_start: dict) -> float:
    xs = [closes_by_start[t] for t in REF_STARTS if t in closes_by_start]
    if len(xs) < 2:
        raise ValueError(f"only {len(xs)} of the 4 bars at Friday's close")
    return statistics.median(xs)


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def fetch_hl(coin: str, _cache={}) -> tuple:
    if "mids" not in _cache:
        meta, ctx = _get("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs", "dex": "xyz"})
        _cache["mids"] = {u["name"]: float(c.get("midPx") or c.get("markPx")) for u, c in zip(meta["universe"], ctx)
                          if (c.get("midPx") or c.get("markPx"))}
    bars = _get("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "5m", "startTime": _ms(REF_STARTS[0]), "endTime": _ms(REF_STARTS[-1]) + 60_000}})
    ref = _ref({datetime.fromtimestamp(int(b["t"]) / 1000, timezone.utc): float(b["c"]) for b in bars})
    return ref, _cache["mids"][coin]


def fetch_kraken(pair: str) -> tuple:
    o = _get(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=5&since={int(REF_STARTS[0].timestamp()) - 300}")
    rows = next(v for k, v in o["result"].items() if k != "last")
    ref = _ref({datetime.fromtimestamp(int(r[0]), timezone.utc): float(r[4]) for r in rows})
    t = next(iter(_get(f"https://api.kraken.com/0/public/Ticker?pair={pair}")["result"].values()))
    return ref, (float(t["b"][0]) + float(t["a"][0])) / 2


def fetch_bitso(book: str) -> tuple:
    o = _get(f"https://api.bitso.com/api/v3/ohlc?book={book}&time_bucket=300"
             f"&start={_ms(REF_STARTS[0])}&end={_ms(REF_STARTS[-1]) + 300_000}")
    ref = _ref({datetime.fromtimestamp(int(b["bucket_start_time"]) / 1000, timezone.utc): float(b["last_rate"])
                for b in o["payload"]})
    t = _get(f"https://api.bitso.com/v3/ticker/?book={book}")["payload"]
    return ref, (float(t["bid"]) + float(t["ask"])) / 2


FETCH = {"hl": fetch_hl, "kraken": fetch_kraken, "bitso": fetch_bitso}


def live_moves(prev: dict, now: datetime, fetch=None) -> tuple:
    """({series: {"src", "ref", "now", "ratio"[, "from_prev"]}}, {series: error}). A source
    that fails reuses the previous file's entry while that file is under STALE_MIN old."""
    fetch = fetch or FETCH
    out, failed = {}, {}
    prev_live = prev.get("live") or {}
    try:
        prev_at = datetime.fromisoformat(str(prev.get("at")).replace("Z", "+00:00"))
    except ValueError:
        prev_at = None
    for s, (src, sym) in PROXY.items():
        try:
            ref, cur = fetch[src](sym)
            if not (ref > 0 and cur > 0):
                raise ValueError(f"bad prices {ref} / {cur}")
            out[s] = {"src": f"{src}:{sym}", "ref": ref, "now": cur, "ratio": cur / ref}
        except Exception as ex:  # noqa: BLE001
            p = prev_live.get(s)
            if p and prev_at is not None and now - prev_at < timedelta(minutes=STALE_MIN):
                out[s] = dict(p, from_prev=str(prev.get("at")))
            else:
                failed[s] = f"{src}:{sym}: {ex!r:.100}"
    return out, failed


def trading_days(t: datetime) -> float:
    """Open-market days from T0 to t (Sun 22:00Z - Fri 22:00Z), + the Sunday-open gap."""
    d, x = 0.0, T0
    while x < t:
        step = min(t, x + timedelta(hours=1))
        wd, h = x.weekday(), x.hour
        if not (wd == 5 or (wd == 4 and h >= 22) or (wd == 6 and h < 22)):
            d += (step - x).total_seconds() / 86400
        x = step
    return max(d * 7 / 5, 0.02) + WEEKEND_GAP_DAYS


def N(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def main(out_path: str, live_on: bool) -> int:
    sys.path.insert(0, KL)
    from kalshi_reads import kalshi_get_all   # noqa: E402
    now = datetime.now(timezone.utc)
    spot, live, failed = dict(CLOSE), {}, {}
    if live_on:
        try:
            with open(out_path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except (OSError, ValueError):
            prev = {}
        live, failed = live_moves(prev, now)
        if not live:
            print(f"! every 24/7 source failed ({failed}): {out_path} left as it is (it runs out at its valid_until)")
            return 1
        for s, v in live.items():
            spot[s] = CLOSE[s] * v["ratio"]
    fair, skipped = {}, 0
    for s in CLOSE:
        if s in failed:
            continue                     # no live price: no fair, floor sides only
        for m in kalshi_get_all("/markets", {"series_ticker": s, "status": "open"}, items_key="markets") or []:
            K = float(m.get("floor_strike") or 0)
            if not K or m.get("strike_type") != "greater" or not m.get("close_time"):
                skipped += 1
                continue
            T = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            sig = VOL_D[s] * math.sqrt(trading_days(T))
            fair[m["ticker"]] = round(100 * N(math.log(spot[s] / K) / sig), 2)
    valid = min(now + timedelta(minutes=STALE_MIN), REOPEN - timedelta(minutes=30)) if live_on \
        else REOPEN - timedelta(minutes=30)
    doc = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "close_at": T0.strftime("%Y-%m-%dT%H:%MZ"),
           "close": CLOSE, "spot": {k: round(v, 6) for k, v in spot.items()}, "live": live, "failed": failed,
           "vol_d": VOL_D, "weekend_gap_days": WEEKEND_GAP_DAYS, "margin": MARGIN,
           "valid_until": valid.strftime("%Y-%m-%dT%H:%M:%SZ"), "fair": fair}
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=0, sort_keys=True)
    os.replace(tmp, out_path)
    moves = ", ".join(f"{s.replace('KX', '').replace('EVENT', '')} {100 * (v['ratio'] - 1):+.2f}%"
                      + (" (last good)" if v.get("from_prev") else "") for s, v in live.items())
    print(f"wrote {len(fair)} fairs ({skipped} skipped) valid to {doc['valid_until']}"
          + (f"; 24/7 moves since Fri close: {moves}" if live else "; Friday close only")
          + (f"; NO FAIR (floor sides only): {', '.join(failed)}" if failed else "") + f" -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.path.abspath(sys.argv[1]), "--live" in sys.argv))
