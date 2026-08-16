# data_fetcher 去上帝化 计划书

> 成文日期 2026-08-16 · 文档类型 计划书 · 是「深模块接口债治理」的后续专项
> 版本：最终版（第 2 轮审计后定稿）。本文所有 `[文件:行号]` 引用均经 `docs/audit/_verify_references.py` 机器校验。

## 修订记录

- **v1（初稿）**：两处实质设计漏洞，经第 1 轮审计纠正。
- **v2（第 1 轮审计后）**：① 窗口数学拆分改用 `calendar_fetcher` 回调（解决薄壳结构空洞）；② 「合并日历」撤回（审计证明会吞 `get_trading_dates` 异常、破坏 server.py 兜底），改为「加注释区分」。
- **v3（本最终版，第 2 轮审计后）**：`calendar_fetcher` 契约补「必须返回已升序去重 Timestamp」（bisect 依赖有序）；依赖清单去掉 numpy、补 typing；re-export 补模块级 import（防 NameError）；monkeypatch 清单补 4 个测试文件；跨文档「合并日历」残留同步。

---

## 一、目标与范围

**目标**：把 `core/data_fetcher.py`（594 行，13 个公开方法，5 个职责）里的**窗口数学纯逻辑**抽成独立模块 `core/window.py`，让 DataFetcher 收敛回「TDX 取数门面」。

**核心判断（沿用评估结论）**：data_fetcher 是「TDX 大杂烩接口的镜像」，**按数据域拆多个类 = 只加转发层，是错误方向**。唯一值得做的减法是把「窗口边界计算」这类纯逻辑挪出去——它本就不该跟 TDX 取数混在一起。

**范围（做）**：
1. `merge_window_masks`（原 `_merge_window_masks`）+ `compute_window_bounds` 移到 `core/window.py`。

**范围（不做）**：
- 「合并日历」——第 1 轮审计证明 `get_trading_dates`（raw，异常上抛）与 `get_trading_days`（robust，异常返空）语义不同、不可无脑合并；只给两个方法补 docstring 区分（见阶段 3）。
- 板块/名称映射（已委托 `DataCache`）、`get_kline`/`get_kline_single`/`get_stock_universe`（TDX 门面本体）。

---

## 二、精确现状

### 2.1 方法职责分布（13 公开方法）

| 职责 | 方法 | 行号 |
|---|---|---|
| K线取数 | `get_kline` / `get_kline_single` / `get_kline_windowed` / `get_index_data` | `[core/data_fetcher.py:75]` / `[core/data_fetcher.py:414]` / `[core/data_fetcher.py:278]` / `[core/data_fetcher.py:453]` |
| **窗口数学（要挪走）** | `compute_window_bounds`（现含 TDX 传递依赖，见下）+ 模块级 `_merge_window_masks` | `[core/data_fetcher.py:216]` / `[core/data_fetcher.py:22]` |
| 交易日历 | `get_trading_days`（Timestamp，robust）/ `get_trading_dates`（str，raw） | `[core/data_fetcher.py:189]` / `[core/data_fetcher.py:582]` |
| 股票池 | `get_stock_universe` | `[core/data_fetcher.py:468]` |
| 板块（已委托 DataCache） | `get_sector_list` / `get_sector_stocks` / `clear_sector_cache` | `[core/data_fetcher.py:486]` / `[core/data_fetcher.py:505]` / `[core/data_fetcher.py:525]` |
| 名称映射（已委托 DataCache） | `get_name_map` / `clear_name_cache` | `[core/data_fetcher.py:544]` / `[core/data_fetcher.py:577]` |

### 2.2 `compute_window_bounds` 现含 TDX 传递依赖（关键事实）

`compute_window_bounds` 现在是 classmethod，当 `trading_days is None` 时在 `[core/data_fetcher.py:245-255]` 里：
1. 用 `first_sig.min()/last_sig.max()`（`[core/data_fetcher.py:242-243]` 的 groupby 产物）算 `global_start/global_end/cal_end`；
2. 调 `cls.get_trading_days(...)` 拉日历（`[core/data_fetcher.py:250-252]`）。

而 `get_trading_days` 内部走 `cls._ensure_ready()` + `cls._connector().tq()`（`[core/data_fetcher.py:200-201]`）——**即 `compute_window_bounds` 经由 `get_trading_days` 间接依赖 TDX**，不是直接调，但确实是传递依赖。挪出时不能用「trading_days 必传」的简单切法（见 §3.1 的 calendar_fetcher 方案）。

### 2.3 调用方（grep 实测，已修正计数）

- `compute_window_bounds` 生产调用方 2 处：`get_kline_windowed`（`[core/data_fetcher.py:314]`）、engine 降级（`[backtest/engine.py:950]`）。测试 `tests/test_window_end_clip.py`。
- `_merge_window_masks` 生产调用方 1 处：`get_kline_windowed`（`[core/data_fetcher.py:398]`）。测试 `tests/test_merge_window_masks.py`。
- `get_trading_days` 调用方：`selection/signal_day_cache.py`（`[selection/signal_day_cache.py:160]`、`[selection/signal_day_cache.py:186]`）2 处、`backtest/engine.py`（`[backtest/engine.py:898]`）1 处。另有 monkeypatch 点（`tools/quantqq_5m_sweep_2010.py:195`、`tools/calibrate_degrade_5m.py:53`、`tests/test_degrade_5m.py:343`、`tests/test_open_positions.py:154`、`tests/test_kline_cache.py:388`、`tests/test_signal_day_cache.py:33`）——非调用方，本计划不改 `get_trading_days`，不受影响。
- `get_trading_dates` 调用方：`server.py` + `research/pond_optimizer.py`（2 处）+ `tools/{batch_formula_rank,backfill_kline_cache,import_lc5_to_cache,repaint_check,random_entry_control,pond_axis_experiment,future_func_check}.py` + 内部 `[core/data_fetcher.py:176]`。其中 `[tools/backfill_kline_cache.py:47]`、`[tools/import_lc5_to_cache.py:160]`、`[core/data_fetcher.py:176]` 是位置传参 `get_trading_dates("SH", "20100101", "20991231")`。

---

## 三、目标设计

### 3.1 新模块 `core/window.py`（窗口数学，无 TDX 依赖，用回调注入日历）

```python
# core/window.py
# 依赖: pandas / bisect / typing(List, Optional) / utils.code_normalizer.normalize_list / utils.logger
# (两函数都不用 numpy; 类型标注需 from typing import List, Optional)

def merge_window_masks(mask_frames: List[pd.DataFrame]) -> pd.DataFrame:
    """原 core/data_fetcher._merge_window_masks, 逻辑一字不动, 改名公开。"""

def compute_window_bounds(selections, window_trading_days,
                          trading_days: Optional[List[pd.Timestamp]] = None,
                          end_time: Optional[str] = None, *,
                          calendar_fetcher=None) -> tuple:
    """窗口边界纯函数。原 DataFetcher.compute_window_bounds 全文搬入,
    唯一改动: 拉日历的 TDX 调用换成注入的 calendar_fetcher 回调。

    calendar_fetcher: (start_str, end_str) -> List[pd.Timestamp], 契约:
    **必须返回已升序去重的 Timestamp 列表** (内部 _window_end 用 bisect.bisect_left
    依赖有序, 乱序会静默算错窗口终点)。trading_days is None 时: 用 groupby 算出
    global_start/global_end/cal_end, 若 calendar_fetcher 非 None 则调它拉日历
    (返回空/None → 退化自然日估算), 否则直接退化自然日估算。None 默认 →
    纯函数可独立测 (不触 TDX)。薄壳传的 cls.get_trading_days 本身已排序去重,
    满足契约, 零漂移。
    """
```

**为什么用 `calendar_fetcher` 回调而非「trading_days 必传」**：拉日历那步（`[core/data_fetcher.py:245-255]`）依赖 groupby 产物 `first_sig.min()/last_sig.max()`，若把 groupby 放进纯函数、却要求 `trading_days` 必传，薄壳就拿不到 `global_end` 去算 `cal_end`——结构上自相矛盾。用回调把「拉日历」留在纯函数体内、把「怎么拉」参数化，才能做到「逻辑一字不动搬 + 无 TDX 依赖」两全。

### 3.2 `DataFetcher` 变成薄委托

```python
# core/data_fetcher.py
@classmethod
def compute_window_bounds(cls, selections, window_trading_days,
                          trading_days=None, end_time=None):
    from . import window
    return window.compute_window_bounds(
        selections, window_trading_days, trading_days, end_time,
        calendar_fetcher=cls.get_trading_days)
```

- `get_kline_windowed` 改用 `from core.window import merge_window_masks`。
- 两个生产调用方（`get_kline_windowed` / engine 降级）**零改动**（仍调 `DataFetcher.compute_window_bounds`）。
- 阶段 1 保留 `_merge_window_masks = merge_window_masks` re-export 防遗漏，阶段 4 删。

### 3.3 日历方法：不合并，只补注释

第 1 轮审计证实：`get_trading_dates`（raw，`tq.get_trading_dates` 异常**上抛**，`server.py:455-467` 依赖该异常降级本地 JSON）与 `get_trading_days`（robust，异常返空 + 排序去重）语义不同。合并会吞异常、破坏 `/api/calendar` 的兜底。

**改为**：两个方法各补一句 docstring 注明「raw（异常上抛，server.py 依赖）/ robust（异常返空，窗口数学用）」，不合并、不改签名、不动函数体。这解决「名字像、语义不像」的混淆，零风险。

---

## 四、分阶段实施

### 阶段 1：新建 `core/window.py` + 移 `merge_window_masks`

1. 新建 `core/window.py`，把 `_merge_window_masks` 原样搬入改名 `merge_window_masks`（公开）。
2. `core/data_fetcher.py` 顶部 import 区加 `from .window import merge_window_masks`（模块级，放 `[core/data_fetcher.py:8]` 之后），删 `_merge_window_masks` 函数体，`get_kline_windowed` 改用已 import 的 `merge_window_masks`。
3. `core/data_fetcher.py` 模块级加一行 `_merge_window_masks = merge_window_masks` re-export（必须先有步骤 2 的 import，否则 NameError）。
4. `tests/test_merge_window_masks.py` 改 import + 三处调用点名（`[43,103,115]` 的 `_merge_window_masks(...)` → `merge_window_masks(...)`）+ 两处 `caplog.at_level(logger="core.data_fetcher")` → `"core.window"`。

### 阶段 2：移 `compute_window_bounds`

1. `core/window.py` 加 `compute_window_bounds(..., calendar_fetcher=None)`（逻辑全文搬入，拉日历换回调）。
2. `DataFetcher.compute_window_bounds` 变薄委托（§3.2）。
3. `tests/test_window_end_clip.py` 仍调 `DataFetcher.compute_window_bounds`（薄壳），不破；补对纯函数的直测（`calendar_fetcher=None` 走自然日退化路径）。

### 阶段 3：日历方法补注释

`get_trading_dates` 与 `get_trading_days` 各补一句 docstring 区分 raw/robust。**不改签名、不动函数体**。

### 阶段 4：清 re-export + 全量验证

1. 删阶段 1 的 `_merge_window_masks` re-export（grep 确认无遗漏调用方后）。
2. 全量测试 + 逐行验证引用。

---

## 五、风险与兜底

| 风险 | 等级 | 兜底 |
|---|---|---|
| `_merge_window_masks` 有遗漏调用方 | 低 | 阶段 1 保留 re-export，grep 清零再删 |
| `compute_window_bounds` 薄壳日历拉取逻辑漂移 | 中 | calendar_fetcher 回调方案：纯函数全文搬入，只换拉日历那 3 行，逻辑一字不动 |
| 纯函数 `calendar_fetcher=None` 退化自然日路径 | 低 | `tests/test_window_end_clip.py` 已覆盖自然日估算分支 |
| logger 名变更影响测试 | 低 | 同步改 `caplog.at_level(logger=...)` |

**核心兜底**：`tests/test_merge_window_masks.py`（parity）+ `tests/test_window_end_clip.py`（compute_window_bounds 截断）+ 全量测试。

---

## 六、验收标准

1. `core/window.py` 独立模块存在，`merge_window_masks` + `compute_window_bounds` 无 TDX 依赖（calendar_fetcher 回调注入）。
2. `core/data_fetcher.py` 从 594 行降到 ~517-520 行（移走 ~90 行纯函数，薄壳 + re-export 留存 ~12 行）。
3. `DataFetcher.compute_window_bounds` 是薄委托，两个生产调用方零改动。
4. `get_trading_dates`/`get_trading_days` 各补了 docstring 区分 raw/robust（未合并、未改语义）。
5. 全量测试 0 失败。
6. 所有 `[文件:行号]` 引用机器校验通过。

---

## 七、工作量估算

| 阶段 | 估计 |
|---|---|
| 阶段 1（merge_window_masks 搬移） | 0.5 天 |
| 阶段 2（compute_window_bounds 搬移 + 回调） | 0.5~1 天 |
| 阶段 3（日历补注释） | 0.5 小时 |
| 阶段 4（清 re-export + 验证） | 0.5 天 |
| 合计 | 1.5~2 天 |
