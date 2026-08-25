# -*- coding: utf-8 -*-
"""Render the KXLOW public-tape bot dashboard as self-contained HTML.

Reads analysis/kxlow/output/dashboard_data.json (from analyze_public.py),
writes analysis/kxlow/output/kxlow_public_dashboard.html. Inline SVG only —
no external scripts, safe for the Artifacts CSP.
"""

import html
import json
import math
import os
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
D = json.load(open(os.path.join(OUT, "dashboard_data.json")))
E = html.escape


def money(v, signed=True, dec=0):
    if v is None:
        return "—"
    s = "−" if v < 0 else ("+" if signed else "")
    return f"{s}${abs(v):,.{dec}f}"


def nice_ticks(lo, hi, n=4):
    if hi <= lo:
        hi = lo + 1
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / n))
    for m in (1, 2, 2.5, 5, 10, 20):
        if span / (step * m) <= n:
            step *= m
            break
    t = math.floor(lo / step) * step
    out = []
    while t <= hi + 1e-9:
        if t >= lo - 1e-9:
            out.append(round(t, 6))
        t += step
    return out


def bar_path(x, y_base, y_val, w, r=3):
    """Rounded at the data end, square at the baseline."""
    r = max(0.0, min(r, w / 2, abs(y_val - y_base)))
    if y_val <= y_base:
        return (f"M{x:.1f},{y_base:.1f} L{x:.1f},{y_val+r:.1f} "
                f"Q{x:.1f},{y_val:.1f} {x+r:.1f},{y_val:.1f} L{x+w-r:.1f},{y_val:.1f} "
                f"Q{x+w:.1f},{y_val:.1f} {x+w:.1f},{y_val+r:.1f} L{x+w:.1f},{y_base:.1f} Z")
    return (f"M{x:.1f},{y_base:.1f} L{x:.1f},{y_val-r:.1f} "
            f"Q{x:.1f},{y_val:.1f} {x+r:.1f},{y_val:.1f} L{x+w-r:.1f},{y_val:.1f} "
            f"Q{x+w:.1f},{y_val:.1f} {x+w:.1f},{y_val-r:.1f} L{x+w:.1f},{y_base:.1f} Z")


def parse_d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def dspan():
    d0, d1 = parse_d(D["era"][0]), parse_d(D["era"][1])
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def fmt_day(dd):
    return dd.strftime("%b %d").replace(" 0", " ")


# --------------------------------------------------------------- over time
def daily_section():
    daily = D["daily"]
    days = dspan()
    by = {r["date"]: r for r in daily}
    W, H1, H2, ML, MR, MT = 960, 200, 170, 52, 16, 10
    AX = 24
    plot_w = W - ML - MR
    slot = plot_w / len(days)
    bw = min(24.0, slot - 2)

    pnls = [by.get(str(dd), {}).get("pnl", 0) for dd in days]
    lo = min(min(pnls), 0.0)
    hi = max(max(pnls), 0.0)
    pad = (hi - lo) * 0.12 + 0.5
    lo, hi = lo - pad, hi + pad
    y = lambda v: MT + (hi - v) / (hi - lo) * (H1 - MT - 8)
    xs = lambda i: ML + i * slot + (slot - bw) / 2

    g1, xt, bars = [], [], []
    for t in nice_ticks(lo, hi, 4):
        g1.append(f'<line x1="{ML}" x2="{W-MR}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                  f'<text x="{ML-7}" y="{y(t)+3.5:.1f}" class="tick" text-anchor="end">{money(t, signed=False)}</text>')
    g1.append(f'<line x1="{ML}" x2="{W-MR}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="axisline"/>')
    for i, dd in enumerate(days):
        if (dd.day in (1, 5, 10, 15, 20) and i < len(days) - 2) or i in (0, len(days) - 1):
            xt.append(f'<text x="{xs(i)+bw/2:.1f}" y="{H1+15}" class="tick" text-anchor="middle">{fmt_day(dd)}</text>')
        r = by.get(str(dd))
        if not r:
            continue
        cls = "pos" if r["pnl"] >= 0 else "neg"
        tip = (f"{dd.strftime('%a')} {fmt_day(dd)} — {money(r['pnl'], dec=2)} on "
               f"{r['contracts']} contracts ({money(r['cost'], signed=False)} deployed)")
        bars.append(f'<path d="{bar_path(xs(i), y(0), y(r["pnl"]), bw)}" class="{cls} hit" '
                    f'tabindex="0" data-tip="{E(tip)}"/>')

    cum, running = {}, 0.0
    for dd in days:
        r = by.get(str(dd))
        if r:
            running = r["cum"]
        cum[str(dd)] = running
    clo = min(min(cum.values()), 0.0)
    chi = max(max(cum.values()), 0.0)
    cpad = (chi - clo) * 0.14 + 0.5
    clo, chi = clo - cpad, chi + cpad
    y2 = lambda v: MT + (chi - v) / (chi - clo) * (H2 - MT - 8)
    pts = [(ML + (i + 0.5) * slot, y2(cum[str(dd)])) for i, dd in enumerate(days)]
    line = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area = line + f" L{pts[-1][0]:.1f},{y2(0):.1f} L{pts[0][0]:.1f},{y2(0):.1f} Z"
    g2 = []
    for t in nice_ticks(clo, chi, 3):
        g2.append(f'<line x1="{ML}" x2="{W-MR}" y1="{y2(t):.1f}" y2="{y2(t):.1f}" class="grid"/>'
                  f'<text x="{ML-7}" y="{y2(t)+3.5:.1f}" class="tick" text-anchor="end">{money(t, signed=False)}</text>')
    g2.append(f'<line x1="{ML}" x2="{W-MR}" y1="{y2(0):.1f}" y2="{y2(0):.1f}" class="axisline"/>')
    end_v = cum[str(days[-1])]
    endlab = (f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="4.5" class="linedot"/>'
              f'<text x="{pts[-1][0]-9:.1f}" y="{pts[-1][1]-10:.1f}" class="endlab" text-anchor="end">{money(end_v)}</text>')
    xt2 = [f'<text x="{ML + (i+0.5)*slot:.1f}" y="{H2+15}" class="tick" text-anchor="middle">{fmt_day(dd)}</text>'
           for i, dd in enumerate(days) if (dd.day in (1, 5, 10, 15, 20) and i < len(days) - 2) or i in (0, len(days) - 1)]

    rows = "".join(f"<tr><td>{r['date']}</td><td>{r['contracts']}</td>"
                   f"<td>{money(r['cost'], signed=False, dec=2)}</td><td>{money(r['pnl'], dec=2)}</td>"
                   f"<td>{money(r['cum'], dec=2)}</td></tr>" for r in daily)
    return f"""
<section class="card">
  <header><h2>Over time</h2>
  <p class="sub">Daily estimated gross P&amp;L of tape fills matching the bot's signature (strict filter), and the cumulative curve. Marked at settlement.</p></header>
  <svg viewBox="0 0 {W} {H1+AX}" role="img" aria-label="Daily estimated P&L">{''.join(g1)}{''.join(xt)}{''.join(bars)}</svg>
  <p class="chartlab">Cumulative</p>
  <svg viewBox="0 0 {W} {H2+AX}" role="img" aria-label="Cumulative estimated P&L">{''.join(g2)}<path d="{area}" class="areawash"/><path d="{line}" class="cumline"/>{endlab}{''.join(xt2)}</svg>
  <details><summary>Table view</summary><div class="tblwrap"><table>
  <thead><tr><th>Date</th><th>Contracts</th><th>Deployed</th><th>Est. P&amp;L</th><th>Cumulative</th></tr></thead>
  <tbody>{rows}</tbody></table></div></details>
</section>"""


# ---------------------------------------------------------------- by city
def city_section():
    bc = sorted(D["by_city"], key=lambda r: r["pnl_adj"])
    tail = {r["city"]: r["tail_day_share"] for r in D.get("tail_share", [])}
    W, RH, ML, MR = 960, 34, 132, 96
    H = RH * len(bc) + 14
    vlo = min(min(r["pnl_adj"] for r in bc), 0.0)
    vhi = max(max(r["pnl_adj"] for r in bc), 0.0)
    pad = (vhi - vlo) * 0.08 + 0.5
    vlo, vhi = vlo - pad, vhi + pad
    x = lambda v: ML + (v - vlo) / (vhi - vlo) * (W - ML - MR)
    svg = [f'<line x1="{x(0):.1f}" x2="{x(0):.1f}" y1="4" y2="{H-6}" class="axisline"/>']
    for i, r in enumerate(bc):
        yc = 8 + i * RH
        bh = 22
        x0, x1 = x(0), x(r["pnl_adj"])
        left, wid = (x1, x0 - x1) if x1 < x0 else (x0, x1 - x0)
        cls = "pos" if r["pnl_adj"] >= 0 else "neg"
        tip = (f"{r['city']}: adj {money(r['pnl_adj'], dec=2)} (strict {money(r['pnl'], dec=2)}), "
               f"{r['wins']}/{r['mkts']} markets won, ~{r['bot_share']*100:.0f}% attributable")
        lab_x, anch = (x1 + 7, "start") if r["pnl_adj"] >= 0 else (x1 - 7, "end")
        svg.append(
            f'<text x="{ML-9}" y="{yc+bh/2+4}" class="citylab" text-anchor="end">{E(r["city"])}</text>'
            f'<rect x="{left:.1f}" y="{yc}" width="{max(wid,1.2):.1f}" height="{bh}" rx="3" '
            f'class="{cls} hit" tabindex="0" data-tip="{E(tip)}"/>'
            f'<text x="{lab_x:.1f}" y="{yc+bh/2+4}" class="vlab" text-anchor="{anch}">{money(r["pnl_adj"])}</text>')
    trs = "".join(
        f"<tr><td>{E(r['city'])}</td><td>{money(r['pnl_adj'], dec=2)}</td><td>{money(r['pnl'], dec=2)}</td>"
        f"<td>{r['bot_share']*100:.0f}%</td><td>{r['wins']}/{r['mkts']}</td><td>{r['win_rate']*100:.0f}%</td>"
        f"<td>{r['contracts']}</td><td>{r['avg_no']:.0f}¢</td>"
        f"<td>{tail.get(r['city'], 0)*100:.0f}%</td></tr>"
        for r in sorted(bc, key=lambda q: -q["pnl_adj"]))
    return f"""
<section class="card">
  <header><h2>By city</h2>
  <p class="sub">Attribution-adjusted estimated P&amp;L (strict estimate × per-city bot share measured from the pre-live and Thursday-pause controls). Hover for the strict figure.</p></header>
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="Estimated P&L by city">{''.join(svg)}</svg>
  <details open><summary>City detail</summary><div class="tblwrap"><table>
  <thead><tr><th>City</th><th>Adj. P&amp;L</th><th>Strict</th><th>Bot share</th><th>Mkts won</th><th>Win rate</th><th>Contracts</th><th>Avg NO</th><th>Tail-day share</th></tr></thead>
  <tbody>{trs}</tbody></table></div>
  <p class="fine">Tail-day share = how often the actual low escaped all four listed buckets (a cold snap or warm surprise) — the days a bucket NO-ladder cashes everything. Houston and New Orleans have never had one since go-live; Boston and Las Vegas live on them.</p>
  </details>
</section>"""


# ------------------------------------------------------------------ heat
def heat_section():
    heat = D["heat"]
    days = dspan()
    order = [r["city"] for r in sorted(D["by_city"], key=lambda r: -r["contracts"])]
    hm = {(r["city"], r["date"]): r["n"] for r in heat}
    mx = max(r["n"] for r in heat)
    CW, CH, ML, MT = 23, 20, 132, 28
    W = ML + CW * len(days) + 10
    H = MT + CH * len(order) + 8
    cells = []
    for j, dd in enumerate(days):
        if (dd.day in (1, 5, 10, 15, 20) and j < len(days) - 2) or j in (0, len(days) - 1):
            cells.append(f'<text x="{ML+j*CW+CW/2}" y="{MT-9}" class="tick" text-anchor="middle">{fmt_day(dd)}</text>')
    for i, c in enumerate(order):
        cells.append(f'<text x="{ML-9}" y="{MT+i*CH+CH/2+4}" class="citylab" text-anchor="end">{E(c)}</text>')
        for j, dd in enumerate(days):
            n = hm.get((c, str(dd)), 0)
            q = 0 if n == 0 else 1 + min(5, int(5 * (n / mx) ** 0.5))
            tip = f"{c} — {fmt_day(dd)}: {n:.0f} est. contracts"
            cells.append(f'<rect x="{ML+j*CW}" y="{MT+i*CH}" width="{CW-2}" height="{CH-2}" rx="2" '
                         f'class="q{q} hit" tabindex="0" data-tip="{E(tip)}"/>')
    legend = "".join(f'<span class="lg q{k}"></span>' for k in range(7))
    head_cells = "".join(f"<th>{fmt_day(dd)}</th>" for dd in days)
    body_rows = "".join(
        "<tr><td>" + E(c) + "</td>" +
        "".join(f"<td>{hm.get((c, str(dd)), 0):.0f}</td>" for dd in days) + "</tr>"
        for c in order)
    return f"""
<section class="card">
  <header><h2>Where the fills happen</h2>
  <p class="sub">Estimated contracts per city and day (strict filter). Flow concentrates in the Texas and Southeast series; several cities barely trade.</p></header>
  <div class="hscroll"><svg viewBox="0 0 {W} {H}" style="min-width:{W*0.85:.0f}px" role="img" aria-label="Fill heatmap by city and day">{''.join(cells)}</svg></div>
  <p class="legendrow">0 {legend} {mx:.0f} contracts/day</p>
  <details><summary>Table view</summary><div class="tblwrap"><table>
  <thead><tr><th>City</th>{head_cells}</tr></thead><tbody>{body_rows}</tbody></table></div></details>
</section>"""


# --------------------------------------------------------------- edge/session
def edge_section():
    pe = [r for r in D["price_edge"] if (r["contracts"] or 0) > 0]
    sess = D["sessions"]
    W, H, ML, MR, MT, MB = 470, 320, 56, 18, 16, 46
    x = lambda p: ML + p * (W - ML - MR)
    y = lambda p: MT + (1 - p) * (H - MT - MB)
    g = []
    for t in (0, .25, .5, .75, 1):
        g.append(f'<line x1="{ML}" x2="{W-MR}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                 f'<text x="{ML-7}" y="{y(t)+3.5:.1f}" class="tick" text-anchor="end">{t*100:.0f}%</text>'
                 f'<text x="{x(t):.1f}" y="{H-MB+16}" class="tick" text-anchor="middle">{t*100:.0f}%</text>')
    g.append(f'<line x1="{x(0):.1f}" y1="{y(0):.1f}" x2="{x(1):.1f}" y2="{y(1):.1f}" class="ref"/>')
    g.append(f'<text x="{x(0.63):.1f}" y="{y(0.70):.1f}" class="reflab" '
             f'transform="rotate(-42 {x(0.63):.1f} {y(0.70):.1f})">break-even: realized = implied</text>')
    for r in pe:
        imp = 1 - r["avg_no"] / 100
        px, py = x(imp), y(r["win_share"])
        tip = (f"NO at {E(str(r['pbin']))}¢ — implied win {imp*100:.0f}%, realized {r['win_share']*100:.0f}% "
               f"({r['contracts']:.0f} contracts, {money(r['pnl'], dec=0)})")
        g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7" class="dot hit" tabindex="0" data-tip="{E(tip)}"/>')
    g.append(f'<text x="{(ML+W-MR)/2:.1f}" y="{H-8}" class="axis" text-anchor="middle">implied win probability at fill (1 − NO price)</text>')
    g.append(f'<text x="14" y="{(MT+H-MB)/2:.1f}" class="axis" text-anchor="middle" transform="rotate(-90 14 {(MT+H-MB)/2:.1f})">realized win share</text>')

    tiles = ""
    for s in sess:
        roi = "—" if s.get("roi") is None else f"{s['roi']*100:+.0f}%"
        cls = "goodtxt" if (s.get("pnl") or 0) >= 0 else "badtxt"
        when = "orders rest 20:17 ET → local midnight" if s["session"] == "evening" else "orders rest ~02:2x → 02:59 local"
        tiles += f"""
    <div class="tile">
      <div class="tlabel">{E(str(s['session']).title())} fills</div>
      <div class="tvalue {cls}">{money(s['pnl'], dec=0)}</div>
      <div class="tsub">ROI {roi} · {s['contracts']:.0f} contracts · {money(s['cost'], signed=False)} deployed</div>
      <div class="tsub2">{when}</div>
    </div>"""
    rows = "".join(f"<tr><td>{E(str(r['pbin']))}¢</td><td>{r['contracts']:.0f}</td><td>{100-r['avg_no']:.0f}%</td>"
                   f"<td>{r['win_share']*100:.0f}%</td><td>{money(r['pnl'], dec=2)}</td></tr>" for r in pe)
    return f"""
<section class="card">
  <header><h2>The edge check</h2>
  <p class="sub">Every price bucket fills below the break-even line: fills win less often than their price implied — the signature of adverse selection, not bad luck. Evening quotes (resting through the 00Z model cycle) bleed faster than day-of quotes.</p></header>
  <div class="cols">
    <svg viewBox="0 0 {W} {H}" role="img" aria-label="Implied versus realized win rate by price bin">{''.join(g)}</svg>
    <div class="tilecol">{tiles}</div>
  </div>
  <details><summary>Table view</summary><div class="tblwrap"><table>
  <thead><tr><th>NO price bin</th><th>Contracts</th><th>Implied win</th><th>Realized win</th><th>Est. P&amp;L</th></tr></thead>
  <tbody>{rows}</tbody></table></div></details>
</section>"""


# ------------------------------------------------------------- forecast
def forecast_section():
    fc = sorted(D["forecast"], key=lambda r: r["mae"])
    active = {r["city"] for r in D["by_city"]}
    fc = [r for r in fc if r["city"] in active] + [r for r in fc if r["city"] not in active]
    W, RH, ML, MID = 960, 30, 132, 520
    H = RH * len(fc) + 44
    mmax = max(r["mae"] for r in fc) * 1.12
    bx = lambda v: ML + v / mmax * (MID - ML - 64)
    brange = max(abs(min(r["bias"] for r in fc)), abs(max(r["bias"] for r in fc))) * 1.15
    dx = lambda v: MID + 40 + (v + brange) / (2 * brange) * (W - MID - 120)
    rows = []
    for i, r in enumerate(fc):
        yc = 34 + i * RH
        dim = "" if r["city"] in active else " dim"
        t1 = f"{r['city']}: MAE {r['mae']}°F (evening-before proxy)"
        t2 = f"{r['city']}: bias {r['bias']:+.1f}°F (forecast − actual low)"
        rows.append(
            f'<text x="{ML-9}" y="{yc+4}" class="citylab{dim}" text-anchor="end">{E(r["city"])}</text>'
            f'<rect x="{bx(0):.1f}" y="{yc-8}" width="{max(bx(r["mae"])-bx(0),1):.1f}" height="16" rx="3" '
            f'class="mae{dim} hit" tabindex="0" data-tip="{E(t1)}"/>'
            f'<text x="{bx(r["mae"])+6:.1f}" y="{yc+4}" class="vlab{dim}">{r["mae"]:.1f}°</text>'
            f'<circle cx="{dx(r["bias"]):.1f}" cy="{yc}" r="6" class="biasdot{dim} hit" tabindex="0" data-tip="{E(t2)}"/>')
    head = (f'<text x="{ML}" y="12" class="axis">Mean absolute error, °F</text>'
            f'<text x="{dx(0):.1f}" y="12" class="axis" text-anchor="middle">Bias: forecast − actual, °F</text>'
            f'<line x1="{dx(0):.1f}" x2="{dx(0):.1f}" y1="20" y2="{H-22}" class="axisline"/>'
            f'<text x="{dx(-brange*0.75):.1f}" y="{H-6}" class="tick" text-anchor="middle">← runs cold</text>'
            f'<text x="{dx(brange*0.75):.1f}" y="{H-6}" class="tick" text-anchor="middle">runs warm →</text>')
    return f"""
<section class="card">
  <header><h2>Forecast terrain</h2>
  <p class="sub">Evening-before Tmin from archived model runs (GFS + ECMWF blend at the settlement station's coordinates) vs the NWS CLI actual low, over the live era. A proxy for forecastability — not the bot's own NWS + Weather Underground ensemble. Dimmed cities have no KXLOW events listed.</p></header>
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="Forecast error and bias by city">{head}{''.join(rows)}</svg>
  <p class="fine">Grid-vs-station quirks inflate a few of these (LAX and Las Vegas GFS read a different microclimate); treat the ranking, not the absolute °F, as the signal. Oklahoma City's big warm bias is real in both models — actual lows keep undershooting forecasts there, which is exactly where the tape says the NO-ladder cashes.</p>
</section>"""


# --------------------------------------------------------------- method
def method_section():
    c = D["controls"]
    sens_rows = "".join(
        f"<tr><td>{E(r['variant'])}</td><td>{r['trades']:,}</td><td>{r['contracts']:,.0f}</td>"
        f"<td>{money(r['cost'], signed=False)}</td><td>{money(r['pnl'], dec=0)}</td></tr>"
        for r in D["sensitivity"])
    klass_pnl = sum(r["pnl"] for r in D["maker_class"])
    a = D["adjusted"]
    return f"""
<section class="card method">
  <header><h2>Method &amp; honesty box</h2></header>
  <p>Built from <strong>public data only</strong>: the anonymous Kalshi trade tape, market settlements, NWS CLI climate reports, and archived model forecasts. This session has no account key and no BigQuery access, so these are <strong>estimates of the bot's footprint, not the account ledger</strong>.</p>
  <p><strong>Attribution.</strong> A fill is counted as bot-like when it matches the bot's execution signature: a resting NO bid getting lifted, on a bucket market, at ≤ 50¢, in prints of ≤ 2 contracts (≤ 4 after the Jul 24 size raise), between 18:00 local the evening before and 03:00 local on the event day — the only window the post-only ladder is ever live.</p>
  <p><strong>Controls.</strong> Two windows where the bot provably wasn't there measure the look-alike flow: events before go-live ({c['pre_live_sig_contracts_per_day']}/day matched) and Thursday day-of windows, which Kalshi's maintenance pause blocks ({c['thu_dayof_contracts_per_day']}/day matched vs {c['nonthu_dayof_contracts_per_day']}/day when the bot runs). Both say <strong>≈ half the strict volume is other people</strong> — and that the look-alike flow roughly breaks even ({c['lookalike_roi_combined']*100:+.1f}% ROI), which pins the strict filter's losses on the bot's own quotes. Hence the range: gross ROI <strong>{a['roi_range'][0]*100:.0f}% (if look-alikes lose like the rest)</strong> to <strong>{a['roi_range'][1]*100:.0f}% (if look-alikes break even as measured)</strong>.</p>
  <table><thead><tr><th>Filter variant</th><th>Trades</th><th>Contracts</th><th>Cost</th><th>Est. P&amp;L</th></tr></thead>
  <tbody>{sens_rows}</tbody></table>
  <p class="fine">All resting-NO flow on KXLOW buckets over the era comes to {money(klass_pnl, dec=0)} — the entire maker side of this niche is being run over during the pre-dawn reveal window; the bot's ≤ 50¢ cap keeps it out of the worst of it. Settlement banding was cross-checked against CLI lows on 2,616 markets (99.9% agreement; one Seattle Jul 22 correction). Open, unsettled exposure at pull time: {money(D['open_exposure'], signed=False)}. Verified against the one committed run summary (Aug 14: Seattle + Phoenix day-of orders — the tape shows exactly those two cities' windows active, bot-size prints among larger makers'). For the exact ledger, run <code>weather-analysis-2</code> on the laptop, or give a cloud session Kalshi/BQ credentials.</p>
</section>"""


# ----------------------------------------------------------------- page
def build():
    T = D["totals"]
    a = D["adjusted"]
    c = D["controls"]
    era0, era1 = parse_d(D["era"][0]), parse_d(D["era"][1])
    tiles = [
        ("Est. gross P&L (strict)", money(T["pnl"], dec=0), "badtxt" if T["pnl"] < 0 else "goodtxt",
         f"attribution band {money(a['pnl'], dec=0)} to {money(a['pnl_pessimistic'], dec=0)}"),
        ("Gross ROI", f"{T['roi']*100:.0f}%", "badtxt" if T["roi"] < 0 else "goodtxt",
         f"to {a['roi_range'][1]*100:.0f}% if look-alikes break even"),
        ("Deployed", money(T["cost"], signed=False), "", f"{T['contracts']:,} contracts · {T['markets']} markets"),
        ("Market win rate", f"{T['win_rate_mkts']*100:.0f}%", "", "filled positions settling NO"),
        ("Fill cadence", f"{T['days_with_fills']}/{T['days_live']}", "", "days with fills since go-live"),
        ("Max drawdown", money(T["max_drawdown"], dec=0), "badtxt" if T["max_drawdown"] < 0 else "",
         "on the strict cumulative"),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="tlabel">{E(l)}</div><div class="tvalue {cl}">{E(v)}</div>'
        f'<div class="tsub">{E(s)}</div></div>' for l, v, cl, s in tiles)

    return f"""<title>The Overnight Book</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&display=swap">
<style>
:root {{
  color-scheme: light;
  --page:#f4f6f8; --surface:#fcfdfe; --ink:#101720; --ink2:#46505c; --muted:#85909c;
  --grid:#e5e8ec; --axis:#c6ccd4; --border:rgba(16,23,32,.10);
  --pos:#2a78d6; --neg:#e34948; --accent:#2a78d6;
  --goodtxt:#006300; --badtxt:#b3271e;
  --mae:#9ec5f4; --wash:rgba(42,120,214,.10); --refline:#c6ccd4;
  --q0:#edf0f3; --q1:#cde2fb; --q2:#9ec5f4; --q3:#6da7ec; --q4:#3987e5; --q5:#256abf; --q6:#184f95;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --page:#0c0f13; --surface:#151a20; --ink:#eef2f6; --ink2:#b4bcc7; --muted:#7d8794;
    --grid:#242b33; --axis:#3a434e; --border:rgba(255,255,255,.10);
    --pos:#3987e5; --neg:#e66767; --accent:#3987e5;
    --goodtxt:#0ca30c; --badtxt:#e66767;
    --mae:#1c5cab; --wash:rgba(57,135,229,.14); --refline:#3a434e;
    --q0:#1b2129; --q1:#173257; --q2:#1c477e; --q3:#2a5fa3; --q4:#3987e5; --q5:#6da7ec; --q6:#9ec5f4;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --page:#0c0f13; --surface:#151a20; --ink:#eef2f6; --ink2:#b4bcc7; --muted:#7d8794;
  --grid:#242b33; --axis:#3a434e; --border:rgba(255,255,255,.10);
  --pos:#3987e5; --neg:#e66767; --accent:#3987e5;
  --goodtxt:#0ca30c; --badtxt:#e66767;
  --mae:#1c5cab; --wash:rgba(57,135,229,.14); --refline:#3a434e;
  --q0:#1b2129; --q1:#173257; --q2:#1c477e; --q3:#2a5fa3; --q4:#3987e5; --q5:#6da7ec; --q6:#9ec5f4;
}}
* {{ box-sizing: border-box; }}
body {{
  margin:0; background:var(--page); color:var(--ink);
  font:15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}}
.wrap {{ max-width:1040px; margin:0 auto; padding:28px 20px 64px; }}
.masthead h1 {{
  font-family:"Archivo", system-ui, sans-serif; font-weight:700; letter-spacing:-.015em;
  font-size:clamp(26px,4vw,36px); margin:0 0 2px; text-wrap:balance;
}}
.masthead .kicker {{
  font-size:12px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted);
  margin:0 0 10px; font-weight:600;
}}
.masthead .deck {{ color:var(--ink2); max-width:72ch; margin:8px 0 0; }}
.chips {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }}
.chip {{
  font-size:12px; color:var(--ink2); border:1px solid var(--border); border-radius:999px;
  padding:3px 10px; background:var(--surface);
}}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0 6px; }}
.tile {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px 14px; }}
.tlabel {{ font-size:12px; color:var(--muted); }}
.tvalue {{ font-size:26px; font-weight:650; margin-top:2px; }}
.tsub, .tsub2 {{ font-size:12px; color:var(--ink2); margin-top:3px; }}
.tsub2 {{ color:var(--muted); }}
.goodtxt {{ color:var(--goodtxt); }} .badtxt {{ color:var(--badtxt); }}
section.card {{
  background:var(--surface); border:1px solid var(--border); border-radius:12px;
  padding:18px 20px 16px; margin-top:18px;
}}
section.card h2 {{
  font-family:"Archivo", system-ui, sans-serif; font-weight:600; font-size:19px; margin:0;
}}
.sub {{ color:var(--ink2); font-size:13.5px; margin:6px 0 12px; max-width:88ch; }}
.chartlab {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.1em; margin:14px 0 2px; }}
svg {{ width:100%; height:auto; display:block; }}
svg text {{ font-family:system-ui, -apple-system, "Segoe UI", sans-serif; fill:var(--ink2); }}
.grid {{ stroke:var(--grid); stroke-width:1; }}
.axisline {{ stroke:var(--axis); stroke-width:1; }}
.tick {{ font-size:10.5px; fill:var(--muted); font-variant-numeric:tabular-nums; }}
.axis {{ font-size:11px; fill:var(--muted); }}
.citylab {{ font-size:12px; fill:var(--ink2); }}
.vlab {{ font-size:11px; fill:var(--ink2); font-variant-numeric:tabular-nums; }}
.endlab {{ font-size:11.5px; font-weight:600; fill:var(--ink); }}
.pos {{ fill:var(--pos); }} .neg {{ fill:var(--neg); }}
.cumline {{ fill:none; stroke:var(--accent); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
.areawash {{ fill:var(--wash); stroke:none; }}
.linedot {{ fill:var(--accent); stroke:var(--surface); stroke-width:2; }}
.dot {{ fill:var(--accent); stroke:var(--surface); stroke-width:2; }}
.biasdot {{ fill:var(--pos); stroke:var(--surface); stroke-width:2; }}
.mae {{ fill:var(--mae); }}
.dim {{ opacity:.38; }}
.ref {{ stroke:var(--refline); stroke-width:1; }}
.reflab {{ font-size:10px; fill:var(--muted); }}
.q0{{fill:var(--q0);}} .q1{{fill:var(--q1);}} .q2{{fill:var(--q2);}} .q3{{fill:var(--q3);}}
.q4{{fill:var(--q4);}} .q5{{fill:var(--q5);}} .q6{{fill:var(--q6);}}
.legendrow {{ font-size:11px; color:var(--muted); display:flex; align-items:center; gap:4px; margin:8px 0 0; }}
.lg {{ width:16px; height:12px; border-radius:2px; display:inline-block; }}
.hscroll {{ overflow-x:auto; }}
.cols {{ display:grid; grid-template-columns: 1.4fr 1fr; gap:16px; align-items:start; }}
.tilecol {{ display:flex; flex-direction:column; gap:10px; }}
.tilecol .tile {{ border:1px solid var(--border); }}
@media (max-width:760px) {{ .cols {{ grid-template-columns:1fr; }} }}
details {{ margin-top:10px; }}
summary {{ cursor:pointer; font-size:13px; color:var(--ink2); }}
.tblwrap {{ overflow-x:auto; margin-top:8px; }}
table {{ border-collapse:collapse; font-size:13px; min-width:520px; }}
th, td {{ text-align:right; padding:5px 12px; border-bottom:1px solid var(--grid); font-variant-numeric:tabular-nums; white-space:nowrap; }}
th:first-child, td:first-child {{ text-align:left; }}
thead th {{ color:var(--muted); font-weight:600; font-size:12px; }}
.fine {{ font-size:12.5px; color:var(--muted); margin:10px 0 0; max-width:92ch; }}
.method p {{ max-width:92ch; color:var(--ink2); font-size:14px; }}
.method strong {{ color:var(--ink); }}
code {{ background:var(--page); border:1px solid var(--border); border-radius:4px; padding:1px 5px; font-size:12.5px; }}
.hit {{ cursor:default; }}
.hit:hover, .hit:focus {{ opacity:.82; outline:none; }}
.hit:focus-visible {{ outline:2px solid var(--accent); outline-offset:1px; }}
#tip {{
  position:fixed; pointer-events:none; background:var(--ink); color:var(--page);
  font-size:12px; line-height:1.4; padding:6px 9px; border-radius:6px; max-width:300px;
  opacity:0; transition:opacity .08s; z-index:10;
}}
@media (prefers-reduced-motion: reduce) {{ #tip, .hit {{ transition:none; }} }}
footer {{ color:var(--muted); font-size:12px; margin-top:26px; }}
</style>
<div class="wrap">
  <div class="masthead">
    <p class="kicker">KXLOW · daily-low maker bot · public-tape read</p>
    <h1>The Overnight Book</h1>
    <p class="deck">How the low-temperature bot has traded since going live on Jul 22 —
    reconstructed from the public tape, settlements, and NWS climate reports, with the
    bot's fills identified by its execution signature and honest error bars from two
    natural no-bot control windows.</p>
    <div class="chips">
      <span class="chip">{era0.strftime('%b %d')} – {era1.strftime('%b %d, %Y')} (settled era)</span>
      <span class="chip">13 active city series</span>
      <span class="chip">gross of nothing — maker pays no fee</span>
      <span class="chip">generated {E(D['generated'])}</span>
    </div>
  </div>
  <div class="tiles">{tile_html}</div>
  {daily_section()}
  {city_section()}
  {edge_section()}
  {heat_section()}
  {forecast_section()}
  {method_section()}
  <footer>Estimates from public data; not the account ledger. Sources: Kalshi public market API (markets, settlements, trade tape) · IEM NWS CLI reports · Open-Meteo previous-runs archive. Attribution bot share ≈ {c['bot_share']*100:.0f}% (Thursday-pause control).</footer>
</div>
<div id="tip" role="status"></div>
<script>
(function () {{
  var tip = document.getElementById('tip');
  function show(el) {{
    tip.textContent = el.getAttribute('data-tip') || '';
    tip.style.opacity = '1';
  }}
  function move(ev) {{
    var x = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    var y = Math.min(ev.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }}
  function hide() {{ tip.style.opacity = '0'; }}
  document.querySelectorAll('.hit').forEach(function (el) {{
    el.addEventListener('mouseenter', function () {{ show(el); }});
    el.addEventListener('mousemove', move);
    el.addEventListener('mouseleave', hide);
    el.addEventListener('focus', function () {{
      show(el);
      var r = el.getBoundingClientRect();
      tip.style.left = Math.min(r.left, window.innerWidth - 310) + 'px';
      tip.style.top = (r.bottom + 8) + 'px';
    }});
    el.addEventListener('blur', hide);
  }});
}})();
</script>
"""


if __name__ == "__main__":
    out_path = os.path.join(OUT, "kxlow_public_dashboard.html")
    page = build()
    with open(out_path, "w") as f:
        f.write(page)
    print(f"wrote {out_path} ({len(page):,} bytes)")
