#!/usr/bin/env python3
"""Carbon Arc fair values for incentive_mm's Carbon Arc-settled markets.

Every Kalshi Carbon Arc contract ("Amazon Credit Card Spend for September
2026 above 104") resolves on the FIRST value Carbon Arc reports for the
month: a year-over-year index, 100 = flat. Carbon Arc's Prisms feed carries
the month-to-date (MTD) value of that same index, refreshed daily and
lagged 2-6 days (point of sale ~2, card spend ~3, app downloads ~5, foot
traffic / ad spend ~6). That MTD series is what the informed flow in these
books trades on: on 2026-09-26 the market-implied median sat within ~0.5
index points of the MTD value on card spend, point of sale and foot
traffic, and our own fills marked out -7.8c/contract (bids -14.6c).

Jack 2026-09-26, with Carbon Arc's written consent: "use Carbon Arc's data
when quoting". This module turns the feed into, per series and measurement
month, a normal N(mu, sigma) for the first print:

    mu      = latest MTD value + CA_DRIFT_PTS (default 0)
    sigma   = CA_SIGMA_MULT * sqrt(((1 - w) * sigma_m) ** 2 + floor ** 2)
              * early_sigma_mult(category, days observed)
    w       = days observed / days in the month (the data_through day)
    sigma_m = stdev of the entity's month-over-month changes (hist_yoy):
              the scale of what the unobserved rest of the month can move
    floor   = max(CA_SIGMA_FLOOR_PTS, CA_SIGMA_FLOOR_REL * mu): the gap
              between an MTD read and the first print. Revisions are real
              and one-sided: August card-spend values were later revised UP
              a median 3.4 points over the first print (foot traffic ~0),
              so only the FIRST print matters and hist_yoy's revised values
              are used for their month-to-month SPREAD only, never a level.
    CA_SIGMA_MULT 1.7 = the median ratio of market-implied sigma to this
              formula without it on 2026-09-26 (card 1.71, POS 1.51, foot
              traffic 1.77, ads 1.76, apps 1.56), so a gate built on it
              fires on real divergence rather than on model overconfidence.
    early_sigma_mult = max(1, A / sqrt(days observed)) per category (see
              CA_EARLY_SIGMA_A): the first days' reads miss by more than
              the formula says on card spend and foot traffic.

READ FROM THE FIRST DAY (Jack 2026-10-04, "yes ship it"). Until then a month
had no entry before 10% of it was observed, and with the feed's 2-6 day lag
every market's measurement month went unread for its first ~6-10 days.
That is exactly when the bot quoted: the September book stopped on 9/26
(the late-month rule), a day before the feed came online, and the October
book quoted from 10/01 on no read at all -- the gate never stood a single
market aside, and the October fills were the worst-marked Carbon Arc fills
yet (-9.4c/contract at 24h, -$462 on 5,426 contracts by 10/04, point of
sale -$231). Replayed, point of sale's day-1 read at the formula's own
sigma would have prevented 19-20 of those fills, ~$142 of the loss, for
~$34-46 of reward. So a read is an entry from its first day, with sigma
widened where the early days are known to be noisy (CA_EARLY_SIGMA_A,
CA_MIN_OBS_DAYS); the 20%-of-mu noise cap in incentive_mm still turns the
gate off for a read too vague to overrule the book.

incentive_mm prices a strike K as P(first print > K) and stands a market
down when its touch fights that fair (the rain-gate shape). Series ->
(prism, entity) comes from each series' Kalshi settlement-source URL
(".../prisms?prism=<id>&entity=<name>"), read from Kalshi's GET /series
catalog at most once a day -- signed when incentive_mm passes its reader,
publicly otherwise.

Feed, in CA_FEED_CONFIG (default ~/.carbonarc_feed.json, outside the repo
because the repo is public) as {"token": ..., "url": ...}, or the env vars
IMM_CA_FEED_TOKEN / IMM_CA_FEED_URL:
  - a TOKEN alone (the Carbon Arc account's API token, app.carbonarc.ai ->
    Developers) reads the official Prisms API, the one the `carbonarc` SDK's
    client.prisms wraps: GET {CA_API_HOST}/v2/prisms?insight_id=<id> per
    insight, falling back to GET /v2/prisms/<prism_id>, with
    "Authorization: Bearer <token>". Carbon Arc: "Any valid API token may
    read prisms. There is no entitlement to enable and no cost per call."
    The prism -> insight map is learned on the first read and kept in the
    fair file, so a steady sweep is one call per insight (~6), not per
    prism (~40).
  - a URL (+ optional token as Bearer) is read as one payload.
Either way the data is the Prisms JSON shape

    {"prisms": [{"prism_id", "category", "data_through",
                 "last_refreshed_at",
                 "entities": [{"entity_name",
                               "mtd_yoy": [{"date", "value"}, ...],
                               "hist_yoy": [{"month", "value"}, ...]}]}]}

Unset URL = no fetch: write_fair_file returns (0, 0) without touching the
file, so incentive_mm's gate stays open and quoting is exactly as before.
Every failure mode degrades the same way (per-entry TTL in the bot).

Every new vintage (an entity's data_through moving) is appended to
CA_VINTAGE_FILE so the model can be calibrated against the prints.

Standalone:
    python carbon_arc_fair.py --payload saved.json --out fair.json
"""

import argparse
import calendar
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
# Feed settings live OUTSIDE the repo (it is public): the env vars win, else
# a JSON file {"url": ..., "token": ...} in the user's home directory. Read
# at call time, so adding the file needs no restart.
CA_FEED_CONFIG = os.environ.get(
    "IMM_CA_FEED_CONFIG",
    os.path.join(os.path.expanduser("~"), ".carbonarc_feed.json"))
CA_VINTAGE_FILE = os.environ.get(
    "IMM_CA_VINTAGE_FILE", os.path.join(STATUS_DIR, "carbon_arc_vintages.jsonl"))
KALSHI_SERIES_URL = os.environ.get(
    "IMM_CA_SERIES_URL",
    "https://api.elections.kalshi.com/trade-api/v2/series")
# the official API host (the carbonarc SDK's BaseAPIClient default)
CA_API_HOST = os.environ.get("IMM_CA_API_HOST", "https://api.carbonarc.co")
CA_SIGMA_MULT = _env_float("IMM_CA_SIGMA_MULT", 1.7)
CA_SIGMA_FLOOR_PTS = _env_float("IMM_CA_SIGMA_FLOOR_PTS", 1.0)
CA_SIGMA_FLOOR_REL = _env_float("IMM_CA_SIGMA_FLOOR_REL", 0.01)
# fallback month-to-month spread (fraction of mu) when an entity has fewer
# than 3 usable month-over-month changes
CA_SIGMA_M_DEFAULT_REL = _env_float("IMM_CA_SIGMA_M_DEFAULT_REL", 0.05)
CA_DRIFT_PTS = _env_float("IMM_CA_DRIFT_PTS", 0.0)
# no entry until this fraction of the month is observed. 0.10 (~3 days)
# until 2026-10-04, which left every month unread for its first ~6-10 days
# once the feed lag is added (READ FROM THE FIRST DAY, above);
# IMM_CA_MIN_OBS_FRAC=0.10 with IMM_CA_EARLY_SIGMA=0 restores the old model.
CA_MIN_OBS_FRAC = _env_float("IMM_CA_MIN_OBS_FRAC", 0.0)
# EARLY-MONTH SIGMA (2026-10-04). Measured on September's daily paths (75
# entities; each day-k read against the month's last read, divided by the
# sigma above): card spend missed by 7.7x sigma on day 1 and 1.4-2.4x on
# days 2-5 (23-26% of misses past 2 sigma), foot traffic by 1.2-1.9x on
# days 1-2 (plus a Labor Day calendar shift on days 4-5), while apps and ad
# spend stayed inside it (0.6-1.35x; the noise cap already turns most of
# them off early). sigma is multiplied by max(1, A / sqrt(days observed)),
# which brings card spend's days 2-5 to 0.95-1.0x and foot traffic's days
# 1-3 to 0.7x. Point of sale has no early path on file (its prism rolled to
# October before one was kept) and its day-1 read at the formula's sigma is
# what the replay scored, so it stays at 1 until calibrated. Keys are the
# prism's category, lowercased; an unknown category takes the widest A.
CA_EARLY_SIGMA_A = {"credit card": 3.4, "foot traffic": 2.5,
                    "advertising": 1.8, "app": 1.8, "point of sale": 1.0}
CA_EARLY_SIGMA_A_DEFAULT = 3.4
CA_EARLY_SIGMA_ENABLE = os.environ.get("IMM_CA_EARLY_SIGMA", "1") == "1"
# a card-spend read of ONE day is past repair (7.7x sigma): two days first
CA_MIN_OBS_DAYS = {"credit card": 2}
# an MTD read whose data_through is older than this is not a read of the
# month in progress (feed stalled) -> no entry
CA_MAX_DATA_AGE_DAYS = _env_float("IMM_CA_MAX_DATA_AGE_DAYS", 10)
SERIES_MAP_TTL_SECS = 24 * 3600
HTTP_TIMEOUT = 20


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[ca-fair] {msg}", flush=True)


# the last signed-read failure, so each distinct one is logged once
_signed_err: Optional[str] = None


def _signed_read(get_json: Optional[Callable[[str, dict], dict]], path: str,
                 params: dict) -> Optional[dict]:
    """A SIGNED Kalshi read through `get_json(path, params)`, or None when
    there is no reader or it failed -- the caller then reads the public
    endpoint as before. incentive_mm passes the reader (2026-09-27): Kalshi
    throttles unsigned list reads from any IP, while signed reads ride the
    account's token bucket. Each distinct failure is logged once."""
    global _signed_err
    if get_json is None:
        return None
    try:
        js = get_json(path, dict(params))
        if not isinstance(js, dict):
            raise ValueError(f"signed read returned {type(js).__name__}")
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:120]}"
        if err != _signed_err:
            _log(f"! signed Kalshi read failed ({err}); reading the public endpoint")
        _signed_err = err
        return None
    if _signed_err is not None:
        _log("signed Kalshi reads back")
        _signed_err = None
    return js


def feed_settings() -> Tuple[str, str]:
    """(url, token) for the feed: the env vars when either is set, else
    CA_FEED_CONFIG. ("", "") when neither is set -- the gate stays open.
    utf-8-sig: Windows Notepad may save the file with a BOM."""
    url = os.environ.get("IMM_CA_FEED_URL", "").strip()
    token = os.environ.get("IMM_CA_FEED_TOKEN", "").strip()
    if url or token:
        return url, token
    try:
        with open(CA_FEED_CONFIG, encoding="utf-8-sig") as f:
            cfg = json.load(f) or {}
        return (str(cfg.get("url") or "").strip(),
                str(cfg.get("token") or "").strip())
    except (OSError, ValueError, AttributeError):
        return "", ""


def feed_configured() -> bool:
    """A URL or an API token is set."""
    url, token = feed_settings()
    return bool(url or token)


def _auth_headers(token: str) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_prisms_api(prism_ids: List[str], token: str,
                     insights: Dict[str, int],
                     timeout: float = HTTP_TIMEOUT) -> dict:
    """{"prisms": [...]} for `prism_ids` from the official Prisms API. One
    GET /v2/prisms?insight_id= per insight already known in `insights`
    (prism_id -> insight_id, updated in place), then GET /v2/prisms/<id>
    for any prism still missing. A 404 (unknown / not public / never
    published -- one answer by design) skips that prism; any other HTTP
    error raises so the caller keeps the previous file."""
    base = f"{CA_API_HOST.rstrip('/')}/v2/prisms"
    headers = _auth_headers(token)
    want = set(prism_ids)
    got: Dict[str, dict] = {}
    by_insight: Dict[int, List[str]] = {}
    for pid in sorted(want):
        iid = insights.get(pid)
        if iid is not None:
            by_insight.setdefault(iid, []).append(pid)
    for iid in sorted(by_insight):
        r = requests.get(base, params={"insight_id": iid}, headers=headers,
                         timeout=timeout)
        r.raise_for_status()
        for p in (r.json() or {}).get("prisms") or []:
            if isinstance(p, dict) and p.get("prism_id") in want:
                got[p["prism_id"]] = p
    for pid in sorted(want - set(got)):
        r = requests.get(f"{base}/{pid}", headers=headers, timeout=timeout)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        p = r.json()
        if not isinstance(p, dict):
            continue
        p.setdefault("prism_id", pid)
        got[pid] = p
        if p.get("insight_id") is not None:
            try:
                insights[pid] = int(p["insight_id"])
            except (TypeError, ValueError):
                pass
    return {"prisms": [got[k] for k in sorted(got)]}


def fetch_payload(url: str = "", token: str = "",
                  prism_ids: Optional[List[str]] = None,
                  insights: Optional[Dict[str, int]] = None,
                  timeout: float = HTTP_TIMEOUT) -> Optional[dict]:
    """The Prisms payload from the configured feed; None when no feed is
    configured. A URL is one GET of a {"prisms": [...]} payload; a token
    alone reads `prism_ids` from the official Prisms API. Raises on HTTP /
    parse errors (the caller logs and keeps the previous file)."""
    if not url and not token:
        url, token = feed_settings()
    if not url and not token:
        return None
    if not url:
        return fetch_prisms_api(list(prism_ids or []), token,
                                insights if insights is not None else {},
                                timeout=timeout)
    r = requests.get(url, headers=_auth_headers(token), timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or not isinstance(data.get("prisms"), list):
        raise ValueError("feed payload has no 'prisms' list")
    return data


def prism_ref(url: str) -> Optional[Tuple[str, str]]:
    """(prism_id, entity_name) from a Carbon Arc prisms settlement URL, or
    None when the URL is not one."""
    if "carbonarc" not in (url or "").lower():
        return None
    q = parse_qs(urlparse(url).query)
    pid = (q.get("prism") or [""])[0].strip()
    ent = (q.get("entity") or [""])[0].strip()
    if not pid or not ent:
        return None
    return pid, ent


def series_map_from_catalog(series_list: List[dict]) -> Dict[str, dict]:
    """series ticker -> {"prism": id, "entity": name} for every series whose
    settlement source is a Carbon Arc prisms page."""
    out: Dict[str, dict] = {}
    for s in series_list or []:
        if not isinstance(s, dict):
            continue
        for src in s.get("settlement_sources") or []:
            if not isinstance(src, dict):
                continue
            ref = prism_ref(str(src.get("url") or ""))
            if ref:
                out[str(s.get("ticker") or "")] = {"prism": ref[0],
                                                   "entity": ref[1]}
                break
    out.pop("", None)
    return out


def fetch_series_map(timeout: float = HTTP_TIMEOUT,
                     get_json: Optional[Callable[[str, dict], dict]] = None
                     ) -> Dict[str, dict]:
    """The Carbon Arc series map from Kalshi's series catalog (one call,
    ~14k series): signed through `get_json` when given, else -- or when that
    read fails -- the public endpoint. Raises on failure."""
    js = _signed_read(get_json, "/series", {})
    if js is None:
        r = requests.get(KALSHI_SERIES_URL, timeout=timeout)
        r.raise_for_status()
        js = r.json() or {}
    return series_map_from_catalog(js.get("series") or [])


def _month_key(date_iso: str) -> str:
    return str(date_iso)[:7]


def _category_key(category) -> str:
    return str(category or "").strip().lower()


def early_sigma_mult(category, days: int) -> float:
    """max(1, A / sqrt(days observed)) for the read's category (see
    CA_EARLY_SIGMA_A); 1 when the knob is off."""
    if not CA_EARLY_SIGMA_ENABLE or days <= 0:
        return 1.0
    a = CA_EARLY_SIGMA_A.get(_category_key(category), CA_EARLY_SIGMA_A_DEFAULT)
    return max(1.0, a / math.sqrt(days))


def month_spread(hist: List[dict]) -> Optional[float]:
    """Population stdev of month-over-month changes of the COMPLETE months
    in hist_yoy (the last entry is the month in progress -- a partial-month
    ratio, not comparable -- and is dropped). None with <3 changes."""
    vals = []
    for h in (hist or [])[:-1]:
        try:
            v = h.get("value")
            if v is not None:
                vals.append(float(v))
        except (AttributeError, TypeError, ValueError):
            continue
    ch = [b - a for a, b in zip(vals, vals[1:])]
    if len(ch) < 3:
        return None
    return statistics.pstdev(ch)


def entity_entry(prism: dict, entity: dict,
                 now: datetime) -> Optional[dict]:
    """The N(mu, sigma) entry for one entity's month in progress, or None
    when the read is too thin or too old to say anything."""
    mtd = [m for m in (entity.get("mtd_yoy") or [])
           if isinstance(m, dict) and m.get("value") is not None
           and m.get("date")]
    if not mtd:
        return None
    mtd.sort(key=lambda m: str(m["date"]))
    last = mtd[-1]
    try:
        d_last = datetime.strptime(str(last["date"])[:10], "%Y-%m-%d")
        mu0 = float(last["value"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(mu0) or mu0 <= 0:
        return None
    # the observed stretch ends at the last MTD point (the prism header's
    # data_through says the same thing; the series is what we price off)
    d_obs = d_last
    now_naive = now.astimezone(timezone.utc).replace(tzinfo=None)
    age_days = (now_naive - d_obs).total_seconds() / 86400.0
    if age_days > CA_MAX_DATA_AGE_DAYS:
        return None
    n_days = calendar.monthrange(d_obs.year, d_obs.month)[1]
    w = d_obs.day / float(n_days)
    if w < CA_MIN_OBS_FRAC:
        return None
    category = (prism or {}).get("category")
    if d_obs.day < CA_MIN_OBS_DAYS.get(_category_key(category), 1):
        return None
    sig_m = month_spread(entity.get("hist_yoy") or [])
    if sig_m is None:
        sig_m = CA_SIGMA_M_DEFAULT_REL * mu0
    floor = max(CA_SIGMA_FLOOR_PTS, CA_SIGMA_FLOOR_REL * mu0)
    early = early_sigma_mult(category, d_obs.day)
    sigma = (CA_SIGMA_MULT * math.sqrt(((1.0 - w) * sig_m) ** 2 + floor ** 2)
             * early)
    return {
        "month": d_obs.strftime("%Y-%m"),
        "mu": round(mu0 + CA_DRIFT_PTS, 4),
        "mtd": round(mu0, 4),
        "sigma": round(sigma, 4),
        "early_mult": round(early, 4),
        "w": round(w, 4),
        "sigma_m": round(sig_m, 4),
        "data_through": d_obs.strftime("%Y-%m-%d"),
        "refreshed_at": str(prism.get("last_refreshed_at") or ""),
        "category": str(prism.get("category") or ""),
    }


def build_entries(payload: dict, series_map: Dict[str, dict],
                  now: datetime) -> Tuple[Dict[str, dict], List[str]]:
    """(series -> entry, series with no usable read). Entity names match
    case-insensitively (Kalshi's KXSTREAMINGADS says "Film & TV Streaming",
    the feed "Film & Tv Streaming")."""
    by_pid = {}
    for p in (payload or {}).get("prisms") or []:
        if isinstance(p, dict) and p.get("prism_id"):
            by_pid[str(p["prism_id"])] = p
    entries: Dict[str, dict] = {}
    missing: List[str] = []
    for series, ref in sorted(series_map.items()):
        p = by_pid.get(ref.get("prism", ""))
        ent = None
        if p is not None:
            want = str(ref.get("entity", "")).strip().lower()
            for e in p.get("entities") or []:
                if isinstance(e, dict) and \
                        str(e.get("entity_name", "")).strip().lower() == want:
                    ent = e
                    break
        entry = entity_entry(p, ent, now) if ent is not None else None
        if entry is None:
            missing.append(series)
            continue
        entries[series] = entry
    return entries, missing


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _append_vintages(old: Dict[str, dict], new: Dict[str, dict],
                     fetched_at: str, path: str) -> int:
    """One line per series whose read moved (new data_through or value)."""
    lines = []
    for s, e in sorted(new.items()):
        o = old.get(s) or {}
        if (o.get("data_through"), o.get("mtd")) == \
                (e.get("data_through"), e.get("mtd")):
            continue
        lines.append(json.dumps({"fetched_at": fetched_at, "series": s, **e},
                                sort_keys=True))
    if lines:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
    return len(lines)


def write_fair_file(path: str, payload: Optional[dict] = None,
                    now: Optional[datetime] = None,
                    series_map: Optional[Dict[str, dict]] = None,
                    vintage_path: Optional[str] = None,
                    get_json: Optional[Callable[[str, dict], dict]] = None
                    ) -> Tuple[int, int]:
    """Fetch (unless `payload` is given), build and atomically write the
    fair file. Returns (series with an entry, mapped series without one);
    (0, 0) and no write when no feed is configured. The series map is
    reused from the previous file for SERIES_MAP_TTL_SECS; a failed catalog
    read keeps the old map. `get_json` is incentive_mm's signed Kalshi
    reader (fetch_series_map)."""
    now = now or datetime.now(timezone.utc)
    if payload is None and not feed_configured():
        return 0, 0
    prev = _read_json(path)
    smap = series_map
    if smap is None:
        smap = prev.get("series_map") or {}
        age = time.time() - float(prev.get("series_map_ts") or 0)
        if not smap or age > SERIES_MAP_TTL_SECS:
            try:
                smap = fetch_series_map(get_json=get_json)
                prev["series_map_ts"] = time.time()
            except Exception as e:   # keep the old map
                _log(f"! series catalog read failed ({e}); "
                     f"keeping {len(smap)} mapped series")
    insights: Dict[str, int] = {}
    for k, v in (prev.get("prism_insights") or {}).items():
        try:
            insights[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    if payload is None:
        payload = fetch_payload(
            prism_ids=sorted({r.get("prism", "") for r in smap.values()
                              if r.get("prism")}),
            insights=insights)
        if payload is None:
            return 0, 0
    entries, missing = build_entries(payload, smap, now)
    fetched_at = now.isoformat()
    for e in entries.values():
        e["fetched_at"] = fetched_at
    _append_vintages(prev.get("entries") or {}, entries, fetched_at,
                     vintage_path or CA_VINTAGE_FILE)
    _write_json_atomic(path, {
        "generated_at": fetched_at,
        "entries": entries,
        "missing": missing,
        "series_map": smap,
        "series_map_ts": float(prev.get("series_map_ts") or time.time()),
        "prism_insights": insights,
        "model": {"sigma_mult": CA_SIGMA_MULT,
                  "sigma_floor_pts": CA_SIGMA_FLOOR_PTS,
                  "sigma_floor_rel": CA_SIGMA_FLOOR_REL,
                  "drift_pts": CA_DRIFT_PTS,
                  "min_obs_frac": CA_MIN_OBS_FRAC,
                  "min_obs_days": CA_MIN_OBS_DAYS,
                  "early_sigma_a": (CA_EARLY_SIGMA_A if CA_EARLY_SIGMA_ENABLE
                                    else {})},
    })
    return len(entries), len(missing)


def p_above(strike: float, mu: float, sigma: float) -> float:
    """P(first print > strike) under N(mu, sigma)."""
    if sigma <= 0:
        return 1.0 if mu > strike else 0.0
    return 0.5 * math.erfc((strike - mu) / (sigma * math.sqrt(2.0)))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(STATUS_DIR,
                                                  "carbon_arc_fair.json"))
    ap.add_argument("--payload", help="read the Prisms payload from this "
                    "file instead of the feed (offline test)")
    args = ap.parse_args(argv)
    payload = None
    if args.payload:
        with open(args.payload, encoding="utf-8") as f:
            payload = json.load(f)
    ok, miss = write_fair_file(args.out, payload=payload)
    if ok == 0 and miss == 0 and payload is None:
        _log(f"no feed configured (token or url in {CA_FEED_CONFIG}, or "
             f"IMM_CA_FEED_TOKEN / IMM_CA_FEED_URL); nothing written")
        return 1
    _log(f"{ok} series with a fair entry, {miss} mapped without one "
         f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
