# -*- coding: utf-8 -*-
"""
portal/virtual/selfcheck_p4.py — P4 融合自检（全部离线）

覆盖 V1–V7：

    V1  游资 analyzer      —— 有池子/无池子两种情形；分节完整
    V2  情绪不进评分        —— 换一套情绪数据，个股评分必须完全不变（核心约束）
    V3  前瞻收益口径        —— T+1 开盘买、扣一次成本、开盘涨停必须剔除
    V4  数据集拼装          —— 每种丢弃原因都要计数，不许静默丢样本
    V5  工具契约            —— 4 把工具、schema 合法、失败返回 ok=False 而不是抛错
    V6  报告自包含          —— 外部引用必须为 0
    V7  发报前审计          —— 审计不通过时**不写文件**

运行：由 `python -m portal.virtual.selfcheck` 统一调用。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault(
    "PORTAL_VIRTUAL_ROOT",
    str(Path(tempfile.mkdtemp(prefix="portal_virtual_p4_")) / "virtual"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portal.virtual import dataset, paths, report, rules, tagging, toolbox  # noqa: E402
from portal.virtual._check import Check, close_enough  # noqa: E402


def _pool(n_zt: int = 2, n_zb: int = 1, n_dt: int = 1,
          seal_ratio: float = 0.5, boards: float = 1) -> dict:
    zt = [{"code": "600001", "name": "甲", "boards": boards,
           "seal_ratio_pct": seal_ratio, "seal_amount": 2e7, "float_cap": 5e9,
           "first_seal": "093512", "break_count": 0}]
    for i in range(2, n_zt + 1):
        zt.append({"code": f"60000{i}", "name": f"票{i}", "boards": 1,
                   "seal_ratio_pct": 1.5, "seal_amount": 1e7, "float_cap": 5e9,
                   "first_seal": "100000", "break_count": 0})
    return {
        "date": "20261008",
        "zt": zt,
        "zb": [{"code": f"6001{i:02d}"} for i in range(n_zb)],
        "dt": [{"code": f"6002{i:02d}"} for i in range(n_dt)],
        "errors": {},
    }


def _analyzer(pool_doc: dict):
    from portal.analyzers.yuzi import YuziAnalyzer

    class _Fake(YuziAnalyzer):
        def _fetch_pools(self, date8):        # noqa: D102
            return pool_doc

        def _fetch_dragon_tiger(self, code):  # noqa: D102
            return {}

    return _Fake()


def _kline(n: int = 60, start: str = "2026-07-01") -> pd.DataFrame:
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]
    closes = list(np.linspace(10.0, 12.0, n))
    return pd.DataFrame({
        "date": dates, "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1_000_000] * n,
    })


# ─────────────────────────────────────────────────────────────
# V1 · 游资 analyzer
# ─────────────────────────────────────────────────────────────
def check_analyzer() -> Check:
    c = Check("V1", "游资 analyzer：有/无池子两种情形都能出结果，不抛异常")

    a = _analyzer(_pool())
    r = a.analyze(stock_code="600001", stock_name="甲", df=_kline(), modules=[
        "limit_up_status", "buyability", "emotion_brake"], llm_call=None, search=None)
    keys = [s.key for s in r.sections]
    c.expect(keys == ["limit_up_status", "buyability", "emotion_brake"],
             f"三节齐全：{keys}")
    c.expect(r.error == "", "无错误")
    c.expect(0 <= r.score <= 100, f"评分在 0-100：{r.score}")
    c.expect(r.dimension == "yuzi", "维度名正确")

    buy_sec = next(s for s in r.sections if s.key == "buyability")
    c.expect(buy_sec.data["bucket"] == "buyable",
             f"封单/流通 0.5% → 可买（实际 {buy_sec.data['bucket']}）")

    emo = next(s for s in r.sections if s.key == "emotion_brake")
    c.expect(emo.data.get("usage") == "brake_only", "情绪节标注 usage=brake_only")
    c.expect(emo.signal == "hold", "情绪节 signal 恒为 hold（不表达方向）")
    c.expect("禁止" in emo.content and "同步指标" in emo.content,
             "情绪节正文写明用途约束")

    # 不在池子里 → 照常返回，不报错
    r2 = a.analyze(stock_code="000999", stock_name="乙", df=_kline(),
                   modules=["limit_up_status", "buyability"], llm_call=None, search=None)
    c.expect(r2.error == "", "不在涨停池时不报错")
    c.expect("不在今日涨停池" in r2.sections[0].content, "明确说明不在池中")

    # 数据源整体不可用 → 降级，不抛
    class _Broken(_analyzer(_pool()).__class__):
        def _fetch_pools(self, date8):  # noqa: D102
            raise RuntimeError("来源挂了")

    r3 = _Broken().analyze(stock_code="600001", stock_name="甲", df=_kline(),
                           modules=["limit_up_status", "buyability", "emotion_brake"],
                           llm_call=None, search=None)
    c.expect(r3.error == "", "池子来源挂掉时仍返回结果（降级不崩）")
    c.expect("—" in r3.sections[-1].content or "未知" in r3.sections[1].content,
             "降级时显式标注缺数据，而不是编造")
    return c


# ─────────────────────────────────────────────────────────────
# V2 · 情绪不许进评分（核心约束）
# ─────────────────────────────────────────────────────────────
def check_emotion_not_in_score() -> Check:
    c = Check("V2", "情绪读数不进个股评分：换一套情绪数据，评分必须完全不变")

    calm = _pool(n_zb=0, n_dt=0)
    wild = _pool(n_zt=2, n_zb=25, n_dt=9)      # 同样的个股，但市场情绪完全不同

    ra = _analyzer(calm).analyze(stock_code="600001", stock_name="甲", df=_kline(),
                                 modules=["limit_up_status", "buyability", "emotion_brake"],
                                 llm_call=None, search=None)
    rb = _analyzer(wild).analyze(stock_code="600001", stock_name="甲", df=_kline(),
                                 modules=["limit_up_status", "buyability", "emotion_brake"],
                                 llm_call=None, search=None)

    c.expect(ra.score == rb.score,
             f"情绪从 0 炸板变成 25 炸板，评分不变：{ra.score} vs {rb.score}")
    c.expect(ra.signal == rb.signal, "信号也不变")

    ea = next(s for s in ra.sections if s.key == "emotion_brake").data
    eb = next(s for s in rb.sections if s.key == "emotion_brake").data
    c.expect(ea["zb_n"] != eb["zb_n"], f"情绪读数确实变了（{ea['zb_n']} → {eb['zb_n']}）")

    # 反证：个股级事实变了，评分必须变（说明评分不是死的）
    rc = _analyzer(_pool(seal_ratio=18.0, boards=1)).analyze(
        stock_code="600001", stock_name="甲", df=_kline(),
        modules=["limit_up_status", "buyability"], llm_call=None, search=None)
    c.expect(rc.score < ra.score,
             f"封单/流通 18%（买不到）应降低评分：{rc.score} < {ra.score}")
    return c


# ─────────────────────────────────────────────────────────────
# V3 · 前瞻收益口径
# ─────────────────────────────────────────────────────────────
def check_forward_return() -> Check:
    c = Check("V3", "前瞻收益：T+1 开盘买、扣一次成本、开盘涨停必须剔除")

    from portal import backtest_core as core

    n = 30
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-07-01", periods=n)]
    closes = [10.0 + i * 0.1 for i in range(n)]
    df = pd.DataFrame({"date": dates, "open": closes,
                       "high": [x * 1.01 for x in closes],
                       "low": [x * 0.99 for x in closes], "close": closes,
                       "volume": [1e6] * n})

    fwd = core.forward_return_after(df, "2026-07-10", 1, code="600000")
    c.expect(fwd is not None, "取到一笔前瞻收益")
    idx_sig = dates.index("2026-07-10")
    exp_entry = dates[idx_sig + 1]
    exp_exit = dates[idx_sig + 2]
    c.expect(fwd["entry_date"] == exp_entry,
             f"入场是信号日之后的第一个交易日：{fwd['entry_date']} == {exp_entry}")
    c.expect(fwd["exit_date"] == exp_exit, f"出场是再下一日：{fwd['exit_date']}")
    c.expect(fwd["entry_date"] != dates[idx_sig],
             f"不是信号日当天买入（信号 {dates[idx_sig]}，入场 {fwd['entry_date']}）")

    gross = (closes[idx_sig + 2] / closes[idx_sig + 1] - 1) * 100
    c.expect(close_enough(fwd["gross_pct"], round(gross, 4), 1e-3),
             f"毛收益 = 次日开盘→次日收盘：{fwd['gross_pct']} vs {gross:.4f}")
    c.expect(close_enough(fwd["net_pct"], round(gross - core.COST_ROUND_TRIP * 100, 4), 1e-3),
             f"净收益只扣一次成本：{fwd['net_pct']}")

    # 开盘即涨停 → 必须标记 excluded
    n2 = 20
    d2 = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-07-01", periods=n2)]
    closes2 = [10.0] * n2
    opens2 = list(closes2)
    i_sig = 5
    opens2[i_sig + 1] = closes2[i_sig] * 1.10      # 次日开盘涨停
    closed2 = list(closes2)
    closed2[i_sig + 2] = 11.0
    df2 = pd.DataFrame({"date": d2, "open": opens2,
                        "high": [max(o, c) for o, c in zip(opens2, closed2)],
                        "low": [min(o, c) for o, c in zip(opens2, closed2)],
                        "close": closed2, "volume": [1e6] * n2})
    fwd2 = core.forward_return_after(df2, d2[i_sig], 1, code="600000")
    c.expect(fwd2 is not None and fwd2["excluded"] == "limit_up_open",
             f"开盘即涨停被标记 excluded：{(fwd2 or {}).get('excluded')}")

    c.expect(core.forward_return_after(df, dates[-1], 1) is None,
             "信号日是最后一根 → 返回 None（不猜）")
    c.expect(core.forward_return_after(df, "2026-07-10", 99) is None,
             "持有期超出数据范围 → None")
    return c


# ─────────────────────────────────────────────────────────────
# V4 · 数据集拼装
# ─────────────────────────────────────────────────────────────
def check_dataset() -> Check:
    c = Check("V4", "数据集拼装：每种丢弃原因都计数，不许静默丢样本")

    stock = {"code": "600001", "name": "甲", "prev_close": 10.0,
             "seal_amount": 2e7, "float_cap": 5e9, "seal_ratio_pct": 0.5,
             "boards": 1, "price": 11.0, "turnover_pct": 3.0,
             "break_count": 0, "first_seal": "093512"}
    doc = {"date": "20260710", "stage": "pre",
           "market": {"zt_n": 50, "zb_rate": 0.15},
           "stocks": [{"code": "600001", "name": "甲",
                       "pre": {"zone_pos_120": 0.2, "lbc": 1}, "post": None},
                      {"code": "600002", "name": "乙",      # 没有 K 线
                       "pre": {"zone_pos_120": 0.3}, "post": None}]}

    rows, stats = dataset.build_rows([doc], {"600001": _kline()}, horizon=1)
    c.expect(stats["kept"] == 1, f"保留 1 行（实际 {stats['kept']}）")
    c.expect(stats["no_kline"] == 1, f"缺 K 线被计数（实际 {stats['no_kline']}）")
    c.expect(len(rows) == 1, "返回行数为 1")

    row = rows[0]
    c.expect("ret_pct" in row and isinstance(row["ret_pct"], float), "行里带 ret_pct")
    c.expect("m_zt_n" in row and "zone_pos_120" in row, "特征字段已摊平（含 m_ 前缀）")
    c.expect(row["date"] == "20260710", "带日期，供分年/分月统计")

    # 找不到收益时也要计数
    rows2, stats2 = dataset.build_rows(
        [{"date": "20991231", "market": {},
          "stocks": [{"code": "600001", "name": "甲", "pre": {}, "post": None}]}],
        {"600001": _kline()})
    c.expect(stats2["no_return"] == 1 and rows2 == [],
             f"信号日超出数据范围 → 计入 no_return（实际 {stats2['no_return']}）")
    return c


# ─────────────────────────────────────────────────────────────
# V5 · 工具契约
# ─────────────────────────────────────────────────────────────
def check_toolbox() -> Check:
    c = Check("V5", "工具契约：4 把工具、schema 合法、失败必须 ok=False 而不是抛错")
    rules.reset_for_tests()

    tools = toolbox.build_tools()
    c.expect(len(tools) == 4, f"4 把工具（实际 {len(tools)}）")
    names = {t.name for t in tools}
    c.expect(names == {"freeze_virtual_plan", "settle_virtual_plan",
                       "backtest_virtual_rule", "query_virtual_memory"},
             f"工具名齐全：{sorted(names)}")
    for t in tools:
        c.expect(bool(t.description), f"{t.name} 有描述")
        c.expect(all(getattr(p, "name") for p in t.parameters), f"{t.name} 参数名齐全")

    man = toolbox.manifest()
    c.expect(len(man["tools"]) == 4 and len(man["forbidden"]) == 5,
             "manifest 含 4 工具 + 5 条禁则")

    # 注册到假 registry
    class _Reg:
        def __init__(self): self.items = []
        def register(self, t): self.items.append(t)

    reg = _Reg()
    out = toolbox.register(reg)
    c.expect(out["ok"] and out["registered"] == 4, f"注册 4 把：{out}")

    # 非法计划 → ok=False，不抛
    bad = toolbox.freeze_virtual_plan({"picks": []})
    c.expect(bad["ok"] is False and bad.get("problems"), "非法计划返回 ok=False + problems")

    # 不存在的计划结算 → ok=False（SettleAbort 被转成可叙述的失败）
    r = toolbox.settle_virtual_plan("NOPE")
    c.expect(r["ok"] is False and "error" in r, "结算失败返回 ok=False")

    # 非法表达式 → ok=False
    r2 = toolbox.backtest_virtual_rule({"field": "x", "op": "?", "value": 1})
    c.expect(r2["ok"] is False and r2.get("problems"), "非法表达式返回 ok=False")

    # 样本不足 → 明确说缺什么，不拿空气当证据
    r3 = toolbox.backtest_virtual_rule(
        {"all": [{"field": "zone_pos_120", "op": "<", "value": 0.3}]})
    c.expect(r3["ok"] is False and "样本" in r3["error"],
             f"样本不足时明确说明：{r3.get('error', '')[:40]}")

    # 只读查询
    q = toolbox.query_virtual_memory("rules")
    c.expect(q["ok"] and q["count"] == 0, "空规则库可查询")
    q2 = toolbox.query_virtual_memory("nonsense")
    c.expect(q2["ok"] is False, "未知 kind 返回 ok=False")
    return c


# ─────────────────────────────────────────────────────────────
# V6 · 报告自包含
# ─────────────────────────────────────────────────────────────
def check_report_selfcontained() -> Check:
    c = Check("V6", "报告自包含：外部引用必须为 0，关键区块齐全")

    payload = {
        "title": "测试复盘",
        "caliber": "T+1 开盘买、扣费 0.10%",
        "kpis": [{"label": "总资产", "value": 100123.45, "display": "100,123.45"},
                 {"label": "当日盈亏", "value": 123.45}],
        "gate": {"passed": True, "checks": [
            {"code": "A", "passed": True, "detail": "上证竞价 +0.2%"},
            {"code": "B", "passed": False, "detail": "只有 1 只落区间"},
        ]},
        "rulers": {"index": {"ret_pct": 0.5},
                   "attribution": {"actual_pct": 1.2, "shadow_pct": 0.9,
                                   "baseline_pct": -0.3, "execution_pp": 0.3,
                                   "selection_pp": 1.2,
                                   "basis": "统一到投入资金"}},
        "rules": [{"rule_id": "R1", "version": 1, "status": "hypothesis",
                   "scope": "entry", "n": 100, "mean_pct": 0.5,
                   "after_cost_pct": 0.4, "desc": "测试规则",
                   "flags": ["thin_margin:0.4"]}],
        "text": "情绪读数仅用于仓位。",
        "claims": [], "numbers": [],
        "params_text": {"z1": "关闭"}, "params_machine": {"z1": "关闭"},
        "backtest": None,
        "known_bias_declared": True,
    }
    html_text = report.render(payload)
    c.expect(report.external_refs(html_text) == [],
             f"外部引用为 0：{report.external_refs(html_text)}")
    c.expect("<style>" in html_text and "cdn" not in html_text.lower(),
             "样式内联，没有 CDN")
    for token in ("开仓前提打分", "三把零自由度尺子", "规则库", "不构成投资建议"):
        c.expect(token in html_text, f"包含区块「{token}」")
    c.expect("100,123.45" in html_text, "KPI 原样展示")

    # 转义：内容里的尖括号不能破坏页面
    evil = report.render({**payload, "title": "<script>alert(1)</script>"})
    c.expect("<script>alert(1)</script>" not in evil, "标题被 HTML 转义")
    return c


# ─────────────────────────────────────────────────────────────
# V7 · 发报前审计拦截
# ─────────────────────────────────────────────────────────────
def check_report_gate() -> Check:
    c = Check("V7", "发报前审计：不通过时不写文件")

    out_dir = paths.VIRTUAL_ROOT / "reports_test"
    blocked = {
        "title": "有问题的报告",
        "text": "情绪冰点，明日必然反转。",        # A4 必拦
        "claims": [{"action": "buy", "code": "600001"}],   # A1 缺三件套
        "numbers": [], "params_text": {}, "params_machine": {},
        "backtest": {"after_cost_pct": -0.1, "n": 5, "insufficient_labeled": False},
        "known_bias_declared": False,
    }
    r = report.build_and_write(blocked, filename="blocked.html", out_dir=out_dir)
    c.expect(r["ok"] is False and r["written"] is False, "审计不通过 → 不写")
    c.expect(not (out_dir / "blocked.html").exists(), "磁盘上确实没有这个文件")
    codes = {f["code"] for f in r["audit"]["findings"]}
    c.expect({"A1", "A4", "A5", "A6", "A7"} <= codes, f"多条同时被拦：{sorted(codes)}")

    good = {
        "title": "干净的报告",
        "text": "情绪读数仅用于仓位档位，不构成方向判断。",
        "claims": [], "numbers": [], "params_text": {}, "params_machine": {},
        "backtest": None, "known_bias_declared": True,
    }
    r2 = report.build_and_write(good, filename="good.html", out_dir=out_dir)
    c.expect(r2["ok"] is True and r2["written"] is True, "审计通过 → 写盘")
    p = Path(r2["path"])
    c.expect(p.exists(), f"文件存在：{p.name}")
    text = p.read_text(encoding="utf-8")
    c.expect(report.external_refs(text) == [], "写出的文件同样自包含")
    p.unlink()
    return c


CHECKS = (
    ("V1", check_analyzer),
    ("V2", check_emotion_not_in_score),
    ("V3", check_forward_return),
    ("V4", check_dataset),
    ("V5", check_toolbox),
    ("V6", check_report_selfcontained),
    ("V7", check_report_gate),
)


def run() -> dict:
    results = []
    for _, fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:
            import traceback
            chk = Check("ERR", f"{fn.__name__} 执行异常")
            chk.expect(False, f"{type(exc).__name__}: {exc}")
            chk.note(traceback.format_exc().splitlines()[-1])
            results.append(chk)
    passed = all(c.passed for c in results)
    return {"passed": passed, "n_checks": len(results),
            "n_failed": sum(1 for c in results if not c.passed),
            "checks": [c.to_dict() for c in results]}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    rep = run()
    if "--json" in argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["passed"] else 1
    print("=" * 72)
    print("portal.virtual 自检 · P4 融合")
    print("=" * 72)
    for chk in rep["checks"]:
        print(f"\n[{'PASS' if chk['passed'] else 'FAIL'}] {chk['id']} · {chk['title']}")
        for line in chk["detail"]:
            print(line)
    print("\n" + "=" * 72)
    print(f"结果：{rep['n_checks'] - rep['n_failed']}/{rep['n_checks']} 通过")
    print("=" * 72)
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
