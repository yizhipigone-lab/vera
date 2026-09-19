"""trade/rotation_ledger.py — 轮动在途买卖台账 + 隔夜核销 (2026-09-19 批次 4.5 自 rotation.py 端出)。

端出动机(架构审查 P1-9): `trade/rotation.py` 89KB 七合一。**在途台账**是一块
自包含职责 (挂单登记 → 次日/当轮核销 → 损坏处置), 抽出后调仓主逻辑与台账
各自的漂移面变小, 台账的判定表也集中在一处可读。

**为什么用 mixin 而不是"独立类 + 依赖注入"**: 这些方法读写的全是
RotationFeature 的消费者线程内状态 (self._store / _gateway / _lots / _cfg_getter…)。
抽成独立类就得先把 8+ 个内部依赖定义成接口 —— 那是在**实盘台账逻辑**上做手术,
而本批次的目的是"把文件端小、把职责分开", 不是重设计。用 mixin 达成同样目标,
方法体逐字不动, 风险面为零 (踩过的坑: 台账有"恰好一次扣减""失败方向=账本偏多"
两条硬不变量, 见 tests/trade/test_rotation_lots.py)。

**依赖契约** (由 RotationFeature 提供): `_store` / `_gateway` / `_lots` /
`_ledger_max_missing_rounds`。两侧台账的判定差异是**刻意的**, 不是遗漏:
| | 查不到 | 终态 filled=0 |
|---|---|---|
| 卖侧 (`_settle_open_sells`) | 按已成交核销 (扣满额) | 不动 (份额回来) |
| 买侧 (`_settle_open_buys`) | 保留 ≤5 轮后移除, **不扣** | 扣全部记录量 (幻影清零) |
"""
from __future__ import annotations

from datetime import datetime

from trade.book import (
    DIRECTION_BUY,
    TERMINAL_STATUSES,
    label_of,
)
from utils.logger import get_logger

_logger = get_logger("trade.rotation_ledger")

#: 代码 → '简称(代码)' 的人话标签 (唯一实现在 trade/book.label_of, 2026-09-18 收口)。
#: 保留本模块内的旧名 _etf_label, 端出时方法体逐字不动。
_etf_label = label_of


class RotationLedgerMixin:
    """在途买卖台账 + 核销 (配合 RotationFeature 使用, 不单独实例化)。"""

    def _clear_open_ledgers(self) -> list[str]:
        """迁移时清空两侧在途台账 (整表替换为空), 返回被丢弃条目的
        人话描述列表 (逐条审计用, §3.6)。读失败/为空 → 空列表。"""
        import json as _json
        out: list[str] = []
        for key in ("rotation_open_buys", "rotation_open_sells"):
            try:
                raw = self._store.rotation_meta.get(key)
                cur = _json.loads(raw) if raw else {}
            except Exception:
                cur = {}
            if not isinstance(cur, dict):
                cur = {}
            for oid, rec in cur.items():
                out.append(f"{key}: {oid} {rec}")
            self._save_open_ledger(key, {}, merge=False)
        return out

    def _settle_open_sells(self) -> None:
        """开场核销上一轮在途卖单 (自愈, 2026-09-16 错峰簿记配套)。

        只核销「我们自己挂出去的」卖单 (order_id 台账存 rotation_meta);
        人工买卖不动簿记 (仍走 _reconcile_lots 对账告警)。核销口径:
        - 终态: 按已成交量减簿记 (废单 filled=0 → 不动, 份额回来簿记仍在);
        - 查不到 (隔夜委托 QMT 不再返回): 按已成交核销 —— ETF 限价@买一
          隔夜未成交极罕见; 若实际废单, 差额由对账告警兜给人看 (方向可见);
        - 仍在途: 保留台账, 簿记不动 (份额还冻在挂单里)。

        与买侧的判定差异见 _settle_open_buys docstring 的表 (卖侧「查不到」
        按全额扣, 买侧「查不到」保留 ≠5 轮 —— 两侧语义不同, 刻意不合并)。
        """
        import json
        try:
            raw = self._store.rotation_meta.get("rotation_open_sells")
            pending = json.loads(raw) if raw else {}
            if not isinstance(pending, dict) or not pending:
                return
        except Exception:
            return
        try:
            ods = {o["order_id"]: o for o in self._gateway.query_orders()}
        except Exception as e:
            # 2026-09-17 §3.4 加固 (M2): 查询异常 ≠ 查不到 —— 一次超时若
            # 并进「查不到」会按全额扣簿记, 失败方向错误 → 立即早退,
            # 台账与簿记都不动 + 留审计
            _logger.warning("卖单台账查询异常 (台账与簿记均不动): %s", e)
            self._store.write_audit(
                "rotation_settle_query_failed",
                f"在途卖单回报查询异常, 本轮不核销 (台账不动): {e}",
                {"side": "sell", "error": str(e), "pending": len(pending)})
            return
        remaining: dict = {}
        for oid, rec in pending.items():
            try:
                i, code, qty = int(rec[0]), str(rec[1]), int(rec[2])
            except (TypeError, ValueError, IndexError):
                continue
            o = ods.get(oid)
            if o is None:
                self._reduce_lot(i, code, qty)          # 隔夜查不到 → 按成交核销
            elif o.get("status") in TERMINAL_STATUSES:
                filled = int(o.get("filled_qty") or 0)
                if filled > 0:
                    self._reduce_lot(i, code, min(filled, qty))
            else:
                remaining[oid] = [i, code, qty]         # 仍在途 → 留台账
        self._save_open_ledger("rotation_open_sells", remaining, merge=False)

    def _reduce_lot(self, tranche: int, code: str, qty: int) -> None:
        """扣份内簿记 (扣到 ≤0 时**删内存行** —— 2026-09-17 §3.5/L1:
        残留的 0 行会让逐份状态里的 entry_high 变脏)。"""
        key = (tranche, code)
        lot = self._lots.get(key)
        if lot is None:
            return
        lot["qty"] = max(0, lot["qty"] - qty)
        if lot["qty"] <= 0:
            self._lots.pop(key, None)

    def _save_open_ledger(self, key: str, entries: dict,
                          merge: bool = True) -> bool:
        """在途台账落盘 (rotation_meta JSON, 买卖两侧共用一份实现, §3.9)。
        key ∈ {rotation_open_sells, rotation_open_buys}。
        merge=True 只做新增 (下单期: 本轮新挂单 + 上轮仍在途); merge=False
        **整表替换** (核销期与 pass 末 —— 写出的集合 = 上一轮在途且此刻仍
        非终态 ∪ 本轮新下且仍非终态, 防跨轮残留双扣, H1)。
        返回是否写入成功; 失败只告警不抛 (写失败的不变量由调用方守:
        买侧核销失败 → 整条跳过不扣, 失败方向 = 账本偏多, §3.2/H2)。"""
        import json
        try:
            if merge:
                cur = self._read_open_ledger(key)
                cur.update(entries)
                entries = cur
            self._store.rotation_meta.set(key, json.dumps(entries))
            return True
        except Exception as e:
            _logger.warning("在途台账 %s 落库失败 (不扣簿记, 对账兜底): %s",
                            key, e)
            return False

    def _read_open_ledger(self, key: str) -> dict:
        """读在途台账原始 dict (不校验条目格式)。JSON 坏 / 顶层不是 dict →
        审计 + 备份到 `<key>.bak` 后返回 {} (2026-09-17 §3.4 / L2:
        损坏绝不静默丢)。"""
        import json
        try:
            raw = self._store.rotation_meta.get(key)
        except Exception as e:
            _logger.warning("在途台账 %s 读取失败 (按空处理): %s", key, e)
            return {}
        if not raw:
            return {}
        try:
            cur = json.loads(raw)
        except Exception as e:
            self._ledger_corrupt(key, raw, f"JSON 解析失败: {e}")
            return {}
        if not isinstance(cur, dict):
            self._ledger_corrupt(key, raw, f"顶层不是 dict 而是 {type(cur).__name__}")
            return {}
        return cur

    def _ledger_corrupt(self, key: str, raw: str, why: str) -> None:
        """台账损坏处置 (§3.4/L2): 原文备份到 `<key>.bak` + 写审计;
        备份写入自身失败也不抛 (交易链不能被台账损坏带停)。"""
        _logger.warning("在途台账 %s 损坏 (%s), 备份到 %s.bak", key, why, key)
        try:
            self._store.rotation_meta.set(f"{key}.bak", str(raw))
        except Exception as e:
            _logger.warning("在途台账 %s 损坏备份失败: %s", key, e)
        try:
            self._store.write_audit(
                "rotation_open_ledger_corrupt",
                f"在途台账 {key} 损坏 ({why}), 已备份到 {key}.bak; 本轮按空"
                f"台账处理 (在途单不再核销, 由漂移告警与 "
                f"rotation_lots_understated 闸兜底)",
                {"key": key, "reason": why, "raw": str(raw)[:500]})
        except Exception:
            pass

    def _settle_open_buys(self) -> None:
        """在途买单核销 —— 幻影持仓纠偏 (2026-09-17 §3.2/§3.4, 唯一扣减点)。

        台账条目 = rotation_meta 的 rotation_open_buys:
            {order_id: [份序号, 代码, 记录量, 已核销量, 委托时间]}

        **判定表 (买侧, §3.4 唯一真相源; 卖侧口径不同见 _settle_open_sells)**
        | QMT 回报 | 处置 |
        | 查询**抛异常** | 立即 return, 台账与簿记都不动 + 审计 |
        | 终态, filled_qty > 0 | 扣 记录量−filled_qty (部成/部撤) |
        | 终态, filled_qty = 0 | 扣全部记录量 (废单/已撤, 幻影清零) |
        | 非终态 (在途) | 台账留下, 簿记不动 |
        | 查不到 | 保留并计数, 最多 5 轮 (每轮审计); 超期移除 + 审计, **不扣** |
        | 回报代码/方向/交易日不符 | 跳过 + 审计 (单号复用防错扣) |

        **顺序倒置 (H2)**: 先把「已扣后」的台账状态落库成功, **才**改内存
        簿记 —— 台账写失败 → 整条跳过不扣 (失败方向 = 账本偏多, 绝不少记)。
        第二次调用 (pass 末) 对已扣完的条目不再扣 (条目已不在或已核销量满)。
        """
        pending = self._read_open_ledger("rotation_open_buys")
        if not pending:
            return
        try:
            ods = {o["order_id"]: o for o in self._gateway.query_orders()}
        except Exception as e:
            # 查询异常 ≠ 查不到 (M2): 台账不动, 一条不扣
            _logger.warning("在途买单回报查询异常 (台账与簿记均不动): %s", e)
            self._store.write_audit(
                "rotation_settle_query_failed",
                f"在途买单回报查询异常, 本轮不核销 (台账不动): {e}",
                {"side": "buy", "error": str(e), "pending": len(pending)})
            return
        remaining: dict = {}
        for oid, rec in pending.items():
            try:
                i, code, qty = int(rec[0]), str(rec[1]), int(rec[2])
                done = int(rec[3]) if len(rec) > 3 else 0
                ots = rec[4] if len(rec) > 4 else None
            except (TypeError, ValueError, IndexError):
                # 单条格式坏 → 审计不静默丢 (L2), 保留原条目留给人看
                self._store.write_audit(
                    "rotation_buy_ledger_corrupt",
                    f"在途买单条目格式坏, 原样保留不核销: {oid} {rec}",
                    {"order_id": oid, "entry": str(rec)[:200]})
                remaining[oid] = rec
                continue
            o = ods.get(oid)
            if o is None:
                # 查不到 (M1): 不放弃自愈也不无限留 —— 保留 ≤5 轮, 每轮审计
                tries = int(rec[5]) if len(rec) > 5 else 0
                tries += 1
                if tries <= self._ledger_max_missing_rounds:
                    remaining[oid] = [i, code, qty, done, ots, tries]
                    self._store.write_audit(
                        "rotation_buy_ledger_missing",
                        f"在途买单 {oid} ({_etf_label(code)} {qty}份) 查不到, "
                        f"保留第 {tries}/{self._ledger_max_missing_rounds} 轮",
                        {"order_id": oid, "code": code, "qty": qty,
                         "tries": tries})
                else:
                    self._store.write_audit(
                        "rotation_buy_ledger_expired",
                        f"在途买单 {oid} ({_etf_label(code)} {qty}份) 连续 "
                        f"{tries - 1} 轮查不到, 超期移除 (不扣簿记, 留给人看)",
                        {"order_id": oid, "code": code, "qty": qty,
                         "tries": tries - 1})
                    # 不扣: 份额可能真的成交了而回报查不到, 扣了就是「少记」
                continue
            # 单号复用防护 (M3): 代码/方向/同交易日 三项校验, 不符 → 跳过
            mismatch = None
            if str(o.get("code")) != code:
                mismatch = f"代码 {o.get('code')} ≠ {code}"
            elif int(o.get("direction", -1)) != DIRECTION_BUY:
                mismatch = f"方向 {o.get('direction')} ≠ 买入"
            elif ots and o.get("ts") and (
                    datetime.fromtimestamp(float(o["ts"])).date()
                    != datetime.fromtimestamp(float(ots)).date()):
                mismatch = (f"委托日 {datetime.fromtimestamp(float(o['ts'])).date()}"
                            f" ≠ 台账日 {datetime.fromtimestamp(float(ots)).date()}")
            if mismatch:
                remaining[oid] = rec
                self._store.write_audit(
                    "rotation_buy_ledger_mismatch",
                    f"在途买单 {oid} 回报与台账不符 ({mismatch}), 跳过不扣 "
                    f"(疑似单号复用, 留给人看)",
                    {"order_id": oid, "code": code, "qty": qty,
                     "mismatch": mismatch})
                continue
            if o.get("status") not in TERMINAL_STATUSES:
                remaining[oid] = rec          # 在途: 簿记不动 (份额冻在挂单里)
                continue
            filled = int(o.get("filled_qty") or 0)
            reduce_by = qty - filled - done   # §3.2: (记录量−实际成交量)−已核销量
            if reduce_by <= 0:
                continue                      # 已扣满 → 条目移除 (与扣减同步)
            rec2 = [i, code, qty, done + reduce_by, ots]
            # 先把「已扣后」的台账状态落库成功, 才改内存 (§3.2B 顺序倒置)。
            # 注: 这条定向写用 merge=True (只更新本条, 不动其他在途条目),
            # 作用是「成功性探测」—— 写失败即整条跳过不扣; 真正的整表替换
            # 在本函数末尾 merge=False 完成。「条目移除与其扣减同步」由本条
            # rec2 + _reduce_lot 同处一个循环步保证 (H1)。
            if not self._save_open_ledger("rotation_open_buys", {oid: rec2},
                                          merge=True):
                # H2: 台账写失败 → 整条跳过不扣 (账本偏多方向), 条目留台账
                remaining[oid] = rec
                self._store.write_audit(
                    "rotation_buy_ledger_write_failed",
                    f"在途买单 {oid} 台账落库失败, 本条跳过不扣簿记 "
                    f"(账本偏多方向, 下轮重试)",
                    {"order_id": oid, "code": code,
                     "reduce_by": reduce_by, "done": done})
                continue
            before = (self._lots.get((i, code)) or {}).get("qty")
            self._reduce_lot(i, code, reduce_by)
            if before is None:
                # 份/代码对不上 (份数热变更 / 该行已被卖侧扣到 0 删行, §E8):
                # 不扣 + 审计; 条目按规则移除 (已核销量已落库, 不再重复扣)
                self._store.write_audit(
                    "rotation_buy_ledger_no_lot",
                    f"在途买单 {oid} 核销时份内无对应簿记行 (份{i + 1} "
                    f"{_etf_label(code)}), 跳过扣减 (条目按规则移除)",
                    {"order_id": oid, "tranche": i, "code": code,
                     "reduce_by": reduce_by})
                continue
            self._store.write_audit(
                "rotation_buy_settle",
                f"在途买单核销 {oid} ({_etf_label(code)}): 委托 {qty} 份, "
                f"实际成交 {filled} 份 → 簿记扣回 {reduce_by} 份 "
                f"({before} → {max(0, before - reduce_by)})",
                {"order_id": oid, "tranche": i, "code": code, "qty": qty,
                 "filled": filled, "reduce_by": reduce_by,
                 "before": before, "after": max(0, before - reduce_by)})
        self._save_open_ledger("rotation_open_buys", remaining, merge=False)
