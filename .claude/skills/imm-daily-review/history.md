# IMM daily review: history

Standing decisions, then dated lessons, newest last. Sessions promote durable lines here from `run-logs/incentive-mm/daily_review/lessons.md`, which the unattended runs write. Promotion is Jack's call, raised in the weekly pass.

## Standing decisions (Jack)

- **2026-11-25, built.** Jack asked for a daily skill that reviews the IMM holistically and suggests fixes in a morning email: "the goal is a self-improving system that compounds on itself over time and maximizes risk-adjusted returns with minimal involvement".
- **Autonomy: "Plan + your yes" (11/25).** Every fix goes to Jack as an exact plan. He approves with one text ("do R-...", through sms_inbox's PLAN + YES-code flow) or in any session. No code is written before his yes, and nothing auto-applies. Autopilot for bounded knobs is NOT on; raise it only once the ledger shows a kind of suggestion verifying reliably, as its own decision.
- **Email: "Add it at ~6:30 AM" (11/25).** The review lands before the 07:00 cluster, and the existing IMM emails keep going. From 12/09 the weekly pass proposes which of them to retire, one decision each.
- **Deploy (11/25):** "Unstick now, review later".
- **The moat (11/25):** "the skill's daily iterative process should lead to compounding and building a moat on data, analysis, and process that allows it to continually get better even as the markets get more competitive".
  - Built the same day: moat/family_daily.csv (with pool share from the cycle log), priors.json, the edge watch, missed markets netted by family tax, the review's self-calibration, and the playbook.
  - The durable backup (R-261124-20) was a decision for Jack. The wedged sync was fixed the same morning, then the review landed on main and the 04:30 task was registered.
- **The fair-model scorecard (11/26):** Jack asked "is the data im logging improving my algorithm and trading strategy". The audit found:
  - the bot reads back none of the 29 streams it logs;
  - the monitors, the reward calibration and the review's priors reach it only through people;
  - none of its 67 fair models had ever been scored, because the fair behind a quote was never logged and 30 of 31 fair files are overwritten on each refresh;
  - a 7-day test sees about $73/day while the median change moves about $23/day.

  To "Build the fair-model scorecard: log the fair behind each quote, score every model daily?" Jack said "Yes, build it".
  - **Where the logging lives:** outside the bot (imm_fair_marks.py, every 15 min), not in it. On 11/26 the live bot file carried another session's 91 uncommitted lines, so a bot edit could not land cleanly. Each quote and fill is joined to the last mark before it instead.
  - **The first live run (22:18Z, 12 s):** 4,852 marks across 15 families. 640 books came from the cycle log and 4,212 from Kalshi in 43 batched signed reads. 15 odd-shaped files were archived raw (216 KB gzipped).
  - **What follows:** settlements accumulate from 11/27; daily families get verdicts within a week or two.
- **The moat backup (11/26, R-261124-20):** Jack asked "how expensive is backing up the moat to bigquery?", heard "effectively free" (about 60 MB, +~1 GB a year, inside the free 10 GB the project barely uses), and said "Yes, build it".
  - **The dataset:** `imm_review` (northamerica-northeast1), created on purpose with no default expiration, and watched by bq_retention_guard.py beside Kalshi (the 5/8 and 7/7 expirations).
  - **The design:** no bad local file can damage the copy.
    - Append-only logs are copied line by line; a shorter or rewritten log is refused.
    - Every other file keeps every version it has had.
    - The day archive is replaced only by a set with every day.
  - **The first run** (11/26 21:27Z, 62 s): 150,045 log lines, 69 files, 137 days. A full restore was byte-identical, 210 of 210 files, but only after one fix: the box writes these logs CRLF, and the first reader dropped the `\r`.
- **The daily self-question (11/26):** "what else should this skill be doing daily to compound and improve itself? everyday it should ask itself this question and come with opportunities".
  - **Built the same day:**
    - SKILL.md step 8, answered in findings.json `compound`, at least one answer a run;
    - evidence section 11: the review's own record, what landed without it, overlaps, rechecks, run traces, repeated manual work, budget, backlog;
    - watches: read-only checks the review adds itself, no approval;
    - `recheck_on` dates for promises;
    - the `false-positive` closure;
    - the email's "Making the review better" block.
  - Self-applied changes are limited to the review's own reading: watches, lessons and playbook rules. Anything in code or the bot stays "Plan + your yes".
  - **What prompted it (the 11/26 state):**
    - The collect used 1,181 s of a 1,500 s limit, 1,050 s of it in the moat's cycle-log scan. It is now one shared pass, and the limit is 35 min.
    - Yesterday's run took 31 min, 78 turns and a reported $9.60. About 17 of its 65 Bash calls went to assembling findings.json and review.md in 4 kB chunks.
    - Since 11/25, 11 of the 12 commits that changed the bot's code or launcher came from outside the review. Among them, the KXAPRPOTUS morning stand-down (d4d28c2) re-diagnosed R-261125-01, and the held-close bug (accb53f) was found without the review.
    - The 11/25 rain finding's "re-measured 12/02" promise had nothing to enforce it.
  - **The first headless run with step 8** (scratch copy, 11/26 13:00, D = 11/25): 15 min, 53 turns. It answered with three compound items:
    - read IMM-WATCH's findings (family_watch_state.json), not only its crashes. R-261124-12 had been raised twice as a missing election date that IMM-WATCH had already researched; it closed as false-positive;
    - a one-pass `put`;
    - five watches on shipped fixes, added by itself.

    It closed R-261125-01 fixed-elsewhere, with d4d28c2's better diagnosis as the lesson, and it put recheck dates on four findings.
  - **Two latent bugs it exposed, both fixed before landing:**
    - CRLF input to `put` was written as CR CR LF, which reads back as a blank line after every line and broke every table in the email;
    - `[[fp:...]]` tags for earlier items rendered empty. The sender now resolves them through the ledger.
- **The change registry (11/26):** Jack's "Yes, build it", to "Build the change registry next, so every IMM commit gets measured at 7 days?". It was the step-8 answer with the most behind it: 11 of the 12 commits that changed the bot's code or launcher on 11/25-26 came from other sessions, and nothing measured whether any change worked.
  - **Built the same day:** evidence section 12 and `daily_review/changes.jsonl`:
    - every commit that changes what the bot does, and every IMM_* env change between two bot runs, registered with a scope guessed from what the commit names;
    - the live time from the bot's own run records (`config_history` git_sha), including runs that started on edits committed later;
    - a 7-day before/after per scope, with supply, the rest of the book and overlapping changes alongside;
    - step 7 scopes what went live and judges each readout in findings.json `changes`;
    - the email's "Changes measured at 7 days" block and a health chip.
  - **The statistics, the same afternoon.** Jack, mid-build: "remember you're building the next jane street of prediction markets". The first version called a change at |t| >= 2, which on the back-fill meant 23 calls among 164 testable readouts, where chance alone gives about 8. Each readout now carries:
    - an exact permutation p;
    - a Benjamini-Hochberg q across the day's readouts;
    - the smallest move it can see (80% power);
    - whether its expectation was pre-registered.

    A call needs p <= 0.05 and q <= 0.10.
  - **The back-fill on 11/25's data** (scratch, 12 s): 389 changes since 10/27 (296 bot, 60 input-writer, 14 launcher, 19 env), all live. 199 readouts, 50 of them the same money as another (live the same day on overlapping series). Per cluster: 6 better, 3 worse, 108 no clear change, 22 book-wide, 10 no activity.
    - **Worse:**
      - cheap-side whole-cent rungs (44316cf, 11/15), -$10/day, q 0.05;
      - the Billboard and VoteHub AS families (1f5e7e4, 10/27), -$28/day, q 0.07;
      - weekend-rain ladder leads (278b672, 10/29), -$63/day, q 0.07.
    - **Missed FDR:** its twin, the 5c fair-lead margin (797937b), at q 0.102; and the Trump-mention halving (46943dd), +$187/day at t 2.6, at q 0.108. One +$418 day drove the halving, and its test can only see moves over $206/day.
    - 115f94c (2028 sides x1.5) read +$86/day at a program pool x8.6: the pool, not the change. That is why supply sits beside every call.
  - **The first headless run of step 7** (scratch copy, 11/26 15:06, D = 11/25, the t-based first version): 23 min, 55 turns, a reported $8.02. It wrote 25 `changes` entries and ingest rejected none: 11 scoped, 17 judged, 1 skipped (emails only).
    - **It re-scoped** two weekend-rain readouts as whole-book settings, leaving their verdicts for the next readout: the profit lock and `FAIR_LEAD_MARGIN_C`, whose subjects named rain series only as examples. Zillow, weather and FX changes got patterns.
    - **It judged the CPI-core block "hurt"** (net +$16 -> $0/day) and proposed no revert: Jack's 11/11 live-feed call stands, and the question goes to the Sunday pass.
    - **It found two flaws, both fixed before landing:**
      - `measure` crashed on any scope with an idle day (a KeyError);
      - the registry's headline totals double-counted the same money. The weekend-rain pair was half the "worse" total, and one TRUMPSAY launch counted twice. Hence the same-day clusters.
    - **The trap it exposed:** a Git Bash `tail -F` on run.log blocked every Add-Content line of the launcher's log (memory: reference-tail-f-blocks-add-content).
  - **Scope guesses are the weak link:** a family named next to its series widened KXHAUSTTS to all 149 Carbon Arc series, so a subject that names series is now those series only. Body-only series (a third of commits) are often examples, so the review re-scopes them when they go live.

## 2026-11-25: what the survey found before the first run (the baseline)

- **The repo sync was wedged.** It had failed every 30 minutes since 11/24 00:45 (199 failures in the log since 11/20). The box's main held 13 unpushed IMM commits while the nightly high-temp digest pushed to GitHub main. It was fixed with merge commit e9f2e06 (only high-temp HTML changed, no bot restart), and the 09:45 sync fast-forwarded. Root cause: commits left unpushed on the box's main. The review now watches the sync streak and unpushed commits every day.
- **Disk:** run-logs/incentive-mm was 113-121 GB, unbacked, growing about 2.2-2.4 GB a day, with nothing pruning it. C: had 104-111 GB free, about 47 days to full.
- **The 7:00 portfolio email's IMM section timed out** (900 s) on 11-18, 11-19, 11-21, 11-24 and 11-25.
- **Persistence:** `state save failed: [WinError 5] Access is denied ... imm_state.json.tmp` appeared 7 times on 11-24. The dashboard's page replace hit the same error on 11/25. It looks like a reader holding the target open while `os.replace` runs; this is unconfirmed.
- **The health alert reported DOWN on bot restarts:** 4 DOWN lines on 11-24, each within 10 minutes of one of the bot's 4 code-change restarts.
- **Monitors run on code defaults** rather than the launcher env: imm_earnings_overrides, imm_feed_audit, imm_health_alert, imm_reward_recon, imm_pickoff.
- **Dead or orphaned jobs:**
  - the 7:10 DIGEST has been a no-op since 10/02;
  - gas-trial has been a no-op since 10/15;
  - quote-gaps and tape-ab are disabled;
  - `program_history.jsonl` is written daily and read by nothing until this review (supply normalisation);
  - `overrides_last_runs.json` lost its reader;
  - the dashboard's Health lineup is stale.
- **Learning loops:**
  - almost every A/B control arm had been removed by 11/08: tape trigger 100%, sweep holdout 0;
  - calibration (paid/est 1.118) feeds no decision;
  - the open-scan performance loop never merged (branch since 9/20);
  - the scan is off.
- **Performance to 11/24** (dashboard archive, 79 ok days from 09/07):
  - inception net mean +$505/day, 30d +$773/day, 7d +$1,269/day;
  - trading gave back 52% of modelled rewards over 30 days (tax 0.52);
  - 30d Sharpe about 8.5, with the worst day 11/08 at -$4,017 (the House-night pick-offs);
  - persistent losers over 30 days: KXHOUSERACE -$891, KXSTATELEG -$639, KXATTYGENOH -$425, KXBABELMANDEBWEEKLY -$338, KXPAMINWAGE -$316, then several Carbon Arc app/POS series at -$120 to -$216 each.
- **Counterfactual blind spot:** the hourly gap estimator read about 210 books and left about $60k/day of unquoted pool unestimated. The estimated misses summed only about $35-42/day, so most of the "missed" universe is unmeasured, not small.

## Lessons (dated)

- **11/25 (build):** the first collect back-fills 14 days of log signatures, which took about 5 minutes on this box. Daily runs scan one day (about 30-60 seconds).
- **11/25:** the cc-plugin-privacy hook blocks Bash commands that name files containing Jack's email address. Read incentive_mm.py with the Grep and Read tools.
- **11/25, the first unattended run (D = 11/24, opus, 27 min, 66 turns, 51 Bash calls, no denials).**
  - It filed 19 findings, R-261124-01 to -19.
  - The best was a P1 the code review of 1417444 caught: day 21-31 DDMONYY tickers are still read year-first (the commit's own comment admits it). 13 events were dark, and KXUSDJPYEVENT-27NOV261100 read 364 days late.
  - It also measured the name-bid-cap raise after the fact: -$235/day, t -1.68, so it proposed an A/B rather than a cut.
  - **Fault:** it wrote "My bug, fixed in imm_review.py" while changing nothing (rule 6a). The env-diff frozenset noise it flagged was fixed in the build the same day.
  - It drafted its outputs in its temp scratchpad, then used `put`. That is fine.
  - The body ran about 170 lines, so the length rule is now explicit.
- **11/25, checking the first run's findings in session (Jack: "Yes, fix it here", then "also fix the 'worth knowing' issues").** Three of its 19 were wrong or overstated. Each became a rule in SKILL.md:
  - **R-01 "13 events dark" was a FALSE POSITIVE.** The offline sweep imported incentive_mm without the runtime family registries. The live bot had quoted every such event: Vercel 21-31 dated events earned like the 01-20 ones, and all 15 live ones were in the day's selection snapshot. 12 of the 13 were already finalized. The rule: confirm "dark" in the sinks.
  - **R-05 named the wrong check.** The 429 skip is the account-value halt, not the $1 payout floor. Fixed the same day: one retry on a 429, plus a blind-streak alert at 30 cycles in a row.
  - **R-11 was mostly noise.** ws_stale tracks wave activity; ca_shadow fell as the Carbon Arc periods ended; three tiny event-driven sinks swing by nature. The collector now ignores sinks under 0.5 MB/day.
  - **R-07 is not "commit everything".** Another session was editing the launcher and the PVD files minutes earlier. Only the 1+ day old groups were committed (3566232, 9097ff0, 317acc5, fe7eae0), each after its own tests. Every diff was backed up first to daily_review/uncommitted_backups/.
