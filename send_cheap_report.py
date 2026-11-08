#!/usr/bin/env python3
r"""send_cheap_report.py -- the daily CHEAP-SIDE BOOKS email (Jack 2026-11-08:
"ship and list at $50, give daily email report on performance. add a
schedule").

CHEAP-SIDE BOOKS (incentive_mm CHEAP_FILE, cheap_side_books.json, written
hourly by cheap_scan.py): far-dated markets (outcome known 6M+ out) where the
bot rests ONE rung on the cheap side -- a YES bid at 1-2c on a long shot, or a
NO bid at 1-2c on a near-certainty -- for the liquidity rewards. A fill costs at
most 1-2c a contract and the side is capped at its size.

ONE EMAIL per ET day, money first, ending in KEEP / WATCH / STOP:
  rewards   the bot's own modelled accrual for the listed markets over the ET
            day: est_frac x pool_per_day x the cycle interval, from the cycle
            logs (the imm_reward_recon integral; intervals capped at 15 min).
            Kalshi pays a market only once it banks $1 in its program period.
  fills     every fill on a listed market that day (fills_<UTC date>.jsonl):
            contracts, cost, and what they are worth now at Kalshi's mid (or
            the settlement)
  net       rewards + (worth now - cost), for the day and since launch
  now       markets listed, rungs resting on Kalshi, $ at risk if every rung
            fills, the programs' end dates
Verdict: STOP when fills since launch have cost more than the rewards earned
and more than $25; WATCH when a rung filled yesterday, a listed market rests
nothing, or nothing is listed; KEEP otherwise.
Then NEW FAR-DATED SERIES FOR YOU TO APPROVE (Jack 2026-11-08: "Add it"): the
series outside the list where a 1-2c rung would pay, from the morning's
cheap_scan.py --discover (run-logs/cheap-scan/discover.json); NEW until one
email has shown it (the subject counts them). Approving is Jack's call.

SOURCES, read-only: cheap_side_books.json, run-logs/cheap-scan/list_history.
jsonl (every listing cheap_scan wrote), cycle_log_<UTC date>.csv, fills_<UTC
date>.jsonl, Kalshi /markets (signed-first reads) and the bot key's resting
orders. Writes only the sent-marker and cheap_report.json (each day's result,
for the running totals). Imports imm_quote_gaps first, like the other morning
emails, so the launcher env and the alert credentials apply. Idle priority.

USAGE:
  python send_cheap_report.py                   # the scheduled daily run
  python send_cheap_report.py --dry             # build + print, send nothing
  python send_cheap_report.py --test            # send now, ignore the marker
  python send_cheap_report.py --day 2026-11-08  # a specific ET day
Scheduled daily as "KL imm cheap-report" (register_cheap_tasks.ps1), 08:30 local.
All console output is ASCII (the task console is cp1252).
"""
import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import imm_quote_gaps as _gaps  # noqa: F401,E402  (import side effects: env + alert creds)
import incentive_mm as imm  # noqa: E402
import cheap_scan as cs  # noqa: E402  (the new-series section)
import kalshi_reads  # noqa: E402
import send_lone_ref_monitor as slr  # noqa: E402  (low_priority)
from incentive_mm import log  # noqa: E402

STORE = os.path.join(imm.STATUS_DIR, "cheap_report.json")
HISTORY = os.path.join(HERE, "run-logs", "cheap-scan", "list_history.jsonl")
MAX_DT = 900.0
STOP_LOSS_USD = 25.0


def zs(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def et_window(day):
    lo = imm.ET.localize(datetime(day.year, day.month, day.day)).astimezone(timezone.utc)
    hi = imm.ET.localize(datetime(day.year, day.month, day.day) + timedelta(days=1)).astimezone(timezone.utc)
    return lo, hi


def utc_dates(lo, hi):
    d, out = lo.date(), []
    while d <= (hi - timedelta(seconds=1)).date():
        out.append(d)
        d += timedelta(days=1)
    return out


def listed_in(lo, hi):
    """{ticker: last known {side, px, size, stop}} for every market listed at any
    time in [lo, hi): the list history, plus the file as it stands."""
    out = {}
    state = None
    try:
        with open(HISTORY, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    at = datetime.fromisoformat(rec["at"].replace("Z", "+00:00"))
                except (ValueError, KeyError):
                    continue
                if at < lo:
                    state = rec["markets"]          # what was listed going into the window
                    continue
                if at >= hi:
                    break
                for t, e in (state or {}).items():
                    out.setdefault(t, e)
                state = rec["markets"]
                for t, e in state.items():
                    out[t] = e
    except OSError:
        pass
    for t, e in (state or {}).items():
        out.setdefault(t, e)
    try:
        with open(imm.cheap_path(), encoding="utf-8") as fh:
            cur = (json.load(fh) or {}).get("markets") or {}
    except (OSError, ValueError):
        cur = {}
    if hi > datetime.now(timezone.utc) - timedelta(days=2):
        for t, e in cur.items():
            out.setdefault(t, {k: e.get(k) for k in ("side", "px", "size", "stop")})
    return out, cur


def listed_spans():
    """{ticker: [(from_ts, to_ts)]}: when each market was on the cheap-side list,
    from every listing cheap_scan wrote (list_history.jsonl); a market still on
    it runs to +inf. Rewards and fills count only inside these spans, so a
    market the bot quoted under its family rules before (or after) is not
    credited to the cheap-side books."""
    spans, open_at = defaultdict(list), {}
    try:
        with open(HISTORY, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    at = datetime.fromisoformat(rec["at"].replace("Z", "+00:00")).timestamp()
                    now_set = set(rec.get("markets") or {})
                except (ValueError, KeyError, TypeError):
                    continue
                for t in now_set - set(open_at):
                    open_at[t] = at
                for t in set(open_at) - now_set:
                    spans[t].append((open_at.pop(t), at))
    except OSError:
        pass
    for t, at in open_at.items():
        spans[t].append((at, float("inf")))
    return dict(spans)


def in_spans(spans, t, x):
    return any(a <= x < b for a, b in spans.get(t, ()))


def accrue(tickers, lo, hi, spans=None):
    """{ticker: [usd, cycles, cycles_quoted]} over [lo, hi) from the cycle logs
    (only inside the ticker's listed spans, when given)."""
    want = {t.encode() for t in tickers}
    res = defaultdict(lambda: [0.0, 0, 0])
    if not want:
        return {}
    lo_s, hi_s = zs(lo).encode(), zs(hi).encode()
    for d in utc_dates(lo, hi):
        p = os.path.join(imm.STATUS_DIR, f"cycle_log_{d}.csv")
        if not os.path.exists(p):
            continue
        prev_ts = cur_ts = None
        cur_b = None
        batch = []

        def flush(dt):
            for tk, frac, pool, quoted in batch:
                if spans is not None and not in_spans(spans, tk, cur_ts):
                    continue
                r = res[tk]
                r[0] += frac * pool * dt / 86400.0
                r[1] += 1
                r[2] += quoted
        with open(p, "rb") as fh:
            fh.readline()
            for line in fh:
                c1 = line.find(b",")
                if c1 < 0:
                    continue
                tsb = line[:c1]
                if tsb != cur_b:
                    try:
                        ts = datetime.strptime(tsb.decode(), "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        continue
                    if cur_ts is not None and prev_ts is not None and lo_s <= cur_b < hi_s:
                        dt = min(cur_ts - prev_ts, MAX_DT)
                        if dt > 0:
                            flush(dt)
                    if cur_ts is not None:
                        prev_ts = cur_ts
                    cur_b, cur_ts, batch = tsb, ts, []
                c2 = line.find(b",", c1 + 1)
                tk = line[c1 + 1:c2]
                if tk not in want:
                    continue
                q = line.split(b",")
                try:
                    frac, pool = float(q[7]), float(q[11])
                    quoted = 1 if (float(q[13] or 0) + float(q[14] or 0)) > 0 else 0
                except (ValueError, IndexError):
                    continue
                batch.append((tk.decode(), frac, pool, quoted))
            if batch and cur_ts is not None and prev_ts is not None and lo_s <= cur_b < hi_s:
                dt = min(cur_ts - prev_ts, MAX_DT)
                if dt > 0:
                    flush(dt)
    return dict(res)


def fills_in(tickers, lo, hi, spans=None):
    out = defaultdict(list)
    for d in utc_dates(lo, hi):
        p = os.path.join(imm.STATUS_DIR, f"fills_{d}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                try:
                    f = json.loads(line)
                except ValueError:
                    continue
                t = f.get("ticker")
                if t not in tickers or not (lo.timestamp() <= float(f.get("ts") or 0) < hi.timestamp()):
                    continue
                if spans is not None and not in_spans(spans, t, float(f.get("ts") or 0)):
                    continue
                px = f.get("our_price_cents")
                if px is None:
                    px = f.get("yes_price_cents")
                bs = f.get("our_book_side") or ("bid" if (f.get("side") == "yes") == (f.get("action") == "buy")
                                                else "ask")
                if px is None:
                    continue
                # in the contract bought: a bid buys YES at px, an ask buys NO at 100 - px
                own_px = float(px) if bs == "bid" else 100.0 - float(px)
                out[t].append({"ts": f.get("ts"), "side": "yes" if bs == "bid" else "no",
                               "n": float(f.get("count") or 0), "px": own_px})
    return dict(out)


def market_values(tickers):
    """{ticker: {"yes": value of 1 YES in $, "status"}} from Kalshi now: the
    settlement if settled, else the mid."""
    out, tick = {}, sorted(tickers)
    for i in range(0, len(tick), 100):
        try:
            j = kalshi_reads.read("/markets", {"tickers": ",".join(tick[i:i + 100]), "limit": 1000}) or {}
        except Exception as e:  # noqa: BLE001
            log(f"! /markets read failed ({e!r})")
            continue
        for m in j.get("markets") or []:
            res = str(m.get("result") or "").lower()
            if res in ("yes", "no"):
                v = 1.0 if res == "yes" else 0.0
            else:
                b = float(m.get("yes_bid_dollars") or 0)
                a = float(m.get("yes_ask_dollars") or 0)
                v = (b + a) / 2 if b > 0 and a > 0 else (b or a)
            out[m["ticker"]] = {"yes": v, "status": m.get("status"),
                                "book": f"{float(m.get('yes_bid_dollars') or 0) * 100:g}x"
                                        f"{float(m.get('yes_ask_dollars') or 0) * 100:g}"}
    return out


def resting_now(tickers):
    out = defaultdict(float)
    try:
        client = imm._fair_reader_client()
        cursor = None
        for _ in range(60):
            q = {"status": "resting", "limit": 1000}
            if cursor:
                q["cursor"] = cursor
            r = client.get("/portfolio/orders", params=q) or {}
            for o in r.get("orders") or []:
                if o.get("ticker") in tickers and str(o.get("client_order_id", "")).startswith(
                        imm.CLIENT_ORDER_PREFIX + "-"):
                    out[o["ticker"]] += imm.order_remaining(o)
            cursor = r.get("cursor")
            if not cursor or not r.get("orders"):
                break
    except Exception as e:  # noqa: BLE001
        log(f"! resting orders read failed ({e!r})")
        return None
    return dict(out)


def load_store():
    try:
        with open(STORE, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def build(day, now):
    lo, hi = et_window(day)
    hi_eff = min(hi, now)
    listed, cur = listed_in(lo, hi_eff)
    spans = listed_spans()
    store = load_store()
    days = store.setdefault("days", {})
    rew = accrue(set(listed), lo, hi_eff, spans)
    fills = fills_in(set(listed) | set(cur), lo, hi_eff, spans)
    # every fill since launch, for the running position and its worth now
    launch = store.get("launch") or zs(lo)
    lo_all = datetime.fromisoformat(launch.replace("Z", "+00:00"))
    all_t = set(listed) | set(cur) | set(store.get("tickers") or [])
    fills_all = fills_in(all_t, lo_all, now, spans)
    vals = market_values(all_t | set(fills_all))
    rest = resting_now(set(cur))
    per = []
    for t in sorted(set(listed) | set(fills)):
        e = listed.get(t) or {}
        r = rew.get(t, [0.0, 0, 0])
        fl = fills.get(t, [])
        per.append({"t": t, "side": e.get("side"), "px": e.get("px"), "size": e.get("size"),
                    "stop": e.get("stop"), "usd": r[0], "cycles": r[1], "quoted": r[2],
                    "fill_n": sum(x["n"] for x in fl),
                    "fill_cost": sum(x["n"] * x["px"] / 100 for x in fl)})
    pos = {}
    for t, fl in fills_all.items():
        v = (vals.get(t) or {}).get("yes")
        n = sum(x["n"] for x in fl)
        cost = sum(x["n"] * x["px"] / 100 for x in fl)
        worth = None if v is None else sum(x["n"] * (v if x["side"] == "yes" else 1 - v) for x in fl)
        pos[t] = {"n": n, "cost": cost, "worth": worth}
    day_rew = sum(x["usd"] for x in per)
    day_fill_cost = sum(x["fill_cost"] for x in per)
    day_fill_worth = 0.0
    for t, fl in fills.items():
        v = (vals.get(t) or {}).get("yes")
        if v is not None:
            day_fill_worth += sum(x["n"] * (v if x["side"] == "yes" else 1 - v) for x in fl)
    days[str(day)] = {"rewards": round(day_rew, 4), "fill_cost": round(day_fill_cost, 4),
                      "markets": len(per), "by_ticker": {x["t"]: round(x["usd"], 4) for x in per}}
    store["launch"] = launch
    store["tickers"] = sorted(all_t)
    tot_rew = sum(v.get("rewards", 0.0) for k, v in days.items() if k <= str(day))
    tot_cost = sum(p["cost"] for p in pos.values())
    tot_worth = sum(p["worth"] or 0.0 for p in pos.values())
    risk = sum((e.get("size") or 0) * (e.get("px") or 0) / 100 for e in cur.values())
    n_rest = None if rest is None else sum(1 for t in cur if rest.get(t, 0) > 0)
    full = [t for t, e in cur.items() if (pos.get(t) or {}).get("n", 0) >= (e.get("size") or 1e18)]
    net_all = tot_rew + tot_worth - tot_cost
    why = []
    if tot_cost - tot_worth > max(tot_rew, 0) and tot_cost - tot_worth > STOP_LOSS_USD:
        verdict = "STOP"
        why.append(f"fills since launch are down ${tot_cost - tot_worth:,.2f}, more than the "
                   f"${tot_rew:,.2f} of rewards")
    else:
        verdict = "KEEP"
        if day_fill_cost > 0:
            why.append(f"a rung filled yesterday ({sum(x['fill_n'] for x in per):,.0f} contracts)")
        if full:
            why.append(f"{len(full)} market(s) filled to their cap and rest nothing: {', '.join(full[:5])}")
        if not cur:
            why.append("nothing is listed")
        elif n_rest is not None and n_rest < len(cur):
            idle = [t for t in cur if rest.get(t, 0) <= 0 and t not in full]
            if idle:
                why.append(f"{len(idle)} listed market(s) rest nothing now (the rung stands aside "
                           f"while it would not earn): {', '.join(idle[:5])}")
        if why:
            verdict = "WATCH"
    ends = sorted({str(e.get("stop"))[:10] for e in cur.values() if e.get("stop")})
    disc = None
    try:
        with open(cs.DISCOVER_OUT, encoding="utf-8") as fh:
            disc = json.load(fh)
    except (OSError, ValueError):
        pass
    try:
        disc_lines, n_new = cs.render_discover(disc, now)
    except Exception as e:  # noqa: BLE001  (the money part still goes out)
        disc_lines, n_new, disc = ["", f"NEW FAR-DATED SERIES: discover.json unreadable ({e!r})."], 0, None
    return {"disc_lines": disc_lines, "n_new": n_new,
            "disc_series": [x["series"] for x in (disc or {}).get("candidates") or []],
            "day": str(day), "lo": lo, "hi": hi_eff, "per": per, "pos": pos, "vals": vals,
            "day_rew": day_rew, "day_fill_cost": day_fill_cost, "day_fill_worth": day_fill_worth,
            "tot_rew": tot_rew, "tot_cost": tot_cost, "tot_worth": tot_worth, "net_all": net_all,
            "listed_now": len(cur), "resting_now": n_rest, "risk": risk, "ends": ends,
            "verdict": verdict, "why": why, "store": store, "launch": launch}


def render(c):
    d = datetime.strptime(c["day"], "%Y-%m-%d")
    day_net = c["day_rew"] + c["day_fill_worth"] - c["day_fill_cost"]
    lines = [
        f"{c['verdict']}: about ${day_net:+,.2f} on {d:%a %m/%d} (ET) -- ${c['day_rew']:,.2f} of "
        f"modelled rewards on {len(c['per'])} cheap-side markets, ${c['day_fill_cost']:,.2f} of fills "
        f"worth ${c['day_fill_worth']:,.2f} now.",
        f"Since launch ({c['launch'][:10]}): rewards ${c['tot_rew']:,.2f}, fills cost ${c['tot_cost']:,.2f} "
        f"and are worth ${c['tot_worth']:,.2f} now, net ${c['net_all']:+,.2f}.",
        f"Now: {c['listed_now']} listed, "
        + (f"{c['resting_now']} resting on Kalshi" if c["resting_now"] is not None else "resting orders unread")
        + f", ${c['risk']:,.0f} if every rung fills; programs end {', '.join(c['ends']) or '-'} "
          f"(the hourly re-scan lists the next periods).",
    ]
    if c["why"]:
        lines.append("Why " + c["verdict"] + ": " + "; ".join(c["why"]) + ".")
    lines += c.get("disc_lines") or []
    lines += ["", "By market, yesterday (rewards, share of the day quoted, rung, fills; position since launch):"]
    for x in sorted(c["per"], key=lambda x: -x["usd"]):
        p = c["pos"].get(x["t"]) or {}
        q = f"{x['quoted'] / x['cycles'] * 100:.0f}%" if x["cycles"] else "-"
        rung = f"{(x['side'] or '?').upper()} {x['px']}c x{x['size']}" if x["side"] else "delisted"
        posn = (f"holds {p['n']:,.0f} (cost ${p['cost']:,.2f}, worth ${p['worth'] or 0:,.2f})"
                if p.get("n") else "flat")
        fl = f"{x['fill_n']:,.0f} filled (${x['fill_cost']:,.2f})" if x["fill_n"] else "no fills"
        book = (c["vals"].get(x["t"]) or {}).get("book", "")
        lines.append(f"  {x['t']:36} ${x['usd']:6.2f}  quoted {q:>4}  {rung:15} {fl}; {posn}; book {book}")
    lines += ["",
              "Rewards are the bot's own share model over the day (Kalshi pays a market only once it banks "
              "$1 in its program period; it has paid ~1.15x the model). Fills are marked at Kalshi's mid, "
              "or the settlement. The list: run-logs/incentive-mm/cheap_side_books.json, rewritten hourly by "
              "cheap_scan.py (task KL cheap-scan); IMM_CHEAP_SIDE=off or an empty list stops it."]
    return "\n".join(lines)


def subject(c):
    d = datetime.strptime(c["day"], "%Y-%m-%d")
    net = c["day_rew"] + c["day_fill_worth"] - c["day_fill_cost"]
    return (f"Cheap-side books {d:%m/%d}: {net:+.2f} (rewards ${c['day_rew']:.2f}, fills "
            f"${c['day_fill_cost']:.2f}) -- {c['verdict']}"
            + (f" | {c['n_new']} new series to review" if c.get("n_new") else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="send now regardless of the sent-marker; no marker")
    ap.add_argument("--dry", action="store_true", help="build and print only; no email, marker or store")
    ap.add_argument("--day", help="ET day to report (YYYY-MM-DD); default yesterday")
    args = ap.parse_args(argv)
    slr.low_priority()
    now = datetime.now(timezone.utc)
    today_et = now.astimezone(imm.ET).date()
    day = datetime.strptime(args.day, "%Y-%m-%d").date() if args.day else today_et - timedelta(days=1)
    marker = os.path.join(imm.STATUS_DIR, f"imm_cheap_report_sent_{today_et}.marker")
    if not (args.test or args.dry or args.day) and os.path.exists(marker):
        log(f"cheap-side email already sent for {today_et}; exiting")
        return 0
    c = build(day, now)
    text, subj = render(c), subject(c)
    log("cheap-side email: " + subj + "\n" + text)
    if args.dry:
        return 0
    try:
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(c["store"], fh, indent=1, sort_keys=True)
        os.replace(tmp, STORE)
    except OSError as e:
        log(f"! cheap_report.json not written ({e!r})")
    alerter = imm.Alerter("IMM-CHEAP", live=True)
    if not alerter.enabled:
        log("cannot send cheap-side email: alert credentials not configured")
        return 1
    attempts = 1 if args.test else 8
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=("[TEST] " if args.test else "") + subj)
        if ok:
            break
        log(f"cheap-side send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"cheap-side send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.day):
        cs.mark_emailed(c.get("disc_series") or [], str(today_et))   # NEW only in one email
        try:
            with open(marker, "w") as fh:
                fh.write(now.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR, "imm_cheap_report_sent_*.marker")):
            name = os.path.basename(old)[len("imm_cheap_report_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
