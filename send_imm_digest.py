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
import textwrap
import time
from datetime import datetime, timedelta, timezone


def _env_from_registry(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError):     # ImportError: non-Windows (tests)
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
    fresh = {}
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
        # Live in-window recompute wins (marks/settles move until final) and
        # is upserted into the store — UNLESS the stored record saw MORE
        # fills: the oldest in-window day is only partially covered by the
        # fill window, and freezing it would clobber a fuller measurement.
        if dfills and not (bf and int(bf.get("fills") or 0) > len(dfills)):
            tot, _ev, _p = raw_pnl_for_fills(client, dfills, mids, results)
            raw, contracts, nf = tot["raw"], tot["contracts"], len(dfills)
            fresh[key] = {"raw": round(raw, 2),
                          "realized": round(_f(tot.get("realized")), 2),
                          "settle": round(_f(tot.get("settle")), 2),
                          "mtm": round(_f(tot.get("unrealized")), 2),
                          "fees": round(_f(tot.get("fees")), 2),
                          "contracts": round(contracts, 2), "fills": nf}
        elif bf:                            # frozen: backfill or prior upsert
            raw = _f(bf.get("raw"))
            contracts, nf = _f(bf.get("contracts")), int(bf.get("fills") or 0)
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
    # Persist the live-computed days: each day keeps refreshing while inside
    # the fill window, then stays FROZEN at its last (fullest) measurement
    # instead of dropping to n/a once it ages past FILL_LOOKBACK_HOURS. The
    # 8/4-8/28 n/a hole was this store going stale after its one-time 8/3
    # backfill; wholesale rebuilds remain imm_backfill_daily_pnl.py's job.
    if fresh:
        merged = dict(backfill)
        merged.update(fresh)
        try:
            tmp = DAILY_PNL_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=1, sort_keys=True)
            os.replace(tmp, DAILY_PNL_PATH)
        except OSError as e:
            log(f"! daily_pnl.json upsert failed: {e}")
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


# Caps whose live value we can check against the launcher string. Verifying the
# OUTCOME rather than the attempt matters: if anything imports incentive_mm
# before _apply_launcher_env runs (its config is read at import), the env is
# set but the constants are already frozen at defaults — and a note that just
# counted parsed vars would cheerfully report "mirrored" over a $1,000 budget.
_CAP_CHECKS = (("IMM_COLLATERAL_BUDGET", lambda: imm.COLLATERAL_BUDGET),
               ("IMM_MAX_MARKETS", lambda: imm.MAX_MARKETS),
               ("IMM_MAX_TOTAL_RESTING", lambda: imm.MAX_TOTAL_RESTING_ORDERS),
               ("IMM_MAX_POSITION", lambda: imm.MAX_POSITION_CONTRACTS),
               ("IMM_MAX_EVENT", lambda: imm.MAX_EVENT_CONTRACTS))


def capacity_config_mismatches():
    """[(var, launcher_value, in_effect)] where the live constant does NOT
    match what the launcher sets. Empty = the section's ceilings are real."""
    out = []
    for var, getter in _CAP_CHECKS:
        want = LAUNCHER_ENV.get(var)
        if want is None:
            continue
        try:
            if abs(float(want) - float(getter())) > 1e-6:
                out.append((var, want, getter()))
        except (TypeError, ValueError):
            continue
    return out


def capacity_note():
    """Loud when the ceilings are not the bot's — see _apply_launcher_env."""
    if not LAUNCHER_ENV:
        return ("!! caps are incentive_mm DEFAULTS — the launcher $ProbeEnv "
                "could not be parsed, so these ceilings are NOT what the bot "
                "is running")
    bad = capacity_config_mismatches()
    if bad:
        return ("!! caps do NOT match the launcher ({}) — incentive_mm was "
                "imported before the env was applied, so these ceilings are "
                "NOT what the bot is running".format(
                    ", ".join(f"{v}: launcher {w}, in effect {g:g}"
                              for v, w, g in bad)))
    return ("caps mirrored from the live launcher ({} vars, {} verified)"
            .format(len(LAUNCHER_ENV), len(_CAP_CHECKS)))


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


# ---------------------------------------------------------------------------
# CUTOFF AUDIT — our earnings call-time override vs Kalshi's own ticker date.
#
# Why this section exists (Jack 2026-08-05): KXEARNINGSMENTIONLLY-26AUG07 stopped
# being quoted at 10:50Z. The override said the Eli Lilly call was 2026-08-05
# 07:00 ET while the Kalshi TICKER (-26AUG07), the sub_title ("On Aug 7, 2026")
# and the incentive program's end_date all said Aug 7. The first read was "our
# override is wrong". It was not: Lilly reported the morning of Aug 5, Kalshi
# closed and finalized the event at 2026-08-05T16:08Z, and standing down at
# 10:50Z is exactly what kept the bot from quoting through a live print. Kalshi's
# three "independent" signals are really one — ticker date, sub_title and program
# end all derive from the same internal date (program end == ticker date 10 of 10
# on live earnings events), so they corroborate each other for free and in this
# case were wrong together.
#
# The two directions are NOT symmetric, which is the whole point of the section:
#   * override EARLIER than the ticker date -> the bot stands down early. Costs
#     reward accrual, carries NO adverse-selection risk, and on every conflict
#     that has actually resolved the override was the right date and the ticker
#     the wrong one (BA closed 7/28 vs ticker 7/21, HOOD 7/29 vs 7/17, PGR 8/4
#     vs 7/15, LLY 8/5 vs 8/7 — override 4, Kalshi ticker 0). Show it, quietly.
#   * override LATER than the ticker date -> the bot keeps quoting PAST Kalshi's
#     date and can make markets straight through a real earnings call. Real
#     money, real adverse selection. This direction shouts.
#
# Tolerance is EXACTLY ZERO, deliberately. The tempting carve-out is "a call
# after the close on day N with a ticker dated N+1 is just Kalshi's encoding" —
# but that shape occurs 1 time in 34 after-close overrides (33 of 34 AMC entries
# sit on delta 0; ABNB and LYFT are both Aug-6 after-close carrying 26AUG06
# tickers). Kalshi's convention IS ticker date = release date, so the lone -1
# (DKNG-26AUG07, measured 2026-08-05) is a genuine date error and a tolerance
# window would exist only to hide the very case Jack asked to see.
#
# SCOPE is every override with a parseable ticker date, NOT just earnings.
# The first cut gated on KXEARNINGSMENTION and that was wrong: incentive_mm.py
# (:1195) puts company-disclosure RELEASE times — KXINTC, KXFSLR, KXCOINBASE —
# under the same tight OVERRIDE_BUFFER_MIN as earnings calls, i.e. the bot
# already treats them as adverse-selection-critical, and a LATE override on a
# political-mention series is the same class of bug. Measured 2026-08-05,
# widening the gate adds 10 checked keys and ZERO extra rows, so the narrow
# gate bought no quiet and cost coverage. The ONE exclusion that survives is
# SCHEDULE_RESOLVED_SERIES (WNBA/MLB/WC), where a LATE override means a
# POSTPONED GAME — legitimate, different remedy — and the excluded count is
# printed so the exclusion is never silent.
#
# Kept here rather than in a shared module: only the digest reports it, and
# imm_earnings_overrides.py already owns a different question (does an override
# EXIST for every paying event) with its own stale-ticker autofix.
# ---------------------------------------------------------------------------


def _override_hour_label(ov_et: datetime, is_earnings: bool) -> str:
    """What the override's TIME actually means. nasdaq_release_datetime()
    (imm_earnings_overrides.py) SYNTHESIZES 16:00 from an after-close flag and
    07:00 from a before-open flag — those are proxies for a date, not call
    times. Anything else was scraped off the IR page and is a real time. The
    email must not claim "the call is at 4:00pm ET" on a synthesized hour.

    The proxy reading is only valid for earnings series, which is the only
    place nasdaq_release_datetime() writes; a 16:00 on KXTRUMPMENTION is just
    a start time somebody set."""
    if not is_earnings:
        return "hand-set start time"
    if (ov_et.hour, ov_et.minute) == (16, 0):
        return "after close (Nasdaq proxy 4pm ET)"
    if (ov_et.hour, ov_et.minute) == (7, 0):
        return "before open (Nasdaq proxy 7am ET)"
    return "scraped call time"


# ---------------------------------------------------------------------------
# UNVERIFIED CALL TIMES — the hole this section plugs (Jack 2026-08-06).
#
# The audit above compares DATES. KXEARNINGSMENTIONCELH-26AUG06 had the right
# date and the wrong HOUR: Nasdaq's calendar carried no time flag, the resolver
# fell through to 16:00 ET, Celsius actually reported before the open with an
# 8:00am call, and the bot quoted the event all morning with the results already
# public. delta was 0, so the audit skipped it at the `if delta == 0: continue`
# and tomorrow's 7:10am email would have said nothing.
#
# WHY NOT JUST FLAG EVERY 16:00: 29 of the 49 live earnings overrides sit at
# 16:00 and 28 of them are a real Nasdaq after-close flag. A "16:00 is
# suspicious" rule ships 29 rows every morning to catch one — a ratio that
# guarantees the section gets skimmed, and a skimmed section is worth less than
# no section because it also buries the date rows next to it. The distinguishing
# fact is not the hour, it is whether anyone MEASURED the hour, and that fact
# was thrown away at write time. imm_earnings_overrides.record_meta() now keeps
# it in a sidecar; measured over the 43 Nasdaq resolutions in the task log
# (2026-07-23..08-06) the "guess" class is 1. This section is empty on ~97% of
# mornings BY CONSTRUCTION, which is the only reason it will still be read on
# the morning it is not.
#
# Sidecar, not a shape change to event_start_overrides.json: incentive_mm's
# load_file_event_overrides() (:1481) runs parse_iso_utc(str(iso).strip()) on
# every value and DROPS entries it cannot parse, so a dict-valued override would
# delete the live bot's cutoff outright. See the module comment in
# imm_earnings_overrides.py. Absent sidecar => the check reports itself as not
# yet effective rather than reporting "clean".
OVERRIDE_META_PATH = os.path.join(STATUS_DIR, "event_start_overrides_meta.json")


def _days_out_phrase(n: int) -> str:
    """Imminence, in words. A +6d LATE row whose ticker date is three weeks
    out needs nothing this morning; one whose ticker date is TODAY needs
    action within the hour. An ISO date renders those identically at 7:10am,
    so the reader has to do the subtraction — this does it for them."""
    if n == 0:
        return "TODAY"
    if n == 1:
        return "TOMORROW"
    if n > 1:
        return "in {}d".format(n)
    if n == -1:
        return "yesterday"
    return "{}d ago".format(-n)


def cutoff_audit(client, now_utc: datetime, own_pos: dict = None) -> dict:
    """Start overrides whose ET DATE disagrees with their Kalshi ticker date.

    Returns {"rows": [...], "checked", "total", "excluded", "no_day",
    "dead": [event...], "error"}. NEVER raises — build_digest has no
    per-section guard and main() would retry a raising build 8 times at 5-minute
    intervals and then send nothing all day, so every failure comes back as
    {"error": ...} for the renderers to print.

    own_pos is build_digest's own_book() position map (market ticker -> signed
    contracts). It costs no extra API call and it is the only number in the
    section that is actual EXPOSURE; everything else is reward pool, i.e. what
    standing down would forfeit.

    Source of truth is imm.EVENT_START_OVERRIDES after load_file_event_overrides()
    (in-process merge only, no writes), NOT the JSON file: five overrides are
    hard-coded as the IMM_EVENT_START_OVERRIDE default in incentive_mm.py and
    never appear in the file — one of them, KXEARNINGSMENTIONNFLX-26JUL02, is
    itself +14d LATE.

    Gates, in order:
      1. never schedule-resolved series (WNBA/MLB/WC): a LATE override there is
         a POSTPONED GAME, which is legitimate and has a different remedy. This
         is the ONLY series exclusion — see the module comment on why the old
         earnings-only gate was removed — and it is counted and printed.
      2. parse_event_date() -> None means the ticker carries no calendar day at
         all (KXTLN-26AUGGEN, KXCOINBASE-26JULVOL). That is "not comparable",
         never "mismatch"; counted and reported, not flagged.
      3. compare in ET on BOTH sides. parse_event_date returns midnight ET as
         UTC and overrides are stored at -04:00, so a UTC-date comparison
         invents a spurious +1d on any evening entry.
      4. delta == 0 -> agree. See the module comment on why the tolerance is
         exactly zero.

    Then the money gate: an event with no LIVE incentive program is dead history.
    The overrides file is never garbage-collected, so without this the section
    would ship BA-26JUL21 and PGR-26JUL15 forever and grow by one row with every
    stale-ticker autofix. The same sweep supplies the pool $/day, which sizes a
    row but is NOT its risk."""
    a = {"rows": [], "checked": 0, "total": 0, "excluded": 0, "no_day": 0,
         "dead": [], "error": None, "unverified": [], "no_prov": 0}
    try:
        imm.load_file_event_overrides()          # in-process merge; no writes
        meta = load_json(OVERRIDE_META_PATH)      # advisory; {} when absent

        # Live liquidity pool per event, same accounting as the bot's own
        # fetch_programs(): period_reward is CENTI-cents, spread over the
        # program's own length.
        pools, cursor = {}, None
        for _page in range(20):
            params = {"limit": 1000, "status": "active"}
            if cursor:
                params["cursor"] = cursor
            resp = client.get("/incentive_programs", params=params)
            batch = resp.get("incentive_programs") or []
            for p in batch:
                if p.get("incentive_type") != "liquidity" or p.get("paid_out"):
                    continue
                start = imm.parse_iso_utc(p.get("start_date", ""))
                end = imm.parse_iso_utc(p.get("end_date", ""))
                if not start or not end or not (start <= now_utc < end):
                    continue
                days = max((end - start).total_seconds() / 86400.0, 1.0 / 24)
                t = p.get("market_ticker") or ""
                cur = pools.setdefault(_event_of(t), {"mkts": set(), "dpd": 0.0})
                cur["mkts"].add(t)
                cur["dpd"] += (p.get("period_reward") or 0) / 10000.0 / days
            cursor = resp.get("next_cursor")
            if not cursor or not batch:
                break

        # Contracts on the book per event — the actual exposure. Costs nothing:
        # build_digest already called own_book(state) before us.
        book = {}
        for t, v in (own_pos or {}).items():
            try:
                book[_event_of(t)] = book.get(_event_of(t), 0.0) + abs(_f(v))
            except Exception:
                continue

        today_et = now_utc.astimezone(ET).date()
        for ev, ov in sorted(imm.EVENT_START_OVERRIDES.items()):
            a["total"] += 1
            series = ev.split("-")[0]
            if series in imm.SCHEDULE_RESOLVED_SERIES:
                a["excluded"] += 1           # postponed game, not a date bug
                continue
            td = imm.parse_event_date(ev)
            if td is None or ov is None:
                a["no_day"] += 1
                continue
            a["checked"] += 1
            t_et = td.astimezone(ET).date()
            ov_et = ov.astimezone(ET)
            delta = (ov_et.date() - t_et).days
            cutoff_et = ov_et - timedelta(minutes=imm.OVERRIDE_BUFFER_MIN)

            # --- unverified-HOUR check, deliberately BEFORE the delta gate ---
            # CELH's delta was 0. Anything that runs after `if delta == 0:
            # continue` cannot see this class at all.
            rec = meta.get(ev) or {}
            rec_dt = imm.parse_iso_utc(str(rec.get("iso") or ""))
            if rec_dt is None or rec_dt != ov:
                # No record, or the record describes a value that has since been
                # superseded (a re-resolve, or Jack's --set). A superseded guess
                # is a FIXED guess and must stop being flagged, or the section
                # becomes a permanent red row nobody reads.
                a["no_prov"] += 1
            elif str(rec.get("confidence")) == "guess":
                pool_u = pools.get(ev)
                # Money gate + "is there still time to act": once the cutoff has
                # passed the bot is already standing down and the row is history.
                if pool_u and cutoff_et > now_utc:
                    a["unverified"].append({
                        "event": ev, "ticker_date": t_et, "override_et": ov_et,
                        "cutoff_et": cutoff_et, "days_out": (t_et - today_et).days,
                        "label": str(rec.get("label") or "unknown"),
                        "contracts": book.get(ev, 0.0),
                        "mkts": len(pool_u["mkts"]), "dpd": pool_u["dpd"],
                        # A guess that landed AFTER midday is a guess on the
                        # UNSAFE side: the bot keeps quoting through a morning
                        # call. A guess that landed in the morning already fails
                        # early and only forfeits accrual, so it is listed but
                        # never shouted.
                        "unsafe": ov_et.hour >= 12})

            if delta == 0:
                continue
            pool = pools.get(ev)
            if not pool:
                a["dead"].append(ev)         # programs over: history, not risk
                continue
            # LATE splits in two and the split is the severity, not the size:
            # BA/HOOD/PGR were LATE by 7-20 days and harmless because the ticker
            # date had ALREADY ELAPSED (the stale-ticker trap — quoting on is how
            # that pool gets earned). NBIS is LATE by 6 and dangerous because
            # both dates are still ahead, so nothing is stale and one of the two
            # sources is simply wrong about a call that has not happened yet.
            #
            # NOTE the copy for "warn" must stay conditional. t_et < today_et is
            # a PROXY for "this was a stale-ticker autofix", not a measurement of
            # it: a forward disagreement becomes an elapsed one purely by the
            # passage of a day, so NBIS is risk on Aug 6 and warn on Aug 7 with
            # no new information. Today that is masked because program end ==
            # ticker date on live earnings events (so the row goes dead first),
            # but that is a Kalshi convention, not a guarantee — Kalshi issues
            # 16-24 day mention programs. Do not let this branch assert safety.
            if delta > 0:
                sev = "risk" if t_et >= today_et else "warn"
            else:
                sev = "info"
            a["rows"].append({
                "event": ev, "ticker_date": t_et, "override_et": ov_et,
                "delta": delta, "severity": sev,
                "days_out": (t_et - today_et).days,
                "contracts": book.get(ev, 0.0),
                "mkts": len(pool["mkts"]), "dpd": pool["dpd"],
                "cutoff_et": cutoff_et,
                "hour_label": _override_hour_label(
                    ov_et, series.startswith(imm._EARNINGS_PREFIX))})
        # risk rows are ranked by IMMINENCE, not by pool size: the thing that
        # decides whether this needs action before the open is how soon Kalshi
        # thinks the call is, and dpd is the reward forfeited by standing down,
        # i.e. the argument for doing nothing.
        order = {"risk": 0, "warn": 1, "info": 2}
        a["rows"].sort(key=lambda r: (
            order[r["severity"]],
            r["days_out"] if r["severity"] == "risk" else 0,
            -r["dpd"]))
        # Same principle for the unverified list: soonest cutoff first. Pool is
        # the tie-break only — it sizes the row, it is not the reason to act.
        a["unverified"].sort(key=lambda r: (not r["unsafe"], r["cutoff_et"],
                                            -r["dpd"]))
    except Exception as e:
        a["error"] = repr(e)
    return a


def _cutoff_meaning(r: dict):
    """(what it means, what to DO) for one row.

    Every branch carries an imperative. A flag that raises a question it cannot
    help answer gets read once and then skimmed, and the EARLY branch in
    particular has to lead with the PROHIBITION: a tired reader who sees "costs
    accrual" next to a $310/day pool will reach for the obvious remedy — push
    the override out to match Kalshi — which is exactly the edit that would have
    had the bot quoting through Lilly's print on Aug 5."""
    when = r["cutoff_et"].strftime("%b %d %H:%M")
    if r["severity"] == "risk":
        return ("Kalshi says the call is {} ({}); the bot keeps quoting until {} "
                "ET, straight through the print if Kalshi is right".format(
                    _days_out_phrase(r["days_out"]), r["ticker_date"], when),
                "ACTION: confirm the date on the company IR page or a press "
                "release before the open. If Kalshi is right, stand the event "
                "down NOW (add it to IMM_BLOCKLIST in run_incentive_mm.ps1) — "
                "do not wait for tomorrow's digest.")
    if r["severity"] == "warn":
        return ("Kalshi's ticker date passed {}; if that was a stale-ticker "
                "autofix then quoting on is how this pool is earned".format(
                    _days_out_phrase(r["days_out"])),
                "ACTION: none IF the call has already happened — confirm that "
                "before treating it as safe; an elapsed ticker date is a proxy "
                "for 'stale', not proof of it. If the call is still ahead, this "
                "is the risk case.")
    return ("DO NOT push this out to match Kalshi without a primary source — "
            "that is how the bot ends up quoting through a live print (LLY, "
            "Aug 5). Standing down {}d early (cutoff {} ET) only forfeits "
            "reward accrual; it risks nothing".format(-r["delta"], when),
            "ACTION: none.")


def _unverified_meaning(r: dict) -> tuple:
    """(what it means, what to DO) for one unverified-hour row.

    The action must name a PRIMARY source, not "check Nasdaq": Nasdaq is what
    already failed here, and re-reading a blank field returns the same blank.

    The SAFE branch leads with the PROHIBITION for the same reason the EARLY
    branch of _cutoff_meaning does (:1308), and it matters MORE here: once the
    resolver's fallback assumes before-open, every future guess lands on the safe
    side, so this branch is the section's entire steady-state output. A row that
    ends "confirm only if you want the $X/day back" is an invitation to push a
    07:00 guess out to 16:00 — which is CELH, re-created by hand, by a tired
    reader at 7:10am. The dollar figure is named only as what standing down
    COSTS, never as a reason to move the cutoff."""
    when = r["cutoff_et"].strftime("%b %d %H:%M")
    if r["unsafe"]:
        return ("nobody measured this hour — the resolver had no time from "
                "Nasdaq and SYNTHESIZED {} ET, on the unsafe side. If the "
                "company actually reports before the open, the bot quotes "
                "through the call and the cutoff at {} ET never bites in time"
                .format(r["override_et"].strftime("%H:%M"), when),
                "ACTION: read the company IR page or the release wire and "
                "confirm the call time, then `python imm_earnings_overrides.py "
                "--set {} \"YYYY-MM-DDTHH:MM:00-04:00\"`. If you cannot confirm "
                "it before the open, --set it to 07:00 ET — standing down early "
                "forfeits ${:,.0f}/day and risks nothing.".format(
                    r["event"], r["dpd"]))
    return ("hour was synthesized, not measured, but it landed on the SAFE "
            "(morning) side — the bot stands down at {} ET whether or not the "
            "guess is right. DO NOT push this override later to recover the "
            "${:,.0f}/day: moving a GUESSED cutoff into the afternoon on "
            "anything short of a primary source is exactly the edit that had "
            "the bot quoting through Celsius's 8am call (CELH, Aug 6)".format(
                when, r["dpd"]),
            "ACTION: none. The only thing that justifies a later cutoff here is "
            "the company's own IR page or release wire stating an after-close "
            "time — NOT Nasdaq, which is what returned nothing for this ticker "
            "in the first place. Left alone this forfeits ${:,.0f}/day and "
            "risks nothing.".format(r["dpd"]))


def cutoff_banner(a: dict) -> str:
    """One-line shout for the top of the digest, or "" when nothing is at risk.

    RISK ONLY — deliberately. Two classes are excluded and each exclusion is
    load-bearing:
      * EARLY: standing down before Kalshi's date is the conservative direction.
        Detail block only.
      * WARN (LATE onto an ALREADY-ELAPSED ticker date): this is the exact shape
        imm_earnings_overrides.discover_stale_ticker_events() (:255, added
        2026-08-03 after PGR) is DESIGNED to create — it writes LATE overrides on
        live paying events whose ticker date has passed. BA, HOOD and PGR all
        passed through it while their programs were live, so a warn-triggered
        banner would have been red on roughly one morning in four of earnings
        season for the bot doing exactly the right thing, and the old copy then
        ended the same red sentence with "no print risk". A red box that
        disavows itself trains the reader to skip red boxes, which is precisely
        what would kill the NBIS-class banner. Warn renders in #d9821b in the
        detail table, where it reads as information rather than alarm.

    Silent on failure rather than degraded-with-a-message: this is the one piece
    called straight from build_digest, and an empty banner just falls back to the
    normal headline while the detail block below still reports the problem."""
    try:
        risk = [r for r in a.get("rows") or [] if r["severity"] == "risk"]
        # An unverified hour on the UNSAFE side is the CELH shape and belongs in
        # the banner for the same reason a LATE date does: the bot is quoting on
        # a time nobody checked. The SAFE-side guesses stay out — they fail early
        # by construction, and once the resolver's fallback is fixed to assume
        # before-open, every guess lands there and this banner goes quiet on its
        # own instead of having to be muted by hand.
        unsafe = [r for r in a.get("unverified") or [] if r["unsafe"]]
        if not risk and not unsafe:
            return ""
        parts = []
        if risk:
            # LIVE EXPOSURE is contracts on the book. The pool $/day is named as
            # what standing down COSTS, never as the exposure — it is the
            # argument for the wrong action and must not be the number the
            # reader triages on.
            parts.append(
                "{} override{} run PAST Kalshi's ticker date — the bot quotes "
                "beyond the date Kalshi thinks the call is on. ".format(
                    len(risk), "" if len(risk) == 1 else "s")
                + " ".join(
                    "LIVE EXPOSURE: Kalshi says the {ev} call is {when} ({date})"
                    " — the bot has {cts:,.0f} contracts on the book and keeps "
                    "quoting until {cut} ET. Standing down forfeits "
                    "${dpd:,.0f}/day of reward pool.".format(
                        ev=_short_event(r["event"]),
                        when=_days_out_phrase(r["days_out"]),
                        date=r["ticker_date"], cts=r["contracts"],
                        cut=r["cutoff_et"].strftime("%b %d %H:%M"),
                        dpd=r["dpd"]) for r in risk))
        parts.extend(
            "UNVERIFIED CALL TIME: nobody measured when {ev} reports — the "
            "resolver had no time from Nasdaq and assumed {hh} ET ({lab}). The "
            "bot has {cts:,.0f} contracts on the book and keeps quoting until "
            "{cut} ET; if the call is before the open it quotes straight through "
            "it (CELH, Aug 6). Confirm on the IR page, or --set 07:00 and "
            "forfeit ${dpd:,.0f}/day.".format(
                ev=_short_event(r["event"]),
                hh=r["override_et"].strftime("%H:%M"), lab=r["label"],
                cts=r["contracts"],
                cut=r["cutoff_et"].strftime("%b %d %H:%M"), dpd=r["dpd"])
            for r in unsafe)
        return " ".join(parts)
    except Exception:
        return ""


_CUTOFF_EXPLAINER = (
    "Our override is the CALL / release time (Nasdaq calendar or IR page), held "
    "in run-logs/incentive-mm/event_start_overrides.json and written by "
    "imm_earnings_overrides.py; Kalshi's ticker date, sub_title and program end "
    "are all one internal date, so they never disagree with each other and can "
    "all be wrong together. LATE (override after the ticker date) = the bot "
    "quotes past Kalshi's date and can make markets through a live print — money "
    "at risk, verify against a primary source and blocklist the event if Kalshi "
    "is right. EARLY = the bot stands down first, which forfeits reward accrual "
    "and risks nothing; do NOT edit an override to chase that accrual. On every "
    "conflict that has actually resolved the override was right and the ticker "
    "wrong: KXEARNINGSMENTIONLLY-26AUG07 stood down 2026-08-05 10:50Z against an "
    "Aug 7 ticker, Lilly reported that morning, and Kalshi closed and finalized "
    "the event six hours later. The DATE agreeing is not the all-clear: CELH "
    "matched on the date and was wrong by nine HOURS (Nasdaq had no time flag, "
    "the resolver assumed 4pm, the call was at 8am and the bot quoted through "
    "it), which is what the unverified-call-times block below covers.")


def _prov_coverage_line(a: dict) -> str:
    """How much of the population this check can actually see. "" only when
    there is nothing checked at all.

    ALWAYS printed, never gated on coverage being zero. The old version only
    admitted "NOT yet effective" while coverage was exactly zero; one new
    resolution flipped it to a sentence that listed "written before provenance
    recording" alongside "hand-set" and "env-pinned" as though all three were
    benign human-owned values and then closed on "the rest were measured, not
    guessed". That is reassurance at ~3% coverage, and the unrecorded set is not
    benign — it is precisely CELH's class: a pre-fix 16:00 written by the old
    else-branch is byte-identical to a measured after-close reading.

    It also states that coverage does NOT converge, because it does not:
    imm_earnings_overrides.py's `covered` short-circuit never rewrites an
    existing entry, record_meta only records what the resolver itself writes, and
    the overrides file is never pruned. The old copy promised "it fills in as
    imm_earnings_overrides.py rewrites each entry" — a promise the writer cannot
    keep. Coverage clears only as events churn out of the file, or by seeding the
    sidecar from the resolver's own task-log history."""
    checked = a.get("checked", 0) or 0
    no_prov = a.get("no_prov") or 0
    if not checked:
        return ""
    recorded = max(checked - no_prov, 0)
    if not no_prov:
        return ("(call-time provenance recorded for all {} checked overrides — "
                "every one was measured, not guessed)".format(checked))
    return ("(call-time provenance recorded for {}/{} checked overrides — those "
            "are the ONLY ones this check can see. The other {} were written "
            "before provenance recording, hand-set or env-pinned; a pre-fix "
            "16:00 entry is indistinguishable from a measured after-close "
            "reading and is NOT covered by this check. This does not fill in on "
            "its own — the resolver never rewrites an existing entry — so it "
            "clears only as events churn out of the file.)"
            .format(recorded, checked, no_prov))


def _unverified_lines(a: dict) -> list:
    """Plain-text UNVERIFIED CALL TIMES block.

    Never returns [] while anything is checked: an empty section that looks
    identical whether the check is clean or simply blind to 60 of 62 overrides
    is how a silent regression hides for a month. The coverage line carries that
    distinction and is emitted in BOTH branches."""
    rows = a.get("unverified") or []
    cov = textwrap.wrap(_prov_coverage_line(a), width=76,
                        initial_indent="  ", subsequent_indent="  ")
    if not rows:
        return cov
    out = ["UNVERIFIED CALL TIMES — {} live override(s) whose HOUR was "
           "synthesized, not measured".format(len(rows))]
    for r in rows:
        why, action = _unverified_meaning(r)
        out.append("  {:34s} ours {} ET  cutoff {} ET  ticker {} {}  "
                   "{} mkts  {:,.0f} cts  ${:,.2f}/day{}".format(
                       _short_event(r["event"])[:34],
                       r["override_et"].strftime("%m-%d %H:%M"),
                       r["cutoff_et"].strftime("%m-%d %H:%M"),
                       r["ticker_date"].isoformat()[5:],
                       _days_out_phrase(r["days_out"]), r["mkts"],
                       r["contracts"], r["dpd"],
                       "  <== UNSAFE SIDE" if r["unsafe"] else ""))
        for para in (why, action):
            out.extend(textwrap.wrap(para, width=76, initial_indent="      ",
                                     subsequent_indent="      "))
    out.extend(cov)
    return out


def _unverified_html(a: dict) -> str:
    """HTML twin of _unverified_lines. Never raises (caller is guarded)."""
    rows = a.get("unverified") or []
    if not rows:
        # the unwrapped sentence, not the 76-col text block re-joined
        txt = _prov_coverage_line(a)
        return ('<div style="color:#888;font-size:11px;margin-top:4px">{}</div>'
                .format(txt)) if txt else ""
    h = ['<div style="font-size:14px;font-weight:600;margin:12px 0 4px">'
         'Unverified call times &mdash; {} override(s) whose HOUR was '
         'synthesized, not measured</div>'.format(len(rows)),
         '<table style="border-collapse:collapse">',
         '<tr style="background:#f0f0f0;font-weight:600">'
         '<td style="{0}">EVENT</td><td style="{1}">OUR CALL TIME (ET)</td>'
         '<td style="{1}">CUTOFF (ET)</td><td style="{1}">KALSHI SAYS</td>'
         '<td style="{1}">MKTS</td><td style="{1}">CTS ON BOOK</td>'
         '<td style="{1}">POOL $/DAY</td></tr>'.format(TDL, TD)]
    for i, r in enumerate(rows):
        colour = "#c0392b" if r["unsafe"] else "#777"
        # The action line is bold ONLY when there is an action. On a safe-side
        # row the correct action is to do nothing, and rendering "ACTION: none"
        # as the loudest text in the row is how a no-op becomes a to-do.
        act_style = ("color:#333;font-weight:600" if r["unsafe"]
                     else "color:#777;font-weight:400")
        why, action = _unverified_meaning(r)
        h.append(
            '<tr style="background:{bg}"><td style="{tdl}">{ev}'
            '<div style="color:{col};font-size:11px">{why}</div>'
            '<div style="{acts};font-size:11px">{act}</div>'
            '</td>'
            '<td style="{td}">{ov}<div style="color:{col};font-size:11px">'
            'guessed &mdash; {lab}</div></td>'
            '<td style="{td}">{cut}</td>'
            '<td style="{td}">{tick}<div style="color:#999;font-size:11px">'
            '{when}</div></td>'
            '<td style="{td}">{mkts}</td><td style="{td}">{cts:,.0f}</td>'
            '<td style="{td}">{dpd:,.2f}</td></tr>'.format(
                bg="#fafafa" if i % 2 else "#fff", tdl=TDL, td=TD, col=colour,
                acts=act_style,
                ev=_short_event(r["event"]), why=why, act=action,
                ov=r["override_et"].strftime("%b %d %H:%M"), lab=r["label"],
                cut=r["cutoff_et"].strftime("%b %d %H:%M"),
                tick=r["ticker_date"].isoformat(),
                when=_days_out_phrase(r["days_out"]), mkts=r["mkts"],
                cts=r["contracts"], dpd=r["dpd"]))
    h.append("</table>")
    cov = _prov_coverage_line(a)
    if cov:
        h.append('<div style="color:#888;font-size:11px;margin-top:4px">{}</div>'
                 .format(cov))
    return "".join(h)


def _cutoff_audit_text(a: dict):
    """Plain-text CUTOFF AUDIT block. Returns a list of lines; never raises."""
    try:
        if a.get("error"):
            return ["CUTOFF AUDIT — could not compute ({}); override-vs-Kalshi "
                    "date checking did not run this morning".format(a["error"]), ""]
        # Suppressed = HAD disagreed, but the programs have ended. Past tense
        # and explicitly closed out, so it can never read as a live count: the
        # old copy said "all agree" and then "5 disagree" two lines apart, which
        # forces a re-read on the exact morning the section should cost zero
        # attention. Reported at all (rather than dropped) because silence would
        # be indistinguishable from a filter that had quietly eaten a live row.
        supp = ("  ({} resolved event(s) HAD disagreed; their programs have "
                "ended — history, no action: {})".format(
                    len(a["dead"]),
                    ", ".join(_short_event(e) for e in a["dead"][:6])
                    + (", ..." if len(a["dead"]) > 6 else ""))
                if a["dead"] else "")
        skipped = ("  ({} override(s) not comparable: {} carry no calendar day "
                   "in the ticker, {} are schedule-resolved series where a LATE "
                   "override means a postponed game)".format(
                       a["no_day"] + a["excluded"], a["no_day"], a["excluded"])
                   if (a["no_day"] or a["excluded"]) else "")
        if not a["rows"]:
            out = ["CUTOFF AUDIT: {} live overrides checked, all agree with "
                   "their Kalshi ticker date.".format(a["checked"])]
            for extra in (supp, skipped):
                if extra:
                    out.append(extra)
            # Date agreement is NOT the all-clear: CELH agreed on the date and
            # was still wrong by nine hours. The hour block renders here too.
            return out + _unverified_lines(a) + [""]
        out = ["CUTOFF AUDIT — our call-time override vs Kalshi's ticker date "
               "({} of {} checked disagree)".format(len(a["rows"]), a["checked"]),
               # every header MUST fit its field — "OUR CALL (ET)" is 13 chars
               # in a 12-wide column and silently shoved the whole header row
               # one column right of its data.
               "{:28s} {:>12s} {:>14s} {:>9s} {:>5s} {:>6s} {:>11s}".format(
                   "EVENT", "OUR CALL ET", "KALSHI SAYS", "DELTA", "MKTS",
                   "CTS", "POOL $/DAY")]
        for r in a["rows"]:
            why, action = _cutoff_meaning(r)
            out.append("{:28s} {:>12s} {:>14s} {:>9s} {:>5d} {:>6,.0f} {:>11,.2f}"
                       "{}".format(
                           _short_event(r["event"])[:28],
                           r["override_et"].strftime("%m-%d %H:%M"),
                           "{} {}".format(r["ticker_date"].isoformat()[5:],
                                          _days_out_phrase(r["days_out"]))[:14],
                           "{:+d}d {}".format(
                               r["delta"],
                               "LATE" if r["delta"] > 0 else "EARLY"),
                           r["mkts"], r["contracts"], r["dpd"],
                           "  <== RISK" if r["severity"] == "risk" else ""))
            for para in ("{} | {}".format(why, r["hour_label"]), action):
                out.extend(textwrap.wrap(para, width=76,
                                         initial_indent="      ",
                                         subsequent_indent="      "))
        out.append("  POOL $/DAY is the reward at stake if we stand down, NOT "
                   "the exposure; CTS is contracts on the book.")
        out.extend(_unverified_lines(a))
        for extra in (supp, skipped):
            if extra:
                out.append(extra)
        out.extend(textwrap.wrap(_CUTOFF_EXPLAINER, width=78,
                                 initial_indent="  ", subsequent_indent="  "))
        out.append("")
        return out
    except Exception as e:
        return ["CUTOFF AUDIT — render failed ({})".format(repr(e)), ""]


def _cutoff_audit_html(a: dict) -> str:
    """HTML twin of _cutoff_audit_text. Never raises."""
    try:
        head = ('<div style="font-size:15px;font-weight:600;margin:16px 0 4px">'
                'Cutoff audit &mdash; our call time vs Kalshi\'s ticker date</div>')
        if a.get("error"):
            return (head + '<div style="color:#c0392b;font-size:12px">Could not '
                    'compute ({}) &mdash; override-vs-Kalshi date checking did '
                    'not run this morning.</div>'.format(a["error"]))
        supp = (' &nbsp;{} resolved event(s) HAD disagreed; their programs have '
                'ended &mdash; history, no action ({}).'.format(
                    len(a["dead"]),
                    ", ".join(_short_event(e) for e in a["dead"][:6])
                    + (", &hellip;" if len(a["dead"]) > 6 else ""))
                if a["dead"] else "")
        skipped = (' &nbsp;{} override(s) not comparable: {} carry no calendar '
                   'day in the ticker, {} are schedule-resolved series where a '
                   'LATE override means a postponed game.'.format(
                       a["no_day"] + a["excluded"], a["no_day"], a["excluded"])
                   if (a["no_day"] or a["excluded"]) else "")
        if not a["rows"]:
            return (head + '<div style="color:#0a7a2f;font-size:12px">{} live '
                    'overrides checked &mdash; all agree with their Kalshi '
                    'ticker date.{}{}</div>'.format(a["checked"], supp, skipped)
                    + _unverified_html(a))
        h = [head, '<table style="border-collapse:collapse">',
             '<tr style="background:#f0f0f0;font-weight:600">'
             '<td style="{0}">EVENT</td><td style="{1}">OUR CALL TIME (ET)</td>'
             '<td style="{1}">KALSHI SAYS</td><td style="{1}">DELTA</td>'
             '<td style="{1}">DIR</td><td style="{1}">MKTS</td>'
             '<td style="{1}">CTS ON BOOK</td>'
             '<td style="{1}">POOL $/DAY</td></tr>'.format(TDL, TD)]
        for i, r in enumerate(a["rows"]):
            colour = {"risk": "#c0392b", "warn": "#d9821b",
                      "info": "#777"}[r["severity"]]
            why, action = _cutoff_meaning(r)
            h.append(
                '<tr style="background:{bg}"><td style="{tdl}">{ev}'
                '<div style="color:{col};font-size:11px">{why}</div>'
                '<div style="color:#333;font-size:11px;font-weight:600">{act}'
                '</div></td>'
                '<td style="{td}">{ov}<div style="color:#999;font-size:11px">'
                '{hour}</div></td>'
                '<td style="{td}">{tick}<div style="color:{col};font-size:11px">'
                '{when}</div></td>'
                # TD already ends in ';' — no extra one, or the declaration
                # renders as 'text-align:right;;color:...'
                '<td style="{td}color:{col};font-weight:700">{d:+d}d</td>'
                '<td style="{td}color:{col};font-weight:700">{dir}</td>'
                '<td style="{td}">{mkts}</td><td style="{td}">{cts:,.0f}</td>'
                '<td style="{td}">{dpd:,.2f}</td>'
                '</tr>'.format(
                    bg="#fafafa" if i % 2 else "#fff", tdl=TDL, td=TD,
                    col=colour, ev=_short_event(r["event"]),
                    why=why, act=action, hour=r["hour_label"],
                    ov=r["override_et"].strftime("%b %d %H:%M"),
                    tick=r["ticker_date"].isoformat(),
                    when=_days_out_phrase(r["days_out"]), d=r["delta"],
                    dir="LATE" if r["delta"] > 0 else "EARLY",
                    mkts=r["mkts"], cts=r["contracts"], dpd=r["dpd"]))
        h.append("</table>")
        h.append('<div style="color:#888;font-size:11px;margin-top:4px">POOL '
                 '$/DAY is the reward at stake if we stand down &mdash; not the '
                 'exposure. CTS ON BOOK is the exposure.</div>')
        h.append(_unverified_html(a))
        h.append('<div style="color:#666;font-size:12px;margin-top:6px;'
                 'border-left:3px solid #d9a441;padding-left:8px">{}{}{}</div>'
                 .format(_CUTOFF_EXPLAINER, supp, skipped))
        return "".join(h)
    except Exception as e:
        return ('<div style="color:#c0392b;font-size:12px">Cutoff audit render '
                'failed ({}).</div>'.format(repr(e)))


def finecon_section(state, w, today_ct):
    """(text_lines, html) — the Finance/Econ sweep tracker (Jack 2026-09-04
    "make sure im able to track performance of these"). Three layers, most
    trustworthy last: current members with the bot's period-to-date accrual
    ESTIMATE and net inventory; the group's past-day/week TRADING result
    (fill-attributed, from the same per-event windows the events table
    uses); and Kalshi-CREDITED rewards on group events from the recon
    ledger — the only number that is actual paid money."""
    fin = getattr(imm, "FINECON_SERIES", frozenset())
    if not fin:
        return [], ""

    def _is_fin(ticker_or_event):
        return ticker_or_event.split("-")[0] in fin

    members = sorted(t for t in (state.get("selected_tickers") or [])
                     if _is_fin(t))
    accrued = state.get("accrued_est") or {}
    own_pos = state.get("own_pos") or {}
    top_n = getattr(imm, "FINECON_TOP_N", 0)

    def _win_pnl(key):
        evs = (w.get(key) or {}).get("events") or {}
        tot, n = 0.0, 0
        for ev, e in evs.items():
            if _is_fin(ev):
                tot += e["realized"] + e["settle"] + e["unrealized"]
                n += 1
        return tot, n

    day_pnl, day_n = _win_pnl("day")
    week_pnl, week_n = _win_pnl("week")
    rows, _calib = load_credit_ledger()
    fin_credits = [(d, ev, a) for d, ev, a in rows if _is_fin(ev)]
    cred_life = sum(a for _d, _e, a in fin_credits)
    week_dates = {(today_ct - timedelta(days=i)).isoformat()
                  for i in range(0, 8)}
    cred_week = sum(a for d, _e, a in fin_credits if d in week_dates)
    acc_sum = sum(_f(accrued.get(t)) for t in members)

    # Daily over-cap openings (Jack 2026-09-05): show today's burn when the
    # state's counter day is current — after a quiet midnight the bot may
    # not have rolled the day yet, so a stale day reads as 0 used.
    openings_cap = getattr(imm, "FINECON_DAILY_OPENINGS", 0)
    used = (int(_f(state.get("finecon_admits_today")))
            if state.get("finecon_admit_day") == today_ct.isoformat() else 0)

    L = []
    L.append("FINECON SWEEP (Finance/Econ, top-{} by ROI, "
             "quote-to-completion)".format(top_n))
    L.append("Quoting {}/{} slots (+{}/{} daily openings used); est accrued "
             "this period ${:,.2f} across members.".format(
                 len(members), top_n, used, openings_cap, acc_sum))
    if members:
        L.append("{:36s} {:>9s} {:>7s}".format("MEMBER", "ACCRUED$", "POS"))
        for t in members:
            L.append("{:36s} {:>9.2f} {:>+7.0f}".format(
                t[:36], _f(accrued.get(t)), _f(own_pos.get(t))))
    else:
        L.append("  (no members quoting right now)")
    L.append("Group trading P&L: past day {:+,.2f} ({} events), past week "
             "{:+,.2f} ({} events).".format(day_pnl, day_n, week_pnl, week_n))
    L.append("Kalshi-CREDITED rewards on group events: past 7d ${:,.2f}, "
             "all-time ${:,.2f}{}.".format(
                 cred_week, cred_life,
                 "" if fin_credits else " (none in ledger yet — credits land "
                 "1-2d after each period ends)"))

    h = ['<div style="font-size:15px;font-weight:600;margin:14px 0 4px">'
         'Finecon sweep <span style="color:#888;font-weight:400">'
         '&mdash; top-{} by ROI, quote-to-completion</span></div>'.format(top_n)]
    h.append('<div style="color:#555;font-size:13px;margin-bottom:4px">'
             'quoting <b>{}/{}</b> slots &nbsp;&middot;&nbsp; est accrued '
             'this period ${:,.2f} &nbsp;&middot;&nbsp; trading P&amp;L day '
             '{} / week {} &nbsp;&middot;&nbsp; Kalshi-credited 7d '
             '<b>${:,.2f}</b> / all-time <b>${:,.2f}</b></div>'.format(
                 len(members), top_n, acc_sum, _pnl_span(day_pnl),
                 _pnl_span(week_pnl), cred_week, cred_life))
    if members:
        h.append('<table style="border-collapse:collapse">')
        h.append('<tr style="background:#f0f0f0;font-weight:600">'
                 '<td style="{0}">MEMBER</td><td style="{1}">ACCRUED$ (est)</td>'
                 '<td style="{1}">POS</td></tr>'.format(TDL, TD))
        for i, t in enumerate(members):
            h.append('<tr style="background:{0}"><td style="{1}">{2}</td>'
                     '<td style="{3}">{4:,.2f}</td>'
                     '<td style="{3}">{5:+,.0f}</td></tr>'.format(
                         "#fafafa" if i % 2 else "#fff", TDL, t, TD,
                         _f(accrued.get(t)), _f(own_pos.get(t))))
        h.append('</table>')
    else:
        h.append('<div style="color:#666;font-size:13px">no members quoting '
                 'right now.</div>')
    h.append('<div style="color:#888;font-size:12px;margin-top:4px">ACCRUED '
             'is the bot&rsquo;s estimator, period-to-date; CREDITED is '
             'actual Kalshi money from the recon ledger (lands 1&ndash;2d '
             'after each period ends). Members ride to natural completion, '
             'so a slot only frees at settlement/cutoff.</div>')

    # OPEN SCAN tier (Jack 2026-09-05 "extend the opportunistic IMM with 15
    # slots and 5 to scan all markets"): the machine-screened all-market
    # tier, same walk/openings/quote-to-completion; membership + counters
    # come straight from the bot's persisted state.
    scan_top = getattr(imm, "SCAN_TOP_N", 0)
    if scan_top > 0:
        scan_members = sorted(state.get("scan_members") or [])
        s_cap = getattr(imm, "SCAN_DAILY_OPENINGS", 0)
        s_used = (int(_f(state.get("scan_admits_today")))
                  if state.get("scan_admit_day") == today_ct.isoformat() else 0)
        s_halted = state.get("scan_halt_day") == today_ct.isoformat()
        s_evicted = len(state.get("scan_evicted_events") or {})
        s_acc = sum(_f(accrued.get(t)) for t in scan_members)
        L.append("OPEN SCAN (all-market tier, top-{} by ROI, machine-screened "
                 "for adverse selection, quote-to-completion)".format(scan_top))
        L.append("Quoting {}/{} slots (+{}/{} daily openings used){}; {} "
                 "event(s) evicted by tripwires to date; est accrued this "
                 "period ${:,.2f} across members.".format(
                     len(scan_members), scan_top, s_used, s_cap,
                     " — HALTED today (loss budget)" if s_halted else "",
                     s_evicted, s_acc))
        if scan_members:
            L.append("{:36s} {:>9s} {:>7s}".format("MEMBER", "ACCRUED$", "POS"))
            for t in scan_members:
                L.append("{:36s} {:>9.2f} {:>+7.0f}".format(
                    t[:36], _f(accrued.get(t)), _f(own_pos.get(t))))
        else:
            L.append("  (no scan members quoting right now)")
        h.append('<div style="font-size:15px;font-weight:600;margin:14px 0 4px">'
                 'Open scan <span style="color:#888;font-weight:400">&mdash; '
                 'all-market tier, top-{} by ROI, machine-screened, '
                 'quote-to-completion</span></div>'.format(scan_top))
        h.append('<div style="color:#555;font-size:13px;margin-bottom:4px">'
                 'quoting <b>{}/{}</b> slots &nbsp;&middot;&nbsp; +{}/{} daily '
                 'openings used{} &nbsp;&middot;&nbsp; {} event(s) evicted by '
                 'tripwires &nbsp;&middot;&nbsp; est accrued this period '
                 '${:,.2f}</div>'.format(
                     len(scan_members), scan_top, s_used, s_cap,
                     ' &nbsp;&middot;&nbsp; <b style="color:#b00">HALTED today'
                     ' (loss budget)</b>' if s_halted else "",
                     s_evicted, s_acc))
        if scan_members:
            h.append('<table style="border-collapse:collapse">')
            h.append('<tr style="background:#f0f0f0;font-weight:600">'
                     '<td style="{0}">MEMBER</td><td style="{1}">ACCRUED$ (est)'
                     '</td><td style="{1}">POS</td></tr>'.format(TDL, TD))
            for i, t in enumerate(scan_members):
                h.append('<tr style="background:{0}"><td style="{1}">{2}</td>'
                         '<td style="{3}">{4:,.2f}</td>'
                         '<td style="{3}">{5:+,.0f}</td></tr>'.format(
                             "#fafafa" if i % 2 else "#fff", TDL, t, TD,
                             _f(accrued.get(t)), _f(own_pos.get(t))))
            h.append('</table>')
        else:
            h.append('<div style="color:#666;font-size:13px">no scan members '
                     'quoting right now.</div>')
    return L, "".join(h)


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
    # pos (own_book, above) gives the audit its only real exposure number;
    # everything else it reports is reward pool. never raises — see docstring.
    audit = cutoff_audit(client, now_utc, pos)

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
    _banner = cutoff_banner(audit)
    if _banner:
        L.append("!! " + _banner)
        L.append("")
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
    L.extend(_cutoff_audit_text(audit))
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
    fin_L, fin_html = finecon_section(state, w, today_ct)
    if fin_L:
        L.append("")
        L.extend(fin_L)
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
    if _banner:
        h.append('<div style="background:#fdecea;border-left:4px solid #c0392b;'
                 'color:#8e2b21;padding:8px 10px;margin:8px 0;font-weight:600">'
                 '{}</div>'.format(_banner))
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

    h.append(_cutoff_audit_html(audit))

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
    if fin_html:
        h.append(fin_html)
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
