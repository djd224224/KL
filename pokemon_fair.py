#!/usr/bin/env python3
"""Collectr ungraded-price fair values for incentive_mm's Pokemon gate.

Jack 2026-10-03: "yes build the gate to quote all pokemon events", after
"are you able to quote KXPOKEMON-26OCTCHA based on Collectr realtime data".

THE MARKETS (rules text read 2026-10-03): KXPOKEMON-<YY><MON><CODE>-<K>, one
market per item per month, listed around the 2nd at 22:00 ET:
  "If the Ungraded Price of the <item> on Collectr is above $<K> on
   <Mon DD, YYYY>, then the market resolves to Yes."
<K> is Collectr's price when Kalshi lists the event (an at-the-money "Up or
Down"), strike_type greater, close 23:59 ET on the date; Kalshi settles on
the Collectr price it reads just after (September: 04:31Z on Oct 1). The
event code is NOT stable across months -- 26SEPCHA was "Charmander",
26OCTCHA is "Charizard" -- so products are mapped per EVENT, and the map's
item name must equal the rules'.

THE FEED -- TCGplayer, never Collectr:
  Collectr's catalog ids ARE TCGplayer product ids, and its ungraded price is
  TCGplayer's Market Price for the printing, or with no Market Price the
  listed median: 26OCTMEWBRG's strike 5010.87 is 717609's listedMedianPrice
  (no market price); on 2026-10-03 Collectr matched TCGplayer within 1% on
  11 of 13 cards, the gaps being Collectr's refresh lag (26OCTCHA: Collectr
  194.14, TCGplayer 171.77 the same morning). TCGplayer LEADS the source.
  Collectr has no public API -- its backend 403s non-browser clients and an
  AWS WAF CAPTCHA follows ~30 browser calls -- so it is not read here.
  TCGplayer (undocumented, plain requests):
    pricepoints  mpapi.tcgplayer.com/v2/product/<id>/pricepoints -- market
                 price + listed median per printing, every refresh
    details      mp-search-api.tcgplayer.com/v2/product/<id>/details --
                 sellers / listings, with the meta refresh
    search       mp-search-api.tcgplayer.com/v1/search/request (--suggest)

PRODUCT MAP -- pokemon_products.json (tracked; re-read when it changes):
event ticker -> {item, tcgplayer_id, printing, kind}. An open event with no
entry, or whose rules name another item, gets an `err` (the gate stands it
aside). `python pokemon_fair.py --suggest` lists TCGplayer candidates for
the unmapped events.

MODEL -- the settlement value V log-normal from today's TCGplayer value V0
over tau = months (30.44 days) to the close:
    ln(V / V0) ~ N(mu * tau, sigma^2 * tau)
    P(YES) = Phi((ln(V0 / (K + 0.005)) + mu * tau) / (sigma * sqrt(tau)))
("above" is strict and Collectr shows cents, hence K + 0.005). Calibrated
on the 53 settled KXPOKEMON markets of Jul-Sep 2026, strike -> settlement
(~1 month each; the 26SEPCHA mis-settlement at 190.24 dropped): cards
mean -9.0% sd 15.0% (n 25), sealed mean -4.4% sd 7.1% (n 28); 11 of 53
at-the-money strikes resolved YES. Defaults shrink the drift a third toward
zero and widen sigma: cards mu -6% sigma 18%, sealed mu -3% sigma 8%.
THIN items -- fewer than THIN_SELLERS sellers, or no Market Price (Collectr
then shows the listed median) -- move only when something sells: with
q = exp(-THIN_MOVES * tau) the chance that nothing does,
    P(YES) = q * [V0 > K + 0.005] + (1 - q) * P(sigma = THIN_SIGMA)
so an unmoved price that sits at its own strike (26OCTMEWRRG 6000.27) does
not clear it.
"""

import argparse
import json
import math
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from statistics import NormalDist
from typing import Callable, Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    ET = timezone(timedelta(hours=-5))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


SERIES_TICKER = os.environ.get("IMM_POKE_SERIES_TICKER", "KXPOKEMON")
PRODUCTS_FILE = os.environ.get(
    "IMM_POKE_PRODUCTS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "pokemon_products.json"))
# per-month drift and sigma of ln(V), by kind (see the docstring)
MU_CARD = _env_float("IMM_POKE_MU_CARD", -0.06)
SIGMA_CARD = _env_float("IMM_POKE_SIGMA_CARD", 0.18)
MU_SEALED = _env_float("IMM_POKE_MU_SEALED", -0.03)
SIGMA_SEALED = _env_float("IMM_POKE_SIGMA_SEALED", 0.08)
# thin items: fewer sellers than this (or no Market Price) price only on a sale
THIN_SELLERS = _env_float("IMM_POKE_THIN_SELLERS", 15)
THIN_SIGMA = _env_float("IMM_POKE_THIN_SIGMA", 0.25)
THIN_MOVES = _env_float("IMM_POKE_THIN_MOVES_PER_MONTH", 1.0)
# |ln(V0 / K)| past this reads as a wrong product, not a move (x2 / x0.5)
MAX_STRIKE_GAP = _env_float("IMM_POKE_MAX_STRIKE_GAP", 0.7)
# a read that moves V0 by at least this much stamps `moved_at` (the gate
# holds off for IMM_POKE_MOVE_HOLD_MIN after it)
MOVE_PCT = _env_float("IMM_POKE_MOVE_PCT", 0.05)
META_REFRESH_SECS = _env_float("IMM_POKE_META_REFRESH_SECS", 900)
TCG_MIN_GAP_SECS = _env_float("IMM_POKE_TCG_MIN_GAP_SECS", 0.25)

MONTH_DAYS = 30.4375
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
TCG_PRICEPOINTS = "https://mpapi.tcgplayer.com/v2/product/{pid}/pricepoints"
TCG_DETAILS = "https://mp-search-api.tcgplayer.com/v2/product/{pid}/details"
TCG_SEARCH = "https://mp-search-api.tcgplayer.com/v1/search/request"
HTTP_TIMEOUT = 15.0
KINDS = ("card", "sealed")

_RULES_RE = re.compile(
    r"Ungraded Price of the (?P<item>.+?) on Collectr is (?P<dir>above|below) "
    r"\$(?P<k>\d[\d,]*(?:\.\d+)?) on (?P<date>[A-Z][a-z]+\.? \d{1,2}, \d{4})")
_CARD_NO_RE = re.compile(r"\s+-\s+\d+/\d+$")       # "Mew ex - 158/128"
_N = NormalDist()


def _log(msg: str) -> None:
    # ASCII only: the IMM task console is cp1252
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} "
          f"[poke-fair] {msg}", flush=True)


def _num(v) -> Optional[float]:
    try:
        x = float(str(v).replace(",", "")) if v is not None else None
    except (TypeError, ValueError):
        return None
    return x if x is not None and math.isfinite(x) else None


def norm_item(s: str) -> str:
    """Item names compared the way a reader would: NFKC, straight quotes,
    one space, case-blind ("Team Rocket's" vs "Team Rocket’s")."""
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = s.replace("’", "'").replace("‘", "'")
    return " ".join(s.split()).casefold()


def et_date(ts_utc: datetime) -> date:
    return ts_utc.astimezone(ET).date()


def _parse_ts(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def parse_rules(text: str) -> Optional[dict]:
    """{'item', 'dir', 'k', 'date'} from a KXPOKEMON rules_primary, else None."""
    m = _RULES_RE.search(text or "")
    if not m:
        return None
    k = _num(m.group("k"))
    raw = m.group("date").replace(".", "")
    d = None
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            d = datetime.strptime(raw, fmt).date()
            break
        except ValueError:
            continue
    if k is None or d is None:
        return None
    return {"item": " ".join(m.group("item").split()), "dir": m.group("dir"),
            "k": k, "date": d}


# ------------------------------------------------------------------- model

def p_above(v0: float, k: float, tau_m: float, mu: float, sigma: float) -> float:
    """P(V > K) for ln(V / v0) ~ N(mu tau, sigma^2 tau): strict, at cents."""
    bar = k + 0.005
    if tau_m <= 0 or sigma <= 0:
        return 1.0 if v0 > bar else 0.0
    return _N.cdf((math.log(v0 / bar) + mu * tau_m) / (sigma * math.sqrt(tau_m)))


def p_thin(v0: float, k: float, tau_m: float, mu: float, sigma: float,
           moves_per_month: float) -> Tuple[float, float]:
    """(P(YES), q) for an item whose price moves only on a sale: q the
    chance nothing sells by the close, when V stays V0."""
    q = math.exp(-max(moves_per_month, 0.0) * max(tau_m, 0.0))
    stay = 1.0 if v0 > k + 0.005 else 0.0
    return q * stay + (1.0 - q) * p_above(v0, k, tau_m, mu, sigma), q


def pick_price(points, printing: Optional[str]) -> dict:
    """V0 the way Collectr shows it: TCGplayer's Market Price for the mapped
    printing, else its listed median. {'v0', 'src', 'market', 'median',
    'printing'} or {'err'}."""
    rows = [p for p in (points if isinstance(points, list) else [])
            if isinstance(p, dict)]
    if printing:
        rows = [p for p in rows
                if str(p.get("printingType") or "").casefold() == printing.casefold()]
        if not rows:
            return {"err": f"no {printing} printing on TCGplayer"}
    else:
        rows = [p for p in rows if _num(p.get("marketPrice"))
                or _num(p.get("listedMedianPrice"))]
        if len(rows) != 1:
            return {"err": f"{len(rows)} priced printings and none mapped"}
    p = rows[0]
    mk, md = _num(p.get("marketPrice")), _num(p.get("listedMedianPrice"))
    base = {"market": mk, "median": md, "printing": p.get("printingType")}
    if mk is not None and mk > 0:
        return dict(base, v0=mk, src="market")
    if md is not None and md > 0:
        return dict(base, v0=md, src="median")
    return {"err": "no TCGplayer market price or listed median"}


def market_entry(m: dict, spec: Optional[dict], price: Optional[dict],
                 sellers: Optional[float], now: datetime) -> dict:
    """One market's gate entry: {'p', 'k', 'v0', ...} or {'err': why}."""
    t = m.get("ticker") or ""
    ev = m.get("event_ticker") or t.rsplit("-", 1)[0]
    r = parse_rules(m.get("rules_primary") or "")
    if r is None:
        return {"event": ev, "err": "rules: not 'Ungraded Price of the <item> "
                                    "on Collectr is above $K on <date>'"}
    e = {"event": ev, "item": r["item"], "k": r["k"],
         "date": r["date"].isoformat()}
    if r["dir"] != "above" or (m.get("strike_type") or "greater") != "greater":
        return dict(e, err=f"{r['dir']} / strike_type {m.get('strike_type')}")
    fk, tk = _num(m.get("floor_strike")), _num(t.rsplit("-", 1)[-1])
    if fk is None or tk is None or abs(fk - r["k"]) > 0.005 \
            or abs(tk - r["k"]) > 0.005:
        return dict(e, err=f"strike mismatch: rules {r['k']} floor {fk} "
                           f"ticker {t.rsplit('-', 1)[-1]}")
    ct = _parse_ts(m.get("close_time"))
    if ct is None:
        return dict(e, err="no close_time")
    if et_date(ct) != r["date"]:
        return dict(e, err=f"close {et_date(ct)} is not the rules date {r['date']}")
    if spec is None:
        return dict(e, err="unmapped event (pokemon_products.json)")
    if norm_item(spec.get("item")) != norm_item(r["item"]):
        return dict(e, err=f"rules name '{r['item']}', the map "
                           f"'{spec.get('item')}'")
    pid = spec["tcgplayer_id"]
    e["pid"] = pid
    if price is None:
        return dict(e, err=f"no TCGplayer read for {pid} yet")
    if price.get("err"):
        return dict(e, err=f"TCGplayer {pid}: {price['err']}")
    if sellers is None:
        return dict(e, err=f"no TCGplayer seller count for {pid} yet")
    v0 = float(price["v0"])
    gap = math.log(v0 / r["k"])
    if abs(gap) > MAX_STRIKE_GAP:
        return dict(e, v0=v0, err=f"TCGplayer {v0:.2f} is {gap:+.0%} (log) from "
                                  f"the strike: check the mapping")
    kind = spec.get("kind") or "card"
    mu, sigma = (MU_SEALED, SIGMA_SEALED) if kind == "sealed" else (MU_CARD, SIGMA_CARD)
    tau = max((ct - now).total_seconds(), 0.0) / (MONTH_DAYS * 86400.0)
    thin = price["src"] != "market" or sellers < THIN_SELLERS
    e.update(kind=kind, v0=v0, src=price["src"], sellers=sellers, thin=thin,
             tau_m=round(tau, 4), mu=mu, price_ts=price.get("ts"),
             moved_at=price.get("moved_at"), last_move=price.get("last_move"))
    if thin:
        p, q = p_thin(v0, r["k"], tau, mu, THIN_SIGMA, THIN_MOVES)
        e.update(sigma=THIN_SIGMA, q=round(q, 4), p=p)
    else:
        e.update(sigma=sigma, p=p_above(v0, r["k"], tau, mu, sigma))
    return e


# ------------------------------------------------------------------- reads

_signed_err: Optional[str] = None


def _read(get_json: Optional[Callable[[str, dict], dict]], path: str,
          params: dict) -> dict:
    """A Kalshi read: signed through `get_json` when given, else -- or when
    that fails -- the public endpoint (raises on a public failure)."""
    global _signed_err
    if get_json is not None:
        try:
            js = get_json(path, dict(params))
            if not isinstance(js, dict):
                raise ValueError(f"signed read returned {type(js).__name__}")
            if _signed_err is not None:
                _log("signed Kalshi reads back")
                _signed_err = None
            return js
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:120]}"
            if err != _signed_err:
                _log(f"! signed Kalshi read failed ({err}); reading the public endpoint")
            _signed_err = err
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = requests.get(KALSHI_BASE + path, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(2.0 * (attempt + 1) ** 2)
    raise RuntimeError(f"Kalshi {path} failed: {last}")


def _markets(get_json, params: dict) -> List[dict]:
    out: List[dict] = []
    cursor = None
    for _ in range(10):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        js = _read(get_json, "/markets", p)
        out.extend(js.get("markets") or [])
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


def load_products(path: str = PRODUCTS_FILE) -> Dict[str, dict]:
    """event ticker -> validated map entry. Raises on an unreadable file;
    a malformed entry is dropped (its event then reads as unmapped)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out: Dict[str, dict] = {}
    for ev, ent in ((data or {}).get("events") or {}).items():
        if not isinstance(ent, dict):
            continue
        try:
            pid = int(ent["tcgplayer_id"])
            item = str(ent["item"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        kind = str(ent.get("kind") or "card")
        if pid <= 0 or not item or kind not in KINDS:
            continue
        out[str(ev)] = {"item": item, "tcgplayer_id": pid, "kind": kind,
                        "printing": (str(ent["printing"]) if ent.get("printing")
                                     else None)}
    return out


class PokemonWatch:
    """The refresher's state: the family's open markets, the product map and
    the TCGplayer seller counts every META_REFRESH_SECS, each mapped item's
    TCGplayer price every call; `snap` is what the gate reads."""

    def __init__(self, get_json: Optional[Callable[[str, dict], dict]] = None,
                 session=None, products_file: str = PRODUCTS_FILE):
        self.get_json = get_json
        self.session = session if session is not None else requests.Session()
        self.products_file = products_file
        self.products: Dict[str, dict] = {}
        self.products_mtime: Optional[float] = None
        self.meta_at = 0.0
        self.family: List[dict] = []
        self.sellers: Dict[int, float] = {}          # pid -> last good count
        self.prices: Dict[int, dict] = {}            # pid -> last read
        self._tcg_at = 0.0
        self.snap: Optional[dict] = None

    def _tcg(self, url: str):
        gap = TCG_MIN_GAP_SECS - (time.time() - self._tcg_at)
        if gap > 0:
            time.sleep(gap)
        self._tcg_at = time.time()
        r = self.session.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _load_products(self, errors: List[str]) -> None:
        try:
            mt = os.path.getmtime(self.products_file)
        except OSError as e:
            errors.append(f"map: {type(e).__name__}: {str(e)[:100]}")
            return
        if mt == self.products_mtime:
            return
        try:
            self.products = load_products(self.products_file)
            self.products_mtime = mt
            _log(f"product map: {len(self.products)} events")
        except (OSError, ValueError) as e:
            errors.append(f"map: {type(e).__name__}: {str(e)[:100]}")

    def mapped_pids(self) -> Dict[int, Optional[str]]:
        """pid -> printing for the mapped events with an open market."""
        out: Dict[int, Optional[str]] = {}
        for m in self.family:
            ev = m.get("event_ticker") or (m.get("ticker") or "").rsplit("-", 1)[0]
            spec = self.products.get(ev)
            if spec:
                out[spec["tcgplayer_id"]] = spec.get("printing")
        return out

    def _read_price(self, pid: int, printing: Optional[str], now_ts: float) -> None:
        got = pick_price(self._tcg(TCG_PRICEPOINTS.format(pid=pid)), printing)
        prev = self.prices.get(pid) or {}
        got["ts"] = now_ts
        got["moved_at"] = prev.get("moved_at")
        got["last_move"] = prev.get("last_move")
        pv = prev.get("v0")
        if "v0" in got and pv:
            mv = math.log(got["v0"] / pv)
            if abs(mv) >= MOVE_PCT:
                got["moved_at"], got["last_move"] = now_ts, round(mv, 4)
                _log(f"{pid}: {pv:.2f} -> {got['v0']:.2f} ({mv:+.1%}, {got['src']})")
        self.prices[pid] = got

    def refresh(self, now_ts: Optional[float] = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        errors: List[str] = []
        self._load_products(errors)
        meta_due = now_ts - self.meta_at >= META_REFRESH_SECS or not self.family
        if meta_due:
            try:
                self.family = _markets(self.get_json, {
                    "series_ticker": SERIES_TICKER, "status": "open",
                    "limit": 1000})
                self.meta_at = now_ts
            except Exception as e:                  # noqa: BLE001
                errors.append(f"meta: {type(e).__name__}: {str(e)[:100]}")
        for pid, printing in sorted(self.mapped_pids().items()):
            if meta_due or pid not in self.sellers:
                try:
                    s = _num(self._tcg(TCG_DETAILS.format(pid=pid)).get("sellers"))
                    if s is not None:
                        self.sellers[pid] = s
                except Exception as e:              # noqa: BLE001
                    errors.append(f"details {pid}: {type(e).__name__}: {str(e)[:80]}")
            try:
                self._read_price(pid, printing, now_ts)
            except Exception as e:                  # noqa: BLE001
                # the last good read stays; its price_ts ages out in the gate
                errors.append(f"price {pid}: {type(e).__name__}: {str(e)[:80]}")
        self.snap = build_snapshot(now_ts, self.family, self.products,
                                   self.prices, self.sellers, errors)
        return self.snap


def build_snapshot(now_ts: float, family: List[dict], products: Dict[str, dict],
                   prices: Dict[int, dict], sellers: Dict[int, float],
                   errors: Optional[List[str]] = None) -> dict:
    now = datetime.fromtimestamp(now_ts, timezone.utc)
    entries: Dict[str, dict] = {}
    unmapped = set()
    for m in family:
        t = m.get("ticker")
        if not t:
            continue
        ev = m.get("event_ticker") or t.rsplit("-", 1)[0]
        spec = products.get(ev)
        if spec is None:
            unmapped.add(ev)
        pid = spec["tcgplayer_id"] if spec else None
        entries[t] = market_entry(m, spec, prices.get(pid) if pid else None,
                                  sellers.get(pid) if pid else None, now)
    return {
        "ts": now_ts,
        "fetched_at": now.isoformat(),
        "markets": entries,
        "prices": {str(k): dict(v, sellers=sellers.get(k))
                   for k, v in sorted(prices.items())},
        "unmapped": sorted(unmapped),
        "errors": list(errors or []),
        "model": {"mu_card": MU_CARD, "sigma_card": SIGMA_CARD,
                  "mu_sealed": MU_SEALED, "sigma_sealed": SIGMA_SEALED,
                  "thin_sellers": THIN_SELLERS, "thin_sigma": THIN_SIGMA,
                  "thin_moves_per_month": THIN_MOVES,
                  "max_strike_gap": MAX_STRIKE_GAP, "move_pct": MOVE_PCT,
                  "meta_refresh_secs": META_REFRESH_SECS,
                  "products_file": PRODUCTS_FILE},
    }


def write_status(path: str, snap: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


# ---------------------------------------------------------------- --suggest

def search_tcgplayer(session, q: str, size: int = 24) -> List[dict]:
    """TCGplayer's own site search, Pokemon product line only."""
    body = {"algorithm": "sales_dismax", "from": 0, "size": size,
            "filters": {"term": {"productLineName": ["pokemon"]},
                        "range": {}, "match": {}},
            "listingSearch": {"context": {"cart": {}}, "filters": {
                "term": {"sellerStatus": "Live", "channelId": 0},
                "range": {"quantity": {"gte": 1}},
                "exclude": {"channelExclusion": 0}}},
            "context": {"cart": {}, "shippingCountry": "US", "userProfile": {}},
            "settings": {"useFuzzySearch": True, "didYouMean": {}}, "sort": {}}
    r = session.post(TCG_SEARCH, params={"q": q, "isList": "false"}, json=body,
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    res = ((r.json().get("results") or [{}])[0].get("results")) or []
    return [{"pid": int(x.get("productId") or 0),
             "name": str(x.get("productName") or ""),
             "set": str(x.get("setName") or ""),
             "market": _num(x.get("marketPrice"))} for x in res]


def suggest(get_json=None, session=None, products_file: str = PRODUCTS_FILE
            ) -> List[str]:
    """Lines for a human: each open, unmapped event with its TCGplayer
    candidates (exact-name matches first, the card number stripped as
    Collectr strips it) and their price against the strike."""
    session = session if session is not None else requests.Session()
    products = load_products(products_file)
    lines: List[str] = []
    for m in _markets(get_json, {"series_ticker": SERIES_TICKER,
                                 "status": "open", "limit": 1000}):
        ev = m.get("event_ticker") or ""
        if ev in products:
            continue
        r = parse_rules(m.get("rules_primary") or "")
        if r is None:
            lines.append(f"{ev}: rules not parsed")
            continue
        lines.append(f"{ev}  item '{r['item']}'  K {r['k']:.2f}  "
                     f"(opened {m.get('open_time')})")
        cands = search_tcgplayer(session, r["item"])
        exact = [c for c in cands if norm_item(_CARD_NO_RE.sub("", c["name"]))
                 == norm_item(r["item"])]
        for c in (exact or cands)[:8]:
            try:
                pr = pick_price(session.get(TCG_PRICEPOINTS.format(pid=c["pid"]),
                                            timeout=HTTP_TIMEOUT).json(), None)
            except Exception as e:                  # noqa: BLE001
                pr = {"err": type(e).__name__}
            v = pr.get("v0")
            lines.append(
                f"    {'=' if c in exact else '~'} {c['pid']:>8}  {c['name'][:44]:44} "
                f"{c['set'][:34]:34} "
                + (f"{v:10.2f} ({pr['src']}, {pr['printing']}) {math.log(v / r['k']):+.1%}"
                   if v else pr.get("err", "")))
            time.sleep(TCG_MIN_GAP_SECS)
    return lines


if __name__ == "__main__":                          # a dry read, no orders
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="KXPOKEMON fair values (read-only)")
    ap.add_argument("--suggest", action="store_true",
                    help="TCGplayer candidates for the open, unmapped events")
    args = ap.parse_args()
    try:
        from kalshi_reads import kalshi_get
        gj = lambda p, q: kalshi_get(p, q)          # noqa: E731
    except Exception:                               # pragma: no cover
        gj = None
    if args.suggest:
        for line in suggest(gj):
            print(line)
        raise SystemExit(0)
    snap = PokemonWatch(gj).refresh()
    print(f"unmapped {snap['unmapped']}, errors {snap['errors']}")
    for t, e in sorted(snap["markets"].items()):
        if "p" in e:
            print(f"  {t:34} p {e['p'] * 100:5.1f}c  V0 {e['v0']:9.2f} ({e['src']}"
                  f"{', thin q ' + format(e['q'], '.2f') if e['thin'] else ''}) "
                  f"K {e['k']:9.2f}  {math.log(e['v0'] / e['k']):+6.1%}  "
                  f"tau {e['tau_m']:.2f}m  sellers {e['sellers']:g}")
        else:
            print(f"  {t:34} {e.get('err', '')}")
