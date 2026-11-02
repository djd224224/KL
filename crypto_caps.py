r"""crypto_caps.py -- which crypto-fleet caps were hit in the last 24 hours.

Jack 2026-11-01: "make sure daily crypto email clearly alerts if any caps are
hit". Every cycle the bots log what each cap is measured on: the event net and
its dollars at risk per side, each quoted market's position, and (weekly and
annual) the net across all of a bot's events. This reads the last 24 hours of
those lines from the bots' day logs and checks them against each fleet's live
limits, taken from the bot modules. No bot change, no API calls.

Caps (a cap is HIT when the logged value reaches its limit):
  position     |position| on one market            >= max_position
  event        |net| across one event's markets     >= max_event
  asset        |net| across a bot's events          >= max_asset (weekly, annual)
  risk_short   $ at risk if the short side loses    >= the event's $ cap
  risk_long    $ at risk if the long side loses     >= the event's $ cap
  reduce_only  |event net|                          >= reduce_only_at
  backstop     a "side cap" / "level cap: NOT placing" line (should never happen)

The log values are rounded the way the bots print them (whole contracts and
dollars), so a cap reads as hit once the rounded value reaches it. A live bot
with no cycle lines in the window is reported as BLIND rather than as clean.
"""

import glob
import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone

import pytz

LOG_ROOT = os.environ.get("CMM_RUN_LOGS", r"C:\Users\jackd\Documents\KL\run-logs")
ET = pytz.timezone("US/Eastern")
WINDOW_HOURS = 24
NEAR_RATIO = 0.8          # "near" list: peak use at 80%+ of a limit, not hit
NOW_TOLERANCE_SECS = 900  # an observation this close to the bot's last cycle is "now"
HEARTBEAT_FRESH_MIN = 30
CHUNK = 8 << 20

TS = r"(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ)"
NUM = r"[+-]?\d+(?:\.\d+)?"
MONTHLY_HEAD = re.compile(
    TS + r" \[(?P<bot>[A-Z]+-M(?:AX|IN))\] .*? event=(?P<event>\S+) net=(?P<net>" + NUM
    + r") risk short/long \$(?P<rs>" + NUM + r")/\$(?P<rl>" + NUM + r")")
UD_HEAD = re.compile(
    TS + r" \[(?P<bot>[A-Z]+) (?:ud|yr)\] [A-Z]+=\$[\d,.]+ .*? net=(?P<net>" + NUM
    + r") events=\d+")
UD_EVENT = re.compile(
    TS + r"\s+(?P<event>KX\S+) \[(?P<cad>\w+)\] [\d.]+h left, \d+ strikes -> \d+ quotable, "
    r"net=(?P<net>" + NUM + r")(?:, skew=[+-]?[\d.]+c)?"
    r"(?:, risk short/long \$(?P<rs>" + NUM + r")/\$(?P<rl>" + NUM + r") cap \$(?P<cap>[\d.e+]+))?")
STRIKE = re.compile(
    TS + r"\s+(?P<t>KX\S+) (?:strike=(?P<k1>[^:\s]+):?|K=(?P<k2>\S+)) fair=\d+c ext \S+ "
    r"pos=(?P<pos>" + NUM + r") ")
BACKSTOP = re.compile(TS + r" \[[^\]]+\] ! (?P<which>side|level) cap: NOT placing (?P<what>.*)")
FIRST_TS = re.compile(rb"(?:^|\n)(\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ) ")

KIND_TEXT = {
    "position": "position cap",
    "event": "event cap (contracts across the event)",
    "asset": "asset cap (contracts across all its events)",
    "risk_short": "$-at-risk cap, short side",
    "risk_long": "$-at-risk cap, long side",
    "reduce_only": "reduce-only line (it stops adding)",
    "backstop": "order backstop blocked a placement",
}
KIND_ORDER = ("risk_short", "risk_long", "reduce_only", "event", "asset", "position", "backstop")


def fleet_specs():
    """[(fleet, status dir, log-glob for a bot key, limits)] for the three
    live fleets, limits read off the bot classes the running bots use."""
    import crypto_touch_mm as mm
    import crypto_updown_mm as ud
    import crypto_annual_mm as ann
    m = mm.MonthlyTouchMarketMaker
    u = ud.UpDownMarketMaker
    a = ann.AnnualMarketMaker
    return [
        ("monthly", os.path.join(LOG_ROOT, "crypto-touch"), lambda k: f"{k.lower()}-2*.log",
         {"position": m.max_position, "event": m.max_event, "risk": m.max_event_risk_dollars,
          "reduce_only": {"*": m.reduce_only_at}}),
        ("weekly", os.path.join(LOG_ROOT, "crypto-updown"), lambda k: f"{k.lower()}-*.log",
         {"position": u.max_position, "event": u.max_event, "asset": u.max_asset,
          "risk": dict(u.max_event_risk_by_cadence),
          "reduce_only": dict(u.reduce_only_by_cadence)}),
        ("annual", os.path.join(LOG_ROOT, "crypto-annual"), lambda k: f"{k.lower()}-*.log",
         {"position": a.max_position, "event": a.max_event, "asset": a.max_asset,
          "risk": dict(a.max_event_risk_by_cadence),
          "reduce_only": dict(a.reduce_only_by_cadence)}),
    ]


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%SZ").replace(tzinfo=timezone.utc)


def lines_since(path, cutoff, chunk=CHUNK):
    """The file's lines stamped at or after `cutoff` (a "YYYY-MM-DD HH:MM:SSZ"
    string), reading backwards from the end only as far as it must: the day
    logs run to hundreds of MB. Lines without a timestamp are dropped."""
    size = os.path.getsize(path)
    blocks, pos = [], size
    with open(path, "rb") as f:
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            blocks.append(f.read(step))
            m = FIRST_TS.search(blocks[-1])
            if m and m.group(1).decode() < cutoff and pos > 0:
                break
    data = b"".join(reversed(blocks))
    if pos > 0:                       # drop the partial first line
        data = data[data.find(b"\n") + 1:]
    for raw in data.split(b"\n"):
        line = raw.decode("utf-8", "replace").rstrip("\r")
        if len(line) > 20 and line[:4].isdigit() and line[:20] >= cutoff:
            yield line


class Track:
    """One cap at one place (a market, an event, a bot) over the window."""
    __slots__ = ("kind", "where", "limit", "n", "n_at", "peak", "peak_ts",
                 "first_at", "last_at", "last_ts", "last_v", "last_hit")

    def __init__(self, kind, where):
        self.kind, self.where = kind, where
        self.limit = None
        self.n = self.n_at = 0
        self.peak, self.peak_ts = 0.0, None
        self.first_at = self.last_at = self.last_ts = None
        self.last_v, self.last_hit = 0.0, False

    def obs(self, ts, value, limit):
        self.n += 1
        self.limit = limit
        v = abs(value)
        if self.peak_ts is None or v > self.peak:
            self.peak, self.peak_ts = v, ts
        hit = v >= limit
        if hit:
            self.n_at += 1
            self.first_at = self.first_at or ts
            self.last_at = ts
        self.last_ts, self.last_v, self.last_hit = ts, value, hit


def scan_bot(fleet, files, cutoff, limits):
    """(tracks, header timestamps) for one bot from its logs' recent lines."""
    tracks, heads = {}, []

    def track(kind, where, ts, value, limit):
        if limit is None or not limit > 0 or limit == float("inf"):
            return
        t = tracks.get((kind, where))
        if t is None:
            t = tracks[(kind, where)] = Track(kind, where)
        t.obs(ts, value, limit)

    for path in files:
        for line in lines_since(path, cutoff):
            if " pos=" in line:
                m = STRIKE.match(line)
                if m:
                    k = m.group("k1") or m.group("k2")
                    track("position", f"{m.group('t')}|{k}", m.group("ts"),
                          float(m.group("pos")), limits.get("position"))
                    continue
            if fleet == "monthly":
                m = MONTHLY_HEAD.match(line)
                if m:
                    ts, ev, net = m.group("ts"), m.group("event"), float(m.group("net"))
                    heads.append(ts)
                    track("event", ev, ts, net, limits.get("event"))
                    track("reduce_only", ev, ts, net, limits["reduce_only"].get("*"))
                    track("risk_short", ev, ts, float(m.group("rs")), limits.get("risk"))
                    track("risk_long", ev, ts, float(m.group("rl")), limits.get("risk"))
                    continue
            else:
                m = UD_HEAD.match(line)
                if m:
                    heads.append(m.group("ts"))
                    track("asset", m.group("bot"), m.group("ts"), float(m.group("net")),
                          limits.get("asset"))
                    continue
                m = UD_EVENT.match(line)
                if m:
                    ts, ev, cad = m.group("ts"), m.group("event"), m.group("cad")
                    net = float(m.group("net"))
                    track("event", ev, ts, net, limits.get("event"))
                    track("reduce_only", ev, ts, net, limits["reduce_only"].get(cad))
                    if m.group("rs") is not None:
                        cap = float(m.group("cap"))
                        track("risk_short", ev, ts, float(m.group("rs")), cap)
                        track("risk_long", ev, ts, float(m.group("rl")), cap)
                    continue
            if "cap: NOT placing" in line:
                m = BACKSTOP.match(line)
                if m:
                    t = tracks.get(("backstop", m.group("which")))
                    if t is None:
                        t = tracks[("backstop", m.group("which"))] = Track(
                            "backstop", m.group("which"))
                    t.obs(m.group("ts"), 1, 1)
    return tracks, heads


def _cycle_secs(heads):
    if len(heads) < 3:
        return 30.0
    gaps = [(_ts(b) - _ts(a)).total_seconds() for a, b in zip(heads, heads[1:])]
    gaps = [g for g in gaps if g > 0]
    return min(900.0, max(15.0, statistics.median(gaps))) if gaps else 30.0


def _status_keys(status_dir):
    """{bot key: status dict} for every heartbeat in a fleet's status dir."""
    out = {}
    for path in sorted(glob.glob(os.path.join(status_dir, "status_*.json"))):
        key = os.path.basename(path)[len("status_"):-len(".json")]
        try:
            with open(path, encoding="utf-8") as f:
                out[key] = json.load(f)
        except Exception:
            out[key] = {}
    return out


def cap_report(now_utc=None, window_hours=WINDOW_HOURS, specs=None):
    """{"hits", "near", "top", "blind", "bots"} over the last `window_hours`.
    hits / near / top are dicts (see _item); blind lists bots whose heartbeat
    is fresh but whose logs had no cycle lines in the window."""
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff_dt = now_utc - timedelta(hours=window_hours)
    cutoff = cutoff_dt.strftime("%Y-%m-%d %H:%M:%SZ")
    hits, near, blind, every = [], [], [], []
    bots = 0
    for fleet, status_dir, glob_for, limits in (specs or fleet_specs()):
        for key, st in _status_keys(status_dir).items():
            bots += 1
            files = [p for p in glob.glob(os.path.join(status_dir, glob_for(key)))
                     if os.path.getmtime(p) >= cutoff_dt.timestamp()]
            files.sort(key=os.path.getmtime)
            tracks, heads = scan_bot(fleet, files, cutoff, limits)
            if not heads:
                if _fresh(st, now_utc):
                    blind.append(f"{key} {fleet}")
                continue
            cyc = _cycle_secs(heads)
            last_head = _ts(heads[-1])
            for t in tracks.values():
                it = _item(fleet, key, t, cyc, last_head)
                every.append(it)
                if t.n_at:
                    hits.append(it)
                elif it["ratio"] >= NEAR_RATIO:
                    near.append(it)
    order = {k: i for i, k in enumerate(KIND_ORDER)}
    hits.sort(key=lambda i: (not i["now"], order[i["kind"]], -i["ratio"]))
    near.sort(key=lambda i: -i["ratio"])
    top = max((i for i in every if i["kind"] != "backstop"), key=lambda i: i["ratio"],
              default=None)
    return {"hits": hits, "near": near, "top": top, "blind": blind, "bots": bots,
            "window_hours": window_hours}


def _fresh(st, now_utc):
    try:
        upd = datetime.strptime(st.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return False
    return (now_utc - upd).total_seconds() <= HEARTBEAT_FRESH_MIN * 60


def _item(fleet, key, t, cyc, last_head):
    now = (t.last_hit and t.last_ts is not None
           and (last_head - _ts(t.last_ts)).total_seconds() <= NOW_TOLERANCE_SECS)
    return {"fleet": fleet, "bot": key, "kind": t.kind, "where": t.where,
            "limit": t.limit, "peak": t.peak, "peak_ts": t.peak_ts, "last": t.last_v,
            "ratio": (t.peak / t.limit) if t.limit else 0.0,
            "now": bool(now), "first_at": t.first_at, "last_at": t.last_at,
            "secs_at": t.n_at * cyc, "count": t.n_at}


# ---- wording -----------------------------------------------------------------

def _et(ts):
    return _ts(ts).astimezone(ET).strftime("%a %H:%M ET") if ts else "?"


def _where(item):
    w = item["where"]
    if item["kind"] == "position":
        ticker, k = (w.split("|", 1) + [""])[:2]
        return f" on the {k} strike" + ("" if item["fleet"] == "monthly" else
                                        f" of {ticker.split('-')[1]}")
    if item["kind"] in ("event", "reduce_only", "risk_short", "risk_long") \
            and item["fleet"] != "monthly":
        return f" on {w}"
    return ""


def _side(item):
    if item["kind"] in ("position", "event", "reduce_only", "asset") and item["last"]:
        return " short" if item["last"] < 0 else " long"
    return ""


def _amount(item, v):
    if item["kind"] in ("risk_short", "risk_long"):
        return f"${v:,.0f}"
    if item["kind"] == "backstop":
        return f"{int(item['count'])} blocked order(s)"
    return f"{v:,.0f}"


def _dur(secs):
    return f"{secs / 3600:.1f}h" if secs >= 5400 else f"{max(1, round(secs / 60))} min"


def describe(item):
    """{bot, cap, value, when} display strings for one hit or near item."""
    out = {"bot": f"{item['bot']} {item['fleet']}",
           "cap": KIND_TEXT[item["kind"]] + _where(item)}
    if item["kind"] == "backstop":
        out["value"] = _amount(item, 0)
        out["when"] = (f"last {_et(item['last_at'])} (a backstop that should never fire: "
                       f"worth a look)")
        return out
    out["value"] = (f"{_amount(item, item['peak'])}{_side(item)} of "
                    f"{_amount(item, item['limit'])}")
    if item["count"]:
        dur = f"{_dur(item['secs_at'])} at the cap in the last {int(WINDOW_HOURS)}h"
        out["when"] = (f"AT THE CAP NOW ({dur})" if item["now"]
                       else f"cleared, last at the cap {_et(item['last_at'])} ({dur})")
    else:
        out["when"] = f"{100 * item['ratio']:.0f}% of the cap at its peak, {_et(item['peak_ts'])}"
    return out


def hit_text(item):
    """One plain-words line for a cap hit."""
    d = describe(item)
    return f"CAP HIT: {d['bot']} -- {d['cap']}: {d['value']}; {d['when']}"


def near_text(item):
    d = describe(item)
    return f"{d['bot']} {d['cap']}: peak {d['value']} ({100 * item['ratio']:.0f}%)"


def limits_text(specs=None):
    """The live limits per fleet, one line."""
    parts = []
    for fleet, _d, _g, lim in (specs or fleet_specs()):
        risk = lim["risk"] if not isinstance(lim["risk"], dict) else \
            next(iter(lim["risk"].values()), None)
        ro = next(iter(lim["reduce_only"].values()), None)
        bits = [f"{lim['position']:,.0f}/market", f"{lim['event']:,.0f}/event"]
        if lim.get("asset"):
            bits.append(f"{lim['asset']:,.0f}/asset")
        if risk:
            bits.append(f"${risk:,.0f} at risk per side")
        if ro:
            bits.append(f"reduce-only at {ro:,.0f}")
        parts.append(f"{fleet} " + ", ".join(bits))
    return "Limits: " + " | ".join(parts)


def summary_text(rep):
    """The always-on caps line for the top of the email."""
    if rep is None:
        return "Caps: NOT CHECKED this morning (the cap check failed; see digest-task.log)"
    if rep["hits"]:
        n_now = sum(1 for i in rep["hits"] if i["now"])
        return (f"Caps: {len(rep['hits'])} HIT in the last {int(rep['window_hours'])}h"
                f" ({n_now} still at the cap now) -- details below")
    top = rep["top"]
    tail = (f"; highest use: {near_text(top)}" if top else "")
    return (f"Caps: none hit in the last {int(rep['window_hours'])}h across {rep['bots']} bots"
            + tail)
