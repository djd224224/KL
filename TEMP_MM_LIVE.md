# Hourly-temp market maker, LIVE executor (KXTEMPMIAH)

Jack, 2026-10-15: "yes build F, which is a superset of G right?" This followed the shadow test (TEMP_SHADOW.md), where variant F_full netted about +$73 an hour and G was positive in every hour.

`temp_mm_live.py` quotes Kalshi's hourly Miami temperature markets with the same policy code the shadow replays (`temp_mm_policy.decide`). The shadow's numbers therefore describe the code that trades.

- **Variants:** it runs F by default. `--variant G` (decided strikes only) or `--variant J` (decided, favorite size 100) switches it, and so does any `temp_mm_policy.VARIANTS` key.
- **Policy switches:** every policy setting is an env var, for example `TMM_Q_LIVE=300` or `TMM_EVENT_WCL_CAP=500`.

**It is DRY by default.** It reads live data, logs `[DRY] would ...` for every create, amend, decrease and cancel, and keeps a dry book. It never sends an order unless started with `--live --subaccount N`.

**Jack's steps:** only Jack creates and funds the subaccount and starts it live. It is not a scheduled task.

## Is F a superset of G?

In structure, yes. Order for order, not quite.

- **Decided strikes** (fair under 3% or over 97%): F rests exactly G's orders.
- **Live strikes** (10–90%): only F quotes.
- **Near-decided strikes** (3–10% and 90–97%): both quote them as live at half size. F rests 300 a side, skewed inside 5–10% and 90–95%. G rests 150, unskewed.
- **Shared risk budget:** both share one event risk budget (`pos_cap`, `event_wcl_cap`), so F's live fills can trim decided orders G would have rested.

`test_temp_mm_policy.TestFSupersetOfG` pins all of this.

## What it does

The bot is one process, event-driven:
- It wakes every 50 ms.
- It makes a full decision at least every 250 ms.
- It decides immediately on a new station row, a book change, a fill, or a landing-window edge.

| input | source |
|---|---|
| books and our fills | one WebSocket (`kalshi_ws.KalshiFeed`), signed with kalshi_reads' key (the IMM's account, which owns the subaccount). It covers the current and next event's 10 markets. Fills are matched by order id or by client_order_id prefix `tmm-`. |
| station readings | The shadow collector's wx stream, tailed read-only. If the collector is dead (heartbeat stale, or no row inside :13–:50 or for 90 s), it polls `/live_data/weather/miami?detailed=true&last_sec=420` itself. That poll is unsigned, at most 1 request/s, and backs off 2, 4 … 60 s on a 429. |
| fair | `temp_shadow_model` WxTape plus FairModel, using the shadow's `model_params.json` and calibrations (reloaded hourly). Rows are appended to the tape incrementally, so a new reading costs about 0.2 ms. The result is identical to a full rebuild, checked on live rows. |
| markets | `GET /markets?series_ticker=KXTEMPMIAH&min_close_ts=…` every 45 s |
| programs | `GET /incentive_programs?status=active`, which early-stops after page 1 (1 read). Every 15 s in hh:00–06, when the hour's programs start (about 2–3 minutes after the open). Every 5 s while an open event has no program yet, for up to 5 min: in the dry run, discovery lagged the program start by about 30 s at the old 30 s cadence. Otherwise every 5 min. `upcoming` is not read: it never lists these programs. |

**Each decision, per event:**

1. Build the MarketStates. The book the policy sees is the WebSocket book minus our resting orders. In DRY mode the dry book is not subtracted, because it never reached the exchange; that matches the shadow's view.
2. Call `temp_mm_policy.decide`.
3. Apply the guards (below).
4. Turn the result into an order-level plan:

| change | how it is sent |
|---|---|
| new price | **Amended in place,** only to a price of the same kind (live/near, favorite, or 1c pads). A favorite that gives way to a 1c pad is a pull: it is cancelled at once. It is never amended through the last-read book, because amend has no `post_only`. If it would cross, the bot cancels and sends a `post_only` create instead. |
| smaller size | **Decrease** (`reduce_by`), newest order first. |
| larger size | **A new order** at the back of the queue (the sim's lot). With `TMM_INC_MODE=amend`, the existing order is amended up instead. |
| no longer wanted | **Cancel.** |

The plan also follows these rules:
- **Size changes:** same-price size changes under max(5 contracts, 3%) are skipped. Without this, the size taper sends a 1-contract decrease every few seconds. `TMM_SIZE_TOL_ABS=0 TMM_SIZE_TOL_FRAC=0` restores the sim's exact sizes.
- **Order expiry:** every order carries `expiration_ts`: 90 s, or 300 s for 1c pads and qualification pads. An expiry-only amend renews it 25 s before it expires. If Kalshi refuses that amend, the bot falls back to cancel and re-place.
- **Urgent vs paced:**
  - Urgent changes go out at once, with several cancels batched into one request. A change is urgent if it cancels, cuts a size, or moves a price down from above the fair guard's cap (the fair fell under the order).
  - Every other change is paced. It waits for `min_act_s` (1 s, the sim's tick) after the last change on that market, and for the write budget.
- **Reprice debounce:** a reprice the guard does not force waits until the new price has held for 2 s (`TMM_REPRICE_DEBOUNCE_S`; 0 = the sim's instant repricing).
  - Why: in the first dry run, favorites joining a reference that flickered by 1c made 395 amends in 21 min, 113 of them a bounce back within 3 s. A flicker now costs no write.
  - New quotes, such as the :45.9 restore, are not debounced.
- **Landing-window pull:** the policy pulls live and near-decided quotes when their resting interval `[now + 0.15 s, + 1 s)` touches seconds :20–:45. The bot acts on the first wake past that edge, at about :18.9, so the pull reaches Kalshi before :20. Quotes come back at about :45.9.

## Risk limits and guards (all defaults conservative; env `TMM_<FIELD>`)

**Policy limits** (`temp_mm_policy`; F's values, as the shadow measured them):

| limit | default | env |
|---|---|---|
| position cap per market (net YES) | 1,500 contracts | `TMM_POS_CAP` |
| event worst-case loss across the nested strikes, filled in full | $1,500 (variant K is $500) | `TMM_EVENT_WCL_CAP` |
| live size / skew / window | q_live 600, skew 0.3, pull :20–:45 | `TMM_Q_LIVE`, `TMM_SKEW`, `TMM_WINDOW_MODE`, … |

**Executor limits:**

| limit | default | env |
|---|---|---|
| event loss halt (realized + MTM at the external mid, else the fair): cancel every order on the event, pads included, for the rest of the hour; email | $200 | `TMM_EVENT_LOSS_HALT` |
| daily loss halt (ET day, realized + MTM): cancel all, write HALT, email, exit 3 (the launcher stops) | $300 | `TMM_DAILY_LOSS_HALT` |
| max contracts per order | 1,100 | `TMM_MAX_CONTRACTS_ORDER` |
| max new orders per minute | 60 | `TMM_MAX_NEW_ORDERS_MIN` |
| max writes per minute (creates + amends + decreases; cancels never capped) | 180 | `TMM_MAX_WRITES_MIN` |
| paced write rate / burst | 3/s, burst 6 | `TMM_WRITE_RATE`, `TMM_WRITE_BURST` |
| signed REST reads | ≤ 1/s average, burst 5 | `TMM_READ_RATE`, `TMM_READ_BURST` |
| cash: live refuses to start on a $0 subaccount; an order the cash cannot fund (balance − fills since the last read − reserve) is trimmed or skipped. Resting orders reserve nothing, so each order must be fundable alone. | reserve $0 | `TMM_CASH_RESERVE` |
| order TTL (the backstop if the process dies) | 90 s; pads 300 s | `TMM_ORDER_TTL_S`, `TMM_PAD_TTL_S` |
| order errors in 5 min (post-only cross rejects excluded): email at 10, HALT at 40 | 10 / 40 | `TMM_ORDER_ERROR_ALERT`, `TMM_ORDER_ERROR_HALT` |

**Guards:**

| condition | action |
|---|---|
| WebSocket unhealthy (no frame for 15 s) | cancel everything |
| one book untrusted (gap, no snapshot) | cancel that market |
| station data stale: no new station minute for 3 min (`TMM_STALE_WX_S`), or the newest minute older than 3 min + the ~330 s publication lag | live, near and favorite quotes pulled. Only 1c longshot pads and 1c qualification pads rest. |
| no model fair for a market (`TMM_REQUIRE_FAIR`) | no quotes there. The policy's book-mid fallback is never used live. |
| no program known for a market (`TMM_REQUIRE_PROGRAM`) | no quotes there |
| within 1 s of the close | no quotes |
| `KL-data\temp-mm\HALT` present | cancel all, idle; quoting resumes when the file is deleted |
| Ctrl+C / SIGBREAK / SIGTERM / normal exit / atexit | cancel every tracked order, then a REST sweep of every resting `tmm-` order in the subaccount |
| startup (live) | cancel stray `tmm-` orders left by a previous run |

**Subaccount read-back:**
- The first live order is read back before any other is placed (nfl_snipe_bot's rule).
- Every later order is confirmed in a batched read (`GET /portfolio/orders?event_ticker=…&subaccount=N`) within about 10 s. An order missing from it is read back on its own.
- An order booked to another subaccount means HALT. So does a fill reporting another subaccount, or an order that cannot be confirmed within 60 s.

**Reconciliation (live):**
- **Resting orders, every 60 s:** a `tmm-` order the bot does not track is cancelled, and an order of ours that vanished is dropped. Non-`tmm-` orders are never touched.
- **Fills, every 120 s:** a REST sweep, deduplicated by trade id with the WebSocket fills.
- **Positions, every 5 min:** a mismatch seen twice halts the event.

## Jack's commands

All commands run from `C:\Users\jackd\Documents\KL`, after this branch is merged. The shadow collector must keep running: its wx stream is the bot's station feed.

**1. Create the subaccount.** It prints its number N. Subaccount 1 is the NFL sniper's.

```
python kalshi_subaccount.py create
```

**2. Fund it.** This moves cash from the primary to N on shard 0, where KXTEMPMIAH trades.

```
python kalshi_subaccount.py fund N 1500
```

How much: $1,000–1,500 for F. Its worst-case loss is capped at $1,500 an event, and favorites at 98c cost nearly a dollar a contract to hold. G and J need less, about $500.

**3. Dry run against it.** This reads the subaccount's balance only. It places nothing.

```
python temp_mm_live.py --subaccount N --duration 900
```

**4. Start live.** Run it detached through the launcher:

```
Start-Process powershell -WindowStyle Hidden -ArgumentList '-ExecutionPolicy','Bypass','-File','C:\Users\jackd\Documents\KL\run_temp_mm.ps1','-Live','-Subaccount','N','-Variant','F'
```

For G, pass `-Variant G`. Limits can be overridden first, for example `$env:TMM_EVENT_WCL_CAP = '500'` in that shell or as a user env var.

**5. Stop.**
- **Pause:** `New-Item C:\Users\jackd\Documents\KL-data\temp-mm\HALT`. The bot cancels everything and idles; delete the file to resume.
- **Full stop:** end the `run_temp_mm.ps1` powershell and its python. A normal exit cancels every `tmm-` order; if the process is killed, the TTL (90 s / 300 s) clears them.
- **Belt and braces:** `python temp_mm_live.py --live --subaccount N --cancel-all`

**6. Take the cash back.**

```
python kalshi_subaccount.py withdraw N <dollars>
```

**Exit codes:** 0 done, 1 crash, 2 refused to start, 3 halted. The launcher restarts after 20 s unless the code is 2 or 3. After a halt, read the log, delete HALT, and start it again.

## Files

Live files go in `C:\Users\jackd\Documents\KL-data\temp-mm\`. A dry run writes the same set under `…\temp-mm\dry\`. The HALT file is shared by both modes.

| file | what |
|---|---|
| `tmm_<UTC day>.log` | text log: every op (`[DRY] would …` in dry mode), fills, guards, halts, a summary line each minute (loop latency, REST counts, guards, day P&L), and a breakdown of any step slower than 200 ms |
| `orders_<UTC day>.jsonl` | `place` / `amend` / `renew` / `decrease` / `cancel` / `gone` / `expired` / `reject` (with Kalshi's error body) / `uncertain` / `*_error`, with mode, variant, ticker, side, price, size, order and client ids, tag, reason and latency |
| `fills_<UTC day>.jsonl` | every fill: source ws / rest, side, price, count, fee, order id, trade id, position after |
| `decisions_<UTC day>.jsonl` | one sample per market per minute (fair, slope, latest station minute, class, rule, book best and reference, desired orders and tags, resting, guards, position, event worst-case loss), plus a `pull` / `restore` row at each landing-window edge with the second of the minute |
| `status.json` | heartbeat every 5 s: mode, variant, overrides, loop p50 / p95 / max, wx source and lag, feed, per event (orders, resting size by tag, P&L, guards, fairs), positions, day P&L, cash, guard / op / deferral counters, REST counts (last minute and total) |
| `summary.jsonl` | one line per ET day: ops, fills, contracts, P&L, halted events |
| `state.json` | halted events and settlement results, so they survive a restart. Positions and day P&L are rebuilt from the fills logs and the REST fills sweep on start. |
| `launcher-<date>.log` | the launcher's own log |
| `HALT` | pause file |

## Comparing with the shadow

The shadow keeps simulating F, G and J on the same hours.

**Per hour:** compare `KL-data\temp-shadow\sim\<ET day>\<EVENT>.json`, variant `F_full|cons` and `|opt` (reward, contracts, trading P&L), with the live run:
- contracts and P&L at settlement come from `fills_<day>.jsonl` and `summary.jsonl`;
- rewards come from Kalshi's rewards statement (the subaccount's credits).

Live fills should sit between the `cons` and `opt` fill models. Live P&L per contract should track the sim's `markouts`.

**Decision parity:** run the sim's F pass on the same hour with fills off, and compare its desired orders with the executor's `decisions` samples at the same instants: class, price per side, size per side. This was done in the dry smoke run below, using a copy of the recording. On live, expect differences only where:
- live fills move the risk caps;
- the size tolerance (±3%) applies;
- the book moved between the recording's 1 s grid and the live decision.

## Dry-run smoke (2026-10-16 01:52–02:36Z, variant F, reads only)

There were three runs against the live market:
- 21 min spanning the 02:00Z hour boundary: 26OCT1522 closing and 26OCT1523 opening;
- 10 min;
- 12 min on the final code.

| measure | result |
|---|---|
| real orders | none. All 1,385 order rows are `dry`, and the REST counters show reads only (no `w_*`). The shadow's data dir was only read. |
| decision parity with the sim's F (fills off, same instants, recording copied to scratch) | 26OCT1522: 69 of 70 in-scope samples identical. 26OCT1523: 219 of 220 (runs 1–2) and 106 of 110 (run 3). The differences are 1–3c reference-price moments between the recording's 1 s grid and the live book. Fairs agreed within 0.02 in all 450 samples. Out of scope: startup before the WebSocket connected, after the close, and about 30 s of program-discovery lag at 02:02 (since fixed by the 5 s eager poll). |
| landing-window pull / restore (run 3, every minute) | pulls at :18.85–:18.90 (3–6 live/near orders), restores at :45.85–:45.91 |
| loop | p50 0.4 ms, p95 2.4 ms, max 37 ms (run 3); no step over 200 ms |
| reaction to a station row | 35 ms (p50) from reading the row to the decision done. The collector's stream reached us 0.38 s (p50) / 0.53 s (max) after the collector received it. |
| hour boundary | All orders came off at the close (the `closed` guard). 26OCT1523 was quoted from 02:02:34, when its programs appeared. |
| writes it would have sent (run 3) | 17.8 a minute (max 26) of creates, amends and renewals, plus 6 cancels a minute. In run 1, before the reprice debounce, amends alone ran at 18.4 a minute. |
| REST | about 1.7 signed reads a minute (markets, programs). No account reads in dry mode without a subaccount. Zero fallback station polls in runs 2–3. |
| errors | none |

## Open items to watch in the first live hour

No live write has ever been sent: the bot was only ever run DRY. The request shapes follow Kalshi's V2 docs (checked 10/15) and are pinned by `TestExchangeWire`:
- create: a body field `subaccount`;
- amend and decrease: query `?subaccount=N`;
- cancel: `?subaccount=N&exchange_index=-1&market_ticker=…`;
- batch cancel: a per-item `subaccount`.

| item | status | fallback / what to check |
|---|---|---|
| request shapes | unverified live | Watch the first orders in `orders_<day>.jsonl` for `reject` / `*_error` rows. The read-back proves where they booked. |
| expiry-only amend | The docs say an amend's `expiration_time` can be extended. The IMM's 8/02 comment says it could not. | After the first 400, the bot switches to cancel and re-place automatically. Check `amend_expiry_ok` in status. |
| WebSocket fills for a numbered subaccount | unknown whether the fill channel delivers them | The REST fills sweep (every 2 min) and the positions check (every 5 min) are the backstop. The source of each fill is logged. |
| liquidity rewards for orders in a numbered subaccount | unconfirmed (INCENTIVE_MM_HANDOFF.md flagged this) | Check the first day's statement for subaccount N's credits before reading anything into the P&L. |
| IMM interaction | The IMM's `KXTEMP[A-Z]+H` block must stay. Self-trade prevention spans subaccounts, but the IMM never quotes these markets. | — |

## Tests

`test_temp_mm_live.py` uses no network: a fake exchange, fake feed, fake station rows and a fake fair drive `Bot.step()` on a fake clock. It covers:
- the order plan: create, cancel, decrease, increase, amend in place, the crossing fallback, TTL renewal and the size tolerance;
- dry mode placing nothing;
- live refused without `--subaccount`;
- the first-order read-back and the subaccount-mismatch HALT;
- batched confirmation and its timeout;
- landing-window pull and restore timing;
- the expiry amend and its fallback;
- fills, positions and risk trims;
- the event and daily loss halts;
- cash refusal and trims;
- shutdown and startup sweeps;
- stray and gone reconciliation;
- error alerts and halts;
- position-mismatch halts;
- fills restored after a restart;
- every guard: WS unhealthy, untrusted book, stale wx, no fair, no program, before the program, after the close, HALT file;
- write pacing and caps;
- the Exchange wire shapes;
- the wx tail: partial lines and the hour roll;
- the fallback poller with 429 backoff;
- collector-heartbeat staleness;
- the real FairEngine, including its staleness.

Run each file in its own process: `PYTHONIOENCODING=utf-8 python -m unittest test_temp_mm_live -v`.
