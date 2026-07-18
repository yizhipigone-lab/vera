"""回测矩阵级缓存 (2026-07-18) — engine.run() 准备段产物落盘复用。

动机: 止盈止损参数不影响 选股/取数/合并/矩阵准备, 但 server 前台路径
每次重跑都重付全流程成本 (实测 2026-07-18 假死事件: 取数 ~6.5min +
矩阵准备, 详见 commit ebc8fa6)。本模块把准备段产物 (close/entries/
high/low/open/tradable/last_tradable_idx + idx/cols) 按 key 落盘 .npy;
命中时 mmap 加载, 改 1-2 个止盈止损参数重跑 ≈ 只剩核心循环。
与 tools/quantqq_5m_sweep.py 的 prep/run 分离同思路, 这里是 server 路径版。

设计约束:
- 仅 degrade_5m=off 时由 engine 启用 (degrade 产物 degrade_res 含非序列化对象,
  opt-in 实验特性, 不值得为它扩 schema)
- key = 选股结果哈希 + 时间区间 + period + 窗口长度 + 复权 + K线缓存开关
  + ENGINE_VERSION + SCHEMA_VERSION; **不含** stop_config (止盈止损参数随便改)
- K 线数据指纹 (kline_cache 目录最新 mtime) 存 meta.json, 查找时重扫比对,
  数据更新 (新 bar 入库/回填) 自动失效, 不会吃到旧矩阵
- LRU 保留最近 N 份 (默认 3), 5m 全市场单份 GB 级, 防磁盘膨胀
- engine 默认关 (config matrix_cache:true 启用, 测试隔离不受污染);
  pipeline step2 默认开 (server 前台路径受益)
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1
KEEP_DEFAULT = 3

# prep 产物中落盘的数组字段 (None 值字段在 meta 里记 flag)
_ARRAY_FIELDS = ("close", "entries", "high", "low", "open",
                 "tradable", "last_tradable_idx")


def default_cache_root() -> Path:
    """项目根 data/matrix_cache。"""
    return Path(__file__).resolve().parent.parent / "data" / "matrix_cache"


def _kline_cache_dir() -> Path:
    """K 线缓存目录 (与 DataFetcher 同一解析逻辑, 测试可经 _KLINE_CACHE_DIR 覆盖)。"""
    from core.data_fetcher import DataFetcher
    if DataFetcher._KLINE_CACHE_DIR:
        return Path(DataFetcher._KLINE_CACHE_DIR)
    return Path(__file__).resolve().parent.parent / "data" / "kline_cache"


def data_fingerprint() -> float:
    """K 线缓存数据指纹 = 目录树最新文件 mtime (缺目录返回 0.0)。

    scandir 遍历数千 parquet 约 1-2s, 仅缓存查找/保存时各算一次, 可接受。
    """
    root = _kline_cache_dir()
    if not root.exists():
        return 0.0
    latest = 0.0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            try:
                mt = os.path.getmtime(os.path.join(dirpath, fn))
            except OSError:
                continue
            if mt > latest:
                latest = mt
    return latest


def build_key(selections: pd.DataFrame, start_time: str, end_time: str,
              period: str, win_td, use_kline_cache: bool,
              engine_version: str) -> str:
    """缓存 key。选股结果内容哈希 → 公式/股票池/参数区间任何变化自动换 key。

    win_td 在 key 里: max_hold_days 改到触发窗口加长时矩阵本就不同, skip 不得。
    """
    sel = selections[["stock_code", "select_date"]].copy()
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    sel = sel.sort_values(["stock_code", "select_date"]).reset_index(drop=True)
    row_hashes = pd.util.hash_pandas_object(sel, index=False).values
    h = hashlib.blake2b(row_hashes.tobytes(), digest_size=16)
    h.update(str(start_time).encode())
    h.update(str(end_time).encode())
    h.update(str(period).encode())
    h.update(str(win_td).encode())
    h.update(str(bool(use_kline_cache)).encode())
    h.update(str(engine_version).encode())
    h.update(str(SCHEMA_VERSION).encode())
    return h.hexdigest()


def load(cache_root, key: str, engine_version: str):
    """命中返回 prep dict (close/entries 为 DataFrame, 其余 ndarray), 未命中 None。

    任何一步异常 (目录缺失/指纹不符/meta 损坏/shape 不符) 一律当未命中,
    坏目录顺手清掉, 绝不让缓存问题中断回测。
    """
    entry = Path(cache_root) / key
    meta_path = entry / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema") != SCHEMA_VERSION:
            return None
        if meta.get("engine_version") != engine_version:
            return None
        fp_now = data_fingerprint()
        if abs(fp_now - float(meta.get("fingerprint", -1))) > 1e-6:
            logger.info("矩阵缓存失效: K线数据已更新 (指纹 %.0f → %.0f)",
                        float(meta.get("fingerprint", 0)), fp_now)
            return None
        idx = pd.DatetimeIndex(np.load(entry / "idx.npy"))
        cols = meta["cols"]
        prep = {"idx": idx, "cols": cols}
        for f in _ARRAY_FIELDS:
            flag = meta.get(f"has_{f}", True)
            if not flag:
                prep[f] = None
                continue
            arr = np.load(entry / f"{f}.npy", mmap_mode="r")
            if tuple(arr.shape) != tuple(meta["shapes"][f]):
                raise ValueError(f"{f} shape 不符: {arr.shape} != {meta['shapes'][f]}")
            prep[f] = arr
        prep["close"] = pd.DataFrame(
            np.asarray(prep["close"]), index=idx, columns=cols)
        prep["entries"] = pd.DataFrame(
            np.asarray(prep["entries"]), index=idx, columns=cols)
        os.utime(meta_path)  # LRU: 命中刷新访问时间
        logger.info("矩阵缓存命中: %s (%d 股 × %d bar), 跳过选股后全部准备段",
                    key[:12], len(cols), len(idx))
        return prep
    except Exception as e:
        logger.warning("矩阵缓存读取异常 (%s), 按未命中处理并清理: %s", key[:12], e)
        try:
            import shutil
            shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass
        return None


def save(cache_root, key: str, engine_version: str, prep: dict,
         keep: int = KEEP_DEFAULT) -> None:
    """prep dict 落盘 (保存后重扫指纹, 保证与取数后的数据状态一致)。

    prep 需含: close(DataFrame)/entries(DataFrame)/high/low/open/tradable/
    last_tradable_idx (ndarray 或 None)/idx/cols。保存失败只警告不抛。
    """
    try:
        root = Path(cache_root)
        entry = root / key
        tmp = root / f".{key}.tmp"
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        shapes, meta = {}, {}
        for f in _ARRAY_FIELDS:
            v = prep.get(f)
            meta[f"has_{f}"] = v is not None
            if v is None:
                continue
            arr = v.values if isinstance(v, pd.DataFrame) else np.asarray(v)
            np.save(tmp / f"{f}.npy", arr)
            shapes[f] = list(arr.shape)
        idx = prep.get("idx")
        if idx is None:
            idx = prep["close"].index
        np.save(tmp / "idx.npy", np.asarray(idx.values))
        meta.update({
            "schema": SCHEMA_VERSION,
            "engine_version": engine_version,
            "fingerprint": data_fingerprint(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "cols": list(prep["close"].columns),
            "shapes": shapes,
        })
        (tmp / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        shutil.rmtree(entry, ignore_errors=True)
        os.replace(tmp, entry)
        logger.info("矩阵缓存已保存: %s (%d 股 × %d bar)",
                    key[:12], len(meta["cols"]), len(idx))
        _prune(root, keep)
    except Exception as e:
        logger.warning("矩阵缓存保存失败 (不中断回测): %s", e)


def _prune(root: Path, keep: int) -> None:
    """LRU: 只保留最近 keep 份 (按 meta.json mtime)。"""
    entries = [d for d in root.iterdir()
               if d.is_dir() and not d.name.startswith(".")]
    if len(entries) <= keep:
        return
    def _mtime(d):
        try:
            return (d / "meta.json").stat().st_mtime
        except OSError:
            return 0.0
    entries.sort(key=_mtime)
    import shutil
    for d in entries[: len(entries) - keep]:
        logger.info("矩阵缓存 LRU 清理: %s", d.name[:12])
        shutil.rmtree(d, ignore_errors=True)
