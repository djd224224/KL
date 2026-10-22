# 15-minute shadow test (Q15)

Jack 2026-10-22: "Should I quote any 15min IMM markets at night?", then "Yes
use a live price feed like Miami temp, with recorder and a week of testing".
The hourly-temp test (TEMP_SHADOW.md) is the template: record everything,
model the fair value from the settlement source, replay quoting policies
against the recorded books with a sweep-aware fill model, judge them on
pre-registered rules, and never trade.

## The markets

Kalshi's paying "price up in next 15 mins?" series, one market per series per
quarter-hour (the crypto race: one per coin), target 300, discount 0.5:

| series | pays per 15-min market | settles on |
|---|---|---|
| KXEURUSD15M KXGBPUSD15M KXUSDJPY15M KXAUDUSD15M KXUSDCAD15M | $50 | Pyth FX.*/USD |
| KXBRENT15M | $50 | Pyth Commodities.Index.BRENT/USD (1-min close) |
| KX2YRRATE15M KX5YRRATE15M KX10YRRATE15M KX30YRRATE15M | $50 | Pyth InterestRate.US* (no programs overnight) |
| KXNATGAS15M KXCOPPER15M KXPLATINUM15M KXPALLADIUM15M | $20 | Pyth commodity / metal feeds |
| KXCRYPTOLEAD15M (BTC ETH SOL XRP HYPE) | $20 each | CF Benchmarks |

Overnight pools on 10/21-22: ~$17.8k across the 11 non-yield series. Maker P&L
at settlement (all makers) swung +-$5-15k per series per night and per day
(report_tools/q15/q15_study.py). The IMM leaves these out on purpose.

## Phase 1: the recorder (q15_collector.py) -- LIVE from 2026-10-22

Task "KL q15-shadow collector" (register_q15_collector.ps1; logon + every
10 min, IgnoreNew, no time limit) runs run_q15_collector.ps1 through
run_q15_collector_hidden.vbs until Q15_SHADOW_UNTIL (2026-10-30T12:00Z) or
KL-data\q15-shadow\HALT. Data under KL-data\q15-shadow:

- raw/<UTC day>/ws_<HH>.jsonl(.gz): Kalshi orderbook_delta + trade, verbatim
  (temp_shadow_collector.WSRecorder, signed with the fleet key like Miami's).
- raw/<UTC day>/px_<HH>.jsonl(.gz): one row a second per price source with
  the symbols that changed: `kraken` (FX bbo mid, a proxy), `hl` (Hyperliquid
  mids for the race coins, a proxy), `pyth` (the settlement source).
- raw/<UTC day>/meta_<HH>.jsonl(.gz): market records (discover / opened /
  settle), programs, status.
- settled/<ticker>.json, collector.log, launcher-<date>.log, collector_status.json.

**Scope: FX and the crypto race (Jack 2026-10-22: "It's paid, do FX and
crypto").** Since 2026-08-26 Pyth's Hermes refuses unkeyed price reads (HTTP
401) and its keys are paid (app.pyth.com/products), so the recorder covers the
five FX series and KXCRYPTOLEAD15M, priced off the free proxies: Kraken's FX
mid (FX settles on Pyth) and Hyperliquid's mids (the race settles on CF
Benchmarks). The fidelity of a proxy is part of what the test measures
(Kalshi's own results are recorded to check it against). Q15_SHADOW_SERIES
and Q15_PYTH=1 (with PYTH_API_KEY or KL-data\q15-shadow\pyth_api_key.txt)
bring the other series back if a key is ever bought.

## Phase 2: sim + daily report -- built 2026-10-22

- q15_sim.py --day <ET day>: one pass over the day's recording -- books from
  the ws stream, the fair (FX: a driftless walk on Kraken's mid, EWMA sigma;
  the race: Monte Carlo of the 5 coins' remaining returns), the
  Q15_PREREG.md variants quoted each second without crossing, rewards via
  temp_shadow_scoring.score (the IMM's exact scorer) at the program's
  target, STRESS fills marked to Kalshi's result, Brier of fair vs mid, the
  FX proxy's call vs Kalshi -> sim/<day>/q15_day.json.
- q15_report.py [--day] [--no-sim] [--send]: sims yesterday, then yesterday +
  the running totals since 10/23 + the pre-registered verdict, emailed via
  send_html_email.py. Task "KL q15-shadow report" (register_q15_report.ps1)
  daily 08:45 ET, 10/24-10/30.
- Not built (reported as a gap): the queue fill model -- the STRESS model
  decides by the pre-registration.
