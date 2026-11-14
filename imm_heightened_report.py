#!/usr/bin/env python3
"""The heightened bait mode's dry-run score (HEIGHTENED_MODE, Jack 2026-11-13:
"anytime there is suspicious activity e.g. show better price and pull 3
times, move to 'heightened awareness bait mode'", then "Build it dry").

Since 2026-11-13 the IMM counts PULLS -- a side's external target/5
reference that improves and reverts within 2 min with no print at the better
price -- and opens a window on a market (or its event) after 3 in 30 min
(imm_heightened.py). Dry, nothing changes; this weighs what acting would
have been worth:

  lost      our maker fills inside the windows, marked out (4 h, + = the fill
            lost money): the most the live actions could have saved
  forgone   our reward an hour over the windows' markets for the time they
            were open: what standing fully aside would have cost (the live
            actions keep base size, so they would cost less)
  inside vs outside
            c/ct at 4 h of our fills inside windows against all our other
            maker fills: is a heightened market worse to be filled in?

PRE-REGISTERED VERDICT (the exempt series -- mention books, crypto -- left
out; needs 7+ UTC days):
  GO     lost >= forgone and lost >= $5/day: build the live actions as an A/B
  WATCH  lost >= 25% of forgone
  STOP   lost < 25% of forgone, or more than 30 trips a day

Inputs, in the bot's STATUS_DIR:
  heightened_<UTC date>.jsonl    pull / trip / clear / fill / actor lines
  fills_<date>.jsonl             every fill of ours (the outside comparison)
  dashboard/imm_dashboard.html   each fill's 5-min and 4-hour mark-outs
  reward_est_hourly_cache.json   our reward per market-hour

Writes run-logs/incentive-mm/heightened_report_<ET date>.md and prints its
path. Read-only: no Kalshi call, nothing in the bot touched.

  python imm_heightened_report.py [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--dir DIR]
"""
import argparse
import collections
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from imm_fast_cancel_report import ET, load_markouts, load_reward_rates, markout

DEFAULT_DIR = r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm"
MIN_DAYS = 7
MAX_TRIPS_PER_DAY = 30.0
GO_MIN_PER_DAY = 5.0
WATCH_SHARE = 0.25


def series_of(ticker):
    return (ticker or "").split("-", 1)[0]


def event_of(ticker):
    """The bot's _event_of: the ticker less its last part."""
    return (ticker or "").rsplit("-", 1)[0]


def family(series):
    try:
        import imm_dashboard
        return imm_dashboard.family_of(series)[0]
    except Exception:
        return series


def _days(since, until):
    d = since
    while d <= until:
        yield d
        d += timedelta(days=1)


def _jsonl(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def load_rows(d, since, until):
    out = collections.defaultdict(list)
    for day in _days(since, until):
        for r in _jsonl(os.path.join(d, f"heightened_{day.isoformat()}.jsonl")):
            out[r.get("ev")].append(r)
    for rows in out.values():
        rows.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def load_our_fills(d, since, until):
    """Our maker fills, every market."""
    out = []
    for day in _days(since - timedelta(days=1), until + timedelta(days=1)):
        for r in _jsonl(os.path.join(d, f"fills_{day.isoformat()}.jsonl")):
            if not r.get("is_taker") and r.get("ticker"):
                out.append(r)
    return out


def windows_of(rows, now_ts):
    """[{key, scope, ticker, event, since, until, exempt}] from the trip
    lines, closed by their clear lines (a window still open ends now or at
    its trip's until, whichever is first)."""
    clears = collections.defaultdict(list)
    for c in rows.get("clear", ()):
        clears[c["key"]].append(c)
    out = []
    for t in rows.get("trip", ()):
        since = float(t["ts"])
        until = min(float(t.get("until") or since), now_ts)
        c = next((c for c in clears.get(t["key"], ())
                  if abs(float(c.get("since") or 0) - since) < 1.0), None)
        if c is not None:
            until = float(c["until"])
        out.append({"key": t["key"], "scope": t.get("scope"),
                    "ticker": t.get("ticker") if t.get("scope") == "market" else None,
                    "event": t.get("event") or event_of(t.get("ticker")),
                    "since": since, "until": until, "exempt": t.get("exempt"),
                    "trip": t})
    return out


def covers(w, ticker):
    return (w["ticker"] == ticker) if w["scope"] == "market" \
        else (event_of(ticker) == w["event"])


def forgone_of(wins, rates):
    """Our modelled reward over each ticker's union of window time."""
    by_event = collections.defaultdict(set)
    for (tk, _h) in rates:
        by_event[event_of(tk)].add(tk)
    spans = collections.defaultdict(list)          # ticker -> [(a, b)]
    for w in wins:
        tks = [w["ticker"]] if w["scope"] == "market" else by_event.get(w["event"], ())
        for tk in tks:
            spans[tk].append((w["since"], w["until"]))
    total = 0.0
    per_key = collections.Counter()
    for tk, sp in spans.items():
        sp.sort()
        merged = []
        for a, b in sp:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            t = a
            while t < b:
                h = int(t // 3600)
                seg = min(b, (h + 1) * 3600.0) - t
                x = rates.get((tk, h), 0.0) * seg / 3600.0
                total += x
                per_key[event_of(tk)] += x
                t += seg
    return total, per_key


def mark(fill, marks):
    return markout({"ts": fill.get("ts"), "ticker": fill.get("ticker"),
                    "count": fill.get("count")}, marks)


def verdict(days, trips_per_day, lost_per_day, forgone_per_day):
    if days < MIN_DAYS:
        return "TOO EARLY", f"{days} of {MIN_DAYS} days"
    if trips_per_day > MAX_TRIPS_PER_DAY:
        return "STOP", f"{trips_per_day:.1f} trips a day (over {MAX_TRIPS_PER_DAY:g})"
    if lost_per_day >= forgone_per_day and lost_per_day >= GO_MIN_PER_DAY:
        return "GO", "fills inside windows lost more than the windows' reward"
    if lost_per_day >= WATCH_SHARE * forgone_per_day and lost_per_day > 0:
        return "WATCH", "fills inside windows lost 25-100% of the windows' reward"
    return "STOP", "fills inside windows lost under 25% of the windows' reward"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    today = datetime.now(timezone.utc).date()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--since", default="2026-11-14")
    ap.add_argument("--until", default=today.isoformat())
    a = ap.parse_args(argv)
    since, until = date.fromisoformat(a.since), date.fromisoformat(a.until)
    days = (until - since).days + 1
    now_ts = datetime.now(timezone.utc).timestamp()
    rows = load_rows(a.dir, since, until)
    marks = load_markouts(a.dir)
    rates = load_reward_rates(a.dir)
    wins = windows_of(rows, now_ts)
    live = [w for w in wins if not w["exempt"]]
    exempt = [w for w in wins if w["exempt"]]
    pulls = rows.get("pull", [])
    trips = [w["trip"] for w in live]

    # fills inside windows (the sink's own lines), counted once
    seen = set()
    inside, inside_ex = [], []
    for f in rows.get("fill", ()):
        k = (f.get("order_id"), round(float(f.get("ts") or 0), 1))
        if k in seen:
            continue
        seen.add(k)
        (inside_ex if f.get("exempt") else inside).append(f)
    m4_in = [(f, mark(f, marks)[1]) for f in inside]
    lost = -sum(m for _f, m in m4_in if m is not None)
    unmarked = sum(1 for _f, m in m4_in if m is None)
    forgone, forgone_by_ev = forgone_of(live, rates)

    # inside vs outside, every maker fill of ours over the same days
    t_lo = datetime(since.year, since.month, since.day, tzinfo=timezone.utc).timestamp()
    t_hi = t_lo + days * 86400.0
    ins = {"n": 0, "ct": 0.0, "usd": 0.0}
    out = {"n": 0, "ct": 0.0, "usd": 0.0}
    for f in load_our_fills(a.dir, since, until):
        ts = float(f.get("ts") or 0)
        if not (t_lo <= ts < t_hi):
            continue
        m4 = mark(f, marks)[1]
        if m4 is None:
            continue
        hit = any(w["since"] <= ts < w["until"] and covers(w, f["ticker"]) for w in live)
        b = ins if hit else out
        b["n"] += 1
        b["ct"] += float(f.get("count") or 0)
        b["usd"] += m4

    def cpc(b):
        return 100.0 * b["usd"] / b["ct"] if b["ct"] else 0.0

    v, why = verdict(days, len(trips) / days, lost / days, forgone / days)
    L = [f"# Heightened bait mode, dry run: {since} to {until} ({days} UTC day(s))", ""]
    L.append(f"**Verdict: {v}** ({why}).")
    L.append("")
    L.append(f"Our fills inside the windows lost **${lost:,.2f}** at 4 h "
             f"(${lost / days:,.2f} a day, {len(inside)} fills, {unmarked} unmarked), "
             f"against **${forgone:,.2f}** of reward the windows held "
             f"(${forgone / days:,.2f} a day). Standing fully aside would have "
             f"netted ${lost - forgone:+,.2f}; the live actions keep base size, so "
             f"they would cost less and save less.")
    L.append("")
    L.append(f"Inside the windows our fills marked {cpc(ins):+.2f}c a contract at 4 h "
             f"({ins['n']} fills, {ins['ct']:,.0f} contracts); everywhere else "
             f"{cpc(out):+.2f}c ({out['n']} fills, {out['ct']:,.0f} contracts).")
    L.append("")
    joined = sum(1 for p in pulls if p.get("joined"))
    L.append(f"Pulls: {len(pulls)} ({len(pulls) / days:,.1f} a day; our rung had "
             f"followed {joined}). Trips: {len(trips)} counted "
             f"({len(trips) / days:,.1f} a day: "
             f"{sum(1 for t in trips if t.get('scope') == 'market')} market, "
             f"{sum(1 for t in trips if t.get('scope') == 'event')} event; "
             f"{sum(1 for t in trips if t.get('to_midnight'))} ran to midnight), "
             f"{len(exempt)} exempt. Repeat sizes seen: {len(rows.get('actor', []))}.")
    if inside_ex:
        ex_lost = -sum(m for m in (mark(f, marks)[1] for f in inside_ex) if m is not None)
        L.append(f"Exempt windows (mention, crypto): {len(inside_ex)} fills inside, "
                 f"${ex_lost:,.2f} lost at 4 h.")
    L += ["", "| UTC day | pulls | trips | fills inside | lost 4h | forgone |",
          "|---|---:|---:|---:|---:|---:|"]
    for d in _days(since, until):
        lo = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
        hi = lo + 86400.0

        def _in(r):
            return lo <= float(r.get("ts") or 0) < hi
        fd = [(f, m) for f, m in m4_in if _in(f)]
        fg, _ = forgone_of([dict(w, since=max(w["since"], lo), until=min(w["until"], hi))
                            for w in live if w["until"] > lo and w["since"] < hi], rates)
        L.append(f"| {d} | {sum(1 for p in pulls if _in(p))} | "
                 f"{sum(1 for t in trips if _in(t))} | {len(fd)} | "
                 f"${-sum(m for _f, m in fd if m is not None):,.2f} | ${fg:,.2f} |")
    by_fam = collections.defaultdict(lambda: {"trips": 0, "fills": 0, "lost": 0.0})
    for t in trips:
        by_fam[family(series_of(t.get("ticker")))]["trips"] += 1
    for f, m in m4_in:
        b = by_fam[family(series_of(f.get("ticker")))]
        b["fills"] += 1
        b["lost"] -= m or 0.0
    if by_fam:
        L += ["", "| family | trips | fills inside | lost 4h |", "|---|---:|---:|---:|"]
        for fam, b in sorted(by_fam.items(), key=lambda kv: -kv[1]["lost"]):
            L.append(f"| {fam} | {b['trips']} | {b['fills']} | ${b['lost']:,.2f} |")
    top = sorted(m4_in, key=lambda fm: (fm[1] if fm[1] is not None else 0.0))[:10]
    if top:
        L += ["", "Biggest losing fills inside windows:", ""]
        for f, m in top:
            if m is None or m >= 0:
                continue
            L.append(f"- {datetime.fromtimestamp(float(f['ts']), timezone.utc):%m-%d %H:%M:%SZ} "
                     f"{f.get('ticker')} {f.get('side')} {f.get('count')} @ {f.get('px')}c, "
                     f"{f.get('since_trip_s')}s after the {f.get('scope')} trip: ${m:+,.2f}")
    L += ["", "Caveats: a fill the live mode avoided may come later at a worse "
          "price (or not at all); the reward is the bot's model; 4-h marks "
          "understate settlement swings; pulls are read off book changes "
          "sampled every second, so a better price shown and pulled within "
          "one second is missed."]
    name = f"heightened_report_{datetime.now(ET).strftime('%Y-%m-%d')}.md"
    path = os.path.join(a.dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
