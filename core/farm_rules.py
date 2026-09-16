# -*- coding: utf-8 -*-
"""公式农场判定口径 — **单一真相源** (2026-09-11)。

2026-09-11 用户拍板: 粗扫达标线统一定为

    **年化 ≥ 15% 且 |最大回撤| ≤ 15% 且 笔数 ≥ 20**

三条口径为什么长这样 (全部有出处, 不是拍脑袋):

- **15%**: QUANTQQ 系扫描器里早就写着"用户拍板: 年化不低于 15%"
  (`tools/quantqq_1m_sweep.py:48`), 09-09 那批农场粗扫实际也在用 ≥15%
  (`docs/2026-09-09_公式农场第一批回测结论_研究报告.md:15` 的"粗扫达标 5 条");
  而 gs 系三份报告 (`gs_5m_sweep` / `gs_make_report` / `gs_1d_detail_report`)
  各自硬编码了一份 `0.30 / 0.15 / 1000` —— **同一条规则三份实现、两个数**,
  2026-09-11 审计定为缺陷, 本模块收口。
- **回撤 ≤ 15%**: 与 09-09 的口径一致 (账户级回撤; 注意本口径是"轻仓"回撤,
  见下方"口径提醒")。
- **笔数 ≥ 20**: 最小样本门槛。不足 20 笔的组合 **记「样本不足」, 既不判达标,
  也不参与「最优组合」评选** —— 2026-09-11 事件: 3 笔 100% 胜率、卡玛 8.23 的
  GS1292 被农场报告当成「最优组合」摆出来, 属于拿噪声当结论。

**口径提醒 (报告里也照抄一句)**: 粗扫用 300 万本金 + 单票上限 2 万, 账户年化
≈ 仓位暴露 × 单笔边际 × 笔数, 所以年化主要在排"这公式出票多不多"; 轻仓 + 少样本
时夏普会结构性为负 (`backtest/metrics.py` 每期扣 1.5%/年无风险利率)、卡玛在
|回撤|<0.01% 时被死区归零 —— 这几个指标别单独拿来下结论。

`research/` 下各研究脚本自己的门槛 (18.88% / 23% / 25%) 是各自课题口径, 不归本模块管。
"""
from __future__ import annotations

import math

#: 达标: 年化下限
TARGET_ANN = 0.15
#: 达标: 最大回撤绝对值上限
TARGET_MAXDD = 0.15
#: 最小样本门槛 (笔数), 不足则记「样本不足」
MIN_TRADES = 20

#: 粗扫执行口径 — 单一真相源 (2026-09-16 达标榜回填回测页计划书收口)。
#: 原 farm_backtest.CALIBER 手写一份 (报告抬头用), 回填接口需要同一份 →
#: 两处手写必然漂 (池子改一处不改另一处 = 报告写沪深300实际跑全A)。
#: farm_backtest.CALIBER 是本常量的别名; 实际取数链 = gs_5m_sweep 读
#: config/default.yaml (universe type 23), 与本常量一致 (2026-09-16 验证链)。
SWEEP_CALIBER = {
    "universe_type": "23", "universe": "沪深300 (TDX type 23)",
    "period": "5m", "dividend": "前复权",
    "capital": 3_000_000.0, "max_buy": 20_000.0,
    "entry": "信号日 T 最后一根 5m bar (15:00) 收盘买入",
    "entry_price_mode": "close_t",
    "priority": "移动止盈优先",          # 显示值 (报告抬头在用)
    "priority_value": "trailing_first",  # 机器值 (回填回测页单选框用)
    "combos": 36,
}

PASS = "pass"
FAIL = "fail"
THIN = "insufficient"
INVALID = "invalid"

_LABELS = {PASS: "达标", FAIL: "未达标", THIN: "样本不足", INVALID: "无效"}


def _num(v):
    """None/NaN/非数 → None (脏值一律当"没有", 不参与判定)。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _mk(code: str, reason: str) -> dict:
    return {"code": code, "label": _LABELS[code], "reason": reason}


def verdict(annret, maxdd, trades) -> dict:
    """单组合判定 → {"code", "label", "reason"} (纯函数, 无 I/O)。

    code: pass 达标 / fail 未达标 / insufficient 样本不足 / invalid 无效。
    顺序有意: **先判样本, 再判达标** —— 样本不足时数字再好看也不给"达标"。
    """
    ann, dd, n = _num(annret), _num(maxdd), _num(trades)
    if ann is None or dd is None or n is None or n <= 0:
        return _mk(INVALID, "无有效指标 (组合全部失败 / 零信号)")
    if n < MIN_TRADES:
        return _mk(THIN, f"笔数 {int(n)} < {MIN_TRADES}, 样本不足 (数字不作数)")
    if ann < TARGET_ANN:
        return _mk(FAIL, f"年化 {ann * 100:.2f}% < {TARGET_ANN * 100:.0f}%")
    if abs(dd) > TARGET_MAXDD:
        return _mk(FAIL, f"回撤 {dd * 100:.2f}% 超 {TARGET_MAXDD * 100:.0f}%")
    return _mk(PASS, f"年化 {ann * 100:.2f}% ≥ {TARGET_ANN * 100:.0f}% 且 "
                     f"回撤 {dd * 100:.2f}% 在 {TARGET_MAXDD * 100:.0f}% 内 "
                     f"({int(n)} 笔)")


def is_pass(row) -> bool:
    """组合行是否达标 (报告/脚本通用便利函数)。

    只要求 row 有 `.get` (dict 或 pandas Series 都行) —— 报告侧直接
    `df[df.apply(farm_rules.is_pass, axis=1)]` 过滤, 与 verdict 同一口径。
    """
    try:
        return verdict(row.get("annret"), row.get("maxdd"),
                       row.get("trades"))["code"] == PASS
    except Exception:                                            # noqa: BLE001
        return False


def pick_best(rows):
    """从组合行里挑「最优组合」: **只在笔数 ≥ MIN_TRADES 的行里选**。

    一行都不够样本时退回全量里的最高年化 (调用方据此判「样本不足」,
    而不是把噪声当结论)。空 → None。
    """
    rows = [r for r in (rows or []) if _num(r.get("annret")) is not None]
    if not rows:
        return None
    enough = [r for r in rows if (_num(r.get("trades")) or 0) >= MIN_TRADES]
    pool = enough or rows
    return max(pool, key=lambda r: _num(r.get("annret")))


def describe() -> str:
    """一行口径说明 (报告抬头 / 日志用)。"""
    return (f"达标线: 年化≥{TARGET_ANN * 100:.0f}% 且 |最大回撤|≤{TARGET_MAXDD * 100:.0f}% "
            f"且 笔数≥{MIN_TRADES}; 笔数<{MIN_TRADES} 记「样本不足」, "
            f"既不判达标也不参与最优评选")
