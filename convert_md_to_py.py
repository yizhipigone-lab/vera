"""将 E:\gongshi\*.md 转为独立的 PY 策略文件

每个生成的 .py 文件包含:
1. 公式元数据(名称、来源、原始代码)
2. 信号计算函数 compute_signal(C,H,L,O,V) → DataFrame
3. 标准回测配置
4. 独立运行能力 (python xxx.py)
"""
import sys,os,re,warnings,time
warnings.filterwarnings('ignore')
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))

gongshi_dir = r'E:\gongshi'
output_dir = r'E:\gongshi\py_strategies'
os.makedirs(output_dir, exist_ok=True)

files = sorted([f for f in os.listdir(gongshi_dir) if f.endswith('.md')])
total = len(files)

# 模板头部
HEADER = '''"""
VERA Strategy: {title}
Source: {source_url}
TDX Formula Code:
{formula_code}
"""
import sys,os
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np

# === Strategy Metadata ===
STRATEGY_NAME = "{title}"
FORMULA_NAME = "{formula_name}"

# === Signal Computation ===
def compute_signal(C, H, L, O, V):
    """
    计算选股信号。
    C: Close price DataFrame (index=date, columns=stock_code)
    H: High price DataFrame
    L: Low price DataFrame
    O: Open price DataFrame
    V: Volume DataFrame
    Returns: boolean DataFrame same shape as C
    """
    # 使用 preprocessor 自动翻译执行
    try:
        from preprocessor import preprocess, F
    except ImportError:
        pass

    # 内联 preprocessor (fallback if preprocessor.py not available)
    return _compute_signal_fallback(C,H,L,O,V)

def _compute_signal_fallback(C,H,L,O,V):
    """Fallback: inline formula evaluation."""
    import re
    code = \"\"\"{formula_code}\"\"\"
    # 此处为占位 — 实际执行需要完整的 preprocessor + TDX函数实现
    # 如 batch_fixed.py 中的 preprocess() 和 F dict
    try:
        from batch_fixed import preprocess, F as FUNCS
        py_code, out_var = preprocess(code)
        if out_var is None:
            return pd.DataFrame(False, index=C.index, columns=C.columns)
        loc = {{'C':C,'CLOSE':C,'O':O,'OPEN':O,'H':H,'HIGH':H,'L':L,'LOW':L,'V':V,'VOL':V,
               'np':np,'pd':pd,'True':True,'False':False,
               'abs':abs,'max':max,'min':min,'round':round,'sum':sum,'len':len,
               'int':int,'float':float,'str':str,'bool':bool,
               'list':list,'dict':dict,'pow':pow,'any':any,'all':all}}
        loc.update(FUNCS)
        def _if(cond,t,f):
            cv=cond.values.astype(bool)if isinstance(cond,pd.DataFrame)else np.array(cond,dtype=bool)
            tv=t.values if isinstance(t,pd.DataFrame)else t
            fv=f.values if isinstance(f,pd.DataFrame)else f
            return pd.DataFrame(np.where(cv,tv,fv),index=C.index,columns=C.columns)
        loc['IF']=_if
        exec(py_code,{{'__builtins__':{{}}}},loc)
        result=loc.get(out_var)
        if isinstance(result,pd.DataFrame):
            if result.dtypes.iloc[0]!=bool:result=result>0.5
            return result.astype(bool)
    except Exception:
        pass
    return pd.DataFrame(False,index=C.index,columns=C.columns)

# === Backtest Config ===
ENGINE_CFG = {{
    'initial_capital':200000.0,'commission':0.0003,'slippage':0.001,'period':'1d',
    'position_sizing':{{'min_buy_amount':2000.0,'max_buy_amount':10000.0,'lot_size':100,'min_lots':1}},
}}

STOP_CONFIG = {{
    'cost_stop':{{'enabled':True,'threshold':-0.08}},
    'trailing_stop':{{'enabled':True,'activation':0.05,'drawdown':0.03}},
    'ladder_tp':{{'enabled':True,'levels':[{{'profit':0.05,'sell_ratio':0.3}},{{'profit':0.12,'sell_ratio':0.3}}]}},
    'time_stop':{{'enabled':True,'max_hold_days':20}},
    'cond_time_stop':{{'enabled':True,'days':7,'profit':0.02}},
}}

# === Standalone Runner ===
if __name__=='__main__':
    print(f"Strategy: {{STRATEGY_NAME}}")
    from core.connector import TdxConnector
    from core.data_fetcher import DataFetcher
    from backtest.engine import BacktestEngine

    TdxConnector.ensure_connected()
    codes=DataFetcher.get_stock_universe('50')
    start,end='20260101','20260603'
    k=DataFetcher.get_kline(codes,start,end,dividend_type='front',period='1d')
    C=k['Close'].sort_index();H=k['High'].sort_index();L=k['Low'].sort_index()
    O=k['Open'].sort_index();V=k.get('Volume',pd.DataFrame()).sort_index()
    valid=C.notna().sum()>100
    for d in[C,H,L,O,V]:d=d.loc[:,valid]
    univ=[c for c in C.columns if'ST'not in c and'*ST'not in c]

    sig=compute_signal(C,H,L,O,V).loc[start:end]
    ts=int(sig.sum().sum())
    print(f"Signals: {{ts:,}}")

    if ts>=5:
        recs=[]
        for col in univ:
            if col not in sig.columns:continue
            for idx in sig.index[sig[col]]:
                recs.append({{'stock_code':col,'select_date':idx.strftime('%Y-%m-%d')}})
        import pandas as _pd
        sel=_pd.DataFrame(recs)
        common=sorted(set(C.columns)&set(sel['stock_code'].unique()))
        cs=C[common].ffill().bfill();hs=H.reindex(index=cs.index,columns=common).ffill().bfill()
        ls=L.reindex(index=cs.index,columns=common).ffill().bfill()
        entries=_pd.DataFrame(False,index=cs.index,columns=cs.columns)
        for _,row in sel.iterrows():
            code,dt=row['stock_code'],_pd.to_datetime(row['select_date'])
            if code not in entries.columns:continue
            if dt in entries.index:entries.loc[dt,code]=True
            else:
                m=entries.index>=dt
                if m.any():entries.loc[entries.index[m][0],code]=True
        engine=BacktestEngine(ENGINE_CFG)
        bp=np.array([0.05,0.12],dtype=np.float64);br=np.array([0.30,0.30],dtype=np.float64)
        r=engine.run_cached(cs,entries,hs.values.astype(np.float64),
                            ls.values.astype(np.float64),STOP_CONFIG,sel,bp,br,2,skip_sm=True)
        m=r['metrics']
        print(f"CumRet: {{r['cumulative_return']*100:+.2f}}% | Ann: {{m['annualized_return']*100:+.2f}}% | DD: {{m['max_drawdown']*100:.2f}}% | Trades: {{len(r['trades'])}}")
    else:
        print("Not enough signals for backtest")
'''

count = 0
for fname in files:
    filepath = os.path.join(gongshi_dir, fname)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except:
        continue

    # Extract title
    nm = re.search(r'^#\s*(.+)', content, re.MULTILINE)
    title = nm.group(1).strip() if nm else fname.replace('.md', '')

    # Extract source URL
    url_m = re.search(r'来源:\s*(https?://[^\s>]+)', content)
    source_url = url_m.group(1) if url_m else ''

    # Extract formula code
    cm = re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`', content, re.DOTALL)
    formula_code = cm.group(1) if cm else ''

    # Generate safe filename
    safe_name = re.sub(r'[^\w\-_]', '_', title)[:50]

    # Generate formula name (safe Python identifier)
    formula_name = f"GS_{count:04d}"

    # Fill template
    py_content = HEADER.format(
        title=title,
        source_url=source_url,
        formula_code=formula_code,
        formula_name=formula_name,
    )

    out_path = os.path.join(output_dir, f'{formula_name}_{safe_name}.py')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(py_content)

    count += 1

print(f'Generated {count} PY strategy files in {output_dir}')
print(f'Each file can be run standalone: python py_strategies/GS_0000_xxx.py')
print()
print('Structure:')
print('  - STRATEGY_NAME: formula title')
print('  - compute_signal(C,H,L,O,V): returns boolean signal DataFrame')
print('  - ENGINE_CFG / STOP_CONFIG: backtest parameters')
print('  - if __name__=="__main__": standalone backtest runner')
