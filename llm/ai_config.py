# -*- coding: utf-8 -*-
"""llm/ai_config.py — 对话大脑 AI 接入配置 (config/ai.json)。

背景 (2026-09-06 用户要求): 新增独立页签「AI 设置」, 让用户在界面上
配置 VERA 对话大脑三档 (快速/标准/深度) 各自的 API Key + 模型, 不用
改 .env / ~/.claude / DSH settings。

设计:
- 单文件 config/ai.json (*.json 已在 .gitignore, Key 不入库, 半密钥惯例)。
- 热生效: 无内存缓存, 调用方每次读文件 (三档每问一次读盘, 开销微秒级,
  换取"保存即生效、不用重启"的体验)。
- 松耦合: 文件不存在/损坏 → load 返 {}; section() 返 None; 调用方回落
  各自默认 (.env deepseek / ~/.claude / DSH settings 现状)。
- 打码: 前端只拿 mask, 永不明文回传; 保存时 Key 留空 = 保留旧值。

结构与三档协议 (各按各的协议, 铁律见 quick_chat/claude_cli/dsh_channel):
    fast:      {base_url, api_key, model}   OpenAI 兼容 /chat/completions
    standard:  {base_url, api_key, model}   Anthropic 兼容 (claude CLI 端点)
    deep:      {provider, model}            DSH 适配器注册名 + 模型

公开接口 (≤8):
- path()                  → config/ai.json 路径
- load(path?)             → dict (损坏返 {})
- save(cfg, path?)        → 写盘 (调用方保证字段形状)
- section(name)           → 某档 dict | None (无文件/该档空返 None)
- mask_key(key)           → 打码 (前4+****+后4; 短 key 全 ****)
"""
from __future__ import annotations

import json
from pathlib import Path

from utils.logger import get_logger
from utils.sysutil import project_root

logger = get_logger(__name__)

_AI_CONFIG = project_root() / "config" / "ai.json"


def path() -> Path:
    """config/ai.json 路径 (测试可 monkeypatch 默认)。"""
    return _AI_CONFIG


def load(path_: Path | None = None) -> dict:
    """读全量配置; 不存在/损坏/任何异常返 {} (松耦合, 不抛)。

    path_ 省略时走 path() (默认 config/ai.json) — 测试 monkeypatch path()
    即可整链隔离到 tmp, 不碰生产文件。
    """
    p = path_ or path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"ai_config 读取失败(回落空配置): {e}")
        return {}


def save(cfg: dict, path_: Path | None = None) -> None:
    """全量写盘 (调用方保证形状); 写失败抛, 由路由层兜底。"""
    p = path_ or path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def section(name: str, path_: Path | None = None) -> dict | None:
    """某档配置 dict | None (缺档/空 dict → None, 调用方回落默认)。"""
    cfg = load(path_)
    sec = cfg.get(name)
    return sec if isinstance(sec, dict) and sec else None


# 各档字段形状 (唯一真相源: 合并/视图都依此遍历, 杜绝手写三份漂移)
_FIELDS = {
    "fast": ("base_url", "api_key", "model"),
    "standard": ("base_url", "api_key", "model"),
    "deep": ("provider", "model"),
}


def merge_patch(old: dict, patch: dict) -> dict:
    """把前端保存 patch 合并进现有配置 (纯函数, 不写盘)。

    语义 (三档统一, 防漂移):
    - patch 只更新出现的档; 没出现的档原样保留。
    - 档内字段: 非空覆盖; 空串/None → 保留旧值 (Key 留空不误清)。
    - 档内带 __clear__: true → 整档清空 (回落默认, 前端"清空本档"按钮)。

    返完整可写盘 dict (三档齐全); old 形状不齐也能收敛。
    """
    old = old if isinstance(old, dict) else {}
    patch = patch if isinstance(patch, dict) else {}
    new = {}
    for s, fields in _FIELDS.items():
        prev = old.get(s) if isinstance(old.get(s), dict) else {}
        p = patch.get(s) if isinstance(patch.get(s), dict) else {}
        # 仅严格布尔 True 触发清空 (防 "false" 字符串 truthy 误清)
        if p.get("__clear__") is True:
            new[s] = {f: "" for f in fields}
            continue
        new[s] = {
            f: (p.get(f) if p.get(f) else prev.get(f, ""))
            for f in fields
        }
    return new


def mask_key(key: str | None) -> str:
    """Key 打码: 前4+****+后4; 空/短 (<9) 全 **** (前端展示用)。"""
    if not key:
        return ""
    if len(key) < 9:
        return "****"
    return key[:4] + "****" + key[-4:]
