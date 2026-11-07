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
                     the read misses keeps its limits.

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", required=True, help="comma-separated EXACT series names, or ALL")
    ap.add_argument("--exclude-series", default="", help="comma-separated series left out (with ALL: other waves)")
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
    ap.add_argument("--cheap-mult", type=float, help="the cheap side rests this x the size")
    ap.add_argument("--cheap-px", type=float, default=5.0, help="cheap = a bid touch <= this c / an ask touch >= 100 - this c")
    ap.add_argument("--series-tier", action="append", default=[],
                    help="SERIES:SIDE:C:X -- for that series a SIDE (bid/ask) costing <= C c rests X x the size, ahead of --cheap-tiers")
    ap.add_argument("--cheap-tiers", default="", help="C:X,... -- a side costing <= C c a contract rests X x the size (first tier that fits)")
    ap.add_argument("--follow-book", action="store_true",
                    help='set "follow_book" (the bot keeps the listed limits with a last trade too) and re-anchor traded entries as well')
    ap.add_argument("--cushion", type=int)
    ap.add_argument("--reanchor", type=int)
    ap.add_argument("--note", default="")
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
    all_series = series == {"ALL"}

    def in_scope(t: str) -> bool:
        s = t.split("-")[0]
        return (all_series or s in series) and s not in exclude
    since = ts(a.since)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # the touches are read BEFORE the lock (18 calls for ~1,700 markets), so the
    # lock is held only for the read-modify-write of the list
    touch, missed = {}, 0
    if a.reanchor is not None or tiers or stiers:
        pre = json.load(open(LIST, encoding="utf-8")).get("markets") or {}
        touch, missed = read_touches([t for t in pre if in_scope(t)])
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
        n_room = n_band = n_seen = n_up = n_down = n_banked = n_cush = n_anch = n_traded = n_max = n_flip = n_nohalt = n_cheap = n_follow = n_scaled = 0
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
            if a.price_min is not None and a.price_max is not None and (
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
                    if (b1, a1) != (b0, a0):
                        e["bid_max"], e["ask_min"] = b1, a1
                        e["anchored"] = f"{stamp}: touch {yb:g}x{ya:g} -> limits {b1}/{a1} (were {b0}/{a0})"
                        n_anch += 1
            # SIZE MULTIPLIER (--size-mult, Jack 2026-11-07: "increase everything by 1.5x"):
            # an entry's sizes are scaled once to the new multiple (its size_mult key says
            # which it carries); the scanner writes a relit entry unscaled, so it is scaled
            # again here. The bank step below works in the scaled sizes.
            m_cur = float(e.get("size_mult") or 1.0)
            if abs(m_cur - a.size_mult) > 1e-9:
                f = a.size_mult / m_cur
                for k in ("size", "bid_size", "ask_size"):
                    if e.get(k):
                        e[k] = max(1, int(round(int(e[k]) * f)))
                if isinstance(e.get("pre_bank"), dict):
                    for k in ("size", "bid_size", "ask_size"):
                        if e["pre_bank"].get(k):
                            e["pre_bank"][k] = max(1, int(round(int(e["pre_bank"][k]) * f)))
                if abs(a.size_mult - 1.0) > 1e-9:
                    e["size_mult"] = a.size_mult
                else:
                    e.pop("size_mult", None)
                e["scaled"] = f"{stamp}: sizes x{f:g} (now x{a.size_mult:g}) -> {e.get('size')} a side {a.note}".strip()
                e["scaled_at"] = stamp
                n_scaled += 1
            sm = a.size_mult
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
            if (tiers or stiers) and t in touch:
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
                now_c = set(mult)
                prev_c = set(e.get("cheap_sides") or [])
                ch = now_c != prev_c
                for k in ("bid_size", "ask_size"):
                    if k in now_c:
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
                                  + (", ".join(f"{k.split('_')[0]} {e[k]} (x{mult[k]:g})" for k in sorted(now_c)) or "none cheap")
                                  + f" {a.note}").strip()
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
        if a.no_halts:
            msg += f"; no halts: {n_nohalt} set"
        if a.follow_book:
            msg += f"; follow_book: {n_follow} set"
        if a.cushion is not None:
            msg += f"; cushion {a.cushion}: {n_cush} set"
        if a.reanchor is not None:
            msg += (f"; follow the book ({a.reanchor}c off the touch): {n_anch} re-anchored, "
                    f"{n_traded} traded (guard follows the trade), {missed} missed by the read")
        if abs(a.size_mult - 1.0) > 1e-9 or n_scaled:
            msg += f"; sizes x{a.size_mult:g}: {n_scaled} re-scaled"
        msg += bank_msg
        if a.write and (n_room or n_band or n_up or n_down or n_cush or n_anch or n_flip or n_nohalt or n_cheap or n_follow or n_scaled):
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
