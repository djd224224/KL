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

  FIT      -- long-dated (median settle >= --min-days-out), not quoted, not
              listed, category not on the high-info-risk list, allowed (not
              blocklisted), not a mention series.
  NEAR     -- settles in 1-14 days: QUOTABLE, listed pinned at the base size (no
              multipliers; news moves these), like the 11/04 midterm wave.
  SOON     -- settles within a day (or no date): Jack's call.
  CHECK    -- high-info-risk category (sports, crypto, financials, weather,
              mentions), an info-risk title (personnel, appearances /
              announcements, counts of reported events, pre-taped TV, company
              reports: INFO_RISK) or blocklisted (company KPIs included): needs Jack.
  QUOTED / LISTED -- the bot already has it.
  MEMBER  -- a member series: the bot's family rules decide (gates, fairs).
  FEW      -- fewer than --min-events (5) events lit in the series: not a wave,
              never listed or alerted (Jack 2026-11-08: "market WAVES
              autoquoting overnight should only enroll WAVES, meaning 5+ more
              events in the same family. things like KXCCLA-28JANALBD should
              not quote" -- w1108-mixed had 19 series, none with 5 events).

A WAVE = new FIT + NEAR series with >= --min-markets markets in all, or >=
--min-pool $/day of pools: exit code 3. No wave, but new CHECK / SOON series over
the same bar: exit 4 (tell Jack, list nothing). Else 0. A series alerts once
(state file). Read-only on Kalshi; never writes the list.

    python wave_detect.py                       # since the last check (or 6 h)
    python wave_detect.py --since 2026-11-06T22:30:00Z --json out.json
    python wave_detect.py --update-state        # the scheduled check: record what it alerted
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import incentive_mm as imm                       # noqa: E402  (reads only)
import send_imm_new_programs as snp              # noqa: E402
from kalshi_reads import kalshi_get, kalshi_get_all  # noqa: E402

LIST = os.path.join(HERE, "run-logs", "incentive-mm", "post_vote_dead_books.json")
CYCLE_DIR = os.path.join(HERE, "run-logs", "incentive-mm")
STATE = os.path.join(HERE, "run-logs", "pvd-scan", "wave_detect_state.json")
REGISTRY = os.path.join(HERE, "run-logs", "pvd-scan", "waves.json")
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
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    state = load_json(STATE, {})
    since = ts(a.since) if a.since else (ts(state["last_check"]) - timedelta(minutes=10)
                                          if state.get("last_check") else now - timedelta(hours=6))

    client = imm._fair_reader_client()
    rows = [p for p in snp.fetch_programs_raw(client)
            if p.get("incentive_type") == "liquidity" and p.get("start_date") and p.get("end_date")
            and ts(p["end_date"]) > now and ts(p["start_date"]) >= since]
    listed = {t for t, e in (load_json(LIST, {}).get("markets") or {}).items()
              if e.get("stop") and ts(e["stop"]) > now}
    reg = load_json(REGISTRY, {}).get("waves") or {}
    reg_series = {s for w in reg.values() for s in (w.get("series") or []) + (w.get("near") or [])}
    quoted = bot_quoted(now)

    per = collections.defaultdict(list)
    for p in rows:
        per[p["market_ticker"]].append(p)
    series = collections.defaultdict(lambda: {"markets": {}, "pool": 0.0, "start": None, "end": None})
    for t, ps in per.items():
        end = max(ts(p["end_date"]) for p in ps)
        cur = [p for p in ps if ts(p["end_date"]) == end]
        st = min(ts(p["start_date"]) for p in cur)
        days = max((end - st).total_seconds() / 86400, 1 / 24)
        pd = sum(float(p.get("period_reward") or 0) / 10000 for p in cur) / days
        s = t.split("-")[0]
        g = series[s]
        g["markets"][t] = {"pool": pd, "start": st, "end": end,
                           "target": float(cur[0].get("target_size_fp") or 1000)}
        g["pool"] += pd
        g["start"] = min(g["start"] or st, st)
        g["end"] = max(g["end"] or end, end)

    out = []
    for s, g in sorted(series.items(), key=lambda kv: -kv[1]["pool"]):
        tick = list(g["markets"])
        try:
            ms = {m["ticker"]: m for m in (kalshi_get_all("/markets", {"series_ticker": s, "status": "open"},
                                                           items_key="markets") or [])}
        except Exception as ex:  # noqa: BLE001
            ms = {}
            print(f"! /markets {s}: {ex}", file=sys.stderr)
        try:
            meta = (kalshi_get(f"/series/{s}") or {}).get("series") or {}
        except Exception:  # noqa: BLE001
            meta = {}
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
        n_list = sum(1 for t in tick if t in listed)
        n_quote = sum(1 for t in tick if t in quoted)
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
        if n_list >= 0.5 * len(tick) or s in reg_series:
            verdict = "LISTED"
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
            if cat in RISKY_CATEGORIES:
                why.append(f"category {cat}")
            if live_price_ladder(s, cat):
                why.append("live-price ladder ('after economic events'): only with a fair, while its market is "
                           "shut (market-wave section 12)")
            if "MENTION" in s:
                why.append("mention series")
            ir = info_risk(meta.get("title") or "")
            if ir:
                why.append(f"info risk: {ir}")
            if n_blocked:
                why.append(f"{n_blocked} blocklisted")
            if why:
                verdict = "CHECK"
            elif days_out is None or days_out < a.min_soon_days:
                # settles within a day (or no date): live / resolving now -- Jack's call
                verdict = "SOON"
                why.append(f"settles in {days_out * 24:.0f}h" if days_out is not None else "no settle date")
            elif days_out < a.min_days_out:
                # near-term but quotable (Jack 2026-11-07: "NEAR ... for the skill, this is ok.
                # like the midterm elections market. that was near but quotable"): listed, pinned
                # at the base size (no multipliers), since news moves these
                verdict = "NEAR"
                why.append(f"settles in {days_out:.0f}d: pin at base")
            else:
                verdict = "FIT"
        out.append({"series": s, "title": meta.get("title") or "", "category": cat, "verdict": verdict,
                    "why": why, "markets": len(tick), "events": len(events), "pool_day": round(g["pool"], 2),
                    "start": zs(g["start"]), "end": zs(g["end"]),
                    "median_settle": zs(med_exp) if med_exp else None,
                    "days_out": round(days_out, 1) if days_out is not None else None,
                    "listed": n_list, "quoted": n_quote, "allowed": n_allowed,
                    "median_spread_c": round(statistics.median(spreads), 1) if spreads else None,
                    "target": statistics.median([v["target"] for v in g["markets"].values()]),
                    "event_list": event_list[:25]})

    alerted = state.get("alerted") or {}
    fit = [r for r in out if r["verdict"] == "FIT"]
    near = [r for r in out if r["verdict"] == "NEAR"]
    ask = [r for r in out if r["verdict"] in ("CHECK", "SOON")]
    quotable = fit + near
    new_q = [r for r in quotable if r["series"] not in alerted]
    new_ask = [r for r in ask if r["series"] not in alerted]

    def over_bar(rs):
        return bool(rs) and (sum(r["markets"] for r in rs) >= a.min_markets
                             or sum(r["pool_day"] for r in rs) >= a.min_pool)
    is_wave = over_bar(new_q)
    needs_jack = not is_wave and over_bar(new_ask)
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
           "wave": is_wave, "needs_jack": needs_jack}

    print(f"wave_detect {zs(now)}: {len(rows)} new liquidity programs since {zs(since)}, {len(out)} series")
    print(f"{'verdict':7} {'series':26} {'mkts':>5} {'evts':>5} {'pool$/d':>9} {'settles':>10} {'days':>6} "
          f"{'listed':>6} {'quoted':>6} {'spread':>6}  category / why")
    for r in out:
        print(f"{r['verdict']:7} {r['series'][:26]:26} {r['markets']:5d} {r['events']:5d} {r['pool_day']:9,.0f} "
              f"{(r['median_settle'] or '-')[:10]:>10} {r['days_out'] if r['days_out'] is not None else '-':>6} "
              f"{r['listed']:6d} {r['quoted']:6d} {r['median_spread_c'] if r['median_spread_c'] is not None else '-':>6}  "
              f"{r['category']}{(' -- ' + '; '.join(r['why'])) if r['why'] else ''}")
    print(f"QUOTABLE (FIT {len(fit)} + NEAR {len(near)} series): {fit_mk} markets, ${fit_pool:,.0f}/day of pools; "
          f"new since the last alert: {len(new_q)} series -> "
          f"{'WAVE' if is_wave else 'NEEDS JACK (CHECK/SOON over the bar)' if needs_jack else 'no wave'} "
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
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, STATE)
    return 3 if is_wave else 4 if needs_jack else 0


if __name__ == "__main__":
    sys.exit(main())
