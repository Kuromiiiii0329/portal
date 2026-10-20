# -*- coding: utf-8 -*-
"""均线多头排列（MA5 > MA10 > MA20），1/0。"""

from .base import ma

META = {
    "id": "ma_bull",
    "name": "均线多头排列",
    "kind": "bool",
    "description": "MA5 > MA10 > MA20（均含今日收盘）→ 1，否则 0。",
    "source": "builtin",
    "direction": "high",
}


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        s = panel.series_with_today(c)
        m5, m10, m20 = ma(s, 5), ma(s, 10), ma(s, 20)
        if m5 is None or m10 is None or m20 is None:
            continue
        out[c] = 1 if (m5 > m10 > m20) else 0
    return out
