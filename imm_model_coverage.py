#!/usr/bin/env python3
"""IMM model coverage: is every paying market in a per-entity-modeled family
actually modeled? (Jack 2026-10-09: "when new markets are added onto an event
that is modeled separately like cities in rain, they should automatically be
modeled generally....ensure i get alerted if this is not the case".)

Several IMM families price each market through a per-entity table: a rain
city's NWS station, a Carbon Arc series' Prism entity, a monthly rain / snow
city's climate station, a Pokemon product, an NFL player, ... When Kalshi
adds a market whose entity the table lacks, the family's gate either
quotes it WITHOUT the model's protection (fail-OPEN: daily rain, Carbon Arc)
or stands it aside for good (fail-CLOSED: monthly rain / snow, Pokemon,
NFL props, ...). On 10/09 thirteen rain cities had no station
(CMH/TAM/ABQ/LEX were -$196 that day) and three Carbon Arc foot-traffic
series had never been gated; nothing said so.

Where an entity can be resolved from the market itself it now is (daily
rain: rain_fair reads each new city's CLI station from its rules). Every
family below is CHECKED on each IMM family-watch run (every 30 min): a gap is
emailed on first sight and re-sent every REALERT_HOURS while it stays open
(imm_family_watch pass 5). Each check reads the bot's own membership test
and the fair file its refresher writes, so "covered" means what the live
gate means. A check that errors is itself reported (an unreadable fair file
is a coverage gap of its own).

  python imm_model_coverage.py           # print the current gaps (read-only)
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

# imm_quote_gaps FIRST: it mirrors the launcher's env before incentive_mm reads
# its config, so the membership tests below mean what the live bot means
import imm_quote_gaps  # noqa: F401
import incentive_mm as imm

REALERT_HOURS = float(os.environ.get("IMM_MODEL_GAP_REALERT_HOURS", "24"))
# a fair file older than this is a gap of its own: its refresher has stopped,
# so nothing it would add (or alert on) is happening
FAIR_STALE_HOURS = float(os.environ.get("IMM_MODEL_FAIR_STALE_HOURS", "6"))


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f) or {}


def _series(t: str) -> str:
    return t.split("-")[0]


def _gap(family: str, entity: str, markets: Iterable[str], fail: str,
         why: str, fix: str) -> dict:
    mk = sorted(set(markets))
    return {"key": f"{family}|{entity}", "family": family, "entity": entity,
            "markets": mk, "fail": fail, "why": why, "fix": fix}


def _age_hours(iso_or_ts, now_ts: float) -> Optional[float]:
    if iso_or_ts is None:
        return None
    try:
        ts = float(iso_or_ts)
    except (TypeError, ValueError):
        dt = imm.parse_iso_utc(str(iso_or_ts))
        if dt is None:
            return None
        ts = dt.timestamp()
    return (now_ts - ts) / 3600.0


def _stale_gap(family: str, path: str, data: dict, key: str, now_ts: float,
               markets: Iterable[str]) -> List[dict]:
    age = _age_hours(data.get(key), now_ts)
    if age is None or age > FAIR_STALE_HOURS:
        return [_gap(family, "(fair file)", markets, "open",
                     f"{os.path.basename(path)} last written "
                     + (f"{age:.1f}h ago" if age is not None else "never"),
                     "the bot's refresher thread for this family has stopped; "
                     "check the bot log for its refresh lines, restart if needed")]
    return []


# ---------------------------------------------------------------- checks ---
# Each check: (progs: {market ticker: program}, now_ts) -> [gap, ...].
# progs = the markets in an active reward program (what the bot can quote).

def check_rain_daily(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """KXRAIN city binaries: a city needs a station in rain_fair.STATIONS or
    an auto-resolved row in the fair file (FAIL-OPEN: the NWS gate and the
    directional take don't apply to an unpriced city on events 2+ days out;
    the next-day event is exempt from the gate by design either way)."""
    import rain_fair
    mk = [t for t in progs if t.split("-")[0] == imm.RAIN_FAIR_SERIES
          and len(t.split("-")) == 3]
    if not mk:
        return []
    data = _read(imm.RAIN_FAIR_FILE)
    out = _stale_gap("Daily rain (rain_fair)", imm.RAIN_FAIR_FILE, data,
                     "generated_at", now_ts, mk)
    covered = set(rain_fair.STATIONS) | set(data.get("auto_stations") or {})
    unmapped = data.get("unmapped") or {}
    by_city = defaultdict(list)
    for t in mk:
        by_city[t.split("-")[2]].append(t)
    for city, ts in sorted(by_city.items()):
        if city not in covered:
            out.append(_gap(
                "Daily rain (rain_fair)", city, ts, "open",
                unmapped.get(city) or "no station row and not auto-resolved yet",
                "rain_fair resolves a new city from its rules' CLI code within "
                "6h; if it can't, add a STATIONS row (NWS station record)"))
    return out


def check_carbon_arc(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """Carbon Arc-settled series (carbon_arc_settled, the gate's own test) need a Prism
    entity in the fair file's series_map (FAIL-OPEN: without one the CA
    gate never reads the series, so it quotes on the book alone)."""
    by_ser = defaultdict(list)
    for t in progs:
        s = _series(t)
        if imm.carbon_arc_settled(s):
            by_ser[s].append(t)
    if not by_ser:
        return []
    data = _read(imm.CA_FAIR_FILE)
    out = _stale_gap("Carbon Arc (carbon_arc_fair)", imm.CA_FAIR_FILE, data,
                     "generated_at", now_ts, [t for ts in by_ser.values() for t in ts])
    mapped = set(data.get("series_map") or {})
    for s, ts in sorted(by_ser.items()):
        if s not in mapped:
            out.append(_gap(
                "Carbon Arc (carbon_arc_fair)", s, ts, "open",
                "no Prism entity in carbon_arc_fair's series map (its settlement "
                "link did not resolve to a Carbon Arc entity)",
                "map the series to its Carbon Arc entity in carbon_arc_fair"))
    return out


def _weather_events(family: str, path: str, member: Callable[[str], bool],
                    progs: Dict[str, dict], now_ts: float, fix: str) -> List[dict]:
    """Monthly / period weather books priced per event from a CLI station
    (rain_monthly_fair, snow_monthly_fair): FAIL-CLOSED -- the gate stands a
    market aside without a fair. An event is a gap when the writer marks it
    "no station" (the rules' CLI code is not in its station table) or has
    never read it at all."""
    by_ev = defaultdict(list)
    for t in progs:
        if member(t):
            by_ev[imm.event_ticker_of(t)].append(t)
    if not by_ev:
        return []
    data = _read(path)
    out = _stale_gap(family, path, data, "generated_at", now_ts,
                     [t for ts in by_ev.values() for t in ts])
    state = data.get("event_state") or {}
    known = set(data.get("events") or {}) | set(state)
    for ev, ts in sorted(by_ev.items()):
        st = state.get(ev) or {}
        if ev not in known:
            out.append(_gap(family, ev, ts, "closed",
                            "the fair writer has never read this event", fix))
        elif st.get("error") == "no station":
            out.append(_gap(family, ev, ts, "closed",
                            f"no station for CLI code {st.get('code')!r} in the "
                            f"writer's station table", fix))
    return out


def check_rain_monthly(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """Monthly city rain (KXRAIN<CITY>M) and the period totals (KXRAINNAPAM,
    any KXRAIN two-date shape) -- rain_monthly_fair."""
    return _weather_events(
        "Monthly / period rain (rain_monthly_fair)", imm.RAIN_MONTHLY_FILE,
        lambda t: imm.rain_monthly_series(_series(t)) or imm.rain_period_gated(t),
        progs, now_ts,
        "add the CLI code to rain_monthly_fair.CLI_STATIONS (IEM network, "
        "ACIS id, coordinates, zone; check all three feeds) and restart")


def check_snow_monthly(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """Monthly city snow (KX<CITY>SNOWM) -- snow_monthly_fair."""
    return _weather_events(
        "Monthly snow (snow_monthly_fair)", imm.SNOW_MONTHLY_FILE,
        lambda t: imm.snow_monthly_series(_series(t)), progs, now_ts,
        "add the city to snow_monthly_fair CITY_CODES + SNOW_STATIONS and "
        "restart (restart_imm.ps1)")


def _per_market(family: str, path: str, member: Callable[[str], bool],
                progs: Dict[str, dict], now_ts: float, stamp: str,
                group: Callable[[str, dict], str], fix: str,
                extra_unmapped: Iterable[str] = ()) -> List[dict]:
    """Books priced per market from a fair file's "markets" map (Pokemon,
    NFL props): FAIL-CLOSED -- the gate stands a market aside when it is
    missing from the read or carries an "err". Grouped by `group` (the
    entity: an item, a player) so one new player is one alert line."""
    mk = [t for t in progs if member(t)]
    if not mk:
        return []
    data = _read(path)
    out = _stale_gap(family, path, data, stamp, now_ts, mk)
    entries = data.get("markets") or {}
    unmapped_ev = set(extra_unmapped)
    groups: Dict[tuple, List[str]] = defaultdict(list)
    for t in mk:
        e = entries.get(t)
        if imm.event_ticker_of(t) in unmapped_ev:
            why = "event not mapped"
        elif e is None:
            why = "market not in the read"
        elif e.get("err"):
            why = str(e["err"])[:120]
        else:
            continue
        groups[(group(t, e or {}), why)].append(t)
    for (entity, why), ts in sorted(groups.items()):
        # digits out of the key, so "only 2 games" -> "only 3 games" next
        # week stays the same open gap instead of a new one
        out.append(_gap(family, f"{entity}: {re.sub(r'[0-9]+', 'N', why)}",
                        ts, "closed", why, fix))
    return out


def check_pokemon(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """KXPOKEMON items -- pokemon_fair (an event needs its TCGplayer product
    in pokemon_products.json, mapped by hand each month)."""
    path = os.path.join(imm.STATUS_DIR, "pokemon_fair.json")
    try:
        unmapped = _read(path).get("unmapped") or []
    except (OSError, ValueError):
        unmapped = []
    return _per_market(
        "Pokemon (pokemon_fair)", path, lambda t: imm.poke_series(_series(t)),
        progs, now_ts, "fetched_at",
        lambda t, e: imm.event_ticker_of(t),
        "map the event in pokemon_products.json (python pokemon_fair.py "
        "--candidates lists TCGplayer candidates)", unmapped)


def check_nfl_props(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """NFL ladders / escalators -- nfl_prop_fair's player-history fair (a
    player it can't read or match, too little history, or a ladder series
    its stat table lacks)."""
    return _per_market(
        "NFL props (nfl_prop_fair)", os.path.join(imm.STATUS_DIR, "nfl_prop_fair.json"),
        lambda t: imm.nfl_series(_series(t)), progs, now_ts, "fetched_at",
        lambda t, e: f"{_series(t)} {e.get('player') or t.rsplit('-', 1)[-1]}",
        "nfl_prop_fair: add the series to SERIES / fix the player match")


def check_treasury(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """Treasury yields gated on CNBC's live yield (treasury_fair.classify /
    strike_of): FAIL-CLOSED -- an unmodelled series or ticker shape stands
    aside."""
    import treasury_fair as tf
    groups: Dict[tuple, List[str]] = defaultdict(list)
    for t in progs:
        s = _series(t)
        if not imm.treasury_gated_series(s):
            continue
        if tf.classify(s) is None:
            groups[(s, "no Treasury fair model for this series")].append(t)
        elif tf.strike_of(t) is None:
            groups[(s, "ticker shape not parsed (strike)")].append(t)
    return [_gap("Treasury yields (treasury_fair)", f"{s}: {why}", ts, "closed", why,
                 "teach treasury_fair.classify / strike_of the new shape")
            for (s, why), ts in sorted(groups.items())]


def check_vercel(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """Vercel AI-gateway labs (vercel_fair.LAB_SERIES + the open-weights
    share): FAIL-CLOSED -- an enrolled series the model has no lab for, or
    an event it can't date, never gets a read."""
    import vercel_fair as vf
    groups: Dict[tuple, List[str]] = defaultdict(list)
    for t in progs:
        s = _series(t)
        if not imm.vercel_series(s):
            continue
        if s not in vf.ALL_SERIES:
            groups[(s, "no lab for this series in vercel_fair.LAB_SERIES")].append(t)
        elif imm.vercel_measured_day(imm.event_ticker_of(t)) is None:
            groups[(s, "event ticker not parsed (measured day)")].append(t)
    return [_gap("Vercel labs (vercel_fair)", f"{s}: {why}", ts, "closed", why,
                 "add the lab to vercel_fair.LAB_SERIES / teach the parser the shape")
            for (s, why), ts in sorted(groups.items())]


def check_datacenter(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """State data-center counts (datacenter_fair): every state with a program
    is read automatically; a state with paying markets but no count in the
    file is FAIL-CLOSED (the gate stands it aside)."""
    by_state = defaultdict(list)
    for t in progs:
        st = imm.dc_state_of(_series(t))
        if st:
            by_state[st].append(t)
    if not by_state:
        return []
    data = _read(imm.DC_FAIR_FILE)
    out = _stale_gap("Data-center counts (datacenter_fair)", imm.DC_FAIR_FILE, data,
                     "updated_at", now_ts, [t for ts in by_state.values() for t in ts])
    have = set(data.get("states") or {})
    for st, ts in sorted(by_state.items()):
        if st not in have:
            out.append(_gap("Data-center counts (datacenter_fair)", st, ts, "closed",
                            "no count read for this state",
                            "check the datacenter refresher's source for the state"))
    return out


def check_youtube_weekly(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """KXYTVIEWSW: a pilot quoted per artist (YTW_ARTISTS, each admitted on
    evidence) against the realtime nowcast. Another artist's event gets no
    fair BY DESIGN -- reported once (not daily) so Jack can decide."""
    groups = defaultdict(list)
    for t in progs:
        if imm.ytw_series(_series(t)):
            code = imm.ytw_event_code(imm.event_ticker_of(t))
            if code not in imm.YTW_ARTISTS:
                groups[code or "?"].append(t)
    out = []
    for code, ts in sorted(groups.items()):
        g = _gap("YouTube weekly pilot (yt_weekly_fair)", code, ts, "closed",
                 f"artist {code} is not in the pilot (YTW_ARTISTS), so no nowcast",
                 "admit the artist (IMM_YTW_PILOT_ARTISTS) if its tape supports it")
        g["once"] = True
        out.append(g)
    return out


# sports ladders / escalators: only the NFL has a player fair (nfl_prop_fair);
# the pattern allowlist admits every league's ladders
MODELED_LADDER_LEAGUES = frozenset({"NFL"})
_ALLOW_BOT = None


def _allowed(ticker: str) -> bool:
    """The live bot's allow test (blocklist, allowlists, extra-allow file),
    built once per process and only when a check needs it -- the feed
    audit's own recipe."""
    global _ALLOW_BOT
    if _ALLOW_BOT is None:
        imm.load_extra_allow_series()
        _ALLOW_BOT = imm.IncentiveMarketMaker(client=None, live=False)
    return _ALLOW_BOT._allowed(ticker)


def check_sports_ladders(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """Ladders / escalators of a league with no player fair (NBA, NHL, MLB
    ...): FAIL-OPEN -- the bot quotes them on the book alone."""
    groups = defaultdict(list)
    for t in progs:
        s = _series(t)
        lg = imm.sports_ladder_league(s)
        if lg and lg not in MODELED_LADDER_LEAGUES:
            groups[(lg, s)].append(t)
    out = []
    for (lg, s), ts in sorted(groups.items()):
        if not any(_allowed(t) for t in ts[:3]):
            continue                 # not quoted (a new family is the watch's
                                     # not-allowed alert, pass 4)
        out.append(_gap("Sports ladders without a model", f"{lg} {s}", ts, "open",
                        f"{lg} ladders / escalators quote with no player fair "
                        f"(only the NFL has one: nfl_prop_fair)",
                        "build a fair for the league, or block the series"))
    return out


def check_vercel_labs(progs: Dict[str, dict], now_ts: float) -> List[dict]:
    """KXOPENSOURCESHARE sums a fixed list of open-weight labs
    (vercel_fair.OPEN_WEIGHT_LABS): a lab that is new to Vercel's export is
    left out of the sum silently. Each one is reported once (on the pass's
    first run the labs already there are recorded without an email)."""
    import glob
    import vercel_fair as vf
    if not any(_series(t) == vf.OPEN_SERIES for t in progs):
        return []
    files = sorted(glob.glob(os.path.join(vf.INTRADAY_DIR, "export_*.jsonl")))
    if not files:
        return [_gap("Vercel open-weight labs (vercel_fair)", "(export files)", [],
                     "open", f"no vercel-logger export in {vf.INTRADAY_DIR}",
                     "check the vercel-logger task")]
    last = None
    with open(files[-1], encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                last = line
    labs_by_day = (json.loads(last).get("labs") or {}) if last else {}
    day = max(labs_by_day) if labs_by_day else None
    shares = defaultdict(float)                      # token share, the open sum's metric
    for k, v in (labs_by_day.get(day) or {}).items():
        lab, _, metric = k.partition("|")
        shares[lab] += float(v or 0) if metric == "tokens" else 0.0
    known = set(vf.OPEN_WEIGHT_LABS) | {lab for lab, _m in vf.LAB_SERIES.values()}
    out = []
    for lab, sh in sorted(shares.items()):
        if lab in known:
            continue
        g = _gap("Vercel open-weight labs (vercel_fair)", lab,
                 [t for t in progs if _series(t) == vf.OPEN_SERIES][:3], "open",
                 f"lab {lab!r} is in Vercel's export ({sh:.2f}% of tokens on {day}) but not in "
                 f"OPEN_WEIGHT_LABS", "if it ships only open-weight models, add it "
                 "to vercel_fair.OPEN_WEIGHT_LABS (KXOPENSOURCESHARE's sum)")
        g["once"] = True
        out.append(g)
    return out



CHECKS: List[Callable[[Dict[str, dict], float], List[dict]]] = [
    check_rain_daily, check_carbon_arc, check_rain_monthly, check_snow_monthly,
    check_pokemon, check_nfl_props, check_treasury, check_vercel,
    check_datacenter, check_youtube_weekly, check_sports_ladders, check_vercel_labs]


def find_gaps(progs: Dict[str, dict], now_ts: Optional[float] = None,
              checks: Optional[List[Callable]] = None) -> Dict[str, dict]:
    """{gap key: gap} over every check; a check that raises is a gap too."""
    now_ts = time.time() if now_ts is None else now_ts
    gaps: Dict[str, dict] = {}
    for chk in (checks if checks is not None else CHECKS):
        try:
            for g in chk(progs, now_ts):
                gaps[g["key"]] = g
        except Exception as e:                       # noqa: BLE001 - reported
            g = _gap(chk.__name__, "(check failed)", [], "unknown",
                     f"{type(e).__name__}: {str(e)[:160]}",
                     "the coverage check itself failed; fix the check or the "
                     "file it reads")
            gaps[g["key"]] = g
    return gaps


ONCE_MEMORY_DAYS = 30


def due(state: Dict[str, dict], gaps: Dict[str, dict], now_ts: float,
        realert_hours: float = REALERT_HOURS, baseline_once: bool = False):
    """(gaps to email now, new state). New on first sight; re-sent every
    realert_hours while open. A by-design exclusion ("once": a pilot's
    unadmitted artist) is sent once and remembered ONCE_MEMORY_DAYS after it
    closes, so next week's event of the same artist stays quiet; with
    baseline_once (the pass's first run) today's exclusions are recorded
    without an email. Any other closed gap leaves the state."""
    new_state: Dict[str, dict] = {}
    out = []
    for k, g in sorted(gaps.items()):
        st = dict(state.get(k) or {"first": now_ts})
        st["seen"] = now_ts
        if g.get("once"):
            st["once"] = True
        last = st.get("sent")
        if last is None and baseline_once and g.get("once"):
            st["sent"] = now_ts                      # known on day one: no email
        elif last is None or (not g.get("once")
                              and now_ts - float(last) >= realert_hours * 3600.0):
            out.append((k, g, st.get("first", now_ts), last is None))
            st["sent"] = now_ts
        new_state[k] = st
    for k, st in state.items():                      # remembered exclusions
        if k not in new_state and st.get("once") and \
                now_ts - float(st.get("seen", 0)) < ONCE_MEMORY_DAYS * 86400:
            new_state[k] = st
    return out, new_state


def body(items) -> tuple:
    """(subject, text) for the due gaps."""
    n_new = sum(1 for _k, _g, _f, is_new in items if is_new)
    n_mk = sum(len(g["markets"]) for _k, g, _f, _n in items)
    subject = (f"IMM-WATCH model coverage: {len(items)} gap(s), {n_mk} market(s)"
               + (f", {n_new} new" if n_new else ""))
    lines = ["Paying markets in a per-entity-modeled family whose model has no",
             "entry for them. FAIL-OPEN = the bot quotes them without that",
             "model's protection; FAIL-CLOSED = they sit dark.", ""]
    by_fam = defaultdict(list)
    for k, g, first, is_new in items:
        by_fam[g["family"]].append((g, first, is_new))
    now = time.time()
    for fam in sorted(by_fam):
        lines.append(fam)
        for g, first, is_new in by_fam[fam]:
            age = (now - float(first)) / 3600.0
            tag = "NEW" if is_new else f"open {age:.0f}h"
            mk = g["markets"]
            mk_txt = ", ".join(mk[:4]) + (f" +{len(mk) - 4} more" if len(mk) > 4 else "")
            lines.append(f"  [{tag}] {g['entity']} -- FAIL-{g['fail'].upper()}: {g['why']}")
            if mk:
                lines.append(f"      markets: {mk_txt}")
            lines.append(f"      fix: {g['fix']}")
        lines.append("")
    lines.append(f"Re-sent every {REALERT_HOURS:g}h while open "
                 "(imm_model_coverage.py, IMM family watch pass 5).")
    return subject, "\n".join(lines)


def main(argv=None) -> int:
    import imm_earnings_overrides as ieo
    import imm_feed_audit as ifa
    client = ieo.build_client()
    progs = ifa.fetch_active_programs(client)
    gaps = find_gaps(progs)
    if not gaps:
        print(f"model coverage: no gaps over {len(progs)} paying markets")
        return 0
    _subject, text = body([(k, g, time.time(), True) for k, g in sorted(gaps.items())])
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
