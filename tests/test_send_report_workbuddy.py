"""tools/send_report_workbuddy.py 的单元测试(纯函数部分, 不碰真实 WorkBuddy 进程/网络)。

红绿纪律: 先写测试看红, 再实现看绿。
"""
import base64
import hashlib

import pytest


def test_extract_bearer_token_from_cmdline():
    from tools.send_report_workbuddy import extract_bearer_token

    fake = (
        '"D:\\Program Files\\WorkBuddy\\WorkBuddy.exe" codebuddy --serve '
        '--mcp-config "{\\"mcpServers\\":{\\"connector-proxy\\":{\\"url\\":'
        '\\"http://127.0.0.1:64079/mcp\\",\\"headers\\":{\\"Authorization\\":'
        '\\"Bearer abc123-_XYZ\\"}}}}" --port 0'
    )
    assert extract_bearer_token(fake) == "abc123-_XYZ"


def test_extract_bearer_token_missing_raises():
    from tools.send_report_workbuddy import extract_bearer_token

    with pytest.raises(RuntimeError):
        extract_bearer_token("codebuddy --serve --port 0")


def test_build_attachment_fields():
    from tools.send_report_workbuddy import build_attachment

    raw = b"<html>demo</html>"
    att = build_attachment("demo.html", raw, content_type="text/html")
    assert att["filename"] == "demo.html"
    assert att["content_type"] == "text/html"
    assert base64.b64decode(att["content"]) == raw
    assert att["size"] == len(raw)
    assert att["sha1"] == hashlib.sha1(raw).hexdigest()


def test_build_send_message_args_html():
    from tools.send_report_workbuddy import build_send_message_args

    args = build_send_message_args(
        to_email="me@agent.mail",
        subject="演示报告",
        html="<h1>hi</h1>",
        attachments=[{"filename": "a.html"}],
    )
    assert args["to"] == [{"email": "me@agent.mail"}]
    assert args["subject"] == "演示报告"
    assert args["body"] == "<h1>hi</h1>"
    assert args["body_format"] == "HTML"
    assert args["attachments"][0]["filename"] == "a.html"


def test_classify_getme_deactivated():
    from tools.send_report_workbuddy import MailboxDeactivatedError, raise_for_getme

    with pytest.raises(MailboxDeactivatedError):
        raise_for_getme("Agent 邮箱主账号已注销。请引导用户点击 workbuddy://agentmail 前往")
    # 正常返回(含邮箱地址)不抛错
    raise_for_getme('{"aliases": [{"email": "x@agent.mail", "alias_id": "alias_1"}]}')


def test_pick_primary_alias():
    from tools.send_report_workbuddy import pick_primary_alias

    text = '{"aliases": [{"email": "a@agent.mail", "alias_id": "alias_1", "is_primary": true}]}'
    assert pick_primary_alias(text) == "a@agent.mail"


def test_send_maps_getme_iserror_to_deactivated(tmp_path, monkeypatch):
    """GetMe 走 isError 通道返回『已注销』时, send_html_report 要抛人话异常。"""
    import tools.send_report_workbuddy as m

    class FakeMcp:
        def __init__(self):
            pass

        def call_tool(self, name, args=None):
            raise RuntimeError("agent-mail_GetMe 报错: Agent 邮箱主账号已注销。"
                               "请引导用户点击 workbuddy://agentmail 重新开通")

    monkeypatch.setattr(m, "WorkBuddyMcp", FakeMcp)
    html = tmp_path / "r.html"
    html.write_text("<html></html>", encoding="utf-8")
    with pytest.raises(m.MailboxDeactivatedError):
        m.send_html_report(html)
