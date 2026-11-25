#!/usr/bin/env python3
r"""imm_review.py -- the deterministic half of the IMM daily review.

Jack 2026-11-25: "create a skill that on a daily basis goes through IMM bot in
incredible detail and evaluates the prior day from a wholistic standpoint
including overall performance, any attractive markets that counterfactually
should have quoted that didnt, making sure all relevant data is tracked logged
and continaully analyzed to improve the quoting strategies, and scans the
system itself for any bugs / errors, and suggests fixes to me in a morning
email. the goal is a self-improving system that compounds on itself over time
and maximizes risk-adjusted returns with minimal involvement."

This file gathers, checks and remembers. It never judges, trades, edits the
bot, restarts anything or sends mail. The skill (.claude/skills/imm-daily-review)
reads what it writes and judges; send_imm_review.py sends the email; the
launcher (run_imm_daily_review.ps1) runs the three every morning.

    python imm_review.py collect [--day YYYY-MM-DD]     # evidence pack for an ET day
    python imm_review.py ledger list [--all]            # open findings
    python imm_review.py ledger show ID
    python imm_review.py ledger due [--day D]           # shipped items due a re-measure
    python imm_review.py ledger stats
    python imm_review.py ledger ingest --day D [--file F]   # the skill's findings.json
    python imm_review.py ledger decide ID approved|rejected|deferred --note "..." [--by jack]
    python imm_review.py ledger ship ID --ref <commit> [--note "..."]
    python imm_review.py ledger close ID --status closed|fixed-elsewhere|wontfix|reverted --note "..."
    python imm_review.py measure (--pattern REGEX | --family NAME) --split YYYY-MM-DD[THH:MM]
                                 [--before 7] [--after 7]
    python imm_review.py put findings.json|review.md --day D  < content   # the unattended run's writes

Everything it writes lives under REVIEW_DIR (run-logs/incentive-mm/daily_review,
untracked, so the 30-min sync never touches it):
    <D>/evidence.json, <D>/evidence.md     the pack (collect)
    <D>/findings.json, <D>/review.md       written by the skill
    <D>/ids.json                           fingerprint -> ledger id (ingest)
    ledger.jsonl                           the append-only findings ledger
    scorecard.csv                          one row per reviewed ET day
    error_signatures.json                  bot-log fingerprints per day (new / spiking)
    counterfactual.jsonl                   each day's missed-market predictions
    config_manifest.jsonl, config_store/   versions of the reloadable config files
    task_state.json                        the previous run's scheduled-task results
    lessons.md                             what the unattended runs learned (curated later)

Basis tags: every number is MEASURED (Kalshi fills / marks / settlements, logs,
files) or MODELLED (the bot's reward estimator, one-reading estimates).
Read-only on the bot: no Kalshi calls, no writes outside REVIEW_DIR.
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import hashlib
import io
import json
import math
import os
import pickle
import re
import shutil
import statistics
import subprocess
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Day files older than 35 days are .gz (imm_log_retention, R-261124-03): a
# re-run for an old day finds and opens them through imm_logfiles.
import imm_logfiles  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - tzdata is installed on the box
    ET = timezone(timedelta(hours=-5))

try:  # the task console is cp1252: never die on a glyph
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.stderr.reconfigure(errors="backslashreplace")
except Exception:
    pass

VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Paths (configure() re-derives them; tests point them at a temp dir)
# ---------------------------------------------------------------------------
LIVE_REPO = STATUS_DIR = REVIEW_DIR = DASH_DIR = CACHE_DIR = ARCHIVE_DIR = RUN_LOGS = MOAT_DIR = ""


def configure(status_dir: Optional[str] = None, review_dir: Optional[str] = None,
              repo: Optional[str] = None) -> None:
    global LIVE_REPO, STATUS_DIR, REVIEW_DIR, DASH_DIR, CACHE_DIR, ARCHIVE_DIR, RUN_LOGS, MOAT_DIR
    LIVE_REPO = repo or os.environ.get("IMM_REVIEW_REPO") or r"C:\Users\jackd\Documents\KL"
    STATUS_DIR = status_dir or os.environ.get("IMM_STATUS_DIR") or os.path.join(
        LIVE_REPO, "run-logs", "incentive-mm")
    REVIEW_DIR = review_dir or os.environ.get("IMM_REVIEW_DIR") or os.path.join(
        STATUS_DIR, "daily_review")
    DASH_DIR = os.path.join(STATUS_DIR, "dashboard")
    CACHE_DIR = os.path.join(DASH_DIR, "cache")
    ARCHIVE_DIR = os.path.join(CACHE_DIR, "day_archive")
    RUN_LOGS = os.path.dirname(os.path.normpath(STATUS_DIR))
    MOAT_DIR = os.path.join(REVIEW_DIR, "moat")


configure()

# The dashboard's per-market day rows (imm_dashboard.DAY_FIELDS / FX_KEYS).
DAY_FIELDS = ("rew", "pnl", "real", "du", "xfer", "fills", "cts", "usd", "mk", "mk_n",
              "mk_cts", "rest", "pos0", "pos1", "mk0", "mk1", "settled", "cr", "cs", "fx")
FX_KEYS = ("fe", "fe_cts", "m5", "m5_cts", "m4", "m4_cts", "mt", "mt_cts")
SUM_KEYS = ("rew", "pnl", "real", "du", "xfer", "fills", "cts", "usd", "mk", "mk_n", "mk_cts",
            "rest", "cr", "cs") + FX_KEYS

# Sinks the bot writes per UTC day (IMM_LOGGING.md + the newer ones in code).
SINKS = ("cycle_log", "orders", "fills", "realized", "settlements", "marks", "selection_events",
         "selection_snapshot", "floor_state", "guard_skips", "config_history", "toxic_halts",
         "ws_stale", "ws_sweep", "join_wait", "back_of_line", "risk_dry", "heightened",
         "ws_tape", "ws_group", "ws_cand", "comp_hedge", "book_depth")

# The scheduled IMM jobs' own logs (agent survey 11/25), relative to RUN_LOGS.
TASK_LOGS = (
    ("new-programs", "incentive-mm/new-programs-task.log"),
    ("earnings-overrides", "incentive-mm/earnings-overrides-task.log"),
    ("back-of-line", "incentive-mm/back-of-line-task.log"),
    ("portfolio digest", "portfolio-digest/digest-task.log"),
    ("toxic-halts", "incentive-mm/toxic-halts-task.log"),
    ("opportunistic", "incentive-mm/opportunistic-task.log"),
    ("carbon-arc", "incentive-mm/carbon-arc-task.log"),
    ("lone-ref", "incentive-mm/lone-ref-task.log"),
    ("program-history", "incentive-mm/reward-recon-task.log"),
    ("comp lead", "incentive-mm/comp-lead-task.log"),
    ("rt-settled", "incentive-mm/rt-settled-task.log"),
    ("join-wait", "incentive-mm/join-wait-task.log"),
    ("cheap-report", "incentive-mm/cheap-report-task.log"),
    ("family-watch", "incentive-mm/family-watch.log"),
    ("saturday-tracker", "incentive-mm/saturday-tracker-task.log"),
    ("dashboard", "incentive-mm/dashboard/dashboard-task.log"),
)
TASK_LOG_ERR = re.compile(r"FAILED|Traceback|timed out|unavailable|PermissionError|"
                          r"WinError|UnicodeEncodeError|Too Many Requests|\bERROR\b|send: FAILED")

# Monitor stores: (label, file, field whitelist). Each is {day: {...}} or {"days": {...}}.
MONITORS = (
    ("toxic halts", "toxic_halts_profit.json",
     ("halts_net", "blocked", "forgone", "n_halts", "n_picks", "picks_5m", "picks_now")),
    ("lone-ref guard (dry)", "lone_ref_monitor.json",
     ("level", "verdict", "net", "cost", "holds", "would_net", "let_thru")),
    ("join wait", "join_wait_report.json",
     ("level", "net", "better", "cost", "kept", "joined", "gone")),
    ("back of line", "bol_report.json",
     ("level", "saved", "saved_4h", "avoided_ct", "cost", "bol_amends")),
    ("cheap-side books", "cheap_report.json",
     ("level", "verdict", "net", "rewards", "fills", "pnl")),
    ("RT settled strikes", "rt_settled_report.json",
     ("level", "verdict", "net", "rewards", "pnl", "swing")),
    ("risk controls", "risk_controls_history.json", ()),
)

# Gap reasons from imm_quote_gaps.classify_and_estimate -> a class the review reasons about.
REASON_CLASSES = (
    ("deliberate", ("blocklist", "frozen", "excluded family", "yielded to manual")),
    ("not enrolled", ("not in allowlist",)),
    ("capacity", ("capacity",)),
    ("economics", ("under payout floor", "under rate floor", "zero est yield",
                   "admission clock", "no-new")),
    ("market screen", ("screened",)),
    ("open scan", ("open-scan", "open scan")),
)
# Single-reading estimates realise ~0.71-0.74x of themselves at admission
# (incentive_mm.py comment at the admission clock). Replaced by the measured
# calibration of our own predictions once there is enough history.
DEFAULT_REALISATION = 0.72

LOG_TS = re.compile(rb"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})Z ")
OK_TASK_CODES = {0, 267009, 267011, 267045}
LEDGER_ID = re.compile(r"\bR-\d{6}-\d{2}\b")

STATUSES = ("open", "proposed", "approved", "rejected", "deferred", "shipped", "verified",
            "failed", "inconclusive", "closed", "fixed-elsewhere", "wontfix", "reverted")
TERMINAL = {"rejected", "verified", "closed", "fixed-elsewhere", "wontfix", "reverted"}
KINDS = ("bug", "ops", "data", "missed", "loss", "strategy", "experiment", "review")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _f(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _load_pickle(path: str):
    try:
        with open(path, "rb") as fh:
            return pickle.load(fh)
    except Exception:
        return None


def _iter_jsonl(path: str) -> Iterable[dict]:
    try:
        with imm_logfiles.open_text(imm_logfiles.resolve(path), encoding="utf-8",
                                    errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, default=str)
    for i in range(5):                 # Windows: a reader can hold the target open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.5 * (i + 1))
    os.replace(tmp, path)


def _ts_of(v) -> Optional[float]:
    """epoch seconds from an epoch number or an ISO string (Z or offset)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def usd(x, signed: bool = True) -> str:
    x = _f(x)
    if signed:
        return ("-" if x < 0 else "+") + "${:,.2f}".format(abs(x))
    return "${:,.2f}".format(x)


def usd0(x, signed: bool = True) -> str:
    x = _f(x)
    if signed:
        return ("-" if x < 0 else "+") + "${:,.0f}".format(abs(x))
    return "${:,.0f}".format(x)


def cpc(dollars, cts) -> Optional[float]:
    """cents per contract, None without contracts."""
    c = _f(cts)
    return round(_f(dollars) / c * 100.0, 2) if c > 0 else None


def et_bounds(day: date) -> Tuple[float, float]:
    s = datetime.combine(day, dtime(0), tzinfo=ET).timestamp()
    e = datetime.combine(day + timedelta(days=1), dtime(0), tzinfo=ET).timestamp()
    return s, e


def utc_dates(day: date) -> List[str]:
    s, e = et_bounds(day)
    a = datetime.fromtimestamp(s, timezone.utc).date()
    b = datetime.fromtimestamp(e - 1, timezone.utc).date()
    out, d = [], a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def prior_et_day(now: Optional[float] = None) -> date:
    n = datetime.fromtimestamp(now if now is not None else time.time(), ET)
    return n.date() - timedelta(days=1)


def et_str(ts: Optional[float], fmt: str = "%m-%d %H:%M") -> str:
    if ts is None:
        return "n/a"
    return datetime.fromtimestamp(ts, ET).strftime(fmt)


def series_of(ticker: str) -> str:
    return (ticker or "").split("-", 1)[0]


_META = None


def event_of(ticker: str) -> str:
    global _META
    if _META is None:
        _META = _load_json(os.path.join(CACHE_DIR, "markets_meta.json"), {}) or {}
    rec = _META.get(ticker)
    if isinstance(rec, dict) and rec.get("ev"):
        return rec["ev"]
    parts = (ticker or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 else ticker


_FAM_CACHE: Dict[str, Tuple[str, str]] = {}
_FAM_FN = None


def family_of(series: str) -> Tuple[str, str]:
    """(family, group) by the dashboard's rules; series prefix when they can't load."""
    global _FAM_FN
    if series in _FAM_CACHE:
        return _FAM_CACHE[series]
    if _FAM_FN is None:
        try:
            if HERE not in sys.path:
                sys.path.insert(0, HERE)
            import imm_dashboard  # light: constants + pure functions; no bot import
            imm_dashboard.STATUS_DIR = STATUS_DIR
            imm_dashboard.CACHE_DIR = CACHE_DIR
            _FAM_FN = imm_dashboard.family_of
        except Exception:
            _FAM_FN = False
    out = (series, series)
    if _FAM_FN:
        try:
            out = tuple(_FAM_FN(series))[:2]
        except Exception:
            out = (series, series)
    _FAM_CACHE[series] = out
    return out


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 60) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # missing binary, timeout
        return -1, f"{type(e).__name__}: {e}"


def _git(*args: str, timeout: int = 60) -> Tuple[int, str]:
    return _run(["git", "-c", "core.quotepath=off"] + list(args), cwd=LIVE_REPO, timeout=timeout)


def _section(name: str, fn, *a, **kw) -> dict:
    t0 = time.time()
    try:
        out = fn(*a, **kw)
        if not isinstance(out, dict):
            out = {"value": out}
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}",
               "trace": traceback.format_exc(limit=4)[-1200:]}
    out["_secs"] = round(time.time() - t0, 1)
    return out


# ---------------------------------------------------------------------------
# Day archive (the dashboard's per-market, per-ET-day rows)
# ---------------------------------------------------------------------------

def archive_days() -> List[str]:
    try:
        return sorted(n[:10] for n in os.listdir(ARCHIVE_DIR)
                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", n))
    except OSError:
        return []


def load_archive(iso: str) -> Optional[dict]:
    blob = _load_json(os.path.join(ARCHIVE_DIR, iso + ".json"))
    return blob if isinstance(blob, dict) and isinstance(blob.get("m"), dict) else None


def market_rows(blob: dict) -> Iterable[Tuple[str, dict]]:
    f = blob.get("f") or list(DAY_FIELDS)
    fxk = blob.get("fx") or list(FX_KEYS)
    for t, row in (blob.get("m") or {}).items():
        v = dict(zip(f, row))
        fx = v.get("fx")
        if isinstance(fx, list):
            for k, x in zip(fxk, fx):
                v[k] = x
        yield t, v


def day_totals(blob: dict) -> dict:
    tot = defaultdict(float)
    n = n_rew = n_fill = 0
    for _t, v in market_rows(blob):
        n += 1
        for k in SUM_KEYS:
            tot[k] += _f(v.get(k))
        tot["inv_cts"] += abs(_f(v.get("pos1")))
        if _f(v.get("rew")) > 0:
            n_rew += 1
        if _f(v.get("fills")) > 0:
            n_fill += 1
    out = {k: round(x, 4) for k, x in tot.items()}
    out["net"] = round(tot["rew"] + tot["pnl"], 4)
    out["markets"] = n
    out["markets_rew"] = n_rew
    out["markets_filled"] = n_fill
    out["ok"] = bool(blob.get("ok", True))
    return out


def all_day_totals() -> Dict[str, dict]:
    """{iso: totals} for every archived day; cached by file size+mtime."""
    cache_path = os.path.join(REVIEW_DIR, "archive_totals_cache.json")
    cache = _load_json(cache_path, {}) or {}
    out, dirty = {}, False
    for iso in archive_days():
        p = os.path.join(ARCHIVE_DIR, iso + ".json")
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        c = cache.get(iso)
        if c and c.get("sig") == sig:
            out[iso] = c["t"]
            continue
        blob = load_archive(iso)
        if not blob:
            continue
        out[iso] = day_totals(blob)
        cache[iso] = {"sig": sig, "t": out[iso]}
        dirty = True
    if dirty:
        try:
            _write_json(cache_path, cache)
        except OSError:
            pass
    return out


def window_stats(vals: List[float]) -> dict:
    n = len(vals)
    if not n:
        return {"n": 0}
    mean = sum(vals) / n
    sd = statistics.pstdev(vals) if n > 1 else 0.0
    down = [min(v, 0.0) for v in vals]
    dsd = math.sqrt(sum(d * d for d in down) / n) if n else 0.0
    cum = peak = 0.0
    mdd = 0.0
    for v in vals:
        cum += v
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {"n": n, "sum": round(sum(vals), 2), "mean": round(mean, 2), "sd": round(sd, 2),
            "sharpe": round(mean / sd * math.sqrt(365), 2) if sd > 0 else None,
            "sortino": round(mean / dsd * math.sqrt(365), 2) if dsd > 0 else None,
            "max_dd": round(mdd, 2), "worst": round(min(vals), 2), "best": round(max(vals), 2),
            "green": sum(1 for v in vals if v > 0)}


# ---------------------------------------------------------------------------
# 1. Scorecard
# ---------------------------------------------------------------------------

def sec_scorecard(day: date) -> dict:
    iso = day.isoformat()
    totals = all_day_totals()
    days = sorted(d for d in totals if d <= iso and totals[d].get("ok", True))
    out: dict = {"day": iso, "basis": {"rew": "MODELLED (bot estimator, pre-floor)",
                                       "pnl": "MEASURED (MTM at last trade, dashboard replay)"}}
    d = totals.get(iso)
    out["day_totals"] = d
    if d is None:
        out["missing_day"] = True
    for label, k in (("7d", 7), ("30d", 30), ("inception", 100000)):
        sel = days[-k:]
        nets = [totals[x]["net"] for x in sel]
        rews = [totals[x]["rew"] for x in sel]
        pnls = [totals[x]["pnl"] for x in sel]
        st = window_stats(nets)
        st["from"] = sel[0] if sel else None
        st["rew_mean"] = round(sum(rews) / len(sel), 2) if sel else None
        st["pnl_mean"] = round(sum(pnls) / len(sel), 2) if sel else None
        st["tax"] = round(-sum(pnls) / sum(rews), 3) if sel and sum(rews) > 0 else None
        rest = [totals[x].get("rest", 0.0) for x in sel]
        st["rest_mean"] = round(sum(rest) / len(sel), 0) if sel else None
        st["net_per_1k_rest"] = (round(st["mean"] / (st["rest_mean"] / 1000.0), 3)
                                 if sel and st.get("rest_mean") else None)
        if label == "30d" and sel:
            worst = min(sel, key=lambda x: totals[x]["net"])
            st["worst_day"] = worst
        out[label] = st
    out["recent"] = [{"d": x, "rew": round(totals[x]["rew"], 2), "pnl": round(totals[x]["pnl"], 2),
                      "net": round(totals[x]["net"], 2), "fills": int(totals[x].get("fills", 0)),
                      "cts": round(totals[x].get("cts", 0.0)), "rest": round(totals[x].get("rest", 0.0))}
                     for x in days[-14:]]
    # the dashboard's own numbers for the day (the page and the 7:00 email use these)
    summ = _load_json(os.path.join(DASH_DIR, "imm_dashboard_summary.json"), {}) or {}
    row = next((r for r in summ.get("days") or [] if r.get("d") == iso), None)
    if row and d:
        out["dash_check"] = {"dash_rew": row.get("rew"), "dash_pnl": row.get("pnl"),
                             "archive_rew": round(d["rew"], 2), "archive_pnl": round(d["pnl"], 2),
                             "diff_rew": round(_f(row.get("rew")) - d["rew"], 2),
                             "diff_pnl": round(_f(row.get("pnl")) - d["pnl"], 2)}
    out["dash_generated"] = summ.get("generated")
    tw = (summ.get("windows") or {}).get("today") or {}
    if tw:
        out["today_so_far"] = {"rew": tw.get("rew"), "pnl": tw.get("pnl"), "fills": tw.get("fills"),
                               "as_of": summ.get("generated_et")}
    st = _load_json(os.path.join(STATUS_DIR, "status_incentive_mm.json"), {}) or {}
    if st:
        out["now"] = {k: st.get(k) for k in (
            "updated_at", "mode", "markets_line", "selected", "reward_est_today",
            "reward_paid_today", "reward_est_lifetime", "reward_paid_lifetime", "unrealized_mtm",
            "unrealized_ext", "inventory_contracts", "pnl_today", "fills_today", "cycles_today",
            "errors_today", "halted_until", "programs_seen")}
        out["now"]["alerts_today"] = st.get("alerts_today")
    cal = _load_json(os.path.join(STATUS_DIR, "reward_calibration.json"), {}) or {}
    if cal:
        fam = ((cal.get("post_amendment") or {}).get("by_family") or {})
        out["calibration"] = {
            "realization_factor": cal.get("realization_factor"),
            "ledger_through": cal.get("ledger_through"), "generated_at": cal.get("generated_at"),
            "by_family": {k: v.get("realization_factor") for k, v in fam.items()
                          if isinstance(v, dict)}}
    out["credits"] = credit_ledger(day)
    return out


def credit_ledger(day: date) -> dict:
    path = os.path.join(STATUS_DIR, "reward_credits.csv")
    last = None
    by_day = defaultdict(float)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                cd = (r.get("credit_date") or "")[:10]
                if not cd:
                    continue
                last = cd if last is None or cd > last else last
                if (r.get("kind") or "").lower().startswith("liq"):
                    by_day[cd] += _f(r.get("amount"))
    except OSError:
        return {"error": "reward_credits.csv unreadable"}
    out = {"through": last}
    if last:
        out["age_days"] = (day + timedelta(days=1) - date.fromisoformat(last)).days
        lo = (date.fromisoformat(last) - timedelta(days=6)).isoformat()
        out["last7_credited"] = round(sum(v for k, v in by_day.items() if lo <= k <= last), 2)
    return out


# ---------------------------------------------------------------------------
# 2. Attribution + 3. adverse selection (from the day archive)
# ---------------------------------------------------------------------------

def _agg(rows: Iterable[Tuple[str, dict]], key_fn) -> Dict[str, dict]:
    acc: Dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for t, v in rows:
        a = acc[key_fn(t)]
        for k in SUM_KEYS:
            a[k] += _f(v.get(k))
        a["n"] += 1
    out = {}
    for k, a in acc.items():
        a = dict(a)
        a["net"] = a.get("rew", 0.0) + a.get("pnl", 0.0)
        out[k] = a
    return out


def _fam_key(t: str) -> str:
    return family_of(series_of(t))[0]


def _round_row(a: dict, keys=("rew", "pnl", "net", "fills", "cts", "rest")) -> dict:
    out = {k: round(_f(a.get(k)), 2) for k in keys}
    out["mk30_c"] = cpc(a.get("mk"), a.get("mk_cts"))
    out["fe_c"] = cpc(a.get("fe"), a.get("fe_cts"))
    out["m5_c"] = cpc(a.get("m5"), a.get("m5_cts"))
    out["m4_c"] = cpc(a.get("m4"), a.get("m4_cts"))
    out["mt_c"] = cpc(a.get("mt"), a.get("mt_cts"))
    return out


def sec_attribution(day: date) -> dict:
    iso = day.isoformat()
    days = [d for d in archive_days() if d <= iso]
    blob = load_archive(iso)
    if not blob:
        return {"error": f"no day archive for {iso}"}
    d_rows = list(market_rows(blob))
    win7 = days[-7:]
    win30 = days[-30:]
    rows7: List[Tuple[str, dict]] = []
    rows30: List[Tuple[str, dict]] = []
    for x in win30:
        b = blob if x == iso else load_archive(x)
        if not b:
            continue
        rr = list(market_rows(b))
        rows30.extend(rr)
        if x in win7:
            rows7.extend(rr)
    fam_d, fam7, fam30 = _agg(d_rows, _fam_key), _agg(rows7, _fam_key), _agg(rows30, _fam_key)
    fams = sorted(set(fam_d) | set(fam7) | set(fam30),
                  key=lambda f: _f((fam30.get(f) or {}).get("net")))
    families = []
    for f in fams:
        a, b, c = fam_d.get(f) or {}, fam7.get(f) or {}, fam30.get(f) or {}
        families.append({"family": f, "day": _round_row(a), "d7": _round_row(b),
                         "d30": _round_row(c)})
    ev_d = _agg(d_rows, event_of)
    ser7, ser30 = _agg(rows7, series_of), _agg(rows30, series_of)
    worst_ev = sorted(ev_d.items(), key=lambda kv: kv[1]["net"])[:12]
    best_ev = sorted(ev_d.items(), key=lambda kv: -kv[1]["net"])[:8]
    worst_mk = sorted(d_rows, key=lambda tv: _f(tv[1].get("pnl")))[:12]
    losers = []
    for s, a in ser30.items():
        b = ser7.get(s) or {}
        if a["net"] < -25 and _f(b.get("net")) < 0:
            losers.append((s, a, b))
    losers.sort(key=lambda x: x[1]["net"])
    winners = sorted(((s, a) for s, a in ser30.items() if a["net"] > 0),
                     key=lambda x: -x[1]["net"])[:10]
    return {
        "families": families,
        "worst_events": [{"event": e, **_round_row(a)} for e, a in worst_ev],
        "best_events": [{"event": e, **_round_row(a)} for e, a in best_ev],
        "worst_markets": [{"ticker": t, "pnl": round(_f(v.get("pnl")), 2),
                           "carry_remark": round(_f(v.get("cr")), 2),
                           "carry_settled": round(_f(v.get("cs")), 2),
                           "new_fills": round(_f(v.get("pnl")) - _f(v.get("cr")) - _f(v.get("cs")), 2),
                           "rew": round(_f(v.get("rew")), 2), "fills": _f(v.get("fills")),
                           "cts": round(_f(v.get("cts"))), "mt_c": cpc(v.get("mt"), v.get("mt_cts")),
                           "pos1": v.get("pos1"), "settled": v.get("settled")}
                          for t, v in worst_mk if _f(v.get("pnl")) < 0],
        "persistent_losers": [{"series": s, "family": family_of(s)[0], "net30": round(a["net"], 2),
                               "rew30": round(a.get("rew", 0), 2), "pnl30": round(a.get("pnl", 0), 2),
                               "net7": round(_f(b.get("net")), 2),
                               "mt_c30": cpc(a.get("mt"), a.get("mt_cts"))}
                              for s, a, b in losers[:15]],
        "top_series_30d": [{"series": s, "family": family_of(s)[0], "net30": round(a["net"], 2),
                            "rew30": round(a.get("rew", 0), 2), "pnl30": round(a.get("pnl", 0), 2)}
                           for s, a in winners],
        "windows": {"d7_from": win7[0] if win7 else None, "d30_from": win30[0] if win30 else None},
    }


def sec_adverse(day: date) -> dict:
    s, e = et_bounds(day)
    kinds: Counter = Counter()
    by_event: Counter = Counter()
    for u in utc_dates(day):
        for r in _iter_jsonl(os.path.join(STATUS_DIR, f"toxic_halts_{u}.jsonl")):
            ts = _ts_of(r.get("ts"))
            if ts is None or not (s <= ts < e):
                continue
            kinds[r.get("kind") or "?"] += 1
            if r.get("kind") == "pickoff":
                by_event[r.get("event") or event_of(r.get("ticker") or "")] += 1
    sweeps: Counter = Counter()
    for u in utc_dates(day):
        for r in _iter_jsonl(os.path.join(STATUS_DIR, f"ws_sweep_{u}.jsonl")):
            ts = _ts_of(r.get("ts"))
            if ts is not None and s <= ts < e:
                sweeps[str(r.get("mode") or r.get("kind") or "trip")] += 1
    return {"toxic": dict(kinds), "pickoff_events": by_event.most_common(10),
            "sweeps": dict(sweeps)}


def sec_monitors(day: date) -> dict:
    iso = day.isoformat()
    out = []
    for label, fname, keys in MONITORS:
        blob = _load_json(os.path.join(STATUS_DIR, fname))
        if not isinstance(blob, dict):
            out.append({"monitor": label, "status": "missing"})
            continue
        days = blob.get("days") if isinstance(blob.get("days"), dict) else blob
        ks = sorted(k for k in days if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(k)) and k <= iso)
        if not ks:
            out.append({"monitor": label, "status": "no day <= " + iso})
            continue
        k = ks[-1]
        rec = days[k] if isinstance(days[k], dict) else {}
        flat = {}
        for kk, vv in rec.items():
            if (not keys or kk in keys) and isinstance(vv, (int, float, str)) and not isinstance(vv, bool):
                flat[kk] = round(vv, 2) if isinstance(vv, float) else vv
        if not keys and isinstance(rec.get("m"), dict):  # risk controls: the metrics row
            flat = {kk: (round(vv, 3) if isinstance(vv, float) else vv)
                    for kk, vv in list(rec["m"].items())[:14]}
        out.append({"monitor": label, "day": k, "lag_days": (day - date.fromisoformat(k)).days,
                    "fields": flat})
    return {"monitors": out}


# ---------------------------------------------------------------------------
# 4. Counterfactual: paying markets we did not quote
# ---------------------------------------------------------------------------

def reason_class(reason: str) -> str:
    first = (reason or "").split(",")[0].strip().lower()
    for cls, keys in REASON_CLASSES:
        if any(k in first for k in keys):
            return cls
    return "other"


def realisation_factor() -> Tuple[float, str]:
    cal = _load_json(os.path.join(REVIEW_DIR, "counterfactual_calibration.json"), {}) or {}
    if _f(cal.get("n")) >= 15 and _f(cal.get("factor")) > 0:
        return float(cal["factor"]), f"measured on {int(cal['n'])} of our own predictions"
    return DEFAULT_REALISATION, "default 0.72 (admission realised 0.71-0.74x of a single reading)"


def sec_counterfactual(day: date) -> dict:
    iso = day.isoformat()
    out: dict = {"basis": "MODELLED: one-reading estimates, full-day coverage, no competitor response"}
    fac, fac_note = realisation_factor()
    out["realisation"] = {"factor": fac, "note": fac_note}
    gaps = _load_pickle(os.path.join(CACHE_DIR, "api_gaps.pkl")) or {}
    val = gaps.get("val") if isinstance(gaps, dict) else None
    if isinstance(val, dict):
        rows = [r for r in val.get("rows") or [] if isinstance(r, dict)]
        out["gaps_as_of"] = gaps.get("at")
        out["universe"] = {k: (val.get("ctx") or {}).get(k) for k in (
            "programs", "pool_total", "quoted", "quoted_events", "event_cap", "unquoted_rows",
            "books_read", "est_missed_total", "unest_pool", "updated_at")}
        by_cls = defaultdict(lambda: {"rows": 0, "pool": 0.0, "est": 0.0})
        for r in rows:
            c = by_cls[reason_class(r.get("reason") or "")]
            c["rows"] += 1
            c["pool"] += _f(r.get("pool"))
            c["est"] += _f(r.get("est"))
        out["by_class"] = {k: {"rows": v["rows"], "pool": round(v["pool"], 2),
                               "est": round(v["est"], 2), "est_adj": round(v["est"] * fac, 2)}
                           for k, v in sorted(by_cls.items(), key=lambda kv: -kv[1]["pool"])}

        # THE MOAT AT WORK: a missed market is worth its modelled reward x the
        # realisation factor x (1 - its family's trading tax), the tax being the
        # share of rewards that family's fills gave back over 30 days (priors.json,
        # shrunk toward the book; the book's own for a family never quoted).
        pri = _load_json(os.path.join(MOAT_DIR, "priors.json"), {}) or {}
        book_tax = ((pri.get("book") or {}).get("tax"))

        def tax_of(r):
            fam = family_of(r.get("series") or series_of(r.get("event") or ""))[0]
            t = (((pri.get("families") or {}).get(fam) or {}).get("prior") or {}).get("tax")
            t = book_tax if t is None else t
            return fam, (None if t is None else min(max(float(t), -0.5), 1.5))

        def slim(r):
            fam, tax = tax_of(r)
            adj = _f(r.get("est")) * fac
            return {"event": r.get("event"), "series": r.get("series"), "title": (r.get("title") or "")[:70],
                    "reason": r.get("reason"), "class": reason_class(r.get("reason") or ""),
                    "pool": round(_f(r.get("pool")), 2), "est": round(_f(r.get("est")), 2),
                    "est_adj": round(adj, 2), "family": fam, "tax": tax,
                    "est_net": round(adj * (1.0 - tax), 2) if tax is not None else round(adj, 2),
                    "yld": r.get("yld"), "n": r.get("n"), "n_est": r.get("n_est"), "end": r.get("end")}
        est_rows = sorted((slim(r) for r in rows if _f(r.get("est")) > 0), key=lambda x: -x["est_net"])
        out["top_estimated"] = est_rows[:20]
        out["net_basis"] = (f"est x {fac} realisation x (1 - the family's 30-day trading tax; "
                            f"book {book_tax})" if book_tax is not None else "no priors yet")
        unest = sorted((r for r in rows if not _f(r.get("n_est"))), key=lambda r: -_f(r.get("pool")))
        out["top_unestimated_pool"] = [slim(r) for r in unest[:12]]
        log_predictions(day, est_rows[:40], fac)
    else:
        out["gaps_error"] = "dashboard cache api_gaps.pkl missing or unreadable"
    unq = _load_pickle(os.path.join(CACHE_DIR, "api_unquoted.pkl")) or {}
    uval = unq.get("val") if isinstance(unq, dict) else None
    if isinstance(uval, dict):
        urows = [r for r in uval.get("rows") or [] if isinstance(r, dict)]
        out["not_enrolled_as_of"] = unq.get("at")
        out["not_enrolled_calls"] = (uval.get("ctx") or {}).get("calls")
        top = sorted(urows, key=lambda r: -_f(r.get("x1")))[:12]
        out["not_enrolled_top"] = [{"series": r.get("series"), "title": (r.get("title") or "")[:60],
                                    "fam": r.get("fam"), "pool": r.get("pool"), "x1": r.get("x1"),
                                    "x3": r.get("x3"), "call": r.get("call"),
                                    "why": (r.get("why") or "")[:90], "tape_c": r.get("tape_c")}
                                   for r in top]
    cg = _load_json(os.path.join(STATUS_DIR, "config_gaps.json"), {}) or {}
    gaps_now = cg.get("gaps") or {}
    out["config_gaps"] = [{"event": k, "series": v.get("series"), "since": v.get("since"),
                           "msg": (v.get("msg") or "")[:160]}
                          for k, v in list(gaps_now.items())[:25] if isinstance(v, dict)]
    out["selection"] = selection_day(day)
    out["calibration"] = calibrate_predictions(day)
    return out


def log_predictions(day: date, rows: List[dict], fac: float) -> None:
    path = os.path.join(REVIEW_DIR, "counterfactual.jsonl")
    iso = day.isoformat()
    seen = set()
    for r in _iter_jsonl(path):
        if r.get("day") == iso:
            seen.add(r.get("event"))
    os.makedirs(REVIEW_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            ev = r.get("event")
            if not ev or ev in seen:
                continue
            fh.write(json.dumps({"day": iso, "event": ev, "series": r.get("series"),
                                 "reason": r.get("reason"), "est": round(_f(r.get("est")), 3),
                                 "pool": round(_f(r.get("pool")), 2), "fac": fac}) + "\n")


def calibrate_predictions(day: date) -> dict:
    """Score past predictions against events the bot later quoted: realised modelled
    reward per quoted day / predicted est. Writes the factor the next run uses."""
    preds = [r for r in _iter_jsonl(os.path.join(REVIEW_DIR, "counterfactual.jsonl"))
             if r.get("day") and r["day"] <= (day - timedelta(days=2)).isoformat()]
    if not preds:
        return {"n": 0, "note": "no predictions old enough yet"}
    days = [d for d in archive_days() if d <= day.isoformat()]
    want = defaultdict(list)
    for p in preds:
        want[p["event"]].append(p)
    realised = defaultdict(lambda: defaultdict(float))     # event -> day -> rew
    for d in days[-30:]:
        b = load_archive(d)
        if not b:
            continue
        for t, v in market_rows(b):
            ev = event_of(t)
            if ev in want and _f(v.get("rew")) > 0:
                realised[ev][d] += _f(v.get("rew"))
    ratios = []
    for ev, plist in want.items():
        p = plist[0]
        later = {d: x for d, x in realised.get(ev, {}).items() if d > p["day"]}
        if len(later) >= 1 and _f(p.get("est")) > 0:
            ratios.append(sum(later.values()) / len(later) / _f(p["est"]))
    out = {"n_predictions": len(want), "n": len(ratios)}
    if ratios:
        ratios.sort()
        med = ratios[len(ratios) // 2]
        out.update({"median_ratio": round(med, 3),
                    "factor": round(max(0.05, min(med, 2.0)), 3)})
        _write_json(os.path.join(REVIEW_DIR, "counterfactual_calibration.json"),
                    {"factor": out["factor"], "n": len(ratios), "at": time.time()})
    return out


def selection_day(day: date) -> dict:
    """Allowed candidates the bot cut, from the hourly selection snapshots: the
    average modelled $/day by decision, and floor misses (periods that ended in
    the day with accrual under the $1 floor)."""
    s, e = et_bounds(day)
    snaps = set()
    by_dec = defaultdict(lambda: {"est": 0.0, "tickers": set()})
    top = defaultdict(float)
    last_row: Dict[str, dict] = {}
    for u in utc_dates(day):
        for r in _iter_jsonl(os.path.join(STATUS_DIR, f"selection_snapshot_{u}.jsonl")):
            ts = _ts_of(r.get("ts"))
            if ts is None or not (s <= ts < e):
                continue
            snaps.add(int(ts // 60))
            dec = r.get("decision") or "?"
            est = _f(r.get("est_dollars_per_day"))
            b = by_dec[dec]
            b["est"] += est
            b["tickers"].add(r.get("ticker"))
            if dec != "selected" and est > 0:
                top[(r.get("event_ticker") or event_of(r.get("ticker") or ""), dec)] += est
            pe = _ts_of(r.get("program_end"))
            if pe is not None and s <= pe < e + 3600:
                last_row[r.get("ticker")] = r
    n = max(len(snaps), 1)
    decisions = {k: {"tickers": len(v["tickers"]), "avg_est_day": round(v["est"] / n, 2)}
                 for k, v in sorted(by_dec.items(), key=lambda kv: -kv[1]["est"])}
    under, near = [], []
    for t, r in last_row.items():
        banked = _f(r.get("accrued_lifetime")) - _f(r.get("period_base"))
        if 0.0 < banked < 0.9:
            under.append((t, round(banked, 3)))
        elif 0.9 <= banked < 1.0:
            near.append((t, round(banked, 3)))
    under.sort(key=lambda x: -x[1])
    by_series = Counter(series_of(t) for t, _ in under)
    return {"snapshots": len(snaps), "decisions": decisions,
            "top_cut": [{"event": ev, "decision": dec, "avg_est_day": round(v / n, 2)}
                        for (ev, dec), v in sorted(top.items(), key=lambda kv: -kv[1])[:15]],
            "floor_misses": {"periods_ending": len(last_row),
                             "under_090": len(under),
                             "under_090_modelled": round(sum(b for _, b in under), 2),
                             "near_090_100": len(near),
                             "near_modelled": round(sum(b for _, b in near), 2),
                             "by_series": by_series.most_common(8), "top": under[:8]}}


# ---------------------------------------------------------------------------
# 5. Coverage and uptime
# ---------------------------------------------------------------------------

def cycle_times(day: date) -> List[float]:
    s, e = et_bounds(day)
    seen = set()
    for u in utc_dates(day):
        path = imm_logfiles.resolve(os.path.join(STATUS_DIR, f"cycle_log_{u}.csv"))
        last = b""
        try:
            with imm_logfiles.open_bin(path) as fh:
                for line in fh:
                    head = line[:20]
                    if head == last or not head[:2].isdigit():
                        continue
                    last = head
                    try:
                        ts = datetime.strptime(head.decode("ascii"), "%Y-%m-%dT%H:%M:%SZ") \
                            .replace(tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        continue
                    if s <= ts < e:
                        seen.add(ts)
        except OSError:
            continue
    return sorted(seen)


def sec_coverage(day: date) -> dict:
    s, e = et_bounds(day)
    ts = cycle_times(day)
    out: dict = {"cycles": len(ts)}
    if len(ts) > 1:
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        gs = sorted(gaps)
        out.update({"gap_p50_s": round(gs[len(gs) // 2], 1),
                    "gap_p95_s": round(gs[int(len(gs) * 0.95)], 1),
                    "gap_max_s": round(gs[-1], 1),
                    "gap_max_at": et_str(ts[gaps.index(gs[-1])]),
                    "dark_minutes": round(sum(g - 600 for g in gaps if g > 600) / 60.0, 1),
                    "head_gap_min": round((ts[0] - s) / 60.0, 1),
                    "tail_gap_min": round((e - ts[-1]) / 60.0, 1)})
    enters: Counter = Counter()
    for u in utc_dates(day):
        for r in _iter_jsonl(os.path.join(STATUS_DIR, f"guard_skips_{u}.jsonl")):
            if r.get("kind") != "enter":
                continue
            t = _ts_of(r.get("ts"))
            if t is not None and s <= t < e:
                enters[r.get("guard") or "?"] += 1
    out["guard_enters"] = enters.most_common(12)
    return out


# ---------------------------------------------------------------------------
# 6. System health
# ---------------------------------------------------------------------------

_FP_SUBS = (
    (re.compile(r"https?://\S+"), "<URL>"),
    (re.compile(r"'[A-Za-z]:\\[^']*'"), "<PATH>"),
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+"), "<PATH>"),
    (re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9.]+)+\b"), "<T>"),
    (re.compile(r"\bKX[A-Z0-9]{2,}\b"), "<S>"),
    (re.compile(r"\b[0-9a-f]{8}\b"), "<ID>"),
    (re.compile(r"\d+(?:\.\d+)?"), "N"),
    (re.compile(r"(?:<T>|<S>)(?:\s*,\s*(?:<T>|<S>|N))+"), "<T>"),     # a list of any length
    (re.compile(r"\s+"), " "),
)


def fingerprint(msg: str) -> str:
    s = msg
    for rx, rep in _FP_SUBS:
        s = rx.sub(rep, s)
    return s.strip()[:180]


def _bot_log_files(day: date) -> List[str]:
    s, _e = et_bounds(day)
    out = []
    for p in glob.glob(os.path.join(STATUS_DIR, "incentive-mm-*.log")):
        m = re.search(r"incentive-mm-(\d{4}-\d{2}-\d{2})\.log$", p)
        if not m:
            continue
        d = date.fromisoformat(m.group(1))
        if d > day + timedelta(days=1) or d < day - timedelta(days=7):
            continue
        try:
            if os.path.getmtime(p) < s:
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out)


def scan_bot_logs(days: List[date]) -> Dict[str, dict]:
    """One pass over the bot log files covering `days`: per ET day, Counter of
    fingerprints by severity, examples, starts/exits and traceback heads."""
    days = sorted(set(days))
    bounds = {d.isoformat(): et_bounds(d) for d in days}
    res = {iso: {"fp": Counter(), "sev": {}, "ex": {}, "starts": 0, "start_ts": [],
                 "exits": Counter()}
           for iso in bounds}
    files = sorted(set(p for d in days for p in _bot_log_files(d)))
    lo = min(b[0] for b in bounds.values())
    hi = max(b[1] for b in bounds.values())

    def bucket(ts):
        for iso, (a, b) in bounds.items():
            if a <= ts < b:
                return res[iso]
        return None

    for p in files:
        cur_ts = None
        tb_open = False
        try:
            fh = open(p, "rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                m = LOG_TS.match(raw)
                if m:
                    try:
                        cur_ts = datetime.strptime(m.group(1).decode() + " " + ":".join(
                            g.decode() for g in m.groups()[1:]), "%Y-%m-%d %H:%M:%S") \
                            .replace(tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        pass
                    tb_open = False
                if cur_ts is None or not (lo <= cur_ts < hi):
                    continue
                if raw.startswith(b"Traceback"):
                    tb_open = True
                    continue
                if tb_open and not m:
                    if raw[:1] in (b" ", b"\t"):
                        continue
                    tb_open = False
                    msg = raw.decode("utf-8", "replace").strip()
                    r = bucket(cur_ts)
                    if r is not None:
                        fp = "TRACEBACK " + fingerprint(msg)
                        r["fp"][fp] += 1
                        r["sev"][fp] = "TB"
                        r["ex"].setdefault(fp, msg[:300])
                    continue
                if not m:
                    continue
                body = raw[m.end():]
                sev = None
                if b" !! " in raw[:60] or body.lstrip().startswith(b"!!") or b"] !! " in body:
                    sev = "!!"
                elif b"ALERT [" in body:
                    sev = "ALERT"
                elif b"] ! " in body or body.startswith(b"! ") or b" ! " in body[:40]:
                    sev = "!"
                r = bucket(cur_ts)
                if r is None:
                    continue
                if b"kalshi account: key" in body:
                    r["starts"] += 1
                    r["start_ts"].append(cur_ts)
                if b"clean exit" in body:
                    r["exits"]["clean exit (code change)"] += 1
                elif b"hard-exiting" in body:
                    r["exits"]["watchdog hard exit"] += 1
                elif b"=== done ===" in body:
                    r["exits"]["done"] += 1
                if sev is None:
                    continue
                msg = body.decode("utf-8", "replace").strip()
                if sev == "ALERT":
                    mm = re.search(r"ALERT \[([^\]]+)\]", msg)
                    fp = "ALERT [" + (mm.group(1) if mm else "?") + "] " + fingerprint(
                        msg[mm.end():] if mm else msg)[:80]
                else:
                    fp = fingerprint(msg)
                r["fp"][fp] += 1
                r["sev"][fp] = sev
                r["ex"].setdefault(fp, msg[:300])
    return res


def update_signatures(scanned: Dict[str, dict]) -> dict:
    path = os.path.join(REVIEW_DIR, "error_signatures.json")
    db = _load_json(path, {}) or {}
    for iso, r in scanned.items():
        for fp, n in r["fp"].items():
            rec = db.setdefault(fp, {"days": {}, "first": iso, "sev": r["sev"].get(fp),
                                     "example": r["ex"].get(fp)})
            rec["days"][iso] = int(n)
            rec["first"] = min(rec.get("first") or iso, iso)
            rec["last"] = max(rec.get("last") or iso, iso)
    horizon = (date.today() - timedelta(days=90)).isoformat()
    for fp in list(db):
        db[fp]["days"] = {d: n for d, n in db[fp]["days"].items() if d >= horizon}
        if not db[fp]["days"]:
            del db[fp]
    _write_json(path, db)
    return db


def classify_signatures(db: dict, day: date) -> dict:
    iso = day.isoformat()
    lo = (day - timedelta(days=14)).isoformat()
    new, spikes, top, gone = [], [], [], []
    for fp, rec in db.items():
        days = rec.get("days") or {}
        n = days.get(iso, 0)
        prior = [c for d, c in days.items() if lo <= d < iso]
        if n:
            top.append((fp, n, rec.get("sev")))
            if not prior:
                new.append({"fp": fp, "n": n, "sev": rec.get("sev"), "example": rec.get("example")})
            else:
                med = statistics.median(prior)
                if n >= max(5, 3 * med):
                    spikes.append({"fp": fp, "n": n, "median_14d": med, "sev": rec.get("sev")})
        elif prior and len(prior) >= 5 and max(days) < iso:
            gone.append({"fp": fp, "last": max(days), "days_seen_14d": len(prior)})
    sev_rank = {"TB": 0, "!!": 1, "ALERT": 2, "!": 3}
    top.sort(key=lambda x: (sev_rank.get(x[2], 9), -x[1]))
    new.sort(key=lambda x: (sev_rank.get(x["sev"], 9), -x["n"]))
    return {"new": new[:25], "spikes": spikes[:15], "top": top[:30], "gone": gone[:15]}


def sec_system(day: date, bootstrap_days: int = 14) -> dict:
    out: dict = {}
    iso = day.isoformat()
    sig_db = _load_json(os.path.join(REVIEW_DIR, "error_signatures.json"), {}) or {}
    have = {d for rec in sig_db.values() for d in (rec.get("days") or {})}
    # the day itself, plus any of the prior days the history lacks (all 14 on
    # the first run; a day the job missed afterwards)
    want = [day] + [day - timedelta(days=i) for i in range(1, bootstrap_days + 1)
                    if (day - timedelta(days=i)).isoformat() not in have]
    if len(want) > 1:
        out["signatures_backfilled_days"] = len(want) - 1
    scanned = scan_bot_logs(want)
    db = update_signatures(scanned)
    r = scanned[iso]
    sev_tot = Counter()
    for fp, n in r["fp"].items():
        sev_tot[r["sev"].get(fp)] += n
    out["log"] = {"files": [os.path.basename(p) for p in _bot_log_files(day)],
                  "by_severity": dict(sev_tot), "starts": r["starts"], "exits": dict(r["exits"]),
                  **classify_signatures(db, day)}
    out["runs"] = config_runs(day)
    out["tasks"] = scheduled_tasks()
    out["task_logs"] = task_log_errors(day)
    out["health_alert"] = health_alert(day, r.get("start_ts") or [])
    out["sync"] = sync_status(day)
    out["git"] = git_state()
    out["machine"] = machine_state(day)
    out["retention"] = retention_status()
    return out


def retention_status(now: float = None) -> dict:
    """imm_log_retention's last run (R-261124-03) from retention/last_run.json,
    plus lifetime totals from its manifest. {"present": False} before the
    first run."""
    rdir = os.path.join(STATUS_DIR, "retention")
    lr = _load_json(os.path.join(rdir, "last_run.json"), None)
    if not isinstance(lr, dict):
        return {"present": False}
    now = time.time() if now is None else now
    ts = _ts_of(lr.get("ts"))
    out = {"present": True, "age_h": round((now - ts) / 3600.0, 1) if ts else None}
    for k in ("ts", "mode", "reason", "stopped", "cutoff_days", "candidates", "candidates_gb", "next_run",
              "next_run_gb", "compressed", "removed_plain", "plain_held", "in_gb", "freed_gb", "remaining",
              "remaining_gb", "held", "held_gb", "held_reasons", "skipped", "disk_free_gb"):
        out[k] = lr.get(k)
    out["failed"] = lr.get("failed") or []
    out["conflicts"] = lr.get("conflicts") or []
    files = restored = 0
    freed = 0
    try:
        with open(os.path.join(rdir, "manifest.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                act = r.get("action")
                if act == "compressed":
                    files += 1
                    freed += (r.get("size") or 0) - (r.get("gz_size") or 0)
                elif act == "removed_plain":
                    files += 1
                    freed += r.get("size") or 0
                elif act == "restored":
                    restored += 1
    except OSError:
        pass
    out.update(lifetime_files=files, lifetime_freed_gb=round(freed / 1e9, 1), lifetime_restored=restored)
    return out


_SETLIKE = re.compile(r"^\s*(?:frozenset\(|set\(|tuple\()?\s*[\[{(](.*)[\]})]\s*\)?\s*$", re.S)
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _set_items(v) -> Optional[List[str]]:
    """The items of a logged set/list/tuple repr ("frozenset({'B', 'A'})"),
    sorted, or None when the value is not one. Sets print in hash order, which
    changes from run to run, so raw reprs differ when nothing changed."""
    s = str(v) if v is not None else ""
    m = _SETLIKE.match(s)
    if not m or ("'" not in s and '"' not in s):
        return None
    return sorted(a or b for a, b in _QUOTED.findall(m.group(1)))


def config_runs(day: date) -> dict:
    s, e = et_bounds(day)
    rows = []
    for u in sorted(set(utc_dates(day) + [(day - timedelta(days=1)).isoformat()])):
        rows.extend(_iter_jsonl(os.path.join(STATUS_DIR, f"config_history_{u}.jsonl")))
    rows = sorted((r for r in rows if _ts_of(r.get("ts")) is not None), key=lambda r: _ts_of(r["ts"]))
    before = [r for r in rows if _ts_of(r["ts"]) < s]
    inside = [r for r in rows if s <= _ts_of(r["ts"]) < e]
    prev = before[-1] if before else None
    runs, changes = [], []
    for r in inside:
        runs.append({"at": et_str(_ts_of(r["ts"])), "run_id": r.get("run_id"),
                     "git_sha": r.get("git_sha"), "mode": r.get("mode")})
        if prev:
            a, b = prev.get("config") or {}, r.get("config") or {}
            for k in sorted(set(a) | set(b)):
                if k in ("IMM_HEARTBEAT_URL",) or a.get(k) == b.get(k):
                    continue
                ia, ib = _set_items(a.get(k)), _set_items(b.get(k))
                if ia is not None and ib is not None:
                    if ia == ib:
                        continue                  # same set, printed in another order
                    add, rem = sorted(set(ib) - set(ia)), sorted(set(ia) - set(ib))
                    old = ("-" + ",".join(rem))[:80] if rem else ""
                    new = ("+" + ",".join(add))[:80] if add else ""
                else:
                    old, new = str(a.get(k))[:80], str(b.get(k))[:80]
                changes.append({"at": et_str(_ts_of(r["ts"])), "key": k, "old": old, "new": new})
        prev = r
    return {"runs": runs, "env_changes": changes[:40]}


def _ps_json(script: str, timeout: int = 120):
    code, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                     timeout=timeout)
    if code != 0:
        raise RuntimeError(out.strip()[-300:])
    txt = out.strip()
    if not txt:
        return []
    obj = json.loads(txt[txt.find("["):] if txt.lstrip().startswith("[") or "[" in txt[:3] else txt)
    return obj if isinstance(obj, list) else [obj]


TASKS_PS = (r"$ErrorActionPreference='SilentlyContinue'; "
            r"Get-ScheduledTask | Where-Object { $_.TaskName -like 'KL*' } | ForEach-Object { "
            r"$i = $_ | Get-ScheduledTaskInfo; [pscustomobject]@{ n=$_.TaskName; s=[string]$_.State; "
            r"l= if ($i.LastRunTime) { $i.LastRunTime.ToString('s') } else { '' }; "
            r"r=$i.LastTaskResult; x= if ($i.NextRunTime) { $i.NextRunTime.ToString('s') } else { '' }; "
            r"m=$i.NumberOfMissedRuns } } | ConvertTo-Json -Compress")


def scheduled_tasks() -> dict:
    try:
        tasks = _ps_json(TASKS_PS)
    except Exception as e:
        return {"error": f"task query failed: {e}"}
    prev = _load_json(os.path.join(REVIEW_DIR, "task_state.json"), {}) or {}
    failing, disabled, running = [], [], []
    now_state = {}
    for t in tasks:
        name, state, code = t.get("n"), t.get("s"), int(_f(t.get("r")))
        now_state[name] = {"s": state, "r": code, "l": t.get("l")}
        if state == "Disabled":
            disabled.append(name)
            continue
        if state == "Running":
            running.append(name)
            continue
        if code not in OK_TASK_CODES:
            p = prev.get(name) or {}
            failing.append({"task": name, "result": code, "hex": hex(code & 0xFFFFFFFF),
                            "last_run": t.get("l"),
                            "chronic": bool(p) and int(_f(p.get("r"))) == code})
    _write_json(os.path.join(REVIEW_DIR, "task_state.json"), now_state)
    imm = [n for n in now_state if re.search(r"imm|incentive|portfolio|sync|sms|remote|pvd|cheap", n, re.I)]
    return {"count": len(tasks), "failing": failing, "disabled": sorted(disabled),
            "running": len(running), "imm_related": len(imm)}


_DATE_IN_LINE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def task_log_errors(day: date) -> List[dict]:
    keep = {day.isoformat(), (day + timedelta(days=1)).isoformat()}
    out = []
    for label, rel in TASK_LOGS:
        path = os.path.join(RUN_LOGS, *rel.split("/"))
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                fh.seek(max(0, size - 400_000))
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        cur = None
        hits: Counter = Counter()
        first = {}
        for line in text.splitlines():
            m = _DATE_IN_LINE.search(line[:30])
            if m:
                cur = m.group(1)
            if cur not in keep or not TASK_LOG_ERR.search(line):
                continue
            fp = fingerprint(_DATE_IN_LINE.sub("", line, count=1))[:140]
            hits[fp] += 1
            first.setdefault(fp, line.strip()[:240])
        if hits:
            out.append({"task": label, "log": rel,
                        "lines": [{"n": n, "line": first[fp]} for fp, n in hits.most_common(5)]})
    return out


def health_alert(day: date, starts: Optional[List[float]] = None) -> dict:
    """DOWN / recovered lines in the day; a DOWN within 10 min of a bot process
    start (a deploy restart) is counted apart, since the bot was only relaunching."""
    s, e = et_bounds(day)
    starts = starts or []
    path = os.path.join(STATUS_DIR, "health-alert.log")
    downs, recov, at_restart = [], 0, 0
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 3_000_000))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return {"error": "health-alert.log unreadable"}
    for line in text.splitlines():
        if "[IMM-HEALTH]" not in line or ("DOWN" not in line and "recovered" not in line):
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z", line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        if not (s <= ts < e):
            continue
        if "recovered" in line:
            recov += 1
        else:
            if any(-600 <= st - ts <= 600 for st in starts):
                at_restart += 1
            downs.append(et_str(ts) + " " + re.sub(r"[^\x20-\x7e]+", "-", line[m.end():]).strip()[:120])
    return {"down_lines": len(downs), "within_10min_of_restart": at_restart, "recovered": recov,
            "bot_starts": len(starts), "examples": downs[:8]}


def sync_status(day: date) -> dict:
    path = os.path.join(RUN_LOGS, "sync-kl-main.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip("\n").lstrip("\ufeff") for l in fh]
    except OSError:
        return {"error": "sync-kl-main.log unreadable"}
    runs = {}
    for l in lines:
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)", l)
        if not m:
            continue
        st = runs.setdefault(m.group(1), {"fail": False, "msg": ""})
        if m.group(2).startswith("!"):
            st["fail"] = True
            st["msg"] = st["msg"] or m.group(2)[:160]
    keys = sorted(runs)
    last_ok = next((k for k in reversed(keys) if not runs[k]["fail"]), None)
    streak = 0
    for k in reversed(keys):
        if runs[k]["fail"]:
            streak += 1
        else:
            break
    d0 = day.isoformat()
    in_day = [k for k in keys if k.startswith(d0)]
    return {"last_run": keys[-1] if keys else None, "last_success": last_ok,
            "failure_streak": streak, "last_failure_msg": runs[keys[-1]]["msg"] if streak else "",
            "runs_in_day": len(in_day), "fails_in_day": sum(1 for k in in_day if runs[k]["fail"])}


def git_state() -> dict:
    out: dict = {}
    code, br = _git("rev-parse", "--abbrev-ref", "HEAD")
    out["branch"] = br.strip() if code == 0 else None
    code, head = _git("rev-parse", "--short", "HEAD")
    out["head"] = head.strip() if code == 0 else None
    code, ab = _git("rev-list", "--left-right", "--count", "HEAD...origin/main")
    if code == 0 and ab.split():
        a, b = ab.split()[:2]
        out["ahead"], out["behind"] = int(a), int(b)
    code, unp = _git("log", "--format=%h %ad %s", "--date=format:%m-%d %H:%M", "origin/main..HEAD")
    out["unpushed"] = [l[:110] for l in unp.splitlines() if l.strip()][:20] if code == 0 else []
    code, st = _git("status", "--porcelain=v1")
    dirty, untracked = [], []
    if code == 0:
        for l in st.splitlines():
            if not l.strip():
                continue
            path = l[3:].strip().strip('"')
            if l.startswith("??"):
                if not path.startswith(("run-logs/", "__pycache__")) and not path.endswith(".pyc"):
                    untracked.append(path)
            else:
                try:
                    age_h = (time.time() - os.path.getmtime(os.path.join(LIVE_REPO, path))) / 3600.0
                except OSError:
                    age_h = None
                dirty.append((l[:2].strip() + " " + path, age_h))
    # an edit hours old is most likely a session at work (leave it to them); one
    # untouched for a day is a forgotten commit the sync can clobber
    out["dirty_tracked"] = [f"{p} ({'?' if a is None else f'{a:.0f}h'} old)" for p, a in dirty[:40]]
    out["dirty_stale_24h"] = [p for p, a in dirty if a is not None and a >= 24]
    out["dirty_count"] = len(dirty)
    out["untracked_count"] = len(untracked)
    code, wt = _git("worktree", "list", "--porcelain")
    paths = [l[9:].strip() for l in wt.splitlines() if l.startswith("worktree ")] if code == 0 else []
    out["worktrees"] = len(paths)
    out["worktrees_missing"] = sum(1 for p in paths if not os.path.isdir(p))
    return out


def machine_state(day: date) -> dict:
    out: dict = {}
    try:
        du = shutil.disk_usage(STATUS_DIR)
        out["disk_free_gb"] = round(du.free / 1e9, 1)
        out["disk_total_gb"] = round(du.total / 1e9, 1)
    except OSError:
        pass
    total = 0
    per_day = defaultdict(int)
    recent = {(day - timedelta(days=i)).isoformat() for i in range(7)}
    tmp_files = []
    for root, _dirs, files in os.walk(STATUS_DIR):
        if os.path.normpath(root).startswith(os.path.normpath(REVIEW_DIR)):
            continue
        for n in files:
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            total += st.st_size
            m = re.search(r"(\d{4}-\d{2}-\d{2})", n)
            if m and m.group(1) in recent:
                per_day[m.group(1)] += st.st_size
            if n.endswith(".tmp") and root == STATUS_DIR or n.endswith(".html.tmp"):
                if time.time() - st.st_mtime > 900:
                    tmp_files.append(n)
    out["status_dir_gb"] = round(total / 1e9, 1)
    growth = sorted(per_day.values())
    if growth:
        med = growth[len(growth) // 2]
        out["growth_gb_day"] = round(med / 1e9, 2)
        if med > 0 and "disk_free_gb" in out:
            out["days_to_full"] = round(out["disk_free_gb"] / (med / 1e9), 0)
    out["stale_tmp"] = tmp_files[:10]
    tok = os.path.join(os.path.expanduser("~"), ".claude", "imm-research-token.txt")
    try:
        out["token_age_days"] = round((time.time() - os.path.getmtime(tok)) / 86400.0, 0)
    except OSError:
        out["token_age_days"] = None
    summ = _load_json(os.path.join(DASH_DIR, "imm_dashboard_summary.json"), {}) or {}
    if summ.get("generated"):
        out["dashboard_age_min"] = round((time.time() - _f(summ["generated"])) / 60.0, 0)
    st = _load_json(os.path.join(STATUS_DIR, "status_incentive_mm.json"), {}) or {}
    if st.get("updated_at"):
        out["status_age_min"] = round((time.time() - (_ts_of(st["updated_at"]) or 0)) / 60.0, 1)
    mem = ((st.get("power") or {}).get("memory") if isinstance(st.get("power"), dict) else None)
    if mem:
        out["memory"] = mem
    return out


# ---------------------------------------------------------------------------
# 7. Data health
# ---------------------------------------------------------------------------

SINK_NOISE_FLOOR_BYTES = 500_000      # usual day under 0.5 MB: event-driven, size is not health
_SINK_FILE = re.compile(r"([a-z][a-z0-9_]*?)_(\d{4}-\d{2}-\d{2})(?:_[A-Za-z0-9.]+)?\.(?:jsonl|csv)$")


def sink_sizes(u: str) -> Dict[str, int]:
    out = defaultdict(int)
    for n in os.listdir(STATUS_DIR):
        m = _SINK_FILE.match(n)
        if m and m.group(2) == u:
            try:
                out[m.group(1)] += os.path.getsize(os.path.join(STATUS_DIR, n))
            except OSError:
                pass
    bd = os.path.join(STATUS_DIR, "book_depth")
    try:
        for n in os.listdir(bd):
            if n.startswith(f"book_depth_{u}"):
                out["book_depth"] += os.path.getsize(os.path.join(bd, n))
    except OSError:
        pass
    return dict(out)


def sec_data(day: date) -> dict:
    u = day.isoformat()
    hist = {}
    for i in range(0, 15):
        d = (day - timedelta(days=i)).isoformat()
        hist[d] = sink_sizes(d)
    rows, alerts = [], []
    prefixes = set(SINKS)
    for d in hist.values():
        prefixes |= set(d)
    for p in sorted(prefixes):
        cur = hist[u].get(p, 0)
        prior = [hist[d].get(p, 0) for d in hist if d != u]
        present = [x for x in prior if x > 0]
        med = statistics.median(present) if present else 0
        ratio = round(cur / med, 2) if med else None
        status = "ok"
        if cur == 0 and len(present) >= 10:
            status = "MISSING"
        elif (ratio is not None and len(present) >= 5 and med >= SINK_NOISE_FLOOR_BYTES
              and (ratio < 0.3 or ratio > 3.0)):
            # (a sink whose usual day is under the noise floor is event-driven --
            # twin_check, ws_group, alone_front: its size swings with the day's
            # events, so LOW/HIGH there is noise; the first run flagged three)
            status = "LOW" if ratio < 0.3 else "HIGH"
        elif cur == 0 and not present:
            continue
        rows.append({"sink": p, "mb": round(cur / 1e6, 1), "median_mb": round(med / 1e6, 1),
                     "ratio": ratio, "days_present_14": len(present), "status": status})
        if status != "ok":
            alerts.append(p + " " + status)
    doc = ""
    try:
        with open(os.path.join(LIVE_REPO, "IMM_LOGGING.md"), encoding="utf-8", errors="replace") as fh:
            doc = fh.read()
    except OSError:
        pass
    undocumented = sorted(p for p in hist[u] if doc and p not in doc and p not in (
        "incentive", "imm_review"))
    return {"utc_day": u, "sinks": rows, "alerts": alerts, "undocumented": undocumented}


# ---------------------------------------------------------------------------
# 8. Changes: commits, env, reloadable config files
# ---------------------------------------------------------------------------

# The reloadable inputs a person or a session sets (lists, dates, overrides,
# switch files, gates) -- not model outputs (*_fair.json), state or reports,
# which change all day and would bury the real changes.
CONFIG_INCLUDE = re.compile(
    r"(^HALT|^UNWIND|_stop\.(txt|json)$|dates_extra|_series\.json$|^cheap_side_books\.json$|"
    r"^env_overrides\.txt$|^event_start_overrides|verdicts|_gate\.json$|"
    r"^release_calendar_found\.json$|^astest_picks\.json$|^post_vote_dead_books\.json$|"
    r"freeze|frozen|blocklist|allow)", re.I)
CONFIG_SKIP = re.compile(r"^daily_series\.json$|\.tmp$")
CONFIG_MAX_BYTES = 16_000_000


def config_candidates() -> List[str]:
    out = []
    for n in sorted(os.listdir(STATUS_DIR)):
        p = os.path.join(STATUS_DIR, n)
        if (not os.path.isfile(p) or not CONFIG_INCLUDE.search(n) or CONFIG_SKIP.search(n)
                or re.search(r"\d{4}-\d{2}-\d{2}", n)):
            continue
        try:
            if os.path.getsize(p) > CONFIG_MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(n)
    return out


def _json_diff(a, b) -> str:
    if isinstance(a, dict) and isinstance(b, dict):
        add = sorted(set(b) - set(a))
        rem = sorted(set(a) - set(b))
        chg = sorted(k for k in set(a) & set(b) if a[k] != b[k])
        parts = []
        if add:
            parts.append(f"+{len(add)} keys ({', '.join(map(str, add[:6]))})")
        if rem:
            parts.append(f"-{len(rem)} keys ({', '.join(map(str, rem[:6]))})")
        if chg:
            parts.append(f"~{len(chg)} changed ({', '.join(map(str, chg[:6]))})")
        return "; ".join(parts) or "reordered"
    if isinstance(a, list) and isinstance(b, list):
        sa = {json.dumps(x, sort_keys=True) for x in a}
        sb = {json.dumps(x, sort_keys=True) for x in b}
        add, rem = sb - sa, sa - sb
        ex = [x[:60] for x in list(add)[:3]]
        return f"+{len(add)} / -{len(rem)} items" + (f" (e.g. {', '.join(ex)})" if ex else "")
    return "value changed"


def snapshot_config(day: date) -> dict:
    store = os.path.join(REVIEW_DIR, "config_store")
    os.makedirs(store, exist_ok=True)
    man_path = os.path.join(REVIEW_DIR, "config_manifest.jsonl")
    prev = None
    for r in _iter_jsonl(man_path):
        if r.get("day") < day.isoformat():
            prev = r
    files = {}
    for n in config_candidates():
        p = os.path.join(STATUS_DIR, n)
        try:
            with open(p, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        h = hashlib.sha1(data).hexdigest()
        files[n] = h
        dst = os.path.join(store, h + ".gz")
        if not os.path.exists(dst):
            with gzip.open(dst, "wb") as fh:
                fh.write(data)
    with open(man_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"day": day.isoformat(), "at": time.time(), "files": files}) + "\n")
    out = {"tracked_files": len(files), "changes": []}
    if not prev:
        out["note"] = "first snapshot: nothing to compare yet"
        return out
    pf = prev.get("files") or {}
    for n in sorted(set(pf) | set(files)):
        a, b = pf.get(n), files.get(n)
        if a == b:
            continue
        if a is None:
            out["changes"].append({"file": n, "change": "added"})
        elif b is None:
            out["changes"].append({"file": n, "change": "removed"})
        else:
            desc = "changed"
            try:
                with gzip.open(os.path.join(store, a + ".gz"), "rt", encoding="utf-8") as fa, \
                        gzip.open(os.path.join(store, b + ".gz"), "rt", encoding="utf-8") as fb:
                    desc = _json_diff(json.load(fa), json.load(fb))
            except Exception:
                pass
            out["changes"].append({"file": n, "change": desc})
    out["compared_with"] = prev.get("day")
    return out


def commits_in(day: date, ref: str) -> List[dict]:
    s, e = et_bounds(day)
    since = datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    code, out = _git("log", ref, f"--since={since}", f"--until={until}",
                     "--format=%x1e%h%x1f%ct%x1f%s%x1f%b%x1d", "--name-only")
    if code != 0:
        return []
    return parse_git_log(out)


def parse_git_log(out: str) -> List[dict]:
    """Records of `--format=%x1e%h%x1f%ct%x1f%s%x1f%b%x1d --name-only`: the body
    may span lines, so it ends at \\x1d and the file names follow it."""
    res = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        head, _, files = chunk.partition("\x1d")
        parts = head.split("\x1f")
        if len(parts) < 3:
            continue
        body = parts[3] if len(parts) > 3 else ""
        names = [l.strip() for l in files.splitlines() if l.strip()]
        res.append({"sha": parts[0].strip(), "at": et_str(_f(parts[1])), "subject": parts[2][:160],
                    "ids": sorted(set(LEDGER_ID.findall(parts[2] + " " + body))),
                    "bot": "incentive_mm.py" in names, "files": len(names)})
    return res


def sec_changes(day: date) -> dict:
    seen, commits = set(), []
    for ref in ("HEAD", "origin/main"):
        for c in commits_in(day, ref):
            if c["sha"] not in seen:
                seen.add(c["sha"])
                commits.append(c)
    commits.sort(key=lambda c: c["at"])
    return {"commits": commits[:60], "n_commits": len(commits),
            "n_bot": sum(1 for c in commits if c["bot"]),
            "config_files": snapshot_config(day)}


# ---------------------------------------------------------------------------
# Ledger (append-only findings memory)
# ---------------------------------------------------------------------------

def ledger_path() -> str:
    return os.path.join(REVIEW_DIR, "ledger.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_append(ev: dict) -> None:
    os.makedirs(REVIEW_DIR, exist_ok=True)
    ev = dict(ev)
    ev.setdefault("ts", _now_iso())
    with open(ledger_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, sort_keys=True, default=str) + "\n")


def ledger_state() -> Dict[str, dict]:
    items: Dict[str, dict] = {}
    for ev in _iter_jsonl(ledger_path()):
        op, iid = ev.get("op"), ev.get("id")
        if not iid:
            continue
        if op == "create":
            it = {k: v for k, v in ev.items() if k not in ("op",)}
            it.setdefault("status", "open")
            it.setdefault("seen_days", [ev.get("day")])
            it.setdefault("decisions", [])
            it.setdefault("evals", [])
            items[iid] = it
            continue
        it = items.get(iid)
        if it is None:
            continue
        if op == "seen":
            d = ev.get("day")
            if d and d not in it["seen_days"]:
                it["seen_days"].append(d)
            it["last_seen"] = d
            for k in ("severity", "title", "summary", "impact", "evidence", "ask", "plan", "metric", "email"):
                if ev.get(k):
                    it[k] = ev[k]
            if ev.get("status"):
                it["status"] = ev["status"]
        elif op == "decide":
            it["decisions"].append({k: ev.get(k) for k in ("ts", "status", "by", "note")})
            it["status"] = ev.get("status") or it["status"]
        elif op == "ship":
            it["ship"] = {"ref": ev.get("ref"), "at": ev.get("at") or ev.get("ts"),
                          "by": ev.get("by"), "note": ev.get("note")}
            if it["status"] not in TERMINAL:
                it["status"] = "shipped"
        elif op == "eval":
            it["evals"].append({k: ev.get(k) for k in ("ts", "day", "verdict", "before", "after",
                                                       "delta_usd_day", "note")})
            v = ev.get("verdict")
            if v in ("verified", "failed", "inconclusive"):
                it["status"] = v
        elif op == "close":
            it["status"] = ev.get("status") or "closed"
            it["closed"] = {"ts": ev.get("ts"), "note": ev.get("note")}
        elif op == "update":
            for k, v in (ev.get("fields") or {}).items():
                it[k] = v
    return items


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def next_id(day: date, items: Dict[str, dict]) -> str:
    pre = "R-" + day.strftime("%y%m%d") + "-"
    n = sum(1 for k in items if k.startswith(pre))
    return f"{pre}{n + 1:02d}"


def _clean_finding(f: dict) -> dict:
    kind = f.get("kind") if f.get("kind") in KINDS else "review"
    sev = str(f.get("severity") or "P3").upper()
    if sev not in ("P0", "P1", "P2", "P3"):
        sev = "P3"
    fp = _slug(f.get("fingerprint") or (kind + "-" + (f.get("title") or "untitled")))
    imp = f.get("impact") if isinstance(f.get("impact"), dict) else {}
    em = f.get("email") if isinstance(f.get("email"), dict) else {}
    ev = f.get("evidence") if isinstance(f.get("evidence"), list) else (
        [str(f["evidence"])] if f.get("evidence") else [])
    return {"kind": kind, "severity": sev, "fingerprint": fp,
            "title": str(f.get("title") or fp)[:160], "summary": str(f.get("summary") or "")[:1500],
            "evidence": [str(x)[:300] for x in ev[:10]],
            "impact": {"usd_day": _f(imp.get("usd_day")) if imp.get("usd_day") is not None else None,
                       "basis": imp.get("basis") or "n/a", "confidence": imp.get("confidence") or "low"},
            "decision_needed": bool(f.get("decision_needed")),
            "ask": str(f.get("ask") or "")[:300],
            "plan": f.get("plan") if isinstance(f.get("plan"), dict) else (
                {"change": str(f["plan"])} if f.get("plan") else {}),
            "metric": f.get("metric") if isinstance(f.get("metric"), dict) else {},
            "email": {k: str(em.get(k))[:260] for k in ("headline", "why", "fix", "value") if em.get(k)},
            "ledger_id": f.get("ledger_id")}


def ledger_ingest(day: date, path: Optional[str] = None) -> dict:
    iso = day.isoformat()
    path = path or os.path.join(REVIEW_DIR, iso, "findings.json")
    data = _load_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"findings file missing or not a JSON object: {path}")
    items = ledger_state()
    by_fp = {}
    for iid, it in items.items():
        if it.get("status") not in TERMINAL:
            by_fp[it.get("fingerprint")] = iid
    ids, created, seen = {}, [], []
    for raw in data.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        f = _clean_finding(raw)
        iid = f["ledger_id"] if f["ledger_id"] in items else by_fp.get(f["fingerprint"])
        body = {k: f[k] for k in ("kind", "severity", "fingerprint", "title", "summary",
                                  "evidence", "impact", "ask", "plan", "metric", "email")}
        if iid:
            it = items[iid]
            if iso not in (it.get("seen_days") or []):
                ev = {"op": "seen", "id": iid, "day": iso, **body}
                if f["decision_needed"] and it.get("status") == "open":
                    ev["status"] = "proposed"
                ledger_append(ev)
                it.setdefault("seen_days", []).append(iso)
            seen.append(iid)
        else:
            iid = next_id(day, items)
            ev = {"op": "create", "id": iid, "day": iso, "first_seen": iso, "last_seen": iso,
                  "status": "proposed" if f["decision_needed"] else "open", **body}
            ledger_append(ev)
            items[iid] = dict(ev, seen_days=[iso], decisions=[], evals=[])
            by_fp[f["fingerprint"]] = iid
            created.append(iid)
        ids[f["fingerprint"]] = iid
    evals = 0
    for e in data.get("evaluations") or []:
        if isinstance(e, dict) and e.get("id") in items:
            ledger_append({"op": "eval", "id": e["id"], "day": iso,
                           "verdict": e.get("verdict") if e.get("verdict") in (
                               "verified", "failed", "inconclusive") else "inconclusive",
                           "before": e.get("before"), "after": e.get("after"),
                           "delta_usd_day": e.get("delta_usd_day"), "note": str(e.get("note") or "")[:600]})
            evals += 1
    closes = 0
    for c in data.get("closures") or []:
        if isinstance(c, dict) and c.get("id") in items:
            st = c.get("status") if c.get("status") in TERMINAL else "closed"
            ledger_append({"op": "close", "id": c["id"], "status": st,
                           "note": str(c.get("note") or "")[:600]})
            closes += 1
    _write_json(os.path.join(REVIEW_DIR, iso, "ids.json"), ids)
    _write_json(os.path.join(REVIEW_DIR, iso, "ingested.json"),
                {"at": _now_iso(), "created": created, "seen": seen, "evals": evals, "closes": closes})
    lessons = [str(x).strip() for x in data.get("lessons") or [] if str(x).strip()]
    if lessons:
        with open(os.path.join(REVIEW_DIR, "lessons.md"), "a", encoding="utf-8") as fh:
            for l in lessons[:5]:
                fh.write(f"- {iso}: {l[:400]}\n")
    # PROCESS MOAT: a verified fix becomes a reusable rule the next runs apply
    # to other families and new waves (moat/playbook.md, one line per rule)
    rules = [p for p in data.get("playbook") or [] if isinstance(p, dict) and str(p.get("rule") or "").strip()]
    if rules:
        os.makedirs(MOAT_DIR, exist_ok=True)
        path = os.path.join(MOAT_DIR, "playbook.md")
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            if new:
                fh.write("# IMM playbook: rules that verified (appended by imm_review.py ledger ingest)\n\n")
            for p in rules[:5]:
                fh.write(f"- {iso} [{str(p.get('evidence') or '')[:40]}] "
                         f"{str(p.get('applies_to') or 'all')[:80]}: {str(p['rule']).strip()[:400]}\n")
    return {"created": created, "seen": seen, "evals": evals, "closes": closes, "ids": ids}


def ledger_due(day: date) -> List[dict]:
    out = []
    for iid, it in ledger_state().items():
        if it.get("status") != "shipped" or not it.get("ship"):
            continue
        at = _ts_of(it["ship"].get("at"))
        if at is None:
            continue
        wait = _f((it.get("metric") or {}).get("eval_after_days"), 7)
        due_day = datetime.fromtimestamp(at, ET).date() + timedelta(days=int(wait))
        last_eval = max((e.get("day") or "" for e in it.get("evals") or []), default="")
        if due_day <= day and last_eval < due_day.isoformat():
            out.append({"id": iid, "title": it.get("title"), "shipped": et_str(at),
                        "due": due_day.isoformat(), "metric": it.get("metric")})
    return out


def ledger_stats() -> dict:
    items = ledger_state()
    by_status = Counter(it.get("status") for it in items.values())
    by_kind = Counter(it.get("kind") for it in items.values())
    verified = [it for it in items.values() if it.get("status") == "verified"]
    gain = sum(_f((it.get("evals") or [{}])[-1].get("delta_usd_day")) for it in verified)
    decided = [it for it in items.values() if it.get("decisions")]
    approved = sum(1 for it in decided if any(d.get("status") == "approved" for d in it["decisions"]))
    return {"items": len(items), "by_status": dict(by_status), "by_kind": dict(by_kind),
            "verified_delta_usd_day": round(gain, 2), "decided": len(decided), "approved": approved,
            "failed": by_status.get("failed", 0)}


def sec_ledger(day: date, commits: List[dict]) -> dict:
    items = ledger_state()
    shipped_now = []
    for c in commits:
        for iid in c.get("ids") or []:
            it = items.get(iid)
            if it and it.get("status") in ("open", "proposed", "approved", "deferred"):
                ledger_append({"op": "ship", "id": iid, "ref": c["sha"], "by": "git",
                               "note": "commit subject names the id: " + c["subject"][:100]})
                shipped_now.append(iid)
    items = ledger_state()
    open_items = [it for it in items.values() if it.get("status") not in TERMINAL]
    awaiting = [{"id": it["id"], "title": it.get("title"), "since": it.get("first_seen"),
                 "severity": it.get("severity")}
                for it in open_items if it.get("status") == "proposed"]
    awaiting.sort(key=lambda x: (x["severity"], x["since"] or ""))
    return {"open": len(open_items), "awaiting_decision": awaiting[:20],
            "due": ledger_due(day), "shipped_detected": shipped_now, "stats": ledger_stats(),
            "open_list": [{"id": it["id"], "status": it.get("status"), "kind": it.get("kind"),
                           "severity": it.get("severity"), "title": it.get("title"),
                           "fingerprint": it.get("fingerprint"),
                           "seen": len(it.get("seen_days") or [])}
                          for it in sorted(open_items, key=lambda x: (x.get("severity") or "P9",
                                                                     x.get("id")))][:60]}


# ---------------------------------------------------------------------------
# The moat: what compounds (Jack 2026-11-25: "the skill's daily iterative
# process should lead to compounding and building a moat on data, analysis,
# and process that allows it to continually get better even as the markets
# get more competitive")
#
# DATA      moat/family_daily.csv   one row per (ET day, family), kept forever:
#                                   rewards, trading, net, fills, contracts, $ resting,
#                                   mark-outs, markets, and our SHARE of the reward pool
#                                   from the cycle log -- the competition signal no one
#                                   else has for our book
# ANALYSIS  moat/priors.json        per family over 30 days, shrunk toward the book:
#                                   rewards and net per $1k resting, the trading tax,
#                                   mark-out, share. The counterfactual nets its
#                                   missed-market estimates with each family's tax.
#           edge watch              last 7 vs the prior 21 days per family: where other
#                                   makers are eating the edge, where it is growing
#           review calibration      the review's predicted $/day vs what verified, by kind
# PROCESS   moat/playbook.md        verified fixes as reusable rules (ingest appends
#                                   findings.json "playbook"); the skill applies them
# ---------------------------------------------------------------------------

MOAT_PRIOR_DAYS = 30
MOAT_PRIOR_SHRINK = 5.0      # days of the book's own average blended into each family's prior
MOAT_SHARE_BACKFILL = 7      # first run only: the cycle-log share for this many prior days
EDGE_RECENT, EDGE_BASE = 7, 21
FAMILY_DAILY_COLS = ["day", "family", "rew", "pnl", "net", "fills", "cts", "rest",
                     "fe", "fe_cts", "m5", "m5_cts", "mk", "mk_cts", "m4", "m4_cts",
                     "mt", "mt_cts", "markets", "markets_rew", "share", "pool_cov"]


def family_rows_for(iso: str) -> Dict[str, dict]:
    """{family: sums} for one archived ET day."""
    blob = load_archive(iso)
    if not blob:
        return {}
    fams: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t, v in market_rows(blob):
        a = fams[_fam_key(t)]
        for k in ("rew", "pnl", "fills", "cts", "rest", "mk", "mk_cts") + FX_KEYS:
            a[k] += _f(v.get(k))
        a["markets"] += 1
        if _f(v.get("rew")) > 0:
            a["markets_rew"] += 1
    out = {}
    for f, a in fams.items():
        a = dict(a)
        a["net"] = a.get("rew", 0.0) + a.get("pnl", 0.0)
        out[f] = a
    return out


def cycle_share(day: date) -> Dict[str, dict]:
    """{family: {"share", "pool_cov"}} for an ET day from the cycle log: our
    pool-weighted fraction of the reward pool (sum est_frac x pool / sum pool over
    every managed market-cycle) and the average pool $/day under management. A
    share falling at a steady size is other makers crowding in."""
    s, e = et_bounds(day)
    num: Dict[str, float] = defaultdict(float)
    den: Dict[str, float] = defaultdict(float)
    cycles: Dict[str, set] = defaultdict(set)
    for u in utc_dates(day):
        try:
            fh = imm_logfiles.open_text(imm_logfiles.resolve(os.path.join(STATUS_DIR, f"cycle_log_{u}.csv")),
                                        encoding="utf-8", errors="replace")
        except OSError:
            continue
        idx = None
        last_s, last_ts = None, None
        with fh:
            for line in fh:
                if line.startswith("ts,"):
                    cols = line.rstrip("\r\n").split(",")
                    try:
                        idx = (cols.index("ts"), cols.index("ticker"), cols.index("est_frac"),
                               cols.index("pool_per_day"))
                    except ValueError:
                        idx = None
                    continue
                if idx is None:
                    continue
                parts = line.split(",")
                if len(parts) <= max(idx):
                    continue
                ts_s = parts[idx[0]]
                if ts_s != last_s:
                    last_s = ts_s
                    try:
                        last_ts = datetime.strptime(ts_s, "%Y-%m-%dT%H:%M:%SZ") \
                            .replace(tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        last_ts = None
                if last_ts is None or not (s <= last_ts < e):
                    continue
                pool = _f(parts[idx[3]])
                if pool <= 0:
                    continue
                fam = _fam_key(parts[idx[1]])
                num[fam] += _f(parts[idx[2]]) * pool
                den[fam] += pool
                cycles[fam].add(ts_s)
    return {f: {"share": round(num[f] / den[f], 5), "pool_cov": round(den[f] / max(1, len(cycles[f])), 2)}
            for f in den if den[f] > 0}


def _read_rows(path: str) -> List[dict]:
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def _write_rows(path: str, rows: List[dict], cols: List[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    os.replace(tmp, path)


def _window_stats(rows: List[dict]) -> dict:
    """Ratios over a set of family-day rows: per $1k resting (rest is a day's
    average $ resting, so sum(rest)/1000 is $1k-days), the trading tax, mark-out."""
    S = defaultdict(float)
    shares = []
    for r in rows:
        for k in ("rew", "pnl", "rest", "mt", "mt_cts", "fills"):
            S[k] += _f(r.get(k))
        if r.get("share") not in (None, ""):
            shares.append(_f(r["share"]))
    kd = S["rest"] / 1000.0
    return {"days": len(rows),
            "rew_per_1k": round(S["rew"] / kd, 4) if kd > 0 else None,
            "net_per_1k": round((S["rew"] + S["pnl"]) / kd, 4) if kd > 0 else None,
            "tax": round(-S["pnl"] / S["rew"], 4) if S["rew"] > 0 else None,
            "mt_c": cpc(S["mt"], S["mt_cts"]),
            "share": round(sum(shares) / len(shares), 5) if shares else None,
            "share_days": len(shares),
            "rew": round(S["rew"], 2), "net": round(S["rew"] + S["pnl"], 2)}


def compute_priors(rows: List[dict], day: date) -> dict:
    """Per family over the last MOAT_PRIOR_DAYS, each ratio blended with the
    book's own value: (n * family + m * book) / (n + m), m = MOAT_PRIOR_SHRINK.
    A family with a few days leans on the book; one with a month stands alone."""
    iso = day.isoformat()
    lo = (day - timedelta(days=MOAT_PRIOR_DAYS - 1)).isoformat()
    sel = [r for r in rows if lo <= r["day"] <= iso and _f(r.get("rest")) > 0]
    book = _window_stats(sel)
    by_f: Dict[str, List[dict]] = defaultdict(list)
    for r in sel:
        by_f[r["family"]].append(r)
    fams = {}
    m = MOAT_PRIOR_SHRINK
    for f, rs in by_f.items():
        st = _window_stats(rs)
        n = st["days"]
        blended = {}
        for k in ("rew_per_1k", "net_per_1k", "tax", "mt_c"):
            fv, bv = st.get(k), book.get(k)
            if fv is None:
                blended[k] = bv
            elif bv is None:
                blended[k] = fv
            else:
                blended[k] = round((n * fv + m * bv) / (n + m), 4)
        fams[f] = {"raw": st, "prior": blended, "days": n}
    return {"as_of": iso, "window_days": MOAT_PRIOR_DAYS, "shrink_days": m, "book": book, "families": fams}


def edge_watch(rows: List[dict], day: date) -> List[dict]:
    """Families whose edge moved: the last EDGE_RECENT days against the EDGE_BASE
    before them -- rewards per $1k resting (falls when other makers crowd the
    books we sit in), our pool share when the cycle log gave it, net per $1k."""
    iso = day.isoformat()
    r_lo = (day - timedelta(days=EDGE_RECENT - 1)).isoformat()
    b_lo = (day - timedelta(days=EDGE_RECENT + EDGE_BASE - 1)).isoformat()
    by_f: Dict[str, Tuple[List[dict], List[dict]]] = defaultdict(lambda: ([], []))
    for r in rows:
        if _f(r.get("rest")) <= 0:
            continue
        if r_lo <= r["day"] <= iso:
            by_f[r["family"]][0].append(r)
        elif b_lo <= r["day"] < r_lo:
            by_f[r["family"]][1].append(r)
    out = []
    for f, (rec, base) in by_f.items():
        if len(rec) < 4 or len(base) < 10:
            continue
        a, b = _window_stats(rec), _window_stats(base)

        def ratio(k):
            x, y = a.get(k), b.get(k)
            return round(x / y, 3) if x is not None and y not in (None, 0) and y > 0 else None
        rr = ratio("rew_per_1k")
        # share leads only when both windows have it on enough days (the cycle
        # log share starts with the review: one base day would be noise)
        sr = ratio("share") if a.get("share_days", 0) >= 4 and b.get("share_days", 0) >= 4 else None
        lead = sr if sr is not None else rr
        if lead is None:
            continue
        verdict = "eroding" if lead <= 0.75 else "strengthening" if lead >= 1.33 else "steady"
        out.append({"family": f, "verdict": verdict, "rew_per_1k_ratio": rr, "share_ratio": sr,
                    "recent": a, "base": b, "lead": lead})
    out.sort(key=lambda x: -abs(math.log(max(x["lead"], 1e-6))))
    return out


def review_calibration() -> dict:
    """The review's own record: for findings that were verified or failed with a
    measured $/day, the realised / predicted ratio by kind."""
    by_kind: Dict[str, List[float]] = defaultdict(list)
    for it in ledger_state().values():
        if it.get("status") not in ("verified", "failed"):
            continue
        pred = _f((it.get("impact") or {}).get("usd_day"))
        ev = (it.get("evals") or [{}])[-1]
        if pred > 0 and ev.get("delta_usd_day") is not None:
            by_kind[it.get("kind") or "?"].append(_f(ev["delta_usd_day"]) / pred)
    return {k: {"n": len(v), "median_ratio": round(sorted(v)[len(v) // 2], 3)} for k, v in by_kind.items()}


def sec_moat(day: date) -> dict:
    os.makedirs(MOAT_DIR, exist_ok=True)
    path = os.path.join(MOAT_DIR, "family_daily.csv")
    rows = {(r["day"], r["family"]): r for r in _read_rows(path)}
    have_days = {d for d, _ in rows}
    iso = day.isoformat()
    totals = all_day_totals()
    archived = [d for d in archive_days() if d <= iso and (totals.get(d) or {}).get("ok", True)]
    todo = [d for d in archived if d not in have_days or d == iso]
    share_days = {iso} if have_days else set(archived[-(MOAT_SHARE_BACKFILL + 1):])
    added = 0
    for d in todo:
        fr = family_rows_for(d)
        sh = cycle_share(date.fromisoformat(d)) if d in share_days else {}
        for f, a in fr.items():
            old = rows.get((d, f)) or {}
            row = {"day": d, "family": f, "markets": int(a.get("markets", 0)),
                   "markets_rew": int(a.get("markets_rew", 0))}
            for k in ("rew", "pnl", "net", "fills", "cts", "rest", "mk", "mk_cts") + FX_KEYS:
                row[k] = round(_f(a.get(k)), 4)
            s = sh.get(f) or {}
            row["share"] = s.get("share", old.get("share") or None)
            row["pool_cov"] = s.get("pool_cov", old.get("pool_cov") or None)
            rows[(d, f)] = row
            added += 1
    ordered = [rows[k] for k in sorted(rows)]
    _write_rows(path, ordered, FAMILY_DAILY_COLS)
    priors = compute_priors(ordered, day)
    _write_json(os.path.join(MOAT_DIR, "priors.json"), priors)
    edge = edge_watch(ordered, day)
    playbook = os.path.join(MOAT_DIR, "playbook.md")
    try:
        with open(playbook, encoding="utf-8") as fh:
            n_rules = sum(1 for l in fh if l.startswith("- "))
    except OSError:
        n_rules = 0
    days = sorted({r["day"] for r in ordered})
    assets = {
        "family_days": len(ordered), "days": len(days), "first_day": days[0] if days else None,
        "share_days": len({r["day"] for r in ordered if r.get("share") not in (None, "")}),
        "predictions": sum(1 for _ in _iter_jsonl(os.path.join(REVIEW_DIR, "counterfactual.jsonl"))),
        "findings": len(ledger_state()), "playbook_rules": n_rules,
        "signatures": len(_load_json(os.path.join(REVIEW_DIR, "error_signatures.json"), {}) or {}),
        "config_versions": sum(1 for _ in _iter_jsonl(os.path.join(REVIEW_DIR, "config_manifest.jsonl"))),
    }
    today = [r for r in ordered if r["day"] == iso]
    return {"assets": assets, "rows_added": added,
            "edge": [e for e in edge if e["verdict"] != "steady"][:10],
            "edge_steady": sum(1 for e in edge if e["verdict"] == "steady"),
            "priors_book": priors["book"],
            "priors_top": sorted(({"family": f, **v["prior"], "days": v["days"]}
                                  for f, v in priors["families"].items()),
                                 key=lambda x: -_f(x.get("net_per_1k")))[:20],
            "share_today": {r["family"]: r.get("share") for r in today if r.get("share") not in (None, "")},
            "review_calibration": review_calibration()}


# ---------------------------------------------------------------------------
# Before/after measurement for loop closure
# ---------------------------------------------------------------------------

def _supply(day_iso: str, series_rx) -> float:
    tot = 0.0
    for r in _iter_jsonl(os.path.join(STATUS_DIR, "program_history.jsonl")):
        if r.get("date") == day_iso and series_rx(r.get("series") or ""):
            tot += _f(r.get("total_reward_raw"))
    return tot


def measure(pattern: Optional[str], family: Optional[str], split: str,
            before: int = 7, after: int = 7) -> dict:
    split_ts = _ts_of(split if "T" in split else split + "T00:00:00-05:00")
    if split_ts is None:
        raise SystemExit("bad --split")
    split_day = datetime.fromtimestamp(split_ts, ET).date()
    rx = re.compile(pattern) if pattern else None

    def match(t: str) -> bool:
        if rx is not None:
            return bool(rx.search(t))
        return family_of(series_of(t))[0].lower() == (family or "").lower()

    def ser_match(s: str) -> bool:
        if rx is not None:
            return bool(rx.search(s))
        return family_of(s)[0].lower() == (family or "").lower()

    days = archive_days()
    pre = [d for d in days if split_day - timedelta(days=before) <= date.fromisoformat(d) < split_day]
    post = [d for d in days if split_day < date.fromisoformat(d) <= split_day + timedelta(days=after)]
    hist = os.path.exists(os.path.join(STATUS_DIR, "program_history.jsonl"))

    def per_day(ds):
        out = []
        for d in ds:
            b = load_archive(d)
            if not b:
                continue
            a = defaultdict(float)
            for t, v in market_rows(b):
                if match(t):
                    for k in SUM_KEYS:
                        a[k] += _f(v.get(k))
            a["net"] = a["rew"] + a["pnl"]
            a["supply"] = _supply(d, ser_match) if hist else 0.0
            out.append((d, dict(a)))
        return out

    a, b = per_day(pre), per_day(post)

    def summ(rows):
        if not rows:
            return {"days": 0}
        n = len(rows)
        net = [r["net"] for _, r in rows]
        sup = sum(r.get("supply", 0.0) for _, r in rows)
        rew = sum(r["rew"] for _, r in rows)
        return {"days": n, "rew_day": round(rew / n, 2),
                "pnl_day": round(sum(r["pnl"] for _, r in rows) / n, 2),
                "net_day": round(sum(net) / n, 2), "net_sd": round(statistics.pstdev(net), 2) if n > 1 else None,
                "fills_day": round(sum(r["fills"] for _, r in rows) / n, 1),
                "rest_day": round(sum(r["rest"] for _, r in rows) / n, 0),
                "mt_c": cpc(sum(r.get("mt", 0) for _, r in rows), sum(r.get("mt_cts", 0) for _, r in rows)),
                "rew_per_supply": round(rew / sup * 1e6, 4) if sup > 0 else None}

    sa, sb = summ(a), summ(b)
    out = {"selector": pattern or ("family=" + str(family)), "split": split,
           "before": sa, "after": sb, "basis": "before/after, not causal; rew MODELLED, pnl MEASURED"}
    if sa.get("days") and sb.get("days"):
        out["delta_net_day"] = round(sb["net_day"] - sa["net_day"], 2)
        va = [r["net"] for _, r in a]
        vb = [r["net"] for _, r in b]
        if len(va) > 1 and len(vb) > 1:
            se = math.sqrt(statistics.pvariance(va) / len(va) + statistics.pvariance(vb) / len(vb))
            out["t_stat"] = round(out["delta_net_day"] / se, 2) if se > 0 else None
    out["daily"] = {"before": [(d, round(r["net"], 2)) for d, r in a],
                    "after": [(d, round(r["net"], 2)) for d, r in b]}
    return out


# ---------------------------------------------------------------------------
# collect + evidence.md
# ---------------------------------------------------------------------------

def collect(day: date, bootstrap_days: int = 14, only: Optional[List[str]] = None) -> dict:
    t0 = time.time()
    os.makedirs(os.path.join(REVIEW_DIR, day.isoformat()), exist_ok=True)
    ev: dict = {"day": day.isoformat(), "generated": time.time(), "version": VERSION,
                "window_et": [et_str(et_bounds(day)[0], "%Y-%m-%d %H:%M"),
                              et_str(et_bounds(day)[1], "%Y-%m-%d %H:%M")]}

    def want(name):
        return not only or name in only

    if want("scorecard"):
        ev["scorecard"] = _section("scorecard", sec_scorecard, day)
    if want("attribution"):
        ev["attribution"] = _section("attribution", sec_attribution, day)
    if want("adverse"):
        ev["adverse"] = _section("adverse", sec_adverse, day)
    if want("monitors"):
        ev["monitors"] = _section("monitors", sec_monitors, day)
    if want("moat"):          # before the counterfactual: it nets estimates with the priors
        ev["moat"] = _section("moat", sec_moat, day)
    if want("counterfactual"):
        ev["counterfactual"] = _section("counterfactual", sec_counterfactual, day)
    if want("coverage"):
        ev["coverage"] = _section("coverage", sec_coverage, day)
    if want("system"):
        ev["system"] = _section("system", sec_system, day, bootstrap_days=bootstrap_days)
    if want("data"):
        ev["data"] = _section("data", sec_data, day)
    if want("changes"):
        ev["changes"] = _section("changes", sec_changes, day)
    commits = (ev.get("changes") or {}).get("commits") or []
    ev["ledger"] = _section("ledger", sec_ledger, day, commits)
    ev["secs"] = round(time.time() - t0, 1)
    ev["errors"] = {k: v.get("error") for k, v in ev.items() if isinstance(v, dict) and v.get("error")}
    if want("scorecard"):
        append_scorecard(day, ev.get("scorecard") or {})
    d = os.path.join(REVIEW_DIR, day.isoformat())
    _write_json(os.path.join(d, "evidence.json"), ev)
    with open(os.path.join(d, "evidence.md"), "w", encoding="utf-8") as fh:
        fh.write(render_evidence(ev))
    return ev


def append_scorecard(day: date, sc: dict) -> None:
    d = sc.get("day_totals") or {}
    if not d:
        return
    path = os.path.join(REVIEW_DIR, "scorecard.csv")
    cols = ["day", "rew", "pnl", "net", "real", "du", "fills", "cts", "rest", "mk", "mk_cts", "mt",
            "mt_cts", "markets", "markets_rew", "inv_cts", "sharpe30", "maxdd30", "tax30"]
    row = {k: d.get(k) for k in cols}
    row["day"] = day.isoformat()
    w30 = sc.get("30d") or {}
    row.update({"sharpe30": w30.get("sharpe"), "maxdd30": w30.get("max_dd"), "tax30": w30.get("tax")})
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("day") != row["day"]]
    rows.append({k: ("" if row.get(k) is None else row[k]) for k in cols})
    rows.sort(key=lambda r: r["day"])
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _fmt(v, nd=2) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return str(v)


def _c(v) -> str:
    return "n/a" if v is None else f"{v:+.1f}c"


def render_evidence(ev: dict) -> str:
    L: List[str] = []
    a = L.append
    day = ev["day"]
    a(f"# IMM evidence pack: ET day {day}")
    a(f"Window {ev['window_et'][0]} -> {ev['window_et'][1]} ET. Built {et_str(ev['generated'], '%Y-%m-%d %H:%M')} "
      f"ET in {ev.get('secs')}s by imm_review.py v{ev.get('version')}. Tags: [M] measured, [MOD] modelled.")
    if ev.get("errors"):
        a("")
        a("**Sections that failed (treat as data-health findings):** " + "; ".join(
            f"{k}: {v}" for k, v in ev["errors"].items()))
    # 1 scorecard
    sc = ev.get("scorecard") or {}
    a("\n## 1. Scorecard")
    dt = sc.get("day_totals") or {}
    if dt:
        w7, w30, wi = sc.get("7d") or {}, sc.get("30d") or {}, sc.get("inception") or {}
        a("| | Day | 7d avg | 30d avg | Inception avg |")
        a("|---|---:|---:|---:|---:|")
        a(f"| Rewards [MOD] | {usd(dt.get('rew'))} | {usd(w7.get('rew_mean'))} | {usd(w30.get('rew_mean'))} | {usd(wi.get('rew_mean'))} |")
        a(f"| Trading MTM [M] | {usd(dt.get('pnl'))} | {usd(w7.get('pnl_mean'))} | {usd(w30.get('pnl_mean'))} | {usd(wi.get('pnl_mean'))} |")
        a(f"| Net | {usd(dt.get('net'))} | {usd(w7.get('mean'))} | {usd(w30.get('mean'))} | {usd(wi.get('mean'))} |")
        a(f"| Avg $ resting | {usd0(dt.get('rest'), False)} | {usd0(w7.get('rest_mean'), False)} | {usd0(w30.get('rest_mean'), False)} | {usd0(wi.get('rest_mean'), False)} |")
        a(f"| Fills / contracts | {int(_f(dt.get('fills')))} / {_f(dt.get('cts')):,.0f} | | | |")
        a(f"| Markets with rewards / filled | {dt.get('markets_rew')} / {dt.get('markets_filled')} of {dt.get('markets')} | | | |")
        a("")
        for lab, w in (("7d", w7), ("30d", w30), ("Inception", wi)):
            if w.get("n"):
                a(f"- {lab} ({w.get('n')} days from {w.get('from')}): Sharpe {_fmt(w.get('sharpe'))}, "
                  f"Sortino {_fmt(w.get('sortino'))}, max drawdown {usd(w.get('max_dd'))}, worst day {usd(w.get('worst'))}, "
                  f"green {w.get('green')}/{w.get('n')}, trading/rewards tax {_fmt(w.get('tax'), 3)}, "
                  f"net per $1k resting/day {_fmt(w.get('net_per_1k_rest'), 3)}")
        if w30.get("worst_day"):
            a(f"- 30d worst day: {w30['worst_day']}")
    else:
        a("- No day-archive row for this day (dashboard not archived yet?).")
    chk = sc.get("dash_check")
    if chk:
        ok = abs(_f(chk["diff_rew"])) < 1 and abs(_f(chk["diff_pnl"])) < 1
        a(f"- Dashboard check: summary rew {chk['dash_rew']} / pnl {chk['dash_pnl']} vs archive "
          f"{chk['archive_rew']} / {chk['archive_pnl']} -> {'OK' if ok else 'MISMATCH'}")
    if sc.get("recent"):
        a("\nLast 14 days (net = rewards + trading):")
        a("| Day | Rewards | Trading | Net | Fills | Contracts | $ resting |")
        a("|---|---:|---:|---:|---:|---:|---:|")
        for r in sc["recent"]:
            a(f"| {r['d']} | {usd(r['rew'])} | {usd(r['pnl'])} | {usd(r['net'])} | {r['fills']} | {r['cts']:,} | {usd0(r['rest'], False)} |")
    now = sc.get("now") or {}
    if now:
        a(f"\nNow (status {now.get('updated_at')}): {now.get('markets_line')}; inventory "
          f"{_f(now.get('inventory_contracts')):,.0f} contracts; unrealized {usd(now.get('unrealized_mtm'))} [M]; "
          f"errors_today {now.get('errors_today')}; alerts_today {now.get('alerts_today')}; "
          f"halted_until {now.get('halted_until')}")
    if sc.get("today_so_far"):
        t = sc["today_so_far"]
        a(f"Today so far ({t.get('as_of')}): rewards {usd(t.get('rew'))}, trading {usd(t.get('pnl'))}, fills {t.get('fills')}")
    cal = sc.get("calibration") or {}
    cr = sc.get("credits") or {}
    if cal or cr:
        fam = ", ".join(f"{k} {v}" for k, v in (cal.get("by_family") or {}).items())
        a(f"Credits [M]: ledger through {cr.get('through')} ({cr.get('age_days')} days old); last 7 credited days "
          f"{usd(cr.get('last7_credited'), False)}. Paid/est {cal.get('realization_factor')} (by family: {fam}).")
    # 2 attribution
    at = ev.get("attribution") or {}
    a("\n## 2. Attribution")
    if at.get("error"):
        a(f"- ERROR: {at['error']}")
    else:
        a("| Family | Day rew | Day trading | Day net | 7d net | 30d net | 30d fills | 30d mark-out to date | 30d 4h |")
        a("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for f in at.get("families") or []:
            d, s7, s30 = f["day"], f["d7"], f["d30"]
            a(f"| {f['family']} | {usd(d['rew'])} | {usd(d['pnl'])} | {usd(d['net'])} | {usd(s7['net'])} | "
              f"{usd(s30['net'])} | {int(s30['fills'])} | {_c(s30.get('mt_c'))} | {_c(s30.get('m4_c'))} |")
        a("\nWorst events (day):")
        for r in at.get("worst_events") or []:
            a(f"- {r['event']}: net {usd(r['net'])} (rew {usd(r['rew'])}, trading {usd(r['pnl'])}, "
              f"{int(r['fills'])} fills / {r['cts']:,.0f} cts, mark-out to date {_c(r.get('mt_c'))})")
        a("\nBest events (day):")
        for r in at.get("best_events") or []:
            a(f"- {r['event']}: net {usd(r['net'])} (rew {usd(r['rew'])}, trading {usd(r['pnl'])})")
        if at.get("worst_markets"):
            a("\nWorst markets by trading (day):")
            for r in at["worst_markets"]:
                a(f"- {r['ticker']}: trading {usd(r['pnl'])} (new fills {usd(r['new_fills'])}, carry re-marked "
                  f"{usd(r['carry_remark'])}, carry settled {usd(r['carry_settled'])}), rew {usd(r['rew'])}, "
                  f"{r['cts']:,} cts, mark-out {_c(r.get('mt_c'))}, end pos {r.get('pos1')}, settled {r.get('settled')}")
        if at.get("persistent_losers"):
            a("\nPersistent losers (series net < -$25 over 30d AND < 0 over 7d):")
            for r in at["persistent_losers"]:
                a(f"- {r['series']} ({r['family']}): 30d {usd(r['net30'])} (rew {usd(r['rew30'])}, trading "
                  f"{usd(r['pnl30'])}), 7d {usd(r['net7'])}, mark-out {_c(r.get('mt_c30'))}")
        if at.get("top_series_30d"):
            a("\nTop series 30d: " + ", ".join(f"{r['series']} {usd0(r['net30'])}" for r in at["top_series_30d"]))
    # 3 adverse
    ad = ev.get("adverse") or {}
    a("\n## 3. Adverse selection")
    if at.get("families"):
        a("| Family (30d) | fill edge | 5m | 30m | 4h | to date | contracts |")
        a("|---|---:|---:|---:|---:|---:|---:|")
        for f in at["families"]:
            s30 = f["d30"]
            if _f(s30.get("cts")) <= 0:
                continue
            a(f"| {f['family']} | {_c(s30.get('fe_c'))} | {_c(s30.get('m5_c'))} | {_c(s30.get('mk30_c'))} | "
              f"{_c(s30.get('m4_c'))} | {_c(s30.get('mt_c'))} | {_f(s30.get('cts')):,.0f} |")
    a(f"\nToxic-halt sink (day): {ad.get('toxic')}; most picked-off events: {ad.get('pickoff_events')}; "
      f"sweep trips: {ad.get('sweeps')}")
    mon = (ev.get("monitors") or {}).get("monitors") or []
    if mon:
        a("\nMonitors (latest stored day; they run 06:45-08:30, so usually the day before):")
        for m in mon:
            a(f"- {m['monitor']}: {m.get('day', m.get('status'))} "
              f"{json.dumps(m.get('fields') or {}, sort_keys=True)[:300]}")
    # 4 counterfactual
    cf = ev.get("counterfactual") or {}
    a("\n## 4. Counterfactual: paying markets we did not quote [MOD]")
    if cf.get("error") or cf.get("gaps_error"):
        a(f"- ERROR: {cf.get('error') or cf.get('gaps_error')}")
    u = cf.get("universe") or {}
    if u:
        a(f"Universe (dashboard cache {et_str(cf.get('gaps_as_of'))}): {u.get('programs')} programs, pool "
          f"{usd0(u.get('pool_total'), False)}/day; quoted {u.get('quoted')} mkts in {u.get('quoted_events')} events "
          f"(cap {u.get('event_cap')}); {u.get('unquoted_rows')} unquoted rows; {u.get('books_read')} books read; "
          f"UNESTIMATED pool {usd0(u.get('unest_pool'), False)}/day; estimated missed total {usd(u.get('est_missed_total'), False)}/day.")
    r = cf.get("realisation") or {}
    a(f"Estimates shown raw and x{r.get('factor')} ({r.get('note')}).")
    if cf.get("by_class"):
        a("| Reason class | Rows | Pool $/day | Est $/day | Adj $/day |")
        a("|---|---:|---:|---:|---:|")
        for k, v in cf["by_class"].items():
            a(f"| {k} | {v['rows']} | {usd0(v['pool'], False)} | {usd(v['est'], False)} | {usd(v['est_adj'], False)} |")
    if cf.get("top_estimated"):
        a(f"\nTop estimated misses, ranked by NET ({cf.get('net_basis')}):")
        for x in cf["top_estimated"]:
            a(f"- {x['event']} [{x['class']}: {x['reason']}] pool {usd0(x['pool'], False)}/day, est "
              f"{usd(x['est'], False)} -> adj {usd(x['est_adj'], False)} -> net {usd(x.get('est_net'), False)} "
              f"({x.get('family')}, tax {x.get('tax')}), {x['n_est']}/{x['n']} mkts read - {x['title']}")
    if cf.get("top_unestimated_pool"):
        a("\nBiggest UNESTIMATED pools (no book read):")
        for x in cf["top_unestimated_pool"]:
            a(f"- {x['event']} [{x['class']}: {x['reason']}] pool {usd0(x['pool'], False)}/day, {x['n']} mkts - {x['title']}")
    if cf.get("not_enrolled_top"):
        a(f"\nNot-enrolled series (imm_unquoted calls {cf.get('not_enrolled_calls')}):")
        for x in cf["not_enrolled_top"]:
            a(f"- {x['series']} ({x.get('fam')}): pool {usd0(x.get('pool'), False)}/day, x1 {x.get('x1')} x3 {x.get('x3')} "
              f"$/day, call {x.get('call')} - {x.get('why')}")
    if cf.get("config_gaps"):
        a("\nConfig gaps (standing down, fail closed):")
        for x in cf["config_gaps"]:
            a(f"- {x['event']} since {x['since']}: {x['msg']}")
    sel = cf.get("selection") or {}
    if sel.get("decisions"):
        a(f"\nAllowed candidates by decision ({sel.get('snapshots')} hourly snapshots; avg modelled $/day):")
        a("; ".join(f"{k} {v['tickers']} mkts / {usd(v['avg_est_day'], False)}" for k, v in list(sel["decisions"].items())[:14]))
        if sel.get("top_cut"):
            a("Top cut candidates: " + "; ".join(f"{x['event']} ({x['decision']}) {usd(x['avg_est_day'], False)}"
                                                 for x in sel["top_cut"][:10]))
        fm = sel.get("floor_misses") or {}
        a(f"Floor misses: of {fm.get('periods_ending')} market-periods ending in the day, {fm.get('under_090')} closed "
          f"under $0.90 of modelled accrual ({usd(fm.get('under_090_modelled'), False)} earned nothing) and "
          f"{fm.get('near_090_100')} at $0.90-1.00 ({usd(fm.get('near_modelled'), False)}; paid/est ~1.1 may still clear). "
          f"By series {fm.get('by_series')}; top {fm.get('top')}")
    cal = cf.get("calibration") or {}
    a(f"Prediction calibration: {cal}")
    # 5 coverage
    cv = ev.get("coverage") or {}
    a("\n## 5. Coverage and uptime")
    a(f"- Cycles {cv.get('cycles')}; gap p50 {cv.get('gap_p50_s')}s p95 {cv.get('gap_p95_s')}s max {cv.get('gap_max_s')}s "
      f"at {cv.get('gap_max_at')}; dark minutes (gaps > 10 min) {cv.get('dark_minutes')}; head/tail gaps "
      f"{cv.get('head_gap_min')}/{cv.get('tail_gap_min')} min")
    a(f"- Guard enters: {cv.get('guard_enters')}")
    # 6 system
    sy = ev.get("system") or {}
    a("\n## 6. System health")
    if sy.get("error"):
        a(f"- ERROR: {sy['error']}")
    lg = sy.get("log") or {}
    if lg:
        a(f"Bot log files {lg.get('files')}: by severity {lg.get('by_severity')}; process starts {lg.get('starts')}; exits {lg.get('exits')}")
        if sy.get("signatures_backfilled_days"):
            a(f"(signature history back-filled for {sy['signatures_backfilled_days']} prior days)")
        if lg.get("new"):
            a("NEW signatures (not seen in the prior 14 days):")
            for x in lg["new"]:
                a(f"- [{x['sev']}] x{x['n']}: {x['fp']}\n  e.g. {x.get('example')}")
        if lg.get("spikes"):
            a("SPIKES (>= 3x the 14-day median):")
            for x in lg["spikes"]:
                a(f"- [{x['sev']}] x{x['n']} (median {x['median_14d']}): {x['fp']}")
        if lg.get("top"):
            a("Top signatures (severity first):")
            for fp, n, sev in lg["top"][:20]:
                a(f"- [{sev}] x{n}: {fp}")
        if lg.get("gone"):
            a("Gone (seen most days before, absent now): " + "; ".join(f"{x['fp'][:80]} (last {x['last']})" for x in lg["gone"][:8]))
    rn = sy.get("runs") or {}
    if rn:
        a(f"Runs started in the day: {len(rn.get('runs') or [])} " + "; ".join(
            f"{x['at']} {x['run_id']} {x['git_sha']}" for x in (rn.get("runs") or [])[:12]))
        if rn.get("env_changes"):
            a("Env changes between runs: " + "; ".join(f"{x['at']} {x['key']}: {x['old']} -> {x['new']}"
                                                       for x in rn["env_changes"][:15]))
    tk = sy.get("tasks") or {}
    if tk:
        if tk.get("error"):
            a(f"Scheduled tasks: {tk['error']}")
        else:
            a(f"Scheduled tasks: {tk.get('count')} KL tasks, {tk.get('running')} running; failing: " + (
                "; ".join(f"{x['task']} {x['hex']} (last {x['last_run']}{', chronic' if x['chronic'] else ''})"
                          for x in tk.get("failing") or []) or "none"))
            a(f"Disabled: {', '.join(tk.get('disabled') or []) or 'none'}")
    if sy.get("task_logs"):
        a("Task-log error lines (dated the day or this morning):")
        for t in sy["task_logs"]:
            for ln in t["lines"]:
                a(f"- {t['task']}: x{ln['n']} {ln['line']}")
    ha = sy.get("health_alert") or {}
    a(f"Health alert: {ha.get('down_lines')} DOWN lines ({ha.get('within_10min_of_restart')} within 10 min of one of "
      f"the bot's {ha.get('bot_starts')} process starts), {ha.get('recovered')} recovered; {ha.get('examples')}")
    sn = sy.get("sync") or {}
    a(f"Repo sync: last run {sn.get('last_run')}, last success {sn.get('last_success')}, failure streak "
      f"{sn.get('failure_streak')} {sn.get('last_failure_msg') or ''}; in day {sn.get('fails_in_day')}/{sn.get('runs_in_day')} failed")
    g = sy.get("git") or {}
    if g:
        a(f"Git (live tree): {g.get('branch')} @ {g.get('head')}, ahead {g.get('ahead')} / behind {g.get('behind')} "
          f"origin/main; {g.get('dirty_count')} tracked files modified but uncommitted; {g.get('untracked_count')} untracked "
          f"outside run-logs; {g.get('worktrees')} worktrees ({g.get('worktrees_missing')} with missing dirs)")
        if g.get("unpushed"):
            a("Unpushed commits: " + "; ".join(g["unpushed"][:10]))
        if g.get("dirty_tracked"):
            a("Uncommitted (age = since the file last changed): " + ", ".join(g["dirty_tracked"][:25]))
        if g.get("dirty_stale_24h"):
            a(f"Untouched for 24h+ (forgotten commits, not a session at work): {', '.join(g['dirty_stale_24h'])}")
    m = sy.get("machine") or {}
    if m:
        a(f"Machine: disk free {m.get('disk_free_gb')} of {m.get('disk_total_gb')} GB; run-logs/incentive-mm "
          f"{m.get('status_dir_gb')} GB growing {m.get('growth_gb_day')} GB/day -> ~{m.get('days_to_full')} days to full; "
          f"stale .tmp {m.get('stale_tmp')}; research token {m.get('token_age_days')} days old (1-year life); "
          f"dashboard {m.get('dashboard_age_min')} min old; status {m.get('status_age_min')} min old; memory {m.get('memory')}")
    rt = sy.get("retention") or {}
    if rt.get("present"):
        why = f" ({rt['reason']})" if rt.get("reason") else ""
        stale = " STALE: no run in 30 h" if (rt.get("age_h") or 0) > 30 else ""
        if rt.get("mode") == "apply":
            what = (f"{rt.get('compressed')} files compressed ({rt.get('in_gb')} GB in), {rt.get('freed_gb')} GB "
                    f"freed; {rt.get('remaining')} files ({rt.get('remaining_gb')} GB) past the cutoff still plain"
                    + (f"; stopped at {rt['stopped']}" if rt.get("stopped") else ""))
        elif rt.get("mode") == "dry-run":
            what = (f"would compress {rt.get('candidates')} files ({rt.get('candidates_gb')} GB); an --apply run "
                    f"takes {rt.get('next_run')} ({rt.get('next_run_gb')} GB)")
        else:
            what = "nothing done"
        a(f"Log retention (imm_log_retention, R-261124-03): last run {rt.get('ts')} ({rt.get('age_h')} h ago){stale}, "
          f"mode {rt.get('mode')}{why}: {what}; {rt.get('held')} old files held {rt.get('held_reasons') or {}}; "
          f"skipped {rt.get('skipped') or {}}; failed {len(rt['failed'])}, conflicts {len(rt['conflicts'])}; "
          f"lifetime {rt.get('lifetime_files')} files, {rt.get('lifetime_freed_gb')} GB freed, "
          f"{rt.get('lifetime_restored')} restored")
        bad = rt["failed"] + rt["conflicts"]
        if bad:
            a("Log retention FAILURES (the original is kept; read run-logs/incentive-mm/retention/run.log): "
              + "; ".join(f"{x.get('file')}: {x.get('reason')}" for x in bad[:10]))
    elif "retention" in sy:
        a("Log retention: no run yet (Task Scheduler 'KL imm log-retention', 02:40 ET)")
    # 7 data
    dh = ev.get("data") or {}
    a("\n## 7. Data health (sizes of the UTC day's sink files)")
    if dh.get("sinks"):
        a("| Sink | MB | 14d median MB | ratio | days present | status |")
        a("|---|---:|---:|---:|---:|---|")
        for x in dh["sinks"]:
            a(f"| {x['sink']} | {x['mb']} | {x['median_mb']} | {x['ratio']} | {x['days_present_14']} | {x['status']} |")
    if dh.get("undocumented"):
        a(f"Sinks on disk not mentioned in IMM_LOGGING.md: {', '.join(dh['undocumented'])}")
    # 8 changes
    ch = ev.get("changes") or {}
    a("\n## 8. Changes")
    a(f"{ch.get('n_commits')} commits in the day ({ch.get('n_bot')} touch incentive_mm.py):")
    for c in ch.get("commits") or []:
        a(f"- {c['at']} {c['sha']}{' [bot]' if c['bot'] else ''}{' ' + ','.join(c['ids']) if c['ids'] else ''}: {c['subject']}")
    cfg = ch.get("config_files") or {}
    a(f"Reloadable config files tracked: {cfg.get('tracked_files')} (vs {cfg.get('compared_with') or cfg.get('note')}): "
      + ("; ".join(f"{x['file']} {x['change']}" for x in cfg.get("changes") or []) or "no changes"))
    # 9 ledger
    lg2 = ev.get("ledger") or {}
    a("\n## 9. Ledger")
    a(f"Open items {lg2.get('open')}; stats {lg2.get('stats')}")
    if lg2.get("shipped_detected"):
        a(f"Marked shipped from commit subjects: {lg2['shipped_detected']}")
    if lg2.get("due"):
        a("DUE for re-measurement:")
        for x in lg2["due"]:
            a(f"- {x['id']} {x['title']} (shipped {x['shipped']}, due {x['due']}): metric {json.dumps(x.get('metric'))[:300]}")
    if lg2.get("awaiting_decision"):
        a("Awaiting Jack's decision: " + "; ".join(f"{x['id']} {x['severity']} {x['title']} (since {x['since']})"
                                                     for x in lg2["awaiting_decision"]))
    if lg2.get("open_list"):
        a("Open: " + "; ".join(f"{x['id']} [{x['status']}/{x['severity']}/{x['kind']}] {x['title']} (fp {x['fingerprint']}, seen {x['seen']}d)"
                               for x in lg2["open_list"]))
    # 10 moat
    mo = ev.get("moat") or {}
    a("\n## 10. The moat (what compounds)")
    if mo.get("error"):
        a(f"- ERROR: {mo['error']}")
    else:
        s = mo.get("assets") or {}
        a(f"Assets: {s.get('family_days')} family-days over {s.get('days')} days since {s.get('first_day')} "
          f"({s.get('share_days')} days with pool share); {s.get('predictions')} logged predictions; "
          f"{s.get('findings')} findings; {s.get('playbook_rules')} playbook rules; {s.get('signatures')} log "
          f"signatures; {s.get('config_versions')} config snapshots.")
        b = mo.get("priors_book") or {}
        a(f"Book, last 30 days: rewards {usd(b.get('rew_per_1k'), False)} and net {usd(b.get('net_per_1k'))} per $1k "
          f"resting/day; trading tax {_fmt(b.get('tax'), 3)}; mark-out to date {_c(b.get('mt_c'))}.")
        if mo.get("edge"):
            a(f"Edge watch (last {EDGE_RECENT} days vs the {EDGE_BASE} before; {mo.get('edge_steady')} families steady):")
            a("| Family | Verdict | Share now / before | Rewards per $1k now / before | Net per $1k now / before |")
            a("|---|---|---:|---:|---:|")
            for e in mo["edge"]:
                r, bb = e["recent"], e["base"]
                a(f"| {e['family']} | {e['verdict']} | {_fmt(r.get('share'), 4)} / {_fmt(bb.get('share'), 4)} | "
                  f"{_fmt(r.get('rew_per_1k'))} / {_fmt(bb.get('rew_per_1k'))} | "
                  f"{_fmt(r.get('net_per_1k'))} / {_fmt(bb.get('net_per_1k'))} |")
        if mo.get("priors_top"):
            a("Family priors (30 days, shrunk to the book), best net per $1k resting first:")
            a("; ".join(f"{p['family']} net {_fmt(p.get('net_per_1k'))}/rew {_fmt(p.get('rew_per_1k'))} per $1k, "
                        f"tax {_fmt(p.get('tax'), 2)}, {p['days']}d" for p in mo["priors_top"][:12]))
        if mo.get("review_calibration"):
            a(f"The review's own calibration (realised / predicted $/day, by kind): {mo['review_calibration']}")
    a("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _day_arg(s: Optional[str]) -> date:
    return date.fromisoformat(s) if s else prior_et_day()


PUT_NAMES = ("findings.json", "review.md")


def put(day: date, name: str, content: str) -> str:
    """The unattended run's only way to write: one of PUT_NAMES into DIR.
    findings.json must parse as a JSON object; it is stored pretty-printed."""
    if name not in PUT_NAMES:
        raise SystemExit(f"put: only {', '.join(PUT_NAMES)}")
    if not content.strip():
        raise SystemExit("put: empty content")
    path = os.path.join(REVIEW_DIR, day.isoformat(), name)
    if name.endswith(".json"):
        try:
            obj = json.loads(content)
        except ValueError as e:
            raise SystemExit(f"put: findings.json is not valid JSON: {e}")
        if not isinstance(obj, dict):
            raise SystemExit("put: findings.json must be a JSON object")
        _write_json(path, obj)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content.rstrip() + "\n")
    return path


def _print_item(it: dict) -> None:
    print(json.dumps(it, indent=1, sort_keys=True, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--day")
    c.add_argument("--bootstrap-days", type=int, default=14)
    c.add_argument("--only", help="comma list of sections (debug)")
    l = sub.add_parser("ledger")
    l.add_argument("action", choices=("list", "show", "due", "stats", "ingest", "decide", "ship", "close"))
    l.add_argument("id", nargs="?")
    l.add_argument("status", nargs="?")
    l.add_argument("--day")
    l.add_argument("--file")
    l.add_argument("--note", default="")
    l.add_argument("--by", default="jack")
    l.add_argument("--ref")
    l.add_argument("--status", dest="status_opt", help="same as the positional status")
    l.add_argument("--all", action="store_true")
    pp = sub.add_parser("put", help="write findings.json or review.md from stdin into the day's folder")
    pp.add_argument("name", choices=PUT_NAMES)
    pp.add_argument("--day")
    m = sub.add_parser("measure")
    m.add_argument("--pattern")
    m.add_argument("--family")
    m.add_argument("--split", required=True)
    m.add_argument("--before", type=int, default=7)
    m.add_argument("--after", type=int, default=7)
    a = ap.parse_args(argv)

    if a.cmd == "collect":
        day = _day_arg(a.day)
        ev = collect(day, bootstrap_days=a.bootstrap_days,
                     only=[x.strip() for x in a.only.split(",")] if a.only else None)
        d = os.path.join(REVIEW_DIR, day.isoformat())
        print(f"evidence for {day} in {ev['secs']}s -> {os.path.join(d, 'evidence.md')}")
        if ev.get("errors"):
            print("section errors: " + json.dumps(ev["errors"]))
        return 0
    if a.cmd == "put":
        data = sys.stdin.buffer.read().decode("utf-8", "replace")
        print("wrote " + put(_day_arg(a.day), a.name, data))
        return 0
    if a.cmd == "measure":
        if not a.pattern and not a.family:
            ap.error("measure needs --pattern or --family")
        print(json.dumps(measure(a.pattern, a.family, a.split, a.before, a.after), indent=1))
        return 0
    # ledger
    if a.action == "list":
        for iid, it in sorted(ledger_state().items()):
            if not a.all and it.get("status") in TERMINAL:
                continue
            print(f"{iid} [{it.get('status')}/{it.get('severity')}/{it.get('kind')}] {it.get('title')} "
                  f"(seen {len(it.get('seen_days') or [])}d, fp {it.get('fingerprint')})")
        return 0
    if a.action == "show":
        it = ledger_state().get(a.id or "")
        if not it:
            print(f"no item {a.id}")
            return 1
        _print_item(it)
        return 0
    if a.action == "due":
        print(json.dumps(ledger_due(_day_arg(a.day)), indent=1))
        return 0
    if a.action == "stats":
        print(json.dumps(ledger_stats(), indent=1))
        return 0
    if a.action == "ingest":
        res = ledger_ingest(_day_arg(a.day), a.file)
        print(json.dumps({k: v for k, v in res.items() if k != "ids"}, indent=1))
        print("ids: " + json.dumps(res["ids"]))
        return 0
    items = ledger_state()
    if not a.id or a.id not in items:
        print(f"no item {a.id}")
        return 1
    a.status = a.status_opt or a.status
    if a.action == "decide":
        if a.status not in ("approved", "rejected", "deferred"):
            ap.error("decide takes approved|rejected|deferred")
        ledger_append({"op": "decide", "id": a.id, "status": a.status, "by": a.by, "note": a.note})
    elif a.action == "ship":
        if not a.ref:
            ap.error("ship needs --ref <commit>")
        ledger_append({"op": "ship", "id": a.id, "ref": a.ref, "by": a.by, "note": a.note,
                       "at": _now_iso()})
    elif a.action == "close":
        st = a.status if a.status in TERMINAL else "closed"
        ledger_append({"op": "close", "id": a.id, "status": st, "note": a.note})
    _print_item(ledger_state()[a.id])
    return 0


if __name__ == "__main__":
    sys.exit(main())
