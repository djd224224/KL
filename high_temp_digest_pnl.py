#!/usr/bin/env python3
"""P&L for the high-temp (KXHIGH) daily digest: this bot's own fills, each
valued by how its market settled, with the IMM's market making on the same
Kalshi account kept apart.

Why (Jack 2026-10-28, on the 10/27 digest "-$1,420.27 on $4,583.73":
"is this real -1400?"). It was not:
- The digest read every KXHIGH fill on the account. Since 2026-10-24 the IMM
  quotes next-day KXHIGH markets on the same account, and on 10/27 61 of the
  84 fills (6,500 of 6,848 contracts) were the IMM's. This bot made +$20.04.
- It took P&L as Kalshi's settlement revenue - YES cost - NO cost. When a YES
  and a NO in one market net out, Kalshi pays the $1 at once, and that cash
  is not in the settlement's revenue. The IMM buys both sides, so on 10/27
  the formula left out $599.29.

Here every fill is valued at settlement:
    contracts x (payout - price paid) - fee
at Kalshi's raw price for its side (raw_yes_price_dollars or
raw_no_price_dollars: BigQuery's views book NO fills at the YES price); a
sell counts the other way round.

Whose fill:
- bot: its order is in KXHIGH_orders (high_temp_monitoring joins it in, which
  sets KXHIGH_fills_clean.client_order_id);
- imm: in the IMM's own fill log (run-logs/incentive-mm/fills_*.jsonl in KL,
  read-only);
- unattributed: neither -- a gap in the bot's orders log, or a manual trade.

A market's result comes from KXHIGH_settlements_clean, else from Kalshi's
market record once it is final, else it is estimated from
KXHIGH_cli_readings and marked est.

    python high_temp_digest_pnl.py --date 2026-10-27                 # the digest's numbers
    python high_temp_digest_pnl.py --date 2026-10-27 --json          # the same as JSON
    python high_temp_digest_pnl.py --date 2026-10-27 --summary-line  # the email subject's line
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import urllib.request
from collections import defaultdict

PROJECT = "elite-contact-446323-q7"
DATASET = "Kalshi"
LOCATION = "northamerica-northeast1"
KL_DIR = r"C:\Users\jackd\Documents\KL"
IMM_FILL_LOG_DIR = (os.environ.get("IMM_FILL_LOG_DIR")
                    or os.path.join(KL_DIR, "run-logs", "incentive-mm"))
KALSHI_MARKET_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/"
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
OWNERS = ("bot", "imm", "unattributed")


def date_code(day: dt.date) -> str:
    """2026-10-27 -> '26OCT27', as in KXHIGHAUS-26OCT27-B82.5."""
    return f"{day:%y}{MONTHS[day.month - 1]}{day:%d}"


def parse_ticker(ticker: str):
    """'KXHIGHTATL-26OCT27-B72.5' -> ('TATL', 'B', 72.5). The city code is
    KXHIGH_cli_readings' city_abv (the fills' own city_abv is not reliable)."""
    series, _, strike = ticker.split("-", 2)
    return series[len("KXHIGH"):], strike[0], float(strike[1:])


def band_label(kind: str, strike: float, direction: str = "") -> str:
    if kind == "B":
        lo = math.floor(strike)
        return f"{lo}-{lo + 1}F"
    x = f"{strike:g}"
    if direction.startswith("less"):
        return f"<{x}F"
    if direction.startswith("greater"):
        return f">{x}F"
    return f"T{x}"


def estimate_yes(kind: str, strike: float, direction: str, actual):
    """Does the market settle YES at this actual high? None when it can't be told.
    B{X} is YES at floor(X) or floor(X)+1; a tail needs Kalshi's strike_type."""
    if actual is None:
        return None
    if kind == "B":
        lo = math.floor(strike)
        return lo <= actual <= lo + 1
    return {"less": actual < strike, "less_or_equal": actual <= strike,
            "greater": actual > strike, "greater_or_equal": actual >= strike}.get(direction)


def fill_value(side: str, action: str, count: float, yes_price: float,
               no_price: float, fee: float, result_yes: bool):
    """(P&L, cost) of one fill when its market settles result_yes."""
    price = yes_price if side == "yes" else no_price
    wins = result_yes if side == "yes" else not result_yes
    sign = 1.0 if action == "buy" else -1.0
    pnl = sign * count * ((1.0 if wins else 0.0) - price) - fee
    return pnl, (count * price if action == "buy" else 0.0)


def owner_of(fill: dict, imm_fill_ids) -> str:
    if fill["fill_id"] in imm_fill_ids:
        return "imm"
    if str(fill.get("client_order_id") or "").strip():
        return "bot"
    return "unattributed"


def load_imm_fill_ids(day: dt.date, log_dir: str = IMM_FILL_LOG_DIR):
    """fill_ids of the IMM's KXHIGH fills on D's markets, and whether any of its
    logs was there. It quotes them the day before, so read D-2 .. D+1."""
    code = "-" + date_code(day) + "-"
    ids, seen = set(), False
    for k in range(-2, 2):
        path = os.path.join(log_dir, f"fills_{day + dt.timedelta(days=k):%Y-%m-%d}.jsonl")
        if not os.path.exists(path):
            continue
        seen = True
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "KXHIGH" not in line or code not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t = str(r.get("ticker") or "")
                if t.startswith("KXHIGH") and code in t and r.get("fill_id"):
                    ids.add(r["fill_id"])
    return ids, seen


def build(day: dt.date, fills, settlements, actuals, meta, imm_fill_ids, imm_log_seen=True):
    """Everything the digest prints, from plain rows (no I/O here).
    fills: KXHIGH_fills_clean rows; settlements: KXHIGH_settlements_clean rows;
    actuals: {city_abv: (city, high_temp_f)}; meta: {ticker: {strike_type, result}}
    from Kalshi's market records."""
    results = {}
    for s in settlements:
        r = str(s.get("result") or "").lower()
        if r in ("yes", "no"):
            results[s["market_ticker"]] = (r == "yes", "settled")
    for t, m in meta.items():
        r = str(m.get("result") or "").lower()
        if t not in results and r in ("yes", "no"):
            results[t] = (r == "yes", "settled")

    seen, rows = set(), {}
    for f in sorted(fills, key=lambda f: (f["market_ticker"], str(f.get("fill_ts") or ""))):
        if f["fill_id"] in seen:
            continue
        seen.add(f["fill_id"])
        t = f["market_ticker"]
        owner = owner_of(f, imm_fill_ids)
        key = (t, owner)
        if key not in rows:
            city_abv, kind, strike = parse_ticker(t)
            direction = str(meta.get(t, {}).get("strike_type") or "")
            city, actual = actuals.get(city_abv, (city_abv, None))
            if t in results:
                result_yes, status = results[t]
            else:
                result_yes = estimate_yes(kind, strike, direction, actual)
                status = "est." if result_yes is not None else "pending"
            rows[key] = {"ticker": t, "owner": owner, "city_abv": city_abv, "city": city,
                         "band": band_label(kind, strike, direction), "actual": actual,
                         "result": (None if result_yes is None else ("YES" if result_yes else "NO")),
                         "status": status, "yes_ct": 0.0, "no_ct": 0.0, "cost": 0.0,
                         "pnl": (None if result_yes is None else 0.0), "fills": 0}
        row = rows[key]
        n = float(f["filled_count"])
        row["yes_ct" if f["side"] == "yes" else "no_ct"] += n
        row["fills"] += 1
        price = float(f["raw_yes_price_dollars"] if f["side"] == "yes" else f["raw_no_price_dollars"])
        row["cost"] += n * price if f.get("action", "buy") == "buy" else 0.0
        if row["result"] is not None:
            pnl, _ = fill_value(f["side"], f.get("action", "buy"), n,
                                float(f["raw_yes_price_dollars"]), float(f["raw_no_price_dollars"]),
                                float(f.get("fee_cost_dollars") or 0), row["result"] == "YES")
            row["pnl"] += pnl

    totals = {}
    for owner in OWNERS:
        rs = [r for r in rows.values() if r["owner"] == owner]
        known = [r for r in rs if r["pnl"] is not None]
        totals[owner] = {
            "pnl": round(sum(r["pnl"] for r in known), 2),
            "cost": round(sum(r["cost"] for r in rs), 2),
            "contracts": round(sum(r["yes_ct"] + r["no_ct"] for r in rs), 2),
            "fills": sum(r["fills"] for r in rs), "markets": len(rs),
            "wins": sum(1 for r in known if r["pnl"] > 0),
            "losses": sum(1 for r in known if r["pnl"] <= 0),
            "est": sum(1 for r in rs if r["status"] == "est."),
            "pending": sum(1 for r in rs if r["status"] == "pending"),
        }
    bot = totals["bot"]
    if not bot["markets"]:
        status = "no fills"
    elif bot["est"] + bot["pending"] == 0:
        status = "settled"
    elif bot["est"] + bot["pending"] < bot["markets"]:
        status = "partly settled"
    else:
        status = "estimated"

    # Account check on settled markets, every owner: the fills' sum against
    # Kalshi's settlement records. The gap is the $1 paid on YES/NO pairs that
    # netted before settlement; anything else means fills are missing.
    by_t = defaultdict(lambda: {"pnl": 0.0, "yes": 0.0, "no": 0.0})
    for r in rows.values():
        if r["status"] == "settled":
            by_t[r["ticker"]]["pnl"] += r["pnl"]
            by_t[r["ticker"]]["yes"] += r["yes_ct"]
            by_t[r["ticker"]]["no"] += r["no_ct"]
    rec, rec_cost = {}, {}
    for s in settlements:
        yc = float(s.get("yes_total_cost_dollars") or 0)
        nc = float(s.get("no_total_cost_dollars") or 0)
        rec[s["market_ticker"]] = (float(s.get("revenue_dollars") or 0) - yc - nc
                                   - float(s.get("fee_cost_dollars") or 0))
        rec_cost[s["market_ticker"]] = yc + nc
    # a record with a cost and no fills at all is a missing fill too
    checked = [t for t in rec if t in by_t or rec_cost[t] > 0]
    fills_sum = sum(by_t[t]["pnl"] for t in checked)
    record_sum = sum(rec[t] for t in checked)
    pairs = sum(min(by_t[t]["yes"], by_t[t]["no"]) for t in checked)
    check = {"markets": len(checked), "fills_sum": round(fills_sum, 2),
             "settlement_records": round(record_sum, 2), "netted_pairs": round(pairs, 2),
             "ok": abs((fills_sum - record_sum) - pairs) < 0.05}

    return {"date": day.isoformat(), "status": status, "totals": totals, "check": check,
            "imm_log_seen": imm_log_seen,
            "summary_line": f"{money(bot['pnl'], sign=True)} on {money(bot['cost'])}, "
                            f"{bot['wins']}W/{bot['losses']}L, {status}",
            "markets": sorted(rows.values(), key=lambda r: (r["owner"], r["city"], r["ticker"]))}


def money(x: float, sign: bool = False) -> str:
    s = f"${abs(x):,.2f}"
    if x < 0 and round(abs(x), 2) > 0:
        return "-" + s
    return ("+" + s) if sign else s


def owner_line(t: dict) -> str:
    extra = []
    if t["est"]:
        extra.append(f"{t['est']} est.")
    if t["pending"]:
        extra.append(f"{t['pending']} pending, not in the P&L")
    return (f"{money(t['pnl'], sign=True)} on {money(t['cost'])}, {t['fills']} fills, "
            f"{t['contracts']:,.1f} contracts in {t['markets']} markets"
            + (f" ({'; '.join(extra)})" if extra else ""))


def render(s: dict) -> str:
    t = s["totals"]
    out = [f"High-temp digest P&L for {s['date']}: fill level, this bot only",
           f"DIGEST_SUMMARY: {s['summary_line']}", "",
           f"This bot (orders in KXHIGH_orders): {owner_line(t['bot'])}, "
           f"{t['bot']['wins']}W/{t['bot']['losses']}L"]
    imm = owner_line(t["imm"]) if t["imm"]["markets"] else "none"
    out.append(f"IMM market making on the same account (counted in IMM's own P&L, not here): {imm}")
    if not s["imm_log_seen"]:
        out.append("  (the IMM's fill log was not found, so the IMM's fills show as unattributed)")
    un = t["unattributed"]
    out.append("Unattributed fills (in neither KXHIGH_orders nor the IMM's fill log): "
               + (owner_line(un) if un["markets"] else "none"))
    for owner, title in (("bot", "This bot by market"), ("unattributed", "Unattributed by market")):
        ms = [m for m in s["markets"] if m["owner"] == owner]
        if not ms:
            continue
        out += ["", f"{title}:",
                f"{'city':15s} {'market':28s} {'band':8s} {'actual':>6s} {'result':9s} "
                f"{'YES ct':>7s} {'NO ct':>7s} {'avg px':>7s} {'cost':>9s} {'P&L':>10s}"]
        for m in ms:
            ct = m["yes_ct"] + m["no_ct"]
            res = "-" if m["result"] is None else m["result"] + (" est." if m["status"] == "est." else "")
            pnl = "pending" if m["pnl"] is None else money(m["pnl"], sign=True)
            out.append(f"{m['city'][:15]:15s} {m['ticker']:28s} {m['band']:8s} "
                       f"{'-' if m['actual'] is None else m['actual']:>6} {res:9s} {m['yes_ct']:7.1f} "
                       f"{m['no_ct']:7.1f} {(m['cost'] / ct if ct else 0):7.3f} {money(m['cost']):>9s} {pnl:>10s}")
    bot = [m for m in s["markets"] if m["owner"] == "bot"]
    if bot:
        cities = defaultdict(list)
        for m in bot:
            cities[m["city"]].append(m)
        out += ["", "This bot by city:"]
        for city, ms in sorted(cities.items()):
            known = [m for m in ms if m["pnl"] is not None]
            out.append(f"{city:15s} {len(ms)} market(s), {sum(m['yes_ct'] + m['no_ct'] for m in ms):,.1f} contracts, "
                       f"cost {money(sum(m['cost'] for m in ms))}, P&L {money(sum(m['pnl'] for m in known), sign=True)}, "
                       f"{sum(1 for m in known if m['pnl'] > 0)}W/{sum(1 for m in known if m['pnl'] <= 0)}L"
                       + (" (est.)" if any(m["status"] != "settled" for m in ms) else ""))
    c = s["check"]
    if c["markets"]:
        out += ["", f"Account check ({c['markets']} settled markets, every owner): the fills sum to "
                    f"{money(c['fills_sum'], sign=True)}; Kalshi's settlement records say "
                    f"{money(c['settlement_records'], sign=True)}. The gap is the $1 paid on "
                    f"{c['netted_pairs']:,.1f} YES/NO pairs that netted out before settlement."]
        if not c["ok"]:
            out.append("WARNING: the gap is not the netted pairs alone; fills may be missing from KXHIGH_fills_clean.")
    return "\n".join(out)


# ---------------------------------------------------------------- I/O

def bq_rows(sql: str, day: dt.date):
    os.environ.setdefault("CLOUDSDK_PYTHON", r"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe")
    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", day)])
    return [dict(r.items()) for r in client.query(sql, job_config=cfg, location=LOCATION).result()]


def fetch(day: dt.date):
    ds = f"`{PROJECT}.{DATASET}"
    fills = bq_rows(f"""
        SELECT fill_id, client_order_id, market_ticker, side, action, filled_count,
               raw_yes_price_dollars, raw_no_price_dollars, fee_cost_dollars, fill_ts
        FROM {ds}.KXHIGH_fills_clean` WHERE event_date = @d""", day)
    settlements = bq_rows(f"""
        SELECT market_ticker, result, revenue_dollars, yes_total_cost_dollars,
               no_total_cost_dollars, fee_cost_dollars
        FROM {ds}.KXHIGH_settlements_clean` WHERE event_date = @d""", day)
    actuals = {r["city_abv"]: (r["city"], r["high_temp_f"]) for r in bq_rows(f"""
        SELECT city_abv, city, high_temp_f FROM {ds}.KXHIGH_cli_readings` WHERE event_date = @d
        QUALIFY ROW_NUMBER() OVER (PARTITION BY city_abv ORDER BY fetched_at DESC) = 1""", day)}
    # Kalshi's own market record: the tails' direction, and the result of a
    # market that is final but not yet in BigQuery.
    settled = {s["market_ticker"] for s in settlements if str(s.get("result") or "").lower() in ("yes", "no")}
    meta = {}
    for t in sorted({f["market_ticker"] for f in fills}):
        if parse_ticker(t)[1] == "B" and t in settled:
            continue
        try:
            with urllib.request.urlopen(KALSHI_MARKET_URL + t, timeout=15) as r:
                m = json.load(r)["market"]
            meta[t] = {"strike_type": m.get("strike_type") or "", "result": m.get("result") or ""}
        except Exception:
            pass
    return fills, settlements, actuals, meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--date", required=True, help="D, the markets' day (YYYY-MM-DD)")
    ap.add_argument("--json", action="store_true", help="print the numbers as JSON")
    ap.add_argument("--summary-line", action="store_true", help="print only the DIGEST_SUMMARY line")
    ap.add_argument("--imm-log-dir", default=IMM_FILL_LOG_DIR)
    a = ap.parse_args(argv)
    day = dt.date.fromisoformat(a.date)
    fills, settlements, actuals, meta = fetch(day)
    imm_ids, imm_seen = load_imm_fill_ids(day, a.imm_log_dir)
    s = build(day, fills, settlements, actuals, meta, imm_ids, imm_seen)
    if a.summary_line:
        print(f"DIGEST_SUMMARY: {s['summary_line']}")
    elif a.json:
        print(json.dumps(s, indent=1, default=str))
    else:
        print(render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
