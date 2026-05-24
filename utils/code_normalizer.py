"""股票代码标准化工具 — 确保代码格式为 6位数字.后缀。"""

import re
from typing import List, Optional

# 后缀映射：常见简写 → 标准后缀
SUFFIX_ALIAS = {
    "SH": "SH", "SZ": "SZ", "BJ": "BJ",
    "上": "SH", "深": "SZ", "京": "BJ",
    "上证": "SH", "深证": "SZ", "北证": "BJ",
}

# A 股代码前缀 → 默认后缀
_PREFIX_SUFFIX = {
    "6": "SH",  # 上海主板
    "5": "SH",
    "9": "SH",
    "0": "SZ",  # 深圳主板
    "2": "SZ",
    "3": "SZ",  # 创业板
    "8": "BJ",  # 北交所
    "4": "BJ",
}

CODE_PATTERN = re.compile(r"^(\d{6})(?:[\.\s]*(\w+))?$")


def normalize(code: str) -> Optional[str]:
    """
    将多种代码写法标准化为 'XXXXXX.SH/SZ/BJ' 格式。

    支持: '600519', '600519.SH', '600519 SH', '000001', '300750.SZ'
    """
    if not isinstance(code, str):
        return None
    code = code.strip().upper()
    if not code:
        return None

    # 已有标准格式
    m = CODE_PATTERN.match(code)
    if not m:
        return None

    digits = m.group(1)
    suffix = m.group(2)

    if suffix:
        suffix = SUFFIX_ALIAS.get(suffix, suffix)
        return f"{digits}.{suffix}"

    # 无后缀时自动推断
    first = digits[0]
    if first in _PREFIX_SUFFIX:
        return f"{digits}.{_PREFIX_SUFFIX[first]}"
    return None


def normalize_list(codes: List[str]) -> List[str]:
    """批量标准化，丢弃无法识别的代码并去重。"""
    result = []
    seen = set()
    for c in codes:
        nc = normalize(c)
        if nc and nc not in seen:
            seen.add(nc)
            result.append(nc)
    return result


def to_market_format(codes: List[str]) -> str:
    """
    将标准代码列表转换为通达信 TQ API 的市场#代码格式。
    例如: ['600519.SH', '000001.SZ'] → '1#600519|0#000001'
    """
    suffix_map = {"SH": "1", "SZ": "0", "BJ": "2"}
    parts = []
    for c in codes:
        if "." in c:
            code, suffix = c.split(".", 1)
            num = suffix_map.get(suffix.upper())
            if num:
                parts.append(f"{num}#{code}")
            else:
                import logging
                logging.getLogger("VERA").warning(f"to_market_format: 无法识别的后缀 {suffix} (code={c})")
    return "|".join(parts)


def get_market(code: str) -> Optional[str]:
    """获取单个代码的市场后缀。"""
    nc = normalize(code)
    if nc and "." in nc:
        return nc.split(".")[-1]
    return None
