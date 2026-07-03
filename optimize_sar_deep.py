"""SAR起爆深度止盈止损优化 — 扩展网格搜索目标20%年化"""
import sys,os;sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np,json,time
from core.connector import TdxConnector;from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from utils.logger import get_logger
logger = get_logger(__name__)

TdxConnector.ensure_connected()
codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(codes,'20241201','20260605',dividend_type="front",period="1d")
C=k["Close"].sort_index();H=k["High"].sort_index();L=k["Low"].sort_index();O=k["Open"].sort_index();V=k.get("Volume",pd.DataFrame()).sort_index()
valid=C.notna().sum()>30
for d in[C,H,L,O,V]:d=d.loc[:,valid]
univ=[c for c in C.columns if"ST"not in c and"*ST"not in c]
ENG={"initial_capital":200000.0,"commission":0.0003,"slippage":0.001,"period":"1d","position_sizing":{"min_buy_amount":2000.0,"max_buy_amount":10000.0,"lot_size":100,"min_lots":1}}

# Pre-compute SAR signal
MA20=C.rolling(20).mean()
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
sig=(C>sar)&(C.shift(1)<=sar.shift(1))&(C>MA20)&(V>V.rolling(5).mean()*1.2)
sig_bt=sig.loc["20250101":"20260531"]

# Build selections once
recs=[];t0=time.time()
print("Building signals...",flush=True)
for col in univ:
    if col not in sig_bt.columns:continue
    for idx in sig_bt.index[sig_bt[col]]:recs.append({"stock_code":col,"select_date":idx.strftime("%Y-%m-%d")})
sel=pd.DataFrame(recs)
print(f"  {len(sel)} selections",flush=True)

# Extended stop config grid
CONFIGS=[]
# cost_thr, trail_act, trail_dd, ladder_levels, max_hold
# Ladder: (profit, sell_ratio) pairs

# A: Vary cost stop
for cs in[-0.05,-0.06,-0.07,-0.08,-0.10,-0.12,-0.15]:
    CONFIGS.append((f"CS{cs:.0%}",cs,0.12,0.08,[(0.10,0.3),(0.25,0.3)],30))

# B: Vary trailing activation
for ta in[0.05,0.06,0.08,0.10,0.12,0.15,0.18,0.20]:
    CONFIGS.append((f"TA{ta:.0%}",-0.08,ta,0.08,[(0.10,0.3),(0.25,0.3)],30))

# C: Vary trailing drawdown
for td in[0.03,0.04,0.05,0.06,0.08,0.10,0.12]:
    CONFIGS.append((f"TD{td:.0%}",-0.08,0.12,td,[(0.10,0.3),(0.25,0.3)],30))

# D: Vary max hold
for mh in[10,15,20,25,30,40,50]:
    CONFIGS.append((f"MH{mh}d",-0.08,0.12,0.08,[(0.10,0.3),(0.25,0.3)],mh))

# E: Ladder combinations
ladder_grid=[
    ("L1:5:25+15:25",0.05,0.25,0.15,0.25),
    ("L2:8:30+20:30",0.08,0.30,0.20,0.30),
    ("L3:6:30+12:30+20:30",0.06,0.30,0.12,0.30,0.20,0.30),
    ("L4:10:50",0.10,0.50),
    ("L5:15:50",0.15,0.50),
    ("L6:8:40+18:40",0.08,0.40,0.18,0.40),
    ("L7:5:20+10:20+20:20+30:20",0.05,0.20,0.10,0.20,0.20,0.20,0.30,0.20),
    ("L8:3:30+6:30+9:30",0.03,0.30,0.06,0.30,0.09,0.30),
    ("L9:12:30+25:30+40:30",0.12,0.30,0.25,0.30,0.40,0.30),
    ("L10:单5:100",0.05,1.0),
]
for lname,*largs in ladder_grid:
    levels=[(largs[i],largs[i+1])for i in range(0,len(largs),2)]
    CONFIGS.append((lname,-0.08,0.12,0.08,levels,30))

# F: Hybrid combos (cost+trailing+ladder co-optimized)
hybrids=[
    ("H1:激进紧",-0.05,0.08,0.04,[(0.05,0.3),(0.12,0.3),(0.20,0.3)],15),
    ("H2:大波段宽",-0.15,0.15,0.10,[(0.10,0.3),(0.25,0.3)],40),
    ("H3:快进快出",-0.04,0.05,0.03,[(0.04,0.5),(0.10,0.5)],10),
    ("H4:中位平衡",-0.08,0.10,0.05,[(0.06,0.25),(0.15,0.25),(0.25,0.25)],25),
    ("H5:超宽不割",-0.20,0.20,0.12,[(0.15,0.3),(0.30,0.3)],50),
    ("H6:保守微利",-0.06,0.06,0.03,[(0.03,0.3),(0.06,0.3),(0.10,0.3)],20),
    ("H7:极端紧",-0.03,0.04,0.02,[(0.03,0.5),(0.08,0.5)],10),
    ("H8:趋势跟随",-0.10,0.15,0.10,[(0.08,0.2),(0.20,0.2),(0.35,0.2)],45),
]
CONFIGS.extend(hybrids)

# G: No-stop baselines
CONFIGS.append(("G1:纯阶梯",-0.08,0.12,0.08,[(0.05,0.25),(0.12,0.25),(0.20,0.25),(0.30,0.25)],30))
CONFIGS.append(("G2:无止损仅止盈",-0.99,0.08,0.05,[(0.10,0.3),(0.25,0.3)],30))

# Unique names
seen=set();unique_cfgs=[]
for name,*args in CONFIGS:
    if name not in seen:seen.add(name);unique_cfgs.append((name,*args))
CONFIGS=unique_cfgs

print(f"\nTesting {len(CONFIGS)} stop configs...\n",flush=True)

def run_one(name,cost_thr,trail_act,trail_dd,ladder_lv,max_hold):
    try:
        common=sorted(set(C.columns)&set(sel["stock_code"].unique()))
        cs=C[common].ffill().bfill();hs=H.reindex(index=cs.index,columns=common).ffill().bfill();ls=L.reindex(index=cs.index,columns=common).ffill().bfill()
        entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
        for _,row in sel.iterrows():
            code,dt=row["stock_code"],pd.Timestamp(row["select_date"])
            if code not in entries.columns:continue
            if dt in entries.index:entries.loc[dt,code]=True
            else:
                mask=entries.index>=dt
                if mask.any():entries.loc[entries.index[mask][0],code]=True
        STP={"cost_stop":{"enabled":True,"threshold":cost_thr},"trailing_stop":{"enabled":True,"activation":trail_act,"drawdown":trail_dd},"ladder_tp":{"enabled":True,"levels":[{"profit":p,"sell_ratio":r}for p,r in ladder_lv]},"time_stop":{"enabled":True,"max_hold_days":max_hold},"cond_time_stop":{"enabled":True,"days":min(7,max_hold-1),"profit":0.02}}
        lp=np.array([p for p,_ in ladder_lv],dtype=np.float64);lr=np.array([r for _,r in ladder_lv],dtype=np.float64)
        engine=BacktestEngine(ENG)
        result=engine.run_cached(cs,entries,hs.values.astype(np.float64),ls.values.astype(np.float64),STP,sel,lp,lr,len(ladder_lv),skip_sm=True)
        met=result["metrics"]
        return {"name":name,"cum":result["cumulative_return"]*100,"ann":met["annualized_return"]*100,"dd":met["max_drawdown"]*100,"sr":met["sharpe_ratio"],"wr":met["win_rate"]*100,"t":len(result["trades"])}
    except Exception as e: logger.warning("Backtest failed: %s", e);return None

results=[]
for i,(name,*args) in enumerate(CONFIGS):
    r=run_one(name,*args)
    if r:_name=r["name"];results.append(r)
    if i%10==0:print(f"  [{i+1}/{len(CONFIGS)}] best so far: {max(results,key=lambda x:x['ann'])['ann']:.1f}%"if results else f"  [{i+1}/{len(CONFIGS)}]...",flush=True)

results.sort(key=lambda x:x["ann"],reverse=True)

print("\n"+"="*80)
print("  TOP 20")
print("="*80)
for i,r in enumerate(results[:20],1):
    print(f"  {i:<4} {r['name']:<30s} Cum:{r['cum']:>+7.2f}% Ann:{r['ann']:>+7.2f}% DD:{r['dd']:>+7.2f}% SR:{r['sr']:>6.2f} WR:{r['wr']:>5.1f}% T:{r['t']:>6}")

# Show 20% threshold
above20=[r for r in results if r["ann"]>=20]
print(f"\n  {'='*40}")
if above20:print(f"  >=20% Ann: {len(above20)} configs")
else:print(f"  BEST: {results[0]['ann']:.2f}% (closest to 20%)")

# Output best config details
best=results[0]
print(f"\n  Best: {best['name']}")
print(f"  Cum:{best['cum']:+.2f}% Ann:{best['ann']:+.2f}% DD:{best['dd']:.2f}% SR:{best['sr']:.2f} WR:{best['wr']:.1f}% T:{best['t']}")

with open("output/sar_optimize_deep.json","w",encoding="utf-8")as f:json.dump(results,f,ensure_ascii=False,indent=2,default=str)
print("  Saved: output/sar_optimize_deep.json")
