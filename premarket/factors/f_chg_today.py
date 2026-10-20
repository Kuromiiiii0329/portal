# -*- coding: utf-8 -*-
"""今日涨幅（%）。所有战法都要用它做区间过滤，也是「禁止追高」的第一道尺。"""

from .base import pct

META = {
    "id": "chg_today",
    "name": "今日涨幅",
    "kind": "numeric",
    "unit": "%",
    "description": "收盘相对昨收的涨跌幅。涨停≈10/20，内核禁止 ≥7%。",
    "source": "builtin",
    "direction": "range",
}


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        v = pct(panel.close.get(c), panel.prev_close.get(c))
        if v is not None:
            out[c] = round(v, 3)
    return out
