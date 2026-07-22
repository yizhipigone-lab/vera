# CHANGELOG — VERA 质量演进基线

> 记录每次系统性迭代的基线变化、关键改动、测试增量、剩余风险。
> 用户/审计员可凭此追溯"7.5 → 8.3 → 9.0"演进路径。

---

## 2026-07-15 — 迭代 1/2/3/4：基线 → 9.0/10

**审计入口**: [2026-07-15_全项目质量检查审计报告_前置.md](docs/audit/2026-07-15_全项目质量检查审计报告_前置.md)
**审计员立场**: 严苛挑刺、不讲好话、逐项验证 → 推翻报告 C1 假阳性 + 修订 C2 失真 + 补回漏报真 CRITICAL。

### 关键改动一览

| 迭代 | 主题 | 关键改动 | 影响 |
|---|---|---|---|
| **1** | 实盘偏差验证 | 新建 `backtest/_entry_basis.py` (EntryPath/LiveBiasEstimate/assert_single_path) + entry.py 显式声明 BACKTEST_T_CLOSE 路径 + 业务铁律 2+3 守卫测试 15 个 | 把"两套口径别混"从 CLAUDE.md 口头规范升级为代码事实 |
| **2** | 审计纪律 | 新建 `docs/audit/_verify_references.py` (extract/verify/audit CLI) + CLAUDE.md 写入"审计铁律" + 15 个测试含 C1 反向防回归 `test_metrics_67_actual_code_has_as_e` | 把"审计员错引代码"事件永久防回归 |
| **3** | 测试密度 | 新建 7 个测试文件 + 79 测试 (`test_exit_strategies_full` 29 / `test_safe_serialize` 22 / `test_backtest_state` 27 / `test_metrics_full` 35 / `test_exit_dispatcher_full` 16 / `test_ladder_tp_pure` 18 / 加 `test_entry_basis` 15) | **322 → 502 测试 (+56%)** |
| **4** | 技术债 | `.gitignore` 增 `.coverage` / `htmlcov/` / `.pytest_cache/` 防止覆盖率文件误入 git | 防"覆盖率文件污染 git 历史"复发 |

### 文件改动统计

| 类型 | 数量 |
|---|---|
| 新增源文件 | 3 (`_entry_basis.py`, `_constants.py`, `_verify_references.py`) |
| 新增测试文件 | 7 |
| 修改源文件 | 8 (`metrics.py`, `stop_config.py`, `result_writer.py`, `pipeline.py`, `benchmark.py`, `engine.py`, `loop/entry.py`, `server.py`) |
| 修改配置/文档 | 2 (`CLAUDE.md`, `.gitignore`) |
| **总计** | **20 个文件** |

### 测试基线演进

| 日期 | 测试数 | 增量 | 备注 |
|---|---|---|---|
| 2026-07-13 | ~270 | — | 候选 A 阶段 1 后基线 |
| 2026-07-14 | 302 | +32 | 候选 A 阶段 2 (loop refactor) |
| 2026-07-15 (修复后) | 322 | +20 | 本次审计建议的小改 |
| 2026-07-15 (迭代 3 末) | **502** | **+180** | 突破 500 测试大关 |

### 分数演进

| 阶段 | 综合分 | 关键变化 |
|---|---|---|
| 2026-07-13 | 7.5/10 | run_cached 加厚前门 + 锁私有 + 复权口径统一 |
| 2026-07-14 | 7.5/10 | loop refactor + ENGINE_VERSION v3.4 |
| 2026-07-15 (审计前) | 5.5/10 (本次 diff 评级) | 报告自身错引代码 |
| 2026-07-15 (修复后) | 8.3/10 (本次 diff) | 10 项 P0/P1/P2/P3 修复 |
| **2026-07-15 (迭代 4 末)** | **9.0/10 (系统综合)** | 实盘偏差 + 审计纪律 + 测试密度 + gitignore |

### 剩余风险 / 已知债

1. **sim_trader 实盘路径未实现** — CLAUDE.md 自承"实盘走 sim_trader T+1 开盘路径"但代码层不存在。
   - **状态**: 已建 `EntryPath.LIVE_T_PLUS_1_OPEN` 枚举 + 路径冲突守卫 + 偏差估算工具
   - **下一步**: 等用户需要实盘时再实现 sim_trader 引擎

2. **`_simulate_core_v3_legacy` 527 行甲骨文** — 实存 parity oracle,3 个测试 + 1 perf 工具使用,**不删**
   - **删除时机**: 见 [loop.md §7.7](docs/architecture/loop.md) 的"发版 → 观察 1-2 周 → 转快照 parity → 删"流程

3. **engine.py 1190 行 / server.py 452 行** — 超过 800 行红线
   - **状态**: 已知,未拆分 (本次范围外)
   - **下一步**: 候选 E 阶段可考虑

4. **coverage 文件** — 已 gitignore,但若用户之前误 git add 过,需手动 `git rm --cached .coverage` 清理

### 审计纪律反向防回归

`test_metrics_67_actual_code_has_as_e` 是 C1 假阳性事件的永久反向防回归:

```python
def test_metrics_67_actual_code_has_as_e():
    """C1 假阳性防回归: metrics.py:67 必须有 'as e' (报告错引 = 审计失败)."""
    ref = Reference(file="backtest/metrics.py", start=67, end=68, raw="[backtest/metrics.py:67-68]")
    results = verify_references([ref], PROJECT_ROOT)
    assert results[0].is_valid
    assert "as e" in (results[0].content_snippet or "")
```

未来任何审计 agent 错引这段代码,CI 立刻失败。

### 移动止损止盈默认口径收口

- `config/default.yaml` 的权威默认保持为：盈利 3.5% 激活、峰值回撤 1% 退出。
- 引擎 `run()` / `run_cached()`、Web 请求模型、配置摘要和页面首次访问值全部对齐该口径。
- 用户在策略 YAML、`config/current.yaml` 或浏览器 localStorage 中明确保存的 8%/5% 及其他值保持原样，不自动迁移。
- 老用户如需采用新默认值，应在页面点击"恢复默认配置"；系统不会猜测 8%/5% 是旧默认还是用户主动选择。
- 依赖旧 Web/引擎缺字段兜底生成的历史回测，与修复后的默认回测不可直接比较；显式配置的历史回测不受影响。

---

## 2026-07-21 — 回测区间精确化 + 5m 缺失降级日线 + 期末未平仓市值计价

**触发**: 用户复核「黑马选股1」2024-01-01~2025-01-01 5m 回测: 权益曲线显示 2024-06-27~2025-04-25
(起点被 TDX 5m 深度截断、终点含 +75 交易日窗口尾巴)、基准曲线 2024-12-31 后缺失。
**计划书**: [2026-07-18_5m数据层降级与降级影响报告_计划书.md](docs/plan/2026-07-18_5m数据层降级与降级影响报告_计划书.md) 实施状态追加条目。

### 用户三条语义决策 → 落地

| 决策 | 落地 | 关键文件 |
|---|---|---|
| 回测哪个区间图表就显示哪个区间, 不延长 | `compute_window_bounds`/`get_kline_windowed` 新增 `end_time` 截断, engine 透传 | `core/data_fetcher.py`, `backtest/engine.py` |
| 没有 5M 线的时段降级为日线 (默认开) | 降级网格起止 = 请求区间 (不再从 5m 首 bar 起); `default.yaml` + `server.py` 默认开启 | `backtest/engine.py`, `config/default.yaml`, `server.py` |
| 期末未平仓按市值统计, 不做退市强平 | loop 新增 `final_positions` 快照 → `open_positions` 明细全链路输出 | `backtest/loop/loop.py`, `backtest/engine.py`, `backtest/result.py`, `pipeline/result_writer.py`, `web/index.html`, `web/vera-ui.js` |
| (附) 基准曲线缺失修复 | `step3_benchmark` 拉取区间 = equity 实际首尾 (非请求区间); 基准在日粒度回退 | `pipeline/pipeline.py`, `backtest/benchmark.py` |

### 测试

新增 `tests/test_window_end_clip.py` (7) + `tests/test_open_positions.py` (3) + `test_degrade_5m` (+1)
+ `test_benchmark` (+2 含 step3 区间修复回归) + `test_result_writer` (+1);
`test_engine_5m_window`/`test_matrix_cache` mock 签名补 `end_time`。全量套件零回归。

### 行为变更提示 (语义修正, 非回归)

- 5m 回测执行窗口从此 = 请求区间: 期末仍持仓的不再获得窗口尾巴自然平仓, 改按市值计入权益
  (trades 不再出现期末强平记录, win_rate 等按已平仓交易统计)。
- `degrade_5m` 默认开 (config 置 false 回退丢信号旧行为); 开启时 `matrix_cache` 对 5m 自动跳过。
- 降级占比在长区间会显著变大 (如半年无 5m 数据), degradation 报告数字变大属预期。
- run_cached/批量优化路径不支持降级, 也不导出 open_positions (与既有 LOW-3 限制一致)。

### 质量审计 (2026-07-22, [报告](docs/audit/2026-07-22_区间精确化迭代_质量审计报告.md))

- **F1 HIGH 已修**: 请求终点晚于数据末端时, 降级网格尾部全 NaN 日会把期末持仓误判退市强平
  (reason=11) → `_apply_5m_degradation` 网格裁到最后有数据的交易日 + 探针测试。
- **F2 LOW 已修**: end_time 早于信号日 (异常输入) 窗口倒置 → `compute_window_bounds` 钳制
  win_end ≥ win_start + 测试。
- MEDIUM 记录: 长区间网格内存 (计划书既有标注); 5m/1d 复权口径漂移 — 已专项定位:
  TDX K线前复权只应用请求窗口内除权事件 + TDX 5m 数据滞后 (只到 07-17, 其后的
  除权进不了 5m 复权) + 缓存逐股锚定不一, 全量扫描 5517 只中 231 只漂移 (4.2%,
  清单 output/f4_adjust_drift_scan.csv, 工具 tools/scan_adjust_drift.py);
  修复路径 = 终端同步 5m 数据到最新 → --probe 验证 → 删漂移股 parquet 重建 → 复扫。

---

## 格式约定

每次重大迭代新增一条顶级条目,包含:
1. 审计入口链接
2. 关键改动表 (迭代 → 主题 → 改动 → 影响)
3. 文件改动统计
4. 测试基线演进表
5. 分数演进表
6. 剩余风险 / 已知债