# -*- coding: utf-8 -*-
"""公式解释器执行器 (2026-08-26, 全新编写)。

职责: AST 求值 → (股票×日期) 布尔信号矩阵。
- 数据: 本地 parquet K 线 (data/kline_cache/1d), 只用 OHLCV+amount
- 参数: txt 参数行值按「未定义标识符出现序」绑定 (P1..Pn / N / M 均可)
- 信号: 唯一输出语句 (具名或裸表达式) 的值 > 0 (用户确认语义)
- 求值策略: 按股票分块 (T×chunk) 矩阵化, 变量环境内逐语句递推
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.formula_pipeline.interpreter.functions import (  # noqa: E402
    UnsupportedFormula, call_function)
from tools.formula_pipeline.interpreter.parser import (  # noqa: E402
    BUILTIN_VARS as _BUILTIN_VARS, Bare, Assign, Output, DrawStmt, Num, Var,
    Bin, Cmp, Logic, Not, Neg, Call, IndRef, _Str, parse_formula, signal_expr)

KLINE_DIR = Path(r"E:\1target\VERA\data\kline_cache\1d")

# 沪深 A 股代码前缀 (用户: 仅上交所深交所; 60=沪主板 68=科创板 00=深主板 30=创业板)
SH_PREFIXES = ("60", "68")
SZ_PREFIXES = ("00", "30")


def shsz_codes() -> list:
    """本地日线缓存中的沪深 A 股代码 (剔北交所/指数/其他)。"""
    codes = []
    for p in KLINE_DIR.glob("*.parquet"):
        stem = p.stem
        if "." not in stem:
            continue
        num, mkt = stem.split(".", 1)
        if mkt == "SH" and num.startswith(SH_PREFIXES):
            codes.append(stem)
        elif mkt == "SZ" and num.startswith(SZ_PREFIXES):
            codes.append(stem)
    return sorted(codes)


def load_matrix(codes: list, start=None, end=None, field="close") -> np.ndarray:
    """加载 (T, N) 矩阵; 日期轴 = 全体股票日期并集 (排序)。"""
    pass  # 由 DataLoader 统一加载


class DataLoader:
    """一次性加载股票块的全字段 K 线 → (T, chunk) 矩阵字典。"""

    def __init__(self, codes: list, start=None, end=None):
        self.codes = codes
        self.start = pd.Timestamp(start) if start else None
        self.end = pd.Timestamp(end) if end else None

    def load_chunk(self, chunk_codes, fixed_index=None) -> dict:
        """加载股票块。fixed_index 提供统一日期轴 (跨块可拼接), 缺失文件剔除。"""
        used = []
        frames = {}
        for c in chunk_codes:
            fp = KLINE_DIR / f"{c}.parquet"
            if not fp.exists():
                continue
            try:
                df = pd.read_parquet(fp)
            except Exception:
                continue
            df["date"] = pd.to_datetime(df["date"])
            if self.start is not None:
                df = df[df["date"] >= self.start]
            if self.end is not None:
                df = df[df["date"] <= self.end]
            frames[c] = df.set_index("date")
            used.append(c)
        if fixed_index is not None:
            idx = pd.DatetimeIndex(fixed_index)
        elif frames:
            all_dates = sorted(set().union(*[set(f.index) for f in frames.values()]))
            idx = pd.DatetimeIndex(all_dates)
        else:
            empty = np.full((0, 0), np.nan)
            out = {k: empty for k in
                   ("open", "high", "low", "close", "volume", "amount")}
            out["__codes__"] = used
            return out
        out = {}
        for field in ("open", "high", "low", "close", "volume", "amount"):
            mat = np.full((len(idx), len(used)), np.nan)
            for j, c in enumerate(used):
                s = frames[c].get(field)
                if s is not None:
                    mat[:, j] = s.reindex(idx).values
            out[field] = mat
        out["__index__"] = idx
        out["__codes__"] = used
        return out


class Evaluator:
    """AST 求值器: 逐语句执行, 变量环境 → 矩阵。"""

    def __init__(self, ctx):
        self.ctx = ctx  # {"codes": [...], "T": int, "N": int}

    def eval(self, node, env):
        if isinstance(node, Num):
            return np.float64(node.v)
        if isinstance(node, _Str):
            return node.s
        if isinstance(node, Var):
            up = node.name.upper()
            if up in env:
                return env[up]
            raise UnsupportedFormula(f"未定义变量 {node.name}")
        if isinstance(node, Neg):
            return -np.asarray(self.eval(node.e, env), dtype=np.float64)
        if isinstance(node, Not):
            v = self.eval(node.e, env)
            v = np.asarray(v, dtype=np.float64)
            return np.where(np.isnan(v), np.nan,
                            (np.nan_to_num(v, nan=0.0) == 0).astype(np.float64))
        if isinstance(node, Bin):
            l = self.eval(node.l, env)
            r = self.eval(node.r, env)
            l = np.asarray(l, dtype=np.float64)
            r = np.asarray(r, dtype=np.float64)
            if node.op == "+":
                return l + r
            if node.op == "-":
                return l - r
            if node.op == "*":
                return l * r
            if node.op == "/":
                with np.errstate(divide="ignore", invalid="ignore"):
                    return l / r
            if node.op == "^":
                with np.errstate(divide="ignore", invalid="ignore"):
                    return np.power(l, r)
            raise UnsupportedFormula(f"运算符 {node.op}")
        if isinstance(node, Cmp):
            l = np.asarray(self.eval(node.l, env), dtype=np.float64)
            r = np.asarray(self.eval(node.r, env), dtype=np.float64)
            op = {"=": "==", "==": "==", "<>": "!=", "!=": "!="}.get(
                node.op, node.op)
            with np.errstate(invalid="ignore"):
                if op == ">":
                    return (l > r)
                if op == "<":
                    return (l < r)
                if op == ">=":
                    return (l >= r)
                if op == "<=":
                    return (l <= r)
                if op == "==":
                    return (l == r)
                if op == "!=":
                    return (l != r)
            raise UnsupportedFormula(f"比较符 {node.op}")
        if isinstance(node, Logic):
            l = np.asarray(self.eval(node.l, env), dtype=np.float64)
            r = np.asarray(self.eval(node.r, env), dtype=np.float64)
            if node.op == "AND":
                return ((l > 0) & (r > 0)).astype(np.float64)
            return ((l > 0) | (r > 0)).astype(np.float64)
        if isinstance(node, Call):
            args = [self.eval(a, env) for a in node.args]
            return call_function(node.name, args, self.ctx)
        if isinstance(node, IndRef):
            params = [float(np.ravel(self.eval(p, env))[0])
                      for p in node.params]
            return self._indicator_line(node.ind, node.line, params, env)
        raise UnsupportedFormula(f"未知节点 {type(node).__name__}")

    # ---------------------------------------------------------------- 指标线

    def _indicator_line(self, ind, line, params, env):
        """经典指标线本地计算 (跨指标引用 MACD.DEA / "KDJ.K"(9,3,3) 等)。"""
        from tools.formula_pipeline.interpreter.functions import (
            _f_ema, _f_sma, _roll_max, _roll_min, _shift)

        def p(i, default):
            return int(params[i]) if i < len(params) and params[i] else default

        C, H, L = env.get("CLOSE"), env.get("HIGH"), env.get("LOW")
        if C is None or H is None or L is None:
            raise UnsupportedFormula("指标引用缺行情数据")
        if ind == "MACD":
            s, lg, m = p(0, 12), p(1, 26), p(2, 9)
            dif = _f_ema(C, s) - _f_ema(C, lg)
            if line == "DIF":
                return dif
            if line == "DEA":
                return _f_ema(dif, m)
            if line in ("MACD", "MACD柱"):
                dea = _f_ema(dif, m)
                return 2.0 * (dif - dea)
        elif ind == "KDJ":
            n, k_s, d_s = p(0, 9), p(1, 3), p(2, 3)
            hh, ll = _roll_max(H, n), _roll_min(L, n)
            with np.errstate(divide="ignore", invalid="ignore"):
                rsv = (C - ll) / (hh - ll) * 100.0
            k = _f_sma(rsv, k_s, 1)
            if line == "K":
                return k
            d = _f_sma(k, d_s, 1)
            if line == "D":
                return d
            if line == "J":
                return 3.0 * k - 2.0 * d
        elif ind == "RSI":
            n1 = p(0, 6)
            diff = C - _shift(C, 1)
            up = _f_sma(np.where(diff > 0, diff, 0.0), n1, 1)
            dn = _f_sma(np.abs(diff), n1, 1)
            with np.errstate(divide="ignore", invalid="ignore"):
                rsi = up / dn * 100.0
            if line in ("RSI1", "RSI"):
                return rsi
            if line == "RSI2":
                n2 = p(1, 12) if len(params) > 1 else 12
                up2 = _f_sma(np.where(diff > 0, diff, 0.0), n2, 1)
                dn2 = _f_sma(np.abs(diff), n2, 1)
                with np.errstate(divide="ignore", invalid="ignore"):
                    return up2 / dn2 * 100.0
            if line == "RSI3":
                n3 = p(2, 24) if len(params) > 2 else 24
                up3 = _f_sma(np.where(diff > 0, diff, 0.0), n3, 1)
                dn3 = _f_sma(np.abs(diff), n3, 1)
                with np.errstate(divide="ignore", invalid="ignore"):
                    return up3 / dn3 * 100.0
        elif ind == "MTM":
            n = p(0, 12)
            if line == "MTM":
                return C - _shift(C, n)
        raise UnsupportedFormula(f"指标引用 {ind}.{line} 不支持")

    def run_statements(self, stmts, env):
        for s in stmts:
            if isinstance(s, DrawStmt):
                continue
            if isinstance(s, (Assign, Output)):
                env[s.name.upper()] = self.eval(s.expr, env)
            elif isinstance(s, Bare):
                env["__LAST_BARE__"] = self.eval(s.expr, env)


# ---------------------------------------------------------------- 主入口

def bind_params(undefined_names: list, param_values: list) -> dict:
    """未定义标识符 ← 参数行值 (按出现序绑定; 值不足/无参数行 → 公式出局)。"""
    bind = {}
    for i, name in enumerate(undefined_names):
        if i < len(param_values):
            try:
                bind[name] = np.float64(float(param_values[i]))
            except ValueError:
                raise UnsupportedFormula(f"参数值非法: {param_values[i]!r}")
        else:
            raise UnsupportedFormula(f"参数 {name} 无默认值 (参数行不足)")
    return bind


def run_formula(source: str, param_values: list, data: dict, codes: list):
    """公式源码 + K 线数据 → 信号布尔矩阵 (T, N)。

    data: DataLoader.load_chunk 输出 (含 open/high/low/close/volume/amount/__index__)
    返回 (signal_matrix, date_index, undefined_used)
    """
    stmts, undefined = parse_formula(source)
    # 内置行情变量不是参数
    undefined = [u for u in undefined if u not in _BUILTIN_VARS]
    sig = signal_expr(stmts)
    if sig is None:
        raise UnsupportedFormula("无有效输出语句")

    T = len(data["__index__"])
    N = len(codes)
    ctx = {"codes": codes, "T": T, "N": N}

    env = {}
    for k, field in _BUILTIN_VARS.items():
        env[k] = data[field]
    env.update(bind_params(undefined, param_values))

    ev = Evaluator(ctx)
    ev.run_statements(stmts, env)

    if isinstance(sig, Output):
        val = env.get(sig.name.upper())
        if val is None:
            val = ev.eval(sig.expr, env)
    else:
        val = env.get("__LAST_BARE__")
        if val is None:
            val = ev.eval(sig.expr, env)
    val = np.asarray(val, dtype=np.float64)
    if val.ndim == 0:
        val = np.full((T, N), float(val))
    signal = np.nan_to_num(val, nan=0.0) > 0
    return signal, data["__index__"], undefined


def run_formula_batch(source: str, param_values: list, codes: list,
                      start=None, end=None, chunk_size=400,
                      fixed_index=None):
    """全量执行: 分块加载 → 分块求值 → 列拼接 → (signal, index, used_codes)。

    fixed_index: 统一日期轴 (推荐传基准日历), 保证各块行对齐可拼接。
    """
    loader = DataLoader(codes, start, end)
    if fixed_index is not None:
        idx_all = pd.DatetimeIndex(pd.to_datetime(list(fixed_index)))
        if start is not None:
            idx_all = idx_all[idx_all >= pd.Timestamp(start)]
        if end is not None:
            idx_all = idx_all[idx_all <= pd.Timestamp(end)]
    else:
        idx_all = None
    sig_parts, used_all, index = [], [], None
    for c0 in range(0, len(codes), chunk_size):
        chunk = codes[c0:c0 + chunk_size]
        data = loader.load_chunk(chunk, fixed_index=idx_all)
        used = data.pop("__codes__", [])
        if not used:
            continue
        sig, idx, undef = run_formula(source, param_values, data, used)
        index = idx if index is None else index
        sig_parts.append(sig)
        used_all.extend(used)
    if not sig_parts:
        return np.zeros((0, 0), dtype=bool), pd.DatetimeIndex([])
    signal = np.concatenate(sig_parts, axis=1)
    return signal, index, used_all
