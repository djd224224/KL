#!/usr/bin/env python3
"""One-shot deploy probe for the live-event depth gate (PR #9, merged
2026-08-31): is the TRADING BOX's bot running the new code yet?

Runs in GitHub Actions (Kalshi reachable there; Claude's cloud container
cannot reach kalshi.com), same account key as the box — so resting imm-
orders ARE the live bot's output, and order TTL <= 30 min means any resting
imm- order reflects the quote loop within the last half hour.

The behavioral signature (gated series = imm.EVENT_DEPTH_GATE_PREFIXES,
i.e. KXTRUMPMENTION*/KXMAMDANIMENTION*):
  OLD code: pads gated markets' thin sides to target with 1c/99c orders,
            and quotes markets whose in-band book has a sub-1k side.
  NEW code: NEVER rests at 1c/99c on gated series (legacy pads are
            cancelled on its first cycle), and a sub-1k side on any in-band
            gated market stands down the WHOLE event — zero imm- orders on
            all of its markets.

Sections:
  1. Gated universe (public): open markets + active programs per series.
  2. Account signature (counts only — repo and Action logs are PUBLIC:
     counts, tickers and booleans; no sizes, balances or positions).
  3. Dry run_cycle() of the checked-out (merged) code under the live
     launcher's $ProbeEnv against live Kalshi: shows the gate acting on
     real books (event_depth_halt contents, zero sim pads on gated series).
     Proves the CODE; section 2 proves what the BOX runs.
  4. Verdict.
"""

import os
import re
from datetime import datetime, timezone

import requests


def _apply_probe_env() -> None:
    """Live launcher's $ProbeEnv, applied BEFORE incentive_mm import (same
    trick as kxtruev_diag.py / imm_quote_gaps.py — keep the three in sync).
    Workflow-provided env wins via setdefault."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "run_incentive_mm.ps1"),
                  encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return
    m = re.search(r'\$ProbeEnv\s*=\s*"(set [^"\n]+)"', txt)
    if not m:
        return
    for k, v in re.findall(r"set ([A-Z_0-9]+)=([^&]*)&&", m.group(1) + "&&"):
        os.environ.setdefault(k, v)


_apply_probe_env()

import incentive_mm as imm  # noqa: E402  (env parity must precede import)

BASE = imm.KALSHI_API_BASE
GATED = tuple(imm.EVENT_DEPTH_GATE_PREFIXES)


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def public_get(path: str, **params):
    r = requests.get(BASE + path, params=params or None, timeout=20)
    r.raise_for_status()
    return r.json()


def is_gated(ticker: str) -> bool:
    return str(ticker).startswith(GATED)


def order_is_pad_priced(o: dict) -> bool:
    parsed = imm.order_yes_book_cents(o)
    if parsed is None:
        return False
    side, yes_px = parsed
    return (side == "bid" and yes_px <= imm.PAD_BID_CENTS) or \
        (side == "ask" and yes_px >= imm.PAD_ASK_CENTS)


def main() -> None:
    now = datetime.now(timezone.utc)
    print(f"UTC now: {now:%Y-%m-%d %H:%M:%S} (ET {now.astimezone(imm.ET):%H:%M})")
    print(f"gated prefixes: {','.join(GATED)}; pads on gated: "
          f"{any(imm.series_pad_to_target(s) for s in GATED)} "
          f"(False = gate code checked out); depth min "
          f"{imm.EVENT_DEPTH_MIN_CONTRACTS:g}")

    section("1. GATED UNIVERSE (public: open markets + programs)")
    open_gated = []
    for prefix in GATED:
        markets, cursor = [], None
        while True:
            params = {"series_ticker": prefix, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = public_get("/markets", **params)
            except Exception as e:
                print(f"  {prefix}: /markets sweep failed: {e}")
                break
            batch = resp.get("markets") or []
            markets.extend(batch)
            cursor = resp.get("cursor")
            if not cursor or not batch or len(markets) > 2000:
                break
        opens = [m for m in markets if m.get("status") in ("active", "open")]
        open_gated.extend(opens)
        print(f"  {prefix}: {len(markets)} markets, OPEN now: {len(opens)}")
    by_event = {}
    for m in open_gated:
        by_event.setdefault(m.get("event_ticker", "?"), []).append(m)
    for ev, ms in sorted(by_event.items()):
        print(f"  event {ev}: {len(ms)} open markets")

    client = imm.build_client()
    bot = imm.IncentiveMarketMaker(client=client, live=False)
    programs = bot.fetch_programs()
    gated_prog = sorted(t for t in programs if is_gated(t))
    print(f"active liquidity programs: {len(programs)} markets; "
          f"on gated series: {len(gated_prog)}")

    section("2. ACCOUNT SIGNATURE (counts only)")
    resting, cursor = [], None
    while True:
        resp = client.get_orders(status="resting", limit=200, cursor=cursor)
        batch = resp.get("orders") or []
        resting.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch or len(resting) > 5000:
            break
    imm_orders = [o for o in resting
                  if str(o.get("client_order_id", "")).startswith("imm-")]
    gated_orders = [o for o in imm_orders if is_gated(o.get("ticker", ""))]
    gated_pads = [o for o in gated_orders if order_is_pad_priced(o)]
    created = sorted(str(o.get("created_time") or "") for o in imm_orders
                     if o.get("created_time"))
    print(f"resting orders: {len(resting)}; imm- bot orders: {len(imm_orders)} "
          f"(created {created[0] if created else 'n/a'} .. "
          f"{created[-1] if created else 'n/a'}; TTL {imm.ORDER_TTL_SECS}s "
          f"bounds max age)")
    print(f"imm- on gated series: {len(gated_orders)}; at PAD prices "
          f"(1c bid / 99c ask): {len(gated_pads)}")
    per_mkt = {}
    for o in gated_orders:
        per_mkt[o.get("ticker", "?")] = per_mkt.get(o.get("ticker", "?"), 0) + 1

    def event_of(ticker: str) -> str:
        return str(ticker).rsplit("-", 1)[0]

    # Book sweep over EVERY programmed gated market — quoted or not. The
    # old/new discriminator lives on markets with an in-band side under the
    # 1k bar once OUR orders are netted out: old code quotes (and pads)
    # exactly there; new code stands the whole EVENT down. On books >= 1k
    # both sides, old and new code are behaviorally IDENTICAL (old code
    # only pads a side that is short), so deep-everywhere runs are
    # inconclusive by behavior — the verdict says so instead of guessing.
    ev_orders, ev_prog, ev_thin = {}, {}, {}
    for t in per_mkt:
        ev_orders[event_of(t)] = ev_orders.get(event_of(t), 0) + per_mkt[t]
    for t in sorted(gated_prog):
        ev = event_of(t)
        ev_prog[ev] = ev_prog.get(ev, 0) + 1
        try:
            yl, nl = imm.orderbook_levels(client.get_orderbook(ticker=t))
        except Exception as e:
            print(f"  {t}: orderbook read failed: {e}")
            continue
        own = [imm.order_yes_book_cents(o) + (imm.order_remaining(o),)
               for o in gated_orders if o.get("ticker") == t
               and imm.order_yes_book_cents(o) is not None]
        d_yes, d_no = imm.external_depths(yl, nl, own)
        ext_b, ext_a = imm.external_best(yl, nl, own)
        in_band = imm.pad_band_ok(imm.series_of(t), ext_b, ext_a)
        thin = in_band and (d_yes < imm.EVENT_DEPTH_MIN_CONTRACTS
                            or d_no < imm.EVENT_DEPTH_MIN_CONTRACTS)
        if thin:
            ev_thin[ev] = ev_thin.get(ev, 0) + 1
        print(f"  {t}: {per_mkt.get(t, 0)} imm- orders; ext depth "
              f"yes {d_yes:.0f} / no {d_no:.0f}; in-band {in_band}"
              f"{'; SUB-1K SIDE' if thin else ''}")
    print("per-event: programmed / imm- orders / in-band sub-1k markets")
    for ev in sorted(set(ev_prog) | set(ev_orders)):
        print(f"  {ev}: {ev_prog.get(ev, 0)} / {ev_orders.get(ev, 0)} / "
              f"{ev_thin.get(ev, 0)}")
    old_proof = sorted(ev for ev, n in ev_thin.items()
                       if n and ev_orders.get(ev, 0))
    halt_sig = sorted(ev for ev, n in ev_thin.items()
                      if n and not ev_orders.get(ev, 0))

    section("3. DRY-RUN CYCLE of the CHECKED-OUT code (read-only)")
    # Caveat: this fresh dry instance sees the LIVE bot's resting orders as
    # foreign, so its yield-to-human standoff skips exactly the markets the
    # live bot is quoting ('manual' skips below are expected, not a bug) —
    # gated markets may therefore be absent from this section whenever the
    # live bot is on them. Section 2 is the box verdict; this section only
    # demonstrates the checked-out code refuses pads/thin events itself.
    bot.run_cycle()
    sims = bot.state.sim_orders or {}
    sim_iter = list(sims.values()) if isinstance(sims, dict) else list(sims)
    sim_gated = [o for o in sim_iter if is_gated(o.get("ticker", ""))]
    sim_gated_pads = [o for o in sim_gated
                      if o.get("yes_price") in (imm.PAD_BID_CENTS,
                                                imm.PAD_ASK_CENTS)]
    print(f"selected: {len(bot.state.selected or {})} markets "
          f"({sum(1 for t in (bot.state.selected or {}) if is_gated(t))} gated); "
          f"sim orders: {len(sim_iter)} ({len(sim_gated)} gated, "
          f"{len(sim_gated_pads)} gated at pad prices — MUST be 0)")
    if bot.state.event_depth_halt:
        for e, ts in sorted(bot.state.event_depth_halt.items()):
            print(f"  event_depth_halt: {e}")
    else:
        print("  event_depth_halt: (none this cycle)")
    for e in sorted(bot.state.event_live_halt or {}):
        print(f"  event_LIVE_halt (permanent): {e}")
    for cat, msg in bot.alerter.today:
        if cat in ("event_depth", "event_live"):
            print(f"  alert [{cat}] {msg}")

    section("4. VERDICT — what code is the BOX running?")
    if not imm_orders:
        print("NO resting imm- orders: bot looks DOWN or between waves "
              "(possibly mid-restart on the deploy) — re-probe in ~15 min.")
    elif gated_pads:
        print(f"OLD code: {len(gated_pads)} pad-priced imm- order(s) still "
              f"resting on gated series. New code never pads these and "
              f"cancels legacy pads on its first cycle. The box has not "
              f"restarted onto the merge yet.")
    elif old_proof:
        print(f"OLD code: event(s) {', '.join(old_proof)} carry imm- orders "
              f"while holding an in-band sub-1k market — new code stands "
              f"the whole event down there.")
    elif halt_sig:
        print(f"NEW code signature: event(s) {', '.join(halt_sig)} hold an "
              f"in-band sub-1k market and carry ZERO imm- orders while the "
              f"bot rests elsewhere — that is the event-wide halt acting "
              f"(old code would have quoted and padded there, unless "
              f"unselected for unrelated floors).")
    elif not gated_prog and not open_gated:
        print("Bot ALIVE, but no gated markets are open/programmed right "
              "now — nothing for the gate to act on, so box code version is "
              "UNOBSERVABLE from behavior. The dry run above proves the "
              "merged code itself; re-probe when a gated event lists.")
    elif gated_orders:
        print("INCONCLUSIVE by behavior (leaning new): bot alive, quoting "
              "gated events, zero pads — but every gated in-band book is "
              ">= 1k both sides, where old and new code act identically "
              "(old code only pads a SHORT side). Zero pads is REQUIRED by "
              "new code and merely unneeded by old. Re-probe when any side "
              "thins, or rely on the deploy chain (sync + "
              "code_change_exit_due self-restart, both verified enabled).")
    else:
        print("Consistent with NEW code: bot alive, zero imm- orders on "
              "gated series (halted/unselected events), zero pads. If the "
              "dry run above also refused/halted them, box behavior matches "
              "the merge.")


if __name__ == "__main__":
    main()
