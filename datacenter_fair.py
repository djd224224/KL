#!/usr/bin/env python3
r"""datacenter_fair.py -- Data Center Map's state counts for the
KX<state>DATACENTERS family: the live read the IMM gate quotes against, and
the log the year-end fair value will be fitted from.

Jack 2026-10-01: "allowlist the datacenter family, use the live feed to quote
realtime. and start the logger so we can hone the fair value." Data Center Map
gave permission the same day -- their terms otherwise forbid programmatic
reads ("only direct, human access via standard web browsers is permitted
unless otherwise authorized by a licensing agreement"). Keep the pace polite.

SETTLEMENT. "the whole-number summary count displayed on the Data Center Map
directory page for <state>" at 11:59:59 PM ET Dec 31 -- the page's "We
currently have N data centers listed" line. The page is server-rendered and
uncached (Vercel, cache-control no-store), so every read is the live count.
The same N is in the page title ("Texas Data Centers - 537 Facilities from
197 Operators"); the per-market breakdown does NOT always add up to it (Texas
on 10/1: 533 across 44 markets vs 537), so it is logged, never used as N.

FILES (written by the IMM's refresher thread every IMM_DC_REFRESH_SECS):
  datacenter_fair.json   {"updated_at", "states": {"TX": {"count",
                         "read_at", "changed_at", "prev_count"}}} -- what
                         the gate reads. changed_at = the first read that
                         saw the current count (null until a change is seen).
                         A failed read keeps the previous entry, which ages
                         out of the gate's TTL: the gate fails closed.
  datacenter_counts_YYYY-MM-DD.jsonl  one row per read per state (UTC day):
                         kind "read" (count, title count, operators, market
                         count, ms) or "error"; plus kind "detail" -- the
                         per-market counts and Data Center Map's capacity
                         stats -- on the first read of a day and whenever the
                         count or the breakdown changes.

USAGE: python datacenter_fair.py --once [--states TX,PA] [--dir DIR]
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

BASE = "https://www.datacentermap.com/usa/{slug}/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 "
              "KL-datacenter-fair/1.0")
TIMEOUT = 20.0
PAUSE_SECS = 1.0              # between two state pages in one pass

# Data Center Map's state slugs (verified 2026-10-01 for the nine listed
# states; the rest follow the same lower-case, hyphenated form). DC has no
# verified slug: a series for it gets no read and stands aside.
STATE_SLUGS = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut",
    "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii",
    "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine",
    "MD": "maryland", "MA": "massachusetts", "MI": "michigan",
    "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
    "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new-hampshire", "NJ": "new-jersey", "NM": "new-mexico",
    "NY": "new-york", "NC": "north-carolina", "ND": "north-dakota",
    "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode-island", "SC": "south-carolina", "SD": "south-dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
    "VA": "virginia", "WA": "washington", "WV": "west-virginia",
    "WI": "wisconsin", "WY": "wyoming",
}

_VISIBLE_RE = re.compile(
    r"We currently have\s*(?:<[^>]+>\s*)*([0-9][0-9,]*)\s*(?:<[^>]+>\s*)*data centers",
    re.I)
_TITLE_RE = re.compile(
    r"Data Centers\s*-\s*([0-9][0-9,]*)\s+Facilities(?:\s+from\s+([0-9][0-9,]*)\s+Operators)?",
    re.I)
_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_STAT_KEYS = ("operators", "mw_live", "mw_planned", "mw_pipeline",
              "mw_builtout", "mw_referenced")


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def parse_page(html: str) -> dict:
    """The counts on one state page. Raises ValueError without a count."""
    vis = _VISIBLE_RE.search(html)
    title = _TITLE_RE.search(html)
    out: dict = {"count": None, "title_count": None, "operators": None,
                 "markets": None, "geo_sum": None, "geos": {}, "stats": {}}
    if title:
        out["title_count"] = _int(title.group(1))
        if title.group(2):
            out["operators"] = _int(title.group(2))
    nd = _NEXT_RE.search(html)
    if nd:
        try:
            pp = (json.loads(nd.group(1)).get("props") or {}).get("pageProps") or {}
        except ValueError:
            pp = {}
        geos = (pp.get("mapdata") or {}).get("geos") or []
        for g in geos:
            p = g.get("properties") or {}
            if p.get("link") is not None and p.get("datacenters") is not None:
                out["geos"][str(p["link"])] = int(p["datacenters"])
        if geos:
            out["markets"] = len(geos)
            out["geo_sum"] = sum(out["geos"].values())
        dcs = (((pp.get("geodata") or {}).get("meta_stats") or {}).get("dcs")) or {}
        out["stats"] = {k: dcs.get(k) for k in _STAT_KEYS if k in dcs}
        if out["title_count"] is None:
            t2 = _TITLE_RE.search(str(pp.get("pageTitle") or ""))
            if t2:
                out["title_count"] = _int(t2.group(1))
                if t2.group(2):
                    out["operators"] = _int(t2.group(2))
    # The displayed summary line is what settles; the title carries the same
    # number and stands in only if the line's markup ever changes.
    out["count"] = _int(vis.group(1)) if vis else out["title_count"]
    if out["count"] is None:
        raise ValueError("no data center count on the page")
    return out


def fetch_page(slug: str, session=None) -> str:
    import requests
    get = (session or requests).get
    r = get(BASE.format(slug=slug), headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _append(log_dir: str, rows: List[dict], now: float) -> None:
    if not rows:
        return
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, f"datacenter_counts_{day}.jsonl"), "a",
              encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def write_fair_file(path: str, log_dir: str, states: Iterable[str],
                    now: Optional[float] = None,
                    fetch: Optional[Callable[[str], str]] = None,
                    pause: float = PAUSE_SECS) -> Tuple[int, List[str], List[str]]:
    """Read every state in `states`, append the log rows and rewrite `path`.
    Returns (states read, states that failed, changes as text)."""
    prev = _load(path)
    entries: Dict[str, dict] = dict(prev.get("states") or {})
    details: Dict[str, str] = dict(prev.get("detail_keys") or {})
    rows: List[dict] = []
    changes: List[str] = []
    ok, failed = 0, []
    fetch = fetch or fetch_page
    for i, st in enumerate(sorted(set(states))):
        slug = STATE_SLUGS.get(st)
        if not slug:
            failed.append(st)
            continue
        if i and pause:
            time.sleep(pause)
        t0 = time.time()
        ts = now if now is not None else t0
        try:
            page = parse_page(fetch(slug))
        except Exception as e:
            failed.append(st)
            rows.append({"ts": _iso(ts), "kind": "error", "state": st,
                         "err": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        ms = int((time.time() - t0) * 1000)
        ok += 1
        old = entries.get(st) or {}
        changed_at = old.get("changed_at")
        prev_count = old.get("prev_count")
        if old.get("count") is not None and old["count"] != page["count"]:
            changed_at, prev_count = _iso(ts), old["count"]
            changes.append(f"{st} {old['count']} -> {page['count']}")
        entries[st] = {"count": page["count"], "read_at": _iso(ts),
                       "changed_at": changed_at, "prev_count": prev_count}
        rows.append({"ts": _iso(ts), "kind": "read", "state": st,
                     "count": page["count"], "title_count": page["title_count"],
                     "operators": page["operators"], "markets": page["markets"],
                     "ms": ms})
        key = json.dumps([page["count"], page["geos"]], sort_keys=True)
        day = _iso(ts)[:10]
        if details.get(st) != f"{day}|{key}":
            details[st] = f"{day}|{key}"
            rows.append({"ts": _iso(ts), "kind": "detail", "state": st,
                         "count": page["count"], "geo_sum": page["geo_sum"],
                         "geos": page["geos"], "stats": page["stats"]})
    _append(log_dir, rows, now if now is not None else time.time())
    out = {"updated_at": _iso(now if now is not None else time.time()),
           "states": entries, "detail_keys": details}
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, path)
    return ok, failed, changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="one pass, then print")
    ap.add_argument("--states", default="AZ,CA,FL,GA,NY,OH,PA,TX,VA")
    ap.add_argument("--dir", default=os.environ.get(
        "IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm"))
    a = ap.parse_args(argv)
    if not a.once:
        ap.error("only --once is supported; the IMM's refresher thread loops")
    path = os.path.join(a.dir, "datacenter_fair.json")
    ok, failed, changes = write_fair_file(
        path, a.dir, [s.strip().upper() for s in a.states.split(",") if s.strip()])
    data = _load(path)
    for st, e in sorted((data.get("states") or {}).items()):
        print(f"{st} {e.get('count')} read {e.get('read_at')} changed {e.get('changed_at')}")
    print(f"read {ok}, failed {failed or 'none'}, changes {changes or 'none'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
