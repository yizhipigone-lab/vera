"""仓位加大 4 池对比: 每笔 5万-20万 (原 2千-2万), 验证收益鸿沟是复利驱动还是小盘单笔。
若鸿沟收窄 → 复利驱动; 不变 → 小盘单笔更赚。止损等用 default.yaml 默认。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
from core.connector import TdxConnector
from pipeline.pipeline import Pipeline

POOLS = [('沪深300成分', '23'), ('中证500成分', '24'), ('创业板(全1400)', '51'), ('全A', '5')]

print("=" * 60, flush=True)
print("仓位加大 4 池对比 (每笔 5万-20万, 资金100万)  2019-2026", flush=True)
print("=" * 60, flush=True)

TdxConnector.initialize()
pipe = Pipeline('config/strategy_heima_chuangye.yaml', 'config/default.yaml')
# 覆盖仓位: 2千-2万 → 5万-20万
pipe.config['backtest']['position_sizing']['min_buy_amount'] = 50000
pipe.config['backtest']['position_sizing']['max_buy_amount'] = 200000

rows = []
for name, utype in POOLS:
    print(f"\n===== {name} (list_type={utype}) =====", flush=True)
    pipe.config['selection']['universe']['type'] = utype
    pipe.config['strategy']['name'] = f'黑马起步_大仓位_{name}'
    try:
        selections = pipe.step1_select()
        if selections.empty:
            rows.append({'池子': name, '说明': '无信号'}); continue
        n_stocks = selections['stock_code'].nunique()
        bt = pipe.step2_backtest(selections)
        m = bt.get('metrics', {}) or {}
        rows.append({
            '池子': name, '股票数': n_stocks, '信号数': len(selections),
            '累计收益': m.get('cumulative_return'), '年化收益': m.get('annualized_return'),
            '最大回撤': m.get('max_drawdown'), '夏普': m.get('sharpe_ratio'),
            'Calmar': m.get('calmar_ratio'), '胜率': m.get('win_rate'),
            '交易笔数': m.get('total_trades'),
        })
        print(f"  累计={m.get('cumulative_return',0):+.2%} 年化={m.get('annualized_return',0):+.2%} "
              f"回撤={m.get('max_drawdown',0):+.2%} 夏普={m.get('sharpe_ratio',0):.2f} "
              f"Calmar={m.get('calmar_ratio',0):.2f} 交易={m.get('total_trades',0)}", flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        rows.append({'池子': name, '说明': f'失败: {e}'})

TdxConnector.close()

print("\n\n========== 大仓位 4 池对比汇总 ==========", flush=True)
df = pd.DataFrame(rows)
def fp(x): return f"{x:+.2%}" if isinstance(x, (int, float)) else ('-' if pd.isna(x) else x)
def fn(x): return f"{x:.2f}" if isinstance(x, (int, float)) else ('-' if pd.isna(x) else x)
for c in ['累计收益', '年化收益', '最大回撤', '胜率']:
    if c in df.columns: df[c] = df[c].apply(fp)
for c in ['夏普', 'Calmar']:
    if c in df.columns: df[c] = df[c].apply(fn)
print(df.to_string(index=False), flush=True)
df.to_csv('output/heima_bigpos_4pool.csv', index=False, encoding='utf-8-sig')
print("\n(对照: 小仓位原结果)", flush=True)
print("  沪深300 +58.85% / 中证500 +92.11% / 创业板 +280.75% / 全A +704.78%", flush=True)
print("=== 完成 ===", flush=True)
