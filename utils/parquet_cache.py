# -*- coding: utf-8 -*-
"""文件缓存公共原语 (2026-08-01, 批次2: B1 .tmp 竞态修复 + B2 样板收编)。

B1 背景: selection_cache/signal_day_cache/universe_cache 此前用固定 `.tmp`
文件名做原子写, 多进程/多线程同 key 并发写会互相 replace 移走 tmp →
FileNotFoundError (core/kline_cache.py 2026-07-23 同类事故, 其 _write_merge
的 pid+uuid 临时名 + Windows 退避 atomic replace 范式照抄于此)。

B2 背景: 四个缓存模块 (selection/ 三个 + backtest/matrix_cache) 各自复制
~40 行逐行级样板 (blake2b key、tmp+os.replace 原子写、退避重试、LRU 清理、
缓存根目录), 收编于此。放 utils/ 而非 selection/ 是因 matrix_cache 在
backtest/ 也用, 依赖方向底层←上层。

契约:
- blake2b_key 逐字节复刻各模块原有哈希拼接 (字段 b"\\x00" 分隔,
  digest_size=16) — 落盘文件名不变, 存量缓存不失效
- tmp_path_for: tmp 名带 pid+uuid, 并发写同 key 各进程/线程独立 tmp,
  互不踩踏/移走
- atomic_replace: PermissionError/FileNotFoundError 退避重试 3 次
  (Windows 杀软/索引器短暂锁文件, kline_cache 2026-07-21/23 已 3 次
  杀死长批任务); FileNotFoundError 且 tmp 失踪时经 rewrite 回调重写再试
- cache_root 注册表: set_root(name, path) 覆盖默认缓存根 (tests/conftest.py
  一行式隔离, 根治 2026-07-27 缓存投毒类事故), 未注册回退调用方默认值
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

# cache_root 注册表: name -> Path (set_root 覆盖, get_root 未注册回退默认)
_roots: dict = {}


def set_root(name: str, path) -> None:
    """注册/覆盖某缓存模块的根目录 (测试隔离用; 生产不调用即默认)。"""
    _roots[name] = Path(path)


def get_root(name: str, default) -> Path:
    """取缓存根目录: 注册表命中用注册值, 否则回退模块自带默认。"""
    return _roots.get(name, Path(default))


def blake2b_key(*parts, digest_size: int = 16, seed: bytes | None = None,
                sep: bytes | None = b"\x00") -> str:
    """统一 key 哈希。默认逐字节复刻 selection 三缓存原拼接
    (str 化 + b"\\x00" 字段分隔); seed 供 matrix_cache 传 pandas 行哈希
    字节流, sep=None 复刻其无分隔拼接 — 落盘文件名与旧实现完全一致。"""
    h = hashlib.blake2b(seed or b"", digest_size=digest_size)
    for part in parts:
        h.update(str(part).encode("utf-8"))
        if sep is not None:
            h.update(sep)  # 字段分隔, 防拼接歧义
    return h.hexdigest()


def tmp_path_for(target) -> Path:
    """target 的唯一临时名 (pid+uuid, 照 kline_cache 2026-07-23 范式)。

    并发写同一 target 时各进程/线程独立 tmp, 互不 replace 移走 — 这是
    B1 修复核心: 原固定名 `.tmp` 跨进程共享, 同 key 并发写必现
    FileNotFoundError 且会混合两进程数据。
    """
    target = Path(target)
    return target.with_suffix(f"{target.suffix}.{os.getpid()}.{uuid4().hex}.tmp")


def atomic_replace(tmp, target, *, rewrite=None, attempts: int = 3,
                   backoff: float = 0.5) -> None:
    """os.replace + Windows 退避重试 (照 kline_cache 2026-07-21/23 范式)。

    Windows 杀软/索引器常在 write→replace 间隙短暂锁定文件 →
    PermissionError(WinError 5); tmp 被偶删 (杀软) → FileNotFoundError。
    两种异常退避重试 attempts 次; FileNotFoundError 且 tmp 失踪时经
    rewrite(tmp) 回调重写再试 (rewrite=None 表示内容重建代价高, 直接重试
    到失败抛出, 由调用方外层兜底)。
    """
    tmp, target = Path(tmp), Path(target)
    last_err = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except (PermissionError, FileNotFoundError) as e:
            last_err = e
            time.sleep(backoff * (attempt + 1))
            if (isinstance(e, FileNotFoundError) and not tmp.exists()
                    and rewrite is not None):
                rewrite(tmp)
    raise last_err


def prune_lru(root, keep: int, glob: str = "*.parquet", *, pred=None,
              mtime=None, on_prune=None) -> None:
    """LRU 清理: entries 超 keep 时按 mtime 最老先清 (照四缓存原语义抽象)。

    glob: entries 模式 (signal_day_cache 用 "*/*.parquet",
    matrix_cache 用 "*" + pred 过滤目录); pred: 可选二次过滤;
    mtime: 可选自定义取时 (matrix_cache 用 meta.json mtime);
    on_prune: 可选清理前回调 (各模块日志文案不同, 由调用方给)。
    文件 unlink, 目录 rmtree(ignore_errors); 单个清理失败不中断其余。
    """
    entries = [f for f in Path(root).glob(glob) if pred is None or pred(f)]
    if len(entries) <= keep:
        return

    def _mtime(f) -> float:
        if mtime is not None:
            return mtime(f)
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    entries.sort(key=_mtime)
    for f in entries[: len(entries) - keep]:
        if on_prune is not None:
            on_prune(f)
        try:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink()
        except OSError:
            pass
