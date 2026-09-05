# -*- coding: utf-8 -*-
"""公式批量回测排名 (2026-07-23 用户任务)。

对公式清单逐个: TDX 选股 (全部A股, 剔ST) → 30 日首信号过滤 →
引擎回测 (default.yaml 止盈止损 + 卖出 20 交易日冷却) → 逐公式落盘 JSON
→ 汇总排名 MD 报告。

规则 (用户拍板):
- 30 日首信号: 一只票近 30 个交易日内已有过信号 (无论是否成交/保留),
  本次信号丢弃不买。选股取 padded 区间 (start-70 自然日) 以覆盖窗口回溯,
  规则应用后裁回真实 start。
- 卖出冷却: 全清仓后 20 个交易日内禁止同票重新买入 (engine
  sell_cooldown_days; 持仓中换股不受影响)。

断点续跑: 每公式落盘 results/{safe_name}.json, 已存在则跳过 (--force 重跑)。
失败 (无信号/TDX 报错/引擎异常) 记录后继续, 不中断整批。

用法:
    # 小样本
    python tools/batch_formula_rank.py --formulas QUANTQQ,黑马,MACD金叉 --limit 5
    # 全量 (公式清单默认读 MQ 的 valid_formulas_clean.txt)
    python tools/batch_formula_rank.py
    # 只汇总报告 (从已有 results/*.json)
    python tools/batch_formula_rank.py --report-only
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

from backtest.engine import BacktestEngine  # noqa: E402
from backtest.stop_config import load_stop_config  # noqa: E402
from core.connector import TdxConnector  # noqa: E402
from core.data_fetcher import DataFetcher  # noqa: E402
from selection.selector import StockSelector  # noqa: E402
from selection.signal_rules import filter_first_signal_in_window  # noqa: E402
from utils.config_loader import ConfigLoader  # noqa: E402
from utils import parquet_cache as pcu  # noqa: E402  (AV 安全原子写, 照 kline_cache 范式)

DEFAULT_FORMULA_FILE = r"E:\1target\MQ\MQ\x-tdxqmt\_logs\valid_formulas_clean.txt"

# 窗口回溯 padding: 30 交易日 ≈ 45 自然日, 取 70 留余量
PAD_DAYS = 70


def safe_name(formula: str, taken: set) -> str:
    """公式名 → 安全文件名 (保留中英文/数字/-, 其余转 _; 冲突加 hash)。"""
    base = re.sub(r"[^\w一-龥-]", "_", formula).strip("_") or "unnamed"
    name = base
    if name in taken:
        name = f"{base}_{hashlib.md5(formula.encode('utf-8')).hexdigest()[:8]}"
    taken.add(name)
    return name


def read_formulas(path: str) -> list:
    formulas, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name and name not in seen:
                seen.add(name)
                formulas.append(name)
    return formulas


def _universe_cfg(args) -> dict:
    """universe 配置: --sectors 非空时走板块并集 (selector 忽略 type 下拉)。
    --include-delisted: 保留已退市股 (样本外回测缓解幸存者偏差, 2026-07-25)。"""
    return {
        "type": args.universe_type,
        "exclude_st": True,
        "exclude_quit": not bool(getattr(args, "include_delisted", False)),
        "sectors": [s.strip() for s in str(args.sectors or "").split(",") if s.strip()],
    }


def run_one(formula: str, stocks: list, calendar: list, args,
            bt_cfg: dict, stop_config: dict) -> dict:
    """单公式全流程, 返回结果记录 dict (不抛异常)。"""
    rec = {"formula": formula, "status": "error", "error": "",
           "signals_raw": 0, "signals_used": 0, "n_trades": 0,
           "metrics": {}, "elapsed_s": 0.0}
    t0 = time.time()
    try:
        padded_start = (pd.Timestamp(args.start) - pd.Timedelta(days=PAD_DAYS)
                        ).strftime("%Y%m%d")
        sel_cfg = {
            "formula_name": formula,
            "formula_arg": "",
            "universe": _universe_cfg(args),
            "period": "1d",
            "dividend_type": 1,
        }
        selector = StockSelector(sel_cfg)
        raw = selector.run(start_time=padded_start, end_time=args.end,
                           stock_list=stocks)
        rec["signals_raw"] = int(len(raw)) if raw is not None else 0

        filtered = filter_first_signal_in_window(
            raw, window_td=args.first_signal_window,
            real_start=args.start, calendar=calendar)
        rec["signals_used"] = int(len(filtered)) if filtered is not None else 0

        if filtered is None or len(filtered) == 0:
            rec["status"] = "no_signals"
            rec["elapsed_s"] = round(time.time() - t0, 1)
            return rec

        engine = BacktestEngine(bt_cfg)
        result = engine.run(selections=filtered, start_time=args.start,
                            end_time=args.end, stop_config=stop_config)
        metrics = result.get("metrics") or {}
        if not metrics:
            rec["status"] = "no_metrics"
            rec["elapsed_s"] = round(time.time() - t0, 1)
            return rec
        trades = result.get("trades")
        rec["n_trades"] = int(len(trades)) if trades is not None else 0
        rec["metrics"] = {k: (float(v) if isinstance(v, (int, float)) else v)
                          for k, v in metrics.items()}
        rec["status"] = "ok"
    except Exception as e:  # 单公式失败不中断整批
        rec["error"] = f"{type(e).__name__}: {e}"[:500]
    rec["elapsed_s"] = round(time.time() - t0, 1)
    return rec


def _save_result(jp: Path, rec: dict) -> bool:
    """AV 安全写结果 JSON: 唯一 tmp → os.replace 退避重试 (照 kline_cache 范式)。

    杀软/索引器常在 write→replace 间隙短暂锁文件 → PermissionError(WinError 5)。
    原代码 `tmp.replace(jp)` 无重试, 一旦被杀软锁住整批直接崩 (GS0005 即此死法)。
    返回 True=落盘成功, False=重试耗尽仍失败 (调用方标记本条失败但不中断整批)。
    """
    payload = json.dumps(rec, ensure_ascii=False, indent=1)
    tmp = pcu.tmp_path_for(jp)           # pid+uuid 唯一 tmp, 避免跨进程/重跑碰撞
    def _rewrite(t):
        t.write_text(payload, encoding="utf-8")
    try:
        tmp.write_text(payload, encoding="utf-8")
        pcu.atomic_replace(tmp, jp, rewrite=_rewrite, attempts=6, backoff=0.7)
        return True
    except Exception as e:
        print(f"[WARN] 结果写盘失败 {jp.name}: {type(e).__name__}: {e} "
              f"(本条结果丢失, 下次重跑将重试)")
        return False


def _pct(v, digits=2):
    return f"{v * 100:.{digits}f}%" if isinstance(v, (int, float)) else "—"


def _num(v, digits=2):
    return f"{v:.{digits}f}" if isinstance(v, (int, float)) else "—"


def write_report(results: list, args, outdir: Path) -> Path:
    """汇总 results/*.json → 排名 MD。"""
    ok = [r for r in results if r["status"] == "ok"]
    no_sig = [r["formula"] for r in results if r["status"] == "no_signals"]
    failed = [r for r in results if r["status"] not in ("ok", "no_signals")]
    ok.sort(key=lambda r: r["metrics"].get("cumulative_return", -9), reverse=True)

    lines = []
    lines.append(f"# 通达信公式批量回测排名 ({args.start} ~ {args.end})")
    lines.append("")
    lines.append(f"> 生成时间: {pd.Timestamp.now():%Y-%m-%d %H:%M} | "
                 f"公式总数 {len(results)} | 有效 {len(ok)} | "
                 f"无信号 {len(no_sig)} | 失败 {len(failed)}")
    lines.append("")
    lines.append("## 一、回测设置")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 回测区间 | {args.start} ~ {args.end} |")
    lines.append(f"| 股票池 | 全部A股 (TDX list_type={args.universe_type}, 含北交所, 剔除ST/退市/港股) |")
    lines.append(f"| 初始资金 | {args.capital:,.0f} 元 |")
    lines.append(f"| 单票买入上限 | {args.max_buy_amount:,.0f} 元 |")
    lines.append("| 周期/复权 | 日线 / 前复权 |")
    lines.append("| 公式参数 | 全部留空 (通用公式不带参数) |")
    lines.append(f"| 30日首信号规则 | 近 {args.first_signal_window} 个交易日内已有过信号的, 新信号丢弃不买 |")
    lines.append(f"| 卖出冷静期 | 全清仓后 {args.cooldown_days} 个交易日内不买回同一只票 |")
    lines.append("")
    lines.append("## 二、方法学说明")
    lines.append("")
    lines.append("- **入场**: 尾盘选股, 信号日 T 以收盘价买入 (系统业务铁律), 含 0.1% 买入滑点")
    lines.append("- **T+1**: 当日买入不可当日卖出")
    lines.append("- **交易成本**: 佣金万三 (双边) + 滑点 0.1% (双边) + 印花税 0.05% (卖出单边)")
    lines.append("- **止盈止损机制** (现行 default.yaml, 优先级 trailing_first: 阶梯 > 移动 > 成本 > 时间):")
    lines.append("  - 成本止损: 浮亏 -12% 清仓 (含跳空低开保护)")
    lines.append("  - 移动止盈: 浮盈 +3.5% 激活, 自最高价回撤 1% 触发")
    lines.append("  - 阶梯止盈: +6% 卖 30%, +15% 再卖 30%")
    lines.append("  - 时间止损: 持仓满 20 个交易日清仓")
    lines.append("  - 公式卖出: 关闭; 期末未平仓按市值计价, 不强平")
    lines.append("- **30日首信号**: 选股区间向前多取 70 自然日 (覆盖 30 交易日回溯窗), "
                 "一只票与其上一信号 (无论当时是否成交) 相距 ≤30 个交易日的信号一律丢弃")
    lines.append("- **卖出冷静期**: 任何全清仓 (含止损/止盈/时间/退市强平) 后 20 个交易日内, "
                 "该票再出信号也不买; 持仓中的换股 (卖旧买新) 不受限")
    lines.append("- **排名口径**: 按区间累计收益率降序")
    lines.append("")
    lines.append("## 三、Top 20 公式")
    lines.append("")
    lines.append("| 排名 | 公式 | 累计收益 | 年化 | 胜率 | 最大回撤 | 夏普 | Calmar | 交易数 | 信号(用/原始) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(ok[:20], 1):
        m = r["metrics"]
        lines.append(
            f"| {i} | {r['formula']} | {_pct(m.get('cumulative_return'))} "
            f"| {_pct(m.get('annualized_return'))} | {_pct(m.get('win_rate'))} "
            f"| {_pct(m.get('max_drawdown'))} | {_num(m.get('sharpe_ratio'))} "
            f"| {_num(m.get('calmar_ratio'))} | {r['n_trades']} "
            f"| {r['signals_used']}/{r['signals_raw']} |")
    lines.append("")
    lines.append("## 四、完整排名")
    lines.append("")
    lines.append("| 排名 | 公式 | 累计收益 | 年化 | 胜率 | 最大回撤 | 夏普 | Calmar | 交易数 | 信号(用/原始) | 耗时s |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(ok, 1):
        m = r["metrics"]
        lines.append(
            f"| {i} | {r['formula']} | {_pct(m.get('cumulative_return'))} "
            f"| {_pct(m.get('annualized_return'))} | {_pct(m.get('win_rate'))} "
            f"| {_pct(m.get('max_drawdown'))} | {_num(m.get('sharpe_ratio'))} "
            f"| {_num(m.get('calmar_ratio'))} | {r['n_trades']} "
            f"| {r['signals_used']}/{r['signals_raw']} | {r['elapsed_s']:.0f} |")
    lines.append("")
    lines.append("## 五、无信号公式 (区间内 30 日规则后无可买信号)")
    lines.append("")
    lines.append("、".join(no_sig) if no_sig else "无")
    lines.append("")
    lines.append("## 六、失败公式")
    lines.append("")
    if failed:
        lines.append("| 公式 | 状态 | 原因 |")
        lines.append("|---|---|---|")
        for r in failed:
            lines.append(f"| {r['formula']} | {r['status']} | {r['error'][:120]} |")
    else:
        lines.append("无")
    lines.append("")
    lines.append("## 七、风险与局限")
    lines.append("")
    lines.append("- 单区间回测 (2026 上半年), 未做样本外/滚动窗口验证, 历史收益不代表未来")
    lines.append("- 股票池为选股执行当日的全A快照, 存在幸存者偏差 (期间退市股可能不在池中)")
    lines.append("- 信号质量依赖通达信公式本身与本地数据完整性; 失败公式已单列, 不参与排名")
    lines.append("- 单票 1 万上限 × 100 万资金, 信号密集公式的实际成交受资金占用排队影响")
    lines.append("")
    path = outdir / f"公式批量回测排名_{args.start}_{args.end}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def collect_results(results_dir: Path) -> list:
    out = []
    for p in sorted(results_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formula-file", default=DEFAULT_FORMULA_FILE)
    ap.add_argument("--start", default="20260101")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y%m%d"))
    ap.add_argument("--universe-type", default="5", help="5=全部A股(含北交所)")
    ap.add_argument("--sectors", default="",
                    help="行业板块代码 (逗号分隔, 如 881319.SH=半导体); 设置后忽略 universe-type 下拉, 用板块并集")
    ap.add_argument("--capital", type=float, default=1000000.0)
    ap.add_argument("--max-buy-amount", type=float, default=10000.0)
    ap.add_argument("--cooldown-days", type=int, default=20)
    ap.add_argument("--first-signal-window", type=int, default=30)
    ap.add_argument("--outdir", default=str(ROOT / "output" / "formula_rank"))
    ap.add_argument("--formulas", default="", help="只跑指定公式 (逗号分隔)")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个")
    ap.add_argument("--force", action="store_true", help="忽略已有结果重跑")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--include-delisted", action="store_true",
                    help="保留已退市股 (样本外回测缓解幸存者偏差)")
    ap.add_argument("--trailing-gap-protection", action="store_true",
                    help="移动止盈跳空保护: 跳空跌穿回撤线按开盘价成交 (悲观口径)")
    ap.add_argument("--slippage", type=float, default=None,
                    help="覆盖滑点 (默认用 default.yaml 的 0.001)")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    results_dir = outdir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    if args.report_only:
        path = write_report(collect_results(results_dir), args, outdir)
        print(f"[OK] 报告: {path}")
        return

    formulas = read_formulas(args.formula_file)
    if args.formulas:
        want = [s.strip() for s in args.formulas.split(",") if s.strip()]
        formulas = [f for f in formulas if f in want] or want
    if args.limit:
        formulas = formulas[: args.limit]
    print(f"[INFO] 公式 {len(formulas)} 个, 区间 {args.start}~{args.end}, "
          f"池={args.universe_type}{' sectors=' + args.sectors if args.sectors else ''}, "
          f"资金={args.capital:,.0f}, "
          f"单票上限={args.max_buy_amount:,.0f}, "
          f"冷却={args.cooldown_days}td, 首信号窗={args.first_signal_window}td")

    # 1. 股票池只解析一次 (剔ST全A ~5540 只, get_stock_info 批量约 1 分钟)
    TdxConnector.initialize()
    uni_selector = StockSelector({
        "formula_name": "_", "universe": _universe_cfg(args)})
    stocks = uni_selector.resolve_universe()
    print(f"[INFO] 股票池 {len(stocks)} 只")

    # 2. 交易日历 (30 日窗口距离度量), 覆盖 padded 区间
    padded_start = (pd.Timestamp(args.start) - pd.Timedelta(days=PAD_DAYS)
                    ).strftime("%Y%m%d")
    calendar = DataFetcher.get_trading_dates(
        "SH", start_time=padded_start, end_time=args.end)
    print(f"[INFO] 交易日历 {len(calendar)} 天 ({calendar[0] if calendar else '?'} "
          f"~ {calendar[-1] if calendar else '?'})")

    # 3. 回测配置: default.yaml + 任务覆盖
    defaults = ConfigLoader.load_defaults()
    bt_cfg = dict(defaults.get("backtest", {}))
    bt_cfg["initial_capital"] = float(args.capital)
    ps = dict(bt_cfg.get("position_sizing", {}))
    ps["max_buy_amount"] = float(args.max_buy_amount)
    bt_cfg["position_sizing"] = ps
    bt_cfg["matrix_cache"] = False  # 392 公式各跑一次, 缓存纯属占盘
    bt_cfg["sell_cooldown_days"] = int(args.cooldown_days)
    if args.slippage is not None:
        bt_cfg["slippage"] = float(args.slippage)
    stop_config = load_stop_config()
    if args.trailing_gap_protection:
        # 2026-07-26 机制乐观项定量: 跳空跌穿回撤线按开盘价成交 (V2/V3 悲观口径)
        stop_config.setdefault("trailing_stop", {})["gap_protection"] = True

    # 4. 逐公式执行 (断点续跑)
    taken = set()
    name_map = {}
    for f in read_formulas(args.formula_file):
        name_map[f] = safe_name(f, taken)

    n_done = n_skip = n_fail = 0
    for idx, formula in enumerate(formulas, 1):
        jp = results_dir / f"{name_map[formula]}.json"
        if jp.exists() and not args.force:
            n_skip += 1
            print(f"[{idx}/{len(formulas)}] {formula} — 已有结果, 跳过")
            continue
        rec = run_one(formula, stocks, calendar, args, bt_cfg, stop_config)
        saved = _save_result(jp, rec)
        if not saved:
            n_fail += 1
            print(f"[{idx}/{len(formulas)}] {formula} — 写盘失败(已重试), 下次重跑")
            continue
        tag = {"ok": "OK", "no_signals": "无信号"}.get(rec["status"], "失败")
        if rec["status"] == "ok":
            n_done += 1
            m = rec["metrics"]
            print(f"[{idx}/{len(formulas)}] {formula} — {tag} "
                  f"收益={_pct(m.get('cumulative_return'))} "
                  f"胜率={_pct(m.get('win_rate'))} 交易={rec['n_trades']} "
                  f"信号={rec['signals_used']}/{rec['signals_raw']} "
                  f"({rec['elapsed_s']:.0f}s)")
        else:
            n_fail += 1
            print(f"[{idx}/{len(formulas)}] {formula} — {tag} "
                  f"{rec['error'][:100]} ({rec['elapsed_s']:.0f}s)")

    # 5. 汇总报告
    path = write_report(collect_results(results_dir), args, outdir)
    TdxConnector.close()
    print(f"[DONE] 成功 {n_done} / 无信号或失败 {n_fail} / 跳过 {n_skip}")
    print(f"[OK] 报告: {path}")


if __name__ == "__main__":
    main()
