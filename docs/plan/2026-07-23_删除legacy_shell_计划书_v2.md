# 删除 engine.py Legacy Shell — 计划书 v2（自迭代第 1 轮）

> v1→v2 改进: 步骤重排序(先改调用方再删定义)、补充完整 30 文件清单、具体的代码改写方案

---

## 0. 精确影响面（来自 Grep 全量扫描）

| 类别 | 文件数 | 文件列表 |
|---|---|---|
| **生产定义+调用** | 1 | `backtest/engine.py` |
| **生产注释** | 10 | `loop/__init__`, `loop/builder`, `loop/loop`, `loop/entry`, `loop/equity`, `loop/exit_engine`, `loop/state`, `loop/strategies/atr_stop`, `ladder_tp`, `formula_exit` |
| **工具 import+调用** | 2 | `tools/bench_engine_speed.py`, `tools/benchmark_perf.py` |
| **工具 monkeypatch** | 1 | `tools/real_parity_check.py` |
| **工具字符串** | 1 | `tools/officecli_make_docx.py`(只改文档文本) |
| **测试 monkeypatch** | 5 | `test_degrade_5m`, `test_engine_5m_window`, `test_matrix_cache`, `test_window_end_clip`, `test_real_parity` |
| **测试直接调用** | 8 | `test_dual_trigger`, `test_gap_and_delist_nan`, `test_end_to_end_formula_sell`, `test_priority_switch`, `test_run_cached_parity`, `test_loop_parity`, `test_loop_perf`, `test_prefilter` |
| **测试签名守卫** | 1 | `test_engine_run_path` |
| **测试注释** | 1 | `test_end_to_end_real_pipeline` |
| **合计** | **30** | 10 只改注释, 20 需代码改动 |

---

## 1. 实施步骤（重新排序：先改调用方，最后删定义）

### Step 1: 改工具脚本（2 个 import + 1 个 monkeypatch）

**bench_engine_speed.py**:
```python
# 原: from backtest.engine import _simulate_core_v3, _simulate_core_v3_legacy
# 改: from backtest.loop import build_backtest_loop
# 原: t_new = bench(_simulate_core_v3, args)
# 改: loop = build_backtest_loop(...); t_new = bench(lambda *a: loop.run(...), args)
```
实际做法：bench_engine_speed.py 本身只做 benchmark，直接删除文件(其功能已被 test_loop_perf 取代)

**benchmark_perf.py**: 同上——删除文件

**real_parity_check.py**: 删除文件(功能已被 parity 测试取代，且 legacy 删除后无对拍目标)

**验证**: 3 个工具文件删除/改写后，`grep "import.*_simulate_core_v3" tools/` → 仅剩 officecli_make_docx.py(字符串)

### Step 2: 改 monkeypatch 测试（5 个文件）

统一模式：`monkeypatch.setattr(engine_module, '_simulate_core_v3', fake_core)` → `monkeypatch.setattr(engine_module, 'BacktestLoop', ...)` 或改为 monkeypatch `build_backtest_loop`

- **test_degrade_5m.py:369**: fake_core 捕获壳入参做断言 → 改为 monkeypatch `BacktestLoop.run` 或 `build_backtest_loop`，捕获入参
- **test_engine_5m_window.py:57**: 同上
- **test_matrix_cache.py:158**: 同上 
- **test_window_end_clip.py:131**: 返回 stub → 改为 monkeypatch `build_backtest_loop`
- **test_real_parity.py:101-106**: 删除整个测试(legacy 已不存在，无对拍对象)

### Step 3: 改直接调用测试（8 个文件）

统一模式：`_simulate_core_v3(**args)` → `build_backtest_loop(...)` + `loop.run(...)`

每个测试文件的具体改写：
- **test_dual_trigger.py**: 4 处 `_simulate_core_v3(**args)` → 用通用 helper 包装
- **test_gap_and_delist_nan.py**: 1 处 → 同上
- **test_end_to_end_formula_sell.py**: 7 处 → 同上
- **test_priority_switch.py**: 17 处，调用量最大 → 提取 `_make_loop_and_run(**args)` helper
- **test_run_cached_parity.py**: 改为直接测 `BacktestEngine.run_cached()`
- **test_loop_parity.py**: 删除 legacy runner，删除 parity 对比，改为 BacktestLoop 行为测试
- **test_loop_perf.py**: 删除 legacy runner parametrize，只 benchmark `build_backtest_loop` + `loop.run()`
- **test_prefilter.py**: 2 处 → 同上

### Step 4: 改签名守卫测试

**test_engine_run_path.py**: 
- 删除 `_simulate_core_v3` 签名守卫（22 位置参数约束）
- 删除源码扫描正则检查
- 改为：验证 `BacktestEngine.run()` 和 `run_cached()` 不崩溃（合成数据）

### Step 5: 重构 engine.py 内部

```python
# 原 (run() 内):
equity_arr, raw_trades = _simulate_core_v3(
    close.values.astype(np.float64), entries.values,
    float(self.initial_capital), float(self.eff_commission),
    float(self.min_buy_amount), float(self.max_buy_amount),
    int(self.lot_size), int(self.min_lots),
    cost.get("enabled", True), float(cost.get("threshold", -0.12)),
    ...  # ~30 个位置参数
    trailing_gap_protection=trailing_gap_protection,
)

# 改:
from backtest.loop import build_backtest_loop
loop = build_backtest_loop(
    float(self.initial_capital), float(self.eff_commission),
    float(self.min_buy_amount), float(self.max_buy_amount),
    int(self.lot_size), int(self.min_lots),
    cost.get("enabled", True), float(cost.get("threshold", -0.12)),
    ...
    trailing_gap_protection=trailing_gap_protection,
)
self._last_loop = loop  # 替代全局 _LAST_LOOP
equity_arr, raw_trades = loop.run(
    close.values.astype(np.float64), entries.values,
    high_np, low_np, open_np,
    tradable_np, last_tradable_idx, formula_exit_np,
)
```

**关键改动**:
1. `_LAST_LOOP` 全局变量 → `self._last_loop` 实例属性
2. `run()` 方法内 import `build_backtest_loop`（或顶部 import）
3. 删除 `_simulate_core_v3` 函数定义（行 68-121）
4. `run()` 行 1088 读 `_LAST_LOOP.final_positions` → `self._last_loop.final_positions`

### Step 6: 删除 `_simulate_core_v3_legacy`

删除 engine.py 行 123-655(532行)

### Step 7: 清理注释（10 个生产文件 + 1 个测试文件 + 1 个工具文件）

将注释中的 `_simulate_core_v3` 引用改为 `BacktestLoop` 或直接删除过时描述：
- `loop/__init__.py:3`: "把 _simulate_core_v3 (527 行/39 参数) 拆成..." → "回测核心循环子包"
- `loop/builder.py:1,49,51`: "把 _simulate_core_v3 的 39 参数映射成..." → "构造 BacktestLoop 对象图"
- 其他类似——只改注释，不动代码

---

## 2. 影响面汇总（精确版）

| 步骤 | 文件数 | 改动类型 | 风险 |
|---|---|---|---|
| Step 1 | 3 | 删除工具文件 | 低（benchmark 工具非生产路径） |
| Step 2 | 5 | monkeypatch target 改名 | 中（需逐一验证） |
| Step 3 | 8 | import+调用改写 | 中（test_priority_switch 17 处最多） |
| Step 4 | 1 | 删除签名守卫+重写 | 低 |
| Step 5 | 1 | engine 内部重构 | **高**（核心生产路径） |
| Step 6 | 1 | 删除 legacy 定义 | 低 |
| Step 7 | 12 | 注释更新 | 极低 |
| **合计** | **~31** | | |

---

## 3. 风险缓解（v2 强化）

| 风险 | v2 缓解 |
|---|---|
| **Step 5 改坏 run()/run_cached()** | 先跑 `pytest tests/test_engine_run_path.py tests/test_run_cached_parity.py -q` 建立基线；改后立即重跑 |
| **_LAST_LOOP → self._last_loop 迁移漏** | grep 全量搜索 `_LAST_LOOP`，确保只有 2 处引用（写+读） |
| **test_priority_switch 17 处调用改写遗漏** | 改完后 grep `_simulate_core_v3` 确认 0 残留 |
| **monkeypatch 测试改后语义漂移** | 逐文件 diff 确认 fake 入参 shape 不变 |

---

## 4. 验收标准

- [ ] `grep "_simulate_core_v3" --include="*.py" -rl` → 仅返回注释/文档引用（0 import/调用）
- [ ] `pytest tests/ -q` 全部通过
- [ ] `BacktestEngine.run()` 和 `run_cached()` 直接构造 `BacktestLoop`
- [ ] `_LAST_LOOP` 全局变量已移除
- [ ] 期末未平仓导出功能正常（`open_positions` in BacktestResult）
