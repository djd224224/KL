#!/usr/bin/env python3
r"""combo_book_lines.py -- the sportsbook's vig-free line as a SECOND FAIR for
the combo shadow (Jack 2026-11-05: "Yes, add it", on "Add DraftKings'
vig-free line to the scorer as a second fair, to test which one settlements
bear out?").

ESPN's public scoreboard carries the book's moneylines for every game it lists
(DraftKings on 11/05). Game-winner legs only: the feed has one spread and one
total a game, while Kalshi's spread / total / prop legs are ladders of strikes.

  snapshot_loop   the logger's side thread: every SNAPSHOT_EVERY_S the lines of
                  today's and the next days' games -> odds_<UTC date>.jsonl,
                  a row only when a game's line is new or moved
  Lines           those snapshots (+ closing lines read at scoring time),
                  matched to a Kalshi game-winner leg: the game by both team
                  codes in the event ticker and the start (Kalshi expires game
                  markets 3 h after it), the side by the leg's team code
  calibration     per leg use in executed parlays: Kalshi's mid at the RFQ vs
                  the book's vig-free probability (the snapshot at or before
                  the RFQ; the closing line when no snapshot is older, which
                  KNOWS MORE and is labelled so) vs what happened

Read-only: public ESPN reads, no Kalshi call.
"""
import collections
import gzip
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

ESPN = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
SNAPSHOT_EVERY_S = float(os.environ.get("COMBO_ODDS_EVERY_S", "600"))
# Kalshi game-winner series -> (ESPN path, ESPN groups, ET days to read from today)
LEAGUES = {"KXNFLGAME": ("football/nfl", None, 5),
           "KXNBAGAME": ("basketball/nba", None, 2),
           "KXNHLGAME": ("hockey/nhl", None, 2),
           "KXNCAAMBGAME": ("basketball/mens-college-basketball", 50, 2),
           "KXNCAAWBGAME": ("basketball/womens-college-basketball", 50, 2),
           "KXNCAAFGAME": ("football/college-football", 80, 3)}
GAME_HOURS = 3.0          # Kalshi game markets expire this long after the start
START_SLACK_S = 900.0


def american(v):
    """A moneyline ('+139', '-165', 'EVEN', 139.0) -> float, or None."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("", "OFF", "N/A", "NONE"):
        return None
    if s == "EVEN":
        return 100.0
    try:
        x = float(s.replace("+", ""))
    except ValueError:
        return None
    return x if abs(x) >= 100 else None


def implied(ml: float) -> float:
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def no_vig(ml_home: float, ml_away: float) -> float:
    """The home team's probability with the book's margin taken out."""
    h, a = implied(ml_home), implied(ml_away)
    return h / (h + a)


def moneyline(odds: dict, side: str):
    """ESPN's two shapes: homeTeamOdds.moneyLine, or moneyline.home.{current,
    close, open}.odds."""
    v = american((odds.get(f"{side}TeamOdds") or {}).get("moneyLine"))
    if v is not None:
        return v
    m = (odds.get("moneyline") or {}).get(side) or {}
    for k in ("current", "close", "open"):
        d = m.get(k) or {}
        v = american(d.get("odds") if d.get("odds") is not None else d.get("american"))
        if v is not None:
            return v
    return None


def odds_rows(js: dict, series: str, t: float) -> list:
    """An ESPN scoreboard -> one row a game that carries both moneylines."""
    out = []
    for ev in js.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        odds = (comp.get("odds") or [None])[0]
        if not odds:
            continue
        teams = {x.get("homeAway"): (x.get("team") or {}).get("abbreviation", "")
                 for x in comp.get("competitors") or []}
        h, a = teams.get("home"), teams.get("away")
        mh, ma = moneyline(odds, "home"), moneyline(odds, "away")
        if not (h and a and mh and ma):
            continue
        out.append({"k": "odds", "t": round(t, 1), "lg": series, "eid": str(ev.get("id")),
                    "start": ev.get("date"), "home": h, "away": a, "hml": mh, "aml": ma,
                    "p_home": round(no_vig(mh, ma), 5),
                    "prov": (odds.get("provider") or {}).get("name"),
                    "state": ((comp.get("status") or {}).get("type") or {}).get("state")})
    return out


def _http_get(url, params):
    import requests
    return requests.get(url, params=params, timeout=15).json()


def et_date(ts: float) -> date:
    try:
        import pytz
        return datetime.fromtimestamp(ts, pytz.timezone("US/Eastern")).date()
    except ImportError:
        return datetime.fromtimestamp(ts - 5 * 3600, timezone.utc).date()


def fetch_day(series: str, day: str, get=_http_get, t: float = None) -> list:
    path, groups, _ahead = LEAGUES[series]
    params = {"dates": day, "limit": 400}
    if groups:
        params["groups"] = groups
    return odds_rows(get(ESPN.format(path=path), params), series, t if t is not None else time.time())


def snapshot_pass(writer, last: dict, now: float, get=_http_get, log=print) -> int:
    """One pass over every league and day: rows written for new or moved lines."""
    n = 0
    for ser, (_path, _groups, ahead) in LEAGUES.items():
        for d in range(ahead):
            day = (et_date(now) + timedelta(days=d)).strftime("%Y%m%d")
            try:
                rows = fetch_day(ser, day, get, time.time())
            except Exception as e:
                log(f"! odds {ser} {day}: {type(e).__name__}: {str(e)[:120]}")
                continue
            for r in rows:
                key, line = (r["lg"], r["eid"]), (r["hml"], r["aml"])
                if last.get(key) == line:
                    continue
                last[key] = line
                writer.write(r)
                n += 1
    writer.flush()
    return n


def snapshot_loop(out_dir: str, stop_evt, every: float = SNAPSHOT_EVERY_S, get=_http_get, log=print):
    """The logger's side thread (never touches the WebSocket)."""
    from combo_rfq_logger import DayWriter
    w = DayWriter(out_dir, prefix="odds")
    last = {}
    try:
        while not stop_evt.is_set():
            t0 = time.time()
            try:
                n = snapshot_pass(w, last, t0, get, log)
                log(f"odds: {n} new or moved lines")
            except Exception as e:                     # never kills the logger
                log(f"! odds pass: {type(e).__name__}: {str(e)[:160]}")
            stop_evt.wait(max(5.0, every - (time.time() - t0)))
    finally:
        w.close()


# ---- reading them back -------------------------------------------------------

def load(dir_: str, since: date, until: date) -> list:
    """The logger's odds rows, plain or gzip'd, from the day before `since`
    (yesterday's lines price today's early RFQs) to `until`."""
    out = []
    d = since - timedelta(days=1)
    while d <= until:
        for name in (f"odds_{d.isoformat()}.jsonl", f"odds_{d.isoformat()}.jsonl.gz"):
            p = os.path.join(dir_, name)
            if not os.path.exists(p):
                continue
            opener = gzip.open if name.endswith(".gz") else open
            try:
                with opener(p, "rt", encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("k") == "odds":
                            out.append(r)
            except (OSError, EOFError):
                pass
        d += timedelta(days=1)
    return out


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Lines:
    """Sportsbook lines by game: logged snapshots (a time series) and closing
    lines (read after the fact)."""

    def __init__(self, rows=(), close_rows=()):
        self.snaps = collections.defaultdict(list)     # (lg, eid) -> [(t, p_home)]
        self.close = {}
        self.meta = {}                                  # (lg, eid) -> (start, home, away)
        for r in rows:
            self._meta(r)
            self.snaps[(r["lg"], r["eid"])].append((float(r["t"]), float(r["p_home"])))
        for v in self.snaps.values():
            v.sort()
        for r in close_rows:
            self._meta(r)
            self.close[(r["lg"], r["eid"])] = float(r["p_home"])

    def _meta(self, r):
        st = _ts(r.get("start"))
        if st is not None:
            self.meta[(r["lg"], str(r["eid"]))] = (st, r["home"], r["away"])

    def match(self, ticker: str, market: dict):
        """(game key, the leg's team is home) for a Kalshi game-winner leg, or None."""
        series = ticker.split("-")[0]
        if series not in LEAGUES:
            return None
        ev = (market or {}).get("event_ticker") or "-".join(ticker.split("-")[:2])
        suffix = ev.split("-", 1)[1][7:] if "-" in ev else ""
        team = ticker.rsplit("-", 1)[1]
        exp = _ts((market or {}).get("expected_expiration_time"))
        hits = []
        for key, (start, h, a) in self.meta.items():
            if key[0] != series or not (h in suffix and a in suffix):
                continue
            if exp is not None and abs(exp - GAME_HOURS * 3600 - start) > START_SLACK_S:
                continue
            is_h, is_a = team.startswith(h), team.startswith(a)
            if is_h != is_a:
                hits.append((key, is_h))
        return hits[0] if len(hits) == 1 else None

    def p_home(self, key, t: float):
        """(p, "snapshot" | "later") at time t, or None: the last logged line
        at or before t, else the line read at scoring time (the close for a
        finished game), which knows more than the RFQ did."""
        v = self.snaps.get(key) or []
        best = None
        for ts, p in v:
            if ts <= t:
                best = p
            else:
                break
        if best is not None:
            return best, "snapshot"
        if key in self.close:
            return self.close[key], "later"
        return None


def close_rows(needed, get=_http_get, log=print) -> list:
    """Closing lines for {(series, YYYYMMDD)} read now (after the games)."""
    out = []
    for ser, day in sorted(needed):
        try:
            out += fetch_day(ser, day, get)
        except Exception as e:
            log(f"! closing lines {ser} {day}: {type(e).__name__}: {str(e)[:120]}")
    return out


MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                      "OCT", "NOV", "DEC"), 1)}


def day_of_event(ev: str):
    """KXNBAGAME-26NOV05MIAGSW -> '20261105' (None if the date does not parse)."""
    try:
        s = ev.split("-", 1)[1][:7]
        return f"20{s[:2]}{MONTHS[s[2:5]]:02d}{s[5:7]}"
    except (IndexError, KeyError):
        return None


def calibration(execs: list, markets: dict, book, lines: Lines, sport_of=None) -> list:
    """One row per game-winner leg use in the executed parlays: Kalshi's mid
    on the side picked (the last candle before the RFQ), the book's vig-free
    probability of that side, its source, and the leg's result (1 / 0 /
    None while open)."""
    rows = []
    for e in execs:
        t = e["rfq_ms"] / 1000.0
        for leg in e["legs"]:
            tk, side = leg[0], leg[1]
            m = markets.get(tk) or {}
            mt = lines.match(tk, m)
            if mt is None:
                continue
            key, is_home = mt
            got = lines.p_home(key, t)
            q = book.price_at(tk, t)
            if got is None or q is None or q[0] is None or q[1] is None or not (0 < q[0] < q[1] < 1):
                continue
            p_team = got[0] if is_home else 1.0 - got[0]
            mid = (q[0] + q[1]) / 2.0
            res = str(m.get("result") or "").lower()
            hit = None if res not in ("yes", "no") else float((res == "yes") == (side == "yes"))
            rows.append({"sport": tk.split("-")[0][2:-4], "src": got[1],
                         "p_kalshi": mid if side == "yes" else 1.0 - mid,
                         "p_book": p_team if side == "yes" else 1.0 - p_team, "hit": hit,
                         "tid": e.get("tid"), "leg": tk})
    return rows


def summarize(rows: list) -> dict:
    """Mean Kalshi vs book probability of the sides picked, the hit rate and
    each one's Brier score (lower = closer to what happened)."""
    def block(rs):
        done = [r for r in rs if r["hit"] is not None]
        out = {"n": len(rs), "settled": len(done),
               "p_kalshi": sum(r["p_kalshi"] for r in rs) / len(rs) if rs else None,
               "p_book": sum(r["p_book"] for r in rs) / len(rs) if rs else None,
               "kalshi_higher_2pt": sum(1 for r in rs if r["p_kalshi"] - r["p_book"] > 0.02) / len(rs) if rs else None,
               "book_higher_2pt": sum(1 for r in rs if r["p_book"] - r["p_kalshi"] > 0.02) / len(rs) if rs else None}
        if done:
            out.update(hit=sum(r["hit"] for r in done) / len(done),
                       brier_kalshi=sum((r["p_kalshi"] - r["hit"]) ** 2 for r in done) / len(done),
                       brier_book=sum((r["p_book"] - r["hit"]) ** 2 for r in done) / len(done),
                       p_kalshi_settled=sum(r["p_kalshi"] for r in done) / len(done),
                       p_book_settled=sum(r["p_book"] for r in done) / len(done))
        return out
    by = collections.defaultdict(list)
    for r in rows:
        by[r["sport"]].append(r)
    return {"all": block(rows), "snapshot": block([r for r in rows if r["src"] == "snapshot"]),
            "later": block([r for r in rows if r["src"] == "later"]),
            "by_sport": {k: block(v) for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))}}


def render(s: dict) -> list:
    """The report's section (markdown lines)."""
    a = s.get("all") or {}
    L = ["## Kalshi mid vs the sportsbook (DraftKings via ESPN, vig removed) as the fair", ""]
    if not a.get("n"):
        return L + ["No game-winner leg matched a sportsbook line yet.", ""]
    L.append(f"{a['n']:,} game-winner leg uses in executed parlays matched a book line "
             f"({(s.get('snapshot') or {}).get('n', 0):,} at the RFQ's time, "
             f"{(s.get('later') or {}).get('n', 0):,} only from a later read, which knows more). "
             f"On the sides bettors picked, Kalshi's mid averaged {a['p_kalshi']:.1%} and the book "
             f"{a['p_book']:.1%}; Kalshi was 2+ pts higher on {a['kalshi_higher_2pt']:.0%}, lower on "
             f"{a['book_higher_2pt']:.0%}.")
    L.append("")
    L += ["| legs | uses | settled | Kalshi mid | book | hit rate | Brier Kalshi | Brier book |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]

    def row(name, b):
        if not b.get("n"):
            return
        if b.get("settled"):
            L.append(f"| {name} | {b['n']:,} | {b['settled']:,} | {b['p_kalshi_settled']:.1%} | "
                     f"{b['p_book_settled']:.1%} | {b['hit']:.1%} | {b['brier_kalshi']:.4f} | {b['brier_book']:.4f} |")
        else:
            L.append(f"| {name} | {b['n']:,} | 0 | {b['p_kalshi']:.1%} | {b['p_book']:.1%} | | | |")
    row("all", a)
    row("book line at the RFQ", s.get("snapshot") or {})
    row("later line only", s.get("later") or {})
    for k, b in (s.get("by_sport") or {}).items():
        row(k, b)
    L += ["", "Brier = mean squared gap to the result: the lower one is the better fair. Read it on "
          "the at-the-RFQ rows; a later line has seen more news.", ""]
    return L
