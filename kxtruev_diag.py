#!/usr/bin/env python3
"""One-shot KXTRUEV diagnostic (runs in GitHub Actions, where Kalshi is
reachable; Claude's cloud container cannot reach kalshi.com).

Ground truth for "KXTRUEV still not quoting", from the exchange itself:
  1. MARKETS  — do KXTRUEV markets exist / are any open right now?
  2. PROGRAMS — does an active liquidity program cover them? (bot's own
     fetch_programs view + raw-feed sweep)
  3. DRY RUN  — the decisive test: one full dry run_cycle() of the actual
     bot, under the LIVE LAUNCHER'S $ProbeEnv (parsed below before import,
     the imm_quote_gaps parity trick), against live Kalshi. If KXTRUEV is
     selected and sim-quoted here, the code is proven end-to-end and any
     remaining silence is the trading box's deploy/restart chain. If not,
     the cycle log names the reason. READ-ONLY: live=False sim-gates every
     place/cancel ([DRY] branches), same mode imm_quote_gaps runs in daily.
  4. VERDICT.

Prints only public exchange data (this repo is public and so are its
Action logs — keep account balances/positions out of here).
"""

import glob
import os
import re
from datetime import datetime, timezone

import requests


def _apply_probe_env() -> None:
    """Apply the live launcher's $ProbeEnv to our environment BEFORE
    incentive_mm is imported, so blocklist/ladder/caps/window mults here are
    exactly what the running bot sees (keep in sync with imm_quote_gaps.py,
    which does the same). Workflow-provided env wins via setdefault —
    IMM_STATUS_DIR must keep pointing at the scratch dir."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "run_incentive_mm.ps1"),
                  encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return
    # Anchor on `"set ` and stay single-line: the launcher also has the
    # empty `$ProbeEnv = ""` initializer, which a lazy `.+?` with re.S
    # walks straight through into cross-line garbage.
    m = re.search(r'\$ProbeEnv\s*=\s*"(set [^"\n]+)"', txt)
    if not m:
        return
    for k, v in re.findall(r"set ([A-Z_0-9]+)=([^&]*)&&", m.group(1) + "&&"):
        os.environ.setdefault(k, v)


_apply_probe_env()

import incentive_mm as imm  # noqa: E402  (env parity must precede import)

BASE = imm.KALSHI_API_BASE


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def public_get(path: str, **params):
    r = requests.get(BASE + path, params=params or None, timeout=20)
    r.raise_for_status()
    return r.json()


def main() -> None:
    now = datetime.now(timezone.utc)
    print(f"UTC now: {now:%Y-%m-%d %H:%M:%S}"
          f" (ET {now.astimezone(imm.ET):%H:%M})")
    for k in ("IMM_LEVELS", "IMM_LADDER_MODE", "IMM_MAX_MARKETS",
              "IMM_BLOCKLIST", "IMM_COLLATERAL_BUDGET"):
        print(f"  probe-env {k}={os.environ.get(k, '<unset>')}")

    section("1. KXTRUEV MARKETS (public /markets, all statuses)")
    markets = []
    cursor = None
    while True:
        params = {"series_ticker": "KXTRUEV", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = public_get("/markets", **params)
        batch = resp.get("markets") or []
        markets.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch or len(markets) > 2000:
            break
    open_now = [m for m in markets if m.get("status") in ("active", "open")]
    print(f"total KXTRUEV markets: {len(markets)}; OPEN now: {len(open_now)}")
    for m in open_now[:20]:
        print(f"  {m.get('ticker', '?'):42s} open {m.get('open_time', '?')}"
              f"  close {m.get('close_time', '?')}")

    section("0. LIVE ACCOUNT SNAPSHOT (is the trading box's bot alive?)")
    # Same account key as the box, so resting imm- orders ARE the live bot's
    # output: order TTL is <=30 min, so ANY resting imm- order proves the
    # quote loop ran within the last half hour, and a KXTRUEV order proves
    # the new code is in. Counts + one timestamp only — repo and logs are
    # public, keep the footprint out of them.
    client = imm.build_client()
    resting = []
    cursor = None
    while True:
        resp = client.get_orders(status="resting", limit=200, cursor=cursor)
        batch = resp.get("orders") or []
        resting.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch or len(resting) > 5000:
            break
    imm_orders = [o for o in resting
                  if str(o.get("client_order_id", "")).startswith("imm-")]
    truev_orders = [o for o in imm_orders
                    if str(o.get("ticker", "")).startswith("KXTRUEV")]
    newest = max((str(o.get("created_time") or "") for o in imm_orders),
                 default="")
    print(f"resting orders on the account: {len(resting)}; "
          f"imm- bot orders: {len(imm_orders)}; on KXTRUEV: {len(truev_orders)}")
    print(f"newest imm- order created: {newest or 'n/a'}")
    if truev_orders:
        print("-> the box IS quoting KXTRUEV; nothing left to fix.")
    elif imm_orders:
        print("-> bot ALIVE on the box (fresh imm- orders) but ZERO KXTRUEV "
              "-> it is running OLD code; the restart is what's missing.")
    else:
        print("-> NO resting imm- orders: the bot on the box looks DOWN "
              "(or between waves); launcher/watchdog territory.")

    section("2. INCENTIVE PROGRAMS (the bot's own fetch_programs view)")
    bot = imm.IncentiveMarketMaker(client=client, live=False)
    by_market = bot.fetch_programs()
    truev_prog = {t: v for t, v in by_market.items()
                  if t.split("-")[0] == "KXTRUEV"}
    print(f"active liquidity programs cover {len(by_market)} markets; "
          f"KXTRUEV: {len(truev_prog)}")
    for t, v in sorted(truev_prog.items()):
        print(f"  {t:42s} ${v['dollars_per_day']:.2f}/day  "
              f"target {v['target']:.0f}  ends {v['end']:%Y-%m-%d %H:%M}Z")

    section("3. DRY-RUN CYCLE (production ProbeEnv parity, read-only)")
    bot.run_cycle()
    selected_truev = sorted(t for t in (bot.state.selected or [])
                            if str(t).startswith("KXTRUEV"))
    print(f"\nselected markets total: {len(bot.state.selected or [])}; "
          f"KXTRUEV selected: {len(selected_truev)}")
    for t in selected_truev:
        print(f"  selected: {t}")
    sims = bot.state.sim_orders or {}
    sim_iter = sims.values() if isinstance(sims, dict) else sims
    truev_sims = [o for o in sim_iter if "KXTRUEV" in str(o)]
    print(f"sim orders total: {len(sims)}; on KXTRUEV: {len(truev_sims)}")
    for o in truev_sims[:12]:
        print(f"  sim: {o}")

    print("\ncycle-log rows mentioning KXTRUEV:")
    status_dir = os.environ.get("IMM_STATUS_DIR", ".")
    for path in sorted(glob.glob(os.path.join(status_dir, "cycle_log*.csv"))):
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if lines:
            print(f"  [{os.path.basename(path)}] header: {lines[0].rstrip()}")
        hits = [ln.rstrip() for ln in lines[1:] if "KXTRUEV" in ln]
        for ln in hits[:20]:
            print(f"  {ln}")
        if not hits:
            print("  (no KXTRUEV rows)")

    section("4. VERDICT")
    if not truev_prog:
        print("NO incentive program on KXTRUEV -> the bot will never quote "
              "it, by design; idle until Kalshi lights a program.")
    elif truev_sims:
        print("CODE PROVEN: this exact repo state selected KXTRUEV and "
              "placed dry quotes against the live book. A silent bot on the "
              "trading box means the box is running old code or was never "
              "restarted — the deploy/restart chain is the blocker, not the "
              "config.")
    elif selected_truev:
        print("KXTRUEV selected but no sim quotes — placement-stage blocker "
              "(caps/budget/bands); see the [DRY]/skip lines above.")
    else:
        print("KXTRUEV NOT selected in a fresh dry run — the reason is in "
              "the cycle-log rows / screen lines above; that is the bug to "
              "fix.")


if __name__ == "__main__":
    main()
