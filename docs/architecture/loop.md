# BacktestLoop 架构说明

> 候选 A 阶段 2 — 核心回测循环拆解。`_simulate_core_v3`（527 行/39 参数）拆成 `backtest/loop/` 子包。
> 实现日期：2026-07-14。ENGINE_VERSION：`v3.4-loop-refactor-20260714`。

## 1. 为什么拆

旧 `_simulate_core_v3` 一个函数 527 行，3 套优先级 if 链重复维护，11 种退出原因全内联在一个 `triggered` 变量里，测试只能从外头戳完整 numpy 矩阵。拆成 Strategy 模式后：每种止损可单独测、单独维护、单独扩展。

## 2. 目录结构

```
backtest/loop/
├── __init__.py          # 对外导出
├── state.py             # BacktestParams/Context/Position/PositionBook/TradeBuffer/Bar/TradeColumns
├── base.py              # ExitStrategy/AbsoluteStrategy Protocol + TriggerResult
├── exit_engine.py       # Priority 枚举 + ExitDispatcher（多结果模型）
├── absolute.py          # FormulaSellStrategy（reason=12, 绝对优先级）
├── entry.py             # EntryEngine（买入 + 换股 reason=1）
├── equity.py            # EquityTracker（权益曲线, 期末不平仓）
├── loop.py              # BacktestLoop.run() 主循环协调
├── builder.py           # 39 参数 → BacktestLoop 对象图（兼容壳 + parity 共用）
└── strategies/
    ├── cost_stop.py     # reason=3
    ├── ladder_tp.py     # reason=5（薄壳转调 backtest.ladder_tp）
    ├── trailing.py      # reason=4/8
    ├── time_stop.py     # reason=6/9
    ├── cond_time.py     # reason=7
    └── first_day.py     # reason=10
```

退市（reason=11）不是 ExitStrategy，归 `PositionBook` 的 pre-priority hook（`backtest/loop/loop.py` 内联处理，对齐 engine.py:99-129）。

## 3. 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| Dispatcher 返回类型 | `List[TriggerResult]`（多结果） | trailing_first 同 bar 双触发（ladder 部分卖 + trailing 全卖剩余, engine.py:346-384）单结果 `Optional` 编码不了 (CA1) |
| execution_price | 折进 TriggerResult | 检测与执行价分离会数据耦合 (CA4) |
| capability gating | 构造时过滤 | 禁用策略不进 dispatcher dict (HA1) |
| Position 可变性 | mutable | ladder_done bitmask + high_px/high_hi 跨 bar 累计；策略只读，loop 统一 mutate (HA4) |
| cash 归属 | BacktestLoop 持有 | EntryEngine/执行平仓通过参数传递 (HA3) |
| dtype | float64 价格 / int32 索引+bitmask | 对齐 engine.py:64-70 (M1) |

## 4. 评估顺序（每 bar 每持仓）

```
1. 退市驱逐（reason=11, pre-priority）  ← PositionBook
2. 停牌跳过
3. T+1 当日不卖（i//bpday == entry_idx//bpday）
4. formula_sell 绝对优先（reason=12）   ← AbsoluteStrategy, 先于 dispatcher
5. ExitDispatcher.evaluate() → List[TriggerResult]
   - stop_first / ladder_tp_first: first-trigger-wins
   - trailing_first: ladder 部分卖不阻塞, trailing/cost_stop 可追加第 2 触发
6. 公共尾部: time_stop → cond_time → first_day（triggered<0 才走）
7. 执行: 部分卖(keep) / 全卖(clear) / 双触发(先部分后全卖剩余)
```

## 5. parity 验证

`tests/test_loop_parity.py` 50 组对照：`_simulate_core_v3_legacy`（527 行甲骨文）vs `_simulate_core_v3`（兼容壳 → BacktestLoop），`np.array_equal` 字节级断言 equity_arr + raw_trades。覆盖 3 优先级 × 8 seed、4 capability 组合、双触发、formula_sell、退市/停牌、连续 ladder 部分卖、T+1 bpday=4、cond_time、first_day、无 high/low、滑点印花、max_position_pct、空信号、换股。

---

## 6. 加新策略范例 — ATR 波动率止损（60 分钟 checklist）

以 ATR（Average True Range，平均真实波幅）止损为例：持仓期间回撤超过 N 倍 ATR 即平仓。

### Step 1 — 实现 ExitStrategy（~20 行，<15 分钟）

新建 `backtest/loop/strategies/atr_stop.py`：

```python
"""ATR 波动率止损 (reason=13)。"""
from __future__ import annotations
from typing import List
from .base import ExitStrategy, TriggerResult
from ..state import Bar, Context, Position


class AtrStopStrategy:
    """回撤超过 N 倍 ATR 即全卖。reason=13（新原因码, 需同步 engine.py:89 注释）。"""

    name = "atr_stop"

    def __init__(self, atr_value: float, multiplier: float = 3.0):
        # atr_value: 当前持仓的 ATR 值（由 loop 预算传入 Context 或策略持有）
        # 这里简化: 策略持有固定 atr_value; 实际可改为从 ctx 读动态 ATR
        self.atr_value = float(atr_value)
        self.multiplier = float(multiplier)

    def check(self, pos: Position, bar: Bar, ctx: Context) -> List[TriggerResult]:
        ep = pos.entry_px
        if ep <= 0 or self.atr_value <= 0:
            return []
        # 回撤线 = peak_hi - multiplier * ATR; Low 触及即触发
        trail_line = ctx.peak_hi - self.multiplier * self.atr_value
        if bar.low <= trail_line:
            return [TriggerResult(
                reason=13, strategy_name=self.name, execution_price=trail_line,
            )]
        return []
```

### Step 2 — 注册到 dispatcher（<5 分钟）

在 `backtest/loop/builder.py` 的 `build_backtest_loop` 里，按 capability 过滤处加：

```python
if atr_enabled:  # 新增参数
    strategies["atr_stop"] = AtrStopStrategy(
        atr_value=atr_value, multiplier=atr_multiplier)
```

dispatcher 的 `stop_first`/`ladder_tp_first` 顺序表里加 `"atr_stop"`（按想要的优先级位置插入）。`trailing_first` 若要让 ATR 也参与双触发，在 `_eval_trailing_first` 里相应位置调用。

### Step 3 — 单测（~15 行，<15 分钟）

在 `tests/test_loop_strategies.py` 加：

```python
class TestAtrStop:
    def test_trigger_when_low_below_atr_line(self):
        s = AtrStopStrategy(atr_value=0.5, multiplier=3.0)  # 回撤线 = peak - 1.5
        pos = make_pos(entry_px=10.0)
        ctx = make_ctx(peak_hi=11.0)  # 回撤线 = 11 - 1.5 = 9.5
        bar = make_bar(low=9.4)  # <= 9.5 触发
        res = s.check(pos, bar, ctx)
        assert len(res) == 1
        assert res[0].reason == 13
        assert res[0].execution_price == pytest.approx(9.5)

    def test_no_trigger_when_low_above(self):
        s = AtrStopStrategy(atr_value=0.5, multiplier=3.0)
        ctx = make_ctx(peak_hi=11.0)
        assert s.check(make_pos(), make_bar(low=9.6), ctx) == []
```

### Step 4 — parity（<20 分钟）

在 `tests/test_loop_parity.py` 加一组：先在 `_simulate_core_v3_legacy` 里加 ATR 分支（或跳过 legacy 对比，仅对比新结构自洽），跑 `pytest tests/test_loop_strategies.py::TestAtrStop tests/test_loop_parity.py -v`。

### 验收 checklist

- [ ] `AtrStopStrategy` 实现 ExitStrategy Protocol（name + check）
- [ ] builder.py 注册（capability gating）
- [ ] dispatcher 顺序表含 atr_stop
- [ ] 单测 ≥2 个（触发 + 不触发）
- [ ] `pytest tests/test_loop_strategies.py::TestAtrStop` 绿
- [ ] 全量 `pytest tests/` 仍 258 passed + 5 skipped

按此模板，新人 60 分钟内可跑通一个新策略测试。
