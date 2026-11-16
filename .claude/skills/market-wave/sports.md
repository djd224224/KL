# Sports waves (type E)

Read this when a batch of sports programs lights (NFL, NBA, NHL, MLB, college, soccer, tennis, esports), when Jack asks about sports props, or before quoting anything that settles on a game. It is section 16 of `SKILL.md` in full. The worked example is the NFL LADDER / ESCALATOR waves of 9/24-10/20 (section 5). Numbers are measured unless marked "model"; the data and scripts behind them are in section 8.

**The short version:**
- **Sports is never a known shape.** The detector calls a sports series `MEMBER` (one of the bot's sports families already quotes it, on that family's own rules) or `CHECK` (Jack's call). Auto mode lists neither.
- **Nothing sports goes on the wave list.** The list has no kickoff clock, no news hold and no player fair; the families have all three.
- **The information clock decides, not the settle date:** injury reports, inactives, lineups, the game itself. Every sports family stops quoting at the scheduled start, and nothing rests in-game.
- **No fair, no player props.** NFL props have quoted only inside a player-history band since 10/04. Another league's ladders would quote on the bare touch, which is how the bot bought 400 at 40c against a 5c fair on 10/03 (section 7, gap 1).

## 1. The shapes

| Shape | Looks like | Settles | What moves it |
|---|---|---|---|
| **Player-stat ladders** (`KXNFLLADDERREC`, `...RECYDS`, `...RSHYDS`, `KXNFLFFPTSLADDER`) | One market per player per game. An event is one stat in one game (`KXNFLLADDERREC-26OCT05ATLNO`). They list 1-3 days before kickoff. | Scalar and linear: $0.05 a catch (cap 20), $0.0025 a yard (cap 400), $0.01 a PPR point (cap 100). They settle within hours of the game. | Injury tags, inactives (~90 min before kickoff), teammates out, the game |
| **Escalators** (`KXNFLESCALATORREC`, `...RECYDS`, `...RSHYDS`) | Same as ladders | Scalar and cubic: (catches / 14)^3, or (yards floored to 10, / 200)^3. Most settle near 0c; a big game pays up to 100c. | Same as ladders. The tail is fat: a sold YES can lose 80c+ a contract. |
| **Head-to-head** (`KXNFL<STAT>H2H`) | "A records more X than B", one market per pair. They list 2-6 h before kickoff. | Binary; a tie pays 50/50 | Same as ladders |
| **Single-game markets** (`KXNFLTD`, `KXNFLPASSYDS`, `KXNBAGAME` / `SPREAD` / `TOTAL` / `PTS` ...) | Per game | Binary | Same as ladders, plus lineups |
| **Season props** (`KXNFLLASTTOWIN`, `KXNFLPLAYOFFC`, `KXNHLSEASONGOALS`, MVPs, `KXMLBSEASONGAMES`) | One event per season or round | Binary | Every game of the league. An MVP is announced on the field minutes after the clinching out. |
| **Esports** (`KXLOL*`: Worlds qualifiers, regions, awards, Teemo / pentakill props) | Per tournament | Binary | Every match. Books are thin. |
| **Event-adjacent** (marathons, Super Bowl halftime setlists) | Per event | Binary | The event day |

## 2. How the bot quotes sports today (11/16, read from code)

| Family | What it admits | Cutoff and live rule | Fair and news | Size (on the 20 base) | Loss stop | Switches, files |
|---|---|---|---|---|---|---|
| **Sports ladders / escalators** | By name, in code: `KX(NFL\|NBA\|WNBA\|NHL\|MLB\|NCAAF\|NCAAB\|CFB\|CBB\|MLS)...(LADDER\|ESCALATOR)...` (`_SPORTS_LADDER_PATTERN`), cloned from the archetype `KXNFLLADDERREC` (safe-join) | The scheduled start, read from ESPN's scoreboard (`start_buffer_min` 0, b790b29); NFL fantasy ladders stop 90 min before kickoff (7c08f29). Order expiry is capped at the cutoff. If ESPN can't place the game, the market is out the night before (the ticker-date midnight-ET rule). | **NFL only:** `nfl_gate` on `nfl_prop_fair` (rules 1 and 4). **No other league has a fair.** | Family x5 times hour x4, every hour: 400 a side, with Saturday steps on top. Caps: 750 a market (1,000 on Saturday), 5,000 an event. Prices 0.05-99.5c on the exact sub-penny grid. Entry bar $1.00. **Asks rest only while shard 0 has $3,500+ free, and stop under $1,500.** | None of its own: the bot-wide halts, the sweep breaker, and the group breaker, which treats one NFL game as one group | `IMM_SPORTS_LADDER_BLOCK=1` blocks the whole family. `IMM_NFL_FAIR_ENABLE=0` removes the gate, so the props quote with no fair. |
| **NFL head-to-head** | `^KXNFL...H2H$` | Kickoff | A team-news hold on both teams, from the ladders' ESPN roster read; fails closed. No fair band. | Same as the ladders | None (760ac9f) | `IMM_NFL_H2H_ENABLE` |
| **NFL auto** (`nfl_auto_series.json`, hot-loaded) | A newly paying KXNFL series in Kalshi's Sports category with the Football tag; off-field names stay out. The 30-min family watch enrolls it and emails Jack. | Game members: kickoff, or 90 min before it where the rules don't cover a player who never plays. Season members: out while any NFL game is live (+15 min after the final). | Game members: the team-news hold | x3 (60 a side), with the quiet-hour and Saturday steps | -$100/day per event | `IMM_NFL_AUTO_ENABLE`. Members now: `KXNFLLASTTOWIN`, `KXNFLPLAYOFFC`, `KXNFLWEEKQBSTART` (all season) |
| **League auto** (`league_auto_series.json`) | KXNBA / KXWNBA / KXMLB / KXNHL series with the league's tag; LoL season members (Esports tag) | Game members: out 90 min before the start. Season members: out while the league plays (+15 min), and out for good once their round is decided. LoL reads an undocumented lolesports feed, and any failed read holds every LoL member. | NBA game members: a 15-min hold after any ESPN injury-report change, failing closed (62c22e0). WNBA, MLB and NHL: none. | x3 | -$100/day per event | `IMM_LEAGUE_AUTO_ENABLE`, `IMM_LEAGUE_NEWS_LEAGUES` |
| **MLB postseason MVPs** (exact names) | ALCS / NLCS / WS MVP | Out while any MLB game is live (+15 min); out for good once the round is decided | None | x2 | The per-event contract cap | `IMM_MLB_MVP_ENABLE` |
| **Others** | Awards (out a day before the first narrowing), marathons (out 10 min before the division's start), Super Bowl halftime (`KXSBSETLISTS` x4.5, 2-98c) | | | | | Memories `imm-awards-family`, `imm-marathon-family`, `imm-superbowl-halftime-allow` |
| **The NFL sniper** (`nfl_snipe_bot.py`: a separate taker in subaccount 1, on the same fair) | It sold into reward-inflated prop bids | | | | | OFF since 11/07 (its HALT file). Only Jack restarts it. |

Programs live at 13:15Z 11/16, all small (from `reward_programs.json`): NHL 71 markets, Super Bowl 72, NFL 35, MLB 32, Champions League soccer 32, college football 17, LoL 12, NBA 8, WTA tennis 8, WNBA 5. None are ladders or escalators. Soccer (`KXUCL`), college (`KXNCAAF`) and tennis (`KXWTA`) belong to no family, so they read `CHECK` if they ever clear the wave bar.

## 3. The rules (each one came from a loss or a measurement)

1. **No fair, no player props.**
   - **What happened:** on 10/03 at 05:02Z, Bijan Robinson's receptions escalator jumped from 6 / 97 to 45 / 60 on no news. The quote loop followed the touch and bought 400 at 40c against a ~5c fair. It settled at 0: -$160. The bot's reward estimator ranks exactly these thin, dislocated books highest.
   - **The rule since 10/04 (94d637c):** NFL props quote only inside `nfl_prop_fair`'s band. Bids go at most to the band top + 1c, and asks no lower than the band bottom - 1c. The band is the wider of the payout at 0.7x / 1.5x the player's mean and his last-4-games band. Nothing rests on a book 3c+ outside the band, on a read older than 30 min, or on a roster read older than 45 min.
   - **What the fair is for:** the model's error equals the pre-game book's own (MSE 59.5 vs 59.1 on 368 settled props). It guards against a dislocated book; it gives no edge over a quiet one.
   - **Evidence:** a replay of 9/30-10/03 found the 32 fills outside the band lost $384, while the 241 inside made $97. Fill P&L went from -1.65c a contract before the gate to -0.50c after.
2. **The contract's non-participation clause sets the cutoff.** Read it before choosing one.
   - **Receptions / yards ladders and escalators, and H2H** (FOOTBALLENTITYSCALARSTAT, FOOTBALLH2HSTAT): a player who never plays resolves at the Exchange's last fair price. Quote to kickoff.
   - **The fantasy ladder** (FFPTSSCALAR): he is deemed to have scored 0.0 points, so YES pays $0. Stop 90 min before kickoff, when the inactives post (Jack 10/04: "So block fantasy ladders 90min before kickoff").
   - **A rule silent on inactives** (`KXNFLTD`, `KXNFLPASSYDS`): stop 90 min before kickoff.
3. **Never rest in-game. Every sports family ends at the scheduled start.**
   - **Take the start from the league's schedule, never from Kalshi's dates.** On the ladders, Kalshi's `occurrence_datetime` is ~3 h after kickoff and the close two days later. A LoL match's ticker time was hours off its real start, and the MVP close dates are placeholders.
   - **Season props:** out while any game of their league is live (+15 min after a final), and out for good once their round is decided (Jack 10/12: "any props that go beyond a single game, should be turned off anytime there's a live game").
   - **Trading during a game is a live session** (skill `market-live-trade`, Jack's call), never a wave.
4. **News moves whole teams: hold the team, and fail closed.**
   - **The hold:** any contributor's news holds every prop of his team for 15 min (32faaf7; Jack 10/09: "shorten the team news hold to 15 min"). An injury tag stands aside only the fantasy ladder, because the other props resolve at the last fair price. A stale or missing ESPN read stands the market aside.
   - **Teammates out move the fair:** +7-16% to a key pass-catcher's teammates, ~+37% to a lead back's backup, -5% to the receivers when the starting QB is out. That is ~30% of a proportional reshare.
   - **The miss that led to it:** on 10/03 the bot bought Terry McLaurin's yards ladder 2 h after ESPN tagged him Doubtful.
   - **Expect flapping:** on an injury-report afternoon, whole events go on and off. That is the hold working.
5. **On these books, the bid is the side that loses.**
   - **The books:** reward-chasing makers hold NFL ladder YES bids at ~1.6-2.4x the players' output. The ask side sits nearly empty, because selling YES at 5c locks 95c a contract.
   - **Over the wave:** our bids lost $1,496 on 69,859 contracts (-2.1c each). Our asks made $347 on 103,252 (+0.3c).
   - **The asks carry the tail:** an escalator pays cubically. The worst single fill was an ask: Puka Nacua's receptions escalator, 400 sold at 21.8c that settled at 100c (-$313, MNF 10/12).
   - **Bids-only was a cash decision, not an edge** (Jack 10/04: "actually turn off the ASK sides only, bid sides can stay on").
6. **Cash binds before risk does.**
   - **10/04:** 42k of the day's 52k cash-bound placement rejects were ladder asks (free cash $1.5-3.8k, against ~$83k of planned ladder collateral).
   - **The rule since:** the family's asks follow shard 0's free cash. They come on at $3,500 and go off under $1,500 (1ab3318; Jack: "adjust to 1.5 and 3.5k instead of 2k and 4k").
   - **Shard 3:** baseball, basketball and tennis trade there. The MLB MVPs rested NOTHING for two days (~27k orders refused, `insufficient_shard_balance`) until Jack moved $1,000 there.
   - **So:** check the event's shard before the first slate. Only Jack moves money.
7. **Expect to be crowded within hours.**
   - **Who else is there:** designated makers rest 55k-330k contracts a side. Our estimated share of the scored book ran 0.1-0.3%.
   - **10/06 (TB@DAL):** another maker loaded the books 5-40x deeper ~2-3 h after listing (1.1M YES at 4.16c on one escalator). The hopeless exit pulled ~18-20 of 37 markets.
   - **Size bought share only until others answered:** our share of a Sunday's escalator pools fell from 2.4% (10/04) to 1.8% (10/11) while our size went up. Over the whole wave we took 1.0% of what Kalshi listed.
   - **The family's bar is $1.00** (Jack 10/11: "lower ladders and escalators to $1"). Keeping a near-cliff market alive needs $0.50 banked, which diluted markets rarely have.
8. **The estimate understates escalators.**
   - **Paid against the bot's estimate (10/15):** escalators 1.57-1.74x (133 events), ladders 1.10-1.13x, the other families 1.00-1.12x.
   - **So:** never drop an escalator on its estimate alone, and report the estimate and the paid figure separately.
9. **On single-game prime-time nights, the last hour loses.**
   - **TNF / MNF escalators (10/05, 10/08, 10/12):** -$904 at settlement. The last hour before kickoff lost $436 (-7.2c a contract; asks lifted for -$823) against ~$10-16 of that hour's rewards.
   - **A Sunday's last hour was a wash:** +$92 of rewards against -$79.
   - **Not built:** pulling escalators 60 min before single-game kickoffs was recommended on 10/15. The programs ended first.
10. **Thin prop books get baited.**
    - **LoL Worlds, 10/15-16 (11 PM-midnight ET):** a lone 200-lot walked Teemo from 6c to 69c, and pentakill 1+ from 10c to 93c, in minutes. A 200-lot is target / 5: it IS the reward reference our at-reference bid rests on, so our bids followed. A seller then hit exactly our size 16 times, up to the 180 cap: -$275 at mid against $0.34 of rewards.
    - **Safe-join did nothing,** because it is capped at the reference.
    - **What came of it:** the lone-reference guard (on for the wave list, dry elsewhere), and `KXLOLTEEMO` was cut on 11/01 (57ae4d3).
11. **Programs end without notice.**
    - **What happened:** after the 10/12 games Kalshi posted no programs on NFL ladders, escalators, fantasy or H2H, except one Monday game (10/19, 20 markets). It stopped the same day Kalshi ended its volume program early (the cause is unconfirmed). Credits went from ~$2.9k a week to ~$20.
    - **Nothing to unwind:** props settle within hours of the game.
    - **In season:** re-check `/incentive_programs` (active and upcoming) on Thursdays.
12. **Props settle scalar.**
    - **Fills:** score a prop fill at its settlement value per contract (the settlements archive's `revenue` divided by the count held), never at 0 / 100.
    - **Fairs:** judge them against the model's own payout map (`nfl_prop_fair.py`, which reproduced every settlement to 10/01).
13. **A game's props are separate Kalshi events.**
    - **The shape:** one game has a receptions ladder, a yards ladder, escalators and a fantasy ladder, four or more events in all.
    - **The breakers:** the per-event sweep breaker sees each event alone. The group breaker treats an NFL game as one group (SKILL.md 1.2).
14. **The model is only as good as its rows.** The sniper, a taker on the same model, found two gaps:
    - **A benched player has no stats row,** so the model read his older games: the sniper bought 3,999 contracts of Cam Skattebo's yards ladder at 2c.
    - **A teammate's return isn't seen:** it bought 887 of Aaron Jones's rushing ladder at 9c after Jordan Mason came back.
    - **The fix reached only the sniper:** it got the `missed_games` and `role_change` guards (58cef4d, c81e6a0); the IMM's gate did not.

## 4. When a sports batch lights

1. **Detect** (SKILL.md 2). A sports series reads `MEMBER` or `CHECK`; it is never FIT, NEAR or NEW, because Sports is a risky category. Report both groups with markets, events and $/day.
2. **MEMBER: name the family (section 2) and check that it is fit for this batch.** Don't list it. The checks are all read-only:
   - **Fair** (NFL props): `run-logs/incentive-mm/nfl_prop_fair.json` was written in the last 30 min and prices every listed market. `python nfl_prop_fair.py --event <EVENT>` prints each prop against its book. A new stat series is unmodelled, so it stands aside.
   - **Cutoff:** `selection_snapshot_<UTC date>.jsonl` shows each event's cutoff at the scheduled start (fantasy: 90 min before).
   - **Shard cash:** check the event's `exchange_index` (NFL 0; MLB / NBA / tennis 3) against the bids. Ladder asks need shard 0 at $3,500+ free (`python kalshi_subaccount.py shards`). Money is Jack's.
   - **Resting on the exchange:** in the cycle log, sum `own_bid_ct + own_ask_ct` by market. Stand-asides show in `guard_skips_<date>.jsonl` (the `nfl_fair` reasons: `no_read`, `stale`, `stale_roster`, `injury`, `news_hold`, the band) and in the day log ("nfl stand-aside" / "nfl resume", "league team-news stand-aside", "mlb mvp stand-aside"). Anything else: memory `imm-quoting-triage`.
3. **CHECK: Jack's call.** Bring him:
   - the shape (section 1);
   - whether a family could take it (NFL / league auto enroll by Kalshi category and tag; the ladder pattern admits by name);
   - the fair (NFL props only);
   - the non-participation clause (the contract PDF) and the live-game rule;
   - the event's shard and its cash;
   - the pool;
   - a recommendation. A league with no ESPN schedule or no fair: recommend no.
4. **Before the first slate, tell Jack what will rest:** the size (400 a side on ladders), the cash it needs, and what stands aside when.
5. **Watch each slate.** No relight pass covers the families: the pick-off watch (pass 8) reads the wave list only. Watch:
   - **Dilution:** in the selection snapshot, a 5-40x jump in `book_depth_contracts`, `projected_total` under $1, hopeless exits.
   - **Fills:** `fills_<date>.jsonl` by market, marked at settlement, bids against asks.
   - **The last hour before a single-game prime-time kickoff.**
   - **Injury-report afternoons:** flapping stand-asides there are the hold working.
6. **Report each slate:** credits (they land 0-1 days after the period, in `reward_credits.csv` once the statement is in), fill P&L at settlement value, our share of Kalshi's pool, and paid against the estimate.

## 5. Worked example: the NFL LADDER / ESCALATOR waves (9/24-10/20)

**What Kalshi ran:** from 9/23 to 10/20, liquidity programs on 1,509 NFL prop markets: 612 ladders, 612 escalators and 285 fantasy ladders, across 48 games of Weeks 3-6. $694k was listed, mostly $500 a market and $200-1,000 on the rest. That came as one wave a week: Thursday-to-Monday's games, each listing 1-3 days ahead and settling within hours of its game. The registry didn't exist yet, so the bot quoted them as a code family (section 2), changed slate by slate on Jack's word.

### 5.1 Timeline

| Date | What happened | What changed |
|---|---|---|
| 9/24 | Jack: "allowlist sports ladders and escalators, up until the game starts" | Admitted by a name pattern. Cutoff = kickoff from ESPN, because Kalshi's `occurrence_datetime` is ~3 h into the game (64b257b). |
| 9/26 | Week 3's Sunday slate: 466 markets, but 221 ladders failed the $1.50 bar (a designated maker rests 20k-60k contracts at the touch; our 30-lot was ~0.3% of the scored depth), 117 escalators sat under the 5c mid screen, and 69 read zero yield (their sub-penny prices round to a 0c spread). 46 rested. | 1-99c at x2 (cda6f2b; Jack: "for ESCALATOR/LADDER only, allow quoting range 1-99c and double contract size"). Rungs rest at the exact sub-penny touch (c76a0e3). x3 and a $1.20 entry bar (e46ed31). |
| 9/29-10/02 | 9/25-10/02: $434 of modelled rewards, +$12 of trading marks on 9,746 contracts | x4 (d0fcb99), full unwind (4e01668), x5 (9d77258) |
| 10/03 | The Robinson escalator, 6c to 45c overnight: 400 bought at 40c. McLaurin's yards ladder bought after his Doubtful tag. The open prop book was -$544 at marks. Jack: "how to avoid getting blown up ... can you model what it should be, based on past data of the player" | The player-history fair and gate (94d637c). Fantasy ladders out 90 min early (7c08f29). A read-only scanner (89f0475). |
| 10/04 | 42k of 52k cash-bound rejects were ladder asks. Jack: "turn off LADDER/ESCALATOR events for now", then "actually turn off the ASK sides only" | Teammate adjustment and team news hold (c71c529). Only sides that earn rest (f75a6b5). Family off (a9cf657), then asks off only (61a6999). Asks back by shard-0 cash at $3.5k / $1.5k (e7a63f6, 1ab3318). Injury tags affect the fantasy ladder only; quotes 0.05-99.5c exact (5b00b70). The recent-form band (5ba3104). The sniper (193f700). |
| 10/06 | TB@DAL: another maker loaded the books 5-40x deeper 2-3 h after listing; ~18-20 of 37 markets were cut | x3 at every hour, not only overnight (8134d74) |
| 10/09 | | News hold 30 -> 15 min (32faaf7) |
| 10/11 | | The hour part to x4 = 400 a side (ef3f00f). Bar $1.00 (5f9c31e). Quote to kickoff (b790b29). |
| 10/12 | | The H2H pilot, sized as the ladders (264fab8, b250818). NFL auto families (fdc233f, 959080a). |
| 10/12-10/20 | The last big slate (MNF BUF@LAR), then one Monday game (10/19, 20 markets). None since. | |

### 5.2 Results

By slate (credits from `reward_credits.csv`; fill P&L = the bot's own fills at settlement value, before fees):

| NFL week (games) | Program markets | Kalshi listed | Our credits | Our share | Fill P&L | Contracts filled |
|---|---|---|---|---|---|---|
| Week 3, 9/24-9/28 (16) | 542 | $196,050 | $283 | 0.1% | +$14 | 5,040 |
| Week 4, 10/1-10/5 (16) | 526 | $281,500 | $4,146 | 1.5% | -$394 | 52,480 |
| Week 5, 10/8-10/12 (15) | 421 | $210,500 | $2,801 | 1.3% | -$777 | 112,849 |
| Week 6, MNF 10/19 (1) | 20 | $6,000 | $18 | 0.3% | +$8 | 2,742 |
| **Total (48)** | **1,509** | **$694,050** | **$7,248** | **1.0%** | **-$1,148** | **173,111** |

- **Net about +$6.1k, keeping 84% of the credits.** Through the 10/15 statement, NFL props were 44% of the whole account's four-week net (+$6,019 of +$13,549).
- **By family:** escalators $4,280 of credits against -$594 of fills (keep 86%). Ladders $2,188 against -$172 (92%). Fantasy $780 against -$383 (51%). H2H earned $22, with no fills.
- **By phase:** to 10/03, with no fair, -$415 on 25,223 contracts (-1.65c each; bids -5.5c). From 10/04, behind the gate, -$733 on 147,888 (-0.50c).
- **By side:** bids -$1,496 (-2.1c a contract); asks +$347 (+0.3c).
- **The worst fills:**
  - Nacua's receptions escalator: 400 sold at 21.8c, settled 100c, -$313 (MNF 10/12).
  - Robinson's receptions escalator: 400 bought at 40c, settled 0c, -$160 (10/03).
  - Jonathan Taylor's rushing escalator: 650 bought at 15.1c, settled 0c, -$98 (10/11).
- **Credits fell from Week 4 to Week 5 while size rose** (from x2 overnight only to x3-x4 every hour): the pool fell 25% and our share 1.5% -> 1.3% as other makers answered.
- **Paid against the estimate:** escalators 1.57-1.74x, ladders 1.10-1.13x.

### 5.3 What it taught

- **A fair before anything rests:** rule 1, the same lesson as type B.
- **The non-participation clause sets the cutoff:** rule 2.
- **The schedule, not Kalshi's dates:** rule 3.
- **Team-wide news holds:** rule 4.
- **The losing side and the cash:** rules 5-6.
- **Dilution within hours:** rule 7. This is the 11/11 nominee lesson, five weeks earlier.
- **Estimate vs paid:** rule 8.
- **Prime time:** rule 9.
- **The end:** rule 11.

The size-ups (x2 to x5, then the hour part x3 to x4) were Jack's, slate by slate. They bought rewards until other makers answered, as on every wave since.

### 5.4 If the ladder programs come back

- **The family is still in code and admits them by name (`MEMBER`):** 400 a side every hour, asks on while shard 0 has $3,500+ free, the gate, kickoff cutoffs. Nothing to list.
- **Before the first slate:**
  - `python nfl_prop_fair.py --event <EVENT>` prices every prop (its nflverse cache refreshes every 6 h).
  - The roster reads are fresh.
  - Shard 0's cash covers the asks.
  - Run section 4's checks.
- **Offer Jack (each his call; measure first):**
  - escalators out 60 min before single-game kickoffs (rule 9);
  - the sniper's data-gap guards in the IMM's gate (rule 14);
  - a lower bid cap than the band top + 1c on reward-inflated books (rule 5).

## 6. Other sports so far (since 9/24)

Credits since 9/24 from `reward_credits.csv`; fill P&L = the bot's fills at settlement value, settled markets only.

| Family | Credits | Fill P&L (settled) | Still open | Notes |
|---|---|---|---|---|
| LoL Worlds (league auto) | $1,001 | -$299 | 540 contracts | `KXLOLQUALIFIERS` $719 vs -$369. The 10/16 bait (rule 10). Teemo cut 11/01. |
| NHL season totals (league auto) | $410 | none settled | 2,325 contracts | Out every night the NHL plays |
| MLB, other (league auto) | $338 | +$81 | 25 | `KXMLBSERIESHR` +$168, `KXMLBPLAYOFFHOST` -$148 |
| NFL, other (auto family) | $241 | -$39 | 0 | |
| Super Bowl halftime | $173 | +$119 | 1,375 contracts | |
| WNBA Finals MVP | $141 | -$44 | 0 | |
| MLB postseason MVPs | $97 | -$138 | 0 | Dark for two days on shard-3 cash (rule 6) |
| NBA | $36 | +$159 | 0 | |
| Marathons | $26 | -$59 | 0 | |
| NFL H2H | $22 | no fills | 0 | |
| **Total** | **~$2.5k** | **~-$0.2k** | | The NFL ladder wave alone earned ~3x this |

## 7. Gaps (each one is Jack's call to build)

1. **Non-NFL ladders quote with no fair.** The name pattern admits NBA, WNBA, NHL, MLB, college and MLS ladders and escalators at the same x5 times x4 size, with only safe-join between them and the touch. That is the 10/03 failure mode. NBA events also sit on shard 3. The choice is Jack's: block the non-NFL leagues in the pattern until a fair exists, or accept the risk.
2. **NFL single-game members depend on the ladders' roster read.** The team-news hold for H2H and for NFL auto game members reads ESPN rosters. `nfl_prop_fair` fetches those only for teams with an open ladder or escalator market. With no ladders listed (none since 10/20), these members fail closed and rest nothing. This is read from code; not yet seen live.
3. **No pick-off watch covers the sports families.** Pass 8 reads the wave list only. The cover is the toxic halts, the sweep and group breakers, and the auto families' -$100/day halts per event. Review fills per slate (section 4).
4. **Escalators still quote to the kickoff of single prime-time games** (rule 9).
5. **The IMM's NFL fair lacks the sniper's data-gap guards** (rule 14). Its bid cap (band top + 1c) sits where reward-inflated bids already are (rule 5).
6. **Caps are in contracts, not dollars.** 750 contracts of a 2c escalator and 750 of a 60c ladder are different risks.
7. **Leagues with no news hold:** WNBA, MLB and NHL game members. College, soccer and tennis have no family at all (they read `CHECK`).

## 8. Files, logs and knobs

| What | Where |
|---|---|
| The NFL fair | `nfl_prop_fair.py`, run as `python nfl_prop_fair.py [--event E \| --ticker T]`. In the bot it is a thread every 120 s (`IMM_NFL_FAIR_REFRESH_SECS`). Status: `run-logs/incentive-mm/nfl_prop_fair.json`. nflverse cache: `run-logs/incentive-mm/nfl_stats/`. |
| Gate knobs (code defaults) | `IMM_NFL_FAIR_ENABLE` 1, `_FAIR_TOL_CENTS` 1, `_BAND_TOL_CENTS` 3, `_FAIR_TTL_MIN` 30, `_ROSTER_TTL_MIN` 45, `IMM_NFL_NEWS_HOLD_MIN` 15, `IMM_NFL_RECENT_BAND` 1, `IMM_NFL_FFPTS_START_BUFFER_MIN` 90 |
| Family knobs (code defaults; the launcher sets none) | `IMM_SPORTS_LADDER_SIZE_MULT` 5, `_HOUR_MULT` 4, `_MIN_EST_TOTAL` 1.00, `_START_BUFFER_MIN` 0, `_PRICE_MIN` / `_MAX` 1 / 99, `_PRICE_MIN_EXACT` / `_MAX_EXACT` 0.05 / 99.5, `_ASKS_OFF` 1 with `_ASKS_ON_CASH` 3500 / `_ASKS_OFF_CASH` 1500, `_BLOCK` 0, `_UNWIND_FULL` 1. A launcher change is a restart (`restart_imm.ps1 -Task`), and it is Jack's word. |
| Auto families | `run-logs/incentive-mm/nfl_auto_series.json` and `league_auto_series.json` are hot-loaded, and the family watch writes them. To keep a series out, cut it in code (57ae4d3 / 226016e did this for `KXLOLTEEMO`). `IMM_NFL_AUTO_*`, `IMM_LEAGUE_AUTO_*`, `IMM_LEAGUE_NEWS_*`. `league_news.py` reads ESPN's NBA injuries every 120 s. `family-watch.log` shows enrollments. |
| Logs | `selection_snapshot_<date>.jsonl` (hourly; every candidate's decision, est_total, banked, projected_total, floor_bar, book_depth_contracts), `selection_events_<date>.jsonl`, `guard_skips_<date>.jsonl`, `fills_<date>.jsonl`, and the day log's stand-aside / resume lines |
| Settlements and credits | `settlements_archive_thru_<date>.json` (`revenue` / the count held = the scalar value), `reward_credits.csv`, `reward_programs.json` (`reward` / 10,000 = $) |
| Contract rules | Kalshi's contract PDFs: FOOTBALLENTITYSCALARSTAT (receptions / yards), FFPTSSCALAR (fantasy), FOOTBALLH2HSTAT (H2H), ESPORTSENTITYSTAT (LoL props) |
| ESPN | `site.web.api.espn.com`: `site.api.espn.com` has answered 403 since ~09/2026. Scoreboard dates: one day per call. |
| The sniper | `nfl_snipe_bot.py` and `run_nfl_snipe.ps1 -Live -Subaccount 1` (Jack runs it). `run-logs/nfl-snipe/`, where the HALT file has sat since 11/07. |
| How this file's numbers were made (11/16) | Credits: `reward_credits.csv` summed by event ticker. Fill P&L: each of the bot's fills in `fills_*.jsonl`, (S - price) x (pos_after - pos_before) / 100, where S is the market's settlement value from `settlements_archive_thru_20261115.json` (`revenue` / the count held). Kalshi's pool: `reward_programs.json`. The family regex is `_SPORTS_LADDER_PATTERN`. A slate runs Tuesday to Monday. Each read takes ~2 min. |
| Memories | `imm-nfl-prop-gate`, `imm-ladder-whale-dilution`, `imm-rewards-diagnosis-1015`, `imm-nfl-auto-families`, `imm-nfl-h2h-pilot`, `imm-league-auto-families`, `imm-mlb-mvp-family`, `imm-lol-bait-pickoff-1016`, `nfl-snipe-bot` |
