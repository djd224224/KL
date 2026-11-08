#!/usr/bin/env python3
"""The market-wave alerts, in ONE layout (Jack 2026-11-08: "make sure my texts /
email alerts on market waves are clear: X # of events greenlit or denied,
reasoning, 10 names"). The overnight check (scheduled task
market-wave-overnight-check) used to write each text / push / email by hand;
it now runs this, so every alert reads the same:

  line 1   what happened + the counts: "WAVE w1108-x LIVE: 14 events greenlit, 23 denied"
  line 2   why (greenlit: the rule they met; denied: the reasons, by count)
  1..10    10 event names, greenlit first (+), then denied (x)

    # a new wave (wave_detect.py's --json): outcome live / not-live / needs-you / error
    python wave_alert.py wave --detect run-logs/pvd-scan/wave_detect_last.json \
        --outcome live --wave w1108-x [--listed 95 --est-day 66] [--report PATH] [--send]
    python wave_alert.py wave --detect ... --outcome not-live --why-not "shard-0 cash $3.2k < $5k" --send
    # a relight (relight_watch.py's --json)
    python wave_alert.py relight --relight run-logs/pvd-scan/relight_watch_last.json [--send]
    # pick-offs on wave markets (pvd_waves.py pickoffs --json): texts at $100+ new,
    # a series trip or a stand-down; email whenever something new was flagged
    python wave_alert.py pickoff --pickoffs run-logs/pvd-scan/pickoffs_last.json [--send]

Without --send it prints TEXT, PUSH, SUBJECT and BODY. With --send it texts
(sms_alert.py, capped; only for a live / not-live wave, or a relight of 100+
markets or a 1,000+ program jump), emails jackdu224@gmail.com
(send_alert_email.py) and prints the PUSH line for the task's PushNotification.
Read-only otherwise (a relight's names take one /markets read).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LIST = os.path.join(HERE, "run-logs", "incentive-mm", "post_vote_dead_books.json")
ALERT_TO = "jackdu224@gmail.com"
N_NAMES = 10
NAME_CHARS = 60          # one name in the text
TEXT_CHARS = 900         # sms_alert.py --long (T-Mobile delivers a long one as MMS)
DENIED = ("FEW", "CHECK", "SOON")
TUNE_NOTE = "Reply in a Claude session to tune: the tune menu is at the end of the report."


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


_FOLD = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
         "…": "...", " ": " "}


def ascii_text(s: str) -> str:
    """Plain ASCII for the carrier gateway (curly quotes, dashes, accents folded)."""
    import unicodedata
    s = "".join(_FOLD.get(ch, ch) for ch in (s or ""))
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def short(s: str, n: int = NAME_CHARS) -> str:
    s = " ".join(ascii_text(s).split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def event_titles(events, cap: int = 2 * N_NAMES) -> dict:
    """Kalshi's own event titles for the names shown (one read each, at most
    `cap`); a miss keeps the detector's market title."""
    out = {}
    try:
        sys.path.insert(0, HERE)
        from kalshi_reads import kalshi_get
    except Exception:  # noqa: BLE001
        return out
    for e in list(dict.fromkeys(events))[:cap]:
        try:
            ev = (kalshi_get(f"/events/{e}") or {}).get("event") or {}
        except Exception:  # noqa: BLE001
            continue
        t = (ev.get("title") or "").strip()
        sub = (ev.get("sub_title") or "").strip()
        if t:   # a short subtitle is a date ("Before May 1, 2027"); a long one repeats the title
            keep = sub and len(sub) <= 22 and "(" not in t and sub.lower() not in t.lower()
            out[e] = t + (f" ({sub})" if keep else "")
    return out


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 or word.endswith('s') else 's'}"


def text_name(s: str) -> str:
    """A name for the text: no leading "Will ", ASCII, cut at NAME_CHARS."""
    s = " ".join(ascii_text(s).split())
    if s.startswith("Will "):
        s = s[5:6].upper() + s[6:]
    if len(s) > NAME_CHARS and s.endswith(")") and " (" in s:
        s = s[: s.rindex(" (")]                 # the date suffix goes first; the email keeps it
    return short(s)


def deny_reason(r: dict) -> str:
    """One short reason per denied series, as the detector decided it."""
    v = r["verdict"]
    if v == "FEW":
        return "under 5 events in their series"
    if v == "SOON":
        return "settles within a day"
    why = [w for w in r.get("why") or [] if w]
    for w in why:
        if w.startswith("category "):
            return w[len("category "):]          # "Sports", "Crypto", ...
    if any("mention" in w for w in why):
        return "mention series"
    if any("blocklisted" in w for w in why):
        return "blocklisted"
    return "; ".join(why) or "needs your call"


def names_of(rows, mark: str):
    out = []
    for r in rows:
        for e in r.get("event_list") or []:
            out.append({"mark": mark, "name": e.get("name") or e["event"], "event": e["event"],
                        "series": r["series"], "pool_day": e.get("pool_day") or 0.0})
    return sorted(out, key=lambda x: -x["pool_day"])


def wave_msg(d: dict, outcome: str, wave: str, why_not: str, listed, est_day, fetch_titles: bool = True):
    rows = d.get("series") or []
    new_q = set(d.get("new_fit_series") or []) | set(d.get("new_near_series") or [])
    new_ask = set(d.get("new_check_series") or [])
    if outcome == "needs-you":
        green = []
        held = [r for r in rows if r["series"] in new_ask]
    else:
        green = [r for r in rows if r["series"] in new_q]
        held = []
    denied = [r for r in rows if r["verdict"] in DENIED and r not in held]
    already = [r for r in rows if r["verdict"] in ("LISTED", "QUOTED", "MEMBER")]
    g_ev = sum(r["events"] for r in green)
    g_mk = sum(r["markets"] for r in green)
    g_pool = sum(r["pool_day"] for r in green)
    h_ev = sum(r["events"] for r in held)
    d_ev = sum(r["events"] for r in denied)
    name = wave or "(unnamed)"

    # line 1
    if outcome == "live":
        head = f"WAVE {name} LIVE: {plural(g_ev, 'event')} greenlit, {d_ev:,} denied"
    elif outcome == "not-live":
        head = f"WAVE {name} NOT LIVE: {plural(g_ev, 'event')} passed but not quoted, {d_ev:,} denied"
    elif outcome == "needs-you":
        head = f"WAVE? Your call: {plural(h_ev, 'event')} held for you, 0 greenlit, {d_ev:,} denied"
    else:
        head = f"WAVE check ERROR: {why_not or 'see the email'}"
    size = (f"{listed:,} mkts listed" if listed is not None else f"{g_mk:,} mkts") \
        + (f", ~${est_day:,.0f}/day est" if est_day is not None else f", ${g_pool:,.0f}/day pools")

    # line 2: the reasons
    gw = []
    fit = [r for r in green if r["verdict"] == "FIT"]
    near = [r for r in green if r["verdict"] == "NEAR"]
    if fit:
        gw.append(f"{sum(r['events'] for r in fit)} have 5+ events in one series, settle 14d+ out, safe category")
    if near:
        gw.append(f"{sum(r['events'] for r in near)} settle in 1-14d (base size, no multipliers)")
    by = collections.Counter()
    for r in denied:
        by[deny_reason(r)] += r["events"]
    dw = [f"{n} {why}" for why, n in by.most_common()]
    hb = collections.Counter()
    for r in held:
        hb[deny_reason(r)] += r["events"]
    hw = [f"{n} {why}" for why, n in hb.most_common()]
    why_lines = []
    if outcome == "not-live" and why_not:
        why_lines.append(f"Not live because: {why_not}")
    if gw:
        why_lines.append(("Passed because: " if outcome == "not-live" else "Greenlit because: ") + "; ".join(gw))
    if hw:
        why_lines.append("Held for you because: " + "; ".join(hw))
    if dw:
        why_lines.append("Denied because: " + "; ".join(dw))
    if not why_lines:
        why_lines.append("Why: nothing new cleared the bar")

    # the 10 names: greenlit (or held) first, then denied
    first = names_of(green or held, "+" if green else "?")
    rest = names_of(denied, "x")
    names = (first + rest)[:N_NAMES]
    shown = names + (rest[:N_NAMES] if len(first) >= N_NAMES else [])
    titles = event_titles([n["event"] for n in shown]) if fetch_titles else {}
    for n in first + rest:
        n["name"] = titles.get(n["event"]) or n["name"]

    text_lines = [head + (f" ({size})" if green else "")] + why_lines
    text_lines += [f"{n['mark']}{i}. {text_name(n['name'])}" for i, n in enumerate(names, 1)]
    text = "\n".join(text_lines)
    if len(text) > TEXT_CHARS:
        text = text[: TEXT_CHARS - 3] + "..."
    push = short(head + (f" ({size})" if green else ""), 150)

    # email
    b = [head, f"{size}" if green else ""] + why_lines + [""]
    b.append(f"Checked {d.get('at')} for programs since {d.get('since')}: "
             f"{plural(d.get('programs') or 0, 'new program')}, {plural(len(rows), 'series')}.")
    b.append("")
    label = {"live": "GREENLIT (quoting now)", "not-live": "PASSED, NOT LIVE", "needs-you": "HELD FOR YOUR CALL"}
    if green or held:
        grp = green or held
        b.append(f"{label.get(outcome, 'GREENLIT')}: {plural(sum(r['events'] for r in grp), 'event')} "
                 f"in {plural(len(grp), 'series')}, {sum(r['markets'] for r in grp):,} markets, "
                 f"${sum(r['pool_day'] for r in grp):,.0f}/day of pools")
        if outcome == "not-live" and why_not:
            b.append(f"  Why not live: {why_not}")
        for r in sorted(grp, key=lambda r: -r["pool_day"]):
            rule = ("5+ events, settles 14d+ out, safe category" if r["verdict"] == "FIT" else
                    "settles in 1-14d: base size, no multipliers" if r["verdict"] == "NEAR" else deny_reason(r))
            b.append(f"  {r['series']} ({r['verdict']}): {plural(r['events'], 'event')}, {r['markets']} mkts, "
                     f"${r['pool_day']:,.0f}/day, settles {(r.get('median_settle') or '?')[:10]} -- {rule}"
                     + (f" -- {short(r.get('title') or '', 60)}" if r.get("title") else ""))
        b.append("")
    if denied:
        b.append(f"DENIED: {plural(d_ev, 'event')} in {plural(len(denied), 'series')}")
        for why, n in by.most_common():
            ss = sorted((r for r in denied if deny_reason(r) == why), key=lambda r: -r["pool_day"])
            b.append(f"  {n} {why}: " + ", ".join(f"{r['series']} ({r['events']})" for r in ss[:12])
                     + (f", +{len(ss) - 12} more" if len(ss) > 12 else ""))
        b.append("")
    if already:
        b.append("ALREADY HANDLED (not counted): " + ", ".join(
            f"{r['series']} {r['verdict'].lower()}" for r in already[:15])
            + (f", +{len(already) - 15} more" if len(already) > 15 else ""))
        b.append("")
    b.append(f"{N_NAMES} NAMES ({'+ greenlit' if green else '? held' if held else ''}"
             f"{', x denied' if rest else ''}, richest pools first)")
    for i, n in enumerate(names, 1):
        b.append(f"  {n['mark']}{i}. {ascii_text(n['name'])}  [{n['event']}, ${n['pool_day']:,.0f}/day]")
    if rest and len(first) >= N_NAMES:
        b.append("  Denied, top names:")
        for n in rest[:N_NAMES]:
            b.append(f"    x {ascii_text(n['name'])}  [{n['event']}, {n['series']}]")
    subject = "market-wave: " + push
    return text, push, subject, "\n".join(x for x in b if x is not None)


def relight_names(r: dict):
    """Events the relight tasks listed in the window (the list's "added" stamps),
    grouped by Kalshi's event ticker, richest first, with Kalshi's event titles."""
    since = (r.get("since") or "").replace(" ", "T")[:16]
    until = (r.get("until") or "").replace(" ", "T")[:16]
    try:
        mk = load(LIST).get("markets") or {}
    except (OSError, ValueError):
        return []
    tick = []
    for t, e in mk.items():
        a = str(e.get("added") or "")
        if a.startswith("scan ") and since < a[5:21] <= until:   # "scan 2026-11-07T03:36Z (new; ...)"
            tick.append(t)
    recs = {}
    try:
        sys.path.insert(0, HERE)
        from kalshi_reads import kalshi_get
        for k in range(0, len(tick), 100):
            for m in (kalshi_get("/markets", {"tickers": ",".join(tick[k:k + 100])}) or {}).get("markets") or []:
                recs[m.get("ticker")] = m
    except Exception:  # noqa: BLE001  (names fall back to tickers)
        pass
    ev = collections.OrderedDict()
    for t in tick:
        m = recs.get(t) or {}
        e = m.get("event_ticker") or t.rsplit("-", 1)[0]
        g = ev.setdefault(e, {"event": e, "name": (m.get("title") or "").strip() or e, "markets": 0})
        g["markets"] += 1
    out = sorted(ev.values(), key=lambda g: -g["markets"])
    titles = event_titles([g["event"] for g in out[:N_NAMES]])
    for g in out:
        g["name"] = titles.get(g["event"]) or g["name"]
    return out


def relight_msg(r: dict):
    n_mk, usd = r.get("listed") or 0, r.get("usd_day") or 0
    p0, p1, jump = r.get("programs_before"), r.get("programs_now"), r.get("programs_jump") or 0
    evs = relight_names(r)
    # each list's latest pass only: the same wave's 15-min passes skip the same markets again
    last_skip = {}
    for p in r.get("passes") or []:
        if p.get("skipped") is not None:
            last_skip[p["where"]] = p["skipped"]
    skipped = collections.Counter()
    for sk in last_skip.values():
        skipped.update(sk)
    d_mk = sum(skipped.values())
    named = sum(e["markets"] for e in evs)
    g = (f"{plural(len(evs), 'event')} ({n_mk:,} mkts)" if evs and named == n_mk
         else plural(n_mk, "market"))
    head = (f"RELIGHT: {g} greenlit, {d_mk:,} mkts denied (~${usd:,.0f}/day)"
            if n_mk else f"RELIGHT: programs {p0} -> {p1} (+{jump:,}), 0 greenlit")
    tot = collections.Counter()
    for p in r.get("passes") or []:
        tot.update({"new": p["new"], "relit": p["relit"], "extended": p["extended"]})
    whys = [f"Greenlit because: pool ROI over the $1.10/day bar (new {tot['new']}, relit {tot['relit']}, "
            f"extended {tot['extended']})"] if n_mk else []
    if d_mk:
        whys.append("Denied because: " + "; ".join(f"{v} {k}" for k, v in skipped.most_common() if v))
    if jump:
        whys.append(f"Programs {p0} -> {p1} (+{jump:,})")
    why = "\n".join(whys)
    lines = [head] + whys + [f"+{i}. {text_name(e['name'])}" for i, e in enumerate(evs[:N_NAMES], 1)]
    if evs and named < n_mk:
        lines.append(f"(+{n_mk - named} mkts no longer on the list)")
    text = "\n".join(lines)
    if len(text) > TEXT_CHARS:
        text = text[: TEXT_CHARS - 3] + "..."
    body = [head, why, "", f"{N_NAMES} NAMES (+ listed)"]
    body += [f"  +{i}. {ascii_text(e['name'])}  [{e['event']}, {plural(e['markets'], 'mkt')}]"
             for i, e in enumerate(evs[:N_NAMES], 1)]
    if len(evs) > N_NAMES:
        body.append(f"  +{len(evs) - N_NAMES} more events")
    body += ["", "Passes:"] + [f"  {p['ts']} {p['where']}: {p['taken']} listed (relit {p['relit']}, new {p['new']}, "
                               f"extended {p['extended']}), ~${p['usd_day']:,.0f}/day"
                               + (f"; skipped {p['skipped']}" if p.get("skipped") else "")
                               for p in r.get("passes") or []]
    big = n_mk >= 100 or jump >= 1000
    return text, short(head, 150), "market-wave relight: " + short(head, 150), "\n".join(body), big


PICKOFF_TEXT_USD = 100.0      # text a pick-off alert at $100+ new, a series trip or a stand-down


def pickoff_msg(r: dict):
    """A pick-off alert from `pvd_waves.py pickoffs --json` (Jack 2026-11-08: "the
    pickoffs yesterday were unacceptable"). Line 1: how many new, $, stood down."""
    new, flagged = r.get("new") or [], r.get("flagged") or []
    acted, trips = r.get("acted") or [], r.get("series_trips") or []
    new_usd = sum(float(g.get("adverse_usd") or 0) for g in new)
    head = (f"PICK-OFF: {plural(len(new), 'wave mkt')} -${new_usd:,.0f} new "
            f"({len(flagged)} flagged, -${float(r.get('flagged_usd') or 0):,.0f} in {r.get('hours', 0):g}h)"
            + (f"; {len(acted)} stood down at base" if acted else ""))
    p = r.get("params") or {}
    whys = [f"Why: adding fills now {p.get('min_c', 5):g}c+ and ${p.get('min_usd', 25):g}+ against us, "
            f"marked to the book with our orders out"]
    if trips:
        whys.append("Series trip: " + ", ".join(f"{t['series']} ({len(t['markets'])} mkts)" for t in trips)
                    + " -- stand the series down")
    leg = [g for g in flagged if not g.get("registry")]
    if leg:
        whys.append(f"{plural(len(leg), 'flagged mkt')} on legacy waves: a hand stand-down (your word)")

    def one(g):
        verb = "bought" if g["side"] == "bid" else "sold"
        return (f"{g['ticker']} {verb} {g['contracts']:,.0f} @{g['vwap_c']:g}, mark {g['mark_c']:.1f} "
                f"(-${g['adverse_usd']:,.0f})")
    rows = sorted(new or flagged, key=lambda g: float(g.get("adverse_usd") or 0), reverse=True)
    lines = [head] + whys + [f"x{i}. {one(g)}" for i, g in enumerate(rows[:N_NAMES], 1)]
    lines += [f"DOWN {x.get('ticker') or 'series ' + x.get('series', '')} at {x['pin']} a side ({x['wave']})"
              for x in acted[:5]]
    text = "\n".join(lines)
    if len(text) > TEXT_CHARS:
        text = text[: TEXT_CHARS - 3] + "..."
    body = [head] + whys + ["", "FLAGGED (marked to the book, our orders out):"]
    body += [f"  {'NEW ' if g in new else '    '}{one(g)}  [{g['wave']}{'' if g.get('registry') else ', legacy'}, "
             f"held {g.get('pos_now', 0):+,.0f}, last fill {str(g.get('last'))[:16]}Z]" for g in flagged]
    body += ["", "Stood down: " + (", ".join(f"{x.get('ticker') or x.get('series')} at {x['pin']}" for x in acted)
                                   or "nothing (legacy waves need your word; registry waves only with --act)")]
    body += ["", "Undo a stand-down: python pvd_waves.py setarg <WAVE> pin <TICKER>:<N> --unset ... or ask a Claude session."]
    do_text = new_usd >= PICKOFF_TEXT_USD or bool(trips) or bool(acted)
    return text, short(head, 150), "market-wave pick-off: " + short(head, 150), "\n".join(body), do_text


ALERTS_LOG = os.path.join(HERE, "run-logs", "pvd-scan", "alerts_sent.jsonl")


def send(text, subject, body, do_text: bool) -> int:
    # the log a text reply reads for context (sms_inbox.py / phone_actions.py alerts)
    try:
        with open(ALERTS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "kind": subject.split(":", 1)[0], "texted": do_text, "text": text,
                                 "subject": subject}) + "\n")
    except OSError:
        pass
    rc = 0
    if do_text:
        p = subprocess.run([sys.executable, os.path.join(HERE, "sms_alert.py"), "--long", text],
                           capture_output=True, text=True)
        print(f"text: {(p.stdout or p.stderr).strip()} (exit {p.returncode})")
        rc = p.returncode if p.returncode not in (0, 2) else 0   # 2 = capped: fine
    env = dict(os.environ, ALERT_EMAIL_TO=ALERT_TO)
    p = subprocess.run([sys.executable, os.path.join(HERE, "send_alert_email.py"), subject, body],
                       capture_output=True, text=True, env=env)
    print(f"email: {(p.stdout or p.stderr).strip()} (exit {p.returncode})")
    return rc or p.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="kind", required=True)
    w = sub.add_parser("wave")
    w.add_argument("--detect", required=True)
    w.add_argument("--outcome", required=True, choices=("live", "not-live", "needs-you", "error"))
    w.add_argument("--wave", default="")
    w.add_argument("--why-not", default="", help="not-live / error: the reason, in a few words")
    w.add_argument("--listed", type=int, help="markets the scan listed")
    w.add_argument("--est-day", type=float, help="the report's estimated $/day")
    w.add_argument("--report", help="the wave report (markdown), appended to the email")
    w.add_argument("--send", action="store_true")
    w.add_argument("--no-titles", action="store_true", help="skip the event-title reads (tests)")
    rl = sub.add_parser("relight")
    rl.add_argument("--relight", required=True)
    rl.add_argument("--send", action="store_true")
    po = sub.add_parser("pickoff", help="from pvd_waves.py pickoffs --json: texts at $100+ new, a series trip or a stand-down")
    po.add_argument("--pickoffs", required=True)
    po.add_argument("--send", action="store_true")
    a = ap.parse_args()

    if a.kind == "wave":
        try:
            d = load(a.detect)
        except (OSError, ValueError) as ex:
            d, a.outcome, a.why_not = {}, "error", a.why_not or f"no detector output ({ex})"
        text, push, subject, body = wave_msg(d, a.outcome, a.wave, a.why_not, a.listed, a.est_day,
                                             fetch_titles=not a.no_titles)
        if a.report:
            try:
                with open(a.report, encoding="utf-8") as fh:
                    body += "\n\n----- WAVE REPORT -----\n" + fh.read()
            except OSError as ex:
                body += f"\n\n(report not readable: {ex})"
        body += "\n\n" + TUNE_NOTE
        do_text = a.outcome in ("live", "not-live")
    elif a.kind == "pickoff":
        text, push, subject, body, do_text = pickoff_msg(load(a.pickoffs))
    else:
        text, push, subject, body, do_text = relight_msg(load(a.relight))

    print("TEXT" + ("" if do_text else " (not sent: texts are for waves / big relights only)")
          + f" [{len(text)} chars]:\n" + text)
    print("\nPUSH:\n" + push)
    print("\nSUBJECT:\n" + subject)
    print("\nBODY:\n" + body)
    if a.send:
        print("\n--- sending ---")
        return send(text, subject, body, do_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
