"""23 B-pinyin formulas translation + backtest — expression-only approach"""
import sys,os,re,time,json
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np
from core.connector import TdxConnector;from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
TdxConnector.ensure_connected()

# Data
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

# TDX functions
def _ma(x,n):return x.rolling(int(n),min_periods=1).mean()
def _ema(x,n):return x.ewm(span=int(n),adjust=False).mean()
def _ref(x,n):return x.shift(int(n))
def _hhv(x,n):return x.rolling(int(n),min_periods=1).max()
def _llv(x,n):return x.rolling(int(n),min_periods=1).min()
def _cross(a,b):return(a>b)&(a.shift(1)<=b.shift(1))
def _count(x,n):return x.astype(float).rolling(int(n),min_periods=1).sum()
def _every(x,n):return(x>0).rolling(int(n),min_periods=1).min()>0
def _exist(x,n):return _count(x>0,int(n))>0
def _filter(x,n):
    r=x.copy()
    for col in x.columns:v=x[col].values.astype(bool);o=np.zeros(len(v),dtype=bool);lt=-int(n)-1
    for i in range(len(v)):
        if v[i]and(i-lt)>int(n):o[i]=True;lt=i
    r[col]=o;return r
def _barslast(x):
    r=pd.DataFrame(np.nan,index=x.index,columns=x.columns)
    for col in x.columns:v=x[col].values;o=np.full(len(v),np.nan);lt=-1
    for i in range(len(v)):
        if v[i]and not np.isnan(v[i]):lt=i
        if lt>=0:o[i]=i-lt
    r[col]=o;return r
def _abs(x):return x.abs()
def _max(a,b):
    av=a.values if isinstance(a,pd.DataFrame)else a;bv=b.values if isinstance(b,pd.DataFrame)else b
    return pd.DataFrame(np.maximum(av,bv),index=a.index if isinstance(a,pd.DataFrame)else b.index,columns=a.columns if isinstance(a,pd.DataFrame)else b.columns)
def _min(a,b):
    av=a.values if isinstance(a,pd.DataFrame)else a;bv=b.values if isinstance(b,pd.DataFrame)else b
    return pd.DataFrame(np.minimum(av,bv),index=a.index if isinstance(a,pd.DataFrame)else b.index,columns=a.columns if isinstance(a,pd.DataFrame)else b.columns)
def _sma(x,n,m=1):
    r=x.copy()
    for col in x.columns:v=x[col].values.astype(float);s=np.full(len(v),np.nan);fv=np.where(~np.isnan(v))[0]
    if len(fv)>0:s[fv[0]]=v[fv[0]]
    for i in range(fv[0]+1,len(v)):s[i]=(m*v[i]+(n-m)*s[i-1])/n if not np.isnan(v[i])else s[i-1]
    r[col]=s;return r
def _pow(x,n):return x**float(n)
def _sqrt(x):return np.sqrt(x.clip(0,None))
def _between(x,lo,hi):return(x>=float(lo))&(x<=float(hi))
def _sum(x,n):return x.rolling(int(n),min_periods=1).sum()
def _if(c,t,f):
    cv=c.values.astype(bool)if isinstance(c,pd.DataFrame)else np.array(c,dtype=bool)
    tv=t.values if isinstance(t,pd.DataFrame)else t;fv=f.values if isinstance(f,pd.DataFrame)else f
    return pd.DataFrame(np.where(cv,tv,fv),index=C.index,columns=C.columns)
def _std(x,n):return x.rolling(int(n)).std()
def _forcast(x,n):
    return x.rolling(int(n),min_periods=1).apply(lambda v:np.polyval(np.polyfit(np.arange(len(v)),v,1),len(v))if len(v)==int(n)else np.nan,raw=True)
def _barslastcount(x):return _barslast(~x.astype(bool))
def _barssincen(x,n):return _count(~(x.astype(bool)),int(n))
# Stubs
def _ppart(x):return pd.DataFrame(50.0,index=C.index,columns=C.columns)
def _dynainfo(x):return pd.DataFrame(0.0,index=C.index,columns=C.columns)
def _codelike(x):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _namelike(x):return pd.DataFrame(False,index=C.index,columns=C.columns)
def _capital():return V.rolling(20).mean()*20/C
def _winner(x):return pd.DataFrame(0.5,index=C.index,columns=C.columns)
def _amount():return V*C
def _hsl():return(C/C.shift(1)-1).abs()*100
def _ztprice(c,lim=0.1):return c*(1+float(lim)if isinstance(lim,(int,float))else 0.1)

FUNCS={'MA':_ma,'EMA':_ema,'REF':_ref,'HHV':_hhv,'LLV':_llv,'CROSS':_cross,
    'COUNT':_count,'EVERY':_every,'EXIST':_exist,'FILTER':_filter,
    'BARSLAST':_barslast,'ABS':_abs,'MAX':_max,'MIN':_min,'SMA':_sma,
    'POW':_pow,'SQRT':_sqrt,'BETWEEN':_between,'SUM':_sum,'IF':_if,
    'STD':_std,'FORCAST':_forcast,'BARSLASTCOUNT':_barslastcount,'BARSSINCEN':_barssincen,
    'NOT':lambda x:~x.astype(bool),'ZTPRICE':_ztprice,
    'PPART':_ppart,'DYNAINFO':_dynainfo,'WINNER':_winner,'CAPITAL':_capital,
    'CODELIKE':_codelike,'NAMELIKE':_namelike,'HSL':_hsl,'AMOUNT':_amount,
    'expma':_ema,'EXPMA':_ema,'EXPMEMA':_ema,'DMA':_ma,'WMA':_ma,'MEMA':_ema,
    'crOSS':_cross,'cROSS':_cross,'Abs':_abs,'MAX':_max,'MIN':_min,
    'vol':V,'VOL':V,'MACD':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'SAR':lambda *a:L,'BACKSET':lambda x,n:x,
    'PEAK':lambda x,n,m:x,'TROUGH':lambda x,n,m:x,'ZIG':lambda x,n:x,
    'HHVBARS':lambda x,n:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'LLVBARS':lambda x,n:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'BARSCOUNT':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'CURRBARSCOUNT':lambda:0,'CONST':lambda x:float(x.iloc[-1]),
    'FINDHIGH':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'FINDLOW':lambda *a:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'SUMBARS':lambda x,n:pd.DataFrame(0,index=C.index,columns=C.columns,dtype=float),
    'RANGE':lambda x,n:_hhv(x,n)-_llv(x,n),
    'FINANCE':lambda n:pd.DataFrame(1.0,index=C.index,columns=C.columns),
    'RSI1':lambda *a:pd.DataFrame(50.0,index=C.index,columns=C.columns),
    'RSI':lambda *a:pd.DataFrame(50.0,index=C.index,columns=C.columns),
    'KDJ':lambda *a:pd.DataFrame(50.0,index=C.index,columns=C.columns),
    'WR':lambda *a:pd.DataFrame(50.0,index=C.index,columns=C.columns),
    'CR':lambda *a:pd.DataFrame(50.0,index=C.index,columns=C.columns),
    'LONGCROSS':lambda a,b,n:(a>b)&(a.shift(int(n))<=b.shift(int(n))),
    'ATAN':lambda x:np.arctan(x)*180/np.pi,
    'LN':lambda x:np.log(x.clip(1e-10,None)),
}#

def translate_formula(code):
    lines=code.strip().split('\n')
    var_map={};var_idx=0;out_var=None
    for l in lines:
        l=l.strip().rstrip(';')
        if not l or l.startswith('{'):continue
        if any(l.upper().startswith(k)for k in['DRAW','STICK','DRAWTEXT','DRAWNUMBER','DRAWICON','DRAWKLINE','DRAWBAND','FILLRGN','VERTLINE','DRAWNULL','NODRAW','ALIGNRIGHT','CIRCLEDOT','POINTDOT','DOTLINE','CROSSDOT','VOLSTICK','COLORSTICK','PARTLINE']):continue
        l=re.sub(r',\s*COLOR\w*','',l,flags=re.IGNORECASE)
        l=re.sub(r',\s*LINETHICK\d*','',l,flags=re.IGNORECASE)
        l=re.sub(r',\s*(DOTLINE|NODRAW|CIRCLEDOT|POINTDOT|STICK|VOLSTICK|COLORSTICK)','',l,flags=re.IGNORECASE)
        for mk in[':=',':']:
            if mk in l:
                var=l.split(mk,1)[0].strip()
                if var and not any(c in var for c in'><=+-*/()[]&|!,.')and not var[0].isdigit():
                    if var not in var_map:var_map[var]=f'v{var_idx}';var_idx+=1
                    if mk==':':out_var=var_map[var]
                break
    py_lines=[]
    for l in lines:
        l=l.strip().rstrip(';')
        if not l or l.startswith('{'):continue
        if any(l.upper().startswith(k)for k in['DRAW','STICK','DRAWTEXT','DRAWNUMBER','DRAWICON','DRAWKLINE','DRAWBAND','FILLRGN','VERTLINE','DRAWNULL','NODRAW','ALIGNRIGHT','CIRCLEDOT','POINTDOT','DOTLINE','CROSSDOT','VOLSTICK','COLORSTICK','PARTLINE']):continue
        l=re.sub(r',\s*COLOR\w*','',l,flags=re.IGNORECASE)
        l=re.sub(r',\s*LINETHICK\d*','',l,flags=re.IGNORECASE)
        l=re.sub(r',\s*(DOTLINE|NODRAW|CIRCLEDOT|POINTDOT|STICK|VOLSTICK|COLORSTICK)','',l,flags=re.IGNORECASE)
        for cn,py in sorted(var_map.items(),key=lambda x:-len(x[0])):l=l.replace(cn,py)
        if ':='in l:v,e=l.split(':=',1);l=f'{v.strip()} = {e.strip()}'
        elif ':'in l:
            p=l.split(':',1);pv=p[0].strip()
            if pv and not any(c in pv for c in'><=+-*/()[]&|!,.')and not pv[0].isdigit():
                pe=p[1].strip()if len(p)>1 else'';l=f'{pv} = {pe}'
        l=l.replace('&&','&').replace('||','|')
        l=re.sub(r'\bAND\b','&',l,flags=re.IGNORECASE)
        l=re.sub(r'\bOR\b','|',l,flags=re.IGNORECASE)
        l=l.replace('<>','!=')
        l=re.sub(r'(?<!\w)0+(\d+)(?!\w)',r'\1',l)
        if ' = ' in l:
            parts=l.split(' = ',1);rhs=parts[1]
            if re.match(r'^[A-Za-z_]\w*$',parts[0].strip()):
                rhs=re.sub(r'(?<![=!<>])=(?!=)',r'==',rhs)
                l=f'{parts[0].strip()} = {rhs}'
        py_lines.append(l)
    if out_var is None:
        for pl in reversed(py_lines):
            if' = 'in pl:out_var=pl.split(' = ')[0].strip();break
    if out_var is None:return None,None
    return'\n'.join(py_lines),out_var

# Process all B formulas
b_chars='爆波布倍北本白保暴必百博变背捕飙补半冰'
gongshi=r'E:\gongshi'
matches=sorted([f for f in os.listdir(gongshi) if f.endswith('.md') and re.sub(r'^gs_\d+_','',f)[0] in b_chars])
print(f'Processing {len(matches)} B-pinyin formulas...\n')

results=[];t0=time.time()
for fi,fname in enumerate(matches):
    with open(os.path.join(gongshi,fname),'r',encoding='utf-8')as f:content=f.read()
    nm=re.search(r'^#\s*(.+)',content,re.MULTILINE);title=nm.group(1)[:45]if nm else fname[:45]
    cm=re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`',content,re.DOTALL)
    if not cm:continue
    code_py,out_var=translate_formula(cm.group(1))
    if code_py is None:print(f'  [{fi+1:2d}] {title:<40s} NO_OUTPUT');continue
    loc={'C':C,'CLOSE':C,'O':O,'OPEN':O,'H':H,'HIGH':H,'L':L,'LOW':L,'V':V,'VOL':V,'np':np,'pd':pd,'True':True,'False':False,'abs':abs,'max':max,'min':min,'round':round,'sum':sum,'len':len,'int':int,'float':float,'str':str,'bool':bool,'list':list,'dict':dict,'pow':pow,'any':any,'all':all}
    loc.update(FUNCS)
    # Safe AND/OR that handle float DataFrames
    def _safe_and(a,b):
        if isinstance(a,pd.DataFrame)and a.dtypes.iloc[0]!=bool:a=a>0
        if isinstance(b,pd.DataFrame)and b.dtypes.iloc[0]!=bool:b=b>0
        if isinstance(a,(int,float)):a=bool(a)
        if isinstance(b,(int,float)):b=bool(b)
        return a&b
    def _safe_or(a,b):
        if isinstance(a,pd.DataFrame)and a.dtypes.iloc[0]!=bool:a=a>0
        if isinstance(b,pd.DataFrame)and b.dtypes.iloc[0]!=bool:b=b>0
        if isinstance(a,(int,float)):a=bool(a)
        if isinstance(b,(int,float)):b=bool(b)
        return a|b
    loc['AND']=_safe_and;loc['OR']=_safe_or
    # Monkey-patch operator to handle float DataFrame in &/|
    import operator as _op
    _orig_and=_op.and_;_orig_or=_op.or_
    def _new_and(a,b):
        if isinstance(a,pd.DataFrame)and a.dtypes.iloc[0]!=bool:a=a>0
        if isinstance(b,pd.DataFrame)and b.dtypes.iloc[0]!=bool:b=b>0
        if isinstance(a,(int,float,np.floating)):a=bool(a)
        if isinstance(b,(int,float,np.floating)):b=bool(b)
        return _orig_and(a,b)
    def _new_or(a,b):
        if isinstance(a,pd.DataFrame)and a.dtypes.iloc[0]!=bool:a=a>0
        if isinstance(b,pd.DataFrame)and b.dtypes.iloc[0]!=bool:b=b>0
        if isinstance(a,(int,float,np.floating)):a=bool(a)
        if isinstance(b,(int,float,np.floating)):b=bool(b)
        return _orig_or(a,b)
    _op.and_=_new_and;_op.or_=_new_or
    # Fix: replace residual & and | (that aren't inside function calls) with safe versions
    # This is complex, so instead add pre-processing: wrap expressions with auto-bool
    # Simpler: add __bool_conversion in expressions
    try:
        exec(code_py,{'__builtins__':{}},loc);sig=loc.get(out_var)
        if not isinstance(sig,pd.DataFrame):print(f'  [{fi+1:2d}] {title:<40s} NOT_DF');continue
        sig=sig.astype(bool)
    except Exception as e:print(f'  [{fi+1:2d}] {title:<40s} EXEC:{str(e)[:45]}');continue
    sig_bt=sig.loc['20220101':]
    mo=market_on.loc[sig_bt.index]
    for col in sig_bt.columns:sig_bt.loc[~mo,col]=False
    ts=int(sig_bt.sum().sum());print(f'  [{fi+1:2d}] {title:<40s} sig={ts}',end='',flush=True)
    if ts<50:print(' SKIP');continue
    recs=[]
    for col in univ:
        if col not in sig_bt.columns:continue
        for idx in sig_bt.index[sig_bt[col]]:recs.append({'stock_code':col,'select_date':idx.strftime('%Y-%m-%d')})
    sel=pd.DataFrame(recs)
    common=sorted(set(C.columns)&set(sel['stock_code'].unique()))
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
    results.append({'title':title,'cum':cum,'ann':ann,'dd':dd,'sr':sr,'wr':wr,'trades':len(t),'signals':ts,'yearly':yrs})

print(f'\nSuccess: {len(results)}/{len(matches)}')
results.sort(key=lambda x:x['ann']if x['ann']is not None else-999,reverse=True)
print(f"\n{'='*80}")
print(f"  B-Pinyin RESULTS ({len(results)} successful)")
print(f"{'='*80}")
print(f"{'#':<5} {'Formula':<45} {'Cum%':>8} {'Ann%':>8} {'DD%':>8} {'SR':>6} {'WR%':>6} {'T':>6}")
print('  '+'-'*85)
for i,r in enumerate(results,1):
    print(f"  {i:<5} {r['title'][:43]:<45} {r['cum']*100:>+8.2f} {r['ann']*100:>+8.2f} {r['dd']*100:>+8.2f} {r['sr']:>6.2f} {r['wr']*100:>6.1f} {r['trades']:>6}")
for i,r in enumerate(results[:3],1):
    print(f"\n  TOP{i}: {r['title']}")
    for y,yd in sorted(r['yearly'].items()):
        print(f"    {y}: {yd['pnl']:>+12,.0f} ({yd['ret']:+.2f}%)")

with open('output/b_pinyin_final.json','w',encoding='utf-8')as f:json.dump(results,f,ensure_ascii=False,indent=2,default=str)
print(f"\nSaved: output/b_pinyin_final.json")
