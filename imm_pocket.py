#!/usr/bin/env python3
"""IMM Pocket: the IMM dashboard's top section and families table, small
enough to publish to claude.ai and read on a phone.

Reads the model the 10-minute build embeds in imm_dashboard.html (const D)
and adds it up exactly as the page does (aggregate / addM / derive /
baseline in imm_dashboard_template.html): per window, the hero, the tiles,
the cumulative curve and one row per family; and, as the dashboard's header
box, the whole account (account()). Writes imm_pocket.json and
imm_pocket.html (imm_pocket_template.html with the data embedded) beside the
dashboard.

The page is published to claude.ai as "IMM Pocket"
(https://claude.ai/artifact/4qqWauCV23mEMCUT1g8Lj9). It refreshes on request
only: the imm-pocket skill (~/.claude/skills/imm-pocket) runs this script,
then ArtifactData sets the page's database document pocket/latest from
imm_pocket.json. The page shows whichever build is newer, that one or the
one embedded at publish. imm_pocket_publish.py republishes the page itself
(template changes, or a session without ArtifactData).

    python imm_pocket.py [--dash path\\imm_dashboard.html] [--out dir] [--template path] [--summary]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                               # pragma: no cover
    ET = timezone(timedelta(hours=-4))

HERE = os.path.dirname(os.path.abspath(__file__))
DASH_DIR = os.path.join(HERE, "run-logs", "incentive-mm", "dashboard")
DASH = os.path.join(DASH_DIR, "imm_dashboard.html")
TEMPLATE = os.path.join(HERE, "imm_pocket_template.html")
WINS = ["today", "yesterday", "24h", "7d"]
SUM_KEYS = ["rew", "pnl", "real", "du", "fills", "cts", "usd", "mk", "mk_n", "mk_cts", "rest",
            "cr", "cs", "fe", "fe_cts", "m5", "m5_cts", "m4", "m4_cts", "mt", "mt_cts"]
CURVE_MAX = 144
# Top events per family (Jack 2026-10-12: "when I click into a family, I
# should be able to see the top events contributing"): the TOP_EVENTS
# biggest by |net| in the window, the rest summed in one row so the list ties
# to the family row.
TOP_EVENTS = 8
# Event titles: the dashboard's cache (events with a live program) first,
# then this file's own cache, filled from Kalshi's public GET /events/{ticker}
# at most TITLE_FETCH_BUDGET per build ("" = looked up, no title).
DASH_TITLES = os.path.join(DASH_DIR, "cache", "event_titles.json")
TITLE_CACHE = os.path.join(DASH_DIR, "cache", "pocket_event_titles.json")
TITLE_FETCH_BUDGET = 60
KALSHI_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"


def num(v):
    return v if isinstance(v, (int, float)) and v == v and abs(v) != float("inf") else 0.0


def load_model(path):
    with open(path, encoding="utf-8") as f:
        s = f.read()
    i = s.index("const D = ") + len("const D = ")
    model, _ = json.JSONDecoder().raw_decode(s, i)
    return model


def blank():
    d = dict.fromkeys(SUM_KEYS, 0.0)
    d.update(nset=0, pset=0.0, nq=0, pnl7=0.0, q2=0, q1=0, held=0, restNow=0.0, estNow=0.0,
             u=0.0, risk=0.0, npos=0, wc=0.0)
    return d


def add_market(acc, m, wkey):
    w = (m.get("w") or {}).get(wkey)
    if w:
        for k in SUM_KEYS:
            acc[k] += num(w.get(k))
        if w.get("settled"):
            acc["nset"] += 1
            acc["pset"] += num(w.get("pnl"))
        if num(w.get("rest")) > 0:
            acc["nq"] += 1
    w7 = (m.get("w") or {}).get("7d")
    if w7:
        acc["pnl7"] += num(w7.get("pnl"))
    c = m.get("cur")
    if c:
        if c.get("q") == 2:
            acc["q2"] += 1
        elif c.get("q") == 1:
            acc["q1"] += 1
        acc["restNow"] += num(c.get("usd"))
        acc["estNow"] += num(c.get("est_day"))
    if m.get("sel") and (not c or not c.get("q")) and (m.get("guard") or m.get("halt")):
        acc["held"] += 1
    if m.get("pos"):
        acc["u"] += num(m.get("u"))
        acc["risk"] += num(m.get("risk"))
        acc["npos"] += 1


def eod_iso(D, wkey):
    """The page shows yesterday's book columns at that day's close."""
    if wkey != "yesterday":
        return None
    s = D["windows"]["yesterday"]["start"]
    for iso, d in D["days"].items():
        if abs(d["s"] - s) < 2:
            return iso
    return None


def fam_daily(D):
    ir, ip = D["day_fields"].index("rew"), D["day_fields"].index("pnl")
    out = {}
    for iso in sorted(D["days"]):
        d = D["days"][iso]
        f = defaultdict(float)
        for t, row in (d.get("m") or {}).items():
            m = D["markets"].get(t)
            if m:
                f[m["fam"]] += num(row[ir]) + num(row[ip])
        out[iso] = {"ok": bool(d.get("ok")), "s": d["s"], "e": d["e"], "f": f}
    return out


def typical(FD, fam, start):
    """Mean net per complete ET day over the 7 days that ended before `start`
    (the page's Typical day, 7-day figure); None under 3 days."""
    vals = []
    for iso in sorted(FD, reverse=True):
        b = FD[iso]
        if not b["ok"] or b["e"] > start + 1 or b["e"] - b["s"] < 22 * 3600:
            continue
        vals.append(sum(v for f, v in b["f"].items() if fam is None or f == fam))
        if len(vals) == 7:
            break
    return round(sum(vals) / len(vals), 2) if len(vals) >= 3 else None


def r2(d):
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}


def thin(points, n=CURVE_MAX):
    if len(points) <= n:
        return points
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]


def top_events(evs, quoting):
    """(rows, rest) for one family in one window: the TOP_EVENTS events with
    the biggest |net|, each {e, rew, pnl, net, fills, q}, and the remainder
    summed as {n, rew, pnl, net, fills} (None when nothing is left over)."""
    live = [(e, a) for e, a in evs.items()
            if abs(a["rew"]) >= 0.005 or abs(a["pnl"]) >= 0.005 or a["fills"]]
    live.sort(key=lambda x: (-abs(x[1]["rew"] + x[1]["pnl"]), -x[1]["rew"]))
    rows = [r2({"e": e, "rew": a["rew"], "pnl": a["pnl"], "net": a["rew"] + a["pnl"],
                "fills": int(a["fills"]), "q": quoting.get(e, 0)}) for e, a in live[:TOP_EVENTS]]
    left = live[TOP_EVENTS:]
    rest = None
    if left:
        rew = sum(a["rew"] for _e, a in left)
        pnl = sum(a["pnl"] for _e, a in left)
        rest = r2({"n": len(left), "rew": rew, "pnl": pnl, "net": rew + pnl,
                   "fills": int(sum(a["fills"] for _e, a in left))})
    return rows, rest


def load_json_file(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def kalshi_title(e):
    """Kalshi's title for a dashboard event key: GET /events/{e}; on a 404 the
    parent event (a key like KXBRPRES2MOV-26OCT25-FBOL is one market's
    ticker), then the series (series-level keys like KXBTCVSGOLD). '' when
    none exists; raises on anything but a 404 (rate limit, network)."""
    paths = [f"/events/{e}"]
    if e.count("-") >= 2:
        paths.append(f"/events/{e.rsplit('-', 1)[0]}")
    paths.append(f"/series/{e.split('-')[0]}")
    for p in paths:
        try:
            with urllib.request.urlopen(KALSHI_PUBLIC + p, timeout=5) as r:
                d = json.load(r) or {}
        except urllib.error.HTTPError as err:
            if err.code == 404:
                continue
            raise
        t = str((d.get("event") or d.get("series") or {}).get("title") or "").strip()
        if t:
            return t
    return ""


def event_titles(evs, fetch=True):
    """{event: title} for `evs`: the dashboard's title cache, this file's own
    cache, then Kalshi's public endpoints for up to TITLE_FETCH_BUDGET misses
    (a rate-limited or failed read stops the fetching; the page shows the
    ticker instead)."""
    dash = load_json_file(DASH_TITLES, {})
    cache = load_json_file(TITLE_CACHE, {})
    out, miss = {}, []
    for e in sorted(evs):
        t = dash.get(e) or cache.get(e)
        if t:
            out[e] = t
        elif e not in cache:
            miss.append(e)
    fetched = 0
    if fetch:
        for e in miss[:TITLE_FETCH_BUDGET]:
            try:
                cache[e] = kalshi_title(e)
            except Exception:
                break
            fetched += 1
            if cache[e]:
                out[e] = cache[e]
    if fetched:
        try:
            os.makedirs(os.path.dirname(TITLE_CACHE), exist_ok=True)
            with open(TITLE_CACHE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(TITLE_CACHE + ".tmp", TITLE_CACHE)
        except OSError:
            pass
    return out


def account(D):
    """The account box, as the dashboard's header shows it (Jack 2026-10-29:
    "Make sure IMM pocket refresh also has the account box"): from the
    build's live account read (D.acct, the portfolio email's own numbers),
    the account value and its move since the newest 7am snapshot, split by
    bot; with no live read, the last 7am email's day. None without either
    (a build from before 2026-10-30)."""
    a = D.get("acct") or {}
    s, live = a.get("live"), True
    if not s:
        em = a.get("email") or []
        s, live = (em[-1] if em else None), False
    if not s or s.get("value") is None:
        return None
    return {"live": live, "at": a.get("at") if live else s.get("until"),
            "since": s.get("since"), "until": s.get("until"), "value": s.get("value"),
            "app": s.get("app"), "profit": s.get("profit"), "day": s.get("day"),
            "bots": {b[0]: round(num(b[1]), 2) for b in s.get("bots") or []},
            "resid": s.get("resid"), "err": a.get("err")}


def account_line(ac):
    """The account for the refresh reply: value, then the move since 7am by bot."""
    if not ac:
        return None
    since = ("" if not ac.get("since") else
             datetime.fromtimestamp(ac["since"], ET).strftime(" since %a %I:%M %p").replace(" 0", " "))
    bots = ac.get("bots") or {}
    out = f"account ${num(ac['value']):,.0f}"
    if ac.get("day") is not None:
        out += (f", {_usd(num(ac['day']))}{since}" + (" (the 7am email's day)" if not ac["live"] else "")
                + ": " + ", ".join(f"{b.lower() if b != 'IMM' else b} {_usd(num(bots.get(b)))}"
                                   for b in ("Crypto", "Weather", "Other", "IMM") if b in bots))
    return out


def build(D, fetch_titles=True):
    gen = D["generated"]
    FD = fam_daily(D)
    w7 = D["windows"].get("7d") or {}
    days7 = max((w7.get("end", gen) - w7.get("start", gen - 7 * 86400)) / 86400, 1)
    events_now = D.get("events") or {}

    # event -> quoting now (any market resting a quote); markets quoting per event
    ev_q = defaultdict(bool)
    ev_nq = defaultdict(int)
    for m in D["markets"].values():
        if (m.get("cur") or {}).get("q"):
            ev_q[m["ev"]] = True
            ev_nq[m["ev"]] += 1
    shown = set()

    out = {"generated": gen, "generated_et": D.get("generated_et"), "windows": {}, "curves": {}}
    for wkey in WINS:
        w = D["windows"].get(wkey)
        if not w:
            continue
        fams, tot = {}, blank()
        fam_ev = defaultdict(dict)        # family -> event -> window sums
        for m in D["markets"].values():
            f = fams.setdefault(m["fam"], blank())
            add_market(f, m, wkey)
            add_market(tot, m, wkey)
            mw = (m.get("w") or {}).get(wkey)
            if mw:
                a = fam_ev[m["fam"]].setdefault(m["ev"], {"rew": 0.0, "pnl": 0.0, "fills": 0.0})
                a["rew"] += num(mw.get("rew"))
                a["pnl"] += num(mw.get("pnl"))
                a["fills"] += num(mw.get("fills"))
        iso = eod_iso(D, wkey)
        wc_src = (D["days"].get(iso) or {}).get("wc") or {} if iso else None
        seen = set()
        for m in D["markets"].values():
            if m["ev"] in seen:
                continue
            seen.add(m["ev"])
            wc = wc_src.get(m["ev"]) if wc_src is not None else (events_now.get(m["ev"]) or {}).get("wc")
            if wc:
                fams[m["fam"]]["wc"] += num(wc[0])
                tot["wc"] += num(wc[0])
        qev = defaultdict(int)
        for ev, q in ev_q.items():
            if q:
                qev[(events_now.get(ev) or {}).get("fam")] += 1
        rows = []
        for name, f in fams.items():
            nf = f["pnl"] - f["cr"] - f["cs"]
            if not (abs(f["rew"]) >= 0.005 or abs(f["pnl"]) >= 0.005 or f["fills"] or f["q1"] + f["q2"]
                    or f["npos"] or abs(f["wc"]) >= 0.005):
                continue
            ev_rows, ev_rest = top_events(fam_ev.get(name, {}), ev_nq)
            shown.update(r["e"] for r in ev_rows)
            rows.append(dict(r2({
                "name": name, "rew": f["rew"], "pnl": f["pnl"], "net": f["rew"] + f["pnl"], "nf": nf,
                "cr": f["cr"], "cs": f["cs"], "fills": int(f["fills"]), "cts": f["cts"],
                "mk": f["mk"], "mk_cts": f["mk_cts"], "fe": f["fe"], "fe_cts": f["fe_cts"],
                "mt": f["mt"], "mt_cts": f["mt_cts"], "cov": (f["rew"] / -nf) if nf < -0.005 else None,
                "wc": f["wc"], "nset": f["nset"], "pset": f["pset"],
                "q": f["q1"] + f["q2"], "held": f["held"], "qev": qev.get(name, 0),
                "est": f["estNow"], "exp": f["estNow"] + f["pnl7"] / days7, "npos": f["npos"], "u": f["u"],
                "typ": typical(FD, name, w["start"]),
            }), ev=ev_rows, ev_rest=ev_rest))
        rows.sort(key=lambda r: r["net"])
        span_days = max((w["end"] - w["start"]) / 86400, 1 / 24)
        t = r2({k: tot[k] for k in SUM_KEYS + ["nset", "pset", "q2", "q1", "held", "restNow", "estNow",
                                               "u", "risk", "npos", "wc"]})
        t["net"] = round(tot["rew"] + tot["pnl"], 2)
        t["qev"] = sum(1 for q in ev_q.values() if q)
        t["yield_c"] = round(tot["rew"] / tot["rest"] / span_days * 100, 2) if tot["rest"] > 0 else None
        t["typ"] = typical(FD, None, w["start"])
        out["windows"][wkey] = {"desc": w.get("desc"), "label": w.get("label"), "start": w["start"],
                                "end": w["end"], "pnl_na": bool(w.get("pnl_na")), "tot": t, "fams": rows}
        pts = (D.get("curves") or {}).get(wkey) or []
        out["curves"][wkey] = [[int(p[0]), round(num(p[1]), 2), round(num(p[2]), 2)] for p in thin(pts)]

    # book now (the same for every window)
    halts = D.get("halts") or {}
    out["book_wc"] = round(sum(num(e["wc"][0]) for e in events_now.values() if e.get("wc")), 2)
    out["halts"] = {"side": len(halts.get("side") or []), "event": len(halts.get("event") or []),
                    "depth": len(halts.get("depth") or []), "guards": len(halts.get("guards") or []),
                    "risk_guards": sum(1 for g in (halts.get("guards") or []) if g.get("risk"))}
    out["history"] = [{"d": h["d"], "rew": h.get("rew"), "pnl": h.get("pnl")} for h in (D.get("history") or [])[-14:]]
    p = D.get("proj") or {}
    out["proj"] = {"rest": p.get("rest"), "na": p.get("na")}
    # at this time yesterday (the Today hero's comparison line)
    cy = (D.get("curves") or {}).get("yesterday") or []
    tw, yw = D["windows"].get("today"), D["windows"].get("yesterday")
    if cy and tw and yw:
        target = yw["start"] + (gen - tw["start"])
        best = None
        for q in cy:
            if q[0] <= target + 1:
                best = q
        if best:
            out["ystd"] = {"rew": round(num(best[1]), 2), "pnl": round(num(best[2]), 2)}
    out["titles"] = event_titles(shown, fetch=fetch_titles)
    out["acct"] = account(D)
    return out


def _usd(v):
    return f"{'-' if v < 0 else '+'}${abs(v):,.0f}"


def explain(data, wkey="today", n=3):
    """What moved the window, for the refresh reply (Jack 2026-10-22: "When
    I ask for refresh, always give me an explanation of the days
    performance"): the day against a typical full day and against yesterday
    at this time, its split, then the families that moved it most either way,
    each with its split and its biggest event."""
    w = (data.get("windows") or {}).get(wkey)
    if not w:
        return []
    t, titles = w["tot"], data.get("titles") or {}
    carry = t["cr"] + t["cs"]
    head = f"net {_usd(t['net'])}"
    if t.get("typ") is not None:
        head += f" vs a typical full day {_usd(t['typ'])} (7-day mean)"
    ys = data.get("ystd") if wkey == "today" else None
    if ys:
        head += (f"; yesterday at this time {_usd(ys['rew'] + ys['pnl'])} (rewards "
                 f"${ys['rew']:,.0f}, trading {_usd(ys['pnl'])})")
    out = [head]
    if not w.get("pnl_na"):
        out.append(f"rewards ${t['rew']:,.0f}, trading {_usd(t['pnl'])} = new fills "
                   f"{_usd(t['pnl'] - carry)} + carry {_usd(carry)} (marks {_usd(t['cr'])}, "
                   f"settled {_usd(t['cs'])}); {int(t['fills'])} fills")

    def fam(r):
        s = (f"{r['name']} {_usd(r['net'])} (rewards ${r['rew']:,.0f}, new fills {_usd(r['nf'])}, "
             f"carry {_usd(r['cr'] + r['cs'])}, {r['fills']} fills)")
        ev = max(r.get("ev") or [], key=lambda e: abs(e["net"]), default=None)
        if ev and abs(ev["net"]) >= 1:
            s += f"; biggest event {titles.get(ev['e']) or ev['e']} {_usd(ev['net'])}"
        return s
    fams = w.get("fams") or []
    out += ["drag: " + fam(r) for r in sorted(fams, key=lambda r: r["net"])[:n] if r["net"] <= -1]
    out += ["lift: " + fam(r) for r in sorted(fams, key=lambda r: -r["net"])[:n] if r["net"] >= 1]
    return out


def render(data, template_path):
    with open(template_path, encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(data, separators=(",", ":"))
    blob = blob.replace("</", "<\\/").replace("<!--", "<\\!--")
    return tpl.replace("/*__POCKET_DATA__*/null", blob)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dash", default=DASH)
    ap.add_argument("--out", default=DASH_DIR)
    ap.add_argument("--template", default=TEMPLATE, help="page template the data is embedded in")
    ap.add_argument("--summary", action="store_true", help="print each window's totals")
    ap.add_argument("--no-fetch", action="store_true",
                    help="no Kalshi reads for missing event titles (cached titles only)")
    a = ap.parse_args(argv)
    data = build(load_model(a.dash), fetch_titles=not a.no_fetch)
    os.makedirs(a.out, exist_ok=True)
    jp = os.path.join(a.out, "imm_pocket.json")
    with open(jp + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(jp + ".tmp", jp)
    hp = os.path.join(a.out, "imm_pocket.html")
    page = render(data, a.template)
    with open(hp + ".tmp", "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(hp + ".tmp", hp)
    msg = f"wrote {jp} ({os.path.getsize(jp) / 1e3:.1f} KB), {hp} ({len(page) / 1e3:.1f} KB)"
    print(msg)
    if a.summary:
        print(f"build {data.get('generated_et')}")
        print(f"  {account_line(data.get('acct')) or 'account n/a (no account read in this build)'}")
        for k, w in data["windows"].items():
            t = w["tot"]
            # trading = the window's new fills + the inventory carried into it
            # (marks on positions held from before it, and their settlements):
            # the split every refresh reply gives (Jack 2026-10-22: "IMM pocket
            # refresh had carry vs new fills, but that's gone ... Fix it")
            carry = t["cr"] + t["cs"]
            split = ("  (trading n/a)" if w.get("pnl_na") else
                     f"  = new fills {t['pnl'] - carry:+.2f} + carry {carry:+.2f}"
                     f" (marks {t['cr']:+.2f}, settled {t['cs']:+.2f})")
            print(f"  {k:<9} net {t['net']:+10.2f}  rewards {t['rew']:10.2f}  trading {t['pnl']:+10.2f}"
                  f"{split}  fills {int(t['fills'])}  families {len(w['fams'])}")
        print("explain today (the refresh reply explains the day from these):")
        for line in explain(data, "today"):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
