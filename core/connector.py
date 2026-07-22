"""TDX 连接管理器 — 封装 TQ API 的初始化、健康检查和关闭。"""
import sys
import os
import threading
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

# TDX 安装路径 — 优先环境变量 TDX_HOME，否则用默认值
_TDX_PATH = os.environ.get("TDX_HOME", r"E:\NEW_TDX")
TQCENTER_PATH = os.path.join(_TDX_PATH, "PYPlugins", "user", "tqcenter.py")


class TdxConnector:
    """
    单例模式 TDX 连接管理器。

    设置环境变量 TDX_HOME 可覆盖通达信安装路径。

    用法:
        TdxConnector.initialize()
        if TdxConnector.is_ready():
            df = tq.get_market_data(...)
        TdxConnector.close()
    """

    _initialized: bool = False
    _lock = threading.Lock()

    @classmethod
    def initialize(cls) -> None:
        """初始化 TDX 连接。"""
        if cls._initialized:
            return

        with cls._lock:
            if cls._initialized:
                return

            pyplugins_user = str(Path(TQCENTER_PATH).parent)
            if pyplugins_user not in sys.path:
                sys.path.insert(0, pyplugins_user)

            try:
                from tqcenter import tq, tqconst
                cls._tq = tq
                cls._tqconst = tqconst

                tq.initialize(TQCENTER_PATH)
                cls._initialized = True
                logger.info("TDX 连接初始化成功")
            except SystemExit as e:
                logger.error(f"TDX 初始化失败（ErrorId=20）: {e}")
                cls._initialized = False
                raise
            except Exception as e:
                logger.error(f"TDX 连接初始化异常: {e}")
                cls._initialized = False

    @classmethod
    def is_ready(cls) -> bool:
        """检查连接是否就绪。P2-4: _tq 可能为 None (close 后清理)。"""
        if not cls._initialized:
            return False
        if cls._tq is None:
            return False
        try:
            return cls._tq._initialized
        except (AttributeError, NameError):
            return False

    @classmethod
    def close(cls) -> None:
        """断开 TDX 连接，清理模块缓存以支持下次重新初始化。

        P2-4 (2026-07-15): close 后仅设 _initialized=False 不够——
        Python import cache 会复用已 close 的 tq 模块, tq.initialize() 在脏状态上
        重初始化可能失败, 导致切周期(1d→5m→1d)后"通达信未打开"。
        现在 close 时同时:
        1. 清 _tq/_tqconst 引用
        2. 从 sys.modules 移除 tqcenter, 强制下次 initialize 做干净 import
        """
        if cls._initialized:
            try:
                cls._tq.close()
                logger.info("TDX 连接已关闭")
            except Exception as e:
                logger.warning(f"关闭 TDX 连接时出错: {e}")
            finally:
                cls._initialized = False
                cls._tq = None
                cls._tqconst = None
                # 强制下次 initialize 重新导入, 避免复用已 close 的模块
                for mod_name in ("tqcenter", "tqconst"):
                    sys.modules.pop(mod_name, None)

    @classmethod
    def ensure_connected(cls) -> None:
        """确保连接就绪, 掉线自动重连. 否则抛异常.

        2026-07-21 修: 原 initialize() 单例保护 (cls._initialized True 直接 return),
        但 tq._initialized 可能在长时间无活动/超时后变 False (连接掉), cls._initialized
        不同步 → 不重连 → RuntimeError "无法连接 TDX". 通达信进程其实开着.
        现在 is_ready False 时先 close 重置单例状态, 再 initialize 重连.
        """
        if cls.is_ready():
            return
        # tq._initialized False (连接掉), 重置单例状态 + 重连
        if cls._initialized:
            cls.close()
        cls.initialize()
        if not cls.is_ready():
            raise RuntimeError(
                "无法连接到 TDX。请确认：\n"
                "1. 通达信客户端已启动并登录\n"
                f"2. 通达信安装路径正确（当前: {_TDX_PATH}）\n"
                "3. TQ 策略框架版本兼容\n"
                "4. 可通过环境变量 TDX_HOME 修改安装路径"
            )

    @classmethod
    def get_data_dir(cls) -> str:
        return os.path.join(_TDX_PATH, "T0001")

    @classmethod
    def get_plugin_dir(cls) -> str:
        return os.path.join(_TDX_PATH, "PYPlugins")

    @classmethod
    def tq(cls):
        """返回 TQ API 对象（懒初始化，调用前确保已连接）。

        用法:
            TdxConnector.ensure_connected()
            tq_api = TdxConnector.tq()
            data = tq_api.get_market_data(...)

        等价于旧: from tqcenter import tq; tq.get_*(...)

        P2-4 (2026-07-15): _tq 可能因 close 被清为 None, ensure_connected 内部
        调 initialize 会重新赋值, 此处加防御断言。
        """
        cls.ensure_connected()
        if cls._tq is None:
            raise RuntimeError("TDX 连接异常: _tq 为 None, 请重启 server")
        return cls._tq
