# -*- coding: utf-8 -*-
"""
开盘缺口 =（今开 / 昨收 - 1）× 100。

含义
----
正缺口 = 高开；负缺口 = 低开。用它区分「一根阳线是怎么来的」：
- 高开高走 → 缺口正、收盘位置高
- 低开高走 → 缺口负、收盘位置高（更"自力更生"的强势）
- 高开低走 → 缺口正、收盘位置低（出货形态）

这个因子目前**没有**被任何战法引用 —— 这是有意的演示：
新增一个因子文件不等于必须立刻上战法。它会立刻出现在
`python -m portal.premarket.factors list` 与 `check` 里，
之后随时可以被写进战法条件（或由 agent 讨论后写进）。
"""
from .base import pct, safe

META = {
    "id": "gap_pct",
    "name": "开盘缺口",
    "kind": "numeric",
    "unit": "%",
    "description": "（今开 / 昨收 - 1）×100。正=高开，负=低开。用于区分阳线的来源。",
    "source": "builtin",
    "direction": "range",
}


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        op = safe(panel.open_today.get(c))
        prev = safe(panel.prev_close.get(c))
        v = pct(op, prev)
        if v is not None:
            out[c] = round(v, 3)
    return out
