"""批量回测TDX中所有条件选股公式 — 用原生TDX引擎+VERA回测"""
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

print("="*80)
print("  TDX条件选股公式批量回测")
print("="*80)

# 1. 提取TDX中所有条件选股公式名
print("\n[1] 提取TDX公式名...",flush=True)
from tqcenter import tq

with open(os.path.join(os.environ.get('TDX_HOME', r'E:\NEW_TDX'), r'T0002\PriGS.dat'),'rb')as f:data=f.read()

# 方法: 扫描二进制文件, 找所有GBK编码的中文或ASCII字符串
# 公式名特征: null-terminated, 前面有特定的二进制标记
formulas=set()
# 扫描所有连续非null字节段
i=0;segments=[]
while i<len(data):
    if data[i]!=0:
        j=i
        while j<len(data) and data[j]!=0:j+=1
        seg=data[i:j]
        if 3<=len(seg)<=50:
            try:
                s=seg.decode('gbk')
                if s.isprintable() and not s.isspace():
                    segments.append(s)
            except Exception as e: pass  # decode may fail for non-text segments
        i=j
    else:i+=1

# 过滤候选公式名(中文或大写英文+数字)
import unicodedata
for s in segments:
    has_cjk=any('一'<=c<='鿿'for c in s)
    is_ascii_id=s.isascii() and any(c.isalpha()for c in s) and not s[0].isdigit()
    if has_cjk or is_ascii_id:
        formulas.add(s)

# 已知的假公式名(变量名/函数名)
false_names={'MA','EMA','REF','HHV','LLV','CROSS','COUNT','EVERY','EXIST','SUM',
    'ABS','MAX','MIN','BARSLAST','FILTER','UPNDAY','BETWEEN','NOT','STD','VAR',
    'CLOSE','OPEN','HIGH','LOW','VOL','AMOUNT','O','C','H','L','V','CAPITAL',
    'IF','THEN','ELSE','AND','OR','XG','ZP','SELECT','BUY','SELL','DRAW',
    'MA20','MA20A','UDAYS','ANGEL',
    'A0','A1','A2','A3','A4','A5','A6','A7','A8','A9','B1','B2','C1','C2',
    'D1','D2','E1','E2','F1','F2','G1','G2','H1','H2','I1','I2','J1','J2',
    'M1','M2','M3','M4','M5','N1','N2','N3','N4','N5','P1','P2','R1','R2',
    'S1','S2','T1','T2','U1','V1','V2','W1','W2','X1','X2','Y1','Y2','Z1','Z2',
    'AA','AB','AC','AD','AE','AF','AG','AH','AI','AJ','AK','AL','AM','AN',
    'BA','BB','BC','BD','BE','BF','BG','BH','BI','BJ','BK','BL','BM','BN',
    'CA','CB','CC','CD','CE','CF','CG','CH','CI','CJ','CK','CL','CM','CN',
    'DA','DB','DC','DD','DE','DF','DG','DH','DI','DJ','DK','DL','DM','DN',
    'EQ','NE','GT','LT','GE','LE','VAR','VO','RANGE','COLOR','STICK','LINE',
    'DIFF','DEA','MACD','KDJ','RSI','WR','BOLL','UB','LB','MB','SAR',
    'LONGCROSS','BARSNEXT','FILTERX','BACKSET','TROUGH','PEAK','ZIG','PLOYLINE',
    'TOTALBARSCOUNT','CURRBARSCOUNT','DATATYPE','PERIOD','CONST','DRAWNULL',
    'ALIGNRIGHT','PRECISION','NODRAW','DOTLINE','CIRCLEDOT','POINTDOT',
    'COLORRED','COLORGREEN','COLORBLUE','COLORYELLOW','COLORWHITE','COLORMAGENTA',

    'A', 'B', 'D', 'E', 'F', 'G', 'I', 'J', 'K', 'M', 'N', 'P', 'Q', 'R', 'S', 'T', 'U', 'W', 'X', 'Y', 'Z',
    'N', 'O', 'C', 'H', 'L', 'V', 'O1', 'H1', 'L1', 'C1', 'V1', 'O2', 'C2',
}

# 验证: 逐个尝试用TDX执行
verified=[]
for name in sorted(formulas):
    if name in false_names:continue
    # 过滤纯数字和单字母
    if len(name)<2:continue
    if name.isdigit():continue
    if len(name)==2 and name[0].isalpha() and name[1].isdigit():continue
    if len(name)==3 and all(c.isdigit() for c in name):continue

    try:
        r=tq.formula_process_mul_xg(
            formula_name=name,formula_arg='',return_count=0,return_date=True,
            stock_list=['600519.SH'],stock_period='1d',
            start_time='20260101',end_time='20260601',count=3000,dividend_type=1)
        eid=r.get('ErrorId','?')
        if eid=='0':
            verified.append(name)
            if len(verified)<=30:print(f'  [{len(verified)}] {name}')
    except Exception as e: logger.warning("Verify formula failed: %s", e)

print(f'\n  验证通过: {len(verified)} 个条件选股公式')

# 2. 批量回测
print(f"\n[2] 批量回测...",flush=True)
univ=DataFetcher.get_stock_universe('50')
codes_list=[c for c in univ if 'ST'not in c and'*ST'not in c]
print(f"  股票池: {len(codes_list)}只")

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

results=[];ok=0;no_sig=0;err=0;t0=time.time()
for fi,name in enumerate(verified):
    try:
        sel_df=FormulaRunner.run_stock_selection_with_dates(
            formula_name=name,formula_arg='',stock_list=codes_list,
            start_time='20260101',end_time='20260605',stock_period='1d',dividend_type=1)
    except Exception as e: logger.warning("run_stock_selection failed: %s", e);err+=1;continue

    if sel_df.empty or len(sel_df)<10:no_sig+=1
    if fi%10==0:
        elapsed=max(time.time()-t0,1)
        print(f"  [{fi+1}/{len(verified)}] OK:{ok} NoSig:{no_sig} Err:{err} | ~{(fi+1)/elapsed*60:.0f}/min",flush=True)
    if sel_df.empty or len(sel_df)<10:continue

    try:
        k=DataFetcher.get_kline(sel_df['stock_code'].unique().tolist(),'20240601','20260605',dividend_type='front',period='1d')
        C=k['Close'].sort_index();H=k['High'].sort_index();L=k['Low'].sort_index()
        valid=C.notna().sum()>10;C=C.loc[:,valid];H=H.reindex(columns=C.columns);L=L.reindex(columns=C.columns)
        common=sorted(set(C.columns)&set(sel_df['stock_code'].unique()))
        cs=C[common].ffill().bfill();hs=H.reindex(index=cs.index,columns=common).ffill().bfill()
        ls=L.reindex(index=cs.index,columns=common).ffill().bfill()
        entries=pd.DataFrame(False,index=cs.index,columns=cs.columns)
        for _,row in sel_df.iterrows():
            code,dt=row['stock_code'],pd.to_datetime(row['select_date'])
            if code not in entries.columns:continue
            if dt in entries.index:entries.loc[dt,code]=True
            else:
                m=entries.index>=dt
                if m.any():entries.loc[entries.index[m][0],code]=True
        engine=BacktestEngine(ENGINE_CFG)
        bp=np.array([0.05,0.12],dtype=np.float64);br=np.array([0.30,0.30],dtype=np.float64)
        brs=engine.run_cached(cs,entries,hs.values.astype(np.float64),
                              ls.values.astype(np.float64),STOP_CONFIG,sel_df,bp,br,2,skip_sm=True)
        m=brs['metrics']
        results.append({
            'name':name,'signals':len(sel_df),
            'cumret':brs['cumulative_return'],'annret':m['annualized_return'],
            'maxdd':m['max_drawdown'],'sharpe':m['sharpe_ratio'],
            'winrate':m['win_rate'],'trades':len(brs['trades']),
        })
        ok+=1
    except Exception as e: logger.warning("Backtest failed: %s", e);err+=1

# 3. 输出
print(f"\n[3] Done! OK:{ok} NoSig:{no_sig} Err:{err}",flush=True)
results.sort(key=lambda r:r['annret']if r['annret']is not None else-999,reverse=True)

print("\n"+"="*100)
print(f"  TDX公式批量回测 | 2026-01-01 ~ 2026-06-05")
print("="*100)
for i,r in enumerate(results,1):
    print(f"  {i:<5} {r['name']:<30} Cum:{r['cumret']*100:>+7.2f}% Ann:{r['annret']*100:>+7.2f}% DD:{r['maxdd']*100:>+7.2f}% SR:{r['sharpe']:>5.2f} WR:{r['winrate']*100:>5.1f}% T:{r['trades']:>6} Sig:{r['signals']:>6}")

output={'results':results}
with open('output/tdx_batch_results.json','w',encoding='utf-8')as f:
    json.dump(output,f,ensure_ascii=False,indent=2,default=str)
print(f"\nSaved: output/tdx_batch_results.json ({len(results)})")
print("="*80)
