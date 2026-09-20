# -*- coding: utf-8 -*-
"""tools/deploy_snapshot.py — 部署快照 + tag (2026-09-20 脆弱期 P0 修复 item 9)。

回答的问题: **出事故时, "当时跑的是哪一版代码"**。
背景: 本项目的部署形态是"工作区即生产"—— 实跑的是 feature 分支 + 一堆
未提交改动; git tag 只能标"基于哪个提交", 标不了 dirty 的真身。所以快照的
主角是 patch 与文件清单, tag 只是锚点。

用法:
    python tools/deploy_snapshot.py            # 只写快照 (无副作用, 仅新增文件)
    python tools/deploy_snapshot.py --tag      # 快照 + 打附注 tag deploy-<ts>

上线三步: 跑快照 → 重启受影响进程 → --tag。

产出: output/deploy_snapshots/<YYYYmmdd-HHMMSS>/
    changes.patch   git diff HEAD (已跟踪文件的全部未提交改动, 可 git apply)
    new_files/      未跟踪文件按相对路径原样拷贝 (单文件 >10MB 不拷内容,
                    只在 manifest 记路径+sha256 —— 防把数据/图片拷爆)
    manifest.txt    时间 / 分支 / HEAD sha+subject / git status 全文 /
                    每个源文件 sha256

零依赖 (纯 stdlib + git CLI)。exit 0=成功; 1=git 失败; 2=自验失败。
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIG_FILE_BYTES = 10 * 1024 * 1024  # >10MB 不拷内容, 只记哈希


def _git(*args: str, text: bool = True):
    """跑 git, 失败抛。text=False 拿 bytes (处理 -z 的 NUL 分隔)。"""
    kwargs = {"capture_output": True}
    if text:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    # 注意: text=False 时绝不能传 errors/encoding —— 任一存在都会
    # 隐式打开文本模式 (subprocess 文档), 输出变 str 而非 bytes。
    r = subprocess.run(["git", *args], cwd=ROOT, **kwargs)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr}")
    return r.stdout


def _untracked_files() -> list[Path]:
    """未跟踪文件清单。-z: NUL 分隔且关闭 quotepath 转义 (中文路径安全);
    git 对全未跟踪目录只报 dir/, 须递归展开 (逐行审查 P2-3)。"""
    raw = _git("status", "--porcelain", "-z", text=False)
    out: list[Path] = []
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        if entry[:2] != b"??":
            continue
        p = entry[3:].decode("utf-8", errors="replace")
        full = ROOT / p
        if full.is_dir():
            out.extend(f for f in full.rglob("*") if f.is_file())
        elif full.is_file():
            out.append(full)
    return sorted(out)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_snapshot(tag: bool = False) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "output" / "deploy_snapshots" / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    head_sha = _git("rev-parse", "HEAD").strip()
    branch = _git("branch", "--show-current").strip() or "(detached)"
    subject = _git("log", "-1", "--format=%s").strip()
    status_text = _git("status", "--porcelain")
    diff = _git("diff", "HEAD")
    untracked = _untracked_files()

    (out_dir / "changes.patch").write_text(diff, encoding="utf-8")

    new_files_dir = out_dir / "new_files"
    new_files_dir.mkdir(exist_ok=True)
    big: list[tuple[str, str]] = []   # (路径, sha256) 太大不拷内容
    copied = 0
    for f in untracked:
        rel = f.relative_to(ROOT)
        if f.stat().st_size > BIG_FILE_BYTES:
            big.append((str(rel), _sha256(f)))
            continue
        dst = new_files_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(f.read_bytes())
        copied += 1

    # manifest: 元信息 + status 全文 + 源文件哈希
    lines = [
        f"ts: {ts}",
        f"branch: {branch}",
        f"HEAD: {head_sha}",
        f"HEAD_subject: {subject}",
        f"dirty: {'yes' if status_text.strip() else 'no'}",
        "",
        "== git status --porcelain ==",
        status_text,
        "== 未跟踪文件哈希 (含 >10MB 未拷内容的) ==",
    ]
    hashed = set()
    for rel, sha in big:
        lines.append(f"{sha}  {rel}  (过大未拷)")
        hashed.add(rel)
    for f in untracked:
        rel = f.relative_to(ROOT)
        if rel in hashed:
            continue
        lines.append(f"{_sha256(f)}  {rel}")
    lines.append("")
    lines.append("== 已跟踪源文件哈希 ==")
    for p in _git("ls-files", "-z", text=False).split(b"\x00"):
        if not p:
            continue
        rel = Path(p.decode("utf-8", errors="replace"))
        full = ROOT / rel
        if full.is_file():
            lines.append(f"{_sha256(full)}  {rel}")
    (out_dir / "manifest.txt").write_text("\n".join(lines), encoding="utf-8")

    tag_name = ""
    if tag:
        tag_name = f"deploy-{ts}"
        _git("tag", "-a", tag_name, "-m",
             f"部署快照 {ts}\nHEAD: {head_sha}\n"
             f"dirty: {'yes' if status_text.strip() else 'no'}\n"
             f"快照: output/deploy_snapshots/{ts}\n"
             f"注意: tag 只标'基于哪个提交'; dirty 的真身在快照 patch 里。")

    # ── 自验 (四项链式检查, 计划书 §3.5) ──
    errors = []
    if head_sha != _git("rev-parse", "HEAD").strip():
        errors.append("manifest HEAD 与当前 HEAD 不一致")
    patch_lines = diff.count("\n")
    stat_lines = _git("diff", "HEAD", "--stat").count("\n")
    if (patch_lines > 0) != bool(diff.strip()):
        errors.append("changes.patch 内容自相矛盾")
    if patch_lines > 0 and "diff --git" not in diff:
        errors.append("changes.patch 缺 diff --git 头")
    if patch_lines > 0 and stat_lines == 0:
        errors.append("changes.patch 与 git diff --stat 不一致")
    copied_count = sum(1 for _ in new_files_dir.rglob("*") if _.is_file())
    expect_copy = len(untracked) - len(big)
    if copied_count != expect_copy:
        errors.append(f"new_files 拷贝数 {copied_count} != 预期 {expect_copy}")
    if "deploy_snapshots" in _git("status", "--porcelain"):
        errors.append("快照目录未被 gitignore (应落在 output/ 内)")

    print(f"快照: {out_dir}")
    print(f"  HEAD: {head_sha[:12]} ({branch}) dirty={'yes' if status_text.strip() else 'no'}")
    print(f"  changes.patch: {patch_lines} 行")
    print(f"  new_files: {copied_count} 拷 + {len(big)} 仅记哈希")
    if tag_name:
        print(f"  tag: {tag_name}")
    if errors:
        for e in errors:
            print(f"  [自验失败] {e}", file=sys.stderr)
        raise SystemExit(2)
    print("  自验: 4 项链式检查全过")
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="部署快照 + 可选 tag")
    ap.add_argument("--tag", action="store_true",
                    help="额外打附注 tag deploy-<ts> 指向 HEAD")
    args = ap.parse_args()
    try:
        build_snapshot(tag=args.tag)
    except RuntimeError as e:
        print(f"失败: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
