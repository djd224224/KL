#!/usr/bin/env python3
r"""combo_poly_lines.py -- Polymarket's moneylines as a THIRD FAIR for the combo
maker (Jack 2026-11-06: "use Polymarket as another fair so DraftKings + Polymarket
+ Kalshi"), where the sportsbook feed has nothing: UFC and soccer.

  PolyLines.refresh   Polymarket's public Gamma API: the moneyline markets of each
                      league's tag (GET /markets?tag_id=..&sports_market_types=
                      moneyline, ~90-300 KB a league), ending within days_ahead
  PolyLines.match     a Kalshi game-winner leg -> its Polymarket outcome:
                        soccer  by the slug, which carries Kalshi's own codes:
                                KXLALIGAGAME-26NOV06SEVALA-SEV / -TIE / -ALA ->
                                lal-sev-ala-2026-11-06-sev / -draw / -ala (a day
                                either side; else the team's name, not the draw)
                        UFC     by the fighter's full name (Kalshi's yes_sub_title
                                -> the market's outcomes), the card's date +-1 day
  PolyLines.p_yes     that outcome's mid, with the book's spread and liquidity

Read-only: public reads, no account, nothing sent.
"""
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

GAMMA = "https://gamma-api.polymarket.com"
# Kalshi series -> (Polymarket tag slugs to try, the market slug prefix); UFC goes by names
SERIES = {"KXUFCFIGHT": (("ufc",), "ufc"),
          "KXEPLGAME": (("epl",), "epl"),
          "KXLALIGAGAME": (("la-liga",), "lal"),
          "KXSERIEAGAME": (("sea", "serie-a"), "sea"),
          "KXBUNDESLIGAGAME": (("bundesliga",), "bun"),
          "KXLIGUE1GAME": (("ligue-1",), "fl1"),
          "KXMLSGAME": (("mls",), "mls"),
          "KXUCLGAME": (("ucl", "champions-league"), "ucl"),
          "KXUELGAME": (("uel", "europa-league"), "uel"),
          "KXUECLGAME": (("uecl", "conference-league"), "uecl"),
          "KXEREDIVISIEGAME": (("ere", "eredivisie"), "ere"),
          "KXLIGAMXGAME": (("mex", "liga-mx"), "mex")}
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                      "OCT", "NOV", "DEC"), 1)}
DROP = {"fc", "cf", "afc", "sc", "ac", "as", "ss", "ssc", "calcio", "club", "de", "del", "la", "le", "the", "cd",
        "rc", "us", "vfb", "vfl", "tsg", "fsv", "sv", "bv", "rcd", "ud", "sd", "ca", "real", "united", "city", "fk"}


def _http_get(path, params):
    import requests
    return requests.get(GAMMA + path, params=params, timeout=20).json()


def tokens(name) -> set:
    """'Deportivo Alavés' -> {'deportivo', 'alaves'}: lower case, accents off,
    the club words (FC, CF, Calcio ...) dropped."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", s) if w not in DROP} or set(re.findall(r"[a-z0-9]+", s))


def _last(market) -> str:
    """The last word of a Kalshi leg's name ('Seok Hyun Ko' -> 'ko')."""
    w = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str((market or {}).get("yes_sub_title") or ""))
                   .encode("ascii", "ignore").decode().lower())
    return w[-1] if w else ""


def kalshi_date(ticker: str):
    """KXLALIGAGAME-26NOV06SEVALA-SEV -> date(2026, 11, 6) (None if it doesn't parse)."""
    try:
        s = ticker.split("-")[1][:7]
        return date(2000 + int(s[:2]), MONTHS[s[2:5]], int(s[5:7]))
    except (IndexError, KeyError, ValueError):
        return None


class PolyLines:
    """Polymarket's moneylines for the SERIES leagues, refreshed on a background
    thread (start) or by hand (refresh); match / p_yes are dict lookups."""

    def __init__(self, get=_http_get, clock=time.time, days_ahead: float = 4.0, log=print):
        self.get, self.clock, self.days_ahead, self.log = get, clock, days_ahead, log
        self.tag_ids = {}               # tag slug -> id (None: Polymarket has no such tag)
        self.markets = {}               # market id -> {"bid", "ask", "liq", "outcomes", "slug"}
        self.by_slug = {}               # (prefix, date, codes, outcome) -> (market id, outcome index)
        self.by_name = {}               # (prefix, date) -> [(name tokens, market id, outcome index)]
        self.at, self.error, self.reads = None, None, 0
        self.lock = threading.Lock()
        self.thread = None

    def _tag(self, slug: str):
        if slug not in self.tag_ids:
            try:
                js = self.get(f"/tags/slug/{slug}", {})
                self.reads += 1
                self.tag_ids[slug] = (js or {}).get("id") if isinstance(js, dict) else None
            except Exception:
                self.tag_ids[slug] = None
        return self.tag_ids[slug]

    def refresh(self, now: float = None) -> int:
        """Re-read every league's moneylines; the new index replaces the old whole."""
        now = self.clock() if now is None else now
        lo = datetime.fromtimestamp(now - 3 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = datetime.fromtimestamp(now + self.days_ahead * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        markets, by_slug, by_name, errors = {}, {}, {}, []
        for series, (tags, prefix) in SERIES.items():
            tid = next((t for t in (self._tag(s) for s in tags) if t), None)
            if tid is None:
                continue
            try:
                rows = self.get("/markets", {"tag_id": tid, "sports_market_types": "moneyline", "closed": "false",
                                             "active": "true", "limit": 500, "end_date_min": lo, "end_date_max": hi})
                self.reads += 1
            except Exception as e:
                errors.append(f"{prefix}: {type(e).__name__}")
                continue
            for m in rows if isinstance(rows, list) else []:
                self._add(m, prefix, markets, by_slug, by_name)
        with self.lock:
            self.markets, self.by_slug, self.by_name = markets, by_slug, by_name
            self.at, self.error = now, ("; ".join(errors) or None)
        return len(markets)

    @staticmethod
    def _add(m: dict, prefix: str, markets: dict, by_slug: dict, by_name: dict) -> None:
        slug = str(m.get("slug") or "")
        if not slug.startswith(prefix + "-") or not m.get("acceptingOrders", True) or m.get("closed"):
            return
        try:
            import json
            outcomes = m.get("outcomes")
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else (outcomes or [])
            bid, ask = float(m.get("bestBid")), float(m.get("bestAsk"))
        except (TypeError, ValueError):
            return
        mid = str(m.get("id"))
        markets[mid] = {"bid": bid, "ask": ask, "liq": float(m.get("liquidityNum") or 0.0), "outcomes": outcomes,
                        "slug": slug}
        parts = slug.split("-")
        dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", slug)
        if not dm:
            return
        d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        if prefix == "ufc":                                   # a fight: its two fighters by name
            for i, o in enumerate(outcomes[:2]):
                by_name.setdefault((prefix, d), []).append((tokens(o), mid, i))
            return
        # soccer: <prefix>-<c1>-<c2>-<yyyy>-<mm>-<dd>-<outcome>, a Yes/No market per outcome
        if len(parts) < 7:
            return
        c1, c2, out = parts[1], parts[2], parts[-1]
        by_slug[(prefix, d, c1 + c2, out)] = (mid, 0)
        if out != "draw":
            by_name.setdefault((prefix, d), []).append((tokens(m.get("groupItemTitle") or ""), mid, 0))

    def match(self, ticker: str, market: dict = None):
        """(market id, outcome index) for a Kalshi game-winner leg, or None."""
        series = str(ticker).split("-")[0]
        if series not in SERIES:
            return None
        prefix = SERIES[series][1]
        d0 = kalshi_date(ticker)
        if d0 is None:
            return None
        parts = str(ticker).split("-")
        days = (d0, d0 + timedelta(days=1), d0 - timedelta(days=1))
        with self.lock:
            if prefix != "ufc" and len(parts) >= 3:
                codes, out = parts[1][7:].lower(), parts[-1].lower()
                out = "draw" if out == "tie" else out
                for d in days:
                    hit = self.by_slug.get((prefix, d, codes, out))
                    if hit:
                        return hit
                if out == "draw":
                    return None
            name = tokens((market or {}).get("yes_sub_title") or "")
            if not name:
                return None
            codes = parts[1][7:].lower() if len(parts) >= 2 else ""
            hits = []
            for d in days:
                for toks, mid, i in self.by_name.get((prefix, d), []):
                    if prefix == "ufc":
                        # a fighter by any spelling sharing the last name ("Seok Hyun Ko" / "Seokhyeon
                        # Ko"), in a fight whose BOTH fighters fit Kalshi's two codes
                        # (KXUFCFIGHT-26NOV06BASBRE = BAS + BRE): 11/06 Polymarket had Bashi vs
                        # Calderon where Kalshi had Bashi vs Brennan -- another bout is not this leg
                        if not (name <= toks or toks <= name or _last(market) in toks):
                            continue
                        if not self._fight_fits(mid, codes):
                            continue
                    elif not name <= toks:
                        continue
                    hits.append((mid, i))
                if hits:
                    break
            return hits[0] if len(hits) == 1 else None

    def _fight_fits(self, mid: str, codes: str) -> bool:
        if len(codes) != 6:
            return False
        outs = [tokens(o) for o in (self.markets.get(mid) or {}).get("outcomes", [])[:2]]
        if len(outs) != 2:
            return False
        fits = lambda toks, code: any(t.startswith(code) for t in toks)        # noqa: E731
        a, b = codes[:3], codes[3:]
        return (fits(outs[0], a) and fits(outs[1], b)) or (fits(outs[0], b) and fits(outs[1], a))

    def p_yes(self, hit):
        """(the outcome's mid, the book's spread, its liquidity) for a match, or None."""
        if not hit:
            return None
        with self.lock:
            m = self.markets.get(hit[0])
        if m is None or not (0.0 <= m["bid"] <= m["ask"] <= 1.0):
            return None
        mid = (m["bid"] + m["ask"]) / 2.0
        return (mid if hit[1] == 0 else 1.0 - mid), m["ask"] - m["bid"], m["liq"]

    def start(self, every: float = 180.0, stop=None) -> None:
        """Refresh on a background thread every `every` s (never on the quote path)."""
        stop = stop or threading.Event()

        def loop():
            while True:
                try:
                    n = self.refresh()
                    self.log(f"polymarket: {n} moneyline markets" + (f" ({self.error})" if self.error else ""))
                except Exception as e:                        # never kills the maker
                    self.error = f"{type(e).__name__}: {str(e)[:120]}"
                if stop.wait(every):
                    return
        self.thread = threading.Thread(target=loop, daemon=True, name="polymarket")
        self.thread.start()
