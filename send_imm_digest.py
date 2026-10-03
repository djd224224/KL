#!/usr/bin/env python3
r"""
send_imm_digest.py — the incentive-rewards MM bot's morning section: since
2026-10-02 (Jack: "cut it as a standalone email and add it into the Kalshi
portfolio ... email") it rides inside the 7:00 portfolio email, which runs
this script with --section-out. Run with no arguments (the old 7:10 task) it
does nothing; --test still sends the section as an email of its own.

Layout: lifetime net, the PICK-OFF block, the P&L windows (yesterday, 7 days,
lifetime), the last 30 days' daily P&L, the cutoff audit and a one-line
health check. The risk controls (every halt, cap and guard: what fired, what
binds, what has slack — see imm_risk_controls) are built here but placed by
the portfolio email under its chart; the finecon / open-scan tracker was
removed 2026-10-03.

Everything is the INCENTIVE BOT's own book, not the raw account. Yesterday, 7
days and the daily table are the IMM dashboard's own figures, read from the
summary it writes beside its page (imm_dashboard_summary.json): trading P&L
marked to market and modeled rewards per ET calendar day, so the email and the
page cannot disagree. Lifetime comes from the bot's persisted counters
(imm_state.json realized_lifetime + its own book at mid; status reward
estimate).

Credentials (--test): ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD from the
environment, falling back to HKCU\Environment (Task Scheduler's stripped env).
"""

import argparse
import ast
import csv
import glob
import json
import os
import re
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone


def _env_from_registry(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError):     # ImportError: non-Windows (tests)
        return ""


LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "run_incentive_mm.ps1")


def _apply_launcher_env() -> dict:
    """Mirror the LIVE bot's config by parsing `set NAME=VALUE&&` pairs out of
    the launcher's $ProbeEnv string. Must run BEFORE incentive_mm is imported —
    its config is read at import time.

    Required for the capacity section to mean anything: on defaults this
    process sees a $1,000 budget and a 35-event cap, where the live bot runs
    $50,000 and 75. Reporting headroom against the wrong ceiling is worse than
    reporting none, so the section says so loudly when the parse comes back
    empty. Same helper (and same gotcha) as imm_quote_gaps.py."""
    applied = {}
    try:
        with open(LAUNCHER_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return applied
    for chunk in re.findall(r'\$ProbeEnv\s*=\s*"(set .*?)"', text, re.S):
        for name, val in re.findall(
                r"set ([A-Za-z_][A-Za-z0-9_]*)=([^&]*)&&", chunk):
            os.environ[name] = val
            applied[name] = val
    return applied


LAUNCHER_ENV = _apply_launcher_env()

for _v in ("ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD"):
    if not os.environ.get(_v):
        _val = _env_from_registry(_v)
        if _val:
            os.environ[_v] = _val

# Import AFTER the env fixup so the module-level cred constants pick them up.
import incentive_mm as imm                                          # noqa: E402
import imm_pickoff                                                  # noqa: E402
from incentive_mm import (CT, ET, STATUS_DIR, Alerter,  # noqa: E402
                          build_client, bulk_mark_cents, log, market_cents)

STALE_AFTER_MINUTES = 30
STATE_PATH = os.path.join(STATUS_DIR, "imm_state.json")
STATUS_PATH = os.path.join(STATUS_DIR, "status_incentive_mm.json")
# NOTE on reconciling estimates vs Kalshi credits (Jack 2026-07-21): a naive
# same-day comparison is WRONG — programs run multiple days and a day's
# accrual can pay out across the program's life, so an "est $536 vs paid $300"
# single-day ratio understates realization. A short-lived 0.56 "expected paid"
# haircut was removed for exactly that reason; the digest reports the raw
# estimate only.


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def _short_event(event_ticker: str) -> str:
    """Compact label for the table: drop the leading 'KX'."""
    return event_ticker[2:] if event_ticker.startswith("KX") else event_ticker


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def own_book(state: dict):
    """(own_pos, own_avg) in signed contracts and entry-cost cents."""
    pos = {t: _f(v) for t, v in (state.get("own_pos") or {}).items() if abs(_f(v)) > 0.01}
    avg = {t: _f(v) for t, v in (state.get("own_avg") or {}).items()}
    return pos, avg


# results[t] for a voided market. A void refunds cost, so the position settles
# at its own entry price for zero settlement P&L; there is no YES value.
VOID = "void"


def settlement_cents(m: dict):
    """What one YES contract of market `m` settled at, in cents, or None while
    Kalshi has not settled it. A yes / no result pays 100 / 0. A scalar pays
    imm.settlement_value_cents, read exactly: the NFL ladders and escalators
    settle at values like "0.2160", which is 21.6c, never rounded to the
    penny. A void returns VOID. As in incentive_mm's _settle_or_drop, a
    scalar or void counts only once the market is finalized
    (imm.SETTLED_STATUSES), and a scalar without a readable value is not
    settled. Before 2026-09-29 a scalar-settled position was marked at the
    last trade: a ladder that settled at 12c was carried at 82c."""
    res = str(m.get("result") or "").lower()
    if res in ("yes", "no"):
        return 100.0 if res == "yes" else 0.0
    if res not in ("scalar", "void") or \
            str(m.get("status") or "").lower() not in imm.SETTLED_STATUSES:
        return None
    return VOID if res == "void" else imm.settlement_value_cents(m)


def current_mids(client, tickers):
    """(mids, results): ticker -> YES mark in CENTS, the bot's own
    incentive_mm.bulk_mark_cents (the bid/ask mid of a two-sided book; the
    last trade clamped inside a book MARK_WIDE_SPREAD_CENTS or wider, or to
    the live side of a one-sided one: an empty ask reads $1.00 and is never
    averaged in), and ticker -> settlement_cents() (cents, or VOID) for
    settled markets so the caller can book settlement P&L. A settled market's
    book reads 0 / 100, so its entry in `mids` is the last trade, never the
    settlement value; value a settled position from `results`."""
    mids, results = {}, {}
    tickers = list(tickers)
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            resp = client.get_markets(tickers=",".join(chunk), limit=len(chunk))
        except Exception as e:
            log(f"! mids read failed for a chunk: {e}")
            continue
        for m in (resp.get("markets") or []):
            t = m.get("ticker", "")
            px = settlement_cents(m)
            if px is not None:
                results[t] = px
            mk = bulk_mark_cents(m)
            if mk is not None:
                mids[t] = mk
    return mids, results


def event_rows(client):
    """Per-event rollup of the bot's own book. Returns (rows, totals, resting)."""
    state = load_json(STATE_PATH)
    pos, avg = own_book(state)

    # Resting imm- orders: which events are actively quoted + capital deployed.
    resting_by_event = {}
    resting_collateral = 0.0
    try:
        cursor = None
        for _page in range(40):
            resp = client.get_orders(status="resting", limit=200, cursor=cursor)
            batch = resp.get("orders") or []
            for o in batch:
                if o.get("status") != "resting" or \
                        not str(o.get("client_order_id", "")).startswith("imm-"):
                    continue
                t = o.get("ticker", "")
                px = market_cents(o, "yes_price")
                if px is None and o.get("yes_price_dollars") is not None:
                    px = round(_f(o.get("yes_price_dollars")) * 100)
                rem = _f(o.get("remaining_count_fp") or o.get("remaining_count") or o.get("count"))
                side = o.get("side")
                # collateral: a YES buy reserves px; a NO buy reserves 100-price
                if side == "no":
                    npx = market_cents(o, "no_price")
                    if npx is None and o.get("no_price_dollars") is not None:
                        npx = round(_f(o.get("no_price_dollars")) * 100)
                    reserve = (npx or 0)
                else:
                    reserve = (px or 0)
                resting_collateral += reserve / 100.0 * rem
                ev = _event_of(t)
                resting_by_event[ev] = resting_by_event.get(ev, 0) + 1
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
    except Exception as e:
        log(f"! resting-order read failed: {e}")

    # This function only reports the CURRENT open book and resting quotes, so
    # it no longer replays fills (the section's P&L windows are the
    # dashboard's: dashboard_windows).
    realized = {}
    mids, _results = current_mids(client, set(pos))

    events = {}
    for t, p in pos.items():
        ev = _event_of(t)
        d = events.setdefault(ev, {"realized": 0.0, "unrealized": 0.0,
                                   "net_pos": 0.0, "exposure": 0.0, "mkts": 0})
        d["net_pos"] += p
        d["mkts"] += 1
        a = avg.get(t, 0.0)
        mid = mids.get(t)
        if mid is not None:
            d["unrealized"] += p * (mid - a) / 100.0
        d["exposure"] += (p * a if p > 0 else -p * (100 - a)) / 100.0
    for t, r in realized.items():
        ev = _event_of(t)
        events.setdefault(ev, {"realized": 0.0, "unrealized": 0.0,
                               "net_pos": 0.0, "exposure": 0.0, "mkts": 0})
        events[ev]["realized"] += r

    rows = []
    tot = {"realized": 0.0, "unrealized": 0.0, "net_pos": 0.0, "exposure": 0.0}
    for ev, d in events.items():
        for k in tot:
            tot[k] += d[k]
        d["pnl"] = d["realized"] + d["unrealized"]
        d["quoted"] = resting_by_event.get(ev, 0)
        if (abs(d["realized"]) > 0.005 or abs(d["unrealized"]) > 0.005
                or abs(d["net_pos"]) > 0.5 or d["quoted"] > 0):
            rows.append((ev, d))
    rows.sort(key=lambda r: -r[1]["pnl"])
    return rows, tot, {"collateral": resting_collateral,
                       "orders": sum(resting_by_event.values()),
                       "events": len(resting_by_event)}


# Rewards actually CREDITED by Kalshi. There is no credits endpoint (verified
# 2026-08-03 across 8 paths), so the numbers come from the account statement
# via `imm_reward_recon.py --statement <paste>`, which keeps a permanent
# per-credit ledger and writes reward_calibration.json. The env vars below are
# a manual fallback for when the ledger has not been refreshed.
#
# 2026-08-04: the first COMPLETE statement was reconciled and it retired a
# large piece of folklore. Every credit this account has ever received is in
# the ledger (2,721 rows summing to the statement's own lifetime total to the
# penny), and it shows:
#   * only $112.85 of credit predates IMM's 2026-07-12 go-live, not ~$1,500;
#   * the KXHIGH weather bot earned $5.80 of liquidity incentive in its LIFE,
#     so the "~$1,500 of non-IMM reward from 152 pre-IMM days" figure — a
#     modelled replay, never a measurement — was wrong by more than 10x and
#     is deleted rather than re-tuned;
#   * non-IMM credit is ~$780, and it is identifiable event by event (MLB /
#     fight / mention markets belong to the other bots on this key).
# Attribution is now per-event against the events IMM actually quoted, so no
# hand-set offset is needed at all.
CALIB_PATH = os.path.join(STATUS_DIR, "reward_calibration.json")
CREDITS_PATH = os.path.join(STATUS_DIR, "reward_credits.csv")
# Credits arrive 1-2 days after the liquidity that earned them (measured lag:
# median 1.3d, p90 1.7d, max 3.0d), so a ledger more than this many days
# behind is genuinely missing money rather than merely waiting on settlement.
LEDGER_STALE_DAYS = int(os.environ.get("IMM_LEDGER_STALE_DAYS", "4"))
# IMM_REWARDS_CREDITED / IMM_REWARDS_CREDITED_MTD are GONE (2026-08-04). They
# were hand-set account-level statement totals; nothing reads them now that the
# reported reward is the bot estimate and the ledger is per-event. The digest
# scheduled task still exports them — harmless, but delete them from the task
# when convenient so a stale value can never look meaningful again.


def load_credit_ledger():
    """(rows, calibration) — the per-credit ledger and the summary written by
    imm_reward_recon.py. Empty/absent is fine: the digest falls back to the
    bot's own estimate and says so."""
    rows = []
    try:
        with open(CREDITS_PATH, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((r["credit_date"], r["event_ticker"],
                             _f(r["amount"])))
    except (OSError, KeyError, ValueError):
        rows = []
    calib = load_json(CALIB_PATH) or {}
    return rows, calib


def et_midnight_ts(day) -> float:
    """Epoch of 00:00 ET on `day` (a date)."""
    return ET.localize(datetime(day.year, day.month, day.day)).timestamp()


def _et_label(ts) -> str:
    """'00:00 ET Fri Sep 26' for an epoch."""
    t = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET)
    return t.strftime("%H:%M ET %a %b ") + str(t.day)


def status_summary(status: dict) -> dict:
    """Activity figures from the last stored daily summary (yesterday's
    completed roll), falling back to the live heartbeat counters. The
    section's day and week rewards are the dashboard's (dashboard_windows)."""
    body = status.get("summary_body") or ""

    def grab(pat, default=0.0):
        m = re.search(pat, body)
        return _f(m.group(1).replace(",", "")) if m else default

    return {
        "reward": grab(r"est reward today \$([\-\d.,]+)",
                       _f(status.get("reward_est_today"))),
        "reward_lifetime": _f(status.get("reward_est_lifetime")),
        "contract_min": grab(r"\(([\d,]+) contract-min",
                             _f(status.get("contract_minutes_today"))),
        "efficiency": grab(r"([\d.]+)c/1k-contract-min",
                          _f(status.get("cents_per_1k_contract_min"))),
        "fills": grab(r"fills (\d+)", _f(status.get("fills_today"))),
        "errors": grab(r"errs (\d+)", _f(status.get("errors_today"))),
        "summary_date": status.get("summary_date", ""),
    }


def health_line(status: dict, ss: dict, dash_note: str = "") -> str:
    now = datetime.now(timezone.utc)
    problems = [dash_note] if dash_note else []
    try:
        age = (now - datetime.strptime(status.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc)).total_seconds() / 60.0
    except ValueError:
        age = float("inf")
    alive = age <= STALE_AFTER_MINUTES
    if not alive:
        problems.append(f"HEARTBEAT STALE ({status.get('updated_at', '?')})")
    if _f(status.get("halted_until")) > time.time():
        problems.append("DAILY-LOSS HALT active")
    standoff = status.get("manual_standoff") or []
    # A credit ledger that stops being refreshed reports a shrinking fraction
    # of reality while still LOOKING authoritative — the one failure mode of
    # moving lifetime reward onto the statement. Say so out loud.
    rows, _cal = load_credit_ledger()
    if rows:
        latest = max(d for d, _e, _a in rows)
        try:
            lag = (now.astimezone(ET).date()
                   - datetime.strptime(latest, "%Y-%m-%d").date()).days
        except ValueError:
            lag = 0
        if lag > LEDGER_STALE_DAYS:
            problems.append(
                f"REWARD LEDGER STALE ({lag}d; last credit {latest}) — paste a "
                f"fresh statement through imm_reward_recon.py --statement")
    else:
        problems.append("NO REWARD LEDGER — lifetime reward is the bot estimate")
    line = f"Bot: {'alive' if alive else 'DOWN'}, mode {status.get('mode', '?')}, " \
           f"{int(ss['errors'])} errors in last summary"
    if standoff:
        line += f" | {len(standoff)} market(s) yielded to manual/other bots"
    if problems:
        line += " | " + "; ".join(problems)
    return line


import imm_risk_controls as risk                                    # noqa: E402

# The selection gate's "universe:" line; imm_risk_controls owns the shape now.
_UNIVERSE_RE = risk.UNIVERSE_RE


def risk_context(client, state, status, resting):
    """What imm_risk_controls needs from the live bot: its limits (the
    mirrored launcher env, see _apply_launcher_env), where it stands now, and
    its positions against their OWN caps.

    Caps go through imm.ensure_family_override first. The bot clones a
    family's override onto each member at refresh, so a fresh import sees
    the global 150 for a sports ladder whose cap is 750: the old capacity
    table flagged a 400-lot NFL ladder "267% AT CAP" on 10/3 for exactly
    that reason."""
    pos = {t: _f(p) for t, p in (state.get("own_pos") or {}).items() if abs(_f(p)) > 0.5}
    positions, by_ev = [], {}
    for t, p in pos.items():
        series = t.split("-")[0]
        try:
            imm.ensure_family_override(series)
        except Exception:                                   # noqa: BLE001
            pass
        positions.append((t, p, imm.series_max_position(series)))
        ev = _event_of(t)
        by_ev[ev] = by_ev.get(ev, 0.0) + p
    events = [(ev, n, imm.event_cap_contracts(ev)) for ev, n in by_ev.items()]
    try:
        value = imm.account_value_dollars(client.get_balance())
    except Exception as e:                                  # noqa: BLE001
        log(f"risk controls: balance read failed ({e!r}); account value n/a")
        value = None
    st = status or {}
    caps = {k: getattr(imm, k, None) for k in (
        "ACCOUNT_DROP_HALT", "DAILY_LOSS_LIMIT", "SCAN_DAILY_LOSS_LIMIT", "SCAN_TOP_N",
        "FINECON_TOP_N",
        "FAILSAFE_CANCEL_AFTER", "MAX_MARKETS", "COLLATERAL_BUDGET", "MAX_CANDIDATE_BOOKS",
        "MAX_TOTAL_RESTING_ORDERS", "MAX_PLACEMENTS_PER_CYCLE", "PLACE_RATE_PER_SEC",
        "TOXIC_HALT", "TOXIC_PICKOFFS", "BREAKERS_ENABLED", "EVENT_FILL_HALT_CONTRACTS",
        "SCAN_FILL_HALT_CONTRACTS", "SCAN_MID_JUMP_CENTS", "SCAN_DRIFT_CENTS")}
    caps["TOXIC_HALT_MIN"] = (getattr(imm, "TOXIC_HALT_SECS", 1800.0) or 0) / 60.0
    return {"caps": caps, "now": {
        "pnl_today": _f(st["pnl_today"]) if "pnl_today" in st else None,
        "scan_pnl": _f(st["scan_pnl_today"]) if "scan_pnl_today" in st else None,
        "scan_halted": bool(st.get("scan_halted_today")),
        "errors_today": int(_f(st.get("errors_today"))),
        "halt_file": os.path.exists(imm.HALT_FILE),
        "manual_standoff": list(st.get("manual_standoff") or []),
        "acct_value": value,
        "acct_anchor": _f(state.get("account_value_day_start")) or None,
        "resting_orders": resting.get("orders") if resting else None,
        "resting_notional": resting.get("collateral") if resting else None,
        "positions": positions, "events": events}}


def risk_section(client, state, status, resting, now_utc):
    """(evidence, rows) for the risk-controls block: the last 24h of the
    bot's own logs and sinks (imm_risk_controls.gather) judged against the
    live limits."""
    ev = risk.gather(STATUS_DIR, now_utc.timestamp())
    return ev, risk.build_rows(ev, risk_context(client, state, status, resting), ET)


# Caps whose live value we can check against the launcher string. Verifying the
# OUTCOME rather than the attempt matters: if anything imports incentive_mm
# before _apply_launcher_env runs (its config is read at import), the env is
# set but the constants are already frozen at defaults — and a note that just
# counted parsed vars would cheerfully report "mirrored" over a $1,000 budget.
_CAP_CHECKS = (("IMM_COLLATERAL_BUDGET", lambda: imm.COLLATERAL_BUDGET),
               ("IMM_MAX_MARKETS", lambda: imm.MAX_MARKETS),
               ("IMM_MAX_TOTAL_RESTING", lambda: imm.MAX_TOTAL_RESTING_ORDERS),
               ("IMM_MAX_POSITION", lambda: imm.MAX_POSITION_CONTRACTS),
               ("IMM_MAX_EVENT", lambda: imm.MAX_EVENT_CONTRACTS),
               ("IMM_MAX_PLACEMENTS_PER_CYCLE", lambda: imm.MAX_PLACEMENTS_PER_CYCLE))


def capacity_config_mismatches():
    """[(var, launcher_value, in_effect)] where the live constant does NOT
    match what the launcher sets. Empty = the section's ceilings are real."""
    out = []
    for var, getter in _CAP_CHECKS:
        want = LAUNCHER_ENV.get(var)
        if want is None:
            continue
        try:
            if abs(float(want) - float(getter())) > 1e-6:
                out.append((var, want, getter()))
        except (TypeError, ValueError):
            continue
    return out


def capacity_note():
    """Loud when the ceilings are not the bot's — see _apply_launcher_env."""
    if not LAUNCHER_ENV:
        return ("!! caps are incentive_mm DEFAULTS — the launcher $ProbeEnv "
                "could not be parsed, so these ceilings are NOT what the bot "
                "is running")
    bad = capacity_config_mismatches()
    if bad:
        return ("!! caps do NOT match the launcher ({}) — incentive_mm was "
                "imported before the env was applied, so these ceilings are "
                "NOT what the bot is running".format(
                    ", ".join(f"{v}: launcher {w}, in effect {g:g}"
                              for v, w, g in bad)))
    return ("caps mirrored from the live launcher ({} vars, {} verified)"
            .format(len(LAUNCHER_ENV), len(_CAP_CHECKS)))


def calibration_status():
    """(validated, unvalidated) family lists from reward_calibration.json.

    A family is VALIDATED only where settled, paid-out programs exist to
    compare against. That is not a detail: hourly temp settles inside the hour
    and is measurable the next day, while a 5-day earnings-mention program
    contributes to the reward column for days before it can be checked at all.
    Reporting one blended realization factor let a temp-only measurement read
    as a whole-book accuracy claim (Jack caught this 2026-08-04: the 8/1 and
    8/2 rows are 67% and 27% families with NO settled credit evidence)."""
    cal = load_json(CALIB_PATH) or {}
    pa = cal.get("post_amendment") or {}
    by_fam = pa.get("by_family") or {}
    validated, unvalidated = [], []
    for name, v in sorted(by_fam.items()):
        if v.get("realization_factor") and v.get("credited", 0) >= 1.0:
            validated.append((name, v["realization_factor"], v["events"],
                              v["credited"]))
        else:
            unvalidated.append(name)
    return validated, unvalidated, pa


def _calibration_caveat_text():
    validated, _unval, pa = calibration_status()
    if not validated:
        return ["  (REWARD is the bot estimate — no credit ledger yet; run "
                "imm_reward_recon.py --statement)"]
    out = ["  REWARD accuracy — measured per family against real credits, not "
           "assumed. Only families whose programs have SETTLED and PAID can be",
           "  checked at all, so this is a statement about part of the column, "
           "not all of it:"]
    for name, fac, n, cred in validated:
        out.append("    {:<28} {:.3f}x  ({} settled events, ${:,.2f} credited)"
                   .format(name, fac, n, cred))
    out.append("    every other family              UNVALIDATED — no settled "
               "post-{} credit yet".format(pa.get("cutover", "?")[:10]))
    return out


def _calibration_caveat_html():
    validated, _unval, pa = calibration_status()
    if not validated:
        return ('<div style="color:#b8860b;font-size:12px;margin-top:4px">'
                'REWARD is the bot estimate — no credit ledger yet; run '
                '<code>imm_reward_recon.py --statement</code>.</div>')
    rows = "".join(
        "<li><b>{}</b> — {:.3f}x ({} settled events, ${:,.2f} credited)</li>"
        .format(n, f, e, c) for n, f, e, c in validated)
    return ('<div style="color:#666;font-size:12px;margin-top:6px;'
            'border-left:3px solid #d9a441;padding-left:8px">'
            '<b>How much of this REWARD column is actually verified?</b> Only '
            'families whose programs have settled AND paid can be compared to '
            'credits at all — hourly temp settles inside the hour, a 5-day '
            'earnings-mention program does not. Verified against real credits '
            'since the {} estimator rewrite:<ul style="margin:4px 0">{}</ul>'
            'Every other family — earnings-mention, rain, company/econ — is '
            '<b>unvalidated</b>: it contributes to the numbers above with no '
            'settled credit to check it against. Where pre-rewrite evidence '
            'exists it ran 0.33–0.64x, i.e. those contributions may be '
            'materially overstated.</div>'.format(pa.get("cutover", "?")[:10], rows))


def _pnl_span(v: float, decimals: int = 2) -> str:
    color = "#0a7a2f" if v > 0.005 else ("#c0392b" if v < -0.005 else "#777")
    return f'<span style="color:{color}">{v:+,.{decimals}f}</span>'


TD = 'padding:5px 12px;border:1px solid #ddd;text-align:right;'
TDL = 'padding:5px 12px;border:1px solid #ddd;text-align:left;'


def rain_dir_section(client):
    """(text_lines, html) for the rain-directional ledger — settled P&L,
    open MTM, hit rate (Jack 2026-07-28: 'make sure we can see how it
    performs'). None when no ledger exists yet."""
    import csv as _csv
    ledger = os.path.join(os.path.dirname(STATUS_PATH), "rain_directional_ledger.csv")
    if not os.path.exists(ledger):
        return None
    try:
        with open(ledger, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
    except OSError:
        return None
    if not rows:
        return None
    tickers = sorted({r["ticker"] for r in rows})
    markets = {}
    try:
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            resp = client.get_markets(tickers=",".join(chunk), limit=len(chunk))
            for m in resp.get("markets") or []:
                markets[m.get("ticker", "")] = m
    except Exception:
        pass
    settled = wins = 0
    pnl_settled = mtm_open = 0.0
    open_n = 0
    for r in rows:
        n = _f(r["contracts"]); px = _f(r["price_cents"]); side = r["take_side"]
        m = markets.get(r["ticker"], {})
        result = m.get("result") or ""
        if result in ("yes", "no"):
            settled += 1
            win = result == side
            wins += 1 if win else 0
            pnl_settled += ((100 - px) if win else -px) / 100.0 * n
        else:
            open_n += 1
            try:
                # the bot's mark, as current_mids: a one-sided or 50c+ wide
                # book is the last trade clamped to its touch, never
                # (bid + the $1.00 empty ask) / 2
                mid = bulk_mark_cents(m)
                if mid is not None:
                    mark = mid if side == "yes" else 100 - mid
                    mtm_open += (mark - px) / 100.0 * n
            except Exception:
                pass
    hit = f"{100 * wins / settled:.0f}%" if settled else "—"
    text = [f"RAIN DIRECTIONAL: {len(rows)} bets ({settled} settled, hit {hit}) | "
            f"settled P&L {pnl_settled:+.2f} | open {open_n} MTM {mtm_open:+.2f} | "
            f"total {pnl_settled + mtm_open:+.2f}"]
    html = (f'<div style="font-size:15px;font-weight:600;margin:10px 0 2px">'
            f'Rain directional (NWS-vs-book)</div>'
            f'<div style="color:#555;margin-bottom:10px">'
            f'{len(rows)} bets &nbsp;·&nbsp; {settled} settled, hit <b>{hit}</b>'
            f' &nbsp;·&nbsp; settled {_pnl_span(pnl_settled)}'
            f' &nbsp;·&nbsp; {open_n} open, MTM {_pnl_span(mtm_open)}'
            f' &nbsp;·&nbsp; total {_pnl_span(pnl_settled + mtm_open)}</div>')
    return text, html


# ---------------------------------------------------------------------------
# CUTOFF AUDIT — our earnings call-time override vs Kalshi's own ticker date.
#
# Why this section exists (Jack 2026-08-05): KXEARNINGSMENTIONLLY-26AUG07 stopped
# being quoted at 10:50Z. The override said the Eli Lilly call was 2026-08-05
# 07:00 ET while the Kalshi TICKER (-26AUG07), the sub_title ("On Aug 7, 2026")
# and the incentive program's end_date all said Aug 7. The first read was "our
# override is wrong". It was not: Lilly reported the morning of Aug 5, Kalshi
# closed and finalized the event at 2026-08-05T16:08Z, and standing down at
# 10:50Z is exactly what kept the bot from quoting through a live print. Kalshi's
# three "independent" signals are really one — ticker date, sub_title and program
# end all derive from the same internal date (program end == ticker date 10 of 10
# on live earnings events), so they corroborate each other for free and in this
# case were wrong together.
#
# The two directions are NOT symmetric, which is the whole point of the section:
#   * override EARLIER than the ticker date -> the bot stands down early. Costs
#     reward accrual, carries NO adverse-selection risk, and on every conflict
#     that has actually resolved the override was the right date and the ticker
#     the wrong one (BA closed 7/28 vs ticker 7/21, HOOD 7/29 vs 7/17, PGR 8/4
#     vs 7/15, LLY 8/5 vs 8/7 — override 4, Kalshi ticker 0). Show it, quietly.
#   * override LATER than the ticker date -> the bot keeps quoting PAST Kalshi's
#     date and can make markets straight through a real earnings call. Real
#     money, real adverse selection. This direction shouts.
#
# Tolerance is EXACTLY ZERO, deliberately. The tempting carve-out is "a call
# after the close on day N with a ticker dated N+1 is just Kalshi's encoding" —
# but that shape occurs 1 time in 34 after-close overrides (33 of 34 AMC entries
# sit on delta 0; ABNB and LYFT are both Aug-6 after-close carrying 26AUG06
# tickers). Kalshi's convention IS ticker date = release date, so the lone -1
# (DKNG-26AUG07, measured 2026-08-05) is a genuine date error and a tolerance
# window would exist only to hide the very case Jack asked to see.
#
# SCOPE is every override with a parseable ticker date, NOT just earnings.
# The first cut gated on KXEARNINGSMENTION and that was wrong: incentive_mm.py
# (:1195) puts company-disclosure RELEASE times — KXINTC, KXFSLR, KXCOINBASE —
# under the same tight OVERRIDE_BUFFER_MIN as earnings calls, i.e. the bot
# already treats them as adverse-selection-critical, and a LATE override on a
# political-mention series is the same class of bug. Measured 2026-08-05,
# widening the gate adds 10 checked keys and ZERO extra rows, so the narrow
# gate bought no quiet and cost coverage. The ONE exclusion that survives is
# SCHEDULE_RESOLVED_SERIES (WNBA/MLB/WC), where a LATE override means a
# POSTPONED GAME — legitimate, different remedy — and the excluded count is
# printed so the exclusion is never silent.
#
# Kept here rather than in a shared module: only the digest reports it, and
# imm_earnings_overrides.py already owns a different question (does an override
# EXIST for every paying event) with its own stale-ticker autofix.
# ---------------------------------------------------------------------------


def _override_hour_label(ov_et: datetime, is_earnings: bool) -> str:
    """What the override's TIME actually means. nasdaq_release_datetime()
    (imm_earnings_overrides.py) SYNTHESIZES 16:00 from an after-close flag and
    07:00 from a before-open flag — those are proxies for a date, not call
    times. Anything else was scraped off the IR page and is a real time. The
    email must not claim "the call is at 4:00pm ET" on a synthesized hour.

    The proxy reading is only valid for earnings series, which is the only
    place nasdaq_release_datetime() writes; a 16:00 on KXTRUMPMENTION is just
    a start time somebody set."""
    if not is_earnings:
        return "hand-set start time"
    if (ov_et.hour, ov_et.minute) == (16, 0):
        return "after close (Nasdaq proxy 4pm ET)"
    if (ov_et.hour, ov_et.minute) == (7, 0):
        return "before open (Nasdaq proxy 7am ET)"
    # a stated release time from the company's announcement (2026-09-27), a
    # scraped IR time, or a hand --set: a real clock time either way
    return "announced / IR / hand-set time"


# ---------------------------------------------------------------------------
# UNVERIFIED CALL TIMES — the hole this section plugs (Jack 2026-08-06).
#
# The audit above compares DATES. KXEARNINGSMENTIONCELH-26AUG06 had the right
# date and the wrong HOUR: Nasdaq's calendar carried no time flag, the resolver
# fell through to 16:00 ET, Celsius actually reported before the open with an
# 8:00am call, and the bot quoted the event all morning with the results already
# public. delta was 0, so the audit skipped it at the `if delta == 0: continue`
# and tomorrow's 7:10am email would have said nothing.
#
# WHY NOT JUST FLAG EVERY 16:00: 29 of the 49 live earnings overrides sit at
# 16:00 and 28 of them are a real Nasdaq after-close flag. A "16:00 is
# suspicious" rule ships 29 rows every morning to catch one — a ratio that
# guarantees the section gets skimmed, and a skimmed section is worth less than
# no section because it also buries the date rows next to it. The distinguishing
# fact is not the hour, it is whether anyone MEASURED the hour, and that fact
# was thrown away at write time. imm_earnings_overrides.record_meta() now keeps
# it in a sidecar; measured over the 43 Nasdaq resolutions in the task log
# (2026-07-23..08-06) the "guess" class is 1. This section is empty on ~97% of
# mornings BY CONSTRUCTION, which is the only reason it will still be read on
# the morning it is not.
#
# Sidecar, not a shape change to event_start_overrides.json: incentive_mm's
# load_file_event_overrides() (:1481) runs parse_iso_utc(str(iso).strip()) on
# every value and DROPS entries it cannot parse, so a dict-valued override would
# delete the live bot's cutoff outright. See the module comment in
# imm_earnings_overrides.py. Absent sidecar => the check reports itself as not
# yet effective rather than reporting "clean".
OVERRIDE_META_PATH = os.path.join(STATUS_DIR, "event_start_overrides_meta.json")


def _days_out_phrase(n: int) -> str:
    """Imminence, in words. A +6d LATE row whose ticker date is three weeks
    out needs nothing this morning; one whose ticker date is TODAY needs
    action within the hour. An ISO date renders those identically at 7:10am,
    so the reader has to do the subtraction — this does it for them."""
    if n == 0:
        return "TODAY"
    if n == 1:
        return "TOMORROW"
    if n > 1:
        return "in {}d".format(n)
    if n == -1:
        return "yesterday"
    return "{}d ago".format(-n)


def cutoff_audit(client, now_utc: datetime, own_pos: dict = None,
                 kalshi: dict = None, pick_events=()) -> dict:
    """Start overrides whose ET DATE disagrees with their Kalshi ticker date.

    Returns {"rows": [...], "checked", "total", "excluded", "no_day",
    "dead": [event...], "error"}. NEVER raises — build_digest has no
    per-section guard and main() would retry a raising build 8 times at 5-minute
    intervals and then send nothing all day, so every failure comes back as
    {"error": ...} for the renderers to print.

    own_pos is build_digest's own_book() position map (market ticker -> signed
    contracts). It costs no extra API call and it is the only number in the
    section that is actual EXPOSURE; everything else is reward pool, i.e. what
    standing down would forfeit.

    Source of truth is imm.EVENT_START_OVERRIDES after load_file_event_overrides()
    (in-process merge only, no writes), NOT the JSON file: five overrides are
    hard-coded as the IMM_EVENT_START_OVERRIDE default in incentive_mm.py and
    never appear in the file — one of them, KXEARNINGSMENTIONNFLX-26JUL02, is
    itself +14d LATE.

    Gates, in order:
      1. never schedule-resolved series (WNBA/MLB/WC): a LATE override there is
         a POSTPONED GAME, which is legitimate and has a different remedy. This
         is the ONLY series exclusion — see the module comment on why the old
         earnings-only gate was removed — and it is counted and printed.
      2. parse_event_date() -> None means the ticker carries no calendar day at
         all (KXTLN-26AUGGEN, KXCOINBASE-26JULVOL). That is "not comparable",
         never "mismatch"; counted and reported, not flagged.
      3. compare in ET on BOTH sides. parse_event_date returns midnight ET as
         UTC and overrides are stored at -04:00, so a UTC-date comparison
         invents a spurious +1d on any evening entry.
      4. delta == 0 -> agree. See the module comment on why the tolerance is
         exactly zero.

    Then the money gate: an event with no LIVE incentive program is dead history.
    The overrides file is never garbage-collected, so without this the section
    would ship BA-26JUL21 and PGR-26JUL15 forever and grow by one row with every
    stale-ticker autofix. The same sweep supplies the pool $/day, which sizes a
    row but is NOT its risk.

    kalshi / pick_events come from imm_pickoff.scan() (Jack 2026-09-27: say
    what time the event is AND what time Kalshi thinks it is). Every row gets
    "kalshi_start" -- Kalshi's milestone, i.e. when "at event start" orders are
    pulled -- and "pickoff" when that start is far enough past ours to leave
    those orders resting through the real event. Omitted, rows carry
    kalshi_start None and read exactly as before."""
    a = {"rows": [], "checked": 0, "total": 0, "excluded": 0, "no_day": 0,
         "dead": [], "error": None, "unverified": [], "no_prov": 0}
    try:
        imm.load_file_event_overrides()          # in-process merge; no writes
        meta = load_json(OVERRIDE_META_PATH)      # advisory; {} when absent

        # Live liquidity pool per event, same accounting as the bot's own
        # fetch_programs(): period_reward is CENTI-cents, spread over the
        # program's own length.
        pools, cursor = {}, None
        for _page in range(20):
            params = {"limit": 1000, "status": "active"}
            if cursor:
                params["cursor"] = cursor
            resp = client.get("/incentive_programs", params=params)
            batch = resp.get("incentive_programs") or []
            for p in batch:
                if p.get("incentive_type") != "liquidity" or p.get("paid_out"):
                    continue
                start = imm.parse_iso_utc(p.get("start_date", ""))
                end = imm.parse_iso_utc(p.get("end_date", ""))
                if not start or not end or not (start <= now_utc < end):
                    continue
                days = max((end - start).total_seconds() / 86400.0, 1.0 / 24)
                t = p.get("market_ticker") or ""
                cur = pools.setdefault(_event_of(t), {"mkts": set(), "dpd": 0.0})
                cur["mkts"].add(t)
                cur["dpd"] += (p.get("period_reward") or 0) / 10000.0 / days
            cursor = resp.get("next_cursor")
            if not cursor or not batch:
                break

        # Contracts on the book per event — the actual exposure. Costs nothing:
        # build_digest already called own_book(state) before us.
        book = {}
        for t, v in (own_pos or {}).items():
            try:
                book[_event_of(t)] = book.get(_event_of(t), 0.0) + abs(_f(v))
            except Exception:
                continue

        today_et = now_utc.astimezone(ET).date()
        for ev, ov in sorted(imm.EVENT_START_OVERRIDES.items()):
            a["total"] += 1
            series = ev.split("-")[0]
            if series in imm.SCHEDULE_RESOLVED_SERIES:
                a["excluded"] += 1           # postponed game, not a date bug
                continue
            td = imm.parse_event_date(ev)
            if td is None or ov is None:
                a["no_day"] += 1
                continue
            a["checked"] += 1
            t_et = td.astimezone(ET).date()
            ov_et = ov.astimezone(ET)
            delta = (ov_et.date() - t_et).days
            cutoff_et = ov_et - timedelta(minutes=imm.OVERRIDE_BUFFER_MIN)

            # --- unverified-HOUR check, deliberately BEFORE the delta gate ---
            # CELH's delta was 0. Anything that runs after `if delta == 0:
            # continue` cannot see this class at all.
            rec = meta.get(ev) or {}
            rec_dt = imm.parse_iso_utc(str(rec.get("iso") or ""))
            if rec_dt is None or rec_dt != ov:
                # No record, or the record describes a value that has since been
                # superseded (a re-resolve, or Jack's --set). A superseded guess
                # is a FIXED guess and must stop being flagged, or the section
                # becomes a permanent red row nobody reads.
                a["no_prov"] += 1
            elif str(rec.get("confidence")) == "guess":
                pool_u = pools.get(ev)
                # Money gate + "is there still time to act": once the cutoff has
                # passed the bot is already standing down and the row is history.
                if pool_u and cutoff_et > now_utc:
                    a["unverified"].append({
                        "event": ev, "ticker_date": t_et, "override_et": ov_et,
                        "cutoff_et": cutoff_et, "days_out": (t_et - today_et).days,
                        "label": str(rec.get("label") or "unknown"),
                        "contracts": book.get(ev, 0.0),
                        "mkts": len(pool_u["mkts"]), "dpd": pool_u["dpd"],
                        # A guess that landed AFTER midday is a guess on the
                        # UNSAFE side: the bot keeps quoting through a morning
                        # call. A guess that landed in the morning already fails
                        # early and only forfeits accrual, so it is listed but
                        # never shouted.
                        "unsafe": ov_et.hour >= 12})

            if delta == 0:
                continue
            pool = pools.get(ev)
            if not pool:
                a["dead"].append(ev)         # programs over: history, not risk
                continue
            # LATE splits in two and the split is the severity, not the size:
            # BA/HOOD/PGR were LATE by 7-20 days and harmless because the ticker
            # date had ALREADY ELAPSED (the stale-ticker trap — quoting on is how
            # that pool gets earned). NBIS is LATE by 6 and dangerous because
            # both dates are still ahead, so nothing is stale and one of the two
            # sources is simply wrong about a call that has not happened yet.
            #
            # NOTE the copy for "warn" must stay conditional. t_et < today_et is
            # a PROXY for "this was a stale-ticker autofix", not a measurement of
            # it: a forward disagreement becomes an elapsed one purely by the
            # passage of a day, so NBIS is risk on Aug 6 and warn on Aug 7 with
            # no new information. Today that is masked because program end ==
            # ticker date on live earnings events (so the row goes dead first),
            # but that is a Kalshi convention, not a guarantee — Kalshi issues
            # 16-24 day mention programs. Do not let this branch assert safety.
            if delta > 0:
                sev = "risk" if t_et >= today_et else "warn"
            else:
                sev = "info"
            a["rows"].append({
                "event": ev, "ticker_date": t_et, "override_et": ov_et,
                "delta": delta, "severity": sev,
                "days_out": (t_et - today_et).days,
                "contracts": book.get(ev, 0.0),
                "mkts": len(pool["mkts"]), "dpd": pool["dpd"],
                "cutoff_et": cutoff_et,
                "hour_label": _override_hour_label(
                    ov_et, series.startswith(imm._EARNINGS_PREFIX))})
        # Kalshi's own start per row. The scan's sweep only carries starts
        # still ahead, so a row whose Kalshi start is behind us (yesterday's
        # ticker date) costs one lookup; rows are a handful.
        for r in a["rows"]:
            ks = (kalshi or {}).get(r["event"])
            if ks is None and kalshi is not None:
                ks = imm_pickoff.kalshi_start_for(client, r["event"])
            r["kalshi_start"] = ks
            r["kalshi_days_out"] = ((ks.astimezone(ET).date() - today_et).days
                                    if ks is not None else None)
            r["pickoff"] = r["event"] in set(pick_events or ())
        # risk rows are ranked by IMMINENCE, not by pool size: the thing that
        # decides whether this needs action before the open is how soon Kalshi
        # thinks the call is, and dpd is the reward forfeited by standing down,
        # i.e. the argument for doing nothing.
        order = {"risk": 0, "warn": 1, "info": 2}
        a["rows"].sort(key=lambda r: (
            order[r["severity"]],
            r["days_out"] if r["severity"] == "risk" else 0,
            -r["dpd"]))
        # Same principle for the unverified list: soonest cutoff first. Pool is
        # the tie-break only — it sizes the row, it is not the reason to act.
        a["unverified"].sort(key=lambda r: (not r["unsafe"], r["cutoff_et"],
                                            -r["dpd"]))
    except Exception as e:
        a["error"] = repr(e)
    return a


def _cutoff_meaning(r: dict):
    """(what it means, what to DO) for one row.

    Every branch carries an imperative. A flag that raises a question it cannot
    help answer gets read once and then skimmed, and the EARLY branch in
    particular has to lead with the PROHIBITION: a tired reader who sees "costs
    accrual" next to a $310/day pool will reach for the obvious remedy — push
    the override out to match Kalshi — which is exactly the edit that would have
    had the bot quoting through Lilly's print on Aug 5.

    Every branch then says what time Kalshi's own event start is and which
    way that cuts for "at event start" orders (imm_pickoff.kalshi_note)."""
    why, action = _cutoff_meaning_base(r)
    note = imm_pickoff.kalshi_note(r["override_et"], r.get("kalshi_start"),
                                   r.get("pickoff", False))
    return (why + ". " + note if note else why), action


def _cutoff_meaning_base(r: dict):
    when = r["cutoff_et"].strftime("%b %d %H:%M")
    if r["severity"] == "risk":
        return ("Kalshi says the call is {} ({}); the bot keeps quoting until {} "
                "ET, straight through the print if Kalshi is right".format(
                    _days_out_phrase(r["days_out"]), r["ticker_date"], when),
                "ACTION: confirm the date on the company IR page or a press "
                "release before the open. If Kalshi is right, stand the event "
                "down NOW (add it to IMM_BLOCKLIST in run_incentive_mm.ps1) — "
                "do not wait for tomorrow's digest.")
    if r["severity"] == "warn":
        return ("Kalshi's ticker date passed {}; if that was a stale-ticker "
                "autofix then quoting on is how this pool is earned".format(
                    _days_out_phrase(r["days_out"])),
                "ACTION: none IF the call has already happened — confirm that "
                "before treating it as safe; an elapsed ticker date is a proxy "
                "for 'stale', not proof of it. If the call is still ahead, this "
                "is the risk case.")
    return ("DO NOT push this out to match Kalshi without a primary source — "
            "that is how the bot ends up quoting through a live print (LLY, "
            "Aug 5). Standing down {}d early (cutoff {} ET) only forfeits "
            "reward accrual; it risks nothing".format(-r["delta"], when),
            "ACTION: none.")


def _unverified_meaning(r: dict) -> tuple:
    """(what it means, what to DO) for one unverified-hour row.

    The action must name a PRIMARY source, not "check Nasdaq": Nasdaq is what
    already failed here, and re-reading a blank field returns the same blank.

    The SAFE branch leads with the PROHIBITION for the same reason the EARLY
    branch of _cutoff_meaning does (:1308), and it matters MORE here: once the
    resolver's fallback assumes before-open, every future guess lands on the safe
    side, so this branch is the section's entire steady-state output. A row that
    ends "confirm only if you want the $X/day back" is an invitation to push a
    07:00 guess out to 16:00 — which is CELH, re-created by hand, by a tired
    reader at 7:10am. The dollar figure is named only as what standing down
    COSTS, never as a reason to move the cutoff."""
    when = r["cutoff_et"].strftime("%b %d %H:%M")
    if r["unsafe"]:
        return ("nobody measured this hour — the resolver had no time from "
                "Nasdaq and SYNTHESIZED {} ET, on the unsafe side. If the "
                "company actually reports before the open, the bot quotes "
                "through the call and the cutoff at {} ET never bites in time"
                .format(r["override_et"].strftime("%H:%M"), when),
                "ACTION: read the company IR page or the release wire and "
                "confirm the call time, then `python imm_earnings_overrides.py "
                "--set {} \"YYYY-MM-DDTHH:MM:00-04:00\"`. If you cannot confirm "
                "it before the open, --set it to 07:00 ET — standing down early "
                "forfeits ${:,.0f}/day and risks nothing.".format(
                    r["event"], r["dpd"]))
    return ("hour was synthesized, not measured, but it landed on the SAFE "
            "(morning) side — the bot stands down at {} ET whether or not the "
            "guess is right. DO NOT push this override later to recover the "
            "${:,.0f}/day: moving a GUESSED cutoff into the afternoon on "
            "anything short of a primary source is exactly the edit that had "
            "the bot quoting through Celsius's 8am call (CELH, Aug 6)".format(
                when, r["dpd"]),
            "ACTION: none. The only thing that justifies a later cutoff here is "
            "the company's own IR page or release wire stating an after-close "
            "time — NOT Nasdaq, which is what returned nothing for this ticker "
            "in the first place. Left alone this forfeits ${:,.0f}/day and "
            "risks nothing.".format(r["dpd"]))


def _kalshi_vs_ours(r: dict) -> str:
    """Both times for a banner row: what Kalshi thinks (its milestone start,
    i.e. when "at event start" orders go) and what we have. Falls back to the
    ticker date when Kalshi publishes no start for the event."""
    ev = _short_event(r["event"])
    if r.get("kalshi_start") is None:
        return "Kalshi says the {} call is {} ({})".format(
            ev, _days_out_phrase(r["days_out"]), r["ticker_date"])
    return ("Kalshi thinks the {} call starts {}, {}; our time is {}, {}"
            .format(ev, _days_out_phrase(r["kalshi_days_out"]),
                    imm_pickoff.fmt_et(r["kalshi_start"]),
                    imm_pickoff.fmt_et(r["override_et"]), r["hour_label"]))


def cutoff_banner(a: dict) -> str:
    """One-line shout for the top of the digest, or "" when nothing is at risk.

    RISK ONLY — deliberately. Two classes are excluded and each exclusion is
    load-bearing:
      * EARLY: standing down before Kalshi's date is the conservative direction.
        Detail block only.
      * WARN (LATE onto an ALREADY-ELAPSED ticker date): this is the exact shape
        imm_earnings_overrides.discover_stale_ticker_events() (:255, added
        2026-08-03 after PGR) is DESIGNED to create — it writes LATE overrides on
        live paying events whose ticker date has passed. BA, HOOD and PGR all
        passed through it while their programs were live, so a warn-triggered
        banner would have been red on roughly one morning in four of earnings
        season for the bot doing exactly the right thing, and the old copy then
        ended the same red sentence with "no print risk". A red box that
        disavows itself trains the reader to skip red boxes, which is precisely
        what would kill the NBIS-class banner. Warn renders in #d9821b in the
        detail table, where it reads as information rather than alarm.

    Silent on failure rather than degraded-with-a-message: this is the one piece
    called straight from build_digest, and an empty banner just falls back to the
    normal headline while the detail block below still reports the problem."""
    try:
        risk = [r for r in a.get("rows") or [] if r["severity"] == "risk"]
        # An unverified hour on the UNSAFE side is the CELH shape and belongs in
        # the banner for the same reason a LATE date does: the bot is quoting on
        # a time nobody checked. The SAFE-side guesses stay out — they fail early
        # by construction, and once the resolver's fallback is fixed to assume
        # before-open, every guess lands there and this banner goes quiet on its
        # own instead of having to be muted by hand.
        unsafe = [r for r in a.get("unverified") or [] if r["unsafe"]]
        if not risk and not unsafe:
            return ""
        parts = []
        if risk:
            # LIVE EXPOSURE is contracts on the book. The pool $/day is named as
            # what standing down COSTS, never as the exposure — it is the
            # argument for the wrong action and must not be the number the
            # reader triages on.
            parts.append(
                "{} override{} run PAST Kalshi's ticker date — the bot quotes "
                "beyond the date Kalshi thinks the call is on. ".format(
                    len(risk), "" if len(risk) == 1 else "s")
                + " ".join(
                    "LIVE EXPOSURE: {kalshi} — the bot has {cts:,.0f} contracts "
                    "on the book and keeps quoting until {cut} ET. Standing "
                    "down forfeits ${dpd:,.0f}/day of reward pool.".format(
                        kalshi=_kalshi_vs_ours(r), cts=r["contracts"],
                        cut=r["cutoff_et"].strftime("%b %d %H:%M"),
                        dpd=r["dpd"]) for r in risk))
        parts.extend(
            "UNVERIFIED CALL TIME: nobody measured when {ev} reports — the "
            "resolver had no time from Nasdaq and assumed {hh} ET ({lab}). The "
            "bot has {cts:,.0f} contracts on the book and keeps quoting until "
            "{cut} ET; if the call is before the open it quotes straight through "
            "it (CELH, Aug 6). Confirm on the IR page, or --set 07:00 and "
            "forfeit ${dpd:,.0f}/day.".format(
                ev=_short_event(r["event"]),
                hh=r["override_et"].strftime("%H:%M"), lab=r["label"],
                cts=r["contracts"],
                cut=r["cutoff_et"].strftime("%b %d %H:%M"), dpd=r["dpd"])
            for r in unsafe)
        return " ".join(parts)
    except Exception:
        return ""


_CUTOFF_EXPLAINER = (
    "Our override is the CALL / release time (Nasdaq calendar or IR page), held "
    "in run-logs/incentive-mm/event_start_overrides.json and written by "
    "imm_earnings_overrides.py; Kalshi's ticker date, sub_title and program end "
    "are all one internal date, so they never disagree with each other and can "
    "all be wrong together. LATE (override after the ticker date) = the bot "
    "quotes past Kalshi's date and can make markets through a live print — money "
    "at risk, verify against a primary source and blocklist the event if Kalshi "
    "is right. EARLY = the bot stands down first, which forfeits reward accrual "
    "and risks nothing; do NOT edit an override to chase that accrual. On every "
    "conflict that has actually resolved the override was right and the ticker "
    "wrong: KXEARNINGSMENTIONLLY-26AUG07 stood down 2026-08-05 10:50Z against an "
    "Aug 7 ticker, Lilly reported that morning, and Kalshi closed and finalized "
    "the event six hours later. The DATE agreeing is not the all-clear: CELH "
    "matched on the date and was wrong by nine HOURS (Nasdaq had no time flag, "
    "the resolver assumed 4pm, the call was at 8am and the bot quoted through "
    "it), which is what the unverified-call-times block below covers.")


def _prov_coverage_line(a: dict) -> str:
    """How much of the population this check can actually see. "" only when
    there is nothing checked at all.

    ALWAYS printed, never gated on coverage being zero. The old version only
    admitted "NOT yet effective" while coverage was exactly zero; one new
    resolution flipped it to a sentence that listed "written before provenance
    recording" alongside "hand-set" and "env-pinned" as though all three were
    benign human-owned values and then closed on "the rest were measured, not
    guessed". That is reassurance at ~3% coverage, and the unrecorded set is not
    benign — it is precisely CELH's class: a pre-fix 16:00 written by the old
    else-branch is byte-identical to a measured after-close reading.

    It also states that coverage does NOT converge, because it does not:
    imm_earnings_overrides.py's `covered` short-circuit never rewrites an
    existing entry, record_meta only records what the resolver itself writes, and
    the overrides file is never pruned. The old copy promised "it fills in as
    imm_earnings_overrides.py rewrites each entry" — a promise the writer cannot
    keep. Coverage clears only as events churn out of the file, or by seeding the
    sidecar from the resolver's own task-log history."""
    checked = a.get("checked", 0) or 0
    no_prov = a.get("no_prov") or 0
    if not checked:
        return ""
    recorded = max(checked - no_prov, 0)
    if not no_prov:
        return ("(call-time provenance recorded for all {} checked overrides — "
                "every one was measured, not guessed)".format(checked))
    return ("(call-time provenance recorded for {}/{} checked overrides — those "
            "are the ONLY ones this check can see. The other {} were written "
            "before provenance recording, hand-set or env-pinned; a pre-fix "
            "16:00 entry is indistinguishable from a measured after-close "
            "reading and is NOT covered by this check. This does not fill in on "
            "its own — the resolver never rewrites an existing entry — so it "
            "clears only as events churn out of the file.)"
            .format(recorded, checked, no_prov))


def _unverified_lines(a: dict) -> list:
    """Plain-text UNVERIFIED CALL TIMES block.

    Never returns [] while anything is checked: an empty section that looks
    identical whether the check is clean or simply blind to 60 of 62 overrides
    is how a silent regression hides for a month. The coverage line carries that
    distinction and is emitted in BOTH branches."""
    rows = a.get("unverified") or []
    cov = textwrap.wrap(_prov_coverage_line(a), width=76,
                        initial_indent="  ", subsequent_indent="  ")
    if not rows:
        return cov
    out = ["UNVERIFIED CALL TIMES — {} live override(s) whose HOUR was "
           "synthesized, not measured".format(len(rows))]
    for r in rows:
        why, action = _unverified_meaning(r)
        out.append("  {:34s} ours {} ET  cutoff {} ET  ticker {} {}  "
                   "{} mkts  {:,.0f} cts  ${:,.2f}/day{}".format(
                       _short_event(r["event"])[:34],
                       r["override_et"].strftime("%m-%d %H:%M"),
                       r["cutoff_et"].strftime("%m-%d %H:%M"),
                       r["ticker_date"].isoformat()[5:],
                       _days_out_phrase(r["days_out"]), r["mkts"],
                       r["contracts"], r["dpd"],
                       "  <== UNSAFE SIDE" if r["unsafe"] else ""))
        for para in (why, action):
            out.extend(textwrap.wrap(para, width=76, initial_indent="      ",
                                     subsequent_indent="      "))
    out.extend(cov)
    return out


def _unverified_html(a: dict) -> str:
    """HTML twin of _unverified_lines. Never raises (caller is guarded)."""
    rows = a.get("unverified") or []
    if not rows:
        # the unwrapped sentence, not the 76-col text block re-joined
        txt = _prov_coverage_line(a)
        return ('<div style="color:#888;font-size:11px;margin-top:4px">{}</div>'
                .format(txt)) if txt else ""
    h = ['<div style="font-size:14px;font-weight:600;margin:12px 0 4px">'
         'Unverified call times &mdash; {} override(s) whose HOUR was '
         'synthesized, not measured</div>'.format(len(rows)),
         '<table style="border-collapse:collapse">',
         '<tr style="background:#f0f0f0;font-weight:600">'
         '<td style="{0}">EVENT</td><td style="{1}">OUR CALL TIME (ET)</td>'
         '<td style="{1}">CUTOFF (ET)</td><td style="{1}">KALSHI SAYS</td>'
         '<td style="{1}">MKTS</td><td style="{1}">CTS ON BOOK</td>'
         '<td style="{1}">POOL $/DAY</td></tr>'.format(TDL, TD)]
    for i, r in enumerate(rows):
        colour = "#c0392b" if r["unsafe"] else "#777"
        # The action line is bold ONLY when there is an action. On a safe-side
        # row the correct action is to do nothing, and rendering "ACTION: none"
        # as the loudest text in the row is how a no-op becomes a to-do.
        act_style = ("color:#333;font-weight:600" if r["unsafe"]
                     else "color:#777;font-weight:400")
        why, action = _unverified_meaning(r)
        h.append(
            '<tr style="background:{bg}"><td style="{tdl}">{ev}'
            '<div style="color:{col};font-size:11px">{why}</div>'
            '<div style="{acts};font-size:11px">{act}</div>'
            '</td>'
            '<td style="{td}">{ov}<div style="color:{col};font-size:11px">'
            'guessed &mdash; {lab}</div></td>'
            '<td style="{td}">{cut}</td>'
            '<td style="{td}">{tick}<div style="color:#999;font-size:11px">'
            '{when}</div></td>'
            '<td style="{td}">{mkts}</td><td style="{td}">{cts:,.0f}</td>'
            '<td style="{td}">{dpd:,.2f}</td></tr>'.format(
                bg="#fafafa" if i % 2 else "#fff", tdl=TDL, td=TD, col=colour,
                acts=act_style,
                ev=_short_event(r["event"]), why=why, act=action,
                ov=r["override_et"].strftime("%b %d %H:%M"), lab=r["label"],
                cut=r["cutoff_et"].strftime("%b %d %H:%M"),
                tick=r["ticker_date"].isoformat(),
                when=_days_out_phrase(r["days_out"]), mkts=r["mkts"],
                cts=r["contracts"], dpd=r["dpd"]))
    h.append("</table>")
    cov = _prov_coverage_line(a)
    if cov:
        h.append('<div style="color:#888;font-size:11px;margin-top:4px">{}</div>'
                 .format(cov))
    return "".join(h)


def _cutoff_audit_text(a: dict):
    """Plain-text CUTOFF AUDIT block. Returns a list of lines; never raises."""
    try:
        if a.get("error"):
            return ["CUTOFF AUDIT — could not compute ({}); override-vs-Kalshi "
                    "date checking did not run this morning".format(a["error"]), ""]
        # Suppressed = HAD disagreed, but the programs have ended. Past tense
        # and explicitly closed out, so it can never read as a live count: the
        # old copy said "all agree" and then "5 disagree" two lines apart, which
        # forces a re-read on the exact morning the section should cost zero
        # attention. Reported at all (rather than dropped) because silence would
        # be indistinguishable from a filter that had quietly eaten a live row.
        supp = ("  ({} resolved event(s) HAD disagreed; their programs have "
                "ended — history, no action: {})".format(
                    len(a["dead"]),
                    ", ".join(_short_event(e) for e in a["dead"][:6])
                    + (", ..." if len(a["dead"]) > 6 else ""))
                if a["dead"] else "")
        skipped = ("  ({} override(s) not comparable: {} carry no calendar day "
                   "in the ticker, {} are schedule-resolved series where a LATE "
                   "override means a postponed game)".format(
                       a["no_day"] + a["excluded"], a["no_day"], a["excluded"])
                   if (a["no_day"] or a["excluded"]) else "")
        if not a["rows"]:
            out = ["CUTOFF AUDIT: {} live overrides checked, all agree with "
                   "their Kalshi ticker date.".format(a["checked"])]
            for extra in (supp, skipped):
                if extra:
                    out.append(extra)
            # Date agreement is NOT the all-clear: CELH agreed on the date and
            # was still wrong by nine hours. The hour block renders here too.
            return out + _unverified_lines(a) + [""]
        out = ["CUTOFF AUDIT — our call-time override vs Kalshi's ticker date "
               "({} of {} checked disagree)".format(len(a["rows"]), a["checked"]),
               # every header MUST fit its field — "OUR CALL (ET)" is 13 chars
               # in a 12-wide column and silently shoved the whole header row
               # one column right of its data.
               "{:28s} {:>12s} {:>14s} {:>9s} {:>5s} {:>6s} {:>11s}".format(
                   "EVENT", "OUR CALL ET", "KALSHI SAYS", "DELTA", "MKTS",
                   "CTS", "POOL $/DAY")]
        for r in a["rows"]:
            why, action = _cutoff_meaning(r)
            out.append("{:28s} {:>12s} {:>14s} {:>9s} {:>5d} {:>6,.0f} {:>11,.2f}"
                       "{}".format(
                           _short_event(r["event"])[:28],
                           r["override_et"].strftime("%m-%d %H:%M"),
                           "{} {}".format(r["ticker_date"].isoformat()[5:],
                                          _days_out_phrase(r["days_out"]))[:14],
                           "{:+d}d {}".format(
                               r["delta"],
                               "LATE" if r["delta"] > 0 else "EARLY"),
                           r["mkts"], r["contracts"], r["dpd"],
                           "  <== RISK" if r["severity"] == "risk" else ""))
            for para in ("{} | {}".format(why, r["hour_label"]), action):
                out.extend(textwrap.wrap(para, width=76,
                                         initial_indent="      ",
                                         subsequent_indent="      "))
        out.append("  POOL $/DAY is the reward at stake if we stand down, NOT "
                   "the exposure; CTS is contracts on the book.")
        out.extend(_unverified_lines(a))
        for extra in (supp, skipped):
            if extra:
                out.append(extra)
        out.extend(textwrap.wrap(_CUTOFF_EXPLAINER, width=78,
                                 initial_indent="  ", subsequent_indent="  "))
        out.append("")
        return out
    except Exception as e:
        return ["CUTOFF AUDIT — render failed ({})".format(repr(e)), ""]


def _cutoff_audit_html(a: dict) -> str:
    """HTML twin of _cutoff_audit_text. Never raises."""
    try:
        head = ('<div style="font-size:15px;font-weight:600;margin:16px 0 4px">'
                'Cutoff audit &mdash; our call time vs Kalshi\'s ticker date</div>')
        if a.get("error"):
            return (head + '<div style="color:#c0392b;font-size:12px">Could not '
                    'compute ({}) &mdash; override-vs-Kalshi date checking did '
                    'not run this morning.</div>'.format(a["error"]))
        supp = (' &nbsp;{} resolved event(s) HAD disagreed; their programs have '
                'ended &mdash; history, no action ({}).'.format(
                    len(a["dead"]),
                    ", ".join(_short_event(e) for e in a["dead"][:6])
                    + (", &hellip;" if len(a["dead"]) > 6 else ""))
                if a["dead"] else "")
        skipped = (' &nbsp;{} override(s) not comparable: {} carry no calendar '
                   'day in the ticker, {} are schedule-resolved series where a '
                   'LATE override means a postponed game.'.format(
                       a["no_day"] + a["excluded"], a["no_day"], a["excluded"])
                   if (a["no_day"] or a["excluded"]) else "")
        if not a["rows"]:
            return (head + '<div style="color:#0a7a2f;font-size:12px">{} live '
                    'overrides checked &mdash; all agree with their Kalshi '
                    'ticker date.{}{}</div>'.format(a["checked"], supp, skipped)
                    + _unverified_html(a))
        h = [head, '<table style="border-collapse:collapse">',
             '<tr style="background:#f0f0f0;font-weight:600">'
             '<td style="{0}">EVENT</td><td style="{1}">OUR CALL TIME (ET)</td>'
             '<td style="{1}">KALSHI SAYS</td><td style="{1}">DELTA</td>'
             '<td style="{1}">DIR</td><td style="{1}">MKTS</td>'
             '<td style="{1}">CTS ON BOOK</td>'
             '<td style="{1}">POOL $/DAY</td></tr>'.format(TDL, TD)]
        for i, r in enumerate(a["rows"]):
            colour = {"risk": "#c0392b", "warn": "#d9821b",
                      "info": "#777"}[r["severity"]]
            why, action = _cutoff_meaning(r)
            h.append(
                '<tr style="background:{bg}"><td style="{tdl}">{ev}'
                '<div style="color:{col};font-size:11px">{why}</div>'
                '<div style="color:#333;font-size:11px;font-weight:600">{act}'
                '</div></td>'
                '<td style="{td}">{ov}<div style="color:#999;font-size:11px">'
                '{hour}</div></td>'
                '<td style="{td}">{tick}<div style="color:{col};font-size:11px">'
                '{when}</div>{kstart}</td>'
                # TD already ends in ';' — no extra one, or the declaration
                # renders as 'text-align:right;;color:...'
                '<td style="{td}color:{col};font-weight:700">{d:+d}d</td>'
                '<td style="{td}color:{col};font-weight:700">{dir}</td>'
                '<td style="{td}">{mkts}</td><td style="{td}">{cts:,.0f}</td>'
                '<td style="{td}">{dpd:,.2f}</td>'
                '</tr>'.format(
                    bg="#fafafa" if i % 2 else "#fff", tdl=TDL, td=TD,
                    col=colour, ev=_short_event(r["event"]),
                    why=why, act=action, hour=r["hour_label"],
                    ov=r["override_et"].strftime("%b %d %H:%M"),
                    tick=r["ticker_date"].isoformat(),
                    when=_days_out_phrase(r["days_out"]),
                    kstart=('<div style="color:#999;font-size:11px">event '
                            'start {}</div>'.format(imm_pickoff.fmt_et(
                                r["kalshi_start"]))
                            if r.get("kalshi_start") is not None else ""),
                    d=r["delta"],
                    dir="LATE" if r["delta"] > 0 else "EARLY",
                    mkts=r["mkts"], cts=r["contracts"], dpd=r["dpd"]))
        h.append("</table>")
        h.append('<div style="color:#888;font-size:11px;margin-top:4px">POOL '
                 '$/DAY is the reward at stake if we stand down &mdash; not the '
                 'exposure. CTS ON BOOK is the exposure.</div>')
        h.append(_unverified_html(a))
        h.append('<div style="color:#666;font-size:12px;margin-top:6px;'
                 'border-left:3px solid #d9a441;padding-left:8px">{}{}{}</div>'
                 .format(_CUTOFF_EXPLAINER, supp, skipped))
        return "".join(h)
    except Exception as e:
        return ('<div style="color:#c0392b;font-size:12px">Cutoff audit render '
                'failed ({}).</div>'.format(repr(e)))


DASH_DIR = os.environ.get("IMM_DASH_DIR", os.path.join(STATUS_DIR, "dashboard"))
DASH_SUMMARY_PATH = os.path.join(DASH_DIR, "imm_dashboard_summary.json")
DASH_SUMMARY_STALE_MIN = 45
DAILY_TABLE_DAYS = 30           # "show Daily P&L (raw) going back 1 month" (Jack 2026-10-02)
DAILY_NOTE = ("RAW = the dashboard's trading P&L: the bot's own book marked to "
              "market (realized + the change in open marks + settlements) per ET "
              "calendar day, n/a where the dashboard's position log does not "
              "reach; REWARD = its modeled rewards, accrual per ET day before "
              "Kalshi's $1-per-market floor")


def load_dashboard_summary(path=None) -> dict:
    """imm_dashboard.py's card totals ({} when missing or unreadable)."""
    return load_json(path or DASH_SUMMARY_PATH)


def dashboard_windows(summary: dict, now_ts: float, days: int = DAILY_TABLE_DAYS) -> dict:
    """The email's yesterday / 7-days rows and its daily table, read off the
    dashboard's summary (imm_dashboard.summary_of):

      day    the dashboard's Yesterday card: the full ET day
      week   its 7-days card: 00:00 ET six days ago through its last build
      daily  [(date, raw, reward, contracts)], newest first: each of the
             last `days` complete ET days as its day card shows it

    A window counts only when the build that cut it ran today (ET): an older
    build's "yesterday" is a different day. RAW is None where the dashboard
    shows n/a (the position log does not cover it). Per-event trading P&L for
    both windows feeds the finecon line; `note` says why figures are missing."""
    gen = _f(summary.get("generated"))
    wins = summary.get("windows") or {}
    today = datetime.fromtimestamp(now_ts, timezone.utc).astimezone(ET).date()
    built = _et_label(gen) if gen else ""

    def window(key, first_day):
        w = wins.get(key)
        start = et_midnight_ts(first_day)
        if not w or abs(_f(w.get("start")) - start) > 1:
            return {"raw": None, "reward": None, "events": None, "since": start}
        na = bool(w.get("pnl_na"))
        return {"raw": None if na else _f(w.get("pnl")), "reward": _f(w.get("rew")),
                "events": None if na else {ev: _f(v[1]) for ev, v in (w.get("e") or {}).items()},
                "since": start, "until": _f(w.get("end"))}

    day = window("yesterday", today - timedelta(days=1))
    week = window("7d", today - timedelta(days=6))
    first = today - timedelta(days=days)
    daily = []
    for r in summary.get("days") or []:
        try:
            d = datetime.strptime(str(r.get("d")), "%Y-%m-%d").date()
        except ValueError:
            continue
        if first <= d < today and not r.get("live"):
            daily.append((d, None if r.get("pnl") is None else _f(r.get("pnl")),
                          _f(r.get("rew")), _f(r.get("cts"))))
    daily.sort(key=lambda x: x[0], reverse=True)
    if not gen:
        note = ("DASHBOARD SUMMARY MISSING ({}) -- yesterday, 7 days and the daily "
                "table are n/a".format(DASH_SUMMARY_PATH))
    elif gen < et_midnight_ts(today):
        note = "DASHBOARD NOT REBUILT SINCE {} -- yesterday and 7 days n/a".format(built)
    elif (now_ts - gen) / 60.0 > DASH_SUMMARY_STALE_MIN:
        note = "dashboard last built {} ({:.0f} min ago)".format(built, (now_ts - gen) / 60.0)
    else:
        note = ""
    return {"day": day, "week": week, "daily": daily, "built": built, "note": note}


def lifetime_raw(state: dict, mids: dict):
    """(raw, realized, unrealized) for the lifetime row: the bot's PERSISTED
    realized_lifetime (the only source that survives restarts AND captures
    settlements of multi-week holds) plus its own book marked to mid now."""
    pos, avg = own_book(state)
    life_realized = _f(state.get("realized_lifetime"))
    life_unreal = 0.0
    for t, p in pos.items():
        m = mids.get(t)
        if m is not None:
            life_unreal += p * (m - avg.get(t, 0.0)) / 100.0
    return life_realized + life_unreal, life_realized, life_unreal


def build_digest(now_utc: datetime):
    """Returns (plain_text, html, risk_text, risk_html): the IMM section of
    the 7:00 portfolio email (send_portfolio_digest runs this script with
    --section-out; the IMM's own 7:10 email was cut 2026-10-02), and its
    risk-controls block on its own, which the portfolio email places under
    its chart. Yesterday, 7 days and the daily table are the dashboard's own
    figures (dashboard_windows); lifetime is the bot's."""
    today_et = now_utc.astimezone(ET).date()
    client = build_client()
    status = load_json(STATUS_PATH)
    state = load_json(STATE_PATH)
    ss = status_summary(status)
    pos, avg = own_book(state)
    # marks for the lifetime row: the bot's own book, now
    mids, _results = current_mids(client, set(pos))
    life_raw, _life_real, _life_unreal = lifetime_raw(state, mids)
    dw = dashboard_windows(load_dashboard_summary(), now_utc.timestamp())
    _rows, _tot, resting = event_rows(client)     # resting quotes, for capacity
    risk_ev, risk_rows = risk_section(client, state, status, resting, now_utc)
    # Kalshi's event starts vs ours, for the PICK-OFF block (never raises).
    # First, so the audit rows below can print Kalshi's start beside ours.
    pick = imm_pickoff.scan(client, now_utc,
                            meta=load_json(OVERRIDE_META_PATH))
    # pos (own_book, above) gives the audit its only real exposure number;
    # everything else it reports is reward pool. never raises — see docstring.
    audit = cutoff_audit(client, now_utc, pos, kalshi=pick["kalshi"],
                         pick_events=[r["event"] for r in pick["rows"]])
    health = health_line(status, ss, dw["note"])
    # LIFETIME reward is the BOT ESTIMATE (Jack 2026-08-04), not the credit
    # ledger: credits are paid at each program's period end, so the ledger
    # trails what the book has earned and never includes in-flight programs,
    # and beside same-instant RAW that lag reads as underperformance. Nor
    # reward_paid_lifetime: that counter began 2026-08-04 without
    # back-crediting, so it cannot stand for lifetime.
    w = {"day": dw["day"], "week": dw["week"],
         "life": {"raw": life_raw, "reward": ss["reward_lifetime"]}}

    def net_of(k):
        r, x = w[k]["reward"], w[k]["raw"]
        return (x + r) if (r is not None and x is not None) else None

    def money(v, dash="n/a"):
        return "{:+,.2f}".format(v) if v is not None else dash

    win_note = ("yesterday and 7 days are the dashboard's own cards: ET calendar "
                "days, 7 days = since {} through its {} build; lifetime is the "
                "bot's own counters".format(_et_label(w["week"]["since"]),
                                            dw["built"] or "last"))
    # TOTAL only over the days with a RAW, so the row adds up
    tot_days = [r for r in dw["daily"] if r[1] is not None]
    t_raw = sum(r[1] for r in tot_days)
    t_rew = sum(r[2] for r in tot_days)
    tot_note = ("TOTAL = the {} of {} days with a RAW; the dashboard's position "
                "log starts 2026-09-06".format(len(tot_days), len(dw["daily"]))
                if len(tot_days) < len(dw["daily"]) else "")

    # ---- plain text ---------------------------------------------------------
    L = ["=" * 66, "INCENTIVE MM — {} (the IMM bot's own book)".format(today_et), ""]
    # An opportunity, not a risk: its own marker, above the red banner,
    # because a window can be open right now.
    _pick = imm_pickoff.text_lines(pick, now_utc)
    if _pick:
        L.append(">> " + _pick[0])
        L.extend(_pick[1:])
        L.append("")
    _banner = cutoff_banner(audit)
    if _banner:
        L.append("!! " + _banner)
        L.append("")
    L.append("P&L  (RAW = trading P&L, marked to market; NET = RAW + modeled rewards)")
    L.append("{:10s} {:>11s} {:>11s} {:>11s}".format(
        "WINDOW", "RAW$", "REWARD$", "NET$"))
    for key, lbl in (("day", "yesterday"), ("week", "7 days"),
                     ("life", "lifetime")):
        L.append("{:10s} {:>11s} {:>11s} {:>11s}".format(
            lbl, money(w[key]["raw"]), money(w[key]["reward"]),
            money(net_of(key))))
    L.append("  (" + win_note + ")")
    if dw["note"]:
        L.append("  !! " + dw["note"])
    L.append("")
    L.append("DAILY P&L — the last {} days, as on the dashboard".format(DAILY_TABLE_DAYS))
    L.append("{:12s} {:>11s} {:>11s} {:>11s} {:>10s}".format(
        "DATE", "RAW$", "REWARD$", "NET$", "CONTRACTS"))
    for day, raw, reward, contracts in dw["daily"]:          # newest first
        net = (raw + reward) if (raw is not None and reward is not None) else None
        L.append("{:12s} {:>11s} {:>11s} {:>11s} {:>10,.0f}".format(
            day.isoformat(), money(raw), money(reward), money(net), contracts))
    if dw["daily"]:
        L.append("{:12s} {:>11s} {:>11s} {:>11s}".format(
            "TOTAL", money(t_raw), money(t_rew), money(t_raw + t_rew)))
    else:
        L.append("  (no days: " + (dw["note"] or "the dashboard summary has none") + ")")
    if tot_note:
        L.append("  (" + tot_note + ")")
    L.append("  (" + DAILY_NOTE + ")")
    L.append("  (REWARD is accrual-dated and does NOT line up with a credit "
             "date — Kalshi pays at each program's period end, 1-2 days later)")
    L.extend(_calibration_caveat_text())
    L.append("")
    L.append("")
    L.extend(_cutoff_audit_text(audit))
    _perr = imm_pickoff.error_text(pick)
    if _perr:
        L.append(_perr)
    L.append("")
    L.append(health)
    text = "\n".join(L)
    # The risk-controls block is returned on its own (Jack 2026-10-03: "move
    # the risk controls section right under the chart at the top"): the
    # portfolio email places it under its balance chart. The finecon /
    # open-scan tracker that used to close this section is gone (same day:
    # "remove open scan and finecon tables and sections").
    risk_text = "\n".join(risk.text_lines(risk_rows, risk_ev, ET, capacity_note()))

    # ---- html ---------------------------------------------------------------
    h = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;'
         'border-top:2px solid #ddd;margin-top:22px;padding-top:12px">']
    h.append('<div style="font-size:17px;font-weight:600">Incentive MM'
             ' <span style="color:#888;font-weight:400">— the IMM bot&rsquo;s '
             'own book, {}</span></div>'.format(today_et))
    nl = net_of("life")
    h.append('<div style="font-size:18px;font-weight:700;margin:6px 0 2px">'
             'Lifetime net: {}<span style="font-size:13px;font-weight:400;'
             'color:#999"> &nbsp;= trading {:+,.2f} + rewards {:,.2f}</span>'
             '</div>'.format(_pnl_span(nl) if nl is not None else "n/a",
                             w["life"]["raw"], w["life"]["reward"]))
    h.append(imm_pickoff.html_block(pick, now_utc))        # "" when none
    if _banner:
        h.append('<div style="background:#fdecea;border-left:4px solid #c0392b;'
                 'color:#8e2b21;padding:8px 10px;margin:8px 0;font-weight:600">'
                 '{}</div>'.format(_banner))
    h.append('<table style="border-collapse:collapse;margin:10px 0">')
    h.append('<tr style="background:#f0f0f0;font-weight:600">'
             '<td style="{0}">WINDOW</td><td style="{1}">RAW (trading)</td>'
             '<td style="{1}">REWARD</td><td style="{1}">NET</td></tr>'
             .format(TDL, TD))
    for i, (key, lbl) in enumerate((("day", "Yesterday"), ("week", "7 days"),
                                    ("life", "Lifetime"))):
        bg = "#fafafa" if i % 2 else "#fff"
        x, n = w[key]["raw"], net_of(key)
        h.append('<tr style="background:{0}"><td style="{1}">{2}</td>'
                 '<td style="{3}">{4}</td><td style="{3}">{5}</td>'
                 '<td style="{3};font-weight:700">{6}</td></tr>'.format(
                     bg, TDL, lbl, TD, _pnl_span(x) if x is not None else "n/a",
                     money(w[key]["reward"]), _pnl_span(n) if n is not None else "n/a"))
    h.append("</table>")
    h.append('<div style="color:#888;font-size:12px;margin:-6px 0 6px">{}.</div>'
             .format(win_note))
    if dw["note"]:
        h.append('<div style="color:#c0392b;font-size:12px;font-weight:600;'
                 'margin:-2px 0 6px">{}</div>'.format(dw["note"]))
    h.append('<div style="font-size:15px;font-weight:600;margin:10px 0 4px">'
             'Daily P&amp;L &mdash; the last {} days, as on the dashboard</div>'
             .format(DAILY_TABLE_DAYS))
    h.append('<table style="border-collapse:collapse">')
    h.append('<tr style="background:#f0f0f0;font-weight:600">'
             '<td style="{0}">DATE</td><td style="{1}">RAW$</td>'
             '<td style="{1}">REWARD$</td><td style="{1}">NET$</td>'
             '<td style="{1}">CONTRACTS</td></tr>'.format(TDL, TD))
    for i, (day, raw, reward, contracts) in enumerate(dw["daily"]):
        bg = "#fafafa" if i % 2 else "#fff"
        net = (raw + reward) if (raw is not None and reward is not None) else None
        h.append('<tr style="background:{0}"><td style="{1}">{2}</td>'
                 '<td style="{3}">{4}</td><td style="{3}">{5}</td>'
                 '<td style="{3};font-weight:600">{6}</td>'
                 '<td style="{3}">{7:,.0f}</td></tr>'.format(
                     bg, TDL, day, TD,
                     _pnl_span(raw) if raw is not None else "n/a",
                     money(reward),
                     _pnl_span(net) if net is not None else "n/a", contracts))
    if dw["daily"]:
        h.append('<tr style="background:#f0f0f0;font-weight:700">'
                 '<td style="{0}">TOTAL</td><td style="{1}">{2}</td>'
                 '<td style="{1}">{3}</td><td style="{1}">{4}</td>'
                 '<td style="{1}"></td></tr>'.format(
                     TDL, TD, _pnl_span(t_raw), money(t_rew), _pnl_span(t_raw + t_rew)))
    h.append("</table>")
    if not dw["daily"]:
        h.append('<div style="color:#c0392b;font-size:12px">No days: {}.</div>'.format(
            dw["note"] or "the dashboard summary has none"))
    h.append('<div style="color:#888;font-size:12px;margin-top:4px">{}{}. REWARD is '
             'accrual-dated, so it does NOT line up with a credit date &mdash; '
             'Kalshi pays at each program\'s period end, 1&ndash;2 days later.'
             '</div>'.format(tot_note + ". " if tot_note else "", DAILY_NOTE))
    h.append(_calibration_caveat_html())

    h.append(_cutoff_audit_html(audit))
    if _perr:
        h.append('<div style="color:#888;font-size:11px;margin-top:4px">{}'
                 '</div>'.format(imm_pickoff._esc(_perr)))

    h.append('<div style="color:#777;font-size:12px;margin-top:12px;'
             'border-top:1px solid #eee;padding-top:8px">{}</div>'.format(health))
    h.append("</div>")
    risk_html = risk.html(risk_rows, risk_ev, ET, capacity_note(), TD, TDL)
    return text, "".join(h), risk_text, risk_html


def subject_flag(text: str) -> str:
    """The PICK-OFF tag for the email's subject line ("" when no window is in
    the text): the portfolio email carries it since the IMM section moved in."""
    return (" - " + imm_pickoff.HEADER) if (">> " + imm_pickoff.HEADER) in text else ""


def write_section(path: str, now_utc: datetime) -> None:
    """--section-out: build the section and write {text, html, risk_text,
    risk_html, subject_flag, built_at} as JSON for send_portfolio_digest,
    which puts the risk block under its chart and the rest after its movers
    table. A few retries, as the old standalone send had: a Kalshi read can
    fail on the first try."""
    for attempt in range(1, 4):
        try:
            text, html, risk_text, risk_html = build_digest(now_utc)
            break
        except Exception as e:
            log(f"imm section build attempt {attempt}/3 failed: {e!r}")
            if attempt == 3:
                raise
            time.sleep(60)
    out = {"text": text, "html": html, "risk_text": risk_text,
           "risk_html": risk_html, "subject_flag": subject_flag(text),
           "built_at": datetime.now(timezone.utc).isoformat()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, path)
    log(f"imm section written: {path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--section-out", metavar="PATH",
                    help="build the IMM section and write it to PATH as JSON "
                         "(send_portfolio_digest runs this); sends nothing")
    ap.add_argument("--test", action="store_true",
                    help="send the section now as a standalone email (the "
                         "pre-2026-10-02 digest); writes no marker")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="build and print the section; send nothing")
    ap.add_argument("--html-out", help="with --print, also write the HTML here")
    args = ap.parse_args(argv)

    if args.section_out:
        write_section(args.section_out, datetime.now(timezone.utc))
        return 0

    if args.print_only:
        body, html, risk_text, risk_html = build_digest(datetime.now(timezone.utc))
        print(risk_text + "\n\n" + body)
        if args.html_out:
            with open(args.html_out, "w", encoding="utf-8") as f:
                f.write(risk_html + html)
            log(f"wrote {args.html_out}")
        return 0

    if not args.test:
        # Jack 2026-10-02: "cut it as a standalone email and add it into the
        # Kalshi portfolio email". The 7:10 task still lands here; it is a
        # no-op until the task is deleted.
        log("standalone IMM digest retired 2026-10-02: the IMM section rides in "
            "the 7:00 portfolio email (send_portfolio_digest.py); --test sends "
            "one by hand")
        return 0

    now_utc = datetime.now(timezone.utc)
    body, html, risk_text, risk_html = build_digest(now_utc)
    # standalone, the risk block leads, as it does under the portfolio chart
    body, html = risk_text + "\n\n" + body, risk_html + html
    alerter = Alerter("IMM-DIGEST", live=True)
    if not alerter.enabled:
        log("cannot send imm digest: alert credentials not configured")
        return 1
    subject = "Kalshi incentive MM digest {}{}".format(
        now_utc.astimezone(ET).date(), subject_flag(body))
    ok = alerter.send_message(body, subject=subject, html=html)
    log(f"imm digest send: {'ok' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
