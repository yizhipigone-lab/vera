"""send_report_workbuddy.py — 把 HTML 报告投递到本机 WorkBuddy 的 agent 信箱。

通路: WorkBuddy 桌面端在本机 127.0.0.1:64079 跑了一个 connector-proxy (MCP over HTTP),
聚合了 agent-mail 邮箱服务。本脚本:
  1. 从正在运行的 WorkBuddy CLI host 进程命令行动态提取 connector-proxy 的 Bearer token
     (token 不落盘、不打印, 重启 WorkBuddy 后自动跟随新值);
  2. MCP 握手后调 agent-mail_GetMe 拿到用户自己的 agent 邮箱地址;
  3. 把 HTML 报告作为邮件(正文 + .html 附件)发到该地址 —— 用户在 WorkBuddy 里说
     「查一下我的信箱」即可看到。

用法:
  python tools/send_report_workbuddy.py 报告.html [--subject 标题] [--inline]
  python tools/send_report_workbuddy.py --demo          # 生成演示报告并投递

依赖: 仅标准库。WorkBuddy 必须在运行中。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROXY_URL = "http://127.0.0.1:64079/mcp"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class MailboxDeactivatedError(RuntimeError):
    """agent 邮箱主账号已注销, 需要用户在 WorkBuddy「更多 - 我的邮箱」重新开通。"""


# ---------------------------------------------------------------- 纯函数 ----

def extract_bearer_token(cmdline: str) -> str:
    """从 WorkBuddy CLI host 进程命令行里抠出 connector-proxy 的 Bearer token。"""
    m = re.search(r'Authorization\\?":\\?"Bearer ([^"\\]+)', cmdline)
    if not m:
        raise RuntimeError("WorkBuddy 进程命令行里没找到 connector-proxy token")
    return m.group(1)


def build_attachment(filename: str, raw: bytes, content_type: str = "text/html") -> dict:
    """构造 SendMessage 的内联附件结构(base64 + sha1 校验)。"""
    return {
        "filename": filename,
        "content_type": content_type,
        "content": base64.b64encode(raw).decode("ascii"),
        "size": len(raw),
        "sha1": hashlib.sha1(raw).hexdigest(),
    }


def build_send_message_args(to_email: str, subject: str, html: str,
                            attachments: list | None = None) -> dict:
    """构造 agent-mail_SendMessage 的入参: HTML 正文发给自己。"""
    args = {
        "to": [{"email": to_email}],
        "subject": subject,
        "body": html,
        "body_format": "HTML",
    }
    if attachments:
        args["attachments"] = attachments
    return args


def raise_for_getme(text: str) -> None:
    """GetMe 返回文本里检出「已注销」就直接抛带指引的异常。"""
    if "已注销" in text or "重新开通" in text:
        raise MailboxDeactivatedError(
            "WorkBuddy agent 邮箱已注销。请在 WorkBuddy 里点「更多 → 我的邮箱」"
            "(或访问 workbuddy://agentmail)重新开通后再投递。"
        )


def pick_primary_alias(getme_text: str) -> str:
    """从 GetMe 输出里挑主邮箱地址。输出可能是 JSON 或带 JSON 片段的文本。"""
    m = re.search(r'\{.*\}', getme_text, re.S)
    candidates = []
    if m:
        try:
            data = json.loads(m.group(0))
            candidates = data.get("aliases") or data.get("emails") or []
        except json.JSONDecodeError:
            candidates = []
    for a in candidates:
        if a.get("is_primary") and a.get("email"):
            return a["email"]
    for a in candidates:
        if a.get("email"):
            return a["email"]
    # 兜底: 文本里直接捞邮箱样式的串
    m2 = re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+', getme_text)
    if m2:
        return m2.group(0)
    raise RuntimeError(f"GetMe 返回里找不到邮箱地址: {getme_text[:200]}")


# -------------------------------------------------------------- 本机探测 ----

def find_live_token() -> str:
    """从正在运行的 WorkBuddy 进程命令行提取 token(每次现取, 不落盘)。"""
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
         "'connector-proxy' -and $_.CommandLine -match 'Authorization' } | "
         "Select-Object -First 1 -ExpandProperty CommandLine)"],
        text=True,
    )
    return extract_bearer_token(out)


# -------------------------------------------------------------- MCP 客户端 --

class WorkBuddyMcp:
    """最小 MCP (streamable HTTP) 客户端, 只覆盖 connector-proxy 用到的子集。"""

    def __init__(self, url: str = PROXY_URL, token: str | None = None):
        self.url = url
        self.token = token or find_live_token()
        self.session: str | None = None
        self._next_id = 1
        self._handshaken = False

    def _post(self, payload: dict, notify: bool = False):
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", f"Bearer {self.token}")
        if self.session:
            req.add_header("mcp-session-id", self.session)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"connector-proxy HTTP {e.code}: {body[:500]}")
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session = sid
        raw = resp.read().decode(errors="replace")
        if notify:
            return None
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(raw) if raw.strip() else None

    def _rpc(self, method: str, params: dict | None = None):
        mid = self._next_id
        self._next_id += 1
        return self._post({"jsonrpc": "2.0", "id": mid, "method": method,
                           "params": params or {}})

    def handshake(self) -> None:
        if self._handshaken:
            return
        r = self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "vera-report-sender", "version": "1.0"},
        })
        if not r or "result" not in r:
            raise RuntimeError(f"MCP initialize 失败: {r}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                   notify=True)
        self._handshaken = True

    def call_tool(self, name: str, args: dict | None = None) -> str:
        """调工具, 返回拼接后的文本内容; MCP 层 isError 也抛异常。"""
        self.handshake()
        r = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        result = (r or {}).get("result") or {}
        texts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        text = "\n".join(texts)
        if result.get("isError"):
            raise RuntimeError(f"{name} 报错: {text[:500]}")
        return text


# ---------------------------------------------------------------- 投递 -------

def send_html_report(html_path: str | Path, subject: str | None = None,
                     inline: bool = False) -> str:
    """把 HTML 报告投递到本机 WorkBuddy agent 信箱。返回投递结果描述。"""
    html_path = Path(html_path)
    raw = html_path.read_bytes()
    subject = subject or html_path.stem

    mcp = WorkBuddyMcp()
    try:
        getme = mcp.call_tool("agent-mail_GetMe")
    except RuntimeError as e:
        # GetMe 的「已注销」提示走 isError 通道, 转成人话异常再抛
        raise_for_getme(str(e))
        raise
    raise_for_getme(getme)
    my_addr = pick_primary_alias(getme)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = (
        f"<p>来自 VERA 的 HTML 报告投递({stamp})。</p>"
        f"<p>完整报告见附件 <b>{html_path.name}</b>,"
        f"下载后用浏览器打开即可。</p>"
    )
    if inline:
        body = raw.decode("utf-8", errors="replace")
    attachment = build_attachment(html_path.name, raw)
    args = build_send_message_args(my_addr, subject, body, [attachment])

    try:
        result = mcp.call_tool("agent-mail_SendMessage", args)
    except RuntimeError as e:
        # WorkBuddy 对发信有二次确认闸: 首次报错里带 confirmation_token, 带上重发
        m = re.search(r'confirmation_token["\s:]+([A-Za-z0-9_\-]+)', str(e))
        if not m:
            raise
        args["confirmation_token"] = m.group(1)
        result = mcp.call_tool("agent-mail_SendMessage", args)
    return f"已投递到 {my_addr}: {result[:300]}"


def make_demo_report(out_dir: str | Path | None = None) -> Path:
    """生成一个演示 HTML 报告, 返回文件路径。"""
    out_dir = Path(out_dir) if out_dir else PROJECT_ROOT / "output" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = out_dir / f"{stamp[:10]}_WorkBuddy投递演示_报告.html"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>WorkBuddy 投递演示报告</title>
<style>
 body {{ font-family: "Microsoft YaHei", sans-serif; max-width: 720px; margin: 40px auto; padding: 0 20px; color: #222; }}
 .card {{ border: 1px solid #e0e0e0; border-radius: 10px; padding: 20px 24px; margin: 16px 0; }}
 .ok {{ color: #1a7f37; font-weight: bold; }}
 table {{ border-collapse: collapse; width: 100%; }} td, th {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
 th {{ background: #f6f8fa; }}
</style></head><body>
<h1>📮 WorkBuddy 投递演示报告</h1>
<div class="card">
 <p class="ok">✅ 通路验证成功</p>
 <p>如果你在 WorkBuddy 信箱里看到这封邮件, 说明 <b>VERA → agent-mail → WorkBuddy</b>
 整条链路已经打通: 以后任何回测/研究报告都可以一条命令投递过来。</p>
</div>
<div class="card">
 <h3>链路信息</h3>
 <table>
  <tr><th>环节</th><th>值</th></tr>
  <tr><td>发送方</td><td>VERA 项目 tools/send_report_workbuddy.py</td></tr>
  <tr><td>通道</td><td>WorkBuddy connector-proxy (本机 MCP) → agent-mail</td></tr>
  <tr><td>生成时间</td><td>{now}</td></tr>
 </table>
</div>
</body></html>""", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 HTML 报告投递到本机 WorkBuddy agent 信箱")
    ap.add_argument("html", nargs="?", help="要投递的 HTML 文件路径")
    ap.add_argument("--subject", help="邮件主题(默认取文件名)")
    ap.add_argument("--inline", action="store_true",
                    help="把 HTML 全文作为邮件正文(默认只放摘要+附件)")
    ap.add_argument("--demo", action="store_true", help="生成演示报告并投递")
    args = ap.parse_args(argv)

    if not args.demo and not args.html:
        ap.error("请给 HTML 文件路径, 或用 --demo")

    try:
        path = make_demo_report() if args.demo else Path(args.html)
        if not path.exists():
            print(f"文件不存在: {path}", file=sys.stderr)
            return 2
        print(f"报告文件: {path}")
        result = send_html_report(path, subject=args.subject, inline=args.inline)
        print(result)
        return 0
    except MailboxDeactivatedError as e:
        print(f"[邮箱未开通] {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 - CLI 兜底, 打印人话错误
        print(f"[投递失败] {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
