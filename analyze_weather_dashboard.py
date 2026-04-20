#!/usr/bin/env python3
"""Weather trading analysis — generates interactive HTML dashboard from Kalshi KXHIGH settlement CSV."""
import sys, csv, json, math, os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

def load_csv(fp):
    if not fp or fp == 'none' or not os.path.exists(fp): return []
    with open(fp, encoding='utf-8-sig') as f: return list(csv.DictReader(f))

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

def run_analysis(settlement_file, trade_file=None):
    raw = load_csv(settlement_file)
    settlements = []
    for row in raw:
        if row.get('type') != 'Settlement': continue
        tk = parse_ticker(row.get('Market_Ticker',''))
        pnl = compute_pnl(row)
        dt = parse_date(row.get('Original_Date'))
        day = (dt - timedelta(hours=6)).date() if dt else None
        settlements.append({**tk, **pnl, 'date': dt, 'day': day,
            'day_str': day.isoformat() if day else None,
            'dow': day.weekday() if day else None})

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
    down = [p for p in daily_pnls if p<0]
    ds = math.sqrt(sum(p**2 for p in down)/len(down)) if down else 0
    sortino = (avg_d/ds*ann) if ds>0 else None
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

    overview = {
        'total_pnl':round(tp,2),'total_cost':round(tc,2),'roi':round(roi,1),
        'n_settlements':len(active_cities),'n_days':n_days,
        'sharpe':round(sharpe,2) if sharpe else None,'sortino':round(sortino,2) if sortino else None,
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
    for s in active_cities:
        if s['day_str'] and s['city'] in cities:
            city_day_pnl[s['city']][s['day_str']] += s['pnl']
            city_day_cost[s['city']][s['day_str']] += s['cost']

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
        return {'labels':plabels,'pnl':pnls,'cost':costs,'roi':rois,'cum_total':cum_total,
                'roi_roll':roi_roll,'cost_roll':cost_roll,'city_pnl':c_daily,'city_cum':c_cum,'city_roi':c_roi}

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
    n_al=n_aw=n_ml=n_dd=0
    for d2,cp in cdp.items():
        if len(cp)<2: continue
        n_dd+=1; lo=sum(1 for v in cp.values() if v<0); wi=sum(1 for v in cp.values() if v>0)
        if lo==len(cp): n_al+=1
        if wi==len(cp): n_aw+=1
        if lo>len(cp)/2: n_ml+=1
    n_pairs = len(cities)*(len(cities)-1)//2
    corr_stats={'avg_r':round(sum(corr_matrix[c1][c2] for c1 in cities for c2 in cities if c1<c2)/max(n_pairs,1),3),
        'all_lose_pct':round(n_al/n_dd*100,1) if n_dd else 0,
        'all_win_pct':round(n_aw/n_dd*100,1) if n_dd else 0,
        'majority_lose_pct':round(n_ml/n_dd*100,1) if n_dd else 0}

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

    # Recent settlements
    rec_sett=sorted(active_cities,key=lambda s:s['day_str'] or '',reverse=True)[:50]
    recent_out=[]
    for s in rec_sett:
        side='No' if s['no_c']>0 and s['yes_c']==0 else ('Yes' if s['yes_c']>0 and s['no_c']==0 else 'Both')
        recent_out.append({'date':s['day_str'] or '','ticker':s.get('ticker',''),'city':s['city'],
            'threshold':s['threshold'],'side':side,'cost':round(s['cost'],2),'pnl':round(s['pnl'],2)})

    # ── Trade-based analyses (if trade CSV provided) ──
    trade_data = {'fill_timing':[],'fill_time_roi':[],'size_buckets':[],'fill_price':[]}
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
            trades.append({'ticker':tk, 'date':dt,
                'price':int(row.get('Price_In_Cents') or 0),
                'amount':float(row.get('Amount_In_Dollars') or 0),
                'hour':dt.hour, 'order_type':row.get('Order_Type',''),
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

        # Fill time ROI — match trades to settlements, bucket by ET hour
        # For Direction=No trades, Price_In_Cents IS the NO price paid per contract.
        # Win (settles NO):  edge per contract = 100 - NO_price (payout $1 - cost)
        # Loss (settles YES): edge per contract = -NO_price
        hour_pnl = defaultdict(lambda:{'n':0,'edge':0,'wins':0,'amount':0})
        for t in trades:
            s = sett_by_ticker.get(t['ticker'])
            if not s: continue
            et_h = (t['hour'] - 4) % 24  # UTC to ET
            won = s['result'] == 'no'
            edge = (100 - t['price']) if won else -t['price']
            hp = hour_pnl[et_h]
            hp['n']+=1; hp['edge']+=edge; hp['amount']+=t['amount']
            if won: hp['wins']+=1
        for h in range(24):
            hp = hour_pnl.get(h)
            if not hp or hp['n'] < 5: continue
            roi = (hp['edge']/100) / hp['amount'] * 100 if hp['amount'] > 0 else 0
            trade_data['fill_time_roi'].append({'hour':h,'label':f"{h:02d}:00",
                'n':hp['n'],'wr':round(hp['wins']/hp['n']*100,1),
                'avg_edge':round(hp['edge']/hp['n'],2),
                'total_edge':round(hp['edge'],0),'roi':round(roi,1)})

        # Fill price vs settlement outcome
        # Bucket by NO price paid (t['price'] for Direction=No trades).
        # avg_edge = mean edge across all trades in bucket = realized cents profit per contract.
        fp_buckets = defaultdict(lambda:{'n':0,'wins':0,'edge':0})
        for t in trades:
            s = sett_by_ticker.get(t['ticker'])
            if not s: continue
            no_price = t['price']
            buc = (no_price//5)*5
            won = s['result'] == 'no'
            edge = (100 - t['price']) if won else -t['price']
            b = fp_buckets[buc]
            b['n']+=1; b['edge']+=edge
            if won: b['wins']+=1
        for buc in sorted(fp_buckets.keys()):
            b = fp_buckets[buc]
            if b['n'] < 5: continue
            trade_data['fill_price'].append({'price':f"{buc}-{buc+4}",
                'n':b['n'],'wr':round(b['wins']/b['n']*100,1),
                'avg_edge':round(b['edge']/b['n'],2),
                'total_edge':round(b['edge'],0)})

    # Win rate / ROI by position size (from settlements, no trade CSV needed)
    sz_buckets = defaultdict(lambda:{'n':0,'pnl':0,'cost':0,'wins':0,'contracts':0,'avg_price':0})
    for s in [x for x in active_cities if x['no_c']>0]:
        buc = min((s['no_c']//100)*100, 900)
        b = sz_buckets[buc]
        b['n']+=1; b['pnl']+=s['pnl']; b['cost']+=s['cost']; b['contracts']+=s['no_c']
        b['avg_price']+=s['no_avg']*100
        if s['pnl']>0: b['wins']+=1
    for buc in sorted(sz_buckets.keys()):
        b = sz_buckets[buc]
        if b['n'] < 5: continue
        trade_data['size_buckets'].append({'size':f"{buc}-{buc+99}",'n':b['n'],
            'pnl':round(b['pnl'],0),'roi':round(b['pnl']/b['cost']*100,1) if b['cost']>0 else 0,
            'wr':round(b['wins']/b['n']*100,1),'avg_price':round(b['avg_price']/b['n'],1),
            'avg_pnl':round(b['pnl']/b['n'],2)})

    return {'overview':overview,'periods':periods,'cities':cities,'sharpe_ts':{'labels':sharpe_labels,'data':sharpe_data},
        'dist':{'outlay':{'labels':outlay_l,'data':outlay_d},'up':{'labels':up_l,'data':up_d},'down':{'labels':dn_l,'data':dn_d}},
        'corr':{'matrix':corr_matrix,'stats':corr_stats},'dow':dow_data,
        'price_levels':price_level_data,'city_summary':city_summary,'recent':recent_out,
        'trade':trade_data}


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

    with open(out_path, 'w') as f:
        f.write(f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
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
        for sid,lbl in [('summary','Summary'),('overview','Overview'),('timeseries','Time series'),('distributions','Distributions'),
                        ('correlation','Correlation'),('sharpe','Rolling Sharpe'),('dow','Day of week'),
                        ('possize','Position size'),('filltime','Fill timing'),('fillprice','Fill price'),
                        ('pricelevels','Price levels'),('citysummary','City summary'),('recent','Recent')]:
            f.write(f'<a onclick="document.getElementById(\'{sid}\').scrollIntoView({{behavior:\'smooth\'}})">{lbl}</a>')
        f.write('</div>\n')

        # Summary
        f.write(f'''<div id="summary"><h2>Executive summary</h2>
<div style="background:var(--bg2);border-radius:10px;padding:1.25rem;margin-bottom:1rem;line-height:1.7">
<p><strong>All-time:</strong> Deployed ${o['total_cost']:,.0f} over {o['n_days']} trading days since {daily_labels[0] if daily_labels else "—"}, generating {money(o['total_pnl'])} at {o['roi']:+.1f}% ROI. Sharpe {sh}, max drawdown ${o['max_dd']:,.0f}. Profitable on {o['daily_wr']:.0f}% of days, {o['weekly_wr']:.0f}% of weeks, {o['monthly_wr']:.0f}% of months.</p>
<p><strong>Last 7 days ({r7['n']} trading days):</strong> P&L {money(r7['pnl'])} on ${r7['cost']:,.0f} outlay ({r7['roi']:+.1f}% ROI), win rate {r7['wr']:.0f}%. Vs prior 7d ({money(p7['pnl'])}): {d7_vs}.</p>
<p><strong>Last 30 days ({r30['n']} trading days):</strong> P&L {money(r30['pnl'])} on ${r30['cost']:,.0f} outlay ({r30['roi']:+.1f}% ROI), win rate {r30['wr']:.0f}%. Vs prior 30d ({money(p30['pnl'])} at {p30['roi']:+.1f}%): {d30_dir}.</p>
<p><strong>City leaderboard (90d):</strong> {CN.get(top_c[0],top_c[0]) if top_c else "—"} leads at {money(top_c[1]['r90_pnl']) if top_c else "—"} ({top_c[1]['r90_roi']:+.1f}% ROI), {CN.get(bot_c[0],bot_c[0]) if bot_c else "—"} trails at {money(bot_c[1]['r90_pnl']) if bot_c else "—"} ({bot_c[1]['r90_roi']:+.1f}% ROI).</p>
<p><strong>Timing:</strong> {best_d['day'] if best_d else "—"} is strongest ({money(best_d['pnl']) if best_d else "—"}, {best_d['roi']:+.1f}% ROI), {worst_d['day'] if worst_d else "—"} is weakest ({money(worst_d['pnl']) if worst_d else "—"}, {worst_d['roi']:+.1f}% ROI).</p>
</div></div>
''')

        # Overview cards
        f.write(f'''<div id="overview"><h2>Overview</h2>
<div class="cards">
<div class="card"><div class="l">Net P&L</div><div class="v {'green' if o['total_pnl']>0 else 'red'}">${o['total_pnl']:+,.0f}</div><div class="s">ROI: {o['roi']:+.1f}%</div></div>
<div class="card"><div class="l">Sharpe</div><div class="v">{sh}</div><div class="s">Sortino: {so}</div></div>
<div class="card"><div class="l">Max DD</div><div class="v red">${o['max_dd']:,.0f}</div></div>
<div class="card"><div class="l">Avg outlay/day</div><div class="v">${o['avg_daily_cost']:,.0f}</div><div class="s">p25 ${o['outlay_p25']:,.0f} &bull; p50 ${o['outlay_p50']:,.0f} &bull; p75 ${o['outlay_p75']:,.0f}</div></div>
<div class="card"><div class="l">Avg profit/day</div><div class="v {'green' if o['avg_daily_pnl']>0 else 'red'}">${o['avg_daily_pnl']:+,.2f}</div><div class="s"><span class="green">win ${o['avg_win']:+,.0f}</span> &bull; <span class="red">loss ${o['avg_loss']:+,.0f}</span></div></div>
<div class="card"><div class="l">Daily WR</div><div class="v">{o['daily_wr']:.1f}%</div><div class="s">{o['n_days']} days</div></div>
<div class="card"><div class="l">Weekly WR</div><div class="v">{o['weekly_wr']:.1f}%</div><div class="s">{o['n_weeks']} weeks</div></div>
<div class="card"><div class="l">Monthly WR</div><div class="v">{o['monthly_wr']:.1f}%</div><div class="s">{o['n_months']} months</div></div>
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
        f.write('<h3>Capital outlay</h3>\n<div class="cc"><canvas id="costChart"></canvas></div>\n</div>\n')

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
</div><div style="overflow-x:auto"><table class="corr">{corr_rows}</table></div></div>\n''')

        # Rolling Sharpe
        f.write('<div id="sharpe"><h2>Rolling 30-day Sharpe</h2>\n<div class="cc"><canvas id="sharpeChart"></canvas></div></div>\n')

        # Day of week
        f.write('<div id="dow"><h2>Day of week</h2>\n<div class="cc"><canvas id="dowChart"></canvas></div></div>\n')

        # ── Trade-based sections ──
        td = data['trade']
        has_trades = bool(td['fill_timing'])

        # Position size (always available — from settlements)
        f.write('''<div id="possize"><h2>Position size analysis</h2>
<p class="sub">Win rate and ROI by number of NO contracts held at settlement. Lower win rate at larger sizes is expected — wins are bigger to compensate.</p>
<div class="g"><div><h3>ROI by position size</h3><div class="cc"><canvas id="sizeROI"></canvas></div></div>
<div><h3>Win rate by position size</h3><div class="cc"><canvas id="sizeWR"></canvas></div></div></div></div>\n''')

        if has_trades:
            # Fill timing
            f.write('''<div id="filltime"><h2>Fill time analysis (ET)</h2>
<p class="sub">When do your NO orders get filled? Evening/overnight fills tend to have better edge than morning fills.</p>
<div class="g"><div><h3>Fills per hour</h3><div class="cc tall"><canvas id="hourFills"></canvas></div></div>
<div><h3>Edge per contract by hour (cents)</h3><div class="cc tall"><canvas id="hourEdge"></canvas></div></div></div></div>\n''')

            # Fill price vs outcome
            f.write('''<div id="fillprice"><h2>Fill price vs settlement outcome</h2>
<p class="sub">At each NO price level, what's the realized edge per contract? Positive = buying NO below fair value. The critical cutoff is where edge turns negative.</p>
<div class="g"><div><h3>Edge per contract (cents) by NO fill price</h3><div class="cc"><canvas id="fpEdge"></canvas></div></div>
<div><h3>Win rate by NO fill price</h3><div class="cc"><canvas id="fpWR"></canvas></div></div></div></div>\n''')

        # Add nav items for new sections
        # (already handled below when we update the nav)

        # Price levels
        f.write(f'<div id="pricelevels"><h2>Edge per dollar by price level</h2>\n{pl_html}</div>\n')

        # City summary
        f.write(f'''<div id="citysummary"><h2>City summary</h2>
<table><thead><tr><th>City</th><th>n</th><th>P&L</th><th>ROI</th><th>B n</th><th>B P&L</th><th>T n</th><th>T P&L</th><th>Last 90d</th></tr></thead>
<tbody>{cs_rows}</tbody></table></div>\n''')

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

CHARTS.cost=new Chart(document.getElementById('costChart'),{
  data:{labels:d.labels,datasets:[
    {type:'bar',data:d.cost,backgroundColor:bgP(d.pnl,'0.5'),borderWidth:0,barPercentage:1,categoryPercentage:1,order:2,label:'Outlay'},
    {type:'line',data:d.cost_roll,borderColor:'#378ADD',borderWidth:2,pointRadius:0,pointHoverRadius:4,tension:0.3,fill:false,order:1,label:'Rolling avg'}
  ]},options:{...base,interaction:{mode:'index',intersect:false},scales:mkScales(function(v){return '$'+v.toLocaleString()}),
    plugins:{...base.plugins,tooltip:{mode:'index',intersect:false,callbacks:{label:function(c){return c.dataset.label+': $'+Math.round(c.raw).toLocaleString()}}}}}});

function setGran(g){
  currentGran=g;var d=PERIODS[g];
  document.getElementById('rollNote').textContent=ROLL_NAME[g]+' rolling';
  CHARTS.totalCum.data.labels=d.labels;CHARTS.totalCum.data.datasets[0].data=d.cum_total;CHARTS.totalCum.update();
  CHARTS.roi.data.labels=d.labels;CHARTS.roi.data.datasets[0].data=d.roi;CHARTS.roi.data.datasets[1].data=d.roi_roll;CHARTS.roi.update();
  CHARTS.cityRoi.data.labels=d.labels;CITIES.forEach(function(c,i){CHARTS.cityRoi.data.datasets[i].data=d.city_roi[c]||[];});CHARTS.cityRoi.update();
  CHARTS.pnl.data.labels=d.labels;CHARTS.pnl.data.datasets[0].data=d.pnl;CHARTS.pnl.data.datasets[0].backgroundColor=bgP(d.pnl,'0.6');CHARTS.pnl.data.datasets[0].borderColor=bdP(d.pnl);CHARTS.pnl.update();
  CHARTS.cum.data.labels=d.labels;CITIES.forEach(function(c,i){var a=d.city_cum[c]||[];var fl=a.length?a[a.length-1]:0;CHARTS.cum.data.datasets[i].data=a;CHARTS.cum.data.datasets[i].label=CN[c]+' $'+(fl>=0?'+':'')+Math.round(fl).toLocaleString();});CHARTS.cum.update();
  CHARTS.stack.data.labels=d.labels;CITIES.forEach(function(c,i){CHARTS.stack.data.datasets[i].data=d.city_pnl[c]||[];});CHARTS.stack.update();
  CHARTS.cost.data.labels=d.labels;CHARTS.cost.data.datasets[0].data=d.cost;CHARTS.cost.data.datasets[0].backgroundColor=bgP(d.pnl,'0.5');CHARTS.cost.data.datasets[1].data=d.cost_roll;CHARTS.cost.update();
  document.querySelectorAll('.gran-btn').forEach(function(b){b.classList.toggle('active',b.dataset.gran===g);});
}
document.querySelectorAll('.gran-btn').forEach(function(b){b.addEventListener('click',function(){setGran(b.dataset.gran);});});

function fadeColor(c,a){if(!c||typeof c!=='string')return c;if(c[0]==='#'){var r=parseInt(c.slice(1,3),16),g=parseInt(c.slice(3,5),16),b=parseInt(c.slice(5,7),16);return 'rgba('+r+','+g+','+b+','+a+')';}if(c.indexOf('rgba')===0)return c.replace(/[\d.]+\)$/,a+')');return c;}
function highlightDataset(cn,ti){var ch=CHARTS[cn];if(!ch)return;ch.data.datasets.forEach(function(ds,i){if(!('_ob' in ds)){ds._ob=ds.borderColor;ds._og=ds.backgroundColor;ds._ow=ds.borderWidth;}if(ti===null){ds.borderColor=ds._ob;ds.backgroundColor=ds._og;ds.borderWidth=ds._ow;}else if(i===ti){ds.borderColor=ds._ob;ds.backgroundColor=ds._og;ds.borderWidth=(ds._ow||2)+1.5;}else{ds.borderColor=fadeColor(ds._ob,0.12);if(typeof ds._og==='string')ds.backgroundColor=fadeColor(ds._og,0.12);ds.borderWidth=1;}});ch.update('none');}
document.querySelectorAll('.legend-item').forEach(function(el){el.addEventListener('mouseenter',function(){highlightDataset(el.dataset.chart,parseInt(el.dataset.idx));});el.addEventListener('mouseleave',function(){highlightDataset(el.dataset.chart,null);});});

function mkHist(id,d,color){new Chart(document.getElementById(id),{type:'bar',data:{labels:d.labels,datasets:[{data:d.data,backgroundColor:color+'99',borderColor:color,borderWidth:1,borderRadius:2,barPercentage:1,categoryPercentage:.92}]},options:{...base,scales:mkScales(function(v){return v})}});}
mkHist('distOutlay',DIST.outlay,'#378ADD');mkHist('distUp',DIST.up,'#1D9E75');mkHist('distDown',DIST.down,'#E24B4A');

new Chart(document.getElementById('sharpeChart'),{type:'line',data:{labels:SHARPE.labels,datasets:[{data:SHARPE.data,borderColor:'#378ADD',borderWidth:2,pointRadius:0,pointHoverRadius:3,tension:0.3,fill:{target:'origin',above:'rgba(29,158,117,0.08)',below:'rgba(226,75,74,0.08)'}},{data:SHARPE.labels.map(function(){return 0}),borderColor:'rgba(226,75,74,0.3)',borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false}]},options:{...base,interaction:{mode:'index',intersect:false},scales:mkScales(function(v){return v.toFixed(1)}),plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){return c.datasetIndex===0?'Sharpe: '+c.raw.toFixed(2):null}}}}}});

var dowL=DOW.map(function(d){return d.day}),dowP=DOW.map(function(d){return d.pnl});
new Chart(document.getElementById('dowChart'),{type:'bar',data:{labels:dowL,datasets:[{data:dowP,backgroundColor:bgP(dowP,'0.6'),borderRadius:4}]},options:{...base,scales:mkScales(function(v){return (v>=0?'':'-')+'$'+Math.abs(v).toLocaleString()}),plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=DOW[c.dataIndex];return ['$'+Math.round(d.pnl).toLocaleString(),'ROI: '+d.roi+'%','WR: '+d.wr+'%','n='+d.n]}}}}}});

// Position size
if(TD.size_buckets&&TD.size_buckets.length){
  var sz=TD.size_buckets;
  new Chart(document.getElementById('sizeROI'),{type:'bar',
    data:{labels:sz.map(function(d){return d.size}),datasets:[{data:sz.map(function(d){return d.roi}),
      backgroundColor:bgP(sz.map(function(d){return d.roi}),'0.6'),borderColor:bdP(sz.map(function(d){return d.roi})),borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v+'%'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=sz[c.dataIndex];return ['ROI: '+d.roi+'%','P&L: $'+d.pnl,'Avg price: '+d.avg_price+'c','n='+d.n]}}}}}});
  new Chart(document.getElementById('sizeWR'),{type:'bar',
    data:{labels:sz.map(function(d){return d.size}),datasets:[{data:sz.map(function(d){return d.wr}),
      backgroundColor:'#378ADD99',borderColor:'#378ADD',borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v+'%'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=sz[c.dataIndex];return ['WR: '+d.wr+'%','Avg P&L/settle: $'+d.avg_pnl,'n='+d.n]}}}}}});
}

// Fill timing
if(TD.fill_timing&&TD.fill_timing.length){
  new Chart(document.getElementById('hourFills'),{type:'bar',
    data:{labels:TD.fill_timing.map(function(d){return d.label}),datasets:[{data:TD.fill_timing.map(function(d){return d.n}),
      backgroundColor:'#378ADD99',borderColor:'#378ADD',borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=TD.fill_timing[c.dataIndex];return [d.n+' fills','$'+d.amount+' deployed']}}}}}});
}
if(TD.fill_time_roi&&TD.fill_time_roi.length){
  var ftr=TD.fill_time_roi;
  new Chart(document.getElementById('hourEdge'),{type:'bar',
    data:{labels:ftr.map(function(d){return d.label}),datasets:[{data:ftr.map(function(d){return d.avg_edge}),
      backgroundColor:bgP(ftr.map(function(d){return d.avg_edge}),'0.6'),
      borderColor:bdP(ftr.map(function(d){return d.avg_edge})),borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v.toFixed(1)+'c'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=ftr[c.dataIndex];return [d.avg_edge.toFixed(1)+'c/contract','WR: '+d.wr+'%','ROI: '+d.roi+'%','Total: '+d.total_edge+'c','n='+d.n]}}}}}});
}

// Fill price vs outcome
if(TD.fill_price&&TD.fill_price.length){
  var fp=TD.fill_price;
  new Chart(document.getElementById('fpEdge'),{type:'bar',
    data:{labels:fp.map(function(d){return d.price+'c'}),datasets:[{data:fp.map(function(d){return d.avg_edge}),
      backgroundColor:bgP(fp.map(function(d){return d.avg_edge}),'0.6'),
      borderColor:bdP(fp.map(function(d){return d.avg_edge})),borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v.toFixed(1)+'c'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=fp[c.dataIndex];return [d.avg_edge.toFixed(1)+'c edge/contract','Total: '+d.total_edge+'c','WR: '+d.wr+'%','n='+d.n]}}}}}});
  new Chart(document.getElementById('fpWR'),{type:'bar',
    data:{labels:fp.map(function(d){return d.price+'c'}),datasets:[{data:fp.map(function(d){return d.wr}),
      backgroundColor:'#378ADD99',borderColor:'#378ADD',borderWidth:1,borderRadius:3}]},
    options:{...base,scales:mkScales(function(v){return v+'%'}),
      plugins:{...base.plugins,tooltip:{callbacks:{label:function(c){var d=fp[c.dataIndex];return ['WR: '+d.wr+'%','n='+d.n]}}}}}});
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
