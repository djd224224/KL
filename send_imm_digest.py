#!/usr/bin/env python3
r"""
send_imm_digest.py — one clear morning email for the incentive-rewards MM bot,
structured like the crypto fleet digest (send_daily_digest.py).

Layout: headline estimated reward (the point of the bot) + P&L, then one row per
EVENT the bot is working — realized$, unrealized$, net position, and $ exposure —
sorted best to worst, with a TOTAL row, balance, capital-at-work, and a one-line
health check. Events with nothing going on are omitted.

Everything is the INCENTIVE BOT's own book, not the raw account: positions come
from the bot's persisted own-book (run-logs/incentive-mm/imm_state.json, which is
isolated from the user's manual trades and the other cloud bots by order-ownership
fill matching); realized P&L is replayed from the bot's own fills; unrealized marks
each open position to the current market mid. Reward figures and health come from
the bot's heartbeat (status_incentive_mm.json) and last stored daily summary.

Scheduled daily shortly after the bot's 6 AM ET summary roll. Idempotent via a
sent-marker; --test sends immediately and skips the marker.

Credentials: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD from the environment, falling
back to HKCU\Environment (works under Task Scheduler's stripped env).
"""

import argparse
import collections
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone


def _env_from_registry(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ""


for _v in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD"):
    if not os.environ.get(_v):
        _val = _env_from_registry(_v)
        if _val:
            os.environ[_v] = _val

# Import AFTER the env fixup so the module-level cred constants pick them up.
from incentive_mm import (CT, ET, STATUS_DIR, Alerter, PnlTracker,  # noqa: E402
                          build_client, log, market_cents)

STALE_AFTER_MINUTES = 30
STATE_PATH = os.path.join(STATUS_DIR, "imm_state.json")
STATUS_PATH = os.path.join(STATUS_DIR, "status_incentive_mm.json")
# 96 -> 168 (Jack 2026-08-03: "i do hold multi-week inventory"). 168h is the
# HARD ceiling for fill-based attribution: fills carry no client_order_id and
# the bot's our_order_ids map prunes at 7 days, so nothing older can be
# attributed to the bot at all. Multi-week positions are therefore handled by
# the bot's PERSISTED realized_lifetime counter (which captures settlements of
# arbitrarily old holds) plus own-book MTM — not by this window.
FILL_LOOKBACK_HOURS = int(os.environ.get("IMM_DIGEST_FILL_HOURS", 168))
# NOTE on reconciling estimates vs Kalshi credits (Jack 2026-07-21): a naive
# same-day comparison is WRONG — programs run multiple days and a day's
# accrual can pay out across the program's life, so an "est $536 vs paid $300"
# single-day ratio understates realization. A short-lived 0.56 "expected paid"
# haircut was removed for exactly that reason; the digest reports the raw
# estimate only.


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def _short_event(event_ticker: str) -> str:
    """Compact label for the table: drop the leading 'KX'."""
    return event_ticker[2:] if event_ticker.startswith("KX") else event_ticker


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def own_book(state: dict):
    """(own_pos, own_avg) in signed contracts and entry-cost cents."""
    pos = {t: _f(v) for t, v in (state.get("own_pos") or {}).items() if abs(_f(v)) > 0.01}
    avg = {t: _f(v) for t, v in (state.get("own_avg") or {}).items()}
    return pos, avg


def current_mids(client, tickers):
    """(mids, results): ticker -> mid YES price in CENTS (bid/ask mid, else
    last), and ticker -> settlement result ('yes'/'no') for settled markets
    so the caller can book settlement P&L."""
    mids, results = {}, {}
    tickers = list(tickers)
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            resp = client.get_markets(tickers=",".join(chunk), limit=len(chunk))
        except Exception as e:
            log(f"! mids read failed for a chunk: {e}")
            continue
        for m in (resp.get("markets") or []):
            t = m.get("ticker", "")
            res = str(m.get("result") or "").lower()
            if res in ("yes", "no"):
                results[t] = res
            bid, ask = market_cents(m, "yes_bid"), market_cents(m, "yes_ask")
            if bid and ask:
                mids[t] = (bid + ask) / 2.0
            else:
                lp = market_cents(m, "last_price")
                if lp:
                    mids[t] = float(lp)
    return mids, results


def replay_realized(client, our_ids: set):
    """(realized, positions, avg_cost) replayed from the bot's OWN fills only
    (matched by order id). Isolated from the user's manual fills and the other
    cloud bots. Positions/avg are returned so the caller can book SETTLEMENT
    P&L: a position that settled is gone from the persisted own-book, so
    realized-from-fills alone silently dropped the entire settlement result
    (audit finding 2026-07-23, fixed 2026-08-03 — it hid $920 of temp
    settlement losses on 8/3). Best-effort — returns empties on read failure."""
    pnl = PnlTracker()
    cursor = None
    min_ts = int(time.time()) - FILL_LOOKBACK_HOURS * 3600
    seen = set()
    try:
        for _page in range(400):   # page to exhaustion; fleet shares this stream
            resp = client.get_fills(min_ts=min_ts, limit=200, cursor=cursor)
            batch = resp.get("fills") or []
            for f in batch:
                fid = f.get("fill_id") or f.get("trade_id") or ""
                if f.get("order_id") not in our_ids or fid in seen:
                    continue
                seen.add(fid)
                side, action = f.get("side"), f.get("action")
                count = _f(f.get("count_fp") or f.get("count"))
                px = f.get("yes_price_dollars")
                px_c = _f(px) * 100 if px is not None else _f(f.get("yes_price"))
                if side in ("yes", "no") and action in ("buy", "sell") and count > 0:
                    pnl.on_fill(f.get("ticker", "?"), side, action, count, px_c)
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
    except Exception as e:
        log(f"! fill replay failed ({e}); realized shown as 0")
        return {}, {}, {}
    return dict(pnl.realized), dict(pnl.pos), dict(pnl.avg)


def event_rows(client):
    """Per-event rollup of the bot's own book. Returns (rows, totals, resting)."""
    state = load_json(STATE_PATH)
    pos, avg = own_book(state)
    our_ids = set(state.get("our_order_ids") or {})

    # Resting imm- orders: which events are actively quoted + capital deployed.
    resting_by_event = {}
    resting_collateral = 0.0
    try:
        cursor = None
        for _page in range(40):
            resp = client.get_orders(status="resting", limit=200, cursor=cursor)
            batch = resp.get("orders") or []
            for o in batch:
                if o.get("status") != "resting" or \
                        not str(o.get("client_order_id", "")).startswith("imm-"):
                    continue
                t = o.get("ticker", "")
                px = market_cents(o, "yes_price")
                if px is None and o.get("yes_price_dollars") is not None:
                    px = round(_f(o.get("yes_price_dollars")) * 100)
                rem = _f(o.get("remaining_count_fp") or o.get("remaining_count") or o.get("count"))
                side = o.get("side")
                # collateral: a YES buy reserves px; a NO buy reserves 100-price
                if side == "no":
                    npx = market_cents(o, "no_price")
                    if npx is None and o.get("no_price_dollars") is not None:
                        npx = round(_f(o.get("no_price_dollars")) * 100)
                    reserve = (npx or 0)
                else:
                    reserve = (px or 0)
                resting_collateral += reserve / 100.0 * rem
                ev = _event_of(t)
                resting_by_event[ev] = resting_by_event.get(ev, 0) + 1
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
    except Exception as e:
        log(f"! resting-order read failed: {e}")

    # P&L now comes from pnl_windows() (raw_pnl_for_fills handles settlement
    # booking per window); this function only reports the CURRENT open book
    # and resting quotes, so it no longer replays fills.
    realized = {}
    mids, _results = current_mids(client, set(pos))

    events = {}
    for t, p in pos.items():
        ev = _event_of(t)
        d = events.setdefault(ev, {"realized": 0.0, "unrealized": 0.0,
                                   "net_pos": 0.0, "exposure": 0.0, "mkts": 0})
        d["net_pos"] += p
        d["mkts"] += 1
        a = avg.get(t, 0.0)
        mid = mids.get(t)
        if mid is not None:
            d["unrealized"] += p * (mid - a) / 100.0
        d["exposure"] += (p * a if p > 0 else -p * (100 - a)) / 100.0
    for t, r in realized.items():
        ev = _event_of(t)
        events.setdefault(ev, {"realized": 0.0, "unrealized": 0.0,
                               "net_pos": 0.0, "exposure": 0.0, "mkts": 0})
        events[ev]["realized"] += r

    rows = []
    tot = {"realized": 0.0, "unrealized": 0.0, "net_pos": 0.0, "exposure": 0.0}
    for ev, d in events.items():
        for k in tot:
            tot[k] += d[k]
        d["pnl"] = d["realized"] + d["unrealized"]
        d["quoted"] = resting_by_event.get(ev, 0)
        if (abs(d["realized"]) > 0.005 or abs(d["unrealized"]) > 0.005
                or abs(d["net_pos"]) > 0.5 or d["quoted"] > 0):
            rows.append((ev, d))
    rows.sort(key=lambda r: -r[1]["pnl"])
    return rows, tot, {"collateral": resting_collateral,
                       "orders": sum(resting_by_event.values()),
                       "events": len(resting_by_event)}


# Rewards actually CREDITED by Kalshi, from the account statement (Kalshi
# exposes no credits endpoint — verified 2026-08-03 across 8 paths). Update
# from the statement; falls back to the bot's estimate when unset.
REWARDS_CREDITED = os.environ.get("IMM_REWARDS_CREDITED", "")
# Current-month credited, also from the statement. Kalshi pays a program at its
# PERIOD END, so a month in progress is always credited LESS than it has
# accrued — without this the digest's estimate looks simply wrong.
REWARDS_CREDITED_MTD = os.environ.get("IMM_REWARDS_CREDITED_MTD", "")
# Credited reward that is NOT IMM's. Kalshi pays on the ACCOUNT's aggregate
# book presence — single user_id, no per-strategy segregation, and no
# per-strategy reward endpoint exists. reward_est_lifetime starts at $0 on
# 2026-07-11 while the account's first fill is 2026-02-09, so 152 of the 176
# credited days have no estimate term at all; the KXHIGH weather bot rested on
# programmed markets that whole time. Measured 2026-08-04 by replaying that
# bot's reconstructed ladder against 8,389 in-window book snapshots: ~$1,500
# (band $700-$2,800). Comparing account-level credit to an IMM-only estimate
# overstates realization and is what made "prior periods" read 1.18x.
REWARDS_NON_IMM = os.environ.get("IMM_REWARDS_NON_IMM", "1500")
DAILY_PNL_PATH = os.path.join(STATUS_DIR, "daily_pnl.json")


def fetch_own_fills(client, our_ids: set, hours: int) -> list:
    """The bot's own fills over `hours`, newest-first pagination exhausted.
    Attribution is by order id — the account is shared with the crypto fleet,
    the cloud bots and Jack's manual trading."""
    out, cursor, seen = [], None, set()
    min_ts = int(time.time()) - hours * 3600
    try:
        for _page in range(600):
            resp = client.get_fills(min_ts=min_ts, limit=200, cursor=cursor)
            batch = resp.get("fills") or []
            for f in batch:
                fid = f.get("fill_id") or f.get("trade_id") or ""
                if f.get("order_id") not in our_ids or fid in seen:
                    continue
                seen.add(fid)
                out.append(f)
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
    except Exception as e:
        log(f"! fill fetch failed ({e})")
    return out


def _yes_delta_and_price(f):
    """(yes_delta_contracts, price_in_yes_cents, fee). A NO buy is a SHORT
    yes position priced at yes_price_dollars — the sign trap in this data."""
    cnt = _f(f.get("count_fp") or f.get("count"))
    px = f.get("yes_price_dollars")
    pxc = _f(px) * 100 if px is not None else _f(f.get("yes_price"))
    side, action = f.get("side"), f.get("action")
    if (side, action) == ("yes", "buy"):
        yd = cnt
    elif (side, action) == ("yes", "sell"):
        yd = -cnt
    elif (side, action) == ("no", "buy"):
        yd = -cnt
    else:
        yd = cnt
    return yd, pxc, _f(f.get("fee_cost"))


def raw_pnl_for_fills(client, fills, mids=None, results=None):
    """RAW (trading-only) P&L for a set of fills: realized from offsetting
    fills + settlement on the residual position + MTM on what is still open,
    minus fees. Returns (totals, per_event, per_ticker_positions)."""
    pnl = PnlTracker()
    fees = 0.0
    for f in sorted(fills, key=lambda x: x.get("ts") or 0):
        cnt = _f(f.get("count_fp") or f.get("count"))
        side, action = f.get("side"), f.get("action")
        px = f.get("yes_price_dollars")
        pxc = _f(px) * 100 if px is not None else _f(f.get("yes_price"))
        if side in ("yes", "no") and action in ("buy", "sell") and cnt > 0:
            pnl.on_fill(f.get("ticker", "?"), side, action, cnt, pxc)
        fees += _f(f.get("fee_cost"))
    tickers = set(pnl.pos) | set(pnl.realized)
    if mids is None or results is None:
        mids, results = current_mids(client, tickers)
    settle, unreal = {}, {}
    for t, p in pnl.pos.items():
        if abs(p) < 0.01:
            continue
        a = pnl.avg.get(t, 0.0)
        if t in results:                       # settled: book the real outcome
            val = 100.0 if results[t] == "yes" else 0.0
            settle[t] = p * (val - a) / 100.0
        elif mids.get(t) is not None:          # still open: mark to mid
            unreal[t] = p * (mids[t] - a) / 100.0
    per_event = collections.defaultdict(
        lambda: {"realized": 0.0, "settle": 0.0, "unrealized": 0.0,
                 "net_pos": 0.0, "mkts": set(), "contracts": 0.0})
    for t, v in pnl.realized.items():
        per_event[_event_of(t)]["realized"] += v
        per_event[_event_of(t)]["mkts"].add(t)
    for t, v in settle.items():
        per_event[_event_of(t)]["settle"] += v
        per_event[_event_of(t)]["mkts"].add(t)
    for t, v in unreal.items():
        per_event[_event_of(t)]["unrealized"] += v
        per_event[_event_of(t)]["mkts"].add(t)
    for t, p in pnl.pos.items():
        per_event[_event_of(t)]["net_pos"] += p
    for f in fills:
        per_event[_event_of(f.get("ticker", "?"))]["contracts"] += \
            _f(f.get("count_fp") or f.get("count"))
    totals = {
        "realized": sum(pnl.realized.values()),
        "settle": sum(settle.values()),
        "unrealized": sum(unreal.values()),
        "fees": fees,
        "contracts": sum(_f(f.get("count_fp") or f.get("count")) for f in fills),
        "open_markets": sum(1 for t, p in pnl.pos.items()
                            if abs(p) >= 0.01 and t not in results),
    }
    totals["raw"] = (totals["realized"] + totals["settle"]
                     + totals["unrealized"] - fees)
    return totals, per_event, pnl


def daily_series(client, fills, mids, results, state, days=60):
    """[(date_et, raw, contracts, fills_n)] for the last `days` ET days.
    A fill is attributed to the ET day it occurred; the P&L of the position
    it leaves behind is evaluated at settlement/mark. This is a per-day
    TRADING result, so a day's number can move until its positions settle."""
    by_day = collections.defaultdict(list)
    for f in fills:
        ts = f.get("ts")
        if not ts:
            continue
        day = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET).date()
        by_day[day].append(f)
    hist = {str(k): _f(v) for k, v in (state.get("reward_history") or {}).items()}
    # Backfilled per-day RAW P&L (imm_backfill_daily_pnl.py) reaches back to
    # the bot's first fill, well past the fill-attribution window available
    # live. Prefer it for any day it covers.
    backfill = load_json(DAILY_PNL_PATH)
    out = []
    today = datetime.now(timezone.utc).astimezone(ET).date()
    # Every prior day that EARNED rewards (Jack 2026-08-03) — union of the
    # reward-history days and the days we have attributable fills for. Fill
    # attribution only reaches back FILL_LOOKBACK_HOURS, so older days show
    # their reward with raw P&L marked unavailable rather than a false 0.
    days_set = set(hist)
    for day in by_day:
        days_set.add(day.isoformat())
    for key in sorted(days_set):
        try:
            day = datetime.strptime(key, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= today:
            continue                       # only PRIOR days
        reward = hist.get(key)
        dfills = by_day.get(day, [])
        bf = backfill.get(key)
        if bf:                              # authoritative: full-history rebuild
            raw = _f(bf.get("raw"))
            contracts, nf = _f(bf.get("contracts")), int(bf.get("fills") or 0)
        elif dfills:
            tot, _ev, _p = raw_pnl_for_fills(client, dfills, mids, results)
            raw, contracts, nf = tot["raw"], tot["contracts"], len(dfills)
        elif reward is not None:
            raw, contracts, nf = None, 0.0, 0
        else:
            continue
        out.append((day, raw, reward, contracts, nf))
    for key, bf in backfill.items():        # days with P&L but no reward record
        if key in hist:
            continue
        try:
            day = datetime.strptime(key, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= today or any(r[0] == day for r in out):
            continue
        out.append((day, _f(bf.get("raw")), None, _f(bf.get("contracts")),
                    int(bf.get("fills") or 0)))
    out.sort(key=lambda r: r[0])
    return out


def pnl_windows(client, state, our_ids, fills, mids, results, reward_lifetime):
    """RAW (trading-only) and NET (raw + rewards) for past day / past week /
    lifetime. Lifetime RAW comes from the bot's PERSISTED realized_lifetime
    (the only source that survives restarts AND captures settlements of
    multi-week holds) plus current own-book MTM."""
    now = time.time()
    day_fills = [f for f in fills if _f(f.get("ts")) >= now - 86400]
    week_fills = fills
    day_tot, day_ev, _ = raw_pnl_for_fills(client, day_fills, mids, results)
    week_tot, _, _ = raw_pnl_for_fills(client, week_fills, mids, results)

    # lifetime: persisted realized + MTM on the persisted own-book
    pos, avg = own_book(state)
    life_realized = _f(state.get("realized_lifetime"))
    life_unreal = 0.0
    for t, p in pos.items():
        m = mids.get(t)
        if m is not None:
            life_unreal += p * (m - avg.get(t, 0.0)) / 100.0
    life_raw = life_realized + life_unreal

    credited = _f(REWARDS_CREDITED) if REWARDS_CREDITED else None
    rew_life = credited if credited else reward_lifetime
    rew_basis = "credited (statement)" if credited else "bot estimate"
    # Sub-window rewards come from the persisted per-day history; days before
    # the 2026-08-03 fix are missing and are reported as such rather than
    # silently summed to a wrong number.
    hist = {str(k): _f(v) for k, v in (state.get("reward_history") or {}).items()}
    today_et = datetime.now(timezone.utc).astimezone(ET).date()
    day_key = (today_et - timedelta(days=1)).isoformat()
    rew_day = hist.get(day_key)
    week_keys = [(today_et - timedelta(days=i)).isoformat() for i in range(1, 8)]
    have = [hist[k] for k in week_keys if k in hist]
    rew_week = sum(have) if have else None
    return {
        "day": {"raw": day_tot["raw"], "reward": rew_day, "detail": day_tot,
                "events": day_ev, "have_reward": rew_day is not None,
                "reward_days": 1 if rew_day is not None else 0},
        "week": {"raw": week_tot["raw"], "reward": rew_week, "detail": week_tot,
                 "have_reward": rew_week is not None, "reward_days": len(have)},
        "life": {"raw": life_raw, "reward": rew_life, "have_reward": True,
                 "realized": life_realized, "unrealized": life_unreal,
                 "basis": rew_basis},
    }


def last_full_day_reward(state: dict, status: dict, today_ct):
    """(amount, label) for the headline. Source of truth is the persisted
    reward_history written at each 5am-CT roll (2026-08-03): the previous
    sources — the in-process reward_est_today counter and the summary_body
    text — both reset on every restart, and this bot restarts many times a
    day, so the digest was reporting a small fraction of reality ($45.81
    against a measured $959.47 on 8/3). Falls back to the old sources only
    when no history exists (first run after this fix)."""
    hist = {str(k): _f(v) for k, v in (state.get("reward_history") or {}).items()}
    for back in (1, 2):
        key = (today_ct - timedelta(days=back)).isoformat()
        if key in hist:
            return hist[key], key
    if hist:
        key = sorted(hist)[-1]
        return hist[key], key
    body = status.get("summary_body") or ""
    m = re.search(r"est reward today \$([\-\d.,]+)", body)
    if m:
        return _f(m.group(1).replace(",", "")), "last summary"
    return _f(status.get("reward_est_today")), "today so far (partial)"


def status_summary(status: dict) -> dict:
    """Activity figures from the last stored daily summary (yesterday's
    completed roll), falling back to the live heartbeat counters. Reward is
    supplied separately by last_full_day_reward()."""
    body = status.get("summary_body") or ""

    def grab(pat, default=0.0):
        m = re.search(pat, body)
        return _f(m.group(1).replace(",", "")) if m else default

    return {
        "reward": grab(r"est reward today \$([\-\d.,]+)",
                       _f(status.get("reward_est_today"))),
        "reward_lifetime": _f(status.get("reward_est_lifetime")),
        "contract_min": grab(r"\(([\d,]+) contract-min",
                             _f(status.get("contract_minutes_today"))),
        "efficiency": grab(r"([\d.]+)c/1k-contract-min",
                          _f(status.get("cents_per_1k_contract_min"))),
        "fills": grab(r"fills (\d+)", _f(status.get("fills_today"))),
        "errors": grab(r"errs (\d+)", _f(status.get("errors_today"))),
        "summary_date": status.get("summary_date", ""),
    }


def health_line(status: dict, ss: dict) -> str:
    now = datetime.now(timezone.utc)
    problems = []
    try:
        age = (now - datetime.strptime(status.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc)).total_seconds() / 60.0
    except ValueError:
        age = float("inf")
    alive = age <= STALE_AFTER_MINUTES
    if not alive:
        problems.append(f"HEARTBEAT STALE ({status.get('updated_at', '?')})")
    if _f(status.get("halted_until")) > time.time():
        problems.append("DAILY-LOSS HALT active")
    standoff = status.get("manual_standoff") or []
    line = f"Bot: {'alive' if alive else 'DOWN'}, mode {status.get('mode', '?')}, " \
           f"{int(ss['errors'])} errors in last summary"
    if standoff:
        line += f" | {len(standoff)} market(s) yielded to manual/other bots"
    if problems:
        line += " | " + "; ".join(problems)
    return line


def _pnl_span(v: float, decimals: int = 2) -> str:
    color = "#0a7a2f" if v > 0.005 else ("#c0392b" if v < -0.005 else "#777")
    return f'<span style="color:{color}">{v:+,.{decimals}f}</span>'


TD = 'padding:5px 12px;border:1px solid #ddd;text-align:right;'
TDL = 'padding:5px 12px;border:1px solid #ddd;text-align:left;'


def rain_dir_section(client):
    """(text_lines, html) for the rain-directional ledger — settled P&L,
    open MTM, hit rate (Jack 2026-07-28: 'make sure we can see how it
    performs'). None when no ledger exists yet."""
    import csv as _csv
    ledger = os.path.join(os.path.dirname(STATUS_PATH), "rain_directional_ledger.csv")
    if not os.path.exists(ledger):
        return None
    try:
        with open(ledger, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
    except OSError:
        return None
    if not rows:
        return None
    tickers = sorted({r["ticker"] for r in rows})
    markets = {}
    try:
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            resp = client.get_markets(tickers=",".join(chunk), limit=len(chunk))
            for m in resp.get("markets") or []:
                markets[m.get("ticker", "")] = m
    except Exception:
        pass
    settled = wins = 0
    pnl_settled = mtm_open = 0.0
    open_n = 0
    for r in rows:
        n = _f(r["contracts"]); px = _f(r["price_cents"]); side = r["take_side"]
        m = markets.get(r["ticker"], {})
        result = m.get("result") or ""
        if result in ("yes", "no"):
            settled += 1
            win = result == side
            wins += 1 if win else 0
            pnl_settled += ((100 - px) if win else -px) / 100.0 * n
        else:
            open_n += 1
            try:
                bid = _f(m.get("yes_bid_dollars")) * 100
                ask = _f(m.get("yes_ask_dollars")) * 100
                if bid and ask:
                    mid = (bid + ask) / 2
                    mark = mid if side == "yes" else 100 - mid
                    mtm_open += (mark - px) / 100.0 * n
            except Exception:
                pass
    hit = f"{100 * wins / settled:.0f}%" if settled else "—"
    text = [f"RAIN DIRECTIONAL: {len(rows)} bets ({settled} settled, hit {hit}) | "
            f"settled P&L {pnl_settled:+.2f} | open {open_n} MTM {mtm_open:+.2f} | "
            f"total {pnl_settled + mtm_open:+.2f}"]
    html = (f'<div style="font-size:15px;font-weight:600;margin:10px 0 2px">'
            f'Rain directional (NWS-vs-book)</div>'
            f'<div style="color:#555;margin-bottom:10px">'
            f'{len(rows)} bets &nbsp;·&nbsp; {settled} settled, hit <b>{hit}</b>'
            f' &nbsp;·&nbsp; settled {_pnl_span(pnl_settled)}'
            f' &nbsp;·&nbsp; {open_n} open, MTM {_pnl_span(mtm_open)}'
            f' &nbsp;·&nbsp; total {_pnl_span(pnl_settled + mtm_open)}</div>')
    return text, html


def build_digest(now_utc: datetime):
    """Returns (plain_text, html)."""
    today_ct = now_utc.astimezone(CT).date()
    client = build_client()
    status = load_json(STATUS_PATH)
    state = load_json(STATE_PATH)
    ss = status_summary(status)
    reward_amt, reward_label = last_full_day_reward(state, status, today_ct)
    ss["reward"] = reward_amt
    ss["reward_label"] = reward_label
    our_ids = set(state.get("our_order_ids") or {})

    # One fill pull + one market read drives every window below.
    fills = fetch_own_fills(client, our_ids, FILL_LOOKBACK_HOURS)
    pos, avg = own_book(state)
    touched = {f.get("ticker", "") for f in fills} | set(pos)
    mids, results = current_mids(client, touched)
    w = pnl_windows(client, state, our_ids, fills, mids, results,
                    ss["reward_lifetime"])
    series = daily_series(client, fills, mids, results, state)
    rows, tot, resting = event_rows(client)     # open book + resting quotes

    try:
        bal_str = "${:,.2f}".format(_f(client.get_balance().get("balance_dollars")))
    except Exception:
        bal_str = "?"
    health = health_line(status, ss)

    def net_of(k):
        r = w[k]["reward"]
        return (w[k]["raw"] + r) if r is not None else None

    def money(v, dash="n/a"):
        return "{:+,.2f}".format(v) if v is not None else dash

    d = w["day"]["detail"]
    # est-vs-credited, split at the month boundary: the completed prior period
    # is the only fair test of the estimator; the current month is dominated by
    # unpaid in-flight programs.
    recon = []
    cred_life = _f(REWARDS_CREDITED) if REWARDS_CREDITED else None
    cred_mtd = _f(REWARDS_CREDITED_MTD) if REWARDS_CREDITED_MTD else None
    if cred_life is not None:
        est_life = ss["reward_lifetime"]
        non_imm = _f(REWARDS_NON_IMM)
        # ONLY the lifetime, IMM-attributable row is reported. The month/prior
        # split was removed 2026-08-04: it is not two measurements but one
        # measurement and a subtraction, so every month-boundary error lands
        # entirely on "prior" and inverted its ratio. The month row was worse
        # still — its denominator holds accrual sitting in programs that have
        # not ended, so it cannot be credited yet, and the statement's "as of"
        # instant is unknown; across plausible cut dates it spanned 0.39x-5.9x.
        recon.append(("account credited", est_life, cred_life))
        if non_imm:
            recon.append(("less non-IMM (other strategies, pre-IMM days)",
                          0.0, -non_imm))
            recon.append(("IMM-attributable", est_life, cred_life - non_imm))
    ev_rows = sorted(w["day"]["events"].items(),
                     key=lambda kv: -(kv[1]["realized"] + kv[1]["settle"]
                                      + kv[1]["unrealized"]))

    # ---- plain text ---------------------------------------------------------
    L = ["Kalshi incentive MM \u2014 {}".format(today_ct), ""]
    L.append("P&L  (RAW = trading only; NET = RAW + incentive rewards)")
    L.append("{:10s} {:>11s} {:>11s} {:>11s}".format(
        "WINDOW", "RAW$", "REWARD$", "NET$"))
    for key, lbl in (("day", "past day"), ("week", "past week"),
                     ("life", "lifetime")):
        L.append("{:10s} {:>11s} {:>11s} {:>11s}".format(
            lbl, money(w[key]["raw"]), money(w[key]["reward"]),
            money(net_of(key))))
    if w["week"]["reward"] is not None and w["week"]["reward_days"] < 7:
        L.append("  (week reward covers {}/7 days \u2014 daily reward history "
                 "began 2026-08-03)".format(w["week"]["reward_days"]))
    if w["day"]["reward"] is None:
        L.append("  (past-day reward pending the next 5am-CT roll)")
    L.append("  lifetime reward basis: {}; bot estimate ${:,.2f}".format(
        w["life"]["basis"], ss["reward_lifetime"]))
    if recon:
        L.append("")
        L.append("REWARD RECONCILIATION (estimate vs actually credited)")
        for lbl, est, cred in recon:
            ratio = ("{:.2f}x".format(cred / est) if est else "")
            L.append("  {:44s} est ${:>10,.2f}   credited ${:>10,.2f}   {}"
                     .format(lbl, est, cred, ratio))
        L.append("  Rewards are paid on the ACCOUNT's book presence, not per "
                 "strategy, and the")
        L.append("  estimate only starts 2026-07-11 — so account credit "
                 "includes reward IMM never")
        L.append("  estimated. IMM-attributable is the like-for-like row.")
        L.append("  NOTE: credited-to-date understates IMM because accrual in "
                 "programs that have")
        L.append("  not ended yet cannot be paid; on a fully-settled basis the "
                 "ratio is ~1.0x.")
        L.append("  Daily reward figures below are ESTIMATES.")
    L.append("")
    L.append("PAST DAY detail: realized {:+,.2f} | settled {:+,.2f} | open MTM "
             "{:+,.2f} | fees {:+,.2f} | {:,.0f} contracts filled".format(
                 d["realized"], d["settle"], d["unrealized"], -d["fees"],
                 d["contracts"]))
    L.append("Open book: net {:+,.0f} contracts, exposure ${:,.2f} | resting "
             "${:,.2f} ({} orders / {} events) | balance {}".format(
                 tot["net_pos"], tot["exposure"], resting["collateral"],
                 resting["orders"], resting["events"], bal_str))
    L.append("")
    L.append("DAILY P&L — every prior day that earned (raw; a day moves until "
             "its positions settle)")
    L.append("{:12s} {:>11s} {:>11s} {:>11s} {:>10s}".format(
        "DATE", "RAW$", "REWARD$", "NET$", "CONTRACTS"))
    d_raw = d_rew = 0.0
    for day, raw, reward, contracts, nf in series:
        net = (raw + reward) if (raw is not None and reward is not None) else None
        if raw is not None:
            d_raw += raw
        if reward is not None:
            d_rew += reward
        L.append("{:12s} {:>11s} {:>11s} {:>11s} {:>10,.0f}".format(
            day.isoformat(),
            "{:+,.2f}".format(raw) if raw is not None else "n/a",
            "{:+,.2f}".format(reward) if reward is not None else "n/a",
            "{:+,.2f}".format(net) if net is not None else "n/a",
            contracts))
    L.append("{:12s} {:>11s} {:>11s} {:>11s}".format(
        "TOTAL", "{:+,.2f}".format(d_raw), "{:+,.2f}".format(d_rew),
        "{:+,.2f}".format(d_raw + d_rew)))
    L.append("  (RAW shows n/a for days older than the {}h fill-attribution "
             "window)".format(FILL_LOOKBACK_HOURS))
    L.append("")
    L.append("EVENTS TRADED IN THE PAST DAY ({})".format(len(ev_rows)))
    if ev_rows:
        L.append("{:28s} {:>9s} {:>9s} {:>9s} {:>9s} {:>7s} {:>5s}".format(
            "EVENT", "P&L$", "REAL$", "SETTLE$", "MTM$", "CTS", "MKTS"))
        e_tot = {"realized": 0.0, "settle": 0.0, "unrealized": 0.0,
                 "contracts": 0.0, "mkts": 0}
        for ev, e in ev_rows:
            tot_e = e["realized"] + e["settle"] + e["unrealized"]
            for k in ("realized", "settle", "unrealized", "contracts"):
                e_tot[k] += e[k]
            e_tot["mkts"] += len(e["mkts"])
            L.append("{:28s} {:>+9.2f} {:>+9.2f} {:>+9.2f} {:>+9.2f} {:>7,.0f} "
                     "{:>5d}".format(_short_event(ev)[:28], tot_e, e["realized"],
                                     e["settle"], e["unrealized"],
                                     e["contracts"], len(e["mkts"])))
        L.append("{:28s} {:>+9.2f} {:>+9.2f} {:>+9.2f} {:>+9.2f} {:>7,.0f} "
                 "{:>5d}".format(
                     "TOTAL", e_tot["realized"] + e_tot["settle"]
                     + e_tot["unrealized"], e_tot["realized"], e_tot["settle"],
                     e_tot["unrealized"], e_tot["contracts"], e_tot["mkts"]))
    else:
        L.append("  (no fills in the past 24h)")
    L.append("")
    L.append(health)
    text = "\n".join(L)

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">']
    h.append('<div style="font-size:17px;font-weight:600">Kalshi incentive MM'
             ' <span style="color:#888;font-weight:400">\u2014 {}</span></div>'
             .format(today_ct))
    nl = net_of("life")
    h.append('<div style="font-size:24px;font-weight:800;margin:8px 0 2px">'
             'Lifetime net: {}<span style="font-size:13px;font-weight:400;'
             'color:#999"> &nbsp;= trading {:+,.2f} + rewards {:,.2f}</span>'
             '</div>'.format(_pnl_span(nl) if nl is not None else "n/a",
                             w["life"]["raw"], w["life"]["reward"]))
    h.append('<table style="border-collapse:collapse;margin:10px 0">')
    h.append('<tr style="background:#f0f0f0;font-weight:600">'
             '<td style="{0}">WINDOW</td><td style="{1}">RAW (trading)</td>'
             '<td style="{1}">REWARD</td><td style="{1}">NET</td></tr>'
             .format(TDL, TD))
    for i, (key, lbl) in enumerate((("day", "Past day"), ("week", "Past week"),
                                    ("life", "Lifetime"))):
        bg = "#fafafa" if i % 2 else "#fff"
        r = w[key]["reward"]
        n = net_of(key)
        h.append('<tr style="background:{0}"><td style="{1}">{2}</td>'
                 '<td style="{3}">{4}</td><td style="{3}">{5}</td>'
                 '<td style="{3};font-weight:700">{6}</td></tr>'.format(
                     bg, TDL, lbl, TD, _pnl_span(w[key]["raw"]),
                     money(r), _pnl_span(n) if n is not None else "n/a"))
    h.append("</table>")
    notes = ["lifetime reward basis: <b>{}</b> (bot estimate ${:,.2f})".format(
        w["life"]["basis"], ss["reward_lifetime"])]
    if w["week"]["reward"] is not None and w["week"]["reward_days"] < 7:
        notes.append("week reward covers {}/7 days (daily history began "
                     "2026-08-03)".format(w["week"]["reward_days"]))
    if w["day"]["reward"] is None:
        notes.append("past-day reward pending the next 5am-CT roll")
    h.append('<div style="color:#888;font-size:12px;margin-bottom:10px">{}</div>'
             .format(" &nbsp;\u00b7&nbsp; ".join(notes)))
    if recon:
        h.append('<div style="font-size:15px;font-weight:600;margin:10px 0 4px">'
                 'Reward reconciliation (estimate vs credited)</div>')
        h.append('<table style="border-collapse:collapse">')
        h.append('<tr style="background:#f0f0f0;font-weight:600">'
                 '<td style="{0}">PERIOD</td><td style="{1}">ESTIMATE</td>'
                 '<td style="{1}">CREDITED</td><td style="{1}">REALIZATION</td>'
                 '</tr>'.format(TDL, TD))
        for i, (lbl, est, cred) in enumerate(recon):
            bg = "#fafafa" if i % 2 else "#fff"
            ratio = ("{:.2f}x".format(cred / est) if est else "")
            h.append('<tr style="background:{0}"><td style="{1}">{2}</td>'
                     '<td style="{3}">${4:,.2f}</td><td style="{3}">${5:,.2f}</td>'
                     '<td style="{3};font-weight:600">{6}</td></tr>'.format(
                         bg, TDL, lbl, TD, est, cred, ratio))
        h.append("</table>")
        h.append('<div style="color:#888;font-size:12px;margin:4px 0 10px">'
                 "Rewards are paid on the ACCOUNT's book presence, not per "
                 'strategy, and the estimate only starts 2026-07-11 &mdash; so '
                 'account credit includes reward IMM never estimated. '
                 '<b>IMM-attributable</b> is the like-for-like row. Credited-'
                 'to-date still understates IMM because accrual in programs '
                 'that have not ended cannot be paid yet; fully settled the '
                 'ratio is ~1.0x. Daily reward figures below are ESTIMATES.'
                 '</div>')
    h.append('<div style="color:#555;margin-bottom:12px">Past day: realized {} '
             '&nbsp;\u00b7&nbsp; settled {} &nbsp;\u00b7&nbsp; open MTM {} '
             '&nbsp;\u00b7&nbsp; {:,.0f} contracts<br>Open book: net <b>{:+,.0f}'
             '</b> &nbsp;\u00b7&nbsp; exposure <b>${:,.2f}</b> &nbsp;\u00b7&nbsp; '
             'resting <b>${:,.2f}</b> ({} orders) &nbsp;\u00b7&nbsp; balance '
             '<b>{}</b></div>'.format(
                 _pnl_span(d["realized"]), _pnl_span(d["settle"]),
                 _pnl_span(d["unrealized"]), d["contracts"], tot["net_pos"],
                 tot["exposure"], resting["collateral"], resting["orders"],
                 bal_str))
    h.append('<div style="font-size:15px;font-weight:600;margin:10px 0 4px">'
             'Daily P&amp;L (raw)</div>')
    h.append('<table style="border-collapse:collapse">')
    h.append('<tr style="background:#f0f0f0;font-weight:600">'
             '<td style="{0}">DATE</td><td style="{1}">RAW$</td>'
             '<td style="{1}">REWARD$</td><td style="{1}">NET$</td>'
             '<td style="{1}">CONTRACTS</td></tr>'.format(TDL, TD))
    h_raw = h_rew = 0.0
    for i, (day, raw, reward, contracts, nf) in enumerate(series):
        bg = "#fafafa" if i % 2 else "#fff"
        net = (raw + reward) if (raw is not None and reward is not None) else None
        if raw is not None:
            h_raw += raw
        if reward is not None:
            h_rew += reward
        h.append('<tr style="background:{0}"><td style="{1}">{2}</td>'
                 '<td style="{3}">{4}</td><td style="{3}">{5}</td>'
                 '<td style="{3};font-weight:600">{6}</td>'
                 '<td style="{3}">{7:,.0f}</td></tr>'.format(
                     bg, TDL, day, TD,
                     _pnl_span(raw) if raw is not None else "n/a",
                     "{:+,.2f}".format(reward) if reward is not None else "n/a",
                     _pnl_span(net) if net is not None else "n/a", contracts))
    h.append('<tr style="background:#f0f0f0;font-weight:700">'
             '<td style="{0}">TOTAL</td><td style="{1}">{2}</td>'
             '<td style="{1}">{3:+,.2f}</td><td style="{1}">{4}</td>'
             '<td style="{1}"></td></tr>'.format(
                 TDL, TD, _pnl_span(h_raw), h_rew, _pnl_span(h_raw + h_rew)))
    h.append("</table>")
    h.append('<div style="color:#888;font-size:12px;margin-top:4px">RAW is n/a '
             'for days older than the {}h fill-attribution window; rewards are '
             'the bot estimate (lifetime realization 0.975x vs credited).'
             '</div>'.format(FILL_LOOKBACK_HOURS))
    h.append('<div style="font-size:15px;font-weight:600;margin:14px 0 4px">'
             'Events traded in the past day ({})</div>'.format(len(ev_rows)))
    if ev_rows:
        h.append('<table style="border-collapse:collapse">')
        h.append('<tr style="background:#f0f0f0;font-weight:600">'
                 '<td style="{0}">EVENT</td><td style="{1}">P&amp;L$</td>'
                 '<td style="{1}">REAL$</td><td style="{1}">SETTLE$</td>'
                 '<td style="{1}">MTM$</td><td style="{1}">CTS</td>'
                 '<td style="{1}">MKTS</td></tr>'.format(TDL, TD))
        et = {"realized": 0.0, "settle": 0.0, "unrealized": 0.0,
              "contracts": 0.0, "mkts": 0}
        for i, (ev, e) in enumerate(ev_rows):
            bg = "#fafafa" if i % 2 else "#fff"
            tot_e = e["realized"] + e["settle"] + e["unrealized"]
            for k in ("realized", "settle", "unrealized", "contracts"):
                et[k] += e[k]
            et["mkts"] += len(e["mkts"])
            h.append('<tr style="background:{0}"><td style="{1}">{2}</td>'
                     '<td style="{3};font-weight:600">{4}</td>'
                     '<td style="{3}">{5}</td><td style="{3}">{6}</td>'
                     '<td style="{3}">{7}</td><td style="{3}">{8:,.0f}</td>'
                     '<td style="{3}">{9}</td></tr>'.format(
                         bg, TDL, _short_event(ev), TD, _pnl_span(tot_e),
                         _pnl_span(e["realized"]), _pnl_span(e["settle"]),
                         _pnl_span(e["unrealized"]), e["contracts"],
                         len(e["mkts"])))
        h.append('<tr style="background:#f0f0f0;font-weight:700">'
                 '<td style="{0}">TOTAL</td><td style="{1}">{2}</td>'
                 '<td style="{1}">{3}</td><td style="{1}">{4}</td>'
                 '<td style="{1}">{5}</td><td style="{1}">{6:,.0f}</td>'
                 '<td style="{1}">{7}</td></tr>'.format(
                     TDL, TD,
                     _pnl_span(et["realized"] + et["settle"] + et["unrealized"]),
                     _pnl_span(et["realized"]), _pnl_span(et["settle"]),
                     _pnl_span(et["unrealized"]), et["contracts"], et["mkts"]))
        h.append("</table>")
    else:
        h.append("<div>No fills in the past 24h.</div>")
    h.append('<div style="color:#777;font-size:12px;margin-top:12px;'
             'border-top:1px solid #eee;padding-top:8px">{}</div>'.format(health))
    h.append("</div>")
    return text, "".join(h)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; do not write it")
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    today_ct = now_utc.astimezone(CT).date()
    marker = os.path.join(STATUS_DIR, f"imm_digest_sent_{today_ct}.marker")

    if not args.test and os.path.exists(marker):
        log(f"imm digest already sent for {today_ct}; exiting")
        return 0

    # The 7am trigger can fire while the laptop is in Modern Standby with the
    # network radio off — retry for ~40 minutes so it goes out after wake.
    body = html = None
    for attempt in range(1, 9):
        try:
            body, html = build_digest(now_utc)
            break
        except Exception as e:
            log(f"imm digest build attempt {attempt}/8 failed: {e!r}; retry in 5min")
            if attempt == 8:
                log("giving up for today")
                return 1
            time.sleep(300)
    log("imm digest body:\n" + body)

    alerter = Alerter("IMM-DIGEST", live=True)
    if not alerter.enabled:
        log("cannot send imm digest: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, 9):
        ok = alerter.send_message(body, subject=f"Kalshi incentive MM digest {today_ct}",
                                  html=html)
        if ok:
            break
        log(f"imm digest send attempt {attempt}/8 failed; retry in 5min")
        time.sleep(300)
    log(f"imm digest send: {'ok' if ok else 'FAILED'}")
    if ok and not args.test:
        with open(marker, "w") as f:
            f.write(now_utc.isoformat())
        cutoff = today_ct - timedelta(days=7)
        for old in glob.glob(os.path.join(STATUS_DIR, "imm_digest_sent_*.marker")):
            name = os.path.basename(old)[len("imm_digest_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
