"""结构化日志模块。"""

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime


def setup_logger(
    name: str = "VERA",
    level: str = "INFO",
    log_file: str = "",
    max_mb: int = 100,
    backup_count: int = 5,
    fmt: str = "",
) -> logging.Logger:
    """
    创建结构化 logger，同时输出到控制台和文件。

    Args:
        name: logger 名称
        level: 日志级别
        log_file: 日志文件路径（空则不写文件）
        max_mb: 单文件最大大小(MB)
        backup_count: 保留的备份文件数
        fmt: 自定义格式
    """
    if not fmt:
        fmt = "[%(asctime)s] [%(levelname)-7s] %(name)s | %(message)s"

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    # 控制台输出
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # 文件输出
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            str(path),
            maxBytes=max_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


def get_logger(name: str = "VERA") -> logging.Logger:
    """获取已存在的 logger，不存在则创建默认 logger。"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger = setup_logger(name)
    return logger
