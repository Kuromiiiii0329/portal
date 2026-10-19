# -*- coding: utf-8 -*-
"""
portal/virtual/gate.py — 规则活化闸门（G1–G5）

这是整套系统的**裁判**。Agent 只能提议，只有这里能放行。

五条闸门，每一条都对应一次真实翻车（全部来自原包自己踩过的坑）：

| 闸门 | 判据 | 来自哪次翻车 |
|---|---|---|
| **G1 样本量** | n ≥ 30；n < 100 标注「样本偏少」 | 避坑手册 §4.4：18 个样本做不了统计 |
| **G2 扣费后超额** | 扣费后 > 0，且相对基准 > 0 | SKILL §9.3：基准本身 −0.113%/笔 |
| **G3 方向同向** | 分年（≥3 年）同号 | §9.4.2：只在一年为正 = 行情特征，不是规律 |
| **G4 剔除最大窗口** | 剔除权重最大窗口后仍为正 | §9.7.2：复合档 41% 样本来自 2024-02 单月，剔除后转负 |
| **G5 多重比较** | BH-FDR 校正后仍显著 | `robustness.py: bh_fdr`：试 50 个挑最好 = 抽卡 |

设计原则
--------
1. **不许用「没测过」当及格**。缺哪一项证据，G 就判不通过，并写明「未提供」。
   默认值不是中性选项，它是隐形的放行。（对应 SKILL §9.7.3）
2. **闸门只判不改**。通过与否是纯函数；真正写状态由 `promote()` 调 `rules.transition()`。
3. **失败也要留档**。`promote()` 拒绝时写 `rejected.jsonl` 并带上失败原因，
   否则同一个坏主意会被反复提（§9.4.1）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import rules

#: 单笔往返成本（与 backtest_core 一致）
COST_ROUND_TRIP_PCT = 0.10
#: 相对零自由度基准必须跨过的毛超额门槛（基准自身 −0.113%/笔 + 成本 0.102%）
BASELINE_NET_PCT = -0.113
#: 经济意义下限：SKILL §9.3 —— 基准本身 −0.113%/笔、成本 0.102%/笔，
#: 所以任何规则要「有意义」必须提供 > 0.215% 的超额。
#: 少了这一条，任何比平均略高的分桶都会被放进 active —— 实测踩到：
#: 一条与收益无关的维度（连板数分桶）以 +0.034pp 的超额通过了 G2。
MIN_EFFECT_PP = 0.215

MIN_SAMPLES = 30
SMALL_SAMPLE = 100
MIN_YEARS = 3
FDR_ALPHA = 0.05

#: 权重最大窗口剔除后的最低容差
EPS = 1e-9


@dataclass
class GateCheck:
    code: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "passed": self.passed, "detail": self.detail}


@dataclass
class GateResult:
    passed: bool
    checks: list[GateCheck] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def failed(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]

    def verdict(self) -> str:
        if self.passed:
            return "通过 G1–G5，可进入 testing"
        return "拒绝：" + "；".join(f"{c.code}({c.detail})" for c in self.failed())

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "verdict": self.verdict(),
            "checks": [c.to_dict() for c in self.checks],
            "flags": self.flags,
        }


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(float(v)):
        return None
    return float(v)


def _parse_years(spec: Any) -> tuple[int, int] | None:
    """把 "4/4" 解析成 (4,4)；无法解析返回 None。"""
    if not isinstance(spec, str) or "/" not in spec:
        return None
    a, _, b = spec.partition("/")
    try:
        return int(a), int(b)
    except ValueError:
        return None


def evaluate(evidence: dict | None, *, min_samples: int = MIN_SAMPLES,
             fdr_alpha: float = FDR_ALPHA) -> GateResult:
    """对一份证据跑 G1–G5。纯函数，不写任何状态。"""
    ev = evidence or {}
    checks: list[GateCheck] = []

    n = _num(ev.get("n"))
    if n is None:
        checks.append(GateCheck("G1", False, "未提供样本量 n —— 不许用「没测过」当及格"))
    elif n < min_samples:
        checks.append(GateCheck("G1", False, f"n={int(n)} < {min_samples}，不得下任何结论"))
    elif n < SMALL_SAMPLE:
        checks.append(GateCheck("G1", True, f"n={int(n)}，样本偏少（<{SMALL_SAMPLE}），只能当线索"))
    else:
        checks.append(GateCheck("G1", True, f"n={int(n)}"))

    after = _num(ev.get("after_cost_pct"))
    vs_base = _num(ev.get("vs_baseline_pp"))
    if after is None:
        checks.append(GateCheck("G2", False, "未提供扣费后收益 after_cost_pct"))
    elif after <= EPS:
        checks.append(GateCheck("G2", False, f"扣费后 {after:+.3f}% ≤ 0，跑不过成本"))
    else:
        # ★ 只有「> 0」是不够的：经济意义下限见 MIN_EFFECT_PP
        margin = vs_base if vs_base is not None else after
        if margin is not None and margin < MIN_EFFECT_PP - 1e-9:
            checks.append(GateCheck(
                "G2", False,
                f"扣费后 {after:+.3f}% 虽为正，但相对基准仅 {margin:+.3f}pp < "
                f"{MIN_EFFECT_PP}pp 门槛 —— 统计上像是信号，经济上不够付成本"))
        else:
            tail = (f"，相对基准 {vs_base:+.3f}pp"
                    if vs_base is not None else "（未提供基准对照，按无基准处理）")
            checks.append(GateCheck("G2", True, f"扣费后 {after:+.3f}%{tail}"))

    yrs = _parse_years(ev.get("same_sign_years"))
    if yrs is None:
        checks.append(GateCheck("G3", False, "未提供分年同向性 same_sign_years（如 \"4/4\"）"))
    else:
        same, total = yrs
        if total < MIN_YEARS:
            checks.append(GateCheck("G3", False, f"只有 {total} 个年度，不足 {MIN_YEARS}"))
        elif same != total:
            checks.append(GateCheck("G3", False,
                                    f"分年方向不一致 {same}/{total} —— 只在部分年份为正属行情特征"))
        else:
            checks.append(GateCheck("G3", True, f"分年 {same}/{total} 同向"))

    drop = _num(ev.get("drop_top_window_mean_pct"))
    if drop is None:
        checks.append(GateCheck("G4", False,
                                "未提供「剔除权重最大窗口」后的均值 —— 不许跳过这一步"))
    elif drop <= EPS:
        checks.append(GateCheck("G4", False,
                                f"剔除最大窗口后 {drop:+.3f}% ≤ 0，疑似单一窗口撑起"))
    else:
        checks.append(GateCheck("G4", True, f"剔除最大窗口后 {drop:+.3f}% 仍为正"))

    p = _num(ev.get("bh_fdr_p"))
    if p is None:
        checks.append(GateCheck("G5", False,
                                "未提供 BH-FDR 校正后 p 值 —— 不校正等于「试 50 个挑最好」"))
    elif p >= fdr_alpha:
        checks.append(GateCheck("G5", False, f"BH-FDR p={p:.4f} ≥ {fdr_alpha}"))
    else:
        checks.append(GateCheck("G5", True, f"BH-FDR p={p:.4f} < {fdr_alpha}"))

    result = GateResult(passed=all(c.passed for c in checks), checks=checks)
    result.flags = rules.evidence_flags(ev) + _extra_flags(ev)
    return result


def _extra_flags(ev: dict) -> list[str]:
    """闸门之外的附加提示（不影响通过与否，但必须出现在报告里）。"""
    flags: list[str] = []
    mean = _num(ev.get("mean_pct"))
    median = _num(ev.get("median_pct"))
    if mean is not None and median is None:
        flags.append("no_median_reported:只给了均值，无法判断是否「均值正、中位负」")
    after = _num(ev.get("after_cost_pct"))
    if after is not None and 0 < after < COST_ROUND_TRIP_PCT:
        # 注意措辞：after_cost 已经是**扣费后**的数，不能说它「不够付成本」（那会自相矛盾）。
        # 真正的风险是：净超额比成本假设的一半还小，成本稍高（滑点/费率上调）就归零。
        flags.append(
            f"thin_margin:净超额仅 {after:+.3f}%/笔，小于单次往返成本假设 "
            f"{COST_ROUND_TRIP_PCT}%；成本若高出 {after:.3f}pp 即转负")
    return flags


# ─────────────────────────────────────────────────────────────
# 与规则注册表联动
# ─────────────────────────────────────────────────────────────
def promote(rule_id: str, evidence: dict, *, actor: str = "gate",
            reason: str = "") -> dict:
    """跑闸门；通过就推进状态，不通过就归档拒绝理由。

    返回 {"passed": bool, "gate": GateResult, "rule": dict|None, "rejected": bool}
    """
    result = evaluate(evidence)
    cur = rules.get(rule_id)
    if cur is None:
        raise rules.RuleError(f"规则 {rule_id} 不存在")

    if not result.passed:
        rules.record_rejection(
            rule_id=rule_id,
            expr=cur.get("expr") or {},
            reason=result.verdict(),
            evidence=evidence,
            actor=actor,
        )
        return {"passed": False, "gate": result, "rule": cur, "rejected": True}

    if cur.get("status") != "hypothesis":
        return {"passed": True, "gate": result, "rule": cur, "rejected": False,
                "note": f"规则当前已是 {cur.get('status')}，无需重复推进"}

    new = rules.transition(
        rule_id, "testing", actor=actor,
        reason=reason or result.verdict(),
        evidence_patch=evidence,
    )
    return {"passed": True, "gate": result, "rule": new, "rejected": False}


__all__ = [
    "COST_ROUND_TRIP_PCT",
    "BASELINE_NET_PCT",
    "MIN_EFFECT_PP",
    "MIN_SAMPLES",
    "SMALL_SAMPLE",
    "MIN_YEARS",
    "FDR_ALPHA",
    "GateCheck",
    "GateResult",
    "evaluate",
    "promote",
]
