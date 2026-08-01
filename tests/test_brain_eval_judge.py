"""tests/test_brain_eval_judge.py — brain/eval_judge.py 单元测试。"""
from __future__ import annotations

from brain.eval_judge import parse_judge_output


class TestParseJudgeOutput:
    def test_干净JSON(self):
        text = '{"accuracy":4,"completeness":3,"citation":5,"counter":4,"clarity":4,"reason":"ok"}'
        obj = parse_judge_output(text)
        assert obj is not None
        assert obj["accuracy"] == 4
        assert obj["counter"] == 4

    def test_带围栏(self):
        text = '```json\n{"accuracy":5,"completeness":5,"citation":4,"counter":3,"clarity":5,"reason":"excellent"}```'
        obj = parse_judge_output(text)
        assert obj is not None
        assert obj["completeness"] == 5

    def test_带杂波(self):
        text = '分析如下：\n这是评分。\n{"accuracy":3,"completeness":4,"citation":2,"counter":3,"clarity":4,"reason":"一般"}'
        obj = parse_judge_output(text)
        assert obj is not None
        assert obj["accuracy"] == 3

    def test_缺维度返None(self):
        text = '{"accuracy":4,"completeness":3,"citation":5}'
        assert parse_judge_output(text) is None

    def test_越界返None(self):
        text = '{"accuracy":6,"completeness":3,"citation":4,"counter":4,"clarity":4,"reason":"越界"}'
        assert parse_judge_output(text) is None

    def test_空文本返None(self):
        assert parse_judge_output("") is None
        assert parse_judge_output("hello world") is None

    def test_非JSON返None(self):
        assert parse_judge_output("不是JSON") is None


class TestJudgeAnswer:
    def test_CLI缺失(self, monkeypatch):
        """_find_cli 返 None 时 judge_answer 返 error 不抛。"""
        from brain.eval_judge import judge_answer
        monkeypatch.setattr("brain.claude_cli._find_cli", lambda: None)
        result = judge_answer("问题", "回答")
        assert "error" in result
