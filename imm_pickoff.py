#!/usr/bin/env python3
r"""imm_pickoff.py -- PICK-OFF WINDOWS: events whose Kalshi event start is
LATER than the real event.

Jack 2026-09-27: "when kalshi's datetimes are off, sometimes i can manually
pick off other traders who have their orders expire 'at event start'. be clear
in the emails when this is the case, with what time the event is and what time
kalshi thinks the event is."

Kalshi's order ticket has an "At event start" time-in-force: a resting order is
pulled when the scheduled event its market is tied to starts. Kalshi's schedule
is its milestone feed (GET /milestones, start_date -- "Nike Earnings Call"),
and it is wrong often enough to matter, in both directions:

  LATER than the real event -> "at event start" orders are still resting while
      the real event runs, and Jack picks them off by hand. LLY-26AUG07:
      milestone Fri Aug 7 08:30 ET, Lilly reported Wed Aug 5, Kalshi closed the
      markets 12:08 that day. DELL-26SEP03: milestone Thu Sep 3 16:30, Dell
      reported Tue Sep 1 after the close, markets closed Wed Sep 2 09:38.
  EARLIER -> those orders are pulled BEFORE the real event: nothing to pick off
      (CCL-26SEP28: milestone Mon Sep 28 10:00, call Tue Sep 29 10:00;
      NKE-26SEP29: milestone Tue Sep 29 17:00, results Thu Oct 1 16:15).

THE REAL TIME is our own: the event_start_overrides the bot trades on (hand-set,
IR page, Nasdaq, broadcast schedule), else -- for an earnings-mention event with
no override, i.e. no reward program, which is most of them -- the Nasdaq
calendar, read the way imm_earnings_overrides.py reads it. For earnings that
time is the RELEASE, and the call follows it: 0.5-4h later the same day, or the
NEXT MORNING for split reporters (TOL-26AUG18: release Tue after the close, call
Wed 08:30, and the milestone had that right). So a gap only counts once it is
bigger than a normal release->call gap:

  after-close release, or no hour published   Kalshi's start after NOON next day
  anything else                               Kalshi's start 6h+ after ours

Measured 2026-09-27 over the 135 overrides that have a milestone: LLY (+49.5h)
and DELL (+48.5h) clear those bars and nothing else does -- every other gap sat
in -47h..+4.8h, plus TOL's +16.5h after the close. What the bars cannot see: a
same-day hour error smaller than the bar. The data cannot tell that from a
normal release->call gap, or from an override set early on purpose (the Trump
broadcast overrides stand down at the programme start, 2-5h before the
milestone's speech time).

Candidates: a milestone that starts AFTER now (once Kalshi's start passes the
orders are gone), a real time in [now - 3d, now + 14d], and a market still
trading (Kalshi closes the markets once the real call is over). ALL milestone
types are swept, not just company_report: DELL's call was a one_off_milestone.

Read-only. scan() never raises; failures come back as {"error": ...}.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import incentive_mm as imm
from incentive_mm import ET

# Kill switch: IMM_PICKOFF_ENABLE=0 makes scan() report nothing (all three
# emails then read exactly as they did before 2026-09-27).
ENABLED = os.environ.get("IMM_PICKOFF_ENABLE", "1") != "0"
LOOKBACK_DAYS = 3
LOOKAHEAD_DAYS = int(os.environ.get("IMM_PICKOFF_LOOKAHEAD_DAYS", "14"))
# Further apart than this is a different quarter's call, not a mis-dated one
# (a company that reported yesterday already has next quarter's event listed).
SAME_CALL_MAX_DAYS = 30
SLACK_HOURS = 6.0

# The text header doubles as the marker send_imm_digest.main() looks for to
# tag the subject line, so it is one constant.
HEADER = "PICK-OFF WINDOW"


MAX_MILESTONE_PAGES = 200


def _merge_milestones(out: dict, batch) -> None:
    """Fold milestone rows into {event: {"start", "end", "title"}}. Primary
    events only. When an event has several milestones the EARLIEST start
    wins: that is when its orders go."""
    for m in batch or []:
        start = imm.parse_iso_utc(m.get("start_date") or "")
        if start is None:
            continue
        for ev in m.get("primary_event_tickers") or []:
            cur = out.get(ev)
            if cur is None or start < cur["start"]:
                out[ev] = {"start": start,
                           "end": imm.parse_iso_utc(m.get("end_date") or ""),
                           "title": str(m.get("title") or "")}


def kalshi_event_starts(client, now_utc: datetime) -> dict:
    """{event_ticker: {"start", "end", "title"}} for every milestone that
    starts AFTER now, in one sweep of every type (~6,000 rows, 13 pages, ~2s
    on 2026-09-27). Only a future start can still pull orders, so nothing
    earlier is fetched: from a date a month back the same sweep is 30,000+
    rows of finished sports games, and the feed is not ordered by start, so a
    page cap there silently drops events (it lost DELL in a replay). The cap
    here is far above the live size and is logged if it is ever reached."""
    out = {}
    since = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = None
    for _page in range(MAX_MILESTONE_PAGES):
        params = {"limit": 500, "minimum_start_date": since}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/milestones", params=params) or {}
        batch = resp.get("milestones") or []
        _merge_milestones(out, batch)
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
    else:
        imm.log("! pick-off check: milestone sweep stopped at {} pages; "
                "events past that are not compared".format(MAX_MILESTONE_PAGES))
    return out


def kalshi_start_for(client, ev: str):
    """Kalshi's start for ONE event (any date), or None. For callers that
    need a start the future-only sweep does not carry, e.g. a digest row
    whose Kalshi start is already behind us."""
    try:
        resp = client.get("/milestones", params={
            "related_event_ticker": ev, "limit": 20}) or {}
    except Exception:
        return None
    one = {}
    _merge_milestones(one, resp.get("milestones"))
    return (one.get(ev) or {}).get("start")


def _is_earnings(ev: str) -> bool:
    return ev.startswith(imm._EARNINGS_PREFIX)


def _real_from_override(ev: str, ov: datetime, rec) -> dict:
    """What our override says about the real event, and how far past it
    Kalshi's start has to be before the gap means anything."""
    ov_et = ov.astimezone(ET)
    label = ""
    if isinstance(rec, dict) and imm.parse_iso_utc(str(rec.get("iso") or "")) == ov:
        label = str(rec.get("label") or "")
    kind = "start"
    if _is_earnings(ev):
        import imm_earnings_overrides as ieo
        if label and ieo.provenance_of(label) == "guess":
            kind = "no_hour"
        elif (ov_et.hour, ov_et.minute) == (16, 0):
            kind = "after_close"
        elif (ov_et.hour, ov_et.minute) == (7, 0):
            kind = "before_open"
        else:
            kind = "call"
    return {"at": ov, "kind": kind, "label": label, "source": "our override"}


def _nasdaq_by_symbol(now_utc: datetime, for_date=None) -> dict:
    """{SYMBOL: (date_et, time_flag)}: the FIRST day in [now - LOOKBACK_DAYS,
    now + LOOKAHEAD_DAYS] each symbol reports on. `for_date` is the overrides
    task's per-run cached Nasdaq read (imm_earnings_overrides.
    nasdaq_earnings_for_date); that task passes its own so a run that already
    read those days pays nothing."""
    if for_date is None:
        import imm_earnings_overrides as ieo
        for_date = ieo.nasdaq_earnings_for_date
    out = {}
    d0 = now_utc.astimezone(ET).date() - timedelta(days=LOOKBACK_DAYS)
    for i in range(LOOKBACK_DAYS + LOOKAHEAD_DAYS + 1):
        d = d0 + timedelta(days=i)
        for sym, flag in (for_date(d.isoformat()) or {}).items():
            out.setdefault(sym, (d, flag or ""))
    return out


def _real_from_nasdaq(ev: str, nasdaq: dict):
    """Nasdaq's release for an earnings-mention event with no override, or
    None when the series suffix is not a symbol Nasdaq lists (ARITZIA)."""
    hit = nasdaq.get(ev.split("-")[0][len(imm._EARNINGS_PREFIX):].upper())
    if not hit:
        return None
    d, flag = hit
    if "after" in flag:
        kind, hour = "after_close", 16
    elif "pre" in flag or "before" in flag:
        kind, hour = "before_open", 7
    else:
        kind, hour = "no_hour", 7
    at = ET.localize(datetime(d.year, d.month, d.day, hour, 0))
    return {"at": at, "kind": kind, "label": "", "source": "Nasdaq calendar"}


def _bar(real: dict) -> datetime:
    """Kalshi's start must be LATER than this for the gap to be a mis-dated
    event rather than the normal wait between a release and its call."""
    if real["kind"] in ("after_close", "no_hour"):
        d = real["at"].astimezone(ET).date() + timedelta(days=1)
        return ET.localize(datetime(d.year, d.month, d.day, 12, 0))
    return real["at"] + timedelta(hours=SLACK_HOURS)


def _live_markets(client, ev: str) -> int:
    """Markets of `ev` still trading; -1 when the read failed (the row is kept
    and says so, rather than silently dropped)."""
    try:
        resp = client.get("/events/" + ev,
                          params={"with_nested_markets": "true"}) or {}
    except Exception:
        return -1
    markets = ((resp.get("event") or {}).get("markets")
               or resp.get("markets") or [])
    return sum(1 for m in markets if m.get("status") in ("active", "open"))


def scan(client, now_utc: datetime, meta=None, nasdaq_for_date=None) -> dict:
    """{"rows": [pick-off windows], "kalshi": {event: start_utc}, "checked": n,
    "error": None}. `kalshi` covers every event in the sweep, so a caller can
    print Kalshi's start next to rows of its own. `meta` is the overrides
    provenance sidecar; None reads it. Never raises."""
    res = {"rows": [], "kalshi": {}, "checked": 0, "error": None}
    if not ENABLED:
        return res
    try:
        starts = kalshi_event_starts(client, now_utc)
        res["kalshi"] = {ev: v["start"] for ev, v in starts.items()}
        imm.load_file_event_overrides()          # in-process merge; no writes
        if meta is None:
            import imm_earnings_overrides as ieo
            meta = ieo.load_meta()
        lo = now_utc - timedelta(days=LOOKBACK_DAYS)
        hi = now_utc + timedelta(days=LOOKAHEAD_DAYS)
        nasdaq = None
        for ev, ks in sorted(starts.items()):
            if ks["start"] <= now_utc:
                continue              # Kalshi's start passed: the orders are gone
            ov = imm.EVENT_START_OVERRIDES.get(ev)
            if ov is not None:
                real = _real_from_override(ev, ov, meta.get(ev))
            elif _is_earnings(ev):
                if nasdaq is None:
                    nasdaq = _nasdaq_by_symbol(now_utc, nasdaq_for_date)
                real = _real_from_nasdaq(ev, nasdaq)
            else:
                continue              # nothing of ours to compare against
            if real is None or not (lo <= real["at"] <= hi):
                continue
            res["checked"] += 1
            gap_h = (ks["start"] - real["at"]).total_seconds() / 3600.0
            if gap_h > SAME_CALL_MAX_DAYS * 24 or ks["start"] <= _bar(real):
                continue
            live = _live_markets(client, ev)
            if live == 0:
                continue              # Kalshi already closed it: window over
            res["rows"].append({
                "event": ev, "title": ks["title"], "kalshi_start": ks["start"],
                "real": real["at"], "kind": real["kind"],
                "label": real["label"], "source": real["source"],
                "gap_h": gap_h, "open_now": real["at"] <= now_utc,
                "live_mkts": live})
        # open windows first, then the soonest real event
        res["rows"].sort(key=lambda r: (not r["open_now"], r["real"]))
    except Exception as e:                       # noqa: BLE001 -- never raise
        res["error"] = repr(e)
    return res


SEEN_KEEP_DAYS = 45


def row_key(r: dict) -> str:
    """One window's identity: the event AND both times, so a window whose real
    or Kalshi time moves is news again. UTC, so a source switch (Nasdaq ->
    override) that names the same instant is not."""
    return "{}|{}|{}".format(
        r["event"], r["real"].astimezone(timezone.utc).isoformat(),
        r["kalshi_start"].astimezone(timezone.utc).isoformat())


def new_rows(res: dict, now_utc: datetime, path: str, save: bool) -> list:
    """Rows of `res` never alerted before, per the seen file at `path`. With
    `save`, records them and forgets keys older than SEEN_KEEP_DAYS. Never
    raises: an unreadable seen file means alerting again, not going quiet."""
    try:
        with open(path, encoding="utf-8") as f:
            seen = json.load(f) or {}
        if not isinstance(seen, dict):
            seen = {}
    except (OSError, ValueError):
        seen = {}
    new = [r for r in res.get("rows") or [] if row_key(r) not in seen]
    if save:
        try:
            for r in new:
                seen[row_key(r)] = now_utc.isoformat()
            keep = now_utc - timedelta(days=SEEN_KEEP_DAYS)
            seen = {k: v for k, v in seen.items()
                    if (imm.parse_iso_utc(str(v)) or now_utc) >= keep}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(seen, f, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except Exception as e:                   # noqa: BLE001 -- advisory
            imm.log("! pick-off seen file not written: {}".format(e))
    return new


# ---------------------------------------------------------------------------
# Rendering. Plain text is ASCII-only on purpose: the IMM email tasks log their
# text body to a cp1252-redirected stdout, and one non-cp1252 glyph kills the
# run before it sends (2026-09-21). HTML carries entities instead.
# ---------------------------------------------------------------------------

def _short(ev: str) -> str:
    return ev[2:] if ev.startswith("KX") else ev


def fmt_et(dt: datetime, date_only: bool = False) -> str:
    """'Tue Sep 29 17:00 ET'; the date alone when no hour is known."""
    e = dt.astimezone(ET)
    return e.strftime("%a %b %d") if date_only else e.strftime("%a %b %d %H:%M ET")


_PROXY_HOW = {
    "after_close": "after the close - ~4pm ET release, call time not confirmed",
    "before_open": "before the open - ~7am ET release, call time not confirmed",
    "no_hour": "no time published",
}


def real_when(r: dict) -> str:
    return fmt_et(r["real"], date_only=r["kind"] == "no_hour")


def real_how(r: dict) -> str:
    """How we know the real time: a release proxy is never passed off as the
    call itself (the call can follow it by hours). An override taken from the
    company's own announcement names the call, so its label is shown as is."""
    label = r.get("label") or ""
    if label.startswith("announced:"):
        how = label
    else:
        how = (_PROXY_HOW.get(r["kind"]) or label
               or ("call time" if r["kind"] == "call" else "our start time"))
    return "{}; {}".format(how, r["source"])


def real_str(r: dict) -> str:
    """What time the event is, with how we know it, in one phrase."""
    return "{} ({})".format(real_when(r), real_how(r))


def _status(r: dict, now_utc: datetime) -> str:
    if r["open_now"]:
        return "OPEN NOW"
    n = (r["real"].astimezone(ET).date() - now_utc.astimezone(ET).date()).days
    return {0: "TODAY", 1: "TOMORROW"}.get(n, "in {}d".format(n))


def _gap(h: float) -> str:
    return "~{:.0f}h".format(h) if h < 72 else "~{:.1f}d".format(h / 24.0)


def _title(r: dict) -> str:
    return r["title"] or _short(r["event"])


def error_text(res: dict) -> str:
    """The failure, said out loud, or "". A silent failure would look exactly
    like "no windows today". Kept apart from the block so a caller can put
    it at the bottom rather than in the headline slot."""
    if not res.get("error"):
        return ""
    return ("{} CHECK could not run ({}); Kalshi event starts were not "
            "compared this time.".format(HEADER, res["error"]))


def text_lines(res: dict, now_utc: datetime) -> list:
    """The plain-text block; [] when there is nothing to say (see
    error_text for the failure case)."""
    rows = res.get("rows") or []
    if not rows:
        return []
    out = ["{}{} - Kalshi thinks the event starts LATER than it really does, "
           "so orders set to expire \"at event start\" are still resting while "
           "it happens:".format(HEADER, "S" if len(rows) > 1 else "")]
    for r in rows:
        out.append("  {} ({}) - {}".format(_title(r), _short(r["event"]),
                                           _status(r, now_utc)))
        out.append("    Event is:      " + real_str(r))
        out.append("    Kalshi thinks: {} - \"at event start\" orders stay up "
                   "until then".format(fmt_et(r["kalshi_start"])))
        out.append("    Window: up to {}, or until Kalshi closes the markets "
                   "after the real event{}".format(
                       _gap(r["gap_h"]),
                       "" if r["live_mkts"] >= 0
                       else " (market status unread)"))
    out.append("  Confirm the time on the company IR page / release before "
               "trading on it.")
    return out


def html_block(res: dict, now_utc: datetime) -> str:
    """HTML twin of text_lines; "" when there is nothing to say."""
    rows = res.get("rows") or []
    if not rows:
        return ""
    td = "padding:5px 10px;border:1px solid #c7d7f5;text-align:left;"
    h = ['<div style="background:#eef4ff;border-left:4px solid #2563eb;'
         'padding:8px 10px;margin:8px 0">',
         '<div style="font-weight:700;color:#1e3a8a">Pick-off window{} '
         '&mdash; Kalshi thinks the event starts LATER than it really does, so '
         'orders set to expire &ldquo;at event start&rdquo; are still resting '
         'while it happens</div>'.format("s" if len(rows) > 1 else ""),
         '<table style="border-collapse:collapse;margin-top:6px;'
         'background:#fff">',
         '<tr style="background:#dbe6fd;font-weight:600">'
         '<td style="{0}">EVENT</td><td style="{0}">EVENT IS</td>'
         '<td style="{0}">KALSHI THINKS</td><td style="{0}">WINDOW</td>'
         '</tr>'.format(td)]
    for r in rows:
        h.append(
            '<tr><td style="{td}"><b>{title}</b><div style="color:#666;'
            'font-size:11px">{ev} &middot; <b>{status}</b></div></td>'
            '<td style="{td}"><b>{real}</b><div style="color:#666;'
            'font-size:11px">{how}</div></td>'
            '<td style="{td}"><b>{kal}</b><div style="color:#666;'
            'font-size:11px">&ldquo;at event start&rdquo; orders stay up until '
            'then</div></td>'
            '<td style="{td}">up to {gap}<div style="color:#666;font-size:11px">'
            'or until Kalshi closes the markets after the real event{unk}'
            '</div></td></tr>'.format(
                td=td, title=_esc(_title(r)), ev=_esc(_short(r["event"])),
                status=_status(r, now_utc), real=real_when(r),
                how=_esc(real_how(r)),
                kal=fmt_et(r["kalshi_start"]), gap=_gap(r["gap_h"]),
                unk="" if r["live_mkts"] >= 0 else " (market status unread)"))
    h.append('</table><div style="color:#555;font-size:11px;margin-top:4px">'
             'Confirm the time on the company IR page / release before '
             'trading on it.</div></div>')
    return "".join(h)


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def kalshi_note(ours: datetime, kalshi_start, is_pickoff: bool) -> str:
    """One sentence for a row that compares OUR time with Kalshi's elsewhere
    in an email (the digest's cutoff audit); "" when Kalshi has no start for
    it. Says which way the gap cuts for "at event start" orders."""
    if kalshi_start is None:
        return ""
    k = fmt_et(kalshi_start)
    if is_pickoff:
        return ("Kalshi thinks the event starts {} - AFTER the real one, so "
                "\"at event start\" orders are still resting through it: see "
                "{} at the top.".format(k, HEADER))
    if kalshi_start < ours:
        return ("Kalshi thinks the event starts {}, before ours: \"at event "
                "start\" orders are pulled then, so there is nothing to pick "
                "off.".format(k))
    return "Kalshi thinks the event starts {}.".format(k)
