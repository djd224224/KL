# IMM daily review: history

Standing decisions, then dated lessons, newest last. Sessions promote durable lines here from `run-logs/incentive-mm/daily_review/lessons.md`, which the unattended runs write. Promotion is Jack's call, raised in the weekly pass.

## Standing decisions (Jack)

- **2026-11-25, built.** Jack asked for a daily skill that reviews the IMM holistically and suggests fixes in a morning email: "the goal is a self-improving system that compounds on itself over time and maximizes risk-adjusted returns with minimal involvement".
- **Autonomy: "Plan + your yes" (11/25).** Every fix goes to Jack as an exact plan. He approves with one text ("do R-...", through sms_inbox's PLAN + YES-code flow) or in any session. No code is written before his yes, and nothing auto-applies. Autopilot for bounded knobs is NOT on; raise it only once the ledger shows a kind of suggestion verifying reliably, as its own decision.
- **Email: "Add it at ~6:30 AM" (11/25).** The review lands before the 07:00 cluster, and the existing IMM emails keep going. From 12/09 the weekly pass proposes which of them to retire, one decision each.
- **Deploy (11/25):** "Unstick now, review later". The wedged sync was fixed the same morning, then the review landed on main and the 04:30 task was registered.

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
