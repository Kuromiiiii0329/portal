# -*- coding: utf-8 -*-
"""
portal/premarket/factors/playbooks.py — 战法 = 因子的布尔组合

战法本身不写计算代码，只声明「要满足哪些因子条件」。这样：

- 想调阈值 → 改这里的一行，不动因子实现
- 想加战法 → 加一个 dict
- 想验证某个因子 → 直接看哪些战法用了它

条件表达式由 `base.compile_expr` 白名单求值（只用因子名 + 数字 + 比较/算术），
**没有属性访问、没有函数调用、没有 import**。
"""
from __future__ import annotations

from typing import Any

from .base import FactorError, compile_expr, eval_expr

#: 战法定义。`buyable=False` 的战法只产出观察标的，内核拒绝据此买入。
PLAYBOOKS: list[dict[str, Any]] = [
    {
        "id": "first_board_pullback",
        "name": "首板回踩低吸",
        "buyable": True,
        "why": "近期做过龙头的票缩量回踩 5 日线，属于强势股回调买点，不在高点接棒",
        "conditions": [
            "limit_up_10d >= 1",
            "chg_today >= -3.0",
            "chg_today <= 3.0",
            "ma5_dev >= -3.0",
            "ma5_dev <= 3.0",
            "vol_ratio <= 0.90",
            "above_ma20 == 1",
            "amount_yi >= 1.0",
        ],
        # 报告里按这个顺序解释每条条件为什么成立
        "evidence": ["limit_up_10d", "chg_today", "ma5_dev", "vol_ratio",
                     "above_ma20", "amount_yi"],
        # 回踩战法：越缩量越好、越贴 5 日线越好 —— 排序方向与突破战法相反
        "sort": [["vol_ratio", "asc"], ["amount_yi", "desc"]],
    },
    {
        "id": "platform_breakout",
        "name": "平台突破",
        "buyable": True,
        "why": "横盘够久、振幅够窄，今日放量站上箱体上沿 —— 启动位而非加速位",
        "conditions": [
            "box_amp <= 12.0",
            "box_break == 1",
            "vol_ratio >= 1.50",
            "chg_today >= 2.0",
            "chg_today < 7.0",
            "amount_yi >= 1.5",
        ],
        "evidence": ["box_amp", "box_break", "vol_ratio", "chg_today", "amount_yi"],
        "sort": [["vol_ratio", "desc"], ["amount_yi", "desc"]],
    },
    {
        "id": "gentle_trend",
        "name": "温和趋势放量",
        "buyable": True,
        "why": "多头排列 + 温和放量 + 5 日没被拉爆，跟着趋势走而不是追最后一棒",
        "conditions": [
            "ma_bull == 1",
            "chg_today >= 2.0",
            "chg_today < 7.0",
            "chg_5d < 15.0",
            "vol_ratio >= 1.20",
            "amount_yi >= 2.0",
        ],
        "evidence": ["ma_bull", "chg_today", "chg_5d", "vol_ratio", "amount_yi"],
        "sort": [["vol_ratio", "desc"], ["chg_5d", "asc"]],
    },
    {
        "id": "limit_up_chase",
        "name": "涨停/连板（仅观察）",
        "buyable": False,
        "why": "当日已封涨停：只进观察表等回调，内核禁止打板买入",
        "conditions": [],       # 由涨停池直接给出，不用因子筛
        "evidence": [],
    },
]

#: 禁止出现在战法条件里的因子 —— 这几个直接对应「追高打板」，
#: 任何战法引用它们当买入条件都会被内核否决。
FORBIDDEN_FOR_BUY = ("chg_today_ge7", "boards", "seal_yi", "limit_price_hit")

_BY_ID = {p["id"]: p for p in PLAYBOOKS}


def get(playbook_id: str) -> dict[str, Any] | None:
    return _BY_ID.get(playbook_id)


def buyable_ids() -> list[str]:
    return [p["id"] for p in PLAYBOOKS if p.get("buyable")]


def compiled(*, factor_ids: set[str]) -> dict[str, list[tuple[str, Any]]]:
    """把每个战法的条件编译成 AST。缺因子时报错（避免战法默默失效）。"""
    out: dict[str, list[tuple[str, Any]]] = {}
    for p in PLAYBOOKS:
        if not p["conditions"]:
            continue
        exprs = []
        for cond in p["conditions"]:
            tree = compile_expr(cond, allowed_names=factor_ids)
            exprs.append((cond, tree))
        out[p["id"]] = exprs
    return out


def check_no_chase(playbook: dict[str, Any]) -> None:
    """战法若以「追高/打板」为买入条件，直接拒绝注册。"""
    if not playbook.get("buyable"):
        return
    text = " ".join(playbook.get("conditions") or [])
    for bad in FORBIDDEN_FOR_BUY:
        if bad in text:
            raise FactorError(
                f"战法 {playbook['id']} 引用了 {bad}，属于追高/打板类条件，"
                f"与「禁止追高打板」冲突，不能作为买入战法")


def match(playbook_id: str, row: dict[str, Any],
          exprs: list[tuple[str, Any]]) -> tuple[bool, list[str]]:
    """对单只票判定是否命中战法。返回 (是否命中, 命中条件文本)。"""
    hits = []
    for cond, tree in exprs:
        try:
            ok = bool(eval_expr(tree, row))
        except Exception:
            ok = False          # 缺值/类型不对一律视为不命中
        if not ok:
            return False, []
        hits.append(cond)
    return True, hits


def sort_spec(playbook: dict[str, Any]) -> list[tuple[str, bool]]:
    """返回 [(因子, 是否降序)]。没写 sort 就按成交额降序。"""
    spec = playbook.get("sort") or [["amount_yi", "desc"]]
    return [(f, direction != "asc") for f, direction in spec]


def fmt_value(v: Any) -> str:
    """证据里显示实际值：整数原样，小数两位。"""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return str(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:.2f}"


__all__ = ["PLAYBOOKS", "get", "buyable_ids", "compiled", "match",
           "check_no_chase", "FORBIDDEN_FOR_BUY", "sort_spec",
           "fmt_value"]
