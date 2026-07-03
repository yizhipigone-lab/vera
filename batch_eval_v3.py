"""批量公式回测 V3 — 中文变量映射 + 全面函数支持

关键修复:
1. 中文变量名 → _V000 安全映射
2. AND/OR 作为运算符不误报为未知函数
3. FINANCE/DYNAINFO mock支持
4. 所有不支持的函数 → 返回0值DataFrame (不阻断公式执行)
"""
import sys,os,re,json,warnings,time
warnings.filterwarnings('ignore')
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd, numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from utils.logger import get_logger
logger = get_logger(__name__)
TdxConnector.ensure_connected()

print("="*80); print("  批量公式回测 V3"); print("="*80)

# ═══ 1. 数据 ═══
print("\n[1] 数据...",flush=True)
all_codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(all_codes,'20240601','20260603',dividend_type="front",period="1d")
C=k["Close"].sort_index();H=k["High"].sort_index();L=k["Low"].sort_index()
O=k["Open"].sort_index();V=k.get("Volume",pd.DataFrame()).sort_index()
valid=C.notna().sum()>100
for d in [C,H,L,O,V]:d=d.loc[:,valid]
CLOSE=C;HIGH=H;LOW=L;OPEN=O;VOL=V
def ex_st(cl):return[c for c in cl if 'ST' not in c and '*ST' not in c]
univ=ex_st([c for c in C.columns])
print(f"  {C.shape} | {len(univ)}只")

# ═══ 2. TDX函数 → Python ═══
def _ma(x,n):return x.rolling(int(n),min_periods=1).mean()
def _ema(x,n):return x.ewm(span=int(n),adjust=False).mean()
def _ref(x,n):return x.shift(int(n))
def _hhv(x,n):return x.rolling(int(n),min_periods=1).max()
def _llv(x,n):return x.rolling(int(n),min_periods=1).min()
def _cross(a,b):return(a>b)&(a.shift(1)<=b.shift(1))
def _count(x,n):return x.astype(float).rolling(int(n),min_periods=1).sum()
def _every(x,n):return(x>0).rolling(int(n),min_periods=1).min()>0
def _exist(x,n):return _count(x>0,int(n))>0
def _barslast(x):
    r=pd.DataFrame(np.nan,index=x.index,columns=x.columns)
    for col in x.columns:
        v=x[col].values;o=np.full(len(v),np.nan);lt=-1
        for i in range(len(v)):
            if v[i] and not np.isnan(v[i]):lt=i
            if lt>=0:o[i]=i-lt
        r[col]=o
    return r
def _filter(x,n):
    r=x.copy()
    for col in x.columns:
        v=x[col].values.astype(bool);o=np.zeros(len(v),dtype=bool);lt=-int(n)-1
        for i in range(len(v)):
            if v[i] and(i-lt)>int(n):o[i]=True;lt=i
        r[col]=o
    return r
def _sum(x,n):return x.rolling(int(n),min_periods=1).sum()
def _abs(x):return x.abs()
def _max(a,b):
    av=a.values if isinstance(a,pd.DataFrame)else a
    bv=b.values if isinstance(b,pd.DataFrame)else b
    return pd.DataFrame(np.maximum(av,bv),index=a.index if isinstance(a,pd.DataFrame)else b.index,columns=a.columns if isinstance(a,pd.DataFrame)else b.columns)
def _min(a,b):
    av=a.values if isinstance(a,pd.DataFrame)else a
    bv=b.values if isinstance(b,pd.DataFrame)else b
    return pd.DataFrame(np.minimum(av,bv),index=a.index if isinstance(a,pd.DataFrame)else b.index,columns=a.columns if isinstance(a,pd.DataFrame)else b.columns)
def _upnday(x,n):
    r=pd.DataFrame(True,index=x.index,columns=x.columns)
    for i in range(1,int(n)):r=r&(x>x.shift(i))
    return r
def _sma(x,n,m=1):
    r=x.copy()
    for col in x.columns:
        v=x[col].values.astype(float);s=np.full(len(v),np.nan)
        fv=np.where(~np.isnan(v))[0]
        if len(fv)>0:
            s[fv[0]]=v[fv[0]]
            for i in range(fv[0]+1,len(v)):
                s[i]=(m*v[i]+(n-m)*s[i-1])/n if not np.isnan(v[i])else s[i-1]
        r[col]=s
    return r
def _atan(x):return np.arctan(x)*180/np.pi
def _barslastcount(x):
    r=pd.DataFrame(0,index=x.index,columns=x.columns,dtype=float)
    for col in x.columns:
        v=x[col].values.astype(bool);o=np.zeros(len(v));c=0
        for i in range(len(v)):
            if v[i]:o[i]=c;c=0
            else:o[i]=c;c+=1
        r[col]=o
    return r
def _barssincen(x,n):return _count(~x.astype(bool),int(n))
def _longcross(a,b,n):return(a>b)&(a.shift(int(n))<=b.shift(int(n)))
def _exp(x):return np.exp(x.clip(-20,20))
def _log(x):return np.log(x.clip(1e-10,None))
def _std(x,n):return x.rolling(int(n),min_periods=1).std()
def _round(x,n=0):return x.round(int(n))
def _intpart(x):return np.floor(x)
def _mod(a,b):return a%float(b)
def _reverse(x):return -x
def _ztprice(c,lim=0.1):return c*(1+float(lim)if isinstance(lim,(int,float))else 0.1)
def _finance(n):
    r=pd.DataFrame(1.0,index=C.index,columns=C.columns)
    if n==3:
        for col in C.columns:r[col]=4.0 if col.startswith('688')else 1.0
    return r
def _dynainfo(n):
    return pd.DataFrame(0.0,index=C.index,columns=C.columns)
def _cost(x):return x  # 近似:成本分布简化为价格本身
def _pwinner(x):return pd.DataFrame(50.0,index=x.index,columns=x.columns)
def _inblock(x):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _codelike(x):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _namelike(x):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _between(x,lo,hi):return(x>=float(lo))&(x<=float(hi))
def _not(x):return~(x.astype(bool))
def _hhvbars(x,n):
    r=pd.DataFrame(0,index=x.index,columns=x.columns,dtype=float)
    for col in x.columns:
        v=x[col].values;o=np.zeros(len(v))
        for i in range(len(v)):
            start=max(0,i-int(n)+1);o[i]=i-np.argmax(v[start:i+1])-start
        r[col]=o
    return r
def _llvbars(x,n):
    r=pd.DataFrame(0,index=x.index,columns=x.columns,dtype=float)
    for col in x.columns:
        v=x[col].values;o=np.zeros(len(v))
        for i in range(len(v)):
            start=max(0,i-int(n)+1);o[i]=i-np.argmin(v[start:i+1])-start
        r[col]=o
    return r
def _tp():return pd.DataFrame(0.0,index=C.index,columns=C.columns)
def _forcast(x,n):
    return x.rolling(int(n),min_periods=1).apply(
        lambda v:np.polyval(np.polyfit(np.arange(len(v)),v,1),len(v)),raw=True
    )
def _findhigh(x,n,m,a):return pd.DataFrame(0.0,index=x.index,columns=x.columns)
def _findhighbars(x,n,m,a):return pd.DataFrame(0.0,index=x.index,columns=x.columns)
def _findlow(x,n,m,a):return pd.DataFrame(0.0,index=x.index,columns=x.columns)
def _findlowbars(x,n,m,a):return pd.DataFrame(0.0,index=x.index,columns=x.columns)
def _strcat(*a):return''
def _con2str(*a):return''
def _rgb(*a):return 0
def _drawgbk(*a):return 0
def _dtprice(*a):return pd.DataFrame(0.0,index=C.index,columns=C.columns)
def _bartime(*a):return pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float)
def _vol(*a):return V
def _amount(*a):return V*C
def _sar(*a):return L
def _slope(x,n):return(x-x.shift(int(n)))/int(n)
def _partline(x,cond):return x
def _capital():return V.rolling(20).mean()*20/C
def _hsl():return(C/C.shift(1)-1).abs()*100
def _datetoday(d):return 20240601
def _stub0(*a,**k):return pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float)
def _stubFalse(*a,**k):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _stubStr(*a,**k):return''
def _stubNum(*a,**k):return 0.0

# 完整函数映射
FUNCS={
    'MA':_ma,'EMA':_ema,'SMA':_sma,'REF':_ref,'HHV':_hhv,'LLV':_llv,
    'CROSS':_cross,'COUNT':_count,'EVERY':_every,'EXIST':_exist,'SUM':_sum,
    'ABS':_abs,'MAX':_max,'MIN':_min,'BARSLAST':_barslast,'FILTER':_filter,
    'UPNDAY':_upnday,'BETWEEN':_between,'NOT':_not,'IF':None,'STD':_std,
    'REFX':_ref,'BARSLASTCOUNT':_barslastcount,'BARSSINCEN':_barssincen,
    'LONGCROSS':_longcross,'ATAN':_atan,'TAN':lambda x:np.tan(x),'COS':lambda x:np.cos(x),
    'SIN':lambda x:np.sin(x),'LN':_log,'LOG':_log,'SQRT':lambda x:np.sqrt(x.clip(0,None)),
    'POW':lambda x,n:np.power(x,float(n)),'INTPART':_intpart,'FLOOR':lambda x:np.floor(x),
    'CEILING':lambda x:np.ceil(x),'ROUND':_round,'EXP':_exp,'MOD':_mod,'REVERSE':_reverse,
    'ZTPRICE':_ztprice,'FINANCE':_finance,'DYNAINFO':_dynainfo,'COST':_cost,
    'PWINNER':_pwinner,'INBLOCK':_inblock,'CODELIKE':_codelike,'NAMELIKE':_namelike,
    'HHVBARS':_hhvbars,'LLVBARS':_llvbars,'TP':_tp,'FORCAST':_forcast,'SLOPE':_slope,
    'FINDHIGH':_findhigh,'FINDHIGHBARS':_findhighbars,'FINDLOW':_findlow,
    'FINDLOWBARS':_findlowbars,'SAR':_sar,'DMA':_ma,'WMA':_ma,'MEMA':_ema,
    'EXPMEMA':_ema,'EXPMA':_ema,'RANGE':lambda x,n:_hhv(x,n)-_llv(x,n),
    'DEVSQ':lambda x,n:(x-x.rolling(int(n)).mean())**2,
    'VAR':lambda x,n:x.rolling(int(n),min_periods=1).var(),
    'AVEDEV':lambda x,n:x.rolling(int(n)).apply(lambda v:(v-v.mean()).abs().mean(),raw=True),
    'COVAR':lambda x,y,n:x.rolling(int(n)).cov(y),
    'BACKSET':lambda x,n:x,'PEAK':_hhv,'TROUGH':_llv,
    'BARSCOUNT':_stub0,'BARSLAST':_barslast,'CONST':lambda x:x.iloc[-1],
    'CURRBARSCOUNT':_stub0,'DATATYPE':_stub0,'PERIOD':_stub0,'TOTALBARSCOUNT':_stub0,
    'ISLASTBAR':_stubFalse,'PARTLINE':_partline,'CAPITAL':_capital,'HSL':_hsl,
    'VOL':V,'AMOUNT':lambda:V*C,'DATE':_stub0,'TIME':_stub0,
    'STRCAT':_stubStr,'CON2STR':_stubStr,'RGB':_stubNum,'DRAWGBK':_stubNum,
    'DTPRICE':_dtprice,'BARTIME':_bartime,'DATETODAY':_datetoday,'REFDATE':_ref,
    'FILTERX':_filter,'K':C,
}

# ═══ 3. 公式执行引擎 ═══
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

def eval_formula(code_str):
    """执行TDX公式,返回(信号DataFrame, 错误信息)"""
    lines=code_str.strip().split('\n')

    # 收集所有需要映射的变量名
    all_vars=set()
    for line in lines:
        line=line.strip().rstrip(';')
        if not line or line.startswith('{'):continue
        if ':=' in line:
            var=line.split(':=',1)[0].strip()
            if var:all_vars.add(var)
        elif ':' in line:
            var=line.split(':',1)[0].strip()
            if var and not any(c in var for c in '><=+-*/() []{}'):
                all_vars.add(var)

    # 为中文/非ASCII变量名创建安全映射
    cn_map={}
    cn_idx=0
    for v in sorted(all_vars,key=lambda x:-len(x)):
        if any(ord(c)>127 for c in v) or not v.isidentifier():
            safe=f'_CN{cn_idx:04d}'
            cn_map[v]=safe
            cn_idx+=1

    # 处理每一行
    local_vars={
        'C':C,'CLOSE':C,'O':O,'OPEN':O,'H':H,'HIGH':H,'L':L,'LOW':L,'V':V,'VOL':V,
        'np':np,'pd':pd,
    }
    local_vars.update(FUNCS)
    local_vars.update(cn_map)

    output_var=None
    processed=[]

    for line in lines:
        line=line.strip()
        if not line or line.startswith('{')or line.startswith('//'):continue
        line=line.rstrip(';')

        if any(line.upper().startswith(kw)for kw in['DRAW','STICK','PLOYLINE','DRAWTEXT','VERTLINE','DRAWNUMBER','DRAWICON','DRAWKLINE','DRAWBAND','FILLRGN','PARTLINE','COLOR','LINETHICK','DRAWNULL','NODRAW','ALIGNRIGHT','CIRCLEDOT','POINTDOT','DOTLINE','CROSSDOT','VOLSTICK','COLORSTICK']):
            continue

        # 替换中文变量名
        for cn,safe in cn_map.items():
            # 使用word boundary替换
            line=re.sub(r'\b'+re.escape(cn)+r'\b',safe,line)

        # 替换AND/OR为位运算符 (在变量替换后)
        # 先保护字符串内的AND/OR
        line=re.sub(r'\bAND\b','&',line,flags=re.IGNORECASE)
        line=re.sub(r'\bOR\b','|',line,flags=re.IGNORECASE)
        line=line.replace('<>','!=')

        if ':=' in line:
            var,expr=line.split(':=',1)
            var=var.strip();expr=expr.strip()
            processed.append(f'{var} = {expr}')
        elif ':' in line:
            parts=line.split(':',1)
            pvar=parts[0].strip();pexpr=parts[1].strip()if len(parts)>1 else''
            # 检查是否是输出变量
            if pvar and not any(c in pvar for c in'><=+-*/() []{}&|!') and not pvar[0].isdigit():
                processed.append(f'{pvar} = {pexpr}')
                output_var=pvar
            else:
                # 可能是条件表达式
                processed.append(f'__e = {line}')
                output_var='__e'

    if output_var is None:
        for pl in reversed(processed):
            if' = 'in pl:
                output_var=pl.split(' = ')[0].strip();break

    if output_var is None:return None,'无输出变量'

    code='\n'.join(processed)

    # 在exec前，映射IF为自定义函数
    def _if_fn(cond,t,f):
        c=cond.values.astype(bool)if isinstance(cond,pd.DataFrame)else cond
        tv=t.values if isinstance(t,pd.DataFrame)else(np.full(c.shape,t)if isinstance(c,np.ndarray)else t)
        fv=f.values if isinstance(f,pd.DataFrame)else(np.full(c.shape,f)if isinstance(c,np.ndarray)else f)
        r=np.where(c,tv,fv)
        return pd.DataFrame(r,index=cond.index if isinstance(cond,pd.DataFrame)else C.index,columns=cond.columns if isinstance(cond,pd.DataFrame)else C.columns)

    local_vars['IF']=_if_fn

    try:
        exec(code,{'__builtins__':{'np':np,'pd':pd,'True':True,'False':False,'abs':abs,'max':max,'min':min,'round':round,'sum':sum,'len':len,'range':range,'int':int,'float':float,'str':str,'bool':bool,'list':list,'dict':dict,'pow':pow,'isinstance':isinstance,'any':any,'all':all}},local_vars)
        result=local_vars.get(output_var)
        if isinstance(result,pd.DataFrame):
            if result.dtypes.iloc[0]!=bool:result=result>0.5
            return result.astype(bool),None
        return pd.DataFrame(bool(result),index=C.index,columns=C.columns),None
    except Exception as e:
        return None,str(e)[:80]

# ═══ 4. 批量处理 ═══
print("\n[2] 批量执行...",flush=True)
gongshi_dir=r'E:\gongshi'
files=sorted([f for f in os.listdir(gongshi_dir) if f.endswith('.md')])
print(f"  公式: {len(files)}")

results=[]
success=0;parse_err=0;sig_err=0;bt_err=0
t0=time.time()

for fi,fname in enumerate(files):
    if fi%20==0 or fi==len(files)-1:
        elapsed=time.time()-t0
        rate=(fi+1)/elapsed*60 if elapsed>0 else 0
        print(f"  [{fi+1}/{len(files)}] OK:{success} 解析:{parse_err} 信号:{sig_err} BT:{bt_err} | ~{rate:.0f}/min",flush=True)

    try:
        with open(os.path.join(gongshi_dir,fname),'r',encoding='utf-8')as f:content=f.read()
    except Exception as e: logger.warning("Parse error: %s", e);parse_err+=1;continue

    nm=re.search(r'^#\s*(.+)',content,re.MULTILINE)
    title=nm.group(1).strip()[:60]if nm else fname.replace('.md','')[:60]

    cm=re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`',content,re.DOTALL)
    if not cm:parse_err+=1;continue

    sig_df,err=eval_formula(cm.group(1))
    if sig_df is None:parse_err+=1;continue

    sig_bt=sig_df.loc['20260101':]
    ts=sig_bt.sum().sum()
    if ts<5:sig_err+=1;continue

    try:
        recs=[]
        for col in univ:
            if col not in sig_bt.columns:continue
            for idx in sig_bt.index[sig_bt[col]]:
                recs.append({'stock_code':col,'select_date':idx.strftime('%Y-%m-%d')})
        sel=pd.DataFrame(recs)
        if len(sel)<10:sig_err+=1;continue

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
        bp=np.array([0.05,0.12],dtype=np.float64);br=np.array([0.30,0.30],dtype=np.float64)
        brs=engine.run_cached(cs,entries,hs.values.astype(np.float64),
                              ls.values.astype(np.float64),STOP_CONFIG,sel,bp,br,2,skip_sm=True)
        m=brs['metrics']
        results.append({
            'file':fname,'title':title,
            'cumret':brs['cumulative_return'],'annret':m['annualized_return'],
            'maxdd':m['max_drawdown'],'sharpe':m['sharpe_ratio'],
            'winrate':m['win_rate'],'trades':len(brs['trades']),'signals':ts,
        })
        success+=1
    except Exception as e: logger.warning("Backtest error: %s", e);bt_err+=1;continue

# ═══ 5. 输出 ═══
print(f"\n[3] 完成! OK:{success} 解析:{parse_err} 信号:{sig_err} BT:{bt_err}",flush=True)
results.sort(key=lambda r:r['annret']if r['annret']is not None else-999,reverse=True)

print("\n"+"="*100)
print(f"  TOP 100 公式 | 2026-01-01 ~ 2026-06-03 | {len(univ)}只(无ST)")
print("="*100)
print(f"  {'#':<5} {'公式名称':<44} {'收益%':>7} {'年化%':>7} {'回撤%':>7} {'夏普':>5} {'胜率':>6} {'交易':>6} {'信号':>6}")
print("  "+"-"*100)

for i,r in enumerate(results[:100],1):
    print(f"  {i:<5} {r['title'][:42]:<44} {r['cumret']*100:>+7.2f} {r['annret']*100:>+7.2f} "
          f"{r['maxdd']*100:>+7.2f} {r['sharpe']:>5.2f} {r['winrate']*100:>6.1f} {r['trades']:>6} {r['signals']:>6}")

output={'config':{'period':'2026-01-01~2026-06-03','universe':f'{len(univ)}无ST'},'top100':results[:100],'all':results}
with open('output/batch_formula_top100.json','w',encoding='utf-8')as f:
    json.dump(output,f,ensure_ascii=False,indent=2,default=str)
print(f"\n  已保存: output/batch_formula_top100.json ({len(results)}个)")
print("="*80)
