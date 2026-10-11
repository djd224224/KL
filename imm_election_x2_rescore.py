#!/usr/bin/env python3
r"""imm_election_x2_rescore.py -- the 10/18 re-score of the elections family
x2 (Jack 2026-10-11: "yes double elections, re-score 10/18 and email me").

THE CHANGE (d218a57, live 2026-10-11 ~13:31Z): ELECTION_SIZE_MULT 2.0 on the
election archetype -- rungs, the per-market cap (100 -> 200) and the
per-event cap doubled for every election series. Revert with
IMM_ELECTION_SIZE_MULT=1 in run_incentive_mm.ps1 + restart_imm.ps1 -Task.

THE YARDSTICK is the dashboard's own per-ET-day family numbers (the model
embedded in run-logs/incentive-mm/dashboard/imm_dashboard.html, rebuilt every
10 min by "KL imm dashboard"): rewards at the family's paid rate, and trading
= realized + the change in marks (the same "pnl" the page and IMM Pocket
show). Elections at x1, the 12 complete days 9/29-10/10: rewards $943,
trading -$518, net +$425 ($35/day, 45% of rewards kept). 10/11 is the
switch-over day and counts on neither side.

WHAT IT REPORTS:
  * net per day before (x1) and after (x2), rewards and trading each, and
    the after/before ratio of each (x2 should roughly double both);
  * the 30-minute mark-out per filled contract, before and after (worse
    after = the extra size is being picked off harder, not just more);
  * settled P&L inside the window (votes: BC local 10/17), shown apart;
  * the book now against the pre-x2 snapshot (markets quoted, positions,
    cost of the inventory, unrealized);
  * a verdict -- KEEP x2 / WATCH / BACK TO x1 -- advisory only.

USAGE:
  python imm_election_x2_rescore.py          # the scheduled run (sends once, on/after 10/18)
  python imm_election_x2_rescore.py --dry    # build and print; no email, no marker
  python imm_election_x2_rescore.py --test   # send now; no marker
SCHEDULE: one-time Windows task "KL imm election-x2-rescore", Sun 2026-10-18
08:10 ET (register_imm_election_x2_rescore.ps1). Marker
election_x2_rescore_sent.marker in the status dir stops a second send.
STRICTLY READ-ONLY apart from the marker. Console output is ASCII.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

KL = os.path.dirname(os.path.abspath(__file__))
if KL not in sys.path:
    sys.path.insert(0, KL)

FAMILY = "Elections"
FIRST_DAY = "2026-09-29"          # the family's first full day at the 1-99c band
SWITCH_DAY = "2026-10-11"         # x2 live ~13:31Z: counted on neither side
SEND_FROM = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)   # 08:00 ET
# the book just before x2 (dashboard build 2026-10-11 09:25 ET)
PRE_BOOK = {"quoted": 97, "positions": 183, "cost": 3159.7, "unreal": -426.5}
MARKER = "election_x2_rescore_sent.marker"
DAY_KEYS = ("rew", "pnl", "fills", "cts", "mk", "mk_cts", "cs")


def num(v):
    return v if isinstance(v, (int, float)) and v == v and abs(v) != float("inf") else 0.0


def load_model(path):
    import imm_pocket
    return imm_pocket.load_model(path)


def family_days(D, family=FAMILY, first=FIRST_DAY):
    """{iso: {rew, pnl, fills, cts, mk, mk_cts, cs, ok}} for the family's ET days."""
    ix = {k: D["day_fields"].index(k) for k in DAY_KEYS}
    out = {}
    for iso in sorted(D["days"]):
        if iso < first:
            continue
        d = D["days"][iso]
        acc = dict.fromkeys(DAY_KEYS, 0.0)
        for t, row in (d.get("m") or {}).items():
            m = D["markets"].get(t)
            if not m or m.get("fam") != family:
                continue
            for k, i in ix.items():
                acc[k] += num(row[i]) if i < len(row) else 0.0
        acc["ok"] = bool(d.get("ok"))
        acc["full"] = num(d.get("e")) - num(d.get("s")) >= 22 * 3600
        out[iso] = acc
    return out


def window(days, isos):
    w = dict.fromkeys(DAY_KEYS, 0.0)
    for iso in isos:
        for k in DAY_KEYS:
            w[k] += days[iso][k]
    n = len(isos)
    w["n"] = n
    w["net"] = w["rew"] + w["pnl"]
    w["net_day"] = w["net"] / n if n else 0.0
    w["rew_day"] = w["rew"] / n if n else 0.0
    w["pnl_day"] = w["pnl"] / n if n else 0.0
    w["keep"] = w["net"] / w["rew"] if w["rew"] > 0 else None
    w["mk_c"] = 100.0 * w["mk"] / w["mk_cts"] if w["mk_cts"] else None
    return w


def book_now(D, family=FAMILY):
    el = [m for m in D["markets"].values() if m.get("fam") == family]
    pos = [m for m in el if m.get("pos")]
    return {"quoted": sum(1 for m in el if (m.get("cur") or {}).get("q")),
            "positions": len(pos),
            "cost": sum(num(m.get("risk")) for m in pos),
            "unreal": sum(num(m.get("u")) for m in pos)}


def split(days, today_iso):
    before = [i for i in days if FIRST_DAY <= i < SWITCH_DAY and days[i]["ok"]]
    after = [i for i in days if SWITCH_DAY < i < today_iso and days[i]["ok"] and days[i]["full"]]
    return before, after


def verdict(b, a):
    if not a["n"]:
        return "NO DATA", "no complete x2 day yet"
    if a["net_day"] < b["net_day"]:
        return ("BACK TO x1", f"net ${a['net_day']:.0f}/day at x2 vs ${b['net_day']:.0f}/day at x1: "
                "the extra size loses more than it earns")
    if a["net_day"] >= 1.25 * b["net_day"] and (a["keep"] or 0) >= 0.30:
        return ("KEEP x2", f"net ${a['net_day']:.0f}/day vs ${b['net_day']:.0f}/day at x1, "
                f"{(a['keep'] or 0):.0%} of rewards kept")
    return ("WATCH", f"net ${a['net_day']:.0f}/day vs ${b['net_day']:.0f}/day at x1 -- better, "
            "but not clearly enough to call; re-check after the Brazil (10/25) and Israel (10/27) votes")


def _m(v):
    return ("-$%.2f" % -v) if v < 0 else ("$%.2f" % v)


def _r(a, b):
    return ("x%.2f" % (a / b)) if b else "n/a"


def render(D, days, before, after, now_book):
    b, a = window(days, before), window(days, after)
    tag, why = verdict(b, a)
    L = []
    L.append(f"ELECTIONS x2 RE-SCORE -- {tag}")
    L.append(why)
    L.append("")
    L.append(f"Dashboard build: {D.get('generated_et')} (per-ET-day family numbers; trading = realized + change in marks)")
    L.append("")
    L.append(f"{'':<22}{'x1 (before)':>16}{'x2 (after)':>16}{'ratio':>9}")
    L.append(f"{'days':<22}{b['n']:>16}{a['n']:>16}")
    L.append(f"{'rewards / day':<22}{_m(b['rew_day']):>16}{_m(a['rew_day']):>16}{_r(a['rew_day'], b['rew_day']):>9}")
    L.append(f"{'trading / day':<22}{_m(b['pnl_day']):>16}{_m(a['pnl_day']):>16}{_r(a['pnl_day'], b['pnl_day']):>9}")
    L.append(f"{'NET / day':<22}{_m(b['net_day']):>16}{_m(a['net_day']):>16}{_r(a['net_day'], b['net_day']):>9}")
    kb = f"{b['keep']:.0%}" if b["keep"] is not None else "n/a"
    ka = f"{a['keep']:.0%}" if a["keep"] is not None else "n/a"
    L.append(f"{'rewards kept':<22}{kb:>16}{ka:>16}")
    L.append(f"{'fills / day':<22}{b['fills'] / max(b['n'], 1):>16.1f}{a['fills'] / max(a['n'], 1):>16.1f}")
    L.append(f"{'contracts / day':<22}{b['cts'] / max(b['n'], 1):>16.0f}{a['cts'] / max(a['n'], 1):>16.0f}"
             f"{_r(a['cts'] / max(a['n'], 1), b['cts'] / max(b['n'], 1)):>9}")
    mb = f"{b['mk_c']:+.2f}c" if b["mk_c"] is not None else "n/a"
    ma = f"{a['mk_c']:+.2f}c" if a["mk_c"] is not None else "n/a"
    L.append(f"{'30m mark-out / ct':<22}{mb:>16}{ma:>16}")
    L.append(f"{'settled P&L (in trading)':<22}{_m(b['cs']):>14}{_m(a['cs']):>16}")
    L.append("")
    L.append("Expectation when x2 was switched on: rewards and trading both ~x2 (our median share of the")
    L.append("scored book was ~0.6%, and whole-rung fills carried all the loss), so net ~x2. A mark-out per")
    L.append("contract clearly worse after than before means the bigger rungs are being picked off harder.")
    L.append("")
    L.append(f"{'BOOK':<22}{'before x2':>16}{'now':>16}")
    L.append(f"{'markets quoted':<22}{PRE_BOOK['quoted']:>16}{now_book['quoted']:>16}")
    L.append(f"{'positions':<22}{PRE_BOOK['positions']:>16}{now_book['positions']:>16}")
    L.append(f"{'inventory cost':<22}{_m(PRE_BOOK['cost']):>16}{_m(now_book['cost']):>16}")
    L.append(f"{'unrealized':<22}{_m(PRE_BOOK['unreal']):>16}{_m(now_book['unreal']):>16}")
    L.append("")
    L.append("DAILY (ET)          rewards    trading        net  fills    cts  30m c/ct")
    for iso in sorted(days):
        d = days[iso]
        side = "x1" if iso in before else ("x2" if iso in after else "--")
        mk = f"{100.0 * d['mk'] / d['mk_cts']:+.2f}" if d["mk_cts"] else "   n/a"
        L.append(f"{iso} {side}  {d['rew']:>9.2f} {d['pnl']:>10.2f} {d['rew'] + d['pnl']:>10.2f}"
                 f" {int(d['fills']):>6} {d['cts']:>6.0f} {mk:>9}")
    L.append("")
    L.append("Revert: IMM_ELECTION_SIZE_MULT=1 in run_incentive_mm.ps1, then restart_imm.ps1 -Task.")
    L.append("Upcoming votes: BC provincial 10/24, Brazil runoff 10/25, Israel 10/27, US general 11/3.")
    return "\n".join(L), tag, b, a


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="build and print; no email, no marker")
    ap.add_argument("--test", action="store_true", help="send now; no marker")
    ap.add_argument("--dash", default="", help="dashboard html (default: the live one)")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    # imm_quote_gaps FIRST: it mirrors the launcher env and the alert
    # credentials (the other morning emails do the same)
    import imm_quote_gaps as _gaps  # noqa: F401
    import incentive_mm as imm
    status = imm.STATUS_DIR
    marker = os.path.join(status, MARKER)
    now = datetime.now(timezone.utc)
    if not (a.dry or a.test):
        if os.path.exists(marker):
            imm.log("[ELECTION-X2] re-score already sent; nothing to do")
            return 0
        if now < SEND_FROM:
            imm.log(f"[ELECTION-X2] not due before {SEND_FROM.isoformat()}; nothing sent")
            return 0
    dash = a.dash or os.path.join(status, "dashboard", "imm_dashboard.html")
    D = load_model(dash)
    age_h = (time.time() - num(D.get("generated"))) / 3600.0
    days = family_days(D)
    today_iso = datetime.now(imm.ET if hasattr(imm, "ET") else timezone.utc).strftime("%Y-%m-%d")
    before, after = split(days, today_iso)
    text, tag, b, aw = render(D, days, before, after, book_now(D))
    if age_h > 2:
        text = (f"!! the dashboard model is {age_h:.1f}h old (KL imm dashboard task stalled?) -- "
                "numbers stop at its build time\n\n") + text
    print(text)
    if a.dry:
        return 0
    subject = (f"IMM elections x2 re-score: {tag} -- net {_m(aw['net_day'])}/day at x2 vs "
               f"{_m(b['net_day'])}/day at x1")
    alerter = imm.Alerter("IMM-ELECTION-X2", live=True)
    if not alerter.enabled:
        imm.log("[ELECTION-X2] cannot send: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, (2 if a.test else 6) + 1):
        ok = alerter.send_message(text, subject=subject)
        if ok:
            break
        imm.log(f"[ELECTION-X2] send attempt {attempt} failed; retrying in 5 min")
        time.sleep(300)
    imm.log(f"[ELECTION-X2] email {'sent' if ok else 'FAILED'}: {subject}")
    if ok and not a.test:
        with open(marker, "w") as f:
            f.write(now.isoformat() + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
