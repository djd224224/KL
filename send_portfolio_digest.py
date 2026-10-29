#!/usr/bin/env python3
r"""
send_portfolio_digest.py — 7:00 AM ET whole-account portfolio email.

One email covering the ENTIRE Kalshi account (every bot + manual trades):
a chart of daily profit, realized and estimated after rewards are paid out
(since 2026-10-10; it was account value, which jumps with every deposit),
the IMM's risk-controls table right under it (since 2026-10-03), then the
biggest movers since the prior morning by family / event (markets dropped
2026-10-10), grouped exactly as the IMM dashboard's Drivers table groups
them (imm_dashboard.family_of, since 2026-10-03; settled and unrealized
alike, ranked by the size of the move, realized and mark-to-mid split out).
(The "Settled since yesterday, by series" table was removed 2026-09-29.)

Under the day change, WHAT TO DO (since 2026-10-19, Jack: "share
recommendations on what I should do based on prior day trading
performance"; centred on yesterday since 2026-10-22): imm_advice() reads
the IMM dashboard's per-market days -- yesterday against the last month and
the last 7 full days, each P&L split into rewards, its own fills (and how
they lost: picked off, at settlement, or trending) and the positions it
already held -- and says what moved yesterday, then numbered
recommendations with their why: SHRINK a family, PAUSE / SHRINK a series
or a specific event whose quoting loses persistently, HOLD / TRIM a
family's open losers by what is left to lose, NO CHANGE on one bad day,
DOUBLE DOWN (x1.5) on a line that earns with light fills and was not just
sized up (since 2026-10-29), KEEP what works;
then what is NEW THIS WEEK (lines, series and events first seen) and, at
the end of the email, EVERY LINE (MLB, Red Rocks, ...: a league, a group
or a series) with its call (since 2026-10-25). Kill: PF_ADVICE=0.

Then the IMM bot's section (since 2026-10-02, when its own 7:10 email was
cut): send_imm_digest.py --section-out, run as a child process (imm_section),
with the dashboard's yesterday / 7-day / 30-day figures; its PICK-OFF WINDOW
flag goes onto this subject, and its risk block (risk_text / risk_html) is
the one placed under the chart. A failed section is one line saying so,
never a held email. Kill: PF_IMM_SECTION=0 or --no-imm.

Day P&L per event (since 2026-09-29, replay_day): every fill and settlement
since the prior morning replayed per market, from the prior snapshot's own
positions and marks to today's: the cash those produced plus today's value
minus the prior value, split average-cost into realized (contracts that left
the book) and unrealized (contracts still held). The day change splits
exactly into that trading, the cash with no trade or settlement behind it
(reward credits; deposits apart), and Kalshi's valuation vs our mids. The
old event-rollup diff (positions endpoint realized + settlement lifetime
totals) double-counted events traded both ways that then settled.

Account value = cash + Kalshi's valuation of the open event contracts +
the perpetual-futures account's equity (the margin API, Jack 2026-09-28) +
the numbered subaccounts (10/9 on). The subaccounts are API-only, so the
Kalshi app shows the account value less them; the headline gives that
figure beside it (Jack 2026-10-28: "app shows 58.5k across main account and
perps", the email $59.6k).
The headline also carries, in parentheses, an ESTIMATE of the value once the
liquidity rewards already earned are paid out (imm_reward_recon.
unpaid_estimate: accrual in program periods still running or ended since
midnight ET yesterday, $1 floor per market per period, x each family's
paid/modelled ratio). Kalshi has no credits endpoint, so it is a model.
Under it, the TOTAL PROFIT (since 2026-10-04): the account value less every
dollar deposited plus every dollar withdrawn since the account opened
(fetch_transfers: /portfolio/deposits and /portfolio/withdrawals, read in
full each morning), with the same after-rewards estimate. The day change
leaves deposits and withdrawals out too: it is the day's change in total
profit, so the day's transfers are the move in the all-time totals the two
mornings recorded (day_transfers), not the transfers Kalshi dates inside
the day. The subject carries both, with the day's trading and reward
credits: "portfolio 2026-10-05: profit +$18.1k (day +$1.1k: trading
-$0.8k, rewards +$2.4k)".

State lives in portfolio_daily\:
    pf_snapshot_YYYY-MM-DD.json  - per-event E components (diff baseline)
    balance_history.csv          - date,cash,positions_value,equity,... (chart,
                                   with the deposits read each morning)
Snapshots/history are written on the real morning run (at build time, so a
failed send still baselines tomorrow's diff); --test and --dry-run never
write them. First ever run has no baseline and reports P&L to date instead.

Idempotent via a sent-marker in run-logs\portfolio-digest\. Same
Modern-Standby retry loops as send_daily_digest.py (task can fire mid-sleep
with the radio off). Credentials: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD
(env, falling back to HKCU\Environment); Kalshi key per load_private_key.
Recipient: PF_DIGEST_TO (default jackdu224@gmail.com) — email only, no SMS.
"""

import argparse
import csv
import glob
import json
import os
import re
import smtplib
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


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
import pytz  # noqa: E402
from crypto_touch_mm import build_client, log  # noqa: E402
import imm_dashboard as dash  # noqa: E402  (family_of: stdlib-only import)

ET = pytz.timezone("US/Eastern")
KL_DIR = r"C:\Users\jackd\Documents\KL"
DATA_DIR = os.path.join(KL_DIR, "portfolio_daily")
LOG_DIR = os.path.join(KL_DIR, "run-logs", "portfolio-digest")
HISTORY_CSV = os.path.join(DATA_DIR, "balance_history.csv")
# Settlements window: two mornings (+2h), so a settlement that raced the
# prior snapshot (feed lag) or a skipped day still gets picked up and
# flagged; already-baselined settlements diff to ~0 and drop out anyway.
LOOKBACK_H = float(os.environ.get("PF_LOOKBACK_H", "50"))
RECIPIENTS = [r.strip() for r in os.environ.get(
    "PF_DIGEST_TO", "jackdu224@gmail.com").split(",") if r.strip()]

# Chart + table colors (dataviz reference palette, light mode fixed for email).
C_EQUITY = "#2a78d6"     # series 1 blue — realized profit
C_EST = "#eb6834"        # series 2 orange — estimated profit (dashed)
C_INK = "#0b0b0b"
C_INK2 = "#52514e"
C_MUTED = "#898781"
C_GRID = "#e1e0d9"
C_AXIS = "#c3c2b7"
C_POS = "#0a7a2f"        # matches the other digests' green/red
C_NEG = "#c0392b"

TD = 'padding:5px 12px;border:1px solid #ddd;text-align:right;white-space:nowrap;'
TDL = 'padding:5px 12px;border:1px solid #ddd;text-align:left;'


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("·", "&middot;"))


def _pnl_span(v: float) -> str:
    color = C_POS if v > 0.005 else (C_NEG if v < -0.005 else "#777")
    return f'<span style="color:{color}">{v:+,.2f}</span>'


def _signed_usd(v: float) -> str:
    """+$1,234.56 / -$1,234.56."""
    return f"{'-' if v < -0.005 else '+'}${abs(v):,.2f}"


def _signed_k(v: float) -> str:
    """+$18.2k / -$0.7k: the subject line's dollars, in thousands."""
    k = round(v / 1000.0, 1)
    return "$0.0k" if k == 0 else f"{'-' if k < 0 else '+'}${abs(k):,.1f}k"


def event_from_ticker(ticker: str) -> str:
    """Fallback only (used when the markets endpoint can't tell us): Kalshi
    market tickers are <event>-<strike-suffix>; single-segment tickers are
    their own event."""
    return ticker.rsplit("-", 1)[0] if ticker.count("-") >= 2 else ticker


# Families as the IMM dashboard's Drivers table files them (Jack 2026-10-03:
# "mirror the family/event/market in the table in imm_dashboard.html#drivers
# when grouping families in the table in the email e.g. AI & tech, Company
# KPIs, Crypto, Elections"). The same function, imm_dashboard.family_of, fed
# the same inputs: Kalshi's series category from the bot's scan_series_meta
# places a series no name rule knows, exactly as the dashboard reads it. It
# replaces the fleet roll-ups ("High temps (KXHIGH*)", "NFL (KXNFL*)", ...)
# this table used since 8/14. The dashboard's fallback election list matched
# incentive_mm.election_series on all 362 series in the bot's book on 10/3,
# so the bot itself is not imported here.
IMM_STATE_PATH = os.path.join(KL_DIR, "run-logs", "incentive-mm", "imm_state.json")
_SERIES_CATS = None                # series -> Kalshi category, loaded once


def series_categories() -> dict:
    global _SERIES_CATS
    if _SERIES_CATS is None:
        try:
            with open(IMM_STATE_PATH, encoding="utf-8") as f:
                meta = json.load(f).get("scan_series_meta") or {}
            _SERIES_CATS = {k: str((v or {}).get("category") or "")
                            for k, v in meta.items()}
        except (OSError, ValueError, AttributeError):
            _SERIES_CATS = {}
    return _SERIES_CATS


def family_and_group(event_ticker: str):
    """(family, group) of an event ticker, the dashboard's two labels: the
    family is the table's top level (Econ & rates, Crypto, ...), the group
    the label the dashboard prints beside an event (CPI & inflation, Treasury
    yields, ...; the series itself where no group applies)."""
    series = event_ticker.split("-", 1)[0]
    return dash.family_of(series, series_categories().get(series, ""))


def family_for(event_ticker: str) -> str:
    return family_and_group(event_ticker)[0]


# The day by bot (Jack 2026-10-22: "in the email, clearly show daily only
# split between IMM vs crypto vs weather vs other"). Each event row of the
# day goes to the bot that holds it: the IMM when its own book on the
# dashboard has the event (a position now, or a day row yesterday or today:
# the dashboard tracks the IMM's own fills only, so its rain, snow and BTC-vs
# markets stay the IMM's), else the crypto fleets for a crypto asset's
# series, the weather bots for the temperature dailies, and "other" for the
# rest (the cloud mention bots, hand trades). Reward credits are the IMM's
# (Kalshi's liquidity incentives), the perpetuals the crypto side's, and each
# numbered subaccount its own bot's. Kalshi's pricing vs our mids is a gap
# between two valuations, no bot's P&L, so it keeps its own line and the
# lines still add up to the day change.
BY_BOT_ENABLED = os.environ.get("PF_BY_BOT", "1") != "0"
BOTS = ("IMM", "Crypto", "Weather", "Other")
CRYPTO_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC")
WEATHER_PREFIXES = ("KXHIGH", "KXLOWT", "KXTEMP")   # high_temp / low_temp bots, hourly temps
SUBACCOUNT_BOTS = {1: "Other",      # nfl_snipe_bot, since 10/09
                   2: "Weather"}    # the KXTEMPMIAH hourly-temp live test (halted 10/16)


def imm_book_events(D, today_str: str):
    """Event tickers in the IMM bot's own book across the digest's 7am -> 7am
    window: a position now, or a dashboard day row (own fills, re-marks,
    settlements, rewards) on the ET day before today_str or on today_str.
    None without a dashboard."""
    if not D:
        return None
    mk = D.get("markets") or {}
    days = D.get("days") or {}
    y = (datetime.strptime(today_str, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    tickers = {t for t, m in mk.items() if abs(_f((m or {}).get("pos"))) > 1e-9}
    for d in (y, today_str):
        tickers |= set((days.get(d) or {}).get("m") or {})
    return {(mk.get(t) or {}).get("ev") or event_from_ticker(t) for t in tickers}


def bot_of_event(event_ticker: str, imm_events) -> str:
    """Which of BOTS a day row belongs to (see BY_BOT_ENABLED above)."""
    if imm_events and event_ticker in imm_events:
        return "IMM"
    series = event_ticker.split("-", 1)[0]
    if any(series.startswith("KX" + a) for a in CRYPTO_ASSETS):
        return "Crypto"
    if series.startswith(WEATHER_PREFIXES):
        return "Weather"
    return "Other"


# ----------------------------------------------------------------------------
# Data pulls
# ----------------------------------------------------------------------------

def fetch_unsettled_positions(client):
    """All unsettled positions account-wide. Returns (events, mkt_pos):
    events: ev -> {"realized": $, "fees": $, "exposure": $};
    mkt_pos: ticker -> {"pos": contracts, "cost": $ paid for them}."""
    events, mkt_pos = {}, {}
    cursor = None
    pages = 0
    while True:
        resp = client.get_positions(limit=200, cursor=cursor,
                                    settlement_status="unsettled")
        for ev in resp.get("event_positions") or []:
            t = ev.get("event_ticker")
            if t:
                events[t] = {"realized": _f(ev.get("realized_pnl_dollars")),
                             "fees": _f(ev.get("fees_paid_dollars")),
                             "exposure": _f(ev.get("event_exposure_dollars"))}
        for p in resp.get("market_positions") or []:
            pos = _f(p.get("position"))
            if abs(pos) > 0.0001 and p.get("ticker"):
                mkt_pos[p["ticker"]] = {"pos": pos,
                                        "cost": _f(p.get("market_exposure_dollars"))}
        pages += 1
        cursor = resp.get("cursor") or None
        if not cursor or pages > 100:
            break
    log(f"positions: {len(events)} unsettled events, "
        f"{len(mkt_pos)} open market positions ({pages} pages)")
    return events, mkt_pos


def collateral_returned(events, mkt_pos) -> float:
    """Collateral Kalshi has handed back on open positions: the markets'
    exposure summed less their events' exposure. Across mutually exclusive
    outcomes (vote-share buckets, awards, "who finishes 4th") at most one NO
    can lose, so Kalshi releases the guaranteed part up front, and takes it
    back when the positions change or settle (2026-10-09: Quebec 4th place,
    NO on three parties, settled with cash $100 under its revenue). The cash
    moves with no fill or settlement behind it, and Kalshi's portfolio_value
    is the positions at their last trade less this amount (10/9 19:37Z:
    $21,285.24 + $2,859.57 = $24,144.81 against $24,143.54 at the last
    trades), so a release lifts the cash and lowers the valuation alike: no
    gain, and no reward. Before 10/10 it read as reward credits (Jack: "the
    reward credit is wrong in the email")."""
    return round(sum(r["cost"] for r in mkt_pos.values())
                 - sum(e.get("exposure", 0.0) for e in events.values()), 2)


def event_refunds(events, mkt_pos, ev_of) -> dict:
    """event -> the collateral Kalshi has refunded on its open positions:
    the event's markets' exposure less the event's own exposure, i.e.
    collateral_returned event by event (Jack 2026-10-22: "Make email movers
    table treat refunds the same as kalshi ui"). Markets go to their events
    by the markets endpoint's event_ticker (ev_of); a ticker-prefix split had
    read negatives on 10/09. On 10/22 13:53 ET: 101 events, $6,295.95, the
    account figure to the cent, none negative."""
    cost = {}
    for tk, r in mkt_pos.items():
        ev = ev_of(tk)
        cost[ev] = cost.get(ev, 0.0) + r["cost"]
    out = {}
    for ev, c in cost.items():
        if ev in events:
            ref = round(c - _f(events[ev].get("exposure")), 2)
            if abs(ref) >= 0.005:
                out[ev] = ref
    return out


def net_of_refund(realized, value_d, value_now, refund_now, refund_prior):
    """A movers row the way Kalshi books a refund: the open value net of the
    event's refund (as Kalshi's positions value), the refund's cash in
    realized as it comes and goes back; the day (realized + value_d) is
    unchanged. refund_prior None (the prior morning did not record refunds)
    leaves realized / unrealized as they are and only nets the open value."""
    if refund_prior is not None:
        d = refund_now - refund_prior
        realized, value_d = realized + d, value_d - d
    return round(realized, 2), round(value_d, 2), round(value_now - refund_now, 2)


def fetch_recent_settlements(client, cutoff_utc: datetime):
    """Settlement records newer than cutoff (newest-first API; stop early)."""
    out = []
    cursor = None
    pages = 0
    while True:
        resp = client.get_portfolio_settlements(limit=200, cursor=cursor)
        batch = resp.get("settlements") or []
        done = False
        for s in batch:
            ts = s.get("settled_time") or ""
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt < cutoff_utc:
                done = True
                break
            out.append(s)
        pages += 1
        cursor = resp.get("cursor") or None
        if done or not cursor or not batch or pages > 50:
            break
    log(f"settlements: {len(out)} in the last {LOOKBACK_H:.0f}h")
    return out


def fetch_perps(client):
    """Kalshi perpetual futures ("perps": the margin API, /margin/*, same key
    and signing as event contracts). Value = every subaccount's
    account_equity summed -- its cash plus unsettled P&L; settled_funds is
    the cash alone and already inside it (9/28: 99.85 + 25.09 of equity on
    99.15 settled funds, one KXBTCPERP position). Kalshi's portfolio_value
    covers event contracts only, so nothing is counted twice. None when the
    balance read fails."""
    try:
        bal = client.get("/margin/balance")
    except Exception as e:
        log(f"! perps balance read failed: {e}")
        return None
    subs = bal.get("subaccount_balances") or []
    out = {"equity": round(sum(_f(x.get("account_equity")) for x in subs), 2),
           "settled_funds": round(_f(bal.get("settled_funds")), 2),
           "positions": []}
    try:
        for p in client.get("/margin/positions").get("positions") or []:
            q = _f(p.get("position"))
            if abs(q) > 1e-9:
                out["positions"].append({
                    "ticker": p.get("market_ticker") or "?", "position": q,
                    "unrealized": round(_f(p.get("unrealized_pnl")), 2)})
    except Exception as e:
        log(f"! perps positions read failed: {e}")
    return out


def fetch_subaccounts(client):
    """The numbered subaccounts (nfl_snipe_bot trades live in subaccount 1
    since 2026-10-09, funded from the primary). The primary's balance and
    portfolio_value leave them out, so a move between the primary and a
    subaccount read as cash that left with no trade behind it: on 10/9 the
    $300 funding showed as reward credits +53.24 for +353.24 (Jack: "the
    reward credit is wrong in the email").

    {"equity": $ over every numbered subaccount (its cash + Kalshi's
    portfolio_value, as the primary), "accounts": {n: $}, "moves": [(ts, $
    into the primary)], "moves_by": [(ts, n, $ that left subaccount n)]},
    None when a read fails."""
    try:
        bals = client.get("/portfolio/subaccounts/balances").get("subaccount_balances") or []
        moves, moves_by = [], []
        for t in client.get("/portfolio/subaccounts/transfers").get("transfers") or []:
            amt = _f(t.get("amount_cents")) / 100.0
            src, dst = int(t.get("from_subaccount") or 0), int(t.get("to_subaccount") or 0)
            if (src == 0) != (dst == 0):          # numbered <-> numbered nets to 0
                moves.append((_f(t.get("created_ts")), amt if dst == 0 else -amt))
            # per subaccount, for the day by bot: (ts, n, $ that left
            # subaccount n; - = arrived in it), whichever account sent it
            for n, a in ((src, amt), (dst, -amt)):
                if n:
                    moves_by.append((_f(t.get("created_ts")), n, a))
        accounts = {}
        for n in sorted({int(b.get("subaccount_number") or 0) for b in bals} - {0}):
            b = client.get("/portfolio/balance", params={"subaccount": n})
            accounts[n] = round(_f(b.get("balance_dollars"))
                                + _f(b.get("portfolio_value")) / 100.0, 2)
    except Exception as e:
        log(f"! subaccount read failed: {e!r}")
        return None
    return {"equity": round(sum(accounts.values()), 2), "accounts": accounts,
            "moves": moves, "moves_by": moves_by}


def estimate_unpaid_rewards(client, now_utc):
    """imm_reward_recon.unpaid_estimate, never fatal: None on any failure,
    and the headline then simply carries no after-rewards figure."""
    try:
        if KL_DIR not in sys.path:
            sys.path.append(KL_DIR)     # the script's own dir wins
        import imm_reward_recon as rr
        est = rr.unpaid_estimate(client, now_utc)
        log(f"unpaid rewards (est.): ${est['total']:,.2f} = model "
            f"${est['raw']:,.2f} on {est['market_periods']} market-periods, "
            f"{est['markets']} markets, since {est['since']}")
        return est
    except Exception as e:
        log(f"! unpaid-rewards estimate failed: {e!r}")
        return None


def fetch_fills(client, t0: datetime, t1: datetime):
    """Every account fill in [t0, t1], all shards, oldest first."""
    out, cursor = [], None
    for _page in range(200):
        p = {"min_ts": int(t0.timestamp()), "max_ts": int(t1.timestamp()),
             "limit": 200}
        if cursor:
            p["cursor"] = cursor
        resp = client.get("/portfolio/fills", params=p)
        out += resp.get("fills") or []
        cursor = resp.get("cursor") or None
        if not cursor:
            break
    out.sort(key=lambda f: (int(f.get("ts") or 0), f.get("created_time") or ""))
    log(f"fills: {len(out)} since the prior morning")
    return out


def fetch_transfers(client):
    """Every deposit and withdrawal since the account opened, as [(ts, $)]:
    deposits positive, withdrawals negative. None when either read fails:
    the day change then lumps them in with credits, and the headline has no
    total profit. Failed, cancelled, rejected and still-pending transfers
    are left out. On 10/4 that was 12 deposits back to 2026-02-08 and no
    withdrawals, but every page is read anyway."""
    out = []
    for path, sign in (("/portfolio/deposits", 1.0), ("/portfolio/withdrawals", -1.0)):
        cursor = None
        for _page in range(50):
            p = {"limit": 200}
            if cursor:
                p["cursor"] = cursor
            try:
                resp = client.get(path, params=p)
            except Exception as e:
                log(f"! {path} read failed: {e!r}")
                return None
            items = next((v for v in resp.values() if isinstance(v, list)), [])
            for it in items:
                status = str(it.get("status") or "").lower()
                if status in ("failed", "cancelled", "canceled", "rejected", "pending"):
                    continue
                ts = _f(it.get("finalized_ts") or it.get("created_ts"))
                out.append((ts, sign * _f(it.get("amount_cents")) / 100.0))
            cursor = resp.get("cursor") or None
            if not cursor or not items:
                break
    return out


def sum_transfers(transfers, t0, t1: datetime):
    """(deposited $, withdrawn $) among fetch_transfers' records with
    t0 < ts <= t1; t0 None = since the account opened."""
    lo = float("-inf") if t0 is None else t0.timestamp()
    amts = [a for ts, a in transfers if lo < ts <= t1.timestamp()]
    return (round(sum(a for a in amts if a > 0), 2),
            round(-sum(a for a in amts if a < 0), 2))


def day_transfers(transfers, prior, deposited, withdrawn, t0, t1: datetime):
    """Net $ deposited (+) / withdrawn (-) since the prior morning, for the
    day change: the move in the all-time totals the two mornings recorded,
    so the day is exactly the move in total profit. Not the transfers dated
    t0 < ts <= t1: Kalshi dates a deposit when it is STARTED and leaves it out
    while pending, so a bank (ACH) deposit that clears days later fell in no
    day. The $500 started 10/10 cleared overnight 10/15 -> 10/16 and its cash
    read as reward credits: "day -$0.2k, rewards +$1.2k" where total profit
    fell $740.68 and Kalshi credited $671.86 (Jack 2026-10-17: "10/16 had
    profit at $28.6k but only saw -$200 on the day so what explains the $700
    drop"). A prior morning that recorded no totals falls back to the dated
    window; None when today's transfer read failed."""
    if transfers is None or deposited is None or withdrawn is None:
        return None
    p_dep, p_wd = (prior or {}).get("deposited"), (prior or {}).get("withdrawn")
    if p_dep is not None and p_wd is not None:
        return round((deposited - withdrawn) - (_f(p_dep) - _f(p_wd)), 2)
    dep, wd = sum_transfers(transfers, t0, t1)
    return round(dep - wd, 2)


def side_value(pos: float, mark, cost: float) -> float:
    """What `pos` contracts are worth at a YES mark (pos > 0 = YES, < 0 =
    NO), the way the snapshot values them; no mark = at cost."""
    if mark is None:
        return cost
    m = _f(mark)
    return pos * m if pos > 0 else -pos * (1.0 - m)


def replay_day(start, fills, settlements, end, ev_of):
    """Each event's P&L since the prior morning, from what actually happened
    to each market (Jack 2026-09-29: "double confirm that the 'Biggest
    movers since yesterday, by family' is accurate. i dont trust it").

    start: ticker -> (pos, value) at the prior snapshot's marks (pos > 0 =
    YES, < 0 = NO); fills: account fills since then, oldest first;
    settlements: settlement records since then; end: ticker -> (pos, value)
    now, at today's marks; ev_of: ticker -> event ticker.

    Kalshi's semantics, verified 2026-09-28 by replaying a day of fills to
    the positions endpoint with zero mismatches: `book_side` is relative to
    the YES book (bid = +YES at yes_price, ask = -YES at yes_price, opening
    NO at 1 - yes when no YES is held; the action/side labels are
    misleading); YES/NO pairs pay $1 at the fill that nets them; a
    settlement's `revenue` is the NET position's payout only. The old
    method added lifetime settlement totals on top of the prior snapshot's
    cumulative realized, so an event traded both ways and then settled
    counted its earlier realized P&L twice (9/29: Rotten Tomatoes -256.60
    shown against -9.02 real; -868.60 for the day against -536.28).

    Average cost per market, starting from the prior mark: `realized` = the
    contracts that left the book (sold, netted or settled) against that
    basis, `value_d` = what is still held, valued now, against the same
    basis, so realized + value_d is the day's P&L exactly and sums to
    cash + value now - value at the prior morning.

    Returns (events, cash, mismatches, settled): events maps event ->
    {"realized", "value_d"}; cash = the day's trading + settlement cash;
    mismatches = markets whose replayed position disagrees with `end`;
    settled = event -> set of market results."""
    st = {}
    for tk, (q, v) in start.items():
        st[tk] = {"q": q, "basis": v, "real": 0.0}
    cash = 0.0
    settled = {}
    stream = [(int(f.get("ts") or 0), 0, f) for f in fills]
    for s in settlements:
        t = datetime.fromisoformat((s.get("settled_time") or "").replace("Z", "+00:00"))
        stream.append((int(t.timestamp()), 1, s))
    stream.sort(key=lambda x: (x[0], x[1]))
    for _, kind, x in stream:
        tk = x.get("ticker") or x.get("market_ticker") or ""
        d = st.setdefault(tk, {"q": 0.0, "basis": 0.0, "real": 0.0})
        if kind == 1:
            pay = _f(x.get("revenue")) / 100.0 - _f(x.get("fee_cost"))
            d["real"] += pay - d["basis"]
            cash += pay
            d["q"] = d["basis"] = 0.0
            d["settled"] = x.get("market_result") or "?"
            settled.setdefault(ev_of(tk), set()).add(d["settled"])
            continue
        n, p, q = _f(x.get("count_fp")), _f(x.get("yes_price_dollars")), d["q"]
        if x.get("book_side") == "bid":                  # +YES at p
            k = min(n, -q) if q < 0 else 0.0             # closes NO at 1 - p
            if k > 0:
                cb = d["basis"] * k / -q
                d["real"] += k * (1.0 - p) - cb
                d["basis"] -= cb
                cash += k * (1.0 - p)
                q += k
            if n - k > 0:
                d["basis"] += (n - k) * p
                cash -= (n - k) * p
                q += n - k
        else:                                            # -YES at p
            k = min(n, q) if q > 0 else 0.0              # sells YES held
            if k > 0:
                cb = d["basis"] * k / q
                d["real"] += k * p - cb
                d["basis"] -= cb
                cash += k * p
                q -= k
            if n - k > 0:                                # opens NO at 1 - p
                d["basis"] += (n - k) * (1.0 - p)
                cash -= (n - k) * (1.0 - p)
                q -= n - k
        d["q"] = q
        fee = _f(x.get("fee_cost"))
        d["real"] -= fee
        cash -= fee
    events, mismatches = {}, []
    for tk in set(st) | set(end):
        d = st.get(tk) or {"q": 0.0, "basis": 0.0, "real": 0.0}
        q_end, v_end = end.get(tk, (0.0, 0.0))
        if abs(d["q"] - q_end) > 0.011:
            mismatches.append(tk)
        e = events.setdefault(ev_of(tk), {"realized": 0.0, "value_d": 0.0})
        e["realized"] += d["real"]
        e["value_d"] += v_end - d["basis"]
    return events, round(cash, 2), mismatches, settled


def fetch_market_info(client, tickers):
    """ticker -> {"event": ..., "yes_bid": $, "yes_ask": $, "last": $} in
    chunks. Missing tickers just aren't in the result."""
    info = {}
    tickers = sorted(set(tickers))
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            resp = client.get_markets(tickers=",".join(chunk), limit=len(chunk))
        except Exception as e:
            log(f"! get_markets chunk failed ({e}); {len(chunk)} tickers unmapped")
            continue
        for m in resp.get("markets") or []:
            info[m.get("ticker")] = {
                "event": m.get("event_ticker") or "",
                "yes_bid": _f(m.get("yes_bid_dollars")),
                "yes_ask": _f(m.get("yes_ask_dollars")),
                "last": _f(m.get("last_price_dollars")),
            }
    return info


# incentive_mm.MARK_WIDE_SPREAD_CENTS, off the bot's own knob: a two-sided
# book this many cents wide or wider marks at the last trade clamped inside
# its touch, not the mid (Jack 2026-10-01, "Yes, at 50c+"); 0 = off.
MARK_WIDE_SPREAD_CENTS = int(os.environ.get("IMM_MARK_WIDE_SPREAD", "50"))


def side_live(px: float) -> bool:
    """True when a YES bid or ask off a market read (dollars) is a real
    resting level. Kalshi reads an empty bid as $0 and an empty ask as
    $1.00, and nobody can trade at either, so only a price strictly inside
    (0, $1) is a side."""
    return 0.0 < px < 1.0


def wide_book(bid: float, ask: float) -> bool:
    """A two-sided touch (dollars) MARK_WIDE_SPREAD_CENTS or wider: its mid
    is no more a price than the $1.00 placeholder."""
    return (MARK_WIDE_SPREAD_CENTS > 0
            and round(100.0 * (ask - bid), 6) >= MARK_WIDE_SPREAD_CENTS)


def _yes_mark_src(bid: float, ask: float, last: float):
    """(yes_mark, the rule that set it): "mid", "wide", "one_sided" or
    "last" (an empty book); (None, None) when there is no price."""
    has_bid, has_ask = side_live(bid), side_live(ask)
    if has_bid and has_ask:
        if last > 0.0 and wide_book(bid, ask):
            return min(max(last, bid), ask), "wide"
        return (bid + ask) / 2.0, "mid"
    if not last > 0.0:
        return None, None
    if has_bid:
        return max(last, bid), "one_sided"
    if has_ask:
        return min(last, ask), "one_sided"
    return last, "last"


def yes_mark(bid: float, ask: float, last: float):
    """The YES mark ($) of a held position off one market read (yes_bid,
    yes_ask, last_price in dollars), or None when the read carries no price.
    incentive_mm.touch_mark_cents's rule, kept local because importing the
    bot reads its config from the environment. A two-sided book marks at the
    mid, or, MARK_WIDE_SPREAD_CENTS or wider, at the last trade clamped
    inside the touch (the mid if it never traded). A one-sided book marks at
    the last trade clamped to the live side: max(last, bid) with only a bid,
    min(last, ask) with only an offer. An empty book (a closed or settled
    market reads 0 / 1.00) marks at the last trade. Until 2026-10-01 the
    empty ask's $1.00 was averaged in -- KXDDCOLDBREW-26OCT02-T4.45 had a
    stray 5c bid, no offer and a 97c last trade, marked (0.05 + 1.00) / 2 =
    52.5c, and settled YES -- and a 1/70 book under a 1c last trade
    (KXDKNGAPP-26OCT08-T185) marked at its 35.5c mid."""
    return _yes_mark_src(bid, ask, last)[0]


# THE MARK BASIS (Jack 2026-10-22: "Why not move to last-trade pricing in
# email, so it's consistent with kalshi UI?" -> "Everything last trade").
# Open positions are valued at each market's last trade, Kalshi's own
# valuation (its portfolio_value is the positions at their last trade less
# the collateral it has handed back), so the trading lines, the movers table
# and the day by bot add up to the day the Kalshi UI shows, and the
# reconciling line shrinks to a residual (a market that never traded keeps
# its mid-rule mark; the reads are seconds apart). The day needs the prior
# morning's last trades: after a snapshot that did not record them (the
# mid-basis mornings before 10/23) the day stays on the mid basis, so no
# event reads the one-time re-pricing as a move. A stale last trade is
# Kalshi's number too: 10/22 had a 30Y Treasury strike at a 69c print under
# a 0/10c book. PF_MARK_BASIS=mid restores the mids (yes_mark).
MARK_BASIS = os.environ.get("PF_MARK_BASIS", "last").strip().lower()


def last_marks(tickers, mark_info, mid_marks):
    """Each open position's YES mark ($) at its market's last trade; the
    mid-basis mark (mid_marks) where the market has no trade on record."""
    out = {}
    for tk in tickers:
        last = _f((mark_info.get(tk) or {}).get("last"))
        if last > 0.0:
            out[tk] = last
        elif tk in mid_marks:
            out[tk] = mid_marks[tk]
    return out


def day_basis(prior) -> str:
    """The day's mark basis: "last" when MARK_BASIS asks for it and the
    prior snapshot recorded its last trades, else "mid"."""
    if MARK_BASIS != "last" or not prior or not prior.get("lasts_recorded"):
        return "mid"
    return "last"


def start_values(prior_tickers, basis: str):
    """(ticker -> (pos, value) at the prior morning, ticker -> event): each
    prior position valued on the day's basis (its recorded last trade on
    "last", its mid-rule mark otherwise; at cost without either)."""
    start, ev_map = {}, {}
    for ev, tks in (prior_tickers or {}).items():
        for tk, r in tks.items():
            q = _f(r.get("pos"))
            m = r.get("mark")
            if basis == "last" and r.get("last") is not None:
                m = r.get("last")
            start[tk] = (q, side_value(q, m, _f(r.get("cost"))))
            ev_map[tk] = ev
    return start, ev_map


def mark_positions(tickers, mark_info, prior_marks):
    """(marks, mark_src): each open position's YES mark ($) off today's
    market read (yes_mark), else the prior snapshot's mark, else None = at
    cost (unrealized 0 for that leg -- neutral, never a fake loss).
    mark_src counts the rule behind each mark: "wide" (a 50c+ book),
    "one_sided" and "last" (an empty book) are all off the last trade."""
    marks = {}
    mark_src = {"mid": 0, "wide": 0, "one_sided": 0, "last": 0,
                "carried": 0, "at_cost": 0}
    for tk in tickers:
        mi = mark_info.get(tk) or {}
        mark, src = _yes_mark_src(mi.get("yes_bid", 0.0), mi.get("yes_ask", 0.0),
                                  mi.get("last", 0.0))
        if mark is not None:
            marks[tk] = mark
            mark_src[src] += 1
        elif tk in prior_marks:
            marks[tk] = _f(prior_marks[tk])
            mark_src["carried"] += 1
        else:
            marks[tk] = None
            mark_src["at_cost"] += 1
    return marks, mark_src


# ----------------------------------------------------------------------------
# Snapshot store
# ----------------------------------------------------------------------------

def snapshot_path(d) -> str:
    return os.path.join(DATA_DIR, f"pf_snapshot_{d}.json")


def load_prior_snapshot(today_str: str):
    """Newest snapshot strictly older than today (ET)."""
    best = None
    for p in glob.glob(os.path.join(DATA_DIR, "pf_snapshot_*.json")):
        m = re.search(r"pf_snapshot_(\d{4}-\d{2}-\d{2})\.json$", p)
        if m and m.group(1) < today_str and (best is None or m.group(1) > best[0]):
            best = (m.group(1), p)
    if not best:
        return None
    try:
        with open(best[1], encoding="utf-8") as f:
            snap = json.load(f)
        log(f"prior snapshot: {best[0]} ({len(snap.get('events') or {})} events)")
        return snap
    except Exception as e:
        log(f"! prior snapshot {best[1]} unreadable: {e}")
        return None


HISTORY_COLS = ["date", "cash", "positions_value", "equity",
                "kalshi_positions_value", "perps_equity", "unpaid_rewards_est",
                "subs_equity", "deposited", "withdrawn"]
# The first morning whose snapshot records the all-time deposited / withdrawn
# totals (the total-profit headline, 10/4 pm). History rows written before
# those columns existed are filled from their morning's snapshot.
TOTALS_RECORDED_FROM = "2026-10-05"


def load_history():
    rows = []
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    row = {"date": r["date"], "cash": float(r["cash"]),
                           "positions_value": float(r["positions_value"]),
                           "equity": float(r["equity"])}
                except (KeyError, ValueError):
                    continue
                try:        # column added 8/14 pm; older rows have it blank
                    row["kalshi_positions_value"] = float(
                        r.get("kalshi_positions_value") or "")
                except ValueError:
                    row["kalshi_positions_value"] = ""
                for k in ("perps_equity", "unpaid_rewards_est",    # 9/28 on
                          "subs_equity",                           # 10/9 on
                          "deposited", "withdrawn"):               # 10/17 on
                    try:
                        row[k] = float(r.get(k) or "")
                    except ValueError:
                        row[k] = ""
                rows.append(row)
    # Rows from before the deposited / withdrawn columns: their morning's
    # snapshot recorded the totals that run read (from TOTALS_RECORDED_FROM)
    for row in rows:
        if row["date"] < TOTALS_RECORDED_FROM or "" not in (row["deposited"],
                                                             row["withdrawn"]):
            continue
        try:
            with open(snapshot_path(row["date"]), encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            continue
        if snap.get("deposited") is not None and snap.get("withdrawn") is not None:
            row["deposited"] = round(_f(snap["deposited"]), 2)
            row["withdrawn"] = round(_f(snap["withdrawn"]), 2)
    rows.sort(key=lambda r: r["date"])
    return rows


def upsert_history(rows, today_str, cash, pos_value, equity, kalshi_pv,
                   perps_equity=None, unpaid=None, subs_equity=None,
                   deposited=None, withdrawn=None):
    rows = [r for r in rows if r["date"] != today_str]
    rows.append({"date": today_str, "cash": round(cash, 2),
                 "positions_value": round(pos_value, 2),
                 "equity": round(equity, 2),
                 "kalshi_positions_value": round(kalshi_pv, 2),
                 "perps_equity": "" if perps_equity is None else round(perps_equity, 2),
                 "unpaid_rewards_est": "" if unpaid is None else round(unpaid, 2),
                 "subs_equity": "" if subs_equity is None else round(subs_equity, 2),
                 "deposited": "" if deposited is None else round(_f(deposited), 2),
                 "withdrawn": "" if withdrawn is None else round(_f(withdrawn), 2)})
    rows.sort(key=lambda r: r["date"])
    return rows


def write_history(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLS)
        w.writeheader()
        w.writerows(rows)


# ----------------------------------------------------------------------------
# Portfolio build
# ----------------------------------------------------------------------------

def build_portfolio(now_utc: datetime):
    """Returns a dict with everything the email needs (and the snapshot)."""
    today_str = str(now_utc.astimezone(ET).date())
    client = build_client()

    bal = client.get_balance()
    cash = _f(bal.get("balance_dollars"))
    kalshi_pv = _f(bal.get("portfolio_value")) / 100.0   # Kalshi's own valuation
    ev_roll, mkt_pos = fetch_unsettled_positions(client)
    collat = collateral_returned(ev_roll, mkt_pos)
    log(f"collateral Kalshi has returned on open positions: ${collat:,.2f}")
    cutoff = now_utc - timedelta(hours=LOOKBACK_H)
    settlements = fetch_recent_settlements(client, cutoff)
    prior = load_prior_snapshot(today_str)
    prior_events = dict((prior or {}).get("events") or {})
    prior_marks = dict((prior or {}).get("marks") or {})

    # Group settlements by event (records carry event_ticker natively).
    settled_events = {}                      # ev -> [settlement, ...]
    for s in settlements:
        ev = s.get("event_ticker") or event_from_ticker(s.get("ticker") or "")
        if ev:
            settled_events.setdefault(ev, []).append(s)

    # Events with settlements but no unsettled presence (fully settled since
    # the prior snapshot, or opened AND settled inside the window). Once an
    # event settles it is archived out of /portfolio/positions entirely
    # (verified 2026-08-14: settlement_status="all" returns nothing), so
    # their realized comes from the settlement records themselves — revenue
    # minus the settled contracts' cost — added on top of the prior
    # snapshot's cumulative. Only settlements NEWER than the prior snapshot
    # count, so the overlapping lookback window can't double-count.
    prior_created = None
    if prior:
        try:
            prior_created = datetime.strptime(
                prior.get("created_utc", ""), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    carried_dead = set()      # settled long ago, nothing new: prune from snapshot
    ev_counted = {}           # ev -> settlement keys already folded into realized
    for ev, setts in settled_events.items():
        if ev in ev_roll:
            continue          # unsettled rollup is already cumulative incl. settled strikes
        y = prior_events.get(ev)
        y_counted = set((y or {}).get("counted") or [])
        new_setts, new_keys = [], []
        for s in setts:
            try:
                ts = datetime.fromisoformat(
                    (s.get("settled_time") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            key = f'{s.get("ticker")}@{s.get("settled_time")}'
            if y_counted:
                # arithmetic chain: exact dedupe, so the window can be generous
                fresh = (key not in y_counted
                         and (prior_created is None
                              or ts > prior_created - timedelta(hours=6)))
            else:
                # prior state came from the positions rollup, which was
                # cumulative through the snapshot moment: strictly newer only
                fresh = prior_created is None or ts > prior_created
            if fresh:
                new_setts.append(s)
                new_keys.append(key)
        if not y and not new_setts:
            continue          # settled before the baseline; already accounted
        # Settlement P&L per market = net-side payout (`revenue` covers ONLY
        # the net position) + $1 per paired yes/no contract (pairs cash at
        # settlement — they are the bots' frozen inventory, never realized
        # earlier) - cost of both sides. Verified against the positions
        # rollup 2026-08-14 (KXBTCD-26AUG1417: formula +194.07 vs +189.61
        # marked just before settlement).
        spnl = sum(_f(s.get("revenue")) / 100.0
                   + min(_f(s.get("yes_count_fp")), _f(s.get("no_count_fp")))
                   - _f(s.get("yes_total_cost_dollars"))
                   - _f(s.get("no_total_cost_dollars")) for s in new_setts)
        sfees = sum(_f(s.get("fee_cost")) for s in new_setts)
        ev_roll[ev] = {"realized": (y["realized"] if y else 0.0) + round(spnl, 2),
                       "fees": (y["fees"] if y else 0.0) + round(sfees, 2)}
        ev_counted[ev] = sorted(y_counted | set(new_keys))[-200:]
        if y and not new_setts:
            carried_dead.add(ev)


    # Mark every open position: the mid of a two-sided book, else the last
    # trade clamped inside a 50c+ book or to the live side of a one-sided
    # one, else yesterday's mark, else at cost (unrealized 0 for that leg —
    # neutral, never a fake loss).
    mark_info = fetch_market_info(client, list(mkt_pos))
    marks, mark_src = mark_positions(mkt_pos, mark_info, prior_marks)
    # the day's basis (MARK_BASIS): the last trades once the prior morning
    # recorded them; both marks go into the snapshot either way
    lasts = last_marks(mkt_pos, mark_info, marks)
    mark_basis = day_basis(prior)      # (`basis` below is each event's cost)
    val_marks = lasts if mark_basis == "last" else marks

    # Group open value + cost basis by event.
    ev_value, ev_basis, ev_tickers = {}, {}, {}
    tk_end, tk_event = {}, {}          # per market, for the day replay
    for tk, rec in mkt_pos.items():
        pos, cost = rec["pos"], rec["cost"]
        ev = (mark_info.get(tk) or {}).get("event") or event_from_ticker(tk)
        mark = val_marks.get(tk)
        if mark is None:
            val = cost                     # marked at cost: unrealized 0
        else:
            val = pos * mark if pos > 0 else -pos * (1.0 - mark)
        tk_end[tk] = (pos, val)
        tk_event[tk] = ev
        ev_value[ev] = ev_value.get(ev, 0.0) + val
        ev_basis[ev] = ev_basis.get(ev, 0.0) + cost
        mid_mark, last = marks.get(tk), lasts.get(tk)
        ev_tickers.setdefault(ev, {})[tk] = {
            "pos": pos, "cost": round(cost, 2),
            "mark": None if mid_mark is None else round(mid_mark, 4),
            "last": None if last is None else round(last, 4)}
    # Kalshi's collateral refunds by event: the movers table nets them out of
    # the open values as Kalshi's UI does (net_of_refund)
    refunds = event_refunds(ev_roll, mkt_pos,
                            lambda tk: tk_event.get(tk) or event_from_ticker(tk))

    # Prior events now absent everywhere (no open legs, no settlement trail):
    # can't tell a data gap from an ancient settlement — flag, don't guess.
    unreadable = [ev for ev in sorted(prior_events)
                  if ev not in ev_roll and ev not in ev_value]

    # Today's per-event P&L components and the snapshot. Per-event
    # P&L-to-date = value - basis (unrealized) + realized - fees; the day
    # table diffs this against yesterday's snapshot, so a plain buy at the
    # market is P&L-neutral (cash became contracts of equal value).
    events_today = {}
    for ev in set(ev_roll) | set(ev_value):
        roll = ev_roll.get(ev) or {"realized": 0.0, "fees": 0.0}
        value = ev_value.get(ev, 0.0)
        basis = ev_basis.get(ev, 0.0)
        if (abs(roll["realized"]) < 0.005 and abs(roll["fees"]) < 0.005
                and abs(value) < 0.005 and abs(basis) < 0.005
                and ev not in prior_events):
            continue                       # never traded, nothing at stake
        events_today[ev] = {"realized": round(roll["realized"], 2),
                            "fees": round(roll["fees"], 2),
                            "value": round(value, 2),
                            "basis": round(basis, 2)}
        if ev in ev_counted:
            events_today[ev]["counted"] = ev_counted[ev]
        if ev in refunds:
            events_today[ev]["refund"] = refunds[ev]

    positions_value = round(sum(e["value"] for e in events_today.values()), 2)
    equity = round(cash + positions_value, 2)

    # Perpetuals (Jack 2026-09-28: "include the value of perps as well in
    # portfolio value"). A failed read carries the prior morning's value,
    # flagged, rather than dropping it out of the account value.
    perps = fetch_perps(client)
    perps_stale = False
    if perps is not None:
        perps_equity = perps["equity"]
    elif (prior or {}).get("perps_equity") is not None:
        perps_equity, perps_stale = _f(prior["perps_equity"]), True
    else:
        perps_equity = None
    # Numbered subaccounts, the same way: a failed read carries the prior
    # value, flagged, and leaves the day's moves unknown (sub_moves None).
    subs = fetch_subaccounts(client)
    subs_stale = False
    if subs is not None:
        subs_equity = subs["equity"] if subs["accounts"] else None
    elif (prior or {}).get("subs_equity") is not None:
        subs_equity, subs_stale = _f(prior["subs_equity"]), True
    else:
        subs_equity = None
    equity_kalshi = round(cash + kalshi_pv, 2)
    account_value = round(equity_kalshi + (perps_equity or 0.0)
                          + (subs_equity or 0.0), 2)
    unpaid = estimate_unpaid_rewards(client, now_utc)

    # Total profit (Jack 2026-10-04: "at the top also show total profit,
    # excluding deposits/withdrawals"): the account value less every dollar
    # deposited, plus every dollar withdrawn, since the account opened. The
    # same read splits the day's transfers out of the day change below.
    transfers = fetch_transfers(client)
    deposited = withdrawn = total_profit = None
    if transfers is not None:
        deposited, withdrawn = sum_transfers(transfers, None, now_utc)
        total_profit = round(account_value - deposited + withdrawn, 2)
        log(f"total profit {total_profit:+,.2f} = account value "
            f"{account_value:,.2f} - deposited {deposited:,.2f} + withdrawn "
            f"{withdrawn:,.2f}")

    snapshot = {"date": today_str,
                "created_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cash": round(cash, 2), "positions_value": positions_value,
                "equity": equity,
                "kalshi_positions_value": round(kalshi_pv, 2),
                "equity_kalshi": equity_kalshi,
                "perps_equity": perps_equity, "perps_stale": perps_stale,
                "perps_positions": (perps or {}).get("positions"),
                "subs_equity": subs_equity, "subs_stale": subs_stale,
                "collateral_returned": collat,
                "subs": None if subs is None else {
                    str(n): v for n, v in subs["accounts"].items()},
                "account_value": account_value,
                # every ticker carries its last trade from here on (MARK_BASIS)
                "lasts_recorded": True, "mark_basis": mark_basis,
                "refunds_recorded": True,            # events carry "refund"
                "unpaid_rewards_est": (unpaid or {}).get("total"),
                "deposited": deposited, "withdrawn": withdrawn,
                "total_profit": total_profit,
                "events": {ev: e for ev, e in events_today.items()
                           if ev not in carried_dead},
                "tickers": ev_tickers,
                "marks": {tk: m for tk, m in ((t, marks.get(t)) for t in mkt_pos)
                          if m is not None}}

    # Day-over-day rows: replay_day over every fill and settlement since the
    # prior morning (2026-09-29), starting from the prior snapshot's own
    # per-market positions and marks and ending at today's. The legacy
    # event-rollup diff below is kept only for a prior snapshot without
    # per-market positions (none since 8/14).
    rows, no_trade_cash, net_transfers, replay_info = [], None, None, None
    sub_moves = sub_moves_by = None
    prior_tickers = (prior or {}).get("tickers")
    if prior and prior_created is not None and prior_tickers:
        start, ev_map = start_values(prior_tickers, mark_basis)
        win_setts = []
        for s in settlements:
            ts = datetime.fromisoformat((s.get("settled_time") or "").replace("Z", "+00:00"))
            if prior_created < ts <= now_utc:
                win_setts.append(s)
                if s.get("event_ticker"):
                    ev_map.setdefault(s.get("ticker") or "", s["event_ticker"])
        ev_map.update(tk_event)
        fills = fetch_fills(client, prior_created, now_utc)
        evs, trade_cash, mism, settled_now = replay_day(
            start, fills, win_setts, tk_end,
            lambda tk: ev_map.get(tk) or event_from_ticker(tk))
        if mism:
            log(f"! replay: {len(mism)} market(s) disagree with the positions "
                f"endpoint: {', '.join(sorted(mism)[:8])}")
        prior_names = set(prior_tickers) | set(prior_events)
        prior_val = {}
        for tk, (_q, v) in start.items():
            prior_val[ev_map[tk]] = prior_val.get(ev_map[tk], 0.0) + v
        prior_refunds = bool(prior.get("refunds_recorded"))
        for ev, e in evs.items():
            realized, value_d = round(e["realized"], 2), round(e["value_d"], 2)
            day = round(realized + value_d, 2)
            if abs(day) < 0.005 and abs(realized) < 0.005 and abs(value_d) < 0.005:
                continue
            gross_now = round(ev_value.get(ev, 0.0), 2)
            realized, value_d, value_now = net_of_refund(
                realized, value_d, gross_now, refunds.get(ev, 0.0),
                _f((prior_events.get(ev) or {}).get("refund")) if prior_refunds else None)
            if ev in settled_now:
                note = "settled " + "/".join(sorted(settled_now[ev]))
            elif ev not in prior_names:
                note = "new"
            elif abs(gross_now) < 0.005 and abs(prior_val.get(ev, 0.0)) >= 0.005:
                note = "closed"
            else:
                note = ""
            rows.append({"event": ev, "day": day, "realized": realized,
                         "value_d": value_d, "value_now": value_now, "note": note})
        no_trade_cash = round(cash - _f(prior.get("cash")) - trade_cash, 2)
        net_transfers = day_transfers(transfers, prior, deposited, withdrawn,
                                      prior_created, now_utc)
        if subs is not None:
            sub_moves = round(sum(a for ts, a in subs["moves"]
                                  if prior_created.timestamp() < ts <= now_utc.timestamp()), 2)
            sub_moves_by = {}
            for ts, n, a in subs.get("moves_by") or []:
                if prior_created.timestamp() < ts <= now_utc.timestamp():
                    sub_moves_by[n] = round(sub_moves_by.get(n, 0.0) + a, 2)
        check = (sum(r["day"] for r in rows) - trade_cash
                 - (sum(v for _, v in tk_end.values()) - sum(v for _, v in start.values())))
        replay_info = {"fills": len(fills), "settlements": len(win_setts),
                       "mismatches": len(mism), "trade_cash": trade_cash}
        log(f"replay: {len(fills)} fills, {len(win_setts)} settlements, "
            f"{len(mism)} mismatches; trading cash {trade_cash:+,.2f}, cash with "
            f"no trade behind it {no_trade_cash:+,.2f}, transfers "
            f"{'n/a' if net_transfers is None else f'{net_transfers:+,.2f}'}, "
            f"into the primary from subaccounts "
            f"{'n/a' if sub_moves is None else f'{sub_moves:+,.2f}'}; "
            f"rows vs cash+value check {check:+.2f}")
    else:
        def P(e):
            return (e["value"] - e.get("basis", 0.0)) + e["realized"] - e["fees"]

        empty = {"realized": 0.0, "fees": 0.0, "value": 0.0, "basis": 0.0}
        for ev in set(events_today) | set(prior_events):
            t = events_today.get(ev) or empty
            y = prior_events.get(ev) or empty
            if ev in unreadable:
                continue
            day = P(t) - P(y)
            d_realized = (t["realized"] - t["fees"]) - (y["realized"] - y["fees"])
            d_unreal = ((t["value"] - t.get("basis", 0.0))
                        - (y["value"] - y.get("basis", 0.0)))
            if abs(day) < 0.005 and abs(d_unreal) < 0.005 and abs(d_realized) < 0.005:
                continue
            if ev in settled_events:
                results = {(s.get("market_result") or "?") for s in settled_events[ev]}
                note = "settled " + "/".join(sorted(results))
            elif ev not in prior_events:
                note = "new"
            elif abs(t["value"]) < 0.005 and abs(y["value"]) >= 0.005:
                note = "closed"
            else:
                note = ""
            rows.append({"event": ev, "day": round(day, 2),
                         "realized": round(d_realized, 2), "value_d": round(d_unreal, 2),
                         "value_now": t["value"], "note": note})
    rows.sort(key=lambda r: -r["day"])

    log(f"marks: {mark_src} | basis {mark_basis} | Kalshi values positions ${kalshi_pv:,.2f} "
        f"(+ ${collat:,.2f} collateral returned) vs our "
        f"{'last trades' if mark_basis == 'last' else 'mids'} "
        f"${positions_value:,.2f}"
        + (f" | unreadable: {', '.join(unreadable)}" if unreadable else ""))
    return {"today": today_str, "cash": round(cash, 2),
            "positions_value": positions_value, "equity": equity,
            "kalshi_positions_value": round(kalshi_pv, 2),
            "equity_kalshi": equity_kalshi,
            "perps_equity": perps_equity, "perps": perps,
            "perps_stale": perps_stale, "subs_equity": subs_equity,
            "subs": subs, "subs_stale": subs_stale, "sub_moves": sub_moves,
            "sub_moves_by": sub_moves_by,
            "collateral_returned": collat,
            "collateral_d": (None if (prior or {}).get("collateral_returned") is None
                             else round(collat - _f(prior["collateral_returned"]), 2)),
            "account_value": account_value,
            "unpaid": unpaid, "deposited": deposited, "withdrawn": withdrawn,
            "total_profit": total_profit, "transfers": transfers,
            "rows": rows, "snapshot": snapshot, "prior": prior,
            "mark_src": mark_src, "unreadable": unreadable, "mark_basis": mark_basis,
            "refunds_total": round(sum(refunds.values()), 2),
            "n_settlements": len(settlements),
            "no_trade_cash": no_trade_cash, "net_transfers": net_transfers,
            "replay": replay_info,
            "first_run": prior is None}


# ----------------------------------------------------------------------------
# Chart
# ----------------------------------------------------------------------------

def history_account_value(r) -> float:
    """A balance_history row's account value on Kalshi's own positions
    valuation (Jack 8/14: "value it based on Kalshi"); rows predating that
    column fall back to our marks. Perpetuals are in from 9/28, numbered
    subaccounts from 10/9 (earlier rows have none recorded)."""
    return ((r["cash"] + r["kalshi_positions_value"]
             if isinstance(r.get("kalshi_positions_value"), float) else r["equity"])
            + sum(r[k] for k in ("perps_equity", "subs_equity")
                  if isinstance(r.get(k), float)))


def profit_series(history, transfers, today=None, today_profit=None):
    """[(date, realized, estimated)], one per history row: realized = that
    morning's account value less every dollar deposited, plus every dollar
    withdrawn, by 7:00 ET (the headline's total profit; `today`'s point is
    `today_profit` exactly), estimated = realized + that morning's
    unpaid-rewards estimate, None before 9/29 when it was not recorded.

    The deposits are the totals that morning's own run recorded, where it
    did (Jack 2026-10-17: "Fix it"). Re-read today, Kalshi dates a deposit
    when it was started: the $500 ACH begun 10/10 that cleared overnight
    10/15 -> 10/16 netted out of 10/11-10/15, whose account values never held
    its cash, and plotted them $500 low. Rows with no recorded totals still
    net the transfers dated by 7:00 ET."""
    out = []
    for r in history:
        d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        if r["date"] == today and today_profit is not None:
            profit = today_profit
        elif (isinstance(r.get("deposited"), float)
              and isinstance(r.get("withdrawn"), float)):
            profit = round(history_account_value(r) - r["deposited"] + r["withdrawn"], 2)
        else:
            dep, wd = sum_transfers(transfers, None,
                                    ET.localize(datetime(d.year, d.month, d.day, 7)))
            profit = round(history_account_value(r) - dep + wd, 2)
        unpaid = r.get("unpaid_rewards_est")
        out.append((d, profit,
                    round(profit + unpaid, 2) if isinstance(unpaid, float) else None))
    return out


def render_chart(history, transfers, out_png: str, today=None,
                 today_profit=None) -> bool:
    """Daily profit, two lines (Jack 2026-10-10: "instead of chart of account
    value, show 2 lines: Realized profit (+$23,916.81) / Estimated profit
    (est. +$27,021.04 after rewards are paid out)"). No deposit history
    today = no chart: profit can't be told from a deposit without it."""
    if transfers is None:
        log("! deposit history unread; sending without the profit chart")
        return False
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except Exception as e:
        log(f"! matplotlib unavailable ({e}); sending without chart")
        return False
    pts = profit_series(history[-120:], transfers, today, today_profit)
    dates = [d for d, _, _ in pts]
    real = [p for _, p, _ in pts]
    est = [(d, e) for d, _, e in pts if e is not None]

    fig, ax = plt.subplots(figsize=(7.6, 3.1), dpi=180)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")
    # a dot per morning only while they stay apart; always one on the latest
    dots = dict(marker="o", ms=4.5, mec="#ffffff", mew=0.8)
    every = dots if len(dates) <= 20 else {}
    ax.plot(dates, real, color=C_EQUITY, lw=2.2, zorder=3, **every,
            label=f"Realized profit ({_signed_usd(real[-1])})")
    ax.plot(dates[-1:], real[-1:], color=C_EQUITY, zorder=4, **dots)
    if est:
        ax.plot([d for d, _ in est], [e for _, e in est], color=C_EST, lw=2.2,
                ls=(0, (4, 1.6)), zorder=3, **every,
                label="Estimated profit" + (
                    "" if pts[-1][2] is None else
                    f" (est. {_signed_usd(pts[-1][2])} after rewards are paid out)"))
        ax.plot(est[-1][:1], est[-1][1:], color=C_EST, zorder=4, **dots)
    ax.legend(loc="upper left", frameon=False, fontsize=8.5, labelcolor=C_INK2,
              handlelength=2.6)
    ax.yaxis.set_major_formatter(FuncFormatter(
        lambda v, _: f"{'-' if v < 0 else ''}${abs(v):,.0f}"))
    if len(dates) <= 4:      # AutoDateLocator invents year ticks for sparse data
        ax.set_xticks(dates)
        ax.set_xlim(dates[0] - timedelta(days=1), dates[-1] + timedelta(days=1))
    else:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.grid(axis="y", color=C_GRID, lw=0.8)
    ax.tick_params(colors=C_MUTED, labelsize=8.5, length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(C_AXIS)
    ax.margins(x=0.05 if len(dates) > 1 else 0.3)
    lo, hi = ax.get_ylim()
    if lo < 0 < hi:
        ax.axhline(0, color=C_AXIS, lw=0.8, zorder=1)
    pad = max((hi - lo) * 0.12, 1.0)
    ax.set_ylim(lo - pad * 0.3, hi + pad * 2.5)    # room for the legend
    fig.tight_layout()
    fig.savefig(out_png, facecolor="#ffffff")
    plt.close(fig)
    return True


# ----------------------------------------------------------------------------
# Email body
# ----------------------------------------------------------------------------

def family_movers(rows, top_n: int = 25, per_family: int = 3):
    """Every event row — settled, still open, new, closed — rolled up by
    family and ranked by the SIZE of the day's move, realized and the
    mark-to-mid change alike (Jack 2026-09-22: "include the position families
    that moved the most (even if unrealized) since the last daily update").

    Returns (shown, totals, hidden): `shown` is the top_n families by |day|
    as (name, group) pairs, each group carrying day / realized / unreal /
    value_now sums, its rows, and `top` = the per_family events with the
    largest |day|; `totals` covers ALL families; `hidden` is
    (n_families_not_shown, their net day) so the shown rows plus the hidden
    line always add to the total. Per row day == realized + value_d (P&L to
    date differenced against the prior morning), so the split is exact."""
    groups = {}
    for r in rows:
        g = groups.setdefault(family_for(r["event"]),
                              {"day": 0.0, "realized": 0.0, "unreal": 0.0,
                               "value_now": 0.0, "rows": []})
        g["day"] += r["day"]
        g["realized"] += r["realized"]
        g["unreal"] += r["value_d"]
        g["value_now"] += r["value_now"]
        g["rows"].append(r)
    for g in groups.values():
        for k in ("day", "realized", "unreal", "value_now"):
            g[k] = round(g[k], 2)
        g["rows"].sort(key=lambda r: -abs(r["day"]))
        g["top"] = g["rows"][:per_family]
    ranked = sorted(groups.items(), key=lambda kv: (-abs(kv[1]["day"]), kv[0]))
    shown, hidden = ranked[:top_n], ranked[top_n:]
    totals = {k: round(sum(g[k] for _, g in ranked), 2)
              for k in ("day", "realized", "unreal", "value_now")}
    totals["n_events"] = len(rows)
    totals["n_families"] = len(ranked)
    hidden_net = round(sum(g["day"] for _, g in hidden), 2)
    return shown, totals, (len(hidden), hidden_net)


def mover_group(r) -> str:
    """The dashboard's group label beside an event row of the movers table
    (CPI & inflation, Treasury yields, ...; "" where the group is just the
    series). The table stops at family / event: its markets are left out
    (Jack 2026-10-10: "no need to show market. just family/event so the
    table is more condensed")."""
    _fam, grp = family_and_group(r["event"])
    return grp if grp and grp != r["event"].split("-", 1)[0] else ""


def _mover_tag(r) -> str:
    """Why an event moved: the settlement/new/closed note when there is one,
    otherwise whether the move was realized (a sell), a pure mark change, or
    both."""
    note = r.get("note") or ""
    realized_abs = abs(r.get("realized", 0.0))
    marked_abs = abs(r.get("value_d", 0.0))
    if note.startswith("settled") and marked_abs > realized_abs:
        # a strike settled, but the still-open legs' mark drove the number
        # (monthly touch events settle strike by strike and live on)
        return f"mostly mark ({note})"
    if note:
        return note
    realized = realized_abs >= 0.005
    marked = marked_abs >= 0.005
    if realized and marked:
        return "partly realized"
    return "realized" if realized else "mark"


# ----------------------------------------------------------------------------
# What to do (Jack 2026-10-19: "In daily portfolio digest email, share
# recommendations on what I should do based on prior day trading
# performance"; 2026-10-22: "Clean up this section of daily email. Center it
# around last day's results (though still contextualize with 7D results).
# Make it more insightful with clear recommendations and why")
# ----------------------------------------------------------------------------
# Read off the IMM dashboard's per-market day rows: the IMM bot's own book
# (the crypto fleets, the weather bot and the snipe bot have their own
# emails), rewards as modelled. Yesterday leads; the last ADVICE_DAYS full ET
# days are the context each line carries. Every P&L splits three ways:
#   rewards    what the quoting earned;
#   own fills  the day's fills, marked to the day's end -- and their
#              mark-outs say how they lost: within 5 minutes (picked off: the
#              taker knew first), at the market's settlement that day (the
#              wrong side of the outcome), or over the hours after (the price
#              trended through them);
#   held       positions it already held, re-marked (cr) or settled (cs).
# The recommendations, each with its why:
#   SHRINK a FAMILY whose quoting loses persistently -- ADVICE_FAMILY_SHRINK_7D
#     over the 7 days, losing on most of them -- naming its worst series and
#     its live events that lost yesterday (2026-10-25: families AND events).
#   PAUSE / SHRINK a series whose quoting (rewards + own fills) loses
#     persistently -- over the 7 days, on most of them, and again yesterday
#     -- while it is still quoted. One bad day is never enough (the 10/09
#     Carbon Arc review: pruning on a trailing net barely persists; the
#     10/16 LoL bait pick-off was one day and is guarded since).
#   PAUSE / SHRINK / NO CHANGE a specific EVENT still quoted whose quoting
#     lost ADVICE_EVENT_Y or more yesterday, by its own 7 days: losing over
#     them -> pause or shrink it; up over them -> one bad day, no change.
#     Events are named by Kalshi's title (the dashboard's title caches).
#   Each of those cuts then hears the 30 days (Jack 2026-10-25: "Make sure
#     shrink/double down decisions are also contemplating 30D of data not
#     just 7"): a month whose quoting still pays, its rewards still at the
#     month's pace, takes the cut a step down (PAUSE to SHRINK, SHRINK to
#     NO CHANGE: a bad week in a paying month); a month paid by rewards
#     since faded is no defence; a losing month agrees, and the line says so.
#   TRIM an open loser that still has a lot to lose (ADVICE_TRIM_LEFT), in
#     a family whose held positions cost the week -- to cut risk, Jack's call.
#   HOLD the rest of those families' open losers, in one line: their
#     quoting pays and what is left to lose is small next to what is gone,
#     so selling would lock the loss in and pay the spread.
#   NO CHANGE on a family's one bad day inside a good week, and on a losing
#     week whose losses were one-offs (no series losing on most days).
#   DOUBLE DOWN (x1.5) on a LINE (below) that earns day after day with fills
#     light next to its rewards: up on ADVICE_DD_UP_DAYS of the 7 days and on
#     ADVICE_DD_UP_SHARE_30 of the days it earned over the 30, up over both,
#     and adding ADVICE_DD_GAIN a day or more at both paces by the 10/11 size
#     study's response (rewards ~ size^0.51, fill losses ~ size^0.90; held
#     positions count with the fills, and only as losses). Not on a line
#     sized up in the last ADVICE_DD_RESIZE_DAYS, nor on one in the size
#     test's x1.5 half (Jack 2026-10-29: "i had already 1.5x'ed these. is the
#     daily email saying i should double down again?" -- the 10/29 email
#     named KXRT, Vercel and KXAPRPOTUS again three days after he took them
#     x2 -> x3 and x3 -> x5, on 30 days 27 of which ran at the old size): a
#     context line names them and the day they are judged again. A DOUBLE
#     DOWN names the line's size now and after (x3 -> x4.5). Sizes: the
#     dashboard's D.sizes (the bot's ladder x its family multiplier);
#     series_sizes.json keeps the day each series last went up.
#   KEEP the families whose rewards beat their fills.
# Below the family (Jack 2026-10-25: "How about mlb, red rocks, etc? Also give
# suggestions on where to double down. Also explicitly callout performance
# of any new families/series/events that were added") every series sits in
# a LINE: its league in Sports & awards (MLB, NFL, NHL, LoL, ...; the NFL
# ladders keep theirs), else its dashboard group when the group holds more
# (Agriculture, Treasury yields, *CC card spend, Sports & venues: Red Rocks,
# ...), else itself -- a family's single series whose titles open with the
# same words making one line ("Highest temperature in ..."). Then:
#   NEW THIS WEEK: the lines, series and events first seen in the 7 days
#     (an event only when its series lists few), each so far with an early
#     read: earning, held positions marked down, or fills outrunning rewards.
#   EVERY LINE, at the end of the email: yesterday / 7 days / 30 days with a
#     call -- DOUBLE DOWN / PAUSE / SHRINK from the recommendations, NEW,
#     IDLE (nothing quoted now; a daily or weekly series between its events
#     still counts as quoted), WATCH (down over 7 or 30 days: the biggest
#     drag, still quoted or not) or KEEP -- and its why. An event the
#     recommendations shrink is noted on its line, not the line's call.
# Nothing here changes the bot: each line is Jack's call.
IMM_DASH_HTML = os.path.join(KL_DIR, "run-logs", "incentive-mm", "dashboard",
                             "imm_dashboard.html")
ADVICE_ENABLED = os.environ.get("PF_ADVICE", "1") != "0"
ADVICE_DAYS = 7
# the longer view every line also carries (Jack 2026-10-22: "Also include L30D
# for context") -- the dashboard keeps ~30 full days; fewer read as "N days"
ADVICE_LONG_DAYS = 30
ADVICE_DRIVERS = 3             # families under "What moved it"
ADVICE_DRIVER_MIN = 25.0       # ... each at least this big yesterday
ADVICE_PAUSE_7D = -100.0       # a live series' 7-day quoting at/below -> PAUSE
ADVICE_SHRINK_7D = -40.0       # ... at/below -> SHRINK
ADVICE_LOSING_DAYS = 4         # ... losing on at least this many of the 7
ADVICE_MAX_PAUSE = 3
ADVICE_HELD_7D = -100.0        # a family's held positions at/below -> HOLD / TRIM
ADVICE_OPEN_MIN = -20.0        # an open position this far under water is named
ADVICE_TRIM_LEFT = 50.0        # ... down this much already, and as much still
ADVICE_TRIM_LOST = -50.0       #     to lose -> TRIM (a fresh long-dated short
                               #     -- KXPRESRUN's -- is not a trim)
ADVICE_MAX_TRIM = 2
ADVICE_REVIEW_NET = -100.0     # a family's 7-day net at/below: say why, or act
ADVICE_BAD_DAY = -100.0        # a family's yesterday at/below, 7 days still up
ADVICE_KEEP_NET = 100.0        # 7-day net at/above (quoting paying) -> KEEP
ADVICE_BOTTOM_N = 3            # yesterday among the N worst days -> below normal
ADVICE_PICKOFF_SHARE = 0.5     # a mark-out this share of a fill loss names the cause
ADVICE_REWARD_DROP = 0.2       # yesterday's rewards this far under the average: say where
# families and specific events beside the series (Jack 2026-10-25: "Make sure
# daily email suggestions comb through both specific events and families when
# recommending what to do")
ADVICE_FAMILY_SHRINK_7D = -150.0  # a live family's 7-day quoting at/below, losing on
ADVICE_MAX_FAMILIES = 2           #   ADVICE_LOSING_DAYS+ of them, two series or more
ADVICE_FAMILY_SERIES_MIN = -20.0  #   each down this much -> SHRINK the family
ADVICE_EVENT_Y = -25.0            # a live event's quoting yesterday at/below -> its line:
ADVICE_EVENT_PAUSE_7D = -100.0    #   its 7 days at/below -> PAUSE it,
ADVICE_EVENT_SHRINK_7D = -25.0    #   at/below -> SHRINK it, above -> one bad day
ADVICE_MAX_EVENTS = 3
# lines, doubling down and what is new (Jack 2026-10-25, above)
ADVICE_LEAGUES = {"MLB": "MLB", "NFL": "NFL", "NBA": "NBA", "WNBA": "WNBA", "NHL": "NHL",
                  "LOL": "LoL", "NCAAF": "NCAAF", "NCAAB": "NCAAB", "MLS": "MLS",
                  "EPL": "EPL", "UFC": "UFC", "PGA": "PGA", "ATP": "ATP", "WTA": "WTA",
                  "NASCAR": "NASCAR", "F1": "F1"}
ADVICE_LEAGUE_RE = re.compile(r"KX(" + "|".join(sorted(ADVICE_LEAGUES, key=len, reverse=True))
                              + ")")
ADVICE_STEM_WORDS = 3          # a family's single series whose titles open with the
ADVICE_STEM_MIN = 3            #   same words, this many or more, make one line
ADVICE_LINE_REW = 15.0         # EVERY LINE lists a line with 30-day rewards at/above,
ADVICE_LINE_NET = 25.0         #   a 30-day net this big either way,
ADVICE_LINE_7D = 10.0          #   or a 7-day net this big (the rest: one row a family);
ADVICE_IDLE_NET = 100.0        #   an idle one (nothing quoted) a 30-day net this big, or
                               #   a 7-day one ADVICE_LINE_NET
ADVICE_FADE_REW = 100.0        # a line with 30-day rewards at/above whose 7 days earned
ADVICE_FADE_SHARE = 0.25       #   under this share of its 30-day pace: WATCH, faded
ADVICE_DD_MULT = 1.5           # DOUBLE DOWN at this size,
ADVICE_DD_REW_ELAST = 0.51     #   by the 10/11 size study: rewards ~ size^0.51,
ADVICE_DD_FILL_ELAST = 0.90    #   fill losses ~ size^0.90 (memory: imm-base-size-1022)
ADVICE_DD_GAIN = 3.0           # ... when that adds this much a day or more,
ADVICE_DD_30D = 150.0          #   its 30-day net is at/above,
ADVICE_DD_7D = 25.0            #   its 7-day quoting at/above, up on
ADVICE_DD_UP_DAYS = 5          #   this many of the 7 days,
ADVICE_DD_ACTIVE = 14          #   it earned on this many of the 30,
ADVICE_DD_UP_SHARE_30 = 0.6    #   its quoting up on this share of those, and up over them
ADVICE_MAX_DD = 3
ADVICE_DD_RESIZE_DAYS = 14     # ... and not within this many days of a size-up
ADVICE_MAX_DD_HELD = 5         # sized-up lines named in the context line
# the size-ups from before series_sizes.json kept them: series -> (ET day it
# went live, size before, size after), used only while the dashboard reads
# that size after
_VERCEL_SERIES = ("KXOPENVSPEND", "KXMOONVSPEND", "KXANTHVSPEND", "KXGOOGVREQ", "KXOPENVREQ",
                  "KXDEEPVREQ", "KXANTHVREQ", "KXOPENSOURCESHARE")
ADVICE_SIZE_SEED = {"KXAPRPOTUS": ("2026-10-25", 3.0, 5.0),                # 49519da
                    "KXRT": ("2026-10-26", 2.0, 3.0),                      # 3a64752
                    **dict.fromkeys(_VERCEL_SERIES, ("2026-10-26", 2.0, 3.0))}
SIZE_HISTORY_PATH = os.path.join(LOG_DIR, "series_sizes.json")
# the 30 days' say on a cut the 7 days call for: a month whose quoting pays
# takes the cut a step down while the week's rewards run at least this share
# of the month's pace (below it, the month was paid by a program since gone)
ADVICE_MONTH_REW_SHARE = 0.5
ADVICE_NEW_EVENT = 25.0        # a new event of a running series: |net| or rewards at/above,
ADVICE_ROUTINE = 4             #   its series listing fewer events than this in the 30 days
ADVICE_MAX_NEW = 8
# size tests running now: a DOUBLE DOWN says so through each one's last ET day
ADVICE_SIZE_TESTS = (
    ("2026-11-08", "the base-size split test runs half the series at x1.5 from 10/26 to 11/08 "
                   "and will measure x1.5 book-wide"),
    ("2026-11-05", "the side leans and the long-shot cut report on 11/05"))


def load_imm_dashboard(path: str = IMM_DASH_HTML) -> dict:
    """The IMM dashboard's data object (its `const D = {...}`)."""
    with open(path, encoding="utf-8") as f:
        html = f.read()
    i = html.index("const D = ") + len("const D = ")
    data, _ = json.JSONDecoder().raw_decode(html, i)
    return data


def size_history(sizes: dict, old: dict, today_str: str) -> dict:
    """Each series' size after today's read: {series: {"x": its size outside
    the size test, "up": the ET day it last went up, "was": the size
    before}}. `sizes`: the dashboard's D.sizes, {series: [size now, the size
    test's part]}; a series it leaves out keeps its entry. A series new to
    the history takes ADVICE_SIZE_SEED's day while it reads the seed's size."""
    out = dict(old)
    for s, v in (sizes or {}).items():
        x = round(_f(v[0]) / (_f(v[1]) or 1.0), 3)
        prev = old.get(s)
        if prev is None:
            seed = ADVICE_SIZE_SEED.get(s)
            out[s] = ({"x": x, "up": seed[0], "was": seed[1]}
                      if seed and abs(seed[2] - x) < 0.01 else {"x": x})
        elif x > _f(prev.get("x")) * 1.01:
            out[s] = {"x": x, "up": today_str, "was": _f(prev.get("x"))}
        elif x < _f(prev.get("x")) * 0.99:
            out[s] = {"x": x}                       # sized down: not a size-up any more
    return out


def load_size_history(path: str = SIZE_HISTORY_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_size_history(hist: dict, path: str = SIZE_HISTORY_PATH) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _xg(v: float) -> str:
    """A size multiplier: 3, 4.5, 0.75."""
    return f"{round(v, 2):g}"


def _usd0(v: float) -> str:
    """+$1,234 / -$12 / $0: whole dollars for the advice lines."""
    if abs(v) < 0.5:
        return "$0"
    return f"{'-' if v < 0 else '+'}${abs(v):,.0f}"


def _nth(n: int) -> str:
    """1st, 2nd, 3rd, 4th, 11th, 21st."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# a day of a family / series / event: rewards, own fills, held re-marked,
# held settled, the own fills' 5-minute and 4-hour mark-outs, and the own
# fills of markets that settled that day
_PARTS = ("rew", "own", "cr", "cs", "m5", "m4", "own_set")


def _zero() -> dict:
    return dict.fromkeys(_PARTS, 0.0)


def _add(a: dict, b: dict) -> dict:
    return {k: a[k] + b[k] for k in _PARTS}


def _net(a: dict) -> float:
    return a["rew"] + a["own"] + a["cr"] + a["cs"]


def _fill_cause(a: dict) -> str:
    """How the own fills lost, from their mark-outs ("" when they did not,
    or no one horizon carries it)."""
    own = a["own"]
    if own > -0.5:
        return ""
    if a["m5"] <= ADVICE_PICKOFF_SHARE * own:
        return "most of it within 5 minutes of the fills (picked off: the takers knew first)"
    if a["own_set"] <= ADVICE_PICKOFF_SHARE * own:
        return "the fills ended on the wrong side of the settlement"
    if a["m4"] <= ADVICE_PICKOFF_SHARE * own:
        return "the price trended through the fills over the next hours"
    return ""


def _biggest(evs: dict, key, sign: int):
    """(event, $) with the largest `key` in the direction of `sign`, or None."""
    best = None
    for ev, a in evs.items():
        v = key(a)
        if v * sign > 0.5 and (best is None or v * sign > best[1] * sign):
            best = (ev, v)
    return best


def _ascii(s: str) -> str:
    """A title the cp1252 task console and the plain-text email can carry."""
    import unicodedata
    s = (s or "").replace("·", "-").replace("–", "-").replace("—", "-")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").strip()


def load_event_titles(dash_html: str = IMM_DASH_HTML) -> dict:
    """{event: Kalshi's title} from the dashboard's and the IMM Pocket's title
    caches beside the dashboard ({} when neither reads)."""
    out = {}
    cache = os.path.join(os.path.dirname(dash_html), "cache")
    for name in ("event_titles.json", "pocket_event_titles.json"):
        try:
            with open(os.path.join(cache, name), encoding="utf-8") as f:
                js = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(js, dict):
            for k, v in js.items():
                if isinstance(v, str) and v.strip() and k not in out:
                    out[k] = _ascii(v)
    return out


def _why_day(a: dict, evs: dict, name=lambda e: e) -> str:
    """What made a family's day: its biggest parts in the direction of its
    net (rewards and own fills read together; a second part only when it is
    a quarter of the net or more), each named by its biggest event (`name`:
    its title)."""
    sign = -1 if _net(a) < 0 else 1
    parts = [p for p in (("q", a["rew"] + a["own"]), ("cr", a["cr"]), ("cs", a["cs"]))
             if p[1] * sign > 0.5]
    parts.sort(key=lambda p: -abs(p[1]))
    parts = parts[:1] + [p for p in parts[1:2]
                         if abs(p[1]) >= max(15.0, 0.25 * abs(_net(a)))]
    out = []
    for k, v in parts:
        if k == "q":
            ev = _biggest(evs, lambda x: x["rew"] + x["own"], sign)
            if sign > 0:
                txt = f"rewards {_usd0(a['rew'])} against fills {_usd0(a['own'])}"
            else:
                txt = f"fills {_usd0(a['own'])} against rewards {_usd0(a['rew'])}"
                cause = _fill_cause(a)
                if cause:
                    txt += f" -- {cause}"
        elif k == "cr":
            ev = _biggest(evs, lambda x: x["cr"], sign)
            txt = (f"held positions marked {'down' if sign < 0 else 'up'} {_usd0(v)}")
        else:
            ev = _biggest(evs, lambda x: x["cs"], sign)
            txt = (f"held positions settled {'against us' if sign < 0 else 'our way'} "
                   f"{_usd0(v)}")
        out.append(txt + (f" ({name(ev[0])} {_usd0(ev[1])})" if ev else ""))
    return "; ".join(out)


def _position_detail(m: dict) -> str:
    pos, avg, mark = _f(m.get("pos")), _f(m.get("avg")), _f(m.get("mark"))
    side = "bought" if pos > 0 else "sold"
    return (f"{abs(pos):,.0f} YES {side} at {avg:.0f}c, now {mark:.0f}c: "
            f"{_usd0(_f(m.get('u')))}, ${abs(_f(m.get('wc'))):,.0f} left to lose")


def _position_line(t: str, m: dict, title: str = "") -> str:
    return f"{t}{' -- ' + title if title else ''} ({_position_detail(m)})"


def _short(s: str, n: int = 48) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n - 3].rstrip() + "..."


_TITLE_TAIL = frozenset(
    "on in for at of by the a an to from during between this his her their its vs and or - "
    "jan feb mar apr may jun "
    "jul aug sep sept oct nov dec january february march april june july august september "
    "october november december".split())


def _common_title(titles_: list) -> str:
    """What a series' events' titles share: the one title, or their common
    opening in whole words less a dangling date or connector ("Trump's
    approval rating on Oct 30, 2026?" + "... on Oct 23, 2026?" -> "Trump's
    approval rating"), "" under three words."""
    ts = [t for t in titles_ if t]
    if not ts:
        return ""
    if len(set(ts)) == 1:
        return ts[0]
    p = os.path.commonprefix(ts)
    words = p.split(" ")
    if not p.endswith(" "):
        words = words[:-1]                       # a word cut short
    while words and (not words[-1].strip(",:;(-") or
                     words[-1].lower().strip(",:;(-") in _TITLE_TAIL):
        words.pop()
    return " ".join(words) if len(words) >= 3 else ""


def _title_stem(title: str) -> str:
    """A title's first ADVICE_STEM_WORDS words ("" for a shorter title)."""
    words = re.findall(r"[A-Za-z0-9']+", title or "")
    return " ".join(words[:ADVICE_STEM_WORDS]) if len(words) > ADVICE_STEM_WORDS else ""


def advice_lines(ser_fam: dict, ser_grp: dict, ser_title: dict) -> dict:
    """{series: (family, line)}: a Sports & awards series' league (the NFL
    ladders keep their group), else its dashboard group when the group is
    more than the series, else the series -- or, when ADVICE_STEM_MIN or more
    of a family's single series have titles opening with the same words,
    "<those words> ..." for all of them."""
    out, singles = {}, defaultdict(list)
    for s, fam in ser_fam.items():
        grp = ser_grp.get(s) or s
        m = ADVICE_LEAGUE_RE.match(s)
        if fam == "Sports & awards" and grp != "Sports ladders" and m:
            out[s] = (fam, ADVICE_LEAGUES[m.group(1)])
        elif grp != s:
            out[s] = (fam, grp)
        else:
            singles[(fam, _title_stem(ser_title.get(s, "")))].append(s)
    for (fam, stem), ss in singles.items():
        for s in ss:
            out[s] = (fam, f"{stem} ..." if stem and len(ss) >= ADVICE_STEM_MIN else s)
    return out


def _parts_phrase(a: dict) -> str:
    """The part that made a P&L: its held positions, or its fills against
    its rewards."""
    held, q = a["cr"] + a["cs"], a["rew"] + a["own"]
    if abs(held) > abs(a["own"]) and abs(held) >= 0.5:
        return (f"held positions {_usd0(held)}"
                + (f", quoting {_usd0(q)}" if abs(q) >= 0.5 else ""))
    return f"fills {_usd0(a['own'])} against rewards {_usd0(a['rew'])}"


def _early_read(a: dict) -> str:
    """How something new is doing so far, from its parts."""
    rew, own, held = a["rew"], a["own"], a["cr"] + a["cs"]
    if rew < 0.5 and abs(own) < 0.5 and abs(held) < 0.5:
        return "nothing earned or traded yet"
    parts = (f"rewards {_usd0(rew)}, fills {_usd0(own)}"
             + (f", held {_usd0(held)}" if abs(held) >= 0.5 else ""))
    if rew + own < -0.5:
        cause = _fill_cause(a)
        return f"{parts}: the fills outrun the rewards" + (f", {cause}" if cause else "")
    if _net(a) < -0.5:
        return f"{parts}: the quoting pays, held positions marked down"
    return f"{parts}: earning, the rewards cover the fills"


def imm_advice(D: dict, today_str: str, titles=None, sizes=None) -> dict:
    """The What-to-do block from the dashboard data object (see the comment
    above): {"title", "basis", "headline", "drivers": [str], "recs": [(verb,
    text)], "context": [str], "new_head": str, "new": [str], "lines":
    [{"fam", "y", "w", "l", "rows": [{"name", "y", "w", "l", "call",
    "why"}], "rest": (n, y, w, l) or None}]}. Days before today_str only:
    the dashboard's last day is the one still running. `titles`: {event:
    title}, read from the dashboard's caches when None. `sizes`:
    size_history()'s result; None reads the dashboard's sizes with no history
    but ADVICE_SIZE_SEED."""
    titles = load_event_titles() if titles is None else titles
    dsz = D.get("sizes") or {}                    # series -> [size now, the size test's part]
    hist = size_history(dsz, {}, today_str) if sizes is None else sizes

    def evname(e):
        return titles.get(e) or e

    def evlabel(e):
        return f"{titles[e]} ({e})" if titles.get(e) else e
    ix = {f: i for i, f in enumerate(D["day_fields"])}
    fxk = {f: i for i, f in enumerate(D.get("fx_keys") or
                                      ("fe", "fe_cts", "m5", "m5_cts", "m4", "m4_cts",
                                       "mt", "mt_cts"))}
    mk = D.get("markets") or {}
    all_days = D.get("days") or {}
    days = sorted(d for d in all_days if d < today_str)
    if len(days) < 2:
        raise ValueError(f"only {len(days)} full day(s) on the dashboard")
    win = days[-ADVICE_DAYS:]
    prev = days[-2 * ADVICE_DAYS:-ADVICE_DAYS]
    long_ = days[-ADVICE_LONG_DAYS:]
    y = days[-1]
    n, nl = len(win), len(long_)
    daily = {}                                              # day -> [rewards, trading]
    fam_day = defaultdict(lambda: defaultdict(_zero))       # family -> day -> parts
    ser_day = defaultdict(lambda: defaultdict(_zero))       # series -> day -> parts
    ev_day = defaultdict(lambda: defaultdict(lambda: defaultdict(_zero)))
    ser_fam = {}
    ser_grp, ser_first, ev_first, ev_ser_d = {}, {}, {}, {}  # first: the first full day seen
    for d in days:
        tot = [0.0, 0.0]
        for t, v in ((all_days.get(d) or {}).get("m") or {}).items():
            rew, pnl = _f(v[ix["rew"]]), _f(v[ix["pnl"]])
            cr, cs = _f(v[ix["cr"]]), _f(v[ix["cs"]])
            fx = v[ix["fx"]] if "fx" in ix else 0
            own = pnl - cr - cs
            a = {"rew": rew, "own": own, "cr": cr, "cs": cs,
                 "m5": _f(fx[fxk["m5"]]) if fx else 0.0,
                 "m4": _f(fx[fxk["m4"]]) if fx else 0.0,
                 "own_set": own if ("settled" in ix and v[ix["settled"]]) else 0.0}
            tot[0] += rew
            tot[1] += pnl
            info = mk.get(t) or {}
            fam = info.get("fam") or "Other"
            ser = info.get("ser") or t.split("-", 1)[0]
            ev = info.get("ev") or event_from_ticker(t)
            ser_fam.setdefault(ser, fam)
            ser_grp.setdefault(ser, info.get("grp") or ser)
            ser_first.setdefault(ser, d)
            ev_first.setdefault(ev, d)
            ev_ser_d[ev] = ser
            fam_day[fam][d] = _add(fam_day[fam][d], a)
            ser_day[ser][d] = _add(ser_day[ser][d], a)
            ev_day[fam][d][ev] = _add(ev_day[fam][d][ev], a)
        daily[d] = tot

    def span(src, ds):
        out = _zero()
        for d in ds:
            if d in src:
                out = _add(out, src[d])
        return out

    fams = {fam: {"y": span(dd, [y]), "w": span(dd, win), "p": span(dd, prev),
                  "l": span(dd, long_)}
            for fam, dd in fam_day.items()}

    def wl(fam):
        """"7 days +$X, 30 days +$Y" for a family."""
        return (f"{n} days {_usd0(_net(fams[fam]['w']))}, "
                f"{nl} days {_usd0(_net(fams[fam]['l']))}")
    ydate = datetime.strptime(y, "%Y-%m-%d")

    def why_y(fam):
        return _why_day(fams[fam]["y"], ev_day[fam].get(y, {}), evname)

    # 1. yesterday, against the dashboard's other full days and the week
    y_rew, y_trd = daily[y]
    y_net = y_rew + y_trd
    worse = sum(1 for d in days if d != y and sum(daily[d]) < y_net)
    better = sum(1 for d in days if d != y and sum(daily[d]) > y_net)
    med = statistics.median(sum(daily[d]) for d in days)
    head = f"Yesterday {_usd0(y_net)}: rewards {_usd0(y_rew)}, trading {_usd0(y_trd)}. "
    if worse < ADVICE_BOTTOM_N and y_net < 0:
        which = "" if worse == 0 else _nth(worse + 1) + "-"
        head += (f"Below normal: the {which}worst of the last {len(days)} days "
                 f"(median {_usd0(med)}).")
    elif y_net < med:
        head += (f"Below the median: {worse} of the last {len(days)} days were worse "
                 f"(median {_usd0(med)}).")
    elif better < ADVICE_BOTTOM_N:
        head += (f"A strong day: {'none' if better == 0 else f'only {better}'} of the last "
                 f"{len(days)} days {'was' if better == 1 else 'were'} better "
                 f"(median {_usd0(med)}).")
    else:
        head += (f"A normal day: {worse} of the last {len(days)} days were worse "
                 f"(median {_usd0(med)}).")
    w_tot = sum(sum(daily[d]) for d in win)
    head += f" Last {n} days {_usd0(w_tot)} ({_usd0(w_tot / n)}/day)"
    if len(prev) == ADVICE_DAYS:
        head += f", vs {_usd0(sum(sum(daily[d]) for d in prev))} the {len(prev)} before"
    l_rew = sum(daily[d][0] for d in long_)
    l_trd = sum(daily[d][1] for d in long_)
    head += (f"; last {nl} days {_usd0(l_rew + l_trd)} ({_usd0((l_rew + l_trd) / nl)}/day: "
             f"rewards {_usd0(l_rew)}, trading {_usd0(l_trd)}).")

    # 2. what moved yesterday: the biggest families either way, with the week
    drivers = []
    for fam in sorted(fams, key=lambda f: -abs(_net(fams[f]["y"])))[:ADVICE_DRIVERS]:
        v = fams[fam]
        if abs(_net(v["y"])) < ADVICE_DRIVER_MIN:
            continue
        drivers.append(f"{fam} {_usd0(_net(v['y']))} ({wl(fam)}): {why_y(fam)}.")

    # 3. recommendations
    live_ser = {m.get("ser") or t.split("-", 1)[0] for t, m in mk.items() if m.get("sel")}
    live_fam = {m.get("fam") or "Other" for t, m in mk.items() if m.get("sel")}
    live_ev = {m.get("ev") or event_from_ticker(t) for t, m in mk.items() if m.get("sel")}
    recs = []
    paused = set()
    flag_verb = {}                    # series -> PAUSE / SHRINK named below (EVERY LINE)
    flag_ev = {}                      # event -> PAUSE / SHRINK named below
    month_notes = []                  # cuts the 30 days talked down to NO CHANGE
    month_fams = set()                # ... their families (no other NO CHANGE line)
    fam_week = set()                  # losing series of a family the month talked down

    def month(verb, dd):
        """The 30 days' say on a cut the 7 days call for (Jack 2026-10-25:
        "Make sure shrink/double down decisions are also contemplating 30D
        of data not just 7"): (verb, why). A month whose quoting still pays,
        on rewards still coming at its pace, takes the cut a step down --
        PAUSE to SHRINK, SHRINK to NO CHANGE; a month that paid on rewards
        since faded is no defence (the program that paid it is gone); a
        losing month agrees. Nothing to add for something no older than
        the week."""
        l, w = span(dd, long_), span(dd, win)
        act = [d for d in long_ if d in dd and abs(dd[d]["rew"]) + abs(dd[d]["own"]) > 0.5]
        if not act or act[0] >= win[0]:
            return verb, ""
        q30 = l["rew"] + l["own"]
        lose = sum(1 for d in act if dd[d]["rew"] + dd[d]["own"] < -0.5)
        days_txt = (f"{_usd0(q30)} over {nl}, losing on {lose} of the {len(act)} days "
                    f"it traded")
        if q30 <= 0.5:
            return verb, f"The {nl} days agree: {days_txt}."
        pace7, pace30 = w["rew"] / n, l["rew"] / nl
        if pace7 < ADVICE_MONTH_REW_SHARE * pace30:
            return verb, (f"Its {nl} days paid ({days_txt}), but on rewards since faded to "
                          f"{_usd0(pace7)}/day from {_usd0(pace30)}/day: no defence.")
        step = {"PAUSE": "SHRINK", "SHRINK": "NO CHANGE"}[verb]
        return step, (f"But its {nl} days still pay ({days_txt}), its rewards at their "
                      f"pace: " + ("shrink, not pause." if step == "SHRINK" else
                                   "a bad week in a paying month, no change yet."))
    # 3a. families whose quoting loses on most days: shrink the family
    fcands = []
    for fam, dd in fam_day.items():
        if fam not in live_fam:
            continue
        w, yy = span(dd, win), span(dd, [y])
        q7 = w["rew"] + w["own"]
        losing = sum(1 for d in win if d in dd and dd[d]["rew"] + dd[d]["own"] < -0.5)
        worst = sorted((span(sd, win)["rew"] + span(sd, win)["own"], s)
                       for s, sd in ser_day.items() if ser_fam.get(s) == fam)
        worst = [(q, s) for q, s in worst if q <= ADVICE_FAMILY_SERIES_MIN and s in live_ser]
        # a family-wide problem: two series or more losing and still quoted
        # (one alone is the series rule's, which names it more sharply; a
        # decided one -- the ALCS MVP -- is no problem to shrink)
        if q7 <= ADVICE_FAMILY_SHRINK_7D and losing >= ADVICE_LOSING_DAYS and len(worst) >= 2:
            fcands.append((q7, fam, w, yy["rew"] + yy["own"], losing, worst[:2]))
    for q7, fam, w, qy, losing, worst in sorted(fcands, key=lambda c: c[0])[:ADVICE_MAX_FAMILIES]:
        verb, why30 = month("SHRINK", fam_day[fam])
        cause = _fill_cause(w)
        txt = (f"the {fam} family. Why: its quoting lost {_usd0(q7)} over the {n} days "
               f"-- fills {_usd0(w['own'])} against rewards {_usd0(w['rew'])} -- losing on "
               f"{losing} of them; yesterday {_usd0(qy)}" + (f"; {cause}" if cause else "")
               + "." + (f" {why30}" if why30 else ""))
        if verb == "NO CHANGE":
            # the month pays: no family-wide cut; its losing series are
            # judged one by one, the family's week standing in for "again
            # yesterday" (a mention series fills in bursts, around its events)
            month_notes.append((verb, txt + " Its series are judged one by one instead."))
            month_fams.add(fam)
            fam_week.update(s for _, s in worst)
            continue
        bad_y = sorted((a["rew"] + a["own"], e) for e, a in ev_day[fam].get(y, {}).items()
                       if e in live_ev and a["rew"] + a["own"] < -0.5)[:2]
        if worst:
            txt += " Its worst series: " + ", ".join(f"{s} {_usd0(q)}" for q, s in worst) + "."
        if bad_y:
            txt += (" Still quoted and down yesterday: "
                    + ", ".join(f"{evlabel(e)} {_usd0(q)}" for q, e in bad_y) + ".")
        recs.append((verb, txt))
        paused.add(fam)
        flag_verb.update((s, "SHRINK") for _, s in worst)
    # 3b. series whose quoting loses on most days and again yesterday
    flagged_ser = set()
    cands = []
    for ser, dd in ser_day.items():
        if ser not in live_ser or ser_fam.get(ser) in paused:
            continue
        w, yy = span(dd, win), span(dd, [y])
        q7, qy = w["rew"] + w["own"], yy["rew"] + yy["own"]
        losing = sum(1 for d in win if d in dd and dd[d]["rew"] + dd[d]["own"] < -0.5)
        if (q7 <= ADVICE_SHRINK_7D and (qy < -0.5 or ser in fam_week)
                and losing >= ADVICE_LOSING_DAYS):
            cands.append((q7, ser, w, qy, losing))
    for q7, ser, w, qy, losing in sorted(cands, key=lambda c: c[0])[:ADVICE_MAX_PAUSE]:
        verb, why30 = month("PAUSE" if q7 <= ADVICE_PAUSE_7D else "SHRINK", ser_day[ser])
        cause = _fill_cause(w)
        txt = (f"{ser} ({ser_fam.get(ser, 'Other')}). Why: its quoting lost {_usd0(q7)} over "
               f"the {n} days -- fills {_usd0(w['own'])} against rewards {_usd0(w['rew'])} "
               f"-- losing on {losing} of them"
               + (f" and again yesterday ({_usd0(qy)})" if qy < -0.5 else
                  f" (yesterday {_usd0(qy)}), as its family lost the week")
               + (f"; {cause}" if cause else "") + "." + (f" {why30}" if why30 else ""))
        if verb == "NO CHANGE":
            month_notes.append((verb, txt))
            month_fams.add(ser_fam.get(ser))
            continue
        recs.append((verb, txt))
        paused.add(ser_fam.get(ser))
        flagged_ser.add(ser)
        flag_verb[ser] = verb
    # 3c. specific events still quoted that lost yesterday, by their own week
    ev_all = defaultdict(dict)
    ev_fam = {}
    for fam, dd in ev_day.items():
        for d, evs in dd.items():
            for e, a in evs.items():
                ev_all[e][d] = a
                ev_fam[e] = fam
    ev_ser = {(m.get("ev") or event_from_ticker(t)): (m.get("ser") or t.split("-", 1)[0])
              for t, m in mk.items()}
    ecands = []
    for e, dd in ev_all.items():
        yy = dd.get(y)
        if (yy is None or e not in live_ev or ev_fam.get(e) in paused
                or ev_ser.get(e) in flagged_ser):
            continue
        qy = yy["rew"] + yy["own"]
        if qy <= ADVICE_EVENT_Y:
            w = span(dd, win)
            ecands.append((qy, e, yy, w["rew"] + w["own"]))
    # a family's own one-bad-day line (below) already says "no change"
    one_bad = {f for f, v in fams.items() if _net(v["y"]) <= ADVICE_BAD_DAY and _net(v["w"]) > 0}
    ecands = [c for c in ecands
              if not (ev_fam.get(c[1]) in one_bad and c[3] > ADVICE_EVENT_SHRINK_7D)]
    for qy, e, yy, q7 in sorted(ecands, key=lambda c: c[0])[:ADVICE_MAX_EVENTS]:
        verb = ("PAUSE" if q7 <= ADVICE_EVENT_PAUSE_7D else
                "SHRINK" if q7 <= ADVICE_EVENT_SHRINK_7D else "NO CHANGE")
        cause = _fill_cause(yy)
        txt = (f"the event {evlabel(e)}, {ev_fam.get(e, 'Other')}. Why: its quoting lost "
               f"{_usd0(qy)} yesterday -- fills {_usd0(yy['own'])} against rewards "
               f"{_usd0(yy['rew'])}" + (f"; {cause}" if cause else ""))
        if verb == "NO CHANGE":
            txt += f" -- but it is {_usd0(q7)} over its {n} days: one bad day."
        else:
            verb, why30 = month(verb, ev_all[e])
            txt += (f" -- and {_usd0(q7)} over its {n} days, still quoted."
                    + (f" {why30}" if why30 else ""))
            if verb != "NO CHANGE":
                flag_ev[e] = verb
        recs.append((verb, txt))
    # held positions that cost the week, in families whose quoting pays
    held_fams = [f for f in sorted(fams, key=lambda f: fams[f]["w"]["cr"] + fams[f]["w"]["cs"])
                 if fams[f]["w"]["cr"] + fams[f]["w"]["cs"] <= ADVICE_HELD_7D
                 and fams[f]["w"]["rew"] + fams[f]["w"]["own"] > 0]
    opens = sorted(((t, m) for t, m in mk.items()
                    if m.get("fam") in held_fams and abs(_f(m.get("pos"))) > 0
                    and _f(m.get("u")) <= ADVICE_OPEN_MIN),
                   key=lambda tm: _f(tm[1].get("u")))
    trims = sorted((tm for tm in opens if abs(_f(tm[1].get("wc"))) >= ADVICE_TRIM_LEFT
                    and _f(tm[1].get("u")) <= ADVICE_TRIM_LOST),
                   key=lambda tm: _f(tm[1].get("wc")))[:ADVICE_MAX_TRIM]
    for t, m in trims:
        fam = m.get("fam")
        title = titles.get(m.get("ev") or event_from_ticker(t))
        recs.append(("TRIM", f"{t}{' -- ' + title if title else ''} if you no longer "
                             f"believe it. Why: ${abs(_f(m.get('wc'))):,.0f} is still at risk "
                             f"on a position already {_usd0(_f(m.get('u')))} "
                             f"({_position_detail(m)}), in {fam}, whose held positions cost "
                             f"{_usd0(fams[fam]['w']['cr'] + fams[fam]['w']['cs'])} over "
                             f"{n} days."))
    trimmed = {t for t, _ in trims}
    holds = [(t, m) for t, m in opens if t not in trimmed]
    if held_fams:
        held_tot = sum(fams[f]["w"]["cr"] + fams[f]["w"]["cs"] for f in held_fams)
        held_l = sum(fams[f]["l"]["cr"] + fams[f]["l"]["cs"] for f in held_fams)
        left = sum(abs(_f(m.get("wc"))) for _, m in holds)
        txt = (f"the open losers. Why: held positions cost {_usd0(held_tot)} over {n} days ("
               + ", ".join(f"{f} {_usd0(fams[f]['w']['cr'] + fams[f]['w']['cs'])}"
                           for f in held_fams)
               + f"; {_usd0(held_l)} over {nl}) while those families' quoting paid.")
        if holds:
            txt += (f" The {len(holds)} still open {'is' if len(holds) == 1 else 'are'} down "
                    f"{_usd0(sum(_f(m.get('u')) for _, m in holds))} with at most "
                    f"${left:,.0f} more to lose: selling at the bid would lock that in and "
                    f"pay the spread. Biggest: "
                    f"{_position_line(holds[0][0], holds[0][1], titles.get(holds[0][1].get('ev') or event_from_ticker(holds[0][0])) or '')}.")
        else:
            txt += " They have settled or recovered: nothing open to sell."
        recs.append(("HOLD", txt))
    recs += month_notes
    for fam in sorted(fams, key=lambda f: _net(fams[f]["y"])):
        v = fams[fam]
        if (_net(v["y"]) <= ADVICE_BAD_DAY and _net(v["w"]) > 0
                and fam not in held_fams and fam not in paused and fam not in month_fams):
            recs.append(("NO CHANGE", f"{fam}. Why: one bad day ({_usd0(_net(v['y']))}: "
                                      f"{why_y(fam)}) inside a {_usd0(_net(v['w']))} week "
                                      f"({nl} days {_usd0(_net(v['l']))})."))
    for fam in sorted(fams, key=lambda f: _net(fams[f]["w"])):
        v = fams[fam]
        if (_net(v["w"]) > ADVICE_REVIEW_NET or fam in held_fams or fam in paused
                or fam in month_fams):
            continue
        # a losing week with no series losing day after day: say where it went
        worst = sorted((_net(span(dd, win)), ser) for ser, dd in ser_day.items()
                       if ser_fam.get(ser) == fam)
        worst = [(q, ser) for q, ser in worst if q < -0.5][:2]
        w = v["w"]
        txt = (f"{fam}. Why: {_usd0(_net(w))} over {n} days (quoting "
               f"{_usd0(w['rew'] + w['own'])}, held positions {_usd0(w['cr'] + w['cs'])}), "
               f"but no series in it is losing day after day")
        if worst:
            txt += "; the biggest: " + ", ".join(
                f"{ser} {_usd0(q)}" + ("" if ser in live_ser else " (over)")
                for q, ser in worst)
        txt += f"; {nl} days {_usd0(_net(v['l']))}"
        if _net(v["y"]) > 0.5:
            txt += f"; yesterday {_usd0(_net(v['y']))}"
        recs.append(("NO CHANGE", txt + "."))
    # 3d. lines below the family, and where to double down
    ser_title = {}                    # the newest event's title (the stems)
    ser_titles = defaultdict(list)
    for e in sorted(ev_ser_d, key=lambda e: (ev_first[e], e)):
        if titles.get(e):
            ser_title[ev_ser_d[e]] = titles[e]
            ser_titles[ev_ser_d[e]].append(titles[e])
    ser_name = {s: _common_title(ts) for s, ts in ser_titles.items()}   # what to call it
    sline = advice_lines(ser_fam, ser_grp, ser_title)
    line_sers = defaultdict(set)
    for s, key in sline.items():
        line_sers[key].add(s)
    line_day = {}
    for key, ss in line_sers.items():
        ld = defaultdict(_zero)
        for s in ss:
            for d, a in ser_day[s].items():
                ld[d] = _add(ld[d], a)
        line_day[key] = ld
    line_evs = defaultdict(set)
    for e, s in ev_ser_d.items():
        line_evs[sline[s]].add(e)
    n_ev = defaultdict(int)                                  # series -> its events in the days
    for e, s in ev_ser_d.items():
        n_ev[s] += 1
    # running: quoted now, or earning yesterday, or -- a series listing an
    # event a week or so (daily, weekly) -- earning in the 7 days, between
    # its events now; an event: quoted now or earning yesterday
    live_now = live_ser | {s for s, dd in ser_day.items() if dd.get(y, _zero())["rew"] > 0.005}
    live_now |= {s for s, dd in ser_day.items() if n_ev[s] >= ADVICE_ROUTINE
                 and any(dd.get(d, _zero())["rew"] > 0.005 for d in win)}
    live_ev_now = live_ev | {e for e, dd in ev_all.items() if dd.get(y, _zero())["rew"] > 0.005}

    def sname(s):
        return f"{s} -- {_short(ser_name[s])}" if ser_name.get(s) else s

    def lname(key):
        ss = line_sers[key]
        if len(ss) > 1:
            return f"{key[1]} ({len(ss)} series)"
        s = next(iter(ss))
        return f"{key[1]} ({s})" if key[1] != s else sname(s)
    r_up = ADVICE_DD_MULT ** ADVICE_DD_REW_ELAST - 1
    f_up = ADVICE_DD_MULT ** ADVICE_DD_FILL_ELAST - 1
    dd_cands = []
    for key, ld in line_day.items():
        ss = line_sers[key]
        if (not ss & live_now or key[0] in paused or any(s in flag_verb for s in ss)
                or line_evs[key] & set(flag_ev)):
            continue
        w, l = span(ld, win), span(ld, long_)
        q7, q30 = w["rew"] + w["own"], l["rew"] + l["own"]
        up = sum(1 for d in win if d in ld and ld[d]["rew"] + ld[d]["own"] > 0.5)
        up30 = sum(1 for d in long_ if d in ld and ld[d]["rew"] + ld[d]["own"] > 0.5)
        active = sum(1 for d in long_ if d in ld and ld[d]["rew"] > 0.5)
        active7 = sum(1 for d in win if d in ld and ld[d]["rew"] > 0.5)
        tr7, tr30 = w["own"] + w["cr"] + w["cs"], l["own"] + l["cr"] + l["cs"]
        # a day earning, at the 7-day pace and at the 30-day pace, the lower:
        # a line whose rewards faded this week (or whose held positions
        # just cost it) does not size up on last month's numbers. Held
        # positions count with the fills, and only as losses: a lucky carry
        # is no reason to size up.
        gain = min((r_up * w["rew"] + f_up * min(0.0, tr7)) / max(1, active7),
                   (r_up * l["rew"] + f_up * min(0.0, tr30)) / max(1, active))
        # the 30 days as well as the 7 (Jack 2026-10-25): up on most of the
        # days it earned, its quoting up over them, and the gain at their pace
        if (active >= ADVICE_DD_ACTIVE and _net(l) >= ADVICE_DD_30D and _net(w) >= 0
                and q7 >= ADVICE_DD_7D and up >= ADVICE_DD_UP_DAYS and gain >= ADVICE_DD_GAIN
                and q30 > 0 and up30 >= ADVICE_DD_UP_SHARE_30 * active):
            dd_cands.append((gain, key, w, l, q7, q30, up, up30, active, tr7, tr30))
    dd_cands.sort(key=lambda c: (-c[0], c[1]))

    def fmt(d):
        return f"{datetime.strptime(d, '%Y-%m-%d'):%a %m/%d}"

    def sized_up(key):
        """A line's latest size-up inside ADVICE_DD_RESIZE_DAYS: (ET day,
        size before, size after), or None."""
        t0 = datetime.strptime(today_str, "%Y-%m-%d")
        ups = [(h["up"], _f(h.get("was")), _f(h.get("x")))
               for h in (hist.get(s) or {} for s in line_sers[key])
               if h.get("up") and (t0 - datetime.strptime(h["up"], "%Y-%m-%d")).days
               < ADVICE_DD_RESIZE_DAYS]
        return max(ups) if ups else None

    def size_now(key):
        """(the line's size now, whether a series of it is in the size
        test's half), or (None, False) when the dashboard gave no sizes."""
        ss = [s for s in line_sers[key] if s in dsz]
        if not ss:
            return None, False
        return max(_f(dsz[s][0]) for s in ss), any(_f(dsz[s][1]) > 1.0 for s in ss)
    picked, dd_held = [], []
    for c in dd_cands:
        su = sized_up(c[1])
        now, in_test = size_now(c[1])
        if su:                        # its numbers ran mostly at the old size
            again = datetime.strptime(su[0], "%Y-%m-%d") + timedelta(days=ADVICE_DD_RESIZE_DAYS)
            dd_held.append((True, f"{lname(c[1])} x{_xg(su[1])} -> x{_xg(su[2])} on "
                                  f"{fmt(su[0])} (judged again from {again:%a %m/%d})"))
        elif in_test:                 # the size test sizes it up already
            dd_held.append((False, f"{lname(c[1])} (in the size test's x1.5 half)"))
        elif len(picked) < ADVICE_MAX_DD:
            picked.append((c, now))
    dd_recs = []
    for (gain, key, w, l, q7, q30, up, up30, active, tr7, tr30), now in picked:
        to = f" (x{_xg(now)} -> x{_xg(now * ADVICE_DD_MULT)})" if now else ""
        dd_recs.append((key, ("DOUBLE DOWN", (
            f"{lname(key)}, {key[0]}: x{ADVICE_DD_MULT:g}{to}. Why: its quoting was up on {up} of "
            f"the {n} days ({_usd0(q7)}, net {_usd0(_net(w))}) and on {up30} of {active} "
            f"earning days over the {nl} ({_usd0(q30)}, net {_usd0(_net(l))}). Its fills and "
            f"held positions came to "
            f"{_usd0(tr7)} over the {n} days and {_usd0(tr30)} over {nl}, against rewards "
            f"{_usd0(w['rew'])} and {_usd0(l['rew'])}: light enough that x{ADVICE_DD_MULT:g} "
            f"adds about {_usd0(gain)} a day earning at either pace (the 10/11 size study: "
            f"rewards +{r_up:.0%}, fill losses +{f_up:.0%})."))))
    dd_keys = {key for key, _ in dd_recs}
    if not any(verb in ("PAUSE", "SHRINK", "HOLD", "TRIM") for verb, _ in recs):
        recs.insert(0, ("NO CUTS" if dd_recs else "NO CHANGES",
                        f"Why: no series' quoting lost on most of the last {n} days and again "
                        f"yesterday, and no family's held positions lost more than "
                        f"{_usd0(-ADVICE_HELD_7D)[1:]} while its quoting paid."))
    recs += [rec for _, rec in dd_recs]
    noted = {t.split(".", 1)[0] for verb, t in recs if verb == "NO CHANGE"}
    keep = sorted((f for f, v in fams.items()
                   if _net(v["w"]) >= ADVICE_KEEP_NET and v["w"]["rew"] + v["w"]["own"] > 0
                   and f not in paused and f not in held_fams and f not in noted),
                  key=lambda f: -_net(fams[f]["w"]))[:3]
    if keep:
        recs.append(("KEEP", ", ".join(f"{f} {_usd0(_net(fams[f]['w']))}" for f in keep)
                     + f" over {n} days ({nl} days "
                     + ", ".join(_usd0(_net(fams[f]["l"])) for f in keep)
                     + "). Why: their rewards cover their fills."))

    # 4. new this week: lines and series first seen in the 7 days, and new
    # events of series that list few (a daily or weekly series' are routine)
    new_ser = {s for s, d in ser_first.items() if d >= win[0]}
    new_lines = sorted((k for k, ss in line_sers.items() if len(ss) > 1 and ss <= new_ser),
                       key=lambda k: (-abs(_net(span(line_day[k], win))), k))
    in_new = set().union(*(line_sers[k] for k in new_lines))
    items = []                                               # (parts so far, text)
    for k in new_lines:
        a = span(line_day[k], win)
        items.append((a, f"{lname(k)}, {k[0]}, from {fmt(min(ser_first[s] for s in line_sers[k]))}"
                         f": {_usd0(_net(a))} so far -- {_early_read(a)}; yesterday "
                         f"{_usd0(_net(span(line_day[k], [y])))}."))
    more = []                         # new series, then new events, biggest first
    for s in new_ser - in_new:
        a = span(ser_day[s], win)
        fam, ln = sline[s]
        more.append((-abs(_net(a)), s, a, f"{sname(s)}, {fam}{'' if ln == s else ' / ' + ln}, "
                                          f"from {fmt(ser_first[s])}: {_usd0(_net(a))} so far -- "
                                          f"{_early_read(a)}."))
    for e, d0 in ev_first.items():
        s = ev_ser_d[e]
        if d0 < win[0] or s in new_ser or n_ev[s] >= ADVICE_ROUTINE:
            continue
        a = span(ev_all.get(e, {}), win)
        if abs(_net(a)) >= ADVICE_NEW_EVENT or a["rew"] >= ADVICE_NEW_EVENT:
            more.append((-abs(_net(a)), e, a, f"the event {evlabel(e)}, {ev_fam.get(e, 'Other')} "
                                              f"/ {s}, from {fmt(d0)}: {_usd0(_net(a))} so far -- "
                                              f"{_early_read(a)}."))
    items += [(a, t) for _, _, a, t in sorted(more, key=lambda m: m[:2])]
    new = [t for _, t in items[:ADVICE_MAX_NEW]]
    if len(items) > ADVICE_MAX_NEW:
        a = _zero()
        for b, _ in items[ADVICE_MAX_NEW:]:
            a = _add(a, b)
        new.append(f"{len(items) - ADVICE_MAX_NEW} more, together {_usd0(_net(a))} "
                   f"({_early_read(a)}).")
    tot = _zero()
    for s in new_ser:
        tot = _add(tot, span(ser_day[s], win))
    new_head = (f"{len(new_ser)} series first seen {fmt(win[0])}-{fmt(y)}"
                + (f" ({len(new_lines)} new line{'' if len(new_lines) == 1 else 's'})"
                   if new_lines else "")
                + f": {_usd0(_net(tot))} so far (rewards {_usd0(tot['rew'])}, fills "
                  f"{_usd0(tot['own'])}, held {_usd0(tot['cr'] + tot['cs'])})."
                if new_ser else "")

    # 5. every line: yesterday / 7 days / 30 days, a call and its why
    def pick(key, ds, sign):
        """(name, running, parts) of the line's event -- or series, when one
        is twice the biggest event -- with the biggest net over `ds` in the
        direction of `sign`; None when nothing moved that way."""
        best = None
        for e in line_evs[key]:
            a = span(ev_all.get(e, {}), ds)
            if _net(a) * sign > 0.5 and (best is None or _net(a) * sign > _net(best[2]) * sign):
                best = (_short(titles.get(e) or e, 44), e in live_ev_now, a)
        for s in line_sers[key] if len(line_sers[key]) > 1 else ():
            a = span(ser_day[s], ds)
            if n_ev[s] > 1 and _net(a) * sign > 0.5 and (
                    best is None or _net(a) * sign >= 2 * _net(best[2]) * sign):
                best = (f"{s} ({n_ev[s]} events)", s in live_now, a)
        return best

    def phrase(u, key, tag=True):
        """A picked event or series: its name (not for a one-event line),
        whether it is still quoted (`tag`), its net and what made it."""
        head = (("" if len(line_evs[key]) == 1 else u[0])
                + (" (not quoted now)" if tag and not u[1] else "")).strip()
        return (f"{head} {_usd0(_net(u[2]))}: " if head else "") + _parts_phrase(u[2])

    def line_call(key, w, l):
        call, why = line_call0(key, w, l)
        flagged = sorted((flag_ev[e], e) for e in line_evs[key] if e in flag_ev)
        if flagged and call not in ("PAUSE", "SHRINK"):
            why += "; " + ", ".join(f"{v} the event {_short(titles.get(e) or e, 44)}"
                                    for v, e in flagged) + " (see the recommendations)"
        su = sized_up(key)
        if su:
            why += f"; sized up x{_xg(su[1])} -> x{_xg(su[2])} on {fmt(su[0])}"
        return call, why

    def line_call0(key, w, l):
        ss = line_sers[key]
        if key in dd_keys:
            return "DOUBLE DOWN", f"x{ADVICE_DD_MULT:g}, see the recommendations"
        verbs = {flag_verb[s] for s in ss if s in flag_verb}
        if verbs:
            return ("PAUSE" if "PAUSE" in verbs else "SHRINK"), "see the recommendations"
        first = min(ser_first[s] for s in ss)
        if first >= win[0]:
            return "NEW", f"from {fmt(first)}: {_early_read(w)}"
        if not ss & live_now:
            ld = line_day[key]
            last = max((d for d in long_ if d in ld and ld[d]["rew"] > 0.005), default=None)
            u = pick(key, win, -1 if _net(w) < 0 else 1)
            return "IDLE", ("nothing quoted now" + (f", last earned {fmt(last)}" if last else "")
                            + (f"; {phrase(u, key, tag=False)}" if u else ""))
        call, why = line_call1(key, w, l)
        if l["rew"] >= ADVICE_FADE_REW and w["rew"] < ADVICE_FADE_SHARE * l["rew"] * n / nl:
            call, why = "WATCH", (f"rewards faded to {_usd0(w['rew'] / n)}/day over the {n} days "
                                  f"from {_usd0(l['rew'] / nl)}/day over {nl}; {why}")
        return call, why

    def line_call1(key, w, l):                 # a quoted line by its P&L
        if _net(w) < -0.5:
            u = pick(key, win, -1)
            txt = phrase(u, key) if u else _parts_phrase(w)
            if u and not u[1]:               # the drag stopped: what the running part did
                la = _zero()
                for e in line_evs[key] & live_ev_now:
                    la = _add(la, span(ev_all.get(e, {}), win))
                txt += f"; the markets still quoted {_usd0(_net(la))} ({_parts_phrase(la)})"
            if _net(l) > 0.5:                # a down week inside an up month
                txt += f"; still {_usd0(_net(l))} over {nl} days"
            return "WATCH", txt
        if _net(l) < -0.5:
            u = pick(key, long_, -1)
            return "WATCH", (f"up {_usd0(_net(w))} over {n} days after {_usd0(_net(l))} over "
                             f"{nl}" + (f"; the {nl}-day drag: {phrase(u, key)}" if u else ""))
        u = pick(key, win, 1) or pick(key, long_, 1)
        return "KEEP", (f"rewards {_usd0(l['rew'])} against fills {_usd0(l['own'])} over {nl} "
                        f"days" + (f"; biggest: {phrase(u, key)}"
                                   if u and len(line_evs[key]) > 1 else ""))
    lines = []
    for fam in sorted({k[0] for k in line_day}):
        rows, rest_n, rest = [], 0, [_zero(), _zero(), _zero()]
        for key in sorted((k for k in line_day if k[0] == fam),
                          key=lambda k: (-_net(span(line_day[k], win)), k[1])):
            ld, ss = line_day[key], line_sers[key]
            yy, w, l = span(ld, [y]), span(ld, win), span(ld, long_)
            moved = any(abs(ld[d][p]) > 0.5 for d in win if d in ld for p in ("rew", "own", "cr", "cs"))
            if ss & live_now:
                shown = (key in dd_keys or any(s in flag_verb for s in ss)
                         or l["rew"] >= ADVICE_LINE_REW or abs(_net(l)) >= ADVICE_LINE_NET
                         or abs(_net(w)) >= ADVICE_LINE_7D)
            else:                            # idle: only what still moves the book
                shown = moved and (abs(_net(l)) >= ADVICE_IDLE_NET
                                   or abs(_net(w)) >= ADVICE_LINE_NET)
            if shown:
                call, why = line_call(key, w, l)
                rows.append({"name": lname(key), "y": _net(yy), "w": _net(w), "l": _net(l),
                             "call": call, "why": why})
            else:
                rest_n += 1
                rest = [_add(rest[0], yy), _add(rest[1], w), _add(rest[2], l)]
        lines.append({"fam": fam, "y": _net(fams[fam]["y"]), "w": _net(fams[fam]["w"]),
                      "l": _net(fams[fam]["l"]), "rows": rows,
                      "rest": (rest_n, _net(rest[0]), _net(rest[1]), _net(rest[2]))
                      if rest_n else None})

    # 6. context: yesterday's rewards against the week, the week against the one before
    context = []
    r_avg = sum(daily[d][0] for d in win) / n
    if r_avg > 0 and y_rew < (1 - ADVICE_REWARD_DROP) * r_avg:
        drops = sorted((fams[f]["y"]["rew"] - fams[f]["w"]["rew"] / n, f) for f in fams)
        drops = [(dv, f) for dv, f in drops if dv < -0.5][:2]
        context.append(f"Rewards {_usd0(y_rew)} were {100 * (1 - y_rew / r_avg):.0f}% under "
                       f"the {n}-day average ({_usd0(r_avg)}/day)"
                       + ("; the biggest drops against each family's average: "
                          + ", ".join(f"{f} {_usd0(dv)}" for dv, f in drops) if drops else "")
                       + ".")
    if len(prev) == ADVICE_DAYS:
        w = [sum(daily[d][k] for d in win) for k in (0, 1)]
        pv = [sum(daily[d][k] for d in prev) for k in (0, 1)]
        line = (f"Week over week: {_usd0(sum(w))} vs {_usd0(sum(pv))} (rewards "
                f"{_usd0(w[0] - pv[0])}, trading {_usd0(w[1] - pv[1])})")
        moves = sorted((_net(v["w"]) - _net(v["p"]), f) for f, v in fams.items())
        drops = [(dv, f) for dv, f in moves if dv < -0.5][:2]
        if sum(w) < sum(pv) and drops:
            line += "; the biggest drops: " + ", ".join(f"{f} {_usd0(dv)}" for dv, f in drops)
        context.append(line + ".")
    if dd_held:
        more = len(dd_held) - ADVICE_MAX_DD_HELD
        context.append("Already sized up, so no DOUBLE DOWN yet: "
                       + "; ".join(t for _, t in dd_held[:ADVICE_MAX_DD_HELD])
                       + (f"; and {more} more" if more > 0 else "") + "."
                       + (f" A size-up runs {ADVICE_DD_RESIZE_DAYS} days at its new size before "
                          f"it is judged again." if any(c[0] for c in dd_held) else ""))
    tests = [txt for end, txt in ADVICE_SIZE_TESTS if today_str <= end]
    if dd_recs and tests:
        context.append("Before doubling down: " + "; ".join(tests) + ".")
    if not dd_recs and not dd_held:
        context.append(f"Double down: no line clears the bar today. Size pays only where fills "
                       f"stay light next to the rewards (fill losses grow about "
                       f"{ADVICE_DD_FILL_ELAST:.2f} per unit of size, rewards "
                       f"{ADVICE_DD_REW_ELAST:.2f}).")
    basis = (f"the IMM bot's book on its dashboard: yesterday against the {n} full ET days "
             f"{win[0][5:].replace('-', '/')}-{win[-1][5:].replace('-', '/')} and the {nl} "
             f"from {long_[0][5:].replace('-', '/')}; rewards as modelled; own fills = the "
             f"day's fills at the day's end, held = positions from earlier days")
    gen = _f(D.get("generated"))
    midnight = ET.localize(datetime.strptime(today_str, "%Y-%m-%d")).timestamp()
    if gen and gen < midnight:
        basis += (f"; the dashboard was last built "
                  f"{datetime.fromtimestamp(gen, ET):%m/%d %H:%M} ET, so yesterday may be "
                  f"partial")
    return {"title": f"What to do -- {ydate:%a %m/%d}", "basis": basis, "headline": head,
            "drivers": drivers, "recs": recs, "context": context, "new_head": new_head,
            "new": new, "lines": lines, "long_days": nl}


def advice_text(advice) -> list:
    """The What-to-do block, plain text lines."""
    if advice is None:
        return []
    if advice.get("error"):
        return [f"What to do: unavailable ({advice['error']})", ""]
    out = [f"{advice['title']} ({advice['basis']}):", f"  {advice['headline']}"]
    if advice["drivers"]:
        out.append("  What moved it:")
        out += [f"    - {t}" for t in advice["drivers"]]
    out.append("  Recommendations:")
    out += [f"    {i}. {verb} {t}" for i, (verb, t) in enumerate(advice["recs"], 1)]
    if advice.get("new"):
        out.append(f"  New this week: {advice['new_head']}")
        out += [f"    - {t}" for t in advice["new"]]
    out += [f"  {t}" for t in advice["context"]]
    if advice.get("lines"):
        out.append("  Every line (MLB, Red Rocks, ...): at the end of this email.")
    return out + [""]


def advice_lines_text(advice) -> list:
    """EVERY LINE, plain text: a family's totals, then its lines."""
    if not advice or advice.get("error") or not advice.get("lines"):
        return []
    out = [f"Every line, yesterday / {ADVICE_DAYS} days / {advice.get('long_days', 30)} days "
           f"(the IMM book; What to do's calls):"]
    for f in advice["lines"]:
        out.append(f"  {f['fam']}: {_usd0(f['y'])} / {_usd0(f['w'])} / {_usd0(f['l'])}")
        for r in f["rows"]:
            out.append(f"    - {r['name']}: {_usd0(r['y'])} / {_usd0(r['w'])} / {_usd0(r['l'])}. "
                       f"{r['call']}: {r['why']}.")
        if f["rest"]:
            k, yv, wv, lv = f["rest"]
            out.append(f"    - {k} smaller line{'' if k == 1 else 's'}: {_usd0(yv)} / "
                       f"{_usd0(wv)} / {_usd0(lv)}")
    return out + [""]


_VERB_COLOR = {"PAUSE": C_NEG, "SHRINK": C_NEG, "TRIM": C_NEG, "HOLD": "#9a6a00",
               "WATCH": "#9a6a00", "KEEP": C_POS, "DOUBLE DOWN": C_POS, "NEW": C_EQUITY,
               "IDLE": C_MUTED}


def advice_html(advice) -> str:
    if advice is None:
        return ""
    if advice.get("error"):
        return (f'<div style="color:{C_MUTED};margin:8px 0">What to do: unavailable '
                f'({_esc(advice["error"])})</div>')
    sub = 'style="font-size:13px;font-weight:600;margin:8px 0 2px"'
    out = [f'<div style="border:1px solid {C_GRID};border-radius:6px;padding:8px 12px;'
           f'margin:10px 0;background:#fbfbf8">'
           f'<div style="font-size:15px;font-weight:600">{_esc(advice["title"])}</div>'
           f'<div style="color:{C_MUTED};font-size:12px">{_esc(advice["basis"])}</div>'
           f'<div style="margin:6px 0">{_esc(advice["headline"])}</div>']
    if advice["drivers"]:
        out.append(f'<div {sub}>What moved it</div><ul style="margin:2px 0;padding-left:20px">'
                   + "".join(f'<li style="margin:3px 0">{_esc(t)}</li>'
                             for t in advice["drivers"]) + "</ul>")
    lis = []
    for verb, t in advice["recs"]:
        color = _VERB_COLOR.get(verb, "#555555")
        lis.append(f'<li style="margin:4px 0"><b style="color:{color}">{_esc(verb)}</b> '
                   f'{_esc(t)}</li>')
    out.append(f'<div {sub}>Recommendations</div>'
               f'<ol style="margin:2px 0;padding-left:20px">{"".join(lis)}</ol>')
    if advice.get("new"):
        out.append(f'<div {sub}>New this week</div>'
                   f'<div style="color:{C_INK2};font-size:12px">{_esc(advice["new_head"])}</div>'
                   f'<ul style="margin:2px 0;padding-left:20px">'
                   + "".join(f'<li style="margin:3px 0">{_esc(t)}</li>' for t in advice["new"])
                   + "</ul>")
    context = list(advice["context"])
    if advice.get("lines"):
        context.append("Every line (MLB, Red Rocks, ...): at the end of this email.")
    if context:
        out.append(f'<div style="color:{C_MUTED};font-size:12px;margin-top:6px">'
                   + "<br>".join(_esc(t) for t in context) + "</div>")
    return "".join(out) + "</div>"


def advice_lines_html(advice) -> str:
    """EVERY LINE: one table, a family's totals over its lines, each line's
    call and why under its name (four columns: a phone reads it). Lean
    markup -- attributes over repeated inline styles -- as the email runs
    near Gmail's ~102 KB clip."""
    if not advice or advice.get("error") or not advice.get("lines"):
        return ""
    nl = advice.get("long_days", 30)

    def cells(vals):
        return "".join(f'<td align="right" style="color:'
                       f'{C_POS if v > 0.5 else C_NEG if v < -0.5 else C_INK2}">'
                       f'{_esc(_usd0(v))}</td>' for v in vals)
    out = [f'<div style="font-size:15px;font-weight:600;margin:16px 0 2px">Every line</div>'
           f'<div style="color:{C_MUTED};font-size:12px;margin-bottom:4px">The IMM book below the '
           f'family -- a sports league, a dashboard group, or a series -- with What to do\'s '
           f'call on each. Yesterday / {ADVICE_DAYS} days / {nl} days.</div>'
           f'<table cellpadding="3" cellspacing="0" width="100%" '
           f'style="border-collapse:collapse;font-size:12px">'
           f'<tr bgcolor="#f0f0f0"><th align="left">Line</th><th align="right">Yday</th>'
           f'<th align="right">{ADVICE_DAYS} days</th><th align="right">{nl} days</th></tr>']
    for f in advice["lines"]:
        out.append(f'<tr bgcolor="#f6f6f4" valign="top" style="font-weight:600"><td>'
                   f'{_esc(f["fam"])}</td>{cells((f["y"], f["w"], f["l"]))}</tr>')
        for r in f["rows"]:
            color = _VERB_COLOR.get(r["call"], "#555555")
            out.append(f'<tr valign="top"><td style="padding-left:12px">{_esc(r["name"])}<br>'
                       f'<span style="font-size:11px;color:{C_INK2}"><b style="color:{color}">'
                       f'{_esc(r["call"])}</b> {_esc(r["why"])}</span></td>'
                       f'{cells((r["y"], r["w"], r["l"]))}</tr>')
        if f["rest"]:
            k, yv, wv, lv = f["rest"]
            out.append(f'<tr valign="top" style="color:{C_MUTED}"><td style="padding-left:12px">'
                       f'{k} smaller line{"" if k == 1 else "s"}</td>{cells((yv, wv, lv))}</tr>')
    return "".join(out) + "</table>"


def sub_pnl_by(pf):
    """{n: the day's P&L of numbered subaccount n} = its value move plus the
    cash that left it (a move is no gain or loss). None when either
    morning's per-account values or today's per-account moves are missing,
    or the set of subaccounts changed."""
    before = (pf.get("prior") or {}).get("subs")
    now = (pf.get("subs") or {}).get("accounts")
    moves = pf.get("sub_moves_by")
    if not now or not before or moves is None:
        return None
    before = {int(k): _f(v) for k, v in before.items()}
    if set(before) != set(now):
        return None
    return {n: round(_f(v) - before[n] + moves.get(n, 0.0), 2) for n, v in now.items()}


def day_by_bot(pf, imm_events, rewards_part, d_perps, subs_pnl):
    """The day by bot (BY_BOT_ENABLED): [(bot, $, [(label, $), ...])] over
    BOTS. Each bot's line is its rows' trading at mid plus its own non-trade
    lines: the reward credits (rewards_part, the split's (label, $)) for the
    IMM, the perpetuals for crypto, each numbered subaccount for its bot.
    These plus Kalshi's pricing vs mid make the day change. None without the
    IMM's book (no dashboard) or the day's replay."""
    if imm_events is None or rewards_part is None:
        return None
    trading = dict.fromkeys(BOTS, 0.0)
    for r in pf["rows"]:
        trading[bot_of_event(r["event"], imm_events)] += r["day"]
    parts = {b: [("trading", round(trading[b], 2))] for b in BOTS}
    parts["IMM"].append(rewards_part)
    if d_perps is not None:
        parts["Crypto"].append(("perpetuals", d_perps))
    if subs_pnl is not None:
        by = sub_pnl_by(pf)
        if by is None:
            parts["Other"].append(("subaccounts", subs_pnl))
        else:
            for n, v in sorted(by.items()):
                parts[SUBACCOUNT_BOTS.get(n, "Other")].append((f"subaccount {n}", v))
    return [(b, round(sum(v for _, v in parts[b]), 2), parts[b]) for b in BOTS]


# Kalshi's reward batch usually lands before the 7am read (~10:15Z); a day
# with no credits at all most likely means it came late (10/22, after the
# Thursday 03:00-05:00 ET maintenance: +$0.00 at 7am, ~+$745 by 13:02 ET)
LATE_PAYOUT_NOTE = ("no reward payout had landed by 7am; a late one shows in "
                    "tomorrow's credits")


def build_email(pf, history, chart_ok: bool, imm=None, advice=None, imm_events=None):
    """(subject, text, html). `imm` is imm_section()'s result: the IMM bot's
    section goes after the movers table, and its PICK-OFF flag onto the
    subject; None leaves the email as it was before 2026-10-02. `advice` is
    imm_advice()'s result (or {"error": why}): the What-to-do block, under
    the day change; None leaves it out. `imm_events` is imm_book_events():
    the day-by-bot block under the day change; None leaves it out."""
    today = pf["today"]
    # The "Settled since yesterday, by series" table is gone (Jack
    # 2026-09-29: "remove 'Settled since yesterday, by series'"); settled
    # events stay in the movers table, tagged "settled".
    first = pf["first_run"]
    prior = pf["prior"] or {}
    # Account value on Kalshi's own valuation (cash + their portfolio_value)
    # plus the perpetuals account's equity (9/28 on). The day change compares
    # like with like: perps enter it once both mornings have a value.
    ek = pf["equity_kalshi"]
    perps_eq = pf.get("perps_equity")
    acct = pf.get("account_value", ek)
    d_ek = None if first else round(
        ek - _f(prior.get("equity_kalshi") or prior.get("equity")), 2)
    prior_perps = prior.get("perps_equity")
    d_perps = (None if first or perps_eq is None or prior_perps is None
               else round(perps_eq - _f(prior_perps), 2))
    # Numbered subaccounts (10/9 on), like the perps: in the day change once
    # both mornings have a value. Cash moved between them and the primary
    # (sub_moves, + = into the primary) is no gain or loss: it comes out of
    # the reward credits, and the subaccounts' line is their move net of it.
    subs_eq = pf.get("subs_equity")
    prior_subs = prior.get("subs_equity")
    d_subs = (None if first or subs_eq is None or prior_subs is None
              else round(subs_eq - _f(prior_subs), 2))
    moves = None if first else pf.get("sub_moves", 0.0)
    subs_pnl = None if d_subs is None else round(d_subs + (moves or 0.0), 2)
    d_equity = None if first else round(d_ek + (d_perps or 0.0) + (d_subs or 0.0), 2)
    # Kalshi's numbered subaccounts are API-only ("not yet supported in the
    # Kalshi web or mobile app", docs.kalshi.com/getting_started/subaccounts),
    # so the app's figure is the main account plus the perps: the headline
    # gives it beside the account value (Jack 2026-10-28: "app shows 58.5k
    # across main account and perps", the email $59.6k).
    if subs_eq is None:
        subs_note = ""
    else:
        why = ("yesterday's value: today's read failed" if pf.get("subs_stale")
               else "first counted today, so not in the day change"
               if not first and prior_subs is None else "")
        subs_note = " (API-only, not in the app" + (f"; {why}" if why else "") + ")"
    app_value = None if subs_eq is None else round(ek + (perps_eq or 0.0), 2)
    accts = (pf.get("subs") or {}).get("accounts") or {}
    subs_label = (f"subaccount {next(iter(accts))}" if len(accts) == 1
                  else "subaccounts")
    unpaid = pf.get("unpaid") or None
    unpaid_total = unpaid.get("total") if unpaid else None
    after_txt = ("" if unpaid_total is None else
                 f"  (est. ${acct + unpaid_total:,.2f} after rewards are paid out)")
    if perps_eq is None:
        perps_note = ""
    elif pf.get("perps_stale"):
        perps_note = " (yesterday's value: today's read failed)"
    elif not first and prior_perps is None:
        perps_note = " (first counted today, so not in the day change)"
    else:
        pos = (pf.get("perps") or {}).get("positions") or []
        perps_note = ("" if not pos else " (" + ", ".join(
            f"{p['ticker']} {p['position']:+g}, unrealized {p['unrealized']:+,.2f}"
            for p in pos[:3]) + (f", +{len(pos) - 3} more" if len(pos) > 3 else "") + ")")
    # Total profit, all time, under the account value (Jack 2026-10-04: "at
    # the top also show total profit, excluding deposits/withdrawals"). A
    # failed transfer read says so rather than dropping the line.
    profit = pf.get("total_profit")
    if profit is None:
        profit_txt = ("Total profit n/a today: Kalshi's deposit / withdrawal "
                      "history did not load")
        profit_basis = ""
    else:
        profit_txt = (f"Total profit {_signed_usd(profit)}"
                      + ("" if unpaid_total is None else
                         f"  (est. {_signed_usd(profit + unpaid_total)} after "
                         f"rewards are paid out)"))
        withdrawn = _f(pf.get("withdrawn"))
        profit_basis = (f"account value - ${_f(pf.get('deposited')):,.2f} deposited"
                        + (f" + ${withdrawn:,.2f} withdrawn" if withdrawn else "")
                        + ", all time")
    unpaid_note = ("" if unpaid_total is None else
                   f"Rewards earned, not yet paid (est.): ${unpaid_total:,.2f} = the "
                   f"IMM's modelled accrual in {unpaid['market_periods']} program "
                   f"periods still running or ended since midnight ET yesterday "
                   f"(Kalshi pays 0-3 days after a period ends), $1 floor per "
                   f"market per period, model ${unpaid['raw']:,.2f} x each family's "
                   f"paid/modelled ratio.")

    # Biggest movers, settled AND unrealized: every event row, each replayed
    # from its own fills and settlements since the prior morning
    # (replay_day). The day change splits exactly into this table's trading,
    # the cash that came in with no trade or settlement behind it (reward
    # credits; deposits apart when the transfer reads work), and Kalshi's
    # valuation of the open positions against the mids these rows use.
    movers, mv_tot, (mv_hidden_n, mv_hidden_net) = family_movers(pf["rows"])
    # the rows' mark basis (MARK_BASIS): on the last trades Kalshi uses, its
    # valuation and the rows differ only by a residual
    last_basis = pf.get("mark_basis") == "last"
    at = "last trade" if last_basis else "mid"
    trade_lbl = f"trading (at {at})"
    px_lbl = "valuation residual" if last_basis else "Kalshi's pricing vs mid"
    px_why = ("Kalshi's value of the positions vs ours, both at last trades: "
              "a market that never traded sits at its mid" if last_basis else
              "a gap between two valuations, no bot's")
    trading = mv_tot["day"]
    parts = None                        # [(label, $)] summing to d_ek exactly
    transfers = None if first else pf.get("net_transfers")
    ntc = None if first else pf.get("no_trade_cash")
    # The day's change in collateral Kalshi has returned early (+ = released,
    # - = taken back): cash with no fill or settlement behind it, and Kalshi's
    # valuation moves the other way by the same amount, so it is neither a
    # reward nor P&L and drops out of the split: the reward credits are the
    # no-trade cash less it, and Kalshi's pricing vs mid compares Kalshi's
    # last-trade prices with our mids without it (it held -collat_d before
    # 10/10). None = the prior morning did not record it.
    collat_d = None if first else pf.get("collateral_d", 0.0)
    rewards = None
    if not first:
        if ntc is None:                 # no replay (legacy prior snapshot)
            parts = [(trade_lbl, trading),
                     (f"credits, deposits & {px_lbl}",
                      round(d_ek - trading, 2))]
        else:
            rewards = round(ntc - (transfers or 0.0) - (moves or 0.0)
                            - (collat_d or 0.0), 2)
            parts = [(trade_lbl, trading),
                     ("reward credits" + ("" if transfers is not None else " & deposits")
                      + ("" if moves is not None else " & subaccount moves")
                      + ("" if collat_d is not None else " & Kalshi collateral"), rewards)]
            if transfers:
                parts.append(("deposits/withdrawals", transfers))
            if moves:
                parts.append(("subaccount transfers", moves))
            parts.append((px_lbl,
                          round(d_ek - trading - ntc + (collat_d or 0.0), 2)))

    # The day figure, in the subject and the body, leaves deposits and
    # withdrawals out (Jack 2026-10-04: "make the day figure exclude
    # deposits"): it is the day's change in total profit. Unread transfers
    # stay in it, and the subject says so. Cash moved to a subaccount is
    # still the account's: with the subaccounts in the day change it nets
    # out there; without them (their first morning) it is taken back out.
    d_day = None if first else round(
        d_equity - (transfers or 0.0) - ((moves or 0.0) if d_subs is None else 0.0), 2)
    day_parts = [(k, v) for k, v in parts or []
                 if k not in ("deposits/withdrawals", "subaccount transfers")]
    day_tail = (("" if d_perps is None else f"  +  perpetuals {d_perps:+,.2f}")
                + ("" if subs_pnl is None else f"  +  {subs_label} {subs_pnl:+,.2f}"))
    day_tail_html = (("" if d_perps is None else
                      f' &nbsp;+&nbsp; perpetuals {_pnl_span(d_perps)}')
                     + ("" if subs_pnl is None else
                        f' &nbsp;+&nbsp; {subs_label} {_pnl_span(subs_pnl)}'))
    excl = (([f"deposits/withdrawals {transfers:+,.2f}"] if transfers else [])
            + ([f"subaccount transfers {moves:+,.2f}"] if moves else []))
    excl_txt = f"  (excludes {', '.join(excl)})" if excl else ""
    # the day by bot: the rows by bot plus each bot's own lines; Kalshi's
    # pricing vs mid closes it to the day change
    by_bot = None
    if BY_BOT_ENABLED and rewards is not None and parts:
        by_bot = day_by_bot(pf, imm_events, parts[1], d_perps, subs_pnl)
    pricing = dict(parts or []).get(px_lbl)
    late = rewards is not None and abs(rewards) < 1.0

    # Jack 2026-10-04: "shorten like this: portfolio 2026-10-04: profit
    # +$18.2k (day +$1.6k, trading -$0.7K)", then "yes add rewards to the
    # subject": the split's reward credits, "portfolio 2026-10-05: profit
    # +$18.1k (day +$1.1k: trading -$0.8k, rewards +$2.4k)". Kalshi's
    # pricing vs mid and the perps are the rest of the day. With the
    # transfers unread, the day and the credits may hold a deposit.
    if first:
        tail = "first baseline"
    else:
        tail = (f"day {_signed_k(d_day)}"
                + ("" if transfers is not None else " incl. any deposits")
                + f": trading {_signed_k(trading)}")
        if rewards is not None:
            tail += (", rewards" + ("" if transfers is not None else " & deposits")
                     + ("" if moves is not None else " & subaccount moves")
                     + ("" if collat_d is not None else " & collateral")
                     + f" {_signed_k(rewards)}")
    subject = (f"portfolio {today}: profit "
               + ("n/a" if profit is None else _signed_k(profit)) + f" ({tail})")
    if imm and imm.get("subject_flag"):
        subject += imm["subject_flag"]

    # ---- plain text ---------------------------------------------------------
    lines = [f"Kalshi portfolio — {today} (7am ET)", ""]
    lines.append(f"Account value ${acct:,.2f}{after_txt}"
                 + ("" if app_value is None else
                    f"  ·  Kalshi app ${app_value:,.2f} (without the subaccounts)"))
    lines.append(f"  =  cash ${pf['cash']:,.2f}  +  open positions "
                 f"${pf['kalshi_positions_value']:,.2f}"
                 + ("" if perps_eq is None else
                    f"  +  perpetuals ${perps_eq:,.2f}{perps_note}")
                 + ("" if subs_eq is None else
                    f"  +  {subs_label} ${subs_eq:,.2f}{subs_note}"))
    lines.append(profit_txt)
    if profit_basis:
        lines.append(f"  =  {profit_basis}")
    if unpaid_note:
        lines.append(unpaid_note)
    if first:
        lines.append("First run: baseline saved; day-over-day starts tomorrow.")
    else:
        lines.append(f"vs yesterday: {d_day:+,.2f}  =  "
                     + "  +  ".join(f"{k} {v:+,.2f}" for k, v in day_parts)
                     + day_tail + excl_txt)
    if by_bot:
        lines.append("")
        lines.append(f"The day by bot (7am yesterday -> 7am today, at {at}):")
        for b, tot, ps in by_bot:
            lines.append(f"  {b:8s} {tot:>+11,.2f}  =  "
                         + "  +  ".join(f"{k} {v:+,.2f}" for k, v in ps)
                         + (f"  ({LATE_PAYOUT_NOTE})" if b == "IMM" and late else ""))
        if pricing is not None:
            lines.append(f"  {'Kalshi':8s} {pricing:>+11,.2f}  =  {px_lbl} ({px_why})")
        lines.append(f"  {'= day':8s} {d_day:>+11,.2f}")
    elif late:
        lines.append(f"({LATE_PAYOUT_NOTE})")
    lines.append("")
    lines += advice_text(advice)
    # the risk-controls block under the account summary, as in the html
    # (Jack 2026-10-03: "move the risk controls section right under the
    # chart at the top")
    if imm and imm.get("risk_text"):
        lines.append("")
        lines.append(imm["risk_text"])
    lines.append("")
    lines.append(f"Biggest movers since yesterday, by family / event, grouped "
                 f"as on the IMM dashboard (settled + unrealized at {at}; top {len(movers)} of "
                 f"{mv_tot['n_families']} families):")
    lines.append(f"{'FAMILY / EVENT':56s} {'DAY P&L':>9s} {'REALIZED':>9s} "
                 f"{'UNREALIZED':>10s} {'OPEN NOW':>9s}")
    for name, g in movers:
        n = len(g["rows"])
        lines.append(f"{(name + f' ({n} event' + ('s' if n != 1 else '') + ')')[:56]:56s} "
                     f"{g['day']:>+9.2f} {g['realized']:>+9.2f} "
                     f"{g['unreal']:>+10.2f} {g['value_now']:>9.2f}")
        for r in g["top"]:
            sub, tag = mover_group(r), _mover_tag(r)
            lbl = "  " + r["event"] + (f"  {sub}" if sub else "")
            lines.append(f"{lbl[:56]:56s} {r['day']:>+9.2f} {r['realized']:>+9.2f} "
                         f"{r['value_d']:>+10.2f} {r['value_now']:>9.2f}"
                         + (f"  {tag}" if tag else ""))
        more = n - len(g["top"])
        if more > 0:
            lines.append(f"  +{more} more event{'s' if more != 1 else ''}")
    if mv_hidden_n:
        lines.append(f"{'+' + str(mv_hidden_n) + ' more families':56s} "
                     f"{mv_hidden_net:>+9.2f}")
    lines.append(f"{'ALL FAMILIES':56s} {mv_tot['day']:>+9.2f} {mv_tot['realized']:>+9.2f} "
                 f"{mv_tot['unreal']:>+10.2f} {mv_tot['value_now']:>9.2f}"
                 f"  ({mv_tot['n_events']} events)")
    if not movers:
        lines.append("(nothing moved since the prior morning)")
    # the account-value bridge and the refund note under the table are gone
    # (Jack 2026-10-25: "Cut out"): the "vs yesterday" line carries the split
    if imm is not None:
        lines.append("")
        lines.append(imm.get("text") or
                     f"INCENTIVE MM: section unavailable ({imm.get('error', '?')})")
    # What to do's scorecard of every line, last: the long reference table
    # (Jack 2026-10-25: "How about mlb, red rocks, etc?")
    if advice and advice.get("lines"):
        lines.append("")
        lines += advice_lines_text(advice)
    text = "\n".join(lines)

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;'
         f'color:{C_INK};max-width:820px">']
    h.append(f'<div style="font-size:17px;font-weight:600">Kalshi portfolio'
             f' <span style="color:#888;font-weight:400">— {today} (7am ET)</span></div>')
    h.append(f'<div style="font-size:22px;font-weight:700;margin:8px 0 2px">'
             f'Account value ${acct:,.2f}'
             + ("" if unpaid_total is None else
                f' <span style="font-size:15px;font-weight:600;color:{C_INK2}">'
                f'(est. ${acct + unpaid_total:,.2f} after rewards are paid out)</span>')
             + ("" if app_value is None else
                f' <span style="font-size:15px;font-weight:600;color:{C_INK2}">'
                f'&nbsp;&middot;&nbsp; Kalshi app ${app_value:,.2f} '
                f'(without the subaccounts)</span>')
             + '</div>')
    if profit is None:
        h.append(f'<div style="font-size:15px;color:{C_INK2};margin:0 0 4px">'
                 f'{_esc(profit_txt)}</div>')
    else:
        p_col = C_POS if profit > 0.005 else (C_NEG if profit < -0.005 else C_INK)
        h.append(f'<div style="font-size:22px;font-weight:700;margin:0 0 4px">'
                 f'Total profit <span style="color:{p_col}">{_signed_usd(profit)}</span>'
                 + ("" if unpaid_total is None else
                    f' <span style="font-size:15px;font-weight:600;color:{C_INK2}">'
                    f'(est. {_signed_usd(profit + unpaid_total)} after rewards are '
                    f'paid out)</span>')
                 + '</div>')
    h.append(f'<div style="color:{C_INK2};margin-bottom:6px">'
             f'cash <b>${pf["cash"]:,.2f}</b> &nbsp;&middot;&nbsp; '
             f'open positions <b>${pf["kalshi_positions_value"]:,.2f}</b>'
             + ("" if perps_eq is None else
                f' &nbsp;&middot;&nbsp; perpetuals <b>${perps_eq:,.2f}</b>'
                f'<span style="color:{C_MUTED}">{perps_note}</span>')
             + ("" if subs_eq is None else
                f' &nbsp;&middot;&nbsp; {subs_label} <b>${subs_eq:,.2f}</b>'
                f'<span style="color:{C_MUTED}">{subs_note}</span>')
             + ("" if unpaid_total is None else
                f' &nbsp;&middot;&nbsp; rewards earned, not yet paid '
                f'<b>&asymp; ${unpaid_total:,.2f}</b>')
             + ("" if not profit_basis else
                f'<br>total profit = {_esc(profit_basis).replace(" - ", " &minus; ")}')
             + ("" if first else
                f'<br>day change {_pnl_span(d_day)} = '
                + ' &nbsp;+&nbsp; '.join(f'{k.replace("&", "&amp;")} {_pnl_span(v)}'
                                         for k, v in day_parts)
                + day_tail_html
                + ("" if not excl else
                   f' <span style="color:{C_MUTED}">{excl_txt.strip()}</span>'))
             + '</div>')
    if by_bot:
        h.append(f'<div style="font-size:15px;font-weight:600;margin:10px 0 4px">'
                 f'The day by bot <span style="color:{C_MUTED};font-weight:400;'
                 f'font-size:13px">7am yesterday &rarr; 7am today, at {at}</span></div>')
        h.append('<table style="border-collapse:collapse;font-size:13px;margin-bottom:6px">')
        for b, tot, ps in by_bot:
            made = ' &nbsp;+&nbsp; '.join(f'{_esc(k)} {_pnl_span(v)}' for k, v in ps)
            if b == "IMM" and late:
                made += f' <span style="color:{C_MUTED}">({_esc(LATE_PAYOUT_NOTE)})</span>'
            h.append(f'<tr><td style="{TDL}font-weight:700">{b}</td>'
                     f'<td style="{TD}font-weight:700">{_pnl_span(tot)}</td>'
                     f'<td style="{TDL}color:{C_INK2};font-size:12px">{made}</td></tr>')
        if pricing is not None:
            h.append(f'<tr style="color:{C_MUTED}"><td style="{TDL}">Kalshi</td>'
                     f'<td style="{TD}">{_pnl_span(pricing)}</td>'
                     f'<td style="{TDL}font-size:12px">{_esc(px_lbl)} '
                     f'({_esc(px_why)})</td></tr>')
        h.append(f'<tr style="background:#f0f0f0;font-weight:700"><td style="{TDL}">Day</td>'
                 f'<td style="{TD}">{_pnl_span(d_day)}</td><td style="{TDL}"></td></tr>')
        h.append('</table>')
    elif late:
        h.append(f'<div style="color:{C_MUTED};font-size:12px;margin-bottom:6px">'
                 f'({_esc(LATE_PAYOUT_NOTE)})</div>')
    if unpaid_note:
        h.append(f'<div style="color:{C_MUTED};font-size:12px;margin-bottom:6px">'
                 f'{unpaid_note}</div>')
    if first:
        h.append(f'<div style="color:{C_INK2};margin-bottom:6px">First run — '
                 f'baseline saved; day-over-day starts tomorrow.</div>')
    h.append(advice_html(advice))
    if chart_ok:
        h.append('<div style="margin:10px 0"><img src="cid:balancechart" '
                 'alt="Daily profit, realized and estimated after rewards" width="760" '
                 'style="width:100%;max-width:760px;height:auto"></div>')

    # the risk-controls block right under the chart (Jack 2026-10-03: "move
    # the risk controls section right under the chart at the top"); the IMM
    # section builds it and hands it over on its own
    if imm and imm.get("risk_html"):
        h.append(imm["risk_html"])

    h.append(f'<div style="font-size:15px;font-weight:600;margin:16px 0 4px">'
             f'Biggest movers since yesterday, by family / event'
             f' <span style="color:{C_MUTED};font-weight:400;font-size:13px">'
             f'grouped as on the IMM dashboard &middot; settled + unrealized at {at} '
             f'&middot; top {len(movers)} of {mv_tot["n_families"]} families</span></div>')
    if movers:
        mono = "font-family:Consolas,Menlo,monospace;font-size:12px"
        h.append('<table style="border-collapse:collapse;font-size:13px">')
        h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
                 f'<td style="{TDL}">Family / event</td>'
                 f'<td style="{TD}">Day P&amp;L $</td>'
                 f'<td style="{TD}">Realized $</td><td style="{TD}">Unrealized $</td>'
                 f'<td style="{TD}">Open now $</td><td style="{TDL}">Why</td></tr>')
        for name, g in movers:
            n = len(g["rows"])
            h.append(f'<tr style="background:#f6f6f4">'
                     f'<td style="{TDL}font-weight:700">{_esc(name)} '
                     f'<span style="color:{C_MUTED};font-weight:400;font-size:11px">'
                     f'{n} event{"s" if n != 1 else ""}</span></td>'
                     f'<td style="{TD}font-weight:700">{_pnl_span(g["day"])}</td>'
                     f'<td style="{TD}">{_pnl_span(g["realized"])}</td>'
                     f'<td style="{TD}">{_pnl_span(g["unreal"])}</td>'
                     f'<td style="{TD}">{g["value_now"]:,.2f}</td>'
                     f'<td style="{TDL}"></td></tr>')
            for r in g["top"]:
                sub = mover_group(r)
                lab = f'<span style="{mono}">{_esc(r["event"])}</span>'
                if sub:
                    lab += f' <span style="color:{C_MUTED};font-size:11px">{_esc(sub)}</span>'
                h.append(f'<tr>'
                         f'<td style="{TDL}padding-left:22px;">{lab}</td>'
                         f'<td style="{TD}">{_pnl_span(r["day"])}</td>'
                         f'<td style="{TD}">{_pnl_span(r["realized"])}</td>'
                         f'<td style="{TD}">{_pnl_span(r["value_d"])}</td>'
                         f'<td style="{TD}">{r["value_now"]:,.2f}</td>'
                         f'<td style="{TDL}color:{C_MUTED};font-size:12px">'
                         f'{_esc(_mover_tag(r))}</td></tr>')
            more = n - len(g["top"])
            if more > 0:
                h.append(f'<tr><td style="{TDL}padding-left:22px;color:{C_MUTED};font-size:11px" '
                         f'colspan="6">+{more} more event{"s" if more != 1 else ""}</td></tr>')
        if mv_hidden_n:
            h.append(f'<tr style="color:{C_MUTED}">'
                     f'<td style="{TDL}">+{mv_hidden_n} more families</td>'
                     f'<td style="{TD}">{_pnl_span(mv_hidden_net)}</td>'
                     f'<td style="{TD}" colspan="4"></td></tr>')
        h.append(f'<tr style="background:#f0f0f0;font-weight:700">'
                 f'<td style="{TDL}">ALL FAMILIES</td>'
                 f'<td style="{TD}">{_pnl_span(mv_tot["day"])}</td>'
                 f'<td style="{TD}">{_pnl_span(mv_tot["realized"])}</td>'
                 f'<td style="{TD}">{_pnl_span(mv_tot["unreal"])}</td>'
                 f'<td style="{TD}">{mv_tot["value_now"]:,.2f}</td>'
                 f'<td style="{TDL}font-weight:400;color:{C_INK2}">'
                 f'{mv_tot["n_events"]} events</td></tr>')
        h.append('</table>')
    else:
        h.append(f'<div style="color:{C_INK2}">Nothing moved since the prior '
                 f'morning.</div>')

    if imm is not None:
        if imm.get("html"):
            h.append(imm["html"])
        else:
            why = str(imm.get("error", "?")).replace("&", "&amp;").replace("<", "&lt;")
            h.append(f'<div style="border-top:2px solid #ddd;margin-top:22px;'
                     f'padding-top:12px;color:{C_NEG};font-weight:600">'
                     f'Incentive MM section unavailable: {why}</div>')
    h.append(advice_lines_html(advice))
    h.append('</div>')
    return subject, text, "".join(h)


def send_email(subject: str, text: str, html: str, chart_png) -> bool:
    frm = os.environ.get("ALERT_EMAIL_FROM", "")
    pw = os.environ.get("ALERT_EMAIL_PASSWORD", "")
    if not frm or not pw:
        log("cannot send: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not configured")
        return False
    root = MIMEMultipart("related")
    root["Subject"] = subject
    root["From"] = frm
    root["To"] = ", ".join(RECIPIENTS)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text))
    alt.attach(MIMEText(html, "html"))
    root.attach(alt)
    if chart_png and os.path.exists(chart_png):
        with open(chart_png, "rb") as f:
            img = MIMEImage(f.read(), _subtype="png")
        img.add_header("Content-ID", "<balancechart>")
        img.add_header("Content-Disposition", "inline",
                       filename=os.path.basename(chart_png))
        root.attach(img)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(frm, pw)
            server.sendmail(frm, RECIPIENTS, root.as_string())
        log(f"sent to {', '.join(RECIPIENTS)}")
        return True
    except Exception as e:
        log(f"! send failed: {e}")
        return False


# The IMM bot's section (Jack 2026-10-02: "cut it as a standalone email and
# add it into the Kalshi portfolio ... email"). send_imm_digest.py builds it
# in a child process: importing it mirrors the IMM launcher's env into
# os.environ, which must not leak into this one. Kill: PF_IMM_SECTION=0.
IMM_SECTION_ENABLED = os.environ.get("PF_IMM_SECTION", "1") != "0"
IMM_SECTION_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "send_imm_digest.py")
IMM_SECTION_TIMEOUT_S = int(os.environ.get("PF_IMM_SECTION_TIMEOUT", "900"))
IMM_SECTION_LOG = os.path.join(LOG_DIR, "imm-section.log")


def imm_section(today_str: str, script: str = IMM_SECTION_SCRIPT,
                timeout: int = IMM_SECTION_TIMEOUT_S) -> dict:
    """{"text", "html", "subject_flag"} from `send_imm_digest.py
    --section-out`, or {"error": why}: the portfolio email goes out either
    way, saying the section is missing. The child's output goes to
    imm-section.log (UTF-8; the task log's console encoding is not)."""
    out = os.path.join(LOG_DIR, f"imm_section_{today_str}.json")
    t0 = time.time()
    try:
        if os.path.exists(out):
            os.remove(out)
        with open(IMM_SECTION_LOG, "ab") as lf:
            rc = subprocess.run(
                [sys.executable, script, "--section-out", out],
                stdout=lf, stderr=subprocess.STDOUT, timeout=timeout,
                cwd=os.path.dirname(os.path.abspath(script)),
                env=dict(os.environ, PYTHONIOENCODING="utf-8")).returncode
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s"}
    except OSError as e:
        return {"error": f"could not run it: {e}"}
    if rc != 0:
        return {"error": f"exit {rc}, see {IMM_SECTION_LOG}"}
    try:
        with open(out, encoding="utf-8") as f:
            sec = json.load(f)
    except (OSError, ValueError) as e:
        return {"error": f"unreadable output: {e}"}
    if not sec.get("text") or not sec.get("html"):
        return {"error": "empty section"}
    log(f"IMM section built in {time.time() - t0:.0f}s")
    return sec


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="send now; no sent-marker, no snapshot/history writes")
    ap.add_argument("--dry-run", action="store_true",
                    help="build + write the HTML/PNG to run-logs, send nothing, "
                         "write no snapshot")
    ap.add_argument("--no-imm", action="store_true",
                    help="leave out the IMM bot's section (as PF_IMM_SECTION=0)")
    args = ap.parse_args(argv)

    os.makedirs(LOG_DIR, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    today_str = str(now_utc.astimezone(ET).date())
    marker = os.path.join(LOG_DIR, f"digest_sent_{today_str}.marker")

    if not (args.test or args.dry_run) and os.path.exists(marker):
        log(f"portfolio digest already sent for {today_str}; exiting")
        return 0

    # Build (with the Modern-Standby retry loop: the 6am CT trigger can fire
    # while the laptop is asleep with the radio off).
    pf = None
    attempts = 1 if (args.test or args.dry_run) else 8
    for attempt in range(1, attempts + 1):
        try:
            pf = build_portfolio(now_utc)
            break
        except Exception as e:
            log(f"build attempt {attempt}/{attempts} failed: {e!r}")
            if attempt == attempts:
                log("giving up for today")
                return 1
            time.sleep(300)

    # Persist state at build time (real runs only): a failed send must still
    # baseline tomorrow's diff.
    history = load_history()
    history_preview = upsert_history(history, today_str, pf["cash"],
                                     pf["positions_value"], pf["equity"],
                                     pf["kalshi_positions_value"],
                                     pf.get("perps_equity"),
                                     (pf.get("unpaid") or {}).get("total"),
                                     pf.get("subs_equity"),
                                     pf.get("deposited"), pf.get("withdrawn"))
    if not (args.test or args.dry_run):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(snapshot_path(today_str), "w", encoding="utf-8") as f:
            json.dump(pf["snapshot"], f, indent=1)
        write_history(history_preview)
        log(f"snapshot + history written for {today_str}")

    chart_png = os.path.join(LOG_DIR, f"chart_{today_str}.png")
    chart_ok = render_chart(history_preview, pf.get("transfers"), chart_png,
                            today_str, pf.get("total_profit"))

    imm = None
    if IMM_SECTION_ENABLED and not args.no_imm:
        imm = imm_section(today_str)
        if imm.get("error"):
            log(f"! IMM section unavailable: {imm['error']}")

    # the IMM dashboard, read once: the What-to-do block and the IMM's book
    # for the day by bot
    dash, dash_err = None, None
    if ADVICE_ENABLED or BY_BOT_ENABLED:
        try:
            dash = load_imm_dashboard()
        except Exception as e:                    # never holds the email
            dash_err = e
            log(f"! IMM dashboard unavailable: {e!r}")
    advice = None
    if ADVICE_ENABLED:
        try:
            if dash_err is not None:
                raise dash_err
            # each series' size: the day it last went up (real runs keep it)
            sizes = size_history(dash.get("sizes") or {}, load_size_history(), today_str)
            if dash.get("sizes") and not (args.test or args.dry_run):
                try:
                    write_size_history(sizes)
                except OSError as e:              # tomorrow reads today's sizes again
                    log(f"! series sizes not saved: {e!r}")
            advice = imm_advice(dash, today_str, sizes=sizes)
        except Exception as e:                    # never holds the email
            advice = {"error": f"{type(e).__name__}: {e}"}
            log(f"! what-to-do block unavailable: {e!r}")
    imm_events = None
    if BY_BOT_ENABLED and dash is not None:
        try:
            imm_events = imm_book_events(dash, today_str)
        except Exception as e:                    # never holds the email
            log(f"! day by bot unavailable: {e!r}")

    subject, text, html = build_email(pf, history_preview, chart_ok, imm, advice=advice,
                                      imm_events=imm_events)
    if args.test:
        subject = "[TEST] " + subject
    html_path = os.path.join(LOG_DIR, f"digest_{today_str}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"digest built: {len(pf['rows'])} event rows; html -> {html_path}")

    if args.dry_run:
        log("dry run: not sending")
        print(text)
        return 0

    ok = False
    for attempt in range(1, attempts + 1):
        ok = send_email(subject, text, html, chart_png if chart_ok else None)
        if ok:
            break
        if attempt < attempts:
            log(f"send attempt {attempt}/{attempts} failed; retrying in 5min")
            time.sleep(300)
    if ok and not args.test:
        with open(marker, "w") as f:
            f.write(now_utc.isoformat())
        cutoff = now_utc.astimezone(ET).date() - timedelta(days=7)
        for old in glob.glob(os.path.join(LOG_DIR, "digest_sent_*.marker")):
            m = re.search(r"(\d{4}-\d{2}-\d{2})\.marker$", old)
            try:
                if m and datetime.strptime(m.group(1), "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
        for old in glob.glob(os.path.join(LOG_DIR, "chart_*.png")) + \
                glob.glob(os.path.join(LOG_DIR, "digest_*.html")) + \
                glob.glob(os.path.join(LOG_DIR, "imm_section_*.json")):
            m = re.search(r"(\d{4}-\d{2}-\d{2})\.(?:png|html|json)$", old)
            try:
                if m and (datetime.strptime(m.group(1), "%Y-%m-%d").date()
                          < cutoff - timedelta(days=7)):
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
