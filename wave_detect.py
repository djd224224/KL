#!/usr/bin/env python3
"""WAVE DETECTOR + SIZE-UP for the market-wave playbook (Jack 2026-11-07: "the
skill should auto-invoke when a wave of events light up overnight that fit the
scheme").

Reads every active LIQUIDITY program, keeps those that STARTED since --since
(default: this file's state, else 6 h ago), drops markets already on the PVD
list or already quoted by the bot (cycle-log tail), and sizes up the rest by
series: markets, events, pool $/day, program window, horizon (Kalshi's
expected_expiration_time, NOT close_time, which runs up to a year late),
category, book state. Each series gets a verdict:

  FIT      -- long-dated (median settle >= --min-days-out), a KNOWN SHAPE
              (KNOWN_SHAPES: Elections, Politics), not quoted, not listed,
              allowed (not blocklisted), not a mention series.
  NEAR     -- the same, settling in 1-14 days: QUOTABLE, listed pinned at the
              base size (no multipliers; news moves these), like the 11/04
              midterm wave.
  NEW      -- would be FIT / NEAR, but a shape no wave has run (its category is
              not in KNOWN_SHAPES): Jack's call, texted, never auto-listed (Jack
              2026-11-14: "for upcoming new shapes, notify me and it's my call so
              dont default to turning it on e.g. KXYTVIEWSHIGH").
  SOON     -- settles within a day (or no date): Jack's call.
  CHECK    -- high-info-risk category (sports, crypto, financials, weather,
              economics), a mention series, an info-risk title (personnel,
              appearances / announcements, counts of reported events, pre-taped
              TV, company reports: INFO_RISK), a known shape settling on a live
              feed (the bot's own SCAN_LIVE_SOURCE_KEYWORDS), an "after economic
              events" ladder no registry wave lists, or blocklisted (company KPIs
              included): needs Jack.
  QUOTED / LISTED -- the bot already has it (LISTED: on the list, or a series of
              an ENABLED registry wave whose stop cap hasn't passed -- pass 7
              lists its new periods, e.g. the after-economic-events waves'
              next batch; a wave Jack turned off no longer counts).
  MEMBER  -- a member series: the bot's family rules decide (gates, fairs).
  FEW      -- fewer than --min-events (5) events lit in the series: not a wave,
              never listed or alerted (Jack 2026-11-08: "market WAVES
              autoquoting overnight should only enroll WAVES, meaning 5+ more
              events in the same family. things like KXCCLA-28JANALBD should
              not quote" -- w1108-mixed had 19 series, none with 5 events).

A WAVE = new FIT + NEAR series with >= --min-markets markets in all, or >=
--min-pool $/day of pools: exit code 3. No wave, but new CHECK / SOON / NEW
series over the same bar: exit 4 (tell Jack, list nothing; wave_alert texts when
the NEW ones alone clear the bar, "new_shape_over_bar"). Else 0. A series alerts
once (state file). Read-only on Kalshi; never writes the list.

UPCOMING WAVES (--upcoming; Jack 2026-11-14: "market-wave skill should also scan
upcoming rewards markets and callout if a wave would be greenlit so i have
advanced notice"). Kalshi lists programs before they start (GET
/incentive_programs?status=upcoming). This mode judges each market's NEXT
program the way the 24/7 check will judge it at its start: the same verdicts,
the 5+-event rule and the wave bar, per START WINDOW (programs starting within
30 min of a window's first start: about what one 20-min check sees together),
with the dates measured from the start, and without the series the live check
already alerted (it never re-triggers on them).
  exit 3  a window over the bar with NEW FIT / NEAR series: auto mode would list
          it at the start ("would be greenlit"), if preflight passes then.
  exit 4  only CHECK / SOON / NEW over the bar ("needs you"; texted when the NEW
          shapes alone clear the bar).
  exit 5  only a RELIGHT over the bar: an active registry wave's series whose
          markets aren't listed now (a new period after a gap -- the
          after-economic-events waves' next batch, a weekly election relight):
          pass 7 lists them at the start (FX / metals only in the FX close), with
          the wave's current args. Email; texted when its preflight now FAILs.
  0       nothing new.
--preflight runs `pvd_waves.py preflight --auto` NOW on each newly noticed wave
(and `preflight NAME` on each relighting wave), so a FAIL (shard cash,
capacity, the scan budget) can be fixed before the start. Each series is
noticed once per start (its own state file, wave_upcoming_state.json; the live
check's state is only read). The relight task's pass 11 runs it every 15 min
and `wave_alert.py upcoming` sends it. Read-only on Kalshi, like the live check.

    python wave_detect.py                       # since the last check (or 6 h)
    python wave_detect.py --since 2026-11-06T22:30:00Z --json out.json
    python wave_detect.py --update-state        # the scheduled check: record what it alerted
    python wave_detect.py --upcoming [--horizon-hours 72] [--preflight] [--json out.json] [--update-state]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if __name__ == "__main__" and "--upcoming" in sys.argv[1:]:
    # the relight task's pass 11 runs this every 15 min: the bot module's ~20 load lines at
    # import stay out of its log (printed if the import fails)
    import contextlib
    import io
    _buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(_buf):
            import incentive_mm as imm           # noqa: E402  (reads only)
            import send_imm_new_programs as snp  # noqa: E402
    except BaseException:
        sys.stdout.write(_buf.getvalue())
        raise
else:
    import incentive_mm as imm                   # noqa: E402  (reads only)
    import send_imm_new_programs as snp          # noqa: E402
from kalshi_reads import kalshi_get, kalshi_get_all  # noqa: E402

LIST = os.path.join(HERE, "run-logs", "incentive-mm", "post_vote_dead_books.json")
CYCLE_DIR = os.path.join(HERE, "run-logs", "incentive-mm")
STATE = os.path.join(HERE, "run-logs", "pvd-scan", "wave_detect_state.json")
REGISTRY = os.path.join(HERE, "run-logs", "pvd-scan", "waves.json")
UPCOMING_STATE = os.path.join(HERE, "run-logs", "pvd-scan", "wave_upcoming_state.json")
PREFLIGHT_DETECT = os.path.join(HERE, "run-logs", "pvd-scan", "wave_upcoming_window.json")
PREFLIGHT_JSON = os.path.join(HERE, "run-logs", "pvd-scan", "wave_upcoming_preflight.json")
WINDOW_MIN = 30          # one start window: what a 20-min check (since = last check - 10 min) sees together
TICKERS_PER_READ = 80    # GET /markets?tickers=... (open or not yet open)
RISKY_CATEGORIES = {"Sports", "Crypto", "Financials", "Climate and Weather", "Economics"}
# LIVE-PRICE LADDERS (2026-11-14, the market-wave skill update; Jack 2026-11-13 "Do it" on
# "FX + metals only while their markets are closed, with a fair; never weekdays without a
# live feed"): Kalshi's "after economic events" KX<asset>EVENT series settle on a feed price
# one minute after a release. FX / crypto / yields were CHECK by category already, but the
# metals (Commodities) came back FIT on 11/13: auto mode could have listed them on the
# default preset with no fair (84 of 1,002 such quotes sat 5c+ on the wrong side). Every
# such series is CHECK: Jack's call, with the runbook's section 12 playbook.
LIVE_PRICE_CATEGORIES = {"Commodities", "Financials", "Crypto"}


def live_price_ladder(series: str, category: str) -> bool:
    """A KX<asset>EVENT price ladder ("above X one minute after a release")."""
    return bool(re.fullmatch(r"KX[A-Z0-9]+EVENT", series or "")) and (category or "") in LIVE_PRICE_CATEGORIES


# KNOWN SHAPES (Jack 2026-11-14, asked whether the detector should hold back live-feed series
# like KXYTVIEWSHIGH: "yes for upcoming new shapes, notify me and it's my call so dont default
# to turning it on e.g. KXYTVIEWSHIGH. but default to running the wave for elections, after
# economic events, and things of similar shapes to what's been run on waves so far"). A type A
# wave's shape is its series' Kalshi category; the ones he has run as waves and kept are below.
# A FIT / NEAR series in any other category is NEW: held for him and texted, never auto-listed.
# Not counted: the two auto waves he turned off (w1107-mixed: The Voice, AGI-by-date;
# w1108-mixed: headliners, top LLM, local projects), and KXCHINA1, one Science and Technology
# series moved into w1111-w28 as bookkeeping. The "after economic events" ladders (type B) are
# their own shape: their registry waves (w1113-fxmetals, w1114-cryptoev) relight each new
# period by themselves in pass 7, with the fair (LISTED here). Adding a shape is Jack's word:
# add its category here.
KNOWN_SHAPES = {
    "Elections": "the election waves: 11/04 post-vote, 11/06 2028, w1109-control, w1111-elec2028, w1111-w28, "
                 "w1111-statenom",
    "Politics": "the political waves: 11/06 political (w1111-pol), w1111-w28's state laws and House bills",
}


def live_feed(meta: dict) -> str:
    """The host of a settlement source the bot's own live-feed screen names (SCAN_LIVE_SOURCE_
    KEYWORDS, Jack 2026-09-24: "dont quote markets that are easily adversely selected against
    because there is live data flowing visibly directly impacting the market"), '' if none."""
    try:
        for b in imm.series_source_blobs(meta):
            if imm.sources_are_live([b]):
                u = (b.split() or [b])[-1]
                return (re.sub(r"^https?://", "", u).split("/")[0] or u)[:60]
    except Exception:  # noqa: BLE001  (no source read = no live-feed note; the shape rule still holds)
        pass
    return ""
# INFORMATION-RISK SCREEN (the 2026-11-08 pick-off review: "what moves these prices,
# and can someone see it before our resting order moves?"). The overnight auto waves
# listed personnel departures (KXTRUMPADMINLEAVE), a pre-taped TV result
# (KXTHEVOICE), appearance / count ladders (KXTRUMPMEETM, KXTRUMPPHOTO,
# KXLAGOVISITCOUNT, KXTRUMPNUMSTATES) and a company report (KXCCLA) -- outcomes
# someone knows before our quotes move. A series title naming one of these goes to
# CHECK (Jack's call: alerted, never auto-listed). A false CHECK costs an alert; a
# false FIT costs a pick-off.
INFO_RISK = (
    ("personnel", r"\b(leave|leaves|leaving|depart\w*|resign\w*|fired|step(s)? down|ousted|out as|appoint\w*|cabinet)\b"),
    ("appearance / announcement", r"\b(meet|meets|meet with|photo\w*|visit\w*|attend\w*|announce\w*|tweet\w*|"
                                  r"truth social|post|posts|headline\w*|perform\w*|lineup|setlist)\b"),
    ("count of reported events", r"\b(how many|number of|count)\b"),
    ("pre-taped or judged TV", r"\b(the voice|idol|survivor|bachelor\w*|elimination|eliminated|winner of season)\b"),
    ("company report", r"\b(kpi|kpis|earnings|revenue|deliveries|subscribers|guidance)\b"),
)


def info_risk(title: str) -> str:
    """The INFO_RISK class a series title falls in ('' = none)."""
    t = (title or "").lower()
    for label, pat in INFO_RISK:
        m = re.search(pat, t)
        if m:
            return f"{label} ('{m.group(0)}' in the title)"
    return ""


def ts(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def zs(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(p, default):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(p, obj) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, p)


def bot_quoted(now: datetime) -> set:
    """Tickers with our orders resting in the last 15 min of the bot's cycle log."""
    out = set()
    for day in sorted({now.strftime("%Y-%m-%d"), (now - timedelta(minutes=20)).strftime("%Y-%m-%d")}):
        p = os.path.join(CYCLE_DIR, f"cycle_log_{day}.csv")
        if not os.path.exists(p):
            continue
        with open(p, "rb") as fh:
            hdr = fh.readline().decode("utf-8", "replace").strip().split(",")
            ix = {k: i for i, k in enumerate(hdr)}
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 40_000_000))
            chunk = fh.read().decode("utf-8", "replace")
        cut = zs(now - timedelta(minutes=15))
        for line in chunk.split("\n")[1:]:
            q = line.split(",")
            if len(q) < len(hdr) or q[ix["ts"]] < cut:
                continue
            try:
                if float(q[ix["own_bid_ct"]] or 0) + float(q[ix["own_ask_ct"]] or 0) > 0:
                    out.add(q[ix["ticker"]])
            except (ValueError, KeyError):
                continue
    return out


def registry_series(reg: dict) -> dict:
    """{series: {"wave", "cap", "window"}} of the ENABLED registry waves: pass 7 lists their new
    periods (a relight), up to the wave's stop cap. A wave Jack turned off (w1107-mixed,
    w1108-mixed) lists nothing, so its series are judged like any other (they read LISTED
    forever before 2026-11-14)."""
    out = {}
    for name, w in (reg.get("waves") or {}).items():
        if not w.get("enabled", True):
            continue
        cap = None
        if w.get("stop_cap"):
            try:
                cap = ts(w["stop_cap"])
            except ValueError:
                cap = None
        for s in (w.get("series") or []) + (w.get("near") or []):
            out.setdefault(s, {"wave": name, "cap": cap, "window": w.get("window") or ""})
    return out


def registry_wave(ctx: dict, s: str, at: datetime) -> str:
    """The registry wave that lists series `s` for a program at `at` ('' = none): enabled, and
    its stop cap (if any) still ahead of `at`. A capped wave that ran out (the after-economic-
    events waves were capped at their first program end) lists nothing more."""
    r = (ctx.get("reg_series") or {}).get(s)
    if not r or (r.get("cap") is not None and r["cap"] <= at):
        return ""
    return r["wave"]


def bot_context(now: datetime) -> dict:
    """What the bot and the list already have: listed tickers, registry series, quoted tickers."""
    listed = {t for t, e in (load_json(LIST, {}).get("markets") or {}).items()
              if e.get("stop") and ts(e["stop"]) > now}
    return {"listed": listed, "reg_series": registry_series(load_json(REGISTRY, {})), "quoted": bot_quoted(now)}


def live_markets(rows) -> dict:
    """Active program rows -> {market: {pool, start, end, target}}: per market the programs
    with its latest end (their union window and summed $/day)."""
    per = collections.defaultdict(list)
    for p in rows:
        per[p["market_ticker"]].append(p)
    out = {}
    for t, ps in per.items():
        end = max(ts(p["end_date"]) for p in ps)
        cur = [p for p in ps if ts(p["end_date"]) == end]
        st = min(ts(p["start_date"]) for p in cur)
        days = max((end - st).total_seconds() / 86400, 1 / 24)
        pd = sum(float(p.get("period_reward") or 0) / 10000 for p in cur) / days
        out[t] = {"pool": pd, "start": st, "end": end, "target": float(cur[0].get("target_size_fp") or 1000)}
    return out


def upcoming_markets(rows, now: datetime, horizon_h: float) -> dict:
    """status=upcoming rows -> {market: its NEXT program} (the bot's upcoming_by_market:
    the earliest start, programs sharing it summed), starting after now and within the horizon."""
    cut = now + timedelta(hours=horizon_h)
    out = {}
    for t, i in imm.IncentiveMarketMaker.upcoming_by_market(rows, now).items():
        if i and now < i["start"] <= cut:
            out[t] = {"pool": float(i.get("dollars_per_day") or 0), "start": i["start"], "end": i["end"],
                      "target": float(i.get("target") or 0) or 1000.0}
    return out


def group_series(markets: dict) -> dict:
    """{market: program} -> {series: {markets, pool, start, end}}."""
    series = collections.defaultdict(lambda: {"markets": {}, "pool": 0.0, "start": None, "end": None})
    for t, m in markets.items():
        g = series[t.split("-")[0]]
        g["markets"][t] = m
        g["pool"] += m["pool"]
        g["start"] = min(g["start"] or m["start"], m["start"])
        g["end"] = max(g["end"] or m["end"], m["end"])
    return series


def start_windows(markets: dict, minutes: float = WINDOW_MIN) -> list:
    """[(first start, last start, {market: program})], soonest first: the programs starting
    within `minutes` of a window's first start, i.e. what one check sees together at the start."""
    spans = []
    for st in sorted({m["start"] for m in markets.values()}):
        if spans and st < spans[-1][0] + timedelta(minutes=minutes):
            spans[-1][1] = st
        else:
            spans.append([st, st])
    return [(a, b, {t: m for t, m in markets.items() if a <= m["start"] <= b}) for a, b in spans]


def quick_verdict(s: str, tick: list, ctx: dict, min_events: int, at: datetime) -> str:
    """The verdicts judge() reaches without a Kalshi read ('' = read the markets first)."""
    n = len(tick)
    if sum(1 for t in tick if t in ctx["listed"]) >= 0.5 * n or registry_wave(ctx, s, at):
        return "LISTED"
    if sum(1 for t in tick if t in ctx["quoted"]) >= 0.5 * n:
        return "QUOTED"
    if sum(1 for t in tick if imm.IncentiveMarketMaker._allowed(t)) >= 0.5 * n:
        return "MEMBER"
    if len({t.rsplit("-", 1)[0] for t in tick}) < min_events:
        return "FEW"       # ticker prefixes split events at least as finely as Kalshi's event tickers
    return ""


def read_series_markets(s: str) -> dict:
    """The live check's read: the series' open markets."""
    return {m["ticker"]: m for m in (kalshi_get_all("/markets", {"series_ticker": s, "status": "open"},
                                                     items_key="markets") or [])}


def read_tickers(tickers) -> dict:
    """Markets by ticker, open or not yet open (an upcoming program's market may still be
    'initialized': a series' status=open read misses it)."""
    out, tl = {}, sorted(tickers)
    for i in range(0, len(tl), TICKERS_PER_READ):
        for m in kalshi_get_all("/markets", {"tickers": ",".join(tl[i:i + TICKERS_PER_READ])},
                                items_key="markets") or []:
            out[m["ticker"]] = m
    return out


def read_meta(s: str) -> dict:
    try:
        return (kalshi_get(f"/series/{s}") or {}).get("series") or {}
    except Exception:  # noqa: BLE001
        return {}


def judge(s: str, g: dict, ms: dict, meta: dict, ctx: dict, a, now: datetime) -> dict:
    """One series' row and verdict (the live check and the upcoming scan share it). `now` is
    the moment the dates are measured from: the check's time, or an upcoming window's start."""
    tick = list(g["markets"])
    # a market's horizon = the EARLIER of its close and its expected expiration: a general
    # election's close runs a year past its settle (House: close 2029-11, settle 2029-05),
    # but a "when" ladder's strike closes at its own date while the event's expected
    # expiration is the last one (KXPAMINWAGE "Before Mar 1, 2027": close 2027-03-01,
    # expected 2029-01-08)
    exp = []
    for t in tick:
        x = ms.get(t) or {}
        ds = [ts(x[k]) for k in ("expected_expiration_time", "close_time") if x.get(k)]
        if ds:
            exp.append(min(ds))
    med_exp = sorted(exp)[len(exp) // 2] if exp else None
    days_out = (med_exp - now).total_seconds() / 86400 if med_exp else None
    # a market missing from the open list still counts by its own event prefix
    events = {(ms[t].get("event_ticker") if t in ms else None) or t.rsplit("-", 1)[0] for t in tick}
    # each event's readable name (its market's title) and pool, richest first, for the
    # alerts' names list (wave_alert.py; Jack 2026-11-08: "X # of events greenlit or
    # denied, reasoning, 10 names")
    ev_info = {}
    for t in tick:
        m = ms.get(t) or {}
        e = m.get("event_ticker") or t.rsplit("-", 1)[0]
        r = ev_info.setdefault(e, {"event": e, "name": "", "pool_day": 0.0, "markets": 0})
        r["name"] = r["name"] or (m.get("title") or "").strip()
        r["pool_day"] += g["markets"][t]["pool"]
        r["markets"] += 1
    event_list = sorted(ev_info.values(), key=lambda r: (-r["pool_day"], r["event"]))
    for r in event_list:
        r["pool_day"] = round(r["pool_day"], 2)
    n_list = sum(1 for t in tick if t in ctx["listed"])
    n_quote = sum(1 for t in tick if t in ctx["quoted"])
    n_allowed = sum(1 for t in tick if imm.IncentiveMarketMaker._allowed(t))
    n_blocked = sum(1 for t in tick if imm.IncentiveMarketMaker._blocked(t))
    spreads = []
    for t in tick:
        m = ms.get(t) or {}
        try:
            b, k = float(m.get("yes_bid_dollars") or 0) * 100, float(m.get("yes_ask_dollars") or 0) * 100
            if 0 < b < k < 100:
                spreads.append(k - b)
        except (TypeError, ValueError):
            pass
    cat = meta.get("category") or ""
    why = []
    reg_w = registry_wave(ctx, s, now)
    dated = ""
    if n_list >= 0.5 * len(tick) or reg_w:
        verdict = "LISTED"
        if reg_w and n_list < 0.5 * len(tick):
            # not on the list now: a new period after a gap, which pass 7 lists at the start
            w_ = (ctx.get("reg_series") or {}).get(s) or {}
            why.append(f"relight: registry wave {reg_w} lists it (pass 7)"
                       + (" in the FX close only (Fri 5 PM - Sun 5 PM ET)" if w_.get("window") == "fx-shut" else ""))
    elif n_quote >= 0.5 * len(tick):
        verdict = "QUOTED"
    elif n_allowed >= 0.5 * len(tick):
        # a member series: the bot's own family rules (gates, fairs, cutoffs) decide
        # what it quotes there, e.g. KXRT quotes 46 of 187 on purpose
        verdict = "MEMBER"
        why.append(f"member series, bot quotes {n_quote}/{len(tick)} under its family rules")
    elif len(events) < a.min_events:
        # Jack 2026-11-08: a wave is 5+ events in one family; a one-off event
        # (KXCCLA-28JANALBD, 13 strikes of one company report) is not quoted
        verdict = "FEW"
        why.append(f"{len(events)} event(s) lit: a wave needs {a.min_events}+ in one series")
    else:
        ladder = live_price_ladder(s, cat)
        known = cat in KNOWN_SHAPES
        feed = "" if ladder else live_feed(meta)
        if cat in RISKY_CATEGORIES:
            why.append(f"category {cat}")
        if ladder:
            # the after-economic-events waves relight their own series (LISTED above); a ladder
            # that reaches here has no wave and no fair set up for it (yields, a new asset)
            why.append("live-price ladder ('after economic events') no registry wave lists: only with a fair, "
                       "while its market is shut (market-wave section 12)")
        if "MENTION" in s:
            why.append("mention series")
        ir = info_risk(meta.get("title") or "")
        if ir:
            why.append(f"info risk: {ir}")
        if n_blocked:
            why.append(f"{n_blocked} blocklisted")
        if feed and known:
            why.append(f"settles on a live feed ({feed}): the bot's own scan keeps these out (Jack 09/24)")
        if why:
            verdict = "CHECK"
        elif days_out is None or days_out < a.min_soon_days:
            # settles within a day (or no date): live / resolving now -- Jack's call
            verdict = "SOON"
            why.append(f"settles in {days_out * 24:.0f}h" if days_out is not None else "no settle date")
        else:
            # near-term but quotable (Jack 2026-11-07: "NEAR ... for the skill, this is ok.
            # like the midterm elections market. that was near but quotable"): listed, pinned
            # at the base size (no multipliers), since news moves these
            dated = "NEAR" if days_out < a.min_days_out else "FIT"
            if known:
                verdict = dated
                if dated == "NEAR":
                    why.append(f"settles in {days_out:.0f}d: pin at base")
            else:
                # a shape no wave has run (KNOWN_SHAPES): Jack's call, never auto-listed
                verdict = "NEW"
                tags = ", ".join(str(x) for x in (meta.get("tags") or [])[:4])
                why.append(f"new shape: {cat or 'no category read'}{f' ({tags})' if tags else ''} -- no wave has "
                           f"run one")
                if feed:
                    why.append(f"settles on a live feed ({feed}): the bot's own scan keeps these out (Jack 09/24)")
    return {"series": s, "title": meta.get("title") or "", "category": cat, "verdict": verdict,
            "dated": dated, "registry_wave": reg_w,
            "relight": bool(reg_w) and n_list < 0.5 * len(tick),
            "why": why, "markets": len(tick), "events": len(events), "pool_day": round(g["pool"], 2),
            "start": zs(g["start"]), "end": zs(g["end"]),
            "median_settle": zs(med_exp) if med_exp else None,
            "days_out": round(days_out, 1) if days_out is not None else None,
            "listed": n_list, "quoted": n_quote, "allowed": n_allowed,
            "median_spread_c": round(statistics.median(spreads), 1) if spreads else None,
            "target": statistics.median([v["target"] for v in g["markets"].values()]),
            "event_list": event_list[:25]}


def over_bar(rs, a) -> bool:
    return bool(rs) and (sum(r["markets"] for r in rs) >= a.min_markets
                         or sum(r["pool_day"] for r in rs) >= a.min_pool)


ASK_VERDICTS = ("CHECK", "SOON", "NEW")


def wave_call(out: list, alerted: dict, a) -> dict:
    """The wave bar on one check's series: new FIT + NEAR over it = a wave; else new CHECK /
    SOON / NEW over it = Jack's call. A series the live check alerted before never counts as new.
    `new_shape_over_bar`: the NEW shapes alone clear the bar (wave_alert texts for it)."""
    fit = [r for r in out if r["verdict"] == "FIT"]
    near = [r for r in out if r["verdict"] == "NEAR"]
    ask = [r for r in out if r["verdict"] in ASK_VERDICTS]
    new_q = [r for r in fit + near if r["series"] not in alerted]
    new_ask = [r for r in ask if r["series"] not in alerted]
    new_shape = [r for r in new_ask if r["verdict"] == "NEW"]
    is_wave = over_bar(new_q, a)
    return {"fit": fit, "near": near, "new_q": new_q, "new_ask": new_ask, "wave": is_wave,
            "needs_jack": not is_wave and over_bar(new_ask, a),
            "new_shape": new_shape, "new_shape_over_bar": over_bar(new_shape, a)}


def print_rows(out: list) -> None:
    print(f"{'verdict':7} {'series':26} {'mkts':>5} {'evts':>5} {'pool$/d':>9} {'settles':>10} {'days':>6} "
          f"{'listed':>6} {'quoted':>6} {'spread':>6}  category / why")
    for r in out:
        print(f"{r['verdict']:7} {r['series'][:26]:26} {r['markets']:5d} {r['events']:5d} {r['pool_day']:9,.0f} "
              f"{(r['median_settle'] or '-')[:10]:>10} {r['days_out'] if r['days_out'] is not None else '-':>6} "
              f"{r['listed']:6d} {r['quoted']:6d} {r['median_spread_c'] if r['median_spread_c'] is not None else '-':>6}  "
              f"{r['category']}{(' -- ' + '; '.join(r['why'])) if r['why'] else ''}")


def run_preflight(win: dict, now: datetime, fit=None, near=None, auto: bool = True) -> dict:
    """`pvd_waves.py preflight --auto` NOW on an upcoming window's would-be wave (read-only):
    the gates auto mode runs at the start, so a FAIL can be fixed before then. A new shape's
    series (Jack's call) run without --auto: what it would take if he says yes."""
    fit = list(win["new_fit_series"]) if fit is None else list(fit)
    near = list(win["new_near_series"]) if near is None else list(near)
    det = {"at": zs(now), "since": win["start"], "programs": win["programs"], "series": win["series"],
           "new_fit_series": fit, "new_near_series": near, "new_check_series": win["new_check_series"],
           "upcoming": True}
    write_json(PREFLIGHT_DETECT, det)
    cmd = [sys.executable, os.path.join(HERE, "pvd_waves.py"), "preflight"] + (["--auto"] if auto else []) + \
        ["--detect", PREFLIGHT_DETECT, "--json", PREFLIGHT_JSON]
    if fit:
        cmd += ["--series", ",".join(fit)]
    if near:
        cmd += ["--near", ",".join(near)]
    return _preflight(cmd, now)


def run_wave_preflight(name: str, now: datetime) -> dict:
    """`pvd_waves.py preflight NAME` NOW on a registry wave that relights at an upcoming start
    (manual mode: the wave's own args are Jack's, so his size-ups WARN rather than FAIL): cash on
    its shards, resting room, the scan budget, the bot's guards."""
    return _preflight([sys.executable, os.path.join(HERE, "pvd_waves.py"), "preflight", name,
                       "--json", PREFLIGHT_JSON], now)


def _preflight(cmd: list, now: datetime) -> dict:
    try:
        os.remove(PREFLIGHT_JSON)
    except OSError:
        pass
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=HERE)
    except subprocess.TimeoutExpired:
        return {"at": zs(now), "exit": None, "error": "preflight timed out (300 s)", "fail": [], "warn": [], "pass": 0}
    checks = load_json(PREFLIGHT_JSON, None)
    if not isinstance(checks, list):
        tail = [x for x in (p.stderr or p.stdout or "").strip().splitlines() if x.strip()]
        return {"at": zs(now), "exit": p.returncode, "error": (tail[-1] if tail else "no output")[:200],
                "fail": [], "warn": [], "pass": 0}
    return {"at": zs(now), "exit": p.returncode,
            "fail": [f"{c['check']}: {c['detail']}" for c in checks if c.get("status") == "FAIL"],
            "warn": [f"{c['check']}: {c['detail']}" for c in checks if c.get("status") == "WARN"],
            "pass": sum(1 for c in checks if c.get("status") == "PASS")}


def main_upcoming(a, now: datetime) -> int:
    alerted = load_json(STATE, {}).get("alerted") or {}       # the live check's: read, never written here
    ust = load_json(UPCOMING_STATE, {})
    noticed = ust.get("noticed") or {}
    client = imm._fair_reader_client()
    raw = snp.fetch_programs_raw(client, statuses=("upcoming",))
    markets = upcoming_markets(raw, now, a.horizon_hours)
    ctx = bot_context(now)
    ms_cache, meta_cache, wins = {}, {}, []
    for st, last, mk in start_windows(markets):
        out = []
        for s, g in sorted(group_series(mk).items(), key=lambda kv: -kv[1]["pool"]):
            tick = list(g["markets"])
            ms, meta = {}, {}
            if not quick_verdict(s, tick, ctx, a.min_events, st):
                need = [t for t in tick if t not in ms_cache]
                if need:
                    try:
                        ms_cache.update(read_tickers(need))
                    except Exception as ex:  # noqa: BLE001
                        print(f"! /markets {s}: {ex}", file=sys.stderr)
                ms = {t: ms_cache[t] for t in tick if t in ms_cache}
                if s not in meta_cache:
                    meta_cache[s] = read_meta(s)
                meta = meta_cache[s]
            r = judge(s, g, ms, meta, ctx, a, st)     # dates as the check at the start will see them
            if s in alerted:
                r["alerted_before"] = alerted[s]
            out.append(r)
        c = wave_call(out, alerted, a)
        start = zs(st)
        fresh, mark, notice = [], [], None
        if c["wave"]:
            mark = [r["series"] for r in c["new_q"] + c["new_ask"]]
            fresh = [r["series"] for r in c["new_q"] if f"{r['series']}@{start}" not in noticed]
            notice = "greenlit" if fresh else None
        elif c["needs_jack"]:
            mark = [r["series"] for r in c["new_ask"]]
            fresh = [s for s in mark if f"{s}@{start}" not in noticed]
            notice = "needs-you" if fresh else None
        # RELIGHTS (2026-11-14, with Jack's "default to running the wave for elections, after
        # economic events"): an active registry wave's series whose markets aren't listed now --
        # pass 7 lists the new period at the start on the wave's CURRENT args (its size-ups too;
        # FX / metals only in the FX close). No decision is needed, so it's an email; it texts
        # when its preflight now FAILs.
        relit = [r for r in out if r.get("relight")]
        relight = None
        if over_bar(relit, a):
            rw = {}
            for r in relit:
                x = rw.setdefault(r["registry_wave"], {
                    "series": [], "markets": 0, "events": 0, "pool_day": 0.0,
                    "window": ((ctx.get("reg_series") or {}).get(r["series"]) or {}).get("window") or ""})
                x["series"].append(r["series"])
                x["markets"] += r["markets"]
                x["events"] += r["events"]
                x["pool_day"] = round(x["pool_day"] + r["pool_day"], 2)
            rl_fresh = [r["series"] for r in relit if f"{r['series']}@{start}" not in noticed]
            relight = {"waves": rw, "fresh_series": rl_fresh, "notice": bool(rl_fresh), "preflight": {}}
            mark += [r["series"] for r in relit if r["series"] not in mark]
        wins.append({"start": start, "last_start": zs(last), "in_hours": round((st - now).total_seconds() / 3600, 1),
                     "programs": len(mk), "series": out,
                     "fit_series": [r["series"] for r in c["fit"]], "near_series": [r["series"] for r in c["near"]],
                     "new_fit_series": [r["series"] for r in c["new_q"] if r["verdict"] == "FIT"],
                     "new_near_series": [r["series"] for r in c["new_q"] if r["verdict"] == "NEAR"],
                     "new_check_series": [r["series"] for r in c["new_ask"]],
                     "new_shape_series": [r["series"] for r in c["new_shape"]],
                     "new_shape_over_bar": c["new_shape_over_bar"],
                     "wave": c["wave"], "needs_jack": c["needs_jack"], "notice": notice,
                     "fresh_series": fresh, "mark": mark, "preflight": None, "relight": relight})
    if a.preflight:
        for w in wins:
            if w["notice"] == "greenlit":
                w["preflight"] = run_preflight(w, now)
            elif w["notice"] == "needs-you" and w["new_shape_over_bar"]:
                # a new shape is Jack's call: what listing it would take if he says yes (no --auto)
                rows = {r["series"]: r for r in w["series"]}
                ns = w["new_shape_series"]
                w["preflight"] = run_preflight(w, now, fit=[s for s in ns if rows[s].get("dated") == "FIT"],
                                               near=[s for s in ns if rows[s].get("dated") == "NEAR"], auto=False)
            rl = w.get("relight")
            if rl and rl["notice"]:
                for name in rl["waves"]:
                    rl["preflight"][name] = run_wave_preflight(name, now)

    res = {"at": zs(now), "mode": "upcoming", "horizon_hours": a.horizon_hours, "window_min": WINDOW_MIN,
           "params": {"min_markets": a.min_markets, "min_pool": a.min_pool, "min_days_out": a.min_days_out,
                      "min_soon_days": a.min_soon_days, "min_events": a.min_events,
                      "risky_categories": sorted(RISKY_CATEGORIES)},
           "upcoming_programs": len(raw), "markets": len(markets), "windows": wins,
           "known_shapes": sorted(KNOWN_SHAPES),
           "notices": sum(1 for w in wins if w["notice"] or (w.get("relight") or {}).get("notice"))}

    print(f"wave_detect --upcoming {zs(now)}: {len(raw)} upcoming programs, {len(markets)} markets starting "
          f"within {a.horizon_hours:g} h, {len(wins)} start window(s)")
    quiet = collections.Counter()
    judged = ("FIT", "NEAR", "NEW", "CHECK", "SOON")
    for w in wins:
        rows = w["series"]
        cnt = collections.Counter(r["verdict"] for r in rows)
        rl = w.get("relight")
        if not w["notice"] and not rl and not any(r["verdict"] in judged for r in rows):
            quiet["windows"] += 1          # only LISTED / QUOTED / MEMBER / FEW: one summary line below
            quiet.update(cnt)
            continue
        call = ("WOULD BE GREENLIT" if w["wave"] else
                "NEEDS JACK (new shape over the bar)" if w["needs_jack"] and w["new_shape_over_bar"] else
                "NEEDS JACK (CHECK/SOON/NEW over the bar)" if w["needs_jack"] else "no wave")
        q = [r for r in rows if r["series"] in set(w["new_fit_series"]) | set(w["new_near_series"])]
        size = (f" (new FIT {len(w['new_fit_series'])} + NEAR {len(w['new_near_series'])} series: "
                f"{sum(r['markets'] for r in q)} markets, ${sum(r['pool_day'] for r in q):,.0f}/day)" if q else "")
        print(f"window {w['start']} (in {w['in_hours']:g} h): {w['programs']} markets, {len(rows)} series -> "
              f"{call}{size}{' [NEW NOTICE: ' + w['notice'] + ']' if w['notice'] else ''}; "
              + ", ".join(f"{n} {v}" for v, n in cnt.most_common()))
        show = [r for r in rows if r["verdict"] in judged]
        if show:
            print_rows(show)
        for r in show:
            if r.get("alerted_before"):
                print(f"        {r['series']}: alerted {r['alerted_before'][:16]} by the live check -- the start "
                      f"won't re-trigger it")
        pf = w.get("preflight")
        if pf:
            print(f"        preflight now: exit {pf.get('exit')}, {pf.get('pass', 0)} pass, {len(pf.get('fail') or [])} "
                  f"fail, {len(pf.get('warn') or [])} warn{(' -- ' + pf['error']) if pf.get('error') else ''}")
            for x in (pf.get("fail") or [])[:6]:
                print(f"          FAIL {x}")
        if rl:
            for name, x in rl["waves"].items():
                print(f"        RELIGHT{' [NEW NOTICE]' if rl['notice'] else ''}: {name} lists {x['markets']} markets "
                      f"({x['events']} events, ${x['pool_day']:,.0f}/day) at the start"
                      + (" -- in the FX close only" if x.get("window") == "fx-shut" else "")
                      + f": {', '.join(x['series'][:6])}{' ...' if len(x['series']) > 6 else ''}")
                p = (rl.get("preflight") or {}).get(name)
                if p:
                    print(f"          preflight {name} now: exit {p.get('exit')}, {p.get('pass', 0)} pass, "
                          f"{len(p.get('fail') or [])} fail, {len(p.get('warn') or [])} warn"
                          f"{(' -- ' + p['error']) if p.get('error') else ''}")
                    for f_ in (p.get("fail") or [])[:4]:
                        print(f"            FAIL {f_}")
    if quiet:
        n = quiet.pop("windows")
        print(f"{n} other window(s), nothing to judge: " + ", ".join(
            f"{v} {k}" for v, k in sorted(((v, k) for k, v in quiet.items()), reverse=True)) + " series")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
    if a.update_state:
        for w in wins:
            for s in w["mark"]:
                noticed.setdefault(f"{s}@{w['start']}", zs(now))
        keep = now - timedelta(days=1)
        noticed = {k: v for k, v in noticed.items() if ts(k.rsplit("@", 1)[1]) > keep}
        write_json(UPCOMING_STATE, {"last_check": zs(now), "noticed": noticed})
    if any(w["notice"] == "greenlit" for w in wins):
        return 3
    if any(w["notice"] for w in wins):
        return 4
    return 5 if any((w.get("relight") or {}).get("notice") for w in wins) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="ISO UTC: programs that started at or after this (default: state, else 6 h ago)")
    ap.add_argument("--min-markets", type=int, default=40)
    ap.add_argument("--min-pool", type=float, default=1000.0, help="$/day of pools")
    ap.add_argument("--min-days-out", type=float, default=14.0, help="FIT at or past this; NEAR below it")
    ap.add_argument("--min-soon-days", type=float, default=1.0, help="SOON (Jack's call) below this")
    ap.add_argument("--min-events", type=int, default=5,
                    help="a series needs this many events lit to count as a wave (FEW below it)")
    ap.add_argument("--json", help="write the full result here")
    ap.add_argument("--update-state", action="store_true",
                    help="record this check (and the series it alerted) in the state file")
    ap.add_argument("--upcoming", action="store_true",
                    help="judge Kalshi's UPCOMING programs as the check will at their start (advance notice)")
    ap.add_argument("--horizon-hours", type=float, default=72.0,
                    help="--upcoming: programs starting within this many hours")
    ap.add_argument("--preflight", action="store_true",
                    help="--upcoming: run pvd_waves.py preflight --auto now on each newly noticed wave")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    if a.upcoming:
        return main_upcoming(a, now)
    state = load_json(STATE, {})
    since = ts(a.since) if a.since else (ts(state["last_check"]) - timedelta(minutes=10)
                                          if state.get("last_check") else now - timedelta(hours=6))

    client = imm._fair_reader_client()
    rows = [p for p in snp.fetch_programs_raw(client)
            if p.get("incentive_type") == "liquidity" and p.get("start_date") and p.get("end_date")
            and ts(p["end_date"]) > now and ts(p["start_date"]) >= since]
    ctx = bot_context(now)
    series = group_series(live_markets(rows))

    out = []
    for s, g in sorted(series.items(), key=lambda kv: -kv[1]["pool"]):
        try:
            ms = read_series_markets(s)
        except Exception as ex:  # noqa: BLE001
            ms = {}
            print(f"! /markets {s}: {ex}", file=sys.stderr)
        out.append(judge(s, g, ms, read_meta(s), ctx, a, now))

    alerted = state.get("alerted") or {}
    c = wave_call(out, alerted, a)
    fit, near, new_q, new_ask = c["fit"], c["near"], c["new_q"], c["new_ask"]
    quotable = fit + near
    is_wave, needs_jack = c["wave"], c["needs_jack"]
    fit_mk = sum(r["markets"] for r in quotable)
    fit_pool = sum(r["pool_day"] for r in quotable)
    res = {"at": zs(now), "since": zs(since), "params": {"min_markets": a.min_markets, "min_pool": a.min_pool,
                                                          "min_days_out": a.min_days_out,
                                                          "min_soon_days": a.min_soon_days,
                                                          "min_events": a.min_events,
                                                          "risky_categories": sorted(RISKY_CATEGORIES)},
           "programs": len(rows), "series": out,
           "fit_series": [r["series"] for r in fit], "near_series": [r["series"] for r in near],
           "fit_markets": fit_mk, "fit_pool_day": round(fit_pool, 2),
           "new_fit_series": [r["series"] for r in new_q if r["verdict"] == "FIT"],
           "new_near_series": [r["series"] for r in new_q if r["verdict"] == "NEAR"],
           "new_check_series": [r["series"] for r in new_ask],
           "new_shape_series": [r["series"] for r in c["new_shape"]],
           "new_shape_over_bar": c["new_shape_over_bar"],
           "known_shapes": sorted(KNOWN_SHAPES),
           "wave": is_wave, "needs_jack": needs_jack}

    print(f"wave_detect {zs(now)}: {len(rows)} new liquidity programs since {zs(since)}, {len(out)} series")
    print_rows(out)
    print(f"QUOTABLE (FIT {len(fit)} + NEAR {len(near)} series): {fit_mk} markets, ${fit_pool:,.0f}/day of pools; "
          f"new since the last alert: {len(new_q)} series -> "
          f"{'WAVE' if is_wave else 'NEEDS JACK (new shape over the bar)' if needs_jack and c['new_shape_over_bar'] else 'NEEDS JACK (CHECK/SOON/NEW over the bar)' if needs_jack else 'no wave'} "
          f"(bar: {a.min_markets} markets or ${a.min_pool:,.0f}/day, from series with "
          f"{a.min_events}+ events lit)")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
    if a.update_state:
        state["last_check"] = zs(now)
        for r in (new_q if is_wave else []) + (new_ask if (is_wave or needs_jack) else []):
            alerted[r["series"]] = zs(now)
        state["alerted"] = {k: v for k, v in alerted.items() if ts(v) > now - timedelta(days=30)}
        write_json(STATE, state)
    return 3 if is_wave else 4 if needs_jack else 0


if __name__ == "__main__":
    sys.exit(main())
