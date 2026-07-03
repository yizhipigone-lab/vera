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
import sys
import time
import warnings
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

warnings.filterwarnings('ignore')

from core.connector import TdxConnector


def _get_info_safe(code: str) -> dict:
    """单只股票信息获取，吞异常返回空 dict"""
    try:
        from tqcenter import tq
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
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for code, info in zip(codes, ex.map(_get_info_safe, codes)):
            if info:
                result[code] = info
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
    for code in codes:
        info = cache.get(code, {})
        if not info:
            # 拿不到信息 → 保守保留 (不误伤), 但记 warn
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

    return filtered, excluded


# 便捷: 模块级缓存 (复用 TDX 连接, 避免重复 get_stock_info)
_INFO_CACHE: Dict[str, dict] = {}


def get_cached_info(code: str) -> dict:
    """获取单只股票信息 (带进程级缓存)"""
    if code in _INFO_CACHE:
        return _INFO_CACHE[code]
    info = _get_info_safe(code)
    if info:
        _INFO_CACHE[code] = info
    return info


def clear_cache():
    """清空缓存 (切换股票池时调用)"""
    _INFO_CACHE.clear()


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