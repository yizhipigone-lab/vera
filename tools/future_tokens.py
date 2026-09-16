# -*- coding: utf-8 -*-
"""未来函数黑名单 — 单一真相源 (2026-09-16 审计 F4 收口)。

此前同一黑名单手写三份且已漂移:
  - tools/formula_farm/static_vetting.py FUTURE_TOKENS (有 ZXNH/POLYLINE)
  - tools/formula_farm/common.py        L2_FUTURE_TOKENS (有 ZXNH)
  - tools/formula_pipeline/common.py    FUTURE_FUNCS (多 FLATZIGA/PEAKBARSA)

本模块 = 三份清单的**并集** (黑名单方向宁多勿漏, 更严=更安全)。
三处原常量名保留为 import 别名, 调用方不用改。

注意: 匹配方一律用词边界正则 (如 `\b` 或 `(?<![A-Z0-9_])`),
防止 ZIG 误伤 ZIGA、XMA 误伤 EXMA —— 清单加长后这点更不能省。
"""

FUTURE_TOKEN_BLACKLIST = [
    # ZIG 家族 (之字转向, 事后改历史路况)
    "ZIG", "ZIGA", "ZIGBARS", "FLATZIG", "FLATZIGA",
    # 峰谷家族 (事后才知道哪里是顶/底)
    "PEAK", "PEAKA", "PEAKBARS", "PEAKBARSA",
    "TROUGH", "TROUGHA", "TROUGHBARS",
    # 未来引用/回设
    "BACKSET", "REFX", "REFXV", "REFXR", "BARSNEXT",
    # 跨周期日线引用 (盘中取值会变)
    "DCLOSE", "DHIGH", "DLOW", "DOPEN", "DVOL",
    # 漂移画图/未来平滑
    "DRAWLINE", "POLYLINE", "XMA", "FFT",
    # 其它 (信号闪烁类)
    "ZXNH",
]
