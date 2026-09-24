# run_cached 前门收敛 专项计划书

> 成文日期 2026-08-16 · 文档类型 计划书 · 是 `docs/plan/2026-08-16_深模块接口债治理_计划书.md` 的 P2-1 专项展开
> 版本：最终版（第 2 轮审计后定稿）。本文所有 `[文件:行号]` 引用均经 `docs/audit/_verify_references.py` 机器校验。

## 修订记录

- **v1（初稿）**：调用方清单混入 StockSelector.run_cached、close_raw 错标标量，经第 1 轮审计纠正。
- **v2（第 1 轮审计后）**：调用方 22/14、close_raw 改标能力矩阵、阶段 4 补 4 个签名守卫测试、验收 grep 口径、禁 bump ENGINE_VERSION。
- **v3（本最终版，第 2 轮审计后）**：补第 5 个硬编码锚点（test_engine_run_path.py:306）、守卫测试的 required_kw/expected_defaults 删迁走关键字 + self 口径、清理 stale 注释、验收点名 docstring 残留、「前序审计 H」标签去循环引用。

## 一、目标与范围

**目标**：把 `BacktestEngine.run_cached` 的 18 参数前门（9 位置 + 9 关键字）收敛到 11 参数，消除三类接口债：

1. **位置顺序陷阱**：9 个位置参数顺序是隐性契约，传错不报错、只静默算错（证据：`[backtest/engine.py:710-715]` 的 tuple 错位兼容 hack 就是历史踩坑打的补丁）。
2. **配对不变量散落**：`tradable_np` 必须与 `last_tradable_idx` 成对（`[backtest/engine.py:735-737]`）、`ladder_profits` 要升序（`[backtest/engine.py:741-743]`）——靠调用方背，不靠类型约束。
3. **死参数**：`selections` 在函数体里从不被引用，但 22 个调用点都在傻传。

**范围（做）**：
- 新增 `PreparedMatrix` frozen dataclass，打包 7 个 K 线/能力矩阵。
- `run_cached` 新签名收敛到 5 位置 + 6 关键字 = 11 参数。
- 删死参数 `selections`。
- 过渡期旧壳 + 22 调用方迁移 + 快照 parity 兜底。

**范围（不做，本次）**：`ladder_profits/ladder_ratios/n_ladder` 收成 LadderSpec、`formula_exit_np/close_raw` 收成 CapabilitySpec——收益递减、易范围蔓延，留待以后。

---

## 二、精确现状（逐条核对过）

### 2.1 签名（`[backtest/engine.py:679-686]`）

```python
def run_cached(self, close, entries, high_np, low_np, stop_config, selections,
               ladder_profits, ladder_ratios, n_ladder, *,
               filter_limit_up=True,
               open_np=None,
               tradable_np=None, last_tradable_idx=None,
               formula_exit_np=None, formula_exit_ratio=None, formula_exit_lag_bars=1,
               close_raw=None,
               return_raw=False):
```

9 位置参数分类：
- **K 线矩阵 ×4**：`close`(DataFrame) / `entries`(DataFrame) / `high_np` / `low_np`
- **配置 ×1**：`stop_config`
- **死参数 ×1**：`selections`（函数体从不引用）
- **阶梯数组 ×2**：`ladder_profits` / `ladder_ratios`
- **标量 ×1**：`n_ladder`

9 关键字分类：
- **能力矩阵 ×3**：`open_np` / `tradable_np` / `last_tradable_idx`
- **能力矩阵 ×2**：`formula_exit_np`（公式卖出信号 bool 矩阵）/ `close_raw`（原始未 ffill 价的 DataFrame，提供时自建 tradable）
- **开关/标量 ×4**：`filter_limit_up` / `formula_exit_ratio` / `formula_exit_lag_bars` / `return_raw`

### 2.2 陷阱清单（函数体内，逐条）

| 陷阱 | 位置 | 说明 |
|---|---|---|
| tuple 错位兼容 hack | `[backtest/engine.py:710-715]` | `close` 若传成 `(close, entries)` tuple 则自动 unpack |
| capabilities 三开关 | `[backtest/engine.py:719-734]` | formula_exit/gap_protection/delisting 默认全开，gate 已提供数据；close_raw 提供时自建 tradable |
| 配对不变量 | `[backtest/engine.py:735-737]` | tradable_np 单传无 last_tradable_idx 时只 warning 不报错 |
| formula_exit_ratio 兜底 | `[backtest/engine.py:738-740]` | None 回退 stop_config.formula_sell.sell_ratio |
| ladder 升序 | `[backtest/engine.py:741-743]` | 不升序只 warning 不重排 |
| 涨停过滤 | `[backtest/engine.py:745]` | `_filter_limit_up(entries, close)` |
| 共享段调用 | `[backtest/engine.py:747-754]` | 把逐个矩阵传给 `_resolve_stop_and_build_loop` |

### 2.3 共享段签名（`[backtest/engine.py:301-307]`）

```python
def _resolve_stop_and_build_loop(self, stop, close, entry_np,
                                 high_np, low_np, open_np,
                                 tradable_np, last_tradable_idx,
                                 ladder_profits, ladder_ratios, n_ladder,
                                 formula_exit_np, formula_exit_ratio,
                                 formula_exit_lag_bars=1,
                                 degraded_np=None):
```

**关键结论（前序审计 H）**：共享段接收的是**逐个矩阵参数**，不是 dataclass。所以 run_cached 在边界把 `PreparedMatrix` unpack 成逐个矩阵再传给共享段，**共享段签名和 `run()` 路径完全不用动**。这是"可隔离"的改动，不是"牵一发动全身"。

### 2.4 调用方清单（grep 实测 22 个调用点 / 14 文件）

> 注：`pipeline/pipeline.py` 里的 `run_cached` 是 `StockSelector.run_cached`（选股缓存接缝，P1-3 引入），不是 `BacktestEngine.run_cached`，不在本清单。

| 组 | 文件 | 调用点数 |
|---|---|---|
| 生产 | `auto_iter/common.py` / `tools/gs_5m_sweep.py` / `tools/quantqq_1m_sweep.py` / `tools/quantqq_5m_sweep.py` / `tools/quantqq_5m_sweep_2010.py` | 5 |
| 研究 | `research/quantqq_sweep/{equity_curve_export,regime_filter_test,run_continuous}.py` | 3 |
| 测试 | `tests/test_atr_production.py`(4) / `test_engine_run_path.py`(2) / `test_gap_and_delist_nan.py`(2) / `test_run_cached_capabilities.py`(3) / `test_run_cached_parity.py`(1) / `test_snapshot_parity.py`(2) | 14 |

---

## 三、目标签名

### 3.1 `PreparedMatrix`（新增，放 `backtest/engine.py` 顶部或独立 `backtest/prepared.py`）

```python
@dataclass(frozen=True, slots=True)
class PreparedMatrix:
    """run_cached 预取矩阵束。7 个 K 线/能力矩阵打包, 消除位置顺序陷阱 +
    配对不变量 (tradable_np 与 last_tradable_idx 恒成对存在于同一对象)。"""
    close: pd.DataFrame
    entries: pd.DataFrame
    high_np: np.ndarray
    low_np: np.ndarray
    open_np: np.ndarray | None = None        # 跳空保护能力, None=off
    tradable_np: np.ndarray | None = None    # 退市检测, 与 last_tradable_idx 成对
    last_tradable_idx: np.ndarray | None = None

    def __post_init__(self):
        # 陷阱 2 从"靠人背"变"靠类型约束": 配对不变量在构造期 fail-fast,
        # 而非 runtime 只 warning 不报错 (engine.py:735-737 的旧行为)。
        if (self.tradable_np is None) != (self.last_tradable_idx is None):
            raise ValueError("tradable_np 与 last_tradable_idx 必须成对出现")
```

### 3.2 新签名（18 → 11）

```python
def run_cached(self, prepared: PreparedMatrix, stop_config,
               ladder_profits, ladder_ratios, n_ladder, *,
               filter_limit_up=True,
               formula_exit_np=None, formula_exit_ratio=None, formula_exit_lag_bars=1,
               close_raw=None,
               return_raw=False):
```

- 5 位置：`prepared` / `stop_config` / `ladder_profits` / `ladder_ratios` / `n_ladder`
- 6 关键字：`filter_limit_up` / `formula_exit_np` / `formula_exit_ratio` / `formula_exit_lag_bars` / `close_raw` / `return_raw`
- 删掉的：`selections`（死参数）、以及位置参数里的 `close/entries/high_np/low_np/open_np/tradable_np/last_tradable_idx`（7 个矩阵 → 1 个 `prepared`）

**为什么不再往下砍到 ~8**：`ladder_profits/ratios/n_ladder` 是阶梯配置（本就该跟 `stop_config` 相关、但历史上拆开了），`formula_exit_np` 是可选能力矩阵、`close_raw` 是只在边界自建 tradable 的原始价 DataFrame（不进共享段）——两者都是"可选能力"，与 7 个必传 K 线矩阵性质不同。把它们再打包收益递减、且 `formula_exit_ratio/lag_bars` 是标量跟矩阵绑一起不自然。11 参数已解决三大核心债，见好就收。

---

## 四、分 4 阶段实施

### 阶段 1：新增 `PreparedMatrix` + 新方法 `run_cached_prepared`

1. 定义 `PreparedMatrix`（frozen dataclass，字段见 §3.1）。
2. 新增 `run_cached_prepared(prepared, stop_config, ladder_profits, ladder_ratios, n_ladder, *, ...)`：把现有 `run_cached` 函数体**原样搬入**，只改三处：
   - 入参从 7 矩阵改为 `prepared`，函数体开头 unpack `close, entries, high_np, low_np, open_np, tradable_np, last_tradable_idx = prepared.close, prepared.entries, ...`；
   - 删掉 `selections` 相关（本就无引用）；
   - tuple hack（`[backtest/engine.py:710-715]`）从 `run_cached_prepared` 移出（它只对旧壳有意义，见阶段 2）。
3. 旧的 `run_cached` 变成**旧壳**（阶段 2）。

**验收**：`run_cached_prepared` 独立可跑，直调它与直调旧 `run_cached` 在同一数据上结果一致（临时对拍脚本验证后删）。

### 阶段 2：旧壳 `run_cached` 保持兼容

1. 旧 `run_cached(close, entries, high_np, low_np, stop_config, selections, ladder_profits, ladder_ratios, n_ladder, *, ...)` 保留为**薄壳**：
   - 保留 tuple 错位 hack（`[backtest/engine.py:710-715]`）；
   - 内部构造 `PreparedMatrix(close, entries, high_np, low_np, open_np, tradable_np, last_tradable_idx)`；
   - 调 `run_cached_prepared(prepared, stop_config, ladder_profits, ladder_ratios, n_ladder, filter_limit_up=..., formula_exit_np=..., ...)`；
   - `selections` 参数仍接收但忽略（保持旧签名兼容）。
2. 旧壳 docstring 标注 `@deprecated`（指向新签名）。

**验收**：旧壳与新方法字节级一致（同数据同 stop_config 产出 identical），现有 22 调用方**零改动**仍能跑——这是过渡期的安全网。

### 阶段 3：迁移 22 调用方

按 §2.4 清单，把每个调用点从「9 位置 + 9 关键字」改成「构造 PreparedMatrix + 调新签名」。**建议顺序**：先 `research/quantqq_sweep/` 的 3 个脚本（结构最简单、都是固定套路空 ladder 数组，改完立刻用 sweep 冒烟验证 = 免费真实环境对拍）→ 再测试（6 文件 14 点）→ 最后生产（5 文件，改完跑对应 sweep 冒烟）。

每处迁移 = 把 `close, entries, high_np, low_np, ...` 包进 `PreparedMatrix(...)`，其余关键字原样搬。

**验收**：`git grep "run_cached("` 里旧壳调用点清零——注意 `StockSelector.run_cached`（`[selection/selector.py]`）是选股缓存、恒在，grep 时要排除它，只看 `BacktestEngine.run_cached` 的旧签名调用点。

### 阶段 4：删壳 + 正名

1. 确认旧壳调用点清零后，删旧 `run_cached` 壳。
2. `run_cached_prepared` 改名回 `run_cached`（新签名）。
3. 更新 22 调用方的 `run_cached_prepared(` → `run_cached(`。
4. 删 `selections` 死参数在测试里的所有引用。
5. **同步改写 `tests/test_engine_run_path.py` 的 5 个签名守卫测试**（这是删壳正名的硬依赖，漏了必炸）：
   - `test_run_cached_positional_params_unchanged`（`[tests/test_engine_run_path.py:353-367]`）：断言"位置参数 ≤10"（含 self 是 11）要改成新签名 5 位置参数（含 self 是 6）。
   - `test_run_cached_capability_kwargs_keyword_only`（`[tests/test_engine_run_path.py:369-385]`）：锁 keyword-only 能力参数；`open_np/tradable_np/last_tradable_idx` 已收进 `PreparedMatrix`，要从该测试的 `required_kw` 里删掉。
   - `test_run_cached_capability_kwargs_default_values`（`[tests/test_engine_run_path.py:387-406]`）：锁默认值；`open_np/tradable_np/last_tradable_idx` 也要从 `expected_defaults` 里删掉。
   - `test_run_cached_forwards_capability_keywords`（`[tests/test_engine_run_path.py:408-433]`）：**内含硬编码锚点 `src.find('def run_cached(self, close')`（line 419）**——删壳正名后 `find` 返回 -1 必炸，必须同步改成 `'def run_cached(self, prepared'`。
   - **另有第二处同类锚点**：`test_both_entries_call_shared_segment`（`[tests/test_engine_run_path.py:302-318]`，line 306 的 `src.find('def run_cached(self, close')`）——同样必炸，一并改成新锚点。
6. **清理 stale 注释**：`tests/test_engine_run_path.py:347-350`（还写「10 位置 + 9 keyword / 40 调用方」）和 `auto_iter/common.py:504`（「run_cached 不读 selections」）——迁移完它们已不成立，一并更新，否则留下误导注释。

**验收**：`grep "run_cached_prepared"` 清零；`inspect.signature(run_cached)` 位置参数 = `(prepared, stop_config, ladder_profits, ladder_ratios, n_ladder)` 5 个；`selections` 全仓无引用；5 个签名守卫测试已改写并全绿；`test_engine_run_path.py:2` docstring 里的 `engine.run_cached()` 字样一并清理（否则 grep 恒命中）。

---

## 五、风险与兜底

| 风险 | 等级 | 兜底 |
|---|---|---|
| 迁移漏改某调用方 → 旧壳兜底期仍在，不会静默错（阶段 2 未删壳前） | 中 | 阶段 3 末尾 grep 清零才进阶段 4 |
| 新方法 unpack 漏字段 → 行为漂移 | 高 | `tests/test_snapshot_parity.py` 字节级 parity + 阶段 1 临时对拍 |
| 删壳后有个别调用方仍走旧签名 | 高 | 阶段 4 前 grep 清零 + 全量测试 |
| `run()` 路径被连带改 | 中 | 本方案不动 `_resolve_stop_and_build_loop` 与 `run()`，边界 unpack 即可隔离 |
| 快照 parity 测试本身也用了 run_cached | 中 | parity 测试在阶段 3 一并迁移，且它对比的是 loop 直调 vs run_cached，迁移后仍对比同一逻辑 |

**核心兜底**：`tests/test_snapshot_parity.py`（`run_cached` vs 核心循环直调字节级一致）+ `tests/test_engine_run_path.py`（签名契约守卫，需同步更新以锁新签名）。

---

## 六、验收标准

1. `run_cached` 新签名 5 位置 + 6 关键字，`selections` 死参数全仓无引用。
2. 旧壳已删，`run_cached_prepared` 已正名回 `run_cached`。
3. 22 个 `BacktestEngine.run_cached` 调用点全部迁移（`StockSelector.run_cached` 是选股缓存，不算在内）。
4. `tests/test_snapshot_parity.py` / `test_engine_run_path.py` / `test_run_cached_parity.py` 全绿，且 4 个签名守卫测试已改写锁新签名。
5. 全量测试 0 失败。
6. 所有 `[文件:行号]` 引用机器校验通过。
7. **禁止 bump `ENGINE_VERSION`**：本次是签名收敛、不改回测数值语义，bump 会触发引擎版本漂移告警、让已有缓存误判失效，属无关副作用。

---

## 七、工作量估算

| 阶段 | 估计 |
|---|---|
| 阶段 1（dataclass + 新方法） | 0.5 天 |
| 阶段 2（旧壳） | 0.5 天 |
| 阶段 3（迁移 22 调用方） | 1~1.5 天 |
| 阶段 4（删壳正名 + 全量验证） | 0.5 天 |
| 合计 | 2.5~3 天 |
