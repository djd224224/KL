#!/usr/bin/env python3
r"""combo_poly_lines.py -- Polymarket's moneylines as a THIRD FAIR for the combo
maker (Jack 2026-11-06: "use Polymarket as another fair so DraftKings + Polymarket
+ Kalshi"): UFC and soccer, which the sportsbook feed lacks, and the NBA, NFL, NHL
and college football, where its line can be missing (11/06: none yet for the 11/07
NBA slate) or its team codes don't line up (college football).

  PolyLines.refresh   Polymarket's public Gamma API: the moneyline markets of each
                      league's tag (GET /markets?tag_id=..&sports_market_types=
                      moneyline, ~90-300 KB a league), ending within days_ahead
  PolyLines.match     a Kalshi game-winner leg -> its Polymarket outcome, the first of
                        slug      Polymarket's slugs carry Kalshi's own codes:
                                  soccer, a Yes/No market an outcome:
                                  KXLALIGAGAME-26NOV06SEVALA-SEV / -TIE / -ALA ->
                                  lal-sev-ala-2026-11-06-sev / -draw / -ala;
                                  US, one market a game, outcomes in the slug's order:
                                  KXNBAGAME-26NOV07SASPOR-SAS -> nba-sas-por-2026-11-07
                        one code  soccer, NBA, NFL, NHL, where Polymarket codes one
                                  team differently (Kalshi ATH / Polymarket bil, AVL /
                                  ast, CGY / cal): the one game that day holding the
                                  other team's code
                        names     the two sides Kalshi's rules name ("the Virginia Tech
                                  vs SMU college football game"): college football (its
                                  codes rarely agree) and UFC, the market whose two
                                  outcomes they are (college football exactly: "Texas"
                                  is not "Texas Tech"); soccer, the one game that day
                                  holding both teams, or one with the other named
                                  nowhere (Kalshi "Bayern Munich", Polymarket "FC
                                  Bayern München") -- a draw too
                        UFC       the fighter's name in a fight whose BOTH fighters fit
                                  Kalshi's two codes (KXUFCFIGHT-26NOV06BASBRE = BAS + BRE)
                        soccer    the team's name (not the draw)
                      the game's day or one either side; the NBA and NHL (back-to-
                      backs) that day only
  PolyLines.p_yes     that outcome's mid, with the book's spread and liquidity
  PolyLines.bout_changed  a UFC bout Polymarket shows changed: one of the fighters
                      Kalshi's rules name in a fight against someone else (11/06:
                      Kalshi's Austin Bashi vs Lucas Brennan, Polymarket's Bashi vs
                      Calderon after Brennan's injury). The rules name both fighters,
                      so both legs of the bout see it

Read-only: public reads, no account, nothing sent.
"""
import json
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

GAMMA = "https://gamma-api.polymarket.com"
# Kalshi series -> (Polymarket tag slugs to try, the market slug prefix)
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
          "KXLIGAMXGAME": (("mex", "liga-mx"), "mex"),
          "KXNBAGAME": (("nba",), "nba"),
          "KXNFLGAME": (("nfl",), "nfl"),
          "KXNHLGAME": (("nhl",), "nhl"),
          "KXNCAAFGAME": (("cfb", "ncaaf"), "cfb")}
US = {"nba", "nfl", "nhl", "cfb"}           # one two-outcome market a game
SAME_DAY = {"nba", "nhl"}                   # back-to-backs: the game's own day only
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                      "OCT", "NOV", "DEC"), 1)}
DROP = {"fc", "cf", "afc", "sc", "ac", "as", "ss", "ssc", "calcio", "club", "de", "del", "la", "le", "the", "cd",
        "rc", "us", "vfb", "vfl", "tsg", "fsv", "sv", "bv", "rcd", "ud", "sd", "ca", "real", "united", "city", "fk"}
SUFFIX = {"jr", "sr", "ii", "iii", "iv"}
ALIAS = {"st": "state"}                     # Kalshi's "Penn St." is Polymarket's "Penn State"
# "If SMU wins the Virginia Tech vs SMU college football game originally scheduled for ...",
# "If Tie is the result of the Malaga vs Santander professional La Liga soccer game ..."
_VS = re.compile(r"\b(?:wins|is the result of) the (.+?) vs\.? (.+?) originally scheduled")
_TAIL = re.compile(r"\s+(?:professional (?:[A-Za-z0-9.]+ )*?(?:soccer game|MMA fight)|Pro [A-Z][a-z]+ game"
                   r"|college football game|[A-Z]{2,5} game)$")


def _http_get(path, params):
    import requests
    return requests.get(GAMMA + path, params=params, timeout=20).json()


def _words(name) -> list:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", s)


def tokens(name) -> set:
    """'Deportivo Alavés' -> {'deportivo', 'alaves'}: lower case, accents off,
    the club words (FC, CF, Calcio ...) dropped, St. read as State."""
    w = [ALIAS.get(x, x) for x in _words(name)]
    return {x for x in w if x not in DROP} or set(w)


def _lastw(name) -> str:
    """A name's last word, a Jr. / III passed over ('Seok Hyun Ko' -> 'ko')."""
    w = [x for x in _words(name) if x not in SUFFIX]
    return w[-1] if w else ""


def _last(market) -> str:
    """The last word of a Kalshi leg's name."""
    return _lastw((market or {}).get("yes_sub_title"))


def _same(a: set, b: set, strict: bool) -> bool:
    """One name: the same words (strict), or one's inside the other's with two words
    or more to it ("Austin Bashi" / "Austin J Bashi"; never "Texas" / "Texas Tech")."""
    if not a or not b:
        return False
    return a == b or (not strict and min(len(a), len(b)) >= 2 and (a <= b or b <= a))


def kalshi_date(ticker: str):
    """KXLALIGAGAME-26NOV06SEVALA-SEV -> date(2026, 11, 6) (None if it doesn't parse)."""
    try:
        s = ticker.split("-")[1][:7]
        return date(2000 + int(s[:2]), MONTHS[s[2:5]], int(s[5:7]))
    except (IndexError, KeyError, ValueError):
        return None


def kalshi_sides(market):
    """The two sides a Kalshi leg's rules name ("If SMU wins the Virginia Tech vs SMU
    college football game originally scheduled ..." -> ("Virginia Tech", "SMU")),
    or None."""
    m = _VS.search(re.sub(r"\s+", " ", str((market or {}).get("rules_primary") or "")))
    if not m:
        return None
    a, b = m.group(1).strip(), _TAIL.sub("", m.group(2).strip())
    return (a, b) if a and b else None


def _split(codes: str, out: str):
    """Kalshi's game code as its two teams' codes, by the leg's own (ANACGY + CGY ->
    ana, cgy: codes run 2-4 letters); a draw splits a six-letter code 3 + 3."""
    if out == "draw":
        return (codes[:3], codes[3:]) if len(codes) == 6 else None
    if not out or len(out) >= len(codes):
        return None
    head, tail = codes.startswith(out), codes.endswith(out)
    if head == tail:                                    # neither, or both: can't tell
        return None
    return (out, codes[len(out):]) if head else (codes[:-len(out)], out)


class PolyLines:
    """Polymarket's moneylines for the SERIES leagues, refreshed on a background
    thread (start) or by hand (refresh); match / p_yes / bout_changed are lookups."""

    def __init__(self, get=_http_get, clock=time.time, days_ahead: float = 4.0, log=print, keep_s: float = 900.0):
        self.get, self.clock, self.days_ahead, self.log = get, clock, days_ahead, log
        self.keep_s = keep_s            # a league whose read fails keeps its last good one this long
        self.league_at = {}             # prefix -> the last good read's time
        self.tag_ids = {}               # tag slug -> id (None: Polymarket has no such tag)
        self.markets = {}               # market id -> {"bid", "ask", "liq", "outcomes", "slug"}
        self.by_slug = {}               # (prefix, date, codes, outcome) -> (market id, outcome index)
        self.by_name = {}               # (prefix, date) -> [(name tokens, market id, outcome index)]
        self.by_game = {}               # (prefix, date) -> {(code 1, code 2)}
        self.by_pair = {}               # (prefix, date) -> [(outcome 0 tokens, outcome 1 tokens, market id)]
        self.at, self.error, self.reads = None, None, 0
        self.lock = threading.Lock()
        self.thread = None

    def _tag(self, slug: str):
        if slug not in self.tag_ids:
            try:
                js = self.get(f"/tags/slug/{slug}", {})
            except Exception:
                return None                             # unknown yet: asked again next read
            self.reads += 1
            self.tag_ids[slug] = (js or {}).get("id") if isinstance(js, dict) else None
        return self.tag_ids[slug]

    def refresh(self, now: float = None) -> int:
        """Re-read every league's moneylines; the new index replaces the old whole,
        but a league whose read fails keeps its last good one up to keep_s."""
        now = self.clock() if now is None else now
        lo = datetime.fromtimestamp(now - 3 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = datetime.fromtimestamp(now + self.days_ahead * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        markets, ix, errors, keep = {}, ({}, {}, {}, {}), [], set()

        def failed(prefix, why):
            errors.append(f"{prefix}: {why}")
            if now - self.league_at.get(prefix, float("-inf")) <= self.keep_s:
                keep.add(prefix)
        for series, (tags, prefix) in SERIES.items():
            tid = next((t for t in (self._tag(s) for s in tags) if t), None)
            if tid is None:
                if any(s not in self.tag_ids for s in tags):
                    failed(prefix, "tag read")
                continue
            try:
                rows = self.get("/markets", {"tag_id": tid, "sports_market_types": "moneyline", "closed": "false",
                                             "active": "true", "limit": 500, "end_date_min": lo, "end_date_max": hi})
                self.reads += 1
            except Exception as e:
                failed(prefix, type(e).__name__)
                continue
            self.league_at[prefix] = now
            for m in rows if isinstance(rows, list) else []:
                self._add(m, prefix, markets, ix)
        with self.lock:
            if keep:                                    # the failed leagues' last good read
                markets.update({k: m for k, m in self.markets.items() if m["slug"].split("-")[0] in keep})
                for old, new in zip((self.by_slug, self.by_name, self.by_game, self.by_pair), ix):
                    new.update({k: v for k, v in old.items() if k[0] in keep})
            self.markets = markets
            self.by_slug, self.by_name, self.by_game, self.by_pair = ix
            self.at, self.error = now, ("; ".join(errors) or None)
        return len(markets)

    @staticmethod
    def _add(m: dict, prefix: str, markets: dict, ix: tuple) -> None:
        by_slug, by_name, by_game, by_pair = ix
        slug = str(m.get("slug") or "")
        if not slug.startswith(prefix + "-") or not m.get("acceptingOrders", True) or m.get("closed"):
            return
        try:
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
            if len(outcomes) == 2:
                by_pair.setdefault((prefix, d), []).append((tokens(outcomes[0]), tokens(outcomes[1]), mid))
            return
        if prefix in US:
            # <prefix>-<c1>-<c2>-<yyyy>-<mm>-<dd>: one market a game, its two outcomes the
            # teams in the slug's order (nba-sas-por-2026-11-07: ["Spurs", "Trail Blazers"])
            if len(parts) != 6 or len(outcomes) != 2:
                return
            c1, c2 = parts[1], parts[2]
            for codes in (c1 + c2, c2 + c1):
                by_slug[(prefix, d, codes, c1)] = (mid, 0)
                by_slug[(prefix, d, codes, c2)] = (mid, 1)
            by_game.setdefault((prefix, d), set()).add((c1, c2))
            by_pair.setdefault((prefix, d), []).append((tokens(outcomes[0]), tokens(outcomes[1]), mid))
            return
        # soccer: <prefix>-<c1>-<c2>-<yyyy>-<mm>-<dd>-<outcome>, a Yes/No market per outcome
        if len(parts) < 7:
            return
        c1, c2, out = parts[1], parts[2], parts[-1]
        markets[mid].update(game=(c1, c2), out=out)
        for codes in (c1 + c2, c2 + c1):
            by_slug[(prefix, d, codes, out)] = (mid, 0)
        by_game.setdefault((prefix, d), set()).add((c1, c2))
        if out != "draw":
            by_name.setdefault((prefix, d), []).append((tokens(m.get("groupItemTitle") or ""), mid, 0))

    def match(self, ticker: str, market: dict = None):
        """(market id, outcome index) for a Kalshi game-winner leg, or None."""
        series = str(ticker).split("-")[0]
        if series not in SERIES:
            return None
        prefix = SERIES[series][1]
        d0, parts = kalshi_date(ticker), str(ticker).split("-")
        if d0 is None or len(parts) < 3:
            return None
        days = (d0,) if prefix in SAME_DAY else (d0, d0 + timedelta(days=1), d0 - timedelta(days=1))
        codes, out = parts[1][7:].lower(), parts[-1].lower()
        with self.lock:
            if prefix == "ufc":
                return self._by_names(prefix, days, market, strict=False) or self._ufc_name(days, codes, market)
            out = "draw" if out == "tie" else out
            for d in days:
                hit = self.by_slug.get((prefix, d, codes, out))
                if hit:
                    return hit
            if prefix == "cfb":
                return self._by_names(prefix, days, market, strict=True)
            hit = self._one_code(prefix, days, codes, out, market)
            if hit or prefix in US:
                return hit
            hit = self._soccer_by_names(prefix, days, market, out)
            if hit is not None or out == "draw":             # False: the names disagree, no guessing
                return hit or None
            return self._soccer_name(prefix, days, market)

    def _one_code(self, prefix: str, days, codes: str, out: str, market=None):
        """A game Polymarket codes ONE team of differently (11/06: Kalshi OSAATH /
        Polymarket osa-bil, MUNAVL / mun-ast, ANACGY / ana-cal): the one game that day
        holding the other team's code; this leg is that team, the other, or the draw.
        A team Kalshi's rules name in another game that day says it isn't."""
        pair = _split(codes, out)
        if pair is None:
            return None
        for d in days:
            hits = [(c1, c2, k) for c1, c2 in self.by_game.get((prefix, d), ()) for k in pair if k in (c1, c2)]
            if not hits:
                continue
            if len(hits) != 1:
                return None
            c1, c2, k = hits[0]
            if not self._names_agree(prefix, d, (c1, c2), market):
                return None
            o = "draw" if out == "draw" else k if out == k else (c2 if k == c1 else c1)
            return self.by_slug.get((prefix, d, c1 + c2, o))
        return None

    def _names_agree(self, prefix: str, d, g: tuple, market) -> bool:
        """Neither side Kalshi's rules name is a team only in another game that day
        (soccer, where Polymarket's outcomes carry team names)."""
        sides = kalshi_sides(market)
        for side in sides or ():
            k = tokens(side)
            games = {(self.markets.get(mid) or {}).get("game") for toks, mid, _ in self.by_name.get((prefix, d), [])
                     if k <= toks}
            if games and g not in games:
                return False
        return True

    def _by_names(self, prefix: str, days, market, strict: bool):
        """The market whose two outcomes are the two sides Kalshi's rules name; this
        leg is the outcome that is its yes_sub_title."""
        sides = kalshi_sides(market)
        own = tokens((market or {}).get("yes_sub_title") or "")
        if not sides or not own:
            return None
        a, b = tokens(sides[0]), tokens(sides[1])
        same = lambda x, y: _same(x, y, strict)           # noqa: E731
        for d in days:
            hits = []
            for t0, t1, mid in self.by_pair.get((prefix, d), ()):
                if not ((same(a, t0) and same(b, t1)) or (same(a, t1) and same(b, t0))):
                    continue
                if same(own, t0) != same(own, t1):
                    hits.append((mid, 0 if same(own, t0) else 1))
            if hits:
                return hits[0] if len(hits) == 1 else None
        return None

    def _ufc_name(self, days, codes: str, market):
        # a fighter by any spelling sharing the last name ("Seok Hyun Ko" / "Seokhyeon Ko"),
        # in a fight whose BOTH fighters fit Kalshi's two codes (KXUFCFIGHT-26NOV06BASBRE =
        # BAS + BRE): 11/06 Polymarket had Bashi vs Calderon where Kalshi had Bashi vs
        # Brennan -- another bout is not this leg
        name = tokens((market or {}).get("yes_sub_title") or "")
        if not name:
            return None
        for d in days:
            hits = [(mid, i) for toks, mid, i in self.by_name.get(("ufc", d), [])
                    if (name <= toks or toks <= name or _last(market) in toks) and self._fight_fits(mid, codes)]
            if hits:
                return hits[0] if len(hits) == 1 else None
        return None

    def _soccer_by_names(self, prefix: str, days, market, out: str):
        """Soccer by the two teams Kalshi's rules name: the one game that day holding
        both, or one with the other nowhere that day (Kalshi's "Bayern Munich" is
        Polymarket's "FC Bayern München"); this leg is a team or the draw. False:
        the two teams are in different games, or the game doesn't say which this is."""
        sides = kalshi_sides(market)
        if not sides:
            return None
        ka, kb = tokens(sides[0]), tokens(sides[1])
        own = None if out == "draw" else tokens((market or {}).get("yes_sub_title") or "")
        if own is not None and own not in (ka, kb):
            return None
        for d in days:
            teams = [(toks, mid, self.markets[mid]) for toks, mid, _ in self.by_name.get((prefix, d), [])
                     if "game" in self.markets.get(mid, {})]
            ga = {m["game"] for toks, _, m in teams if ka <= toks}
            gb = {m["game"] for toks, _, m in teams if kb <= toks}
            if not ga and not gb:
                continue
            if len(ga & gb) == 1:
                g = next(iter(ga & gb))
            elif not ga & gb and len(ga) + len(gb) == 1:   # one side, the other nowhere that day
                g = next(iter(ga or gb))
            else:
                return False
            c1, c2 = g
            if out == "draw":
                return self.by_slug.get((prefix, d, c1 + c2, "draw"))
            mine = [mid for toks, mid, m in teams if m["game"] == g and own <= toks]
            if len(mine) == 1:
                return mine[0], 0
            other = kb if own == ka else ka                 # this team unnamed there: the other one's rival
            theirs = [m["out"] for toks, _, m in teams if m["game"] == g and other <= toks]
            if len(theirs) != 1:
                return False
            return self.by_slug.get((prefix, d, c1 + c2, c2 if theirs[0] == c1 else c1))
        return None

    def _soccer_name(self, prefix: str, days, market):
        name = tokens((market or {}).get("yes_sub_title") or "")
        if not name:
            return None
        for d in days:
            hits = [(mid, i) for toks, mid, i in self.by_name.get((prefix, d), []) if name <= toks]
            if hits:
                return hits[0] if len(hits) == 1 else None
        return None

    def _fight_fits(self, mid: str, codes: str) -> bool:
        if len(codes) != 6:
            return False
        outs = [tokens(o) for o in (self.markets.get(mid) or {}).get("outcomes", [])[:2]]
        if len(outs) != 2:
            return False
        fits = lambda toks, code: any(t.startswith(code) for t in toks)        # noqa: E731
        a, b = codes[:3], codes[3:]
        return (fits(outs[0], a) and fits(outs[1], b)) or (fits(outs[0], b) and fits(outs[1], a))

    def bout_changed(self, ticker: str, market: dict = None):
        """For a UFC leg, the Polymarket fight showing its bout changed: one of the two
        fighters Kalshi's rules name (by full name) against someone neither Kalshi's
        other fighter, however spelled, nor fitting either of Kalshi's codes -- as
        "Austin Bashi vs Carlos Calderon" -- or None (a day either side)."""
        if str(ticker).split("-")[0] != "KXUFCFIGHT":
            return None
        d0, sides, parts = kalshi_date(ticker), kalshi_sides(market), str(ticker).split("-")
        if d0 is None or not sides or len(parts) < 2:
            return None
        codes = parts[1][7:].lower()
        codes = (codes[:3], codes[3:]) if len(codes) == 6 else ()
        ks = [(tokens(s), _lastw(s)) for s in sides]
        with self.lock:
            for d in (d0, d0 + timedelta(days=1), d0 - timedelta(days=1)):
                for t0, t1, mid in self.by_pair.get(("ufc", d), ()):
                    for i, (k, _) in enumerate(ks):
                        other, other_last = ks[1 - i]
                        for here, there in ((t0, t1), (t1, t0)):
                            if len(k) < 2 or not _same(k, here, strict=False):
                                continue
                            if (_same(other, there, strict=False) or (other_last and other_last in there)
                                    or any(t.startswith(c) for t in there for c in codes)):
                                continue                    # the same bout, however spelled
                            outs = (self.markets.get(mid) or {}).get("outcomes") or []
                            return " vs ".join(str(o) for o in outs[:2]) or mid
        return None

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
