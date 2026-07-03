"""
抽样 5 个 gongshi 公式回测 — 覆盖不同技术类型。

复用 batch_fixed.py 的 preprocess + F 函数表，纯 Python 解释 TDX 公式，
不依赖通达信客户端导入公式。

抽样（横跨 5 个类型）：
  - MACD 空中加油 (趋势)
  - V 反转 (反转)
  - RSI-WR 共振 (摆动指标)
  - RSRS 回归斜率 (统计/量化)
  - 一跃龙门 (形态/突破)

区间: 2025-06-23 ~ 2026-06-23  (近 1 年)
范围: 沪深 A 股 type=50
"""
import sys
import os
import re
import json
import time
import warnings

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config

# === 配置 ===
GONGSHI_DIR = r'E:\1target\gongshi'
START = '20250623'
END   = '20260623'
UNIVERSE_TYPE = '50'

# 与 BUG-5 修复后用过的相同参数 (default.yaml 的真实值)
ENGINE_CFG = {
    'initial_capital': 1_000_000.0,
    'commission': 0.0003,
    'slippage': 0.001,
    'period': '1d',
    'position_sizing': {
        'min_buy_amount': 2000.0,
        'max_buy_amount': 20_000.0,
        'lot_size': 100,
        'min_lots': 1,
    },
}
STOP_CONFIG = load_stop_config()

# 抽样 5 个公式（按文件名精确匹配）
SAMPLE_FORMULAS = [
    ('MACD空中加油之选股指标公式',  '趋势'),
    ('V反转主图之选股指标公式',     '反转'),
    ('RSI-WR共振之选股指标公式',    '摆动指标'),
    ('RSRS回归斜率之选股指标公式',   '统计量化'),
    ('一跃龙门之选股指标公式',      '形态突破'),
]

# ============ TDX 函数表（与 batch_fixed.py 保持一致）============
def _ma(x, n): return x.rolling(int(n), min_periods=1).mean()
def _ema(x, n): return x.ewm(span=int(n), adjust=False).mean()
def _ref(x, n): return x.shift(int(n))
def _hhv(x, n): return x.rolling(int(n), min_periods=1).max()
def _llv(x, n): return x.rolling(int(n), min_periods=1).min()
def _cross(a, b): return (a > b) & (a.shift(1) <= b.shift(1))
def _count(x, n): return x.astype(float).rolling(int(n), min_periods=1).sum()
def _sum(x, n): return x.rolling(int(n), min_periods=1).sum()
def _every(x, n): return (x > 0).rolling(int(n), min_periods=1).min() > 0
def _exist(x, n): return _count(x > 0, int(n)) > 0
def _barslast(x):
    r = pd.DataFrame(np.nan, index=x.index, columns=x.columns)
    for col in x.columns:
        v = x[col].values
        o = np.full(len(v), np.nan)
        lt = -1
        for i in range(len(v)):
            if v[i] and not np.isnan(v[i]):
                lt = i
            if lt >= 0:
                o[i] = i - lt
        r[col] = o
    return r
def _filter(x, n):
    r = x.copy()
    for col in x.columns:
        v = x[col].values.astype(bool)
        o = np.zeros(len(v), dtype=bool)
        lt = -int(n) - 1
        for i in range(len(v)):
            if v[i] and (i - lt) > int(n):
                o[i] = True
                lt = i
        r[col] = o
    return r
def _sma(x, n, m=1):
    r = x.copy()
    for col in x.columns:
        v = x[col].values.astype(float)
        s = np.full(len(v), np.nan)
        fv = np.where(~np.isnan(v))[0]
        if len(fv) > 0:
            s[fv[0]] = v[fv[0]]
            for i in range(fv[0] + 1, len(v)):
                s[i] = (m * v[i] + (n - m) * s[i - 1]) / n if not np.isnan(v[i]) else s[i - 1]
        r[col] = s
    return r
def _std(x, n): return x.rolling(int(n), min_periods=1).std()
def _barslastcount(x): return _barslast(~x.astype(bool))
def _between(x, lo, hi): return (x >= float(lo)) & (x <= float(hi))
def _upnday(x, n):
    r = pd.DataFrame(True, index=x.index, columns=x.columns)
    for i in range(1, int(n)):
        r = r & (x > x.shift(i))
    return r


def build_F(C, H, L, O, V):
    F = {
        'MA': _ma, 'EMA': _ema, 'SMA': _sma, 'REF': _ref, 'HHV': _hhv, 'LLV': _llv,
        'CROSS': _cross, 'COUNT': _count, 'EVERY': _every, 'EXIST': _exist, 'SUM': _sum,
        'ABS': lambda x: x.abs(),
        'MAX': lambda a, b: np.maximum(a.values if isinstance(a, pd.DataFrame) else a,
                                        b.values if isinstance(b, pd.DataFrame) else b),
        'MIN': lambda a, b: np.minimum(a.values if isinstance(a, pd.DataFrame) else a,
                                        b.values if isinstance(b, pd.DataFrame) else b),
        'BARSLAST': _barslast, 'FILTER': _filter, 'UPNDAY': _upnday,
        'BETWEEN': _between, 'NOT': lambda x: ~x.astype(bool),
        'STD': _std, 'VAR': lambda x, n: x.rolling(int(n), min_periods=1).var(),
        'BARSLASTCOUNT': _barslastcount,
        'BARSSINCEN': lambda x, n: _count(~(x.astype(bool)), int(n)),
        'LONGCROSS': lambda a, b, n: (a > b) & (a.shift(int(n)) <= b.shift(int(n))),
        'ATAN': lambda x: np.arctan(x) * 180 / np.pi,
        'LN': lambda x: np.log(x.clip(1e-10, None)),
        'SQRT': lambda x: np.sqrt(x.clip(0, None)),
        'POW': lambda x, n: np.power(x, float(n)),
        'EXP': lambda x: np.exp(x.clip(-20, 20)),
        'INTPART': lambda x: np.floor(x), 'ROUND': lambda x, n=0: np.round(x, int(n)),
        'REVERSE': lambda x: -x, 'RANGE': lambda x, n: _hhv(x, n) - _llv(x, n),
        'ZTPRICE': lambda c, lim=0.1: c * (1 + float(lim) if isinstance(lim, (int, float)) else 0.1),
        'FINANCE': lambda n: pd.DataFrame(1.0, index=C.index, columns=C.columns),
        'DYNAINFO': lambda n: pd.DataFrame(0.0, index=C.index, columns=C.columns),
        'CAPITAL': lambda: V.rolling(20).mean() * 20 / C,
        'HSL': lambda: (C / C.shift(1) - 1).abs() * 100,
        'CODELIKE': lambda p: pd.DataFrame(False, index=C.index, columns=C.columns),
        'NAMELIKE': lambda p: pd.DataFrame(False, index=C.index, columns=C.columns),
        'INBLOCK': lambda p: pd.DataFrame(False, index=C.index, columns=C.columns),
        'DMA': _ma, 'WMA': _ma, 'EXPMA': _ema, 'EXPMEMA': _ema, 'MEMA': _ema,
        'FORCAST': lambda x, n: x.rolling(int(n), min_periods=1).apply(
            lambda v: np.polyval(np.polyfit(np.arange(len(v)), v, 1), len(v))
            if len(v) == int(n) else np.nan, raw=True),
        'SLOPE': lambda x, n: (x - x.shift(int(n))) / int(n),
        'SAR': lambda *a: L, 'BACKSET': lambda x, n: x,
        'PEAK': lambda x, n, m: x, 'TROUGH': lambda x, n, m: x, 'ZIG': lambda x, n: x,
        'BARSCOUNT': lambda *a: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'CONST': lambda x: x.iloc[-1] if isinstance(x, pd.DataFrame) else float(x),
        'CURRBARSCOUNT': lambda: len(C.index),
        'TOTALBARSCOUNT': lambda: len(C.index),
        'ISLASTBAR': lambda: pd.DataFrame(False, index=C.index, columns=C.columns),
        'PERIOD': lambda: 5,
        'DATATYPE': lambda: 6,
        'HHVBARS': lambda x, n: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'LLVBARS': lambda x, n: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'WINNER': lambda x: pd.DataFrame(0.5, index=C.index, columns=C.columns),
        'AMOUNT': lambda: V * C, 'VOL': V, 'CLOSE': C, 'OPEN': O, 'HIGH': H, 'LOW': L,
        'AVEDEV': lambda x, n: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'COST': lambda x: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'DATE': lambda: pd.DataFrame(20260623, index=C.index, columns=C.columns, dtype=float),
        'YEAR': lambda: pd.DataFrame(2026, index=C.index, columns=C.columns, dtype=float),
        'MONTH': lambda: pd.DataFrame(6, index=C.index, columns=C.columns, dtype=float),
        'DAY': lambda: pd.DataFrame(23, index=C.index, columns=C.columns, dtype=float),
        'INDEXC': C, 'INDEXO': O, 'INDEXH': H, 'INDEXL': L, 'INDEXV': V,
        'STRCAT': lambda *a: '', 'CON2STR': lambda *a: '',
        'PLOYLINE': lambda a, b: a,
        'MACD': lambda *a: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'KDJ': lambda *a: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'RSI': lambda *a: pd.DataFrame(50, index=C.index, columns=C.columns, dtype=float),
        'WR': lambda *a: pd.DataFrame(50, index=C.index, columns=C.columns, dtype=float),
        'CR': lambda *a: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'VOL_MULTIPLE': lambda: V / V.rolling(20).mean(),
        'CYC': lambda *a: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'HYBLOCK': lambda p: pd.DataFrame(False, index=C.index, columns=C.columns),
        'DYBLOCK': lambda p: pd.DataFrame(False, index=C.index, columns=C.columns),
        'GNBLOCK': lambda p: pd.DataFrame(False, index=C.index, columns=C.columns),
        'SUMBARS': lambda x, n: pd.DataFrame(0.0, index=C.index, columns=C.columns),
        'DRAWNULL': lambda: pd.DataFrame(np.nan, index=C.index, columns=C.columns),
        'TROUGHBARS': lambda x, n, m: pd.DataFrame(0.0, index=C.index, columns=C.columns),
        'PEAKBARS': lambda x, n, m: pd.DataFrame(0.0, index=C.index, columns=C.columns),
        'ALIGNRIGHT': lambda: pd.DataFrame(False, index=C.index, columns=C.columns),
        'BARTIME': lambda: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'DTPRICE': lambda: pd.DataFrame(0, index=C.index, columns=C.columns, dtype=float),
        'PARTLINE': lambda x, cond: x,
        'VERTLINE': lambda *a, **k: pd.DataFrame(False, index=C.index, columns=C.columns),
        'DRAWGBK': lambda *a, **k: pd.DataFrame(False, index=C.index, columns=C.columns),
        'K': C,
    }
    F['IF'] = None
    return F


def preprocess(code):
    """把 TDX 公式转 Python 代码。修复 RSRS 的 && 问题。"""
    # 预处理：把 TDX 的 && 替换成 and
    code = code.replace('&&', ' and ').replace('||', ' or ')

    lines = code.strip().split('\n')
    all_vars = set()
    for l in lines:
        l = l.strip().rstrip(';')
        if not l or l.startswith('{'):
            continue
        for mk in [':=', ':']:
            if mk in l:
                v = l.split(mk, 1)[0].strip()
                if v and not any(c in v for c in '><=+-*/()[]&|!,.'):
                    if not v[0].isdigit():
                        all_vars.add(v)
                break

    return lines, all_vars


def preprocess_v2(code, F):
    """完整预处理：把 TDX 公式转为可 exec 的 Python 代码，返回 (code_py, out_var)"""
    code = code.replace('&&', ' and ').replace('||', ' or ')
    lines = code.strip().split('\n')

    all_vars = set()
    for l in lines:
        l = l.strip().rstrip(';')
        if not l or l.startswith('{'):
            continue
        for mk in [':=', ':']:
            if mk in l:
                v = l.split(mk, 1)[0].strip()
                if v and not any(c in v for c in '><=+-*/()[]&|!,.'):
                    if not v[0].isdigit():
                        all_vars.add(v)
                break

    cn_map = {}
    ci = 0
    for v in sorted(all_vars, key=lambda x: -len(x)):
        if any(ord(c) > 127 for c in v) or not v.isidentifier() or v.upper() in F:
            cn_map[v] = f'_C{ci:04d}'
            ci += 1

    ci_funcs = {fn.lower(): fn for fn in F}
    for e in 'ABS MIN MAX NOT IF WINNER AMOUNT AVEDEV DATE YEAR MONTH DAY HOUR MINUTE ' \
            'INDEXC INDEXO INDEXH INDEXL INDEXV STRCAT CON2STR COST PLOYLINE MACD KDJ RSI ' \
            'WR CR VOL EXPMA EXPMEMA CLOSE OPEN HIGH LOW CAPITAL HSL VOL_MULTIPLE CYC'.split():
        ci_funcs[e.lower()] = e

    processed = []
    out_var = None
    for l in lines:
        l = l.strip().rstrip(';')
        if not l or l.startswith('{'):
            continue
        up = l.upper()
        if any(up.startswith(k) for k in
               ['DRAW', 'STICK', 'PLOYLINE', 'DRAWTEXT', 'DRAWNUMBER', 'DRAWICON',
                'DRAWKLINE', 'DRAWBAND', 'FILLRGN', 'VERTLINE', 'DRAWNULL', 'NODRAW',
                'ALIGNRIGHT', 'CIRCLEDOT', 'POINTDOT', 'DOTLINE', 'CROSSDOT',
                'VOLSTICK', 'COLORSTICK', 'PARTLINE']):
            continue
        for pat in [r',?\s*LINETHICK\d*', r',\s*COLOR\w*', r',\s*LINETHICK\d*',
                    r',\s*DOTLINE', r',\s*NODRAW', r',\s*CIRCLEDOT', r',\s*POINTDOT',
                    r',\s*STICK', r',\s*VOLSTICK', r',\s*COLORSTICK']:
            l = re.sub(pat, '', l, flags=re.IGNORECASE)

        for cn, s in sorted(cn_map.items(), key=lambda x: -len(x[0])):
            if cn in l:
                l = re.sub(r'\b' + re.escape(cn) + r'\b', s, l)

        if ':=' in l:
            v, e = l.split(':=', 1)
            l = f'{v.strip()} = {e.strip()}'
        elif ':' in l:
            p = l.split(':', 1)
            pv = p[0].strip()
            if pv and not any(c in pv for c in '><=+-*/()[]&|!,.') and not pv[0].isdigit():
                pe = p[1].strip() if len(p) > 1 else ''
                l = f'{pv} = {pe}'
                out_var = pv

        l = re.sub(r'\bAND\b', '&', l, flags=re.IGNORECASE)
        l = re.sub(r'\bOR\b', '|', l, flags=re.IGNORECASE)
        l = l.replace('<>', '!=')
        l = re.sub(r'(?<!\w)0+(\d+)(?!\w)', r'\1', l)

        words = re.findall(r'\b([A-Za-z_]\w*)\b', l)
        for w in set(words):
            wl = w.lower()
            if wl in ci_funcs:
                canonical = ci_funcs[wl]
                if w != canonical:
                    l = re.sub(r'\b' + re.escape(w) + r'\b', canonical, l)
            elif w in cn_map:
                l = re.sub(r'\b' + re.escape(w) + r'\b', cn_map[w], l)

        if ' = ' in l:
            parts = l.split(' = ', 1)
            lhs = parts[0].strip()
            rhs = parts[1]
            if re.match(r'^[A-Za-z_]\w*$', lhs):
                rhs = re.sub(r'(?<![=!<>])=(?!=)', r'==', rhs)
                l = f'{lhs} = {rhs}'
            else:
                l = re.sub(r'(?<![=!<>])=(?!=)', r'==', l)
        else:
            l = re.sub(r'(?<![=!<>])=(?!=)', r'==', l)

        processed.append(l)

    if out_var is None:
        for pl in reversed(processed):
            if ' = ' in pl:
                out_var = pl.split(' = ', 1)[0].strip()
                break
    return '\n'.join(processed), out_var


def compute_signal(code, F, C, H, L, O, V):
    """解析 TDX 公式，返回 bool DataFrame"""
    code_py, out_var = preprocess_v2(code, F)
    if out_var is None:
        return None, 'no_out_var'

    loc = {'C': C, 'CLOSE': C, 'O': O, 'OPEN': O, 'H': H, 'HIGH': H, 'L': L, 'LOW': L,
           'V': V, 'VOL': V, 'np': np, 'pd': pd, 'True': True, 'False': False,
           'abs': abs, 'max': max, 'min': min, 'round': round, 'sum': sum, 'len': len,
           'int': int, 'float': float, 'str': str, 'bool': bool, 'list': list, 'dict': dict,
           'pow': pow, 'any': any, 'all': all}
    loc.update(F)

    def _if(cond, t, f):
        c = cond.values.astype(bool) if isinstance(cond, pd.DataFrame) else np.array(cond, dtype=bool)
        tv = t.values if isinstance(t, pd.DataFrame) else t
        fv = f.values if isinstance(f, pd.DataFrame) else f
        return pd.DataFrame(np.where(c, tv, fv), index=C.index, columns=C.columns)
    loc['IF'] = _if

    try:
        exec(code_py, {'__builtins__': {}}, loc)
        result = loc.get(out_var)
        if not isinstance(result, pd.DataFrame):
            return None, f'out_var_not_dataframe (type={type(result).__name__})'
        if result.dtypes.iloc[0] != bool:
            result = result > 0.5
        return result.astype(bool), None
    except Exception as e:
        return None, f'exec_error: {type(e).__name__}: {e}'


def run_one_formula(title, formula_type, C, H, L, O, V, F, univ):
    """跑单个公式: 解析 → 选股 → 回测 → 收集指标"""
    fp = os.path.join(GONGSHI_DIR, f'{title}.md')
    if not os.path.exists(fp):
        return {'title': title, 'type': formula_type, 'status': 'file_not_found'}

    with open(fp, 'r', encoding='utf-8') as f:
        content = f.read()
    cm = re.search(r'```\s*\n(.*?)\n```', content, re.DOTALL)
    if not cm:
        return {'title': title, 'type': formula_type, 'status': 'no_code_block'}
    code = cm.group(1)

    sig, err = compute_signal(code, F, C, H, L, O, V)
    if sig is None:
        return {'title': title, 'type': formula_type, 'status': f'signal_error: {err}'}

    # 限制信号在回测区间内
    sig_bt = sig.loc[START:END]
    ts = int(sig_bt.sum().sum())
    if ts < 5:
        return {'title': title, 'type': formula_type, 'status': f'too_few_signals ({ts})'}

    try:
        recs = []
        for col in univ:
            if col not in sig_bt.columns:
                continue
            for idx in sig_bt.index[sig_bt[col]]:
                recs.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
        sel = pd.DataFrame(recs)
        if len(sel) < 5:
            return {'title': title, 'type': formula_type, 'status': f'too_few_records ({len(sel)})'}

        common = sorted(set(C.columns) & set(sel['stock_code'].unique()))
        cs = C[common].ffill().bfill()
        hs = H.reindex(index=cs.index, columns=common).ffill().bfill()
        ls = L.reindex(index=cs.index, columns=common).ffill().bfill()
        entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
        for _, row in sel.iterrows():
            code_, dt = row['stock_code'], pd.to_datetime(row['select_date'])
            if code_ not in entries.columns:
                continue
            if dt in entries.index:
                entries.loc[dt, code_] = True
            else:
                m = entries.index >= dt
                if m.any():
                    entries.loc[entries.index[m][0], code_] = True

        engine = BacktestEngine(ENGINE_CFG)
        bp = np.array([0.06, 0.15], dtype=np.float64)
        br = np.array([0.30, 0.30], dtype=np.float64)
        brs = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                                ls.values.astype(np.float64), STOP_CONFIG, sel, bp, br, 2, skip_sm=True)
        m = brs['metrics']

        # 统计阶梯止盈档位命中
        trades = brs.get('trades', pd.DataFrame())
        ladder = trades[trades.get('exit_reason', '') == '阶梯止盈'] if not trades.empty else pd.DataFrame()
        ladder_6 = 0
        ladder_15 = 0
        if not ladder.empty:
            for _, t in ladder.iterrows():
                p = t.get('profit_pct', 0)
                if abs(p - 0.06) < 0.005:
                    ladder_6 += 1
                elif abs(p - 0.15) < 0.01:
                    ladder_15 += 1

        return {
            'title': title, 'type': formula_type, 'status': 'ok',
            'signals': ts, 'selections': len(sel), 'universe_used': len(common),
            'trades': len(trades),
            'cumret': brs['cumulative_return'],
            'annret': m['annualized_return'],
            'maxdd': m['max_drawdown'],
            'sharpe': m['sharpe_ratio'],
            'winrate': m['win_rate'],
            'ladder_6_count': ladder_6,
            'ladder_15_count': ladder_15,
        }
    except Exception as e:
        return {'title': title, 'type': formula_type, 'status': f'backtest_error: {type(e).__name__}: {e}'}


def main():
    t_start = time.time()
    print('=' * 80)
    print('  VERA 抽样公式回测 — 5 个不同技术类型')
    print(f'  区间: {START} ~ {END}  范围: 沪深 A 股 type=50')
    print('=' * 80)

    # 1. 连 TDX + 取 K 线
    print('\n[1] 连接 TDX + 取 K 线...', flush=True)
    TdxConnector.ensure_connected()
    codes = DataFetcher.get_stock_universe(UNIVERSE_TYPE)
    print(f'  股票池: {len(codes)} 只')
    k = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
    C = k['Close'].sort_index()
    H = k['High'].sort_index()
    L = k['Low'].sort_index()
    O = k['Open'].sort_index()
    V = k.get('Volume', pd.DataFrame()).sort_index()
    valid = C.notna().sum() > 100
    for d in [C, H, L, O, V]:
        d.drop(columns=[c for c in d.columns if not valid[c]], inplace=True)
    univ = [c for c in C.columns if 'ST' not in c and '*ST' not in c]
    print(f'  K线: {C.shape}, 有效股票: {len(univ)}')

    # 2. 构建 F 函数表
    F = build_F(C, H, L, O, V)

    # 3. 逐个公式回测
    print('\n[2] 跑 5 个公式...', flush=True)
    results = []
    for i, (title, ftype) in enumerate(SAMPLE_FORMULAS, 1):
        t0 = time.time()
        print(f'\n  [{i}/5] {title} ({ftype})')
        r = run_one_formula(title, ftype, C, H, L, O, V, F, univ)
        r['elapsed_s'] = round(time.time() - t0, 1)
        results.append(r)
        if r['status'] == 'ok':
            print(f'    → 信号 {r["signals"]} | 选股 {r["selections"]} | 交易 {r["trades"]} | '
                  f'累计 {r["cumret"]*100:+.2f}% | 胜率 {r["winrate"]*100:.1f}% | '
                  f'用时 {r["elapsed_s"]}s')
        else:
            print(f'    → {r["status"]}')

    # 4. 输出汇总
    print('\n' + '=' * 100)
    print(f'  汇总 | {START}~{END} | 沪深A股 {len(univ)} 只')
    print('=' * 100)
    print(f'  {"#":<3} {"类型":<10} {"公式":<35} {"信号":>5} {"选股":>5} {"交易":>5} '
          f'{"累计":>8} {"年化":>8} {"回撤":>8} {"夏普":>6} {"胜率":>6} {"6%档":>5} {"15%档":>6}')
    for i, r in enumerate(results, 1):
        if r['status'] == 'ok':
            print(f'  {i:<3} {r["type"]:<10} {r["title"][:33]:<35} '
                  f'{r["signals"]:>5} {r["selections"]:>5} {r["trades"]:>5} '
                  f'{r["cumret"]*100:>+7.2f}% {r["annret"]*100:>+7.2f}% '
                  f'{r["maxdd"]*100:>+7.2f}% {r["sharpe"]:>6.2f} '
                  f'{r["winrate"]*100:>5.1f}% {r["ladder_6_count"]:>5} {r["ladder_15_count"]:>6}')
        else:
            print(f'  {i:<3} {r["type"]:<10} {r["title"][:33]:<35} '
                  f'{"FAIL":>5}  原因: {r["status"][:40]}')

    print(f'\n  总用时: {time.time() - t_start:.1f}s')

    # 5. 保存 JSON + Markdown
    out_json = 'output/gongshi_sample_5.json'
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump({
            'config': {'start': START, 'end': END, 'universe': UNIVERSE_TYPE, 'count': len(univ)},
            'results': results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f'  保存: {out_json}')

    out_md = 'output/gongshi_sample_5.md'
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write(f'# 抽样公式回测报告\n\n')
        f.write(f'- **区间**: {START} ~ {END}\n')
        f.write(f'- **范围**: 沪深 A 股 type=50 ({len(univ)} 只)\n')
        f.write(f'- **初始资金**: {ENGINE_CFG["initial_capital"]:,.0f}\n')
        f.write(f'- **阶梯止盈**: 6%:30% / 15%:30%\n')
        f.write(f'- **成本止损**: -12% (default.yaml)\n')
        f.write(f'- **总用时**: {time.time() - t_start:.1f}s\n\n')

        f.write('## 汇总表\n\n')
        f.write('| # | 类型 | 公式 | 信号 | 选股 | 交易 | 累计收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 6%档 | 15%档 |\n')
        f.write('|---|------|------|-----:|-----:|-----:|---------:|-----:|---------:|-----:|-----:|-----:|------:|\n')
        for i, r in enumerate(results, 1):
            if r['status'] == 'ok':
                f.write(f'| {i} | {r["type"]} | {r["title"]} | {r["signals"]} | {r["selections"]} | '
                        f'{r["trades"]} | {r["cumret"]*100:+.2f}% | {r["annret"]*100:+.2f}% | '
                        f'{r["maxdd"]*100:.2f}% | {r["sharpe"]:.2f} | {r["winrate"]*100:.1f}% | '
                        f'{r["ladder_6_count"]} | {r["ladder_15_count"]} |\n')
            else:
                f.write(f'| {i} | {r["type"]} | {r["title"]} | — | — | — | — | — | — | — | — | — | — |\n')
                f.write(f'\n**{r["title"]} 失败原因**: `{r["status"]}`\n\n')

        f.write('\n## 详细结果\n\n')
        for r in results:
            f.write(f'### {r["title"]} ({r["type"]})\n\n')
            if r['status'] == 'ok':
                f.write(f'- 信号: {r["signals"]} 笔\n')
                f.write(f'- 选股样本: {r["selections"]} 笔\n')
                f.write(f'- 实际交易: {r["trades"]} 笔\n')
                f.write(f'- 累计收益: **{r["cumret"]*100:+.2f}%**\n')
                f.write(f'- 年化收益: {r["annret"]*100:+.2f}%\n')
                f.write(f'- 最大回撤: {r["maxdd"]*100:.2f}%\n')
                f.write(f'- 夏普比率: {r["sharpe"]:.2f}\n')
                f.write(f'- 胜率: {r["winrate"]*100:.1f}%\n')
                f.write(f'- 阶梯止盈: 6%档 {r["ladder_6_count"]} 笔, 15%档 {r["ladder_15_count"]} 笔\n')
                f.write(f'- 回测用时: {r["elapsed_s"]}s\n')
            else:
                f.write(f'**失败**: {r["status"]}\n')
            f.write('\n')

        f.write('\n## 注解\n\n')
        f.write('- **信号**: TDX 公式在区间内返回 XG=1 的 (stock, date) 组合数\n')
        f.write('- **选股**: 过滤 ST 股票 + 有效数据后, 实际进入回测的样本数\n')
        f.write('- **交易**: 触发实际买入并完成平仓的交易笔数\n')
        f.write('- **阶梯止盈档位**: 6% 档触发卖出 30%, 15% 档触发卖出 30% (同 bar 累加 = 60%)\n')

    print(f'  保存: {out_md}')


if __name__ == '__main__':
    main()
