# 删除 engine.py Legacy Shell — 计划书 v3（最终版）

> v1→v2: 步骤重排序 + 30 文件全量清单
> v2→v3: _LAST_LOOP 精确处理 + parity 测试转化策略 + 验证检查点

---

## 0. 精确影响面

| 类别 | 数量 | 改动 |
|---|---|---|
| engine.py 内部 | 1 | 删 ~585 行，重构 run/run_cached |
| 工具删除 | 3 | bench_engine_speed, benchmark_perf, real_parity_check |
| 工具字符串 | 1 | officecli_make_docx.py（只改文本） |
| monkeypatch 测试 | 5 | target 改名 |
| 直接调用测试 | 8 | import 改为 build_backtest_loop |
| 签名守卫测试 | 1 | 重写 |
| 注释 | 11 | 更新 |
| **需代码改动** | **20** | |

---

## 1. 实施步骤（7 步，每步有验证检查点）

### Step 1: 删工具脚本 + 改注释（风险：极低）
- 删除 `tools/bench_engine_speed.py`, `tools/benchmark_perf.py`, `tools/real_parity_check.py`
- `tools/officecli_make_docx.py`: 只改字符串，不改逻辑
- **验证**: `python -c "import tools.bench_engine_speed"` → ImportError ✅

### Step 2: 改 monkeypatch 测试（5 个文件）
- `_simulate_core_v3` monkeypatch → `build_backtest_loop` monkeypatch
- test_real_parity.py 整体删除
- **验证**: `pytest tests/test_degrade_5m.py tests/test_engine_5m_window.py tests/test_matrix_cache.py tests/test_window_end_clip.py -q` → 全绿

### Step 3: 改直接调用测试（8 个文件）
- `_simulate_core_v3(**args)` → 提取 `_run_loop(**args)` helper = `build_backtest_loop(...).run(...)`
- test_loop_parity.py: 删 legacy runner，parity 对比改为 "两次 build+run 结果一致"（验证确定性）
- test_loop_perf.py: 删 legacy parametrize
- test_run_cached_parity.py: 改为直接测 run_cached
- test_engine_run_path.py: 删签名守卫，改测 engine.run()/run_cached() 不崩溃
- **验证**: 上述 8 个测试文件逐个 `pytest -q` → 全绿

### Step 4: engine.py 内部重构（风险：高，最关键）

**4a. `_LAST_LOOP` 全局 → 实例属性**:
```python
# engine.py 类内 run() 方法:
loop = build_backtest_loop(...)
self._last_loop = loop           # 替代原 engine._LAST_LOOP
equity_arr, raw_trades = loop.run(...)
# 后续读 self._last_loop.final_positions 而非 _LAST_LOOP.final_positions
```

**4b. run() 和 run_cached() 的调用改写**:
```python
# 原: equity_arr, raw_trades = _simulate_core_v3(
#     close.values.astype(np.float64), entries.values,
#     float(self.initial_capital), ...
# )
# 改:
loop = build_backtest_loop(
    float(self.initial_capital), float(self.eff_commission),
    float(self.min_buy_amount), float(self.max_buy_amount),
    int(self.lot_size), int(self.min_lots),
    cost.get("enabled", True), float(cost.get("threshold", -0.12)),
    trail.get("enabled", True), float(trailing_activation), float(trailing_drawdown),
    ladder.get("enabled", True), ladder_profits, ladder_ratios, len(lv),
    time_s.get("enabled", True), mhd_scaled,
    cond_t.get("enabled", False), ctd_scaled, float(cond_t.get("profit", 0.01)),
    first_day_enabled, fd_target,
    bpday=bpday, slippage=float(self.eff_slippage), stamp_tax=float(self.eff_stamp_tax),
    max_position_pct=float(self.max_position_pct),
    ladder_tp_first=(priority == "ladder_tp_first"),
    trailing_first=(priority == "trailing_first"),
    formula_exit_np=formula_exit_np, formula_exit_ratio=formula_exit_ratio, formula_exit_lag_bars=formula_exit_lag_bars,
    atr_enabled=atr_enabled, atr_matrix=atr_matrix, atr_multiplier=atr_multiplier,
    trailing_gap_protection=trailing_gap_protection,
)
self._last_loop = loop
equity_arr, raw_trades = loop.run(
    close.values.astype(np.float64), entries.values,
    high_np, low_np, open_np,
    tradable_np, last_tradable_idx, formula_exit_np,
)
```

**4c. 删除 `_simulate_core_v3` 函数定义**（行 68-121）

**验证**: `pytest tests/test_engine_run_path.py tests/test_run_cached_parity.py -q` → 全绿

### Step 5: 删除 `_simulate_core_v3_legacy`（行 123-655，532 行）
- **验证**: `grep "_simulate_core_v3_legacy" --include="*.py" -rl` → 0 results

### Step 6: 更新 10 个注释文件
- 将注释中 `_simulate_core_v3` 改为 `BacktestLoop`，删除过时描述
- **验证**: 人工抽查 3 个关键文件

### Step 7: 全量回归
- `pytest tests/ -q` → 全部通过
- `grep "_simulate_core_v3" --include="*.py" -rl` → 仅注释引用(无 import/调用)
- 手动跑一次 `python main.py config/strategy_QUANTQQ.yaml` 验证端到端

---

## 2. 验收标准
- [ ] `_simulate_core_v3` 和 `_simulate_core_v3_legacy` 定义已删除
- [ ] engine 全局 `_LAST_LOOP` 已改为 `self._last_loop`
- [ ] `pytest tests/ -q` 全部通过
- [ ] `main.py` CLI 端到端正常
- [ ] 期末未平仓导出功能正常
