"""手工翻译10个B拼音公式 → VERA回测"""
import sys,os,re,json,time,collections
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np
from core.connector import TdxConnector;from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
TdxConnector.ensure_connected()

codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(codes,'20211201','20260607',dividend_type='front',period='1d')
C=k['Close'].sort_index();H=k['High'].sort_index();L=k['Low'].sort_index();O=k['Open'].sort_index();V=k.get('Volume',pd.DataFrame()).sort_index()
valid=C.notna().sum()>100
for d in[C,H,L,O,V]:d=d.loc[:,valid]
univ=[c for c in C.columns if'ST'not in c and'*ST'not in c]

idx_k=DataFetcher.get_kline(['999999.SH'],'20201201','20260607',dividend_type='none',period='1d')
idxC=idx_k['Close'].sort_index().iloc[:,0];idxMA60=idxC.rolling(60).mean()
idxUp5=(idxMA60>idxMA60.shift(1))&(idxMA60.shift(1)>idxMA60.shift(2))&(idxMA60.shift(2)>idxMA60.shift(3))&(idxMA60.shift(3)>idxMA60.shift(4))&(idxMA60.shift(4)>idxMA60.shift(5))
market_on=idxUp5.reindex(C.index,method='ffill').fillna(False).astype(bool)

ENG={'initial_capital':1000000.0,'commission':0.0003,'slippage':0.001,'period':'1d','position_sizing':{'min_buy_amount':2000.0,'max_buy_amount':10000.0,'lot_size':100,'min_lots':1}}
STP={'cost_stop':{'enabled':True,'threshold':-0.08},'trailing_stop':{'enabled':True,'activation':0.05,'drawdown':0.03},'ladder_tp':{'enabled':True,'levels':[{'profit':0.05,'sell_ratio':0.3},{'profit':0.12,'sell_ratio':0.3}]},'time_stop':{'enabled':True,'max_hold_days':20},'cond_time_stop':{'enabled':True,'days':7,'profit':0.02}}
bp=np.array([0.05,0.12]);br=np.array([0.30,0.30])

def run_bt(sig,title):
    sig_bt=sig.loc['20220101':]
    mo=market_on.loc[sig_bt.index]
    sig_bt=sig_bt.copy()
    for col in sig_bt.columns:sig_bt.loc[~mo,col]=False
    ts=int(sig_bt.sum().sum())
    print(f'[{title}] sig={ts}',end='',flush=True)
    if ts<50:print(' SKIP');return None
    recs=[]
    for col in univ:
        if col not in sig_bt.columns:continue
        for idx in sig_bt.index[sig_bt[col]]:recs.append({'stock_code':col,'select_date':idx.strftime('%Y-%m-%d')})
    sel=pd.DataFrame(recs);common=sorted(set(C.columns)&set(sel['stock_code'].unique()))
    cs=C[common].ffill().bfill();hs=H.reindex(index=cs.index,columns=common).ffill().bfill();ls=L.reindex(index=cs.index,columns=common).ffill().bfill()
    entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
    for _,row in sel.iterrows():
        code,dt=row['stock_code'],pd.Timestamp(row['select_date'])
        if code not in entries.columns:continue
        if dt in entries.index:entries.loc[dt,code]=True
        else:
            mask=entries.index>=dt
            if mask.any():entries.loc[entries.index[mask][0],code]=True
    engine=BacktestEngine(ENG)
    brs=engine.run_cached(cs,entries,hs.values.astype(np.float64),ls.values.astype(np.float64),STP,sel,bp,br,2,skip_sm=True)
    met=brs['metrics'];t=brs['trades'];t['year']=pd.to_datetime(t['exit_date']).dt.year
    cum=brs['cumulative_return'];ann=met['annualized_return'];dd=met['max_drawdown'];sr=met['sharpe_ratio'];wr=met['win_rate']
    print(f' => Cum:{cum*100:+.2f}% Ann:{ann*100:+.2f}% DD:{dd*100:.2f}% SR:{sr:.2f} WR:{wr*100:.1f}% T:{len(t)}',flush=True)
    yrs={};cap=1000000.0
    for y in sorted(t['year'].unique()):
        yt=t[t['year']==y];p=yt['pnl'].sum();ret=p/cap*100;cap+=p;yrs[int(y)]={'pnl':int(p),'ret':ret}
        print(f'    {y}: {p:>+12,.0f} ({ret:+.2f}%)')
    return {'title':title,'cum':cum,'ann':ann,'dd':dd,'sr':sr,'wr':wr,'trades':len(t),'signals':ts,'yearly':yrs}

results=[]

# === #6 波动爆发主图之选股 === (simplest, 5 lines)
print('\n--- #6 波动爆发 ---')
# VAR1: limit-up (>=9.90% AND close=high)
# VAR2: triple EMA(high,13)
var2=H.ewm(span=13,adjust=False).mean().ewm(span=13,adjust=False).mean().ewm(span=13,adjust=False).mean()
# VAR3: pct change
var3=(C-C.shift(1))/C.shift(1)*100
# VAR8: >9.85% AND high below VAR2*1.25
var8=(var3>9.85)&(H<var2*1.25)
# XG: FILTER(VAR8,5)
sig6=pd.DataFrame(False,index=var8.index,columns=var8.columns)
for col in var8.columns:
    v=var8[col].values;o=np.zeros(len(v),dtype=bool);lt=-6
    for i in range(len(v)):
        if v[i]and(i-lt)>5:o[i]=True;lt=i
    sig6[col]=o
run_bt(sig6,'6.波动爆发')

# === #8 波动狙击 ===
print('\n--- #8 波动狙击 ---')
var2_8=(C-L.rolling(18).min())/(H.rolling(18).max()-L.rolling(18).min())*100
var3_8=var2_8.ewm(span=13,adjust=False).mean()  # SMA(13,8) ~ ema approximation
# 趋势线 = 18
sig8=(var2_8>var3_8)&(var2_8.shift(1)<=var3_8.shift(1))
run_bt(sig8,'8.波动狙击(KDJ金叉)')

# === #9 波动双通道-抄底2 ===
print('\n--- #9 波动双通道-抄底2 ---')
var1_9=(C/C.shift(1)>=1.098)&(C==H)  # limit-up
var22=(var1_9.rolling(2).sum()>=1)&(~(var1_9.rolling(3).sum()>=2))
di9=pd.DataFrame(False,index=var22.index,columns=var22.columns)
for col in var22.columns:
    v=var22[col].values;o=np.zeros(len(v),dtype=bool);lt=-14
    for i in range(len(v)):
        if v[i]and(i-lt)>13:o[i]=True;lt=i
    di9[col]=o
cd9=di9&(di9.rolling(18).sum()==1)
run_bt(cd9,'9.双通道抄底2(首板后回踩)')

# === #10 波动双通道-追涨2 ===
print('\n--- #10 波动双通道-追涨2 ---')
var23=(var1_9.rolling(3).sum()>=2)&(~(var1_9.rolling(4).sum()>=3))
zz=pd.DataFrame(False,index=var23.index,columns=var23.columns)
for col in var23.columns:
    v=var23[col].values;o=np.zeros(len(v),dtype=bool);lt=-14
    for i in range(len(v)):
        if v[i]and(i-lt)>13:o[i]=True;lt=i
    zz[col]=o
zj10=zz&(zz.rolling(18).sum()==1)
run_bt(zj10,'10.双通道追涨2(二连板后首现)')

# === #4 波启圣诞主图之选股 ===
print('\n--- #4 波启圣诞主图 ---')
ema8=H.ewm(span=8,adjust=False).mean()  # 动态压力线=EMA(HHV(H,1),8)
ema8c=C.ewm(span=8,adjust=False).mean()
trend_down=(ema8c<ema8c.shift(1))&(C<ema8c)
s1=(ema8<ema8.shift(1))|trend_down
rsv=(np.maximum(C-C.shift(1),0)).rolling(2).mean()/(abs(C-C.shift(1))).rolling(2).mean()*100
# SMA simulation via ema
rsi4=rsv.ewm(span=2,adjust=False).mean()
bull_sig=(rsi4<45)&(rsi4.shift(1)>45)
value_sig=(rsi4<20)&(rsi4.shift(1)>20)
strong_count=(bull_sig==1).rolling(4).sum()==3
strong_cond=strong_count&(bull_sig==0)&(O<C)
strong_buy=(strong_cond&((C-C.shift(1))/C.shift(1)>0.065))
big_up=((C-C.shift(1))/C.shift(1)>0.065)
second_sig=big_up&(value_sig.shift(1)|bull_sig.shift(1))
sig4=strong_buy|second_sig
run_bt(sig4,'4.波启圣诞(RSI反转)')

# === #5 波启资金主图 ===
print('\n--- #5 波启资金主图 ---')
dc5=(O>0)&(H.rolling(10).max()/L.rolling(10).min()<1.5)&(C.shift(1)<(L.rolling(15).min()+(H.rolling(15).max()-L.rolling(15).min())*0.75))&(C>O)&(C>=H.rolling(10).max().shift(1))
dc_fil=pd.DataFrame(False,index=dc5.index,columns=dc5.columns)
for col in dc5.columns:
    v=dc5[col].values;o=np.zeros(len(v),dtype=bool);lt=-6
    for i in range(len(v)):
        if v[i]and(i-lt)>5:o[i]=True;lt=i
    dc_fil[col]=o
ma34=C.rolling(34).mean()
sig5=dc_fil&(ma34>ma34.shift(1))&(C>ma34)
run_bt(sig5,'5.波启资金(窄幅突破)')

# === #2 波路追涨-突破选股 === (same as 箱体突破)
print('\n--- #2 波路追涨-突破 ---')
xiangding=H.rolling(30).max().shift(1).rolling(2).mean()
xiangdi=L.rolling(30).min().shift(1).rolling(2).mean()
zhicheng=C.ewm(span=20,adjust=False).mean()
a4=np.sqrt(((C-zhicheng)**2).rolling(20).mean())
shanggui=(zhicheng+2*a4).shift(1)
liangbi=V/V.rolling(5).mean().shift(1)
s_up1=(shanggui>xiangding)&(shanggui>=shanggui.shift(1))
xd2=(xiangding>=xiangding.shift(1))&(C>O)
breakout=(C>xiangding)&(C>shanggui)&(liangbi>2)
breakout7=breakout.rolling(7).sum()
tupo=((C>shanggui)&(C.shift(1)<=shanggui.shift(1)))&(C>xiangding)&(liangbi>1)&(liangbi<5)&s_up1&xd2
run_bt(tupo,'2.波路追涨-突破')

# === #3 波路追涨-追涨选股 === (same as 箱体追涨)
print('\n--- #3 波路追涨-追涨 ---')
zhuizhang3=breakout&(breakout7==1)&s_up1&xd2
run_bt(zhuizhang3,'3.波路追涨-追涨')

# === #7 波动套利 === (uses MACD.DEA ref)
print('\n--- #7 波动套利 ---')
ema12=C.ewm(span=12,adjust=False).mean();ema26=C.ewm(span=26,adjust=False).mean()
dif=ema12-ema26;dea7=dif.ewm(span=9,adjust=False).mean()
macd7=(dif-dea7)*2
af7=dea7<=0.6
af1_7=(C>=C.shift(1)*1.099)&(C<C.rolling(20).mean()*1.16)&(C>C.rolling(20).mean())&af7&(C>=C.rolling(20).max())
a1_7=(C.rolling(5).mean()-C.rolling(5).mean().shift(1))/C.rolling(5).mean()>0.015
a2_7=C>=H.rolling(30).max().shift(1)
a3_7=C>C.shift(1)*1.07
xg7=a1_7&a2_7&a3_7
sig7=af1_7&xg7
run_bt(sig7,'7.波动套利(MACD底+突破)')

# === #1 波动爆发装逼之选股 === (complex, approximate core logic)
print('\n--- #1 波动爆发装逼(简化) ---')
# Too complex with WINNER/CAPITAL/FINANCE/DYNAINFO/PPART - approximate core
winner_cond=(C/C.shift(1)>=1.045)&(V>V.shift(1)*0.35)
macd_ema12=C.ewm(span=12,adjust=False).mean();macd_ema26=C.ewm(span=26,adjust=False).mean()
macd_dif=macd_ema12-macd_ema26;macd_dea=macd_dif.ewm(span=9,adjust=False).mean()
macd_strong=(macd_dif>=0)&(macd_dea>=0)&(macd_dif>=0).rolling(7).sum()>=2
# approximate: winner>80, big day, volume surge, MACD strong, no recent limit-up
no_limit=(C/C.shift(1)<1.098).rolling(15).min()>0
sig1=winner_cond&macd_strong&no_limit&(C>C.shift(1))
run_bt(sig1,'1.波动爆发(简化-放量+MACD)')

# Summary
print('\n'+'='*80)
print('  B-Pinyin 10 Formula Results')
print('='*80)
results.sort(key=lambda x:x['ann']if x['ann']is not None else-999,reverse=True)
for i,r in enumerate(results,1):
    print(f"  {i}. {r['title'][:45]:<47} Cum:{r['cum']*100:>+7.2f}% Ann:{r['ann']*100:>+7.2f}% DD:{r['dd']*100:>+7.2f}% SR:{r['sr']:>5.2f} WR:{r['wr']*100:>5.1f}% T:{r['trades']:>6}")
