"""Reader for incentive_mm's full-depth order-book log.

Files: <STATUS_DIR>/book_depth/book_depth_<UTC day>_<RUN_ID>[.pN].jsonl.gz,
one per bot run per day. Each flush appends ONE complete gzip member, so a
file is a concatenation of gzip members (valid gzip). A hard kill mid-write
can leave a truncated TAIL member; iter_rows() stops cleanly there instead of
raising, which is what plain gzip.open / pandas.read_json would do.

Row schema (one JSON object per book read):
  ts            epoch seconds of the read
  cycle_ts      the cycle's start, same format as cycle_log.ts -> join on
                (cycle_ts, ticker) for managed rows
  ticker, source ("managed" | "candidate"), fast (fast-lane mini-cycle)
  unit          "dollars" (orderbook_fp, sub-penny exact) or "cents" (legacy)
  yes, no       the RAW Kalshi levels [[price, size], ...] -- YES bids and NO
                bids, INCLUDING our own resting orders
  own_yes_cents / own_no_cents
                our own resting size per exact level, in CENTS: yes side keyed
                by YES cents, no side by NO cents (= 100 - our ask's YES price).
                Taken from an exchange resting read made before the book read,
                so own can briefly exceed a level; competitor_levels clamps at 0.
  own_src       where own size came from:
                  "resting_read"       managed row: this cycle's read. Empty
                                       for an event an earlier sibling's
                                       event-wide cancel pulled this cycle.
                  "resting_prev_cycle" candidate row: the PREVIOUS cycle's
                                       read (~1 cycle old). For any market we
                                       quote, prefer the managed row of the
                                       same cycle_ts.
  target, discount, pool_per_day   the program terms the book was scored under
  run_id, config_hash

Dedup on read by (ts, ticker, source): a retried flush after a partial write
can repeat rows.
"""
import glob
import json
import os
import zlib


def book_dir(status_dir=None):
    if status_dir:
        return os.path.join(status_dir, "book_depth")
    import incentive_mm as imm            # honours IMM_BOOK_LOG_DIR
    return imm.book_log_dir()


def day_files(day, status_dir=None):
    """All files for one UTC day (YYYY-MM-DD), every run and part."""
    return sorted(glob.glob(os.path.join(book_dir(status_dir),
                                         f"book_depth_{day}_*.jsonl.gz")))


def iter_rows(path, chunk=1 << 20):
    """Yield row dicts from one file, member by member, streaming in chunks.

    Linear in file size, holding about one member in memory. (Re-slicing the
    whole remaining buffer per member was quadratic: a day file has ~1,000+
    members, one per flush.) A member's rows are yielded only once that
    member is fully decompressed -- gzip end-of-stream reached -- so a
    truncated or corrupt tail member (a hard kill mid-write) yields nothing
    and the iteration stops cleanly instead of raising."""
    with open(path, "rb") as f:
        dec = zlib.decompressobj(wbits=31)      # 31 = gzip wrapper
        acc = b""                               # decompressed bytes of the member
        buf = b""
        while True:
            if not buf:
                buf = f.read(chunk)
                if not buf:
                    return                      # EOF; a member without eof = truncated tail
            try:
                acc += dec.decompress(buf)
            except zlib.error:
                return                          # corrupt tail member
            buf = b""
            if dec.eof:
                for ln in acc.splitlines():
                    if ln:
                        yield json.loads(ln)
                buf = dec.unused_data           # leftover of THIS chunk only
                acc = b""
                dec = zlib.decompressobj(wbits=31)


def iter_day(day, status_dir=None, dedup=True):
    seen = set()
    for path in day_files(day, status_dir):
        for row in iter_rows(path):
            if dedup:
                key = (row.get("ts"), row.get("ticker"), row.get("source"))
                if key in seen:
                    continue
                seen.add(key)
            yield row


def levels_cents(side, unit="dollars"):
    """Raw levels -> [[price_cents, size], ...] as floats (sub-penny kept)."""
    f = 100.0 if unit == "dollars" else 1.0
    return [[round(float(p) * f, 4), float(q)] for p, q in side]


def competitor_levels(row, side="yes"):
    """External (competitor) depth on one side: book minus our own size,
    clamped at zero, in cents. side is 'yes' or 'no'."""
    lv = levels_cents(row.get(side) or [], row.get("unit", "dollars"))
    own = {round(float(c), 4): float(n) for c, n in (row.get(f"own_{side}_cents") or [])}
    out = []
    for c, q in lv:
        ext = q - own.get(c, 0.0)
        if ext > 1e-9:
            out.append([c, ext])
    return out
