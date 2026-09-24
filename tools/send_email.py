"""tools/send_email.py — 任意文件作为邮件附件经 QQ SMTP 发出 (2026-08-24)。

独立能力(用户 2026-08-24 要求): 区别于
  - send_report_workbuddy.py  (WorkBuddy 本机 MCP → agent 信箱, 只能发给自己)
  - send_report_feishu.py    (飞书卡片)
本工具走标准 SMTP, 能发到**任意邮箱**。

用法:
  python tools/send_email.py <文件> [--to 收件人] [--subject 标题] [--inline]

配置(从 .env 读, 半密钥不打印):
  SMTP_USER      = 发件邮箱 (如 393328@qq.com)
  SMTP_PASSWORD  = 授权码 (QQ 邮箱对外 SMTP 用的是「授权码」, 不是登录密码)
  SMTP_HOST      = smtp.qq.com (默认)
  SMTP_PORT      = 587 (默认 STARTTLS; 连接失败自动回退 465 SSL)

默认收件人 = jayziheng@agent.qq.com (用户本人的 WorkBuddy agent 邮箱)。
"""
from __future__ import annotations

import argparse
import smtplib
import ssl
import sys
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

DEFAULT_TO = "jayziheng@agent.qq.com"
DEFAULT_HOST = "smtp.qq.com"
DEFAULT_PORT = 587


def _env(key: str) -> str | None:
    """读环境变量, 回退读项目根 .env 文件 (半密钥, 不打印)。"""
    import os
    v = os.environ.get(key)
    if v:
        return v.strip()
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_config() -> dict:
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    if not user or not password:
        sys.exit("缺 SMTP 配置: 请在 .env 写 SMTP_USER 和 SMTP_PASSWORD(QQ 授权码)")
    return {
        "user": user,
        "password": password,
        "host": _env("SMTP_HOST") or DEFAULT_HOST,
        "port": int(_env("SMTP_PORT") or DEFAULT_PORT),
    }


def build_msg(frm: str, to: str, subject: str, raw: bytes,
              filename: str, inline: bool) -> str:
    msg = MIMEMultipart()
    msg["From"] = formataddr(("VERA", frm))
    msg["To"] = to
    msg["Subject"] = subject
    if inline and filename.lower().endswith(".html"):
        body = raw.decode("utf-8", errors="replace")
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg.attach(MIMEText(f"来自 VERA 的投递, 完整内容见附件 {filename}(用浏览器打开)。",
                            "plain", "utf-8"))
    att = MIMEApplication(raw, _subtype="html" if filename.lower().endswith(".html") else "octet-stream")
    att.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(att)
    return msg.as_string()


def send(cfg: dict, to: str, subject: str, raw: bytes,
         filename: str, inline: bool) -> str:
    text = build_msg(cfg["user"], to, subject, raw, filename, inline)
    ctx = ssl.create_default_context()
    last_err: Exception | None = None
    # 首选配置端口 (587 STARTTLS), 失败回退 465 SSL
    attempts = [(cfg["port"], "starttls"), (465, "ssl")]
    for port, mode in attempts:
        try:
            if mode == "ssl":
                s = smtplib.SMTP_SSL(cfg["host"], port, context=ctx, timeout=30)
            else:
                s = smtplib.SMTP(cfg["host"], port, timeout=30)
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
            s.login(cfg["user"], cfg["password"])
            s.sendmail(cfg["user"], [to], text)
            s.quit()
            return f"{mode}:{port}"
        except smtplib.SMTPAuthenticationError as e:
            raise SystemExit(f"认证失败 (QQ SMTP 需「授权码」非登录密码): {e}")
        except Exception as e:
            last_err = e
            continue
    raise SystemExit(f"发送失败 (host={cfg['host']}): {last_err}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="经 QQ SMTP 把文件作为邮件附件发出")
    ap.add_argument("file", help="要发送的文件路径 (html/md/任意)")
    ap.add_argument("--to", default=DEFAULT_TO, help=f"收件人 (默认 {DEFAULT_TO})")
    ap.add_argument("--subject", default=None, help="邮件主题 (默认取文件名)")
    ap.add_argument("--inline", action="store_true",
                    help="HTML 文件同时作为正文 (默认只放附件)")
    args = ap.parse_args(argv)

    p = Path(args.file)
    if not p.exists():
        print(f"文件不存在: {p}", file=sys.stderr)
        return 2
    cfg = load_config()
    subject = args.subject or p.stem
    path = send(cfg, args.to, subject, p.read_bytes(), p.name, args.inline)
    print(f"已发送: {cfg['user']} -> {args.to}  [{subject}] ({path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
