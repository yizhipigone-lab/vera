# -*- coding: utf-8 -*-
"""utils.logger.attach_file_logger 单测 (2026-09-05 体检 P0-1).

覆盖: 挂 root 文件 handler 后各模块日志沿 propagate 落盘; 同文件重复
attach 幂等不双写; teardown 摘除 handler 不污染其他用例。
"""
import pytest

from utils.logger import attach_file_logger, get_logger


@pytest.fixture
def _attached(tmp_path, monkeypatch):
    log = tmp_path / "sched.log"
    fh = attach_file_logger(str(log))
    assert fh is not None
    yield log
    import logging
    logging.getLogger().removeHandler(fh)


def test_records_land_in_file(_attached):
    log = _attached
    logger = get_logger("scheduler.test_prop")
    logger.info("探测行 A")
    logger.warning("探测行 B")
    text = log.read_text(encoding="utf-8")
    assert "探测行 A" in text and "探测行 B" in text


def test_attach_idempotent_same_file(_attached, tmp_path):
    log = _attached
    # 同文件再 attach → 返回 None, 不加第二个 handler
    assert attach_file_logger(str(log)) is None
    logger = get_logger("scheduler.test_idem")
    logger.info("只写一次")
    text = log.read_text(encoding="utf-8")
    assert text.count("只写一次") == 1


def test_multi_child_loggers_no_duplicate(_attached):
    log = _attached
    # 不同子 logger 各写一条 → 文件各 1 条 (propagate 到 root 不会双写)
    get_logger("scheduler.child_a").info("A 记录")
    get_logger("scheduler.child_b").info("B 记录")
    text = log.read_text(encoding="utf-8")
    assert text.count("A 记录") == 1
    assert text.count("B 记录") == 1
