#!/usr/bin/env python3
"""HEIGHTENED BAIT MODE -- the detector, run DRY (Jack 2026-11-13: "anytime
there is suspicious activity e.g. show better price and pull 3 times, move to
'heightened awareness bait mode' and increase vigilance on the event and
market", then "Build it dry").

What it watches. Kalshi's book feed shows price levels, not orders, so a PULL
is read off the book: on one side of a market (our own orders netted out),
the EXTERNAL REFERENCE -- the first level, best price first, where the size
reaches target/5, the price an at-ref rung follows -- improves by at least
MIN_C, and then, within FLASH_MAX_SECS, goes back to where it was (or worse)
with no print on the public tape at the better price. A better price that
stands longer is a reprice (it becomes the new base); one that traded was
taken, not pulled.

Why the filters. A bare count of "the better price went away" (join wait's
`gone` rows, 11/04-11/14) reaches 3 in 30 min on one market side ~63 times a
day across 341 market sides -- mostly the Carbon Arc arrays and
KXVOTEGENERAL, other makers repricing; such a price lasted 20 s at the
median, 99 s at p90. Counting only reference moves that revert untraded is
the narrower signal.

NARROWED (Jack 2026-11-14: "narrow the trips"). Live 02:02-02:43Z 11/14
every pull counted: 3,119 pulls, ~18,500 trips a day -- 80% the spot-priced
*EVENT strikes (FX, metals, crypto), the rest mostly target/5 flickers of
other makers' bots. A pull now COUNTS only when it is
  * outside the exempt series (the bot's: mention books, crypto, *EVENT),
  * count_min_c (10c) or more over the base, or from no reference at all,
  * stood min_life_secs (10 s) or more -- our rung moves on the cycle, so a
    shorter one cannot drag it,
  * FOLLOWED: our rung rested at the better price as it went away and had
    not been there when it came (the probe that maps our reaction).
On that sample (with a looser follow test) this left ~210 trips a day.
Our own fills are netted out of the "was it taken" test, so a bait pulled
while a taker hits us is still a pull, and a fill of ours at a price better
than the base within strike_secs after (or during) a counted pull is logged
as a `strike` -- the hit the probes set up. Uncounted pulls are counters in
the stats, not rows.

The trip. PULLS pulls inside WINDOW_SECS on one market (either side) open a
MARKET window; PULLS across one event's markets open an EVENT window. A
window runs HOLD_SECS past its last pull; a second trip of the same key on
the same ET day runs it to ET midnight. While a window is open:
  * our maker fills in its market / event are logged (`fill`), for the
    scorer to mark out against the reward the windows hold;
  * the sizes of the pulled levels are remembered, and a level of the same
    size appearing anywhere in the event is logged (`actor`): Kalshi shows
    no trader IDs, but bait lots repeat (Paris's ~10,456 came back on the
    other side six minutes later).

This module is pure: no I/O, no clock of its own. The bot feeds it books,
prints and fills and writes what it returns to its `heightened` sink. Nothing
here changes a quote: "on" (the actions -- no following, base size, the
lone-ref guard on, a 2c fast cancel, a side out after a fill) is not built.

PRE-REGISTERED GO / NO-GO (imm_heightened_report.py, after 7+ days dry, the
exempt series left out):
  GO     our fills inside windows lost at least the reward the windows held
         (4h mark-outs), and at least $5/day: even standing fully aside
         would have paid, so build the live actions as an A/B;
  WATCH  they lost 25-100% of it: the partial actions might still pay;
  STOP   under 25%, or more than 30 trips a day (too noisy to act on).
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

EPS = 1e-6


def side_ref(levels: Dict[float, float], target: float) -> Tuple[Optional[float], float]:
    """(reference price, size there) of one side, prices in the side's own
    cents (higher = better): the first level, best first, where the
    cumulative size reaches target/5. (None, 0) when it never does."""
    if target <= 0:
        return None, 0.0
    need = target / 5.0
    cum = 0.0
    for px in sorted(levels, reverse=True):
        q = levels[px]
        if q <= EPS:
            continue
        cum += q
        if cum + EPS >= need:
            return px, q
    return None, 0.0


class HeightenedWatch:
    """The pull counter, its windows and the actor sizes (see the module
    docstring). Keys: a side is (ticker, "bid" | "ask"); a window is
    "m|<ticker>" or "e|<event>". Every method returns the rows to log."""

    def __init__(self, day_end: Callable[[float], float],
                 flash_max_secs: float = 120.0, min_c: float = 1.0,
                 window_secs: float = 1800.0, pulls: int = 3,
                 hold_secs: float = 3600.0, print_grace_secs: float = 3.0,
                 exempt: Optional[Callable[[str], Optional[str]]] = None,
                 count_min_c: float = 10.0, min_life_secs: float = 10.0,
                 followed_only: bool = True, strike_secs: float = 120.0):
        self.day_end = day_end
        self.flash_max_secs = float(flash_max_secs)
        self.min_c = float(min_c)
        self.window_secs = float(window_secs)
        self.pulls = max(1, int(pulls))
        self.hold_secs = float(hold_secs)
        self.print_grace_secs = float(print_grace_secs)
        self.exempt = exempt or (lambda ticker: None)
        # which pulls COUNT toward a trip (the narrowed trigger, 2026-11-14)
        self.count_min_c = float(count_min_c)
        self.min_life_secs = float(min_life_secs)
        self.followed_only = bool(followed_only)
        self.strike_secs = float(strike_secs)
        # (t, side) -> deque[(ts, own px, n, order id)]: our recent fills
        self.own_fills: Dict[Tuple[str, str], Deque[Tuple[float, float, float, str]]] = {}
        # (t, side) -> deque[counted pull]: for strikes after the pull
        self.counted_recent: Dict[Tuple[str, str], Deque[dict]] = {}
        # (t, side) -> {"base", "flash", "levels", "seen"}
        self.sides: Dict[Tuple[str, str], dict] = {}
        # t -> deque[(ts, taker_side, yes_px, n)]
        self.prints: Dict[str, Deque[Tuple[float, str, float, float]]] = {}
        self.pending: List[dict] = []          # reverted, waiting on the tape
        self.pulls_mkt: Dict[str, Deque[dict]] = {}
        self.pulls_ev: Dict[str, Deque[dict]] = {}
        self.windows: Dict[str, dict] = {}
        self.trips_day: Dict[str, Tuple[float, int]] = {}  # key -> (day end, trips)
        self.sizes: Dict[str, Dict[float, float]] = {}     # event -> {size: ts}
        self.actor_seen: Dict[Tuple[str, str, float, float], float] = {}
        self.stats = {"flashes": 0, "stood": 0, "taken": 0, "raw_pulls": 0,
                      "pulls": 0, "unc_exempt": 0, "unc_small": 0, "unc_brief": 0,
                      "unc_unfollowed": 0, "trips": 0, "fills": 0, "strikes": 0,
                      "actors": 0}

    # ---- inputs -------------------------------------------------------------

    def note_print(self, t: str, taker_side: str, yes_px: float, n: float,
                   ts: float) -> None:
        """One public print (block trades left out by the caller)."""
        if taker_side not in ("yes", "no") or n <= 0:
            return
        dq = self.prints.get(t)
        if dq is None:
            dq = self.prints[t] = deque(maxlen=400)
        dq.append((float(ts), taker_side, float(yes_px), float(n)))

    def observe(self, t: str, ev: str, side: str, levels: Dict[float, float],
                target: float, now: float, own_best: Optional[float] = None
                ) -> List[dict]:
        """One side's external book (own cents -> size, our orders netted
        out) after a change; the watch keeps the dict, so pass a fresh one.
        `own_best` is our best resting price on that side (own cents): a
        pull is FOLLOWED when our rung rested at the better price as it went
        away and had not been there when it came. Returns actor rows."""
        key = (t, side)
        st = self.sides.get(key)
        if st is not None and levels == st["levels"]:
            st["seen"] = now            # the other side of the book changed
            return []
        ref, ref_q = side_ref(levels, target)
        out: List[dict] = []
        if st is None:
            self.sides[key] = {"base": ref, "flash": None, "levels": levels,
                               "seen": now}
            return out
        prev_levels = st["levels"]
        st["seen"] = now
        if ev in self.sizes and self._window_open("e|" + ev, now):
            out.extend(self._actor_check(t, ev, side, prev_levels, levels, now))
        st["levels"] = levels
        base, fl = st["base"], st["flash"]
        if fl is None:
            if ref is not None and (base is None or ref >= base + self.min_c - EPS):
                self.stats["flashes"] += 1
                added = ref_q - prev_levels.get(ref, 0.0)
                st["flash"] = {"t0": now, "base": base, "px": ref, "px_hi": ref,
                               "size_hi": ref_q, "added": max(0.0, added),
                               "own0": own_best}
            else:
                st["base"] = ref
            return out
        improved = ref is not None and (fl["base"] is None or ref > fl["base"] + EPS)
        age = now - fl["t0"]
        if improved:
            if age > self.flash_max_secs:
                self.stats["stood"] += 1              # a reprice: the new base
                st["base"], st["flash"] = ref, None
            else:
                fl["px"] = ref
                if ref > fl["px_hi"] + EPS:
                    fl["px_hi"], fl["size_hi"] = ref, ref_q
                    fl["added"] = max(0.0, ref_q - prev_levels.get(ref, 0.0))
            return out
        # back to the base or worse
        st["base"], st["flash"] = ref, None
        if age <= self.flash_max_secs:
            there = own_best is not None and own_best >= fl["px_hi"] - EPS
            own0 = fl.get("own0")
            self.pending.append({
                "t": t, "ev": ev, "side": side, "base": fl["base"],
                "px_hi": fl["px_hi"], "size_hi": fl["size_hi"],
                "added": fl["added"], "t0": fl["t0"], "t1": now,
                "joined": there,
                "followed": there and (own0 is None or own0 < fl["px_hi"] - EPS),
                "after": ref})
        return out

    def note_fill(self, t: str, ev: str, side: str, yes_px: float, n: float,
                  ts: float, order_id: str) -> List[dict]:
        """One of our maker fills: kept a few minutes (a better price that
        "traded" only into us was still pulled), a `strike` row when it came
        within strike_secs after a counted pull on that side at a price
        better than the pull's base, and a `fill` row when a window holds
        it."""
        out: List[dict] = []
        own = float(yes_px) if side == "bid" else 100.0 - float(yes_px)
        key = (t, side)
        if side in ("bid", "ask"):
            self.own_fills.setdefault(key, deque(maxlen=200)).append(
                (float(ts), own, float(n), order_id))
            for p in self.counted_recent.get(key, ()):
                if 0.0 <= ts - p["ts"] <= self.strike_secs and \
                        (p["_base"] is None or own >= p["_base"] + self.min_c - EPS):
                    out.append(self._strike(p, ts, yes_px, n, order_id))
        mk, ek = "m|" + t, "e|" + ev
        w = self.windows.get(mk) if self._window_open(mk, ts) else None
        scope = "market"
        if w is None:
            w = self.windows.get(ek) if self._window_open(ek, ts) else None
            scope = "event"
        if w is None:
            return out
        self.stats["fills"] += 1
        w["fills"] = w.get("fills", 0) + 1
        w["fill_ct"] = w.get("fill_ct", 0.0) + float(n)
        side_pulled = any(p["side"] == side for p in self.pulls_mkt.get(t, ()))
        return out + [{"ev": "fill", "ts": round(ts, 3), "ticker": t, "event": ev,
                 "side": side, "px": yes_px, "count": round(float(n), 2),
                 "order_id": order_id, "scope": scope, "key": w["key"],
                 "trip_ts": round(w["since"], 3),
                 "since_trip_s": round(ts - w["since"], 1),
                 "side_pulled": side_pulled, "exempt": w.get("exempt")}]

    # ---- the clock ----------------------------------------------------------

    def tick(self, now: float) -> List[dict]:
        """Settle the reverted flashes whose tape grace is over (a pull, or a
        take), trip windows, close the ones that ran out; prune."""
        out: List[dict] = []
        if self.pending:
            keep = []
            for p in self.pending:
                if now - p["t1"] < self.print_grace_secs:
                    keep.append(p)
                    continue
                if self._traded(p):
                    self.stats["taken"] += 1
                    continue
                out.extend(self._pull(p, now))
            self.pending = keep
        for k in [k for k, w in self.windows.items() if w["until"] <= now]:
            w = self.windows.pop(k)
            out.append({"ev": "clear", "ts": round(now, 3), "key": k,
                        "scope": w["scope"], "ticker": w.get("ticker"),
                        "event": w["event"], "since": round(w["since"], 3),
                        "until": round(w["until"], 3),
                        "dur_min": round((w["until"] - w["since"]) / 60.0, 1),
                        "pulls": w.get("pulls", 0), "fills": w.get("fills", 0),
                        "fill_ct": round(w.get("fill_ct", 0.0), 2),
                        "exempt": w.get("exempt")})
        self._prune(now)
        return out

    # ---- internals ----------------------------------------------------------

    def _traded(self, p: dict) -> bool:
        """Did the better price trade? The prints hitting that side at a
        price better than the base while it stood (a slack of a second or two
        either side: the tape rides its own connection), less our own fills
        there, must reach half the size that came: a bait pulled while a
        taker hits us (and the other followers) is still a pull."""
        lo, hi = p["t0"] - 2.0, p["t1"] + self.print_grace_secs
        hits = "no" if p["side"] == "bid" else "yes"

        def better(own: float) -> bool:
            return p["base"] is None or own >= p["base"] + self.min_c - EPS
        printed = 0.0
        for ts, tk, ypx, n in self.prints.get(p["t"], ()):
            if lo <= ts <= hi and tk == hits and \
                    better(ypx if p["side"] == "bid" else 100.0 - ypx):
                printed += n
        if printed <= 0:
            return False
        mine = sum(n for ts, own, n, _oid in self.own_fills.get((p["t"], p["side"]), ())
                   if lo <= ts <= hi and better(own))
        return printed - mine >= max(1.0, 0.5 * p["added"])

    def _window_open(self, key: str, ts: float) -> bool:
        w = self.windows.get(key)
        return w is not None and w["until"] > ts

    def _pull(self, p: dict, now: float) -> List[dict]:
        """A better price pulled. It COUNTS toward a trip only outside the
        exempt series, at count_min_c+ over the base (or from no reference),
        having stood min_life_secs+ (our rung moves on the cycle, so a
        shorter one cannot drag it), and -- followed_only -- followed by our
        rung. The rest are counted in the stats, not logged."""
        self.stats["raw_pulls"] += 1
        t, ev, ts = p["t"], p["ev"], p["t1"]
        ex = self.exempt(t)
        gain = None if p["base"] is None else p["px_hi"] - p["base"]
        if ex:
            self.stats["unc_exempt"] += 1
            return []
        if gain is not None and gain < self.count_min_c - EPS:
            self.stats["unc_small"] += 1
            return []
        if ts - p["t0"] < self.min_life_secs - EPS:
            self.stats["unc_brief"] += 1
            return []
        if self.followed_only and not p.get("followed"):
            self.stats["unc_unfollowed"] += 1
            return []
        self.stats["pulls"] += 1

        def yes(px: Optional[float]) -> Optional[float]:
            if px is None:
                return None
            return round(px if p["side"] == "bid" else 100.0 - px, 2)
        rec = {"ticker": t, "event": ev, "side": p["side"], "ts": round(ts, 3),
               "base": yes(p["base"]), "px": yes(p["px_hi"]),
               "after": yes(p["after"]),
               "gain_c": None if p["base"] is None
               else round(p["px_hi"] - p["base"], 2),
               "life_s": round(ts - p["t0"], 1), "size": round(p["size_hi"], 2),
               "added": round(p["added"], 2), "joined": p["joined"],
               "followed": bool(p.get("followed"))}
        dm = self.pulls_mkt.setdefault(t, deque())
        de = self.pulls_ev.setdefault(ev, deque())
        dm.append(rec)
        de.append(rec)
        for dq in (dm, de):
            while dq and dq[0]["ts"] < ts - self.window_secs:
                dq.popleft()
        if p["added"] > 0:
            self.sizes.setdefault(ev, {})[round(p["added"], 2)] = ts
        out = [dict(rec, ev="pull", n_market=len(dm), n_event=len(de), exempt=ex)]
        # strikes: our fills on that side, at a price better than the base,
        # from the better price's start to now; later ones come via note_fill
        cr = dict(rec, _base=p["base"])
        for f_ts, own, n, oid in self.own_fills.get((t, p["side"]), ()):
            if p["t0"] - 2.0 <= f_ts <= now and \
                    (p["base"] is None or own >= p["base"] + self.min_c - EPS):
                ypx = own if p["side"] == "bid" else 100.0 - own
                out.append(self._strike(cr, f_ts, ypx, n, oid))
        self.counted_recent.setdefault((t, p["side"]), deque(maxlen=20)).append(cr)
        for key, scope, dq in (("m|" + t, "market", dm), ("e|" + ev, "event", de)):
            w = self.windows.get(key)
            if w is not None and w["until"] > ts:
                w["until"] = max(w["until"], ts + self.hold_secs)
                w["pulls"] = w.get("pulls", 0) + 1
                continue
            if len(dq) < self.pulls:
                continue
            out.append(self._trip(key, scope, t, ev, list(dq)[-self.pulls:], ts, ex))
        return out

    def _strike(self, p: dict, ts: float, yes_px: float, n: float,
                order_id: str) -> dict:
        """One fill of ours after (or during) a counted pull on its side: the
        bait's hit."""
        self.stats["strikes"] += 1
        return {"ev": "strike", "ts": round(float(ts), 3), "ticker": p["ticker"],
                "event": p["event"], "side": p["side"], "px": round(float(yes_px), 2),
                "count": round(float(n), 2), "order_id": order_id,
                "pull_ts": p["ts"], "since_pull_s": round(float(ts) - p["ts"], 1),
                "pull_px": p["px"], "pull_base": p["base"]}

    def _trip(self, key: str, scope: str, t: str, ev: str, pulls: List[dict],
              ts: float, ex: Optional[str]) -> dict:
        self.stats["trips"] += 1
        d_end = self.day_end(ts)
        prev_end, n = self.trips_day.get(key, (0.0, 0))
        n = n + 1 if abs(prev_end - d_end) < 1.0 else 1
        self.trips_day[key] = (d_end, n)
        until = ts + self.hold_secs
        if n >= 2:
            until = max(until, d_end)          # a second trip today: to midnight
        self.windows[key] = {"key": key, "scope": scope, "event": ev,
                             "ticker": t if scope == "market" else None,
                             "since": ts, "until": until, "pulls": len(pulls),
                             "fills": 0, "fill_ct": 0.0, "exempt": ex}
        return {"ev": "trip", "ts": round(ts, 3), "key": key, "scope": scope,
                "ticker": t, "event": ev, "until": round(until, 3),
                "trips_today": n, "to_midnight": n >= 2, "exempt": ex,
                "markets": sorted({x["ticker"] for x in pulls}),
                "pulls": [{k: x.get(k) for k in ("ticker", "side", "ts", "base", "px",
                                                 "life_s", "size", "added", "joined")}
                          for x in pulls]}

    def _actor_check(self, t: str, ev: str, side: str,
                     prev: Dict[float, float], cur: Dict[float, float],
                     now: float) -> List[dict]:
        """A level that grew by exactly a remembered pulled size."""
        sizes = self.sizes.get(ev) or {}
        out = []
        for px, q in cur.items():
            add = round(q - prev.get(px, 0.0), 2)
            if add <= 0 or add not in sizes:
                continue
            k = (t, side, px, add)
            if now - self.actor_seen.get(k, -1e18) < 600.0:
                continue
            self.actor_seen[k] = now
            self.stats["actors"] += 1
            out.append({"ev": "actor", "ts": round(now, 3), "ticker": t,
                        "event": ev, "side": side,
                        "px": round(px if side == "bid" else 100.0 - px, 2),
                        "size": add, "first_seen_ts": round(sizes[add], 3)})
        return out

    def _prune(self, now: float) -> None:
        for t in [t for t, dq in self.prints.items()
                  if not dq or dq[-1][0] < now - 300.0]:
            del self.prints[t]
        for t, dq in list(self.prints.items()):
            while dq and dq[0][0] < now - 300.0:
                dq.popleft()
        for d in (self.pulls_mkt, self.pulls_ev):
            for k in [k for k, dq in d.items()
                      if not dq or dq[-1]["ts"] < now - self.window_secs]:
                del d[k]
        for k in [k for k, st in self.sides.items() if st["seen"] < now - 3600.0]:
            del self.sides[k]
        for k in [k for k, (end, _n) in self.trips_day.items() if end < now]:
            del self.trips_day[k]
        for ev in [ev for ev, s in self.sizes.items()
                   if max(s.values()) < now - 86400.0]:
            del self.sizes[ev]
        for k in [k for k, v in self.actor_seen.items() if v < now - 600.0]:
            del self.actor_seen[k]
        for k in [k for k, dq in self.own_fills.items()
                  if not dq or dq[-1][0] < now - 300.0]:
            del self.own_fills[k]
        for k in [k for k, dq in self.counted_recent.items()
                  if not dq or dq[-1]["ts"] < now - self.strike_secs]:
            del self.counted_recent[k]

    # ---- persistence (imm_state.json) ---------------------------------------

    def payload(self, now: float) -> dict:
        """What a restart keeps: open windows, today's trip counts, the pulls
        still inside the window, the remembered sizes."""
        return {
            "windows": {k: dict(w) for k, w in self.windows.items()
                        if w["until"] > now},
            "trips_day": {k: [round(e, 1), n] for k, (e, n) in self.trips_day.items()
                          if e > now},
            # each pull once, as a compact row (the event lists are rebuilt
            # from it): [ts, ticker, event, side, base, px, life_s, size,
            # added, joined]
            "pulls": [[r["ts"], r["ticker"], r.get("event", ""), r["side"],
                       r.get("base"), r.get("px"), r.get("life_s"), r.get("size"),
                       r.get("added"), bool(r.get("joined"))]
                      for dq in self.pulls_mkt.values() for r in dq
                      if r["ts"] >= now - self.window_secs],
            "sizes": {ev: {str(s): round(v, 1) for s, v in d.items()}
                      for ev, d in self.sizes.items()
                      if d and max(d.values()) >= now - 86400.0},
        }

    def load(self, data: Optional[dict], now: float) -> None:
        """Adopt a payload; anything malformed is dropped, never raised."""
        if not isinstance(data, dict):
            return
        try:
            for k, w in (data.get("windows") or {}).items():
                if isinstance(w, dict) and float(w.get("until") or 0) > now:
                    self.windows[str(k)] = dict(w)
            for k, v in (data.get("trips_day") or {}).items():
                if float(v[0]) > now:
                    self.trips_day[str(k)] = (float(v[0]), int(v[1]))
            pulls = []
            for row in data.get("pulls") or []:
                if isinstance(row, list) and len(row) >= 10 and                         float(row[0]) >= now - self.window_secs:
                    pulls.append(dict(zip(
                        ("ts", "ticker", "event", "side", "base", "px", "life_s",
                         "size", "added", "joined"), row)))
            # the first build's payload kept whole rows per market and event
            for lst in (data.get("pulls_mkt") or {}).values():
                for r in lst:
                    if isinstance(r, dict) and float(r.get("ts") or 0) >=                             now - self.window_secs:
                        r = dict(r)
                        r.setdefault("event", str(r.get("ticker") or "").rsplit("-", 1)[0])
                        pulls.append(r)
            for r in sorted(pulls, key=lambda r: float(r["ts"])):
                self.pulls_mkt.setdefault(str(r["ticker"]), deque()).append(r)
                self.pulls_ev.setdefault(str(r["event"]), deque()).append(r)
            for ev, s in (data.get("sizes") or {}).items():
                self.sizes[str(ev)] = {round(float(k), 2): float(v) for k, v in s.items()}
        except (TypeError, ValueError, IndexError, AttributeError):
            pass
