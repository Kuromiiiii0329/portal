# -*- coding: utf-8 -*-
"""
portal/virtual/selfcheck_p3.py — P3 闸门 / 衰减 / 审计 / 演化自检（全部离线）

覆盖 U1–U8：

    U1  闸门 G1–G5      —— 每条单独失败、全过、缺证据必须判不过
    U2  闸门联动         —— 过则 hypothesis→testing；不过则写 rejected；角色受限
    U3  衰减监控         —— 计数累加 / 达标清零 / 连续 3 次降级 / 复活必须写理由
    U4  报告审计 A1–A7   —— 每条都有正例与反例（探针必须能失败）
    U5  BH-FDR           —— 用手算可验证的样例
    U6  演化器           —— 植入已知边缘必须被抓到；纯噪声不得大量误报
    U7  单维度硬限制     —— 不许悄悄生成多维交叉
    U8  元数据修订权限   —— agent 不得改已入库规则的证据

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
    str(Path(tempfile.mkdtemp(prefix="portal_virtual_p3_")) / "virtual"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from portal.virtual import audit, decay, evolve, gate, rules  # noqa: E402
from portal.virtual._check import Check, close_enough  # noqa: E402


# ─────────────────────────────────────────────────────────────
# 合格证据（可逐项破坏）
# ─────────────────────────────────────────────────────────────
GOOD_EV = {
    "n": 5093, "mean_pct": 0.580, "median_pct": 0.152, "winrate": 53.1,
    "t_stat": 3.41, "by_year": {"2023": 0.41, "2024": 0.98, "2025": 0.32, "2026": 0.19},
    "same_sign_years": "4/4", "drop_top_window_mean_pct": 0.152,
    "after_cost_pct": 0.050, "vs_baseline_pp": 0.693, "bh_fdr_p": 0.012,
}


def _ev(**over) -> dict:
    d = dict(GOOD_EV)
    d.update(over)
    return d


# ─────────────────────────────────────────────────────────────
# U1 · 闸门
# ─────────────────────────────────────────────────────────────
def check_gate() -> Check:
    c = Check("U1", "闸门 G1–G5：每条都能单独拦住，缺证据不许当及格")

    r = gate.evaluate(GOOD_EV)
    c.expect(r.passed, f"合格证据通过 5 条：{[x.code for x in r.checks]}")
    c.expect(len(r.checks) == 5, f"恰好 5 条闸门（实际 {len(r.checks)}）")

    c.expect(not gate.evaluate(_ev(n=12)).passed, "G1：n=12 被拒")
    c.expect(any("样本偏少" in x.detail for x in gate.evaluate(_ev(n=50)).checks if x.code == "G1"),
             "G1：n=50 标「样本偏少」但放行")
    c.expect(not gate.evaluate(_ev(n=None)).passed, "G1：缺 n 被判不过（不许没测过就及格）")

    c.expect(not gate.evaluate(_ev(after_cost_pct=-0.02)).passed, "G2：扣费后为负被拒")
    c.expect(not gate.evaluate(_ev(after_cost_pct=None)).passed, "G2：缺扣费后收益被拒")
    c.expect(not gate.evaluate(_ev(after_cost_pct=0.3, vs_baseline_pp=-0.2)).passed,
             "G2：绝对为正但跑输基准 → 没有 alpha，被拒")
    # ★ 经济意义下限：+0.034pp 这种「比平均略高」不许当信号
    tiny = gate.evaluate(_ev(after_cost_pct=0.16, vs_baseline_pp=0.034))
    c.expect(not tiny.passed, "G2：超额仅 +0.034pp < 0.215pp 门槛 → 被拒（经济上不够付成本）")
    c.expect(any(c.code == "G2" and "门槛" in c.detail for c in tiny.checks),
             f"G2 明细说明是门槛问题：{[x.detail for x in tiny.checks if x.code == 'G2']}")
    c.expect(gate.evaluate(_ev(after_cost_pct=0.30, vs_baseline_pp=0.30)).passed,
             "G2：超额 +0.30pp > 门槛 → 通过")

    c.expect(not gate.evaluate(_ev(same_sign_years="3/4")).passed, "G3：3/4 不同向被拒")
    c.expect(not gate.evaluate(_ev(same_sign_years="2/2")).passed, "G3：只有 2 年被拒（不足 3 年）")
    c.expect(not gate.evaluate(_ev(same_sign_years=None)).passed, "G3：缺分年被拒")

    c.expect(not gate.evaluate(_ev(drop_top_window_mean_pct=-0.05)).passed,
             "G4：剔除最大窗口后转负被拒（均值陷阱）")
    c.expect(not gate.evaluate(_ev(drop_top_window_mean_pct=None)).passed,
             "G4：缺这一步被拒（不许跳过）")

    c.expect(not gate.evaluate(_ev(bh_fdr_p=0.20)).passed, "G5：未过 FDR 被拒")
    c.expect(not gate.evaluate(_ev(bh_fdr_p=None)).passed, "G5：缺 p 值被拒")

    # 附加提示（不影响通过，但必须出现）
    thin = gate.evaluate(_ev(after_cost_pct=0.05))
    c.expect(any("thin_margin" in f for f in thin.flags),
             f"扣费后仅 0.05% → 提示对滑点极敏感：{thin.flags}")

    # 反例：探针有效性 —— 把 verdict 拿去当期望必须对不上
    c.expect(not gate.evaluate(_ev(n=5)).passed, "破坏任意一条 → 整体不通过（探针有判别力）")
    return c


# ─────────────────────────────────────────────────────────────
# U2 · 闸门与规则注册表联动
# ─────────────────────────────────────────────────────────────
def check_promote() -> Check:
    c = Check("U2", "闸门联动：过则进 testing，不过则归档拒绝理由，角色受约束")
    rules.reset_for_tests()

    base = dict(scope="candidate_filter", desc_human="测试规则",
                expr={"all": [{"field": "zone_pos_120", "op": "<", "value": 0.3}]},
                author="agent:propose")

    rules.append(rule_id="U2-GOOD", evidence={"n": 1}, **base)
    r = gate.promote("U2-GOOD", GOOD_EV, actor="gate")
    c.expect(r["passed"] and r["rule"]["status"] == "testing",
             f"通过闸门 → testing（当前 {r['rule']['status']}）")
    c.expect(r["rule"]["evidence"]["n"] == 5093, "证据被并入规则记录")

    rules.append(rule_id="U2-BAD", evidence={"n": 1}, **base)
    r2 = gate.promote("U2-BAD", _ev(n=8), actor="gate")
    c.expect(not r2["passed"] and r2["rejected"], "不通过 → rejected=True")
    c.expect(r2["rule"]["status"] == "hypothesis", "被拒的规则状态不变（仍是 hypothesis）")
    c.expect("U2-BAD" in rules.rejected_keys(), "拒绝写入 rejected.jsonl（防止重复提议）")
    recs = [x for x in rules.rejected_records() if x["rule_id"] == "U2-BAD"]
    c.expect(recs and "G1" in recs[-1]["reason"], f"拒绝理由写明失败闸门：{recs[-1]['reason'][:50]}")

    # 通过闸门后，推进到 active 仍需 human/gate —— 闸门本身不授予 active
    c.raises(rules.RuleError,
             lambda: rules.transition("U2-GOOD", "active", actor="agent:propose"),
             "闸门通过后 agent 仍不能直接 active")

    c.raises(rules.RuleError, lambda: gate.promote("NOPE", GOOD_EV), "对不存在的规则提议 → 报错")
    return c


# ─────────────────────────────────────────────────────────────
# U3 · 衰减监控
# ─────────────────────────────────────────────────────────────
def check_decay() -> Check:
    c = Check("U3", "衰减监控：累加 / 清零 / 连续 3 次降级 / 复活须写理由")
    rules.reset_for_tests()
    base = dict(scope="candidate_filter", desc_human="衰减测试",
                expr={"all": [{"field": "x", "op": ">", "value": 0}]},
                author="human:t", evidence={})

    rules.append(rule_id="U3-R", **base)
    rules.transition("U3-R", "testing", actor="gate", reason="ok")
    rules.transition("U3-R", "active", actor="human:t", reason="批准")

    o1 = decay.observe("U3-R", -0.5)
    c.expect(o1.consecutive_fail == 1 and o1.action == "counted",
             f"第一次低于阈值 → 计数 1（{o1.detail}）")
    o2 = decay.observe("U3-R", 0.8)
    c.expect(o2.consecutive_fail == 0, "达标一次 → 计数清零（不累计历史）")
    decay.observe("U3-R", -0.1)
    decay.observe("U3-R", -0.2)
    o5 = decay.observe("U3-R", -0.3)
    c.expect(o5.action == "demoted", f"连续 3 次低于阈值 → 降级（{o5.detail}）")
    c.expect(rules.get("U3-R")["status"] == "decaying", "状态变为 decaying")
    c.expect("连续 3 次" in rules.get("U3-R")["status_reason"], "降级理由写清楚")

    o6 = decay.observe("U3-R", None)
    c.expect(o6.action == "skipped" and o6.consecutive_fail == 3,
             "没有表现数据时计数保持不变（不许静默清零掩盖失效）")

    c.raises(rules.RuleError, lambda: decay.revive("U3-R", reason=""), "复活不写理由 → 被拒")
    back = decay.revive("U3-R", actor="human:op", reason="重新回测通过")
    c.expect(back["status"] == "active", "human 可复活为 active")

    # 未活化的规则不参与衰减
    rules.append(rule_id="U3-H", **{**base, "desc_human": "未活化"})
    oh = decay.observe("U3-H", -5.0)
    c.expect(oh.action == "skipped", "hypothesis 状态不参与衰减（没活化就没有失效）")

    c.expect(isinstance(decay.health(), list), "health() 可读")
    return c


# ─────────────────────────────────────────────────────────────
# U4 · 报告审计
# ─────────────────────────────────────────────────────────────
def _payload(**over) -> dict:
    base = {
        "title": "测试报告",
        "text": "当前情绪读数为同步指标，据此降低仓位至 20%。",
        "claims": [{"action": "buy", "code": "600001", "name": "甲",
                    "buyability": True, "excluded_listed": True, "empty_exit": True}],
        "numbers": [{"value": 1.23, "source": "plan", "label": "涨幅"}],
        "params_text": {"z1": "适用"},
        "params_machine": {"z1": "适用"},
        "backtest": {"after_cost_pct": 0.05, "n": 500, "insufficient_labeled": False},
        "known_bias_declared": True,
    }
    base.update(over)
    return base


def check_audit() -> Check:
    c = Check("U4", "报告审计 A1–A7：每条都有正例与反例")

    ok = audit.audit(_payload())
    c.expect(ok["ok"] and not ok["blocked"], f"合规报告通过（findings={ok['findings']}）")

    # A1
    bad = _payload()
    bad["claims"][0]["buyability"] = False
    r = audit.audit(bad)
    c.expect(r["blocked"] and any("可买性" in f["message"] for f in r["findings"]),
             "A1：买入选股缺可买性校验 → 拦")
    bad2 = _payload()
    bad2["claims"].append({"action": "watch", "code": "600002"})
    c.expect(audit.audit(bad2)["ok"], "A1：观察类结论不要求三件套（不误伤）")

    # A2
    bad = _payload()
    bad["numbers"].append({"value": 88.8, "source": "", "label": "编的"})
    r = audit.audit(bad)
    c.expect(r["blocked"] and any("没有标注来源" in f["message"] for f in r["findings"]),
             "A2：来路不明的数字 → 拦")

    # A3
    bad = _payload(params_text={"z1": "适用"}, params_machine={"z1": False})
    r = audit.audit(bad)
    c.expect(r["blocked"] and any("文本写" in f["message"] for f in r["findings"]),
             "A3：文本说适用、引擎关闭 → 拦（§9.7.3 的事故）")

    # A4 双向对照
    bad = _payload(text="情绪冰点，明日必然反转。")
    r = audit.audit(bad)
    c.expect(r["blocked"] and any(f["code"] == "A4" for f in r["findings"]),
             "A4：把同步指标写成预测 → 拦")
    c.expect(audit.audit(_payload(text="冰点之后必然反弹"))["blocked"], "A4：「冰点之后必然反弹」被拦")
    c.expect(audit.audit(_payload(text="涨停家数预示明天走强"))["blocked"],
             "A4：「涨停家数预示」被拦")
    # 负例：合规写法不能误报
    for good_text in (
        "当前情绪读数为同步指标，据此降低仓位至 20%。",
        "涨停家数 69 家、炸板率 16.9%，仅用于仓位档位，不构成方向判断。",
        "连板高度压缩至 2-3 板，说明赔率下降，故降低使用强度。",
    ):
        c.expect(not audit.audit(_payload(text=good_text))["blocked"],
                 f"A4 负例应放过：{good_text[:22]}…")

    # A5
    bad = _payload(backtest={"after_cost_pct": -0.02, "n": 500,
                             "insufficient_labeled": False, "negative_labeled": False})
    r = audit.audit(bad)
    c.expect(r["blocked"] and any(f["code"] == "A5" for f in r["findings"]),
             "A5：扣费后为负却未标注 → 拦")
    good = _payload(backtest={"after_cost_pct": -0.02, "n": 500,
                              "insufficient_labeled": False, "negative_labeled": True})
    c.expect(audit.audit(good)["ok"], "A5：已标注为负期望 → 放行")

    # A6
    bad = _payload(backtest={"after_cost_pct": 0.5, "n": 8, "insufficient_labeled": False})
    r = audit.audit(bad)
    c.expect(r["blocked"] and any(f["code"] == "A6" for f in r["findings"]),
             "A6：n=8 未标样本极少 → 拦")

    # A7
    bad = _payload(known_bias_declared=False)
    r = audit.audit(bad)
    c.expect(r["blocked"] and any(f["code"] == "A7" for f in r["findings"]),
             "A7：引用回测却不声明偏差 → 拦")

    c.raises(ValueError, lambda: audit.assert_signable(_payload(text="冰点，明日将反转")),
             "assert_signable 对不合格报告抛错")
    audit.assert_signable(_payload())
    c.expect(True, "assert_signable 对合格报告不抛错")
    return c


# ─────────────────────────────────────────────────────────────
# U5 · BH-FDR
# ─────────────────────────────────────────────────────────────
def check_bh() -> Check:
    c = Check("U5", "BH-FDR：手算可验证")

    adj = evolve.benjamini_hochberg([0.01, 0.02, 0.03, 0.04])
    c.expect(all(close_enough(v, 0.04) for v in adj), f"p=[.01,.02,.03,.04] → 全部 0.04：{adj}")

    adj2 = evolve.benjamini_hochberg([0.01, 0.5])
    c.expect(close_enough(adj2[0], 0.02) and close_enough(adj2[1], 0.5),
             f"两个假设：[0.02, 0.5]：{adj2}")

    adj3 = evolve.benjamini_hochberg([0.001])
    c.expect(close_enough(adj3[0], 0.001), "单个假设不放大（m/rank=1）")

    c.expect(evolve.benjamini_hochberg([]) == [], "空输入返回空")
    # 校正后 p 必然 >= 原始 p（单调不减），这是 BH 的基本性质
    raw = [0.001, 0.004, 0.02, 0.03, 0.049]
    adj4 = evolve.benjamini_hochberg(raw)
    c.expect(all(a >= r - 1e-12 for a, r in zip(adj4, raw)),
             f"校正后不小于原始 p：{adj4}")
    return c


# ─────────────────────────────────────────────────────────────
# U6 · 演化器（植入已知边缘 / 纯噪声对照）
# ─────────────────────────────────────────────────────────────
def _make_rows(n: int, *, edge: bool, seed: int = 7) -> list[dict]:
    """构造特征行。edge=True 时 zone_pos_120<0.3 有稳定的正收益。

    日期必须**跨多年**：闸门 G3 要求「分年同向」且至少 3 年。
    第一版只造了 2026 一年的数据，结果植入的边缘也过不了闸门 ——
    那不是闸门太严，是测试数据不满足前提。
    """
    import random
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        zone = rng.random()
        lbc = rng.choice([1, 1, 1, 2, 3])
        chg5 = rng.gauss(0, 3)
        if edge and zone < 0.3:
            ret = 1.2 + rng.gauss(0, 1.0)          # 低位：稳定正期望
        else:
            ret = -0.4 + rng.gauss(0, 1.0)         # 其余：负期望
        year = 2023 + (i % 4)
        rows.append({
            "date": f"{year}{(i % 12) + 1:02d}{(i % 28) + 1:02d}",
            "zone_pos_120": zone, "zone_pos_60": zone, "lbc": lbc,
            "chg_5d_pct": chg5, "chg_10d_pct": chg5, "chg_20d_pct": chg5,
            "seal_ratio_pct": rng.random() * 5, "ma_spread_pct": rng.random() * 3,
            "turnover_pct": rng.random() * 10, "zbc": rng.choice([0, 0, 1]),
            "m_zb_rate": 0.15, "m_max_lbc": 4, "m_zt_n": 60,
            "ret_pct": ret,
        })
    return rows


def check_evolve() -> Check:
    c = Check("U6", "演化器：植入边缘必须被抓到；纯噪声不得大量误报")
    rules.reset_for_tests()

    rows = _make_rows(600, edge=True)
    res = evolve.run(rows, dims=("zone_pos_120", "chg_5d_pct", "lbc"), n_buckets=4)
    c.expect(res["baseline"]["n"] == 600, f"基准样本 {res['baseline']['n']}")
    c.expect(len(res["tested"]) > 0, f"被测条件 {len(res['tested'])} 条")

    ids = {e["rule_id"] for e in res["passed"]}
    c.expect(any("ZONE-POS-120" in i for i in ids),
             f"植入的低位边缘被识别为通过：{sorted(ids)}")
    c.expect(not any("CHG-5D" in i for i in ids),
             "无关维度（前5日涨幅随机）不应通过 —— 说明不是全靠运气")
    # ★ 连板数与收益无关，它的任何分桶都不该通过。
    #   第一次实现时它确实通过了（+0.034pp 的超额），暴露 G2 缺少经济意义下限；
    #   补上 MIN_EFFECT_PP 之后这条才成立 —— 这条断言是那个 bug 的回归测试。
    c.expect(not any("LBC" in i for i in ids),
             f"与收益无关的连板数维度不得通过（实际通过：{[i for i in ids if 'LBC' in i]}）")

    # 纯噪声：不应大量通过（BH 校正 + 闸门的共同作用）
    noise = _make_rows(600, edge=False, seed=99)
    res2 = evolve.run(noise, dims=("zone_pos_120", "chg_5d_pct"), n_buckets=4)
    c.expect(len(res2["passed"]) <= 1,
             f"纯噪声下通过的条件数 {len(res2['passed'])} ≤ 1（不靠抽卡）")

    # 落库：通过的写 hypothesis，拒绝的进 rejected.jsonl
    out = evolve.propose(res)
    c.expect(len(out["created"]) == len(res["passed"]), "通过的条件写成规则")
    c.expect(all(r["status"] == "hypothesis" for r in out["created"]),
             "演化器只能产出 hypothesis（活化必须再过人工/闸门）")
    c.expect(len(out["archived"]) == len(res["rejected"]), "被拒条件归档")
    c.expect(len(rules.rejected_keys()) > 0, "rejected.jsonl 非空，防止重复提议")

    # 再跑一轮：已提议/已拒绝的不再重复
    res3 = evolve.run(rows, dims=("zone_pos_120", "chg_5d_pct", "lbc"), n_buckets=4)
    c.expect(len(res3["tested"]) == 0,
             f"第二轮不再重复测试已处理的条件（实际 {len(res3['tested'])} 条）")

    # 样本太少时不许提议
    res4 = evolve.run(_make_rows(10, edge=True), dims=("zone_pos_120",))
    c.expect(res4["passed"] == [] and "样本" in (res4.get("note") or ""),
             f"样本不足时明确说明不提议：{res4.get('note')}")
    return c


# ─────────────────────────────────────────────────────────────
# U7 · 单维度硬限制
# ─────────────────────────────────────────────────────────────
def check_single_dimension() -> Check:
    c = Check("U7", "单维度硬限制：不许悄悄生成多维交叉（复合档已被推翻）")
    rows = _make_rows(400, edge=True)
    cands = evolve.build_conditions(rows, dims=("zone_pos_120", "chg_5d_pct", "lbc"))
    c.expect(len(cands) > 0, f"生成 {len(cands)} 条候选")

    from portal.virtual import expr as E
    multi = [x for x in cands if len(E.flatten(x.expr)) > evolve.MAX_LEAVES_PER_RULE]
    c.expect(multi == [], f"没有任何一条候选是多叶子（违例 {len(multi)}）")
    c.expect(evolve.MAX_LEAVES_PER_RULE == 1, "常量固定为 1")

    # 「小于 0」这种有先验含义的阈值必须出现在 chg_* 维度上
    ids = {x.rule_id for x in cands}
    c.expect(any(i.endswith("CHG-5D-PCT-NEG") for i in ids),
             f"chg_5d_pct 生成了「< 0」候选：{[i for i in ids if 'NEG' in i]}")
    return c


# ─────────────────────────────────────────────────────────────
# U8 · 元数据修订权限
# ─────────────────────────────────────────────────────────────
def check_revise_role() -> Check:
    c = Check("U8", "元数据修订权限：agent 不得改动已入库规则的证据")
    rules.reset_for_tests()
    rules.append(rule_id="U8-R", scope="entry", desc_human="权限测试",
                expr={"all": [{"field": "x", "op": ">", "value": 0}]},
                author="agent:propose", evidence={"n": 1})

    c.raises(rules.RuleError,
             lambda: rules.revise("U8-R", actor="agent:propose", evidence_patch={"n": 999}),
             "agent 改证据 → 被拒")
    r = rules.revise("U8-R", actor="decay_monitor", evidence_patch={"n": 42})
    c.expect(r["version"] == 2 and r["evidence"]["n"] == 42, "decay_monitor 可改 → v2")
    c.expect(r["status"] == "hypothesis", "revise 不改状态")
    c.expect(len(rules.history("U8-R")) == 2, "历史两条都保留（append-only）")
    return c


CHECKS = (
    ("U1", check_gate),
    ("U2", check_promote),
    ("U3", check_decay),
    ("U4", check_audit),
    ("U5", check_bh),
    ("U6", check_evolve),
    ("U7", check_single_dimension),
    ("U8", check_revise_role),
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
    report = run()
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    print("=" * 72)
    print("portal.virtual 自检 · P3 闸门 / 衰减 / 审计 / 演化")
    print("=" * 72)
    for chk in report["checks"]:
        print(f"\n[{'PASS' if chk['passed'] else 'FAIL'}] {chk['id']} · {chk['title']}")
        for line in chk["detail"]:
            print(line)
    print("\n" + "=" * 72)
    print(f"结果：{report['n_checks'] - report['n_failed']}/{report['n_checks']} 通过")
    print("=" * 72)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
