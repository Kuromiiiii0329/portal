# -*- coding: utf-8 -*-
"""收盘相对 MA5 的偏离（%）。回踩低吸要它接近 0（贴着 5 日线）。"""

from .base import ma, pct

META = {
    "id": "ma5_dev",
    "name": "距MA5偏离",
    "kind": "numeric",
    "unit": "%",
    "description": "(收盘 / MA5 - 1)×100，MA5 含今日。0 附近=贴着 5 日线。",
    "source": "builtin",
    "direction": "range",
}


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        s = panel.series_with_today(c)
        m5 = ma(s, 5)
        v = pct(panel.close.get(c), m5)
        if v is not None:
            out[c] = round(v, 3)
    return out
