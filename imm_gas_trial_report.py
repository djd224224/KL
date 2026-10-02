#!/usr/bin/env python3
r"""imm_gas_trial_report.py -- the 1pm-midnight gas trial, reported at the
1-week and 2-week marks (Jack 2026-10-01: "adjust all three to trade
1pm-midnight. and report on results at the 1 week and 2 week marks", then
"National + diesel only").

THE TRIAL (incentive_mm GAS_TRIAL_*, GB_NATGAS_ENABLE / GB_DIESEL_ENABLE /
GB_STATE_QUOTE): the national gas daily KXAAAGASD and the diesel daily
KXDIESELD quote PLAIN (their GasBuddy gates off) from 13:00 ET to the close
only -- the daily no-quote window is 00:00-13:00 ET; the 26 state dailies
are blocked; the GasBuddy refresher keeps logging. The trial starts at the
bot's first "gas trial:" startup line (run-logs/incentive-mm/
incentive-mm-*.log).

WHAT IT REPORTS, per family, from the trial start to now:
  * quoting: ET hours with orders placed, markets quoted, placements inside
    the 00:00-13:00 ET window (must be 0) and on the state dailies (must
    be 0);
  * fills: count and contracts, by ET band and by side;
  * trading P&L: settled fills at Kalshi's result, open fills at the current
    mid -- a one-sided or 50c+ wide book at the last trade clamped to its
    touch, see yes_mark (signed reads through kalshi_reads.py);
  * reward: the bot's estimate rebuilt from its cycle logs (est_frac x
    pool_per_day over each cycle, as imm_reward_recon.py does), with the
    exchange's $1-per-market floor applied, and Kalshi's CREDITS beside it
    where a statement has been pasted (reward_credits.csv);
  * net = reward + trading, against September's plain quoting in the same
    hours (BASELINE);
  * verdict: KEEP while the trading loss stays under the reward, STOP once
    it does not -- advisory, nothing here changes the bot.

SCHEDULE: "KL imm gas-trial" (register_imm_gas_trial.ps1), daily 07:45 ET.
It emails only on the first run at or after day 7 (the 1-week report) and
day 14 (the 2-week report) of the trial -- markers gas_trial_sent_<1w|2w>.
marker in the status dir -- so both land on time whatever day the trial went
live; on every other run it exits quietly.

USAGE:
  python imm_gas_trial_report.py            # the scheduled run
  python imm_gas_trial_report.py --dry      # build and print; no email, no marker
  python imm_gas_trial_report.py --test     # send now; no marker
  python imm_gas_trial_report.py --since 2026-10-01T17:00:00Z   # override the start
STRICTLY READ-ONLY apart from the sent-markers. All console output is ASCII
(the task console is cp1252).
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    ET = timezone(timedelta(hours=-4))

KL = os.path.dirname(os.path.abspath(__file__))
FAMILIES = {"KXAAAGASD": "national", "KXDIESELD": "diesel"}
STATE_RE = re.compile(r"KXAAAGASD[A-Z][A-Z]")
TRIAL_START_HOUR_ET = 13
BANDS = (("13-17 ET", 13, 18), ("18-20 ET", 18, 21), ("21-24 ET", 21, 24))
MARKS = ((7.0, "1w", "1-week"), (14.0, "2w", "2-week"))
PAYOUT_FLOOR = 1.00
MAX_DT = 900.0                                   # the recon's cycle-gap cap
# incentive_mm.MARK_WIDE_SPREAD_CENTS, off the bot's own knob: a two-sided
# book this many cents wide or wider marks at the last trade clamped inside
# its touch, not the mid (Jack 2026-10-01, "Yes, at 50c+"); 0 = off.
MARK_WIDE_SPREAD_CENTS = int(os.environ.get("IMM_MARK_WIDE_SPREAD", "50"))
# September 2026, plain quoting before the GasBuddy gates (settled fills;
# reward = the bot's estimate -- Kalshi credited about two thirds of it on
# the national). The numbers the trial was chosen on: IN-SAMPLE.
BASELINE = {
    "national": {"contracts": 12297, "trading": 126.40, "est_reward": 145.91,
                 "morning_trading": -197.48, "morning_est_reward": 158.54},
    "diesel": {"contracts": 3296, "trading": 13.04, "est_reward": 67.0,
               "morning_trading": -214.67, "morning_est_reward": 53.0},
}
_LOG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z (?:\[IMM\] )?gas trial:")


def family_of(ticker: str):
    s = (ticker or "").split("-")[0]
    if s in FAMILIES:
        return FAMILIES[s]
    return "state" if STATE_RE.fullmatch(s) else None


def trial_start(status_dir: str, first_day: str = "2026-09-30"):
    """UTC instant of the first "gas trial:" startup line, else None."""
    for path in sorted(glob.glob(os.path.join(status_dir, "incentive-mm-*.log"))):
        day = os.path.basename(path)[len("incentive-mm-"):-len(".log")]
        if day < first_day:
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "gas trial:" not in line:
                    continue
                m = _LOG_RE.match(line)
                if m:
                    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc)
    return None


def _day_files(status_dir: str, prefix: str, ext: str, since: datetime):
    first = (since - timedelta(days=1)).strftime("%Y-%m-%d")
    for path in sorted(glob.glob(os.path.join(status_dir, f"{prefix}_*.{ext}"))):
        day = os.path.basename(path)[len(prefix) + 1:-len(ext) - 1]
        if day >= first:
            yield path


def _jsonl(status_dir: str, prefix: str, since: datetime):
    for path in _day_files(status_dir, prefix, "jsonl", since):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "KXAAAGASD" not in line and "KXDIESELD" not in line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def _ts(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fill_pnl(fill: dict, outcome) -> float:
    """$ P&L of one fill against `outcome` (1.0 = YES, 0.0 = NO, or a mid in
    0..1 for an open market). A YES buy pays yes_price; a NO buy is a YES
    sale at yes_price."""
    p = float(fill["yes_price_cents"]) / 100.0
    n = float(fill["count"])
    return (outcome - p) * n if fill["side"] == "yes" else (p - outcome) * n


def _dollars(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def yes_mark(bid: float, ask: float, last: float):
    """The YES mark (0..1) off one market read (yes_bid, yes_ask, last_price
    in dollars), or None when the read carries no price. incentive_mm.
    touch_mark_cents's rule, kept local so these pure functions import no
    bot. A side is live only strictly inside (0, $1): Kalshi reads an empty
    bid as $0 and an empty ask as $1.00. A two-sided book marks at the mid,
    or, MARK_WIDE_SPREAD_CENTS or wider, at the last trade clamped inside
    the touch (the mid if it never traded); a one-sided book at the last
    trade clamped to the live side, max(last, bid) with only a bid and
    min(last, ask) with only an offer; an empty book at the last trade.
    Until 2026-10-01 this took (bid + ask) / 2 whenever the ask read above
    0, so a stray bid under no offer marked half way to $1, a lone offer
    half way to $0, and an empty book at 50c."""
    has_bid, has_ask = 0.0 < bid < 1.0, 0.0 < ask < 1.0
    if has_bid and has_ask:
        if (last > 0.0 and MARK_WIDE_SPREAD_CENTS > 0
                and round(100.0 * (ask - bid), 6) >= MARK_WIDE_SPREAD_CENTS):
            return min(max(last, bid), ask)
        return (bid + ask) / 2.0
    if not last > 0.0:
        return None
    if has_bid:
        return max(last, bid)
    if has_ask:
        return min(last, ask)
    return last


def outcomes(events, get_all=None) -> dict:
    """{ticker: (value, settled)}: 1.0 / 0.0 on a result, else the YES mark
    (yes_mark); a market with no price at all is left out."""
    if get_all is None:
        sys.path.insert(0, KL)
        from kalshi_reads import kalshi_get_all as get_all
    out = {}
    for ev in sorted(events):
        for m in get_all("/markets", {"event_ticker": ev, "limit": 1000}, "markets"):
            r = m.get("result")
            if r in ("yes", "no"):
                out[m["ticker"]] = (1.0 if r == "yes" else 0.0, True)
                continue
            mark = yes_mark(_dollars(m.get("yes_bid_dollars")), _dollars(m.get("yes_ask_dollars")),
                            _dollars(m.get("last_price_dollars")))
            if mark is not None:
                out[m["ticker"]] = (mark, False)
    return out


def scan_rewards(status_dir: str, since: datetime, until: datetime) -> dict:
    """{ticker: estimated $} for the trial families from the cycle logs:
    each family row accrues est_frac x pool_per_day over the gap since the
    previous cycle (any ticker's), capped at MAX_DT -- imm_reward_recon's
    integral, read only on the rows that matter."""
    lo, hi = since.timestamp(), until.timestamp()
    est = defaultdict(float)
    for path in _day_files(status_dir, "cycle_log", "csv", since):
        prev_ts = cur_ts = None
        cur_s = None
        with open(path, encoding="utf-8", errors="replace") as f:
            next(f, None)
            for line in f:
                ts_s = line[:20]
                if ts_s != cur_s:
                    try:
                        t = datetime.strptime(ts_s, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        continue
                    prev_ts, cur_ts, cur_s = cur_ts, t, ts_s
                if ",KXAAAGASD-" not in line and ",KXDIESELD-" not in line:
                    continue
                if prev_ts is None or not (lo <= cur_ts <= hi):
                    continue
                row = next(csv.reader([line]))
                try:
                    frac, pool = float(row[7]), float(row[11])
                except (IndexError, ValueError):
                    continue
                dt = min(cur_ts - prev_ts, MAX_DT)
                if dt > 0:
                    est[row[1]] += frac * pool * dt / 86400.0
    return dict(est)


def credits(status_dir: str, since: datetime) -> dict:
    """{family: (credited $, rows)} from the pasted-statement ledger."""
    out = defaultdict(lambda: [0.0, 0])
    path = os.path.join(status_dir, "reward_credits.csv")
    if not os.path.exists(path):
        return {}
    first = since.astimezone(ET).date().isoformat()
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("kind") or "").lower() != "liquidity" or (r.get("credit_date") or "") < first:
                continue
            fam = family_of(r.get("event_ticker") or "")
            if fam in ("national", "diesel"):
                out[fam][0] += float(r.get("amount") or 0)
                out[fam][1] += 1
    return {k: tuple(v) for k, v in out.items()}


def build(status_dir: str, since: datetime, now: datetime, get_all=None) -> dict:
    fams = {f: {"fills": 0, "contracts": 0.0, "bands": defaultdict(lambda: [0.0, 0.0]),
                "sides": defaultdict(lambda: [0.0, 0.0]), "settled": 0.0, "open": 0.0,
                "markets": set(), "hours": set(), "early": 0}
            for f in ("national", "diesel")}
    state_orders = 0
    lo = since.timestamp()
    for o in _jsonl(status_dir, "orders", since):
        t = _ts(o.get("ts"))
        fam = family_of(o.get("ticker", ""))
        if t is None or t < lo or o.get("kind") not in ("place", "amend"):
            continue
        if fam == "state":
            state_orders += 1
        elif fam in fams:
            et = datetime.fromtimestamp(t, ET)
            fams[fam]["markets"].add(o["ticker"])
            fams[fam]["hours"].add((et.date().isoformat(), et.hour))
            if et.hour < TRIAL_START_HOUR_ET:
                fams[fam]["early"] += 1
    fills = [f for f in _jsonl(status_dir, "fills", since)
             if family_of(f.get("ticker", "")) in fams and (_ts(f.get("ts")) or 0) >= lo]
    outc = outcomes({f["event_ticker"] for f in fills}, get_all) if fills else {}
    for f in fills:
        d = fams[family_of(f["ticker"])]
        o = outc.get(f["ticker"])
        if o is None:
            continue
        pnl = fill_pnl(f, o[0])
        n = float(f["count"])
        d["fills"] += 1
        d["contracts"] += n
        d["settled" if o[1] else "open"] += pnl
        h = datetime.fromtimestamp(_ts(f["ts"]), ET).hour
        band = next((b for b, a, z in BANDS if a <= h < z), "before 13 ET")
        d["bands"][band][0] += n
        d["bands"][band][1] += pnl
        side = "bought YES" if f["side"] == "yes" else "sold YES"
        d["sides"][side][0] += n
        d["sides"][side][1] += pnl
    est = scan_rewards(status_dir, since, now)
    cred = credits(status_dir, since)
    for fam, d in fams.items():
        mk = {t: v for t, v in est.items() if family_of(t) == fam}
        d["est_raw"] = sum(mk.values())
        d["est_floored"] = sum(v for v in mk.values() if v >= PAYOUT_FLOOR)
        d["est_markets"] = len(mk)
        d["credited"] = cred.get(fam)
        d["trading"] = d["settled"] + d["open"]
        d["net"] = d["est_floored"] + d["trading"]
        d["verdict"] = verdict(d)
    return {"since": since, "now": now, "days": (now - since).total_seconds() / 86400.0,
            "fams": fams, "state_orders": state_orders}


def verdict(d: dict) -> str:
    if d["fills"] == 0 and not d["hours"]:
        return "NOT QUOTING -- check the bot (selection / blackout)"
    if d["trading"] < 0 and -d["trading"] > d["est_floored"]:
        return "STOP -- the trading loss is larger than the reward"
    return "KEEP"


def _m(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def render(ctx: dict, label: str) -> str:
    s, n = ctx["since"], ctx["now"]
    lines = [f"IMM gas trial -- {label} report ({ctx['days']:.1f} days)",
             f"Trial: KXAAAGASD (national gas daily) + KXDIESELD (diesel daily), plain quoting "
             f"13:00 ET to the close; state dailies blocked.",
             f"Window: {s.astimezone(ET):%Y-%m-%d %H:%M} ET -> {n.astimezone(ET):%Y-%m-%d %H:%M} ET.",
             f"State-daily placements in the window: {ctx['state_orders']} (must be 0)."]
    for fam in ("national", "diesel"):
        d, b = ctx["fams"][fam], BASELINE[fam]
        lines += ["", f"== {fam.upper()} ({'KXAAAGASD' if fam == 'national' else 'KXDIESELD'}) =="]
        lines.append(f"verdict: {d['verdict']}")
        lines.append(f"quoting: {len(d['hours'])} ET hours, {len(d['markets'])} markets; "
                     f"placements before 13:00 ET: {d['early']} (must be 0)")
        lines.append(f"fills: {d['fills']} ({d['contracts']:,.0f} contracts)")
        for band, _a, _z in BANDS + (("before 13 ET", 0, 13),):
            c, p = d["bands"].get(band, [0.0, 0.0])
            if c:
                lines.append(f"  {band:12} {c:7,.0f} ct  {_m(p):>10}  ({100 * p / c:+.1f}c/ct)")
        for side, (c, p) in sorted(d["sides"].items()):
            lines.append(f"  {side:12} {c:7,.0f} ct  {_m(p):>10}  ({100 * p / c:+.1f}c/ct)")
        lines.append(f"trading P&L: {_m(d['trading'])} (settled {_m(d['settled'])}, "
                     f"open at mid {_m(d['open'])})")
        cr = d["credited"]
        lines.append(f"reward (bot estimate, $1/market floor applied): {_m(d['est_floored'])} "
                     f"(raw {_m(d['est_raw'])} over {d['est_markets']} markets)"
                     + (f"; Kalshi credited {_m(cr[0])} on {cr[1]} events so far" if cr else
                        "; no Kalshi credits pasted for the window yet"))
        lines.append(f"NET (estimated reward + trading): {_m(d['net'])}"
                     + (f" = {_m(d['net'] / max(ctx['days'], 0.01))}/day" if ctx["days"] > 0 else ""))
        lines.append(f"September baseline, same hours, plain (in-sample, ~20 trading days): "
                     f"trading {_m(b['trading'])} on {b['contracts']:,} ct + est reward "
                     f"{_m(b['est_reward'])}; the dropped 08-13 ET block: trading "
                     f"{_m(b['morning_trading'])}, est reward {_m(b['morning_est_reward'])}")
    lines += ["", "Reward figures are the bot's own estimates (Kalshi credited about two thirds "
                  "of the national's in September); trading P&L is measured. To end the trial: "
                  "IMM_GAS_TRIAL_BLACKOUT_ET=\"\" (back to full-day plain quoting) or a blocklist "
                  "entry for KXAAAGASD / KXDIESELD (stop)."]
    return "\n".join(lines)


def due_mark(days: float, sent: set):
    """(key, label, keys to mark) of the report due now, else None: the
    latest reached mark not yet sent (a missed 1-week is folded into the
    2-week)."""
    due = None
    for d, key, label in MARKS:
        if days >= d and key not in sent:
            due = (key, label)
    if due is None:
        return None
    keys = [k for d, k, _l in MARKS if days >= d and k not in sent]
    return due[0], due[1], keys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="build and print; no email, no marker")
    ap.add_argument("--test", action="store_true", help="send now; no marker")
    ap.add_argument("--since", default="", help="trial start (ISO UTC) instead of the log line")
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
    since = (datetime.fromisoformat(a.since.replace("Z", "+00:00")) if a.since
             else trial_start(status))
    if since is None:
        imm.log("[GAS-TRIAL] no 'gas trial:' startup line yet -- the trial has not started; nothing to report")
        return 0
    now = datetime.now(timezone.utc)
    days = (now - since).total_seconds() / 86400.0
    marker = lambda k: os.path.join(status, f"gas_trial_sent_{k}.marker")   # noqa: E731
    sent = {k for _d, k, _l in MARKS if os.path.exists(marker(k))}
    due = due_mark(days, sent)
    if not (a.dry or a.test) and due is None:
        imm.log(f"[GAS-TRIAL] day {days:.1f} of the trial: no report due "
                f"(sent: {', '.join(sorted(sent)) or 'none'})")
        return 0
    label = due[1] if due else ("interim (day %.1f)" % days)
    ctx = build(status, since, now)
    text = render(ctx, label)
    print(text)
    if a.dry:
        return 0
    subject = (f"IMM gas trial {label} report - national {_m(ctx['fams']['national']['net'])}, "
               f"diesel {_m(ctx['fams']['diesel']['net'])} net "
               f"({ctx['fams']['national']['verdict'].split(' ')[0]} / "
               f"{ctx['fams']['diesel']['verdict'].split(' ')[0]})")
    alerter = imm.Alerter("IMM-GAS-TRIAL", live=True)
    if not alerter.enabled:
        imm.log("[GAS-TRIAL] cannot send: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, (2 if a.test else 6) + 1):
        ok = alerter.send_message(text, subject=subject)
        if ok:
            break
        imm.log(f"[GAS-TRIAL] send attempt {attempt} failed; retrying in 5 min")
        time.sleep(300)
    imm.log(f"[GAS-TRIAL] email {'sent' if ok else 'FAILED'}: {subject}")
    if ok and not a.test and due:
        for k in due[2]:
            with open(marker(k), "w") as f:
                f.write(now.isoformat() + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
