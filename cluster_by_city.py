import csv, math, re
from collections import defaultdict

PATH = r"C:/Users/jackd/Downloads/Kalshi-Recent-Activity-Settlement (jane).csv"

def f(x):
    try: return float(x)
    except: return 0.0

# city = chars after KXHIGH up to first '-'
def city_of(t):
    m = re.match(r'KXHIGH([A-Z]+)-', t)
    return m.group(1) if m else None

daily = defaultdict(lambda: defaultdict(float))  # city -> date -> pnl
with open(PATH, encoding='utf-8-sig') as fh:
    for r in csv.DictReader(fh):
        if r['type'] != 'Settlement': continue
        t = r['Market_Ticker']
        city = city_of(t)
        if not city: continue
        yc=f(r['Yes_Contracts_Owned']); ya=f(r['Yes_Contracts_Average_Price_In_Cents'])
        nc=f(r['No_Contracts_Owned']);  na=f(r['No_Contracts_Average_Price_In_Cents'])
        if yc==0 and nc==0: continue
        cost = yc*ya + nc*na
        res = r['Result'].strip().lower()
        gross = yc if res=='yes' else (nc if res=='no' else 0.0)
        daily[city][r['Original_Date'][:10]] += gross-cost

def autocorr(x, lag):
    n=len(x); m=sum(x)/n
    den=sum((xi-m)**2 for xi in x)
    if not den: return 0.0
    return sum((x[i]-m)*(x[i+lag]-m) for i in range(n-lag))/den

def runs_z(signs):
    M=len(signs); npos=sum(1 for s in signs if s>0); nneg=M-npos
    if npos==0 or nneg==0: return None
    runs=1+sum(1 for i in range(1,M) if signs[i]!=signs[i-1])
    exp=1+2*npos*nneg/M
    var=(2*npos*nneg*(2*npos*nneg-M))/(M*M*(M-1))
    sd=math.sqrt(var) if var>0 else 0
    return (runs,exp,(runs-exp)/sd if sd else 0.0)

rows=[]
for city in sorted(daily, key=lambda c: -sum(daily[c].values())):
    d=daily[city]; days=sorted(d); vals=[d[x] for x in days]; N=len(vals)
    total=sum(vals); pos=sum(1 for v in vals if v>0); neg=sum(1 for v in vals if v<0)
    ac1=autocorr(vals,1) if N>2 else float('nan')
    z1=ac1*math.sqrt(N) if N>2 else float('nan')
    signs=[1 if v>0 else -1 for v in vals if v!=0]
    rz=runs_z(signs)
    # conditional persistence
    M=len(signs)
    uu=ut=dd=dt=0
    for i in range(1,M):
        if signs[i-1]==1: ut+=1; uu+=signs[i]==1
        else: dt+=1; dd+=signs[i]==-1
    rows.append((city,N,total,pos,neg,ac1,z1,rz,
                 uu/ut if ut else float('nan'), dd/dt if dt else float('nan')))

print(f"{'City':<6}{'Days':>5}{'Total$':>11}{'Up%':>6}{'AC1':>8}{'z(AC1)':>8}"
      f"{'Runs z':>8}{'P(u|u)':>8}{'P(d|d)':>8}")
print('-'*68)
for c,N,tot,pos,neg,ac1,z1,rz,puu,pdd in rows:
    rzs = f"{rz[2]:+.2f}" if rz else "  n/a"
    print(f"{c:<6}{N:>5}{tot:>11,.0f}{pos/N:>6.0%}{ac1:>+8.3f}{z1:>+8.2f}"
          f"{rzs:>8}{puu:>8.0%}{pdd:>8.0%}")

print("\nFlags: |z(AC1)|>1.96 or |Runs z|>1.96 = significant at 5%")
sig=[r for r in rows if (not math.isnan(r[6]) and abs(r[6])>1.96) or (r[7] and abs(r[7][2])>1.96)]
if sig:
    for r in sig: print(f"  * {r[0]}: AC1 z={r[6]:+.2f}, Runs z={r[7][2]:+.2f}")
else:
    print("  none — no city shows significant daily clustering")
