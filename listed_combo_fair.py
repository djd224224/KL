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
the pump price on a date, the S&P 500's change over 2029, December 2029 CPI, 2029 GDP, EUR/USD
across the French vote). A cell is
P(yes/no leg = its outcome) x P(number in its bin | that outcome).

LEGS (EVENTS, per combo event: a new event stays unpriced -- the gate keeps it out -- until its
legs are mapped here; the status file lists it under "unmapped"):
  yes/no   the mid of the Kalshi market the combo's rules name; its own bid / ask feed the band,
           a 'not' outcome is 1 - it. A book wider than LEG_MAX_SPREAD is no read.
  number   'lognormal' (a price on a date: the forward from the named futures contract, vol x
           sqrt(T)) or 'normal' (a % change or a print: mean, sd -- a prior). The bins come from
           the cells' own titles ("Below $60", "$60 to $74.99", "2.0% to 2.9%", "20% or above");
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


# ---- bins -----------------------------------------------------------------------------------

_NUM = r"([+\-−]?\$?[+\-−]?[\d,]*\.?\d+)\s*%?"


def _num(s: str) -> float:
    s = s.replace("−", "-").replace("$", "").replace(",", "").replace("+", "").strip()
    return float(s)


def parse_bin(text: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """(lo, hi) as written -- None for an open end -- or None if the text is no range:
    'Below $60' (None, 60), '$60 to $74.99' (60, 74.99), '$90 or above' (90, None),
    '1.9% or below' (None, 1.9), '-10% to 0%' (-10, 0), 'Below -10%' (None, -10)."""
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
        mapped = [e for e in wanted if e in self.map]
        unmapped = [e for e in wanted if e not in self.map]
        if now_ts - self.meta_at >= META_REFRESH_S or any(e not in self.markets for e in mapped):
            got = 0
            for ev in mapped:
                try:
                    self.markets[ev] = self._list_markets(ev)
                    got += 1
                except Exception as e:  # noqa: BLE001 -- keep the last list
                    errors.append(f"{ev} markets: {type(e).__name__}: {str(e)[:80]}")
            if got:
                self.meta_at = now_ts
        # legs: every yes/no market once; futures every FUT_REFRESH_S
        bins_needed = {}
        for ev in mapped:
            cfg = self.map[ev]
            for mk, _is_yes in set(cfg["binary"]["outcomes"].values()):
                bins_needed[mk] = True
        for mk in sorted(bins_needed):
            self._read_binary(mk, now_ts, errors)
        if now_ts - self.fut_at >= FUT_REFRESH_S:
            for ev in mapped:
                num = self.map[ev]["number"]
                if num["model"] == "lognormal":
                    self._read_futures(num, f"fut {ev}", now_ts, errors)
            self.fut_at = now_ts
        legs_out = {k: dict(v, hold_until=self.hold_until.get(k)) for k, v in self.leg.items()}
        ev_out: Dict[str, dict] = {}
        mk_out: Dict[str, dict] = {}
        for ev in mapped:
            ev_out[ev], cells = self._price_event(ev, now_ts)
            mk_out.update(cells)
        for ev in unmapped:
            ev_out[ev] = {"err": "no leg map for this combo event (listed_combo_fair.EVENTS)"}
        # the series with a priced event: the bot's listed combo family admits these
        priced_series = sorted({ev.split("-")[0] for ev, e in ev_out.items() if e.get("n_priced")})
        self.snap = {"checked_ts": now_ts, "legs": legs_out, "events": ev_out, "markets": mk_out,
                     "unmapped": unmapped, "priced_series": priced_series, "errors": errors,
                     "model": {"leg_max_spread": LEG_MAX_SPREAD, "leg_jump_c": LEG_JUMP_C,
                               "leg_jump_window_s": LEG_JUMP_WINDOW_S, "leg_hold_s": LEG_HOLD_S,
                               "fut_jump_pct": FUT_JUMP_PCT, "fut_hold_s": FUT_HOLD_S,
                               "cutoff_h": CUTOFF_H}}
        return self.snap

    def _price_event(self, ev: str, now_ts: float) -> Tuple[dict, Dict[str, dict]]:
        cfg = self.map[ev]
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
            sp = split_title(str(m.get("yes_sub_title") or ""), binary["label"], num["label"])
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
