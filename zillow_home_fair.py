"""ZILLOW HOME VALUE FAIR (Jack 2026-11-12: "there are also home value markets
KXVENICEHOME-30NOV26, KXSEAHOME-30NOV26 starting at 4:41pm today. can you
build a fair value for this?").

WHAT SETTLES. Kalshi's HOMEVALUE contract: "Will the Zillow Home Value Index
for <home type> in <geography> (Zillow region ID N) be above $K at <Month>
<Year>?" -- the MIDDLE-TIER, SMOOTHED, SEASONALLY ADJUSTED ZHVI for that home
type, the value FIRST published for the month (Zillow's routine restatement
in a later release does not count). The 2026-11 city markets are all
"single-family homes": Zillow Research's uc_sfr_tier_0.33_0.67_sm_sa cut
(the September values line up with every strike ladder: Seattle $891,130 vs
875-905k, Venice $2,042,448 vs 2.035-2.1M, Santa Monica $2,652,619 vs
2.655-2.72M; the all-homes cut is 6-60% lower and matches none).

THE MODEL. Each release (mid-month; 2026-10-16 01:50Z carried September):
  1. adds the new month, whose seasonally adjusted month-over-month change
     follows the recent ones (AR(1) on 36 months of log changes: h=1 RMSE
     0.27%, h=2 0.69%, h=3 1.2% across 4,300 cities and neighborhoods,
     z-scores sd 0.8-1.06 -- HOME_SHAPE_SD_MULT scales it), and
  2. RE-ANCHORS the region's WHOLE HISTORY by one constant factor. Every
     archived vintage shows it (Wayback copies of the metro and state files,
     2026-04 .. 2026-10): the revision is identical in every month of a
     region's history (within-region sd 0.000%), and varies by release:
     4 releases Apr->Aug +1.8% in total, then -0.4%, -0.87%, -0.88% per
     release (US -0.74% on 10/16; Seattle metro -0.94%, LA -0.43%), with a
     cross-region spread of 0.4-0.6% per release at metro/state level. It
     is partly the seasonal factor (raw / adjusted ratio change, cross-metro
     correlation 0.67), partly a drift.
So the first print of target month T, h months after the latest published
month L, is   V_L x exp(AR path over h months) x exp(sum of h re-anchors),
and P(above K) is lognormal with
  mean  log V_L + AR forecast + h x HOME_REANCHOR_MEAN_PCT
  var   (HOME_SHAPE_SD_MULT x AR sd_h)^2
        + h x (HOME_REANCHOR_COMMON_SD_PCT^2 + idiosyncratic sd(geo)^2),
floored at HOME_SD_FLOOR_PCT. The re-anchor mean is the main judgement call
(-0.5%/release: between the seasonal ratio change, about -0.45%, and the
last two releases' -0.9%); it is a knob, and the gate's tolerance band
(incentive_mm HOME_FAIR_TOL_CENTS) is wide enough that a 1-sd miss in it
still keeps quotes on the safe side.

THE FEED. Zillow Research's public CSVs
(files.zillowstatic.com/research/public_csvs/zhvi/<Geo>_zhvi_<cut>_month.csv,
~100MB each for City / Neighborhood). HomeWatch HEADs every file in use each
HEAD_SECS and re-downloads one only when its Last-Modified changes, keeping
just the regions the markets need. The snapshot carries `release_ts` (the
newest Last-Modified in use) so the gate can hold after a release, and
`checked_ts` (the last time every file answered) so it fails closed when
Zillow cannot be reached.

ANY ZILLOW SERIES (Jack 2026-11-13: "why isnt KXMEMHOME-31DEC26 quoting? make
the Zillow markets generalizable and auto-quote new ones"). The region comes
from each market's own rules ("Zillow region ID N"), so nothing is mapped by
hand: HomeWatch takes `discover`, the series the bot's programs feed lists
with HOME in the ticker, and a candidate whose open markets' rules name a
Zillow Home Value Index joins `auto` (KXMEMHOME, KXHOUHOME); one whose rules
do not (KXEXUSHOME: FRED; KX<ST>HOMEPRICE: FHFA) is probed again only every
META_REFRESH_SECS. "below" / "at most" strikes price as 1 - P(above) and
"at least" as "above"; a target month more than MAX_H months past the latest
print is not modelled (the re-anchor drift is measured over a season, not
years: KXHOUHOME's December 2027 strikes stand aside).
"""
import csv
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from statistics import NormalDist
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import requests


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ZILLOW_BASE = os.environ.get(
    "IMM_HOME_ZILLOW_BASE",
    "https://files.zillowstatic.com/research/public_csvs/zhvi")
FAMILY_SERIES = tuple(s.strip() for s in os.environ.get(
    "IMM_HOME_SERIES", "KXSEAHOME,KXVENICEHOME").split(",") if s.strip())
# model knobs (percent, per release / per month)
REANCHOR_MEAN_PCT = _env_float("IMM_HOME_REANCHOR_MEAN_PCT", -0.5)
REANCHOR_COMMON_SD_PCT = _env_float("IMM_HOME_REANCHOR_COMMON_SD_PCT", 0.5)
REANCHOR_IDIO_SD_PCT = {
    "City": _env_float("IMM_HOME_REANCHOR_IDIO_SD_CITY_PCT", 0.7),
    "Neighborhood": _env_float("IMM_HOME_REANCHOR_IDIO_SD_NBHD_PCT", 1.0),
}
REANCHOR_IDIO_SD_OTHER_PCT = _env_float("IMM_HOME_REANCHOR_IDIO_SD_OTHER_PCT", 0.6)
SHAPE_SD_MULT = _env_float("IMM_HOME_SHAPE_SD_MULT", 1.0)
SD_FLOOR_PCT = _env_float("IMM_HOME_SD_FLOOR_PCT", 0.5)
AR_MONTHS = int(_env_float("IMM_HOME_AR_MONTHS", 36))
# the furthest target month modelled, in months past the latest print: the
# AR fit is scored to h=3 and the re-anchor drift (h x REANCHOR_MEAN_PCT) is
# a fall-2026 reading, so a year out it would be a -6% guess
MAX_H = int(_env_float("IMM_HOME_MAX_H", 6))
# every minute since the HOME LEADS (2026-11-13): 200-lot leads ~15c+ off the
# fair rest on every strike, and a new Zillow release is what moves it, so the
# gate's release hold must start within a minute of Zillow's file changing
HEAD_SECS = _env_float("IMM_HOME_HEAD_SECS", 60)
META_REFRESH_SECS = _env_float("IMM_HOME_META_REFRESH_SECS", 900)

# home type (as the rules word it) -> Zillow Research cut (middle tier, sm_sa)
HOME_TYPE_CUT = {
    "single-family homes": "uc_sfr_tier_0.33_0.67_sm_sa",
    "all homes": "uc_sfrcondo_tier_0.33_0.67_sm_sa",
    "condos/co-ops": "uc_condo_tier_0.33_0.67_sm_sa",
}
# where a region ID is looked for, in order (Zip is never searched: 500MB+)
GEO_ORDER = ("City", "Neighborhood", "Metro", "County", "State")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HTTP_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 300.0

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"))}
_RULES_RE = re.compile(
    r"Zillow Home Value Index for (?P<type>.+?) in (?P<geo>.+?) "
    r"\(Zillow region ID (?P<rid>\d+)\) is (?P<op>above|below|at least|at most|exactly) "
    r"\$(?P<val>[\d,]+(?:\.\d+)?)(?: (?P<unit>million|thousand|billion))? "
    r"(?P<inat>at|in) (?P<mon>[A-Z][a-z]+) (?P<yr>\d{4})")
_SERIES_URL_RE = re.compile(r"zillow\.com/home-values/(\d+)/")
# the rules' comparison -> (YES on the low side?, strict?)
_OPS = {"above": (False, True), "at least": (False, False),
        "below": (True, True), "at most": (True, False)}


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[home-fair] {msg}", flush=True)


# ------------------------------------------------------------------ parsing

def parse_rules(text: str) -> Optional[dict]:
    """The contract's terms from a market's rules_primary, else None:
    {type, rid, op, value, inat, year, month}."""
    m = _RULES_RE.search(text or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group("mon"))
    if mon is None:
        return None
    val = float(m.group("val").replace(",", ""))
    val *= {"million": 1e6, "thousand": 1e3, "billion": 1e9}.get(m.group("unit") or "", 1.0)
    return {"type": m.group("type").strip().lower(), "rid": int(m.group("rid")),
            "op": m.group("op"), "value": round(val), "inat": m.group("inat"),
            "year": int(m.group("yr")), "month": mon}


def month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def month_of(col: str) -> Optional[Tuple[int, int]]:
    """'2026-09-30' -> (2026, 9)."""
    try:
        return int(col[:4]), int(col[5:7])
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------------------- model

def ar_forecast(values: List[float], h: int, n: int = AR_MONTHS) -> Tuple[float, float]:
    """(log change forecast over h months, its sd) from an AR(1) on the last
    n log month-over-month changes of `values` (oldest first, all > 0)."""
    if h <= 0:
        return 0.0, 0.0
    d = [math.log(values[i] / values[i - 1]) for i in range(len(values) - n, len(values))]
    x, y = d[:-1], d[1:]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxx = sum((a - mx) ** 2 for a in x)
    b = sum((a - mx) * (c - my) for a, c in zip(x, y)) / sxx if sxx > 0 else 0.0
    b = max(-0.2, min(0.98, b))
    a = my - b * mx
    res = [c - (a + b * p) for p, c in zip(x, y)]
    sd = math.sqrt(sum(r * r for r in res) / len(res))
    cur, tot = d[-1], 0.0
    for _ in range(h):
        cur = a + b * cur
        tot += cur
    var = sum(sum(b ** j for j in range(k + 1)) ** 2 for k in range(h)) * sd * sd
    return tot, math.sqrt(var)


def first_print_dist(values: List[float], h: int, geo: str) -> Tuple[float, float]:
    """(mean, sd) of log(first print of the month h after the last value)."""
    f, sd_shape = ar_forecast(values, h)
    idio = REANCHOR_IDIO_SD_PCT.get(geo, REANCHOR_IDIO_SD_OTHER_PCT) / 100.0
    common = REANCHOR_COMMON_SD_PCT / 100.0
    var = (SHAPE_SD_MULT * sd_shape) ** 2 + h * (idio * idio + common * common)
    sd = max(math.sqrt(var), SD_FLOOR_PCT / 100.0)
    mu = math.log(values[-1]) + f + h * REANCHOR_MEAN_PCT / 100.0
    return mu, sd


def p_above(mu: float, sd: float, strike: float) -> float:
    """P(value > strike) for log value ~ N(mu, sd)."""
    if strike <= 0:
        return 1.0
    return 1.0 - NormalDist(mu, sd).cdf(math.log(strike))


# --------------------------------------------------------------------- feed

def file_url(geo: str, cut: str) -> str:
    return f"{ZILLOW_BASE}/{geo}_zhvi_{cut}_month.csv"


def _head(url: str) -> Tuple[Optional[float], str]:
    """(Last-Modified epoch, etag) of a Zillow file; raises on failure."""
    r = requests.head(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    lm = r.headers.get("Last-Modified")
    ts = parsedate_to_datetime(lm).timestamp() if lm else None
    return ts, str(r.headers.get("ETag") or "")


def _lines(url: str) -> Iterable[str]:
    """The file's lines (streamed); a local path reads from disk (tests)."""
    if not url.startswith("http"):
        with open(url, encoding="utf-8", newline="") as f:
            for line in f:
                yield line
        return
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        r.encoding = "utf-8"
        for line in r.iter_lines(decode_unicode=True):
            if line:
                yield line


def read_regions(url: str, rids: Iterable[int]) -> Dict[int, dict]:
    """{rid: {name, geo_type, months: [(y, m)], values: [float|None]}} for
    the wanted region IDs present in a ZHVI file."""
    want = set(rids)
    out: Dict[int, dict] = {}
    rows = csv.reader(_lines(url))
    hdr = next(rows)
    mi = [i for i, h in enumerate(hdr) if month_of(h) and h[:2] in ("19", "20")]
    months = [month_of(hdr[i]) for i in mi]
    for row in rows:
        try:
            rid = int(row[0])
        except (ValueError, IndexError):
            continue
        if rid not in want:
            continue
        vals = []
        for i in mi:
            try:
                vals.append(float(row[i]) if row[i] else None)
            except (ValueError, IndexError):
                vals.append(None)
        out[rid] = {"name": row[2] if len(row) > 2 else "",
                    "geo_type": row[3] if len(row) > 3 else "",
                    "months": months, "values": vals}
    return out


# ------------------------------------------------------------------- reads

def _read(get_json: Optional[Callable[[str, dict], dict]], path: str,
          params: dict) -> dict:
    """A Kalshi read: signed through `get_json` when given, else -- or when
    that fails -- the public endpoint (raises on a public failure)."""
    if get_json is not None:
        try:
            js = get_json(path, dict(params))
            if isinstance(js, dict):
                return js
        except Exception:                               # noqa: BLE001
            pass
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = requests.get(KALSHI_BASE + path, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:                          # noqa: BLE001
            last = e
            time.sleep(2.0 * (attempt + 1) ** 2)
    raise RuntimeError(f"Kalshi {path} failed: {last}")


def _markets(get_json, series: str) -> List[dict]:
    out: List[dict] = []
    cursor = None
    for _ in range(10):
        p = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        js = _read(get_json, "/markets", p)
        out.extend(js.get("markets") or [])
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


# ---------------------------------------------------------------- snapshot

def market_entry(m: dict, region: Optional[dict], geo: Optional[str]) -> dict:
    """One market's fair, or {"err": why}. Models "above / at least / below /
    at most $K at <month>" up to MAX_H months past the latest print."""
    terms = parse_rules(m.get("rules_primary") or "")
    if terms is None:
        return {"err": "rules do not parse"}
    if terms["op"] not in _OPS or terms["inat"] != "at":
        return {"err": f"not modelled: {terms['op']} / {terms['inat']}"}
    low, strict = _OPS[terms["op"]]
    if terms["type"] not in HOME_TYPE_CUT:
        return {"err": f"home type {terms['type']!r} not modelled"}
    # an "above" ladder carries floor_strike, a "below" one cap_strike
    field = "cap_strike" if low else "floor_strike"
    strike = m.get(field)
    try:
        strike = float(strike)
    except (TypeError, ValueError):
        return {"err": f"no {field}"}
    if abs(strike - terms["value"]) > 0.5:
        return {"err": f"{field} {strike:g} != rules ${terms['value']:,}"}
    if region is None:
        return {"err": f"region {terms['rid']} not in the {terms['type']} files"}
    months, vals = region["months"], region["values"]
    last = max((i for i, v in enumerate(vals) if v), default=None)
    if last is None or last < AR_MONTHS + 1 or any(
            not v or v <= 0 for v in vals[last - AR_MONTHS:last + 1]):
        return {"err": "too little history"}
    ly, lm = months[last]
    h = month_index(terms["year"], terms["month"]) - month_index(ly, lm)
    base = {"strike": strike, "target": f"{terms['year']}-{terms['month']:02d}",
            "last_month": f"{ly}-{lm:02d}", "last_value": round(vals[last]),
            "h": h, "rid": terms["rid"], "type": terms["type"], "geo": geo,
            "op": terms["op"]}
    if h < 0:
        return dict(base, err="the target month's first print is out")
    if h == 0:
        # the latest file IS the target's first print
        v = vals[last]
        yes = (v < strike if strict else v <= strike) if low else \
            (v > strike if strict else v >= strike)
        return dict(base, p=1.0 if yes else 0.0, decided=True)
    if h > MAX_H:
        return dict(base, err=f"not modelled: {h} months past the latest "
                              f"print (max {MAX_H})")
    mu, sd = first_print_dist(vals[last - AR_MONTHS:last + 1], h, geo or "")
    up = p_above(mu, sd, strike)
    return dict(base, p=1.0 - up if low else up, mu=round(math.exp(mu)),
                sd_pct=round(100 * sd, 3))


class HomeWatch:
    """The refresher's state. Kalshi markets every META_REFRESH_SECS; every
    Zillow file in use HEADed each HEAD_SECS (re-downloaded on a new
    Last-Modified); `snap` is what the gate reads.

    `discover` (optional) returns candidate series -- the bot passes the
    programs feed's HOME series. A candidate whose open markets' rules name a
    Zillow Home Value Index joins `auto` (priced from then on, and listed in
    the snapshot's auto_series); one with no open market yet, or whose rules
    do not parse, is probed again META_REFRESH_SECS later. `auto` seeds the
    confirmed set (a restart reads it back from the status file)."""

    def __init__(self, get_json: Optional[Callable[[str, dict], dict]] = None,
                 series: Iterable[str] = FAMILY_SERIES,
                 cache_path: Optional[str] = None,
                 discover: Optional[Callable[[], Iterable[str]]] = None,
                 auto: Iterable[str] = ()):
        self.get_json = get_json
        self.series = tuple(series)
        self.discover = discover
        self.auto: set = set(auto) - set(self.series)
        # unconfirmed candidate -> (last probe ts, why)
        self.probed: Dict[str, Tuple[float, str]] = {}
        # the parsed regions on disk, keyed by file and its Last-Modified /
        # ETag, so a restart does not download ~100MB a file again
        self.cache_path = cache_path
        self.cache: Dict[str, dict] = {}
        if cache_path:
            try:
                with open(cache_path, encoding="utf-8") as f:
                    self.cache = json.load(f) or {}
            except (OSError, ValueError):
                self.cache = {}
        self.meta_at = 0.0
        self.markets: List[dict] = []
        # (geo, cut) -> {"lm": epoch, "etag": str, "regions": {rid: region}}
        self.files: Dict[Tuple[str, str], dict] = {}
        self.where: Dict[Tuple[int, str], str] = {}     # (rid, cut) -> geo
        # (geo, cut) -> {rid: (Last-Modified, etag) it was searched at}
        self.searched: Dict[Tuple[str, str], Dict[int, tuple]] = {}
        self.head_at = 0.0
        self.checked_ts: Optional[float] = None
        self.snap: Optional[dict] = None

    def _need(self) -> Dict[str, set]:
        """cut -> region IDs the open markets need."""
        need: Dict[str, set] = {}
        for m in self.markets:
            t = parse_rules(m.get("rules_primary") or "")
            if t and t["type"] in HOME_TYPE_CUT:
                need.setdefault(HOME_TYPE_CUT[t["type"]], set()).add(t["rid"])
        return need

    def _load(self, geo: str, cut: str, rids: set, lm: Optional[float],
              etag: str) -> None:
        ck = f"{geo}/{cut}"
        c = self.cache.get(ck)
        if isinstance(c, dict) and c.get("lm") == lm and c.get("etag") == etag \
                and set(rids) <= {int(r) for r in c.get("searched") or []}:
            regions = {int(r): {"name": v.get("name", ""),
                                "geo_type": v.get("geo_type", ""),
                                "months": [tuple(m) for m in v["months"]],
                                "values": v["values"]}
                       for r, v in (c.get("regions") or {}).items() if int(r) in rids}
            self.files[(geo, cut)] = {"lm": lm, "etag": etag, "regions": regions,
                                      "loaded_ts": time.time()}
            return
        url = file_url(geo, cut)
        t0 = time.time()
        regions = read_regions(url, rids)
        self.files[(geo, cut)] = {"lm": lm, "etag": etag, "regions": regions,
                                  "loaded_ts": time.time()}
        if self.cache_path:
            self.cache[ck] = {"lm": lm, "etag": etag, "searched": sorted(rids),
                              "regions": {str(r): {"name": v["name"],
                                                   "geo_type": v["geo_type"],
                                                   "months": [list(m) for m in v["months"]],
                                                   "values": v["values"]}
                                          for r, v in regions.items()}}
            try:
                write_status(self.cache_path, self.cache)
            except OSError:
                pass
        _log(f"loaded {geo} {cut}: {len(regions)}/{len(rids)} regions "
             f"({time.time() - t0:.0f}s, last-modified "
             f"{datetime.fromtimestamp(lm, timezone.utc).isoformat() if lm else '?'})")

    def _found_in(self, geo: str, cut: str) -> set:
        return {r for (r, c), g in self.where.items() if c == cut and g == geo}

    def _sync_zillow(self, now_ts: float, errors: List[str]) -> None:
        """HEAD every file in use and re-read one whose Last-Modified or ETag
        changed; then look for regions not located yet, file by file in
        GEO_ORDER (a file already searched at the same Last-Modified for a
        region is not downloaded again for it). `checked_ts` advances only
        when every HEAD and download in the pass succeeded."""
        need = self._need()
        ok = True
        heads: Dict[Tuple[str, str], Tuple[Optional[float], str]] = {}

        def head(geo: str, cut: str):
            nonlocal ok
            key = (geo, cut)
            if key not in heads:
                try:
                    heads[key] = _head(file_url(geo, cut))
                except Exception as e:                  # noqa: BLE001
                    ok = False
                    errors.append(f"zillow {geo} {cut}: {type(e).__name__}: {str(e)[:80]}")
                    heads[key] = None
            return heads[key]

        def load(geo: str, cut: str, rids: set, hv) -> bool:
            nonlocal ok
            try:
                self._load(geo, cut, rids, hv[0], hv[1])
                return True
            except Exception as e:                      # noqa: BLE001
                ok = False
                errors.append(f"zillow {geo} {cut} download: "
                              f"{type(e).__name__}: {str(e)[:80]}")
                return False

        for (geo, cut), cur in list(self.files.items()):
            hv = head(geo, cut)
            if hv is None:
                continue
            if (hv[0], hv[1]) != (cur["lm"], cur["etag"]):
                load(geo, cut, self._found_in(geo, cut), hv)
        for cut, rids in need.items():
            missing = {r for r in rids if (r, cut) not in self.where}
            for geo in GEO_ORDER:
                if not missing:
                    break
                hv = head(geo, cut)
                if hv is None:
                    continue
                memo = self.searched.setdefault((geo, cut), {})
                todo = {r for r in missing if memo.get(r) != hv}
                if not todo:
                    continue
                if not load(geo, cut, self._found_in(geo, cut) | todo, hv):
                    continue
                got = set(self.files[(geo, cut)]["regions"])
                for r in todo:
                    memo[r] = hv
                    if r in got:
                        self.where[(r, cut)] = geo
                        missing.discard(r)
            for r in sorted(missing):
                errors.append(f"region {r} not found in any {cut} file")
        if ok:
            self.checked_ts = now_ts

    def _candidates(self, now_ts: float, errors: List[str]) -> List[str]:
        """Discovered series neither listed, confirmed, nor probed within
        META_REFRESH_SECS."""
        if self.discover is None:
            return []
        try:
            got = {str(s) for s in (self.discover() or ()) if s}
        except Exception as e:                          # noqa: BLE001
            errors.append(f"discover: {type(e).__name__}: {str(e)[:80]}")
            return []
        return sorted(s for s in got - set(self.series) - self.auto
                      if now_ts - self.probed.get(s, (-1e18, ""))[0]
                      >= META_REFRESH_SECS)

    def _probe(self, s: str, now_ts: float) -> List[dict]:
        """A candidate's open markets if their rules name a Zillow Home Value
        Index (it joins `auto`), else [] (probed again later)."""
        ms = _markets(self.get_json, s)
        if any(parse_rules(m.get("rules_primary") or "") for m in ms):
            self.auto.add(s)
            self.probed.pop(s, None)
            _log(f"auto: {s} settles on a Zillow Home Value Index "
                 f"({len(ms)} open markets) -- priced from now on")
            return ms
        why = "rules name no Zillow Home Value Index" if ms else "no open market yet"
        if self.probed.get(s, (0.0, ""))[1] != why:
            _log(f"auto: {s} not taken: {why}")
        self.probed[s] = (now_ts, why)
        return []

    def refresh(self, now_ts: Optional[float] = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        errors: List[str] = []
        new = self._candidates(now_ts, errors)
        if now_ts - self.meta_at >= META_REFRESH_SECS or not self.markets:
            try:
                mk: List[dict] = []
                for s in self.series + tuple(sorted(self.auto)):
                    mk.extend(_markets(self.get_json, s))
                self.markets = mk
                self.meta_at = now_ts
            except Exception as e:                      # noqa: BLE001
                errors.append(f"markets: {type(e).__name__}: {str(e)[:100]}")
        for s in new:
            # a new series is probed at once, not at the next listing
            try:
                ms = self._probe(s, now_ts)
            except Exception as e:                      # noqa: BLE001
                errors.append(f"probe {s}: {type(e).__name__}: {str(e)[:80]}")
                continue
            have = {m.get("ticker") for m in self.markets}
            self.markets = self.markets + [m for m in ms
                                           if m.get("ticker") not in have]
        if now_ts - self.head_at >= HEAD_SECS or self.checked_ts is None:
            self.head_at = now_ts
            self._sync_zillow(now_ts, errors)
        self.snap = self.build(now_ts, errors)
        return self.snap

    def region(self, rid: int, cut: str) -> Tuple[Optional[dict], Optional[str]]:
        geo = self.where.get((rid, cut))
        if geo is None:
            return None, None
        return self.files.get((geo, cut), {}).get("regions", {}).get(rid), geo

    def build(self, now_ts: float, errors: List[str]) -> dict:
        markets: Dict[str, dict] = {}
        lms = []
        for m in self.markets:
            t = m.get("ticker") or ""
            terms = parse_rules(m.get("rules_primary") or "")
            cut = HOME_TYPE_CUT.get(terms["type"]) if terms else None
            region, geo = self.region(terms["rid"], cut) if cut else (None, None)
            if geo is not None:
                lm = self.files.get((geo, cut), {}).get("lm")
                if lm:
                    lms.append(lm)
            try:
                markets[t] = market_entry(m, region, geo)
            except Exception as e:                      # noqa: BLE001
                markets[t] = {"err": f"{type(e).__name__}: {str(e)[:80]}"}
        return {
            "ts": now_ts, "checked_ts": self.checked_ts,
            "release_ts": max(lms) if lms else None,
            "markets": markets, "errors": errors,
            "series": sorted(set(self.series) | self.auto),
            "auto_series": sorted(self.auto),
            "probed": {s: why for s, (_ts, why) in sorted(self.probed.items())},
            "files": {f"{g}/{c}": {"last_modified": v.get("lm"), "etag": v.get("etag"),
                                   "regions": sorted(v.get("regions", {}))}
                      for (g, c), v in self.files.items()},
            "model": {"reanchor_mean_pct": REANCHOR_MEAN_PCT,
                      "reanchor_common_sd_pct": REANCHOR_COMMON_SD_PCT,
                      "reanchor_idio_sd_pct": dict(REANCHOR_IDIO_SD_PCT,
                                                   other=REANCHOR_IDIO_SD_OTHER_PCT),
                      "shape_sd_mult": SHAPE_SD_MULT, "sd_floor_pct": SD_FLOOR_PCT,
                      "ar_months": AR_MONTHS, "max_h": MAX_H},
        }


def write_status(path: str, snap: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


if __name__ == "__main__":                              # a dry read, no orders
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from kalshi_reads import kalshi_get
        gj = lambda p, q: kalshi_get(p, q)              # noqa: E731
    except Exception:                                   # pragma: no cover
        gj = None
    snap = HomeWatch(gj, sys.argv[1].split(",") if len(sys.argv) > 1
                     else FAMILY_SERIES).refresh()
    print(f"checked {snap['checked_ts']}, release {snap['release_ts']}, "
          f"errors {snap['errors']}")
    for t, e in sorted(snap["markets"].items()):
        print(f"  {t:34} " + (f"p {e['p'] * 100:5.1f}c  mu ${e.get('mu', 0):,} "
                              f"sd {e.get('sd_pct')}% h {e.get('h')} last "
                              f"{e.get('last_month')} ${e.get('last_value'):,}"
                              if "p" in e else e.get("err", "")))
