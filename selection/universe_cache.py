"""股票池缓存 (L1, 2026-07-26) — resolve_universe 输出按日缓存。

计划书: docs/plan/2026-07-26_选股缓存二期_L1池缓存_L2按日信号缓存_计划书.md §3.1

resolve_universe 实测 ~17s (拉全 A 5206 只 + 逐只 ST/退市/港股过滤),
输出只随 universe 配置 + 日期变化 (ST 状态日内漂移由按日失效掩蔽,
force_refresh 兜底 — 与一期 selection_cache R8 同口径)。

- key = blake2b(universe 完整配置归一化 + today_str + SCHEMA_VERSION)
- value = 股票代码列表 (json), tmp+os.replace 原子写, LRU 10
- custom 类型池不缓存 (配置即数据, 无计算成本)
- 缓存异常一律当未命中, 绝不中断选股

开关: 模块级 ENABLED / FORCE_REFRESH, pipeline 读 yaml 经 configure() 应用,
tools 默认全开 (L1/L2 接缝在 selector 内部, tools 自动受益)。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# 2026-08-01 批次2: 公共原语收编 (B1 pid+uuid tmp 修并发竞态, B2 去样板)
from utils import parquet_cache as pcu
from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1
KEEP_DEFAULT = 10

ENABLED = True
FORCE_REFRESH = False


def configure(enabled=None, force_refresh=None) -> None:
    """pipeline 读 selection_cache yaml 应用; 缺省全开。"""
    global ENABLED, FORCE_REFRESH
    if enabled is not None:
        ENABLED = bool(enabled)
    if force_refresh is not None:
        FORCE_REFRESH = bool(force_refresh)


def default_cache_root() -> Path:
    """项目根 data/universe_cache (可经 pcu.set_root 覆盖, conftest 隔离)。"""
    return pcu.get_root("universe_cache",
                        Path(__file__).resolve().parent.parent / "data" / "universe_cache")


def build_key(universe_cfg: dict, today_str: str) -> str:
    """universe 完整配置归一化哈希 + 当天日期 (复用一期归一化: 假值默认键剔除)。"""
    from selection.selection_cache import _normalize_universe
    uni_json = json.dumps(_normalize_universe(universe_cfg),
                          sort_keys=True, ensure_ascii=False, default=str)
    # 2026-08-01: 哈希拼接收编 pcu.blake2b_key, 与旧实现逐字节一致 (文件名不变)
    return pcu.blake2b_key(uni_json, today_str, SCHEMA_VERSION)


def pool_hash(stock_list) -> str:
    """池内容哈希 (L2 组合 key 用; L1 命中后算它零成本, 比按日失效更精确)。"""
    return pcu.blake2b_key(*sorted(str(x) for x in stock_list))


def load(cache_root, key: str):
    """命中返回股票代码 list, 未命中 None。任何异常当未命中, 坏文件顺手清。"""
    p = Path(cache_root) / f"{key}.json"
    if not p.exists():
        return None
    try:
        stocks = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(stocks, list) or not stocks:
            raise ValueError("缓存内容异常 (空或非 list)")
        os.utime(p)  # LRU: 命中刷新访问时间
        logger.info("池缓存命中: %s (%d 只)", key[:12], len(stocks))
        return stocks
    except Exception as e:
        logger.warning("池缓存读取异常 (%s), 按未命中处理并清理: %s", key[:12], e)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def save(cache_root, key: str, stocks: list, keep: int = KEEP_DEFAULT) -> None:
    """池列表落盘 (tmp+os.replace 原子写 + Windows 退避重试)。空池不缓存。"""
    if not stocks:
        return
    try:
        root = Path(cache_root)
        root.mkdir(parents=True, exist_ok=True)
        pfile = root / f"{key}.json"
        # 2026-08-01 B1: pid+uuid 独立 tmp (原固定 .tmp 名并发写同 key 互踩
        # → FileNotFoundError, kline_cache 07-23 同类事故); 退避重试收编原语
        tmp = pcu.tmp_path_for(pfile)
        payload = json.dumps(list(stocks), ensure_ascii=False)
        tmp.write_text(payload, encoding="utf-8")
        pcu.atomic_replace(
            tmp, pfile,
            rewrite=lambda t: t.write_text(payload, encoding="utf-8"))
        logger.info("池缓存已保存: %s (%d 只)", key[:12], len(stocks))
        _prune(root, keep)
    except Exception as e:
        logger.warning("池缓存保存失败 (不中断选股): %s", e)


def _prune(root: Path, keep: int) -> None:
    """LRU: 只保留最近 keep 份 (按 json mtime)。"""
    pcu.prune_lru(root, keep, "*.json",
                  on_prune=lambda f: logger.info("池缓存 LRU 清理: %s",
                                                 f.name[:12]))
