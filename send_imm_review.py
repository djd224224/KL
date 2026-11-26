#!/usr/bin/env python3
r"""send_imm_review.py -- email the IMM daily review (Jack 2026-11-25: "... and
suggests fixes to me in a morning email"; "make the email way clearer and easier
to read"; then "make the email comprehensive, but just make it easy to read and
legible").

    python send_imm_review.py --day D                  # build + send; once a day (marker)
    python send_imm_review.py --day D --dry            # write email.html only, send nothing
    python send_imm_review.py --day D --test           # send with [TEST] in the subject, no marker
    python send_imm_review.py --day D --fallback --reason "why"
                                                       # numbers-only email: the analysis did not run

THE EMAIL carries the whole review, laid out to be read on a phone:
  1. the day's net in one big number, rewards / trading / the 7-day average, and
     the verdict -- then the write-up's opening paragraph;
  2. NEEDS YOUR OK: up to three cards (headline, why, fix, worth, the exact text
     that approves it), from findings.json;
  3. THE FULL REVIEW: every section of the model's review.md, each in its own card,
     rendered for reading -- short paragraphs, real tables, bullets, gains in green
     and losses in red, ledger ids as small tags (nothing is cut);
  4. EVERYTHING FOUND TODAY: one row per finding, so nothing is missed;
  5. THE NUMBERS from evidence.json (day / 7d / 30d, best and worst families, the
     edge watch) and the green / amber / red health checks.
The numbers in 1 and 5 come from evidence.json, never from the model. The
evidence pack rides along as an attachment for deep dives.

Reads run-logs/incentive-mm/daily_review/<D>/: evidence.json, evidence.md,
findings.json, review.md, ids.json, ingested.json, and the ledger. Writes
<D>/email.html and, once sent, <D>/sent.marker.

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

# palette and type (inline styles: Gmail keeps those)
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "Consolas,Menlo,monospace"
INK, MUTED, LINE, PAGE, CARD, ZEBRA = "#1f2328", "#5f6368", "#e3e6ea", "#f4f5f7", "#ffffff", "#f8f9fa"
GREEN, RED, AMBER, BLUE = "#137333", "#c5221f", "#b06000", "#1a56db"
SEV_STYLE = {"P0": ("#fce8e6", RED), "P1": ("#fce8e6", RED),
             "P2": ("#fef3e0", AMBER), "P3": ("#eef0f2", MUTED)}
DOT = {"green": GREEN, "amber": AMBER, "red": RED, "gray": "#9aa0a6"}
S_P = "margin:0 0 10px;line-height:1.55"
S_LIST = "margin:0 0 10px;padding-left:20px;line-height:1.5"
S_LI = "margin:0 0 6px"
S_H3 = "margin:14px 0 6px;font-size:15px;font-weight:700"
S_CODE = (f"font-family:{MONO};font-size:13px;background:#f1f3f4;border-radius:4px;"
          f"padding:0 4px;white-space:nowrap")
S_TH = f"padding:6px 8px;border-bottom:2px solid {LINE};font-size:12px;color:{MUTED};font-weight:600"
S_TD = f"padding:6px 8px;border-top:1px solid {LINE};font-size:14px;vertical-align:top"


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
    """The four short phrases a decision card shows. The skill writes them in the
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


# ---------------------------------------------------------------------------
# Markdown -> legible, inline-styled HTML
# ---------------------------------------------------------------------------

_RID = re.compile(r"\bR-\d{6}-\d{2}\b")
_MONEY = re.compile(r"(?<![\w$~])([+-])\$([\d,]+(?:\.\d+)?)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITAL = re.compile(r"(?<![*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?![*\w])")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def id_chip(rid: str) -> str:
    return (f'<span style="font-size:12px;color:{MUTED};background:#eef0f2;border-radius:8px;'
            f'padding:0 6px;white-space:nowrap">{rid}</span>')


def inline(text: str) -> str:
    """One line's inline Markdown: `code`, **bold** (which may wrap code),
    *italics*, signed dollars coloured, ledger ids as tags. Everything else
    escaped. Code spans are set aside first so nothing inside them is styled."""
    codes: List[str] = []

    def stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"
    s = esc(re.sub(r"`([^`]+)`", stash, text))
    s = _BOLD.sub(r"<b>\1</b>", s)
    s = _ITAL.sub(r"<i>\1</i>", s)
    s = _MONEY.sub(lambda m: f'<span style="color:{GREEN if m.group(1) == "+" else RED};'
                             f'white-space:nowrap">{m.group(1)}${m.group(2)}</span>', s)
    s = _RID.sub(lambda m: id_chip(m.group(0)), s)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: f'<code style="{S_CODE}">{esc(codes[int(m.group(1))])}</code>', s)


def _cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _table_html(head: List[str], aligns: List[str], rows: List[List[str]]) -> str:
    def al(i):
        return aligns[i] if i < len(aligns) else "left"
    h = "".join(f'<th style="{S_TH};text-align:{al(i)}">{inline(c)}</th>' for i, c in enumerate(head))
    body = []
    for n, r in enumerate(rows):
        bg = f"background:{ZEBRA};" if n % 2 else ""
        body.append("<tr>" + "".join(f'<td style="{S_TD};{bg}text-align:{al(i)}">{inline(c)}</td>'
                                     for i, c in enumerate(r)) + "</tr>")
    return (f'<div style="overflow-x:auto;margin:0 0 12px"><table style="width:100%;border-collapse:collapse">'
            f'<tr>{h}</tr>{"".join(body)}</table></div>')


def _indent(line: str) -> int:
    return len(line.replace("\t", "    ")) - len(line.replace("\t", "    ").lstrip(" "))


def _dedent(lines: List[str]) -> List[str]:
    ind = min((_indent(l) for l in lines if l.strip()), default=0)
    return [l.replace("\t", "    ")[ind:] if l.strip() else "" for l in lines]


def _parse_list(lines: List[str], i: int) -> Tuple[str, int]:
    """A list starting at lines[i] -> (html, index after it). Each item owns the
    lines indented under it -- nested lists, tables, paragraphs -- rendered by
    md_html, so a table inside an item stays inside it. A numbered list keeps
    its numbers (start=)."""
    m = _LIST.match(lines[i])
    base = _indent(lines[i])
    ordered = m.group(2)[0].isdigit()
    start = int(re.match(r"\d+", m.group(2)).group(0)) if ordered else 1
    items: List[list] = []
    j = i
    while j < len(lines):
        l = lines[j]
        lm = _LIST.match(l)
        if lm and _indent(l) == base:
            if lm.group(2)[0].isdigit() != ordered:
                break                                   # bullets after numbers (or back): a new list
            items.append([lm.group(3), []])
            j += 1
            continue
        if not l.strip():
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k < len(lines):
                nm = _LIST.match(lines[k])
                if _indent(lines[k]) > base or (nm and _indent(lines[k]) == base
                                                and nm.group(2)[0].isdigit() == ordered):
                    items[-1][1].append("")
                    j += 1
                    continue
            break
        if _indent(l) > base and items:
            items[-1][1].append(l)
            j += 1
            continue
        break
    tag = "ol" if ordered else "ul"
    attr = f' start="{start}"' if ordered and start != 1 else ""
    out = [f'<{tag}{attr} style="{S_LIST}">']
    for first, sub in items:
        body = md_html("\n".join(_dedent(sub))) if any(x.strip() for x in sub) else ""
        out.append(f'<li style="{S_LI}">{inline(first)}{body}</li>')
    out.append(f"</{tag}>")
    return "".join(out), j


def md_html(md: str) -> str:
    """Markdown (headings, paragraphs, bullet and numbered lists with nesting,
    pipe tables, fenced code, rules) -> HTML with inline styles."""
    lines = md.replace("\r\n", "\n").split("\n")
    html, para, i = [], [], 0

    def flush():
        if para:
            html.append(f'<p style="{S_P}">{inline(" ".join(l.strip() for l in para))}</p>')
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush()
            i += 1
            continue
        if s.startswith("```"):
            flush()
            j, code = i + 1, []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            html.append(f'<pre style="font-family:{MONO};font-size:12px;background:#f6f8fa;padding:8px;'
                        f'border-radius:6px;white-space:pre-wrap;margin:0 0 10px">{esc(chr(10).join(code))}</pre>')
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", s)
        if m:
            flush()
            html.append(f'<div style="{S_H3}">{inline(m.group(2))}</div>')
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            head = _cells(s)
            aligns = []
            for c in _cells(lines[i + 1]):
                aligns.append("right" if c.endswith(":") and not c.startswith(":")
                              else "center" if c.startswith(":") and c.endswith(":") else "left")
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(_cells(lines[j]))
                j += 1
            html.append(_table_html(head, aligns, rows))
            i = j
            continue
        if _RULE.match(s):
            flush()
            html.append(f'<div style="border-top:1px solid {LINE};margin:12px 0"></div>')
            i += 1
            continue
        if _LIST.match(line):
            flush()
            block, i = _parse_list(lines, i)
            html.append(block)
            continue
        para.append(line)
        i += 1
    flush()
    return "".join(html)


def split_review(md: str) -> Tuple[str, str, List[Tuple[str, str]]]:
    """(title, intro, [(section heading, body)]) of a review.md."""
    title, intro, sections, cur = "", [], [], None
    for line in md.replace("\r\n", "\n").split("\n"):
        if line.startswith("# ") and not title and cur is None and not any(x.strip() for x in intro):
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            cur = [line[3:].strip(), []]
            sections.append(cur)
            continue
        (cur[1] if cur is not None else intro).append(line)
    return title, "\n".join(intro).strip(), [(h, "\n".join(b).strip()) for h, b in sections]


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

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
    # findings, then the day's answers to "what else should this review be
    # doing to compound?" (findings.json "compound", Jack 2026-11-26): those get
    # their own block under the decision cards, never one of the 3 cards
    raws = [(f, False) for f in findings.get("findings") or [] if isinstance(f, dict)]
    raws += [(rv._clean_compound(c), True) for c in findings.get("compound") or [] if isinstance(c, dict)]
    for f, comp in raws:
        fp = rv._slug(f.get("fingerprint") or "")
        iid = ids.get(fp) or (f.get("ledger_id") if f.get("ledger_id") in ledger else None) or ""
        sev = str(f.get("severity") or "P3").upper()
        it = ledger.get(iid) or {}
        rows.append({"id": iid, "sev": sev if sev in SEV_STYLE else "P3",
                     "decision": bool(f.get("decision_needed")), "new": iid in created or not iid,
                     "seen": len(it.get("seen_days") or []), "status": it.get("status") or "",
                     "usd": abs(rv._f((f.get("impact") or {}).get("usd_day")))
                     if isinstance(f.get("impact"), dict) else 0.0,
                     "compound": comp, "self_apply": f.get("self_apply") if comp else None,
                     "applied": _short(f.get("applied") or "", 160) if comp else "",
                     **email_fields(f)})
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    rows.sort(key=lambda r: (order[r["sev"]], -r["usd"]))
    decisions = [r for r in rows if r["decision"] and not r["compound"]
                 and r["status"] not in rv.TERMINAL][:MAX_DECISIONS]
    compound = [r for r in rows if r["compound"]]
    today_ids = {r["id"] for r in rows}
    waiting = []
    for iid, it in sorted(ledger.items()):
        if it.get("compound"):
            continue          # the compound block lists its own backlog
        if it.get("status") == "proposed" and iid not in today_ids and (it.get("first_seen") or "") < day.isoformat():
            age = (day - date.fromisoformat(it["first_seen"])).days if it.get("first_seen") else 0
            head = (it.get("email") or {}).get("headline") if isinstance(it.get("email"), dict) else None
            waiting.append({"id": iid, "headline": _short(head or it.get("title"), 80), "age": age})
    # [[fp:...]] for an item from an earlier day resolves through the ledger, not
    # only today's ids.json (the 11/26 test run's lesson: those tags rendered empty)
    fp_ids = {it.get("fingerprint"): iid for iid, it in sorted(ledger.items()) if it.get("fingerprint")}
    review_md = substitute_ids(_read(os.path.join(dd, "review.md")), {**fp_ids, **ids}).strip()
    title, intro, sections = split_review(review_md)
    backlog = [{"id": iid, "headline": _short(((it.get("email") or {}).get("headline") if isinstance(
                    it.get("email"), dict) else None) or it.get("title"), 80),
                "age": (day - date.fromisoformat(it["first_seen"])).days if it.get("first_seen") else 0}
               for iid, it in sorted(ledger.items())
               if it.get("compound") and it.get("status") == "proposed" and iid not in today_ids]
    return {"day": day, "ev": ev, "findings": findings, "rows": rows, "decisions": decisions,
            "compound": compound, "compound_backlog": backlog[:6], "change_rows": change_rows(day, ev),
            "waiting": waiting[:8], "review": review_md, "title": title, "intro": intro,
            "sections": sections,
            "open": sum(1 for it in ledger.values() if it.get("status") not in rv.TERMINAL),
            "verdict": _short(findings.get("verdict") or findings.get("headline") or title or "", 300)}


def _change_title(subject: str, n: int = 90) -> str:
    """A commit subject for a phone: the topic prefix without its "imm"
    ("imm fair lead: ..." -> "Fair lead: ..."; "imm: ..." -> "..."), no
    trailing "(Jack 2026-11-26: ...)" or "(review R-...)" quote."""
    s = str(subject or "")
    s = re.sub(r"^env (IMM_\w+):", r"Setting \1:", s)
    s = re.sub(r"^(?:imm|pvd) +(?=[A-Z])", "", s)          # "imm Trump mentions: ..." -> "Trump mentions: ..."
    m = re.match(r"^([a-z][a-z0-9_ .+/-]{0,40}): (.*)$", s, re.S)   # lower-case topic prefixes only
    if m:
        topic = re.sub(r"^(?:imm|incentive_mm|pvd)\b[ _]?", "", m.group(1)).strip()
        if topic and not re.search(r"[_.]", topic):
            topic = topic[:1].upper() + topic[1:]
        s = f"{topic}: {m.group(2)}" if topic else m.group(2)
    s = re.sub(r"\s*\((?:Jack \d{4}-\d{2}-\d{2}|review R-\d{6}-\d{2})[^)]*\)?\s*$", "", s)
    if not re.match(r"^[a-z0-9]+_[a-z0-9_]*:", s):        # a module name keeps its case
        s = s[:1].upper() + s[1:]
    return _short(s, n)


def change_rows(day: date, ev: dict) -> List[dict]:
    """Today's 7-day readouts (evidence section 12) with the review's verdict,
    for the email's "Changes measured" block."""
    rg = ev.get("registry") or {}
    try:
        st = rv.registry_state()
    except Exception:
        st = {}
    iso = day.isoformat()
    out = []
    for r in rg.get("readouts") or []:
        if r.get("same_money_as"):
            continue          # the same money as another readout: shown once, under that one
        keys = r.get("keys") or []
        verdict = note = ""
        for k in keys:
            for v in reversed((st.get(k) or {}).get("verdicts") or []):
                if v.get("day") == iso:
                    verdict, note = v.get("verdict") or "", v.get("note") or ""
                    break
            if verdict:
                break
        subj = (r.get("subjects") or [""])[0]
        out.append({"keys": keys, "title": _change_title(subj), "more": max(0, len(keys) - 1),
                    "split": r.get("split"), "scope": r.get("scope"), "call": r.get("call") or "",
                    "metric": r.get("metric") or "net", "before": (r.get("before") or {}).get("net"),
                    "after": (r.get("after") or {}).get("net"), "delta": r.get("delta") or {},
                    "t": (r.get("t") or {}).get("net"), "p": r.get("p"), "q": r.get("q"),
                    "mde": r.get("mde"), "supply": r.get("supply_ratio"),
                    "verdict": verdict, "note": _short(note, 220), "ids": r.get("ids") or []})
    return out


# ---------------------------------------------------------------------------
# health checks and the edge line
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
            out.append((f"Repo sync failing x{streak}" if streak else f"{unp} unpushed commit(s)",
                        "red" if streak > 2 else "amber"))
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
    w = ((ev.get("self") or {}).get("watches")) or {}
    if w.get("active"):
        nf = len(w.get("firing") or []) + len(w.get("errors") or [])
        hot = any((x.get("severity") or "") in ("P0", "P1") for x in w.get("firing") or [])
        out.append((f"Review checks: {nf} of {w['active']} firing" if nf else f"Review checks: all {w['active']} OK",
                    "green" if not nf else ("red" if hot else "amber")))
    rg = ev.get("registry") or {}
    if rg.get("n_readouts"):
        worse = sum(n for c, n in (rg.get("readout_calls") or {}).items() if c.endswith("worse"))
        tot = int(rv._f(rg["n_readouts"])) - int(rv._f(rg.get("n_merged")))
        out.append((f"Changes at {rv.REGISTRY_DAYS} days: {worse} of {tot} worse" if worse
                    else f"Changes at {rv.REGISTRY_DAYS} days: none of {tot} worse",
                    "amber" if worse else "green"))
    mo = ev.get("models") or {}
    if mo and not mo.get("error"):   # every fair model against the market (imm_fair_scorecard.py)
        h = mo.get("health") or {}
        c = mo.get("counts") or {}
        if h.get("state") in ("stale", "never run"):
            out.append((f"Model log {h['state']}", "red" if h["state"] == "stale" else "amber"))
        elif c.get("loses to the market"):
            out.append((f"Models: {c['loses to the market']} lose to the market", "amber"))
        elif c.get("beats the market"):
            out.append((f"Models: {c['beats the market']} beat the market", "green"))
        else:
            out.append(("Model scorecard accumulating", "gray"))
    bk = ((ev.get("moat") or {}).get("backup")) or {}
    if bk.get("state"):           # the moat's BigQuery copy (R-261124-20)
        s = bk["state"]
        out.append(({"ok": "Moat backed up", "stale": f"Moat backup {rv._f(bk.get('age_h')):.0f}h old",
                     "FAILED": "Moat backup FAILED", "off": "Moat backup off",
                     "never run": "Moat backup not run yet"}.get(s, f"Moat backup {s}"),
                    {"ok": "green", "stale": "amber", "FAILED": "red", "off": "amber"}.get(s, "gray")))
    b = ((ev.get("self") or {}).get("budget")) or {}
    if rv._f(b.get("collect_pct")) >= 0.6:
        out.append((f"Review data gathering at {rv._f(b['collect_pct']) * 100:.0f}% of its limit",
                    "red" if rv._f(b["collect_pct"]) >= 0.85 else "amber"))
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


SECTION_RENAME = {"decisions for you": "Decisions in detail"}
_STAND_ORDER = {"needs your OK": 0, "open": 1, "shipped": 2, "fixed": 2, "closed": 3}


def _standing(r: dict) -> Tuple[str, str]:
    """(label, colour) for where a finding stands in the ledger now."""
    st = r.get("status") or ""
    if st in ("closed", "fixed-elsewhere", "wontfix", "reverted", "rejected"):
        return "closed", MUTED
    if st == "verified":
        return "fixed", GREEN
    if st == "shipped":
        return "shipped", GREEN
    if st in ("proposed", "approved") or (r.get("decision") and st not in rv.TERMINAL and st != "open"):
        return "needs your OK", BLUE
    if r.get("decision") and not st:
        return "needs your OK", BLUE
    return "open", AMBER


def self_line(ev: dict) -> str:
    """One line of the review's own score (evidence section 11)."""
    se = ev.get("self") or {}
    st = (se.get("record") or {}).get("stats") or {}
    if not st:
        return ""
    parts = [f"{st.get('items')} findings so far", f"{st.get('approved')} approved",
             f"{st.get('verified')} verified"]
    if st.get("false_positives"):
        parts.append(f"{st['false_positives']} false positive{'s' if st['false_positives'] != 1 else ''}")
    runs = se.get("runs") or []
    if runs and runs[0].get("minutes"):
        cost = f", ${rv._f(runs[0]['cost_usd']):.2f}" if runs[0].get("cost_usd") is not None else ""
        parts.append(f"last run {rv._f(runs[0]['minutes']):.0f} min{cost}")
    b = se.get("budget") or {}
    if rv._f(b.get("collect_pct")) >= 0.5:
        parts.append(f"data gathering at {rv._f(b['collect_pct']) * 100:.0f}% of its time limit")
    return "; ".join(parts) + "."


def render_compound(ctx: dict) -> str:
    """The block under the decision cards: what the review will do to get
    better, asked and answered every day (Jack 2026-11-26)."""
    comp = ctx.get("compound") or []
    ev = ctx.get("ev") or {}
    w = ((ev.get("self") or {}).get("watches")) or {}
    added = ((rv._load_json(os.path.join(day_dir(ctx["day"]), "ingested.json"), {}) or {}).get("watches") or {}).get("added") or []
    P = [_h2("Making the review better", len(comp))]
    line = self_line(ev)
    if line:
        P.append(f'<div style="margin:0 0 8px;font-size:13px;color:{MUTED}">{esc(line)}</div>')
    if not comp:
        P.append(_card(f'<span style="color:{AMBER}">The review did not answer its daily question today '
                       '(what else it should be doing to compound and improve itself).</span>', AMBER))
    for r in comp:
        inner = [f'<div style="font-size:15px;font-weight:700;margin-bottom:6px">{esc(r["headline"])}</div>']
        if r["why"]:
            inner.append(f'<div style="margin-bottom:6px">{inline(r["why"])}</div>')
        if r["fix"]:
            inner.append(f'<div style="margin-bottom:6px"><b>Change:</b> {inline(r["fix"])}</div>')
        if r["value"]:
            inner.append(f'<div style="margin-bottom:8px"><b>Worth:</b> {esc(r["value"])}</div>')
        if r.get("self_apply") and not r["decision"]:
            did = r.get("applied") or {"watch": "added a read-only check", "lesson": "filed a lesson",
                                         "playbook": "added a playbook rule"}.get(r["self_apply"], "")
            ids_ = f' ({", ".join(added)})' if r["self_apply"] == "watch" and added else ""
            inner.append(f'<div style="font-size:14px;color:{GREEN}">Done by the review itself: {esc(did)}{esc(ids_)}'
                         f'{" " + id_chip(r["id"]) if r["id"] else ""}</div>')
        else:
            idt = r["id"] or "(not filed)"
            inner.append(f'<div style="font-size:14px;color:{BLUE}">Build it: text '
                         f'<b style="white-space:nowrap">do {esc(idt)}</b></div>')
        P.append(_card("".join(inner), GREEN if (r.get("self_apply") and not r["decision"]) else BLUE))
    if w.get("firing") or w.get("errors"):
        rows = [f'<div style="padding:3px 0">{_sev(x.get("severity") or "P2")} {esc(x.get("title"))} '
                f'<span style="color:{MUTED};font-size:13px">({esc(x["id"])}: '
                f'{esc(x.get("error") or ("value " + str(x.get("value"))))})</span></div>'
                for x in (w.get("firing") or []) + (w.get("errors") or [])]
        P.append(_card(f'<div style="font-weight:700;margin-bottom:4px">The review\'s own checks firing</div>'
                       + "".join(rows), AMBER))
    if ctx.get("compound_backlog"):
        P.append(f'<div style="margin:0 0 8px;font-size:13px;color:{MUTED}">Earlier ideas still waiting: '
                 + "; ".join(f'{esc(b["headline"])} ({esc(b["id"])}, {b["age"]}d)' for b in ctx["compound_backlog"])
                 + '</div>')
    return "".join(P)


CHANGE_SHOW = 8
CHANGE_SHOW_WORSE = 5        # on a day with more readouts than fit: 5 that look worse, then 3 better
_CALL_COLOUR = {"better": GREEN, "new, better": GREEN, "worse": RED, "new, worse": RED}
_VERDICT_COLOUR = {"worked": GREEN, "hurt": RED, "no-effect": MUTED, "inconclusive": AMBER, "extend": AMBER}


def _change_side(r: dict) -> str:
    """worse / better / other, by the review's verdict when there is one."""
    if r.get("verdict") in ("hurt", "worked"):
        return "worse" if r["verdict"] == "hurt" else "better"
    c = r.get("call") or ""
    return "worse" if c.endswith("worse") else ("better" if c.endswith("better") else "other")


def pick_changes(rows: List[dict], n: int = CHANGE_SHOW) -> List[dict]:
    """The rows the email shows: all of them when they fit; else up to 5 that
    look worse and then the better ones, in the evidence's order, topped up with
    the rest. The first back-fill (199 readouts) otherwise showed 8 worse rows."""
    if len(rows) <= n:
        return list(rows)
    worse = [r for r in rows if _change_side(r) == "worse"][:CHANGE_SHOW_WORSE]
    better = [r for r in rows if _change_side(r) == "better"][:n - len(worse)]
    pick = worse + better
    pick += [r for r in rows if not any(r is p for p in pick)][:n - len(pick)]
    return [r for r in rows if any(r is p for p in pick)]


def _short_day(iso: str) -> str:
    try:
        return _dow(date.fromisoformat(iso))
    except (TypeError, ValueError):
        return str(iso or "")


def render_changes(ctx: dict) -> str:
    """Every IMM change read out 7 days after it went live (evidence section 12,
    Jack 2026-11-26): the biggest moves first, each with the review's verdict."""
    ev = ctx.get("ev") or {}
    rg = ev.get("registry") or {}
    if not rg or rg.get("error"):
        return ""
    rows = ctx.get("change_rows") or []
    n = int(rv._f(rg.get("n_readouts"))) - int(rv._f(rg.get("n_merged")))
    P = [_h2(f"Changes measured at {rv.REGISTRY_DAYS} days", n)]
    sy = rg.get("sync") or {}
    nd = rg.get("next_due") or []
    line = (f"Every change to the bot is measured {rv.REGISTRY_DAYS} days after it went live: the "
            f"{rv.REGISTRY_DAYS} days after against the {rv.REGISTRY_DAYS} before, on the markets it names "
            "(before/after, not proof). Better or worse means it beat chance after allowing for how many "
            "changes were tested together. ")
    if sy.get("first"):
        line += (f"First readout: every change since {sy.get('since')} measured at once ({n} distinct "
                 "readouts); the biggest moves are below. ")
    line += (f"{int(rv._f(rg.get('n_went_live')))} went live yesterday; {int(rv._f(rg.get('in_window')))} are "
             f"inside their {rv.REGISTRY_DAYS} days"
             + (f"; next readouts {_short_day(nd[0][0])} ({nd[0][1]})" if nd else "") + ".")
    P.append(f'<div style="margin:0 0 8px;font-size:13px;color:{MUTED}">{esc(line)}</div>')
    if rows:
        inner = []
        for i, r in enumerate(pick_changes(rows)):
            top = "" if i == 0 else f"border-top:1px solid {LINE};padding-top:10px;"
            d = r["delta"] or {}
            more = f' <span style="color:{MUTED};font-size:12px">(+{r["more"]} more)</span>' if r["more"] else ""
            t = "" if r.get("p") is None else (", p < 0.001" if rv._f(r["p"]) < 0.001
                                               else f", p {rv._f(r['p']):.3f}".rstrip("0").rstrip("."))
            sup = r.get("supply")
            sup_t = "" if sup is None or 0.8 <= rv._f(sup) <= 1.25 else f" &middot; program pool x{rv._f(sup):.1f}"
            call_col = _CALL_COLOUR.get(r["call"], MUTED)
            verdict = ""
            if r["verdict"]:
                verdict = (f'<div style="margin-top:4px;font-size:14px"><b style="color:'
                           f'{_VERDICT_COLOUR.get(r["verdict"], INK)}">Review: {esc(r["verdict"])}</b>'
                           f'{" &mdash; " + inline(r["note"]) if r["note"] else ""}</div>')
            metric = "" if r["metric"] == "net" else (f' <span style="font-size:12px;color:{MUTED}">'
                                                       f'(judged on {esc(r["metric"])})</span>')
            if r["call"].endswith("no clear change") and r.get("mde") is not None:
                metric += (f' <span style="font-size:12px;color:{MUTED};font-weight:400">(this test only sees '
                           f'moves over {esc(money(r["mde"], False))}/day)</span>')
            inner.append(
                f'<div style="{top}margin-bottom:10px">'
                f'<div style="font-weight:700">{esc(r["title"])}{more}</div>'
                f'<div style="font-size:13px;color:{MUTED}">Live {esc(_short_day(r["split"]))} &middot; '
                f'{esc(_short(r["scope"], 70))}{"".join(" " + id_chip(x) for x in r["ids"][:2])}</div>'
                f'<div style="font-size:14px">Net {_num(r["before"])} &rarr; {_num(r["after"])} a day '
                f'({_num(d.get("net"))}{esc(t)}) &middot; rewards {_num(d.get("rew"))} &middot; trading '
                f'{_num(d.get("pnl"))}{sup_t}</div>'
                f'<div style="font-size:13px;font-weight:600;color:{call_col}">{esc(r["call"])}{metric}</div>'
                f'{verdict}</div>')
        if n > CHANGE_SHOW:
            inner.append(f'<div style="font-size:12px;color:{MUTED}">and {n - CHANGE_SHOW} more in the '
                         'evidence pack (section 12)</div>')
        P.append(_card("".join(inner)))
    warns = rg.get("warnings") or []
    if warns:
        P.append(_card(f'<div style="font-weight:700;margin-bottom:4px;color:{AMBER}">Down hard already '
                       f'(before day {rv.REGISTRY_DAYS})</div>'
                       + "".join(f'<div style="padding:3px 0">{esc(_change_title(w.get("subject"), 90))}: '
                                 f'{esc(w.get("metric"))} {_num(w.get("before"))} &rarr; {_num(w.get("after"))} '
                                 f'a day over {int(rv._f(w.get("after_days")))} days</div>' for w in warns[:5]),
                       AMBER))
    return "".join(P)


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
    a(f'<div style="background:{PAGE};padding:16px 0;font-family:{FONT};color:{INK};font-size:15px;line-height:1.5">')
    a('<div style="max-width:680px;margin:0 auto;padding:0 12px">')
    # 1. header
    a(f'<div style="font-size:13px;color:{MUTED}">IMM daily review &middot; '
      f'{esc(day.strftime("%A, %B %d").replace(" 0", " "))}</div>')
    if d:
        a(f'<div style="margin:6px 0 2px">{_num(d.get("net"), size=34, bold=True)}'
          f'<span style="font-size:16px;color:{MUTED}"> net</span></div>')
        a(f'<div style="font-size:14px;color:{MUTED}">Rewards {_num(d.get("rew"))} &middot; Trading {_num(d.get("pnl"))}'
          f' &middot; 7-day avg {_num(w7.get("mean"))}/day</div>')
    if fallback_reason:
        a(_card(f'<b style="color:{RED}">The analysis did not run</b> ({esc(fallback_reason)}). Below are the '
                'numbers and the evidence pack exactly as the collector built them; nothing was judged.', RED))
        a(render_changes(ctx))        # deterministic: the readouts stand without the analysis
    else:
        if ctx["verdict"]:
            a(f'<div style="margin:12px 0 0;font-size:17px;font-weight:600;line-height:1.4">{inline(ctx["verdict"])}</div>')
        if ctx["intro"]:
            a(f'<div style="margin:10px 0 0">{md_html(ctx["intro"])}</div>')
        # 2. decisions
        dec = ctx["decisions"]
        a(_h2("Needs your OK", len(dec)))
        if not dec:
            a(f'<div style="color:{MUTED}">Nothing today.</div>')
        for i, r in enumerate(dec, 1):
            idt = r["id"] or "(not filed)"
            inner = [f'<div style="font-size:16px;font-weight:700;margin-bottom:6px">{i}. {esc(r["headline"])} {_sev(r["sev"])}</div>']
            if r["why"]:
                inner.append(f'<div style="margin-bottom:6px">{inline(r["why"])}</div>')
            if r["fix"]:
                inner.append(f'<div style="margin-bottom:6px"><b>Fix:</b> {inline(r["fix"])}</div>')
            if r["value"]:
                inner.append(f'<div style="margin-bottom:8px"><b>Worth:</b> {esc(r["value"])}</div>')
            inner.append(f'<div style="font-size:14px;color:{BLUE}">Approve: text '
                         f'<b style="white-space:nowrap">do {esc(idt)}</b></div>'
                         f'<div style="font-size:12px;color:{MUTED}">or text '
                         f'<span style="white-space:nowrap">no {esc(idt)}</span> to drop it '
                         f'&middot; details below</div>')
            a(_card("".join(inner), SEV_STYLE[r["sev"]][1]))
        # 2b. every bot change, read out at 7 days (evidence section 12)
        a(render_changes(ctx))
        # 2c. the review's answer to its own daily question
        a(render_compound(ctx))
        # 3. the full review, section by section
        for head, body in ctx["sections"]:
            if not body.strip():
                continue
            a(_h2(SECTION_RENAME.get(head.strip().lower(), head)))
            a(_card(md_html(body)))
        if ctx["waiting"] and not any("waiting" in h.lower() for h, _ in ctx["sections"]):
            a(_h2("Still waiting on you", len(ctx["waiting"])))
            a(_card("".join(f'<div style="padding:4px 0">{id_chip(w["id"])} {esc(w["headline"])} '
                            f'<span style="color:{MUTED};font-size:13px">({w["age"]}d; text do {esc(w["id"])})</span></div>'
                            for w in ctx["waiting"])))
        # 4. every finding, with where it stands now
        if ctx["rows"]:
            a(_h2("Everything found today", len(ctx["rows"])))
            t = [f'<table style="width:100%;border-collapse:collapse">'
                 f'<tr><th style="{S_TH};text-align:left">Finding</th><th style="{S_TH};text-align:right">Status</th></tr>']
            for n, r in enumerate(sorted(ctx["rows"], key=lambda r: (_STAND_ORDER[_standing(r)[0]],
                                                                    {"P0": 0, "P1": 1, "P2": 2}.get(r["sev"], 3)))):
                bg = f"background:{ZEBRA};" if n % 2 else ""
                label, col = _standing(r)
                age = "new today" if r["new"] else f'seen {r["seen"]} days'
                t.append(f'<tr><td style="{S_TD};{bg}">{_sev(r["sev"])} {esc(r["headline"])}'
                         f'<div style="font-size:12px;color:{MUTED}">{age}</div></td>'
                         f'<td style="{S_TD};{bg}text-align:right;white-space:nowrap">'
                         f'<div style="font-size:12px;font-weight:600;color:{col}">{label}</div>'
                         f'{id_chip(r["id"]) if r["id"] else ""}</td></tr>')
            t.append("</table>")
            a(_card("".join(t)))
    # 5. numbers
    if d:
        a(_h2("The numbers"))
        rows = [("Net", d.get("net"), w7.get("mean"), w30.get("mean")),
                ("Rewards (modelled)", d.get("rew"), w7.get("rew_mean"), w30.get("rew_mean")),
                ("Trading (marked to last trade)", d.get("pnl"), w7.get("pnl_mean"), w30.get("pnl_mean"))]
        t = [f'<table style="width:100%;border-collapse:collapse"><tr><th style="{S_TH};text-align:left"></th>'
             f'<th style="{S_TH};text-align:right">{esc(_dow(day))}</th>'
             f'<th style="{S_TH};text-align:right">7-day avg</th><th style="{S_TH};text-align:right">30-day avg</th></tr>']
        for lab, x, y, z in rows:
            t.append(f'<tr><td style="{S_TD}">{esc(lab)}</td><td style="{S_TD};text-align:right">{_num(x)}</td>'
                     f'<td style="{S_TD};text-align:right">{_num(y)}</td><td style="{S_TD};text-align:right">{_num(z)}</td></tr>')
        t.append(f'<tr><td style="{S_TD}">Fills / contracts</td><td style="{S_TD};text-align:right">'
                 f'{int(rv._f(d.get("fills")))} / {rv._f(d.get("cts")):,.0f}</td><td style="{S_TD}"></td>'
                 f'<td style="{S_TD}"></td></tr></table>')
        extra = []
        if w30.get("n"):
            tax = f', trading gave back {rv._f(w30.get("tax")) * 100:.0f}% of rewards' if w30.get("tax") is not None else ""
            extra.append(f'<div style="margin-top:10px;color:{MUTED};font-size:13px">30 days: worst day '
                         f'{esc(money(w30.get("worst")))}, max drawdown {esc(money(w30.get("max_dd")))}, '
                         f'{w30.get("green")} of {w30.get("n")} days green{tax}.</div>')
        fams = (ev.get("attribution") or {}).get("families") or []
        if fams:
            ranked = sorted((f for f in fams if abs(rv._f(f["day"]["net"])) >= 1), key=lambda f: -rv._f(f["day"]["net"]))
            best = [f'{f["family"]} {money(f["day"]["net"])}' for f in ranked[:3] if rv._f(f["day"]["net"]) > 0]
            worst = [f'{f["family"]} {money(f["day"]["net"])}' for f in ranked[::-1][:3] if rv._f(f["day"]["net"]) < 0]
            if best:
                extra.append(f'<div style="margin-top:8px"><b style="color:{GREEN}">Best:</b> {inline(", ".join(best))}</div>')
            if worst:
                extra.append(f'<div><b style="color:{RED}">Worst:</b> {inline(", ".join(worst))}</div>')
        a(_card("".join(t) + "".join(extra)))
        edge = (ev.get("moat") or {}).get("edge") or []
        if edge:
            a(_h2("Edge watch (last 7 days vs the 21 before)"))
            et = [f'<table style="width:100%;border-collapse:collapse"><tr>'
                  f'<th style="{S_TH};text-align:left">Family</th><th style="{S_TH};text-align:right">Rewards per $1k resting</th>'
                  f'<th style="{S_TH};text-align:right">Change</th></tr>']
            for n, e in enumerate(edge[:6]):
                bg = f"background:{ZEBRA};" if n % 2 else ""
                lead = rv._f(e.get("lead"))
                col = GREEN if e["verdict"] == "strengthening" else RED
                r_, b_ = e.get("recent") or {}, e.get("base") or {}
                et.append(f'<tr><td style="{S_TD};{bg}">{esc(e["family"])}<div style="font-size:12px;color:{col}">'
                          f'{esc(e["verdict"])}</div></td>'
                          f'<td style="{S_TD};{bg}text-align:right">${rv._f(r_.get("rew_per_1k")):,.2f} vs '
                          f'${rv._f(b_.get("rew_per_1k")):,.2f}</td>'
                          f'<td style="{S_TD};{bg}text-align:right;color:{col};font-weight:600">{(lead - 1) * 100:+.0f}%</td></tr>')
            et.append("</table>")
            a(_card("".join(et)))
    checks = health_checks(ev)
    if checks:
        a(_h2("Health checks"))
        a('<div>' + "".join(_chip(t, c) for t, c in checks) + '</div>')
    if fallback_reason:
        evmd = _read(os.path.join(day_dir(day), "evidence.md"))
        _t, _i, secs = split_review(evmd)
        for head, body in secs:
            a(_h2(head))
            a(_card(md_html(body)))
    foot = [f'{ctx["open"]} open item(s) in the ledger.'] if ctx.get("open") else []
    foot.append("The evidence pack is attached.")
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
    else:
        if ctx["verdict"]:
            L.append(ctx["verdict"])
        L += ["", f"NEEDS YOUR OK ({len(ctx['decisions'])})"]
        for i, r in enumerate(ctx["decisions"], 1):
            L.append(f"{i}. {r['headline']} [{r['sev']}]")
            for k, lab in (("why", ""), ("fix", "Fix: "), ("value", "Worth: ")):
                if r[k]:
                    L.append(f"   {lab}{r[k]}")
            L.append(f"   Approve: text \"do {r['id']}\"")
        if not ctx["decisions"]:
            L.append("Nothing today.")
        crs = ctx.get("change_rows") or []
        if crs:
            L += ["", f"CHANGES MEASURED AT {rv.REGISTRY_DAYS} DAYS ({len(crs)})"]
            for r in pick_changes(crs):
                d = r["delta"] or {}
                L.append(f"- {r['title']} (live {_short_day(r['split'])}): net {money(r['before'])} -> "
                         f"{money(r['after'])}/day ({money(d.get('net'))}) [{r['call']}]"
                         + (f" review: {r['verdict']}" + (f" - {r['note']}" if r["note"] else "") if r["verdict"] else ""))
        comp = ctx.get("compound") or []
        L += ["", f"MAKING THE REVIEW BETTER ({len(comp)})"]
        if self_line(ctx["ev"]):
            L.append(self_line(ctx["ev"]))
        for r in comp:
            L.append(f"- {r['headline']}")
            for k, lab in (("why", ""), ("fix", "Change: "), ("value", "Worth: ")):
                if r[k]:
                    L.append(f"   {lab}{r[k]}")
            L.append("   Done by the review itself" if r.get("self_apply") and not r["decision"]
                     else f"   Build it: text \"do {r['id']}\"")
        if not comp:
            L.append("The review did not answer its daily question today.")
        if ctx["review"]:
            L += ["", ctx["review"]]
    edge = edge_line(ctx["ev"])
    if edge:
        L += ["", "EDGE WATCH: " + edge]
    checks = health_checks(ctx["ev"])
    if checks:
        L += ["", "HEALTH: " + "; ".join(t for t, _ in checks)]
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


def evidence_attachment(day: date) -> Tuple[str, str]:
    md = _read(os.path.join(day_dir(day), "evidence.md"))
    return f"IMM-evidence-{day.isoformat()}.html", mdr.page(f"IMM evidence pack {day.isoformat()}", [mdr.render(md)])


def build(day: date, fallback: bool = False, reason: str = "") -> Tuple[str, str, str, List[Tuple[str, str]]]:
    dd = day_dir(day)
    os.makedirs(dd, exist_ok=True)
    ctx = load_ctx(day)
    has_review = bool(ctx["review"]) and bool(ctx["findings"])
    why = ""
    if fallback or not has_review:
        why = reason or ("no review was written" if not has_review else "fallback requested")
    page = render_html(ctx, why)
    text = render_text(ctx, why)
    subject = subject_for(ctx, why)
    with open(os.path.join(dd, "email.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    return subject, page, text, [evidence_attachment(day)]


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
    if len(page) > 100_000:
        print("warning: over ~100 KB of HTML -- Gmail clips the message there")
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
