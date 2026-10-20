# -*- coding: utf-8 -*-
"""收盘是否在 MA20 上方（1/0）。判断中期趋势有没有坏。"""

from .base import ma

META = {
    "id": "above_ma20",
    "name": "站上MA20",
    "kind": "bool",
    "description": "收盘价 > MA20（含今日）→ 1。回踩战法要求它=1，趋势没坏。",
    "source": "builtin",
    "direction": "high",
}


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        s = panel.series_with_today(c)
        m20 = ma(s, 20)
        px = panel.close.get(c)
        if m20 is None or not px:
            continue
        out[c] = 1 if px > m20 else 0
    return out
