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
    python imm_review.py ledger close ID --status closed|fixed-elsewhere|wontfix|reverted|false-positive --note "..."
    python imm_review.py ledger recheck ID --on YYYY-MM-DD [--note "..."]   # a promise to look again
    python imm_review.py watch list [--all] | eval [--day D] | retire W-... --note "..."
    python imm_review.py changes list [--all] | show KEY | sync | readout KEY [--day D]
    python imm_review.py changes add SHA | skip KEY --note "..." | verdict KEY worked|hurt|... --note "..."
    python imm_review.py changes scope KEY [--series A,B] [--family F] [--group G] [--event E] [--wave W]
                                 [--pattern RX] [--book] [--metric net|rew|pnl|...] [--expect "..."]
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
    watches.json, watch_results.jsonl      the review's own read-only checks and their history
                                           (added by the skill through findings.json "watches")
    changes.jsonl                          the change registry: every IMM change, when it went
                                           live, its scope and its 7-day readout and verdict

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
import itertools
import json
import math
import os
import pickle
import random
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


MEMORY_DIR = ""
# the change registry's per-process caches (section 12): per-day event sums of
# the day archive, program supply per day, the bot's module closure
_REG_DAYS: dict = {}
_SUPPLY_CACHE: dict = {}
_BOT_MODULES = None


def configure(status_dir: Optional[str] = None, review_dir: Optional[str] = None,
              repo: Optional[str] = None, memory_dir: Optional[str] = None) -> None:
    global LIVE_REPO, STATUS_DIR, REVIEW_DIR, DASH_DIR, CACHE_DIR, ARCHIVE_DIR, RUN_LOGS, MOAT_DIR, MEMORY_DIR
    global _BOT_MODULES
    _REG_DAYS.clear()
    _SUPPLY_CACHE.clear()
    _BOT_MODULES = None
    LIVE_REPO = repo or os.environ.get("IMM_REVIEW_REPO") or r"C:\Users\jackd\Documents\KL"
    # Claude Code's project memory for the repo: the notes other sessions write
    # (section 11 lists the ones written on the review day)
    MEMORY_DIR = memory_dir or os.environ.get("IMM_REVIEW_MEMORY_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", LIVE_REPO), "memory")
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
    ("fair-marks", "incentive-mm/fair-marks-task.log"),
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
    ("unread", ("not read",)),          # imm_quote_gaps: no book read reached it (R-261124-22)
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
            "failed", "inconclusive", "closed", "fixed-elsewhere", "wontfix", "reverted",
            "false-positive")
# false-positive (11/26): a finding the review got wrong, closed as such, so
# its precision is a number rather than a lesson buried in a note
TERMINAL = {"rejected", "verified", "closed", "fixed-elsewhere", "wontfix", "reverted", "false-positive"}
# "allocation" (11/26): a size move from evidence section 14, so its realised /
# predicted ratio is calibrated apart (review_calibration groups by kind)
KINDS = ("bug", "ops", "data", "missed", "loss", "strategy", "experiment", "review", "allocation")


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


UNMEASURED_CLASSES = (
    ("blocked on purpose", "nothing (Jack's blocks)"),
    ("not enrolled: estimated", "imm_unquoted (the not-enrolled scan), per series"),
    ("not enrolled: unscored", "nothing"),
    ("allowed: estimated by the bot", "the bot's selection snapshot, every hourly refresh"),
    ("allowed: not in the bot's snapshot", "nothing"),
)
_UNMEASURED_OPEN = ("blocked on purpose", "not enrolled: unscored", "allowed: not in the bot's snapshot")


def unmeasured_class(r: dict, unq_series: Dict[str, dict], bot_events: Dict[str, dict]) -> str:
    cls = reason_class(r.get("reason") or "")
    if cls == "deliberate":
        return "blocked on purpose"
    if cls == "not enrolled":
        u = unq_series.get(r.get("series") or series_of(r.get("event") or ""))
        return "not enrolled: estimated" if u and _f(u.get("scored")) > 0 else "not enrolled: unscored"
    return ("allowed: estimated by the bot" if r.get("event") in bot_events
            else "allowed: not in the bot's snapshot")


def unmeasured_pool(rows: List[dict], urows: List[dict], latest: Dict[str, tuple],
                    gap_scan_unest) -> dict:
    """THE BLIND SPOT, MEASURED (review R-261124-21, Jack 2026-11-25: "Yes, all
    four"). The gap scan reads ~210 books an hour, so its "unestimated pool"
    (api_gaps ctx unest_pool) counts every event it did not read, though most of
    them are estimated elsewhere: the not-enrolled scan (api_unquoted) scores each
    series outside the allowlist, and the bot estimates every allowed candidate
    on each hourly refresh (selection snapshot). Only what none of the three
    reads is unmeasured -- and most of that is blocked on purpose."""
    unq_series = {u.get("series"): u for u in urows if u.get("series")}
    bot_events: Dict[str, dict] = {}
    for _t, (_ts, dec, est, ev) in latest.items():
        if not ev or dec == "selected":
            continue
        b = bot_events.setdefault(ev, {"est": 0.0, "under_1": 0.0, "n": 0})
        b["est"] += est
        b["n"] += 1
        if est < 1.0:
            b["under_1"] += est
    acc = {name: {"events": 0, "pool": 0.0, "est": 0.0, "est_x3": 0.0, "under_1": 0.0, "by": by}
           for name, by in UNMEASURED_CLASSES}
    seen_series = set()
    for r in rows:
        if _f(r.get("n_est")):
            continue
        name = unmeasured_class(r, unq_series, bot_events)
        a = acc[name]
        a["events"] += 1
        a["pool"] += _f(r.get("pool"))
        if name == "not enrolled: estimated":
            ser = r.get("series") or series_of(r.get("event") or "")
            if ser not in seen_series:               # x1/x3 are per series: count each once
                seen_series.add(ser)
                a["est"] += _f(unq_series[ser].get("x1"))
                a["est_x3"] += _f(unq_series[ser].get("x3"))
        elif name == "allowed: estimated by the bot":
            b = bot_events[r["event"]]
            a["est"] += b["est"]
            a["under_1"] += b["under_1"]
    classes = {}
    for name, a in acc.items():
        if not a["events"]:
            continue
        c = {"events": a["events"], "pool": round(a["pool"], 2), "by": a["by"]}
        if name == "not enrolled: estimated":
            c.update({"est_x1": round(a["est"], 2), "est_x3": round(a["est_x3"], 2)})
        elif name == "allowed: estimated by the bot":
            c.update({"est": round(a["est"], 2), "est_under_1_per_mkt": round(a["under_1"], 2)})
        classes[name] = c
    open_pool = sum(acc[k]["pool"] for k in _UNMEASURED_OPEN)
    return {"gap_scan_unest_pool": round(_f(gap_scan_unest), 2),
            "classes": classes,
            "unmeasured_pool": round(open_pool, 2),
            "unmeasured_not_blocked": round(open_pool - acc["blocked on purpose"]["pool"], 2),
            "basis": ("pool: Kalshi program $/day of events the gap scan left unread (MEASURED); estimates "
                      "MODELLED: not-enrolled x1/x3 $/day per series, the bot's est $/day per allowed market "
                      "at its last read of the day (est_under_1_per_mkt: the part from markets under $1/day "
                      "each, which a one-day period never pays)")}


def sec_counterfactual(day: date) -> dict:
    iso = day.isoformat()
    out: dict = {"basis": "MODELLED: one-reading estimates, full-day coverage, no competitor response"}
    fac, fac_note = realisation_factor()
    out["realisation"] = {"factor": fac, "note": fac_note}
    latest: Dict[str, tuple] = {}
    sel = selection_day(day, latest)
    rows: List[dict] = []
    urows: List[dict] = []
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

    gaps = _load_pickle(os.path.join(CACHE_DIR, "api_gaps.pkl")) or {}
    val = gaps.get("val") if isinstance(gaps, dict) else None
    if isinstance(val, dict):
        rows = [r for r in val.get("rows") or [] if isinstance(r, dict)]
        out["gaps_as_of"] = gaps.get("at")
        out["universe"] = {k: (val.get("ctx") or {}).get(k) for k in (
            "programs", "pool_total", "quoted", "quoted_events", "event_cap", "unquoted_rows",
            "books_read", "books_batched", "book_calls", "batch_error", "est_missed_total",
            "unest_pool", "updated_at")}
        by_cls = defaultdict(lambda: {"rows": 0, "pool": 0.0, "est": 0.0})
        for r in rows:
            c = by_cls[reason_class(r.get("reason") or "")]
            c["rows"] += 1
            c["pool"] += _f(r.get("pool"))
            c["est"] += _f(r.get("est"))
        out["by_class"] = {k: {"rows": v["rows"], "pool": round(v["pool"], 2),
                               "est": round(v["est"], 2), "est_adj": round(v["est"] * fac, 2)}
                           for k, v in sorted(by_cls.items(), key=lambda kv: -kv[1]["pool"])}

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
    if rows:
        um = unmeasured_pool(rows, urows, latest, (out.get("universe") or {}).get("unest_pool"))
        out["unmeasured"] = um
        unq_series = {u.get("series"): u for u in urows if u.get("series")}
        bot_events = {ev for _t, (_ts, dec, _est, ev) in latest.items() if ev and dec != "selected"}
        for x in out.get("top_unestimated_pool") or []:
            x["measured_by"] = unmeasured_class(x, unq_series, dict.fromkeys(bot_events))
    try:
        out["whole_day"] = whole_day_misses(day, {r.get("event") for r in rows}, fac, tax_of)
    except Exception as ex:                      # a report section, never a crash
        out["whole_day"] = {"scans": 0, "error": f"{type(ex).__name__}: {ex}"}
    cg = _load_json(os.path.join(STATUS_DIR, "config_gaps.json"), {}) or {}
    gaps_now = cg.get("gaps") or {}
    out["config_gaps"] = [{"event": k, "series": v.get("series"), "since": v.get("since"),
                           "msg": (v.get("msg") or "")[:160]}
                          for k, v in list(gaps_now.items())[:25] if isinstance(v, dict)]
    out["selection"] = sel
    out["calibration"] = calibrate_predictions(day)
    return out


WHOLE_DAY_GAP_CAP_S = 2 * 3600       # a scan stands for at most 2h after it


def gap_scans_of_day(day: date) -> List[Tuple[float, dict]]:
    """[(ts, scan)] of the ET day from STATUS_DIR/gap_scans_<UTC date>.jsonl (one line
    per gap scan, imm_quote_gaps.archive_scan), oldest first."""
    s, e = et_bounds(day)
    scans = []
    for u in utc_dates(day):
        for r in _iter_jsonl(os.path.join(STATUS_DIR, f"gap_scans_{u}.jsonl")):
            ts = _ts_of(r.get("ts"))
            if ts is not None and s <= ts < e and isinstance(r.get("rows"), list):
                scans.append((ts, r))
    scans.sort(key=lambda x: x[0])
    return scans


def whole_day_misses(day: date, morning_events: set, fac: float, tax_of) -> dict:
    """THE WHOLE DAY (review R-261124-24, Jack 2026-11-25: "Push and archive"): the
    morning gap scan only knows what is unquoted at 04:30, so a pool that lived and
    ended during the day was never counted. Every hourly scan is archived; each
    stands for the time to the next one (capped at 2h; the first also for the gap
    since midnight), so an event's modelled $ over the day = sum of its est $/day x
    the share of the day each scan covers. MODELLED: one reading per scan, x1."""
    scans = gap_scans_of_day(day)
    if not scans:
        return {"scans": 0, "note": "no gap_scans file for the day (the archive starts 2026-11-25)"}
    s, e = et_bounds(day)
    covers = []
    for i, (ts, _r) in enumerate(scans):
        nxt = scans[i + 1][0] if i + 1 < len(scans) else e
        c = max(0.0, min(nxt, ts + WHOLE_DAY_GAP_CAP_S) - ts)
        if i == 0 and ts - s <= WHOLE_DAY_GAP_CAP_S:
            c += ts - s
        covers.append(c)
    ev: Dict[str, dict] = {}
    for (ts, r), c in zip(scans, covers):
        w = c / 86400.0
        for x in r["rows"]:
            if not isinstance(x, dict) or not x.get("event"):
                continue
            a = ev.setdefault(x["event"], {"series": x.get("series"), "pool_days": 0.0, "est_days": 0.0,
                                           "unread_pool_days": 0.0, "hours": 0.0})
            a["pool_days"] += _f(x.get("pool")) * w
            a["hours"] += c / 3600.0
            if x.get("est") is not None:
                a["est_days"] += _f(x.get("est")) * w
            else:
                a["unread_pool_days"] += _f(x.get("pool")) * w
            a["reason"] = x.get("reason") or ""
    by_cls = defaultdict(lambda: {"events": 0, "pool_days": 0.0, "est_days": 0.0, "unread_pool_days": 0.0})
    for a in ev.values():
        b = by_cls[reason_class(a["reason"])]
        b["events"] += 1
        for k in ("pool_days", "est_days", "unread_pool_days"):
            b[k] += a[k]

    def slim_day(name, a):
        fam, tax = tax_of({"series": a["series"], "event": name})
        adj = a["est_days"] * fac
        return {"event": name, "series": a["series"], "reason": a["reason"], "class": reason_class(a["reason"]),
                "hours": round(a["hours"], 1), "pool_days": round(a["pool_days"], 2),
                "est_days": round(a["est_days"], 2), "est_adj": round(adj, 2), "family": fam, "tax": tax,
                "est_net": round(adj * (1.0 - tax), 2) if tax is not None else round(adj, 2),
                "in_morning_scan": name in morning_events}
    tops = sorted((slim_day(k, a) for k, a in ev.items() if a["est_days"] > 0), key=lambda x: -x["est_net"])
    ended = [x for x in tops if not x["in_morning_scan"]]
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"scans": len(scans), "first": iso(scans[0][0]), "last": iso(scans[-1][0]),
            "covered_hours": round(sum(covers) / 3600.0, 1),
            "by_class": {k: {"events": v["events"], "pool_days": round(v["pool_days"], 2),
                             "est_days": round(v["est_days"], 2), "est_adj": round(v["est_days"] * fac, 2),
                             "unread_pool_days": round(v["unread_pool_days"], 2)}
                         for k, v in sorted(by_cls.items(), key=lambda kv: -kv[1]["est_days"])},
            "top": tops[:20],
            "ended_before_review": {"events": len(ended), "est_days": round(sum(x["est_days"] for x in ended), 2),
                                    "est_net": round(sum(x["est_net"] for x in ended), 2), "top": ended[:10]},
            "unread_pool_days": round(sum(a["unread_pool_days"] for a in ev.values()), 2),
            "basis": (f"MODELLED: each scan's est $/day x the share of the day it covers (to the next scan, "
                      f"<= 2h); net = x {fac} realisation x (1 - family trading tax); x1 size")}


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


_WAVE_RE = re.compile(r"\(wave (w[0-9A-Za-z_-]+)\)")


def pvd_wave_events() -> Dict[str, dict]:
    """{event: {"stop": latest stop ts, "waves": {names}}} from the wave lists the bot
    reads (STATUS_DIR/post_vote_dead_books.json, as of this run). A wave entry
    forces its whole EVENT past the payout floor and the hopeless exit
    (incentive_mm.py, "a wave entry forces its EVENT"); "stop" also counts the
    period that just ended (prev_period), so a miss that closed it still shows."""
    d = _load_json(os.path.join(STATUS_DIR, "post_vote_dead_books.json"), {}) or {}
    out: Dict[str, dict] = {}
    for _t, v in ((d.get("markets") or {}) if isinstance(d, dict) else {}).items():
        if not isinstance(v, dict) or not v.get("event"):
            continue
        stops = [_ts_of(v.get("stop"))]
        pp = v.get("prev_period")
        if isinstance(pp, dict):
            stops.append(_ts_of(pp.get("stop")))
        stops = [x for x in stops if x is not None]
        e = out.setdefault(v["event"], {"stop": None, "waves": set()})
        if stops:
            e["stop"] = max(stops + ([e["stop"]] if e["stop"] is not None else []))
        for x in v.values():
            if isinstance(x, str):
                e["waves"].update(_WAVE_RE.findall(x))
    return out


SIZE_NEEDED_BUCKETS = ((2.0, "<=2x"), (3.0, "2-3x"), (5.0, "3-5x"), (float("inf"), ">5x"))


def selection_day(day: date, latest: Optional[dict] = None) -> dict:
    """Allowed candidates the bot cut, from the hourly selection snapshots: the
    average modelled $/day by decision, and floor misses (periods that ended in
    the day with accrual under the $1 floor). With `latest`, also fills it with
    each ticker's last row of the day: {ticker: (ts, decision, est $/day, event)}.

    FLOOR MISSES, COST AND CAUSE (review R-261124-23, Jack 2026-11-25: "Yes, all
    four"): a miss is "quoted to the end" when its last read before the period
    end still had it selected, else "dropped" (the floor logic stood it down and
    the accrual was left over); "wave-forced" when a wave list held its event
    through the period end. Each class carries the trading P&L, fills and $
    resting of its markets over the 7 ET days to the day (day archive: MEASURED
    at last-trade marks) and, for the quoted ones, the size multiple each needed
    to reach $1 (1 / banked: MODELLED, linear in size)."""
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
            ev = r.get("event_ticker") or event_of(r.get("ticker") or "")
            if dec != "selected" and est > 0:
                top[(ev, dec)] += est
            if latest is not None and r.get("ticker"):
                prev = latest.get(r["ticker"])
                if prev is None or ts >= prev[0]:
                    latest[r["ticker"]] = (ts, dec, est, ev)
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
    fm = {"periods_ending": len(last_row),
          "under_090": len(under),
          "under_090_modelled": round(sum(b for _, b in under), 2),
          "near_090_100": len(near),
          "near_modelled": round(sum(b for _, b in near), 2),
          "by_series": by_series.most_common(8), "top": under[:8]}
    try:
        fm.update(_floor_miss_cost(day, under, last_row))
    except Exception as ex:                      # a report line, never a crash
        fm["cost_error"] = f"{type(ex).__name__}: {ex}"
    return {"snapshots": len(snaps), "decisions": decisions,
            "top_cut": [{"event": ev, "decision": dec, "avg_est_day": round(v / n, 2)}
                        for (ev, dec), v in sorted(top.items(), key=lambda kv: -kv[1])[:15]],
            "floor_misses": fm}


def _floor_miss_cost(day: date, under: List[Tuple[str, float]], last_row: Dict[str, dict]) -> dict:
    """The cause and cost of the under-$0.90 misses (see selection_day)."""
    waves = pvd_wave_events()
    cls_of: Dict[str, str] = {}
    split = {k: {"n": 0, "modelled": 0.0, "series": Counter()} for k in ("quoted_to_end", "dropped")}
    forced = {"n": 0, "modelled": 0.0, "waves": Counter()}
    size_needed = {lab: 0 for _x, lab in SIZE_NEEDED_BUCKETS}
    for t, banked in under:
        r = last_row.get(t) or {}
        k = "quoted_to_end" if r.get("decision") == "selected" else "dropped"
        cls_of[t] = k
        c = split[k]
        c["n"] += 1
        c["modelled"] += banked
        c["series"][series_of(t)] += 1
        ev = r.get("event_ticker") or event_of(t)
        pe = _ts_of(r.get("program_end"))
        w = waves.get(ev)
        if w and w["stop"] is not None and pe is not None and w["stop"] >= pe - 300:
            forced["n"] += 1
            forced["modelled"] += banked
            forced["waves"].update(sorted(w["waves"]) or ["(unnamed wave)"])
        if k == "quoted_to_end" and banked > 0:
            need = 1.0 / banked
            for lim, lab in SIZE_NEEDED_BUCKETS:
                if need <= lim:
                    size_needed[lab] += 1
                    break
    cost = {k: {"pnl": 0.0, "fills": 0, "rest_sum": 0.0, "days": 0} for k in split}
    end = day.isoformat()
    start = (day - timedelta(days=6)).isoformat()
    for d in [x for x in archive_days() if start <= x <= end]:
        b = load_archive(d)
        if not b:
            continue
        for t, v in market_rows(b):
            k = cls_of.get(t)
            if k is None:
                continue
            cc = cost[k]
            cc["pnl"] += _f(v.get("pnl"))
            cc["fills"] += int(_f(v.get("fills")))
            cc["rest_sum"] += _f(v.get("rest"))
            cc["days"] += 1
    return {
        "split": {k: {"n": v["n"], "modelled": round(v["modelled"], 2),
                      "by_series": v["series"].most_common(6)} for k, v in split.items()},
        "wave_forced": {"n": forced["n"], "modelled": round(forced["modelled"], 2),
                        "by_wave": forced["waves"].most_common(6),
                        "basis": "wave lists as of this run (post_vote_dead_books.json)"},
        "size_needed": size_needed,
        "cost_7d": {k: {"pnl": round(v["pnl"], 2), "fills": v["fills"],
                        "rest_avg": round(v["rest_sum"] / v["days"], 2) if v["days"] else 0.0}
                    for k, v in cost.items()},
        "cost_basis": (f"trading P&L at last-trade marks, fills and average $ resting per market-day, "
                       f"{start} to {end} ET (day archive, MEASURED); size needed = 1 / banked (MODELLED)")}


# ---------------------------------------------------------------------------
# 5. Coverage and uptime
# ---------------------------------------------------------------------------

def _zstamp(ts: float) -> bytes:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ").encode("ascii")


def _fb(b: bytes) -> float:
    try:
        x = float(b)
    except ValueError:
        return 0.0
    return x if math.isfinite(x) else 0.0


# One pass over an ET day's cycle logs feeds both the coverage section (the
# distinct cycle timestamps) and the moat's pool share. The 11/26 04:30 run
# spent 1,050 of its collect's 1,181 s in the old text-mode share scan (it
# decoded and split all 34 columns of every row, at BelowNormal priority beside
# the bot), with the launcher's limit at 1,500 s. Now: bytes, timestamps
# compared as text, rows outside the day skipped before any split, the split
# stopped at the last column needed, and the read stopped an hour past the day.
# Cached per file: a file last written more than 10 min past the day's stop
# already holds all of the day (the bot only appends), so it keys as complete
# and the live UTC file growing between the moat and coverage sections does
# not force a second read; a file still inside the window keys by size and
# mtime, and a .gz (a re-run for an old day) is another path.
_CYCLE_PASS: Dict[tuple, dict] = {}


def _cycle_pass(day: date) -> dict:
    s, e = et_bounds(day)
    stop_ts = e + 3600
    paths = [imm_logfiles.resolve(os.path.join(STATUS_DIR, f"cycle_log_{u}.csv")) for u in utc_dates(day)]
    ident, present = [], []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            ident.append((p, None))
            continue
        present.append(p)
        ident.append((p, "complete") if st.st_mtime > stop_ts + 600 else (p, st.st_size, st.st_mtime_ns))
    key = (day.isoformat(), tuple(ident))
    if key in _CYCLE_PASS:
        return _CYCLE_PASS[key]
    lo, hi, stop = _zstamp(s), _zstamp(e), _zstamp(stop_ts)
    times = set()
    num: Dict[str, float] = defaultdict(float)
    den: Dict[str, float] = defaultdict(float)
    cycles: Dict[str, set] = defaultdict(set)
    fam_of: Dict[bytes, str] = {}
    for path in present:
        try:
            fh = imm_logfiles.open_bin(path)
        except OSError:
            continue
        idx, need = None, 0
        last_head, last_in = b"", False
        with fh:
            for line in fh:
                if line.startswith(b"ts,"):
                    cols = line.rstrip(b"\r\n").split(b",")
                    try:
                        idx = (cols.index(b"ticker"), cols.index(b"est_frac"), cols.index(b"pool_per_day"))
                        need = max(idx)
                    except ValueError:
                        idx = None
                    continue
                head = line[:20]
                if head != last_head:
                    if not head[:2].isdigit():
                        continue
                    last_head = head
                    last_in = lo <= head < hi
                    if last_in:
                        try:
                            times.add(datetime.strptime(head.decode("ascii"), "%Y-%m-%dT%H:%M:%SZ")
                                      .replace(tzinfo=timezone.utc).timestamp())
                        except ValueError:
                            last_in = False
                    elif head >= stop:
                        break       # rows are written in cycle order: the rest is a later day
                if not last_in or idx is None:
                    continue
                parts = line.split(b",", need + 1)
                if len(parts) <= need:
                    continue
                pool = _fb(parts[idx[2]])
                if pool <= 0:
                    continue
                ser = parts[idx[0]].split(b"-", 1)[0]
                fam = fam_of.get(ser)
                if fam is None:
                    fam = fam_of[ser] = family_of(ser.decode("ascii", "replace"))[0]
                num[fam] += _fb(parts[idx[1]]) * pool
                den[fam] += pool
                cycles[fam].add(head)
    out = {"times": sorted(times),
           "share": {f: {"share": round(num[f] / den[f], 5), "pool_cov": round(den[f] / max(1, len(cycles[f])), 2)}
                     for f in den if den[f] > 0}}
    if len(_CYCLE_PASS) > 16:
        _CYCLE_PASS.clear()
    _CYCLE_PASS[key] = out
    return out


def cycle_times(day: date) -> List[float]:
    return list(_cycle_pass(day)["times"])


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
        ids = subject_ids(parts[2])
        res.append({"sha": parts[0].strip(), "at": et_str(_f(parts[1])), "subject": parts[2][:160],
                    # only the SUBJECT ships an item (apply mode writes "(review R-...)"
                    # there). A body that mentions one is context: d4d28c2 (11/26) opens
                    # "On the daily review's R-261125-01 ..." and shipped something else
                    "ids": ids, "mentions": sorted(set(LEDGER_ID.findall(body)) - set(ids)),
                    "bot": "incentive_mm.py" in names, "files": len(names),
                    # what section 11 matches against open items (a shared series or file)
                    "names": names[:40], "body": body.strip()[:400]})
    return res


_ID_TAIL = re.compile(r"\b(R-\d{6})-\d{2}((?:\s*(?:,|and|&)\s*-\d{2}\b)+)")


def subject_ids(subject: str) -> List[str]:
    """Ledger ids a commit subject names, with the shorthand "R-261124-05, -06"."""
    ids = set(LEDGER_ID.findall(subject or ""))
    for m in _ID_TAIL.finditer(subject or ""):
        for n in re.findall(r"-(\d{2})\b", m.group(2)):
            ids.add(f"{m.group(1)}-{n}")
    return sorted(ids)


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
            for k in ("severity", "title", "summary", "impact", "evidence", "ask", "plan", "metric", "email",
                      "recheck_on", "compound", "self_apply"):
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


def _iso_day(v) -> Optional[str]:
    try:
        return date.fromisoformat(str(v).strip()[:10]).isoformat() if v else None
    except ValueError:
        return None


SELF_APPLY = ("watch", "lesson", "playbook")


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
            # a promise to look again ("re-measured 12/02") gets a date the ledger
            # enforces: ledger_due lists the item on that day, shipped or not
            "recheck_on": _iso_day(f.get("recheck_on") or (
                f["metric"].get("recheck_on") if isinstance(f.get("metric"), dict) else None)),
            "compound": bool(f.get("compound")),
            "self_apply": f.get("self_apply") if f.get("self_apply") in SELF_APPLY else None,
            "ledger_id": f.get("ledger_id")}


def _clean_compound(c: dict) -> dict:
    """findings.json "compound": the day's answer to "what else should this
    review be doing to compound and improve itself?" (Jack 2026-11-26). Each
    becomes a ledger item of kind review. One the review applies itself (a
    watch, a lesson, a playbook rule) is open; anything else waits on Jack."""
    c = dict(c)
    c["kind"] = "review"
    c["compound"] = True
    fp = str(c.get("fingerprint") or "")
    if not fp:
        c["fingerprint"] = "review-" + str(c.get("title") or "untitled")
    if not c.get("summary") and c.get("gap"):
        c["summary"] = str(c["gap"])
    if not c.get("plan") and c.get("change"):
        c["plan"] = {"change": str(c["change"])}
    if "decision_needed" not in c:
        c["decision_needed"] = bool(c.get("needs_jack", c.get("self_apply") not in SELF_APPLY))
    return c


def ledger_ingest(day: date, path: Optional[str] = None) -> dict:
    iso = day.isoformat()
    path = path or os.path.join(REVIEW_DIR, iso, "findings.json")
    data = _load_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"findings file missing or not a JSON object: {path}")
    items = ledger_state()
    by_fp = {}
    # an item this same file closes keeps its id on a second ingest of the day,
    # instead of reopening as a new item (a closed fingerprint seen on a LATER
    # day still reopens, as before)
    closing = {c.get("id") for c in data.get("closures") or [] if isinstance(c, dict)}
    for iid, it in sorted(items.items()):
        if it.get("status") not in TERMINAL or iid in closing:
            by_fp[it.get("fingerprint")] = iid
    ids, created, seen = {}, [], []
    raws = [r for r in data.get("findings") or [] if isinstance(r, dict)]
    raws += [_clean_compound(c) for c in data.get("compound") or [] if isinstance(c, dict)]
    for raw in raws:
        f = _clean_finding(raw)
        iid = f["ledger_id"] if f["ledger_id"] in items else by_fp.get(f["fingerprint"])
        body = {k: f[k] for k in ("kind", "severity", "fingerprint", "title", "summary",
                                  "evidence", "impact", "ask", "plan", "metric", "email",
                                  "recheck_on", "compound", "self_apply")
                if f[k] not in (None, False) or k not in ("recheck_on", "compound", "self_apply")}
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
    # a second ingest of the same day (a run that fixed a rejected watch and
    # ingested again, or the launcher's catch-up) must not append the same
    # evaluation, closure, lesson or playbook line twice
    prev = _load_json(os.path.join(REVIEW_DIR, iso, "ingested.json"), {}) or {}
    done = {k: set(v) for k, v in (prev.get("done") or {}).items() if isinstance(v, list)}

    def first_time(kind: str, obj) -> bool:
        h = hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        s = done.setdefault(kind, set())
        if h in s:
            return False
        s.add(h)
        return True

    evals = 0
    for e in data.get("evaluations") or []:
        if isinstance(e, dict) and e.get("id") in items and first_time("evals", e):
            ledger_append({"op": "eval", "id": e["id"], "day": iso,
                           "verdict": e.get("verdict") if e.get("verdict") in (
                               "verified", "failed", "inconclusive") else "inconclusive",
                           "before": e.get("before"), "after": e.get("after"),
                           "delta_usd_day": e.get("delta_usd_day"), "note": str(e.get("note") or "")[:600]})
            evals += 1
    closes = 0
    for c in data.get("closures") or []:
        if isinstance(c, dict) and c.get("id") in items and first_time("closes", c):
            st = c.get("status") if c.get("status") in TERMINAL else "closed"
            ledger_append({"op": "close", "id": c["id"], "status": st,
                           "note": str(c.get("note") or "")[:600]})
            closes += 1
    # the review's own read-only checks (findings.json "watches" / "retire_watches")
    wres = watches_ingest(day, data.get("watches") or [], data.get("retire_watches") or [])
    # the change registry: scopes for what went live, skips, readout verdicts
    cres = registry_ingest(day, data.get("changes") or [], first_time)
    lessons = [str(x).strip() for x in data.get("lessons") or [] if str(x).strip()]
    lessons = [l for l in lessons[:5] if first_time("lessons", l)]
    if lessons:
        with open(os.path.join(REVIEW_DIR, "lessons.md"), "a", encoding="utf-8") as fh:
            for l in lessons:
                fh.write(f"- {iso}: {l[:400]}\n")
    # PROCESS MOAT: a verified fix becomes a reusable rule the next runs apply
    # to other families and new waves (moat/playbook.md, one line per rule)
    rules = [p for p in data.get("playbook") or [] if isinstance(p, dict) and str(p.get("rule") or "").strip()]
    rules = [p for p in rules[:5] if first_time("playbook", p)]
    pw = prev.get("watches") or {}
    _write_json(os.path.join(REVIEW_DIR, iso, "ids.json"), {**(_load_json(os.path.join(REVIEW_DIR, iso, "ids.json"), {})
                                                               if prev else {}), **ids})
    _write_json(os.path.join(REVIEW_DIR, iso, "ingested.json"),
                {"at": _now_iso(), "created": list(dict.fromkeys((prev.get("created") or []) + created)),
                 "seen": seen, "evals": int(_f(prev.get("evals"))) + evals,
                 "closes": int(_f(prev.get("closes"))) + closes,
                 "watches": {k: list(dict.fromkeys((pw.get(k) or []) + wres[k])) if k != "rejected" else wres[k]
                             for k in ("added", "updated", "retired", "rejected")},
                 "changes": {k: (list(dict.fromkeys(((prev.get("changes") or {}).get(k) or []) + cres[k]))
                                 if k != "rejected" else cres[k])
                             for k in ("scoped", "judged", "skipped", "rejected")},
                 "done": {k: sorted(v) for k, v in done.items()}})
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
    return {"created": created, "seen": seen, "evals": evals, "closes": closes, "ids": ids,
            "watches": wres, "changes": cres}


def ledger_due(day: date) -> List[dict]:
    """Items to measure today: shipped ones past their eval_after_days, and any
    open item whose recheck_on date has come (a promise to look again, shipped
    or not: 11/25's "re-measured 12/02" had no mechanism until 11/26)."""
    out = []
    for iid, it in ledger_state().items():
        last_eval = max((e.get("day") or "" for e in it.get("evals") or []), default="")
        rc = it.get("recheck_on")
        if rc and it.get("status") not in TERMINAL and it.get("status") != "shipped" \
                and rc <= day.isoformat() and last_eval < rc:
            out.append({"id": iid, "title": it.get("title"), "why": "recheck", "due": rc,
                        "status": it.get("status"), "metric": it.get("metric")})
            continue
        if it.get("status") != "shipped" or not it.get("ship"):
            continue
        at = _ts_of(it["ship"].get("at"))
        if at is None:
            continue
        wait = _f((it.get("metric") or {}).get("eval_after_days"), 7)
        due_day = datetime.fromtimestamp(at, ET).date() + timedelta(days=int(wait))
        if due_day <= day and last_eval < due_day.isoformat():
            out.append({"id": iid, "title": it.get("title"), "why": "shipped", "shipped": et_str(at),
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
    rejected = sum(1 for it in decided if any(d.get("status") == "rejected" for d in it["decisions"]))
    fps = [iid for iid, it in items.items() if _is_false_positive(it)]
    judged = by_status.get("verified", 0) + by_status.get("failed", 0)
    return {"items": len(items), "by_status": dict(by_status), "by_kind": dict(by_kind),
            "verified_delta_usd_day": round(gain, 2), "decided": len(decided), "approved": approved,
            "rejected": rejected, "failed": by_status.get("failed", 0),
            "verified": by_status.get("verified", 0),
            "precision": round(by_status.get("verified", 0) / judged, 3) if judged else None,
            "false_positives": len(fps), "false_positive_ids": sorted(fps),
            "compound": sum(1 for it in items.values() if it.get("compound"))}


_FP_NOTE = re.compile(r"false[ -]positive|was wrong|misread|not a (?:bug|defect)", re.I)


def _is_false_positive(it: dict) -> bool:
    """Closed as false-positive, or (before that status existed, 11/26) closed
    with a note that says so (R-261124-01)."""
    if it.get("status") == "false-positive":
        return True
    note = str((it.get("closed") or {}).get("note") or "")
    return it.get("status") in TERMINAL and bool(_FP_NOTE.search(note))


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
    share falling at a steady size is other makers crowding in. The scan is
    shared with the coverage section (_cycle_pass)."""
    return {f: dict(v) for f, v in _cycle_pass(day)["share"].items()}


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
            "review_calibration": review_calibration(),
            "backup": backup_status()}


BACKUP_STALE_H = 36                  # the backup runs daily after the email: older than this is late


def backup_status(now: Optional[float] = None) -> dict:
    """The moat's BigQuery backup, from the last imm_review_backup.py run
    (backup_state.json; review R-261124-20): ok / FAILED / off / never run,
    how old, what it added, the copy's row counts, and any refusal."""
    st = _load_json(os.path.join(REVIEW_DIR, "backup_state.json"))
    if not isinstance(st, dict):
        return {"state": "never run"}
    at = _ts_of(st.get("at"))
    age = round(((now or time.time()) - at) / 3600, 1) if at else None
    state = "off" if st.get("off") else ("ok" if st.get("ok") else "FAILED")
    if state == "ok" and age is not None and age > BACKUP_STALE_H:
        state = "stale"
    return {"state": state, "at": et_str(at, "%Y-%m-%d %H:%M") if at else None, "age_h": age,
            "dataset": st.get("dataset"), "added": st.get("added") or {}, "totals": st.get("totals") or {},
            "errors": (st.get("errors") or [])[:5], "refused": (st.get("refused") or [])[:5],
            "secs": st.get("secs")}


# ---------------------------------------------------------------------------
# 11. The review itself: how it compounds. Jack 2026-11-26: "what else should
# this skill be doing daily to compound and improve itself? everyday it should
# ask itself this question and come with opportunities".
#
# The skill answers that question on every run (SKILL.md step 8, findings.json
# "compound"). This section hands it facts instead of a blank page:
#   record    its own score: precision, false positives, Jack's decisions,
#             approved but never applied, verified $/day
#   outside   what landed on the review day that the review did not raise:
#             commits naming no ledger id, memory notes other sessions wrote
#   overlaps  open items someone else acted on (a shared series, file or id)
#   runs      the last runs' cost and friction, from their own tool traces
#   repeated  analysis done by hand in two or more runs: belongs in collect
#   budget    this collect's section times against the launcher's limits
#   backlog   the open compound items, so a run never proposes one twice
#   watches   the read-only checks the review added itself, evaluated now
# ---------------------------------------------------------------------------

SELF_QUESTION = "What else should this review be doing, daily, to compound and improve itself?"
SELF_RUNS = 3                # tool traces read for repeated manual work
WATCH_TYPES = ("log", "sink", "file_age", "json", "family")
WATCH_MAX_NEW = 5            # per run
WATCH_MAX_ACTIVE = 60
WATCH_SINK_MAX_BYTES = 250 * 1024 * 1024
WATCH_STALE_DAYS = 30        # never fired in this long: the weekly pass proposes retiring it
FAMILY_METRICS = ("net", "rew", "pnl", "fills", "cts", "rest", "markets", "tax", "share")
_GENERIC_FILES = {"incentive_mm.py", "test_incentive_mm.py", "imm_review.py", "send_imm_review.py",
                  "test_imm_review.py", "run_incentive_mm.ps1"}
_TOKEN_RX = re.compile(r"\bKX[A-Z0-9]{2,}\b|\b[A-Za-z][A-Za-z0-9_]*\.(?:py|ps1)\b")
_SRC_NAMES = tuple(sorted(set(SINKS) | {"gap_scans", "day_archive", "family_daily", "priors", "program_history",
                                        "evidence", "ledger", "markets_meta", "imm_dashboard_summary",
                                        "status_incentive_mm", "claude_stream", "watch_results"},
                          key=len, reverse=True))
_SRC_RX = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(n) for n in _SRC_NAMES)
                     + r")(?=[_.\-/\\$*{\s'\"]|$)|(incentive-mm-(?:\d|\*|\$))")
# the runbook's own tools, repeated by design
_INTENDED = ("imm_review ledger", "imm_review measure", "imm_review collect", "imm_review put",
             "git log", "git show", "git diff", "ls", "wc", "head", "tail", "cat", "echo")


def launcher_limits() -> dict:
    """The launcher's time limits, read from the script beside this file."""
    out = {"collect_min": 25.0, "claude_min": 100.0}
    try:
        with open(os.path.join(HERE, "run_imm_daily_review.ps1"), encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except OSError:
        return out
    for k, var in (("collect_min", "CollectTimeoutMin"), ("claude_min", "ClaudeTimeoutMin")):
        m = re.search(r"\$" + var + r"\s*=\s*(\d+(?:\.\d+)?)", txt)
        if m:
            out[k] = float(m.group(1))
    return out


def _one_line(s, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 3] + "..."


def _sources(cmd: str) -> List[str]:
    out = set()
    for a, b in _SRC_RX.findall(cmd or ""):
        out.add("bot log" if b else a)
    return sorted(out)


def cmd_kind(cmd: str) -> str:
    """A Bash command reduced to what it was for: 'python snippet on fills+marks',
    'grep on orders', 'imm_review measure', 'assembling an output file'."""
    c = re.sub(r"^(?:\s*cd\s+\S+\s*(?:&&|;)\s*)+", "", (cmd or "").strip())
    words = c.split("\n", 1)[0].split()
    if not words:
        return "?"
    src = "+".join(_sources(c)[:4])
    on = f" on {src}" if src else ""
    w0 = words[0]
    if re.match(r"^[A-Za-z_]+=", w0) and re.search(r"cat\s*>>?|:\s*>", c):
        return "assembling an output file in chunks"
    if w0 in ("python", "python3", "py", "python.exe"):
        rest = [w for w in words[1:] if not w.startswith("-I")]
        if rest and rest[0].endswith(".py"):
            script = os.path.basename(rest[0])[:-3]
            sub = rest[1] if len(rest) > 1 and not rest[1].startswith(("-", "<", "|", ">")) else ""
            return f"{script} {sub}".strip()
        return "python snippet" + on
    if w0 == "git":
        return "git " + (words[1] if len(words) > 1 else "")
    if w0 in ("for", "while", "if"):
        return "shell loop" + on
    return w0 + on


def run_trace(day_iso: str) -> Optional[dict]:
    """What one unattended run did, from its stream-json tool trace and run.log."""
    d = os.path.join(REVIEW_DIR, day_iso)
    path = os.path.join(d, "claude_stream.jsonl")
    if not os.path.exists(path):
        return None
    tools: Counter = Counter()
    kinds: Counter = Counter()
    errors: List[dict] = []
    calls: Dict[str, str] = {}
    denials = 0
    result: dict = {}
    for r in _iter_jsonl(path):
        t = r.get("type")
        if t == "assistant":
            for b in (r.get("message") or {}).get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = b.get("name") or "?"
                tools[name] += 1
                inp = b.get("input") or {}
                if name == "Bash":
                    cmd = str(inp.get("command") or "")
                    kinds[cmd_kind(cmd)] += 1
                    calls[b.get("id") or ""] = cmd
                else:
                    calls[b.get("id") or ""] = f"{name} {inp.get('file_path') or inp.get('pattern') or ''}"
        elif t == "user":
            for b in (r.get("message") or {}).get("content") or []:
                if not (isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")):
                    continue
                txt = b.get("content")
                if isinstance(txt, list):
                    txt = " ".join(str(x.get("text") or "") for x in txt if isinstance(x, dict))
                txt = str(txt or "")
                if "Permission to use" in txt or "has been denied" in txt:
                    denials += 1
                errors.append({"cmd": _one_line(calls.get(b.get("tool_use_id") or "", "?"), 140),
                               "error": _one_line(txt, 200)})
        elif t == "result":
            result = r
    usage = result.get("usage") or {}
    out = {"day": day_iso, "turns": result.get("num_turns"),
           "minutes": round(_f(result.get("duration_ms")) / 60000.0, 1) if result.get("duration_ms") else None,
           "cost_usd": round(_f(result.get("total_cost_usd")), 2) if result.get("total_cost_usd") is not None else None,
           "output_tokens": usage.get("output_tokens"), "is_error": result.get("is_error"),
           "tools": dict(tools.most_common()), "bash": sum(kinds.values()),
           "kinds": dict(kinds.most_common()), "errors": errors[:8], "n_errors": len(errors),
           "denials": denials,
           "output_calls": kinds.get("assembling an output file in chunks", 0) + kinds.get("imm_review put", 0)}
    try:
        with open(os.path.join(d, "run.log"), encoding="utf-8-sig", errors="replace") as fh:
            log = fh.read()
        m = re.search(r"collect exit=(\S+) timedout=(\S+): evidence for \S+ in ([\d.]+)s", log)
        if m:
            out["collect_secs"] = _f(m.group(3))
            out["collect_timedout"] = m.group(2) == "True"
        m = re.search(r"=== done in (\d+) min, exit=(\d+)", log)
        if m:
            out["total_min"], out["exit"] = int(m.group(1)), int(m.group(2))
        out["fallback"] = "analysis did not run" in log      # the fallback email's subject
        out["tripwire"] = "TRIPWIRE" in log
    except OSError:
        pass
    return out


def repeated_manual(traces: List[dict]) -> List[dict]:
    """Command kinds done by hand in 2+ of the traced runs (3+ calls in all),
    the runbook's own tools aside: analysis the collector should do instead."""
    per: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for tr in traces:
        for k, n in (tr.get("kinds") or {}).items():
            per[k].append((tr["day"], int(n)))
    out = []
    for k, lst in per.items():
        if k.startswith(_INTENDED) or k == "?":
            continue
        tot = sum(n for _, n in lst)
        if len(lst) >= 2 and tot >= 3:
            out.append({"kind": k, "runs": len(lst), "calls": tot, "days": [d for d, _ in lst]})
    out.sort(key=lambda x: (-x["calls"], x["kind"]))
    return out[:12]


def _frontmatter(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4000)
    except OSError:
        return {}
    out = {}
    if head.startswith("---"):
        for line in head.split("\n")[1:40]:
            if line.strip() == "---":
                break
            m = re.match(r"^(name|description):\s*(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def memory_notes(day: date) -> List[dict]:
    """Project memory notes written or updated from the start of the ET day to
    this run (at most 6 h past the day): what other sessions found, fixed or
    decided with Jack. A note touched again after that drops out of the list."""
    s, e = et_bounds(day)
    e = min(time.time(), e + 6 * 3600)
    out = []
    try:
        names = os.listdir(MEMORY_DIR)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".md") or n == "MEMORY.md":
            continue
        p = os.path.join(MEMORY_DIR, n)
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if s <= m < e:
            fm = _frontmatter(p)
            out.append({"file": n, "name": fm.get("name") or n[:-3], "at": et_str(m),
                        "description": _one_line(fm.get("description"), 240)})
    out.sort(key=lambda x: x["at"])
    return out[:25]


# merges and the nightly high-temp digest are bookkeeping, never a miss
_NOT_A_CHANGE = re.compile(r"^(?:Merge |High-temp digest for )")


def _item_tokens(it: dict) -> set:
    text = " ".join(str(it.get(k) or "") for k in ("title", "summary", "fingerprint")) + " " + \
        json.dumps(it.get("plan") or {}) + " " + " ".join(str(x) for x in it.get("evidence") or [])
    return {t for t in _TOKEN_RX.findall(text) if t not in _GENERIC_FILES}


def overlaps(items: Dict[str, dict], commits: List[dict], notes: List[dict]) -> List[dict]:
    """Open items that a commit or a memory note of the day touched: the same
    series or file, or the note names the id. Someone else acting on a finding
    either confirms it, supersedes it or diagnoses it differently."""
    out = []
    srcs = []
    for c in commits:
        if _NOT_A_CHANGE.match(c.get("subject") or ""):
            continue
        toks = set(_TOKEN_RX.findall(c.get("subject", "") + " " + c.get("body", ""))) | \
            {os.path.basename(n) for n in c.get("names") or []}
        srcs.append((f"commit {c['sha']}", c.get("subject", ""), toks - _GENERIC_FILES,
                     set(c.get("mentions") or []), set(c.get("ids") or [])))
    for n in notes:
        text = f"{n.get('name')} {n.get('description')}"
        srcs.append((f"memory {n['file']}", n.get("description", ""), set(_TOKEN_RX.findall(text)) - _GENERIC_FILES,
                     set(LEDGER_ID.findall(text)), set()))
    for iid, it in sorted(items.items()):
        if it.get("status") in TERMINAL:
            continue
        mine = _item_tokens(it)
        hits = []
        for label, text, toks, ids, shipped_by in srcs:
            if iid in shipped_by:
                continue        # the commit that shipped it: the ledger already knows
            if iid in ids and it.get("status") == "shipped" and label.startswith("memory"):
                continue        # a note recording its own ship
            hit = sorted(mine & toks)
            if iid in ids or hit:
                hits.append({"id": iid, "title": _one_line(it.get("title"), 100), "by": label,
                             "what": _one_line(text, 160),
                             "match": "names the id" if iid in ids else ", ".join(hit[:4])})
        # an item that names the id outranks a shared series; 3 leads an item at most
        hits.sort(key=lambda h: h["match"] != "names the id")
        out += hits[:3]
    out.sort(key=lambda h: h["match"] != "names the id")
    return out[:20]


def self_record(day: date) -> dict:
    items = ledger_state()
    st = ledger_stats()
    approved = []
    lags = []
    for iid, it in items.items():
        decs = it.get("decisions") or []
        if decs and it.get("first_seen"):
            t0 = _ts_of(decs[0].get("ts"))
            if t0:
                lags.append((datetime.fromtimestamp(t0, ET).date() - date.fromisoformat(it["first_seen"])).days)
        if it.get("status") == "approved":
            at = _ts_of(decs[-1].get("ts")) if decs else None
            approved.append({"id": iid, "title": _one_line(it.get("title"), 100),
                             "days": (day - datetime.fromtimestamp(at, ET).date()).days if at else None})
    proposed = [(day - date.fromisoformat(it["first_seen"])).days for it in items.values()
                if it.get("status") == "proposed" and it.get("first_seen")]
    by_kind: Dict[str, Counter] = defaultdict(Counter)
    for it in items.values():
        k = it.get("kind") or "?"
        by_kind[k]["raised"] += 1
        if any(d.get("status") == "approved" for d in it.get("decisions") or []):
            by_kind[k]["approved"] += 1
        if any(d.get("status") == "rejected" for d in it.get("decisions") or []):
            by_kind[k]["rejected"] += 1
        if it.get("status") in ("verified", "failed"):
            by_kind[k][it["status"]] += 1
        if _is_false_positive(it):
            by_kind[k]["false_positive"] += 1
    return {"stats": st, "approved_unapplied": approved,
            "proposed": len(proposed), "oldest_proposed_days": max(proposed) if proposed else None,
            "decision_lag_days": sorted(lags)[len(lags) // 2] if lags else None,
            "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())}}


def self_budget(ev: dict, traces: List[dict]) -> dict:
    lim = launcher_limits()
    secs = {k: v.get("_secs") for k, v in ev.items() if isinstance(v, dict) and v.get("_secs") is not None}
    elapsed = time.time() - _f(ev.get("generated"), time.time())
    prev = [{"day": t["day"], "collect_secs": t.get("collect_secs"), "claude_min": t.get("minutes"),
             "total_min": t.get("total_min")} for t in traces]
    return {"collect_limit_s": lim["collect_min"] * 60, "claude_limit_min": lim["claude_min"],
            "collect_so_far_s": round(elapsed, 1),
            "collect_pct": round(elapsed / (lim["collect_min"] * 60), 3) if lim["collect_min"] else None,
            "slowest": sorted(secs.items(), key=lambda kv: -_f(kv[1]))[:5], "previous": prev}


# --- watches: the review's own read-only checks (findings.json "watches") ---

def watches_path() -> str:
    return os.path.join(REVIEW_DIR, "watches.json")


def load_watches() -> List[dict]:
    d = _load_json(watches_path(), {}) or {}
    return [w for w in (d.get("watches") or []) if isinstance(w, dict) and w.get("id")]


def _save_watches(ws: List[dict]) -> None:
    _write_json(watches_path(), {"updated": _now_iso(), "watches": ws})


def _safe_path(rel) -> Optional[str]:
    """A watch reads only under run-logs: a relative path that stays inside."""
    rel = str(rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in rel.split("/"):
        return None
    root = os.path.normpath(RUN_LOGS)
    full = os.path.normpath(os.path.join(root, rel))
    return full if full.startswith(root + os.sep) else None


def _num_or_none(v) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def clean_watch(w: dict) -> Tuple[Optional[dict], str]:
    t = w.get("type")
    if t not in WATCH_TYPES:
        return None, f"type must be one of {', '.join(WATCH_TYPES)}"
    title = _one_line(w.get("title"), 160)
    if not title:
        return None, "needs a title: what it guards against"
    lo, hi = _num_or_none(w.get("min")), _num_or_none(w.get("max"))
    if t == "file_age":
        hi = _num_or_none(w.get("max_hours", w.get("max")))
    if lo is None and hi is None:
        return None, "needs min and/or max (file_age: max_hours)"
    sev = str(w.get("severity") or "P2").upper()
    out = {"type": t, "title": title, "why": _one_line(w.get("why"), 200),
           "severity": sev if sev in ("P0", "P1", "P2", "P3") else "P2",
           "min": lo, "max": hi, "expires": _iso_day(w.get("expires"))}
    if t == "log":
        c = str(w.get("contains") or "")
        if not 3 <= len(c) <= 200:
            return None, "log: 'contains' must be a literal of 3-200 characters (it prefilters every line)"
        out["contains"] = c
        if w.get("regex"):
            rx = str(w["regex"])[:200]
            try:
                re.compile(rx)
            except re.error as e:
                return None, f"log: bad regex: {e}"
            out["regex"] = rx
    elif t == "sink":
        name = str(w.get("sink") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
            return None, "sink: a sink name like fills, toxic_halts (files <sink>_<UTC date>.jsonl)"
        out["sink"] = name
        if w.get("contains"):
            out["contains"] = str(w["contains"])[:200]
        if w.get("field"):
            out["field"] = str(w["field"])[:60]
            out["equals"] = None if w.get("equals") is None else str(w.get("equals"))[:120]
    elif t in ("file_age", "json"):
        if not _safe_path(w.get("path")):
            return None, f"{t}: 'path' must be relative to run-logs, e.g. incentive-mm/status_incentive_mm.json"
        out["path"] = str(w["path"]).replace("\\", "/")
        if t == "json":
            key = str(w.get("key") or "")
            if not re.fullmatch(r"[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+){0,6}", key):
                return None, "json: 'key' is a dotted path, e.g. reward.paid_today"
            out["key"] = key
    elif t == "family":
        fam, metric = str(w.get("family") or "").strip(), str(w.get("metric") or "")
        if not fam or metric not in FAMILY_METRICS:
            return None, f"family: needs 'family' and a metric in {', '.join(FAMILY_METRICS)}"
        out.update({"family": fam[:80], "metric": metric,
                    "days": int(min(30, max(1, _f(w.get("days"), 1))))})
    return out, ""


def _watch_spec(w: dict) -> str:
    return json.dumps({k: w.get(k) for k in sorted(w) if k in (
        "type", "contains", "regex", "sink", "field", "equals", "path", "key", "family", "metric", "days",
        "min", "max")}, sort_keys=True)


def watches_ingest(day: date, adds: list, retires: list) -> dict:
    """Add, update or retire the review's own watches. They only read run-logs,
    so the review may add them unattended; they never touch the bot."""
    ws = load_watches()
    by_id = {w["id"]: w for w in ws}
    active = [w for w in ws if not w.get("retired")]
    pre = "W-" + day.strftime("%y%m%d") + "-"
    added, updated, retired, rejected = [], [], [], []
    for raw in (adds or [])[:20]:
        if not isinstance(raw, dict):
            continue
        cw, err = clean_watch(raw)
        if cw is None:
            rejected.append({"title": _one_line(raw.get("title"), 80), "why": err})
            continue
        spec = _watch_spec(cw)
        if raw.get("id") in by_id:
            by_id[raw["id"]].update(cw, spec=spec, updated=day.isoformat())
            by_id[raw["id"]].pop("retired", None)
            updated.append(raw["id"])
            continue
        dup = next((w for w in active if w.get("spec") == spec), None)
        if dup:
            dup.update({"title": cw["title"], "why": cw["why"] or dup.get("why"), "updated": day.isoformat()})
            updated.append(dup["id"])
            continue
        if len(added) >= WATCH_MAX_NEW or len(active) >= WATCH_MAX_ACTIVE:
            rejected.append({"title": cw["title"][:80], "why": f"limit: {WATCH_MAX_NEW} new a run, "
                                                             f"{WATCH_MAX_ACTIVE} active"})
            continue
        n = sum(1 for k in by_id if k.startswith(pre)) + 1
        w = dict(cw, id=f"{pre}{n:02d}", added=day.isoformat(), spec=spec)
        ws.append(w)
        active.append(w)
        by_id[w["id"]] = w
        added.append(w["id"])
    for r in retires or []:
        rid = r.get("id") if isinstance(r, dict) else r
        if rid in by_id and not by_id[rid].get("retired"):
            by_id[rid]["retired"] = day.isoformat()
            by_id[rid]["retire_note"] = _one_line(r.get("note") if isinstance(r, dict) else "", 200)
            retired.append(rid)
    if added or updated or retired:
        _save_watches(ws)
    return {"added": added, "updated": updated, "retired": retired, "rejected": rejected}


def _log_watch_counts(day: date, ws: List[dict]) -> Dict[str, int]:
    s, e = et_bounds(day)
    needles = [(w["id"], w["contains"].encode("utf-8"), re.compile(w["regex"].encode("utf-8")) if w.get("regex") else None)
               for w in ws]
    counts = {w["id"]: 0 for w in ws}
    for p in _bot_log_files(day):
        try:
            fh = open(p, "rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                hits = [(wid, rx) for wid, nd, rx in needles if nd in raw]
                if not hits:
                    continue
                m = LOG_TS.match(raw)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(1).decode() + " " + ":".join(g.decode() for g in m.groups()[1:]),
                                           "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                if not (s <= ts < e):
                    continue
                for wid, rx in hits:
                    if rx is None or rx.search(raw):
                        counts[wid] += 1
    return counts


def _sink_watch_count(day: date, w: dict) -> float:
    s, e = et_bounds(day)
    nd = (w.get("contains") or "").encode("utf-8")
    n = 0
    found = False
    for u in utc_dates(day):
        p = imm_logfiles.resolve(os.path.join(STATUS_DIR, f"{w['sink']}_{u}.jsonl"))
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        found = True
        if size > WATCH_SINK_MAX_BYTES:
            raise ValueError(f"{os.path.basename(p)} is {size / 1e6:.0f} MB: too big for a watch "
                             "(propose a collector check instead)")
        with imm_logfiles.open_bin(p) as fh:
            for raw in fh:
                if nd and nd not in raw:
                    continue
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                ts = _ts_of(r.get("ts") if isinstance(r, dict) else None)
                if ts is None or not (s <= ts < e):
                    continue
                if w.get("field") and str(r.get(w["field"])) != str(w.get("equals")):
                    continue
                n += 1
    if not found:
        raise ValueError(f"no {w['sink']}_<UTC date>.jsonl file for the day")
    return float(n)


def _family_value(day: date, w: dict) -> Optional[float]:
    lo = (day - timedelta(days=w.get("days", 1) - 1)).isoformat()
    rows = [r for r in _read_rows(os.path.join(MOAT_DIR, "family_daily.csv"))
            if lo <= r.get("day", "") <= day.isoformat() and r.get("family") == w["family"]]
    if not rows:
        return None
    m = w["metric"]
    if m == "tax":
        rew = sum(_f(r.get("rew")) for r in rows)
        return -sum(_f(r.get("pnl")) for r in rows) / rew if rew > 0 else None
    if m == "share":
        xs = [_f(r["share"]) for r in rows if r.get("share") not in (None, "")]
        return sum(xs) / len(xs) if xs else None
    return sum(_f(r.get(m)) for r in rows)


def _json_value(w: dict) -> Optional[float]:
    obj = _load_json(_safe_path(w["path"]))
    for k in w["key"].split("."):
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and k.isdigit() and int(k) < len(obj):
            obj = obj[int(k)]
        else:
            return None
    if isinstance(obj, bool):
        return float(obj)
    return _num_or_none(obj)


def eval_watches(day: date, record: bool = True) -> dict:
    """Evaluate every active watch for the ET day. FIRING = outside its bounds
    (or its input is missing). Results append to watch_results.jsonl, the
    history the weekly pass reads (never fired in 30 days -> retire)."""
    if os.environ.get("IMM_REVIEW_WATCHES", "1") == "0":
        return {"off": True, "results": [], "firing": [], "errors": []}
    iso = day.isoformat()
    all_ws = load_watches()
    ws = [w for w in all_ws if not w.get("retired") and not (w.get("expires") and w["expires"] < iso)]
    results = []
    logw = [w for w in ws if w.get("type") == "log"]
    counts: Dict[str, int] = {}
    log_err = None
    if logw:
        try:
            counts = _log_watch_counts(day, logw)
        except Exception as e:   # pragma: no cover - a log read failure is reported, never fatal
            log_err = f"{type(e).__name__}: {e}"
    for w in ws:
        res = {"id": w["id"], "title": w.get("title"), "type": w.get("type"), "min": w.get("min"),
               "max": w.get("max"), "severity": w.get("severity"), "why": w.get("why"), "added": w.get("added")}
        try:
            t = w.get("type")
            if t == "log":
                if log_err:
                    raise ValueError(log_err)
                val = float(counts.get(w["id"], 0))
            elif t == "sink":
                val = _sink_watch_count(day, w)
            elif t == "file_age":
                p = _safe_path(w.get("path"))
                val = (time.time() - os.path.getmtime(p)) / 3600.0 if p and os.path.exists(p) else None
            elif t == "json":
                val = _json_value(w)
            elif t == "family":
                val = _family_value(day, w)
            else:
                raise ValueError(f"unknown type {t}")
            ok = val is not None and (w.get("min") is None or val >= w["min"]) and \
                (w.get("max") is None or val <= w["max"])
            res.update({"value": None if val is None else round(val, 4), "state": "ok" if ok else "FIRING"})
            if val is None:
                res["note"] = "input missing"
        except Exception as e:
            res.update({"value": None, "state": "error", "error": _one_line(f"{type(e).__name__}: {e}", 200)})
        results.append(res)
    hist_path = os.path.join(REVIEW_DIR, "watch_results.jsonl")
    if record and results:
        os.makedirs(REVIEW_DIR, exist_ok=True)
        with open(hist_path, "a", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps({"day": iso, "id": r["id"], "state": r["state"], "value": r.get("value")}) + "\n")
    last: Dict[Tuple[str, str], str] = {}
    for r in _iter_jsonl(hist_path):
        if r.get("day") and r.get("id"):
            last[(r["day"], r["id"])] = r.get("state") or ""
    fires: Counter = Counter()
    evals: Counter = Counter()
    for (d, wid), stt in last.items():
        evals[wid] += 1
        if stt == "FIRING":
            fires[wid] += 1
    stale = [w["id"] for w in ws if fires[w["id"]] == 0 and w.get("added")
             and (day - date.fromisoformat(w["added"])).days >= WATCH_STALE_DAYS]
    return {"active": len(ws), "retired": sum(1 for w in all_ws if w.get("retired")),
            "results": results, "firing": [r for r in results if r["state"] == "FIRING"],
            "errors": [r for r in results if r["state"] == "error"],
            "fires": {k: fires[k] for k in sorted(fires)}, "evals": {k: evals[k] for k in sorted(evals)},
            "stale": stale}


def sec_self(day: date, ev: dict) -> dict:
    iso = day.isoformat()
    items = ledger_state()
    commits = ((ev.get("changes") or {}).get("commits")) or []
    if not commits and "changes" not in ev:
        commits = sec_changes(day).get("commits") or []
    notes = memory_notes(day)
    # the runs before this one: D's own run has not happened yet when collect runs
    traces = []
    for back in range(0, SELF_RUNS + 4):
        tr = run_trace((day - timedelta(days=back)).isoformat())
        if tr:
            traces.append(tr)
        if len(traces) >= SELF_RUNS:
            break
    backlog = []
    for iid, it in sorted(items.items()):
        if it.get("status") in TERMINAL or not (it.get("compound") or it.get("kind") == "review"):
            continue
        backlog.append({"id": iid, "status": it.get("status"), "title": _one_line(it.get("title"), 110),
                        "compound": bool(it.get("compound")), "self_apply": it.get("self_apply"),
                        "age": (day - date.fromisoformat(it["first_seen"])).days if it.get("first_seen") else None,
                        "seen": len(it.get("seen_days") or [])})
    return {"question": SELF_QUESTION, "record": self_record(day),
            "outside": {"commits": [{k: c.get(k) for k in ("sha", "at", "subject", "bot", "mentions")}
                                    for c in commits if not c.get("ids")
                                    and not _NOT_A_CHANGE.match(c.get("subject") or "")][:20],
                        "memory": notes},
            "overlaps": overlaps(items, commits, notes),
            "rechecks": [d for d in ledger_due(day) if d.get("why") == "recheck"],
            "runs": [{k: v for k, v in t.items() if k != "kinds"} for t in traces],
            "repeated": repeated_manual(traces),
            "budget": self_budget(ev, traces),
            "backlog": backlog[:30],
            "watches": eval_watches(day)}


# ---------------------------------------------------------------------------
# 12. The change registry: every IMM change, measured at 7 days. Jack
# 2026-11-26: "Yes, build it" (the registry, so every IMM commit gets measured
# at 7 days, not only the review's own: 11 of the 12 bot commits since 11/25
# came from outside the review, and nothing measured whether they worked).
#
# REGISTER  every commit on main or origin/main that changes what the bot
#           does: the bot file, its launcher, a repo module the bot or an
#           input writer imports, a writer of a file the bot reads
#           (INPUT_FILES); and every IMM_* env change a bot run started with
#           (config_history). The scope (series, events, waves, families it
#           should move) is guessed from what the commit names; the review
#           corrects it the next morning (findings.json "changes").
# LIVE      a bot or launcher commit: the first bot run that started on a HEAD
#           containing it (config_history git_sha). A run started on
#           uncommitted edits of the bot file (source_mtime after its HEAD
#           reached main) counts for the next bot commit made within 6 hours:
#           the apply-then-commit path (d4d28c2 ran from 15:57Z, committed
#           17:25Z). A module or writer commit is live when main has it (the
#           reflog); an env change, at the run that started with it.
# READOUT   the 7 full ET days after the live day against the 7 before, for
#           the scope, from the dashboard's day archive: net, rewards
#           (MODELLED), trading (MEASURED at the last trade), fills, $ resting;
#           program supply on the same series; the rest of the book over the
#           same days; other changes on overlapping series in either window as
#           confounds. Statistics a trading desk would accept (Jack 11/26:
#           "remember you're building the next jane street of prediction
#           markets"): an exact permutation p (every 7/7 split of the 14 days),
#           Benjamini-Hochberg across the readouts read out together (199 tests
#           at p < 0.05 give ~10 hits by chance), and the smallest move the
#           test could see (80% power). The provisional call (better / worse:
#           p <= 0.05, surviving FDR q 0.10, |delta| >= max($5, 15%)) is still a
#           before/after, not causal; "no clear change" on a test that cannot
#           see the expected move is "can't tell", and a holdout settles it.
# VERDICT   the skill judges each readout (worked / hurt / no-effect /
#           inconclusive / extend); a change that hurt becomes a revert
#           finding; extend reads it again at 14 days.
#
# File: daily_review/changes.jsonl, append-only ops register / live / scope /
# readout / verdict / skip, folded by registry_state().
# ---------------------------------------------------------------------------

REGISTRY_DAYS = 7                    # the readout horizon (Jack: "measured at 7 days")
REGISTRY_EXTEND_DAYS = 14            # a readout the review extends comes again at 14 days
REGISTRY_BACKFILL_DAYS = 30          # the first run registers this much history
REGISTRY_LOOKBACK_DAYS = 21          # later runs re-read this much (a branch merged late)
REGISTRY_DIRTY_SPAN_S = 6 * 3600     # a run on uncommitted bot edits: the next bot commit within this
REGISTRY_MIN_USD = 5.0               # a move under $5/day is no clear change, whatever its p
REGISTRY_MIN_FRAC = 0.15             # ... or under 15% of the before level
REGISTRY_P = 0.05                    # two-sided permutation p for a call
REGISTRY_FDR_Q = 0.10                # Benjamini-Hochberg across one collect's readouts
REGISTRY_PERM_EXACT = 20000          # enumerate every split up to this many (7/7 days: 3,432)
REGISTRY_PERM_SAMPLES = 20000        # else this many seeded random relabellings
REGISTRY_MDE_Z = 2.8                 # 1.96 + 0.84: the move seen 80% of the time at p 0.05
REGISTRY_EARLY_USD = 20.0            # early warning: down $20+/day at t <= -2.5 after 2+ days
REGISTRY_EARLY_T = 2.5
REGISTRY_SHOW = 15                   # readouts listed in the evidence per run (all are recorded)
BOT_FILE = "incentive_mm.py"
LAUNCHER_FILES = ("run_incentive_mm.ps1", "restart_imm.ps1")
# writers of files the bot reads that it does not import (post_vote_dead_books.json
# and the wave fairs, cheap_side_books.json, release_calendar_found.json, the
# upcoming list the weather writers read, the family verdicts and auto-series
# lists, the award / election date rows, the earnings cutoff overrides) and the
# repo data file it reads. Their own imports are not followed: pvd_scan pulls in
# the quote-gap and email modules, which change no input.
INPUT_FILES = ("pvd_scan.py", "pvd_waves.py", "pvd_wave_pace.py", "pvd_wave_tune.py", "pvd_marginal.py",
               "wave_fair.py", "cheap_scan.py", "imm_release_calendar.py", "imm_upcoming.py",
               "imm_family_watch.py", "imm_rows.py", "imm_earnings_overrides.py", "release_calendar.json")
# imported by the bot, but a change there is display or the review itself; a
# family-rule change in imm_dashboard is registered by hand (`changes add SHA`)
NOT_BOT_FILES = ("imm_dashboard.py", "imm_review.py", "send_imm_review.py")
CHANGE_VERDICTS = ("worked", "hurt", "no-effect", "inconclusive", "extend")
CHANGE_METRICS = ("net", "rew", "pnl", "fills", "cts", "rest")
_MONEY = ("net", "rew", "pnl")
_AGG_KEYS = ("rew", "pnl", "fills", "cts", "rest", "mt", "mt_cts")
_KX_RX = re.compile(r"\bKX[A-Z0-9]{2,}\b")
_WAVE_NAME_RX = re.compile(r"\bw\d{4}-[a-z0-9][a-z0-9-]*[a-z0-9]\b")
_IMPORT_RX = re.compile(r"^[ \t]*(?:from[ \t]+([A-Za-z_]\w*)[ \t.]|import[ \t]+([A-Za-z_][\w \t,.]*))", re.M)
_ENV_SECRET = re.compile(r"URL|KEY|TOKEN|SECRET|PASS|EMAIL|PHONE", re.I)
# words too generic to name a family on their own in a commit subject
_SCOPE_STOP = {"other", "general", "data", "company", "state", "international", "health", "candidates",
               "turnout", "food", "county", "earnings", "econ", "commodities", "energy", "awards", "trump",
               "the", "and", "new", "all", "space", "media", "prints"}
# words subjects use for a family that its rules do not spell (case-sensitive)
_SCOPE_WORDS = {"RT": {"series": ["KXRT"]}}


def bot_modules() -> set:
    """Repo modules the bot imports, transitively (`import X` or `from X import`
    naming an X.py in the repo), less NOT_BOT_FILES: the fair modules and the
    client libraries. A change there changes what the bot does at its next start."""
    global _BOT_MODULES
    if _BOT_MODULES is not None:
        return _BOT_MODULES
    seen: set = set()
    todo = [BOT_FILE]
    while todo and len(seen) < 500:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        try:
            with open(os.path.join(LIVE_REPO, f), encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
        except OSError:
            continue
        for m in _IMPORT_RX.finditer(txt):
            names = [m.group(1)] if m.group(1) else [p.strip().split()[0].split(".")[0]
                                                     for p in m.group(2).split(",") if p.strip()]
            for n in names:
                fn = n + ".py"
                if fn not in seen and fn not in NOT_BOT_FILES and os.path.isfile(os.path.join(LIVE_REPO, fn)):
                    todo.append(fn)
    _BOT_MODULES = seen - {BOT_FILE} - set(INPUT_FILES)
    return _BOT_MODULES


def change_kind(files: Iterable[str]) -> Optional[str]:
    """bot / launcher / input for a commit touching those files, else None."""
    names = {str(f).replace("\\", "/") for f in files}
    if BOT_FILE in names:
        return "bot"
    if names & set(LAUNCHER_FILES):
        return "launcher"
    mods = bot_modules()
    if any(n in mods or n in INPUT_FILES for n in names):
        return "input"
    return None


def _family_aliases(series: Iterable[str]) -> Dict[str, Tuple[str, str]]:
    """{alias: ("families" | "groups", name)} for the families and groups the
    dashboard rules give `series`: each name, and its first word or two when
    distinctive ("Vercel shares" -> "vercel"; "Carbon Arc consumer" -> "carbon arc").
    A family wins an alias it shares with one of its groups."""
    pool: Dict[str, set] = {"families": set(), "groups": set()}
    for s in series:
        f, g = family_of(s)
        for kind, n in (("families", f), ("groups", g)):
            if n and n != s and not re.fullmatch(r"KX[A-Z0-9]+", n):
                pool[kind].add(n)
    out: Dict[str, Tuple[str, str]] = {}
    for kind in ("groups", "families"):
        for n in sorted(pool[kind]):
            words = re.findall(r"[A-Za-z][A-Za-z0-9]*", n)
            cands = {n.lower()}
            if len(words) >= 2:
                if len(words[0]) >= 4:
                    cands.add(words[0].lower())
                cands.add((words[0] + " " + words[1]).lower())
            for c in cands:
                if c in _SCOPE_STOP or len(c) < 3:
                    continue
                if kind == "families" or c not in out:
                    out[c] = (kind, n)
    return out


def _named_families(text: str, aliases: Dict[str, Tuple[str, str]]) -> Dict[str, List[str]]:
    """Families and groups `text` names: the longest alias wins where two overlap
    ("sports ladders" is the group, not the Sports family)."""
    low = (text or "").lower()
    hits = []
    for al, (kind, name) in aliases.items():
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(al) + r"(?![a-z0-9])", low):
            hits.append((m.start(), m.end(), kind, name))
    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
    taken: List[Tuple[int, int]] = []
    out: Dict[str, set] = {"families": set(), "groups": set()}
    for s, e, kind, name in hits:
        if any(s < te and ts < e for ts, te in taken):
            continue
        taken.append((s, e))
        out[kind].add(name)
    return {k: sorted(v) for k, v in out.items() if v}


def infer_scope(subject: str, body: str = "", aliases: Optional[Dict[str, Tuple[str, str]]] = None) -> dict:
    """What a commit should move, from what it names: series in the subject,
    families and groups the subject names, wave names; series named only in the
    body when the subject names nothing; else the whole book."""
    subj, body = subject or "", body or ""
    sc: dict = {}
    basis = []
    st = list(dict.fromkeys(_KX_RX.findall(subj)))
    if st:
        sc["series"] = st[:12]
        basis.append("series in the subject")
    # a family only when the subject names no series: "Carbon Arc TikTok Shop
    # series (KXHAUSTTS ...)" is those series, not all 149 Carbon Arc ones
    named = {} if st else _named_families(subj, aliases or {})
    if named:
        sc.update(named)
        basis.append("family in the subject")
    for word, extra in _SCOPE_WORDS.items():
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", subj):
            for k, v in extra.items():
                sc[k] = sorted(set(sc.get(k) or []) | set(v))
            basis.append(f"'{word}' in the subject")
    waves = sorted(set(_WAVE_NAME_RX.findall(subj + " " + body)))
    if waves:
        sc["waves"] = waves[:6]
        basis.append("wave named")
    if not sc:
        bt = list(dict.fromkeys(_KX_RX.findall(body)))
        if bt:
            sc["series"] = bt[:12]
            basis.append("series in the body")
    if not sc:
        return {"book": True, "basis": "names no series, wave or family"}
    sc["basis"] = "; ".join(basis)
    return sc


def clean_scope(sc) -> Tuple[Optional[dict], str]:
    """A scope from findings.json or the CLI, or (None, why)."""
    if not isinstance(sc, dict):
        return None, "scope must be an object"
    if sc.get("book"):
        return {"book": True, "basis": str(sc.get("basis") or "the whole book")[:200]}, ""
    out: dict = {}
    for k in ("series", "events", "waves", "families", "groups"):
        v = sc.get(k) or []
        if isinstance(v, str):
            v = [x for x in re.split(r"[,\s]+", v) if x] if k in ("series", "events", "waves") else [v]
        v = [str(x).strip() for x in v if str(x).strip()][:40]
        if v:
            out[k] = v
    if sc.get("pattern"):
        try:
            re.compile(str(sc["pattern"]))
        except re.error as e:
            return None, f"bad pattern: {e}"
        out["pattern"] = str(sc["pattern"])[:200]
    if not out:
        return None, "the scope names nothing (book: true for the whole book)"
    out["basis"] = str(sc.get("basis") or "set by the review")[:200]
    return out, ""


def scope_label(sc: Optional[dict]) -> str:
    if not sc or sc.get("book"):
        return "whole book"
    parts = []
    for k, n in (("series", 4), ("events", 3), ("waves", 3), ("groups", 3), ("families", 3)):
        v = sc.get(k) or []
        if v:
            parts.append(", ".join(v[:n]) + (f" +{len(v) - n}" if len(v) > n else ""))
    if sc.get("pattern"):
        parts.append("/" + sc["pattern"] + "/")
    return "; ".join(parts) or "whole book"


def scope_key(sc: Optional[dict]) -> str:
    if not sc or sc.get("book"):
        return "book"
    return json.dumps({k: (sorted(sc[k]) if isinstance(sc[k], list) else sc[k])
                       for k in ("series", "events", "waves", "families", "groups", "pattern") if sc.get(k)},
                      sort_keys=True)


def registry_path() -> str:
    return os.path.join(REVIEW_DIR, "changes.jsonl")


def registry_append(ev: dict) -> None:
    ev = dict(ev)
    ev.setdefault("at", _now_iso())
    os.makedirs(REVIEW_DIR, exist_ok=True)
    with open(registry_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, sort_keys=True, default=str) + "\n")


def registry_state() -> Dict[str, dict]:
    """{key: entry} folded from changes.jsonl. A key is the commit's short sha
    or env:<NAME>@<run_id>."""
    st: Dict[str, dict] = {}
    for ev in _iter_jsonl(registry_path()):
        op = ev.get("op")
        keys = ev.get("keys") if isinstance(ev.get("keys"), list) else [ev.get("key")]
        if op == "register":
            k = ev.get("key")
            if k and k not in st:
                st[k] = {**{x: ev.get(x) for x in ("key", "full", "kind", "ct", "subject", "files", "ids",
                                                    "body", "registered", "run_id")},
                         "scope": ev.get("scope") or {"book": True}, "scope_by": "auto",
                         "readouts": {}, "verdicts": []}
            continue
        for k in keys:
            it = st.get(k)
            if it is None:
                continue
            if op == "live":
                it["live_at"] = _f(ev.get("live_at")) or None
                it["live_basis"] = ev.get("basis")
            elif op == "scope":
                if isinstance(ev.get("scope"), dict):
                    it["scope"] = ev["scope"]
                it["scope_by"] = ev.get("by") or "review"
                for x in ("metric", "expect"):
                    if ev.get(x):
                        it[x] = ev[x]
                if ev.get("expect"):          # when it was written: before the readout = pre-registered
                    it["expect_day"] = ev.get("day") or str(ev.get("at") or "")[:10]
            elif op == "readout":
                it["readouts"][str(ev.get("horizon"))] = {"day": ev.get("day"), "group": ev.get("group"),
                                                          "result": ev.get("result") or {}}
            elif op == "verdict":
                it["verdicts"].append({x: ev.get(x) for x in ("day", "verdict", "note", "finding", "by")})
                it["verdict"] = ev.get("verdict")
                if ev.get("verdict") == "extend":
                    it["horizon"] = REGISTRY_EXTEND_DAYS
            elif op == "skip":
                it["skipped"] = str(ev.get("note") or "skipped")
            elif op == "unskip":
                it.pop("skipped", None)
    return st


def resolve_key(st: Dict[str, dict], k) -> Optional[str]:
    """A registry key from a key, a full sha or a 4+ character prefix of either."""
    k = str(k or "").strip()
    if not k:
        return None
    if k in st:
        return k
    if len(k) < 4:
        return None
    c = [key for key, it in st.items() if key.startswith(k) or (it.get("full") or "").startswith(k)]
    return c[0] if len(c) == 1 else None


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def registry_commits(since_ts: float) -> List[dict]:
    """Non-merge commits on main and origin/main since `since_ts`: full sha,
    commit time, subject, body, files."""
    code, out = _git("log", "main", "origin/main", "--no-merges", f"--since={_utc_iso(since_ts)}",
                     "--format=%x1e%H%x1f%ct%x1f%s%x1f%b%x1d", "--name-only", timeout=120)
    if code != 0:
        return []
    res = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        head, _, files = chunk.partition("\x1d")
        parts = head.split("\x1f")
        if len(parts) < 3 or not re.fullmatch(r"[0-9a-f]{40}", parts[0].strip()):
            continue
        res.append({"full": parts[0].strip(), "ct": int(_f(parts[1])), "subject": parts[2].strip(),
                    "body": (parts[3] if len(parts) > 3 else "").strip(),
                    "files": [l.strip() for l in files.splitlines() if l.strip()]})
    return res


def registry_dag(since_ts: float) -> Tuple[List[str], Dict[str, List[str]]]:
    """Commits reachable from main or origin/main since `since_ts` (3 days of
    slack), children before parents, and each one's parents."""
    code, out = _git("rev-list", "--topo-order", "--parents", f"--since={_utc_iso(since_ts - 3 * 86400)}",
                     "main", "origin/main", timeout=120)
    order: List[str] = []
    parents: Dict[str, List[str]] = {}
    if code == 0:
        for line in out.splitlines():
            p = line.split()
            if p and re.fullmatch(r"[0-9a-f]{40}", p[0]):
                order.append(p[0])
                parents[p[0]] = p[1:]
    return order, parents


def registry_reflog() -> List[Tuple[int, str]]:
    """(unix time, sha) of every move of main on this box, oldest first."""
    code, out = _git("reflog", "show", "--date=unix", "--format=%H%x1f%gd", "main", timeout=60)
    res = []
    if code == 0:
        for line in out.splitlines():
            h, _, gd = line.partition("\x1f")
            m = re.search(r"@\{(\d{9,})\}", gd)
            if m and re.fullmatch(r"[0-9a-f]{40}", h.strip()):
                res.append((int(m.group(1)), h.strip()))
    res.sort()
    return res


def bot_runs(since_ts: float) -> List[dict]:
    """Every bot run since `since_ts` (config_history): start, run id, the HEAD
    it started on, its source file's mtime and its config, oldest first."""
    d0 = datetime.fromtimestamp(since_ts, timezone.utc).date() - timedelta(days=1)
    rows = []
    for p in glob.glob(os.path.join(STATUS_DIR, "config_history_*.jsonl")):
        m = re.search(r"config_history_(\d{4}-\d{2}-\d{2})\.jsonl$", p)
        if not m or date.fromisoformat(m.group(1)) < d0:
            continue
        for r in _iter_jsonl(p):
            ts = _ts_of(r.get("ts"))
            if ts is None:
                continue
            src = r.get("source_mtime")
            rows.append({"ts": ts, "run_id": str(r.get("run_id") or ""), "sha": str(r.get("git_sha") or ""),
                         "src": float(src) if isinstance(src, (int, float)) else None,
                         "config": r.get("config") if isinstance(r.get("config"), dict) else {}})
    rows.sort(key=lambda r: r["ts"])
    return rows


def resolve_live(order: List[str], parents: Dict[str, List[str]], reflog: List[Tuple[int, str]],
                 runs: List[dict], commits: List[dict]) -> Dict[str, Tuple[float, str]]:
    """{full sha: (live ts, basis)} for `commits` (full, kind, ct). Bot and
    launcher commits: the first run whose HEAD has them, or an earlier run that
    started on the uncommitted edits they became. Others: when main got them."""
    idx = {h: i for i, h in enumerate(order)}
    reach: Dict[str, int] = {}
    for h in reversed(order):                 # parents first: --topo-order lists children first
        b = 1 << idx[h]
        for p in parents.get(h) or ():
            b |= reach.get(p, 0)
        reach[h] = b
    by_short: Dict[str, List[str]] = defaultdict(list)
    for h in order:
        by_short[h[:7]].append(h)

    def full(s: str) -> Optional[str]:
        if s in reach:
            return s
        c = [h for h in by_short.get(s[:7], []) if h.startswith(s)] if len(s) >= 7 else []
        return c[0] if len(c) == 1 else None

    def members(mask: int):
        while mask:
            low = mask & -mask
            yield order[low.bit_length() - 1]
            mask ^= low

    arrival: Dict[str, float] = {}
    seen = 0
    for ts, h in reflog:
        b = reach.get(h)
        if b is None:
            continue
        for c in members(b & ~seen):
            arrival[c] = float(ts)
        seen |= b
    first_run: Dict[str, dict] = {}
    seen = 0
    for r in runs:
        h = full(r.get("sha") or "")
        if h is None:
            continue
        for c in members(reach[h] & ~seen):
            first_run[c] = r
        seen |= reach[h]
    bot_by_ct = sorted((int(_f(c.get("ct"))), c["full"]) for c in commits if c.get("kind") == "bot")
    dirty: Dict[str, dict] = {}
    for r in runs:
        h = full(r.get("sha") or "")
        if h is None or r.get("src") is None or arrival.get(h) is None or r["src"] <= arrival[h] + 5:
            continue
        # the bot file changed after its HEAD reached main: the run is on edits a
        # later commit records -- the next bot commit made within 6 hours
        for ct, c in bot_by_ct:
            if ct < r["src"] - 5:
                continue
            if ct - r["src"] > REGISTRY_DIRTY_SPAN_S:
                break
            if c not in idx or (reach[h] >> idx[c]) & 1:
                continue
            if c not in dirty or r["ts"] < dirty[c]["ts"]:
                dirty[c] = r
            break
    out: Dict[str, Tuple[float, str]] = {}
    for c in commits:
        f, kind = c["full"], c.get("kind")
        if kind in ("bot", "launcher"):
            fr, dr = first_run.get(f), dirty.get(f)
            if dr is not None and (fr is None or dr["ts"] < fr["ts"]):
                out[f] = (dr["ts"], f"bot run {dr['run_id']} {et_str(dr['ts'])} ET started on these edits "
                                    f"before they were committed ({et_str(_f(c.get('ct')))})")
            elif fr is not None:
                out[f] = (fr["ts"], f"first bot run on it: {fr['run_id']} {et_str(fr['ts'])} ET")
        elif arrival.get(f) is not None:
            out[f] = (arrival[f], f"reached main {et_str(arrival[f])} ET")
    return out


def env_changes(runs: List[dict]) -> List[dict]:
    """IMM_* env values that changed from one bot run to the next (a launcher
    edit or env_overrides.txt, applied at a task restart). Sets printed in
    another order are not changes; names that look like secrets are skipped."""
    out = []
    for prev, r in zip(runs, runs[1:]):
        a, b = prev.get("config") or {}, r.get("config") or {}
        if not a or not b:
            continue
        for k in sorted(set(a) | set(b)):
            if not k.startswith("IMM_") or _ENV_SECRET.search(k) or a.get(k) == b.get(k):
                continue
            ia, ib = _set_items(a.get(k)), _set_items(b.get(k))
            if ia is not None and ib is not None:
                if ia == ib:
                    continue
                add, rem = sorted(set(ib) - set(ia)), sorted(set(ia) - set(ib))
                desc = " ".join(x for x in (("+" + ",".join(add)) if add else "",
                                            ("-" + ",".join(rem)) if rem else "") if x)
                words = add + rem
            else:
                desc = f"{str(a.get(k))[:80]} -> {str(b.get(k))[:80]}"
                words = [str(a.get(k) or ""), str(b.get(k) or "")]
            out.append({"key": f"env:{k}@{r['run_id']}", "name": k, "change": desc[:240], "ts": r["ts"],
                        "run_id": r["run_id"], "tokens": sorted(set(_KX_RX.findall(" ".join(words))))[:12]})
    return out


def _day_agg(iso: str) -> Optional[Dict[Tuple[str, str], List[float]]]:
    """{(series, event): [rew, pnl, fills, cts, rest, mt, mt_cts, markets]} for an
    archived ET day, cached for the process."""
    if iso in _REG_DAYS:
        return _REG_DAYS[iso]
    blob = load_archive(iso)
    agg: Optional[dict] = None
    if blob:
        agg = {}
        for t, v in market_rows(blob):
            k = (series_of(t), event_of(t))
            a = agg.get(k)
            if a is None:
                a = agg[k] = [0.0] * (len(_AGG_KEYS) + 1)
            for i, f in enumerate(_AGG_KEYS):
                a[i] += _f(v.get(f))
            a[-1] += 1
    _REG_DAYS[iso] = agg
    return agg


def _supply_days(days: Iterable[str]) -> Dict[str, Dict[str, float]]:
    """{day: {series: program reward pool}} from program_history.jsonl, one pass
    for every day not yet cached."""
    days = sorted(set(days))
    want = [d for d in days if d not in _SUPPLY_CACHE]
    if want:
        got: Dict[str, Dict[str, float]] = {d: {} for d in want}
        for r in _iter_jsonl(os.path.join(STATUS_DIR, "program_history.jsonl")):
            d = r.get("date")
            if d in got:
                s = r.get("series") or ""
                got[d][s] = got[d].get(s, 0.0) + _f(r.get("total_reward_raw"))
        _SUPPLY_CACHE.update(got)
    return {d: _SUPPLY_CACHE.get(d) or {} for d in days}


def scope_matcher(sc: Optional[dict], universe: set, wave_events: Optional[dict] = None):
    """match(series, event) -> bool for a scope. A series token is exact when the
    archive knows that series, else a prefix (KXRAIN covers KXRAINNYCM)."""
    if not sc or sc.get("book"):
        return lambda s, e: True
    toks = [t for t in sc.get("series") or [] if t]
    exact = {t for t in toks if t in universe}
    pre = tuple(t for t in toks if t not in universe)
    fams = {x.lower() for x in sc.get("families") or []}
    grps = {x.lower() for x in sc.get("groups") or []}
    evs = set(sc.get("events") or [])
    if sc.get("waves"):
        we = wave_events if wave_events is not None else pvd_wave_events()
        for w in sc["waves"]:
            evs |= {e for e, v in we.items() if w in (v.get("waves") or ())}
    pat = None
    if sc.get("pattern"):
        try:
            pat = re.compile(sc["pattern"])
        except re.error:
            pat = None
    memo: Dict[Tuple[str, str], bool] = {}

    def m(s: str, e: str) -> bool:
        k = (s, e)
        if k in memo:
            return memo[k]
        hit = s in exact or (bool(pre) and s.startswith(pre)) or (bool(e) and e in evs)
        if not hit and (fams or grps):
            f, g = family_of(s)
            hit = (f or "").lower() in fams or (g or "").lower() in grps
        if not hit and pat is not None:
            hit = bool(pat.search(e or "") or pat.search(s))
        memo[k] = hit
        return hit
    return m


def _scope_rows(match, days: List[str]) -> Tuple[List[dict], set]:
    rows, hit = [], set()
    for iso in days:
        agg = _day_agg(iso)
        if agg is None:
            continue
        a = [0.0] * (len(_AGG_KEYS) + 1)
        bn = br = 0.0
        for (s, e), v in agg.items():
            bn += v[0] + v[1]
            br += v[4]
            if match(s, e):
                for i in range(len(a)):
                    a[i] += v[i]
                hit.add(s)
        r = dict(zip(_AGG_KEYS, a))
        r.update(day=iso, markets=a[-1], net=a[0] + a[1], book_net=bn, book_rest=br)
        rows.append(r)
    return rows, hit


def _mean(vals: List[float]) -> Optional[float]:
    return round(sum(vals) / len(vals), 2) if vals else None


def _welch_t(a: List[float], b: List[float]) -> Optional[float]:
    """(mean b - mean a) / its standard error, sample variances."""
    if len(a) < 2 or len(b) < 2:
        return None
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    if se <= 1e-9:
        return None
    return round((sum(b) / len(b) - sum(a) / len(a)) / se, 2)


def perm_p(a: List[float], b: List[float]) -> Optional[float]:
    """Two-sided p that mean(b) - mean(a) is as far from 0 as observed when the
    days are relabelled at random: exact over every split up to
    REGISTRY_PERM_EXACT of them (7 days a side: 3,432), else seeded samples. No
    normality assumption, which 7 daily P&L values a side cannot support."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    pool = [float(x) for x in a] + [float(x) for x in b]
    tot = sum(pool)
    obs = abs(sum(b) / nb - sum(a) / na)
    if obs <= 1e-9:
        return 1.0
    tol = obs * 1e-9 + 1e-12

    def far(sb: float) -> bool:
        return abs(sb / nb - (tot - sb) / na) >= obs - tol

    if math.comb(na + nb, nb) <= REGISTRY_PERM_EXACT:
        hits = n = 0
        for comb in itertools.combinations(pool, nb):
            n += 1
            hits += far(sum(comb))
        return round(hits / n, 5)
    rng = random.Random(7)
    hits = sum(far(sum(rng.sample(pool, nb))) for _ in range(REGISTRY_PERM_SAMPLES))
    return round((hits + 1) / (REGISTRY_PERM_SAMPLES + 1), 5)


def bh_qvalues(ps: List[Optional[float]]) -> List[Optional[float]]:
    """Benjamini-Hochberg adjusted p (q) for each p; None stays None. A call
    survives at q <= REGISTRY_FDR_Q: across a batch, at most ~10% of the calls
    that survive are expected to be noise."""
    idx = [i for i, p in enumerate(ps) if p is not None]
    m = len(idx)
    out: List[Optional[float]] = [None] * len(ps)
    running = 1.0
    for rank, i in reversed(list(enumerate(sorted(idx, key=lambda i: ps[i]), 1))):
        running = min(running, ps[i] * m / rank)
        out[i] = round(min(1.0, running), 5)
    return out


def mde(a: List[float], b: List[float]) -> Optional[float]:
    """The smallest move in the mean this test sees 80% of the time at p 0.05
    (2.8 standard errors, sample variances; one side may be all zeros)."""
    if len(a) < 2 or len(b) < 2:
        return None
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return round(REGISTRY_MDE_Z * se, 2)


def change_call(r: dict) -> str:
    """The provisional call on a readout (before/after, not causal). better /
    worse need the move to be big, p <= REGISTRY_P, and to survive the batch's
    false-discovery control (fdr_ok, set by sec_registry; a lone readout has it)."""
    if r.get("book"):
        return "book-wide"
    if not r.get("before_days") or not r.get("after_days"):
        return "no data"
    b, d = r.get("before") or {}, r.get("delta") or {}

    def active(x: dict) -> bool:
        return abs(_f(x.get("net"))) >= 1 or _f(x.get("fills")) >= 1 or _f(x.get("rest")) >= 100

    if not active(b) and not active(r.get("after") or {}):
        return "no activity"
    m = r.get("metric") or "net"
    dv, p = d.get(m), r.get("p")
    new = not active(b)
    if dv is not None:
        floor = REGISTRY_MIN_USD if m in _MONEY else 1.0
        big = abs(dv) >= max(floor, REGISTRY_MIN_FRAC * abs(_f(b.get(m))))
        if big and p is not None and p <= REGISTRY_P and r.get("fdr_ok", True):
            word = ("better" if dv > 0 else "worse") if m in _MONEY else ("up" if dv > 0 else "down")
            return ("new, " + word) if new else word
    return "new, no clear change" if new else "no clear change"


def change_readout(sc: Optional[dict], split: date, horizon: int, day: date, metric: str = "net",
                   universe: Optional[set] = None, wave_events: Optional[dict] = None) -> dict:
    """The scope's ET days split+1..split+horizon (to `day`) against
    split-horizon..split-1. Rewards MODELLED, trading MEASURED at the last trade."""
    sc = sc or {"book": True}
    before = [(split - timedelta(days=i)).isoformat() for i in range(horizon, 0, -1)]
    after = [(split + timedelta(days=i)).isoformat() for i in range(1, horizon + 1)
             if split + timedelta(days=i) <= day]
    if universe is None:
        universe = {s for iso in before + after for s, _e in (_day_agg(iso) or {})}
    match = scope_matcher(sc, universe, wave_events)
    rb, hb = _scope_rows(match, before)
    ra, ha = _scope_rows(match, after)
    hits = hb | ha
    sup = _supply_days([r["day"] for r in rb + ra])
    for r in rb + ra:
        d = sup.get(r["day"]) or {}
        r["supply"] = sum(x for s, x in d.items() if s in hits or match(s, "")) if d else None
    keys = ("net", "rew", "pnl", "fills", "cts", "rest", "markets")
    out: dict = {"split": split.isoformat(), "horizon": horizon, "metric": metric,
                 "scope": scope_label(sc), "scope_key": scope_key(sc), "book": bool(sc.get("book")),
                 "before_days": len(rb), "after_days": len(ra), "complete": len(ra) >= horizon,
                 "series_hit": len(hits), "series_sample": sorted(hits)[:8],
                 "before": {k: _mean([r[k] for r in rb]) for k in keys},
                 "after": {k: _mean([r[k] for r in ra]) for k in keys}}
    out["delta"] = {k: (round(out["after"][k] - out["before"][k], 2) if rb and ra else None) for k in keys}
    out["t"] = {k: _welch_t([r[k] for r in rb], [r[k] for r in ra]) for k in keys if k != "markets"}
    m = metric if metric in keys else "net"
    out["p"] = perm_p([r[m] for r in rb], [r[m] for r in ra])
    out["mde"] = mde([r[m] for r in rb], [r[m] for r in ra])
    out["net_sd"] = {"before": round(statistics.pstdev([r["net"] for r in rb]), 2) if len(rb) > 1 else None,
                     "after": round(statistics.pstdev([r["net"] for r in ra]), 2) if len(ra) > 1 else None}
    ob = [r["book_net"] - r["net"] for r in rb]
    oa = [r["book_net"] - r["net"] for r in ra]
    out["outside"] = {"before": _mean(ob), "after": _mean(oa),
                      "delta": round(_mean(oa) - _mean(ob), 2) if ob and oa else None, "t": _welch_t(ob, oa)}
    sb = [r["supply"] for r in rb if r.get("supply") is not None]
    sa = [r["supply"] for r in ra if r.get("supply") is not None]
    out["supply_ratio"] = round(_mean(sa) / _mean(sb), 2) if sb and sa and _f(_mean(sb)) > 0 else None

    def per1k(rows):
        rest = sum(r["rest"] for r in rows)
        return round(sum(r["net"] for r in rows) / rest * 1000, 2) if rest > 0 else None

    out["net_per_1k"] = {"before": per1k(rb), "after": per1k(ra)}
    out["daily"] = {"before": [[r["day"][5:], round(r["net"], 2)] for r in rb],
                    "after": [[r["day"][5:], round(r["net"], 2)] for r in ra]}
    out["call"] = change_call(out)
    return out


def _split_day(it: dict) -> Optional[date]:
    return datetime.fromtimestamp(it["live_at"], ET).date() if it.get("live_at") else None


def _horizon(it: dict) -> int:
    return int(_f(it.get("horizon")) or REGISTRY_DAYS)


def registry_groups(st: Dict[str, dict], keys: Iterable[str]) -> Dict[str, dict]:
    """Changes that went live the same ET day with the same scope, horizon and
    metric share one readout: a day archive cannot tell them apart."""
    groups: Dict[str, dict] = {}
    for k in keys:
        it = st[k]
        sp, h, m = _split_day(it), _horizon(it), it.get("metric") or "net"
        sc = it.get("scope") or {"book": True}
        gk = f"{sp.isoformat()}|{h}|{m}|{scope_key(sc)}"
        g = groups.setdefault(gk, {"group": gk, "split": sp, "horizon": h, "metric": m, "scope": sc, "keys": []})
        g["keys"].append(k)
    return groups


def _registry_due(it: dict, day: date) -> bool:
    """Past its horizon with no readout for it -- or one taken on this same day
    (a re-collect recomputes), or under a scope or metric changed since."""
    sp = _split_day(it)
    if sp is None or it.get("skipped"):
        return False
    h = _horizon(it)
    if day < sp + timedelta(days=h):
        return False
    rd = (it.get("readouts") or {}).get(str(h))
    if rd is None or rd.get("day") == day.isoformat():
        return True
    res = rd.get("result") or {}
    return (res.get("scope_key") != scope_key(it.get("scope"))
            or (res.get("metric") or "net") != (it.get("metric") or "net"))


def registry_sync(day: date, now: Optional[float] = None) -> dict:
    """Register new IMM changes and resolve when pending ones went live."""
    now = now or time.time()
    st = registry_state()
    first = not st
    since = now - (REGISTRY_BACKFILL_DAYS if first else REGISTRY_LOOKBACK_DAYS) * 86400
    commits = registry_commits(since)
    have = {it.get("full") for it in st.values() if it.get("full")}
    reg = []
    for c in commits:
        if c["full"] in have or _NOT_A_CHANGE.match(c["subject"]):
            continue
        c["kind"] = change_kind(c["files"])
        if c["kind"]:
            reg.append(c)
    aliases = _family_aliases(_recent_series(day)) if reg else {}
    used = set(st)
    new = []
    for c in sorted(reg, key=lambda c: c["ct"]):
        key = c["full"][:7] if c["full"][:7] not in used else c["full"][:10]
        used.add(key)
        registry_append({"op": "register", "key": key, "full": c["full"], "kind": c["kind"], "ct": c["ct"],
                         "subject": c["subject"][:200], "body": c["body"][:600], "files": c["files"][:40],
                         "ids": subject_ids(c["subject"]), "scope": infer_scope(c["subject"], c["body"], aliases),
                         "registered": day.isoformat()})
        new.append(key)
    st = registry_state()
    runs = bot_runs(since)
    pending = [it for it in st.values() if it.get("full") and not it.get("live_at") and not it.get("skipped")]
    resolved = 0
    if pending:
        order, parents = registry_dag(since)
        live = resolve_live(order, parents, registry_reflog(), runs,
                            [{"full": it["full"], "kind": it.get("kind"), "ct": it.get("ct")}
                             for it in st.values() if it.get("full")])
        for it in pending:
            got = live.get(it["full"])
            if got:
                registry_append({"op": "live", "key": it["key"], "live_at": round(got[0], 1), "basis": got[1]})
                resolved += 1
    n_env = 0
    st = registry_state()
    # a launcher commit that went live in the same run is the change; its env
    # lines are recorded, not measured twice
    launch_at = {round(_f(it.get("live_at")), 1): k for k, it in st.items()
                 if it.get("kind") == "launcher" and it.get("live_at")}
    for e in env_changes(runs):
        if e["key"] in st:
            continue
        sc = ({"series": e["tokens"], "basis": "series in the env value"} if e["tokens"]
              else {"book": True, "basis": "an env knob"})
        registry_append({"op": "register", "key": e["key"], "kind": "env", "ct": int(e["ts"]),
                         "subject": f"env {e['name']}: {e['change']}"[:200], "files": [], "ids": [],
                         "scope": sc, "registered": day.isoformat(), "run_id": e["run_id"]})
        registry_append({"op": "live", "key": e["key"], "live_at": round(e["ts"], 1),
                         "basis": f"bot run {e['run_id']} started with it {et_str(e['ts'])} ET"})
        lk = launch_at.get(round(e["ts"], 1))
        if lk:
            registry_append({"op": "skip", "key": e["key"], "by": "registry",
                             "note": f"measured with launcher commit {lk} (the same bot run)"})
        new.append(e["key"])
        n_env += 1
    return {"first": first, "registered": new, "env": n_env, "resolved": resolved,
            "commits_read": len(commits), "runs_read": len(runs), "since": et_str(since, "%Y-%m-%d")}


def _recent_series(day: date, days: int = 30) -> List[str]:
    out: set = set()
    for i in range(days):
        out.update(s for s, _e in (_day_agg((day - timedelta(days=i)).isoformat()) or {}))
    return sorted(out)


_CALLED = ("better", "worse", "up", "down")


def registry_clusters(readouts: List[dict]) -> None:
    """Readouts that measure the same money -- live the same ET day on
    overlapping series, so their windows are the same days -- form one cluster
    that counts once (the 11/26 headless run: the two weekend-rain readouts were
    the same -$63/day, one TRUMPSAY launch counted as +$24 and +$20). The lead is
    the strongest called member (lowest q), else the widest scope: a narrow
    change that moved stays visible inside a wider one that didn't. Sets
    `cluster` (the lead's group) on each, `same_money_as` (the lead's keys) on
    the others. Whole-book readouts overlap everything and get no call: never
    merged. Different days are never merged (no chains across the week)."""
    idx = [i for i, r in enumerate(readouts) if r.get("_fp")]
    parent = {i: i for i in idx}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a_, i in enumerate(idx):
        for j in idx[a_ + 1:]:
            ri, rj = readouts[i], readouts[j]
            if ri["split"] == rj["split"] and ri["_fp"] & rj["_fp"]:
                parent[find(i)] = find(j)
    members: Dict[int, List[int]] = defaultdict(list)
    for i in idx:
        members[find(i)].append(i)
    for ms in members.values():
        called = [i for i in ms if str(readouts[i].get("call") or "").endswith(_CALLED)]
        if called:
            lead = min(called, key=lambda i: (_f(readouts[i].get("q"), 1.0),
                                              -abs(_f((readouts[i].get("delta") or {}).get(
                                                  readouts[i].get("metric") or "net")))))
        else:
            lead = max(ms, key=lambda i: (len(readouts[i]["_fp"]), -_f(readouts[i].get("p"), 1.0)))
        for i in ms:
            readouts[i]["cluster"] = readouts[lead]["group"]
            if i != lead:
                readouts[i]["same_money_as"] = readouts[lead]["keys"]


def registry_board(st: Dict[str, dict]) -> dict:
    """The registry's running score: one vote per readout group, and in the
    calls and the dollar sums one per cluster (overlapping readouts of the
    same money count once, at the widest scope)."""
    kinds = Counter(it.get("kind") for it in st.values())
    seen: set = set()
    calls: Counter = Counter()
    better = worse = 0.0
    merged = 0
    for it in st.values():
        for h, rd in (it.get("readouts") or {}).items():
            gk = (rd.get("group"), h)
            if gk in seen:
                continue
            seen.add(gk)
            res = rd.get("result") or {}
            if res.get("same_money_as"):
                merged += 1
                continue
            c = res.get("call") or "?"
            calls[c] += 1
            dv = _f((res.get("delta") or {}).get(res.get("metric") or "net"))
            if c.endswith("better"):
                better += dv
            elif c.endswith("worse"):
                worse += dv
    return {"changes": len(st), "kinds": dict(kinds),
            "live": sum(1 for it in st.values() if it.get("live_at")),
            "not_live": sum(1 for it in st.values() if not it.get("live_at") and not it.get("skipped")),
            "skipped": sum(1 for it in st.values() if it.get("skipped")),
            "read_out": len(seen), "merged": merged, "calls": dict(calls),
            "verdicts": dict(Counter(it["verdict"] for it in st.values() if it.get("verdict"))),
            "better_usd_day": round(better, 2), "worse_usd_day": round(worse, 2)}


def sec_registry(day: date, now: Optional[float] = None) -> dict:
    iso = day.isoformat()
    s, e = et_bounds(day)
    now = now or time.time()
    sync = registry_sync(day, now)
    st = registry_state()
    live = {k: it for k, it in st.items() if it.get("live_at") and not it.get("skipped")}
    due, window = [], []
    for k, it in live.items():
        sp = _split_day(it)
        if sp is None or sp > day:
            continue
        if _registry_due(it, day):
            due.append(k)
        elif sp < day < sp + timedelta(days=_horizon(it)):
            window.append(k)
    lo = day - timedelta(days=2 * REGISTRY_EXTEND_DAYS + 2)
    need = {(lo + timedelta(days=i)).isoformat() for i in range((day - lo).days + 1)}
    for k in due + window:
        sp, h = _split_day(live[k]), _horizon(live[k])
        need |= {(sp + timedelta(days=i)).isoformat() for i in range(-h, h + 1) if sp + timedelta(days=i) <= day}
    _supply_days(need)                  # one pass over program_history for every readout
    uk: set = set()
    for d in sorted(need):
        uk |= set(_day_agg(d) or {})
    universe = {x for x, _e in uk}
    we = pvd_wave_events() if any((it.get("scope") or {}).get("waves") for it in live.values()) else {}
    fps: Dict[str, Optional[frozenset]] = {}

    def footprint(sc: dict) -> Optional[frozenset]:
        sk = scope_key(sc)
        if sk not in fps:
            if sk == "book":
                fps[sk] = None
            else:
                m = scope_matcher(sc, universe, we)
                fps[sk] = frozenset(x for x, ev in uk if m(x, ev))
        return fps[sk]

    # every change that can overlap a window read today, back to the oldest
    # due readout's before window (the first run reads out 30 days at once)
    near_lo = min([lo] + [_split_day(live[k]) - timedelta(days=_horizon(live[k])) for k in due])
    near = [k for k, it in live.items() if near_lo <= _split_day(it) <= day]

    def confounds(g: dict) -> Tuple[List[dict], int]:
        sp, h = g["split"], g["horizon"]
        mine = footprint(g["scope"])
        out, n_book = [], 0
        for k in near:
            if k in g["keys"]:
                continue
            it = live[k]
            sk = _split_day(it)
            if not (sp - timedelta(days=h) <= sk <= sp + timedelta(days=h)):
                continue
            other = footprint(it.get("scope") or {})
            if other is None or mine is None:
                n_book += 1          # a whole-book change on either side: counted, not listed
                continue
            if mine & other:
                out.append({"key": k, "split": sk.isoformat(), "subject": (it.get("subject") or "")[:90],
                            "side": "before" if sk < sp else ("same day" if sk == sp else "after")})
        out.sort(key=lambda x: x["split"])
        return out[:12], n_book

    readouts = []
    for gk, g in registry_groups(st, due).items():
        r = change_readout(g["scope"], g["split"], g["horizon"], day, g["metric"], universe, we)
        cf, nb = confounds(g)
        later = [c for c in cf if c["side"] == "after"]
        exp_days = [st[k].get("expect_day") for k in g["keys"] if st[k].get("expect")]
        r["_fp"] = footprint(g["scope"])
        r.update(keys=g["keys"], group=gk, confounds=cf, book_changes_in_windows=nb,
                 clean_days=((date.fromisoformat(later[0]["split"]) - g["split"]).days - 1) if later else g["horizon"],
                 ids=sorted({i for k in g["keys"] for i in st[k].get("ids") or []}),
                 kinds=sorted({st[k].get("kind") or "?" for k in g["keys"]}),
                 expect="; ".join(dict.fromkeys(st[k]["expect"] for k in g["keys"] if st[k].get("expect"))),
                 # pre-registered: the expectation was written before this readout's day
                 expect_pre=bool(exp_days) and all(d_ and d_ < iso for d_ in exp_days),
                 scope_by=sorted({st[k].get("scope_by") or "auto" for k in g["keys"]}),
                 scope_basis="; ".join(sorted({str((st[k].get("scope") or {}).get("basis") or "")
                                               for k in g["keys"]} - {""})),
                 live=et_str(min(st[k]["live_at"] for k in g["keys"])))
        readouts.append(r)
    # false-discovery control across everything read out together: a call must
    # survive Benjamini-Hochberg at q 0.10 (11/25's back-fill: 164 testable
    # readouts, 23 called at |t| >= 2, where chance alone gives ~8 at p < 0.05)
    qs = bh_qvalues([r.get("p") if r["call"] not in ("book-wide", "no data", "no activity") else None
                     for r in readouts])
    for r, q in zip(readouts, qs):
        r["q"], r["fdr_ok"], r["fdr_m"] = q, q is not None and q <= REGISTRY_FDR_Q, sum(x is not None for x in qs)
        r["call"] = change_call(r)
    registry_clusters(readouts)
    for r in readouts:
        r.pop("_fp", None)
        registry_append({"op": "readout", "keys": r["keys"], "group": r["group"], "horizon": r["horizon"],
                         "day": iso, "result": r})
        r["subjects"] = [st[k].get("subject") or "" for k in r["keys"]][:6]
    rank = {"worse": 0, "new, worse": 0, "better": 1, "new, better": 1, "down": 2, "up": 2}
    # called first (worse, then better); then the most suggestive by p, a big
    # delta at p 0.9 being noise; whole-book last
    readouts.sort(key=lambda r: (rank.get(r["call"], 3 if r["call"] != "book-wide" else 4),
                                 r["p"] if r.get("p") is not None else 2.0,
                                 -abs(_f((r.get("delta") or {}).get(r["metric"])))))
    warns = []
    for gk, g in registry_groups(st, window).items():
        if g["scope"].get("book") or g["metric"] not in _MONEY or (day - g["split"]).days < 2:
            continue
        r = change_readout(g["scope"], g["split"], g["horizon"], day, g["metric"], universe, we)
        dv, t = _f(r["delta"].get(g["metric"])), r["t"].get(g["metric"])
        if r["after_days"] >= 2 and t is not None and t <= -REGISTRY_EARLY_T and \
                dv <= -max(REGISTRY_EARLY_USD, 0.25 * abs(_f(r["before"].get(g["metric"])))):
            warns.append({"keys": g["keys"], "subject": st[g["keys"][0]].get("subject"), "split": g["split"].isoformat(),
                          "scope": r["scope"], "metric": g["metric"], "after_days": r["after_days"],
                          "before": r["before"].get(g["metric"]), "after": r["after"].get(g["metric"]),
                          "delta": dv, "t": t, "supply_ratio": r.get("supply_ratio"),
                          "outside_delta": (r.get("outside") or {}).get("delta")})
    warns.sort(key=lambda w: w["delta"])
    went = sorted((it for it in st.values() if it.get("live_at") and s <= it["live_at"] < e),
                  key=lambda it: it["live_at"])
    went_rows = [{"key": it["key"], "at": et_str(it["live_at"]), "kind": it.get("kind"),
                  "scope": scope_label(it.get("scope")), "scope_by": it.get("scope_by"),
                  "basis": (it.get("scope") or {}).get("basis"), "subject": it.get("subject"),
                  "ids": it.get("ids") or [], "skipped": it.get("skipped"), "live_basis": it.get("live_basis")}
                 for it in went]
    cutoff = now - 7 * 86400
    pend = sorted((it for it in st.values() if it.get("full") and not it.get("live_at") and not it.get("skipped")),
                  key=lambda it: -_f(it.get("ct")))
    nxt: Counter = Counter()
    for k in window:
        nxt[(_split_day(live[k]) + timedelta(days=_horizon(live[k]))).isoformat()] += 1
    return {"sync": sync, "board": registry_board(registry_state()),
            "went_live": went_rows[:40], "n_went_live": len(went_rows),
            "readouts": readouts[:REGISTRY_SHOW], "n_readouts": len(readouts),
            # one call per cluster: a readout of the same money as another is listed, not counted
            "readout_calls": dict(Counter(r["call"] for r in readouts if not r.get("same_money_as"))),
            "n_merged": sum(1 for r in readouts if r.get("same_money_as")),
            "warnings": warns[:10], "in_window": len(window),
            "next_due": sorted(nxt.items())[:3],
            "not_live": [{"key": it["key"], "at": et_str(_f(it.get("ct"))), "kind": it.get("kind"),
                          "subject": it.get("subject")} for it in pend if _f(it.get("ct")) >= cutoff][:15],
            "not_live_old": sum(1 for it in pend if _f(it.get("ct")) < cutoff)}


def registry_ingest(day: date, items: list, first_time=None) -> dict:
    """findings.json "changes": the review scopes what went live, skips what
    cannot move P&L, and judges readouts."""
    st = registry_state()
    iso = day.isoformat()
    res: Dict[str, list] = {"scoped": [], "judged": [], "skipped": [], "rejected": []}
    for c in items[:100] if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        raw = c.get("keys") or c.get("shas") or c.get("key") or c.get("sha")
        raw = raw if isinstance(raw, list) else ([raw] if raw else [])
        keys = [resolve_key(st, x) for x in raw]
        if not keys or None in keys:
            res["rejected"].append({"keys": [str(x) for x in raw[:6]],
                                    "why": "unknown change key (use the Key column of evidence section 12)"})
            continue
        if first_time is not None and not first_time("changes", c):
            continue
        note = str(c.get("note") or "")[:400]
        if c.get("skip"):
            why = c["skip"] if isinstance(c["skip"], str) else (note or "skipped")
            for k in keys:
                registry_append({"op": "skip", "key": k, "note": str(why)[:300], "by": "review", "day": iso})
            res["skipped"] += keys
            continue
        sc = None
        if c.get("scope") is not None:
            sc, why = clean_scope(c["scope"])
            if sc is None:
                res["rejected"].append({"keys": keys, "why": why})
                continue
        met = c.get("metric")
        if met is not None and met not in CHANGE_METRICS:
            res["rejected"].append({"keys": keys, "why": "metric must be one of " + ", ".join(CHANGE_METRICS)})
            continue
        v = c.get("verdict")
        if v is not None and v not in CHANGE_VERDICTS:
            res["rejected"].append({"keys": keys, "why": "verdict must be one of " + ", ".join(CHANGE_VERDICTS)})
            continue
        if sc is not None or met or c.get("expect"):
            for k in keys:
                registry_append({"op": "scope", "key": k, "scope": sc or st[k].get("scope"), "metric": met,
                                 "expect": str(c.get("expect") or "")[:300] or None, "by": "review", "day": iso})
            res["scoped"] += keys
        if v is not None:
            for k in keys:
                registry_append({"op": "verdict", "key": k, "verdict": v, "note": note,
                                 "finding": c.get("finding"), "by": "review", "day": iso})
            res["judged"] += keys
    return res


def registry_add(sha: str, day: date, note: str = "") -> Optional[str]:
    """Register a commit the rules missed (an imm_dashboard family rule, say)."""
    code, out = _git("log", "-1", "--format=%x1e%H%x1f%ct%x1f%s%x1f%b%x1d", "--name-only", sha)
    if code != 0:
        return None
    st = registry_state()
    for chunk in out.split("\x1e"):
        head, _, files = chunk.partition("\x1d")
        parts = head.split("\x1f")
        if len(parts) < 3 or not re.fullmatch(r"[0-9a-f]{40}", parts[0].strip()):
            continue
        full = parts[0].strip()
        if any(it.get("full") == full for it in st.values()):
            return next(k for k, it in st.items() if it.get("full") == full)
        names = [l.strip() for l in files.splitlines() if l.strip()]
        key = full[:7] if full[:7] not in st else full[:10]
        registry_append({"op": "register", "key": key, "full": full, "kind": change_kind(names) or "manual",
                         "ct": int(_f(parts[1])), "subject": parts[2].strip()[:200],
                         "body": (parts[3] if len(parts) > 3 else "").strip()[:600], "files": names[:40],
                         "ids": subject_ids(parts[2]), "registered": day.isoformat(), "note": note[:300],
                         "scope": infer_scope(parts[2], parts[3] if len(parts) > 3 else "",
                                              _family_aliases(_recent_series(day)))})
        return key
    return None


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
            # every key present on an idle day too: summ() reads fills / rest / mt
            # directly (the 11/26 headless run's finding: measure died on an idle-day scope)
            a = defaultdict(float, {k: 0.0 for k in SUM_KEYS})
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
    # every IMM change, measured at 7 days (Jack 2026-11-26); IMM_REVIEW_REGISTRY=0 turns it off
    if want("registry") and os.environ.get("IMM_REVIEW_REGISTRY", "1") != "0":
        ev["registry"] = _section("registry", sec_registry, day)
    # every fair model scored against the market (imm_fair_scorecard.py, Jack
    # 2026-11-26); IMM_REVIEW_MODELS=0 turns it off
    if want("models") and os.environ.get("IMM_REVIEW_MODELS", "1") != "0":
        ev["models"] = _section("models", sec_models, day)
    # where the next dollar of size goes, from the family economics (moat, written
    # above) and the registry's measured margin (imm_allocation.py, Jack
    # 2026-11-26); IMM_REVIEW_ALLOCATION=0 turns it off
    if want("allocation") and os.environ.get("IMM_REVIEW_ALLOCATION", "1") != "0":
        ev["allocation"] = _section("allocation", sec_allocation, day)
    if want("self"):          # last: it reads the other sections' times and the ledger
        ev["self"] = _section("self", sec_self, day, ev)
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
          f"(cap {u.get('event_cap')}); {u.get('unquoted_rows')} unquoted rows; {u.get('books_read')} books read "
          f"({u.get('books_batched') or 0} in {u.get('book_calls') or 0} batch calls"
          f"{'; batch error: ' + str(u.get('batch_error')) if u.get('batch_error') else ''}); "
          f"the gap scan left {usd0(u.get('unest_pool'), False)}/day unread (its own count, most of it estimated "
          f"elsewhere: see the classes below); estimated missed total {usd(u.get('est_missed_total'), False)}/day.")
    um = cf.get("unmeasured") or {}
    if um.get("classes"):
        a(f"\nUnmeasured pool by class (R-261124-21): of {usd0(um.get('gap_scan_unest_pool'), False)}/day the gap scan "
          f"left unread, {usd0(um.get('unmeasured_pool'), False)}/day is read by nothing, "
          f"{usd0(um.get('unmeasured_not_blocked'), False)}/day of it NOT blocked on purpose. {um.get('basis')}.")
        a("| Class | Events | Pool $/day | Estimate $/day | Estimated by |")
        a("|---|---:|---:|---:|---|")
        for k, v in um["classes"].items():
            if "est_x1" in v:
                est = f"x1 {usd(v['est_x1'], False)}, x3 {usd(v['est_x3'], False)}"
            elif "est" in v:
                est = f"{usd(v['est'], False)} ({usd(v['est_under_1_per_mkt'], False)} from mkts under $1/day)"
            else:
                est = "none"
            a(f"| {k} | {v['events']} | {usd0(v['pool'], False)} | {est} | {v['by']} |")
    wd = cf.get("whole_day") or {}
    if wd.get("scans"):
        eb = wd.get("ended_before_review") or {}
        a(f"\nTHE WHOLE ET DAY from every hourly gap scan (R-261124-24): {wd['scans']} scans "
          f"{et_str(_ts_of(wd.get('first')))} to {et_str(_ts_of(wd.get('last')))} ET, {wd.get('covered_hours')}h covered. "
          f"{eb.get('events')} estimated events ENDED BEFORE THIS REVIEW (not in the morning scan): "
          f"{usd(eb.get('est_days'), False)} modelled over the day, net {usd(eb.get('est_net'), False)}. "
          f"Pool nothing read: {usd0(wd.get('unread_pool_days'), False)} pool-days. {wd.get('basis')}.")
        a("| Reason class | Events | Pool-days | Modelled $ over the day | Adj $ | Unread pool-days |")
        a("|---|---:|---:|---:|---:|---:|")
        for k, v in (wd.get("by_class") or {}).items():
            a(f"| {k} | {v['events']} | {usd0(v['pool_days'], False)} | {usd(v['est_days'], False)} | "
              f"{usd(v['est_adj'], False)} | {usd0(v['unread_pool_days'], False)} |")
        if wd.get("top"):
            a("Top misses over the whole day, ranked by NET:")
            for x in wd["top"][:12]:
                a(f"- {x['event']} [{x['class']}: {x['reason']}] {x['hours']}h unquoted, {usd(x['est_days'], False)} "
                  f"modelled over the day -> net {usd(x['est_net'], False)} ({x.get('family')}, tax {x.get('tax')})"
                  f"{'' if x.get('in_morning_scan') else ' -- ENDED BEFORE THE REVIEW'}")
    elif wd.get("note") or wd.get("error"):
        a(f"\nWhole-day gap scans: {wd.get('note') or 'ERROR ' + str(wd.get('error'))}")
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
        a("\nBiggest pools the gap scan left unread (with who measures them instead):")
        for x in cf["top_unestimated_pool"]:
            a(f"- {x['event']} [{x['class']}: {x['reason']}; {x.get('measured_by') or '?'}] pool "
              f"{usd0(x['pool'], False)}/day, {x['n']} mkts - {x['title']}")
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
        sp = fm.get("split") or {}
        if sp:
            q, dr = sp.get("quoted_to_end") or {}, sp.get("dropped") or {}
            wf = fm.get("wave_forced") or {}
            c7 = fm.get("cost_7d") or {}
            cq, cd = c7.get("quoted_to_end") or {}, c7.get("dropped") or {}
            a(f"Floor misses by cause (R-261124-23): {q.get('n')} QUOTED TO THE END ({usd(q.get('modelled'), False)}; "
              f"{q.get('by_series')}) vs {dr.get('n')} DROPPED by the floor logic, accrual left over "
              f"({usd(dr.get('modelled'), False)}; {dr.get('by_series')}). Wave-forced (a wave list held the event "
              f"through the period end): {wf.get('n')} ({usd(wf.get('modelled'), False)}), by wave {wf.get('by_wave')}.")
            a(f"Floor-miss COST, 7 ET days to the day: quoted-to-the-end markets trading {usd(cq.get('pnl'))} on "
              f"{cq.get('fills')} fills, ~{usd0(cq.get('rest_avg'), False)} resting per market-day; dropped markets "
              f"trading {usd(cd.get('pnl'))} on {cd.get('fills')} fills. Size each quoted miss needed to reach $1: "
              f"{fm.get('size_needed')}. ({fm.get('cost_basis')})")
        elif fm.get("cost_error"):
            a(f"Floor-miss cost: ERROR {fm['cost_error']}")
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
        bk = mo.get("backup") or {}
        if bk:
            tot = bk.get("totals") or {}
            ad = bk.get("added") or {}
            a(f"**Backup** (BigQuery {bk.get('dataset') or 'imm_review'}, imm_review_backup.py, R-261124-20): "
              f"{bk.get('state')}" + (f" at {bk['at']} ET ({bk.get('age_h')}h ago)" if bk.get("at") else "")
              + (f"; added {sum((ad.get('lines') or {}).values())} log lines, {ad.get('files')} file versions, "
                 f"{ad.get('days_changed')} changed day files" if ad else "")
              + (f"; the copy holds {tot.get('files')} file versions, {tot.get('day_archive')} days, "
                 f"{tot.get('ledger_lines')} ledger and {tot.get('changes_lines')} registry lines" if tot else "")
              + ".")
            for x in (bk.get("errors") or []) + (bk.get("refused") or []):
                a(f"- backup problem: {x}")
            if bk.get("state") in ("FAILED", "stale"):
                a("- A failed or late backup leaves the moat on one unbacked disk: raise it as P1 (kind ops).")
    render_self(ev.get("self") or {}, a)
    render_registry(ev.get("registry") or {}, a)
    render_models(ev.get("models") or {}, a)
    render_allocation(ev.get("allocation") or {}, a)
    a("")
    return "\n".join(L)


def sec_models(day: date) -> dict:
    import imm_fair_scorecard  # here, not at the top: it imports this module
    return imm_fair_scorecard.sec_models(day)


def sec_allocation(day: date) -> dict:
    import imm_allocation  # here, not at the top: it imports this module
    return imm_allocation.sec_allocation(day)


def _q_str(q: Optional[dict]) -> str:
    """A bootstrap's p50 [p10, p90] in $/day."""
    if not q:
        return "n/a"
    return f"{usd(q.get('p50'))} [{usd(q.get('p10'))}, {usd(q.get('p90'))}]"


def render_allocation(al: dict, a) -> None:
    a("\n## 14. Capital allocation (where the next dollar of size goes)")
    if not al:
        a("- not collected (IMM_REVIEW_ALLOCATION=0 turns it off)")
        return
    if al.get("error"):
        a(f"- ERROR: {al['error']}")
        return
    w, b, tr = al.get("window") or {}, al.get("book") or {}, al.get("track") or {}
    ru, stp = al.get("rules") or {}, al.get("steps") or {}
    a(f"FAMILY ECONOMICS: the {w.get('days')} ET days {w.get('from')} to {w.get('to')} (moat/family_daily.csv); "
      "rewards MODELLED (the dashboard's estimate), trading MEASURED at the last trade. Book: "
      f"{usd(b.get('rest'), False)} resting a day, net {usd(b.get('net'))} a day, "
      f"{_fmt(b.get('net_per_1k'))} net per $1k resting a day. An average overstates what the next dollar "
      "earns, so:")
    a(f"THE MARGIN, MEASURED: the change registry's readouts in which a scope's resting $ moved "
      f"{ru.get('move_min')}x or more with the programs' supply within {ru.get('supply_band')}; the "
      "proportional prediction from the 7 days before against the 7 days after (before/after, not causal).")
    for name, label in (("adds", "Adds"), ("cuts", "Cuts")):
        d = tr.get(name) or {}
        if not d.get("n"):
            a(f"- {label}: no readout yet")
            continue
        a(f"- {label}: {d['n']} readouts, resting {usd(d.get('d_rest'))} a day. Rewards predicted "
          f"{usd(d.get('pred_rew'))}, measured {usd(d.get('meas_rew'))} a day; trading predicted "
          f"{usd(d.get('pred_pnl'))}, measured {usd(d.get('meas_pnl'))}. Marginal net per $1k "
          f"{_fmt(d.get('marginal_per_1k'))} (their average before: {_fmt(d.get('average_per_1k'))}). "
          + (f"Calibration used: rewards x{_fmt(d.get('c_rew'))}, trading x{_fmt(d.get('c_pnl'))}."
             if d.get("calibrated") else "Too few to calibrate: the proportional model stands in."))
        for m in (d.get("moves") or [])[:3]:
            a(f"  - {m.get('split')} x{_fmt(m.get('k'))} ({_fmt(m.get('rest_b'), 0)} -> {_fmt(m.get('rest_a'), 0)} "
              f"resting): rewards {_fmt(m.get('rew_b'), 0)} -> {_fmt(m.get('rew_a'), 0)}, trading "
              f"{_fmt(m.get('pnl_b'), 0)} -> {_fmt(m.get('pnl_a'), 0)} a day; "
              + "; ".join(s for s in (m.get("subjects") or []) if s))
    a(f"THE MODEL (MODELLED): a step on one family, cut x{_fmt(stp.get('cut'))} or add x{_fmt(stp.get('add'))}: "
      "rewards by the bot's share model (rew x k / (k s + 1 - s), s our pool share), trading in proportion, each "
      "times its direction's calibration; days and readouts resampled together. Net $/day change, "
      "p50 [p10, p90]:")
    rows = al.get("families") or []
    if rows:
        a("| Family | Resting share | Net per $1k (p10 to p90) | t | Pool share | Trading c/ct | "
          "Cut: net $/day | Add: net $/day | Changed in 7 days |")
        a("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for r in rows:
            nq = r.get("net_per_1k_q") or {}
            rec = "; ".join(f"{c['day'][5:]} {c['key']}" for c in (r.get("recent") or [])[:2])
            a(f"| {r['family']} | {_fmt((r.get('rest_share') or 0) * 100, 1)}% | {_fmt(r.get('net_per_1k'))} "
              f"({_fmt(nq.get('p10'))} to {_fmt(nq.get('p90'))}) | {_fmt(r.get('t'))} | {_fmt(r.get('share'), 3)} | "
              f"{_fmt(r.get('trade_c'))} | {_q_str(r.get('cut'))} | {_q_str(r.get('add'))} | {rec} |")
    props = al.get("proposals") or []
    a(f"PROPOSALS (p10 gain above zero, p50 at least {usd(ru.get('min_gain'))}/day, {ru.get('min_days')}+ days of "
      f"data, no registered change in the last {ru.get('recent_days')} days):")
    if not props:
        a("- none clears the bar today")
    for p in props:
        a(f"- {p['kind'].upper()} {p['family']} x{_fmt(p['k'])} ({usd(p.get('capital'))} resting a day): "
          f"net {usd(p['p50'])}/day [{usd(p['p10'])}, {usd(p['p90'])}], P(gain) {_fmt(p.get('p_gain'), 2)}; "
          f"net per $1k now {_fmt(p.get('net_per_1k'))}, t {_fmt(p.get('t'))}.")
    ex = al.get("experiment")
    if ex:
        a(f"ADD EXPERIMENT (no add clears the bar; the best-measured family): {ex['family']} x{_fmt(ex['k'])} "
          f"({usd(ex.get('capital'))} resting a day). Net per $1k {_fmt(ex.get('net_per_1k'))} (p10 "
          f"{_fmt(ex.get('net_per_1k_p10'))}), t {_fmt(ex.get('t'))}. Modelled: {usd(ex.get('proportional'))}/day "
          f"if the next dollar earned the average, {usd(ex.get('calibrated'))}/day at the adds' measured "
          "calibration. Only as a pre-registered experiment with a stop rule.")
    for c in al.get("waits") or []:
        r0 = (c.get("recent") or [{}])[0]
        a(f"- WAITS: {c['kind']} {c['family']} would clear the bar ({usd(c['p50'])}/day), but "
          f"{r0.get('key')} ({r0.get('day')}: {r0.get('subject')}) changed it: read that change out first.")


def render_models(md: dict, a) -> None:
    a("\n## 13. Model scorecard (every fair model against the market)")
    if not md:
        a("- not collected (IMM_REVIEW_MODELS=0 turns it off)")
        return
    if md.get("error"):
        a(f"- ERROR: {md['error']}")
        return
    h = md.get("health") or {}
    a("imm_fair_marks.py logs every model's fair beside the market every 15 min (task KL imm fair-marks); "
      f"imm_fair_scorecard.py scores them. Snapshotter: {h.get('state')}"
      + (f", last run {h.get('last_run')} ET ({h.get('age_min')} min ago)" if h.get("last_run") else "")
      + f"; {h.get('markets')} markets with a fair, {h.get('books')} with a cycle-log book"
      + (f"; errors: {'; '.join(h['errors'])}" if h.get("errors") else "") + ".")
    if h.get("state") in ("stale", "never run"):
        a("- The snapshotter is not running: no model history accrues. Raise it as P1 (kind ops).")
    a(f"SETTLEMENT (the {md.get('window_days')} days to the day; MEASURED): Brier score of the model's fair "
      "and of the market's mid at the same moment before the close (lower is better); skill = Brier(market) "
      "- Brier(model), positive when the model knew more; the 90% interval resamples EVENTS. 'Disagree' = "
      "the markets where model and mid were 5c+ apart, where following the model is a trade. CONVERGENCE "
      "(the day): the mid's move against the model's gap at the start (slope; share toward the model). "
      "FILLS (the day): our fills scored by the model then, in cents from our side; 'against' = contracts "
      f"at -3c or worse. Scored today: {md.get('scored_today')} settled markets.")
    rows = md.get("families") or []
    if not rows:
        a("- no model data yet")
        return
    a("| Family | Markets | Settled (events) | Brier model / market | Skill (90% CI) | Disagree n: skill | "
      "Verdict | Converge n: slope, toward | Fills (cts): edge, against |")
    a("|---|---:|---:|---:|---:|---:|---|---:|---:|")
    for r in rows:
        ci = ("" if r.get("skill_lo") is None else f" ({_fmt(r['skill_lo'], 4)} to {_fmt(r['skill_hi'], 4)})")
        a(f"| {r['family']} | {r.get('markets') if r.get('markets') is not None else ''} | "
          f"{r['settled']} ({r['events']}) | {_fmt(r.get('brier_model'), 4)} / {_fmt(r.get('brier_market'), 4)} | "
          f"{_fmt(r.get('skill'), 4)}{ci} | {r['disagree_n']}: {_fmt(r.get('disagree_skill'), 4)} | {r['verdict']} | "
          f"{r['conv_n']}: {_fmt(r.get('conv_slope'), 2)}, {_fmt(r.get('conv_toward'), 2)} | "
          f"{r['fills']} ({_fmt(r.get('fill_cts'), 0)}): {_fmt(r.get('fill_edge_c'), 2)}c, "
          f"{_fmt(r.get('against_share'), 2)} |")
    bad = [r for r in rows if (r.get("against_share") or 0) >= 0.2 and r.get("fills", 0) >= 5]
    for r in bad:
        a(f"- {r['family']}: {r['against_share']:.0%} of today's filled contracts were 3c+ against its own model; "
          "worst: " + ", ".join(f"{w['ticker']} ({w['cents_x_contracts']:+.0f} c x ct)" for w in r.get("worst_fills") or []))


def render_self(se: dict, a) -> None:
    a("\n## 11. The review itself (how it compounds)")
    if not se:
        a("- not collected")
        return
    if se.get("error"):
        a(f"- ERROR: {se['error']}")
        return
    a(f'The question every run answers (Jack 11/26), in findings.json "compound": "{se.get("question")}"')
    rec = se.get("record") or {}
    st = rec.get("stats") or {}
    prec = st.get("precision")
    a(f"**Record:** {st.get('items')} findings; {st.get('approved')} approved, {st.get('rejected')} rejected; "
      f"{st.get('verified')} verified ({usd(st.get('verified_delta_usd_day'))}/day measured), {st.get('failed')} failed"
      f"{'' if prec is None else f', precision {prec:.0%}'}; {st.get('false_positives')} false positive(s) "
      f"{st.get('false_positive_ids') or ''}; {rec.get('proposed')} waiting on Jack"
      f"{'' if rec.get('oldest_proposed_days') is None else ', oldest ' + str(rec['oldest_proposed_days']) + 'd'}"
      f"{'' if rec.get('decision_lag_days') is None else '; median ' + str(rec['decision_lag_days']) + 'd to a decision'}.")
    if rec.get("approved_unapplied"):
        a("Approved but not applied: " + "; ".join(f"{x['id']} {x['title']} ({x['days']}d)" for x in rec["approved_unapplied"]))
    if rec.get("by_kind"):
        a("By kind: " + "; ".join(f"{k} " + ", ".join(f"{n} {s}" for s, n in v.items()) for k, v in rec["by_kind"].items()))
    b = se.get("budget") or {}
    if b:
        pct = b.get("collect_pct")
        a(f"**Budget:** this collect {b.get('collect_so_far_s')} s of the launcher's {b.get('collect_limit_s'):.0f} s"
          f"{'' if pct is None else f' ({pct:.0%})'}; slowest sections: "
          + ", ".join(f"{k} {v} s" for k, v in b.get("slowest") or []) + ".")
        for p in b.get("previous") or []:
            a(f"- run for {p['day']}: collect {p.get('collect_secs')} s, analysis {p.get('claude_min')} min "
              f"of {b.get('claude_limit_min'):.0f}, whole run {p.get('total_min')} min")
    runs = se.get("runs") or []
    if runs:
        a("**The last runs** (from their own tool traces):")
        a("| Day | Minutes | Turns | Reported cost | Bash | Errors | Denied | Writing outputs | Exit |")
        a("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in runs:
            cost = "n/a" if r.get("cost_usd") is None else f"${r['cost_usd']:.2f}"
            a(f"| {r['day']} | {_fmt(r.get('minutes'))} | {_fmt(r.get('turns'))} | {cost} | {r.get('bash')} | "
              f"{r.get('n_errors')} | {r.get('denials')} | {r.get('output_calls')} | {_fmt(r.get('exit'))} |")
        errs = runs[0].get("errors") or []
        if errs:
            a(f"Errors in the {runs[0]['day']} run:")
            for x in errs[:6]:
                a(f"- `{x['cmd']}` -> {x['error']}")
    rep = se.get("repeated") or []
    if rep:
        a("**Done by hand in 2+ runs** (each one is work the collector could do):")
        for x in rep:
            a(f"- {x['kind']}: {x['calls']} calls in {x['runs']} runs ({', '.join(x['days'])})")
    out = se.get("outside") or {}
    if out.get("commits"):
        a(f"**Landed on the day without a ledger id** ({len(out['commits'])}; did the review see any of it first?):")
        for c in out["commits"]:
            men = f" (mentions {', '.join(c['mentions'])})" if c.get("mentions") else ""
            a(f"- {c['at']} {c['sha']}{' [bot]' if c.get('bot') else ''}: {c['subject']}{men}")
    if out.get("memory"):
        a(f"**Memory notes other sessions wrote** ({len(out['memory'])}):")
        for n in out["memory"]:
            a(f"- {n['at']} {n['name']}: {n['description']}")
    if se.get("overlaps"):
        a("**Open items someone else acted on:**")
        for o in se["overlaps"]:
            a(f"- {o['id']} ({o['title']}) <- {o['by']} [{o['match']}]: {o['what']}")
    if se.get("rechecks"):
        a("**Promised rechecks due:** " + "; ".join(f"{r['id']} {r['title']} (due {r['due']})" for r in se["rechecks"]))
    bl = se.get("backlog") or []
    if bl:
        a(f"**Self-improvement backlog** ({len(bl)} open, kind review):")
        for x in bl:
            tag = " [compound]" if x.get("compound") else ""
            a(f"- {x['id']} [{x['status']}]{tag} {x['title']} ({x.get('age')}d, seen {x.get('seen')}d)")
    w = se.get("watches") or {}
    if w.get("off"):
        a("**Watches:** off (IMM_REVIEW_WATCHES=0).")
    elif w:
        a(f"**Watches** (the review's own read-only checks): {w.get('active')} active, {len(w.get('firing') or [])} "
          f"firing, {len(w.get('errors') or [])} errors, {w.get('retired')} retired.")
        for r in (w.get("firing") or []) + (w.get("errors") or []):
            bound = " ".join(x for x in (f"min {r['min']}" if r.get("min") is not None else "",
                                         f"max {r['max']}" if r.get("max") is not None else "") if x)
            a(f"- {r['state']} {r['id']} [{r.get('severity')}] {r.get('title')}: value {r.get('value')} ({bound})"
              f"{' ' + r['error'] if r.get('error') else ''}{' (' + r['note'] + ')' if r.get('note') else ''}"
              f"{'; why: ' + r['why'] if r.get('why') else ''}")
        if w.get("stale"):
            a(f"Never fired in {WATCH_STALE_DAYS}+ days (retire?): {', '.join(w['stale'])}")


def _cell(s, n: int = 100) -> str:
    return _one_line(str(s or "").replace("|", "/"), n)


def render_registry(rg: dict, a) -> None:
    a(f"\n## 12. Change registry (every IMM change, measured at {REGISTRY_DAYS} days)")
    if not rg:
        a("- not collected (IMM_REVIEW_REGISTRY=0 turns it off)")
        return
    if rg.get("error"):
        a(f"- ERROR: {rg['error']}")
        return
    a(f"Every commit that changes what the bot does, and every IMM_* env change a bot run started with, is "
      f"read out {REGISTRY_DAYS} full ET days after the day it went live against the {REGISTRY_DAYS} before, "
      "on the series it names (Jack 11/26). Before/after, not causal: rewards MODELLED, trading MEASURED "
      "at the last trade; 'outside' is the rest of the book over the same days; supply x is the program "
      "pool on the scope's series, after / before. p is an exact permutation p (every split of the days); "
      f"q is Benjamini-Hochberg across today's readouts, and a call needs p <= {REGISTRY_P} and q <= "
      f"{REGISTRY_FDR_Q}; 'can see' is the smallest move this test detects 80% of the time. A 'no clear "
      "change' whose expected move is under 'can see' is can't-tell, not no-effect.")
    b = rg.get("board") or {}
    sy = rg.get("sync") or {}
    kinds = ", ".join(f"{n} {k}" for k, n in sorted((b.get("kinds") or {}).items(), key=lambda x: -x[1]))
    calls = ", ".join(f"{n} {c}" for c, n in sorted((b.get("calls") or {}).items(), key=lambda x: -x[1])) or "none yet"
    verd = ", ".join(f"{n} {v}" for v, n in sorted((b.get("verdicts") or {}).items(), key=lambda x: -x[1])) or "none yet"
    a(f"**Registry:** {b.get('changes')} changes ({kinds}); {b.get('live')} live, {b.get('not_live')} not live, "
      f"{b.get('skipped')} skipped. {b.get('read_out')} readouts so far, {b.get('merged') or 0} of them the same "
      f"money as another (live within a day on overlapping series), counted once: {calls}. The review's "
      f"verdicts: {verd}. Calls of better add up to {usd(b.get('better_usd_day'))}/day, worse to "
      f"{usd(b.get('worse_usd_day'))}/day (before/after deltas, one per cluster).")
    if sy.get("first"):
        a(f"**First run:** registered {len(sy.get('registered') or [])} changes since {sy.get('since')}; every "
          f"one past its {REGISTRY_DAYS} days is read out today, so only the {REGISTRY_SHOW} biggest are listed.")
    elif sy.get("registered"):
        a(f"Registered since the last run: {len(sy['registered'])} ({sy.get('env')} env).")
    wl = rg.get("went_live") or []
    a(f"**Went live on the day** ({rg.get('n_went_live')}). Scope each one whose guess is wrong, and skip what "
      "cannot move P&L, in findings.json `changes` (SKILL.md step 7):")
    if wl:
        a("| Key | Live (ET) | Kind | Scope (from) | Subject |")
        a("|---|---|---|---|---|")
        for w in wl:
            by = w.get("basis") if w.get("scope_by") == "auto" else f"set by {w.get('scope_by')}"
            skip = f" SKIPPED: {w['skipped']}" if w.get("skipped") else ""
            a(f"| {w['key']} | {w['at']} | {w['kind']} | {_cell(w['scope'], 60)} ({_cell(by, 50)}) | "
              f"{_cell(w['subject'], 120)}{_cell(skip, 80)} |")
    else:
        a("- none")
    ro = rg.get("readouts") or []
    rc = ", ".join(f"{n} {c}" for c, n in sorted((rg.get("readout_calls") or {}).items(), key=lambda x: -x[1]))
    if rg.get("n_merged"):
        rc += f"; {rg['n_merged']} more are the same money as one of these"
    a(f"**Read out today** ({rg.get('n_readouts')}{'' if not rc else ': ' + rc}"
      f"{'' if len(ro) == rg.get('n_readouts') else f'; the {len(ro)} biggest listed'}). Judge each in "
      "findings.json `changes` (worked / hurt / no-effect / inconclusive / extend):")
    if ro:
        a("| Keys | Live | Scope | Net/day before -> after | Delta (t; p; q) | Can see | Rewards / Trading delta | "
          "Outside delta | Supply x | Call | Overlapping changes |")
        a("|---|---|---|---:|---:|---:|---:|---:|---:|---|---|")
        for r in ro:
            bb, aa, d, t = r.get("before") or {}, r.get("after") or {}, r.get("delta") or {}, r.get("t") or {}
            m = r.get("metric") or "net"
            md = "" if m == "net" else f" ({m}: {_fmt(bb.get(m))} -> {_fmt(aa.get(m))}, t {_fmt(t.get(m))})"
            ov = len(r.get("confounds") or [])
            ovs = (f"{ov} on its series" if ov else "none on its series") + (
                f" + {r.get('book_changes_in_windows')} whole-book" if r.get("book_changes_in_windows") else "")
            a(f"| {', '.join(r.get('keys') or [])[:60]} | {r.get('split')} | {_cell(r.get('scope'), 50)} | "
              f"{usd(bb.get('net'))} -> {usd(aa.get('net'))} | {usd(d.get('net'))} "
              f"({_fmt(t.get('net'))}; {_fmt(r.get('p'), 3)}; {_fmt(r.get('q'), 3)}) | "
              f"{'' if r.get('mde') is None else ('+/-' + usd(r['mde'], False))} | "
              f"{usd(d.get('rew'))} / {usd(d.get('pnl'))} | {usd((r.get('outside') or {}).get('delta'))} | "
              f"{_fmt(r.get('supply_ratio'))} | {r.get('call')}{md} | {ovs} |")
        a("Details:")
        for r in ro:
            subj = " / ".join(_cell(s, 110) for s in (r.get("subjects") or [])[:3])
            more = len(r.get("keys") or []) - 3
            a(f"- {', '.join(r.get('keys') or [])} ({'/'.join(r.get('kinds') or [])}; live {r.get('live')} ET; "
              f"scope by {'/'.join(r.get('scope_by') or [])}"
              f"{' from ' + _cell(r['scope_basis'], 60) if r.get('scope_basis') else ''}; {r.get('series_hit')} series, e.g. "
              f"{', '.join(r.get('series_sample') or [])[:80]}): {subj}{f' (+{more} more)' if more > 0 else ''}")
            extra = []
            if r.get("same_money_as"):
                extra.append("SAME MONEY as " + ", ".join(r["same_money_as"]) + " (counted there; judge them together)")
            if r.get("ids"):
                extra.append("ledger " + ", ".join(r["ids"]))
            if r.get("expect"):
                extra.append(("pre-registered: " if r.get("expect_pre") else "expected (written at the readout, "
                              "not pre-registered): ") + _cell(r["expect"], 160))
            if r.get("confounds"):
                extra.append("overlapping: " + "; ".join(f"{c['key']} {c['side']} {c['split'][5:]} "
                                                        f"{_cell(c['subject'], 60)}" for c in r["confounds"][:5]))
            if r.get("clean_days") is not None and r["clean_days"] < r.get("horizon", REGISTRY_DAYS):
                extra.append(f"{r['clean_days']} clean days before the next overlapping change")
            n1k = r.get("net_per_1k") or {}
            extra.append(f"net per $1k resting {_fmt(n1k.get('before'))} -> {_fmt(n1k.get('after'))}; "
                         f"$ resting {usd0((r.get('before') or {}).get('rest'), False)} -> "
                         f"{usd0((r.get('after') or {}).get('rest'), False)}; fills "
                         f"{_fmt((r.get('before') or {}).get('fills'), 1)} -> {_fmt((r.get('after') or {}).get('fills'), 1)}"
                         f"; daily net after: {', '.join(f'{x[0]} {x[1]:+.0f}' for x in (r.get('daily') or {}).get('after') or [])}")
            a("  " + "; ".join(extra))
    else:
        a("- none due")
    if rg.get("warnings"):
        a("**Early warnings** (in their window and down hard already; look now, don't wait for day 7):")
        for w in rg["warnings"]:
            a(f"- {', '.join(w['keys'])} live {w['split']} ({_cell(w['scope'], 50)}): {w['metric']} "
              f"{usd(w['before'])} -> {usd(w['after'])}/day over {w['after_days']} days (t {_fmt(w['t'])}); "
              f"supply x {_fmt(w.get('supply_ratio'))}; outside {usd(w.get('outside_delta'))}: {_cell(w['subject'], 100)}")
    nd = ", ".join(f"{d} ({n})" for d, n in rg.get("next_due") or [])
    a(f"In their window: {rg.get('in_window')} changes; next readouts: {nd or 'none scheduled'}.")
    nl = rg.get("not_live") or []
    if nl:
        a(f"**Committed, not live yet** (no bot run or main has them; {rg.get('not_live_old')} older ones never went live): "
          + "; ".join(f"{x['key']} {x['at']} {_cell(x['subject'], 70)}" for x in nl))


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
    # CRLF in, text-mode write out = CR CR LF on Windows, which reads back as a
    # blank line after every line and breaks every table (the 11/26 test run)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
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


def _changes_cli(a, ap) -> int:
    day = _day_arg(a.day)
    if a.action == "sync":
        print(json.dumps(registry_sync(day), indent=1))
        return 0
    if a.action == "add":
        if not a.key:
            ap.error("changes add needs a commit sha")
        k = registry_add(a.key, day, a.note)
        if not k:
            print(f"no commit {a.key}")
            return 1
        res = registry_sync(day)
        print(f"registered {k}; {res['resolved']} resolved live")
        return 0
    st = registry_state()
    if a.action == "list":
        horizon = time.time() - 14 * 86400
        rows = sorted(st.values(), key=lambda it: -_f(it.get("live_at") or it.get("ct")))
        for it in rows:
            if not a.all and _f(it.get("live_at") or it.get("ct")) < horizon:
                continue
            rd = (it.get("readouts") or {}).get(str(_horizon(it))) or {}
            call = (rd.get("result") or {}).get("call")
            state = ("skipped" if it.get("skipped") else it.get("verdict") or call
                     or ("live" if it.get("live_at") else "not live"))
            when = et_str(it["live_at"]) if it.get("live_at") else "not live"
            print(f"{it['key']} [{it.get('kind')}/{state}] {when} | {scope_label(it.get('scope'))} | "
                  f"{_one_line(it.get('subject'), 100)}")
        return 0
    k = resolve_key(st, a.key)
    if not k:
        print(f"no change {a.key}")
        return 1
    it = st[k]
    if a.action == "show":
        _print_item(it)
        return 0
    if a.action == "readout":
        sp = _split_day(it)
        if sp is None:
            print(f"{k} is not live yet")
            return 1
        print(json.dumps(change_readout(it.get("scope"), sp, _horizon(it), day, it.get("metric") or "net"),
                         indent=1, default=str))
        return 0
    if a.action == "skip":
        registry_append({"op": "skip", "key": k, "note": a.note or "skipped by hand", "by": a.by,
                         "day": day.isoformat()})
    elif a.action == "verdict":
        if a.verdict not in CHANGE_VERDICTS:
            ap.error("verdict takes " + "|".join(CHANGE_VERDICTS))
        registry_append({"op": "verdict", "key": k, "verdict": a.verdict, "note": a.note, "by": a.by,
                         "day": day.isoformat()})
    elif a.action == "scope":
        raw = {"book": a.book, "series": a.series, "families": [a.family] if a.family else None,
               "groups": [a.group] if a.group else None, "events": a.event, "waves": a.wave,
               "pattern": a.pattern, "basis": a.note or f"set by {a.by}"}
        sc = None
        if any(raw[x] for x in ("book", "series", "families", "groups", "events", "waves", "pattern")):
            sc, why = clean_scope(raw)
            if sc is None:
                ap.error(why)
        if sc is None and not a.metric and not a.expect:
            ap.error("changes scope needs a scope flag, --metric or --expect")
        registry_append({"op": "scope", "key": k, "scope": sc or it.get("scope"), "metric": a.metric,
                         "expect": a.expect, "by": a.by, "day": day.isoformat()})
    print(f"{a.action}: {k}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--day")
    c.add_argument("--bootstrap-days", type=int, default=14)
    c.add_argument("--only", help="comma list of sections (debug)")
    l = sub.add_parser("ledger")
    l.add_argument("action", choices=("list", "show", "due", "stats", "ingest", "decide", "ship", "close",
                                      "recheck"))
    l.add_argument("id", nargs="?")
    l.add_argument("status", nargs="?")
    l.add_argument("--day")
    l.add_argument("--file")
    l.add_argument("--note", default="")
    l.add_argument("--by", default="jack")
    l.add_argument("--ref")
    l.add_argument("--on", help="recheck: the day to look again (YYYY-MM-DD)")
    l.add_argument("--status", dest="status_opt", help="same as the positional status")
    l.add_argument("--all", action="store_true")
    w = sub.add_parser("watch", help="the review's own read-only checks (daily_review/watches.json)")
    w.add_argument("action", choices=("list", "eval", "retire"))
    w.add_argument("id", nargs="?")
    w.add_argument("--day")
    w.add_argument("--note", default="")
    w.add_argument("--all", action="store_true")
    ch = sub.add_parser("changes", help="the change registry (daily_review/changes.jsonl)")
    ch.add_argument("action", choices=("list", "show", "sync", "readout", "add", "scope", "verdict", "skip"))
    ch.add_argument("key", nargs="?")
    ch.add_argument("verdict", nargs="?")
    ch.add_argument("--day")
    ch.add_argument("--note", default="")
    ch.add_argument("--all", action="store_true")
    ch.add_argument("--by", default="jack")
    for f in ("series", "family", "group", "event", "wave", "pattern", "expect"):
        ch.add_argument("--" + f)
    ch.add_argument("--book", action="store_true")
    ch.add_argument("--metric", choices=CHANGE_METRICS)
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
    if a.cmd == "watch":
        if a.action == "list":
            for x in load_watches():
                if x.get("retired") and not a.all:
                    continue
                print(f"{x['id']} [{x.get('type')}/{x.get('severity')}{'/retired ' + x['retired'] if x.get('retired') else ''}] "
                      f"{x.get('title')} (added {x.get('added')}; {_watch_spec(x)})")
            return 0
        if a.action == "eval":       # prints only; the daily collect records the history
            print(json.dumps(eval_watches(_day_arg(a.day), record=False), indent=1, default=str))
            return 0
        if not a.id:
            ap.error("watch retire needs an id")
        res = watches_ingest(_day_arg(a.day), [], [{"id": a.id, "note": a.note}])
        print(json.dumps(res))
        return 0 if res["retired"] else 1
    if a.cmd == "changes":
        return _changes_cli(a, ap)
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
    elif a.action == "recheck":
        on = _iso_day(a.on)
        if not on:
            ap.error("recheck needs --on YYYY-MM-DD")
        ledger_append({"op": "update", "id": a.id, "fields": {"recheck_on": on}, "note": a.note})
    _print_item(ledger_state()[a.id])
    return 0


if __name__ == "__main__":
    sys.exit(main())
