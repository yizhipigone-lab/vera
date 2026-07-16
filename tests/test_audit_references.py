"""审计引用校验器自身测试 + 历史报告校验 (迭代 2, 2026-07-15).

锁住:
- _verify_references.extract_references 正确提取
- verify_references 正确校验存在性 + 行号范围
- 历史审计报告必须能通过校验 (C1 假阳性事件已修复, 应 PASS)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docs.audit._verify_references import (
    Reference,
    VerificationResult,
    AuditReport,
    extract_references,
    verify_references,
    audit_markdown_file,
    REF_PATTERN,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ═══════════════════════════════════════════════════════════════
# extract_references 单元测试
# ═══════════════════════════════════════════════════════════════


def test_extract_single_line_reference():
    """[file.py:42] 单行引用."""
    text = "见 [backtest/engine.py:687] 的 PERIODS_PER_YEAR 定义"
    refs = extract_references(text)
    assert len(refs) == 1
    assert refs[0].file == "backtest/engine.py"
    assert refs[0].start == 687
    assert refs[0].end is None
    assert refs[0].is_range is False


def test_extract_range_reference():
    """[file.py:42-51] 范围引用."""
    text = "见 [backtest/metrics.py:67-68] 的 except 块"
    refs = extract_references(text)
    assert len(refs) == 1
    assert refs[0].start == 67
    assert refs[0].end == 68
    assert refs[0].is_range is True
    assert refs[0].line_count == 2


def test_extract_multiple_references():
    """多引用 + 去重."""
    text = """
        [a.py:1] 和 [a.py:1] 是同一处 (应去重)
        [b.py:5-10] 是范围
        [c.py:7] 是单独
    """
    refs = extract_references(text)
    # a.py:1 出现两次, 只算 1 条
    files = [r.file for r in refs]
    assert files.count("a.py") == 1
    assert "b.py" in files
    assert "c.py" in files


def test_extract_no_reference():
    """无引用时不返回."""
    text = "没有引用的纯文本段落"
    assert extract_references(text) == []


def test_extract_ignores_non_python_files():
    """非 .py 文件不匹配 (报告里可能引用 .md / .yaml)."""
    text = "[CLAUDE.md:14] 和 [config/default.yaml:62]"
    refs = extract_references(text)
    assert refs == []  # 只匹配 .py


# ═══════════════════════════════════════════════════════════════
# verify_references 单元测试
# ═══════════════════════════════════════════════════════════════


def test_verify_existing_file_with_valid_line():
    """存在文件 + 合法行号 → valid."""
    ref = Reference(file="backtest/engine.py", start=687, end=None, raw="[backtest/engine.py:687]")
    results = verify_references([ref], PROJECT_ROOT)
    assert len(results) == 1
    assert results[0].is_valid
    assert results[0].content_snippet is not None


def test_verify_existing_file_with_valid_range():
    """存在文件 + 合法范围 → valid + 含 snippet."""
    ref = Reference(file="backtest/metrics.py", start=67, end=68, raw="[backtest/metrics.py:67-68]")
    results = verify_references([ref], PROJECT_ROOT)
    assert len(results) == 1
    assert results[0].is_valid
    assert "except" in (results[0].content_snippet or "")


def test_verify_nonexistent_file():
    """文件不存在 → invalid."""
    ref = Reference(file="does/not/exist.py", start=1, end=None, raw="[does/not/exist.py:1]")
    results = verify_references([ref], PROJECT_ROOT)
    assert len(results) == 1
    assert not results[0].is_valid
    assert "不存在" in results[0].error


def test_verify_line_out_of_range():
    """行号越界 → invalid."""
    ref = Reference(file="backtest/engine.py", start=99999, end=None, raw="[backtest/engine.py:99999]")
    results = verify_references([ref], PROJECT_ROOT)
    assert len(results) == 1
    assert not results[0].is_valid
    assert "越界" in results[0].error


def test_verify_range_end_before_start():
    """end < start → invalid."""
    ref = Reference(file="backtest/engine.py", start=100, end=50, raw="[x:100-50]")
    results = verify_references([ref], PROJECT_ROOT)
    assert not results[0].is_valid


def test_verify_line_zero_is_invalid():
    """行号 0 越界 (1-indexed)."""
    ref = Reference(file="backtest/engine.py", start=0, end=None, raw="[x:0]")
    results = verify_references([ref], PROJECT_ROOT)
    assert not results[0].is_valid


# ═══════════════════════════════════════════════════════════════
# C1 假阳性事件防回归 — 关键测试
# ═══════════════════════════════════════════════════════════════


def test_metrics_67_actual_code_has_as_e():
    """C1 假阳性防回归: metrics.py:67 必须有 'as e' (报告错引 = 审计失败)."""
    ref = Reference(file="backtest/metrics.py", start=67, end=68, raw="[backtest/metrics.py:67-68]")
    results = verify_references([ref], PROJECT_ROOT)
    assert results[0].is_valid
    # 关键: 实际代码第 67 行必须有 'as e'
    assert "as e" in (results[0].content_snippet or ""), (
        "metrics.py:67 必须含 'as e', 若不含则审计报告 C1 错引回归"
    )


# ═══════════════════════════════════════════════════════════════
# 历史审计报告校验 — 本日报告 (修改后) 应通过
# ═══════════════════════════════════════════════════════════════


def test_today_audit_report_passes_or_explains():
    """本日 (2026-07-15) 审计报告应通过引用校验.

    注: 该报告描述失真但代码引用本身存在, 所以本测试只校验'代码引用真实',
    不校验'描述与代码内容一致' (那是人工审计责任).
    """
    today_report = PROJECT_ROOT / "docs" / "audit" / "2026-07-15_全项目质量检查审计报告.md"
    if not today_report.exists():
        pytest.skip("当日审计报告不存在, 跳过")

    report = audit_markdown_file(today_report, PROJECT_ROOT)
    # 只要所有 file:line 引用都存在, 即视为通过
    assert report.is_clean, (
        f"审计报告有 {report.invalid} 处无效引用:\n"
        + "\n".join(f"  - {r.reference.raw}: {r.error}" for r in report.results if not r.is_valid)
    )


# ═══════════════════════════════════════════════════════════════
# AuditReport 数据类
# ═══════════════════════════════════════════════════════════════


def test_audit_report_pass_rate():
    """AuditReport.pass_rate 计算正确."""
    report = AuditReport(markdown_path="x.md", total_refs=10, valid=8, invalid=2)
    assert report.pass_rate == 0.8
    assert not report.is_clean


def test_audit_report_is_clean_when_all_valid():
    """全有效 → is_clean=True."""
    report = AuditReport(markdown_path="x.md", total_refs=3, valid=3, invalid=0)
    assert report.is_clean