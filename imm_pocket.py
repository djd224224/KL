#!/usr/bin/env python3
"""IMM Pocket: the IMM dashboard's top section and families table, small
enough to publish to claude.ai and read on a phone.

Reads the model the 10-minute build embeds in imm_dashboard.html (const D)
and adds it up exactly as the page does (aggregate / addM / derive /
baseline in imm_dashboard_template.html): per window, the hero, the tiles,
the cumulative curve and one row per family. Writes imm_pocket.json and
imm_pocket.html (imm_pocket_template.html with the data embedded) beside the
dashboard.

The page is published to claude.ai as "IMM Pocket"
(https://claude.ai/artifact/4qqWauCV23mEMCUT1g8Lj9). Its numbers come from
the page's own database document pocket/latest, which the scheduled Claude
task "imm-pocket-refresh" rewrites every 30 minutes from imm_pocket.json
(ArtifactData set); the build embedded at publish is only the fallback.
Republish the page itself only when the template changes.

    python imm_pocket.py [--dash path\\imm_dashboard.html] [--out dir] [--template path] [--summary]
"""
import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DASH_DIR = os.path.join(HERE, "run-logs", "incentive-mm", "dashboard")
DASH = os.path.join(DASH_DIR, "imm_dashboard.html")
TEMPLATE = os.path.join(HERE, "imm_pocket_template.html")
WINS = ["today", "yesterday", "24h", "7d"]
SUM_KEYS = ["rew", "pnl", "real", "du", "fills", "cts", "usd", "mk", "mk_n", "mk_cts", "rest",
            "cr", "cs", "fe", "fe_cts", "m5", "m5_cts", "m4", "m4_cts", "mt", "mt_cts"]
CURVE_MAX = 144


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


def build(D):
    gen = D["generated"]
    FD = fam_daily(D)
    w7 = D["windows"].get("7d") or {}
    days7 = max((w7.get("end", gen) - w7.get("start", gen - 7 * 86400)) / 86400, 1)
    events_now = D.get("events") or {}

    # event -> quoting now (any market resting a quote)
    ev_q = defaultdict(bool)
    for m in D["markets"].values():
        if (m.get("cur") or {}).get("q"):
            ev_q[m["ev"]] = True

    out = {"generated": gen, "generated_et": D.get("generated_et"), "windows": {}, "curves": {}}
    for wkey in WINS:
        w = D["windows"].get(wkey)
        if not w:
            continue
        fams, tot = {}, blank()
        for m in D["markets"].values():
            f = fams.setdefault(m["fam"], blank())
            add_market(f, m, wkey)
            add_market(tot, m, wkey)
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
            rows.append(r2({
                "name": name, "rew": f["rew"], "pnl": f["pnl"], "net": f["rew"] + f["pnl"], "nf": nf,
                "cr": f["cr"], "cs": f["cs"], "fills": int(f["fills"]), "cts": f["cts"],
                "mk": f["mk"], "mk_cts": f["mk_cts"], "fe": f["fe"], "fe_cts": f["fe_cts"],
                "mt": f["mt"], "mt_cts": f["mt_cts"], "cov": (f["rew"] / -nf) if nf < -0.005 else None,
                "wc": f["wc"], "nset": f["nset"], "pset": f["pset"],
                "q": f["q1"] + f["q2"], "held": f["held"], "qev": qev.get(name, 0),
                "est": f["estNow"], "exp": f["estNow"] + f["pnl7"] / days7, "npos": f["npos"], "u": f["u"],
                "typ": typical(FD, name, w["start"]),
            }))
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
    a = ap.parse_args(argv)
    data = build(load_model(a.dash))
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
        for k, w in data["windows"].items():
            t = w["tot"]
            print(f"  {k:<9} net {t['net']:+10.2f}  rewards {t['rew']:10.2f}  trading {t['pnl']:+10.2f}"
                  f"  fills {int(t['fills'])}  families {len(w['fams'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
