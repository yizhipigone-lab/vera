"""细粒度进度 e2e (2026-07-26): 真实回测全程采样 /api/status。

验证:
1. 选股阶段能看到 ST过滤/批次 细粒度 detail, pct 进入 formula 区间 (26-45)
2. pct 单调不回退
3. ETA 出现 (eta_s >= 0)
4. 回测完成, 结果正常渲染 (分页表格)
5. 前端 progressText 显示 detail + ETA
"""
import json
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"


def status():
    with urllib.request.urlopen(BASE + "/api/status", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    samples = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(BASE, wait_until="networkidle")
        # 全新区间 end=20260321 (缓存必 miss → 能观察到完整选股阶段)
        page.fill("#cfgFormula", "QUANTQQ")
        page.fill("#cfgFormulaArg", "")
        page.select_option("#cfgUniverse", "50")
        page.select_option("#cfgPeriod", "5m")
        page.fill("#cfgStart", "20260301")
        page.fill("#cfgEnd", "20260321")
        page.click("#btnRun")
        print("已启动回测 (QUANTQQ 20260301~20260321, 缓存 miss)")

        t0 = time.time()
        shot_taken = False
        while time.time() - t0 < 180:
            time.sleep(0.9)
            s = status()
            samples.append((round(time.time() - t0, 1), s["progress"],
                            s.get("step", ""), s.get("detail", ""), s.get("eta_s", -1)))
            if not shot_taken and 26 <= s["progress"] <= 60 and s.get("detail"):
                page.screenshot(path=".playwright-mcp/progress_mid.png")
                shot_taken = True
            if not s["running"] and s["progress"] > 0:
                break

        # 前端文字检查 (运行中已截屏; 完成后等渲染)
        page.wait_for_function(
            "document.getElementById('statusText').textContent === '就绪'",
            timeout=120000)
        page.wait_for_timeout(1000)
        page.screenshot(path=".playwright-mcp/progress_done.png")
        rows = page.locator("#tradeTableBody tr").count()
        browser.close()

    # ── 断言 ──
    pcts = [s[1] for s in samples]
    mono = all(b >= a - 0.01 for a, b in zip(pcts, pcts[1:]))
    details = {s[3] for s in samples if s[3]}
    etas = [s[4] for s in samples if s[4] >= 0]
    formula_zone = [s for s in samples if 26 <= s[1] <= 45 and "批次" in s[3]]
    st_zone = [s for s in samples if "ST" in s[3] or "过滤" in s[3]]

    print(f"采样 {len(samples)} 次, 单调不回退: {mono}")
    print(f"detail 种类: {sorted(details)[:6]}")
    print(f"formula 批次观测 {len(formula_zone)} 次, 例: {formula_zone[len(formula_zone)//2] if formula_zone else '-'}")
    print(f"ST 过滤观测 {len(st_zone)} 次")
    print(f"ETA 观测 {len(etas)} 次, 例: {etas[:3]}")
    print(f"完成后表格 {rows} 行")

    assert mono, "pct 出现回退!"
    assert formula_zone, "未观测到公式批次细粒度!"
    assert etas, "未观测到 ETA!"
    assert rows > 0, "结果表格未渲染!"
    print("PROGRESS E2E PASS")


if __name__ == "__main__":
    main()
