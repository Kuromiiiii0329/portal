# -*- coding: utf-8 -*-
"""
portal/virtual/verify_report.py — 带图表的报告必须做**真实渲染**校验

《回测避坑手册》与 SKILL.md §4 反复强调同一个坑：

    **「生成了文件」≠「图表能画」。**

原包踩过：HTML 引了 CDN 的 echarts，本机网络抖动导致 `pageerror: echarts is not defined`，
整页图表全灭，但文件大小、结构、正则检查全都正常。所以唯一可信的判据是
**真的用浏览器渲染一遍，数 canvas，并收集 pageerror**。

本模块用 Playwright + 本机 Edge（`channel="msedge"`，零下载；本机没装 chromium 时
`launch()` 会报 `Executable doesn't exist`）。

用法：
    python -m portal.virtual.verify_report <html路径> [--expect-canvas N]
    python -m portal.virtual.verify_report --selftest     # 用已知坏样本验证探针有效
退出码：0 = 通过；1 = 失败。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 30000


def check_html(path: str | Path, *, expect_canvas: int | None = None,
               headless: bool = True, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """渲染并检查。返回结构化结果（不抛异常，除导入失败外）。"""
    p = Path(path).resolve()
    if not p.exists():
        return {"ok": False, "error": f"文件不存在：{p}"}
    if not p.name.lower().endswith((".html", ".htm")):
        return {"ok": False, "error": "只接受 .html"}

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {"ok": False, "error": f"未安装 playwright：{exc}",
                "hint": "pip install playwright（本机用 Edge，无需 electron/chromium 下载）"}

    errors: list[str] = []
    console_errors: list[str] = []
    result: dict[str, Any] = {"ok": False, "file": str(p)}

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge", headless=headless)
        except Exception:
            browser = pw.chromium.launch(headless=headless)   # 回退到自带 chromium
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)
        try:
            # 自包含文件用 file:// 打开；wait_until=load 在 CDN 场景会卡死，
            # 这里的文件不联网，所以 load 是安全的。
            page.goto(p.as_uri(), wait_until="load", timeout=timeout)
            page.wait_for_timeout(600)
            canvases = page.eval_on_selector_all("canvas", "els => els.length")
            chart_count = page.evaluate("() => (window.__PORTAL_CHARTS__ || 0)")
            echarts_loaded = page.evaluate("() => (typeof echarts !== 'undefined')")
            title = page.title()
        finally:
            browser.close()

    body = p.read_text(encoding="utf-8", errors="ignore")
    result.update({
        "title": title,
        "canvases": canvases,
        "charts_initialized": chart_count,
        "echarts_defined": echarts_loaded,
        "page_errors": errors[:10],
        "console_errors": console_errors[:10],
        "bytes": len(body.encode("utf-8")),
        "has_external_refs": bool(__import__("re").search(
            r"""(?:<script[^>]+src=|<link[^>]+href=)\s*['"]?(?:https?:)?//""", body)),
    })

    problems: list[str] = []
    if errors:
        problems.append(f"pageerror {len(errors)} 条：{errors[0][:160]}")
    if expect_canvas is not None and canvases != expect_canvas:
        problems.append(f"canvas 数 {canvases} != 期望 {expect_canvas}")
    if expect_canvas and canvases == 0:
        problems.append("图表一个都没画出来")
    if chart_count and canvases == 0:
        problems.append(f"初始化了 {chart_count} 个图表，但页面里 0 个 canvas")
    result["problems"] = problems
    result["ok"] = not problems
    return result


def selftest() -> int:
    """证明探针能失败：造一个「引 CDN 且 echarts 未定义」的坏样本。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="verify_report_"))
    bad = tmp / "bad.html"
    bad.write_text(
        "<!DOCTYPE html><html><body><div id='c' style='width:200px;height:100px'></div>"
        "<script>var chart = echarts.init(document.getElementById('c'));</script>"
        "</body></html>", encoding="utf-8")

    r = check_html(bad, expect_canvas=1)
    ok = True
    if r.get("page_errors"):
        print(f"✅ 探针有效：坏样本触发 pageerror（{r['page_errors'][0][:80]}…）")
    else:
        print("❌ 探针无效：坏样本没有触发 pageerror")
        ok = False
    if not r.get("ok"):
        print(f"✅ 探针有效：坏样本判定为失败（{r.get('problems')}）")
    else:
        print("❌ 探针无效：坏样本被判定为通过")
        ok = False

    good = tmp / "good.html"
    good.write_text(
        "<!DOCTYPE html><html><body>"
        "<canvas id='x' width='10' height='10'></canvas></body></html>",
        encoding="utf-8")
    g = check_html(good, expect_canvas=1)
    if g.get("ok"):
        print("✅ 对照样本判定为通过")
    else:
        print(f"❌ 对照样本被误判：{g.get('problems')}")
        ok = False
    for f in (bad, good):
        f.unlink(missing_ok=True)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if not args:
        print(__doc__)
        return 2
    path = args[0]
    expect = None
    if "--expect-canvas" in args:
        expect = int(args[args.index("--expect-canvas") + 1])
    r = check_html(path, expect_canvas=expect)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
