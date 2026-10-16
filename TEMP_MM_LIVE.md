# Hourly-temp market maker, LIVE executor (KXTEMPMIAH)

Jack, 2026-10-15: "yes build F, which is a superset of G right?" This followed the shadow test (TEMP_SHADOW.md), where variant F_full netted about +$73 an hour and G was positive in every hour.

`temp_mm_live.py` quotes Kalshi's hourly Miami temperature markets with the same policy code the shadow replays (`temp_mm_policy.decide`). The shadow's numbers therefore describe the code that trades.

- **Variants:** it runs F by default. `--variant G` (decided strikes only, with the 180 s pad hold since 10/16) or `--variant J` (decided, favorite size 100) switches it, and so does any `temp_mm_policy.VARIANTS` key. `--variant G0` is G without the pad hold. `--variant FAV_only` quotes only G's favorite joins (no 1c filler of any kind, and no favorite join under 50c: `fav_min_price_c`) and `--variant FAV_night` does the same in the events settling 20:00-07:00 ET (Jack 2026-10-16, "1"; see Quote hours).
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

## Pad hold (10/16)

Jack, 2026-10-16: "hold the pads for the first 3 minutes of each hour going forward".

**Why.** G went live on subaccount 2 at 13:00Z on 10/16. In its first hour (26OCT1610), the pads went up at +152.9 s, when the programs appeared. Sweepers sold into them from +155.7 s: 4,650 1c contracts by +197 s (4,659 by +264 s), and positions hit the 1,500 cap on three strikes (`fills_2026-10-16.jsonl`).

**What it does.** The policy key is `pad_hold_open_s`: 180 in `G_decided_only`, and 0 (off) everywhere else.
- While the orders would start resting before the event's open + 180 s, no 1c longshot pad and no 1c qualification pad is placed.
- Favorite joins, live and near-decided quotes are unaffected; a near quote that sits at 1c stays.
- The open is `EventInfo.open_ts`, the markets' `open_time` (close − 3600 when unknown).
- The test is `temp_mm_policy.pad_hold_flag`, on the same resting start the shadow uses (now + 0.15 s), so the bot and the sim agree on the boundary.
- `TMM_PAD_HOLD_OPEN_S=<s>` overrides it; 0 turns it off.

**At the boundary:**
- The bot decides on the first wake past the edge (at most 50 ms late, as for the landing window), not on the next 250 ms tick. It logs `pad hold over` and a `pad_hold_end` row in `decisions_<day>.jsonl`.
- The pads are new creates, so they are never debounced. They are paced like any create: 1 s per market, and the write budget of 3/s with a burst of 6.
- 1c pads no longer count in the reprice debounce's price key, so a pad appearing (the hold ending, or a thin side's qualification pad) does not restart a favorite's 2 s wait.
- Nothing already resting is touched. The flag only turns off once per event: pads placed after the edge stay, and only the favorites' expiry renewals follow.
- `status.json` shows `pad_hold_until` per event.

**What the recording says** (the shadow's 35 recorded hours, 10/14 21:00 ET – 10/16 09:00 ET, `G` vs `G0_nohold`):

| ET day | hours | cons: G net | G0 net | G − G0 | opt: G net | G0 net | G − G0 |
|---|---|---|---|---|---|---|---|
| 10/14 (evening) | 3 | $175.74 | $189.66 | −$13.92 | $262.15 | $279.07 | −$16.92 |
| 10/15 | 22 | $1,688.31 | $1,822.18 | −$133.87 | $1,811.14 | $1,947.01 | −$135.87 |
| 10/16 (to 09:00 ET) | 10 | $1,552.36 | $1,417.36 | +$134.99 | $1,521.44 | $1,580.84 | −$59.40 |
| total | 35 | $3,416.40 | $3,429.20 | −$12.80 | $3,594.73 | $3,806.92 | −$212.19 |

- **The first 3 minutes:** the hold gives up $236 of reward and avoids 2,295 (cons) to 2,326 (opt) contracts of 1c fills, worth $23.
- **The rest of the hour:** +$201 in cons. On 10/16, G0's early sweep fills used up its position cap and took the front of the 1c queue. The opt model has no queue: +$0.85 there.
- **Sweep timing:** the 3-minute mark is already crowded. In 33 of the 35 hours, others add new 1c bids (typically 100 YES and 200 NO a strike, 2,000–21,000 contracts in all) between +179.5 s and +184.5 s. In 18 of those hours, a sweeper sells into fresh 1c bids at +181 to +186 s. A 180 s hold puts G's pads up in the same seconds, so that sweep hits them too. G's 1c fills in [+180.65 s, +240 s) are 11,454 against G0's 13,339 (cons), and 13,669 against 13,369 (opt).
- **Other hold lengths:**

| hold | cons net | opt net |
|---|---|---|
| none | $3,429 | $3,807 |
| 180 s | $3,416 | $3,595 |
| 185 s | $3,593 | $3,644 |
| 190 s | $3,583 | $3,648 |
| 240 s | $3,505 | $3,624 |
| 300 s | $3,378 | $3,546 |

These are other traders' habits over 35 hours, so they can change. The live hour 26OCT1610 is not in these numbers: it had not settled when they were run, and its recording holds the bot's own orders and fills.

## Quote hours (10/16)

Jack, 2026-10-16 ("1"): "ship the fixes + the sweep-aware report column, and add favorite-only and overnight-favorite-only to the shadow". Under the sweep-aware fill model (swp, TEMP_SHADOW.md) every variant that rests 1c fillers loses in expectation, because sweepers take the fillers within seconds, as they took live G's on 10/16. Only favorite joins without fillers are positive (about +$13 an hour, almost all of it in the events settling 20:00-07:00 ET).

**What it does.** The policy key is `quote_hours_et`: a list of ET clock hours, `None` (the default) for every hour.
- An event is quoted only when the ET hour of its SETTLEMENT is listed. The settlement is `EventInfo.settle_ts`, the ticker's hour: `KXTEMPMIAH-26OCT1607` settles at 07:00 ET, hour 7.
- Outside the hours `temp_mm_policy.decide()` fails every market's program test (`diag.reason` = `off_hours`), so nothing rests on that event and anything resting there is cancelled on the next decision. The shadow sim applies the same rule, so its numbers describe this behaviour.
- `FAV_night` = `FAV_only` + `quote_hours_et` `[20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7]`. `FAV_only` = live off, decided on, `q_pad` 0, `qual_pads` off, `near_decided_half` off, `fav_min_price_c` 50: the favorite joins of G and nothing under 50c. Without the floor a favorite join follows the reward reference down to a 1-5c pad when the favorite side's top holds fewer than target/5 = 200 contracts (the 10/16 DRY run amended a join 95c -> 79c -> 5c for about 5 s); with it that join is dropped and nothing rests on the market until the reference is back at 50c or more. `TMM_FAV_MIN_PRICE_C` sets it on any variant (default 0 = no floor).
- The ET hour is `temp_mm_policy.et_hour`, computed without tz files (the policy does no I/O). US Eastern is EDT from the second Sunday of March 07:00Z to the first Sunday of November 06:00Z. It agrees with `zoneinfo` hour by hour over 2024-2030 (a test). On the 2026-11-01 fall-back, the 07:00 event settles at 12:00Z (EST) and is quoted, while 08:00 (13:00Z) is not. The repeated 01:00 takes the ticker's first occurrence (05:00Z, `temp_shadow_common.parse_event` fold 0). The existing close_time-vs-ticker guard still refuses an event whose API close disagrees with its ticker's hour.
- `TMM_QUOTE_HOURS_ET="20,21,22,23,0,1,2,3,4,5,6,7"` (commas or spaces, brackets allowed) sets it on any variant; `all` (or `any`) means every hour. Unset or empty leaves the variant's own hours (FAV_night keeps 20-07). `none`, `null`, `[]` and an empty list refuse to start (`config error`): in the policy an empty list quotes no hour while `None` quotes every hour, so those spellings were ambiguous (before the 10/16 fix pass `[]` and `none` meant every hour, turning FAV_night into 24-hour quoting). Anything that is not a whole hour 0-23 also refuses to start. To quote nothing, do not start the bot.
- Visibility: the start line names the hours. An off-hours event is logged once (`... settles 09:00 ET, outside quote_hours_et [...]: not quoted`). It also gets guard `off_hours` (with the hour), `status.json` `quote_hours_et` plus per event `quote_hour`, and decision samples with `reason: off_hours` and `quote_hour: false`.

`test_temp_mm_live.TestQuoteHours` pins this on the fake clock: no write at all for the 09:00 ET event under FAV_night, the favorites only (no 1c) with the hour listed, a narrower list cancelling what rests, the env parsing, and settle hours from tickers across the Nov 1 switch.

## What it does

The bot is one process, event-driven:
- It wakes every 50 ms.
- It makes a full decision at least every 250 ms.
- It decides immediately on a new station row, a book change, a fill, or a landing-window edge.

| input | source |
|---|---|
| books and our fills | one WebSocket (`kalshi_ws.KalshiFeed`), signed with kalshi_reads' key (the IMM's account, which owns the subaccount). It covers the current and next event's 10 markets. Fills are matched by order id or by client_order_id prefix `tmm-`. |
| station readings | The shadow collector's wx stream, tailed read-only. If the collector is dead (heartbeat stale, or no row inside :13–:50 or for 90 s), it polls `/live_data/weather/miami?detailed=true&last_sec=420` itself, unsigned, on the collector's cadence: 1 s inside :13–:50, 5 s outside, 2 s for 2 min after a 429 (and backs off 2, 4 … 60 s). At start, when the collector's files do not reach back over the 75-min lookback (a restart after the collector stopped), one unsigned read of the history route (`…?detailed=true&from=…&to=…`) seeds the tape; it retries every 2 min while the history is short. |
| fair | `temp_shadow_model` WxTape plus FairModel, using the shadow's `model_params.json` and calibrations (or copies: `TMM_MODEL_PARAMS`, `TMM_CALIBRATIONS`), reloaded hourly. Rows are appended to the tape incrementally, so a new reading costs about 0.2 ms. The result is identical to a full rebuild, checked on live rows. The fair counts as stale (pads only) until the station history spans 60 min (`TMM_MIN_WX_SPAN_S`): trend, volatility and the drift anomaly need the hour. `--live` refuses to start on the built-in default model (params or calibrations missing), and the hourly reload keeps the loaded model if the file disappears (and emails). |
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
- **Order expiry:** every order carries `expiration_ts`: 90 s, or 300 s for 1c pads and qualification pads. An expiry-only amend renews it 25 s before it expires; only renewals send `expiration_time` (a reprice keeps the order's expiry, as the IMM's amends do). If Kalshi refuses the renewal (a 400), or the next resting read shows it kept the old expiry, the bot switches to cancel and re-place.
- **Urgent vs paced:**
  - Urgent changes go out at once, with several cancels batched into one request. A change is urgent if it cancels, cuts a size, or moves a price down from above the fair guard's cap (the fair fell under the order). The most exposed go first (price over the cap × size); renewals last.
  - Two or more guard-forced reprices in one decision (a station row moved the fair under several orders) go as **one batched cancel**, then post-only creates at the new prices, rather than one blocking amend after another.
  - A reprice that also cuts the size sends the cut at once (a decrease, or cancels of the other orders at the old price); only the move itself is paced.
  - An urgent reprice or a size cut that Kalshi refuses cancels the order instead.
  - Every other change is paced. It waits for `min_act_s` (1 s, the sim's tick) after the last change on that market, and for the write budget.
- **Reprice debounce:** a reprice UP (more aggressive) waits until the new price has held for 2 s (`TMM_REPRICE_DEBOUNCE_S`; 0 = the sim's instant repricing). A reprice down never waits: resting above the policy's price, alone at the top while the others back away, is where the shadow's P&L never took fills.
  - Why debounce at all: in the first dry run, favorites joining a reference that flickered by 1c made 395 amends in 21 min, 113 of them a bounce back within 3 s. A 1c flicker up now costs no write; a flicker down costs two (down at once, back up after 2 s).
  - New quotes, such as the :45.9 restore, are not debounced.
- **Landing-window pull:** the policy pulls live and near-decided quotes when their resting interval `[now + 0.15 s, + 1 s)` touches seconds :20–:45. The bot acts on the first wake past that edge, at about :18.9, so the pull reaches Kalshi before :20. Quotes come back at about :45.9.

## Risk limits and guards (all defaults conservative; env `TMM_<FIELD>`)

**Policy limits** (`temp_mm_policy`; F's values, as the shadow measured them):

| limit | default | env |
|---|---|---|
| position cap per market (net YES) | 1,500 contracts | `TMM_POS_CAP` |
| event worst-case loss of the FILLED positions across the nested strikes | $1,500 (variant K is $500) | `TMM_EVENT_WCL_CAP` |
| one event budget for what RESTS (opt-in; see below) | off (the shadow measured F without it) | `TMM_WCL_RESTING=1` |
| live size / skew / window | q_live 600, skew 0.3, pull :20–:45 | `TMM_Q_LIVE`, `TMM_SKEW`, `TMM_WINDOW_MODE`, … |

**What `event_wcl_cap` does and does not bound.** It sizes orders against the losses of positions already *filled*. With nothing filled, every strike may rest up to the cap on its own, so if only the losing side's resting orders fill (a jump through the strikes that lifts every NO bid, say), the event can lose more than the cap: on an 8-strike F book at 50 min to the close (fairs 99.5 … 1%), the adverse case is **$1,662** against the $1,500 cap (K: $1,569 against $500). The executor logs this number every decision (`resting_wcl` in the decision samples and in `status.json` per event). `TMM_WCL_RESTING=1` caps it: in every outcome, the filled loss plus every resting order that loses there stays under `event_wcl_cap`, by scaling down the orders that lose in the worst outcome (F's example: $1,497). It is live-only (the sim refuses it), so with it on the live book no longer matches the shadow's F. **The hard bound is the subaccount's cash**: every fill is paid from it, so the bot cannot lose more than it holds.

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
| order rejects in 5 min (4xx other than 404 / 409 / 429; post-only cross rejects excluded): email at 10, HALT at 40 | 10 / 40 | `TMM_ORDER_ERROR_ALERT`, `TMM_ORDER_ERROR_HALT` |
| write failures that are Kalshi's or the network's (429, 409 paused, 5xx, timeouts): no writes for 0.5 s, doubling to 8 s, resumed by the first success; never counted toward the HALT; email after 10 min | 0.5 → 8 s, 600 s | `TMM_BACKOFF_MIN_S`, `TMM_BACKOFF_MAX_S`, `TMM_UNHEALTHY_ALERT_S` |
| a rejected op is not resent for | 5 s | `TMM_OP_COOL_S` |
| HTTP timeouts: writes 2 s connect / 3 s read, one try; reader-thread reads 10 s, 2 tries; trading-thread reads 3 s, one try | | `TMM_WRITE_TIMEOUT_S`, `TMM_READ_TIMEOUT_S`, `TMM_READ_ATTEMPTS`, `TMM_SYNC_READ_TIMEOUT_S` |

**Guards:**

| condition | action |
|---|---|
| WebSocket unhealthy (no frame for 15 s) | cancel everything |
| one book untrusted (gap, no snapshot) | cancel that market |
| station data stale: no new station minute for 3 min (`TMM_STALE_WX_S`), or the newest minute older than 3 min + the ~330 s publication lag | live, near and favorite quotes pulled. Only 1c longshot pads and 1c qualification pads rest. |
| no model fair for a market (`TMM_REQUIRE_FAIR`) | no quotes there. The policy's book-mid fallback is never used live. |
| no program known for a market (`TMM_REQUIRE_PROGRAM`) | no quotes there |
| within `pad_hold_open_s` of the event's open (G: 180 s) | no 1c longshot or qualification pads; favorites, live and near quotes as usual |
| the event's settle hour (ET) not in `quote_hours_et` (FAV_night: 20-07) | nothing rests on that event; what rests is cancelled (guard `off_hours`, logged once) |
| within 1 s of the close | no quotes |
| the API's close_time is not the ticker's hour (a DST fall-back hour?) | that event is not quoted (logged once); the model would price the wrong hour |
| a market suspect of a missed fill (below) | no new orders there (cancels and cuts still go) |
| `KL-data\temp-mm\HALT` present | cancel all, idle; quoting resumes when the file is deleted. A live bot emails when it first sees one. A dry run obeys it too, but its own halts write `dry\HALT`, never this one. |
| Ctrl+C / SIGBREAK / SIGTERM / normal exit / atexit | cancel every tracked order, then a REST sweep of every resting `tmm-` order in the subaccount |
| startup (live) | cancel stray `tmm-` orders left by a previous run |

**Subaccount read-back:**
- The first live order is read back before any other is placed (nfl_snipe_bot's rule). `GET /portfolio/orders/{id}` takes no subaccount parameter, so it confirms only when it reports `subaccount_number == N`; when it reports none, the subaccount's own list (`GET /portfolio/orders?ticker=…&subaccount=N`) must show the order.
- Every later order is confirmed in a batched read of the subaccount's list (`GET /portfolio/orders?event_ticker=…&subaccount=N`) within about 10 s. An order missing from it is read back on its own.
- An order booked to another subaccount means HALT, and that order is cancelled **in the subaccount it booked to** (the primary: no parameter), read back, and named in the email if it could not be confirmed cancelled. So does a fill reporting another subaccount. An order that a *working* read cannot place within 60 s also halts (and is probed in the primary); while the confirm reads fail (an outage), new orders are held instead.

**Reconciliation (live):**
- **Resting orders, every 60 s:** a `tmm-` order the bot does not track is cancelled, and an order of ours that vanished is dropped (and its market made suspect). Non-`tmm-` orders are never touched. A create that got no answer (a timeout, or a 500 / 502 / 504) forces this read at once: if the order rests, the bot adopts it; until then that price is not re-sent. A cancel Kalshi answered as done (or not found) whose order still rests 10 s later means cancels are not landing: HALT.
- **Cancels:** only a 404 on a single cancel means "already gone". A batch item's `not_found` is retried as a single cancel first, and a 409 (Kalshi paused) leaves the order tracked.
- **Fills, every 120 s:** a REST sweep, deduplicated by trade id with the WebSocket fills. A fill is booked at the price it executed at (the fill's own price), not the order's current one.
- **Positions, every 5 min, for every event the bot placed orders in** (not only events it believes it holds).
- **Missed fills:** an order that vanished, a cancel / amend / decrease that finds it gone, Kalshi's fill count on an order running ahead of our fills (counted in the risk caps at once, at the order's price), or the WebSocket coming back after a drop make the market *suspect*: no new orders there until a fills sweep and a positions read taken after it agree with our fills (every 5 s). If Kalshi shows a position our fills do not explain, again after a fills sweep, the fills are not reaching the bot and its risk caps and loss halts are blind: **HALT**.
- **Settlement results** are read for every event the bot holds, from its positions, so an event that closed just before a restart still counts in the day's P&L; they are saved to `state.json` as soon as they arrive.

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

How much: $1,000–1,500 for F. The cash is the hard limit on what the bot can lose: every fill is paid from it. `event_wcl_cap` ($1,500) bounds an event's *filled* positions only; what rests can lose more if only the losing side fills (about $1,660 on a full 8-strike F book, see "Risk limits"), unless `TMM_WCL_RESTING=1`. Favorites at 98c cost nearly a dollar a contract to hold. G and J need less, about $500 (G's adverse resting case in the same example: $727).

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
- **Belt and braces:** `python temp_mm_live.py --live --subaccount N --cancel-all`. It works while the bot is still running (it takes no lock): it writes HALT first, so a running bot cancels and idles too, cancels every resting `tmm-` order in subaccount N, reads again (up to three rounds) and prints anything still resting. Delete HALT before starting the bot again.

**6. Take the cash back.**

```
python kalshi_subaccount.py withdraw N <dollars>
```

**Exit codes:** 0 done, 1 crash, 2 refused to start, 3 halted. The launcher restarts after 20 s unless the code is 2 or 3. After a halt, read the log, delete HALT, and start it again.

**What halts the bot** (cancel all, HALT, email, exit 3): the daily loss halt; an order or fill booked outside subaccount N; an order a working read cannot confirm in 60 s; a position Kalshi reports that our fills do not explain (after a fills sweep); an order still resting 10 s after Kalshi answered our cancel; 40 order rejects in 5 min. **What does not:** Kalshi maintenance, 429 throttling, 5xx, a Wi-Fi drop. Writes back off and resume by themselves (email after 10 min); what rests expires by its TTL meanwhile.

## Files

Live files go in `C:\Users\jackd\Documents\KL-data\temp-mm\`. A dry run writes the same set under `…\temp-mm\dry\`. Both modes obey the root HALT; a dry run's own halts write `dry\HALT`.

| file | what |
|---|---|
| `tmm_<UTC day>.log` | text log: every op (`[DRY] would …` in dry mode), fills, guards, halts, a summary line each minute (loop latency, REST counts, guards, day P&L), and a breakdown of any step slower than 200 ms |
| `orders_<UTC day>.jsonl` | `place` / `amend` / `renew` / `decrease` / `cancel` / `gone` / `expired` / `reject` (with Kalshi's error body) / `uncertain` / `adopt` (an uncertain create found resting) / `cancel_elsewhere` (a mis-booked order cancelled where it booked) / `*_error`, with mode, variant, ticker, side, price, size, order and client ids, tag, reason and latency |
| `fills_<UTC day>.jsonl` | every fill: source ws / rest, side, price, count, fee, order id, trade id, position after |
| `decisions_<UTC day>.jsonl` | one sample per market per minute (fair, slope, latest station minute, class, rule, book best and reference, desired orders and tags, resting, guards, position, event worst-case loss of the filled positions and `resting_wcl`, the adverse case of what rests, and whether the market is suspect), plus a `pull` / `restore` row at each landing-window edge with the second of the minute |
| `status.json` | heartbeat every 5 s: mode, variant, overrides, `quote_hours_et`, loop p50 / p95 / max, wx source and lag, feed, per event (orders, resting size by tag, P&L, guards, fairs, `quote_hour`), positions, day P&L, cash, guard / op / deferral counters, REST counts (last minute and total), write backoff and transient failures, suspect markets, uncertain creates, the confirm hold, and the model's sources and history span |
| `summary.jsonl` | one line per ET day: ops, fills, contracts, P&L, halted events |
| `state.json` | halted events and settlement results, so they survive a restart. Positions and day P&L are rebuilt from the fills logs and the REST fills sweep on start. |
| `launcher-<date>.log` | the launcher's own log |
| `HALT` | pause file |

## Comparing with the shadow

The shadow keeps simulating F, G (with the pad hold), G0_nohold, J, FAV_only and FAV_night on the same hours, under three fill models since 10/16: opt, cons and swp, the sweep-aware model that reproduced G's live hour. The daily report's headline is swp.

**Per hour:** compare `KL-data\temp-shadow\sim\<ET day>\<EVENT>.json`, variant `F_full|cons` and `|opt` (reward, contracts, trading P&L), with the live run:
- contracts and P&L at settlement come from `fills_<day>.jsonl` and `summary.jsonl`;
- rewards come from Kalshi's rewards statement (the subaccount's credits).

Live fills at prices above 1c should sit between the `cons` and `opt` fill models. Live 1c fills should track `swp` (`filler_contracts`, `sweep_src`): on 26OCT1610 swp predicted 9,271 against 10,006 live, while cons predicted 3,264. Live P&L per contract should track the sim's `markouts`.

**Decision parity:** run the sim's F pass on the same hour with fills off, and compare its desired orders with the executor's `decisions` samples at the same instants: class, price per side, size per side. This was done in the dry smoke run below, using a copy of the recording. On live, expect differences only where:
- live fills move the risk caps;
- the size tolerance (±3%) applies;
- the book moved between the recording's 1 s grid and the live decision;
- a reprice up is still held by the 2 s debounce.

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

## Second dry smoke, after the review fixes (2026-10-16 03:36–03:51Z, variant F, reads only)

15 min on 26OCT1600 (and 26OCT1601 listed ahead of its open), data dir in a scratch folder.

| measure | result |
|---|---|
| real orders | none. All 273 order rows are `dry`; the REST counters show reads only (20 markets, 5 programs; no `w_*`, no account reads). |
| decision parity with the sim's F (fills off, same instants, recording copied to scratch) | 140 of 140 samples identical (prices, and sizes within the ±3% tolerance); fairs within 0.02 in all 140. |
| executor parity (review f16) | 0 of 147 side samples rested above the policy's top price. Reprices: 17 down (none waited for the debounce; a falling favorite reference was followed step by step, 1 s apart), 20 up. |
| guard-forced moves batched (f17) | at 03:37:18 a station row moved the fair under T80.99 YES 92c and T81.99 YES 40c: one batched cancel and two post-only re-places at 80c / 10c. |
| expiry (f5) | 109 renewals carried an expiry; none of the 37 reprice amends did. |
| model (f18) | params and calibrations from the shadow's files (logged at start); history span 79–91 min (f19: no `short_history` guard). |
| resting worst case (f2) | `resting_wcl` up to **$1,739** on 26OCT1600 against the $1,500 `event_wcl_cap` (wcl_resting off): the adverse case is real on the live book. |
| writes it would have sent | 14.0 a minute (max 26) of creates, amends, renewals and decreases, plus 4.2 cancels a minute. |
| loop | p50 0.56 ms, p95 3.2 ms, max 37 ms; no step over 200 ms; reaction to a station row 32 ms (p50). |
| station feed | the collector's stream (tail lag p50 0.33 s), zero fallback polls. One history-route read at start that found nothing to seed (the span was computed from the snapshot, not yet built); fixed after the smoke (the span comes from the rows) and pinned by `test_f19_history_seeded…`. |
| errors | none (no `!` lines) |

## Open items to watch in the first live hour

No live write has ever been sent: the bot was only ever run DRY. The request shapes follow Kalshi's V2 docs (checked 10/15) and are pinned by `TestExchangeWire`:
- create: a body field `subaccount`;
- amend and decrease: query `?subaccount=N`;
- cancel: `?subaccount=N&exchange_index=-1&market_ticker=…`;
- batch cancel: a per-item `subaccount`.

| item | status | fallback / what to check |
|---|---|---|
| request shapes | unverified live | Watch the first orders in `orders_<day>.jsonl` for `reject` / `*_error` rows. The read-back proves where they booked. |
| batch cancel's per-item `subaccount` | unverified live | A `not_found` item is retried as a single cancel (`?subaccount=N`); an order still resting after a "successful" cancel halts the bot. Watch for `gone` rows right after `cancel` rows on orders that should have been resting. |
| expiry-only amend | The docs say an amend's `expiration_time` can be extended. The IMM's 8/02 comment says it could not. | After a 400, or a resting read that shows the old expiry, the bot switches to cancel and re-place automatically. Check `amend_expiry_ok` in status. |
| WebSocket fills for a numbered subaccount | unknown whether the fill channel delivers them | The REST fills sweep (every 2 min, and at once when a fill may have been missed) and the positions checks are the backstop. If neither delivers, the first unexplained position halts the bot. The source of each fill is logged. |
| `GET /portfolio/positions?subaccount=N` | assumed to list only subaccount N | If it ignores the parameter and the primary holds KXTEMPMIAH positions, the first check halts the bot (a false alarm, but on the safe side). |
| liquidity rewards for orders in a numbered subaccount | unconfirmed (INCENTIVE_MM_HANDOFF.md flagged this) | Check the first day's statement for subaccount N's credits before reading anything into the P&L. |
| IMM interaction | The IMM's `KXTEMP[A-Z]+H` block must stay. Self-trade prevention spans subaccounts, but the IMM never quotes these markets. | — |

## Tests

`test_temp_mm_live.py` uses no network. `TestQuoteHours` pins `quote_hours_et` (FAV_night) on the fake clock. `TestPadHold` pins the 10/16 pad hold: no pad or qualification creates before open + 180 s, both on the first wake after it, no churn afterwards, the event's `open_time` as the start, and no delay to a favorite's move. The harness is a fake exchange (whose cancels and resting reads reach only the named subaccount, as Kalshi's `subaccount` parameter does), fake feed, fake station rows and a fake fair drive `Bot.step()` on a fake clock. The `TestReview*` classes pin each fix of the 10/16 review (named `test_f<N>_…`): unseen fills never re-quoted and halting, Kalshi's fill count in the risk inputs, cancels that cannot reach or do not land, ambiguous creates adopted, refused urgent reprices cancelled, expiry only on renewals, size cuts with a reprice, `--cancel-all` under the lock, dry halts, 429 storms and outages without a halt, confirm holds, GetOrder without a subaccount, 409 not gone, fill prices, batched guard moves, the default-model refusal, history seeding and the 60-min span, the poller cadence, results after a restart, per-event pruning and the settle/close guard. It also covers:
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
