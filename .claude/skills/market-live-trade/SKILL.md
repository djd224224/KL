---
name: market-live-trade
description: Playbook for a hand-specified LIVE trading session on one event while the information is landing (election night results, a debate, a game, a hearing) -- Jack's window, ladder, anchor and caps; the bot ignores his manual trades; no halts; a live P&L page; changes made live during the session; then end it cleanly and reconcile. Built from the 11/03 KXSENATESEATSD-27 election night session (ENS). Use when Jack wants the IMM to trade an event live for a few hours, mentions an "election night session" / "live session" / trading an event as it happens, or invokes /market-live-trade.
---

# Live event trading session playbook

Built from election night 2026-11-03 on KXSENATESEATSD-27 (a ladder of "Democrats win at
least N Senate seats" markets, 47-58), 7 PM to midnight ET. Jack's goal: "primary goal is
trading profit, while secondary goal is to collect rewards". Full history: memory
`imm-election-night-session`. Read `jack-working-style` first.

**How this differs from `/market-wave`.** The wave playbook avoids information risk: it
quotes thin books for rewards and stands down before news lands. This one trades INTO the
news with a hand-set ladder, while Jack watches and trades the same event by hand.

## Ground rules

- **Get the spec in Jack's words, then read it back as numbers before building:** event, markets to skip, window, rung size, rung count and spacing, anchor rule, caps, halts, and how his manual trades count. 11/03: "starting at 7:15pm until midnight ... ignore [my manual orders] ... 1x base contracts, 3 rungs in ladder placed 3c apart, market cap of 150 in any direction. dont want any halting", then "launch at 7pm".
- **Land the deploy BEFORE the start.** An env change needs a task restart (`restart_imm.ps1 -Task`); a code change needs push -> KL sync -> the code-change restart. 11/03 was pushed at 23:51Z, and the `KL sync-kl-main` task was started by hand at 6:52 PM ET, so the 7 PM start held.
- **Expect many live changes.** 11/03 had about 10 in two hours. Each one is a short, quoted commit (`(Jack 2026-11-03: "...")`), pushed, synced and checked on Kalshi within minutes. Give Jack the result, not the plan.
- Jack alone moves money and flattens positions by hand. The bot never trades his book.

## The machinery (ENS, `incentive_mm.py` around `ENS_EVENTS`)

All of it is env-driven; the defaults are OFF since 36049fa (`IMM_ENS_EVENTS=""`).

| Setting | Meaning | 11/03 final |
|---|---|---|
| `IMM_ENS_EVENTS` | Event(s) in the session. Empty = off. | `KXSENATESEATSD-27` |
| `IMM_ENS_START_ET` / `IMM_ENS_END_ET` | Window, `YYYY-MM-DD HH:MM` ET. A 00:00 -> start blackout; the cutoff is END (it replaces the family cutoff). | 19:00 -> 00:00 |
| `IMM_ENS_LEVELS` | Offsets ladder from the anchor, `offset:size,...` | `0:36,3:36,6:36,9:36` |
| `IMM_ENS_MIN_AHEAD` / `IMM_ENS_JOIN_TOUCH_MIN` | Anchor 1c behind the best price where OTHERS rest at least 50; join the touch when others rest 100+ there. A side with no such price rests nothing. | 50 / 100 |
| `IMM_ENS_SKIP_MARKETS` | Markets never quoted (blocked; their orders get pulled). | 47, 51, 57, 58 |
| `IMM_ENS_MAX_POSITION` / `IMM_ENS_EVENT_CAP` | Per-market and event caps. 1,000,000 = none. | none, after "remove event cap and market caps" |
| `IMM_ENS_SKEW_SOFT` / `_HARD` | The adding side is halved at SOFT net contracts and pulled at HARD, on the ACCOUNT position. | 90 / 180 |
| `IMM_ENS_PRICE_MIN` / `_MAX` | Price band, no pads. | 2 / 98 |

Built-in behavior:
- **Forced** (`FORCE_EVENTS`): no payout or rate floor, no hopeless exit, no admission wait.
- **No halts:** `toxic_exempt` and `sweep_exempt` (no toxic halt, no sweep breaker, no tape pause). The WS fast cancel and the account-level halts stay.
- **Ignores his trading:**
  - His positions and resting orders never stand the bot off (`manual_coexist` + the foreign-order scan).
  - His fills never touch the bot's book (fills are matched by order ownership).
  - The cap, skew and unwind use the ACCOUNT position (`ens_cap_pos`; Jack: "account for the manual trades, when looking at exposure and caps").
- **Flat sizes:** no family x2, no size test, no near-cliff boost, no long-shot cut or lean, `atref_price_tol_ticks=0`.
- **No-loss close** (`ens_no_loss_quotes`, Jack "try not to match at a loss"): the closing side starts at or past the bot's average cost. The cost: a short can't be cut when the market runs against it.
- **Comp hedges** (ladder locks on the neighboring strike) stay on: maker-only, at least +2c a contract after fees.
- **Flatten file:** `run-logs/incentive-mm/own_book_flatten.txt`, one ticker a line. It zeroes the BOT's own position and average after Jack offsets a position by hand. It's consumed once and renamed `.done`.

Limits to fix or work around first:
- **The override is keyed by SERIES** (`ens_series_override` on `SERIES_OVERRIDES[series]`), so every event of that series inherits the blackout, the ladder and no caps. 11/05: KXSENATESEATSD-29 was blacked out every day until 7 PM ET and quoted uncapped in the evening. Pick an event whose series has no other open event, or scope the override to the event first (a code change).
- **Base archetype = elections.** For a non-election event, check that its series passes `IncentiveMarketMaker._allowed` (add it to `extra_allow_series.json` if not), and walk its family's own gates.
- The session's blackout logs with the generic text "AAA print blackout".

## Phase 1 -- spec and build (before the start)

1. Read the event's markets and books; propose which to skip (Jack skips extremes and ones he trades).
2. If the machinery fits, it's env only: set the `IMM_ENS_*` values in the launcher (`run_incentive_mm.ps1`), then run `restart_imm.ps1 -Task` before the start. Otherwise it's a code change with tests:
   - Tests: `TestElectionNightSession` / `TestElectionNightSessionEnded`, with `_ens_1103_on(self)` turning the 11/03 session on in a test.
   - Run the full suite with `IMM_STATUS_DIR` at a scratch dir and compare it against a main baseline. Don't trust "exit 0" from a piped tail, and never edit the worktree while its suite runs.
3. Start the live page, standalone and hidden. The desktop app stops browser-pane preview servers, so Start-Process it:
   ```
   Start-Process -FilePath "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe" -ArgumentList 'C:\Users\jackd\Documents\KL\imm_ens_watch.py','--event','<EVENT>','--port','8765' -WorkingDirectory C:\Users\jackd\Documents\KL -WindowStyle Hidden
   ```
   - What it reads: markets, orders and positions every 5 s, plus the bot's `fills_*.jsonl`.
   - What it shows: P&L, FIFO realized vs open (at mid); matched contracts split profit/loss/flat; per-market curves on one scale.
   - Don't use `/portfolio/fills` for this: it ignores event_ticker, and the order-id split misattributes his fills.

## Phase 2 -- at the start

Verify on Kalshi, not the log: the bot's orders per market and side at the expected
anchor and rungs (11/03: 51 orders on strikes 47-56 at 00:01Z), his resting orders left
alone, and the skip markets empty.

## Phase 3 -- during (check every 10-15 min, and on every ask)

- P&L realized vs open, by market. Inventory vs the skew knees. Which sides are dark and why (no 50+ price to anchor behind, the skew, the band, a skip).
- His trades vs the bot's book: `own_pos` vs `acct_pos` in the cycle log.

The asks that came up on 11/03, and what fixed them:

| Jack's ask | Change |
|---|---|
| "dont sit at the touch, start at least 1 below the touch, and must be below a rung that has at least 50 contracts" | Anchor = 1c behind the best price with 50+ others (`ens_anchors`) |
| "sit at the touch if there are at least 100 contracts there" | Join the touch at 100+ |
| "Yes, all at 20" / "increase to 30" / "add a 4th rung" / "scale everything up by 20%" | Flat rungs: 20 -> 30 -> 4 rungs -> 36 |
| "what is this 60 limit?" -> "Yes, scale to 150" | Skew knees 30/60 -> 75/150 -> 90/180 |
| "dont quote at 99c/1c" | Band 2-98, no pads |
| The event cap split over ~9 markets fit only 3 rungs | Event cap = 150 a market x 10 |
| "try not to match at a loss" | No-loss close at the bot's average cost |
| He offset a position by hand | Flatten file on that ticker |
| "account for the manual trades, when looking at exposure and caps" | Caps and skew on the account position |
| His +4,610 net YES left the bot only selling -> "remove event cap and market caps" | Caps off; the skew is the only inventory limit |
| "dont quote 'at least 47'", "stop trading '51'", "dont trade 57, 58" | Skip markets |
| A falling ask left the bot's asks 10c behind | `atref_price_tol_ticks=0` |

## Phase 4 -- end and reconcile

- At END the cutoff cancels everything; positions ride to settlement.
- **Next morning:** reconcile the bot's book against the account per market. The dashboards' carry and worst case use the BOT's book. On 56 the bot was -505 (avg 20.9) while his 38 hand fills were +504, so the account was -1.26, yet carry showed -$330 to -$380. Use the cycle log's `acct_pos` vs `own_pos`, or Kalshi fills split by the bot's order ids (sign by `book_side`). Propose a flatten-file entry where the account is flat but the bot's book isn't.
- **End the session** (`IMM_ENS_EVENTS=""`, the default, plus a task restart), so the series goes back to its family rules. Check that nothing else in the series inherited the override.
- **Post-mortem:** realized vs open P&L by market, matched profit/loss/flat contracts, rewards earned, which live changes paid. Update the session's memory and this playbook.

## 11/03 reference

- **Live:** 00:00:45Z 11/04 (run b9eab0de). By 10 PM ET +$160.94 ($126.86 realized).
- **No-loss close cost:** with the close on, market 55 sat short 179 at an average of 47.3 against a 53/66 market (-$20 open).
- **Where it hurt:**
  - The rules that capped exposure on the account position also flipped the bot one-sided when his hand position was large.
  - The series-wide override outlived the night.
