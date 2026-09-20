# -*- coding: utf-8 -*-
"""tools/sentiment_dry_run.py — 舆情阈值干跑：只统计，不推送（2026-09-20）。

**为什么需要它**：规则 1（个股情绪）/ 规则 2（板块聚集）因"打分层返回嵌套、
规则层按扁平读"的形状 bug，**自上线起从未触发过一次**（§9.2）。修好之后它们
第一次真正工作，而 `polarity_min 0.6 + strength_min 2` 这个阈值是 2026-08-13
拍板的"中等档"，**从未在真实数据上验证过** —— 偏松会糊用户一脸卡片，偏紧等于
没修。本工具给出"这批真实新闻会触发几条"的读数，供决定阈值。

**零副作用（三条，缺一不可）**：
- 去重库开**临时库**做标记（生产库只读用来数"真正新的有几条"）—— 否则干跑会把
  新闻标记成"已见"，真实 tick 就再也看不到它们了；
- notifier 用**计数桩**，不发飞书；
- 不写任何持久状态（不落 alert_log、不推卡片）。

**注意**：一次干跑只是**一次采样**，≠ 全天触发量。要估日频，得多跑几次，
或按计划书 §9.5 把 webhook 留空跑一天、再用「舆情」页读数。

用法：
    python tools/sentiment_dry_run.py                # 按 config 的 max_per_scan
    python tools/sentiment_dry_run.py --limit 20     # 打分 20 条
    python tools/sentiment_dry_run.py --new-only     # 只打分去重后"新的"那些

铁律 1：不 import trade。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain.alert_rules import (RULE_NAMES, apply_push_suppression,  # noqa: E402
                               load_config, rule_index_move,
                               rule_sector_cluster, rule_stock_sentiment,
                               rule_volume_anomaly)
from brain.news_dedup import NewsDedup  # noqa: E402
from brain.sentiment_judge import judge_batch  # noqa: E402
from utils.sysutil import ensure_utf8_stdout, project_root  # noqa: E402

_CFG_PATH = project_root() / "config" / "sentiment.yaml"


class _CountingNotifier:
    """只记不发。替掉 production notifier，保证不碰飞书。"""

    def __init__(self) -> None:
        self.pushed: list = []

    def notify_alert(self, payload, title="", color="red") -> None:
        self.pushed.append((payload, title, color))

    def notify_summary(self, payload, title="舆情日报") -> None:
        pass


def _describe(alerts, cfg) -> None:
    """按规则分组打印命中明细。"""
    rc = cfg.get("rules", {})
    for rule in ("stock_sentiment", "sector_cluster", "index_move", "volume_anomaly"):
        rows = [a for a in alerts if a.rule == rule]
        name = RULE_NAMES.get(rule, rule)
        enabled = rc.get(rule, {}).get("enabled", rule not in ("volume_anomaly",))
        flag = "" if enabled else "  [配置里已关]"
        print(f"   {name:<10}({rule}) : {len(rows)} 条{flag}")
        for a in rows[:12]:
            print(f"        · {a.code} p={a.polarity:+.2f} s={a.strength} "
                  f"| {a.evidence_quote[:44]}")
        if len(rows) > 12:
            print(f"        · ……其余 {len(rows) - 12} 条")


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    ap = argparse.ArgumentParser(prog="python tools/sentiment_dry_run.py",
                                 description="舆情阈值干跑（只统计不推送）")
    ap.add_argument("--limit", type=int, default=0,
                    help="打分条数上限（默认取 config 的 news.max_per_scan）")
    ap.add_argument("--new-only", action="store_true",
                    help="只打分生产去重库判为'新的'那些（贴近真实 tick 口径）")
    args = ap.parse_args(argv)

    from brain import sentiment_pipeline as sp

    cfg = load_config(_CFG_PATH)
    max_n = args.limit or int(cfg.get("news", {}).get("max_per_scan", 15))

    print("=" * 68)
    print("舆情阈值干跑（只统计，不推送，不写库）")
    print("=" * 68)

    print("\n① 拉新闻（真实网络：财新要闻 akshare + search_web）…")
    news = sp._fetch_watch_news(cfg)
    print(f"   抓到 {len(news)} 条")

    # 生产去重库：**只读**判"真正新的有几条"，绝不 mark
    try:
        prod = NewsDedup()
        try:
            fresh_ids = {id(n) for n in prod.filter_unseen([dict(n) for n in news])}
        finally:
            prod.close()
        n_new = len(fresh_ids)
    except Exception as e:
        print(f"   (生产去重库读取失败，跳过新旧的统计: {e})")
        n_new = -1
    print(f"   其中在**生产去重库**里没见过的: {n_new if n_new >= 0 else '未知'} 条")

    pool = news
    if args.new_only:
        if n_new <= 0:
            print("\n   去重后没有新新闻 —— 干跑到此为止（真实 tick 这一轮也不会打分）")
            return 0
        pool = [n for n in news if id(n) in fresh_ids]

    sel = pool[:max_n]
    print(f"\n② LLM 打分（真实调用，逐条串行；本次 {len(sel)} 条，"
          f"judge_batch 内部单批上限 10）…")
    batch = [{"text": n.get("text", ""), "url": n.get("url", ""), "ts": 0.0}
             for n in sel]
    results = judge_batch(batch)
    scored = [r["sentiment"] for r in results
              if isinstance(r, dict) and isinstance(r.get("sentiment"), dict)
              and "error" not in r["sentiment"]]
    failed = len(results) - len(scored)
    print(f"   打分成功 {len(scored)} 条，失败 {failed} 条")

    if scored:
        print("\n   逐条分数（定阈值的关键读数）:")
        for s in scored:
            p = abs(float(s.get("polarity") or 0.0))
            st = int(s.get("strength") or 0)
            hp = s.get("hit_pool") or []
            hits = ",".join(str(h.get("value") if isinstance(h, dict) else h)
                            for h in hp) or "—"
            print(f"      |p|={p:.2f} s={st} hit=[{hits[:26]:<26}] "
                  f"{str(s.get('text', ''))[:34]}")
        print("\n   阈值敏感性 —— 规则1 会命中几条（要求 |p|≥阈值 且 s≥强度 且 hit_pool 非空）:")
        for pm in (0.6, 0.5, 0.4, 0.3):
            row = []
            for sm in (2, 1):
                n = sum(1 for s in scored
                        if abs(float(s.get("polarity") or 0.0)) >= pm
                        and int(s.get("strength") or 0) >= sm
                        and (s.get("hit_pool") or []))
                row.append(f"s>={sm}: {n} 条")
            print(f"      |p|>={pm} → " + "   ".join(row))
        print("   （上面任何一格 >0，说明把阈值放到那一格规则1就开始工作）")

    print("\n③ 行情快照（规则 3/4 用）…")
    snapshot = sp._fetch_snapshot()
    print(f"   快照 {len(snapshot)} 字符")

    print("\n④ 过四条规则（阈值取自 config/sentiment.yaml）…")
    alerts = []
    alerts += rule_stock_sentiment(scored, cfg)
    alerts += rule_sector_cluster(scored, cfg)
    alerts += rule_index_move(snapshot, cfg, ts=0.0)
    alerts += rule_volume_anomaly(snapshot, cfg, ts=0.0)
    _describe(alerts, cfg)

    print("\n⑤ 推送抑制后（这就是真实 tick 会推的条数）…")
    with tempfile.TemporaryDirectory() as td:
        dry = NewsDedup(db_path=Path(td) / "dry.db")
        try:
            pushed = apply_push_suppression(alerts, dry, cfg)
        finally:
            dry.close()
    print(f"   会推 {len(pushed)} 张卡（max_per_tick="
          f"{cfg.get('suppression', {}).get('max_per_tick', 3)}）")

    rc = cfg.get("rules", {})
    print("\n当前阈值："
          f"stock_sentiment |p|>={rc.get('stock_sentiment', {}).get('polarity_min', 0.6)} "
          f"且 strength>={rc.get('stock_sentiment', {}).get('strength_min', 2)}；"
          f"sector_cluster 命中>={rc.get('sector_cluster', {}).get('min_hits', 3)}"
          f" 且平均 |p|>={rc.get('sector_cluster', {}).get('polarity_min', 0.4)}")
    print("\n提醒：这是一次采样，不等于全天触发量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
