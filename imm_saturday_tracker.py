#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""imm_saturday_tracker.py — weekly re-measurement of the Saturday ladder
multiplier (Jack 2026-09-12: "Saturday multiplier of 1.5x, only on long-dated
families. and lets remeasure each weekend to understand performance").

For every ET day since the multiplier went live (SINCE, default 2026-09-12)
it computes, separately for the LONG-DATED families the multipliers apply
to and for the EXCLUDED daily families (imm.is_daily_series: the prefix
floor gas/diesel/rain/temp plus the structural class the bot persists in
daily_series.json):

  resting     mean own resting contracts (cycle log `quoted`, one cycle =
              1/cycles-that-day of a day)
  rent        modelled reward accrual, $/day (est_frac x pool_per_day per
              cycle; pre-realization — paid credits ran ~1.2x modelled since
              Aug 1, ~2x on the mention family)
  fills       own maker fills from the fills_*.jsonl sink, contracts
  turnover    fills / resting  (contracts filled per resting contract-day)
  rent/fill   rent per filled contract, cents   <- the decision metric
  mo24        24h mark-out per filled contract, cents, from cycle-log mids
              (the early read on fresh fills: available the next day)
  settled     share of filled contracts whose market has settled
  loss/fill   settlement loss per filled contract, cents, on the settled
              subset (settlements_*.jsonl sink; fills in on later runs as
              markets settle)
  net/fill    rent/fill - loss/fill (cents)
  net/ct-day  rent per resting contract-day - turnover x loss/fill (cents):
              what a size multiplier actually scales
  mult        mean cycle-log hour_mult on the group's quoted rows OUTSIDE
              the 0-9am ET quiet window — proof the multiplier was live
              (Saturday long-dated 1.5, everything else 1.0)

grouped per day, pooled by day type (Saturday / Sunday / Weekday) since
SINCE, and set against the pre-change baseline (2026-08-08..09-11, measured
2026-09-12 from the account fills API + cycle logs, same definitions).

The bot's OWN sinks are the source (IMM_LOGGING.md): fills_*.jsonl is exactly
the bot's fills (no account-sharing ambiguity), settlements_*.jsonl carries
the result of every market it held at settle. Cycle-log parsing (~100 MB/day)
is cached per file day under STATUS_DIR/sat_tracker_cache/; the two newest
files are always re-parsed because they are still growing.

Scheduled "KL imm saturday-tracker" WEEKLY Monday 07:40 ET (after the 07:10
digest / 07:20 gaps / 07:25 opportunistic). --print builds and prints only;
--dry builds, prints and skips the email; --test emails without the sent
marker; --html-out PATH also writes the HTML body to PATH (with --print: a
no-send preview of the email). Credentials: ALERT_EMAIL_FROM /
ALERT_EMAIL_PASSWORD from the
environment, falling back to HKCU\\Environment like the other IMM reports.
"""
import argparse
import bisect
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.abspath(__file__))
LAUNCHER_PATH = os.path.join(REPO, "run_incentive_mm.ps1")


def _env_from_registry(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError):
        return ""


def _apply_launcher_env() -> dict:
    """Mirror the live bot's config (the launcher's $ProbeEnv `set NAME=VALUE&&`
    pairs) BEFORE incentive_mm is imported, so SAT_SIZE_MULT / SAT_MULT_EXCLUDE
    here are the live ones."""
    applied = {}
    try:
        with open(LAUNCHER_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return applied
    for chunk in re.findall(r'\$ProbeEnv\s*=\s*"(set .*?)"', text, re.S):
        for name, val in re.findall(r"set ([A-Za-z_][A-Za-z0-9_]*)=([^&]*)&&", chunk):
            os.environ[name] = val
            applied[name] = val
    return applied


_LAUNCHER_ENV = _apply_launcher_env()
for _v in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD"):
    if not os.environ.get(_v):
        _val = _env_from_registry(_v)
        if _val:
            os.environ[_v] = _val

import incentive_mm as imm                      # noqa: E402
from incentive_mm import ET, STATUS_DIR, log    # noqa: E402

import numpy as np                              # noqa: E402
import pandas as pd                             # noqa: E402

SINCE = os.environ.get("IMM_SAT_TRACKER_SINCE", "2026-09-12")
CACHE_DIR = os.path.join(STATUS_DIR, "sat_tracker_cache")
imm.load_daily_series_file()          # the bot's persisted structural daily set
EXCL = tuple(imm.DAILY_PREFIXES)
GROUPS = ("long-dated", "excluded")
COLS = ("ts,ticker,ext_bid,ext_ask,yes_depth,no_depth,target,est_frac,qual_sides,acct_pos,own_pos,"
        "pool_per_day,quoted,own_bid_ct,own_ask_ct,own_bid_top,own_ask_top,own_pad_bid_ct,"
        "own_pad_ask_ct,want_bid_ct,want_ask_ct,want_pad_ct,hour_mult,rung_lo,room_buy,room_sell,"
        "vol24h,discount,is_scan,is_sticky,reduce_only,fast,run_id,config_hash").split(",")

# Pre-change baseline, 2026-08-08..09-11 (25 weekday / 4 Saturday / 4 Sunday
# full days), measured 2026-09-12 with these same definitions from the account
# fills API joined to the cycle logs. Modelled rent, pre-realization.
BASELINE = {
    ("long-dated", "Weekday"):  dict(resting=12745, rent=198.4, fills=6539, turnover=0.51, rent_per_fill=3.03, loss_per_fill=1.92, net_per_fill=1.11, net_per_ct_day=0.57),
    ("long-dated", "Saturday"): dict(resting=13209, rent=137.6, fills=1020, turnover=0.08, rent_per_fill=13.48, loss_per_fill=5.43, net_per_fill=8.05, net_per_ct_day=0.62),
    ("long-dated", "Sunday"):   dict(resting=6351, rent=43.7, fills=2361, turnover=0.37, rent_per_fill=1.85, loss_per_fill=1.53, net_per_fill=0.32, net_per_ct_day=0.12),
    ("excluded", "Weekday"):    dict(resting=2165, rent=107.8, fills=2850, turnover=1.32, rent_per_fill=3.78, loss_per_fill=4.14, net_per_fill=-0.36, net_per_ct_day=-0.47),
    ("excluded", "Saturday"):   dict(resting=1736, rent=76.2, fills=1811, turnover=1.04, rent_per_fill=4.21, loss_per_fill=2.63, net_per_fill=1.58, net_per_ct_day=1.64),
    ("excluded", "Sunday"):     dict(resting=1393, rent=77.4, fills=1436, turnover=1.03, rent_per_fill=5.39, loss_per_fill=9.48, net_per_fill=-4.09, net_per_ct_day=-4.21),
}


def group_of(series: str) -> str:
    """Same classification the bot sizes with: prefix floor + the structural
    daily set (imm.is_daily_series), so 'excluded' here == no multiplier."""
    return "excluded" if imm.is_daily_series(series) else "long-dated"


def day_type(d: str) -> str:
    wd = datetime.strptime(d, "%Y-%m-%d").weekday()
    return "Saturday" if wd == 5 else ("Sunday" if wd == 6 else "Weekday")


# ---------------------------------------------------------------------------
# cycle logs -> per (et_date, et_hour, series) sums, cycles per hour, mids
# ---------------------------------------------------------------------------

def _parse_cycle_file(path: str):
    df = pd.read_csv(path, names=COLS, header=None, dtype=str, engine="c", on_bad_lines="skip")
    df = df[df["ts"] != "ts"]
    ts = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df[ts.notna()].copy()
    ts = ts[ts.notna()]
    for c in ("ext_bid", "ext_ask", "est_frac", "pool_per_day", "quoted", "hour_mult"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    et = ts.dt.tz_convert("America/New_York")
    df["et_date"] = et.dt.strftime("%Y-%m-%d")
    df["et_hour"] = et.dt.hour
    df["series"] = df["ticker"].str.split("-").str[0]
    df["quoted"] = df["quoted"].fillna(0.0)
    df["est_usd"] = df["est_frac"].fillna(0.0) * df["pool_per_day"].fillna(0.0)
    q = df[df["quoted"] > 0]
    ser = df.groupby(["et_date", "et_hour", "series"]).agg(
        sum_est_usd=("est_usd", "sum"), sum_quoted=("quoted", "sum"), n_rows=("ts", "size")).reset_index()
    # hour_mult outside the 0-9am ET quiet window (its x2 would otherwise
    # dominate a partial morning): Saturday long-dated rows should read the
    # Saturday knob exactly, everything else 1.0.
    qd = q[~q["et_hour"].between(0, 9)]
    hm = qd.groupby(["et_date", "et_hour", "series"]).agg(
        q_rows=("ts", "size"), hm_sum=("hour_mult", "sum")).reset_index()
    ser = ser.merge(hm, on=["et_date", "et_hour", "series"], how="left").fillna({"q_rows": 0, "hm_sum": 0.0})
    cyc = df.groupby(["et_date", "et_hour"])["ts"].nunique().reset_index(name="n_cycles")
    df["b10"] = (ts.astype("int64") // 10**9 // 600 * 600).values
    df["mid"] = (df["ext_bid"] + df["ext_ask"]) / 2.0
    mids = df.dropna(subset=["mid"]).groupby(["ticker", "b10"])["mid"].mean().reset_index()
    return ser, cyc, mids


def load_cycle_data(first_file_day: str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fresh_floor = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    sers, cycs, mids = [], [], []
    for path in sorted(glob.glob(os.path.join(STATUS_DIR, "cycle_log_*.csv"))):
        day = os.path.basename(path)[10:20]
        if day < first_file_day or day > today_utc:
            continue
        ps = os.path.join(CACHE_DIR, f"{day}_series.csv")
        pc = os.path.join(CACHE_DIR, f"{day}_cycles.csv")
        pm = os.path.join(CACHE_DIR, f"{day}_mids.csv")
        if day < fresh_floor and all(os.path.exists(p) for p in (ps, pc, pm)):
            sers.append(pd.read_csv(ps, dtype={"et_date": str}))
            cycs.append(pd.read_csv(pc, dtype={"et_date": str}))
            mids.append(pd.read_csv(pm))
            continue
        t0 = time.time()
        try:
            s, c, m = _parse_cycle_file(path)
        except Exception as e:                      # a torn file must not kill the report
            log(f"[SAT] ! cycle log {day} unreadable: {e}")
            continue
        s.to_csv(ps, index=False)
        c.to_csv(pc, index=False)
        m.to_csv(pm, index=False)
        sers.append(s)
        cycs.append(c)
        mids.append(m)
        log(f"[SAT] parsed cycle_log_{day}.csv ({len(s)} series-hours) in {time.time() - t0:.0f}s")
    if not sers:
        return pd.DataFrame(), pd.DataFrame(), {}
    ser = pd.concat(sers, ignore_index=True)
    cyc = pd.concat(cycs, ignore_index=True).groupby(["et_date", "et_hour"])["n_cycles"].sum().reset_index()
    mid_tab = {}
    allm = pd.concat(mids, ignore_index=True).groupby(["ticker", "b10"])["mid"].mean().reset_index()
    for tk, g in allm.groupby("ticker"):
        g = g.sort_values("b10")
        mid_tab[tk] = (g["b10"].astype(int).tolist(), g["mid"].astype(float).tolist())
    return ser, cyc, mid_tab


# ---------------------------------------------------------------------------
# fills + settlements sinks
# ---------------------------------------------------------------------------

def load_fills(first_file_day: str) -> pd.DataFrame:
    rows = []
    for path in sorted(glob.glob(os.path.join(STATUS_DIR, "fills_*.jsonl"))):
        day = os.path.basename(path)[6:16]
        if day < first_file_day:
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("is_taker"):
                    continue
                side = (r.get("side") or "").lower()
                action = (r.get("action") or "buy").lower()
                yp = float(r.get("yes_price_cents") or 0.0)
                px = yp if side == "yes" else 100.0 - yp
                if action == "sell":            # selling YES at p == buying NO at 100-p
                    side = "no" if side == "yes" else "yes"
                    px = 100.0 - px
                t = int(float(r.get("ts") or 0))
                et = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(ET)
                rows.append(dict(t=t, et_date=et.strftime("%Y-%m-%d"), ticker=r.get("ticker"),
                                 series=r.get("series") or str(r.get("ticker")).split("-")[0],
                                 eff_side=side, px=px, cnt=float(r.get("count") or 0.0)))
    return pd.DataFrame(rows)


def load_results() -> dict:
    res = {}
    for path in sorted(glob.glob(os.path.join(STATUS_DIR, "settlements_*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("result") in ("yes", "no"):
                    res[r["ticker"]] = r["result"]
    return res


def mid_at(mid_tab: dict, ticker: str, t: int, horizon: int):
    ent = mid_tab.get(ticker)
    if not ent:
        return np.nan
    keys, vals = ent
    target = (t + horizon) // 600 * 600
    i = bisect.bisect_left(keys, target)
    if i < len(keys) and keys[i] - target <= 7200:
        return vals[i]
    return np.nan


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

def build_rows(ser, cyc, mid_tab, fills, results, through_et: str):
    """One row per (et_date, group) for SINCE..through_et, plus per-day hours
    logged (partial days are flagged and kept out of the pooled rows)."""
    if ser.empty:
        return pd.DataFrame()
    ser = ser[(ser["et_date"] >= SINCE) & (ser["et_date"] <= through_et)].copy()
    cyc = cyc[(cyc["et_date"] >= SINCE) & (cyc["et_date"] <= through_et)]
    day_cycles = cyc.groupby("et_date")["n_cycles"].sum()
    day_hours = cyc.groupby("et_date")["et_hour"].nunique()
    ser["group"] = ser["series"].map(group_of)
    g = ser.groupby(["et_date", "group"]).agg(sum_est=("sum_est_usd", "sum"), sum_q=("sum_quoted", "sum"),
                                              q_rows=("q_rows", "sum"), hm_sum=("hm_sum", "sum")).reset_index()
    g["cycles"] = g["et_date"].map(day_cycles)
    g["hours"] = g["et_date"].map(day_hours)
    g["resting"] = g["sum_q"] / g["cycles"]
    g["rent"] = g["sum_est"] / g["cycles"]
    g["mult"] = g["hm_sum"] / g["q_rows"].replace(0, np.nan)

    if not fills.empty:
        f = fills[(fills["et_date"] >= SINCE) & (fills["et_date"] <= through_et)].copy()
        f["group"] = f["series"].map(group_of)
        f["result"] = f["ticker"].map(results)
        f["settle_pnl"] = np.where(f["result"].isna(), np.nan,
                                   f["cnt"] * ((f["result"] == f["eff_side"]).astype(float) * 100.0 - f["px"]) / 100.0)
        mo = []
        for r in f.itertuples():
            m = mid_at(mid_tab, r.ticker, r.t, 86400)
            mo.append(np.nan if np.isnan(m) else ((m - r.px) if r.eff_side == "yes" else ((100.0 - m) - r.px)))
        f["mo24"] = mo
        f["mo24_w"] = f["mo24"] * f["cnt"]
        f["mo24_n"] = np.where(f["mo24"].notna(), f["cnt"], 0.0)
        f["settled_cts"] = np.where(f["settle_pnl"].notna(), f["cnt"], 0.0)
        fa = f.groupby(["et_date", "group"]).agg(fills=("cnt", "sum"), settled_cts=("settled_cts", "sum"),
                                                 settle_pnl=("settle_pnl", "sum"), mo24_w=("mo24_w", "sum"),
                                                 mo24_n=("mo24_n", "sum")).reset_index()
        g = g.merge(fa, on=["et_date", "group"], how="left")
    for c in ("fills", "settled_cts", "settle_pnl", "mo24_w", "mo24_n"):
        if c not in g.columns:
            g[c] = 0.0
    g = g.fillna({"fills": 0.0, "settled_cts": 0.0, "settle_pnl": 0.0, "mo24_w": 0.0, "mo24_n": 0.0})
    g["day_type"] = g["et_date"].map(day_type)
    g["partial"] = g["hours"] < 20
    return g


def _derive(a: pd.DataFrame) -> pd.DataFrame:
    """Per-row metrics from summed columns (works for single days and pools)."""
    out = pd.DataFrame(index=a.index)
    out["days"] = a["days"]
    out["resting"] = a["resting"] / a["days"]
    out["rent$/d"] = a["rent"] / a["days"]
    out["fills/d"] = a["fills"] / a["days"]
    out["turnover"] = a["fills"] / a["resting"].replace(0, np.nan)
    out["rent/fill"] = 100.0 * a["rent"] / a["fills"].replace(0, np.nan)
    out["mo24"] = a["mo24_w"] / a["mo24_n"].replace(0, np.nan)
    out["settled%"] = 100.0 * a["settled_cts"] / a["fills"].replace(0, np.nan)
    out["loss/fill"] = -100.0 * a["settle_pnl"] / a["settled_cts"].replace(0, np.nan)
    out["net/fill"] = out["rent/fill"] - out["loss/fill"]
    out["net/ct-day"] = 100.0 * a["rent"] / a["resting"].replace(0, np.nan) - out["turnover"] * out["loss/fill"]
    out["mult"] = a["mult"]
    return out


# ---------------------------------------------------------------------------
# rendering. One set of derived frames (_tables) feeds two renderers: the plain
# text that goes to the console, the task log and the email's text part, and
# the HTML email body. Jack 2026-09-21: "This email is hard to read, make it
# more legible, clear, add tables instead of just raw text" -- the HTML was a
# <pre> dump of DataFrame.to_string(); it is real tables now, with the
# Saturday rows tinted, the pre-change baseline set directly under each
# pooled day-type row with a delta row, and the column legend as a table
# instead of a run-on sentence.
# ---------------------------------------------------------------------------

FMT = {"days": "{:.0f}", "resting": "{:,.0f}", "rent$/d": "{:,.1f}", "fills/d": "{:,.0f}", "turnover": "{:.2f}",
       "rent/fill": "{:.2f}", "mo24": "{:.2f}", "settled%": "{:.0f}", "loss/fill": "{:.2f}", "net/fill": "{:.2f}",
       "net/ct-day": "{:.2f}", "mult": "{:.2f}"}
METRICS = ("resting", "rent$/d", "fills/d", "turnover", "rent/fill", "mo24", "settled%", "loss/fill",
           "net/fill", "net/ct-day", "mult")
# the baseline was measured without mark-out / settled share / hour_mult
BASELINE_METRICS = ("resting", "rent$/d", "fills/d", "turnover", "rent/fill", "loss/fill", "net/fill", "net/ct-day")
LABELS = {"days": "Days", "resting": "Resting cts", "rent$/d": "Rent $/day", "fills/d": "Fills/day",
          "turnover": "Turnover", "rent/fill": "Rent/fill ¢", "mo24": "Mark-out 24h ¢",
          "settled%": "Settled %", "loss/fill": "Loss/fill ¢", "net/fill": "Net/fill ¢",
          "net/ct-day": "Net/ct-day ¢", "mult": "Mult"}
GOOD_POS = frozenset(("mo24", "net/fill", "net/ct-day"))   # bigger is better
BAD_POS = frozenset(("loss/fill",))                          # bigger is worse
# In a DELTA row the rent columns carry a sign too: more rent per fill than the
# baseline is the whole point of the multiplier. Level rows leave them plain
# (rent is always positive there, so colouring it would be noise).
DELTA_GOOD_POS = GOOD_POS | frozenset(("rent$/d", "rent/fill"))
DAY_TYPES = ("Saturday", "Weekday", "Sunday")
BASELINE_LABEL = "baseline Aug 8 – Sep 11"

_FONT = "font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#222"
_TD = "padding:4px 9px;border:1px solid #ddd;text-align:right;white-space:nowrap"
_TDL = "padding:4px 9px;border:1px solid #ddd;text-align:left;white-space:nowrap"
_TH = "padding:5px 9px;border:1px solid #ccc;text-align:right;background:#f0f0f0;font-weight:600;white-space:nowrap"
_THL = "padding:5px 9px;border:1px solid #ccc;text-align:left;background:#f0f0f0;font-weight:600;white-space:nowrap"
_SAT_BG = "#eaf2fb"          # Saturday rows: the multiplier's own day
_MUTED = "#888"


def _isnan(v) -> bool:
    return v is None or (isinstance(v, float) and np.isnan(v))


def _tables(g: pd.DataFrame):
    """(per_day, pooled_m, base): one row per (date, day_type, group) with the
    partial flag kept as a column; full-day pools by (group, day_type); and the
    pre-change baseline by (group, day_type). Shared by both renderers."""
    base = pd.DataFrame([dict(group=k[0], day_type=k[1], **v) for k, v in BASELINE.items()]).set_index(["group", "day_type"])
    base = base.rename(columns={"rent": "rent$/d", "fills": "fills/d", "rent_per_fill": "rent/fill",
                                "loss_per_fill": "loss/fill", "net_per_fill": "net/fill", "net_per_ct_day": "net/ct-day"})
    if g.empty:
        return pd.DataFrame(), pd.DataFrame(), base
    g = g.copy()
    g["days"] = 1.0
    per_day = _derive(g.set_index(["et_date", "day_type", "group"])).reset_index()
    per_day["partial"] = g["partial"].values
    full = g[~g["partial"]]
    pooled = full.groupby(["group", "day_type"]).agg(days=("et_date", "nunique"), resting=("resting", "sum"), rent=("rent", "sum"),
                                                     fills=("fills", "sum"), settled_cts=("settled_cts", "sum"),
                                                     settle_pnl=("settle_pnl", "sum"), mo24_w=("mo24_w", "sum"),
                                                     mo24_n=("mo24_n", "sum"), mult=("mult", "mean"))
    pooled_m = _derive(pooled) if not pooled.empty else pd.DataFrame()
    return per_day, pooled_m, base


def _hours_summary(hours: dict, html: bool = False) -> str:
    """{0:2.0,...,9:2.0} -> '0-9 ET x2'; runs of equal multipliers collapse.
    ASCII unless html: the text part is printed to a cp1252 console by the
    scheduled task, and a stray glyph there crashes the run before the email
    is sent (the new-programs email hit exactly this on 2026-09-16..18)."""
    dash, times = ("–", "×") if html else ("-", "x")
    if not hours:
        return "off"
    items = sorted(hours.items())
    runs, start, prev, m = [], items[0][0], items[0][0], items[0][1]
    for h, v in items[1:]:
        if h == prev + 1 and v == m:
            prev = h
            continue
        runs.append((start, prev, m))
        start, prev, m = h, h, v
    runs.append((start, prev, m))
    return "; ".join((f"{a}{dash}{b} ET {times}{v:g}" if a != b else f"{a} ET {times}{v:g}") for a, b, v in runs)


def _knob_lines(through_et: str, html: bool = False):
    """The live-config facts the email states, as (label, value) pairs.
    ASCII unless html -- see _hours_summary for why."""
    structural = sorted(imm.DAILY_SERIES_DYNAMIC)
    times, arrow = ("×", "→") if html else ("x", "->")
    return [
        ("Saturday multiplier", f"{times}{imm.SAT_SIZE_MULT:g} on Saturdays (ET), long-dated families only"),
        ("Quiet hours", f"{_hours_summary(dict(imm.HOUR_SIZE_MULTS), html)} (long-dated only)"),
        ("No multiplier", f"daily families: prefixes {', '.join(EXCL)} + {len(structural)} structural"
                          + (f" ({', '.join(structural)})" if structural else " (none yet)")),
        ("Window", f"ET days {SINCE} {arrow} {through_et}; baseline 2026-08-08 {arrow} 2026-09-11 (before the multiplier)"),
    ]


LEGEND = (
    ("Resting cts", "mean own resting contracts (cycle-log `quoted`; one cycle = 1/cycles-that-day of a day)"),
    ("Rent $/day", "modelled reward accrual, est_frac × pool $/day per cycle. Pre-realization: credits ran "
                   "~1.2× modelled account-wide since Aug 1, ~2× on the mention family"),
    ("Fills/day", "own maker fills from the fills sink, contracts"),
    ("Turnover", "fills ÷ resting: contracts filled per resting contract-day"),
    ("Rent/fill ¢", "rent per filled contract — the decision metric"),
    ("Mark-out 24h ¢", "24h mark-out per filled contract from cycle-log mids, all fills — the early read, "
                             "available the next day"),
    ("Settled %", "share of filled contracts whose market has settled"),
    ("Loss/fill ¢", "settlement loss per filled contract on the settled subset; fills in on later runs as "
                         "markets settle, so it only means something once Settled % is high"),
    ("Net/fill ¢", "Rent/fill − Loss/fill"),
    ("Net/ct-day ¢", "rent per resting contract-day − Turnover × Loss/fill: what a size multiplier "
                          "actually scales"),
    ("Mult", "mean cycle-log hour_mult on the group's quoted rows outside the 0–9 ET quiet window — proof "
             "the multiplier was live (Saturday long-dated 1.5, everything else 1.0)"),
)


# ---- plain text (console, task log, email text part) ------------------------

def render(g: pd.DataFrame, through_et: str):
    pd.set_option("display.width", 250)

    def table(df: pd.DataFrame) -> str:
        d = df.copy()
        for c, f in FMT.items():
            if c in d.columns:
                d[c] = d[c].map(lambda v: "-" if _isnan(v) else f.format(v))
        return d.to_string()

    lines = []
    lines.append(f"IMM Saturday multiplier tracker - through {through_et} (ET days since {SINCE})")
    for label, value in _knob_lines(through_et):
        lines.append(f"{label}: {value}")
    lines.append("cents per contract unless noted; rent = modelled accrual (pre-realization, ~1.2x paid account-wide, "
                 "~2x on mention); loss/fill = settlement loss on the settled subset; mo24 = 24h mark-out (all fills).")
    per_day, pooled_m, base = _tables(g)
    if per_day.empty:
        lines.append("\n(no cycle-log rows in the window yet)")
        return "\n".join(lines)
    per_day = per_day.copy()
    per_day["et_date"] = per_day["et_date"] + np.where(per_day["partial"], "*", "")
    for grp in GROUPS:
        lines.append(f"\n== per day: {grp.upper()} " + ("(the multiplier applies)" if grp == "long-dated" else "(daily families, no multiplier)") + " ==")
        d = per_day[per_day["group"] == grp].drop(columns=["group", "partial"]).set_index(["et_date", "day_type"])
        lines.append(table(d))
    lines.append("\n(* partial day: fewer than 20 hours logged; kept out of the pooled rows)")
    if not pooled_m.empty:
        lines.append(f"\n== pooled by day type since {SINCE} (full days only) ==")
        lines.append(table(pooled_m))
    lines.append("\n== baseline 2026-08-08..09-11 (before the multiplier; per-day means, same definitions) ==")
    lines.append(table(base))
    lines.append("\nread it as: long-dated Saturday rent/fill and net/ct-day vs the baseline row, with turnover the "
                 "reason either moved; loss/fill only means something once settled% is high, mo24 is the early read.")
    return "\n".join(lines)


# ---- HTML (the email body) --------------------------------------------------

def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(col: str, v, signed: bool = False) -> str:
    if _isnan(v):
        return "–"
    f = FMT.get(col, "{}")
    return (f.replace("{:", "{:+") if signed else f).format(v)


def _colour(col: str, v, text: str, delta: bool = False) -> str:
    """Green/red by sign on the columns where the sign has a meaning (a wider
    set in a delta row, see DELTA_GOOD_POS)."""
    good_pos = DELTA_GOOD_POS if delta else GOOD_POS
    if _isnan(v) or (col not in good_pos and col not in BAD_POS):
        return text
    good = v > 0.005 if col in good_pos else v < -0.005
    bad = v < -0.005 if col in good_pos else v > 0.005
    return f'<span style="color:{"#0a7a2f" if good else ("#c0392b" if bad else "#777")}">{text}</span>'


def _cell(col: str, v, style: str = _TD, signed: bool = False) -> str:
    """`signed` marks a delta cell: explicit +/- and the delta colour set."""
    return f'<td style="{style}">{_colour(col, v, _fmt(col, v, signed), delta=signed)}</td>'


def _html_table(head, rows) -> str:
    """head: [(label, left_aligned)], rows: [(row_style, [cell_html, ...])]."""
    h = ['<table style="border-collapse:collapse;margin:6px 0 14px">', "<tr>"]
    for label, left in head:
        h.append(f'<th style="{_THL if left else _TH}">{label}</th>')
    h.append("</tr>")
    for style, cells in rows:
        h.append(f'<tr style="{style}">' + "".join(cells) + "</tr>")
    h.append("</table>")
    return "".join(h)


def _headline_table(pooled_m: pd.DataFrame, base: pd.DataFrame, grp: str) -> str:
    """Pooled day-type rows since SINCE with the baseline row and a delta row
    directly under each -- the comparison the email exists to make."""
    head = [("Day type", True), ("Period", True), ("Days", False)] + [(LABELS[c], False) for c in METRICS]
    rows = []
    for dt in DAY_TYPES:
        now = pooled_m.loc[(grp, dt)] if (not pooled_m.empty and (grp, dt) in pooled_m.index) else None
        b = base.loc[(grp, dt)] if (grp, dt) in base.index else None
        bg = f"background:{_SAT_BG}" if dt == "Saturday" else ""
        if now is not None:
            cells = [f'<td style="{_TDL};font-weight:600">{dt}</td>', f'<td style="{_TDL}">since {SINCE}</td>',
                     _cell("days", float(now["days"]))]
            cells += [_cell(c, now[c]) for c in METRICS]
            rows.append((bg, cells))
        else:
            rows.append((bg, [f'<td style="{_TDL};font-weight:600">{dt}</td>',
                              f'<td style="{_TDL};color:{_MUTED}">since {SINCE}: no full day yet</td>',
                              f'<td style="{_TD}" colspan="{len(METRICS) + 1}"></td>']))
        if b is not None:
            cells = [f'<td style="{_TDL}"></td>', f'<td style="{_TDL};color:{_MUTED}">{BASELINE_LABEL}</td>',
                     f'<td style="{_TD};color:{_MUTED}">–</td>']
            cells += [_cell(c, b[c] if c in BASELINE_METRICS else float("nan"), style=f"{_TD};color:{_MUTED}")
                      for c in METRICS]
            rows.append((bg, cells))
        if now is not None and b is not None:
            cells = [f'<td style="{_TDL}"></td>', f'<td style="{_TDL}">Δ vs baseline</td>', f'<td style="{_TD}"></td>']
            for c in METRICS:
                if c in BASELINE_METRICS and not _isnan(now[c]) and not _isnan(b[c]):
                    cells.append(_cell(c, float(now[c]) - float(b[c]), signed=True))
                else:
                    cells.append(f'<td style="{_TD}"></td>')
            rows.append((bg + ";font-style:italic", cells))
    return _html_table(head, rows)


def _per_day_table(per_day: pd.DataFrame, grp: str) -> str:
    head = [("Date", True), ("Day", True)] + [(LABELS[c], False) for c in METRICS]
    rows = []
    d = per_day[per_day["group"] == grp].sort_values("et_date")
    for idx, r in d.iterrows():
        partial = bool(r["partial"])
        muted = f";color:{_MUTED}" if partial else ""
        style = (f"background:{_SAT_BG}" if r["day_type"] == "Saturday" else "") + muted
        cells = [f'<td style="{_TDL}">{r["et_date"]}{"*" if partial else ""}</td>',
                 f'<td style="{_TDL}">{r["day_type"]}</td>']
        cells += [_cell(c, r[c], style=_TD + muted) for c in METRICS]
        rows.append((style, cells))
    return _html_table(head, rows)


def render_html(g: pd.DataFrame, through_et: str) -> str:
    per_day, pooled_m, base = _tables(g)
    h = [f'<div style="{_FONT}">']
    h.append(f'<div style="font-size:17px;font-weight:600">IMM Saturday ×{imm.SAT_SIZE_MULT:g} tracker'
             f' <span style="color:{_MUTED};font-weight:400">— through {through_et}</span></div>')
    h.append('<table style="border-collapse:collapse;margin:8px 0 14px">')
    for label, value in _knob_lines(through_et, html=True):
        h.append(f'<tr><td style="{_TDL};color:{_MUTED}">{_esc(label)}</td>'
                 f'<td style="{_TDL};white-space:normal">{_esc(value)}</td></tr>')
    h.append("</table>")
    if per_day.empty:
        h.append(f'<div style="color:{_MUTED}">(no cycle-log rows in the window yet)</div></div>')
        return "".join(h)

    h.append('<div style="font-size:15px;font-weight:600;margin-top:6px">Long-dated families — the multiplier applies</div>')
    h.append(f'<div style="color:{_MUTED};margin:2px 0 4px">Pooled by day type over full days since {SINCE}, '
             f'with the pre-change baseline under each row. Read the Saturday row against its baseline: '
             f'Rent/fill and Net/ct-day are the verdict, Turnover is the reason either moved.</div>')
    h.append(_headline_table(pooled_m, base, "long-dated"))
    h.append('<div style="font-size:14px;font-weight:600">Per day — long-dated</div>')
    h.append(_per_day_table(per_day, "long-dated"))

    h.append('<div style="font-size:15px;font-weight:600;margin-top:10px">Excluded daily families — no multiplier (control)</div>')
    h.append(_headline_table(pooled_m, base, "excluded"))
    h.append('<div style="font-size:14px;font-weight:600">Per day — excluded</div>')
    h.append(_per_day_table(per_day, "excluded"))

    h.append(f'<div style="color:{_MUTED};margin:4px 0 10px">* partial day: fewer than 20 hours logged; kept out of the '
             f'pooled rows. Cents per contract unless noted. Loss/fill only means something once Settled % is high; '
             f'Mark-out 24h is the early read.</div>')
    h.append('<div style="font-size:14px;font-weight:600">Column legend</div>')
    h.append('<table style="border-collapse:collapse;margin:6px 0">')
    for label, desc in LEGEND:
        h.append(f'<tr><td style="{_TDL};font-weight:600">{_esc(label)}</td>'
                 f'<td style="{_TDL};white-space:normal;color:#444">{_esc(desc)}</td></tr>')
    h.append("</table></div>")
    return "".join(h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="build + print, no email, no marker")
    ap.add_argument("--dry", action="store_true", help="alias of --print")
    ap.add_argument("--test", action="store_true", help="email now, no sent marker")
    ap.add_argument("--through", default="", help="last ET date to include (default: yesterday ET)")
    ap.add_argument("--html-out", default="", help="also write the HTML email body to this path (with --print: a no-send preview)")
    args = ap.parse_args()
    now_et = datetime.now(timezone.utc).astimezone(ET)
    through_et = args.through or (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
    marker = os.path.join(STATUS_DIR, f"sat_tracker_sent_{now_et.strftime('%Y-%m-%d')}.marker")
    if not (args.print or args.dry or args.test) and os.path.exists(marker):
        log(f"[SAT] already sent today ({marker}); exiting")
        return 0
    first_file = (datetime.strptime(SINCE, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"[SAT] mirrored {len(_LAUNCHER_ENV)} launcher env vars; SAT_SIZE_MULT={imm.SAT_SIZE_MULT:g} exclude={EXCL}")
    ser, cyc, mid_tab = load_cycle_data(first_file)
    fills = load_fills(first_file)
    results = load_results()
    g = build_rows(ser, cyc, mid_tab, fills, results, through_et)
    text = render(g, through_et)
    html = render_html(g, through_et)
    print(text)
    if args.html_out:
        with open(args.html_out, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"[SAT] html written: {args.html_out}")
    if args.print or args.dry:
        return 0
    subject = f"IMM Saturday x{imm.SAT_SIZE_MULT:g} tracker - through {through_et}"
    ok = imm.Alerter("IMM-SAT", live=True).send_message(text, subject=subject, html=html)
    log(f"[SAT] email {'sent' if ok else 'FAILED'}: {subject}")
    if ok and not args.test:
        with open(marker, "w") as f:
            f.write(subject + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
