# -*- coding: utf-8 -*-
"""
portal/virtual/evolve.py — 演化循环（提议，不裁决）

它是「自我完善」的实现，但**只负责提议**。能不能活化由 `gate.py` 决定。

为什么必须这样分工（用原包自己的两次翻车做依据）：

1. 让 Agent 直接改规则 = 允许「改参数的人顺手重算哈希」——
   `anchor.py` 注释里点名的**自我认证等于没有认证**。
2. `SKILL.md` §9.7.4 实测过代价：一条没回测的排序规则，悄悄扣掉 **1.150pp**。
   免责声明不能代替回测。

因此本模块的产出永远是 `hypothesis`，并且：

- **只做单维度**。§9.7.2 已经证明「低位×回调」复合档是 2024-02 单月的产物，
  剔除后转负。多维度交叉在这里被硬性禁止（`MAX_LEAVES_PER_RULE = 1`）。
- **被拒的假设要记住**。`rejected_keys()` 里的 rule_id 不再重复提议，
  否则「试 50 个条件挑最好的」会无限循环。
- **必须做多重比较校正**。测了 N 个条件，就要用 BH-FDR 校正，
  否则「显著」只是抽卡抽出来的。

输入契约（很重要）
------------------
`rows` 是**已经算好收益的**摊平特征行：

    [{"zone_pos_120": 0.12, "lbc": 1, "date": "20261008",
      "ret_pct": 1.23, ...}, ...]

其中 `ret_pct` 必须是**扣费后的净收益**，且口径由 `backtest_core` 统一计算。
**本模块不自己算收益** —— 口径分叉本身就是静默错误的来源（P0 刚吃过这个亏）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import expr as expr_mod
from . import gate as gate_mod
from . import rules

#: 单条规则最多几个叶子 —— hard limit，防止悄悄生成多维交叉
MAX_LEAVES_PER_RULE = 1

DEFAULT_DIMS = (
    "zone_pos_120", "zone_pos_60", "chg_5d_pct", "chg_10d_pct", "chg_20d_pct",
    "lbc", "seal_ratio_pct", "ma_spread_pct", "turnover_pct", "zbc",
    "m_zb_rate", "m_max_lbc", "m_zt_n",
)

N_BUCKETS = 4


# ─────────────────────────────────────────────────────────────
# 统计
# ─────────────────────────────────────────────────────────────
def _mean(xs: Iterable[float]) -> float | None:
    v = [float(x) for x in xs]
    return sum(v) / len(v) if v else None


def _median(xs: Iterable[float]) -> float | None:
    v = sorted(float(x) for x in xs)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _t_stat(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return None
    return m / math.sqrt(var / n)


def _p_two_sided(t: float | None) -> float | None:
    """正态近似的双侧 p 值（样本量较大时足够；不作为唯一判据）。"""
    if t is None:
        return None
    z = abs(float(t)) / math.sqrt(2.0)
    return max(0.0, min(1.0, 1.0 - math.erf(z)))


def benjamini_hochberg(pvals: list[float]) -> list[float]:
    """BH-FDR 校正。返回与输入同序的校正后 p 值。"""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [1.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        val = min(prev, pvals[i] * m / rank)
        adj[i] = val
        prev = val
    return adj


def quantile_buckets(values: list[float], n_buckets: int = N_BUCKETS) -> list[tuple[float, float]]:
    """把取值切成有序分位区间。返回 [(lo, hi), ...]。"""
    vals = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if len(vals) < n_buckets * 4:
        return []
    # 注意最后一个切点必须夹到 len-1：k=n_buckets 时 len*k/n_buckets == len，
    # 直接用会 IndexError（这个越界是自检抓到的，不是猜的）。
    last = len(vals) - 1
    cuts = [vals[min(int(len(vals) * k / n_buckets), last)] for k in range(n_buckets + 1)]
    out = []
    for k in range(n_buckets):
        lo, hi = cuts[k], cuts[k + 1]
        if hi <= lo:
            continue
        out.append((lo, hi))
    return out


# ─────────────────────────────────────────────────────────────
# 候选条件
# ─────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    rule_id: str
    field: str
    op: str
    value: Any
    expr: dict
    desc: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "field": self.field, "op": self.op,
                "value": self.value, "expr": self.expr, "desc": self.desc,
                "evidence": self.evidence}


def _safe_id(field_name: str, suffix: str) -> str:
    base = "".join(ch if ch.isalnum() else "-" for ch in field_name).strip("-")
    return f"EVO-{base}-{suffix}".upper()


def build_conditions(
    rows: list[dict],
    *,
    dims: Iterable[str] = DEFAULT_DIMS,
    n_buckets: int = N_BUCKETS,
    skip_ids: set[str] | None = None,
) -> list[Candidate]:
    """按单维度分位切出候选条件。

    **只做单维度**：每条条件只有一个叶子（`MAX_LEAVES_PER_RULE`）。
    """
    skip_ids = skip_ids or set()
    out: list[Candidate] = []

    for dim in dims:
        vals = [r.get(dim) for r in rows
                if isinstance(r.get(dim), (int, float)) and not isinstance(r.get(dim), bool)]
        if len(vals) < 40:
            continue
        buckets = quantile_buckets(vals, n_buckets)
        for k, (lo, hi) in enumerate(buckets, 1):
            cond = {"all": [{"field": dim, "op": "between", "value": [lo, hi]}]}
            rid = _safe_id(dim, f"Q{k}")
            if rid in skip_ids:
                continue
            # 硬限制：一条规则一个叶子
            if len(expr_mod.flatten(cond)) > MAX_LEAVES_PER_RULE:
                continue
            out.append(Candidate(
                rule_id=rid, field=dim, op="between", value=[lo, hi], expr=cond,
                desc=f"{dim} ∈ [{lo:.4g}, {hi:.4g}]（第 {k}/{len(buckets)} 分位）",
            ))

        # 额外给「小于 0 / 大于 0」这种有先验含义的阈值（例如前5日回调）
        if dim.startswith("chg_"):
            for sign, op, val in (("NEG", "<", 0.0), ("POS", ">", 0.0)):
                rid = _safe_id(dim, sign)
                if rid in skip_ids:
                    continue
                out.append(Candidate(
                    rule_id=rid, field=dim, op=op, value=val,
                    expr={"all": [{"field": dim, "op": op, "value": val}]},
                    desc=f"{dim} {op} 0",
                ))
    return out


def evaluate_condition(
    rows: list[dict],
    condition: dict,
    *,
    baseline_mean: float | None = None,
    with_p: bool = True,
) -> dict[str, Any]:
    """对一条条件算证据。`ret_pct` 视为**已扣费净收益**。"""
    hit = [r for r in rows
           if expr_mod.is_valid(condition) and expr_mod.evaluate(condition, r)
           and isinstance(r.get("ret_pct"), (int, float))]
    rets = [float(r["ret_pct"]) for r in hit]
    n = len(rets)
    mean = _mean(rets)
    median = _median(rets)
    t = _t_stat(rets)

    by_year: dict[str, float] = {}
    per_year: dict[str, list[float]] = {}
    for r in hit:
        y = str(r.get("date") or "")[:4] or "?"
        per_year.setdefault(y, []).append(float(r["ret_pct"]))
    for y, vs in sorted(per_year.items()):
        if len(vs) >= 10:
            by_year[y] = round(_mean(vs), 3)

    same_sign = None
    if len(by_year) >= 2:
        pos = sum(1 for v in by_year.values() if v > 0)
        same_sign = f"{max(pos, len(by_year) - pos)}/{len(by_year)}"

    # 剔除权重最大的窗口（按月），这是 §9.7.2 的均值陷阱检验
    drop_mean = None
    if hit:
        per_month: dict[str, list[float]] = {}
        for r in hit:
            key = str(r.get("date") or "")[:6] or "?"
            per_month.setdefault(key, []).append(float(r["ret_pct"]))
        heaviest = max(per_month, key=lambda k: len(per_month[k]))
        rest = [v for k, vs in per_month.items() if k != heaviest for v in vs]
        if rest:
            drop_mean = round(_mean(rest), 3)

    evidence = {
        "n": n,
        "mean_pct": round(mean, 3) if mean is not None else None,
        "median_pct": round(median, 3) if median is not None else None,
        "winrate": round(sum(1 for v in rets if v > 0) / n * 100, 1) if n else None,
        "t_stat": round(t, 3) if t is not None else None,
        "by_year": by_year,
        "same_sign_years": same_sign,
        "drop_top_window_mean_pct": drop_mean,
        # ret_pct 已是净收益，故扣费后 = 均值本身（不重复扣费）
        "after_cost_pct": round(mean, 3) if mean is not None else None,
        "vs_baseline_pp": (round(mean - baseline_mean, 3)
                           if (mean is not None and baseline_mean is not None) else None),
        "cost_already_deducted": True,
    }
    if with_p:
        evidence["_p"] = _p_two_sided(t)
    return evidence


# ─────────────────────────────────────────────────────────────
# 主循环
# ─────────────────────────────────────────────────────────────
def run(
    rows: list[dict],
    *,
    dims: Iterable[str] = DEFAULT_DIMS,
    n_buckets: int = N_BUCKETS,
    skip_ids: set[str] | None = None,
    min_n: int = gate_mod.MIN_SAMPLES,
) -> dict[str, Any]:
    """跑一轮演化：枚举单维候选 → 算证据 → BH 校正 → 闸门裁定。

    返回 {"tested", "passed", "rejected", "baseline"}。
    **不写任何状态**；要落库请调 `propose()`。
    """
    usable = [r for r in rows
              if isinstance(r.get("ret_pct"), (int, float)) and not isinstance(r.get("ret_pct"), bool)]
    if len(usable) < min_n:
        return {"tested": [], "passed": [], "rejected": [],
                "baseline": {"n": len(usable), "mean_pct": None},
                "note": f"可用样本 {len(usable)} < {min_n}，本轮不做任何提议"}

    baseline_mean = _mean([float(r["ret_pct"]) for r in usable])
    skip = set(skip_ids or set()) | rules.rejected_keys() | set(rules.current().keys())

    cands = build_conditions(usable, dims=dims, n_buckets=n_buckets, skip_ids=skip)
    evaluated: list[dict] = []
    for cand in cands:
        ev = evaluate_condition(usable, cand.expr, baseline_mean=baseline_mean)
        if ev["n"] < min_n:
            continue
        evaluated.append({**cand.to_dict(), "evidence": ev})

    # BH-FDR：必须在**所有被测条件**上校正，不能只挑好看的
    ps = [e["evidence"].get("_p") for e in evaluated]
    idx = [i for i, p in enumerate(ps) if isinstance(p, (int, float))]
    if idx:
        adj = benjamini_hochberg([float(ps[i]) for i in idx])
        for slot, i in enumerate(idx):
            evaluated[i]["evidence"]["bh_fdr_p"] = round(adj[slot], 6)
    for e in evaluated:
        e["evidence"].pop("_p", None)

    passed, rejected = [], []
    for e in evaluated:
        verdict = gate_mod.evaluate(e["evidence"])
        e["gate"] = verdict.to_dict()
        (passed if verdict.passed else rejected).append(e)

    return {
        "baseline": {"n": len(usable), "mean_pct": round(baseline_mean, 3)
                     if baseline_mean is not None else None},
        "tested": evaluated,
        "passed": passed,
        "rejected": rejected,
    }


def propose(result: dict, *, author: str = "agent:propose",
            scope: str = "candidate_filter") -> dict[str, Any]:
    """把通过闸门的候选写成 hypothesis 规则；不通过的写进 rejected.jsonl。

    注意：即便「通过闸门」，这里也**只写 hypothesis** —— 推进到 testing 是
    `gate.promote()` 的事，活化到 active 必须由人/gate 再做一次决定。
    """
    created, archived, skipped = [], [], []

    for e in result.get("passed") or []:
        rid = e["rule_id"]
        if rid in rules.current():
            skipped.append({"rule_id": rid, "reason": "已存在"})
            continue
        try:
            rec = rules.append(
                rule_id=rid, scope=scope, desc_human=e["desc"], expr=e["expr"],
                author=author, evidence=e["evidence"],
                status_reason="演化器自动提议；已过 G1–G5，待 gate 推进到 testing",
            )
            created.append(rec)
        except rules.RuleError as exc:
            skipped.append({"rule_id": rid, "reason": str(exc)})

    for e in result.get("rejected") or []:
        rid = e["rule_id"]
        try:
            rules.record_rejection(
                rule_id=rid, expr=e["expr"],
                reason=(e.get("gate") or {}).get("verdict") or "未通过闸门",
                evidence=e["evidence"], actor="gate",
            )
            archived.append({"rule_id": rid,
                             "reason": (e.get("gate") or {}).get("verdict")})
        except Exception as exc:  # 归档失败不该中断整轮
            skipped.append({"rule_id": rid, "reason": f"归档失败: {exc}"})

    return {"created": created, "archived": archived, "skipped": skipped,
            "tested_n": len(result.get("tested") or [])}


__all__ = [
    "MAX_LEAVES_PER_RULE",
    "DEFAULT_DIMS",
    "N_BUCKETS",
    "Candidate",
    "benjamini_hochberg",
    "quantile_buckets",
    "build_conditions",
    "evaluate_condition",
    "run",
    "propose",
]
