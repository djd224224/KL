---
name: market-wave
description: >-
  Executable runbook for quoting a wave of newly incentivized Kalshi markets with the IMM (elections, politics, awards, the "after economic events" metals / FX / crypto ladders, sports props such as the NFL LADDER / ESCALATOR waves, any series) without getting picked off, and for sizing a wave over the $1 payout floor. End to end: detect (wave_detect.py), choose scope, pass the gates (pvd_waves.py preflight), list on the default preset, start the dashboard, verify quoting, watch pick-offs, report and ask what to tune. Use when a wave or batch of programs launches (sports too: NFL / NBA / NHL / MLB / esports props, head-to-heads, MVPs), an upcoming-wave notice arrives or Jack asks what's coming, Jack names series to quote fast, he invokes /market-wave, or the 24/7 wave check reports a wave. Also for a live wave's follow-ups: pacing under $1, "size them up", a share target ("at least 2% of each book"), why a market isn't quoting or is one-sided, "why denied", other makers' walls stepping up, the fair's edge, the wave dashboard's tiles and tables, "am I getting baited", pick-off / dark / relight alerts, new periods, switch files, the post-mortem.
---

# Market wave runbook

What taught it (details and numbers: `history.md` beside this file):

- **9/24-10/20 NFL LADDER / ESCALATOR waves** (before the registry; the bot's sports family): 1,509 player-prop markets over 48 games, $694k listed; we took $7.2k of credits against -$1.1k of fills. A 400-lot bought at 40c against a 5c fair on a dislocated escalator book -> no player props without a fair. The inactives and the contracts' non-participation rules -> the cutoffs. Other makers diluted us within hours, and our bids were the losing side. Type E: section 16 and `sports.md`.
- **11/04 post-vote election wave** and **11/06 2028 election wave** (1,827 programs; ~1,880 markets; first quotes 25 min after the ask).
- **11/07-11/08 pick-offs** (Jack: "the pickoffs yesterday were unacceptable"): baited thin books took ~$2.9k on the House night; 53 adding fills ~$4.3k that day. Section 1.
- **Two auto-listed overnight batches turned off** (w1107-mixed, w1108-mixed): the 5+-event rule, the info-risk screen, the gates.
- **11/11 2028 relight (w1111-elec2028):** 1 of 724 relit markets quoted under the unwind-only lock while the texts said "greenlit". Fixed by stamping, the dark check and registry waves (section 14).
- **11/11 Senate/Gov nominees (w1111-statenom):** auto-listed; 440 of 654 dark at first (not election-family members); the rate fell ~$4.4k -> ~$0.75k/day in a day as other makers crowded in; Jack's six size-ups by cost bucket; the 11/12 cross-event sweep (18 events, -$256 at marks), not bait -> the group breaker.
- **11/13 FX + metals "after economic events" (w1113-fxmetals):** quoted only through the weekend program; no fair at first (84 of 1,002 quotes 5c+ on the wrong side); ticker dates misread; tails screened out; the 8,000-order read doubled 3,462 sides; 76% under the $1 floor until sized up to 5x. Sections 11-12.
- **11/14 known shapes** (Jack: "yes for upcoming new shapes, notify me and it's my call so dont default to turning it on e.g. KXYTVIEWSHIGH. but default to running the wave for elections, after economic events, and things of similar shapes to what's been run on waves so far"): the `NEW` verdict for a shape no wave has run; the after-economic-events waves relight themselves (sections 0.1, 2, 12).
- **11/14 crypto after-events strikes (w1114-cryptoev):** quoting around the crypto fair; listed strikes had to behave like wave books; the size model overpromised (45 of 108); bank early at 1,500 a side took 230 -> 469 of 498 strikes to $1+; dropping banked strikes to 300 cut ~$1.4k -> ~$0.6k/day until Jack kept 1,500; then jump holds and restarts took about a third of its hours. Sections 10-12.
- **11/15 one name across states, and the ladders' protections:** a taker bought Mark Kelly YES state by state (KXDPRESPRIMARY, 7 states in 80 s) -> the candidate breaker and the batched fast pull (736cc94). None of that reached the crypto / FX ladders -> the fair hold pull (a held coin's or pair's orders all at once) and a cross-event group for every wave series (2063782); the $1-floor check reads the bot's own fair (crypto: offs only). Sections 1.2, 11, 12.
- **11/18 FX relight: the wall race** (336 strikes, $17.57/day each, 2 contracts ever traded).
  - 211 were denied "under bar" against 30-230k-contract walls -> `scan_max_mult` (Jack: "1.5-3x the size").
  - The floor check turned 99 off on first-hour reads -> the `held` and `restoring` rules.
  - The walls stepped up four times in four hours -> cheap tiers to 15c.
  - One-sided strikes -> the far-from-fair guard exemption and a 0c FX edge.
  - The dash's one-cycle pacing -> the 15-min mean, the Latest relight row, the One side or both table.
  - Near-fair quoting modelled and declined ("No, stay at walls").
  - Result: 313 of 336 at 2%+ share. Sections 6, 11, 12, 14; `history.md`.

Background reading, only when time allows: memories `jack-working-style`, `imm-quoting-triage`, `market-wave-playbook`. Lookup tables (tools, list fields, bot knobs, deploy safety, gotchas): `reference.md` beside this file. Sports (type E): `sports.md` beside this file.

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
| **E. Sports** (game props, season props, esports) | player-stat ladders / escalators, head-to-heads and game markets listed 1-3 days before a game; season props and MVPs; settle on a box score or a decided round | 2 (detect), then `sports.md` section 4: a sports family quotes it (`MEMBER`) or it is Jack's call (`CHECK`); never on the wave list (the families carry the game clock and the news rules) | the family's own (NFL ladders 400 a side, auto families x3); changing it is Jack's word | NFL LADDER / ESCALATOR 9/24-10/20 (`sports.md` section 5) |

**Known shapes run by default; a new shape is Jack's call** (Jack 2026-11-14: "yes for upcoming new shapes, notify me and it's my call so dont default to turning it on e.g. KXYTVIEWSHIGH. but default to running the wave for elections, after economic events, and things of similar shapes to what's been run on waves so far"):
- **Type A:** auto mode lists the detector's FIT / NEAR series with 5+ events (its NEAR group included), and FIT / NEAR now need a KNOWN SHAPE: a Kalshi category Jack has run as waves and kept (`KNOWN_SHAPES` in `wave_detect.py`: Elections, Politics; the auto waves he turned off, w1107-mixed / w1108-mixed, don't count). Any other category that would be FIT / NEAR is `NEW`: held for him and TEXTED, never auto-listed (KXYTVIEWSHIGH: Entertainment, settles on charts.youtube.com). Adding a shape is his word: add the category to `KNOWN_SHAPES`.
- **Type B relights itself:** the after-economic-events registry waves list each new batch of their own series in pass 7 (the detector calls those series LISTED): w1113-fxmetals with no stop cap, any time its programs run (Jack 11/14: "after-economic events can quote anytime not just when markets are shut, but need a fair value"), on `wave_fair.py`'s fair (its Friday close rolled each weekend; the weekday mode while FX and metals trade, section 12); w1114-cryptoev with no stop cap, gated by the bot's crypto fair. Nothing rests on a strike without a fair. Each relight starts on the wave's current args (bank-early, floor_auto, margins). A ladder no registry wave lists (yields, a new asset) is CHECK: Jack's call. Auto mode never creates a type B wave.
- On 11/13 the metals came back FIT (Commodities) and only the auto run's own judgment kept them off a no-fair default listing: since 11/14 every `KX<asset>EVENT` ladder is LISTED (its wave relights it) or CHECK.
- **Type E (sports) is never a known shape.** Sports is a risky category, so a sports series reads `MEMBER` or `CHECK`. `MEMBER`: a sports family in the bot quotes it on its own clock, fair and news rules (the ladder / escalator name pattern, the NFL and league auto families, the MLB MVPs). `CHECK`: Jack's call. Auto mode lists neither, and no sports series goes on the wave list, which has no kickoff clock, news hold or player fair (`sports.md`).

**What the skill may do on its own (manual or auto mode):**
- List FIT series on the `default` preset, and NEAR series as the wave's NEAR group (the 11/04 midterm treatment).
- Start the dashboard, run the tune, verify, run the pick-off watch.
- Stand a picked-off REGISTRY-wave market down at its base size (`pickoffs --act`), and turn off a wave it just added that didn't go live (`pvd_waves.py stop`).
- Exclude SOON / CHECK / NEW / MEMBER / FEW series, and say so.
- Run the read-only `pace`, `whydark`, `darkcheck`, `report`, `resize` WITHOUT `--apply`, and `wave_detect.py --upcoming` WITHOUT `--update-state` (section 2.1), any time.
- **The $1-floor protocol (section 11) on a type B wave with a live fair, in MANUAL mode** (Jack 2026-11-14: "Yes, pre-approve"): run it without asking first, then report what it did. Bank early up to 5x the base (`setarg NAME bank-size` / `bank-max`, e.g. 1,500 on a 300 base) until $1.30 is banked; `resize --apply` up to 5x aiming at $1.20; off under $1 only on two reads. Only while the fair is live (FX / metals: a fresh `--fair-file` from `fair_cmd`; crypto strikes: `crypto_event_fair`) and preflight passes. Never in auto mode, never above 5x, never on a market Jack turned off, never on a wave without a fair.

**What needs Jack's yes** (offer it in the report; never do it on your own):
- `--no-halts` or `--halts-on`.
- Any size-up: `--size-mult`, `--series-tier`, `--cheap-tiers` above the default (mid-price tiers included), a bank size above 300, a pin above base, `resize --apply` -- except the $1-floor protocol on a type B wave with a live fair, pre-approved 11/14 (above).
- The `2028` preset.
- Extra budget above +$100k (auto mode: never above +$50k).
- Quoting SOON, CHECK, NEW or FEW series, an info-risk title, or a type B ladder outside its registry wave's own relight (a new asset, yields).
- Anything that changes how a sports family quotes (type E): its size, its asks or cash switch, a cutoff, its fair or news gate, enrolling or cutting a series, quoting a `CHECK` sports series.
- Adding a shape to `KNOWN_SHAPES` (a NEW shape he runs and wants by default from then on).
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
    - `wave_alert.py upcoming --upcoming ... --send` (pass 11 sends it: section 2.1)
  - Without `--send` it only prints a preview.
- **Texts and email are the only routes that reliably reach Jack.** A PushNotification from a scheduled run never reached his phone. Send the PUSH line anyway.
  - Texts go through `sms_alert.py --long` (<= 900 chars), capped 1 per 10 min and 10 a day (exit 2 = capped, fine).
  - It texts for: every quotable wave (exit 3), live or not; an UPCOMING wave that would be greenlit (pass 11, section 2.1); a relight of 100+ markets or a 1,000+ program jump; a pick-off of $100+ new, a series trip or a stand-down; dark-check and bait-watch alerts (section 14).
  - It emails Jack's inbox (wave_alert.py's ALERT_TO) on every trigger.
- Jack can answer by text (task "KL sms-inbox" / `phone_actions.py`): pause-overnight writes `run-logs/pvd-scan/OVERNIGHT_PAUSED` and the check stops on its own. Never delete that file.
- Then end. Jack tunes later in a normal session.

---

## 1. Pick-off rules (read every time)

### 1.1 What happened (numbers: `history.md`)

- **11/07-11/08:** every wave fill since 11/04 netted -$4,094 (losing fills -$6,033). Baits on thin books (a lone ~200-lot 9-37c off fair, our rung re-anchored to it, a taker of exactly our size, the bait pulled) cost -$3,653 in 19 fills; real repricing -$550; a cross-market sweep of 90c+ sides -$352; a halt-restart rejoin -$89; a one-event auto-listing -$26. Nothing alerted; the halts were not the bait defense.
- **11/12 (w1111-statenom), a sweep, not bait:** one seller took the whole top bid level from every maker in 18 KXSENATENOM markets in 21 min (both parties' favorites, 32-91c). Our 17 full fills + 2,071 @ 9.7c (39% of that level after the 6-15c size-up): -$130 at marks, -$256 by evening. The per-event sweep breaker can't stop a taker that moves to the next event -> the group breaker.
- **11/13-11/14 (type B):** with no fair, 84 of 1,002 FX/metals quotes sat 5c+ on the wrong side (if all filled: -$1,602), and the pick-off watch marked a -$12 fill +$56 off a 26x95 book. The 8,000-order read under a 12,000 cap left 3,462 sides doubled for ~2 h (2 doubled fills, -$34). Crypto: 4 fills in ~27 h (three ETH takes of 11-23 lots; a 1,000-lot SOL bid at 9c in the 11/14 21:58Z selloff, ~1c under the fair, +$44 by the next morning).
- **11/15 (w1111-elec2028), one name across states:** a taker bought Mark Kelly YES at 2c in KXDPRESPRIMARY state by state (AK, CA, CO, CT, HI, IA, ID in 80 s): 1,600 contracts off our asks, about -$24. Every state is its own event; the group breaker tripped on CA 29 s after AK, but its 1,063-order pull ran at 120 cancels a minute in no set order, and five more filled 4-50 s after the trip -> the candidate breaker and the fast pull (736cc94).

### 1.2 The defense stack (live)

| Layer | Where | What it does | Evidence |
|---|---|---|---|
| Close clamp | bot, 340fbe9 + 3032ea8 | The side that REDUCES a position rests no more than 5c past its 6 h usual price toward the taker, or at breakeven or better where that reaches further (Jack 11/08) | Every reducing-side bait fill of 11/07 to 0 |
| Wave unwind | bot, 85a5d8e + 95eef3b | While UNWIND_ONLY_WAVES exists, a HELD market still on the wave list rests one closing order for the whole position at the outside price: never better than the best outside 20+ lot (no lead), at most 5c past its usual price toward the taker or at cost where that's further (the close clamp's bound: no bait), at cost while it has no usual price; its adding side rests 0. A market moved off the list (`stop`, "stopped") is not unwound: its position rides. Jack 11/12: "unwind, doesnt have to be at breakeven but dont lead", "and make sure unwind positions also dont get baited". Closing orders ride through restarts (11/13: "Yes, keep them"). Back out: `IMM_UNWIND_WAVES_AT_COST=1` | What-if at deploy: -$3.0k realized if all filled, already marked |
| Off-usual size | bot, a0e19ff | A listed side resting 3c+ toward the taker past its 6 h median EXTERNAL touch rests only base size; cold start (under 4 samples, ~1 h) base too (crypto strikes 400, 1f68841). It also holds Jack's size-ups at base while other makers bid the prices up (statenom 11/12: 282 of 365 sides). Exempt since 8281473: a wave side 10c+ (20c until 32bd17c) on the safe side of its live FX / crypto event fair (section 12, the wall race) | $3,041 of the House-night losses |
| Re-anchor cap | tune, every 15 min | Toward the taker a limit moves <= 5c a run, held a run after a 10c+ jump or an empty side | $3,003 |
| Join wait + rest memory | bot, 4903ecb + a0e19ff | Joining a better price waits 2 s per $ at risk ($50 free), penny and 0.1c books; a side back after a halt starts from its last rest. Exempt since 8281473: a wave side 10c+ on the safe side of its live fair (`IMM_FAR_FROM_FAIR_C`) joins at once | $1,160-2,386; 11/12: 112 better prices pulled before we joined |
| Lone-ref guard (waves) | bot, 3032ea8 | A listed side whose reference is a lone thin top (< 3 x target/5, 20c+ off the rest) our rung jumped to stands aside. At go-live after darkness every single-maker top counts as a jump: 37 KXPRESSTATE sides (11/11), ~500 statenom sides at its listing and ~155 by the afternoon -- expected, leave it | Logged before every House bait |
| Twin check | bot, 6fbad53 | Jack 11/11: "make sure the quotes on each side make sense relative to each other". A bid rests at most 100 - the siblings' bids (+2c); a D/R ask at least the other leg's NO bid (-2c); re-priced, never cancelled. Treats a 2-candidate nominee event as a pair (asks move up only) | $736 of the 2028 losers; 0 re-prices when legs agree |
| Twin block | tune | A D/R pair's same-bet leg rests nothing once one leg holds 200+ | |
| Group breaker | bot, 76d9201 + 25cbd54; wave series groups 2063782 | The 2nd fill of 200+ on the same side in a DIFFERENT event of one group within 10 min holds that side of the whole group 20 min (Jack 11/12-11/13: "after the 3rd fill and hold for 20min", then "on the 2nd fill"). Groups: NFL games, Carbon Arc, corn yields, US general elections, state legislatures; since 11/15 every other wave series is its own group ("wave KXBTCEVENT": a coin's strikes in its other events, an FX pair's, a politics series), but a no-halts series. `IMM_SWEEP_GROUP_MIN_EVENTS=3` backs it out; `IMM_WAVE_GROUPS=off` drops the series groups | The 11/12 sweep: ~$117 of -$130 avoided, ~$10 of rewards a trip. Series groups replayed 11/06-11/15: 0 trips |
| Candidate breaker | bot, 736cc94 | A maker fill of 200+ on a name in a per-state series (KXDPRESPRIMARY-28AK-MKEL) holds that name's side in every state 10 min, pulled at once; the other names, the other side and other series keep quoting. Only the presidential primaries have that shape | Replay of 11/15: 1,300 of the 1,600 Kelly contracts never fill |
| Fast pull | bot, 736cc94 | Breaker pulls and the cycle's cancels go 20 to a batch DELETE, paced to the write tokens; a pull goes most at risk first (the hit name in its series, then its series, the name elsewhere, the rest), at most 20 s. `IMM_PULL_BATCH=1` = one by one | 1,063 orders ~9 s (was ~85 s) |
| Wave breaker + 200+ tape trigger | bot | Two 200+ fills at 90c+ in 10 min hold 90c+ rungs 15 min -- NO-HALTS series only, so it misses default-preset waves | $207 on 11/07 |
| Sweep breaker, tape trigger, toxic halts, bait-fingerprint stop | bot | Pull the event or side after a cleared order, a 100+ burst, two pick-offs, or a whole-rung bait fill | Per event only |
| Fair limits (type B) | bot `fx_event_fair` (FX + metals, 70d05b7 + d736cc4) and `crypto_event_fair`, live every 15 s; tune `--fair-file` underneath | Bids <= fair - margin, asks >= fair + margin, live every cycle (the margin: section 12). A strike stands aside -- rests NOTHING, weekend or weekday -- on a read 180 s old, a jump hold, no kept close or (weekdays) 12 h to its measurement; the weekend's 2c / 98c floor is only for strikes with a fair | 0 of 650 FX quotes wrong-side after |
| Fair hold pull | bot, 2063782 | A coin or FX / metals series in its jump hold or on a stale read has EVERY order on it pulled at the feed's next look (~0.5 s), in batches, and nothing placed or amended on it until it is back. It was strike by strike as the quote loop got there: BTC's 02:05Z 11/15 hold took 95 s for its 128 orders. `IMM_FAIR_HOLD_PULL=off`; log `fair hold pull: crypto BTC (...)`, sink `fair_hold_<date>.jsonl` | Built 11/15 |
| Earn only | bot, 1fd880b (another session, 11/15) | The quote loop's last step: no rung rests that the program's 1,000-contract walk would not pay, except on the side that reduces a position (Jack: "unless purposefully unwinding, no IMM should quote without earning"). `IMM_EARN_ONLY` on / dry / off | 13:49Z 11/15: 273 markets resting earned nothing, 258 of them reduce-only |
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
   - FIT or NEAR only, which means a known shape (`KNOWN_SHAPES`: Elections, Politics; a `NEW` shape is Jack's call); no company KPIs; nothing on the blocklist.
   - Info-risk titles are Jack's call: personnel, appearances / announcements, counts of reported events, pre-taped TV, company reports.
   - Ask "what moves this price, and can someone see it before our resting order moves?" "Truly far" means the OUTCOME date, not Kalshi's settle date.
   - Live-price ladders (type B) only with a fair (Jack 11/14: "after-economic events can quote anytime not just when markets are shut, but need a fair value"): the bot's live fair every 15 s (FX / metals `fx_event_fair`, crypto `crypto_event_fair`), `wave_fair.py`'s file underneath for FX / metals. A strike the fair stands aside rests nothing, weekend or weekday; the 2c / 98c weekend floor is for strikes with a fair.
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
- **A live fair can still be late:** the bot reprices every 15 s and requotes in ~1.5-2.5 min, so a jump inside that can fill before the limits move; a source that stops stands its series aside (USD/MXN went dark twice 11/14-15 on Bitso's wide book). The jump hold, the fair hold pull (all of the underlying at once, 2063782) and the WS fast cancel are the cover.
- **Jump holds cost quoting time:** a hold re-arms on every read while the price stays 1% off its 5-min range, so a slide holds a coin the whole way down. 11/14-15: at least one coin was held 36% of the time (HYPE 30%, ETH 19%, SOL 18%, BTC 8%: one 1% trigger for every coin, and HYPE moves ~1.9x BTC); hours with 20-48 min of holds banked $5-23 against $50-55 clean. A per-coin trigger is Jack's call; measure what the holds avoided first.
- **One-event news risk isn't detected:** e.g. a pending appointment decides a nominee event (KXSENATENOM-TNR-28 waits on Blackburn's appointee: its 12 markets are pinned at 0 on w1111-statenom since 11/14, Jack's "Yes, do 1-5", until she names someone).
- **A sweep's fills can trip a series stand-down hours later:** the pick-off watch's 6 h lookback flags a fill the first time its mark is 5c+ and $25+ against us, so three of the 11/12 sweep's fills crossing the bar as the books drifted pinned ALL of KXSENATENOM at 200 a side (01:30Z 11/13, a series trip), so his tiers stopped reaching the series until he lifted it on 11/14 ("Yes, do 1-5"; the per-market pins stayed). Removing a stand-down is his word.
- **Heightened bait mode is dry; the wave dashboards' marks include our quotes.**
- **Sports ladders outside the NFL have no fair.** The name pattern admits NBA / NHL / MLB / college ladders and escalators at the NFL size with only safe-join between them and the touch: the 10/03 failure mode. NFL single-game members fail closed while no ladders are listed. The sports families have no pick-off watch (`sports.md` section 7).

---

## 2. Detect and size up

```
python wave_detect.py --since <ISO: 30 min before the first new program, or the state's last check> --json <scratch>/detect.json
```

- **What it reads:** every active LIQUIDITY program that started since `--since`, minus markets already on the list or quoted by the bot.
- **Per series:** markets, events, pool $/day, median horizon, days out, listed / quoted counts, median spread, category, title. Horizon = the EARLIER of `close_time` and `expected_expiration_time`.
- **Verdicts, in order:**
  - `LISTED` / `QUOTED`: already handled. LISTED = on the list, or a series of an ENABLED registry wave whose stop cap hasn't passed (pass 7 lists its new periods; a relight after a gap says so in `why`). A wave Jack turned off no longer counts (its series are judged like any other).
  - `MEMBER`: the bot's own family admits it; its gates decide (triage with `imm-quoting-triage`; the sports families: `sports.md` section 2).
  - `FEW`: fewer than 5 events lit. Never listed or alerted.
  - `CHECK`: Jack's call -- a high-info-risk category (Sports, Crypto, Financials, Climate and Weather, Economics), a mention series, an info-risk title (`INFO_RISK`), a known shape settling on a live feed (the bot's own `SCAN_LIVE_SOURCE_KEYWORDS`; Jack 09/24), an after-economic-events ladder no registry wave lists, or blocklisted. A sports `CHECK`: `sports.md` section 4.
  - `SOON`: settles within a day, or no date. Jack's call.
  - `NEAR`: settles in 1-14 days. Quotable as the NEAR group.
  - `NEW`: would be NEAR / FIT, but its category isn't a known shape (`KNOWN_SHAPES`): Jack's call, texted (its tags and any live-feed source in `why`; `dated` says which it would be), never auto-listed.
  - `FIT`: long-dated (>= 14 d), a known shape, and none of the above.
- **Wave bar:** new FIT + NEAR series with 5+ events each total >= 40 markets or >= $1,000/day of pools. Exit 3 = a wave; 4 = only CHECK / SOON / NEW over the bar (tell Jack; list nothing; `wave_alert.py` texts when the NEW shapes alone clear the bar, `new_shape_over_bar`); 0 = nothing. A series triggers once (state file).
- **Batches:** size up EVERY start since the last scan, not just the batch Jack named.
- **The "after economic events" ladders** (`KXGOLDEVENT`, `KXUSDJPYEVENT`, `KXBTCEVENT`, `KX10YRRATEEVENT` ...: "above X one minute after a release") are type B (section 12). Their registry waves relight them (LISTED); a ladder no registry wave lists (yields, a new asset) is CHECK, Jack's word.
- **A big RELIGHT jump** ("programs 6903 -> 10573 (+3670)") is new programs, not relit ones: say what they are by series family and which the wave check already judged (11/13: 3,169 after-economic-events held for Jack, 501 small series the bot's selection picks up itself).
- **If Jack named the series,** take them as the scope (his word overrides FEW / CHECK), but still run the detector and report each verdict.

### 2.1 Upcoming waves: advance notice (pass 11)

Jack 2026-11-14: "market-wave skill should also scan upcoming rewards markets and callout if a wave would be greenlit so i have advanced notice".

```
python wave_detect.py --upcoming [--horizon-hours 72] [--preflight] [--json OUT]   # read-only; --update-state is pass 11's
```

- **What it reads:** Kalshi lists programs before they start (`GET /incentive_programs?status=upcoming`): mostly the next 05:00Z roll plus odd-minute starts, a few up to ~2 weeks out. Per market only its NEXT program counts (the bot's `upcoming_by_market`); markets not open yet are read by ticker.
- **How it judges:** the live check's verdicts, 5+-event rule and wave bar, per START WINDOW (programs starting within 30 min of the window's first start: what one 20-min check sees together), with settle dates measured from the start. A series the live check alerted in the last 30 days never re-triggers, so it doesn't count (the email lists it under ALERTED BEFORE).
- **Calls:** new FIT / NEAR over the bar = WOULD BE GREENLIT (exit 3): at the start, auto mode lists it if `preflight --auto` passes then. Only CHECK / SOON / NEW over the bar = YOUR CALL (exit 4): a NEW shape (KXYTVIEWSHIGH) is TEXTED, with `preflight` run now as if he said yes; the rest email. A RELIGHT over the bar (exit 5): an active registry wave's series whose markets aren't on the list now (the after-economic-events waves' next batch, a weekly election relight) lists at the start by itself (pass 7, on the wave's current args): email with `preflight NAME` run now, texted only when that FAILs. A renewal of markets listed now is not a relight.
- **`--preflight`:** `pvd_waves.py preflight --auto` NOW on each newly noticed wave, so a FAIL (shard cash, resting room, scan budget) shows while it can still be fixed. Moving cash is Jack's: give him the command.
- **Pass 11** (relight task, every 15 min) runs it with `--preflight --update-state`, then `wave_alert.py upcoming --send`: TEXT + email when a wave would be greenlit (line 1 "UPCOMING WAVE Sun 12:00 AM ET (in 11 h): N events would be greenlit, M denied", the reasons, preflight now, names) or a NEW shape would be held for him ("UPCOMING, YOUR CALL (new shape) ..."); email only for any other your-call window and for a relight ("UPCOMING RELIGHT ..."; texted when its preflight FAILs). Each series is noticed once per start (`run-logs/pvd-scan/wave_upcoming_state.json`; the live check's state is only read). At the start the 24/7 check still sends its own WAVE alert.
- **On a notice, in a session:** check the preflight line; read each greenlit series' title for info risk (1.3 rule 2) and its events for one-announcement news (section 3); for type B, build the fair; then tell Jack what lists when, on which preset. Keeping a series off before its start is his word, and there is no per-series skip yet: his "pause overnight" text stops the check, or `pvd_waves.py stop NAME` right after it lists.
- **Not covered:** a program Kalshi creates at its start never shows as upcoming; renewals the bot already quotes read QUOTED (the bot's own admission of upcoming programs is the 6:45 email's table, `imm_upcoming.py`).
- **First read (11/14 18:53Z):** 1,217 upcoming programs, 1,155 markets in 20 windows over 72 h: no wave. KXYTVIEWSHIGH (YouTube highest daily views: 140 markets, 16 events, $1,000/day, 11/15 16:00Z) was the one FIT; since the known-shapes rule it reads NEW: texted, held for Jack, never auto-listed.

## 3. Choose scope (fixed rules, then report)

- **In:** every FIT series with 5+ events. Series Jack named are in whatever their verdict, but report it.
- **In, as the NEAR group** (`--near`): every NEAR series with 5+ events. Dead 120 / live 80 a side (300 on a decided side), the bot's kind cushions (dead 2c, live 5c off the last trade), band 3-97c, no tiers / multipliers / bank-early. Jack: "near series should have the dead/live book distinction, and 120/80 base contracts like in the midterms".
- **Out:** FEW, CHECK, NEW, SOON, MEMBER, QUOTED and LISTED, with the reason and the $/day left out, so Jack can say "add X".
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
| Shard-0 cash | Under $1,000 (`PVD_MIN_CASH0`; $5,000 until Jack 2026-11-19: "Waves stop auto-listing under $1,000 free") |
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
- **What it shows (11/18 additions, Jack: "clearly show this data for the latest relit wave", "build into the dash how many markets are one sided vs two sided"):**
  - **Latest relight** tile row on top (4d59e4a): only the markets on the NEWEST program period -- rewards banked this period (+ projected by the stop), trading P&L / fills / cash held on this period's fills only, rate, quoting (how many we quote on both sides vs one), resting exposure, pacing vs $1.20 (`PVD_WATCH_LATEST_TARGET`) and $1. The page's own tiles below ("Whole wave, every period since go-live") mix every earlier period's rewards, fills and positions (11/18: "$2,774 so far" ran from 11/13; its 2 fills were 11/15-11/17 events).
  - **Pacing this period** ($1.30 with 6 h spare ... under $0.75) and **One side or both** (06421a0): active markets by the sides WE rest (both / bid only / ask only / not quoting), each by $1.20+ / $1-1.20 / under $1. "Scoring both sides" elsewhere means the book's qualifying sides, not ours.
  - **Fills and cash by price band:** by COST per contract (a bid its price, an ask 100 - its price), so a 95c ask is a 5c side; nothing over 50c is normal on a walled ladder (every side we rest is the cheap side of its strike, and the fair keeps the expensive side out).
  - **Shares are the 15-min mean** of the bot's cycle rows since 5265a34 (one cycle swings 0 <-> 9%: at 22:42Z 11/18, 40 of 112 "under $1" were one-cycle dips). `pace` is still the tool for sizing decisions (section 11). Its open P&L marks with our quotes in the book: use `pickoffs`.
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

**Off means both side sizes 0.** The bot reads `"size": 0` alone as its default size (PVD_SIZE, 40 a side): the 341 FX markets "turned off" that way kept 40-80 a side until the tune wrote `bid_size`/`ask_size` 0 (fixed 11/14: a 0 pin or a 0 multiple now writes both). A listed crypto strike goes off like any other: a 0 multiple keeps it listed at 0 a side (how `floor_auto` stops it). Moving its entry to "stopped" instead UNLISTS it, and an unlisted strike falls back to the family's own quoting: to keep one off for good, `crypto_event_stop.json` too (e4dd1f4, by hand; it covers strikes the wave never listed).

**A decided side** (last trade >= 90 / <= 10) ignores the band AND the limits: to stop such a market, `--pause-at SERIES:0` plus `--side-cap SERIES:0:0` (KXJEFFRIESFIRST 11/11). Keep it listed: delisted, its family could quote it.

### Levers (`pvd_wave_tune.py --help` lists every flag)

| Lever | Flag | Notes |
|---|---|---|
| Bank early | `--bank-size 200 --bank-max 300 --bank-by-hrs 6 --bank-usd 1.30 --bank-url <dash>/data --no-drop --hold-max [--base-size N]` | Needs the wave dashboard; sizes only ratchet UP within a period; the bank step waits 25 min after a re-size. Short windows: bank big early and KEEP it: `--base-size` = the bank size (crypto 1,500 / 1,500 since Jack 11/14 "Yes, keep 1,500"). A retune of the bank args must keep `--no-drop` or that base: dropping 1,500 -> 300 after $1.30 cut each strike's share ~85-95% (crypto ~$1.4k -> ~$0.6k/day, 11/14 14-18Z) |
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
| Fair limits | `--fair-file PATH [--fair-margin 3 --fair-vol-err 0.25 --fair-floor 2]` | Bid <= fair - margin, ask >= fair + margin (margin = base + vol-error term); no fair / stale = floor sides only (the bot's live fair, when on, stands such a strike aside entirely). Put the margin in the fair file's "margin" so a pass started earlier can't revert it. Registry `fair_cmd` refreshes it before each tune |

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
| Under-$1 count jumps on a type B wave | the touch vs the cheap tiers (did the walls step up?) | The wall race, section 12: extend the tier (Jack's word; each step drew the next one on 11/18) |
| One-sided / dark on a fair-gated wave | `whydark` + the bot log's `earn-only` / `join wait` / `off-usual size` lines | "Fair clamp": leave it if the missing side would trade at or through the fair. Guard lines on sides 10c+ from the fair: the far-from-fair exemption (8281473; 10c since 32bd17c) covers them |
| Markets go off right after a tier / size change | the pass order: scan -> resize -> tune | Lift their 0s after that pass (section 11, "the pass order") |
| Total rate dropped | restart / re-size times; others' size at our levels | Restarts and re-sizes read low ~15-25 min; competition: re-read, size by measured share |
| Fills cluster / a pick-off | `pickoffs NAME`, the tape | Pin at base (`--act` does it); a series trip = tell Jack |

## 11. Size to the $1 floor

Kalshi pays a market's period only from $1; under it the period's share is lost. Jack, 11/14 on both short waves: "size them up", "go up to 5x every market, to get them over the $1 threshold", "after these increases, stop quoting markets still pacing <$1", "like prior market-waves, bank as much size early to be safe", "quoting time is valuable ... so prioritize quoting time"; and 11/11: "in general as a rule of thumb when sizing to hit threshold, aim for $1.2". The bot's own floor boosts keep their $1.10 re-aim trigger (Jack 11/11: "Keep the $1.10 trigger").

```
python pvd_waves.py pace NAME [--aim 1.20] [--window-min 60] [--json OUT]       # read-only
python pvd_waves.py resize NAME [--aim 1.20] [--max-mult 5] [--keep-within 0.05] # dry run: the plan
python pvd_waves.py resize NAME ... --apply                                      # Jack's word
python pvd_waves.py set NAME floor_auto on|off                                    # the same, every 15-min pass (Jack's word per wave; adds --size-mult-file)
python pvd_waves.py set NAME floor_max_mult 1                                     # the check only turns markets off (a wave already at 5x)
python pvd_waves.py set NAME scan_max_mult 2                                      # size-up to the bar at LISTING (Jack's word per wave; needs --size-mult-file)
```

**Size-up to the bar at listing (`scan_max_mult`, Jack 2026-11-18 on w1113-fxmetals: "1.5-3x the size"):** the scan judges a market still under the $1.10 bar after the cheap tiers again at the wave's bank size (`--bank-max`, where the tune's bank step rests every market that can't bank at the flat size), then quarter steps up to `scan_max_mult` x that; the first that clears lists it and goes into `size_mult_NAME.json` (never lowering one, never lifting an off). The relight alert says how many listed only sized up, and what the denied ones were judged at. Set on w1113-fxmetals at 2 (11/18): 1,500-3,000 a side at 3-5c, 3,000-6,000 at 1-2c. 1 = off; at most 5.

**Walls move: the cheap tier's edge is a cliff.** A side past the wave's last cheap tier falls back to the base size. When other makers step their walls over that edge, a whole batch drops under $1 at once. The 11/18 relight's sequence, and what was done each time, is section 12's **wall race**.

**The share target (Jack 11/18: "let's try to get at least 2% share of each book and hit that $1 threshold"):** a 2% share of a $17.57/day pool is ~$1.14 over 3.25 days, so the floor check's $1.20 aim is roughly 2.1% with ~77 h left. Check it on the 15-min MEAN of the bot's `est_frac` per strike (cycle log rows since the last restart or size change), never one cycle, and classify every strike under it:
- off (multiple 0);
- one side only (the fair clamp on the other: say whether that side would trade at or through the fair);
- held at base by the off-usual cut (a side under `IMM_FAR_FROM_FAIR_C` from its fair);
- past the last cheap tier;
- landing.

11/18-19 on the 336-strike FX relight: 306 at 2%+ at 23:35Z (median 3.8%); 313 at 00:41Z (median 4.2%). The 23 under were 2 off, 2 one-sided UK GDP strikes, and 19 with a side within 20c of its fair or past the 15c tier.

On those numbers the far-from-fair threshold went to 10c (32bd17c, "Yes, 10c"). But before it went live, 328 were already at 2%+ (01:41-01:56Z): the off-usual cut had let most of the held sides go once its 6 h median caught up with the walls. Over 02:04-02:18Z, 333 were at 2%+ (median 4.2%). Only 2 of the 73 sides 10-20c from their fair got bigger. The 5 that crossed 2% did it on the books: an ask wall thinned 44.9k -> 27.2k, and three deep MXN strikes sit near 2% at full size. The 3 under were UK GDP 12/11 strikes turned off. **Re-measure a held strike just before proposing a guard change:** the off-usual cut releases on its own once its 6 h median catches up, about 3 h after a step-up.

**The pass order (11/18):** pass 7 runs scan -> fair -> resize (the floor check) -> tune. A tier or size change set before a pass lands only at that pass's TUNE, so its resize still judges the markets at the old size and can turn them off. 11/18 this happened twice: 99 strikes off at 19:48Z on first-hour reads, and 20 off at 21:01Z just before the 10c tier applied. To lift them:
1. Compute each one's needed multiple on the current book: the scan's model, at the bank max x the tier x quarter steps up to 2.
2. Write it into `size_mult_NAME.json` under the wave's pass lock: 1 = drop the key, else the multiple. Lift only sides whose bad read came from a size mismatch -- cheap-tier sides, where the base differs from the listed size. A live book rests its base on both sides, so its read was fair.
3. The next tune restores `pre_off`. Until it does, `pace` reads the market as "restoring" (3317216), so no read turns it off again.

**How `pace` measures** (`pvd_wave_pace.py`):
- Projection = banked this period (the dashboard's `period_est`) + the AVERAGE rate since the market's size landed x the hours left. Not one cycle: the bot's per-cycle share swings ~2% <-> 9% on wide books and reads 0 for a cycle after a stand-down (FX 13:45Z 11/14). A cycle resting nothing counts 0, so the rate is uptime-weighted.
- Rows from 10 min after the bot's last start; a market resting under 90% of what the bot wants is "landing" and not judged. Within 15 min of a restart reads run low (crypto 11/14: 160 at $1+ seven minutes after one, 230-286 before): `pace` warns, `resize --apply` refuses without `--force`.
- **Held = not judged (c30a81f):** a market where the bot WANTS under 90% of the list's size on a side it quotes -- its first hour on a market (the off-usual cold start, under 4 touch samples 15 min apart, rests the entry's base: not the cheap tiers, not the multiple) or its off-usual cut -- is "held"; only rows after the latest held row count. 11/18: the 19:33Z and 19:48Z reads judged 99 new FX strikes at the 300 a side the bot rested in its first hour, against 1,500 listed (the floor check's "5x" there is 7,500), and turned them all off; one it sized up instead went 0.48% -> 2.35% at 1,500. A live book's sides rest the base itself, so its first-hour reads are fair.
- **Only cycles since the last size change (3317216):** a market's rows start at its list entry's `scaled_at` / `unpinned` stamp, and an entry still pinned with its `pre_off` sizes while the multiples file says non-zero is "restoring" (not judged). 11/18 20:16:39Z: the 99 strikes just lifted from a 0 were judged on the half hour they rested nothing while pinned off, 3 s before the pass's tune restored them.
- Under-$1 tables: by the multiple each needs for the aim (the model above: an upper bound), with one-sided counts and the cash added if every added contract filled; by side cost (1-2c, 3-5c, 6-10c, 11-20c, 21-35c, 36-50c, 51c+: what Jack asked for 11/14); by series.

**The protocol:**
1. **Bank early, as big as the window needs: pre-approved up to 5x the base on a type B wave with a live fair (Jack 11/14: "Yes, pre-approve"); elsewhere a bank size above 300 is Jack's word.** On a 1-2 day window the most size from the start paid (crypto: 1,500 a side from the start took 230 -> 469 of 498 strikes to $1+; keep that size after banking: section 10's bank-early row). Cold starts cost quoting time: crypto strikes rest 400 instead of base while they have no usual price (1f68841).
2. **Read `pace`** once sizes have rested and 15+ min after the last restart.
3. **`resize` dry run, then `--apply`** (pre-approved on a type B wave with a live fair; elsewhere Jack's word): the smallest quarter-step multiple up to 5x that projects $1.20 (or 5x if that still reaches $1); never lowers a multiple; an off market stays off.
4. **Two reads before an off:** a market that can't reach $1 even at 5x goes to 0 only when the previous `resize` read, 5-90 min earlier, judged it the same way. One read right after a restart would have stopped 258 strikes (11/14). A market projecting $0.95-1.00 is kept (estimate noise; it keeps its banked share).
   - **Every pass (`floor_auto`):** Jack 2026-11-14 on w1113-fxmetals, "Yes, every 15 min". With `set NAME floor_auto on`, pass 7 runs `resize --apply` (minus its own wave run) after the fair refresh and before the tune, so a market goes off on its 2nd pass in a row under $1 even at 5x. It needs a live fair -- a fresh `--fair-file`, or with none the bot's own (`crypto_event_fair.json` / `fx_event_fair.json` written within 5 min and listing the wave's series, 11/15) -- and a `--size-mult-file` (`set NAME floor_auto on` adds it); it skips within 15 min of a restart and on a stale dashboard read; never above 5x or the wave's `floor_max_mult`. w1114-cryptoev runs it since 11/15 with `floor_max_mult 1`: it banks at 1,500 = 5x its 300 base already, so only offs. An off stays off into later periods (Jack's "Keep them off" on FX): clear its 0 in `size_mult_NAME.json` to bring it back. `set NAME floor_auto off` stops it.
5. **Re-measure ~25 min after the sizes rest, and again at +2 h:** other makers answer with size (FX 11/14: within 2 h of the 5x size-up they added ~25M contracts against our 5M; median share 8.8% -> 6.3%, the rate $3.5k -> $2.5k/day; the 12:50Z re-read turned 523 more markets off).
6. **Say what it costs:** the cash if every added contract filled, by cost bucket; the expensive sides (21-50c) carry most of it.

What a 10x lift would buy, and what turning markets off frees, are not the same question: turning list markets off frees no collateral budget (section 7). Once Jack says "Keep them off", don't re-offer the lift for that wave.

## 12. Short-window live-price ladders (type B)

Kalshi's "after economic events" series: will gold / silver / copper, USD/JPY, EUR/USD, AUD/USD, USD/MXN, USD/CAD, GBP/USD, BTC / ETH / SOL / HYPE, or the 2/5/10/30-year yields be above X one minute after a release. Thousands of strikes, $20 a strike, ~1-2 day programs (11/13 batch: 3,670 programs, $52.7k/day of pools, programs to Sat midnight ET).

**The decision (Jack 11/13 "Do it" on this rule):**
- **FX + metals: any time, with a fair** (Jack 11/14: "after-economic events can quote anytime not just when markets are shut, but need a fair value"; it replaced the 11/13 weekend-only rule). w1113-fxmetals has no window and no stop cap: it lists whenever its programs run.
  - **The fair:** the BOT prices every strike every 15 s (`fx_event_fair.py`, thread "fx-event-fair", 70d05b7 + d736cc4, Jack 11/14 "Yes, build it"): wave_fair.py's lognormal on the kept Friday close, moved by the 24/7 sources; `fx_event_gate` + `pvd_limits` set each listed strike's limits every cycle. The tune's 15-min `--fair-file` limits stay underneath (`IMM_FX_EVENT_FAIR=0` falls back to them). Status `run-logs/incentive-mm/fx_event_fair.json`; log lines "fx-event fair (...)", "fx-event stand-aside" / "fx-event resume" (`whydark` names them).
  - **The margin (what sets the limits): 0c since 13ffefe, 11/19 00:21Z** (Jack 11/18, on one-sided strikes: "also 1c of edge is enough", then "0c edge is ok"). Bid <= floor(fair), ask >= ceil(fair) -- `fx_event_fair.EDGE_C`, env `IMM_FX_EVENT_EDGE_C` in cents.
    - `=off` restores the 11/14 margin: the 2c base + the vol-error cushion (100 x 0.25 x phi(z) x |z|) + on weekdays the requote term (2 sd of the fair's move over a 3-min requote), ~5-6c at the median. That margin was "kept" on 11/15 ("Yes, keep it") and superseded on 11/18: don't re-offer a margin above 0c on this wave.
    - A wave book never leads its book, so 0c only lets in walls that sit at or past the fair on OUR side of it (11/18: a 96c ask on a 95.4c fair). A wall that would trade THROUGH the fair still stays out: on 11/19 the UK GDP 12/11 bids were 9-13c over it and the MXN tail asks 4-9c under it. Those strikes stay one-sided, by design.
    - Give the whole gap on each side, never just the base (11/15: "wdym?").
  - **Standing aside (cancel; rests nothing):** a source read 180 s old (USD/MXN went dark twice 11/14-15 when Bitso's book went wide: d736cc4 refuses a wide book's mid), a jump hold (the source moved 0.35 daily sigma inside 5 min: 5 min out; the fair hold pull takes every order of the series at once), no kept close, a weekday strike within 12 h of its measurement (the release it settles on outweighs the model inside that), a strike the model doesn't price.
  - **Weekend vs weekday:** the weekend (Fri 5 PM - Sun 5 PM ET) keeps 2c / 98c floor sides on strikes with a fair; weekdays have no floor and add the requote term. Weekday mode starts Sunday 5 PM ET.
  - **The fallback file** (`wave_fair.py OUT --live`, the wave's `fair_cmd`): while FX and metals trade (Sun 5 PM - Fri 5 PM ET) `wave_fair.py` runs its WEEKDAY mode: the same model priced from now with its own staleness margin per strike (2 sd of the fair's move over the ~18 min between refreshes: ~8c at the money a day out, ~3c a week out -- the file's, not the bot's), nothing within 12 h of a measurement, "floor": 0 and a 20-min life. A simulated Wednesday on 11/14's books: 2,097 fairs, 393 strikes inside 12 h standing aside, staleness median 1.7c (max 8.3c). The margin lives in the file AND `fx_event_fair.MARGIN`; a test keeps them in step.
  - Like the crypto strikes, these series skip the lone-ref guard (a fair-gated series). `set NAME window fx-shut` brings the weekend-only rule back.
- **Crypto:** 24/7, so only with the bot's live crypto fair (`crypto_event_fair`, 1442295: the crypto fleet's GBM on Kraken spot, every 15 s; stale > 180 s = stand aside). THE JUMP HOLD: a 1% move inside 5 min holds the coin 5 min (fa0c63c, Jack 11/14 "Stay down for 5min"), re-armed on every read while it stays 1% off its 5-min range, then ~1-3 min to re-place; the fair hold pull takes all of the coin's orders at once. $0/day on the page during a selloff = holds, not a fault (`whydark` says "jump hold"). After a drop the books widen and our asks sit behind the new touch; the join wait walks them back a step a pass, so the rate takes ~10 min to recover. Touch markets ("hit by", early close) stay NO-only.
- **Yields:** skip (tiny pools).

**Setup:**
1. **Fair first.** FX / metals: `wave_fair.py OUT --live` (lognormal on Friday's 5 PM ET close -- metals = Kalshi's weekly settlements KXGOLDW / KXSILVERW / KXCOPPERW, FX = Yahoo -- moved by 24/7 venues: Hyperliquid trade.xyz perps for GOLD / SILVER / COPPER / JPY / EUR / GBP, Kraken for AUD/USD and USD/CAD, Bitso for USD/MXN), in the wave's `fair_cmd`, read by `--fair-file`; valid 45 min after each refresh (a failed source reuses its last price under 45 min old; stale = floor sides only). **The weekend rolls by itself (11/14):** `fx_shut_window` gives the close and the reopen; each weekend's closes are fetched once (metals: Kalshi's weekly settlement, ~80 min after the close; FX: Yahoo's last 5-min bar) into `run-logs/pvd-scan/wave_fair_closes.json`, and used only within 0.5% of the 24/7 source's own price at the close (11/13: all nine within 0.1%; 11/06: copper 1.06% off, refused). A close not out yet or refused = no fair for that series until a later pass gets it: its strikes rest nothing. Each 24/7 source's own price at the close is kept there too (Kraken serves 5-min bars only ~60 h back). Weekdays: section 12's decision above; the tune reads the file's "floor" and per-strike "margin_extra" (pvd_wave_tune.py, 11/14), and the wave's `--fair-floor 0` keeps an unreadable file at nothing. The 11/13 weekend keeps the closes entered by hand (Kalshi and Yahoo reproduce them within 0.03%). The margin: the decision above (copper 6c until Jack 11/14: "make copper and all metals/fx at the 2c margin"). Crypto: the bot's gate, nothing to add.
2. **Dates:** these tickers are DDMONYYHHMM (KXGOLDEVENT-17NOV261100 = Nov 17 2026 11:00 ET); the bot reads YYMONDD and drops them (27 of 807 got in on 11/13). `run --write` runs `datefix`, which writes `event_start_overrides.json` rows (hot-reloaded); check its output after the first pass.
3. **Wave books:** a listed crypto strike quotes like any wave book -- no fair lead, the wave's reference on both sides (20aca4e; Jack 11/14: "it should function like the other market-wave books") -- and its limits come from the live fair every cycle, not the 5c-a-run walk (28837b4).
4. **Cash:** metals and crypto trade on shard 2 (shared with the crypto bots); FX and yields on shard 0. Preflight checks both. Only Jack moves money.
5. **Capacity:** 2,000+ markets means the placement queue, the resting cap and the collateral budget (section 7) bind within the first hour.
6. **Size for the floor from the start** (section 11), pre-approved while the fair is live: bank early up to 5x the base at go-live; `pace` / `resize` once the sizes rest; two-read offs; `set NAME floor_auto on` at go-live (pre-approved on a type B wave with a live fair; the bot's own fair counts), with `set NAME floor_max_mult 1` when the bank size is already 5x the base; `crypto_event_stop.json` (by hand) for strikes the wave never listed (Jack 11/14 "Stop them early").
7. **Deploys:** batch bot deploys during the window: each restart pulls every coin ~1 min, re-queues the book and reads low ~15 min (8 restarts the night of 11/14; 6 more 21:52-02:12Z 11/14-15, several of them FX fair deploys).
8. **Check a fair change** (after a margin or model deploy): margins by series from the status file (`fx_event_fair.json` / `crypto_event_fair.json`: each market's `margin`, `bid_max`, `ask_min`); resting strikes by series from the newest COMPLETE cycle of the cycle log (`own_bid_ct + own_ask_ct > 0`; its `quoted` column reads 0 on every row); our tops (`own_bid_top`, `own_ask_top`) against the new limits, and `place` rows in `orders_<date>.jsonl` since the restart outside them: 0 expected. The bot loads a change only at its restart (reference.md, deploy safety).

**The wall race (11/18 relight, w1113-fxmetals: 336 FX strikes, $17.57/day each, 19:01Z -> 11/22 05:00Z).** The books are pure reward farms: these strikes had traded 2 contracts EVER. Other makers wall both sides with 30-230k contracts at the touch, sized in DOLLARS and pegged to the best price, and they step their walls toward the middle whenever our size takes share.

| When (11/18 Z) | The walls | What it did | What was done |
|---|---|---|---|
| 19:18 relight | 3-5c / 95-97c | 211 of 336 under the $1.10 bar at 1,000 a side (~1.2% share) | Jack "1.5-3x the size": `scan_max_mult 2` (section 11); 201 listed at 20:02 |
| ~20:30 | 6-8c / 93-94c | 43 strikes past the <=5c tier -> base -> under $1 | Jack "yes, extend to 10c": `cheap-tiers 2:10,5:5,10:5` |
| ~22:40 | 10-13c / 87-89c | rate $260 -> $155/day; 75 under $1 on the 15-min mean | Jack "Yes, extend to 15c" (told each extension had drawn the next step) |
| ~23:10 | 12-15c / 84-91c | 99 one-sided, 43 dark: the one-sided loop below | Jack "Yes, build it": the far-from-fair exemption (8281473) |

- **The one-sided loop on a step-up:** the join wait (2 s per $ at risk past $50: 100-270 s here) holds our side at the old price behind the new wall. Earn-only pulls it, because 1,000+ contracts sit in front, past the walk. When it rejoins, the off-usual cut holds it at base, because the price is 3c+ past its 6 h usual. 23:05-23:19Z: 465 of the 499 sides those guards held sat 20c+ from their live fair, where no bait can hurt.
- **Far-from-fair exemption (8281473):** a wave side `IMM_FAR_FROM_FAIR_C` (10c since 32bd17c, Jack 11/18: "Yes, 10c"; built at 20c) or more on the safe side of its LIVE FX / crypto event fair -- a bid that far under, an ask that far over -- skips the join wait and the off-usual cut. Every other guard stands: the lone-ref and alone-front guards, the bait-fingerprint stop, every breaker, the fair hold pull, the close clamp, the twin check, earn-only. Jack: "Make sure bait guards are up" -- after a deploy, list them from the startup lines. `=0` turns it off.
- **Then the 0c edge (13ffefe):** above.
- **Result:** 23:03Z 264 two-sided / 70 one-sided, then 23:35Z 296 / 38. Guard lines on far sides went from 449 to 1 per window. 313 of 336 at 2%+ share by 00:41Z.
- **Near-fair quoting (modelled 11/18, `fx_nearfair_model.py`):**
  - $4,819/day at 300 a side if we quote alone, against $208 at the walls -- but only $156-475/day once the walls match. They matched 37% of our new bests within 15 min (11/17), and stepped up within ~45 min here. 1% pair jumps inside a refresh cost $0.5-2.8k a pair at 300.
  - Jack: **"No, stay at walls."** Don't re-offer it, or a near-fair pilot, unless the walls stop following.

**What it earned:** FX + metals ~$1.1k/day at first (budget-capped, tails screened, dates misread) -> ~$3.5k/day sized up -> ~$2.2-2.5k after other makers answered; 1 fill in ~20 h. Crypto ~$110 -> ~$1,350/day with fair limits and bank-early; ~$600/day while banked strikes dropped to 300; ~$1.25-1.4k/day back at 1,500 (19-20Z 11/14); then jump holds and restarts: $940 banked for the period (to 05:00Z 11/15), 4 fills (+$42 at the next morning's fair). The bot's first estimate (~$9k for FX + metals over 30 h) is a ceiling.

## 13. Cash, sweeps and the switch files

- **Sweeps:** 11/07 a taker worked the House districts (~1/min, 20 min): 6,450 of our contracts, ~$7k of cash locked to 2029, prices didn't move. 11/12 a seller cleared the top bid of 18 Senate-nominee events in 21 min (section 1.1). The group breaker (2nd fill of 200+ in another event of the group within 10 min -> that side of the group held 20 min) is the standing cross-event protection; the per-event sweep breaker can't stop a taker that moves on. 11/15 a taker went state by state on one name (Mark Kelly, 7 states in 80 s): the candidate breaker now holds a name in every state after its first 200+ fill, and every breaker pull is batched, most at risk first (736cc94). Every wave series with no group is its own since 2063782.
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
| 7 | `pvd_waves.py run --write`: every enabled registry wave, scan + fair + tune + datefix, under each wave's pass lock; the after-economic-events waves' relights included |
| 8 | Pick-off watch + alert |
| 9 | `darkcheck --act` + `wave_alert.py dark`: listed vs resting on Kalshi |
| 10 | `bait_watch.py` + `wave_alert.py bait` |
| 11 | `wave_detect.py --upcoming --preflight` + `wave_alert.py upcoming`: advance notice of a wave that would be greenlit at its start, a NEW shape held for Jack, a registry relight (section 2.1) |

- **A new period (type C):** the scanner rebuilds each relit entry from scratch and the tune applies the registry's CURRENT args. So a relit market starts with every size-up Jack made last period (as of 11/14: statenom `2:30,5:15,15:11.25,30:3,70:1.25`, with Tennessee's 12 Senate-nominee markets pinned at 0 and three nominee markets at 200; elec2028 `2:15,5:15,15:3,30:1.5`, i.e. x15 to 5c, x3 to 15c, x1.5 to 30c). Say so in the relight report. Jack 11/14 on the nominee wave: keep the size-ups into its next period ("Yes, do 1-5"); for any other wave, ask whether the new period starts there or at the default. A relit market with no fresh usual price rests base for ~1 h (cold start) before its tiers apply.
- **The 11/11 miss** (history.md): relit 2028 markets listed under the old-wave lock rested nothing while the texts said "greenlit"; nothing compared the list with Kalshi. Now: every listing is stamped within a run; relight alerts say "listed", never "greenlit"; the dark check verifies.
- **darkcheck** (pass 9; the 24/7 check's STEP 0b re-runs it if pass 9 goes 40+ min stale): every live entry 20+ min into its listing is two-sided / one-sided / dark on Kalshi, each dark one with a reason (on purpose, held, the old-wave lock, the bot's own, unexplained). It ALERTS only on darkness seen at two checks 10-45 min apart: a market still under the old-wave lock after `--act`'s try, or WAVE-WIDE (100+ markets and a quarter of those checked). Jack 11/11: "stop false alarming". `--act` re-tunes a lock series' wave or adopts it into `w<MMDD>-adopt` after the auto preflight. For one-sided markets and per-side reasons, use `whydark`.
- **Bait watch** (pass 10; Jack 11/11: "Be extremely careful for baiters, notify me when something suspicious occurs", "Just cover market wave"): TEXT + email on a bait-fingerprint stop or a 3rd sweep trip in 30 min in one event; EMAIL on a toxic halt, a 2nd sweep trip, or a flash (a better price shown and pulled 3+ times in 60 min on one side).
- **A program jump that is a new wave the check listed:** the relight head reads "= new wave NAME, N mkts listed" and sends no second text.
- **The relight alert's wording (beb9966; Jack 11/18, "why denied" -> "reword the alert"):**
  - The head reads "RELIGHT: N events (M mkts) listed (~$X/day), D mkts denied". The $/day is the LISTED markets'; before, it sat after "denied" and read as the denied ones' value.
  - "Listed because" says the bar is $1.10 by the period end, not "/day", and how many listed only sized up.
  - Each denial reads in words: "under the $1.10 bar: their share of the pool at the wave's size (or even sized up to Nx its bank size) projects under $1.10 by the period end (Kalshi pays nothing under $1 a market per period)". `relight_watch.py` reads the bar and the size-up count from the scan's line.
  - When Jack asks "why denied", read the strikes' books: the walls, our share at the wave's size, and the size that would clear.
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

## 16. Sports (type E)

The playbook, the shapes, the bot's sports families and the worked example (the NFL LADDER / ESCALATOR waves, 9/24-10/20) are in `sports.md` beside this file. In brief:

1. **A family or Jack, never the list.** A sports series reads `MEMBER`: its family's rules quote it, and you check that the family is fit for the batch (`sports.md` section 4). Or it reads `CHECK`: Jack's call, with the checklist there. Nothing sports goes on the wave list.
2. **No fair, no player props.** The 10/03 Robinson escalator (6c to 45c overnight; 400 bought at 40c against a 5c fair) is what following the bare touch does on a prop book. NFL props have quoted inside `nfl_prop_fair`'s band since 10/04. No other league has a fair.
3. **The clock is the league's schedule.**
   - Cut off at the scheduled start, read from ESPN: Kalshi's dates run hours past kickoff.
   - Out 90 min early where a player who never plays pays $0 (the fantasy ladder; the inactives post then).
   - Season props are out while their league plays, and out for good once their round is decided.
   - Nothing rests in-game.
4. **News holds the whole team for 15 min, and a missing read stands the market aside.**
5. **Cash comes before size.** A ladder ask locks ~95c a contract, so the family's asks follow shard 0's free cash ($3,500 on, $1,500 off). Baseball, basketball and tennis need shard-3 cash: without it the MLB MVPs rested nothing for two days.
6. **Report each slate:** credits against fills at settlement value (props settle scalar), our share of Kalshi's pool, and paid against the estimate (escalators paid 1.6-1.7x the estimate). Expect other makers to dilute us within hours, and programs to stop without notice.

---

## Talking to Jack

- **Cash per contract, by side:** "sides tying up 90c+". **Multiples against the base:** "20x -> 30x".
- **Margins and limits:** give the whole gap on each side -- the base + the vol cushion + the weekday term, e.g. "bid <= 48c / ask >= 54c on a 51c fair" -- never just the base (11/15: "wdym?").
- **Options:** numbered, with $/day and $ at risk, the recommended one first. Short replies approve exactly what was offered.
- **Measured vs model:** say which every number is. Never "model: enough" (11/14: Jack: "you said there should be 270+57+33+11+11 strikes over $1").
- **First-hour rates are ceilings;** other makers arrive. Name the wave by date and theme ("11/11 · Senate/Gov nominees"), never a bookkeeping wave as a new one.
- **Pick-offs:** in $ marked to the book with our orders out, each with its mechanism (bait, sweep, real move, restart).
- **Commits:** only on "commit". Quote him in commit messages and list notes.
- **"What's coming?" / "any waves tomorrow?":** `wave_detect.py --upcoming --preflight` (no `--update-state`), then each window: when (ET), what would be greenlit, held for him (a NEW shape: say so) or relit, preflight now. Kalshi usually lists a start about a day ahead.
- **"Check that all N pace above $X / hold X% share":**
  - Answer on the 15-min mean (the dash since 5265a34, `pace`, or the cycle log's `est_frac` averaged), never one cycle -- he pasted a one-cycle "25/93 under $1" on 11/18 and asked "is this accurate".
  - Give the count that meets it, then every miss by class with an example and what would fix it.
  - If he names a time ("check again at 21:45"), schedule it and report at that time.
  - Right after a size or tier change, say the sizes are still landing and when the read is fair (~25 min).
- **"Why denied / why one-sided / why isn't X quoting":**
  - Classify every side by its first reason, with the numbers: `whydark`, plus the bot log's `earn-only` / `join wait` / `off-usual size` lines, plus the fair and the walls.
  - Say which reasons protect him (the fair clamp: the side would trade through the fair) and which are friction (a guard on a side 10c+ from the fair).
  - His follow-ups are short and literal ("0c edge is ok", "extend to 15c"): build exactly that, with a kill switch.

## Open questions for Jack (as of 11/19; closed ones in `history.md`)

1. 1c bid / 99c ask edges for crypto tail strikes worth under 1c (offered 11/14); ranking dark sides by $/day in the placement queue (offered 11/14).
2. A wave-level loss stop (e.g. -$500 in an hour of pick-offs) in the bot.
3. The clamp on every side always (+$263 replayed, lags real moves ~3 h).
4. Hard-cap crowded cheap sides at the 1,000 target; roll back the <= 5c doubling on 2028.
5. A per-series skip for a noticed upcoming wave, so it stays off at its start (today: the "pause overnight" text, or `stop` right after it lists)? (offered 11/14)
6. The 24/7 check's prompt still says its needs-you alert "emails, never texts": since 11/14 `wave_alert.py` texts a NEW shape (it decides; the prompt's note is stale but harmless). Updating the prompt is Jack's word.
7. KXYTVIEWSHIGH (YouTube highest daily views, 140 markets, $1,000/day, from 11/15 16:00Z): NEW, held; recommended off.
8. USD/MXN's dark spells (Bitso's wide book refused twice 11/14-15): a second source, or accept them.
9. A per-coin jump-hold trigger (HYPE held 30% of 11/14-15, BTC 8%): measure what the holds avoided first (offered 11/15).
10. Non-NFL sports ladders / escalators (NBA, NHL, MLB, college) are admitted by name with no fair. Block those leagues until a fair exists? (`sports.md` section 7, gap 1)
11. If the NFL ladder programs return: pull escalators 60 min before single-game kickoffs, put the sniper's data-gap guards into the IMM's gate, and lower the bid cap on reward-inflated books (`sports.md` section 5.4).
12. NFL single-game members (H2H, auto game families) need the ladders' ESPN roster read, so with no ladders listed they rest nothing. Give them their own read? (`sports.md` section 7, gap 2)