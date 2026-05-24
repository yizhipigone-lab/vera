"""管线状态管理器 — 记录运行状态，支持断点续跑。"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)


class StateManager:
    """记录管线运行状态。"""

    def __init__(self, strategy_name: str, state_dir: str = ""):
        if not state_dir:
            state_dir = str(Path(__file__).resolve().parents[1] / "output" / "logs")
        self.state_file = os.path.join(state_dir, f"{strategy_name}_state.json")
        self.state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"steps": {}, "last_full_run": None}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False, default=str)

    def mark_step(self, step_name: str, status: str, data: dict = None) -> None:
        """标记步骤状态。"""
        self.state["steps"][step_name] = {
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "data": data or {},
        }
        self._save()

    def get_last_completed_step(self) -> Optional[str]:
        """获取最后一次成功的步骤。"""
        completed = [
            name for name, info in self.state["steps"].items()
            if info.get("status") == "completed"
        ]
        return completed[-1] if completed else None

    def is_stale(self, max_age_hours: int = 24) -> bool:
        """检查上次完整运行是否过期。"""
        last = self.state.get("last_full_run")
        if not last:
            return True
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt) > timedelta(hours=max_age_hours)

    def mark_full_run(self) -> None:
        """标记一次完整运行。"""
        self.state["last_full_run"] = datetime.now().isoformat()
        self._save()
