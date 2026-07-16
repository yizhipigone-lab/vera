"""tests/test_config_save.py — 前端配置保存到 YAML 的单测。

测 ConfigLoader 的 current_exists / load_current / save_current / delete_current。
全部用 pytest 的 tmp_path 隔离，绝不污染项目 config/ 目录。
合并测试依赖项目真实 config/default.yaml（靠 __file__ 定位，与 cwd 无关）。

用法: pytest tests/test_config_save.py -v
"""
import pytest
import yaml

from utils.config_loader import ConfigLoader


def test_roundtrip_basic(tmp_path):
    """save → load 关键字段往返一致（含小数档位、formula_sell）。"""
    p = tmp_path / "current.yaml"
    cfg = {
        "strategy": {"name": "回测"},
        "selection": {"formula_name": "UPN", "formula_arg": "3"},
        "stop_loss": {
            "priority": "trailing_first",
            "cost_stop": {"enabled": True, "threshold": -0.07},
            "ladder_tp": {"enabled": True,
                          "levels": [{"profit": 0.065, "sell_ratio": 0.3}]},
            "formula_sell": {"enabled": True, "formula_name": "卖出XG",
                             "sell_ratio": 0.4, "priority": 0},
        },
        "benchmark": {"indices": ["shanghai"]},
    }
    ConfigLoader.save_current(cfg, path=str(p))
    loaded = ConfigLoader.load_current(path=str(p))
    assert loaded is not None
    assert loaded["selection"]["formula_name"] == "UPN"
    assert loaded["stop_loss"]["cost_stop"]["threshold"] == -0.07
    # 小数档位后端原样存（前端 cleanNum 已防四舍五入）
    assert loaded["stop_loss"]["ladder_tp"]["levels"][0]["profit"] == 0.065
    assert loaded["stop_loss"]["formula_sell"]["enabled"] is True
    assert loaded["stop_loss"]["formula_sell"]["sell_ratio"] == 0.4


def test_roundtrip_chinese(tmp_path):
    """中文 strategy.name / formula_name 往返不乱码。"""
    p = tmp_path / "current.yaml"
    cfg = {"strategy": {"name": "我的策略"},
           "selection": {"formula_name": "通信解盘"}}
    ConfigLoader.save_current(cfg, path=str(p))
    loaded = ConfigLoader.load_current(path=str(p))
    assert loaded["strategy"]["name"] == "我的策略"
    assert loaded["selection"]["formula_name"] == "通信解盘"
    # 文件确实是 utf-8 编码
    assert "我的策略" in p.read_text(encoding="utf-8")


def test_load_merges_with_default(tmp_path):
    """load_current 与 default.yaml 合并：current 缺的字段走 default。"""
    p = tmp_path / "current.yaml"
    cfg = {"stop_loss": {"cost_stop": {"enabled": True, "threshold": -0.05}}}
    ConfigLoader.save_current(cfg, path=str(p))
    loaded = ConfigLoader.load_current(path=str(p))
    # current 里的字段覆盖
    assert loaded["stop_loss"]["cost_stop"]["threshold"] == -0.05
    # default 兜底（default.yaml 的 trailing_stop.activation = 0.035）
    assert "trailing_stop" in loaded["stop_loss"]
    assert loaded["stop_loss"]["trailing_stop"]["activation"] == 0.035


def test_load_missing_returns_none(tmp_path):
    """文件不存在：load_current 返回 None，current_exists 返回 False。"""
    p = tmp_path / "current.yaml"
    assert ConfigLoader.current_exists(path=str(p)) is False
    assert ConfigLoader.load_current(path=str(p)) is None


def test_save_backs_up_bak(tmp_path):
    """覆盖前复制 .bak；.bak 内容是上一版，current.yaml 是新版（原件完整）。"""
    p = tmp_path / "current.yaml"
    bak = tmp_path / "current.yaml.bak"
    ConfigLoader.save_current({"strategy": {"name": "第一版"}}, path=str(p))
    assert not bak.exists()                       # 首次保存无 .bak
    ConfigLoader.save_current({"strategy": {"name": "第二版"}}, path=str(p))
    assert bak.exists()                           # 第二次产生 .bak
    assert yaml.safe_load(bak.read_text(encoding="utf-8"))["strategy"]["name"] == "第一版"
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["strategy"]["name"] == "第二版"


def test_atomic_write_failure_preserves_original(tmp_path, monkeypatch):
    """写 tmp 阶段抛异常：tmp 清理 + 原 current.yaml 完整未破坏（copy2 复制不移动的保证）。"""
    p = tmp_path / "current.yaml"
    ConfigLoader.save_current({"strategy": {"name": "原件"}}, path=str(p))

    def boom(*a, **k):
        raise RuntimeError("模拟写失败")
    monkeypatch.setattr(yaml, "safe_dump", boom)

    with pytest.raises(RuntimeError):
        ConfigLoader.save_current({"strategy": {"name": "新版"}}, path=str(p))

    # 原 current.yaml 完整（copy2 复制不移动，写失败时原件不动）
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["strategy"]["name"] == "原件"
    # tmp 被清理（无残留 .tmp）
    assert list(tmp_path.glob(".current.yaml.*.tmp")) == []
    # .bak 已产生（备份步骤在写之前）
    assert (tmp_path / "current.yaml.bak").exists()


def test_delete_idempotent(tmp_path):
    """delete_current 幂等：存在删返回 True，再删返回 False。"""
    p = tmp_path / "current.yaml"
    ConfigLoader.save_current({"strategy": {"name": "x"}}, path=str(p))
    assert ConfigLoader.delete_current(path=str(p)) is True
    assert ConfigLoader.current_exists(path=str(p)) is False
    assert ConfigLoader.delete_current(path=str(p)) is False   # 再删不抛


def test_saved_file_has_header_and_lf(tmp_path):
    """落盘文件含顶部注释头 + LF 行尾（非 CRLF，避免 git 行尾噪音）。"""
    p = tmp_path / "current.yaml"
    ConfigLoader.save_current({"strategy": {"name": "x"}}, path=str(p))
    raw = p.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8").startswith("# VERA 前端保存的用户配置")


def test_roundtrip_preserves_explicit_trailing_values(tmp_path):
    """用户保存的 8%/5% 是显式策略值，不能被新默认覆盖。"""
    path = tmp_path / "current.yaml"
    config = {
        "stop_loss": {
            "trailing_stop": {
                "enabled": True,
                "activation": 0.08,
                "drawdown": 0.05,
            },
        },
    }

    ConfigLoader.save_current(config, path=str(path))
    loaded = ConfigLoader.load_current(path=str(path))

    assert loaded["stop_loss"]["trailing_stop"]["activation"] == 0.08
    assert loaded["stop_loss"]["trailing_stop"]["drawdown"] == 0.05
