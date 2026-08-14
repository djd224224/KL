"""Diagnostic: print the per-date walk-forward bias as the bot would have seen
it during the slump. Also check why the cap effect is so small."""
import csv
from collections import defaultdict
from datetime import date, timedelta

LOOKBACK_DAYS = 14
MIN_SAMPLE = 5
SHRINKAGE_N = 10
MAX_CORRECTION_F = 2.0

history = defaultdict(dict)
with open('bias_history.csv') as f:
    for row in csv.DictReader(f):
        d = date.fromisoformat(row['d'][:10])
        history[row['city']][d] = (float(row['fcst']), float(row['actual']),
                                    float(row['err']))

def trailing_bias_on(target_date):
    raw = {}
    for city, days in history.items():
        errs = [err for d, (_, _, err) in days.items()
                if target_date - timedelta(days=LOOKBACK_DAYS) <= d < target_date]
        if errs:
            raw[city] = (sum(errs) / len(errs), len(errs))
    total_n = sum(n for _, n in raw.values())
    gb = sum(b * n for b, n in raw.values()) / total_n if total_n else 0.0
    return raw, max(-MAX_CORRECTION_F, min(MAX_CORRECTION_F, gb))

print("Walk-forward GLOBAL bias by day:")
print(f"{'date':<12} {'global_bias':>11} {'total_n':>8}")
for d in sorted({m['event_date'] for m in []} | set(
    date(2026, 5, 17) + timedelta(days=i) for i in range(10))):
    raw, gb = trailing_bias_on(d)
    total_n = sum(n for _, n in raw.values())
    print(f"{d.isoformat():<12} {gb:>+11.2f} {total_n:>8}")

print("\nWalk-forward Boston bias (city with biggest correction TODAY):")
print(f"{'date':<12} {'n':>3} {'raw_bias':>9}")
for d in (date(2026, 5, 17) + timedelta(days=i) for i in range(10)):
    raw, gb = trailing_bias_on(d)
    if 'Boston' in raw:
        b, n = raw['Boston']
        print(f"{d.isoformat():<12} {n:>3} {b:>+9.2f}")

# Now check: out of 100 markets, how many had hi_no_config WELL below fair_no?
# i.e., the bid was bound by hi_no_config, not by fair_no - margin?
print("\nWhy the cap effect is small — hi_no_config vs fair_no spread:")
buckets = {'cap_dominant': 0, 'fair_no_dominant': 0, 'unclear': 0}
samples = []
with open('bias_sim_orders.csv') as f:
    for row in csv.DictReader(f):
        try:
            hnc = float(row['hi_no_config'])
            fnc = float(row['fair_no_cents'])
            eff = float(row['effective_hi_no'])
            sm  = float(row['safety_margin'])
        except (ValueError, KeyError):
            continue
        treatment_cap = fnc - sm  # what cap would be in treatment arm
        if hnc < treatment_cap - 1:
            buckets['cap_dominant'] += 1  # hi_no_config below cap, dominates
        elif hnc > treatment_cap + 1:
            buckets['fair_no_dominant'] += 1
        else:
            buckets['unclear'] += 1
        if len(samples) < 5:
            samples.append((row['market_ticker'], hnc, fnc, eff, treatment_cap, row['ab_arm']))

print(f"  hi_no_config IS the binding cap: {buckets['cap_dominant']:>3} markets (correction never bites bid)")
print(f"  fair_no - margin IS the cap:     {buckets['fair_no_dominant']:>3} markets (correction can reduce bid)")
print(f"  within 1c of each other:         {buckets['unclear']:>3} markets (edge case)")
print()
print("Sample markets (ticker, hi_no_config, fair_no_cents, effective_hi, treatment_cap, ab_arm):")
for s in samples:
    print(f"  {s[0]:<28} hnc={s[1]:>5} fnc={s[2]:>5} eff={s[3]:>5} treat_cap={s[4]:>5.1f} {s[5]}")
