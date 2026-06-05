"""优化 TOP6 公式止盈止损参数"""
import sys,os;sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np,json
from core.connector import TdxConnector;from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine

TdxConnector.ensure_connected()
codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(codes,'20250701','20260605',dividend_type="front",period="1d")
C=k["Close"].sort_index();H=k["High"].sort_index();L=k["Low"].sort_index()
O=k["Open"].sort_index();V=k.get("Volume",pd.DataFrame()).sort_index()
valid=C.notna().sum()>30
for d in[C,H,L,O,V]:d=d.loc[:,valid]
univ=[c for c in C.columns if"ST"not in c and"*ST"not in c]
ENG={"initial_capital":200000.0,"commission":0.0003,"slippage":0.001,"period":"1d","position_sizing":{"min_buy_amount":2000.0,"max_buy_amount":10000.0,"lot_size":100,"min_lots":1}}

MA5=C.rolling(5).mean();MA10=C.rolling(10).mean();MA20=C.rolling(20).mean();MA60=C.rolling(60).mean()

# Pre-compute all 6 signal sets
print("Computing signals...",flush=True)
signals={}

def psar(high,low):
    result=pd.DataFrame(np.nan,index=high.index,columns=high.columns)
    for col in high.columns:
        hi=high[col].values;lo=low[col].values
        sar=np.full(len(hi),np.nan);is_up=True;ep=hi[0];af=0.02;s=lo[0]
        for i in range(1,len(hi)):
            if is_up:s=max(s,lo[i-1]);s+=(ep-s)*af;s=min(s,lo[i-1],lo[i-2]if i>1 else lo[0])
            else:s=min(s,hi[i-1]);s-=(s-ep)*af;s=max(s,hi[i-1],hi[i-2]if i>1 else hi[0])
            sar[i]=s
            if is_up and hi[i]>ep:ep=hi[i];af=min(af+0.02,0.2)
            elif not is_up and lo[i]<ep:ep=lo[i];af=min(af+0.02,0.2)
            if(is_up and lo[i]<sar[i]):is_up=False;ep=lo[i];af=0.02;s=hi[i]
            elif(not is_up and hi[i]>sar[i]):is_up=True;ep=hi[i];af=0.02;s=lo[i]
        result[col]=sar
    return result

# F1: 一骑红尘
sig1=(C/C.shift(1)-1)>0.06;sig1=sig1&(V>V.rolling(20).mean()*2)
signals["F1.一骑红尘"]=sig1

# F2: SAR起爆
ps=psar(H,L);sig2=(C>ps)&(C.shift(1)<=ps.shift(1))&(C>MA20)&(V>V.rolling(5).mean()*1.2)
signals["F2.SAR起爆"]=sig2

# F3: 一阳三穿
sig3=(C>MA5)&(C>MA10)&(C>MA20)&(C>O)&(C.shift(1)<MA5.shift(1))&(V>V.rolling(5).mean()*1.5)
signals["F3.一阳三穿"]=sig3

# F4: 上影反攻
us=H-np.maximum(C,O);bd=abs(C-O);sig4=(us>bd*2)&(C>O)&(V>V.rolling(5).mean())&(C>C.shift(1))
signals["F4.上影反攻"]=sig4

# F5: N字冷剑封喉
sig5=(C>C.shift(1))&(C.shift(1)<C.shift(2))&(V>V.rolling(5).mean()*1.5)&(C>MA10)&(C.shift(1)<MA10.shift(1))
signals["F5.N字冷剑封喉"]=sig5

# F6: 主力买盘
buy_vol=V*((C-L)-(H-C))/(H-L+0.0001);buy_vol=buy_vol.clip(lower=0)
net_buy=buy_vol.rolling(5).sum()/V.rolling(5).sum()
sig6=(net_buy>0.6)&(C>C.shift(1))&(C>MA5)&(V>V.rolling(5).mean())
signals["F6.主力买盘"]=sig6

# Stop configs to try (6 configs per formula)
STOP_CONFIGS=[
    ("默认",-0.08,0.05,0.03,[(0.05,0.3),(0.12,0.3)],20),
    ("激进A",-0.06,0.03,0.02,[(0.03,0.3),(0.08,0.3),(0.15,0.3)],15),
    ("大波段B",-0.12,0.10,0.06,[(0.08,0.3),(0.20,0.3)],30),
    ("紧止损C",-0.05,0.05,0.03,[(0.05,0.5)],10),
    ("宽止损D",-0.15,0.08,0.05,[(0.06,0.3),(0.15,0.3)],25),
    ("三档E",-0.10,0.08,0.04,[(0.04,0.2),(0.10,0.2),(0.18,0.2)],20),
    ("快进快出F",-0.04,0.04,0.02,[(0.04,0.5),(0.10,0.5)],10),
    ("高盈亏G",-0.08,0.12,0.08,[(0.10,0.3),(0.25,0.3)],30),
]

def run_bt_sig(sig_df,stop_name,cost_thr,trail_act,trail_dd,lad,hd):
    sig=sig_df.loc["20250801":];ts=int(sig.sum().sum())
    if ts<10:return None
    recs=[]
    for col in univ:
        if col not in sig.columns:continue
        for idx in sig.index[sig[col]]:recs.append({"stock_code":col,"select_date":idx.strftime("%Y-%m-%d")})
    sel=pd.DataFrame(recs)
    common=sorted(set(C.columns)&set(sel["stock_code"].unique()))
    if len(common)<10:return None
    cs=C[common].ffill().bfill();hs=H.reindex(index=cs.index,columns=common).ffill().bfill();ls=L.reindex(index=cs.index,columns=common).ffill().bfill()
    entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
    for _,row in sel.iterrows():
        code,dt=row["stock_code"],pd.to_datetime(row["select_date"])
        if code not in entries.columns:continue
        if dt in entries.index:entries.loc[dt,code]=True
        else:
            mask=entries.index>=dt
            if mask.any():entries.loc[entries.index[mask][0],code]=True
    STP={"cost_stop":{"enabled":True,"threshold":cost_thr},"trailing_stop":{"enabled":True,"activation":trail_act,"drawdown":trail_dd},"ladder_tp":{"enabled":True,"levels":[{"profit":p,"sell_ratio":r}for p,r in lad]},"time_stop":{"enabled":True,"max_hold_days":hd},"cond_time_stop":{"enabled":True,"days":min(7,hd-1),"profit":0.02}}
    lp=np.array([p for p,_ in lad],dtype=np.float64);lr=np.array([r for _,r in lad],dtype=np.float64)
    engine=BacktestEngine(ENG)
    result=engine.run_cached(cs,entries,hs.values.astype(np.float64),ls.values.astype(np.float64),STP,sel,lp,lr,len(lad),skip_sm=True)
    met=result["metrics"]
    return {"stop":stop_name,"cum":result["cumulative_return"]*100,"ann":met["annualized_return"]*100,"dd":met["max_drawdown"]*100,"sr":met["sharpe_ratio"],"wr":met["win_rate"]*100,"t":len(result["trades"])}

print("\nOptimizing...",flush=True)
all_results={}

for sname,sig_df in signals.items():
    print(f"\n--- {sname} ---")
    best=None
    for sc in STOP_CONFIGS:
        r=run_bt_sig(sig_df,*sc)
        if r is None:continue
        r["fname"]=sname
        if best is None or r["ann"]>best["ann"]:best=r
        print(f"  [{sc[0]:<12s}] Cum:{r['cum']:+.2f}% Ann:{r['ann']:+.2f}% DD:{r['dd']:.2f}% SR:{r['sr']:.2f} WR:{r['wr']:.1f}% T:{r['t']}")
    if best:
        if sname not in all_results:all_results[sname]=[]
        all_results[sname].append(best)
        print(f"  >>> BEST: {best['stop']} Ann:{best['ann']:+.2f}%")

# Final comparison table
print("\n"+"="*80)
print("  TOP 6 优化结果对比 (原始 vs 最优)")
print("="*80)
print(f"  {'公式':<20} {'原始Ann%':>9} {'最优Ann%':>9} {'最优配置':<12} {'DD%':>7} {'SR':>5}")
print("  "+"-"*65)
# original baselines
baselines={"F1.一骑红尘":26.81,"F2.SAR起爆":24.08,"F3.一阳三穿":17.41,"F4.上影反攻":15.16,"F5.N字冷剑封喉":10.72,"F6.主力买盘":9.18}
for sname in signals:
    bl=baselines.get(sname,0)
    best_list=all_results.get(sname,[])
    if best_list:
        best=best_list[0]
        imp=best["ann"]-bl
        print(f"  {sname:<20} {bl:>+9.2f}% {best['ann']:>+9.2f}% {best['stop']:<12s} {best['dd']:>+7.2f}% {best['sr']:>5.2f}  {'+'if imp>0 else ''}{imp:+.1f}%")
