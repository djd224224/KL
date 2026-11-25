#!/usr/bin/env python3
r"""send_imm_review.py -- email the IMM daily review (Jack 2026-11-25: "... and
suggests fixes to me in a morning email").

    python send_imm_review.py --day D                  # review.md + exact-number appendix -> email
    python send_imm_review.py --day D --dry            # write review.html only, send nothing
    python send_imm_review.py --day D --test           # send with [TEST] in the subject, no marker
    python send_imm_review.py --day D --fallback --reason "why"
                                                       # evidence-only email: the analysis did not run

Reads run-logs/incentive-mm/daily_review/<D>/: review.md and findings.json (the
skill's), evidence.json (imm_review.py collect), ids.json (ledger ingest).
`[[fp:<fingerprint>]]` in review.md becomes that finding's ledger id. The
appendix is generated here from evidence.json, so the headline numbers in the
email never pass through a model. Writes <D>/review.html and, once sent,
<D>/sent.marker (a second run the same day sends nothing unless --force).

Mail goes through the ALERT_EMAIL_* account send_alert_email.py uses, to
IMM_REVIEW_TO, else ALERT_EMAIL_TO, else the sender; each is read from the
environment first, then from HKCU\Environment (Task Scheduler sessions do not
always carry user variables).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Tuple

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


def substitute_ids(md: str, ids: dict) -> str:
    """[[fp:<fingerprint>]] -> the ledger id (or nothing when it was never ingested)."""
    def rep(m):
        fp = rv._slug(m.group(1))
        return ids.get(fp) or ids.get(m.group(1)) or ""
    return re.sub(r"\[\[fp:([^\]]+)\]\]", rep, md)


def _dow(day: date) -> str:
    return day.strftime("%a ") + f"{day.month}/{day.day}"


def subject_for(day: date, ev: dict, findings: dict, fallback: bool = False,
                reason: str = "") -> str:
    d = ((ev.get("scorecard") or {}).get("day_totals") or {})
    nums = ""
    if d:
        nums = (f"net {rv.usd0(d.get('net'))} (rewards {rv.usd0(d.get('rew'), False)} | "
                f"trading {rv.usd0(d.get('pnl'))})")
    if fallback:
        return f"IMM review {_dow(day)}: {nums or 'no numbers'} -- DATA ONLY ({reason or 'analysis did not run'})"[:200]
    s = str((findings or {}).get("subject") or "").strip()
    if s:
        s = re.sub(r"\s+", " ", s)
        return (s if s.lower().startswith("imm review") else f"IMM review {_dow(day)}: {s}")[:200]
    n_dec = sum(1 for f in (findings or {}).get("findings") or []
                if isinstance(f, dict) and f.get("decision_needed"))
    return f"IMM review {_dow(day)}: {nums} | {n_dec} decision{'s' if n_dec != 1 else ''}"[:200]


def appendix_md(ev: dict) -> str:
    """Exact numbers straight from evidence.json (never model-written)."""
    sc = ev.get("scorecard") or {}
    d = sc.get("day_totals") or {}
    L = ["---", "## The numbers (generated from the evidence pack, not written by the model)"]
    if d:
        w7, w30 = sc.get("7d") or {}, sc.get("30d") or {}
        L += ["| | Day | 7d avg | 30d avg |", "|---|---:|---:|---:|",
              f"| Rewards (modelled) | {rv.usd(d.get('rew'))} | {rv.usd(w7.get('rew_mean'))} | {rv.usd(w30.get('rew_mean'))} |",
              f"| Trading, marked at last trade | {rv.usd(d.get('pnl'))} | {rv.usd(w7.get('pnl_mean'))} | {rv.usd(w30.get('pnl_mean'))} |",
              f"| Net | {rv.usd(d.get('net'))} | {rv.usd(w7.get('mean'))} | {rv.usd(w30.get('mean'))} |",
              f"| Fills / contracts | {int(rv._f(d.get('fills')))} / {rv._f(d.get('cts')):,.0f} | | |",
              ""]
        if w30.get("n"):
            L.append(f"30d risk: Sharpe {rv._fmt(w30.get('sharpe'))}, max drawdown {rv.usd(w30.get('max_dd'))}, "
                     f"worst day {rv.usd(w30.get('worst'))}, green {w30.get('green')}/{w30.get('n')}, "
                     f"trading ate {rv._fmt(w30.get('tax'), 3)} of rewards.")
        chk = sc.get("dash_check")
        if chk:
            ok = abs(rv._f(chk["diff_rew"])) < 1 and abs(rv._f(chk["diff_pnl"])) < 1
            L.append(f"Dashboard tie-out: {'matches' if ok else 'MISMATCH'} (page {chk['dash_rew']} / {chk['dash_pnl']}).")
    fams = (ev.get("attribution") or {}).get("families") or []
    if fams:
        L += ["", "| Family | Day net | 7d net | 30d net |", "|---|---:|---:|---:|"]
        ranked = sorted(fams, key=lambda f: rv._f(f["d30"]["net"]))
        for f in ranked[:5] + ([] if len(ranked) <= 10 else [None]) + ranked[-5:]:
            if f is None:
                L.append("| ... | | | |")
                continue
            L.append(f"| {f['family']} | {rv.usd(f['day']['net'])} | {rv.usd(f['d7']['net'])} | {rv.usd(f['d30']['net'])} |")
    sy = ev.get("system") or {}
    m = sy.get("machine") or {}
    sn = sy.get("sync") or {}
    g = sy.get("git") or {}
    lg = sy.get("log") or {}
    tk = sy.get("tasks") or {}
    checks = []
    if lg:
        checks.append(f"bot log {lg.get('by_severity')}, {len(lg.get('new') or [])} new signature(s), "
                      f"{lg.get('starts')} process start(s)")
    if sn:
        checks.append(f"repo sync last success {sn.get('last_success')}, failure streak {sn.get('failure_streak')}")
    if g:
        checks.append(f"live tree {g.get('ahead')} ahead / {g.get('behind')} behind GitHub, "
                      f"{g.get('dirty_count')} uncommitted tracked files")
    if m:
        checks.append(f"disk {m.get('disk_free_gb')} GB free, ~{m.get('days_to_full')} days to full at "
                      f"{m.get('growth_gb_day')} GB/day")
    if tk and not tk.get("error"):
        checks.append(f"{len(tk.get('failing') or [])} failing scheduled task(s)")
    dh = (ev.get("data") or {}).get("alerts") or []
    if dh:
        checks.append("data sinks off their normal size: " + ", ".join(dh[:8]))
    if checks:
        L += ["", "System checks: " + "; ".join(checks) + "."]
    led = ev.get("ledger") or {}
    if led:
        st = led.get("stats") or {}
        L += ["", f"Ledger: {led.get('open')} open items; {len(led.get('awaiting_decision') or [])} awaiting you; "
                  f"{len(led.get('due') or [])} due a re-measure; verified gains so far "
                  f"{rv.usd(st.get('verified_delta_usd_day'))}/day."]
    errs = ev.get("errors") or {}
    if errs:
        L += ["", "Evidence sections that failed: " + "; ".join(f"{k}: {v}" for k, v in errs.items())]
    L += ["", f"Files: {day_dir(date.fromisoformat(ev['day']))} (evidence.md, review.md, findings.json). "
              "Approve a decision by texting \"do <id>\" or in any Claude session: /imm-daily-review apply <id>."]
    return "\n".join(L)


def build(day: date, fallback: bool = False, reason: str = "") -> Tuple[str, str, str]:
    dd = day_dir(day)
    ev = rv._load_json(os.path.join(dd, "evidence.json"), {}) or {}
    findings = rv._load_json(os.path.join(dd, "findings.json"), {}) or {}
    ids = rv._load_json(os.path.join(dd, "ids.json"), {}) or {}
    review = substitute_ids(_read(os.path.join(dd, "review.md")), ids).strip()
    if not ev:
        ev = {"day": day.isoformat()}
    if fallback or not review:
        why = reason or ("review.md missing" if not review else "")
        head = (f"# IMM review {day.isoformat()}: data only\n\n**The analysis did not run ({why}).** "
                f"Below is the evidence pack the collector built; nothing in it was judged.\n")
        body_md = head + "\n" + _read(os.path.join(dd, "evidence.md"))
        subject = subject_for(day, ev, findings, fallback=True, reason=why)
        text = body_md
    else:
        body_md = review + "\n\n" + appendix_md(ev)
        subject = subject_for(day, ev, findings)
        text = body_md
    page = mdr.page(f"IMM review {day.isoformat()}", [mdr.render(body_md)])
    with open(os.path.join(dd, "review.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    return subject, page, text


def send(subject: str, html: str, text: str) -> bool:
    frm, pw = setting("ALERT_EMAIL_FROM"), setting("ALERT_EMAIL_PASSWORD")
    to = setting("IMM_REVIEW_TO") or setting("ALERT_EMAIL_TO") or frm
    if not frm or not pw:
        print("send: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not configured")
        return False
    try:
        import send_html_email as she
        html_body = she.wrap_body_for_email(html)
    except Exception:
        html_body = html
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
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
    subject, page, text = build(day, fallback=a.fallback, reason=a.reason)
    if a.test:
        subject = "[TEST] " + subject
    print("subject: " + subject)
    print(f"html: {os.path.join(dd, 'review.html')} ({len(page):,} bytes)")
    if a.dry:
        return 0
    if not send(subject, page, text):
        return 1
    if not a.test:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(datetime.now().isoformat() + " " + subject + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
