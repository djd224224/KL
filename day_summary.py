"""Compute per-day summary from Settlement CSV.

Uses TRUE net P&L = gross_payout - cost, NOT the misleading `Profit_In_Dollars`
column (which is gross payout only).

TARGET_DAY is the EVENT date (YYYY-MM-DD). For Kalshi tickers containing an
embedded event date like "26APR17", that date is used directly — so a Friday-
night NBA game that settles at 1 AM Saturday ET is still grouped with Friday.
For tickers with no embedded date (long-running markets like Oscars), falls
back to the ET date of settled_time."""
import csv, re, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

CSV_PATH = sys.argv[1]
TARGET_DAY = sys.argv[2]  # "YYYY-MM-DD"
ET = ZoneInfo("America/New_York")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
# Matches a YYMMMDD embedded in a ticker segment (e.g. "26APR17").
_TICKER_DATE_RE = re.compile(r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})")


def family(ticker):
    # Sports (group spreads/games/mentions/props under one family each)
    if ticker.startswith("KXNBA"): return "NBA"
    if ticker.startswith("KXNCAAB"): return "NCAAB"
    if ticker.startswith("KXMLB"): return "MLB"
    if ticker.startswith("KXNFL"): return "NFL"
    if ticker.startswith("KXFIGHT"): return "Fight"
    if ticker.startswith("KXUFC"): return "UFC"
    # Weather
    if ticker.startswith("KXHIGH"): return "High Temp Weather"
    # Entertainment
    if ticker.startswith("KXOSCAR"): return "Oscars"
    if ticker.startswith("KXPERFORM"): return "Entertainment"
    # Politics
    if ticker.startswith(("KXTRUMP", "KXPRES", "KXFED")): return "Politics"
    # Generic mentions bucket
    if ticker.startswith("KXMENTION"): return "Mentions"
    return "Other"


def ts_et_date(ts):
    """Convert an ISO-UTC timestamp to its YYYY-MM-DD in America/New_York."""
    if not ts:
        return ""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
    return dt.strftime("%Y-%m-%d")


def event_date(ticker, settled_ts):
    """Resolve which trading day a market belongs to.
    Primary: the YYMMMDD embedded in the ticker (e.g. KXNBAGAME-26APR17…).
    Fallback: ET date of settled_ts (for tickers without an embedded date)."""
    m = _TICKER_DATE_RE.search(ticker)
    if m:
        yy, mon, dd = m.groups()
        try:
            return f"20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}"
        except (KeyError, ValueError):
            pass
    return ts_et_date(settled_ts)


rows = []
with open(CSV_PATH, encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        ts = r["Original_Date"]
        if event_date(r["Market_Ticker"], ts) != TARGET_DAY:
            continue
        yc = int(float(r["Yes_Contracts_Owned"] or 0))
        nc = int(float(r["No_Contracts_Owned"] or 0))
        if yc == 0 and nc == 0:
            continue
        yp = float(r["Yes_Contracts_Average_Price_In_Cents"] or 0)
        np_ = float(r["No_Contracts_Average_Price_In_Cents"] or 0)
        result = r["Result"]
        cost = yc * yp + nc * np_
        if result == "yes":
            gross = yc * 1.0
        elif result == "no":
            gross = nc * 1.0
        else:
            gross = 0.0
        net = gross - cost
        rows.append({
            "ticker": r["Market_Ticker"],
            "family": family(r["Market_Ticker"]),
            "yc": yc, "nc": nc, "yp": yp, "np": np_,
            "result": result, "cost": cost, "gross": gross, "net": net,
            "time": ts,
        })


if not rows:
    print(f"No active settlements on {TARGET_DAY}")
    sys.exit(0)

total_net = sum(r["net"] for r in rows)
total_cost = sum(r["cost"] for r in rows)
total_gross = sum(r["gross"] for r in rows)
wins = [r for r in rows if r["net"] > 0]
losses = [r for r in rows if r["net"] < 0]
scratches = [r for r in rows if r["net"] == 0]

print(f"=== {TARGET_DAY} Settlement Summary ===")
print(f"Markets settled (with active position): {len(rows)}")
print(f"Gross payout: ${total_gross:,.2f}")
print(f"Cost basis:   ${total_cost:,.2f}")
print(f"Net P&L:      ${total_net:+,.2f}")
print(f"ROI:          {(total_net/total_cost*100) if total_cost else 0:+.2f}%")
print(f"Win rate:     {len(wins)}/{len(rows)} = {len(wins)/len(rows)*100:.1f}%  "
      f"(W:{len(wins)} L:{len(losses)} scratch:{len(scratches)})")
print()

# By family
fam = defaultdict(lambda: {"n": 0, "net": 0.0, "cost": 0.0, "w": 0})
for r in rows:
    f_ = r["family"]
    fam[f_]["n"] += 1
    fam[f_]["net"] += r["net"]
    fam[f_]["cost"] += r["cost"]
    if r["net"] > 0: fam[f_]["w"] += 1
print("By family:")
for f_, d in sorted(fam.items(), key=lambda x: -x[1]["net"]):
    roi = (d["net"]/d["cost"]*100) if d["cost"] else 0
    wr = (d["w"]/d["n"]*100) if d["n"] else 0
    print(f"  {f_:25} n={d['n']:3}  ${d['net']:+9,.2f}  ROI {roi:+6.1f}%  win {wr:.0f}%")
print()

# Top 5 winners / losers
rows_sorted = sorted(rows, key=lambda r: r["net"])
print("Biggest wins:")
for r in rows_sorted[-5:][::-1]:
    side = f"{r['yc']}Y@${r['yp']:.2f}" if r['yc'] else f"{r['nc']}N@${r['np']:.2f}"
    print(f"  {r['net']:+8.2f}  {side:18}  result={r['result']}  {r['ticker']}")
print()
print("Biggest losses:")
for r in rows_sorted[:5]:
    side = f"{r['yc']}Y@${r['yp']:.2f}" if r['yc'] else f"{r['nc']}N@${r['np']:.2f}"
    print(f"  {r['net']:+8.2f}  {side:18}  result={r['result']}  {r['ticker']}")
