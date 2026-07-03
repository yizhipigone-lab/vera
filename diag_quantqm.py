"""诊断 QUANTQM 公式 — 逐项排查为何 TDX 找不到该公式。"""
import sys, os
TDX_HOME = os.environ.get('TDX_HOME', r'E:\NEW_TDX')
sys.path.insert(0, os.path.join(TDX_HOME, r"PYPlugins\user"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tqcenter import tq

print("=" * 60)
print("Step 1: 初始化 TDX 连接")
print("=" * 60)
tq.initialize(os.path.join(TDX_HOME, r"PYPlugins\user\tqcenter.py"))
print(f"  run_id={tq.run_id}, initialized={tq._initialized}")
print(f"  TDX路径: {TDX_HOME}")

# 获取股票列表
all_stocks = tq.get_stock_list("50", list_type=1)
print(f"\n股票池: {len(all_stocks)} 只")

# ── 测试 QUANTQQ (应该成功) ──
print("\n" + "=" * 60)
print("Step 2: 测试 QUANTQQ (对照组，应该成功)")
print("=" * 60)
r_qq = tq.formula_process_mul_xg(
    formula_name="QUANTQQ",
    formula_arg="",
    return_count=0,
    return_date=True,
    stock_list=all_stocks[:20],
    stock_period="1d",
    start_time="20260101",
    end_time="20260131",
    dividend_type=1,
)
err_id = r_qq.get("ErrorId", "MISSING")
err_msg = r_qq.get("Error", "")
print(f"  ErrorId={err_id}  Error={err_msg}")
# 统计结果
count = sum(1 for k, v in r_qq.items() if k != "ErrorId" and v)
print(f"  有结果的股票数: {count}")

# ── 测试 QUANTQM ──
print("\n" + "=" * 60)
print("Step 3: 测试 QUANTQM")
print("=" * 60)
r_qm = tq.formula_process_mul_xg(
    formula_name="QUANTQM",
    formula_arg="",
    return_count=0,
    return_date=True,
    stock_list=all_stocks[:20],
    stock_period="1d",
    start_time="20260101",
    end_time="20260131",
    dividend_type=1,
)
err_id = r_qm.get("ErrorId", "MISSING")
err_msg = r_qm.get("Error", "")
print(f"  ErrorId={err_id}  Error={err_msg}")
count = sum(1 for k, v in r_qm.items() if k != "ErrorId" and v)
print(f"  有结果的股票数: {count}")

# ── 尝试作为指标公式执行 ──
print("\n" + "=" * 60)
print("Step 4: 尝试 QUANTQM 作为指标公式 (ZB)")
print("=" * 60)
r_qm_zb = tq.formula_process_mul_zb(
    formula_name="QUANTQM",
    formula_arg="",
    return_count=1,
    return_date=False,
    stock_list=all_stocks[:5],
    stock_period="1d",
    start_time="20260101",
    end_time="20260131",
    dividend_type=1,
    xsflag=-1,
)
err_id = r_qm_zb.get("ErrorId", "MISSING")
err_msg = r_qm_zb.get("Error", "")
print(f"  ErrorId={err_id}  Error={err_msg}")
if err_id in ("0", "19"):
    vals = r_qm_zb.get("Value", {})
    print(f"  作为指标公式执行成功! 结果数: {len(vals)}")
    for k, v in list(vals.items())[:3]:
        print(f"  {k}: {str(v)[:200]}")
else:
    print("  作为指标公式也失败")

# ── 验证大小写 ──
print("\n" + "=" * 60)
print("Step 5: 大小写变体测试")
print("=" * 60)
variants = ["QUANTQM", "QuantQM", "quantqm", "QUANT QM", "QUANT_QM"]
for variant in variants:
    r = tq.formula_process_mul_xg(
        formula_name=variant,
        formula_arg="",
        return_count=0,
        return_date=True,
        stock_list=all_stocks[:5],
        stock_period="1d",
        start_time="20260101",
        end_time="20260131",
        dividend_type=1,
    )
    err = r.get("Error", "")
    print(f"  [{variant}] ErrorId={r.get('ErrorId','?')} Error={err}")

tq.close()
print("\n诊断完成")
print("\n排查建议:")
print("  1. 如果 QUANTQM 作为指标公式(ZB)成功 → 该公式是技术指标,不是条件选股公式")
print("    需要在 TDX 中将其另存为'条件选股公式'类型")
print("  2. 如果所有变体都失败 → 确认 TDX 中公式名称完全一致(含大小写)")
print("  3. 检查 TDX 公式管理器: 功能→公式系统→公式管理器")
print("     确认 QUANTQM 在'条件选股公式'分类下")
