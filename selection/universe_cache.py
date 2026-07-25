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

import hashlib
import json
import os
import time
from pathlib import Path

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
    """项目根 data/universe_cache。"""
    return Path(__file__).resolve().parent.parent / "data" / "universe_cache"


def build_key(universe_cfg: dict, today_str: str) -> str:
    """universe 完整配置归一化哈希 + 当天日期 (复用一期归一化: 假值默认键剔除)。"""
    from selection.selection_cache import _normalize_universe
    uni_json = json.dumps(_normalize_universe(universe_cfg),
                          sort_keys=True, ensure_ascii=False, default=str)
    h = hashlib.blake2b(digest_size=16)
    for part in (uni_json, today_str, SCHEMA_VERSION):
        h.update(str(part).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def pool_hash(stock_list) -> str:
    """池内容哈希 (L2 组合 key 用; L1 命中后算它零成本, 比按日失效更精确)。"""
    h = hashlib.blake2b(digest_size=16)
    for c in sorted(str(x) for x in stock_list):
        h.update(c.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


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
        tmp = pfile.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(list(stocks), ensure_ascii=False), encoding="utf-8")
        last_err = None
        for _attempt in range(3):
            try:
                os.replace(tmp, pfile)
                break
            except (PermissionError, FileNotFoundError) as e:
                last_err = e
                time.sleep(0.5 * (_attempt + 1))
                if isinstance(e, FileNotFoundError) and not tmp.exists():
                    tmp.write_text(json.dumps(list(stocks), ensure_ascii=False),
                                   encoding="utf-8")
        else:
            raise last_err
        logger.info("池缓存已保存: %s (%d 只)", key[:12], len(stocks))
        _prune(root, keep)
    except Exception as e:
        logger.warning("池缓存保存失败 (不中断选股): %s", e)


def _prune(root: Path, keep: int) -> None:
    """LRU: 只保留最近 keep 份 (按 json mtime)。"""
    entries = list(root.glob("*.json"))
    if len(entries) <= keep:
        return

    def _mtime(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    entries.sort(key=_mtime)
    for f in entries[: len(entries) - keep]:
        logger.info("池缓存 LRU 清理: %s", f.name[:12])
        try:
            f.unlink()
        except OSError:
            pass
