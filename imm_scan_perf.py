#!/usr/bin/env python3
"""
imm_scan_perf.py — the OFFLINE SCORER of the open-scan performance loop.

It re-derives, from the IMM sinks alone, how the open-scan tier actually
traded, and writes one small table (``scan_perf.json``) that the bot may
later read to demand EXTRA admission ROI from structurally bad candidates.

Labels, used in every printed and stored number (Jack, "a model is not a
measurement" — the "$1,500 non-IMM reward" was a reconstruction stated as
fact and was off 10x):

  MEASURED  re-derived from fills / settlements / marks / cycle_log / ledger
  MODELLED  the bot's est_frac x pool accrual, a mid-based mark, or a
            counterfactual replay

EST and CREDITED are NEVER summed anywhere (memory
``reference_imm_accrued_est_never_resets.md``: the accrual counter never
resets at a period end but the known_tickers prune DELETES it, so neither
quantity contains the other).

WHAT THIS EXISTS TO CATCH (all MEASURED on 2026-09-06..09-16, 147 tickers /
83 events / 71 series, Phase-1 ``empirical_scan_tier``):

  1. The tier lost -$237.77 of trading P&L on 10,434 $-days at risk while
     its own model said +$6.55, and 12 events are 89% of that loss. The
     admission ROI the walk ranks on is ANTI-predictive conditional on a
     fill (Spearman -0.43 vs trading P&L, -0.66 per quoted day, n=47).
  2. The losses are STRUCTURAL, not per-series: mid 30-70c (n=82, -150.7),
     spread 5-9c (n=31, -106.7) and 20c+ (n=38, -83.8), 30-90 days to close
     (n=38, -107.9), pools <$20/day (n=86, -153.2); while 1c-spread state
     prints (56% of the tier's risk-days) lost only -21.8.
  3. Per-event net quality does NOT persist at series level
     (``empirical_persistence`` 8.1: the unshrunk series mean is the WORST
     predictor in every cut), so the acting key is structure known at
     admission, and the series term is shrunk at m=20 fills.

WHAT IT MUST NEVER DO
  * write anything into STATUS_DIR other than its own three files (the live
    bot and four daily tasks own that directory);
  * call ``imm_reward_recon.rebuild_estimates()`` — that rewrites the live
    ``reward_est_cache.json`` unconditionally and would fight the 07:50
    ``KL imm program-history`` job. The accrual integral is re-implemented
    here against a PRIVATE cache;
  * publish a table that can RAISE an ROI. Every deviation is clipped at
    0.0 on the upside; the bot subtracts ``req >= 0`` and nothing else.

Usage
    python imm_scan_perf.py --dry
    python imm_scan_perf.py --now
    python imm_scan_perf.py --explain KXCPIYOY
    python imm_scan_perf.py --status-dir <dir> --work-dir <dir> --dry

Exit codes: 0 wrote (or --dry OK), 1 internal error, 2 reconciliation
failed (no file), 3 exposure-drop abort (no file), 4 clamp storm (no file).
"""

from __future__ import annotations

import argparse
import bisect
import csv
import glob
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- locations

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", os.path.join(_SCRIPT_DIR, "run-logs", "incentive-mm"))
WORK_DIR = STATUS_DIR

TABLE_NAME = "scan_perf.json"
CACHE_NAME = "scan_perf_cache.json"
HISTORY_NAME = "scan_perf_history.jsonl"
# The ONLY basenames this process may ever create. Everything else under
# STATUS_DIR belongs to the live bot or to one of the four daily tasks.
_ALLOWED_WRITE = {TABLE_NAME, CACHE_NAME, HISTORY_NAME}

# ---------------------------------------------------------------- parameters
# Scorer-side parameters live here and are echoed into params, so a re-score
# never needs a bot restart (SPEC 6.10).

WINDOW_DAYS = 45
# NO TIME DECAY IN v1. SPEC 3 defines no half-life weighting anywhere, and a
# decay invented here would silently change mu. An earlier draft carried a
# --halflife-days flag that was echoed into params and printed in the header
# but applied to nothing; it was REMOVED 2026-09-18 rather than left to
# mislead a reader into thinking the window is weighted. Every observation in
# the window has weight 1.
HORIZON_H = 24
MATURITY_SLACK_SECS = 900           # a fill enters the sample at t + H + 15min
MARK_TOLERANCE_SECS = 900           # same dt grain as imm_reward_recon
MAX_DT = 900.0                      # accrual integral cap (restarts cannot bill hours)
WINSOR_CENTS = 40.0                 # KXCPIYOY's 24h markout is -25.5 c/ct; 40 keeps it
COHORT_PRIOR_RISK_DAYS = 400.0      # K: thinnest real bucket (397.8 $-days) gets 50%
SERIES_PRIOR_FILLS = 20             # m
DIM_CLIP = 0.04
ADJ_CLIP = 0.04                     # equals DIM_CLIP by construction of min()
MAX_REQ = 0.10                      # effective admission bar <= 0.15/day
DATA_THIN_UNMARKED_FRAC = 0.35
BUCKET_MUTE_MIN_SERIES = 5
BUCKET_MUTE_MIN_EVENTS = 3
BAR_MIN_EPISODES = 30               # the ratified section-6 floor, in EPISODE units
BAR_MIN_MARKETS = 3
BAR_MIN_DAYS = 2
BAR_CI = 0.90
BAR_BOOTSTRAP = 2000
BAR_TTL_DAYS = 14
BAR_SUSTAIN_RUNS = 2
BAR_MIN_DWELL_DAYS = 7
RATCHET_EASE_PER_RUN = 0.01
RENT_FACTOR = 1.0
RENT_FACTOR_SOURCE = "default_n2"   # 2 MEASURED scan credits: no basis to haircut
PAYOUT_FLOOR = 1.00                 # MEASURED: min Liquidity credit is exactly $1.00
CREDIT_LAG_DAYS = 2                 # a period is measurable 2d before the newest credit
CREDIT_WINDOW_DAYS = 4              # ledger rows land 1-2d after a period end
MAX_BARRED_FILE = 40
MAX_PENALIZED = 25
MAX_BARRED = 5
MAX_RECORDS = 500
MAX_BAR_FRAC_UNIVERSE = 0.25
RECON_TOLERANCE_DOLLARS = 1.00
EXPOSURE_DROP_ABORT = 0.30
CLAMP_STORM_FRAC = 0.10
SCAN_MIN_ROI = 0.05                 # incentive_mm.SCAN_MIN_ROI (Jack 9/13, seat stays EMPTY)
BOOTSTRAP_SEED = 20260918           # deterministic: two runs in a minute must agree

QUOTE_GRID_SECS = 300               # cycle samples are kept on a 5-min grid

# ------------------------------------------------- PRE-REGISTERED, FROZEN
# SPEC 3.3. Selected after reading empirical_scan_tier section 4's loss table
# on n=147 markets over 12 days in which 12 events are 89% of the loss.
# These are hereby pre-registered and frozen: the scorer refuses to emit a
# table whose sha differs unless --refit-edges is passed, and the bot's
# loader refuses a re-fit table while IMM_SCAN_PERF_REQUIRE_FROZEN_EDGES=1.
#
# lo/hi convention: "closed":"both" -> lo <= v <= hi (integer cents);
#                   "closed":"left" -> lo <= v < hi.  hi = None means +inf.
COHORT_EDGES = {
    "spread": {
        "field": "spread_cents", "assign": "trailing_median_6h", "closed": "both",
        "buckets": [("1c", 0, 1), ("2-4c", 2, 4), ("5-9c", 5, 9),
                    ("10-19c", 10, 19), ("20c+", 20, None)],
    },
    "mid": {
        "field": "mid_cents", "assign": "trailing_median_6h", "closed": "left",
        "buckets": [("0-10", 0, 10), ("10-30", 10, 30), ("30-70", 30, 70),
                    ("70-90", 70, 90), ("90-100", 90, None)],
    },
    "dtc": {
        "field": "days_to_close", "assign": "admission_snapshot", "closed": "left",
        "buckets": [("0-7", 0, 7), ("7-30", 7, 30), ("30-90", 30, 90),
                    ("90-180", 90, 180), ("180+", 180, None)],
    },
    "pool": {
        "field": "dollars_per_day", "assign": "admission_snapshot", "closed": "left",
        "buckets": [("0-20", 0, 20), ("20-50", 20, 50), ("50-100", 50, 100),
                    ("100-200", 100, 200), ("200+", 200, None)],
    },
}
DIM_ORDER = ("spread", "mid", "dtc", "pool")
_DIM_CODE = {"spread": "sp", "mid": "m", "dtc": "dtc", "pool": "p"}


def edges_sha256(edges=None) -> str:
    """Canonical hash of the pre-registered bucket edges."""
    payload = json.dumps(edges if edges is not None else COHORT_EDGES,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Pinned by test_frozen_edges_hash_matches_the_constant (SPEC test 48).
FROZEN_EDGES_SHA256 = "cb9ed77fb1ba0cfdefde3b63c94b2b1296b46537cb6797938b3b2930436c5a85"

MON = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_TICKER_DATE_RE = re.compile(r"^[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)?")
_CYCLE_HDR = ("ts,ticker,ext_bid,ext_ask,yes_depth,no_depth,target,est_frac,qual_sides,"
              "acct_pos,own_pos,pool_per_day,quoted,own_bid_ct,own_ask_ct,own_bid_top,"
              "own_ask_top,own_pad_bid_ct,own_pad_ask_ct,want_bid_ct,want_ask_ct,"
              "want_pad_ct,hour_mult,rung_lo,room_buy,room_sell,vol24h,discount,is_scan,"
              "is_sticky,reduce_only,fast,run_id,config_hash")
_CYCLE_COLS = _CYCLE_HDR.split(",")
C_TS, C_TKR, C_BID, C_ASK = 0, 1, 2, 3
C_EST_FRAC, C_SIDES, C_OWN_POS, C_POOL = 7, 8, 10, 11
C_OWN_BID_CT, C_OWN_ASK_CT = 13, 14
C_IS_SCAN = 28


# ---------------------------------------------------------------- utilities

def ev_of(ticker: str) -> str:
    """LAST-RESORT event guess: the first two dash segments.

    It is WRONG for any family whose event ticker is not exactly two
    segments -- MEASURED over the last 20 selection_events files, 580 of
    1,455 distinct scan-candidate tickers (KXVOTEGENERAL 574, KXWEEKSNUM1 6)
    disagree with the authoritative `event_ticker` field, and
    reward_credits.csv has 18 event keys with >2 segments that this can
    never reach. Both the fills and the selection_events sinks carry
    `event_ticker`, so ScanPerfScorer.ev() prefers that map and falls back
    here only when a ticker appears in neither."""
    parts = ticker.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else ticker


def ser_of(ticker: str) -> str:
    return ticker.split("-")[0]


# VERBATIM the bot's incentive_mm.scan_perf_event_root rule. The scorer emits
# the `events` block and the bot looks records up in it, so the two must
# derive the key by the SAME rule or the scorer publishes a key the bot can
# never find: rsplit("-", 1) maps KXHYPEMINMON-HYPE-26JUL31 to
# KXHYPEMINMON-HYPE (agreeing) but KXVOTEGENERAL-HOUSECO3-26CBRO-7's guessed
# event to KXVOTEGENERAL (disagreeing). Pinned against the bot by a test.
_EVENT_ROOT_RE = re.compile(r"^(?P<root>.+)-\d{2}[A-Z]{3}")


def root_of(event_ticker: str) -> str:
    """EVENT ROOT = the event ticker minus its TRAILING DATE SEGMENT.

    KXAXP-26OCTCARDS -> KXAXP;  KXHYPEMINMON-HYPE-26JUL31 -> KXHYPEMINMON-HYPE;
    KXCPIYOY-26NOV -> KXCPIYOY. An UNDATED event (KXMLBPLAYOFFS) is its own
    root and is returned unchanged. Re-listed weeklies therefore share one
    root (SPEC section 7)."""
    s = str(event_ticker or "")
    m = _EVENT_ROOT_RE.match(s)
    return m.group("root") if m else s


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_of(rec_ts):
    """Sink timestamps are either an epoch float (fills) or an ISO string."""
    if rec_ts is None:
        return None
    if isinstance(rec_ts, (int, float)):
        return float(rec_ts)
    d = parse_iso(rec_ts)
    return d.timestamp() if d else None


def ticker_close_date(ticker: str):
    """Kalshi ticker date -> UTC datetime (the LISTING/close date convention
    used by Phase-1; memory project_imm_ticker_date_cutoff_bug is about the
    bot's cutoff, not about this descriptive field)."""
    m = _TICKER_DATE_RE.match(ticker)
    if not m:
        return None
    year = 2000 + int(m.group(1))
    mo = MON.get(m.group(2))
    if not mo:
        return None
    if m.group(3):
        try:
            return datetime(year, mo, int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    nxt = datetime(year + (mo == 12), (mo % 12) + 1, 1, tzinfo=timezone.utc)
    return nxt - timedelta(days=1)


def fnum(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def clip(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def safe_div(a, b, default=0.0):
    return (a / b) if b else default


# ---------------------------------------------------------- the write guard

class WriteGuardError(RuntimeError):
    pass


def check_write_path(path: str) -> str:
    """Every write in this module goes through here.

    The scorer is READ-ONLY on STATUS_DIR except for its own three files, and
    when --work-dir points elsewhere it must not touch STATUS_DIR at all.
    Pinned by test_scorer_is_read_only_on_status_dir (monkeypatches open)."""
    ap = os.path.abspath(path)
    base = os.path.basename(ap)
    if base.endswith(".tmp"):
        base = base[:-4]
    if base not in _ALLOWED_WRITE:
        raise WriteGuardError(f"refusing to write {ap}: not one of {sorted(_ALLOWED_WRITE)}")
    if os.path.dirname(ap) != os.path.abspath(WORK_DIR):
        raise WriteGuardError(f"refusing to write {ap}: outside work dir {WORK_DIR}")
    return ap


def atomic_write_json(path: str, obj) -> None:
    """tmp + os.replace, the imm_earnings_overrides._merge_series_file pattern."""
    ap = check_write_path(path)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    tmp = ap + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=False)
    os.replace(tmp, ap)


def append_jsonl(path: str, rec) -> None:
    ap = check_write_path(path)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    with open(ap, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


# ------------------------------------------------------------- sink reading

def _day_files(status_dir: str, prefix: str, ext: str, lo_date: str, hi_date: str):
    out = []
    for p in sorted(glob.glob(os.path.join(status_dir, f"{prefix}_*.{ext}"))):
        day = os.path.basename(p)[len(prefix) + 1:len(prefix) + 11]
        if len(day) == 10 and lo_date <= day <= hi_date:
            out.append((day, p))
    return out


def read_jsonl(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ------------------------------------------------------- cycle-log scanning

def _cycle_file_scan(path: str, extra_roster):
    """One cycle_log day-file -> per-ticker hourly aggregates + 5-min quotes.

    Reproduces imm_reward_recon._scan_cycle_log's accrual integral exactly:
    rows grouped by DISTINCT cycle timestamp, dt = min(gap, 900 s), the first
    cycle of each UTC day-file gets no dt (the per-file caching seam,
    <0.1%/day). Rows are kept when is_scan == 1, or when the row is a
    pre-9/6 13-column row (no is_scan column) whose ticker is in the roster.
    """
    out = {}
    n_rows = n_scan = n_pre = 0
    prev_ts = cur_ts = None
    batch = []

    def flush(ts, rows, dt):
        dd = dt / 86400.0
        hkey = str(int(ts // 3600))
        for (tkr, frac, sides, pool, pos, bid, ask, bid_ct, ask_ct, pre) in rows:
            d = out.get(tkr)
            if d is None:
                d = out[tkr] = {"h": {}, "q": [], "first": ts, "last": ts, "pre": 0}
            if ts < d["first"]:
                d["first"] = ts
            if ts > d["last"]:
                d["last"] = ts
            if pre:
                d["pre"] += 1
            h = d["h"].get(hkey)
            if h is None:
                # est, rest_dd, dt_secs, cycles, two_sided, {abs_pos: dt_secs}
                h = d["h"][hkey] = [0.0, 0.0, 0.0, 0, 0, {}]
            h[0] += frac * pool * dd
            rest = ((bid_ct * (bid if bid is not None else 0.0)) +
                    (ask_ct * (100.0 - (ask if ask is not None else 100.0)))) / 100.0
            h[1] += rest * dd
            h[2] += dt
            h[3] += 1
            if sides == 2:
                h[4] += 1
            pk = f"{abs(pos):.1f}"
            h[5][pk] = h[5].get(pk, 0.0) + dt

    # q is stored flat as [grid, ts, bid, ask] * n; the grid key lets the
    # writer overwrite the previous sample in the same 5-min bucket, which
    # keeps the private cache ~10x smaller than one entry per cycle while
    # staying well inside the 900 s mark tolerance.
    def sample2(ts, tkr, bid, ask):
        d = out.get(tkr)
        if d is None:
            return
        q = d["q"]
        g = int(ts // QUOTE_GRID_SECS)
        b = -1 if bid is None else int(bid)
        a = -1 if ask is None else int(ask)
        if q and q[-4] == g:
            q[-3], q[-2], q[-1] = int(ts), b, a
        else:
            q.extend([g, int(ts), b, a])

    def col(row, i, default=None):
        """13-column pre-9/6 rows simply do not have the later columns."""
        return row[i] if i < len(row) else default

    ts_cache = {}

    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        first = f.readline()
        use_csv = first.strip() != _CYCLE_HDR
        if use_csv:
            f.seek(0)
            reader = csv.reader(f)
            next(reader, None)
            lines = reader
        else:
            lines = f
        for raw in lines:
            row = raw if use_csv else raw.rstrip("\n").split(",")
            n_rows += 1
            if len(row) < 13 or row[C_TS] == "ts":
                continue
            tkr = row[C_TKR]
            pre = len(row) < 29
            if pre:
                if tkr not in extra_roster:
                    continue
                n_pre += 1
            elif row[C_IS_SCAN] != "1":
                continue
            else:
                n_scan += 1
            raw_ts = row[C_TS]
            ts = ts_cache.get(raw_ts)
            if ts is None:
                try:
                    ts = datetime.strptime(raw_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc).timestamp()
                except ValueError:
                    ts_cache[raw_ts] = False
                    continue
                ts_cache[raw_ts] = ts
            elif ts is False:
                continue
            frac = fnum(row[C_EST_FRAC], 0.0)
            pool = fnum(row[C_POOL], 0.0)
            sides = int(fnum(row[C_SIDES], 0.0) or 0)
            pos = fnum(col(row, C_OWN_POS), 0.0) or 0.0
            bid = fnum(row[C_BID])
            ask = fnum(row[C_ASK])
            bid_ct = fnum(col(row, C_OWN_BID_CT), 0.0) or 0.0
            ask_ct = fnum(col(row, C_OWN_ASK_CT), 0.0) or 0.0
            if cur_ts is None:
                cur_ts = ts
            elif ts != cur_ts:
                if prev_ts is not None:
                    dt = min(cur_ts - prev_ts, MAX_DT)
                    if dt > 0:
                        flush(cur_ts, batch, dt)
                prev_ts, cur_ts, batch = cur_ts, ts, []
            batch.append((tkr, frac, sides, pool, pos, bid, ask, bid_ct, ask_ct, pre))
            if tkr not in out:
                out[tkr] = {"h": {}, "q": [], "first": ts, "last": ts, "pre": 0}
            sample2(ts, tkr, bid, ask)
    if batch and prev_ts is not None:
        dt = min(cur_ts - prev_ts, MAX_DT)
        if dt > 0:
            flush(cur_ts, batch, dt)
    return out, {"rows": n_rows, "scan_rows": n_scan, "pre_rows": n_pre}


class CycleCache:
    """Per-file-signature cache, PRIVATE to this scorer.

    Only COMPLETE UTC day files are cached; today's partial day is recomputed
    from scratch on every run and never stored (the
    send_opportunistic_imm.durable_history convention — summing a partial day
    twice double-counts)."""

    def __init__(self, path: str, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self.data = load_json(path, {}) or {}
        if not isinstance(self.data, dict):
            self.data = {}
        self.dirty = False
        self.hits = 0
        self.misses = 0

    @staticmethod
    def sig(path: str) -> str:
        st = os.stat(path)
        return f"{st.st_size}:{int(st.st_mtime)}"

    def get(self, path: str, today_key: str, extra_roster):
        key = os.path.basename(path)
        complete = key[10:20] < today_key
        sig = self.sig(path)
        # A pre-column (13-col) file depends on the roster it was filtered
        # with, so its signature carries the roster hash too; every other file
        # is keyed on (path, size, mtime) alone and survives a roster change.
        ent = self.data.get(key)
        if complete and ent and ent.get("sig") == sig:
            if not ent.get("pre_any") or ent.get("roster_sig") == _roster_sig(extra_roster):
                self.hits += 1
                return ent["t"], ent.get("stats", {})
        self.misses += 1
        tickers, stats = _cycle_file_scan(path, extra_roster)
        if complete and self.enabled:
            self.data[key] = {"sig": sig, "t": tickers, "stats": stats,
                              "pre_any": stats.get("pre_rows", 0) > 0,
                              "roster_sig": _roster_sig(extra_roster)}
            self.dirty = True
        return tickers, stats

    def save(self):
        if self.enabled and self.dirty:
            atomic_write_json(self.path, self.data)


def _roster_sig(roster) -> str:
    return hashlib.sha1(",".join(sorted(roster)).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------- avg-cost P&L replay

class PnlReplay:
    """Independent avg-cost replay of fills + settlements.

    Phase-1 proved this reproduces the ``realized`` sink to the cent on all
    147 scan tickers (it is the same arithmetic as incentive_mm.PnlTracker
    plus _settle_or_drop's 0/100 booking). The ``realized`` sink is read ONLY
    as the comparison side of the section-4.4 self-check, never as a
    numerator — the sink writes per-process DELTAS and the process restarts
    ~20x/day."""

    def __init__(self):
        self.pos = {}
        self.avg = {}
        self.realized = {}
        self.filled = {}

    def on_fill(self, t, side, action, count, px):
        if side == "no":
            action = "sell" if action == "buy" else "buy"
        self.filled[t] = self.filled.get(t, 0.0) + abs(count)
        signed = count if action == "buy" else -count
        pos = self.pos.get(t, 0.0)
        avg = self.avg.get(t, 0.0)
        if pos * signed >= 0:
            new = pos + signed
            if abs(new) > 1e-9:
                self.avg[t] = (abs(pos) * avg + abs(signed) * px) / abs(new)
            self.pos[t] = new
            return 0.0
        closed = min(abs(signed), abs(pos))
        per = (px - avg) if pos > 0 else (avg - px)
        delta = closed * per / 100.0
        self.realized[t] = self.realized.get(t, 0.0) + delta
        new = pos + signed
        self.pos[t] = new
        if pos * new < 0:
            self.avg[t] = px
        elif abs(new) < 1e-9:
            self.avg[t] = 0.0
        return delta


# --------------------------------------------------------------- the scorer

class ScanPerfScorer:

    def __init__(self, status_dir, work_dir, *, asof=None, window_days=WINDOW_DAYS,
                 horizon_h=HORIZON_H,
                 score_basis="trading", refit_edges=False, use_cache=True,
                 bar_enabled=None, log=print):
        self.status_dir = status_dir
        self.work_dir = work_dir
        self.asof = asof or datetime.now(timezone.utc)
        self.window_days = window_days
        self.horizon_h = horizon_h
        self.score_basis = score_basis
        self.refit_edges = refit_edges
        self.use_cache = use_cache
        self.log = log
        self.edges = json.loads(json.dumps(COHORT_EDGES))
        if bar_enabled is None:
            bar_enabled = os.environ.get("IMM_SCAN_PERF_BAR", "0") == "1"
        self.bar_enabled = bool(bar_enabled)
        self.window_start = self.asof - timedelta(days=window_days)
        self.maturity_cutoff = self.asof - timedelta(
            seconds=horizon_h * 3600 + MATURITY_SLACK_SECS)
        self.warnings = []
        self.notes = []
        self.timings = {}

    # ------------------------------------------------------------- loading

    def _warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def ev(self, ticker: str) -> str:
        """The market's EVENT TICKER, from the sinks' own `event_ticker`
        field where either sink carried it, else ev_of's two-segment guess.

        The guess is wrong for every family whose event is not exactly two
        segments, and the `events` block the bot reads is keyed by
        root_of(event), so a wrong event here publishes a key the bot can
        never look up and misses that event's ledger rows (MEASURED: 580 of
        1,455 scan-candidate tickers over the last 20 selection_events
        files; 0 of them in today's scored roster, so this is latent, not
        observed)."""
        return self.event_of.get(ticker) or ev_of(ticker or "")

    def load(self):
        t0 = time.time()
        sd = self.status_dir
        lo = self.window_start.strftime("%Y-%m-%d")
        hi = self.asof.strftime("%Y-%m-%d")
        self.today_key = hi

        self.state = load_json(os.path.join(sd, "imm_state.json"), {}) or {}
        self.roster_file = load_json(os.path.join(sd, "opportunistic_roster.json"), {}) or {}
        self.programs = load_json(os.path.join(sd, "reward_programs.json"), {}) or {}
        self.series_meta = self.state.get("scan_series_meta") or {}

        # --- roster (SPEC 2.1): the union. is_scan on a CANDIDATE row is not
        # membership; only decision == "selected" AND is_scan is.
        self.admission = {}
        self.sel_rows = defaultdict(int)
        # ticker -> AUTHORITATIVE event ticker. Both sinks carry the field the
        # bot itself used; ev_of's two-segment guess is the fallback only.
        self.event_of = {}
        scan_book = set(self.state.get("scan_book") or [])
        roster_events = set(self.roster_file.get("scan_events") or [])
        selected = set()
        self.recent_scan_groups = set()
        recent_cut = (self.asof - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _day, p in _day_files(sd, "selection_events", "jsonl", lo, hi):
            for d in read_jsonl(p):
                if not d.get("is_scan"):
                    continue
                t = d.get("ticker")
                if not t:
                    continue
                _evt = d.get("event_ticker")
                if _evt:
                    self.event_of[t] = str(_evt)
                # any scan CANDIDATE in the last 24 h, so a bar whose group
                # has gone quiet shows up as an unmatched_bar_key instead of
                # silently mis-firing (SPEC 3.6).
                if str(d.get("ts") or "") >= recent_cut:
                    self.recent_scan_groups.add(ser_of(t))
                    self.recent_scan_groups.add(root_of(self.ev(t)))
                if d.get("decision") != "selected":
                    continue
                selected.add(t)
                self.sel_rows[t] += 1
                cur = self.admission.get(t)
                if cur is None or str(d.get("ts") or "") < str(cur.get("ts") or ""):
                    self.admission[t] = d
        self.prelim_roster = set(scan_book) | selected
        self.roster_events_file = roster_events

        # --- cycle logs
        cache_path = os.path.join(self.work_dir, CACHE_NAME)
        self.cache = CycleCache(cache_path, enabled=self.use_cache)
        per_ticker_h = defaultdict(dict)
        quotes = defaultdict(list)
        self.pre_column_files = []
        files = _day_files(sd, "cycle_log", "csv", lo, hi)
        self.n_cycle_files = len(files)
        for _day, p in files:
            tickers, stats = self.cache.get(p, self.today_key, self.prelim_roster)
            if stats.get("pre_rows"):
                self.pre_column_files.append(os.path.basename(p))
            for t, d in tickers.items():
                dst = per_ticker_h[t]
                for hk, h in d["h"].items():
                    cur = dst.get(hk)
                    if cur is None:
                        dst[hk] = [h[0], h[1], h[2], h[3], h[4], dict(h[5])]
                    else:
                        cur[0] += h[0]
                        cur[1] += h[1]
                        cur[2] += h[2]
                        cur[3] += h[3]
                        cur[4] += h[4]
                        for k, v in h[5].items():
                            cur[5][k] = cur[5].get(k, 0.0) + v
                if d["q"]:
                    quotes[t].append(d["q"])
        self.cycle_hours = per_ticker_h
        self.quotes = {}
        asof_cut = self.asof.timestamp()
        for t, chunks in quotes.items():
            samples = []
            for q in chunks:
                for i in range(0, len(q), 4):
                    if q[i + 1] > asof_cut:
                        continue
                    samples.append((q[i + 1], q[i + 2], q[i + 3]))
            samples.sort()
            self.quotes[t] = (
                [s[0] for s in samples],
                [None if s[1] < 0 else float(s[1]) for s in samples],
                [None if s[2] < 0 else float(s[2]) for s in samples],
            )
        for fn in self.pre_column_files:
            self._warn(f"{fn} contains 13-column rows (pre-is_scan); attributed by roster")

        self.roster = set(self.prelim_roster) | set(self.cycle_hours)

        # --- fills (deduped by fill_id), marks, settlements, realized sink
        # AS-OF DISCIPLINE: a day file may hold records written after the
        # --asof instant, so every stream is cut at the asof TIMESTAMP, not
        # just at the asof day. Without this a backward re-score marks a
        # position with a price from the future.
        asof_ts = self.asof.timestamp()
        self.fills = []
        seen_fill = set()
        fill_files = _day_files(sd, "fills", "jsonl", "0000-00-00", hi)
        for _day, p in fill_files:
            for d in read_jsonl(p):
                fid = d.get("fill_id")
                if fid in seen_fill:
                    continue
                ts = ts_of(d.get("ts"))
                if ts is None or ts > asof_ts:
                    continue
                seen_fill.add(fid)
                t = d.get("ticker")
                _evt = d.get("event_ticker")
                if _evt:
                    self.event_of.setdefault(t, str(_evt))
                if t in self.roster or self.ev(t or "") in roster_events:
                    self.fills.append(d)
        self.fills.sort(key=lambda d: ts_of(d.get("ts")) or 0.0)
        self.roster |= {d["ticker"] for d in self.fills}

        self.settlements = []
        seen_settle = set()
        for _day, p in _day_files(sd, "settlements", "jsonl", "0000-00-00", hi):
            for d in read_jsonl(p):
                t = d.get("ticker")
                if t not in self.roster:
                    continue
                sts = ts_of(d.get("ts"))
                if sts is None or sts > asof_ts:
                    continue
                key = (t, str(d.get("ts")))
                if key in seen_settle:
                    continue
                seen_settle.add(key)
                self.settlements.append(d)
        self.settlements.sort(key=lambda d: ts_of(d.get("ts")) or 0.0)
        self.settle_by_ticker = defaultdict(list)
        for d in self.settlements:
            self.settle_by_ticker[d["ticker"]].append(d)

        self.marks = defaultdict(list)
        self.last_mark = {}
        for _day, p in _day_files(sd, "marks", "jsonl", lo, hi):
            for d in read_jsonl(p):
                t = d.get("ticker")
                if t not in self.roster:
                    continue
                ts = ts_of(d.get("ts"))
                if ts is None or ts > asof_ts:
                    continue
                self.marks[t].append((ts, fnum(d.get("mark_cents")), fnum(d.get("avg_cents"))))
                self.last_mark[t] = d
        for t in self.marks:
            self.marks[t].sort()
        self.mark_ts = {t: [x[0] for x in v] for t, v in self.marks.items()}

        # realized sink -> per (ticker, UTC day) delta sums, COMPLETE days only
        self.sink_realized_by_day = defaultdict(float)
        for day, p in _day_files(sd, "realized", "jsonl", "0000-00-00", hi):
            for d in read_jsonl(p):
                if d.get("ticker") in self.roster:
                    self.sink_realized_by_day[(d["ticker"], day)] += fnum(
                        d.get("realized_delta_dollars"), 0.0) or 0.0

        # --- ledger
        self.credits = defaultdict(list)
        self.newest_credit_date = None
        lp = os.path.join(sd, "reward_credits.csv")
        if os.path.exists(lp):
            try:
                with open(lp, newline="", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        e = r.get("event_ticker")
                        d = r.get("credit_date")
                        amt = fnum(r.get("amount"), 0.0) or 0.0
                        self.credits[e].append((d, amt, r.get("kind")))
                        if d and (self.newest_credit_date is None or d > self.newest_credit_date):
                            self.newest_credit_date = d
            except OSError:
                pass
        if self.newest_credit_date:
            stale = (self.asof.date() - datetime.strptime(
                self.newest_credit_date, "%Y-%m-%d").date()).days
            self.ledger_stale_days = stale
            if stale > 3:
                self._warn(f"credit ledger {stale} days stale "
                           f"(newest {self.newest_credit_date}); rent is MODELLED")
        else:
            self.ledger_stale_days = None
            self._warn("no reward_credits.csv found; all rent is MODELLED")

        self.timings["load_secs"] = round(time.time() - t0, 2)
        if not self.roster:
            self._warn("window contains no open-scan activity (sinks may start after "
                       "the window start); the table is empty and every lookup is neutral")

    # -------------------------------------------------------- mark hierarchy

    def _cycle_near(self, ticker, t, two_sided):
        """Nearest cycle sample within MARK_TOLERANCE_SECS.

        two_sided=True  -> both sides present and ask > bid
        two_sided=False -> exactly one side present"""
        q = self.quotes.get(ticker)
        if not q:
            return None
        ts, bids, asks = q
        # The WHOLE +/-MARK_TOLERANCE_SECS window, not the three bisect
        # neighbours: on the 300s quote grid a +/-900s window spans up to
        # seven samples, and the predicate here is CONDITIONAL (both sides
        # present and ask > bid), so a qualifying sample can sit inside the
        # tolerance while the nearest neighbours do not qualify. The
        # nearest-3 shortcut is only correct for an unconditional predicate;
        # with one, it silently demoted the fill to a lower mark tier.
        # (MEASURED 2026-09-18: 0 of 128 matured fills were affected on
        # today's data -- latent, not observed. Bounded by the tolerance, so
        # this stays O(7).)
        best = None
        for j in range(bisect.bisect_left(ts, t - MARK_TOLERANCE_SECS),
                       bisect.bisect_right(ts, t + MARK_TOLERANCE_SECS)):
            dt = abs(ts[j] - t)
            if dt > MARK_TOLERANCE_SECS:
                continue
            b, a = bids[j], asks[j]
            ok = (b is not None and a is not None and a > b) if two_sided else \
                 ((b is None) != (a is None))
            if not ok:
                continue
            if best is None or dt < best[0]:
                best = (dt, b, a)
        return best

    def _mark_near_sink(self, ticker, t):
        arr = self.mark_ts.get(ticker)
        if not arr:
            return None
        # Same +/-tolerance sweep as _cycle_near: the predicate (mark_cents
        # is not None) is conditional, so the nearest three are not enough.
        best = None
        for j in range(bisect.bisect_left(arr, t - MARK_TOLERANCE_SECS),
                       bisect.bisect_right(arr, t + MARK_TOLERANCE_SECS)):
            dt = abs(arr[j] - t)
            if dt > MARK_TOLERANCE_SECS:
                continue
            mc = self.marks[ticker][j][1]
            if mc is None:
                continue
            if best is None or dt < best[0]:
                best = (dt, mc)
        return best[1] if best else None

    def _median_spread(self, ticker, t):
        """Trailing-24h MEDIAN two-sided spread for that ticker; never the
        instantaneous one (SPEC 2.4 tier 3). Falls back series -> tier -> 4c."""
        q = self.quotes.get(ticker)
        vals = []
        if q:
            ts, bids, asks = q
            lo = bisect.bisect_left(ts, t - 86400)
            hi = bisect.bisect_right(ts, t)
            for j in range(lo, hi):
                b, a = bids[j], asks[j]
                if b is not None and a is not None and a > b:
                    vals.append(a - b)
        if vals:
            return statistics.median(vals)
        s = ser_of(ticker)
        if self._series_spread.get(s):
            return self._series_spread[s]
        return self._tier_spread or 4.0

    def _mark(self, ticker, t):
        """M_H(ticker, t). Returns (value_cents, source) or (None, 'unmarked').
        Four tiers, NO fifth carry-forward tier (SPEC 2.4 / judge must_fix 7).

        SOURCE is the SUB-source, not the SPEC's three-key bucket. Tier 2 has
        two sinks -- the live two-sided cycle book and the marks_*.jsonl
        fallback -- and reporting the fallback as `two_sided` blinded the one
        detector SPEC 2.4 added "so a drift toward one-sided/settlement marks
        is visible BEFORE it changes a verdict". MEASURED 2026-09-18 on the
        live window: cycle_two_sided 87 fills / 2,169 ct (67.97%), sink_mark
        29 fills / 719 ct (22.66%), settlement 12 fills / 382 ct (9.38%) --
        i.e. 22% of the acting statistic's contracts rode the sink channel
        under a label that said otherwise. The two are NOT equivalent: the
        sink value is `state.last_mark`, which incentive_mm fills from a bulk
        get_markets mid, falls back to `last_price` (a trade print, not a
        book mid) when bid/ask are missing, and on an API failure leaves the
        previous value in place ("stale marks stand"). `mark_source_mix`
        keeps the SPEC's three keys by folding sink_mark into two_sided;
        `mark_source_detail` publishes the split."""
        for d in self.settle_by_ticker.get(ticker, ()):
            sts = ts_of(d.get("ts"))
            if sts is not None and sts <= t and d.get("result") in ("yes", "no"):
                px = fnum(d.get("settle_price_cents"))
                if px is not None:
                    return px, "settlement"
        two = self._cycle_near(ticker, t, True)
        if two:
            return (two[1] + two[2]) / 2.0, "cycle_two_sided"
        mc = self._mark_near_sink(ticker, t)
        if mc is not None:
            return mc, "sink_mark"
        one = self._cycle_near(ticker, t, False)
        if one:
            s = self._median_spread(ticker, t)
            b, a = one[1], one[2]
            if b is not None:
                return b + s / 2.0, "one_sided"
            return a - s / 2.0, "one_sided"
        return None, "unmarked"

    # ------------------------------------------------------------ the build

    def build(self):
        t0 = time.time()
        self._prep_spreads()
        self._build_markets()
        self._build_markouts()
        self._build_rent()
        self._build_cohorts()
        self._build_units()
        self.timings["build_secs"] = round(time.time() - t0, 2)

    def _prep_spreads(self):
        """Trailing-6h median spread / mid per market (the FITTING side).

        A book that flickers between 4c and 5c must not move a market between
        buckets on one selection_events row (judge must_fix J2-4)."""
        self._series_spread = {}
        self._tier_spread = None
        per_series = defaultdict(list)
        all_sp = []
        self.fit_spread = {}
        self.fit_mid = {}
        for t, q in self.quotes.items():
            ts, bids, asks = q
            if not ts:
                continue
            t0 = ts[0]
            sp, mid = [], []
            for j, tt in enumerate(ts):
                if tt > t0 + 6 * 3600:
                    break
                b, a = bids[j], asks[j]
                if b is not None and a is not None and a > b:
                    sp.append(a - b)
                    mid.append((a + b) / 2.0)
            if sp:
                self.fit_spread[t] = statistics.median(sp)
                self.fit_mid[t] = statistics.median(mid)
                per_series[ser_of(t)].extend(sp)
                all_sp.extend(sp)
        for s, v in per_series.items():
            self._series_spread[s] = statistics.median(v)
        self._tier_spread = statistics.median(all_sp) if all_sp else 4.0

    def _build_markets(self):
        """Per-market exposure ($-days), position and MTM."""
        w_lo = self.window_start.timestamp()
        w_hi = self.asof.timestamp()

        # own fill contracts inside W -> the ADOPTED-POSITION CAP.
        self.own_fill_ct = defaultdict(float)
        for d in self.fills:
            ts = ts_of(d.get("ts"))
            if ts is None or ts < w_lo or ts > w_hi:
                continue
            self.own_fill_ct[d["ticker"]] += abs(fnum(d.get("count"), 0.0) or 0.0)

        self.markets = {}
        self.adopted_capped = []
        for t, hours in self.cycle_hours.items():
            cap = self.own_fill_ct.get(t, 0.0)
            rest_dd = inv_dd = inv_dd50 = 0.0
            est_raw_by_hour = {}
            cycles = two_sided = 0
            first_ts = last_ts = None
            capped = False
            for hk, h in sorted(hours.items(), key=lambda kv: int(kv[0])):
                h_start = int(hk) * 3600
                if h_start + 3600 < w_lo or h_start > w_hi:
                    continue
                inv_px = self._inv_px(t, h_start + 1800)
                rest_dd += h[1]
                cycles += h[3]
                two_sided += h[4]
                for pk, dt in h[5].items():
                    p = float(pk)
                    if p <= 0:
                        continue
                    used = min(p, cap)
                    if used < p - 1e-9:
                        capped = True
                    dd = dt / 86400.0
                    inv_dd += used * inv_px / 100.0 * dd
                    inv_dd50 += used * 0.5 * dd
                est_raw_by_hour[h_start] = h[0]
                if first_ts is None:
                    first_ts = h_start
                last_ts = h_start
            if capped:
                self.adopted_capped.append(t)
            self.markets[t] = {
                "risk_days": rest_dd + inv_dd,
                "risk_days_at50c": rest_dd + inv_dd50,
                "rest_days": rest_dd,
                "inv_days": inv_dd,
                "est_by_hour": est_raw_by_hour,
                "cycles": cycles,
                "two_sided_cycles": two_sided,
                "first_cycle": first_ts,
                "last_cycle": last_ts,
                "adopted_capped": capped,
            }
        # markets in the roster with no cycle rows at all (selected but never
        # placed a quote: 4 of 147 in Phase-1) still get a record.
        for t in self.roster:
            self.markets.setdefault(t, {
                "risk_days": 0.0, "risk_days_at50c": 0.0, "rest_days": 0.0,
                "inv_days": 0.0, "est_by_hour": {}, "cycles": 0,
                "two_sided_cycles": 0, "first_cycle": None, "last_cycle": None,
                "adopted_capped": False})
        self.adopted_capped.sort()

        # --- independent avg-cost replay (fills + settlements), per UTC day
        # SEEDING. Two positions predate the fills sink (KXCAFAIRPLAN,
        # KXMARCELLUSGAS on 2026-09-06 before 13:35Z) and at least one was
        # ADOPTED from the account with no bot fill at all
        # (KXAALA-27JANPLF-83, +30 @ 85.67). Phase-1 seeded from
        # imm_state.own_pos for tickers with NO fills; that rule breaks the
        # cent-exact check the moment such a ticker finally trades (KXAALA
        # filled once after 9/16 and the sink booked -$6.33 against a basis
        # the replay did not have). So the primary seed is the FIRST fill's
        # own pos_before/avg_before — the same basis the bot's tracker used —
        # and imm_state is only the fallback for tickers that never filled.
        replay = PnlReplay()
        seeded = set()
        first_fill = {}
        for d in self.fills:
            t = d["ticker"]
            if t not in first_fill:
                first_fill[t] = d
        for t, d in first_fill.items():
            pb = fnum(d.get("pos_before"), 0.0) or 0.0
            if abs(pb) > 1e-9:
                replay.pos[t] = pb
                replay.avg[t] = fnum(d.get("avg_before"), 0.0) or 0.0
                seeded.add(t)
        own_pos = self.state.get("own_pos") or {}
        own_avg = self.state.get("own_avg") or {}
        for t in sorted(self.roster):
            sp = fnum(own_pos.get(t), 0.0) or 0.0
            if abs(sp) > 1e-9 and t not in first_fill:
                replay.pos[t] = sp
                replay.avg[t] = fnum(own_avg.get(t), 0.0) or 0.0
                seeded.add(t)
        self.seeded_tickers = sorted(seeded)
        stream = ([("f", ts_of(d.get("ts")) or 0.0, d) for d in self.fills] +
                  [("s", ts_of(d.get("ts")) or 0.0, d) for d in self.settlements])
        stream.sort(key=lambda x: x[1])
        self.replay_realized_by_day = defaultdict(float)
        self.realized_fills = defaultdict(float)
        self.realized_settle = defaultdict(float)
        for kind, ts, d in stream:
            t = d["ticker"]
            day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            if kind == "f":
                delta = replay.on_fill(t, d.get("side"), d.get("action"),
                                       fnum(d.get("count"), 0.0) or 0.0,
                                       fnum(d.get("yes_price_cents"), 0.0) or 0.0)
                self.realized_fills[t] += delta
            else:
                if d.get("result") not in ("yes", "no"):
                    continue
                pos = replay.pos.get(t, 0.0)
                px = fnum(d.get("settle_price_cents"), 0.0) or 0.0
                delta = replay.on_fill(t, "yes", "sell" if pos > 0 else "buy",
                                       abs(pos), px)
                self.realized_settle[t] += delta
            self.replay_realized_by_day[(t, day)] += delta
        self.replay = replay

        for t, m in self.markets.items():
            pos = replay.pos.get(t, 0.0)
            avg = replay.avg.get(t, 0.0)
            mk = self.last_mark.get(t)
            mark = fnum((mk or {}).get("mark_cents"))
            if mark is None:
                q = self.quotes.get(t)
                if q and q[0]:
                    b, a = q[1][-1], q[2][-1]
                    if b is not None and a is not None:
                        mark = (a + b) / 2.0
            m["pos"] = pos
            m["avg_cents"] = avg
            m["mark_cents"] = mark
            m["realized_dollars"] = replay.realized.get(t, 0.0)
            m["mtm_dollars"] = (pos * (mark - avg) / 100.0
                                if (abs(pos) > 1e-9 and mark is not None) else 0.0)

    def _inv_px(self, ticker, t):
        """Inventory price for the $-day integral: the nearest marks_*.jsonl
        avg_cents, else 50c (the flat convention the Phase-1 10,434 baseline
        used, which is why risk_days_at50c is emitted alongside)."""
        arr = self.mark_ts.get(ticker)
        if not arr:
            return 50.0
        i = bisect.bisect_left(arr, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(arr):
                a = self.marks[ticker][j][2]
                if a is None:
                    continue
                d = abs(arr[j] - t)
                if best is None or d < best[0]:
                    best = (d, a)
        return best[1] if best and best[1] else 50.0

    def _build_markouts(self):
        """Episodes, signed direction and the 24h markout (MEASURED)."""
        w_lo = self.window_start.timestamp()
        w_hi = self.asof.timestamp()
        mat = self.maturity_cutoff.timestamp()

        self.fill_rows = []
        eps = defaultdict(lambda: {"num": 0.0, "ct": 0.0, "fills": 0})
        src_mix = defaultdict(int)
        src_ct = defaultdict(float)
        self.market_mo = defaultdict(float)
        self.market_ct_matured = defaultdict(float)
        self.market_unmarked = defaultdict(int)
        self.market_flat = defaultdict(int)
        self.market_fills = defaultdict(int)
        self.market_fills_matured = defaultdict(int)
        self.market_pending = defaultdict(int)
        self.market_episodes = defaultdict(set)
        self.market_days = defaultdict(set)
        all_hours = set()
        n_fills = n_matured = n_pending = n_unmarked = n_flat = 0
        contracts = contracts_mat = 0.0

        for d in self.fills:
            ts = ts_of(d.get("ts"))
            t = d.get("ticker")
            if ts is None or ts < w_lo or ts > w_hi:
                continue
            q = (fnum(d.get("pos_after"), 0.0) or 0.0) - (fnum(d.get("pos_before"), 0.0) or 0.0)
            if abs(q) < 1e-9:
                # a flat-through fill carries no direction; count it, score it not
                q = 0.0
            direction = 0.0 if q == 0 else (1.0 if q > 0 else -1.0)
            px = fnum(d.get("yes_price_cents"), 0.0) or 0.0
            n_fills += 1
            self.market_fills[t] += 1
            contracts += abs(q)
            self.market_days[t].add(
                datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"))
            row = {"ticker": t, "ts": ts, "q": q, "d": direction, "px": px,
                   "fill_id": d.get("fill_id"), "count": fnum(d.get("count"), 0.0)}
            all_hours.add((t, int(ts // 3600)))
            if ts + self.horizon_h * 3600 + MATURITY_SLACK_SECS > self.asof.timestamp():
                n_pending += 1
                self.market_pending[t] += 1
                row.update(status="pending", mark=None, source="pending", mo=None)
                self.fill_rows.append(row)
                continue
            n_matured += 1
            self.market_fills_matured[t] += 1
            if direction == 0.0:
                # A FLAT-THROUGH fill (pos_after == pos_before) is not an
                # unmarkable fill -- it carries no direction, so there is
                # nothing to mark. Booking it as `unmarked` put the one thing
                # that CAN raise the ratio into a denominator that names
                # something else, and `unmarked/n_fills` is the input to the
                # data_thin rule and to bar condition 4.
                n_flat += 1
                self.market_flat[t] += 1
                row.update(status="flat_through", mark=None,
                           source="flat_through", mo=None)
                self.fill_rows.append(row)
                continue
            mark, source = self._mark(t, ts + self.horizon_h * 3600)
            if mark is None:
                n_unmarked += 1
                self.market_unmarked[t] += 1
                row.update(status="unmarked", mark=None, source="unmarked", mo=None)
                self.fill_rows.append(row)
                continue
            src_mix[source] += 1
            src_ct[source] += abs(q)
            mo = direction * (mark - px)
            row.update(status="marked", mark=mark, source=source, mo=mo)
            self.fill_rows.append(row)
            hour = int(ts // 3600)
            e = eps[(t, hour)]
            e["num"] += abs(q) * mo
            e["ct"] += abs(q)
            e["fills"] += 1
            contracts_mat += abs(q)

        self.episodes = {}
        for (t, hour), e in eps.items():
            if e["ct"] <= 0:
                continue
            mo_ep = clip(e["num"] / e["ct"], -WINSOR_CENTS, WINSOR_CENTS)
            self.episodes[(t, hour)] = {"mo": mo_ep, "ct": e["ct"], "fills": e["fills"]}
            self.market_mo[t] += e["ct"] * mo_ep / 100.0
            self.market_ct_matured[t] += e["ct"]
            self.market_episodes[t].add(hour)

        tot = sum(src_mix.values()) or 1
        # The SPEC's three keys (sink_mark folds into two_sided: same tier,
        # different sink) ...
        self.mark_source_mix = {
            "two_sided": round((src_mix.get("cycle_two_sided", 0)
                                + src_mix.get("sink_mark", 0)) / tot, 4),
            "one_sided": round(src_mix.get("one_sided", 0) / tot, 4),
            "settlement": round(src_mix.get("settlement", 0) / tot, 4),
        }
        # ... and the SUB-source split, which is the honest freshness
        # detector: a drift from the live cycle book toward `state.last_mark`
        # is invisible in the three-key mix because both report as two_sided.
        tot_ct = sum(src_ct.values()) or 1.0
        self.mark_source_detail = {}
        for k in ("cycle_two_sided", "sink_mark", "one_sided", "settlement"):
            self.mark_source_detail[k] = round(src_mix.get(k, 0) / tot, 4)
            self.mark_source_detail[k + "_fills"] = src_mix.get(k, 0)
            self.mark_source_detail[k + "_contract_frac"] = round(
                src_ct.get(k, 0.0) / tot_ct, 4)
        self.cov = {
            "fills": n_fills, "fills_matured": n_matured, "fills_pending": n_pending,
            "fills_unmarked": n_unmarked, "fills_flat_through": n_flat,
            # episodes = EVERY fill collapsed to (ticker, UTC hour);
            # episodes_matured = the marked-and-matured subset. Emitting
            # len(self.episodes) for both made the two identical by
            # construction, so the file could not express SPEC 2.2's
            # "106 fills -> 85 episodes (63 matured)" shape at all.
            "episodes": len(all_hours),
            "episodes_matured": len(self.episodes),
            "contracts_filled": round(contracts, 2),
            "contracts_matured": round(contracts_mat, 2),
        }
        self.markout_horizons = {
            h: self._markout_at(h, w_lo, w_hi) for h in (1, self.horizon_h, 72)}

    def _markout_at(self, h, w_lo, w_hi):
        """The SAME statistic as the acting 24h markout, at horizon `h`.

        Its own maturity cutoff (a fill enters at t + h + slack, NOT at
        t + 24h + slack), its own (ticker, hour) episode collapse and its own
        +/-WINSOR_CENTS clip. The first cut computed the 1h and 72h figures
        inside the 24h loop, so both were conditioned on the fill having
        already matured AND been markable at 24h, and neither was winsorised
        or episode-collapsed -- three different statistics printed on one
        line labelled MEASURED, and not comparable with the SPEC 10.3
        pre-registered baselines (1h -5.75 on n=105, 72h -2.95 on n=49) that
        they exist to be compared with."""
        eps = defaultdict(lambda: [0.0, 0.0])
        n_fills = 0
        for d in self.fills:
            ts = ts_of(d.get("ts"))
            if ts is None or ts < w_lo or ts > w_hi:
                continue
            if ts + h * 3600 + MATURITY_SLACK_SECS > w_hi:
                continue
            q = ((fnum(d.get("pos_after"), 0.0) or 0.0)
                 - (fnum(d.get("pos_before"), 0.0) or 0.0))
            if abs(q) < 1e-9:
                continue
            t = d.get("ticker")
            mark, _src = self._mark(t, ts + h * 3600)
            if mark is None:
                continue
            px = fnum(d.get("yes_price_cents"), 0.0) or 0.0
            direction = 1.0 if q > 0 else -1.0
            n_fills += 1
            e = eps[(t, int(ts // 3600))]
            e[0] += abs(q) * direction * (mark - px)
            e[1] += abs(q)
        mo = ct = 0.0
        for num, c in eps.values():
            if c <= 0:
                continue
            mo += c * clip(num / c, -WINSOR_CENTS, WINSOR_CENTS) / 100.0
            ct += c
        return {"horizon_h": h, "c_per_ct": round(safe_div(100.0 * mo, ct), 3),
                "mo_dollars": round(mo, 4), "n_fills": n_fills,
                "n_contracts": round(ct, 2), "n_episodes": len(eps)}

    # ------------------------------------------------------------ rent side

    def _periods_for(self, ticker):
        """Program period boundaries for a market.

        reward_programs.json COLLAPSES periods per market (min start / max end
        / paid OR'd, imm_reward_recon 335-343) and cannot answer 'which period
        ENDED', so its start/end are used as the only two boundaries we know,
        and everything else falls back to 7-day buckets anchored to the
        market's first scan cycle."""
        pr = self.programs.get(ticker) or {}
        start = parse_iso(pr.get("start"))
        end = parse_iso(pr.get("end"))
        cuts = sorted({d.timestamp() for d in (start, end) if d})
        m = self.markets.get(ticker) or {}
        first = m.get("first_cycle")
        if not cuts:
            if first is None:
                return []
            out = []
            t = float(first)
            stop = self.asof.timestamp() + 7 * 86400
            while t < stop:
                out.append((t, t + 7 * 86400))
                t += 7 * 86400
            return out
        bounds = [-math.inf] + cuts + [math.inf]
        return list(zip(bounds[:-1], bounds[1:]))

    def _build_rent(self):
        """MODELLED rent: the cycle-log accrual integral, then the MEASURED
        $1.00-per-market-per-program-period floor."""
        self.market_rent_periods = {}
        self.market_rent_raw = defaultdict(float)
        self.market_rent_floored = defaultdict(float)
        for t, m in self.markets.items():
            by_hour = m.get("est_by_hour") or {}
            raw_total = sum(by_hour.values())
            self.market_rent_raw[t] = raw_total
            periods = self._periods_for(t)
            if not periods:
                periods = [(-math.inf, math.inf)]
            acc = []
            for lo, hi in periods:
                v = sum(x for h, x in by_hour.items() if lo <= h < hi)
                acc.append({"lo": lo, "hi": hi, "raw": v,
                            "floored": v if v >= PAYOUT_FLOOR else 0.0})
            self.market_rent_periods[t] = acc
            self.market_rent_floored[t] = sum(p["floored"] for p in acc)

        # Rent basis: for a (market, period) whose period ended >= 2 days
        # before the newest credit_date and whose EVENT has ledger rows, the
        # MEASURED credit REPLACES the estimate for THAT period. Never summed.
        newest = (datetime.strptime(self.newest_credit_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc) if self.newest_credit_date else None)
        self.event_rent = defaultdict(float)
        self.event_rent_measured = defaultdict(float)
        self.event_credited_all = defaultdict(float)
        by_event = defaultdict(list)
        for t, acc in self.market_rent_periods.items():
            by_event[self.ev(t)].append((t, acc))
        for ev, items in by_event.items():
            rows = self.credits.get(ev) or []
            # Distinct period bounds ACROSS the event's markets: ledger rows
            # are event-keyed (one credit per market clearing the floor), so
            # per-market attribution inside an event is not recoverable and a
            # credit must be consumed exactly ONCE per event period.
            bounds = sorted({(p["lo"], p["hi"]) for _t, acc in items for p in acc},
                            key=lambda b: b[1], reverse=True)
            consumed = set()
            qualified = {}
            for (lo, hi) in bounds:
                measured = None
                # `lo` non-finite is the (-inf, program_start) PSEUDO-period
                # that _periods_for manufactures from bounds[0]. It always
                # carries $0 of raw rent and it has a finite `hi`, so it was
                # the ONLY period old enough to pass the CREDIT_LAG_DAYS test
                # while the ledger is stale -- i.e. the real, rent-bearing
                # period stayed est_floored and a $0 pseudo-period consumed
                # the credit and got stamped `credited`. It is not a program
                # period and can never be credited.
                if newest and rows and math.isfinite(hi) and math.isfinite(lo):
                    p_end = datetime.fromtimestamp(hi, timezone.utc)
                    if p_end <= newest - timedelta(days=CREDIT_LAG_DAYS):
                        lo_d = p_end.strftime("%Y-%m-%d")
                        hi_d = (p_end + timedelta(days=CREDIT_WINDOW_DAYS)).strftime("%Y-%m-%d")
                        amt = 0.0
                        hit = False
                        for i, (dte, a, _k) in enumerate(rows):
                            if i in consumed or not dte:
                                continue
                            if lo_d <= dte <= hi_d:
                                amt += a
                                hit = True
                                consumed.add(i)
                        # $0.00 is NOT evidence of a credit. A period that
                        # qualifies but whose ledger rows fall outside its
                        # window has no MEASURED term at all; stamping it
                        # `credited` and zeroing the estimate produced records
                        # reading `rent_basis: mixed_by_period` next to
                        # `rent_measured_frac: 0.0`. Leave it MODELLED.
                        measured = amt if hit else None
                qualified[(lo, hi)] = measured
            for _t, acc in items:
                for p in acc:
                    # MEASURED replaces MODELLED for this period at EVENT
                    # level only; the market-level share is unknowable, so
                    # `market_rent` below stays MODELLED in every case and
                    # there is deliberately NO per-period `value` field --
                    # an earlier cut wrote one that nothing read, so a future
                    # editor "fixing" it would have changed nothing while the
                    # real EST/CREDITED-never-summed invariant lives in the
                    # `total` accumulator underneath.
                    p["basis"] = ("credited"
                                  if qualified.get((p["lo"], p["hi"])) is not None
                                  else "est_floored")
            total = meas_total = 0.0
            for (lo, hi), meas in qualified.items():
                if meas is not None:
                    total += meas
                    meas_total += meas
                else:
                    total += sum(p["floored"] for _t, acc in items for p in acc
                                 if (p["lo"], p["hi"]) == (lo, hi)) * RENT_FACTOR
            self.event_rent[ev] = total
            self.event_rent_measured[ev] = meas_total
        roster_events = {self.ev(t) for t in self.roster} | set(self.roster_events_file)
        for ev, rows in self.credits.items():
            if ev in roster_events:
                self.event_credited_all[ev] = sum(a for (_d, a, _k) in rows)

        # A credited period is attributed to the event, so the market-level
        # rent stays MODELLED (ledger rows are event-keyed; per-market
        # attribution inside an event is NOT recoverable).
        self.market_rent = {t: sum(p["floored"] for p in acc) * RENT_FACTOR
                            for t, acc in self.market_rent_periods.items()}

    def _rent_basis(self, periods):
        bases = {p.get("basis", "est_floored") for p in periods}
        if bases == {"credited"}:
            return "credited"
        if "credited" in bases:
            return "mixed_by_period"
        return "est_floored"

    # ------------------------------------------------------------- cohorts

    def _assign(self, dim, ticker):
        a = self.admission.get(ticker) or {}
        if dim == "spread":
            v = self.fit_spread.get(ticker)
            if v is None:
                v = fnum(a.get("spread_cents"))
        elif dim == "mid":
            v = self.fit_mid.get(ticker)
            if v is None:
                v = fnum(a.get("mid_cents"))
        elif dim == "dtc":
            close = ticker_close_date(ticker)
            adm = parse_iso(a.get("ts"))
            if adm is None:
                fc = (self.markets.get(ticker) or {}).get("first_cycle")
                adm = datetime.fromtimestamp(fc, timezone.utc) if fc else None
            v = ((close - adm).total_seconds() / 86400.0
                 if (close and adm) else None)
        else:
            # pool $/day is the admission snapshot; it does not flicker.
            v = fnum(a.get("dollars_per_day"))
        return v

    def _bucket_of(self, dim, v):
        if v is None:
            return None
        spec = self.edges[dim]
        closed = spec["closed"]
        # A "closed":"both" dimension has INTEGER-CENT edges with GAPS
        # between them -- spread is [0,1], [2,4], [5,9], [10,19], [20,inf).
        # The fitting-side value is statistics.median() over the trailing 6h
        # of two-sided rows, which returns a half-cent for any even-length
        # sample, so 1.5 / 4.5 / 9.5 / 19.5 fell into NO bucket and the
        # market vanished from the whole `spread` dimension with no warning
        # and no count -- and 4.5 is exactly the 4c<->5c flicker the
        # trailing median was introduced to smooth (judge must_fix J2-4).
        # Rounding HALF-UP to the cent the edges are written in keeps the
        # frozen sha intact; the INTERPOLATION on the applying side still
        # uses the unrounded value (dim_value), so the cliff fix is
        # unaffected.
        if closed == "both":
            v = math.floor(v + 0.5) if v >= 0 else math.ceil(v - 0.5)
        for key, lo, hi in spec["buckets"]:
            if hi is None:
                if v >= lo:
                    return key
            elif closed == "both":
                if lo <= v <= hi:
                    return key
            else:
                if lo <= v < hi:
                    return key
        return spec["buckets"][-1][0] if v >= spec["buckets"][-1][1] else None

    def _refit_edges(self):
        """--refit-edges: quintiles of risk-days per dimension. Stamps
        edges_refit:true and the bot's loader rejects the file by default."""
        for dim in DIM_ORDER:
            pairs = []
            for t, m in self.markets.items():
                v = self._assign(dim, t)
                if v is None or m["risk_days"] <= 0:
                    continue
                pairs.append((v, m["risk_days"]))
            if len(pairs) < 10:
                continue
            pairs.sort()
            total = sum(p[1] for p in pairs)
            cuts, acc, k = [], 0.0, 1
            for v, w in pairs:
                acc += w
                while k < 5 and acc >= total * k / 5.0:
                    cuts.append(round(v, 2))
                    k += 1
            cuts = sorted(set(cuts))[:4]
            bounds = [0.0] + cuts
            buckets = []
            for i, lo in enumerate(bounds):
                hi = bounds[i + 1] if i + 1 < len(bounds) else None
                buckets.append((f"q{i+1}", lo, hi))
            self.edges[dim]["buckets"] = buckets
            self.edges[dim]["closed"] = "left"

    def _build_cohorts(self):
        if self.refit_edges:
            self._refit_edges()
        self.bucket_of = {dim: {} for dim in DIM_ORDER}
        self.dim_value = {dim: {t: self._assign(dim, t) for t in self.markets}
                          for dim in DIM_ORDER}
        # SILENT EXCLUSIONS NEVER SHOW IN LOGS (feedback_sweep_class_after_fix):
        # a market with a real dimension value that lands in no bucket
        # contributes to the tier mu and to n_markets but to NO bucket, so the
        # dimension's bucket risk-days stop summing to coverage.risk_days and
        # every dev_D on that dimension shifts. Count it and say so.
        self.unassigned = {}
        for dim in DIM_ORDER:
            miss, miss_rd = [], 0.0
            for t in self.markets:
                v = self.dim_value[dim][t]
                b = self._bucket_of(dim, v)
                self.bucket_of[dim][t] = b
                if b is None and v is not None:
                    miss.append(t)
                    miss_rd += self.markets[t]["risk_days"]
            self.unassigned[dim] = {"n_markets": len(miss),
                                    "risk_days": round(miss_rd, 2),
                                    "tickers": sorted(miss)[:10]}
            if miss:
                self._warn(
                    f"{len(miss)} market(s) / {miss_rd:,.1f} $-days have a "
                    f"{dim} value that falls in NO bucket and are dropped from "
                    f"that dimension only (e.g. {', '.join(sorted(miss)[:3])})")

        self.risk_total = sum(m["risk_days"] for m in self.markets.values())
        self.risk_total_50 = sum(m["risk_days_at50c"] for m in self.markets.values())
        self.mo_total = sum(self.market_mo.values())
        self.rent_total = sum(self.market_rent.values())
        self.mu = {
            "trading": safe_div(self.mo_total, self.risk_total),
            "blend": safe_div(self.mo_total + self.rent_total, self.risk_total),
        }
        self.cohorts = self._fit_cohorts(set(self.markets))
        self.prev_table = load_json(os.path.join(self.work_dir, TABLE_NAME), None)
        self._apply_ratchet()

    def _fit_cohorts(self, keep):
        """The cohort table on BOTH bases. Only `keep` markets contribute
        (leave-one-EVENT-out / leave-one-SERIES-out re-fit the same way)."""
        risk_total = sum(self.markets[t]["risk_days"] for t in keep)
        mo_total = sum(self.market_mo.get(t, 0.0) for t in keep)
        rent_total = sum(self.market_rent.get(t, 0.0) for t in keep)
        mu = {"trading": safe_div(mo_total, risk_total),
              "blend": safe_div(mo_total + rent_total, risk_total)}
        out = {}
        for dim in DIM_ORDER:
            spec = self.edges[dim]
            buckets = []
            for key, lo, hi in spec["buckets"]:
                members = [t for t in keep if self.bucket_of[dim].get(t) == key]
                d = sum(self.markets[t]["risk_days"] for t in members)
                num_tr = sum(self.market_mo.get(t, 0.0) for t in members)
                num_bl = num_tr + sum(self.market_rent.get(t, 0.0) for t in members)
                n_ser = len({ser_of(t) for t in members})
                n_ev = len({self.ev(t) for t in members})
                n_fills = sum(self.market_fills_matured.get(t, 0) for t in members)
                n_unm = sum(self.market_unmarked.get(t, 0) for t in members)
                w = safe_div(d, d + COHORT_PRIOR_RISK_DAYS)
                raw_tr = safe_div(num_tr, d)
                raw_bl = safe_div(num_bl, d)
                muted = (n_ser < BUCKET_MUTE_MIN_SERIES or n_ev < BUCKET_MUTE_MIN_EVENTS
                         or (n_fills > 0 and n_unm / n_fills > DATA_THIN_UNMARKED_FRAC))
                dev_tr = 0.0 if muted else clip(w * (raw_tr - mu["trading"]), -DIM_CLIP, DIM_CLIP)
                dev_bl = 0.0 if muted else clip(w * (raw_bl - mu["blend"]), -DIM_CLIP, DIM_CLIP)
                vals = [(self.dim_value[dim][t], self.markets[t]["risk_days"])
                        for t in members if self.dim_value[dim].get(t) is not None]
                wsum = sum(v[1] for v in vals)
                if wsum > 0:
                    centre = sum(v[0] * v[1] for v in vals) / wsum
                elif hi is None:
                    centre = float(lo)
                else:
                    centre = (float(lo) + float(hi)) / 2.0
                buckets.append({
                    "key": key, "lo": lo, "hi": hi, "centre": round(centre, 4),
                    "n_markets": len(members), "n_series": n_ser, "n_events": n_ev,
                    "n_fills": n_fills, "risk_days": round(d, 2),
                    "raw_trading": round(raw_tr, 6), "dev_trading": round(dev_tr, 6),
                    "raw_blend": round(raw_bl, 6), "dev_blend": round(dev_bl, 6),
                    "muted": bool(muted),
                })
            out[dim] = {"field": spec["field"], "assign": spec["assign"],
                        "closed": spec["closed"], "buckets": buckets}
        return out

    def _apply_ratchet(self):
        """SPEC 3.7: req may DEEPEN immediately but may only EASE by at most
        RATCHET_EASE_PER_RUN versus the previously published value."""
        self.ratchet_eased = 0
        prev = self.prev_table
        if not prev:
            self.notes.append("no previous scan_perf.json: the asymmetry ratchet "
                              "has nothing to ease against on this run")
            return
        pb = {}
        for dim, blk in (prev.get("cohorts") or {}).items():
            for b in blk.get("buckets") or []:
                pb[(dim, b.get("key"))] = b
        for dim, blk in self.cohorts.items():
            for b in blk["buckets"]:
                p = pb.get((dim, b["key"]))
                if not p:
                    continue
                for col in ("dev_trading", "dev_blend"):
                    pv = p.get(col)
                    if pv is None:
                        continue
                    lim = float(pv) + RATCHET_EASE_PER_RUN
                    if b[col] > lim:
                        b[col] = round(min(0.0, lim), 6)
                        self.ratchet_eased += 1

    # --------------------------------------------------------- the adjustment

    def _interp_dev(self, dim, value, basis="trading", cohorts=None):
        """Piecewise-linear between adjacent bucket CENTRES; flat outside the
        end centres (the cliff fix, judge must_fix J2-4)."""
        blk = (cohorts or self.cohorts)[dim]
        pts = [(b["centre"], b["dev_trading" if basis == "trading" else "dev_blend"])
               for b in blk["buckets"]]
        pts.sort()
        if value is None or not pts:
            return 0.0
        if value <= pts[0][0]:
            return pts[0][1]
        if value >= pts[-1][0]:
            return pts[-1][1]
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            if x0 <= value <= x1:
                if x1 == x0:
                    return min(y0, y1)
                f = (value - x0) / (x1 - x0)
                return y0 + f * (y1 - y0)
        return pts[-1][1]

    def adj_struct(self, ticker, basis=None, cohorts=None):
        """adj_struct = the SINGLE most negative dimension deviation.

        NEVER a sum: spread, days-to-close and pool are heavily collinear on
        this roster (KXCPIYOY sits in spread 20c+, dtc 30-90d AND pool <$20 at
        once), so summing counts the same -$46.30 three times."""
        basis = basis or self.score_basis
        best = 0.0
        code = ""
        for dim in DIM_ORDER:
            v = self.dim_value[dim].get(ticker)
            if v is None:
                continue
            d = self._interp_dev(dim, v, basis, cohorts)
            if d < best:
                best = d
                b = self.bucket_of[dim].get(ticker) or ""
                code = f"{_DIM_CODE[dim]}{b}"
        return best, code

    # --------------------------------------------------------- series/events

    def _unit_stats(self, tickers, basis):
        risk = sum(self.markets[t]["risk_days"] for t in tickers)
        mo = sum(self.market_mo.get(t, 0.0) for t in tickers)
        rent = sum(self.market_rent.get(t, 0.0) for t in tickers)
        num = mo if basis == "trading" else mo + rent
        eps = sum(len(self.market_episodes.get(t, ())) for t in tickers)
        fills = sum(self.market_fills.get(t, 0) for t in tickers)
        fills_mat = sum(self.market_fills_matured.get(t, 0) for t in tickers)
        unm = sum(self.market_unmarked.get(t, 0) for t in tickers)
        ct = sum(self.market_ct_matured.get(t, 0.0) for t in tickers)
        days = set()
        for t in tickers:
            days |= self.market_days.get(t, set())
        return {"risk_days": risk, "mo": mo, "rent": rent, "num": num,
                "episodes": eps, "fills": fills, "fills_matured": fills_mat,
                "unmarked": unm, "contracts": ct, "n_days": len(days),
                "n_markets": len(tickers), "y": safe_div(num, risk)}

    def _bootstrap_ci_hi(self, tickers, rent, seed_key):
        """One-sided upper bound of (markout + rent) per $-day.

        CLUSTERED: SPEC 3.6 says "clustered by market" but SPEC test 44
        ("three strikes of one event are one draw") defines the cluster as
        the EVENT, and the two cannot both hold. The event is taken, because
        three strikes of one event move on the same print (KXCPIYOY's three
        strikes all repriced on the same September CPI) and the coarser
        cluster gives the WIDER interval — i.e. it bars LESS often, which is
        the fail-safe direction. Recorded as a deviation.

        Rent enters as a deterministic offset: the CI covers the markout
        term, which is what queue selection makes noisy."""
        ts = [t for t in tickers if self.markets[t]["risk_days"] > 0]
        if len(ts) < 2:
            return None
        rng = random.Random(f"{BOOTSTRAP_SEED}:{seed_key}")
        clusters = defaultdict(lambda: [0.0, 0.0])
        for t in ts:
            c = clusters[self.ev(t)]
            c[0] += self.market_mo.get(t, 0.0)
            c[1] += self.markets[t]["risk_days"]
        mos = [c[0] for c in clusters.values()]
        rds = [c[1] for c in clusters.values()]
        n = len(mos)
        if n < 2:
            return None
        draws = []
        for _ in range(BAR_BOOTSTRAP):
            num = den = 0.0
            for _j in range(n):
                k = rng.randrange(n)
                num += mos[k]
                den += rds[k]
            if den > 0:
                draws.append((num + rent) / den)
        if not draws:
            return None
        draws.sort()
        idx = min(len(draws) - 1, int(math.ceil(BAR_CI * len(draws))) - 1)
        return draws[idx]

    def _prev_verdicts(self, kind):
        prev = self.prev_table or {}
        return {k: v for k, v in (prev.get(kind) or {}).items()}

    def _build_units(self):
        basis = self.score_basis
        by_series = defaultdict(list)
        by_root = defaultdict(list)
        self.event_markets = defaultdict(list)
        for t in self.markets:
            by_series[ser_of(t)].append(t)
            ev = self.ev(t)
            self.event_markets[ev].append(t)
            by_root[root_of(ev)].append(t)

        prev_series = self._prev_verdicts("series")
        prev_events = self._prev_verdicts("events")
        self.series_recs = {}
        self.event_recs = {}
        self.unit_dev = {}
        self.bar_blocked_reason = []
        self.max_episodes_unit = ("", 0)

        for unit_kind, groups, prev in (("series", by_series, prev_series),
                                        ("events", by_root, prev_events)):
            for key, tickers in sorted(groups.items()):
                st = self._unit_stats(tickers, basis)
                if st["episodes"] > self.max_episodes_unit[1]:
                    self.max_episodes_unit = (key, st["episodes"])
                dated = {self.ev(t) for t in tickers}
                if unit_kind == "events" and len(dated) < 2:
                    continue          # the event term would be a market-level n=1 scorecard
                thin = (st["fills_matured"] > 0 and
                        st["unmarked"] / st["fills_matured"] > DATA_THIN_UNMARKED_FRAC)
                # structural expectation for this unit
                wsum = sum(self.markets[t]["risk_days"] for t in tickers)
                if wsum > 0:
                    struct = sum(self.adj_struct(t, basis)[0] * self.markets[t]["risk_days"]
                                 for t in tickers) / wsum
                else:
                    struct = 0.0
                y_cohort = self.mu[basis] + struct
                shrink = safe_div(st["fills"], st["fills"] + SERIES_PRIOR_FILLS)
                raw_dev = clip(shrink * (st["y"] - y_cohort), -DIM_CLIP, DIM_CLIP)
                if thin or st["risk_days"] <= 0:
                    raw_dev = 0.0
                pk = prev.get(key) or {}
                pdev = pk.get("dev")
                dev = raw_dev
                if isinstance(pdev, (int, float)):
                    lim = float(pdev) + RATCHET_EASE_PER_RUN
                    if dev > lim:
                        dev = min(0.0, lim)
                        self.ratchet_eased += 1
                dev = clip(dev, -DIM_CLIP, 0.0) if dev < 0 else 0.0

                ci_hi = None
                bar_ok = False
                if (st["episodes"] >= BAR_MIN_EPISODES and st["n_markets"] >= BAR_MIN_MARKETS
                        and st["n_days"] >= BAR_MIN_DAYS and not thin):
                    ci_hi = self._bootstrap_ci_hi(tickers, st["rent"], key)
                    bar_ok = ci_hi is not None and ci_hi < 0
                # sustain_runs >= 2 means "the same verdict on the previous
                # run", but a first run can never publish "bar", so the
                # sustain flag is the previous run's bar ELIGIBILITY (which is
                # what condition 3 actually tests) — otherwise the bar could
                # never arm at all.
                sustained = bool(pk.get("verdict") == "bar" or pk.get("bar_eligible"))
                bar_since = pk.get("bar_since")
                verdict = "neutral"
                until = None
                if dev < -1e-9:
                    verdict = "down_rank"
                if bar_ok and sustained and self.bar_enabled:
                    verdict = "bar"
                    until = iso_z(self.asof + timedelta(days=BAR_TTL_DAYS))
                elif bar_ok and not sustained:
                    self.bar_blocked_reason.append(
                        f"{key}: bar-eligible but sustain_runs<{BAR_SUSTAIN_RUNS}")
                    if verdict == "neutral":
                        verdict = "down_rank"
                elif bar_ok and not self.bar_enabled:
                    self.bar_blocked_reason.append(
                        f"{key}: bar-eligible but IMM_SCAN_PERF_BAR=0")
                    if verdict == "neutral":
                        verdict = "down_rank"
                # MINIMUM DWELL: a bar already live keeps running for
                # BAR_MIN_DWELL_DAYS unless its own TTL expires. Tightening is
                # immediate; relaxing is by timeout only.
                if verdict != "bar" and pk.get("verdict") == "bar" and bar_since:
                    since = parse_iso(bar_since)
                    prev_until = parse_iso(pk.get("until"))
                    live = prev_until is None or prev_until > self.asof
                    if (live and since and
                            (self.asof - since).days < BAR_MIN_DWELL_DAYS):
                        verdict = "bar"
                        until = pk.get("until")

                # a data_thin group is forced neutral and can NEVER be barred
                cohort_label = "data_thin" if thin else ""
                if thin:
                    verdict = "neutral"
                    dev = 0.0
                    until = None

                code = self._unit_code(tickers, basis)
                tset = set(tickers)          # hoisted: was rebuilt per fill row
                rec = {
                    "n_fills": st["fills"], "n_episodes": st["episodes"],
                    "n_markets": st["n_markets"], "n_days": st["n_days"],
                    "contracts_filled": round(sum(abs(r["q"]) for r in self.fill_rows
                                                  if r["ticker"] in tset), 2),
                    "contracts_matured": round(st["contracts"], 2),
                    "risk_days": round(st["risk_days"], 2),
                    "mo_dollars": round(st["mo"], 4),
                    "markout_c_per_ct_24h": round(safe_div(100.0 * st["mo"], st["contracts"]), 3),
                    "realized_dollars": round(sum(self.markets[t]["realized_dollars"]
                                                  for t in tickers), 4),
                    "mtm_dollars": round(sum(self.markets[t]["mtm_dollars"]
                                             for t in tickers), 4),
                    "rent_modelled": round(st["rent"], 4),
                    "rent_basis": self._unit_rent_basis(tickers),
                    "rent_measured_frac": round(self._unit_measured_frac(tickers), 4),
                    "rent_measured_dollars": round(
                        self._unit_measured_dollars(tickers), 4),
                    "credited_measured": round(self._unit_credited(tickers), 4),
                    "y_trading": round(safe_div(st["mo"], st["risk_days"]), 6),
                    "y_cohort": round(y_cohort, 6),
                    "dev": round(dev, 6),
                    "ci_hi": (None if ci_hi is None else round(ci_hi, 6)),
                    "unmarked_fills": st["unmarked"],
                    "cohort": cohort_label,
                    "verdict": verdict,
                    "bar_eligible": bool(bar_ok),
                    "code": code,
                }
                if until:
                    rec["until"] = until
                if verdict == "bar":
                    rec["bar_since"] = bar_since or iso_z(self.asof)
                if unit_kind == "events":
                    rec["root"] = key
                    rec["n_dated_events"] = len(dated)
                    rec["events"] = sorted(dated)
                    self.event_recs[key] = rec
                else:
                    self.series_recs[key] = rec
                self.unit_dev[(unit_kind, key)] = dev
        self._enforce_bar_caps()

    def _enforce_bar_caps(self):
        """Bars over the caps are downgraded to down_rank BEFORE writing and
        the downgrade is recorded in notes (writer half of the double guard;
        the loader rejects >40 and the reader truncates to 5)."""
        bars = [(k, "series", r) for k, r in self.series_recs.items() if r["verdict"] == "bar"]
        bars += [(k, "events", r) for k, r in self.event_recs.items() if r["verdict"] == "bar"]
        # The "universe" denominator is the scored MARKET count, not the unit
        # count: with two scored units 25% would round to zero and no bar
        # could ever fire, which is not what a cap is for.
        n_markets = max(1, len(self.markets))
        allowed = min(MAX_BARRED, int(MAX_BAR_FRAC_UNIVERSE * n_markets))
        if len(bars) <= allowed:
            return
        bars.sort(key=lambda x: x[2].get("ci_hi") if x[2].get("ci_hi") is not None else 0.0)
        for key, _kind, rec in bars[allowed:]:
            rec["verdict"] = "down_rank"
            rec.pop("until", None)
            rec.pop("bar_since", None)
            self.notes.append(f"bar on {key} downgraded to down_rank before writing: "
                              f"over the writer cap of {allowed} live bars")

    def _unit_code(self, tickers, basis):
        counts = defaultdict(float)
        for t in tickers:
            _d, c = self.adj_struct(t, basis)
            if c:
                counts[c] += self.markets[t]["risk_days"] or 1.0
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:2]
        return "|".join(k for k, _v in top)

    def _unit_rent_basis(self, tickers):
        bases = set()
        for t in tickers:
            bases.add(self._rent_basis(self.market_rent_periods.get(t, [])))
        if bases == {"credited"}:
            return "credited"
        if "credited" in bases or "mixed_by_period" in bases:
            return "mixed_by_period"
        return "est_floored"

    def _unit_measured_dollars(self, tickers):
        """The MEASURED credit that REPLACED an estimate for a qualifying
        period (not the same thing as `credited_measured`, which is every
        ledger dollar the event ever received). Emitted next to the fraction
        because a fraction that rounds to 0.0 next to a non-zero
        `credited_measured` reads as a contradiction."""
        evs = {self.ev(t) for t in tickers}
        return sum(self.event_rent_measured.get(e, 0.0) for e in evs)

    def _unit_measured_frac(self, tickers):
        evs = {self.ev(t) for t in tickers}
        meas = sum(self.event_rent_measured.get(e, 0.0) for e in evs)
        tot = sum(self.event_rent.get(e, 0.0) for e in evs)
        return safe_div(meas, tot)

    def _unit_credited(self, tickers):
        evs = {self.ev(t) for t in tickers}
        return sum(self.event_credited_all.get(e, 0.0) for e in evs)

    # ------------------------------------------------------------- guards

    def reconciliation(self):
        """Cent-exact self-check, DE-CIRCULARISED: the scorer's INDEPENDENT
        avg-cost replay must equal the sum of realized-sink DELTAS over
        COMPLETE UTC days in W. The sink is the comparison side only."""
        today = self.asof.strftime("%Y-%m-%d")
        lo = self.window_start.strftime("%Y-%m-%d")
        mine = sum(v for (t, d), v in self.replay_realized_by_day.items()
                   if lo <= d < today)
        sink = sum(v for (t, d), v in self.sink_realized_by_day.items()
                   if lo <= d < today)
        return round(mine - sink, 6), mine, sink

    def exposure_dod(self):
        path = os.path.join(self.work_dir, HISTORY_NAME)
        self.prev_history_row = None
        prev = None
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            prev = json.loads(line)
                        except ValueError:
                            continue
        except OSError:
            return None
        if not prev:
            return None
        self.prev_history_row = prev
        pr = fnum(prev.get("risk_days"))
        if not pr:
            return None
        return (self.risk_total - pr) / pr

    def clamped_frac(self):
        """SPEC 4.4 guard 3 / SPEC 6.2 -- the sign-flip detector.

        INTEGRATION 2026-09-18: the bot's `_scan_perf_validate` counts a
        DIFFERENT set than this writer did -- every bucket's `dev_trading`
        (muted ones included, which carry 0.0) plus each series/event
        `dev`, and never `dev_blend`, because `trading` is the only acting
        column (SPEC 3.2). MEASURED on today's live table that denominator
        is 115 against the writer's 129, so a file sitting at 12 clamped
        acting devs is 10.4% to the loader (REFUSED WHOLE, tier silently
        reverts to the last good table) and 9.3% to the writer (shipped).
        The writer must never be laxer than the reader: both bases are
        computed and the MAX drives the abort and the reported figure, so
        the printed number is never below what the bot will compute."""
        acting = []
        for blk in self.cohorts.values():
            for b in blk["buckets"]:
                acting.append(b["dev_trading"])
        blended = []
        for blk in self.cohorts.values():
            for b in blk["buckets"]:
                if b["muted"]:
                    continue
                blended.append(b["dev_trading"])
                blended.append(b["dev_blend"])
        for rec in list(self.series_recs.values()) + list(self.event_recs.values()):
            acting.append(rec["dev"])
            blended.append(rec["dev"])

        def _frac(xs):
            if not xs:
                return 0.0
            return sum(1 for d in xs
                       if abs(abs(d) - DIM_CLIP) < 1e-9) / float(len(xs))
        if not acting:
            return 0.0, 0
        return max(_frac(acting), _frac(blended)), len(acting)

    # ------------------------------------------------------ counterfactual

    def counterfactual(self):
        """[IN-SAMPLE, MODELLED COUNTERFACTUAL] — never a measurement."""
        blocked = []
        n_adm = 0
        for t, a in self.admission.items():
            denom = fnum(a.get("est_collateral_dollars"), 0.0) or 0.0
            per_day = fnum(a.get("est_dollars_per_day"), 0.0) or 0.0
            if denom <= 0:
                continue
            n_adm += 1
            gross = per_day / denom
            adj, _c = self.adj_struct(t, self.score_basis)
            for kind, key in (("series", ser_of(t)), ("events", root_of(self.ev(t)))):
                d = self.unit_dev.get((kind, key))
                if d is not None and d < adj:
                    adj = d
            adj = clip(adj, -ADJ_CLIP, 0.0)
            req = clip(-1.0 * adj, 0.0, MAX_REQ)
            if gross >= SCAN_MIN_ROI and gross - req < SCAN_MIN_ROI:
                blocked.append(t)
        mo = sum(self.market_mo.get(t, 0.0) for t in blocked)
        rent = sum(self.market_rent.get(t, 0.0) for t in blocked)
        cats = defaultdict(float)
        for t in blocked:
            cat = ((self.series_meta.get(ser_of(t)) or {}).get("category") or "unknown")
            cats[cat] += 1.0
        tot = sum(cats.values()) or 1.0
        return {"n_admissions": n_adm, "n_blocked": len(blocked),
                "blocked": sorted(blocked),
                "mo_avoided": mo, "rent_forgone": rent,
                "category_share": {k: round(v / tot, 4)
                                   for k, v in sorted(cats.items(), key=lambda kv: -kv[1])}}

    def leave_one_out(self, level):
        """leave-one-EVENT-out and leave-one-SERIES-out re-fits (judge
        must_fix J1-2: the per-MARKET leave-one-out is nearly a no-op)."""
        base = self.cohorts
        keyfn = (lambda t: self.ev(t)) if level == "event" else (lambda t: ser_of(t))
        groups = defaultdict(list)
        for t in self.markets:
            groups[keyfn(t)].append(t)
        ranked = sorted(groups.items(),
                        key=lambda kv: -sum(abs(self.market_mo.get(t, 0.0)) for t in kv[1]))
        rows = []
        for key, tickers in ranked[:10]:
            keep = set(self.markets) - set(tickers)
            ref = self._fit_cohorts(keep)
            flips = 0
            acting = 0
            for dim in DIM_ORDER:
                for b0, b1 in zip(base[dim]["buckets"], ref[dim]["buckets"]):
                    if b0["muted"] or b1["muted"]:
                        continue
                    acting += 1
                    s0 = (b0["dev_trading"] > 1e-9) - (b0["dev_trading"] < -1e-9)
                    s1 = (b1["dev_trading"] > 1e-9) - (b1["dev_trading"] < -1e-9)
                    if s0 and s1 and s0 != s1:
                        flips += 1
            worst = min((b["dev_trading"] for dim in DIM_ORDER
                         for b in ref[dim]["buckets"] if not b["muted"]), default=0.0)
            rows.append({"key": key, "n_markets": len(tickers),
                         "sign_flips": flips, "acting_buckets": acting,
                         "worst_dev_trading": round(worst, 5)})
        return rows

    # --------------------------------------------------------------- output

    def table(self):
        cov_markets = len(self.markets)
        cov_events = len({self.ev(t) for t in self.markets})
        cov_series = len({ser_of(t) for t in self.markets})
        recon_delta, _mine, _sink = self.reconciliation()
        dod = self.exposure_dod()
        clamped, _n = self.clamped_frac()
        cf = self.counterfactual()

        barred_series = sum(1 for r in self.series_recs.values() if r["verdict"] == "bar")
        barred_events = sum(1 for r in self.event_recs.values() if r["verdict"] == "bar")
        down = (sum(1 for r in self.series_recs.values() if r["verdict"] == "down_rank") +
                sum(1 for r in self.event_recs.values() if r["verdict"] == "down_rank"))
        n_units = max(1, len(self.markets))

        notes = list(self.notes) + [
            "markout is MEASURED from own fills against the INCENTIVE_MM_STRATEGY "
            "s6 mark hierarchy (4 tiers, no carry-forward tier)",
            "rent is MODELLED (cycle-log accrual integral, $1.00/market/period floor) "
            "unless rent_basis=credited",
            "EST and CREDITED are never summed: per (market, period) the rent is one "
            "basis or the other",
            "MARKET-level rent is ALWAYS MODELLED: ledger rows are event-keyed, so a "
            "credit replaces an estimate at EVENT level only. rent_modelled and "
            "credited_measured on one record are NOT two halves of a total and must "
            "never be added; rent_measured_dollars is the part that replaced an "
            "estimate, and rent_basis != est_floored implies it is non-zero",
            "adj = the SINGLE most negative dimension deviation, never a sum",
            "cohort bucket 'hi': null means +infinity (open-ended top bucket); 'closed' "
            "is 'both' (lo<=v<=hi) for spread and 'left' (lo<=v<hi) elsewhere",
            "'events' records are keyed by EVENT ROOT (SPEC 6.1), not by dated event",
            "NO time decay in v1: SPEC 3 defines no half-life weighting, so every "
            "observation in the window has weight 1 (an echoed-but-unapplied "
            "--halflife-days was removed 2026-09-18 rather than left to mislead)",
            "mark tiers 3 (one-sided) and 4 (unmarked) are UNREACHABLE while a "
            "position is open: marks_*.jsonl writes every 300s per open position, so "
            "tier 2's sink fallback pre-empts them. data_thin and bar condition 4 "
            "therefore cannot fire on a held market; the honest freshness detector is "
            "tier.mark_source_detail, not mark_source_mix",
            "the avg-cost replay is independent of the REALIZED SINK but NOT of the "
            "bot's cost basis on the tickers listed in tier.recon_seeded_tickers "
            "(seeded from the first fill's pos_before/avg_before)",
            "the bootstrap clusters by EVENT (SPEC test 44), not by market: the "
            "coarser cluster widens the interval and therefore bars less often",
        ]
        if barred_series + barred_events == 0:
            notes.append("no group qualifies for a bar in this window")

        markets = {}
        for t, m in sorted(self.markets.items()):
            markets[t] = {
                "realized_dollars": round(m["realized_dollars"], 4),
                "mtm_dollars": round(m["mtm_dollars"], 4),
                "mo_dollars": round(self.market_mo.get(t, 0.0), 4),
                "risk_days": round(m["risk_days"], 3),
                "pos": round(m["pos"], 3),
                "avg_cents": round(m["avg_cents"], 3),
                "mark_cents": (None if m["mark_cents"] is None else round(m["mark_cents"], 2)),
                "adopted_capped": bool(m["adopted_capped"]),
            }
        if len(markets) + len(self.series_recs) + len(self.event_recs) > MAX_RECORDS:
            keep = sorted(markets, key=lambda t: -abs(markets[t]["mo_dollars"]))
            room = max(0, MAX_RECORDS - len(self.series_recs) - len(self.event_recs))
            dropped = len(markets) - room
            markets = {t: markets[t] for t in keep[:room]}
            self._warn(f"markets block trimmed to {room} records "
                       f"({dropped} dropped) to stay under MAX_RECORDS={MAX_RECORDS}")

        # INTEGRATION 2026-09-18: the trim above counts markets+series+events,
        # but the bot's record cap counts series + events + BUCKETS -- it never
        # reads `markets` (SPEC 5). Trimming `markets` therefore does not bound
        # the quantity that actually decides acceptance, and a table over the
        # cap is refused WHOLE (fail-open to the last good table, i.e. a stale
        # verdict outliving its evidence). Bound the loader's denominator here
        # too, shedding only NEUTRAL records, most-positive `dev` first: a
        # neutral record with dev >= 0 is a pure no-op for `scan_perf_adj`, and
        # shedding a mildly negative one can only REDUCE a penalty, never add
        # one. down_rank and bar records are never shed.
        series_out = dict(self.series_recs)
        events_out = dict(self.event_recs)
        n_buckets = sum(len(blk["buckets"]) for blk in self.cohorts.values())
        n_loader = len(series_out) + len(events_out) + n_buckets
        if n_loader > MAX_RECORDS:
            shed = sorted(
                [("s", k, r) for k, r in series_out.items()
                 if r.get("verdict") == "neutral"]
                + [("e", k, r) for k, r in events_out.items()
                   if r.get("verdict") == "neutral"],
                key=lambda p: -float(p[2].get("dev") or 0.0))
            n_drop = min(len(shed), n_loader - MAX_RECORDS)
            for kind, k, _r in shed[:n_drop]:
                (series_out if kind == "s" else events_out).pop(k, None)
            residual = n_loader - n_drop
            self._warn(
                f"{n_drop} neutral record(s) dropped so the BOT LOADER's count "
                f"(series+events+buckets = {n_loader}) comes down to "
                f"{residual} against MAX_RECORDS={MAX_RECORDS}; above that it "
                f"refuses the file whole and the tier reverts to the last "
                f"good table")
            if residual > MAX_RECORDS:
                # There were not enough NEUTRAL records to get under the cap
                # and down_rank/bar records are never shed, so the loader WILL
                # refuse this file whole. Say so rather than claim the count
                # "stays under" a cap it does not.
                self._warn(
                    f"! the loader count is still {residual} > "
                    f"{MAX_RECORDS} after shedding every sheddable neutral "
                    f"record: the BOT WILL REFUSE THIS FILE WHOLE and keep "
                    f"the last good table")

        ct = self.cov["contracts_matured"]
        out = {
            "version": 1,
            "generated_at": iso_z(self.asof),
            "generated_by": f"imm_scan_perf.py@{_git_sha()}",
            "window": {
                "start": iso_z(self.window_start), "end": iso_z(self.asof),
                "days": self.window_days,
                "horizon_hours": self.horizon_h,
                "maturity_cutoff": iso_z(self.maturity_cutoff),
            },
            "params": {
                "score_basis": self.score_basis,
                "cohort_prior_risk_days": COHORT_PRIOR_RISK_DAYS,
                "series_prior_fills": SERIES_PRIOR_FILLS,
                "dim_clip": DIM_CLIP, "adj_clip": ADJ_CLIP, "max_req": MAX_REQ,
                "winsor_cents": WINSOR_CENTS, "mark_tolerance_secs": MARK_TOLERANCE_SECS,
                "data_thin_unmarked_frac": DATA_THIN_UNMARKED_FRAC,
                "bucket_mute_min_series": BUCKET_MUTE_MIN_SERIES,
                "bucket_mute_min_events": BUCKET_MUTE_MIN_EVENTS,
                "bar_min_episodes": BAR_MIN_EPISODES, "bar_min_markets": BAR_MIN_MARKETS,
                "bar_min_days": BAR_MIN_DAYS, "bar_ci": BAR_CI,
                "bar_bootstrap": BAR_BOOTSTRAP, "bar_ttl_days": BAR_TTL_DAYS,
                "bar_sustain_runs": BAR_SUSTAIN_RUNS,
                "bar_min_dwell_days": BAR_MIN_DWELL_DAYS,
                "ratchet_ease_per_run": RATCHET_EASE_PER_RUN,
                "rent_factor": RENT_FACTOR, "rent_factor_source": RENT_FACTOR_SOURCE,
                "cohort_edges_sha256": edges_sha256(self.edges),
                "edges_refit": bool(self.refit_edges),
            },
            "coverage": {
                "markets": cov_markets, "events": cov_events, "series": cov_series,
                "fills": self.cov["fills"], "fills_matured": self.cov["fills_matured"],
                "fills_pending": self.cov["fills_pending"],
                "fills_unmarked": self.cov["fills_unmarked"],
                "episodes": self.cov["episodes"],
                "episodes_matured": self.cov["episodes_matured"],
                "contracts_filled": self.cov["contracts_filled"],
                "contracts_matured": self.cov["contracts_matured"],
                "risk_days": round(self.risk_total, 2),
                "risk_days_at50c": round(self.risk_total_50, 2),
                "settled_markets": len({d["ticker"] for d in self.settlements}),
                "credited_events": len([e for e, v in self.event_credited_all.items() if v]),
                "adopted_capped_tickers": self.adopted_capped,
            },
            "tier": {
                "mu_trading_only": round(self.mu["trading"], 6),
                "mu_rent_blended": round(self.mu["blend"], 6),
                "markout_c_per_ct_24h": round(safe_div(100.0 * self.mo_total, ct), 3),
                "markout_c_per_ct_1h": self.markout_horizons[1]["c_per_ct"],
                "markout_c_per_ct_72h": self.markout_horizons[72]["c_per_ct"],
                # same statistic at three horizons, each with its own
                # maturity cutoff / episode collapse / winsorisation, and its
                # own sample size next to the SPEC 10.3 baselines
                "markout_horizons": {str(h): v for h, v
                                     in sorted(self.markout_horizons.items())},
                "mo_dollars": round(self.mo_total, 2),
                "realized_dollars": round(sum(m["realized_dollars"]
                                              for m in self.markets.values()), 2),
                "mtm_dollars": round(sum(m["mtm_dollars"] for m in self.markets.values()), 2),
                "rent_modelled_dollars": round(self.rent_total, 2),
                "rent_cents_per_contract": round(safe_div(100.0 * self.rent_total, ct), 3),
                "rent_basis": self._unit_rent_basis(list(self.markets)),
                "rent_measured_frac": round(self._unit_measured_frac(list(self.markets)), 4),
                "rent_measured_dollars": round(
                    self._unit_measured_dollars(list(self.markets)), 2),
                "credited_measured_dollars": round(sum(self.event_credited_all.values()), 2),
                "ledger_newest_credit_date": self.newest_credit_date,
                "ledger_stale_days": self.ledger_stale_days,
                "mark_source_mix": self.mark_source_mix,
                "mark_source_detail": self.mark_source_detail,
                "reconciliation_delta_dollars": recon_delta,
                # The replay is INDEPENDENT of the realized sink but NOT of
                # the bot's cost basis on these tickers: they predate the
                # fills sink, so the replay is seeded from the first fill's
                # own pos_before/avg_before. For them the check verifies the
                # replay arithmetic, not the basis. Published in `tier`, not
                # only in audit.diagnostics, because it bounds what the
                # cent-exact result means.
                "recon_seeded_tickers": self.seeded_tickers,
                "recon_seeded_frac": round(
                    safe_div(len(self.seeded_tickers), len(self.markets)), 4),
                "risk_days_dod_change": (None if dod is None else round(dod, 4)),
                "clamped_record_frac": round(clamped, 4),
            },
            "cohorts": self.cohorts,
            "series": series_out,
            "events": events_out,
            "markets": markets,
            "limits": {
                "barred_series": barred_series, "barred_events": barred_events,
                "down_ranked": down,
                "bar_frac_universe": round((barred_series + barred_events) / n_units, 4),
                "max_barred": MAX_BARRED, "max_penalized": MAX_PENALIZED,
                "max_bar_frac_universe": MAX_BAR_FRAC_UNIVERSE,
            },
            "audit": {
                "category_block_share": cf["category_share"],
                "category_warning": bool(cf["category_share"] and
                                         max(cf["category_share"].values()) > 0.60),
                "counterfactual_label": "IN-SAMPLE, MODELLED COUNTERFACTUAL",
                "diagnostics": {
                    "counterfactual_blocked": cf["n_blocked"],
                    "counterfactual_admissions": cf["n_admissions"],
                    "counterfactual_mo_avoided_measured": round(cf["mo_avoided"], 2),
                    "counterfactual_rent_forgone_modelled": round(cf["rent_forgone"], 2),
                    "seeded_tickers": self.seeded_tickers,
                    "bucket_unassigned": self.unassigned,
                    "ratchet_eased_records": self.ratchet_eased,
                    "cycle_files_scanned": self.n_cycle_files,
                    "cache_hits": self.cache.hits, "cache_misses": self.cache.misses,
                    "timings": self.timings,
                },
            },
            "unmatched_bar_keys": sorted(
                k for k, r in (list(self.series_recs.items()) +
                               list(self.event_recs.items()))
                if r["verdict"] == "bar" and k not in self.recent_scan_groups),
            "warnings": self.warnings,
            "notes": notes,
        }
        return out

    def history_row(self, tbl, exit_code):
        return {
            "generated_at": tbl["generated_at"], "window_days": self.window_days,
            "basis": self.score_basis,
            "risk_days": tbl["coverage"]["risk_days"],
            "risk_days_at50c": tbl["coverage"]["risk_days_at50c"],
            "mo_dollars": tbl["tier"]["mo_dollars"],
            "realized_dollars": tbl["tier"]["realized_dollars"],
            "mtm_dollars": tbl["tier"]["mtm_dollars"],
            "rent_modelled": tbl["tier"]["rent_modelled_dollars"],
            "credited_measured": tbl["tier"]["credited_measured_dollars"],
            "mu_trading_only": tbl["tier"]["mu_trading_only"],
            "mu_rent_blended": tbl["tier"]["mu_rent_blended"],
            "markout_c_per_ct_24h": tbl["tier"]["markout_c_per_ct_24h"],
            "fills": tbl["coverage"]["fills"], "episodes": tbl["coverage"]["episodes"],
            "episodes_matured": tbl["coverage"]["episodes_matured"],
            "mark_source_detail": tbl["tier"]["mark_source_detail"],
            "unmarked_frac": round(safe_div(tbl["coverage"]["fills_unmarked"],
                                            tbl["coverage"]["fills_matured"]), 4),
            "mark_source_mix": tbl["tier"]["mark_source_mix"],
            "n_down_rank": tbl["limits"]["down_ranked"],
            "n_bar": tbl["limits"]["barred_series"] + tbl["limits"]["barred_events"],
            "reconciliation_delta": tbl["tier"]["reconciliation_delta_dollars"],
            "exit_code": exit_code,
        }


def _git_sha() -> str:
    """The attribution stamp in generated_by. In a worktree .git is a FILE
    holding 'gitdir: <path>', which is how this repo is developed."""
    try:
        gd = os.path.join(_SCRIPT_DIR, ".git")
        if os.path.isfile(gd):
            with open(gd, encoding="utf-8") as f:
                line = f.read().strip()
            gd = line.split("gitdir:", 1)[1].strip() if "gitdir:" in line else gd
        with open(os.path.join(gd, "HEAD"), encoding="utf-8") as f:
            ref = f.read().strip()
        if ref.startswith("ref: "):
            path = os.path.join(gd, ref[5:])
            if not os.path.exists(path):          # worktree -> commondir
                cd = os.path.join(gd, "commondir")
                with open(cd, encoding="utf-8") as f:
                    path = os.path.join(gd, f.read().strip(), ref[5:])
            with open(path, encoding="utf-8") as f:
                return f.read().strip()[:7]
        return ref[:7]
    except (OSError, IndexError):
        return "unknown"


# ---------------------------------------------------------------- printing

def print_report(sc: ScanPerfScorer, tbl, out=sys.stdout):
    p = lambda s="": print(s, file=out)
    cov, tier = tbl["coverage"], tbl["tier"]
    p(f"scan-perf {tbl['generated_at']}  window {sc.window_days}d  "
      f"no decay  H={sc.horizon_h}h  basis={sc.score_basis}")
    p(f"coverage : {cov['markets']} mkts / {cov['events']} events / {cov['series']} series"
      f" | {cov['fills']} fills -> {cov['episodes']} episodes "
      f"({cov['episodes_matured']} matured, {cov['fills_pending']} pending fills, "
      f"{cov['fills_unmarked']} unmarked, "
      f"{cov.get('fills_flat_through', 0)} flat-through)")
    p(f"           risk_days {cov['risk_days']:,.1f} (at50c {cov['risk_days_at50c']:,.1f})"
      f"  adopted-capped: {', '.join(cov['adopted_capped_tickers']) or 'none'}")
    m = tier["mark_source_mix"]
    md = tier["mark_source_detail"]
    p(f"marks    : two_sided {m['two_sided']:.2f} | one_sided {m['one_sided']:.2f} "
      f"| settlement {m['settlement']:.2f}")
    p(f"           of which  cycle book {md['cycle_two_sided']:.2f} "
      f"({md['cycle_two_sided_fills']} fills) | marks sink "
      f"{md['sink_mark']:.2f} ({md['sink_mark_fills']} fills, "
      f"state.last_mark -- NOT a live two-sided book)")
    hz = tier["markout_horizons"]
    p(f"tier     : markout {tier['markout_c_per_ct_24h']:+.2f} c/ct  (MEASURED)")
    for hk in sorted(hz, key=lambda k: int(k)):
        r = hz[hk]
        p(f"           markout @{r['horizon_h']:>2}h {r['c_per_ct']:+.2f} c/ct on "
          f"{r['n_fills']} fills / {r['n_episodes']} episodes / "
          f"{r['n_contracts']:,.0f} ct  (own maturity cutoff, winsorised)")
    p(f"           rent    ${tier['rent_modelled_dollars']:,.2f} floored = "
      f"{tier['rent_cents_per_contract']:+.2f} c/ct  (MODELLED, {tier['rent_basis']}, "
      f"RENT_FACTOR {RENT_FACTOR})")
    p(f"           MEASURED credits ${tier['credited_measured_dollars']:,.2f} on "
      f"{cov['credited_events']} of {cov['events']} events  [never summed with rent]")
    p(f"           realized ${tier['realized_dollars']:,.2f} (MEASURED)   "
      f"mtm ${tier['mtm_dollars']:,.2f} (MODELLED mark)   "
      f"mo ${tier['mo_dollars']:,.2f} (MEASURED)")
    p(f"           mu_trading_only  {tier['mu_trading_only']:+.5f} /$-day     "
      f"mu_rent_blended  {tier['mu_rent_blended']:+.5f} /$-day")
    dod = tier["risk_days_dod_change"]
    p(f"reconciliation vs realized sink: "
      f"{'OK' if abs(tier['reconciliation_delta_dollars']) <= RECON_TOLERANCE_DOLLARS else 'FAILED'}"
      f" (delta ${tier['reconciliation_delta_dollars']:+.2f})   "
      f"risk_days d/d: {'n/a' if dod is None else f'{dod*100:+.1f}%'}   "
      f"clamped records: {tier['clamped_record_frac']*100:.1f}%")
    p("cohorts (BOTH BASES, acting column = " + sc.score_basis + "):")
    p("  dim    bucket    mkts ser evt  fills     $-days     raw_tr     dev_tr |"
      "     raw_bl     dev_bl | muted")
    for dim in DIM_ORDER:
        blk = tbl["cohorts"][dim]
        for b in blk["buckets"]:
            p(f"  {dim:<6} {b['key']:<9} {b['n_markets']:>4} {b['n_series']:>3} "
              f"{b['n_events']:>3} {b['n_fills']:>6} {b['risk_days']:>10,.1f} "
              f"{b['raw_trading']:>+10.4f} {b['dev_trading']:>+10.4f} |"
              f" {b['raw_blend']:>+10.4f} {b['dev_blend']:>+10.4f} | "
              f"{'MUTED' if b['muted'] else ''}  centre={b['centre']:g}")
    lim = tbl["limits"]
    worst = min([r["dev"] for r in list(tbl["series"].values()) +
                 list(tbl["events"].values())] or [0.0])
    p(f"verdicts : {lim['down_ranked']} down_rank (worst req {abs(worst):.4f} /day)   "
      f"{lim['barred_series'] + lim['barred_events']} bar")
    key, n = sc.max_episodes_unit
    if lim["barred_series"] + lim["barred_events"] == 0:
        p(f"bars     : 0 live. NO group qualifies for a bar today (max n_episodes = {n}"
          f"{' on ' + key if key else ''} vs the floor of {BAR_MIN_EPISODES}),")
        p(f"           and IMM_SCAN_PERF_BAR is {1 if sc.bar_enabled else 0} until the "
          f"first statement paste. The bar fires on nothing.")
    else:
        p(f"bars     : {lim['barred_series'] + lim['barred_events']} live "
          f"(TTL {BAR_TTL_DAYS}d, dwell {BAR_MIN_DWELL_DAYS}d).")
    cf = sc.counterfactual()
    p(f"counterfactual [IN-SAMPLE, MODELLED COUNTERFACTUAL]: would have blocked "
      f"~{cf['n_blocked']} of {cf['n_admissions']} admissions "
      f"({safe_div(cf['n_blocked'], cf['n_admissions'])*100:.0f}%),")
    p(f"           [IN-SAMPLE, MODELLED COUNTERFACTUAL] MEASURED markout avoided "
      f"~${-cf['mo_avoided']:,.2f}, MODELLED floored rent forgone ~${cf['rent_forgone']:,.2f}")
    for level in ("event", "series"):
        rows = sc.leave_one_out(level)
        p(f"           leave-one-{level.upper()}-out (top 10 by |markout|): "
          f"max sign flips {max([r['sign_flips'] for r in rows] or [0])} of "
          f"{max([r['acting_buckets'] for r in rows] or [0])} acting buckets")
        for r in rows:
            p(f"             {r['key']:<32} mkts {r['n_markets']:>3}  "
              f"flips {r['sign_flips']:>2}/{r['acting_buckets']:<3} "
              f"worst_dev {r['worst_dev_trading']:+.5f}")
    aud = tbl["audit"]
    share = "  ".join(f"{k} {v:.2f}" for k, v in aud["category_block_share"].items())
    p(f"category concentration (WARNING ONLY, never acted on): {share or 'n/a'}"
      f"{'   [WARNING >60%]' if aud['category_warning'] else ''}")
    for w in tbl["warnings"]:
        p(f"warning  : {w}")


def print_explain(sc: ScanPerfScorer, key, out=sys.stdout):
    p = lambda s="": print(s, file=out)
    sel = [t for t in sc.markets
           if ser_of(t) == key or sc.ev(t) == key or root_of(sc.ev(t)) == key or t == key]
    if not sel:
        p(f"--explain {key}: no scan market matches (series, event, event root or ticker)")
        return
    p(f"--explain {key}: {len(sel)} market(s), H={sc.horizon_h}h, "
      f"winsor +/-{WINSOR_CENTS:g}c, tolerance {MARK_TOLERANCE_SECS}s")
    p(f"  {'ticker':<34} {'fill ts (UTC)':<20} {'q':>7} {'px':>6} {'mark':>7} "
      f"{'src':<11} {'mo c/ct':>8}  status")
    sset = set(sel)
    for r in sorted((r for r in sc.fill_rows if r["ticker"] in sset), key=lambda r: r["ts"]):
        ts = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        mark_s = "-" if r["mark"] is None else format(r["mark"], ".1f")
        mo_s = "-" if r["mo"] is None else format(r["mo"], "+.2f")
        p(f"  {r['ticker']:<34} {ts:<20} {r['q']:>+7.0f} {r['px']:>6.1f} "
          f"{mark_s:>7} {r['source']:<11} {mo_s:>8}  {r['status']}")
    p("  episodes:")
    for (t, hour), e in sorted((kv for kv in sc.episodes.items() if kv[0][0] in sset)):
        hs = datetime.fromtimestamp(hour * 3600, timezone.utc).strftime("%Y-%m-%d %H:00")
        p(f"    {t:<34} {hs}  fills {e['fills']:>2}  contracts {e['ct']:>7.0f}  "
          f"mo {e['mo']:+.2f} c/ct (winsorised)")
    tot_mo = sum(sc.market_mo.get(t, 0.0) for t in sel)
    tot_rd = sum(sc.markets[t]["risk_days"] for t in sel)
    tot_rent = sum(sc.market_rent.get(t, 0.0) for t in sel)
    p(f"  totals: mo ${tot_mo:+.2f} (MEASURED)  risk_days {tot_rd:,.1f}  "
      f"rent ${tot_rent:,.2f} (MODELLED)  y_trading {safe_div(tot_mo, tot_rd):+.5f}/$-day")
    for dim in DIM_ORDER:
        vals = {t: sc.dim_value[dim].get(t) for t in sel}
        buck = {t: sc.bucket_of[dim].get(t) for t in sel}
        p(f"  {dim:<7}: " + ", ".join(
            f"{t.split('-')[-1]}={'-' if vals[t] is None else format(vals[t], '.2f')}"
            f"[{buck[t]}]" for t in sorted(sel)))
    for t in sorted(sel):
        adj, code = sc.adj_struct(t, sc.score_basis)
        p(f"  adj_struct {t:<34} {adj:+.5f}  binding={code or 'none'}  "
          f"req@weight1.0 {clip(-adj, 0.0, MAX_REQ):.4f}/day  "
          f"effective bar {SCAN_MIN_ROI + clip(-adj, 0.0, MAX_REQ):.4f}/day")


# -------------------------------------------------------------------- main

def run(argv=None, out=sys.stdout):
    global STATUS_DIR, WORK_DIR
    ap = argparse.ArgumentParser(description="open-scan performance scorer")
    ap.add_argument("--now", action="store_true", help="write the table (default)")
    ap.add_argument("--dry", action="store_true", help="print everything, write NOTHING")
    ap.add_argument("--explain", metavar="SERIES|EVENT")
    ap.add_argument("--asof", metavar="ISO")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--horizon-h", type=int, default=HORIZON_H)
    ap.add_argument("--score-basis", choices=("trading", "blend"), default="trading")
    ap.add_argument("--out", metavar="PATH")
    ap.add_argument("--refit-edges", action="store_true")
    ap.add_argument("--status-dir", metavar="DIR")
    ap.add_argument("--work-dir", metavar="DIR")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args(argv)

    status_dir = a.status_dir or STATUS_DIR
    work_dir = a.work_dir or status_dir
    if a.out:
        # --out relocates the whole private working set, so the write guard
        # keeps its "one directory, three known files" invariant.
        work_dir = os.path.dirname(os.path.abspath(a.out)) or work_dir
    STATUS_DIR = status_dir
    WORK_DIR = work_dir
    asof = parse_iso(a.asof) if a.asof else None
    if asof and asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)

    t_start = time.time()
    sc = ScanPerfScorer(status_dir, work_dir, asof=asof, window_days=a.window_days,
                        horizon_h=a.horizon_h,
                        score_basis=a.score_basis, refit_edges=a.refit_edges,
                        use_cache=not a.no_cache)
    if not a.refit_edges and edges_sha256() != FROZEN_EDGES_SHA256:
        print(f"! cohort edges sha {edges_sha256()} != frozen {FROZEN_EDGES_SHA256}; "
              f"the pre-registered edges were modified. Pass --refit-edges to accept.",
              file=out)
        return 1
    sc.load()
    sc.build()

    if a.explain:
        print_explain(sc, a.explain, out)
        return 0

    tbl = sc.table()
    print_report(sc, tbl, out)

    # ---- writer-side guards: all three abort the write and alert
    recon_delta, mine, sink = sc.reconciliation()
    if abs(recon_delta) > RECON_TOLERANCE_DOLLARS:
        print(f"! RECONCILIATION FAILED: replay ${mine:.2f} vs realized sink "
              f"${sink:.2f} (delta ${recon_delta:+.2f} > ${RECON_TOLERANCE_DOLLARS:.2f}). "
              f"NO FILE WRITTEN.", file=out)
        return 2
    dod = sc.exposure_dod()
    if dod is not None and dod < -EXPOSURE_DROP_ABORT:
        # A BACKWARD --asof re-score covers an earlier window than the
        # history row it would be compared against, so a smaller denominator
        # is arithmetic, not the sink going dark. The skip is announced, never
        # silently applied; a forward or same-day run still trips the guard.
        prev_gen = parse_iso((sc.prev_history_row or {}).get("generated_at"))
        if prev_gen and sc.asof < prev_gen - timedelta(hours=12):
            print(f"  (exposure-drop guard skipped: --asof {iso_z(sc.asof)} is a "
                  f"BACKWARD re-score against a history row from "
                  f"{iso_z(prev_gen)}; d/d {dod*100:+.1f}%)", file=out)
            dod = None
    if dod is not None and dod < -EXPOSURE_DROP_ABORT:
        print(f"! EXPOSURE DROP {dod*100:.1f}% d/d exceeds "
              f"{EXPOSURE_DROP_ABORT*100:.0f}%: a silently shrunken denominator "
              f"inflates every dev. NO FILE WRITTEN.", file=out)
        return 3
    clamped, n_dev = sc.clamped_frac()
    if clamped > CLAMP_STORM_FRAC:
        print(f"! CLAMP STORM: {clamped*100:.1f}% of {n_dev} dev records sit exactly at "
              f"+/-{DIM_CLIP} (sign-flip detector). NO FILE WRITTEN.", file=out)
        return 4

    if a.dry:
        print(f"[--dry] nothing written. cold/warm: cache hits {sc.cache.hits}, "
              f"misses {sc.cache.misses}, total {time.time() - t_start:.1f}s", file=out)
        return 0

    # The private cache is saved only AFTER all three guards pass, so
    # "NO FILE WRITTEN" is literally true of every file this process owns.
    # It was previously written before sc.table(), which cost the abort paths
    # their honesty for the sake of one saved scan on a run that failed.
    # --dry means "write NOTHING", and that includes the cache; it is still
    # READ, so a dry preview after a real run is fast.
    sc.cache.save()
    path = a.out or os.path.join(work_dir, TABLE_NAME)
    atomic_write_json(path, tbl)
    append_jsonl(os.path.join(work_dir, HISTORY_NAME), sc.history_row(tbl, 0))
    print(f"wrote {os.path.basename(path)} ({tbl['limits']['down_ranked']} down_rank, "
          f"{tbl['limits']['barred_series'] + tbl['limits']['barred_events']} bar); "
          f"history row appended  [{time.time() - t_start:.1f}s]", file=out)
    return 0


def main(argv=None):
    try:
        return run(argv)
    except WriteGuardError as e:
        print(f"! write guard: {e}", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
