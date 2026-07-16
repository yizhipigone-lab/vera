"""Web 首次访问默认值与 localStorage 向后兼容契约。"""

from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InputValueParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        attributes = dict(attrs)
        input_id = attributes.get("id")
        if input_id:
            self.values[input_id] = attributes.get("value")


def _input_values() -> dict:
    parser = InputValueParser()
    parser.feed((PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8"))
    return parser.values


def test_html_trailing_inputs_match_default_yaml_percentages():
    values = _input_values()
    assert values["cfgTrailingAct"] == "3.5"
    assert values["cfgTrailingDD"] == "1"


def test_html_trailing_summary_matches_input_defaults():
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="sumTrailing"' in html
    assert "盈利 <b>3.5%</b> 激活后" in html
    assert "回撤 <b>1%</b> 触发全仓卖出" in html


def test_local_storage_contract_keys_remain_unchanged():
    script = (PROJECT_ROOT / "web" / "vera-ui.js").read_text(encoding="utf-8")
    assert "const STORAGE_KEY = 'vera_all_config';" in script
    assert "'cfgTrailingAct'" in script
    assert "'cfgTrailingDD'" in script