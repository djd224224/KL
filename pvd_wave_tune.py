"""Re-apply a wave's hand tuning to its post_vote_dead_books.json entries.

pvd_scan.py rebuilds a relit entry from scratch, so anything set by hand on the
previous period (widened limits, a price band) is lost at every relight. This
re-applies it to the wave's LIVE entries (stop in the future, period_start at or
after --since, series in --series), idempotently:

  --room N           widen the no-last-trade limits N c each way (bid_max += N,
                     ask_min -= N, never crossing), once per entry ("widened" note)
  --price-min/-max   the entry's price band (the bot's default is 3-97c)
  --bank-size/--base-size/--bank-usd/--bank-hist
                     BANK EARLY: an entry rests at --bank-size a side until the
                     dashboard (imm_pvd_watch history.json "pcum", banked est THIS
                     program period) shows --bank-usd banked, then at --base-size.
                     A relit entry starts a new period, so it goes back up. A side
                     the scanner sized above --bank-size (a decided side) is kept.
                     If history.json is older than --bank-stale-min, sizes are left
                     as they are. WITHOUT --base-size, a raised entry keeps its own
                     size in "pre_bank" and goes back to it once banked (Jack
                     2026-11-07: "dont go lower than 200 on any market right now
                     until hitting $1.30 threshold" -- the other waves' own sizes).
  --no-drop          once banked, a market KEEPS its size: no drop to --base-size, no
                     return to its pre_bank size (Jack 2026-11-07: "dont drop any size
                     after banking 1.3")
  --series ALL --exclude-series A,B
                     every listed series except those (another wave's own pass).
  --bank-url/--bank-max/--bank-by-hrs
                     ...and where --bank-size won't get there in time, --bank-max: read
                     the dashboard's /data rows (banked this period, live $/day at the
                     size resting, hours to the stop); an unbanked market goes to
                     --bank-max when banked + rate x (bank-size / size now) x
                     (hours left - bank-by-hrs) / 24 < --bank-usd, and stays there
                     until it banks (no flapping). A market with nothing resting keeps
                     its size. /data replaces --bank-hist when given.
  --flip-through     set "flip_through": the bot rests the side that reduces a
                     position at its size PLUS the position (Jack 2026-11-07: "if i
                     bought 200 on one side, i can sell 400 on the other side")
  --no-halts         set "no_halts": the bot exempts the entry's series from the sweep
                     breaker, tape trigger, group breaker and toxic halts (Jack
                     2026-11-07: "remove any sweep breakers / halts on these markets")
  --halts-on         clear "no_halts" from the entries in scope: the sweep breaker, tape
                     trigger and toxic halts apply again (dropping --no-halts alone leaves
                     the flag on entries already listed; a relit entry is rebuilt without
                     it). Jack's word, like --no-halts (2026-11-08 pick-off review).
  --cheap-tiers C:X,C2:X2  (or --cheap-mult X --cheap-px C for one tier)
                     a CHEAP side (a bid at its touch, an ask at 100 - its touch: what a
                     fill ties up a contract) rests X x the size for the first tier it
                     fits (Jack 2026-11-07: "double cheap sides" = <= 5c x2, then "1.5x on
                     size 5 to 15" = <= 15c x1.5); back to the size when it leaves them
  --series-tier SERIES:SIDE:C:X  (repeatable)
                     that series' SIDE costing <= C c rests X x the size, ahead of the
                     general tiers (Jack 2026-11-07, "yes" to 10x on the 2028 Democratic
                     primaries' 1-2c bids: KXDPRESPRIMARY:bid:2:10)
  --follow-book      set "follow_book": the bot keeps the listed limits even with a last
                     trade, and --reanchor re-anchors traded entries too (Jack 2026-11-07:
                     "Yes, fix it" -- a stale last trade held a side out of the book)
  --cushion N        the entry's last-trade cushion (the bot: live 5c, dead 2c). With
                     a last trade the guard holds a bid N c under it and an ask N c over
                     it, ignoring bid_max / ask_min; at 0 a side may rest at the trade.
  --reanchor N       FOLLOW THE BOOK: each run, an entry with no last trade gets
                     bid_max = best bid + N, ask_min = best ask - N from a fresh
                     /markets read (the touch, ours included). Fixed limits went stale
                     within minutes as other makers tightened the spreads. A market
                     the read misses keeps its limits. CAPPED (Jack 2026-11-08: "Cap
                     the reanchor"): toward the taker (bid_max up, ask_min down) a
                     limit moves at most --reanchor-step c (5) a run, and holds in a
                     run where that side's touch jumped --reanchor-jump c (10)+ that
                     way since the last run (entry "anchor_touch"); away from the
                     taker it moves at once. 8 jumped books cost ~$2k 11/07-11/08.
  --size-mult-file PATH
                     PER-MARKET MULTIPLES (Jack 2026-11-14: "size them up and report back",
                     on the FX + metals wave's ~400 markets pacing within 2x of the $1
                     payout floor): PATH = {ticker: X}. A listed market's target multiple
                     is --size-mult x its X (1 when absent), applied once per entry like
                     --size-mult (base, bank and kept sizes; the cheap tiers multiply it),
                     so dropping a ticker from the file scales it back. X = 0 is OFF:
                     pinned at 0 a side (only a position's closing order rests). Unreadable: x1.
  --fair-file PATH   FAIR LIMITS (Jack 2026-11-13, the after-economic-events FX + metals
                     wave: "Are there Fair value calculated on these markets?" -> "1",
                     stand down the wrong-side quotes and build a fair). PATH =
                     {"fair": {ticker: cents}, "margin": {series: c}, "valid_until": ISO}.
                     After the re-anchor, an in-scope entry's bid limit is at most
                     fair - margin and its ask limit at least fair + margin; margin = the
                     series' base (the file's, else --fair-margin, 3c) + 100 x
                     --fair-vol-err (0.25) x phi(z)|z| -- how far the fair moves under that
                     vol error: 0 at the money and deep in the tails, ~6c at one sd out.
                     Never tighter than --fair-floor (2): a bid at <= 2c and an ask at >= 98c
                     always pass (a fill there costs <= 2c a contract). A side whose touch is
                     past its limit stands out (the bot's "pvd guard ... out (limits ...)").
                     No fair for the ticker, a file past valid_until, or an unreadable file:
                     only the floor sides may rest. Entry note "fair_clamp".

Holds pvd_scan's lock while it reads and writes, so a scan can't overwrite it
(and it waits up to --wait secs for a running scan, then gives up: the next run
catches up). Dry run unless --write.

2028 election wave, Jack 2026-11-06: "i said prioritize quoting" (3c room, after
other makers stepped inside the scan-time limits), "also do 2 and 3" (2-98c
band on the long shots and safe seats, a relight task for the wave) and
"200 a side until $1.30 is banked, then back down" (bank early: Kalshi pays a
period only once it accrues $1, and 816 of 1,722 projected just $1.10-1.50).
2026-11-07 "Yes, follow the book" (cushion 0 + re-anchor 3c every 15 min: at
00:35Z 370 of 1,723 had a side pulled, 362 of them never traded -- the books
had tightened to 1c past the 3c room), and "want to bank $1.30 sooner. go up
to 300 a side where 200 wont bank $1.30 by 6 hours before period end."
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from statistics import NormalDist

HERE = os.path.dirname(os.path.abspath(__file__))
LIST = os.path.join(HERE, "run-logs", "incentive-mm", "post_vote_dead_books.json")
LOCK = os.path.join(HERE, "run-logs", "pvd-scan", "scan.lock")
PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def read_touches(tickers):
    """{ticker: (best bid c, best ask c, last c)} from /markets reads, 100 a call,
    SIGNED first via kalshi_reads (Kalshi throttles unsigned list reads: the first
    public-only try lost 700 of 1,723 to 429s), 3 tries each; a batch that still
    fails is just missing (counted)."""
    sys.path.insert(0, HERE)
    try:
        import kalshi_reads                     # noqa: E402
    except Exception:  # noqa: BLE001
        kalshi_reads = None
    out, missed = {}, 0
    tickers = sorted(tickers)
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        params = {"tickers": ",".join(batch), "limit": 1000}
        j = None
        for k in range(3):
            try:
                if kalshi_reads is not None:
                    j = kalshi_reads.kalshi_get("/markets", params)
                else:
                    url = PUBLIC + "/markets?" + urllib.parse.urlencode(params)
                    req = urllib.request.Request(url, headers={"User-Agent": "pvd_wave_tune",
                                                               "Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        j = json.load(r)
                if j:
                    break
            except Exception:  # noqa: BLE001 -- 429s and timeouts: back off and retry
                j = None
            time.sleep(2 * (k + 1))
        if j is None:
            missed += len(batch)
            continue
        for m in j.get("markets") or []:
            def c(f):
                try:
                    return float(m.get(f) or 0) * 100
                except (TypeError, ValueError):
                    return 0.0
            out[m["ticker"]] = (c("yes_bid_dollars"), c("yes_ask_dollars"), c("last_price_dollars"))
        time.sleep(0.3)
    return out, missed


_ND = NormalDist()


def fair_margin(base: float, fair_c: float, vol_err: float) -> float:
    """--fair-file's margin (c) at a fair of fair_c: the base + the fair's move under a
    relative vol error of vol_err, 100 x vol_err x phi(z)|z| (z = the fair's normal score)."""
    p = min(max(fair_c / 100.0, 1e-6), 1 - 1e-6)
    z = _ND.inv_cdf(p)
    return base + 100.0 * vol_err * _ND.pdf(z) * abs(z)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", required=True, help="comma-separated EXACT series names, or ALL")
    ap.add_argument("--exclude-series", default="", help="comma-separated series left out (with ALL: other waves)")
    ap.add_argument("--exclude-registry", action="store_true",
                    help="also leave out every series of run-logs/pvd-scan/waves.json (pvd_waves.py)")
    ap.add_argument("--since", required=True, help="ISO: only entries whose period_start is at or after this")
    ap.add_argument("--room", type=int, default=0)
    ap.add_argument("--price-min", type=int)
    ap.add_argument("--price-max", type=int)
    ap.add_argument("--bank-size", type=int)
    ap.add_argument("--base-size", type=int)
    ap.add_argument("--bank-usd", type=float, default=1.30)
    ap.add_argument("--bank-hist", help="imm_pvd_watch history.json of the wave's dashboard")
    ap.add_argument("--bank-stale-min", type=float, default=30.0)
    ap.add_argument("--no-drop", action="store_true",
                    help="a market that has banked --bank-usd keeps its size (no drop to --base-size or back to its own)")
    ap.add_argument("--bank-url", help="the wave dashboard's /data, e.g. http://127.0.0.1:8773/data")
    ap.add_argument("--bank-max", type=int)
    ap.add_argument("--bank-by-hrs", type=float, default=6.0)
    ap.add_argument("--twin-block", type=float, default=0.0,
                    help="N: in a two-leg D/R event, when one leg holds >= N contracts (Kalshi position), the "
                         "other leg's side that makes the same bet rests 0 -- off: the bot rests only an order that closes a "
                         "position there (long YES on D -> R's ask; short -> R's bid)")
    ap.add_argument("--side-cap", action="append", default=[],
                    help="SERIES:C:N -- a side of SERIES whose fill ties up >= C c a contract rests N "
                         "(0 = off: the bot rests only an order that closes a position, 1b58188)")
    ap.add_argument("--pause-at", action="append", default=[],
                    help="SERIES:C -- a market of SERIES at C c+ (bid or last) quotes nothing, or only the "
                         "side that unwinds its position; resumes under C - 2")
    ap.add_argument("--pin", action="append", default=[],
                    help="TICKER:N -- that market rests N a side: no --size-mult, bank step or cheap tier")
    ap.add_argument("--pin-series", action="append", default=[],
                    help="SERIES:N[:flat] -- --pin for every market of the series (a --pin of the market wins)")
    ap.add_argument("--hold-max", action="store_true",
                    help="a market raised to --bank-max stays there for its period (no step back down "
                         "to --bank-size before it banks)")
    ap.add_argument("--size-mult", type=float, default=1.0,
                    help="every size is X times what the rules give (bank size, bank max, base size, a banked "
                         "market's kept size; the cheap tiers multiply it); the bank-early test stays on the "
                         "unscaled --bank-size. Applied once per entry (its size_mult key)")
    ap.add_argument("--flip-through", action="store_true",
                    help='set "flip_through" (the bot: the reducing side rests its size + the position)')
    ap.add_argument("--no-halts", action="store_true",
                    help='set "no_halts" (the bot: no sweep breaker, tape trigger, group or toxic halt on the series)')
    ap.add_argument("--halts-on", action="store_true",
                    help="clear no_halts from the entries in scope (the bot's breakers and halts apply again)")
    ap.add_argument("--cheap-mult", type=float, help="the cheap side rests this x the size")
    ap.add_argument("--cheap-px", type=float, default=5.0, help="cheap = a bid touch <= this c / an ask touch >= 100 - this c")
    ap.add_argument("--series-tier", action="append", default=[],
                    help="SERIES:SIDE:C:X -- for that series a SIDE (bid/ask) costing <= C c rests X x the size, ahead of --cheap-tiers")
    ap.add_argument("--cheap-tiers", default="", help="C:X,... -- a side costing <= C c a contract rests X x the size (first tier that fits)")
    ap.add_argument("--follow-book", action="store_true",
                    help='set "follow_book" (the bot keeps the listed limits with a last trade too) and re-anchor traded entries as well')
    ap.add_argument("--cushion", type=int)
    ap.add_argument("--reanchor", type=int)
    ap.add_argument("--reanchor-step", type=int, default=5,
                    help="a re-anchor moves bid_max up / ask_min down at most this c a run (0 = no cap)")
    ap.add_argument("--reanchor-jump", type=int, default=10,
                    help="a side whose touch jumped this c+ toward the taker since the last run keeps its limit (0 = off)")
    ap.add_argument("--size-mult-file",
                    help="{ticker: X}: that market's sizes are --size-mult x X (applied once per entry)")
    ap.add_argument("--fair-file",
                    help='{"fair": {ticker: cents}, "margin": {series: c}, "valid_until": ISO}: after the '
                         "re-anchor, bid limit <= fair - margin and ask limit >= fair + margin (see the docstring)")
    ap.add_argument("--fair-margin", type=float, default=3.0,
                    help="--fair-file's base margin c (the file's per-series margin wins)")
    ap.add_argument("--fair-vol-err", type=float, default=0.25,
                    help="--fair-file: margin += 100 x this x phi(z)|z|, the fair's move under this relative vol error")
    ap.add_argument("--fair-floor", type=int, default=2,
                    help="--fair-file: a bid at <= this c and an ask at >= 100 - this c always pass")
    ap.add_argument("--note", default="")
    ap.add_argument("--wave-added", default="",
                    help="ISO: the registry wave's creation time, written on every entry in scope (the bot: "
                         "UNWIND_ONLY_WAVES covers only waves added before the switch; Jack 2026-11-08)")
    ap.add_argument("--wait", type=int, default=120)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    series = {s.strip() for s in a.series.split(",") if s.strip()}
    tiers = sorted((float(c), float(m)) for c, m in (x.split(":") for x in a.cheap_tiers.split(",") if x.strip()))
    if not tiers and a.cheap_mult:
        tiers = [(a.cheap_px, a.cheap_mult)]
    stiers = {}                         # {(series, 'bid_size'/'ask_size'): [(C, X), ...]}
    for x in a.series_tier:
        ss, sd, cc, mm = x.split(":")
        stiers.setdefault((ss, sd + "_size"), []).append((float(cc), float(mm)))
    for v in stiers.values():
        v.sort()
    exclude = {s.strip() for s in (a.exclude_series or "").split(",") if s.strip()}
    if a.exclude_registry:
        # the registry's waves (pvd_waves.py) tune themselves in pass 7; a catch-all pass
        # (--series ALL) must leave them alone or the two passes fight every run
        try:
            with open(os.path.join(os.path.dirname(LIST), "..", "pvd-scan", "waves.json"), encoding="utf-8") as fh:
                exclude |= {s for w in (json.load(fh).get("waves") or {}).values()
                            for s in (w.get("series") or []) + (w.get("near") or [])}
        except (OSError, ValueError):
            pass
    all_series = series == {"ALL"}
    pause_at = {x.split(":")[0].strip(): float(x.split(":")[1]) for x in a.pause_at if ":" in x}
    side_caps = [(x.split(":")[0].strip(), float(x.split(":")[1]), int(x.split(":")[2]))
                 for x in a.side_cap if x.count(":") == 2]
    pins, pin_flat = {}, set()
    for x in a.pin:                 # TICKER:N or TICKER:N:flat
        pp = [s.strip() for s in x.split(":")]
        if len(pp) >= 2:
            pins[pp[0]] = int(pp[1])
            if len(pp) > 2 and pp[2].lower() == "flat":
                pin_flat.add(pp[0])
    pin_series, pin_series_flat = {}, set()
    for x in a.pin_series:          # SERIES:N or SERIES:N:flat
        pp = [s.strip() for s in x.split(":")]
        if len(pp) >= 2:
            pin_series[pp[0]] = int(pp[1])
            if len(pp) > 2 and pp[2].lower() == "flat":
                pin_series_flat.add(pp[0])

    def in_scope(t: str) -> bool:
        s = t.split("-")[0]
        return (all_series or s in series) and s not in exclude
    since = ts(a.since)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    tmult, tmult_why, off_by_file = {}, "", set()
    if a.size_mult_file:
        try:
            with open(a.size_mult_file, encoding="utf-8") as fh:
                tmult = {str(k): float(v) for k, v in (json.load(fh) or {}).items() if float(v) >= 0}
            # a 0 is OFF (Jack 2026-11-14: "stop quoting the rest"): that market is pinned
            # at 0 a side -- it rests nothing but the order that closes a position
            for k in [k for k, v in tmult.items() if v == 0]:
                pins.setdefault(k, 0)
                off_by_file.add(k)
                del tmult[k]
        except (OSError, ValueError, TypeError, AttributeError) as ex:
            tmult_why = f"; size-mult file unreadable ({ex}): x1"
            tmult = {}
    # FAIR LIMITS (--fair-file): None = off; {} = on with no usable fair (floors only)
    fair_map, fair_base, fair_why = None, {}, ""
    if a.fair_file:
        try:
            with open(a.fair_file, encoding="utf-8") as fh:
                _fd = json.load(fh)
            fair_map = {k: float(v) for k, v in (_fd.get("fair") or {}).items()}
            fair_base = {k: float(v) for k, v in (_fd.get("margin") or {}).items()}
            if _fd.get("valid_until") and now >= ts(_fd["valid_until"]):
                fair_why = f"fair file stale since {_fd['valid_until']}"
                fair_map = {}
        except (OSError, ValueError, TypeError, AttributeError) as ex:
            fair_why = f"fair file unreadable ({ex})"
            fair_map = {}

    # the touches are read BEFORE the lock (18 calls for ~1,700 markets), so the
    # lock is held only for the read-modify-write of the list
    touch, missed = {}, 0
    if a.reanchor is not None or tiers or stiers or side_caps or a.twin_block:
        pre = json.load(open(LIST, encoding="utf-8")).get("markets") or {}
        touch, missed = read_touches([t for t in pre if in_scope(t)])
    # TWIN BLOCK: the account's positions (Kalshi, so fills from before any dashboard's
    # history count) and the two-leg D/R events on the list
    acct_pos, twin_of = {}, {}
    if a.twin_block:
        try:
            from kalshi_reads import kalshi_get_all
            for x in kalshi_get_all("/portfolio/positions", {"limit": 1000, "count_filter": "position"},
                                    items_key="market_positions") or []:
                v = float(x.get("position_fp") or x.get("position") or 0)
                if v:
                    acct_pos[x["ticker"]] = v
        except Exception as ex:  # noqa: BLE001
            print(f"{stamp} ! twin block: position read failed ({ex}); no twin caps this run")
            acct_pos = None
        evs = {}
        for t_ in (json.load(open(LIST, encoding="utf-8")).get("markets") or {}):
            evs.setdefault(t_.rsplit("-", 1)[0], []).append(t_)
        for ev, ts_ in evs.items():
            if len(ts_) == 2 and {x.rsplit("-", 1)[1] for x in ts_} == {"D", "R"}:
                twin_of[ts_[0]], twin_of[ts_[1]] = ts_[1], ts_[0]
    if a.write:
        t0 = time.time()
        while os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) < 1500:
            if time.time() - t0 > a.wait:
                print(f"{stamp} scan.lock held for {a.wait}s -- not writing (the next run catches up)")
                return 2
            time.sleep(3)
        open(LOCK, "w").close()
    bank = None                     # {ticker: [period_start, banked $]} or None = sizes untouched
    rows = {}                       # /data rows by ticker (rate, size, hrs_left, rest_ct)
    bank_msg = ""
    if a.bank_size and a.bank_url:
        try:
            req = urllib.request.Request(a.bank_url, headers={"User-Agent": "pvd_wave_tune"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = (json.load(r) or {}).get("data") or {}
            age_min = (datetime.now(timezone.utc) - ts(d["now"])).total_seconds() / 60.0
            if age_min > a.bank_stale_min:
                bank_msg = f"; bank sizing SKIPPED: {a.bank_url} is {age_min:.0f} min old"
            else:
                rows = {x["ticker"]: x for x in d.get("rows") or []}
                bank = {t: [x.get("period"), float(x.get("period_est") or 0)] for t, x in rows.items()}
        except Exception as ex:  # noqa: BLE001
            bank_msg = f"; bank sizing SKIPPED: {a.bank_url}: {ex}"
    elif a.bank_size and a.bank_hist:
        try:
            age_min = (time.time() - os.path.getmtime(a.bank_hist)) / 60.0
            if age_min > a.bank_stale_min:
                bank_msg = f"; bank sizing SKIPPED: {a.bank_hist} is {age_min:.0f} min old"
            else:
                bank = json.load(open(a.bank_hist, encoding="utf-8")).get("pcum") or {}
        except (OSError, ValueError) as ex:
            bank_msg = f"; bank sizing SKIPPED: {ex}"
    try:
        cur = json.load(open(LIST, encoding="utf-8"))
        n_room = n_band = n_seen = n_up = n_down = n_banked = n_cush = n_anch = n_traded = n_max = n_flip = n_nohalt = n_cheap = n_follow = n_scaled = n_pin = n_capped = n_touch = n_wadd = 0
        n_pause = n_pause_off = n_pause_unw = 0
        n_fair = n_fair_none = 0
        # the bank sizes, set here too: a run with no unpinned live entry never reaches
        # the per-entry lines below, and the summary used to crash on M (pass 4, every
        # run from 05:00Z 11/08, once the 2028 programs ended -- nothing was written)
        B = int(round(a.bank_size * a.size_mult)) if a.bank_size else 0
        M = int(round(a.bank_max * a.size_mult)) if a.bank_max else 0

        def caps_for(t: str, e: dict) -> dict:
            """{side key: N} for the sides --side-cap holds, by the touch. SERIES "ALL"
            caps every in-scope market (Jack 2026-11-07: "turn off quoting 90c+"). A
            capped side that reduces our position (the wave dashboard's "pos") still
            clears it: with flip-through the bot adds the position to N; without, the
            side rests the position. N = 0 is OFF (Jack 2026-11-08: "why even have a
            probe?"): 0 is written as is, and the bot sizes the order that closes a
            position from the live position each cycle (1b58188)."""
            out: dict = {}
            capwhy.clear()
            if t not in touch:
                return out
            # the twin leg holds the same bet: block that side here -- 0, OFF (Jack
            # 2026-11-08: "why even have a probe?"; it was a 1-lot, which the sweeps
            # took; the bot rests only an order that closes a position there, 1b58188)
            if a.twin_block and acct_pos is not None and t in twin_of:
                p_tw = acct_pos.get(twin_of[t], 0.0)
                if p_tw >= a.twin_block:
                    out["ask_size"], capwhy["ask_size"] = 0, f"twin {twin_of[t].rsplit('-', 1)[1]} long {p_tw:.0f}"
                elif p_tw <= -a.twin_block:
                    out["bid_size"], capwhy["bid_size"] = 0, f"twin {twin_of[t].rsplit('-', 1)[1]} short {-p_tw:.0f}"
            if not side_caps:
                return out
            yb, ya, _lp = touch[t]
            cost = {"bid_size": yb if yb > 0 else None, "ask_size": 100 - ya if 0 < ya < 100 else None}
            pos_c = float((rows.get(t) or {}).get("pos") or 0) if rows else 0.0
            for cs_, cc_, cn_ in side_caps:
                if cs_ in ("ALL", "*") or t.split("-")[0] == cs_:
                    for k, c in cost.items():
                        if c is not None and c >= cc_:
                            if k in out:
                                continue        # already blocked by the twin rule
                            reduces = (k == "bid_size" and pos_c <= -1) or (k == "ask_size" and pos_c >= 1)
                            # (N = 0 is OFF: the bot sizes the closing order live, 1b58188)
                            out[k] = (max(cn_, int(abs(pos_c)))
                                      if cn_ >= 1 and reduces and not e.get("flip_through") else cn_)
            return out
        capwhy: dict = {}
        for t, e in (cur.get("markets") or {}).items():
            if not in_scope(t):
                continue
            try:
                if ts(e["stop"]) <= now or (e.get("period_start") and ts(e["period_start"]) < since):
                    continue
            except (KeyError, ValueError):
                continue
            n_seen += 1
            if a.room > 0 and "widened" not in e:
                b0, a0 = int(e["bid_max"]), int(e["ask_min"])
                r = max(0, min(a.room, (a0 - b0 - 1) // 2))     # never let the bid limit reach the ask limit
                e["bid_max"], e["ask_min"] = b0 + r, a0 - r
                e["widened"] = f"{stamp}: limits {b0}/{a0} -> {b0 + r}/{a0 - r} ({a.room}c room each way) {a.note}".strip()
                n_room += 1
            if a.price_min is not None and a.price_max is not None and not (
                    isinstance(e.get("tpause"), dict) and e["tpause"].get("mode") == "off") and (
                    e.get("price_min") != a.price_min or e.get("price_max") != a.price_max):
                e["price_min"], e["price_max"] = a.price_min, a.price_max
                e["band"] = f"{stamp}: {a.price_min}-{a.price_max}c {a.note}".strip()
                n_band += 1
            if a.flip_through and not e.get("flip_through"):
                e["flip_through"] = True
                n_flip += 1
            if a.no_halts and not e.get("no_halts"):
                e["no_halts"] = True
                n_nohalt += 1
            elif a.halts_on and not a.no_halts and e.get("no_halts"):
                e.pop("no_halts", None)
                n_nohalt += 1
            if a.wave_added and e.get("wave_added") != a.wave_added:
                e["wave_added"] = a.wave_added
                n_wadd += 1
            if a.cushion is not None and e.get("cushion") != a.cushion:
                e["cushion_set"] = f"{stamp}: cushion {e.get('cushion')} -> {a.cushion} {a.note}".strip()
                e["cushion"] = a.cushion
                n_cush += 1
            if a.follow_book and not e.get("follow_book"):
                e["follow_book"] = True
                n_follow += 1
            if a.reanchor is not None and t in touch:
                yb, ya, lp = touch[t]
                if lp > 0 and not e.get("follow_book"):
                    n_traded += 1           # the guard follows the last trade; bid_max / ask_min unused
                else:
                    b0, a0 = int(e["bid_max"]), int(e["ask_min"])
                    b1 = min(99, int(math.floor(yb)) + a.reanchor) if yb > 0 else b0
                    a1 = max(1, int(math.ceil(ya)) - a.reanchor) if 0 < ya < 100 else a0
                    # CAPPED RE-ANCHOR (Jack 2026-11-08: "Cap the reanchor"). 23:19Z 11/07 -
                    # 02:08Z 11/08, 8 thin KXHOUSERACE books jumped far off fair for 5-15 min
                    # (IN04-D, a 3c long shot, to 40x80; IL06-D, a ~95c favorite, to 23x66);
                    # the next run put the limits on the jumped touch, the bot rested full
                    # size there and was hit (~$2k), then the books snapped back. Now a limit
                    # moves toward the taker's side (bid_max up, ask_min down) at most
                    # --reanchor-step c a run, and not at all in a run where that side's touch
                    # jumped --reanchor-jump c+ that way since the last run (it has to stand a
                    # run first). Moves away from the taker (bid_max down, ask_min up) apply at
                    # once. 0 turns either off.
                    prev = e.get("anchor_touch")
                    pb, pa = (prev[0], prev[1]) if isinstance(prev, list) and len(prev) >= 2 else (None, None)
                    why = []
                    # ...and a side that was EMPTY last run (no bid / no ask) holds a run too:
                    # a bait order into an empty side is a jump from nothing (11/08 audit)
                    if b1 > b0:
                        if a.reanchor_jump > 0 and pb is not None and yb > 0 and (
                                pb <= 0 or yb - pb >= a.reanchor_jump):
                            b1 = b0
                            why.append(f"bid touch jumped {pb:g}->{yb:g}, held")
                        elif a.reanchor_step > 0 and b1 - b0 > a.reanchor_step:
                            b1 = b0 + a.reanchor_step
                            why.append(f"bid up {a.reanchor_step}c")
                    if a1 < a0:
                        if a.reanchor_jump > 0 and pa is not None and 0 < ya < 100 and (
                                not 0 < pa < 100 or pa - ya >= a.reanchor_jump):
                            a1 = a0
                            why.append(f"ask touch jumped {pa:g}->{ya:g}, held")
                        elif a.reanchor_step > 0 and a0 - a1 > a.reanchor_step:
                            a1 = a0 - a.reanchor_step
                            why.append(f"ask down {a.reanchor_step}c")
                    if why:
                        n_capped += 1
                        e["anchor_capped"] = f"{stamp}: touch {yb:g}x{ya:g}: {'; '.join(why)}"
                    now_touch = [round(yb, 2), round(ya, 2)]
                    if prev != now_touch:
                        e["anchor_touch"] = now_touch
                        n_touch += 1
                    if (b1, a1) != (b0, a0):
                        e["bid_max"], e["ask_min"] = b1, a1
                        e["anchored"] = f"{stamp}: touch {yb:g}x{ya:g} -> limits {b1}/{a1} (were {b0}/{a0})"
                        n_anch += 1
            # FAIR LIMITS (--fair-file, Jack 2026-11-13: see the docstring): after the
            # re-anchor, so a limit it walks toward the touch comes back to the fair each run
            if fair_map is not None:
                b0, a0 = int(e["bid_max"]), int(e["ask_min"])
                f_ = fair_map.get(t)
                if f_ is None:
                    fb, fa = a.fair_floor, 100 - a.fair_floor
                    why_f = f"no fair ({fair_why or 'not in the file'}): floors only"
                    n_fair_none += 1
                else:
                    m_ = fair_margin(fair_base.get(t.split("-")[0], a.fair_margin), f_, a.fair_vol_err)
                    fb = max(a.fair_floor, int(math.floor(f_ - m_)))
                    fa = min(100 - a.fair_floor, int(math.ceil(f_ + m_)))
                    why_f = f"fair {f_:g} +-{m_:.1f}c"
                b1, a1 = min(b0, fb), max(a0, fa)
                if (b1, a1) != (b0, a0):
                    e["bid_max"], e["ask_min"] = b1, a1
                    e["fair_clamp"] = f"{stamp}: {why_f} -> limits {b1}/{a1} (were {b0}/{a0})"
                    n_fair += 1
            # PINNED (--pin TICKER:N, Jack 2026-11-07 on KXJEFFRIESFIRST-27FEB01: "too active,
            # scale it down to base contract sizes"): N a side, no --size-mult, bank step or
            # cheap tier. Band, limits, flip-through etc. above still apply. In the task's
            # args, so a relit entry (rebuilt by the scanner) is pinned again.
            # ...or every market of a series (--pin-series SERIES:N[:flat], Jack 2026-11-07 on
            # the 14 markets settling before July 2028: "all 14 to 300, keep position unwinds")
            s_t = t.split("-")[0]
            pin = pins.get(t) if t in pins else pin_series.get(s_t)
            # OFF BY THE MULTIPLES FILE (a 0 there): the sizes are kept in "pre_off" so lifting
            # the 0 brings them back (a pin at 0 alone would leave the market at size 0)
            if t in off_by_file and pin == 0 and "pre_off" not in e and int(e.get("size") or 0) > 0:
                e["pre_off"] = {k: e[k] for k in ("size", "bid_size", "ask_size", "size_mult") if k in e}
            elif pin is None and isinstance(e.get("pre_off"), dict):
                for k in ("bid_size", "ask_size", "cheap_sides", "pre_bank", "pinned", "size_mult"):
                    e.pop(k, None)
                e.update(e.pop("pre_off"))
                e["unpinned"] = f"{stamp}: back on from the multiples file's 0 {a.note}".strip()
                n_pin += 1
            if pin is not None:
                is_flat = t in pin_flat or (t not in pins and s_t in pin_series_flat)
                # the --side-cap still holds on a pinned market ("turn off quoting 90c+")
                want_sides = caps_for(t, e)
                # ...and a pin at 0 is OFF on both sides: the bot reads "size": 0 as its default
                # size (PVD_SIZE, 40 a side), so only "bid_size"/"ask_size" 0 rest nothing but a
                # closing order (PVD_ZERO_OFF). 11/14: 335 of the 341 markets the w1113 multiples
                # file turned off still rested 40-80 a side
                if pin == 0:
                    want_sides = {"bid_size": 0, "ask_size": 0}
                have_sides = {k: int(e[k]) for k in ("bid_size", "ask_size") if k in e}
                if int(e.get("size") or 0) != pin or have_sides != want_sides \
                        or set(e.get("cheap_sides") or []) != set(want_sides):
                    for k in ("bid_size", "ask_size", "cheap_sides", "pre_bank"):
                        e.pop(k, None)
                    e["size"] = pin
                    e.update(want_sides)
                    if want_sides:
                        e["cheap_sides"] = sorted(want_sides)
                    e["pinned"] = (f"{stamp}: {pin} a side (no size mult, bank step or cheap tiers"
                                   + (f"; capped {', '.join(f'{k.split(chr(95))[0]} {v}' for k, v in sorted(want_sides.items()))}"
                                      if want_sides else "") + f") {a.note}").strip()
                    n_pin += 1
                # ...":flat" (Jack 2026-11-07 on Jeffries long 450: "try to fully offset the
                # exposure, so quote 450"): the reducing side rests max(N, position), not
                # N + position -- one fill flattens it and none goes past flat (the bot's
                # "flip_to_flat")
                if is_flat != bool(e.get("flip_to_flat")):
                    if is_flat:
                        e["flip_to_flat"] = True
                    else:
                        e.pop("flip_to_flat", None)
                    n_pin += 1
                continue
            # SIZE MULTIPLIER (--size-mult, Jack 2026-11-07: "increase everything by 1.5x"):
            # an entry's sizes are scaled once to the new multiple (its size_mult key says
            # which it carries); the scanner writes a relit entry unscaled, so it is scaled
            # again here. The bank step below works in the scaled sizes.
            m_cur = float(e.get("size_mult") or 1.0)
            sm_t = a.size_mult * tmult.get(t, 1.0)      # the wave's multiple x this market's own
            if abs(m_cur - sm_t) > 1e-9:
                f = sm_t / m_cur
                for k in ("size", "bid_size", "ask_size"):
                    if e.get(k):
                        e[k] = max(1, int(round(int(e[k]) * f)))
                if isinstance(e.get("pre_bank"), dict):
                    for k in ("size", "bid_size", "ask_size"):
                        if e["pre_bank"].get(k):
                            e["pre_bank"][k] = max(1, int(round(int(e["pre_bank"][k]) * f)))
                if abs(sm_t - 1.0) > 1e-9:
                    e["size_mult"] = sm_t
                else:
                    e.pop("size_mult", None)
                e["scaled"] = f"{stamp}: sizes x{f:g} (now x{sm_t:g}) -> {e.get('size')} a side {a.note}".strip()
                e["scaled_at"] = stamp
                n_scaled += 1
            sm = sm_t
            B = int(round(a.bank_size * sm)) if a.bank_size else 0
            M = int(round(a.bank_max * sm)) if a.bank_max else 0
            BASE = int(round(a.base_size * sm)) if a.base_size else 0
            for _once in (0,):         # the bank step; break (not continue) keeps the tier step below
                if bank is not None:
                    try:
                        sa_age = (now - ts(e["scaled_at"])).total_seconds() if e.get("scaled_at") else 1e9
                    except (TypeError, ValueError):
                        sa_age = 1e9
                    if sa_age < 1500:
                        # re-scaled < 25 min ago: the dashboard's rate is still the bot's share at
                        # the OLD size over the NEW listed size, so it reads every market short
                        # (12:31:57Z 11/07: 153 markets 300 -> 450 two minutes after the x1.5)
                        break
                    pk, pv = bank.get(t) or [None, 0.0]
                    banked = float(pv) if pk == (e.get("period_start") or "wave-1104") else 0.0   # the dashboard's key
                    done = banked >= a.bank_usd
                    n_banked += done
                    old = int(e.get("size") or 0)
                    pre = e.get("pre_bank") if isinstance(e.get("pre_bank"), dict) else None
                    if done and a.no_drop:
                        break               # banked: keep the size it has (Jack 2026-11-07: "dont drop any size after banking 1.3")
                    if done and not a.base_size:
                        # no --base-size: back to the entry's OWN size from before the raise
                        if pre is not None:
                            e.pop("pre_bank", None)
                            for k in ("bid_size", "ask_size"):
                                e.pop(k, None)
                            e.update({k: v for k, v in pre.items() if v is not None})
                            e["bank"] = (f"{stamp}: banked ${banked:.2f} this period -> back to its own "
                                         f"{pre.get('size')} a side {a.note}").strip()
                            n_down += 1
                        break
                    want = BASE if done else B
                    if not done and old < B and pre is None and not a.base_size:
                        e["pre_bank"] = {"size": old, "bid_size": e.get("bid_size"), "ask_size": e.get("ask_size")}
                    why = ""
                    if not done and a.hold_max and M and old >= M:
                        # --hold-max: a market at the bank max stays there for its period (Jack
                        # 2026-11-07, "increase by 1.5x again everything 1c - 70c": the bigger
                        # cheap sides earn more, and the 25%-spare step down to the bank size
                        # would cut every side of the market by a third, undoing the raise)
                        want, why = old, "at the bank max: held (--hold-max)"
                    elif not done and a.bank_max and rows:
                        x = rows.get(t)
                        rate = float((x or {}).get("rate") or 0)
                        if x and float(x.get("rest_ct") or 0) > 0 and rate > 0:
                            sz = float(x.get("size") or old or B)
                            r_bank = rate * a.bank_size / max(sz, 1.0)
                            hrs = max(0.0, float(x.get("hrs_left") or 0) - a.bank_by_hrs)
                            p_by = banked + r_bank * hrs / 24.0
                            # up when the bank size misses; back down only with 25% to spare
                            # (so a market doesn't flap between the two sizes poll to poll)
                            if p_by < a.bank_usd or (old >= M and p_by < 1.25 * a.bank_usd):
                                want = M
                                why = (f"${p_by:.2f} by {a.bank_by_hrs:g}h before the stop at {a.bank_size} "
                                       f"(${r_bank:.2f}/day)")
                        elif old > B:
                            # nothing resting, or earning $0 (outside the scoring window: size
                            # won't help) -- an already-raised market keeps its size
                            want, why = old, "nothing earning now: kept"
                    n_max += (want == M and not done and bool(a.bank_max))
                    # a side the scanner sized above the bank size (a decided side at 300) keeps its own
                    # size, as does a cheap side (--cheap-mult re-sizes it below)
                    cs = set(e.get("cheap_sides") or [])
                    sides = {k: e[k] for k in ("bid_size", "ask_size")
                             if k in e and (int(e[k]) > B or k in cs)}
                    if old != want or any(k in e and k not in sides for k in ("bid_size", "ask_size")):
                        for k in ("bid_size", "ask_size"):
                            e.pop(k, None)
                        e.update(sides)
                        e["size"] = want
                        e["bank"] = (f"{stamp}: banked ${banked:.2f} this period -> {want} a side "
                                     f"({'past' if done else 'under'} ${a.bank_usd:.2f}"
                                     f"{'; ' + why if why else ''}) {a.note}").strip()
                        if want > old:
                            n_up += 1
                        else:
                            n_down += 1
            if (tiers or stiers or side_caps or a.twin_block) and t in touch:
                # CHEAP SIDES by tier (--cheap-tiers C:X,...): a side whose fill ties up c
                # a contract -- a bid at its touch, an ask at 100 - its touch -- rests X x
                # the market's size for the first tier with c <= C (Jack 2026-11-07:
                # "double cheap sides" <= 5c, then "1.5x on size 5 to 15"); back to the
                # size once the touch leaves every tier
                yb, ya, _lp = touch[t]
                sz = int(e.get("size") or 0)
                cost = {"bid_size": yb if yb > 0 else None, "ask_size": 100 - ya if 0 < ya < 100 else None}
                mult = {}
                for k, c in cost.items():
                    if c is not None:
                        # a series' own tier first (--series-tier), then the general ones
                        for cpx, cm in stiers.get((t.split("-")[0], k), []) + tiers:
                            if c <= cpx:
                                mult[k] = cm
                                break
                # SIDE CAP (--side-cap SERIES:C:N, Jack 2026-11-07 on the House sweep: "its ok to
                # quote if it's 5c of cash held but not if its 95c of cash held"): a side of SERIES
                # whose fill ties up >= C c a contract rests N (0 = off since 1b58188). With
                # flip-through, the side that reduces a position still rests N + the position.
                capk = caps_for(t, e)
                now_c = set(mult) | set(capk)
                prev_c = set(e.get("cheap_sides") or [])
                ch = now_c != prev_c
                for k in ("bid_size", "ask_size"):
                    if k in capk:
                        want_k = int(capk[k])
                        if k not in e or int(e.get(k) or 0) != want_k:      # (a 0 is written too)
                            e[k] = want_k
                            ch = True
                    elif k in now_c:
                        want_k = int(round(sz * mult[k]))
                        if int(e.get(k) or 0) != want_k:
                            e[k] = want_k
                            ch = True
                    elif k in prev_c:
                        e.pop(k, None)          # no longer cheap: the side rests the size again
                if now_c:
                    e["cheap_sides"] = sorted(now_c)
                else:
                    e.pop("cheap_sides", None)
                if ch:
                    n_cheap += 1
                    e["cheap"] = (f"{stamp}: touch {yb:g}x{ya:g} -> "
                                  + (", ".join(f"{k.split('_')[0]} {e.get(k, e.get('size'))} "
                                               + ((f"(cap: {capwhy[k]})" if k in capwhy else f"(cap: {cost[k]:g}c a contract)")
                                                  if k in capk else f"(x{mult[k]:g})")
                                               for k in sorted(now_c)) or "none cheap")
                                  + f" {a.note}").strip()
            # PAUSE AT (--pause-at SERIES:C, Jack 2026-11-07 on the House sweep: "fine to quote
            # everything besides the 90c+ markets", "dont pause the legs that would unwind the
            # positions"): a market of SERIES whose bid or last trade is >= C (back under C - 2
            # to resume) quotes nothing -- price band 99..1 -- or, holding a position (the wave
            # dashboard's "pos"), only the side that unwinds it: "max_position" 1 +
            # "flip_to_flat", so the bot rests max(size, position) there and has no room on the
            # other side. Its own fields come back when it resumes.
            pc = pause_at.get(t.split("-")[0])
            if pc is None and isinstance(e.get("tpause"), dict):
                # its series left --pause-at: its own fields come back
                for k, v in e["tpause"]["orig"].items():
                    if v is None:
                        e.pop(k, None)
                    else:
                        e[k] = v
                e.pop("tpause", None)
                n_pause += 1
            if pc is not None:
                tp = e.get("tpause") if isinstance(e.get("tpause"), dict) else None
                mode = tp.get("mode") if tp else None
                if t in touch:
                    yb, ya, lp = touch[t]
                    # the 2c hysteresis holds only under the threshold it was paused at (Jack
                    # 2026-11-07 raised 90 -> 93: "reopen 90-92" -- a 91c market resumes at once)
                    same = bool(mode) and tp.get("pc") is not None and float(tp["pc"]) == pc
                    hot = max(yb, lp) >= (pc - 2 if same else pc)
                else:
                    yb = ya = lp = 0.0
                    hot = mode is not None          # no read: leave it as it is
                if rows:
                    pos = float((rows.get(t) or {}).get("pos") or 0)
                else:
                    pos = float((tp or {}).get("pos") or 0)
                want = None if not hot else ("unwind" if abs(pos) >= 1 else "off")
                if want and want == mode and tp.get("pc") != pc:
                    tp["pc"] = pc
                    n_pause += 1
                if want != mode:
                    if tp:
                        for k, v in tp["orig"].items():
                            if v is None:
                                e.pop(k, None)
                            else:
                                e[k] = v
                        e.pop("tpause", None)
                    if want:
                        orig = {k: e.get(k) for k in ("price_min", "price_max", "max_position", "flip_to_flat")}
                        if want == "off":
                            e["price_min"], e["price_max"] = 99, 1
                        else:
                            e["max_position"], e["flip_to_flat"] = 1, True
                        e["tpause"] = {"mode": want, "since": stamp, "pos": pos, "orig": orig, "pc": pc,
                                       "touch": f"{yb:g}x{ya:g} last {lp:g}"}
                    n_pause += 1
                    if want == "off":
                        n_pause_off += 1
                    elif want == "unwind":
                        n_pause_unw += 1
        msg = (f"{stamp} {len(series)} series since {a.since}: {n_seen} live entries, "
               f"{n_room} widened {a.room}c, {n_band} banded "
               f"{a.price_min}-{a.price_max}c" if a.price_min is not None else
               f"{stamp} {len(series)} series since {a.since}: {n_seen} live entries, {n_room} widened {a.room}c")
        if bank is not None:
            msg += (f"; bank early: {n_banked} past ${a.bank_usd:.2f} this period, "
                    f"{n_max} at {M or B}, {n_up} sized up, {n_down} sized down")
        if tiers or stiers:
            msg += (f"; cheap sides ({', '.join(f'<= {c:g}c x{m:g}' for c, m in tiers)}"
                    + "".join(f"; {ss} {sd.split('_')[0]} " + ", ".join(f"<= {c:g}c x{m:g}" for c, m in v)
                              for (ss, sd), v in sorted(stiers.items())) + f"): {n_cheap} re-sized")
        if a.flip_through:
            msg += f"; flip-through: {n_flip} set"
        if a.wave_added:
            msg += f"; wave added {a.wave_added}: {n_wadd} set"
        if fair_map is not None:
            msg += (f"; fair limits ({os.path.basename(a.fair_file)}, margin {a.fair_margin:g}c + vol err "
                    f"{a.fair_vol_err:g}, floor {a.fair_floor}c{'; ' + fair_why if fair_why else ''}): "
                    f"{n_fair} clamped, {n_fair_none} with no fair (floors only)")
        if a.no_halts:
            msg += f"; no halts: {n_nohalt} set"
        elif a.halts_on:
            msg += f"; halts on: no_halts cleared on {n_nohalt}"
        if a.follow_book:
            msg += f"; follow_book: {n_follow} set"
        if a.cushion is not None:
            msg += f"; cushion {a.cushion}: {n_cush} set"
        if a.reanchor is not None:
            msg += (f"; follow the book ({a.reanchor}c off the touch, toward the taker at most "
                    f"{a.reanchor_step}c a run, held on a {a.reanchor_jump}c+ jump): {n_anch} re-anchored, "
                    f"{n_capped} capped or held, "
                    f"{n_traded} traded (guard follows the trade), {missed} missed by the read")
        if abs(a.size_mult - 1.0) > 1e-9 or n_scaled or tmult:
            msg += f"; sizes x{a.size_mult:g}" + (f" ({len(tmult)} markets with their own multiple)" if tmult else "") \
                + f": {n_scaled} re-scaled" + tmult_why
        if pins:
            msg += f"; pinned ({', '.join(f'{k} {v}' for k, v in sorted(pins.items()))}): {n_pin} set"
        if pause_at:
            n_paused_now = sum(1 for e in (cur.get("markets") or {}).values()
                               if isinstance(e.get("tpause"), dict))
            msg += (f"; paused at ({', '.join(f'{k} {v:g}c' for k, v in sorted(pause_at.items()))}): "
                    f"{n_paused_now} paused now ({n_pause} changed: {n_pause_off} off, {n_pause_unw} unwind-only)")
        msg += bank_msg
        if a.write and (n_room or n_band or n_up or n_down or n_cush or n_anch or n_touch or n_flip or n_nohalt or n_cheap or n_follow or n_scaled or n_pin or n_pause or n_wadd or n_fair):
            cur["tune_last"] = msg          # one key, overwritten (this runs every 15 min)
            tmp = LIST + ".tune.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cur, fh, indent=1, sort_keys=True)
            os.replace(tmp, LIST)
            print("WROTE", msg)
        else:
            print(("nothing to change: " if a.write else "dry run: ") + msg)
    finally:
        if a.write and os.path.exists(LOCK):
            os.remove(LOCK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
