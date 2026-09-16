"""选股结果缓存 (2026-07-24) — StockSelector.run() 输出整段落盘复用。

计划书: docs/plan/2026-07-24_选股结果缓存_计划书.md
动机: bench_phases 实测选股占回测总耗时 92% (5m 全 A: 32.8s/35.5s,
resolve_universe ~17s + 公式分批 ~16s), 而 selector/formula_runner 零缓存,
改止盈止损重跑同一公式也全付 ~32s。本模块把 selections DataFrame 按 key
落盘 parquet, 命中时选股阶段 → 亚秒级。

设计约束:
- key = 公式 + formula_arg + universe 完整配置哈希 + 区间 + period + 复权
  + today_str (按日失效, 盘后数据日级更新天然匹配) + SCHEMA_VERSION
- **不纳入** resolve_universe 实际输出列表哈希 (R8): 算列表哈希要先花 ~17s
  跑 resolve_universe, 整段缓存省 32s 的意义打折; 接受按日失效掩蔽日内
  ST 过滤漂移 (实测 33 秒内池 5001↔5002), force_refresh 开关兜底
- parquet 落盘 (照 kline_cache), tmp + os.replace 原子写 + Windows 退避重试
- LRU 保留最近 10 份 (selections KB 级, 远小于 matrix_cache GB 级)
- 空结果不缓存 (防空帧误导; 空结果常意味着公式/数据异常, 值得每次重跑)
- 任何缓存异常一律当未命中/只警告, 绝不让缓存问题中断管线
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# 2026-08-01 批次2: 公共原语收编 (B1 pid+uuid tmp 修并发竞态, B2 去样板)
from utils import parquet_cache as pcu
from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 3  # v3 (2026-07-31): _adaptive_scan_count 回退写死3000 (TDX count 从当前日期往前、忽略 end_time; 旧自适应按 end_time 估算致 end<今天的历史回测丢前段信号)。bump 使 v2 错误选股缓存全部失效重算
KEEP_DEFAULT = 10


def default_cache_root() -> Path:
    """项目根 data/selection_cache (照 data/matrix_cache, data/kline_cache)。
    可经 pcu.set_root 覆盖 (conftest 一行式隔离, 2026-08-01)。"""
    return pcu.get_root("selection_cache",
                        Path(__file__).resolve().parent.parent / "data" / "selection_cache")


# 2026-09-16 审计 P0-1 修复: selector 中缺省为 True 的键 (exclude_quit,
# 见 selection/selector.py:146 的 u.get("exclude_quit", True)), 显式 False
# 是"保留退市股"非缺省语义, 绝不能当假值剔掉 — 否则 exclude_quit: False
# 与缺键撞 key, 两种池子内容不同却共用一份缓存 (回测池语义被偷换)。
# 这类键: 显式 False 保留在 key 里; 值恰为 True (= 缺省) 才剔除
# (与"缺键"语义相同, 维持 web/yaml 两入口 key 收敛)。其余键维持原假值剔除。
_DEFAULT_TRUE_KEYS = {"exclude_quit"}


def _keep_universe_key(k: str, v) -> bool:
    """归一化单键判定 (2026-09-16 P0-1 缺省表驱动)。

    - _DEFAULT_TRUE_KEYS (缺省为 True 的键): 仅 True (= 缺省值) 与 None 剔除,
      显式 False 必须保留在 key 里 (语义与缺省相反 — 保留退市股);
      注意不能走通用假值链 — Python 里 False == 0, 会被 v != 0 误剔
    - 其余键维持原假值剔除逻辑 (False/""/[]/None/0)
    """
    if k in _DEFAULT_TRUE_KEYS:
        return v is not None and v is not True
    return v is not None and v is not False and v != "" and v != 0 and v != []


def _normalize_universe(cfg: dict) -> dict:
    """universe 配置归一化 (R5: 完整配置哈希, 新增字段自动纳入)。

    - 剔除假值键 (False/""/[]/None/0): selector 全部 u.get(k, 假值默认),
      "缺键" 与 "显式默认值" 语义相同 — web 路径 (补全默认键) 与 yaml 路径
      (省略默认键) 应产出同 key, 否则跨入口永远 miss。
      例外: _DEFAULT_TRUE_KEYS 的显式 False 保留 (见 _keep_universe_key)
    - 列表值排序: sectors/stocks 语义上是集合, 顺序无关, 防顺序漂移假 miss
    """
    u = {k: v for k, v in dict(cfg or {}).items()
         if _keep_universe_key(k, v)}
    for k, v in u.items():
        if isinstance(v, (list, tuple)) and all(
                isinstance(x, (str, int, float, bool)) for x in v):
            u[k] = sorted(v, key=str)
    return u


def build_key(formula_name: str, formula_arg: str, universe_cfg: dict,
              start_time: str, end_time: str, period: str,
              dividend_type, today_str: str) -> str:
    """缓存 key (blake2b hex)。

    today_str (YYYYMMDD) 由调用方注入: 生产侧 datetime.now(), 测试传常量
    绕过真实跨日等待。formula_arg 的 None 与空串统一归一化 (R6)。
    """
    uni_json = json.dumps(_normalize_universe(universe_cfg),
                          sort_keys=True, ensure_ascii=False, default=str)
    # 2026-08-01: 哈希拼接收编 pcu.blake2b_key, 与旧实现逐字节一致 (文件名不变)
    return pcu.blake2b_key(formula_name, formula_arg or "", uni_json,
                           start_time, end_time, period, dividend_type,
                           today_str, SCHEMA_VERSION)


def _parquet_path(cache_root, key: str) -> Path:
    return Path(cache_root) / f"{key}.parquet"


def load(cache_root, key: str):
    """命中返回 selections DataFrame, 未命中 None。

    任何异常 (目录缺失/文件损坏/缺列) 一律当未命中, 坏文件顺手清掉,
    绝不让缓存问题中断管线。
    """
    pfile = _parquet_path(cache_root, key)
    if not pfile.exists():
        return None
    try:
        df = pq.read_table(pfile).to_pandas()
        if df.empty or "stock_code" not in df.columns:
            raise ValueError("缓存内容异常 (空或缺 stock_code 列)")
        os.utime(pfile)  # LRU: 命中刷新访问时间
        logger.info("选股缓存命中: %s (%d 条)", key[:12], len(df))
        return df
    except Exception as e:
        logger.warning("选股缓存读取异常 (%s), 按未命中处理并清理: %s",
                       key[:12], e)
        try:
            pfile.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def save(cache_root, key: str, selections: pd.DataFrame,
         keep: int = KEEP_DEFAULT) -> None:
    """selections 落盘 parquet (tmp + os.replace 原子写)。保存失败只警告不抛。

    空结果不落盘 (防空帧误导)。
    """
    if selections is None or selections.empty:
        return
    try:
        root = Path(cache_root)
        root.mkdir(parents=True, exist_ok=True)
        pfile = _parquet_path(root, key)
        # 2026-08-01 B1: pid+uuid 独立 tmp (原固定 .tmp 名并发写同 key 互踩
        # → FileNotFoundError, kline_cache 07-23 同类事故); 退避重试收编原语
        tmp = pcu.tmp_path_for(pfile)
        table = pa.Table.from_pandas(selections.reset_index(drop=True),
                                     preserve_index=False)
        pq.write_table(table, tmp)
        pcu.atomic_replace(tmp, pfile,
                           rewrite=lambda t: pq.write_table(table, t))
        logger.info("选股缓存已保存: %s (%d 条)", key[:12], len(selections))
        _prune(root, keep)
    except Exception as e:
        logger.warning("选股缓存保存失败 (不中断管线): %s", e)


def _prune(root: Path, keep: int) -> None:
    """LRU: 只保留最近 keep 份 (按 parquet mtime)。"""
    pcu.prune_lru(root, keep, "*.parquet",
                  on_prune=lambda f: logger.info("选股缓存 LRU 清理: %s",
                                                 f.name[:12]))
