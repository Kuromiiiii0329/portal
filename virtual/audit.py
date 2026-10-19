# -*- coding: utf-8 -*-
"""
portal/virtual/audit.py — 报告签发前自检（A1–A7）

准确率的最后一道防线。它不判断「观点对不对」，只检查**这份报告有没有资格被签发**。

七条检查，每条都对应一个真实缺陷：

| # | 检查 | 防的是 |
|---|---|---|
| A1 | 买入结论必须附可买性 / 被排除项 / 空仓出口 | 「给一只今天封死涨停的票，却不校验明天能否成交」 |
| A2 | 每个数字都能找到来源 | LLM 编数字 / 记忆里的旧数字混入 |
| A3 | 文本参数与机器参数一致 | `SKILL.md` §9.7.3：文本说 Z1 适用、引擎 Z1 关闭 |
| A4 | 同步指标不许写成预测句式 | 华泰金工：量价类情绪指标无领先性 |
| A5 | 扣费后为负必须标注 | 把负期望包装成「优于基率」 |
| A6 | n<30 必须标注样本极少 | 18 个样本做不了统计 |
| A7 | 已知偏差必须显式声明 | 假装没有回放/实操之间的偏差 |

任意一条 `blocker` 不通过 → **拒绝签发**。
`warn` 只提示，不拦。

设计说明
--------
A4 用正则，而正则最容易变成「永远不报」或「永远报」。所以自检里对 A4 做了
**双向对照**：一个必须被抓的正例 + 一个必须放过的负例。负例是合规写法
（「情绪读数为同步指标，据此降低仓位」），它不含预测句式，因此不该命中。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

BLOCKER = "blocker"
WARN = "warn"

#: A4：把同步指标写成预测句式的典型句式
_SYNC_PREDICTIVE = [
    # 「冰点之后必然反弹」「退潮之后明日将反转」
    re.compile(r"(冰点|退潮|高潮|发酵|分歧)[^。；\n]{0,20}"
               r"(所以|因此|明日|明天|将会|必然|即将|预示|反转|反弹)"),
    # 「涨停家数预示…」「炸板率提前…」
    re.compile(r"(涨停家数|跌停家数|炸板率|连板高度|封板率)[^。；\n]{0,15}"
               r"(预示|提前|将会|明日|必然|说明明天)"),
    # 「情绪冰点，明日…」
    re.compile(r"情绪[^。；\n]{0,10}(冰点|退潮)[^。；\n]{0,10}(明日|明天|将|必然|反转)"),
]


@dataclass
class Finding:
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


def find_sync_predictive(text: str) -> list[str]:
    """返回命中「同步指标当预测」的片段。空列表 = 合规。"""
    hits: list[str] = []
    for line in str(text or "").splitlines():
        for pat in _SYNC_PREDICTIVE:
            m = pat.search(line)
            if m:
                hits.append(m.group(0).strip())
                break
    return hits


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def audit(payload: dict) -> dict[str, Any]:
    """对一份报告载荷跑 A1–A7。

    Args:
        payload: 结构见模块 docstring / `docs/p3-*.md`

    Returns:
        {"ok": bool, "blocked": bool, "findings": [...], "checked": [...]}
    """
    p = payload or {}
    findings: list[Finding] = []
    checked: list[str] = []

    # ── A1 买入结论的三件套
    checked.append("A1")
    claims = p.get("claims") or []
    for i, c in enumerate(claims):
        if not isinstance(c, dict) or c.get("action") != "buy":
            continue
        tag = f"claims[{i}]({c.get('name') or c.get('code') or '?'})"
        for field, label in (("buyability", "可买性校验"),
                             ("excluded_listed", "被排除项"),
                             ("empty_exit", "空仓出口")):
            if not c.get(field):
                findings.append(Finding(
                    "A1", BLOCKER, f"{tag} 是买入结论，但缺少{label}"))

    # ── A2 数字必须有来源
    checked.append("A2")
    for i, n in enumerate(p.get("numbers") or []):
        if not isinstance(n, dict):
            continue
        if not str(n.get("source") or "").strip():
            findings.append(Finding(
                "A2", BLOCKER,
                f"numbers[{i}] 的值 {n.get('value')!r} 没有标注来源"
                f"（label={n.get('label')!r}）—— 报告里不许有来路不明的数字"))

    # ── A3 文本参数 vs 机器参数
    checked.append("A3")
    pt = p.get("params_text") or {}
    pm = p.get("params_machine") or {}
    for k in sorted(set(pt) & set(pm)):
        if pt[k] != pm[k]:
            findings.append(Finding(
                "A3", BLOCKER,
                f"参数 {k} 文本写 {pt[k]!r}，引擎实际 {pm[k]!r} —— "
                f"以机器可读参数为准，文本必须改到一致"))

    # ── A4 同步指标当预测
    checked.append("A4")
    hits = find_sync_predictive(p.get("text") or "")
    for h in hits:
        findings.append(Finding(
            "A4", BLOCKER,
            f"把同步指标写成了预测句式：「{h}」—— 情绪类指标无领先性，"
            f"只能用于仓位（刹车），不能用于择时（方向盘）"))

    # ── A5 扣费后为负必须标注
    checked.append("A5")
    bt = p.get("backtest") or {}
    after = bt.get("after_cost_pct")
    if _is_num(after) and after <= 0 and not bt.get("negative_labeled"):
        findings.append(Finding(
            "A5", BLOCKER,
            f"扣费后为 {after:+.3f}%，但没有标注为负期望 —— "
            f"不许把负期望包装成「优于基率」"))

    # ── A6 小样本必须标注
    checked.append("A6")
    n = bt.get("n")
    if _is_num(n) and n < 30 and not bt.get("insufficient_labeled"):
        findings.append(Finding(
            "A6", BLOCKER, f"n={int(n)} < 30，但报告没有标注「样本极少」"))

    # ── A7 已知偏差必须声明
    checked.append("A7")
    if bt and not p.get("known_bias_declared"):
        findings.append(Finding(
            "A7", BLOCKER,
            "报告引用了回测/结算数据，但没有声明已知偏差"
            "（如「回放用最终开盘价，实操用 9:24 虚拟价」）"))

    blocked = any(f.severity == BLOCKER for f in findings)
    return {
        "ok": not blocked,
        "blocked": blocked,
        "findings": [f.to_dict() for f in findings],
        "checked": checked,
    }


def assert_signable(payload: dict) -> None:
    """不通过就抛错。报告生成器在落盘前调用。"""
    result = audit(payload)
    if result["blocked"]:
        lines = [f"[{f['code']}] {f['message']}" for f in result["findings"]
                 if f["severity"] == BLOCKER]
        raise ValueError("报告未通过签发检查：\n  " + "\n  ".join(lines))


__all__ = ["BLOCKER", "WARN", "Finding", "audit", "assert_signable",
           "find_sync_predictive"]
