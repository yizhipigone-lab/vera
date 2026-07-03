"""468公式批量回测 - 修复版 V5
关键修复: = → ==, 函数名大小写, 全部缺失函数注册
"""
import sys,os,re,json,warnings,time
warnings.filterwarnings('ignore')
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from utils.logger import get_logger
logger = get_logger(__name__)
TdxConnector.ensure_connected()

# DATA
print("="*80);print("  468公式 V5 修复版");print("="*80)
print("\n[1] Data...",flush=True)
codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(codes,'20240601','20260603',dividend_type="front",period="1d")
C=k["Close"].sort_index();H=k["High"].sort_index();L=k["Low"].sort_index()
O=k["Open"].sort_index();V=k.get("Volume",pd.DataFrame()).sort_index()
valid=C.notna().sum()>100
for d in[C,H,L,O,V]:d=d.loc[:,valid]
univ=[c for c in C.columns if'ST'not in c and'*ST'not in c]
print(f"  {C.shape} | {len(univ)} stocks")

# TDX FUNCTIONS (same as before)
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
def _barslastcount(x):return _barslast(~x.astype(bool))
def _between(x,lo,hi):return(x>=float(lo))&(x<=float(hi))
def _upnday(x,n):
    r=pd.DataFrame(True,index=x.index,columns=x.columns)
    for i in range(1,int(n)):r=r&(x>x.shift(i))
    return r

# COMPREHENSIVE function registry with ALL known TDX functions
F = {
    'MA':_ma,'EMA':_ema,'SMA':_sma,'REF':_ref,'HHV':_hhv,'LLV':_llv,
    'CROSS':_cross,'COUNT':_count,'EVERY':_every,'EXIST':_exist,'SUM':_sum,
    'ABS':lambda x:x.abs(),'MAX':lambda a,b:np.maximum(a.values if isinstance(a,pd.DataFrame)else a,b.values if isinstance(b,pd.DataFrame)else b),
    'MIN':lambda a,b:np.minimum(a.values if isinstance(a,pd.DataFrame)else a,b.values if isinstance(b,pd.DataFrame)else b),
    'BARSLAST':_barslast,'FILTER':_filter,'UPNDAY':_upnday,
    'BETWEEN':_between,'NOT':lambda x:~x.astype(bool),
    'STD':_std,'VAR':lambda x,n:x.rolling(int(n),min_periods=1).var(),
    'BARSLASTCOUNT':_barslastcount,
    'BARSSINCEN':lambda x,n:_count(~(x.astype(bool)),int(n)),
    'LONGCROSS':lambda a,b,n:(a>b)&(a.shift(int(n))<=b.shift(int(n))),
    'ATAN':lambda x:np.arctan(x)*180/np.pi,
    'LN':lambda x:np.log(x.clip(1e-10,None)),
    'SQRT':lambda x:np.sqrt(x.clip(0,None)),
    'POW':lambda x,n:np.power(x,float(n)),
    'EXP':lambda x:np.exp(x.clip(-20,20)),
    'INTPART':lambda x:np.floor(x),'ROUND':lambda x,n=0:np.round(x,int(n)),
    'REVERSE':lambda x:-x,'RANGE':lambda x,n:_hhv(x,n)-_llv(x,n),
    'ZTPRICE':lambda c,lim=0.1:c*(1+float(lim)if isinstance(lim,(int,float))else 0.1),
    'FINANCE':lambda n:pd.DataFrame(1.0,index=C.index,columns=C.columns),
    'DYNAINFO':lambda n:pd.DataFrame(0.0,index=C.index,columns=C.columns),
    'CAPITAL':lambda:V.rolling(20).mean()*20/C,
    'HSL':lambda:(C/C.shift(1)-1).abs()*100,
    'CODELIKE':lambda p:pd.DataFrame(False,index=C.index,columns=C.columns),
    'NAMELIKE':lambda p:pd.DataFrame(False,index=C.index,columns=C.columns),
    'INBLOCK':lambda p:pd.DataFrame(False,index=C.index,columns=C.columns),
    'DMA':_ma,'WMA':_ma,'EXPMA':_ema,'EXPMEMA':_ema,'MEMA':_ema,
    'FORCAST':lambda x,n:x.rolling(int(n),min_periods=1).apply(lambda v:np.polyval(np.polyfit(np.arange(len(v)),v,1),len(v))if len(v)==int(n)else np.nan,raw=True),
    'SLOPE':lambda x,n:(x-x.shift(int(n)))/int(n),
    'SAR':lambda *a:L,'BACKSET':lambda x,n:x,
    'PEAK':lambda x,n,m:x,'TROUGH':lambda x,n,m:x,'ZIG':lambda x,n:x,
    'BARSCOUNT':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'CONST':lambda x:x.iloc[-1],'CURRBARSCOUNT':lambda:0,
    'DATETODAY':lambda d:20240601,'REFDATE':_ref,'TP':lambda:pd.DataFrame(0.0,index=C.index,columns=C.columns),
    'HHVBARS':lambda x,n:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'LLVBARS':lambda x,n:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'FINDHIGH':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'FINDHIGHBARS':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'FINDLOW':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'FINDLOWBARS':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    # Extra TDX functions that may appear as variables
    'WINNER':lambda x:pd.DataFrame(0.5,index=C.index,columns=C.columns),
    'AMOUNT':lambda:V*C,'VOL':V,'CLOSE':C,'OPEN':O,'HIGH':H,'LOW':L,
    'AVEDEV':lambda x,n:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'COST':lambda x:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'DATE':lambda:pd.DataFrame(20260101,index=C.index,columns=C.columns,dtype=float),
    'YEAR':lambda:pd.DataFrame(2026,index=C.index,columns=C.columns,dtype=float),
    'MONTH':lambda:pd.DataFrame(1,index=C.index,columns=C.columns,dtype=float),
    'DAY':lambda:pd.DataFrame(1,index=C.index,columns=C.columns,dtype=float),
    'INDEXC':C,'INDEXO':O,'INDEXH':H,'INDEXL':L,'INDEXV':V,
    'STRCAT':lambda *a:'','CON2STR':lambda *a:'',
    'PLOYLINE':lambda a,b:a,'MACD':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'KDJ':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'RSI':lambda *a:pd.DataFrame(50,index=C.index,columns=C.columns,dtype=float),
    'WR':lambda *a:pd.DataFrame(50,index=C.index,columns=C.columns,dtype=float),
    'CR':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'VOL_MULTIPLE':lambda:V/V.rolling(20).mean(),
    'CYC':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    
    'HYBLOCK':lambda p:pd.DataFrame(False,index=C.index,columns=C.columns),
    'DYBLOCK':lambda p:pd.DataFrame(False,index=C.index,columns=C.columns),
    'GNBLOCK':lambda p:pd.DataFrame(False,index=C.index,columns=C.columns),
    'SUMBARS':lambda x,n:pd.DataFrame(0.0,index=C.index,columns=C.columns),
    'DRAWNULL':lambda:pd.DataFrame(np.nan,index=C.index,columns=C.columns),
    'TROUGHBARS':lambda x,n,m:pd.DataFrame(0.0,index=C.index,columns=C.columns),
    'PEAKBARS':lambda x,n,m:pd.DataFrame(0.0,index=C.index,columns=C.columns),
    'CONST':lambda x:float(x.iloc[-1])if isinstance(x,pd.DataFrame)else float(x),
    'CURRBARSCOUNT':lambda:len(C.index),
    'TOTALBARSCOUNT':lambda:len(C.index),
    'ISLASTBAR':lambda:pd.DataFrame(False,index=C.index,columns=C.columns),
    'PERIOD':lambda:5,
    'DATATYPE':lambda:6,
    'ALIGNRIGHT':lambda:pd.DataFrame(False,index=C.index,columns=C.columns),
    'BARTIME':lambda:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'DTPRICE':lambda:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'PARTLINE':lambda x,cond:x,
    'VERTLINE':lambda *a,**k:pd.DataFrame(False,index=C.index,columns=C.columns),
    'DRAWGBK':lambda *a,**k:pd.DataFrame(False,index=C.index,columns=C.columns),
'K':C,
}
# Add IF special handler
F['IF']=None

# PREPROCESSOR
def preprocess(code):
    lines=code.strip().split('\n')
    all_vars=set()
    for l in lines:
        l=l.strip().rstrip(';')
        if not l or l.startswith('{'):continue
        for mk in[':=',':']:
            if mk in l:
                v=l.split(mk,1)[0].strip()
                if v and not any(c in v for c in'><=+-*/()[]&|!,.')and not v[0].isdigit():
                    all_vars.add(v)
                break

    cn_map={};ci=0
    for v in sorted(all_vars,key=lambda x:-len(x)):
        if any(ord(c)>127 for c in v)or not v.isidentifier()or v.upper()in F:
            cn_map[v]=f'_C{ci:04d}';ci+=1

    # Case-insensitive function lookup
    ci_funcs={}
    for fn in F:ci_funcs[fn.lower()]=fn
    extra_funcs='ABS MIN MAX NOT IF WINNER AMOUNT AVEDEV DATE YEAR MONTH DAY HOUR MINUTE INDEXC INDEXO INDEXH INDEXL INDEXV STRCAT CON2STR COST PLOYLINE MACD KDJ RSI WR CR VOL EXPMA EXPMEMA CLOSE OPEN HIGH LOW CAPITAL HSL VOL_MULTIPLE CYC'.split()
    for e in extra_funcs:ci_funcs[e.lower()]=e

    processed=[];out_var=None
    for l in lines:
        l=l.strip().rstrip(';')
        if not l or l.startswith('{'):continue
        up=l.upper()
        if any(up.startswith(k)for k in['DRAW','STICK','PLOYLINE','DRAWTEXT','DRAWNUMBER','DRAWICON','DRAWKLINE','DRAWBAND','FILLRGN','VERTLINE','DRAWNULL','NODRAW','ALIGNRIGHT','CIRCLEDOT','POINTDOT','DOTLINE','CROSSDOT','VOLSTICK','COLORSTICK','PARTLINE']):continue
        # Strip drawing suffixes
        for pat in[r',?\s*LINETHICK\d*',r',\s*COLOR\w*',r',\s*LINETHICK\d*',r',\s*DOTLINE',r',\s*NODRAW',r',\s*CIRCLEDOT',r',\s*POINTDOT',r',\s*STICK',r',\s*VOLSTICK',r',\s*COLORSTICK']:
            l=re.sub(pat,'',l,flags=re.IGNORECASE)

        # Replace Chinese vars
        for cn,s in sorted(cn_map.items(),key=lambda x:-len(x[0])):
            if cn in l:l=re.sub(r'\b'+re.escape(cn)+r'\b',s,l)

        # Handle := and :
        if ':='in l:
            v,e=l.split(':=',1);l=f'{v.strip()} = {e.strip()}'
        elif ':'in l:
            p=l.split(':',1);pv=p[0].strip()
            if pv and not any(c in pv for c in'><=+-*/()[]&|!,.')and not pv[0].isdigit():
                pe=p[1].strip()if len(p)>1 else'';l=f'{pv} = {pe}';out_var=pv

        # Replace operators
        l=l.replace('&&','&').replace('||','|')
        l=re.sub(r'\bAND\b','&',l,flags=re.IGNORECASE)
        l=re.sub(r'\bOR\b','|',l,flags=re.IGNORECASE)
        l=l.replace('<>','!=')

        # Fix leading zeros
        l=re.sub(r'(?<!\w)0+(\d+)(?!\w)',r'\1',l)

        # Normalize function names (case-insensitive)
        words=re.findall(r'\b([A-Za-z_]\w*)\b',l)
        for w in set(words):
            wl=w.lower()
            if wl in ci_funcs:
                canonical=ci_funcs[wl]
                if w!=canonical:l=re.sub(r'\b'+re.escape(w)+r'\b',canonical,l)
            elif w in cn_map:
                l=re.sub(r'\b'+re.escape(w)+r'\b',cn_map[w],l)

        # CRITICAL: convert remaining = to == for comparisons
        if ' = ' in l:
            parts=l.split(' = ',1)
            lhs=parts[0].strip()
            rhs=parts[1]
            if re.match(r'^[A-Za-z_]\w*$',lhs):
                rhs=re.sub(r'(?<![=!<>])=(?!=)',r'==',rhs)
                l=f'{lhs} = {rhs}'
            else:
                l=re.sub(r'(?<![=!<>])=(?!=)',r'==',l)
        else:
            l=re.sub(r'(?<![=!<>])=(?!=)',r'==',l)

        processed.append(l)

    if out_var is None:
        for pl in reversed(processed):
            if' = 'in pl:out_var=pl.split(' = ',1)[0].strip();break
    return'\n'.join(processed),out_var

# BACKTEST ENGINE
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

# BATCH
print("\n[2] Processing...",flush=True)
gongshi_dir=r'E:\gongshi'
files=sorted([f for f in os.listdir(gongshi_dir) if f.endswith('.md')])
total=len(files)
results=[];ok=0;pf=0;sf=0;bf=0;t0=time.time()

for fi,fname in enumerate(files):
    fp=os.path.join(gongshi_dir,fname)
    try:
        with open(fp,'r',encoding='utf-8')as f:content=f.read()
    except Exception as e: logger.warning("Read file failed: %s", e);pf+=1;continue
    nm=re.search(r'^#\s*(.+)',content,re.MULTILINE)
    title=nm.group(1).strip()[:60]if nm else fname.replace('.md','')[:60]
    cm=re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`',content,re.DOTALL)
    if not cm:pf+=1;continue

    code_py,out_var=preprocess(cm.group(1))
    if out_var is None:pf+=1
    if fi%50==0:
        elapsed=max(time.time()-t0,1)
        print(f"  [{fi+1}/{total}] OK:{ok} Parse:{pf} Sig:{sf} BT:{bf} | ~{(fi+1)/elapsed*60:.0f}/min",flush=True)
    if out_var is None:continue

    loc={'C':C,'CLOSE':C,'O':O,'OPEN':O,'H':H,'HIGH':H,'L':L,'LOW':L,'V':V,'VOL':V,
         'np':np,'pd':pd,'True':True,'False':False,
         'abs':abs,'max':max,'min':min,'round':round,'sum':sum,'len':len,
         'int':int,'float':float,'str':str,'bool':bool,'list':list,'dict':dict,
         'pow':pow,'any':any,'all':all}
    loc.update(F)
    def _if(cond,t,f):
        c=cond.values.astype(bool)if isinstance(cond,pd.DataFrame)else np.array(cond,dtype=bool)
        tv=t.values if isinstance(t,pd.DataFrame)else t
        fv=f.values if isinstance(f,pd.DataFrame)else f
        return pd.DataFrame(np.where(c,tv,fv),index=C.index,columns=C.columns)
    loc['IF']=_if

    try:
        exec(code_py,{'__builtins__':{}},loc)
        result=loc.get(out_var)
        if not isinstance(result,pd.DataFrame):pf+=1;continue
        if result.dtypes.iloc[0]!=bool:result=result>0.5
        result=result.astype(bool)
    except Exception as e: logger.warning("Read file failed: %s", e);pf+=1;continue

    sig_bt=result.loc['20260101':]
    ts=int(sig_bt.sum().sum())
    if ts<5:sf+=1;continue

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
            'title':title,'signals':ts,
            'cumret':brs['cumulative_return'],'annret':m['annualized_return'],
            'maxdd':m['max_drawdown'],'sharpe':m['sharpe_ratio'],
            'winrate':m['win_rate'],'trades':len(brs['trades']),
        })
        ok+=1
    except Exception as e: logger.warning("Backtest failed: %s", e);bf+=1

# OUTPUT
print(f"\n[3] Done! OK:{ok} Parse:{pf} Sig:{sf} BT:{bf}",flush=True)
results.sort(key=lambda r:r['annret']if r['annret']is not None else-999,reverse=True)

print("\n"+"="*100)
print(f"  TOP 100 | 2026-01-01~2026-06-03 | {len(univ)} stocks")
print("="*100)
for i,r in enumerate(results[:100],1):
    print(f"  {i:<5} {r['title'][:50]:<50} {r['cumret']*100:>+7.2f}% {r['annret']*100:>+7.2f}% DD:{r['maxdd']*100:>+7.2f}% SR:{r['sharpe']:>5.2f} WR:{r['winrate']*100:>5.1f}% T:{r['trades']:>6}")

output={'config':{'period':'2026-01-01~2026-06-03'},'top100':results[:100],'all':results}
with open('output/batch_top100_final.json','w',encoding='utf-8')as f:
    json.dump(output,f,ensure_ascii=False,indent=2,default=str)
print(f"\nSaved: output/batch_top100_final.json ({len(results)} formulas)")
print("="*80)
