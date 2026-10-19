# -*- coding: utf-8 -*-
"""
portal/virtual/selfcheck.py — P1 验收自检（全部离线，不依赖网络与真实缓存）

与 P0 的 `backtest_audit.py` 同一套哲学：

    一个判据如果「永远不会失败」，它就不是判据。
    所以每条检查都要能塞一个**已知错误**进去，并确认它真的会报错。

覆盖：
    S1 表达式求值器   —— 白名单算子、缺失字段语义、类型不匹配、深度/叶子上限、禁 eval
    S2 规则注册表     —— schema、append-only 版本、迁移白名单、角色权限、拒绝归档
    S3 锚点链         —— 冻结/校验、改一个空格被抓、改 anchors 断链、幂等
    S4 特征层         —— 防时序泄漏、涨停价银行家舍入、区间位、post 回填不猜
    S5 池子与情绪     —— 列名别名、炸板率口径、可买性分档
    S6 题材归因解析   —— 多种外层结构、空结果必须报错
    S7 分时           —— 指数前缀映射、无 0925、1500 优先于 1530、日期护栏

用法：
    python -m portal.virtual.selfcheck
    python -m portal.virtual.selfcheck --json
退出码：0 = 全过；1 = 有失败。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# ── 先隔离数据根，再导入任何会读 paths 的子模块 ──────────────
_TMP = tempfile.mkdtemp(prefix="portal_virtual_selfcheck_")
os.environ["PORTAL_VIRTUAL_ROOT"] = str(Path(_TMP) / "virtual")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portal.virtual import anchor, expr as expr_mod, pools, rules, tagging  # noqa: E402
from portal.virtual import minute_data, paths, ths_reason  # noqa: E402
from portal.virtual._check import Check  # noqa: E402


# ─────────────────────────────────────────────────────────────
# 断言工具：统一由 _check.Check 提供（P2 自检共用同一套）
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# S1 · 表达式求值器
# ─────────────────────────────────────────────────────────────
def check_expr() -> Check:
    c = Check("S1", "表达式求值器：白名单算子 / 缺失字段 / 类型不匹配 / 上限 / 禁 eval")

    ok = {"all": [{"field": "zone_pos_120", "op": "<", "value": 0.30}]}
    c.expect(expr_mod.is_valid(ok), "合法表达式通过校验")
    c.expect(expr_mod.evaluate(ok, {"zone_pos_120": 0.15}), "0.15 < 0.30 → 满足")
    c.expect(not expr_mod.evaluate(ok, {"zone_pos_120": 0.55}), "0.55 < 0.30 → 不满足")

    # any / not / between / in / is_null
    any_e = {"any": [{"field": "lbc", "op": ">=", "value": 5},
                     {"field": "seal_ratio_pct", "op": "<", "value": 1.0}]}
    c.expect(expr_mod.evaluate(any_e, {"lbc": 1, "seal_ratio_pct": 0.3}), "any：第二项成立即通过")
    c.expect(not expr_mod.evaluate(any_e, {"lbc": 1, "seal_ratio_pct": 5}), "any：都不成立则不通过")
    c.expect(expr_mod.evaluate({"not": {"field": "lbc", "op": ">=", "value": 2}}, {"lbc": 1}),
             "not：1 >= 2 为假 → 取反为真")
    c.expect(expr_mod.evaluate({"field": "x", "op": "between", "value": [1, 3]}, {"x": 2}),
             "between 闭区间内")
    c.expect(not expr_mod.evaluate({"field": "x", "op": "between", "value": [1, 3]}, {"x": 4}),
             "between 区间外")
    c.expect(expr_mod.evaluate({"field": "x", "op": "in", "value": ["a", "b"]}, {"x": "b"}),
             "in 命中")
    c.expect(expr_mod.evaluate({"field": "x", "op": "is_null"}, {"x": None}), "is_null 命中")
    c.expect(not expr_mod.evaluate({"field": "x", "op": "is_null"}, {"x": 0}), "is_null 对 0 不成立")

    # 缺失字段：默认不满足 + 原因可追溯；strict 模式必须抛错
    ok_missing, why = expr_mod.evaluate_traced(ok, {})
    c.expect(ok_missing is False, "字段缺失 → 判不满足（不拿 0 蒙混）")
    c.expect(any(w.startswith("missing:zone_pos_120") for w in why),
             f"原因里写明缺失字段：{why}")
    c.raises(expr_mod.ExprError,
             lambda: expr_mod.evaluate(ok, {}, missing="raise"),
             "missing='raise' 时缺字段直接抛错")

    # 类型不匹配：判 False 且给出理由，不抛异常打断流水线
    ok_ty, why_ty = expr_mod.evaluate_traced(ok, {"zone_pos_120": "低"})
    c.expect(ok_ty is False and any(w.startswith("type_mismatch") for w in why_ty),
             f"字符串与数字比大小 → 类型不匹配并记录：{why_ty}")

    # 非法表达式必须被拒
    c.raises(expr_mod.ExprError,
             lambda: expr_mod.evaluate({"field": "x", "op": "~=", "value": 1}, {"x": 1}),
             "未知算子被拒")
    c.raises(expr_mod.ExprError,
             lambda: expr_mod.evaluate({"field": "x", "op": "<"}, {"x": 1}),
             "缺 value 被拒")
    c.raises(expr_mod.ExprError,
             lambda: expr_mod.evaluate({"all": []}, {"x": 1}),
             "空 all 被拒")
    c.raises(expr_mod.ExprError,
             lambda: expr_mod.evaluate({"field": "x", "op": "between", "value": 1}, {"x": 1}),
             "between 的 value 不是二元数组被拒")
    c.raises(expr_mod.ExprError,
             lambda: expr_mod.evaluate({"field": "x", "op": "is_null", "value": 1}, {"x": 1}),
             "is_null 带 value 被拒")

    # 上限：防僵尸规则
    deep = {"field": "x", "op": ">", "value": 0}
    for _ in range(expr_mod.MAX_DEPTH + 3):
        deep = {"not": deep}
    c.expect(not expr_mod.is_valid(deep), f"嵌套超过 {expr_mod.MAX_DEPTH} 被拒")
    wide = {"all": [{"field": f"f{i}", "op": ">", "value": 0}
                    for i in range(expr_mod.MAX_LEAVES + 5)]}
    c.expect(not expr_mod.is_valid(wide), f"叶子超过 {expr_mod.MAX_LEAVES} 被拒")

    # 禁 eval：真正的证明是「执行了就留下痕迹」，所以埋一个标记文件
    marker = Path(_TMP) / "pwned.marker"
    evil_field = ("__import__('pathlib').Path(r'" + str(marker) + "')"
                  ".write_text('pwned')")
    evil = {"field": evil_field, "op": ">", "value": 0}
    c.expect(not expr_mod.evaluate(evil, {}), "恶意字段名在空行上不命中")
    c.expect(not marker.exists(),
             "标记文件未被创建 —— 字段名只被当字符串查表，没有执行任何代码（无 eval）")
    c.expect(expr_mod.fields_used(ok) == ["zone_pos_120"], "fields_used 正确收集字段")
    return c


# ─────────────────────────────────────────────────────────────
# S2 · 规则注册表
# ─────────────────────────────────────────────────────────────
def check_rules() -> Check:
    c = Check("S2", "规则注册表：schema / append-only 版本 / 迁移白名单 / 角色权限 / 拒绝归档")
    rules.reset_for_tests()

    base = dict(
        rule_id="TEST-POS", scope="candidate_filter",
        desc_human="区间位 < 0.3", expr={"all": [{"field": "zone_pos_120", "op": "<", "value": 0.3}]},
        author="agent:propose", evidence={"n": 100, "mean_pct": 0.5},
    )

    r1 = rules.append(**base)
    c.expect(r1["version"] == 1 and r1["status"] == "hypothesis", "新规则以 v1 hypothesis 入库")
    c.expect(rules.next_version("TEST-POS") == 2, "next_version 自动 +1")

    r2 = rules.append(**{**base, "desc_human": "区间位 < 0.25"})
    c.expect(r2["version"] == 2, "同 rule_id 再次 append → v2")
    c.expect(r2["supersedes"] == "TEST-POS@1", "supersedes 指向上一版本")
    c.expect(len(rules.history("TEST-POS")) == 2, "历史两条都保留（append-only）")
    c.expect(rules.get("TEST-POS")["version"] == 2, "current() 取最后一条")

    # 新规则不许直接以 active 入库
    c.raises(rules.RuleError,
             lambda: rules.append(**{**base, "rule_id": "TEST-DIRECT", "status": "active"}),
             "直接以 active 入库被拒（提案与激活必须分离）")

    # schema 校验
    c.raises(rules.RuleError,
             lambda: rules.append(**{**base, "rule_id": "X", "scope": "candidate_filter"}),
             "rule_id 过短被拒")
    c.raises(rules.RuleError,
             lambda: rules.append(**{**base, "rule_id": "TEST-SCOPE", "scope": "nonsense"}),
             "非法 scope 被拒")
    c.raises(rules.RuleError,
             lambda: rules.append(**{**base, "rule_id": "TEST-NOEXPR", "expr": {"field": "x", "op": "?", "value": 1}}),
             "非法 expr 被拒")
    no_expr = {k: v for k, v in base.items() if k != "expr"}
    c.raises(rules.RuleError,
             lambda: rules.append(**{**no_expr, "rule_id": "TEST-EMPTY", "expr": {"all": []}}),
             "空 expr 被拒（只有 desc_human 的规则不许入库）")

    # 状态迁移 + 角色权限
    c.raises(rules.RuleError,
             lambda: rules.transition("TEST-POS", "active", actor="gate"),
             "hypothesis → active 越级被拒（必须先 testing）")
    c.raises(rules.RuleError,
             lambda: rules.transition("TEST-POS", "testing", actor="agent:propose"),
             "agent 不能把规则推进到 testing（只有 gate 能）")
    t = rules.transition("TEST-POS", "testing", actor="gate", reason="闸门通过")
    c.expect(t["status"] == "testing" and t["version"] == 3, "gate 可 → testing，版本 +1")

    c.raises(rules.RuleError,
             lambda: rules.transition("TEST-POS", "active", actor="agent:propose"),
             "agent 不能激活规则（必须 human/gate）")
    a = rules.transition("TEST-POS", "active", actor="human:kuromi", reason="人工批准")
    c.expect(a["status"] == "active", "human 可 → active")

    c.raises(rules.RuleError,
             lambda: rules.transition("TEST-POS", "decaying", actor="human:kuromi"),
             "→ decaying 只能由 decay_monitor/gate（机械降级，不靠人发现）")
    d = rules.transition("TEST-POS", "decaying", actor="decay_monitor", reason="连续3次低于阈值")
    c.expect(d["status"] == "decaying", "decay_monitor 可 → decaying")

    ret = rules.transition("TEST-POS", "retired", actor="human:kuromi", reason="失效")
    c.expect(ret["status"] == "retired", "retired 可达")
    c.raises(rules.RuleError,
             lambda: rules.transition("TEST-POS", "active", actor="human:kuromi"),
             "retired 是终态，不可复活")

    # 拒绝归档
    rules.record_rejection(rule_id="TEST-REJ", expr={"all": [{"field": "x", "op": ">", "value": 9}]},
                          reason="n=5 样本不足 + 剔除最大窗口后转负")
    c.expect("TEST-REJ" in rules.rejected_keys(), "被拒假设进入 rejected_keys()")
    c.expect(len(rules.rejected_records()) == 1, "拒绝记录可回读（防止重复提议）")

    # 证据提示（只提示不裁决）
    flags = rules.evidence_flags({"n": 5093, "mean_pct": 1.3, "median_pct": -0.4,
                                  "same_sign_years": "4/4", "after_cost_pct": 0.05,
                                  "drop_top_window_mean_pct": 0.08})
    c.expect(any("mean_positive_median_negative" in f for f in flags),
             f"均值正/中位负被标出：{flags}")
    clean = rules.evidence_flags({"n": 5093, "mean_pct": 0.58, "median_pct": 0.12,
                                 "same_sign_years": "4/4", "after_cost_pct": 0.05})
    c.expect(clean == [], f"干净证据不产生噪音：{clean}")

    # 种子导入幂等
    created = rules.bootstrap_rules()
    c.expect(len(created) >= 3, f"种子规则导入 {len(created)} 条")
    c.expect(rules.bootstrap_rules() == [], "再次导入为幂等（不重复写）")
    ids = {r["rule_id"] for r in rules.current().values()}
    c.expect({"POS-120-LOW", "CHG5D-NEG", "SEAL-RATIO-BUYABLE"} <= ids, "种子规则 id 齐全")
    return c


# ─────────────────────────────────────────────────────────────
# S3 · 锚点链
# ─────────────────────────────────────────────────────────────
def check_anchor() -> Check:
    c = Check("S3", "锚点链：冻结/校验、改一个空格被抓、改 anchors 断链、幂等")
    paths.ensure_dirs()

    # 本检查会故意把 anchors.json 改坏。必须保存/还原，否则同一进程里后续的
    # P2 结算检查会因为「锚点链已损坏」而集体失败 —— 那是测试没隔离干净，
    # 不是被测代码有问题。（第一次跑就踩到了这个：S3 之后 T8/T9 全红。）
    _orig_anchors = paths.ANCHORS.read_text(encoding="utf-8") if paths.ANCHORS.exists() else None
    for p in (paths.ANCHORS,):
        if p.exists():
            p.unlink()

    def write_plan(pid: str, payload: dict) -> None:
        paths.ensure_dirs()
        paths.plan_path(pid).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                        encoding="utf-8")

    write_plan("20261008", {"trade_date": "2026-10-08", "params_version": "v1.0",
                            "params_hash": "abc123", "params_frozen_at": "2026-10-01 16:40",
                            "picks": [{"code": "603906"}]})
    rec = anchor.freeze("20261008")
    c.expect(rec["plan_sha256"], "冻结返回 plan_sha256")
    ok, msg = anchor.verify("20261008")
    c.expect(ok, f"未改动时校验通过：{msg[:40]}…")

    # 幂等：内容未变时重复冻结不报错、不追加
    before = len(anchor.load()["order"])
    anchor.freeze("20261008")
    c.expect(len(anchor.load()["order"]) == before, "重复冻结幂等（链长度不变）")

    # ★ 只加一个空格也要被抓到
    pf = paths.plan_path("20261008")
    original = pf.read_text(encoding="utf-8")
    pf.write_text(original.replace('"603906"', '"603906" '), encoding="utf-8")
    ok2, msg2 = anchor.verify("20261008")
    c.expect(not ok2, f"plan 里只加一个空格 → 校验失败：{msg2[:44]}…")
    c.raises(RuntimeError, lambda: anchor.freeze("20261008"),
             "内容变了再冻结 → 拒绝（规则变更必须新 plan_id）")
    pf.write_text(original, encoding="utf-8")
    ok3, _ = anchor.verify("20261008")
    c.expect(ok3, "还原后校验恢复通过")

    # 第二条锚点 → 链式
    write_plan("20261009", {"trade_date": "2026-10-09", "params_version": "v1.0",
                            "params_hash": "abc123", "picks": []})
    anchor.freeze("20261009")
    d = anchor.load()
    c.expect(len(d["order"]) == 2, "链上两条记录")
    c.expect(d["records"]["20261009"]["prev"] == d["records"]["20261008"]["chain_sha256"],
             "第二条的 prev 指向第一条的 chain_sha256（真的成链）")

    # 篡改 anchors.json 本身 → 断链
    d2 = anchor.load()
    d2["records"]["20261008"]["plan_sha256"] = "0" * 64
    paths.ANCHORS.write_text(json.dumps(d2, ensure_ascii=False, indent=1), encoding="utf-8")
    ok4, msg4 = anchor.verify("20261008")
    c.expect(not ok4, f"直接改 anchors.json → 断链被抓：{msg4[:50]}…")
    c.expect(len(anchor.verify_chain()) > 0, "verify_chain 报出问题")

    # 未锚定的 plan 在 strict 模式下必须失败
    write_plan("20261010", {"trade_date": "2026-10-10", "picks": []})
    ok5, _ = anchor.verify("20261010")
    c.expect(not ok5, "未锚定的 plan 默认不放行（strict=True）")

    # ── 还原 anchors.json，保证后续检查在干净状态上跑
    if _orig_anchors is None:
        if paths.ANCHORS.exists():
            paths.ANCHORS.unlink()
    else:
        paths.ANCHORS.write_text(_orig_anchors, encoding="utf-8")
    c.expect(anchor.verify_chain(anchor.load()) == [], "还原后锚点链完整（检查已隔离）")
    return c


# ─────────────────────────────────────────────────────────────
# S4 · 特征层
# ─────────────────────────────────────────────────────────────
def _kline(dates: list[str], closes: list[float], highs: list[float] | None = None,
           lows: list[float] | None = None) -> pd.DataFrame:
    n = len(dates)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    return pd.DataFrame({
        "date": dates, "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": [1_000_000] * n,
    })


def check_tagging() -> Check:
    c = Check("S4", "特征层：防时序泄漏 / 涨停价银行家舍入 / 区间位 / post 不猜")

    # ── 涨停价：银行家舍入必须被修正
    #    实测（Python 3.14）：1.15*1.1 与 4.85*1.1 落在 .xx5 边界，
    #    Python round 会给出比交易所低一分的答案。
    c.expect(round(1.15 * 1.1, 2) == 1.26,
             "先确认 Python 原生 round 确实会算错（1.15 → 1.26）")
    c.expect(tagging.limit_up_price(1.15, "600000") == 1.27,
             f"Decimal 半进位修正为 1.27（实际 {tagging.limit_up_price(1.15, '600000')}）")
    c.expect(round(4.85 * 1.1, 2) == 5.33, "第二个边界样本：原生 round 给 5.33")
    c.expect(tagging.limit_up_price(4.85, "600000") == 5.34,
             f"Decimal 修正为 5.34（实际 {tagging.limit_up_price(4.85, '600000')}）")
    c.expect(tagging.limit_up_price(6.75, "600000") == 7.43, "非边界值 6.75 → 7.43")
    c.expect(tagging.limit_up_price(30.34, "600000") == 33.37, "30.34 → 33.37")
    c.expect(tagging.limit_up_price(10.0, "300001") == 12.0, "创业板 ×1.2")
    c.expect(tagging.limit_up_price(10.0, "830001") == 13.0, "北交所 ×1.3")
    c.expect(tagging.limit_up_price(None, "600000") is None, "无昨收 → None（不猜）")

    # ── 区间位
    #    注意：单调上涨不等于区间位 1.0 —— 收盘价在区间高点下方，
    #    要 close == 区间最高价才是 1.0。手算：(18-9)/(19-9) = 0.9。
    seg = _kline(["2026-01-0%d" % i for i in range(1, 6)],
                 [10, 12, 14, 16, 18], highs=[11, 13, 15, 17, 19], lows=[9, 11, 13, 15, 17])
    c.expect(tagging.zone_pos(seg) == 0.9,
             f"手算 (18-9)/(19-9)=0.9（实际 {tagging.zone_pos(seg)}）")
    seg_top = _kline(["2026-01-0%d" % i for i in range(1, 6)],
                     [10, 12, 14, 16, 19], highs=[11, 13, 15, 17, 19], lows=[9, 11, 13, 15, 17])
    c.expect(tagging.zone_pos(seg_top) == 1.0, "收盘等于区间最高价 → 1.0")
    seg_low = _kline(["2026-01-0%d" % i for i in range(1, 6)],
                     [18, 16, 14, 12, 9], highs=[19, 17, 15, 13, 11], lows=[17, 15, 13, 11, 9])
    c.expect(tagging.zone_pos(seg_low) == 0.0,
             f"收盘等于区间最低价 → 0.0（实际 {tagging.zone_pos(seg_low)}）")

    # ── ★ 防时序泄漏：把 date8 之后的未来价格改到天上，pre 特征必须一字不变
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-01", periods=140)]
    base_closes = list(np.linspace(10, 20, 140))
    df_now = _kline(dates, base_closes)

    # 用日期（而不是下标）界定「未来」，并自证构造里确实有未来行 ——
    # 否则一旦区间算错，这个测试就变成了空转（永远通过）。
    is_future = pd.to_datetime(df_now["date"]) > pd.Timestamp("2026-06-30")
    c.expect(int(is_future.sum()) > 0,
             f"构造数据里确有 date8 之后的行（{int(is_future.sum())} 行），测试不是空转")
    future_bumped = list(base_closes)
    for i in df_now.index[is_future]:
        future_bumped[i] = future_bumped[i] * 5
    df_future = _kline(dates, future_bumped)

    stock = {"code": "600000", "price": 20.0, "boards": 1, "seal_amount": 1e8,
             "float_cap": 1e10, "seal_ratio_pct": 1.0}
    pre_a = tagging.stock_pre(stock, klines=df_now, date8="20260630")
    pre_b = tagging.stock_pre(stock, klines=df_future, date8="20260630")
    same = all(abs((pre_a.get(k) or 0) - (pre_b.get(k) or 0)) < 1e-9
               for k in ("zone_pos_120", "zone_pos_60", "chg_5d_pct", "chg_20d_pct"))
    c.expect(same,
             "把 date8 之后的未来价格放大 5 倍，pre 的位置类特征完全不变（无未来数据）")
    c.expect(pre_a["kline_rows"] > 0, f"自证字段 kline_rows={pre_a['kline_rows']}")
    c.expect(pre_a["zone_pos_120"] is not None, "区间位有值")

    # asof 只能看到 <= date8
    seg_le = tagging.asof(df_now, "20260630", 1000)
    c.expect(str(seg_le["date"].max().date()) <= "2026-06-30", "asof 不返回 date8 之后的行")

    # ── post：没有下一交易日时必须返回 None，不许拿最后一行充数
    c.expect(tagging.build_post("600000", klines=df_now, date8="20260630")["next_date"]
             > "2026-06-30", "post 取到的是下一个交易日")
    last_day = dates[-1]
    c.expect(tagging.build_post("600000", klines=df_now, date8=last_day) is None,
             "date8 是最后一根 K 线时 → post 返回 None（不猜）")

    # ── 整天构造 + 落盘 + 回填
    snap = pools.emotion_snapshot(
        [{"boards": 1, "seal_ratio_pct": 0.5}, {"boards": 3, "seal_ratio_pct": 2.0}],
        [{"code": "000001"}], [{"code": "000002"}],
    )
    doc = tagging.build_day("20260630", snapshot=snap, stocks=[stock],
                            klines_by_code={"600000": df_now})
    p = tagging.write_features(doc)
    c.expect(p.exists(), f"特征落盘：{p.name}")
    back = tagging.read_features("20260630")
    c.expect(back is not None and back["stage"] == "pre", "回读 stage=pre")
    filled = tagging.backfill_post("20260630", {"600000": df_now})
    c.expect(filled["stage"] == "post" and filled["post_filled_n"] == 1, "回填 post 成功")
    c.expect(tagging.read_features("20260630")["stage"] == "post", "回填后落盘为 post")

    row = tagging.flatten_row(back, back["stocks"][0])
    c.expect("m_zt_n" in row and "zone_pos_120" in row, "flatten_row 带 m_ 前缀且含个股字段")
    c.expect("zone_pos_120" in tagging.feature_fields(), "feature_fields 覆盖规则常用字段")
    return c


# ─────────────────────────────────────────────────────────────
# S5 · 池子与情绪
# ─────────────────────────────────────────────────────────────
def check_pools() -> Check:
    c = Check("S5", "池子与情绪：列名别名 / 炸板率口径 / 可买性分档")

    zt_raw = {"代码": "603906", "名称": "龙蟠科技", "最新价": "19.04", "涨跌幅": "10.02",
              "成交额": "1.2e9", "流通市值": "5.0e9", "总市值": "8.0e9", "换手率": "3.2",
              "连板数": "1", "首次封板时间": "093512", "最后封板时间": "145959",
              "封板资金": "1.95e7", "炸板次数": "0", "所属行业": "*", "_": "x"}
    row = pools.normalize_row(zt_raw)
    c.expect(row is not None and row["code"] == "603906", "涨停池行规范化")
    c.expect(abs(row["seal_ratio_pct"] - 0.39) < 0.01,
             f"封单/流通 = {row['seal_ratio_pct']}%（19.5e6 / 5.0e9）")
    c.expect(row["first_seal"] == "093512", f"封板时间归一为 6 位：{row['first_seal']}")

    # 跌停池的列名不一样：封单资金 / 连续跌停
    dt_raw = {"代码": "000002", "名称": "某跌停", "最新价": "8.0", "涨跌幅": "-10.0",
              "流通市值": "2.0e9", "封单资金": "5.0e6", "连续跌停": "2",
              "最后封板时间": "93005", "开板次数": "1", "所属行业": "地产"}
    drow = pools.normalize_row(dt_raw)
    c.expect(drow is not None and drow["boards"] == 2, "跌停池「连续跌停」映射到 boards")
    c.expect(abs(drow["seal_ratio_pct"] - 0.25) < 0.01, f"跌停池封单资金也映射：{drow['seal_ratio_pct']}")

    # 炸板率口径：炸板 ÷ (涨停 + 炸板)，不是 ÷(涨停+跌停)
    snap = pools.emotion_snapshot(
        [{"boards": 1}, {"boards": 1}, {"boards": 2}],       # 3 涨停
        [{"code": "1"}],                                      # 1 炸板
        [{"code": "2"}, {"code": "3"}],                       # 2 跌停
    )
    c.expect(snap["zt_n"] == 3 and snap["zb_n"] == 1 and snap["dt_n"] == 2, "计数正确")
    c.expect(abs(snap["zb_rate"] - 0.25) < 1e-9,
             f"炸板率 = 1/(3+1) = 0.25（实际 {snap['zb_rate']}）；若误用 ÷(涨停+跌停) 会得 0.6")
    c.expect(snap["max_lbc"] == 2 and snap["lbc_ge2_n"] == 1, "连板高度与 2 板数正确")
    c.expect(snap["usage"] == "brake_only", "快照带 usage=brake_only 约束")
    c.expect("待校准" in snap["emotion_formula"], "合成分明确标注待校准")

    # 可买性分档
    c.expect(pools.buyability_bucket(0.5) == "buyable", "<1% → 可买")
    c.expect(pools.buyability_bucket(2.0) == "auction_decide", "1-3% → 竞价判断")
    c.expect(pools.buyability_bucket(5.0) == "likely_sealed", "3-10% → 大概率一字")
    c.expect(pools.buyability_bucket(18.0) == "unbuyable", ">10% → 买不到")
    c.expect(pools.buyability_bucket(None) == "unknown", "缺数据 → unknown（不猜）")
    return c


# ─────────────────────────────────────────────────────────────
# S6 · 题材归因解析
# ─────────────────────────────────────────────────────────────
def check_ths() -> Check:
    c = Check("S6", "题材归因解析：多种外层结构 / 字段别名 / 空结果必须报错")

    shapes = [
        {"data": {"list": [{"股票代码": "603906", "股票简称": "龙蟠科技", "涨停原因": "固态电池+新能源"}]}},
        {"data": [{"代码": "603906", "名称": "龙蟠科技", "题材": "固态电池"}]},
        {"list": [{"code": "603906", "name": "龙蟠科技", "reason": "固态电池"}]},
        [{"code": "603906", "name": "龙蟠科技", "reason": "固态电池"}],
    ]
    for i, sh in enumerate(shapes, 1):
        parsed = ths_reason.parse_payload(sh)
        c.expect("603906" in parsed, f"外层结构 {i} 解析成功")

    parsed = ths_reason.parse_payload(shapes[0])
    c.expect(parsed["603906"]["tags"] == ["固态电池", "新能源"],
             f"题材串按分隔符拆标签：{parsed['603906']['tags']}")
    c.expect(ths_reason.reason_for(parsed, "603906") == "固态电池+新能源", "reason_for 取值")
    c.expect(ths_reason.reason_for(parsed, "999999") is None, "查不到 → None（不猜）")

    c.raises(ths_reason.ThsReasonError, lambda: ths_reason.parse_payload({"data": {"list": []}}),
             "解析出 0 条必须报错（不能静默当『今天没归因』）")
    c.raises(ths_reason.ThsReasonError, lambda: ths_reason.parse_payload({"weird": 1}),
             "结构无法识别必须报错")
    c.raises(ths_reason.ThsReasonError, lambda: ths_reason.fetch_reason("2026-13-99"),
             "非法日期被拒")
    return c


# ─────────────────────────────────────────────────────────────
# S7 · 分时
# ─────────────────────────────────────────────────────────────
def check_minute() -> Check:
    c = Check("S7", "分时：指数前缀映射 / 无 0925 / 1500 优先 / 日期护栏")

    c.expect(minute_data.tencent_symbol("600032") == "sh600032", "沪市个股")
    c.expect(minute_data.tencent_symbol("000002") == "sz000002", "深市个股（000002 是深市）")
    c.expect(minute_data.tencent_symbol("300750") == "sz300750", "创业板")
    c.expect(minute_data.tencent_symbol("830001") == "bj830001", "北交所")
    c.expect(minute_data.tencent_symbol("000300", is_index=True) == "sh000300",
             "沪深300 是上海指数（纯按 0 开头会错到 sz）")
    c.expect(minute_data.tencent_symbol("000688", is_index=True) == "sh000688", "科创50 是上海")
    c.expect(minute_data.tencent_symbol("399006", is_index=True) == "sz399006", "创业板指 深市")
    c.raises(minute_data.MinuteDataError,
             lambda: minute_data.tencent_symbol("999999", is_index=True),
             "未知指数代码不许猜前缀")

    payload = {"data": {"sh600032": {"data": {
        "data": ["0930 6.81 1866 1270746.0", "0931 6.90 800 552000.0",
                 "1450 7.20 100 72000.0", "1500 7.15 200 143000.0",
                 "1530 7.15 50 35750.0"],
        "date": "20260928",
    }}}}
    parsed = minute_data.parse_payload(payload, "600032")
    rows = parsed["rows"]
    c.expect(parsed["date"] == "20260928", "解析出日期")
    c.expect(len(rows) == 5, f"解析出 {len(rows)} 行")
    c.expect(minute_data.at_time(rows, "0925") is None,
             "at_time('0925') 返回 None —— 分时里本来就没有竞价那一条，不许伪造")
    c.expect(minute_data.close_price(rows) == 7.15, "收盘价取 1500")
    c.expect(minute_data.at_time(rows, "1530") == 7.15, "1530 行存在但不参与收盘价")
    c.expect(minute_data.window_low(rows, "0930", "1450") == 6.81, "窗口最低价")
    c.expect(minute_data.window_high(rows, "0930", "1500") == 7.20, "窗口最高价")

    c.raises(minute_data.StaleMinuteData,
             lambda: _guard(parsed, "600032", "20260929"),
             "日期不符 → StaleMinuteData（结算必须中止，不许写账本）")
    c.expect(isinstance(minute_data.StaleMinuteData("20260929", "20260928", "603906"),
                        minute_data.MinuteDataError),
             "StaleMinuteData 是 MinuteDataError 的子类（上层可统一捕获）")
    c.raises(minute_data.MinuteDataError,
             lambda: minute_data.parse_payload({"data": {}}, "600032"),
             "响应里没有该 symbol → 报错")
    c.raises(minute_data.MinuteDataError,
             lambda: minute_data.parse_payload({"data": {"sh600032": {"data": {"data": []}}}}, "600032"),
             "分时为空 → 报错（不返回空壳）")
    return c


def _guard(parsed: dict, code: str, expect_date: str) -> None:
    """复刻 fetch_minute 的日期护栏逻辑，便于离线验证。"""
    if parsed["date"] != expect_date:
        raise minute_data.StaleMinuteData(expect_date, parsed["date"], code)


# ─────────────────────────────────────────────────────────────
# 运行器
# ─────────────────────────────────────────────────────────────
CHECKS = (
    ("S1", check_expr),
    ("S2", check_rules),
    ("S3", check_anchor),
    ("S4", check_tagging),
    ("S5", check_pools),
    ("S6", check_ths),
    ("S7", check_minute),
)


def run() -> dict:
    results = []
    for _, fn in _all_checks():
        try:
            results.append(fn())
        except Exception as exc:  # 检查本身崩了也算失败，但要说清在哪
            c = Check("ERR", f"{fn.__name__} 执行异常")
            import traceback
            c.expect(False, f"{type(exc).__name__}: {exc}")
            c.note(traceback.format_exc().splitlines()[-1])
            results.append(c)
    passed = all(c.passed for c in results)
    return {
        "passed": passed,
        "n_checks": len(results),
        "n_failed": sum(1 for c in results if not c.passed),
        "checks": [c.to_dict() for c in results],
    }


def _all_checks():
    """P1 的 S1–S7 + P2 的 T1–T10 + P3 的 U1–U8。

    子套件导入失败必须显式暴露，不许静默少跑一批 —— 少跑的检查会让人误以为
    「全绿」。
    """
    from portal.virtual import selfcheck_p2, selfcheck_p3

    return (tuple(CHECKS) + tuple(selfcheck_p2.CHECKS) + tuple(selfcheck_p3.CHECKS))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    report = run()
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1

    print("=" * 72)
    print("portal.virtual 自检（P1 记忆层 + P2 结算闭环）")
    print(f"数据根（隔离）：{paths.VIRTUAL_ROOT}")
    print("=" * 72)
    for chk in report["checks"]:
        flag = "PASS" if chk["passed"] else "FAIL"
        print(f"\n[{flag}] {chk['id']} · {chk['title']}")
        for line in chk["detail"]:
            print(line)
    print("\n" + "=" * 72)
    print(f"结果：{report['n_checks'] - report['n_failed']}/{report['n_checks']} 通过"
          + ("" if report["passed"] else "  ← 有检查失败"))
    print("=" * 72)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
