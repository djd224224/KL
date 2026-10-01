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

Days are ET calendar days (midnight to midnight). The bot's own halt/carry
counters roll at 5am CT; the page shows those beside the daily-loss meter.

SOURCES (all read-only): run-logs/incentive-mm/ -- imm_state.json,
status_incentive_mm.json, cycle_log_*.csv, marks_*, realized_*, fills_*,
settlements_*, guard_skips_*, toxic_halts_*, selection_snapshot_* (the
latest hourly block), reward_credits.csv. With the API on (default), also
Kalshi's incentive-program feed (new events), the 7:20 quote-gaps estimator
(imm_quote_gaps.classify_and_estimate: what unquoted events would earn) and
imm_pickoff.scan (pick-off windows), each cached for API_TTL minutes. Every
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
PROMISING_EST = float(os.environ.get("IMM_DASH_PROMISING_EST", "3.0"))
PROMISING_LEFT = float(os.environ.get("IMM_DASH_PROMISING_LEFT", "500"))
TITLE_BUDGET = int(os.environ.get("IMM_DASH_TITLE_BUDGET", "80"))
PAD_BID, PAD_ASK = 1, 99  # incentive_mm PAD_BID_CENTS / PAD_ASK_CENTS
CACHE_VERSION = 5
DAY_CURVE_STEP = 900              # per-day intraday curves at 15-minute steps
# per-market day arrays on the page: D.days[day].m[ticker] = [...] in this order
DAY_FIELDS = ("rew", "pnl", "real", "du", "xfer", "fills", "cts", "usd", "mk", "mk_n",
              "mk_cts", "rest", "pos0", "pos1", "mk0", "mk1", "settled")
VANISH_REAPPEAR_SECS = 6 * 3600   # a position back within this long was only a blip
EXIT_RESULTS_TTL = 3600           # re-ask Kalshi about an unsettled exit hourly

# Guards that mean "something went wrong / risk stood us down" rather than a
# structural reason (band, cutoff, qualify). Names from IMM_LOGGING.md.
RISK_GUARDS = frozenset((
    "event_fill_tripwire", "scan_fill_tripwire", "fill_burst", "blind",
    "event_depth_trip", "event_depth_hold", "one_sided_breaker",
    "scan_mid_tripwire", "move_breaker", "crossed", "breaker_cooldown",
    "toxic_halt"))

# Morning lineup: (label, marker prefix or None, task log, scheduled ET time)
LINEUP = (
    ("Earnings overrides", None, "overrides_last_runs.json", "06:45"),
    ("New programs email", "imm_new_programs_sent", "new-programs-task.log", "06:45"),
    ("IMM digest", "imm_digest_sent", "digest-task.log", "07:10"),
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
    if s.startswith("KXUST"):
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
    `queries` {key: (ticker, target_ts)} is answered in place with the first
    snapshot mark at or after target_ts (within MARKOUT_MAX_LAG): the fill
    mark-outs, resolved while the rows are in memory."""
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
        for key, (t, target) in queries.items():
            want[t].append((target, key))
        for t, lst in want.items():
            series = [(ts, snaps[ts][t][2]) for ts in order if t in snaps[ts]
                      and snaps[ts][t][2] is not None]
            if not series:
                continue
            for target, key in lst:
                for ts, mk in series:
                    if ts >= target:
                        if ts - target <= MARKOUT_MAX_LAG:
                            queries[key] = ("ok", mk, ts)
                        break
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
            out.append({
                "id": fid, "ts": ts, "t": t,
                "ev": r.get("event_ticker") or event_of(t), "side": side, "px": px,
                "n": cnt, "taker": bool(r.get("is_taker")), "pad": bool(r.get("is_pad")),
                "scan": bool(r.get("is_scan")),
                "pos0": _f(r.get("pos_before")), "pos1": _f(r.get("pos_after")),
                "bid": r.get("ext_bid"), "ask": r.get("ext_ask"),
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
    resolver = imm.EventStartResolver()
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
        mk_cache_path = os.path.join(CACHE_DIR, "markouts.json")
        mk_cache = load_json(mk_cache_path, {})
        # mark-out queries for fills that do not have one yet and are due
        queries = {}
        for f in self.fills:
            if f["pad"] or f["taker"] or f["id"] in mk_cache:
                continue
            target = f["ts"] + MARKOUT_SECS
            if target <= self.now - 60:
                queries[f["id"]] = (f["t"], target)
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
            if val is None:
                # queries whose target falls in (or before the end of) this file
                end = dt_day + 86400 + MARKOUT_MAX_LAG
                q = {k: v for k, v in queries.items() if dt_day - 86400 <= v[1] <= end}
                val = parse_marks_file(path, keep_hours=True, queries=q,
                                       max_ts=None if complete else self.now)
                for k, v in q.items():
                    if isinstance(v, tuple) and v and v[0] == "ok":
                        mk_cache[k] = [round(v[1], 3), round(v[2], 1)]
                        queries.pop(k, None)
                if complete:
                    # every query targeting a completed file is final now
                    for k, v in list(queries.items()):
                        if dt_day <= v[1] < dt_day + 86400 - MARKOUT_MAX_LAG:
                            mk_cache[k] = None
                            queries.pop(k, None)
                    stored = val if keep_hours else dict(val, hours={})
                    cache.put(path, stored)
                log(f"parsed {os.path.basename(path)}{'' if complete else ' [live]'}")
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
        self.markouts = mk_cache
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
    def window_markets(self, w0: float, w1: float, sums=None) -> dict:
        """{ticker: metrics} for one window. `sums` = ({ticker: rewards},
        {ticker: resting $-seconds}) precomputed for the window (the per-day
        pass buckets every market-hour once instead of once per day)."""
        h0, h1 = int(w0 // 3600), int(math.ceil(w1 / 3600.0))
        m = defaultdict(lambda: {"rew": 0.0, "rest_s": 0.0, "real": 0.0, "u0": 0.0,
                                 "u1": 0.0, "xfer": 0.0, "fills": 0, "cts": 0.0,
                                 "usd": 0.0, "mk": 0.0, "mk_n": 0, "mk_cts": 0.0,
                                 "buy": 0.0, "sell": 0.0, "settled": None})
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
            elif s["result"] in ("scalar", "void") and s["px"] is not None:
                # booked by the bot itself since 2026-09-29 (its realized
                # delta is already in "real"); labelled as build_exits labels
                # the older offset rows it resolves
                m[s["t"]]["settled"] = f"settled {s['result']} {float(s['px']):.1f}c"
        for e in self.exits_in(e0, e1):
            mm = m[e["t"]]
            if e["realized"]:
                mm["real"] += e["amount"]            # a settlement the bot did not book
            else:
                mm["xfer"] += e["amount"]            # out at its last mark
            mm["settled"] = e["label"]
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
            mo = self.markouts.get(f["id"])
            if mo:
                d = 1.0 if f["side"] == "bid" else -1.0
                mm["mk"] += d * (mo[0] - f["px"]) * f["n"] / 100.0
                mm["mk_n"] += 1
                mm["mk_cts"] += f["n"]
        out = {}
        span = max(w1 - w0, 1.0)
        for t, v in m.items():
            v["pnl"] = v["real"] + v["u1"] - v["u0"] + v["xfer"]
            v["net"] = v["rew"] + v["pnl"]
            v["rest_avg"] = v["rest_s"] / span
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
                           v.get("mk0"), v.get("mk1"), v["settled"] or 0]
            out[iso] = {"s": a, "e": b, "ok": bool(ok), "e0": e0, "e1": e1, "m": rows,
                        "c": self.curve(a, b, DAY_CURVE_STEP) if ok else
                        [[p[0], p[1], None] for p in self.curve_rewards_only(a, b, DAY_CURVE_STEP)]}
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
            if mo:
                d = 1.0 if f["side"] == "bid" else -1.0
                fr["mk"] = round(d * (mo[0] - f["px"]) * f["n"] / 100.0, 3)
                fr["mkc"] = round(d * (mo[0] - f["px"]), 2)
            elif f["id"] in self.markouts:
                fr["mk_na"] = 1
            elif f["ts"] + MARKOUT_SECS > self.now:
                fr["mk_pending"] = 1
            fills_out.append(fr)

        health = self._health(cycle_ts)
        opp = self._opportunities(selected, cur) if self.api else {"off": True}
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
            "days": days, "day_fields": list(DAY_FIELDS),
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
                "scan_halted": bool(st.get("scan_halt_day")),
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
            for t in list(selected) + list(cur):
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
