"""上影反攻 + SAR起爆 深度优化 — 止盈止损×公式变体"""
import sys,os;sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np,json
from core.connector import TdxConnector;from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
TdxConnector.ensure_connected()

START='20250101';END='20260607'
codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(codes,'20241101',END,dividend_type='front',period='1d')
C=k['Close'].sort_index();H=k['High'].sort_index();L=k['Low'].sort_index();O=k['Open'].sort_index();V=k.get('Volume',pd.DataFrame()).sort_index()
valid=C.notna().sum()>30
for d in[C,H,L,O,V]:d=d.loc[:,valid]
univ=[c for c in C.columns if'ST'not in c and'*ST'not in c]

# Market filter
idx_k=DataFetcher.get_kline(['999999.SH'],'20240101',END,dividend_type='none',period='1d')
idxC=idx_k['Close'].sort_index().iloc[:,0];idxMA60=idxC.rolling(60).mean()
idxUp5=(idxMA60>idxMA60.shift(1))&(idxMA60.shift(1)>idxMA60.shift(2))&(idxMA60.shift(2)>idxMA60.shift(3))&(idxMA60.shift(3)>idxMA60.shift(4))&(idxMA60.shift(4)>idxMA60.shift(5))
market_on=idxUp5.reindex(C.index,method='ffill').fillna(False).astype(bool)
market_pct=market_on.sum()/len(market_on)*100
print(f'Market on: {market_pct:.0f}%')

ENG={'initial_capital':1000000.0,'commission':0.0003,'slippage':0.001,'period':'1d','position_sizing':{'min_buy_amount':2000.0,'max_buy_amount':10000.0,'lot_size':100,'min_lots':1}}

# === PRECOMPUTE SIGNALS ===
MA5=C.rolling(5).mean();MA10=C.rolling(10).mean();MA20=C.rolling(20).mean();MA60=C.rolling(60).mean()

# 上影反攻
us=H-np.maximum(C,O);bd=abs(C-O)
s1_base=(us>bd*2)&(C>O)&(V>V.rolling(5).mean())&(C>C.shift(1))
# 上影反攻变体
s1_plus=s1_base&(C>MA20)  # +MA20确认
s1_vol=s1_base&(V>V.rolling(5).mean()*1.3)  # 放量1.3倍
s1_both=s1_base&(C>MA20)&(V>V.rolling(5).mean()*1.3)  # 双重确认

# SAR起爆
sar=pd.DataFrame(np.nan,index=H.index,columns=H.columns)
for col in H.columns:
    hi=H[col].values;lo=L[col].values;s=np.full(len(hi),np.nan);is_up=True;ep=hi[0];af=0.02
    for i in range(1,len(hi)):
        if is_up:s[i]=max(s[i-1]if not np.isnan(s[i-1])else lo[i-1],lo[i-1]);s[i]+=(ep-s[i])*af;s[i]=min(s[i],lo[i-1],lo[i-2]if i>1 else lo[0])
        else:s[i]=min(s[i-1]if not np.isnan(s[i-1])else hi[i-1],hi[i-1]);s[i]-=(s[i]-ep)*af;s[i]=max(s[i],hi[i-1],hi[i-2]if i>1 else hi[0])
        if np.isnan(s[i]):s[i]=s[i-1]if i>0 else lo[i]
        if is_up and hi[i]>ep:ep=hi[i];af=min(af+0.02,0.2)
        elif not is_up and lo[i]<ep:ep=lo[i];af=min(af+0.02,0.2)
        if is_up and lo[i]<s[i]:is_up=False;ep=lo[i];af=0.02
        elif not is_up and hi[i]>s[i]:is_up=True;ep=hi[i];af=0.02
    sar[col]=s
s2_base=(C>sar)&(C.shift(1)<=sar.shift(1))&(C>MA20)&(V>V.rolling(5).mean()*1.2)
s2_plus=s2_base&(MA20>MA20.shift(1))  # MA20向上
s2_vol=s2_base&(V>V.rolling(5).mean()*1.5)  # 放量1.5倍
s2_both=s2_base&(MA20>MA20.shift(1))&(V>V.rolling(5).mean()*1.5)

# === STOP CONFIGS ===
STOPS=[
    ("默认",-0.08,0.05,0.03,[(0.05,0.30),(0.12,0.30)],20),
    ("TD3%",-0.08,0.12,0.03,[(0.10,0.30),(0.25,0.30)],30),
    ("紧C",-0.05,0.05,0.03,[(0.05,0.50)],10),
    ("宽D",-0.15,0.08,0.05,[(0.06,0.30),(0.15,0.30)],25),
    ("大波B",-0.12,0.10,0.06,[(0.08,0.30),(0.20,0.30)],30),
    ("三档E",-0.10,0.08,0.04,[(0.04,0.20),(0.10,0.20),(0.18,0.20)],20),
    ("高盈G",-0.08,0.12,0.08,[(0.10,0.30),(0.25,0.30)],30),
    ("TD4%",-0.08,0.12,0.04,[(0.10,0.30),(0.25,0.30)],30),
    ("TD2%",-0.08,0.15,0.02,[(0.10,0.30),(0.25,0.30)],40),
    ("CS6%",-0.06,0.08,0.05,[(0.08,0.30),(0.20,0.30)],20),
    ("MH40d",-0.08,0.10,0.05,[(0.06,0.30),(0.15,0.30)],40),
    ("快进F",-0.04,0.04,0.02,[(0.04,0.50),(0.08,0.50)],10),
]

def run_bt(sig,title,name,cost_thr,trail_act,trail_dd,lad,hd):
    sig_bt=sig.loc[START:];mo=market_on.loc[sig_bt.index];sig_bt=sig_bt.copy()
    for col in sig_bt.columns:sig_bt.loc[~mo,col]=False
    ts=int(sig_bt.sum().sum())
    if ts<50:return None
    recs=[]
    for col in univ:
        if col not in sig_bt.columns:continue
        for idx in sig_bt.index[sig_bt[col]]:recs.append({'stock_code':col,'select_date':idx.strftime('%Y-%m-%d')})
    sel=pd.DataFrame(recs);common=sorted(set(C.columns)&set(sel['stock_code'].unique()))
    if len(common)<10:return None
    cs=C[common].ffill().bfill();hs=H.reindex(index=cs.index,columns=common).ffill().bfill();ls=L.reindex(index=cs.index,columns=common).ffill().bfill()
    entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
    for _,row in sel.iterrows():
        code=row['stock_code'];dt=pd.Timestamp(row['select_date'])
        if code not in entries.columns:continue
        if dt in entries.index:
            entries.loc[dt,code]=True
        else:
            mask=entries.index>=dt
            if mask.any():
                entries.loc[entries.index[mask][0],code]=True
    STP={'cost_stop':{'enabled':True,'threshold':cost_thr},'trailing_stop':{'enabled':True,'activation':trail_act,'drawdown':trail_dd},'ladder_tp':{'enabled':True,'levels':[{'profit':p,'sell_ratio':r}for p,r in lad]},'time_stop':{'enabled':True,'max_hold_days':hd},'cond_time_stop':{'enabled':True,'days':min(7,hd-1),'profit':0.02}}
    lp=np.array([p for p,_ in lad],dtype=np.float64);lr=np.array([r for _,r in lad],dtype=np.float64)
    engine=BacktestEngine(ENG)
    brs=engine.run_cached(cs,entries,hs.values.astype(np.float64),ls.values.astype(np.float64),STP,sel,lp,lr,len(lad),skip_sm=True)
    met=brs['metrics']
    return {'sig':title,'stop':name,'cum':brs['cumulative_return']*100,'ann':met['annualized_return']*100,'dd':met['max_drawdown']*100,'sr':met['sharpe_ratio'],'wr':met['win_rate']*100,'t':len(brs['trades']),'signals':ts}

# === RUN ALL ===
all_sigs=[
    ("上影反攻:基础",s1_base),("上影反攻:+MA20",s1_plus),("上影反攻:+放量1.3x",s1_vol),("上影反攻:+MA20+放量",s1_both),
    ("SAR起爆:基础",s2_base),("SAR起爆:+MA20向上",s2_plus),("SAR起爆:+放量1.5x",s2_vol),("SAR起爆:+MA20+放量",s2_both),
]

results=[]
for stitle,sig in all_sigs:
    print(f'\n-- {stitle} --')
    for sn,cost_thr,trail_act,trail_dd,lad,hd in STOPS:
        r=run_bt(sig,stitle,sn,cost_thr,trail_act,trail_dd,lad,hd)
        if r:results.append(r)
        if r and r['ann']>10:print(f"  [{sn:<10}] Cum:{r['cum']:+.2f}% Ann:{r['ann']:+.2f}% DD:{r['dd']:.2f}% SR:{r['sr']:.2f} WR:{r['wr']:.1f}% T:{r['t']}")

# Rank
results.sort(key=lambda x:x['ann'],reverse=True)
print('\n'+'='*80)
print(f'  TOP 20 | {START}~{END} | Market:{market_pct:.0f}%')
print('='*80)
print("%-4s %-30s %-10s %7s %7s %7s %5s %5s %5s" % ("#","sig","stop","Cum%","Ann%","DD%","SR","WR%","T"))
print('  '+'-'*80)
for i,r in enumerate(results[:20],1):
    sig=r["sig"][:28];st=r["stop"];cu=r["cum"];an=r["ann"];dd=r["dd"];sr=r["sr"];wr=r["wr"];tr=r["t"]
    print("%-4d %-30s %-10s %+7.2f %+7.2f %+7.2f %5.2f %5.1f %5d" % (i,sig,st,cu,an,dd,sr,wr,tr))

# Best per strategy
print('\n--- Best per signal variant ---')
seen=set()
for r in results:
    key=r['sig']
    sig=r["sig"][:30];st=r["stop"];an=r["ann"];dd=r["dd"];sr=r["sr"]
    if key not in seen:seen.add(key);print("%-32s %-10s Ann:%+.2f%% DD:%+.2f%% SR:%.2f" % (sig,st,an,dd,sr))

with open('output/optimize_top2_final.json','w',encoding='utf-8')as f:json.dump(results,f,ensure_ascii=False,indent=2,default=str)
print('\nSaved: output/optimize_top2_final.json')
