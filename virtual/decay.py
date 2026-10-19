# -*- coding: utf-8 -*-
"""
portal/virtual/decay.py — 衰减监控（机械降级）

规则活化不是终点。SKILL §9.3 的实测结论是：**正期望逐年衰减**
（低位档 2024 +0.982% → 2026 +0.189%）。所以「这周好使」不能当成常态。

这个模块只做一件事：**连续 N 次低于阈值就降级**，而且由机械规则执行，
不靠人发现。

为什么必须是机械的
------------------
人的失效发现机制是「亏到受不了」——那时已经晚了。而规则失效的第一信号是
「统计上不再为正」，它在亏损变成痛之前就出现。把这件事交给代码，等于给自己
装了一个不带情绪的温度计。

权限设计
--------
- 累加 `consecutive_fail`：`rules.revise()`，允许 decay_monitor/gate/human
- `active → decaying`：`rules.transition()`，**只允许 decay_monitor / gate**
- `decaying → active`（复活）：**必须 human/gate** —— 复活是重新押注，不是自动的

阈值默认 3 次：少于 3 次的波动是噪声；`SKILL.md` 的纪律是「连续 N 次低于阈值
→ 降权」，这里取 N=3。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from . import rules

DEFAULT_CONSECUTIVE = 3
DEFAULT_THRESHOLD_PCT = 0.0


@dataclass
class Observation:
    rule_id: str
    rolling_n: int
    rolling_mean_pct: float | None
    threshold_pct: float
    consecutive_fail: int
    action: str            # none | counted | demoted | skipped
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id, "rolling_n": self.rolling_n,
            "rolling_mean_pct": self.rolling_mean_pct,
            "threshold_pct": self.threshold_pct,
            "consecutive_fail": self.consecutive_fail,
            "action": self.action, "detail": self.detail,
        }


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(float(v)) else None


def observe(
    rule_id: str,
    rolling_mean_pct: float | None,
    *,
    rolling_n: int = 0,
    actor: str = "decay_monitor",
    threshold_pct: float | None = None,
    consecutive_limit: int | None = None,
) -> Observation:
    """喂一次滚动表现，按需累加失败计数或降级。

    只处理 `active` / `decaying` 的规则；其余状态直接跳过（没活化的规则没有
    「失效」一说）。
    """
    cur = rules.get(rule_id)
    if cur is None:
        return Observation(rule_id, rolling_n, rolling_mean_pct,
                           threshold_pct or DEFAULT_THRESHOLD_PCT, 0,
                           "skipped", "规则不存在")

    status = cur.get("status")
    if status not in ("active", "decaying"):
        return Observation(rule_id, rolling_n, rolling_mean_pct,
                           threshold_pct or DEFAULT_THRESHOLD_PCT, 0,
                           "skipped", f"状态 {status} 不参与衰减监控（未活化）")

    mon = dict(cur.get("decay_monitor") or {})
    thr = threshold_pct
    if thr is None:
        thr = _num(mon.get("threshold_pct")) or DEFAULT_THRESHOLD_PCT
    limit = consecutive_limit or int(mon.get("rolling_n") or DEFAULT_CONSECUTIVE)
    if limit <= 0:
        limit = DEFAULT_CONSECUTIVE

    mean = _num(rolling_mean_pct)
    if mean is None:
        # 取不到表现数据时不累加也不重置 —— 静默把失败清零等于掩盖失效
        return Observation(rule_id, rolling_n, None, float(thr),
                           int(mon.get("consecutive_fail") or 0),
                           "skipped", "本轮没有表现数据，计数保持不变（不清零）")

    fail = mean < float(thr)
    count = int(mon.get("consecutive_fail") or 0)
    count = count + 1 if fail else 0

    if count >= limit and status == "active":
        new_mon = {**mon, "rolling_n": rolling_n, "threshold_pct": float(thr),
                   "consecutive_fail": count,
                   "last_rolling_mean_pct": mean,
                   "demoted_at_round": count}
        rules.transition(
            rule_id, "decaying", actor=actor,
            reason=(f"连续 {count} 次滚动均值低于阈值 {thr:+.3f}% "
                    f"（最近一次 {mean:+.3f}%，n={rolling_n}）"),
            decay_monitor=new_mon,
        )
        return Observation(rule_id, rolling_n, mean, float(thr), count,
                           "demoted", f"连续 {count} 次低于阈值 → 降级为 decaying")

    new_mon = {**mon, "rolling_n": rolling_n, "threshold_pct": float(thr),
               "consecutive_fail": count, "last_rolling_mean_pct": mean}
    rules.revise(rule_id, actor=actor, decay_monitor=new_mon,
                 status_reason=cur.get("status_reason", ""))
    return Observation(rule_id, rolling_n, mean, float(thr), count,
                       "counted" if fail else "none",
                       ("低于阈值，计数 +1" if fail else "达标，计数清零"))


def sweep(perf: dict[str, float | None], *, actor: str = "decay_monitor") -> list[Observation]:
    """对一批规则的滚动表现跑一遍。`perf` = {rule_id: rolling_mean_pct}。"""
    out: list[Observation] = []
    for rid, mean in perf.items():
        out.append(observe(rid, mean, actor=actor))
    return out


def revive(rule_id: str, *, actor: str = "human:operator", reason: str = "") -> dict:
    """把 decaying 复活为 active。**必须 human/gate**，并写清理由。"""
    if not reason:
        raise rules.RuleError("复活规则必须写明理由（复活是重新押注，不是自动的）")
    return rules.transition(rule_id, "active", actor=actor, reason=reason,
                            decay_monitor={"consecutive_fail": 0})


def health() -> list[dict]:
    """所有活化规则的衰减健康度（给报告/前端）。"""
    out = []
    for rid, rec in sorted(rules.current().items()):
        if rec.get("status") not in ("active", "decaying"):
            continue
        mon = rec.get("decay_monitor") or {}
        out.append({
            "rule_id": rid,
            "status": rec.get("status"),
            "consecutive_fail": int(mon.get("consecutive_fail") or 0),
            "last_rolling_mean_pct": mon.get("last_rolling_mean_pct"),
            "threshold_pct": mon.get("threshold_pct"),
            "reason": rec.get("status_reason"),
        })
    return out


__all__ = [
    "DEFAULT_CONSECUTIVE",
    "DEFAULT_THRESHOLD_PCT",
    "Observation",
    "observe",
    "sweep",
    "revive",
    "health",
]
