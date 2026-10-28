#!/usr/bin/env python3
"""Dated rows the live IMM bot hot-reloads (Jack 2026-10-07: "how to ensure new
events get quoted when they should fall under existing quote families ... dont
want to manually catch this going fwd" -> "yes build all 5").

Two files beside the bot's state, re-read every universe refresh (no deploy):
  election_dates_extra.json -- election-day rows (incentive_mm.ELECTION_DATES_EXTRA)
  awards_dates_extra.json   -- award nomination/announcement rows (AWARDS_DATES_EXTRA)
each {"rows": [{"glob", "date", "zone", "sources", "by", "at", "note"}]}.

Writers: hand fixes and imm_family_watch.py's research run (this CLI), and
the watch's US-general rule (add_row). Every row needs TWO different
sources -- Jack on election dates: "dont trust that, verify yourself". A row the
code table already covers is refused (the code row wins at lookup; fix it there).

  python imm_rows.py election add KXSTATELEG-LAHOUSE27 2027-10-23 --zone America/Chicago \\
      --source "https://www.sos.la.gov/..." --source "https://ballotpedia.org/..." --by hand
  python imm_rows.py award add KXCRITICSCHOICENOM-SONG27 2026-11-16 --source ... --source ...
  python imm_rows.py election list
  python imm_rows.py award remove KXCRITICSCHOICENOM-SONG27
  python imm_rows.py election lookup KXSTATELEG-TXSENA26   # read-only Kalshi wording

TERM END (Jack 2026-10-28: "date it to term end as a rule"): a vote nobody has
called yet takes --term-end -- the day by which it must happen (the sitting
term's end, a default date in law, the contract's deadline or Kalshi's own
date, the earliest). The row gets a "checked" stamp; the bot quotes to it for
incentive_mm.ELECTION_TERM_END_RECHECK_DAYS from that stamp, the family
watch's research re-checks it weekly, adding the same row again re-stamps it,
and a voting-day add replaces it once the vote is called:
  python imm_rows.py election add KXJAPANHOUSE-30FEB07 2030-02-07 --zone Asia/Tokyo \\
      --term-end --source ... --source ...
"""

import argparse
import fnmatch
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import List, Tuple

import incentive_mm as imm

KINDS = ("election", "award")


def rows_path(kind: str) -> str:
    return imm.election_dates_extra_path() if kind == "election" else imm.awards_dates_extra_path()


def load_rows(kind: str) -> list:
    try:
        with open(rows_path(kind), encoding="utf-8") as f:
            return [r for r in ((json.load(f) or {}).get("rows") or []) if isinstance(r, dict)]
    except (OSError, ValueError):
        return []


def save_rows(kind: str, rows: list) -> None:
    path = rows_path(kind)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"rows": sorted(rows, key=lambda r: r.get("glob", ""))}, f, indent=1)
    os.replace(tmp, path)


def _code_table(kind: str) -> List[str]:
    return [r[0] for r in (imm.ELECTION_DATES if kind == "election" else imm.AWARDS_EVENT_DATES)]


def validate(kind: str, glob: str, day: str, zone: str, sources: List[str],
             term_end: bool = False) -> str:
    """'' when the row is acceptable, else why not."""
    if kind not in KINDS:
        return f"kind must be one of {KINDS}"
    if term_end and kind != "election":
        return "--term-end is for election rows only"
    if not imm.ROWS_GLOB_RE.fullmatch(glob or ""):
        return (f"bad event glob {glob!r} (a series name of 3+ letters/digits, "
                f"then letters/digits/-/*?[])")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except (TypeError, ValueError):
        return f"bad date {day!r} (YYYY-MM-DD)"
    if zone and kind != "election":
        return "--zone is for election rows only"
    if len({s.strip() for s in sources if s and s.strip()}) < 2:
        return "two different sources are required"
    spec = f"{glob}={day}" + (f"@{zone}" if zone else "")
    try:
        (imm._parse_election_dates if kind == "election" else imm._parse_awards_dates)(spec)
    except ValueError as e:
        return str(e)
    shadow = [g for g in _code_table(kind) if fnmatch.fnmatchcase(glob, g)]
    if shadow:
        return f"the code table already dates {glob} (row {shadow[0]}); fix it there"
    return ""


def add_row(kind: str, glob: str, day: str, sources: List[str], zone: str = "",
            by: str = "", note: str = "", replace: bool = False,
            term_end: bool = False) -> Tuple[bool, str]:
    """Validate and write one row. (ok, message). The same term-end row again
    re-stamps its "checked"; a voting-day row replaces a term-end one (the
    vote was called) without `replace`."""
    why = validate(kind, glob, day, zone, sources, term_end)
    if why:
        return False, why
    rows = load_rows(kind)
    old = [r for r in rows if r.get("glob") == glob]
    restamp = False
    if old and not replace:
        was_term = bool(old[0].get("term_end"))
        if old[0].get("date") == day and (old[0].get("zone") or "") == (zone or "") \
                and was_term == term_end:
            if not term_end:
                return True, f"{glob} already = {day} (unchanged)"
            restamp = True
        elif not (was_term and not term_end):
            return False, (f"{glob} already has a row ({old[0].get('date')}"
                           + (", term end" if was_term else "")
                           + "); pass replace to change it")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = {"glob": glob, "date": day, "sources": [s.strip() for s in sources if s.strip()],
           "by": by or "hand", "at": (old[0].get("at") or now) if restamp else now}
    if zone:
        row["zone"] = zone
    if note:
        row["note"] = note
    if term_end:
        row["term_end"] = True
        row["checked"] = now
    save_rows(kind, [r for r in rows if r.get("glob") != glob] + [row])
    return True, (f"{kind} row {glob} = {day}" + (f" @{zone}" if zone else "")
                  + ((" (term end, re-checked)" if restamp else " (term end)")
                     if term_end else ""))


def remove_row(kind: str, glob: str) -> Tuple[bool, str]:
    rows = load_rows(kind)
    keep = [r for r in rows if r.get("glob") != glob]
    if len(keep) == len(rows):
        return False, f"no {kind} row {glob}"
    save_rows(kind, keep)
    return True, f"removed {kind} row {glob}"


def lookup(event: str) -> str:
    """Read-only: what Kalshi says about an event -- its title, sub_title and
    the first markets' own terms and dates -- for the research run."""
    c = imm._fair_reader_client()
    try:
        e = (c.get_event(event) or {}).get("event") or {}
    except Exception as ex:
        return f"event {event}: read failed ({ex})"
    try:
        ms = (c.get_markets(event_ticker=event) or {}).get("markets") or []
    except Exception:
        ms = []
    out = [f"event {event}: {e.get('title')} | sub_title: {e.get('sub_title')} | "
           f"category {e.get('category')}"]
    for m in ms[:3]:
        out.append(f"  {m.get('ticker')} ({m.get('yes_sub_title') or ''}): "
                   f"{(m.get('rules_primary') or '').strip()[:500]} | expected_expiration "
                   f"{m.get('expected_expiration_time')} | occurrence "
                   f"{m.get('occurrence_datetime')} | close {m.get('close_time')}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("kind", choices=KINDS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("glob")
    a.add_argument("date")
    a.add_argument("--zone", default="")
    a.add_argument("--source", action="append", default=[])
    a.add_argument("--by", default="hand")
    a.add_argument("--note", default="")
    a.add_argument("--replace", action="store_true")
    a.add_argument("--term-end", action="store_true",
                   help="election only: a vote not yet called, dated to its term end "
                        "(re-adding the same row re-stamps its check)")
    r = sub.add_parser("remove")
    r.add_argument("glob")
    sub.add_parser("list")
    lk = sub.add_parser("lookup", help="read-only: Kalshi's own wording and dates")
    lk.add_argument("event")
    args = ap.parse_args(argv)
    if args.cmd == "lookup":
        print(lookup(args.event))
        return 0
    if args.cmd == "list":
        for row in load_rows(args.kind):
            print(f"{row.get('glob')} = {row.get('date')}"
                  + (f" @{row['zone']}" if row.get("zone") else "")
                  + (f" (term end, checked {row.get('checked')})" if row.get("term_end") else "")
                  + f"  [{row.get('by')}, {row.get('at')}]  " + " | ".join(row.get("sources") or []))
        return 0
    if args.cmd == "remove":
        ok, msg = remove_row(args.kind, args.glob)
    else:
        ok, msg = add_row(args.kind, args.glob, args.date, args.source, zone=args.zone,
                          by=args.by, note=args.note, replace=args.replace,
                          term_end=args.term_end)
    print(("OK: " if ok else "REFUSED: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
