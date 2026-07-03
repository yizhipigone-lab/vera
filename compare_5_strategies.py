"""5策略对比回测 — 100万 | 2022.01.01~今 | ± 指数MA60上行5日过滤"""
import sys,os;sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np,json
from core.connector import TdxConnector;from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
TdxConnector.ensure_connected()

START='20220101';END='20260607';CAPITAL=1000000.0

# ═══ 1. Data ═══
print("="*80);print("  5策略对比回测 | 100万 | 2022.01~今");print("="*80)
print("\n[1] Loading data...",flush=True)

# Stocks
codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(codes,'20211201',END,dividend_type='front',period='1d')
C=k['Close'].sort_index();H=k['High'].sort_index();L=k['Low'].sort_index();O=k['Open'].sort_index();V=k.get('Volume',pd.DataFrame()).sort_index()
valid=C.notna().sum()>100
for d in[C,H,L,O,V]:d=d.loc[:,valid]
univ=[c for c in C.columns if'ST'not in c and'*ST'not in c]

# Index
idx_k=DataFetcher.get_kline(['999999.SH'],'20201201',END,dividend_type='none',period='1d')
idxC=idx_k['Close'].sort_index().iloc[:,0]
idxMA60=idxC.rolling(60).mean()
idxUp5=(idxMA60>idxMA60.shift(1))&(idxMA60.shift(1)>idxMA60.shift(2))&(idxMA60.shift(2)>idxMA60.shift(3))&(idxMA60.shift(3)>idxMA60.shift(4))&(idxMA60.shift(4)>idxMA60.shift(5))
market_on=idxUp5.reindex(C.index,method='ffill').fillna(False).astype(bool)
print(f"  Stocks:{C.shape} | Universe:{len(univ)} | Market:100万 | Index filter:{market_on.sum()/len(market_on)*100:.0f}% on")

# ═══ 2. Pre-compute indicators ═══
print("\n[2] Computing indicators...",flush=True)
MA5=C.rolling(5).mean();MA10=C.rolling(10).mean();MA20=C.rolling(20).mean();MA60=C.rolling(60).mean()
EMA60=C.ewm(span=60,adjust=False).mean()

# PSAR for SAR strategy
print("  PSAR...",flush=True)
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

# ═══ 3. Signals ═══
print("\n[3] Generating signals...",flush=True)
signals={}

# SAR起爆
s1=(C>sar)&(C.shift(1)<=sar.shift(1))&(C>MA20)&(V>V.rolling(5).mean()*1.2)
signals["SAR起爆"]=s1

# 上影反攻
us=H-np.maximum(C,O);bd=abs(C-O)
signals["上影反攻"]=(us>bd*2)&(C>O)&(V>V.rolling(5).mean())&(C>C.shift(1))

# N字冷剑封喉
signals["冷剑封喉"]=(C>C.shift(1))&(C.shift(1)<C.shift(2))&(V>V.rolling(5).mean()*1.5)&(C>MA10)&(C.shift(1)<MA10.shift(1))

# 筹码集中
signals["筹码集中"]=(C/EMA60>0.92)&(C/EMA60<1.20)&(C>MA5)&(MA5>MA10)&(MA10>MA20)&(C>C.shift(1))&(V>V.rolling(5).mean()*1.3)

# 智能能量
V10b=V.rolling(10).mean();deg=np.arctan((V10b/V10b.shift(1)-1)*100)*180/np.pi
signals["智能能量"]=(deg>75)&(V/V.shift(1)>1.5)&(C>C.shift(1))&(C>MA20)

for n,s in signals.items():print(f"  {n}: {s.sum().sum():,} raw signals")

# ═══ 4. Stop configs ═══
ENG={'initial_capital':CAPITAL,'commission':0.0003,'slippage':0.001,'period':'1d','position_sizing':{'min_buy_amount':2000.0,'max_buy_amount':10000.0,'lot_size':100,'min_lots':1}}

stops={
    "SAR起爆":{'cost_stop':{'enabled':True,'threshold':-0.08},'trailing_stop':{'enabled':True,'activation':0.12,'drawdown':0.03},'ladder_tp':{'enabled':True,'levels':[{'profit':0.10,'sell_ratio':0.3},{'profit':0.25,'sell_ratio':0.3}]},'time_stop':{'enabled':True,'max_hold_days':30},'cond_time_stop':{'enabled':True,'days':7,'profit':0.02}},
    "上影反攻":{'cost_stop':{'enabled':True,'threshold':-0.08},'trailing_stop':{'enabled':True,'activation':0.05,'drawdown':0.03},'ladder_tp':{'enabled':True,'levels':[{'profit':0.05,'sell_ratio':0.3},{'profit':0.12,'sell_ratio':0.3}]},'time_stop':{'enabled':True,'max_hold_days':20},'cond_time_stop':{'enabled':True,'days':7,'profit':0.02}},
    "冷剑封喉":{'cost_stop':{'enabled':True,'threshold':-0.05},'trailing_stop':{'enabled':True,'activation':0.05,'drawdown':0.03},'ladder_tp':{'enabled':True,'levels':[{'profit':0.05,'sell_ratio':0.5}]},'time_stop':{'enabled':True,'max_hold_days':10},'cond_time_stop':{'enabled':True,'days':5,'profit':0.02}},
    "筹码集中":{'cost_stop':{'enabled':True,'threshold':-0.08},'trailing_stop':{'enabled':True,'activation':0.05,'drawdown':0.03},'ladder_tp':{'enabled':True,'levels':[{'profit':0.05,'sell_ratio':0.3},{'profit':0.12,'sell_ratio':0.3}]},'time_stop':{'enabled':True,'max_hold_days':20},'cond_time_stop':{'enabled':True,'days':7,'profit':0.02}},
    "智能能量":{'cost_stop':{'enabled':True,'threshold':-0.08},'trailing_stop':{'enabled':True,'activation':0.05,'drawdown':0.03},'ladder_tp':{'enabled':True,'levels':[{'profit':0.05,'sell_ratio':0.3},{'profit':0.12,'sell_ratio':0.3}]},'time_stop':{'enabled':True,'max_hold_days':20},'cond_time_stop':{'enabled':True,'days':7,'profit':0.02}},
}

ladder_lens={"SAR起爆":2,"上影反攻":2,"冷剑封喉":1,"筹码集中":2,"智能能量":2}

# ═══ 5. Backtest with/without market filter ═══
print("\n[4] Backtesting...",flush=True)
results=[]

for sname,sig in signals.items():
    for use_filter in[False,True]:
        label=f"{sname}{'(+市场过滤)'if use_filter else'(原始)'}"
        sig_w=sig.loc[START:END]
        if use_filter:
            mo=market_on.loc[START:END]
            for col in sig_w.columns:sig_w.loc[~mo,col]=False
        ts=int(sig_w.sum().sum())
        print(f"\n  [{label}] signals={ts}",flush=True)
        if ts<50:print(f"    信号不足");continue

        recs=[]
        for col in univ:
            if col not in sig_w.columns:continue
            for idx in sig_w.index[sig_w[col]]:recs.append({'stock_code':col,'select_date':idx.strftime('%Y-%m-%d')})
        sel=pd.DataFrame(recs)
        common=sorted(set(C.columns)&set(sel['stock_code'].unique()))
        cs=C[common].ffill().bfill();hs=H.reindex(index=cs.index,columns=common).ffill().bfill();ls=L.reindex(index=cs.index,columns=common).ffill().bfill()
        entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
        for _,row in sel.iterrows():
            code,dt=row['stock_code'],pd.Timestamp(row['select_date'])
            if code not in entries.columns:continue
            if dt in entries.index:entries.loc[dt,code]=True
            else:
                mask=entries.index>=dt
                if mask.any():entries.loc[entries.index[mask][0],code]=True

        sp=stops[sname];nl=ladder_lens[sname]
        lp=np.array([lv['profit']for lv in sp['ladder_tp']['levels']],dtype=np.float64)
        lr=np.array([lv['sell_ratio']for lv in sp['ladder_tp']['levels']],dtype=np.float64)

        engine=BacktestEngine(ENG)
        result=engine.run_cached(cs,entries,hs.values.astype(np.float64),ls.values.astype(np.float64),sp,sel,lp,lr,nl,skip_sm=True)
        met=result['metrics'];t=result['trades']
        t['entry_date']=pd.to_datetime(t['entry_date']);t['exit_date']=pd.to_datetime(t['exit_date'])
        t['year']=t['exit_date'].dt.year

        # Yearly breakdown
        yearly={}
        cape=CAPITAL
        for y in sorted(t['year'].unique()):
            yt=t[t['year']==y];p=yt['pnl'].sum();cnt=len(yt);w=len(yt[yt['pnl']>0])
            wr=w/cnt*100 if cnt>0 else 0;ret=p/cape*100;cape+=p
            yearly[int(y)]={'pnl':p,'trades':cnt,'wr':wr,'ret':ret,'eq':cape}

        r={'label':label,'cum':result['cumulative_return'],'ann':met['annualized_return'],'dd':met['max_drawdown'],'sr':met['sharpe_ratio'],'wr':met['win_rate'],'trades':len(t),'signals':ts,'yearly':yearly}
        results.append(r)
        print(f"    Cum:{r['cum']*100:+.2f}% Ann:{r['ann']*100:+.2f}% DD:{r['dd']*100:.2f}% SR:{r['sr']:.2f} WR:{r['wr']*100:.1f}% T:{r['trades']}",flush=True)

# ═══ 6. Comparison ═══
print("\n"+"="*80)
print("  FINAL COMPARISON | 1,000,000 | 2022.01.01 ~ 2026.06.07")
print("="*80)

# Sort by annual return
sorted_by_ann=sorted(results,key=lambda x:x['ann']if x['ann']is not None else-999,reverse=True)
print(f"\n  {'='*30} 按年化收益排名 {'='*30}")
print(f"  {'策略':<30s} {'累计':>8} {'年化':>8} {'回撤':>8} {'夏普':>6} {'胜率':>6} {'交易':>6} {'信号':>8}")
print("  "+"-"*85)
for r in sorted_by_ann:
    print(f"  {r['label']:<30s} {r['cum']*100:>+8.2f}% {r['ann']*100:>+8.2f}% {r['dd']*100:>+8.2f}% {r['sr']:>6.2f} {r['wr']*100:>6.1f}% {r['trades']:>6} {r['signals']:>8}")

# Sort by Sharpe (stability)
sorted_by_sr=sorted(results,key=lambda x:x['sr']if x['sr']is not None else-999,reverse=True)
print(f"\n  {'='*30} 按稳定性(夏普)排名 {'='*30}")
print(f"  {'策略':<30s} {'累计':>8} {'年化':>8} {'回撤':>8} {'夏普':>6} {'胜率':>6} {'交易':>6}")
print("  "+"-"*75)
for r in sorted_by_sr:
    print(f"  {r['label']:<30s} {r['cum']*100:>+8.2f}% {r['ann']*100:>+8.2f}% {r['dd']*100:>+8.2f}% {r['sr']:>6.2f} {r['wr']*100:>6.1f}% {r['trades']:>6}")

# Yearly breakdown for top 3
print(f"\n  {'='*30} 年度明细(TOP3) {'='*30}")
for r in sorted_by_ann[:3]:
    print(f"\n  [{r['label']}]")
    print(f"  {'Year':<8} {'PnL':>14} {'Ret':>10} {'WR':>8} {'Equity':>14}")
    print("  "+"-"*55)
    for y,yd in sorted(r['yearly'].items()):
        print(f"  {y:<8} {yd['pnl']:>+14,.0f} {yd['ret']:>+9.2f}% {yd['wr']:>7.1f}% {yd['eq']:>14,.0f}")

with open('output/compare_5_strategies.json','w',encoding='utf-8')as f:json.dump(results,f,ensure_ascii=False,indent=2,default=str)
print("\n  Saved: output/compare_5_strategies.json")
