import csv, math
from collections import defaultdict
from datetime import datetime

PATH = r"C:/Users/jackd/Downloads/Kalshi-Recent-Activity-Settlement (jane).csv"

def f(x):
    try: return float(x)
    except: return 0.0

daily = defaultdict(float)
n_settle = 0
with open(PATH, encoding='utf-8-sig') as fh:
    for r in csv.DictReader(fh):
        if r['type'] != 'Settlement': continue
        yc = f(r['Yes_Contracts_Owned']); ya = f(r['Yes_Contracts_Average_Price_In_Cents'])
        nc = f(r['No_Contracts_Owned']);  na = f(r['No_Contracts_Average_Price_In_Cents'])
        if yc==0 and nc==0: continue  # inactive
        cost = yc*ya + nc*na
        res = r['Result'].strip().lower()
        gross = yc if res=='yes' else (nc if res=='no' else 0.0)
        pnl = gross - cost
        d = r['Original_Date'][:10]
        daily[d] += pnl
        n_settle += 1

days = sorted(daily)
vals = [daily[d] for d in days]
N = len(vals)
print(f"Active settlements: {n_settle} | Trading days: {N}")
print(f"Date range: {days[0]} -> {days[-1]}")

mean = sum(vals)/N
pos = sum(1 for v in vals if v>0); neg = sum(1 for v in vals if v<0); zero = N-pos-neg
print(f"\nDaily P&L: mean=${mean:,.2f}  total=${sum(vals):,.2f}")
print(f"Up days: {pos} ({pos/N:.1%}) | Down days: {neg} ({neg/N:.1%}) | Flat: {zero}")

# --- Lag-1 autocorrelation of daily P&L (continuous) ---
def autocorr(x, lag):
    n=len(x); m=sum(x)/n
    num=sum((x[i]-m)*(x[i+lag]-m) for i in range(n-lag))
    den=sum((xi-m)**2 for xi in x)
    return num/den if den else 0.0
print("\n--- Autocorrelation of daily P&L ($) ---")
for lag in (1,2,3,5):
    ac=autocorr(vals,lag)
    se=1/math.sqrt(N)  # approx SE under white noise
    z=ac/se
    print(f"  lag {lag}: r={ac:+.3f}  (z={z:+.2f}{'  *' if abs(z)>1.96 else ''})")

# --- Sign-based clustering (treat days as +/-, drop flats) ---
signs=[1 if v>0 else -1 for v in vals if v!=0]
M=len(signs)
n_pos=sum(1 for s in signs if s>0); n_neg=M-n_pos
# Runs test (Wald-Wolfowitz)
runs=1+sum(1 for i in range(1,M) if signs[i]!=signs[i-1])
exp_runs=1+2*n_pos*n_neg/M
var_runs=(2*n_pos*n_neg*(2*n_pos*n_neg-M))/(M*M*(M-1)) if M>1 else 0
sd_runs=math.sqrt(var_runs) if var_runs>0 else 0
z_runs=(runs-exp_runs)/sd_runs if sd_runs else 0
print("\n--- Runs test on sign of daily P&L (flats dropped) ---")
print(f"  n={M} ({n_pos} up, {n_neg} down)")
print(f"  observed runs={runs}  expected={exp_runs:.1f}  sd={sd_runs:.2f}")
print(f"  z={z_runs:+.2f}  -> {'CLUSTERING (fewer runs)' if z_runs<-1.96 else ('alternating' if z_runs>1.96 else 'no significant clustering')}")

# --- Conditional probabilities ---
def cond(prev_sign):
    tot=cnt=0
    for i in range(1,M):
        if signs[i-1]==prev_sign:
            tot+=1; cnt+= (signs[i]==prev_sign)
    return cnt,tot
up_up,up_tot=cond(1); dn_dn,dn_tot=cond(-1)
base_up=n_pos/M
print("\n--- Conditional probabilities (persistence) ---")
print(f"  Base rate up:           {base_up:.1%}")
print(f"  P(up | prev up):        {up_up/up_tot:.1%}  (n={up_tot})")
print(f"  P(down | prev down):    {dn_dn/dn_tot:.1%}  (n={dn_tot})  base down={1-base_up:.1%}")

# --- Run-length stats ---
def run_lengths(target):
    lengths=[]; cur=0
    for s in signs:
        if s==target: cur+=1
        else:
            if cur>0: lengths.append(cur)
            cur=0
    if cur>0: lengths.append(cur)
    return lengths
up_runs=run_lengths(1); dn_runs=run_lengths(-1)
print("\n--- Run lengths ---")
print(f"  Up streaks:   count={len(up_runs)}  avg={sum(up_runs)/len(up_runs):.2f}  max={max(up_runs)}")
print(f"  Down streaks: count={len(dn_runs)}  avg={sum(dn_runs)/len(dn_runs):.2f}  max={max(dn_runs)}")

# expected avg run length under independence = 1/(1-p) for each
print(f"  (expected avg up streak if independent: {1/(1-base_up):.2f}; down: {1/base_up:.2f})")
