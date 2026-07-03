"""批量公式回测引擎 — 解析TDX公式 → Python信号 → VERA回测

468个公式 | 2026-01-01 ~ 2026-06-03 | 全量股票(无ST)
"""
import sys, os, re, json, traceback
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

START, END = '20240601', '20260603'  # 需要2024年起的数据计算MA(250)等
BACKTEST_START = '20260101'

# ═══════════════════════════════════
# 1. 加载数据
# ═══════════════════════════════════
print("=" * 80)
print("  批量公式回测引擎")
print("=" * 80)

print("\n[1] 加载K线数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
close_raw = k["Close"].sort_index()
high_raw = k["High"].sort_index()
low_raw = k["Low"].sort_index()
open_raw = k["Open"].sort_index()
vol_raw = k.get("Volume", pd.DataFrame()).sort_index()

valid = close_raw.notna().sum() > 100
for d in [close_raw, high_raw, low_raw, open_raw, vol_raw]:
    d = d.loc[:, valid] if d is not None else None
all_cols = close_raw.columns.tolist()

def exclude_st(cl): return [c for c in cl if 'ST' not in c and '*ST' not in c]
universe = exclude_st([c for c in all_cols])
print(f"  数据: {close_raw.shape} | 股票池: {len(universe)} 只(无ST)")

# ═══════════════════════════════════
# 2. TDX公式 → Python 翻译器
# ═══════════════════════════════════
print("\n[2] 构建公式翻译器...", flush=True)

# 预计算常用指标（所有公式共享）
O = open_raw  # Open
H = high_raw  # High
L = low_raw   # Low
C = close_raw # Close
V = vol_raw   # Volume

class TdxFormulaEvaluator:
    """TDX公式求值器 — 将TDX语法转换为pandas操作"""

    def __init__(self, O, H, L, C, V):
        self.O = O; self.H = H; self.L = L; self.C = C; self.V = V
        self.vars = {}  # 变量存储
        self.vars['O'] = O; self.vars['H'] = H
        self.vars['L'] = L; self.vars['C'] = C; self.vars['V'] = V
        self.vars['CLOSE'] = C; self.vars['HIGH'] = H
        self.vars['LOW'] = L; self.vars['OPEN'] = O; self.vars['VOL'] = V
        # 预计算常用值
        self.ma_cache = {}
        self.hhv_cache = {}
        self.llv_cache = {}
        self.ema_cache = {}
        self.sma_cache = {}

    def _parse_arg(self, arg_str):
        """解析函数参数: 可能是数字或变量名"""
        arg_str = arg_str.strip()
        # 检查是否是数字
        try:
            return float(arg_str), 'number'
        except ValueError:
            pass
        # 检查是否是字符串常量
        if arg_str.startswith("'") or arg_str.startswith('"'):
            return arg_str.strip("'\""), 'string'
        # 是变量引用
        return arg_str, 'var'

    def _get_val(self, name):
        """获取变量或常量的值"""
        name = name.strip()
        try:
            return float(name)
        except ValueError:
            pass
        if name in self.vars:
            return self.vars[name]
        raise KeyError(f"未知变量: {name}")

    def _extract_n(self, arg):
        """从参数列表中提取数字N（函数参数的最后部分）"""
        try:
            return int(float(arg.strip()))
        except Exception:
            return 5  # default

    def MA(self, X, N):
        key = (id(X), N)
        if key not in self.ma_cache:
            self.ma_cache[key] = X.rolling(N, min_periods=max(1,N//2)).mean()
        return self.ma_cache[key]

    def EMA(self, X, N):
        key = (id(X), N)
        if key not in self.ema_cache:
            self.ema_cache[key] = X.ewm(span=N, adjust=False).mean()
        return self.ema_cache[key]

    def SMA(self, X, N, M=1):
        """SMA(X,N,M) = (M*X + (N-M)*REF(SMA,1))/N"""
        key = (id(X), N, M)
        if key not in self.sma_cache:
            result = X.copy()
            alpha = M / N
            for col in X.columns:
                vals = X[col].values.astype(float)
                sma_vals = np.full(len(vals), np.nan)
                first_valid = np.where(~np.isnan(vals))[0]
                if len(first_valid) > 0:
                    sma_vals[first_valid[0]] = vals[first_valid[0]]
                    for i in range(first_valid[0]+1, len(vals)):
                        if np.isnan(vals[i]):
                            sma_vals[i] = sma_vals[i-1]
                        else:
                            sma_vals[i] = (M * vals[i] + (N-M) * sma_vals[i-1]) / N
                result[col] = sma_vals
            self.sma_cache[key] = result
        return self.sma_cache[key]

    def HHV(self, X, N):
        key = (id(X), N)
        if key not in self.hhv_cache:
            self.hhv_cache[key] = X.rolling(N, min_periods=1).max()
        return self.hhv_cache[key]

    def LLV(self, X, N):
        key = (id(X), N)
        if key not in self.llv_cache:
            self.llv_cache[key] = X.rolling(N, min_periods=1).min()
        return self.llv_cache[key]

    def REF(self, X, N):
        return X.shift(N)

    def CROSS(self, A, B):
        return (A > B) & (A.shift(1) <= B.shift(1))

    def BARSLAST(self, X):
        """距上次X=True的K线数"""
        result = pd.DataFrame(np.nan, index=X.index, columns=X.columns)
        for col in X.columns:
            vals = X[col].values
            out = np.full(len(vals), np.nan)
            last_true = -1
            for i in range(len(vals)):
                if vals[i] and not (isinstance(vals[i], float) and np.isnan(vals[i])):
                    last_true = i
                if last_true >= 0:
                    out[i] = i - last_true
            result[col] = out
        return result

    def COUNT(self, X, N):
        return X.rolling(N, min_periods=1).sum()

    def EVERY(self, X, N):
        """最近N根K线X全部为真"""
        if isinstance(X, pd.DataFrame) and X.dtypes.iloc[0] == bool:
            return X.rolling(N, min_periods=1).min() > 0
        return X.rolling(N, min_periods=1).min() > 0

    def EXIST(self, X, N):
        """最近N根K线X至少一次为真"""
        return self.COUNT(X.astype(float) if hasattr(X, 'astype') else X, N) > 0

    def FILTER(self, X, N):
        """信号过滤: X为真后N根K线内不再为真"""
        result = X.copy()
        for col in X.columns:
            vals = X[col].values.astype(bool)
            out = np.zeros(len(vals), dtype=bool)
            last_true = -N - 1
            for i in range(len(vals)):
                if vals[i] and (i - last_true) > N:
                    out[i] = True
                    last_true = i
            result[col] = out
        return result

    def MAX(self, A, B):
        a = A if isinstance(A, (pd.DataFrame, pd.Series)) else float(A)
        b = B if isinstance(B, (pd.DataFrame, pd.Series)) else float(B)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return max(a, b)
        return pd.DataFrame(np.maximum(
            a.values if hasattr(a, 'values') else a,
            b.values if hasattr(b, 'values') else b
        ), index=A.index if hasattr(A, 'index') else B.index,
           columns=A.columns if hasattr(A, 'columns') else B.columns)

    def MIN(self, A, B):
        return -self.MAX(-A if hasattr(A, '__neg__') else pd.DataFrame(-float(A)), -B)

    def ABS(self, X):
        return X.abs() if hasattr(X, 'abs') else abs(X)

    def SUM(self, X, N):
        return X.rolling(N, min_periods=1).sum()

    def IF(self, cond, true_val, false_val):
        c = cond.astype(bool)
        return c * true_val + (~c) * false_val if isinstance(true_val, (int,float)) else true_val.where(c, false_val)

    def UPNDAY(self, X, N):
        """X连续N日上升"""
        result = pd.DataFrame(True, index=X.index, columns=X.columns)
        for i in range(1, N):
            result = result & (X > X.shift(i))
        return result

    def ZTPRICE(self, close, limit_ratio):
        """涨停价"""
        lr = 0.2 if isinstance(limit_ratio, float) and limit_ratio > 0.15 else limit_ratio
        return close * (1 + lr)

    def BETWEEN(self, X, low, high):
        return (X >= low) & (X <= high)

    def STD(self, X, N):
        return X.rolling(N, min_periods=1).std()

    def VAR(self, X, N):
        return X.rolling(N, min_periods=1).var()

    def FORCAST(self, X, N):
        """线性回归预测"""
        return X.rolling(N).apply(lambda x: np.polyval(np.polyfit(np.arange(N), x, 1), N-1) if len(x)==N else np.nan, raw=True)

    def CODELIKE(self, pattern):
        """股票代码模式匹配"""
        result = pd.Series(False, index=C.columns)
        for c in C.columns:
            result[c] = pattern in str(c)
        # 广播为DataFrame
        df = pd.DataFrame(False, index=C.index, columns=C.columns)
        for col in df.columns:
            df[col] = result[col]
        return df

    def NAMELIKE(self, pattern):
        return pd.DataFrame(False, index=C.index, columns=C.columns)  # 简化

    def FINANCE(self, n):
        """财务数据 — 简化返回"""
        result = pd.DataFrame(1.0, index=C.index, columns=C.columns)
        if n == 3:  # 股票类型: 4=科创板
            for col in C.columns:
                result[col] = 4.0 if col.startswith('688') else 1.0
        return result

    def HSL(self):
        """换手率 = V/CAPITAL"""
        # 简化估算
        return V / (V.rolling(20).mean() * 20) * 100  # 粗略近似

    def CAPITAL(self):
        """流通股本 — 用成交量估算"""
        return V.rolling(20).mean() * 20 / C  # 粗略近似


    def eval_expr(self, expr_str):
        """安全求值表达式（仅支持简单数学运算）"""
        try:
            return eval(expr_str, {"__builtins__": {}}, {"np": np, "pd": pd})
        except Exception:
            return None


def parse_tdx_formula(formula_code):
    """解析TDX公式代码，返回可执行的Python信号"""
    lines = formula_code.strip().split('\n')
    statements = []
    output_var = None

    for line in lines:
        line = line.strip()
        if not line or line.startswith('{') or line.startswith('//'):
            continue
        # 去掉结尾分号
        line = line.rstrip(';')
        if ':= ' in line or ':=' in line:
            # 赋值语句: VAR:=EXPR
            parts = line.split(':=', 1)
            var_name = parts[0].strip()
            expr = parts[1].strip()
            statements.append(('assign', var_name, expr))
        elif ':' in line and not line.startswith('DRAW') and not line.startswith('STICK'):
            # 输出语句: VAR:EXPR
            # 但排除 DRAWICON, STICKLINE等绘图
            for kw in ['DRAW', 'STICK', 'PLOYLINE', 'VERTLINE', 'DRAWTEXT']:
                if line.startswith(kw):
                    break
            else:
                parts = line.split(':', 1)
                output_var = parts[0].strip()
                expr = parts[1].strip()
                statements.append(('output', output_var, expr))

    return statements, output_var


def translate_to_python(statements, evaluator):
    """将解析后的公式语句翻译为Python可执行代码，返回输出信号DataFrame"""
    for stmt_type, var_name, expr in statements:
        try:
            val = translate_expr(expr.strip(), evaluator)
            evaluator.vars[var_name] = val
        except Exception as e:
            evaluator.vars[var_name] = pd.DataFrame(False, index=evaluator.C.index, columns=evaluator.C.columns)

    # 返回最后一个output变量的值
    for stmt_type, var_name, expr in reversed(statements):
        if stmt_type == 'output':
            result = evaluator.vars.get(var_name, None)
            if result is not None:
                if isinstance(result, pd.DataFrame):
                    return result.astype(bool)
    return None


def translate_expr(expr, ev):
    """翻译单个TDX表达式 → Python DataFrame/Series"""
    expr = expr.strip()

    # 处理IF(cond, true, false)
    if expr.upper().startswith('IF('):
        return _parse_if(expr, ev)

    # 处理 FILTER(X, N)
    if expr.upper().startswith('FILTER('):
        return _parse_func2('FILTER', expr, ev)

    # 处理 AND / OR 逻辑 (最低优先级)
    if ' AND ' in expr.upper():
        parts = _split_by_op(expr, ' AND ')
        result = translate_expr(parts[0], ev)
        for p in parts[1:]:
            result = result & translate_expr(p, ev).astype(bool)
        return result
    if ' OR ' in expr.upper():
        parts = _split_by_op(expr, ' OR ')
        result = translate_expr(parts[0], ev)
        for p in parts[1:]:
            result = result | translate_expr(p, ev).astype(bool)
        return result

    # 处理比较运算符
    for op in ['>=', '<=', '!=', '><', '<>', '>', '<', '=']:
        if op in expr and not _inside_func(expr, op):
            left_str, right_str = _split_by_op(expr, op)
            left = translate_expr(left_str, ev)
            right = translate_expr(right_str, ev)
            return _compare(left, right, op, ev)

    # 处理加减
    if '+' in expr and not _inside_func(expr, '+'):
        # 简单数值加减
        try:
            return float(expr)
        except Exception:
            pass
        parts = expr.rsplit('+', 1) if expr.count('+') == 1 else expr.split('+', 1)
        if len(parts) == 2 and not _inside_func(parts[0], '+'):
            return translate_expr(parts[0], ev) + translate_expr(parts[1].strip(), ev)

    # 函数调用: FUNC(args)
    m = re.match(r'(\w+)\((.*)\)$', expr)
    if m:
        func_name = m.group(1).upper()
        args_str = m.group(2)
        return _call_tdx_func(func_name, args_str, ev)

    # 字面量
    try:
        return float(expr)
    except Exception:
        pass
    if expr in ev.vars:
        return ev.vars[expr]
    # 字符串模式匹配
    if expr.startswith("'") and expr.endswith("'"):
        return expr.strip("'")
    # 默认返回False DataFrame
    return pd.DataFrame(False, index=ev.C.index, columns=ev.C.columns)


def _split_by_op(expr, op):
    """按操作符分割表达式（仅顶层，不拆分括号内）"""
    depth = 0
    op_upper = op.upper()
    expr_upper = expr.upper()
    op_len = len(op)
    for i in range(len(expr) - op_len + 1):
        c = expr[i]
        if c == '(': depth += 1
        elif c == ')': depth -= 1
        elif depth == 0 and expr_upper[i:i+op_len] == op_upper:
            # 确保是独立的操作符
            if op == ' AND ' or op == ' OR ':
                return [expr[:i].strip(), expr[i+op_len:].strip()]
            else:
                return [expr[:i].strip(), expr[i+op_len:].strip()]
    return [expr]


def _inside_func(expr, pos_str):
    """检查字符串位置是否在函数括号内"""
    pos = expr.upper().find(pos_str.upper())
    if pos < 0: return True
    depth = 0
    for i, c in enumerate(expr):
        if c == '(': depth += 1
        elif c == ')': depth -= 1
        if i == pos: return depth > 0
    return True


def _parse_if(expr, ev):
    """解析 IF(cond, true, false)"""
    content = expr[3:].strip()  # 去掉 IF(
    # 找匹配的右括号
    depth = 1
    i = 0
    while i < len(content) and depth > 0:
        if content[i] == '(': depth += 1
        elif content[i] == ')': depth -= 1
        i += 1
    args_str = content[:i-1]
    # 分割三个参数(处理嵌套逗号)
    args = _split_args(args_str, 3)
    if len(args) >= 3:
        cond = translate_expr(args[0], ev)
        true_val = translate_expr(args[1], ev)
        false_val = translate_expr(args[2], ev)
        # 布尔运算
        if isinstance(true_val, pd.DataFrame) and isinstance(false_val, pd.DataFrame):
            cond_bool = cond.astype(bool)
            result = pd.DataFrame(0, index=cond.index, columns=cond.columns)
            result[cond_bool] = true_val[cond_bool]
            result[~cond_bool] = false_val[~cond_bool]
            return result
        elif isinstance(true_val, (int,float)) and isinstance(false_val, (int,float)):
            return cond.astype(bool).astype(float) * true_val + (~cond.astype(bool)).astype(float) * false_val
    return pd.DataFrame(False, index=ev.C.index, columns=ev.C.columns)


def _parse_func2(func_name, expr, ev):
    """解析双参数函数 FILTER(X,N)"""
    content = expr[len(func_name)+1:]  # 去掉 FUNC(
    depth = 1
    i = 0
    while i < len(content) and depth > 0:
        if content[i] == '(': depth += 1
        elif content[i] == ')': depth -= 1
        i += 1
    args_str = content[:i-1]
    args = _split_args(args_str, 2)
    if len(args) >= 2:
        x = translate_expr(args[0], ev)
        n = int(float(args[1].strip()))
        if func_name.upper() == 'FILTER':
            return ev.FILTER(x, n)
    return pd.DataFrame(False, index=ev.C.index, columns=ev.C.columns)


def _split_args(args_str, expected=0):
    """智能分割逗号分隔的参数（处理嵌套括号）"""
    args = []
    depth = 0
    current = []
    for c in args_str:
        if c == '(': depth += 1
        elif c == ')': depth -= 1
        elif c == ',' and depth == 0:
            args.append(''.join(current).strip())
            current = []
            continue
        current.append(c)
    if current:
        args.append(''.join(current).strip())
    return args


def _call_tdx_func(func_name, args_str, ev):
    """调用TDX函数"""
    args = _split_args(args_str)

    # 无参数函数
    if func_name in ('CAPITAL',):
        # 返回大致流通股本
        return pd.DataFrame(1e8, index=ev.C.index, columns=ev.C.columns)
    if func_name in ('HSL',):
        # 换手率近似
        return (ev.C / ev.C.shift(1) - 1).abs() * 100  # 粗略

    if len(args) < 1:
        return pd.DataFrame(False, index=ev.C.index, columns=ev.C.columns)

    # 双参数函数
    if func_name in ('COUNT', 'EVERY', 'EXIST', 'BARSLAST', 'ABS', 'FILTER',
                     'HHV', 'LLV', 'MA', 'EMA', 'SMA', 'REF', 'SUM', 'STD', 'VAR',
                     'UPNDAY', 'FORCAST', 'MIN', 'MAX',
                     'ZTPRICE', 'CODELIKE', 'NAMELIKE', 'FINANCE', 'BETWEEN'):
        # 获取第一个参数
        arg0 = args[0].strip()
        try:
            x = translate_expr(arg0, ev)
        except Exception as e:
            logger.warning("translate_expr failed: %s", e)
            x = pd.DataFrame(0, index=ev.C.index, columns=ev.C.columns)

        # 获取第二个参数(如果有)
        n = 5
        if len(args) >= 2:
            n_str = args[1].strip()
            try:
                n = int(float(n_str))
            except Exception:
                n = 5

        # 调用对应的Python方法
        method = getattr(ev, func_name, None)
        if method:
            if func_name in ('MA', 'EMA', 'SMA', 'HHV', 'LLV', 'REF', 'COUNT', 'EVERY', 'EXIST',
                             'SUM', 'STD', 'VAR', 'UPNDAY', 'FORCAST', 'FILTER', 'MIN', 'MAX', 'ABS'):
                if func_name in ('MIN', 'MAX'):
                    n_val = ev._get_val(args[1].strip()) if len(args) >= 2 else n
                    return method(x, n_val) if func_name == 'MAX' else method(x, n_val)
                return method(x, n)
            elif func_name == 'BARSLAST':
                return method(x)
            elif func_name == 'ZTPRICE':
                return method(x, 0.2 if isinstance(n, str) else n)
            elif func_name in ('CODELIKE', 'NAMELIKE'):
                return method(args[0].strip().strip("'\""))

    # CROSS函数
    if func_name == 'CROSS':
        if len(args) >= 2:
            a = translate_expr(args[0].strip(), ev)
            b = translate_expr(args[1].strip(), ev)
            return (a > b) & (a.shift(1) <= b.shift(1))

    # BETWEEN函数
    if func_name == 'BETWEEN':
        if len(args) >= 3:
            x = translate_expr(args[0].strip(), ev)
            lo = translate_expr(args[1].strip(), ev)
            hi = translate_expr(args[2].strip(), ev)
            lo_v = lo if isinstance(lo, (int,float)) else lo
            hi_v = hi if isinstance(hi, (int,float)) else hi
            return (x >= lo_v) & (x <= hi_v)

    # 默认
    return pd.DataFrame(False, index=ev.C.index, columns=ev.C.columns)


def _compare(left, right, op, ev):
    """比较运算"""
    lv = left.values if isinstance(left, pd.DataFrame) else left
    rv = right.values if isinstance(right, pd.DataFrame) else right
    if isinstance(lv, np.ndarray) and isinstance(rv, np.ndarray):
        if op in ('>',): result = lv > rv
        elif op in ('<',): result = lv < rv
        elif op in ('>=',): result = lv >= rv
        elif op in ('<=',): result = lv <= rv
        elif op in ('=', '=='): result = lv == rv
        elif op in ('!=', '<>'): result = lv != rv
        else: result = np.zeros_like(lv, dtype=bool)
        return pd.DataFrame(result, index=left.index, columns=left.columns)
    elif isinstance(lv, (int,float)) and isinstance(rv, np.ndarray):
        if op in ('>',): result = lv > rv
        elif op in ('<',): result = lv < rv
        elif op in ('>=',): result = lv >= rv
        elif op in ('<=',): result = lv <= rv
        elif op in ('=', '=='): result = lv == rv
        elif op in ('!=', '<>'): result = lv != rv
        else: result = np.zeros_like(rv, dtype=bool)
        idx = right.index if isinstance(right, pd.DataFrame) else left.index
        cols = right.columns if isinstance(right, pd.DataFrame) else left.columns
        return pd.DataFrame(result, index=idx, columns=cols)
    return pd.DataFrame(False, index=ev.C.index, columns=ev.C.columns)


# ═══════════════════════════════════
# 3. 批量处理公式
# ═══════════════════════════════════
print("\n[3] 批量解析公式...", flush=True)
gongshi_dir = r'E:\gongshi'
files = sorted([f for f in os.listdir(gongshi_dir) if f.endswith('.md')])

# 统一止盈止损配置
STOP_CONFIG = load_stop_config()

ENGINE_CFG = {
    'initial_capital': 200000.0, 'commission': 0.0003, 'slippage': 0.001, 'period': '1d',
    'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 10000.0, 'lot_size': 100, 'min_lots': 1},
}

results = []
ev = TdxFormulaEvaluator(O, H, L, C, V)

for fi, fname in enumerate(files):
    # 进度提示
    if fi % 50 == 0:
        print(f"  进度: {fi}/{len(files)}...", flush=True)

    filepath = os.path.join(gongshi_dir, fname)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        logger.warning("Read formula file failed: %s", e)
        continue

    # 提取公式名称
    name_match = re.search(r'^#\s*(.+?)(?:之选股指标公式)?\s*$', content, re.MULTILINE)
    formula_title = name_match.group(1).strip() if name_match else fname.replace('.md', '')

    # 提取代码块
    code_match = re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`', content, re.DOTALL)
    if not code_match:
        continue
    formula_code = code_match.group(1)

    # 解析公式
    try:
        statements, output_var = parse_tdx_formula(formula_code)
        if output_var is None:
            continue
    except Exception as e:
        logger.warning("Read formula file failed: %s", e)
        continue

    # 翻译并生成信号
    try:
        sig_df = translate_to_python(statements, ev)
        if sig_df is None or not isinstance(sig_df, pd.DataFrame):
            continue
        # 只取回测区间
        sig_backtest = sig_df.loc[BACKTEST_START:]
        total_sig = sig_backtest.sum().sum()
        if total_sig < 5:  # 信号太少，跳过
            continue
    except Exception as e:
        continue

    # 运行回测
    try:
        from backtest.engine import BacktestEngine

        # 构建selections
        recs = []
        for col in universe:
            if col not in sig_backtest.columns: continue
            for idx in sig_backtest.index[sig_backtest[col]]:
                recs.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
        sel_df = pd.DataFrame(recs)
        if len(sel_df) < 10: continue

        common = sorted(set(C.columns) & set(sel_df['stock_code'].unique()))
        if len(common) < 5: continue
        cs = C[common].ffill().bfill()
        hs = H.reindex(index=cs.index, columns=common).ffill().bfill()
        ls = L.reindex(index=cs.index, columns=common).ffill().bfill()

        entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
        for _, row in sel_df.iterrows():
            code, dt = row['stock_code'], pd.to_datetime(row['select_date'])
            if code not in entries.columns: continue
            if dt in entries.index:
                entries.loc[dt, code] = True
            else:
                m = entries.index >= dt
                if m.any(): entries.loc[entries.index[m][0], code] = True

        engine = BacktestEngine(ENGINE_CFG)
        bp = np.array([0.05, 0.12], dtype=np.float64)
        br = np.array([0.30, 0.30], dtype=np.float64)
        bresult = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                                    ls.values.astype(np.float64), STOP_CONFIG, sel_df,
                                    bp, br, 2, skip_sm=True)

        m = bresult['metrics']
        cumret = bresult['cumulative_return']
        annret = m.get('annualized_return', 0)
        maxdd = m.get('max_drawdown', 0)
        sharpe = m.get('sharpe_ratio', 0)
        winrate = m.get('win_rate', 0)
        trades = len(bresult['trades'])

        results.append({
            'file': fname,
            'title': formula_title,
            'output_var': output_var,
            'signals': total_sig,
            'cumret': cumret,
            'annret': annret,
            'maxdd': maxdd,
            'sharpe': sharpe,
            'winrate': winrate,
            'trades': trades,
        })
    except Exception as e:
        continue

# ═══════════════════════════════════
# 4. 排序输出
# ═══════════════════════════════════
print(f"\n[4] 排序输出... 成功回测: {len(results)} 个公式", flush=True)
results.sort(key=lambda r: r['annret'] if r['annret'] is not None else -999, reverse=True)

print("\n" + "=" * 100)
print(f"  TOP 100 公式 — 批量回测结果")
print(f"  回测区间: {BACKTEST_START} ~ {END}")
print(f"  股票池: 全量(无ST) {len(universe)}只")
print("=" * 100)
print(f"  {'#':<5} {'公式名称':<40} {'文件':<35} {'收益%':>7} {'年化%':>7} {'回撤%':>7} {'夏普':>5} {'胜率%':>6} {'交易':>6}")
print("  " + "-" * 100)

for i, r in enumerate(results[:100], 1):
    print(f"  {i:<5} {r['title'][:38]:<40} {r['file'][:33]:<35} "
          f"{r['cumret']*100:>+7.2f} {r['annret']*100:>+7.2f} {r['maxdd']*100:>+7.2f} "
          f"{r['sharpe']:>5.2f} {r['winrate']*100:>6.1f} {r['trades']:>6}")

# 保存
output = {
    'config': {'period': f'{BACKTEST_START}~{END}', 'universe': '全量(无ST)', 'total_formulas': len(files), 'successful': len(results)},
    'top100': results[:100],
    'all': results,
}
with open('output/batch_formula_top100.json', 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2, default=str)

print(f"\n  结果已保存: output/batch_formula_top100.json")
print("=" * 80)
