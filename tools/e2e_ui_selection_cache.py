"""前端端到端验证 v3 (2026-07-24 选股结果缓存): 真实浏览器填表单跑两轮回测。

v3: 完成判定 = statusText 回到 '就绪' 且日志出现 "回测完成: N笔交易";
第二轮点击前等待按钮回到运行模式 (防误触停止)。两轮到同一全新区间:
第一轮 miss (慢), 第二轮 hit (应显著快), 交易数一致。
"""
import sys, time, json

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
SHOT_DIR = ".playwright-mcp"

_console = []


def fill_form(page, start, end):
    page.fill("#cfgFormula", "QUANTQQ")
    page.fill("#cfgFormulaArg", "")
    page.select_option("#cfgUniverse", "50")
    page.select_option("#cfgPeriod", "5m")
    page.fill("#cfgStart", start)
    page.fill("#cfgEnd", end)


def run_once(page, label, timeout_s=240):
    t0 = time.perf_counter()
    page.click("#btnRun")
    deadline = t0 + timeout_s
    st, log = "", ""
    while time.perf_counter() < deadline:
        time.sleep(1.5)
        st = page.locator("#statusText").inner_text()
        log = page.locator("#logContent").inner_text() if page.locator("#logContent").count() else ""
        if st == "就绪" and "回测完成" in log:
            break
        if "失败" in log or st == "错误":
            break
    elapsed = time.perf_counter() - t0
    page.screenshot(path=f"{SHOT_DIR}/e2e_ui_{label}.png", full_page=False)
    done = [l for l in log.splitlines() if "回测完成" in l]
    print(f"[{label}] elapsed={elapsed:.1f}s status={st!r} done={done[-1] if done else '??'}", flush=True)
    return elapsed, (done[-1] if done else ""), st


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: _console.append(f"{m.type}: {m.text[:200]}"))
        page.on("pageerror", lambda e: _console.append(f"pageerror: {str(e)[:300]}"))
        page.goto(BASE, wait_until="networkidle")
        print("page title:", page.title())

        fill_form(page, "20260302", "20260319")  # 全新区间, 首轮必 miss
        e1, d1, st1 = run_once(page, "miss")
        e2, d2, st2 = run_once(page, "hit")
        browser.close()

    errs = [c for c in _console if c.startswith(("error", "pageerror"))]
    print("console errors:", json.dumps(errs[:8], ensure_ascii=False))
    print(json.dumps({"miss_s": round(e1, 1), "hit_s": round(e2, 1),
                      "speedup": round(e1 / max(e2, 0.01), 1),
                      "status_miss": st1, "status_hit": st2,
                      "done_miss": d1, "done_hit": d2}, ensure_ascii=False))
    assert "回测完成" in d1 and "回测完成" in d2, f"UI 未渲染完成日志: {d1!r} / {d2!r}"
    assert st1 == "就绪" and st2 == "就绪", f"状态未回到就绪: {st1!r} / {st2!r}"
    assert d1.split("回测完成: ")[-1] == d2.split("回测完成: ")[-1], "两轮交易数不一致"
    assert e2 < e1 * 0.5, f"命中未显著提速: miss={e1:.1f}s hit={e2:.1f}s"
    print("UI E2E PASS")


if __name__ == "__main__":
    sys.exit(main())
