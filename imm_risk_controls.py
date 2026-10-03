"""Risk controls for the IMM section of the 7:00 portfolio email.

Jack 2026-10-03: "in 'Capacity — how close to each ceiling' in my daily
email, show all risk controls and if any have been breached e.g. the balance
guard, or other. or are close to breaching. make it clear which risk controls
are actually constraining / have real impact vs not".

Every control the bot has, in four kinds:

  HALT       stops ALL quoting until the next roll (account-value guard,
             daily loss, fail-safe, the manual HALT file), or a whole tier
             (the open-scan loss budget)
  CAPACITY   caps how much the bot can quote (events, collateral budget,
             candidate books, resting orders, placements per cycle)
  POSITION   caps inventory (per market, per event)
  GUARD      stands one market or event down (toxic flow, the event fill
             tripwire, the live-event / depth gate, fair-value and data
             gates, blackouts, cutoffs, manual stand-aside, breakers)

and one verdict each, over a window (24h for the email):

  TRIPPED    fired: a halt cancelled the book (or the tier's)
  BINDING    limited what the bot quoted (markets refused, placements
             deferred, positions held at a cap)
  CLOSE      reached 80% of its limit at the worst point, without binding
  SLACK      never near its limit: not constraining anything
  FIRED      a per-market guard stood something down (working as designed)
  QUIET      a per-market guard that did nothing in the window
  OFF        disabled

The evidence is the bot's own record, never a re-derivation: ALERT lines,
the once-per-cycle summary and "risk:" lines (incentive_mm.risk_line), the
selection gate's "universe:" lines and the placement-cap lines in the bot
logs, plus the toxic_halts and guard_skips sinks. Pure functions over that
evidence; the caller (send_imm_digest) supplies the live limits, positions
and balance, so this module never imports incentive_mm.
"""
import ast
import glob
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

WINDOW_SECS = 24 * 3600.0
CLOSE_FRAC = 0.80        # CLOSE from 80% of a limit
# A cap reads BINDING only if it bound in the window's last 2h. Earlier
# binding is reported in its impact text, so a limit raised mid-window (the
# 10/3 budget raise) does not read BINDING until the next day's email.
RECENT_SECS = 2 * 3600.0
AT_CAP_FRAC = 0.95       # a position this close to its cap holds that side

STATUS_ORDER = ("TRIPPED", "BINDING", "CLOSE", "SLACK", "FIRED", "QUIET", "OFF")
STATUS_COLOUR = {"TRIPPED": "#c0392b", "BINDING": "#d35400", "CLOSE": "#b7950b",
                 "SLACK": "#0a7a2f", "FIRED": "#2e5c8a", "QUIET": "#777",
                 "OFF": "#999"}
STATUS_MEANING = {
    "TRIPPED": "fired and cancelled the book",
    "BINDING": "limited what the bot quoted",
    "CLOSE": f"reached {CLOSE_FRAC:.0%} of its limit",
    "SLACK": "not constraining",
    "FIRED": "stood markets down, as designed",
    "QUIET": "did nothing",
    "OFF": "disabled"}

# ---- log line shapes (incentive_mm writes all of these) ----------------------
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z \[IMM\] (.*)$")
ALERT_RE = re.compile(r"^ALERT \[([a-z_]+)\] (.*)$")
CYCLE_RE = re.compile(r"^\d+/\d+ mkts quoted, (\d+) resting, \d+ amend, \d+ cancel, "
                      r"(\d+) place, .*?P&L today \$([-+]?[\d,.]+)")
UNIVERSE_RE = re.compile(
    r"universe: (\d+) program markets -> (\d+) candidates -> (\d+) selected "
    r"across (\d+)/(\d+) events.*?~\$(\d+), total ~\$(\d+) ladder collateral, "
    r"\$(\d+) inventory reserve\); skips (\{.*\})")
PLACE_CAP_RE = re.compile(r"^placement cap (\d+)/cycle reached; (\d+) deferred")
RENEWAL_RE = re.compile(r"^placement cap: (\d+) identical renewal")
AMEND_CAP_RE = re.compile(r"^amend cap: (\d+)")
CYCLE_ERR_RE = re.compile(r"^! cycle error #(\d+)")
IDLE_RE = re.compile(r"^daily-loss halt active until (\S+ \S+); idle")
RISK_ACCT_RE = re.compile(r"account value \$([\d,]+) \(anchor \$([\d,]+), (down|up) "
                          r"\$([\d,]+) of \$([\d,]+) halt\)")
RISK_PNL_RE = re.compile(r"P&L today \$([-+][\d,.]+) of -\$([\d,]+) halt")
RISK_SCAN_RE = re.compile(r"open-scan \$([-+][\d,.]+) of -\$([\d,]+) budget")
# numbers out of the halt alerts' own wording
DROP_RE = re.compile(r"dropped \$([\d,.]+)")
LIMIT_RE = re.compile(r">= \$([\d,.]+)")
CANCELLED_RE = re.compile(r"cancelled (\d+)")

# guard_skips "guard" names, grouped into the rows below. Anything not named
# here lands in "Other stand-downs" so nothing the bot does goes unreported.
GATE_GUARDS = ("treasury_yield", "rain_monthly", "rain_fair", "quake", "quake_hold",
               "vercel_fair", "or_fair", "share_fair", "mort_fair", "gb_fair",
               "ca_fair", "dc_count", "poke_fair", "manual_yield")
BLACKOUT_GUARDS = ("cpi_blackout", "aaa_blackout")
DEPTH_GUARDS = ("event_depth_trip", "event_depth_hold")
CUTOFF_GUARDS = ("closing", "cutoff_passed", "cutoff_extra")
BREAKER_GUARDS = ("move_breaker", "fill_burst", "one_sided_breaker",
                  "breaker_cooldown")
SCAN_TRIP_GUARDS = ("scan_fill_tripwire", "scan_mid_tripwire", "scan_evicted")
QUOTING_RULES = ("band_both_out", "cannot_qualify", "wide_spread")   # not risk


def _num(s) -> float:
    return float(str(s).replace(",", ""))


def _ts(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc).timestamp()


def _empty_evidence(since: float, until: float) -> dict:
    return {"since": since, "until": until, "lines": 0, "alerts": defaultdict(list),
            "cycles": 0, "resting_max": None, "pnl_min": None, "acct_worst": None,
            "pnl_last": None, "scan_last": None, "acct_last": None,
            "acct_lines": 0, "scan_min": None, "universe": [], "place_cap": [],
            "renewals_kept": 0, "amend_cap": [], "cycle_errors_max": 0,
            "failsafe": [], "idle": [], "halt_file": [],
            "toxic": {"pickoffs": 0, "pick_markets": set(), "side_halts": [],
                      "event_halts": [], "idled_reward": 0.0, "read": False},
            "guards": defaultdict(set), "guards_read": False}


def scan_log_lines(lines, ev: dict) -> None:
    """Fold bot log lines into the evidence dict `ev` (see gather). Lines
    outside [since, until] and anything that is not a bot [IMM] line (the
    launcher writes LOCAL time with a fake Z) are ignored."""
    since_s = datetime.fromtimestamp(ev["since"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    until_s = datetime.fromtimestamp(ev["until"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    idle_open = None                      # [first_ts, last_ts, until_str]
    for line in lines:
        # cheap string screens first: the logs run ~75 MB a day
        if line[:19] < since_s or line[:19] > until_s or "[IMM] " not in line[:28]:
            continue
        m = LINE_RE.match(line.rstrip("\r\n"))
        if not m:
            continue
        ts, msg = _ts(m.group(1)), m.group(2)
        ev["lines"] += 1
        if msg.startswith("ALERT ["):
            a = ALERT_RE.match(msg)
            if a:
                ev["alerts"][a.group(1)].append((ts, a.group(2)))
            continue
        if msg.startswith("risk: "):
            r = RISK_ACCT_RE.search(msg)
            if r:
                ev["acct_lines"] += 1
                drop = _num(r.group(4)) * (1 if r.group(3) == "down" else -1)
                if ev["acct_worst"] is None or drop > ev["acct_worst"][0]:
                    ev["acct_worst"] = (drop, ts, _num(r.group(1)), _num(r.group(2)))
                if ev["acct_last"] is None or ts >= ev["acct_last"][1]:
                    ev["acct_last"] = (drop, ts, _num(r.group(1)), _num(r.group(2)))
            r = RISK_SCAN_RE.search(msg)
            if r:
                v = _num(r.group(1))
                if ev["scan_min"] is None or v < ev["scan_min"][0]:
                    ev["scan_min"] = (v, ts)
                if ev["scan_last"] is None or ts >= ev["scan_last"][1]:
                    ev["scan_last"] = (v, ts)
            continue
        c = CYCLE_RE.match(msg)
        if c:
            if "[fast]" not in msg:
                ev["cycles"] += 1
            n_rest, pnl = int(c.group(1)), _num(c.group(3))
            if ev["resting_max"] is None or n_rest > ev["resting_max"][0]:
                ev["resting_max"] = (n_rest, ts)
            if ev["pnl_min"] is None or pnl < ev["pnl_min"][0]:
                ev["pnl_min"] = (pnl, ts)
            if ev["pnl_last"] is None or ts >= ev["pnl_last"][1]:
                ev["pnl_last"] = (pnl, ts)
            continue
        if msg.startswith("universe: "):
            u = UNIVERSE_RE.match(msg)
            if u:
                try:
                    skips = ast.literal_eval(u.group(9))
                except (ValueError, SyntaxError):
                    skips = {}
                ev["universe"].append({
                    "ts": ts, "candidates": int(u.group(2)), "markets": int(u.group(3)),
                    "events": int(u.group(4)), "event_cap": int(u.group(5)),
                    "reserved": float(u.group(7)) + float(u.group(8)),
                    "ladder": float(u.group(7)), "reserve": float(u.group(8)),
                    "skips": skips if isinstance(skips, dict) else {}})
            continue
        if msg.startswith("placement cap"):
            p = PLACE_CAP_RE.match(msg)
            if p:
                ev["place_cap"].append((ts, int(p.group(1)), int(p.group(2))))
            r = RENEWAL_RE.match(msg)
            if r:
                ev["renewals_kept"] += int(r.group(1))
            continue
        a = AMEND_CAP_RE.match(msg)
        if a:
            ev["amend_cap"].append((ts, int(a.group(1))))
            continue
        e = CYCLE_ERR_RE.match(msg)
        if e:
            ev["cycle_errors_max"] = max(ev["cycle_errors_max"], int(e.group(1)))
            continue
        if msg.startswith("fail-safe: cancelling"):
            ev["failsafe"].append(ts)
            continue
        if msg.startswith("HALT file present"):
            ev["halt_file"].append(ts)
            continue
        i = IDLE_RE.match(msg)
        if i:
            # one halt = one "until" (the roll it waits for), however long
            # the gaps between its idle lines (a restart mid-halt included)
            if idle_open and idle_open[2] == i.group(1):
                idle_open[1] = ts
            else:
                idle_open = [ts, ts, i.group(1)]
                ev["idle"].append(idle_open)


def scan_toxic(records, ev: dict) -> None:
    """Fold toxic_halts sink records (pickoff / side_halt / event_halt)."""
    t = ev["toxic"]
    t["read"] = True
    for r in records:
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if not ev["since"] <= ts <= ev["until"]:
            continue
        kind = r.get("kind")
        if kind == "pickoff":
            t["pickoffs"] += 1
            t["pick_markets"].add(r.get("ticker"))
        elif kind in ("side_halt", "event_halt"):
            t["side_halts" if kind == "side_halt" else "event_halts"].append(r)
            try:
                secs = max(0.0, float(r.get("until") or ts) - ts)
                t["idled_reward"] += float(r.get("est_per_day") or 0) * secs / 86400.0
            except (TypeError, ValueError):
                pass


def scan_guards(records, ev: dict) -> None:
    """Fold guard_skips sink records: the markets each guard stood down."""
    ev["guards_read"] = True
    since_iso = datetime.fromtimestamp(ev["since"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    until_iso = datetime.fromtimestamp(ev["until"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for r in records:
        if r.get("kind") != "enter":
            continue
        ts = str(r.get("ts") or "")[:19]
        if not since_iso <= ts <= until_iso:
            continue
        g = r.get("guard")
        if g:
            ev["guards"][g].add(r.get("ticker"))


def _iter_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
    except OSError:
        return


def _day_files(status_dir: str, prefix: str, since: float, until: float):
    """STATUS_DIR/<prefix>_YYYY-MM-DD.jsonl for every UTC date in the window."""
    lo = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d")
    hi = datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%d")
    out = []
    for p in sorted(glob.glob(os.path.join(status_dir, f"{prefix}_*.jsonl"))):
        day = os.path.basename(p)[len(prefix) + 1:-len(".jsonl")]
        if lo <= day <= hi:
            out.append(p)
    return out


def gather(status_dir: str, now_ts: float, window_secs: float = WINDOW_SECS) -> dict:
    """The window's evidence from the bot's logs and sinks. Never raises: an
    unreadable source leaves its part empty and the rows say so."""
    ev = _empty_evidence(now_ts - window_secs, now_ts)
    # Log files are named for the LAUNCHER's local start date and one file
    # can run for days, so pick by modification time, not by name.
    for path in sorted(glob.glob(os.path.join(status_dir, "incentive-mm-*.log"))):
        try:
            if os.path.getmtime(path) < ev["since"]:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                scan_log_lines(f, ev)
        except OSError:
            continue
    for path in _day_files(status_dir, "toxic_halts", ev["since"], ev["until"]):
        scan_toxic(_iter_jsonl(path), ev)
    for path in _day_files(status_dir, "guard_skips", ev["since"], ev["until"]):
        scan_guards(_iter_jsonl(path), ev)
    return ev


# ---- rows ----------------------------------------------------------------------

def _money(v: float) -> str:
    return "${:,.0f}".format(v)


def _int(v: float) -> str:
    return "{:,.0f}".format(v)


def _pct_status(worst, limit):
    if worst is None or not limit:
        return None
    return "CLOSE" if worst >= CLOSE_FRAC * limit else "SLACK"


def _row(kind, name, status, limit="", worst="", now="", impact="", pct=None):
    return {"kind": kind, "name": name, "status": status, "limit": limit,
            "worst": worst, "now": now, "impact": impact, "pct": pct}


def _when(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).strftime("%a %H:%M")


def _span(secs: float) -> str:
    return f"{secs / 3600:.1f}h" if secs >= 3600 else f"{secs / 60:.0f} min"


def _idle_after(ev, ts):
    """(idle seconds, until text) of the halt idle span that began with a
    halt fired at `ts`, or (None, None)."""
    for first, last, until in ev["idle"]:
        if ts - 60 <= first <= ts + 600:
            try:
                until_ts = datetime.fromisoformat(until).timestamp()
            except ValueError:
                until_ts = last
            return max(min(until_ts, ev["until"]) - ts, last - ts), until_ts
    return None, None


def _trip_text(ev, ts, msg, tz, what):
    d, lim, n = DROP_RE.search(msg), LIMIT_RE.search(msg), CANCELLED_RE.search(msg)
    idle, until_ts = _idle_after(ev, ts)
    bits = [f"{_when(ts, tz)} ET: {what}"]
    if d:
        bits.append(f"down {_money(_num(d.group(1)))}"
                    + (f" vs the {_money(_num(lim.group(1)))} limit" if lim else ""))
    if n:
        bits.append(f"{int(n.group(1)):,} orders cancelled")
    if idle:
        bits.append(f"no quoting for {_span(idle)}"
                    + (f", to {_when(until_ts, tz)} ET" if until_ts else ""))
    return ", ".join(bits)


def halt_rows(ev: dict, ctx: dict, tz) -> list:
    caps, now = ctx["caps"], ctx["now"]
    rows = []

    # -- the account-value guard (the "balance guard") --------------------
    lim = caps.get("ACCOUNT_DROP_HALT") or 0.0
    name = "Account-value guard (balance guard)"
    if lim <= 0:
        rows.append(_row("HALT", name, "OFF", "off (IMM_ACCOUNT_DROP_HALT=0)"))
    else:
        trips = ev["alerts"].get("balance_floor", [])
        worst = ev["acct_worst"]
        value, anchor = now.get("acct_value"), now.get("acct_anchor")
        now_drop = (anchor - value) if (value is not None and anchor) else None
        w_txt = ("not logged yet" if not worst else
                 "never below the anchor" if worst[0] <= 0 else
                 f"down {_money(worst[0])} at {_when(worst[1], tz)} ET")
        n_txt = (f"{'down' if now_drop >= 0 else 'up'} {_money(abs(now_drop))}"
                 if now_drop is not None else "n/a")
        if trips:
            impact = "; ".join(
                _trip_text(ev, ts, msg, tz,
                           "TRIPPED on CASH (the old rule)" if "ACCOUNT balance" in msg
                           else "TRIPPED") for ts, msg in trips)
            status = "TRIPPED"
        else:
            base = worst[0] if worst else now_drop
            status = _pct_status(max(base, 0.0) if base is not None else None, lim) or "SLACK"
            impact = ("cash + positions vs the day's anchor"
                      + (f" (now {_money(value)} vs {_money(anchor)})"
                         if now_drop is not None else "")
                      + "; halts and cancels everything until the 6am ET roll")
        rows.append(_row("HALT", name, status,
                         f"{_money(lim)} drop/day", w_txt, n_txt, impact,
                         (max(worst[0], 0) / lim) if worst else
                         ((max(now_drop, 0) / lim) if now_drop is not None else None)))

    # -- daily loss ----------------------------------------------------------
    lim = caps.get("DAILY_LOSS_LIMIT") or 0.0
    name = "Daily loss halt"
    if lim <= 0:
        rows.append(_row("HALT", name, "OFF", "off"))
    else:
        trips = ev["alerts"].get("loss_halt", [])
        pmin = ev["pnl_min"]
        worst = max(-pmin[0], 0.0) if pmin else None
        # The halt's own P&L (the cycle line's, carry included) beats the
        # status file's pnl_today, which omits the restart carry and reads
        # ~0 after every restart (-$14 vs the halt's -$820 on 10/3).
        pnl_now = ev["pnl_last"][0] if ev["pnl_last"] else now.get("pnl_today")
        w_txt = (f"{pmin[0]:+,.0f} at {_when(pmin[1], tz)} ET" if pmin else "n/a")
        n_txt = f"{pnl_now:+,.0f}" if pnl_now is not None else "n/a"
        if trips:
            status = "TRIPPED"
            impact = "; ".join(_trip_text(ev, ts, msg, tz, "TRIPPED") for ts, msg in trips)
        else:
            status = _pct_status(worst, lim) or "SLACK"
            impact = "IMM P&L today (realized + marked) since the 6am ET roll"
        rows.append(_row("HALT", name, status, f"-{_money(lim)}/day", w_txt, n_txt,
                         impact, (worst / lim) if worst is not None else None))

    # -- the open-scan tier's own budget ---------------------------------------
    lim = caps.get("SCAN_DAILY_LOSS_LIMIT") or 0.0
    name = "Open-scan tier loss budget"
    if lim <= 0 or (caps.get("SCAN_TOP_N") or 0) <= 0:
        rows.append(_row("HALT", name, "OFF",
                         "off" + (" (tier off)" if (caps.get("SCAN_TOP_N") or 0) <= 0 else "")))
    else:
        trips = ev["alerts"].get("scan_halt", [])
        smin = ev["scan_min"]
        worst = max(-smin[0], 0.0) if smin else None
        s_now = ev["scan_last"][0] if ev["scan_last"] else now.get("scan_pnl")
        if trips or now.get("scan_halted"):
            status = "TRIPPED"
            impact = "; ".join(_trip_text(ev, ts, msg, tz, "TRIPPED: tier closed")
                               for ts, msg in trips) or "tier closed until the roll"
        else:
            status = _pct_status(worst if worst is not None else
                                 (max(-s_now, 0.0) if s_now is not None else None), lim) or "SLACK"
            impact = "closes the open-scan tier only; the rest of the book quotes on"
        rows.append(_row("HALT", name, status, f"-{_money(lim)}/day",
                         f"{smin[0]:+,.0f} at {_when(smin[1], tz)} ET" if smin else "not logged yet",
                         f"{s_now:+,.0f}" if s_now is not None else "n/a", impact,
                         (worst / lim) if worst is not None else None))

    # -- fail-safe ------------------------------------------------------------
    lim = caps.get("FAILSAFE_CANCEL_AFTER") or 0
    trips = ev["failsafe"] or [ts for ts, _m in ev["alerts"].get("failsafe", [])]
    worst = ev["cycle_errors_max"]
    if trips:
        status = "TRIPPED"
        impact = "; ".join(f"{_when(t, tz)} ET: {lim} cycle errors in a row, every order "
                           f"cancelled" for t in trips)
    else:
        status = "CLOSE" if lim and worst >= max(lim - 1, 1) else "SLACK"
        impact = "cancels everything after consecutive failed cycles (API down)"
    rows.append(_row("HALT", "Fail-safe (consecutive cycle errors)", status,
                     f"{lim} in a row", f"{worst} in a row", f"{now.get('errors_today', 0)} today",
                     impact, (worst / lim) if lim else None))

    # -- manual kill switch ----------------------------------------------------
    if now.get("halt_file") or ev["halt_file"]:
        first = ev["halt_file"][0] if ev["halt_file"] else None
        rows.append(_row("HALT", "Manual HALT file", "TRIPPED", "kill switch",
                         "", "present" if now.get("halt_file") else "removed",
                         ("since " + _when(first, tz) + " ET" if first else "present now")
                         + ": orders cancelled, bot idle"))
    else:
        rows.append(_row("HALT", "Manual HALT file", "SLACK", "kill switch",
                         "", "absent", "not engaged (a HALT file in run-logs idles the bot)"))
    return rows


def _earlier(ev, last_ts, tz) -> str:
    return f"bound earlier (last {_when(last_ts, tz)} ET), not in the last {RECENT_SECS / 3600:.0f}h"


def capacity_rows(ev: dict, ctx: dict, tz) -> list:
    caps, now = ctx["caps"], ctx["now"]
    rows = []
    uni = ev["universe"]
    last = uni[-1] if uni else None
    n_ref = len(uni)
    recent = ev["until"] - RECENT_SECS

    # -- events -----------------------------------------------------------------
    lim = caps.get("MAX_MARKETS") or 0
    if uni and lim:
        worst = max(u["events"] for u in uni)
        at_cap = [u["ts"] for u in uni if u["events"] >= lim]
        status = "BINDING" if any(t >= recent for t in at_cap) else _pct_status(worst, lim)
        impact = ("new events refused at the cap; open events keep quoting"
                  if status == "BINDING" else
                  _earlier(ev, at_cap[-1], tz) if at_cap else "distinct events the gate may open")
        rows.append(_row("CAPACITY", "Events quoted", status, _int(lim) + " events",
                         _int(worst), _int(last["events"]), impact, worst / lim))

    # -- collateral budget --------------------------------------------------------
    lim = caps.get("COLLATERAL_BUDGET") or 0.0
    if uni and lim:
        worst = max(u["reserved"] for u in uni)
        refused = [u["skips"].get("budget", 0) or 0 for u in uni]
        hit = sum(1 for n in refused if n)
        hit_ts = [u["ts"] for u in uni if u["skips"].get("budget", 0)]
        if hit and hit_ts[-1] >= recent:
            status = "BINDING"
            impact = (f"{refused[-1]:,} new markets refused at the last refresh; refused in "
                      f"{hit} of {n_ref} refreshes (up to {max(refused):,}). A modelled "
                      f"reservation (ladder + inventory reserve), not cash: markets "
                      f"already quoting keep their place")
        elif hit:
            status = _pct_status(worst, lim)
            impact = (_earlier(ev, hit_ts[-1], tz) + f": refused new markets in {hit} of "
                      f"{n_ref} refreshes (up to {max(refused):,}). Modelled reservation, "
                      f"not cash")
        else:
            status = _pct_status(worst, lim)
            impact = "modelled reservation (ladder + inventory reserve), not cash"
        rows.append(_row("CAPACITY", "Collateral budget (modelled)", status, _money(lim),
                         _money(worst), _money(last["reserved"]), impact, worst / lim))

    # -- candidate books ------------------------------------------------------------
    lim = caps.get("MAX_CANDIDATE_BOOKS") or 0
    if uni and lim:
        worst = max(u["candidates"] for u in uni)
        at_cap = [u["ts"] for u in uni if u["candidates"] >= lim]
        status = "BINDING" if any(t >= recent for t in at_cap) else _pct_status(worst, lim)
        impact = ("the lowest-$ candidates are dropped unread at the cap"
                  if status == "BINDING" else _earlier(ev, at_cap[-1], tz) if at_cap else
                  "candidates read per refresh; past the cap the lowest-$ go unread")
        rows.append(_row("CAPACITY", "Candidate books read", status, _int(lim),
                         _int(worst), _int(last["candidates"]), impact, worst / lim))
    # -- slot caps: strikes per event, and the two opportunistic tiers -----------
    if uni:
        # scan_top_n also counts candidates under the tier's ROI floor
        # (SCAN_MIN_ROI): the group walk returns both as one cut
        slots = (("event_top_n", "per-event strike cap"),
                 ("scan_top_n", "open-scan slots or ROI floor"),
                 ("finecon_top_n", "finecon slots"))
        last_n = [(lbl, last["skips"].get(k, 0) or 0) for k, lbl in slots]
        hit_ts = [u["ts"] for u in uni if any(u["skips"].get(k, 0) for k, _l in slots)]
        hit = len(hit_ts)
        if any(n for _l, n in last_n):
            status = "BINDING"
            impact = ("refused at the last refresh: "
                      + ", ".join(f"{lbl} {n:,}" for lbl, n in last_n if n)
                      + f" (in {hit} of {n_ref} refreshes)")
        elif hit and hit_ts[-1] >= recent:
            status = "BINDING"
            impact = f"refused markets in {hit} of {n_ref} refreshes, none at the last"
        elif hit:
            status = "SLACK"
            impact = _earlier(ev, hit_ts[-1], tz) + f": refused markets in {hit} of {n_ref} refreshes"
        else:
            status = "SLACK"
            impact = "never refused a market"
        lim_txt = " / ".join(f"{lbl} {caps[k]}" for k, lbl in (("SCAN_TOP_N", "scan"),
                                                                ("FINECON_TOP_N", "finecon"))
                             if caps.get(k) is not None)
        rows.append(_row("CAPACITY", "Slot caps (strikes per event, scan, finecon)", status,
                         lim_txt, "", "", impact))
    if not uni:
        rows.append(_row("CAPACITY", "Selection gate (events / budget / candidates)",
                         "SLACK", "", "n/a", "n/a",
                         "no universe line in the window's logs; nothing to judge"))

    # -- resting orders ----------------------------------------------------------------
    lim = caps.get("MAX_TOTAL_RESTING_ORDERS") or 0
    if lim:
        hits = ev["alerts"].get("order_cap", [])
        rmax = ev["resting_max"]
        r_now = now.get("resting_orders")
        worst = max(rmax[0] if rmax else 0, r_now or 0) if (rmax or r_now is not None) else None
        if hits and hits[-1][0] >= recent:
            status = "BINDING"
            impact = (f"cap hit {len(hits)}x (first {_when(hits[0][0], tz)} ET): new "
                      f"placements blocked until orders fill or cancel")
        else:
            status = _pct_status(worst, lim) or "SLACK"
            impact = (_earlier(ev, hits[-1][0], tz) + f": cap hit {len(hits)}x" if hits
                      else "past the cap no new order is placed anywhere")
        note = now.get("resting_notional")
        if note:
            impact += f"; resting notional {_money(note)} if every order filled"
        rows.append(_row("CAPACITY", "Resting orders", status, _int(lim),
                         _int(worst) if worst is not None else "n/a",
                         _int(r_now) if r_now is not None else "n/a", impact,
                         (worst / lim) if worst is not None else None))

    # -- placements per cycle ----------------------------------------------------------------
    lim = caps.get("MAX_PLACEMENTS_PER_CYCLE") or 0
    if lim:
        hits = ev["place_cap"]
        rate = caps.get("PLACE_RATE_PER_SEC") or 0
        lim_txt = f"{_int(lim)}/cycle"
        if hits:
            cyc = ev["cycles"]
            status = "BINDING" if hits[-1][0] >= recent else "SLACK"
            pushed = sum(d for _t, _c, d in hits)
            mins = (ev["until"] - ev["since"]) / 60.0 / cyc if cyc else None
            impact = (f"hit in {len(hits)}" + (f" of {cyc:,}" if cyc >= len(hits) else "")
                      + f" cycles (placements paced at {rate:g}/s): on those cycles "
                      f"~{pushed / len(hits):,.0f} placements (up to "
                      f"{max(d for _t, _c, d in hits):,}) waited for the next cycle"
                      + (f" (~{mins:.0f} min)" if mins else "")
                      + (f"; {ev['renewals_kept'] / pushed:.0%} were renewals whose old "
                         f"order kept resting" if pushed and ev["renewals_kept"] else ""))
            if status == "SLACK":
                impact = _earlier(ev, hits[-1][0], tz) + ": " + impact
        else:
            status = "SLACK"
            impact = "never hit: every wanted order went out the same cycle"
        if ev["amend_cap"]:
            impact += f"; amend cap hit {len(ev['amend_cap'])}x"
        rows.append(_row("CAPACITY", "Placements per cycle", status, lim_txt,
                         f"{len(hits)} cycles at cap", "", impact,
                         1.0 if hits else None))
    return rows


def position_rows(ev: dict, ctx: dict, tz) -> list:
    """Per-market and per-event caps, each judged against ITS OWN cap (the
    bot's family-aware series_max_position / event_cap_contracts, resolved by
    the caller) — a sports ladder's cap is 5x the global 150."""
    now = ctx["now"]
    rows = []
    for kind, items, label, unit in (
            ("market", now.get("positions") or [], "Per-market position cap", "cts"),
            ("event", now.get("events") or [], "Per-event net cap", "cts")):
        items = [(k, abs(v), c) for k, v, c in items if c and c > 0]
        if not items:
            rows.append(_row("POSITION", label, "SLACK", "", "", "flat",
                             "no open positions"))
            continue
        k, v, c = max(items, key=lambda x: x[1] / x[2])
        at = [x for x in items if x[1] >= AT_CAP_FRAC * x[2]]
        over = [x for x in items if x[1] > x[2] + 0.5]
        worst_pct = v / c
        if at:
            status = "BINDING"
            impact = (f"{len(at)} {kind}{'s' if len(at) != 1 else ''} at the cap: "
                      f"the side that would add is not quoted")
            if over:
                impact += (f"; {len(over)} OVER it (e.g. {over[0][0]} {over[0][1]:,.0f} "
                           f"vs {over[0][2]:,.0f})")
        else:
            status = _pct_status(v, c)
            near = sum(1 for x in items if x[1] >= CLOSE_FRAC * x[2])
            impact = (f"{near} {kind}{'s' if near != 1 else ''} above "
                      f"{CLOSE_FRAC:.0%} of the cap, none at it; " if near else "")                 + f"worst: {k}"
        alerts = []
        if kind == "market":
            for cat in ("side_cap", "level_cap"):
                n = len(ev["alerts"].get(cat, []))
                if n:
                    alerts.append(f"{cat} backstop {n}x")
        if alerts:
            impact += "; " + ", ".join(alerts)
            if status in ("SLACK", "CLOSE"):
                status = "BINDING"
        # positions are read NOW (the state file); there is no 24h history
        rows.append(_row("POSITION", label, status, "per-series cap", "",
                         f"{v:,.0f} / {c:,.0f} ({worst_pct:.0%})",
                         impact + f"; {len(items):,} open", worst_pct))
    return rows


def guard_rows(ev: dict, ctx: dict, tz) -> list:
    caps, now = ctx["caps"], ctx["now"]
    g = ev["guards"]
    rows = []

    def markets(names):
        s = set()
        for n in names:
            s |= g.get(n, set())
        return s

    def top(names, k=4):
        lst = sorted(((len(g.get(n, ())), n) for n in names if g.get(n)), reverse=True)
        return ", ".join(f"{n} {c}" for c, n in lst[:k])

    # toxic flow
    t = ev["toxic"]
    if not caps.get("TOXIC_HALT", True):
        rows.append(_row("GUARD", "Toxic-flow halts", "OFF", "off (IMM_TOXIC_HALT=0)"))
    else:
        n_side, n_ev = len(t["side_halts"]), len(t["event_halts"])
        if n_side or n_ev:
            rows.append(_row(
                "GUARD", "Toxic-flow halts", "FIRED",
                f"{caps.get('TOXIC_PICKOFFS', 2)} pick-offs/24h",
                "", "",
                f"{n_side} side + {n_ev} event halts after {t['pickoffs']} pick-offs on "
                f"{len(t['pick_markets'])} markets; ~{_money(t['idled_reward'])} of modelled "
                f"reward idled"))
        else:
            rows.append(_row("GUARD", "Toxic-flow halts", "QUIET", "", "", "",
                             f"{t['pickoffs']} pick-offs, no halt" if t["read"] else
                             "no toxic_halts sink in the window"))

    # event fill tripwire
    fills = ev["alerts"].get("event_fill", [])
    perm = sum(1 for _t, m in fills if "PERMANENTLY" in m)
    lim = caps.get("EVENT_FILL_HALT_CONTRACTS") or 0
    if lim <= 0:
        rows.append(_row("GUARD", "Event fill tripwire", "OFF", "off"))
    else:
        evs = {m.split(":")[0] for _t, m in fills}
        rows.append(_row("GUARD", "Event fill tripwire", "FIRED" if fills else "QUIET",
                         f"{lim:g} cts/cycle", "", "",
                         (f"{len(evs)} event(s) stood down" + (f", {perm} permanently" if perm else ""))
                         if fills else "no gated event was swept"))

    # live-event / depth gate
    live = ev["alerts"].get("event_live", []) + ev["alerts"].get("event_depth", [])
    dm = markets(DEPTH_GUARDS)
    if live or dm:
        evs = {m.split(":")[0] for _t, m in live}
        rows.append(_row("GUARD", "Live-event / depth gate", "FIRED", "", "", "",
                         (f"{len(evs)} event(s) PERMANENTLY stood down; " if evs else "")
                         + f"depth trips/holds on {len(dm)} markets"))
    else:
        rows.append(_row("GUARD", "Live-event / depth gate", "QUIET", "", "", "",
                         "nothing tripped"))

    # fair-value and data gates
    gm = markets(GATE_GUARDS)
    rows.append(_row("GUARD", "Fair-value & data gates", "FIRED" if gm else "QUIET", "",
                     "", "", (f"{len(gm)} markets stood down: " + top(GATE_GUARDS)) if gm
                     else "nothing gated"))

    # blackouts
    bm = markets(BLACKOUT_GUARDS)
    rows.append(_row("GUARD", "Release blackouts (CPI, AAA)", "FIRED" if bm else "QUIET",
                     "", "", "", (f"{len(bm)} markets: " + top(BLACKOUT_GUARDS)) if bm
                     else "no blackout window in the window"))

    # cutoffs
    cm = markets(CUTOFF_GUARDS)
    uni = ev["universe"]
    past = (uni[-1]["skips"].get("cutoff", 0) or 0) if uni else 0
    rows.append(_row("GUARD", "Trade cutoffs & closing", "FIRED" if (cm or past) else "QUIET",
                     "", "", "",
                     (f"{past:,} markets past their cutoff at the last refresh"
                      if past else "") + (f"; {len(cm)} stood down mid-quote" if cm else "")
                     or "none"))

    # manual stand-aside (Jack's own orders)
    man = now.get("manual_standoff") or []
    man_skip = (uni[-1]["skips"].get("manual", 0) or 0) if uni else 0
    rows.append(_row("GUARD", "Manual stand-aside", "FIRED" if (man or man_skip) else "QUIET",
                     "", "", "", (f"{max(len(man), man_skip):,} markets where your own orders "
                                  f"rest; the bot stands aside") if (man or man_skip)
                     else "no manual orders in the bot's markets"))

    # watchdog / coverage (page only)
    wd = ev["alerts"].get("watchdog", []) + ev["alerts"].get("coverage", [])
    rows.append(_row("GUARD", "Watchdog & coverage alerts", "FIRED" if wd else "QUIET", "",
                     "", "", f"{len(wd)} page(s)" if wd else "no empty-book or one-sided page"))

    # breakers
    if caps.get("BREAKERS_ENABLED"):
        n = sum(len(ev["alerts"].get(c, [])) for c in ("move_breaker", "fill_burst", "one_sided"))
        bm = markets(BREAKER_GUARDS)
        rows.append(_row("GUARD", "Circuit breakers (mid move / fill burst / one-sided)",
                         "FIRED" if (n or bm) else "QUIET", "", "", "",
                         f"{n} trips on {len(bm)} markets" if (n or bm) else "nothing tripped"))
    else:
        rows.append(_row("GUARD", "Circuit breakers (mid move / fill burst / one-sided)",
                         "OFF", "", "", "", "off: IMM_BREAKERS=0"))

    # open-scan tripwires
    if any((caps.get(k) or 0) > 0 for k in ("SCAN_FILL_HALT_CONTRACTS", "SCAN_MID_JUMP_CENTS",
                                              "SCAN_DRIFT_CENTS")):
        n = len(ev["alerts"].get("scan_evict", []))
        rows.append(_row("GUARD", "Open-scan tripwires", "FIRED" if n else "QUIET", "", "", "",
                         f"{n} event(s) evicted" if n else "nothing evicted"))
    else:
        rows.append(_row("GUARD", "Open-scan tripwires (fill / mid jump / drift)", "OFF",
                         "", "", "", "off: IMM_SCAN_FILL_HALT / MID_JUMP / DRIFT = 0"))

    # everything else the bot stood down, so nothing is hidden
    named = set(GATE_GUARDS + BLACKOUT_GUARDS + DEPTH_GUARDS + CUTOFF_GUARDS + BREAKER_GUARDS
                + SCAN_TRIP_GUARDS + QUOTING_RULES + ("event_fill_tripwire",))
    other = [n for n in g if n not in named]
    if other:
        rows.append(_row("GUARD", "Other stand-downs", "FIRED", "", "", "",
                         f"{len(markets(other))} markets: " + top(other, 6)))
    return rows


def build_rows(ev: dict, ctx: dict, tz=timezone.utc) -> list:
    """All rows, account-wide controls first, worst verdict first."""
    main = halt_rows(ev, ctx, tz) + capacity_rows(ev, ctx, tz) + position_rows(ev, ctx, tz)
    main.sort(key=lambda r: STATUS_ORDER.index(r["status"]))
    guards = guard_rows(ev, ctx, tz)
    guards.sort(key=lambda r: STATUS_ORDER.index(r["status"]))
    return main + guards


def headline(rows: list) -> str:
    """One line: what fired, what binds, what is close; the rest has slack."""
    main = [r for r in rows if r["kind"] != "GUARD"]
    by = defaultdict(list)
    for r in main:
        by[r["status"]].append(r["name"])
    parts = []
    for st in ("TRIPPED", "BINDING", "CLOSE"):
        if by[st]:
            parts.append(f"{st}: " + ", ".join(by[st]))
    n_slack, n_off = len(by["SLACK"]), len(by["OFF"])
    parts.append(f"{n_slack} with slack (not constraining)" + (f", {n_off} off" if n_off else ""))
    fired = [r["name"] for r in rows if r["kind"] == "GUARD" and r["status"] == "FIRED"]
    if fired:
        parts.append(f"per-market guards fired: {len(fired)}")
    if not by["TRIPPED"] and not by["BINDING"] and not by["CLOSE"]:
        parts.insert(0, "Nothing fired or bound")
    return " | ".join(parts)


def window_note(ev: dict, tz) -> str:
    s = (f"last 24h: {_when(ev['since'], tz)} -> {_when(ev['until'], tz)} ET, from the "
         f"bot's own logs ({ev['lines']:,} lines, {ev['cycles']:,} full cycles)")
    if ev["lines"] and not ev["acct_lines"]:
        s += "; the account value is logged per cycle only from the 2026-10-03 risk-line deploy"
    if not ev["lines"]:
        s = "!! no bot log lines in the last 24h: every verdict below is blind"
    return s


def text_lines(rows: list, ev: dict, tz, caps_note: str) -> list:
    """Plain-text version: one line per control (verdict, name, limit /
    worst / now), its impact indented under it. A list, not a fixed-width
    table: the limits and impacts are prose and a table truncates them."""
    L = ["RISK CONTROLS & CAPACITY — what fired, what binds, what has slack",
         "  (" + window_note(ev, tz) + "; " + caps_note + ")",
         "  " + headline(rows)]
    guard_hdr = False
    for r in rows:
        if r["kind"] == "GUARD" and not guard_hdr:
            L.append("  -- per-market guards: stand one market or event down --")
            guard_hdr = True
        mark = "!!" if r["status"] in ("TRIPPED", "BINDING") else             ("! " if r["status"] == "CLOSE" else "  ")
        facts = [f"{k} {v}" for k, v in (("limit", r["limit"]), ("worst 24h", r["worst"]),
                                         ("now", r["now"])) if v]
        L.append("{}{:8s}{}{}".format(mark, r["status"], r["name"],
                                      ("  [" + " | ".join(facts) + "]") if facts else ""))
        if r["impact"]:
            L.append(" " * 10 + r["impact"])
    L.append("  (" + "; ".join(f"{k} = {v}" for k, v in STATUS_MEANING.items()) + ")")
    return L


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def html(rows: list, ev: dict, tz, caps_note: str, td: str, tdl: str) -> str:
    h = ['<div style="font-size:15px;font-weight:600;margin:16px 0 2px">'
         'Risk controls &amp; capacity &mdash; what fired, what binds, what has slack</div>',
         '<div style="color:#888;font-size:12px">{}</div>'.format(_esc(window_note(ev, tz)))]
    tripped = [r for r in rows if r["status"] == "TRIPPED"]
    binding = [r for r in rows if r["status"] == "BINDING"]
    if tripped:
        h.append('<div style="background:#fdecea;border-left:4px solid #c0392b;color:#8e2b21;'
                 'padding:6px 10px;margin:6px 0;font-weight:600">TRIPPED in the last 24h: '
                 + "; ".join(_esc(r["name"]) + " &mdash; " + _esc(r["impact"]) for r in tripped)
                 + '</div>')
    h.append('<div style="margin:4px 0 6px;font-size:13px">{}</div>'.format(
        _esc(headline(rows))))
    h.append('<table style="border-collapse:collapse">')
    h.append('<tr style="background:#f0f0f0;font-weight:600"><td style="{0}">STATUS</td>'
             '<td style="{0}">CONTROL</td><td style="{1}">LIMIT</td>'
             '<td style="{1}">WORST 24H</td><td style="{1}">NOW</td>'
             '<td style="{0}">WHAT IT DID</td></tr>'.format(tdl, td))
    guard_hdr = False
    for i, r in enumerate(rows):
        if r["kind"] == "GUARD" and not guard_hdr:
            h.append('<tr><td colspan="6" style="{};font-weight:600;color:#555;'
                     'padding-top:10px">Per-market guards &mdash; stand one market or event '
                     'down</td></tr>'.format(tdl))
            guard_hdr = True
        col = STATUS_COLOUR[r["status"]]
        bg = "#fafafa" if i % 2 else "#fff"
        strong = r["status"] in ("TRIPPED", "BINDING")
        h.append(
            '<tr style="background:{bg}"><td style="{tdl}"><span style="background:{col};'
            'color:#fff;font-size:11px;font-weight:700;padding:1px 6px;border-radius:3px">'
            '{st}</span></td><td style="{tdl}{fw}">{name}</td><td style="{td}">{lim}</td>'
            '<td style="{td}">{worst}</td><td style="{td}">{now}</td>'
            '<td style="{tdl};font-size:12px;color:{ic}">{imp}</td></tr>'.format(
                bg=bg, tdl=tdl, td=td, col=col, st=r["status"],
                fw=";font-weight:700" if strong else "", name=_esc(r["name"]),
                lim=_esc(r["limit"]) or "&mdash;", worst=_esc(r["worst"]) or "&mdash;",
                now=_esc(r["now"]) or "&mdash;",
                ic="#222" if strong else "#666", imp=_esc(r["impact"])))
    h.append("</table>")
    h.append('<div style="color:#888;font-size:12px;margin-top:4px">{}. {}</div>'.format(
        _esc("; ".join(f"{k} = {v}" for k, v in STATUS_MEANING.items())), _esc(caps_note)))
    return "".join(h)
