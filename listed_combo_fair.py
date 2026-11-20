#!/usr/bin/env python3
"""
listed_combo_fair.py -- the fair value of Kalshi's LISTED combo grids (KX...COMBO events: "Will a
US-Iran nuclear deal be agreed before 2028 AND WTI settle at $60 to $74.99 on Dec 31, 2027?"),
for the IMM's listed-combo gate. Jack 2026-11-17: "build the fair, expect combos as a line of
business", after the party combos' asks were lifted 2-6c under today's price within hours of the
11/13 start (no fair: the bot rested at the book) and KXIRANWTICOMBO-28JAN was left unquoted for
the same reason. (Not the RFQ multivariate combos: combo_maker.py.)

THE SHAPE. Every listed grid Kalshi runs today crosses ONE yes/no leg that another Kalshi market
prices (the 2028 party, Le Pen, the US-Iran deal, Hormuz traffic) with ONE binned number (WTI or
the pump price on a date, the S&P 500's change over 2029, December 2029 CPI, 2029 GDP, the jobs
added in 2029, the December 2029 unemployment rate, EUR/USD across the French vote). A cell is
P(yes/no leg = its outcome) x P(number in its bin | that outcome).
CELLS: each cell's legs from its title ("Iran deal: Agreed, WTI: $60 to $74.99") or, where the
title is short ("Democrat, 0-500K": the party grids since 11/19), from the cell's own rules
("If ALL of the following occur: Party: Democratic, Jobs added in 2029: 0 to 500,000, ...").

LEGS (EVENTS, per combo event, by hand; any other event is AUTO-MAPPED from its own rules when
both legs are of a kind priced here -- see auto_map -- and otherwise stays unpriced, the gate
keeping it out, listed under "unmapped" with its reason; the bot emails both kinds once):
  yes/no   the mid of the Kalshi market the combo's rules name; its own bid / ask feed the band,
           a 'not' outcome is 1 - it. A book wider than LEG_MAX_SPREAD is no read.
  number   'lognormal' (a price on a date: the forward from the named futures contract, vol x
           sqrt(T)) or 'normal' (a % change or a print: mean, sd -- a prior). The bins come from
           the cells' legs ("Below $60", "$60 to $74.99", "2.0% to 2.9%", "20% or above",
           "500,000 to 1 million");
           adjacent bins split at the midpoint of their facing edges (74.99 | 75 -> 74.995,
           1.9 | 2.0 -> 1.95, 0 | 0 -> 0), which honours each grid's inclusive wording.
  link     DELTA = the number's mean in the YES world minus its mean in the NO world (log units
           for a price, the leg's units otherwise). The unconditional mean stays the market's (F,
           or the prior's): YES world + (1 - p) x DELTA, NO world - p x DELTA.
BAND per cell: each input moved alone to its low and its high -- the yes/no leg to its bid and
ask, the number's centre and scale, DELTA -- and the moves combined in quadrature, up and down
apart (independent errors: all four at an extreme at once would overstate it).
HOLDS: a yes/no leg whose mid moves LEG_JUMP_C or more inside LEG_JUMP_WINDOW_S holds every combo
on it LEG_HOLD_S -- the leg market is the headline feed (a deal headline reprices the agreement
market first). A futures leg whose front month moves FUT_JUMP_PCT inside the window holds its
combos FUT_HOLD_S. Both re-arm while the move stands, as the crypto jump hold.
CUT: no fair within CUTOFF_H of the earliest scheduled decision (the party's election day, the
EUR/USD window's start, WTI's settlement) or the market's close.
STALE: the gate (incentive_mm.listed_combo_gate) stands an event aside once its oldest live leg
read is LISTED_COMBO_TTL_SECS old.

Dry read (no orders):  python listed_combo_fair.py [EVENT,...]
"""

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))

LEG_MAX_SPREAD = 0.10            # a yes/no leg book wider than this (dollars) is no read
LEG_JUMP_C = 4.0                 # a yes/no leg mid move (cents) inside the window = a headline
LEG_JUMP_WINDOW_S = 600
LEG_HOLD_S = 900
FUT_JUMP_PCT = 0.025             # a futures front-month move inside the window = a jump
FUT_HOLD_S = 600
FUT_REFRESH_S = 120              # futures quotes (Yahoo, delayed ~10 min) every 2 min
META_REFRESH_S = 600             # Kalshi market lists
CUTOFF_H = 24.0
YEAR_S = 365.25 * 86400.0

# ---------------------------------------------------------------------------------------------
# THE LEG MAP. Each listed combo event: its yes/no leg (the Kalshi market(s) each outcome reads)
# and its number (model and parameters, each with the band it is judged over), and the link.
# Priors are judgement, sized wide on purpose: the band is what the gate quotes outside of.
# ---------------------------------------------------------------------------------------------
PARTY_2028 = {
    "label": "Party", "yes": "Democratic",
    # each outcome its own market ("If neither a Democratic nor a Republican candidate is
    # inaugurated as President, every market resolves to No"), so P(D) + P(R) may be < 1
    "outcomes": {"Democratic": ("KXPRESPARTY-2028-D", True),
                 "Republican": ("KXPRESPARTY-2028-R", True)},
    "decides": "2028-11-07T11:00:00Z",          # election day (06:00 ET), the cut's anchor
}
EVENTS: Dict[str, dict] = {
    # Iran deal before Jan 1, 2028 x WTI's Dec 31, 2027 settlement (the Feb 2028 contract).
    # Feb-2028 CL traded $71.5 on 11/17 against an ~$87 front month (steep backwardation).
    # Vol 32% (26-40%): a year-plus crude vol in a supply-shock regime. Link: a deal puts
    # Iranian barrels back and drains the risk premium -- WTI ~8% lower in the deal world
    # (0 to -20%).
    "KXIRANWTICOMBO-28JAN": {
        "binary": {"label": "Iran deal", "yes": "Agreed",
                   "outcomes": {"Agreed": ("KXUSAIRANAGREEMENT-27-28", True),
                                "Not agreed": ("KXUSAIRANAGREEMENT-27-28", False)},
                   "decides": None},             # unscheduled: the leg hold covers it
        "number": {"label": "WTI", "model": "lognormal",
                   "fwd": ["CLG28.NYM", "CLF28.NYM", "CLH28.NYM"], "front": "CL=F",
                   "fwd_band": 0.03, "vol": 0.32, "vol_band": (0.26, 0.40),
                   "at": "2027-12-31T19:30:00Z", "decides": "2027-12-31T19:30:00Z"},
        "delta": -0.08, "delta_band": (-0.20, 0.0),
    },
    # Hormuz traffic back to normal before Jul 1, 2027 (IMF PortWatch 7-day average of transit
    # calls above 60: KXHORMUZNORM's "Before Jul 1, 2027", the same rule, 36/37 on 11/17) x the
    # AAA national regular price on Jul 31, 2027 (Jack 2026-11-17: "allow gas markets on
    # combos"). The pump price = Aug-2027 RBOB (RBQ27, $2.61 on 11/17; summer grade) + ~$1.00
    # of taxes and margin, +-4% for that spread; retail vol 25% (19-32%: RBOB's ~35% on a
    # price ~70% wholesale). Link: traffic back -> gasoline ~6% lower (0 to -15%).
    "KXHORMUZGASCOMBO-27AUG": {
        "binary": {"label": "Hormuz traffic", "yes": "Back to normal",
                   "outcomes": {"Back to normal": ("KXHORMUZNORM-26MAR17-B270701", True),
                                "Not back to normal": ("KXHORMUZNORM-26MAR17-B270701", False)},
                   "decides": None},             # unscheduled: the leg hold covers it
        "number": {"label": "Gas price", "model": "lognormal",
                   "fwd": ["RBQ27.NYM", "RBN27.NYM", "RBU27.NYM"], "front": "RB=F",
                   "fwd_add": 1.00, "fwd_band": 0.04, "vol": 0.25, "vol_band": (0.19, 0.32),
                   "at": "2027-07-31T14:00:00Z", "decides": "2027-07-31T14:00:00Z"},
        "delta": -0.06, "delta_band": (-0.15, 0.0),
    },
    # Party x the S&P 500's change from the Jan 19, 2029 close to the Dec 31, 2029 close
    # (0.95 y, no market prices 2029 yet): 7% (3-11%) mean, 17%/yr (14-20) vol. Party link
    # 0 (+-4 points): no reliable sign.
    "KXINXPARTYCOMBO-30JAN": {
        "binary": PARTY_2028,
        "number": {"label": "S&P 500 change", "model": "normal",
                   "mean": 7.0, "mean_band": (3.0, 11.0), "sd": 16.6, "sd_band": (13.6, 19.5),
                   "decides": "2029-01-19T21:00:00Z"},
        "delta": 0.0, "delta_band": (-4.0, 4.0),
    },
    # Party x December 2029 CPI-U YoY (first print, one decimal): 2.7% (2.3-3.2), sd 1.0
    # (0.8-1.3) four years out. Party link 0 (+-0.5).
    "KXCPIPARTYCOMBO-30MAR": {
        "binary": PARTY_2028,
        "number": {"label": "CPI inflation", "model": "normal",
                   "mean": 2.7, "mean_band": (2.3, 3.2), "sd": 1.0, "sd_band": (0.8, 1.3),
                   "decides": "2030-01-15T13:30:00Z"},
        "delta": 0.0, "delta_band": (-0.5, 0.5),
    },
    # Party x 2029 real GDP growth (advance estimate, one decimal): 1.9% (1.4-2.4), sd 1.4
    # (1.1-1.8). Party link 0 (+-0.5).
    "KXGDPPARTYCOMBO-30MAR": {
        "binary": PARTY_2028,
        "number": {"label": "GDP growth", "model": "normal",
                   "mean": 1.9, "mean_band": (1.4, 2.4), "sd": 1.4, "sd_band": (1.1, 1.8),
                   "decides": "2030-01-30T13:30:00Z"},
        "delta": 0.0, "delta_band": (-0.5, 0.5),
    },
    # Party x the jobs added in 2029: seasonally adjusted total nonfarm payrolls, December 2029
    # minus December 2028, first print (before the Feb 2030 benchmark). Jack 2026-11-20: "Yes,
    # map them". 11/20 anchor: November 2026 ~+90K (KXPAYROLLS-26NOV's median), a ~1.0M/yr
    # pace; low immigration holds the trend near 50-90K a month by 2029, and a 2029 recession
    # (~1 in 5) takes the year below 0. 550K (150K-950K), sd 850K (650K-1.1M). Party link 0
    # (+-300K).
    "KXPAYROLLSPARTYCOMBO-30SEP": {
        "binary": PARTY_2028,
        "number": {"label": "Jobs added in 2029", "model": "normal",
                   "mean": 550000.0, "mean_band": (150000.0, 950000.0),
                   "sd": 850000.0, "sd_band": (650000.0, 1100000.0),
                   "decides": "2030-01-04T13:30:00Z"},
        "delta": 0.0, "delta_band": (-300000.0, 300000.0),
    },
    # Party x the December 2029 U-3 unemployment rate (seasonally adjusted, first print, one
    # decimal). 11/20 anchor: November 2026 ~4.1% (KXU3-26NOV); ~4.2-4.4% longer run, and the
    # recession tail over three years lifts the mean. 4.5% (4.1-4.9), sd 0.8 (0.6-1.1). Party
    # link 0 (+-0.3).
    "KXU3PARTYCOMBO-30SEP": {
        "binary": PARTY_2028,
        "number": {"label": "Unemployment rate", "model": "normal",
                   "mean": 4.5, "mean_band": (4.1, 4.9), "sd": 0.8, "sd_band": (0.6, 1.1),
                   "decides": "2030-01-04T13:30:00Z"},
        "delta": 0.0, "delta_band": (-0.3, 0.3),
    },
    # French president 2027 (Le Pen) x EUR/USD's change from 4 PM ET Fri Apr 16 to 10 AM ET Mon
    # May 3, 2027 (Pyth, 16.75 days): EUR/USD ~7.5%/yr -> 1.6% over the window, 1.8% with the
    # vote (1.4-2.4). Link: a Le Pen win ~1.5 points weaker for the euro (0 to -3).
    "KXLEPENEURCOMBO-27JUN": {
        "binary": {"label": "French president", "yes": "Marine Le Pen",
                   "outcomes": {"Marine Le Pen": ("KXFRENCHPRES-27-MLEP", True),
                                "Not Marine Le Pen": ("KXFRENCHPRES-27-MLEP", False)},
                   "decides": "2027-04-18T06:00:00Z"},
        "number": {"label": "EUR/USD change", "model": "normal",
                   "mean": 0.0, "mean_band": (-0.5, 0.5), "sd": 1.8, "sd_band": (1.4, 2.4),
                   "decides": "2027-04-16T20:00:00Z"},
        "delta": -1.5, "delta_band": (-3.0, 0.0),
    },
}


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# AUTO-MAPPING (Jack 2026-11-17: "Yes, build it", to auto-mapping new combos plus an alert).
# A combo event with no EVENTS row is mapped from its own rules when both legs are of a kind
# already priced here: the rules name each leg's Kalshi ruleset ("Each component is resolved
# according to its corresponding Kalshi ruleset: Iran deal (AGREEMENT), WTI (WTIDIR)") and the
# dates that pick its market or contract ("the 'Before Jan 1, 2028' market", "the February 2028
# contract"). The yes/no resolvers find the leg's live Kalshi market; the number builders take
# the same model and bands as the hand rows above. A hand row always wins; anything else -- an
# unknown ruleset, two yes/no legs, a window already running, a grid that does not sum -- stays
# unmapped with its reason, and the bot emails it.
# ---------------------------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")
_MON = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                         "sep", "oct", "nov", "dec"))}
_FUT_CODE = "FGHJKMNQUVXZ"                       # CME month codes, Jan..Dec
_DATE = r"([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})"
AGREEMENT_SERIES = {"US-Iran nuclear deal": "KXUSAIRANAGREEMENT"}
FRENCH_RIGHT = ("Marine Le Pen", "Jordan Bardella")        # a win weakens the euro (the link)


def _date(s: str, hhmm: str = "12:00 AM") -> datetime:
    """'Dec 31, 2027' (+ '2:30 PM', ET) -> UTC."""
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}), (\d{4})", s.strip())
    t = re.fullmatch(r"(\d{1,2}):(\d{2}) ([AP])M", hhmm.strip())
    if not m or not t:
        raise ValueError(f"date '{s} {hhmm}'")
    h = int(t.group(1)) % 12 + (12 if t.group(3) == "P" else 0)
    d = datetime(int(m.group(3)), _MON[m.group(1).lower()], int(m.group(2)), h, int(t.group(2)),
                 tzinfo=ET)
    return d.astimezone(timezone.utc)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _election_day(year: int) -> datetime:
    """The Tuesday after the first Monday of November, 06:00 ET."""
    d = datetime(year, 11, 1, 6, 0, tzinfo=ET)
    while d.weekday() != 0:
        d = d.replace(day=d.day + 1)
    return d.replace(day=d.day + 1).astimezone(timezone.utc)


def _jobs_day(year: int, month: int) -> datetime:
    """A month's Employment Situation: the first Friday of the next month, 8:30 AM ET (BLS
    moves it a week when that Friday falls on the 1st-2nd; the market's close covers that)."""
    y, mo = (year + 1, 1) if month == 12 else (year, month + 1)
    d = datetime(y, mo, 1, 8, 30, tzinfo=ET)
    while d.weekday() != 4:
        d = d.replace(day=d.day + 1)
    return d.astimezone(timezone.utc)


def _fut(root: str, year: int, month: int) -> str:
    while month > 12:
        month, year = month - 12, year + 1
    while month < 1:
        month, year = month + 12, year - 1
    return f"{root}{_FUT_CODE[month - 1]}{year % 100:02d}.NYM"


def _norm(text: str) -> str:
    return " ".join((text or "").replace("“", '"').replace("”", '"')
                    .replace("’", "'").split())


def _components(rules: str) -> List[Tuple[str, str]]:
    m = re.search(r"corresponding Kalshi ruleset:\s*(.+?)\s*\(([A-Z0-9]+)\),\s*(.+?)\s*\(([A-Z0-9]+)\)",
                  rules)
    if m:
        return [(m.group(1), m.group(2)), (m.group(3), m.group(4))]
    m = re.search(r"([A-Z][^,.:]+?) uses ([A-Z0-9]+), ([A-Z][^,.:]+?) uses ([A-Z0-9]+)", rules)
    return [(m.group(1), m.group(2)), (m.group(3), m.group(4))] if m else []


def _open_markets(get_json, **params) -> List[dict]:
    body = get_json("/markets", dict(params, status="open", limit=200)) or {}
    return [m for m in body.get("markets") or [] if isinstance(m, dict)]


def _binary_leg(label: str, ruleset: str, outcomes: set, rules: str, get_json,
                now_ts: float) -> dict:
    """The yes/no leg's map row ({label, yes, outcomes, decides, kind}) or ValueError."""
    if ruleset == "PRESPARTY":
        m = re.search(r"term beginning Jan(?:uary)? 20, (\d{4})", rules)
        if not m:
            raise ValueError("party: no inauguration year in the rules")
        year = int(m.group(1)) - 1
        mk = _open_markets(get_json, event_ticker=f"KXPRESPARTY-{year}")
        out = {}
        for o in outcomes:
            hit = [x["ticker"] for x in mk if str(x.get("yes_sub_title") or "").startswith(o)]
            if len(hit) != 1:
                raise ValueError(f"party: no KXPRESPARTY-{year} market for '{o}'")
            out[o] = (hit[0], True)
        return {"label": label, "yes": "Democratic" if "Democratic" in out else sorted(out)[0],
                "outcomes": out, "decides": _iso(_election_day(year)), "kind": "PRESPARTY"}
    if ruleset == "FRENCHPRES":
        m = re.search(r"in the (\d{4}) presidential election", rules)
        nots = [o for o in outcomes if o.startswith("Not ")]
        if not m or len(nots) != 1 or nots[0][4:] not in outcomes or len(outcomes) != 2:
            raise ValueError("French president: not one name and its 'Not'")
        name = nots[0][4:]
        mk = _open_markets(get_json, event_ticker=f"KXFRENCHPRES-{int(m.group(1)) % 100:02d}")
        hit = [x["ticker"] for x in mk if str(x.get("yes_sub_title") or "") == name]
        if len(hit) != 1:
            raise ValueError(f"French president: no market for '{name}'")
        fr = re.search(r"first round is held on " + _DATE, rules)
        return {"label": label, "yes": name,
                "outcomes": {name: (hit[0], True), nots[0]: (hit[0], False)},
                "decides": _iso(_date(fr.group(1), "12:00 AM")) if fr else None,
                "kind": "FRENCHPRES_RIGHT" if name in FRENCH_RIGHT else "FRENCHPRES"}
    if ruleset == "AGREEMENT":
        m = re.search(r'"(Before ' + _DATE + r')" market of the (.+?) event', rules)
        if not m or outcomes != {"Agreed", "Not agreed"}:
            raise ValueError("agreement: no 'Before <date>' market named")
        series = AGREEMENT_SERIES.get(m.group(3).strip())
        if not series:
            raise ValueError(f"agreement: no series known for '{m.group(3)}'")
        hit = [x["ticker"] for x in _open_markets(get_json, series_ticker=series)
               if str(x.get("yes_sub_title") or "") == m.group(1)]
        if len(hit) != 1:
            raise ValueError(f"agreement: no {series} market '{m.group(1)}'")
        return {"label": label, "yes": "Agreed",
                "outcomes": {"Agreed": (hit[0], True), "Not agreed": (hit[0], False)},
                "decides": None, "kind": "AGREEMENT"}
    if ruleset == "POLITICALSTAT" and "hormuz" in label.lower():
        m = re.search(r"PortWatch is above 60 at any time before " + _DATE, rules)
        if not m or outcomes != {"Back to normal", "Not back to normal"}:
            raise ValueError("Hormuz: not the PortWatch-above-60 rule")
        want = f"Before {m.group(1)}"
        hit = [x["ticker"] for x in _open_markets(get_json, series_ticker="KXHORMUZNORM")
               if str(x.get("yes_sub_title") or "") == want]
        if len(hit) != 1:
            raise ValueError(f"Hormuz: no KXHORMUZNORM market '{want}'")
        return {"label": label, "yes": "Back to normal",
                "outcomes": {"Back to normal": (hit[0], True),
                             "Not back to normal": (hit[0], False)},
                "decides": None, "kind": "HORMUZ"}
    raise ValueError(f"no pricer for the yes/no leg '{label}' ({ruleset})")


def _number_leg(label: str, ruleset: str, rules: str, now_ts: float) -> dict:
    """The number leg's map row (the hand rows' models and bands) or ValueError."""
    now = datetime.fromtimestamp(now_ts, timezone.utc)
    if ruleset == "WTIDIR":
        m = re.search(r"settlement price of WTI Crude Oil on " + _DATE + r" at (\d{1,2}:\d{2} [AP]M) ET",
                      rules)
        c = re.search(r"based on the ([A-Z][a-z]+) (\d{4}) contract", rules)
        if not m or not c:
            raise ValueError("WTI: no settlement date or contract in the rules")
        at = _date(m.group(1), m.group(2))
        yr, mo = int(c.group(2)), _MON[c.group(1)[:3].lower()]
        return {"label": label, "model": "lognormal", "kind": "WTIDIR",
                "fwd": [_fut("CL", yr, mo), _fut("CL", yr, mo - 1), _fut("CL", yr, mo + 1)],
                "front": "CL=F", "fwd_band": 0.03, "vol": 0.32, "vol_band": (0.26, 0.40),
                "at": _iso(at), "decides": _iso(at)}
    if ruleset == "AAAGAS":
        m = re.search(r"AAA national average price for regular gasoline on " + _DATE, rules)
        if not m:
            raise ValueError("gas: no AAA date in the rules")
        at = _date(m.group(1), "9:00 AM")
        return {"label": label, "model": "lognormal", "kind": "AAAGAS",
                "fwd": [_fut("RB", at.year, at.month + 1), _fut("RB", at.year, at.month),
                        _fut("RB", at.year, at.month + 2)],
                "front": "RB=F", "fwd_add": 1.00, "fwd_band": 0.04, "vol": 0.25,
                "vol_band": (0.19, 0.32), "at": _iso(at), "decides": _iso(at)}
    if ruleset == "INX":
        m = re.search(r"from its closing value on " + _DATE + r" to its closing value on " + _DATE,
                      rules)
        if not m:
            raise ValueError("S&P: no window in the rules")
        s, e = _date(m.group(1), "4:00 PM"), _date(m.group(2), "4:00 PM")
        if (s - now).days < 30:
            raise ValueError("S&P: the window starts within 30 days (it needs the live index)")
        y = max((e - s).days / 365.25, 0.05)
        r = math.sqrt(y)
        return {"label": label, "model": "normal", "kind": "INX",
                "mean": round(7.0 * y, 3), "mean_band": (round(3.0 * y, 3), round(11.0 * y, 3)),
                "sd": round(17.0 * r, 3), "sd_band": (round(14.0 * r, 3), round(20.0 * r, 3)),
                "decides": _iso(s)}
    if ruleset == "YOYCPI":
        m = re.search(r"for ([A-Z][a-z]+) (\d{4}), as first published", rules)
        if not m:
            raise ValueError("CPI: no month in the rules")
        mo, yr = _MON[m.group(1)[:3].lower()], int(m.group(2))
        if (yr - now.year) * 12 + (mo - now.month) < 12:
            raise ValueError("CPI: under a year out (it needs a market read, not the prior)")
        rel = datetime(yr + (mo == 12), mo % 12 + 1, 15, 13, 30, tzinfo=timezone.utc)
        return {"label": label, "model": "normal", "kind": "YOYCPI", "mean": 2.7,
                "mean_band": (2.3, 3.2), "sd": 1.0, "sd_band": (0.8, 1.3), "decides": _iso(rel)}
    if ruleset == "GDPYEAR":
        m = re.search(r"real gross domestic product \(GDP\) for (\d{4})", rules)
        if not m:
            raise ValueError("GDP: no year in the rules")
        yr = int(m.group(1))
        if yr <= now.year:
            raise ValueError("GDP: the year is under way (it needs a market read, not the prior)")
        return {"label": label, "model": "normal", "kind": "GDPYEAR", "mean": 1.9,
                "mean_band": (1.4, 2.4), "sd": 1.4, "sd_band": (1.1, 1.8),
                "decides": _iso(datetime(yr + 1, 1, 30, 13, 30, tzinfo=timezone.utc))}
    if ruleset == "PAYROLLS":
        m = re.search(r"total nonfarm payroll employment from ([A-Z][a-z]+) (\d{4}) to "
                      r"([A-Z][a-z]+) (\d{4})", rules)
        if not m:
            raise ValueError("payrolls: no window in the rules")
        m0, y0 = _MON[m.group(1)[:3].lower()], int(m.group(2))
        m1, y1 = _MON[m.group(3)[:3].lower()], int(m.group(4))
        if (y1 - y0) * 12 + (m1 - m0) != 12:
            raise ValueError("payrolls: not a 12-month change (the prior is a year's)")
        if (y0 - now.year) * 12 + (m0 - now.month) < 1:
            raise ValueError("payrolls: the window is under way (it needs the data so far, "
                             "not the prior)")
        return {"label": label, "model": "normal", "kind": "PAYROLLS", "mean": 550000.0,
                "mean_band": (150000.0, 950000.0), "sd": 850000.0,
                "sd_band": (650000.0, 1100000.0), "decides": _iso(_jobs_day(y1, m1))}
    if ruleset == "ECONSTATB" and re.search(r"unemployment rate \(U-3\)", rules, re.I):
        m = re.search(r"unemployment rate \(U-3\) for ([A-Z][a-z]+) (\d{4})", rules, re.I)
        if not m:
            raise ValueError("U-3: no month in the rules")
        mo, yr = _MON[m.group(1)[:3].lower()], int(m.group(2))
        if (yr - now.year) * 12 + (mo - now.month) < 12:
            raise ValueError("U-3: under a year out (it needs a market read, not the prior)")
        return {"label": label, "model": "normal", "kind": "U3", "mean": 4.5,
                "mean_band": (4.1, 4.9), "sd": 0.8, "sd_band": (0.6, 1.1),
                "decides": _iso(_jobs_day(yr, mo))}
    if ruleset == "FXEURUSD":
        m = re.search(r"the start time is " + _DATE + r" at (\d{1,2}:\d{2} [AP]M) ET and the end time is "
                      + _DATE + r" at (\d{1,2}:\d{2} [AP]M) ET", rules)
        if not m:
            raise ValueError("EUR/USD: no window in the rules")
        s, e = _date(m.group(1), m.group(2)), _date(m.group(3), m.group(4))
        if (s - now).days < 7:
            raise ValueError("EUR/USD: the window starts within 7 days (it needs the live rate)")
        sd = 7.5 * math.sqrt(max((e - s).total_seconds() / YEAR_S, 1e-4)) * 1.1
        return {"label": label, "model": "normal", "kind": "FXEURUSD", "mean": 0.0,
                "mean_band": (-0.5, 0.5), "sd": round(sd, 3),
                "sd_band": (round(sd * 0.8, 3), round(sd * 1.35, 3)), "decides": _iso(s)}
    raise ValueError(f"no pricer for the number leg '{label}' ({ruleset})")


# the link (DELTA, its band) by (yes/no kind, number kind): the hand rows' own
LINKS = {("AGREEMENT", "WTIDIR"): (-0.08, (-0.20, 0.0)),
         ("AGREEMENT", "AAAGAS"): (-0.05, (-0.15, 0.0)),
         ("HORMUZ", "AAAGAS"): (-0.06, (-0.15, 0.0)),
         ("HORMUZ", "WTIDIR"): (-0.08, (-0.20, 0.0)),
         ("FRENCHPRES_RIGHT", "FXEURUSD"): (-1.5, (-3.0, 0.0)),
         ("PRESPARTY", "INX"): (0.0, (-4.0, 4.0)),
         ("PRESPARTY", "YOYCPI"): (0.0, (-0.5, 0.5)),
         ("PRESPARTY", "GDPYEAR"): (0.0, (-0.5, 0.5)),
         ("PRESPARTY", "PAYROLLS"): (0.0, (-300000.0, 300000.0)),
         ("PRESPARTY", "U3"): (0.0, (-0.3, 0.3))}


def auto_map(event: str, markets: List[dict], get_json, now_ts: float) -> dict:
    """An EVENTS-shaped row for a combo event from its own rules ({binary, number, delta,
    delta_band, auto: True}), or ValueError with the reason it cannot be mapped."""
    if not markets:
        raise ValueError("no open markets")
    rules = _norm(" ".join(str(markets[0].get(k) or "") for k in ("rules_primary", "rules_secondary")))
    comps = _components(rules)
    if len(comps) != 2:
        raise ValueError("the rules name no two Kalshi rulesets")
    # the cells may label a leg more briefly than the rules ("Rate" for "Federal Funds Rate
    # Decision"): then the cells' own labels, paired with the rulesets in order
    if cell_split(markets[0], comps[0][0], comps[1][0]) is None:
        texts = cell_texts(markets[0])
        m = next((x for x in (re.fullmatch(r"([^:,]+):\s*.+?,\s*([^:,]+):\s*.+", t)
                              for t in texts) if x), None)
        if not m:
            raise ValueError(f"cell '{texts[0] if texts else ''}' is not '<leg>: ..., <leg>: ...'")
        comps = [(m.group(1).strip(), comps[0][1]), (m.group(2).strip(), comps[1][1])]
    vals: Dict[str, set] = {}
    for (la, _ra), (lb, _rb) in [(comps[0], comps[1])]:
        for mk in markets:
            sp = cell_split(mk, la, lb)
            if sp is None:
                raise ValueError(f"cell '{mk.get('yes_sub_title')}' is not '{la}: ..., {lb}: ...'")
            vals.setdefault(la, set()).add(sp[0])
            vals.setdefault(lb, set()).add(sp[1])
    numeric = [(l, r) for l, r in comps if all(parse_bin(v) is not None for v in vals[l])]
    binary = [(l, r) for l, r in comps if (l, r) not in numeric]
    if len(numeric) != 1 or len(binary) != 1:
        raise ValueError(f"not one yes/no leg and one number ({', '.join(f'{l} ({r})' for l, r in comps)})")
    (bl, br), (nl, nr) = binary[0], numeric[0]
    b = _binary_leg(bl, br, vals[bl], rules, get_json, now_ts)
    n = _number_leg(nl, nr, rules, now_ts)
    if (b["kind"], n["kind"]) in LINKS:
        d, band = LINKS[(b["kind"], n["kind"])]
    elif n["model"] == "lognormal":
        d, band = 0.0, (-0.10, 0.10)
    else:
        d, band = 0.0, (round(-0.25 * n["sd"], 3), round(0.25 * n["sd"], 3))
    return {"binary": b, "number": n, "delta": d, "delta_band": band, "auto": True}


# ---- bins -----------------------------------------------------------------------------------

_NUM = r"([+\-−]?\$?[+\-−]?[\d,]*\.?\d+(?:\s*(?:thousand|million|billion|[kmb])\b)?)\s*%?"
_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "k": 1e3, "m": 1e6, "b": 1e9}


def _num(s: str) -> float:
    s = s.replace("−", "-").replace("$", "").replace(",", "").replace("+", "").strip().lower()
    m = re.fullmatch(r"(.+?)\s*(thousand|million|billion|k|m|b)", s)
    if m:
        return float(m.group(1)) * _SCALE[m.group(2)]
    return float(s)


def parse_bin(text: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """(lo, hi) as written -- None for an open end -- or None if the text is no range:
    'Below $60' (None, 60), '$60 to $74.99' (60, 74.99), '$90 or above' (90, None),
    '1.9% or below' (None, 1.9), '-10% to 0%' (-10, 0), 'Below -10%' (None, -10),
    '500,000 to 1 million' (500000, 1000000)."""
    t = " ".join((text or "").strip().split()).lower()
    m = re.fullmatch(r"(?:below|under|less than)\s+" + _NUM, t)
    if m:
        return None, _num(m.group(1))
    m = re.fullmatch(_NUM + r"\s+or\s+(?:below|less|lower|under)", t)
    if m:
        return None, _num(m.group(1))
    m = re.fullmatch(_NUM + r"\s+(?:or|and)\s+(?:above|more|higher|over)", t)
    if m:
        return _num(m.group(1)), None
    m = re.fullmatch(r"(?:above|over|more than|at least)\s+" + _NUM, t)
    if m:
        return _num(m.group(1)), None
    m = re.fullmatch(_NUM + r"\s+to\s+" + _NUM, t)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        return (a, b) if a <= b else None
    return None


def bin_edges(bins: Dict[str, Tuple[Optional[float], Optional[float]]]
              ) -> Dict[str, Tuple[float, float]]:
    """{bin text: [lo, hi)} on the underlying for a grid's bins as written: open tails at each
    end, and each pair of neighbours split at the midpoint of their facing edges. Raises
    ValueError unless the bins tile the line (open both ends, no overlap, small gaps)."""
    rows = sorted(bins.items(), key=lambda kv: (-math.inf if kv[1][0] is None else kv[1][0],
                                                math.inf if kv[1][1] is None else kv[1][1]))
    if len(rows) < 2:
        raise ValueError("fewer than 2 bins")
    if rows[0][1][0] is not None or rows[-1][1][1] is not None:
        raise ValueError("the grid's ends are not open")
    cuts: List[float] = []
    for (ta, (_alo, ahi)), (tb, (blo, _bhi)) in zip(rows, rows[1:]):
        if ahi is None or blo is None:
            raise ValueError(f"bins '{ta}' / '{tb}' overlap")
        if blo < ahi:
            raise ValueError(f"bins '{ta}' / '{tb}' overlap")
        if blo - ahi > max(0.11, 0.02 * max(abs(ahi), abs(blo))):
            raise ValueError(f"gap between '{ta}' and '{tb}'")
        cuts.append((ahi + blo) / 2.0)
    edges = [-math.inf] + cuts + [math.inf]
    return {t: (edges[i], edges[i + 1]) for i, (t, _b) in enumerate(rows)}


# ---- the model ------------------------------------------------------------------------------

def _ncdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def num_cdf(x: float, num: dict, centre: float, scale: float, shift: float) -> float:
    """P(number < x) under the leg's model: 'normal' N(centre + shift, scale); 'lognormal' a
    price with forward `centre` and total log sd `scale` (vol x sqrt(T)), its log mean moved
    by `shift`."""
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    if num["model"] == "lognormal":
        if x <= 0:
            return 0.0
        mu = math.log(centre) - 0.5 * scale * scale + shift
        return _ncdf((math.log(x) - mu) / scale)
    return _ncdf((x - (centre + shift)) / scale)


def cell_p(p_out: float, yes: bool, p_yes: float, delta: float, edges: Tuple[float, float],
           num: dict, centre: float, scale: float) -> float:
    """One cell: P(outcome) x P(number in [lo, hi) | that outcome's world)."""
    shift = (1.0 - p_yes) * delta if yes else -p_yes * delta
    lo, hi = edges
    return max(0.0, p_out) * max(0.0, num_cdf(hi, num, centre, scale, shift)
                                 - num_cdf(lo, num, centre, scale, shift))


def num_points(num: dict, live: Optional[dict], now_ts: float) -> Tuple[List[float], List[float]]:
    """(centres, scales) at low / mid / high for the band, the middle one the fair's."""
    if num["model"] == "lognormal":
        # the futures price, plus a fixed add-on where the leg is not the contract itself
        # (the pump price = wholesale RBOB + taxes and margin)
        f = float(live["fwd"]) + float(num.get("fwd_add", 0.0))
        at = _parse_iso(num["at"])
        t_y = max((at.timestamp() - now_ts) / YEAR_S, 1.0 / 365.25) if at else 1.0
        rt = math.sqrt(t_y)
        b = float(num.get("fwd_band", 0.0))
        v0, (vl, vh) = float(num["vol"]), num["vol_band"]
        return [f * (1 - b), f, f * (1 + b)], [vl * rt, v0 * rt, vh * rt]
    m0, (ml, mh) = float(num["mean"]), num["mean_band"]
    s0, (sl, sh) = float(num["sd"]), num["sd_band"]
    return [ml, m0, mh], [sl, s0, sh]


def grid_fair(cells: List[dict], binary: dict, legs: Dict[str, dict], num: dict,
              live_num: Optional[dict], delta: float, delta_band: Tuple[float, float],
              now_ts: float) -> Dict[str, dict]:
    """{ticker: {p, p_lo, p_hi}} for one event's priced cells. `cells`: {ticker, outcome, edges}.
    `legs`: the yes/no leg reads by market ticker ({bid, ask, mid})."""
    def p_of(outcome: str, which: str) -> float:
        mk, is_yes = binary["outcomes"][outcome]
        r = legs[mk]
        if is_yes:
            return float(r[which])
        flip = {"bid": "ask", "mid": "mid", "ask": "bid"}[which]
        return 1.0 - float(r[flip])

    # the YES world's weight for the link: P(yes outcome) over the outcomes' total
    tot = sum(p_of(o, "mid") for o in binary["outcomes"])
    p_yes = p_of(binary["yes"], "mid") / tot if tot > 0 else 0.5
    centres, scales = num_points(num, live_num, now_ts)
    out: Dict[str, dict] = {}
    for c in cells:
        o, edges = c["outcome"], c["edges"]
        yes = o == binary["yes"]

        def at(w="mid", d=delta, ce=centres[1], sc=scales[1]) -> float:
            return cell_p(p_of(o, w), yes, p_yes, d, edges, num, ce, sc)
        fair = at()
        # each input at its low and high with the rest at the fair's, the moves combined in
        # quadrature: the inputs' errors are independent, so all four at their extremes at
        # once (a 1-in-thousands corner) would overstate the band
        pairs = ((at(w="bid"), at(w="ask")), (at(ce=centres[0]), at(ce=centres[2])),
                 (at(sc=scales[0]), at(sc=scales[2])),
                 (at(d=delta_band[0]), at(d=delta_band[1])))
        up = math.sqrt(sum(max(0.0, max(a, b) - fair) ** 2 for a, b in pairs))
        dn = math.sqrt(sum(max(0.0, fair - min(a, b)) ** 2 for a, b in pairs))
        out[c["ticker"]] = {"p": round(fair, 5), "p_lo": round(max(0.0, fair - dn), 5),
                            "p_hi": round(min(1.0, fair + up), 5)}
    return out


def split_title(title: str, a: str, b: str) -> Optional[Tuple[str, str]]:
    """('<a>: x, <b>: y' or '<b>: y, <a>: x') -> (x, y), else None."""
    t = " ".join((title or "").split())
    for first, second, flip in ((a, b, False), (b, a, True)):
        m = re.fullmatch(re.escape(first) + r":\s*(.+?),\s*" + re.escape(second) + r":\s*(.+)", t)
        if m:
            x, y = m.group(1).strip(), m.group(2).strip()
            return (y, x) if flip else (x, y)
    return None


_RULES_CELL = re.compile(r"If ALL of the following occur(?: for [^:]+)?:\s*(.+?),?\s+then the "
                         r"market resolves to Yes", re.I)


def cell_texts(m: dict) -> List[str]:
    """A cell's '<leg>: x, <leg>: y' candidates: its title, then its own rules ("If ALL of the
    following occur: Party: Democratic, Jobs added in 2029: 0 to 500,000, then the market
    resolves to Yes."). Since 11/19 Kalshi titles the party grids' cells short ("Democrat,
    0-500K"), and only the rules still name the legs."""
    out = [" ".join(str(m.get("yes_sub_title") or "").split())]
    r = _RULES_CELL.search(_norm(str(m.get("rules_primary") or "")))
    if r:
        out.append(r.group(1).strip())
    return [t for t in out if t]


def cell_split(m: dict, a: str, b: str) -> Optional[Tuple[str, str]]:
    """(the a leg's value, the b leg's) from the first of the cell's texts that reads
    '<a>: x, <b>: y' (either order), else None."""
    for t in cell_texts(m):
        sp = split_title(t, a, b)
        if sp is not None:
            return sp
    return None


# ---- reads ----------------------------------------------------------------------------------

def _http_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (listed_combo_fair)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def yahoo_last(symbol: str, get: Callable = None) -> Tuple[float, float]:
    """(last price, its exchange time) of a Yahoo Finance futures symbol (delayed quote)."""
    get = get or _http_json
    j = get("https://query1.finance.yahoo.com/v8/finance/chart/"
            + urllib.parse.quote(symbol) + "?range=1d&interval=1d")
    meta = ((j.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
    px = meta.get("regularMarketPrice")
    if not px or float(px) <= 0:
        raise ValueError(f"{symbol}: no price")
    return float(px), float(meta.get("regularMarketTime") or 0)


class ListedComboWatch:
    """The refresher's state: market lists every META_REFRESH_S, the yes/no legs every
    refresh(), futures every FUT_REFRESH_S; `snap` is what the gate reads.

    snap = {"checked_ts", "legs": {key: {kind, bid, ask, mid, ts, hold_until, err}},
    "events": {event: {legs, ts, hold_until, cut_at, n, n_priced, sum_p, err}},
    "markets": {ticker: {event, outcome, bin, p, p_lo, p_hi, ts, hold_until, cut} or
    {event, err}}, "unmapped": [events], "errors", "model"}"""

    def __init__(self, get_json: Optional[Callable[[str, dict], dict]],
                 events: Callable[[], Iterable[str]] = lambda: sorted(EVENTS),
                 http: Optional[Callable] = None, event_map: Optional[Dict[str, dict]] = None):
        self.get_json = get_json
        self.events_fn = events
        self.http = http or _http_json
        self.map = EVENTS if event_map is None else event_map
        self.meta_at = 0.0
        self.markets: Dict[str, List[dict]] = {}          # event -> its open markets
        self.leg: Dict[str, dict] = {}                   # leg key -> last good read
        self.hist: Dict[str, Deque[Tuple[float, float]]] = {}
        self.hold_until: Dict[str, float] = {}
        self.fut_at = 0.0
        self.auto: Dict[str, dict] = {}                   # event -> auto row, or {"err": why}
        self.auto_at: Dict[str, float] = {}
        self.cfgs: Dict[str, dict] = {}                   # this refresh's rows, hand over auto
        self.snap: Optional[dict] = None

    # -- reads
    def _list_markets(self, event: str) -> List[dict]:
        out: List[dict] = []
        cursor = None
        for _ in range(10):
            params = {"event_ticker": event, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            body = self.get_json("/markets", params) if self.get_json else {}
            out += [m for m in (body or {}).get("markets") or [] if isinstance(m, dict)]
            cursor = (body or {}).get("cursor")
            if not cursor:
                break
        return out

    def _jump(self, key: str, now_ts: float, x: float, thr: float, rel: bool, hold_s: float) -> None:
        h = self.hist.setdefault(key, deque())
        h.append((now_ts, x))
        while h and now_ts - h[0][0] > LEG_JUMP_WINDOW_S:
            h.popleft()
        lo, hi = min(v for _t, v in h), max(v for _t, v in h)
        moved = (hi / lo - 1.0) if (rel and lo > 0) else (hi - lo)
        if moved >= thr:
            self.hold_until[key] = now_ts + hold_s

    def _read_binary(self, mk: str, now_ts: float, errors: List[str]) -> None:
        try:
            body = self.get_json(f"/markets/{mk}", {}) if self.get_json else {}
            m = (body or {}).get("market") or {}
            b, a = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
            b, a = (float(b) if b not in (None, "") else None), (float(a) if a not in (None, "") else None)
            if m.get("status") not in ("active", "open"):
                raise ValueError(f"status {m.get('status')}")
            if b is None or a is None or not (0.0 < b <= a < 1.0):
                raise ValueError(f"no two-sided book ({b} / {a})")
            if a - b > LEG_MAX_SPREAD + 1e-9:
                raise ValueError(f"book {100 * (a - b):.0f}c wide")
            mid = (a + b) / 2.0
            self.leg[mk] = {"kind": "binary", "bid": b, "ask": a, "mid": mid, "ts": now_ts, "err": None}
            self._jump(mk, now_ts, mid * 100.0, LEG_JUMP_C, False, LEG_HOLD_S)
        except Exception as e:  # noqa: BLE001 -- keep the last good read; its ts ages out
            r = self.leg.setdefault(mk, {"kind": "binary", "ts": None})
            r["err"] = f"{type(e).__name__}: {str(e)[:80]}"
            errors.append(f"{mk}: {r['err']}")

    def _read_futures(self, num: dict, key: str, now_ts: float, errors: List[str]) -> None:
        """The forward (first symbol that answers) and the front month (jumps)."""
        why = []
        for sym in num["fwd"]:
            try:
                px, xt = yahoo_last(sym, self.http)
                self.leg[key] = {"kind": "futures", "fwd": px, "symbol": sym, "exch_ts": xt,
                                 "ts": now_ts, "err": None}
                break
            except Exception as e:  # noqa: BLE001
                why.append(f"{sym}: {type(e).__name__}: {str(e)[:60]}")
        else:
            r = self.leg.setdefault(key, {"kind": "futures", "ts": None})
            r["err"] = "; ".join(why)[:200]
            errors.append(f"{key}: {r['err']}")
        if num.get("front"):
            try:
                fpx, _xt = yahoo_last(num["front"], self.http)
                self.leg.setdefault(key, {"kind": "futures", "ts": None})["front"] = fpx
                self._jump(key, now_ts, fpx, FUT_JUMP_PCT, True, FUT_HOLD_S)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{num['front']}: {type(e).__name__}: {str(e)[:60]}")

    # -- the snapshot
    def refresh(self, now_ts: Optional[float] = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        errors: List[str] = []
        wanted = sorted(set(self.events_fn() or ()))
        if now_ts - self.meta_at >= META_REFRESH_S or any(e not in self.markets for e in wanted):
            got = 0
            for ev in wanted:
                try:
                    self.markets[ev] = self._list_markets(ev)
                    got += 1
                except Exception as e:  # noqa: BLE001 -- keep the last list
                    errors.append(f"{ev} markets: {type(e).__name__}: {str(e)[:80]}")
            if got:
                self.meta_at = now_ts
        # AUTO-MAPPING: an event with no hand row is mapped from its own rules (a failure is
        # retried every META_REFRESH_S: a leg market may list later)
        for ev in wanted:
            if ev in self.map:
                continue
            a = self.auto.get(ev)
            if a is not None and ("err" not in a or now_ts - self.auto_at.get(ev, 0.0) < META_REFRESH_S):
                continue
            mk = [m for m in self.markets.get(ev) or [] if m.get("status") in ("active", "open")]
            try:
                self.auto[ev] = (auto_map(ev, mk, self.get_json, now_ts) if self.get_json
                                 else {"err": "no reader"})
            except Exception as e:  # noqa: BLE001 -- unmapped, with its reason
                self.auto[ev] = {"err": str(e)[:200] or type(e).__name__}
            self.auto_at[ev] = now_ts
        self.cfgs = {e: (self.map[e] if e in self.map else self.auto[e]) for e in wanted
                     if e in self.map or (e in self.auto and "err" not in self.auto[e])}
        mapped = sorted(self.cfgs)
        unmapped = [e for e in wanted if e not in self.cfgs]
        # legs: every yes/no market once; futures every FUT_REFRESH_S
        bins_needed = {}
        for ev in mapped:
            cfg = self.cfgs[ev]
            for mk, _is_yes in set(cfg["binary"]["outcomes"].values()):
                bins_needed[mk] = True
        for mk in sorted(bins_needed):
            self._read_binary(mk, now_ts, errors)
        if now_ts - self.fut_at >= FUT_REFRESH_S:
            for ev in mapped:
                num = self.cfgs[ev]["number"]
                if num["model"] == "lognormal":
                    self._read_futures(num, f"fut {ev}", now_ts, errors)
            self.fut_at = now_ts
        legs_out = {k: dict(v, hold_until=self.hold_until.get(k)) for k, v in self.leg.items()}
        ev_out: Dict[str, dict] = {}
        mk_out: Dict[str, dict] = {}
        for ev in mapped:
            ev_out[ev], cells = self._price_event(ev, now_ts)
            ev_out[ev]["mapping"] = "hand" if ev in self.map else "auto"
            mk_out.update(cells)
        unmapped_why, unmapped_detail = {}, {}
        for ev in unmapped:
            why = (self.auto.get(ev) or {}).get("err") or "no leg map"
            unmapped_why[ev] = why
            ev_out[ev] = {"err": f"unmapped: {why}", "mapping": None}
            mk = self.markets.get(ev) or []
            rules = _norm(" ".join(str((mk[0] if mk else {}).get(k) or "")
                                   for k in ("rules_primary", "rules_secondary")))
            unmapped_detail[ev] = {
                "cells": len(mk), "titles": sorted({str(m.get("yes_sub_title") or "") for m in mk})[:4],
                "components": [f"{l} ({r})" for l, r in _components(rules)],
                "close": str((mk[0] if mk else {}).get("close_time") or "")}
        # what each auto row read, for the bot's notice
        auto_out = {ev: {"binary": {o: list(v) for o, v in c["binary"]["outcomes"].items()},
                         "binary_label": c["binary"]["label"], "number_label": c["number"]["label"],
                         "number": {k: c["number"].get(k) for k in ("model", "kind", "fwd", "fwd_add",
                                                                    "vol", "mean", "sd", "at", "decides")
                                    if c["number"].get(k) is not None},
                         "delta": c["delta"], "delta_band": list(c["delta_band"])}
                    for ev, c in self.cfgs.items() if c.get("auto")}
        # the series with a priced event: the bot's listed combo family admits these
        priced_series = sorted({ev.split("-")[0] for ev, e in ev_out.items() if e.get("n_priced")})
        self.snap = {"checked_ts": now_ts, "legs": legs_out, "events": ev_out, "markets": mk_out,
                     "unmapped": unmapped, "unmapped_why": unmapped_why,
                     "unmapped_detail": unmapped_detail, "auto": auto_out,
                     "priced_series": priced_series, "errors": errors,
                     "model": {"leg_max_spread": LEG_MAX_SPREAD, "leg_jump_c": LEG_JUMP_C,
                               "leg_jump_window_s": LEG_JUMP_WINDOW_S, "leg_hold_s": LEG_HOLD_S,
                               "fut_jump_pct": FUT_JUMP_PCT, "fut_hold_s": FUT_HOLD_S,
                               "cutoff_h": CUTOFF_H}}
        return self.snap

    def _price_event(self, ev: str, now_ts: float) -> Tuple[dict, Dict[str, dict]]:
        cfg = self.cfgs[ev]
        binary, num = cfg["binary"], cfg["number"]
        markets = [m for m in self.markets.get(ev) or []
                   if m.get("status") in ("active", "open")]
        leg_keys = sorted({mk for mk, _y in binary["outcomes"].values()})
        fut_key = f"fut {ev}" if num["model"] == "lognormal" else None
        live_keys = leg_keys + ([fut_key] if fut_key else [])
        hold = max([self.hold_until.get(k, 0.0) for k in live_keys] + [0.0])
        ts_list = [(self.leg.get(k) or {}).get("ts") for k in live_keys]
        ts = None if any(t is None for t in ts_list) else min(ts_list)
        # cut: the earliest scheduled decision (or close), less CUTOFF_H
        dec = [_parse_iso(x) for x in (binary.get("decides"), num.get("decides")) if x]
        closes = [_parse_iso(m.get("close_time")) for m in markets]
        anchors = [d.timestamp() for d in dec + closes if d is not None]
        cut_at = (min(anchors) - CUTOFF_H * 3600.0) if anchors else None
        evrow = {"legs": live_keys, "ts": ts, "hold_until": hold or None, "cut_at": cut_at,
                 "n": len(markets), "n_priced": 0, "sum_p": None, "err": None}

        def fail(why: str) -> Tuple[dict, Dict[str, dict]]:
            evrow["err"] = why
            return evrow, {str(m.get("ticker")): {"event": ev, "err": why} for m in markets}

        if not markets:
            return fail("no open markets read")
        bad = [k for k in live_keys if (self.leg.get(k) or {}).get("err") or not self.leg.get(k)]
        if bad and any((self.leg.get(k) or {}).get("ts") is None for k in bad):
            return fail("no read of " + ", ".join(bad))
        # parse every cell: outcome x bin
        cells, bins = [], {}
        for m in markets:
            t = str(m.get("ticker") or "")
            sp = cell_split(m, binary["label"], num["label"])
            if sp is None or sp[0] not in binary["outcomes"]:
                return fail(f"{t}: legs not '{binary['label']}: ..., {num['label']}: ...'")
            b = parse_bin(sp[1])
            if b is None:
                return fail(f"{t}: bin '{sp[1]}' not a range")
            bins[sp[1]] = b
            cells.append({"ticker": t, "outcome": sp[0], "bin": sp[1]})
        try:
            edges = bin_edges(bins)
        except ValueError as e:
            return fail(f"bins: {e}")
        want = len(binary["outcomes"]) * len(bins)
        if len(cells) != want or len({(c["outcome"], c["bin"]) for c in cells}) != want:
            return fail(f"grid has {len(cells)} cells, {want} expected")
        for c in cells:
            c["edges"] = edges[c["bin"]]
        live_num = self.leg.get(fut_key) if fut_key else None
        if fut_key and not (live_num and live_num.get("fwd")):
            return fail("no forward read")
        legs = {k: self.leg[k] for k in leg_keys}
        priced = grid_fair(cells, binary, legs, num, live_num, float(cfg["delta"]),
                           tuple(cfg["delta_band"]), now_ts)
        if cfg.get("auto"):
            # an auto row must price a whole grid: its outcomes' odds sum to ~1
            tot = sum(r["p"] for r in priced.values())
            if not 0.95 <= tot <= 1.05:
                return fail(f"auto map prices the grid at {tot:.3f}, not ~1")
        cut = cut_at is not None and now_ts >= cut_at
        out: Dict[str, dict] = {}
        for c in cells:
            row = priced[c["ticker"]]
            out[c["ticker"]] = dict(row, event=ev, outcome=c["outcome"], bin=c["bin"],
                                    edges=[None if math.isinf(x) else x for x in c["edges"]],
                                    ts=ts, hold_until=hold or None, cut=cut)
        evrow.update(n_priced=len(out), sum_p=round(sum(r["p"] for r in out.values()), 4))
        return evrow, out


def write_status(path: str, snap: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


def public_get(path: str, params: Optional[dict] = None, tries: int = 4) -> dict:
    """Kalshi's public endpoint, unsigned, with backoff on a 429 / 5xx (the dry read's reader;
    the bot hands the watch its own signed reader)."""
    url = "https://api.elections.kalshi.com/trade-api/v2" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last: Optional[Exception] = None
    for i in range(tries):
        try:
            return _http_json(url)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2.0 * (i + 1))
    raise last if last else RuntimeError(f"GET {path} failed")


if __name__ == "__main__":                               # a dry read, no orders
    import sys
    evs = sys.argv[1].split(",") if len(sys.argv) > 1 else sorted(EVENTS)
    w = ListedComboWatch(public_get, lambda: evs)
    snap = w.refresh()
    print("errors:", snap["errors"] or "none")
    for k, v in sorted(snap["legs"].items()):
        print(f"leg {k}: " + ", ".join(f"{a}={v[a]}" for a in ("bid", "ask", "mid", "fwd", "symbol", "front", "err")
                                       if v.get(a) is not None))
    for ev, e in sorted(snap["events"].items()):
        print(f"\n{ev}: {e.get('n_priced', 0)}/{e.get('n', 0)} priced, sum {e.get('sum_p')}"
              + (f", ERR {e['err']}" if e.get("err") else ""))
        for t, m in sorted(snap["markets"].items()):
            if m.get("event") == ev and "p" in m:
                print(f"  {t:42s} {100 * m['p']:5.1f}c  band {100 * m['p_lo']:5.1f}-{100 * m['p_hi']:5.1f}")
