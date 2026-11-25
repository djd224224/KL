#!/usr/bin/env python3
r"""send_imm_review.py -- email the IMM daily review (Jack 2026-11-25: "... and
suggests fixes to me in a morning email"; then "make the email way clearer and
easier to read").

    python send_imm_review.py --day D                  # build + send; once a day (marker)
    python send_imm_review.py --day D --dry            # write email.html only, send nothing
    python send_imm_review.py --day D --test           # send with [TEST] in the subject, no marker
    python send_imm_review.py --day D --fallback --reason "why"
                                                       # numbers-only email: the analysis did not run

THE EMAIL is built here, not written by the model, so it looks the same every
day and fits a phone:
  1. the day's net in one big number, rewards / trading / the 7-day average,
     and the model's one-sentence verdict;
  2. NEEDS YOUR OK: at most three cards (headline, why, fix, what it's worth,
     the exact text that approves it);
  3. WORTH KNOWING: one line per finding that is new today (or P0/P1);
  4. did past fixes work, and what is still waiting on Jack;
  5. the numbers (day / 7d / 30d, best and worst families) and a row of
     health checks, coloured green / amber / red.
The words in the cards come from each finding's short "email" block in
findings.json (headline / why / fix / value); a finding without one falls back
to a shortened title, summary and plan. The model's long review.md rides along
as an attachment ("full write-up"). Numbers come from evidence.json only.

Reads run-logs/incentive-mm/daily_review/<D>/: evidence.json (imm_review.py
collect), findings.json + review.md (the skill), ids.json + ingested.json
(ledger ingest), and the ledger itself. Writes <D>/email.html and, once sent,
<D>/sent.marker.

Mail goes through the ALERT_EMAIL_* account send_alert_email.py uses, to
IMM_REVIEW_TO, else ALERT_EMAIL_TO, else the sender; each is read from the
environment first, then from HKCU\Environment.
"""
from __future__ import annotations

import argparse
import html as _html
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import imm_review as rv  # noqa: E402
import md_report_email as mdr  # noqa: E402

try:
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.stderr.reconfigure(errors="backslashreplace")
except Exception:
    pass

SEND_TRIES = 4
SEND_WAIT = 90
MAX_DECISIONS = 3
MAX_FYI = 7

# palette (inline styles: Gmail keeps those)
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
INK, MUTED, LINE, PAGE, CARD = "#1f2328", "#5f6368", "#e3e6ea", "#f4f5f7", "#ffffff"
GREEN, RED, AMBER, BLUE = "#137333", "#c5221f", "#b06000", "#1a56db"
SEV_STYLE = {"P0": ("#fce8e6", RED), "P1": ("#fce8e6", RED),
             "P2": ("#fef3e0", AMBER), "P3": ("#eef0f2", MUTED)}
DOT = {"green": GREEN, "amber": AMBER, "red": RED, "gray": "#9aa0a6"}


def setting(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0] or "")
    except Exception:
        return ""


def day_dir(day: date) -> str:
    return os.path.join(rv.REVIEW_DIR, day.isoformat())


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def esc(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)


def substitute_ids(md: str, ids: dict) -> str:
    """[[fp:<fingerprint>]] -> the ledger id (or nothing when it was never ingested)."""
    def rep(m):
        fp = rv._slug(m.group(1))
        return ids.get(fp) or ids.get(m.group(1)) or ""
    return re.sub(r"\[\[fp:([^\]]+)\]\]", rep, md)


def _dow(day: date) -> str:
    return day.strftime("%a ") + f"{day.month}/{day.day}"


def money(x, signed: bool = True) -> str:
    """$ rounded to whole dollars: +$415, -$461, $875."""
    v = rv._f(x)
    s = "${:,.0f}".format(abs(v))
    return (("-" if v < 0 else "+") + s) if signed else s


def _short(text, n: int) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = re.sub(r"\[\[fp:[^\]]+\]\]", "", t)
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "..."


def _first_sentence(text, n: int) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    return _short(m.group(1) if m else t, n)


def email_fields(f: dict) -> dict:
    """The four short phrases a card shows. The skill writes them in the
    finding's "email" block; older findings fall back to shortened fields."""
    e = f.get("email") if isinstance(f.get("email"), dict) else {}
    imp = f.get("impact") if isinstance(f.get("impact"), dict) else {}
    plan = f.get("plan") if isinstance(f.get("plan"), dict) else {}
    value = e.get("value") or ""
    if not value and imp.get("usd_day") not in (None, "", 0, 0.0):
        value = f"~{money(imp.get('usd_day'), False)}/day ({imp.get('basis') or 'modelled'})"
    return {"headline": _short(e.get("headline") or f.get("title"), 90),
            "why": _short(e.get("why") or _first_sentence(f.get("summary"), 230), 240),
            "fix": _short(e.get("fix") or _first_sentence(plan.get("change"), 170), 180),
            "value": _short(value, 60)}


def load_ctx(day: date) -> dict:
    dd = day_dir(day)
    ev = rv._load_json(os.path.join(dd, "evidence.json"), {}) or {"day": day.isoformat()}
    findings = rv._load_json(os.path.join(dd, "findings.json"), {}) or {}
    ids = rv._load_json(os.path.join(dd, "ids.json"), {}) or {}
    ing = rv._load_json(os.path.join(dd, "ingested.json"), {}) or {}
    created = set(ing.get("created") or [])
    try:
        ledger = rv.ledger_state()
    except Exception:
        ledger = {}
    rows = []
    for f in findings.get("findings") or []:
        if not isinstance(f, dict):
            continue
        fp = rv._slug(f.get("fingerprint") or "")
        iid = ids.get(fp) or (f.get("ledger_id") if f.get("ledger_id") in ledger else None) or ""
        sev = str(f.get("severity") or "P3").upper()
        rows.append({"id": iid, "sev": sev if sev in SEV_STYLE else "P3",
                     "decision": bool(f.get("decision_needed")), "new": iid in created or not iid,
                     "usd": abs(rv._f((f.get("impact") or {}).get("usd_day")))
                     if isinstance(f.get("impact"), dict) else 0.0,
                     **email_fields(f)})
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    rows.sort(key=lambda r: (order[r["sev"]], -r["usd"]))
    decisions = [r for r in rows if r["decision"]][:MAX_DECISIONS]
    extra_dec = [r for r in rows if r["decision"]][MAX_DECISIONS:]
    fyi_all = [r for r in rows if not r["decision"]] + extra_dec
    fyi = [r for r in fyi_all if r["new"] or r["sev"] in ("P0", "P1")]
    today_ids = {r["id"] for r in rows}
    waiting = []
    for iid, it in sorted(ledger.items()):
        if it.get("status") == "proposed" and iid not in today_ids and (it.get("first_seen") or "") < day.isoformat():
            age = (day - date.fromisoformat(it["first_seen"])).days if it.get("first_seen") else 0
            head = (it.get("email") or {}).get("headline") if isinstance(it.get("email"), dict) else None
            waiting.append({"id": iid, "headline": _short(head or it.get("title"), 80), "age": age})
    evals = []
    for e in findings.get("evaluations") or []:
        if isinstance(e, dict) and e.get("id"):
            it = ledger.get(e["id"]) or {}
            evals.append({"id": e["id"], "verdict": e.get("verdict") or "inconclusive",
                          "headline": _short(it.get("title") or e.get("note"), 80),
                          "delta": e.get("delta_usd_day"), "note": _short(e.get("note"), 140)})
    closures = [{"id": c.get("id"), "status": c.get("status"), "note": _short(c.get("note"), 120)}
                for c in findings.get("closures") or [] if isinstance(c, dict) and c.get("id")]
    return {"day": day, "ev": ev, "findings": findings, "decisions": decisions,
            "fyi": fyi[:MAX_FYI], "fyi_more": max(0, len(fyi) - MAX_FYI),
            "repeat_quiet": len(fyi_all) - len(fyi), "waiting": waiting[:6],
            "evals": evals, "closures": closures,
            "open": sum(1 for it in ledger.values() if it.get("status") not in rv.TERMINAL),
            "verdict": _short(findings.get("verdict") or findings.get("headline") or "", 260)}


# ---------------------------------------------------------------------------
# health checks: (label, colour) chips from the evidence pack
# ---------------------------------------------------------------------------

def health_checks(ev: dict) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    cv = ev.get("coverage") or {}
    sy = ev.get("system") or {}
    if cv.get("cycles") is not None:
        dark = rv._f(cv.get("dark_minutes"))
        out.append(("Bot up all day" if dark <= 0 else f"Bot dark {dark:.0f} min",
                    "green" if dark <= 0 else ("amber" if dark < 30 else "red")))
    lg = sy.get("log") or {}
    if lg:
        sev = lg.get("by_severity") or {}
        crashes = int(rv._f(sev.get("TB"))) + int(rv._f(sev.get("!!")))
        new = len(lg.get("new") or [])
        out.append(("No crashes" if not crashes else f"{crashes} crash line(s)", "green" if not crashes else "red"))
        out.append(("No new errors" if not new else f"{new} new error type(s)", "green" if not new else "amber"))
    tk = sy.get("tasks") or {}
    if tk and not tk.get("error"):
        fails = tk.get("failing") or []
        imm = [t for t in fails if re.search(r"imm|incentive|portfolio|sync|dashboard", t.get("task") or "", re.I)]
        if not fails:
            out.append(("All jobs OK", "green"))
        elif imm:
            out.append((f"{len(imm)} IMM job(s) failing", "red"))
        else:
            out.append((f"{len(fails)} other job(s) failing", "gray"))
    sn = sy.get("sync") or {}
    g = sy.get("git") or {}
    if sn or g:
        streak = int(rv._f(sn.get("failure_streak")))
        unp = len(g.get("unpushed") or [])
        if streak == 0 and unp == 0:
            out.append(("Repo in sync", "green"))
        else:
            out.append((f"Repo sync failing x{streak}" if streak else f"{unp} unpushed commit(s)", "red" if streak > 2 else "amber"))
    m = sy.get("machine") or {}
    if m.get("days_to_full") is not None:
        d = rv._f(m["days_to_full"])
        out.append((f"Disk: {d:.0f} days left", "green" if d > 60 else ("amber" if d > 21 else "red")))
    dh = (ev.get("data") or {}).get("alerts") or []
    if ev.get("data"):
        out.append(("Data feeds normal" if not dh else f"{len(dh)} data feed(s) off", "green" if not dh else "amber"))
    cr = (ev.get("scorecard") or {}).get("credits") or {}
    if cr.get("age_days") is not None:
        a = int(rv._f(cr["age_days"]))
        out.append((f"Rewards statement {a}d old", "green" if a <= 4 else ("amber" if a <= 10 else "red")))
    return out


def edge_line(ev: dict, n: int = 3) -> str:
    """Where competition moved the edge (evidence moat.edge): 'Elections rewards
    per $1k resting -35% (eroding); KXRT +40% (strengthening)'."""
    out = []
    for e in ((ev.get("moat") or {}).get("edge") or [])[:n]:
        lead = rv._f(e.get("lead"))
        what = "pool share" if e.get("share_ratio") is not None else "rewards per $1k resting"
        out.append(f"{e['family']} {what} {(lead - 1) * 100:+.0f}% ({e['verdict']})")
    return "; ".join(out)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _num(x, signed=True, size=None, bold=False) -> str:
    v = rv._f(x)
    col = INK if not signed else (GREEN if v > 0 else RED if v < 0 else INK)
    st = f"color:{col};" + (f"font-size:{size}px;" if size else "") + ("font-weight:700;" if bold else "")
    return f'<span style="{st}">{esc(money(x, signed))}</span>'


def _chip(text: str, colour: str) -> str:
    return (f'<span style="display:inline-block;margin:0 6px 6px 0;padding:3px 9px;border-radius:12px;'
            f'border:1px solid {LINE};background:{CARD};font-size:13px;color:{INK};white-space:nowrap">'
            f'<span style="color:{DOT.get(colour, MUTED)};font-size:14px">&#9679;</span> {esc(text)}</span>')


def _sev(sev: str) -> str:
    bg, fg = SEV_STYLE.get(sev, SEV_STYLE["P3"])
    return (f'<span style="display:inline-block;padding:1px 7px;border-radius:9px;background:{bg};color:{fg};'
            f'font-size:11px;font-weight:700;vertical-align:2px">{esc(sev)}</span>')


def _h2(text: str, count: Optional[int] = None) -> str:
    c = f' <span style="color:{MUTED};font-weight:400">({count})</span>' if count is not None else ""
    return (f'<div style="margin:26px 0 8px;font-size:13px;font-weight:700;letter-spacing:.06em;'
            f'text-transform:uppercase;color:{MUTED}">{esc(text)}{c}</div>')


def _card(inner: str, accent: str = LINE) -> str:
    return (f'<div style="background:{CARD};border:1px solid {LINE};border-left:4px solid {accent};'
            f'border-radius:10px;padding:14px 16px;margin:0 0 10px">{inner}</div>')


def render_html(ctx: dict, fallback_reason: str = "") -> str:
    day: date = ctx["day"]
    ev = ctx["ev"]
    sc = ev.get("scorecard") or {}
    d = sc.get("day_totals") or {}
    w7, w30 = sc.get("7d") or {}, sc.get("30d") or {}
    P = []
    a = P.append
    pre = ctx["verdict"] or (f"Net {money(d.get('net'))} on {_dow(day)}" if d else "")
    a(f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{esc(pre)}</div>')
    a(f'<div style="background:{PAGE};padding:16px 0;font-family:{FONT};color:{INK};font-size:15px;line-height:1.45">')
    a('<div style="max-width:640px;margin:0 auto;padding:0 12px">')
    # header
    a(f'<div style="font-size:13px;color:{MUTED}">IMM daily review &middot; {esc(day.strftime("%A, %B %d").replace(" 0", " "))}</div>')
    if d:
        a(f'<div style="margin:6px 0 2px">{_num(d.get("net"), size=34, bold=True)}'
          f'<span style="font-size:16px;color:{MUTED}"> net</span></div>')
        a(f'<div style="font-size:14px;color:{MUTED}">Rewards {_num(d.get("rew"))} &middot; Trading {_num(d.get("pnl"))}'
          f' &middot; 7-day avg {_num(w7.get("mean"))}/day</div>')
    if fallback_reason:
        a(_card(f'<b style="color:{RED}">The analysis did not run</b> ({esc(fallback_reason)}). Numbers and checks '
                'below come straight from the data; nothing was judged. The full evidence pack is attached.', RED))
    elif ctx["verdict"]:
        a(f'<div style="margin:12px 0 0;font-size:16px">{esc(ctx["verdict"])}</div>')
    # decisions
    if not fallback_reason:
        dec = ctx["decisions"]
        a(_h2("Needs your OK", len(dec)))
        if not dec:
            a(f'<div style="color:{MUTED}">Nothing today.</div>')
        for i, r in enumerate(dec, 1):
            idt = r["id"] or "(not filed)"
            inner = [f'<div style="font-size:16px;font-weight:700;margin-bottom:6px">{i}. {esc(r["headline"])} {_sev(r["sev"])}</div>']
            if r["why"]:
                inner.append(f'<div style="margin-bottom:6px">{esc(r["why"])}</div>')
            if r["fix"]:
                inner.append(f'<div style="margin-bottom:6px"><b>Fix:</b> {esc(r["fix"])}</div>')
            if r["value"]:
                inner.append(f'<div style="margin-bottom:8px"><b>Worth:</b> {esc(r["value"])}</div>')
            inner.append(f'<div style="font-size:14px;color:{BLUE}">Approve: text '
                         f'<b style="white-space:nowrap">do {esc(idt)}</b></div>'
                         f'<div style="font-size:12px;color:{MUTED}">or text '
                         f'<span style="white-space:nowrap">no {esc(idt)}</span> to drop it</div>')
            a(_card("".join(inner), SEV_STYLE[r["sev"]][1]))
        # fyi
        if ctx["fyi"]:
            a(_h2("Worth knowing"))
            items = []
            for r in ctx["fyi"]:
                why = f'<div style="color:{MUTED};font-size:14px">{esc(r["why"])}</div>' if r["why"] else ""
                items.append(f'<div style="padding:8px 0;border-top:1px solid {LINE}">{_sev(r["sev"])} '
                             f'<b>{esc(r["headline"])}</b> <span style="color:{MUTED};font-size:12px;'
                             f'white-space:nowrap">{esc(r["id"])}</span>{why}</div>')
            more = []
            if ctx["fyi_more"]:
                more.append(f'{ctx["fyi_more"]} more new')
            if ctx["repeat_quiet"]:
                more.append(f'{ctx["repeat_quiet"]} already known')
            if more:
                items.append(f'<div style="padding:8px 0;border-top:1px solid {LINE};color:{MUTED};font-size:13px">'
                             f'{esc(" and ".join(more))} - in the full write-up and the ledger.</div>')
            a(_card("".join(items)))
        # past changes
        if ctx["evals"] or ctx["closures"] or ctx["waiting"]:
            a(_h2("Follow-ups"))
            items = []
            mark = {"verified": ("Worked", GREEN), "failed": ("Didn't work", RED), "inconclusive": ("Too early to tell", AMBER)}
            for e in ctx["evals"]:
                lab, col = mark.get(e["verdict"], ("Checked", MUTED))
                dl = f' ({esc(money(e["delta"]))}/day)' if e.get("delta") not in (None, "") else ""
                items.append(f'<div style="padding:6px 0"><b style="color:{col}">{lab}:</b> {esc(e["headline"])}{dl} '
                             f'<span style="color:{MUTED};font-size:12px;white-space:nowrap">{esc(e["id"])}</span></div>')
            for c in ctx["closures"]:
                items.append(f'<div style="padding:6px 0"><b>Closed</b> ({esc(c["status"])}): {esc(c["note"])} '
                             f'<span style="color:{MUTED};font-size:12px;white-space:nowrap">{esc(c["id"])}</span></div>')
            for w in ctx["waiting"]:
                items.append(f'<div style="padding:6px 0"><b style="color:{AMBER}">Still waiting on you</b> '
                             f'({w["age"]}d): {esc(w["headline"])} <span style="color:{MUTED};font-size:12px;'
                             f'white-space:nowrap">text do {esc(w["id"])}</span></div>')
            a(_card("".join(items)))
    # numbers
    if d:
        a(_h2("The numbers"))
        rows = [("Net", d.get("net"), w7.get("mean"), w30.get("mean"), True),
                ("Rewards (modelled)", d.get("rew"), w7.get("rew_mean"), w30.get("rew_mean"), True),
                ("Trading (marked to last trade)", d.get("pnl"), w7.get("pnl_mean"), w30.get("pnl_mean"), True)]
        td = f'style="padding:6px 8px;text-align:right;border-top:1px solid {LINE}"'
        tl = f'style="padding:6px 8px;text-align:left;border-top:1px solid {LINE}"'
        t = [f'<table style="width:100%;border-collapse:collapse;font-size:14px">'
             f'<tr><th style="text-align:left;padding:4px 8px;color:{MUTED};font-weight:600"></th>'
             f'<th style="text-align:right;padding:4px 8px;color:{MUTED};font-weight:600">{esc(_dow(day))}</th>'
             f'<th style="text-align:right;padding:4px 8px;color:{MUTED};font-weight:600">7-day avg</th>'
             f'<th style="text-align:right;padding:4px 8px;color:{MUTED};font-weight:600">30-day avg</th></tr>']
        for lab, x, y, z, sg in rows:
            t.append(f'<tr><td {tl}>{esc(lab)}</td><td {td}>{_num(x, sg)}</td><td {td}>{_num(y, sg)}</td>'
                     f'<td {td}>{_num(z, sg)}</td></tr>')
        t.append(f'<tr><td {tl}>Fills / contracts</td><td {td}>{int(rv._f(d.get("fills")))} / '
                 f'{rv._f(d.get("cts")):,.0f}</td><td {td}></td><td {td}></td></tr></table>')
        risk = []
        if w30.get("n"):
            risk.append(f'30 days: worst day {money(w30.get("worst"))}, max drawdown {money(w30.get("max_dd"))}, '
                        f'{w30.get("green")} of {w30.get("n")} days green')
            if w30.get("tax") is not None:
                risk.append(f'trading gave back {rv._f(w30.get("tax")) * 100:.0f}% of rewards')
        fams = (ev.get("attribution") or {}).get("families") or []
        famline = ""
        if fams:
            ranked = sorted((f for f in fams if abs(rv._f(f["day"]["net"])) >= 1), key=lambda f: -rv._f(f["day"]["net"]))
            best = [f'{f["family"]} {money(f["day"]["net"])}' for f in ranked[:3] if rv._f(f["day"]["net"]) > 0]
            worst = [f'{f["family"]} {money(f["day"]["net"])}' for f in ranked[::-1][:3] if rv._f(f["day"]["net"]) < 0]
            if best:
                famline += f'<div style="margin-top:8px"><b style="color:{GREEN}">Best:</b> {esc(", ".join(best))}</div>'
            if worst:
                famline += f'<div><b style="color:{RED}">Worst:</b> {esc(", ".join(worst))}</div>'
        edge = edge_line(ev)
        if edge:
            famline += f'<div style="margin-top:8px"><b>Edge watch:</b> {esc(edge)}</div>'
        a(_card("".join(t) + (f'<div style="margin-top:10px;color:{MUTED};font-size:13px">{esc("; ".join(risk))}.</div>' if risk else "") + famline))
    checks = health_checks(ev)
    if checks:
        a(_h2("Health checks"))
        a('<div>' + "".join(_chip(t, c) for t, c in checks) + '</div>')
    # footer
    foot = [f'{ctx["open"]} open item(s) in the ledger.'] if ctx.get("open") else []
    foot.append("The full write-up is attached." if not fallback_reason else "The evidence pack is attached.")
    foot.append("Approve by text (do R-...) or in any Claude session (/imm-daily-review apply R-...).")
    foot.append("Rewards are the bot's model until Kalshi pays them (1-2 days later); trading is marked to the last trade.")
    a(f'<div style="margin:22px 0 8px;font-size:12px;color:{MUTED}">{esc(" ".join(foot))}</div>')
    a('</div></div>')
    return "\n".join(P)


# ---------------------------------------------------------------------------
# plain text (previews, old clients)
# ---------------------------------------------------------------------------

def render_text(ctx: dict, fallback_reason: str = "") -> str:
    day: date = ctx["day"]
    sc = (ctx["ev"].get("scorecard") or {})
    d = sc.get("day_totals") or {}
    w7 = sc.get("7d") or {}
    L = [f"IMM daily review - {_dow(day)}"]
    if d:
        L.append(f"Net {money(d.get('net'))} (rewards {money(d.get('rew'))}, trading {money(d.get('pnl'))}); "
                 f"7-day avg {money(w7.get('mean'))}/day.")
    if fallback_reason:
        L.append(f"The analysis did not run ({fallback_reason}). Numbers only; evidence attached.")
    elif ctx["verdict"]:
        L.append(ctx["verdict"])
    if not fallback_reason:
        L += ["", f"NEEDS YOUR OK ({len(ctx['decisions'])})"]
        for i, r in enumerate(ctx["decisions"], 1):
            L.append(f"{i}. {r['headline']} [{r['sev']}]")
            for k, lab in (("why", ""), ("fix", "Fix: "), ("value", "Worth: ")):
                if r[k]:
                    L.append(f"   {lab}{r[k]}")
            L.append(f"   Approve: text \"do {r['id']}\"")
        if not ctx["decisions"]:
            L.append("Nothing today.")
        if ctx["fyi"]:
            L += ["", "WORTH KNOWING"]
            for r in ctx["fyi"]:
                L.append(f"- [{r['sev']}] {r['headline']} ({r['id']})")
        for e in ctx["evals"]:
            L.append(f"- Follow-up {e['id']}: {e['verdict']} - {e['headline']}")
        for w in ctx["waiting"]:
            L.append(f"- Still waiting on you ({w['age']}d): {w['headline']} - text do {w['id']}")
    edge = edge_line(ctx["ev"])
    if edge:
        L += ["", "EDGE WATCH: " + edge]
    checks = health_checks(ctx["ev"])
    if checks:
        L += ["", "HEALTH: " + "; ".join(t for t, _ in checks)]
    L += ["", "Full write-up attached."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# build + send
# ---------------------------------------------------------------------------

def subject_for(ctx: dict, fallback_reason: str = "") -> str:
    day: date = ctx["day"]
    d = ((ctx["ev"].get("scorecard") or {}).get("day_totals") or {})
    net = f"{money(d.get('net'))} net" if d else "no numbers"
    if fallback_reason:
        return f"IMM review {_dow(day)}: {net} | numbers only, analysis did not run"
    n = len(ctx["decisions"])
    s = f"IMM review {_dow(day)}: {net} | " + (f"{n} need{'s' if n == 1 else ''} your OK" if n else "nothing needs you")
    if ctx["decisions"]:
        top = ctx["decisions"][0]["headline"]
        if len(s) + len(top) + 3 <= 110:
            s += " | " + top
    return s


def full_writeup(day: date, ctx: dict, fallback_reason: str = "") -> Tuple[str, str]:
    """(file name, HTML page) of the long version: the model's review.md, or
    the evidence pack when the analysis did not run."""
    dd = day_dir(day)
    if fallback_reason:
        md = _read(os.path.join(dd, "evidence.md"))
        return f"IMM-evidence-{day.isoformat()}.html", mdr.page(f"IMM evidence {day.isoformat()}", [mdr.render(md)])
    ids = rv._load_json(os.path.join(dd, "ids.json"), {}) or {}
    md = substitute_ids(_read(os.path.join(dd, "review.md")), ids).strip()
    return f"IMM-review-{day.isoformat()}-full.html", mdr.page(f"IMM review {day.isoformat()} (full write-up)",
                                                               [mdr.render(md)])


def build(day: date, fallback: bool = False, reason: str = "") -> Tuple[str, str, str, List[Tuple[str, str]]]:
    dd = day_dir(day)
    os.makedirs(dd, exist_ok=True)
    ctx = load_ctx(day)
    has_review = bool(_read(os.path.join(dd, "review.md")).strip()) and bool(ctx["findings"])
    why = ""
    if fallback or not has_review:
        why = reason or ("no review was written" if not has_review else "fallback requested")
    page = render_html(ctx, why)
    text = render_text(ctx, why)
    subject = subject_for(ctx, why)
    attach = [full_writeup(day, ctx, why)]
    with open(os.path.join(dd, "email.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    return subject, page, text, attach


def send(subject: str, html: str, text: str, attachments: Optional[List[Tuple[str, str]]] = None) -> bool:
    frm, pw = setting("ALERT_EMAIL_FROM"), setting("ALERT_EMAIL_PASSWORD")
    to = setting("IMM_REVIEW_TO") or setting("ALERT_EMAIL_TO") or frm
    if not frm or not pw:
        print("send: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not configured")
        return False
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(text, "plain", "utf-8"))
    body.attach(MIMEText(html, "html", "utf-8"))
    msg = MIMEMultipart("mixed")
    msg.attach(body)
    for name, content in attachments or []:
        part = MIMEText(content, "html", "utf-8")
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    rcpt = [r.strip() for r in to.split(",") if r.strip()]
    for i in range(SEND_TRIES):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(frm, pw)
                s.sendmail(frm, rcpt, msg.as_string())
            print(f"send: ok ({len(rcpt)} recipient(s))")
            return True
        except Exception as e:
            print(f"send: try {i + 1} failed: {type(e).__name__}: {e}")
            if i + 1 < SEND_TRIES:
                time.sleep(SEND_WAIT)
    print("send: FAILED")
    return False


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Email the IMM daily review.")
    ap.add_argument("--day")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--fallback", action="store_true")
    ap.add_argument("--reason", default="")
    a = ap.parse_args(argv)
    day = date.fromisoformat(a.day) if a.day else rv.prior_et_day()
    dd = day_dir(day)
    os.makedirs(dd, exist_ok=True)
    marker = os.path.join(dd, "sent.marker")
    if os.path.exists(marker) and not (a.force or a.dry or a.test):
        print(f"already sent for {day} ({marker}); --force to resend")
        return 0
    subject, page, text, attach = build(day, fallback=a.fallback, reason=a.reason)
    if a.test:
        subject = "[TEST] " + subject
    print("subject: " + subject)
    print(f"html: {os.path.join(dd, 'email.html')} ({len(page):,} bytes); attachments: "
          + ", ".join(f"{n} ({len(c):,} bytes)" for n, c in attach))
    if a.dry:
        return 0
    if not send(subject, page, text, attach):
        return 1
    if not a.test:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(datetime.now().isoformat() + " " + subject + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
