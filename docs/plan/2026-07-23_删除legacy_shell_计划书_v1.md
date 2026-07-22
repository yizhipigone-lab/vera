# 删除 engine.py Legacy Shell — 计划书 v1

> 日期: 2026-07-23
> 目标: 删除 `_simulate_core_v3`(53行壳) + `_simulate_core_v3_legacy`(532行甲骨文)，统一到 `build_backtest_loop` + `BacktestLoop.run()`

---

## 0. 现状

| 组件 | 行号 | 行数 | 作用 |
|---|---|---|---|
| `_simulate_core_v3` | engine.py:68-121 | 53 | 兼容壳——import build_backtest_loop → 构造 → run |
| `_simulate_core_v3_legacy` | engine.py:123-655 | 532 | 甲骨文——527行原始实现，仅 parity 测试引用 |
| `build_backtest_loop` | loop/builder.py:26-113 | 87 | 工厂函数——39参数→BacktestLoop对象图 |
| `BacktestLoop.run` | loop/loop.py:63-150 | 87 | 核心循环——接收numpy矩阵，返回(equity, trades) |

**生产路径**（2 处调用）：
- `BacktestEngine.run()` → engine.py:1054 调 `_simulate_core_v3(...)`
- `BacktestEngine.run_cached()` → engine.py:1289 调 `_simulate_core_v3(...)`

**外部调用**（工具脚本）：
- `tools/bench_engine_speed.py` — `from backtest.engine import _simulate_core_v3, _simulate_core_v3_legacy`
- `tools/benchmark_perf.py` — 同上

**测试调用**（~15 文件）：
- 直接 import: test_loop_parity, test_priority_switch, test_end_to_end_formula_sell, test_dual_trigger, test_gap_and_delist_nan, test_loop_perf, test_prefilter, test_run_cached_parity, test_real_parity
- monkeypatch: test_degrade_5m, test_engine_5m_window, test_matrix_cache, test_window_end_clip
- 签名守卫: test_engine_run_path

**全局状态**：
- `_LAST_LOOP` (engine.py:45) — 壳写入，`run()` 路径读 `loop.final_positions`（期末未平仓）

---

## 1. 实施步骤（7 步）

### Step 1: 删除 `_simulate_core_v3_legacy`（532行甲骨文）

1. 删除 engine.py 行 123-655（整个 legacy 函数体）
2. 删除 `backtest/loop/__init__.py` 和 `backtest/loop/strategies/atr_stop.py` 中引用 legacy 的注释
3. 删除 `backtest/engine.py` 行 1-7 docstring 中引用 legacy 的描述

**验证**: `grep "_simulate_core_v3_legacy" --include="*.py" -rl` → 只返回 test_loop_parity.py 和 test_loop_perf.py（下一步处理）

### Step 2: 改写 parity 测试为纯回归测试

**test_loop_parity.py** (55 组对照):
1. 删除 `from backtest.engine import _simulate_core_v3, _simulate_core_v3_legacy`
2. 改为 `from backtest.engine import _simulate_core_v3` + `from backtest.loop import build_backtest_loop`
3. 删除 `_legacy_runner()` 函数（行 82-110）
4. 每个 parity test 改为：构造相同参数，跑 `BacktestLoop.run()`，断言 equity/trades 与 `_simulate_core_v3` 一致（即壳 vs 直接调 loop，都是同一实现）
5. 实际效果：因为壳和直接调 loop 走同一代码路径，测试变成"同一代码跑两次结果一致"——改为测试 BacktestLoop 的基本行为正确

**test_loop_perf.py**:
1. 删除 `from backtest.engine import _simulate_core_v3, _simulate_core_v3_legacy`
2. 改为 benchmark `BacktestLoop.run()` 的性能

**验证**: `pytest tests/test_loop_parity.py tests/test_loop_perf.py -q` → 全绿

### Step 3: 删除 `_simulate_core_v3` 壳——engine 内部重构

1. 在 `BacktestEngine.run()` (~1054) 和 `run_cached()` (~1289) 中：
   - 删除对 `_simulate_core_v3(...)` 的调用
   - 改为直接调 `build_backtest_loop(...)` + `loop.run(...)`
   - 保持 `_LAST_LOOP = loop` 赋值（期末未平仓需要）

2. 删除 `_simulate_core_v3` 函数定义（engine.py:68-121）

3. `_LAST_LOOP` 保留但移到 engine 类的 run() 方法内

**验证**: `pytest tests/ -q` → 核心测试全绿

### Step 4: 重写 `_simulate_core_v3` 的外部 import 为 `BacktestEngine`

**tools/bench_engine_speed.py**:
1. 改为 `from backtest.engine import BacktestEngine`
2. 用 `BacktestEngine.run()` 替代 `_simulate_core_v3(*args)`

**tools/benchmark_perf.py**: 同上

### Step 5: 重写 monkeypatch 测试

以下测试用 `monkeypatch.setattr(engine_module, '_simulate_core_v3', fake_core)`：
- `test_degrade_5m.py:369`
- `test_engine_5m_window.py:57`
- `test_matrix_cache.py:158`
- `test_window_end_clip.py:131`

改为 monkeypatch `build_backtest_loop` 或 `BacktestLoop.run`

### Step 6: 更新直接调用 `_simulate_core_v3` 的测试

以下测试直接 `from backtest.engine import _simulate_core_v3` 并调用：
- `test_priority_switch.py` → 改用 `build_backtest_loop`
- `test_end_to_end_formula_sell.py` → 改用 `build_backtest_loop`
- `test_dual_trigger.py` → 改用 `build_backtest_loop`
- `test_gap_and_delist_nan.py` → 改用 `build_backtest_loop`
- `test_run_cached_parity.py` → 改用 `BacktestEngine.run_cached`
- `test_real_parity.py` → 改用 `BacktestEngine.run`（真实数据对拍）
- `test_engine_run_path.py` → 删除签名守卫测试，改为 engine.run/run_cached 不崩溃测试

### Step 7: 删除 `_simulate_core_v3` 签名守卫测试

`test_engine_run_path.py` 的测试核心是锁死 `_simulate_core_v3` 的 22 位置参数签名。壳删除后这些测试无意义：
1. 删除签名守卫测试函数
2. 保留引擎调用测试（验证 run/run_cached 不崩溃）

---

## 2. 影响面汇总

| 类别 | 文件数 | 改动类型 |
|---|---|---|
| engine.py 内部 | 1 | 删除 585 行，重写 run/run_cached 调用 |
| loop/builder.py | 1 | 可能需要接收 `formula_exit_np` 等壳特有参数（当前已有） |
| 工具脚本 | 2 | import 改为 BacktestEngine |
| 测试 parity | 2 | 删除 legacy 引用，改为纯回归 |
| 测试 monkeypatch | 4 | target 从 `_simulate_core_v3` 改为 `build_backtest_loop` |
| 测试直接调用 | 7 | import 改为 `build_backtest_loop` |
| 测试签名守卫 | 1 | 删除/重写 |
| **合计** | **~17 文件** | |

---

## 3. 风险与缓解

| 风险 | 缓解 |
|---|---|
| `_LAST_LOOP` 全局变量丢失导致期末未平仓静默断掉 | Step 3 保留 `_LAST_LOOP` 赋值，Step 7 加测试 |
| monkeypatch target 改名后测试逻辑需调整 | 每个 monkeypatch 测试逐一手动验证 |
| bench_engine_speed/benchmark_perf 改接口后性能基准漂移 | 用 `BacktestEngine.run()` 替代（同一代码路径） |
| test_engine_run_path 签名守卫删除后签名被误改 | 新的回归测试锁死 `BacktestEngine.run()` 接口 |

---

## 4. 验收标准

- [ ] `_simulate_core_v3` 和 `_simulate_core_v3_legacy` 不再存在于代码库
- [ ] `BacktestEngine.run()` 和 `run_cached()` 直接调用 `build_backtest_loop`
- [ ] 所有 17 个受影响文件的 import 已更新
- [ ] `pytest tests/ -q` 全部通过（含更新后的测试）
- [ ] `_LAST_LOOP` 机制仍正常工作（期末未平仓导出不受影响）
- [ ] `grep "_simulate_core_v3" --include="*.py" -rl` 只返回注释/文档引用
