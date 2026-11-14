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
                 exempt: Optional[Callable[[str], Optional[str]]] = None):
        self.day_end = day_end
        self.flash_max_secs = float(flash_max_secs)
        self.min_c = float(min_c)
        self.window_secs = float(window_secs)
        self.pulls = max(1, int(pulls))
        self.hold_secs = float(hold_secs)
        self.print_grace_secs = float(print_grace_secs)
        self.exempt = exempt or (lambda ticker: None)
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
        self.stats = {"flashes": 0, "stood": 0, "taken": 0, "pulls": 0,
                      "trips": 0, "fills": 0, "actors": 0}

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
        `own_best` is our best resting price on that side (own cents), for
        the `joined` flag. Returns actor rows."""
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
                               "size_hi": ref_q, "added": max(0.0, added)}
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
            self.pending.append({
                "t": t, "ev": ev, "side": side, "base": fl["base"],
                "px_hi": fl["px_hi"], "size_hi": fl["size_hi"],
                "added": fl["added"], "t0": fl["t0"], "t1": now,
                "joined": own_best is not None and own_best >= fl["px_hi"] - EPS,
                "after": ref})
        return out

    def note_fill(self, t: str, ev: str, side: str, yes_px: float, n: float,
                  ts: float, order_id: str) -> List[dict]:
        """One of our maker fills: a `fill` row when a window holds it."""
        mk, ek = "m|" + t, "e|" + ev
        w = self.windows.get(mk) if self._window_open(mk, ts) else None
        scope = "market"
        if w is None:
            w = self.windows.get(ek) if self._window_open(ek, ts) else None
            scope = "event"
        if w is None:
            return []
        self.stats["fills"] += 1
        w["fills"] = w.get("fills", 0) + 1
        w["fill_ct"] = w.get("fill_ct", 0.0) + float(n)
        side_pulled = any(p["side"] == side for p in self.pulls_mkt.get(t, ()))
        return [{"ev": "fill", "ts": round(ts, 3), "ticker": t, "event": ev,
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
                out.extend(self._pull(p))
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
        """A print hitting that side at a price better than the base, while
        the better price stood (a slack of a second or two either side: the
        tape rides its own connection)."""
        lo, hi = p["t0"] - 2.0, p["t1"] + self.print_grace_secs
        hits = "no" if p["side"] == "bid" else "yes"
        for ts, tk, ypx, _n in self.prints.get(p["t"], ()):
            if not (lo <= ts <= hi) or tk != hits:
                continue
            own = ypx if p["side"] == "bid" else 100.0 - ypx
            if p["base"] is None or own >= p["base"] + self.min_c - EPS:
                return True
        return False

    def _window_open(self, key: str, ts: float) -> bool:
        w = self.windows.get(key)
        return w is not None and w["until"] > ts

    def _pull(self, p: dict) -> List[dict]:
        self.stats["pulls"] += 1
        t, ev, ts = p["t"], p["ev"], p["t1"]
        ex = self.exempt(t)

        def yes(px: Optional[float]) -> Optional[float]:
            if px is None:
                return None
            return round(px if p["side"] == "bid" else 100.0 - px, 2)
        rec = {"ticker": t, "side": p["side"], "ts": round(ts, 3),
               "base": yes(p["base"]), "px": yes(p["px_hi"]),
               "after": yes(p["after"]),
               "gain_c": None if p["base"] is None
               else round(p["px_hi"] - p["base"], 2),
               "life_s": round(ts - p["t0"], 1), "size": round(p["size_hi"], 2),
               "added": round(p["added"], 2), "joined": p["joined"]}
        dm = self.pulls_mkt.setdefault(t, deque())
        de = self.pulls_ev.setdefault(ev, deque())
        dm.append(rec)
        de.append(rec)
        for dq in (dm, de):
            while dq and dq[0]["ts"] < ts - self.window_secs:
                dq.popleft()
        if p["added"] > 0:
            self.sizes.setdefault(ev, {})[round(p["added"], 2)] = ts
        out = [dict(rec, ev="pull", event=ev, n_market=len(dm), n_event=len(de),
                    exempt=ex)]
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
                "pulls": [{k: x[k] for k in ("ticker", "side", "ts", "base", "px",
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

    # ---- persistence (imm_state.json) ---------------------------------------

    def payload(self, now: float) -> dict:
        """What a restart keeps: open windows, today's trip counts, the pulls
        still inside the window, the remembered sizes."""
        return {
            "windows": {k: dict(w) for k, w in self.windows.items()
                        if w["until"] > now},
            "trips_day": {k: [round(e, 1), n] for k, (e, n) in self.trips_day.items()
                          if e > now},
            "pulls_mkt": {k: list(dq) for k, dq in self.pulls_mkt.items()
                          if dq and dq[-1]["ts"] >= now - self.window_secs},
            "pulls_ev": {k: list(dq) for k, dq in self.pulls_ev.items()
                         if dq and dq[-1]["ts"] >= now - self.window_secs},
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
            for name in ("pulls_mkt", "pulls_ev"):
                d = getattr(self, name)
                for k, lst in (data.get(name) or {}).items():
                    rows = [dict(r) for r in lst if isinstance(r, dict)
                            and float(r.get("ts") or 0) >= now - self.window_secs]
                    if rows:
                        d[str(k)] = deque(rows)
            for ev, s in (data.get("sizes") or {}).items():
                self.sizes[str(ev)] = {round(float(k), 2): float(v) for k, v in s.items()}
        except (TypeError, ValueError, IndexError, AttributeError):
            pass
