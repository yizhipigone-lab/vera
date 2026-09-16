"""
股票池过滤工具（修复 VERA 审计 C5 问题）

修复点:
  - 原代码 `'ST' not in c` 对纯代码字符串恒为真 → ST 过滤完全失效
  - 改用 TDX `get_stock_info()` 的 `IsSTGP` 字段真实判定

注意: 该模块完全独立, 不修改 DataFetcher.get_stock_universe 原有签名,
       保证老脚本 (33+ 个) 继续按原行为运行。
       新代码/新脚本请用 `from core.stock_filter import filter_stocks, get_stock_info_batch`

TDX 接口依赖: tq.get_stock_info(code) → dict 含 IsSTGP, IsQuitGP, IsHKGP, Name 等
"""
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from core import progress as _progress
from core.connector import TdxConnector
from core.data_cache import SECTOR_TTL_SEC as _STOCK_INFO_TTL_SEC  # TTL 语义同源 (治理III W3-get_or)
from utils.logger import get_logger

# 2026-09-16 C1: 补模块 logger — 原全模块无 logger, filter_stocks 注释承诺的
# "记 warn" 根本发不出去 (TDX 整挂时全池不过滤静默通过, fail-open 无声)。
logger = get_logger(__name__)

# info 缺失占比告警阈值: 超过则过滤结果不可信 (TDX 整挂=100% 缺失)
_INFO_MISSING_WARN_RATIO = 0.05


def _get_info_safe(code: str) -> dict:
    """单只股票信息获取，吞异常返回空 dict"""
    try:
        tq = TdxConnector.tq()
        info = tq.get_stock_info(code)
        return info if isinstance(info, dict) else {}
    except Exception:
        return {}


def get_stock_info_batch(codes: List[str], max_workers: int = 10) -> Dict[str, dict]:
    """
    批量获取股票信息 (并行).

    Returns: {code: info_dict}  (info 含 Name, IsSTGP, IsQuitGP, IsHKGP, Unit, ...)

    注意: TDX 单连接不严格线程安全, max_workers 保守取 5-10.
          单次调用几百到 5k 只股票大约 30-60s.
    """
    TdxConnector.ensure_connected()
    result = {}
    n = len(codes)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i, (code, info) in enumerate(zip(codes, ex.map(_get_info_safe, codes)), 1):
            if info:
                result[code] = info
            if i % 200 == 0 or i == n:  # 2026-07-26: 细粒度进度 (无人读时 ~1µs)
                _progress.report("st_filter", i / n, f"{i}/{n} 只", i, n)
    return result


def filter_stocks(
    codes: List[str],
    exclude_st: bool = True,
    exclude_quit: bool = True,
    exclude_hk: bool = True,
    cache: Optional[Dict[str, dict]] = None,
) -> Tuple[List[str], List[dict]]:
    """
    过滤股票池: 排除 ST / 退市 / 港股 (按 TDX 真实标记)

    Args:
        codes: 输入股票代码列表 (e.g. ['600519.SH', '000001.SZ'])
        exclude_st: 排除 ST/*ST
        exclude_quit: 排除已退市 (IsQuitGP=1)
        exclude_hk: 排除港股 (IsHKGP=1)
        cache: 可选缓存, 加速二次调用 (传上次返回的 info_map)

    Returns:
        (filtered_codes, excluded_records)
        - filtered_codes: 过滤后的股票代码列表
        - excluded_records: 被过滤掉的 [{code, name, reason}], 供审计
    """
    if cache is None:
        cache = get_stock_info_batch(codes)

    filtered = []
    excluded = []
    n_missing = 0
    for code in codes:
        info = cache.get(code, {})
        if not info:
            # 拿不到信息 → 保守保留 (不误伤), 记 debug (占比统计在循环后,
            # 超阈值升 warning — 2026-09-16 C1: 原注释承诺的 warn 发不出)
            n_missing += 1
            logger.debug("stock_info 缺失: %s 拿不到 ST/退市信息, 保守保留", code)
            filtered.append(code)
            continue
        is_st = str(info.get('IsSTGP', '0')) == '1'
        is_quit = str(info.get('IsQuitGP', '0')) == '1'
        is_hk = str(info.get('IsHKGP', '0')) == '1'
        name = info.get('Name', '')

        if exclude_st and is_st:
            excluded.append({'code': code, 'name': name, 'reason': 'ST'})
            continue
        if exclude_quit and is_quit:
            excluded.append({'code': code, 'name': name, 'reason': 'QUIT'})
            continue
        if exclude_hk and is_hk:
            excluded.append({'code': code, 'name': name, 'reason': 'HK'})
            continue
        filtered.append(code)

    # 2026-09-16 C1: info 缺失占比统计 — TDX 整挂时全池静默不过滤 (fail-open
    # 无声, 历史 C5 事故同型), 超 5% 必须显式告警
    if codes and n_missing / len(codes) > _INFO_MISSING_WARN_RATIO:
        logger.warning(
            "ST/退市信息缺失占比 %.1f%% (%d/%d), 过滤可能失效 "
            "(缺失股票已保守保留; TDX 连接异常?)",
            100.0 * n_missing / len(codes), n_missing, len(codes))

    return filtered, excluded


# 便捷: 模块级缓存 (复用 TDX 连接, 避免重复 get_stock_info)
# 治理III W3-get_or: 补 TTL (24h, 与 data_cache SECTOR_TTL 同频) ——
# ST 标记日频更新, 24h 内不重查足够; auto_iter 等预填 (setdefault 无 ts)
# 首次命中时盖当前时间戳, 24h TTL 同样生效 (2026-09-16 P2, 原 ts=None 恒新鲜)。
_INFO_CACHE: Dict[str, dict] = {}
_INFO_TS: Dict[str, float] = {}


def get_cached_info(code: str) -> dict:
    """获取单只股票信息 (带进程级缓存 + 24h TTL, 治理III W3-get_or)。"""
    now = time.time()
    ts = _INFO_TS.get(code)
    if code in _INFO_CACHE:
        if ts is None:
            # 2026-09-16 P2: 预填条目 (auto_iter setdefault 无 ts) 首次命中时
            # 盖上当前时间戳 — 原 ts=None 恒新鲜, 24h TTL 对预填永不生效
            _INFO_TS[code] = now
            return _INFO_CACHE[code]
        if now - ts <= _STOCK_INFO_TTL_SEC:
            return _INFO_CACHE[code]
    info = _get_info_safe(code)
    if info:
        _INFO_CACHE[code] = info
        _INFO_TS[code] = now
    return info


# === 自检 ===
if __name__ == '__main__':
    from core.data_fetcher import DataFetcher
    print('=' * 60)
    print('  core.stock_filter 自检')
    print('=' * 60)
    TdxConnector.ensure_connected()
    codes = DataFetcher.get_stock_universe('50')
    print(f'原始股票池: {len(codes)} 只')
    t0 = time.time()
    filtered, excluded = filter_stocks(codes[:50])  # 测前 50 只
    print(f'前 50 只过滤后: {len(filtered)} 只 (用时 {time.time()-t0:.1f}s)')
    print(f'被过滤: {len(excluded)} 个')
    for x in excluded[:5]:
        print(f'  - {x["code"]} {x["name"]} → {x["reason"]}')
    TdxConnector.close()
