---
name: market-wave
description: Executable runbook for quoting a wave of newly incentivized Kalshi markets with the IMM (elections, politics, awards, the "after economic events" metals / FX / crypto ladders, any series) without getting picked off, and for sizing a wave over the $1 payout floor. End to end: detect the new programs (wave_detect.py), choose scope by fixed rules, pass the gates (pvd_waves.py preflight: guards, capacity, shard cash, scan budget), list on the default preset (add + run), start the dashboard, verify quoting (whydark), watch pick-offs, then report every parameter and ask what to tune. Use when a wave or batch of programs launches, Jack names series to quote fast, he invokes /market-wave, or the 24/7 wave check reports a wave. Also for a live wave's follow-ups: pacing under $1, "size them up", stopping markets under $1, why a market isn't quoting, "am I getting baited", pick-off / dark / relight alerts, size-ups by cost bucket, new program periods, switch files and the post-mortem.
---

# Market wave runbook

What taught it (details and numbers: `history.md` beside this file):

- **11/04 post-vote election wave** and **11/06 2028 election wave** (1,827 programs; ~1,880 markets; first quotes 25 min after the ask).
- **11/07-11/08 pick-offs** (Jack: "the pickoffs yesterday were unacceptable"): baited thin books took ~$2.9k on the House night; 53 adding fills ~$4.3k that day. Section 1.
- **Two auto-listed overnight batches turned off** (w1107-mixed, w1108-mixed): the 5+-event rule, the info-risk screen, the gates.
- **11/11 2028 relight (w1111-elec2028):** 1 of 724 relit markets quoted under the unwind-only lock while the texts said "greenlit". Fixed by stamping, the dark check and registry waves (section 14).
- **11/11 Senate/Gov nominees (w1111-statenom):** auto-listed; 440 of 654 dark at first (not election-family members); the rate fell ~$4.4k -> ~$0.75k/day in a day as other makers crowded in; Jack's six size-ups by cost bucket; the 11/12 cross-event sweep (18 events, -$256 at marks), not bait -> the group breaker.
- **11/13 FX + metals "after economic events" (w1113-fxmetals):** quoted only through the weekend program; no fair at first (84 of 1,002 quotes 5c+ on the wrong side); ticker dates misread; tails screened out; the 8,000-order read doubled 3,462 sides; 76% under the $1 floor until sized up to 5x. Sections 11-12.
- **11/14 crypto after-events strikes (w1114-cryptoev):** quoting around the crypto fair; listed strikes had to behave like wave books; the size model overpromised (45 of 108); bank early at 1,500 a side took 230 -> 469 of 498 strikes to $1+. Sections 11-12.

Background reading, only when time allows: memories `jack-working-style`, `imm-quoting-triage`, `market-wave-playbook`. Lookup tables (tools, list fields, bot knobs, deploy safety, gotchas): `reference.md` beside this file.

---

## 0. Invocation contract (read this first)

**Speed matters, but nothing lists without passing the gates.** Jack's standing direction: "just start quoting them all first, to start earning. and then let me know what adjustments you want to make". So: quote everything that passes section 1's rules and `preflight`, at the safe default preset, right away, then report. On a short program window add his 11/14 rule: "quoting time is valuable given the incentive window is so short, so prioritize quoting time".

| Step | Target time | Output |
|---|---|---|
| 1. Detect and size up | <= 5 min | `wave_detect.py` table + JSON |
| 2. Choose scope | <= 2 min | series in / out with reasons (FIT / NEAR / FEW / CHECK / SOON / MEMBER), and the wave type (0.1) |
| 3. Preflight (the gate) | <= 1 min | `pvd_waves.py preflight ...`: exit 0, or stop and tell Jack why |
| 4. List and tune | <= 10 min | `pvd_waves.py add` + `run --write` |
| 5. Dashboard | <= 1 min | `pvd_waves.py dash` -> URL |
| 6. Verify the bot is quoting | <= 10 min | reload + selection + `pvd_waves.py whydark NAME` |
| 7. Report and ask | right after | parameters, guards, numbers (`pace`), tune menu, "Want to tune any of these?" |
| 8. Watch | every tune / 15 min | `pvd_waves.py pickoffs` (the relight task's pass 8 runs it with `--act`) |

Keep Jack posted with one line per step. Before the first tool call, say what lit and that you're deploying.

### 0.1 Wave types (pick first; they take different steps)

| Type | Looks like | Steps | Sizing | Seen |
|---|---|---|---|---|
| **A. Long-dated** (elections, politics, awards, entertainment) | programs of a week+, outcomes weeks to months out (NEAR, 1-14 d, is its own group), many events a series | 0-11, 13-15 | default preset; size-ups are Jack's word, offered after a clean day (he often asks sooner: then say how clean it has been) | w1111-elec2028, w1111-statenom |
| **B. Short-window live-price ladders** | "after economic events" `KX*EVENT` strikes (metals, FX, crypto, yields): 1-2 day programs, thousands of strikes, settle on a feed price at one minute | 0-4, then 12 (the fair and dates BEFORE listing), then 5-11 and 13-15 | a fair is required before anything lists; bank early; the floor protocol | w1113-fxmetals, w1114-cryptoev |
| **C. New period of a registry wave** (a relight) | the same series' programs restart | nothing by hand: pass 7 relists and retunes (section 14) | inherits the registry's CURRENT args -- including any size-up Jack made; say so in the report | 2028 relight 11/11 |
| **D. Hand-named one-off** | Jack names a series or market | 0-8 on his word (it overrides FEW / CHECK; report the verdict anyway) | default | w1109-control |

Auto mode lists the detector's FIT / NEAR series with 5+ events (type A, its NEAR group included) and nothing else. Type B never auto-lists: since 2026-11-14 the detector sends every live-price ladder (`KX<asset>EVENT` in Commodities / Financials / Crypto) to CHECK, so it alerts as needs-you. On 11/13 the metals came back FIT (Commodities) and only the auto run's own judgment kept them off a no-fair default listing.

**What the skill may do on its own (manual or auto mode):**
- List FIT series on the `default` preset, and NEAR series as the wave's NEAR group (the 11/04 midterm treatment).
- Start the dashboard, run the tune, verify, run the pick-off watch.
- Stand a picked-off REGISTRY-wave market down at its base size (`pickoffs --act`), and turn off a wave it just added that didn't go live (`pvd_waves.py stop`).
- Exclude SOON / CHECK / MEMBER / FEW series, and say so.
- Run the read-only `pace`, `whydark`, `darkcheck`, `report`, and `resize` WITHOUT `--apply`, any time.
- **The $1-floor protocol (section 11) on a type B wave with a live fair, in MANUAL mode** (Jack 2026-11-14: "Yes, pre-approve"): run it without asking first, then report what it did. Bank early up to 5x the base (`setarg NAME bank-size` / `bank-max`, e.g. 1,500 on a 300 base) until $1.30 is banked; `resize --apply` up to 5x aiming at $1.20; off under $1 only on two reads. Only while the fair is live (FX / metals: a fresh `--fair-file` from `fair_cmd`; crypto strikes: `crypto_event_fair`) and preflight passes. Never in auto mode, never above 5x, never on a market Jack turned off, never on a wave without a fair.

**What needs Jack's yes** (offer it in the report; never do it on your own):
- `--no-halts` or `--halts-on`.
- Any size-up: `--size-mult`, `--series-tier`, `--cheap-tiers` above the default (mid-price tiers included), a bank size above 300, a pin above base, `resize --apply` -- except the $1-floor protocol on a type B wave with a live fair, pre-approved 11/14 (above).
- The `2028` preset.
- Extra budget above +$100k (auto mode: never above +$50k).
- Quoting SOON, CHECK or FEW series, an info-risk title, or any type B wave.
- Lifting a market he turned off ("Keep them off", FX 11/14: don't re-offer the lift for that wave).
- Removing a pick-off stand-down (a market pin or a series pin).
- Editing `run_pvd_scan_waves.cmd` (pass 6's args, e.g. a stand-down pin for a pass-6 market) or another wave's settings.
- Creating, deleting or renaming a switch file (HALT, UNWIND_ONLY*). Section 13.
- Launcher changes (resting-order cap, placement cap, collateral budget): each is a restart.
- Bot code changes, schedules (the 24/7 check's prompt included), money (shard transfers: give him the command), commits.

**Auto mode** (started by the scheduled check `market-wave-overnight-check`, now 24/7; nobody watching):
- Same steps, plus these rules:
  - `preflight --auto` must exit 0 before anything is written.
  - The add is `add --auto --detect ... --budget 50000`, which re-runs the gates (capacity and shard cash included) and writes nothing on a FAIL.
  - If the bot doesn't reload and place within ~10 min, run `pvd_waves.py stop NAME`.
- **Alerts go through `wave_alert.py`, never by hand.** Jack 2026-11-08: "make sure my texts / email alerts on market waves are clear: X # of events greenlit or denied, reasoning, 10 names".
  - Line 1: what happened, plus events greenlit / denied. Then "Greenlit because" / "Denied because", then 10 event names.
  - Commands:
    - `wave_alert.py wave --detect ... --outcome live|not-live|needs-you|error [--wave NAME --listed N --est-day X --report PATH | --why-not "..."] --send`
    - `wave_alert.py relight --relight ... --send`
    - `wave_alert.py pickoff --pickoffs ... --send`
  - Without `--send` it only prints a preview.
- **Texts and email are the only routes that reliably reach Jack.** A PushNotification from a scheduled run never reached his phone. Send the PUSH line anyway.
  - Texts go through `sms_alert.py --long` (<= 900 chars), capped 1 per 10 min and 10 a day (exit 2 = capped, fine).
  - It texts for: every quotable wave (exit 3), live or not; a relight of 100+ markets or a 1,000+ program jump; a pick-off of $100+ new, a series trip or a stand-down; dark-check and bait-watch alerts (section 14).
  - It emails Jack's inbox (wave_alert.py's ALERT_TO) on every trigger.
- Jack can answer by text (task "KL sms-inbox" / `phone_actions.py`): pause-overnight writes `run-logs/pvd-scan/OVERNIGHT_PAUSED` and the check stops on its own. Never delete that file.
- Then end. Jack tunes later in a normal session.

---

## 1. Pick-off rules (read every time)

### 1.1 What happened (numbers: `history.md`)

- **11/07-11/08:** every wave fill since 11/04 netted -$4,094 (losing fills -$6,033). Baits on thin books (a lone ~200-lot 9-37c off fair, our rung re-anchored to it, a taker of exactly our size, the bait pulled) cost -$3,653 in 19 fills; real repricing -$550; a cross-market sweep of 90c+ sides -$352; a halt-restart rejoin -$89; a one-event auto-listing -$26. Nothing alerted; the halts were not the bait defense.
- **11/12 (w1111-statenom), a sweep, not bait:** one seller took the whole top bid level from every maker in 18 KXSENATENOM markets in 21 min (both parties' favorites, 32-91c). Our 17 full fills + 2,071 @ 9.7c (39% of that level after the 6-15c size-up): -$130 at marks, -$256 by evening. The per-event sweep breaker can't stop a taker that moves to the next event -> the group breaker.
- **11/13-11/14 (type B):** with no fair, 84 of 1,002 FX/metals quotes sat 5c+ on the wrong side (if all filled: -$1,602), and the pick-off watch marked a -$12 fill +$56 off a 26x95 book. The 8,000-order read under a 12,000 cap left 3,462 sides doubled for ~2 h (2 doubled fills, -$34). Crypto: 0 fills.

### 1.2 The defense stack (live)

| Layer | Where | What it does | Evidence |
|---|---|---|---|
| Close clamp | bot, 340fbe9 + 3032ea8 | The side that REDUCES a position rests no more than 5c past its 6 h usual price toward the taker, or at breakeven or better where that reaches further (Jack 11/08) | Every reducing-side bait fill of 11/07 to 0 |
| Wave unwind | bot, 85a5d8e + 95eef3b | While UNWIND_ONLY_WAVES exists, a HELD market still on the wave list rests one closing order for the whole position at the outside price: never better than the best outside 20+ lot (no lead), at most 5c past its usual price toward the taker or at cost where that's further (the close clamp's bound: no bait), at cost while it has no usual price; its adding side rests 0. A market moved off the list (`stop`, "stopped") is not unwound: its position rides. Jack 11/12: "unwind, doesnt have to be at breakeven but dont lead", "and make sure unwind positions also dont get baited". Closing orders ride through restarts (11/13: "Yes, keep them"). Back out: `IMM_UNWIND_WAVES_AT_COST=1` | What-if at deploy: -$3.0k realized if all filled, already marked |
| Off-usual size | bot, a0e19ff | A listed side resting 3c+ toward the taker past its 6 h median EXTERNAL touch rests only base size; cold start (under 4 samples, ~1 h) base too (crypto strikes 400, 1f68841). It also holds Jack's size-ups at base while other makers bid the prices up (statenom 11/12: 282 of 365 sides) | $3,041 of the House-night losses |
| Re-anchor cap | tune, every 15 min | Toward the taker a limit moves <= 5c a run, held a run after a 10c+ jump or an empty side | $3,003 |
| Join wait + rest memory | bot, 4903ecb + a0e19ff | Joining a better price waits 2 s per $ at risk ($50 free), penny and 0.1c books; a side back after a halt starts from its last rest | $1,160-2,386; 11/12: 112 better prices pulled before we joined |
| Lone-ref guard (waves) | bot, 3032ea8 | A listed side whose reference is a lone thin top (< 3 x target/5, 20c+ off the rest) our rung jumped to stands aside. At go-live after darkness every single-maker top counts as a jump: 37 KXPRESSTATE sides (11/11), ~500 statenom sides at its listing and ~155 by the afternoon -- expected, leave it | Logged before every House bait |
| Twin check | bot, 6fbad53 | Jack 11/11: "make sure the quotes on each side make sense relative to each other". A bid rests at most 100 - the siblings' bids (+2c); a D/R ask at least the other leg's NO bid (-2c); re-priced, never cancelled. Treats a 2-candidate nominee event as a pair (asks move up only) | $736 of the 2028 losers; 0 re-prices when legs agree |
| Twin block | tune | A D/R pair's same-bet leg rests nothing once one leg holds 200+ | |
| Group breaker | bot, 76d9201 + 25cbd54 | The 2nd fill of 200+ on the same side in a DIFFERENT event of one group within 10 min holds that side of the whole group 20 min (Jack 11/12-11/13: "after the 3rd fill and hold for 20min", then "on the 2nd fill"). `IMM_SWEEP_GROUP_MIN_EVENTS=3` backs it out | The 11/12 sweep: ~$117 of -$130 avoided, ~$10 of rewards a trip |
| Wave breaker + 200+ tape trigger | bot | Two 200+ fills at 90c+ in 10 min hold 90c+ rungs 15 min -- NO-HALTS series only, so it misses default-preset waves | $207 on 11/07 |
| Sweep breaker, tape trigger, toxic halts, bait-fingerprint stop | bot | Pull the event or side after a cleared order, a 100+ burst, two pick-offs, or a whole-rung bait fill | Per event only |
| Fair limits (type B) | tune `--fair-file` / bot `crypto_event_fair` | Bids <= fair - margin, asks >= fair + margin; stale or no fair = floor sides only (bid <= 2c, ask >= 98c) | 0 of 650 FX quotes wrong-side after |
| Full-earn size | bot, 44ffefd | Cuts a listed side to the smallest size keeping 95% of its share | Small |
| Pick-off watch | `pvd_waves.py pickoffs`, pass 8 | Adding fills marked to the book with OUR orders out; 5c+ and $25+ = flagged; 3+ in a series = series trip; `--act` pins at base | Texts at $100+ |
| Bait watch | `bait_watch.py`, pass 10 | Texts a bait-fingerprint stop or a 3rd sweep trip in 30 min; emails toxic halts, 2nd sweeps, flashes | 11/11-11/14: 0 texts |
| Heightened bait mode | bot, b41c0d4, DRY | Logs "pulls" (10c+ better, stood 10 s+, our rung followed, then pulled) and "strikes"; protective actions not built; score after 7 days (`imm_heightened_report.py`) | Narrowed 11/14 from ~18,500 trips/day |
| Scope rules | detector + bot | 5+ events a series; info-risk titles -> CHECK; company KPIs frozen | w1107/w1108 would not have listed |
| P&L halts | bot | Daily loss -$8,000 (P&L today marked without our own orders or fills); net halt off; account-value drop -$2,000 | Real prices since 0393404 |

### 1.3 The rules

1. **No listing without `preflight` exit 0** (section 4). Auto mode: `add --auto` enforces it.
2. **Scope:**
   - 5+ events in the series (Jack: "market WAVES autoquoting overnight should only enroll WAVES, meaning 5+ more events in the same family").
   - FIT or NEAR only; no company KPIs; nothing on the blocklist.
   - Info-risk titles are Jack's call: personnel, appearances / announcements, counts of reported events, pre-taped TV, company reports.
   - Ask "what moves this price, and can someone see it before our resting order moves?" "Truly far" means the OUTCOME date, not Kalshi's settle date.
   - Live-price ladders (type B) only with a fair, and only while their market is shut (FX, metals) or the fair is live (crypto).
3. **Sizes:**
   - The default preset multiplies only sides costing <= 5c: <= 2c x10, <= 5c x5. 0 fills at 1-2c in the first ~28 h of the 2028 wave; the pick-offs sat at 12-80c.
   - Mid-price tiers, `--size-mult`, series tiers and `resize --apply` are Jack's word. Offer them after a clean day (`pickoffs NAME --hours 24`). Jack sized both 11/11 election waves within 6-11 h; when he asks earlier, do it and say how clean the wave has been (fills, $ against, flags).
   - Size-ups on mid-price favorites raise cross-event sweep exposure (11/12: the 6-15c x2.5 made us 39% of a 9.7c level that a sweep took).
   - Never raise a market whose touch is 3c+ off its 6 h usual price; the bot holds it at base anyway.
   - After a restart, no size decisions for ~15 min: reads run low (section 11). After a LONG halt, an hour: the halted bot takes no samples, so every listed side rests base until its usual price rebuilds.
4. **Breakers stay on.** `--no-halts` is Jack's word: it removes the sweep breaker, the tape trigger's 100+ pause, toxic halts AND the cross-event group breaker for those series (the wave breaker and the 200+ tape trigger stand in). Dropping `--no-halts` from the args doesn't clear the flag on entries already listed; only `--halts-on` does, and only on LIVE entries: ended entries of the same series keep the series in PVD_NO_HALT_SERIES (11/11: 239 ended entries, cleared by hand under scan.lock). Check the bot's "no halts on N series" line after any halts-on.
5. **Never an uncapped re-anchor** (`--reanchor-step 0` / `--reanchor-jump 0`), and never re-tune by hand to "re-anchor now" on a thin book without checking the 6 h usual price first (the dashboard, or `imm_state.json` `pvd_usual`).
6. **Watch after every change:** `pickoffs NAME` after any size-up or re-tune and in every check-in. A series trip = the series stands down at base; tell Jack.
7. **Judge each fill with the pick-off watch; read "P&L today" as real** (marked without our own orders since 0393404). On wide books (FX / metals) the watch's mark means nothing: mark to the fair.
8. **Switch files are Jack's alone** (section 13).
9. **Turning a wave off leaves its positions riding:** `stop` moves its entries off the list, and the wave unwind (1.2) only works held markets still on the list while UNWIND_ONLY_WAVES exists. To work positions down, keep the markets listed with both sides at 0 (a 0 multiple or a pin 0): the closing order still rests. A stopped market comes back in a LATER program period; only the code blocklist keeps a series off everywhere.
10. **Measure before you promise.** Every projection of a size-up is an upper bound (11/14: "model: enough" held for 45 of 108). Say "measured" or "model", and re-measure after the sizes rest.

### 1.4 Gaps still open (say so when relevant; each is Jack's call to build)

- **Slow walks and long-standing baits get through the re-anchor cap** (~20c an hour after its hold); the off-usual cut carries these.
- **The tune's re-anchor reads Kalshi's touch with our own orders in it.**
- **Real moves at the usual price** are limited only by the size rules.
- **Whole-order triggers miss partial fills** (sweep breaker, bait stop).
- **No wave-level loss stop, and no event cap on listed events** (`IMM_PVD_EVENT_CAP` 10M).
- **Some guards fail open for one run:** a failed positions read lifts the twin block; a pinned market whose touch read misses loses its side cap.
- **The fair refreshes every 15 min (FX / metals) or 15 s (crypto):** a jump faster than that fills the bigger sizes before the fair catches up; the crypto 1% jump hold and the WS fast cancel are the cover.
- **One-event news risk isn't detected:** e.g. a pending appointment decides a nominee event (KXSENATENOM-TNR-28 waits on Blackburn's appointee: its 12 markets are pinned at 0 on w1111-statenom since 11/14, Jack's "Yes, do 1-5", until she names someone).
- **A sweep's fills can trip a series stand-down hours later:** the pick-off watch's 6 h lookback flags a fill the first time its mark is 5c+ and $25+ against us, so three of the 11/12 sweep's fills crossing the bar as the books drifted pinned ALL of KXSENATENOM at 200 a side (01:30Z 11/13, a series trip), so his tiers stopped reaching the series until he lifted it on 11/14 ("Yes, do 1-5"; the per-market pins stayed). Removing a stand-down is his word.
- **Heightened bait mode is dry; the wave dashboards' marks include our quotes.**

---

## 2. Detect and size up

```
python wave_detect.py --since <ISO: 30 min before the first new program, or the state's last check> --json <scratch>/detect.json
```

- **What it reads:** every active LIQUIDITY program that started since `--since`, minus markets already on the list or quoted by the bot.
- **Per series:** markets, events, pool $/day, median horizon, days out, listed / quoted counts, median spread, category, title. Horizon = the EARLIER of `close_time` and `expected_expiration_time`.
- **Verdicts, in order:**
  - `LISTED` / `QUOTED`: already handled.
  - `MEMBER`: the bot's own family admits it; its gates decide (triage with `imm-quoting-triage`).
  - `FEW`: fewer than 5 events lit. Never listed or alerted.
  - `CHECK`: Jack's call -- a high-info-risk category (Sports, Crypto, Financials, Climate and Weather, Economics), a mention series, an info-risk title (`INFO_RISK`), or blocklisted.
  - `SOON`: settles within a day, or no date. Jack's call.
  - `NEAR`: settles in 1-14 days. Quotable as the NEAR group.
  - `FIT`: long-dated (>= 14 d) and none of the above.
- **Wave bar:** new FIT + NEAR series with 5+ events each total >= 40 markets or >= $1,000/day of pools. Exit 3 = a wave; 4 = only CHECK / SOON over the bar (tell Jack; list nothing); 0 = nothing. A series triggers once (state file).
- **Batches:** size up EVERY start since the last scan, not just the batch Jack named.
- **The "after economic events" ladders** (`KXGOLDEVENT`, `KXUSDJPYEVENT`, `KXBTCEVENT`, `KX10YRRATEEVENT` ...: "above X one minute after a release") come as CHECK (Financials / Crypto) or NOT LIVE (metals). They are type B: section 12, on Jack's word.
- **A big RELIGHT jump** ("programs 6903 -> 10573 (+3670)") is new programs, not relit ones: say what they are by series family and which the wave check already judged (11/13: 3,169 after-economic-events held for Jack, 501 small series the bot's selection picks up itself).
- **If Jack named the series,** take them as the scope (his word overrides FEW / CHECK), but still run the detector and report each verdict.

## 3. Choose scope (fixed rules, then report)

- **In:** every FIT series with 5+ events. Series Jack named are in whatever their verdict, but report it.
- **In, as the NEAR group** (`--near`): every NEAR series with 5+ events. Dead 120 / live 80 a side (300 on a decided side), the bot's kind cushions (dead 2c, live 5c off the last trade), band 3-97c, no tiers / multipliers / bank-early. Jack: "near series should have the dead/live book distinction, and 120/80 base contracts like in the midterms".
- **Out:** FEW, CHECK, SOON, MEMBER, QUOTED and LISTED, with the reason and the $/day left out, so Jack can say "add X".
- **Flag anyway:**
  - NEAR entertainment / TV competitions (pre-taped results leak), and anything that settles on a running count published with a lag (the strait weeklies: the shipping family stands them down 2 days before the week ends, 6e9b248; a wave series on a weekly count wants the same stop).
  - Events one announcement decides (an appointee, a withdrawal): search the news before a size-up and offer an event block (section 1.4).
- **Election-family membership:** a series the wave list alone admits is checked for Kalshi's Elections category and enrolled (`allowed_by_wave_list_only`, 11/11; it was what got statenom's long shots quoted before listed markets quoted their own band, 38f07e9 / 0f12693). The side effect: an UNLISTED market of that series can quote under the election rules at x2 once its event has a verified date. Jack 11/11: "No leave it."
- **Name the wave** `w<MMDD>-<theme>`. Same theme and program end as a registry wave: extend that wave (`set NAME series ...`). Bookkeeping waves (registry homes for relit series, e.g. w1111-w28 / w1111-pol) are not new waves: never call them "today's waves".
- **`--since`** = 30 min before the earliest new program start (the tune's `period_start` floor).

## 4. Preflight (the gate)

```
python pvd_waves.py preflight --series A,B --near C --detect <scratch>/detect.json [--auto] [--budget N] [--legacy]
python pvd_waves.py preflight NAME|all [--detect ...] [--auto]       # an existing wave's args too
```

PASS / WARN / FAIL for every guard a wave relies on; exit 1 on any FAIL:

| Check | FAIL when |
|---|---|
| Switch files | HALT exists; UNWIND_ONLY without UNWIND_ONLY_EXCEPT_WAVES; UNWIND_ONLY_WAVES on a bot without the scoped startup line (else INFO: held wave markets unwind, flat stamped ones quote) |
| Bot state | `halted_until` in the future; no "mkts quoted" line in the last 10 min |
| Bot guards, from its latest startup lines | Join wait (penny + 0.1c books, rest memory), off-usual size, sweep breaker, tape trigger, bait stop, company KPIs OFF, close clamp, lone-ref guard on the wave list; a line older than the latest start = WARN |
| Shard-0 cash | Under $5,000 (`PVD_MIN_CASH0`) |
| **Cash on the wave's other shards** (2026-11-14) | The shard its markets trade on (one market a series read for `exchange_index`; metals and crypto are shard 2) under $1,000 (`PVD_MIN_CASH_OTHER`): FAIL in auto, WARN manual. Only Jack moves money; give him `python kalshi_subaccount.py shard N --from 0 --to 2` |
| **Resting-order room** | Under 500 orders left under `IMM_MAX_TOTAL_RESTING` (12,000): FAIL auto / WARN; under 2,000 WARN. At the cap the bot defers EVERY new placement |
| **Resting-order read** | `RESTING_READ_TRUNCATED` since the bot's start (it can't see all its orders and re-places them): FAIL |
| **Placement queue** | INFO: placements deferred last cycle (the 1,500-a-cycle cap; renewals use most of it) |
| **Collateral budget** | WARN when the modelled ladders + reserve pass 90% of `IMM_COLLATERAL_BUDGET` ($3M) or any market is refused for budget |
| **Event ceiling** | WARN at 95% of `IMM_MAX_MARKETS` (1,000 events) |
| **Recent restarts** | WARN within 15 min of a start (reads run low), INFO within 30 |
| **Scan-budget room** | WARN under $5k of listing room (the larger of the wave's own budget against its own listings, and the shared rule) |
| Wave args | `--no-halts` / `--size-mult` / `--series-tier`, tiers above the default (FAIL auto, WARN manual); re-anchor step or jump 0 (always FAIL); a probe-sized 1-9 cap on 90c+ sides (FAIL auto); no twin block (WARN) |
| Scope (with `--detect`) | Not FIT / NEAR, fewer than 5 events, not in the detector output (FAIL auto, WARN manual); company KPI or blocklisted (always FAIL) |
| INFO only | Lone-ref mode (rest of the bot), external marks, net halt, full-earn line, the no-halts series, the daily-loss halt |

`--no-capacity` skips the capacity rows (to read the rest quickly; never to get past a FAIL -- `add --auto` re-checks capacity anyway). `--legacy` adds the hand-written args of the relight task's pass 6. Manual mode: a FAIL means stop and tell Jack what failed and what it would take. A WARN is said in the report. Never touch a switch file to make a wave pass.

## 5. List and tune

```
python pvd_waves.py add <NAME> [--series A,B,C] [--near D,E [--near-dead 120 --near-live 80]] --since <ISO> --preset default [--budget 100000] [--stop-cap ISO] --note "<what lit>"
python pvd_waves.py run --only <NAME> --write
```

Auto mode adds `--auto --detect <file> --budget 50000`.

- **`add`** writes `run-logs/pvd-scan/waves.json`. The relight task's pass 7 then rescans and retunes every enabled wave every 15 min; pass 0 keeps the dashboard up; pass 8 watches pick-offs; pass 9 the dark check; pass 10 the bait watch. **Never edit `run_pvd_scan_waves.cmd` for a registry wave** (passes 1-5 are retired); pass 6's own args change only on Jack's word (section 9).
- **`run --write`**, per group (main = `--series` on the preset, near = `--near`):
  - `pvd_scan.py` lists every market clearing $1.10 by the stop at `scan_size` (200) a side; a market under the bar is re-scored with its <= 5c sides at the group's tier size.
  - **Budget (2026-11-14):** a market is taken while EVERY wave's live scan adds stay within `PVD_SCAN_BUDGET` ($300k) + the wave's `budget`, OR while the wave's OWN live listings stay within its own `budget`. Before this, $691k of other waves' listings blocked new waves completely (the crypto wave listed 0 at +$100k; an auto wave at +$50k would have too). Collateral counts at the LISTING size: tiers make real exposure several times larger.
  - The fair refresh (`fair_cmd`), then `pvd_wave_tune.py` applies the args.
  - `datefix` adds `event_start_overrides.json` rows for DDMONYY tickers the bot misreads (section 12).
- **Pass lock (2026-11-14):** `run --write` holds `run-logs/pvd-scan/wave_pass_NAME.lock` from reading a wave's args through its tune. `setarg` / `set` / `stop` wait for it (up to 15 min), the pick-off pins (10) and darkcheck's re-tune (5) too, and then write on a fresh registry read; the task's pass skips a wave another pass holds; `run --only NAME` waits (`--wait-lock SECS`). Before it, a pass that started before a setarg wrote the old sizes back (FX 04:02Z 11/14).
- **Read the output:** the scan's `taking N by ROI ... new at risk $X ... over budget M` line and its "this wave's own $X of its $Y (N taken on its own budget)"; any `! /markets batch ... failed twice` line; the tune's `WROTE` line.
- **Preset `default`** (every value is printed in the report):

| Setting | Value | Why |
|---|---|---|
| Listing size | 200 a side | Bank $1.30 early; the scan's $/day is optimistic |
| Band | 1-99c | All prices quotable (a listed market quotes its own band: 38f07e9 + 0f12693) |
| Limits | Follow the book: touch +-3c every run, cushion 0, CAPPED: <= 5c a run toward the taker, held a run after a 10c+ jump or an empty side | Static limits went stale in minutes; uncapped, they followed the baits |
| `--flip-through` | On | After a fill, the reducing side rests size + position |
| Bank early | 200 until $1.30 banked; 300 where 200 won't bank 6 h before the stop; no drop; hold the max | Kalshi pays a period only from $1 |
| Cheap tiers | <= 2c x10, <= 5c x5 (nothing above 5c) | Cash-cheap sides; mid-price tiers fed the 11/07 pick-offs |
| Side cap | none (Jack 11/08: "dont preset that") | `--side-cap ALL:90:0` stays a lever |
| Twin block | 200 | A D/R pair's same-bet leg rests nothing once one leg holds 200+ |
| Breakers | ON | Sweep breaker, tape trigger, toxic halts, bait stop |

- **Preset `near`:** flip-through, the capped re-anchor, twin block 200; no side cap, tiers or multipliers.
- **Preset `2028`** (Jack's word only; `add --auto` refuses it): the 11/07 state (tiers to x45, `--size-mult 1.5`, `--no-halts`). Its premise was disproved by the House-night baits.

## 6. Dashboard

```
python pvd_waves.py dash <NAME>          # starts it detached on the wave's port (8775+), filtered to its series
```

- **URL:** `http://127.0.0.1:<port>/`; the first poll takes a few minutes; `/data` is the JSON the tune's bank step and `pace` read (loopback needs no key).
- **What it shows:** tiles (est rewards, trading P&L, cash in positions, "if every resting order filled", shard-0 cash), the pacing table ($1.30 with 6 h spare ... under $0.75), fills and cash by price band. Its pacing reads ONE bot cycle's share per market: use `pace` for decisions (section 11). Its open P&L marks with our quotes in the book: use `pickoffs`.
- **A wave can reuse a fixed page:** the crypto wave uses `imm_crypto_event_watch.py` on :8783 (a fixed keepalive entry; the registry loop skips a port already served). Give the registry wave that port so bank-early reads its `/data`.
- **Phone:** home Wi-Fi at `http://<the PC's LAN IP>:<port>/?k=<token>` (the IP is in memory `market-wave-playbook`) (the key in `run-logs\pvd-scan\dash_token.txt`; never commit it), or Wave Pocket (skill `wave-pocket`, on request only). Name a new wave there "Today #N · theme". The 2028 (:8773) and political (:8771) "Since 11/06" dashboards were retired 11/14 (their markets are registry waves); the one left (:8774, the 11/04 books + other listings) overlaps newer waves, so never add it to a new wave's total. The page holds 256 KB: its builder trims the older tabs first.
- **Restarting a dashboard:** never run `pvd_watch_keepalive.ps1` from a tool shell (its children hang the call); use `pvd_waves.py dash` or `Start-ScheduledTask "KL pvd-scan waves"`.

## 7. Verify the bot is quoting (the exchange is the truth)

1. **Reload:** the bot log (`run-logs/incentive-mm/incentive-mm-<its start date>.log`: read the newest) shows `post-vote dead books: N market(s) ... (k changed)` within 1-3 min.
2. **Selection:** at the next universe refresh (<= 10 min), `selection_events_<UTC date>.jsonl` has the tickers `selected`. Listed is not selected: the bot's screens still apply (cutoffs, payout floor, hopeless, event top-N, budget).
3. **Orders:** `placed / amended <ticker> BID|ASK -> Nx @ Pc` lines (faster than the cycle log, which lands in batches).
4. **Counts and reasons:** `pvd_waves.py whydark NAME` (read-only, ~15 s): two-sided / one-sided / dark on Kalshi, and for every side not resting the first reason that holds -- on purpose (wave off, paused, sized 0, a 0 multiple, a pin 0), closed on Kalshi, held (only the closing side rests), a breaker hold, the bot's selection decision, a bot guard, the bot log's per-side lines (crypto stand-aside, lone-ref, pvd guard limits), the book past the side's limit (fair clamp or the capped re-anchor), wanted but queued, sized 0 by the bot. A dark side is safer than a side chasing a bait: check the touch and the 6 h usual BEFORE re-running a tune.
5. **What can bind right after a big listing** (preflight shows the numbers):
   - **The placement queue:** at most 1,500 placements a cycle (`IMM_MAX_PLACEMENTS_PER_CYCLE`, e403df0), and renewals (each order every ~25 min) use most of it at ~9,000 resting. Dark sides go first (60c237d); a deferred same-price size-up keeps its old order resting (53c816d). A 2,000-market listing takes several cycles.
   - **The resting-order cap** 12,000 (`IMM_MAX_TOTAL_RESTING`) and the bot's own-orders read (1,000 a page, 40,000; 757614b). Keep the read far above the cap: at 40 x 200 under a 12,000 cap the bot re-placed 3,462 sides it couldn't see.
   - **The collateral budget** $3M (`IMM_COLLATERAL_BUDGET`, ea5e796): it charges every SELECTED market a standard ladder whatever its list size, and selected markets stay selected, so turning list markets off frees nothing. At $1M on 11/14 it refused 150 other markets.
   - **The event ceiling** (`IMM_MAX_MARKETS`, 1,000 events) and event top-N families.
   - **Restarts:** each one re-queues the book; other sessions' deploys made 8 in one night (11/14). Reads within ~15 min run low.
   - A position at its side size (that side goes dark until it unwinds).
6. **First pick-off check:** `pvd_waves.py pickoffs <NAME> --hours 1`.
7. **Expect competition:** every wave's rate fell after the first hours as other makers found the pools -- statenom ~$4.4k -> ~$0.75k/day in 26 h (long-shot bids pushed 1c -> 3-6c by makers resting 7,499 @ 3.1c, 129,765 @ 2.8c); elec2028 $761 -> $452; FX $3.5k -> $2.5k (others added ~25M contracts, ~5 for each of ours); crypto makers moved their walls in around the fair. Report the first-hour rate as a ceiling and re-read at +2 h and +6 h.

## 8. Report and ask

```
python pvd_waves.py report <NAME> --detect <scratch>/detect.json --out <scratch>/report.md [--email [--test]]
python pvd_waves.py pace <NAME>               # the under-$1 numbers
```

The report carries the preflight table, the pick-offs since go-live and the "Every parameter" table. `--email` sends it through the bot's alert mailer.

Then answer Jack with:

1. **What lit:** programs, series, markets, pools, settle dates, the program end; the wave type (0.1).
2. **What's in** (FIT on the preset, NEAR on the midterm treatment) **and what's out with the reason** (FEW / CHECK / SOON / MEMBER), plus the $/day left out.
3. **Guards:** preflight PASS / WARN (capacity and shard rows included), and anything in section 1.4 that applies.
4. **Numbers:**
   - Listed / quoting / two-sided (`whydark`), and why the rest isn't.
   - Est $/day: the bot's estimate, labelled a ceiling (competition, section 7). From `pace`: how many project $1+, and the under-$1 count by the multiple each needs.
   - $ if every resting order filled; positions; pick-offs so far.
   - Cash on every shard the wave trades on (fills lock it until settlement).
5. **Every parameter, as a table** (Jack 11/08: "make sure to provide a table of the presets of all the parameters to tune, be thorough"). Put the report's "Every parameter" table itself in the answer: scope and listing; every tune lever of each group, set or not, its `setarg` command and who decides; the bot's guards from its startup lines; halts and switches. Values in cash terms and multiples of the base ("2c sides rest 10x the 200").
6. **Assumptions:** who knows first; the outcome date and cash lock-up; the scan's optimism (11/04 ~2x, 11/06 ~2.7x); competition; weak per-contract scaling of cheap sides.
7. **The tune menu, then "Want to tune any of these?"**
   - Type B with a live fair: what the $1-floor protocol did (pre-approved, section 11). Any other wave pacing under $1: the protocol as option 1, with `pace`'s numbers.
   - Size: tiers by cost bucket / `--size-mult`, gated by a clean day.
   - Breakers: `--no-halts`. Cash: the side-cap level. Scope: add a CHECK / FEW series, move NEAR to main, stop a market. Band and limits.

In auto mode, email the same report (wave_alert attaches it) and don't wait for an answer.

## 9. Watch: pick-offs, stand-downs, "am I getting baited?"

```
python pvd_waves.py pickoffs [NAME|all] [--hours 6] [--min-c 5] [--min-usd 25] [--series-trip 3] [--act] [--update-state] [--json OUT]
python wave_alert.py pickoff --pickoffs run-logs/pvd-scan/pickoffs_last.json [--send]
```

- **What it reads:** every fill on a wave market in the window; only fills that OPENED or ADDED to a position count.
- **Mark:** each market's Kalshi book with OUR resting orders taken out (our orders prop up thin mids). On wide books with a fair (FX / metals) that mark is meaningless: judge against the fair.
- **Flags:** 5c+ and $25+ against us = flagged; 3+ flagged markets in a series = a series trip; exit 3 = something new.
- **`--act`:** pins a flagged REGISTRY-wave market at the wave's base (`--pin TICKER:200`: no tiers, multipliers or bank step), a tripped series with `--pin-series SERIES:200`, on a fresh registry read under the wave's pass lock, and re-tunes now. It never sells a position. Jack's precedent: "too active, scale it down to base contract sizes". Markets outside the registry (pass 6: the 11/04 post-vote books and other listings) are only reported: their stand-down is a `--pin` on pass 6's line in `run_pvd_scan_waves.cmd`, Jack's word -- edit the line, grep it back (the Edit tool can drop a trailing space: "70:2.25--bank-size" broke a pass once), then run that pass by hand with `--write`.
- **Where it runs:** pass 8, every 15 min, 24/7, then `wave_alert.py pickoff --send` on exit 3 (text at $100+ new, a series trip or a stand-down; email otherwise).
- **Late alerts are not delays:** the 6 h lookback alerts a fill the first time its mark crosses the bar, so a fill from hours earlier alerts when the book drifts (11/12: sweep fills from 3:08 PM alerted at 5:31 and 7:02 PM). Each line now carries the fill time ("filled 3:08 PM ET (4h 22m ago)"). A series trip can come the same way (section 1.4).
- **"Am I getting baited?"** Read the public tape (`/markets/trades` by ticker, +-2 h) before anything else:
  - Whole levels taken from every maker across many events, a market every 20-60 s = a SWEEP (11/12). The group breaker is the guard; marks drift for hours after.
  - A better price flashed and pulled, then a taker of exactly our size takes our whole rung = BAIT (the LoL / Paris recipe). The bait stop, join wait and lone-ref guard are the guards.
  - Both parties' favorites hit at once = a liquidation or a fade of favorites, not news.
  - Then check the news for the one event that moved (e.g. an appointment pending).
- **Undo one stand-down:** `pvd_waves.py setarg NAME pin TICKER:200 --unset` (with a value, only that pin). Jack's word.
- **Stronger stand-downs** (Jack's word): `--side-cap SERIES:1:0` (every side off; only closing orders rest), `--pause-at SERIES:C`, `pvd_waves.py stop NAME` (disable + live entries to "stopped"; positions ride, unworked: rule 9).

## 10. Tuning a live wave (after Jack answers)

**Every change goes through the registry, so it persists and relights inherit it:**

```
python pvd_waves.py setarg <NAME> cheap-tiers 2:10,5:5,15:2        # replace a flag
python pvd_waves.py setarg <NAME> pin-series KXFOO:300             # repeatable flags add
python pvd_waves.py setarg <NAME> pin KXFOO-X:200 --unset          # remove one value (no value = all)
python pvd_waves.py setarg <NAME> no-halts                         # a switch (Jack's yes); halts-on reverses it on live entries
python pvd_waves.py setarg <NAME> side-cap ALL:90:0 --group near   # the NEAR group's own args
python pvd_waves.py set <NAME> near KXA,KXB | scan_size 300 | budget 150000 | enabled false
python pvd_waves.py run --only <NAME> --write                      # apply now (waits for a running pass)
```

**Jack's size commands, read this way** (11/11-11/12, statenom and elec2028):
- Cost = what one filled contract ties up: the bid's price, or 100 - the ask's. "<= 15c" covers bids at 15c or under AND asks at 85c or over; say so, and offer to leave the asks out.
- "Nx on <= Yc" multiplies the CURRENT tier of every side in that range ("1.5x everything <= 15c": 2:10,5:5 -> 2:15,5:7.5,15:1.5).
- "A-Bc" = above the A tier through B ("5-30c" leaves the <= 5c tier alone, the 11/07 "5 to 15" convention).
- "Instead of" replaces the last step ("Actually 2x instead of 1.5x" re-bases from before the 1.5x).
- A neighbouring range stays as it is unless he says: ask ("Should I double the <= 2c too?" -> "Yes, double them").
- "Nx" alone is an absolute multiple: check the current size first.

**Every size-up reply gives:** the tier spec before -> after; sides changed; cash if every changed side filled, before -> after; how many rest the new size now vs held at base by the off-usual guard (they step up as the 6 h usual catches up, often overnight); when the rest lands (a step of ~25% or less sits at the edge of the 20% amend tolerance, `IMM_ATREF_COUNT_TOL`: resting orders keep the old size until their next re-placement, ~20-25 min); fills and flags since; and the undo command.

**Check every change:**
1. Snapshot the list before (the scratchpad).
2. Diff after, against the expected multiple per side.
3. Confirm the bot's reload line ("N changed") and its `mkts quoted / resting / amend / cancel / place` lines.
4. Big re-sizes hit the per-cycle amend and placement caps: resting dips for minutes; the dashboard rate lags a re-size ~25 min.
5. `pickoffs NAME --hours 2` 30-60 min later.

**Off means both side sizes 0.** The bot reads `"size": 0` alone as its default size (PVD_SIZE, 40 a side): the 341 FX markets "turned off" that way kept 40-80 a side until the tune wrote `bid_size`/`ask_size` 0 (fixed 11/14: a 0 pin or a 0 multiple now writes both). A crypto strike's stop also needs its entry moved to the list's "stopped" (crypto_event_stop.json + "stopped", e4dd1f4): an unlisted strike falls back to the family's own quoting.

**A decided side** (last trade >= 90 / <= 10) ignores the band AND the limits: to stop such a market, `--pause-at SERIES:0` plus `--side-cap SERIES:0:0` (KXJEFFRIESFIRST 11/11). Keep it listed: delisted, its family could quote it.

### Levers (`pvd_wave_tune.py --help` lists every flag)

| Lever | Flag | Notes |
|---|---|---|
| Bank early | `--bank-size 200 --bank-max 300 --bank-by-hrs 6 --bank-usd 1.30 --bank-url <dash>/data --no-drop --hold-max [--base-size N]` | Needs the wave dashboard; sizes only ratchet UP within a period; the bank step waits 25 min after a re-size. Short windows: bank big early (crypto 1,500 until $1.30, then base 300) |
| Cost-per-contract tiers | `--cheap-tiers C:X,...` | First fit wins. Above 5c: Jack's word |
| Series tiers | `--series-tier SERIES:bid\|ask:C:X` | Checked first; REPLACES the general tiers below its first bound. Jack's word |
| Scale everything | `--size-mult X` | Once per entry. Jack's word |
| Per-market multiples | `--size-mult-file PATH` | `{ticker: X}`, x the size like `--size-mult`; X = 0 is OFF (both sides 0, sizes kept in `pre_off` and restored when lifted). Written by `resize` (section 11) |
| Pin | `--pin TICKER:N[:flat]`, `--pin-series SERIES:N[:flat]` | Flat N a side, no tiers or multipliers; the side cap still applies |
| Cash cap per side | `--side-cap ALL\|SERIES:C:N` | A side tying up >= C c rests N; N = 0 off (only a closing order) |
| Twin block | `--twin-block 200` | Multi-candidate events are not twins |
| Market pause | `--pause-at SERIES:C` | At C c+ (bid or last) a market quotes nothing, or only its unwinding side; resumes under C-2 |
| Breakers | `--no-halts` / `--halts-on` | Jack's word, both ways |
| Limits | `--follow-book --reanchor 3 --reanchor-step 5 --reanchor-jump 10 --cushion 0`, `--room`, `--price-min/max` | Never 0 on step / jump |
| Fair limits | `--fair-file PATH [--fair-margin 3 --fair-vol-err 0.25 --fair-floor 2]` | Bid <= fair - margin, ask >= fair + margin (margin = base + vol-error term); no fair / stale = floor sides only. Put the margin in the fair file's "margin" so a pass started earlier can't revert it. Registry `fair_cmd` refreshes it before each tune |

### Measured (use for advice; details in `history.md`)

- **Per contract, cheap sides scale weakly:** Kalshi scores a side within its 1,000-contract window from the best price. 2028 elasticity of rewards to contracts 0.06-0.09 on <= 2c raises, ~0.3 on 2-70c; contracts 15x -> $ at risk 2.7x -> est $/day 1.5x.
- **Per dollar:** ~$4-7/day per extra $1k at risk on the 2028 wave; 90c+ sides were the worst.
- **The what-if** `pvd_marginal.py` (~12 min): raises realized 55-75% of its prediction. **The size model** (s' = r s / (1 + (r - 1) s), 65% counted) held for 45 of 108 crypto pins: still an upper bound.
- **Other makers answer:** ~5 contracts for each of ours on FX within 2 h of a size-up; the share it bought fell back.
- **Near-term markets** (outcome within ~6 months) move on news: pin them at a flat base. **Correlated tails:** a 10x on one candidate's 1-2c bids across 50 states is one bet on a dropout.
- **In-play markets:** once one has banked $1.15+ this period, stop it (11/05: "Stop the 27").

### Symptom -> lever

| Symptom | Check | Lever |
|---|---|---|
| Dark or one-sided after listing | `whydark NAME` | Wait for the queue; re-tune by hand only off a real move |
| One side dark, book moved off an old trade | last vs touch | `follow_book` (preset default) |
| Asks / bids behind the book on a wave with a fair | `whydark` "past its limit (re-anchor)" | Fair limits replace the 5c walk (crypto 28837b4) |
| Pacing under $1 | `pace NAME` | Section 11 |
| Total rate dropped | restart / re-size times; others' size at our levels | Restarts and re-sizes read low ~15-25 min; competition: re-read, size by measured share |
| Fills cluster / a pick-off | `pickoffs NAME`, the tape | Pin at base (`--act` does it); a series trip = tell Jack |

## 11. Size to the $1 floor

Kalshi pays a market's period only from $1; under it the period's share is lost. Jack, 11/14 on both short waves: "size them up", "go up to 5x every market, to get them over the $1 threshold", "after these increases, stop quoting markets still pacing <$1", "like prior market-waves, bank as much size early to be safe", "quoting time is valuable ... so prioritize quoting time"; and 11/11: "in general as a rule of thumb when sizing to hit threshold, aim for $1.2". The bot's own floor boosts keep their $1.10 re-aim trigger (Jack 11/11: "Keep the $1.10 trigger").

```
python pvd_waves.py pace NAME [--aim 1.20] [--window-min 60] [--json OUT]       # read-only
python pvd_waves.py resize NAME [--aim 1.20] [--max-mult 5] [--keep-within 0.05] # dry run: the plan
python pvd_waves.py resize NAME ... --apply                                      # Jack's word
```

**How `pace` measures** (`pvd_wave_pace.py`):
- Projection = banked this period (the dashboard's `period_est`) + the AVERAGE rate since the market's size landed x the hours left. Not one cycle: the bot's per-cycle share swings ~2% <-> 9% on wide books and reads 0 for a cycle after a stand-down (FX 13:45Z 11/14). A cycle resting nothing counts 0, so the rate is uptime-weighted.
- Rows from 10 min after the bot's last start; a market resting under 90% of what the bot wants is "landing" and not judged. Within 15 min of a restart reads run low (crypto 11/14: 160 at $1+ seven minutes after one, 230-286 before): `pace` warns, `resize --apply` refuses without `--force`.
- Under-$1 tables: by the multiple each needs for the aim (the model above: an upper bound), with one-sided counts and the cash added if every added contract filled; by side cost (1-2c, 3-5c, 6-10c, 11-20c, 21-35c, 36-50c, 51c+: what Jack asked for 11/14); by series.

**The protocol:**
1. **Bank early, as big as the window needs: pre-approved up to 5x the base on a type B wave with a live fair (Jack 11/14: "Yes, pre-approve"); elsewhere a bank size above 300 is Jack's word.** On a 1-2 day window the most size from the start paid (crypto: 1,500 a side until $1.30 banked, then base 300: 230 -> 469 of 498 strikes at $1+). Cold starts cost quoting time: crypto strikes rest 400 instead of base while they have no usual price (1f68841).
2. **Read `pace`** once sizes have rested and 15+ min after the last restart.
3. **`resize` dry run, then `--apply`** (pre-approved on a type B wave with a live fair; elsewhere Jack's word): the smallest quarter-step multiple up to 5x that projects $1.20 (or 5x if that still reaches $1); never lowers a multiple; an off market stays off.
4. **Two reads before an off:** a market that can't reach $1 even at 5x goes to 0 only when the previous `resize` read, 5-90 min earlier, judged it the same way. One read right after a restart would have stopped 258 strikes (11/14). A market projecting $0.95-1.00 is kept (estimate noise; it keeps its banked share).
5. **Re-measure ~25 min after the sizes rest, and again at +2 h:** other makers answer with size (FX 11/14: within 2 h of the 5x size-up they added ~25M contracts against our 5M; median share 8.8% -> 6.3%, the rate $3.5k -> $2.5k/day; the 12:50Z re-read turned 523 more markets off).
6. **Say what it costs:** the cash if every added contract filled, by cost bucket; the expensive sides (21-50c) carry most of it.

What a 10x lift would buy, and what turning markets off frees, are not the same question: turning list markets off frees no collateral budget (section 7). Once Jack says "Keep them off", don't re-offer the lift for that wave.

## 12. Short-window live-price ladders (type B)

Kalshi's "after economic events" series: will gold / silver / copper, USD/JPY, EUR/USD, AUD/USD, USD/MXN, USD/CAD, GBP/USD, BTC / ETH / SOL / HYPE, or the 2/5/10/30-year yields be above X one minute after a release. Thousands of strikes, $20 a strike, ~1-2 day programs (11/13 batch: 3,670 programs, $52.7k/day of pools, programs to Sat midnight ET).

**The decision (Jack 11/13 "Do it" on this rule):**
- **FX + metals:** only while their markets are SHUT (Fri 5 PM - Sun 5 PM ET) with a fair, and only through the program end; if a program runs past the Sunday open, the stop cap is the open (`--stop-cap` 22:00Z Sunday). Never on weekdays without a live feed (the 15-minute markets were passed on for the same reason).
- **Crypto:** 24/7, so only with the bot's live crypto fair (`crypto_event_fair`, 1442295: the crypto fleet's GBM on Kraken spot, every 15 s; 1% jump hold; stale > 180 s = stand aside). Touch markets ("hit by", early close) stay NO-only.
- **Yields:** skip (tiny pools).

**Setup:**
1. **Fair first.** FX / metals: `wave_fair.py OUT --live` (lognormal on Friday's 5 PM ET close -- metals = Kalshi's weekly settlements KXGOLDW / KXSILVERW / KXCOPPERW, FX = Yahoo -- moved by 24/7 venues: Hyperliquid trade.xyz perps for GOLD / SILVER / COPPER / JPY / EUR / GBP, Kraken for AUD/USD and USD/CAD, Bitso for USD/MXN), in the wave's `fair_cmd`, read by `--fair-file`; valid 45 min after each refresh (a failed source reuses its last price under 45 min old; stale = floor sides only). **Its Friday closes and T0 are hard-coded to 11/13: update CLOSE and T0 in `wave_fair.py` for a new weekend before listing.** Margin 2c (copper 6c) + the vol-error term, set in the fair file. Crypto: the bot's gate, nothing to add.
2. **Dates:** these tickers are DDMONYYHHMM (KXGOLDEVENT-17NOV261100 = Nov 17 2026 11:00 ET); the bot reads YYMONDD and drops them (27 of 807 got in on 11/13). `run --write` runs `datefix`, which writes `event_start_overrides.json` rows (hot-reloaded); check its output after the first pass.
3. **Wave books:** a listed crypto strike quotes like any wave book -- no fair lead, the wave's reference on both sides (20aca4e; Jack 11/14: "it should function like the other market-wave books") -- and its limits come from the live fair every cycle, not the 5c-a-run walk (28837b4).
4. **Cash:** metals and crypto trade on shard 2 (shared with the crypto bots); FX and yields on shard 0. Preflight checks both. Only Jack moves money.
5. **Capacity:** 2,000+ markets means the placement queue, the resting cap and the collateral budget (section 7) bind within the first hour.
6. **Size for the floor from the start** (section 11), pre-approved while the fair is live: bank early up to 5x the base at go-live; `pace` / `resize` once the sizes rest; two-read offs; a stop file for crypto strikes (`crypto_event_stop.json` + the list's "stopped") covers strikes the wave never listed too (Jack 11/14 "Stop them early").
7. **Deploys:** batch bot deploys during the window: each restart re-queues the book and reads low for ~15 min (8 restarts the night of 11/14).

**What it earned:** FX + metals ~$1.1k/day at first (budget-capped, tails screened, dates misread) -> ~$3.5k/day sized up -> ~$2.2-2.5k after other makers answered; 1 fill in ~20 h. Crypto ~$110 -> ~$1,356/day with fair limits and bank-early; 0 fills; $405 banked by 12:50Z 11/14. The bot's first estimate (~$9k for FX + metals over 30 h) is a ceiling.

## 13. Cash, sweeps and the switch files

- **Sweeps:** 11/07 a taker worked the House districts (~1/min, 20 min): 6,450 of our contracts, ~$7k of cash locked to 2029, prices didn't move. 11/12 a seller cleared the top bid of 18 Senate-nominee events in 21 min (section 1.1). The group breaker (2nd fill of 200+ in another event of the group within 10 min -> that side of the group held 20 min) is the standing cross-event protection; the per-event sweep breaker can't stop a taker that moves on.
- **If Jack sees a sweep:** pause with the tune (`--pause-at`, or a lower `--side-cap`) and run it now, keeping the unwinding sides open; watch `ws_tape_<date>.jsonl` "burst" events with a Monitor; reopen in steps; show fills by price band and the cash tied.
- **Shard cash funds the bot.** Shard 0 funds most of it; shard 2 funds metals and crypto; shard 3 baseball / basketball / tennis. When a shard runs out, orders there are rejected (404 insufficient_shard_balance). Kalshi holds no cash for resting orders: only fills draw it. Only Jack moves money (`python kalshi_subaccount.py shard <$> --from 0 --to N`, it asks for `MOVE`); transfers need whole cents.

**The switch files** (`run-logs/incentive-mm`, read every cycle). **Only Jack creates, deletes or renames them.** Rename to `.off-<stamp>` to keep a record.

| File | While it exists |
|---|---|
| `HALT` | Everything cancelled; the bot idles |
| `UNWIND_ONLY` | Every market quotes only its reducing side, the whole position as one order at the better of its price and the external touch (listed markets never past usual -/+ 3c) |
| `UNWIND_ONLY_EXCEPT_WAVES` | With UNWIND_ONLY: listed wave markets quote as usual. Alone: inert |
| `UNWIND_ONLY_WAVES` (since 11/08) | Every HELD market on the wave list only unwinds (the wave unwind, 1.2: the outside price, at most 5c past usual or at cost where further, never leading; held listed markets the selection left out too -- not markets moved off the list); a flat market of a STAMPED wave (`wave_added` after the switch) quotes as usual; an unstamped entry stays dark until a tune stamps it (every pass does, within a run). `IMM_UNWIND_WAVES_HELD`, `IMM_UNWIND_WAVES_EXEMPT_NEW` |

**P&L halts:** daily loss `IMM_DAILY_LOSS_LIMIT` $8,000 (P&L today, realized + marked without our own orders, carried across restarts; halts to the 5am-CT roll); net halt off; account-value drop $2,000. To lift a halt early, clear only `halted_until` in `imm_state.json` with the bot stopped (never `--clear-halt`). Jack's word.

## 14. Overnight, relights and the 24/7 check

- **Relight task "KL pvd-scan waves"** (every 15 min at :00/:15/:30/:45 local):

| Pass | What it does |
|---|---|
| 0 | Dashboard keepalive (registry waves + fixed pages such as :8783) |
| 1-5 | RETIRED 11/11: the 2028 and political series are registry waves (w1111-elec2028, w1111-w28, w1111-pol) |
| 6 | The 11/04 post-vote books and other listings outside the registry: 200 floor, stamped |
| 7 | `pvd_waves.py run --write`: every enabled registry wave, scan + fair + tune + datefix, under each wave's pass lock |
| 8 | Pick-off watch + alert |
| 9 | `darkcheck --act` + `wave_alert.py dark`: listed vs resting on Kalshi |
| 10 | `bait_watch.py` + `wave_alert.py bait` |

- **A new period (type C):** the scanner rebuilds each relit entry from scratch and the tune applies the registry's CURRENT args. So a relit market starts with every size-up Jack made last period (as of 11/14: statenom `2:30,5:15,15:11.25,30:3,70:1.25`, with Tennessee's 12 Senate-nominee markets pinned at 0 and three nominee markets at 200; elec2028 `2:15,5:15,15:3,30:1.5`, i.e. x15 to 5c, x3 to 15c, x1.5 to 30c). Say so in the relight report. Jack 11/14 on the nominee wave: keep the size-ups into its next period ("Yes, do 1-5"); for any other wave, ask whether the new period starts there or at the default. A relit market with no fresh usual price rests base for ~1 h (cold start) before its tiers apply.
- **The 11/11 miss** (history.md): relit 2028 markets listed under the old-wave lock rested nothing while the texts said "greenlit"; nothing compared the list with Kalshi. Now: every listing is stamped within a run; relight alerts say "listed", never "greenlit"; the dark check verifies.
- **darkcheck** (pass 9; the 24/7 check's STEP 0b re-runs it if pass 9 goes 40+ min stale): every live entry 20+ min into its listing is two-sided / one-sided / dark on Kalshi, each dark one with a reason (on purpose, held, the old-wave lock, the bot's own, unexplained). It ALERTS only on darkness seen at two checks 10-45 min apart: a market still under the old-wave lock after `--act`'s try, or WAVE-WIDE (100+ markets and a quarter of those checked). Jack 11/11: "stop false alarming". `--act` re-tunes a lock series' wave or adopts it into `w<MMDD>-adopt` after the auto preflight. For one-sided markets and per-side reasons, use `whydark`.
- **Bait watch** (pass 10; Jack 11/11: "Be extremely careful for baiters, notify me when something suspicious occurs", "Just cover market wave"): TEXT + email on a bait-fingerprint stop or a 3rd sweep trip in 30 min in one event; EMAIL on a toxic halt, a 2nd sweep trip, or a flash (a better price shown and pulled 3+ times in 60 min on one side).
- **A program jump that is a new wave the check listed:** the relight head reads "= new wave NAME, N mkts listed" and sends no second text.
- **The 24/7 check** `market-wave-overnight-check` (every 20 min, around the clock since 11/11; Jack: "run throughout the day"): pause file -> `relight_watch.py` (+ alert) -> darkcheck freshness -> `wave_detect.py --update-state`; exit 3 runs this skill in auto mode (sections 0-8); exit 4 or an error notifies only.
- **"KL pvd-scan"** (:07/:22/:37/:52): the election-mode scan (no wave budget; unchanged by the per-wave budget).
- **Before Jack sleeps:** what stops when; what may be added; cash on each shard; which switch files are on; what protects the waves (1.2, passes 8-10); how to turn one off (`pvd_waves.py stop NAME`).

## 15. Post-mortem (next morning, or at a short wave's end)

- **Pick-offs:** `pvd_waves.py pickoffs all --hours 30 --json <out>`; ET day 05:00Z-05:00Z; adding fills only; marked with our orders out (to the fair on type B); the tape +-2 h around each fill: bait, sweep, news, or a restart rejoin.
- **Net by series:** est x 1.15 + trading, at the last trade and settled where decided.
- **Pacing:** `pace NAME --json`: how many paid ($1+), how many ended under $1 at what size; the measured share vs the model's at each multiple.
- **Competition:** our share and others' contracts at our levels before vs after each size-up.
- **Scaling:** own contracts and est $/day per 5 min from the cycle logs; each change's median 30 min before vs 15-45 min after.
- **Fills and cash by price band;** open P&L by fill time.
- **Update** the wave's memory file, `history.md`, and this runbook.

---

## Talking to Jack

- **Cash per contract, by side:** "sides tying up 90c+". **Multiples against the base:** "20x -> 30x".
- **Options:** numbered, with $/day and $ at risk, the recommended one first. Short replies approve exactly what was offered.
- **Measured vs model:** say which every number is. Never "model: enough" (11/14: Jack: "you said there should be 270+57+33+11+11 strikes over $1").
- **First-hour rates are ceilings;** other makers arrive. Name the wave by date and theme ("11/11 · Senate/Gov nominees"), never a bookkeeping wave as a new one.
- **Pick-offs:** in $ marked to the book with our orders out, each with its mechanism (bait, sweep, real move, restart).
- **Commits:** only on "commit". Quote him in commit messages and list notes.

## Open questions for Jack (as of 11/14)

1. DONE 11/14 ("Yes, pre-approve"): the $1-floor protocol runs on its own, in manual mode, on type B waves with a live fair (section 0).
2. DONE 11/14: KXSENATENOM-TNR-28's 12 markets pinned at 0 until Blackburn names her appointee (lifting them -- `setarg w1111-statenom pin KXSENATENOM-TNR-28-<X>:0 --unset` each -- is Jack's word).
3. DONE 11/14: the KXSENATENOM series pin lifted (436 markets back on the tiers: 175,600 -> 1,148,900 contracts listed); KSR-JMOR, ARR-JBOO and COD-MBEN keep their 200 pins.
4. 1c bid / 99c ask edges for crypto tail strikes worth under 1c (offered 11/14); ranking dark sides by $/day in the placement queue (offered 11/14).
5. Add the under-$1 check to the FX wave's 15-minute pass (asked 11/14 13:57Z), i.e. run `resize` on a schedule?
6. A wave-level loss stop (e.g. -$500 in an hour of pick-offs) in the bot.
7. The clamp on every side always (+$263 replayed, lags real moves ~3 h).
8. Hard-cap crowded cheap sides at the 1,000 target; roll back the <= 5c doubling on 2028.
