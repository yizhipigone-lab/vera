"""审计引用校验器 (迭代 2, 2026-07-15).

事件回顾: 2026-07-15 审计报告错引 [backtest/metrics.py:67-68] 说没有 `as e`,
实际代码第 67 行就是 `except Exception as e:` — 假阳性 CRITICAL.

根因: 审计 agent 没真读代码就引用行号.

本模块提供:
- extract_references(text)         从 markdown 文本提取 [file:line] 或 [file:line-line] 引用
- verify_references(refs, root)    校验每个引用: file 存在 + 行号范围在文件内 + 抽样内容比对
- audit_markdown_file(path, root)  单文件审计: 找出所有引用, 校验, 返回报告

用法:
    python -m docs.audit._verify_references docs/audit/2026-07-XX_report.md
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


# 匹配 [path/to/file.py:42] 或 [path/to/file.py:42-51]
# 支持反引号包围 (报告里偶尔出现 `[file.py:42]`)
# file 部分不能含空格/方括号, 可含字母数字 . _ - / \
REF_PATTERN = re.compile(
    r"\[(?P<file>[^\[\]\\\s]+?\.py):(?P<start>\d+)(?:-(?P<end>\d+))?\]"
)


@dataclass(frozen=True)
class Reference:
    """审计报告中的一处代码引用."""
    file: str
    start: int
    end: Optional[int]
    raw: str  # 原始匹配字符串 (用于报告)

    @property
    def is_range(self) -> bool:
        return self.end is not None

    @property
    def line_count(self) -> int:
        if self.end is None:
            return 1
        return self.end - self.start + 1


@dataclass(frozen=True)
class VerificationResult:
    """单条引用的校验结果."""
    reference: Reference
    exists: bool
    in_range: bool
    content_snippet: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.exists and self.in_range and self.error is None


@dataclass
class AuditReport:
    """整份审计 markdown 的引用校验报告."""
    markdown_path: str
    total_refs: int = 0
    valid: int = 0
    invalid: int = 0
    results: List[VerificationResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.valid / self.total_refs if self.total_refs > 0 else 0.0

    @property
    def is_clean(self) -> bool:
        return self.invalid == 0


def extract_references(text: str) -> List[Reference]:
    """从 markdown 文本提取所有 [file.py:line] 或 [file.py:line-line] 引用."""
    refs = []
    seen = set()
    for m in REF_PATTERN.finditer(text):
        raw = m.group(0)
        if raw in seen:
            continue  # 去重
        seen.add(raw)
        file = m.group("file")
        start = int(m.group("start"))
        end_str = m.group("end")
        end = int(end_str) if end_str else None
        refs.append(Reference(file=file, start=start, end=end, raw=raw))
    return refs


def verify_references(refs: List[Reference], project_root: Path) -> List[VerificationResult]:
    """校验每条引用: file 存在 + 行号在文件内 + 抽样内容."""
    results = []
    for ref in refs:
        file_path = project_root / ref.file
        if not file_path.exists():
            results.append(VerificationResult(
                reference=ref, exists=False, in_range=False,
                error=f"文件不存在: {ref.file}",
            ))
            continue

        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            results.append(VerificationResult(
                reference=ref, exists=True, in_range=False,
                error=f"读取失败: {e}",
            ))
            continue

        # 行号校验 (1-indexed)
        if ref.start < 1 or ref.start > len(lines):
            results.append(VerificationResult(
                reference=ref, exists=True, in_range=False,
                error=f"起始行 {ref.start} 越界 (文件 {len(lines)} 行)",
            ))
            continue

        if ref.end is not None and (ref.end < ref.start or ref.end > len(lines)):
            results.append(VerificationResult(
                reference=ref, exists=True, in_range=False,
                error=f"结束行 {ref.end} 越界",
            ))
            continue

        # 抽取内容片段 (供审计员肉眼比对)
        if ref.end is None:
            snippet = lines[ref.start - 1]
        else:
            snippet = "\n".join(lines[ref.start - 1: ref.end])

        results.append(VerificationResult(
            reference=ref, exists=True, in_range=True,
            content_snippet=snippet[:200],  # 截断避免刷屏
        ))

    return results


def audit_markdown_file(md_path: Path, project_root: Path) -> AuditReport:
    """对单份审计 markdown 做引用校验, 返回报告."""
    text = md_path.read_text(encoding="utf-8")
    refs = extract_references(text)
    results = verify_references(refs, project_root)
    valid = sum(1 for r in results if r.is_valid)
    invalid = len(results) - valid
    return AuditReport(
        markdown_path=str(md_path),
        total_refs=len(refs),
        valid=valid,
        invalid=invalid,
        results=results,
    )


def print_report(report: AuditReport, verbose: bool = False) -> None:
    """打印校验报告."""
    print(f"\n{'=' * 70}")
    print(f"审计引用校验: {report.markdown_path}")
    print(f"{'=' * 70}")
    print(f"总引用: {report.total_refs} | 有效: {report.valid} | 无效: {report.invalid}")
    print(f"通过率: {report.pass_rate:.1%}")
    print()

    if report.invalid > 0:
        print("[INVALID REFERENCES]")
        for r in report.results:
            if not r.is_valid:
                print(f"  ❌ {r.reference.raw} — {r.error}")

    if verbose and report.valid > 0:
        print("\n[VALID REFERENCES — 内容抽样]")
        for r in report.results:
            if r.is_valid:
                snippet = (r.content_snippet or "").replace("\n", "\\n")
                print(f"  ✅ {r.reference.raw}")
                print(f"     └─ {snippet[:120]}")

    if report.is_clean:
        print("\n[PASS] 所有引用有效")
    else:
        print(f"\n[FAIL] {report.invalid} 处引用需修复")


def main(argv: List[str]) -> int:
    """CLI 入口.

    用法: python -m docs.audit._verify_references <markdown_path> [--verbose]
    返回码: 0 = 全 PASS, 1 = 有 FAIL
    """
    if len(argv) < 2:
        print("用法: python -m docs.audit._verify_references <markdown_path> [--verbose]")
        return 2

    md_path = Path(argv[1])
    if not md_path.exists():
        print(f"文件不存在: {md_path}")
        return 2

    project_root = Path(__file__).resolve().parents[2]  # VERA/
    verbose = "--verbose" in argv

    report = audit_markdown_file(md_path, project_root)
    print_report(report, verbose=verbose)
    return 0 if report.is_clean else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))