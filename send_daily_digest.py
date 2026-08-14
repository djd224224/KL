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
    annual section (2026-08-13)."""
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
