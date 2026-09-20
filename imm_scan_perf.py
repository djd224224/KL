#!/usr/bin/env python3
"""
imm_scan_perf.py — the OFFLINE SCORER of the open-scan performance loop.

Jack (2026-09-15 / 09-18 / 09-20): "make open scan pick up new markets or
not based on historical ROI of those market families ... If it's a new
event listed then use the family historical ROI but if it's the same event
that is just relisted for new dates (eg gas dailies, AI token share, etc)
then use the specific ROI of that event, not the family historical ROI."

It re-derives, from the IMM sinks alone, what the open-scan tier ACTUALLY
earned per dollar at risk — the historical ROI — for three nested units:

  events    keyed by EVENT ROOT (the event ticker minus its trailing date
            segment, KXTOKENUSE-26SEP28 -> KXTOKENUSE): a re-listed weekly,
            or a new strike on a dated event, is judged on that event's OWN
            history
  series    the ticker prefix (KXCPIYOY, KXAXP, ...): a NEW event root under
            a known series is judged on the series' history
  families  grouped series that are one market family (FAMILY_GROUPS:
            Fiscal.ai KPIs, the AAA state gas dailies, the CPI prints): a
            NEW series inside a known family is judged on the family's
            history. Every other series is its own family.

and writes one table (``scan_perf.json``) that the bot hot-reloads. The bot
looks a candidate up in that order — the MOST SPECIFIC unit with enough
history wins — and (a) refuses to admit it while the unit's historical ROI
is under the block threshold, (b) blends the unit's historical ROI into the
ranking ROI in proportion to the evidence behind it.

  roi_hist = (rent + realized + mtm) / risk_days        $/day per $ at risk

  rent      MEASURED credits from reward_credits.csv where the ledger covers
            a program period, else the same MODELLED est_frac x pool accrual
            the walk's own ROI ranks on (with the MEASURED $1.00 per market
            per period floor). ONE basis per period, never both: EST and
            CREDITED are never summed (memory: neither contains the other).
  realized  MEASURED — an independent avg-cost replay of fills + settlements
            (reproduces the bot's realized sink to the cent; the sink itself
            is only the comparison side of the self-check).
  mtm       the open position at the bot's own last mark (a MODELLED mark;
            positions ride to settlement, so this is the honest current
            value of what the tier still holds).
  risk_days sum over cycles of (resting collateral + inventory) x dt — the
            exposure the walk's est_collateral_dollars approximates.

Verdicts: `insufficient` under MIN_RISK_DAYS of history (the bot treats the
unit as unknown = today's behaviour), `block` when roi_hist < BLOCK_ROI,
else `allow`. Labels (Jack, "a model is not a measurement"): MEASURED =
re-derived from fills / settlements / marks / cycle_log / ledger; MODELLED =
the bot's accrual, a mid-based mark, or a replay.

WHAT IT MUST NEVER DO
  * write anything into STATUS_DIR other than its own three files (the live
    bot and four daily tasks own that directory);
  * call ``imm_reward_recon.rebuild_estimates()`` — that rewrites the live
    ``reward_est_cache.json`` unconditionally and would fight the 07:50
    ``KL imm program-history`` job. The accrual integral is re-implemented
    here against a PRIVATE cache;
  * ship a table it cannot reconcile: the replay must match the realized
    sink to the cent over complete UTC days, and the tier's $-days must not
    have collapsed >30% day over day (a sink gone dark), else NO FILE.

Usage
    python imm_scan_perf.py --dry
    python imm_scan_perf.py --now
    python imm_scan_perf.py --explain KXCPIYOY      (series, event root, family or ticker)
    python imm_scan_perf.py --status-dir <dir> --work-dir <dir> --dry

Exit codes: 0 wrote (or --dry OK), 1 internal error, 2 reconciliation
failed (no file), 3 exposure-drop abort (no file).
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
# never needs a bot restart.

WINDOW_DAYS = 45                    # trailing history the ROI is measured over
HORIZON_H = 24                      # markout horizon (a DIAGNOSTIC, not the verdict)
MATURITY_SLACK_SECS = 900           # a fill enters the markout sample at t + H + 15min
MARK_TOLERANCE_SECS = 900           # same dt grain as imm_reward_recon
MAX_DT = 900.0                      # accrual integral cap (restarts cannot bill hours)
WINSOR_CENTS = 40.0                 # markout winsor (diagnostic)
RENT_FACTOR = 1.0
RENT_FACTOR_SOURCE = "default_n2"   # 2 MEASURED scan credits: no basis to haircut
PAYOUT_FLOOR = 1.00                 # MEASURED: min Liquidity credit is exactly $1.00
CREDIT_LAG_DAYS = 2                 # a period is measurable 2d before the newest credit
CREDIT_WINDOW_DAYS = 4              # ledger rows land 1-2d after a period end
# THE RULE (Jack 2026-09-18/20). A unit is judged only once it carries
# MIN_RISK_DAYS of history: 100 $-days is four market-days at the tier's
# typical ~$25 of resting collateral, i.e. about one weekly program period
# of one market, or a day and a half of a three-strike event. Under it the
# verdict is `insufficient` and the bot treats the unit as unknown (today's
# behaviour). At or above it, roi_hist < BLOCK_ROI blocks NEW admissions
# from the unit and a positive history is blended into the ranking ROI by
# the bot (IMM_SCAN_PERF_BLEND_M). 0.0/day asks exactly Jack's question:
# has this family earned its rent net of what it lost trading?
MIN_RISK_DAYS = 100.0
BLOCK_ROI = 0.0
# A block carries a short `until` so a scorer that dies cannot hold a block
# forever. The bot ages the WHOLE table out at IMM_SCAN_PERF_MAX_AGE_H (48h)
# anyway; the daily 07:55 run re-issues every live block.
BLOCK_TTL_HOURS = 60
# roi_hist is clipped before it is published: a unit at the 100 $-day floor
# carrying one settled $60 loss reads -0.6/day, which the bot's blend must
# see as "very bad" and not as an arithmetic accident that swamps the model.
ROI_CLIP_LO = -2.0
ROI_CLIP_HI = 1.0
# GROUPED FAMILIES: series that are one market family and share one history
# when a NEW series of the family lists (Jack: "family historical ROI").
# Every other series is its own family (family == series). A member is a
# series whose name matches `regex`, or (Fiscal.ai) whose cached
# scan_series_meta verdict carries fiscal=true. The rules are emitted into
# the file so the bot classifies a never-seen series the same way.
FAMILY_GROUPS = (
    {"name": "FISCAL_KPI", "fiscal": True,
     "note": "Fiscal.ai company-KPI weeklies (KXAXP, KXAAL, KXCCLA, ...)"},
    {"name": "AAAGAS_STATE_DAILY", "regex": r"^KXAAAGASD[A-Z]{2}$",
     "note": "AAA per-state gas dailies"},
    {"name": "CPI", "regex": r"^KXCPI(CORE)?(YOY)?$",
     "note": "BLS CPI prints: headline/core x MoM/YoY"},
)
MAX_RECORDS = 2000                  # unit records in the file; the bot refuses more
MAX_MARKET_RECORDS = 600            # the `markets` block (never read by the bot)
MAX_EVENTS_LISTED = 40              # dated events listed per unit record
RECON_TOLERANCE_DOLLARS = 1.00
EXPOSURE_DROP_ABORT = 0.30
SCAN_MIN_ROI = 0.05                 # incentive_mm.SCAN_MIN_ROI (Jack 9/13, seat stays EMPTY)

QUOTE_GRID_SECS = 300               # cycle samples are kept on a 5-min grid

MON = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
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
                 horizon_h=HORIZON_H, use_cache=True, log=print):
        self.status_dir = status_dir
        self.work_dir = work_dir
        self.asof = asof or datetime.now(timezone.utc)
        self.window_days = window_days
        self.horizon_h = horizon_h
        self.use_cache = use_cache
        self.log = log
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
        self._build_totals()
        self._build_units()
        self.timings["build_secs"] = round(time.time() - t0, 2)

    def _prep_spreads(self):
        """Per-series and tier MEDIAN two-sided spreads over each market's
        first 6h of quotes: the fallbacks the one-sided mark tier widens by
        when a ticker has no two-sided sample of its own (SPEC 2.4 tier 3)."""
        self._series_spread = {}
        self._tier_spread = None
        per_series = defaultdict(list)
        all_sp = []
        for t, q in self.quotes.items():
            ts, bids, asks = q
            if not ts:
                continue
            t0 = ts[0]
            for j, tt in enumerate(ts):
                if tt > t0 + 6 * 3600:
                    break
                b, a = bids[j], asks[j]
                if b is not None and a is not None and a > b:
                    per_series[ser_of(t)].append(a - b)
                    all_sp.append(a - b)
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


    # -------------------------------------------------- units (the verdicts)

    def _build_totals(self):
        self.risk_total = sum(m["risk_days"] for m in self.markets.values())
        self.risk_total_50 = sum(m["risk_days_at50c"] for m in self.markets.values())
        self.mo_total = sum(self.market_mo.values())
        self.rent_total = sum(self.market_rent.values())        # MODELLED, floored
        self.realized_total = sum(m["realized_dollars"] for m in self.markets.values())
        self.mtm_total = sum(m["mtm_dollars"] for m in self.markets.values())
        evs = {self.ev(t) for t in self.markets}
        # per-period basis: a MEASURED credit replaced the estimate where the
        # ledger covers the period, else the floored estimate (never both)
        self.rent_used_total = sum(self.event_rent.get(e, 0.0) for e in evs)
        self.rent_measured_total = sum(self.event_rent_measured.get(e, 0.0) for e in evs)
        self.net_total = self.rent_used_total + self.realized_total + self.mtm_total

    def family_of(self, series: str) -> str:
        """The grouped family a series belongs to (FAMILY_GROUPS), else the
        series itself. A Fiscal.ai member is recognised by the `fiscal` flag
        the bot cached on its scan_series_meta verdict; the others by name."""
        meta = self.series_meta.get(series) or {}
        for g in FAMILY_GROUPS:
            if g.get("fiscal") and bool(meta.get("fiscal")):
                return g["name"]
            rx = g.get("regex")
            if rx and re.match(rx, series):
                return g["name"]
        return series

    @staticmethod
    def _roi(num, risk):
        return clip(safe_div(num, risk), ROI_CLIP_LO, ROI_CLIP_HI) if risk > 0 else 0.0

    def _unit_rec(self, kind: str, key: str, tickers) -> dict:
        """One unit's historical-ROI record (event root / series / family).

        roi_hist = (rent_used + realized + mtm) / risk_days. rent_used is the
        per-period basis (MEASURED credit where the ledger covers the period,
        else the floored MODELLED estimate); realized is MEASURED; mtm is the
        bot's own last mark on what the unit still holds. Two companion
        rates are published so nobody has to trust the blend: trading-only
        (no rent at all) and measured-only (credits that replaced an
        estimate + realized; no estimate, no mark)."""
        tickers = sorted(tickers)
        tset = set(tickers)
        evs = sorted({self.ev(t) for t in tickers})
        mk = self.markets
        risk = sum(mk[t]["risk_days"] for t in tickers)
        risk50 = sum(mk[t]["risk_days_at50c"] for t in tickers)
        realized = sum(mk[t]["realized_dollars"] for t in tickers)
        mtm = sum(mk[t]["mtm_dollars"] for t in tickers)
        rent_mod = sum(self.market_rent.get(t, 0.0) for t in tickers)
        rent_used = sum(self.event_rent.get(e, 0.0) for e in evs)
        rent_meas = sum(self.event_rent_measured.get(e, 0.0) for e in evs)
        credited_all = sum(self.event_credited_all.get(e, 0.0) for e in evs)
        mo = sum(self.market_mo.get(t, 0.0) for t in tickers)
        ct = sum(self.market_ct_matured.get(t, 0.0) for t in tickers)
        fills = sum(self.market_fills.get(t, 0) for t in tickers)
        contracts = sum(abs(r["q"]) for r in self.fill_rows if r["ticker"] in tset)
        days = set()
        for t in tickers:
            days |= self.market_days.get(t, set())
        firsts = [mk[t]["first_cycle"] for t in tickers if mk[t]["first_cycle"] is not None]
        lasts = [mk[t]["last_cycle"] for t in tickers if mk[t]["last_cycle"] is not None]
        net = rent_used + realized + mtm
        roi = self._roi(net, risk)
        roi_tr = self._roi(realized + mtm, risk)
        roi_meas = self._roi(rent_meas + realized, risk)
        if risk < MIN_RISK_DAYS:
            verdict = "insufficient"
            reason = f"{risk:.0f} < {MIN_RISK_DAYS:.0f} $-days of history"
        elif roi < BLOCK_ROI:
            verdict = "block"
            reason = (f"roi_hist {roi:+.4f}/day < {BLOCK_ROI:+.2f} on "
                      f"{risk:.0f} $-days (net ${net:+.2f})")
        else:
            verdict = "allow"
            reason = f"roi_hist {roi:+.4f}/day on {risk:.0f} $-days (net ${net:+.2f})"
        rec = {
            "kind": kind, "key": key,
            "n_markets": len(tickers), "n_events": len(evs),
            "events": evs[:MAX_EVENTS_LISTED],
            "n_fills": fills, "contracts_filled": round(contracts, 2),
            "n_fill_days": len(days),
            "first_cycle": (iso_z(datetime.fromtimestamp(min(firsts), timezone.utc))
                            if firsts else None),
            "last_cycle": (iso_z(datetime.fromtimestamp(max(lasts), timezone.utc))
                           if lasts else None),
            "risk_days": round(risk, 2), "risk_days_at50c": round(risk50, 2),
            "rent_modelled": round(rent_mod, 4),
            "rent_used": round(rent_used, 4),
            "rent_measured_dollars": round(rent_meas, 4),
            "rent_basis": self._unit_rent_basis(tickers),
            "credited_measured": round(credited_all, 4),
            "realized_dollars": round(realized, 4),
            "mtm_dollars": round(mtm, 4),
            "net_dollars": round(net, 4),
            "roi_hist": round(roi, 6),
            "roi_hist_trading": round(roi_tr, 6),
            "roi_hist_measured": round(roi_meas, 6),
            "markout_c_per_ct_24h": round(safe_div(100.0 * mo, ct), 3),
            "verdict": verdict, "reason": reason,
        }
        if verdict == "block":
            rec["until"] = iso_z(self.asof + timedelta(hours=BLOCK_TTL_HOURS))
        return rec

    def _build_units(self):
        """events (by ROOT), series and grouped families. The bot resolves a
        candidate root -> series -> family and uses the first unit whose
        verdict is not `insufficient` (lookup_unit mirrors it here)."""
        by_root = defaultdict(list)
        by_series = defaultdict(list)
        by_family = defaultdict(list)
        self.family_map = {}
        for t in self.markets:
            s = ser_of(t)
            fam = self.family_map.get(s)
            if fam is None:
                fam = self.family_map[s] = self.family_of(s)
            by_root[root_of(self.ev(t))].append(t)
            by_series[s].append(t)
            if fam != s:
                by_family[fam].append(t)
        self.event_recs = {}
        for key, tickers in sorted(by_root.items()):
            rec = self._unit_rec("event", key, tickers)
            rec["series"] = ser_of(tickers[0])
            rec["family"] = self.family_map.get(rec["series"], rec["series"])
            self.event_recs[key] = rec
        self.series_recs = {}
        for key, tickers in sorted(by_series.items()):
            rec = self._unit_rec("series", key, tickers)
            rec["family"] = self.family_map.get(key, key)
            rec["roots"] = sorted({root_of(self.ev(t)) for t in tickers})[:MAX_EVENTS_LISTED]
            self.series_recs[key] = rec
        self.family_recs = {}
        for g in FAMILY_GROUPS:
            name = g["name"]
            tickers = by_family.get(name, [])
            rec = self._unit_rec("family", name, tickers)
            rec["members"] = sorted({ser_of(t) for t in tickers})
            rec["match"] = {k: v for k, v in g.items() if k in ("regex", "fiscal")}
            rec["note"] = g.get("note", "")
            self.family_recs[name] = rec

    def lookup_unit(self, ticker: str):
        """The bot's hierarchy, mirrored: event root -> series -> family; the
        most specific unit with enough history wins. (kind, key, rec) or
        (None, None, None) when nothing has MIN_RISK_DAYS yet."""
        s = ser_of(ticker)
        root = root_of(self.ev(ticker))
        fam = self.family_map.get(s, s)
        for kind, key, recs in (("event", root, self.event_recs),
                                ("series", s, self.series_recs),
                                ("family", fam, self.family_recs)):
            rec = recs.get(key)
            if rec and rec["verdict"] != "insufficient":
                return kind, key, rec
        return None, None, None

    def _unit_rent_basis(self, tickers):
        bases = set()
        for t in tickers:
            bases.add(self._rent_basis(self.market_rent_periods.get(t, [])))
        if bases == {"credited"}:
            return "credited"
        if "credited" in bases or "mixed_by_period" in bases:
            return "mixed_by_period"
        return "est_floored"

    # ------------------------------------------------------ counterfactual

    def counterfactual(self):
        """[IN-SAMPLE, MODELLED COUNTERFACTUAL] — never a measurement. The
        markets admitted in the window whose unit, judged on the WHOLE
        window, is now blocked. In-sample because the unit's history at each
        admission was shorter than it is now."""
        rows = []
        for t in sorted(self.markets):
            kind, key, rec = self.lookup_unit(t)
            if rec and rec["verdict"] == "block":
                rows.append((t, kind, key))
        mk = self.markets
        return {
            "n_markets": len(rows), "n_all": len(self.markets),
            "trading": sum(mk[t]["realized_dollars"] + mk[t]["mtm_dollars"]
                           for t, _k, _y in rows),
            "rent_modelled": sum(self.market_rent.get(t, 0.0) for t, _k, _y in rows),
            "risk_days": sum(mk[t]["risk_days"] for t, _k, _y in rows),
            "markets": [t for t, _k, _y in rows],
            "units": sorted({f"{k}:{y}" for _t, k, y in rows}),
        }


    def _unit_measured_frac(self, tickers):
        evs = {self.ev(t) for t in tickers}
        meas = sum(self.event_rent_measured.get(e, 0.0) for e in evs)
        tot = sum(self.event_rent.get(e, 0.0) for e in evs)
        return safe_div(meas, tot)


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


    # --------------------------------------------------------------- output

    # --------------------------------------------------------------- output

    def _shed_insufficient(self, recs: dict, n_over: int) -> int:
        """Bound the file for the bot's record cap by dropping `insufficient`
        records (pure no-ops for the bot), smallest history first."""
        if n_over <= 0:
            return 0
        cand = sorted((k for k, r in recs.items() if r["verdict"] == "insufficient"),
                      key=lambda k: recs[k]["risk_days"])
        for k in cand[:n_over]:
            recs.pop(k)
        return min(len(cand), n_over)

    def table(self):
        cov_markets = len(self.markets)
        cov_events = len({self.ev(t) for t in self.markets})
        cov_series = len({ser_of(t) for t in self.markets})
        recon_delta, _mine, _sink = self.reconciliation()
        dod = self.exposure_dod()
        cf = self.counterfactual()

        events_out = dict(self.event_recs)
        series_out = dict(self.series_recs)
        families_out = dict(self.family_recs)
        n_units = len(events_out) + len(series_out) + len(families_out)
        if n_units > MAX_RECORDS:
            over = n_units - MAX_RECORDS
            shed = self._shed_insufficient(events_out, over)
            shed += self._shed_insufficient(series_out, over - shed)
            n_units -= shed
            self._warn(f"{shed} insufficient record(s) dropped so the bot's record "
                       f"count ({n_units}) stays under MAX_RECORDS={MAX_RECORDS}")
            if n_units > MAX_RECORDS:
                self._warn(f"! still {n_units} > {MAX_RECORDS} records after shedding "
                           f"every insufficient one: the BOT WILL REFUSE THIS FILE WHOLE")

        n_block = {
            "events": sum(1 for r in events_out.values() if r["verdict"] == "block"),
            "series": sum(1 for r in series_out.values() if r["verdict"] == "block"),
            "families": sum(1 for r in families_out.values() if r["verdict"] == "block"),
        }
        recent = self.recent_scan_groups
        unmatched = []
        for k, r in events_out.items():
            if r["verdict"] == "block" and k not in recent:
                unmatched.append(f"event:{k}")
        for k, r in series_out.items():
            if r["verdict"] == "block" and k not in recent:
                unmatched.append(f"series:{k}")
        for k, r in families_out.items():
            if r["verdict"] == "block" and not (set(r.get("members") or []) & recent):
                unmatched.append(f"family:{k}")

        notes = list(self.notes) + [
            "roi_hist = (rent_used + realized + mtm) / risk_days, in $/day per $ at "
            "risk -- the same units as the bot's _raw_roi",
            "rent_used is ONE basis per program period: a MEASURED ledger credit "
            "where the ledger covers the period, else the floored MODELLED accrual; "
            "EST and CREDITED are never summed",
            "MARKET-level rent is ALWAYS MODELLED: ledger rows are event-keyed, so a "
            "credit replaces an estimate at EVENT level only. rent_modelled and "
            "credited_measured on one record are NOT two halves of a total",
            "realized is MEASURED (independent avg-cost replay of fills + "
            "settlements); mtm is the bot's own last mark on open positions (a "
            "MODELLED mark); roi_hist_trading and roi_hist_measured are published "
            "beside roi_hist so the blend is never the only number",
            "verdicts: insufficient (< MIN_RISK_DAYS of history; the bot treats the "
            "unit as unknown), block (roi_hist < BLOCK_ROI), allow",
            "'events' records are keyed by EVENT ROOT (the event ticker minus its "
            "trailing date segment); the bot resolves a candidate root -> series -> "
            "family and uses the most specific unit that is not insufficient",
            "families are FAMILY_GROUPS only (fiscal flag or name regex, emitted "
            "under match); every other series is its own family",
            "markout is a DIAGNOSTIC (INCENTIVE_MM_STRATEGY s6 mark hierarchy, 4 "
            "tiers, no carry-forward); no verdict reads it",
            "the avg-cost replay is independent of the REALIZED SINK but NOT of the "
            "bot's cost basis on the tickers listed in tier.recon_seeded_tickers "
            "(seeded from the first fill's pos_before/avg_before)",
            "NO time decay: every observation in the window has weight 1",
        ]

        markets = {}
        for t, m in sorted(self.markets.items(), key=lambda kv: -kv[1]["risk_days"]):
            if len(markets) >= MAX_MARKET_RECORDS:
                self._warn(f"markets block capped at {MAX_MARKET_RECORDS} records "
                           f"by risk_days ({len(self.markets)} scored)")
                break
            rent = self.market_rent.get(t, 0.0)
            net = rent + m["realized_dollars"] + m["mtm_dollars"]
            markets[t] = {
                "series": ser_of(t), "event": self.ev(t),
                "root": root_of(self.ev(t)),
                "risk_days": round(m["risk_days"], 3),
                "rent_modelled": round(rent, 4),
                "realized_dollars": round(m["realized_dollars"], 4),
                "mtm_dollars": round(m["mtm_dollars"], 4),
                "net_dollars_modelled_rent": round(net, 4),
                "roi_hist_modelled_rent": round(self._roi(net, m["risk_days"]), 6),
                "pos": round(m["pos"], 3),
                "avg_cents": round(m["avg_cents"], 3),
                "mark_cents": (None if m["mark_cents"] is None else round(m["mark_cents"], 2)),
                "fills": self.market_fills.get(t, 0),
                "adopted_capped": bool(m["adopted_capped"]),
            }

        ct = self.cov["contracts_matured"]
        out = {
            "version": 2,
            "generated_at": iso_z(self.asof),
            "generated_by": f"imm_scan_perf.py@{_git_sha()}",
            "window": {
                "start": iso_z(self.window_start), "end": iso_z(self.asof),
                "days": self.window_days,
                "horizon_hours": self.horizon_h,
                "maturity_cutoff": iso_z(self.maturity_cutoff),
            },
            "params": {
                "min_risk_days": MIN_RISK_DAYS, "block_roi": BLOCK_ROI,
                "block_ttl_hours": BLOCK_TTL_HOURS,
                "roi_clip_lo": ROI_CLIP_LO, "roi_clip_hi": ROI_CLIP_HI,
                "rent_factor": RENT_FACTOR, "rent_factor_source": RENT_FACTOR_SOURCE,
                "payout_floor": PAYOUT_FLOOR,
                "winsor_cents": WINSOR_CENTS, "mark_tolerance_secs": MARK_TOLERANCE_SECS,
                "family_groups": [dict(g) for g in FAMILY_GROUPS],
            },
            "coverage": {
                "markets": cov_markets, "events": cov_events, "series": cov_series,
                "fills": self.cov["fills"], "fills_matured": self.cov["fills_matured"],
                "fills_pending": self.cov["fills_pending"],
                "fills_unmarked": self.cov["fills_unmarked"],
                "fills_flat_through": self.cov.get("fills_flat_through", 0),
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
                "roi_hist": round(self._roi(self.net_total, self.risk_total), 6),
                "roi_hist_trading": round(
                    self._roi(self.realized_total + self.mtm_total, self.risk_total), 6),
                "roi_hist_measured": round(
                    self._roi(self.rent_measured_total + self.realized_total,
                              self.risk_total), 6),
                "net_dollars": round(self.net_total, 2),
                "realized_dollars": round(self.realized_total, 2),
                "mtm_dollars": round(self.mtm_total, 2),
                "rent_used_dollars": round(self.rent_used_total, 2),
                "rent_modelled_dollars": round(self.rent_total, 2),
                "rent_measured_dollars": round(self.rent_measured_total, 2),
                "rent_basis": self._unit_rent_basis(list(self.markets)),
                "rent_measured_frac": round(self._unit_measured_frac(list(self.markets)), 4),
                "credited_measured_dollars": round(sum(self.event_credited_all.values()), 2),
                "ledger_newest_credit_date": self.newest_credit_date,
                "ledger_stale_days": self.ledger_stale_days,
                "markout_c_per_ct_24h": round(safe_div(100.0 * self.mo_total, ct), 3),
                "markout_horizons": {str(h): v for h, v
                                     in sorted(self.markout_horizons.items())},
                "mo_dollars": round(self.mo_total, 2),
                "mark_source_mix": self.mark_source_mix,
                "mark_source_detail": self.mark_source_detail,
                "reconciliation_delta_dollars": recon_delta,
                "recon_seeded_tickers": self.seeded_tickers,
                "recon_seeded_frac": round(
                    safe_div(len(self.seeded_tickers), len(self.markets)), 4),
                "risk_days_dod_change": (None if dod is None else round(dod, 4)),
            },
            "events": events_out,
            "series": series_out,
            "families": families_out,
            "markets": markets,
            "limits": {
                "blocked_events": n_block["events"],
                "blocked_series": n_block["series"],
                "blocked_families": n_block["families"],
                "n_units": n_units, "max_records": MAX_RECORDS,
            },
            "audit": {
                "counterfactual_label": "IN-SAMPLE, MODELLED COUNTERFACTUAL",
                "counterfactual": {
                    "n_markets_in_blocked_units": cf["n_markets"],
                    "n_markets": cf["n_all"],
                    "trading_dollars": round(cf["trading"], 2),
                    "rent_modelled_dollars": round(cf["rent_modelled"], 2),
                    "risk_days": round(cf["risk_days"], 2),
                    "units": cf["units"],
                },
                "diagnostics": {
                    "seeded_tickers": self.seeded_tickers,
                    "cycle_files_scanned": self.n_cycle_files,
                    "cache_hits": self.cache.hits, "cache_misses": self.cache.misses,
                    "timings": self.timings,
                },
            },
            "unmatched_block_keys": sorted(unmatched),
            "warnings": self.warnings,
            "notes": notes,
        }
        return out

    def history_row(self, tbl, exit_code):
        tier, cov, lim = tbl["tier"], tbl["coverage"], tbl["limits"]
        return {
            "generated_at": tbl["generated_at"], "window_days": self.window_days,
            "risk_days": cov["risk_days"], "risk_days_at50c": cov["risk_days_at50c"],
            "net_dollars": tier["net_dollars"],
            "realized_dollars": tier["realized_dollars"],
            "mtm_dollars": tier["mtm_dollars"],
            "rent_used": tier["rent_used_dollars"],
            "rent_modelled": tier["rent_modelled_dollars"],
            "rent_measured": tier["rent_measured_dollars"],
            "credited_measured": tier["credited_measured_dollars"],
            "roi_hist": tier["roi_hist"],
            "roi_hist_trading": tier["roi_hist_trading"],
            "roi_hist_measured": tier["roi_hist_measured"],
            "markout_c_per_ct_24h": tier["markout_c_per_ct_24h"],
            "fills": cov["fills"], "episodes": cov["episodes"],
            "episodes_matured": cov["episodes_matured"],
            "mark_source_detail": tier["mark_source_detail"],
            "mark_source_mix": tier["mark_source_mix"],
            "unmarked_frac": round(safe_div(cov["fills_unmarked"], cov["fills_matured"]), 4),
            "n_block_events": lim["blocked_events"],
            "n_block_series": lim["blocked_series"],
            "n_block_families": lim["blocked_families"],
            "n_units": lim["n_units"],
            "reconciliation_delta": tier["reconciliation_delta_dollars"],
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

def _fmt_unit_row(kind, key, r):
    basis = {"est_floored": "est", "credited": "cred", "mixed_by_period": "mixed"}.get(
        r["rent_basis"], r["rent_basis"])
    until = r.get("until", "")
    return (f"  {kind:<7} {key:<30} {r['n_markets']:>4} {r['n_events']:>3} "
            f"{r['risk_days']:>9,.1f} {r['rent_used']:>+8.2f} {basis:<5} "
            f"{r['realized_dollars']:>+9.2f} {r['mtm_dollars']:>+8.2f} "
            f"{r['roi_hist']:>+8.4f} {r['verdict']:<12}"
            + (f" until {until[:16]}" if until else ""))


def print_report(sc: ScanPerfScorer, tbl, out=sys.stdout):
    p = lambda s="": print(s, file=out)
    cov, tier, lim = tbl["coverage"], tbl["tier"], tbl["limits"]
    p(f"scan-perf {tbl['generated_at']}  window {sc.window_days}d  no decay  "
      f"rule: block a unit under {BLOCK_ROI:+.2f}/day once it has "
      f">= {MIN_RISK_DAYS:.0f} $-days of history")
    p(f"coverage : {cov['markets']} mkts / {cov['events']} events / {cov['series']} series"
      f" | {cov['fills']} fills -> {cov['episodes']} episodes "
      f"({cov['episodes_matured']} matured, {cov['fills_pending']} pending fills, "
      f"{cov['fills_unmarked']} unmarked, {cov['fills_flat_through']} flat-through)")
    p(f"           risk_days {cov['risk_days']:,.1f} (at50c {cov['risk_days_at50c']:,.1f})"
      f"  adopted-capped: {', '.join(cov['adopted_capped_tickers']) or 'none'}")
    m = tier["mark_source_mix"]
    md = tier["mark_source_detail"]
    p(f"marks    : two_sided {m['two_sided']:.2f} | one_sided {m['one_sided']:.2f} "
      f"| settlement {m['settlement']:.2f}   (of which cycle book "
      f"{md['cycle_two_sided']:.2f}, marks sink {md['sink_mark']:.2f})")
    p(f"tier     : roi_hist {tier['roi_hist']:+.5f} /$-day  =  "
      f"(rent ${tier['rent_used_dollars']:,.2f} [{tier['rent_basis']}] "
      f"+ realized ${tier['realized_dollars']:,.2f} [MEASURED] "
      f"+ mtm ${tier['mtm_dollars']:,.2f} [MODELLED mark]) / {cov['risk_days']:,.1f} $-days")
    p(f"           trading-only {tier['roi_hist_trading']:+.5f} /$-day   "
      f"measured-only {tier['roi_hist_measured']:+.5f} /$-day "
      f"(credits that replaced an estimate + realized; no estimate, no mark)")
    p(f"           rent MODELLED ${tier['rent_modelled_dollars']:,.2f} floored "
      f"(RENT_FACTOR {RENT_FACTOR}); MEASURED credits "
      f"${tier['credited_measured_dollars']:,.2f} on {cov['credited_events']} of "
      f"{cov['events']} events [never summed with rent]; ledger "
      f"{tier['ledger_stale_days'] if tier['ledger_stale_days'] is not None else '?'}d old")
    hz = tier["markout_horizons"]
    p(f"           markout (DIAGNOSTIC, MEASURED): "
      + "  ".join(f"@{hz[k]['horizon_h']}h {hz[k]['c_per_ct']:+.2f} c/ct on "
                  f"{hz[k]['n_fills']} fills" for k in sorted(hz, key=lambda k: int(k))))
    dod = tier["risk_days_dod_change"]
    p(f"reconciliation vs realized sink: "
      f"{'OK' if abs(tier['reconciliation_delta_dollars']) <= RECON_TOLERANCE_DOLLARS else 'FAILED'}"
      f" (delta ${tier['reconciliation_delta_dollars']:+.2f})   "
      f"risk_days d/d: {'n/a' if dod is None else f'{dod*100:+.1f}%'}   "
      f"recon-seeded {tier['recon_seeded_frac']*100:.1f}%")
    p("units (event ROOT -> series -> family; a re-listed event is judged on its ROOT, "
      "a new root on its SERIES, a new series on its FAMILY):")
    p(f"  {'kind':<7} {'key':<30} {'mkts':>4} {'ev':>3} {'$-days':>9} {'rent$':>8} "
      f"{'basis':<5} {'realized$':>9} {'mtm$':>8} {'roi/day':>8} verdict")
    # An event root that IS its series (KXCPIYOY-26NOV -> root KXCPIYOY ==
    # series KXCPIYOY) carries the same markets and the same numbers as the
    # series record; print it once, as the series. The file keeps both (the
    # bot's lookup tries the root first).
    units = ([("event", k, r) for k, r in tbl["events"].items()
              if k not in tbl["series"]]
             + [("series", k, r) for k, r in tbl["series"].items()]
             + [("family", k, r) for k, r in tbl["families"].items()])
    blocks = sorted((u for u in units if u[2]["verdict"] == "block"),
                    key=lambda u: u[2]["roi_hist"])
    allows = sorted((u for u in units if u[2]["verdict"] == "allow"),
                    key=lambda u: -u[2]["roi_hist"])
    insuff = sorted((u for u in units if u[2]["verdict"] == "insufficient"),
                    key=lambda u: -u[2]["risk_days"])
    for u in blocks:
        p(_fmt_unit_row(*u))
    for u in allows:
        p(_fmt_unit_row(*u))
    shown = 12
    for u in insuff[:shown]:
        p(_fmt_unit_row(*u))
    if len(insuff) > shown:
        p(f"  ... {len(insuff) - shown} more insufficient unit(s) (< {MIN_RISK_DAYS:.0f} "
          f"$-days) not listed; all are neutral for the bot")
    p("families (grouped):")
    for k, r in tbl["families"].items():
        p(f"  {k:<20} members {len(r.get('members') or [])}: "
          f"{', '.join(r.get('members') or []) or '-'}  -> {r['verdict']} "
          f"({r['reason']})")
    p(f"blocks   : {lim['blocked_events']} event root(s), {lim['blocked_series']} "
      f"series, {lim['blocked_families']} family(ies)"
      + (f"; unmatched (no candidate seen in 24h): "
         f"{', '.join(tbl['unmatched_block_keys'])}" if tbl["unmatched_block_keys"] else ""))
    cf = tbl["audit"]["counterfactual"]
    p(f"counterfactual [IN-SAMPLE, MODELLED COUNTERFACTUAL]: "
      f"{cf['n_markets_in_blocked_units']} of {cf['n_markets']} markets admitted in the "
      f"window sit in a unit that is blocked NOW; their trading "
      f"${cf['trading_dollars']:+,.2f} (MEASURED + mark), MODELLED rent "
      f"${cf['rent_modelled_dollars']:,.2f}, {cf['risk_days']:,.1f} $-days")
    for w in tbl["warnings"]:
        p(f"warning  : {w}")


def print_explain(sc: ScanPerfScorer, key, out=sys.stdout):
    p = lambda s="": print(s, file=out)
    sel = [t for t in sc.markets
           if ser_of(t) == key or sc.ev(t) == key or root_of(sc.ev(t)) == key
           or sc.family_map.get(ser_of(t)) == key or t == key]
    if not sel:
        p(f"--explain {key}: no scan market matches (series, event, event root, "
          f"family or ticker)")
        return
    for kind, recs in (("event", sc.event_recs), ("series", sc.series_recs),
                       ("family", sc.family_recs)):
        r = recs.get(key)
        if r:
            p(f"--explain {key} [{kind}]: {r['verdict']} -- {r['reason']}")
            p(f"  rent_used ${r['rent_used']:+.2f} [{r['rent_basis']}]  "
              f"realized ${r['realized_dollars']:+.2f} [MEASURED]  "
              f"mtm ${r['mtm_dollars']:+.2f} [MODELLED mark]  "
              f"risk_days {r['risk_days']:,.1f}  ->  roi_hist {r['roi_hist']:+.5f}/day "
              f"(trading-only {r['roi_hist_trading']:+.5f}, "
              f"measured-only {r['roi_hist_measured']:+.5f})")
    if key in sc.markets:
        # a TICKER: say which unit judges it under the bot's hierarchy
        kind, k, rec = sc.lookup_unit(key)
        if rec is None:
            p(f"--explain {key} [ticker]: no unit with >= {MIN_RISK_DAYS:.0f} $-days "
              f"of history judges it (root/series/family all insufficient) -> the "
              f"bot uses the model alone")
        else:
            p(f"--explain {key} [ticker]: judged by {kind}:{k} -> {rec['verdict']} "
              f"-- {rec['reason']}")
    p(f"  {len(sel)} market(s):")
    p(f"  {'ticker':<34} {'first cycle':<17} {'$-days':>8} {'rent$':>7} {'real$':>8} "
      f"{'mtm$':>8} {'roi/day':>8} {'fills':>5} {'pos':>6}")
    for t in sorted(sel, key=lambda t: -sc.markets[t]["risk_days"]):
        m = sc.markets[t]
        rent = sc.market_rent.get(t, 0.0)
        net = rent + m["realized_dollars"] + m["mtm_dollars"]
        fc = m["first_cycle"]
        fcs = (datetime.fromtimestamp(fc, timezone.utc).strftime("%Y-%m-%d %H:%M")
               if fc else "-")
        p(f"  {t:<34} {fcs:<17} {m['risk_days']:>8,.1f} {rent:>+7.2f} "
          f"{m['realized_dollars']:>+8.2f} {m['mtm_dollars']:>+8.2f} "
          f"{sc._roi(net, m['risk_days']):>+8.4f} {sc.market_fills.get(t, 0):>5} "
          f"{m['pos']:>+6.0f}")
    p("  fills (markout is a diagnostic):")
    sset = set(sel)
    p(f"  {'ticker':<34} {'fill ts (UTC)':<20} {'q':>7} {'px':>6} {'mark':>7} "
      f"{'src':<15} {'mo c/ct':>8}  status")
    for r in sorted((r for r in sc.fill_rows if r["ticker"] in sset), key=lambda r: r["ts"]):
        ts = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        mark_s = "-" if r["mark"] is None else format(r["mark"], ".1f")
        mo_s = "-" if r["mo"] is None else format(r["mo"], "+.2f")
        p(f"  {r['ticker']:<34} {ts:<20} {r['q']:>+7.0f} {r['px']:>6.1f} "
          f"{mark_s:>7} {r['source']:<15} {mo_s:>8}  {r['status']}")


# -------------------------------------------------------------------- main

def run(argv=None, out=sys.stdout):
    global STATUS_DIR, WORK_DIR
    ap = argparse.ArgumentParser(description="open-scan performance scorer")
    ap.add_argument("--now", action="store_true", help="write the table (default)")
    ap.add_argument("--dry", action="store_true", help="print everything, write NOTHING")
    ap.add_argument("--explain", metavar="SERIES|ROOT|FAMILY|TICKER")
    ap.add_argument("--asof", metavar="ISO")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--horizon-h", type=int, default=HORIZON_H)
    ap.add_argument("--out", metavar="PATH")
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
                        horizon_h=a.horizon_h, use_cache=not a.no_cache)
    sc.load()
    sc.build()

    if a.explain:
        print_explain(sc, a.explain, out)
        return 0

    tbl = sc.table()
    print_report(sc, tbl, out)

    # ---- writer-side guards: both abort the write and say so
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
              f"inflates every roi_hist. NO FILE WRITTEN.", file=out)
        return 3

    if a.dry:
        print(f"[--dry] nothing written. cold/warm: cache hits {sc.cache.hits}, "
              f"misses {sc.cache.misses}, total {time.time() - t_start:.1f}s", file=out)
        return 0

    # The private cache is saved only AFTER the guards pass, so "NO FILE
    # WRITTEN" is literally true of every file this process owns. --dry
    # means "write NOTHING", and that includes the cache; it is still READ.
    sc.cache.save()
    path = a.out or os.path.join(work_dir, TABLE_NAME)
    atomic_write_json(path, tbl)
    append_jsonl(os.path.join(work_dir, HISTORY_NAME), sc.history_row(tbl, 0))
    lim = tbl["limits"]
    print(f"wrote {os.path.basename(path)} ({lim['blocked_events']} event roots / "
          f"{lim['blocked_series']} series / {lim['blocked_families']} families blocked "
          f"of {lim['n_units']} units); history row appended  "
          f"[{time.time() - t_start:.1f}s]", file=out)
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
