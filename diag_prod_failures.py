"""诊断生产环境失败 — 使用 batch_fixed.py 完整的真实函数"""
import sys,os,re,collections,warnings
warnings.filterwarnings('ignore')
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np

# 加载真实数据但只用5只股票（加速诊断）
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
TdxConnector.ensure_connected()
codes=DataFetcher.get_stock_universe('50')[:5]
k=DataFetcher.get_kline(codes,'20240601','20260603',dividend_type="front",period="1d")
C=k["Close"].sort_index();H=k["High"].sort_index();L=k["Low"].sort_index()
O=k["Open"].sort_index();V=k.get("Volume",pd.DataFrame()).sort_index()
valid=C.notna().sum()>10
for d in[C,H,L,O,V]:d=d.loc[:,valid]
CLOSE=C;HIGH=H;LOW=L;OPEN=O;VOL=V
print(f"数据: {C.shape}")

# 导入batch_fixed的preprocess和完整F
from batch_fixed import preprocess,F

gongshi_dir=r'E:\gongshi'
files=sorted([f for f in os.listdir(gongshi_dir) if f.endswith('.md')])

errors=collections.Counter()
examples={}
ok=0;total=0

for fi,fname in enumerate(files):
    with open(os.path.join(gongshi_dir,fname),'r',encoding='utf-8')as f:content=f.read()
    cm=re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`',content,re.DOTALL)
    if not cm:errors['NoCodeBlock']+=1;continue
    total+=1

    code_py,out_var=preprocess(cm.group(1))
    if out_var is None:errors['NoOutputVar']+=1;continue

    loc={'C':C,'CLOSE':C,'O':O,'OPEN':O,'H':H,'HIGH':H,'L':L,'LOW':L,'V':V,'VOL':V,
         'np':np,'pd':pd,'True':True,'False':False,
         'abs':abs,'max':max,'min':min,'round':round,'sum':sum,'len':len,
         'int':int,'float':float,'str':str,'bool':bool,
         'list':list,'dict':dict,'pow':pow,'any':any,'all':all}
    loc.update(F)
    def _if(cond,t,f):
        cv=cond.values.astype(bool)if isinstance(cond,pd.DataFrame)else np.array(cond,dtype=bool)
        tv=t.values if isinstance(t,pd.DataFrame)else np.full(cv.shape,t)if not isinstance(t,np.ndarray)else t
        fv=f.values if isinstance(f,pd.DataFrame)else np.full(cv.shape,f)if not isinstance(f,np.ndarray)else f
        return pd.DataFrame(np.where(cv,tv,fv),index=C.index,columns=C.columns)
    loc['IF']=_if

    try:
        exec(code_py,{'__builtins__':{}},loc)
        sig=loc.get(out_var)
        if isinstance(sig,pd.DataFrame):
            ok+=1
        else:
            errors['OutputNotDF']+=1
    except Exception as e:
        et=type(e).__name__;msg=str(e)
        # 提取关键信息
        if et=='TypeError':
            if 'rand_' in msg:k='T:bool*float'
            elif 'operand type' in msg:
                m2=re.search(r"for\s*:\s*'(\w+)'\s*and\s*'(\w+)'",msg)
                if m2:k=f'T:{m2.group(1)}&{m2.group(2)}'
                else:k=f'T:{msg[:45]}'
            elif 'not supported between' in msg:
                ns=msg.find('not supported')
                k=f'T:cmp({msg[ns:ns+50]})'
            else:k=f'T:{msg[:40]}'
        elif et=='SyntaxError':
            m2=re.search(r'line (\d+)',msg);ln=int(m2.group(1))if m2 else 0
            lns=code_py.split('\n');el=lns[ln-1]if ln and ln<=len(lns)else'?'
            if 'invalid syntax' in msg:
                k=f'S:{el[:55]}'if el!='?'else'S:noloc'
            elif 'cannot assign' in msg:k=f'S1:assign:{el[:40]}'
            elif 'expression cannot contain' in msg:k=f'S5:exprAssign'
            elif 'positional argument' in msg:k=f'S6:posKW:{el[:40]}'
            else:k=f'Sx:{msg[:35]}'
        elif et=='NameError':
            vn=msg.split("'")[1]if"'"in msg else'?'
            k=f'N:{vn[:25]}'
        elif et=='ValueError':k=f'V:{msg[:40]}'
        elif et=='AttributeError':k=f'A:{msg[:40]}'
        elif et=='KeyError':k=f'K:{msg[:40]}'
        elif et=='IndexError':k=f'I:{msg[:40]}'
        elif et=='AssertionError':k='Assert'
        else:k=f'{et}:{msg[:35]}'
        errors[k]+=1
        if k not in examples:examples[k]=fname[:40]

print(f'Diagnostic: {total} | OK:{ok} ({ok/total*100:.0f}%)')
print(f'{"Error":<60s} {"#":>5}  {"Example"}')
print('-'*100)
for e,c in errors.most_common(30):
    print(f'{e:<60s} {c:>5}  {examples.get(e,"")}')
