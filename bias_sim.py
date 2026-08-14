"""Walk-forward simulation of the rolling bias correction over 5/17–5/26.

For each settled market we evaluate three effects:

  (1) FILTER  — would the bot's `yes_prob > 0.20` cutoff have flipped?
                Markets that flip traded→skipped contribute zero P&L
                (we sidestep the loss/win entirely).

  (2) BID CAP (treatment arm) — the dynamic cap is
                `min(hi_no_config, fair_no − safety_margin)`. Bias
                correction lowers fair_no, which (in the treatment arm)
                may lower the cap. We assume same contract count fills
                at the lower price (UPPER BOUND on savings — ignores
                fill-rate degradation at lower bids).

  (3) BID CAP (control arm) — unchanged; control uses hi_no_config
                directly. No impact.

P&L impact of bid-cap drop on a NO position of N contracts:
  improvement = (realized_effective_hi_no − corrected_effective_hi_no) / 100 × N

  (Lower paid price means WIN P&L is bigger AND LOSS P&L is smaller — both
   directions improve, regardless of outcome.)

Walk-forward bias: at each market's event_date we use ONLY the trailing
14 days strictly before that date — so the simulation mirrors what a
live bot would have computed at the time.
"""
import csv
from collections import defaultdict
from datetime import date, timedelta
from scipy.stats import norm

LOOKBACK_DAYS    = 14
MIN_SAMPLE       = 5
SHRINKAGE_N      = 10
MAX_CORRECTION_F = 2.0
YES_PROB_CUTOFF  = 0.20

# Per-(city, date) forecast + actual
history = defaultdict(dict)
with open('bias_history.csv') as f:
    for row in csv.DictReader(f):
        d = date.fromisoformat(row['d'][:10])
        history[row['city']][d] = (
            float(row['fcst']), float(row['actual']), float(row['err'])
        )

# Settled markets 5/17–5/26
markets = {}
with open('bias_sim_input.csv') as f:
    for row in csv.DictReader(f):
        markets[row['market_ticker']] = {
            'event_date': date.fromisoformat(row['event_date'][:10]),
            'city':       row['city'],
            'city_abv':   row['city_abv'],
            'low_range':  float(row['low_range']),
            'high_range': float(row['high_range']),
            'fcst':       float(row['forecast_avg']),
            'std':        float(row['forecast_std']),
            'raw_yes':    float(row['raw_yes_prob']),
            'outcome':    int(row['outcome_yes']),
            'pnl':        float(row['pnl_dollars']),
            'capital':    float(row['no_total_cost_dollars']),
            'contracts':  abs(int(row['net_position'])) if row['net_position'] else 0,
        }

# AB arm + bid caps from orders table (one row per market, deduped)
with open('bias_sim_orders.csv') as f:
    for row in csv.DictReader(f):
        m = markets.get(row['market_ticker'])
        if m is None:
            continue
        m['hi_no_config']  = float(row['hi_no_config'])  if row['hi_no_config']  else None
        m['fair_no_cents'] = float(row['fair_no_cents']) if row['fair_no_cents'] else None
        m['effective_hi']  = float(row['effective_hi_no']) if row['effective_hi_no'] else None
        m['ab_arm']        = row['ab_arm']
        m['safety_margin'] = float(row['safety_margin']) if row['safety_margin'] else 3.0

def trailing_bias_on(target_date):
    """Bias map as the bot would compute it on target_date using only
    forecasts/actuals strictly BEFORE target_date."""
    raw = {}
    for city, days in history.items():
        errs = [err for d, (_, _, err) in days.items()
                if target_date - timedelta(days=LOOKBACK_DAYS) <= d < target_date]
        if errs:
            raw[city] = (sum(errs) / len(errs), len(errs))

    total_n = sum(n for _, n in raw.values())
    gb = sum(b * n for b, n in raw.values()) / total_n if total_n else 0.0
    out = {}
    for city in history:
        if city in raw:
            b, n = raw[city]
            corr = gb if n < MIN_SAMPLE else (n / (n + SHRINKAGE_N)) * b + (SHRINKAGE_N / (n + SHRINKAGE_N)) * gb
            out[city] = max(-MAX_CORRECTION_F, min(MAX_CORRECTION_F, corr))
        else:
            out[city] = max(-MAX_CORRECTION_F, min(MAX_CORRECTION_F, gb))
    return out, max(-MAX_CORRECTION_F, min(MAX_CORRECTION_F, gb))

per_city = defaultdict(lambda: {
    'markets': 0, 'realized_pnl': 0.0, 'realized_capital': 0.0,
    'filt_out_n': 0, 'filt_out_pnl': 0.0,
    'cap_savings': 0.0, 'cap_treatment_n': 0,
    'corrections': [],
})

bias_cache = {}
for m in markets.values():
    if m['event_date'] not in bias_cache:
        bias_cache[m['event_date']] = trailing_bias_on(m['event_date'])
    bias_map, gb = bias_cache[m['event_date']]
    corr = bias_map.get(m['city'], gb)

    corrected_avg = m['fcst'] + corr
    corrected_yes = (
        norm.cdf(m['high_range'], loc=corrected_avg, scale=m['std']) -
        norm.cdf(m['low_range'],  loc=corrected_avg, scale=m['std'])
    )
    corrected_fair_no_cents = 100.0 * (1.0 - corrected_yes)

    pc = per_city[m['city']]
    pc['markets'] += 1
    pc['realized_pnl'] += m['pnl']
    pc['realized_capital'] += m['capital']
    pc['corrections'].append(corr)

    raw_passed  = m['raw_yes']    > YES_PROB_CUTOFF
    corr_passed = corrected_yes > YES_PROB_CUTOFF

    if raw_passed and not corr_passed:
        # Filtered out: avoid this market's P&L entirely
        pc['filt_out_n'] += 1
        pc['filt_out_pnl'] += m['pnl']
        continue  # no cap effect on filtered markets

    if m.get('ab_arm') == 'treatment' and m.get('effective_hi') is not None \
            and m.get('hi_no_config') is not None:
        # New cap = min(hi_no_config, corrected_fair_no - safety_margin)
        new_cap = min(m['hi_no_config'], corrected_fair_no_cents - m['safety_margin'])
        # Only counts if cap moved DOWN (saves money). Floor at 0 (no negative impact assumed).
        delta_cents = max(0.0, m['effective_hi'] - new_cap)
        if delta_cents > 0:
            savings = delta_cents / 100.0 * m['contracts']
            pc['cap_savings'] += savings
            pc['cap_treatment_n'] += 1

rows = []
for city, pc in per_city.items():
    counter_pnl = pc['realized_pnl'] - pc['filt_out_pnl'] + pc['cap_savings']
    delta = counter_pnl - pc['realized_pnl']
    avg_corr = sum(pc['corrections']) / len(pc['corrections'])
    rows.append((city, pc['markets'], pc['realized_pnl'], counter_pnl, delta,
                 pc['filt_out_n'], pc['filt_out_pnl'],
                 pc['cap_savings'], pc['cap_treatment_n'], avg_corr))

rows.sort(key=lambda r: r[2])

print(f"{'City':<16} {'mkts':>4} {'real':>9} {'ctr':>9} {'delta':>8} "
      f"{'filt#':>5} {'filt$':>8} {'cap$':>7} {'cap#':>4} {'corr':>6}")
print("-" * 100)
for r in rows:
    print(f"{r[0]:<16} {r[1]:>4} {r[2]:>+9.0f} {r[3]:>+9.0f} {r[4]:>+8.0f} "
          f"{r[5]:>5} {r[6]:>+8.0f} {r[7]:>+7.0f} {r[8]:>4} {r[9]:>+6.2f}")
print("-" * 100)
total_real = sum(r[2] for r in rows)
total_ctr  = sum(r[3] for r in rows)
total_filt = sum(r[6] for r in rows)
total_cap  = sum(r[7] for r in rows)
print(f"{'TOTAL':<16} {sum(r[1] for r in rows):>4} {total_real:>+9.0f} {total_ctr:>+9.0f} "
      f"{total_ctr-total_real:>+8.0f} "
      f"{sum(r[5] for r in rows):>5} {total_filt:>+8.0f} {total_cap:>+7.0f} "
      f"{sum(r[8] for r in rows):>4}")
print()
print(f"Filter effect: {-total_filt:+.0f} ({-total_filt/abs(total_real)*100:+.1f}% of loss)")
print(f"Cap effect:    {total_cap:+.0f} ({total_cap/abs(total_real)*100:+.1f}% of loss)")
print(f"COMBINED:      {total_ctr-total_real:+.0f} ({(total_ctr-total_real)/abs(total_real)*100:+.1f}% recovered)")
