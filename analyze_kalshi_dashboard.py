#!/usr/bin/env python3
"""Kalshi trading analysis — generates interactive HTML dashboard from Kalshi CSV exports.
Matches the reference output format: consolidated + per-family sections + fill rates.
Usage: python3 analyze.py <settlement.csv|none> <trade.csv|none> <order.csv|none> [label]
"""
import sys, csv, json, math, os, re, html as ht
from datetime import datetime, timezone, timedelta
from collections import defaultdict

def load_csv(fp):
    if not fp or fp.lower()=='none' or not os.path.exists(fp): return []
    with open(fp, encoding='utf-8-sig') as f: return list(csv.DictReader(f))

def parse_date(s):
    if not s: return None
    for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ','%Y-%m-%dT%H:%M:%SZ','%Y-%m-%d'):
        try: return datetime.strptime(s,fmt).replace(tzinfo=timezone.utc)
        except ValueError: pass
    return None
def sf(v,d=0.0):
    try:
        if v is None or v=='': return d
        return float(v)
    except: return d
def si(v,d=0):
    try:
        if v is None or v=='': return d
        return int(float(v))
    except: return d

# ── Strategy mapping ──
SERIES_MAP={'KXNBAMENTION':'NBA','KXNCAABMENTION':'NCAAB','KXMLBMENTION':'MLB',
    'KXFIGHTMENTION':'Fight','KXUFCFIGHT':'UFC Props','KXUFCDISTANCE':'UFC Props','KXUFCMOV':'UFC Props',
    'KXTRUMPMENTION':'Politics','KXPERFORMSUPERBOWLB':'Performance',
    'KXNFLMENTION':'NFL','KXENTMENTION':'Entertainment'}
CITY_NAMES={'NY':'New York','AUS':'Austin','PHIL':'Philadelphia','MIA':'Miami',
    'CHI':'Chicago','LAX':'Los Angeles','DEN':'Denver','HOU':'Houston',
    'THOU':'Houston','TDAL':'Dallas','TNOLA':'New Orleans','TPHX':'Phoenix',
    'TLV':'Las Vegas','TSATX':'San Antonio','TATL':'Atlanta','TSEA':'Seattle',
    'TDC':'DC','TOKC':'OKC','TMIN':'Minneapolis','TSFO':'San Francisco',
    'TDEN':'Denver','TBOS':'Boston','TCHAR':'Charlotte','TNASH':'Nashville','TTAMPA':'Tampa'}
FC_COLORS={'NBA':'#60a5fa','NCAAB':'#c084fc','MLB':'#4ade80','Fight':'#f87171',
    'UFC Props':'#f472b6','Weather':'#22d3ee','NFL':'#fb923c','Politics':'#fbbf24',
    'Performance':'#2dd4bf','Entertainment':'#94a3b8','Crypto':'#e879f9','Other':'#6b7280'}
CRYPTO_RE=re.compile(r'^KX(BTC|ETH|SOL|XRP|DOGE|BNB|HYPE|ZEC|LTC|ADA|AVAX|LINK|SUI|TON|TRX|SHIB|PEPE|WLD)')
PALETTE=['#60a5fa','#c084fc','#4ade80','#f87171','#fbbf24','#22d3ee','#2dd4bf','#f472b6','#fb923c','#94a3b8',
    '#818cf8','#a78bfa','#34d399','#fb7185','#fcd34d','#67e8f9','#5eead4','#f9a8d4','#fdba74','#cbd5e1',
    '#6366f1','#8b5cf6','#10b981','#ef4444','#f59e0b','#06b6d4','#14b8a6','#ec4899','#f97316','#64748b']

_MONTHS={m:i+1 for i,m in enumerate(['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'])}
_TICKER_DATE_RE=re.compile(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})')
def event_date_from_ticker(ticker):
    m=_TICKER_DATE_RE.search(ticker or '')
    if not m: return None
    yy,mon,dd=m.groups()
    try: return f'20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}'
    except (KeyError,ValueError): return None

def extract_series(ticker):
    m=re.match(r'^(KX[A-Z]+)',ticker or '')
    return m.group(1) if m else 'UNKNOWN'

def series_to_family(series):
    if series in SERIES_MAP: return SERIES_MAP[series]
    if series.startswith('KXHIGH'): return 'Weather'
    if CRYPTO_RE.match(series): return 'Crypto'
    return 'Other'

def crypto_asset(ticker):
    m=CRYPTO_RE.match(ticker or '')
    return m.group(1) if m else 'UNKNOWN'

def extract_subgroup(ticker, family):
    """Extract city/team/code for sub-group breakdown within a family."""
    if family=='Weather':
        series=extract_series(ticker)
        code=series.replace('KXHIGH','')
        return CITY_NAMES.get(code, code)
    if family=='Crypto':
        return crypto_asset(ticker)
    parts=ticker.split('-')
    return parts[-1] if len(parts)>=3 else ticker

# ── Processing ──
def process_settlements(raw):
    out=[]
    for r in raw:
        if r.get('type')!='Settlement': continue
        ticker=r.get('Market_Ticker','')
        yc=si(r.get('Yes_Contracts_Owned')); nc=si(r.get('No_Contracts_Owned'))
        ya=sf(r.get('Yes_Contracts_Average_Price_In_Cents')); na=sf(r.get('No_Contracts_Average_Price_In_Cents'))
        res=(r.get('Result') or '').strip().lower()
        cost=yc*ya+nc*na
        if res=='yes': pay=yc*1.0
        elif res=='no': pay=nc*1.0
        else: pay=sf(r.get('Profit_In_Dollars'))+cost
        pnl=pay-cost; total_c=yc+nc
        if total_c==0: continue
        dt=parse_date(r.get('Original_Date'))
        day=(dt-timedelta(hours=6)).date() if dt else None
        series=extract_series(ticker); family=series_to_family(series)
        direction='No' if nc>yc else ('Yes' if yc>nc else ('No' if nc>0 else 'Yes'))
        code=ticker.split('-')[-1] if '-' in ticker else ticker
        # price bucket (5c bins based on avg price of held side)
        if nc>0: price_c=round(na*100)
        elif yc>0: price_c=round(ya*100)
        else: price_c=0
        bucket=(price_c//5)*5
        subgroup=extract_subgroup(ticker, family)
        event_str=event_date_from_ticker(ticker)
        out.append({'ticker':ticker,'series':series,'family':family,'subgroup':subgroup,
            'code':code,'pnl':pnl,'cost':cost,'payout':pay,'yc':yc,'nc':nc,'ya':ya,'na':na,
            'result':res,'total_c':total_c,'direction':direction,'bucket':bucket,
            'date':dt,'day':day,'day_str':day.isoformat() if day else None,
            'event_str':event_str})
    return out

def process_trades(raw):
    out=[]
    for r in raw:
        if r.get('type')!='Trade': continue
        ticker=r.get('Market_Ticker','')
        dt=parse_date(r.get('Original_Date'))
        day=(dt-timedelta(hours=6)).date() if dt else None
        contracts=sf(r.get('Amount_In_Dollars'))
        price_c=sf(r.get('Price_In_Cents'))
        notional=contracts*price_c/100.0
        direction=(r.get('Direction') or '').strip()
        mt=(r.get('Order_Type') or '').strip()
        family=series_to_family(extract_series(ticker))
        out.append({'ticker':ticker,'family':family,'date':dt,'day':day,
            'day_str':day.isoformat() if day else None,
            'contracts':contracts,'price_c':price_c,'notional':notional,
            'direction':direction,'maker_taker':mt,'fee':sf(r.get('Fee_In_Dollars'))})
    return out

def process_orders(raw):
    out=[]
    for r in raw:
        if r.get('type')!='Order': continue
        ticker=r.get('Market_Ticker','')
        dt=parse_date(r.get('Original_Date'))
        day=(dt-timedelta(hours=6)).date() if dt else None
        filled=si(r.get('Filled')); remaining=si(r.get('Remaining'))
        status=(r.get('Status') or '').strip()
        family=series_to_family(extract_series(ticker))
        out.append({'ticker':ticker,'family':family,'date':dt,'day':day,
            'day_str':day.isoformat() if day else None,
            'filled':filled,'remaining':remaining,'total':filled+remaining,'status':status})
    return out

# ── Stats helpers ──
def pct(arr,p):
    if not arr: return 0
    s=sorted(arr);k=(len(s)-1)*p;f=int(k);c=min(f+1,len(s)-1)
    return s[f]+(s[c]-s[f])*(k-f)

def analyze_group(settlements, trades=None, orders=None, label='All'):
    """Compute full stats for a group of settlements."""
    if not settlements: return None
    tp=sum(s['pnl'] for s in settlements); tc=sum(s['cost'] for s in settlements)
    n=len(settlements); roi=tp/tc*100 if tc>0 else 0
    wins=[s for s in settlements if s['pnl']>0]; losses=[s for s in settlements if s['pnl']<0]
    nw=len(wins); nl=len(losses)
    wr=nw/n*100 if n else 0
    aw=sum(s['pnl'] for s in wins)/nw if nw else 0
    al=sum(s['pnl'] for s in losses)/nl if nl else 0
    gw=sum(s['pnl'] for s in wins); gl=abs(sum(s['pnl'] for s in losses))
    pf=round(gw/gl,3) if gl>0 else 0
    ev=tp/n if n else 0

    # Daily
    daily=defaultdict(lambda:{'pnl':0,'cost':0})
    for s in settlements:
        if s['day_str']: daily[s['day_str']]['pnl']+=s['pnl']; daily[s['day_str']]['cost']+=s['cost']
    ds=sorted(daily.items()); dpnls=[v['pnl'] for _,v in ds]; dlabels=[k for k,_ in ds]
    nd=len(dpnls); avg_d=sum(dpnls)/nd if nd else 0
    std_d=math.sqrt(sum((p-avg_d)**2 for p in dpnls)/(nd-1)) if nd>1 else 0
    ann=math.sqrt(365)
    sharpe=(avg_d/std_d*ann) if std_d>0 else 0
    down=[p for p in dpnls if p<0]
    dstd=math.sqrt(sum(p**2 for p in down)/len(down)) if down else 0
    sortino_v=(avg_d/dstd*ann) if dstd>0 else 0
    up_sum=sum(p for p in dpnls if p>0); dn_sum=-sum(p for p in down)
    omega=(up_sum/dn_sum) if dn_sum>0 else 0

    # Drawdown with date range
    cum=0;peak=0;mdd=0;dd_series=[];cum_series=[]
    mdd_start=mdd_end=mdd_peak_date=None; cur_peak_date=dlabels[0] if dlabels else None
    for i,p in enumerate(dpnls):
        cum+=p; cum_series.append([dlabels[i],round(cum,2)])
        if cum>peak: peak=cum; cur_peak_date=dlabels[i]
        dd=cum-peak; dd_series.append([dlabels[i],round(dd,2)])
        if -dd>mdd: mdd=-dd; mdd_start=cur_peak_date; mdd_end=dlabels[i]

    var95=round(pct(dpnls,0.05),2) if dpnls else 0
    best_day=max(dpnls) if dpnls else 0; worst_day=min(dpnls) if dpnls else 0

    # Daily P&L series
    daily_pnl_series=[[k,round(v['pnl'],2)] for k,v in ds]

    # Subgroup breakdown
    subgroups=sorted(set(s['subgroup'] for s in settlements))
    sg_stats={}
    for sg in subgroups:
        ss=[s for s in settlements if s['subgroup']==sg]
        sp=sum(s['pnl'] for s in ss); sc=sum(s['cost'] for s in ss)
        sw=sum(1 for s in ss if s['pnl']>0); stc=sum(s['total_c'] for s in ss)
        sg_stats[sg]={'n':len(ss),'pnl':round(sp,2),'cost':round(sc,2),
            'roi':round(sp/sc*100,1) if sc>0 else 0,
            'wr':round(sw/len(ss)*100,1) if ss else 0,
            'contracts':stc,'ppc':round(sp/stc,4) if stc else 0}

    # Cumulative by subgroup
    sg_cum={}
    for sg in subgroups:
        sgd=defaultdict(float)
        for s in settlements:
            if s['subgroup']==sg and s['day_str']: sgd[s['day_str']]+=s['pnl']
        c=0; pts=[]
        for d_str in sorted(sgd.keys()):
            c+=sgd[d_str]; pts.append([d_str,round(c,2)])
        sg_cum[sg]=pts

    # Daily by subgroup (for stacked bar)
    sg_daily={}
    for sg in subgroups:
        sgd={}
        for s in settlements:
            if s['subgroup']==sg and s['day_str']:
                sgd[s['day_str']]=sgd.get(s['day_str'],0)+s['pnl']
        sg_daily[sg]=[[k,round(v,2)] for k,v in sorted(sgd.items())]

    # Direction breakdown
    dir_stats={}
    for d in ['Yes','No']:
        dd2=[s for s in settlements if s['direction']==d]
        if not dd2: continue
        dp=sum(s['pnl'] for s in dd2); dc=sum(s['cost'] for s in dd2)
        dw=sum(1 for s in dd2 if s['pnl']>0); dtc=sum(s['total_c'] for s in dd2)
        dir_stats[d]={'n':len(dd2),'pnl':round(dp,2),'cost':round(dc,2),
            'roi':round(dp/dc*100,1) if dc>0 else 0,
            'wr':round(dw/len(dd2)*100,1) if dd2 else 0,
            'contracts':dtc,'exposure':round(dc,2)}

    # Price buckets (combined, yes-side, no-side) with per-subgroup filtering
    def calc_pb(slist):
        bkts=defaultdict(lambda:{'yes_pnl':0,'yes_count':0,'no_pnl':0,'no_count':0})
        for s in slist:
            if s['nc']>0:
                b=(round(s['na']*100)//5)*5
                bkts[b]['no_pnl']+=s['pnl'] if s['direction']=='No' else 0
                bkts[b]['no_count']+=s['nc']
            if s['yc']>0:
                b=(round(s['ya']*100)//5)*5
                bkts[b]['yes_pnl']+=s['pnl'] if s['direction']=='Yes' else 0
                bkts[b]['yes_count']+=s['yc']
        out={}
        for b,v in bkts.items():
            out[b]={'yes_pnl':round(v['yes_pnl'],2),'yes_count':v['yes_count'],
                'no_pnl':round(v['no_pnl'],2),'no_count':v['no_count'],
                'combined_pnl':round(v['yes_pnl']+v['no_pnl'],2),
                'combined_count':v['yes_count']+v['no_count']}
        return out

    pb_all=calc_pb(settlements)
    pb_groups={sg:calc_pb([s for s in settlements if s['subgroup']==sg]) for sg in subgroups}

    # Trade stats
    t_n=0;t_contracts=0;t_notional=0;t_maker_pct=0
    if trades:
        t_n=len(trades); t_contracts=int(sum(t['contracts'] for t in trades))
        t_notional=round(sum(t['notional'] for t in trades),2)
        t_maker=sum(1 for t in trades if 'maker' in t['maker_taker'].lower())
        t_maker_pct=round(t_maker/t_n*100,0) if t_n else 0

    # Daily capital outlay = settlement cost basis grouped by event date (ticker-encoded),
    # falling back to the settlement day for tickers without an embedded date.
    eod=defaultdict(float)
    for s in settlements:
        k=s.get('event_str') or s.get('day_str')
        if k: eod[k]+=s['cost']
    daily_event_cost=[[k,round(v,2)] for k,v in sorted(eod.items())]

    # Price distribution (from trades)
    price_dist=[]
    if trades:
        pd_bkts=defaultdict(int)
        for t in trades: pd_bkts[(int(t['price_c'])//5)*5]+=1
        price_dist=sorted(pd_bkts.items())

    # Order stats
    o_n=0;o_fill_rate=0;o_contract_fr=0;o_cancel_rate=0;o_status={};o_total_placed=0;o_total_filled=0
    daily_fr=[]
    if orders:
        o_n=len(orders)
        o_total_placed=sum(o['total'] for o in orders)
        o_total_filled=sum(o['filled'] for o in orders)
        o_fill_rate=round(sum(1 for o in orders if o['filled']>0)/o_n*100,1) if o_n else 0
        o_contract_fr=round(o_total_filled/o_total_placed*100,1) if o_total_placed else 0
        sc=defaultdict(int)
        for o in orders:
            st=o['status']
            if st in ('Executed','Filled'): sc['Filled']+=1
            elif st=='Canceled': sc['Canceled']+=1
            elif st=='Placed': sc['Placed']+=1
            else: sc[st]+=1
        # partial fills
        partial=sum(1 for o in orders if o['filled']>0 and o['remaining']>0)
        if partial: sc['Partial']=partial
        o_cancel_rate=round(sc.get('Canceled',0)/o_n*100,1) if o_n else 0
        o_status=dict(sc)
        # daily fill rate
        od=defaultdict(lambda:{'n':0,'filled':0})
        for o in orders:
            if o['day_str']: od[o['day_str']]['n']+=1; od[o['day_str']]['filled']+=(1 if o['filled']>0 else 0)
        daily_fr=[[k,round(v['filled']/v['n']*100,1) if v['n'] else 0] for k,v in sorted(od.items())]

    # Subgroup × direction (for weather: city × direction table)
    sg_dir_stats={}
    for sg in subgroups:
        for d in ['Yes','No']:
            subs=[s for s in settlements if s['subgroup']==sg and s['direction']==d]
            if not subs: continue
            sp=sum(s['pnl'] for s in subs); sc=sum(s['cost'] for s in subs)
            sw=sum(1 for s in subs if s['pnl']>0)
            sg_dir_stats[(sg,d)]={'n':len(subs),'pnl':round(sp,2),'cost':round(sc,2),
                'roi':round(sp/sc*100,1) if sc>0 else 0,
                'wr':round(sw/len(subs)*100,1) if subs else 0,
                'contracts':sum(s['total_c'] for s in subs)}

    # Recent 50
    recent=sorted(settlements,key=lambda s:s['day_str'] or '',reverse=True)[:50]

    # Recent trend analysis (7d/30d)
    if dlabels:
        latest=datetime.fromisoformat(dlabels[-1])
        c7=(latest-timedelta(days=7)).isoformat()[:10]; c30=(latest-timedelta(days=30)).isoformat()[:10]
        pnl_7=sum(v['pnl'] for k,v in ds if k>c7)
        pnl_30=sum(v['pnl'] for k,v in ds if k>c30)
        days_7=sum(1 for k,_ in ds if k>c7)
    else: pnl_7=pnl_30=days_7=0

    return {
        'label':label,'n':n,'pnl':round(tp,2),'cost':round(tc,2),'roi':round(roi,1),
        'nw':nw,'nl':nl,'wr':round(wr,1),'aw':round(aw,2),'al':round(al,2),'pf':pf,'ev':round(ev,2),
        'nd':nd,'avg_d':round(avg_d,2),'std_d':round(std_d,2),
        'sharpe':round(sharpe,3),'sortino':round(sortino_v,3),'omega':round(omega,3),'mdd':round(mdd,2),
        'mdd_start':mdd_start,'mdd_end':mdd_end,
        'var95':var95,'best_day':round(best_day,2),'worst_day':round(worst_day,2),
        'cum_series':cum_series,'dd_series':dd_series,'daily_pnl':daily_pnl_series,
        'subgroups':subgroups,'sg_stats':sg_stats,'sg_cum':sg_cum,'sg_daily':sg_daily,
        'dir_stats':dir_stats,'sg_dir_stats':{f"{k[0]}|{k[1]}":v for k,v in sg_dir_stats.items()},
        'pb_all':pb_all,'pb_groups':pb_groups,
        't_n':t_n,'t_contracts':t_contracts,'t_notional':t_notional,'t_maker_pct':t_maker_pct,
        'daily_event_cost':daily_event_cost,'price_dist':price_dist,
        'o_n':o_n,'o_fill_rate':o_fill_rate,'o_contract_fr':o_contract_fr,'o_cancel_rate':o_cancel_rate,
        'o_status':o_status,'o_total_placed':o_total_placed,'o_total_filled':o_total_filled,
        'daily_fr':daily_fr,
        'total_contracts':sum(s['total_c'] for s in settlements),
        'pnl_7':round(pnl_7,2),'days_7':days_7,
        'recent':recent,
    }

def analyze_crypto_mm(trades, settlements):
    """Fills-based book for the crypto one-touch MM: avg-cost netting in Yes-space.
    A No fill at p is a Yes sale at 100-p, so round-trips realize P&L before
    settlement (the settlement CSV alone misses maker round-trip profit).
    Settled markets close any residual position at 0/100 on the settlement day."""
    ct=sorted([t for t in trades if t['family']=='Crypto' and t['contracts']>0],
        key=lambda t:t['date'] or datetime.min.replace(tzinfo=timezone.utc))
    if not ct: return None
    setl={}
    for s in settlements:
        if s['family']=='Crypto' and s['result'] in ('yes','no'):
            setl[s['ticker']]=(100.0 if s['result']=='yes' else 0.0, s['day_str'])
    books={}
    realized_events=[]  # (day_str, delta_dollars)
    for t in ct:
        tk=t['ticker']; q=t['contracts']
        yes_px=t['price_c'] if t['direction']=='Yes' else 100.0-t['price_c']
        delta=q if t['direction']=='Yes' else -q
        b=books.setdefault(tk,{'pos':0.0,'avg':0.0,'realized_c':0.0,'fills':0,
            'contracts':0,'notional':0.0,'fees':0.0,'last_day':None,'settled':False})
        b['fills']+=1; b['contracts']+=int(q); b['notional']+=t['notional']
        b['fees']+=t.get('fee',0.0); b['last_day']=t['day_str'] or b['last_day']
        pos,avg=b['pos'],b['avg']
        if pos==0 or (pos>0)==(delta>0):
            b['avg']=(abs(pos)*avg+abs(delta)*yes_px)/(abs(pos)+abs(delta))
            b['pos']=pos+delta
        else:
            closing=min(abs(delta),abs(pos))
            r=closing*(yes_px-avg)*(1 if pos>0 else -1)
            b['realized_c']+=r
            if t['day_str']: realized_events.append((t['day_str'],r/100.0))
            rem=abs(delta)-closing
            if rem>0: b['pos']=rem*(1 if delta>0 else -1); b['avg']=yes_px
            else:
                b['pos']=pos+delta
                if b['pos']==0: b['avg']=0.0
    for tk,b in books.items():
        if tk in setl and b['pos']!=0:
            spx,sday=setl[tk]
            r=b['pos']*(spx-b['avg'])
            b['realized_c']+=r
            realized_events.append((sday or b['last_day'],r/100.0))
            b['pos']=0.0; b['avg']=0.0
        if tk in setl: b['settled']=True

    total_fees=round(sum(b['fees'] for b in books.values()),2)
    realized=round(sum(b['realized_c'] for b in books.values())/100.0-total_fees,2)
    # open positions (side-space price for display)
    open_pos=[]
    for tk,b in sorted(books.items()):
        if abs(b['pos'])<1e-9: continue
        side='Yes' if b['pos']>0 else 'No'
        qty=int(round(abs(b['pos'])))
        px=b['avg'] if b['pos']>0 else 100.0-b['avg']
        open_pos.append({'ticker':tk,'asset':crypto_asset(tk),'side':side,'qty':qty,
            'avg_c':round(px,1),'cost':round(qty*px/100.0,2)})
    open_cost=round(sum(p['cost'] for p in open_pos),2)
    # per-asset rollup
    assets={}
    for tk,b in books.items():
        a=crypto_asset(tk)
        d=assets.setdefault(a,{'fills':0,'contracts':0,'notional':0.0,'realized':0.0,
            'open_qty':0,'open_cost':0.0,'markets':0})
        d['fills']+=b['fills']; d['contracts']+=b['contracts']; d['notional']+=b['notional']
        d['realized']+=b['realized_c']/100.0-b['fees']; d['markets']+=1
    for p in open_pos:
        assets[p['asset']]['open_qty']+=p['qty']; assets[p['asset']]['open_cost']+=p['cost']
    for a in assets.values():
        a['notional']=round(a['notional'],2); a['realized']=round(a['realized'],2)
        a['open_cost']=round(a['open_cost'],2)
    # daily notional stacked by asset
    dv=defaultdict(lambda:defaultdict(float))
    for t in ct:
        if t['day_str']: dv[crypto_asset(t['ticker'])][t['day_str']]+=t['notional']
    daily_vol={a:[[k,round(v,2)] for k,v in sorted(d.items())] for a,d in dv.items()}
    # cumulative realized
    rd=defaultdict(float)
    for day,r in realized_events:
        if day: rd[day]+=r
    c=0.0; cum_realized=[]
    for day in sorted(rd):
        c+=rd[day]; cum_realized.append([day,round(c,2)])
    recent=[{'day':t['day_str'],'ticker':t['ticker'],'dir':t['direction'],
        'qty':int(t['contracts']),'price_c':int(t['price_c']),'notional':round(t['notional'],2)}
        for t in ct[-30:]][::-1]
    maker=sum(1 for t in ct if 'maker' in t['maker_taker'].lower())
    return {'n_fills':len(ct),'contracts':int(sum(t['contracts'] for t in ct)),
        'notional':round(sum(t['notional'] for t in ct),2),
        'maker_pct':round(maker/len(ct)*100),'fees':total_fees,'realized':realized,
        'open_pos':open_pos,'open_cost':open_cost,'n_open':len(open_pos),
        'n_markets':len(books),'n_settled':sum(1 for b in books.values() if b['settled']),
        'assets':assets,'daily_vol':daily_vol,'cum_realized':cum_realized,'recent':recent,
        'first_day':ct[0]['day_str'],'last_day':ct[-1]['day_str']}

# ── Takeaway generation ──
def gen_takeaway(stats, sg_stats=None, is_family=False):
    s=stats; parts=[]
    color='#4ade80' if s['pnl']>=0 else '#f87171'
    word='Profitable' if s['pnl']>=0 else 'Unprofitable'
    parts.append(f"<strong>{word}: {money_s(s['pnl'])}</strong> ({s['roi']:+.1f}% ROI) across {s['n']} settlements.")
    if s['sharpe']>0: parts.append(f"Sharpe of {s['sharpe']:.1f} is {'solid' if s['sharpe']>1.5 else 'moderate' if s['sharpe']>0.8 else 'weak'}.")
    if s['mdd']>0:
        if s['mdd']>abs(s['pnl']): parts.append(f"Max DD (${s['mdd']:,.0f}) exceeds total profit — fragile.")
        else: parts.append(f"Max DD (${s['mdd']:,.0f}) is manageable relative to total P&L.")
    if s['days_7']>0 and s['pnl_7']!=0:
        d7_daily=s['pnl_7']/s['days_7'] if s['days_7'] else 0
        overall_daily=s['avg_d']
        if abs(d7_daily)>abs(overall_daily)*1.5:
            parts.append(f"Last 7 days are {'hot' if d7_daily>0 else 'cold'}: {money_s(s['pnl_7'])} (${d7_daily:.0f}/day vs ${overall_daily:.0f}/day overall).")
    if sg_stats and is_family:
        sorted_sg=sorted(sg_stats.items(),key=lambda x:-x[1]['pnl'])
        top=[f"{k} ({money_s(v['pnl'])})" for k,v in sorted_sg[:3] if v['pnl']>0]
        bot=[f"{k} ({money_s(v['pnl'])})" for k,v in sorted_sg[-3:] if v['pnl']<0]
        if top: parts.append(f"Top performers: {', '.join(top)}.")
        if bot: parts.append(f"Biggest drags: {', '.join(bot)}.")
    return ' '.join(parts)

def money_s(v):
    if v>=0: return f'+${v:,.2f}'
    return f'$-{abs(v):,.2f}'

def money_h(v, bold=True):
    c='#4ade80' if v>=0 else '#f87171'
    w='font-weight:600;' if bold else ''
    if v>=0: return f'<span style="color:{c};{w}">+${v:,.2f}</span>'
    return f'<span style="color:{c};{w}">${v:,.2f}</span>'

def pct_h(v):
    c='#4ade80' if v>=0 else '#f87171'
    return f'<span style="color:{c}">{v:+.1f}%</span>'

# ── HTML generation ──
def generate_html(consolidated, family_data, families, fill_data, all_settlements, all_trades, all_orders, out_path, label='Kalshi', crypto=None):
    # Crypto MM (fills-based) supersedes the standard settlement-based Crypto
    # family section: settlement-only stats can't see maker round-trips, and
    # settle-outs are already folded into the MM realized P&L. Crypto
    # settlements still count in the Consolidated section.
    sec_families=[f for f in families if f!='Crypto' or not crypto]
    nav_items=['Consolidated']+sec_families+(['Crypto MM'] if crypto else [])+['Fill Rates']
    nav_html=''.join(f"<a onclick=\"document.getElementById('section-{sid(f)}').scrollIntoView({{behavior:'smooth',block:'start'}})\">{f}</a>" for f in nav_items)

    # Build sections
    sections=[]

    # ── Consolidated ──
    c=consolidated
    sections.append(build_section('consolidated','Consolidated',c,is_consolidated=True,families=families,family_data=family_data))

    # ── Per-family ──
    for fam in sec_families:
        fd=family_data[fam]
        warn=f'<div class="wb">n={fd["nd"]} days — {"high estimation error" if fd["nd"]<30 else "moderate sample"}</div>' if fd['nd']<50 else ''
        sections.append(build_section(sid(fam),fam,fd,warn=warn,is_family=True))

    # ── Crypto MM (fills-based; settlements alone miss maker round-trips) ──
    if crypto:
        sections.append(build_crypto_section(crypto))

    # ── Fill rates ──
    sections.append(build_fill_section(fill_data, sec_families, family_data))

    # JS charts (consolidated pie still spans ALL families incl. Crypto)
    js=build_js(consolidated, family_data, families, fill_data, crypto=crypto, sec_families=sec_families)

    # 15-min meta refresh so a browser tab left open on the file picks up the
    # daily 7 AM rebuild (run_dashboards.ps1) without a manual F5.
    with open(out_path,'w',encoding='utf-8') as f:
        f.write(f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><meta http-equiv="refresh" content="900">
<title>Kalshi Trading Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{{--bg:#0f172a;--bg2:#1e293b;--bg3:#334155;--tx:#e2e8f0;--tx2:#94a3b8;--tx3:#64748b;--bd:#334155}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--bg);color:var(--tx);font-family:'Inter',-apple-system,sans-serif;padding:1.5rem;line-height:1.5}}
h1{{font-size:1.75rem;margin-bottom:.25rem}}h2{{font-size:1.15rem;color:var(--tx2);margin:1.5rem 0 .75rem;border-bottom:1px solid var(--bd);padding-bottom:.35rem}}
h3{{font-size:.95rem;color:var(--tx2);margin:1rem 0 .5rem}}h4{{font-size:.85rem;color:var(--tx3);margin:.5rem 0 .25rem;text-align:center}}
.sub{{color:var(--tx3);font-size:.85rem;margin-bottom:1.5rem}}
.g4{{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}}
.g3{{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(250px,1fr))}}
.g2{{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}}
.card{{background:var(--bg2);border:1px solid var(--bd);border-radius:.5rem;padding:1rem}}
.card .l{{font-size:.75rem;color:var(--tx3);text-transform:uppercase;letter-spacing:.05em}}
.card .v{{font-size:1.5rem;font-weight:700;margin-top:.25rem}}.card .s{{font-size:.8rem;color:var(--tx2);margin-top:.15rem}}
table{{width:100%;border-collapse:collapse;font-size:.8rem}}
th{{text-align:left;padding:.5rem;color:var(--tx3);border-bottom:1px solid var(--bd);font-weight:500;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em}}
td{{padding:.45rem .5rem;border-bottom:1px solid rgba(51,65,85,.4)}}tr:hover{{background:rgba(51,65,85,.3)}}
.bw{{display:inline-block;font-size:.6rem;padding:.1rem .35rem;border-radius:.25rem;margin-left:.25rem;background:rgba(251,191,36,.2);color:#fbbf24}}
.cc{{position:relative;height:280px;background:var(--bg2);border:1px solid var(--bd);border-radius:.5rem;padding:.75rem;margin-bottom:1rem}}
.cc.tall{{height:350px}}.wb{{background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.3);color:#fbbf24;padding:.5rem .75rem;border-radius:.35rem;font-size:.8rem;margin-bottom:1rem}}
.takeaway{{background:var(--bg2);border-left:3px solid #60a5fa;padding:.75rem 1rem;margin-bottom:1rem;font-size:.85rem;line-height:1.7;color:var(--tx2)}}.takeaway strong{{color:var(--tx)}}
code{{font-size:.75rem;background:var(--bg3);padding:.1rem .3rem;border-radius:.2rem}}.section{{margin-bottom:2.5rem;scroll-margin-top:60px}}.st{{overflow-x:auto}}
.sh{{font-size:1.4rem;color:var(--tx);border-bottom:2px solid #60a5fa;padding-bottom:.5rem;margin-top:2.5rem}}
.nav{{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1.5rem;position:sticky;top:0;z-index:10;background:var(--bg);padding:.75rem 0;border-bottom:1px solid var(--bd)}}
.nav a{{background:var(--bg2);border:1px solid var(--bd);color:var(--tx2);padding:.4rem .8rem;border-radius:.35rem;text-decoration:none;font-size:.8rem;cursor:pointer;transition:all .15s}}
.nav a:hover{{background:var(--bg3);color:var(--tx)}}
.dd{{background:var(--bg3);color:var(--tx);border:1px solid var(--bd);border-radius:.25rem;padding:.25rem .5rem;font-size:.8rem;margin-left:.5rem;cursor:pointer}}
@media(max-width:768px){{.g4,.g3,.g2{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>Kalshi Trading Analysis</h1>
<p class="sub">Generated {datetime.now(timezone.utc).strftime("%Y-%m-%d")} &bull; {c['n']:,} settlements &bull; {c['t_n']:,} trades &bull; {c['o_n']:,} orders</p>
<div class="nav">{nav_html}</div>
{"".join(sections)}
<script>
const C={{green:'#4ade80',red:'#f87171',blue:'#60a5fa',purple:'#c084fc',amber:'#fbbf24',cyan:'#22d3ee',teal:'#2dd4bf',pink:'#f472b6',orange:'#fb923c',slate:'#94a3b8'}};
const FC={json.dumps(FC_COLORS)};
const P={json.dumps(PALETTE)};
Chart.defaults.color='#94a3b8';Chart.defaults.borderColor='rgba(51,65,85,0.5)';Chart.defaults.font.size=11;
function fadeHex(hex,alpha){{
  if(typeof hex!=='string')return hex;
  var h=hex.replace('#','');
  if(h.length===8)h=h.slice(0,6);
  if(h.length===3)h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
  var r=parseInt(h.slice(0,2),16),g=parseInt(h.slice(2,4),16),b=parseInt(h.slice(4,6),16);
  return 'rgba('+r+','+g+','+b+','+alpha+')';
}}
Chart.register({{id:'legendHighlight',beforeInit(chart){{
  var leg=chart.options.plugins.legend;if(!leg||leg.display===false)return;
  leg.onHover=function(e,item,legend){{
    var ci=item.datasetIndex;
    chart.data.datasets.forEach(function(ds,i){{
      if(!ds._origBorderColor)ds._origBorderColor=ds.borderColor;
      if(!ds._origBgColor)ds._origBgColor=ds.backgroundColor;
      if(!ds._origBorderWidth)ds._origBorderWidth=ds.borderWidth;
      if(i!==ci){{
        ds.borderColor=fadeHex(ds._origBorderColor,0.12);
        ds.backgroundColor=fadeHex(ds._origBgColor,0.05);
        ds.borderWidth=1;
      }}else{{
        ds.borderColor=ds._origBorderColor;
        ds.backgroundColor=ds._origBgColor;
        ds.borderWidth=(ds._origBorderWidth||1.5)+1;
      }}
    }});chart.canvas.style.cursor='pointer';chart.update('none');
  }};
  leg.onLeave=function(e,item,legend){{
    chart.data.datasets.forEach(function(ds){{
      if(ds._origBorderColor)ds.borderColor=ds._origBorderColor;
      if(ds._origBgColor)ds.backgroundColor=ds._origBgColor;
      if(ds._origBorderWidth)ds.borderWidth=ds._origBorderWidth;
    }});chart.canvas.style.cursor='default';chart.update('none');
  }};
}}}});
function ML(id,labels,datasets,opts={{}}){{var el=document.getElementById(id);if(!el)return;new Chart(el,{{type:'line',data:{{labels,datasets}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:datasets.length>1,position:'top',labels:{{boxWidth:8,padding:4,font:{{size:9}}}}}}}},scales:{{x:{{ticks:{{maxTicksLimit:12,maxRotation:45}}}},y:{{...opts.yOpts}}}},elements:{{point:{{radius:0,hoverRadius:4}},line:{{tension:.3}}}},interaction:{{intersect:false,mode:'index'}}}}}});}}
function MB(id,labels,data){{var el=document.getElementById(id);if(!el)return;new Chart(el,{{type:'bar',data:{{labels,datasets:[{{data,backgroundColor:data.map(v=>v>=0?C.green+'99':C.red+'99'),borderColor:data.map(v=>v>=0?C.green:C.red),borderWidth:1}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{ticks:{{maxTicksLimit:20,maxRotation:45}}}}}}}}}});}}
var PB_DATA={{}};var PB_CHARTS={{}};
function renderPB(sid,pb){{
    var bks=Object.keys(pb).map(Number).sort((a,b)=>a-b);
    if(!bks.length)return;
    var lbl=bks.map(b=>b+'-'+(b+4)+'¢');
    ['c','y','n'].forEach(function(t){{
        var cid=sid+'_pb_'+t;
        if(PB_CHARTS[cid]){{PB_CHARTS[cid].destroy();}}
        var key=t==='c'?'combined_pnl':t==='y'?'yes_pnl':'no_pnl';
        var vals=bks.map(b=>pb[b]?pb[b][key]||0:0);
        var el=document.getElementById(cid);if(!el)return;
        PB_CHARTS[cid]=new Chart(el,{{type:'bar',data:{{labels:lbl,datasets:[{{data:vals,
            backgroundColor:vals.map(v=>v>=0?C.green+'99':C.red+'99'),
            borderColor:vals.map(v=>v>=0?C.green:C.red),borderWidth:1}}]}},
            options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},
            scales:{{x:{{ticks:{{maxTicksLimit:20,maxRotation:45}}}}}}}}}});
    }});
}}
function updatePB(sid){{
    var sel=document.getElementById(sid+'_pb_sel');
    if(!sel||!PB_DATA[sid])return;
    var v=sel.value;
    if(v==='__all__'){{renderPB(sid,PB_DATA[sid].all);}}
    else{{var gd=PB_DATA[sid].groups[v];if(gd)renderPB(sid,gd);else renderPB(sid,{{}});}}
}}
{js}
</script></body></html>''')
    print(f"Dashboard written to {out_path}")

def sid(name):
    return re.sub(r'[^a-z0-9]','',name.lower())

def build_section(section_id, title, s, warn='', is_consolidated=False, is_family=False, families=None, family_data=None):
    takeaway=gen_takeaway(s, s.get('sg_stats'), is_family=is_family)
    bw=lambda n: f' <span class="bw">n={n}</span>' if n<30 else ''

    # KPIs
    kpis=f'''<div class="g4">
<div class="card"><div class="l">Net P&L</div><div class="v">{money_h(s['pnl'])}</div><div class="s">ROI: {pct_h(s['roi'])}</div></div>
<div class="card"><div class="l">Win Rate</div><div class="v">{s['wr']:.1f}%</div><div class="s">{s['nw']}W/{s['nl']}L</div></div>
<div class="card"><div class="l">Sharpe</div><div class="v">{s['sharpe']:.3f}</div><div class="s">Sortino: {s['sortino']:.3f} &middot; &Omega;: {s['omega']:.3f}</div></div>
<div class="card"><div class="l">Max DD</div><div class="v" style="color:#f87171">${s['mdd']:,.2f}</div><div class="s">{s.get('mdd_start','?')}→{s.get('mdd_end','?')}</div></div>
<div class="card"><div class="l">Profit Factor</div><div class="v">{s['pf']}</div><div class="s">W:{money_h(s['aw'])} L:{money_h(s['al'])}</div></div>
<div class="card"><div class="l">EV/Settlement</div><div class="v">{money_h(s['ev'])}</div><div class="s">n={s['n']}</div></div>
<div class="card"><div class="l">Avg Daily/σ</div><div class="v">{money_h(s['avg_d'])}</div><div class="s">σ=${s['std_d']:,.2f} | {s['nd']}d</div></div>
<div class="card"><div class="l">VaR(95%)</div><div class="v">{money_h(s['var95'])}</div><div class="s">Best:{money_h(s['best_day'])} Worst:{money_h(s['worst_day'])}</div></div>'''
    if s.get('t_n',0)>0:
        kpis+=f'\n<div class="card"><div class="l">Dollar Volume</div><div class="v">${s["t_notional"]:,.0f}</div><div class="s">{s["t_contracts"]:,} contracts | {s["t_n"]:,} trades ({s["t_maker_pct"]:.0f}% mkr)</div></div>'
    if s.get('o_n',0)>0:
        kpis+=f'\n<div class="card"><div class="l">Fill Rate</div><div class="v">{s["o_fill_rate"]:.1f}%</div><div class="s">{s["o_total_filled"]:,}/{s["o_n"]:,} orders</div></div>'
    kpis+='</div>'

    # Charts (canvas placeholders)
    sid_v=section_id
    charts=f'''<div class="g2" style="margin-top:1rem">
<div><h3>Cumulative P&L</h3><div class="cc"><canvas id="{sid_v}_cum"></canvas></div></div>
<div><h3>Drawdown</h3><div class="cc"><canvas id="{sid_v}_dd"></canvas></div></div></div>
<h3>Cumulative P&L by {"Strategy Family" if is_consolidated else "City" if s.get('subgroups') else "Sub-group"}</h3><div class="cc tall"><canvas id="{sid_v}_gc"></canvas></div>
<div class="g2">
<div><h3>Daily P&L</h3><div class="cc"><canvas id="{sid_v}_dp"></canvas></div></div>
<div><h3>{"Exposure by Family" if is_consolidated else "Daily Capital Outlay (event date)"}</h3><div class="cc"><canvas id="{sid_v}_{'pie' if is_consolidated else 'dv'}"></canvas></div></div></div>'''
    if not is_consolidated:
        charts+=f'''<div class="g2">
<div><h3>Daily P&L by {"City" if is_family else "Sub-group"}</h3><div class="cc"><canvas id="{sid_v}_gd"></canvas></div></div>
<div><h3>Cumulative Capital Outlay</h3><div class="cc"><canvas id="{sid_v}_cdv"></canvas></div></div></div>'''
    else:
        charts+=f'''<div class="g2">
<div><h3>Daily Capital Outlay (event date)</h3><div class="cc"><canvas id="{sid_v}_vol"></canvas></div></div>
<div><h3>Price Distribution</h3><div class="cc"><canvas id="{sid_v}_pd"></canvas></div></div></div>'''

    # Price bucket dropdown
    sg_options='<option value="__all__">All</option>'+''.join(f'<option value="{sg}">{sg}</option>' for sg in s.get('subgroups',[]))
    charts+=f'''<h3>P&L by Price Bucket (5¢ bins) <select class="dd" id="{sid_v}_pb_sel" onchange="updatePB('{sid_v}')">{sg_options}</select></h3>
<div class="g3">
<div><h4>Combined</h4><div class="cc"><canvas id="{sid_v}_pb_c"></canvas></div></div>
<div><h4>Yes Side</h4><div class="cc"><canvas id="{sid_v}_pb_y"></canvas></div></div>
<div><h4>No Side</h4><div class="cc"><canvas id="{sid_v}_pb_n"></canvas></div></div></div>'''

    # Tables
    # Strategy family / subgroup table
    sg=s.get('sg_stats',{})
    sg_label='Strategy Family' if is_consolidated else 'City' if is_family else 'Sub-group'
    sg_sorted=sorted(sg.items(),key=lambda x:-x[1]['pnl'])
    sg_rows=''.join(f'<tr><td><code>{k}</code></td><td>{money_h(v["pnl"])}</td><td>{pct_h(v["roi"])}</td><td>{v["n"]}{bw(v["n"])}</td><td>{pct_h(v["wr"])}</td><td>{v["contracts"]:,}</td><td>${v["ppc"]:.4f}</td></tr>' for k,v in sg_sorted)
    tables=f'''<h3>P&L by {sg_label}</h3><div class="st"><table><thead><tr><th>{sg_label}</th><th>P&L</th><th>ROI</th><th>n</th><th>WR</th><th>Contracts</th><th>P&L/C</th></tr></thead><tbody>{sg_rows}</tbody></table></div>'''

    # Direction table
    dir_rows=''.join(f'<tr><td>{d}</td><td>{money_h(v["pnl"])}</td><td>{pct_h(v["roi"])}</td><td>{v["n"]}</td><td>{pct_h(v["wr"])}</td><td>{v["contracts"]:,}</td><td>${v["exposure"]:,.2f}</td></tr>' for d,v in s.get('dir_stats',{}).items())
    tables+=f'<h3>P&L by Direction</h3><div class="st"><table><thead><tr><th>Side</th><th>P&L</th><th>ROI</th><th>n</th><th>WR</th><th>Contracts</th><th>Exposure</th></tr></thead><tbody>{dir_rows}</tbody></table></div>'

    # Subgroup × direction (for family sections)
    if is_family and s.get('sg_dir_stats'):
        sgd_rows=''
        for sg_name in [k for k,_ in sg_sorted]:
            for d in ['Yes','No']:
                key=f"{sg_name}|{d}"
                if key not in s['sg_dir_stats']: continue
                v=s['sg_dir_stats'][key]
                sgd_rows+=f'<tr><td><code>{sg_name}</code></td><td>{d}</td><td>{money_h(v["pnl"])}</td><td>{pct_h(v["roi"])}</td><td>{v["n"]}{bw(v["n"])}</td><td>{pct_h(v["wr"])}</td><td>{v["contracts"]:,}</td></tr>'
        if sgd_rows:
            tables+=f'<h3>P&L by {sg_label} × Direction</h3><div class="st"><table><thead><tr><th>{sg_label}</th><th>Side</th><th>P&L</th><th>ROI</th><th>n</th><th>WR</th><th>Contracts</th></tr></thead><tbody>{sgd_rows}</tbody></table></div>'

    # Recent settlements
    rec_rows=''.join(f'<tr><td>{r["day_str"]}</td><td><code>{r["ticker"]}</code></td><td><code>{r["code"]}</code></td><td>{r["subgroup"]}</td><td>{r["direction"]}</td><td>${r["cost"]:.2f}</td><td>{money_h(r["pnl"])}</td></tr>' for r in s.get('recent',[]))
    tables+=f'<h3>Recent Settlements (last 50)</h3><div class="st"><table><thead><tr><th>Date</th><th>Ticker</th><th>Code</th><th>Teams/City</th><th>Dir</th><th>Cost</th><th>P&L</th></tr></thead><tbody>{rec_rows}</tbody></table></div>'

    return f'''<div class="section" id="section-{section_id}">
<h2 class="sh">{title}</h2>
{warn}
<div class="takeaway">{takeaway}</div>
{kpis}
{charts}
{tables}
</div>'''

def build_fill_section(fill_data, families, family_data):
    fd=fill_data
    status_cards=''.join(f'<div class="card"><div class="l">{k}</div><div class="v">{v:,}</div></div>' for k,v in sorted(fd['status'].items(),key=lambda x:-x[1]))

    fam_rows=''
    for fam in families:
        f2=family_data[fam]
        fam_rows+=f'<tr><td>{fam}</td><td>{f2["o_fill_rate"]:.1f}%</td><td>{f2["o_contract_fr"]:.1f}%</td><td>{f2["o_n"]:,}</td><td>{f2["o_total_filled"]:,}</td><td>{f2["o_total_filled"]:,}/{f2["o_total_placed"]:,}</td></tr>'

    return f'''<div class="section" id="section-fillrates">
<h2 class="sh">Fill Rates & Order Analysis</h2>
<div class="g4">
<div class="card"><div class="l">Order Fill Rate</div><div class="v">{fd['fill_rate']:.1f}%</div><div class="s">{fd['filled_orders']:,}/{fd['n']:,}</div></div>
<div class="card"><div class="l">Contract Fill Rate</div><div class="v">{fd['contract_fr']:.1f}%</div><div class="s">{fd['total_filled']:,}/{fd['total_placed']:,}</div></div>
<div class="card"><div class="l">Cancel Rate</div><div class="v">{fd['cancel_rate']:.1f}%</div></div>
<div class="card"><div class="l">Total Orders</div><div class="v">{fd['n']:,}</div></div></div>
<div class="g4" style="margin-top:1rem">{status_cards}</div>
<h3>Daily Fill Rate (Orders)</h3><div class="cc"><canvas id="f_dr"></canvas></div>
<h3>Fill Rates by Family</h3>
<div class="st"><table><thead><tr><th>Family</th><th>Fill Rate (Orders)</th><th>Fill Rate (Contracts)</th><th>Total Orders</th><th>Filled Orders</th><th>Filled/Ordered Contracts</th></tr></thead><tbody>{fam_rows}</tbody></table></div>
</div>'''

def build_crypto_section(cr):
    """Crypto one-touch MM section — fills-based (settlement CSV misses round-trips)."""
    open_note=f"{cr['n_open']} open" + (f", {cr['n_settled']} settled" if cr['n_settled'] else '')
    kpis=f'''<div class="g4">
<div class="card"><div class="l">Realized P&L (fills)</div><div class="v">{money_h(cr['realized'])}</div><div class="s">round-trips + settle-outs, net of fees</div></div>
<div class="card"><div class="l">Open Exposure</div><div class="v">${cr['open_cost']:,.2f}</div><div class="s">at cost &middot; {cr['n_open']} positions</div></div>
<div class="card"><div class="l">Dollar Volume</div><div class="v">${cr['notional']:,.0f}</div><div class="s">{cr['contracts']:,} contracts | {cr['n_fills']:,} fills ({cr['maker_pct']}% mkr)</div></div>
<div class="card"><div class="l">Markets</div><div class="v">{cr['n_markets']}</div><div class="s">{open_note}</div></div>
<div class="card"><div class="l">Fees</div><div class="v">${cr['fees']:,.2f}</div><div class="s">{cr['first_day']} → {cr['last_day']}</div></div>
</div>'''
    charts='''<div class="g2" style="margin-top:1rem">
<div><h3>Daily $ Volume by Asset</h3><div class="cc"><canvas id="cryptomm_vol"></canvas></div></div>
<div><h3>Cumulative Realized P&L</h3><div class="cc"><canvas id="cryptomm_cum"></canvas></div></div></div>
<h3>Realized P&L by Asset</h3><div class="cc"><canvas id="cryptomm_ra"></canvas></div>'''
    asset_rows=''.join(f'<tr><td><code>{a}</code></td><td>{money_h(v["realized"])}</td><td>{v["fills"]:,}</td><td>{v["contracts"]:,}</td><td>${v["notional"]:,.2f}</td><td>{v["markets"]}</td><td>{v["open_qty"]:,}</td><td>${v["open_cost"]:,.2f}</td></tr>'
        for a,v in sorted(cr['assets'].items(),key=lambda x:-x[1]['realized']))
    tables=f'''<h3>By Asset</h3><div class="st"><table><thead><tr><th>Asset</th><th>Realized P&L</th><th>Fills</th><th>Contracts</th><th>Volume</th><th>Markets</th><th>Open Qty</th><th>Open Cost</th></tr></thead><tbody>{asset_rows}</tbody></table></div>'''
    op_rows=''.join(f'<tr><td><code>{p["ticker"]}</code></td><td>{p["asset"]}</td><td>{p["side"]}</td><td>{p["qty"]:,}</td><td>{p["avg_c"]:.1f}&cent;</td><td>${p["cost"]:,.2f}</td></tr>'
        for p in sorted(cr['open_pos'],key=lambda x:-x['cost']))
    tables+=f'''<h3>Open Positions ({cr['n_open']}) — at cost, no mark</h3><div class="st"><table><thead><tr><th>Ticker</th><th>Asset</th><th>Side</th><th>Qty</th><th>Avg Price</th><th>Cost</th></tr></thead><tbody>{op_rows}</tbody></table></div>'''
    rec_rows=''.join(f'<tr><td>{r["day"]}</td><td><code>{r["ticker"]}</code></td><td>{r["dir"]}</td><td>{r["qty"]:,}</td><td>{r["price_c"]}&cent;</td><td>${r["notional"]:,.2f}</td></tr>'
        for r in cr['recent'])
    tables+=f'''<h3>Recent Fills (last 30)</h3><div class="st"><table><thead><tr><th>Date</th><th>Ticker</th><th>Dir</th><th>Qty</th><th>Price</th><th>Notional</th></tr></thead><tbody>{rec_rows}</tbody></table></div>'''
    takeaway=(f"<strong>{'Profitable' if cr['realized']>=0 else 'Unprofitable'} so far: {money_s(cr['realized'])} realized</strong> from fills "
        f"across {cr['n_markets']} markets ({cr['n_fills']} fills, ${cr['notional']:,.0f} volume). "
        f"${cr['open_cost']:,.2f} still at risk in {cr['n_open']} open positions — monthly one-touch markets settle at month-end, "
        f"so realized P&L here comes from maker round-trips{' and settled markets' if cr['n_settled'] else ' only'}.")
    return f'''<div class="section" id="section-cryptomm">
<h2 class="sh">Crypto MM</h2>
<div class="takeaway">{takeaway}</div>
{kpis}
{charts}
{tables}
</div>'''

def build_js(consolidated, family_data, families, fill_data, crypto=None, sec_families=None):
    """Build all chart initialization JS."""
    if sec_families is None: sec_families=families
    js_parts=[]

    def add_charts(sid_v, s, is_consolidated=False):
        # Cumulative
        js_parts.append(f"var {sid_v}_c={json.dumps(s['cum_series'])};if({sid_v}_c.length)ML('{sid_v}_cum',{sid_v}_c.map(d=>d[0]),[{{label:'Cum P&L',data:{sid_v}_c.map(d=>d[1]),borderColor:C.green,backgroundColor:C.green+'22',fill:true,borderWidth:2}}]);")
        # DD
        js_parts.append(f"var {sid_v}_d={json.dumps(s['dd_series'])};if({sid_v}_d.length)ML('{sid_v}_dd',{sid_v}_d.map(d=>d[0]),[{{label:'DD',data:{sid_v}_d.map(d=>d[1]),borderColor:C.red,backgroundColor:C.red+'22',fill:true,borderWidth:1.5}}]);")
        # Daily
        js_parts.append(f"var {sid_v}_dp={json.dumps(s['daily_pnl'])};if({sid_v}_dp.length)MB('{sid_v}_dp',{sid_v}_dp.map(d=>d[0]),{sid_v}_dp.map(d=>d[1]));")
        # Cum by subgroup
        gc_data=json.dumps(s['sg_cum'])
        if is_consolidated:
            # Use family colors
            js_parts.append(f"var {sid_v}_fc={gc_data};var afd=[...new Set(Object.values({sid_v}_fc).flatMap(s=>s.map(d=>d[0])))].sort();if(afd.length){{var ds=Object.entries({sid_v}_fc).map(([k,s])=>{{var lu=Object.fromEntries(s),l=0;return{{label:k,data:afd.map(d=>{{var v=lu[d];if(v!==undefined)l=v;return l;}}),borderColor:FC[k]||'#6b7280',borderWidth:1.5,fill:false}}}});ML('{sid_v}_gc',afd,ds);}}")
        else:
            js_parts.append(f"var {sid_v}_gc2={gc_data};var ad=[...new Set(Object.values({sid_v}_gc2).flatMap(s=>s.map(d=>d[0])))].sort();if(ad.length){{var ds=Object.entries({sid_v}_gc2).map(([k,s],i)=>{{var lu=Object.fromEntries(s),l=0;return{{label:k,data:ad.map(d=>{{var v=lu[d];if(v!==undefined)l=v;return l;}}),borderColor:P[i%P.length],borderWidth:1.5,fill:false}}}});ML('{sid_v}_gc',ad,ds);}}")

        if is_consolidated:
            # Exposure pie
            exp_data=[[fam, round(family_data[fam]['cost'],2)] for fam in families]
            js_parts.append(f"var exp={json.dumps(exp_data)};if(exp.length)new Chart(document.getElementById('{sid_v}_pie'),{{type:'doughnut',data:{{labels:exp.map(d=>d[0]),datasets:[{{data:exp.map(d=>d[1]),backgroundColor:exp.map(d=>FC[d[0]]||'#6b7280'),borderWidth:0}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:'right',labels:{{boxWidth:10,padding:6}}}}}}}}}});")
            # Daily capital outlay (event-date cost basis)
            if s.get('daily_event_cost'):
                js_parts.append(f"var vol={json.dumps(s['daily_event_cost'])};if(vol.length){{MB('{sid_v}_vol',vol.map(d=>d[0]),vol.map(d=>d[1]));var vc=Chart.getChart('{sid_v}_vol');vc.data.datasets[0].backgroundColor=C.teal+'77';vc.data.datasets[0].borderColor=C.teal;vc.update();}}")
            # Price dist
            if s.get('price_dist'):
                pd=[[b,c] for b,c in s['price_dist']]
                js_parts.append(f"var pd={json.dumps(pd)};if(pd.length)new Chart(document.getElementById('{sid_v}_pd'),{{type:'bar',data:{{labels:pd.map(d=>d[0]+'-'+(d[0]+4)+'¢'),datasets:[{{data:pd.map(d=>d[1]),backgroundColor:C.purple+'88',borderColor:C.purple,borderWidth:1}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}}}}}});")
        else:
            # Daily capital outlay (event-date cost basis)
            if s.get('daily_event_cost'):
                js_parts.append(f"var {sid_v}_dv={json.dumps(s['daily_event_cost'])};if({sid_v}_dv.length){{MB('{sid_v}_dv',{sid_v}_dv.map(d=>d[0]),{sid_v}_dv.map(d=>d[1]));var dvc=Chart.getChart('{sid_v}_dv');dvc.data.datasets[0].backgroundColor=C.teal+'77';dvc.data.datasets[0].borderColor=C.teal;dvc.update();}}")
            # Cumulative capital outlay
            if s.get('daily_event_cost'):
                js_parts.append(f"if({sid_v}_dv.length){{var cv=0,cvd={sid_v}_dv.map(d=>{{cv+=d[1];return[d[0],Math.round(cv*100)/100];}});ML('{sid_v}_cdv',cvd.map(d=>d[0]),[{{label:'Cum Outlay',data:cvd.map(d=>d[1]),borderColor:C.teal,backgroundColor:C.teal+'22',fill:true,borderWidth:2}}]);}}")
            # Daily by subgroup (stacked)
            gd_data=json.dumps(s['sg_daily'])
            js_parts.append(f"var {sid_v}_gd2={gd_data};var add=[...new Set(Object.values({sid_v}_gd2).flatMap(s=>s.map(d=>d[0])))].sort();if(add.length){{var ds2=Object.entries({sid_v}_gd2).map(([k,s],i)=>{{var lu=Object.fromEntries(s);return{{label:k,data:add.map(d=>lu[d]||0),backgroundColor:P[i%P.length]+'88',borderColor:P[i%P.length],borderWidth:1}}}});new Chart(document.getElementById('{sid_v}_gd'),{{type:'bar',data:{{labels:add,datasets:ds2}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:true,position:'top',labels:{{boxWidth:8,padding:4,font:{{size:9}}}}}}}},scales:{{x:{{stacked:true,ticks:{{maxTicksLimit:12,maxRotation:45}}}},y:{{stacked:true}}}}}}}});}}")

        # Price buckets
        pb_json=json.dumps({'all':s['pb_all'],'groups':s['pb_groups']})
        js_parts.append(f"PB_DATA['{sid_v}']={pb_json};renderPB('{sid_v}',PB_DATA['{sid_v}'].all);")

    # Consolidated
    add_charts('consolidated', consolidated, is_consolidated=True)

    # Per-family
    for fam in sec_families:
        fid=sid(fam)
        add_charts(fid, family_data[fam])

    # Crypto MM charts
    if crypto:
        js_parts.append(f"var cr_dv={json.dumps(crypto['daily_vol'])};var crd=[...new Set(Object.values(cr_dv).flatMap(s=>s.map(d=>d[0])))].sort();if(crd.length){{var crds=Object.entries(cr_dv).map(([k,s],i)=>{{var lu=Object.fromEntries(s);return{{label:k,data:crd.map(d=>lu[d]||0),backgroundColor:P[i%P.length]+'88',borderColor:P[i%P.length],borderWidth:1}}}});new Chart(document.getElementById('cryptomm_vol'),{{type:'bar',data:{{labels:crd,datasets:crds}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:true,position:'top',labels:{{boxWidth:8,padding:4,font:{{size:9}}}}}}}},scales:{{x:{{stacked:true,ticks:{{maxTicksLimit:12,maxRotation:45}}}},y:{{stacked:true}}}}}}}});}}")
        js_parts.append(f"var cr_cum={json.dumps(crypto['cum_realized'])};if(cr_cum.length)ML('cryptomm_cum',cr_cum.map(d=>d[0]),[{{label:'Realized P&L',data:cr_cum.map(d=>d[1]),borderColor:FC['Crypto'],backgroundColor:FC['Crypto']+'22',fill:true,borderWidth:2}}]);")
        ra=[[a,v['realized']] for a,v in sorted(crypto['assets'].items(),key=lambda x:-x[1]['realized'])]
        js_parts.append(f"var cr_ra={json.dumps(ra)};if(cr_ra.length)MB('cryptomm_ra',cr_ra.map(d=>d[0]),cr_ra.map(d=>d[1]));")

    # Fill rate daily chart
    if fill_data.get('daily_fr'):
        dfr=json.dumps(fill_data['daily_fr'])
        js_parts.append(f"var dfr={dfr};if(dfr.length)ML('f_dr',dfr.map(d=>d[0]),[{{label:'Fill Rate (%)',data:dfr.map(d=>d[1]),borderColor:C.amber,borderWidth:1.5,fill:false}}],{{yOpts:{{min:0,max:100}}}});")

    return '\n'.join(js_parts)


# ── Main ──
def main():
    if len(sys.argv)<4:
        print("Usage: python3 analyze.py <settlement.csv|none> <trade.csv|none> <order.csv|none> [label] [output.html]")
        print("Or set DASHBOARD_OUT env var for output path.")
        sys.exit(1)

    raw_s=load_csv(sys.argv[1]); raw_t=load_csv(sys.argv[2]); raw_o=load_csv(sys.argv[3])
    label=sys.argv[4] if len(sys.argv)>4 else 'Kalshi'
    out=sys.argv[5] if len(sys.argv)>5 else os.environ.get('DASHBOARD_OUT','kalshi_report.html')

    print(f"Loading CSVs... {len(raw_s)} settlements, {len(raw_t)} trades, {len(raw_o)} orders")
    settlements=process_settlements(raw_s)
    trades=process_trades(raw_t)
    orders=process_orders(raw_o)
    print(f"Active: {len(settlements)} settlements, {len(trades)} trades, {len(orders)} orders")

    if not settlements:
        print("No active settlements"); sys.exit(1)

    # Detect families
    families_set=sorted(set(s['family'] for s in settlements))

    # Consolidated analysis
    consolidated=analyze_group(settlements, trades, orders, 'Consolidated')

    # Per-family analysis
    family_data={}
    for fam in families_set:
        fs=[s for s in settlements if s['family']==fam]
        ft=[t for t in trades if t['family']==fam] if trades else []
        fo=[o for o in orders if o['family']==fam] if orders else []
        family_data[fam]=analyze_group(fs, ft, fo, fam)

    # Fill rate data (consolidated)
    fill_data={'n':len(orders),'fill_rate':0,'contract_fr':0,'cancel_rate':0,
        'status':{},'total_placed':0,'total_filled':0,'filled_orders':0,'daily_fr':[]}
    if orders:
        fill_data['total_placed']=sum(o['total'] for o in orders)
        fill_data['total_filled']=sum(o['filled'] for o in orders)
        fill_data['filled_orders']=sum(1 for o in orders if o['filled']>0)
        fill_data['fill_rate']=round(fill_data['filled_orders']/len(orders)*100,1) if orders else 0
        fill_data['contract_fr']=round(fill_data['total_filled']/fill_data['total_placed']*100,1) if fill_data['total_placed'] else 0
        sc=defaultdict(int)
        for o in orders:
            st=o['status']
            if st in ('Executed',): sc['Filled']+=1
            elif st=='Canceled': sc['Canceled']+=1
            elif st=='Placed': sc['Placed']+=1
            else: sc[st]+=1
        partial=sum(1 for o in orders if o['filled']>0 and o['remaining']>0)
        if partial: sc['Partial']=partial
        fill_data['cancel_rate']=round(sc.get('Canceled',0)/len(orders)*100,1) if orders else 0
        fill_data['status']=dict(sc)
        od=defaultdict(lambda:{'n':0,'f':0})
        for o in orders:
            if o['day_str']: od[o['day_str']]['n']+=1; od[o['day_str']]['f']+=(1 if o['filled']>0 else 0)
        fill_data['daily_fr']=[[k,round(v['f']/v['n']*100,1) if v['n'] else 0] for k,v in sorted(od.items())]

    # Crypto MM view (fills-based; independent of settlements)
    crypto=analyze_crypto_mm(trades, settlements)

    generate_html(consolidated, family_data, families_set, fill_data, settlements, trades, orders, out, label, crypto=crypto)

    # Summary
    print(f"\n=== {label} SUMMARY ===")
    print(f"Net P&L: {money_s(consolidated['pnl'])} | ROI: {consolidated['roi']}% | Sharpe: {consolidated['sharpe']}")
    print(f"Max DD: ${consolidated['mdd']:,.2f} ({consolidated['mdd_start']} to {consolidated['mdd_end']})")
    for fam in families_set:
        f2=family_data[fam]
        print(f"  {fam}: {money_s(f2['pnl'])} | ROI: {f2['roi']}% | n={f2['n']}")
    if crypto:
        print(f"  Crypto MM (fills): {money_s(crypto['realized'])} realized | ${crypto['open_cost']:,.2f} open | {crypto['n_fills']} fills")

if __name__=='__main__':
    main()
