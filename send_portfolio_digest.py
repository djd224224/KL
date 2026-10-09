#!/usr/bin/env python3
r"""
send_portfolio_digest.py — 7:00 AM ET whole-account portfolio email.

One email covering the ENTIRE Kalshi account (every bot + manual trades):
a chart of daily account value (cash + open positions marked to mid), the
IMM's risk-controls table right under it (since 2026-10-03), then the
biggest movers since the prior morning by family / event / market, grouped
exactly as the IMM dashboard's Drivers table groups them (imm_dashboard.
family_of, since 2026-10-03; settled and unrealized alike, ranked by the
size of the move, realized and mark-to-mid split out).
(The "Settled since yesterday, by series" table was removed 2026-09-29.)

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
the perpetual-futures account's equity (the margin API, Jack 2026-09-28).
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
profit. The subject carries both, with the day's trading and reward
credits: "portfolio 2026-10-05: profit +$18.1k (day +$1.1k: trading
-$0.8k, rewards +$2.4k)".

State lives in portfolio_daily\:
    pf_snapshot_YYYY-MM-DD.json  - per-event E components (diff baseline)
    balance_history.csv          - date,cash,positions_value,equity (chart)
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
import subprocess
import sys
import time
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
C_EQUITY = "#2a78d6"     # series 1 blue — total account value
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
    nets it out ($2,819.57 returned vs Kalshi $2.8k under our mids, 10/9), so
    a release is no gain: before 10/10 it read as reward credits (Jack:
    "the reward credit is wrong in the email")."""
    return round(sum(r["cost"] for r in mkt_pos.values())
                 - sum(e.get("exposure", 0.0) for e in events.values()), 2)


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
    into the primary)]}, None when a read fails."""
    try:
        bals = client.get("/portfolio/subaccounts/balances").get("subaccount_balances") or []
        moves = []
        for t in client.get("/portfolio/subaccounts/transfers").get("transfers") or []:
            amt = _f(t.get("amount_cents")) / 100.0
            src, dst = int(t.get("from_subaccount") or 0), int(t.get("to_subaccount") or 0)
            if (src == 0) != (dst == 0):          # numbered <-> numbered nets to 0
                moves.append((_f(t.get("created_ts")), amt if dst == 0 else -amt))
        accounts = {}
        for n in sorted({int(b.get("subaccount_number") or 0) for b in bals} - {0}):
            b = client.get("/portfolio/balance", params={"subaccount": n})
            accounts[n] = round(_f(b.get("balance_dollars"))
                                + _f(b.get("portfolio_value")) / 100.0, 2)
    except Exception as e:
        log(f"! subaccount read failed: {e!r}")
        return None
    return {"equity": round(sum(accounts.values()), 2), "accounts": accounts,
            "moves": moves}


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
    {"realized", "value_d", "markets"}, markets mapping each ticker to its
    own {"realized", "value_d", "value_now", "settled"}; cash = the day's trading + settlement cash;
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
        e = events.setdefault(ev_of(tk), {"realized": 0.0, "value_d": 0.0,
                                          "markets": {}})
        e["realized"] += d["real"]
        e["value_d"] += v_end - d["basis"]
        # the market level of the family / event / market table
        e["markets"][tk] = {"realized": d["real"], "value_d": v_end - d["basis"],
                            "value_now": v_end, "settled": d.get("settled")}
    return events, round(cash, 2), mismatches, settled


def market_rows(event: str, markets: dict) -> list:
    """An event's markets as the movers table's third level (the dashboard's
    family / event / market): each market's own day = realized + value_d,
    its value now and a settled note, largest move first. Markets that did
    not move are left out."""
    out = []
    for tk, m in markets.items():
        realized, value_d = round(m["realized"], 2), round(m["value_d"], 2)
        day = round(realized + value_d, 2)
        if abs(day) < 0.005 and abs(realized) < 0.005 and abs(value_d) < 0.005:
            continue
        out.append({"ticker": tk, "day": day, "realized": realized,
                    "value_d": value_d, "value_now": round(m["value_now"], 2),
                    "note": f"settled {m['settled']}" if m.get("settled") else ""})
    out.sort(key=lambda r: -abs(r["day"]))
    return out


def market_label(event: str, ticker: str) -> str:
    """A market as the dashboard prints it under its event: the strike
    suffix (KXCPIYOY-26NOV-T3.6 -> T3.6)."""
    return ticker[len(event) + 1:] if ticker.startswith(event + "-") else ticker


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
                "subs_equity"]


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
                          "subs_equity"):                          # 10/9 on
                    try:
                        row[k] = float(r.get(k) or "")
                    except ValueError:
                        row[k] = ""
                rows.append(row)
    rows.sort(key=lambda r: r["date"])
    return rows


def upsert_history(rows, today_str, cash, pos_value, equity, kalshi_pv,
                   perps_equity=None, unpaid=None, subs_equity=None):
    rows = [r for r in rows if r["date"] != today_str]
    rows.append({"date": today_str, "cash": round(cash, 2),
                 "positions_value": round(pos_value, 2),
                 "equity": round(equity, 2),
                 "kalshi_positions_value": round(kalshi_pv, 2),
                 "perps_equity": "" if perps_equity is None else round(perps_equity, 2),
                 "unpaid_rewards_est": "" if unpaid is None else round(unpaid, 2),
                 "subs_equity": "" if subs_equity is None else round(subs_equity, 2)})
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

    # Group open value + cost basis by event.
    ev_value, ev_basis, ev_tickers = {}, {}, {}
    tk_end, tk_event = {}, {}          # per market, for the day replay
    for tk, rec in mkt_pos.items():
        pos, cost = rec["pos"], rec["cost"]
        ev = (mark_info.get(tk) or {}).get("event") or event_from_ticker(tk)
        mark = marks.get(tk)
        if mark is None:
            val = cost                     # marked at cost: unrealized 0
        else:
            val = pos * mark if pos > 0 else -pos * (1.0 - mark)
        tk_end[tk] = (pos, val)
        tk_event[tk] = ev
        ev_value[ev] = ev_value.get(ev, 0.0) + val
        ev_basis[ev] = ev_basis.get(ev, 0.0) + cost
        ev_tickers.setdefault(ev, {})[tk] = {
            "pos": pos, "cost": round(cost, 2),
            "mark": None if mark is None else round(mark, 4)}

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
    sub_moves = None
    prior_tickers = (prior or {}).get("tickers")
    if prior and prior_created is not None and prior_tickers:
        ev_map, start = {}, {}
        for ev, tks in prior_tickers.items():
            for tk, r in tks.items():
                q = _f(r.get("pos"))
                start[tk] = (q, side_value(q, r.get("mark"), _f(r.get("cost"))))
                ev_map[tk] = ev
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
        for ev, e in evs.items():
            realized, value_d = round(e["realized"], 2), round(e["value_d"], 2)
            day = round(realized + value_d, 2)
            if abs(day) < 0.005 and abs(realized) < 0.005 and abs(value_d) < 0.005:
                continue
            value_now = round(ev_value.get(ev, 0.0), 2)
            if ev in settled_now:
                note = "settled " + "/".join(sorted(settled_now[ev]))
            elif ev not in prior_names:
                note = "new"
            elif abs(value_now) < 0.005 and abs(prior_val.get(ev, 0.0)) >= 0.005:
                note = "closed"
            else:
                note = ""
            rows.append({"event": ev, "day": day, "realized": realized,
                         "value_d": value_d, "value_now": value_now, "note": note,
                         "markets": market_rows(ev, e.get("markets") or {})})
        no_trade_cash = round(cash - _f(prior.get("cash")) - trade_cash, 2)
        if transfers is not None:
            dep, wd = sum_transfers(transfers, prior_created, now_utc)
            net_transfers = round(dep - wd, 2)
        if subs is not None:
            sub_moves = round(sum(a for ts, a in subs["moves"]
                                  if prior_created.timestamp() < ts <= now_utc.timestamp()), 2)
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

    log(f"marks: {mark_src} | Kalshi values positions ${kalshi_pv:,.2f} "
        f"vs our mids ${positions_value:,.2f}"
        + (f" | unreadable: {', '.join(unreadable)}" if unreadable else ""))
    return {"today": today_str, "cash": round(cash, 2),
            "positions_value": positions_value, "equity": equity,
            "kalshi_positions_value": round(kalshi_pv, 2),
            "equity_kalshi": equity_kalshi,
            "perps_equity": perps_equity, "perps": perps,
            "perps_stale": perps_stale, "subs_equity": subs_equity,
            "subs": subs, "subs_stale": subs_stale, "sub_moves": sub_moves,
            "collateral_returned": collat,
            "collateral_d": (None if (prior or {}).get("collateral_returned") is None
                             else round(collat - _f(prior["collateral_returned"]), 2)),
            "account_value": account_value,
            "unpaid": unpaid, "deposited": deposited, "withdrawn": withdrawn,
            "total_profit": total_profit,
            "rows": rows, "snapshot": snapshot, "prior": prior,
            "mark_src": mark_src, "unreadable": unreadable,
            "n_settlements": len(settlements),
            "no_trade_cash": no_trade_cash, "net_transfers": net_transfers,
            "replay": replay_info,
            "first_run": prior is None}


# ----------------------------------------------------------------------------
# Chart
# ----------------------------------------------------------------------------

def render_chart(history, out_png: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except Exception as e:
        log(f"! matplotlib unavailable ({e}); sending without chart")
        return False
    hist = history[-120:]
    dates = [datetime.strptime(r["date"], "%Y-%m-%d").date() for r in hist]
    # Account value on Kalshi's own positions valuation (Jack 8/14: "value it
    # based on Kalshi"); rows predating that column fall back to our marks.
    # Perpetuals are in from 9/28, numbered subaccounts from 10/9 (earlier
    # rows have none recorded).
    eq = [(r["cash"] + r["kalshi_positions_value"]
           if isinstance(r.get("kalshi_positions_value"), float) else r["equity"])
          + sum(r[k] for k in ("perps_equity", "subs_equity")
                if isinstance(r.get(k), float))
          for r in hist]

    fig, ax = plt.subplots(figsize=(7.6, 3.1), dpi=180)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")
    ax.plot(dates, eq, color=C_EQUITY, lw=2.2, marker="o", ms=4.5, zorder=3)
    ax.annotate(f"${eq[-1]:,.0f}", (dates[-1], eq[-1]), xytext=(7, 6),
                textcoords="offset points", color=C_INK, fontsize=9,
                fontweight="bold")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
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
    pad = max((hi - lo) * 0.12, 1.0)
    ax.set_ylim(lo - pad * 0.3, hi + pad)          # room for the end label
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


MARKETS_PER_EVENT = 2


def mover_lines(r) -> list:
    """[(level, label, sub, numbers, tag)] for one event row of the movers
    table and, under it, its markets: the dashboard's family / event /
    market, levels 1 and 2. `sub` is the dashboard's group label beside an
    event (CPI & inflation, Treasury yields, ...; none where the group is
    just the series). An event whose moves came from two or more markets
    lists its top MARKETS_PER_EVENT; a single moving market's strike rides
    on the event line rather than repeating its numbers. A trailing
    (2, "+N more markets", "", None, "") line counts the rest."""
    _fam, grp = family_and_group(r["event"])
    series = r["event"].split("-", 1)[0]
    sub = grp if grp and grp != series else ""
    mk = r.get("markets") or []
    if len(mk) == 1:
        sub = (sub + " · " if sub else "") + market_label(r["event"], mk[0]["ticker"])
    out = [(1, r["event"], sub, r, _mover_tag(r))]
    if len(mk) >= 2:
        for m in mk[:MARKETS_PER_EVENT]:
            out.append((2, market_label(r["event"], m["ticker"]), "", m, m.get("note") or ""))
        rest = len(mk) - MARKETS_PER_EVENT
        if rest > 0:
            out.append((2, f"+{rest} more market{'s' if rest != 1 else ''}", "", None, ""))
    return out


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


def build_email(pf, history, chart_ok: bool, imm=None):
    """(subject, text, html). `imm` is imm_section()'s result: the IMM bot's
    section goes after the movers table, and its PICK-OFF flag onto the
    subject; None leaves the email as it was before 2026-10-02."""
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
    if subs_eq is None:
        subs_note = ""
    elif pf.get("subs_stale"):
        subs_note = " (yesterday's value: today's read failed)"
    elif not first and prior_subs is None:
        subs_note = " (first counted today, so not in the day change)"
    else:
        subs_note = ""
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
    ex_what = [w for w, v in (("perpetuals", perps_eq), ("subaccounts", subs_eq))
               if v is not None]
    ex_perps = " ex-" + " & ".join(ex_what) if ex_what else ""
    trading = mv_tot["day"]
    parts = None                        # [(label, $)] summing to d_ek exactly
    transfers = None if first else pf.get("net_transfers")
    ntc = None if first else pf.get("no_trade_cash")
    # The day's change in collateral Kalshi has returned early (+ = released,
    # - = taken back): cash with no fill or settlement behind it, which
    # Kalshi's valuation offsets, so it leaves the reward credits for
    # Kalshi's pricing vs mid. None = the prior morning did not record it.
    collat_d = None if first else pf.get("collateral_d", 0.0)
    rewards = None
    if not first:
        if ntc is None:                 # no replay (legacy prior snapshot)
            parts = [("trading (at mid)", trading),
                     ("credits, deposits & Kalshi's pricing vs mid",
                      round(d_ek - trading, 2))]
        else:
            rewards = round(ntc - (transfers or 0.0) - (moves or 0.0)
                            - (collat_d or 0.0), 2)
            parts = [("trading (at mid)", trading),
                     ("reward credits" + ("" if transfers is not None else " & deposits")
                      + ("" if moves is not None else " & subaccount moves")
                      + ("" if collat_d is not None else " & Kalshi collateral"), rewards)]
            if transfers:
                parts.append(("deposits/withdrawals", transfers))
            if moves:
                parts.append(("subaccount transfers", moves))
            parts.append(("Kalshi's pricing vs mid",
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
    lines.append(f"Account value ${acct:,.2f}{after_txt}")
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
    lines.append("")
    # the risk-controls block under the account summary, as in the html
    # (Jack 2026-10-03: "move the risk controls section right under the
    # chart at the top")
    if imm and imm.get("risk_text"):
        lines.append("")
        lines.append(imm["risk_text"])
    lines.append("")
    lines.append(f"Biggest movers since yesterday, by family / event / market, grouped "
                 f"as on the IMM dashboard (settled + unrealized; top {len(movers)} of "
                 f"{mv_tot['n_families']} families):")
    lines.append(f"{'FAMILY / EVENT / MARKET':56s} {'DAY P&L':>9s} {'REALIZED':>9s} "
                 f"{'UNREALIZED':>10s} {'OPEN NOW':>9s}")
    for name, g in movers:
        n = len(g["rows"])
        lines.append(f"{(name + f' ({n} event' + ('s' if n != 1 else '') + ')')[:56]:56s} "
                     f"{g['day']:>+9.2f} {g['realized']:>+9.2f} "
                     f"{g['unreal']:>+10.2f} {g['value_now']:>9.2f}")
        for r in g["top"]:
            for lvl, lbl, sub, x, tag in mover_lines(r):
                lbl = "  " * lvl + lbl + (f"  {sub}" if sub else "")
                if x is None:
                    lines.append(lbl)
                    continue
                lines.append(f"{lbl[:56]:56s} {x['day']:>+9.2f} {x['realized']:>+9.2f} "
                             f"{x['value_d']:>+10.2f} {x['value_now']:>9.2f}"
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
    if parts is not None:
        lines.append(f"(account value{ex_perps} moved {d_ek:+,.2f} = this table "
                     f"{trading:+,.2f}"
                     + "".join(f"  +  {k} {v:+,.2f}" for k, v in parts[1:]) + ")")
        if collat_d:
            lines.append(f"(Kalshi's pricing vs mid includes {collat_d:+,.2f} of collateral "
                         f"Kalshi {'released' if collat_d > 0 else 'took back'} on mutually "
                         f"exclusive positions: cash its valuation offsets, not rewards)")
    if imm is not None:
        lines.append("")
        lines.append(imm.get("text") or
                     f"INCENTIVE MM: section unavailable ({imm.get('error', '?')})")
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
    if unpaid_note:
        h.append(f'<div style="color:{C_MUTED};font-size:12px;margin-bottom:6px">'
                 f'{unpaid_note}</div>')
    if first:
        h.append(f'<div style="color:{C_INK2};margin-bottom:6px">First run — '
                 f'baseline saved; day-over-day starts tomorrow.</div>')
    if chart_ok:
        h.append('<div style="margin:10px 0"><img src="cid:balancechart" '
                 'alt="Daily account balance" width="760" '
                 'style="width:100%;max-width:760px;height:auto"></div>')

    # the risk-controls block right under the chart (Jack 2026-10-03: "move
    # the risk controls section right under the chart at the top"); the IMM
    # section builds it and hands it over on its own
    if imm and imm.get("risk_html"):
        h.append(imm["risk_html"])

    h.append(f'<div style="font-size:15px;font-weight:600;margin:16px 0 4px">'
             f'Biggest movers since yesterday, by family / event / market'
             f' <span style="color:{C_MUTED};font-weight:400;font-size:13px">'
             f'grouped as on the IMM dashboard &middot; settled + unrealized '
             f'&middot; top {len(movers)} of {mv_tot["n_families"]} families</span></div>')
    if movers:
        mono = "font-family:Consolas,Menlo,monospace;font-size:12px"
        h.append('<table style="border-collapse:collapse;font-size:13px">')
        h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
                 f'<td style="{TDL}">Family / event / market</td>'
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
                for lvl, lbl, sub, x, tag in mover_lines(r):
                    pad = "padding-left:22px;" if lvl == 1 else "padding-left:42px;"
                    lab = (f'<span style="{mono}">{_esc(lbl)}</span>' if x is not None
                           else f'<span style="color:{C_MUTED};font-size:11px">{_esc(lbl)}</span>')
                    if sub:
                        lab += f' <span style="color:{C_MUTED};font-size:11px">{_esc(sub)}</span>'
                    if x is None:
                        h.append(f'<tr><td style="{TDL}{pad}" colspan="6">{lab}</td></tr>')
                        continue
                    h.append(f'<tr>'
                             f'<td style="{TDL}{pad}">{lab}</td>'
                             f'<td style="{TD}">{_pnl_span(x["day"])}</td>'
                             f'<td style="{TD}">{_pnl_span(x["realized"])}</td>'
                             f'<td style="{TD}">{_pnl_span(x["value_d"])}</td>'
                             f'<td style="{TD}">{x["value_now"]:,.2f}</td>'
                             f'<td style="{TDL}color:{C_MUTED};font-size:12px">{_esc(tag)}</td></tr>')
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
        if parts is not None:
            h.append(f'<div style="color:{C_MUTED};font-size:12px;margin:4px 0 0">'
                     f'Account value{ex_perps.replace("&", "&amp;")} moved '
                     f'{_pnl_span(d_ek)} = this table '
                     f'{_pnl_span(trading)}'
                     + "".join(f' &nbsp;+&nbsp; {k.replace("&", "&amp;")} {_pnl_span(v)}'
                               for k, v in parts[1:])
                     + '. Reward credits = cash that came in with no trade or '
                       'settlement behind it'
                     + ("" if not collat_d else
                        f', less {_pnl_span(collat_d)} of collateral Kalshi '
                        f'{"released" if collat_d > 0 else "took back"} on mutually '
                        f'exclusive positions (in Kalshi\'s pricing vs mid, which '
                        f'offsets it)')
                     + ("" if collat_d is not None else
                        ' (it still holds collateral Kalshi released or took back on '
                        'mutually exclusive positions: the prior morning did not '
                        'record it)')
                     + '; Kalshi values open positions near what they would sell '
                       'for, this table at the mid.</div>')
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
                                     pf.get("subs_equity"))
    if not (args.test or args.dry_run):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(snapshot_path(today_str), "w", encoding="utf-8") as f:
            json.dump(pf["snapshot"], f, indent=1)
        write_history(history_preview)
        log(f"snapshot + history written for {today_str}")

    chart_png = os.path.join(LOG_DIR, f"chart_{today_str}.png")
    chart_ok = render_chart(history_preview, chart_png)

    imm = None
    if IMM_SECTION_ENABLED and not args.no_imm:
        imm = imm_section(today_str)
        if imm.get("error"):
            log(f"! IMM section unavailable: {imm['error']}")

    subject, text, html = build_email(pf, history_preview, chart_ok, imm)
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
