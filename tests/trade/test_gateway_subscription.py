"""订阅治理测试 (2026-08-07): 关注集合 >50 切全推 subscribe_whole_quote。

背景: 官方文档建议单股订阅 ≤50, 超出建议全推; 0807 实盘 54 持仓 +
18 信号票 = 72 个逐票订阅, 当日出现订阅心跳超时降级。
锁定行为:
1. ≤50: 逐票订阅 (旧行为不变)
2. >50: 全推一次 + 退订全部逐票序号 (防双通道)
3. 全推回调 {code: tick} 单条形状兼容 + 按 _watch 过滤
4. unsubscribe_all 连全推序号一起退
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.gateway import RealGateway, _WHOLE_QUOTE_MARKETS


class _FakeXtdata:
    def __init__(self):
        self.per_code = []
        self.whole_calls = []
        self.unsubbed = []

    def subscribe_quote(self, code, period=None, count=0, callback=None):
        self.per_code.append(code)
        return 100 + len(self.per_code)

    def subscribe_whole_quote(self, markets, callback=None):
        self.whole_calls.append(list(markets))
        return 9000

    def unsubscribe_quote(self, seq):
        self.unsubbed.append(seq)

    def run(self):
        pass


def _make_gw():
    gw = RealGateway(account_id="T", timeout_sec=1.0)
    fake = _FakeXtdata()
    gw._xtdata = lambda: fake      # 测试接缝: 实例属性覆盖 lazy import
    return gw, fake


def test_under_threshold_per_code_subscription():
    gw, fake = _make_gw()
    codes = [f"6000{i:02d}.SH" for i in range(10)]
    assert gw.subscribe_quotes(codes)
    assert fake.per_code == codes
    assert fake.whole_calls == []              # 不触发全推


def test_over_threshold_switches_to_whole_quote():
    gw, fake = _make_gw()
    codes = [f"6000{i:02d}.SH" for i in range(51)]
    gw.subscribe_quotes(codes[:10])            # 先逐票订 10 只
    gw.subscribe_quotes(codes[10:])            # 补到 51 → 超阈值
    assert fake.whole_calls == [_WHOLE_QUOTE_MARKETS]
    assert len(fake.per_code) == 10            # 第二批没再逐票订
    # 既有 10 个逐票序号已退订 (防双通道重复推送)
    assert sorted(fake.unsubbed) == [100 + i for i in range(1, 11)]
    # 再次订阅: 全推已激活, 不重复建
    gw.subscribe_quotes(["000001.SZ"])
    assert fake.whole_calls == [_WHOLE_QUOTE_MARKETS]


def test_whole_mode_tick_filtered_by_watch():
    got = []
    gw, fake = _make_gw()
    gw._on_quote = lambda code, q: got.append((code, q))
    codes = [f"6000{i:02d}.SH" for i in range(51)]
    gw.subscribe_quotes(codes)
    tick = {"time": 1733118954000, "lastPrice": 11.39, "askPrice": [11.4],
            "bidPrice": [11.38], "high": 11.4, "low": 11.31, "lastClose": 11.38}
    # 全推形状: {code: 单条 dict}; 一只关注 (600000.SH 在 51 只内) + 一只未关注
    gw._on_tick({"600000.SH": dict(tick), "000001.SZ": dict(tick)})
    assert [c for c, _ in got] == ["600000.SH"]
    assert got[0][1]["last"] == 11.39          # 转换函数口径一致


def test_unsubscribe_all_covers_whole_seq():
    gw, fake = _make_gw()
    gw.subscribe_quotes([f"6000{i:02d}.SH" for i in range(51)])
    gw.unsubscribe_all()
    assert 9000 in fake.unsubbed
    assert gw._whole_seq is None
