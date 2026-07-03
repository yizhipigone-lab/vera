"""468公式批量回测 — 最终版

修复所有已知错误模式:
1. := → = (赋值)
2. 中文变量名 → _C0000 映射
3. 函数名大小写规范化
4. 绘图指令剥离 (COLOR*, LINETHICK*, DRAWTEXT等)
5. AND/OR → &/|
6. || → |, && → &
7. 数字前导零处理
8. 行尾逗号+绘图指令剥离
9. 未知函数 → 0值stub
10. FINANCE/DYNAINFO → mock值
"""
import sys,os,re,json,warnings,time,traceback
warnings.filterwarnings('ignore')
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from utils.logger import get_logger
logger = get_logger(__name__)
TdxConnector.ensure_connected()

print("="*80)
print("  468公式批量回测 — 最终版")
print("="*80)

# ═══ 1. DATA ═══
print("\n[1] 加载数据...",flush=True)
all_codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(all_codes,'20240601','20260603',dividend_type="front",period="1d")
C=k["Close"].sort_index();H=k["High"].sort_index();L=k["Low"].sort_index()
O=k["Open"].sort_index();V=k.get("Volume",pd.DataFrame()).sort_index()
valid=C.notna().sum()>100
for d in[C,H,L,O,V]:d=d.loc[:,valid]
def ex_st(cl):return[c for c in cl if'ST'not in c and'*ST'not in c]
univ=ex_st([c for c in C.columns])
print(f"  {C.shape} | 股票池:{len(univ)}只")

# ═══ 2. TDX FUNCTIONS ═══
def _ma(x,n):return x.rolling(int(n),min_periods=1).mean()
def _ema(x,n):return x.ewm(span=int(n),adjust=False).mean()
def _ref(x,n):return x.shift(int(n))
def _hhv(x,n):return x.rolling(int(n),min_periods=1).max()
def _llv(x,n):return x.rolling(int(n),min_periods=1).min()
def _cross(a,b):return(a>b)&(a.shift(1)<=b.shift(1))
def _count(x,n):return x.astype(float).rolling(int(n),min_periods=1).sum()
def _sum(x,n):return x.rolling(int(n),min_periods=1).sum()
def _every(x,n):return(x>0).rolling(int(n),min_periods=1).min()>0
def _exist(x,n):return _count(x>0,int(n))>0
def _barslast(x):
    r=pd.DataFrame(np.nan,index=x.index,columns=x.columns)
    for col in x.columns:
        v=x[col].values;o=np.full(len(v),np.nan);lt=-1
        for i in range(len(v)):
            if v[i]and not np.isnan(v[i]):lt=i
            if lt>=0:o[i]=i-lt
        r[col]=o
    return r
def _filter(x,n):
    r=x.copy()
    for col in x.columns:
        v=x[col].values.astype(bool);o=np.zeros(len(v),dtype=bool);lt=-int(n)-1
        for i in range(len(v)):
            if v[i]and(i-lt)>int(n):o[i]=True;lt=i
        r[col]=o
    return r
def _abs(x):return x.abs()
def _max(a,b):
    av=a.values if isinstance(a,pd.DataFrame)else a
    bv=b.values if isinstance(b,pd.DataFrame)else b
    if isinstance(av,(int,float))and isinstance(bv,(int,float)):return max(av,bv)
    return pd.DataFrame(np.maximum(av,bv),index=a.index if isinstance(a,pd.DataFrame)else b.index,columns=a.columns if isinstance(a,pd.DataFrame)else b.columns)
def _min(a,b):
    av=a.values if isinstance(a,pd.DataFrame)else a
    bv=b.values if isinstance(b,pd.DataFrame)else b
    if isinstance(av,(int,float))and isinstance(bv,(int,float)):return min(av,bv)
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
def _std(x,n):return x.rolling(int(n),min_periods=1).std()
def _var(x,n):return x.rolling(int(n),min_periods=1).var()
def _barslastcount(x):return _barslast(~x.astype(bool))
def _barssincen(x,n):return _count(~(x.astype(bool)),int(n))
def _between(x,lo,hi):return(x>=float(lo))&(x<=float(hi))
def _ztprice(c,lim=0.1):return c*(1+(float(lim)if isinstance(lim,(int,float))else 0.1))
def _finance(n):
    r=pd.DataFrame(1.0,index=C.index,columns=C.columns)
    if n==3:
        for col in C.columns:r[col]=4.0 if col.startswith('688')else 1.0
    return r
def _dynainfo(n):return pd.DataFrame(0.0,index=C.index,columns=C.columns)
def _capital():return V.rolling(20).mean()*20/C
def _hsl():return(C/C.shift(1)-1).abs()*100
def _codelike(p):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _namelike(p):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _inblock(p):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _longcross(a,b,n):return(a>b)&(a.shift(int(n))<=b.shift(int(n)))
def _atan(x):return np.arctan(x)*180/np.pi
def _round(x,n=0):return np.round(x,int(n))
def _intpart(x):return np.floor(x)
def _mod(a,b):return a%float(b)
def _forcast(x,n):
    return x.rolling(int(n),min_periods=1).apply(
        lambda v:np.polyval(np.polyfit(np.arange(len(v)),v,1),len(v))if len(v)==int(n)else np.nan,raw=True)
def _sar(*a):return L
def _zig(x,n):return x
def _peak(x,n,m):return x
def _trough(x,n,m):return x
def _backset(x,n):return x
def _dma(x,a):return _sma(x,int(a),1)
def _stubDf(*a,**k):return pd.DataFrame(0.0,index=C.index,columns=C.columns)
def _stubFalse(*a,**k):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _stubNum(*a,**k):return 0.0

# 所有支持的函数(大写key)
ALL_FUNCS={
    'MA':_ma,'EMA':_ema,'SMA':_sma,'REF':_ref,'HHV':_hhv,'LLV':_llv,
    'CROSS':_cross,'COUNT':_count,'EVERY':_every,'EXIST':_exist,'SUM':_sum,
    'ABS':_abs,'MAX':_max,'MIN':_min,'BARSLAST':_barslast,'FILTER':_filter,
    'UPNDAY':_upnday,'BETWEEN':_between,'NOT':lambda x:~x.astype(bool),
    'STD':_std,'VAR':_var,'BARSLASTCOUNT':_barslastcount,'BARSSINCEN':_barssincen,
    'LONGCROSS':_longcross,'ATAN':_atan,
    'TAN':lambda x:np.tan(x),'COS':lambda x:np.cos(x),'SIN':lambda x:np.sin(x),
    'LN':lambda x:np.log(x.clip(1e-10,None)),'LOG':lambda x:np.log(x.clip(1e-10,None)),
    'SQRT':lambda x:np.sqrt(x.clip(0,None)),'POW':lambda x,n:np.power(x,float(n)),
    'EXP':lambda x:np.exp(x.clip(-20,20)),'MOD':_mod,
    'INTPART':_intpart,'ROUND':_round,'FLOOR':lambda x:np.floor(x),
    'CEILING':lambda x:np.ceil(x),'REVERSE':lambda x:-x,'RANGE':lambda x,n:_hhv(x,n)-_llv(x,n),
    'ZTPRICE':_ztprice,'FINANCE':_finance,'DYNAINFO':_dynainfo,
    'CAPITAL':_capital,'HSL':_hsl,'CODELIKE':_codelike,'NAMELIKE':_namelike,'INBLOCK':_inblock,
    'HHVBARS':_stubDf,'LLVBARS':_stubDf,'DMA':_dma,'WMA':_ma,'EXPMA':_ema,'EXPMEMA':_ema,
    'MEMA':_ema,'FORCAST':_forcast,'SLOPE':lambda x,n:(x-x.shift(int(n)))/int(n),
    'SAR':_sar,'ZIG':_zig,'PEAK':_peak,'TROUGH':_trough,'BACKSET':_backset,
    'FINDHIGH':_stubDf,'FINDHIGHBARS':_stubDf,'FINDLOW':_stubDf,'FINDLOWBARS':_stubDf,
    'BARSCOUNT':_stubDf,'CONST':lambda x:x.iloc[-1],'CURRBARSCOUNT':_stubNum,
    'DATETODAY':_stubNum,'REFDATE':_ref,'TP':_stubDf,'K':C,
    'IF':None,  # 特殊处理
}

# ═══ 3. PREPROCESSOR ═══
def preprocess_formula(code):
    """将TDX公式代码转为可exec的Python代码. 返回(python_code, output_var_name)"""
    lines=code.strip().split('\n')
    processed=[]
    output_var=None

    # Pass 1: 收集所有变量名(含中文) → 创建映射表
    all_vars=set()
    for line in lines:
        line=line.strip()
        if not line or line.startswith('{'):continue
        line=line.rstrip(';')
        for marker in[':=',':']:
            if marker in line:
                var=line.split(marker,1)[0].strip()
                if var and not any(c in var for c in'><=+-*/()[]{}&|!,.')and not var[0].isdigit():
                    all_vars.add(var)
                break

    # 中文→安全映射
    cn_map={}
    cn_idx=0
    for v in sorted(all_vars,key=lambda x:-len(x)):
        if any(ord(c)>127 for c in v)or not v.isidentifier()or v.upper()in ALL_FUNCS:
            safe=f'_C{cn_idx:04d}'
            cn_map[v]=safe;cn_idx+=1

    # Pass 2: 处理每一行
    for line in lines:
        line=line.strip()
        if not line or line.startswith('{'):continue
        line=line.rstrip(';')

        # 跳过纯绘图行
        upper=line.upper()
        if any(upper.startswith(kw)for kw in['DRAW','STICK','PLOYLINE','DRAWTEXT','DRAWNUMBER',
            'DRAWICON','DRAWKLINE','DRAWBAND','FILLRGN','VERTLINE','DRAWNULL','NODRAW',
            'ALIGNRIGHT','CIRCLEDOT','POINTDOT','DOTLINE','CROSSDOT','VOLSTICK','COLORSTICK',
            'PARTLINE']):continue

        # 剥离行尾绘图指令: ,COLORxxx, LINETHICKx, COLORYELLOW等
        line=re.sub(r',\s*COLOR\w*','',line,flags=re.IGNORECASE)
        line=re.sub(r',\s*LINETHICK\d*','',line,flags=re.IGNORECASE)
        line=re.sub(r',\s*DOTLINE','',line,flags=re.IGNORECASE)
        line=re.sub(r',\s*NODRAW','',line,flags=re.IGNORECASE)
        line=re.sub(r',\s*CIRCLEDOT','',line,flags=re.IGNORECASE)
        line=re.sub(r',\s*POINTDOT','',line,flags=re.IGNORECASE)
        line=re.sub(r',\s*STICK','',line,flags=re.IGNORECASE)
        line=re.sub(r',\s*VOLSTICK','',line,flags=re.IGNORECASE)
        line=re.sub(r',\s*COLORSTICK','',line,flags=re.IGNORECASE)

        # 替换中文变量(先替换长的，避免部分匹配)
        # 按长度降序排列
        cn_sorted=sorted(cn_map.items(),key=lambda x:-len(x[0]))
        for cn,safe in cn_sorted:
            if cn in line:
                # 使用word boundary
                line=re.sub(r'\b'+re.escape(cn)+r'\b',safe,line)

        # 处理 :=  和 :
        is_output=False
        if ':='in line:
            var,expr=line.split(':=',1)
            var=var.strip();expr=expr.strip()
            line=f'{var} = {expr}'
        elif ':'in line:
            parts=line.split(':',1)
            pvar=parts[0].strip()
            if pvar and not any(c in pvar for c in'><=+-*/()[]{}&|!,.')and not pvar[0].isdigit():
                pexpr=parts[1].strip()if len(parts)>1 else''
                line=f'{pvar} = {pexpr}'
                output_var=pvar
                is_output=True

        # 替换运算符
        line=line.replace('&&','&').replace('||','|')
        line=re.sub(r'\bAND\b','&',line,flags=re.IGNORECASE)
        line=re.sub(r'\bOR\b','|',line,flags=re.IGNORECASE)
        line=line.replace('<>','!=')

        # 修复前导零数字: 000 → 0, 001 → 1, etc (但不在字符串内)
        line=re.sub(r'(?<!\w)0+(\d+)(?!\w)',r'\1',line)

        # 规范化函数名(首字母大写，其余保持原样以避免CROSS→Cross问题)
        # 实际上,使用大小写不敏感的查找
        words=re.findall(r'\b([A-Za-z_]\w*)\b',line)
        for w in words:
            if w.upper()in ALL_FUNCS and w!=w.upper():
                # func() → 统一转大写
                line=re.sub(r'\b'+re.escape(w)+r'\b',w.upper(),line)
            elif w in cn_map:
                # 变量名已映射
                line=re.sub(r'\b'+re.escape(w)+r'\b',cn_map[w],line)

        processed.append(line)

    if output_var is None:
        for pl in reversed(processed):
            if'='in pl and'=='not in pl:
                output_var=pl.split('=',1)[0].strip()
                break

    return'\n'.join(processed),output_var

# ═══ 4. ENGINE ═══
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

def run_one(fname):
    """处理一个公式文件: 读取→预处理→exec→回测"""
    with open(fname,'r',encoding='utf-8')as f:content=f.read()
    nm=re.search(r'^#\s*(.+)',content,re.MULTILINE)
    title=nm.group(1).strip()[:60]if nm else os.path.basename(fname).replace('.md','')[:60]
    cm=re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`',content,re.DOTALL)
    if not cm:return None,title,'无代码块'

    code_py,out_var=preprocess_formula(cm.group(1))
    if out_var is None:return None,title,'无输出变量'

    # exec
    loc={'C':C,'CLOSE':C,'O':O,'OPEN':O,'H':H,'HIGH':H,'L':L,'LOW':L,'V':V,'VOL':V,
         'np':np,'pd':pd,'True':True,'False':False,
         'abs':abs,'max':max,'min':min,'round':round,'sum':sum,'len':len,
         'int':int,'float':float,'str':str,'bool':bool,'list':list,'dict':dict,
         'pow':pow,'any':any,'all':all}
    loc.update(ALL_FUNCS)

    # IF函数特殊注入
    def _if(cond,t,f):
        c=cond.values.astype(bool)if isinstance(cond,pd.DataFrame)else np.array(cond,dtype=bool)
        tv=t.values if isinstance(t,pd.DataFrame)else t
        fv=f.values if isinstance(f,pd.DataFrame)else f
        if isinstance(tv,np.ndarray)and isinstance(fv,np.ndarray):
            return pd.DataFrame(np.where(c,tv,fv),index=cond.index if isinstance(cond,pd.DataFrame)else C.index,columns=cond.columns if isinstance(cond,pd.DataFrame)else C.columns)
        return pd.DataFrame(np.where(c,tv,fv),index=C.index,columns=C.columns)
    loc['IF']=_if

    try:
        exec(code_py,{'__builtins__':{}},loc)
        result=loc.get(out_var)
        if isinstance(result,pd.DataFrame):
            if result.dtypes.iloc[0]!=bool:result=result>0.5
            return result.astype(bool),title,None
        # 标量结果
        r=pd.DataFrame(result>0,index=C.index,columns=C.columns)if isinstance(result,(int,float))else None
        return r,title,None
    except Exception as e:
        return None,title,f'{type(e).__name__}:{str(e)[:60]}'

# ═══ 5. BATCH ═══
print("\n[2] 批量处理...",flush=True)
gongshi_dir=r'E:\gongshi'
files=sorted([f for f in os.listdir(gongshi_dir) if f.endswith('.md')])
total=len(files)
results=[]
ok=0;pf=0;sf=0;bf=0
t0=time.time()

for fi,fname in enumerate(files):
    fp=os.path.join(gongshi_dir,fname)
    sig_df,title,err=run_one(fp)

    if sig_df is None:
        if '无代码块' in str(err)or'无输出变量' in str(err):pf+=1
        else:pf+=1
        if fi%30==0:
            elapsed=time.time()-t0
            print(f"  [{fi+1}/{total}] OK:{ok} 解析:{pf} 信号:{sf} BT:{bf} | ~{(fi+1)/elapsed*60:.0f}/min",flush=True)
        continue

    sig_bt=sig_df.loc['20260101':]
    ts=int(sig_bt.sum().sum())
    if ts<5:sf+=1
    if fi%30==0:
        elapsed=time.time()-t0
        print(f"  [{fi+1}/{total}] OK:{ok} 解析:{pf} 信号:{sf} BT:{bf} | ~{(fi+1)/elapsed*60:.0f}/min",flush=True)
    if ts<5:continue

    try:
        recs=[]
        for col in univ:
            if col not in sig_bt.columns:continue
            for idx in sig_bt.index[sig_bt[col]]:
                recs.append({'stock_code':col,'select_date':idx.strftime('%Y-%m-%d')})
        sel=pd.DataFrame(recs)
        if len(sel)<10:sf+=1;continue

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
            'file':fname,'title':title,'signals':ts,
            'cumret':brs['cumulative_return'],'annret':m['annualized_return'],
            'maxdd':m['max_drawdown'],'sharpe':m['sharpe_ratio'],
            'winrate':m['win_rate'],'trades':len(brs['trades']),
        })
        ok+=1
    except Exception as e: logger.warning("Backtest failed: %s", e);bf+=1

# ═══ 6. OUTPUT ═══
print(f"\n[3] 完成! OK:{ok} 解析:{pf} 信号:{sf} BT:{bf}",flush=True)
results.sort(key=lambda r:r['annret']if r['annret']is not None else-999,reverse=True)

print("\n"+"="*100)
print(f"  TOP 100 | 2026-01-01~2026-06-03 | {len(univ)}只(无ST)")
print("="*100)
print(f"  {'#':<5} {'公式':<44} {'收益%':>7} {'年化%':>7} {'回撤%':>7} {'夏普':>5} {'胜率':>6} {'交易':>6} {'信号':>6}")
print("  "+"-"*100)
for i,r in enumerate(results[:100],1):
    print(f"  {i:<5} {r['title'][:42]:<44} {r['cumret']*100:>+7.2f} {r['annret']*100:>+7.2f} "
          f"{r['maxdd']*100:>+7.2f} {r['sharpe']:>5.2f} {r['winrate']*100:>6.1f} {r['trades']:>6} {r['signals']:>6}")

output={'config':{'period':'2026-01-01~2026-06-03'},'top100':results[:100],'all':results}
with open('output/batch_top100_final.json','w',encoding='utf-8')as f:
    json.dump(output,f,ensure_ascii=False,indent=2,default=str)
print(f"\n  已保存: output/batch_top100_final.json ({len(results)}个)")
print("="*80)
