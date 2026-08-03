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
FILL_LOOKBACK_HOURS = int(os.environ.get("IMM_DIGEST_FILL_HOURS", 96))
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

    realized, rep_pos, rep_avg = replay_realized(client, our_ids)
    # Settlement P&L (2026-08-03 fix): a position the bot held into settlement
    # is dropped from the persisted own-book, so it appears in NEITHER
    # realized-from-fills NOR unrealized — the loss (or gain) vanished from
    # the digest entirely. Book it here from the replayed position whenever
    # the market has settled and the own-book no longer carries it.
    mids, results = current_mids(
        client, set(pos) | set(rep_pos) | {t for t in realized})
    settled_pnl = {}
    for t, rp in rep_pos.items():
        if abs(rp) < 0.01 or t not in results:
            continue
        if abs(pos.get(t, 0.0)) >= 0.01:
            continue                    # still open in the own-book: not settled out
        val = 100.0 if results[t] == "yes" else 0.0
        settled_pnl[t] = rp * (val - rep_avg.get(t, 0.0)) / 100.0
    for t, v in settled_pnl.items():
        realized[t] = realized.get(t, 0.0) + v
    if settled_pnl:
        log(f"booked settlement P&L on {len(settled_pnl)} market(s): "
            f"${sum(settled_pnl.values()):+,.2f}")

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
    ss = status_summary(status)
    reward_amt, reward_label = last_full_day_reward(
        load_json(STATE_PATH), status, today_ct)
    ss["reward"] = reward_amt
    ss["reward_label"] = reward_label

    rows, tot, resting = event_rows(client)
    total_pnl = tot["realized"] + tot["unrealized"]

    try:
        balance = _f(client.get_balance().get("balance_dollars"))
        bal_str = f"${balance:,.2f}"
    except Exception:
        bal_str = "?"

    health = health_line(status, ss)
    reward, cmin, eff = ss["reward"], ss["contract_min"], ss["efficiency"]
    reward_lifetime = ss["reward_lifetime"]

    # ---- plain text (fallback part) ----------------------------------------
    lines = [f"Kalshi incentive MM — {today_ct}", ""]
    lines.append(f"TOTAL EST REWARD (cumulative): ${reward_lifetime:,.2f}")
    lines.append(f"EST REWARD ({ss['reward_label']}): ${reward:,.2f}  "
                 f"({cmin:,.0f} contract-min, {eff:.1f}c/1k-contract-min)")
    lines.append(f"P&L: {total_pnl:+,.2f}  (realized {tot['realized']:+,.2f}, "
                 f"unrealized {tot['unrealized']:+,.2f})")
    lines.append(f"Net position {tot['net_pos']:+,.0f} contracts | "
                 f"inventory exposure ${tot['exposure']:,.2f} | "
                 f"resting quotes ${resting['collateral']:,.2f} "
                 f"({resting['orders']} orders / {resting['events']} events) | "
                 f"balance {bal_str}")
    lines.append("")
    if rows:
        lines.append(f"{'EVENT':26s} {'P&L$':>8s} {'REAL$':>8s} {'UNREAL$':>8s} "
                     f"{'NET':>6s} {'EXPO$':>8s} {'Q':>3s}")
        for ev, d in rows:
            lines.append(f"{_short_event(ev)[:26]:26s} {d['pnl']:>+8.2f} "
                         f"{d['realized']:>+8.2f} {d['unrealized']:>+8.2f} "
                         f"{d['net_pos']:>+6.0f} {d['exposure']:>8.2f} {d['quoted']:>3d}")
    else:
        lines.append("No open inventory and no resting quotes.")
    rd = rain_dir_section(client)
    if rd:
        lines.append("")
        lines.extend(rd[0])
    lines.append("")
    lines.append(health)
    lines.append("(reward is the bot's own estimate.)")
    text = "\n".join(lines)

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">']
    h.append(f'<div style="font-size:17px;font-weight:600">Kalshi incentive MM'
             f' <span style="color:#888;font-weight:400">— {today_ct}</span></div>')
    h.append(f'<div style="font-size:24px;font-weight:800;margin:8px 0 2px">'
             f'Total est reward: '
             f'<span style="color:#0a7a2f">${reward_lifetime:,.2f}</span>'
             f'<span style="font-size:13px;font-weight:400;color:#999"> cumulative</span></div>')
    h.append(f'<div style="font-size:16px;font-weight:600;margin:2px 0 2px">'
             f'{ss["reward_label"]}: '
             f'<span style="color:#0a7a2f">${reward:,.2f}</span></div>')
    h.append(f'<div style="color:#555;margin-bottom:10px">'
             f'{cmin:,.0f} contract-min &nbsp;·&nbsp; {eff:.1f}c / 1k contract-min '
             f'&nbsp;·&nbsp; <span style="color:#999">estimate</span></div>')
    h.append(f'<div style="font-size:15px;font-weight:600;margin:4px 0 2px">'
             f'P&amp;L: {_pnl_span(total_pnl)}</div>')
    h.append(f'<div style="color:#555;margin-bottom:12px">'
             f'realized {_pnl_span(tot["realized"])} &nbsp;·&nbsp; '
             f'unrealized {_pnl_span(tot["unrealized"])}<br>'
             f'net position <b>{tot["net_pos"]:+,.0f}</b> contracts &nbsp;·&nbsp; '
             f'inventory exposure <b>${tot["exposure"]:,.2f}</b> &nbsp;·&nbsp; '
             f'resting quotes <b>${resting["collateral"]:,.2f}</b> '
             f'({resting["orders"]} orders) &nbsp;·&nbsp; '
             f'balance <b>{bal_str}</b></div>')
    if rows:
        h.append('<table style="border-collapse:collapse">')
        h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
                 f'<td style="{TDL}">EVENT</td><td style="{TD}">P&amp;L$</td>'
                 f'<td style="{TD}">REAL$</td><td style="{TD}">UNREAL$</td>'
                 f'<td style="{TD}">NET</td><td style="{TD}">EXPO$</td>'
                 f'<td style="{TD}">Q</td></tr>')
        for i, (ev, d) in enumerate(rows):
            bg = "#fafafa" if i % 2 else "#fff"
            h.append(f'<tr style="background:{bg}">'
                     f'<td style="{TDL}">{_short_event(ev)}</td>'
                     f'<td style="{TD};font-weight:600">{_pnl_span(d["pnl"])}</td>'
                     f'<td style="{TD}">{_pnl_span(d["realized"])}</td>'
                     f'<td style="{TD}">{_pnl_span(d["unrealized"])}</td>'
                     f'<td style="{TD}">{d["net_pos"]:+,.0f}</td>'
                     f'<td style="{TD}">{d["exposure"]:,.2f}</td>'
                     f'<td style="{TD}">{d["quoted"]}</td></tr>')
        h.append(f'<tr style="background:#f0f0f0;font-weight:700">'
                 f'<td style="{TDL}">TOTAL</td>'
                 f'<td style="{TD}">{_pnl_span(total_pnl)}</td>'
                 f'<td style="{TD}">{_pnl_span(tot["realized"])}</td>'
                 f'<td style="{TD}">{_pnl_span(tot["unrealized"])}</td>'
                 f'<td style="{TD}">{tot["net_pos"]:+,.0f}</td>'
                 f'<td style="{TD}">{tot["exposure"]:,.2f}</td>'
                 f'<td style="{TD}"></td></tr>')
        h.append('</table>')
        h.append('<div style="color:#888;font-size:12px;margin-top:6px">'
                 'Q = markets currently quoted in the event. EXPO$ = inventory '
                 'cost basis. Own-book only (excludes manual + other bots).</div>')
    else:
        h.append('<div>No open inventory and no resting quotes.</div>')
    if rd:
        h.append(rd[1])
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
