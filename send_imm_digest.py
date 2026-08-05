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
import ast
import collections
import csv
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


LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "run_incentive_mm.ps1")


def _apply_launcher_env() -> dict:
    """Mirror the LIVE bot's config by parsing `set NAME=VALUE&&` pairs out of
    the launcher's $ProbeEnv string. Must run BEFORE incentive_mm is imported —
    its config is read at import time.

    Required for the capacity section to mean anything: on defaults this
    process sees a $1,000 budget and a 35-event cap, where the live bot runs
    $50,000 and 75. Reporting headroom against the wrong ceiling is worse than
    reporting none, so the section says so loudly when the parse comes back
    empty. Same helper (and same gotcha) as imm_quote_gaps.py."""
    applied = {}
    try:
        with open(LAUNCHER_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return applied
    for chunk in re.findall(r'\$ProbeEnv\s*=\s*"(set .*?)"', text, re.S):
        for name, val in re.findall(
                r"set ([A-Za-z_][A-Za-z0-9_]*)=([^&]*)&&", chunk):
            os.environ[name] = val
            applied[name] = val
    return applied


LAUNCHER_ENV = _apply_launcher_env()

for _v in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD"):
    if not os.environ.get(_v):
        _val = _env_from_registry(_v)
        if _val:
            os.environ[_v] = _val

# Import AFTER the env fixup so the module-level cred constants pick them up.
import incentive_mm as imm                                          # noqa: E402
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


# Rewards actually CREDITED by Kalshi. There is no credits endpoint (verified
# 2026-08-03 across 8 paths), so the numbers come from the account statement
# via `imm_reward_recon.py --statement <paste>`, which keeps a permanent
# per-credit ledger and writes reward_calibration.json. The env vars below are
# a manual fallback for when the ledger has not been refreshed.
#
# 2026-08-04: the first COMPLETE statement was reconciled and it retired a
# large piece of folklore. Every credit this account has ever received is in
# the ledger (2,721 rows summing to the statement's own lifetime total to the
# penny), and it shows:
#   * only $112.85 of credit predates IMM's 2026-07-12 go-live, not ~$1,500;
#   * the KXHIGH weather bot earned $5.80 of liquidity incentive in its LIFE,
#     so the "~$1,500 of non-IMM reward from 152 pre-IMM days" figure — a
#     modelled replay, never a measurement — was wrong by more than 10x and
#     is deleted rather than re-tuned;
#   * non-IMM credit is ~$780, and it is identifiable event by event (MLB /
#     fight / mention markets belong to the other bots on this key).
# Attribution is now per-event against the events IMM actually quoted, so no
# hand-set offset is needed at all.
CALIB_PATH = os.path.join(STATUS_DIR, "reward_calibration.json")
CREDITS_PATH = os.path.join(STATUS_DIR, "reward_credits.csv")
# IMM went live on this date; the digest reports NO credit dated before it
# (Jack 2026-08-04) — earlier credit belongs to other strategies by definition.
IMM_INCEPTION = "2026-07-12"
# Credits arrive 1-2 days after the liquidity that earned them (measured lag:
# median 1.3d, p90 1.7d, max 3.0d), so a ledger more than this many days
# behind is genuinely missing money rather than merely waiting on settlement.
LEDGER_STALE_DAYS = int(os.environ.get("IMM_LEDGER_STALE_DAYS", "4"))
# IMM_REWARDS_CREDITED / IMM_REWARDS_CREDITED_MTD are GONE (2026-08-04). They
# were hand-set account-level statement totals; nothing reads them now that the
# reported reward is the bot estimate and the ledger is per-event. The digest
# scheduled task still exports them — harmless, but delete them from the task
# when convenient so a stale value can never look meaningful again.
DAILY_PNL_PATH = os.path.join(STATUS_DIR, "daily_pnl.json")


def load_credit_ledger():
    """(rows, calibration) — the per-credit ledger and the summary written by
    imm_reward_recon.py. Empty/absent is fine: the digest falls back to the
    bot's own estimate and says so."""
    rows = []
    try:
        with open(CREDITS_PATH, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((r["credit_date"], r["event_ticker"],
                             _f(r["amount"])))
    except (OSError, KeyError, ValueError):
        rows = []
    calib = load_json(CALIB_PATH) or {}
    return rows, calib


def credited_windows(rows, calib, today_et):
    """Credited reward by window, IMM-attributable and inception-filtered.

    Kalshi credits a program at its PERIOD END, so these windows are NOT
    comparable to an accrual over the same dates — a day's credits pay for
    liquidity resting over the preceding day or two (measured lag: median
    1.3d, p90 1.7d). They are reported as a settlement fact, not as "what the
    bot earned that day"; the daily table keeps using the accrual for that."""
    if not rows:
        return None
    imm_only = bool(calib.get("credited_imm_attributable"))
    # Per-date IMM-attributable credit, so every window is filtered the same
    # way. Without it "month to date" silently includes the MLB / fight-mention
    # credits earned by the other bots sharing this API key.
    by_date = calib.get("credited_by_date_imm") or {}
    if not by_date:
        by_date = {}
        for d, _e, a in rows:
            if d >= IMM_INCEPTION:
                by_date[d] = by_date.get(d, 0.0) + a
        imm_only = False
    life = calib.get("credited_imm_attributable") if imm_only \
        else sum(by_date.values())
    d1 = (today_et - timedelta(days=1)).isoformat()
    week = {(today_et - timedelta(days=i)).isoformat() for i in range(1, 8)}
    month = today_et.strftime("%Y-%m")
    return {
        "lifetime": life,
        "day": by_date.get(d1, 0.0),
        "week": sum(a for d, a in by_date.items() if d in week),
        "mtd": sum(a for d, a in by_date.items() if d[:7] == month),
        "latest": max((d for d, _e, _a in rows), default=""),
        "account_lifetime": calib.get("credited_lifetime_account"),
        "non_imm": calib.get("credited_non_imm"),
        "attributed": imm_only,
    }


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
    # PAID basis where available: the raw accrual bills for markets that never
    # clear the exchange's $1-per-market program floor and are paid nothing.
    paid_hist = {str(k): _f(v) for k, v
                 in (state.get("reward_paid_history") or {}).items()}
    # Rewards before IMM's go-live are not IMM's (Jack 2026-08-04); the table
    # starts at inception regardless of what history happens to be persisted.
    hist = {k: v for k, v in hist.items() if k >= IMM_INCEPTION}
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
        if day >= today or key < IMM_INCEPTION:
            continue                       # only PRIOR days, only since go-live
        reward = paid_hist.get(key, hist.get(key))
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

    today_et = datetime.now(timezone.utc).astimezone(ET).date()
    ledger, calib = load_credit_ledger()
    cred = credited_windows(ledger, calib, today_et)
    # LIFETIME reward is the BOT ESTIMATE (Jack 2026-08-04). It briefly read
    # off the credit ledger, which is the only figure that is a fact rather
    # than a model — but it is a fact about a DIFFERENT quantity: credits are
    # paid at each program's period end, so the ledger always trails what the
    # book has earned and never includes the in-flight programs. Sitting in a
    # column next to same-instant RAW P&L, that lag reads as underperformance
    # rather than as settlement timing.
    #
    # The raw counter is used rather than reward_paid_lifetime because the
    # paid-basis counter only started accumulating 2026-08-04 and was migrated
    # WITHOUT back-crediting, so it is near zero and cannot represent lifetime
    # yet. Once it has real history it is the better source here (it applies
    # the exchange's $1/market floor); the sub-windows below already prefer it
    # per day wherever it exists.
    rew_life = reward_lifetime
    rew_basis = "bot estimate"
    # Sub-window rewards come from the persisted per-day history; days before
    # the 2026-08-03 fix are missing and are reported as such rather than
    # silently summed to a wrong number. These stay on the ACCRUAL basis on
    # purpose — credits land 1-2 days after the liquidity that earned them, so
    # a credited "yesterday" would not line up with yesterday's RAW P&L.
    hist = {str(k): _f(v) for k, v in (state.get("reward_history") or {}).items()}
    paid_hist = {str(k): _f(v) for k, v
                 in (state.get("reward_paid_history") or {}).items()}
    day_key = (today_et - timedelta(days=1)).isoformat()
    # Prefer the PAID basis (the raw integral with the exchange's $1-per-market
    # floor applied) — the raw one bills for the ~30% of quoted markets that
    # provably pay nothing. Falls back to raw for days recorded before the
    # 2026-08-04 change.
    rew_day = paid_hist.get(day_key, hist.get(day_key))
    week_keys = [(today_et - timedelta(days=i)).isoformat() for i in range(1, 8)]
    have = [paid_hist.get(k, hist[k]) for k in week_keys if k in hist]
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
        "credited": cred,
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
    # A credit ledger that stops being refreshed reports a shrinking fraction
    # of reality while still LOOKING authoritative — the one failure mode of
    # moving lifetime reward onto the statement. Say so out loud.
    rows, _cal = load_credit_ledger()
    if rows:
        latest = max(d for d, _e, _a in rows)
        try:
            lag = (now.astimezone(ET).date()
                   - datetime.strptime(latest, "%Y-%m-%d").date()).days
        except ValueError:
            lag = 0
        if lag > LEDGER_STALE_DAYS:
            problems.append(
                f"REWARD LEDGER STALE ({lag}d; last credit {latest}) — paste a "
                f"fresh statement through imm_reward_recon.py --statement")
    else:
        problems.append("NO REWARD LEDGER — lifetime reward is the bot estimate")
    line = f"Bot: {'alive' if alive else 'DOWN'}, mode {status.get('mode', '?')}, " \
           f"{int(ss['errors'])} errors in last summary"
    if standoff:
        line += f" | {len(standoff)} market(s) yielded to manual/other bots"
    if problems:
        line += " | " + "; ".join(problems)
    return line


_UNIVERSE_RE = re.compile(
    r"universe: (\d+) program markets -> (\d+) candidates -> (\d+) selected "
    r"across (\d+)/(\d+) events.*?~\$(\d+), total ~\$(\d+) ladder collateral, "
    r"\$(\d+) inventory reserve\); skips (\{.*\})")


def last_universe_line():
    """The SELECTION GATE's own view, parsed from the newest bot log.

    This matters because the gate does not compare against deployed capital:
    market_cost() reserves worst-case-ladder x 0.65 for every selected market,
    forward-looking, and THAT total is what gets tested against the budget.
    The live resting book reads lower, so a digest showing only resting
    collateral would report headroom the bot does not believe it has. The
    'budget' skip count is the direct answer to 'am I capped' — non-zero means
    markets were refused for capital this cycle."""
    try:
        logs = sorted(glob.glob(os.path.join(STATUS_DIR, "incentive-mm-*.log")))
        if not logs:
            return None
        with open(logs[-1], encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4_000_000))
            tail = f.read()
    except OSError:
        return None
    m = None
    for m in _UNIVERSE_RE.finditer(tail):
        pass
    if not m:
        return None
    try:
        skips = ast.literal_eval(m.group(9))
    except (ValueError, SyntaxError):
        skips = {}
    return {"programs": int(m.group(1)), "candidates": int(m.group(2)),
            "markets": int(m.group(3)), "events": int(m.group(4)),
            "event_cap": int(m.group(5)), "ladder": float(m.group(7)),
            "reserve": float(m.group(8)), "skips": skips}


def capacity_rows(state, status, resting, pnl_today):
    """[(label, actual, cap, unit, note)] — every governor that can stop the
    bot quoting, with what it is actually running at (Jack 2026-08-04: "so I
    know how close I am to getting capped").

    Actuals come from the LIVE resting book and own positions rather than the
    bot's internal counters, so this stays honest across restarts. Caps come
    from the mirrored launcher env — see _apply_launcher_env."""
    rows = []

    def add(label, actual, cap, unit="", note=""):
        rows.append({"label": label, "actual": actual, "cap": cap,
                     "unit": unit, "note": note,
                     "pct": (100.0 * actual / cap) if cap else None})

    # --- the SELECTION GATE's own numbers, when the log gives them up ----
    u = last_universe_line()
    if u:
        add("Events selected", u["events"], u["event_cap"],
            note="the gate's own count; new events refused at the cap, "
                 "members keep quoting")
        add("Collateral reserved (gate)", u["ladder"] + u["reserve"],
            imm.COLLATERAL_BUDGET, "$",
            note="ladder ${:,.0f} + inventory reserve ${:,.0f} — reserved "
                 "worst-case, NOT capital deployed".format(u["ladder"], u["reserve"]))
        add("Candidate books", u["candidates"], imm.MAX_CANDIDATE_BOOKS)
        nb = int(u["skips"].get("budget", 0) or 0)
        rows.append({"label": "Markets refused for CAPITAL last cycle",
                     "actual": nb, "cap": 0, "unit": "", "pct": None,
                     "note": ("none — the budget is not binding" if not nb else
                              "budget IS binding: {} market(s) skipped".format(nb))})
    else:
        add("Events quoted (live book)", resting.get("events", 0),
            imm.MAX_MARKETS, note="gate numbers unavailable — parsed from the "
                                  "live resting book instead")

    # --- what is actually deployed, as a cross-check ---------------------
    collat = resting.get("collateral", 0.0)
    add("Collateral deployed (resting)", collat, imm.COLLATERAL_BUDGET, "$",
        note="actual collateral locked by resting orders")

    # --- order-count / universe governors --------------------------------
    add("Resting orders", resting.get("orders", 0), imm.MAX_TOTAL_RESTING_ORDERS)

    # --- position caps: report the WORST market / event, not the total ----
    pos = {t: float(p) for t, p in (state.get("own_pos") or {}).items()
           if abs(float(p)) > 0.5}
    if pos:
        wt, wv = max(pos.items(), key=lambda kv: abs(kv[1]) / max(
            imm.series_max_position(kv[0].split("-")[0]), 1))
        cap = imm.series_max_position(wt.split("-")[0])
        add("Per-market position (worst)", abs(wv), cap, "cts", note=wt)
        by_ev = {}
        for t, p in pos.items():
            by_ev["-".join(t.split("-")[:2])] = by_ev.get("-".join(t.split("-")[:2]), 0.0) + p
        we, wev = max(by_ev.items(), key=lambda kv: abs(kv[1]) / max(
            imm.event_cap_contracts(kv[0]), 1))
        add("Per-event net (worst)", abs(wev), imm.event_cap_contracts(we),
            "cts", note=we)

    # --- the halts -------------------------------------------------------
    if pnl_today is not None and imm.DAILY_LOSS_LIMIT:
        add("Daily loss vs halt", max(-pnl_today, 0.0), imm.DAILY_LOSS_LIMIT, "$",
            note="halt cancels everything until the next roll")
    return rows


def capacity_note():
    """Loud when the launcher parse failed — see _apply_launcher_env."""
    if LAUNCHER_ENV:
        return ("caps mirrored from the live launcher ({} vars)"
                .format(len(LAUNCHER_ENV)))
    return ("!! caps are incentive_mm DEFAULTS — the launcher $ProbeEnv could "
            "not be parsed, so these ceilings are NOT what the bot is running")


def calibration_status():
    """(validated, unvalidated) family lists from reward_calibration.json.

    A family is VALIDATED only where settled, paid-out programs exist to
    compare against. That is not a detail: hourly temp settles inside the hour
    and is measurable the next day, while a 5-day earnings-mention program
    contributes to the reward column for days before it can be checked at all.
    Reporting one blended realization factor let a temp-only measurement read
    as a whole-book accuracy claim (Jack caught this 2026-08-04: the 8/1 and
    8/2 rows are 67% and 27% families with NO settled credit evidence)."""
    cal = load_json(CALIB_PATH) or {}
    pa = cal.get("post_amendment") or {}
    by_fam = pa.get("by_family") or {}
    validated, unvalidated = [], []
    for name, v in sorted(by_fam.items()):
        if v.get("realization_factor") and v.get("credited", 0) >= 1.0:
            validated.append((name, v["realization_factor"], v["events"],
                              v["credited"]))
        else:
            unvalidated.append(name)
    return validated, unvalidated, pa


def _calibration_caveat_text():
    validated, _unval, pa = calibration_status()
    if not validated:
        return ["  (REWARD is the bot estimate — no credit ledger yet; run "
                "imm_reward_recon.py --statement)"]
    out = ["  REWARD accuracy — measured per family against real credits, not "
           "assumed. Only families whose programs have SETTLED and PAID can be",
           "  checked at all, so this is a statement about part of the column, "
           "not all of it:"]
    for name, fac, n, cred in validated:
        out.append("    {:<28} {:.3f}x  ({} settled events, ${:,.2f} credited)"
                   .format(name, fac, n, cred))
    out.append("    every other family              UNVALIDATED — no settled "
               "post-{} credit yet".format(pa.get("cutover", "?")[:10]))
    return out


def _calibration_caveat_html():
    validated, _unval, pa = calibration_status()
    if not validated:
        return ('<div style="color:#b8860b;font-size:12px;margin-top:4px">'
                'REWARD is the bot estimate — no credit ledger yet; run '
                '<code>imm_reward_recon.py --statement</code>.</div>')
    rows = "".join(
        "<li><b>{}</b> — {:.3f}x ({} settled events, ${:,.2f} credited)</li>"
        .format(n, f, e, c) for n, f, e, c in validated)
    return ('<div style="color:#666;font-size:12px;margin-top:6px;'
            'border-left:3px solid #d9a441;padding-left:8px">'
            '<b>How much of this REWARD column is actually verified?</b> Only '
            'families whose programs have settled AND paid can be compared to '
            'credits at all — hourly temp settles inside the hour, a 5-day '
            'earnings-mention program does not. Verified against real credits '
            'since the {} estimator rewrite:<ul style="margin:4px 0">{}</ul>'
            'Every other family — earnings-mention, rain, company/econ — is '
            '<b>unvalidated</b>: it contributes to the numbers above with no '
            'settled credit to check it against. Where pre-rewrite evidence '
            'exists it ran 0.33–0.64x, i.e. those contributions may be '
            'materially overstated.</div>'.format(pa.get("cutover", "?")[:10], rows))


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
    cap_rows = capacity_rows(state, status, resting,
                             _f(status.get("pnl_today")) if status else None)

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
    # The credited-rewards block that used to sit here was removed at Jack's
    # request 2026-08-04. The ledger still BACKS the lifetime REWARD figure
    # above (see pnl_windows) and the health line still warns when it goes
    # stale — only the standalone breakdown is gone.
    L.append("")
    L.append("")
    L.append("DAILY P&L — every prior day that earned (raw; a day moves until "
             "its positions settle)")
    L.append("{:12s} {:>11s} {:>11s} {:>11s} {:>10s}".format(
        "DATE", "RAW$", "REWARD$", "NET$", "CONTRACTS"))
    d_raw = d_rew = 0.0
    # most recent first (Jack 2026-08-04)
    for day, raw, reward, contracts, nf in reversed(series):
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
    L.append("  (REWARD is accrual-dated and does NOT line up with a credit "
             "date — Kalshi pays at each program's period end, 1-2 days later)")
    L.extend(_calibration_caveat_text())
    L.append("")
    L.append("")
    L.append("CAPACITY — how close the bot is to each ceiling ({})".format(
        capacity_note()))
    L.append("{:34s} {:>12s} {:>12s} {:>7s}".format(
        "GOVERNOR", "ACTUAL", "CAP", "USED"))
    for r in cap_rows:
        u = "{:.0f}%".format(r["pct"]) if r["pct"] is not None else "-"
        fmt = "{:,.0f}" if r["unit"] != "$" else "{:,.2f}"
        cap = fmt.format(r["cap"]) if r["pct"] is not None else "-"
        L.append("{:34s} {:>12s} {:>12s} {:>7s}{}".format(
            r["label"], fmt.format(r["actual"]), cap, u,
            "  <== AT CAP" if (r["pct"] or 0) >= 95 else
            ("  <- close" if (r["pct"] or 0) >= 80 else "")))
        if r["note"]:
            L.append("{:34s} {}".format("", r["note"]))
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
    # The "Rewards credited by Kalshi" table and the "reward basis" line that
    # used to sit here were removed at Jack's request 2026-08-04. The ledger
    # still BACKS the lifetime REWARD figure in the table above (pnl_windows
    # prefers it over the bot estimate) and health_line still warns when it
    # goes stale — only the standalone breakdown is gone.
    h.append('<div style="font-size:15px;font-weight:600;margin:10px 0 4px">'
             'Daily P&amp;L (raw)</div>')
    h.append('<table style="border-collapse:collapse">')
    h.append('<tr style="background:#f0f0f0;font-weight:600">'
             '<td style="{0}">DATE</td><td style="{1}">RAW$</td>'
             '<td style="{1}">REWARD$</td><td style="{1}">NET$</td>'
             '<td style="{1}">CONTRACTS</td></tr>'.format(TDL, TD))
    h_raw = h_rew = 0.0
    for i, (day, raw, reward, contracts, nf) in enumerate(reversed(series)):
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
             'for days older than the {}h fill-attribution window. REWARD is '
             'accrual-dated, so it does NOT line up with a credit date &mdash; '
             'Kalshi pays at each program\'s period end, 1&ndash;2 days later.'
             '</div>'.format(FILL_LOOKBACK_HOURS))
    h.append(_calibration_caveat_html())

    h.append('<div style="font-size:15px;font-weight:600;margin:16px 0 4px">'
             'Capacity &mdash; how close to each ceiling</div>')
    h.append('<table style="border-collapse:collapse">')
    h.append('<tr style="background:#f0f0f0;font-weight:600">'
             '<td style="{0}">GOVERNOR</td><td style="{1}">ACTUAL</td>'
             '<td style="{1}">CAP</td><td style="{1}">USED</td>'
             '<td style="{0}"></td></tr>'.format(TDL, TD))
    for i, r in enumerate(cap_rows):
        pct = r["pct"] or 0
        colour = "#c0392b" if pct >= 95 else ("#d9821b" if pct >= 80 else "#0a7a2f")
        if r["pct"] is None:
            colour = "#c0392b" if r["actual"] else "#777"
        bar = min(int(pct), 100)
        fmt = "{:,.0f}" if r["unit"] != "$" else "{:,.2f}"
        h.append(
            '<tr style="background:{bg}"><td style="{tdl}">{label}'
            '{note}</td>'
            '<td style="{td}">{act}</td><td style="{td}">{cap}</td>'
            '<td style="{td};color:{col};font-weight:700">{pct}</td>'
            '<td style="{tdl};width:120px">'
            '<div style="background:#eee;height:9px;width:110px">'
            '<div style="background:{col};height:9px;width:{bar}px"></div>'
            '</div></td></tr>'.format(
                bg="#fafafa" if i % 2 else "#fff", tdl=TDL, td=TD, col=colour,
                label=r["label"],
                note=('<div style="color:#999;font-size:11px">{}</div>'.format(
                    r["note"]) if r["note"] else ""),
                act=fmt.format(r["actual"]),
                cap=fmt.format(r["cap"]) if r["pct"] is not None else "&mdash;",
                pct="{:.0f}%".format(pct) if r["pct"] is not None else "&mdash;",
                bar=int(bar * 1.1)))
    h.append("</table>")
    h.append('<div style="color:#888;font-size:12px;margin-top:4px">{}</div>'
             .format(capacity_note()))

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
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="build and print the digest; send nothing, write no marker")
    ap.add_argument("--html-out", help="with --print, also write the HTML here")
    args = ap.parse_args(argv)

    if args.print_only:
        body, html = build_digest(datetime.now(timezone.utc))
        print(body)
        if args.html_out:
            with open(args.html_out, "w", encoding="utf-8") as f:
                f.write(html)
            log(f"wrote {args.html_out}")
        return 0

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
