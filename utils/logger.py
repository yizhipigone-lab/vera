"""结构化日志模块。"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


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


def attach_file_logger(
    log_file: str,
    level: str = "INFO",
    max_mb: int = 100,
    backup_count: int = 5,
    fmt: str = "",
) -> logging.Handler | None:
    """给 root logger 挂文件输出 (独立进程落盘用, 如 scheduler)。

    各模块的 get_logger 只挂控制台 handler, 独立进程 (python -m scheduler)
    关窗即丢日志 —— 2026-09-05 体检 P0-1: 调度停摆/内部错误因此无从复查。
    调用本函数后, 所有 logger 的记录沿 propagate 一并写入该文件。

    幂等: 同一文件已挂则跳过 (进程内重复调用不双写)。只加文件不加控制台,
    避免与控制台 handler 重复打印。返回新 handler; 已存在返回 None。
    """
    if not fmt:
        fmt = "[%(asctime)s] [%(levelname)-7s] %(name)s | %(message)s"
    target = str(Path(log_file).resolve())
    root = logging.getLogger()
    for h in root.handlers:
        if (isinstance(h, RotatingFileHandler)
                and getattr(h, "baseFilename", "") == target):
            return None
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(str(path), maxBytes=max_mb * 1024 * 1024,
                             backupCount=backup_count, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    fh.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(fh)
    return fh
