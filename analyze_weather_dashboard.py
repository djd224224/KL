#!/usr/bin/env python3
"""Weather trading analysis — generates interactive HTML dashboard from Kalshi KXHIGH settlement CSV."""
import sys, csv, json, math, os, re
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_MONTHS = {m: i + 1 for i, m in enumerate(['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'])}
_TICKER_DATE_RE = re.compile(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})')

def event_date_from_ticker(ticker):
    m = _TICKER_DATE_RE.search(ticker or '')
    if not m: return None
    yy, mon, dd = m.groups()
    try: return f'20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}'
    except (KeyError, ValueError): return None

def load_csv(fp):
    if not fp or fp == 'none' or not os.path.exists(fp): return []
    with open(fp, encoding='utf-8-sig') as f: return list(csv.DictReader(f))

def load_volume_cache(path='kxhigh_volume_cache.json'):
    if not os.path.exists(path): return {}
    with open(path) as f: return json.load(f).get('by_city_date', {})

def load_resolved_markets():
    """Pull strike, low_range, actual_high_estimate from BQ KXHIGH_resolved_markets.
    Returns {ticker: {strike, low_range, high_range, actual_high, kind}} or {} on failure."""
    try:
        from google.cloud import bigquery
        client = bigquery.Client(project='elite-contact-446323-q7')
        sql = """SELECT market_ticker, market_strike, low_range, high_range,
                        actual_high_estimate, market_kind
                 FROM `elite-contact-446323-q7.Kalshi.KXHIGH_resolved_markets`
                 WHERE actual_high_estimate IS NOT NULL AND market_strike IS NOT NULL"""
        rows = client.query(sql).result()
        return {r.market_ticker: {
            'strike': float(r.market_strike),
            'low_range': float(r.low_range) if r.low_range is not None else None,
            'high_range': float(r.high_range) if r.high_range is not None else None,
            'actual_high': float(r.actual_high_estimate),
            'kind': r.market_kind or ''} for r in rows}
    except Exception as e:
        print(f"[warn] resolved markets BQ fetch failed: {e}")
        return {}

def parse_date(s):
    if not s: return None
    for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ','%Y-%m-%dT%H:%M:%SZ','%Y-%m-%d'):
        try: return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError: pass
    return None

def parse_ticker(ticker):
    if not ticker: return {'city':'UNKNOWN','series':'','threshold':'','is_B':False,'is_T':False}
    parts = ticker.split('-')
    series = parts[0]
    city = series.replace('KXHIGH','') if series.startswith('KXHIGH') else series
    threshold = parts[-1] if len(parts) >= 3 else ''
    return {'city': city, 'series': series, 'threshold': threshold,
            'is_B': threshold.startswith('B'), 'is_T': threshold.startswith('T'), 'ticker': ticker}

def compute_pnl(row):
    yc = int(float(row.get('Yes_Contracts_Owned') or 0))
    nc = int(float(row.get('No_Contracts_Owned') or 0))
    ya = float(row.get('Yes_Contracts_Average_Price_In_Cents') or 0)
    na = float(row.get('No_Contracts_Average_Price_In_Cents') or 0)
    res = (row.get('Result') or '').strip().lower()
    cost = yc * ya + nc * na
    if res == 'yes': pay = yc * 1.0
    elif res == 'no': pay = nc * 1.0
    else: pay = float(row.get('Profit_In_Dollars') or 0) + cost
    return {'pnl': pay - cost, 'cost': cost, 'yes_c': yc, 'no_c': nc,
            'yes_avg': ya, 'no_avg': na, 'result': res, 'total_c': yc + nc}

def run_analysis(settlement_file, trade_file=None, volume_cache_path='kxhigh_volume_cache.json'):
    raw = load_csv(settlement_file)
    resolved = load_resolved_markets()
    settlements = []
    for row in raw:
        if row.get('type') != 'Settlement': continue
        ticker_str = row.get('Market_Ticker','')
        tk = parse_ticker(ticker_str)
        pnl = compute_pnl(row)
        dt = parse_date(row.get('Original_Date'))
        day = (dt - timedelta(hours=6)).date() if dt else None
        event_str = event_date_from_ticker(ticker_str)
        rm = resolved.get(ticker_str)
        if rm:
            # Reference point: for "tail" markets, use low_range (real YES cutoff);
            # for "between" markets, use market_strike (midpoint of the 2°F window).
            # This gives a consistent "actual_high − boundary" interpretation across both kinds:
            # tail YES iff actual ≥ low_range (distance ≥ 0); between YES iff |distance| < 1.
            if rm['kind'] == 'tail' and rm.get('low_range') is not None:
                ref = rm['low_range']
            else:
                ref = rm['strike']
            distance = rm['actual_high'] - ref
            strike_val = rm['strike']; actual_high = rm['actual_high']; mkt_kind = rm['kind']
        else:
            distance = None; strike_val = None; actual_high = None; mkt_kind = ''
        settlements.append({**tk, **pnl, 'date': dt, 'day': day,
            'day_str': day.isoformat() if day else None,
            'event_str': event_str,
            'dow': day.weekday() if day else None,
            'distance': distance, 'strike_val': strike_val,
            'actual_high': actual_high, 'mkt_kind': mkt_kind})

    active = [s for s in settlements if s['total_c'] > 0 and s['series'].startswith('KXHIGH')]
    if not active:
        print("No active KXHIGH settlements found"); return None
    # Auto-detect cities, sorted by total P&L descending
    city_pnl_totals = defaultdict(float)
    for s in active: city_pnl_totals[s['city']] += s['pnl']
    cities = sorted(city_pnl_totals.keys(), key=lambda c: -city_pnl_totals[c])
    active_cities = active

    # Overview stats
    tp = sum(s['pnl'] for s in active_cities)
    tc = sum(s['cost'] for s in active_cities)
    roi = tp/tc*100 if tc>0 else 0

    daily = defaultdict(lambda: {'pnl':0,'cost':0})
    for s in active_cities:
        if s['day_str']: daily[s['day_str']]['pnl'] += s['pnl']; daily[s['day_str']]['cost'] += s['cost']
    daily_sorted = sorted(daily.items())
    daily_pnls = [v['pnl'] for _,v in daily_sorted]
    daily_costs = [v['cost'] for _,v in daily_sorted]

    n_days = len(daily_pnls)
    avg_d = sum(daily_pnls)/n_days if n_days else 0
    std_d = math.sqrt(sum((p-avg_d)**2 for p in daily_pnls)/(n_days-1)) if n_days>1 else 0
    ann = math.sqrt(365)
    sharpe = (avg_d/std_d*ann) if std_d>0 else None
    if n_days>1 and std_d>0:
        s_d = sharpe/ann
        se_annual = ann*math.sqrt((1+s_d**2/2)/n_days)
        sharpe_lo = round(sharpe-1.96*se_annual,2)
        sharpe_hi = round(sharpe+1.96*se_annual,2)
    else:
        sharpe_lo = sharpe_hi = sharpe
    down = [p for p in daily_pnls if p<0]
    ds = math.sqrt(sum(p**2 for p in down)/len(down)) if down else 0
    sortino = (avg_d/ds*ann) if ds>0 else None
    up_sum = sum(p for p in daily_pnls if p>0); dn_sum = -sum(down)
    omega = (up_sum/dn_sum) if dn_sum>0 else None
    cum=0; peak=0; mdd=0
    for p in daily_pnls:
        cum+=p; peak=max(peak,cum); mdd=max(mdd,peak-cum)
    up_days = sum(1 for p in daily_pnls if p>0)
    dn_days = sum(1 for p in daily_pnls if p<0)
    avg_win = sum(p for p in daily_pnls if p>0)/up_days if up_days else 0
    avg_loss = sum(p for p in daily_pnls if p<0)/dn_days if dn_days else 0

    weeks = defaultdict(float); months = defaultdict(float)
    for ds_key, v in daily_sorted:
        d = datetime.fromisoformat(ds_key)
        weeks[d.isocalendar()[:2]] += v['pnl']
        months[ds_key[:7]] += v['pnl']
    weekly_vals = list(weeks.values()); monthly_vals = list(months.values())
    wk_win = sum(1 for w in weekly_vals if w>0)
    mo_win = sum(1 for m in monthly_vals if m>0)

    # Percentiles
    def pct(arr, p):
        if not arr: return 0
        s = sorted(arr); k = (len(s)-1)*p; f_idx=int(k); c_idx=min(f_idx+1,len(s)-1)
        return s[f_idx]+(s[c_idx]-s[f_idx])*(k-f_idx)
    outlay_nz = [c for c in daily_costs if c>0]
    p25=pct(outlay_nz,0.25); p50=pct(outlay_nz,0.50); p75=pct(outlay_nz,0.75)

    # Recent windows
    if daily_sorted:
        latest = datetime.fromisoformat(daily_sorted[-1][0])
        c7=(latest-timedelta(days=7)).isoformat()[:10]; c30=(latest-timedelta(days=30)).isoformat()[:10]
        c14=(latest-timedelta(days=14)).isoformat()[:10]; c60=(latest-timedelta(days=60)).isoformat()[:10]
        d7=[v for k,v in daily_sorted if k>c7]; d30=[v for k,v in daily_sorted if k>c30]
        d7p=[v for k,v in daily_sorted if c14<k<=c7]; d30p=[v for k,v in daily_sorted if c60<k<=c30]
    else: d7=d30=d7p=d30p=[]

    def wstats(w):
        if not w: return {'pnl':0,'cost':0,'roi':0,'n':0,'wr':0}
        wp=sum(x['pnl'] for x in w); wc=sum(x['cost'] for x in w); ww=sum(1 for x in w if x['pnl']>0)
        return {'pnl':round(wp,2),'cost':round(wc,2),'roi':round(wp/wc*100,1) if wc>0 else 0,'n':len(w),'wr':round(ww/len(w)*100,1)}

    # B vs T aggregate (all cities combined) — settlement-level
    b_all=[s for s in active_cities if s['is_B']]
    t_all=[s for s in active_cities if s['is_T']]
    def _agg(rows):
        n=len(rows)
        if not n: return {'n':0,'pnl':0,'cost':0,'roi':0,'wr':0}
        p=sum(s['pnl'] for s in rows); c=sum(s['cost'] for s in rows)
        w=sum(1 for s in rows if s['pnl']>0)
        return {'n':n,'pnl':round(p,2),'cost':round(c,2),
                'roi':round(p/c*100,1) if c>0 else 0,'wr':round(w/n*100,1)}
    kind_overall={'B':_agg(b_all),'T':_agg(t_all)}

    overview = {
        'total_pnl':round(tp,2),'total_cost':round(tc,2),'roi':round(roi,1),
        'n_settlements':len(active_cities),'n_days':n_days,
        'kind_b':kind_overall['B'],'kind_t':kind_overall['T'],
        'sharpe':round(sharpe,2) if sharpe else None,'sharpe_lo':sharpe_lo,'sharpe_hi':sharpe_hi,'sortino':round(sortino,2) if sortino else None,'omega':round(omega,2) if omega else None,
        'max_dd':round(mdd,2),
        'daily_wr':round(up_days/n_days*100,1) if n_days else 0,
        'weekly_wr':round(wk_win/len(weekly_vals)*100,1) if weekly_vals else 0,
        'monthly_wr':round(mo_win/len(monthly_vals)*100,1) if monthly_vals else 0,
        'avg_win':round(avg_win,2),'avg_loss':round(avg_loss,2),
        'n_weeks':len(weekly_vals),'n_months':len(monthly_vals),
        'avg_daily_cost':round(sum(daily_costs)/n_days,2) if n_days else 0,
        'avg_daily_pnl':round(sum(daily_pnls)/n_days,2) if n_days else 0,
        'outlay_p25':round(p25,2),'outlay_p50':round(p50,2),'outlay_p75':round(p75,2),
        'recent_7':wstats(d7),'recent_30':wstats(d30),'prior_7':wstats(d7p),'prior_30':wstats(d30p),
    }

    # Multi-granularity time series (D/W/M)
    labels = [k for k,_ in daily_sorted]
    city_day_pnl = {c: defaultdict(float) for c in cities}
    city_day_cost = {c: defaultdict(float) for c in cities}
    city_event_cost = {c: defaultdict(float) for c in cities}
    for s in active_cities:
        if s['day_str'] and s['city'] in cities:
            city_day_pnl[s['city']][s['day_str']] += s['pnl']
            city_day_cost[s['city']][s['day_str']] += s['cost']
        # Event-date-keyed cost (fallback to settlement day_str when ticker has no date)
        es = s.get('event_str') or s['day_str']
        if es and s['city'] in cities:
            city_event_cost[s['city']][es] += s['cost']

    def period_key(day_str, gran):
        d = datetime.fromisoformat(day_str).date()
        if gran=='D': return day_str
        if gran=='W': return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')
        if gran=='M': return d.strftime('%Y-%m')
        return day_str

    def make_period_data(gran):
        window = {'D':30,'W':4,'M':3}[gran]
        agg = {}
        for ds_key, v in daily_sorted:
            k = period_key(ds_key, gran)
            if k not in agg: agg[k] = {'pnl':0,'cost':0}
            agg[k]['pnl'] += v['pnl']; agg[k]['cost'] += v['cost']
        plabels = sorted(agg.keys())
        pnls = [round(agg[k]['pnl'],2) for k in plabels]
        costs = [round(agg[k]['cost'],2) for k in plabels]
        rois = [round(agg[k]['pnl']/agg[k]['cost']*100,1) if agg[k]['cost']>0 else 0 for k in plabels]
        cum_total = []; running=0
        for p in pnls: running+=p; cum_total.append(round(running,2))
        roi_roll = []
        for i in range(len(plabels)):
            sp=sum(pnls[max(0,i-window+1):i+1]); sc=sum(costs[max(0,i-window+1):i+1])
            roi_roll.append(round(sp/sc*100,1) if sc>0 else 0)
        cost_roll = []
        for i in range(len(plabels)):
            sl=costs[max(0,i-window+1):i+1]
            cost_roll.append(round(sum(sl)/len(sl),2) if sl else 0)
        city_pnl_p={c:{} for c in cities}; city_cost_p={c:{} for c in cities}
        for c in cities:
            for ds2,pv in city_day_pnl[c].items():
                k=period_key(ds2,gran); city_pnl_p[c][k]=city_pnl_p[c].get(k,0)+pv
            for ds2,cv in city_day_cost[c].items():
                k=period_key(ds2,gran); city_cost_p[c][k]=city_cost_p[c].get(k,0)+cv
        c_daily={}; c_cum={}; c_roi={}
        for c in cities:
            dl=[]; cl=[]; rl=[]; run2=0
            for i,k in enumerate(plabels):
                p2=city_pnl_p[c].get(k,0); dl.append(round(p2,2)); run2+=p2; cl.append(round(run2,0))
                sp2=sum(city_pnl_p[c].get(plabels[j],0) for j in range(max(0,i-window+1),i+1))
                sc2=sum(city_cost_p[c].get(plabels[j],0) for j in range(max(0,i-window+1),i+1))
                min_cost={'D':500,'W':1000,'M':2000}[gran]
                rl.append(round(sp2/sc2*100,1) if sc2>=min_cost else None)
            c_daily[c]=dl; c_cum[c]=cl; c_roi[c]=rl
        c_cost_list={c:[round(city_cost_p[c].get(k,0),2) for k in plabels] for c in cities}
        # Event-date-based cost: separate label axis so cost bars line up with the event date
        # encoded in each ticker (e.g. KXHIGHTHOU-26APR29-B88.5 → 2026-04-29) rather than the
        # settlement timestamp. Other charts continue using settlement-date `plabels`.
        city_event_cost_p={c:{} for c in cities}
        event_keys_set=set()
        for c in cities:
            for ds2,cv in city_event_cost[c].items():
                k=period_key(ds2,gran); city_event_cost_p[c][k]=city_event_cost_p[c].get(k,0)+cv
                event_keys_set.add(k)
        event_labels=sorted(event_keys_set)
        c_event_cost_list={c:[round(city_event_cost_p[c].get(k,0),2) for k in event_labels] for c in cities}
        return {'labels':plabels,'pnl':pnls,'cost':costs,'roi':rois,'cum_total':cum_total,
                'roi_roll':roi_roll,'cost_roll':cost_roll,'city_pnl':c_daily,'city_cum':c_cum,'city_roi':c_roi,'city_cost':c_cost_list,
                'cost_labels':event_labels,'city_cost_event':c_event_cost_list}

    periods = {g: make_period_data(g) for g in ['D','W','M']}

    # Rolling Sharpe
    sharpe_labels=[]; sharpe_data=[]
    for i in range(19,len(daily_pnls)):
        sl=daily_pnls[i-29:i+1] if i>=29 else daily_pnls[:i+1]
        if len(sl)<20: continue
        m=sum(sl)/len(sl); v=sum((x-m)**2 for x in sl)/(len(sl)-1) if len(sl)>1 else 0
        s=math.sqrt(v) if v>0 else 0; sh2=(m/s*ann) if s>0 else 0
        sharpe_labels.append(labels[i]); sharpe_data.append(round(sh2,2))

    # Distributions
    def histogram(vals, bin_size, mn=None, mx=None):
        if not vals: return [],[]
        if mn is None: mn=int(min(vals)//bin_size*bin_size)
        if mx is None: mx=int(max(vals)//bin_size*bin_size)+bin_size
        bins=list(range(mn,mx+bin_size,bin_size)); counts=[0]*(len(bins)-1)
        for v in vals:
            idx=min(int((v-mn)//bin_size),len(counts)-1)
            if 0<=idx<len(counts): counts[idx]+=1
        return [f"${b}" for b in bins[:-1]], counts
    outlay_l,outlay_d=histogram(daily_costs,100,0,2200)
    up_l,up_d=histogram([p for p in daily_pnls if p>0],50,0,1500)
    dn_l,dn_d=histogram([abs(p) for p in daily_pnls if p<0],50,0,1100)

    # Correlation
    city_pivot={c:{} for c in cities}
    for s in active_cities:
        if s['day_str'] and s['city'] in cities:
            city_pivot[s['city']][s['day_str']]=city_pivot[s['city']].get(s['day_str'],0)+s['pnl']
    all_days_set=set()
    for c in cities: all_days_set.update(city_pivot[c].keys())
    all_days_list=sorted(all_days_set)
    def pearson(x,y):
        pairs=[(x.get(d,0),y.get(d,0)) for d in all_days_list if d in x and d in y]
        if len(pairs)<10: return 0
        mx2=sum(a for a,_ in pairs)/len(pairs); my2=sum(b for _,b in pairs)/len(pairs)
        num=sum((a-mx2)*(b-my2) for a,b in pairs)
        dx=math.sqrt(sum((a-mx2)**2 for a,_ in pairs)); dy=math.sqrt(sum((b-my2)**2 for _,b in pairs))
        return round(num/(dx*dy),3) if dx>0 and dy>0 else 0
    corr_matrix={c1:{c2:pearson(city_pivot[c1],city_pivot[c2]) if c1!=c2 else 1.0 for c2 in cities} for c1 in cities}
    cdp=defaultdict(lambda:defaultdict(float))
    for s in active_cities:
        if s['day_str']: cdp[s['day_str']][s['city']]+=s['pnl']
    n_al=n_aw=n_ml=n_mw=n_sw=n_sl=n_es=n_dd=0
    for d2,cp in cdp.items():
        if len(cp)<2: continue
        n_dd+=1; lo=sum(1 for v in cp.values() if v<0); wi=sum(1 for v in cp.values() if v>0)
        if lo==len(cp): n_al+=1
        if wi==len(cp): n_aw+=1
        in_ml = lo>len(cp)/2
        in_mw = wi>len(cp)/2
        if in_ml: n_ml+=1
        if in_mw: n_mw+=1
        if wi>len(cp)*0.75: n_sw+=1
        if lo>len(cp)*0.75: n_sl+=1
        if not in_ml and not in_mw: n_es+=1
    n_pairs = len(cities)*(len(cities)-1)//2
    corr_stats={'avg_r':round(sum(corr_matrix[c1][c2] for c1 in cities for c2 in cities if c1<c2)/max(n_pairs,1),3),
        'all_lose_pct':round(n_al/n_dd*100,1) if n_dd else 0,
        'all_win_pct':round(n_aw/n_dd*100,1) if n_dd else 0,
        'majority_lose_pct':round(n_ml/n_dd*100,1) if n_dd else 0,
        'majority_win_pct':round(n_mw/n_dd*100,1) if n_dd else 0,
        'supermajority_win_pct':round(n_sw/n_dd*100,1) if n_dd else 0,
        'supermajority_lose_pct':round(n_sl/n_dd*100,1) if n_dd else 0,
        'even_split_pct':round(n_es/n_dd*100,1) if n_dd else 0}

    # Regional correlation — group cities into U.S. regions, aggregate daily P&L per region, correlate
    CITY_REGIONS = {
        'NY':'Northeast','PHIL':'Northeast','TDC':'Northeast','TBOS':'Northeast',
        'MIA':'Southeast','TATL':'Southeast','TNOLA':'Southeast','TCHAR':'Southeast','TNASH':'Southeast','TTAMPA':'Southeast',
        'CHI':'Midwest','TMIN':'Midwest',
        'AUS':'South Central','HOU':'South Central','THOU':'South Central','TDAL':'South Central','TSATX':'South Central','TOKC':'South Central',
        'DEN':'Mountain','TDEN':'Mountain','TPHX':'Mountain','TLV':'Mountain',
        'LAX':'West Coast','TSEA':'West Coast','TSFO':'West Coast'}
    region_of={c:CITY_REGIONS.get(c,'Other') for c in cities}
    regions=sorted({region_of[c] for c in cities})
    region_daily={r:{} for r in regions}
    for c in cities:
        r=region_of[c]
        for d,v in city_pivot[c].items():
            region_daily[r][d]=region_daily[r].get(d,0)+v
    region_members={r:[c for c in cities if region_of[c]==r] for r in regions}
    region_corr={r1:{r2:pearson(region_daily[r1],region_daily[r2]) if r1!=r2 else 1.0 for r2 in regions} for r1 in regions}
    n_rpairs=len(regions)*(len(regions)-1)//2
    region_corr_stats={'avg_r':round(sum(region_corr[r1][r2] for r1 in regions for r2 in regions if r1<r2)/max(n_rpairs,1),3),
        'n_regions':len(regions)}

    # Day of week
    dow_names=['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
    dow_agg={i:{'pnl':0,'cost':0,'n':0} for i in range(7)}
    for ds_key,v in daily_sorted:
        dow=datetime.fromisoformat(ds_key).weekday(); dow_agg[dow]['pnl']+=v['pnl']; dow_agg[dow]['cost']+=v['cost']; dow_agg[dow]['n']+=1
    dow_data=[]
    for i in range(7):
        a=dow_agg[i]; wr=sum(1 for ds_key,v in daily_sorted if datetime.fromisoformat(ds_key).weekday()==i and v['pnl']>0)
        dow_data.append({'day':dow_names[i],'n':a['n'],'pnl':round(a['pnl'],0),
            'roi':round(a['pnl']/a['cost']*100,1) if a['cost']>0 else 0,
            'wr':round(wr/a['n']*100,1) if a['n']>0 else 0,'avg':round(a['pnl']/a['n'],2) if a['n']>0 else 0})

    # Per-city price levels
    no_pos=[s for s in active_cities if s['no_c']>0]
    price_level_data={}
    for city in cities:
        cb=[s for s in no_pos if s['city']==city]
        if not cb: continue
        bkts=defaultdict(lambda:{'n':0,'pnl':0,'cost':0,'contracts':0})
        for s in cb:
            pc=round(s['no_avg']*100); buc=(pc//5)*5; b=bkts[buc]
            b['n']+=1; b['pnl']+=s['pnl']; b['cost']+=s['cost']; b['contracts']+=s['no_c']
        cbl=[]
        for buc in sorted(bkts.keys()):
            b=bkts[buc]
            if b['n']<3: continue
            ppc=b['pnl']/b['contracts'] if b['contracts']>0 else 0
            roi_b=b['pnl']/b['cost']*100 if b['cost']>0 else 0
            cbl.append({'bucket':buc,'label':f"{buc}-{buc+4}",'n':b['n'],'pnl':round(b['pnl'],0),
                'contracts':b['contracts'],'ppc':round(ppc,3),'roi':round(roi_b,1)})
        price_level_data[city]=cbl

    # City summary
    city_summary={}; cutoff_90=datetime.now(timezone.utc)-timedelta(days=90)
    for city in cities:
        cs=[s for s in active_cities if s['city']==city]
        if not cs: continue
        tpc=sum(s['pnl'] for s in cs); tcc=sum(s['cost'] for s in cs)
        tm=[s for s in cs if s['is_T']]; bm=[s for s in cs if s['is_B']]
        rec=[s for s in cs if s['date'] and s['date']>=cutoff_90]; rp=sum(s['pnl'] for s in rec); rc=sum(s['cost'] for s in rec)
        city_summary[city]={'n':len(cs),'pnl':round(tpc,2),'cost':round(tcc,0),'roi':round(tpc/tcc*100,1) if tcc>0 else 0,
            't_n':len(tm),'t_pnl':round(sum(s['pnl'] for s in tm),2),'b_n':len(bm),'b_pnl':round(sum(s['pnl'] for s in bm),2),
            'r90_n':len(rec),'r90_pnl':round(rp,2),'r90_roi':round(rp/rc*100,1) if rc>0 else 0}

    # Per-city win rates at D/W/M granularity (based on net daily/weekly/monthly P&L per city)
    city_wr={}
    for city in cities:
        cdays = city_pivot[city]  # {day_str: pnl}
        if not cdays: continue
        d_n = len(cdays); d_w = sum(1 for v in cdays.values() if v > 0)
        wk_pnl=defaultdict(float); mo_pnl=defaultdict(float)
        for ds_key, v in cdays.items():
            d = datetime.fromisoformat(ds_key)
            wk_pnl[d.isocalendar()[:2]] += v
            mo_pnl[ds_key[:7]] += v
        w_n=len(wk_pnl); w_w=sum(1 for v in wk_pnl.values() if v>0)
        m_n=len(mo_pnl); m_w=sum(1 for v in mo_pnl.values() if v>0)
        # Gross $ won vs $ lost — settlement-level (most granular) plus per-granularity totals
        css=[s for s in active_cities if s['city']==city]
        gw_set=sum(s['pnl'] for s in css if s['pnl']>0)
        gl_set=-sum(s['pnl'] for s in css if s['pnl']<0)
        gw_d=sum(v for v in cdays.values() if v>0); gl_d=-sum(v for v in cdays.values() if v<0)
        gw_w=sum(v for v in wk_pnl.values() if v>0); gl_w=-sum(v for v in wk_pnl.values() if v<0)
        gw_m=sum(v for v in mo_pnl.values() if v>0); gl_m=-sum(v for v in mo_pnl.values() if v<0)
        # Per-market-kind (B vs T) breakdown at settlement level
        def _kind_stats(rows):
            n=len(rows)
            if n==0: return {'n':0,'gross_win':0,'gross_loss':0,'net':0,'cost':0,
                             'roi':0,'wr':0,'pf':None,'wins':0,'losses':0}
            gw=sum(s['pnl'] for s in rows if s['pnl']>0)
            gl=-sum(s['pnl'] for s in rows if s['pnl']<0)
            cost=sum(s['cost'] for s in rows)
            wins=sum(1 for s in rows if s['pnl']>0)
            return {'n':n,'gross_win':round(gw,2),'gross_loss':round(gl,2),
                    'net':round(gw-gl,2),'cost':round(cost,2),
                    'roi':round((gw-gl)/cost*100,1) if cost>0 else 0,
                    'wr':round(wins/n*100,1),
                    'pf':round(gw/gl,2) if gl>0 else None,
                    'wins':wins,'losses':n-wins}
        b_rows=[s for s in css if s['is_B']]
        t_rows=[s for s in css if s['is_T']]
        city_wr[city]={
            'd_n':d_n,'d_w':d_w,'d_l':d_n-d_w,'d_wr':round(d_w/d_n*100,1) if d_n else 0,
            'w_n':w_n,'w_w':w_w,'w_l':w_n-w_w,'w_wr':round(w_w/w_n*100,1) if w_n else 0,
            'm_n':m_n,'m_w':m_w,'m_l':m_n-m_w,'m_wr':round(m_w/m_n*100,1) if m_n else 0,
            'gross_win':round(gw_set,2),'gross_loss':round(gl_set,2),
            'net':round(gw_set-gl_set,2),'pf':round(gw_set/gl_set,2) if gl_set>0 else None,
            'd_gw':round(gw_d,2),'d_gl':round(gl_d,2),
            'w_gw':round(gw_w,2),'w_gl':round(gl_w,2),
            'm_gw':round(gw_m,2),'m_gl':round(gl_m,2),
            'kind_b':_kind_stats(b_rows),'kind_t':_kind_stats(t_rows)}

    # Bet accuracy — distance of actual settled high temp vs market strike (signed degrees F)
    with_dist=[s for s in active_cities if s.get('distance') is not None]
    bet_accuracy={}
    if with_dist:
        all_dist=[s['distance'] for s in with_dist]
        def dist_stats(arr):
            if not arr: return None
            arr_sorted=sorted(arr); n=len(arr_sorted)
            mean_signed=sum(arr)/n; mean_abs=sum(abs(x) for x in arr)/n
            med=arr_sorted[n//2] if n%2 else (arr_sorted[n//2-1]+arr_sorted[n//2])/2
            within_1=sum(1 for x in arr if abs(x)<=1)/n*100
            within_2=sum(1 for x in arr if abs(x)<=2)/n*100
            within_5=sum(1 for x in arr if abs(x)<=5)/n*100
            return {'n':n,'mean_signed':round(mean_signed,2),'mean_abs':round(mean_abs,2),
                    'median':round(med,2),'within_1':round(within_1,1),
                    'within_2':round(within_2,1),'within_5':round(within_5,1)}
        # Overall + split by win/loss + per-city
        bet_accuracy['overall']=dist_stats(all_dist)
        bet_accuracy['wins']=dist_stats([s['distance'] for s in with_dist if s['pnl']>0])
        bet_accuracy['losses']=dist_stats([s['distance'] for s in with_dist if s['pnl']<0])
        bet_accuracy['by_city']={}
        for city in cities:
            cd=[s['distance'] for s in with_dist if s['city']==city]
            cw=[s['distance'] for s in with_dist if s['city']==city and s['pnl']>0]
            cl=[s['distance'] for s in with_dist if s['city']==city and s['pnl']<0]
            if cd:
                bet_accuracy['by_city'][city]={
                    'all':dist_stats(cd),'wins':dist_stats(cw),'losses':dist_stats(cl)}
        # Histogram of signed distance (bin = 1°F)
        def hist_signed(vals, bin_size=1, mn=-15, mx=15):
            bins=list(range(mn, mx+bin_size, bin_size))
            counts=[0]*(len(bins)-1)
            for v in vals:
                idx=int((v-mn)//bin_size)
                if 0<=idx<len(counts): counts[idx]+=1
                elif v<mn: counts[0]+=1
                elif v>=mx: counts[-1]+=1
            labels=[f"{b:+d}" for b in bins[:-1]]
            return {'labels':labels,'data':counts}
        bet_accuracy['hist_overall']=hist_signed(all_dist)
        bet_accuracy['hist_wins']=hist_signed([s['distance'] for s in with_dist if s['pnl']>0])
        bet_accuracy['hist_losses']=hist_signed([s['distance'] for s in with_dist if s['pnl']<0])
        bet_accuracy['hist_by_city']={
            c: hist_signed([s['distance'] for s in with_dist if s['city']==c])
            for c in cities if any(s['city']==c for s in with_dist)}

    # Recent settlements
    rec_sett=sorted(active_cities,key=lambda s:s['day_str'] or '',reverse=True)[:50]
    recent_out=[]
    for s in rec_sett:
        side='No' if s['no_c']>0 and s['yes_c']==0 else ('Yes' if s['yes_c']>0 and s['no_c']==0 else 'Both')
        recent_out.append({'date':s['day_str'] or '','ticker':s.get('ticker',''),'city':s['city'],
            'threshold':s['threshold'],'side':side,'cost':round(s['cost'],2),'pnl':round(s['pnl'],2)})

    # ── Trade-based analyses (if trade CSV provided) ──
    trade_data = {'fill_timing':[],'fill_time_roi':[],'size_buckets':[],'fill_price':[],'fill_session':[]}
    if trade_file:
        trade_rows = load_csv(trade_file)
        trades = []
        for row in trade_rows:
            if row.get('type')!='Trade': continue
            if row.get('Direction') != 'No': continue
            tk = row.get('Market_Ticker','')
            if not tk.startswith('KXHIGH'): continue
            dt = parse_date(row.get('Original_Date'))
            if not dt: continue
            price = int(row.get('Price_In_Cents') or 0)
            amount = float(row.get('Amount_In_Dollars') or 0)
            # Inferred contracts — Filled column is empty in trade rows; reconstruct
            # from amount and NO price (amount = contracts * price/100, with rounding).
            contracts = round(amount * 100 / price) if price > 0 else 0
            trades.append({'ticker':tk, 'date':dt, 'price':price, 'amount':amount,
                'contracts':contracts, 'hour':dt.hour,
                'order_type':row.get('Order_Type',''),
                'city':tk.split('-')[0].replace('KXHIGH','')})

        sett_by_ticker = {s['ticker']:s for s in active_cities}

        # Fill timing — fills per hour (ET)
        hour_counts = defaultdict(lambda:{'n':0,'amount':0})
        for t in trades:
            et_h = (t['hour'] - 4) % 24
            hour_counts[et_h]['n'] += 1; hour_counts[et_h]['amount'] += t['amount']
        for h in range(24):
            d = hour_counts.get(h, {'n':0,'amount':0})
            if d['n'] == 0: continue
            trade_data['fill_timing'].append({'hour':h,'label':f"{h:02d}:00",
                'n':d['n'],'amount':round(d['amount'],0)})

        # ── Pro-rate each settlement's true P&L across its fills ──
        # Each fill gets a share of settlement.pnl proportional to its contract weight.
        # Sum across all fills equals total settled P&L (for trade-matched markets).
        # Bucketed by ET fill hour and by night/day session.
        fills_by_ticker = defaultdict(list)
        for t in trades:
            fills_by_ticker[t['ticker']].append(t)

        # Night session: ET hour ∈ [18,23] ∪ [0,5] (evening + overnight runs).
        # Day session:   ET hour ∈ [6,17] (morning/midday runs).
        def is_night(et_h): return et_h >= 18 or et_h < 6

        hour_agg = defaultdict(lambda:{'n':0,'pnl':0.0,'cost':0.0,'wins':0,'edge':0,'amount':0.0})
        fp_agg   = defaultdict(lambda:{'n':0,'pnl':0.0,'cost':0.0,'wins':0,'edge':0})
        sess_agg = {'night':{'n':0,'pnl':0.0,'cost':0.0,'wins':0,'amount':0.0},
                    'day':  {'n':0,'pnl':0.0,'cost':0.0,'wins':0,'amount':0.0}}
        # Per-city fill-time aggregation (mirrors hour_agg structure)
        hour_agg_by_city = defaultdict(lambda: defaultdict(lambda:{'n':0,'pnl':0.0,'cost':0.0,'wins':0,'edge':0,'amount':0.0}))
        fill_timing_by_city = defaultdict(lambda: defaultdict(lambda:{'n':0,'amount':0.0}))

        for tk, fills in fills_by_ticker.items():
            s = sett_by_ticker.get(tk)
            if not s: continue
            total_w = sum(f['contracts'] for f in fills)
            if total_w <= 0: continue
            won = s['result'] == 'no'
            city = s['city']
            for f in fills:
                share = f['contracts'] / total_w
                pnl_share = s['pnl'] * share
                cost_share = s['cost'] * share
                et_h = (f['hour'] - 4) % 24
                edge = (100 - f['price']) if won else -f['price']

                ha = hour_agg[et_h]
                ha['n'] += 1; ha['pnl'] += pnl_share; ha['cost'] += cost_share
                ha['edge'] += edge * f['contracts']; ha['amount'] += f['amount']
                if won: ha['wins'] += 1

                hac = hour_agg_by_city[city][et_h]
                hac['n'] += 1; hac['pnl'] += pnl_share; hac['cost'] += cost_share
                hac['edge'] += edge * f['contracts']; hac['amount'] += f['amount']
                if won: hac['wins'] += 1
                ftc = fill_timing_by_city[city][et_h]
                ftc['n'] += 1; ftc['amount'] += f['amount']

                buc = (f['price']//5)*5
                fb = fp_agg[buc]
                fb['n'] += 1; fb['pnl'] += pnl_share; fb['cost'] += cost_share
                fb['edge'] += edge * f['contracts']
                if won: fb['wins'] += 1

                ss = sess_agg['night' if is_night(et_h) else 'day']
                ss['n'] += 1; ss['pnl'] += pnl_share; ss['cost'] += cost_share
                ss['amount'] += f['amount']
                if won: ss['wins'] += 1

        # Total contracts per hour and per price bucket — for avg_edge denominator
        hr_contracts = defaultdict(int); pr_contracts = defaultdict(int)
        hr_contracts_by_city = defaultdict(lambda: defaultdict(int))
        for f in trades:
            et_h = (f['hour']-4)%24
            hr_contracts[et_h] += f['contracts']
            pr_contracts[(f['price']//5)*5] += f['contracts']
            hr_contracts_by_city[f['city']][et_h] += f['contracts']

        for h in range(24):
            ha = hour_agg.get(h)
            if not ha or ha['n'] == 0: continue
            roi = ha['pnl']/ha['cost']*100 if ha['cost'] > 0 else 0
            avg_edge = ha['edge']/hr_contracts[h] if hr_contracts[h] else 0
            trade_data['fill_time_roi'].append({'hour':h,'label':f"{h:02d}:00",
                'n':ha['n'],'wr':round(ha['wins']/ha['n']*100,1),
                'avg_edge':round(avg_edge,2),
                'roi':round(roi,1),
                'profit':round(ha['pnl'],2),
                'cost':round(ha['cost'],2)})

        # Per-city fill_time_roi and fill_timing
        trade_data['fill_time_roi_by_city'] = {}
        trade_data['fill_timing_by_city'] = {}
        for city in cities:
            ftr_out = []
            ft_out = []
            cd = hour_agg_by_city.get(city, {})
            cf = fill_timing_by_city.get(city, {})
            for h in range(24):
                ha = cd.get(h)
                if ha and ha['n'] > 0:
                    roi = ha['pnl']/ha['cost']*100 if ha['cost'] > 0 else 0
                    denom = hr_contracts_by_city[city].get(h, 0)
                    avg_edge = ha['edge']/denom if denom else 0
                    ftr_out.append({'hour':h,'label':f"{h:02d}:00",
                        'n':ha['n'],'wr':round(ha['wins']/ha['n']*100,1),
                        'avg_edge':round(avg_edge,2),'roi':round(roi,1),
                        'profit':round(ha['pnl'],2),'cost':round(ha['cost'],2)})
                d = cf.get(h)
                if d and d['n'] > 0:
                    ft_out.append({'hour':h,'label':f"{h:02d}:00",
                        'n':d['n'],'amount':round(d['amount'],0)})
            if ftr_out:
                trade_data['fill_time_roi_by_city'][city] = ftr_out
            if ft_out:
                trade_data['fill_timing_by_city'][city] = ft_out

        for buc in sorted(fp_agg.keys()):
            b = fp_agg[buc]
            if b['n'] < 5: continue
            roi = b['pnl']/b['cost']*100 if b['cost'] > 0 else 0
            avg_edge = b['edge']/pr_contracts[buc] if pr_contracts[buc] else 0
            trade_data['fill_price'].append({'price':f"{buc}-{buc+4}",
                'n':b['n'],'wr':round(b['wins']/b['n']*100,1),
                'avg_edge':round(avg_edge,2),
                'profit':round(b['pnl'],2),
                'cost':round(b['cost'],2),
                'roi':round(roi,1)})

        for label in ('night','day'):
            ss = sess_agg[label]
            if ss['n'] == 0: continue
            roi = ss['pnl']/ss['cost']*100 if ss['cost'] > 0 else 0
            trade_data['fill_session'].append({'label':label.capitalize(),
                'n':ss['n'],'wr':round(ss['wins']/ss['n']*100,1),
                'profit':round(ss['pnl'],2),
                'cost':round(ss['cost'],2),
                'roi':round(roi,1),
                'amount':round(ss['amount'],0)})

    # Win rate / ROI by position size (from settlements, no trade CSV needed)
    sz_buckets = defaultdict(lambda:{'n':0,'pnl':0,'cost':0,'wins':0,'contracts':0,'avg_price':0})
    sz_by_city = defaultdict(lambda: defaultdict(lambda:{'n':0,'pnl':0,'cost':0,'wins':0,'contracts':0,'avg_price':0}))
    for s in [x for x in active_cities if x['no_c']>0]:
        buc = (s['no_c']//100)*100
        b = sz_buckets[buc]
        b['n']+=1; b['pnl']+=s['pnl']; b['cost']+=s['cost']; b['contracts']+=s['no_c']
        b['avg_price']+=s['no_avg']*100
        if s['pnl']>0: b['wins']+=1
        bc = sz_by_city[s['city']][buc]
        bc['n']+=1; bc['pnl']+=s['pnl']; bc['cost']+=s['cost']; bc['contracts']+=s['no_c']
        bc['avg_price']+=s['no_avg']*100
        if s['pnl']>0: bc['wins']+=1
    for buc in sorted(sz_buckets.keys()):
        b = sz_buckets[buc]
        if b['n'] < 1: continue
        trade_data['size_buckets'].append({'size':f"{buc}-{buc+99}",'n':b['n'],
            'pnl':round(b['pnl'],0),'roi':round(b['pnl']/b['cost']*100,1) if b['cost']>0 else 0,
            'wr':round(b['wins']/b['n']*100,1),'avg_price':round(b['avg_price']/b['n'],1),
            'avg_pnl':round(b['pnl']/b['n'],2)})
    trade_data['size_buckets_by_city'] = {}
    for city in cities:
        out = []
        for buc in sorted(sz_by_city[city].keys()):
            b = sz_by_city[city][buc]
            if b['n'] < 1: continue
            out.append({'size':f"{buc}-{buc+99}",'n':b['n'],
                'pnl':round(b['pnl'],0),'roi':round(b['pnl']/b['cost']*100,1) if b['cost']>0 else 0,
                'wr':round(b['wins']/b['n']*100,1),'avg_price':round(b['avg_price']/b['n'],1),
                'avg_pnl':round(b['pnl']/b['n'],2)})
        if out:
            trade_data['size_buckets_by_city'][city] = out

    # Platform-wide volume cache (from fetch_kxhigh_volume.py)
    raw_vol = load_volume_cache(volume_cache_path)
    if raw_vol:
        def _vol_period_key(date_str, gran):
            d2 = datetime.fromisoformat(date_str).date()
            if gran == 'D': return date_str
            if gran == 'W': return (d2 - timedelta(days=d2.weekday())).strftime('%Y-%m-%d')
            if gran == 'M': return d2.strftime('%Y-%m')
            return date_str
        def _agg_vol(gran):
            agg = defaultdict(lambda: defaultdict(float))
            for city, dates in raw_vol.items():
                if city not in cities: continue
                for ds, vol in dates.items():
                    agg[city][_vol_period_key(ds, gran)] += vol
            lbls = sorted({k for ca in agg.values() for k in ca})
            return {'labels': lbls, 'by_city': {c: [round(agg[c].get(k, 0)) for k in lbls] for c in cities}}
        platform_volume = {
            'D': _agg_vol('D'), 'W': _agg_vol('W'), 'M': _agg_vol('M'),
            'totals': {c: round(sum(raw_vol.get(c, {}).values())) for c in cities},
            'grand_total': round(sum(v for cd in raw_vol.values() for v in cd.values())),
        }
    else:
        platform_volume = {}

    return {'overview':overview,'periods':periods,'cities':cities,'sharpe_ts':{'labels':sharpe_labels,'data':sharpe_data},
        'dist':{'outlay':{'labels':outlay_l,'data':outlay_d},'up':{'labels':up_l,'data':up_d},'down':{'labels':dn_l,'data':dn_d}},
        'corr':{'matrix':corr_matrix,'stats':corr_stats},
        'region_corr':{'matrix':region_corr,'stats':region_corr_stats,'regions':regions,'members':region_members},'dow':dow_data,
        'price_levels':price_level_data,'city_summary':city_summary,'recent':recent_out,
        'trade':trade_data,'platform_volume':platform_volume,
        'city_wr':city_wr,'bet_accuracy':bet_accuracy}


def generate_html(data, out_path):
    o = data['overview']
    periods = data['periods']
    cities = data['cities']
    daily_labels = periods['D']['labels']
    daily_city_cum = periods['D']['city_cum']

    # Dynamic color palette — enough for 20+ cities
    PALETTE = ['#378ADD','#1D9E75','#7F77DD','#D85A30','#D4537E','#BA7517','#639922',
               '#E24B4A','#5DCAA5','#AFA9EC','#F09F7B','#ED93B1','#FAC775','#97C459',
               '#85B7EB','#B4B2A9','#F5C4B3','#F4C0D1','#C0DD97','#9FE1CB']
    CC = {c: PALETTE[i % len(PALETTE)] for i, c in enumerate(cities)}

    # Auto-generate display names from city codes
    KNOWN_NAMES = {'NY':'New York','AUS':'Austin','PHIL':'Philadelphia','MIA':'Miami',
                   'CHI':'Chicago','LAX':'Los Angeles','DEN':'Denver','HOU':'Houston',
                   'THOU':'Houston','TDAL':'Dallas','TNOLA':'New Orleans','TPHX':'Phoenix',
                   'TLV':'Las Vegas','TSATX':'San Antonio','TATL':'Atlanta','TSEA':'Seattle',
                   'TDC':'Washington DC','TOKC':'Oklahoma City','TMIN':'Minneapolis','TSFO':'San Francisco',
                   'TDEN':'Denver','TBOS':'Boston','TCHAR':'Charlotte','TNASH':'Nashville','TTAMPA':'Tampa'}
    CN = {c: KNOWN_NAMES.get(c, c) for c in cities}

    sh = f'{o["sharpe"]:.2f}' if o['sharpe'] else '—'
    so = f'{o["sortino"]:.2f}' if o['sortino'] else '—'
    om = f'{o["omega"]:.2f}' if o['omega'] else '—'

    def money(v):
        c='#1D9E75' if v>0 else ('#E24B4A' if v<0 else '#94a3b8')
        return f'<span style="color:{c}">${v:+,.0f}</span>'

    # Correlation table
    cm=data['corr']['matrix']
    corr_rows='<tr><th></th>'+''.join(f'<th>{c}</th>' for c in cities)+'</tr>\n'
    for c1 in cities:
        corr_rows+=f'<tr><th>{c1}</th>'
        for c2 in cities:
            r=cm.get(c1,{}).get(c2,0)
            if c1==c2: corr_rows+='<td style="background:rgba(55,138,221,0.15);color:#378ADD">1.00</td>'
            else:
                bg='rgba(29,158,117,0.2)' if r>0.05 else ('rgba(226,75,74,0.2)' if r<-0.05 else 'rgba(148,163,184,0.08)')
                tc2='#1D9E75' if r>0.05 else ('#E24B4A' if r<-0.05 else '#94a3b8')
                corr_rows+=f'<td style="background:{bg};color:{tc2}">{r:+.3f}</td>'
        corr_rows+='</tr>\n'

    # Regional correlation table
    rc=data['region_corr']; rcm=rc['matrix']; regions=rc['regions']; rmem=rc['members']
    rcorr_rows='<tr><th></th>'+''.join(f'<th>{r}</th>' for r in regions)+'</tr>\n'
    for r1 in regions:
        rcorr_rows+=f'<tr><th>{r1}</th>'
        for r2 in regions:
            rv=rcm.get(r1,{}).get(r2,0)
            if r1==r2: rcorr_rows+='<td style="background:rgba(55,138,221,0.15);color:#378ADD">1.00</td>'
            else:
                bg='rgba(29,158,117,0.2)' if rv>0.05 else ('rgba(226,75,74,0.2)' if rv<-0.05 else 'rgba(148,163,184,0.08)')
                tc='#1D9E75' if rv>0.05 else ('#E24B4A' if rv<-0.05 else '#94a3b8')
                rcorr_rows+=f'<td style="background:{bg};color:{tc}">{rv:+.3f}</td>'
        rcorr_rows+='</tr>\n'
    region_members_rows=''.join(f'<tr><td><strong>{r}</strong></td><td>{", ".join(CN.get(c,c) for c in rmem[r])}</td></tr>\n' for r in regions)

    # City summary table
    cs_rows=''
    for c in cities:
        s=data['city_summary'].get(c)
        if not s: continue
        pc='#1D9E75' if s['pnl']>0 else '#E24B4A'
        r90c='#1D9E75' if s['r90_pnl']>0 else '#E24B4A'
        cs_rows+=f'<tr><td>{CN[c]}</td><td>{s["n"]}</td><td style="color:{pc}">${s["pnl"]:+,.0f}</td><td>{s["roi"]:+.1f}%</td>'
        cs_rows+=f'<td>{s["b_n"]}</td><td>${s["b_pnl"]:+,.0f}</td><td>{s["t_n"]}</td><td>${s["t_pnl"]:+,.0f}</td>'
        cs_rows+=f'<td style="color:{r90c}">${s["r90_pnl"]:+,.0f} ({s["r90_roi"]:+.1f}%)</td></tr>\n'

    # B vs T market-kind breakdown per city
    def _kind_cell(k):
        if not k or k['n']==0: return '<td class="muted">—</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td>'
        nc='#1D9E75' if k['net']>0 else ('#E24B4A' if k['net']<0 else '#94a3b8')
        rc='#1D9E75' if k['roi']>0 else ('#E24B4A' if k['roi']<0 else '#94a3b8')
        wrc='#1D9E75' if k['wr']>=55 else ('#E24B4A' if k['wr']<=45 else '#94a3b8')
        return (f'<td>{k["n"]} ({k["wins"]}-{k["losses"]})</td>'
                f'<td style="color:{wrc}">{k["wr"]:.1f}%</td>'
                f'<td class="green">${k["gross_win"]:,.0f}</td>'
                f'<td class="red">${k["gross_loss"]:,.0f}</td>'
                f'<td style="color:{nc}">${k["net"]:+,.0f}</td>'
                f'<td style="color:{rc}">{k["roi"]:+.1f}%</td>')

    kind_rows=''
    kind_sorted=sorted([(c,data['city_wr'][c]) for c in cities if c in data.get('city_wr',{})],
                       key=lambda x:-(x[1]['kind_b']['net']+x[1]['kind_t']['net']))
    for c,w in kind_sorted:
        kind_rows+=f'<tr><td>{CN[c]}</td>{_kind_cell(w["kind_b"])}{_kind_cell(w["kind_t"])}</tr>\n'

    # City win-rate table (D/W/M, sorted by daily WR desc)
    def _wr_color(v):
        return '#1D9E75' if v>=55 else ('#E24B4A' if v<=45 else '#94a3b8')
    cwr_data=data.get('city_wr',{})
    cwr_sorted=sorted([(c,cwr_data[c]) for c in cities if c in cwr_data],
                      key=lambda x:-x[1]['d_wr'])
    cwr_rows=''
    for c,w in cwr_sorted:
        netc='#1D9E75' if w['net']>0 else ('#E24B4A' if w['net']<0 else '#94a3b8')
        pf_str=f'{w["pf"]:.2f}' if w['pf'] is not None else '∞'
        pf_color='#1D9E75' if w['pf'] and w['pf']>=1 else '#E24B4A'
        cwr_rows+=(f'<tr><td>{CN[c]}</td>'
                   f'<td style="color:{_wr_color(w["d_wr"])}">{w["d_wr"]:.1f}%</td><td>{w["d_w"]}-{w["d_l"]}</td><td>{w["d_n"]}</td>'
                   f'<td style="color:{_wr_color(w["w_wr"])}">{w["w_wr"]:.1f}%</td><td>{w["w_w"]}-{w["w_l"]}</td><td>{w["w_n"]}</td>'
                   f'<td style="color:{_wr_color(w["m_wr"])}">{w["m_wr"]:.1f}%</td><td>{w["m_w"]}-{w["m_l"]}</td><td>{w["m_n"]}</td>'
                   f'<td class="green">${w["gross_win"]:,.0f}</td>'
                   f'<td class="red">${w["gross_loss"]:,.0f}</td>'
                   f'<td style="color:{netc}">${w["net"]:+,.0f}</td>'
                   f'<td style="color:{pf_color}">{pf_str}</td></tr>\n')

    # Bet-accuracy table (per-city) — distance of actual high vs market strike
    ba=data.get('bet_accuracy') or {}
    ba_rows=''
    if ba and ba.get('by_city'):
        # Sort by mean_abs ascending (most accurate first)
        ba_sorted=sorted([(c,ba['by_city'][c]) for c in cities if c in ba['by_city']],
                        key=lambda x:x[1]['all']['mean_abs'])
        for c,b in ba_sorted:
            a=b['all']; w=b.get('wins') or {'n':0,'mean_signed':0,'mean_abs':0}
            l=b.get('losses') or {'n':0,'mean_signed':0,'mean_abs':0}
            sc='#1D9E75' if a['mean_signed']>0 else '#E24B4A'
            ba_rows+=(f'<tr><td>{CN[c]}</td><td>{a["n"]}</td>'
                      f'<td style="color:{sc}">{a["mean_signed"]:+.2f}</td><td>{a["mean_abs"]:.2f}</td>'
                      f'<td>{a["within_1"]:.0f}%</td><td>{a["within_2"]:.0f}%</td><td>{a["within_5"]:.0f}%</td>'
                      f'<td class="green">{w["n"]} · {w["mean_signed"]:+.2f}°F</td>'
                      f'<td class="red">{l["n"]} · {l["mean_signed"]:+.2f}°F</td></tr>\n')

    # Recent table
    rec_rows=''
    for r in data['recent']:
        pc='#1D9E75' if r['pnl']>0 else '#E24B4A'
        rec_rows+=f'<tr><td>{r["date"]}</td><td style="font-size:10px">{r["ticker"]}</td><td>{r["city"]}</td><td>{r["threshold"]}</td><td>{r["side"]}</td><td>${r["cost"]:.2f}</td><td style="color:{pc}">${r["pnl"]:+.2f}</td></tr>\n'

    # Per-city price level HTML
    pl_html=''
    for city in cities:
        bkts=data['price_levels'].get(city,[])
        if not bkts: continue
        cs2=data['city_summary'].get(city,{})
        total=cs2.get('pnl',0)
        tag_cls='pos' if total>100 else ('neg' if total<-100 else 'neu')
        pl_html+=f'<h3>{CN[city]} <span class="tag {tag_cls}">${total:+,.0f}</span></h3>\n'
        pl_html+=f'<div class="g"><div><div class="cc"><canvas id="ppc_{city}"></canvas></div></div>'
        pl_html+=f'<div><div class="cc"><canvas id="pnl_{city}"></canvas></div></div></div>\n'
        pl_html+='<table><thead><tr><th>NO price</th><th>n</th><th>Contracts</th><th>P&L</th><th>$/contract</th><th>ROI</th></tr></thead><tbody>\n'
        for b in bkts:
            pc='#1D9E75' if b['ppc']>0 else '#E24B4A'
            pl_html+=f'<tr><td>{b["label"]}c</td><td>{b["n"]}</td><td>{b["contracts"]:,}</td>'
            pl_html+=f'<td style="color:{pc}">${b["pnl"]:+,.0f}</td><td style="color:{pc}">${b["ppc"]:+.3f}</td>'
            pl_html+=f'<td style="color:{pc}">{b["roi"]:+.1f}%</td></tr>\n'
        pl_html+='</tbody></table>\n'

    # Recent stats for summary
    r7=o['recent_7']; r30=o['recent_30']; p7=o['prior_7']; p30=o['prior_30']
    d7_vs='accelerating' if r7['pnl']>p7['pnl'] and p7['pnl']>0 else ('cooling' if r7['pnl']<p7['pnl'] else 'similar')
    d30_dir='improving' if r30['roi']>o['roi']+2 else ('weakening' if r30['roi']<o['roi']-2 else 'stable vs all-time')
    cs_list=list(data['city_summary'].items())
    cs_rs=sorted(cs_list,key=lambda x:-x[1].get('r90_pnl',0))
    top_c=cs_rs[0] if cs_rs else None; bot_c=cs_rs[-1] if cs_rs else None
    dow_s=sorted(data['dow'],key=lambda d:-d['pnl'])
    best_d=dow_s[0] if dow_s else None; worst_d=dow_s[-1] if dow_s else None

    # 15-min meta refresh so a browser tab left open on the file picks up the
    # daily 7 AM rebuild (run_dashboards.ps1) without a manual F5.
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="900">
<title>Weather Trading Dashboard</title>
<style>
:root{{--bg:#0f172a;--bg2:#1e293b;--tx:#e2e8f0;--tx2:#94a3b8;--tx3:#64748b;--bd:#334155}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--bg);color:var(--tx);font-family:-apple-system,system-ui,sans-serif;padding:1.5rem;line-height:1.5}}
h1{{font-size:1.5rem;margin-bottom:.2rem}}
h2{{font-size:1.1rem;color:var(--tx2);margin:2.5rem 0 .5rem;border-bottom:1px solid var(--bd);padding-bottom:.3rem}}
h2:first-of-type{{margin-top:1rem}}
h3{{font-size:.9rem;color:var(--tx3);margin:1.2rem 0 .4rem}}
.sub{{color:var(--tx3);font-size:.8rem;margin-bottom:1rem}}
.cards{{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin:.75rem 0}}
.card{{background:var(--bg2);border-radius:8px;padding:.7rem .9rem;text-align:center}}
.card .l{{font-size:10px;color:var(--tx3);text-transform:uppercase;letter-spacing:.04em}}
.card .v{{font-size:1.2rem;font-weight:600;margin-top:2px}}
.card .s{{font-size:10px;color:var(--tx3);margin-top:1px}}
.cc{{position:relative;width:100%;height:260px;margin-bottom:.75rem}}
.cc.tall{{height:340px}}
.g{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.g3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}}
@media(max-width:700px){{.g,.g3{{grid-template-columns:1fr}}}}
.legend{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:4px;font-size:11px;color:var(--tx2)}}
.legend span{{display:flex;align-items:center;gap:3px}}
.dot{{width:8px;height:8px;border-radius:2px;display:inline-block}}
.line{{width:12px;height:2px;display:inline-block}}
table{{width:100%;border-collapse:collapse;font-size:11px;margin:.5rem 0}}
th{{text-align:left;padding:4px 6px;color:var(--tx3);border-bottom:1px solid var(--bd);font-size:10px;text-transform:uppercase}}
td{{padding:3px 6px;border-bottom:1px solid rgba(51,65,85,0.3);font-family:monospace;font-size:11px}}
table.corr td,table.corr th{{text-align:center;padding:6px 10px}}
.tag{{display:inline-block;font-size:10px;padding:2px 6px;border-radius:4px;margin-left:4px}}
.tag.pos{{background:rgba(29,158,117,0.15);color:#1D9E75}}
.tag.neg{{background:rgba(226,75,74,0.15);color:#E24B4A}}
.tag.neu{{background:rgba(148,163,184,0.1);color:#94a3b8}}
.green{{color:#1D9E75}}.red{{color:#E24B4A}}.blue{{color:#378ADD}}.muted{{color:#94a3b8}}
.nav{{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:1rem;position:sticky;top:0;z-index:10;background:var(--bg);padding:.5rem 0;border-bottom:1px solid var(--bd)}}
.nav a{{background:var(--bg2);border:1px solid var(--bd);color:var(--tx2);padding:.3rem .6rem;border-radius:.3rem;text-decoration:none;font-size:.75rem;cursor:pointer}}
.nav a:hover{{background:rgba(51,65,85,.5);color:var(--tx)}}
.gran-toggle{{display:flex;gap:.3rem;align-items:center;margin:.5rem 0 1rem;padding:.4rem;background:var(--bg2);border-radius:.4rem;width:fit-content}}
.gran-btn{{background:transparent;border:none;color:var(--tx2);padding:.3rem .75rem;border-radius:.3rem;cursor:pointer;font-size:.8rem;font-family:inherit}}
.gran-btn:hover{{color:var(--tx);background:rgba(51,65,85,.5)}}
.gran-btn.active{{background:#378ADD;color:white}}
.gran-note{{margin-left:.75rem;font-size:.75rem;color:var(--tx3)}}
.legend-item{{cursor:pointer;user-select:none;transition:opacity 0.15s}}
.legend-item:hover{{opacity:0.7}}
</style>
''')
        f.write('<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>\n')
        f.write('</head><body>\n')
        f.write(f'<h1>Weather Trading Dashboard</h1>\n')
        f.write(f'<p class="sub">Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} &bull; {o["n_settlements"]:,} settlements &bull; {o["n_days"]} trading days</p>\n')

        f.write('<div class="nav">')
        nav_items=[('summary','Summary'),('overview','Overview'),('timeseries','Time series'),('distributions','Distributions'),
                    ('correlation','Correlation'),('region-correlation','Regional corr'),('sharpe','Rolling Sharpe'),('dow','Day of week'),
                    ('possize','Position size'),('filltime','Fill timing'),('fillprice','Fill price'),
                    ('pricelevels','Price levels'),('citywr','City WR'),('citysummary','City summary')]
        if data.get('bet_accuracy'): nav_items.append(('betaccuracy','Bet accuracy'))
        nav_items.append(('recent','Recent'))
        for sid,lbl in nav_items:
            f.write(f'<a onclick="document.getElementById(\'{sid}\').scrollIntoView({{behavior:\'smooth\'}})">{lbl}</a>')
        f.write('</div>\n')

        # Summary
        f.write(f'''<div id="summary"><h2>Executive summary</h2>
<div style="background:var(--bg2);border-radius:10px;padding:1.25rem;margin-bottom:1rem;line-height:1.7">
<p><strong>All-time:</strong> Deployed ${o['total_cost']:,.0f} over {o['n_days']} trading days since {daily_labels[0] if daily_labels else "—"}, generating {money(o['total_pnl'])} at {o['roi']:+.1f}% ROI. Sharpe {sh}, max drawdown ${o['max_dd']:,.0f}. Profitable on {o['daily_wr']:.0f}% of days, {o['weekly_wr']:.0f}% of weeks, {o['monthly_wr']:.0f}% of months.</p>
<p><strong>Last 7 days ({r7['n']} trading days):</strong> P&L {money(r7['pnl'])} on ${r7['cost']:,.0f} outlay ({r7['roi']:+.1f}% ROI), win rate {r7['wr']:.0f}%. Vs prior 7d ({money(p7['pnl'])}): {d7_vs}.</p>
<p><strong>Last 30 days ({r30['n']} trading days):</strong> P&L {money(r30['pnl'])} on ${r30['cost']:,.0f} outlay ({r30['roi']:+.1f}% ROI), win rate {r30['wr']:.0f}%. Vs prior 30d ({money(p30['pnl'])} at {p30['roi']:+.1f}%): {d30_dir}.</p>
<p><strong>City leaderboard (90d):</strong> {CN.get(top_c[0],top_c[0]) if top_c else "—"} leads at {money(top_c[1]['r90_pnl']) if top_c else "—"} ({top_c[1]['r90_roi']:+.1f}% ROI), {CN.get(bot_c[0],bot_c[0]) if bot_c else "—"} trails at {money(bot_c[1]['r90_pnl']) if bot_c else "—"} ({bot_c[1]['r90_roi']:+.1f}% ROI).</p>
<p><strong>Market kind:</strong> Between (B) {money(o['kind_b']['pnl'])} on {o['kind_b']['n']} bets at {o['kind_b']['roi']:+.1f}% ROI (WR {o['kind_b']['wr']:.0f}%) &middot; Tail (T) {money(o['kind_t']['pnl'])} on {o['kind_t']['n']} bets at {o['kind_t']['roi']:+.1f}% ROI (WR {o['kind_t']['wr']:.0f}%).</p>
<p><strong>Timing:</strong> {best_d['day'] if best_d else "—"} is strongest ({money(best_d['pnl']) if best_d else "—"}, {best_d['roi']:+.1f}% ROI), {worst_d['day'] if worst_d else "—"} is weakest ({money(worst_d['pnl']) if worst_d else "—"}, {worst_d['roi']:+.1f}% ROI).</p>
</div></div>
''')

        # Overview cards
        f.write(f'''<div id="overview"><h2>Overview</h2>
<div class="cards">
<div class="card"><div class="l">Net P&L</div><div class="v {'green' if o['total_pnl']>0 else 'red'}">${o['total_pnl']:+,.0f}</div><div class="s">ROI: {o['roi']:+.1f}%</div></div>
<div class="card"><div class="l">Sharpe</div><div class="v">{sh}</div><div class="s">95% CI [{o['sharpe_lo']:.2f}, {o['sharpe_hi']:.2f}] · Sortino: {so} · &Omega;: {om}</div></div>
<div class="card"><div class="l">Max DD</div><div class="v red">${o['max_dd']:,.0f}</div></div>
<div class="card"><div class="l">Avg outlay/day</div><div class="v">${o['avg_daily_cost']:,.0f}</div><div class="s">p25 ${o['outlay_p25']:,.0f} &bull; p50 ${o['outlay_p50']:,.0f} &bull; p75 ${o['outlay_p75']:,.0f}</div></div>
<div class="card"><div class="l">Avg profit/day</div><div class="v {'green' if o['avg_daily_pnl']>0 else 'red'}">${o['avg_daily_pnl']:+,.2f}</div><div class="s"><span class="green">win ${o['avg_win']:+,.0f}</span> &bull; <span class="red">loss ${o['avg_loss']:+,.0f}</span></div></div>
<div class="card"><div class="l">Daily WR</div><div class="v">{o['daily_wr']:.1f}%</div><div class="s">{o['n_days']} days</div></div>
<div class="card"><div class="l">Weekly WR</div><div class="v">{o['weekly_wr']:.1f}%</div><div class="s">{o['n_weeks']} weeks</div></div>
<div class="card"><div class="l">Monthly WR</div><div class="v">{o['monthly_wr']:.1f}%</div><div class="s">{o['n_months']} months</div></div>
<div class="card"><div class="l">Between (B) P&L</div><div class="v {'green' if o['kind_b']['pnl']>0 else 'red'}">${o['kind_b']['pnl']:+,.0f}</div><div class="s">{o['kind_b']['n']} bets &bull; ROI {o['kind_b']['roi']:+.1f}% &bull; WR {o['kind_b']['wr']:.0f}%</div></div>
<div class="card"><div class="l">Tail (T) P&L</div><div class="v {'green' if o['kind_t']['pnl']>0 else 'red'}">${o['kind_t']['pnl']:+,.0f}</div><div class="s">{o['kind_t']['n']} bets &bull; ROI {o['kind_t']['roi']:+.1f}% &bull; WR {o['kind_t']['wr']:.0f}%</div></div>
</div></div>\n''')

        # Time Series
        f.write('''<div id="timeseries"><h2>Time series</h2>
<div class="gran-toggle">
  <button class="gran-btn active" data-gran="D">Daily</button>
  <button class="gran-btn" data-gran="W">Weekly</button>
  <button class="gran-btn" data-gran="M">Monthly</button>
  <span class="gran-note"><span id="rollNote">30-day rolling</span></span>
</div>
<h3>Cumulative P&L</h3>
<div class="cc tall"><canvas id="totalCumChart"></canvas></div>
<h3>ROI with rolling average</h3>
<div class="legend"><span><span class="dot" style="background:rgba(148,163,184,0.4)"></span>Period ROI</span>
<span><span class="line" style="background:#378ADD"></span>Rolling ROI</span></div>
<div class="cc"><canvas id="roiChart"></canvas></div>
<h3>Rolling ROI by city</h3><div class="legend">
''')
        for i,c in enumerate(cities):
            f.write(f'<span class="legend-item" data-chart="cityRoi" data-idx="{i}"><span class="dot" style="background:{CC[c]}"></span>{CN[c]}</span>\n')
        f.write('</div>\n<div class="cc tall"><canvas id="cityRoiChart"></canvas></div>\n')
        f.write('<h3>Net profit</h3>\n<div class="cc"><canvas id="pnlChart"></canvas></div>\n')
        f.write('<h3>Cumulative P&L by city</h3>\n<div class="legend">\n')
        for i,c in enumerate(cities):
            final=daily_city_cum.get(c,[0])[-1] if daily_city_cum.get(c) else 0
            f.write(f'<span class="legend-item" data-chart="cum" data-idx="{i}"><span class="dot" style="background:{CC[c]}"></span>{CN[c]} ${final:+,.0f}</span>\n')
        f.write('</div>\n<div class="cc tall"><canvas id="cumChart"></canvas></div>\n')
        f.write('<h3>P&L stacked by city</h3>\n<div class="legend">\n')
        for i,c in enumerate(cities):
            f.write(f'<span class="legend-item" data-chart="stack" data-idx="{i}"><span class="dot" style="background:{CC[c]}"></span>{CN[c]}</span>\n')
        f.write('</div>\n<div class="cc tall"><canvas id="stackChart"></canvas></div>\n')
        f.write('<h3>Capital outlay by city (event date) <span class="sub muted">— cost basis of positions, dated by the event date encoded in the ticker, stacked by city</span></h3>\n<div class="legend">\n')
        for i, c in enumerate(cities):
            f.write(f'<span class="legend-item" data-chart="cost" data-idx="{i}"><span class="dot" style="background:{CC[c]}"></span>{CN[c]}</span>\n')
        f.write('</div>\n<div class="cc"><canvas id="costChart"></canvas></div>\n')
        pv = data.get('platform_volume', {})
        if pv:
            gt = pv['grand_total']
            n_days = len(pv.get('D', {}).get('labels', []))
            f.write(f'<h3>Platform-wide volume by city <span class="sub">— {gt:,.0f} total contracts across {n_days} days (all Kalshi participants)</span></h3>\n<div class="legend">\n')
            for i, c in enumerate(cities):
                ctot = pv['totals'].get(c, 0)
                if ctot > 0:
                    f.write(f'<span class="legend-item" data-chart="platVol" data-idx="{i}"><span class="dot" style="background:{CC[c]}"></span>{CN[c]} {ctot:,.0f}</span>\n')
            f.write('</div>\n<div class="cc tall"><canvas id="platVolChart"></canvas></div>\n')
        f.write('</div>\n')

        # Distributions
        f.write('''<div id="distributions"><h2>Distributions</h2>
<div class="g3"><div><h3>Daily outlay</h3><div class="cc"><canvas id="distOutlay"></canvas></div></div>
<div><h3>Up-day profit</h3><div class="cc"><canvas id="distUp"></canvas></div></div>
<div><h3>Down-day loss (abs)</h3><div class="cc"><canvas id="distDown"></canvas></div></div></div></div>\n''')

        # Correlation
        cst=data['corr']['stats']
        f.write(f'''<div id="correlation"><h2>Cross-city correlation</h2>
<div class="cards">
<div class="card"><div class="l">Avg pairwise r</div><div class="v muted">{cst['avg_r']:+.3f}</div></div>
<div class="card"><div class="l">All cities lose</div><div class="v green">{cst['all_lose_pct']:.1f}%</div><div class="s">of trading days</div></div>
<div class="card"><div class="l">All cities win</div><div class="v blue">{cst['all_win_pct']:.1f}%</div><div class="s">of trading days</div></div>
<div class="card"><div class="l">&gt;50% lose</div><div class="v muted">{cst['majority_lose_pct']:.1f}%</div></div>
<div class="card"><div class="l">&gt;50% win</div><div class="v muted">{cst['majority_win_pct']:.1f}%</div></div>
<div class="card"><div class="l">&gt;75% lose</div><div class="v muted">{cst['supermajority_lose_pct']:.1f}%</div></div>
<div class="card"><div class="l">&gt;75% win</div><div class="v muted">{cst['supermajority_win_pct']:.1f}%</div></div>
<div class="card"><div class="l">Even split</div><div class="v muted">{cst['even_split_pct']:.1f}%</div><div class="s">tied win/lose count</div></div>
</div><div style="overflow-x:auto"><table class="corr">{corr_rows}</table></div></div>\n''')

        # Regional correlation
        rcs=data['region_corr']['stats']
        f.write(f'''<div id="region-correlation"><h2>Cross-region correlation</h2>
<p class="sub">Daily P&amp;L within each region is summed across its cities, then correlated across regions. Shows whether whole climatic zones move together.</p>
<div class="cards">
<div class="card"><div class="l">Regions</div><div class="v muted">{rcs['n_regions']}</div></div>
<div class="card"><div class="l">Avg pairwise r</div><div class="v muted">{rcs['avg_r']:+.3f}</div></div>
</div>
<div style="overflow-x:auto"><table class="corr">{rcorr_rows}</table></div>
<h3 style="margin-top:1rem">Region membership</h3>
<table><thead><tr><th>Region</th><th>Cities</th></tr></thead><tbody>{region_members_rows}</tbody></table>
</div>\n''')

        # Rolling Sharpe
        f.write('<div id="sharpe"><h2>Rolling 30-day Sharpe</h2>\n<div class="cc"><canvas id="sharpeChart"></canvas></div></div>\n')

        # Day of week
        f.write('<div id="dow"><h2>Day of week</h2>\n<div class="cc"><canvas id="dowChart"></canvas></div></div>\n')

        # ── Trade-based sections ──
        td = data['trade']
        has_trades = bool(td['fill_timing'])

        # Position size (always available — from settlements)
        possize_city_opts = ''.join(
            f'<option value="{c}">{CN[c]}</option>'
            for c in cities if c in td.get('size_buckets_by_city', {})
        )
        f.write(f'''<div id="possize"><h2>Position size analysis</h2>
<p class="sub">Win rate, ROI, and total profit by number of NO contracts held at settlement. Lower win rate at larger sizes is expected — wins are bigger to compensate.</p>
<div style="margin:0.5rem 0"><label style="color:#94a3b8;font-size:0.85rem;margin-right:0.5rem">City:</label>
<select id="possizeCity" style="background:var(--bg2);color:#e8eef7;border:1px solid #334155;border-radius:6px;padding:0.3rem 0.6rem;font-size:0.9rem">
<option value="__ALL__">All cities</option>{possize_city_opts}</select></div>
<div class="g3"><div><h3>ROI by position size</h3><div class="cc"><canvas id="sizeROI"></canvas></div></div>
<div><h3>Win rate by position size</h3><div class="cc"><canvas id="sizeWR"></canvas></div></div>
<div><h3>Profit by position size ($)</h3><div class="cc"><canvas id="sizePnL"></canvas></div></div></div></div>\n''')

        if has_trades:
            # Fill timing
            ft_city_opts = ''.join(
                f'<option value="{c}">{CN[c]}</option>'
                for c in cities if c in td.get('fill_time_roi_by_city', {})
            )
            f.write(f'''<div id="filltime"><h2>Fill time analysis (ET)</h2>
<p class="sub">When do your NO orders get filled? Profit by hour pro-rates each settlement's realized P&L across its constituent fills, so summing all hours equals total settled P&L.</p>
<div style="margin:0.5rem 0"><label style="color:#94a3b8;font-size:0.85rem;margin-right:0.5rem">City:</label>
<select id="filltimeCity" style="background:var(--bg2);color:#e8eef7;border:1px solid #334155;border-radius:6px;padding:0.3rem 0.6rem;font-size:0.9rem">
<option value="__ALL__">All cities</option>{ft_city_opts}</select></div>
<div class="g3"><div><h3>Fills per hour</h3><div class="cc tall"><canvas id="hourFills"></canvas></div></div>
<div><h3>Edge per contract by hour (cents)</h3><div class="cc tall"><canvas id="hourEdge"></canvas></div></div>
<div><h3>Profit by fill hour ($)</h3><div class="cc tall"><canvas id="hourProfit"></canvas></div></div></div>
<div class="g"><div><h3>Profit by session ($)</h3><div class="cc"><canvas id="sessionPnL"></canvas></div></div>
<div><h3>ROI by session</h3><div class="cc"><canvas id="sessionROI"></canvas></div></div></div>
<p class="sub">Night session = ET 18:00-05:59 (evening + overnight runs). Day session = ET 06:00-17:59 (morning/midday runs).</p></div>\n''')

            # Fill price vs outcome
            f.write('''<div id="fillprice"><h2>Fill price vs settlement outcome</h2>
<p class="sub">At each NO price level, edge per contract = realized cents/contract. Profit = total realized $ pro-rated across this bucket's fills. The critical cutoff is where edge turns negative.</p>
<div class="g3"><div><h3>Edge per contract (cents) by NO fill price</h3><div class="cc"><canvas id="fpEdge"></canvas></div></div>
<div><h3>Win rate by NO fill price</h3><div class="cc"><canvas id="fpWR"></canvas></div></div>
<div><h3>Profit by NO fill price ($)</h3><div class="cc"><canvas id="fpProfit"></canvas></div></div></div></div>\n''')

        # Add nav items for new sections
        # (already handled below when we update the nav)

        # Price levels
        f.write(f'<div id="pricelevels"><h2>Edge per dollar by price level</h2>\n{pl_html}</div>\n')

        # City win-rate (D/W/M) + gross $ won vs lost
        f.write(f'''<div id="citywr"><h2>Win/loss rate by city (D/W/M)</h2>
<p class="sub">A "win" at each granularity is a positive net P&amp;L over that period for that city. Daily = settle-day net P&amp;L &gt; 0; Weekly = ISO-week sum &gt; 0; Monthly = calendar-month sum &gt; 0. $ won / $ lost are gross settlement-level totals (sum of positive P&amp;L vs absolute sum of negative P&amp;L across every settlement for that city). PF = profit factor = $ won / $ lost.</p>
<table><thead><tr><th rowspan="2">City</th><th colspan="3">Daily</th><th colspan="3">Weekly</th><th colspan="3">Monthly</th><th colspan="4">Gross P&amp;L</th></tr>
<tr><th>WR</th><th>W-L</th><th>n</th><th>WR</th><th>W-L</th><th>n</th><th>WR</th><th>W-L</th><th>n</th><th>$ won</th><th>$ lost</th><th>Net</th><th>PF</th></tr></thead>
<tbody>{cwr_rows}</tbody></table>
<h3 style="margin-top:1.5rem">Gross $ won vs $ lost by city <span class="sub muted">— settlement-level totals, sorted by net P&amp;L</span></h3>
<div class="legend">
<span><span class="dot" style="background:rgba(29,158,117,0.7)"></span>$ won</span>
<span><span class="dot" style="background:rgba(226,75,74,0.7)"></span>$ lost</span>
</div>
<div class="cc tall"><canvas id="cityGrossDollars"></canvas></div>
<h3>Daily WR by city</h3>
<div class="cc"><canvas id="cityWRdaily"></canvas></div>
<h3>Weekly WR by city</h3>
<div class="cc"><canvas id="cityWRweekly"></canvas></div>
<h3>Monthly WR by city</h3>
<div class="cc"><canvas id="cityWRmonthly"></canvas></div>

<h3 style="margin-top:1.5rem">Between (B) vs Tail (T) market performance by city</h3>
<p class="sub">"Between" markets bet on a 2&deg;F window (e.g. B89.5 = high in [88.5, 90.5]). "Tail" markets bet on the high being above a cutoff (e.g. T69 = high &ge; 69.5). Sorted by combined net P&amp;L desc.</p>
<table><thead><tr><th rowspan="2">City</th><th colspan="6">Between (B)</th><th colspan="6">Tail (T)</th></tr>
<tr><th>n (W-L)</th><th>WR</th><th>$ won</th><th>$ lost</th><th>Net</th><th>ROI</th>
<th>n (W-L)</th><th>WR</th><th>$ won</th><th>$ lost</th><th>Net</th><th>ROI</th></tr></thead>
<tbody>{kind_rows}</tbody></table>
<h3>Net P&amp;L by market kind (B vs T)</h3>
<div class="legend">
<span><span class="dot" style="background:#378ADD"></span>Between (B)</span>
<span><span class="dot" style="background:#BA7517"></span>Tail (T)</span>
</div>
<div class="cc tall"><canvas id="cityKindNet"></canvas></div>
</div>\n''')

        # City summary
        f.write(f'''<div id="citysummary"><h2>City summary</h2>
<table><thead><tr><th>City</th><th>n</th><th>P&L</th><th>ROI</th><th>B n</th><th>B P&L</th><th>T n</th><th>T P&L</th><th>Last 90d</th></tr></thead>
<tbody>{cs_rows}</tbody></table></div>\n''')

        # Bet accuracy (distance from actual settled high)
        if ba and ba.get('overall'):
            ov=ba['overall']; bw=ba.get('wins') or {}; bl=ba.get('losses') or {}
            sc_color='#1D9E75' if ov['mean_signed']>0 else '#E24B4A'
            f.write(f'''<div id="betaccuracy"><h2>Bet accuracy: distance vs actual settled high</h2>
<p class="sub">Distance = actual settled high &minus; reference point (°F, signed). Reference is the <strong>midpoint</strong> for "between" (B) markets and the <strong>low_range cutoff</strong> for "tail" (T) markets — so the meaning is uniform: |d|&lt;1 indicates YES for between markets, d&ge;0 indicates YES for tail markets. Positive = actual came in above the reference, negative = below. Source: BigQuery KXHIGH_resolved_markets.</p>
<div class="cards">
<div class="card"><div class="l">Settlements w/ distance</div><div class="v">{ov['n']:,}</div></div>
<div class="card"><div class="l">Mean signed</div><div class="v" style="color:{sc_color}">{ov['mean_signed']:+.2f}°F</div><div class="s">positive = hotter than strike</div></div>
<div class="card"><div class="l">Mean |distance|</div><div class="v">{ov['mean_abs']:.2f}°F</div></div>
<div class="card"><div class="l">Median signed</div><div class="v">{ov['median']:+.2f}°F</div></div>
<div class="card"><div class="l">|d|&le;1°F</div><div class="v">{ov['within_1']:.0f}%</div></div>
<div class="card"><div class="l">|d|&le;2°F</div><div class="v">{ov['within_2']:.0f}%</div></div>
<div class="card"><div class="l">|d|&le;5°F</div><div class="v">{ov['within_5']:.0f}%</div></div>
</div>
<div class="g">
<div><h3>Distance distribution (signed °F)</h3>
<div class="legend">
<span><span class="dot" style="background:rgba(148,163,184,0.6)"></span>All</span>
<span><span class="dot" style="background:#1D9E75"></span>Wins</span>
<span><span class="dot" style="background:#E24B4A"></span>Losses</span>
</div>
<div class="cc tall"><canvas id="distHist"></canvas></div></div>
<div><h3>Mean |distance| by city</h3>
<div class="cc tall"><canvas id="distByCity"></canvas></div></div>
</div>
<h3 style="margin-top:1.5rem">Per-city distance stats</h3>
<table><thead><tr><th>City</th><th>n</th><th>Mean signed</th><th>Mean |d|</th><th>|d|&le;1°F</th><th>|d|&le;2°F</th><th>|d|&le;5°F</th><th>Wins (n · μ°F)</th><th>Losses (n · μ°F)</th></tr></thead>
<tbody>{ba_rows}</tbody></table>
<p class="sub" style="margin-top:.5rem">Wins/losses split shows the average signed distance among winning vs losing settlements. A winning <em>between</em> bet on B89.5 means the actual high was inside [88.5, 90.5] so |d| &lt; 1; a winning <em>tail</em> bet on T87.0 means actual &gt;= 87.0.</p>
</div>\n''')
        else:
            f.write('<div id="betaccuracy"><h2>Bet accuracy</h2><p class="sub">No resolved-market data available — BQ fetch from KXHIGH_resolved_markets failed or returned empty. Ensure gcloud auth is set up and the table has data.</p></div>\n')

        # Recent
        f.write(f'''<div id="recent"><h2>Recent settlements (last 50)</h2>
<table><thead><tr><th>Date</th><th>Ticker</th><th>City</th><th>Threshold</th><th>Side</th><th>Cost</th><th>P&L</th></tr></thead>
<tbody>{rec_rows}</tbody></table></div>\n''')

        # Scripts
        f.write('<script>\n')
        f.write('var PERIODS='); json.dump(data['periods'], f); f.write(';\n')
        f.write('var SHARPE='); json.dump(data['sharpe_ts'], f); f.write(';\n')
        f.write('var DIST='); json.dump(data['dist'], f); f.write(';\n')
        f.write('var DOW='); json.dump(data['dow'], f); f.write(';\n')
        f.write('var PL='); json.dump(data['price_levels'], f); f.write(';\n')
        f.write('var CITIES='); json.dump(cities, f); f.write(';\n')
        f.write('var CN='); json.dump(CN, f); f.write(';\n')
        f.write('var CC='); json.dump(CC, f); f.write(';\n')
        f.write('var TD='); json.dump(data['trade'], f); f.write(';\n')
        f.write('var PV='); json.dump(data.get('platform_volume', {}), f); f.write(';\n')
        f.write('var CWR='); json.dump(data.get('city_wr', {}), f); f.write(';\n')
        f.write('var BA='); json.dump(data.get('bet_accuracy', {}), f); f.write(';\n')
        f.write(r'''
var G='rgba(255,255,255,0.06)',TK='#888';
var CHARTS={},currentGran='D';
var ROLL_NAME={D:'30-day',W:'4-week',M:'3-month'},ROLL_SHORT={D:'30d',W:'4w',M:'3m'};
var base={responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}}};
function mkScales(yFmt){return{x:{grid:{display:false},ticks:{color:TK,font:{size:10},maxTicksLimit:16,maxRotation:45}},y:{grid:{color:G},ticks:{color:TK,font:{size:10},callback:yFmt}}};}
function bgP(arr,a){return arr.map(function(v){return v>=0?'rgba(29,158,117,'+a+')':'rgba(226,75,74,'+a+')';});}
function bdP(arr){return arr.map(function(v){return v>=0?'#1D9E75':'#E24B4A';});}
var d=PERIODS.D;

CHARTS.totalCum=new Chart(document.getElementById('totalCumChart'),{type:'line',
  data:{labels:d.labels,datasets:[{data:d.cum_total,borderColor:'#1D9E75',backgroundColor:'rgba(29,158,117,0.1)',borderWidth:2.5,pointRadius:0,pointHoverRadius:4,tension:0.3,fill:true}]},
  options:{...base,interaction:{mode:'index',intersect:false},plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var v=Math.round(c.raw);return 'Cumulative: '+(v>=0?'+':'')+'$'+v.toLocaleString()}}}},scales:mkScales(function(v){return '$'+v.toLocaleString()})}});

CHARTS.roi=new Chart(document.getElementById('roiChart'),{type:'line',
  data:{labels:d.labels,datasets:[
    {label:'Period',data:d.roi,borderColor:'rgba(148,163,184,0.3)',borderWidth:1,pointRadius:0,pointHoverRadius:3,tension:0,fill:false},
    {label:'Rolling',data:d.roi_roll,borderColor:'#378ADD',borderWidth:2.5,pointRadius:0,pointHoverRadius:4,tension:0.3,fill:false}
  ]},options:{...base,interaction:{mode:'index',intersect:false},scales:mkScales(function(v){return v+'%'}),
    plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{label:function(c){return (c.datasetIndex===0?'Period':ROLL_SHORT[currentGran])+': '+c.raw+'%'}}}}}});

var crDs=CITIES.map(function(c){return {label:CN[c]||c,data:d.city_roi[c]||[],borderColor:CC[c],borderWidth:2,pointRadius:0,pointHoverRadius:3,tension:0.3,fill:false,spanGaps:false};});
CHARTS.cityRoi=new Chart(document.getElementById('cityRoiChart'),{type:'line',
  data:{labels:d.labels,datasets:crDs},
  options:{...base,interaction:{mode:'index',intersect:false},plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{label:function(c){if(c.raw===null)return null;return c.dataset.label+': '+c.raw+'%'}}}},scales:mkScales(function(v){return v+'%'})}});

CHARTS.pnl=new Chart(document.getElementById('pnlChart'),{type:'bar',
  data:{labels:d.labels,datasets:[{data:d.pnl,backgroundColor:bgP(d.pnl,'0.6'),borderColor:bdP(d.pnl),borderWidth:.5,barPercentage:1,categoryPercentage:1}]},
  options:{...base,interaction:{mode:'index',intersect:false},scales:mkScales(function(v){return '$'+v.toLocaleString()}),
    plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{label:function(c){var v=Math.round(c.raw);return (v>=0?'+':'')+'$'+v.toLocaleString()}}}}}});

var cumDs=CITIES.map(function(c){var a=d.city_cum[c]||[];var fl=a.length?a[a.length-1]:0;
  return {label:CN[c]+' $'+(fl>=0?'+':'')+Math.round(fl).toLocaleString(),data:a,borderColor:CC[c],borderWidth:2,pointRadius:0,pointHoverRadius:3,tension:0.3,fill:false};});
CHARTS.cum=new Chart(document.getElementById('cumChart'),{type:'line',
  data:{labels:d.labels,datasets:cumDs},
  options:{...base,interaction:{mode:'index',intersect:false},plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{label:function(c){return CN[CITIES[c.datasetIndex]]+': $'+Math.round(c.raw).toLocaleString()}}}},scales:mkScales(function(v){return '$'+v.toLocaleString()})}});

var stDs=CITIES.map(function(c){return {label:CN[c]||c,data:d.city_pnl[c]||[],backgroundColor:CC[c],borderWidth:0,barPercentage:1,categoryPercentage:1};});
CHARTS.stack=new Chart(document.getElementById('stackChart'),{type:'bar',
  data:{labels:d.labels,datasets:stDs},
  options:{...base,interaction:{mode:'index',intersect:false},plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{label:function(c){var v=Math.round(c.raw);if(v===0)return null;return c.dataset.label+': '+(v>=0?'+':'')+'$'+v.toLocaleString()}}}},
    scales:{x:{stacked:true,grid:{display:false},ticks:{color:TK,font:{size:10},maxTicksLimit:16,maxRotation:45}},y:{stacked:true,grid:{color:G},ticks:{color:TK,font:{size:10},callback:function(v){return '$'+v.toLocaleString()}}}}}});

var costDs=CITIES.map(function(c){return {label:CN[c]||c,data:(d.city_cost_event||d.city_cost)[c]||[],backgroundColor:CC[c],borderWidth:0,barPercentage:1,categoryPercentage:1};});
var costTotalsPlugin={id:'costTotals',afterDatasetsDraw:function(chart){
  var meta0=chart.getDatasetMeta(0);if(!meta0||!meta0.data||!meta0.data.length)return;
  var bw=meta0.data[0].width||0;if(bw<18)return;
  var ds=chart.data.datasets,n=meta0.data.length;
  var topMeta=chart.getDatasetMeta(ds.length-1);
  var ctx=chart.ctx;ctx.save();ctx.fillStyle='#ccc';ctx.font='10px sans-serif';ctx.textAlign='center';ctx.textBaseline='bottom';
  for(var i=0;i<n;i++){var t=0;for(var j=0;j<ds.length;j++){var v=ds[j].data[i];if(typeof v==='number')t+=v;}if(t===0)continue;
    ctx.fillText('$'+Math.round(t).toLocaleString(),meta0.data[i].x,topMeta.data[i].y-2);}
  ctx.restore();
}};
CHARTS.cost=new Chart(document.getElementById('costChart'),{type:'bar',
  data:{labels:d.cost_labels||d.labels,datasets:costDs},plugins:[costTotalsPlugin],
  options:{...base,interaction:{mode:'index',intersect:false},layout:{padding:{top:14}},
    plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{
      label:function(c){var v=Math.round(c.raw);if(v===0)return null;return c.dataset.label+': $'+v.toLocaleString();},
      footer:function(items){var t=items.reduce(function(s,i){return s+i.parsed.y;},0);return 'Total: $'+Math.round(t).toLocaleString();}
    }}},
    scales:{x:{stacked:true,grid:{display:false},ticks:{color:TK,font:{size:10},maxTicksLimit:16,maxRotation:45}},y:{stacked:true,grid:{color:G},ticks:{color:TK,font:{size:10},callback:function(v){return '$'+v.toLocaleString()}}}}}});


function setGran(g){
  currentGran=g;var d=PERIODS[g];
  document.getElementById('rollNote').textContent=ROLL_NAME[g]+' rolling';
  CHARTS.totalCum.data.labels=d.labels;CHARTS.totalCum.data.datasets[0].data=d.cum_total;CHARTS.totalCum.update();
  CHARTS.roi.data.labels=d.labels;CHARTS.roi.data.datasets[0].data=d.roi;CHARTS.roi.data.datasets[1].data=d.roi_roll;CHARTS.roi.update();
  CHARTS.cityRoi.data.labels=d.labels;CITIES.forEach(function(c,i){CHARTS.cityRoi.data.datasets[i].data=d.city_roi[c]||[];});CHARTS.cityRoi.update();
  CHARTS.pnl.data.labels=d.labels;CHARTS.pnl.data.datasets[0].data=d.pnl;CHARTS.pnl.data.datasets[0].backgroundColor=bgP(d.pnl,'0.6');CHARTS.pnl.data.datasets[0].borderColor=bdP(d.pnl);CHARTS.pnl.update();
  CHARTS.cum.data.labels=d.labels;CITIES.forEach(function(c,i){var a=d.city_cum[c]||[];var fl=a.length?a[a.length-1]:0;CHARTS.cum.data.datasets[i].data=a;CHARTS.cum.data.datasets[i].label=CN[c]+' $'+(fl>=0?'+':'')+Math.round(fl).toLocaleString();});CHARTS.cum.update();
  CHARTS.stack.data.labels=d.labels;CITIES.forEach(function(c,i){CHARTS.stack.data.datasets[i].data=d.city_pnl[c]||[];});CHARTS.stack.update();
  CHARTS.cost.data.labels=d.cost_labels||d.labels;CITIES.forEach(function(c,i){CHARTS.cost.data.datasets[i].data=(d.city_cost_event||d.city_cost)[c]||[];});CHARTS.cost.update();
  if(CHARTS.platVol&&PV[g]){CHARTS.platVol.data.labels=PV[g].labels;CITIES.forEach(function(c,i){CHARTS.platVol.data.datasets[i].data=PV[g].by_city[c]||[];});CHARTS.platVol.update();}
  document.querySelectorAll('.gran-btn').forEach(function(b){b.classList.toggle('active',b.dataset.gran===g);});
}
document.querySelectorAll('.gran-btn').forEach(function(b){b.addEventListener('click',function(){setGran(b.dataset.gran);});});

function fadeColor(c,a){if(!c||typeof c!=='string')return c;if(c[0]==='#'){var r=parseInt(c.slice(1,3),16),g=parseInt(c.slice(3,5),16),b=parseInt(c.slice(5,7),16);return 'rgba('+r+','+g+','+b+','+a+')';}if(c.indexOf('rgba')===0)return c.replace(/[\d.]+\)$/,a+')');return c;}
function highlightDataset(cn,ti){var ch=CHARTS[cn];if(!ch)return;ch.data.datasets.forEach(function(ds,i){if(!ds._origColors){ds._origColors={bg:ds.backgroundColor,bc:ds.borderColor,bw:ds.borderWidth};}var o=ds._origColors;if(ti===null){ds.backgroundColor=o.bg;ds.borderColor=o.bc;ds.borderWidth=o.bw;}else if(i===ti){ds.backgroundColor=o.bg;ds.borderColor=o.bc;ds.borderWidth=o.bw;}else{ds.backgroundColor=fadeColor(o.bg,0.12);ds.borderColor=fadeColor(o.bc,0.12);ds.borderWidth=o.bw;}});ch.update();}
document.querySelectorAll('.legend-item').forEach(function(el){el.addEventListener('mouseenter',function(){highlightDataset(el.dataset.chart,parseInt(el.dataset.idx));});el.addEventListener('mouseleave',function(){highlightDataset(el.dataset.chart,null);});});

function mkHist(id,d,color){new Chart(document.getElementById(id),{type:'bar',data:{labels:d.labels,datasets:[{data:d.data,backgroundColor:color+'99',borderColor:color,borderWidth:1,borderRadius:2,barPercentage:1,categoryPercentage:.92}]},options:{...base,scales:mkScales(function(v){return v})}});}
mkHist('distOutlay',DIST.outlay,'#378ADD');mkHist('distUp',DIST.up,'#1D9E75');mkHist('distDown',DIST.down,'#E24B4A');

new Chart(document.getElementById('sharpeChart'),{type:'line',data:{labels:SHARPE.labels,datasets:[{data:SHARPE.data,borderColor:'#378ADD',borderWidth:2,pointRadius:0,pointHoverRadius:3,tension:0.3,fill:{target:'origin',above:'rgba(29,158,117,0.08)',below:'rgba(226,75,74,0.08)'}},{data:SHARPE.labels.map(function(){return 0}),borderColor:'rgba(226,75,74,0.3)',borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false}]},options:{...base,interaction:{mode:'index',intersect:false},scales:mkScales(function(v){return v.toFixed(1)}),plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){return c.datasetIndex===0?'Sharpe: '+c.raw.toFixed(2):null}}}}}});

var dowL=DOW.map(function(d){return d.day}),dowP=DOW.map(function(d){return d.pnl});
new Chart(document.getElementById('dowChart'),{type:'bar',data:{labels:dowL,datasets:[{data:dowP,backgroundColor:bgP(dowP,'0.6'),borderRadius:4}]},options:{...base,scales:mkScales(function(v){return (v>=0?'':'-')+'$'+Math.abs(v).toLocaleString()}),plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=DOW[c.dataIndex];return ['$'+Math.round(d.pnl).toLocaleString(),'ROI: '+d.roi+'%','WR: '+d.wr+'%','n='+d.n]}}}}}});

function fmtDollars(v){return (v>=0?'':'-')+'$'+Math.abs(v).toLocaleString()}
function fmtSigned(v){return (v>=0?'+':'-')+'$'+Math.abs(v).toLocaleString()}

// Position size (with per-city dropdown)
function renderPosSize(sz){
  ['sizeROI','sizeWR','sizePnL'].forEach(function(id){if(CHARTS[id]){CHARTS[id].destroy();delete CHARTS[id];}});
  if(!sz||!sz.length){
    ['sizeROI','sizeWR','sizePnL'].forEach(function(id){
      var el=document.getElementById(id);if(!el)return;
      var ctx=el.getContext('2d');ctx.clearRect(0,0,el.width,el.height);
      ctx.fillStyle='#94a3b8';ctx.font='12px sans-serif';ctx.textAlign='center';
      ctx.fillText('No data for selected city',el.width/2,el.height/2);
    });
    return;
  }
  CHARTS.sizeROI=new Chart(document.getElementById('sizeROI'),{type:'bar',
    data:{labels:sz.map(function(d){return d.size}),datasets:[{data:sz.map(function(d){return d.roi}),
      backgroundColor:bgP(sz.map(function(d){return d.roi}),'0.6'),borderColor:bdP(sz.map(function(d){return d.roi})),borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v+'%'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=sz[c.dataIndex];return ['ROI: '+d.roi+'%','P&L: $'+d.pnl,'Avg price: '+d.avg_price+'c','n='+d.n]}}}}}});
  CHARTS.sizeWR=new Chart(document.getElementById('sizeWR'),{type:'bar',
    data:{labels:sz.map(function(d){return d.size}),datasets:[{data:sz.map(function(d){return d.wr}),
      backgroundColor:'#378ADD99',borderColor:'#378ADD',borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v+'%'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=sz[c.dataIndex];return ['WR: '+d.wr+'%','Avg P&L/settle: $'+d.avg_pnl,'n='+d.n]}}}}}});
  CHARTS.sizePnL=new Chart(document.getElementById('sizePnL'),{type:'bar',
    data:{labels:sz.map(function(d){return d.size}),datasets:[{data:sz.map(function(d){return d.pnl}),
      backgroundColor:bgP(sz.map(function(d){return d.pnl}),'0.6'),borderColor:bdP(sz.map(function(d){return d.pnl})),borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(fmtDollars),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=sz[c.dataIndex];return [fmtSigned(d.pnl),'ROI: '+d.roi+'%','WR: '+d.wr+'%','n='+d.n]}}}}}});
}
if(TD.size_buckets&&TD.size_buckets.length){
  renderPosSize(TD.size_buckets);
  var psSel=document.getElementById('possizeCity');
  if(psSel){psSel.addEventListener('change',function(){
    var v=psSel.value;
    var data=(v==='__ALL__')?TD.size_buckets:((TD.size_buckets_by_city||{})[v]||[]);
    renderPosSize(data);
  });}
}

// Fill timing (with per-city dropdown)
function renderFillTime(timing,ftr){
  ['hourFills','hourEdge','hourProfit'].forEach(function(id){if(CHARTS[id]){CHARTS[id].destroy();delete CHARTS[id];}});
  function noData(id){
    var el=document.getElementById(id);if(!el)return;
    var ctx=el.getContext('2d');ctx.clearRect(0,0,el.width,el.height);
    ctx.fillStyle='#94a3b8';ctx.font='12px sans-serif';ctx.textAlign='center';
    ctx.fillText('No data for selected city',el.width/2,el.height/2);
  }
  if(timing&&timing.length){
    CHARTS.hourFills=new Chart(document.getElementById('hourFills'),{type:'bar',
      data:{labels:timing.map(function(d){return d.label}),datasets:[{data:timing.map(function(d){return d.n}),
        backgroundColor:'#378ADD99',borderColor:'#378ADD',borderWidth:1,borderRadius:3}]},
      options:{...base,scales:mkScales(function(v){return v}),
        plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=timing[c.dataIndex];return [d.n+' fills','$'+d.amount+' deployed']}}}}}});
  } else { noData('hourFills'); }
  if(ftr&&ftr.length){
    CHARTS.hourEdge=new Chart(document.getElementById('hourEdge'),{type:'bar',
      data:{labels:ftr.map(function(d){return d.label}),datasets:[{data:ftr.map(function(d){return d.avg_edge}),
        backgroundColor:bgP(ftr.map(function(d){return d.avg_edge}),'0.6'),
        borderColor:bdP(ftr.map(function(d){return d.avg_edge})),borderWidth:1,borderRadius:3}]},
      options:{...base,scales:mkScales(function(v){return v.toFixed(1)+'c'}),
        plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=ftr[c.dataIndex];return [d.avg_edge.toFixed(1)+'c/contract','WR: '+d.wr+'%','ROI: '+d.roi+'%','n='+d.n]}}}}}});
    CHARTS.hourProfit=new Chart(document.getElementById('hourProfit'),{type:'bar',
      data:{labels:ftr.map(function(d){return d.label}),datasets:[{data:ftr.map(function(d){return d.profit}),
        backgroundColor:bgP(ftr.map(function(d){return d.profit}),'0.6'),
        borderColor:bdP(ftr.map(function(d){return d.profit})),borderWidth:1,borderRadius:3}]},
      options:{...base,scales:mkScales(fmtDollars),
        plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=ftr[c.dataIndex];return [fmtSigned(d.profit),'WR: '+d.wr+'%','ROI: '+d.roi+'%','Cost: $'+d.cost.toLocaleString(),'n='+d.n]}}}}}});
  } else { noData('hourEdge'); noData('hourProfit'); }
}
if(TD.fill_timing&&TD.fill_timing.length){
  renderFillTime(TD.fill_timing, TD.fill_time_roi);
  var ftSel=document.getElementById('filltimeCity');
  if(ftSel){ftSel.addEventListener('change',function(){
    var v=ftSel.value;
    var t=(v==='__ALL__')?TD.fill_timing:((TD.fill_timing_by_city||{})[v]||[]);
    var r=(v==='__ALL__')?TD.fill_time_roi:((TD.fill_time_roi_by_city||{})[v]||[]);
    renderFillTime(t, r);
  });}
}

// Night vs day session
if(TD.fill_session&&TD.fill_session.length){
  var ses=TD.fill_session;
  new Chart(document.getElementById('sessionPnL'),{type:'bar',
    data:{labels:ses.map(function(d){return d.label}),datasets:[{data:ses.map(function(d){return d.profit}),
      backgroundColor:bgP(ses.map(function(d){return d.profit}),'0.6'),
      borderColor:bdP(ses.map(function(d){return d.profit})),borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(fmtDollars),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=ses[c.dataIndex];return [fmtSigned(d.profit),'WR: '+d.wr+'%','ROI: '+d.roi+'%','Cost: $'+d.cost.toLocaleString(),'n='+d.n+' fills']}}}}}});
  new Chart(document.getElementById('sessionROI'),{type:'bar',
    data:{labels:ses.map(function(d){return d.label}),datasets:[{data:ses.map(function(d){return d.roi}),
      backgroundColor:bgP(ses.map(function(d){return d.roi}),'0.6'),
      borderColor:bdP(ses.map(function(d){return d.roi})),borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v+'%'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=ses[c.dataIndex];return [d.roi+'% ROI','WR: '+d.wr+'%','Profit: '+fmtSigned(d.profit),'n='+d.n+' fills']}}}}}});
}

// Fill price vs outcome
if(TD.fill_price&&TD.fill_price.length){
  var fp=TD.fill_price;
  new Chart(document.getElementById('fpEdge'),{type:'bar',
    data:{labels:fp.map(function(d){return d.price+'c'}),datasets:[{data:fp.map(function(d){return d.avg_edge}),
      backgroundColor:bgP(fp.map(function(d){return d.avg_edge}),'0.6'),
      borderColor:bdP(fp.map(function(d){return d.avg_edge})),borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v.toFixed(1)+'c'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=fp[c.dataIndex];return [d.avg_edge.toFixed(1)+'c edge/contract','ROI: '+d.roi+'%','WR: '+d.wr+'%','n='+d.n]}}}}}});
  new Chart(document.getElementById('fpWR'),{type:'bar',
    data:{labels:fp.map(function(d){return d.price+'c'}),datasets:[{data:fp.map(function(d){return d.wr}),
      backgroundColor:'#378ADD99',borderColor:'#378ADD',borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v+'%'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=fp[c.dataIndex];return ['WR: '+d.wr+'%','n='+d.n]}}}}}});
  new Chart(document.getElementById('fpProfit'),{type:'bar',
    data:{labels:fp.map(function(d){return d.price+'c'}),datasets:[{data:fp.map(function(d){return d.profit}),
      backgroundColor:bgP(fp.map(function(d){return d.profit}),'0.6'),
      borderColor:bdP(fp.map(function(d){return d.profit})),borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(fmtDollars),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=fp[c.dataIndex];return [fmtSigned(d.profit),'ROI: '+d.roi+'%','WR: '+d.wr+'%','Cost: $'+d.cost.toLocaleString(),'n='+d.n]}}}}}});
}

if(document.getElementById('platVolChart') && PV && PV.D && PV.D.labels.length){
  var pvDs=CITIES.map(function(c){return {label:CN[c]||c,data:PV.D.by_city[c]||[],backgroundColor:CC[c],borderWidth:0,barPercentage:1,categoryPercentage:1};});
  CHARTS.platVol=new Chart(document.getElementById('platVolChart'),{type:'bar',
    data:{labels:PV.D.labels,datasets:pvDs},
    options:{...base,interaction:{mode:'index',intersect:false},
      plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{
        label:function(c){var v=Math.round(c.raw);if(v===0)return null;return c.dataset.label+': '+(v>=1000?(v/1000).toFixed(1)+'K':v)+' contracts';},
        footer:function(items){var t=items.reduce(function(s,i){return s+i.parsed.y;},0);return 'Total: '+(t>=1000?(t/1000).toFixed(0)+'K':t)+' contracts';}
      }}},
      scales:{x:{stacked:true,grid:{display:false},ticks:{color:TK,font:{size:10},maxTicksLimit:16,maxRotation:45}},
              y:{stacked:true,grid:{color:G},ticks:{color:TK,font:{size:10},callback:function(v){return v>=1000?(v/1000).toFixed(0)+'K':v;}}}}}});
}

// City win-rate bar charts (D/W/M)
function renderCityWR(){
  var cityList=CITIES.filter(function(c){return CWR[c];});
  cityList.sort(function(a,b){return (CWR[b].d_wr||0)-(CWR[a].d_wr||0);});
  function mk(canvasId, key){
    var el=document.getElementById(canvasId);if(!el)return;
    var labels=cityList.map(function(c){return CN[c]||c;});
    var vals=cityList.map(function(c){return CWR[c][key];});
    var ns=cityList.map(function(c){return CWR[c][key.replace('_wr','_n')];});
    var ws=cityList.map(function(c){return CWR[c][key.replace('_wr','_w')];});
    var ls=cityList.map(function(c){return CWR[c][key.replace('_wr','_l')];});
    new Chart(el,{type:'bar',
      data:{labels:labels,datasets:[{data:vals,
        backgroundColor:vals.map(function(v){return v>=55?'rgba(29,158,117,0.6)':(v<=45?'rgba(226,75,74,0.6)':'rgba(148,163,184,0.4)');}),
        borderColor:vals.map(function(v){return v>=55?'#1D9E75':(v<=45?'#E24B4A':'#94a3b8');}),
        borderWidth:1,borderRadius:3}]},
      options:{...base,scales:mkScales(function(v){return v+'%'}),
        plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){
          var i=c.dataIndex;return [c.raw+'%','W-L: '+ws[i]+'-'+ls[i],'n='+ns[i]];
        }}}}}});
  }
  mk('cityWRdaily','d_wr');
  mk('cityWRweekly','w_wr');
  mk('cityWRmonthly','m_wr');

  // B vs T net P&L per city — grouped bar chart sorted by combined net desc
  var kel=document.getElementById('cityKindNet');
  if(kel){
    var ks=cityList.slice().sort(function(a,b){
      return ((CWR[b].kind_b.net||0)+(CWR[b].kind_t.net||0))-((CWR[a].kind_b.net||0)+(CWR[a].kind_t.net||0));
    });
    var lbls=ks.map(function(c){return CN[c]||c;});
    var bNet=ks.map(function(c){return CWR[c].kind_b.net||0;});
    var tNet=ks.map(function(c){return CWR[c].kind_t.net||0;});
    var bN=ks.map(function(c){return CWR[c].kind_b.n||0;});
    var tN=ks.map(function(c){return CWR[c].kind_t.n||0;});
    new Chart(kel,{type:'bar',
      data:{labels:lbls,datasets:[
        {label:'Between',data:bNet,backgroundColor:'rgba(55,138,221,0.7)',borderColor:'#378ADD',borderWidth:1,borderRadius:2},
        {label:'Tail',data:tNet,backgroundColor:'rgba(186,117,23,0.7)',borderColor:'#BA7517',borderWidth:1,borderRadius:2}
      ]},
      options:{...base,interaction:{mode:'index',intersect:false},
        plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{
          label:function(c){var n=(c.datasetIndex===0?bN:tN)[c.dataIndex];
            return c.dataset.label+': $'+(c.raw>=0?'+':'')+c.raw.toLocaleString()+' (n='+n+')';}
        }}},
        scales:mkScales(function(v){return (v>=0?'':'-')+'$'+Math.abs(v).toLocaleString();})}});
  }

  // Gross $ won vs $ lost — grouped bar chart, sorted by net desc
  var gel=document.getElementById('cityGrossDollars');
  if(gel){
    var sorted=cityList.slice().sort(function(a,b){return (CWR[b].net||0)-(CWR[a].net||0);});
    var labels=sorted.map(function(c){return CN[c]||c;});
    var won=sorted.map(function(c){return CWR[c].gross_win;});
    var lost=sorted.map(function(c){return -CWR[c].gross_loss;});  // negative so it plots below axis
    var nets=sorted.map(function(c){return CWR[c].net;});
    var pfs=sorted.map(function(c){return CWR[c].pf;});
    new Chart(gel,{type:'bar',
      data:{labels:labels,datasets:[
        {label:'$ won',data:won,backgroundColor:'rgba(29,158,117,0.7)',borderColor:'#1D9E75',borderWidth:1,borderRadius:2},
        {label:'$ lost',data:lost,backgroundColor:'rgba(226,75,74,0.7)',borderColor:'#E24B4A',borderWidth:1,borderRadius:2}
      ]},
      options:{...base,interaction:{mode:'index',intersect:false},
        plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{
          label:function(c){var v=Math.abs(c.raw);return c.dataset.label+': $'+v.toLocaleString();},
          footer:function(items){var i=items[0].dataIndex;var pf=pfs[i];
            return ['Net: '+(nets[i]>=0?'+':'')+'$'+nets[i].toLocaleString(),
                    'PF: '+(pf==null?'∞':pf.toFixed(2))];
          }
        }}},
        scales:mkScales(function(v){return (v>=0?'':'-')+'$'+Math.abs(v).toLocaleString();})}});
  }
}
if(Object.keys(CWR).length) renderCityWR();

// Bet accuracy charts
if(BA && BA.hist_overall){
  // Signed-distance histogram: stacked bars showing wins vs losses
  var hist=BA.hist_overall;
  var hw=BA.hist_wins||{data:[]}; var hl=BA.hist_losses||{data:[]};
  new Chart(document.getElementById('distHist'),{type:'bar',
    data:{labels:hist.labels,datasets:[
      {label:'Wins',data:hw.data,backgroundColor:'rgba(29,158,117,0.7)',borderColor:'#1D9E75',borderWidth:1,stack:'s'},
      {label:'Losses',data:hl.data,backgroundColor:'rgba(226,75,74,0.7)',borderColor:'#E24B4A',borderWidth:1,stack:'s'}
    ]},
    options:{...base,interaction:{mode:'index',intersect:false},
      plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{
        label:function(c){return c.dataset.label+': '+c.raw;},
        title:function(items){return items[0].label+'°F (actual - strike)';}
      }}},
      scales:{x:{stacked:true,grid:{display:false},ticks:{color:TK,font:{size:9}},title:{display:true,text:'Distance (°F)',color:TK,font:{size:10}}},
        y:{stacked:true,grid:{color:G},ticks:{color:TK,font:{size:10}},title:{display:true,text:'Settlements',color:TK,font:{size:10}}}}}});

  // Mean |distance| by city, sorted asc (most accurate first)
  if(BA.by_city){
    var byCity=Object.keys(BA.by_city).map(function(c){return {c:c,n:BA.by_city[c].all.n,abs:BA.by_city[c].all.mean_abs,signed:BA.by_city[c].all.mean_signed};});
    byCity.sort(function(a,b){return a.abs-b.abs;});
    new Chart(document.getElementById('distByCity'),{type:'bar',
      data:{labels:byCity.map(function(x){return CN[x.c]||x.c;}),
        datasets:[{data:byCity.map(function(x){return x.abs;}),
          backgroundColor:byCity.map(function(x){return CC[x.c]+'99';}),
          borderColor:byCity.map(function(x){return CC[x.c];}),borderWidth:1,borderRadius:3}]},
      options:{...base,scales:mkScales(function(v){return v.toFixed(1)+'°F'}),
        plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){
          var x=byCity[c.dataIndex];
          return [c.raw.toFixed(2)+'°F mean |d|','Signed mean: '+x.signed.toFixed(2)+'°F','n='+x.n];
        }}}}}});
  }
}

CITIES.forEach(function(city){
  var bkts=PL[city];if(!bkts)return;
  var labels=bkts.map(function(b){return b.label+'c'});
  var ppc=bkts.map(function(b){return b.ppc});var pnl=bkts.map(function(b){return b.pnl});
  var el1=document.getElementById('ppc_'+city),el2=document.getElementById('pnl_'+city);
  if(el1)new Chart(el1,{type:'bar',data:{labels:labels,datasets:[{data:ppc,backgroundColor:ppc.map(function(v){return v>=0?CC[city]+'99':'rgba(226,75,74,0.6)'}),borderColor:ppc.map(function(v){return v>=0?CC[city]:'#E24B4A'}),borderWidth:1,borderRadius:3}]},options:{...base,scales:{x:{grid:{display:false},ticks:{color:TK,font:{size:9}}},y:{grid:{color:G},ticks:{color:TK,font:{size:9},callback:function(v){return '$'+v.toFixed(2)}},title:{display:true,text:'$/contract',color:TK,font:{size:9}}}},plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var b=bkts[c.dataIndex];return ['$'+c.raw.toFixed(3)+'/c','n='+b.n,'ROI: '+b.roi+'%']}}}}}});
  if(el2)new Chart(el2,{type:'bar',data:{labels:labels,datasets:[{data:pnl,backgroundColor:pnl.map(function(v){return v>=0?'rgba(29,158,117,0.6)':'rgba(226,75,74,0.6)'}),borderColor:pnl.map(function(v){return v>=0?'#1D9E75':'#E24B4A'}),borderWidth:1,borderRadius:3}]},options:{...base,scales:{x:{grid:{display:false},ticks:{color:TK,font:{size:9}}},y:{grid:{color:G},ticks:{color:TK,font:{size:9},callback:function(v){return '$'+v.toLocaleString()}},title:{display:true,text:'Total P&L',color:TK,font:{size:9}}}},plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var b=bkts[c.dataIndex];var v=Math.round(c.raw);return [(v>=0?'+':'')+('$'+v),'n='+b.n]}}}}}});
});
''')
        f.write('</script>\n</body></html>')
    print(f"Dashboard written to {out_path}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python analyze_weather_dashboard.py <settlement.csv> [trade.csv] [output.html]")
        sys.exit(1)
    # Output path: 3rd CLI arg > WEATHER_DASHBOARD_OUT env var > default (CWD)
    if len(sys.argv) >= 4 and sys.argv[3] and sys.argv[3].lower() != 'none':
        out = sys.argv[3]
    else:
        out = os.environ.get('WEATHER_DASHBOARD_OUT', 'weather_report.html')
    trade_f = sys.argv[2] if len(sys.argv) >= 3 and sys.argv[2] != 'none' else None
    data = run_analysis(sys.argv[1], trade_file=trade_f)
    if data:
        generate_html(data, out)
        o = data['overview']
        print(f"\n=== SUMMARY ===")
        print(f"Net P&L: ${o['total_pnl']:+,.2f} | ROI: {o['roi']}%")
        print(f"Sharpe: {o['sharpe']} | Max DD: ${o['max_dd']:,.2f}")
        print(f"Win rates: {o['daily_wr']}% daily, {o['weekly_wr']}% weekly, {o['monthly_wr']}% monthly")
        for city, s in data['city_summary'].items():
            print(f"  {city}: ${s['pnl']:+,.2f} | ROI:{s['roi']}% | n={s['n']}")
