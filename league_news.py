#!/usr/bin/env python3
"""ESPN injury-report news for the league auto families' GAME members (Jack
2026-10-22: "Build it", to "Should I build an NBA team-news hold like NFL's?").

The NFL auto family's game members stand aside for NFL_NEWS_HOLD_MIN after
team news (incentive_mm.nfl_team_news_gate, on nfl_prop_fair's ESPN roster
designations). The league auto families had only their pre-start cutoff
(LEAGUE_AUTO_GAME_BUFFER_MIN, 90 min) -- and an NBA star ruled out three
hours before tip moves every prop of his game. This module reads ESPN's
league-wide injury report (one call: every team with an injury, each entry's
status and the time ESPN last updated it) and keeps, per team:
  news_at  the latest of (a) an entry's update time and (b) the moment a
           read first saw a change against the previous read -- a player
           added, his status changed, or dropped from the report (cleared);
  news     what it was ("Coby White Day-To-Day", "... cleared").
incentive_mm.league_news_gate holds a game member while either team's
news_at is inside LEAGUE_NEWS_HOLD_MIN, failing CLOSED on a missing or stale
read and on a ticker whose two teams it cannot name.

Team names: Kalshi's game segment is "<date><AWAY><HOME>" in its own codes
(GSW, NOP, NYK, SAS, UTA, WAS), ESPN's are GS, NO, NY, SA, UTAH, WSH. A code
resolves to a team by its exact abbreviation, else uniquely by prefix of the
abbreviation (either way) or of the location ("WAS" -> Washington); the
3 + 3 split is tried first.
"""
import re
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
PATHS = {"NBA": "basketball/nba"}
UA = {"User-Agent": "KL-IMM-league-news/1.0"}
HTTP_TIMEOUT = 20
_SEG_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}([A-Z]{4,10})$")


def espn_ts(s) -> Optional[float]:
    """'2026-10-22T14:05Z' -> epoch seconds (None when unreadable)."""
    try:
        return datetime.strptime(str(s)[:16], "%Y-%m-%dT%H:%M").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _get(url: str, get: Optional[Callable] = None) -> dict:
    if get is not None:
        return get(url) or {}
    r = requests.get(url, headers=UA, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json() or {}


def fetch_teams(league: str, get: Optional[Callable] = None) -> List[dict]:
    """[{id, abbreviation, location, name}] for the league from ESPN."""
    js = _get(f"{ESPN}/{PATHS[league]}/teams", get)
    out = []
    for sp in js.get("sports") or []:
        for lg in sp.get("leagues") or []:
            for x in lg.get("teams") or []:
                t = x.get("team") or {}
                if t.get("abbreviation"):
                    out.append({"id": str(t.get("id") or ""),
                                "abbreviation": str(t["abbreviation"]).upper(),
                                "location": str(t.get("location") or "").upper(),
                                "name": str(t.get("displayName") or "")})
    return out


def fetch_injuries(league: str, get: Optional[Callable] = None) -> dict:
    """ESPN's league-wide injury report (raw JSON)."""
    return _get(f"{ESPN}/{PATHS[league]}/injuries", get)


def build_snapshot(league: str, injuries: dict, teams: List[dict],
                   prev: Optional[dict] = None, now_ts: Optional[float] = None) -> dict:
    """{"ts", "league", "teams": {ABBR: {"id", "location", "name", "news_at",
    "news", "players": {name: {"status", "date"}}}}, "changes": [...]} -- every
    team of `teams`, those without an injury with an empty report. `prev` (the
    last snapshot) turns differences into news at `now_ts`; a team's news_at
    never moves back."""
    now_ts = time.time() if now_ts is None else now_ts
    by_id = {t["id"]: t for t in teams}
    by_abbr = {t["abbreviation"]: t for t in teams}
    players: Dict[str, Dict[str, dict]] = {a: {} for a in by_abbr}
    for blk in injuries.get("injuries") or []:
        tm = by_id.get(str(blk.get("id") or ""))
        for inj in blk.get("injuries") or []:
            a = inj.get("athlete") or {}
            abbr = (tm or {}).get("abbreviation") or str(
                (a.get("team") or {}).get("abbreviation") or "").upper()
            name = str(a.get("displayName") or "").strip()
            if abbr not in players or not name:
                continue
            players[abbr][name] = {"status": str(inj.get("status") or "").strip(),
                                   "date": espn_ts(inj.get("date"))}
    old_teams = (prev or {}).get("teams") or {}
    out, changes = {}, []
    for abbr, t in by_abbr.items():
        cur = players[abbr]
        news_at, news = None, None
        for name, p in cur.items():
            if p["date"] is not None and (news_at is None or p["date"] > news_at):
                news_at, news = p["date"], f"{name} {p['status'] or 'listed'}"
        old = old_teams.get(abbr)
        if old is not None:
            was = old.get("players") or {}
            seen = []
            for name, p in cur.items():
                if name not in was or was[name].get("status") != p["status"]:
                    seen.append(f"{name} {p['status'] or 'listed'}")
            seen += [f"{name} cleared" for name in was if name not in cur]
            if seen:
                changes += [f"{abbr} {s}" for s in seen]
                if news_at is None or now_ts > news_at:
                    news_at, news = now_ts, "; ".join(seen)
            # never back: an earlier, later-stamped item still stands
            if old.get("news_at") is not None and (
                    news_at is None or float(old["news_at"]) > news_at):
                news_at, news = float(old["news_at"]), old.get("news")
        out[abbr] = {"id": t["id"], "location": t["location"], "name": t["name"],
                     "news_at": news_at, "news": news, "players": cur}
    return {"ts": now_ts, "league": league, "teams": out, "changes": changes}


def _resolve(code: str, teams: Dict[str, dict]) -> Optional[str]:
    """A Kalshi team code -> the ESPN abbreviation it names, None unless
    exactly one team fits (exact abbreviation first)."""
    if code in teams:
        return code
    hits = [a for a, t in teams.items()
            if a.startswith(code) or code.startswith(a)
            or (t.get("location") or "").replace(" ", "").startswith(code)]
    return hits[0] if len(hits) == 1 else None


def team_pair(snapshot: dict, ticker: str) -> Optional[Tuple[str, str]]:
    """The two ESPN teams of a '<date><TEAMS>' game event ('KXNBAPTS-
    26OCT23CHIMIL-...' -> ('CHI', 'MIL')), None when they cannot be named."""
    seg = ticker.split("-")[1] if "-" in ticker else ""
    m = _SEG_RE.match(seg)
    teams = (snapshot or {}).get("teams") or {}
    if not m or not teams:
        return None
    blob = m.group(1)
    splits = [3] + [i for i in range(2, len(blob) - 1) if i != 3]
    for i in splits:
        x, y = _resolve(blob[:i], teams), _resolve(blob[i:], teams)
        if x and y and x != y:
            return x, y
    return None
