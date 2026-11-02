"""Risk controls for the IMM section of the 7:00 portfolio email.

Jack 2026-10-03: "in 'Capacity — how close to each ceiling' in my daily
email, show all risk controls and if any have been breached e.g. the balance
guard, or other. or are close to breaching. make it clear which risk controls
are actually constraining / have real impact vs not".

Jack 2026-11-01, on making it clearer (the same two rows had read BINDING
on 15 emails running, both by design): "Add vs-usual too". The block reads
top-down:

  verdict        tripped or not; the closest call (the worst % any kill
                 switch reached, vs its usual); what held quoting back
  kill switches  each stops ALL quoting until the 6am ET roll: daily loss,
                 account value, fail-safe, the HALT file (and the open-scan
                 tier's budget while the tier runs)
  holding back   only what limited quoting: positions full on one side (one
                 inside Saturday's larger cap only sells down; over any cap
                 is named), an event cap near, the order-size check refusing
                 orders, a capacity cap that refused or deferred
  room to spare  one line: every capacity cap's worst vs its limit
  by design      the per-event strike cap and trade cutoffs: selection, not
                 risk (the strike cap binds at every refresh)
  guards         what stood markets down, in plain names, each against its
                 usual: the median of the 7 days before, kept in
                 risk_controls_history.json by the 7:00 run

Underneath, every control keeps one verdict over the window (24h for the
email):

  TRIPPED    fired: a halt cancelled the book (or the tier's)
  BINDING    limited what the bot quoted (markets refused, placements
             deferred, positions held at a cap, orders refused)
  CLOSE      reached 80% of its limit at the worst point, without binding
  SLACK      never near its limit: not constraining anything
  FIRED      a per-market guard stood something down (working as designed)
  QUIET      a per-market guard that did nothing in the window
  OFF        disabled

The evidence is the bot's own record, never a re-derivation: ALERT lines,
the once-per-cycle summary and "risk:" lines (incentive_mm.risk_line), the
selection gate's "universe:" lines, the placement-cap and sweep-breaker
lines in the bot logs, plus the toxic_halts and guard_skips sinks. Pure
functions over that evidence; the caller (send_imm_digest) supplies the live
limits, positions and balance, so this module never imports incentive_mm.
"""
import ast
import glob
import json
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

WINDOW_SECS = 24 * 3600.0
CLOSE_FRAC = 0.80        # CLOSE from 80% of a limit
# A cap reads BINDING only if it bound in the window's last 2h. Earlier
# binding is reported in its impact text, so a limit raised mid-window (the
# 10/3 budget raise) does not read BINDING until the next day's email.
RECENT_SECS = 2 * 3600.0
AT_CAP_FRAC = 0.95       # a position this close to its cap holds that side
ROLL_HOUR = 6            # the bot's day rolls at 6am ET (5am CT)
LOCK_LOOKBACK_SECS = 14 * 86400.0   # comp_hedge locks read this far back
# vs usual: the median of the USUAL_DAYS days before, once USUAL_MIN_DAYS
# are on file. A count reads unusual at UNUSUAL_X its usual and UNUSUAL_MIN
# more (UNUSUAL_MIN_PCT more for a % of a limit).
HISTORY_FILE = "risk_controls_history.json"
HISTORY_KEEP_DAYS = 60
USUAL_DAYS = 7
USUAL_MIN_DAYS = 3
UNUSUAL_X = 2.0
UNUSUAL_MIN = 5
UNUSUAL_MIN_PCT = 0.10

STATUS_ORDER = ("TRIPPED", "BINDING", "CLOSE", "SLACK", "FIRED", "QUIET", "OFF")

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
# the event sweep breaker's trip line ("exempt" trips pull nothing)
SWEEP_RE = re.compile(r"^sweep breaker \((on|dry)\): (\S+) (?:BID|ASK) @ [\d.]+c filled out "
                      r"\([\d.]+\) -> (?:pulling|would pull) (\d+) order\(s\) in (\S+)")
# the order-size check's alerts: side_cap "<t> <side>: ...", level_cap
# "<t> <side>@<px>c: ..."
BACKSTOP_RE = re.compile(r"^(\S+) (bid|ask)[:@]")
# numbers out of the halt alerts' own wording
DROP_RE = re.compile(r"dropped \$([\d,.]+)")
LIMIT_RE = re.compile(r">= \$([\d,.]+)")
CANCELLED_RE = re.compile(r"cancelled (\d+)")

# guard_skips "guard" names, grouped into the guard rows below. Anything not
# named here lands in "Other stand-downs" under its own name, so nothing the
# bot does goes unreported (and a new guard shows up to be named).
GATE_GUARDS = ("treasury_yield", "rain_monthly", "rain_period", "snow_monthly", "rain_fair",
               "quake", "quake_hold",
               "vercel_fair", "or_fair", "share_fair", "mort_fair", "gb_fair",
               "ca_fair", "dc_count", "poke_fair",
               # the 2026-10-11 fair-file gates (incentive_mm.fair_file_gate)
               "cpi_fair", "rt_fair", "rain_wknd",
               # the 2026-10-22 ski-resort snowfall gate, on the same path
               "resort_snow",
               # the 2026-10-25 Broadway weekly attendance gate, likewise
               "bway_fair",
               # the 2026-10-26 RCP approval, Trump mention and ERCOT peak gates
               "approve_fair", "tmention_fair", "ercot_fair",
               # temperature, YouTube, NFL prop and RT settled-strike gates
               "lowt_fair", "hight_fair", "ytw_fair", "yt1_fair", "nfl_fair",
               "rt_settled_off")
BLACKOUT_GUARDS = ("cpi_blackout", "aaa_blackout", "ust_blackout", "release_pause")
LIVE_GUARDS = ("league_live", "nfl_live", "mlb_mvp", "league_team_news", "nfl_team_news",
               "announce_halt", "announce_hold")
DEPTH_GUARDS = ("event_depth_trip", "event_depth_hold")
CUTOFF_GUARDS = ("closing", "cutoff_passed", "cutoff_extra")
# the mid-move breaker guards rain and temperatures even with IMM_BREAKERS=0
BREAKER_GUARDS = ("move_breaker", "fill_burst", "one_sided_breaker",
                  "breaker_cooldown")
HALT_GUARDS = ("twk_halt", "yt2_halt", "ytw_halt", "astest_fill_pause", "yt2_fill_pause")
MANUAL_GUARDS = ("manual_yield", "manual_grace")
SCAN_TRIP_GUARDS = ("scan_fill_tripwire", "scan_mid_tripwire", "scan_evicted")
QUOTING_RULES = ("band_both_out", "cannot_qualify", "wide_spread",    # not risk
                 "crossed", "blind", "rest_off")
GUARD_LABELS = {
    "treasury_yield": "Treasury yields", "rain_monthly": "monthly rain",
    "rain_period": "period rain", "snow_monthly": "monthly snow", "rain_fair": "daily rain",
    "quake": "quakes", "quake_hold": "quake hold", "vercel_fair": "Vercel",
    "or_fair": "OpenRouter", "share_fair": "OpenRouter share", "mort_fair": "mortgage rates",
    "gb_fair": "AAA gas (GasBuddy)", "ca_fair": "Carbon Arc", "dc_count": "data centers",
    "poke_fair": "Pokemon", "cpi_fair": "CPI", "rt_fair": "Rotten Tomatoes",
    "rain_wknd": "weekend rain", "resort_snow": "resort snow", "bway_fair": "Broadway",
    "approve_fair": "RCP approval", "tmention_fair": "Trump mention rates",
    "ercot_fair": "ERCOT peak", "lowt_fair": "low temps", "hight_fair": "high temps",
    "ytw_fair": "YouTube weekly", "yt1_fair": "YouTube #1", "nfl_fair": "NFL props",
    "rt_settled_off": "RT settled strikes",
    "cpi_blackout": "CPI", "aaa_blackout": "AAA gas", "ust_blackout": "Treasury 6-7am",
    "release_pause": "state stats",
    "league_live": "leagues live", "nfl_live": "NFL live", "mlb_mvp": "MLB MVP games",
    "league_team_news": "team news", "nfl_team_news": "NFL team news",
    "announce_halt": "announcement jump", "announce_hold": "announcement hold",
    "event_depth_trip": "depth trip", "event_depth_hold": "depth hold",
    "move_breaker": "mid-move breaker", "breaker_cooldown": "mid-move breaker",
    "fill_burst": "fill burst", "one_sided_breaker": "one-sided",
    "twk_halt": "Trump weekly halt", "yt2_halt": "YouTube #2 halt",
    "ytw_halt": "YouTube weekly halt", "astest_fill_pause": "AS-test fill pause",
    "yt2_fill_pause": "YouTube #2 fill pause"}


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
            "failsafe": [], "idle": [], "halt_file": [], "sweeps": [], "locks": {},
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
        if msg.startswith("sweep breaker ("):
            s = SWEEP_RE.match(msg)
            if s:
                ev["sweeps"].append((ts, s.group(2), s.group(4), int(s.group(3)), s.group(1)))
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


def scan_locks(records, ev: dict) -> None:
    """Fold comp_hedge sink "open" records: the events where a lead's fill
    opened a complement lock (event -> its latest open: at, lock kind, lead,
    legs). A lock sells (or buys) across the event on purpose, so its
    event net overstates what it can lose."""
    for r in records:
        if r.get("kind") != "open" or not r.get("ticker"):
            continue
        try:
            at = datetime.fromisoformat(str(r.get("ts"))).timestamp()
        except ValueError:
            continue
        e = str(r["ticker"]).rsplit("-", 1)[0]
        if at >= (ev["locks"].get(e) or {}).get("at", 0):
            ev["locks"][e] = {"at": at, "lock": r.get("lock"), "lead": r["ticker"],
                              "legs": list(r.get("legs") or [])}


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
    # locks outlive the window: a position locked days ago still sits there
    for path in _day_files(status_dir, "comp_hedge", now_ts - LOCK_LOOKBACK_SECS, now_ts):
        scan_locks(_iter_jsonl(path), ev)
    return ev


# ---- rows ----------------------------------------------------------------------

def _money(v: float) -> str:
    return "${:,.0f}".format(v)


def _signed_money(v: float) -> str:
    return ("-" if v < 0 else "+") + _money(abs(v))


def _kmoney(v: float) -> str:
    return "${:,.0f}k".format(v / 1000.0) if v >= 10000 else _money(v)


def _int(v: float) -> str:
    return "{:,.0f}".format(v)


def _s(n) -> str:
    return "" if n == 1 else "s"


def _times(n) -> str:
    return "once" if n == 1 else f"{n:,} times"


def _pct_status(worst, limit):
    if worst is None or not limit:
        return None
    return "CLOSE" if worst >= CLOSE_FRAC * limit else "SLACK"


def _row(kind, name, status, limit="", worst="", now="", impact="", pct=None, **extra):
    """One control. Beside the verdict columns a row may carry: title (its
    name in the email), short (in a one-line list), brief (its numbers on
    the room-to-spare line), tag (in the verdict's holding-back list),
    earlier (bound in the window, not its last 2h), design (selection, not
    risk), show (a guard line worth a line of its own), line (that line,
    "{usual}" where the usual goes), usual_key and metric (what the history
    keeps)."""
    r = {"kind": kind, "name": name, "status": status, "limit": limit,
         "worst": worst, "now": now, "impact": impact, "pct": pct}
    r.update(extra)
    return r


def _when(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).strftime("%a %H:%M")


def _span(secs: float) -> str:
    return f"{secs / 3600:.1f}h" if secs >= 3600 else f"{secs / 60:.0f} min"


def _trading_day(ts: float, tz):
    """The bot's trading day of `ts`: its day rolls at ROLL_HOUR (6am ET),
    so wall-clock 04:34 Sunday is still Saturday's day."""
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).replace(tzinfo=None)
    return (local - timedelta(hours=ROLL_HOUR)).date()


def _day_note(ts: float, until: float, tz) -> str:
    """", Sat's day" when `ts` fell in the trading day before `until`'s: the
    worst and the now of a daily limit can straddle the roll."""
    d = _trading_day(ts, tz)
    return "" if d == _trading_day(until, tz) else f", {d.strftime('%a')}'s day"


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
        rows.append(_row("HALT", name, "OFF", "off (IMM_ACCOUNT_DROP_HALT=0)",
                         short="the account-value guard (IMM_ACCOUNT_DROP_HALT=0)"))
    else:
        trips = ev["alerts"].get("balance_floor", [])
        worst = ev["acct_worst"]
        value, anchor = now.get("acct_value"), now.get("acct_anchor")
        now_drop = (anchor - value) if (value is not None and anchor) else None
        pct = ((max(worst[0], 0) / lim) if worst else
               ((max(now_drop, 0) / lim) if now_drop is not None else None))
        if not worst:
            w_val, w_txt = None, "not logged yet"
        elif worst[0] <= 0:
            w_val, w_txt = None, "never below the anchor"
        else:
            w_val = f"down {_money(worst[0])}"
            w_txt = (f"{w_val} ({worst[0] / lim:.0%}) {_when(worst[1], tz)}"
                     + _day_note(worst[1], ev["until"], tz))
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
            impact = ("cash + positions vs the 6am anchor"
                      + (f" (now {_money(value)} vs {_money(anchor)})"
                         if now_drop is not None else ""))
        rows.append(_row("HALT", name, status, f"{_money(lim)} drop/day", w_txt, n_txt,
                         impact, pct, title="Account value", short="account value",
                         limit_phrase=f"{_money(lim)}/day account-value halt",
                         worst_val=w_val, worst_at=worst[1] if worst and w_val else None,
                         metric={"acct_pct": pct}))

    # -- daily loss ----------------------------------------------------------
    lim = caps.get("DAILY_LOSS_LIMIT") or 0.0
    name = "Daily loss halt"
    if lim <= 0:
        rows.append(_row("HALT", name, "OFF", "off", short="the daily loss halt"))
    else:
        trips = ev["alerts"].get("loss_halt", [])
        pmin = ev["pnl_min"]
        worst = max(-pmin[0], 0.0) if pmin else None
        # The halt's own P&L (the cycle line's, carry included) beats the
        # status file's pnl_today, which omits the restart carry and reads
        # ~0 after every restart (-$14 vs the halt's -$820 on 10/3).
        pnl_now = ev["pnl_last"][0] if ev["pnl_last"] else now.get("pnl_today")
        if not pmin:
            w_val, w_txt = None, "n/a"
        elif pmin[0] >= 0:
            w_val, w_txt = None, "never below $0"
        else:
            w_val = _signed_money(pmin[0])
            w_txt = (f"{w_val} ({worst / lim:.0%}) {_when(pmin[1], tz)}"
                     + _day_note(pmin[1], ev["until"], tz))
        n_txt = _signed_money(pnl_now) if pnl_now is not None else "n/a"
        if trips:
            status = "TRIPPED"
            impact = "; ".join(_trip_text(ev, ts, msg, tz, "TRIPPED") for ts, msg in trips)
        else:
            status = _pct_status(worst, lim) or "SLACK"
            impact = "IMM trading P&L today (realized + marked) since the 6am ET roll"
        pct = (worst / lim) if worst is not None else None
        rows.append(_row("HALT", name, status, f"-{_money(lim)}/day", w_txt, n_txt,
                         impact, pct, title="Daily loss", short="daily loss",
                         limit_phrase=f"-{_money(lim)}/day halt",
                         worst_val=w_val, worst_at=pmin[1] if w_val else None,
                         metric={"loss_pct": pct}))

    # -- the open-scan tier's own budget ---------------------------------------
    lim = caps.get("SCAN_DAILY_LOSS_LIMIT") or 0.0
    name = "Open-scan tier loss budget"
    if lim <= 0 or (caps.get("SCAN_TOP_N") or 0) <= 0:
        tier_off = (caps.get("SCAN_TOP_N") or 0) <= 0
        rows.append(_row("HALT", name, "OFF", "off" + (" (tier off)" if tier_off else ""),
                         short=("the open-scan tier and its loss budget" if tier_off
                                else "the open-scan loss budget")))
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
        w_val = _signed_money(smin[0]) if smin and smin[0] < 0 else None
        rows.append(_row("HALT", name, status, f"-{_money(lim)}/day",
                         (f"{w_val} ({worst / lim:.0%}) {_when(smin[1], tz)}" if w_val else
                          "never below $0" if smin else "not logged yet"),
                         _signed_money(s_now) if s_now is not None else "n/a", impact,
                         (worst / lim) if worst is not None else None,
                         title="Open-scan budget", short="open-scan budget",
                         limit_phrase=f"-{_money(lim)}/day open-scan budget",
                         worst_val=w_val, worst_at=smin[1] if w_val else None))

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
                     f"{lim} failed cycles in a row", f"{worst} in a row",
                     f"{now.get('errors_today', 0)} errors today",
                     impact, (worst / lim) if lim else None, title="Fail-safe",
                     short="fail-safe", limit_phrase=f"{lim}-in-a-row limit",
                     worst_val=f"{worst} failed cycles in a row", worst_at=None,
                     cell=f"{worst} in a row"))

    # -- manual kill switch ----------------------------------------------------
    if now.get("halt_file") or ev["halt_file"]:
        first = ev["halt_file"][0] if ev["halt_file"] else None
        rows.append(_row("HALT", "Manual HALT file", "TRIPPED", "kill switch",
                         "", "present" if now.get("halt_file") else "removed",
                         ("since " + _when(first, tz) + " ET" if first else "present now")
                         + ": orders cancelled, bot idle", title="HALT file"))
    else:
        rows.append(_row("HALT", "Manual HALT file", "SLACK", "kill switch",
                         "", "absent", "not engaged (a HALT file in run-logs idles the bot)",
                         title="HALT file"))
    return rows


def _earlier(last_ts, tz) -> str:
    return f"bound earlier (last {_when(last_ts, tz)}), not in the last {RECENT_SECS / 3600:.0f}h"


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
        impact = (f"new events refused at the {_int(lim)}-event cap; open events keep quoting"
                  if status == "BINDING" else
                  _earlier(at_cap[-1], tz) if at_cap else
                  f"reached {_int(worst)} of {_int(lim)} events")
        rows.append(_row("CAPACITY", "Events quoted", status, _int(lim) + " events",
                         _int(worst), _int(last["events"]), impact, worst / lim,
                         title="Event count", short="events",
                         brief=f"{_int(worst)}/{_int(lim)}", tag="the event-count cap",
                         earlier=bool(at_cap) and status != "BINDING"))

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
            impact = (_earlier(hit_ts[-1], tz) + f": refused new markets in {hit} of "
                      f"{n_ref} refreshes (up to {max(refused):,}). Modelled reservation, "
                      f"not cash")
        else:
            status = _pct_status(worst, lim)
            impact = (f"reserved up to {_kmoney(worst)} of {_kmoney(lim)} (modelled ladder + "
                      f"inventory reserve, not cash)")
        rows.append(_row("CAPACITY", "Collateral budget (modelled)", status, _money(lim),
                         _money(worst), _money(last["reserved"]), impact, worst / lim,
                         title="Collateral budget", short="budget",
                         brief=f"{_kmoney(worst)}/{_kmoney(lim)}",
                         tag=(f"the collateral budget ({refused[-1]:,} refused)"
                              if status == "BINDING" else "the collateral budget"),
                         earlier=bool(hit) and status != "BINDING"))

    # -- candidate books ------------------------------------------------------------
    lim = caps.get("MAX_CANDIDATE_BOOKS") or 0
    if uni and lim:
        worst = max(u["candidates"] for u in uni)
        at_cap = [u["ts"] for u in uni if u["candidates"] >= lim]
        status = "BINDING" if any(t >= recent for t in at_cap) else _pct_status(worst, lim)
        impact = ("the lowest-$ candidates are dropped unread at the cap"
                  if status == "BINDING" else _earlier(at_cap[-1], tz) if at_cap else
                  f"read up to {_int(worst)} of {_int(lim)} candidates a refresh")
        rows.append(_row("CAPACITY", "Candidate books read", status, _int(lim),
                         _int(worst), _int(last["candidates"]), impact, worst / lim,
                         title="Candidate books", short="candidate books",
                         brief=f"{_int(worst)}/{_int(lim)}", tag="the candidate-book cap",
                         earlier=bool(at_cap) and status != "BINDING"))
    # -- slot caps: strikes per event, and the two opportunistic tiers -----------
    # Selection, not risk: the per-event strike cap leaves strikes unquoted at
    # every refresh by design, so it reads on the "by design" line.
    if uni:
        # scan_top_n also counts candidates under the tier's ROI floor
        # (SCAN_MIN_ROI): the group walk returns both as one cut
        slots = (("event_top_n", "the per-event strike cap", "strikes"),
                 ("scan_top_n", "open-scan slots or ROI floor", "markets"),
                 ("finecon_top_n", "finecon slots", "markets"))
        last_n = [(lbl, unit, last["skips"].get(k, 0) or 0) for k, lbl, unit in slots]
        hit_ts = [u["ts"] for u in uni if any(u["skips"].get(k, 0) for k, _l, _u in slots)]
        hit = len(hit_ts)
        parts = [f"{lbl} left {n:,} {unit} unquoted" for lbl, unit, n in last_n if n]
        if parts:
            status = "BINDING"
            impact = ("; ".join(parts) + " at the last refresh"
                      + (" (every refresh)" if hit == n_ref else
                         f" ({hit} of {n_ref} refreshes)"))
        elif hit and hit_ts[-1] >= recent:
            status = "BINDING"
            impact = f"slot caps left markets unquoted in {hit} of {n_ref} refreshes, none at the last"
        elif hit:
            status = "SLACK"
            impact = (_earlier(hit_ts[-1], tz) + f": slot caps left markets unquoted in "
                      f"{hit} of {n_ref} refreshes")
        else:
            status = "SLACK"
            impact = "the slot caps never left a market unquoted"
        lim_txt = " / ".join(f"{lbl} {caps[k]}" for k, lbl in (("SCAN_TOP_N", "scan"),
                                                                ("FINECON_TOP_N", "finecon"))
                             if caps.get(k) is not None)
        rows.append(_row("CAPACITY", "Slot caps (strikes per event, scan, finecon)", status,
                         lim_txt, "", "", impact, design=True,
                         metric={"strike_cap": last_n[0][2]},
                         brief="; ".join(f"{lbl.replace('the per-event strike cap', 'strike cap per event')}"
                                         f" left {n:,} {unit} unquoted"
                                         for lbl, unit, n in last_n if n) or impact))
    if not uni:
        rows.append(_row("CAPACITY", "Selection gate (events / budget / candidates)",
                         "SLACK", "", "n/a", "n/a",
                         "no universe line in the window's logs; nothing to judge",
                         short="selection gate", brief="no selection line in the logs"))

    # -- resting orders ----------------------------------------------------------------
    lim = caps.get("MAX_TOTAL_RESTING_ORDERS") or 0
    if lim:
        hits = ev["alerts"].get("order_cap", [])
        rmax = ev["resting_max"]
        r_now = now.get("resting_orders")
        worst = max(rmax[0] if rmax else 0, r_now or 0) if (rmax or r_now is not None) else None
        if hits and hits[-1][0] >= recent:
            status = "BINDING"
            impact = (f"cap hit {len(hits)}x (first {_when(hits[0][0], tz)}): new "
                      f"placements blocked until orders fill or cancel")
        else:
            status = _pct_status(worst, lim) or "SLACK"
            impact = (_earlier(hits[-1][0], tz) + f": cap hit {len(hits)}x" if hits
                      else f"up to {_int(worst or 0)} of {_int(lim)} orders resting")
        note = now.get("resting_notional")
        if note:
            impact += f"; resting notional {_money(note)} if every order filled"
        rows.append(_row("CAPACITY", "Resting orders", status, _int(lim),
                         _int(worst) if worst is not None else "n/a",
                         _int(r_now) if r_now is not None else "n/a", impact,
                         (worst / lim) if worst is not None else None,
                         title="Resting orders", short="resting orders",
                         brief=(f"{_int(worst)}/{_int(lim)}" if worst is not None else "n/a"),
                         tag="the resting-order cap", earlier=bool(hits) and status != "BINDING"))

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
                impact = _earlier(hits[-1][0], tz) + ": " + impact
            brief = f"capped in {len(hits)} cycle{_s(len(hits))}"
        else:
            status = "SLACK"
            impact = "never hit: every wanted order went out the same cycle"
            brief = "never capped"
        if ev["amend_cap"]:
            impact += f"; amend cap hit {len(ev['amend_cap'])}x"
            brief += f" (amend cap {len(ev['amend_cap'])}x)"
        rows.append(_row("CAPACITY", "Placements per cycle", status, lim_txt,
                         f"{len(hits)} cycles at cap", "", impact,
                         1.0 if hits else None, title="Placements per cycle",
                         short="placements", brief=brief,
                         tag=f"the placement cap ({len(hits)} cycle{_s(len(hits))})",
                         earlier=bool(hits) and status != "BINDING"))
    return rows


def backstop_row(ev: dict, tz) -> dict:
    """The order-size check (ALERT side_cap / level_cap), the last gate
    before an order goes out: it refuses an order bigger than its market's
    own ladder bracket. That is a sizing mismatch somewhere upstream, never
    routine (10/30: a rounding gap refused one bid 679 times), and it is not
    a position cap. Counted by market side, not by alert line: one stuck
    side alerts every cycle. Each side's count reads "refused N times": the
    bare "PNS ask · 2,947" of 11/02 looked like a 2,947-lot order."""
    sides = defaultdict(lambda: [0, 0.0])          # (ticker, side) -> [alerts, last ts]
    for cat in ("side_cap", "level_cap"):
        for ts, msg in ev["alerts"].get(cat, []):
            m = BACKSTOP_RE.match(msg)
            if m:
                s = sides[(m.group(1), m.group(2))]
                s[0] += 1
                s[1] = max(s[1], ts)
    name = "Order-size check (side / level backstop)"
    if not sides:
        return _row("CAPACITY", name, "SLACK", "", "", "", "never refused an order",
                    title="Order-size check", short="order-size check",
                    brief="never refused an order", metric={"backstop_sides": 0})
    last = max(s[1] for s in sides.values())
    top = sorted(sides.items(), key=lambda kv: (-kv[1][0], kv[0]))
    status = "BINDING" if last >= ev["until"] - RECENT_SECS else "SLACK"
    n = len(sides)
    line = (f"refused orders on {n} market side{_s(n)}{{usual}}, each re-tried every "
            f"cycle: "
            + ", ".join(f"{t} {side} {_times(a)}" for (t, side), (a, _l) in top[:3])
            + (f" +{n - 3} more" if n > 3 else "")
            + f"; last {_when(last, tz)}. Each would have put more on its side than "
              f"its ladder allows: a sizing mismatch to look at")
    return _row("CAPACITY", name, status, "", "", "", line.replace("{usual}", ""),
                title="Order-size check", short="order-size check",
                tag=f"the order-size check ({n} market side{_s(n)})", line=line,
                usual_key="backstop_sides", earlier=status != "BINDING",
                metric={"backstop_sides": n}, count=n,
                what=f"refused orders on {n} market side{_s(n)}, last {_when(last, tz)}",
                where=[f"{t} {side} · refused {_times(a)}" for (t, side), (a, _l) in top[:3]]
                + ([f"+{n - 3} more"] if n > 3 else []),
                note="the bot re-tries a refused order every cycle. Each would have put more "
                     "on its side than its ladder allows: a sizing mismatch to look at")


def position_rows(ev: dict, ctx: dict, tz) -> list:
    """Per-market and per-event caps, each judged against ITS OWN cap (the
    bot's family-aware series_max_position / event_cap_contracts, resolved by
    the caller) — a sports ladder's cap is 5x the global 150.

    A market may carry a fourth number, its Saturday cap
    (series_max_position(peak=True): 200 a market on Saturdays since
    ac8ac88). A position above today's cap but inside that one only sells
    down; on 11/01 such Saturday positions read "18 OVER" the cap. Over
    any cap is a real breach and is named, "(lock)" when it is a leg of a
    complement lock (ev["locks"]): the 11/01 KXTRUTHSOCIAL-26NOV07 lock sold
    115 on each of its brackets, -871 net against a 100 event cap, while only
    one bracket can pay."""
    now = ctx["now"]
    locks = ev.get("locks") or {}
    rows = []

    def lock_tag(t):
        return " (lock)" if t.rsplit("-", 1)[0] in locks else ""

    def lock_note(e, pos_by_t):
        lk = locks.get(e)
        if not lk:
            return ""
        legs = lk.get("legs") or []
        held = sum(1 for t in legs if abs(pos_by_t.get(t, 0.0)) >= 1)
        why = ("only one bracket can pay" if lk.get("lock") in ("me_ask", "pair_bid")
               else "its legs hedge each other")
        return (f": a complement lock since {_when(lk['at'], tz)} ({held} of {len(legs)} "
                f"legs held), so the net overstates it ({why})")

    items = []
    for it in now.get("positions") or []:
        k, v, c = it[0], it[1], it[2]
        cp = it[3] if len(it) > 3 and it[3] else c
        if c and c > 0:
            items.append((k, v, c, max(cp, c)))
    name = "Per-market position cap"
    if not items:
        rows.append(_row("POSITION", name, "SLACK", "", "", "flat", "no open positions",
                         title="Position caps", short="positions", brief="flat",
                         metric={"pos_full": 0, "pos_sat": 0, "pos_over": 0}))
    else:
        full = [x for x in items if abs(x[1]) >= AT_CAP_FRAC * x[2]]
        sat = [x for x in full if x[2] + 0.5 < abs(x[1]) <= x[3] + 0.5]
        over = sorted((x for x in items if abs(x[1]) > x[3] + 0.5),
                      key=lambda x: -abs(x[1]) / x[3])
        k, v, c, _cp = max(items, key=lambda x: abs(x[1]) / x[2])
        pct = abs(v) / c
        real_over = [x for x in over if not lock_tag(x[0])]
        if full:
            what = (f"{len(full):,} full on one side"
                    + (f" · {len(sat):,} inside Saturday's cap, sell only" if sat else "")
                    + (f" · {len(over):,} over any cap" if over else ""))
            where = ([f"{t} {p:+,.0f}/{cap:,.0f}{lock_tag(t)}" for t, p, _c, cap in over[:3]]
                     + ([f"+{len(over) - 3} more over" ] if len(over) > 3 else [])
                     if over else [f"fullest: {k} {v:+,.0f}/{c:,.0f}"])
        else:
            near_n = sum(1 for x in items if abs(x[1]) >= CLOSE_FRAC * x[2])
            what = (f"{near_n} above {CLOSE_FRAC:.0%} of their cap, none full" if near_n
                    else f"fullest at {pct:.0%} of its cap")
            where = [f"fullest: {k} {v:+,.0f}/{c:,.0f}"]
        if full:
            status = "BINDING"
            impact = (f"{len(full):,} market{_s(len(full))} full on one side{{usual}}: the "
                      f"side that adds isn't quoted")
            if sat:
                impact += (f"; {len(sat):,} of them above today's cap but inside "
                           f"Saturday's, so they only sell down")
            if over:
                impact += (f"; {len(over):,} over any cap: "
                           + ", ".join(f"{t} {p:+,.0f}/{cap:,.0f}{lock_tag(t)}"
                                       for t, p, _c, cap in over[:3])
                           + (f" +{len(over) - 3} more" if len(over) > 3 else ""))
            elif not sat:
                impact += f"; fullest: {k} {v:+,.0f}/{c:,.0f}"
            tag = f"position caps ({len(full):,} full)"
        else:
            status = _pct_status(abs(v), c)
            near = sum(1 for x in items if abs(x[1]) >= CLOSE_FRAC * x[2])
            impact = ((f"{near} market{_s(near)} above {CLOSE_FRAC:.0%} of the cap, none "
                       f"full; " if near else "") + f"fullest: {k} {v:+,.0f}/{c:,.0f}")
            tag = f"position caps (fullest at {pct:.0%})"
        # positions are read NOW (the state file); there is no 24h history
        impact += f" ({len(items):,} open)"
        rows.append(_row("POSITION", name, status, "per-series cap", "",
                         f"{abs(v):,.0f} / {c:,.0f} ({pct:.0%})",
                         impact.replace("{usual}", ""), pct, title="Position caps",
                         short="positions", brief=f"fullest at {pct:.0%} of its cap",
                         tag=tag, line=impact, usual_key="pos_full",
                         metric={"pos_full": len(full), "pos_sat": len(sat),
                                 "pos_over": len(over)},
                         count=len(full), what=what, where=where, alarm=bool(real_over)))

    items = [(k, v, c) for k, v, c in (now.get("events") or []) if c and c > 0]
    name = "Per-event net cap"
    if not items:
        rows.append(_row("POSITION", name, "SLACK", "", "", "flat", "no open positions",
                         title="Event caps", short="event nets", brief="flat"))
    else:
        pos_by_t = {it[0]: it[1] for it in (now.get("positions") or [])}
        k, v, c = max(items, key=lambda x: abs(x[1]) / x[2])
        pct = abs(v) / c
        at = [x for x in items if abs(x[1]) >= AT_CAP_FRAC * x[2]]
        lk = locks.get(k)
        where = [f"{k} {v:+,.0f} of {c:,.0f}"
                 + (f" · complement lock, {sum(1 for t in lk.get('legs') or [] if abs(pos_by_t.get(t, 0.0)) >= 1)}"
                    f" of {len(lk.get('legs') or [])} legs held: only one bracket can pay"
                    if lk and lk.get("lock") in ("me_ask", "pair_bid") else
                    " · complement lock: its legs hedge each other" if lk else "")]
        if at:
            status = "BINDING"
            over_ev = any(abs(x[1]) > x[2] + 0.5 for x in at)
            impact = (f"{len(at)} event{_s(len(at))} at {'or over ' if over_ev else ''}the "
                      f"cap: the side that adds isn't quoted; fullest: {k} {v:+,.0f} of "
                      f"{c:,.0f}{lock_note(k, pos_by_t)}")
            tag = (f"event caps ({len(at)} full"
                   + (", all locks" if all(x[0] in locks for x in at) else "") + ")")
            what = f"{len(at)} event{_s(len(at))} at {'or over ' if over_ev else ''}the cap"
            alarm = any(abs(x[1]) > x[2] + 0.5 and x[0] not in locks for x in at)
        else:
            status = _pct_status(abs(v), c)
            impact = f"{k} {v:+,.0f} of {c:,.0f} ({pct:.0%}){lock_note(k, pos_by_t)}"
            tag = f"the event cap ({k} at {pct:.0%})"
            what, alarm = f"fullest at {pct:.0%} of its cap", False
        rows.append(_row("POSITION", name, status, "per-series cap", "",
                         f"{abs(v):,.0f} / {c:,.0f} ({pct:.0%})",
                         impact + f" ({len(items):,} events open)", pct, title="Event caps",
                         short="event nets", brief=f"fullest at {pct:.0%} of its cap",
                         tag=tag, metric={"event_pct": pct}, what=what, where=where,
                         alarm=alarm))
    return rows


def guard_rows(ev: dict, ctx: dict, tz) -> list:
    caps, now = ctx["caps"], ctx["now"]
    g = ev["guards"]
    uni = ev["universe"]
    rows = []

    def markets(names):
        s = set()
        for n in names:
            s |= g.get(n, set())
        return s

    def top(names, k=4):
        """The biggest guards by markets stood down, guards that share a
        label (the mid-move breaker and its cool-down) as one."""
        by_label = defaultdict(set)
        for n in names:
            if g.get(n):
                by_label[GUARD_LABELS.get(n, n)] |= g[n]
        lst = sorted(((len(s), lbl) for lbl, s in by_label.items()),
                     key=lambda x: (-x[0], x[1].lower()))
        return (" · ".join(f"{lbl} {c:,}" for c, lbl in lst[:k])
                + (f" +{len(lst) - k} more" if len(lst) > k else ""))

    def counted(names, usual_key, title, short, name, extra=""):
        """A guard row counting the distinct markets its guards stood down."""
        mk = markets(names)
        line = (f"{len(mk):,} market{_s(len(mk))}{{usual}}: " + top(names)) if mk else ""
        line = "; ".join(x for x in (line, extra) if x)
        rows.append(_row("GUARD", name, "FIRED" if line else "QUIET", "", "", "",
                         line.replace("{usual}", "") or "nothing stood down",
                         title=title, short=short, show=bool(line), line=line,
                         usual_key=usual_key, metric={usual_key: len(mk)},
                         count=len(mk), unit="market",
                         detail="; ".join(x for x in (top(names) if mk else "", extra) if x)))

    # toxic flow: pick-offs and the halts they bring
    t = ev["toxic"]
    if not caps.get("TOXIC_HALT", True):
        rows.append(_row("GUARD", "Toxic-flow halts", "OFF", "off (IMM_TOXIC_HALT=0)",
                         short="toxic-flow halts (IMM_TOXIC_HALT=0)"))
    else:
        n_side, n_ev = len(t["side_halts"]), len(t["event_halts"])
        pk, pm = t["pickoffs"], len(t["pick_markets"])
        line = f"{pk:,}{{usual}} on {pm:,} market{_s(pm)}"
        if n_side or n_ev:
            line += (f" -> {n_side} side + {n_ev} event halts; "
                     f"~{_money(t['idled_reward'])} of modelled reward idled")
        elif pk:
            line += ", no halt"
        if not t["read"]:
            line = "no toxic_halts sink in the window"
        rows.append(_row("GUARD", "Toxic-flow halts", "FIRED" if (n_side or n_ev) else "QUIET",
                         f"{caps.get('TOXIC_PICKOFFS', 2)} pick-offs/24h", "", "",
                         line.replace("{usual}", ""), title="Pick-offs",
                         short="pick-offs and toxic-flow halts", show=bool(pk or n_side or n_ev),
                         line=line, usual_key="pickoffs",
                         metric={"pickoffs": pk, "toxic_halts": n_side + n_ev},
                         count=pk, unit="",
                         detail=(f"on {pm:,} market{_s(pm)}"
                                 + (f" -> {n_side} side + {n_ev} event halt{_s(n_side + n_ev)}"
                                    if (n_side or n_ev) else ", no halt"))))

    # the event sweep breaker: a fill that clears our order pulls the event
    sw = [s for s in ev["sweeps"] if s[4] == "on"]
    dry = [s for s in ev["sweeps"] if s[4] != "on"]
    evs = {s[2] for s in sw}
    line = (f"{len(sw):,} trip{_s(len(sw))}{{usual}} on {len(evs):,} event{_s(len(evs))}, "
            f"{sum(s[3] for s in sw):,} orders pulled" if sw else "")
    if dry:
        line = "; ".join(x for x in (line, f"{len(dry)} dry-run trip{_s(len(dry))}") if x)
    rows.append(_row("GUARD", "Sweep breaker", "FIRED" if line else "QUIET", "", "", "",
                     line.replace("{usual}", "") or "nothing tripped", title="Sweep breaker",
                     short="sweep breaker", show=bool(line), line=line, usual_key="sweeps",
                     metric={"sweeps": len(sw)}, count=len(sw), unit="trip",
                     detail="; ".join(x for x in (
                         f"{len(evs):,} event{_s(len(evs))} · {sum(s[3] for s in sw):,} orders "
                         f"pulled" if sw else "",
                         f"{len(dry)} dry-run trip{_s(len(dry))}" if dry else "") if x)))

    # fair-value and data gates
    counted(GATE_GUARDS, "gate", "Fair & data gates", "fair & data gates",
            "Fair-value & data gates")

    # live games, team news and announcements, the depth gate
    live = ev["alerts"].get("event_live", []) + ev["alerts"].get("event_depth", [])
    perm = {m.split(":")[0] for _t, m in live}
    counted(LIVE_GUARDS + DEPTH_GUARDS, "live", "Live games & news", "live games & news",
            "Live games & news holds",
            f"{len(perm)} event{_s(len(perm))} stood down for good" if perm else "")

    # breakers and the per-series loss halts
    trips = sum(len(ev["alerts"].get(c, [])) for c in ("move_breaker", "fill_burst",
                                                       "one_sided"))
    counted(BREAKER_GUARDS + HALT_GUARDS, "breakers", "Breakers & series halts",
            "breakers & series halts", "Breakers & series halts",
            f"{trips:,} breaker trip{_s(trips)}" if trips else "")
    if not caps.get("BREAKERS_ENABLED"):
        rows.append(_row("GUARD", "Global breakers (IMM_BREAKERS)", "OFF", "", "", "",
                         "off: IMM_BREAKERS=0; the mid-move breaker still guards rain and "
                         "temperatures",
                         short="global breakers (IMM_BREAKERS=0; the mid-move breaker still "
                               "guards rain and temperatures)"))

    # release pauses
    counted(BLACKOUT_GUARDS, "pauses", "Release pauses", "release pauses",
            "Release pauses (CPI, AAA, Treasury, state stats)")

    # event fill tripwire
    fills = ev["alerts"].get("event_fill", [])
    perm_f = sum(1 for _t, m in fills if "PERMANENTLY" in m)
    lim = caps.get("EVENT_FILL_HALT_CONTRACTS") or 0
    if lim <= 0:
        rows.append(_row("GUARD", "Event fill tripwire", "OFF", "off",
                         short="the event fill tripwire"))
    else:
        fe = {m.split(":")[0] for _t, m in fills}
        line = ((f"{len(fe)} event{_s(len(fe))} stood down"
                 + (f", {perm_f} for good" if perm_f else "")) if fills else "")
        rows.append(_row("GUARD", "Event fill tripwire", "FIRED" if fills else "QUIET",
                         f"{lim:g} cts/cycle", "", "", line or "no gated event was swept",
                         title="Event fill tripwire", short="event fill tripwire",
                         show=bool(fills), line=line, metric={"event_fill": len(fe)},
                         count=len(fe), unit="event",
                         detail=f"{perm_f} for good" if perm_f else "stood down"))

    # trade cutoffs and closing: selection, by design
    cm = markets(CUTOFF_GUARDS)
    past = (uni[-1]["skips"].get("cutoff", 0) or 0) if uni else 0
    rows.append(_row("GUARD", "Trade cutoffs & closing", "FIRED" if (cm or past) else "QUIET",
                     "", "", "",
                     (f"{past:,} markets past their cutoff" if past else "trade cutoffs")
                     + (f" ({len(cm):,} stood down mid-quote)" if cm else ""),
                     design=True, metric={"cutoff_past": past, "cutoff_mid": len(cm)},
                     brief=(f"{past:,} markets past their cutoff" if past else "cutoffs")
                     + (f" ({len(cm):,} mid-quote)" if cm else "")))

    # your own orders: the bot stands aside where they rest
    man = now.get("manual_standoff") or []
    man_skip = (uni[-1]["skips"].get("manual", 0) or 0) if uni else 0
    n_now, n_win = max(len(man), man_skip), len(markets(MANUAL_GUARDS))
    if n_now:
        line = (f"the bot stands aside in {n_now:,} market{_s(n_now)} where they rest"
                + (f" ({n_win:,} over the window)" if n_win > n_now else ""))
    else:
        line = (f"none rest now; the bot stood aside in {n_win:,} market{_s(n_win)} over "
                f"the window") if n_win else ""
    rows.append(_row("GUARD", "Manual stand-aside", "FIRED" if line else "QUIET", "", "", "",
                     line or "no manual orders in the bot's markets", title="Your own orders",
                     short="your own orders", show=bool(line), line=line,
                     metric={"manual": n_now}, count=n_now or n_win, unit="market",
                     detail=(f"where they rest ({n_win:,} over the window)" if n_now and n_win > n_now
                             else "where they rest" if n_now else "over the window; none rest now")))

    # watchdog / coverage (page only)
    wd = ev["alerts"].get("watchdog", []) + ev["alerts"].get("coverage", [])
    rows.append(_row("GUARD", "Watchdog & coverage alerts", "FIRED" if wd else "QUIET", "",
                     "", "", f"{len(wd)} page{_s(len(wd))} (an empty or one-sided book)"
                     if wd else "no empty-book or one-sided page", title="Watchdog",
                     short="watchdog", show=bool(wd),
                     line=f"{len(wd)} page{_s(len(wd))} (an empty or one-sided book)",
                     metric={"watchdog": len(wd)}, count=len(wd), unit="page",
                     detail="an empty or one-sided book"))

    # open-scan tripwires
    if any((caps.get(k) or 0) > 0 for k in ("SCAN_FILL_HALT_CONTRACTS", "SCAN_MID_JUMP_CENTS",
                                              "SCAN_DRIFT_CENTS")):
        n = len(ev["alerts"].get("scan_evict", []))
        rows.append(_row("GUARD", "Open-scan tripwires", "FIRED" if n else "QUIET", "", "", "",
                         f"{n} event{_s(n)} evicted" if n else "nothing evicted",
                         title="Open-scan tripwires", short="open-scan tripwires",
                         show=bool(n), line=f"{n} event{_s(n)} evicted",
                         count=n, unit="event", detail="evicted"))
    else:
        rows.append(_row("GUARD", "Open-scan tripwires (fill / mid jump / drift)", "OFF",
                         "", "", "", "off: IMM_SCAN_FILL_HALT / MID_JUMP / DRIFT = 0",
                         short="open-scan tripwires"))

    # everything else the bot stood down, under its own name, so nothing is hidden
    named = set(GATE_GUARDS + BLACKOUT_GUARDS + LIVE_GUARDS + DEPTH_GUARDS + CUTOFF_GUARDS
                + BREAKER_GUARDS + HALT_GUARDS + MANUAL_GUARDS + SCAN_TRIP_GUARDS
                + QUOTING_RULES + ("event_fill_tripwire",))
    other = sorted(n for n in g if n not in named)
    if other:
        n = len(markets(other))
        line = f"{n:,} market{_s(n)}{{usual}}: " + top(other, 6)
        rows.append(_row("GUARD", "Other stand-downs", "FIRED", "", "", "",
                         line.replace("{usual}", ""), title="Other stand-downs",
                         short="other stand-downs", show=True, line=line,
                         usual_key="other", metric={"other": n}, count=n, unit="market",
                         detail=top(other, 6)))
    return rows


def build_rows(ev: dict, ctx: dict, tz=timezone.utc) -> list:
    """All rows, account-wide controls first, worst verdict first."""
    main = (halt_rows(ev, ctx, tz) + capacity_rows(ev, ctx, tz) + [backstop_row(ev, tz)]
            + position_rows(ev, ctx, tz))
    main.sort(key=lambda r: STATUS_ORDER.index(r["status"]))
    guards = guard_rows(ev, ctx, tz)
    guards.sort(key=lambda r: STATUS_ORDER.index(r["status"]))
    return main + guards


# ---- the block's parts -----------------------------------------------------------

def block_of(r: dict) -> str:
    """Which part of the email a row reads in: kill / held / room / design /
    guard / quiet / off."""
    if r["status"] == "OFF":
        return "off"
    if r["kind"] == "HALT":
        return "kill"
    if r.get("design"):
        return "design"
    if r["kind"] == "GUARD":
        return "guard" if r.get("show", r["status"] == "FIRED") else "quiet"
    if r["status"] in ("TRIPPED", "BINDING", "CLOSE") or r.get("earlier"):
        return "held"
    return "room"


def _blocks(rows: list) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[block_of(r)].append(r)
    # the kill switches in a fixed order, the HALT file last
    order = ("Daily loss halt", "Account-value guard (balance guard)",
             "Open-scan tier loss budget", "Fail-safe (consecutive cycle errors)",
             "Manual HALT file")
    by["kill"].sort(key=lambda r: order.index(r["name"]) if r["name"] in order else 99)
    return by


def _usual(n, u, pct=False):
    """(" (usual 31)", hot): today's count against its usual; hot when it
    runs UNUSUAL_X the usual and UNUSUAL_MIN (UNUSUAL_MIN_PCT) more."""
    if u is None or n is None:
        return "", False
    fmt = (lambda v: f"{v:.0%}") if pct else (lambda v: f"{v:,.0f}")
    hot = n >= UNUSUAL_X * u and n - u >= (UNUSUAL_MIN_PCT if pct else UNUSUAL_MIN)
    return (f" (usual {fmt(u)}, {n / u:.1f}x)" if hot and u > 0 else
            f" (usual {fmt(u)})"), hot


def _usual_of(r: dict, usual: dict):
    k = r.get("usual_key")
    if not k:
        return "", False
    return _usual((r.get("metric") or {}).get(k), usual.get(k))


def _closest_parts(by: dict, usual: dict, tz, until=None):
    """The kill switch that came closest to its limit without tripping (a
    trip leads the verdict on its own): {what, when, pct, limit, usual,
    hot}, or None when none moved toward its limit."""
    kills = [r for r in by["kill"] if r.get("pct") is not None and r.get("worst_val")
             and r["status"] != "TRIPPED"]
    k = max(kills, key=lambda r: r["pct"]) if kills else None
    if not k or k["pct"] <= 0:
        return None
    u, hot = _usual(k["pct"], usual.get("kill_pct"), pct=True)
    when = ""
    if k.get("worst_at"):
        when = f" at {_when(k['worst_at'], tz)}"
        note = _day_note(k["worst_at"], until, tz) if until else ""
        when += f" ({note[2:]})" if note else ""
    return {"what": f"{k['short']} {k['worst_val']}", "when": when, "pct": k["pct"],
            "limit": k["limit_phrase"], "usual": u, "hot": hot}


def _closest(by: dict, usual: dict, tz, until=None):
    """(text, hot) for the closest call."""
    p = _closest_parts(by, usual, tz, until)
    if not p:
        return "No kill switch moved toward its limit.", False
    return (f"Closest call: {p['what']}{p['when']}, {p['pct']:.0%} of the "
            f"{p['limit']}{p['usual']}."), p["hot"]


def headline(rows: list, usual: dict = None, tz=timezone.utc, until=None) -> str:
    """The verdict: tripped or not, then the closest call."""
    tripped = [r for r in rows if r["status"] == "TRIPPED"]
    head = ("TRIPPED: " + ", ".join(r["name"] for r in tripped) + "." if tripped
            else "Nothing tripped.")
    return head + " " + _closest(_blocks(rows), usual or {}, tz, until)[0]


def held_line(rows: list) -> str:
    held = _blocks(rows)["held"]
    if not held:
        return "Nothing held quoting back."
    return ("Holding back quoting: "
            + ", ".join(r.get("tag", r["name"]) + (" (earlier)" if r.get("earlier") else "")
                        for r in held) + ".")


def window_note(ev: dict, tz) -> str:
    if not ev["lines"]:
        return "!! no bot log lines in the last 24h: every verdict below is blind"
    return (f"{_when(ev['since'], tz)} -> {_when(ev['until'], tz)} ET, from the bot's "
            f"own logs")


def metrics(rows: list) -> dict:
    """What the history keeps for one day: each row's counts, plus the
    closest kill-switch call (kill_pct)."""
    m = {}
    for r in rows:
        for k, v in (r.get("metric") or {}).items():
            if v is not None:
                m[k] = round(float(v), 4)
    kills = [r["pct"] for r in rows
             if r["kind"] == "HALT" and r["status"] != "OFF" and r.get("pct") is not None]
    if kills:
        m["kill_pct"] = round(float(max(kills)), 4)
    return m


def load_history(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            h = json.load(f)
        return h if isinstance(h, dict) else {}
    except (OSError, ValueError):
        return {}


def usual(path: str, day: str) -> dict:
    """{metric: median over the USUAL_DAYS days on file before `day`
    (YYYY-MM-DD)}, for a metric on file for USUAL_MIN_DAYS of them."""
    h = load_history(path)
    vals = defaultdict(list)
    for d in sorted(d for d in h if d < day)[-USUAL_DAYS:]:
        for k, v in ((h.get(d) or {}).get("m") or {}).items():
            if isinstance(v, (int, float)):
                vals[k].append(v)
    return {k: statistics.median(v) for k, v in vals.items() if len(v) >= USUAL_MIN_DAYS}


def record(path: str, day: str, m: dict, until: float = None) -> None:
    """Keep `day`'s metrics (the 7:00 run's; a re-run replaces the day) and
    the last HISTORY_KEEP_DAYS days."""
    h = load_history(path)
    h[day] = {"m": m, "until": until}
    for d in sorted(h)[:-HISTORY_KEEP_DAYS]:
        h.pop(d, None)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---- rendering ---------------------------------------------------------------------
# Jack 2026-11-01, on the first cut: "it's still visually hard to read, improve
# it". So: the verdict in a coloured box, the kill switches with a headroom
# bar each, what held quoting back and the guards as tables (each guard's
# count beside its usual, in columns), the rest in two grey lines.

FOOT_NOTE = ("Account value is cash + positions against the 6am anchor, so reward credits "
             "and transfers move it too; the daily loss is trading P&L only.")
HOT = "#d35400"                  # a count at UNUSUAL_X its usual
RED, AMBER, GREEN = "#c0392b", "#e67e22", "#27ae60"


def _esc(s) -> str:
    """HTML-escape, with every non-ASCII character as an entity."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .encode("ascii", "xmlcharrefreplace").decode("ascii"))


def _usual_html(u: str, hot: bool) -> str:
    if not u:
        return ""
    return (f'<b style="color:{HOT}">{_esc(u)}</b>' if hot else
            f'<span style="color:#888">{_esc(u)}</span>')


def _units(n, unit: str) -> str:
    return f"{unit}{_s(n)}" if unit else ""


def _kill_cell(r: dict, tz, until) -> str:
    """A kill switch's worst, its % aside: the value, when, and its day."""
    if r.get("cell"):
        return r["cell"]
    if r.get("worst_val") and r.get("worst_at"):
        return (f"{r['worst_val']} · {_when(r['worst_at'], tz)}"
                + _day_note(r["worst_at"], until, tz))
    return r["worst"] or "-"


def _kills(by: dict):
    """(the kill switches for the table, the HALT file's row while it is absent)."""
    rows, halt = [], None
    for r in by["kill"]:
        if r["name"] == "Manual HALT file" and r["status"] != "TRIPPED":
            halt = r
        else:
            rows.append(r)
    return rows, halt


def _room_txt(by: dict) -> str:
    """Room to spare, a % of each limit where there is one."""
    out = []
    for r in by["room"]:
        if r["kind"] == "CAPACITY" and r.get("pct") is not None and "/" in (r.get("brief") or ""):
            out.append(f"{r['short']} {r['pct']:.0%}")
        else:
            out.append(f"{r['short']} {r['brief']}")
    return " · ".join(out)


def _design_txt(by: dict) -> str:
    return " · ".join(r.get("brief") or r["impact"] for r in by["design"])


def _bar_html(pct) -> str:
    """A 100px headroom bar: green under half the limit, amber to 80%, red past."""
    p = max(0.0, pct or 0.0)
    w = int(round(min(p, 1.0) * 100))
    col = RED if p >= CLOSE_FRAC else AMBER if p >= 0.5 else GREEN
    cell = "height:8px;line-height:8px;font-size:0;padding:0;"
    return ('<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
            'display:inline-table;vertical-align:middle;margin-right:6px"><tr>'
            + (f'<td style="{cell}width:{w}px;background:{col}"></td>' if w else "")
            + (f'<td style="{cell}width:{100 - w}px;background:#e3e3e3"></td>' if w < 100 else "")
            + "</tr></table>")


def text_lines(rows: list, ev: dict, tz, caps_note: str, usual: dict = None) -> list:
    """Plain-text version, in the html's order and in aligned columns.
    caps_note shows only when the caps are NOT the bot's (it starts "!!")."""
    usual = usual or {}
    by = _blocks(rows)
    until = ev["until"]
    L = ["RISK CONTROLS — " + window_note(ev, tz)]
    if caps_note.startswith("!!"):
        L.append("  " + caps_note)
    L.append("  " + headline(rows, usual, tz, until))
    for r in rows:
        if r["status"] == "TRIPPED":
            L.append(f"  !! TRIPPED {r['name']}: {r['impact']}")
    L.append("  " + held_line(rows))

    kills, halt = _kills(by)
    L += ["", "  KILL SWITCHES — each stops all quoting until the 6am roll",
          "    {:<17s}{:>5s}  {:<38s}{:<26s}{}".format("", "worst", "", "limit",
                                                     "now, since 6am")]
    for r in kills:
        mark = "!!" if r["status"] == "TRIPPED" else "! " if r["status"] == "CLOSE" else "  "
        pct = f"{r['pct']:.0%}" if r.get("pct") is not None else ""
        L.append("  {}{:<17s}{:>5s}  {:<38s}{:<26s}{}".format(
            mark, r.get("title", r["name"]), pct, _kill_cell(r, tz, until), r["limit"],
            r["now"]))
    L.append("    " + (f"HALT file {halt['now']}. " if halt else "") + FOOT_NOTE)

    if by["held"]:
        L += ["", "  HOLDING BACK QUOTING"]
        for r in by["held"]:
            u, _hot = _usual_of(r, usual)
            L.append("  {}{:<18s}{}{}".format("!!" if r.get("alarm") else "  ",
                                             r.get("title", r["name"]),
                                             r.get("what", r["impact"]), u))
            for w in r.get("where") or []:
                L.append(" " * 22 + w)
            if r.get("note"):
                L.append(" " * 22 + "(" + r["note"] + ")")

    L += ["", "  GUARDS — normal work; usual = the median of the 7 days before",
          "    {:<24s}{:>12s}{:>7s}  {}".format("", "today", "usual", "biggest")]
    for r in by["guard"]:
        n = r.get("count", 0)
        u = usual.get(r.get("usual_key"))
        hot = _usual(n, u)[1]
        today = f"{n:,}" + (f" {_units(n, r.get('unit', ''))}" if r.get("unit") else "")
        L.append("  {}{:<24s}{:>12s}{:>7s}  {}".format(
            "! " if hot else "  ", r["title"], today,
            f"{u:,.0f}" if u is not None else "-", r.get("detail", "")))
    if by["quiet"]:
        L.append("    Quiet: " + ", ".join(r["short"] for r in by["quiet"]) + ".")
    if by["off"]:
        L.append("    Off: " + "; ".join(r.get("short", r["name"]) for r in by["off"]) + ".")
    L.append("")
    if by["room"]:
        L.append("  ROOM TO SPARE: " + _room_txt(by))
    if by["design"]:
        L.append("  BY DESIGN: " + _design_txt(by))
    return L


def html(rows: list, ev: dict, tz, caps_note: str, td: str = "", tdl: str = "",
         usual: dict = None) -> str:
    """The email block. td / tdl (the digest's gridded cell styles) are
    unused now: these tables keep to light row rules."""
    usual = usual or {}
    by = _blocks(rows)
    until = ev["until"]
    tripped = [r for r in rows if r["status"] == "TRIPPED"]
    th = ("padding:2px 12px 3px 0;text-align:left;font-size:11px;color:#888;"
          "font-weight:600;border-bottom:1px solid #ddd")
    c = "padding:4px 12px 4px 0;border-bottom:1px solid #eee;vertical-align:top"
    num = c + ";text-align:right;white-space:nowrap"
    mono = "font-family:Consolas,Menlo,monospace;font-size:11px;color:#444"
    grey = "font-size:11px;color:#888;margin:2px 0"
    tbl = ('<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
           'font-size:12px;margin:2px 0 4px">')

    def head(title, sub=""):
        return ('<div style="font-size:11px;font-weight:700;color:#555;letter-spacing:.06em;'
                'margin:14px 0 3px">' + title.upper()
                + (f' <span style="font-weight:400;letter-spacing:0;color:#999">'
                   f'&middot; {_esc(sub)}</span>' if sub else "") + "</div>")

    h = ['<div style="font-size:15px;font-weight:600;margin:16px 0 4px">Risk controls '
         '<span style="color:#888;font-weight:400;font-size:12px">{}</span></div>'.format(
             _esc(window_note(ev, tz)))]
    if caps_note.startswith("!!"):
        h.append(f'<div style="color:{RED};font-size:12px;font-weight:600">'
                 f'{_esc(caps_note)}</div>')

    # the verdict
    col, bg = (RED, "#fdecea") if tripped else ("#1e8449", "#eafaf1")
    v = [f'<div style="font-size:14px;font-weight:700;color:{col}">'
         + ("Tripped" if tripped else "Nothing tripped") + "</div>"]
    for r in tripped:
        v.append(f'<div style="color:{RED};font-weight:600">{_esc(r["name"])}: '
                 f'{_esc(r["impact"])}</div>')
    p = _closest_parts(by, usual, tz, until)
    v.append("<div>Closest call: <b>{}</b>{}, <b>{:.0%}</b> of the {}{}</div>".format(
        _esc(p["what"]), _esc(p["when"]), p["pct"], _esc(p["limit"]),
        _usual_html(p["usual"], p["hot"])) if p else
        "<div>No kill switch moved toward its limit.</div>")
    v.append(f"<div>{_esc(held_line(rows))}</div>")
    h.append(f'<div style="border-left:4px solid {col};background:{bg};padding:7px 12px;'
             f'margin:4px 0;font-size:13px;line-height:1.55">' + "".join(v) + "</div>")

    # the kill switches, each with a headroom bar
    kills, halt = _kills(by)
    h.append(head("Kill switches", "each stops all quoting until the 6am roll"))
    h.append(tbl + f'<tr><td style="{th}">Switch</td><td style="{th}">Worst in the window'
             f'</td><td style="{th}">Limit</td><td style="{th}">Now, since 6am</td></tr>')
    for r in kills:
        pct = r.get("pct")
        tone = (f";color:{RED};font-weight:700" if r["status"] == "TRIPPED" else
                f";color:{AMBER};font-weight:700" if r["status"] == "CLOSE" else "")
        h.append(f'<tr><td style="{c};font-weight:600;white-space:nowrap">'
                 f'{_esc(r.get("title", r["name"]))}</td><td style="{c}{tone}">'
                 + (_bar_html(pct) + f"<b>{pct:.0%}</b> &nbsp;" if pct is not None else "")
                 + f'{_esc(_kill_cell(r, tz, until))}</td>'
                 f'<td style="{c};color:#666;white-space:nowrap">{_esc(r["limit"])}</td>'
                 f'<td style="{c};white-space:nowrap">{_esc(r["now"])}</td></tr>')
    h.append("</table>")
    h.append(f'<div style="{grey}">' + (_esc(f"HALT file {halt['now']}. ") if halt else "")
             + _esc(FOOT_NOTE) + "</div>")

    # what held quoting back
    if by["held"]:
        h.append(head("Holding back quoting"))
        h.append(tbl)
        for r in by["held"]:
            u, hot = _usual_of(r, usual)
            dot = RED if r.get("alarm") else AMBER
            h.append(f'<tr><td style="{c};white-space:nowrap"><span style="color:{dot}">'
                     f'&#9679;</span> <b>{_esc(r.get("title", r["name"]))}</b></td>'
                     f'<td style="{c}">{_esc(r.get("what", r["impact"]))}{_usual_html(u, hot)}'
                     + (f'<div style="color:#888;font-size:11px">{_esc(r["note"])}</div>'
                        if r.get("note") else "")
                     + f'</td><td style="{c};{mono}">'
                     + "<br>".join(_esc(w) for w in (r.get("where") or [])) + "</td></tr>")
        h.append("</table>")

    # the guards, today against their usual
    h.append(head("Guards", "normal work; usual = the median of the 7 days before"))
    h.append(tbl + f'<tr><td style="{th}">Guard</td><td style="{th};text-align:right">Today'
             f'</td><td style="{th};text-align:right">Usual</td><td style="{th}">Biggest'
             f'</td></tr>')
    for r in by["guard"]:
        n = r.get("count", 0)
        u = usual.get(r.get("usual_key"))
        hot = _usual(n, u)[1]
        unit = _units(n, r.get("unit", ""))
        h.append(f'<tr><td style="{c};font-weight:600;white-space:nowrap">'
                 f'{_esc(r["title"])}</td><td style="{num}'
                 + (f";color:{HOT}" if hot else "") + f'"><b>{n:,}</b>'
                 + (f' <span style="font-size:11px;color:#888">{unit}</span>' if unit else "")
                 + (" &#9650;" if hot else "") + "</td>"
                 f'<td style="{num};color:#888">'
                 + (f"{u:,.0f}" if u is not None else "&ndash;") + "</td>"
                 f'<td style="{c};color:#555">{_esc(r.get("detail", ""))}</td></tr>')
    h.append("</table>")
    if by["quiet"]:
        h.append(f'<div style="{grey}"><b>Quiet:</b> '
                 f'{_esc(", ".join(r["short"] for r in by["quiet"]))}</div>')
    if by["off"]:
        h.append(f'<div style="{grey}"><b>Off:</b> '
                 f'{_esc("; ".join(r.get("short", r["name"]) for r in by["off"]))}</div>')
    if by["room"]:
        h.append(f'<div style="{grey};margin-top:8px"><b>Room to spare:</b> '
                 f'{_esc(_room_txt(by))}</div>')
    if by["design"]:
        h.append(f'<div style="{grey}"><b>By design:</b> {_esc(_design_txt(by))}</div>')
    return "".join(h)
