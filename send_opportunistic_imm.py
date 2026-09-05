#!/usr/bin/env python3
r"""send_opportunistic_imm.py — the daily "Opportunistic IMM" email
(Jack 2026-09-05: "add daily email called 'opportunistic IMM' showing a
table of the events quoted, earning est, P&L, and net").

The "opportunistic" book is the finecon sweep (incentive_mm.FINECON_SERIES):
the Finance/Economics quiet-print families the bot quotes on a top-N-by-ROI
group walk, quote-to-completion, with daily over-cap openings. This email is
that book's own scorecard, separate from the whole-account digest.

One row per currently-quoted opportunistic EVENT:
  EARN EST   the bot's period-to-date accrued reward estimate (what we
             expect Kalshi to credit at the program's period end)
  P&L        trading P&L on the event's markets (realized + settlement +
             open-book MTM) over the digest's attribution window — the cost
             of holding the inventory that earns the reward
  NET        P&L + EARN EST — the position's true economics

Numbers come from the SAME validated path as the whole-account digest: this
script imports send_imm_digest and calls its pnl_windows() / own_book() /
credit-ledger helpers, so a row here can never disagree with the digest.
EARN EST is the bot's estimator (accrual basis); the footer carries the
actual Kalshi-CREDITED total on opportunistic events from the recon ledger
(the only actual-money figure — it lands 1-2 days after each period ends).

STRICTLY READ-ONLY. Scheduled daily 7:25 AM ET ("KL imm opportunistic"),
after the 7:10 digest and 7:20 quote-gaps. --test sends now ignoring the
sent-marker; --dry / --print build and print only.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

# Importing send_imm_digest applies the live launcher env (its $ProbeEnv
# mirror) and the ALERT_EMAIL_* registry fallback at import, BEFORE
# incentive_mm's config is read — same ordering the digest itself relies on.
import send_imm_digest as sd
import incentive_mm as imm
from incentive_mm import log
from send_imm_digest import (TD, TDL, _f, _event_of, _short_event, _pnl_span,
                             load_json, own_book, fetch_own_fills,
                             current_mids, pnl_windows, status_summary,
                             load_credit_ledger, STATE_PATH, STATUS_PATH,
                             FILL_LOOKBACK_HOURS)

# Compact family labels; anything unmatched falls back to the Kalshi event
# title (fetched + cached below), so new Carbon Arc self-extensions read
# fine without a code change here.
_LABEL = {
    "KXSPRLVL": "US Strategic Petroleum Reserve level",
    "KXCBDECISIONNZ": "RBNZ rate decision",
    "KXCBDISRAEL": "Bank of Israel rate decision",
    "KXVENEZCRUDE": "Venezuela crude output",
    "KXAAAGASMINM": "Monthly low US gas price",
    "KXAAAGASMAXM": "Monthly high US gas price",
    "KXBRAZILGDP": "Brazil GDP growth",
    "KXJOLTSOPEN": "US job openings (JOLTS)",
    "KXDATACENTCON": "US data-center construction spend",
    "KXWENBACONATOR": "Wendy's Baconator price",
    "KXTBCRUNCHWRAP": "Taco Bell Crunchwrap price",
    "KXTXOIL": "Texas crude oil production",
    "KXVAPORTTEU": "Port of Virginia container volume",
    "KXDKS": "Dick's quarterly KPI",
    "KXZM": "Zoom quarterly KPI",
    "KXURBN": "Urban Outfitters KPI",
    "KXLOW": "Lowe's KPI",
    "KXDG": "Dollar General KPI",
    "KXAFRM": "Affirm KPI",
    "KXBBY": "Best Buy KPI",
    "KXWSM": "Williams-Sonoma KPI",
    "KXOKTA": "Okta KPI",
    "KXDRPEPPERPOS": "Dr Pepper point-of-sale growth",
    "KXAMZNCC": "Amazon credit-card spend",
}
_ADS_SUFFIX = "ADS"
_title_cache: dict = {}


def event_label(client, event_ticker: str) -> str:
    series = event_ticker.split("-")[0]
    if series in _LABEL:
        return _LABEL[series]
    if series.endswith(_ADS_SUFFIX):
        return "Ad spend (Carbon Arc)"
    if event_ticker in _title_cache:
        return _title_cache[event_ticker]
    title = ""
    try:
        e = (client.get(f"/events/{event_ticker}") or {}).get("event") or {}
        title = str(e.get("title") or e.get("sub_title") or "").strip()
    except Exception:
        pass
    title = title if len(title) <= 42 else title[:39] + "..."
    _title_cache[event_ticker] = title or series
    return _title_cache[event_ticker]


def build_report(now_utc):
    """(text, html, subject)."""
    client = sd.build_client()
    # Pick up task-appended Carbon Arc self-extensions so the opportunistic
    # universe here matches what the live bot quotes.
    imm.load_finecon_extra_series()
    fin = imm.FINECON_SERIES

    status = load_json(STATUS_PATH)
    state = load_json(STATE_PATH)
    ss = status_summary(status)
    our_ids = set(state.get("our_order_ids") or {})

    fills = fetch_own_fills(client, our_ids, FILL_LOOKBACK_HOURS)
    pos, avg = own_book(state)
    touched = {f.get("ticker", "") for f in fills} | set(pos)
    mids, results = current_mids(client, touched)
    # pnl_windows only to keep this email's basis identical to the digest's
    # (and to book the past-day per-event realized/settlement).
    w = pnl_windows(client, state, our_ids, fills, mids, results,
                    ss["reward_lifetime"])
    day_ev = w["day"]["events"]

    selected = set(state.get("selected_tickers") or [])
    accrued = state.get("accrued_est") or {}

    def _is_fin(t):
        return t.split("-")[0] in fin

    members = [t for t in selected if _is_fin(t)]
    by_event: dict = {}
    for t in members:
        by_event.setdefault(_event_of(t), []).append(t)

    rows = []
    for ev, tickers in by_event.items():
        earn = sum(_f(accrued.get(t)) for t in tickers)
        # P&L per event: open-book MTM on held inventory (this book almost
        # never sells, so MTM is the P&L; the sum over all held markets
        # equals the digest's lifetime unrealized) + any realized/settlement
        # booked for the event in the past-day window.
        mtm = 0.0
        for t in tickers:
            p = _f(pos.get(t))
            m = mids.get(t)
            if abs(p) > 1e-9 and m is not None:
                mtm += p * (m - _f(avg.get(t))) / 100.0
        de = day_ev.get(ev) or {}
        pnl = mtm + _f(de.get("realized")) + _f(de.get("settle"))
        netpos = sum(_f(pos.get(t)) for t in tickers)
        rows.append({
            "event": ev, "label": event_label(client, ev),
            "mkts": len(tickers), "earn": earn, "pnl": pnl,
            "net": pnl + earn, "pos": netpos})
    rows.sort(key=lambda r: -r["net"])

    tot_earn = sum(r["earn"] for r in rows)
    tot_pnl = sum(r["pnl"] for r in rows)
    tot_net = tot_earn + tot_pnl
    tot_mkts = sum(r["mkts"] for r in rows)

    # Actual Kalshi money on opportunistic events (footer reconciliation).
    ledger, _calib = load_credit_ledger()
    cred_life = sum(a for _d, ev, a in ledger if _is_fin(ev))

    top_n = getattr(imm, "FINECON_TOP_N", 0)
    openings_cap = getattr(imm, "FINECON_DAILY_OPENINGS", 0)
    today_et = now_utc.astimezone(imm.ET).date()
    used = (int(_f(state.get("finecon_admits_today")))
            if state.get("finecon_admit_day") == today_et.isoformat() else 0)

    subject = (f"Opportunistic IMM {today_et} — est ${tot_earn:,.0f}/period, "
               f"net ${tot_net:+,.0f}")

    # ---- plain text ---------------------------------------------------------
    L = [f"Opportunistic IMM — {today_et}", ""]
    L.append(f"{len(rows)} events quoted / {tot_mkts} markets "
             f"({len(members)}/{top_n} slots, +{used}/{openings_cap} daily "
             f"openings used).")
    L.append(f"Est reward this period ${tot_earn:,.2f}  |  trading P&L "
             f"${tot_pnl:+,.2f}  |  net ${tot_net:+,.2f}.")
    L.append("")
    if rows:
        L.append(f"{'EVENT':<26}{'WHAT IT IS':<34}{'MKTS':>5}{'EARN EST$':>11}"
                 f"{'P&L$':>10}{'NET$':>10}")
        for r in rows:
            L.append(f"{_short_event(r['event'])[:25]:<26}{r['label'][:33]:<34}"
                     f"{r['mkts']:>5}{r['earn']:>11.2f}{r['pnl']:>+10.2f}"
                     f"{r['net']:>+10.2f}")
        L.append(f"{'TOTAL':<26}{'':<34}{tot_mkts:>5}{tot_earn:>11.2f}"
                 f"{tot_pnl:>+10.2f}{tot_net:>+10.2f}")
    else:
        L.append("No opportunistic events quoted right now.")
    L.append("")
    L.append(f"Kalshi-credited on opportunistic events to date: "
             f"${cred_life:,.2f} (actual money; lands 1-2d after each period "
             f"ends).")
    L.append("")
    L.append("EARN EST = bot estimator, accrual basis (period-to-date). P&L = "
             "trading only (realized + settlement + open-book MTM), same "
             f"windowed attribution as the digest ({FILL_LOOKBACK_HOURS}h). "
             "NET = P&L + EARN EST.")
    text = "\n".join(L)

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;'
         'color:#222">']
    h.append(f'<div style="font-size:17px;font-weight:600">Opportunistic IMM'
             f' <span style="color:#888;font-weight:400">— {today_et}</span>'
             f'</div>')
    h.append(f'<div style="font-size:24px;font-weight:800;margin:8px 0 2px">'
             f'Net {_pnl_span(tot_net)}'
             f'<span style="font-size:13px;font-weight:400;color:#999">'
             f' &nbsp;= trading {tot_pnl:+,.2f} + est reward {tot_earn:,.2f}'
             f'</span></div>')
    h.append(f'<div style="color:#555;margin-bottom:10px">'
             f'<b>{len(rows)}</b> events quoted &nbsp;·&nbsp; '
             f'<b>{tot_mkts}</b> markets &nbsp;·&nbsp; '
             f'{len(members)}/{top_n} slots &nbsp;·&nbsp; '
             f'+{used}/{openings_cap} daily openings used</div>')
    if rows:
        h.append('<table style="border-collapse:collapse;margin:6px 0">')
        h.append(f'<tr style="background:#f0f0f0;font-weight:600">'
                 f'<td style="{TDL}">EVENT</td><td style="{TDL}">WHAT IT IS</td>'
                 f'<td style="{TD}">MKTS</td><td style="{TD}">EARN EST$</td>'
                 f'<td style="{TD}">P&amp;L$</td><td style="{TD}">NET$</td></tr>')
        for i, r in enumerate(rows):
            bg = "#fafafa" if i % 2 else "#fff"
            h.append(f'<tr style="background:{bg}">'
                     f'<td style="{TDL}"><b>{_short_event(r["event"])}</b></td>'
                     f'<td style="{TDL}">{r["label"]}</td>'
                     f'<td style="{TD}">{r["mkts"]}</td>'
                     f'<td style="{TD}">{r["earn"]:,.2f}</td>'
                     f'<td style="{TD}">{_pnl_span(r["pnl"])}</td>'
                     f'<td style="{TD};font-weight:700">{_pnl_span(r["net"])}</td>'
                     f'</tr>')
        h.append(f'<tr style="background:#f0f0f0;font-weight:700">'
                 f'<td style="{TDL}">TOTAL</td><td style="{TDL}"></td>'
                 f'<td style="{TD}">{tot_mkts}</td>'
                 f'<td style="{TD}">{tot_earn:,.2f}</td>'
                 f'<td style="{TD}">{_pnl_span(tot_pnl)}</td>'
                 f'<td style="{TD}">{_pnl_span(tot_net)}</td></tr>')
        h.append('</table>')
    else:
        h.append('<div>No opportunistic events quoted right now.</div>')
    h.append(f'<div style="color:#555;font-size:13px;margin-top:8px">'
             f'Kalshi-credited on opportunistic events to date: '
             f'<b>${cred_life:,.2f}</b> <span style="color:#999">(actual '
             f'money; lands 1&ndash;2d after each period ends)</span></div>')
    h.append(f'<div style="color:#999;font-size:11px;margin-top:10px">'
             f'EARN EST = bot estimator, accrual basis (period-to-date). '
             f'P&amp;L = trading only (realized + settlement + open-book MTM), '
             f'same windowed attribution as the digest ({FILL_LOOKBACK_HOURS}h). '
             f'NET = P&amp;L + EARN EST.</div>')
    h.append('</div>')
    return text, "".join(h), subject


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="alias for --dry")
    args = ap.parse_args(argv)
    dry = args.dry or args.print_only

    now_utc = datetime.now(timezone.utc)
    today_ct = now_utc.astimezone(imm.CT).date()
    marker = os.path.join(imm.STATUS_DIR,
                          f"opportunistic_imm_sent_{today_ct}.marker")
    if not (args.test or dry) and os.path.exists(marker):
        log(f"opportunistic email already sent for {today_ct}; exiting")
        return 0

    attempts = 1 if (args.test or dry) else 8
    text = html = subject = None
    for attempt in range(1, attempts + 1):
        try:
            text, html, subject = build_report(now_utc)
            break
        except Exception as e:
            log(f"opportunistic build attempt {attempt}/{attempts} failed: {e!r}")
            if attempt == attempts:
                log("giving up for today")
                return 1
            time.sleep(300)
    log("opportunistic body:\n" + text)
    if dry:
        return 0

    alerter = imm.Alerter("IMM-OPP", live=True)
    if not alerter.enabled:
        log("cannot send opportunistic email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject, html=html)
        if ok:
            break
        log(f"opportunistic send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"opportunistic send: {'ok' if ok else 'FAILED'}")
    if ok and not args.test:
        with open(marker, "w") as f:
            f.write(now_utc.isoformat())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
