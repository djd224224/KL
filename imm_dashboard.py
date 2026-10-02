#!/usr/bin/env python3
r"""imm_dashboard.py -- the IMM command center: one self-contained HTML page
that answers "how did the book do, what is driving it, where is the risk and
what is left on the table", with every number drillable down to the market.

Jack 2026-09-28: "build a daily command-center type dashboard for IMM showing
how much modeled earnings in the day, trading P&L change in the day, net
impact, positions with the largest moves, quoting across different event
families and events, new events launched, flagging promising new events that
launched and aren't quoted, markets halted etc. ... help me understand
performance at an eye's glance, opportunities and risk, and also let me go
deep to triage exactly what's driving the performance."

WHAT EACH HEADLINE NUMBER IS (the page repeats these next to the numbers):

  Modeled rewards   the bot's own reward estimator integrated over the window:
                    est_frac x pool_per_day over each gap between consecutive
                    full cycles in cycle_log_*.csv, gaps capped at 900 s --
                    exactly imm_reward_recon._scan_cycle_log, bucketed by UTC
                    hour. A MODEL, before Kalshi's $1-per-market-per-period
                    floor; credits land 1-2 days after a period ends and are
                    shown separately from reward_credits.csv (the ledger).
  Trading P&L       MARK-TO-MARKET on the bot's own book: realized in the
                    window (the realized_*.jsonl deltas: closing fills and
                    settlements booked at 0/100 -- scalar at its settlement
                    value and void at cost since 2026-09-29) plus the change
                    in unrealized (sum of pos x (mark - avg) over the
                    marks_*.jsonl 5-minute snapshots at the window's two
                    edges), plus the exits the bot does not book itself
                    (build_exits): scalar settlements it logged as
                    "manual_offset" before 2026-09-29 and positions that leave
                    its book with no record, valued at Kalshi's settlement price;
                    only an exit Kalshi has not settled is a transfer out at
                    its last mark. Audited 2026-09-29 against a replay of
                    Kalshi's own fills and settlement results: equal to the
                    cent on 9/16, 9/22, 9/27, 9/28, 9/29 and the 7-day window.
  Net               modeled rewards + trading P&L.

The DRIVERS view splits trading P&L exactly into the window's own fills and
the inventory carried in (re-marked, or settled by Kalshi); marks out maker
fills at the fill (edge vs the arrival book's mid), 5m, 30m, 4h and to date;
and nets each event's worst case to settlement across its strikes or
exclusive names (IMM_DASHBOARD.md, "Drivers").

Days are ET calendar days (midnight to midnight). The bot's own halt/carry
counters roll at 5am CT; the page shows those beside the daily-loss meter.

SOURCES (all read-only): run-logs/incentive-mm/ -- imm_state.json,
status_incentive_mm.json, cycle_log_*.csv, marks_*, realized_*, fills_*,
settlements_*, guard_skips_*, toxic_halts_*, selection_snapshot_* (the
latest hourly block), reward_credits.csv, reward_calibration.json,
config_history_* (with `git log` for the deploys' subjects). With the API on
(default), also Kalshi's incentive-program feed (new events), the 7:20
quote-gaps estimator (imm_quote_gaps.classify_and_estimate: what unquoted
events would earn), imm_pickoff.scan (pick-off windows), each cached for
API_TTL minutes, and market / event records (strike structure, value now;
cached, a bounded number of reads per build). Every
Kalshi read goes through the same code the morning emails use; nothing here
places, amends or cancels anything, and nothing writes outside DASH_DIR.

PERFORMANCE: completed UTC-day sink files are parsed once and cached
(DASH_DIR/cache, keyed on size:mtime); the live day's files are re-parsed each
run. A cold start parses every file in the history window (a few minutes);
a warm run takes seconds plus any API refresh.

USAGE:
  python imm_dashboard.py              # build DASH_DIR/imm_dashboard.html
  python imm_dashboard.py --open       # ... and open it in the browser
  python imm_dashboard.py --no-api     # local sinks only, no Kalshi reads
  python imm_dashboard.py --api-refresh  # ignore the API cache TTL
Output lines are ASCII (the IMM task consoles are cp1252).
"""

import argparse
import bisect
import csv
import glob
import html as _html
import json
import math
import os
import pickle
import re
import statistics
import subprocess
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    CT = ZoneInfo("America/Chicago")
except Exception:                               # pragma: no cover
    ET = timezone(timedelta(hours=-4))
    CT = timezone(timedelta(hours=-5))

try:        # a title with a non-cp1252 glyph must never kill a scheduled run
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                               # pragma: no cover
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
DASH_DIR = os.environ.get("IMM_DASH_DIR", os.path.join(STATUS_DIR, "dashboard"))
CACHE_DIR = os.path.join(DASH_DIR, "cache")
OUT_HTML = os.path.join(DASH_DIR, "imm_dashboard.html")
TEMPLATE_PATH = os.path.join(HERE, "imm_dashboard_template.html")
LAUNCHER_PATH = os.path.join(HERE, "run_incentive_mm.ps1")

MAX_DT = 900.0            # cycle-gap cap: the reward estimator's convention
HISTORY_DAYS = int(os.environ.get("IMM_DASH_HISTORY_DAYS", "30"))
DETAIL_DAYS = 8           # per-market hourly detail kept this far back
API_TTL_MIN = float(os.environ.get("IMM_DASH_API_TTL_MIN", "60"))
PROGRAMS_TTL_MIN = float(os.environ.get("IMM_DASH_PROGRAMS_TTL_MIN", "30"))
HEARTBEAT_STALE_MIN = 30
MARKOUT_SECS = 1800       # fill mark-out horizon (the toxic backtest's 30m)
MARKOUT_MAX_LAG = 900     # the mark used must land within 15 min of it
# every snapshot horizon a fill is marked out at: (key, seconds after the fill,
# how late the first snapshot at/after it may land). "mk" is the 30-minute one
# the page has always shown; the 5-minute snapshots make "m5" a 5-10 min read.
# Edge at the fill (vs the book's mid one cycle before it) and "to date" (vs
# the market's value now, or its settlement) bracket them.
MARKOUT_HORIZONS = (("m5", 300, 300), ("mk", MARKOUT_SECS, MARKOUT_MAX_LAG),
                    ("m4", 4 * 3600, 900))
MARKOUT_FINAL_AFTER = 36 * 3600   # a query older than this with no mark never gets one
EDGE_MAX_SPREAD = 50              # an arrival book this wide has no mid (incentive_mm MARK_WIDE_SPREAD_CENTS)
MARKET_NOW_TTL = 1800             # re-read an unsettled market's touch every 30 min
MARKET_READ_BUDGET = 1500         # tickers per build for market records (50 per call)
EVENT_READ_BUDGET = 60            # GET /events/{e} per build (exclusivity of word/name events)
FAMILY_CURVE_STEP = 3600          # per-family sparklines: hourly points
NUMERIC_STRIKES = frozenset(("greater", "greater_or_equal", "less", "less_or_equal", "between"))
PROMISING_EST = float(os.environ.get("IMM_DASH_PROMISING_EST", "3.0"))
PROMISING_LEFT = float(os.environ.get("IMM_DASH_PROMISING_LEFT", "500"))
TITLE_BUDGET = int(os.environ.get("IMM_DASH_TITLE_BUDGET", "80"))
PAD_BID, PAD_ASK = 1, 99  # incentive_mm PAD_BID_CENTS / PAD_ASK_CENTS
CACHE_VERSION = 5
DAY_CURVE_STEP = 900              # per-day intraday curves at 15-minute steps
# per-market day arrays on the page: D.days[day].m[ticker] = [...] in this order.
# "cr" / "cs" split trading P&L: the start-of-day position re-marked (cr) or
# settled (cs); the rest is the day's own fills. "fx" is 0 for a market with
# no maker fills, else [edge $, edge cts, m5 $, m5 cts, m4 $, m4 cts, to-date $,
# to-date cts] -- the multi-horizon mark-outs (the 30-minute one is mk).
DAY_FIELDS = ("rew", "pnl", "real", "du", "xfer", "fills", "cts", "usd", "mk", "mk_n",
              "mk_cts", "rest", "pos0", "pos1", "mk0", "mk1", "settled", "cr", "cs", "fx")
FX_KEYS = ("fe", "fe_cts", "m5", "m5_cts", "m4", "m4_cts", "mt", "mt_cts")
VANISH_REAPPEAR_SECS = 6 * 3600   # a position back within this long was only a blip
EXIT_RESULTS_TTL = 3600           # re-ask Kalshi about an unsettled exit hourly

# Guards that mean "something went wrong / risk stood us down" rather than a
# structural reason (band, cutoff, qualify). Names from IMM_LOGGING.md.
RISK_GUARDS = frozenset((
    "event_fill_tripwire", "scan_fill_tripwire", "fill_burst", "blind",
    "event_depth_trip", "event_depth_hold", "one_sided_breaker",
    "scan_mid_tripwire", "move_breaker", "crossed", "breaker_cooldown",
    "toxic_halt"))

# Morning lineup: (label, marker prefix or None, task log, scheduled ET time);
# marker and log are relative to STATUS_DIR. The IMM digest ships inside the
# 7:00 portfolio email since 2026-10-02, so its row watches that email.
LINEUP = (
    ("Earnings overrides", None, "overrides_last_runs.json", "06:45"),
    ("New programs email", "imm_new_programs_sent", "new-programs-task.log", "06:45"),
    ("Portfolio + IMM digest", os.path.join("..", "portfolio-digest", "digest_sent"),
     os.path.join("..", "portfolio-digest", "digest-task.log"), "07:00"),
    ("Toxic-halts email", "imm_toxic_halts_sent", "toxic-halts-task.log", "07:15"),
    ("Quotes & overrides", "imm_quote_gaps_sent", "quote-gaps-task.log", "07:20"),
    ("Opportunistic email", "opportunistic_imm_sent", "opportunistic-task.log", "07:25"),
)


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} [DASH] {msg}",
          flush=True)


def _f(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def event_of(ticker: str) -> str:
    """incentive_mm's rule (IncentiveMarketMaker._event_of)."""
    return ticker.rsplit("-", 1)[0]


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def iso_ts(s) -> float:
    """ISO-8601 (with offset or Z) -> epoch seconds; 0.0 on junk."""
    if isinstance(s, (int, float)):
        return float(s)
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def fx_array(v: dict):
    """A day row's multi-horizon mark-out block in FX_KEYS order, or 0 for a
    market with no maker fill that day (keeps the page small)."""
    if not (v.get("fe_cts") or v.get("m5_cts") or v.get("m4_cts") or v.get("mt_cts")):
        return 0
    return [round(v.get(k, 0.0), 1 if k.endswith("_cts") else 3) for k in FX_KEYS]


# ----------------------------------------------------------------------------
# Families. The rewards report's scheme (report_tools/gen_reward_report.py:
# fam_of_series / group_of / _qp_group) files everything outside mentions,
# rain, gas and temp under one "Quiet-print & scan" family -- which is now
# most of the book, so for triage its SUBJECT groups are promoted to
# families here, and the families the bot has added since (elections, sports
# ladders, awards, approval polls) get their own. Pure functions; tested.
# ----------------------------------------------------------------------------

_CARBON_ARC_SUFFIX = (("APP", "*APP app charts"), ("FT", "*FT foot traffic"),
                      ("POS", "*POS point of sale"), ("CC", "*CC card spend"),
                      ("ADS", "*ADS ad spend"))
_FOOD = frozenset(("KXCHIPBURRITO", "KXDDCOLDBREW", "KXWENBACONATOR",
                   "KXTBCRUNCHWRAP", "KXBKNUGGETS", "KXCFACHICKSAND",
                   "KXYUMTBFT", "KXSBUXSAR", "KXCOCACOLAPOS", "KXBKDWHOPPER",
                   "KXPOPCHICKSAND"))
_KPI = frozenset(("KXDKS", "KXZM", "KXURBN", "KXLOW", "KXDG", "KXAFRM",
                  "KXBBY", "KXWSM", "KXOKTA"))
# Treasury yields beyond the KXUST names -- the bot's rates allowlist
# (incentive_mm _DEFAULT_RATES_EXTRA_SERIES): the how-high / how-low tenors
# KX<n>YRDIR*, notes, bills, the year max, the 2Y FOMC move, curve spreads and
# inversion. Kalshi files the KX<n>YRDIR* tenors under "Financials", which the
# category fallback read as Company KPIs (10/1: KX10YRDIRLM -$44 in that row).
_TREASURY_PREFIX = ("KXUST", "KXNOTE", "KXTNOTE", "TNOTE", "KXTREASURYMAX", "KX3MTBILL",
                    "TBILL", "KX2YFOMC", "KX10Y2Y", "KX10Y3M", "10Y2Y", "10Y3M",
                    "KXYINVERT", "YINVERT", "KX30YUSTW")
_TREASURY_RE = re.compile(r"KX\d+YR(DIR|RATE)[A-Z0-9]*")
_CO_RE = re.compile(
    r"KX(AAL|ALK|AMZN|AXP|BA|CART|CCL|CMG|COINBASE|CVNA|DPZ|FSLR|GOOG|GOOGL|HOOD|"
    r"INTC|LMND|LUV|META|MELI|MELIA|NCLH|NFLX|PM|RACE|RBLX|RDDT|RIVN|SBUX|SG|TLN|TTAN|"
    r"VZ|WH|WING|YOU|YUM|DKS|ZM|URBN|LOW|DG|AFRM|BBY|WSM|OKTA|CTC|VSX)[AY]?")
# the bot's election membership (incentive_mm.ELECTION_SERIES / _PATTERNS);
# election_series() below prefers the live module when it is importable
_ELECTION_EXACT = frozenset((
    "KXSERBIAPRES", "KXBC2ND", "KXBC3RD", "KXQUEBEC4TH", "KXQUEBEC5TH",
    "KXSAARLAND", "KXNORDRHEINWESTFALEN", "KXSCHLESWIGHOLSTEIN",
    "KXPUNJABASSEMBLY", "KXBRAZILTURNOUT", "KXDEMTRIFECTA", "KXUNDERHARRIS",
    "KXVOTEGENERAL", "KXCAATTORNEYGENERAL", "KXFULTONCHAIR"))
_ELECTION_PATTERNS = tuple(re.compile(p) for p in (
    r"KX[A-Z]+CO(UNTY)?JUDGE", r"KX[A-Z]+JUDGE",
    r"(?!KX(ISTANBUL|ACKMAN|APCALLLA|BBG)MAYOR$)KX[A-Z]+MAYOR",
    r"KXHOUSEWINSTATE", r"KXHOUSE[A-Z][A-Z]\d\d?", r"KX[A-Z][A-Z]HOUSE1R",
    r"KXATTYGEN[A-Z][A-Z]"))
_SPORTS_LADDER_RE = re.compile(
    r"KX(NFL|NBA|WNBA|NHL|MLB|NCAAF|NCAAB|CFB|CBB|MLS)[A-Z0-9]*(LADDER|ESCALATOR)[A-Z0-9]*")
_SPORTS_PREFIX = ("KXNFL", "KXMLB", "KXNBA", "KXNHL", "KXWNBA", "KXNCAA", "KXUFC",
                  "KXVENUEPERFORM", "KXMLS", "KXWC", "KXF1", "KXTTELITE", "KXPGA",
                  "KXATP", "KXWTA")
_AWARDS_PREFIX = ("KXGGNOM", "KXGRAMMY", "KXOSCAR", "KXCMA", "KXART", "KXTOP10BB",
                  "KXNETFLIXTOP", "KXWEEKSNUM", "KXNATBOOK", "KXVMA", "KXEMMY",
                  "KXSPOTIFY", "KXBILLBOARD", "KXBOXOFFICE")
_imm_mod = None      # set by _import_imm() when incentive_mm is importable
# the rewards report's exact "Politics & policy" members (_QP_RULES)
_POLICY_EXACT = frozenset(("KXBCNDPSEATS", "KXCANALBERTAREMAIN", "KXMAMDANIEO",
                           "KXDEFRANCHISETAX", "KXCAFAIRPLAN", "KXCASESSION",
                           "KXPAMAILMARGIN", "KXPAMAILREQ"))
# Kalshi's own series category, for a series no rule above recognises
_CATEGORY_FAMILY = {
    "economics": "Econ & rates", "financials": "Company KPIs", "companies": "Company KPIs",
    "politics": "Politics & approval", "elections": "Politics & approval",
    "world": "Politics & approval", "science and technology": "AI & tech",
    "entertainment": "Sports & awards", "sports": "Sports & awards",
    "climate and weather": "Weather & quakes", "crypto": "Crypto",
    "commodities": "Commodities & shipping", "transportation": "Other prints",
    "health": "Other prints",
}


def election_series(series: str) -> bool:
    if _imm_mod is not None and hasattr(_imm_mod, "election_series"):
        try:
            if _imm_mod.election_series(series):
                return True
        except Exception:
            pass
    return series in _ELECTION_EXACT or any(p.fullmatch(series) for p in _ELECTION_PATTERNS)


def _election_group(series: str) -> str:
    if "JUDGE" in series:
        return "County judges"
    if series.endswith("MAYOR"):
        return "Mayors"
    if series.startswith("KXATTYGEN") or series == "KXCAATTORNEYGENERAL":
        return "Attorneys general"
    if series.startswith("KXHOUSE") or series.endswith("HOUSE1R"):
        return "US House"
    return "General elections"


def family_of(series: str, category: str = ""):
    """(family, group) for a series ticker. `category` (Kalshi's series
    category, when known) places a series none of the rules recognise."""
    s = series or ""
    if s.startswith("KXEARNINGSMENTION"):
        return "Earnings mentions", s[len("KXEARNINGSMENTION"):] or "earnings"
    if s.startswith("KXTRUMPMENTION"):
        return "TRUMP mentions", ("TRUMPMENTION-B" if s.startswith("KXTRUMPMENTIONB")
                                  else "TRUMPMENTION")
    if "MENTION" in s:
        return "Other mentions", s
    if election_series(s):
        return "Elections", _election_group(s)
    if s in _POLICY_EXACT:
        return "Politics & approval", s
    if s.startswith("KXTEMPHELP"):                # temporary-help employment, not weather
        return "Econ & rates", "Jobs & economy"
    if s.startswith(("KXTRUMP", "KXGENERICBALLOT", "KXEOWEEK", "KXVANCE", "KXHARRIS",
                     "KXGABBARD", "KXRFK", "KXMAMDANI", "KXNEWSOM", "KXSCOTUS", "KXNEXT")) \
            or any(k in s for k in ("APPROVE", "BALLOT", "SENATE", "TARIFF", "MINWAGE", "VOTE",
                                    "ELECTION", "GOVERNOR", "CABINET", "IMPEACH", "SHUTDOWN",
                                    "PRESPERSON", "NOMINEE", "PARLIAMENT", "CONGRESS")):
        return "Politics & approval", s
    if s == "KXRT":
        return "Rotten Tomatoes", "KXRT"
    for sfx, name in _CARBON_ARC_SUFFIX:
        if s.endswith(sfx) and s not in _FOOD:
            return "Carbon Arc consumer", name
    if _SPORTS_LADDER_RE.fullmatch(s):
        return "Sports & awards", "Sports ladders"
    if s.startswith(_SPORTS_PREFIX):
        return "Sports & awards", "Sports & venues"
    if s.startswith(_AWARDS_PREFIX) or s == "KXMC" or "NOBEL" in s or "ALBUM" in s \
            or s.startswith(("KXYT", "KXMUSIC", "KXDWTS")):
        return "Sports & awards", "Awards, charts & media"
    if s.startswith("KXRAIN"):
        return "Weather & quakes", ("Rain dailies" if s == "KXRAIN" else
                                    "Weekend rain" if s.startswith("KXRAINWKND") else "Rain spans")
    if s.startswith("KXTEMP"):
        return "Weather & quakes", "Hourly temp"
    if any(k in s for k in ("QUAKE", "HURRICANE", "AQI", "TORNADO")) or s.startswith(("KXAVGT", "KXHIGH", "KXLOWT")):
        return "Weather & quakes", s
    if s.startswith(("KXDIESEL", "KXAAAGAS", "KXUSGASCPI", "KXPAGAS")):
        if s == "KXAAAGASD":
            g = "AAA national daily"
        elif s.startswith("KXAAAGASD"):
            g = "AAA state dailies"
        elif s.startswith("KXAAAGASW"):
            g = "AAA weekly"
        elif s.startswith("KXAAAGASM"):
            g = "AAA monthly"
        elif s.startswith("KXDIESELD"):
            g = "Diesel daily"
        elif s.startswith("KXDIESELW"):
            g = "Diesel weekly"
        elif s.startswith("KXDIESEL"):
            g = "Diesel monthly/yearly"
        else:
            g = "Gas CPI"
        return "Gas & diesel", g
    if s in _FOOD:
        return "Company KPIs", "Food price trackers"
    if s in _KPI or _CO_RE.fullmatch(s):
        return "Company KPIs", s
    if s.startswith(("KXCBD", "KXFED", "KXRBNZ", "KXBOI", "KXECB")):
        return "Econ & rates", "Central banks"
    if s.startswith("KXCPI") or "INFL" in s or s in ("KXOER", "KXIBONDFIX", "KXPCE", "KXCOREPCE",
                                                        "KXSAMOMINF"):
        return "Econ & rates", "CPI & inflation"
    if s.startswith(_TREASURY_PREFIX) or _TREASURY_RE.fullmatch(s):
        return "Econ & rates", "Treasury yields"
    if any(k in s for k in ("MORT", "MTG", "HOME", "HPI", "HOUSING", "NHSALES", "OFFVAC", "PERMITS")):
        return "Econ & rates", "Housing & mortgage"
    if any(k in s for k in ("EMP", "JOBS", "JOLTS", "GDP", "CFNAI", "RETAIL", "WALLSTBONUS",
                            "VEHICLEPROD", "POP", "UNEMP", "REMIT", "DEFICIT", "DEBT")):
        return "Econ & rates", "Jobs & economy"
    if any(k in s for k in ("SUEZ", "BABELMANDEB", "HORMUZ", "PANAMA", "BOSPORUS", "MALACCA",
                            "TEU", "SCFI", "FREIGHT")):
        return "Commodities & shipping", "Straits, ports & freight"
    if any(k in s for k in ("CRUDE", "OIL", "SPRLVL", "COAL", "TACONITE", "MARCELLUS",
                            "NUCLEAR", "RESPOWER", "NATGAS")):
        return "Commodities & shipping", "Energy & mining"
    if any(k in s for k in ("CORN", "WHEAT", "COTTON", "CATTLE", "ETHANOL", "FARMLAND", "MAPLE",
                            "MILK", "LOBSTER", "QUAHOG", "HARVEST", "SOYBEAN", "SCREWWORM")) \
            or s == "KXNECOF":                      # Nebraska cattle on feed
        return "Commodities & shipping", "Agriculture"
    if s.startswith(("KXAI", "KXANTHV", "KXOPENV", "KXMOONV", "KXDEEPV", "KXLLM", "KXOAI",
                     "KXANTHROPIC", "KXOPENAI", "KXGEMINI", "KXGOOGSHARE", "KXANTHSHARE",
                     "KXWAYMO", "KXTESLA", "KXMODEL", "KXARENA", "KXTOPMODEL", "KXCHINAAI")) \
            or "ADOPT" in s or s.startswith("KXTOKENUSE") or "DATACENT" in s \
            or s in ("KXOPENSOURCESHARE", "KXXIAOMISHARE", "KXOPENSHARE",
                     "KXDEEPSHARE", "KXB200WS", "KXGPU"):
        return "AI & tech", s
    if s.startswith(("KXBTC", "KXETH", "KXBNB", "KXSOL", "KXXRP", "KXDOGE", "KXCRYPTO",
                     "KXINXVSBTC", "KXCHINAUNBANBTC")):
        return "Crypto", s
    if any(k in s for k in ("VISIT", "HOTEL", "SUBWAY", "ONTIME", "TSA", "ATTENDANCE", "AIRPORT")):
        return "Other prints", "Travel & visits"
    fam = _CATEGORY_FAMILY.get((category or "").strip().lower())
    if fam:
        return fam, s
    return "Other prints", s


# ----------------------------------------------------------------------------
# Time windows (ET calendar days, hour-aligned)
# ----------------------------------------------------------------------------

def et_midnight(d) -> float:
    """Epoch of 00:00 ET on date `d` (a datetime.date)."""
    return datetime(d.year, d.month, d.day, tzinfo=ET).timestamp()


def build_windows(now: float) -> dict:
    today = datetime.fromtimestamp(now, ET).date()
    t0 = et_midnight(today)
    y0 = et_midnight(today - timedelta(days=1))
    w0 = et_midnight(today - timedelta(days=6))
    h24 = (int(now // 3600) - 24) * 3600.0
    roll = bot_roll_start(now)
    return {
        "today": {"label": "Today", "start": t0, "end": now,
                  "desc": f"since 00:00 ET {today:%a %b} {today.day}"},
        "yesterday": {"label": "Yesterday", "start": y0, "end": t0,
                      "desc": "full ET day " + (today - timedelta(days=1)).strftime("%a %b ")
                      + str((today - timedelta(days=1)).day)},
        "24h": {"label": "Last 24h", "start": h24, "end": now,
                "desc": "since " + datetime.fromtimestamp(h24, ET).strftime("%a %I:%M %p ET").replace(" 0", " ")},
        "7d": {"label": "7 days", "start": w0, "end": now,
               "desc": f"since 00:00 ET {(today - timedelta(days=6)):%a %b} {(today - timedelta(days=6)).day}"},
        "roll": {"label": "Bot day", "start": roll, "end": now, "hidden": True,
                 "desc": "since the bot's 5am CT roll"},
    }


def bot_roll_start(now: float) -> float:
    """Start of the bot's current 5am-CT roll day (incentive_mm._halt_day_key)."""
    ct = datetime.fromtimestamp(now, CT)
    start = ct.replace(hour=5, minute=0, second=0, microsecond=0)
    if ct < start:
        start -= timedelta(days=1)
        start = start.replace(hour=5)
    return start.timestamp()


# ----------------------------------------------------------------------------
# Pure helpers for the drivers view: the mark rule, worst cases, the change
# log and the paid-vs-modeled roll-up (each tested on its own)
# ----------------------------------------------------------------------------

def touch_mark_cents(bid, ask, last, wide=50):
    """incentive_mm.touch_mark_cents: the YES mark of a touch (None = an EMPTY
    side) and the last trade. Two-sided: the mid, or the last trade clamped
    inside the touch when the book is `wide`c or wider. One-sided: the last
    trade clamped to the live side. Nothing live: the last trade."""
    if bid is not None and ask is not None:
        if last is not None and wide and ask - bid >= wide:
            return float(min(max(last, bid), ask))
        return (bid + ask) / 2.0
    if last is None:
        return None
    if bid is not None:
        return float(max(last, bid))
    if ask is not None:
        return float(min(last, ask))
    return float(last)


def _live_dollars(m, base):
    """A side's raw dollar price when it is a real level (strictly inside
    (0, 1)), else None: Kalshi shows an empty bid as $0 and an empty ask as $1."""
    try:
        v = m.get(base + "_dollars")
        x = float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        x = None
    return x if x is not None and 0.0 < x < 1.0 else None


def market_value_cents(m, wide=50):
    """(YES value in cents or None, final) for a Kalshi market record: its
    settlement once Kalshi has one (yes 100, no 0, scalar at its exact
    settlement_value; a void has no value), else the bot's mark rule on the
    touch (bid floored, ask ceiled) and the last trade."""
    res = str(m.get("result") or "").lower()
    if res in ("yes", "no"):
        return (100.0 if res == "yes" else 0.0), True
    if res == "scalar":
        try:
            return round(float(m.get("settlement_value_dollars")) * 100.0, 4), True
        except (TypeError, ValueError):
            return None, True
    if res == "void":
        return None, True
    b, a = _live_dollars(m, "yes_bid"), _live_dollars(m, "yes_ask")
    bid = math.floor(b * 100.0 + 1e-9) if b is not None else None
    ask = math.ceil(a * 100.0 - 1e-9) if a is not None else None
    lv = _f(m.get("last_price_dollars"), 0.0)
    last = math.floor(lv * 100.0 + 1e-9) if lv > 0 else None
    return touch_mark_cents(bid, ask, last, wide), False


def strike_pays(st: str, lo, hi, v: float) -> bool:
    """Does a numeric-strike market settle YES at outcome value v?"""
    if st == "greater":
        return v > lo
    if st == "greater_or_equal":
        return v >= lo
    if st == "less":
        return v < hi
    if st == "less_or_equal":
        return v <= hi
    if st == "between":
        return lo <= v <= hi
    return False


def _strike_usable(s) -> bool:
    st, lo, hi = s[0], s[1], s[2]
    if st in ("greater", "greater_or_equal"):
        return lo is not None
    if st in ("less", "less_or_equal"):
        return hi is not None
    if st == "between":
        return lo is not None and hi is not None
    return False


def market_worst(pos: float, mark) -> float:
    """One market's worst P&L from its mark to settlement (<= 0): a long
    loses its mark, a short loses 100 - mark."""
    if mark is None or abs(pos) < 1e-9:
        return 0.0
    return -pos * mark / 100.0 if pos > 0 else pos * (100.0 - mark) / 100.0


def event_worst_case(holdings, struct, me=None, n_event=None):
    """Worst P&L from the current marks to settlement for one event's
    positions: (worst, gross, method), both <= 0.

    `holdings` [(ticker, pos, mark_cents)], pos > 0 long YES (the caller
    marks a position with no mark at its cost). `struct` {ticker: (strike
    type, floor, cap, market type)} from Kalshi. gross adds every market's
    own worst case, with no netting. worst nets markets that cannot all lose
    at once:
      strikes      every market prices the same number (greater / less /
                   between strikes): each outcome region is a scenario
      exclusive    Kalshi marks the event mutually exclusive (one name wins):
                   each held market winning alone, plus none of them unless
                   the book holds every market of the event
      independent  not exclusive (mention words): worst = gross
      unknown      no structure read yet: worst = gross (conservative)
    A scalar market (fractional settlement) is never netted."""
    gross = 0.0
    live = []
    for t, pos, mk in holdings:
        w = market_worst(pos, mk)
        if mk is None or abs(pos) < 1e-9:
            continue
        gross += w
        live.append((t, pos, mk, w))
    if not live:
        return 0.0, 0.0, "flat"
    binaries, scalars = [], 0.0
    for t, pos, mk, w in live:
        s = struct.get(t)
        if s is not None and len(s) > 3 and s[3] == "scalar":
            scalars += w
        else:
            binaries.append((t, pos, mk, s))
    g = round(gross, 4)
    if not binaries:
        return g, g, "independent"
    if any(s is None for _t, _p, _m, s in binaries):
        return g, g, "unknown"

    def pnl(paid):
        return sum(pos * ((100.0 if t in paid else 0.0) - mk) / 100.0
                   for t, pos, mk, _s in binaries)

    if all(s[0] in NUMERIC_STRIKES and _strike_usable(s) for _t, _p, _m, s in binaries):
        vals = sorted({x for _t, _p, _m, s in binaries for x in (s[1], s[2]) if x is not None})
        pts = [vals[0] - 1.0, vals[-1] + 1.0]
        for x in vals:
            eps = 1e-6 * max(1.0, abs(x))
            pts += [x - eps, x, x + eps]
        worst = min(pnl({t for t, _p, _m, s in binaries if strike_pays(s[0], s[1], s[2], v)})
                    for v in pts)
        return round(min(worst, 0.0) + scalars, 4), g, "strikes"
    if me:
        names = [t for t, _p, _m, _s in binaries]
        scen = [pnl({t}) for t in names]
        if not (n_event and len(names) >= n_event):
            scen.append(pnl(set()))
        return round(min(min(scen), 0.0) + scalars, 4), g, "exclusive"
    if me is False:
        return g, g, "independent"
    return g, g, "unknown"


# which family a code / config change is about, by name: a commit subject or
# an IMM_* knob mentioning these words. Crude on purpose and labelled as such
# on the page ("matched by name"); every change is listed there regardless.
_CHANGE_FAMILY_WORDS = (
    ("Gas & diesel", ("GAS", "DIESEL", "AAA")),
    ("Carbon Arc consumer", ("CARBON ARC", "CARBONARC", "IMM_CA_", "CARBON_ARC")),
    ("Weather & quakes", ("RAIN", "QUAKE", "USGS", "HOURLY TEMP", "KXTEMP", "WEATHER", "HURRICANE")),
    ("Econ & rates", ("TREASURY", "CPI", "MORTGAGE", "_MORT", " MORT", "RATES", "FOMC", "YIELD",
                      "KXUST", "PAYROLL", "INFLATION")),
    ("Sports & awards", ("SPORTS", "LADDER", "ESCALATOR", "NFL", "MLB", "NBA", "NHL", "AWARD",
                         "VENUE", "OSCAR", "GRAMMY", "TABLE TENNIS")),
    ("TRUMP mentions", ("TRUMPMENTION", "TRUMP MENTION", "MENTION GATE", "DEPTH_GATE", "DEPTH GATE")),
    ("Earnings mentions", ("EARNINGS",)),
    ("Politics & approval", ("APPROVE", "APPROVAL", "APRPOTUS", "POLITIC")),
    ("Elections", ("ELECTION",)),
    ("AI & tech", ("VERCEL", "OPENROUTER", "TOKENUSE", "LM ARENA", "GPU", "DATACENTER", "B200")),
    ("Rotten Tomatoes", ("KXRT", "ROTTEN")),
    ("Company KPIs", ("KPI", "FOOD PRICE", "CHIPBURRITO", "SPICE")),
    ("Commodities & shipping", ("CRUDE", "SHIPPING", "STRAIT", "FREIGHT", "SPRLVL")),
    ("Crypto", ("KXBTC", "KXETH", "KXINXVSBTC")),
)


def change_families(text: str):
    """Families a change's subject / knob names mention (sorted)."""
    u = (text or "").upper()
    return sorted({fam for fam, words in _CHANGE_FAMILY_WORDS if any(w in u for w in words)})


def imm_subjects(subjects):
    """The commit subjects ("<sha> <subject>") that are about the IMM: the
    repo's "imm ..." prefix, or a merge naming it. A deploy carries every
    commit since the last one; the crypto fleets' or other bots' commits in
    it change nothing the IMM trades, so they must not tag an IMM family."""
    out = []
    for s in subjects or []:
        text = s.split(" ", 1)[1] if " " in s else s
        low = text.lower()
        if low.startswith("imm") or " imm" in low or "incentive" in low:
            out.append(s)
    return out


def parse_changes(rows):
    """[{ts, sha, prev, keys, nkeys, vals}]: one entry per bot run whose code
    (git_sha) or IMM_* launcher config differs from the run before it, from
    config_history rows in any order. A restart onto the same code and config
    is not a change."""
    out, prev = [], None
    for r in sorted(rows, key=lambda x: iso_ts(x.get("ts"))):
        cfg = {k: str(v) for k, v in (r.get("config") or {}).items() if str(k).startswith("IMM_")}
        sha = str(r.get("git_sha") or "")
        ts = iso_ts(r.get("ts"))
        if prev is not None and ts:
            pcfg, psha = prev
            keys = sorted(k for k in set(cfg) | set(pcfg) if cfg.get(k) != pcfg.get(k))
            if keys or (sha and psha and sha != psha):
                out.append({"ts": ts, "sha": sha, "prev": psha, "keys": keys[:12], "nkeys": len(keys),
                            "vals": {k: [(pcfg.get(k) or "")[:60], (cfg.get(k) or "")[:60]]
                                     for k in keys[:6]}})
        prev = (cfg, sha)
    return out


def rollup_realization(calib, fam_of):
    """Kalshi-paid vs modeled per dashboard family, from imm_reward_recon's
    calibration (settled events only, so periods match): the post-amendment
    series on the RAW estimate when the calibration carries them
    (credited / est: the $1 floor and model error together), else every
    settled series on the FLOORED estimate (credited / est_floor).
    -> {"basis", "generated_at", "fams": {fam: [credited, est, events]}, "all"}."""
    calib = calib or {}
    pa = (calib.get("post_amendment") or {}).get("series")
    basis, src = ("raw", pa) if pa else ("floored", calib.get("series") or {})
    fams = defaultdict(lambda: [0.0, 0.0, 0])
    for ser, v in src.items():
        if not isinstance(v, dict):
            continue
        est = _f(v.get("est")) if basis == "raw" else _f(v.get("est_floor"))
        a = fams[fam_of(ser)]
        a[0] += _f(v.get("credited"))
        a[1] += est
        a[2] += int(_f(v.get("n")))
    tot = [sum(a[0] for a in fams.values()), sum(a[1] for a in fams.values()),
           sum(a[2] for a in fams.values())]
    return {"basis": basis, "generated_at": calib.get("generated_at"),
            "fams": {k: [round(a[0], 2), round(a[1], 2), a[2]] for k, a in sorted(fams.items())},
            "all": [round(tot[0], 2), round(tot[1], 2), tot[2]]}


# ----------------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------------

class FileCache:
    """Per-file parse results for COMPLETED day files, keyed on size:mtime.
    The live (current UTC day) file is never cached: it grows every cycle."""

    def __init__(self, name: str):
        self.path = os.path.join(CACHE_DIR, f"{name}.pkl")
        self.data = {}
        self.dirty = False
        try:
            with open(self.path, "rb") as f:
                blob = pickle.load(f)
            if blob.get("v") == CACHE_VERSION:
                self.data = blob.get("files") or {}
        except Exception:
            self.data = {}

    @staticmethod
    def sig(path: str) -> str:
        st = os.stat(path)
        return f"{st.st_size}:{int(st.st_mtime)}"

    def get(self, path: str):
        e = self.data.get(os.path.basename(path))
        if e and e.get("sig") == self.sig(path):
            return e["val"]
        return None

    def put(self, path: str, val) -> None:
        self.data[os.path.basename(path)] = {"sig": self.sig(path), "val": val}
        self.dirty = True

    def prune(self, keep_names) -> None:
        for k in list(self.data):
            if k not in keep_names:
                del self.data[k]
                self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"v": CACHE_VERSION, "files": self.data}, f,
                        protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, self.path)


def day_files(prefix: str, ext: str, first_day: str):
    """[(utc_day, path)] for STATUS_DIR/<prefix>_<YYYY-MM-DD>.<ext>, sorted,
    from `first_day` on."""
    out = []
    for p in glob.glob(os.path.join(STATUS_DIR, f"{prefix}_*.{ext}")):
        m = re.search(r"_(\d{4}-\d{2}-\d{2})\." + re.escape(ext) + "$", p)
        if m and m.group(1) >= first_day:
            out.append((m.group(1), p))
    return sorted(out)


def file_complete(day: str, path: str, now: float) -> bool:
    """A UTC-day file is final once its day is over and it has sat still for
    10 minutes (the bot flushes the last cycle a little after midnight)."""
    end = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + 86400
    try:
        return now > end + 600 and now - os.stat(path).st_mtime > 600
    except OSError:
        return False


def iter_jsonl(path: str):
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


# ----------------------------------------------------------------------------
# cycle_log: hourly reward accrual, resting-dollar time, cycle timing, and the
# live file's last full cycle (the current quoting state)
# ----------------------------------------------------------------------------

def _row_resting_cents(row) -> float:
    """What our resting book costs if it all fills, in cents: a YES bid at p
    costs p, a YES ask at p (a NO bid) costs 100-p, pads at 1c / 99c. The
    rewards report's quote_dollars.row_cents 'exact' measure."""
    if len(row) < 19:
        return 0.0
    bct, act = _f(row[13], None), _f(row[14], None)
    if bct is None or act is None:
        return 0.0
    btop, atop = _f(row[15], None), _f(row[16], None)
    pb, pa = _f(row[17]), _f(row[18])
    return (pb * PAD_BID + pa * (100 - PAD_ASK)
            + (max(bct - pb, 0.0) * btop if btop is not None else 0.0)
            + (max(act - pa, 0.0) * (100.0 - atop) if atop is not None else 0.0))


_CYCLE_FIELDS = ("ts", "ticker", "ext_bid", "ext_ask", "yes_depth", "no_depth", "target",
                 "est_frac", "qual_sides", "acct_pos", "own_pos", "pool_per_day", "quoted",
                 "own_bid_ct", "own_ask_ct", "own_bid_top", "own_ask_top", "own_pad_bid_ct",
                 "own_pad_ask_ct", "want_bid_ct", "want_ask_ct", "want_pad_ct", "hour_mult",
                 "rung_lo", "room_buy", "room_sell", "vol24h", "discount", "is_scan",
                 "is_sticky", "reduce_only", "fast", "run_id", "config_hash")


def parse_cycle_file(path: str, keep_last: bool = False, max_dt: float = MAX_DT,
                     max_ts: float = None) -> dict:
    """One cycle_log day file ->
         hourly  {ticker: {utc_hour: est $}}       reward accrual
         rest    {ticker: {utc_hour: $-seconds}}   resting-dollar time
         cycles  [cycle epoch, ...]
         runs    {run_id: [first_ts, last_ts, n_cycles]}
         last    {ticker: row dict} of the last FULL cycle (keep_last only)
    The accrual reproduces imm_reward_recon._scan_cycle_log: each cycle is
    credited with est_frac x pool_per_day over the gap since the previous
    cycle, capped at max_dt, and lands in the UTC hour of its own timestamp."""
    hourly = defaultdict(lambda: defaultdict(float))
    rest = defaultdict(lambda: defaultdict(float))
    cycles, runs = [], {}
    prev_ts = cur_ts = None
    cur_str = None
    batch, prev_batch = [], []

    def flush(ts, rows, dt):
        hour = int(ts // 3600)
        for tkr, frac, pool, rc, _row in rows:
            v = frac * pool * dt / 86400.0
            if v:
                hourly[tkr][hour] += v
            if rc:
                rest[tkr][hour] += rc / 100.0 * dt

    try:
        f = open(path, newline="", encoding="utf-8", errors="replace")
    except OSError:
        return {"hourly": {}, "rest": {}, "cycles": [], "runs": {}, "last": {}}
    with f:
        rdr = csv.reader(f)
        # rows the bot writes while this build runs are after the build's "now":
        # leave them for the next build, or windows ending "now" disagree
        max_str = (datetime.fromtimestamp(max_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                   if max_ts else None)
        for row in rdr:
            if len(row) < 13 or row[0] == "ts":
                continue
            if max_str and row[0] > max_str:
                break
            s = row[0]
            if s != cur_str:
                try:
                    ts = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                if cur_ts is not None and ts != cur_ts:
                    if prev_ts is not None:
                        dt = min(cur_ts - prev_ts, max_dt)
                        if dt > 0:
                            flush(cur_ts, batch, dt)
                    prev_ts = cur_ts
                    prev_batch = batch
                    batch = []
                    cycles.append(ts)
                elif cur_ts is None:
                    cycles.append(ts)
                cur_ts, cur_str = ts, s
            try:
                frac, pool = float(row[7]), float(row[11])
            except (ValueError, IndexError):
                continue
            rc = _row_resting_cents(row)
            batch.append((row[1], frac, pool, rc, row if keep_last else None))
            if len(row) >= 33:
                rid = row[32]
                r = runs.get(rid)
                if r is None:
                    runs[rid] = [cur_ts, cur_ts, 0]
                else:
                    r[1] = cur_ts
    if batch and prev_ts is not None:
        dt = min(cur_ts - prev_ts, max_dt)
        if dt > 0:
            flush(cur_ts, batch, dt)
    # run cycle counts
    last = {}
    if keep_last:
        # the newest batch may still be half-written: fall back to the one
        # before it when it is clearly short
        use = batch if (not prev_batch or len(batch) >= 0.9 * len(prev_batch)) else prev_batch
        use_ts = cur_ts if use is batch else prev_ts
        for tkr, frac, pool, rc, row in use:
            if row is None:
                continue
            d = {k: (row[i] if i < len(row) else "") for i, k in enumerate(_CYCLE_FIELDS)}
            d["_ts"] = use_ts
            d["_resting_usd"] = rc / 100.0
            last[tkr] = d
    return {"hourly": {t: dict(h) for t, h in hourly.items()},
            "rest": {t: dict(h) for t, h in rest.items()},
            "cycles": cycles, "runs": runs, "last": last}


# ----------------------------------------------------------------------------
# The Modeled-rewards card's projected full day: the bot's current book
# carried to midnight ET
# ----------------------------------------------------------------------------

PROJ_MAX_CYCLE_AGE_S = 900.0     # no cycle this recent: the bot is not quoting


def share_at(frac: float, k: float) -> float:
    """Our share of a pool at k times our resting size, the rest of the book
    unchanged: k f / (1 + (k - 1) f). On 2026-10-02 it put the 10:00 ET size
    drop (x2 -> x1) at $751/day against the $788/day the bot then logged;
    halving said $733."""
    if frac <= 0 or k == 1.0:
        return frac
    return k * frac / (1.0 + (k - 1.0) * frac)


def project_rest_of_day(rows, now: float, mult_at=None, end: float = None) -> float:
    """Modeled rewards from `now` to 00:00 ET tomorrow if the book holds
    (Jack 2026-10-02: "show the projected full-day total on the card, not
    run-rate"). Every market of the bot's last cycle earns its pool at its
    current share, re-scaled each ET hour to the size the bot quotes then
    (share_at, k = mult_at(series, epoch) / its multiplier now), until its
    stop (cutoff or program end). mult_at None, or a market whose multiplier
    now the schedule does not reproduce (the bot's runtime state: open-scan
    members, structural dailies), keeps its current size all day.
      rows: [(ticker, est_frac, pool $/day, hour_mult now, stop epoch or None)]"""
    if end is None:
        end = et_midnight(datetime.fromtimestamp(now, ET).date() + timedelta(days=1))
    slots, t = [], now
    while t < end:
        nxt = min((math.floor(t / 3600.0) + 1) * 3600.0, end)
        slots.append((t, nxt))
        t = nxt
    cache = {}

    def mult(ser, ts):
        key = (ser, int(ts // 3600))
        if key not in cache:
            cache[key] = _f(mult_at(ser, ts), 1.0)
        return cache[key]

    total = 0.0
    for tkr, frac, pool, hm, stop in rows:
        if frac <= 0 or pool <= 0:
            continue
        ser = series_of(tkr)
        scaled = mult_at is not None and hm > 0 and abs(mult(ser, now) - hm) < 1e-9
        for a, b in slots:
            if stop is not None:
                if a >= stop:
                    break
                b = min(b, stop)
            k = mult(ser, (a + b) / 2.0) / hm if scaled else 1.0
            total += pool * share_at(frac, k) * (b - a) / 86400.0
    return total


# ----------------------------------------------------------------------------
# marks: 5-minute snapshots of every open own-book position
# ----------------------------------------------------------------------------

def parse_marks_file(path: str, keep_hours: bool, queries=None, max_ts: float = None):
    """One marks day file ->
         totals  [(snap_ts, unrealized $, n_positions, gross $ at risk)]
         hours   {utc_hour: (snap_ts, {ticker: (pos, avg_c, mark_c)})} -- the FIRST
                 snapshot at or after each hour boundary (keep_hours only)
         last    (snap_ts, {ticker: (pos, avg, mark)}) -- the newest snapshot
         first   the oldest snapshot, same shape (the file-boundary diff)
         gone    [(ticker, pos, avg, mark, last_seen_ts, first_missing_ts)]
         came    [(ticker, first_seen_ts)] between consecutive snapshots
         edges   {et_midnight_ts: (snap_ts, book)} -- first snapshot of each ET day
    `queries` {key: (ticker, target_ts[, max_lag])} is answered in place with
    the first snapshot mark at or after target_ts (within max_lag, default
    MARKOUT_MAX_LAG): the fill mark-outs, resolved while the rows are in
    memory."""
    snaps = defaultdict(dict)
    for r in iter_jsonl(path):
        ts = iso_ts(r.get("ts"))
        t = r.get("ticker")
        if not ts or not t or (max_ts and ts > max_ts):
            continue
        pos = _f(r.get("pos"))
        if abs(pos) <= 1e-9:
            continue
        mk = r.get("mark_cents")
        snaps[ts][t] = (pos, _f(r.get("avg_cents")), (float(mk) if mk is not None else None))
    order = sorted(snaps)
    totals, hours = [], {}
    for ts in order:
        book = snaps[ts]
        u = gross = 0.0
        for pos, avg, mk in book.values():
            if mk is not None:
                u += pos * (mk - avg) / 100.0
            gross += (pos * avg if pos > 0 else -pos * (100.0 - avg)) / 100.0
        totals.append((ts, round(u, 4), len(book), round(gross, 2)))
        h = int(ts // 3600)
        if keep_hours and h not in hours:
            hours[h] = (ts, book)
    last = (order[-1], snaps[order[-1]]) if order else (0.0, {})
    first = (order[0], snaps[order[0]]) if order else (0.0, {})
    # the first snapshot after each ET midnight in the file: the day edges every
    # past day's P&L is measured between (kept for the whole history, unlike
    # the hourly detail)
    edges = {}
    for a, b in zip(order, order[1:]):
        da = datetime.fromtimestamp(a, ET).date()
        db = datetime.fromtimestamp(b, ET).date()
        if db > da:
            edges[et_midnight(db)] = (b, snaps[b])
    # positions leaving / entering the book between consecutive snapshots:
    # the raw material for spotting a position that left with no record
    gone, came = [], []
    for a, b in zip(order, order[1:]):
        pa, pb = snaps[a], snaps[b]
        for t, (p, avg, mk) in pa.items():
            if t not in pb:
                gone.append((t, p, avg, mk, a, b))
        for t in pb:
            if t not in pa:
                came.append((t, b))
    if queries:
        # per ticker: sorted [(ts, mark)] only for tickers queried
        want = defaultdict(list)
        for key, q in queries.items():
            want[q[0]].append((q[1], q[2] if len(q) > 2 else MARKOUT_MAX_LAG, key))
        for t, lst in want.items():
            series = [(ts, snaps[ts][t][2]) for ts in order if t in snaps[ts]
                      and snaps[ts][t][2] is not None]
            if not series:
                continue
            stamps = [s[0] for s in series]
            for target, lag, key in lst:
                i = bisect.bisect_left(stamps, target)
                if i < len(series) and series[i][0] - target <= lag:
                    queries[key] = ("ok", series[i][1], series[i][0])
    return {"totals": totals, "hours": hours if keep_hours else {}, "last": last,
            "first": first, "gone": gone, "came": came, "edges": edges}


# ----------------------------------------------------------------------------
# Loaders for the small sinks
# ----------------------------------------------------------------------------

def load_realized(first_day: str):
    """[(ts, ticker, event, delta $)]"""
    out = []
    for _d, p in day_files("realized", "jsonl", first_day):
        for r in iter_jsonl(p):
            ts = iso_ts(r.get("ts"))
            t = r.get("ticker")
            if ts and t:
                out.append((ts, t, r.get("event_ticker") or event_of(t),
                            _f(r.get("realized_delta_dollars"))))
    out.sort()
    return out


def load_fills(first_day: str):
    out, seen = [], set()
    for _d, p in day_files("fills", "jsonl", first_day):
        for r in iter_jsonl(p):
            ts = _f(r.get("ts"))
            t = r.get("ticker")
            if not ts or not t:
                continue
            side = r.get("our_book_side")
            if side not in ("bid", "ask"):
                sa = (r.get("side"), r.get("action"))
                side = "bid" if sa in (("yes", "buy"), ("no", "sell")) else "ask"
            px = _f(r.get("yes_price_cents"))
            cnt = _f(r.get("count"))
            fid = r.get("fill_id") or f"{t}:{ts}:{cnt}"
            # the sink writes a fill twice now and then around the UTC file
            # roll (14 of 8,236 rows 9/6-9/29); Kalshi's fill id is unique
            if fid in seen:
                continue
            seen.add(fid)
            # the external book the bot read the cycle before the fill (the
            # context the order was priced on): its mid is the edge's
            # reference. Only a live two-sided book has one (99% of maker
            # fills), and -- the bot's own mark rule -- not a book 50c+ wide
            # (KXSPRLVL 37 x 88 on 10/1 put a 91c sale "28c over mid")
            xb, xa = _f(r.get("ext_bid"), None), _f(r.get("ext_ask"), None)
            mid = ((xb + xa) / 2.0 if (xb is not None and xa is not None and 0 < xb < xa < 100
                                        and xa - xb < EDGE_MAX_SPREAD) else None)
            out.append({
                "id": fid, "ts": ts, "t": t,
                "ev": r.get("event_ticker") or event_of(t), "side": side, "px": px,
                "n": cnt, "taker": bool(r.get("is_taker")), "pad": bool(r.get("is_pad")),
                "scan": bool(r.get("is_scan")),
                "pos0": _f(r.get("pos_before")), "pos1": _f(r.get("pos_after")),
                "bid": r.get("ext_bid"), "ask": r.get("ext_ask"), "mid": mid,
                "age": r.get("order_age_secs"),
                # when the bot BOOKED it (fills are polled ~1s into a cycle);
                # a snapshot stamped at that cycle's start already holds it
                "cyc": _f(r.get("cycle_ts"), None),
            })
    out.sort(key=lambda x: x["ts"])
    return out


def load_settlements(first_day: str):
    out = []
    for _d, p in day_files("settlements", "jsonl", first_day):
        for r in iter_jsonl(p):
            ts = iso_ts(r.get("ts"))
            t = r.get("ticker")
            if ts and t:
                out.append({"ts": ts, "t": t, "ev": r.get("event_ticker") or event_of(t),
                            "result": r.get("result"), "px": r.get("settle_price_cents"),
                            "pos": _f(r.get("own_pos_at_settle")),
                            "avg": _f(r.get("own_avg_cents")),
                            "real": _f(r.get("market_realized_dollars"))})
    out.sort(key=lambda x: x["ts"])
    return out


def load_toxic(first_day: str):
    out = []
    for _d, p in day_files("toxic_halts", "jsonl", first_day):
        for r in iter_jsonl(p):
            r["ts"] = _f(r.get("ts"))
            out.append(r)
    out.sort(key=lambda x: x["ts"])
    return out


def load_guards(now: float):
    """Current guard holds from guard_skips_*.jsonl, replayed within the NEWEST
    run only (a restart re-emits `enter` for every held ticker, and cannot
    emit `clear` for one it no longer manages). Returns (holds, counts_today,
    run_id) with holds {ticker: {guard, since, inputs, bid, ask}}."""
    files = day_files("guard_skips", "jsonl",
                      datetime.fromtimestamp(now - 2 * 86400, timezone.utc).strftime("%Y-%m-%d"))
    rows = []
    for _d, p in files[-2:]:
        rows.extend(iter_jsonl(p))
    if not rows:
        return {}, {}, None
    run = rows[-1].get("run_id")
    holds = {}
    entered = Counter()
    today0 = et_midnight(datetime.fromtimestamp(now, ET).date())
    for r in rows:
        ts = iso_ts(r.get("ts"))
        kind = r.get("kind")
        if kind == "enter" and ts >= today0 and r.get("prev") is None:
            entered[r.get("guard")] += 1
        if r.get("run_id") != run:
            continue
        t = r.get("ticker")
        if kind in ("enter", "snapshot") and t:
            prev = holds.get(t)
            since = prev["since"] if prev and prev["guard"] == r.get("guard") else ts
            holds[t] = {"guard": r.get("guard"), "since": since, "inputs": r.get("inputs") or {},
                        "bid": r.get("ext_bid"), "ask": r.get("ext_ask")}
        elif kind == "clear" and t:
            holds.pop(t, None)
    return holds, dict(entered), run


def read_last_snapshot(prefix: str = "selection_snapshot"):
    """Rows of the newest hourly selection_snapshot block (all candidates the
    bot scored, with decision + modelled yield). Reads the file tail only."""
    files = day_files(prefix, "jsonl", "2000-01-01")
    for _d, path in reversed(files[-2:]):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        span = 8 << 20
        while True:
            start = max(0, size - span)
            with open(path, "rb") as f:
                f.seek(start)
                blob = f.read()
            lines = blob.split(b"\n")
            if start > 0:
                lines = lines[1:]
            rows = []
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    continue
            if not rows:
                break
            last_ts = rows[-1].get("ts")
            block = [r for r in rows if r.get("ts") == last_ts]
            covered = start == 0 or rows[0].get("ts") != last_ts
            if covered or span >= size:
                return block, iso_ts(last_ts)
            span *= 2
    return [], 0.0


def load_credits():
    """(rows [(credit_date, event, amount)], calibration dict)."""
    rows = []
    try:
        with open(os.path.join(STATUS_DIR, "reward_credits.csv"), newline="",
                  encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((r.get("credit_date") or "", r.get("event_ticker") or "",
                             _f(r.get("amount"))))
    except (OSError, ValueError, KeyError):
        rows = []
    return rows, load_json(os.path.join(STATUS_DIR, "reward_calibration.json"))


def launcher_env() -> dict:
    """The live launcher's $ProbeEnv (run_incentive_mm.ps1), longest match --
    line ~56 is the EMPTY default the naive regex hits first."""
    for path in (LAUNCHER_PATH, r"C:\Users\jackd\Documents\KL\run_incentive_mm.ps1"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        hits = re.findall(r'\$ProbeEnv\s*=\s*"(set [^"\n]+)"', text)
        if not hits:
            continue
        env = {}
        for part in max(hits, key=len).split("&&"):
            part = part.strip()
            if part.startswith("set "):
                k, _, v = part[4:].partition("=")
                env[k.strip()] = v.strip()
        return env
    return {}


# ----------------------------------------------------------------------------
# API sections (cached): programs feed, quote-gaps estimator, pick-off windows
# ----------------------------------------------------------------------------

def _kalshi_markets(tickers):
    """{ticker: market record} via kalshi_reads (signed, paced)."""
    sys.path.insert(0, HERE)
    from kalshi_reads import kalshi_get
    out = {}
    for i in range(0, len(tickers), 50):
        js = kalshi_get("/markets", {"tickers": ",".join(tickers[i:i + 50]), "limit": 50})
        for m in js.get("markets") or []:
            out[m.get("ticker")] = m
    return out


def _kalshi_event(ev):
    """{"mutually_exclusive", "markets"} for one Kalshi event (signed GET
    /events/{ev} with its markets), or None when Kalshi has no such event."""
    sys.path.insert(0, HERE)
    from kalshi_reads import kalshi_get
    try:
        js = kalshi_get(f"/events/{ev}", {"with_nested_markets": "true"})
    except Exception as e:
        if getattr(e, "status", None) == 404:
            return None
        raise
    e = js.get("event") or {}
    if not e:
        return None
    return {"mutually_exclusive": e.get("mutually_exclusive"),
            "markets": e.get("markets") or js.get("markets") or []}


def _import_imm():
    """(imm_quote_gaps, incentive_mm) with the launcher env mirrored first, or
    (None, None). imm_quote_gaps must be imported BEFORE incentive_mm."""
    global _imm_mod
    try:
        sys.path.insert(0, HERE)
        import imm_quote_gaps as gaps   # noqa: E402  (mirrors $ProbeEnv)
        import incentive_mm as imm      # noqa: E402
        _imm_mod = imm
        return gaps, imm
    except Exception as e:
        log(f"! incentive_mm import failed ({e!r}); API sections off")
        return None, None


def _api_cache_path(name: str) -> str:
    return os.path.join(CACHE_DIR, f"api_{name}.pkl")


def _api_cached(name: str, ttl_min: float, force: bool, fn):
    """(value, fetched_at, error). Serves the cache inside its TTL; on a
    failed refresh serves the stale value with the error attached."""
    path = _api_cache_path(name)
    old = None
    try:
        with open(path, "rb") as f:
            old = pickle.load(f)
    except Exception:
        old = None
    if old and not force and time.time() - old["at"] < ttl_min * 60:
        return old["val"], old["at"], None
    try:
        t0 = time.time()
        val = fn()
        log(f"api {name}: refreshed in {time.time() - t0:.1f}s")
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            pickle.dump({"at": time.time(), "val": val}, f)
        os.replace(path + ".tmp", path)
        return val, time.time(), None
    except Exception as e:
        log(f"! api {name} failed: {e!r}")
        if old:
            return old["val"], old["at"], repr(e)
        return None, 0.0, repr(e)


EVENT_STARTS_CACHE = "event_starts.pkl"
_http_session = None


def _http_get_json(url: str):
    """EventStartResolver's GET on one kept-alive session. The bot's default
    is a bare requests.get: a new TLS context per call, which loads the CA
    bundle each time (~0.44 s here) -- 55 of the 62 s a programs refresh took
    once ~98 NFL ladder / escalator events needed ESPN kickoffs (2026-10-02).
    The bot's own headers (Nasdaq 403s a minimal UA), timeout and errors."""
    global _http_session
    if _http_session is None:
        import requests
        _http_session = requests.Session()
        _http_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
            "Accept": "application/json"})
    r = _http_session.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def event_start_resolver(imm):
    """incentive_mm.EventStartResolver on the shared session, with the answers
    of earlier builds: a fresh resolver per refresh re-asked ESPN for every
    sports event every 30 minutes. Entries keep the resolver's own expiry (6 h
    found, 30 min not found), and hand-set overrides are still read before
    the cache (resolve() checks them first)."""
    res = imm.EventStartResolver(http_get_json=_http_get_json)
    try:
        with open(os.path.join(CACHE_DIR, EVENT_STARTS_CACHE), "rb") as f:
            kept = pickle.load(f)
        now = time.time()
        res.cache.update({k: v for k, v in kept.items() if v[0] > now})
    except Exception:
        pass                              # no cache yet, or unreadable: ask again
    return res


def save_event_starts(res) -> None:
    now = time.time()
    keep = {k: v for k, v in res.cache.items() if v[0] > now}
    path = os.path.join(CACHE_DIR, EVENT_STARTS_CACHE)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            pickle.dump(keep, f)
        os.replace(path + ".tmp", path)
    except OSError as e:
        log(f"! event-start cache not saved: {e!r}")


def fetch_programs(gaps, imm, now_utc):
    """{event: rec} of every active liquidity program, rolled up per event
    (send_imm_new_programs.roll_up_events), with pool/day, window, target."""
    import send_imm_new_programs as snp
    client = imm.build_client()
    rows = snp.fetch_programs_raw(client)
    events, _skipped = snp.roll_up_events(rows, now_utc)
    imm.load_file_event_overrides()
    imm.load_extra_allow_series()
    try:
        imm.load_finecon_extra_series()
    except Exception:
        pass
    resolver = event_start_resolver(imm)
    state = load_json(os.path.join(STATUS_DIR, "imm_state.json"))
    quoted = set(state.get("selected_tickers") or [])
    # Kalshi's own event title, fetched once per event (GET /events/{ticker}),
    # biggest pools first, TITLE_BUDGET per refresh; kept across refreshes
    titles_path = os.path.join(CACHE_DIR, "event_titles.json")
    titles = load_json(titles_path, {})
    # events the page will show as new launches go first
    reg = load_json(os.path.join(DASH_DIR, "programs_seen.json"), {})
    recent_cut = now_utc.timestamp() - 7 * 86400
    todo = sorted((ev for ev in events if ev not in titles),
                  key=lambda ev: (reg.get(ev, now_utc.timestamp()) < recent_cut,
                                  -events[ev]["pool_total"]))
    # a cold cache fetches the new launches in one go; steady state is a handful
    todo = todo[:TITLE_BUDGET * (4 if len(titles) < 200 else 1)]
    for ev in todo:
        try:
            titles[ev] = gaps.event_title(client, ev, "")
        except Exception:
            titles[ev] = ""
    if todo:
        live = set(events)
        titles = {k: v for k, v in titles.items() if k in live}
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(titles_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(titles, f)
            os.replace(titles_path + ".tmp", titles_path)
        except OSError:
            pass
    out = {}
    for ev, rec in events.items():
        try:
            bot = snp.bot_status(rec, quoted, resolver, now_utc)
        except Exception as e:
            bot = f"? ({type(e).__name__})"
        try:
            what = gaps.describe_event(ev, titles.get(ev, ""))
        except Exception:
            what = titles.get(ev, "")
        out[ev] = {
            "event": ev, "series": rec["series"], "n": len(rec["tickers"]),
            "n_live": len(rec["live_tickers"]), "pool_day": round(rec["pool_day"], 2),
            "pool_total": round(rec["pool_total"], 2), "start": rec["start"].timestamp(),
            "end": rec["end"].timestamp(), "target": rec["target"], "bot": bot,
            "what": what, "tickers": sorted(rec["tickers"]),
        }
    save_event_starts(resolver)
    return out


def fetch_gaps(gaps, imm, now_utc, extra=None):
    """The 7:20 email's classify_and_estimate: every unquoted paying market,
    why it is not quoted, and what the bot's ladder would earn (bounded
    book reads), rolled up per event. `extra` {event: [tickers]} names
    newly launched events to estimate as well -- the email's detail budget
    goes to allowlisted markets first, so a new series outside the allowlist
    usually comes back with no estimate at all."""
    client = imm.build_client()
    bot = imm.IncentiveMarketMaker(client, live=False)
    rows, ctx = gaps.classify_and_estimate(client, bot, now_utc)
    extra_est = estimate_events(gaps, imm, bot, client, extra or {}, now_utc,
                                skip={d["event"] for d in rows if d.get("est") is not None})
    out = []
    for d in rows:
        end = d.get("end")
        out.append({"event": d["event"], "series": d["series"], "n": d["n"],
                    "n_est": d["n_est"], "pool": round(d["pool"], 2),
                    "est": (round(d["est"], 2) if d.get("est") is not None else None),
                    "yld": d.get("yld"), "partial": bool(d.get("partial")),
                    "end": end.timestamp() if end is not None else None,
                    "reason": d.get("reason") or "", "title": d.get("fallback_title") or ""})
    ctx = {k: v for k, v in ctx.items() if isinstance(v, (int, float, str, bool))}
    return {"rows": out, "ctx": ctx, "extra": extra_est}


EXTRA_PER_EVENT = int(os.environ.get("IMM_DASH_EXTRA_PER_EVENT", "3"))
EXTRA_MAX_BOOKS = int(os.environ.get("IMM_DASH_EXTRA_MAX_BOOKS", "120"))


def estimate_events(gaps, imm, bot, client, extra, now_utc, skip=frozenset()):
    """{event: {est, yld, n_est, n_tried}}: the bot's own estimator on the
    biggest-pool markets (EXTRA_PER_EVENT) of each named event, the same
    build_meta + _estimate_candidate_yield path the 7:20 email uses, bounded
    at EXTRA_MAX_BOOKS book reads. A modelled figure: it assumes the bot's
    standard ladder, full coverage, and no competitor response."""
    if not extra:
        return {}
    programs = bot.fetch_programs() or {}
    ranked = []
    for ev, tickers in extra.items():
        if ev in skip:
            continue
        tks = [t for t in tickers if t in programs and programs[t].get("dollars_per_day", 0) > 0]
        tks.sort(key=lambda t: -programs[t]["dollars_per_day"])
        if tks:
            ranked.append((-sum(programs[t]["dollars_per_day"] for t in tks), ev, tks))
    ranked.sort()                      # the biggest pools get the book budget first
    want = {ev: tks[:EXTRA_PER_EVENT] for _p, ev, tks in ranked}
    details = gaps.bulk_market_details(client, [t for v in want.values() for t in v])
    out, books = {}, 0
    for ev, tks in want.items():
        est = exp = 0.0
        n_est = 0
        for t in tks:
            if books >= EXTRA_MAX_BOOKS:
                break
            m = details.get(t)
            if not m or m.get("status") not in ("active", "open"):
                continue
            try:
                imm.ensure_family_override(imm.series_of(t))
                meta = gaps.build_meta(bot, t, programs[t], m, now_utc)
                books += 1
                if bot._estimate_candidate_yield(meta, []):
                    est += float(meta.est_dollars_per_day or 0.0)
                    x = float(getattr(meta, "est_exposure_dollars", 0.0) or 0.0) or \
                        float(getattr(meta, "est_collateral_dollars", 0.0) or 0.0)
                    exp += x
                    n_est += 1
            except Exception as e:           # one bad market must not cost the rest
                log(f"! extra estimate failed for {t}: {e!r}")
        if n_est:
            out[ev] = {"est": round(est, 2), "yld": (est / exp if exp > 0 else None),
                       "n_est": n_est, "n_tried": len(tks)}
    log(f"extra estimates: {len(out)} of {len(want)} new events, {books} book reads")
    return out


def fetch_pickoff(gaps, imm, now_utc):
    import imm_pickoff
    client = imm.build_client()
    res = imm_pickoff.scan(client, now_utc)
    rows = []
    for r in res.get("rows") or []:
        rows.append({"event": r["event"], "title": r.get("title") or "",
                     "kalshi_start": r["kalshi_start"].timestamp(),
                     "real": r["real"].timestamp(), "kind": r.get("kind"),
                     "label": r.get("label"), "source": r.get("source"),
                     "gap_h": r.get("gap_h"), "open_now": bool(r.get("open_now")),
                     "live_mkts": r.get("live_mkts")})
    return {"rows": rows, "checked": res.get("checked", 0), "error": res.get("error")}


# ----------------------------------------------------------------------------
# The model
# ----------------------------------------------------------------------------

class Builder:
    def __init__(self, now: float, api: bool, api_force: bool):
        self.now = now
        self.api = api
        self.api_force = api_force
        self.windows = build_windows(now)
        self.first_day = (datetime.fromtimestamp(now, timezone.utc).date()
                          - timedelta(days=HISTORY_DAYS)).isoformat()
        self.detail_from = now - DETAIL_DAYS * 86400
        self.warnings = []
        self.timing = {}
        self.exits = []
        self.market_lookup = None     # tests inject {ticker: market} lookups
        self.event_lookup = None      # ... and an event -> {mutually_exclusive, markets} one
        self.mrec, self.evmeta = {}, {}
        self.now_values = {}
        self.markouts_h = {}
        self.env = {}

    def _t(self, name, t0):
        self.timing[name] = round(time.time() - t0, 2)

    # ---- raw loads --------------------------------------------------------
    def load(self):
        t0 = time.time()
        self.state = load_json(os.path.join(STATUS_DIR, "imm_state.json"))
        self.status = load_json(os.path.join(STATUS_DIR, "status_incentive_mm.json"))
        self._t("state", t0)

        t0 = time.time()
        cache = FileCache("cycle")
        self.hourly = defaultdict(lambda: defaultdict(float))
        self.rest = defaultdict(lambda: defaultdict(float))
        self.cycles, self.runs = [], {}
        self.last_cycle = {}
        files = day_files("cycle_log", "csv", self.first_day)
        live_path = files[-1][1] if files else None
        for day, path in files:
            complete = file_complete(day, path, self.now)
            val = cache.get(path) if complete else None
            if val is None:
                val = parse_cycle_file(path, keep_last=(path == live_path),
                                       max_ts=None if complete else self.now)
                if complete:
                    val = dict(val, last={})
                    cache.put(path, val)
                log(f"parsed {os.path.basename(path)}"
                    f" ({os.path.getsize(path) / 1e6:.0f} MB){'' if complete else ' [live]'}")
            for t, hs in val["hourly"].items():
                d = self.hourly[t]
                for h, v in hs.items():
                    d[h] += v
            for t, hs in val["rest"].items():
                d = self.rest[t]
                for h, v in hs.items():
                    d[h] += v
            self.cycles.extend(val["cycles"])
            for rid, r in val["runs"].items():
                cur = self.runs.get(rid)
                self.runs[rid] = list(r) if cur is None else [min(cur[0], r[0]), max(cur[1], r[1]), 0]
            if path == live_path:
                self.last_cycle = val.get("last") or {}
        cache.prune({os.path.basename(p) for _d, p in files})
        cache.save()
        self._t("cycle_log", t0)

        t0 = time.time()
        self.fills = load_fills(self.first_day)
        self.realized = load_realized(self.first_day)
        self.settlements = load_settlements(self.first_day)
        self._retime_realized()
        self.toxic = load_toxic(datetime.fromtimestamp(self.now - 3 * 86400, timezone.utc)
                                .strftime("%Y-%m-%d"))
        self._t("small_sinks", t0)

        t0 = time.time()
        self._load_marks()
        self._t("marks", t0)

        t0 = time.time()
        self.guard_holds, self.guard_entered, self.guard_run = load_guards(self.now)
        self.snapshot, self.snapshot_ts = read_last_snapshot()
        self.credit_rows, self.calib = load_credits()
        self.env = launcher_env()
        self._t("misc", t0)

    def _load_marks(self):
        cache = FileCache("marks")
        # {fill id: {horizon key: [mark, snapshot ts] or None (no mark in time)}};
        # the 30-minute marks of the single-horizon cache carry over unchanged
        mk_cache_path = os.path.join(CACHE_DIR, "markouts_h.json")
        if os.path.exists(mk_cache_path):
            mk_cache = load_json(mk_cache_path, {})
        else:
            old = load_json(os.path.join(CACHE_DIR, "markouts.json"), {})
            mk_cache = {k: {"mk": v} for k, v in old.items()}
        # mark-out queries for fills that do not have one yet and are due
        queries = {}
        for f in self.fills:
            if f["pad"] or f["taker"]:
                continue
            have = mk_cache.get(f["id"]) or {}
            for hk, secs, lag in MARKOUT_HORIZONS:
                if hk in have:
                    continue
                target = f["ts"] + secs
                if target <= self.now - 60:
                    queries[f["id"] + "|" + hk] = (f["t"], target, lag)

        def store(key, v):
            fid, _, hk = key.rpartition("|")
            mk_cache.setdefault(fid, {})[hk] = v
        self.snap_totals = []            # [(ts, U, n, gross)]
        self.hour_snaps = {}             # {utc_hour: (ts, book)}
        self.last_snap = (0.0, {})
        self.gone, self.came = [], []    # position exits / entries between snapshots
        prev_last = None
        files = day_files("marks", "jsonl", self.first_day)
        live_path = files[-1][1] if files else None
        keep_names = set()
        for day, path in files:
            keep_names.add(os.path.basename(path))
            dt_day = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            keep_hours = dt_day + 86400 >= self.detail_from
            complete = file_complete(day, path, self.now)
            val = cache.get(path) if complete else None
            if val is not None and keep_hours and not val.get("hours") and val.get("totals"):
                val = None            # cached without detail: re-parse
            # queries whose target falls in (or before the end of) this file
            end = dt_day + 86400 + MARKOUT_MAX_LAG
            q = {k: v for k, v in queries.items() if dt_day - 86400 <= v[1] <= end}
            if val is not None and any(v[1] < dt_day + 86400 for v in q.values()):
                val = None            # a completed file still owes mark-outs (a new horizon)
            if val is None:
                val = parse_marks_file(path, keep_hours=True, queries=q,
                                       max_ts=None if complete else self.now)
                for k, v in q.items():
                    if isinstance(v, tuple) and v and v[0] == "ok":
                        store(k, [round(v[1], 3), round(v[2], 1)])
                        queries.pop(k, None)
                if complete:
                    # every query this completed file was the last chance for is final
                    for k, v in list(queries.items()):
                        if v[1] < dt_day + 86400 - v[2]:
                            store(k, None)
                            queries.pop(k, None)
                    stored = val if keep_hours else dict(val, hours={})
                    cache.put(path, stored)
                log(f"parsed {os.path.basename(path)}{'' if complete else ' [live]'}"
                    + (f" ({len(q)} mark-out queries)" if q else ""))
            elif not keep_hours and val.get("hours"):
                cache.put(path, dict(val, hours={}))
            self.snap_totals.extend(val["totals"])
            if keep_hours:
                self.hour_snaps.update(val["hours"])
            for m_ts, snap in (val.get("edges") or {}).items():
                self.hour_snaps.setdefault(int(m_ts // 3600), snap)
            self.gone.extend(val.get("gone") or [])
            self.came.extend(val.get("came") or [])
            first = val.get("first") or (0.0, {})
            if prev_last and prev_last[0] and first[0]:
                pa, pb = prev_last[1], first[1]
                for t, (p_, a_, mk_) in pa.items():
                    if t not in pb:
                        self.gone.append((t, p_, a_, mk_, prev_last[0], first[0]))
                for t in pb:
                    if t not in pa:
                        self.came.append((t, first[0]))
            if val["last"][0]:
                prev_last = val["last"]
            if path == live_path or val["last"][0] > self.last_snap[0]:
                if val["last"][0] >= self.last_snap[0]:
                    self.last_snap = val["last"]
        cache.prune(keep_names)
        cache.save()
        # a query no file answered (the bot was down for its whole day) is
        # final once it is well past
        for k, v in list(queries.items()):
            if v[1] + v[2] < self.now - MARKOUT_FINAL_AFTER:
                store(k, None)
        # keep the mark-out cache bounded to the fills still on the books
        live_ids = {f["id"] for f in self.fills}
        mk_cache = {k: v for k, v in mk_cache.items() if k in live_ids}
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(mk_cache_path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(mk_cache, fh)
            os.replace(mk_cache_path + ".tmp", mk_cache_path)
        except OSError:
            pass
        self.markouts_h = mk_cache
        # the 30-minute mark-outs as before: absent = pending, None = no mark
        self.markouts = {k: v["mk"] for k, v in mk_cache.items() if "mk" in v}
        self.snap_totals.sort()


    # ---- when a realized row really happened ------------------------------------
    FILL_BOOK_LAG = 5.0      # fills are polled within ~1s of a cycle's start

    def _cycle_start(self, t: float):
        """Start of the full cycle in which time `t` fell (cycle_log stamps,
        whole seconds), or None outside the logged cycles."""
        cyc = self._cycle_sorted
        i = bisect.bisect_right(cyc, t) - 1
        if i < 0 or t - cyc[i] > 120:
            return None
        return cyc[i]

    def _retime_realized(self):
        """Re-stamp each realized row with the moment its P&L entered the
        book rather than when the bot next saved state. The bot writes a
        realized row at its next save (37s after the booking on median, 2+
        min at the 90th percentile), but the 5-minute position snapshot is
        stamped at its cycle's START and written after that cycle booked its
        fills and settlements -- so a snapshot can already show a fill whose
        realized row comes minutes later, and a window ending at that
        snapshot would hold the position change without the realized P&L
        (9/30: KXAAAGASM-26OCT31-4.10 read -$5.80 for a true -$12.80).
        A fill-driven row moves to its booking time less FILL_BOOK_LAG (a
        fast-lane booking stays after the previous full cycle's snapshot); a
        settlement-driven row, booked mid-cycle, moves to just before its
        cycle's start."""
        self._cycle_sorted = sorted(set(self.cycles))
        fills_b = defaultdict(list)
        for f in self.fills:
            fills_b[f["t"]].append(f["cyc"] if f.get("cyc") else f["ts"])
        setl_b = defaultdict(list)
        for s_ in self.settlements:
            setl_b[s_["t"]].append(s_["ts"])
        for d in (fills_b, setl_b):
            for v in d.values():
                v.sort()

        def latest(lst, x):
            i = bisect.bisect_right(lst, x) - 1
            return lst[i] if i >= 0 else None
        out = []
        for ts, t, ev, d in self.realized:
            fb = latest(fills_b.get(t, []), ts + 1.0)
            sb = latest(setl_b.get(t, []), ts + 1.0)
            eff = ts
            if sb is not None and (fb is None or sb >= fb) and ts - sb < 900:
                c0 = self._cycle_start(sb)
                eff = (c0 - 0.5) if c0 is not None else sb
            elif fb is not None and ts - fb < 900:
                eff = fb - self.FILL_BOOK_LAG
            out.append((min(eff, ts), t, ev, d))
        out.sort()
        self.realized = out

    # ---- position exits the realized log does not book ------------------------
    def build_exits(self):
        """Every position that left the bot's book without the realized log
        booking its value, valued the way the account actually experienced it.

        Two sources. (1) settlement rows with result "manual_offset": the bot
        writes these when the account no longer holds a market that Kalshi
        has not settled yes/no -- which on 9/6-9/29 was, 34 times of 34, a
        SCALAR settlement (NFL fantasy-point ladders and escalators settle at
        a fractional value), not a manual trade. Since 2026-09-29 the bot
        books scalar / void itself and writes "manual_offset" only for a
        market Kalshi has not settled. (2) "vanished" positions: in
        one 5-minute snapshot and gone from every snapshot for the next
        VANISH_REAPPEAR_SECS with no settlement row within an hour and no
        closing fill (4 on 9/6-9/29: settled while the bot restarted, then
        zeroed by its startup reconcile -- e.g. two at the 9/26 14:47Z
        restart; that path settles them too since 2026-09-29). Each exit is
        resolved against Kalshi's market record: a
        market Kalshi has settled (yes / no / scalar at settlement_value; a
        void refunds cost) is booked as realized at that price; anything else
        is a transfer out at the last mark (no P&L from the last mark on).
        Without the API the transfer rule applies and the exit is flagged."""
        fills_by = defaultdict(list)
        for f in self.fills:
            fills_by[f["t"]].append(f)
        setl_by = defaultdict(list)
        for s_ in self.settlements:
            setl_by[s_["t"]].append(s_)
        came_by = defaultdict(list)
        for t, ts in self.came:
            came_by[t].append(ts)
        gone_by = defaultdict(list)
        for g in self.gone:
            gone_by[g[0]].append(g)
        exits = []
        # (1) offset rows
        for s_ in self.settlements:
            if s_["result"] != "manual_offset":
                continue
            g = [x for x in gone_by.get(s_["t"], []) if x[4] <= s_["ts"] + 60 and x[5] >= s_["ts"] - 3600]
            if g:
                g = max(g, key=lambda x: x[4])
                pos, avg, mk = g[1], g[2], g[3]
            else:
                pos, avg, mk = s_["pos"], s_["avg"], None
            c0 = self._cycle_start(s_["ts"])
            exits.append({"ts": (c0 - 0.5) if c0 is not None else s_["ts"], "t": s_["t"],
                          "pos": pos, "avg": avg,
                          "mark": mk if mk is not None else avg, "kind": "offset"})
        # (2) vanished positions
        for (t, pos, avg, mk, last_ts, gone_ts) in self.gone:
            if any(last_ts - 3600 <= x["ts"] <= gone_ts + VANISH_REAPPEAR_SECS for x in setl_by.get(t, [])):
                continue                                  # a settlement / offset row covers it
            before = [f for f in fills_by.get(t, []) if f["ts"] <= gone_ts + 60]
            if before and abs(before[-1]["pos1"]) < 0.05:
                continue                                  # closed by a fill
            if any(gone_ts <= c <= gone_ts + VANISH_REAPPEAR_SECS for c in came_by.get(t, [])):
                continue                                  # a blip: back within hours
            exits.append({"ts": gone_ts - 0.001, "t": t, "pos": pos, "avg": avg,
                          "mark": mk if mk is not None else avg, "kind": "vanish"})
        self.exits = sorted(exits, key=lambda e: e["ts"])
        self._resolve_exits()

    def _resolve_exits(self):
        """Kalshi's market record for each exit (result, settlement value and
        time), cached in DASH_DIR/cache/exit_results.json -- final results
        forever, unsettled ones re-read hourly. Then value each exit."""
        path = os.path.join(CACHE_DIR, "exit_results.json")
        cache = load_json(path, {})
        want = sorted({e["t"] for e in self.exits
                       if e["t"] not in cache or (not cache[e["t"]].get("final")
                                                  and self.now - cache[e["t"]].get("at", 0) > EXIT_RESULTS_TTL)})
        if want and self.api:
            try:
                got = (self.market_lookup or _kalshi_markets)(want)
                for t in want:
                    m = got.get(t)
                    if m is None:
                        continue
                    res = str(m.get("result") or "").lower()
                    val = m.get("settlement_value_dollars")
                    cache[t] = {"result": res, "status": m.get("status"),
                                "value": (float(val) * 100.0 if val not in (None, "") else None),
                                "settled_ts": iso_ts(m.get("settlement_ts")) or None,
                                "final": res in ("yes", "no", "scalar", "void"), "at": self.now}
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(cache, f)
                os.replace(path + ".tmp", path)
            except Exception as e:
                log(f"! exit results read failed ({e!r}); unresolved exits count as transfers")
        n_set = n_xfer = n_unres = 0
        for e in self.exits:
            c = cache.get(e["t"])
            px = None
            if c and c.get("final"):
                r = c["result"]
                if r == "yes":
                    px = 100.0
                elif r == "no":
                    px = 0.0
                elif r == "scalar" and c.get("value") is not None:
                    px = c["value"]
                elif r == "void":
                    px = e["avg"]                        # a void refunds the cost
            if px is not None:
                e["settle_px"] = px
                e["amount"] = e["pos"] * (px - e["avg"]) / 100.0
                e["realized"] = True
                e["label"] = ("settled " + (c["result"].upper() if c["result"] in ("yes", "no")
                                            else f"{c['result']} {px:.1f}c"))
                n_set += 1
            else:
                e["amount"] = e["pos"] * (e["mark"] - e["avg"]) / 100.0
                e["realized"] = False
                e["label"] = ("manual offset" if e["kind"] == "offset" else "left the book")
                if not c:
                    e["label"] += " (result unknown)"
                    n_unres += 1
                n_xfer += 1
        if self.exits:
            log(f"exits: {len(self.exits)} ({sum(1 for e in self.exits if e['kind'] == 'vanish')} vanished), "
                f"{n_set} valued at Kalshi's settlement, {n_xfer} as transfers at the last mark"
                + (f", {n_unres} unresolved" if n_unres else ""))

    def exits_in(self, a: float, b: float):
        return [e for e in self.exits if a <= e["ts"] < b]

    # ---- Kalshi's market records: strike structure and value now ----------------
    def _wide_mark(self) -> int:
        try:
            return int(_f(self.env.get("IMM_MARK_WIDE_SPREAD"), 50))
        except (TypeError, ValueError, AttributeError):
            return 50

    def fetch_market_meta(self):
        """self.mrec {ticker: rec} and self.evmeta {kalshi event: {me, n}}.

        rec = {ev, st, lo, hi, mt} (Kalshi's event ticker, strike type, floor
        and cap strikes, market type: static, kept forever) + {v, fin, at}
        (YES value now in cents by the bot's mark rule, or the settlement once
        final; re-read every MARKET_NOW_TTL until final). Cached in
        DASH_DIR/cache/markets_meta.json; at most MARKET_READ_BUDGET tickers
        are read per build, the current book first, then markets traded in
        the history, then markets held at past ET midnights. The event flag
        (mutually exclusive, number of markets) is read only for events whose
        markets carry no numeric strike, EVENT_READ_BUDGET per build."""
        path = os.path.join(CACHE_DIR, "markets_meta.json")
        epath = os.path.join(CACHE_DIR, "events_meta.json")
        rec = load_json(path, {})
        evm = load_json(epath, {})
        self.mrec, self.evmeta = rec, evm
        if not self.api:
            return
        held = set(self.last_snap[1])
        traded, last_fill = [], {}
        for f in reversed(self.fills):
            if f["t"] not in last_fill:
                last_fill[f["t"]] = f["ts"]
                traded.append(f["t"])
        past = sorted({t for _h, (_ts, book) in self.hour_snaps.items() for t in book} - held)
        settled = {s["t"] for s in self.settlements if s["result"] in ("yes", "no", "scalar", "void")}

        def stale(t):
            r = rec.get(t)
            if r is None:
                return True
            if r.get("fin"):
                return False
            # a market traded today moves the to-date mark-outs; an older one
            # only needs its value now and then
            ttl = MARKET_NOW_TTL if self.now - last_fill.get(t, 0) < 86400 else 6 * MARKET_NOW_TTL
            return self.now - _f(r.get("at")) > ttl
        want, wset = [], set()
        for group, need in ((sorted(held), lambda t: t not in rec or "st" not in rec[t]),
                            (traded, lambda t: t not in held and t not in settled and stale(t)),
                            (past, lambda t: t not in rec)):
            for t in group:
                if t not in wset and need(t):
                    wset.add(t)
                    want.append(t)
        want = want[:MARKET_READ_BUDGET]
        wide = self._wide_mark()
        changed = False
        if want:
            try:
                got = (self.market_lookup or _kalshi_markets)(want)
                for t in want:
                    m = got.get(t)
                    if m is None:
                        rec[t] = dict(rec.get(t) or {}, at=self.now, miss=1)
                        continue
                    v, fin = market_value_cents(m, wide)
                    rec[t] = {"ev": m.get("event_ticker") or event_of(t), "st": m.get("strike_type"),
                              "lo": m.get("floor_strike"), "hi": m.get("cap_strike"),
                              "mt": m.get("market_type") or "binary", "v": v, "fin": fin,
                              "at": self.now}
                changed = True
                log(f"market records: read {len(want)} (cache {len(rec)})")
            except Exception as e:
                log(f"! market records read failed ({e!r}); worst cases and to-date marks use the cache")
        # word / name events: is exactly one market paid?
        need_ev = {}
        for t in held | {t for _h, (_ts, b) in self.hour_snaps.items() for t in b}:
            r = rec.get(t)
            if not r or "st" not in r:
                continue
            if r.get("st") in NUMERIC_STRIKES and (r.get("lo") is not None or r.get("hi") is not None):
                continue
            ev = r.get("ev")
            if ev and ev not in evm:
                need_ev[ev] = need_ev.get(ev, 0) + (2 if t in held else 1)
        todo = sorted(need_ev, key=lambda e: -need_ev[e])[:EVENT_READ_BUDGET]
        if todo:
            n_ok = 0
            for ev in todo:
                try:
                    js = (self.event_lookup or _kalshi_event)(ev)
                except Exception as e:
                    log(f"! event read {ev} failed ({e!r})")
                    continue
                if not js:
                    evm[ev] = {"me": None, "n": None, "at": self.now}
                    continue
                evm[ev] = {"me": bool(js.get("mutually_exclusive")),
                           "n": len(js.get("markets") or []) or None, "at": self.now}
                n_ok += 1
            changed = True
            log(f"event records: read {n_ok} of {len(todo)}")
        if changed:
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                for p_, obj in ((path, rec), (epath, evm)):
                    with open(p_ + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(obj, f)
                    os.replace(p_ + ".tmp", p_)
            except OSError:
                pass

    def struct_of(self, t: str):
        """(strike type, floor, cap, market type) or None (not read yet)."""
        r = (getattr(self, "mrec", None) or {}).get(t)
        if not r or "st" not in r:
            return None
        return (r.get("st"), _f(r.get("lo"), None), _f(r.get("hi"), None), r.get("mt") or "binary")

    def _struct_cover(self, held):
        """How much of the current book the worst case could net: positions
        whose strike structure has been read, of all held."""
        return {"held": len(held), "known": sum(1 for t, _p, _m in held if self.struct_of(t) is not None)}

    def build_now_values(self):
        """self.now_values {ticker: (YES value cents, source)} for every
        market with a maker fill: what the fill's position is worth today --
        Kalshi's settlement when it has settled (the bot's settlement rows, the
        exits resolved against Kalshi, the market record), else the bot's own
        mark while it holds the market, else the external mid it quotes
        against, else the market record's touch (the bot's mark rule)."""
        out = {}
        for s_ in self.settlements:
            if s_["result"] in ("yes", "no"):
                out[s_["t"]] = (100.0 if s_["result"] == "yes" else 0.0, "settled")
            elif s_["result"] == "scalar" and s_["px"] is not None:
                out[s_["t"]] = (float(s_["px"]), "settled")
        for e in self.exits:
            if e.get("realized") and e.get("settle_px") is not None and e["t"] not in out:
                out[e["t"]] = (float(e["settle_px"]), "settled")
        rec = getattr(self, "mrec", None) or {}
        traded = {f["t"] for f in self.fills}
        book = self.last_snap[1]
        for t in traded:
            if t in out:
                continue
            r = rec.get(t) or {}
            if r.get("fin") and r.get("v") is not None:
                out[t] = (float(r["v"]), "settled")
                continue
            b = book.get(t)
            if b and b[2] is not None:
                out[t] = (float(b[2]), "held")
                continue
            c = self.last_cycle.get(t)
            if c:
                xb, xa = _f(c.get("ext_bid"), None), _f(c.get("ext_ask"), None)
                if xb is not None and xa is not None and 0 < xb < xa < 100:
                    out[t] = ((xb + xa) / 2.0, "quoted")
                    continue
            if r.get("v") is not None:
                out[t] = (float(r["v"]), "market")
        self.now_values = out

    # ---- snapshot helpers -------------------------------------------------
    def snap_at(self, t: float):
        """(ts, book) of the first snapshot at or after t -- or the newest
        snapshot when t is past it. The hourly index holds the first snapshot
        of each UTC hour, so an hour-aligned t is exact; when the bot was down
        at t the next snapshot within 6 hours stands in, else the last one
        before t (the window edge is then approximate and flagged)."""
        if self.last_snap[0] and t >= self.last_snap[0]:
            return self.last_snap
        h = int(t // 3600)
        for hh in range(h, h + 7):
            s = self.hour_snaps.get(hh)
            if s and s[0] >= t:
                return s
        for hh in range(h - 1, h - 25, -1):
            s = self.hour_snaps.get(hh)
            if s:
                self.warnings.append(
                    "no position snapshot within 6h after "
                    + datetime.fromtimestamp(t, ET).strftime("%b %d %I:%M %p ET")
                    + " (bot down?) -- that window edge uses an earlier snapshot")
                return s
        return None

    @staticmethod
    def book_u(book) -> dict:
        return {t: (pos * (mk - avg) / 100.0 if mk is not None else 0.0)
                for t, (pos, avg, mk) in book.items()}

    def total_u_at(self, t: float):
        """(snap_ts, total unrealized) of the first snapshot at/after t."""
        if not self.snap_totals:
            return None
        lo, hi = 0, len(self.snap_totals)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.snap_totals[mid][0] < t:
                lo = mid + 1
            else:
                hi = mid
        if lo >= len(self.snap_totals):
            lo = len(self.snap_totals) - 1
        return self.snap_totals[lo][0], self.snap_totals[lo][1]

    # ---- per-window market aggregation -------------------------------------
    def _last_held_mark(self, t: str, e0: float, e1: float):
        """The mark of the last snapshot that held `t` before it left the
        book inside [e0, e1) (its cost when that snapshot had no mark), or
        None when it did not leave in the window."""
        idx = getattr(self, "_gone_by", None)
        if idx is None:
            idx = defaultdict(list)
            for g in self.gone:
                idx[g[0]].append(g)
            for v in idx.values():
                v.sort(key=lambda g: g[4])
            self._gone_by = idx
        best = None
        for g in idx.get(t, ()):
            if e0 <= g[4] < e1 and g[5] <= e1:
                best = g
        if best is None:
            return None
        return best[3] if best[3] is not None else best[2]

    def window_markets(self, w0: float, w1: float, sums=None) -> dict:
        """{ticker: metrics} for one window. `sums` = ({ticker: rewards},
        {ticker: resting $-seconds}) precomputed for the window (the per-day
        pass buckets every market-hour once instead of once per day).

        Trading P&L is split three ways (the parts add up to "pnl" exactly):
          cr  the position held at the window's start, re-marked from its
              start mark to its end mark (or to the last mark before it left
              the book) -- inventory carried in
          cs  the same start position, from its start mark to Kalshi's
              settlement, for a market that settled in the window
          (new fills = pnl - cr - cs: the window's own fills, marked to the
              window's end or to the settlement)
        Every maker fill (not a pad, not a taker) is also marked out against
        its arrival book (fe: edge vs the external mid the cycle before the
        fill) and at each horizon: m5, mk (30 min), m4 (4 h), mt (to date:
        the market's value now, or its settlement)."""
        h0, h1 = int(w0 // 3600), int(math.ceil(w1 / 3600.0))
        m = defaultdict(lambda: {"rew": 0.0, "rest_s": 0.0, "real": 0.0, "u0": 0.0,
                                 "u1": 0.0, "xfer": 0.0, "fills": 0, "cts": 0.0,
                                 "usd": 0.0, "mk": 0.0, "mk_n": 0, "mk_cts": 0.0,
                                 "buy": 0.0, "sell": 0.0, "settled": None,
                                 "fe": 0.0, "fe_cts": 0.0, "m5": 0.0, "m5_cts": 0.0,
                                 "m4": 0.0, "m4_cts": 0.0, "mt": 0.0, "mt_cts": 0.0})
        if sums is not None:
            for t, v in sums[0].items():
                m[t]["rew"] += v
            for t, v in sums[1].items():
                m[t]["rest_s"] += v
        else:
            for t, hs in self.hourly.items():
                s_ = 0.0
                for h, v in hs.items():
                    if h0 <= h < h1:
                        s_ += v
                if s_:
                    m[t]["rew"] += s_
            for t, hs in self.rest.items():
                s_ = 0.0
                for h, v in hs.items():
                    if h0 <= h < h1:
                        s_ += v
                if s_:
                    m[t]["rest_s"] += s_
        s0 = self.snap_at(w0)
        s1 = self.snap_at(w1)
        e0 = s0[0] if s0 else w0
        e1 = s1[0] if s1 else w1
        pnl_ok = s0 is not None and s1 is not None
        if s0:
            for t, u in self.book_u(s0[1]).items():
                m[t]["u0"] = u
                m[t]["pos0"] = s0[1][t][0]
                m[t]["mk0"] = s0[1][t][2]
                m[t]["avg0"] = s0[1][t][1]
        if s1:
            for t, u in self.book_u(s1[1]).items():
                m[t]["u1"] = u
                p, a, mk = s1[1][t]
                m[t]["pos1"], m[t]["avg1"], m[t]["mk1"] = p, a, mk
        for ts, t, _ev, d in self.realized:
            if e0 <= ts < e1:
                m[t]["real"] += d
        for s in self.settlements:
            if not (e0 <= s["ts"] < e1):
                continue
            if s["result"] in ("yes", "no"):
                m[s["t"]]["settled"] = f"settled {str(s['result']).upper()}"
                m[s["t"]]["_sv"] = 100.0 if s["result"] == "yes" else 0.0
            elif s["result"] in ("scalar", "void") and s["px"] is not None:
                # booked by the bot itself since 2026-09-29 (its realized
                # delta is already in "real"); labelled as build_exits labels
                # the older offset rows it resolves
                m[s["t"]]["settled"] = f"settled {s['result']} {float(s['px']):.1f}c"
                m[s["t"]]["_sv"] = float(s["px"])
        for e in self.exits_in(e0, e1):
            mm = m[e["t"]]
            if e["realized"]:
                mm["real"] += e["amount"]            # a settlement the bot did not book
                mm["_sv"] = e["settle_px"]
            else:
                mm["xfer"] += e["amount"]            # out at its last mark
                mm["_xm"] = e["mark"]
            mm["settled"] = e["label"]
        mkh = getattr(self, "markouts_h", None) or {}
        nowv = getattr(self, "now_values", None) or {}
        for f in self.fills:
            if not (w0 <= f["ts"] < w1):
                continue
            mm = m[f["t"]]
            mm["fills"] += 1
            mm["cts"] += f["n"]
            cost = f["px"] if f["side"] == "bid" else 100.0 - f["px"]
            mm["usd"] += f["n"] * cost / 100.0
            if f["side"] == "bid":
                mm["buy"] += f["n"]
            else:
                mm["sell"] += f["n"]
            d = 1.0 if f["side"] == "bid" else -1.0
            mo = self.markouts.get(f["id"])
            if mo:
                mm["mk"] += d * (mo[0] - f["px"]) * f["n"] / 100.0
                mm["mk_n"] += 1
                mm["mk_cts"] += f["n"]
            if f["pad"] or f["taker"]:
                continue
            if f.get("mid") is not None:
                mm["fe"] += d * (f["mid"] - f["px"]) * f["n"] / 100.0
                mm["fe_cts"] += f["n"]
            h = mkh.get(f["id"]) or {}
            for hk in ("m5", "m4"):
                mo = h.get(hk)
                if mo:
                    mm[hk] += d * (mo[0] - f["px"]) * f["n"] / 100.0
                    mm[hk + "_cts"] += f["n"]
            nv = nowv.get(f["t"])
            if nv is not None:
                mm["mt"] += d * (nv[0] - f["px"]) * f["n"] / 100.0
                mm["mt_cts"] += f["n"]
        out = {}
        span = max(w1 - w0, 1.0)
        for t, v in m.items():
            v["pnl"] = v["real"] + v["u1"] - v["u0"] + v["xfer"]
            v["net"] = v["rew"] + v["pnl"]
            v["rest_avg"] = v["rest_s"] / span
            # the start position's share of the P&L: carried in, re-marked to
            # the window's end (or to its last mark before leaving the book),
            # or settled by Kalshi; the rest is the window's own fills
            v["cr"] = v["cs"] = 0.0
            p0 = v.get("pos0", 0.0)
            if abs(p0) > 1e-9:
                m0 = v["mk0"] if v.get("mk0") is not None else v.get("avg0", 0.0)
                if v.get("_sv") is not None:
                    v["cs"] = p0 * (v["_sv"] - m0) / 100.0
                else:
                    if "pos1" in v:
                        end = v["mk1"] if v.get("mk1") is not None else v.get("avg1", m0)
                    elif v.get("_xm") is not None:
                        end = v["_xm"]
                    else:
                        end = self._last_held_mark(t, e0, e1)
                        end = m0 if end is None else end
                    v["cr"] = p0 * (end - m0) / 100.0
            if (abs(v["rew"]) < 1e-4 and abs(v["pnl"]) < 1e-4 and not v["fills"]
                    and abs(v.get("pos1", 0.0)) < 1e-9 and abs(v.get("pos0", 0.0)) < 1e-9
                    and v["rest_s"] <= 0 and not v["settled"]):
                continue
            out[t] = v
        return out, (e0, e1, pnl_ok)

    def day_list(self):
        """[(iso day, start, end)] for every ET day the cycle history covers
        in full, oldest first; today ends at `now`."""
        today = datetime.fromtimestamp(self.now, ET).date()
        first_cycle = min(self.cycles) if self.cycles else self.now
        out = []
        for i in range(HISTORY_DAYS, -1, -1):
            d = today - timedelta(days=i)
            a = et_midnight(d)
            b = min(et_midnight(d + timedelta(days=1)), self.now)
            if a < first_cycle - 3600 or b <= a:
                continue
            out.append((d.isoformat(), a, b))
        return out

    def daily_markets(self):
        """{iso day: {"s", "e", "ok", "m": {ticker: [DAY_FIELDS...]}, "c": curve}}
        -- every history day computed exactly as a window, so a past day on
        the page reads the same as "Yesterday" did on the day after."""
        days = self.day_list()
        hour_day = {}
        for iso, a, b in days:
            for h in range(int(a // 3600), int(math.ceil(b / 3600.0))):
                hour_day[h] = iso
        rew = defaultdict(lambda: defaultdict(float))
        rest = defaultdict(lambda: defaultdict(float))
        for src, dst in ((self.hourly, rew), (self.rest, rest)):
            for t, hs in src.items():
                for h, v in hs.items():
                    k = hour_day.get(h)
                    if k is not None:
                        dst[k][t] += v
        out = {}
        for iso, a, b in days:
            mk, (e0, e1, ok) = self.window_markets(a, b, sums=(rew.get(iso, {}), rest.get(iso, {})))
            span = max(b - a, 1.0)
            rows = {}
            for t, v in mk.items():
                if not (abs(v["rew"]) >= 0.0005 or abs(v["pnl"]) >= 0.0005 or v["fills"] or v["settled"]):
                    continue
                rows[t] = [round(v["rew"], 4), round(v["pnl"], 4), round(v["real"], 4),
                           round(v["u1"] - v["u0"], 4), round(v["xfer"], 4), v["fills"],
                           round(v["cts"], 1), round(v["usd"], 2), round(v["mk"], 3), v["mk_n"],
                           round(v["mk_cts"], 1), round(v["rest_s"] / span, 2),
                           (round(v["pos0"], 2) if "pos0" in v else None),
                           (round(v["pos1"], 2) if "pos1" in v else None),
                           v.get("mk0"), v.get("mk1"), v["settled"] or 0,
                           round(v["cr"], 4), round(v["cs"], 4), fx_array(v)]
            out[iso] = {"s": a, "e": b, "ok": bool(ok), "e0": e0, "e1": e1, "m": rows,
                        "c": self.curve(a, b, DAY_CURVE_STEP) if ok else
                        [[p[0], p[1], None] for p in self.curve_rewards_only(a, b, DAY_CURVE_STEP)]}
            if ok:
                # the book as the day ended: worst case per event at the end
                # marks, and how many markets rested quotes that day
                held = [(t, v["pos1"], v["mk1"] if v.get("mk1") is not None else v.get("avg1"))
                        for t, v in mk.items() if abs(v.get("pos1", 0.0)) > 1e-9]
                out[iso]["wc"] = self.worst_by_event(held)
                out[iso]["nq"] = sum(1 for v in mk.values() if v["rest_s"] > 0)
        return out

    def worst_by_event(self, held):
        """{dashboard event: [worst, gross, method]} for positions
        [(ticker, pos, mark)] -- event_worst_case per Kalshi event, summed
        into the page's event (event_of) when the two ever differ."""
        groups = defaultdict(list)
        for t, pos, mk in held:
            r = (getattr(self, "mrec", None) or {}).get(t) or {}
            groups[(event_of(t), r.get("ev") or event_of(t))].append((t, pos, mk))
        out = {}
        for (ev, kev), hold in groups.items():
            struct = {t: self.struct_of(t) for t, _p, _m in hold}
            meta = (getattr(self, "evmeta", None) or {}).get(kev) or {}
            w, g, how = event_worst_case(hold, {t: s for t, s in struct.items() if s},
                                         meta.get("me"), meta.get("n"))
            o = out.setdefault(ev, [0.0, 0.0, how])
            o[0] = round(o[0] + w, 2)
            o[1] = round(o[1] + g, 2)
            if o[2] != how:
                o[2] = "mixed"
        return out

    def curve_rewards_only(self, w0: float, w1: float, step: float):
        """[[ts, cum modeled rewards]] for a day the position log does not cover."""
        rew_h = self.rew_by_hour()
        pts, t = [], w0
        h0 = int(w0 // 3600)
        while True:
            tt = min(t, w1)
            h_end = int(tt // 3600)
            r = sum(rew_h.get(h, 0.0) for h in range(h0, h_end))
            pts.append([round(tt), round(r, 2)])
            if tt >= w1:
                break
            t += step
        return pts

    def _last_u_before(self, t: str, ts: float) -> float:
        best = None
        for h in range(int(ts // 3600), int(ts // 3600) - 30, -1):
            s = self.hour_snaps.get(h)
            if s and s[0] <= ts and t in s[1]:
                best = s
                break
        if not best:
            return 0.0
        pos, avg, mk = best[1][t]
        return pos * (mk - avg) / 100.0 if mk is not None else 0.0

    # ---- curves --------------------------------------------------------------
    def rew_by_hour(self):
        if getattr(self, "_rew_h", None) is None:
            rh = defaultdict(float)
            for hs in self.hourly.values():
                for h, v in hs.items():
                    rh[h] += v
            self._rew_h = rh
        return self._rew_h

    def curve(self, w0: float, w1: float, step: float):
        """[[ts, cum modeled rewards, cum trading P&L]] every `step` seconds
        from w0 to w1. P&L at each point uses the first 5-minute snapshot at
        or after it (same rule as the window totals, so the last point equals
        the window's trading P&L); rewards interpolate linearly inside the
        hour the point falls in."""
        if not self.snap_totals:
            return []
        rew_h = self.rew_by_hour()
        base = self.total_u_at(w0)
        if base is None:
            return []
        e0, u0 = base
        events = [(ts, d) for ts, _t, _e, d in self.realized if ts >= e0]
        events += [(e["ts"], e["amount"]) for e in self.exits if e["ts"] >= e0]
        events.sort()
        h0 = int(w0 // 3600)
        pts, ei, cum_real = [], 0, 0.0
        rew_full, h_done = 0.0, h0
        t = w0
        while True:
            tt = min(t, w1)
            if tt >= self.snap_totals[-1][0]:
                et, ut = self.snap_totals[-1][0], self.snap_totals[-1][1]
            else:
                et, ut = self.total_u_at(tt)
            while ei < len(events) and events[ei][0] < et:
                cum_real += events[ei][1]
                ei += 1
            h_end = int(tt // 3600)
            while h_done < h_end:
                rew_full += rew_h.get(h_done, 0.0)
                h_done += 1
            rew = rew_full
            if tt > h_end * 3600:
                hour_len = max(1.0, min(self.now, (h_end + 1) * 3600.0) - h_end * 3600.0)
                rew += rew_h.get(h_end, 0.0) * min(1.0, (tt - h_end * 3600.0) / hour_len)
            pts.append([round(tt), round(rew, 2), round(cum_real + ut - u0, 2)])
            if tt >= w1:
                break
            t += step
        return pts

    def family_curves(self, w0: float, w1: float, fam_of, step: float = FAMILY_CURVE_STEP):
        """{"x": [ts], "f": {family: {"r": [cum rewards], "p": [cum trading]}}}
        every `step` through the window, for the per-family sparklines.

        A point takes the first 5-minute snapshot of its hour (the hourly
        index), the realized P&L and exits booked before that snapshot, and
        the rewards of the hours before it; the first and last points use the
        window's own edge snapshots, so each family's last point equals its
        window total. None when the hourly snapshot detail (the last
        DETAIL_DAYS) does not cover the window."""
        if w0 < self.detail_from:
            return None
        s0, s1 = self.snap_at(w0), self.snap_at(w1)
        if not s0 or not s1:
            return None
        xs, books = [w0], [s0]
        t = (int(w0 // step) + 1) * step
        while t < w1 - 60:
            s = self.hour_snaps.get(int(t // 3600))
            xs.append(t)
            books.append(s if (s and t <= s[0] <= t + 3600) else None)
            t += step
        xs.append(w1)
        books.append(s1)
        fam_cache = {}

        def fam(tk):
            f = fam_cache.get(tk)
            if f is None:
                f = fam_cache[tk] = fam_of(tk)
            return f

        rew_h = getattr(self, "_rew_fam_h", None)
        if rew_h is None:
            rew_h = defaultdict(lambda: defaultdict(float))
            for tk, hs in self.hourly.items():
                ff = fam(tk)
                for h, v in hs.items():
                    rew_h[ff][h] += v
            self._rew_fam_h = rew_h
        events = [(ts, fam(tk), d) for ts, tk, _e, d in self.realized if ts >= s0[0]]
        events += [(e["ts"], fam(e["t"]), e["amount"]) for e in self.exits if e["ts"] >= s0[0]]
        events.sort()
        u0 = defaultdict(float)
        for tk, u in self.book_u(s0[1]).items():
            u0[fam(tk)] += u
        out = defaultdict(lambda: {"r": [], "p": []})
        cum, ei, last_u = defaultdict(float), 0, dict(u0)
        h0, h_last = int(w0 // 3600), int(math.ceil(w1 / 3600.0))
        fams = set(rew_h) | set(u0) | {f for _ts, f, _d in events}
        hv = {f_: sorted((h, v) for h, v in (rew_h.get(f_) or {}).items() if h0 <= h < h_last)
              for f_ in fams}
        racc, rptr = defaultdict(float), defaultdict(int)
        for k, (x, snap) in enumerate(zip(xs, books)):
            edge = snap[0] if snap else None
            if snap is not None:
                uk = defaultdict(float)
                for tk, u in self.book_u(snap[1]).items():
                    uk[fam(tk)] += u
                last_u = uk
            stop = edge if edge is not None else x
            while ei < len(events) and events[ei][0] < stop:
                cum[events[ei][1]] += events[ei][2]
                ei += 1
            h_end = h_last if k == len(xs) - 1 else int(x // 3600)
            for f_ in fams:
                lst = hv[f_]
                while rptr[f_] < len(lst) and lst[rptr[f_]][0] < h_end:
                    racc[f_] += lst[rptr[f_]][1]
                    rptr[f_] += 1
                out[f_]["r"].append(round(racc[f_], 2))
                out[f_]["p"].append(round(cum[f_] + last_u.get(f_, 0.0) - u0.get(f_, 0.0), 2))
        keep = {f_: v for f_, v in out.items() if any(v["r"]) or any(v["p"])}
        return {"x": [round(x) for x in xs], "f": keep}

    def load_changes(self, since: float):
        """Code deploys and launcher-config changes since `since` (parse_changes
        over config_history_*.jsonl), each with the commits it brought (git log
        prev..sha, cached) and the families its subject / knobs name."""
        rows = []
        first = datetime.fromtimestamp(since - 3 * 86400, timezone.utc).strftime("%Y-%m-%d")
        for _d, p in day_files("config_history", "jsonl", first):
            rows.extend(iter_jsonl(p))
        ch = [c for c in parse_changes(rows) if c["ts"] >= since]
        subj = self._commit_subjects({(c["prev"], c["sha"]) for c in ch
                                      if c["sha"] and c["sha"] != c["prev"]})
        for c in ch:
            c["subj"] = (subj.get(f"{c['prev']}..{c['sha']}") or []) if c["sha"] != c["prev"] else []
            c["fams"] = change_families(" ".join(imm_subjects(c["subj"]) + c["keys"]))
        return ch

    def _commit_subjects(self, pairs):
        """{"prev..sha": ["<sha> <subject>", ...]} via `git log`, cached in
        DASH_DIR/cache/commit_subjects.json (a range git cannot resolve is not
        cached, so it is retried after the next fetch)."""
        path = os.path.join(CACHE_DIR, "commit_subjects.json")
        cache = load_json(path, {})
        changed = False
        for prev, sha in sorted(pairs):
            key = f"{prev}..{sha}"
            if key in cache:
                continue
            rng = [key] if prev else ["-1", sha]
            try:
                res = subprocess.run(["git", "-C", HERE, "log", "--format=%h %s", "-n", "12"] + rng,
                                     capture_output=True, text=True, timeout=15,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                continue
            if res.returncode != 0:
                continue
            cache[key] = [ln.strip()[:140] for ln in res.stdout.splitlines() if ln.strip()][:8]
            changed = True
        if changed:
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(cache, f)
                os.replace(path + ".tmp", path)
            except OSError:
                pass
        return cache

    # ---- daily history --------------------------------------------------------
    def daily_history(self):
        today = datetime.fromtimestamp(self.now, ET).date()
        first_marks = self.snap_totals[0][0] if self.snap_totals else self.now
        first_cycle = min(self.cycles) if self.cycles else self.now
        rew_h = self.rew_by_hour()
        credited = {}
        by_date = (self.calib or {}).get("credited_by_date_imm") or {}
        if by_date:
            credited = {k: _f(v) for k, v in by_date.items()}
        else:
            for d, _e, a in self.credit_rows:
                credited[d] = credited.get(d, 0.0) + a
        # credits beyond the calibration snapshot come straight from the ledger
        cal_max = max(credited) if credited else ""
        for d, _e, a in self.credit_rows:
            if d > cal_max:
                credited[d] = credited.get(d, 0.0) + a
        ledger_max = max((d for d, _e, _a in self.credit_rows), default="")
        out = []
        for i in range(HISTORY_DAYS, -1, -1):
            d = today - timedelta(days=i)
            a, b = et_midnight(d), et_midnight(d + timedelta(days=1))
            b = min(b, self.now)
            if b <= first_cycle or a < first_cycle - 3600:
                continue          # the cycle history does not cover the whole day
            ha, hb = int(a // 3600), int(math.ceil(b / 3600.0))
            rew = sum(v for h, v in rew_h.items() if ha <= h < hb)
            row = {"d": d.isoformat(), "rew": round(rew, 2),
                   "cred": (round(credited.get(d.isoformat(), 0.0), 2)
                            if ledger_max and d.isoformat() <= ledger_max else None)}
            if self.snap_totals and a >= first_marks - 3600:
                u0 = self.total_u_at(a)
                u1 = (self.total_u_at(b) if b < self.snap_totals[-1][0]
                      else (self.snap_totals[-1][0], self.snap_totals[-1][1]))
                if u0 and u1 and u1[0] >= u0[0]:
                    real = sum(dd for ts, _t, _e, dd in self.realized if u0[0] <= ts < u1[0])
                    ex = self.exits_in(u0[0], u1[0])
                    real += sum(e["amount"] for e in ex if e["realized"])
                    xf = sum(e["amount"] for e in ex if not e["realized"])
                    pnl = real + xf + u1[1] - u0[1]
                    row["pnl"] = round(pnl, 2)
                    row["real"] = round(real, 2)
                    row["net"] = round(rew + pnl, 2)     # from the unrounded parts
            day_fills = [f for f in self.fills if a <= f["ts"] < b]
            row["fills"] = len(day_fills)
            row["cts"] = round(sum(f["n"] for f in day_fills), 1)
            out.append(row)
        return out, ledger_max

    # ---- assemble ---------------------------------------------------------------
    def build(self):
        self.load()
        t0 = time.time()
        self.build_exits()
        st, stat = self.state, self.status
        selected = set(st.get("selected_tickers") or [])
        scan_members = set(st.get("scan_members") or [])
        own_pos = {t: _f(v) for t, v in (st.get("own_pos") or {}).items() if abs(_f(v)) > 1e-9}
        own_avg = {t: _f(v) for t, v in (st.get("own_avg") or {}).items()}
        accrued = {t: _f(v) for t, v in (st.get("accrued_est") or {}).items()}

        # credits per event
        cred_ev, cred_ev7 = defaultdict(float), defaultdict(float)
        d7 = (datetime.fromtimestamp(self.now, ET).date() - timedelta(days=7)).isoformat()
        for d, ev, a in self.credit_rows:
            cred_ev[ev] += a
            if d >= d7:
                cred_ev7[ev] += a

        # Kalshi's market records (strike structure, value now): the worst
        # cases and the to-date mark-outs need them before any window is cut
        t1 = time.time()
        self.fetch_market_meta()
        self.build_now_values()
        self._t("market_meta", t1)

        # per window
        wins = {}
        market_union = set()
        for key, w in self.windows.items():
            mk, edges = self.window_markets(w["start"], w["end"])
            wins[key] = (mk, edges)
            market_union |= set(mk)
        # current quoting state
        cur = {}
        for t, r in self.last_cycle.items():
            bct, act = _f(r.get("own_bid_ct")), _f(r.get("own_ask_ct"))
            pb, pa = _f(r.get("own_pad_bid_ct")), _f(r.get("own_pad_ask_ct"))
            real_b, real_a = max(bct - pb, 0.0), max(act - pa, 0.0)
            sides = (1 if real_b > 0 else 0) + (1 if real_a > 0 else 0)
            cur[t] = {
                "q": sides, "bid_ct": round(real_b, 1), "ask_ct": round(real_a, 1),
                "bid_top": _f(r.get("own_bid_top"), None), "ask_top": _f(r.get("own_ask_top"), None),
                "xb": _f(r.get("ext_bid"), None), "xa": _f(r.get("ext_ask"), None),
                "est_day": round(_f(r.get("est_frac")) * _f(r.get("pool_per_day")), 4),
                "pool_day": round(_f(r.get("pool_per_day")), 2),
                "usd": round(_f(r.get("_resting_usd")), 2),
                "scan": r.get("is_scan") == "1", "ro": r.get("reduce_only") == "1",
                "hm": _f(r.get("hour_mult"), 1.0),
            }
        cycle_ts = max((r.get("_ts") or 0) for r in self.last_cycle.values()) if self.last_cycle else 0

        # toxic + other halts
        halts = self._halts(selected, cur)
        market_union |= set(cur) | set(own_pos) | set(self.guard_holds) | set(halts["by_market"])
        # every history day, drillable on the page
        days = self.daily_markets()
        for dd in days.values():
            market_union |= set(dd["m"])

        # snapshot candidates (why not quoted)
        snap_by_t = {}
        for r in self.snapshot:
            t = r.get("ticker")
            if t:
                snap_by_t[t] = r

        cats = {k: str((v or {}).get("category") or "")
                for k, v in (st.get("scan_series_meta") or {}).items()}
        markets = {}
        for t in market_union:
            ser = series_of(t)
            fam, grp = family_of(ser, cats.get(ser, ""))
            rec = {"t": t, "ev": event_of(t), "ser": ser, "fam": fam, "grp": grp,
                   "sel": t in selected, "scanm": t in scan_members,
                   "pos": round(own_pos.get(t, 0.0), 2), "avg": round(own_avg.get(t, 0.0), 2),
                   "acc": round(accrued.get(t, 0.0), 3), "w": {}}
            lm = self.last_snap[1].get(t)
            if lm:
                rec["mark"] = lm[2]
                rec["u"] = round(lm[0] * (lm[2] - lm[1]) / 100.0, 2) if lm[2] is not None else 0.0
                rec["risk"] = round((lm[0] * lm[1] if lm[0] > 0 else -lm[0] * (100 - lm[1])) / 100.0, 2)
            if t in cur:
                rec["cur"] = cur[t]
            g = self.guard_holds.get(t)
            if g:
                rec["guard"] = g["guard"]
                rec["guard_since"] = g["since"]
            h = halts["by_market"].get(t)
            if h:
                rec["halt"] = h
            sr = snap_by_t.get(t)
            if sr:
                rec["dec"] = sr.get("decision")
                rec["cutoff"] = iso_ts(sr.get("cutoff")) or None
                rec["pend"] = iso_ts(sr.get("program_end")) or None
                if sr.get("decision") == "selected" and sr.get("banked") is not None:
                    # this program period's modeled accrual, and whether the
                    # bot projects it to clear Kalshi's $1 per-market floor
                    rec["fl"] = [round(_f(sr.get("banked")), 3), 1 if sr.get("reaches_min") else 0,
                                 round(_f(sr.get("projected_total")), 3)]
            for key, (mk, _e) in wins.items():
                if key == "roll":
                    continue
                v = mk.get(t)
                if not v:
                    continue
                o = {"rew": round(v["rew"], 4), "pnl": round(v["pnl"], 4),
                     "net": round(v["net"], 4), "real": round(v["real"], 4),
                     "du": round(v["u1"] - v["u0"], 4)}
                if v["fills"]:
                    o.update(fills=v["fills"], cts=round(v["cts"], 1), usd=round(v["usd"], 2),
                             buy=round(v["buy"], 1), sell=round(v["sell"], 1))
                    if v["mk_n"]:
                        o.update(mk=round(v["mk"], 3), mk_n=v["mk_n"], mk_cts=round(v["mk_cts"], 1))
                    for k in FX_KEYS:
                        if v[k]:
                            o[k] = round(v[k], 1 if k.endswith("_cts") else 3)
                if abs(v["cr"]) > 1e-6:
                    o["cr"] = round(v["cr"], 4)
                if abs(v["cs"]) > 1e-6:
                    o["cs"] = round(v["cs"], 4)
                if v["rest_avg"] > 0.005:
                    o["rest"] = round(v["rest_avg"], 2)
                if "mk0" in v:
                    o["mk0"] = v["mk0"]
                    o["pos0"] = round(v.get("pos0", 0.0), 2)
                if "mk1" in v:
                    o["mk1"] = v["mk1"]
                if "pos1" in v:
                    o["pos1"] = round(v["pos1"], 2)
                if v["settled"]:
                    o["settled"] = v["settled"]
                if abs(v["xfer"]) > 1e-6:
                    o["xfer"] = round(v["xfer"], 3)
                rec["w"][key] = o
            markets[t] = rec

        # events
        events = {}
        for t, rec in markets.items():
            ev = rec["ev"]
            e = events.get(ev)
            if e is None:
                e = events[ev] = {"ev": ev, "fam": rec["fam"], "grp": rec["grp"], "ser": rec["ser"],
                                  "mkts": [], "cred": round(cred_ev.get(ev, 0.0), 2),
                                  "cred7": round(cred_ev7.get(ev, 0.0), 2)}
            e["mkts"].append(t)

        # forward risk at the current marks: each market's own worst case and
        # each event's, netted across strikes / exclusive names
        book = self.last_snap[1]
        held = [(t, p, mk if mk is not None else a) for t, (p, a, mk) in book.items()]
        for t, p, mk in held:
            if t in markets:
                markets[t]["wc"] = round(market_worst(p, mk), 2)
        for ev, v in self.worst_by_event(held).items():
            if ev in events:
                events[ev]["wc"] = v

        # per-family intraday curves (the drivers sparklines), the code and
        # config changes that land on them, and paid-vs-modeled by family
        t1 = time.time()

        def fam_of_t(tk):
            r = markets.get(tk)
            return r["fam"] if r else family_of(series_of(tk), cats.get(series_of(tk), ""))[0]
        fcurves = {k: self.family_curves(w["start"], w["end"], fam_of_t)
                   for k, w in self.windows.items() if k in ("today", "yesterday", "24h", "7d")}
        for dd in days.values():
            if dd.get("ok"):
                fc = self.family_curves(dd["s"], dd["e"], fam_of_t)
                if fc:
                    dd["fc"] = fc
        changes = self.load_changes(min(self.windows["7d"]["start"], self.detail_from))
        realize = rollup_realization(self.calib, lambda s: family_of(s, cats.get(s, ""))[0])
        self._t("drivers", t1)

        per_market_paths = self._market_paths(markets, wins)

        # families summary is computed client-side from markets (all windows)
        hist, ledger_max = self.daily_history()

        # curves
        curves = {}
        for key, w in self.windows.items():
            if key == "roll":
                continue
            span = w["end"] - w["start"]
            step = 300.0 if span <= 26 * 3600 else 3600.0
            curves[key] = self.curve(w["start"], w["end"], step)

        # fills for the fills table, newest first, as far back as the day history
        fills_from = min([d["s"] for d in days.values()] + [self.windows["7d"]["start"]])
        fills_out = []
        for f in reversed(self.fills):
            if f["ts"] < fills_from:
                break
            mo = self.markouts.get(f["id"])
            fr = {"ts": round(f["ts"]), "t": f["t"], "side": f["side"], "px": round(f["px"], 2),
                  "n": round(f["n"], 2), "pad": f["pad"], "taker": f["taker"],
                  "pos1": round(f["pos1"], 1)}
            d = 1.0 if f["side"] == "bid" else -1.0
            if mo:
                fr["mk"] = round(d * (mo[0] - f["px"]) * f["n"] / 100.0, 3)
                fr["mkc"] = round(d * (mo[0] - f["px"]), 2)
            elif f["id"] in self.markouts:
                fr["mk_na"] = 1
            elif f["ts"] + MARKOUT_SECS > self.now:
                fr["mk_pending"] = 1
            if not (f["pad"] or f["taker"]):
                # cents per contract, signed for our side: the edge at the fill
                # (vs the arrival book's mid) and the other horizons
                if f.get("mid") is not None:
                    fr["ec"] = round(d * (f["mid"] - f["px"]), 2)
                h = self.markouts_h.get(f["id"]) or {}
                for hk in ("m5", "m4"):
                    if h.get(hk):
                        fr[hk + "c"] = round(d * (h[hk][0] - f["px"]), 2)
                nv = self.now_values.get(f["t"])
                if nv is not None:
                    fr["mtc"] = round(d * (nv[0] - f["px"]), 2)
                    if nv[1] == "settled":
                        fr["mts"] = 1
            fills_out.append(fr)

        health = self._health(cycle_ts)
        opp = self._opportunities(selected, cur) if self.api else {"off": True}
        proj = self.projection(snap_by_t, cycle_ts)     # after the API import: the size schedule
        self._t("assemble", t0)

        roll = wins["roll"]
        roll_rew = sum(v["rew"] for v in roll[0].values())
        roll_pnl = sum(v["pnl"] for v in roll[0].values()) if roll[1][2] else None

        model = {
            "generated": self.now,
            "generated_et": datetime.fromtimestamp(self.now, ET).strftime("%a %b %d, %I:%M %p ET").replace(" 0", " "),
            "windows": {k: dict(w, pnl_na=not wins[k][1][2]) for k, w in self.windows.items()},
            "edges": {k: list(v[1][:2]) for k, v in wins.items()},
            "decisions": self._decision_mix(),
            "markets": markets, "events": events, "paths": per_market_paths,
            "curves": curves, "history": hist, "ledger_max": ledger_max,
            "days": days, "day_fields": list(DAY_FIELDS), "fx_keys": list(FX_KEYS),
            "fcurves": fcurves, "changes": changes, "realize": realize, "proj": proj,
            "struct_cover": self._struct_cover(held),
            "fills": fills_out, "halts": halts, "health": health, "opp": opp,
            "exits": [{"ts": round(e["ts"]), "t": e["t"], "pos": round(e["pos"], 2),
                       "avg": round(e["avg"], 2), "mark": round(e["mark"], 2),
                       "kind": e["kind"], "label": e["label"], "amount": round(e["amount"], 2),
                       "realized": e["realized"], "px": e.get("settle_px")}
                      for e in self.exits if e["ts"] >= self.windows["7d"]["start"]],
            "bot": {
                "reward_est_today": _f(st.get("reward_est_today")),
                "reward_paid_today": _f(st.get("reward_paid_today")),
                "pnl_today": _f(st.get("pnl_today_carry")),
                "day_key": st.get("halt_day_key"),
                "roll_rew_ours": round(roll_rew, 2),
                "roll_pnl_ours": (round(roll_pnl, 2) if roll_pnl is not None else None),
                "unrealized_mtm": _f(stat.get("unrealized_mtm")),
                "loss_limit": _f(self.env.get("IMM_DAILY_LOSS_LIMIT"), 1200.0),
                "scan_loss_limit": _f(self.env.get("IMM_SCAN_DAILY_LOSS_LIMIT"), 200.0),
                "scan_pnl": _f(st.get("scan_pnl_carry")),
                # the bot's own verdict (roll-day keyed); scan_halt_day keeps
                # the last trip's day forever, so its mere presence is no halt
                "scan_halted": bool(stat.get("scan_halted_today")),
                "halted_until": _f(st.get("halted_until")),
                "markets_line": stat.get("markets_line", ""),
                "selected": len(selected), "scan_slots": stat.get("scan_slots"),
                "reward_history": st.get("reward_history") or {},
                "reward_paid_history": st.get("reward_paid_history") or {},
                "inventory_contracts": _f(stat.get("inventory_contracts")),
                "max_events": _f(self.env.get("IMM_MAX_MARKETS"), 0) or None,
                "budget": _f(self.env.get("IMM_COLLATERAL_BUDGET"), 0) or None,
            },
            "config": {k: v for k, v in sorted(self.env.items())},
            "credits": {"ledger_max": ledger_max,
                        "lifetime_imm": _f((self.calib or {}).get("credited_imm_attributable")),
                        "est_lifetime": _f(st.get("reward_est_lifetime"))},
            "snapshot_ts": self.snapshot_ts,
            "timing": self.timing, "warnings": self.warnings,
        }
        return model

    def _decision_mix(self):
        """{family: {decision: [markets, modelled $/day]}} over the newest
        hourly selection snapshot -- every candidate the bot scored."""
        out = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
        for r in self.snapshot:
            s_ = r.get("series") or series_of(r.get("ticker") or "")
            fam, _g = family_of(s_)
            d = str(r.get("decision") or "?")
            cell = out[fam][d]
            cell[0] += 1
            cell[1] += _f(r.get("est_dollars_per_day"))
        return {f: {d: [c[0], round(c[1], 2)] for d, c in v.items()} for f, v in out.items()}

    # ---- halts --------------------------------------------------------------
    def _halts(self, selected, cur):
        st = self.state
        now = self.now
        by_market = {}
        side_rows, event_rows = [], []
        for k, until in (st.get("toxic_halt_until") or {}).items():
            until = _f(until)
            if until <= now:
                continue
            t, _, side = k.partition("|")
            side_rows.append({"t": t, "side": side, "until": until})
            by_market.setdefault(t, []).append(f"toxic {side} until "
                                               + datetime.fromtimestamp(until, ET).strftime("%I:%M %p").lstrip("0"))
        for ev, until in (st.get("toxic_event_halt_until") or {}).items():
            until = _f(until)
            if until <= now:
                continue
            event_rows.append({"ev": ev, "until": until})
            for t in set(selected) | set(cur):          # a quoted member is in both
                if event_of(t) == ev:
                    by_market.setdefault(t, []).append("toxic event halt")
        strikes = []
        for k, lst in (st.get("toxic_picks") or {}).items():
            lst = [x for x in lst if now - _f(x) <= 86400]
            if lst:
                t, _, side = k.partition("|")
                strikes.append({"t": t, "side": side, "n": len(lst), "last": max(_f(x) for x in lst)})
        pending = len(st.get("toxic_pending") or [])
        # the sink: today's pick-offs / halts
        t0 = self.windows["today"]["start"]
        y0 = self.windows["yesterday"]["start"]
        sink = []
        for r in self.toxic:
            if r["ts"] < y0:
                continue
            row = {k: r.get(k) for k in ("kind", "ts", "ticker", "event", "side", "fill_px",
                                          "fill_ts", "mark", "until", "pickoffs",
                                          "markets_halted", "est_per_day", "markets_picked")}
            sink.append(row)
        # mention-gate halts (event_depth_halt / event_live_halt)
        depth = []
        # only events the bot still manages: a mention-gate hold on an event
        # whose call is over stands nothing down any more
        live_events = {event_of(t) for t in set(selected) | set(cur) | set(self.guard_holds)}
        for kind, key in (("depth", "event_depth_halt"), ("live", "event_live_halt")):
            for ev, ts in (st.get(key) or {}).items():
                if ev in live_events:
                    depth.append({"ev": ev, "kind": kind, "ts": _f(ts),
                                  "strikes": (st.get("event_fill_strikes") or {}).get(ev)})
        guards = []
        for t, g in self.guard_holds.items():
            guards.append({"t": t, "guard": g["guard"], "since": g["since"],
                           "risk": g["guard"] in RISK_GUARDS,
                           "bid": g.get("bid"), "ask": g.get("ask"),
                           "inputs": {k: v for k, v in (g.get("inputs") or {}).items()
                                      if isinstance(v, (int, float, str, bool))}})
            by_market.setdefault(t, []).append("guard: " + str(g["guard"]))
        manual = os.path.exists(os.path.join(STATUS_DIR, "HALT"))
        return {"side": side_rows, "event": event_rows, "strikes": strikes,
                "pending": pending, "sink": sink, "depth": depth, "guards": guards,
                "guard_entered_today": self.guard_entered, "manual_halt_file": manual,
                "by_market": by_market,
                "hopeless_barred": len(st.get("scan_hopeless_barred") or {}),
                "toxic_sink_present": bool(self.toxic)}

    # ---- per-market hourly paths ---------------------------------------------
    def _market_paths(self, markets, wins):
        """Hourly marks, position and cumulative P&L / rewards over the 7-day
        window for the markets that matter most (top 120 per window by |P&L|
        or |net|, plus the 60 biggest open positions by $ at risk), for the
        detail drawer. Compact: {"hours": [...], "m": {ticker: {p, r, k, q}}}."""
        pick = set()
        for key in ("today", "yesterday", "24h", "7d"):
            mk = wins[key][0]
            ranked = sorted(mk, key=lambda t: -max(abs(mk[t]["pnl"]), abs(mk[t]["net"])))
            pick |= set(ranked[:120])
        book = self.last_snap[1]
        risk = sorted(book, key=lambda t: -abs(book[t][0] * (book[t][1] if book[t][0] > 0
                                                              else 100 - book[t][1])))
        pick |= set(risk[:60])
        hours = sorted(h for h in self.hour_snaps if h * 3600 >= self.windows["7d"]["start"] - 3600)
        paths = {"hours": hours, "m": {}}
        if not hours:
            return paths
        real_by_t = defaultdict(list)
        for ts, t, _e, d in self.realized:
            if t in pick and ts >= self.windows["7d"]["start"] - 3600:
                real_by_t[t].append((ts, d))
        for e in self.exits:
            if e["t"] in pick and e["ts"] >= self.windows["7d"]["start"] - 3600:
                real_by_t[e["t"]].append((e["ts"], e["amount"]))
        for t in real_by_t:
            real_by_t[t].sort()
        for t in pick:
            P, R, K, Q = [], [], [], []
            cum_real = 0.0
            ri = 0
            rl = real_by_t.get(t, [])
            hs = self.hourly.get(t, {})
            cum_rew = 0.0
            for h in hours:
                ts, hb = self.hour_snaps[h]
                while ri < len(rl) and rl[ri][0] < ts:
                    cum_real += rl[ri][1]
                    ri += 1
                v = hb.get(t)
                u = v[0] * (v[2] - v[1]) / 100.0 if (v and v[2] is not None) else 0.0
                cum_rew += hs.get(h - 1, 0.0)
                P.append(round(cum_real + u, 2))
                R.append(round(cum_rew, 2))
                K.append(round(v[2], 1) if v and v[2] is not None else None)
                Q.append(round(v[0]) if v else 0)
            paths["m"][t] = {"p": P, "r": R, "k": K, "q": Q}
        return paths

    # ---- health ----------------------------------------------------------------
    def _health(self, cycle_ts):
        st, stat = self.state, self.status
        now = self.now
        upd = iso_ts(stat.get("updated_at"))
        recent = sorted(c for c in self.cycles if c >= now - 3600)
        gaps = [b - a for a, b in zip(recent, recent[1:]) if b > a]
        period = statistics.median(gaps) if gaps else None
        max_gap = max(gaps) if gaps else None
        t0 = self.windows["today"]["start"]
        runs_today = sorted(((rid, r[0], r[1]) for rid, r in self.runs.items()
                             if r[1] >= t0), key=lambda x: x[1])
        sinks = {}
        for name, ext in (("cycle_log", "csv"), ("marks", "jsonl"), ("fills", "jsonl"),
                          ("orders", "jsonl"), ("selection_events", "jsonl"),
                          ("guard_skips", "jsonl"), ("realized", "jsonl"),
                          ("toxic_halts", "jsonl"), ("book_depth", None)):
            try:
                if ext is None:
                    fs = glob.glob(os.path.join(STATUS_DIR, "book_depth", "*.jsonl.gz"))
                else:
                    fs = glob.glob(os.path.join(STATUS_DIR, f"{name}_*.{ext}"))
                sinks[name] = max(os.path.getmtime(p) for p in fs) if fs else None
            except (OSError, ValueError):
                sinks[name] = None
        today = datetime.fromtimestamp(now, ET).date().isoformat()
        lineup = []
        for label, marker, logname, at in LINEUP:
            row = {"label": label, "at": at, "ok": None, "note": ""}
            if marker:
                row["ok"] = os.path.exists(os.path.join(STATUS_DIR, f"{marker}_{today}.marker"))
                # a job that has never sent (just registered) is not a failure
                row["ever"] = bool(glob.glob(os.path.join(STATUS_DIR, f"{marker}_*.marker")))
            logp = os.path.join(STATUS_DIR, logname)
            try:
                row["log_mtime"] = os.path.getmtime(logp)
                if logname.endswith(".log"):
                    with open(logp, "rb") as f:
                        f.seek(max(0, os.path.getsize(logp) - 4000))
                        tail = f.read().decode("utf-8", "replace")
                    errs = [ln for ln in tail.splitlines() if re.search(
                        r"Error|Traceback|Exception", ln)]
                    if errs and not row["ok"]:
                        row["note"] = errs[-1].strip()[:160]
            except OSError:
                row["log_mtime"] = None
            lineup.append(row)
        ledger_max = max((d for d, _e, _a in self.credit_rows), default="")
        return {
            "updated_at": upd, "age_min": round((now - upd) / 60.0, 1) if upd else None,
            "stale": (now - upd) / 60.0 > HEARTBEAT_STALE_MIN if upd else True,
            "mode": stat.get("mode"), "cycle_ts": cycle_ts,
            "cycle_period": round(period, 1) if period else None,
            "max_gap": round(max_gap, 1) if max_gap else None,
            "runs_today": [{"run": r, "start": a, "end": b} for r, a, b in runs_today],
            "errors_today": stat.get("errors_today"), "fills_today_proc": stat.get("fills_today"),
            "sinks": sinks, "lineup": lineup, "ledger_max": ledger_max,
            "snapshot_ts": self.snapshot_ts, "guard_run": self.guard_run,
            "marks_last": self.last_snap[0], "halt_file": os.path.exists(os.path.join(STATUS_DIR, "HALT")),
        }

    # ---- projected full day ----------------------------------------------------
    def projection(self, snap_by_t, cycle_ts) -> dict:
        """The Modeled-rewards card's projection (project_rest_of_day) from
        the bot's last full cycle: {"rest": $ still to come today, "rate":
        $/day at this cycle's size, "markets", "stopping": markets that stop
        before midnight, "sched": the bot's size schedule applied} or
        {"na": why}. The schedule is incentive_mm.hour_size_mult, loaded with
        the launcher env by the API sections; without it every market keeps
        its current size."""
        if not self.last_cycle:
            return {"na": "no quoting cycle logged"}
        if self.now - cycle_ts > PROJ_MAX_CYCLE_AGE_S:
            return {"na": "bot not cycling since "
                    + datetime.fromtimestamp(cycle_ts, ET).strftime("%H:%M ET")}
        imm = _imm_mod if hasattr(_imm_mod, "hour_size_mult") else None
        mult_at = None if imm is None else (
            lambda ser, ts: imm.hour_size_mult(ser, datetime.fromtimestamp(ts, timezone.utc)))
        end = et_midnight(datetime.fromtimestamp(self.now, ET).date() + timedelta(days=1))
        rows, rate, stopping = [], 0.0, 0
        for t, r in self.last_cycle.items():
            frac, pool = _f(r.get("est_frac")), _f(r.get("pool_per_day"))
            sr = snap_by_t.get(t) or {}
            stops = [x for x in (iso_ts(sr.get("cutoff")), iso_ts(sr.get("program_end"))) if x > 0]
            stop = min(stops) if stops else None
            if frac > 0 and stop is not None and stop < end:
                stopping += 1
            rate += frac * pool
            rows.append((t, frac, pool, _f(r.get("hour_mult"), 1.0), stop))
        try:
            rest = project_rest_of_day(rows, self.now, mult_at, end)
        except Exception as e:                  # a schedule error must not cost the page
            log(f"! projection with the size schedule failed ({e!r}); current size kept")
            rest, mult_at = project_rest_of_day(rows, self.now, None, end), None
        return {"rest": round(rest, 2), "rate": round(rate, 2),
                "markets": sum(1 for _t, f, _p, _h, _s in rows if f > 0),
                "stopping": stopping, "sched": mult_at is not None}

    # ---- opportunities (API) -----------------------------------------------------
    def _opportunities(self, selected, cur):
        gaps, imm = _import_imm()
        if gaps is None:
            return {"error": "incentive_mm import failed"}
        now_utc = datetime.fromtimestamp(self.now, timezone.utc)
        progs, p_at, p_err = _api_cached("programs", PROGRAMS_TTL_MIN, self.api_force,
                                         lambda: fetch_programs(gaps, imm, now_utc))
        reg_path = os.path.join(DASH_DIR, "programs_seen.json")
        reg = load_json(reg_path, {})
        if not reg:
            seen0 = load_json(os.path.join(STATUS_DIR, "imm_new_programs_seen.json"))
            for ev, v in (seen0.get("events") or {}).items():
                fs = iso_ts((v or {}).get("first_seen"))
                if fs:
                    reg[ev] = fs

        def _extra():
            """Events of NEW series launched in the last 72h that nothing
            quotes: estimate them (a routine re-listing is not news)."""
            first = {}
            for ev_, fs in reg.items():
                first[series_of(ev_)] = min(first.get(series_of(ev_), fs), fs)
            out_ = {}
            cut = self.now - 72 * 3600
            for ev, rec in (progs or {}).items():
                launched = reg.get(ev, rec["start"])
                if launched < cut or rec["end"] <= self.now + 2 * 3600:
                    continue
                if first.get(rec["series"], launched) < cut - 3600:
                    continue
                if any(t in selected or t in cur for t in rec["tickers"]):
                    continue
                if str(rec.get("bot") or "").startswith("blocked"):
                    continue
                out_[ev] = rec["tickers"]
            return out_
        gap, g_at, g_err = _api_cached("gaps", API_TTL_MIN, self.api_force,
                                       lambda: fetch_gaps(gaps, imm, now_utc, _extra()))
        pick, k_at, k_err = _api_cached("pickoff", API_TTL_MIN, self.api_force,
                                        lambda: fetch_pickoff(gaps, imm, now_utc))
        out = {"programs_at": p_at, "gaps_at": g_at, "pick_at": k_at,
               "errors": {k: v for k, v in (("programs", p_err), ("gaps", g_err),
                                            ("pickoff", k_err)) if v}}
        # first-seen registry (seeded above from the new-programs email's seen-file)
        gap_by_ev = {r["event"]: r for r in (gap or {}).get("rows", [])} if gap else {}
        for ev, x in ((gap or {}).get("extra") or {}).items():
            g0 = dict(gap_by_ev.get(ev) or {"event": ev, "reason": "", "n": x["n_tried"],
                                              "partial": True})
            g0.update(est=x["est"], yld=x["yld"], n_est=x["n_est"],
                      partial=x["n_est"] < len((progs or {}).get(ev, {}).get("tickers") or []))
            gap_by_ev[ev] = g0
        new_rows = []
        if progs:
            changed = False
            for ev, rec in progs.items():
                if ev not in reg:
                    reg[ev] = min(self.now, rec["start"])
                    changed = True
            if changed:
                try:
                    os.makedirs(DASH_DIR, exist_ok=True)
                    with open(reg_path + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(reg, f)
                    os.replace(reg_path + ".tmp", reg_path)
                except OSError:
                    pass
            horizon = self.now - 7 * 86400
            series_first = {}
            for ev, fs in reg.items():
                ser = series_of(ev)
                series_first[ser] = min(series_first.get(ser, fs), fs)
            seen = load_json(os.path.join(STATUS_DIR, "imm_new_programs_seen.json"))
            for ser, v in (seen.get("series") or {}).items():
                fs = iso_ts((v or {}).get("first_seen"))
                if fs:
                    series_first[ser] = min(series_first.get(ser, fs), fs)
            for ev, rec in progs.items():
                # an event the morning email saw first carries the email's run
                # time; its program start is the earlier, truer launch
                launched = min(reg.get(ev, rec["start"]), rec["start"])
                if launched < horizon or rec["end"] <= self.now:
                    continue
                q_cur = sum(1 for t in rec["tickers"] if t in cur and cur[t]["q"] > 0)
                q_sel = sum(1 for t in rec["tickers"] if t in selected)
                est_day = sum(cur[t]["est_day"] for t in rec["tickers"] if t in cur)
                g = gap_by_ev.get(ev)
                fam, grp = family_of(rec["series"])
                row = {"ev": ev, "ser": rec["series"], "fam": fam, "grp": grp,
                       "what": rec["what"], "n": rec["n"], "pool": rec["pool_day"],
                       "pool_total": rec["pool_total"], "start": rec["start"], "end": rec["end"],
                       "launched": launched, "bot": rec["bot"], "q": q_cur, "sel": q_sel,
                       "est_now": round(est_day, 2),
                       "series_first": series_first.get(rec["series"], launched)}
                if g:
                    row.update(g_est=g["est"], g_yld=g["yld"], g_reason=g["reason"],
                               g_n=g["n"], g_nest=g["n_est"], g_partial=g["partial"])
                new_rows.append(row)
            new_rows.sort(key=lambda r: -r["launched"])
            out["feed"] = {"events": len(progs), "pool_day": round(sum(r["pool_day"] for r in progs.values()), 2),
                           "markets": sum(r["n"] for r in progs.values())}
        out["new_groups"] = {str(h): new_event_groups(new_rows, self.now - h * 3600, self.now)
                             for h in (24, 72, 168)}
        out["params"] = {"promising_est": PROMISING_EST, "promising_left": PROMISING_LEFT}
        if gap:
            out["gaps"] = sorted(gap["rows"], key=lambda d: (
                -(d["yld"] if d.get("yld") is not None else -1),
                -(d["est"] if d["est"] is not None else -1), -d["pool"]))[:60]
            out["gaps_ctx"] = gap.get("ctx") or {}
        if pick:
            out["pickoff"] = pick
        return out


# ----------------------------------------------------------------------------
# New events: grouped by series, flagged
# ----------------------------------------------------------------------------

_DELIBERATE = ("blocklisted", "frozen", "excluded family")


def _deliberate(row: dict) -> bool:
    """Off by a standing decision (launcher blocklist / freeze / an open-scan
    family exclusion), not by anything the bot's economics could change."""
    reason = str(row.get("g_reason") or "")
    bot = str(row.get("bot") or "")
    return reason.startswith(_DELIBERATE) or "excluded family" in reason \
        or bot.startswith("blocked")


def new_event_groups(rows, cut: float, now: float):
    """Events launched since `cut`, one group per series, flagged.

    A group is ROUTINE when its series already had events before `cut`
    (daily rain, state gas, weekly YouTube...): Kalshi re-listing a family the
    bot already handles is not news. `left` is the money still in the
    programs, pool $/day x days remaining -- a 15-minute program at $480/day
    has $5 in it, so ranking by $/day would bury real launches under noise.

    Flags (only for NEW series -- not routine re-listings -- where nothing is
    quoted or selected, not off by a standing decision, with 2h+ left):
      promising        the bot's estimator says its ladder would earn
                       >= PROMISING_EST $/day there (or half that at >= 2%/day
                       ROI), and >= $3 over the time left in the programs
      unestimated pool no estimate yet (the estimator's book budget did not
                       reach it) but >= PROMISING_LEFT dollars left."""
    groups = {}
    for r in rows:
        if r["launched"] < cut or r["end"] <= now:
            continue
        g = groups.get(r["ser"])
        if g is None:
            g = groups[r["ser"]] = {
                "ser": r["ser"], "fam": r["fam"], "grp": r["grp"], "what": r.get("what") or "",
                "routine": r.get("series_first", r["launched"]) < cut - 3600,
                "n_ev": 0, "n": 0, "pool": 0.0, "left": 0.0, "launched": r["launched"],
                "last_launch": r["launched"], "end": r["end"], "remain_h": 0.0, "q": 0, "sel": 0,
                "est_now": 0.0, "g_est": None, "g_n_est": 0, "g_yld": None,
                "reasons": Counter(), "deliberate": True, "events": []}
        remain_d = max(0.0, r["end"] - now) / 86400.0
        left = r["pool"] * remain_d
        g["n_ev"] += 1
        g["n"] += r["n"]
        g["pool"] += r["pool"]
        g["left"] += left
        g["launched"] = min(g["launched"], r["launched"])
        g["last_launch"] = max(g["last_launch"], r["launched"])
        g["end"] = max(g["end"], r["end"])
        g["remain_h"] = max(g["remain_h"], remain_d * 24.0)
        g["q"] += r["q"]
        g["sel"] += r["sel"]
        g["est_now"] += r.get("est_now") or 0.0
        if r.get("g_est") is not None:
            g["g_est"] = (g["g_est"] or 0.0) + r["g_est"]
            g["g_n_est"] += 1
        if r.get("g_yld") is not None:
            g["g_yld"] = max(g["g_yld"] or 0.0, r["g_yld"])
        g["reasons"][str(r.get("g_reason") or r.get("bot") or "")] += 1
        g["deliberate"] = g["deliberate"] and _deliberate(r)
        g["events"].append(dict(r, left=round(left, 2)))
    out = []
    for g in groups.values():
        flag = ""
        unquoted = g["q"] == 0 and g["sel"] == 0
        # a routine re-listing (tomorrow's KXTRUMPAPPROVE, the next state-gas
        # day) is picked up by the bot's own refresh: never flag it
        if unquoted and not g["deliberate"] and not g["routine"] and g["remain_h"] >= 2.0:
            est = g["g_est"]
            if est is not None and (est >= PROMISING_EST
                                    or (est >= PROMISING_EST / 2 and (g["g_yld"] or 0.0) >= 0.02)) \
                    and est * g["remain_h"] / 24.0 >= 3.0:
                flag = "promising"
            elif est is None and g["left"] >= PROMISING_LEFT:
                flag = "unestimated pool"
        g["flag"] = flag
        g["reason"] = g.pop("reasons").most_common(1)[0][0] if g["n_ev"] else ""
        g["events"].sort(key=lambda e: -e["left"])
        g["events"] = g["events"][:40]
        for k in ("pool", "left", "est_now"):
            g[k] = round(g[k], 2)
        if g["g_est"] is not None:
            g["g_est"] = round(g["g_est"], 2)
        out.append(g)
    out.sort(key=lambda g: (g["flag"] != "promising", g["routine"], -g["left"]))
    return out


# ----------------------------------------------------------------------------
# Render
# ----------------------------------------------------------------------------

SUMMARY_NAME = "imm_dashboard_summary.json"
SUMMARY_WINDOWS = ("today", "yesterday", "24h", "7d")
SUMMARY_EVENT_WINDOWS = ("yesterday", "7d")


def summary_of(model: dict) -> dict:
    """The morning email's slice of the page (send_imm_digest reads it, so the
    email shows the page's own numbers -- Jack 2026-10-02: "yesterday RAW
    will equal the dashboard's total trading P&L"). Every figure is a card's
    own sum over the per-market rows, as the page adds them up (aggregate()):
      windows  each window's modeled rewards / trading P&L / realized / change
               in marks, plus each event's [rewards, P&L] for yesterday and
               the 7 days; pnl_na = the position log does not cover it
      days     each ET day as its day card shows it (click the day on the
               page); pnl None where "ok" is false, live = the day was still
               running at this build"""
    wins = {}
    for key in SUMMARY_WINDOWS:
        w = (model.get("windows") or {}).get(key)
        if not w:
            continue
        tot = dict.fromkeys(("rew", "pnl", "real", "du", "fills", "cts"), 0.0)
        evs = defaultdict(lambda: [0.0, 0.0])
        for rec in (model.get("markets") or {}).values():
            o = (rec.get("w") or {}).get(key)
            if not o:
                continue
            for k in tot:
                tot[k] += _f(o.get(k))
            if key in SUMMARY_EVENT_WINDOWS:
                e = evs[rec["ev"]]
                e[0] += _f(o.get("rew"))
                e[1] += _f(o.get("pnl"))
        out = {"start": w.get("start"), "end": w.get("end"), "desc": w.get("desc"),
               "pnl_na": bool(w.get("pnl_na")), **{k: round(v, 2) for k, v in tot.items()}}
        if key in SUMMARY_EVENT_WINDOWS:
            out["e"] = {ev: [round(a, 4), round(b, 4)] for ev, (a, b) in sorted(evs.items())}
        wins[key] = out
    f = model.get("day_fields") or list(DAY_FIELDS)
    ix = {k: f.index(k) for k in ("rew", "pnl", "fills", "cts")}
    gen = _f(model.get("generated"))
    days = []
    for iso in sorted(model.get("days") or {}):
        dd = model["days"][iso]
        tot = dict.fromkeys(ix, 0.0)
        for row in (dd.get("m") or {}).values():
            for k, i in ix.items():
                tot[k] += _f(row[i])
        ok = bool(dd.get("ok"))
        days.append({"d": iso, "ok": ok, "live": _f(dd.get("e")) >= gen - 1,
                     "rew": round(tot["rew"], 2), "pnl": round(tot["pnl"], 2) if ok else None,
                     "fills": int(tot["fills"]), "cts": round(tot["cts"], 1)})
    return {"generated": model.get("generated"), "generated_et": model.get("generated_et"),
            "windows": wins, "days": days}


def write_summary(model: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(summary_of(model), fh, separators=(",", ":"))
    os.replace(tmp, path)


def render(model: dict) -> str:
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(model, separators=(",", ":"), default=str)
    # a JSON string inside <script> must not be able to close the tag
    blob = blob.replace("</", "<\\/").replace("<!--", "<\\!--")
    return tpl.replace("/*__IMM_DATA__*/null", blob)


LOG_MAX_BYTES = 2_000_000


def _redirect_to_log(path: str) -> None:
    """stdout/stderr -> `path` (append, UTF-8), trimmed to its last half once
    it passes LOG_MAX_BYTES so a 10-minute cadence cannot grow it forever."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            with open(path, "rb") as f:
                f.seek(-LOG_MAX_BYTES // 2, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(tail[tail.find(b"\n") + 1:])
        fh = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = sys.stderr = fh
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-api", action="store_true", help="local sinks only")
    ap.add_argument("--api-refresh", action="store_true", help="ignore the API cache TTL")
    ap.add_argument("--open", action="store_true", help="open the page when done")
    ap.add_argument("--out", default=OUT_HTML)
    ap.add_argument("--json", default=None, help="also dump the model JSON here")
    ap.add_argument("--log", default=None,
                    help="append output here (the scheduled task runs under pythonw, which has no console)")
    args = ap.parse_args(argv)
    if args.log:
        _redirect_to_log(args.log)
    t0 = time.time()
    os.makedirs(DASH_DIR, exist_ok=True)
    b = Builder(time.time(), api=not args.no_api, api_force=args.api_refresh)
    try:
        model = b.build()
    except Exception:
        log("! build failed:\n" + traceback.format_exc())
        return 1
    model["timing"]["total"] = round(time.time() - t0, 2)
    page = render(model)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, args.out)
    try:                                      # the morning email reads this
        write_summary(model, os.path.join(os.path.dirname(os.path.abspath(args.out)), SUMMARY_NAME))
    except Exception as e:
        log(f"! summary write failed: {e!r}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(model, f, default=str)
    log(f"wrote {args.out} ({len(page) / 1e6:.1f} MB) in {time.time() - t0:.1f}s "
        f"timing={json.dumps(model['timing'])}")
    if args.open:
        try:
            os.startfile(args.out)       # type: ignore[attr-defined]
        except Exception as e:
            log(f"! open failed: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
