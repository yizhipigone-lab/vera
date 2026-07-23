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

import hashlib
import json
import os
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 2  # v2: universe 归一化剔除假值默认键 (web/yaml 路径 key 收敛)
KEEP_DEFAULT = 10


def default_cache_root() -> Path:
    """项目根 data/selection_cache (照 data/matrix_cache, data/kline_cache)。"""
    return Path(__file__).resolve().parent.parent / "data" / "selection_cache"


def _normalize_universe(cfg: dict) -> dict:
    """universe 配置归一化 (R5: 完整配置哈希, 新增字段自动纳入)。

    - 剔除假值键 (False/""/[]/None/0): selector 全部 u.get(k, 假值默认),
      "缺键" 与 "显式默认值" 语义相同 — web 路径 (补全默认键) 与 yaml 路径
      (省略默认键) 应产出同 key, 否则跨入口永远 miss
    - 列表值排序: sectors/stocks 语义上是集合, 顺序无关, 防顺序漂移假 miss
    """
    u = {k: v for k, v in dict(cfg or {}).items()
         if v is not None and v is not False and v != "" and v != 0 and v != []}
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
    h = hashlib.blake2b(digest_size=16)
    for part in (formula_name, formula_arg or "", uni_json,
                 start_time, end_time, period, dividend_type,
                 today_str, SCHEMA_VERSION):
        h.update(str(part).encode("utf-8"))
        h.update(b"\x00")  # 字段分隔, 防拼接歧义
    return h.hexdigest()


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
        tmp = pfile.with_suffix(".parquet.tmp")
        table = pa.Table.from_pandas(selections.reset_index(drop=True),
                                     preserve_index=False)
        pq.write_table(table, tmp)
        # 照 kline_cache._write_merge (2026-07-21/23): Windows 杀软/索引器短暂锁
        # 文件 → PermissionError; 并发写同 key 时 tmp 被另一进程 replace 移走 →
        # FileNotFoundError (重写 tmp 再试)。两种异常退避重试 3 次。
        last_err = None
        for _attempt in range(3):
            try:
                os.replace(tmp, pfile)
                break
            except (PermissionError, FileNotFoundError) as e:
                last_err = e
                time.sleep(0.5 * (_attempt + 1))
                if isinstance(e, FileNotFoundError) and not tmp.exists():
                    pq.write_table(table, tmp)
        else:
            raise last_err
        logger.info("选股缓存已保存: %s (%d 条)", key[:12], len(selections))
        _prune(root, keep)
    except Exception as e:
        logger.warning("选股缓存保存失败 (不中断管线): %s", e)


def _prune(root: Path, keep: int) -> None:
    """LRU: 只保留最近 keep 份 (按 parquet mtime)。"""
    entries = [f for f in root.glob("*.parquet")]
    if len(entries) <= keep:
        return

    def _mtime(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    entries.sort(key=_mtime)
    for f in entries[: len(entries) - keep]:
        logger.info("选股缓存 LRU 清理: %s", f.name[:12])
        try:
            f.unlink()
        except OSError:
            pass
