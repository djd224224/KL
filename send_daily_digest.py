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


def fleet_health() -> str:
    """One line: only problems (stale bots, error counts from stored summaries)."""
    problems = []
    errs = 0
    seen = 0
    now = datetime.now(timezone.utc)
    for path in sorted(glob.glob(os.path.join(STATUS_DIR, "status_*.json"))):
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
    line = f"Bots: {seen - len([p for p in problems if 'STALE' in p])}/{seen} alive, " \
           f"{errs} errors in yesterday's summaries"
    if problems:
        line += " | " + "; ".join(problems)
    return line


def _pnl_span(v: float, decimals: int = 2) -> str:
    color = "#0a7a2f" if v > 0.005 else ("#c0392b" if v < -0.005 else "#777")
    return f'<span style="color:{color}">{v:+,.{decimals}f}</span>'


TD = 'padding:5px 12px;border:1px solid #ddd;text-align:right;'
TDL = 'padding:5px 12px;border:1px solid #ddd;text-align:left;'


def build_digest(now_utc: datetime):
    """Returns (plain_text, html)."""
    today_ct = now_utc.astimezone(CT).date()
    client = build_client()

    rows = []
    tot = {"realized": 0.0, "fees": 0.0, "unrealized": 0.0, "net_pos": 0.0, "exposure": 0.0}
    failed = []
    for key in sorted(MARKETS):
        ev = event_ticker_for(MARKETS[key], now_utc)
        pnl = event_pnl(client, ev)
        if not pnl["ok"]:
            failed.append(key)
            continue
        for k in tot:
            tot[k] += pnl[k]
        if (abs(pnl["realized"]) > 0.005 or abs(pnl["unrealized"]) > 0.005
                or abs(pnl["net_pos"]) > 0.5 or pnl["exposure"] > 0.005):
            rows.append((key, pnl))
    for _key, p in rows:
        p["pnl"] = p["realized"] + p["unrealized"]
    rows.sort(key=lambda r: -r[1]["pnl"])   # best to worst

    try:
        balance = _f(client.get_balance().get("balance_dollars"))
        bal_str = f"${balance:,.2f}"
    except Exception:
        bal_str = "?"

    total_pnl = tot["realized"] + tot["unrealized"]
    quiet = len(MARKETS) - len(rows) - len(failed)
    health = fleet_health()

    # ---- plain text (fallback part) ----------------------------------------
    lines = [f"Kalshi crypto MM — {today_ct}", ""]
    lines.append(f"FLEET P&L: {total_pnl:+,.2f}  "
                 f"(realized {tot['realized']:+,.2f}, unrealized {tot['unrealized']:+,.2f}, "
                 f"fees {tot['fees']:,.2f})")
    lines.append(f"Net position {tot['net_pos']:+,.0f} contracts | "
                 f"$ exposure ${tot['exposure']:,.2f} | balance {bal_str}")
    lines.append("")
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
    lines.append("")
    lines.append(health)
    text = "\n".join(lines)

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">']
    h.append(f'<div style="font-size:17px;font-weight:600">Kalshi crypto MM'
             f' <span style="color:#888;font-weight:400">— {today_ct}</span></div>')
    h.append(f'<div style="font-size:21px;font-weight:700;margin:8px 0 2px">'
             f'Fleet P&amp;L: {_pnl_span(total_pnl)}</div>')
    h.append(f'<div style="color:#555;margin-bottom:12px">'
             f'realized {_pnl_span(tot["realized"])} &nbsp;·&nbsp; '
             f'unrealized {_pnl_span(tot["unrealized"])} &nbsp;·&nbsp; '
             f'fees {tot["fees"]:,.2f}<br>'
             f'net position <b>{tot["net_pos"]:+,.0f}</b> contracts &nbsp;·&nbsp; '
             f'exposure <b>${tot["exposure"]:,.2f}</b> &nbsp;·&nbsp; '
             f'balance <b>{bal_str}</b></div>')
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
    h.append(f'<div style="color:#777;font-size:12px;margin-top:12px;'
             f'border-top:1px solid #eee;padding-top:8px">{health}</div>')
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
