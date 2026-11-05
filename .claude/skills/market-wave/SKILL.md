---
name: market-wave
description: Playbook for quickly quoting a big wave of newly incentivized Kalshi markets with the IMM (elections, awards, sports props, releases, any series) -- size up the programs, triage the books, deploy a forced per-market list with guards, stand up the dashboard, tune through the day, keep a scanner ready for relights, then post-mortem. Use when Jack says a wave / batch of new markets or programs just launched and he wants them quoted fast, or invokes /market-wave.
---

# Market wave playbook

Built from the 11/04/2026 post-vote election wave: ~5,300 programs (~$450k/day) lit at
05:40Z, Jack asked mid-morning, live at 13:49Z (8:49 AM ET), ~480 markets quoted, ~20
tuning rounds, an overnight scanner by 03:18Z. Full history: memory
`imm-post-vote-dead-books`. Read `jack-working-style` and `imm-quoting-triage` first.

**Speed target:** first quotes within ~1 hour of Jack's ask, dashboard right after, then tune.

## Ground rules

- Jack decides going live, sizes, money, schedules and which markets. Bring numbered options
  with $/day and $ at risk; short replies ("yes", "do 1,2", "Stop the 27") approve exactly
  what was offered. "just deploy it when ready" is a standing go for the first list.
- List changes are hot-loaded (no restart). Bot code changes: worktree -> tests with
  `IMM_STATUS_DIR` at a scratch dir -> push origin/main -> `schtasks /run /tn "KL sync-kl-main"`
  -> the bot's code-change restart. Check the suite result before committing.
- Never run test fixtures against the live STATUS_DIR. Don't read or print credentials.
- Quote Jack in commit messages and in the list's notes (`"x125": "...(Jack: ...)"`).

## The tools (all in KL)

| Tool | What it does | Notes |
|---|---|---|
| `run-logs/incentive-mm/post_vote_dead_books.json` | The forced per-market list. The name is historical: it works for any series the bot allows. Hot-loaded by mtime. | Fields below |
| `pvd_scan.py` | Scorer and scanner: programs -> books (our orders netted out) -> kind, guard, size, $ by stop, ROI -> list entries. Dry run by default; `--write` writes. | Election mode by default; any-wave flags |
| `imm_pvd_watch.py` | Dashboard on http://127.0.0.1:8771: est rewards vs trading P&L, dead/live, source, per-market banked vs $1, fills. | Env for a new wave |
| Task `KL pvd-scan` | Runs `pvd_scan.py --write` (election mode) every 15 min, :07/:22/:37/:52 local. | `run_pvd_scan_hidden.vbs` |
| `send_imm_new_programs.fetch_programs_raw(client)` | Every program row, unaggregated (per-program start/end). | For the size-up |

`pvd_scan.py` and `imm_pvd_watch.py` are untracked in KL until committed. Scratch scripts
in a session scratchpad disappear; put anything worth keeping in KL.

## Phase 1 -- size up the wave (15 min)

1. Pull the programs and group them: series, events, markets, pool $/day, start/end,
   target size. Liquidity programs only (`incentive_type == "liquidity"`); the bot ignores
   volume programs.
2. Why isn't the bot already quoting them? Read the latest `universe:` line's skips and
   `selection_events_<UTC date>.jsonl` for a sample ticker. Walk `imm-quoting-triage`. The
   usual answers: not a member series, past a family cutoff (11/04: the election
   voting-day cutoff), the payout floor.
3. Name the **information risk**: what moves these prices, and can someone see it before
   our resting order moves? (11/04: county count updates moved vote-share and margin
   markets for days; winner markets settled down once called.) This drives the stop rule
   and the size.

## Phase 2 -- triage the books (30 min)

Elections (post-vote, already members): `python pvd_scan.py` (dry run).
Any other wave:

```
python pvd_scan.py --series KXFOO,KXBAR --no-vote-check --new-since <wave start ISO> --stop-cap <ISO> --report <path>.json
```

- `--series`: series prefixes in scope. `--no-vote-check`: skip the election past-the-vote test.
- `--stop-cap`: no entry stops later than this. **Set it outside elections** (Phase 3); the bot also never runs past the series' own cutoff there.
- `--new-since`: programs that started after this count as new.
- `--report`: every market that clears the bar, as JSON + CSV.
- `PVD_SCAN_SIM_HOURS=24` (dry run only): score every market as if relit for a fresh 24h period.
- Sizes, bar and budget come from env: `PVD_SCAN_DEAD_SIZE` (120), `_LIVE_SIZE` (80),
  `_DECIDED_SIZE` (300), `PVD_SCAN_BAR` ($1.10), `PVD_SCAN_BUDGET` ($60k).

What it scores, per market (the 11/04 rules):
- **Kind:** dead = external touch 30c+ wide (we often set the reference ourselves); live = narrower.
- **Decided:** last trade >= 90c means the bid is the decided side; <= 10c means the ask. The decided side gets no cushion, 2-99c.
- **Other sides:** 3-97c, and at least a cushion off the last trade (dead 2c, live 5c).
- **Placement and share:** one rung a side at the book's reference (where cumulative size reaches target/5 = 200). Our share comes from the bot's `estimate_reward_share`: both sides must reach the target or the snapshot pays nobody.
- **$ by the stop** = share x pool $/day x hours left. Under the bar = not listed. Kalshi pays each program **period** only if it accrues >= $1. It has paid ~1.15x the bot's estimate; $1.30 is the healthy bar, $1.10 is the bot's general bar (f0f0941).
- **ROI** = $/day / $ at risk if the rung fills. The best ROI is taken first, within the budget.

Treat the projection as optimistic. 11/04 listed at ~$2,700/day and realized $1,100-1,500/day
once other makers joined the books.

## Phase 3 -- membership, stop and caps (decide before writing)

1. **Membership.** A market with a live list entry is allowed by itself (`IncentiveMarketMaker._allowed`; Jack 2026-11-05 "Yes, close the gaps"). The list admits the MARKET, not its series, and the blocklist and freezes still win. So no enrollment is needed.
   - Enroll a whole series (`extra_allow_series.json`, hot-loaded, merged by the daily task) only if Jack wants every market of it quoted under default rules.
2. **Stop.** Outside elections a list entry's `stop` can only SHORTEN the series' own cutoff: cutoff = min(series cutoff, stop), or the stop when the series has none.
   - An election series' stop REPLACES the cutoff; that's how the list lifts the voting-day stand-down. So does an entry marked `"override_cutoff": true`. Use that only for post-event, unsettled markets, where the family stands down but the news has already happened.
   - Still set `--stop-cap` (or per-entry stops) to before the information lands: a game start, an announcement, a data release, a count drop. Leave a buffer. A non-member series' own cutoff is often just Kalshi's dates.
3. **Per-event caps still apply.** Event top-N holds awards and graduate families to 3 markets an event; gas, diesel, TrueV, data centers and shipping have their own caps. Raise `IMM_EVENT_TOP_N` (env, task restart) if the wave needs more per event.

On the list, a market gets one flat rung a side, its own caps and the PVD guard in place of
the family ladder, multipliers, leans, leads and pads. Family gates and fair-value
stand-asides still run first; a side they empty stays dark. For a series with its own fair
model, prefer family multipliers over the list.

## Phase 4 -- deploy (target: first quotes within the hour)

- Scanner path: the Phase 2 command plus `--write`. Otherwise write entries by hand (fields
  below). Back the file up first (scratchpad), write atomically (tmp + `os.replace`), and
  never while `run-logs/pvd-scan/scan.lock` exists.
- Verify, in order:
  1. The day log shows `post-vote dead books: N market(s) ... (k changed)`.
  2. At the next 10-min universe refresh, `selection_events_<UTC date>.jsonl` flips the
     ticker `cutoff`/other -> `selected`. A list loaded between refreshes waits for it
     (11/05 Collins: written 03:18:33Z, selected 03:20:33, placed 03:21:11).
  3. The bot's resting orders on Kalshi: `imm._fair_reader_client().get("/portfolio/orders",
     params={"status": "resting", ...})`, filtered by the `imm-` client order id. The
     exchange is the truth, not the log.
  4. `pvd guard <t>: <side> out (limits ...)` lines explain a dark side.
- Kill switches:
  - Whole list: empty `"markets"`, or set `IMM_PVD=0` and run `restart_imm.ps1 -Task`.
  - One market: move its entry to the top-level `"stopped"` section (`{ticker: {at, reason, entry}}`; the bot reads only `"markets"`). It's cancelled on the next quote cycle (PVD_REMOVED; 11/05: 0 orders within 45s), the scanner skips it until a later program period, and the dashboard keeps its history.

## Phase 5 -- dashboard (right after first quotes)

Start it hidden, polling every 5 min:

```
Start-Process -FilePath "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe" -ArgumentList 'C:\Users\jackd\Documents\KL\imm_pvd_watch.py','--port','8771' -WorkingDirectory C:\Users\jackd\Documents\KL -WindowStyle Hidden -RedirectStandardOutput C:\Users\jackd\Documents\KL\run-logs\pvd-watch\watch.out.log -RedirectStandardError C:\Users\jackd\Documents\KL\run-logs\pvd-watch\watch.err.log
```

- For a new wave, set `PVD_WATCH_GO_LIVE=<ISO>` and `PVD_WATCH_DIR=run-logs\pvd-watch-<wave>`, so its history and go-live are its own.
- Restart pattern: stop the python processes whose command line contains `imm_pvd_watch`, then start it again.
- The port check is on purpose (`allow_reuse_address = False`): ports 8765/8766 run the ENS page.
- What it reads:
  - Est rewards are live share x pool, integrated poll to poll.
  - Trading is the bot's fills since go-live, FIFO, marked at the LAST TRADE. That understates the settlement loss on positions the result went against.
  - "Banked (period)" resets when an entry's `period_start` changes.
  - A faded row means no order resting, or the market is past its stop.

## Phase 6 -- tune (every 30-60 min, or whenever Jack asks)

| Symptom | Check | Lever (11/04 evidence) |
|---|---|---|
| Rate falls, list unchanged | Faded rows; `pvd guard` lines | Cushion too wide. At 10c, 264 of 571 markets stood down; at 2c dead / 5c live, ~376 quoted and the rate came back to ~$1.3-1.5k/day. Limits follow the last trade both ways. |
| A market pacing under $1 this period | Banked + Proj. by stop | Raise its size up to the caps. Finish line: cushion 0 + band 2-99c. Drop it if it's under $0.87 even at 200 and holds no position. |
| Decided markets (>= 90c / <= 10c) | Last trade | Decided side: no cushion, 2-99c, size up (200 -> 300). Its fills made +3.9c/contract. `no_losing_side` rests only a closing rung on the losing side. |
| Positions with no exit | `pvd close` lines | Closing exemption (built): the reducing side may rest inside the cushion, capped at the position. |
| Join-ahead pushes a rung past its limit | `pvd join fallback` | Built: the rung rests at the reference instead. |
| Fills cluster on information drops | Public tape jumps by family/state; fills by hour | Stop those markets (`"stopped"`) or cut their size. 11/04: in-play dead vote-share/margin books were the only net loser (-$37 at 1.15x and settlement); the 27 western ones were stopped overnight. |
| Trading P&L slides | Realized vs open; marks at 01:00Z vs now | 11/04: -$245 of the slide was re-marks of positions already held, -$6 new fills. Hold: closing costs the spread (~$300 more than the marks). |
| Share of fills from the wrong side | Fills vs whether the market is decided now | 11/04: -$287 of the open loss came from fills while in play; -$37 after the market was decided. |

Re-sizing many markets: the bot resizes an order at its next refresh (`IMM_ORDER_REFRESH_SECS`).
Allow ~25 min to converge before judging. A size change briefly shows old + new orders both
resting.

## Phase 7 -- overnight and relights

- Election waves: the `KL pvd-scan` task already handles relights.
  - A listed market whose program runs past its stop is **extended**: only the stop moves.
  - A new program period is **relit**: re-scored and re-sized.
  - Unlisted programs started after NEW_SINCE are **new**.
  - All are sized at full 120 dead / 80 live / 300 decided side (Jack: "bank rewards early and overnight when there are less fills"), $1.10 bar, $60k budget, best ROI first.
- Another wave: register a second task with the wave's flags, a local-time trigger (StartBoundary `ToString('s')`), IgnoreNew and a 25-min limit, modelled on `KL pvd-scan`. Only on Jack's yes.
- At a stop the bot's selection screen cancels on its next cycle (meta.cutoff = the stop). There's no gap where the market quotes under family rules.
- Before Jack sleeps, give him: what stops when, what the scanner will add (a `PVD_SCAN_SIM_HOURS=24` what-if: count, $ at risk vs budget), shard-0 cash, and how to turn it off.

## Phase 8 -- post-mortem (next morning)

- Net by kind x family x state: est x 1.15 + trading. Show trading two ways: at the last trade, and with decided markets settling where they point (11/04 at 03:30Z: +$318 vs +$456).
- Open P&L split: fill time, in play vs decided at fill, mark moves vs new fills.
- What to change for the next wave. Then update the wave's memory file and this playbook.

## List entry fields (`"markets": {ticker: {...}}`)

| Field | Meaning |
|---|---|
| `bid_max`, `ask_min` | Required ints. Limits used only when there's no last trade (the guard follows the last trade otherwise). |
| `stop` | ISO UTC. Required. Replaces an election series' cutoff; elsewhere it can only shorten it. |
| `override_cutoff` | `true`: the stop replaces the series cutoff (post-event, unsettled markets). |
| `size`, `bid_size`, `ask_size`, `max_position` | Per-side rung sizes. The cap defaults to the largest side. |
| `kind`, `cushion`, `price_min`, `price_max` | `dead`/`live`; the cushion overrides the kind default (2/5); band default 3-97. |
| `no_losing_side`, `event` | Once decided, the losing side rests only a closing rung; event for FORCE_EVENTS. |

Anything else in an entry (notes, `pre_x125`, `period_start`, `coll_usd`, ...) is ignored by
the bot and read by the scanner and dashboard. Top-level keys other than `markets` are notes,
plus `stopped`.

## Gotchas (each cost time on 11/04)

- Public reads are rate-limited per IP and shared by the scanner, the dashboard and scratch scripts. Back off on 429s and reuse the last good read.
- After 00:00Z the bot writes `orders_/fills_<new UTC date>.jsonl`; the day log keeps its start-date name. Read the right file.
- Static limits go stale. Two quotes ended up through the last trade until limits followed it (PVD_LAST, refreshed each universe refresh).
- Sub-penny books: the guard judges `price_exact`, not the cent bucket. Asks at 1.1-1.9c slipped past a 2c floor until 57e904d.
- Don't trust regex state maps: one read "MI" out of KXMIDTERMVOTETURN and "WI" out of KXSENATEWITHIN5. Use per-series rules, and list what can't be placed.
- Old hand-built entries have `added` notes starting "scan 14:10Z ...". Scanner rows start "scan 20...".
- A what-if dry run holds the scanner lock, so the scheduled run skips.
- Over-applying fixes: twice I raised hopeless markets and had to revert. Raise only where it reaches >= $0.87 at the cap.
- Push only after the full suite is green. A red test was pushed once; it never synced.
- National or cross-region markets (popular vote, seat counts) move with every region's count. Don't treat them as "done" when one region is.
