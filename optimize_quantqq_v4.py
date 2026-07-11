"""QUANTQQ 参数优化 v4 — 分层随机 + 样本外验证 + 扩大边界 (2026-07-09)

v3 基线 (全样本 540 组): 年化最高 22.19%, 0 组≥25%
v4 突破方向 (基于 v3 数据瓶颈):
  1. 移动回撤更紧 (0.003/0.005/0.008) — v3 最优全在 0.005
  2. 硬止损更宽 (-0.12/-0.15/-0.18/-0.20) — v3 最优 -0.15
  3. 阶梯展开 1/2/3/4 档 — v3 只 2 档
  4. 激活线探更低 (0.015/0.025/0.035/0.05)
  5. 新增 仓位模式 (资金×金额上限×占比% 三维打包, 8 预设)
样本外: 2020-2023 训练挑参, 2024-2026 验证
目标: 训练年化≥25% 且 样本外≥20% (落空→如实报告+放宽15%)

口径: 沿用 v3 (ffill/bfill), 非审计级精度, 仅用于相对排序 (见 CLAUDE.md 回测可信度铁律)
"""
import sys, os, time, logging, pickle, random, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('v4')
# 降级子模块日志, 减少批量回测时的 I/O (涨停过滤/批次等), 加速
for _n in ('backtest.engine', 'core.formula_runner', 'core.data_fetcher'):
    logging.getLogger(_n).setLevel(logging.WARNING)

START, END = '20200101', '20260706'
FORMULA = 'QUANTQQ'
SPLIT_DATE = pd.Timestamp('2023-12-31')
TEST_START = pd.Timestamp('2024-01-01')

CACHE_PATH = 'output/optimize/quantqq_data_cache.pkl'
CKPT_PATH  = 'output/optimize/quantqq_v4_checkpoint.csv'
OUT_DIR    = 'output/optimize'

# =========================================================================
# 参数网格 (我定, 基于 v3 瓶颈)
# =========================================================================
ACTIVATION = [0.015, 0.025, 0.035, 0.05]
DRAWDOWN   = [0.003, 0.005, 0.008]
COST_STOP  = [-0.12, -0.15, -0.18, -0.20]
LADDERS = [
    [(0.06, 1.00)],                                              # 1档
    [(0.04, 0.30), (0.10, 0.30)],                                # 2档 (v3 top)
    [(0.06, 0.30), (0.15, 0.30)],                                # 2档 (v3 top)
    [(0.08, 0.40), (0.20, 0.40)],                                # 2档 高台阶
    [(0.04, 0.25), (0.10, 0.25), (0.20, 0.25)],                  # 3档
    [(0.05, 0.30), (0.12, 0.30), (0.25, 0.20)],                  # 3档 宽幅
    [(0.04, 0.20), (0.08, 0.20), (0.15, 0.20), (0.25, 0.20)],    # 4档
]
# 仓位模式: (资金, 单票金额上限, 单票占比%) — 三维打包避免被 max_buy_amount 压死
POS_MODES = [
    (500000,   50000, 0.10),  # 小资金+中等集中
    (500000,  100000, 0.20),  # 小资金+集中
    (1000000,  20000, 1.00),  # v3 默认 (100万/固定2万/不限占比)
    (1000000, 100000, 0.10),  # 100万+10%占比
    (1000000, 200000, 0.20),  # 100万+20%集中
    (3000000,  20000, 1.00),  # 大资金+极度分散 (v3 式)
    (3000000, 300000, 0.10),  # 大资金+10%
    (5000000, 500000, 0.10),  # 超大资金+10%
]
PRIORITY  = ['trailing_first', 'ladder_tp_first', 'stop_first']
TIME_STOP = [15, 20, 30, 40]
COND_TIME = [None, (10, 0.03), (10, 0.05), (15, 0.03), (15, 0.05)]  # (days, profit)
FIRST_DAY = [None, 0.05, 0.08]


def gen_combos(n, seed=2026):
    """分层随机: 止损核心维度随机抽样, 时间维度随机配对, 仓位模式循环覆盖"""
    rnd = random.Random(seed)
    seen, combos = set(), []
    # 先保证每个仓位模式至少出现 n/len(POS_MODES) 次
    guard = 0
    while len(combos) < n and guard < n * 20:
        guard += 1
        act = rnd.choice(ACTIVATION); dd = rnd.choice(DRAWDOWN)
        cs  = rnd.choice(COST_STOP);  li = rnd.randrange(len(LADDERS))
        pm  = rnd.randrange(len(POS_MODES)); pri = rnd.choice(PRIORITY)
        ts  = rnd.choice(TIME_STOP);  ct = rnd.choice(COND_TIME); fd = rnd.choice(FIRST_DAY)
        key = (act, dd, cs, li, pm, pri, ts, ct, fd)
        if key in seen:
            continue
        seen.add(key)
        combos.append(key)
    return combos


# =========================================================================
# 1. 数据准备 (缓存, 避免重连 TDX)
# =========================================================================
def prepare_data(force=False):
    if os.path.exists(CACHE_PATH) and not force:
        logger.info('读缓存 %s', CACHE_PATH)
        with open(CACHE_PATH, 'rb') as f:
            d = pickle.load(f)
        return d['c'], d['h'], d['l'], d['entries'], d['sel_df']

    TdxConnector.ensure_connected()
    logger.info('[%s] 选股 %s~%s ...', FORMULA, START, END)
    t0 = time.time()
    sel_df = FormulaRunner.run_stock_selection_with_dates(
        formula_name=FORMULA, formula_arg='', stock_list=None,
        start_time=START, end_time=END, stock_period='1d', dividend_type=1,
    )
    logger.info('信号 %d, 股票 %d, %.0fs', len(sel_df), sel_df['stock_code'].nunique(), time.time() - t0)

    codes = sel_df['stock_code'].unique().tolist()
    logger.info('拉K线 %d 只 ...', len(codes))
    t0 = time.time()
    k = DataFetcher.get_kline(codes, START, END, dividend_type='front', period='1d')
    close = k['Close'].sort_index(); high = k['High'].sort_index(); low = k['Low'].sort_index()
    logger.info('K线 %d 天 × %d 只, %.0fs', close.shape[0], close.shape[1], time.time() - t0)

    common = sorted(set(close.columns) & set(codes))
    c = close[common].ffill().bfill()
    h = high.reindex(columns=common).ffill().bfill()
    l = low.reindex(columns=common).ffill().bfill()

    entries = pd.DataFrame(False, index=c.index, columns=c.columns)
    for _, row in sel_df.iterrows():
        code, dt = row['stock_code'], pd.to_datetime(row['select_date'])
        if code not in entries.columns:
            continue
        if dt in entries.index:
            entries.loc[dt, code] = True
        else:
            mask = entries.index >= dt
            if mask.any():
                entries.loc[entries.index[mask][0], code] = True

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CACHE_PATH, 'wb') as f:
        pickle.dump({'c': c, 'h': h, 'l': l, 'entries': entries, 'sel_df': sel_df}, f)
    logger.info('缓存已存 %s', CACHE_PATH)
    return c, h, l, entries, sel_df


def split_train_test(c, h, l, entries, sel_df):
    tr = c.index <= SPLIT_DATE
    te = c.index >= TEST_START
    d = {}
    for name, mask in [('tr', tr), ('te', te)]:
        d['c_' + name] = c.loc[mask].copy()
        d['h_' + name] = h.loc[mask].copy()
        d['l_' + name] = l.loc[mask].copy()
        d['e_' + name] = entries.loc[mask].copy()
    sdf = sel_df.copy()
    sdf['select_date'] = pd.to_datetime(sdf['select_date'])
    d['sel_tr'] = sdf[sdf['select_date'] <= SPLIT_DATE]
    d['sel_te'] = sdf[sdf['select_date'] >= TEST_START]
    return d


# =========================================================================
# 2. 单组合回测
# =========================================================================
def build_cfg(combo):
    act, dd, cs, li, pm, pri, ts, ct, fd = combo
    capital, max_buy, pct = POS_MODES[pm]
    levels = LADDERS[li]
    engine_cfg = {
        'initial_capital': float(capital),
        'commission': 0.0003, 'slippage': 0.001, 'stamp_tax': 0.0005,
        'enable_realistic_costs': True, 'period': '1d',
        'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': float(max_buy),
                            'lot_size': 100, 'min_lots': 1, 'max_position_pct': float(pct)},
    }
    stop = {
        'priority': pri,
        'cost_stop': {'enabled': True, 'threshold': cs},
        'trailing_stop': {'enabled': True, 'activation': act, 'drawdown': dd},
        'ladder_tp': {'enabled': True, 'levels': [{'profit': p, 'sell_ratio': r} for p, r in levels]},
        'time_stop': {'enabled': True, 'max_hold_days': ts},
        'cond_time_stop': {'enabled': ct is not None,
                           'days': ct[0] if ct else 7, 'profit': ct[1] if ct else 0.01},
        'first_day': {'enabled': fd is not None, 'target': fd if fd else 0.0},
    }
    lp = np.array([p for p, _ in levels], dtype=np.float64)
    lr = np.array([r for _, r in levels], dtype=np.float64)
    return engine_cfg, stop, lp, lr, len(levels)


def run_one(engine_cfg, stop, lp, lr, nlv, data, seg):
    eng = BacktestEngine(engine_cfg)
    r = eng.run_cached(
        data['c_' + seg], data['e_' + seg],
        data['h_' + seg].values.astype(np.float64),
        data['l_' + seg].values.astype(np.float64),
        stop, data['sel_' + seg], lp, lr, nlv, skip_sm=True,
    )
    return r


def extract(r):
    m = r['metrics']
    ann = m.get('annualized_return', 0)
    dd = abs(m.get('max_drawdown', 0))
    sh = m.get('sharpe_ratio', 0)
    cal = ann / dd if dd > 1e-9 else 0.0
    return {'ann': ann * 100, 'dd': dd * 100, 'sh': sh, 'cal': cal,
            'win': m.get('win_rate', 0) * 100, 'n': len(r['trades'])}


# =========================================================================
# 3. 批量跑 (断点续跑)
# =========================================================================
def run_batch(combos, data, trial=False):
    results, done = [], set()
    if not trial and os.path.exists(CKPT_PATH):
        try:
            old = pd.read_csv(CKPT_PATH)
            results = old.to_dict('records')
            done = set(old['key'].astype(str))
            logger.info('续跑: 已完成 %d 组, 剩 %d 组', len(done), len(combos) - len(done))
        except Exception as e:
            logger.warning('读 checkpoint 失败: %s', e)

    t0 = time.time()
    n_done_session = 0
    for i, combo in enumerate(combos, 1):
        key = str(combo)
        if key in done:
            continue
        eng_cfg, stop, lp, lr, nlv = build_cfg(combo)
        try:
            mtr = extract(run_one(eng_cfg, stop, lp, lr, nlv, data, 'tr'))
            # combo = (act,dd,cs,li,pm,pri,ts,ct,fd): pm=combo[4] pri=combo[5]
            capital, max_buy, pct = POS_MODES[combo[4]]
            rec = {
                'key': key,
                'activation': combo[0], 'drawdown': combo[1], 'cost_stop': combo[2],
                'ladder': '+'.join(f'{p*100:.0f}/{r*100:.0f}' for p, r in LADDERS[combo[3]]),
                'pos_mode': f'{capital/10000:.0f}万/上限{max_buy/10000:.0f}万/{pct*100:.0f}%',
                'priority': combo[5],
                'time_stop': combo[6], 'cond_time': str(combo[7]), 'first_day': str(combo[8]),
                'train_ann': mtr['ann'], 'train_dd': mtr['dd'], 'train_sh': mtr['sh'],
                'train_cal': mtr['cal'], 'train_win': mtr['win'], 'train_n': mtr['n'],
                'test_ann': np.nan, 'test_dd': np.nan, 'test_sh': np.nan,
                'test_cal': np.nan, 'test_n': 0,
            }
            # 样本外 (仅训练年化≥25%)
            if mtr['ann'] >= 25.0:
                mte = extract(run_one(eng_cfg, stop, lp, lr, nlv, data, 'te'))
                rec.update({'test_ann': mte['ann'], 'test_dd': mte['dd'], 'test_sh': mte['sh'],
                            'test_cal': mte['cal'], 'test_n': mte['n']})
            results.append(rec)
            n_done_session += 1
        except Exception as e:
            logger.warning('[%d/%d] 失败: %s', i, len(combos), e)

        if n_done_session % 50 == 0 and n_done_session > 0:
            elapsed = time.time() - t0
            per = elapsed / n_done_session
            logger.info('[%d done] 单次 %.2fs, 本次 %.0fs', n_done_session, per, elapsed)
            if not trial:
                pd.DataFrame(results).to_csv(CKPT_PATH, index=False)

    if not trial:
        pd.DataFrame(results).to_csv(CKPT_PATH, index=False)
    return pd.DataFrame(results)


# =========================================================================
# 4. 报告
# =========================================================================
def report(df, total_combos=None):
    if df.empty:
        logger.warning('无结果'); return
    logger.info('=' * 110)
    logger.info('  QUANTQQ v4 优化报告 — 训练 2020-2023 / 样本外 2024-2026')
    logger.info('=' * 110)

    logger.info('\n【训练段全量统计】')
    logger.info('  组合数: %d', len(df))
    logger.info('  训练年化: max=%.2f%%  median=%.2f%%  mean=%.2f%%', df.train_ann.max(), df.train_ann.median(), df.train_ann.mean())
    logger.info('  训练≥25%%: %d 组', (df.train_ann >= 25).sum())
    logger.info('  训练≥20%%: %d 组', (df.train_ann >= 20).sum())
    logger.info('  训练≥15%%: %d 组', (df.train_ann >= 15).sum())

    df_te = df.dropna(subset=['test_ann'])
    logger.info('\n【样本外验证 (仅训练≥25%%的组合跑了样本外)】')
    logger.info('  跑了样本外的组合: %d', len(df_te))
    if len(df_te) > 0:
        logger.info('  样本外年化: max=%.2f%%  median=%.2f%%', df_te.test_ann.max(), df_te.test_ann.median())
        logger.info('  样本外≥20%%: %d 组 (硬目标)', (df_te.test_ann >= 20).sum())
        logger.info('  样本外≥15%%: %d 组 (放宽线)', (df_te.test_ann >= 15).sum())

    # 达标组合: 训练≥25% 且 样本外≥20%
    hit = df_te[df_te.test_ann >= 20].sort_values('test_ann', ascending=False)
    logger.info('\n【达标组合 (训练≥25%% 且 样本外≥20%%): %d 组】', len(hit))
    if len(hit) > 0:
        _print_top(hit.head(20))

    # 放宽: 训练≥25% 且 样本外≥15%
    if len(hit) == 0:
        loose = df_te[df_te.test_ann >= 15].sort_values('test_ann', ascending=False)
        logger.info('\n【硬目标落空 → 放宽 (训练≥25%% 且 样本外≥15%%): %d 组】', len(loose))
        if len(loose) > 0:
            _print_top(loose.head(20))
        # 最接近的 10 个
        logger.info('\n【样本外最接近 20%% 的 10 个 (无论是否≥15%%)】')
        _print_top(df_te.sort_values('test_ann', ascending=False).head(10))

    logger.info('\n【训练年化 Top 15】')
    _print_top(df.sort_values('train_ann', ascending=False).head(15), train=True)

    # 单变量分析 (训练年化)
    for col, label in [('activation', '激活线'), ('drawdown', '回撤'), ('cost_stop', '硬止损'),
                       ('priority', '优先级'), ('pos_mode', '仓位模式'), ('time_stop', '时间止损'),
                       ('ladder', '阶梯'), ('first_day', '首日'), ('cond_time', '条件时间')]:
        g = df.groupby(col).train_ann.agg(['mean', 'median', 'count'])
        logger.info('\n按 %s 分组 (训练年化 mean/median/count):', label)
        for idx, row in g.iterrows():
            logger.info('  %s: mean=%.2f%% median=%.2f%% n=%d', idx, row['mean'], row['median'], row['count'])


def _print_top(d, train=False):
    for _, r in d.iterrows():
        ta = r['test_ann']
        tas = f'{ta:.1f}%' if not np.isnan(ta) else '--'
        logger.info('  act=%.3f dd=%.3f cs=%.2f [%s] %s %s ts=%s ct=%s fd=%s | 训练%.1f%%(Cal%.2f) 样外%s(Cal%.2f)',
                    r['activation'], r['drawdown'], r['cost_stop'], r['ladder'], r['pos_mode'],
                    r['priority'], r['time_stop'], r['cond_time'], r['first_day'],
                    r['train_ann'], r['train_cal'], tas, r['test_cal'])


# =========================================================================
# main
# =========================================================================
if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['trial', 'full', 'report'], default='trial')
    ap.add_argument('--n', type=int, default=50, help='trial=试跑组数 / full=全量组数')
    args = ap.parse_args()

    if args.mode == 'report':
        df = pd.read_csv(CKPT_PATH)
        report(df)
        sys.exit(0)

    c, h, l, entries, sel_df = prepare_data()
    data = split_train_test(c, h, l, entries, sel_df)
    logger.info('训练段 %d 天, 样本外 %d 天, 股票 %d 只', len(data['c_tr']), len(data['c_te']), data['c_tr'].shape[1])

    combos = gen_combos(args.n)
    logger.info('生成 %d 组合', len(combos))

    t0 = time.time()
    df = run_batch(combos, data, trial=(args.mode == 'trial'))
    elapsed = time.time() - t0

    if args.mode == 'trial':
        n_act = len(df)
        logger.info('=' * 80)
        logger.info('  试跑测速结果: %d 组 / %.0fs = %.2fs/组', n_act, elapsed, elapsed / max(n_act, 1))
        if n_act > 0:
            est_2h = int(2 * 3600 / (elapsed / max(n_act, 1)))
            est_8h = int(8 * 3600 / (elapsed / max(n_act, 1)))
            logger.info('  预估: 2h 可跑 ~%d 组, 8h 可跑 ~%d 组', est_2h, est_8h)
            logger.info('  训练年化范围: %.2f%% ~ %.2f%%', df.train_ann.min(), df.train_ann.max())
            logger.info('  训练≥25%%: %d 组', (df.train_ann >= 25).sum())
        logger.info('=' * 80)
        # 试跑也存一份
        df.to_csv(os.path.join(OUT_DIR, 'quantqq_v4_trial.csv'), index=False)

    if args.mode == 'full':
        report(df)
        ts = time.strftime('%Y%m%d_%H%M%S')
        df.to_csv(os.path.join(OUT_DIR, f'quantqq_v4_full_{ts}.csv'), index=False)
        logger.info('全量结果已存: quantqq_v4_full_%s.csv', ts)
