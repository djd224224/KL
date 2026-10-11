#!/usr/bin/env python3
r"""send_ca_monitor.py -- the daily Carbon Arc email (Jack 2026-10-11: "ship
#3 for all three, but i want careful monitoring on carbon arc. give me a
daily email, reporting on each family and if it's performing as expected").

THE EMAIL (simplified 2026-10-11, Jack: "simplify the carbon arc email so
it's easier to read"): the day's total, one row per family with its verdict,
and plain-English reasons only for the families that aren't OK. The full
per-family numbers below go to the task log (and --dry), not the email.

ONE SECTION PER FAMILY (point of sale, card spend, apps, foot traffic, ad
spend) for the prior ET day. Each ends in a verdict, OK / WATCH / PROBLEM,
naming the checks that fired:

  money        reward, the day's own fills marked to now, carried positions
               re-marked, net; and the trailing 7 days
  sides        our fills by side (bid = we bought YES), cents per contract
               marked to now. #3 (a261542) blocks ASKS for point of sale /
               card spend / apps and BIDS for foot traffic / ad spend in the
               late-month window, because in Aug + Sep those were the sides
               makers lost on. Outside the window both sides fill, and the
               side #3 blocks should be the one losing more
  late window  00:00 ET 14 days before the month ends -> the stop 5 days
               before it (Oct 18 -> Oct 27): any fill or placement on the
               blocked side is a PROBLEM
  market       every public Kalshi trade of the day in the family's live
               markets, maker P&L by side marked to the current mid: the
               independent test of #3 once our blocked side stops filling,
               with a running total for the window
  read         the prism's last refresh and data_through against the
               family's normal cadence; a read that MOVED without a new
               refresh (the 10/10 point-of-sale hole, 2ba9306) is a PROBLEM
  gate         caps / stand-asides by side; one side capped on most of the
               family at once means the read disagrees with the whole book
  landing      fills inside the 1x data-landing window (e9aa0f2)
  book         open contracts long / short YES, unrealized vs cost

SOURCES, all read-only: the IMM dashboard data object (const D in
dashboard/imm_dashboard.html, rebuilt every 10 min), carbon_arc_fair.json +
carbon_arc_vintages.jsonl, the bot's day log and orders_<UTC date>.jsonl,
and Kalshi's PUBLIC GET /events, /markets, /markets/trades (no auth, ~4
req/s). No orders. The only files written are the sent-marker and
ca_monitor_market.json (each day's market check, for the window totals).
Imports imm_quote_gaps first, like the other morning emails, so the
launcher env and the alert credentials apply.

USAGE:
  python send_ca_monitor.py                   # the scheduled daily run
  python send_ca_monitor.py --dry             # build + print, send nothing
  python send_ca_monitor.py --test            # send now, ignore the marker
  python send_ca_monitor.py --day 2026-10-10  # a specific ET day
  python send_ca_monitor.py --no-market       # skip the Kalshi trade pull
Scheduled daily as "KL imm carbon-arc" (register_imm_carbon_arc.ps1), 07:35
ET. Sends every morning, so a silent day is distinguishable from a broken
task. All console output is ASCII (the task console is cp1252).
"""
import argparse
import glob
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import escape as _esc

# imm_quote_gaps FIRST: it mirrors the launcher's $ProbeEnv and the alert
# credential fallback into the environment before incentive_mm reads config.
import imm_quote_gaps as _gaps  # noqa: F401  (import side effects)
import incentive_mm as imm
from incentive_mm import log

# (prism category as ca_category returns it, short tag, name in the email)
FAMILIES = (("point of sale", "POS", "Point of sale"),
            ("credit card", "CC", "Card spend"),
            ("app", "APP", "Apps"),
            ("foot traffic", "FT", "Foot traffic"),
            ("advertising", "ADS", "Ad spend"))
# What #3 rests on: maker P&L to SETTLEMENT on every public trade in the
# late window (month end -14d to -5d), c/ct, (August, September).
BENCH = {"point of sale": {"bid": (14.8, 4.2), "ask": (-33.0, -13.2)},
         "credit card": {"bid": (-12.6, 6.6), "ask": (-9.9, -17.7)},
         "app": {"bid": (-0.5, -9.8), "ask": (-5.1, -19.9)},
         "foot traffic": {"bid": (-41.5, -33.1), "ask": (26.5, 28.2)},
         "advertising": {"bid": (None, -14.0), "ask": (None, 0.9)}}
# A refresh older than this many hours / data_through older than this many
# days is WATCH. Normal cadence (prism last_refreshed_at, 9/27-10/10): point
# of sale daily ~07:50 ET, foot traffic ~08h and apps 08-10h most days, card
# spend 11-14h on weekdays, ad spend in lumps every 1-5 days.
REFRESH_MAX_H = {"point of sale": 30, "foot traffic": 40, "app": 40,
                 "credit card": 80, "advertising": 150}
DATA_MAX_D = {"point of sale": 3, "credit card": 6, "app": 8,
              "foot traffic": 9, "advertising": 10}
# verdict thresholds (dollars, cents per contract, contracts)
NET_DAY_PROBLEM = -150.0
NET_DAY_WATCH = -50.0
LANDING_LOSS_WATCH = -40.0
SIDE_MIN_CTS = 300          # our fills: both sides need this many to compare
KEPT_SLACK_C = 10.0         # in the window: kept side this far under its benchmark = WATCH
MARKET_MIN_CTS = 1000       # market check: per side, to call it
CAP_STORM_FRAC = 0.6        # one side capped on this share of the family at once
DASH = os.path.join(imm.STATUS_DIR, "dashboard", "imm_dashboard.html")
MARKET_FILE = os.path.join(imm.STATUS_DIR, "ca_monitor_market.json")
VINTAGES = os.path.join(imm.STATUS_DIR, "carbon_arc_vintages.jsonl")
KALSHI = os.environ.get("IMM_CA_MONITOR_KALSHI",
                        "https://api.elections.kalshi.com/trade-api/v2")
MARKET_BUDGET_SECS = 900
TD = 'padding:4px 9px;border:1px solid #ddd;text-align:right;'
TDL = 'padding:4px 9px;border:1px solid #ddd;text-align:left;'
LEVELS = ("OK", "WATCH", "PROBLEM")


def day_window(day) -> tuple:
    """[00:00 ET day, 00:00 ET next day) as UTC datetimes."""
    a = imm.ET.localize(datetime(day.year, day.month, day.day))
    return a.astimezone(timezone.utc), (a + timedelta(days=1)).astimezone(timezone.utc)


def late_window(day) -> tuple:
    """(start, stop) UTC of the late-month window of the month `day` is in:
    CA_LATE_DAYS / CA_LATE_STOP_DAYS before 00:00 ET on the 1st of the next
    month (every family's measurement month ends there)."""
    y, m = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    me = imm.ET.localize(datetime(y, m, 1)).astimezone(timezone.utc)
    return (me - timedelta(days=imm.CA_LATE_DAYS),
            me - timedelta(days=imm.CA_LATE_STOP_DAYS))


def blocked_side(cat: str) -> str:
    """The side #3 blocks in the late window for this family."""
    by = getattr(imm, "CA_LATE_TOXIC_SIDE_BY_CAT", {}) or {}
    return by.get(cat, imm.CA_LATE_TOXIC_SIDE)


def other(side: str) -> str:
    return "ask" if side == "bid" else "bid"


def load_dashboard(path: str = DASH) -> dict:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    i = html.index("const D = ") + len("const D = ")
    data, _ = json.JSONDecoder().raw_decode(html, i)
    return data


def family_of(series: str):
    """The family (prism category) of a Carbon Arc series, else None."""
    cat = imm.ca_category(series)
    return cat if cat in {f[0] for f in FAMILIES} else None


def _num(x) -> float:
    return float(x) if isinstance(x, (int, float)) else 0.0


def money(D: dict, day, fam_of) -> dict:
    """{family: {rew, own, carried, net, net7, net3, days7}} from the
    dashboard's per-market day rows (own = pnl - cr - cs)."""
    ix = {f: i for i, f in enumerate(D["day_fields"])}
    out = {f[0]: defaultdict(float) for f in FAMILIES}
    for back in range(7):
        d = (day - timedelta(days=back)).isoformat()
        rows = (D.get("days", {}).get(d) or {}).get("m") or {}
        seen = set()
        for t, v in rows.items():
            fam = fam_of(t)
            if fam is None:
                continue
            seen.add(fam)
            rew, pnl = _num(v[ix["rew"]]), _num(v[ix["pnl"]])
            cr, cs = _num(v[ix["cr"]]), _num(v[ix["cs"]])
            o = out[fam]
            o["net7"] += rew + pnl
            if back < 3:
                o["net3"] += rew + pnl
            if back == 0:
                o["rew"] += rew
                o["own"] += pnl - cr - cs
                o["carried"] += cr + cs
                o["net"] += rew + pnl
        for fam in seen:
            out[fam]["days7"] += 1
    return {k: dict(v) for k, v in out.items()}


def side_stats(fills, fam_of, a_ts: float, b_ts: float, windows=None) -> dict:
    """{family: {side: [fills, contracts, $ marked to now, $ markout]}} for
    our maker fills in [a_ts, b_ts) (pads and taker fills left out); with
    `windows` ({family: [(a, b), ...]} ET hours) only fills inside them."""
    out = {f[0]: {"bid": [0, 0.0, 0.0, 0.0], "ask": [0, 0.0, 0.0, 0.0],
                  "max": 0.0} for f in FAMILIES}
    for f in fills:
        if not (a_ts <= f.get("ts", 0) < b_ts) or f.get("pad") or f.get("taker"):
            continue
        fam = fam_of(f.get("t", ""))
        if fam is None or f.get("side") not in ("bid", "ask"):
            continue
        if windows is not None:
            h = datetime.fromtimestamp(f["ts"], timezone.utc).astimezone(imm.ET).hour
            if not any((a <= h < b) if a < b else (h >= a or h < b)
                       for a, b in windows.get(fam, ())):
                continue
        n = _num(f.get("n"))
        s = out[fam][f["side"]]
        s[0] += 1
        s[1] += n
        s[2] += _num(f.get("mtc")) * n / 100.0
        s[3] += _num(f.get("mk"))
        out[fam]["max"] = max(out[fam]["max"], n)
    return out


def cpc(s) -> str:
    """'x.xc' per contract (marked to now) for a side_stats cell."""
    return f"{100.0 * s[2] / s[1]:+.1f}c" if s[1] else "-"


def late_violations(fills, D_orders, fam_of, a_ts: float, b_ts: float) -> dict:
    """{family: [fills on the blocked side, placements/amends on it]} inside
    each market's own late window (pads aside)."""
    out = {f[0]: [0, 0] for f in FAMILIES}

    def _inside(t: str, ts: float) -> bool:
        ws, stop = imm.ca_late_window_start(t), imm.ca_stop_utc(t)
        return (ws is not None and ws.timestamp() <= ts
                and (stop is None or ts < stop.timestamp()))

    for f in fills:
        if not (a_ts <= f.get("ts", 0) < b_ts) or f.get("pad"):
            continue
        fam = fam_of(f.get("t", ""))
        if fam and f.get("side") == blocked_side(fam) and _inside(f["t"], f["ts"]):
            out[fam][0] += 1
    for r in D_orders:
        if r.get("kind") not in ("place", "amend") or r.get("is_pad"):
            continue
        fam = fam_of(r.get("ticker", ""))
        if not fam or r.get("book_side") != blocked_side(fam):
            continue
        try:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
        except (KeyError, ValueError):
            continue
        if a_ts <= ts < b_ts and _inside(r["ticker"], ts):
            out[fam][1] += 1
    return out


def read_orders(a_utc: datetime, b_utc: datetime):
    """The bot's place/amend rows for Carbon Arc-looking tickers in [a, b)
    (orders_<UTC date>.jsonl; substring-filtered before json.loads)."""
    rows = []
    d = a_utc.date()
    while d <= b_utc.date():
        p = os.path.join(imm.STATUS_DIR, f"orders_{d.isoformat()}.jsonl")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if '"kind": "place"' not in line and '"kind": "amend"' not in line:
                        continue
                    if not re.search(r'"ticker": "KX[A-Z0-9]*(POS|CC|APP|FT|ADS)-', line):
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        d += timedelta(days=1)
    return rows


def read_health(fair: dict, cat: str, now: datetime, a_utc: datetime,
                b_utc: datetime, vintages: list) -> dict:
    """The family's read: series with a read / missing, newest refresh,
    data_through range, the day's read changes, and any change that came
    without a new refresh."""
    ent = {s: e for s, e in (fair.get("entries") or {}).items()
           if str(e.get("category") or "").strip().lower() == cat}
    miss = [s for s in (fair.get("missing") or [])
            if isinstance(s, str) and family_of(s) == cat]
    out = {"n": len(ent), "missing": miss, "refresh_h": None, "thru_max": None,
           "thru_min": None, "data_d": None, "changes": 0, "anomalies": []}
    if ent:
        refreshed = [imm.parse_iso_utc(str(e.get("refreshed_at") or "")) for e in ent.values()]
        refreshed = [r for r in refreshed if r is not None]
        if refreshed:
            out["refresh_h"] = (now - max(refreshed)).total_seconds() / 3600.0
        thru = sorted(str(e.get("data_through") or "") for e in ent.values() if e.get("data_through"))
        if thru:
            out["thru_min"], out["thru_max"] = thru[0], thru[-1]
            d = datetime.strptime(thru[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            out["data_d"] = (now - d).total_seconds() / 86400.0
    by_series = defaultdict(list)
    bursts = defaultdict(list)
    for r in vintages:
        if str(r.get("category") or "").strip().lower() == cat:
            by_series[r.get("series")].append(r)
    for s, rows in by_series.items():
        rows.sort(key=lambda r: str(r.get("fetched_at") or ""))
        for prev, cur in zip(rows, rows[1:]):
            ts = imm.parse_iso_utc(str(cur.get("fetched_at") or ""))
            if ts is None or not (a_utc <= ts < b_utc):
                continue
            out["changes"] += 1
            try:
                moved = abs(float(cur["mu"]) - float(prev["mu"])) > 1e-3 * max(1.0, abs(float(prev["mu"])))
            except (KeyError, TypeError, ValueError):
                moved = False
            if moved and cur.get("refreshed_at") == prev.get("refreshed_at") \
                    and cur.get("month") == prev.get("month"):
                bursts[ts.astimezone(imm.ET).strftime("%H:%M ET")].append(
                    f"{s} {prev.get('mu')} -> {cur.get('mu')} (data through "
                    f"{prev.get('data_through')} -> {cur.get('data_through')})")
    for at, items in sorted(bursts.items()):
        out["anomalies"].append(f"{len(items)} series at {at}, e.g. {sorted(items)[0]}")
    return out


_GATE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d)\d:\d\dZ \[IMM\] ca-fair "
                      r"(capped|not earning|stand-aside) (KX[A-Z0-9]+-[^:\s]+)")


def gate_activity(day, fam_of, selected: dict) -> dict:
    """{family: {capped_bid, capped_ask, stand, storm_side, storm_frac}} from
    the bot's day log (one file per ET day). A storm = the most distinct
    markets capped on one side inside one 10-minute bucket, over the
    family's selected markets."""
    out = {f[0]: {"capped_bid": 0, "capped_ask": 0, "notearn": 0, "stand": 0,
                  "storm_side": None, "storm_frac": 0.0, "storm_at": ""}
           for f in FAMILIES}
    path = os.path.join(imm.STATUS_DIR, f"incentive-mm-{day.isoformat()}.log")
    if not os.path.exists(path):
        return out
    buckets = defaultdict(set)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "ca-fair" not in line:
                continue
            m = _GATE_RE.match(line)
            if not m:
                continue
            bucket, kind, t = m.groups()
            fam = fam_of(t)
            if fam is None:
                continue
            side = "bid" if "bid side" in line else ("ask" if "ask side" in line else None)
            o = out[fam]
            if kind == "capped" and side:
                o[f"capped_{side}"] += 1
                buckets[(fam, side, bucket)].add(t)
            elif kind == "not earning":
                o["notearn"] += 1
            else:
                o["stand"] += 1
    for (fam, side, bucket), ts in buckets.items():
        n = max(1, selected.get(fam, 0))
        frac = len(ts) / n
        if frac > out[fam]["storm_frac"]:
            out[fam].update(storm_frac=frac, storm_side=side,
                            storm_at=f"{bucket}0Z")
    return out


def book(D: dict, day, fam_of) -> dict:
    """{family: {long, short, unreal, markets}} for the open positions,
    marked at the day's close mark (else the current book mid)."""
    ix = {f: i for i, f in enumerate(D["day_fields"])}
    rows = (D.get("days", {}).get(day.isoformat()) or {}).get("m") or {}
    out = {f[0]: {"long": 0.0, "short": 0.0, "unreal": 0.0, "markets": 0}
           for f in FAMILIES}
    for t, m in (D.get("markets") or {}).items():
        fam = fam_of(t)
        pos = _num(m.get("pos"))
        if fam is None or not pos:
            continue
        mark = None
        r = rows.get(t)
        if r is not None and isinstance(r[ix["mk1"]], (int, float)):
            mark = r[ix["mk1"]]
        cur = m.get("cur") or {}
        if mark is None and isinstance(cur.get("xb"), (int, float)) \
                and isinstance(cur.get("xa"), (int, float)):
            mark = (cur["xb"] + cur["xa"]) / 2.0
        o = out[fam]
        o["markets"] += 1
        o["long" if pos > 0 else "short"] += abs(pos)
        if mark is not None and isinstance(m.get("avg"), (int, float)):
            o["unreal"] += pos * (mark - m["avg"]) / 100.0
    return out


def selected_counts(D: dict, fam_of) -> dict:
    out = defaultdict(int)
    for t, m in (D.get("markets") or {}).items():
        fam = fam_of(t)
        if fam and m.get("sel"):
            out[fam] += 1
    return dict(out)


# ---- market check: every public trade of the day --------------------------

_RATE_LOCK = threading.Lock()
_NEXT_CALL = [0.0]
MARKET_RPS = 4.0          # shared across the worker threads


def _get(path: str, params: dict) -> dict:
    """One public Kalshi GET, paced to MARKET_RPS across threads; {} on failure."""
    url = f"{KALSHI}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(4):
        with _RATE_LOCK:
            wait = _NEXT_CALL[0] - time.monotonic()
            _NEXT_CALL[0] = max(_NEXT_CALL[0], time.monotonic()) + 1.0 / MARKET_RPS
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 + 3 * attempt)
    return {}


def _market_trades(m: dict, a_utc: datetime, b_utc: datetime) -> dict:
    """{"bid": [cts, $], "ask": [cts, $]}: maker P&L on one market's public
    trades in [a, b) priced 3-97c, marked to its current mid (a maker ask =
    a taker bought YES)."""
    o = {"bid": [0.0, 0.0], "ask": [0.0, 0.0]}
    try:
        bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
        mid = 100.0 * ((bid + ask) / 2.0 if bid > 0 and ask > 0
                       else float(m.get("last_price_dollars") or 0))
    except (TypeError, ValueError):
        return o
    cursor = None
    for _page in range(20):
        q = {"ticker": m["ticker"], "limit": 1000,
             "min_ts": int(a_utc.timestamp()), "max_ts": int(b_utc.timestamp())}
        if cursor:
            q["cursor"] = cursor
        d = _get("markets/trades", q)
        for tr in d.get("trades") or []:
            try:
                px = 100.0 * float(tr["yes_price_dollars"])
                n = float(tr.get("count_fp") or tr.get("count") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if not (3.0 < px < 97.0):
                continue
            side = "ask" if tr.get("taker_side") == "yes" else "bid"
            o[side][0] += n
            o[side][1] += n * ((px - mid) if side == "ask" else (mid - px)) / 100.0
        cursor = d.get("cursor")
        if not cursor or not d.get("trades"):
            break
    return o


def market_check(fair: dict, a_utc: datetime, b_utc: datetime,
                 budget_secs: float = MARKET_BUDGET_SECS) -> dict:
    """{family: {"bid": [cts, $], "ask": [cts, $], "markets": n, "partial": bool}}
    over every live market of the family's mapped series that has ever
    traded (4 worker threads, MARKET_RPS overall)."""
    t0 = time.monotonic()
    fams = {f[0] for f in FAMILIES}
    series = [(s, str(e.get("category") or "").strip().lower())
              for s, e in (fair.get("entries") or {}).items()]
    series = [(s, c) for s, c in series if c in fams]
    out = {c: {"bid": [0.0, 0.0], "ask": [0.0, 0.0], "markets": 0, "partial": False}
           for c in fams}
    jobs = []
    for s, cat in sorted(series):
        if time.monotonic() - t0 > budget_secs / 3:
            out[cat]["partial"] = True
            continue
        for ev in _get("events", {"series_ticker": s, "status": "open"}).get("events") or []:
            for m in _get("markets", {"event_ticker": ev["event_ticker"], "limit": 200}).get("markets") or []:
                try:
                    if float(m.get("volume_fp") or 0) > 0:
                        jobs.append((cat, m))
                except (TypeError, ValueError):
                    pass
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_market_trades, m, a_utc, b_utc): cat for cat, m in jobs}
        for fut in as_completed(futs):
            cat = futs[fut]
            try:
                o = fut.result()
            except Exception:
                out[cat]["partial"] = True
                continue
            for side in ("bid", "ask"):
                out[cat][side][0] += o[side][0]
                out[cat][side][1] += o[side][1]
            out[cat]["markets"] += 1
            if time.monotonic() - t0 > budget_secs:
                out[cat]["partial"] = True
                for f in futs:
                    f.cancel()
                break
    return out


def load_market_file() -> dict:
    try:
        with open(MARKET_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def save_market_file(data: dict) -> None:
    try:
        tmp = MARKET_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, MARKET_FILE)
    except OSError as e:
        log(f"! ca monitor: market file not written ({e!r})")


def window_market_totals(store: dict, day, ws: datetime, stop: datetime) -> dict:
    """{family: {"bid": [cts, $], "ask": [...]}} summed over the stored days
    inside the late window up to `day`."""
    tot = {f[0]: {"bid": [0.0, 0.0], "ask": [0.0, 0.0], "days": 0} for f in FAMILIES}
    for d, fams in store.items():
        try:
            dd = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        a, _b = day_window(dd)
        if not (ws <= a < stop) or dd > day:
            continue
        for cat, o in (fams or {}).items():
            if cat not in tot:
                continue
            for side in ("bid", "ask"):
                tot[cat][side][0] += o.get(side, [0, 0])[0]
                tot[cat][side][1] += o.get(side, [0, 0])[1]
            tot[cat]["days"] += 1
    return tot


# ---- verdict -----------------------------------------------------------------

def _et_at(z) -> str:
    """'2026-10-10 11:40Z' -> ' at 07:40 ET'; '' when unparseable."""
    try:
        t = datetime.strptime(str(z), "%Y-%m-%d %H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return f" at {t.astimezone(imm.ET):%H:%M} ET"


def verdict(f: dict) -> tuple:
    """(level, [reasons]) for one family's context, worst first. Pure: the
    tests drive it."""
    cat, probs, watch = f["cat"], [], []
    blk = blocked_side(cat)
    keep = other(blk)
    # Reasons are plain sentences (Jack 2026-10-11: "simplify the carbon arc
    # email so it's easier to read"); the numbers behind them stay in the
    # detail block (render_detail, task log / --dry).
    m = f.get("money") or {}
    net = m.get("net", 0.0)
    if net <= NET_DAY_PROBLEM:
        probs.append(f"Lost ${-net:,.0f} on the day")
    elif net <= NET_DAY_WATCH:
        watch.append(f"Lost ${-net:,.0f} on the day")
    viol = f.get("late_viol") or [0, 0]
    if viol[0] or viol[1]:
        probs.append(f"Late-month rule broken: {viol[0]} fill(s) and {viol[1]} order(s) "
                     f"on the BLOCKED {blk} side")
    rd = f.get("read") or {}
    for a in rd.get("anomalies") or []:
        probs.append("Our Carbon Arc read changed with no new data: "
                     + a.split(" (data through")[0])
    if rd.get("n", 0) == 0:
        probs.append("No Carbon Arc data for this family")
    if rd.get("refresh_h") is not None and rd["refresh_h"] > REFRESH_MAX_H.get(cat, 48):
        watch.append(f"No new Carbon Arc data for {rd['refresh_h']:.0f}h "
                     f"(usually under {REFRESH_MAX_H.get(cat, 48)}h)")
    if rd.get("data_d") is not None and rd["data_d"] > DATA_MAX_D.get(cat, 8):
        watch.append(f"Newest Carbon Arc data is {rd['data_d']:.0f} days old "
                     f"(through {rd.get('thru_max')})")
    kw = (f.get("sides_window") or {}).get(keep)
    bench = [x for x in (BENCH.get(cat, {}).get(keep) or ()) if x is not None]
    if kw and kw[1] >= SIDE_MIN_CTS and bench:
        kc = 100.0 * kw[2] / kw[1]
        if kc < min(bench) - KEPT_SLACK_C:
            watch.append(f"Late-month: the {keep}s we still quote are losing {-kc:.0f}c a "
                         f"contract, well under Aug/Sep ({min(bench):+.0f}c)")
    mw = f.get("market_window") or {}
    if mw:
        mb, mk = mw.get(blk) or [0, 0], mw.get(keep) or [0, 0]
        if mb[0] >= MARKET_MIN_CTS and mk[0] >= MARKET_MIN_CTS:
            bc, kc = 100.0 * mb[1] / mb[0], 100.0 * mk[1] / mk[0]
            if bc > kc:
                watch.append(f"Late-month: other makers did better on the {blk}s we block "
                             f"({bc:+.0f}c) than on the {keep}s we keep ({kc:+.0f}c), "
                             f"so the rule may be backwards")
    g = f.get("gate") or {}
    if g.get("storm_frac", 0.0) >= CAP_STORM_FRAC and f.get("selected", 0) >= 5:
        watch.append(f"Our read disagreed with the market on most of the family "
                     f"({g['storm_side']}s held back on {g['storm_frac']:.0%}"
                     f"{_et_at(g.get('storm_at'))})")
    land = f.get("landing") or {}
    land_pnl = sum((land.get(s) or [0, 0, 0.0])[2] for s in ("bid", "ask"))
    if land_pnl <= LANDING_LOSS_WATCH:
        watch.append(f"Lost ${-land_pnl:,.0f} on fills while new data was landing")
    if m.get("days7", 0) >= 3 and m.get("net3", 0.0) < 0:
        watch.append(f"Down ${-m['net3']:,.0f} over the last 3 days")
    level = "PROBLEM" if probs else ("WATCH" if watch else "OK")
    return level, probs + watch


# ---- build + render ------------------------------------------------------------

def build_report(day, with_market: bool = True, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    a_utc, b_utc = day_window(day)
    imm.load_ca_fair()
    fam_cache = {}

    def fam_of(ticker: str):
        s = str(ticker).split("-")[0]
        if s not in fam_cache:
            fam_cache[s] = family_of(s)
        return fam_cache[s]

    ctx = {"day": day, "now": now, "families": [], "notes": []}
    try:
        D = load_dashboard()
        gen = datetime.fromtimestamp(float(D.get("generated") or 0), timezone.utc)
        ctx["dash_at"] = gen
        if gen < b_utc:
            ctx["notes"].append(f"dashboard built {gen.astimezone(imm.ET):%m/%d %H:%M} ET, "
                                f"before the day ended: money and fills are partial")
    except Exception as e:
        D = {"day_fields": [], "days": {}, "fills": [], "markets": {}}
        ctx["notes"].append(f"dashboard unreadable ({type(e).__name__}: {e}); money / fills / book missing")
    try:
        with open(imm.CA_FAIR_FILE, encoding="utf-8") as f:
            fair = json.load(f) or {}
    except (OSError, ValueError) as e:
        fair = {}
        ctx["notes"].append(f"carbon_arc_fair.json unreadable ({e})")
    vint = []
    try:
        with open(VINTAGES, encoding="utf-8") as f:
            for line in f:
                try:
                    vint.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        ctx["notes"].append("carbon_arc_vintages.jsonl missing: read-change checks skipped")
    ws, stop = late_window(day)
    ctx["late"] = (ws, stop)
    ctx["late_active"] = a_utc < stop and b_utc > ws
    fills = D.get("fills") or []
    sel = selected_counts(D, fam_of) if D.get("markets") else {}
    mon = money(D, day, fam_of) if D.get("day_fields") else {}
    day_sides = side_stats(fills, fam_of, a_utc.timestamp(), b_utc.timestamp())
    sides7 = side_stats(fills, fam_of, (a_utc - timedelta(days=6)).timestamp(), b_utc.timestamp())
    sides_window = {}
    if ctx["late_active"]:
        sides_window = side_stats(fills, fam_of, ws.timestamp(), min(b_utc, stop).timestamp())
    landing = side_stats(fills, fam_of, a_utc.timestamp(), b_utc.timestamp(),
                         windows=getattr(imm, "CA_LANDING_WINDOWS", {}) or {})
    viol = {f[0]: [0, 0] for f in FAMILIES}
    if ctx["late_active"]:
        viol = late_violations(fills, read_orders(a_utc, b_utc), fam_of,
                               a_utc.timestamp(), b_utc.timestamp())
    gate = gate_activity(day, fam_of, sel)
    bk = book(D, day, fam_of) if D.get("day_fields") else {}
    store = load_market_file()
    mkt_day = {}
    if with_market:
        try:
            mkt_day = market_check(fair, a_utc, b_utc)
            store[day.isoformat()] = mkt_day
            save_market_file(store)
        except Exception as e:
            ctx["notes"].append(f"market check failed ({type(e).__name__}: {e})")
    else:
        mkt_day = store.get(day.isoformat()) or {}
    mwin = window_market_totals(store, day, ws, stop) if ctx["late_active"] else {}
    for cat, tag, name in FAMILIES:
        fctx = {"cat": cat, "tag": tag, "name": name,
                "money": mon.get(cat) or {}, "sides": day_sides.get(cat) or {},
                "sides7": sides7.get(cat) or {}, "landing": landing.get(cat) or {},
                "sides_window": sides_window.get(cat) or {},
                "late_viol": viol.get(cat) or [0, 0],
                "read": read_health(fair, cat, now, a_utc, b_utc, vint),
                "gate": gate.get(cat) or {}, "book": bk.get(cat) or {},
                "selected": sel.get(cat, 0),
                "market": mkt_day.get(cat) or {},
                "market_window": (mwin.get(cat) or {}) if ctx["late_active"] else {}}
        fctx["level"], fctx["reasons"] = verdict(fctx)
        ctx["families"].append(fctx)
    return ctx


def _usd(x) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.0f}"


def _side_line(s: dict, side: str) -> str:
    c = s.get(side) or [0, 0, 0.0, 0.0]
    return f"{c[0]} fills {c[1]:,.0f} ct {_usd(c[2])} ({cpc(c)})"


def _mkt_line(o: dict, side: str) -> str:
    c = o.get(side) or [0.0, 0.0]
    return (f"{c[0]:,.0f} ct {_usd(c[1])} ({100.0 * c[1] / c[0]:+.1f}c)" if c[0] else "no trades")


def _bench(cat: str, side: str) -> str:
    v = BENCH.get(cat, {}).get(side) or (None, None)
    return " / ".join("n/a" if x is None else f"{x:+.1f}c" for x in v)


def _totals(ctx) -> dict:
    """The day's money summed over the families that have it."""
    t = {"rew": 0.0, "trade": 0.0, "net": 0.0, "net7": 0.0, "n": 0}
    for f in ctx["families"]:
        m = f["money"]
        if not m:
            continue
        t["n"] += 1
        t["rew"] += m.get("rew", 0.0)
        t["trade"] += m.get("own", 0.0) + m.get("carried", 0.0)
        t["net"] += m.get("net", 0.0)
        t["net7"] += m.get("net7", 0.0)
    return t


def _flagged(ctx) -> list:
    """The families that aren't OK: PROBLEM before WATCH, worst day first."""
    return sorted((f for f in ctx["families"] if f["level"] != "OK"),
                  key=lambda f: (-LEVELS.index(f["level"]), (f["money"] or {}).get("net", 0.0)))


def subject_for(ctx) -> str:
    t = _totals(ctx)
    head = (f"IMM Carbon Arc {ctx['day']:%m/%d}: "
            + (f"net {_usd(t['net'])}" if t["n"] else "no money data"))
    parts = []
    for lvl in ("PROBLEM", "WATCH"):
        names = [f["name"] for f in _flagged(ctx) if f["level"] == lvl]
        if names:
            parts.append(f"{lvl} {', '.join(names)}")
    return head + " | " + (" | ".join(parts) if parts else "all OK")


def _late_text(ctx) -> str:
    ws, stop = ctx["late"]
    now = ctx["now"]
    if now < ws:
        days = round((ws - now).total_seconds() / 86400)
        return (f"Late-month rule (#3) starts {ws.astimezone(imm.ET):%a %m/%d} "
                f"(in {days} day{'' if days == 1 else 's'}) and ends "
                f"{stop.astimezone(imm.ET):%a %m/%d}.")
    if now < stop:
        by = defaultdict(list)
        for cat, _tag, name in FAMILIES:
            by[blocked_side(cat)].append(name.lower())
        blocks = "; ".join(f"{side}s blocked on {', '.join(n)}" for side, n in by.items())
        return (f"Late-month rule (#3) is ON until {stop.astimezone(imm.ET):%a %m/%d}: "
                f"{blocks}.")
    return (f"Late-month rule (#3) ended {stop.astimezone(imm.ET):%m/%d}; Carbon Arc "
            f"quoting is stopped for the rest of the month.")


def _fills_book(f) -> str:
    """'Our fills: 32 (3,030 contracts, -$392). Open positions: ...' for one family."""
    s = f["sides"]
    n, ct, pnl = (sum((s.get(x) or [0, 0, 0.0])[i] for x in ("bid", "ask")) for i in (0, 1, 2))
    out = (f"Our fills: {n} ({ct:,.0f} contracts, {_usd(pnl)})." if n else "Our fills: none.")
    b = f["book"]
    if b:
        out += (f" Open positions: {b.get('markets', 0)} markets, "
                f"{_usd(b.get('unreal', 0))} unrealized.")
    return out


DETAIL_WHERE = r"run-logs\incentive-mm\carbon-arc-task.log"


def render_text(ctx) -> str:
    """The email (Jack 2026-10-11: "simplify the carbon arc email so it's
    easier to read"): the day's total, one line per family, then a short
    block of plain reasons for each family that isn't OK. Every number
    behind them is in render_detail (task log / --dry)."""
    t = _totals(ctx)
    L = [f"Carbon Arc -- {ctx['day']:%a %m/%d}"]
    if t["n"]:
        L.append(f"Net {_usd(t['net'])} on the day (rewards {_usd(t['rew'])}, trading "
                 f"{_usd(t['trade'])}). Last 7 days: {_usd(t['net7'])}.")
    L.append(_late_text(ctx))
    L += [f"! {n}" for n in ctx["notes"]]
    L.append("")
    w = max(len(f["name"]) for f in ctx["families"])
    for f in ctx["families"]:
        m = f["money"]
        money = (f"day {_usd(m.get('net', 0)):>6}   7 days {_usd(m.get('net7', 0)):>6}"
                 if m else "no money data")
        L.append(f"{f['name']:<{w}}  {f['level']:<7}  {money}")
    flagged = _flagged(ctx)
    if flagged:
        L += ["", "NEEDS A LOOK"]
        for f in flagged:
            L += ["", f"{f['name']} -- {f['level']}"]
            L += [f"  - {r}" for r in f["reasons"]]
            L.append(f"  {_fills_book(f)}")
    else:
        L += ["", "Every family performed as expected."]
    L += ["", f"Full numbers per family: {DETAIL_WHERE}"]
    return "\n".join(L)


def render_detail(ctx) -> str:
    """Every number behind the verdicts, per family: the task log and
    --dry print it; the email does not carry it (2026-10-11)."""
    L = [f"Carbon Arc daily -- {ctx['day']:%a %Y-%m-%d} (ET day)", _late_text(ctx)]
    L += [f"! {n}" for n in ctx["notes"]]
    L.append("")
    for f in ctx["families"]:
        m, blk = f["money"], blocked_side(f["cat"])
        L.append(f"== {f['name']} ({f['tag']}): {f['level']}")
        for r in f["reasons"]:
            L.append(f"   - {r}")
        if m:
            L.append(f"   money: reward {_usd(m.get('rew', 0))}, own fills {_usd(m.get('own', 0))}, "
                     f"carried {_usd(m.get('carried', 0))} -> net {_usd(m.get('net', 0))}; "
                     f"7 days {_usd(m.get('net7', 0))}")
        L.append(f"   our fills  bids: {_side_line(f['sides'], 'bid')} | asks: {_side_line(f['sides'], 'ask')}")
        L.append(f"   7 days     bids: {_side_line(f['sides7'], 'bid')} | asks: {_side_line(f['sides7'], 'ask')}"
                 + ("" if ctx["late_active"] else "  (context only: #3 is about the late window)"))
        if f.get("sides_window"):
            L.append(f"   window     bids: {_side_line(f['sides_window'], 'bid')} | asks: "
                     f"{_side_line(f['sides_window'], 'ask')}")
        L.append(f"   #3 blocks the {blk}s in the late window (Aug / Sep makers: bids "
                 f"{_bench(f['cat'], 'bid')}, asks {_bench(f['cat'], 'ask')})")
        if f["market"]:
            L.append(f"   market (all makers, day, marked to now)  bids: {_mkt_line(f['market'], 'bid')} | "
                     f"asks: {_mkt_line(f['market'], 'ask')}"
                     + (" [partial]" if f["market"].get("partial") else ""))
        if f["market_window"]:
            L.append(f"   market, window so far  bids: {_mkt_line(f['market_window'], 'bid')} | "
                     f"asks: {_mkt_line(f['market_window'], 'ask')}")
        rd = f["read"]
        L.append(f"   read: {rd['n']} series"
                 + (f" (+{len(rd['missing'])} unmapped)" if rd["missing"] else "")
                 + (f", last refresh {rd['refresh_h']:.0f}h ago" if rd["refresh_h"] is not None else "")
                 + (f", data through {rd['thru_min']}..{rd['thru_max']}" if rd["thru_max"] else "")
                 + f", {rd['changes']} read change(s) on the day")
        g = f["gate"]
        L.append(f"   gate: capped bids {g.get('capped_bid', 0)}, asks {g.get('capped_ask', 0)}, "
                 f"not earning {g.get('notearn', 0)}, stand-asides {g.get('stand', 0)}"
                 + (f"; peak {g['storm_side']}s capped on {g['storm_frac']:.0%} of {f['selected']} selected"
                    if g.get("storm_side") else ""))
        ld = f["landing"]
        L.append(f"   landing window (1x): bids {_side_line(ld, 'bid')} | asks {_side_line(ld, 'ask')}"
                 + (f", biggest fill {ld.get('max', 0):.0f}" if ld.get("max") else ""))
        b = f["book"]
        if b:
            L.append(f"   open book: {b.get('markets', 0)} markets, long {b.get('long', 0):,.0f} / "
                     f"short {b.get('short', 0):,.0f} YES, unrealized {_usd(b.get('unreal', 0))}")
        L.append("")
    L.append("Verdict rules: PROBLEM = day net <= -$150, any blocked-side fill/order in the late "
             "window, a read that moved with no new data, or no read. WATCH = day net <= -$50, "
             "3-day net < 0, stale refresh/data, in the late window our kept side running "
             "10c/ct+ under its Aug/Sep benchmark or the market's makers doing better on "
             "the blocked side, one side capped on >= 60% of the family at once, "
             "landing-window fills <= -$40.")
    return "\n".join(L)


def render_html(ctx) -> str:
    color = {"OK": "#1a7f37", "WATCH": "#b26b00", "PROBLEM": "#c62828"}
    t = _totals(ctx)
    rows = []
    for f in ctx["families"]:
        m = f["money"]
        rows.append(
            "<tr>"
            f"<td style='{TDL}'><b>{_esc(f['name'])}</b></td>"
            f"<td style='{TDL};color:{color[f['level']]}'><b>{f['level']}</b></td>"
            f"<td style='{TD}'>{_usd(m.get('net', 0)) if m else '-'}</td>"
            f"<td style='{TD}'>{_usd(m.get('net7', 0)) if m else '-'}</td>"
            "</tr>")
    head = ("<tr>" + "".join(f"<th style='{TDL}'>{h}</th>" for h in
            ("Family", "Status", "Day", "7 days")) + "</tr>")
    top = (f"<p style='margin:0 0 4px;font-size:16px'><b>Net {_usd(t['net'])}</b> on the day "
           f"(rewards {_usd(t['rew'])}, trading {_usd(t['trade'])}). "
           f"Last 7 days: {_usd(t['net7'])}.</p>" if t["n"] else "")
    notes = "".join(f"<p style='margin:0 0 4px;color:#c62828'>{_esc(n)}</p>" for n in ctx["notes"])
    flagged = _flagged(ctx)
    if flagged:
        look = "<h3 style='margin:18px 0 0'>Needs a look</h3>"
        for f in flagged:
            look += (f"<p style='margin:12px 0 2px'><b>{_esc(f['name'])}</b> "
                     f"<span style='color:{color[f['level']]}'><b>{f['level']}</b></span></p>"
                     "<ul style='margin:0;padding-left:20px'>"
                     + "".join(f"<li>{_esc(r)}</li>" for r in f["reasons"])
                     + f"</ul><p style='margin:2px 0 0;color:#555'>{_esc(_fills_book(f))}</p>")
    else:
        look = "<p style='margin:14px 0 0'>Every family performed as expected.</p>"
    return (f"<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.45;max-width:640px'>"
            f"<h2 style='margin:0 0 6px'>Carbon Arc &mdash; {ctx['day']:%a %m/%d}</h2>{top}"
            f"<p style='margin:0 0 12px;color:#555'>{_esc(_late_text(ctx))}</p>{notes}"
            f"<table style='border-collapse:collapse'>{head}{''.join(rows)}</table>{look}"
            f"<p style='margin:18px 0 0;color:#888;font-size:12px'>Full numbers per family "
            f"(fills by side, read, gate, market check): {_esc(DETAIL_WHERE)}</p></div>")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker")
    ap.add_argument("--day", help="ET day to report (YYYY-MM-DD); default yesterday")
    ap.add_argument("--no-market", action="store_true",
                    help="skip the Kalshi public-trade pull (use any stored result)")
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else today_et - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_ca_monitor_sent_{today_et}.marker")
    if not (args.test or args.dry or args.day) and os.path.exists(marker):
        log(f"carbon arc email already sent for {today_et}; exiting")
        return 0
    ctx = build_report(day, with_market=not args.no_market, now=now_utc)
    text, html, subject = render_text(ctx), render_html(ctx), subject_for(ctx)
    log("carbon arc email: " + subject + "\n" + text + "\n\n-- detail --\n" + render_detail(ctx))
    if args.dry:
        return 0
    attempts = 1 if args.test else 8
    alerter = imm.Alerter("IMM-CA", live=True)
    if not alerter.enabled:
        log("cannot send carbon arc email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject, html=html)
        if ok:
            break
        log(f"carbon arc send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"carbon arc send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.day):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR, "imm_ca_monitor_sent_*.marker")):
            name = os.path.basename(old)[len("imm_ca_monitor_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
