# -*- coding: utf-8 -*-
"""解释器单元测试 (2026-08-26, 全新编写)。

每个函数: 构造 5~10 根小样本, 手工计算期望值 → 逐位比对。
重点锚定: SMA 递推 / HHV 右对齐窗口 / FILTER 顺序语义 / REF / CROSS。
运行: python tools/formula_pipeline/interpreter/selftest.py
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.formula_pipeline.interpreter.functions import (  # noqa: E402
    UnsupportedFormula, _roll_max, _roll_min, _roll_sum, _roll_count,
    _roll_every, _roll_exist, _f_ma, _f_ema, _f_sma, _f_dma, _f_cross,
    _f_longcross, _f_barslast, _f_barscount, _f_filter, _rolling_stat,
    _f_avedev, _shift, _hhvbars, _f_reg, call_function)
from tools.formula_pipeline.interpreter.runner import (  # noqa: E402
    run_formula)
from tools.formula_pipeline.interpreter.parser import parse_formula  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want, tol=1e-9):
    got = np.asarray(got, dtype=np.float64).reshape(-1)
    want = np.asarray(want, dtype=np.float64).reshape(-1)
    if got.shape != want.shape:
        FAIL.append((name, got.tolist(), want.tolist()))
        print(f"  ✗ {name} shape {got.shape}!={want.shape}\n"
              f"    got  = {got.tolist()}\n    want = {want.tolist()}")
        return
    if np.array_equal(np.isnan(got), np.isnan(want)) and \
            np.allclose(got[~np.isnan(want)], want[~np.isnan(want)], atol=tol):
        PASS.append(name)
    else:
        FAIL.append((name, got.tolist(), want.tolist()))
        print(f"  ✗ {name}\n    got  = {got.tolist()}\n    want = {want.tolist()}")


def col(x):
    """一维序列 → (T,1) 矩阵。"""
    return np.asarray(x, dtype=np.float64).reshape(-1, 1)


# ---------------------------------------------------------------- 窗口族

def test_windows():
    x = col([3, 1, 4, 1, 5, 9, 2, 6])
    # HHV(C,3): t2起严格窗口; 前两根用现有 (TDX 宽松族)
    check("HHV3", _roll_max(x, 3), [3, 3, 4, 4, 5, 9, 9, 9])
    check("LLV3", _roll_min(x, 3), [3, 1, 1, 1, 1, 1, 2, 2])
    # HHV(C,0) = 历史最高
    check("HHV0", _roll_max(x, 0), [3, 3, 4, 4, 5, 9, 9, 9])
    # SUM(X,3) 宽松: 前两根部分和
    check("SUM3", _roll_sum(x, 3), [3, 4, 8, 6, 10, 15, 16, 17])
    # SUM(X,0) 累计
    check("SUM0", _roll_sum(x, 0), [3, 4, 8, 9, 14, 23, 25, 31])
    c = col([1, 0, 1, 1, 0, 0, 1, 0])
    check("COUNT3", _roll_count(c, 3), [1, 1, 2, 2, 2, 1, 1, 1])
    check("EVERY3", _roll_every(c, 3).astype(float),
          [1, 0, 0, 0, 0, 0, 0, 0])
    check("EXIST3", _roll_exist(c, 3).astype(float),
          [1, 1, 1, 1, 1, 1, 1, 1])
    # NaN 语义: 停牌段
    xn = col([3, np.nan, 4, 1, 5])
    check("HHV3_nan", _roll_max(xn, 3), [3, 3, 4, 4, 5])
    # 严格族: 窗口含 NaN → NaN; t=4 窗口 [4,1,5] 全有效 → 10/3
    check("MA3_nan", _f_ma(xn, 3),
          [np.nan, np.nan, np.nan, np.nan, 10.0 / 3])
    # 宽松族: NaN 当 0 参与和 (TDX: 停牌无价, 现有数据求和)
    check("SUM3_nan", _roll_sum(xn, 3), [3, 3, 7, 5, 10])


def test_ma_ema_sma():
    x = col([1, 2, 3, 4, 5, 6, 7, 8])
    check("MA4", _f_ma(x, 4), [np.nan, np.nan, np.nan, 2.5, 3.5, 4.5, 5.5, 6.5])
    # EMA(X,5): alpha=2/6=1/3, Y0=X0
    a = 2.0 / 6
    y = [1.0]
    for v in [2, 3, 4, 5, 6, 7, 8]:
        y.append(a * v + (1 - a) * y[-1])
    check("EMA5", _f_ema(x, 5), y)
    # SMA(X,3,1): alpha=1/3 与 EMA(X,~) 同型
    check("SMA3_1", _f_sma(x, 3, 1), y)  # alpha 一致
    # SMA(X,3,2): alpha=2/3
    a = 2.0 / 3
    y2 = [1.0]
    for v in [2, 3, 4, 5, 6, 7, 8]:
        y2.append(a * v + (1 - a) * y2[-1])
    check("SMA3_2", _f_sma(x, 3, 2), y2)
    # DMA(X,0.5)
    y3 = [1.0]
    for v in [2, 3, 4, 5, 6, 7, 8]:
        y3.append(0.5 * v + 0.5 * y3[-1])
    check("DMA05", _f_dma(x, 0.5), y3)


def test_ref_cross():
    x = col([1, 2, 3, 4, 5])
    check("REF2", _shift(x, 2), [np.nan, np.nan, 1, 2, 3])
    a = col([1, 1.5, 3, 2, 1])
    b = col([2, 2, 2, 2, 2])
    check("CROSS", _f_cross(a, b), [0, 0, 1, 0, 0])
    # LONGCROSS(A,B,2): 前两期 A 连续 < B 后今日上穿 (t0=1<t, t1=1.5<B, t2=3>B)
    check("LONGCROSS", _f_longcross(a, b, 2), [0, 0, 1, 0, 0])


def test_bars():
    c = col([1, 0, 0, 1, 0, 0, 0, 1])
    check("BARSLAST", _f_barslast(c), [0, 1, 2, 0, 1, 2, 3, 0])
    check("BARSCOUNT", _f_barscount(c), [1, 2, 3, 4, 5, 6, 7, 8])
    x = col([3, 1, 4, 1, 5, 9, 2, 6])
    # HHVBARS(X,3): 距窗口最高的周期数 (右对齐含当日, 最高=当日 → 0)
    # t6 窗口[5,9,2] max=9@t5 → 1; t7 窗口[9,2,6] max=9@t5 → 2
    check("HHVBARS3", _hhvbars(x, 3), [0, 1, 0, 1, 0, 0, 1, 2])
    # FILTER(X,2): 信号后2根内的信号删除
    cf = col([1, 1, 0, 1, 1, 1, 0, 0])
    check("FILTER2", _f_filter(cf, 2), [1, 0, 0, 1, 0, 0, 0, 0])


def test_stat():
    x = col([1, 2, 3, 4, 5])
    # STD(X,4) 样本: 窗口 [1,2,3,4]→std=1.114..; [2..5] 同
    s, m = _rolling_stat(x, 4, 1)
    import math
    v1 = math.sqrt(((1 - 2.5) ** 2 + (2 - 2.5) ** 2 + (3 - 2.5) ** 2 + (4 - 2.5) ** 2) / 3)
    check("STD4", s, [np.nan, np.nan, np.nan, v1, v1])
    check("MEAN4", m, [np.nan, np.nan, np.nan, 2.5, 3.5])
    # AVEDEV(X,4)
    ad = _f_avedev(x, 4)
    dev = (abs(1 - 2.5) + abs(2 - 2.5) + abs(3 - 2.5) + abs(4 - 2.5)) / 4
    check("AVEDEV4", ad, [np.nan, np.nan, np.nan, dev, dev])
    # SLOPE/FORCAST(X,4): 前三窗斜率1, 末端拟合=窗口末值 (线性序列)
    sl, fi = _f_reg(x, 4)
    check("SLOPE4", sl, [np.nan, np.nan, np.nan, 1.0, 1.0])
    check("FORCAST4", fi, [np.nan, np.nan, np.nan, 4.0, 5.0])


def test_dispatch():
    ctx = {"codes": ["600000.SH", "000001.SZ"], "T": 3, "N": 2}
    one = np.ones((3, 2))
    check("IF", call_function("IF", [one, one * 2, one * 3], ctx), one * 2)
    check("AND", call_function("AND", [one, one], ctx), one)
    check("ABS", call_function("ABS", [-one], ctx), one)
    check("MAX", call_function("MAX", [one, one * 2], ctx), one * 2)
    check("BETWEEN", call_function("BETWEEN", [one * 2, one, one * 3], ctx), one)
    try:
        call_function("WINNER", [one], ctx)
        FAIL.append(("WINNER 应拒", None, None))
    except UnsupportedFormula:
        PASS.append("WINNER 拒绝")
    try:
        call_function("REF", [one, -1], ctx)
        FAIL.append(("REF-1 应拒", None, None))
    except UnsupportedFormula:
        PASS.append("REF 负周期拒绝")


# ---------------------------------------------------------------- 端到端

def test_end_to_end():
    data = {
        "open": col([1, 1, 1, 1, 1, 1]),
        "high": col([2, 2, 2, 2, 2, 2]),
        "low": col([0.5, 0.5, 0.5, 0.5, 0.5, 0.5]),
        "close": col([1, 2, 3, 4, 3, 5]),
        "volume": col([10, 20, 30, 40, 50, 60]),
        "amount": col([10, 40, 90, 160, 150, 300]),
        "__index__": __import__("pandas").date_range("2024-01-01", periods=6),
    }
    # 1) 均线金叉: MA3 上穿 MA5 (t=4: MA3=10/3≈3.33 < MA5=2.6? 手算)
    src1 = "A:=MA(C,3);\nB:=MA(C,5);\nXG:CROSS(A,B);"
    sig1, _, _ = run_formula(src1, [], data, ["000001.SZ"])
    ma3 = [np.nan, np.nan, 2, 3, 10 / 3, 4]
    ma5 = [np.nan, np.nan, np.nan, np.nan, 2.6, 17 / 5]
    want = [0, 0, 0, 0, 0, 0]
    for t in range(6):
        if t and not np.isnan(ma3[t - 1]) and not np.isnan(ma5[t - 1]):
            want[t] = int(ma3[t] > ma5[t] and not (ma3[t - 1] > ma5[t - 1]))
    check("E2E_金叉", sig1[:, 0], want)

    # 2) 裸表达式输出 + 参数绑定 (未定义 N ← 参数 2)
    #    X = C>REF(C,2) 且 V>15; REF 宽松: t=2 时 REF= C[0] 有效
    src2 = "X:=C>REF(C,N);\nX AND V>15;"
    sig2, _, undef = run_formula(src2, ["2"], data, ["000001.SZ"])
    want2 = [0, 0, 1, 1, 0, 1]
    check("E2E_裸表达式+参数", sig2[:, 0], want2)
    assert "N" in undef, f"N 应在 undefined: {undef}"

    # 3) 具名输出 + 装饰画图跳过 (STICKLINE 不影响信号);
    #    信号 = 唯一输出 B=REF(MA(C,2),1) 的值 > 0 (用户确认语义)
    src3 = ("A:=MA(C,2);\nB:REF(A,1);\nSTICKLINE(A>B,0,A,6,0),COLORRED;\n")
    sig3, _, _ = run_formula(src3, [], data, ["000001.SZ"])
    want3 = [0, 0, 1, 1, 1, 1]  # nan→0, 1.5/2.5/3.5/3.5/4 均 >0
    check("E2E_画图跳过", sig3[:, 0], want3)


def test_parse_features():
    stmts, undef = parse_formula("A:=MA(C,5);\nXG:CROSS(A,MA(C,10)),COLORRED;")
    assert len([s for s in stmts if type(s).__name__ == "Output"]) == 1
    stmts2, u2 = parse_formula("XG:REF(C,N);\n")
    assert "N" in u2
    stmts3, u3 = parse_formula("V0:=C;V1:=REF(V0,1);\nV0 AND V1;\n")
    assert u3 == [], f"V0/V1 已定义, 不应进 undefined: {u3}"
    PASS.append("解析特性")


def main():
    tests = [test_windows, test_ma_ema_sma, test_ref_cross, test_bars,
             test_stat, test_dispatch, test_end_to_end, test_parse_features]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL.append((t.__name__, f"异常: {e}", None))
            print(f"  ✗ {t.__name__} 异常: {e}")
    print(f"\n[PASS {len(PASS)} / FAIL {len(FAIL)}]")
    if FAIL:
        print("失败项:", [f[0] for f in FAIL])
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
