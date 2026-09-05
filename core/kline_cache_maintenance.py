"""core/kline_cache_maintenance.py — K 线缓存新鲜度检查 + 后台补拉（2026-08-14）。

动机：5M 回测取数超时事故（缓存尾段停在 07-31，回测时几百只票逐只走 TDX
补尾段 + 分红 shift 全量重拉，取数 1 小时+）。调用方三处，共用本模块：
  - scheduler 每交易日 15:45 调 ensure_cache_fresh("scheduler_daily")
  - server 启动时调 ensure_cache_fresh("server_startup")
  - 数据准备 TAB 调 cache_status() / start_backfill()（手动管理台）

设计要点：
- 检查很便宜：读 manifest.db 的 MAX(last_date)，与"最近应有数据的交易日"
  比（收盘后=今天，盘中/非交易日=上一交易日），stale 才动手；
- 补拉很贵（全池 1-2 小时）→ 后台 daemon 线程 + 子进程，调用方秒回；
- 跨进程防重入靠锁文件（server 和 scheduler 是两个进程，进程内变量没用）：
  refresh.lock 4 小时内有效，崩溃遗留的锁超时自动失效；进行中的补拉由
  回填子进程心跳续命 mtime（不续则长任务会被 TTL 误收尸 → 双补拉，2026-09-05 体检 P1）；
- backfill 幂等（已缓存部分零重拉），重复触发无害。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

from utils.logger import get_logger
from utils.sysutil import project_root

_logger = get_logger("core.kline_cache_maintenance")

_CACHE_DIR = project_root() / "data" / "kline_cache"
# (period, 回填起点) — 起点沿用既有 backfill_state 文件约定；
# 1m 排最后：数据量是 5m 的 5 倍（240 根/日 vs 48 根/日），最重的一段压轴
_SEGMENTS = (("5m", "20240627"), ("1d", "20240101"), ("1m", "20260126"))
# 2026-08-14 用户拍板：默认股票池"50"=沪深A股，不含北交所。
# 此前用 backfill 工具默认 "5"=全部A股，把 577 只 .BJ 也拉进了缓存
# （多数还拉不到数据白烧请求）——VERA 交易池是沪深，.BJ 数据零用途。
_DEFAULT_UNIVERSE = "50"
_LOCK = _CACHE_DIR / "refresh.lock"
_LOCK_TTL = dt.timedelta(hours=4)       # 锁超时：崩溃遗留锁自动失效
_REFRESH_LOG = _CACHE_DIR / "refresh.log"

_thread: threading.Thread | None = None  # 进程内防重入（快路径，锁文件是慢路径兜底）


# ── 新鲜度检查 ─────────────────────────────────────────────

def expected_last_trading_day(now: dt.datetime | None = None) -> dt.date:
    """最近应有数据的交易日：交易日 15:00 后=今天，否则=上一交易日。"""
    from scheduler.trading_calendar import is_trading_day
    now = now or dt.datetime.now()
    d = now.date()
    if is_trading_day(d) and now.hour >= 15:
        return d
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def _cache_handle(cache_dir: Path | None):
    """manifest.db 存在时返回共享 KlineCache 实例 (读统计经接口, 治理III W3-schema)。

    db 不存在返 None (调用方按"无缓存/全缺"处理)。db 存在时构造只会走
    _init_db (表已存在 → no-op), 不产生副作用; 持久连接由实例持有。
    """
    from core.kline_cache import KlineCache
    db = (cache_dir or _CACHE_DIR) / "manifest.db"
    if not db.exists():
        return None
    return KlineCache(cache_dir or _CACHE_DIR,
                      tdx_fetcher=lambda *a, **k: {},
                      calendar_fetcher=lambda: [])


def cached_last_date(period: str, cache_dir: Path | None = None) -> str | None:
    """manifest 里该 period 的 MAX(last_date)（'YYYYMMDD'），无记录返 None。

    读操作经 KlineCache.cached_last_date 接口 (schema 单点, 治理III W3-schema);
    异常按"不新鲜"处理 (返回 None → 上层判定 stale 触发补拉, 方向安全)。"""
    kc = _cache_handle(cache_dir)
    if kc is None:
        return None
    try:
        return kc.cached_last_date(period)
    except Exception as e:
        _logger.warning("读 manifest 失败（按不新鲜处理）: %s", e)
        return None


def stale_periods(now: dt.datetime | None = None,
                  cache_dir: Path | None = None) -> list[str]:
    """哪些 period 的缓存落后于最近交易日。全新鲜返 []。"""
    expected = expected_last_trading_day(now).strftime("%Y%m%d")
    return [p for p, _ in _SEGMENTS
            if (cached_last_date(p, cache_dir) or "") < expected]


def period_stats(period: str, cache_dir: Path | None = None) -> dict:
    """单个 period 的缓存概览（数据准备 TAB 用）。

    读操作经 KlineCache.manifest_stats 接口 (schema 单点, 治理III W3-schema)。"""
    stats = {"period": period, "stocks": 0, "first_date": None,
             "last_date": None, "not_intact": 0}
    kc = _cache_handle(cache_dir)
    if kc is None:
        return stats
    try:
        stats.update(kc.manifest_stats(period))
    except Exception as e:
        _logger.warning("读 manifest 统计失败 (%s): %s", period, e)
    return stats


def cache_status() -> dict:
    """数据准备 TAB 状态总览：各 period 新鲜度 + 是否补拉中。"""
    expected = expected_last_trading_day().strftime("%Y%m%d")
    periods = []
    for p, _ in _SEGMENTS:
        s = period_stats(p)
        s["expected"] = expected
        s["stale"] = (s["last_date"] or "") < expected
        periods.append(s)
    return {"expected": expected, "periods": periods,
            "refreshing": is_refreshing()}


# ── 补拉（后台线程 + 子进程，锁文件跨进程防重入）─────────────

def is_refreshing() -> bool:
    """是否有补拉进行中（本进程线程 or 锁文件）。"""
    return (_thread is not None and _thread.is_alive()) or _lock_held()


def _lock_held() -> bool:
    """锁文件存在且未超时 → 有进程正在补拉。"""
    try:
        if not _LOCK.exists():
            return False
        age = dt.datetime.now() - dt.datetime.fromtimestamp(_LOCK.stat().st_mtime)
        if age < _LOCK_TTL:
            return True
        _LOCK.unlink(missing_ok=True)  # 超时锁：上届进程已死（心跳也停了），收尸
        return False
    except Exception:
        return False  # 锁判断本身出错就当没锁（补拉幂等，最坏多跑一遍）


def _lock_owner() -> dict | None:
    """读锁内容（{pid, trigger, ts}），读不出返 None（旧版/被占用半截）。"""
    try:
        data = json.loads(_LOCK.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _lock_held_by() -> str:
    """锁持有者的可读描述（"谁在补拉、何时开始"），无锁返空串。

    体检 P1: 此前锁只是时间戳, 被跳过方只知道"有锁", 不知谁在拉、
    何时开始 —— 归属不可见。改为 JSON 后 skip 原因能带出持有者。
    """
    owner = _lock_owner()
    if not owner:
        return ""
    pid = owner.get("pid", "?")
    trigger = owner.get("trigger", "?")
    ts = owner.get("ts", "")
    return f" (持有者 pid={pid} trigger={trigger} 自 {ts})"


def _run_refresh(trigger: str, segments: list[tuple],
                 universe: str | None, end: str, limit: int) -> None:
    """后台线程体：依次跑各段 backfill 子进程（串行，单段失败不连坐）。"""
    import subprocess
    import sys
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(_REFRESH_LOG, "a", encoding="utf-8") as lf:
            lf.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} "
                     f"缓存补拉启动 (trigger={trigger}) =====\n")
            lf.flush()
            for period, start in segments:
                cmd = [sys.executable, "tools/backfill_kline_cache.py",
                       "--period", period, "--start", start]
                if end:
                    cmd += ["--end", end]
                if universe:
                    cmd += ["--universe", universe]
                if limit:
                    cmd += ["--limit", str(limit)]
                _logger.info("缓存补拉[%s]: %s 段开始", trigger, period)
                rc = subprocess.call(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                     cwd=str(project_root()))
                _logger.info("缓存补拉[%s]: %s 段结束 rc=%s", trigger, period, rc)
    except Exception:
        _logger.exception("缓存补拉[%s] 异常", trigger)
    finally:
        _LOCK.unlink(missing_ok=True)


def start_backfill(segments: list[tuple] | None = None, trigger: str = "manual",
                   universe: str | None = None, end: str = "",
                   limit: int = 0) -> dict:
    """启动后台补拉（锁守卫）。已在运行返 {"started": False, "reason": ...}。

    universe 缺省 = _DEFAULT_UNIVERSE（沪深A股，不含北交所，2026-08-14 拍板）。
    """
    global _thread
    if _thread is not None and _thread.is_alive():
        return {"started": False, "reason": "本进程补拉进行中"}
    if _lock_held():
        return {"started": False,
                "reason": f"其他进程补拉进行中（refresh.lock）{_lock_held_by()}"}
    segs = list(segments or _SEGMENTS)
    universe = universe or _DEFAULT_UNIVERSE
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # 锁内容 {pid, trigger, ts}: 供"谁在拉/何时开始"归属查询 (体检 P1);
    # 活跃锁的 mtime 由回填子进程心跳续命, 长任务不会过期被误收尸。
    _LOCK.write_text(json.dumps(
        {"pid": os.getpid(), "trigger": trigger,
         "ts": dt.datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False), encoding="utf-8")
    _thread = threading.Thread(
        target=_run_refresh, args=(trigger, segs, universe, end, limit),
        name="kline-cache-refresh", daemon=True)
    _thread.start()
    _logger.info("缓存补拉[%s] 已启动: %s（日志 %s）",
                 trigger, [s[0] for s in segs], _REFRESH_LOG)
    return {"started": True, "trigger": trigger,
            "periods": [s[0] for s in segs], "log": str(_REFRESH_LOG)}


def ensure_cache_fresh(trigger: str = "manual") -> dict:
    """检查缓存新鲜度，不新鲜则后台补拉。返 {fresh, stale, refreshing}。

    调用方秒回：检查是毫秒级（一次 sqlite 查询），补拉在后台线程。
    fail-soft：任何异常都返 fresh=True（缓存问题永不挡主流程）。
    """
    try:
        stale = stale_periods()
        if not stale:
            _logger.info("K线缓存检查[%s]: 新鲜，无需补拉", trigger)
            return {"fresh": True, "stale": [], "refreshing": False}
        r = start_backfill(trigger=trigger)
        if not r["started"]:
            _logger.info("K线缓存检查[%s]: 缺 %s 但%s，跳过",
                         trigger, stale, r["reason"])
        else:
            _logger.info("K线缓存检查[%s]: 缺 %s，已后台启动补拉", trigger, stale)
        return {"fresh": False, "stale": stale, "refreshing": True}
    except Exception as e:
        _logger.warning("K线缓存检查[%s] 异常（不挡主流程）: %s", trigger, e)
        return {"fresh": True, "stale": [], "refreshing": False}


def read_log_tail(n: int = 80) -> list[str]:
    """补拉日志最后 n 行（数据准备 TAB 进度显示用）。无日志返 []。"""
    try:
        if not _REFRESH_LOG.exists():
            return []
        lines = _REFRESH_LOG.read_text(
            encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []
