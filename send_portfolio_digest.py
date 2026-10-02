#!/usr/bin/env python3
r"""
send_portfolio_digest.py — 7:00 AM ET whole-account portfolio email.

One email covering the ENTIRE Kalshi account (every bot + manual trades):
a chart of daily account value (cash + open positions marked to mid), then
the biggest movers since the prior morning by family (settled and unrealized
alike, ranked by the size of the move, realized and mark-to-mid split out).
(The "Settled since yesterday, by series" table was removed 2026-09-29.)

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


def _pnl_span(v: float) -> str:
    color = C_POS if v > 0.005 else (C_NEG if v < -0.005 else "#777")
    return f'<span style="color:{color}">{v:+,.2f}</span>'


def event_from_ticker(ticker: str) -> str:
    """Fallback only (used when the markets endpoint can't tell us): Kalshi
    market tickers are <event>-<strike-suffix>; single-segment tickers are
    their own event."""
    return ticker.rsplit("-", 1)[0] if ticker.count("-") >= 2 else ticker


# Table grouping (Jack 8/14: "table is too long, maybe just group by series").
# Strict series tickers barely compress (every city/tenor is its own series),
# so known fleets roll up into families; anything unrecognized shows as its
# series ticker.
FAMILY_RULES = [
    (re.compile(r"^KXHIGH"), "High temps (KXHIGH*)"),
    (re.compile(r"^KXLOW"), "Low temps (KXLOW*)"),
    (re.compile(r"^KXTEMP"), "Hourly temps (KXTEMP*)"),
    (re.compile(r"^KXUST"), "Treasury rates (KXUST*)"),
    (re.compile(r"^KX(BTC|ETH|SOL|XRP|DOGE|BNB|HYPE|ZEC)D$"), "Crypto up/down (KX*D)"),
    (re.compile(r"^KX(BTC|ETH|SOL|XRP|DOGE|BNB|HYPE|ZEC)(MAXMON|MINMON)$"),
     "Crypto monthly touch"),
    (re.compile(r"^KX(BTC|ETH|SOL|XRP|DOGE|BNB|HYPE|ZEC)(MAXY|MINY|Y)$"),
     "Crypto annual"),
    (re.compile(r"^KX(AAAGAS|DIESEL)"), "Gas & diesel (AAA)"),
    (re.compile(r"^KXRAIN"), "Rain (KXRAIN*)"),
    (re.compile(r"MENTION"), "Mention markets"),
    (re.compile(r"^KXAQI"), "Air quality (KXAQI*)"),
    # 2026-09-22: the movers table made the long tail visible — fleets and
    # vendor families that had been showing as one line per series.
    # (?!X): KXNFLX* is Netflix, not football — it belongs to the APP rule
    (re.compile(r"^KXNFL(?!X)"), "NFL (KXNFL*)"),
    (re.compile(r"^KXRT$"), "Rotten Tomatoes (KXRT)"),
    (re.compile(r"CC$"), "Carbon Arc cards (KX*CC)"),
    (re.compile(r"APP$"), "App downloads (KX*APP)"),
]


def family_for(event_ticker: str) -> str:
    series = event_ticker.split("-", 1)[0]
    for rx, label in FAMILY_RULES:
        if rx.search(series):
            return label
    return series


# ----------------------------------------------------------------------------
# Data pulls
# ----------------------------------------------------------------------------

def fetch_unsettled_positions(client):
    """All unsettled positions account-wide. Returns (events, mkt_pos):
    events: ev -> {"realized": $, "fees": $};
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
                             "fees": _f(ev.get("fees_paid_dollars"))}
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


def fetch_net_transfers(client, t0: datetime, t1: datetime):
    """Deposits minus withdrawals in (t0, t1] in dollars, or None when either
    read fails (the day change then lumps them in with credits)."""
    net = 0.0
    for path, sign in (("/portfolio/deposits", 1.0), ("/portfolio/withdrawals", -1.0)):
        try:
            resp = client.get(path, params={"limit": 200})
        except Exception as e:
            log(f"! {path} read failed: {e!r}")
            return None
        items = next((v for v in resp.values() if isinstance(v, list)), [])
        for it in items:
            ts = it.get("finalized_ts") or it.get("created_ts") or 0
            status = str(it.get("status") or "").lower()
            if t0.timestamp() < float(ts) <= t1.timestamp() and \
                    status not in ("failed", "cancelled", "canceled", "rejected", "pending"):
                net += sign * _f(it.get("amount_cents")) / 100.0
    return round(net, 2)


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
            settled.setdefault(ev_of(tk), set()).add(x.get("market_result") or "?")
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
                "kalshi_positions_value", "perps_equity", "unpaid_rewards_est"]


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
                for k in ("perps_equity", "unpaid_rewards_est"):   # 9/28 on
                    try:
                        row[k] = float(r.get(k) or "")
                    except ValueError:
                        row[k] = ""
                rows.append(row)
    rows.sort(key=lambda r: r["date"])
    return rows


def upsert_history(rows, today_str, cash, pos_value, equity, kalshi_pv,
                   perps_equity=None, unpaid=None):
    rows = [r for r in rows if r["date"] != today_str]
    rows.append({"date": today_str, "cash": round(cash, 2),
                 "positions_value": round(pos_value, 2),
                 "equity": round(equity, 2),
                 "kalshi_positions_value": round(kalshi_pv, 2),
                 "perps_equity": "" if perps_equity is None else round(perps_equity, 2),
                 "unpaid_rewards_est": "" if unpaid is None else round(unpaid, 2)})
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
    equity_kalshi = round(cash + kalshi_pv, 2)
    account_value = round(equity_kalshi + (perps_equity or 0.0), 2)
    unpaid = estimate_unpaid_rewards(client, now_utc)

    snapshot = {"date": today_str,
                "created_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cash": round(cash, 2), "positions_value": positions_value,
                "equity": equity,
                "kalshi_positions_value": round(kalshi_pv, 2),
                "equity_kalshi": equity_kalshi,
                "perps_equity": perps_equity, "perps_stale": perps_stale,
                "perps_positions": (perps or {}).get("positions"),
                "account_value": account_value,
                "unpaid_rewards_est": (unpaid or {}).get("total"),
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
                         "value_d": value_d, "value_now": value_now, "note": note})
        no_trade_cash = round(cash - _f(prior.get("cash")) - trade_cash, 2)
        net_transfers = fetch_net_transfers(client, prior_created, now_utc)
        check = (sum(r["day"] for r in rows) - trade_cash
                 - (sum(v for _, v in tk_end.values()) - sum(v for _, v in start.values())))
        replay_info = {"fills": len(fills), "settlements": len(win_setts),
                       "mismatches": len(mism), "trade_cash": trade_cash}
        log(f"replay: {len(fills)} fills, {len(win_setts)} settlements, "
            f"{len(mism)} mismatches; trading cash {trade_cash:+,.2f}, cash with "
            f"no trade behind it {no_trade_cash:+,.2f}, transfers "
            f"{'n/a' if net_transfers is None else f'{net_transfers:+,.2f}'}; "
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
            "perps_stale": perps_stale, "account_value": account_value,
            "unpaid": unpaid,
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
    # Perpetuals are in from 9/28 (earlier rows have none recorded).
    eq = [(r["cash"] + r["kalshi_positions_value"]
           if isinstance(r.get("kalshi_positions_value"), float) else r["equity"])
          + (r["perps_equity"] if isinstance(r.get("perps_equity"), float) else 0.0)
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


def build_email(pf, history, chart_ok: bool):
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
    d_equity = None if first else round(d_ek + (d_perps or 0.0), 2)
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
    ex_perps = " ex-perpetuals" if perps_eq is not None else ""
    trading = mv_tot["day"]
    parts = None                        # [(label, $)] summing to d_ek exactly
    if not first:
        ntc = pf.get("no_trade_cash")
        if ntc is None:                 # no replay (legacy prior snapshot)
            parts = [("trading (at mid)", trading),
                     ("credits, deposits & Kalshi's pricing vs mid",
                      round(d_ek - trading, 2))]
        else:
            transfers = pf.get("net_transfers")
            parts = [("trading (at mid)", trading),
                     ("reward credits" if transfers is not None
                      else "reward credits & deposits",
                      round(ntc - (transfers or 0.0), 2))]
            if transfers:
                parts.append(("deposits/withdrawals", transfers))
            parts.append(("Kalshi's pricing vs mid", round(d_ek - trading - ntc, 2)))

    subject = (f"Kalshi portfolio {today} — first baseline" if first else
               f"Kalshi portfolio {today} — day {d_equity:+,.2f}, "
               f"trading {trading:+,.2f}")

    # ---- plain text ---------------------------------------------------------
    lines = [f"Kalshi portfolio — {today} (7am ET)", ""]
    lines.append(f"Account value ${acct:,.2f}{after_txt}")
    lines.append(f"  =  cash ${pf['cash']:,.2f}  +  open positions "
                 f"${pf['kalshi_positions_value']:,.2f}"
                 + ("" if perps_eq is None else
                    f"  +  perpetuals ${perps_eq:,.2f}{perps_note}"))
    if unpaid_note:
        lines.append(unpaid_note)
    if first:
        lines.append("First run: baseline saved; day-over-day starts tomorrow.")
    else:
        lines.append(f"vs yesterday: {d_equity:+,.2f}  =  "
                     + "  +  ".join(f"{k} {v:+,.2f}" for k, v in parts)
                     + ("" if d_perps is None else
                        f"  +  perpetuals {d_perps:+,.2f}"))
    lines.append("")
    lines.append(f"Biggest movers since yesterday, by family (settled + unrealized; "
                 f"top {len(movers)} of {mv_tot['n_families']} families):")
    lines.append(f"{'FAMILY':28s} {'DAY P&L':>9s} {'REALIZED':>9s} "
                 f"{'UNREALIZED':>10s} {'OPEN NOW':>9s}")
    for name, g in movers:
        n = len(g["rows"])
        lines.append(f"{name[:28]:28s} {g['day']:>+9.2f} {g['realized']:>+9.2f} "
                     f"{g['unreal']:>+10.2f} {g['value_now']:>9.2f}"
                     f"  ({n} event{'s' if n != 1 else ''})")
        for r in g["top"]:
            lines.append(f"    {r['event']:34s} {r['day']:>+9.2f}  {_mover_tag(r)}")
    if mv_hidden_n:
        lines.append(f"{'+' + str(mv_hidden_n) + ' more families':28s} "
                     f"{mv_hidden_net:>+9.2f}")
    lines.append(f"{'ALL FAMILIES':28s} {mv_tot['day']:>+9.2f} {mv_tot['realized']:>+9.2f} "
                 f"{mv_tot['unreal']:>+10.2f} {mv_tot['value_now']:>9.2f}"
                 f"  ({mv_tot['n_events']} events)")
    if not movers:
        lines.append("(nothing moved since the prior morning)")
    if parts is not None:
        lines.append(f"(account value{ex_perps} moved {d_ek:+,.2f} = this table "
                     f"{trading:+,.2f}"
                     + "".join(f"  +  {k} {v:+,.2f}" for k, v in parts[1:]) + ")")
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
    h.append(f'<div style="color:{C_INK2};margin-bottom:6px">'
             f'cash <b>${pf["cash"]:,.2f}</b> &nbsp;&middot;&nbsp; '
             f'open positions <b>${pf["kalshi_positions_value"]:,.2f}</b>'
             + ("" if perps_eq is None else
                f' &nbsp;&middot;&nbsp; perpetuals <b>${perps_eq:,.2f}</b>'
                f'<span style="color:{C_MUTED}">{perps_note}</span>')
             + ("" if unpaid_total is None else
                f' &nbsp;&middot;&nbsp; rewards earned, not yet paid '
                f'<b>&asymp; ${unpaid_total:,.2f}</b>')
             + ("" if first else
                f'<br>day change {_pnl_span(d_equity)} = '
                + ' &nbsp;+&nbsp; '.join(f'{k.replace("&", "&amp;")} {_pnl_span(v)}'
                                         for k, v in parts)
                + ("" if d_perps is None else
                   f' &nbsp;+&nbsp; perpetuals {_pnl_span(d_perps)}'))
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

    h.append(f'<div style="font-size:15px;font-weight:600;margin:12px 0 4px">'
             f'Biggest movers since yesterday, by family'
             f' <span style="color:{C_MUTED};font-weight:400;font-size:13px">'
             f'settled + unrealized &middot; top {len(movers)} of '
             f'{mv_tot["n_families"]} families</span></div>')
    if movers:
        h.append('<table style="border-collapse:collapse;font-size:13px">')
        h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
                 f'<td style="{TDL}">Family</td><td style="{TD}">Day P&amp;L $</td>'
                 f'<td style="{TD}">Realized $</td><td style="{TD}">Unrealized $</td>'
                 f'<td style="{TD}">Open now $</td>'
                 f'<td style="{TDL}">Top events (by size of move)</td></tr>')
        for i, (name, g) in enumerate(movers):
            bg = "#fafafa" if i % 2 else "#fff"
            evs = "<br>".join(
                f'{r["event"]}&nbsp; {_pnl_span(r["day"])} '
                f'<span style="color:{C_MUTED}">{_mover_tag(r)}</span>'
                for r in g["top"])
            more = len(g["rows"]) - len(g["top"])
            if more > 0:
                evs += f'<br><span style="color:{C_MUTED}">+{more} more</span>'
            n = len(g["rows"])
            h.append(f'<tr style="background:{bg}">'
                     f'<td style="{TDL}vertical-align:top">{name}'
                     f'<div style="color:{C_MUTED};font-size:11px">'
                     f'{n} event{"s" if n != 1 else ""}</div></td>'
                     f'<td style="{TD}font-weight:600;vertical-align:top">'
                     f'{_pnl_span(g["day"])}</td>'
                     f'<td style="{TD}vertical-align:top">{_pnl_span(g["realized"])}</td>'
                     f'<td style="{TD}vertical-align:top">{_pnl_span(g["unreal"])}</td>'
                     f'<td style="{TD}vertical-align:top">{g["value_now"]:,.2f}</td>'
                     f'<td style="{TDL}font-size:12px">{evs}</td></tr>')
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
                     f'Account value{ex_perps} moved {_pnl_span(d_ek)} = this table '
                     f'{_pnl_span(trading)}'
                     + "".join(f' &nbsp;+&nbsp; {k.replace("&", "&amp;")} {_pnl_span(v)}'
                               for k, v in parts[1:])
                     + '. Reward credits = cash that came in with no trade or '
                       'settlement behind it; Kalshi values open positions near '
                       'what they would sell for, this table at the mid.</div>')
    else:
        h.append(f'<div style="color:{C_INK2}">Nothing moved since the prior '
                 f'morning.</div>')

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
                                     (pf.get("unpaid") or {}).get("total"))
    if not (args.test or args.dry_run):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(snapshot_path(today_str), "w", encoding="utf-8") as f:
            json.dump(pf["snapshot"], f, indent=1)
        write_history(history_preview)
        log(f"snapshot + history written for {today_str}")

    chart_png = os.path.join(LOG_DIR, f"chart_{today_str}.png")
    chart_ok = render_chart(history_preview, chart_png)

    subject, text, html = build_email(pf, history_preview, chart_ok)
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
                glob.glob(os.path.join(LOG_DIR, "digest_*.html")):
            m = re.search(r"(\d{4}-\d{2}-\d{2})\.(?:png|html)$", old)
            try:
                if m and (datetime.strptime(m.group(1), "%Y-%m-%d").date()
                          < cutoff - timedelta(days=7)):
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
