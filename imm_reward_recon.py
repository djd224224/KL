#!/usr/bin/env python3
"""
imm_reward_recon.py — reconcile incentive_mm's reward ESTIMATE against the
liquidity incentives Kalshi actually CREDITED.

Kalshi exposes no credits endpoint (verified 2026-08-03 across 8 paths), so the
credit side comes from the account statement: open the Kalshi UI rewards page,
select all, paste into a text file, and feed it to --statement. The paste is
4 lines per credit:

    Liquidity Incentive for event <EVENT_TICKER>
    <Mon D, YYYY>
    $<amount>
    Liquidity

Credits are appended to a permanent ledger (reward_credits.csv) so each new
statement only has to overlap the last one; re-pasting the same rows is a
no-op. The estimate side is rebuilt from the bot's own cycle logs, whose rows
carry the two terms of the accrual integral (est_frac and pool_per_day) at
every full cycle, so a market's estimated reward is reproduced exactly rather
than trusted from a running counter.

WHAT THIS EXISTS TO CATCH (all three were live errors found on 2026-08-04 when
the first full statement was reconciled):

  1. A per-market payout FLOOR of exactly $1.00. Across all 2,720 LIQUIDITY
     credits the smallest is $1.00 and not one falls below it (volume
     incentives are a different program and are not floored); "one credit per market
     clearing $1" predicted the actual per-event credit count exactly on 94 of
     99 clean-window events. ~30% of the markets the bot quotes never clear it
     and pay nothing.
  2. Double-counted snapshot exclusion. The accrual multiplied the rate by a
     coverage EMA even though excluded snapshots already score 0.0, costing
     ~7% of the estimate.
  3. Attribution. Kalshi pays the ACCOUNT, not a strategy — MLB/fight/mention
     credits belong to the other bots on this key, and credits predate IMM.
     Comparing an account-level statement total to an IMM-only estimate is
     what produced the fictional "$1,500 of non-IMM reward" figure.

Usage:
    python imm_reward_recon.py --statement "C:/.../incentive rewards.txt"
    python imm_reward_recon.py --report
    python imm_reward_recon.py --report --refresh-programs   # paid_out/periods
    python imm_reward_recon.py --report --since 2026-08-01T19:13
"""

import argparse
import csv
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

STATUS_DIR = os.environ.get(
    "IMM_STATUS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "run-logs", "incentive-mm"))
LEDGER_PATH = os.path.join(STATUS_DIR, "reward_credits.csv")
CALIB_PATH = os.path.join(STATUS_DIR, "reward_calibration.json")
PROGRAM_CACHE = os.path.join(STATUS_DIR, "reward_programs.json")
EST_CACHE = os.path.join(STATUS_DIR, "reward_est_cache.json")

# The bot's first live day. Credits dated before this cannot be IMM's, and the
# digest must not count them (Jack 2026-08-04).
IMM_INCEPTION = "2026-07-12"
# The post-amendment estimator (reference scoring, atref ladders) shipped here.
# Estimates from before it are modelling the PRE-7/30 program rules against
# post-7/30 payouts, so they calibrate nothing — every "the estimator runs 30%
# low on temp" style conclusion drawn across this boundary is an artifact.
AMENDMENT_CUTOVER = "2026-08-01T19:13"
# Kalshi pays nothing below this per market per program period (measured).
PAYOUT_FLOOR = 1.00

TITLE_RE = re.compile(r"^(?:Liquidity|Volume) Incentive for (?:event|market)\s+(.+?)\s*$")
DATE_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})$")
AMT_RE = re.compile(r"^\$([0-9,]+(?:\.\d+)?)$")


def log(msg):
    print(msg, flush=True)


def event_of(ticker):
    """Market ticker -> event ticker. Kalshi market tickers are
    SERIES-EVENTSUFFIX-STRIKE; credits are reported per event."""
    parts = ticker.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else ticker


def series_of(ticker):
    return ticker.split("-")[0]


def reward_family(series):
    """Coarse family for calibration. Deliberately groups by PROGRAM SHAPE
    rather than subject: what determines whether a family can be calibrated at
    all is how fast its programs end and pay. Hourly temp settles inside the
    hour and is measurable the next day; a 5-day earnings-mention program is
    unmeasurable until it ends."""
    if series.startswith("KXTEMP"):
        return "TEMP (hourly)"
    if series.startswith("KXEARNINGSMENTION"):
        return "EARNINGS-MENTION"
    if series.startswith("KXRAIN"):
        return "RAIN"
    if series.startswith("KXDIESEL") or series.startswith("KXAAAGAS"):
        return "GAS/DIESEL"
    if "MENTION" in series:
        return "OTHER MENTION"
    return "OTHER (company/econ/crypto)"


# ----------------------------------------------------------------------------
# Statement parsing + the permanent credit ledger
# ----------------------------------------------------------------------------

def parse_statement(path):
    """[(credit_date, event_ticker, amount, kind)] from a Kalshi UI paste.

    Tolerant by construction: the header lines (month total, 'Lifetime
    rewards', the bare dollar figures under them) and any trailing junk are
    skipped, and a record is only emitted when the date and amount lines both
    parse. Anything that looks like a credit but does NOT parse is returned
    separately so a UI format change surfaces as a loud count instead of a
    silently short ledger."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = [ln.strip() for ln in f.read().splitlines()]
    rows, bad = [], []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        m = TITLE_RE.match(ln) if ln else None
        if not m:
            i += 1
            continue
        ev = m.group(1)
        buf, j = [], i + 1
        while j < n and len(buf) < 3:
            if lines[j]:
                buf.append(lines[j])
            j += 1
        if len(buf) == 3 and DATE_RE.match(buf[0]) and AMT_RE.match(buf[1]):
            d = datetime.strptime(buf[0], "%b %d, %Y").date().isoformat()
            amt = float(AMT_RE.match(buf[1]).group(1).replace(",", ""))
            rows.append((d, ev, amt, buf[2]))
            i = j
        else:
            bad.append((i + 1, ln))
            i += 1
    return rows, bad


def statement_totals(path):
    """The 'Lifetime rewards' / month figures the UI prints above the list, so
    the ledger can be checked against Kalshi's own arithmetic."""
    out = {}
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    for i, ln in enumerate(lines[:12]):
        if re.match(r"^[A-Z][a-z]{2}\s+\d{4}$", ln) and i + 1 < len(lines):
            m = AMT_RE.match(lines[i + 1])
            if m:
                out["month"] = (ln, float(m.group(1).replace(",", "")))
        if ln.lower().startswith("lifetime reward") and i + 1 < len(lines):
            m = AMT_RE.match(lines[i + 1])
            if m:
                out["lifetime"] = float(m.group(1).replace(",", ""))
    return out


def load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return []
    with open(LEDGER_PATH, newline="", encoding="utf-8") as f:
        return [(r["credit_date"], r["event_ticker"], float(r["amount"]),
                 r.get("kind") or "Liquidity") for r in csv.DictReader(f)]


def merge_ledger(existing, incoming):
    """Union as a MULTISET keyed on (date, event, amount, kind).

    Credits carry no id and an event legitimately receives several identical
    amounts on the same day (one per market), so plain set-dedup would delete
    real money. Taking the per-key MAX of the two counts makes re-pasting an
    overlapping statement idempotent while still admitting a genuinely new
    duplicate that a later statement reveals."""
    old, new = Counter(existing), Counter(incoming)
    merged = Counter()
    for key in set(old) | set(new):
        merged[key] = max(old[key], new[key])
    added = sum((merged - old).values())
    return sorted(merged.elements(), reverse=True), added


def write_ledger(rows):
    os.makedirs(STATUS_DIR, exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["credit_date", "event_ticker", "amount", "kind"])
        w.writerows(rows)
    os.replace(tmp, LEDGER_PATH)


# ----------------------------------------------------------------------------
# Estimate side: replay the cycle logs
# ----------------------------------------------------------------------------

def rebuild_estimates(max_dt=900.0):
    """{market_ticker: {est, first, last, cycles, two_sided}} from the cycle
    logs, cached per source file so a rerun costs seconds instead of minutes.

    Reproduces the bot's accrual exactly: only FULL cycles are logged, every
    row in a cycle shares one timestamp, and the accrual integrates
    `est_frac x pool_per_day` over the gap since the previous cycle. `max_dt`
    caps that gap so a restart or outage cannot bill a market for hours it
    was not quoted in."""
    cache = {}
    if os.path.exists(EST_CACHE):
        try:
            with open(EST_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    files = sorted(glob.glob(os.path.join(STATUS_DIR, "cycle_log_*.csv")))
    out = defaultdict(lambda: {"est": 0.0, "first": None, "last": None,
                               "cycles": 0, "two_sided": 0})
    for path in files:
        st = os.stat(path)
        key = os.path.basename(path)
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        entry = cache.get(key)
        if not entry or entry.get("sig") != sig:
            entry = {"sig": sig, "markets": _scan_cycle_log(path, max_dt)}
            cache[key] = entry
            log(f"  scanned {key} ({st.st_size / 1e6:.0f} MB)")
        for t, v in entry["markets"].items():
            d = out[t]
            d["est"] += v[0]
            d["cycles"] += v[3]
            d["two_sided"] += v[4]
            d["first"] = v[1] if d["first"] is None else min(d["first"], v[1])
            d["last"] = v[2] if d["last"] is None else max(d["last"], v[2])
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        tmp = EST_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, EST_CACHE)
    except OSError as e:
        log(f"  ! estimate cache write failed: {e}")
    return dict(out)


def _scan_cycle_log(path, max_dt):
    """{ticker: [est, first_ts, last_ts, cycles, two_sided]} for one day file.

    NOTE the cross-file seam: the first cycle of a day gets no dt because the
    previous cycle lives in yesterday's file. That drops one ~30s interval per
    day (<0.1% of a day's accrual) and is deliberate — carrying state across
    files would break the per-file caching that makes reruns cheap."""
    res = defaultdict(lambda: [0.0, None, None, 0, 0])
    prev_ts = cur_ts = None
    batch = []

    def flush(ts, rows, dt):
        dt_days = dt / 86400.0
        for tkr, frac, sides, pool in rows:
            r = res[tkr]
            r[0] += frac * pool * dt_days
            r[1] = ts if r[1] is None else min(r[1], ts)
            r[2] = ts if r[2] is None else max(r[2], ts)
            r[3] += 1
            r[4] += 1 if sides == 2 else 0

    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rdr = csv.reader(f)
        next(rdr, None)
        for row in rdr:
            if len(row) < 13 or row[0] == "ts":
                continue
            try:
                ts = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc).timestamp()
                tkr, frac, sides, pool = row[1], float(row[7]), int(row[8]), float(row[11])
            except (ValueError, IndexError):
                continue
            if cur_ts is None:
                cur_ts = ts
            elif ts != cur_ts:
                if prev_ts:
                    dt = min(cur_ts - prev_ts, max_dt)
                    if dt > 0:
                        flush(cur_ts, batch, dt)
                prev_ts, cur_ts, batch = cur_ts, ts, []
            batch.append((tkr, frac, sides, pool))
    if batch and prev_ts:
        dt = min(cur_ts - prev_ts, max_dt)
        if dt > 0:
            flush(cur_ts, batch, dt)
    return {t: v for t, v in res.items()}


# ----------------------------------------------------------------------------
# Program metadata (paid_out + period), needed to tell "not paid" from
# "not paid YET" — a long-dated program simply has not settled.
# ----------------------------------------------------------------------------

def refresh_programs():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import incentive_mm as imm
    client = imm.build_client()
    by_market = {}
    for status in (None, "active", "settled", "closed"):
        cursor, got = None, 0
        for _page in range(80):
            params = {"limit": 1000}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            try:
                r = client.get("/incentive_programs", params=params)
            except Exception as e:
                log(f"  ! program page failed ({status}): {e}")
                break
            batch = r.get("incentive_programs") or []
            for p in batch:
                t = p.get("market_ticker") or ""
                cur = by_market.get(t)
                rec = {"start": p.get("start_date"), "end": p.get("end_date"),
                       "paid": bool(p.get("paid_out")),
                       "reward": p.get("period_reward")}
                if cur is None:
                    by_market[t] = rec
                else:
                    cur["paid"] = cur["paid"] or rec["paid"]
                    if rec["end"] and (not cur["end"] or rec["end"] > cur["end"]):
                        cur["end"] = rec["end"]
                    if rec["start"] and (not cur["start"] or rec["start"] < cur["start"]):
                        cur["start"] = rec["start"]
            got += len(batch)
            cursor = r.get("next_cursor")
            if not cursor or not batch:
                break
        log(f"  programs status={status!r}: {got} rows, {len(by_market)} markets")
    os.makedirs(STATUS_DIR, exist_ok=True)
    tmp = PROGRAM_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(by_market, f)
    os.replace(tmp, PROGRAM_CACHE)
    return by_market


def load_programs():
    if os.path.exists(PROGRAM_CACHE):
        try:
            with open(PROGRAM_CACHE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def _ts(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Reconciliation
# ----------------------------------------------------------------------------

def reconcile(since=None, floor=PAYOUT_FLOOR):
    ledger = load_ledger()
    est = rebuild_estimates()
    progs = load_programs()
    now = datetime.now(timezone.utc).timestamp()

    credits, credit_n, credit_last = defaultdict(float), defaultdict(int), {}
    for d, ev, amt, _kind in ledger:
        credits[ev] += amt
        credit_n[ev] += 1
        credit_last[ev] = max(credit_last.get(ev, d), d)

    ev_est = defaultdict(lambda: {"est": 0.0, "est_floor": 0.0, "mkts": 0,
                                  "first": None, "last": None,
                                  "p_start": None, "p_end": None,
                                  "paid": 0, "unpaid": 0})
    for t, v in est.items():
        e = ev_est[event_of(t)]
        e["est"] += v["est"]
        # the exchange's per-market floor, applied per market as it is paid
        e["est_floor"] += v["est"] if v["est"] >= floor else 0.0
        e["mkts"] += 1
        e["first"] = v["first"] if e["first"] is None else min(e["first"], v["first"])
        e["last"] = v["last"] if e["last"] is None else max(e["last"], v["last"])
        p = progs.get(t)
        if p:
            s, en = _ts(p.get("start")), _ts(p.get("end"))
            if s is not None:
                e["p_start"] = s if e["p_start"] is None else min(e["p_start"], s)
            if en is not None:
                e["p_end"] = en if e["p_end"] is None else max(e["p_end"], en)
            e["paid" if p.get("paid") else "unpaid"] += 1

    since_ts = _ts(since + ("" if "Z" in (since or "") else "+00:00")) if since else None
    rows = []
    for ev in set(credits) | set(ev_est):
        e = ev_est.get(ev)
        # `--since` narrows the CALIBRATION sample only. Attribution totals are
        # lifetime by definition, so the row is kept and flagged rather than
        # dropped — filtering here silently deflated "IMM-attributable".
        in_window = not (since_ts is not None
                         and (not e or e["first"] is None or e["first"] < since_ts))
        settled = True
        if e and e["p_end"] is not None:
            # a program that has not ended (or not paid out) cannot be judged
            settled = e["p_end"] < now - 12 * 3600 and e["unpaid"] <= e["paid"]
        elif e and e["last"] is not None:
            settled = e["last"] < now - 3 * 86400
        rows.append({
            "event": ev, "series": series_of(ev),
            "credited": credits.get(ev, 0.0), "credits": credit_n.get(ev, 0),
            "est": e["est"] if e else 0.0,
            "est_floor": e["est_floor"] if e else 0.0,
            "mkts": e["mkts"] if e else 0,
            "over_floor": sum(1 for t, v in est.items()
                              if event_of(t) == ev and v["est"] >= floor) if e else 0,
            "settled": settled and in_window,
            "settled_any": settled,
            "first": e["first"] if e else None,
            "imm": bool(e and e["est"] > 0.0),
            "credit_date": credit_last.get(ev, ""),
            "prog_hours": ((e["p_end"] - e["p_start"]) / 3600.0
                           if e and e["p_start"] and e["p_end"] else None),
        })
    return rows, ledger


def _by_date_imm(ledger, imm_set):
    """{credit_date: IMM-attributable $} from inception forward."""
    out = defaultdict(float)
    for d, ev, amt, _kind in ledger:
        if d >= IMM_INCEPTION and ev in imm_set:
            out[d] += amt
    return out


def _fmt_ratio(c, s):
    return f"{c / s:.3f}" if s > 0.01 else "   n/a"


def report(rows, ledger, floor=PAYOUT_FLOOR, verbose=False):
    lifetime = sum(a for _d, _e, a, _k in ledger)
    since_imm = sum(a for d, _e, a, _k in ledger if d >= IMM_INCEPTION)
    imm_events = {r["event"] for r in rows if r["imm"]}
    imm_credit = sum(a for d, e, a, _k in ledger
                     if d >= IMM_INCEPTION and e in imm_events)

    print("=" * 74)
    print("CREDITED (account statement)")
    print("=" * 74)
    print(f"  lifetime, all strategies          ${lifetime:12,.2f}  ({len(ledger)} credits)")
    print(f"  dated >= {IMM_INCEPTION} (IMM live)     ${since_imm:12,.2f}")
    print(f"  ... and on an event IMM quoted    ${imm_credit:12,.2f}   <- IMM-attributable")
    print(f"  NOT IMM's (other bots / pre-IMM)  ${lifetime - imm_credit:12,.2f}")

    settled = [r for r in rows if r["settled"]]
    print()
    print("=" * 74)
    print(f"ESTIMATE vs CREDIT — settled programs only ({len(settled)} events)")
    print("  ratio > 1 means the estimator UNDERSTATES what Kalshi paid.")
    print("=" * 74)
    c = sum(r["credited"] for r in settled)
    s = sum(r["est"] for r in settled)
    sf = sum(r["est_floor"] for r in settled)
    print(f"  credited ${c:11,.2f}   raw estimate ${s:11,.2f}  -> {_fmt_ratio(c, s)}x")
    print(f"  {'':20}   with ${floor:.2f} floor ${sf:11,.2f}  -> {_fmt_ratio(c, sf)}x")

    # Credit-count check: the floor predicts HOW MANY markets get paid, which
    # is an independent test of the model from the dollar total.
    pred = sum(r["over_floor"] for r in settled if r["credits"] or r["est"] > 0)
    act = sum(r["credits"] for r in settled)
    print(f"  credit ROWS: predicted {pred}, actual {act}"
          f"  ({pred / act:.3f}x)" if act else "")

    print()
    print(f"  {'series':<26}{'n':>4}{'credited':>11}{'est+floor':>11}{'factor':>9}")
    agg = defaultdict(lambda: [0.0, 0.0, 0])
    for r in settled:
        a = agg[r["series"]]
        a[0] += r["credited"]
        a[1] += r["est_floor"]
        a[2] += 1
    for ser, a in sorted(agg.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        if a[0] + a[1] < 5 and not verbose:
            continue
        print(f"  {ser:<26}{a[2]:>4}{a[0]:>11,.2f}{a[1]:>11,.2f}{_fmt_ratio(a[0], a[1]):>9}")

    # Program length is the axis that matters: the estimator is validated on
    # short programs (hourly temp) and has almost no settled evidence on long
    # ones, where it historically ran far too hot.
    print()
    print(f"  {'program length':<26}{'n':>4}{'credited':>11}{'est+floor':>11}{'factor':>9}")
    for lo, hi, lbl in [(0, 1.5, "<= 1.5h"), (1.5, 6, "1.5-6h"), (6, 24, "6-24h"),
                        (24, 72, "24-72h"), (72, 1e9, "> 72h")]:
        g = [r for r in settled if r["prog_hours"] is not None
             and lo <= r["prog_hours"] < hi]
        if not g:
            continue
        cc = sum(r["credited"] for r in g)
        ss = sum(r["est_floor"] for r in g)
        print(f"  {lbl:<26}{len(g):>4}{cc:>11,.2f}{ss:>11,.2f}{_fmt_ratio(cc, ss):>9}")

    worst = sorted([r for r in settled if r["est_floor"] > 5],
                   key=lambda r: r["credited"] / max(r["est_floor"], 0.01))
    if worst:
        print("\n  worst over-estimates (settled, est > $5):")
        for r in worst[:10]:
            ph = f"{r['prog_hours']:.0f}h" if r["prog_hours"] else "?"
            print(f"    {r['event']:<34} prog {ph:>6}  est ${r['est_floor']:8.2f}"
                  f"  credited ${r['credited']:8.2f}  {_fmt_ratio(r['credited'], r['est_floor'])}x")
    return {"lifetime": lifetime, "since_imm": since_imm, "imm_credit": imm_credit,
            "settled_credited": c, "settled_est": s, "settled_est_floor": sf,
            "series": {k: {"credited": round(v[0], 2), "est_floor": round(v[1], 2),
                           "n": v[2], "factor": round(v[0] / v[1], 4) if v[1] > 0.01 else None}
                       for k, v in agg.items()}}


def write_calibration(summary, rows, ledger):
    """The machine-readable half: the digest reads IMM-attributable credited
    totals from here instead of a hand-set env var that goes stale."""
    imm_set = {r["event"] for r in rows if r["imm"]}
    imm_events = sorted(imm_set)
    # The factor that matters is measured on events the CURRENT estimator
    # priced. Anything older is the pre-amendment model scored against
    # post-amendment payouts and calibrates nothing, so record it separately
    # instead of blending the two into one meaningless lifetime number.
    cut = _ts(AMENDMENT_CUTOVER + "+00:00")
    post = [r for r in rows if r["settled_any"] and r["first"] and r["first"] >= cut]
    pc = sum(r["credited"] for r in post)
    pe = sum(r["est_floor"] for r in post)
    # BY FAMILY, because the headline ratio is not a property of "the
    # estimator" — it is a property of the families that happen to have
    # settled AND been paid. Hourly temp settles within the hour, so it
    # dominates the evidence base; a 5-day earnings-mention program that has
    # not paid yet contributes to the digest's reward column while
    # contributing NOTHING to this ratio. Reporting one blended number let a
    # temp-only measurement read as a whole-book accuracy claim.
    fam_agg = defaultdict(lambda: [0.0, 0.0, 0])
    for r in post:
        a = fam_agg[reward_family(r["series"])]
        a[0] += r["credited"]
        a[1] += r["est_floor"]
        a[2] += 1
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "imm_inception": IMM_INCEPTION,
        "payout_floor": PAYOUT_FLOOR,
        "credited_lifetime_account": round(summary["lifetime"], 2),
        "credited_since_inception": round(summary["since_imm"], 2),
        "credited_imm_attributable": round(summary["imm_credit"], 2),
        "credited_non_imm": round(summary["lifetime"] - summary["imm_credit"], 2),
        "settled_credited": round(summary["settled_credited"], 2),
        "settled_estimate_floored": round(summary["settled_est_floor"], 2),
        "realization_factor": (round(summary["settled_credited"]
                                     / summary["settled_est_floor"], 4)
                               if summary["settled_est_floor"] > 0.01 else None),
        "series": summary["series"],
        "imm_event_count": len(imm_events),
        # IMM-attributable credit per credit-date. The digest's day/week/MTD
        # windows read this so they are filtered the same way the lifetime
        # figure is — otherwise "month to date" quietly includes the MLB and
        # fight-mention credits that belong to the other bots on this key.
        "credited_by_date_imm": {
            d: round(v, 2) for d, v in sorted(_by_date_imm(ledger, imm_set).items())},
        "post_amendment": {
            "cutover": AMENDMENT_CUTOVER,
            "events": len(post),
            "credited": round(pc, 2),
            "estimate_floored": round(pe, 2),
            "realization_factor": round(pc / pe, 4) if pe > 0.01 else None,
            "by_family": {
                k: {"events": v[2], "credited": round(v[0], 2),
                    "estimate_floored": round(v[1], 2),
                    "realization_factor": round(v[0] / v[1], 4) if v[1] > 0.01 else None}
                for k, v in sorted(fam_agg.items())},
        },
    }
    os.makedirs(STATUS_DIR, exist_ok=True)
    tmp = CALIB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, CALIB_PATH)
    log(f"wrote {CALIB_PATH}")
    return payload


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--statement", help="Kalshi UI rewards paste to ingest")
    ap.add_argument("--report", action="store_true", help="print the reconciliation")
    ap.add_argument("--refresh-programs", action="store_true",
                    help="re-pull incentive program periods / paid_out from the API")
    ap.add_argument("--since", help="only calibrate on events first quoted after this "
                                    f"ISO instant (default all; {AMENDMENT_CUTOVER} "
                                    "= post-amendment estimator only)")
    ap.add_argument("--floor", type=float, default=PAYOUT_FLOOR,
                    help="per-market payout floor to model (default 1.00)")
    ap.add_argument("--verbose", action="store_true", help="show every series")
    ap.add_argument("--no-write", action="store_true",
                    help="do not update reward_calibration.json")
    args = ap.parse_args(argv)

    if args.statement:
        rows, bad = parse_statement(args.statement)
        totals = statement_totals(args.statement)
        parsed = sum(r[2] for r in rows)
        log(f"parsed {len(rows)} credits totalling ${parsed:,.2f} from "
            f"{os.path.basename(args.statement)}")
        if bad:
            log(f"! {len(bad)} credit-looking lines did NOT parse — check the UI "
                f"format before trusting this ledger; first: {bad[0]}")
        if "lifetime" in totals:
            diff = totals["lifetime"] - parsed
            ok = "OK" if abs(diff) < 0.005 else f"MISMATCH ${diff:+,.2f}"
            log(f"  statement says lifetime ${totals['lifetime']:,.2f} -> {ok}")
            if abs(diff) >= 0.005:
                log("  (a non-zero difference usually means the paste is "
                    "PARTIAL — scroll the rewards page to the bottom first)")
        if "month" in totals:
            mon, amt = totals["month"]
            got = sum(r[2] for r in rows if r[0][:7] == datetime.strptime(
                mon, "%b %Y").strftime("%Y-%m"))
            log(f"  statement says {mon} ${amt:,.2f} -> ledger ${got:,.2f}")
        merged, added = merge_ledger(load_ledger(), rows)
        write_ledger(merged)
        log(f"ledger {LEDGER_PATH}: {len(merged)} credits (+{added} new)")

    if args.refresh_programs:
        refresh_programs()

    if args.report or args.statement:
        if not load_programs():
            log("note: no program cache — run with --refresh-programs to tell "
                "'never paid' apart from 'not paid yet'")
        rec, ledger = reconcile(since=args.since, floor=args.floor)
        summary = report(rec, ledger, floor=args.floor, verbose=args.verbose)
        if not args.no_write:
            write_calibration(summary, rec, ledger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
