"""诊断所有468个公式 — 按错误类型分组统计"""
import sys,os,re,warnings,collections
warnings.filterwarnings('ignore')
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pandas as pd,numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from utils.logger import get_logger
logger = get_logger(__name__)
TdxConnector.ensure_connected()
all_codes=DataFetcher.get_stock_universe('50')
k=DataFetcher.get_kline(all_codes[:10],'20240601','20260603',dividend_type="front",period="1d")
C=k["Close"].sort_index();H=C;L=C;O=C;V=C

from batch_final import preprocess_formula,ALL_FUNCS,_if,C as bC

gongshi_dir=r'E:\gongshi'
files=sorted([f for f in os.listdir(gongshi_dir) if f.endswith('.md')])

error_types=collections.Counter()
error_examples={}
ok_count=0

for fi,fname in enumerate(files):
    fp=os.path.join(gongshi_dir,fname)
    try:
        with open(fp,'r',encoding='utf-8')as f:content=f.read()
    except Exception as e: logger.warning("Read file failed: %s", e);continue
    cm=re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`',content,re.DOTALL)
    if not cm:continue

    code_py,out_var=preprocess_formula(cm.group(1))
    if out_var is None:
        error_types['无输出变量']+=1;continue

    loc={'C':C,'CLOSE':C,'O':O,'OPEN':O,'H':H,'HIGH':H,'L':L,'LOW':L,'V':V,'VOL':V,
         'np':np,'pd':pd,'True':True,'False':False,'abs':abs,'max':max,'min':min,
         'round':round,'sum':sum,'len':len,'int':int,'float':float,'str':str,'bool':bool,
         'list':list,'dict':dict,'pow':pow,'any':any,'all':all}
    loc.update(ALL_FUNCS)
    loc['IF']=_if

    try:
        exec(code_py,{'__builtins__':{}},loc)
        result=loc.get(out_var)
        if isinstance(result,pd.DataFrame):
            ok_count+=1
        else:
            error_types['输出非DataFrame']+=1
    except Exception as e:
        err_msg=str(e)
        # 提取错误类型关键词
        etype=type(e).__name__
        msg=err_msg[:80]

        # 分类错误
        if etype=='SyntaxError':
            if 'cannot assign to function call' in msg:
                key='Syntax: 不能赋值给函数调用'
            elif 'cannot assign to expression' in msg:
                key='Syntax: 不能赋值给表达式'
            elif "Maybe you meant '==' or ':='" in msg:
                key='Syntax: = vs ==混淆'
            elif 'invalid syntax' in msg:
                # 提取具体位置
                key='Syntax: 无效语法'
            elif 'expression cannot contain assignment' in msg:
                key='Syntax: 表达式含赋值'
            elif 'positional argument follows keyword argument' in msg:
                key='Syntax: 位置参数在关键字后'
            elif 'leading zeros' in msg:
                key='Syntax: 前导零'
            elif 'EOL while scanning' in msg:
                key='Syntax: 字符串未闭合'
            else:
                key=f'Syntax: 其他({msg[:40]})'
        elif etype=='NameError':
            vname=err_msg.split("'")[1] if "'" in err_msg else '?'
            if vname.startswith('_C'):
                key='NameError: 未映射中文变量'
            else:
                # 检查是否TDX函数
                if vname.upper() in ALL_FUNCS:
                    key=f'{etype}: 函数{vname}未注入(大小写?)'
                else:
                    key=f'NameError: {vname[:30]}'
        elif etype=='TypeError':
            if 'rand_' in msg:
                key='TypeError: bool-float运算冲突'
            elif 'operand type' in msg:
                key='TypeError: 操作数类型不匹配'
            elif 'cannot perform' in msg:
                key=f'TypeError: {msg[:50]}'
            else:
                key=f'TypeError: {msg[:40]}'
        elif etype=='ValueError':
            key=f'ValueError: {msg[:40]}'
        elif etype=='KeyError':
            key=f'KeyError: {msg[:40]}'
        else:
            key=f'{etype}: {msg[:40]}'

        error_types[key]+=1
        if key not in error_examples:
            error_examples[key]=fname

print('='*80)
print(f'  公式诊断: {len(files)}个 | 成功:{ok_count} | 失败:{len(files)-ok_count}')
print('='*80)
print(f'  {"错误类型":<45s} {"数量":>6}  {"示例"}')
print('  '+'-'*80)
for err,count in error_types.most_common(40):
    ex=error_examples.get(err,'')[:50]
    print(f'  {err:<45s} {count:>6}  {ex}')
print('='*80)
