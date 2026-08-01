"""concept_kb concept_tagger — 通达信概念打标签 (政策研究平台 P1.5, 计划书 §6)。

实时拉通达信 TDGN 概念 + 成分 → {票: [概念]} 反向索引。
TTL 日级缓存 (保持更新 + 不重复拉 ~17s) + 失败用旧缓存 (松耦合)。

公开接口 (≤8, 计划书 §3):
- build_concept_index(force_refresh) → {票: [概念]}  (带 TTL 缓存)
- tag_stocks(stock_codes) → {票: [概念]} | None
- tag_selections(selections_df) → {票: [概念]} | None  (Pipeline 接缝, 镜像 A 层)
- clear_cache()
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_PATH = Path(__file__).resolve().parent / "concept_cache.json"
_TTL_SECONDS = 24 * 3600  # 默认日级失效 (复用 selection_cache 按日哲学)
_TDGN_PREFIX = "TDGN"  # 通达信概念板块前缀 (题材类); TGN 含财报类暂不取


def _load_cache(allow_expired: bool = False) -> Optional[dict]:
    """读本地缓存. 过期(且非 allow_expired)/损坏返 None."""
    if not _CACHE_PATH.exists():
        return None
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if not allow_expired and time.time() - data.get("fetched_at", 0) > _TTL_SECONDS:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(index: dict, concept_list: list) -> None:
    """写缓存 (反向索引 + 概念列表 + timestamp). 失败不抛."""
    try:
        _CACHE_PATH.write_text(
            json.dumps(
                {"fetched_at": time.time(), "concept_list": concept_list, "index": index},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"concept 缓存写入失败(不影响): {e}")


def _fetch_from_tdx() -> tuple[list, dict]:
    """从通达信实时拉 TDGN 概念 + 成分 → (概念列表, {票:[概念]}). 失败抛."""
    from xtquant import xtdata

    xtdata.enable_hello = False
    all_sectors = xtdata.get_sector_list()
    concepts = [s for s in all_sectors if isinstance(s, str) and s.startswith(_TDGN_PREFIX)]
    index: dict[str, list[str]] = {}
    for c in concepts:
        name = c.removeprefix(_TDGN_PREFIX)
        try:
            stocks = xtdata.get_stock_list_in_sector(c)
        except Exception as e:
            logger.warning(f"拉概念成分失败 [{name}]: {e}")
            continue
        for code in stocks:
            if code:
                index.setdefault(code, []).append(name)
    return concepts, index


def build_concept_index(force_refresh: bool = False) -> dict[str, list[str]]:
    """构建 {票: [概念名]} 反向索引. TTL 缓存, 过期/force_refresh 重新拉.

    松耦合: xtdata 失败 → 用旧缓存(不管 TTL); 无旧缓存返 {} (空, 不抛).
    """
    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            return cached.get("index", {})
    try:
        concepts, index = _fetch_from_tdx()
        _save_cache(index, [c.removeprefix(_TDGN_PREFIX) for c in concepts])
        logger.info(f"concept 反向索引构建: {len(index)} 只票 × {len(concepts)} 概念")
        return index
    except Exception as e:
        logger.warning(f"concept 拉通达信失败, 用旧缓存(松耦合): {e}")
        cached = _load_cache(allow_expired=True)  # 兜底: 旧缓存不管 TTL
        return cached.get("index", {}) if cached else {}


def tag_stocks(stock_codes: list[str]) -> Optional[dict[str, list[str]]]:
    """批量: 票列表 → {票: [概念]}. 失败返 None (松耦合, serialize 不加 key)."""
    if not stock_codes:
        return {}
    try:
        index = build_concept_index()
        return {c: index.get(c, []) for c in stock_codes if c}
    except Exception as e:
        logger.warning(f"tag_stocks 失败(松耦合返None): {e}")
        return None


def tag_selections(selections: pd.DataFrame, code_col: str = "stock_code") -> Optional[dict[str, list[str]]]:
    """Pipeline 接缝: 选股结果 DataFrame → {票: [概念]}.

    失败/空/无 code 列 → None (serialize 不加 key, 选股/回测不受影响).
    写入 policy_enriched.concept_tags (计划书 §5, 不动 policy_priority).
    """
    # 延迟 import: 避免 concept_kb 被 policy_tagger 的 core.data_fetcher 链拖重
    from policy_kb.policy_tagger import tag_selections_of
    return tag_selections_of(selections, code_col, tag_stocks, "concept_tags")


def clear_cache() -> None:
    """清缓存 (强制下次重拉 / 测试用)."""
    try:
        _CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    import sys

    if "--refresh" in sys.argv:
        print("[强制刷新] 拉通达信最新 TDGN 概念...")
    idx = build_concept_index(force_refresh=("--refresh" in sys.argv))
    print(f"[OK] 反向索引: {len(idx)} 只票")
    # 抽查
    samples = [(c, idx[c]) for c in list(idx.keys())[:3]]
    for code, tags in samples:
        print(f"  {code}: {tags[:3]}")
