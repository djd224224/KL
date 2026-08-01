#!/usr/bin/env python3
"""
imm_shape_sim_v2.py — ladder-shape decision under the AMENDED liquidity
program rules (effective 2026-07-30: reference = level where cumulative
depth reaches target/5, full weight at/above reference, both-sides-to-target
snapshot exclusion).

Replaces the 2026-07-25 shape sim, whose conclusion (all-at-touch 0:10)
was derived from the old touch-referenced scoring and is obsolete.

For every market with an active liquidity program (top N by pool $/day),
fetches the live book and evaluates candidate per-side ladder shapes:
  - fixed offsets from the touch (join, 1 behind, 2 behind, spread)
  - adaptive "at-ref": rest exactly at the book's reference level — the
    deepest price that still scores full weight. Placing AT the external
    reference cannot move the reference (levels above are unchanged), so
    the placement is stable and maximally fill-protected.
Every shape is evaluated WITH the bot's standard 1c/99c depth pads (they
qualify thin sides and, post-7/30, un-exclude the snapshot itself).

Metrics per shape, aggregated across books:
  est $/day     Σ frac × pool$/day (0 when either side fails target)
  $/contract    est $ per non-pad contract per day
  counted       books where the snapshot counts (both sides >= target)
  touch-load    Σ contracts × 0.5^(ticks from touch)  — fill-risk proxy
  collateral    Σ worst-case $ of the ladder (bid px + 100-ask px)

Read-only: fetches programs + orderbooks, places nothing.

Usage:
    python imm_shape_sim_v2.py [--books 150] [--csv out.csv]
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import incentive_mm as imm


# reference walk + level normalization shared with the bot (incentive_mm)
side_reference = imm.side_reference_level
merged_desc = imm.levels_desc


# Candidate shapes: list of (ticks_behind_touch, contracts) per side, or the
# adaptive sentinel ("ATREF", contracts).
SHAPES: List[Tuple[str, object]] = [
    ("live 0:10 all-touch", [(0, 10)]),
    ("1-behind x10",        [(1, 10)]),
    ("2-behind x10",        [(2, 10)]),
    ("spread 4/3/3",        [(0, 4), (1, 3), (2, 3)]),
    ("at-ref x10",          ("ATREF", 10)),
    ("all-touch x30",       [(0, 30)]),
    ("2-behind x30",        [(2, 30)]),
    ("at-ref x30",          ("ATREF", 30)),
    ("all-touch x100",      [(0, 100)]),
    ("at-ref x100",         ("ATREF", 100)),
]


def build_overlay(shape, book_side: str, anchor: int, opp_best: Optional[int],
                  yes_levels, no_levels, target: float
                  ) -> Tuple[List[Tuple[str, int, float]], List[Tuple[int, int]]]:
    """(overlay orders, [(ticks_behind, count)]) for one side of one shape.
    anchor = external best on that side in YES-book cents (bid side) or
    NO-price cents (ask side, i.e. 100 - yes ask). Prices clamp to [1, 99]
    and never cross the opposite best."""
    rungs: List[Tuple[int, int]] = []
    if isinstance(shape, tuple) and shape[0] == "ATREF":
        n = shape[1]
        ext = merged_desc(yes_levels if book_side == "bid" else no_levels)
        ref = side_reference(ext, target)
        if ref is None:
            rungs = [(0, n)]          # book too thin for a reference: join touch
        else:
            rungs = [(max(anchor - ref, 0), n)]
    else:
        rungs = list(shape)
    out = []
    ticks_counts = []
    for ticks, n in rungs:
        px = anchor - ticks
        if px < 1:
            continue
        if book_side == "bid":
            # yes bid at px; never cross the yes ask
            if opp_best is not None and px >= opp_best:
                px = opp_best - 1
                if px < 1:
                    continue
            out.append(("bid", px, float(n)))
            ticks_counts.append((anchor - px, n))
        else:
            # NO bid at no-price px  == yes ask at 100-px; never cross
            if opp_best is not None and px >= opp_best:
                px = opp_best - 1
                if px < 1:
                    continue
            out.append(("ask", 100 - px, float(n)))
            ticks_counts.append((anchor - px, n))
    return out, ticks_counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--books", type=int, default=150)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args(argv)

    client = imm.build_client()
    now = datetime.now(timezone.utc)

    # active liquidity programs, aggregated per market (same as the bot)
    programs: List[dict] = []
    cursor = None
    for _ in range(20):
        params = {"limit": 1000, "status": "active"}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/incentive_programs", params=params)
        batch = resp.get("incentive_programs") or []
        programs.extend(batch)
        cursor = resp.get("next_cursor")
        if not cursor or not batch:
            break
    by_market: Dict[str, dict] = {}
    for p in programs:
        if p.get("incentive_type") != "liquidity" or p.get("paid_out"):
            continue
        start = imm.parse_iso_utc(p.get("start_date", ""))
        end = imm.parse_iso_utc(p.get("end_date", ""))
        if not start or not end or not (start <= now < end):
            continue
        days = max((end - start).total_seconds() / 86400.0, 1.0 / 24)
        t = p.get("market_ticker") or ""
        cur = by_market.setdefault(t, {"dpd": 0.0, "target": 0.0, "df": 0.5})
        cur["dpd"] += (p.get("period_reward") or 0) / 10000.0 / days
        cur["target"] = max(cur["target"],
                            float(p.get("target_size_fp") or 0))
        cur["df"] = (p.get("discount_factor_bps") or 5000) / 10000.0

    ranked = sorted(by_market.items(), key=lambda kv: -kv[1]["dpd"])
    sample = [(t, info) for t, info in ranked
              if not imm.IncentiveMarketMaker._blocked(t)][:args.books]
    print(f"programs on {len(by_market)} markets; sampling top {len(sample)} "
          f"by pool $/day (total pool ${sum(i['dpd'] for _, i in sample):.0f}/day)")

    agg = {name: {"dollars": 0.0, "contracts": 0.0, "counted": 0,
                  "touch_load": 0.0, "collateral": 0.0, "books": 0,
                  "ticks_sum": 0.0, "ticks_n": 0}
           for name, _ in SHAPES}
    rows = []
    read_fail = 0
    for i, (t, info) in enumerate(sample):
        try:
            ob = client.get_orderbook(ticker=t)
        except Exception:
            read_fail += 1
            continue
        yes_levels, no_levels = imm.orderbook_levels(ob)
        ext_bid, ext_ask = imm.external_best(yes_levels, no_levels)
        if ext_bid is None or ext_ask is None:
            continue
        no_anchor = 100 - ext_ask        # best NO bid in no-price cents
        target, df, dpd = info["target"], info["df"], info["dpd"]
        for name, shape in SHAPES:
            ov_bid, tk_bid = build_overlay(shape, "bid", ext_bid, ext_ask,
                                           yes_levels, no_levels, target)
            ov_ask, tk_ask = build_overlay(shape, "ask", no_anchor, 100 - ext_bid,
                                           yes_levels, no_levels, target)
            overlay = ov_bid + ov_ask
            n_real = sum(n for _s, _p, n in overlay)
            # standard pads on both sides (mirror the candidate estimator)
            if target > 0:
                nb = imm.pad_quantity(
                    sum(q for _p, q in yes_levels) +
                    sum(n for s, _p, n in overlay if s == "bid"), target)
                if nb > 0:
                    overlay.append(("bid", imm.PAD_BID_CENTS, float(nb)))
                na = imm.pad_quantity(
                    sum(q for _p, q in no_levels) +
                    sum(n for s, _p, n in overlay if s == "ask"), target)
                if na > 0:
                    overlay.append(("ask", imm.PAD_ASK_CENTS, float(na)))
            frac, sides = imm.estimate_reward_share(
                yes_levels, no_levels, overlay, target, df, own_in_book=False)
            a = agg[name]
            a["books"] += 1
            a["dollars"] += frac * dpd
            a["contracts"] += n_real
            a["counted"] += 1 if sides == 2 else 0
            for ticks, n in tk_bid + tk_ask:
                a["touch_load"] += n * (0.5 ** ticks)
                a["ticks_sum"] += ticks * n
                a["ticks_n"] += n
            a["collateral"] += sum(
                (p if s == "bid" else 100 - p) / 100.0 * n
                for s, p, n in overlay[:len(ov_bid) + len(ov_ask)])
            rows.append({"ticker": t, "shape": name, "frac": frac,
                         "sides": sides, "est_dollars": frac * dpd})
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(sample)} books scored...")

    print(f"\n(read failures: {read_fail})")
    print(f"\n{'shape':22s} {'est $/day':>10s} {'$/contract':>10s} "
          f"{'counted':>8s} {'touch-load':>10s} {'avg-ticks':>9s} {'collat $':>9s}")
    for name, _ in SHAPES:
        a = agg[name]
        ypc = a["dollars"] / a["contracts"] if a["contracts"] else 0.0
        avg_ticks = a["ticks_sum"] / a["ticks_n"] if a["ticks_n"] else 0.0
        print(f"{name:22s} {a['dollars']:>10.2f} {ypc:>10.4f} "
              f"{a['counted']:>4d}/{a['books']:<3d} {a['touch_load']:>10.0f} "
              f"{avg_ticks:>9.2f} {a['collateral']:>9.0f}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nper-book rows -> {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
