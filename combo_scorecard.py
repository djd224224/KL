#!/usr/bin/env python3
r"""combo_scorecard.py -- the whole-tape scorecard (Jack 2026-11-06, "Yes, build
both", after the desk review: score edge on the whole market every day, not on
our own few dozen fills).

For one UTC day of combo prints -- the RFQ logger's exec records: every print it
could tie to an RFQ in its slice (1 in 50 of them), with the legs -- on
cross-game main-line parlays (no two legs on one game, every leg a main line),
priced from the legs' Kalshi mids at the print:

  WHERE IT CLEARS  the print over the product of the legs' Kalshi mids, by leg
                   count, against what the maker fee costs;
  OVER THE CLOSE   the print over the product of the legs' last mids before
                   their starts (the closing line): + = the makers sold above
                   the close, the buyers paid up;
  SETTLED          what the parlays paid against their premium and what the
                   close said they would pay;
  OUR PRICE        at each markup over our fair (the live maker's: the highest
                   of Kalshi / DraftKings / Polymarket per leg), the prints we'd
                   have undercut and our margin over the close after the fee on
                   them -- what a markup would have earned, first order (no
                   reply from the other makers), and the live rule's own row;
  REQUESTERS       the most active buyers, and the sharpest: their prints'
                   margin to the makers over the close;
  FAIR SOURCES     per leg an hour before its start: Kalshi's mid, DraftKings,
                   Polymarket and their blend against the close and the result.

Leg prices: the maker's leg tape (run-logs\combo-maker\legs_<day>.jsonl, from
11/06 evening on); for a day without one, mids backed out of the maker's own
quotes (a quote's Kalshi-only fair is the product of its legs' mids: least
squares over 20-minute windows; Kalshi only). Results and start times: Kalshi's
/markets through kalshi_reads (signed first: Kalshi throttles unsigned list
reads from any IP), a leg read until its result is in, kept in
run-logs\combo-rfq\leg_results.json.

  python combo_scorecard.py                     # yesterday (UTC)
  python combo_scorecard.py --day 2026-11-06
  python combo_scorecard.py --day today         # so far
  python combo_scorecard.py --email             # the scheduled morning run
Writes run-logs\combo-rfq\scorecard_<day>.md and .json. --email also sends it
(Jack 2026-11-06: "yes i want the scorecard every morning"): once an ET day
(scorecard_sent_<ET day>.marker), at idle priority, through the IMM's alerter
like the other morning emails; --test sends now and writes no marker.
Scheduled daily 7:30 AM ET as "KL combo scorecard" (register_combo_scorecard.ps1).
"""
import argparse
import bisect
import gzip
import json
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import combo_maker as cm  # noqa: E402
from combo_rfq_score import MAKER_FEE_RATE  # noqa: E402

RFQ_DIR = os.environ.get("COMBO_RFQ_DIR", os.path.join(HERE, "run-logs", "combo-rfq"))
MAKER_DIR = os.environ.get("COMBO_MAKER_DIR", os.path.join(HERE, "run-logs", "combo-maker"))
RESULTS_NAME = "leg_results.json"
LOGGER_SLICE = 50                  # the logger ties prints to 1 in 50 of the RFQs (its shard)
WINDOW_S = 1200.0                  # the implied mids' windows
TAPE_AGE_S = 900.0                 # a leg-tape line older than this at a print doesn't price it
CLOSE_GAP_S = 60.0                 # the close: the last mid at least this long before the start...
CLOSE_AGE_S = 7200.0               # ...and no older than this
SOURCE_LEAD_S = 3600.0             # FAIR SOURCES compares the fairs this long before the start
MARKUPS = (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20, 0.30, 0.40)
BUCKETS = (("2", 2, 2), ("3-4", 3, 4), ("5-8", 5, 8), ("9-12", 9, 12), ("13+", 13, 10 ** 6))
LIVE_KEYS = ("markup", "markup_per_leg", "nfl_markup_cut", "confirm_edge_floor", "confirm_floor_per_leg",
             "longshot_markup", "min_legs", "max_legs", "min_fair", "max_fair")


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%SZ} [SCORE] {msg}", flush=True)


def bucket(n: int) -> str:
    return next(b for b, lo, hi in BUCKETS if lo <= n <= hi)


def fee_of(px: float, nfl: bool) -> float:
    """The maker fee a contract at px pays (none on an all-NFL parlay)."""
    return 0.0 if nfl else MAKER_FEE_RATE * px * (1.0 - px)


def _median(v):
    return statistics.median(v) if v else None


# ---- the files ------------------------------------------------------------------

def open_day(path: str):
    """A day file, plain or the .gz of a finished day (None if neither)."""
    if os.path.exists(path):
        return open(path, encoding="utf-8")
    if os.path.exists(path + ".gz"):
        return gzip.open(path + ".gz", "rt", encoding="utf-8")
    return None


def read_kind(path: str, kind: str) -> list:
    """The records of one kind ("exec", "quote", ...) in a day file; [] if none."""
    f = open_day(path)
    if f is None:
        return []
    tag = f'"k":"{kind}"'
    with f:
        return [json.loads(line) for line in f if tag in line]


def prev_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


# ---- leg prices -------------------------------------------------------------------

class Tape:
    """Each leg's prices over time -- rows (t, Kalshi mid, DraftKings YES,
    Polymarket YES), None where not known -- and its expected expiration."""

    def __init__(self, max_age: float = TAPE_AGE_S, source: str = "tape"):
        self.rows, self.exp, self.ts = defaultdict(list), {}, {}
        self.max_age, self.source = max_age, source

    def add(self, tk: str, t: float, mid, dk=None, poly=None) -> None:
        self.rows[tk].append((t, mid, dk, poly))

    def finish(self) -> "Tape":
        for tk, v in self.rows.items():
            v.sort(key=lambda r: r[0])
            self.ts[tk] = [r[0] for r in v]
        return self

    def at(self, tk: str, t: float, max_age: float = None):
        """The leg's latest row with a mid at or before t, at most max_age old."""
        ts = self.ts.get(tk)
        if not ts:
            return None
        age = self.max_age if max_age is None else max_age
        i = bisect.bisect_right(ts, t) - 1
        while i >= 0 and t - ts[i] <= age:
            r = self.rows[tk][i]
            if r[1] is not None:
                return r
            i -= 1
        return None

    def close(self, tk: str, start: float):
        """The closing mid: the last before start - CLOSE_GAP_S, CLOSE_AGE_S old at most."""
        r = self.at(tk, start - CLOSE_GAP_S, CLOSE_AGE_S)
        return None if r is None else r[1]


def load_tape(maker_dir: str, day: str):
    """The maker's leg tape for the day (and the day before, for the first minutes);
    None when the day has none."""
    tape, n = Tape(), 0
    for d in (prev_day(day), day):
        for rec in read_kind(os.path.join(maker_dir, f"legs_{d}.jsonl"), "legs"):
            t = rec["t"]
            for tk, row in (rec.get("m") or {}).items():
                row = list(row) + [None] * 4
                bid, ask, dk, poly = row[:4]
                ok = bid is not None and ask is not None and 0.0 < bid < ask < 1.0
                tape.add(tk, t, (bid + ask) / 2.0 if ok else None, dk, poly)
                if row[4]:
                    tape.exp[tk] = float(row[4])
                n += 1
    return tape.finish() if n else None


def implied_tape(maker_dir: str, day: str, window_s: float = WINDOW_S):
    """Kalshi mids backed out of the maker's quotes: a quote's Kalshi-only fair is
    the product of its legs' probabilities, so log fair = the sum of their logs --
    least squares a window; a leg needs 3 quotes in one. Kalshi only (no outside fairs)."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import lsqr
    by_win = defaultdict(list)
    for r in read_kind(os.path.join(maker_dir, f"combo_{day}.jsonl"), "quote"):
        fk = r.get("fair_k", r.get("fair"))
        if not fk or fk <= 0 or not r.get("legs"):
            continue
        by_win[int(r["at"] // window_s)].append(([(str(l[0]), str(l[1])) for l in r["legs"]], math.log(fk)))
    tape = Tape(max_age=window_s, source="implied")
    for w, qs in sorted(by_win.items()):
        cols, ri, ci, rhs = {}, [], [], []
        for i, (legs, y) in enumerate(qs):
            for leg in legs:
                ri.append(i)
                ci.append(cols.setdefault(leg, len(cols)))
            rhs.append(y)
        a = coo_matrix((np.ones(len(ri)), (ri, ci)), shape=(len(qs), len(cols))).tocsr()
        x = lsqr(a, np.array(rhs), atol=1e-10, btol=1e-10, iter_lim=5000)[0]
        deg = np.diff(a.tocsc().indptr)
        mids = defaultdict(list)
        for (tk, side), j in cols.items():
            if deg[j] >= 3:
                p = math.exp(min(0.0, float(x[j])))
                mids[tk].append(p if side == "yes" else 1.0 - p)
        for tk, v in mids.items():
            tape.add(tk, w * window_s, sum(v) / len(v))
    return tape.finish() if tape.rows else None


# ---- results ----------------------------------------------------------------------

def kalshi_markets(params: dict):
    """GET /markets through kalshi_reads: signed first (Kalshi throttles unsigned
    list reads from any IP; 11/06 this box's were refused for over an hour), paced
    to 4 a second, the public endpoint behind it. None when every route fails."""
    import kalshi_reads
    try:
        return kalshi_reads.kalshi_get("/markets", params)
    except Exception as e:                                       # noqa: BLE001 -- a refusal ends the reads
        log(f"! results read: {type(e).__name__}: {str(e)[:160]}")
        return None


class Results:
    """Each leg's result ("yes" / "no", "void" once settled without one), status and
    expected expiration, kept in leg_results.json; a leg is read again until final."""

    def __init__(self, path: str = None):
        self.path, self.d = path, {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.d = json.load(f)
            except (OSError, ValueError):
                self.d = {}

    def final(self, tk: str) -> bool:
        return (self.d.get(tk) or {}).get("r") in ("yes", "no", "void")

    def result(self, tk: str):
        return (self.d.get(tk) or {}).get("r")

    def exp(self, tk: str):
        return (self.d.get(tk) or {}).get("x")

    @staticmethod
    def parse(m: dict) -> dict:
        r = str(m.get("result") or "").lower()
        status = str(m.get("status") or "").lower()
        if r not in ("yes", "no"):
            r = "void" if status in ("settled", "finalized") else None
        return {"r": r, "s": status, "x": cm._iso_s(m.get("expected_expiration_time"))}

    def fetch(self, tickers, get, now: float = None) -> int:
        """Read the legs not final yet (100 a call); the count left unread (a refused
        call stops the reads, so a throttled API costs one back-off, not one a batch)."""
        need = [t for t in sorted(set(tickers)) if not self.final(t)]
        for i in range(0, len(need), 100):
            js = get({"tickers": ",".join(need[i:i + 100]), "limit": 1000})
            if js is None:
                self.save()
                return len(need) - i
            for m in js.get("markets") or []:
                if m.get("ticker"):
                    self.d[m["ticker"]] = dict(self.parse(m), at=now or time.time())
        self.save()
        return 0

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.path)


def outcome(legs, results: Results):
    """1 / 0 once the parlay is decided by its legs' results (0 at the first leg that
    lost); None while a leg is open, or with a void leg."""
    rs = [results.result(tk) for tk, _ in legs]
    if "void" in rs:
        return None
    if any(r in ("yes", "no") and r != side for r, (_, side) in zip(rs, legs)):
        return 0
    return 1 if all(r in ("yes", "no") for r in rs) else None


# ---- scoring ----------------------------------------------------------------------

def live_config(maker_dir: str, day: str):
    """The pricing knobs of the maker's last start that day (None: no start)."""
    starts = read_kind(os.path.join(maker_dir, f"combo_{day}.jsonl"), "start")
    if not starts:
        return None
    c = starts[-1].get("cfg") or {}
    kw = {k: c[k] for k in LIVE_KEYS if k in c}
    if "longshot_markup" in kw:
        kw["longshot_markup"] = tuple(tuple(x) for x in kw["longshot_markup"])
    return cm.Config(**kw)


def live_price(fair: float, legs, cfg) -> float:
    """What the live rule quotes at this fair: the leg ladder, the all-NFL cut, the
    long-shot step, raised to the after-fee floor (cm.quote_price / net_floor_price)."""
    ask, _ = cm.quote_price(fair, cfg, n_legs=len(legs), legs=legs)
    if cfg.net_floor_for(len(legs)) > 0:
        ask = max(ask, cm.net_floor_price(fair, legs, cfg))
    return ask


def price_prints(prints, tape: Tape, results: Results, now: float):
    """(scored rows, skips): each cross-game main-line YES print with every leg priced:
    its Kalshi-mid fair (fk), our fair (fmax: the highest source per leg), its close
    (None until every leg started and has one) and its outcome (None while open)."""
    rows, skip = [], Counter()
    for e in prints:
        legs = [(str(l[0]), str(l[1])) for l in e.get("legs") or []]
        if len(legs) < 2:
            skip["fewer than 2 legs"] += 1
            continue
        if len({cm.game_of(tk) for tk, _ in legs}) < len(legs):
            skip["same-game"] += 1
            continue
        if any(not cm.MAIN_SERIES.match(tk.split("-")[0]) for tk, _ in legs):
            skip["props / extras"] += 1
            continue
        ts, px, ct = e["ts_ms"] / 1000.0, float(e["px"]), float(e["ct"])
        fk, fmax, close, priced = 1.0, 1.0, 1.0, True
        for tk, side in legs:
            r = tape.at(tk, ts)
            if r is None:
                priced = False
                break
            p = r[1] if side == "yes" else 1.0 - r[1]
            fk *= p
            fmax *= max([p] + [(o if side == "yes" else 1.0 - o) for o in (r[2], r[3]) if o is not None])
            if close is not None:
                x = results.exp(tk) or tape.exp.get(tk)
                start = None if x is None else cm.start_of(tk, x)
                c = None if (start is None or start > now) else tape.close(tk, start)
                close = None if c is None else close * (c if side == "yes" else 1.0 - c)
        if not priced:
            skip["a leg unpriced"] += 1
            continue
        if fk <= 1e-6 or not 0.0 < px < 1.0:
            skip["dust price"] += 1
            continue
        rows.append({"ts": ts, "n": len(legs), "b": bucket(len(legs)), "px": px, "ct": ct, "cr": e.get("cr") or "",
                     "legs": legs, "fk": fk, "fmax": fmax, "close": close, "y": outcome(legs, results),
                     "nfl": all(tk.startswith("KXNFL") for tk, _ in legs)})
    return rows, skip


def seg_stats(rs) -> dict:
    prem = sum(r["ct"] * r["px"] for r in rs)
    out = {"prints": len(rs), "premium": round(prem, 2)}
    if not rs:
        return out
    clr = [r["px"] / r["fk"] - 1.0 for r in rs]
    out["clear_median"] = _median(clr)
    out["clear_wtd"] = sum(r["ct"] * r["px"] * (r["px"] / r["fk"] - 1.0) for r in rs) / prem
    out["fee_share"] = _median([fee_of(r["px"], r["nfl"]) / r["px"] for r in rs])
    out["median_px"] = _median([r["px"] for r in rs])
    cl = [r for r in rs if r["close"] is not None]
    out["closed"] = len(cl)
    if cl:
        cp = sum(r["ct"] * r["px"] for r in cl)
        out["over_close_median"] = _median([(r["px"] - r["close"]) / r["px"] for r in cl])
        out["over_close_wtd"] = sum(r["ct"] * (r["px"] - r["close"]) for r in cl) / cp
    st = [r for r in rs if r["y"] is not None]
    out["settled"] = len(st)
    if st:
        sp = sum(r["ct"] * r["px"] for r in st)
        paid = sum(r["ct"] * r["y"] for r in st)
        fees = sum(r["ct"] * fee_of(r["px"], r["nfl"]) for r in st)
        out.update(settled_premium=round(sp, 2), paid=round(paid, 2), paid_over_premium=paid / sp,
                   maker_margin=(sp - paid - fees) / sp, hits=sum(1 for r in st if r["y"]))
        stc = [r for r in st if r["close"] is not None]
        if stc:
            out["paid_over_close"] = sum(r["ct"] * r["y"] for r in stc) / sum(r["ct"] * r["close"] for r in stc)
            out["paid_over_fk"] = sum(r["ct"] * r["y"] for r in stc) / sum(r["ct"] * r["fk"] for r in stc)
    return out


def our_price(rs, markups=MARKUPS, cfg=None) -> list:
    """What a markup over our fair would have won: for each, the prints our price
    undercut (ours <= theirs) and, on those, at the same dollars, our margin over the
    close after the fee and the settled P&L. cfg: one more row, the live rule's
    (prints outside its legs / fair range left out)."""
    def row(label, priced):
        won = [(r, q) for r, q in priced if q <= r["px"] + 1e-12]
        o = {"markup": label, "of": len(priced), "won": len(won), "premium": 0.0, "closed_premium": 0.0,
             "vs_close": 0.0, "settled_premium": 0.0, "pnl": 0.0}
        for r, q in won:
            c = r["ct"] * r["px"] / q
            fee = c * fee_of(q, r["nfl"])
            o["premium"] += c * q
            if r["close"] is not None:
                o["closed_premium"] += c * q
                o["vs_close"] += c * (q - r["close"]) - fee
            if r["y"] is not None:
                o["settled_premium"] += c * q
                o["pnl"] += c * (q - r["y"]) - fee
        o["margin_close"] = o["vs_close"] / o["closed_premium"] if o["closed_premium"] else None
        for k in ("premium", "closed_premium", "vs_close", "settled_premium", "pnl"):
            o[k] = round(o[k], 2)
        return o
    out = [row(m, [(r, r["fmax"] * (1.0 + m)) for r in rs]) for m in markups]
    if cfg is not None:
        lv = [(r, live_price(r["fmax"], r["legs"], cfg)) for r in rs
              if cfg.min_legs <= r["n"] <= cfg.max_legs and cfg.min_fair <= r["fmax"] <= cfg.max_fair]
        out.append(row("live rule", lv))
    return out


def requesters(rs, top: int = 12, sharp: int = 8, min_closed: int = 15) -> dict:
    by = defaultdict(list)
    for r in rs:
        by[r["cr"]].append(r)

    def stat(cr, v):
        prem = sum(r["ct"] * r["px"] for r in v)
        cl = [r for r in v if r["close"] is not None]
        st = [r for r in v if r["y"] is not None]
        sp = sum(r["ct"] * r["px"] for r in st)
        return {"cr": cr, "prints": len(v), "premium": round(prem, 2), "closed": len(cl),
                "over_close": (sum(r["ct"] * (r["px"] - r["close"]) for r in cl) / sum(r["ct"] * r["px"] for r in cl)
                               if cl else None),
                "settled": len(st), "paid_over_premium": (sum(r["ct"] * r["y"] for r in st) / sp) if sp else None}
    allr = [stat(cr, v) for cr, v in by.items()]
    return {"count": len(allr),
            "top": sorted(allr, key=lambda x: -x["premium"])[:top],
            "sharpest": sorted((x for x in allr if x["closed"] >= min_closed), key=lambda x: x["over_close"])[:sharp]}


def fair_sources(rs, tape: Tape, results: Results) -> dict:
    """Per sport, on the scored prints' legs with a close and a result: the error of
    each fair an hour before the start against the close (mean |p - close|) and the
    result (Brier), each source on the legs it covered, with Kalshi's on the same legs."""
    acc = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0]))   # sport -> src -> n, mae, brier, k mae, k brier
    for tk in sorted({tk for r in rs for tk, _ in r["legs"]}):
        res, x = results.result(tk), results.exp(tk) or tape.exp.get(tk)
        if res not in ("yes", "no") or x is None:
            continue
        start = cm.start_of(tk, x)
        row, close = tape.at(tk, start - SOURCE_LEAD_S), tape.close(tk, start)
        if row is None or close is None:
            continue
        y = 1.0 if res == "yes" else 0.0
        k = row[1]
        outs = [o for o in (row[2], row[3]) if o is not None]
        srcs = {"Kalshi mid": k, "DraftKings": row[2], "Polymarket": row[3],
                "blend": (k + sum(outs)) / (1 + len(outs)) if outs else None, "close": close}
        sport = cm.sport_of(tk.split("-")[0]) or "?"
        for name, p in srcs.items():
            if p is None:
                continue
            a = acc[sport][name]
            a[0] += 1
            a[1] += abs(p - close)
            a[2] += (p - y) ** 2
            a[3] += abs(k - close)
            a[4] += (k - y) ** 2
    return {s: {n: {"legs": a[0], "mae_close": a[1] / a[0], "brier": a[2] / a[0], "k_mae_close": a[3] / a[0],
                    "k_brier": a[4] / a[0]} for n, a in v.items()} for s, v in acc.items()}


def score(prints, tape: Tape, results: Results, now: float, cfg=None, no_prints: int = 0) -> dict:
    rows, skip = price_prints(prints, tape, results, now)
    segs = {b: seg_stats([r for r in rows if r["b"] == b]) for b, _, _ in BUCKETS}
    segs["all"] = seg_stats(rows)
    return {"prints": len(prints), "no_side_prints": no_prints, "scored": len(rows), "skipped": dict(skip),
            "tape": tape.source, "segments": segs,
            "our_price": {b: our_price([r for r in rows if r["b"] == b], cfg=cfg) for b, _, _ in BUCKETS},
            "requesters": requesters(rows), "sources": fair_sources(rows, tape, results),
            "legs": len({tk for r in rows for tk, _ in r["legs"]}),
            "legs_settled": len({tk for r in rows for tk, _ in r["legs"] if results.final(tk)})}


# ---- the report -------------------------------------------------------------------

def _pct(x, d=1, sign=True):
    return "&ndash;" if x is None else (f"{x * 100:+.{d}f}%" if sign else f"{x * 100:.{d}f}%")


def _usd(x):
    return "&ndash;" if x is None else (f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}")


def render_md(day: str, sc: dict, results_left: int = 0, results_read: bool = True) -> str:
    L = [f"# Combo scorecard, {day} (UTC)", ""]
    sk = ", ".join(f"{k} {v:,}" for k, v in sorted(sc["skipped"].items(), key=lambda kv: -kv[1])) or "none"
    L.append(f"{sc['prints']:,} prints where the requester bought YES in the logger's slice (1 in {LOGGER_SLICE} "
             f"of the RFQs), {sc['no_side_prints']:,} more bought NO. Scored: {sc['scored']:,} cross-game main-line "
             f"prints ({_usd(sc['segments']['all']['premium'])} premium). Not scored: {sk}.")
    src = ("the maker's leg tape (Kalshi mids + DraftKings + Polymarket)" if sc["tape"] == "tape" else
           "mids backed out of the maker's own quotes (Kalshi only: our fair = the Kalshi-mid fair)")
    L.append(f"Leg prices: {src}. Results: {sc['legs_settled']:,} of the {sc['legs']:,} legs settled"
             + (f"; {results_left:,} could not be read (Kalshi refused)" if results_left else "")
             + ("" if results_read else " (none read this run: --no-results)")
             + ". The close needs each leg's start: from the tape, or from the results read.")
    L += ["", "## Where the market clears", "",
          "The print over the product of the legs' Kalshi mids at the print, and over their closing mids "
          "(+ = the makers sold above the close). Paid / premium under 100% = the makers kept money.", "",
          "| Legs | Prints | Premium | Median price | Over the mids (median / $-wtd) | Fee | Over the close "
          "(median / $-wtd) | Settled | Paid / premium | Paid / close said | Makers' margin after fees |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for b in [x[0] for x in BUCKETS] + ["all"]:
        s = sc["segments"][b]
        if not s.get("prints"):
            continue
        L.append(f"| {b} | {s['prints']:,} | {_usd(s['premium'])} | {s['median_px'] * 100:.1f}c | "
                 f"{_pct(s.get('clear_median'))} / {_pct(s.get('clear_wtd'))} | {_pct(s.get('fee_share'), sign=False)} | "
                 f"{_pct(s.get('over_close_median'))} / {_pct(s.get('over_close_wtd'))} ({s.get('closed', 0):,}) | "
                 f"{s.get('settled', 0):,} | {_pct(s.get('paid_over_premium'), sign=False)} | "
                 f"{_pct(s.get('paid_over_close'), sign=False)} | {_pct(s.get('maker_margin'))} |")
    L += ["", "## What our price would have won", "",
          "At each markup over our fair (the live maker's: the highest of Kalshi / DraftKings / Polymarket per "
          "leg), the prints our price undercut, and on those, at the same dollars: our margin over the close "
          "after the maker fee, and the settled P&L. First order: the other makers don't answer. In the "
          f"logger's slice: the market is about {LOGGER_SLICE}x.", ""]
    for b, _, _ in BUCKETS:
        rows = sc["our_price"].get(b) or []
        if not rows or not rows[0]["of"]:
            continue
        L += [f"**{b} legs** ({rows[0]['of']:,} prints)", "",
              "| Markup | Undercut | Premium | Margin over the close | $ over the close | Settled P&L |", "|---|---|---|---|---|---|"]
        for o in rows:
            lab = o["markup"] if isinstance(o["markup"], str) else f"{o['markup'] * 100:g}%"
            share = f"{o['won']:,} ({o['won'] / o['of']:.0%})" if o["of"] else "0"
            L.append(f"| {lab} | {share} | {_usd(o['premium'])} | {_pct(o['margin_close'])} | "
                     f"{_usd(o['vs_close']) if o['closed_premium'] else '&ndash;'} | "
                     f"{_usd(o['pnl']) if o['settled_premium'] else '&ndash;'} |")
        L.append("")
    rq = sc["requesters"]
    L += ["## Requesters", "", f"{rq['count']:,} requesters bought the scored prints. The most active:", "",
          "| Requester | Prints | Premium | Over the close (makers' margin) | Settled | Paid / premium |", "|---|---|---|---|---|---|"]
    for x in rq["top"]:
        L.append(f"| {x['cr']} | {x['prints']:,} | {_usd(x['premium'])} | {_pct(x['over_close'])} ({x['closed']}) | "
                 f"{x['settled']} | {_pct(x['paid_over_premium'], sign=False)} |")
    if rq["sharpest"]:
        L += ["", "The sharpest (15+ prints with a close; most negative = they beat the close):", "",
              "| Requester | Prints | Premium | Over the close | Paid / premium |", "|---|---|---|---|---|"]
        for x in rq["sharpest"]:
            L.append(f"| {x['cr']} | {x['prints']:,} | {_usd(x['premium'])} | {_pct(x['over_close'])} | "
                     f"{_pct(x['paid_over_premium'], sign=False)} |")
    L += ["", "## Fair sources an hour before the start", "",
          "Mean gap between the fair and the close, and Brier score against the result, per leg; each source on "
          "the legs it covered, "
          "Kalshi's mid on the same legs beside it.", "",
          "| Sport | Source | Legs | Gap to the close | Kalshi's gap on them | Brier | Kalshi's Brier on them |", "|---|---|---|---|---|---|---|"]
    for sport in sorted(sc["sources"]):
        for name, a in sc["sources"][sport].items():
            L.append(f"| {sport} | {name} | {a['legs']:,} | {a['mae_close'] * 100:.2f} pts | {a['k_mae_close'] * 100:.2f} pts | "
                     f"{a['brier']:.4f} | {a['k_brier']:.4f} |")
    if not sc["sources"]:
        L.append("| &ndash; | no legs with a close and a result yet | | | | | |")
    return "\n".join(L) + "\n"


def md_to_html(md: str) -> str:
    """The report's markdown (headings, tables, bold, paragraphs) as an email's HTML."""
    import re
    esc = lambda s: re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s.replace("<", "&lt;").replace(">", "&gt;"))  # noqa: E731
    out, lines, i = [], md.splitlines(), 0
    cell = "border:1px solid #ddd;padding:3px 6px"
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                if not re.match(r"^\|[-| ]+\|$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table style='border-collapse:collapse;font-size:13px'>")
            for k, r in enumerate(rows):
                tag = "th" if k == 0 else "td"
                out.append("<tr>" + "".join(f"<{tag} style='{cell}'>{esc(c)}</{tag}>" for c in r) + "</tr>")
            out.append("</table>")
            continue
        if ln.startswith("# "):
            out.append(f"<h2>{esc(ln[2:])}</h2>")
        elif ln.startswith("## "):
            out.append(f"<h3>{esc(ln[3:])}</h3>")
        elif ln.strip():
            out.append(f"<p>{esc(ln)}</p>")
        i += 1
    return "<html><body style='font-family:Segoe UI,Arial,sans-serif'>" + "\n".join(out) + "</body></html>"


def subject_for(day: str, sc: dict) -> str:
    """The email's subject: the day, the prints scored, where 2 legs clear, the live rule's wins."""
    s2 = sc["segments"].get("2") or {}
    live = next((o for o in sc["our_price"].get("2") or [] if o["markup"] == "live rule"), None)
    parts = [f"Combo scorecard {day}: {sc['scored']:,} prints scored"]
    if s2.get("clear_median") is not None:
        parts.append(f"2 legs clear {s2['clear_median'] * 100:+.1f}% over the mids (fee "
                     f"{(s2.get('fee_share') or 0) * 100:.1f}%)")
    if live and live["of"]:
        parts.append(f"our 2-leg price beats {live['won'] / live['of']:.0%} of them")
    return "; ".join(parts)


def low_priority() -> None:
    """Idle priority for the scheduled run: the live bots share the PC."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40)      # IDLE_PRIORITY_CLASS
    except Exception:                                            # noqa: BLE001
        pass


def send_email(day: str, sc: dict, md: str, attempts: int = 6, sleep=time.sleep) -> bool:
    """The report through the IMM's alerter (imm_quote_gaps first, like the other
    morning emails, so the launcher env and the alert credentials apply)."""
    import imm_quote_gaps  # noqa: F401  (import side effects)
    import incentive_mm as imm
    alerter = imm.Alerter("COMBO-SCORE", live=True)
    if not alerter.enabled:
        log("cannot send the scorecard email: alert credentials not configured")
        return False
    subject, html = subject_for(day, sc), md_to_html(md)
    for attempt in range(1, attempts + 1):
        if alerter.send_message(md, subject=subject, html=html):
            log(f"emailed: {subject}")
            return True
        log(f"scorecard send attempt {attempt}/{attempts} failed" + ("; retry in 5 min" if attempt < attempts else ""))
        if attempt < attempts:
            sleep(300)
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", default="yesterday", help="UTC day YYYY-MM-DD, 'today' or 'yesterday' (default)")
    ap.add_argument("--rfq-dir", default=RFQ_DIR)
    ap.add_argument("--maker-dir", default=MAKER_DIR)
    ap.add_argument("--out", default=None, help="where the report goes (default: --rfq-dir)")
    ap.add_argument("--no-results", action="store_true", help="read no results from Kalshi (cached ones only)")
    ap.add_argument("--email", action="store_true",
                    help="the scheduled morning run: email the report, once an ET day (a sent-marker), at idle priority")
    ap.add_argument("--test", action="store_true", help="with --email: send now, whatever the marker; write none")
    a = ap.parse_args(argv)
    now = time.time()
    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    marker = os.path.join(a.rfq_dir, f"scorecard_sent_{cm.et_day(now)}.marker")
    if a.email and not a.test:
        if os.path.exists(marker):
            log(f"the scorecard email already went out today ({os.path.basename(marker)}); exiting")
            return 0
        low_priority()
    day = {"today": today, "yesterday": prev_day(today)}.get(a.day, a.day)
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        ap.error("--day YYYY-MM-DD, today or yesterday")
    ex = read_kind(os.path.join(a.rfq_dir, f"combo_{day}.jsonl"), "exec")
    prints = [e for e in ex if e.get("side") == "yes"]
    log(f"{day}: {len(prints):,} YES prints, {len(ex) - len(prints):,} NO")
    tape = load_tape(a.maker_dir, day)
    if tape is None:
        log("no leg tape that day: backing the mids out of the maker's quotes")
        tape = implied_tape(a.maker_dir, day)
    if tape is None:
        log("no leg prices for the day (no tape, no quotes): nothing to score")
        return 2
    results = Results(os.path.join(a.rfq_dir, RESULTS_NAME))
    left = 0
    if not a.no_results:
        rows, _ = price_prints(prints, tape, Results(), now)
        legs = {tk for r in rows for tk, _ in r["legs"]}
        left = results.fetch(legs, kalshi_markets, now)
        log(f"results: {len(legs):,} legs, {sum(1 for t in legs if results.final(t)):,} final"
            + (f", {left:,} unread (Kalshi refused)" if left else ""))
    sc = score(prints, tape, results, now, cfg=live_config(a.maker_dir, day), no_prints=len(ex) - len(prints))
    out = a.out or a.rfq_dir
    md = render_md(day, sc, left, results_read=not a.no_results)
    with open(os.path.join(out, f"scorecard_{day}.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(out, f"scorecard_{day}.json"), "w", encoding="utf-8") as f:
        json.dump(dict(sc, day=day, made=now, results_unread=left), f, indent=1, default=str)
    log(f"wrote scorecard_{day}.md / .json: {sc['scored']:,} prints scored")
    if a.email:
        if not send_email(day, sc, md):
            return 1
        if not a.test:
            try:
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
            except OSError as e:
                log(f"! sent-marker not written ({e!r}): a re-run today would send again")
    return 0


if __name__ == "__main__":
    sys.exit(main())
