#!/usr/bin/env python3
r"""
send_daily_digest.py — one clear morning email for the crypto MM fleet.

Layout: fleet P&L (realized / unrealized / fees), then one row per market
(bot-event) with realized$, unrealized$, net position, and $ exposure —
markets with nothing going on are omitted. Bot health is one line, listing
only problems (stale heartbeats, error counts).

Realized P&L, exposure, and positions come from Kalshi's positions endpoint
(event rollups include early-settled strikes). Unrealized marks each open
position to the current market mid: YES value = pos x mid, NO value =
|pos| x (1 - mid), minus Kalshi's cost basis (market_exposure).

Scheduled daily shortly after the bots' 8:00 AM CT summary hour. Idempotent
via a sent-marker; --test sends immediately and skips the marker.

Credentials: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD from the environment,
falling back to HKCU\Environment (works under Task Scheduler's stripped env).
"""

import argparse
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
from crypto_touch_mm import (CT, MARKETS, STATUS_DIR, Alerter, build_client,  # noqa: E402
                             event_ticker_for, log)

STALE_AFTER_MINUTES = 30


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def market_mids(client, event_ticker: str) -> dict:
    """ticker -> mid YES price in dollars (bid/ask mid, else last price)."""
    mids = {}
    try:
        resp = client.get_markets(event_ticker=event_ticker, limit=200)
        for m in resp.get("markets") or []:
            bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            if bid > 0 and ask > 0:
                mids[m["ticker"]] = (bid + ask) / 2.0
            elif _f(m.get("last_price_dollars")) > 0:
                mids[m["ticker"]] = _f(m.get("last_price_dollars"))
    except Exception as e:
        log(f"! quotes for {event_ticker} failed: {e}")
    return mids


def event_pnl(client, event_ticker: str) -> dict:
    """Realized/unrealized P&L, net position, and $ exposure for one event."""
    out = {"realized": 0.0, "fees": 0.0, "unrealized": 0.0,
           "net_pos": 0.0, "exposure": 0.0, "ok": True}
    try:
        resp = client.get_positions(event_ticker=event_ticker, limit=200)
    except Exception as e:
        log(f"! positions for {event_ticker} failed: {e}")
        out["ok"] = False
        return out
    for ev in resp.get("event_positions") or []:
        if ev.get("event_ticker") == event_ticker:
            out["realized"] = _f(ev.get("realized_pnl_dollars"))
            out["fees"] = _f(ev.get("fees_paid_dollars"))
            out["exposure"] = _f(ev.get("event_exposure_dollars"))
    mps = [p for p in (resp.get("market_positions") or []) if abs(_f(p.get("position"))) > 0.01]
    if mps:
        mids = market_mids(client, event_ticker)
        for p in mps:
            pos = _f(p.get("position"))
            out["net_pos"] += pos
            mid = mids.get(p.get("ticker"))
            if mid is None:
                continue   # no quote (e.g. settling): leave that leg unmarked
            value = pos * mid if pos > 0 else -pos * (1.0 - mid)
            out["unrealized"] += value - _f(p.get("market_exposure_dollars"))
    return out


WEEKLY_STATUS_DIR = os.environ.get(
    "CUD_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\crypto-updown")
ANNUAL_STATUS_DIR = os.environ.get(
    "CAY_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\crypto-annual")


def fleet_health(status_dir: str = STATUS_DIR, label: str = "Bots") -> str:
    """One line: only problems (stale bots, error counts from stored summaries)."""
    problems = []
    errs = 0
    seen = 0
    now = datetime.now(timezone.utc)
    for path in sorted(glob.glob(os.path.join(status_dir, "status_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            continue
        seen += 1
        name = st.get("market", os.path.basename(path))
        try:
            age_min = (now - datetime.strptime(st.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ")
                       .replace(tzinfo=timezone.utc)).total_seconds() / 60.0
        except ValueError:
            age_min = float("inf")
        if age_min > STALE_AFTER_MINUTES:
            problems.append(f"{name} STALE since {st.get('updated_at', '?')}")
        m = re.search(r"errs (\d+)", st.get("summary_body") or "")
        if m:
            errs += int(m.group(1))
    line = f"{label}: {seen - len([p for p in problems if 'STALE' in p])}/{seen} alive, " \
           f"{errs} errors in yesterday's summaries"
    if problems:
        line += " | " + "; ".join(problems)
    return line


def _pnl_span(v: float, decimals: int = 2) -> str:
    color = "#0a7a2f" if v > 0.005 else ("#c0392b" if v < -0.005 else "#777")
    return f'<span style="color:{color}">{v:+,.{decimals}f}</span>'


TD = 'padding:5px 12px;border:1px solid #ddd;text-align:right;'
TDL = 'padding:5px 12px;border:1px solid #ddd;text-align:left;'


def fleet_entries(status_dir: str) -> list:
    """[(asset, [event_tickers])] for a discovery-based fleet (updown,
    annual), read from its bots' own heartbeats (their events are DISCOVERED,
    not computed, so the status files are the source of truth for what each
    bot is trading). A stale heartbeat still names the right event most of the
    day; staleness itself is flagged by that fleet's health line, not here."""
    entries = []
    for path in sorted(glob.glob(os.path.join(status_dir, "status_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            continue
        evs = [e.get("ticker") for e in (st.get("events") or []) if e.get("ticker")]
        entries.append((st.get("market", os.path.basename(path)), evs))
    return entries


# ---------------------------------------------------------------------------
# Cumulative-by-tenor classification (the bottom table, Jack 2026-08-14)
# ---------------------------------------------------------------------------
from crypto_annual_mm import annual_series_for as _annual_series_for  # noqa: E402

MONTHLY_SERIES = {cfg.series for cfg in MARKETS.values()}          # KX*MAXMON/MINMON
UPDOWN_SERIES = {f"KX{cfg.asset}D" for cfg in MARKETS.values()}    # KX*D
ANNUAL_SERIES = set()
for _cfg in MARKETS.values():
    if _cfg.direction == "max":
        ANNUAL_SERIES.update(_annual_series_for(_cfg.asset))       # MAXY/MINY/Y

# series -> row label, matching what each section calls its rows: monthlies
# are "BTC-MIN"/"BTC-MAX", updown and annual are just the asset.
SERIES_LABEL = {}
for _cfg in MARKETS.values():
    SERIES_LABEL[_cfg.series] = _cfg.key
    SERIES_LABEL[f"KX{_cfg.asset}D"] = _cfg.asset
    if _cfg.direction == "max":
        for _s in _annual_series_for(_cfg.asset):
            SERIES_LABEL[_s] = _cfg.asset


def _crypto_family(ticker: str):
    """'daily'/'weekly'/'monthly'/'annual'/'hourly' for a crypto-fleet market
    ticker, else None. KX*D tenor is recovered from the EVENT segment: 5pm-ET
    closes (hour field '17') are weekly on Fridays and daily otherwise; any
    other hour is an hourly market (never quoted by the fleet — manual
    trades land there). Unparseable segments classify as None, not guessed."""
    series = (ticker or "").split("-", 1)[0]
    if series in MONTHLY_SERIES:
        return "monthly"
    if series in ANNUAL_SERIES:
        return "annual"
    if series not in UPDOWN_SERIES:
        return None
    parts = ticker.split("-")
    if len(parts) < 2 or len(parts[1]) < 9:
        return None
    date_part, hour = parts[1][:7], parts[1][7:9]
    if hour != "17":
        return "hourly"
    try:
        close_day = datetime.strptime(date_part, "%y%b%d")
    except ValueError:
        return None
    return "weekly" if close_day.weekday() == 4 else "daily"


def _settlement_pnl(s: dict) -> float:
    """Net P&L of one settlement record: payout on the WINNING side's traded
    count minus ALL money spent on both sides.

    NOT `revenue` — that field is the gross payout on the net held position
    (never negative, never cost-adjusted; summing it inflated the first
    version of this table ~4x, caught by Jack 2026-08-14). The payout-cost
    form is validated against the KXHIGH ground-truth recipe: it reproduces
    the independently-verified monthly sums (Mar -408 / Apr +1,124 / May
    -634 / Jun -1,706 / Jul +1,944) from the same records. Results other
    than yes/no (voids/refunds) fall back to revenue, which for a refund IS
    the money returned."""
    yes_n = _f(s.get("yes_count_fp") or s.get("yes_count"))
    no_n = _f(s.get("no_count_fp") or s.get("no_count"))
    res = s.get("market_result")
    if res == "yes":
        payout = yes_n
    elif res == "no":
        payout = no_n
    else:
        payout = _f(s.get("revenue")) / 100.0
    return payout - _f(s.get("yes_total_cost_dollars")) - _f(s.get("no_total_cost_dollars"))


def _settle_dt(s: dict) -> datetime:
    try:
        return datetime.fromisoformat(
            (s.get("settled_time") or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _event_fully_settled(client, event_ticker: str) -> bool:
    """True iff every market in the event has a result — the whole event is
    done, not just early-touched strikes (verified 2026-08-21: a settled
    monthly shows all markets finalized+result, a live one a mix of active
    and finalized). A failed probe counts as NOT settled: the row appears a
    day late rather than a live event slipping in."""
    try:
        e = client.get_event(event_ticker)
        mkts = e.get("markets") or (e.get("event") or {}).get("markets") or []
        return bool(mkts) and all(m.get("result") for m in mkts)
    except Exception as ex:
        log(f"! settled-probe for {event_ticker} failed: {ex}")
        return False


def recent_settlement_rows(client, recs: list, n: int = 20) -> list:
    """Newest-first [(label, family, settle_date_ct, pnl$)], one row per
    fully settled EVENT (Jack 2026-08-21: early-settled strikes inside a
    still-live monthly/annual one-touch event must NOT make rows). KX*D
    events settle all strikes at one close, so any record there means the
    event is done; monthly/annual events are probed via _event_fully_settled.
    A row carries the WHOLE event's P&L (early touches included) dated by
    its final settle — summed over the records still inside the settlements
    API retention window (~66d), so a far-back early touch (annual events
    especially) can age out of the sum. recs items are
    (dt, family, label, event, pnl)."""
    groups = {}
    for dt, fam, label, event, pnl in recs:
        g = groups.setdefault(event, [label, fam, dt, 0.0])
        g[3] += pnl
        if dt > g[2]:
            g[2] = dt
    rows = []
    for event, (label, fam, dt, pnl) in sorted(
            groups.items(), key=lambda kv: kv[1][2], reverse=True):
        if fam in ("monthly", "annual") and not _event_fully_settled(client, event):
            continue
        rows.append((label, fam, dt.astimezone(CT).date(), pnl))
        if len(rows) >= n:
            break
    return rows


def cumulative_family_realized(client) -> tuple:
    """(family -> all-time realized $, newest-first settlement record tuples
    for recent_settlement_rows) for the crypto fleets' tenor families.

    Settled history comes from the settlements API — the positions endpoint
    AGES OUT settled records (verified 2026-08-14: zero July monthlies remain
    there) while settlements reach back to account origin, the same source
    the KXHIGH ground-truth recipe trusts — scored per record by
    _settlement_pnl (see its docstring for why NOT `revenue`). Round-trip
    realized on STILL-OPEN markets is added from unsettled positions. Round
    trips on markets that later settled are the one blind spot (neither
    source keeps them); these fleets are hold-to-settle-dominant, so the
    miss is small — but this is a measured number with that stated edge,
    not a model."""
    fams = {}
    recs = []
    cursor = None
    for _page in range(500):
        kw = {"limit": 200}
        if cursor:
            kw["cursor"] = cursor
        resp = client.get_portfolio_settlements(**kw)
        batch = resp.get("settlements") or []
        for s in batch:
            ticker = s.get("ticker") or ""
            fam = _crypto_family(ticker)
            if fam:
                pnl = _settlement_pnl(s)
                fams[fam] = fams.get(fam, 0.0) + pnl
                label = SERIES_LABEL.get(ticker.split("-", 1)[0],
                                         ticker.split("-", 1)[0])
                recs.append((_settle_dt(s), fam, label,
                             ticker.rsplit("-", 1)[0], pnl))
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
    cursor = None
    for _page in range(50):
        kw = {"limit": 200, "settlement_status": "unsettled"}
        if cursor:
            kw["cursor"] = cursor
        resp = client.get_positions(**kw)
        batch = resp.get("market_positions") or []
        for p in batch:
            fam = _crypto_family(p.get("ticker") or "")
            if fam:
                fams[fam] = fams.get(fam, 0.0) + _f(p.get("realized_pnl_dollars"))
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
    return fams, recs


CUM_FOOTNOTE = ("realized = settled history + open-market round-trips, "
                "account-level (manual fills on these series included); "
                "unrealized = the sections above")


def cumulative_text(cum, unreal_by) -> list:
    lines = ["== CUMULATIVE P&L by tenor (all-time) ==",
             f"{'TENOR':9s} {'REAL$':>9s} {'UNREAL$':>9s} {'TOTAL$':>9s}"]
    tot_r = tot_u = 0.0
    rows = ["daily", "weekly", "monthly", "annual"]
    if abs(cum.get("hourly", 0.0)) > 0.005:
        rows.append("hourly")
    for fam in rows:
        r = cum.get(fam, 0.0)
        u = unreal_by.get(fam, 0.0)
        tot_r += r
        tot_u += u
        lines.append(f"{fam:9s} {r:>+9.2f} {u:>+9.2f} {r + u:>+9.2f}")
    lines.append(f"{'TOTAL':9s} {tot_r:>+9.2f} {tot_u:>+9.2f} {tot_r + tot_u:>+9.2f}")
    lines.append(f"({CUM_FOOTNOTE})")
    return lines


def cumulative_html(cum, unreal_by) -> list:
    h = ['<div style="font-size:15px;font-weight:600;margin:14px 0 2px">'
         'Cumulative P&amp;L by tenor (all-time)</div>',
         '<table style="border-collapse:collapse">',
         f'<tr style="background:#f0f0f0;font-weight:600">'
         f'<td style="{TDL}">TENOR</td><td style="{TD}">REAL$</td>'
         f'<td style="{TD}">UNREAL$</td><td style="{TD}">TOTAL$</td></tr>']
    tot_r = tot_u = 0.0
    rows = ["daily", "weekly", "monthly", "annual"]
    if abs(cum.get("hourly", 0.0)) > 0.005:
        rows.append("hourly")
    for i, fam in enumerate(rows):
        r = cum.get(fam, 0.0)
        u = unreal_by.get(fam, 0.0)
        tot_r += r
        tot_u += u
        bg = "#fafafa" if i % 2 else "#fff"
        h.append(f'<tr style="background:{bg}"><td style="{TDL}">{fam}</td>'
                 f'<td style="{TD}">{_pnl_span(r)}</td>'
                 f'<td style="{TD}">{_pnl_span(u)}</td>'
                 f'<td style="{TD};font-weight:600">{_pnl_span(r + u)}</td></tr>')
    h.append(f'<tr style="background:#f0f0f0;font-weight:700">'
             f'<td style="{TDL}">TOTAL</td><td style="{TD}">{_pnl_span(tot_r)}</td>'
             f'<td style="{TD}">{_pnl_span(tot_u)}</td>'
             f'<td style="{TD}">{_pnl_span(tot_r + tot_u)}</td></tr>')
    h.append('</table>')
    h.append(f'<div style="color:#888;font-size:12px;margin-top:6px">{CUM_FOOTNOTE}</div>')
    return h


RECENT_FOOTNOTE = ("one row = one fully settled event, whole-event P&L; "
                   "still-live monthlies/annuals appear once they finish")


def recent_settlements_text(recent) -> list:
    lines = ["== RECENT SETTLEMENTS (last 20 events) ==",
             f"{'MARKET':9s} {'TENOR':8s} {'SETTLED':10s} {'P&L$':>9s}"]
    for label, fam, day, pnl in recent:
        lines.append(f"{label:9s} {fam:8s} {day} {pnl:>+9.2f}")
    lines.append(f"({RECENT_FOOTNOTE})")
    return lines


def recent_settlements_html(recent) -> list:
    h = ['<div style="font-size:15px;font-weight:600;margin:14px 0 2px">'
         'Recent settlements (last 20 events)</div>',
         '<table style="border-collapse:collapse">',
         f'<tr style="background:#f0f0f0;font-weight:600">'
         f'<td style="{TDL}">MARKET</td><td style="{TDL}">TENOR</td>'
         f'<td style="{TDL}">SETTLED</td><td style="{TD}">P&amp;L$</td></tr>']
    for i, (label, fam, day, pnl) in enumerate(recent):
        bg = "#fafafa" if i % 2 else "#fff"
        h.append(f'<tr style="background:{bg}"><td style="{TDL}">{label}</td>'
                 f'<td style="{TDL}">{fam}</td><td style="{TDL}">{day}</td>'
                 f'<td style="{TD};font-weight:600">{_pnl_span(pnl)}</td></tr>')
    h.append('</table>')
    h.append(f'<div style="color:#888;font-size:12px;margin-top:6px">'
             f'{RECENT_FOOTNOTE.replace("&", "&amp;")}</div>')
    return h


def _event_exists(client, event_ticker: str) -> bool:
    try:
        client.get_event(event_ticker)
        return True
    except Exception:
        return False


def updown_tenor_entries(client, today_ct) -> tuple:
    """(daily_entries, weekly_entries) for the updown fleet, split by each
    heartbeat event's own cadence tag (the bots quote daily+weekly in one
    process since 2026-08-14, so a single lumped section would hide which
    tenor made the money).

    Each asset's DAILY list also gets yesterday's settled 5pm-ET event
    (ticker computed as {series}-{YY}{MON}{DD}17): a daily settles at 5pm
    and leaves the heartbeat immediately, so the 7am digest would otherwise
    never show a settled daily's realized P&L. When yesterday was Friday
    that 5pm close IS the weekly event, so it lands in the WEEKLY list
    instead. Events that never listed (e.g. KXZECD) are skipped after an
    existence probe; assets with no fills there just count as flat."""
    yday = today_ct - timedelta(days=1)
    yday_ev_suffix = f"{yday:%y%b%d}".upper() + "17"
    yday_was_friday = yday.weekday() == 4
    daily, weekly = [], []
    for path in sorted(glob.glob(os.path.join(WEEKLY_STATUS_DIR, "status_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            continue
        label = st.get("market", os.path.basename(path))
        evs = st.get("events") or []
        d_evs = [e["ticker"] for e in evs
                 if e.get("ticker") and e.get("cadence") == "daily"]
        w_evs = [e["ticker"] for e in evs
                 if e.get("ticker") and e.get("cadence") == "weekly"]
        series = st.get("series")
        if isinstance(series, str) and series:
            settled = f"{series}-{yday_ev_suffix}"
            if settled not in d_evs + w_evs and _event_exists(client, settled):
                (w_evs if yday_was_friday else d_evs).append(settled)
        daily.append((label, d_evs))
        weekly.append((label, w_evs))
    return daily, weekly


def collect_rows(client, entries):
    """Shared row builder: entries = [(label, [event_tickers])]. Returns
    (rows_sorted_best_to_worst, totals, failed_labels, quiet_count). A label
    with several events (future multi-tenor) sums them."""
    rows = []
    tot = {"realized": 0.0, "fees": 0.0, "unrealized": 0.0, "net_pos": 0.0, "exposure": 0.0}
    failed = []
    quiet = 0
    for label, evs in entries:
        agg = {"realized": 0.0, "fees": 0.0, "unrealized": 0.0, "net_pos": 0.0, "exposure": 0.0}
        ok = True
        for ev in evs:
            pnl = event_pnl(client, ev)
            if not pnl["ok"]:
                ok = False
                break
            for k in agg:
                agg[k] += pnl[k]
        if not ok:
            failed.append(label)
            continue
        for k in tot:
            tot[k] += agg[k]
        if (abs(agg["realized"]) > 0.005 or abs(agg["unrealized"]) > 0.005
                or abs(agg["net_pos"]) > 0.5 or agg["exposure"] > 0.005):
            agg["pnl"] = agg["realized"] + agg["unrealized"]
            rows.append((label, agg))
        else:
            quiet += 1
    rows.sort(key=lambda r: -r[1]["pnl"])   # best to worst
    return rows, tot, failed, quiet


def text_section(title, rows, tot, quiet, failed, health):
    total_pnl = tot["realized"] + tot["unrealized"]
    lines = [f"== {title} ==",
             f"P&L: {total_pnl:+,.2f}  (realized {tot['realized']:+,.2f}, "
             f"unrealized {tot['unrealized']:+,.2f}, fees {tot['fees']:,.2f}) | "
             f"net {tot['net_pos']:+,.0f} | expo ${tot['exposure']:,.2f}"]
    if rows:
        lines.append(f"{'MARKET':9s} {'P&L$':>8s} {'REAL$':>8s} {'UNREAL$':>8s} "
                     f"{'NET':>6s} {'EXPO$':>9s}")
        for key, p in rows:
            lines.append(f"{key:9s} {p['pnl']:>+8.2f} {p['realized']:>+8.2f} "
                         f"{p['unrealized']:>+8.2f} {p['net_pos']:>+6.0f} "
                         f"{p['exposure']:>9.2f}")
        if quiet:
            lines.append(f"({quiet} market{'s' if quiet != 1 else ''} flat)")
    else:
        lines.append("All markets flat: no positions, no P&L yet.")
    if failed:
        lines.append(f"! could not read: {', '.join(failed)}")
    lines.append(health)
    return lines


def html_section(title, rows, tot, quiet, failed, health):
    total_pnl = tot["realized"] + tot["unrealized"]
    h = [f'<div style="font-size:15px;font-weight:600;margin:14px 0 2px">{title}</div>']
    h.append(f'<div style="color:#555;margin-bottom:8px">'
             f'P&amp;L {_pnl_span(total_pnl)} &nbsp;&middot;&nbsp; '
             f'realized {_pnl_span(tot["realized"])} &nbsp;&middot;&nbsp; '
             f'unrealized {_pnl_span(tot["unrealized"])} &nbsp;&middot;&nbsp; '
             f'fees {tot["fees"]:,.2f} &nbsp;&middot;&nbsp; '
             f'net <b>{tot["net_pos"]:+,.0f}</b> &nbsp;&middot;&nbsp; '
             f'exposure <b>${tot["exposure"]:,.2f}</b></div>')
    if rows:
        h.append('<table style="border-collapse:collapse">')
        h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
                 f'<td style="{TDL}">MARKET</td><td style="{TD}">P&amp;L$</td>'
                 f'<td style="{TD}">REAL$</td><td style="{TD}">UNREAL$</td>'
                 f'<td style="{TD}">NET</td><td style="{TD}">EXPO$</td></tr>')
        for i, (key, p) in enumerate(rows):
            bg = "#fafafa" if i % 2 else "#fff"
            h.append(f'<tr style="background:{bg}">'
                     f'<td style="{TDL}">{key}</td>'
                     f'<td style="{TD};font-weight:600">{_pnl_span(p["pnl"])}</td>'
                     f'<td style="{TD}">{_pnl_span(p["realized"])}</td>'
                     f'<td style="{TD}">{_pnl_span(p["unrealized"])}</td>'
                     f'<td style="{TD}">{p["net_pos"]:+,.0f}</td>'
                     f'<td style="{TD}">{p["exposure"]:,.2f}</td></tr>')
        h.append(f'<tr style="background:#f0f0f0;font-weight:700">'
                 f'<td style="{TDL}">TOTAL</td>'
                 f'<td style="{TD}">{_pnl_span(total_pnl)}</td>'
                 f'<td style="{TD}">{_pnl_span(tot["realized"])}</td>'
                 f'<td style="{TD}">{_pnl_span(tot["unrealized"])}</td>'
                 f'<td style="{TD}">{tot["net_pos"]:+,.0f}</td>'
                 f'<td style="{TD}">{tot["exposure"]:,.2f}</td></tr>')
        h.append('</table>')
        if quiet:
            h.append(f'<div style="color:#888;font-size:12px;margin-top:6px">'
                     f'{quiet} market{"s" if quiet != 1 else ""} flat '
                     f'(no position, no P&amp;L)</div>')
    else:
        h.append('<div>All markets flat: no positions, no P&amp;L yet.</div>')
    if failed:
        h.append(f'<div style="color:#c0392b;margin-top:6px">could not read: '
                 f'{", ".join(failed)}</div>')
    h.append(f'<div style="color:#777;font-size:12px;margin-top:8px;'
             f'border-top:1px solid #eee;padding-top:6px">{health}</div>')
    return h


def build_digest(now_utc: datetime):
    """Returns (plain_text, html): monthly one-touch section + weekly
    above/below section in the identical format (Jack 2026-08-12), plus the
    annual section (2026-08-13) and, under the cumulative table, the last-20
    settlements table (2026-08-21)."""
    today_ct = now_utc.astimezone(CT).date()
    client = build_client()

    monthly_entries = [(key, [event_ticker_for(MARKETS[key], now_utc)])
                       for key in sorted(MARKETS)]
    m_rows, m_tot, m_failed, m_quiet = collect_rows(client, monthly_entries)
    d_entries, w_entries = updown_tenor_entries(client, today_ct)
    w_rows, w_tot, w_failed, w_quiet = collect_rows(client, w_entries)
    d_rows, d_tot, d_failed, d_quiet = collect_rows(client, d_entries)
    a_rows, a_tot, a_failed, a_quiet = collect_rows(client, fleet_entries(ANNUAL_STATUS_DIR))

    try:
        balance = _f(client.get_balance().get("balance_dollars"))
        bal_str = f"${balance:,.2f}"
    except Exception:
        bal_str = "?"

    # Bottom table: all-time realized per tenor + the sections' current
    # unrealized. A failed sweep degrades to a note — never kills the digest.
    try:
        cum, settle_recs = cumulative_family_realized(client)
        recent = recent_settlement_rows(client, settle_recs)
    except Exception as e:
        log(f"! cumulative-by-tenor sweep failed: {e}")
        cum, recent = None, []
    unreal_by = {"daily": d_tot["unrealized"], "weekly": w_tot["unrealized"],
                 "monthly": m_tot["unrealized"], "annual": a_tot["unrealized"]}

    grand = {k: m_tot[k] + w_tot[k] + d_tot[k] + a_tot[k] for k in m_tot}
    grand_pnl = grand["realized"] + grand["unrealized"]
    m_health = fleet_health(STATUS_DIR, "Monthly bots")
    w_health = fleet_health(WEEKLY_STATUS_DIR, "Updown bots")
    # daily + weekly are the SAME 7 processes; health shown once under weekly
    d_health = "(same bots as the weekly section)"
    a_health = fleet_health(ANNUAL_STATUS_DIR, "Annual bots")

    # ---- plain text (fallback part) ----------------------------------------
    lines = [f"Kalshi crypto MM - {today_ct}", ""]
    lines.append(f"FLEET P&L: {grand_pnl:+,.2f}  "
                 f"(realized {grand['realized']:+,.2f}, unrealized {grand['unrealized']:+,.2f}, "
                 f"fees {grand['fees']:,.2f})")
    lines.append(f"Net position {grand['net_pos']:+,.0f} contracts | "
                 f"$ exposure ${grand['exposure']:,.2f} | balance {bal_str}")
    lines.append("")
    lines += text_section("MONTHLY one-touch (KX*MAXMON/*MINMON)",
                          m_rows, m_tot, m_quiet, m_failed, m_health)
    lines.append("")
    lines += text_section("WEEKLY above/below (KX*D)",
                          w_rows, w_tot, w_quiet, w_failed, w_health)
    lines.append("")
    lines += text_section("DAILY above/below (KX*D, incl. yesterday's settle)",
                          d_rows, d_tot, d_quiet, d_failed, d_health)
    lines.append("")
    lines += text_section("ANNUAL touch+terminal (KX*MAXY/*MINY/*Y)",
                          a_rows, a_tot, a_quiet, a_failed, a_health)
    lines.append("")
    if cum is not None:
        lines += cumulative_text(cum, unreal_by)
    else:
        lines.append("(cumulative-by-tenor table unavailable this morning: "
                     "settlements sweep failed)")
    if recent:
        lines.append("")
        lines += recent_settlements_text(recent)
    text = "\n".join(lines)

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">']
    h.append(f'<div style="font-size:17px;font-weight:600">Kalshi crypto MM'
             f' <span style="color:#888;font-weight:400">- {today_ct}</span></div>')
    h.append(f'<div style="font-size:21px;font-weight:700;margin:8px 0 2px">'
             f'Fleet P&amp;L: {_pnl_span(grand_pnl)}</div>')
    h.append(f'<div style="color:#555;margin-bottom:4px">'
             f'realized {_pnl_span(grand["realized"])} &nbsp;&middot;&nbsp; '
             f'unrealized {_pnl_span(grand["unrealized"])} &nbsp;&middot;&nbsp; '
             f'fees {grand["fees"]:,.2f}<br>'
             f'net position <b>{grand["net_pos"]:+,.0f}</b> contracts &nbsp;&middot;&nbsp; '
             f'exposure <b>${grand["exposure"]:,.2f}</b> &nbsp;&middot;&nbsp; '
             f'balance <b>{bal_str}</b></div>')
    h += html_section("Monthly one-touch (KX*MAXMON / *MINMON)",
                      m_rows, m_tot, m_quiet, m_failed, m_health)
    h += html_section("Weekly above/below (KX*D)",
                      w_rows, w_tot, w_quiet, w_failed, w_health)
    h += html_section("Daily above/below (KX*D, incl. yesterday's settle)",
                      d_rows, d_tot, d_quiet, d_failed, d_health)
    h += html_section("Annual touch+terminal (KX*MAXY / *MINY / *Y)",
                      a_rows, a_tot, a_quiet, a_failed, a_health)
    if cum is not None:
        h += cumulative_html(cum, unreal_by)
    else:
        h.append('<div style="color:#c0392b;margin-top:8px">cumulative-by-tenor '
                 'table unavailable this morning: settlements sweep failed</div>')
    if recent:
        h += recent_settlements_html(recent)
    h.append('</div>')
    return text, "".join(h)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; do not write it")
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    today_ct = now_utc.astimezone(CT).date()
    marker = os.path.join(STATUS_DIR, f"digest_sent_{today_ct}.marker")

    if not args.test and os.path.exists(marker):
        log(f"digest already sent for {today_ct}; exiting")
        return 0

    # The 7am trigger can fire while the laptop is in Modern Standby with the
    # network radio off (observed 2026-07-12: task ran at 7:00:01 mid-standby,
    # exit 1, no email). Retry for up to ~40 minutes so the digest goes out
    # shortly after the machine wakes.
    body = html = None
    for attempt in range(1, 9):
        try:
            body, html = build_digest(now_utc)
            break
        except Exception as e:
            log(f"digest build attempt {attempt}/8 failed: {e!r}; retrying in 5min")
            if attempt == 8:
                log("giving up for today")
                return 1
            time.sleep(300)
    log("digest body:\n" + body)

    alerter = Alerter("FLEET", live=True)
    if not alerter.enabled:
        log("cannot send digest: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, 9):
        ok = alerter.send_message(body, subject=f"Kalshi crypto MM digest {today_ct}",
                                  html=html)
        if ok:
            break
        log(f"digest send attempt {attempt}/8 failed; retrying in 5min")
        time.sleep(300)
    log(f"digest send: {'ok' if ok else 'FAILED'}")
    if ok and not args.test:
        with open(marker, "w") as f:
            f.write(now_utc.isoformat())
        cutoff = today_ct - timedelta(days=7)
        for old in glob.glob(os.path.join(STATUS_DIR, "digest_sent_*.marker")):
            name = os.path.basename(old)[len("digest_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
