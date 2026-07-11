"""黑马起步 / 日线 / 4 池对比: 沪深300成分 / 中证500成分 / 创业板(全1400) / 全A。
时间 2019-06-01 ~ 2026-07-10。止损/资金用 default.yaml 默认 (trailing_first 等)。
连一次 TDX, 循环 4 池选股+回测, 输出对比表 + CSV。

注: 创业板用 list_type=51 (全创业板 ~1400 只); TDX tqcenter 无指数成分股接口,
    取不到创业板指(399006)的 100 只成分, 故用全板块代替, 对比表标注实际规模。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
from core.connector import TdxConnector
from pipeline.pipeline import Pipeline

POOLS = [
    ('沪深300成分', '23'),
    ('中证500成分', '24'),
    ('创业板(全1400)', '51'),
    ('全A', '5'),
]

print("=" * 60, flush=True)
print("黑马起步 日线 4 池对比  2019-06-01 ~ 2026-07-10", flush=True)
print("=" * 60, flush=True)

print("\n=== 连接 TDX ===", flush=True)
TdxConnector.initialize()

pipe = Pipeline('config/strategy_heima_chuangye.yaml', 'config/default.yaml')
rows = []
for name, utype in POOLS:
    print(f"\n===== {name} (list_type={utype}) =====", flush=True)
    pipe.config['selection']['universe']['type'] = utype
    pipe.config['strategy']['name'] = f'黑马起步_{name}'
    try:
        selections = pipe.step1_select()
        if selections.empty:
            print("  选股为空, 跳过", flush=True)
            rows.append({'池子': name, '股票数': 0, '信号数': 0, '说明': '无信号'})
            continue
        n_stocks = selections['stock_code'].nunique()
        print(f"  信号: {len(selections)} 条 / 涉及 {n_stocks} 只", flush=True)
        bt = pipe.step2_backtest(selections)
        m = bt.get('metrics', {}) or {}
        rows.append({
            '池子': name,
            '股票数': n_stocks,
            '信号数': len(selections),
            '累计收益': m.get('cumulative_return'),
            '年化收益': m.get('annualized_return'),
            '最大回撤': m.get('max_drawdown'),
            '夏普': m.get('sharpe_ratio'),
            'Calmar': m.get('calmar_ratio'),
            '胜率': m.get('win_rate'),
            '盈亏比': m.get('profit_loss_ratio'),
            '盈利因子': m.get('profit_factor'),
            '交易笔数': m.get('total_trades'),
        })
        print(f"  累计={m.get('cumulative_return',0):+.2%} 年化={m.get('annualized_return',0):+.2%} "
              f"回撤={m.get('max_drawdown',0):+.2%} 夏普={m.get('sharpe_ratio',0):.2f} "
              f"Calmar={m.get('calmar_ratio',0):.2f} 胜率={m.get('win_rate',0):.1%} "
              f"交易={m.get('total_trades',0)}", flush=True)
    except Exception as e:
        import traceback
        print(f"  失败: {e}", flush=True)
        traceback.print_exc()
        rows.append({'池子': name, '说明': f'失败: {e}'})

try:
    TdxConnector.close()
except Exception:
    pass

# ===== 汇总 =====
print("\n\n" + "=" * 60, flush=True)
print("4 池对比汇总", flush=True)
print("=" * 60, flush=True)
df = pd.DataFrame(rows)
def fmt_pct(x):
    return f"{x:+.2%}" if isinstance(x, (int, float)) else ('-' if pd.isna(x) else x)
def fmt_num(x):
    return f"{x:.2f}" if isinstance(x, (int, float)) else ('-' if pd.isna(x) else x)
for col in ['累计收益', '年化收益', '最大回撤', '胜率']:
    if col in df.columns:
        df[col] = df[col].apply(fmt_pct)
for col in ['夏普', 'Calmar', '盈亏比', '盈利因子']:
    if col in df.columns:
        df[col] = df[col].apply(fmt_num)
print(df.to_string(index=False), flush=True)

out = Path('output/heima_compare_4pool.csv')
out.parent.mkdir(parents=True, exist_ok=True)
df.to_csv(out, index=False, encoding='utf-8-sig')
print(f"\nCSV 已存: {out}", flush=True)
print("=== 完成 ===", flush=True)
