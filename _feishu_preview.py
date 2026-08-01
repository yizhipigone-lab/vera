"""一次性预览: 往真实飞书 webhook 发三条编数据的卡片 (买入/卖出/盘后日报)。
复用 trade.notifier 的卡片构建, 与生产环境一字不差。看完可删。"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trade.notifier import FeishuNotifier

WEBHOOK = os.environ.get("FEISHU_WEBHOOK_URL", "")
if not WEBHOOK:
    print("[FAIL] 请设置环境变量 FEISHU_WEBHOOK_URL")
    sys.exit(1)
n = FeishuNotifier(lambda: True, lambda: WEBHOOK)


def send(card: dict, label: str) -> None:
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"  [{label}] 飞书返回:", resp.read().decode("utf-8"))


def ts(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


print("[1/3] 买入卡")
send(n._build_fill_card({
    "code": "001301.SZ", "direction": 23, "price": 25.60, "qty": 100,
    "amount": 2560.00, "ts": ts("2026-07-29 14:54:51"), "label": "TDX买入",
}), "买入")

print("[2/3] 卖出卡")
send(n._build_fill_card({
    "code": "300996.SZ", "direction": 24, "price": 33.80, "qty": 100,
    "amount": 3380.00, "ts": ts("2026-07-30 09:31:01"), "label": "阶梯止盈",
    "pnl_pct": 11.36, "tier": 0, "sell_ratio": 0.30,
    "remaining_vol": 200, "remaining_value": 6760.00,
}), "卖出")

print("[3/3] 盘后日报卡")
send(n._build_daily_card({
    "total_asset": 1286450.80, "cash": 298796.48, "market_value": 987654.32,
    "day_pnl": 12340.56, "day_pnl_pct": 0.97, "position_count": 8,
    "ts": ts("2026-07-30 15:05:00"),
}), "日报")

print("[OK] 三条已投递")
