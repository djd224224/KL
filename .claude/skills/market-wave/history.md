# Market wave history

What each wave taught, with the numbers behind the runbook's rules (`SKILL.md` beside this file). Newest last. Memories with more detail: `imm-pickoffs-1107`, `market-wave-playbook`, `imm-statenom-sweep-1112`, `imm-fxmetals-wave-1113`, `imm-crypto-event-fair`, `imm-restart-diagnosis`.

## 11/04-11/06: the first waves

- **11/04 post-vote election wave:** ~5,300 programs, ~480 markets quoted (memory `imm-post-vote-dead-books`).
- **11/06 2028 election wave:** 1,827 programs, ~$64k/day of pools; first quotes 25 min after Jack's ask; ~1,880 markets over the night; est ~$1.6k -> ~$6.7k/day after tuning. The 11/07 House sweep tied up ~$7k of cash in 20 min (memory `imm-2028-election-wave-1106`).

## 11/07-11/08: the pick-offs (Jack: "the pickoffs yesterday were unacceptable")

**Every wave fill since 11/04** (forensics 11/08 15:16Z, marked to the external book or settlement): 379 fills, 106,056 contracts, net -$4,094; the losing fills alone -$6,033. By wave: 2028 election 201 fills -$4,561; 11/04 election 164 fills +$553; political 11/06 13 fills -$60; w1108-mixed (KXCCLA) -$26.

| Mechanism | Fills | Cost | Example |
|---|---|---|---|
| Jumped or walked thin book (BAIT) | 19 | -$3,653 | The House night 11/07 22:00Z - 11/08 04:00Z: 94 fills, -$3,818. AZ08-R ask 1,819 @61 (-$550), IL16-D bid 1,783 @33 (-$509), IN04-D bid 1,274 @40 (-$459), AR03-D bid 4,667 @12 (-$371) ... |
| Real repricing at the usual price | 71 | -$550 | ACA subsidies 2,025 @87, Medicaid work 1,519 @50: cheap-tier multiples before a pin |
| Cross-market sweep of 90c+ sides | 32 | -$352 | 11/07 16:18-16:32Z: 21 markets, 6,900 of our contracts |
| Halt-restart rejoin | 7 | -$89 | OH08-D bought 5,452 @14 (usual 8-9) at 03:34Z |
| One-event auto-listing | 1 | -$26 | KXCCLA-28JANALBD: an x3 cheap tier rested 600 @ 8c, 580 filled, marked ~3c |

- Outside the waves the same day: Grammy nominees (BPDGP-SHE 300 @68 over a 20c bid, $114) and the strait weeklies ($274, YES bids as the counts fell; the toxic rule caught one step of a slow slide).
- 30-minute mark-outs MISLEAD on these books: AR03-D read +$840 at +30 min and -$371 later.
- **The bait recipe:** (1) a 690-5,000-lot taker sweeps our capped 1-lot side (the PROBE: 11 of the 24 worst fills had it 8-56 min before); (2) a ~200-lot appears 9-37c off fair or walks 1c at a time; (3) our closing rung followed the touch outside the limits; (4) the next tune's uncapped re-anchor + flip-through made it full size at the bait price (1,000-3,700 contracts; AR03-D 7,595); (5) a taker trades almost exactly our size, the bait is pulled, the book reverts.
- **Nothing alerted.** Found because Jack asked for a dashboard refresh at 02:28Z; the $2k loss halt fired at 02:54Z after 11 of 13 fills; rewards over those hours ~$630.
- **Halts were not the bait defense:** on the 13 baited markets the sweep breaker would have stopped 1, the toxic halts 0; the 11/07 "at most about 3c a contract" estimate was wrong.
- **The defenses built, replayed on the $6,033 of losing fills:** close clamp (+$348 on top), off-usual size ($3,041), re-anchor cap ($3,003), join wait on penny and 0.1c books ($1,160 + $1,226 borderline), lone-ref guard on waves (logged "would stand aside" before every House bait), together $4,619 of $6,033 with the clamp. "Off means zero" (1b58188): no side rests a 1-lot probe. No preset 90c+ cap (Jack: "dont preset that").
- **Two auto-listed overnight batches were turned off:** w1107-mixed ("Turn off the wave you just greenlit"), w1108-mixed ("turn off KXCCLA and anything like it") -> the 5+-event rule, the info-risk screen, the gates.
- **Switch files that night:** HALT 04:07Z 11/08 ("Turn off bot"); bot-wide UNWIND_ONLY 04:27Z, waves exempted 05:01Z; UNWIND_ONLY off 14:07Z ("unwind only was meant for the 'market-wave', not IMM bot"); UNWIND_ONLY_WAVES 14:27Z ("wave unwind-only but at breakeven or better").
- **Daily-loss halt** $2k -> $4k -> $5k -> $6k -> $7k -> $8k over 11/08 (6e9b248). Net halt off (aa46d3b) after it tripped on marks that included our own quotes; P&L today marks without our orders since 0393404.

## Measured on the 2028 wave (11/06-11/08)

- Elasticity of rewards to contracts: 0.06-0.09 on <= 2c raises, ~0.3 on 2-70c, 0.87 on "everything x1.5". The <= 5c doubling realized ~$0.3-0.7 per $1k against $3.66 predicted. Overall: contracts 15x, $ at risk 2.7x, est $/day 1.5x.
- ~$4-7/day per extra $1k at risk; over-90c sides the worst ($3.65/1k) and held nearly all fills and cash.
- 0 fills at 1-2c in ~28 h; the pick-offs were all at 12-80c.
- `pvd_marginal.py` matched the bot on 91-98% of markets; raises realized 55-75% of its prediction (10-20% at <= 5c).
- Near-term markets (outcome within ~6 months) produced the largest positions: 11/07, 14 pinned at 300.

## Closed gaps (from the 11/08 list)

- The closing-rung exemption (the reducing side followed the bait at any price): closed by the close clamp (340fbe9).
- The 1-lot probe: closed by off-means-zero (1b58188). Of the 90 wave fills of 5 or fewer since 11/04, 75 were 90c+ 1-lots.
- The lone-ref guard (dry): ON for the waves (3032ea8).
- Legacy pass 4's 11/07 settings on a relight: passes 1-5 retired 11/11 (registry waves on the default preset).
- The marks bug for the halts: closed by 0393404.
- UNWIND_ONLY_WAVES disqualifying new waves: scoped to existing waves (3032ea8), then held-only (470df6c).

## 11/09: w1109-control (a hand-named one-off)

KXCONTROLS + KXCONTROLH 2030 (4 markets) were quoting 120 as Elections members; Jack: "Yes, 200 a side". The wave check would never list it (FEW, MEMBER): a hand listing on Jack's word.

## 11/11: the 2028 relight (w1111-elec2028)

- **The miss:** the 2028 programs relit at 05:00Z for a week; passes 2-4 re-listed 724 markets with no `wave_added`, so UNWIND_ONLY_WAVES kept every flat one dark (1 of 724 quoting); the overnight texts said "247 / 209 / 199 markets greenlit"; wave_detect called the series LISTED, so the skill never ran; nothing compared the list with Kalshi. Jack: "ELI5 why market-wave didnt autoquote last night" -> "yes build all 3. make no mistakes, this is serious. if the 2028 previously quoted markets get relit, they should be quoted. if new 2028 markets come online, they should be quoted. only the markets with positions should be on unwind mode."
- **Fixes:** the registry wave w1111-elec2028 (12:13Z, "Yes, at 200 a side"; 722 of 723 resting by 12:17Z); `UNWIND_WAVES_HELD` (470df6c); passes 1-5 retired into w1111-w28 / w1111-pol; darkcheck (pass 9); relight alerts say "listed".
- **Found on the way:** 239 ended entries still carried no-halts, keeping the series in PVD_NO_HALT_SERIES (cleared by hand). The scan's old 3-97c test had dropped every 1-2c side, so 191 previously quoted primary markets relit unlisted; the scan now judges a market at the wave's own tiers ("should quote larger size if <=5c, otherwise if still under the $1.1 bar then yes dont quote it").
- **Pacing at go-live:** 130 of 723 short of $1.30, 129 of them crowded 5-50c primary candidates at ~1.4% share -> bank max 300 (Jack "yes"; 192 markets raised). 37 KXPRESSTATE sides one-sided under the lone-ref guard (a single maker at the touch on both legs, the rest 20-90c away).
- **The twin check** (Jack 12:35Z: "make sure the quotes on each side make sense relative to each other. that should also help prevent baiting"): built, replayed ($736 of the 2028 losers re-priced; the House-night baits moved both legs together, so most got past it), deployed 6fbad53.
- **KXJEFFRIESFIRST stop:** a pause alone left the 92c bid up (a decided side ignores the band): pause + side cap 0.
- **The dark check's false alarms:** 17 settled markets (fixed: Kalshi status), a sweep breaker's 10-min hold (fixed: the bot's holds), then "stop false alarming": only darkness seen at two checks.
- **Held positions:** 145 of 246 held wave markets had no closing order, nearly all by design (decided midterm markets ride; 1-4 lots); 3 small shorts had no valid price (Jack: "Leave them").
- **Rate:** ~$326 -> ~$779 -> ~$452/day over the next day (competition).

## 11/11: the Senate/Gov nominees (w1111-statenom)

- **Auto-listed** by the 24/7 check at 16:08Z: 9 FIT 2028 series (Senate / governor nominees, state row offices), 654 markets, ~$27k/day of pools.
- **440 of 654 dark at first:** the 9 series weren't election-family members, so the bot's entry screen dropped 225 long shots (`extreme_mid`) and wouldn't bid 1-4c; `enroll_new_series` had skipped them because the list made them "allowed". Fixed: merged into election_extra_series.json (16:31Z), then `allowed_by_wave_list_only()` enrolls Elections series a wave lists. Side effect: unlisted markets of those series can quote at x2 once dated (Jack: "No leave it").
- **The relight email said "+803 programs, 0 listed":** that was this wave; relight_watch now names new waves.
- **Size-ups (Jack's words -> tiers):**

| Time (Z) | Jack | Tiers after | Note |
|---|---|---|---|
| 11/11 22:06 | "1.5x everything <=15c on the 2nd wave today" | 2:15,5:7.5,15:1.5 | "<= 15c" covers asks at 85c+; 630 sides; cash ceiling $16.9k -> $25.3k |
| 11/11 23:23 | "1.5x size <=30c on first wave, 1.5x size 5-30c on second wave" | 2:15,5:7.5,15:2.25,30:1.5 | "5-30c" = above the 5c tier (the 11/07 "5 to 15" convention) |
| 11/12 02:05 | "1.5x size 2-30c on 2nd wave" | 2:15,5:11.25,15:3.375,30:2.25 | 65 sides held at base by off-usual |
| 11/12 02:13 | "Actually 2x instead of 1.5x on <=30 on 2nd wave" | 2:15,5:15,15:4.5,30:3 | replaced the step; asked about <= 2c |
| 11/12 02:34 | "Yes, double them" | 2:30,5:15,15:4.5,30:3 | 41 bid sides 3,000 -> 6,000 |
| 11/12 02:58 | "2.5x the 5-15c on 2nd wave" | 2:30,5:15,15:11.25,30:3 | 282 of 365 sides held at base by off-usual (long-shot bids rose 1-4c -> 6-9c); "why only avg 556" = that; ~2,150 by morning |
| 11/12 12:57 | "1.25x 30-70c size on2nd wave" | 2:30,5:15,15:11.25,30:3,70:1.25 | +25% sits inside the 20% amend tolerance: lands at re-placement |

- **Competition, not bait:** the rate fell ~$4.4k -> $1.3k -> ~$0.75k/day; other makers bid the long shots above or with us (7,499 @ 3.1c, 129,765 @ 2.8c).
- **The 11/12 sweep** (Jack: "Am I getting baited? A bunch of fills just happened"): 3:08-3:29 PM ET (+3:44) one seller took the whole top bid level from every maker in 18 KXSENATENOM markets, a state every 20-60 s (15,655 @ 35c TN, 8,596 @ 85c NV, 7,741 @ 82c KS). Our 17 full fills of 200-300 + 2,071 @ 9.7c PA: 6,321 contracts long, ~$3.3k, -$130 at 4 PM, -$256 by 5:37 PM as more selling came. The per-event sweep breaker fired on every fill but the taker moved on; the dry group breaker saw it after the 2nd fill (+$117 if on). Jack: "Yes, after the 3rd fill and hold for 20min" (76d9201), then 11/13 "change the 20min pause to be on the 2nd fill" (25cbd54).
- **Late pick-off emails** ("Why are these emails coming? are they delayed?"): sweep fills crossing the bar hours later in the 6 h lookback; the fill time is now in each line. Three of them tripped the series: KXSENATENOM pinned at 200 a side at 01:30Z 11/13.
- **News risk:** KXSENATENOM-TNR-28 waits on Blackburn's Senate appointee; one announcement moves 12 markets; blocking the event was offered, unanswered.
- **Unwind policy:** "are you trying to unwind all positions across waves? is it only at breakeven or better?" -> "unwind, doesnt have to be at breakeven but dont lead" (85a5d8e: the outside price, at most 5c past usual or at cost where further, never leading; what-if -$3.0k realized, already marked). Closing orders dropped for ~16 min at a slow restart (13:05Z 11/13) -> "Yes, keep them" (95eef3b).

## 11/13-11/14: FX + metals after economic events (w1113-fxmetals)

- **What lit:** 3,670 programs at 18:01Z 11/13 to 05:00Z 11/15 (3,169 after-economic-events: metals, six FX pairs, crypto, yields). The wave check had called metals NOT LIVE, FX / crypto CHECK. Jack: "should i quote the 'after economic events'?" -> the weekend-only rule -> "Do it".
- **At go-live:** +$100k budget listed 807 of ~2,469; 27 selected (the DDMONYY date misread; 146 `event_start_overrides` rows fixed it, then `datefix`); 174 tails screened out (fixed 38f07e9 + 0f12693); the resting cap 7,128 of 8,000.
- **No fair:** "Are there Fair value calculated on these markets?" -> 84 of 1,002 orders 5c+ wrong side ($1,602 if all filled); the one fill (sold 180 @93 on USD/JPY > 156, worth ~99c) marked +$56 by the pick-off watch. -> `wave_fair.py` from Friday's close, then "Can you get the 24/7 prices on these markets to calculate fair?" -> Hyperliquid / Kraken / Bitso moves. 0 of 650 wrong-side after.
- **Capacity:** resting cap 8,000 -> 12,000 (38f07e9); the bot's own-orders read stayed at 8,000 and doubled 3,462 sides for ~2 h (757614b fixed it: 1,000 a page); placement cap 1,000 -> 1,500 (e403df0, Jack "Yes, 1,500") after "placement cap isnt for refreshing orders right?" (it is: renewals use most of it); dark sides first (60c237d); a deferred size-up keeps its order (53c816d).
- **The $1 floor:** "what % of market wave is pacing below $1 threshold" -> 76% (1,500 of 1,985) at 03:51Z. "size them up and report back" -> multiples via `--size-mult-file`; the 04:00Z pass started before the change and wrote the old sizes back at 04:02Z (-> the pass lock). "tighten to 2c. and then 2x any falling short of $1, and then stop quoting the rest" -> "actually go up to 5x every market" -> "and like prior market-waves, bank as much size early to be safe". 05:21Z: 82% of quoting markets projected $1+, ~$3.3k/day.
- **The off that wasn't:** the 341 markets set to 0 kept resting 40-80 a side (`"size": 0` = the bot's default 40); fixed in the tune (both sides 0).
- **Collateral:** at $1M the standard-ladder model refused 150 other markets (offs free nothing) -> "Raise budget from $1M to $3M, make sure don't lose quoting time" (ea5e796; 8,640 orders adopted in 54 s).
- **Competition:** others added ~25M contracts 05:30-07:00Z against our 5M (5:1); median share 8.8% -> 6.3%; rate $3.5k -> $2.5k/day. A 10x lift for the 864 off markets was costed by cost bucket -> "Keep them off".
- **Single-cycle shares mislead:** "some pacing <$1, if true then those should stop quoting" (13:42Z) -> judged on the average share since the size landed, uptime-weighted: 326 more off (1,190 total), 13 topped up, ~7 borderline kept; 1,228 quoting at ~$2.2k/day.
- **Result to 13:51Z:** ~$1,224 banked, 1 fill.

## 11/13-11/14: crypto after-events strikes (w1114-cryptoev)

- **Before the wave:** non-fleet crypto series quote NO only at 5x (c4c6bff, "besides the markets the crypto bot quotes, all other crypto markets are fair game"); a cash cap built on the wrong premise was removed ("kalshi doesn't hold cash for resting orders"; c725418). "you can quote both sides on these, built around fair value" -> `crypto_event_fair` (1442295); narrowed after other makers moved their walls in around the fair (6449598, "Yes, narrow it").
- **The wave:** "allow it to quote like market-wave" -> w1114-cryptoev at 02:47Z on :8783 (the crypto page). At +$100k it listed 0: $611k of other waves' scan adds were live (the shared budget) -> +$400k with Jack's yes; 224 of 544 listed.
- **Wave books:** "why is it leading on one side and sitting outside of rewrd earning range on the other side? it should function like the other market-wave books" -> 20aca4e (no fair lead on listed strikes). Asks lagged behind the 5c-a-run limit walk -> "yes, use the fair" -> 28837b4 (limits from the live fair every cycle): two-sided 277 -> 483.
- **Sizing:** "double size on the markets that would get them to the $1 threshold" -> 2x pins; "dont rest base size for the first hour, allow them to go up to 400" (1f68841); "go up to 3x" -> "actually change the 3x to 5x"; "after these increases, stop quoting markets still pacing <$1" (e4dd1f4, the stop file). Measured at 04:55Z: 45 of 108 pinned strikes at $1+ ("you said there should be 270+57+33+11+11 strikes over $1"): the model ignored the 1,000-contract scoring window and other makers' size; the stop was changed to two reads. "like prior market-waves, bank as much size early to be safe" -> 1,500 a side until $1.30: 469 of 498 at $1+. The 38 never-listed strikes under $1 -> "Stop them early".
- **Restarts:** 8 in one night from other sessions' deploys; reads within ~15 min of each ran low.
- **Shard 2:** $1,140 free while the wave's "if everything filled" was far above it; Jack moved $1.5k from shard 0 (02:58Z).
- **Result 12:50Z 11/14:** ~$405 banked, ~$1,356/day, every quoting strike pacing $1+, 57 stopped, 0 fills.

## Open questions closed since 11/08

- Auto-list vs notify-only overnight: keep listing, under the gates (and 24/7 since 11/11).
- UNWIND_ONLY_WAVES scope (3032ea8) and held-only (470df6c).
- Legacy pass 4 inside the default rules: passes 1-5 retired 11/11.
- The lone-reference guard ON for waves (3032ea8); P&L today's marks without our orders (0393404).
