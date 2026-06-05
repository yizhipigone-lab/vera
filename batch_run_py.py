"""批量运行 py_strategies 下所有 PY 策略文件 — VERA 回测

用法: python batch_run_py.py
"""
import sys,os,re,json,warnings,time,importlib
warnings.filterwarnings('ignore')
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))

import pandas as pd,numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine

TdxConnector.ensure_connected()

print("="*80)
print("  468 PY策略文件批量回测")
print("="*80)

# ═══ 1. Load data once ═══
print("\n[1] Loading K-line data...",flush=True)
codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(codes,'20240601','20260603',dividend_type="front",period="1d")
C=k["Close"].sort_index();H=k["High"].sort_index();L=k["Low"].sort_index()
O=k["Open"].sort_index();V=k.get("Volume",pd.DataFrame()).sort_index()
valid=C.notna().sum()>100
for d in[C,H,L,O,V]:d=d.loc[:,valid]
univ=[c for c in C.columns if'ST'not in c and'*ST'not in c]
print(f"  {C.shape} | {len(univ)} stocks")

# ═══ 2. Common configs ═══
ENGINE_CFG={
    'initial_capital':200000.0,'commission':0.0003,'slippage':0.001,'period':'1d',
    'position_sizing':{'min_buy_amount':2000.0,'max_buy_amount':10000.0,'lot_size':100,'min_lots':1},
}
STOP_CONFIG={
    'cost_stop':{'enabled':True,'threshold':-0.08},
    'trailing_stop':{'enabled':True,'activation':0.05,'drawdown':0.03},
    'ladder_tp':{'enabled':True,'levels':[{'profit':0.05,'sell_ratio':0.3},{'profit':0.12,'sell_ratio':0.3}]},
    'time_stop':{'enabled':True,'max_hold_days':20},
    'cond_time_stop':{'enabled':True,'days':7,'profit':0.02},
}
BP=np.array([0.05,0.12],dtype=np.float64);BR=np.array([0.30,0.30],dtype=np.float64)

# ═══ 3. Batch run ═══
py_dir=r'E:\gongshi\py_strategies'
py_files=sorted([f for f in os.listdir(py_dir) if f.endswith('.py')])
total=len(py_files)
print(f"\n[2] Running {total} strategies...",flush=True)

results=[]
ok=0;no_sig=0;bt_fail=0;imp_fail=0
t0=time.time()

for fi,fname in enumerate(py_files):
    module_name=fname[:-3]  # strip .py
    filepath=os.path.join(py_dir,fname)

    # Import the module
    try:
        spec=importlib.util.spec_from_file_location(module_name,filepath)
        mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        imp_fail+=1
        if fi%50==0:
            print(f"  [{fi+1}/{total}] OK:{ok} Sig:{no_sig} BT:{bt_fail} Imp:{imp_fail}",flush=True)
        continue

    # Get signal
    title=getattr(mod,'STRATEGY_NAME',module_name)
    try:
        sig=mod.compute_signal(C,H,L,O,V).loc['20260101':]
    except Exception:
        imp_fail+=1
        if fi%50==0:
            print(f"  [{fi+1}/{total}] OK:{ok} Sig:{no_sig} BT:{bt_fail} Imp:{imp_fail}",flush=True)
        continue

    ts=int(sig.sum().sum())
    if ts<5:
        no_sig+=1
        if fi%50==0:
            print(f"  [{fi+1}/{total}] OK:{ok} Sig:{no_sig} BT:{bt_fail} Imp:{imp_fail}",flush=True)
        continue

    # Build selections & run backtest
    try:
        recs=[]
        for col in univ:
            if col not in sig.columns:continue
            for idx in sig.index[sig[col]]:
                recs.append({'stock_code':col,'select_date':idx.strftime('%Y-%m-%d')})
        sel=pd.DataFrame(recs)
        if len(sel)<10:no_sig+=1;continue

        common=sorted(set(C.columns)&set(sel['stock_code'].unique()))
        cs=C[common].ffill().bfill();hs=H.reindex(index=cs.index,columns=common).ffill().bfill()
        ls=L.reindex(index=cs.index,columns=common).ffill().bfill()
        entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
        for _,row in sel.iterrows():
            code,dt=row['stock_code'],pd.to_datetime(row['select_date'])
            if code not in entries.columns:continue
            if dt in entries.index:entries.loc[dt,code]=True
            else:
                m=entries.index>=dt
                if m.any():entries.loc[entries.index[m][0],code]=True

        engine=BacktestEngine(ENGINE_CFG)
        brs=engine.run_cached(cs,entries,hs.values.astype(np.float64),
                              ls.values.astype(np.float64),STOP_CONFIG,sel,BP,BR,2,skip_sm=True)
        m=brs['metrics']
        results.append({
            'title':title,'file':fname,'signals':ts,
            'cumret':brs['cumulative_return'],'annret':m['annualized_return'],
            'maxdd':m['max_drawdown'],'sharpe':m['sharpe_ratio'],
            'winrate':m['win_rate'],'trades':len(brs['trades']),
        })
        ok+=1
    except Exception:
        bt_fail+=1

    if fi%50==0:
        elapsed=max(time.time()-t0,1)
        print(f"  [{fi+1}/{total}] OK:{ok} Sig:{no_sig} BT:{bt_fail} Imp:{imp_fail} | ~{(fi+1)/elapsed*60:.0f}/min",flush=True)

# ═══ 4. Output ═══
print(f"\n[3] Done! OK:{ok} NoSignal:{no_sig} BTFail:{bt_fail} ImportFail:{imp_fail}",flush=True)
results.sort(key=lambda r:r['annret']if r['annret']is not None else-999,reverse=True)

print("\n"+"="*100)
print(f"  TOP 100 | 2026-01-01~2026-06-03 | {len(univ)} stocks")
print("="*100)
print(f"  {'#':<5} {'公式':<50} {'收益%':>7} {'年化%':>7} {'回撤%':>7} {'夏普':>5} {'胜率':>6} {'交易':>6} {'信号':>6}")
print("  "+"-"*100)
for i,r in enumerate(results[:100],1):
    print(f"  {i:<5} {r['title'][:48]:<50} {r['cumret']*100:>+7.2f} {r['annret']*100:>+7.2f} "
          f"{r['maxdd']*100:>+7.2f} {r['sharpe']:>5.2f} {r['winrate']*100:>6.1f} {r['trades']:>6} {r['signals']:>6}")

output={'config':{'period':'2026-01-01~2026-06-03'},'top100':results[:100],'all':results}
with open('output/batch_py_top100.json','w',encoding='utf-8')as f:
    json.dump(output,f,ensure_ascii=False,indent=2,default=str)
print(f"\nSaved: output/batch_py_top100.json ({len(results)} formulas)")
print("="*80)
