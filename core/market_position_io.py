# -*- coding: utf-8 -*-
"""core/market_position_io.py — 大盘位置 IO 层共享底座 (2026-09-19 批次 5.1 自 runner 端出)。

**为什么要有这一层**: `core/market_position_runner.py` 曾是全场最大文件 (2087 行/八种
职责), 计划书想"四刀各拆一个自包含块"; 实测 AST 依赖后发现 **ERP / 指标体检 / 牛熊
regime / 影子回放 / 体温表文案五块全部依赖同一组共享原语** (路径常量 / `_f` /
`_index_series` / `history` / `_upsert`), 而 runner 公开面也要这些块 —— 先拆任何一块
都会形成 runner ↔ 新模块循环 import。所以**先抽底座**, 之后每刀都是单向依赖
(runner → 块模块 → 本模块)。

**路径常量的唯一所有者就是本模块**: `DAILY_PATH` / `KLINE_1D_DIR` / `_ROOT` 在这里
定义, runner 里**不再保留副本**(旧入口 `mpr.DAILY_PATH` 经模块级 `__getattr__` 动态
转发到本模块) —— 这样测试隔离 (conftest) 只需 patch 本模块一处, 不会出现"一个模块
被指到 tmp、另一个还在写生产路径"的投毒型事故 (2026-07-27 教训)。

内容: 路径/常量 + parquet 读取原语 (`_index_series`) + 格式化 (`_f`) + 交易日
(`_expected_trading_day`) + JSONL 落盘 (跨进程锁 + `_upsert`) + 读取 (`history`/`latest`)。
零 trade 依赖 (业务铁律 1 由 tests/test_market_position.py 的 AST 断言守护)。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

from utils.logger import get_logger

_logger = get_logger(__name__)


_ROOT = Path(__file__).resolve().parent.parent

#: 日线缓存目录 (与 core.kline_cache 的 <cache_dir>/<period>/<code>.parquet 约定一致)
KLINE_1D_DIR = _ROOT / "data" / "kline_cache" / "1d"

#: 连续录像落盘路径 (JSONL, 一天一行)
DAILY_PATH = _ROOT / "data" / "market_position" / "daily.jsonl"

#: 三大指数: (内部键, 中文名, TDX 代码)
INDEX_SPECS = (("shanghai", "上证指数", "000001.SH"),
               ("hs300", "沪深300", "000300.SH"),
               ("chuangyeban", "创业板指", "399006.SZ"))

#: 跨进程写锁等待上限 (秒) —— 采集是后台任务, 卡住不如报错; 测试可注入短值。
_UPSERT_LOCK_TIMEOUT = 30.0


def _index_series(code: str) -> pd.Series | None:
    """读单个指数日线 close 序列; 文件不存在/为空返 None。"""
    p = KLINE_1D_DIR / f"{code}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p, columns=["date", "close"])
    except Exception as e:
        _logger.warning("大盘位置: 读指数 %s 失败: %s", code, e)
        return None
    if len(df) == 0:
        return None
    s = pd.Series(df["close"].to_numpy(dtype=float),
                  index=pd.to_datetime(df["date"]))
    return s[~s.index.duplicated(keep="last")].sort_index()


def _f(v, nd: int = 1):
    """→ float 或 None (NaN/inf 一律 None, 不拿 0 冒充缺失)。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return round(x, nd)


def _expected_trading_day() -> dt.date:
    """最近应有日线数据的交易日 (复用 kline_cache_maintenance 单一真相源)。"""
    try:
        from core.kline_cache_maintenance import expected_last_trading_day
        return expected_last_trading_day()
    except Exception as e:      # 日历不可用 → 退化为今天 (上层只会更宽松, 不谎报新鲜)
        _logger.warning("大盘位置: 交易日历不可用, 按今天处理: %s", e)
        return dt.date.today()


def _upsert(records: list[dict], path: Path | None = None) -> int:
    """按日期 upsert 到 JSONL (同一天覆盖, 不追加重复行) → 返回总行数。

    原子写: 临时文件 + os.replace (照 VeraScheduler._save_state 的写法),
    防崩溃写半个文件把整段录像毁掉。

    2026-09-19 批次 4.4: 加**跨进程文件锁** —— 本文件的写者有两个进程
    (scheduler 的 15:50/15:55/09:05 三个 job + server 的手动 collect 接口),
    而 `_COLLECT_LOCK` 只是 threading.Lock, 跨进程毫无作用; 原来靠"原子写
    last-write-wins"兜底, 两个进程同时读-改-写会丢记录。锁超时 (默认 30s)
    抛 TimeoutError, 由调用方转成"另一个进程正在采集"的明确错误 (fail-closed,
    绝不静默丢一半)。
    """
    if not records:
        return 0
    p = Path(path or DAILY_PATH)
    with _cross_process_lock(p, timeout=_UPSERT_LOCK_TIMEOUT):
        return _upsert_locked(records, p)


def _upsert_locked(records: list[dict], p: Path) -> int:
    """_upsert 的实际实现 (调用方已持跨进程锁)。"""
    existing: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict) and o.get("date"):
                existing[str(o["date"])] = o
    for r in records:
        existing[str(r["date"])] = r
    rows = [json.dumps(existing[k], ensure_ascii=False) for k in sorted(existing)]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(rows) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return len(rows)

@contextlib.contextmanager


def _cross_process_lock(p: Path, timeout: float = 30.0, poll: float = 0.1):
    """跨进程写锁 (2026-09-19 批次 4.4)。

    实现: 数据文件同目录的 `<名>.lock` + 平台文件锁 (Windows msvcrt.locking /
    POSIX fcntl.flock), 非阻塞尝试 + 轮询到 timeout。拿不到 → TimeoutError
    (调用方转人话错误; **不阻塞长等**, 采集是后台任务, 卡住不如报错)。
    锁文件本身不删 (删除会引入"删了别人的锁"竞态), 内容仅 1 字节占位。
    """
    lock_path = p.with_suffix(p.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        if lock_path.stat().st_size == 0:
            fh.write(b"L")
            fh.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                _lock_file(fh)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"跨进程写锁超时 ({timeout:.0f}s): {lock_path.name} "
                        "被另一个进程持有 (scheduler 采集 或 页面手动采集)") from None
                time.sleep(poll)
        try:
            yield
        finally:
            try:
                _unlock_file(fh)
            except OSError:
                pass
    finally:
        fh.close()


if sys.platform == "win32":  # pragma: no cover - 平台分支
    import msvcrt

    def _lock_file(fh) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:  # pragma: no cover - 平台分支
    import fcntl

    def _lock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def history(limit: int = 250) -> list[dict]:
    """读连续录像, 按日期升序; limit=0 返全部。文件坏行跳过不抛。"""
    p = Path(DAILY_PATH)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict) and o.get("date"):
            out.append(o)
    out.sort(key=lambda r: str(r["date"]))
    return out[-int(limit):] if limit else out


def latest() -> dict | None:
    """最新一条记录 (无录像返 None)。"""
    h = history(limit=1)
    return h[-1] if h else None


# ── 第二批搬运 (2026-09-19 批次 5.1 第二刀): 路径 + 格式化/特征原语 ──

#: 外部估值序列 (ERP 股债性价比) 的本地缓存, 一天一行。
#: **为什么单独一个文件**: 它来自网络 (乐咕乐股), 与日线缓存这个数据源无关;
#: 混进 daily.jsonl 会让"回填"这条纯本地路径变成联网路径。
ERP_PATH = _ROOT / "data" / "market_position" / "erp.jsonl"


def _features_frame(recs: list[dict]) -> pd.DataFrame:
    """连续录像 → 照镜子用的特征表 (列 = SIMILAR_FEATURES, 索引 = 日期)。"""
    rows = []
    for r in recs:
        idx = r.get("indices") or {}
        b = r.get("breadth") or {}
        t = r.get("turnover") or {}
        traded = b.get("traded") or 0
        hs = idx.get("hs300") or {}
        sh = idx.get("shanghai") or {}
        rows.append({
            "date": r["date"],
            "sh_pct": sh.get("pct_10y"),
            "hs300_pct": hs.get("pct_10y"),
            "above_ma20_pct": b.get("above_ma20_pct"),
            "hl_spread_pct": (b.get("hl_spread") / traded * 100) if traded else None,
            "vol_ann_20": hs.get("vol_ann_20"),
            "amount_pct_1y": t.get("amount_pct_1y"),
        })
    df = pd.DataFrame(rows)
    return df.set_index("date") if len(df) else df


def _num(v, nd: int = 2) -> str:
    return "【缺】" if v is None else f"{v:.{nd}f}"


def _pct(v) -> str:
    """带符号百分数 (收益/偏离/回撤 这类有方向的量)。

    四舍五入后是 0 时不写符号 —— 写 "+0.0%" / "-0.0%" 会被当成有方向, 误导。
    """
    if v is None:
        return "【缺】"
    return "0.0%" if abs(float(v)) < 0.05 else f"{float(v):+.1f}%"


def _rat(v) -> str:
    """不带符号百分数 (占比/分位 这类 0~100 的量, 写 +90.9% 会误导)。"""
    return "【缺】" if v is None else f"{v:.1f}%"


def _yi(v) -> str:
    return "【缺】" if v is None else f"{v:,.0f}亿元"
