"""手工翻译前15个gongshi公式 → VERA回测"""
import sys,os,re
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
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
STP={"cost_stop":{"enabled":True,"threshold":-0.08},"trailing_stop":{"enabled":True,"activation":0.05,"drawdown":0.03},"ladder_tp":{"enabled":True,"levels":[{"profit":0.05,"sell_ratio":0.3},{"profit":0.12,"sell_ratio":0.3}]},"time_stop":{"enabled":True,"max_hold_days":20},"cond_time_stop":{"enabled":True,"days":7,"profit":0.02}}
bp=np.array([0.05,0.12]);br=np.array([0.30,0.30])

def run_bt(sig_df,label):
    sig=sig_df.loc["20250801":];ts=int(sig.sum().sum())
    print(f"\n{'='*60}\n  [{label}] 信号:{ts}")
    if ts<10:print("  信号不足,跳过");return None
    recs=[]
    for col in univ:
        if col not in sig.columns:continue
        for idx in sig.index[sig[col]]:recs.append({"stock_code":col,"select_date":idx.strftime("%Y-%m-%d")})
    sel=pd.DataFrame(recs)
    common=sorted(set(C.columns)&set(sel["stock_code"].unique()))
    cs=C[common].ffill().bfill();hs=H.reindex(index=cs.index,columns=common).ffill().bfill();ls=L.reindex(index=cs.index,columns=common).ffill().bfill()
    entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
    for _,row in sel.iterrows():
        code,dt=row["stock_code"],pd.to_datetime(row["select_date"])
        if code not in entries.columns:continue
        if dt in entries.index:entries.loc[dt,code]=True
        else:
            mask=entries.index>=dt
            if mask.any():entries.loc[entries.index[mask][0],code]=True
    engine=BacktestEngine(ENG)
    result=engine.run_cached(cs,entries,hs.values.astype(np.float64),ls.values.astype(np.float64),STP,sel,bp,br,2,skip_sm=True)
    met=result["metrics"]
    cum=result["cumulative_return"]*100
    ann=met["annualized_return"]*100
    dd=met["max_drawdown"]*100
    sr=met["sharpe_ratio"]
    wr=met["win_rate"]*100
    nt=len(result["trades"])
    from collections import Counter
    rc=Counter(result["trades"]["exit_reason"])
    top_rc=", ".join(f"{r}:{c}" for r,c in rc.most_common(2))
    print(f"  Cum:{cum:+.2f}% Ann:{ann:+.2f}% DD:{dd:.2f}% SR:{sr:.2f} WR:{wr:.1f}% T:{nt} | {top_rc}")
    return cum,ann,dd,sr,wr,nt

MA5=C.rolling(5).mean();MA10=C.rolling(10).mean();MA20=C.rolling(20).mean()
MA60=C.rolling(60).mean();EMA12=C.ewm(span=12,adjust=False).mean()
EMA26=C.ewm(span=26,adjust=False).mean();EMA9=C.ewm(span=9,adjust=False).mean()
DIFF=EMA12-EMA26;DEA=EMA9;MACD_VAL=(DIFF-DEA)*2

results=[]

# 1. ASI今买明卖
print("\n\n### 1. ASI今买明卖 ###")
AA=abs(H-C.shift(1));BB=abs(L-C.shift(1));CC=abs(H-L.shift(1));DD=abs(C.shift(1)-O.shift(1))
R=np.where((AA>BB)&(AA>CC),AA+BB/2+DD/4,np.where((BB>CC)&(BB>AA),BB+AA/2+DD/4,CC+DD/4))
X=(C-C.shift(1)+(C-O)/2+C.shift(1)-O.shift(1))
max_ab=np.maximum(AA.values,BB.values);SI=16*X.values/R*max_ab
SIR=pd.DataFrame(SI,index=C.index,columns=C.columns)
ASI=SIR.cumsum();MASI=ASI.rolling(5).mean()
ST=((C/C.shift(1)-1)*100>5.6).rolling(60).max()>0
sig1=((ASI>MASI)&(ASI.shift(1)<=MASI.shift(1))&ST&(V>V.rolling(5).mean())&(V>V.rolling(10).mean())).fillna(False).astype(bool)
run_bt(sig1,"1.ASI今买明卖")

# 2. MACD空中加油
print("\n### 2. MACD空中加油 ###")
sig2=(DIFF>DEA)&(DIFF.shift(1)<=DEA.shift(1))&(MACD_VAL>0)&(C>MA5)&(V>V.rolling(5).mean())
run_bt(sig2,"2.MACD空中加油")

# 3. N字冷剑封喉
print("\n### 3. N字冷剑封喉 ###")
VAR3_1=(C>C.shift(1))&(C.shift(1)<C.shift(2))
VAR3_2=V>V.rolling(5).mean()*1.5
sig3=VAR3_1&VAR3_2&(C>MA10)&(C.shift(1)<MA10.shift(1))
run_bt(sig3,"3.N字冷剑封喉")

# 4. RSI-WR共振
print("\n### 4. RSI-WR共振 ###")
def rsi(close,n=14):
    delta=close.diff();gain=delta.clip(lower=0);loss=(-delta).clip(lower=0)
    avg_gain=gain.rolling(n).mean();avg_loss=loss.rolling(n).mean()
    rs=avg_gain/avg_loss;return 100-(100/(1+rs))
RSI14=rsi(C,14);WR14=(H.rolling(14).max()-C)/(H.rolling(14).max()-L.rolling(14).min())*100
sig4=(RSI14<30)&(RSI14>RSI14.shift(1))&(WR14<-80)&(WR14>WR14.shift(1))
run_bt(sig4,"4.RSI-WR共振")

# 5. RSRS回归斜率
print("\n### 5. RSRS回归斜率 ###")
def rsrs_score(c,h,l,n=18):
    scores=pd.DataFrame(np.nan,index=c.index,columns=c.columns)
    for col in c.columns:
        hi=h[col].values;lo=l[col].values
        sc=np.full(len(hi),np.nan)
        for i in range(n,len(hi)):
            y=hi[i-n:i];x=lo[i-n:i]
            if len(x)>1:
                mask=~np.isnan(x)&~np.isnan(y)
                if mask.sum()>5:
                    slope,_=np.polyfit(x[mask],y[mask],1)
                    sc[i]=slope
        scores[col]=sc
    return scores
rsrs=rsrs_score(C,H,L,18);rsrs_ma=rsrs.rolling(10).mean()
sig5=(rsrs>0.7)&(rsrs_ma>0)&(C>EMA12)
run_bt(sig5,"5.RSRS回归斜率")

# 6. SAR起爆
print("\n### 6. SAR起爆 ###")
# SAR simplified: use PSAR approximation
def psar(high,low,n=4,af=0.02,max_af=0.2):
    result=pd.DataFrame(np.nan,index=high.index,columns=high.columns)
    for col in high.columns:
        hi=high[col].values;lo=low[col].values
        sar=np.full(len(hi),np.nan);is_up=True;ep=hi[0];af_step=af;s=lo[0]
        for i in range(1,len(hi)):
            if is_up:s=max(s,lo[i-1]);s+=(ep-s)*af_step;s=min(s,lo[i-1],lo[i-2]if i>1 else lo[0])
            else:s=min(s,hi[i-1]);s-=(s-ep)*af_step;s=max(s,hi[i-1],hi[i-2]if i>1 else hi[0])
            sar[i]=s
            if is_up and hi[i]>ep:ep=hi[i];af_step=min(af_step+af,max_af)
            elif not is_up and lo[i]<ep:ep=lo[i];af_step=min(af_step+af,max_af)
            if(is_up and lo[i]<sar[i]):is_up=False;ep=lo[i];af_step=af;s=hi[i]
            elif(not is_up and hi[i]>sar[i]):is_up=True;ep=hi[i];af_step=af;s=lo[i]
        result[col]=sar
    return result
ps=psar(H,L);sig6=(C>ps)&(C.shift(1)<=ps.shift(1))&(C>MA20)&(V>V.rolling(5).mean()*1.2)
run_bt(sig6,"6.SAR起爆")

# 7. V反转
print("\n### 7. V反转 ###")
low5=L.rolling(5).min();high5=H.rolling(5).max()
sig7=(L<=low5.shift(1))&(C>O)&(C>C.shift(1)*1.02)&(V>V.rolling(5).mean()*1.5)
run_bt(sig7,"7.V反转")

# 8. WR改良
print("\n### 8. WR改良 ###")
WR10=(H.rolling(10).max()-C)/(H.rolling(10).max()-L.rolling(10).min())*100
sig8=(WR10<-90)&(WR10.shift(1)<-90)&(C>O)&(V>V.rolling(5).mean())
run_bt(sig8,"8.WR改良")

# 9. 一阳三穿
print("\n### 9. 一阳三穿 ###")
sig9=(C>MA5)&(C>MA10)&(C>MA20)&(C>O)&(C.shift(1)<MA5.shift(1))&(V>V.rolling(5).mean()*1.5)
run_bt(sig9,"9.一阳三穿")

# 10. 三线反转
print("\n### 10. 三线反转 ###")
sig10=(MA5>MA5.shift(1))&(MA10>MA10.shift(1))&(MA20>MA20.shift(1))&(C>C.shift(1))&(C.shift(1)<C.shift(2))
run_bt(sig10,"10.三线反转向上")

# 11. 两级反转
print("\n### 11. 两级反转 ###")
sig11=(C<MA20)&(C>C.shift(1)*1.03)&(V>V.rolling(5).mean()*2)&(C.shift(1)<C.shift(2))
run_bt(sig11,"11.两级反转")

# 12. 一骑红尘
print("\n### 12. 一骑红尘 ###")
sig12=(C>C.shift(1)*1.05)&(V>V.rolling(20).mean()*2)&(C>MA20)&(H==C)|((C/C.shift(1)-1)>0.06)
sig12b=(C/C.shift(1)-1)>0.06
sig12c=sig12b&(V>V.rolling(20).mean()*2)
run_bt(sig12c,"12.一骑红尘(大阳放量)")

# 13. 万马奔腾爆发
print("\n### 13. 万马奔腾爆发 ###")
sig13=(C>MA20)&(MA20>MA20.shift(1))&(V>V.rolling(20).mean()*1.5)&(C>C.shift(1))&(MACD_VAL>0)&(MACD_VAL>MACD_VAL.shift(1))
run_bt(sig13,"13.万马奔腾爆发")

# 14. 上影反攻
print("\n### 14. 上影反攻 ###")
upper_shadow=H-np.maximum(C,O);body=abs(C-O)
sig14=(upper_shadow>body*2)&(C>O)&(V>V.rolling(5).mean())&(C>C.shift(1))
run_bt(sig14,"14.上影反攻")

# 15. 主力买盘
print("\n### 15. 主力买盘 ###")
buy_vol=V*((C-L)-(H-C))/(H-L+0.0001);buy_vol=buy_vol.clip(lower=0)
net_buy=buy_vol.rolling(5).sum()/V.rolling(5).sum()
sig15=(net_buy>0.6)&(C>C.shift(1))&(C>MA5)&(V>V.rolling(5).mean())
run_bt(sig15,"15.主力买盘")

print("\n"+"="*60)
print("  ALL 15 DONE")
print("="*60)
