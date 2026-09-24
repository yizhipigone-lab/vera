"""tools/send_report_feishu.py — 把 markdown 报告转成飞书卡片推送 (2026-08-23)。

用法: python tools/send_report_feishu.py <md路径> [标题]
webhook 读 .env 的 FEISHU_WEBHOOK_URL (半密钥, 不打印)。
飞书自定义机器人卡片只支持 lark_md (无表格/无图片/无HTML), 转换规则:
  # 标题 → 加粗行; 表格 → "a ｜ b" 文字行; 代码围栏/ HTML 标签 → 去除。
长报告按 "## " 章节拆成多张卡片 (单卡片内容控制在 ~8KB UTF-8 内)。
"""
import json, os, re, sys, urllib.request

def load_webhook():
    # 2026-09-15 审计收口: 手写 .env 解析 → load_dotenv (与全项目惯例一致)
    from dotenv import load_dotenv
    load_dotenv()
    url = os.environ.get("FEISHU_WEBHOOK_URL")
    if not url:
        sys.exit("找不到 FEISHU_WEBHOOK_URL (.env 或环境变量)")
    return url

def md_to_lark(text: str) -> str:
    out = []
    in_code = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append("    " + line)  # 代码缩进为纯文本
            continue
        line = re.sub(r"<[^>]+>", "", line)              # 去 HTML 标签
        line = re.sub(r"^#{1,4}\s*", "", line)           # 标题 → 正文
        line = re.sub(r"^>\s?", "", line)                # 引用符
        line = re.sub(r"^(\s*)[-*]\s+", r"\1· ", line)   # 列表符号统一
        if re.match(r"^\|[\s\-|]+\|$", line.strip()):    # 表格分隔行丢弃
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            line = " ｜ ".join(cells)
        line = line.replace("**", "**")                  # lark_md 支持 **粗体**
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def chunk_sections(lark: str, limit=8000):
    """按空行段落聚合, 每片 ≤ limit 字节 (UTF-8)。"""
    paras, chunks, cur = lark.split("\n\n"), [], ""
    for p in paras:
        cand = (cur + "\n\n" + p) if cur else p
        if len(cand.encode("utf-8")) > limit and cur:
            chunks.append(cur); cur = p
        else:
            cur = cand
    if cur:
        chunks.append(cur)
    return chunks

def send_card(webhook: str, title: str, body: str, idx: int, total: int):
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": f"{title} ({idx}/{total})" if total > 1 else title},
                       "template": "blue"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
        },
    }
    req = urllib.request.Request(
        webhook, data=json.dumps(card).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        r = json.loads(resp.read().decode("utf-8"))
    if r.get("code") not in (0, None) or r.get("StatusCode") not in (0, None):
        raise RuntimeError(f"飞书返回异常: {r}")
    return r

def push_markdown(md_text: str, title: str) -> dict:
    """MD 文本 → 飞书卡片（自动分片）。**fail-soft，永不抛**。

    2026-09-20 收口：此前「取 webhook → md_to_lark → chunk → 逐卡 send_card」
    这段编排在 4 处逐字重复（notes_gen/daily.push_review、notes_gen/morning.push_brief、
    core/market_thermometer.push_thermometer、本文件 main）。本函数是**唯一实现**，
    行为与原四处逐条一致（含两种失败 reason 文案）。
    调度器 job 不该因为推送失败而中断，故此处绝不抛。

    返 {"ok": True, "cards": n, "codes": [...]}
    或 {"ok": False, "reason": "..."}。
    """
    try:
        try:
            webhook = load_webhook()
        except SystemExit:
            # load_webhook 未配 webhook 时 sys.exit —— 文案与既有调用方一致
            return {"ok": False, "reason": "未配置 FEISHU_WEBHOOK_URL (.env)"}
        chunks = chunk_sections(md_to_lark(md_text))
        codes = []
        for i, c in enumerate(chunks, 1):
            rr = send_card(webhook, title, c, i, len(chunks))
            codes.append(rr.get("code", rr.get("StatusCode")))
        return {"ok": True, "cards": len(chunks), "codes": codes}
    except Exception as e:
        return {"ok": False, "reason": f"飞书推送器不可用: {e}"}


def main():
    md_path = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else os.path.basename(md_path)
    r = push_markdown(open(md_path, encoding="utf-8").read(), title)
    if not r.get("ok"):
        sys.exit(f"推送失败: {r.get('reason')}")
    print(f"已发送 {r['cards']} 张卡片, code={r['codes']}")
    print("全部发送完成")

if __name__ == "__main__":
    main()
