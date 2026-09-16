"""tools/mail_to_corpus.py — 把邮箱里的外部研究产物落成 RAG 语料（2026-09-17, M5）。

做两件事，**分开写清**（计划书 §4.6.2 / §12.4）：

1. **调 `qqmail` 技能收信** —— 它只产出 `.eml` / `.txt` / `_summary.txt`，**不解析 zip 附件**；
2. **自己解 zip 取 `.md`** → 按内容哈希去重 → 落 `docs/research_inbox/`。

为什么单独一个脚本、不并进索引器：**索引器只管"读仓库里的文件"，不碰网络/邮箱**。
收信是"拉外部数据"，落盘后就是普通仓库文件，索引器视角完全统一。

**外部依赖必须显式声明**：`qqmail` 技能装在 `~/.dsh/skills/qqmail`，
**在仓库之外、绑定当前 Windows 用户**，换机器/换用户即失效。
技能缺失时本脚本**报错退出（非零）**，绝不静默产出空语料 ——
否则会把"收不到信"伪装成"没有新语料"，下一次 build 就把索引里那部分清空。

用法:
    python tools/mail_to_corpus.py                    # 调技能收信 → 落盘
    python tools/mail_to_corpus.py --from-dir DIR     # 用已收到的 .eml（不调技能, 可离线/可测）
    python tools/mail_to_corpus.py --limit 9 --json   # 只处理最新 9 封, 输出 JSON
"""
from __future__ import annotations

import argparse
import email
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VERA_ROOT = Path(__file__).resolve().parent.parent
#: 落盘根目录。与用户自己手写的 `research/` **物理分开**（§4.6.1）：
#: ①source 路径天然带来源，检索结果一眼能区分"自己研究的"还是"别人发来的"；
#: ②将来想整批清掉只需删一个目录。**已在 `docs` 根之下，索引自动覆盖。**
CORPUS_DIR = VERA_ROOT / "docs" / "research_inbox"
#: qqmail 技能的默认位置（外部依赖，见模块 docstring）
DEFAULT_SKILL_DIR = Path.home() / ".dsh" / "skills" / "qqmail"
#: 只收 .md：实测那批产物包里 html 体积是 md 的 34 倍且满是标签噪音，py/json 是源码与数据
WANTED_SUFFIX = ".md"
#: 单个 .md 小于这么多字节就不收（碎片/占位）
MIN_BYTES = 50


def find_skill(skill_dir: Path | None = None) -> Path | None:
    """定位 qqmail 技能目录；找不到返 None（调用方负责报错退出）。"""
    d = Path(skill_dir or DEFAULT_SKILL_DIR)
    return d if d.is_dir() else None


def receive_mail(skill_dir: Path, out_dir: Path, limit: int) -> int:
    """调技能的收信脚本，把 .eml 落到 out_dir → 返收到的封数。

    技能脚本名实测为 `Receive-QQMail.ps1`（PowerShell）。找不到脚本就抛
    （由 main 统一转成非零退出）。
    """
    ps1 = skill_dir / "Receive-QQMail.ps1"
    if not ps1.exists():
        cands = sorted(skill_dir.glob("*.ps1")) + sorted(skill_dir.glob("*.py"))
        if not cands:
            raise FileNotFoundError(f"技能目录里没有可执行的收信脚本: {skill_dir}")
        ps1 = cands[0]
    out_dir.mkdir(parents=True, exist_ok=True)
    if ps1.suffix.lower() == ".ps1":
        cmd = ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
               "-OutDir", str(out_dir), "-Limit", str(limit)]
    else:
        cmd = [sys.executable, str(ps1), "--out-dir", str(out_dir), "--limit", str(limit)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"收信脚本失败（{r.returncode}）: "
                           f"{(r.stderr or r.stdout or '')[-400:]}")
    return len(list(out_dir.glob("*.eml")))


def _decode_header(raw: str) -> str:
    """解 MIME 编码的邮件头（`=?utf-8?b?...?=` → 明文）。解不出来就原样返回。"""
    if not raw:
        return ""
    try:
        from email.header import decode_header
        parts = []
        for text, enc in decode_header(raw):
            if isinstance(text, bytes):
                parts.append(text.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(str(text))
        return "".join(parts).strip()
    except Exception:
        return str(raw).strip()


def _norm_date(raw: str) -> str:
    """邮件日期 → `YYYY-MM-DD`（拿不到就返空串，绝不编一个日期）。"""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", str(raw))
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def extract_md_from_eml(eml_path: Path) -> list[dict]:
    """一封 .eml → 其中的 `.md` 附件列表 `[{name, text, subject, sender, date, zip}]`。

    只看 zip 附件（实测产物包就是 zip），zip 内**只取 .md**，并保留原始相对路径。
    主题与日期都**解码成明文/规范日期**再落盘 —— 它们会进目录名、进而进 `source`
    与检索结果，留着 `=?utf-8?b?...?=` 那种乱码很难看也很难搜。
    """
    out: list[dict] = []
    try:
        msg = email.message_from_bytes(eml_path.read_bytes())
    except Exception:
        return out
    subject = _decode_header(str(msg.get("Subject") or ""))
    sender = _decode_header(str(msg.get("From") or ""))
    date = _norm_date(str(msg.get("Date") or ""))
    for part in msg.walk():
        fname = part.get_filename()
        if not fname:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        if not str(fname).lower().endswith(".zip"):
            continue
        try:
            import io
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    try:                       # zip 里的中文名可能是 cp437 编码
                        name = name.encode("cp437").decode("gbk")
                    except Exception:
                        pass
                    if not name.lower().endswith(WANTED_SUFFIX):
                        continue
                    if info.file_size < MIN_BYTES:
                        continue
                    raw = zf.read(info)
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        text = raw.decode("gbk", errors="replace")
                    out.append({"name": name, "text": text, "subject": subject,
                                "sender": sender, "date": date,
                                "zip": str(fname)})
        except Exception:
            continue
    return out


def _safe_segment(s: str, limit: int = 60) -> str:
    """把邮件主题变成安全的目录名片段（去非法字符、截断）。"""
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(s)).strip(" ._")
    s = re.sub(r"\s+", "_", s)
    return (s[:limit] or "无主题")


def _dedup_key(text: str) -> str:
    """内容哈希（同内容只留一份 → 同一篇文章不会挤占多个 top-k 名额，§4.6.1）。"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def write_corpus(items: list[dict], corpus_dir: Path | None = None) -> dict:
    """去重 + 落盘 → `{"written": [...], "dups": [...], "skipped": [...]}`。

    每篇顶部注入**来源元信息**（照 `_extract_file_prefix` 对 company 文件的既有做法）：
    检索片段自带出处，大脑引用时能写清"这是外部研报说的"，不会与用户自己的结论混淆。
    """
    base = Path(corpus_dir or CORPUS_DIR)
    base.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    written, dups = [], []
    for it in items:
        key = _dedup_key(it["text"])
        if key in seen:
            dups.append({"name": it["name"], "same_as": seen[key]})
            continue
        # 发件人**保留完整写法**（含邮箱地址）—— 地址才是"这是谁发来的"的凭据
        sender = (it.get("sender") or "").strip() or "未知发件人"
        folder = base / f"{it.get('date') or '日期未知'}_{_safe_segment(it.get('subject') or '')}"
        rel = Path(*[p for p in Path(it["name"]).parts if p not in (".", "..", "/")][-3:])
        dest = folder / rel
        header = (f"<!-- 来源：邮箱 {sender} ｜ 收到：{it.get('date')} ｜ "
                  f"原始附件：{it.get('zip')} ｜ 主题：{it.get('subject')} -->\n"
                  f"> **来源**：外部邮件（{sender}，{it.get('date')}）"
                  f"，原始附件 `{it.get('zip')}`。\n"
                  f"> **注意**：这是**外部**研究产物，**未经本系统复核**，"
                  f"引用时必须说明来源。\n\n")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(header + it["text"], encoding="utf-8")
        written.append(str(dest.relative_to(VERA_ROOT).as_posix()))
        seen[key] = str(dest.name)
    return {"written": written, "dups": dups}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python tools/mail_to_corpus.py",
                                 description="邮箱外部研究产物 → RAG 语料目录")
    ap.add_argument("--from-dir", default=None,
                    help="用已收到的 .eml 目录（不调技能，可离线/可测）")
    ap.add_argument("--skill-dir", default=None, help="qqmail 技能目录（默认 ~/.dsh/skills/qqmail）")
    ap.add_argument("--out", default=None, help="语料落盘目录（默认 docs/research_inbox）")
    ap.add_argument("--limit", type=int, default=20, help="最多收多少封")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    tmp: Path | None = None
    try:
        if args.from_dir:
            eml_dir = Path(args.from_dir)
            if not eml_dir.is_dir():
                print(f"【错】--from-dir 不是目录: {eml_dir}", file=sys.stderr)
                return 2
        else:
            skill = find_skill(Path(args.skill_dir) if args.skill_dir else None)
            if skill is None:
                print(f"【错】找不到 qqmail 技能目录: "
                      f"{args.skill_dir or DEFAULT_SKILL_DIR}\n"
                      "     这是**仓库之外、绑定当前 Windows 用户**的外部依赖；"
                      "换机器/换用户需先安装该技能，或用 --from-dir 指定已收到的 .eml 目录。",
                      file=sys.stderr)
                return 2
            tmp = Path(tempfile.mkdtemp(prefix="mail_corpus_"))
            try:
                n = receive_mail(skill, tmp, args.limit)
            except Exception as e:
                print(f"【错】收信失败: {e}", file=sys.stderr)
                return 3
            if n == 0:
                print("【错】技能没收到任何邮件（不是「没有新语料」，是收信这一步失败了）",
                      file=sys.stderr)
                return 3
            eml_dir = tmp

        emls = sorted(eml_dir.glob("*.eml"))[-max(1, args.limit):]
        if not emls:
            print(f"【错】{eml_dir} 里没有 .eml —— 不许静默产出空语料", file=sys.stderr)
            return 3
        items: list[dict] = []
        for e in emls:
            items.extend(extract_md_from_eml(e))
        if not items:
            print(f"【错】{len(emls)} 封邮件里一个 .md 附件都没解出来 "
                  "（附件格式变了？）—— 不许静默产出空语料", file=sys.stderr)
            return 3
        res = write_corpus(items, Path(args.out) if args.out else None)
        res["emls"] = len(emls)
        res["md_found"] = len(items)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"处理 {len(emls)} 封邮件，解出 {len(items)} 篇 .md；"
                  f"落盘 {len(res['written'])} 篇，去重丢掉 {len(res['dups'])} 篇")
            for w in res["written"][:10]:
                print(f"  + {w}")
        return 0
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
