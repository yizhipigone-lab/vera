"""按日信号缓存 (L2, 2026-07-26) — 选股信号按"每个交易日"落盘复用。

计划书: docs/plan/2026-07-26_选股缓存二期_L1池缓存_L2按日信号缓存_计划书.md §3.2

动机: 一期整段缓存 key 含完整区间, 换一天全算 (~32s)。L2 把信号按
(公式+池哈希+period+复权) × 交易日 拆分存储; 请求区间全命中时零公式
调用 (子区间/历史并集覆盖免费), 有缺失则整段重算一次并按天入库。

计算策略 (e2e 实测修正): TDX 公式调用有 ~51 批固定地板成本 (~16s, 与
扫描 bar 数几乎无关 — count 预热 300 根), 按缺失区段分别补算不省钱,
多区段反而 N 倍于整段直跑 — 故采用"整段重算 + 按天命中"策略。

安全线 (计划书 §5 + e2e 修正):
- 当日 (today) 永不缓存 — 盘后数据可能未到位
- 最近 FRESH_DAYS=2 个交易日 (不含当日): 条目带" computed_on=文件 mtime 日期",
  仅当日命中 (当日数据已 settled), 跨日自动重算
- 超过 MAX_AGE_DAYS=60 天的旧信号重算 — 前复权除权漂移
- 空信号日缓存 (稀疏公式多数天无信号), 但批次失败区段不落盘
  (FormulaRunner.last_batch_errors 区分"真空" vs "失败空")
- period != 1d 不走本模块 (粒度不匹配, 调用方守卫)
- 缓存异常一律当未命中/只警告, 绝不中断选股
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1
KEEP_FILES = 20000       # 全局文件数上限 (单文件 KB 级)
MAX_AGE_DAYS = 60        # 超过 60 天的旧信号重算 (除权漂移)
FRESH_DAYS = 2           # 最近 2 个交易日永不缓存 (盘后数据)

ENABLED = True
FORCE_REFRESH = False

_COLS = ["stock_code", "select_date", "formula_name"]


def configure(enabled=None, force_refresh=None, max_age_days=None,
              fresh_days=None) -> None:
    """pipeline 读 selection_cache yaml 应用; 缺省全开。"""
    global ENABLED, FORCE_REFRESH, MAX_AGE_DAYS, FRESH_DAYS
    if enabled is not None:
        ENABLED = bool(enabled)
    if force_refresh is not None:
        FORCE_REFRESH = bool(force_refresh)
    if max_age_days is not None:
        MAX_AGE_DAYS = int(max_age_days)
    if fresh_days is not None:
        FRESH_DAYS = int(fresh_days)


def default_cache_root() -> Path:
    """项目根 data/signal_day_cache。"""
    return Path(__file__).resolve().parent.parent / "data" / "signal_day_cache"


def _combo_key(formula_name: str, formula_arg: str, pool_h: str,
               period: str, dividend_type) -> str:
    h = hashlib.blake2b(digest_size=16)
    for part in (formula_name, formula_arg or "", pool_h, period,
                 dividend_type, SCHEMA_VERSION):
        h.update(str(part).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _day_path(root: Path, combo: str, ds: str) -> Path:
    return root / combo / f"{ds}.parquet"


def _empty_df(formula_name: str) -> pd.DataFrame:
    return pd.DataFrame({
        "stock_code": pd.Series(dtype=str),
        "select_date": pd.Series(dtype="datetime64[ns]"),
        "formula_name": pd.Series(dtype=str),
    })


def _load_day(root: Path, combo: str, ds: str,
              fresh_only_today: str | None = None):
    """命中返回 DataFrame (可能 0 行 = 已算过无信号), 未命中 None。

    fresh_only_today: 传入当日日期 (YYYYMMDD) 时, 只接受当日计算的条目
    (文件 mtime 日期 == 当日) — 最近交易日的"当日有效"规则。
    """
    p = _day_path(root, combo, ds)
    if not p.exists():
        return None
    try:
        if fresh_only_today is not None:
            mtime_ds = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d")
            if mtime_ds != fresh_only_today:
                return None
        df = pq.read_table(p).to_pandas()
        if "stock_code" not in df.columns:
            raise ValueError("缺 stock_code 列")
        os.utime(p)  # LRU
        return df
    except Exception as e:
        logger.warning("L2 日缓存读取异常 (%s/%s), 按未命中处理并清理: %s",
                       combo[:8], ds, e)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _save_day(root: Path, combo: str, ds: str, df: pd.DataFrame) -> None:
    """单天落盘 (tmp+os.replace 原子写 + Windows 退避重试)。失败只警告。"""
    try:
        pfile = _day_path(root, combo, ds)
        pfile.parent.mkdir(parents=True, exist_ok=True)
        tmp = pfile.with_suffix(".parquet.tmp")
        table = pa.Table.from_pandas(df.reset_index(drop=True),
                                     preserve_index=False)
        pq.write_table(table, tmp)
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
    except Exception as e:
        logger.warning("L2 日缓存保存失败 (%s/%s, 不中断选股): %s",
                       combo[:8], ds, e)


def _prune(root: Path, keep: int) -> None:
    """全局 LRU: 文件数超上限, 按 mtime 最老先清。"""
    try:
        entries = list(root.glob("*/*.parquet"))
        if len(entries) <= keep:
            return

        def _mtime(f):
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0

        entries.sort(key=_mtime)
        for f in entries[: len(entries) - keep]:
            logger.info("L2 缓存 LRU 清理: %s/%s", f.parent.name[:8], f.name)
            try:
                f.unlink()
            except OSError:
                pass
    except Exception as e:
        logger.warning("L2 LRU 清理异常 (不影响选股): %s", e)


def _recent_market_days() -> set:
    """市场最近 FRESH_DAYS 个交易日 (YYYYMMDD 集合)。异常时回退空集
    (调用方再兜底按区间尾部处理)。"""
    try:
        from core.data_fetcher import DataFetcher
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - pd.Timedelta(days=14)).strftime("%Y%m%d")
        days = DataFetcher.get_trading_days(start, end)
        return {pd.Timestamp(d).strftime("%Y%m%d") for d in days[-FRESH_DAYS:]}
    except Exception:
        return set()


def get_or_compute(formula_name: str, formula_arg: str, period: str,
                   dividend_type, stock_list: list, start_time: str,
                   end_time: str, force: bool = False) -> pd.DataFrame:
    """区间内逐日信号: 全命中零成本, 有缺失则整段重算一次并按天入库。

    计算策略 (2026-07-26 e2e 实测修正): TDX 公式调用有 ~51 批固定地板成本
    (~16s, 与扫描 bar 数几乎无关 — count 预热 300 根), 按缺失区段分别补算
    并不省钱, 多区段反而 N 倍于整段直跑。因此: 任一缺失 → 整段 [start,end]
    一次算完 (与直跑同价), 按天拆分入库; 价值在"按天粒度命中" — 子区间/
    历史并集覆盖的请求零公式调用。返回值与整段直跑逐条一致 (parity)。
    """
    from core.data_fetcher import DataFetcher
    from core.formula_runner import FormulaRunner
    from selection import universe_cache as ucache

    pool_h = ucache.pool_hash(stock_list)
    combo = _combo_key(formula_name, formula_arg, pool_h, period, dividend_type)
    root = default_cache_root()

    days = [pd.Timestamp(d) for d in
            DataFetcher.get_trading_days(start_time, end_time)]
    if not days:
        return _empty_df(formula_name)
    day_strs = [d.strftime("%Y%m%d") for d in days]

    recent = _recent_market_days()
    today = pd.Timestamp.now().normalize()
    today_ds = datetime.now().strftime("%Y%m%d")

    cached_frames, missing = [], []
    for i, (d, ds) in enumerate(zip(days, day_strs)):
        if ds == today_ds:
            missing.append((i, d, ds, "today"))       # 当日永不缓存
            continue
        if (today - d).days > MAX_AGE_DAYS:
            missing.append((i, d, ds, "stale"))       # 超龄重算并覆盖
            continue
        if not force:
            if ds in recent:
                # 最近交易日: 仅当日计算的条目可命中 (当日有效, 跨日重算)
                df = _load_day(root, combo, ds, fresh_only_today=today_ds)
            else:
                df = _load_day(root, combo, ds)
            if df is not None:
                cached_frames.append(df)
                continue
        missing.append((i, d, ds, "fresh" if ds in recent else "miss"))

    logger.info("L2 按日缓存: 命中 %d 天, 缺失 %d 天", len(cached_frames), len(missing))

    if missing:
        # 任一缺失 → 整段一次算 (与直跑同价; 分日入库供后续按天命中)
        df = FormulaRunner.run_stock_selection_with_dates(
            formula_name=formula_name,
            formula_arg=formula_arg or "",
            stock_list=stock_list,
            start_time=start_time,
            end_time=end_time,
            stock_period=period,
            dividend_type=dividend_type,
        )
        if FormulaRunner.last_batch_errors == 0:
            for (_, d, ds, kind) in missing:
                if kind == "today":
                    continue                    # 当日永不落盘; fresh/miss/stale 均落
                day_df = (df[df["select_date"] == d]
                          if not df.empty else _empty_df(formula_name))
                _save_day(root, combo, ds, day_df)
            _prune(root, KEEP_FILES)
        merged = df if not df.empty else _empty_df(formula_name)
        merged = merged.drop_duplicates(subset=["stock_code", "select_date"])
        return merged.sort_values(["select_date", "stock_code"]).reset_index(drop=True)

    merged = pd.concat(cached_frames, ignore_index=True)
    if merged.empty:
        return _empty_df(formula_name)
    merged = merged.drop_duplicates(subset=["stock_code", "select_date"])
    merged = merged.sort_values(["select_date", "stock_code"]).reset_index(drop=True)
    merged["formula_name"] = formula_name
    return merged
