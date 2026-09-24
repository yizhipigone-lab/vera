# -*- coding: utf-8 -*-
"""tools/qmt_ready_check.py — QMT 就绪探针 (供 start_vera.bat 启动 trade_main 前等待)。

2026-09-19 架构修订批次 1.1 (计划书 docs/plan/2026-09-19_架构审查修订计划书.md):
2026-09-02 冷启动事故的治本 —— miniQMT 登录初始化需 30 秒~2 分钟, 没就绪就启
trade_main 会在 connect() 拿到 rc=-1 崩溃。本脚本用与 trade_main **完全相同的
配置** (默认 config/trade.yaml) 做一次只读连接测试:
  退出码 0 → QMT 就绪; 非 0 → 未就绪 (调用方等待重试)。
只 connect + disconnect, 不查询、不下单、不写任何文件。

注意: stdout 只打印 ASCII —— 本脚本在 start_vera.bat 主窗口 (GBK 控制台) 里跑,
bat 顶部设了 PYTHONIOENCODING=utf-8, 打中文会乱码; 中文提示由 bat 自己 echo。

用法:
  python tools/qmt_ready_check.py [--config config/trade.yaml]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="QMT readiness probe (exit 0 = ready)")
    ap.add_argument("--config", default="config/trade.yaml",
                    help="与 trade_main 同一份配置文件")
    # 2026-09-20 审计 P3-5: trade_main 的 fake 判定有三条来源, 探针要一一对齐,
    # 否则"走 FakeGateway 却死等 QMT"("--fake" 这条原先探针不知道)。
    ap.add_argument("--fake", action="store_true",
                    help="与 trade_main --fake 对齐: 直连 FakeGateway, 无需 QMT")
    args = ap.parse_args()

    from trade.config import load_trade_config
    try:
        cfg = load_trade_config(args.config)
    except Exception as e:
        print(f"CONFIG-ERROR: {e}")
        return 2

    channel = getattr(cfg, "channel", "qmt")
    # 2026-09-20 审计 P3-5: fake 通道也要跳过 —— trade_main 的 fake 判定是
    # "--fake 参数 or 环境变量 VERA_TRADE_FAKE=1 or config.fake_sdk",
    # 而 config.channel 可能仍是 qmt (fake_sdk 不联动 channel) → 否则会
    # "走 FakeGateway 却死等 QMT", 交易进程根本起不来 (与 bat 注释矛盾)。
    if args.fake or channel != "qmt" or os.environ.get("VERA_TRADE_FAKE") == "1" \
            or getattr(cfg, "fake_sdk", False):
        print(f"NOT-QMT channel={channel} fake={bool(args.fake or getattr(cfg, 'fake_sdk', False))}"
              ", probe skipped")
        return 0
    if not cfg.account_id or not cfg.qmt_path:
        print("CONFIG-ERROR: account_id/qmt_path missing")
        return 2

    from trade.gateway import RealGateway
    gw = RealGateway(account_id=cfg.account_id, mini_qmt_path=cfg.qmt_path)
    try:
        ok = gw.connect()
        if not ok:
            print("NOT-READY: connect returned falsy")
            return 1
        # 2026-09-20 审计 P3-5: connect rc==0 只证明"连上了", 不证明"已登录/已订阅"
        # (gateway.connect 里 subscribe() 的返回值原本被丢弃)。加一步只读查询当
        # 就绪判据: 能读到资产 = 登录+订阅真的可用; 读不到 → 视为未就绪。
        asset = gw.query_asset()
        if not isinstance(asset, dict) or not asset:
            print(f"NOT-READY: connected but query_asset unusable ({asset!r})")
            return 1
        print(f"READY (asset keys={len(asset)})")
        return 0
    except Exception as e:
        print(f"NOT-READY: {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            gw.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
