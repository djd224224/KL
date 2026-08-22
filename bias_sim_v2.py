"""Theoretical-max simulation: assume `hi_no_config` is NOT the binding cap,
so every bid moves with the corrected fair_no. This isolates the bias
correction's potential impact when not blocked by the static hi_no_config
floor that dominates 68 % of current markets.

Compares two policies for treatment-arm bid logic:
  realized:     min(hi_no_config, raw_fair_no - safety_margin)        — what bot did
  full_dynamic: min(99, corrected_fair_no - safety_margin)             — uncapped
"""
import csv
from collections import defaultdict
from datetime import date, timedelta
from scipy.stats import norm

LOOKBACK_DAYS = 14
MIN_SAMPLE = 5
SHRINKAGE_N = 10
MAX_CORRECTION_F = 2.0
YES_PROB_CUTOFF = 0.20
SAFETY_MARGIN = 3.0

history = defaultdict(dict)
with open('bias_history.csv') as f:
    for row in csv.DictReader(f):
        d = date.fromisoformat(row['d'][:10])
        history[row['city']][d] = (float(row['fcst']), float(row['actual']),
                                    float(row['err']))

markets = {}
with open('bias_sim_input.csv') as f:
    for row in csv.DictReader(f):
        markets[row['market_ticker']] = {
            'event_date': date.fromisoformat(row['event_date'][:10]),
            'city': row['city'], 'low': float(row['low_range']),
            'high': float(row['high_range']), 'fcst': float(row['forecast_avg']),
            'std': float(row['forecast_std']), 'raw_yes': float(row['raw_yes_prob']),
            'outcome': int(row['outcome_yes']), 'pnl': float(row['pnl_dollars']),
            'capital': float(row['no_total_cost_dollars']),
            'contracts': abs(int(row['net_position'])) if row['net_position'] else 0,
        }
with open('bias_sim_orders.csv') as f:
    for row in csv.DictReader(f):
        m = markets.get(row['market_ticker'])
        if m is None: continue
        m['hi_no_config'] = float(row['hi_no_config']) if row['hi_no_config'] else None
        m['effective_hi'] = float(row['effective_hi_no']) if row['effective_hi_no'] else None
        m['fair_no_raw'] = float(row['fair_no_cents']) if row['fair_no_cents'] else None

def trailing_bias(target):
    raw = {}
    for city, days in history.items():
        errs = [e for d, (_, _, e) in days.items()
                if target - timedelta(days=LOOKBACK_DAYS) <= d < target]
        if errs: raw[city] = (sum(errs) / len(errs), len(errs))
    total = sum(n for _, n in raw.values())
    gb = sum(b * n for b, n in raw.values()) / total if total else 0.0
    out = {}
    for c in history:
        if c in raw:
            b, n = raw[c]
            corr = gb if n < MIN_SAMPLE else (n / (n + SHRINKAGE_N)) * b + (SHRINKAGE_N / (n + SHRINKAGE_N)) * gb
            out[c] = max(-MAX_CORRECTION_F, min(MAX_CORRECTION_F, corr))
        else:
            out[c] = max(-MAX_CORRECTION_F, min(MAX_CORRECTION_F, gb))
    return out

cache = {}
per_city = defaultdict(lambda: {'mkts': 0, 'real': 0.0, 'real_cap': 0.0,
                                  'filt': 0.0, 'cap_save_curr': 0.0, 'cap_save_full': 0.0,
                                  'corr': []})

for m in markets.values():
    if m['event_date'] not in cache:
        cache[m['event_date']] = trailing_bias(m['event_date'])
    corr = cache[m['event_date']].get(m['city'], 0.0)
    new_yes = (norm.cdf(m['high'], loc=m['fcst']+corr, scale=m['std']) -
               norm.cdf(m['low'],  loc=m['fcst']+corr, scale=m['std']))
    new_fair_no_c = 100.0 * (1.0 - new_yes)

    pc = per_city[m['city']]
    pc['mkts'] += 1
    pc['real'] += m['pnl']
    pc['real_cap'] += m['capital']
    pc['corr'].append(corr)

    raw_pass = m['raw_yes'] > YES_PROB_CUTOFF
    new_pass = new_yes > YES_PROB_CUTOFF
    if raw_pass and not new_pass:
        pc['filt'] += m['pnl']
        continue

    # Current architecture: cap = min(hi_no_config, fair_no - safety)
    if m.get('effective_hi') and m.get('hi_no_config'):
        new_cap_curr = min(m['hi_no_config'], new_fair_no_c - SAFETY_MARGIN)
        delta_curr = max(0.0, m['effective_hi'] - new_cap_curr)
        pc['cap_save_curr'] += delta_curr / 100.0 * m['contracts']

        # Theoretical max (full-dynamic, no hi_no_config floor):
        # bot's old bid was effective_hi. Under full-dynamic policy + correction,
        # bid would be (new_fair_no - safety). Savings = max(0, eff_hi - that).
        new_cap_full = new_fair_no_c - SAFETY_MARGIN
        delta_full = max(0.0, m['effective_hi'] - new_cap_full)
        pc['cap_save_full'] += delta_full / 100.0 * m['contracts']

rows = []
for city, pc in per_city.items():
    ctr_curr = pc['real'] - pc['filt'] + pc['cap_save_curr']
    ctr_full = pc['real'] - pc['filt'] + pc['cap_save_full']
    rows.append((city, pc['mkts'], pc['real'], ctr_curr, ctr_full,
                 ctr_curr - pc['real'], ctr_full - pc['real'],
                 sum(pc['corr']) / len(pc['corr'])))

rows.sort(key=lambda r: r[2])
print(f"{'City':<16} {'mkts':>4} {'real':>8} {'curr_arch':>9} {'full_dyn':>9} "
      f"{'curr_d':>8} {'full_d':>8} {'corr':>6}")
print("-" * 92)
for r in rows:
    print(f"{r[0]:<16} {r[1]:>4} {r[2]:>+8.0f} {r[3]:>+9.0f} {r[4]:>+9.0f} "
          f"{r[5]:>+8.0f} {r[6]:>+8.0f} {r[7]:>+6.2f}")
print("-" * 92)
t = lambda i: sum(r[i] for r in rows)
print(f"{'TOTAL':<16} {t(1):>4} {t(2):>+8.0f} {t(3):>+9.0f} {t(4):>+9.0f} "
      f"{t(5):>+8.0f} {t(6):>+8.0f}")
print()
print(f"Current architecture recovery: {t(5):+.0f} ({t(5)/abs(t(2))*100:+.1f}%)")
print(f"Full-dynamic recovery:         {t(6):+.0f} ({t(6)/abs(t(2))*100:+.1f}%)")
print(f"Gap (architecture limit):      {t(6)-t(5):+.0f} (what hi_no_config blocks)")
