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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="QMT readiness probe (exit 0 = ready)")
    ap.add_argument("--config", default="config/trade.yaml",
                    help="与 trade_main 同一份配置文件")
    args = ap.parse_args()

    from trade.config import load_trade_config
    try:
        cfg = load_trade_config(args.config)
    except Exception as e:
        print(f"CONFIG-ERROR: {e}")
        return 2

    channel = getattr(cfg, "channel", "qmt")
    if channel != "qmt":
        # fake/ths 通道没有"等 QMT 登录"这回事, 探针不适用, 直接放行
        print(f"NOT-QMT channel={channel}, probe skipped")
        return 0
    if not cfg.account_id or not cfg.qmt_path:
        print("CONFIG-ERROR: account_id/qmt_path missing")
        return 2

    from trade.gateway import RealGateway
    gw = RealGateway(account_id=cfg.account_id, mini_qmt_path=cfg.qmt_path)
    try:
        ok = gw.connect()
    except Exception as e:
        print(f"NOT-READY: {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            gw.disconnect()
        except Exception:
            pass
    print("READY" if ok else "NOT-READY: connect returned falsy")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
