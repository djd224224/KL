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
                     as they are.
  --bank-url/--bank-max/--bank-by-hrs
                     ...and where --bank-size won't get there in time, --bank-max: read
                     the dashboard's /data rows (banked this period, live $/day at the
                     size resting, hours to the stop); an unbanked market goes to
                     --bank-max when banked + rate x (bank-size / size now) x
                     (hours left - bank-by-hrs) / 24 < --bank-usd, and stays there
                     until it banks (no flapping). A market with nothing resting keeps
                     its size. /data replaces --bank-hist when given.
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
    ap.add_argument("--series", required=True, help="comma-separated EXACT series names")
    ap.add_argument("--since", required=True, help="ISO: only entries whose period_start is at or after this")
    ap.add_argument("--room", type=int, default=0)
    ap.add_argument("--price-min", type=int)
    ap.add_argument("--price-max", type=int)
    ap.add_argument("--bank-size", type=int)
    ap.add_argument("--base-size", type=int)
    ap.add_argument("--bank-usd", type=float, default=1.30)
    ap.add_argument("--bank-hist", help="imm_pvd_watch history.json of the wave's dashboard")
    ap.add_argument("--bank-stale-min", type=float, default=30.0)
    ap.add_argument("--bank-url", help="the wave dashboard's /data, e.g. http://127.0.0.1:8773/data")
    ap.add_argument("--bank-max", type=int)
    ap.add_argument("--bank-by-hrs", type=float, default=6.0)
    ap.add_argument("--cushion", type=int)
    ap.add_argument("--reanchor", type=int)
    ap.add_argument("--note", default="")
    ap.add_argument("--wait", type=int, default=120)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    series = {s.strip() for s in a.series.split(",") if s.strip()}
    since = ts(a.since)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # the touches are read BEFORE the lock (18 calls for ~1,700 markets), so the
    # lock is held only for the read-modify-write of the list
    touch, missed = {}, 0
    if a.reanchor is not None:
        pre = json.load(open(LIST, encoding="utf-8")).get("markets") or {}
        touch, missed = read_touches([t for t in pre if t.split("-")[0] in series])
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
    if a.bank_size and a.base_size and a.bank_url:
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
    elif a.bank_size and a.base_size and a.bank_hist:
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
        n_room = n_band = n_seen = n_up = n_down = n_banked = n_cush = n_anch = n_traded = n_max = 0
        for t, e in (cur.get("markets") or {}).items():
            if t.split("-")[0] not in series:
                continue
            try:
                if ts(e["stop"]) <= now or ts(e.get("period_start") or "1970-01-01T00:00:00Z") < since:
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
            if a.cushion is not None and e.get("cushion") != a.cushion:
                e["cushion_set"] = f"{stamp}: cushion {e.get('cushion')} -> {a.cushion} {a.note}".strip()
                e["cushion"] = a.cushion
                n_cush += 1
            if a.reanchor is not None and t in touch:
                yb, ya, lp = touch[t]
                if lp > 0:
                    n_traded += 1           # the guard follows the last trade; bid_max / ask_min unused
                else:
                    b0, a0 = int(e["bid_max"]), int(e["ask_min"])
                    b1 = min(99, int(math.floor(yb)) + a.reanchor) if yb > 0 else b0
                    a1 = max(1, int(math.ceil(ya)) - a.reanchor) if 0 < ya < 100 else a0
                    if (b1, a1) != (b0, a0):
                        e["bid_max"], e["ask_min"] = b1, a1
                        e["anchored"] = f"{stamp}: touch {yb:g}x{ya:g} -> limits {b1}/{a1} (were {b0}/{a0})"
                        n_anch += 1
            if bank is not None:
                pk, pv = bank.get(t) or [None, 0.0]
                banked = float(pv) if pk == e.get("period_start") else 0.0
                done = banked >= a.bank_usd
                n_banked += done
                old = int(e.get("size") or 0)
                want = a.base_size if done else a.bank_size
                why = ""
                if not done and a.bank_max and rows:
                    x = rows.get(t)
                    rate = float((x or {}).get("rate") or 0)
                    if x and float(x.get("rest_ct") or 0) > 0 and rate > 0:
                        sz = float(x.get("size") or old or a.bank_size)
                        r_bank = rate * a.bank_size / max(sz, 1.0)
                        hrs = max(0.0, float(x.get("hrs_left") or 0) - a.bank_by_hrs)
                        p_by = banked + r_bank * hrs / 24.0
                        # up when the bank size misses; back down only with 25% to spare
                        # (so a market doesn't flap between the two sizes poll to poll)
                        if p_by < a.bank_usd or (old >= a.bank_max and p_by < 1.25 * a.bank_usd):
                            want = a.bank_max
                            why = (f"${p_by:.2f} by {a.bank_by_hrs:g}h before the stop at {a.bank_size} "
                                   f"(${r_bank:.2f}/day)")
                    elif old > a.bank_size:
                        # nothing resting, or earning $0 (outside the scoring window: size
                        # won't help) -- an already-raised market keeps its size
                        want, why = old, "nothing earning now: kept"
                n_max += (want == a.bank_max and not done and bool(a.bank_max))
                # a side the scanner sized above the bank size (a decided side at 300) keeps its own size
                sides = {k: e[k] for k in ("bid_size", "ask_size") if k in e and int(e[k]) > a.bank_size}
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
        msg = (f"{stamp} {len(series)} series since {a.since}: {n_seen} live entries, "
               f"{n_room} widened {a.room}c, {n_band} banded "
               f"{a.price_min}-{a.price_max}c" if a.price_min is not None else
               f"{stamp} {len(series)} series since {a.since}: {n_seen} live entries, {n_room} widened {a.room}c")
        if bank is not None:
            msg += (f"; bank early: {n_banked} past ${a.bank_usd:.2f} this period, "
                    f"{n_max} at {a.bank_max or a.bank_size}, {n_up} sized up, {n_down} sized down")
        if a.cushion is not None:
            msg += f"; cushion {a.cushion}: {n_cush} set"
        if a.reanchor is not None:
            msg += (f"; follow the book ({a.reanchor}c off the touch): {n_anch} re-anchored, "
                    f"{n_traded} traded (guard follows the trade), {missed} missed by the read")
        msg += bank_msg
        if a.write and (n_room or n_band or n_up or n_down or n_cush or n_anch):
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
