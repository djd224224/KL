#!/usr/bin/env python3
r"""
imm_quote_gaps.py — morning email: which live-incentive markets is the IMM bot
NOT quoting, ranked by what they'd plausibly earn (est $/day and c/min), with a
plain-English line on what each event is (from Kalshi's event title).

Where the numbers come from: the bot's OWN machinery, imported from
incentive_mm.py — fetch_programs() for the full paying universe, _allowed()/
_blocked() for allowlist policy, _screen() for the hard screens, and
_estimate_candidate_yield() (amended-rules estimator: reference-band pro-rata,
0.5^ticks-below-ref, exclusion coverage) run against each unquoted market's
live book with the bot's standard ladder overlaid. Config parity: the live
launcher's $ProbeEnv block (run_incentive_mm.ps1) is parsed and applied to the
environment BEFORE incentive_mm is imported, so blocklist/ladder/caps here are
exactly what the running bot sees. "Currently quoted" = selected_tickers from
the bot's persisted state (imm_state.json, rewritten every cycle).

STRICTLY READ-ONLY against the live system: the offline bot instance here
never runs a cycle, never places/cancels orders, and never calls
_save_persist(), so imm_state.json is only ever read.

Reason labels (why a paying market isn't quoted):
  blocklisted / frozen        deliberate config (launcher IMM_BLOCKLIST,
                              FREEZE_SERIES) — positions ride, zero orders
  not in allowlist            series outside ALLOW_* policy (the DDR5 class)
  no-new gate                 series in run-off (NO_NEW_SERIES), fresh
                              markets barred
  yielded to manual           manual standoff / manual footprint in the event
  screened: <reason>          _screen() verdict (wide, extreme_mid, one_sided,
                              no_volume, ...)
  under payout floor          est total accrual (with accrued + 1h-peak
                              credit) can't reach the series' min-payout
                              floor (global $1; per-series overrides apply)
  zero est yield              estimator sees no earnable share
  capacity (cap/budget)       eligible, positive — lost to the event cap /
                              collateral budget / candidate-cap truncation

Estimation is bounded (IMM_GAPS_MAX_BOOKS orderbook reads, IMM_GAPS_MAX_PER_EV
per event, biggest pools first); events not reached are shown with their pool
only and flagged unestimated — never silently dropped.

Scheduled daily 7:20 AM ET ("KL imm quote-gaps"), after the 6:45 overrides/
auto-enroll task and the 7:10 digest. Idempotent via a sent-marker; --test
sends immediately without the marker; --dry builds and prints only.

Credentials: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD from the environment,
falling back to HKCU\Environment (Task Scheduler's stripped env).
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.abspath(__file__))
LAUNCHER_PATH = os.path.join(REPO, "run_incentive_mm.ps1")


def _env_from_registry(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ""


def _apply_launcher_env() -> dict:
    """Mirror the live bot's config: parse `set NAME=VALUE&&` pairs out of the
    launcher's $ProbeEnv string (the task runs with -Probe) and apply them.
    Must run BEFORE incentive_mm is imported — its config is read at import."""
    applied = {}
    try:
        with open(LAUNCHER_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return applied
    # The launcher initializes $ProbeEnv = "" and only fills it under -Probe
    # (which the live task passes) — take the assignment that actually
    # contains `set` pairs, not the empty initializer.
    for chunk in re.findall(r'\$ProbeEnv\s*=\s*"(set .*?)"', text, re.S):
        for name, val in re.findall(
                r"set ([A-Za-z_][A-Za-z0-9_]*)=([^&]*)&&", chunk):
            os.environ[name] = val
            applied[name] = val
    return applied


_LAUNCHER_ENV = _apply_launcher_env()

for _v in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD"):
    if not os.environ.get(_v):
        _val = _env_from_registry(_v)
        if _val:
            os.environ[_v] = _val

# Import AFTER the env fixups so module-level config/creds pick them up.
import incentive_mm as imm            # noqa: E402
from incentive_mm import log          # noqa: E402

if _LAUNCHER_ENV:
    log(f"[GAPS] mirrored {len(_LAUNCHER_ENV)} launcher env vars "
        f"({', '.join(sorted(_LAUNCHER_ENV))})")
else:
    log("[GAPS] WARNING: no $ProbeEnv vars parsed from the launcher — "
        "running on incentive_mm DEFAULTS (blocklist/caps may differ from live)")

STATE_PATH = os.path.join(imm.STATUS_DIR, "imm_state.json")
STATUS_PATH = os.path.join(imm.STATUS_DIR, "status_incentive_mm.json")

MAX_BOOKS = int(os.environ.get("IMM_GAPS_MAX_BOOKS", "250"))
MAX_PER_EVENT = int(os.environ.get("IMM_GAPS_MAX_PER_EV", "12"))
TOP_EVENTS = int(os.environ.get("IMM_GAPS_TOP", "20"))
MIN_SHOW_DOLLARS = float(os.environ.get("IMM_GAPS_MIN_EST", "0.50"))
# Detail reads are cheap (50/call); estimate-worthy disallowed tail is bounded.
MAX_DETAIL_MARKETS = int(os.environ.get("IMM_GAPS_MAX_DETAILS", "900"))

# _screen verdicts that mean "genuinely unquotable now" — excluded entirely
# rather than listed as opportunities (matches STICKY_DEATH_REASONS + no_target).
DEAD_REASONS = frozenset(
    {"cutoff", "no_event_window", "closing", "program_over", "no_target"})


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def ticker_day_over(t: str, now_utc: datetime) -> bool:
    # Mirror of refresh_universe's ticker_cutoff_passed pre-filter
    # (incentive_mm.py) — keep in sync.
    ov = imm.series_override(imm.series_of(t))
    if ov and ov.cutoff_from_close_min is not None:
        return False
    if imm.series_of(t).startswith(imm._EARNINGS_PREFIX) \
            or t.rsplit("-", 1)[0] in imm.EVENT_START_OVERRIDES:
        return False
    if imm.series_of(t) in imm.SCHEDULE_RESOLVED_SERIES:
        return False
    td = imm.parse_event_date(t)
    return td is not None and now_utc >= td + timedelta(hours=24)


def build_meta(bot, t: str, info: dict, m: dict, now_utc: datetime):
    """Mirror of refresh_universe's MarketMeta construction (incentive_mm.py)
    — same cutoff resolution (close-anchored override / resolver / occurrence
    fallback / series tighteners). Keep in sync."""
    event_ticker = m.get("event_ticker") or t.rsplit("-", 1)[0]
    series = t.split("-")[0]
    close_time = imm.parse_iso_utc(m.get("close_time", ""))
    ov_close = imm.series_override(series)
    if ov_close and ov_close.cutoff_from_close_min is not None:
        cutoff = (close_time - timedelta(minutes=ov_close.cutoff_from_close_min)
                  if close_time is not None else None)
    else:
        resolved = bot.resolver.resolve(series, event_ticker)
        if resolved is not None:
            ov = imm.series_override(series)
            if event_ticker in imm.EVENT_START_OVERRIDES:
                buffer_min = imm.OVERRIDE_BUFFER_MIN
            elif ov and ov.start_buffer_min is not None:
                buffer_min = ov.start_buffer_min
            else:
                buffer_min = imm.EVENT_START_BUFFER_MIN
            cutoff = resolved - timedelta(minutes=buffer_min)
        else:
            cutoff = imm.trade_cutoff_utc(
                event_ticker, imm.parse_iso_utc(m.get("occurrence_datetime", "")),
                imm.parse_iso_utc(m.get("expected_expiration_time", "")))
    cutoff = imm.apply_series_cutoff_adjustments(series, event_ticker, cutoff,
                                                 close_time=close_time)
    bid = imm.market_cents(m, "yes_bid")
    ask = imm.market_cents(m, "yes_ask")
    try:
        volume = float(m.get("volume_fp") or m.get("volume") or 0)
    except (TypeError, ValueError):
        volume = 0.0
    return imm.MarketMeta(
        ticker=t, event_ticker=event_ticker, series=series,
        dollars_per_day=info["dollars_per_day"], program_end=info["end"],
        target_size=info["target"], discount_factor=info["df"],
        cutoff=cutoff, close_time=close_time,
        mid_cents=((bid + ask) / 2.0 if bid and ask else None),
        spread_cents=((ask - bid) if bid and ask else None),
        volume=volume, status=m.get("status", ""),
        open_time=imm.parse_iso_utc(m.get("open_time", "")))


def bulk_market_details(client, tickers) -> dict:
    markets = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            resp = client.get_markets(tickers=",".join(chunk), limit=len(chunk))
            for m in (resp.get("markets") or []):
                markets[m.get("ticker", "")] = m
        except Exception as e:
            log(f"! market detail read failed for a chunk ({e}); skipped")
    return markets


_TITLE_CACHE: dict = {}


# Plain-English descriptions (Jack 2026-08-04: "provide plain explanation of
# what each event is"). Kalshi's own event title is used when it already reads
# like a sentence; these patterns cover the families whose titles are terse or
# absent, and add the concrete subject (which city, which movie, which company).
_CITY = {
    "AUS": "Austin", "CHI": "Chicago", "DC": "Washington DC", "LAX": "Los Angeles",
    "NYC": "New York", "MIA": "Miami", "DEN": "Denver", "HOU": "Houston",
    "DAL": "Dallas", "SEA": "Seattle", "PHIL": "Philadelphia", "PHX": "Phoenix",
    "STP": "St Paul", "OKC": "Oklahoma City", "MIN": "Minneapolis",
    "BOS": "Boston", "ATL": "Atlanta", "LV": "Las Vegas", "SATX": "San Antonio",
    "NOLA": "New Orleans",
}
_FAMILY = {
    "KXDIESELD": "US diesel price, daily AAA print",
    "KXDIESELW": "US diesel price, weekly AAA print",
    "KXAAAGASD": "US gas price, daily AAA print",
    "KXAAAGASW": "US gas price, weekly AAA print",
    "KXAAAGASM": "US gas price, monthly AAA print",
    "KXUSGASCPI": "US gasoline CPI print",
    "KXNHSALES": "US new-home sales print",
    "KXSCFI": "Shanghai container freight index",
    "KXRT": "Rotten Tomatoes critic score",
    "KXBKFT": "Burger King monthly foot traffic",
    "KXYUMTBFT": "Taco Bell monthly foot traffic",
    "KXAQICITY": "Air-quality index reading",
    "KXRAIN": "Whether it rains in a city that day",
    "KXTRUMPMENTION": "Words Trump says during an appearance",
    "KXTRUMPSAY": "Words Trump says this week",
    "KXTRUMPSAYMONTH": "Words Trump says this month",
    "KXTRUMPSAYCOMPANY": "Companies Trump names",
}


def describe_event(event_ticker: str, title: str = "") -> str:
    """One clause a human can read without knowing the ticker scheme."""
    series = event_ticker.split("-")[0]
    t = (title or "").strip()
    # hourly temperature: KXTEMP<CITY>H-26AUG0209 -> city + the hour it settles
    if series.startswith("KXTEMP") and series.endswith("H"):
        city = _CITY.get(series[6:-1], series[6:-1])
        tail = event_ticker.split("-")[-1]
        hour = ""
        if len(tail) >= 2 and tail[-2:].isdigit():
            h = int(tail[-2:])
            ampm = "am" if h < 12 else "pm"
            h12 = h if 1 <= h <= 12 else (12 if h % 12 == 0 else h % 12)
            hour = f", {h12}{ampm} hour"
        return f"Hourly high temperature in {city}{hour}"
    if series.startswith("KXRAIN") and series.endswith("M"):
        city = _CITY.get(series[6:-1], series[6:-1])
        return f"Number of rainy days this month in {city}"
    if series.startswith("KXEARNINGSMENTION"):
        co = series[len("KXEARNINGSMENTION"):]
        return f"Words said on {co}'s earnings call"
    if series.startswith("KXYTVIEWSW"):
        return "Highest daily YouTube view count this week"
    if series.startswith("KXJOINCLUB"):
        return "Which club a footballer signs for next"
    for pref, desc in _FAMILY.items():
        if series == pref or series.startswith(pref):
            return desc
    # Kalshi's own title is often already a plain question — prefer it.
    if t:
        return t if len(t) <= 70 else t[:67] + "..."
    return series


def event_title(client, ev: str, fallback: str = "") -> str:
    if ev in _TITLE_CACHE:
        return _TITLE_CACHE[ev]
    title = ""
    try:
        e = (client.get(f"/events/{ev}") or {}).get("event") or {}
        title = str(e.get("title") or "").strip() \
            or str(e.get("sub_title") or "").strip()
    except Exception:
        pass
    _TITLE_CACHE[ev] = title or fallback
    return _TITLE_CACHE[ev]


def fmt_window(end, now_utc: datetime) -> str:
    """Compact ET rendering of when quoting would have to stop."""
    if end is None:
        return "open-ended"
    local = end.astimezone(imm.ET)
    if end - now_utc <= timedelta(hours=24):
        return "til " + local.strftime("%I:%M%p ET").lstrip("0").lower()
    return "til " + local.strftime("%b %d")


def classify_and_estimate(client, bot, now_utc: datetime):
    """Returns (event_rows, ctx). Walks every live-program market the bot is
    not currently quoting, classifies why, and estimates what the bot's
    standard ladder would earn there (bounded book reads)."""
    now_ts = now_utc.timestamp()
    # Same hot-reload the bot does each refresh: overrides + auto-enrolled
    # series + rain fairs.
    imm.load_file_event_overrides()
    imm.load_extra_allow_series()
    imm.load_rain_fair()

    state = load_json(STATE_PATH)
    status = load_json(STATUS_PATH)
    quoted = set(state.get("selected_tickers") or [])
    quoted_events = {_event_of(t) for t in quoted}
    standoff = set(status.get("manual_standoff") or [])

    programs = bot.fetch_programs()
    if not programs:
        raise RuntimeError("fetch_programs returned nothing (API glitch?)")

    try:
        positions = bot.fetch_positions()
    except Exception as e:
        log(f"! positions read failed ({e}); manual-event detection degraded")
        positions = {}
    manual_evts = bot.manual_events(positions)

    # The live bot's sticky set (selected ∪ persisted) both come from the same
    # persisted selection here.
    prev_selected = quoted

    # ---- classify every unquoted paying market -----------------------------
    rows = []          # dicts: ticker/event/series/pool/reason/meta(None ok)
    for t, info in programs.items():
        if t in quoted or info["dollars_per_day"] <= 0:
            continue
        if ticker_day_over(t, now_utc):
            continue
        series = imm.series_of(t)
        if bot._blocked(t):
            reason = "frozen" if series in imm.FREEZE_SERIES else "blocklisted"
        elif not bot._allowed(t):
            reason = "not in allowlist"
        else:
            reason = None      # resolved after details/screen/estimate
        rows.append({"ticker": t, "event": _event_of(t), "series": series,
                     "pool": info["dollars_per_day"], "info": info,
                     "reason": reason, "est": None, "meta": None})

    # ---- market details (allowed rows always; disallowed by pool rank) -----
    allowed_rows = [r for r in rows if r["reason"] is None]
    other_rows = sorted((r for r in rows if r["reason"] is not None),
                        key=lambda r: -r["pool"])
    detail_set = [r["ticker"] for r in allowed_rows]
    detail_set += [r["ticker"] for r in other_rows][:max(
        0, MAX_DETAIL_MARKETS - len(detail_set))]
    details = bulk_market_details(client, detail_set)

    # Candidate-cap truncation (top-N by pool over the ALLOWED universe incl.
    # quoted markets) — the bot's silent exclusion, surfaced here.
    allowed_pool_rank = sorted(
        (t for t, info in programs.items()
         if bot._allowed(t) and info["dollars_per_day"] > 0),
        key=lambda t: -programs[t]["dollars_per_day"])
    beyond_cap = set(allowed_pool_rank[imm.MAX_CANDIDATE_BOOKS:])

    for r in allowed_rows:
        t = r["ticker"]
        m = details.get(t)
        if not m or m.get("status") not in ("active", "open"):
            r["reason"] = "dead"
            continue
        meta = build_meta(bot, t, r["info"], m, now_utc)
        r["meta"] = meta
        if t in standoff or meta.event_ticker in manual_evts:
            r["reason"] = "yielded to manual"
            continue
        sr = bot._screen(meta, now_utc)
        if sr in DEAD_REASONS:
            r["reason"] = "dead"
        elif sr:
            r["reason"] = f"screened: {sr}"
        elif t in beyond_cap:
            r["reason"] = f"candidate cap (top-{imm.MAX_CANDIDATE_BOOKS} by pool)"
        # else: resolved after estimation (floor / zero / capacity)
    for r in other_rows:
        m = details.get(r["ticker"])
        if m is not None:
            if m.get("status") not in ("active", "open"):
                r["reason"] = "dead"
            else:
                r["meta"] = build_meta(bot, r["ticker"], r["info"], m, now_utc)

    rows = [r for r in rows if r["reason"] != "dead"]

    # ---- bounded estimation, biggest pools first, per-event cap ------------
    # Actionable candidates get the book budget before deliberately-off
    # families (blocklist/freeze are "awareness" rows — the GPU pools alone
    # could otherwise eat every read).
    by_event: dict = {}
    for r in rows:
        by_event.setdefault(r["event"], []).append(r)

    def _deliberate_ev(ev: str) -> bool:
        return all(r["reason"] in ("blocklisted", "frozen")
                   for r in by_event[ev])

    event_order = sorted(
        by_event,
        key=lambda ev: (_deliberate_ev(ev),
                        -sum(r["pool"] for r in by_event[ev])))
    # Deliberately-off families get a reserved slice: the actionable
    # candidates always outnumber the budget, so without it the awareness
    # table would never carry estimates.
    delib_reserve = min(40, MAX_BOOKS // 5)
    books = 0
    for ev in event_order:
        cap = MAX_BOOKS - (0 if _deliberate_ev(ev) else delib_reserve)
        if books >= cap:
            continue
        for r in sorted(by_event[ev], key=lambda r: -r["pool"])[:MAX_PER_EVENT]:
            if books >= cap:
                break
            if r["meta"] is None:
                continue
            books += 1
            if bot._estimate_candidate_yield(r["meta"], []):
                r["est"] = r["meta"].est_dollars_per_day
                # Effective $ exposure of the modeled ladder: collateral the
                # ladder (incl. pads) ties up, scaled up by book ACTION —
                # a book that turns over N times a day will fill resting
                # size, so a dollar resting there is more exposed than one
                # on a dead book. turns = 24h volume / total book depth,
                # capped at 3x. getattr: degrade to no-estimate if the
                # meta fields aren't present (older incentive_mm).
                coll = getattr(r["meta"], "est_collateral_dollars", 0.0)
                depth = getattr(r["meta"], "book_depth_contracts", 0.0)
                md = details.get(r["ticker"], {})
                try:
                    v24 = float(md.get("volume_24h_fp")
                                or md.get("volume_24h") or 0)
                except (TypeError, ValueError):
                    v24 = 0.0
                turns = min(v24 / depth, 3.0) if depth > 0 else 0.0
                r["exp"] = coll * (1.0 + turns) if coll > 0 else None
            elif r["reason"] is None:
                r["reason"] = "book unreadable"

    # ---- resolve remaining reasons via the bot's floor math ----------------
    for r in rows:
        if r["reason"] is not None:
            continue
        meta = r["meta"]
        if r["est"] is None:
            r["reason"] = "capacity (cap/budget)"   # not estimated this run
            continue
        est_total = meta.est_dollars_per_day * imm._quotable_days(meta, now_utc)
        peak, pts = (bot._est_peak.get(r["ticker"]) or (0.0, 0.0))
        if now_ts - pts > imm.EST_PEAK_TTL_SECS:
            peak = 0.0
        accrued = bot.state.accrued_est.get(r["ticker"], 0.0)
        curated = imm.curated_event(meta.event_ticker, meta.series, now_utc)
        if meta.series in imm.NO_NEW_SERIES and r["ticker"] not in prev_selected:
            r["reason"] = "no-new gate"
        elif meta.yield_per_contract <= 0:
            r["reason"] = "zero est yield"
        elif not curated and meta.est_dollars_per_day \
                < imm.series_min_est_rate(meta.series):
            r["reason"] = "under rate floor"
        elif not curated and accrued + max(est_total, peak) \
                < imm.series_min_est_total(meta.series):
            r["reason"] = "under payout floor"
        else:
            r["reason"] = "capacity (cap/budget)"

    # ---- event rollup -------------------------------------------------------
    event_rows = []
    for ev, evr in by_event.items():
        evr = [r for r in evr if r["reason"] != "dead"]
        if not evr:
            continue
        est_vals = [r["est"] for r in evr if r["est"] is not None]
        n_est = len(est_vals)
        est_p = sum(r["est"] for r in evr
                    if r["est"] is not None and r.get("exp"))
        exp_p = sum(r["exp"] for r in evr
                    if r["est"] is not None and r.get("exp"))
        ends = []
        for r in evr:
            if r["meta"] is None:
                continue
            caps = [d for d in (r["meta"].cutoff, r["meta"].close_time,
                                r["meta"].program_end) if d is not None]
            if caps:
                ends.append(min(caps))
        reasons = Counter(r["reason"] for r in evr)
        reason = ", ".join(k for k, _n in reasons.most_common(2))
        sample = details.get(max(evr, key=lambda r: r["pool"])["ticker"], {})
        event_rows.append({
            "event": ev, "series": evr[0]["series"], "n": len(evr),
            "n_est": n_est, "pool": sum(r["pool"] for r in evr),
            "est": sum(est_vals) if est_vals else None,
            "exp": exp_p if exp_p > 0 else None,
            "yld": (est_p / exp_p) if exp_p > 0 else None,
            "partial": 0 < n_est < len(evr),
            "end": min(ends) if ends else None,
            "reason": reason,
            "fallback_title": str(sample.get("title") or ""),
        })
    # Jack 2026-08-12: rank by EST EARNINGS PER $ OF EXPOSURE (the modeled
    # ladder's collateral incl. pads, scaled by book turnover = 24h volume /
    # book depth capped 3x), THEN by est $/day. Rows without a yield estimate
    # fall below every yielded row (est, then pool, as before). The C/MIN
    # column (Jack 2026-08-04) is still displayed, just no longer the sort.
    event_rows.sort(key=lambda d: (
        -(d["yld"] if d.get("yld") is not None else -1),
        -(d["est"] if d["est"] is not None else -1),
        -d["pool"]))

    ctx = {
        "programs": len(programs),
        "pool_total": sum(i["dollars_per_day"] for i in programs.values()),
        "quoted": len(quoted), "quoted_events": len(quoted_events),
        "event_cap": imm.MAX_MARKETS,
        "unquoted_rows": len(rows),
        "books_read": books,
        "est_missed_total": sum(r["est"] for r in rows if r["est"] is not None),
        "unest_pool": sum(r["pool"] for r in rows if r["est"] is None),
        "reward_today": _f(status.get("reward_est_today")),
        "updated_at": status.get("updated_at", "?"),
        "heartbeat_stale": True,
    }
    try:
        age_min = (now_utc - datetime.strptime(
            status.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)).total_seconds() / 60.0
        ctx["heartbeat_stale"] = age_min > 30
    except ValueError:
        pass
    return event_rows, ctx


TD = 'padding:5px 10px;border:1px solid #ddd;text-align:right;'
TDL = 'padding:5px 10px;border:1px solid #ddd;text-align:left;'


def build_report(now_utc: datetime):
    """Returns (plain_text, html, subject)."""
    client = imm.build_client()
    bot = imm.IncentiveMarketMaker(client, live=False)
    event_rows, ctx = classify_and_estimate(client, bot, now_utc)

    deliberate = [d for d in event_rows
                  if d["reason"].startswith(("blocklisted", "frozen"))]
    candidates = [d for d in event_rows
                  if not d["reason"].startswith(("blocklisted", "frozen"))]
    show = [d for d in candidates
            if (d["est"] is not None and d["est"] >= MIN_SHOW_DOLLARS)
            or (d["est"] is None and d["pool"] >= 20)][:TOP_EVENTS]
    shown_events = {d["event"] for d in show}
    hidden = [d for d in candidates if d["event"] not in shown_events]
    hidden_est = sum(d["est"] or 0.0 for d in hidden)
    deliberate_show = sorted(deliberate,
                             key=lambda d: (-(d["est"] or 0), -d["pool"]))[:10]

    for d in show + deliberate_show:   # descriptions for both tables
        d["title"] = describe_event(
            d["event"], event_title(client, d["event"], d["fallback_title"]))

    today_et = now_utc.astimezone(imm.ET).date()
    headline = ctx["est_missed_total"]
    subject = f"IMM quote gaps {today_et} — est ${headline:,.0f}/day unquoted"

    def permin(d):
        return f"{d['est'] / 1440 * 100:.2f}" if d["est"] is not None else "—"

    def yld_str(d):
        return (f"{d['yld'] * 100:,.1f}%" if d.get("yld") is not None else "—")

    def exp_str(d):
        return f"{d['exp']:,.0f}" if d.get("exp") else "—"

    def est_str(d):
        if d["est"] is None:
            return "—"
        return f"{'>=' if d['partial'] else ''}{d['est']:,.2f}"

    # ---- plain text ---------------------------------------------------------
    lines = [f"IMM quote gaps — {today_et}", ""]
    lines.append(
        f"Est ${headline:,.2f}/day currently unquoted "
        f"(over {ctx['unquoted_rows']} paying markets not in the book; "
        f"{ctx['books_read']} books estimated).")
    lines.append(
        f"Quoting {ctx['quoted']} markets across {ctx['quoted_events']}/"
        f"{ctx['event_cap']} events; est reward today so far "
        f"${ctx['reward_today']:,.2f}. {ctx['programs']} program markets, "
        f"${ctx['pool_total']:,.0f}/day total pools.")
    if ctx["heartbeat_stale"]:
        lines.append(f"WARNING: bot heartbeat stale (updated_at "
                     f"{ctx['updated_at']}) — 'quoted' set may be old.")
    lines.append("")
    if show:
        lines.append("ranked by est $/day per $ of exposure, then est $/day")
        lines.append(f"{'YLD%/D':>7} {'C/MIN':>6} {'EST$/D':>8} {'EXPO$':>7} "
                     f"{'EVENT':<28} {'WHAT IT IS':<40} {'MKTS':>4} "
                     f"{'POOL$/D':>8}  WHY NOT QUOTED")
        for d in show:
            lines.append(
                f"{yld_str(d):>7} {permin(d):>6} {est_str(d):>8} {exp_str(d):>7} "
                f"{d['event'][:28]:<28} {(d['title'] or '?')[:40]:<40} "
                f"{d['n']:>4} {d['pool']:>8,.0f}  "
                f"{d['reason']} ({fmt_window(d['end'], now_utc)})")
    else:
        lines.append("No unquoted opportunities above the display floor.")
    if hidden:
        lines.append(f"...+{len(hidden)} smaller events below "
                     f"${MIN_SHOW_DOLLARS:.2f}/day (est ${hidden_est:,.2f}/day total).")
    if ctx["unest_pool"] > 0:
        lines.append(f"Unestimated (book-read budget): pools totalling "
                     f"${ctx['unest_pool']:,.0f}/day — est shown as '—'.")
    if deliberate_show:
        lines.append("")
        lines.append("DELIBERATELY OFF (blocklist/freeze) — for awareness:")
        for d in deliberate_show:
            lines.append(f"  {d['event'][:28]:<28} {(d['title'] or '?')[:40]:<40} "
                         f"pool {d['pool']:>7,.0f}/d est {est_str(d):>8} "
                         f"[{d['reason']}]")
    lines.append("")
    lines.append("Est = bot's own amended-rules estimator with its standard "
                 "at-ref ladder overlaid on the live book; assumes full-day "
                 "two-sided coverage and no competitor response. Partial "
                 "event estimates are marked '>='. Rows are independent "
                 "per-market estimates — quoting them all at once would hit "
                 "the event cap / collateral budget. YLD%/D = est $/day per "
                 "$ of exposure; EXPO$ = the modeled ladder's collateral "
                 "(incl. depth pads) scaled by book action, x(1 + "
                 "min(24h volume / book depth, 3)) — busy books fill resting "
                 "size, so a dollar resting there is more exposed.")
    text = "\n".join(lines)

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">']
    h.append(f'<div style="font-size:17px;font-weight:600">IMM quote gaps'
             f' <span style="color:#888;font-weight:400">— {today_et}</span></div>')
    h.append(f'<div style="font-size:24px;font-weight:800;margin:8px 0 2px">'
             f'Est <span style="color:#b45309">${headline:,.2f}/day</span> unquoted</div>')
    h.append(f'<div style="color:#555;margin-bottom:10px">'
             f'{ctx["unquoted_rows"]} paying markets not in the book '
             f'({ctx["books_read"]} books estimated) &nbsp;·&nbsp; '
             f'quoting <b>{ctx["quoted"]}</b> markets across '
             f'<b>{ctx["quoted_events"]}/{ctx["event_cap"]}</b> events &nbsp;·&nbsp; '
             f'est reward today ${ctx["reward_today"]:,.2f} &nbsp;·&nbsp; '
             f'{ctx["programs"]} program markets, ${ctx["pool_total"]:,.0f}/day pools</div>')
    if ctx["heartbeat_stale"]:
        h.append(f'<div style="color:#c0392b;font-weight:600;margin-bottom:8px">'
                 f'Bot heartbeat stale (updated_at {ctx["updated_at"]}) — '
                 f'"quoted" set may be old.</div>')
    if show:
        h.append('<div style="color:#555;font-size:13px;margin-bottom:4px">'
                 'ranked by est $/day per $ of exposure, then est $/day</div>')
        h.append('<table style="border-collapse:collapse">')
        h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
                 f'<td style="{TD}">YLD %/D</td>'
                 f'<td style="{TD}">C/MIN</td><td style="{TD}">EST $/D</td>'
                 f'<td style="{TD}">EXPO $</td>'
                 f'<td style="{TDL}">EVENT</td><td style="{TDL}">WHAT IT IS</td>'
                 f'<td style="{TD}">MKTS</td><td style="{TD}">POOL $/D</td>'
                 f'<td style="{TDL}">WHY NOT QUOTED</td></tr>')
        for i, d in enumerate(show):
            bg = "#fafafa" if i % 2 else "#fff"
            h.append(
                f'<tr style="background:{bg}">'
                f'<td style="{TD};font-weight:700;color:#b45309">{yld_str(d)}</td>'
                f'<td style="{TD}">{permin(d)}</td>'
                f'<td style="{TD};font-weight:600">{est_str(d)}</td>'
                f'<td style="{TD}">{exp_str(d)}</td>'
                f'<td style="{TDL}"><b>{d["event"]}</b></td>'
                f'<td style="{TDL}">{d["title"] or "?"}</td>'
                f'<td style="{TD}">{d["n"]}</td>'
                f'<td style="{TD}">{d["pool"]:,.0f}</td>'
                f'<td style="{TDL}">{d["reason"]}'
                f'<span style="color:#999"> · {fmt_window(d["end"], now_utc)}</span>'
                f'</td></tr>')
        h.append('</table>')
    else:
        h.append('<div>No unquoted opportunities above the display floor.</div>')
    notes = []
    if hidden:
        notes.append(f'+{len(hidden)} smaller events below '
                     f'${MIN_SHOW_DOLLARS:.2f}/day (est ${hidden_est:,.2f}/day total)')
    if ctx["unest_pool"] > 0:
        notes.append(f'unestimated pools (book budget): ${ctx["unest_pool"]:,.0f}/day')
    if notes:
        h.append(f'<div style="color:#888;font-size:12px;margin-top:6px">'
                 f'{" · ".join(notes)}</div>')
    if deliberate_show:
        h.append('<div style="font-size:15px;font-weight:600;margin:14px 0 4px">'
                 'Deliberately off (blocklist / freeze)</div>')
        h.append('<table style="border-collapse:collapse">')
        h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
                 f'<td style="{TDL}">EVENT</td><td style="{TDL}">WHAT IT IS</td>'
                 f'<td style="{TD}">POOL $/D</td><td style="{TD}">EST $/D</td>'
                 f'<td style="{TDL}">STATUS</td></tr>')
        for i, d in enumerate(deliberate_show):
            bg = "#fafafa" if i % 2 else "#fff"
            h.append(f'<tr style="background:{bg}">'
                     f'<td style="{TDL}">{d["event"]}</td>'
                     f'<td style="{TDL}">{d["title"] or "?"}</td>'
                     f'<td style="{TD}">{d["pool"]:,.0f}</td>'
                     f'<td style="{TD}">{est_str(d)}</td>'
                     f'<td style="{TDL}">{d["reason"]}</td></tr>')
        h.append('</table>')
    h.append('<div style="color:#777;font-size:12px;margin-top:12px;'
             'border-top:1px solid #eee;padding-top:8px">'
             'Est = the bot\'s own amended-rules estimator (reference-band '
             'pro-rata, tick-decay, exclusion coverage) with its standard '
             'at-ref ladder overlaid on the live book. Assumes full-day '
             'two-sided coverage, no competitor response. Partial event '
             'estimates are marked "&ge;". Rows are independent per-market '
             'estimates — quoting them all at once would hit the event cap / '
             'collateral budget. YLD %/D = est $/day per $ of exposure; '
             'EXPO $ = the modeled ladder\'s collateral (incl. depth pads) '
             'scaled by book action, &times;(1 + min(24h volume / book '
             'depth, 3)) — busy books fill resting size, so a dollar '
             'resting there is more exposed. Config mirrored from the live '
             'launcher env.</div>')
    h.append('</div>')
    return text, "".join(h), subject


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; do not write it")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker")
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    today_ct = now_utc.astimezone(imm.CT).date()
    marker = os.path.join(imm.STATUS_DIR, f"imm_quote_gaps_sent_{today_ct}.marker")

    if not (args.test or args.dry) and os.path.exists(marker):
        log(f"quote-gaps email already sent for {today_ct}; exiting")
        return 0

    attempts = 1 if (args.test or args.dry) else 8
    text = html = subject = None
    for attempt in range(1, attempts + 1):
        try:
            text, html, subject = build_report(now_utc)
            break
        except Exception as e:
            log(f"quote-gaps build attempt {attempt}/{attempts} failed: {e!r}")
            if attempt == attempts:
                log("giving up for today")
                return 1
            time.sleep(300)
    log("quote-gaps body:\n" + text)
    if args.dry:
        return 0

    alerter = imm.Alerter("IMM-GAPS", live=True)
    if not alerter.enabled:
        log("cannot send quote-gaps email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject, html=html)
        if ok:
            break
        log(f"quote-gaps send attempt {attempt}/{attempts} failed; retry in 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"quote-gaps send: {'ok' if ok else 'FAILED'}")
    if ok and not args.test:
        with open(marker, "w") as f:
            f.write(now_utc.isoformat())
        cutoff = today_ct - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR,
                                          "imm_quote_gaps_sent_*.marker")):
            name = os.path.basename(old)[len("imm_quote_gaps_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
