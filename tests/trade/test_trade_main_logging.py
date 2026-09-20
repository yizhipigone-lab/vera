# -*- coding: utf-8 -*-
"""tests/trade/test_trade_main_logging.py — 交易进程文件日志 (2026-09-20 item 5)。

现状: trade_main 只有控制台日志 (trade_main.py:81 模块级 get_logger 不带
log_file), 关窗即丢 —— 实测 logs/trade_main_console.log 停在 2026-09-16 而
进程 09-20 在跑。修法: 只在 main() 里 attach_file_logger (scheduler 同款,
scheduler/__main__.py:381-382)。

为什么必须锁"仅 import 不挂 handler": import trade_main 在测试里真实存在
(tests/trade/test_mobile_contract.py:31)。模块级挂文件 handler = 跑一次
测试就往生产 output/logs/ 写文件 —— 2026-07-27 缓存投毒事故的同类。
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import trade_main  # noqa: E402
from trade.config import TradeConfig  # noqa: E402

TARGET = str((Path(trade_main.__file__).resolve().parent
              / "output" / "logs" / "trade_main.log").resolve())


def _has_target_handler() -> bool:
    return any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", "") == TARGET
        for h in logging.getLogger().handlers)


def test_仅import不挂文件handler():
    """模块级绝不挂文件 handler —— 否则 import 即污染生产 output/logs/。"""
    assert not _has_target_handler()


def test_main挂文件日志(monkeypatch, tmp_path):
    """main() 路径必须把 output/logs/trade_main.log 挂到 root logger。"""
    calls = []
    # raising=False: 实现前 trade_main 尚无该属性 —— 红应是 AssertionError
    # (calls==0, "功能不存在") 而非 AttributeError (打错字/导错名)。
    monkeypatch.setattr(trade_main, "attach_file_logger",
                        lambda *a, **kw: calls.append((a, kw)) or None,
                        raising=False)
    # 拦掉 main() 的全部副作用: 生产配置读取 / TradeApp / uvicorn
    monkeypatch.setattr(trade_main, "load_trade_config",
                        lambda _p: TradeConfig(account_id="T", fake_sdk=True))
    monkeypatch.setattr(trade_main.Path, "exists", lambda _self: False)

    class _FakeApp:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(trade_main, "TradeApp", _FakeApp)
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    monkeypatch.setattr("trade.api.create_api_app", lambda *a, **kw: object())
    monkeypatch.setattr(sys, "argv", ["trade_main.py", "--fake"])

    trade_main.main()

    assert len(calls) == 1, f"main() 应恰好挂一次文件日志, 实际 {len(calls)} 次"
    log_path = str(calls[0][0][0])
    assert Path(log_path).resolve() == Path(TARGET), (
        f"日志路径应是 {TARGET}, 实际 {log_path}")
    assert _has_target_handler() is False, (
        "测试里 attach_file_logger 被拦, root 不应真挂 handler")
