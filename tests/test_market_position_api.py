# -*- coding: utf-8 -*-
"""大盘位置 API 端点单测 (TestClient): /api/market_position/*。

**为什么必须有它（2026-09-17 实测坑）**：`/api/market_position/history` 原来
把 `limit` 写成 `ge=1`，而箱线图 `drawBox()` 恰恰传 `limit=0`（要全量分组）→
FastAPI 返回 **422**（`{"detail": ...}`），前端按 JSON 解析得到 `d.success` 为
undefined → 静默 return → **箱线图整张空白、一个错误提示都没有**。
那条"接口挂在服务上"的验收只证明了路由存在，证明不了**入参契约对得上**。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from market_position_api import router  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    """独立 app（只挂大盘位置路由）+ 录像目录指向 tmp（不碰生产录像）。"""
    from core import market_position_runner as mpr
    monkeypatch.setattr(mpr, "DAILY_PATH", tmp_path / "daily.jsonl")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _seed_records(mpr, n=3):
    """写 n 条最小录像（index 结构要够箱线图用）。"""
    recs = []
    for i in range(n):
        recs.append({"date": f"2026-09-{10 + i:02d}",
                     "indices": {"shanghai": {"pct_10y": 90.9, "regime": "range"},
                                 "hs300": {"pct_10y": 72.9, "regime": "range"},
                                 "chuangyeban": {"pct_10y": 88.4, "regime": "range"}},
                     "breadth": {"above_ma20_pct": 21.2},
                     "turnover": {"amount_pct_1y": 1.6},
                     "valuation": None, "shadow": {},
                     "stale": False, "expected_date": "2026-09-16"})
    mpr._upsert(recs)
    return recs


class TestHistoryEndpoint:
    def test_limit_zero_means_all_not_422(self, client, monkeypatch, tmp_path):
        """回归锁：`limit=0` 必须返回全部记录，不许 422（422 = 箱线图空白）。"""
        from core import market_position_runner as mpr
        _seed_records(mpr, 3)
        r = client.get("/api/market_position/history?limit=0")
        assert r.status_code == 200, f"limit=0 不该是 422: {r.text[:200]}"
        d = r.json()
        assert d["success"] is True
        assert len(d["items"]) == 3, f"limit=0 应返回全部 3 条, 实际 {len(d['items'])}"
        # 箱线图依赖的字段必须在
        it = d["items"][0]
        assert it["indices"]["shanghai"]["regime"] is not None
        assert it["indices"]["shanghai"]["pct_10y"] is not None

    def test_limit_clips_to_latest(self, client):
        from core import market_position_runner as mpr
        _seed_records(mpr, 3)
        r = client.get("/api/market_position/history?limit=2")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 2, "limit=2 应只回最近 2 条"

    def test_out_of_range_still_rejected(self, client):
        """上界仍要拦（防止一次拉爆内存）。"""
        r = client.get("/api/market_position/history?limit=99999")
        assert r.status_code == 422, "超上限仍应 422"

    def test_box_contract_fields_present(self, client):
        """箱线图契约：每条都要有 indices.shanghai.{pct_10y, regime}（分组依据）。"""
        from core import market_position_runner as mpr
        _seed_records(mpr, 2)
        d = client.get("/api/market_position/history?limit=0").json()
        for it in d["items"]:
            sh = it["indices"]["shanghai"]
            assert "pct_10y" in sh and "regime" in sh, \
                f"箱线图分组字段缺失: {list(sh.keys())}"
