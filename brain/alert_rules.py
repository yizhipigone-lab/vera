"""brain/alert_rules.py — 舆情异动规则判定 (纯逻辑, 无 IO 无 LLM 无网络)。

四条触发规则 + 一层推送抑制。输入是"已打分新闻列表"和"已解析行情快照",
输出是 Alert 列表。所有 IO (拉新闻/调 LLM/拉行情/发飞书) 在调用方
(sentiment_pipeline), 本模块只做判定 → 好测。

守业务铁律 1: 本模块绝不 import trade/, 绝不触发任何交易动作 (纯报告逻辑)。
配置见 config/sentiment.yaml (可选覆盖; 缺失或无 PyYAML → 用 DEFAULT_CONFIG)。

规则清单 (审计 HIGH-1: 全文统一 4 规则 + 1 抑制层):
  1. 个股情绪突变: LLM 打分 |polarity|≥0.6 且 strength≥2
  2. 板块新闻密集: P1/算力关键词新闻≥3 条且情绪一致
  3. 大盘指数阈值: 主要指数涨跌幅绝对值≥1.5%
  4. 量能异常: v1 默认关 (需前 5 日均值基建, v2 启用相对倍数)
推送抑制 (独立噪声控制层, 不计入规则条数): 同 code 30min 内只推 1 次,
  单 tick 最多 3 条 (按 |polarity|×strength 降序)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)


# ── 默认配置 (config/sentiment.yaml 缺失时兜底) ────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "scan": {
        "interval_min": 10,
        "daily_report_hhmm": "15:05",
    },
    "rules": {
        "stock_sentiment": {
            "enabled": True, "polarity_min": 0.6, "strength_min": 2,
        },
        "sector_cluster": {
            "enabled": True, "min_hits": 3, "polarity_min": 0.4,
        },
        "index_move": {
            "enabled": True, "threshold_pct": 1.5,
            # 短关键词 + 子串匹配: 兜住 akshare 名称变体 (上证指数/上证综指 等)
            "watch_indices": ["上证", "深证", "创业", "科创"],
        },
        "volume_anomaly": {
            "enabled": False, "threshold_x": 1.5,   # v1 关: 需前5日均值
        },
    },
    "suppression": {
        "push_window_min": 30, "max_per_tick": 3,
    },
    "watch": {
        "concepts": ["算力租赁", "华为算力", "CPO", "光通信", "芯片", "EDA",
                     "半导体", "人工智能", "数据中心"],
    },
}


def load_config(path: str | Path | None = None) -> dict:
    """读 config/sentiment.yaml 覆盖默认值。文件缺失 / 无 PyYAML → 默认值 + warning。
    永不抛 (fail-soft: 配置读不到不能用默认值就不该崩)。"""
    cfg = _deep_copy(DEFAULT_CONFIG)
    if not path:
        return cfg
    p = Path(path)
    if not p.exists():
        logger.info("舆情配置 %s 不存在, 用 DEFAULT_CONFIG", p)
        return cfg
    try:
        import yaml  # 延迟 import: PyYAML 是可选依赖
    except ImportError:
        logger.warning("未装 PyYAML, 舆情配置 %s 不加载, 用 DEFAULT_CONFIG", p)
        return cfg
    try:
        override = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("舆情配置 %s 解析失败, 用 DEFAULT_CONFIG: %s", p, e)
        return cfg
    return _deep_merge(cfg, override)


def _deep_copy(d: dict) -> dict:
    return {k: _deep_copy(v) if isinstance(v, dict) else (
        list(v) if isinstance(v, list) else v) for k, v in d.items()}


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# ── Alert 数据结构 ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Alert:
    """一条舆情异动告警。to_payload() 转 sentiment_notifier 的卡片 payload。"""
    rule: str              # stock_sentiment / sector_cluster / index_move / volume_anomaly
    code: str              # 个股代码 / 指数代码 / 板块关键词
    name: str
    polarity: float
    strength: int
    confidence: float
    evidence_quote: str
    detail: dict
    ts: float

    def to_payload(self) -> dict:
        d = asdict(self)
        d["detail_str"] = _fmt_detail(self.detail)
        return d


def _fmt_detail(d: dict) -> str:
    if not d:
        return ""
    return " ".join(f"{k}={v}" for k, v in d.items() if v is not None)


# ── 行情快照解析 (data_tools.market_snapshot 文本 → 结构化) ─────────

def _safe_float(s: str | None) -> float | None:
    """容错解析: +1.23 / 1.23% / 1.2e10 / 4,500 / -- / 空 → float 或 None。"""
    if s is None:
        return None
    s = str(s).strip().rstrip("%").replace(",", "")
    if not s or s in ("--", "nan", "NaN"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _extract_section(text: str, title_keyword: str) -> str:
    """抠 Markdown 里 '## <含 title_keyword>' 到下一个 '## ' 之间的正文。"""
    pat = re.compile(r"## *[^#\n]*" + re.escape(title_keyword) + r"[^\n]*\n(.*?)(?=\n## |\Z)", re.S)
    m = pat.search(text or "")
    return m.group(1).strip() if m else ""


def parse_market_snapshot(text: str) -> dict:
    """解析 market_snapshot Markdown → 结构化 (fail-soft)。

    返回 {indices: [{code,name,pct,amount}], zt_count, zt_by_industry:{行业:count}}。
    任何解析失败 → 对应字段为空, 不抛。格式漂移时该规则自然降级跳过。
    """
    if not text:
        return {"indices": [], "zt_count": 0, "zt_by_industry": {}}
    return {
        "indices": _parse_index_section(text),
        "zt_count": _parse_zt_count(text),
        "zt_by_industry": _parse_zt_by_industry(text),
    }


def _parse_index_section(text: str) -> list[dict]:
    """抠 A 股主要指数表。按 header 列名定位列下标 (稳健于列宽/列序变化)。"""
    body = _extract_section(text, "A股主要指数")
    if not body:
        return []
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split()
    col = {name: i for i, name in enumerate(header)}
    name_idx = col.get("名称")
    code_idx = col.get("代码")
    pct_idx = col.get("涨跌幅")
    amt_idx = col.get("成交额")
    out: list[dict] = []
    for ln in lines[1:]:
        toks = ln.split()
        if len(toks) < len(header):
            continue
        name = toks[name_idx] if name_idx is not None and name_idx < len(toks) else ""
        code = toks[code_idx] if code_idx is not None and code_idx < len(toks) else ""
        pct = _safe_float(toks[pct_idx]) if pct_idx is not None and pct_idx < len(toks) else None
        amount = _safe_float(toks[amt_idx]) if amt_idx is not None and amt_idx < len(toks) else None
        if not name and pct is None:
            continue
        out.append({"code": code, "name": name, "pct": pct, "amount": amount})
    return out


def _parse_zt_count(text: str) -> int:
    """抠涨停池 '涨停 N 家' 的 N。"""
    body = _extract_section(text, "涨停池")
    m = re.search(r"涨停\s*(\d+)\s*家", body)
    return int(m.group(1)) if m else 0


def _parse_zt_by_industry(text: str) -> dict[str, int]:
    """抠涨停池按 '所属行业' 列统计。"""
    body = _extract_section(text, "涨停池")
    lines = [ln for ln in body.splitlines() if ln.strip()]
    # 找 header 行 (含"所属行业")
    industry_idx = None
    header_line = -1
    for i, ln in enumerate(lines):
        toks = ln.split()
        if "所属行业" in toks:
            industry_idx = toks.index("所属行业")
            header_line = i
            break
    if industry_idx is None:
        return {}
    counts: dict[str, int] = {}
    for ln in lines[header_line + 1:]:
        toks = ln.split()
        if len(toks) <= industry_idx:
            continue
        ind = toks[industry_idx]
        if ind and ind not in ("日期", "名称"):
            counts[ind] = counts.get(ind, 0) + 1
    return counts


# ── 四条触发规则 ───────────────────────────────────────────────────

def rule_stock_sentiment(scored_news: list[dict], cfg: dict) -> list[Alert]:
    """规则1: 个股情绪突变。|polarity|≥polarity_min 且 strength≥strength_min。
    从每条新闻的 hit_pool 提取标的代码, 每个标的生成一条 Alert。"""
    rc = cfg.get("rules", {}).get("stock_sentiment", {})
    if not rc.get("enabled", True):
        return []
    pol_min = float(rc.get("polarity_min", 0.6))
    str_min = int(rc.get("strength_min", 2))
    alerts: list[Alert] = []
    for news in scored_news:
        pol = float(news.get("polarity", 0.0) or 0.0)
        strength = int(news.get("strength", 0) or 0)
        if abs(pol) < pol_min or strength < str_min:
            continue
        hits = news.get("hit_pool") or []
        if not hits:
            continue
        for hit in hits:
            code = hit.get("value", "") if isinstance(hit, dict) else str(hit)
            if not code:
                continue
            alerts.append(Alert(
                rule="stock_sentiment", code=str(code), name="",
                polarity=pol, strength=strength,
                confidence=float(news.get("confidence", 0.0) or 0.0),
                evidence_quote=str(news.get("evidence_quote", ""))[:120],
                detail={"news": str(news.get("text", ""))[:160]},
                ts=float(news.get("ts", 0.0) or 0.0),
            ))
    return alerts


def rule_sector_cluster(scored_news: list[dict], cfg: dict) -> list[Alert]:
    """规则2: 板块新闻密集 + 情绪一致。
    watch 关键词命中的新闻数 ≥ min_hits, 且平均 |polarity| ≥ polarity_min → 该板块 Alert。
    v1 用文本匹配 (粗糙但无需 concept_tagger); v2 可接 concept_tagger 精确映射。"""
    rc = cfg.get("rules", {}).get("sector_cluster", {})
    if not rc.get("enabled", True):
        return []
    min_hits = int(rc.get("min_hits", 3))
    pol_min = float(rc.get("polarity_min", 0.4))
    keywords = list(cfg.get("watch", {}).get("concepts", []))
    if not keywords:
        return []
    # 每关键词收集命中新闻的 polarity
    bucket: dict[str, list[float]] = {kw: [] for kw in keywords}
    for news in scored_news:
        text = str(news.get("text", "")) + " " + str(news.get("evidence_quote", ""))
        pol = float(news.get("polarity", 0.0) or 0.0)
        for kw in keywords:
            if kw in text:
                bucket[kw].append(pol)
    alerts: list[Alert] = []
    for kw, pols in bucket.items():
        if len(pols) < min_hits:
            continue
        avg_pol = sum(abs(p) for p in pols) / len(pols)
        if avg_pol < pol_min:
            continue
        # 板块情绪取实际平均 (带符号), 强度按命中数分档
        signed_avg = sum(pols) / len(pols)
        strength = 2 if len(pols) >= min_hits * 2 else 1
        alerts.append(Alert(
            rule="sector_cluster", code=kw, name=f"{kw}板块",
            polarity=signed_avg, strength=strength,
            confidence=min(0.9, len(pols) / 10),
            evidence_quote=f"{kw} 相关新闻 {len(pols)} 条, 平均情绪 {signed_avg:+.2f}",
            detail={"hits": len(pols), "avg_polarity": round(signed_avg, 3)},
            ts=0.0,
        ))
    return alerts


def rule_index_move(snapshot_text: str, cfg: dict, ts: float = 0.0) -> list[Alert]:
    """规则3: 大盘指数阈值。解析 snapshot, 主要指数涨跌幅绝对值 ≥ threshold_pct → Alert。"""
    rc = cfg.get("rules", {}).get("index_move", {})
    if not rc.get("enabled", True):
        return []
    threshold = float(rc.get("threshold_pct", 1.5))
    watch = list(rc.get("watch_indices", []))
    parsed = parse_market_snapshot(snapshot_text)
    alerts: list[Alert] = []
    for idx in parsed["indices"]:
        name = idx.get("name", "")
        pct = idx.get("pct")
        if pct is None or not name:
            continue
        if watch and name not in watch and not any(w in name for w in watch):
            continue
        if abs(pct) >= threshold:
            alerts.append(Alert(
                rule="index_move", code=idx.get("code", name), name=name,
                polarity=max(-1.0, min(1.0, pct / 10)),   # 涨跌幅映射到 [-1,1]: 10%≈满格
                strength=2 if abs(pct) >= threshold * 1.5 else 1,
                confidence=0.95,
                evidence_quote=f"{name} 涨跌幅 {pct:+.2f}%",
                detail={"pct": round(pct, 3), "amount": idx.get("amount")},
                ts=ts,
            ))
    return alerts


def rule_volume_anomaly(snapshot_text: str, cfg: dict, ts: float = 0.0) -> list[Alert]:
    """规则4: 量能异常。v1 默认 disabled (需前 5 日均值做相对倍数, 暂无基建)。
    enabled 时也只做绝对值占位判断; v2 接历史均值后启用 threshold_x 相对倍数。"""
    rc = cfg.get("rules", {}).get("volume_anomaly", {})
    if not rc.get("enabled", False):
        return []
    # v1 占位: 解析上证成交额, 超绝对阈值告警 (相对倍数留 v2)
    parsed = parse_market_snapshot(snapshot_text)
    for idx in parsed["indices"]:
        if "上证" in idx.get("name", ""):
            amt = idx.get("amount")
            if amt and amt > 8e11:   # 粗阈值: 沪市成交 > 8000亿 (v2 改相对倍数)
                return [Alert(
                    rule="volume_anomaly", code=idx.get("code", "sh000001"),
                    name="沪市量能",
                    polarity=0.6, strength=1, confidence=0.5,
                    evidence_quote=f"沪市成交额 {amt/1e8:.0f}亿",
                    detail={"amount": amt, "threshold_x": rc.get("threshold_x", 1.5)},
                    ts=ts,
                )]
    return []


# ── 推送抑制层 (噪声控制, 不计入规则条数) ──────────────────────────

def apply_push_suppression(alerts: list[Alert], dedup, cfg: dict) -> list[Alert]:
    """同 code 在 push_window_min 内推过 → 抑制; 按 |polarity|×strength 降序取前 max_per_tick。
    dedup: NewsDedup 实例 (is_push_suppressed / mark_pushed)。抑制标记的写入 fail-soft。"""
    sc = cfg.get("suppression", {})
    window = int(sc.get("push_window_min", 30))
    max_n = int(sc.get("max_per_tick", 3))
    ranked = sorted(alerts, key=lambda a: abs(a.polarity) * a.strength, reverse=True)
    out: list[Alert] = []
    for a in ranked:
        if len(out) >= max_n:
            break
        try:
            if dedup.is_push_suppressed(a.code, within_min=window):
                continue
        except Exception as e:
            logger.warning("推送抑制查询异常 (降级放行): %s", e)
        out.append(a)
        try:
            dedup.mark_pushed(a.code)
        except Exception as e:
            logger.warning("推送抑制标记异常 (跳过): %s", e)
    return out
