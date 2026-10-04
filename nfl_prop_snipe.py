#!/usr/bin/env python3
"""Rank NFL player props whose book is out of line with the player's history
-- candidates to TAKE by hand. Read-only: it never places an order.

Jack 2026-10-04: "how to snipe markets myself that are out of line with
historicals?" The fair and band are nfl_prop_fair's (the IMM gate's model:
out of sample its error equals the pre-game book's, so a small gap is noise
and only a book OUTSIDE the band is a candidate).

Backtest on the settled props 9/24-10/1 (the bot's logged pre-game books,
first signal per market, Kalshi taker fee 0.07 x P x (1-P) deducted):
  sell YES when the bid > band top + 1c   14 markets, won 13, +3.9c/contract
  sell YES when the bid > band top + 3c    2 markets, +9.0c and +27.0c
  buy YES when the ask < band bottom       never happened
  selling every escalator at its bid      +0.45c/contract (escalators sit
                                          ~1.4x the model, 73% won)
Small sample; the losing kind is the move the market makes on NEWS the
model does not have (Keenan Allen out -> Downs' ladder 24 -> 34), so every
candidate carries its flags:
  INJ      the player has an ESPN designation (a fantasy ladder pays $0 if
           he sits; the other props resolve at Kalshi's "last fair price")
  TEAM     a QB / WR / TE / RB teammate was tagged Out / Doubtful /
           Questionable in the last 3 days -- the book may be right
  SIBLING  the player's other contract on the same stat (ladder vs
           escalator) implies a mean close to THIS book's, i.e. the market
           as a whole moved, not one stale book
A clean candidate has no flag: one book alone, far outside the band, no
news. Robinson's receiving-yards ladder at 18 / 20 on 10/3 (fair 9.3, band
6.5-13.9) while his receiving-yards escalator sat at 3.24 (= the model) was
that shape.

Before hitting a bid: the IMM stands aside (no orders) on a book over 3c
outside the band, but between band top + 1c and + 3c it rests a capped bid
of its own -- trading against your own other account can be a wash trade,
so take only what sits ABOVE the IMM's cap (printed as `imm cap`).

    python nfl_prop_snipe.py                 # candidates, best first
    python nfl_prop_snipe.py --all           # every open prop, with flags
    python nfl_prop_snipe.py --min-edge 2    # band-edge margin in cents
"""

import argparse
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import nfl_prop_fair as nf

TEAM_NEWS_DAYS = 3.0
NEWS_STATUSES = ("out", "doubtful", "questionable")
SKILL = ("QB", "WR", "TE", "RB", "FB")
IMM_BAND_TOL = 3.0        # the gate stands aside past this (NFL_BAND_TOL_CENTS)
IMM_CAP_TOL = 1.0         # ... and caps its bid at band top + this


def taker_fee_cents(p_cents: float) -> float:
    """Kalshi's general taker fee per contract, 0.07 x P x (1 - P)."""
    p = p_cents / 100.0
    return 7.0 * p * (1.0 - p)


def implied_mu(spec: dict, price_cents: float, hi: float) -> Optional[float]:
    """The mean at which the model's fair equals price_cents (bisection)."""
    target = price_cents / 100.0
    lo_mu, hi_mu = 0.0, max(hi, 1.0)
    while nf.expected_payout(spec, hi_mu) < target and hi_mu < 5000:
        hi_mu *= 2.0
    if nf.expected_payout(spec, hi_mu) < target:
        return None
    for _ in range(40):
        mid = (lo_mu + hi_mu) / 2.0
        if nf.expected_payout(spec, mid) < target:
            lo_mu = mid
        else:
            hi_mu = mid
    return (lo_mu + hi_mu) / 2.0


def _px(m: dict, key: str) -> Optional[float]:
    try:
        v = float(m.get(key))
    except (TypeError, ValueError):
        return None
    return v * 100.0 if 0.0 < v < 1.0 else None


def _sz(m: dict, key: str) -> float:
    try:
        return float(m.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def team_news(watch: nf.NflPropWatch, now: datetime) -> Dict[str, List[str]]:
    """ESPN team -> ["Keenan Allen WR Out", ...] tagged in TEAM_NEWS_DAYS."""
    out: Dict[str, List[str]] = {}
    since = now - timedelta(days=TEAM_NEWS_DAYS)
    for team in sorted(watch.rosters):
        try:
            js = watch.session.get(nf.ESPN_ROSTER.format(team=team),
                                   timeout=nf.HTTP_TIMEOUT).json()
        except Exception:                                # noqa: BLE001
            continue
        for grp in js.get("athletes") or []:
            for a in grp.get("items") or []:
                pos = ((a.get("position") or {}).get("abbreviation") or "")
                if pos not in SKILL:
                    continue
                for inj in a.get("injuries") or []:
                    st = str(inj.get("status") or "")
                    try:
                        when = datetime.strptime(str(inj.get("date"))[:16],
                                                 "%Y-%m-%dT%H:%M").replace(
                                                     tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if st.lower() in NEWS_STATUSES and when >= since:
                        out.setdefault(team, []).append(
                            f"{a.get('fullName')} {pos} {st}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-edge", type=float, default=1.0,
                    help="cents past the band edge to list (default 1)")
    ap.add_argument("--all", action="store_true", help="every open prop")
    args = ap.parse_args(argv)
    w = nf.NflPropWatch()
    snap = w.refresh()
    now = datetime.now(timezone.utc)
    news = team_news(w, now)
    fam = {m["ticker"]: m for m in w.family}
    # implied means per (player, stat) for the sibling check
    implied: Dict[Tuple[str, str], Dict[str, float]] = {}
    rows = []
    for t, e in sorted(snap["markets"].items()):
        m = fam.get(t)
        if m is None or e.get("fair") is None:
            continue
        spec = nf.payout_spec(m, e["stat"], e["kind"])
        b, a = _px(m, "yes_bid_dollars"), _px(m, "yes_ask_dollars")
        mid = (b + a) / 2.0 if b is not None and a is not None else None
        imu = implied_mu(spec, mid, e["mu"] * 3) if mid is not None else None
        if imu is not None:
            implied.setdefault((e["player"], e["stat"]), {})[t] = imu
        rows.append((t, e, m, spec, b, a, imu))
    out = []
    for t, e, m, spec, b, a, imu in rows:
        f, lo, hi = e["fair"] * 100, e["fair_lo"] * 100, e["fair_hi"] * 100
        nt = nf.KALSHI_TO_NFLV.get(e.get("team") or "", e.get("team") or "")
        et = nf.NFLV_TO_ESPN.get(nt, nt.lower())
        flags = []
        if e.get("injury"):
            flags.append(f"INJ {e['injury']}")
        mates = [n for n in news.get(et, []) if not n.startswith(e["player"])]
        if mates:
            flags.append("TEAM " + "; ".join(mates[:3]))
        sib = {k: v for k, v in implied.get((e["player"], e["stat"]), {}).items()
               if k != t}
        side = None
        if b is not None and b > hi + args.min_edge:
            side, px, size = "SELL YES", b, _sz(m, "yes_bid_size_fp")
            edge, edge_band = b - f, b - hi
        elif a is not None and a < lo - args.min_edge:
            side, px, size = "BUY YES", a, _sz(m, "yes_ask_size_fp")
            edge, edge_band = f - a, lo - a
        if sib and imu is not None and side:
            s_mu = sorted(sib.values())[len(sib) // 2]
            # the sibling agrees with THIS book (within 15%) -> market moved
            if abs(s_mu - imu) <= 0.15 * imu:
                flags.append(f"SIBLING implied mu {s_mu:.1f}")
        if side is None and not args.all:
            continue
        if side is None:
            out.append((0.0, t, e, "-", b, a, 0, f, lo, hi, 0, 0, imu, flags, None))
            continue
        fee = taker_fee_cents(px)
        imm_cap = math.floor(hi + IMM_CAP_TOL) if b is not None and \
            b <= hi + IMM_BAND_TOL else None
        out.append((edge_band - fee, t, e, side, b, a, size, f, lo, hi, edge,
                    edge_band - fee, imu, flags, imm_cap))
    out.sort(key=lambda r: (bool(r[13]), -r[0]))
    print(f"{'side':8s} {'ticker':54s} {'player':20s} {'book':>12s} {'size':>7s} "
          f"{'fair':>6s} {'band':>12s} {'edge':>6s} {'net':>6s} {'mu':>6s} "
          f"{'mkt mu':>6s}  flags")
    for (_k, t, e, side, b, a, size, f, lo, hi, edge, net, imu, flags,
         cap) in out:
        bk = f"{b:.2f}/{a:.2f}" if b is not None and a is not None else f"{b}/{a}"
        fl = " | ".join(flags) if flags else "clean"
        if cap is not None:
            fl += f" | imm cap {cap}c"
        print(f"{side:8s} {t:54s} {e['player'][:20]:20s} {bk:>12s} {size:7.0f} "
              f"{f:6.2f} {lo:5.2f}-{hi:5.2f} {edge:6.2f} {net:6.2f} "
              f"{e['mu']:6.1f} {(imu if imu is not None else float('nan')):6.1f}  {fl}")
    print(f"\n{sum(1 for r in out if r[3] != '-')} candidates; edge = vs the fair, "
          f"net = past the band edge after the taker fee; mkt mu = the mean "
          f"the book implies. Generated {now.strftime('%Y-%m-%d %H:%MZ')}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
