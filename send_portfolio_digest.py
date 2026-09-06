#!/usr/bin/env python3
r"""
send_portfolio_digest.py — 7:00 AM ET whole-account portfolio email.

One email covering the ENTIRE Kalshi account (every bot + manual trades):
a chart of daily account value (cash + open positions marked to mid), then
a table of gains and losses vs the prior morning, by event.

Day P&L per event = change in E(event) between morning snapshots, where
    E = value of open contracts (marked bid/ask mid, else last, else
        carried/cost) + cumulative realized P&L - cumulative fees
E comes from Kalshi's positions endpoint (event rollups), so settlements,
sells, and new fills are all captured; a settlement just moves money from
the "open value" component into "realized" and the day P&L nets the truth.

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
                "kalshi_positions_value"]


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
                rows.append(row)
    rows.sort(key=lambda r: r["date"])
    return rows


def upsert_history(rows, today_str, cash, pos_value, equity, kalshi_pv):
    rows = [r for r in rows if r["date"] != today_str]
    rows.append({"date": today_str, "cash": round(cash, 2),
                 "positions_value": round(pos_value, 2),
                 "equity": round(equity, 2),
                 "kalshi_positions_value": round(kalshi_pv, 2)})
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


    # Mark every open position: mid, else last, else yesterday's mark, else
    # at cost (unrealized 0 for that leg — neutral, never a fake loss).
    mark_info = fetch_market_info(client, list(mkt_pos))
    marks, mark_src = {}, {"mid": 0, "last": 0, "carried": 0, "at_cost": 0}
    for tk in mkt_pos:
        mi = mark_info.get(tk) or {}
        bid, ask, last = mi.get("yes_bid", 0.0), mi.get("yes_ask", 0.0), mi.get("last", 0.0)
        if bid > 0 and ask > 0:
            marks[tk] = (bid + ask) / 2.0
            mark_src["mid"] += 1
        elif last > 0:
            marks[tk] = last
            mark_src["last"] += 1
        elif tk in prior_marks:
            marks[tk] = _f(prior_marks[tk])
            mark_src["carried"] += 1
        else:
            marks[tk] = None
            mark_src["at_cost"] += 1

    # Group open value + cost basis by event.
    ev_value, ev_basis, ev_tickers = {}, {}, {}
    for tk, rec in mkt_pos.items():
        pos, cost = rec["pos"], rec["cost"]
        ev = (mark_info.get(tk) or {}).get("event") or event_from_ticker(tk)
        mark = marks.get(tk)
        if mark is None:
            val = cost                     # marked at cost: unrealized 0
        else:
            val = pos * mark if pos > 0 else -pos * (1.0 - mark)
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

    snapshot = {"date": today_str,
                "created_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cash": round(cash, 2), "positions_value": positions_value,
                "equity": equity,
                "kalshi_positions_value": round(kalshi_pv, 2),
                "equity_kalshi": round(cash + kalshi_pv, 2),
                "events": {ev: e for ev, e in events_today.items()
                           if ev not in carried_dead},
                "tickers": ev_tickers,
                "marks": {tk: m for tk, m in ((t, marks.get(t)) for t in mkt_pos)
                          if m is not None}}

    # Day-over-day rows.
    def P(e):
        return (e["value"] - e.get("basis", 0.0)) + e["realized"] - e["fees"]

    empty = {"realized": 0.0, "fees": 0.0, "value": 0.0, "basis": 0.0}
    rows = []
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
            "equity_kalshi": round(cash + kalshi_pv, 2),
            "rows": rows, "snapshot": snapshot, "prior": prior,
            "mark_src": mark_src, "unreadable": unreadable,
            "n_settlements": len(settlements),
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
    eq = [r["cash"] + r["kalshi_positions_value"]
          if isinstance(r.get("kalshi_positions_value"), float) else r["equity"]
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

def build_email(pf, history, chart_ok: bool):
    today = pf["today"]
    settled_rows = [r for r in pf["rows"] if r["note"].startswith("settled")]

    # One row per series family (Jack 8/14: per-event was too long). The P&L
    # shown is the realized settlement result (net of fees) straight from
    # Kalshi's records — the former Day-P&L column (realized plus the
    # reversal of our own prior marks) collapsed into it (Jack 8/14: "whats
    # the diff? if nothing then collapse"); mark drift sits in the summary
    # line's open-positions bucket instead.
    groups = {}
    for r in settled_rows:
        g = groups.setdefault(family_for(r["event"]),
                              {"pnl": 0.0, "value_now": 0.0, "rows": []})
        g["pnl"] += r["realized"]
        g["value_now"] += r["value_now"]
        g["rows"].append(r)
    grows = sorted(groups.items(), key=lambda kv: -kv[1]["pnl"])
    tot_pnl = round(sum(g["pnl"] for _, g in grows), 2)
    tot_open = round(sum(g["value_now"] for _, g in grows), 2)
    n_events = len(settled_rows)

    def top_events(g, n=5):
        return sorted(g["rows"], key=lambda r: -abs(r["realized"]))[:n]

    first = pf["first_run"]
    prior = pf["prior"] or {}
    # Account value on Kalshi's own valuation (cash + their portfolio_value).
    d_equity = None if first else round(
        pf["equity_kalshi"] - _f(prior.get("equity_kalshi") or prior.get("equity")), 2)
    other = None if first else round(d_equity - tot_pnl, 2)

    subject = (f"Kalshi portfolio {today} — first baseline" if first else
               f"Kalshi portfolio {today} — day {d_equity:+,.2f}, "
               f"settled {tot_pnl:+,.2f}")

    # ---- plain text ---------------------------------------------------------
    lines = [f"Kalshi portfolio — {today} (7am ET)", ""]
    lines.append(f"Account value ${pf['equity_kalshi']:,.2f}  =  cash "
                 f"${pf['cash']:,.2f}  +  open positions "
                 f"${pf['kalshi_positions_value']:,.2f}")
    if first:
        lines.append("First run: baseline saved; day-over-day starts tomorrow.")
    else:
        lines.append(f"vs yesterday: {d_equity:+,.2f}  =  settled events "
                     f"{tot_pnl:+,.2f}  +  open positions, credits & deposits "
                     f"{other:+,.2f}")
    lines.append("")
    lines.append(f"Settled since yesterday ({n_events} events):")
    lines.append(f"{'SERIES':28s} {'P&L':>10s} {'STILL OPEN':>11s}  TOP EVENTS")
    for name, g in grows:
        lines.append(f"{name[:28]:28s} {g['pnl']:>+10.2f} {g['value_now']:>11.2f}"
                     f"  ({len(g['rows'])} events)")
        for r in top_events(g):
            lines.append(f"    {r['event']:34s} {r['realized']:>+9.2f}")
    lines.append(f"{'ALL SETTLED':28s} {tot_pnl:>+10.2f} {tot_open:>11.2f}"
                 f"  ({n_events} events)")
    if not settled_rows:
        lines.append("(no events settled since the prior morning)")
    text = "\n".join(lines)

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;'
         f'color:{C_INK};max-width:820px">']
    h.append(f'<div style="font-size:17px;font-weight:600">Kalshi portfolio'
             f' <span style="color:#888;font-weight:400">— {today} (7am ET)</span></div>')
    h.append(f'<div style="font-size:22px;font-weight:700;margin:8px 0 2px">'
             f'Account value ${pf["equity_kalshi"]:,.2f}'
             + ("" if first else
                f' <span style="font-size:15px;font-weight:600">'
                f'({_pnl_span(d_equity)} vs yesterday)</span>')
             + '</div>')
    h.append(f'<div style="color:{C_INK2};margin-bottom:6px">'
             f'cash <b>${pf["cash"]:,.2f}</b> &nbsp;&middot;&nbsp; '
             f'open positions <b>${pf["kalshi_positions_value"]:,.2f}</b>'
             + ("" if first else
                f'<br>day change = settled events {_pnl_span(tot_pnl)}'
                f' &nbsp;+&nbsp; open positions, credits &amp; deposits '
                f'{_pnl_span(other)}')
             + '</div>')
    if first:
        h.append(f'<div style="color:{C_INK2};margin-bottom:6px">First run — '
                 f'baseline saved; day-over-day starts tomorrow.</div>')
    if chart_ok:
        h.append('<div style="margin:10px 0"><img src="cid:balancechart" '
                 'alt="Daily account balance" width="760" '
                 'style="width:100%;max-width:760px;height:auto"></div>')

    h.append(f'<div style="font-size:15px;font-weight:600;margin:12px 0 4px">'
             f'Settled since yesterday, by series</div>')
    if settled_rows:
        h.append('<table style="border-collapse:collapse;font-size:13px">')
        h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
                 f'<td style="{TDL}">Series</td><td style="{TD}">P&amp;L $</td>'
                 f'<td style="{TD}">Still open $</td>'
                 f'<td style="{TDL}">Events (top 5 by P&amp;L)</td></tr>')
        for i, (name, g) in enumerate(grows):
            bg = "#fafafa" if i % 2 else "#fff"
            evs = "<br>".join(f'{r["event"]}&nbsp; {_pnl_span(r["realized"])}'
                              for r in top_events(g))
            more = len(g["rows"]) - 5
            if more > 0:
                evs += f'<br><span style="color:{C_MUTED}">+{more} more</span>'
            n = len(g["rows"])
            h.append(f'<tr style="background:{bg}">'
                     f'<td style="{TDL}vertical-align:top">{name}'
                     f'<div style="color:{C_MUTED};font-size:11px">'
                     f'{n} event{"s" if n != 1 else ""}</div></td>'
                     f'<td style="{TD}font-weight:600;vertical-align:top">'
                     f'{_pnl_span(g["pnl"])}</td>'
                     f'<td style="{TD}vertical-align:top">{g["value_now"]:,.2f}</td>'
                     f'<td style="{TDL}font-size:12px">{evs}</td></tr>')
        h.append(f'<tr style="background:#f0f0f0;font-weight:700">'
                 f'<td style="{TDL}">ALL SETTLED</td>'
                 f'<td style="{TD}">{_pnl_span(tot_pnl)}</td>'
                 f'<td style="{TD}">{tot_open:,.2f}</td>'
                 f'<td style="{TDL}font-weight:400;color:{C_INK2}">'
                 f'{n_events} events</td></tr>')
        h.append('</table>')
    else:
        h.append(f'<div style="color:{C_INK2}">No events settled since the '
                 f'prior morning.</div>')

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
                                     pf["kalshi_positions_value"])
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
