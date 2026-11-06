#!/usr/bin/env python3
r"""combo_dash.py -- a small LOCAL dashboard for the combo maker pilot (Jack
2026-11-05: "build a simple dash i can use to track parlays").

    python combo_dash.py              # http://127.0.0.1:8772, refreshes every 15 s
    python combo_dash.py --port 8780

What it shows: the maker's status and settings, the combo shard's cash, today's
funnel (quotes -> accepted -> confirmed, and why it declined), the open tickets
(legs, our price vs fair, what each can lose), the settled ones with their P&L,
the fills Kalshi recorded (price, fee), the latest minute's RFQ funnel, and a
feed of the recent events.

READ-ONLY. It reads the maker's files in run-logs\combo-maker (maker_state.json
and combo_<UTC date>.jsonl), the shadow logger's last stat line
(run-logs\combo-rfq), and at most once a minute three Kalshi reads: the shard
balances, today's fills, and the open tickets' combo markets. It places nothing.
It binds 127.0.0.1 only.
"""
import argparse
import html
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MAKER_DIR = os.environ.get("COMBO_MAKER_DIR", os.path.join(HERE, "run-logs", "combo-maker"))
SHADOW_DIR = os.environ.get("COMBO_RFQ_DIR", os.path.join(HERE, "run-logs", "combo-rfq"))
PORT = 8772
REFRESH_S = 15
KALSHI_TTL_S = 60.0
RUNNING_S = 120.0          # a maker with no line this recent is shown as stopped
FEED_N = 40
KINDS = {"GAME": "win", "SPREAD": "spread", "TOTAL": "total"}


def _f(x, dflt=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return dflt


def et(ts) -> datetime:
    try:
        import pytz
        return datetime.fromtimestamp(ts, pytz.timezone("US/Eastern"))
    except ImportError:
        return datetime.fromtimestamp(ts - 5 * 3600, timezone.utc)


def et_midnight(ts: float) -> float:
    d = et(ts)
    return ts - (d.hour * 3600 + d.minute * 60 + d.second + d.microsecond / 1e6)


def leg_label(leg) -> str:
    """KXNBAGAME-26NOV05MIAGSW-GSW + yes -> 'NBA MIA-GSW: GSW win'."""
    tk, side = leg[0], leg[1]
    parts = tk.split("-")
    series = parts[0][2:] if parts[0].startswith("KX") else parts[0]
    kind = ""
    for suf, name in KINDS.items():
        if series.endswith(suf):
            series, kind = series[: -len(suf)], name
            break
    game = parts[1][7:] if len(parts) > 1 else ""
    pick = parts[-1] if len(parts) > 2 else ""
    if kind == "total":                      # YES on a total = over the line
        return f"{series} {game}: {'over' if side == 'yes' else 'under'} {pick}"
    s = f"{series} {game}: {pick} {kind}".strip()
    return s if side == "yes" else f"NOT ({s})"


# ---- the files ---------------------------------------------------------------

def read_events(maker_dir: str, now: float, days: int = 2) -> list:
    """The maker's log lines, oldest first, from the last `days` UTC days.
    Each gets "_ts": its own time ("t", or "at" on quotes and confirms), else
    the last stamped line's before it (accept / decline / err / executed lines
    carry none; the log is written in order)."""
    out, last = [], 0.0
    for d in range(days - 1, -1, -1):
        day = datetime.fromtimestamp(now - d * 86400, timezone.utc).strftime("%Y-%m-%d")
        p = os.path.join(maker_dir, f"combo_{day}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                own = _f(e.get("t")) or _f(e.get("at"))
                if own:
                    last = own
                e["_ts"] = own or last
                out.append(e)
    return out


def read_state(maker_dir: str) -> list:
    p = os.path.join(maker_dir, "maker_state.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("tickets") or []
    except (OSError, ValueError):
        return []


def shadow_pulse(shadow_dir: str, now: float):
    """The shadow logger's last minute stat (the whole market's combo flow),
    read from the tail of today's file."""
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    p = os.path.join(shadow_dir, f"combo_{day}.jsonl")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        if '"k":"stat"' in line:
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


# ---- Kalshi (cached, read-only) -----------------------------------------------

class KalshiReads:
    """Shard cash, today's combo fills and the open combos' markets, refreshed
    at most every KALSHI_TTL_S; a failed read keeps the last good one."""

    def __init__(self, client_factory=None, ttl: float = KALSHI_TTL_S):
        self.factory = client_factory
        self.ttl = ttl
        self.client = None
        self.at = 0.0
        self.data = {"cash": None, "fills": [], "markets": {}, "error": None, "at": None}
        self.lock = threading.Lock()

    def _client(self):
        if self.client is None:
            if self.factory is not None:
                self.client = self.factory()
            else:
                os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")
                import kalshi_subaccount as ks
                self.client = ks.client()
        return self.client

    def get(self, now: float, open_tickers, day_start: float) -> dict:
        with self.lock:
            if now - self.at < self.ttl:
                return self.data
            self.at = now
            try:
                c = self._client()
                bal = c.get("/portfolio/balance", {})
                cash = {int(r.get("exchange_index")): _f(r.get("balance"), 0.0)
                        for r in bal.get("balance_breakdown") or []}
                fj = c.get("/portfolio/fills", {"min_ts": int(day_start - 86400), "limit": 200})
                fills = [x for x in fj.get("fills") or []
                         if str(x.get("market_ticker") or x.get("ticker") or "").startswith("KXMVE")]
                mk = {}
                tks = sorted(set(open_tickers))
                for i in range(0, len(tks), 100):
                    mj = c.get("/markets", {"tickers": ",".join(tks[i:i + 100]), "limit": 1000})
                    for m in mj.get("markets") or []:
                        mk[m.get("ticker")] = m
                self.data = {"cash": cash.get(1), "fills": fills, "markets": mk, "error": None, "at": now}
            except Exception as e:
                self.data = dict(self.data, error=f"{type(e).__name__}: {str(e)[:160]}")
            return self.data


# ---- the snapshot ----------------------------------------------------------------

def snapshot(maker_dir: str, shadow_dir: str, now: float, kalshi=None) -> dict:
    events = read_events(maker_dir, now)
    tickets = read_state(maker_dir)
    day0 = et_midnight(now)
    starts = [e for e in events if e.get("k") == "start"]
    last_start = starts[-1] if starts else None
    after_start = events[events.index(last_start):] if last_start else []
    stopped = any(e.get("k") == "stop" for e in after_start)
    stats = [e for e in events if e.get("k") == "stat"]
    last_t = max((_ev_ts(e) for e in after_start), default=0.0)
    running = bool(last_start) and not stopped and now - last_t <= RUNNING_S
    today = [e for e in events if _ev_ts(e) >= day0]
    count = lambda k: sum(1 for e in today if e.get("k") == k)  # noqa: E731
    declines = {}
    for e in today:
        if e.get("k") == "decline":
            declines[e.get("why")] = declines.get(e.get("why"), 0) + 1
    open_t = [t for t in tickets if t.get("status") in ("confirmed", "executed")]
    settled = [t for t in tickets if t.get("status") == "settled"]
    k = None
    if kalshi is not None:
        k = kalshi.get(now, [t["mt"] for t in open_t], day0)
    fills = (k or {}).get("fills") or []
    return {
        "now": now, "running": running, "stopped": stopped,
        "live": bool(last_start and last_start.get("live")),
        "cfg": (last_start or {}).get("cfg") or {},
        "started": _f((last_start or {}).get("t")),
        "last_line_age": (now - last_t) if last_t else None,
        "funnel": {"quotes": count("quote"), "would": count("would_quote"), "accepted": count("accept"),
                   "confirmed": count("confirm"), "declined": count("decline"),
                   "executed": count("executed") - count("unfilled"), "unfilled": count("unfilled"),
                   "errors": count("err")},
        "declines": declines,
        "open": open_t, "settled": settled,
        "open_loss": sum(t["contracts"] * (1.0 - t["ask"]) for t in open_t),
        "open_premium": sum(t["contracts"] * t["ask"] for t in open_t),
        "settled_pnl": sum(_f(t.get("pnl"), 0.0) for t in settled),
        "wins": sum(1 for t in settled if t.get("result") == "no"),
        "losses": sum(1 for t in settled if t.get("result") == "yes"),
        "last_stat": stats[-1] if stats else None,
        "feed": [e for e in events if e.get("k") in ("quote", "would_quote", "accept", "confirm", "decline",
                                                       "executed", "filled", "unfilled", "settled", "err",
                                                       "position", "start",
                                                       "stop")][-FEED_N:][::-1],
        "kalshi": k, "fees": sum(_f(x.get("fee_cost"), 0.0) for x in fills), "fills": fills,
        "pulse": shadow_pulse(shadow_dir, now),
    }


def _ev_ts(e) -> float:
    v = _f(e.get("_ts"))
    if v:
        return v
    for key in ("t", "at"):
        v = _f(e.get(key))
        if v:
            return v
    return 0.0


# ---- the page ----------------------------------------------------------------------

def usd(x, d=2) -> str:
    if x is None:
        return "&ndash;"
    return ("&minus;" if x < 0 else "") + f"${abs(x):,.{d}f}"


def signed(x) -> str:
    if x is None:
        return "&ndash;"
    cls = "pos" if x > 0 else "neg" if x < 0 else ""
    return f'<span class="{cls}">{"+" if x > 0 else "&minus;" if x < 0 else ""}${abs(x):,.2f}</span>'


def tm(ts) -> str:
    return et(ts).strftime("%-I:%M:%S %p") if os.name != "nt" else et(ts).strftime("%I:%M:%S %p").lstrip("0")


def legs_html(legs) -> str:
    return "<br>".join(html.escape(leg_label(l)) for l in legs)


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--muted:#6b7280;--line:#e5e7eb;--pos:#15803d;--neg:#b91c1c;
--accent:#2563eb;--warn:#b45309;--chip:#eef2ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2a303b;
--pos:#4ade80;--neg:#f87171;--accent:#60a5fa;--warn:#fbbf24;--chip:#1e2533}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:22px 0 8px}
.top{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:700;font-size:12px;letter-spacing:.04em}
.live{background:#dcfce7;color:#166534}.dry{background:#e0e7ff;color:#3730a3}.off{background:#fee2e2;color:#991b1b}
@media (prefers-color-scheme:dark){.live{background:#14532d;color:#bbf7d0}.dry{background:#1e1b4b;color:#c7d2fe}.off{background:#450a0a;color:#fecaca}}
.muted{color:var(--muted)}.small{font-size:12px}.pos{color:var(--pos)}.neg{color:var(--neg)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card .v{font-size:22px;font-weight:700;margin-top:2px}.card .l{font-size:12px;color:var(--muted)}
.funnel{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:4px}
.funnel b{font-size:18px}.arrow{color:var(--muted)}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-size:11px;color:var(--muted);font-weight:600;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;white-space:nowrap}
.k{display:inline-block;min-width:74px;font-size:11px;font-weight:700;padding:1px 6px;border-radius:6px;background:var(--chip);text-align:center}
.k.confirm,.k.executed,.k.filled{color:var(--pos)}.k.decline,.k.err,.k.unfilled{color:var(--neg)}.k.accept{color:var(--accent)}
.empty{padding:14px;color:var(--muted)}
.chips span{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;margin:2px 4px 2px 0;font-size:12px}
footer{margin:20px 0 6px;color:var(--muted);font-size:12px}
"""


def render(s: dict) -> str:
    cfg = s["cfg"]
    status = ("live" if s["live"] else "dry") if s["running"] else "off"
    label = {"live": "LIVE", "dry": "DRY", "off": "STOPPED" if s["stopped"] else "NOT RUNNING"}[status]
    games = ", ".join(cfg.get("games") or []) or "any"
    mu, step = (_f(cfg.get('markup'), 0) or 0), (_f(cfg.get('markup_per_leg'), 0) or 0)
    setting = (f"markup {mu * 100:.3g}%" + (f" +{step * 100:.3g}%/leg over 2" if step else "")
               + f" &middot; {cfg.get('min_legs', '?')}&ndash;"
               f"{cfg.get('max_legs', '?')} legs &middot; games {html.escape(games)} &middot; "
               f"lead {cfg.get('min_lead_min', '?')} min &middot; "
               + (f"settles &le; {cfg.get('max_days_out'):g} days &middot; " if cfg.get("max_days_out") else "")
               + f"caps ${cfg.get('max_payout', '?')}/ticket, "
               f"${cfg.get('max_game_payout', '?')}/game, open loss ${cfg.get('max_open_loss', '?')}") if cfg else "no run yet"
    k = s.get("kalshi") or {}
    fn = s["funnel"]
    qn = fn["quotes"] or fn["would"]
    H = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<meta http-equiv='refresh' content='{REFRESH_S}'><title>Combo maker</title><style>{CSS}</style></head><body><main>"]
    H.append(f"<div class='top'><div><h1>Combo maker <span class='pill {status}'>{label}</span></h1>"
             f"<div class='muted small'>{setting}</div></div>"
             f"<div class='muted small'>as of {tm(s['now'])} ET &middot; refreshes every {REFRESH_S}s"
             + (f" &middot; last line {s['last_line_age']:.0f}s ago" if s.get("last_line_age") is not None else "")
             + "</div></div>")
    H.append("<div class='cards'>")
    H.append(f"<div class='card'><div class='l'>Combo shard cash</div><div class='v'>{usd(k.get('cash'))}</div>"
             f"<div class='l'>{'Kalshi ' + tm(k['at']) if k.get('at') else 'not read yet'}</div></div>")
    H.append(f"<div class='card'><div class='l'>Open tickets</div><div class='v'>{len(s['open'])}</div>"
             f"<div class='l'>can lose {usd(s['open_loss'])} &middot; premium {usd(s['open_premium'])}</div></div>")
    H.append(f"<div class='card'><div class='l'>Settled P&amp;L</div><div class='v'>{signed(s['settled_pnl'])}</div>"
             f"<div class='l'>{s['wins']} kept &middot; {s['losses']} paid out</div></div>")
    H.append(f"<div class='card'><div class='l'>Fees on combo fills</div><div class='v'>{usd(s['fees'])}</div>"
             f"<div class='l'>{len(s['fills'])} fills recorded by Kalshi</div></div>")
    H.append("</div>")
    H.append("<h2>Today</h2><div class='card'><div class='funnel'>"
             f"<b>{qn:,}</b> {'quotes sent' if fn['quotes'] else 'would quote (dry)'} <span class='arrow'>&rarr;</span> "
             f"<b>{fn['accepted']:,}</b> accepted <span class='arrow'>&rarr;</span> <b>{fn['confirmed']:,}</b> confirmed "
             f"<span class='arrow'>&rarr;</span> <b>{fn['executed']:,}</b> filled"
             + (f" ({fn['unfilled']} canceled by Kalshi, 0 filled)" if fn.get("unfilled") else "")
             + f"<span class='muted'>&nbsp;&middot; {fn['declined']} declined at the last look &middot; "
             f"{fn['errors']} errors</span></div>")
    if s["declines"]:
        H.append("<div class='chips small' style='margin-top:6px'>Declined because: " + "".join(
            f"<span>{html.escape(str(w))} {n}</span>" for w, n in sorted(s["declines"].items(), key=lambda kv: -kv[1])) + "</div>")
    ls = s.get("last_stat")
    if ls:
        top = sorted((ls.get("why") or {}).items(), key=lambda kv: -kv[1])[:6]
        H.append(f"<div class='small muted' style='margin-top:6px'>Last minute: {ls.get('rfq', 0):,} RFQs seen "
                 f"&rarr; {ls.get('pre_ok', 0):,} pass the leg filters &rarr; {ls.get('priced', 0):,} priced &rarr; "
                 f"{ls.get('quoted', ls.get('dry', 0)) or ls.get('dry', 0):,} quoted. Skipped: "
                 + ", ".join(f"{html.escape(w)} {n:,}" for w, n in top) + "</div>")
    p = s.get("pulse")
    if p and p.get("secs"):
        H.append(f"<div class='small muted'>The whole market, last minute: ~{p.get('rfq', 0) * (p.get('shard') or [50])[0] / p['secs']:,.0f} "
                 f"RFQs/s, {p.get('mve_prints', 0) / p['secs']:.0f} combo fills/s, "
                 f"{usd(p.get('mve_usd', 0) / p['secs'] * 60, 0)} of premium a minute.</div>")
    H.append("</div>")
    H.append(f"<h2>Open tickets ({len(s['open'])})</h2><div class='tw'>")
    if s["open"]:
        H.append("<table><tr><th>Confirmed</th><th>Legs (we sold the parlay)</th><th class='n'>Our price</th>"
                 "<th class='n'>Fair (quote &rarr; confirm)</th><th class='n'>Contracts</th><th class='n'>Premium</th>"
                 "<th class='n'>Can lose</th><th>Status</th></tr>")
        mk = k.get("markets") or {}
        for t in sorted(s["open"], key=lambda t: -t.get("at", 0)):
            m = mk.get(t["mt"]) or {}
            st = t["status"] + (f" &middot; Kalshi {html.escape(str(m.get('status')))}" if m else "")
            if m.get("result"):
                st += f" &middot; result {html.escape(str(m['result']).upper())}"
            fc = t.get("fair_confirm")
            H.append(f"<tr><td class='small'>{tm(t.get('at', 0))}</td><td class='small'>{legs_html(t['legs'])}</td>"
                     f"<td class='n'>{t['ask'] * 100:.1f}&cent;</td>"
                     f"<td class='n'>{t['fair'] * 100:.1f} &rarr; {(fc * 100 if fc else 0):.1f}&cent;</td>"
                     f"<td class='n'>{t['contracts']:,.1f}</td><td class='n'>{usd(t['contracts'] * t['ask'])}</td>"
                     f"<td class='n'>{usd(t['contracts'] * (1 - t['ask']))}</td><td class='small'>{st}</td></tr>")
        H.append("</table>")
    else:
        H.append("<div class='empty'>No open tickets.</div>")
    H.append(f"</div><h2>Settled ({len(s['settled'])})</h2><div class='tw'>")
    if s["settled"]:
        H.append("<table><tr><th>Confirmed</th><th>Legs</th><th class='n'>Our price</th><th class='n'>Contracts</th>"
                 "<th>Parlay</th><th class='n'>P&amp;L</th></tr>")
        for t in sorted(s["settled"], key=lambda t: -t.get("at", 0)):
            res = "hit (we paid)" if t.get("result") == "yes" else "missed (we kept it)"
            H.append(f"<tr><td class='small'>{tm(t.get('at', 0))}</td><td class='small'>{legs_html(t['legs'])}</td>"
                     f"<td class='n'>{t['ask'] * 100:.1f}&cent;</td><td class='n'>{t['contracts']:,.1f}</td>"
                     f"<td>{res}</td><td class='n'>{signed(_f(t.get('pnl')))}</td></tr>")
        H.append("</table>")
    else:
        H.append("<div class='empty'>Nothing settled yet.</div>")
    H.append("</div>")
    if s["fills"]:
        H.append(f"<h2>Fills on Kalshi ({len(s['fills'])})</h2><div class='tw'><table><tr><th>Time</th><th>Combo</th>"
                 "<th>Side</th><th class='n'>Contracts</th><th class='n'>YES price</th><th class='n'>Fee</th></tr>")
        for x in sorted(s["fills"], key=lambda x: str(x.get("created_time")), reverse=True)[:30]:
            H.append(f"<tr><td class='small'>{html.escape(str(x.get('created_time', ''))[11:19])}Z</td>"
                     f"<td class='small'>&hellip;{html.escape(str(x.get('market_ticker') or x.get('ticker'))[-10:])}</td>"
                     f"<td>{html.escape(str(x.get('outcome_side') or x.get('side') or ''))}</td>"
                     f"<td class='n'>{_f(x.get('count_fp'), 0):,.2f}</td>"
                     f"<td class='n'>{(_f(x.get('yes_price_dollars'), 0) or 0) * 100:.2f}&cent;</td>"
                     f"<td class='n'>{usd(_f(x.get('fee_cost'), 0), 4)}</td></tr>")
        H.append("</table></div>")
    H.append("<h2>Recent activity</h2><div class='tw'><table><tr><th>Time</th><th>What</th><th>Detail</th></tr>")
    for e in s["feed"]:
        kind = e.get("k")
        when = tm(_ev_ts(e)) if _ev_ts(e) else ""
        if kind in ("quote", "would_quote"):
            d = (f"{len(e.get('legs') or [])} legs &middot; sell at {e['ask'] * 100:.1f}&cent; (fair {e['fair'] * 100:.1f}&cent;)"
                 f" &middot; {e.get('contracts', 0):,.0f} contracts &middot; {usd(e.get('contracts', 0) * e['ask'])}"
                 + (f" &middot; sent in {e['sent_ms']:.0f} ms" if e.get("sent_ms") is not None else "")
                 + f"<div class='muted small'>{legs_html(e.get('legs') or [])}</div>")
        elif kind == "accept":
            d = f"&hellip;{html.escape(str(e.get('mt'))[-10:])} &middot; {_f(e.get('contracts'), 0):,.1f} contracts"
        elif kind == "confirm":
            d = (f"sold {e['contracts']:,.1f} at {e['ask'] * 100:.1f}&cent; &middot; can lose "
                 f"{usd(e['contracts'] * (1 - e['ask']))} &middot; confirmed in {e.get('confirm_ms', 0):.0f} ms")
        elif kind == "decline":
            d = f"{html.escape(str(e.get('why')))} &middot; fair then {(_f(e.get('fair_now')) or 0) * 100:.1f}&cent; vs our {e['ask'] * 100:.1f}&cent;"
        elif kind == "settled":
            d = f"result {html.escape(str(e.get('result')).upper())} &middot; P&amp;L {signed(_f(e.get('pnl')))}"
        elif kind == "err":
            d = f"<span class='neg'>{html.escape(str(e.get('what', '')))}: {html.escape(str(e.get('err') or e.get('msg'))[:200])}</span>"
        elif kind == "position":
            d = f"position read back: {e.get('position')} (negative = NO, as intended)"
        elif kind == "start":
            d = "started " + ("LIVE" if e.get("live") else "DRY")
        else:
            d = html.escape(json.dumps({x: e[x] for x in e if x not in ("k",)})[:160])
        label = {"would_quote": "would quote"}.get(kind, kind)
        H.append(f"<tr><td class='small'>{when}</td><td><span class='k {kind}'>{label}</span></td><td>{d}</td></tr>")
    if not s["feed"]:
        H.append("<tr><td colspan='3' class='empty'>No maker activity yet.</td></tr>")
    H.append("</table></div>")
    if k.get("error"):
        H.append(f"<footer class='neg'>Kalshi read failed: {html.escape(k['error'])}</footer>")
    H.append(f"<footer>Read-only: the maker's files in {html.escape(MAKER_DIR)} and, once a minute, Kalshi's balance, "
             "fills and markets. Times are ET.</footer></main></body></html>")
    return "".join(H)


def make_handler(maker_dir, shadow_dir, kalshi):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                s = snapshot(maker_dir, shadow_dir, time.time(), kalshi)
                if self.path.startswith("/data.json"):
                    body, ctype = json.dumps(s, default=str).encode(), "application/json"
                else:
                    body, ctype = render(s).encode("utf-8"), "text/html; charset=utf-8"
                self.send_response(200)
            except Exception as e:
                body, ctype = f"dashboard error: {type(e).__name__}: {e}".encode(), "text/plain"
                self.send_response(500)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-kalshi", action="store_true", help="local files only")
    a = ap.parse_args(argv)
    kalshi = None if a.no_kalshi else KalshiReads()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(MAKER_DIR, SHADOW_DIR, kalshi))
    print(f"combo dashboard on http://127.0.0.1:{a.port} (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
