"""DSH 深度思考通道 · 便携运行时部署脚本（ADR-0002）。

在 <repo>/dsh-runtime/ 下一键就位第二套 DSH（全部相对路径，真便携）：

    dsh-runtime/
    ├── node/node.exe    便携 Node（从本机复制，node.exe 单文件可运行）
    ├── app/             npm 安装的 @deepseek-ai/dsh（版本钉死 DSH_PIN_VERSION）
    ├── home/            第二套 DSH_HOME（凭据/配置/会话日志）
    ├── workspace/       子进程默认工作目录
    └── dsh.cmd          干净 shim（%~dp0 相对路径、无 cd，拷走即用）

用法：
    python scripts/setup_dsh_home.py            # dry-run：只打印计划，不动文件
    python scripts/setup_dsh_home.py --apply    # 实际部署
    python scripts/setup_dsh_home.py --apply --smoke   # 部署后跑一次 headless 冒烟
    python scripts/setup_dsh_home.py --apply --with-credentials   # 复制本机 ~/.dsh 凭据

注意：home/.credentials.yaml 含密钥。dsh-runtime/ 已 gitignore；整个目录
绝不提交 git、绝不外发（拷给内部同事前自行确认密钥合规）。

2026-09-04 实测坑：robocopy 从别处拷运行时会把 home/profiles/node_modules 下
的 junction 实体化成真目录，DSH 启动即炸（exists and is not a symlink）。
跨机迁移用本脚本重建；robocopy 路线必须补 junction 重建（见计划书 Task 2 Step 1b）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME = REPO_ROOT / "dsh-runtime"
DSH_PIN_VERSION = "0.1.0-rc.6"   # 钉死（ADR-0002：不追升级）。
# 注：实测用的本机 checkout 是 0.1.0-rc.5，但该版本从未发布到 npm registry
# （发布序列 rc.3 → rc.6 跳号）；rc.6 是最接近实测版本的已发布构建，
# 部署末尾的 --smoke 负责验证 headless 输出契约（stdout 文本 + exit 0/1）未漂移。
MIN_NODE = (22, 19)              # engines: ^22.19 || >=24


def _node_version(node: str) -> tuple[int, int] | None:
    try:
        out = subprocess.run([node, "--version"], capture_output=True,
                             text=True, timeout=15)
    except OSError:
        return None
    raw = out.stdout.strip().lstrip("v")
    try:
        major, minor = raw.split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return None


def _ok(v: tuple[int, int] | None) -> bool:
    return bool(v) and (v >= (24, 0) or v >= MIN_NODE)


def plan() -> list[str]:
    return [
        f"1. 复制本机 node.exe → {RUNTIME / 'node' / 'node.exe'}",
        f"2. npm 安装 @deepseek-ai/dsh@{DSH_PIN_VERSION} → {RUNTIME / 'app'}",
        f"3. 从 package.json bin 字段解析入口，生成干净 shim → {RUNTIME / 'dsh.cmd'}",
        f"4. 初始化第二套 HOME → {RUNTIME / 'home'}（settings 钉 deepseek 路由）",
        f"5. 创建工作目录 → {RUNTIME / 'workspace'}",
    ]


def apply(with_credentials: bool, smoke: bool) -> int:
    RUNTIME.mkdir(exist_ok=True)

    # 1. 便携 Node
    src_node = shutil.which("node")
    if not src_node:
        print("✗ 本机未找到 node；请先安装 Node.js >= 22.19")
        return 1
    ver = _node_version(src_node)
    if not _ok(ver):
        print(f"✗ Node 版本 {ver} 不满足 >=22.19 / >=24")
        return 1
    (RUNTIME / "node").mkdir(exist_ok=True)
    shutil.copy2(src_node, RUNTIME / "node" / "node.exe")
    print(f"✓ 便携 Node {'.'.join(map(str, ver))} 就位")

    # 2. dsh 包（pnpm hoisted = 平铺 node_modules 可整体拷走；2026-09-04 实测：
    #    npm 在这棵数百包的依赖树上解析死锁 15 分钟+，pnpm 31 秒完成）
    app = RUNTIME / "app"
    app.mkdir(exist_ok=True)
    if not (app / "node_modules" / "@deepseek-ai" / "dsh").is_dir():
        pnpm = shutil.which("pnpm.cmd") or shutil.which("pnpm")
        if not pnpm:
            print("✗ 本机未找到 pnpm（npm 会在该依赖树上死锁，必须用 pnpm hoisted）")
            return 1
        print(f"… pnpm 安装 @deepseek-ai/dsh@{DSH_PIN_VERSION}（约 30 秒）")
        r = subprocess.run(
            [pnpm, "add", f"@deepseek-ai/dsh@{DSH_PIN_VERSION}",
             "--config.node-linker=hoisted",
             "--registry=https://registry.npmmirror.com"],
            cwd=app,
        )
        if r.returncode != 0:
            print("✗ pnpm 安装失败")
            return 1
    print("✓ dsh 包就位（版本钉死，hoisted 平铺）")

    # 3. 干净 shim：从 package.json bin 字段解析入口（不写死内部路径）
    pkg = app / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    meta = json.loads(pkg.read_text(encoding="utf-8"))
    bin_field = meta.get("bin")
    entry = bin_field["dsh"] if isinstance(bin_field, dict) else bin_field
    if not entry:
        print("✗ 包内未声明 bin 入口")
        return 1
    (RUNTIME / "dsh.cmd").write_text(
        "@echo off\r\n"
        'set "DSH_HOME=%~dp0home"\r\n'
        f'"%~dp0node\\node.exe" "%~dp0app\\node_modules\\@deepseek-ai\\dsh\\{entry}" %*\r\n',
        encoding="ascii",
    )
    print("✓ dsh.cmd 生成（无 cd、%~dp0 相对路径，并自注入 DSH_HOME）")

    # 4. 第二套 HOME
    home = RUNTIME / "home"
    home.mkdir(exist_ok=True)
    settings = home / "settings.yaml"
    if not settings.exists():
        # provider 必须是 deepseek-official（npm 包内 adapter 的注册名）；
        # settings 的 llm-deepseek 段决定哪些 provider 在线，adapter 在 base bundle。
        settings.write_text(
            "# 第二套 DSH（VERA 深度思考通道）最小配置\n"
            "agent-default-model:\n"
            "  provider: deepseek-official\n"
            "  model: deepseek-v4-flash\n",
            encoding="utf-8",
        )
    if with_credentials:
        src_cred = Path.home() / ".dsh" / ".credentials.yaml"
        if src_cred.exists():
            shutil.copy2(src_cred, home / ".credentials.yaml")
            print("✓ 凭据已复制（含密钥，注意保密）")
        else:
            print("⚠ 本机 ~/.dsh/.credentials.yaml 不存在，需手动放置凭据")
    else:
        print("ℹ 未复制凭据（--with-credentials 可从本机 ~/.dsh 复制）")

    # 5. 工作目录
    (RUNTIME / "workspace").mkdir(exist_ok=True)

    # 冒烟（可选）
    if smoke:
        print("… 冒烟：headless 单问实测（真实调用 LLM）")
        t0 = time.monotonic()
        r = subprocess.run(
            [str(RUNTIME / "dsh.cmd"), "--profile", "headless",
             "用一句话回答：1+1 等于几？"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300,
        )
        dt = time.monotonic() - t0
        print(f"{'✓' if r.returncode == 0 else '✗'} exit={r.returncode} "
              f"耗时 {dt:.1f}s")
        print("  stdout:", (r.stdout or "").strip()[:200])
        if r.returncode != 0:
            print("  stderr:", (r.stderr or "").strip()[:300])
            return 1

    print("\n部署完成。应急开关：brain/dsh_channel.py 的 DSH_CHANNEL_ENABLED。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DSH 便携运行时部署（ADR-0002）")
    ap.add_argument("--apply", action="store_true", help="实际执行（默认 dry-run）")
    ap.add_argument("--with-credentials", action="store_true",
                    help="从本机 ~/.dsh 复制凭据到第二套 HOME")
    ap.add_argument("--smoke", action="store_true",
                    help="部署后跑一次 headless 冒烟（真实调用 LLM）")
    args = ap.parse_args()

    if not args.apply:
        print("dry-run 部署计划（--apply 实际执行）：")
        for line in plan():
            print(" ", line)
        return 0
    return apply(args.with_credentials, args.smoke)


if __name__ == "__main__":
    sys.exit(main())
