"""407 TDX formulas batch backtest — TDX native engine + VERA"""
import sys,os,re,json,warnings,time
warnings.filterwarnings('ignore')
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine
from utils.logger import get_logger
logger = get_logger(__name__)

TdxConnector.ensure_connected()
START,END='20260101','20260605'
print("="*80);print("  407 TDX Formulas Batch Backtest");print("="*80)

# 1. Read formula names
print("\n[1] Reading formula names...",flush=True)
txt_dir=os.path.join(os.environ.get('TDX_HOME', r'E:\NEW_TDX'), r'T0001\export\gs_txt')
files=sorted([f for f in os.listdir(txt_dir) if f.endswith('.txt')])
formulas=[]
for fname in files:
    with open(os.path.join(txt_dir,fname),'r',encoding='gbk')as f:first=f.readline().strip()
    if first:formulas.append((fname,first))
print(f"  {len(formulas)} formulas")

# 2. Universe + preload ALL K-line data ONCE
univ=DataFetcher.get_stock_universe('50')
codes_all=[c for c in univ if'ST' not in c and'*ST' not in c]
print(f"\n[2] Universe:{len(codes_all)} stocks. Preloading K-line...",flush=True)
k_full=DataFetcher.get_kline(codes_all,'20240601',END,dividend_type='front',period='1d')
C_all=k_full['Close'].sort_index();H_all=k_full['High'].sort_index();L_all=k_full['Low'].sort_index()
valid_all=C_all.notna().sum()>10
C_all=C_all.loc[:,valid_all];H_all=H_all.reindex(columns=C_all.columns);L_all=L_all.reindex(columns=C_all.columns)
print(f"  Data:{C_all.shape}")

# 3. Configs
ENGINE_CFG={'initial_capital':200000.0,'commission':0.0003,'slippage':0.001,'period':'1d','position_sizing':{'min_buy_amount':2000.0,'max_buy_amount':10000.0,'lot_size':100,'min_lots':1}}
STOP_CONFIG={'cost_stop':{'enabled':True,'threshold':-0.08},'trailing_stop':{'enabled':True,'activation':0.05,'drawdown':0.03},'ladder_tp':{'enabled':True,'levels':[{'profit':0.05,'sell_ratio':0.3},{'profit':0.12,'sell_ratio':0.3}]},'time_stop':{'enabled':True,'max_hold_days':20},'cond_time_stop':{'enabled':True,'days':7,'profit':0.02}}

# 4. Batch
print(f"\n[3] Batch backtest ({len(formulas)} formulas)...",flush=True)
results=[];ok=0;no_sig=0;err=0;t0=time.time()

for fi,(fname,formula_name) in enumerate(formulas):
    # TDX selection
    try:
        sel_df=FormulaRunner.run_stock_selection_with_dates(formula_name=formula_name,formula_arg='',stock_list=codes_all,start_time=START,end_time=END,stock_period='1d',dividend_type=1)
    except Exception as e: logger.warning("FormulaRunner failed: %s", e);err+=1;continue
    if sel_df.empty or len(sel_df)<10:no_sig+=1
    if fi%10==0:elapsed=max(time.time()-t0,1);print(f"  [{fi+1}/{len(formulas)}] OK:{ok} NS:{no_sig} Err:{err} | ~{((fi+1)/elapsed*60):.0f}/min",flush=True)
    if sel_df.empty or len(sel_df)<10:continue

    # Backtest with cached K-line data
    try:
        codes_needed=sel_df['stock_code'].unique().tolist()
        common=sorted(set(C_all.columns)&set(codes_needed))
        if len(common)<10:no_sig+=1;continue
        cs=C_all[common].ffill().bfill();hs=H_all.reindex(index=cs.index,columns=common).ffill().bfill()
        ls=L_all.reindex(index=cs.index,columns=common).ffill().bfill()
        entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
        for _,row in sel_df.iterrows():
            code,dt=row['stock_code'],pd.to_datetime(row['select_date'])
            if code not in entries.columns:continue
            if dt in entries.index:entries.loc[dt,code]=True
            else:m=entries.index>=dt
            if m.any():entries.loc[entries.index[m][0],code]=True
        engine=BacktestEngine(ENGINE_CFG)
        bp=np.array([0.05,0.12],dtype=np.float64);br=np.array([0.30,0.30],dtype=np.float64)
        brs=engine.run_cached(cs,entries,hs.values.astype(np.float64),ls.values.astype(np.float64),STOP_CONFIG,sel_df,bp,br,2,skip_sm=True)
        m=brs['metrics']
        results.append({'name':formula_name,'file':fname,'signals':len(sel_df),'cumret':brs['cumulative_return'],'annret':m['annualized_return'],'maxdd':m['max_drawdown'],'sharpe':m['sharpe_ratio'],'winrate':m['win_rate'],'trades':len(brs['trades'])})
        ok+=1
    except Exception as e: logger.warning("Backtest failed: %s", e);err+=1

# 5. Output
print(f"\n[4] Done! OK:{ok} NoSig:{no_sig} Err:{err}",flush=True)
results.sort(key=lambda r:r['annret']if r['annret']is not None else-999,reverse=True)
print("\n"+"="*100);print(f"  TOP 100 | {START}~{END} | {len(codes_all)} stocks");print("="*100)
for i,r in enumerate(results[:100],1):print(f"  {i:<5} {r['name'][:40]:<42} Cum:{r['cumret']*100:>+7.2f}% Ann:{r['annret']*100:>+7.2f}% DD:{r['maxdd']*100:>+7.2f}% SR:{r['sharpe']:>5.2f} WR:{r['winrate']*100:>5.1f}% T:{r['trades']:>6} S:{r['signals']:>6}")
with open('output/batch_407_tdx.json','w',encoding='utf-8')as f:json.dump({'results':results},f,ensure_ascii=False,indent=2,default=str)
print(f"\nSaved: output/batch_407_tdx.json ({len(results)})");print("="*80)
