"""批量公式回测 V2 — exec() 直接执行TDX公式

关键改进: 不手动解析语法,而是把TDX语法预处理后直接 exec()
"""
import sys, os, re, json, warnings
warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config
from utils.logger import get_logger
logger = get_logger(__name__)

TdxConnector.ensure_connected()

# ═══════════════ 1. 数据 ═══════════════
print("=" * 80)
print("  批量公式回测 V2 — exec() 执行模式")
print("=" * 80)

print("\n[1] 加载数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, '20240601', '20260603', dividend_type="front", period="1d")
C = k["Close"].sort_index(); H = k["High"].sort_index(); L = k["Low"].sort_index()
O = k["Open"].sort_index(); V = k.get("Volume", pd.DataFrame()).sort_index()
valid = C.notna().sum() > 100
for d in [C, H, L, O, V]: d = d.loc[:, valid]
CLOSE=C; HIGH=H; LOW=L; OPEN=O; VOL=V

def ex_st(cl): return [c for c in cl if 'ST' not in c and '*ST' not in c]
univ = ex_st([c for c in C.columns])
print(f"  {C.shape} | 股票池: {len(univ)}只")

# ═══════════════ 2. TDX函数 → Python lambda ═══════════════
# 构建全局函数空间
def tdx_ma(x,n):  return x.rolling(int(n),min_periods=max(1,int(n)//2)).mean()
def tdx_ema(x,n): return x.ewm(span=int(n),adjust=False).mean()
def tdx_ref(x,n): return x.shift(int(n))
def tdx_hhv(x,n): return x.rolling(int(n),min_periods=1).max()
def tdx_llv(x,n): return x.rolling(int(n),min_periods=1).min()
def tdx_cross(a,b): return (a>b)&(a.shift(1)<=b.shift(1))
def tdx_count(x,n):
    if isinstance(x, pd.DataFrame) and x.dtypes.iloc[0]==bool:
        return x.rolling(int(n),min_periods=1).sum()
    return x.rolling(int(n),min_periods=1).sum()
def tdx_every(x,n):
    return x.rolling(int(n),min_periods=1).min()>0
def tdx_exist(x,n): return tdx_count(x.astype(float),int(n))>0
def tdx_sum(x,n): return x.rolling(int(n),min_periods=1).sum()
def tdx_std(x,n): return x.rolling(int(n),min_periods=1).std()
def tdx_barslast(x):
    result=pd.DataFrame(np.nan,index=x.index,columns=x.columns)
    for col in x.columns:
        vals=x[col].values; out=np.full(len(vals),np.nan); lt=-1
        for i in range(len(vals)):
            if vals[i] and not np.isnan(vals[i]): lt=i
            if lt>=0: out[i]=i-lt
        result[col]=out
    return result
def tdx_filter(x,n):
    result=x.copy()
    for col in x.columns:
        vals=x[col].values.astype(bool); out=np.zeros(len(vals),dtype=bool); lt=-int(n)-1
        for i in range(len(vals)):
            if vals[i] and (i-lt)>int(n): out[i]=True; lt=i
        result[col]=out
    return result
def tdx_if(cond, tval, fval):
    c=cond.astype(bool)
    tv=tval.values if isinstance(tval,pd.DataFrame) else tval
    fv=fval.values if isinstance(fval,pd.DataFrame) else fval
    r=np.where(c.values if isinstance(c,pd.DataFrame) else c, tv, fv)
    return pd.DataFrame(r,index=cond.index,columns=cond.columns)
def tdx_abs(x): return x.abs()
def tdx_max(a,b):
    av=a.values if isinstance(a,pd.DataFrame) else a
    bv=b.values if isinstance(b,pd.DataFrame) else b
    return pd.DataFrame(np.maximum(av,bv),index=(a if isinstance(a,pd.DataFrame) else b).index,columns=(a if isinstance(a,pd.DataFrame) else b).columns)
def tdx_min(a,b):
    av=a.values if isinstance(a,pd.DataFrame) else a
    bv=b.values if isinstance(b,pd.DataFrame) else b
    return pd.DataFrame(np.minimum(av,bv),index=(a if isinstance(a,pd.DataFrame) else b).index,columns=(a if isinstance(a,pd.DataFrame) else b).columns)
def tdx_upnday(x,n):
    r=pd.DataFrame(True,index=x.index,columns=x.columns)
    for i in range(1,int(n)): r=r&(x>x.shift(i))
    return r
def tdx_between(x,lo,hi): return (x>=lo)&(x<=hi)
def tdx_not(x): return ~(x.astype(bool))
def tdx_sma(x,n,m=1):
    result=x.copy()
    for col in x.columns:
        vals=x[col].values.astype(float)
        sma=np.full(len(vals),np.nan)
        fv=np.where(~np.isnan(vals))[0]
        if len(fv)>0:
            sma[fv[0]]=vals[fv[0]]
            for i in range(fv[0]+1,len(vals)):
                sma[i]=(m*vals[i]+(n-m)*sma[i-1])/n if not np.isnan(vals[i]) else sma[i-1]
        result[col]=sma
    return result
def tdx_ztprice(c,limit=0.1): return c*(1+limit)

# 建立函数名映射 (小写和大写)
FUNCS = {
    'ma':tdx_ma,'ema':tdx_ema,'ref':tdx_ref,'hhv':tdx_hhv,'llv':tdx_llv,
    'cross':tdx_cross,'count':tdx_count,'every':tdx_every,'exist':tdx_exist,
    'sum':tdx_sum,'std':tdx_std,'barslast':tdx_barslast,'filter':tdx_filter,
    'if':tdx_if,'abs':tdx_abs,'max':tdx_max,'min':tdx_min,'upnday':tdx_upnday,
    'between':tdx_between,'not':tdx_not,'sma':tdx_sma,'ztprice':tdx_ztprice,
}
# 大写版本
for k in list(FUNCS.keys()): FUNCS[k.upper()]=FUNCS[k]

# ═══════════════ 3. 公式执行引擎 ═══════════════
ENGINE_CFG = {
    'initial_capital':200000.0,'commission':0.0003,'slippage':0.001,'period':'1d',
    'position_sizing':{'min_buy_amount':2000.0,'max_buy_amount':10000.0,'lot_size':100,'min_lots':1},
}
STOP_CONFIG = load_stop_config()

def eval_formula(code_str):
    """用exec()执行TDX公式,返回输出信号DataFrame"""
    lines=code_str.strip().split('\n')
    local_vars={
        'C':C,'CLOSE':C,'O':O,'OPEN':O,'H':H,'HIGH':H,'L':L,'LOW':L,'V':V,'VOL':V,
        'np':np,'pd':pd,
    }
    local_vars.update(FUNCS)

    output_var=None
    processed_lines=[]

    for line in lines:
        line=line.strip()
        if not line or line.startswith('{') or line.startswith('//'): continue
        line=line.rstrip(';')

        # 过滤纯绘图/显示语句
        if any(line.upper().startswith(kw) for kw in ['DRAW','STICK','PLOYLINE','DRAWTEXT','VERTLINE']):
            continue

        # 识别赋值 vs 输出
        if ':=' in line:
            var,expr=line.split(':=',1)
            var=var.strip()
            # 预处理表达式: 替换TDX操作符→Python操作符
            expr_clean=_preprocess(expr.strip())
            processed_lines.append(f'{var} = {expr_clean}')
        elif ':' in line:
            # 可能是输出语句 VAR:EXPR
            parts=line.split(':',1)
            potential_var=parts[0].strip()
            potential_expr=parts[1].strip() if len(parts)>1 else ''
            # 排除数字比较 (如 1:0.5 格式) 和数字开头的变量名
            if potential_var and not potential_var[0].isdigit() and not any(c in potential_var for c in ['/','*','+','-','>','<','=','(',')']):
                expr_clean=_preprocess(potential_expr)
                processed_lines.append(f'{potential_var} = {expr_clean}')
                output_var=potential_var
            else:
                # 可能是普通表达式(非赋值),直接评估
                expr_clean=_preprocess(line)
                processed_lines.append(f'__expr = {expr_clean}')
                output_var='__expr'

    if output_var is None:
        # 取最后一个赋值的变量
        for line in reversed(processed_lines):
            if ' = ' in line:
                output_var=line.split(' = ')[0].strip()
                break

    if output_var is None: return None,len(processed_lines)

    code='\n'.join(processed_lines)
    try:
        exec(code, {'__builtins__':{'np':np,'pd':pd}}, local_vars)
        result=local_vars.get(output_var)
        if isinstance(result,pd.DataFrame):
            # 如果是数值DataFrame(非布尔),转布尔
            if result.dtypes.iloc[0]!=bool:
                result=result>0
            return result.astype(bool),len(processed_lines)
        if isinstance(result,(int,float)):
            return pd.DataFrame(bool(result),index=C.index,columns=C.columns),len(processed_lines)
    except Exception as e:
        pass
    return None,len(processed_lines)

def _preprocess(expr):
    """TDX表达式 → Python表达式"""
    # 1. 保护字符串
    strings=[]
    def save_str(m):
        strings.append(m.group(0))
        return f'__STR{len(strings)-1}__'
    expr=re.sub(r"'[^']*'",save_str,expr)

    # 2. AND/OR → &/|
    expr=re.sub(r'\bAND\b','&',expr,flags=re.IGNORECASE)
    expr=re.sub(r'\bOR\b','|',expr,flags=re.IGNORECASE)

    # 3. <> → !=
    expr=expr.replace('<>','!=')

    # 4. 函数调用的参数分割: 逗号 → 保留
    # 5. 恢复字符串
    for i,s in enumerate(strings):
        expr=expr.replace(f'__STR{i}__',s)

    return expr


# ═══════════════ 4. 批量处理 ═══════════════
print("\n[2] 批量执行公式...", flush=True)
gongshi_dir=r'E:\gongshi'
files=sorted([f for f in os.listdir(gongshi_dir) if f.endswith('.md')])
print(f"  公式文件: {len(files)}")

results=[]
success=0
parse_fail=0
sig_fail=0
bt_fail=0

import time
last_report=time.time()

for fi,fname in enumerate(files):
    # 每30秒汇报进度
    if time.time()-last_report>30:
        elapsed=time.time()-last_report
        rate=(fi+1)/(elapsed/60) if elapsed>0 else 0
        print(f"  [{fi}/{len(files)}] 成功:{success} 解析失败:{parse_fail} 信号不足:{sig_fail} 回测失败:{bt_fail} | ~{rate:.0f}个/分",flush=True)
        last_report=time.time()

    filepath=os.path.join(gongshi_dir,fname)
    try:
        with open(filepath,'r',encoding='utf-8') as f: content=f.read()
    except Exception as e: logger.warning("Parse failed: %s", e); parse_fail+=1; continue

    name_match=re.search(r'^#\s*(.+)',content,re.MULTILINE)
    title=name_match.group(1).strip() if name_match else fname.replace('.md','')
    # 截断标题
    if len(title)>60: title=title[:57]+'...'

    code_match=re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`',content,re.DOTALL)
    if not code_match: parse_fail+=1; continue
    formula_code=code_match.group(1)

    # 执行公式
    sig_df,n_lines=eval_formula(formula_code)
    if sig_df is None: parse_fail+=1; continue

    # 统计信号
    sig_bt=sig_df.loc['20260101':]
    total_sig=sig_bt.sum().sum()
    if total_sig<5: sig_fail+=1; continue

    # 回测
    try:
        recs=[]
        for col in univ:
            if col not in sig_bt.columns: continue
            for idx in sig_bt.index[sig_bt[col]]:
                recs.append({'stock_code':col,'select_date':idx.strftime('%Y-%m-%d')})
        sel=pd.DataFrame(recs)
        if len(sel)<10: sig_fail+=1; continue

        common=sorted(set(C.columns)&set(sel['stock_code'].unique()))
        cs=C[common].ffill().bfill()
        hs=H.reindex(index=cs.index,columns=common).ffill().bfill()
        ls=L.reindex(index=cs.index,columns=common).ffill().bfill()

        entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
        for _,row in sel.iterrows():
            code,dt=row['stock_code'],pd.to_datetime(row['select_date'])
            if code not in entries.columns: continue
            if dt in entries.index: entries.loc[dt,code]=True
            else:
                m=entries.index>=dt
                if m.any(): entries.loc[entries.index[m][0],code]=True

        engine=BacktestEngine(ENGINE_CFG)
        bp=np.array([0.05,0.12],dtype=np.float64)
        br=np.array([0.30,0.30],dtype=np.float64)
        brs=engine.run_cached(cs,entries,hs.values.astype(np.float64),
                              ls.values.astype(np.float64),STOP_CONFIG,sel,
                              bp,br,2,skip_sm=True)
        m=brs['metrics']
        results.append({
            'file':fname,'title':title,
            'cumret':brs['cumulative_return'],'annret':m['annualized_return'],
            'maxdd':m['max_drawdown'],'sharpe':m['sharpe_ratio'],
            'winrate':m['win_rate'],'trades':len(brs['trades']),'signals':total_sig,
        })
        success+=1
    except Exception as e: logger.warning("Backtest failed: %s", e); bt_fail+=1; continue

# ═══════════════ 5. 输出 ═══════════════
print(f"\n[3] 完成! 成功:{success} 解析失败:{parse_fail} 信号不足:{sig_fail} 回测失败:{bt_fail}",flush=True)
results.sort(key=lambda r:r['annret'] if r['annret'] is not None else -999,reverse=True)

print("\n"+"="*100)
print(f"  TOP 100 公式 | 2026-01-01 ~ 2026-06-03 | 全量(无ST) {len(univ)}只")
print("="*100)
print(f"  {'#':<5} {'公式名称':<42} {'收益%':>7} {'年化%':>7} {'回撤%':>7} {'夏普':>5} {'胜率':>6} {'交易':>6} {'信号':>6}")
print("  "+"-"*95)

for i,r in enumerate(results[:100],1):
    print(f"  {i:<5} {r['title'][:40]:<42} {r['cumret']*100:>+7.2f} {r['annret']*100:>+7.2f} "
          f"{r['maxdd']*100:>+7.2f} {r['sharpe']:>5.2f} {r['winrate']*100:>6.1f} {r['trades']:>6} {r['signals']:>6}")

output={'config':{'period':'2026-01-01~2026-06-03','universe':f'{len(univ)}无ST'},'top100':results[:100],'all':results}
with open('output/batch_formula_top100.json','w',encoding='utf-8') as f:
    json.dump(output,f,ensure_ascii=False,indent=2,default=str)
print(f"\n  已保存: output/batch_formula_top100.json ({len(results)}个公式)")
print("="*80)
