# -*- coding: utf-8 -*-
"""5 日累计涨幅（%）。防止在已经被拉爆的位置进场。"""

from .base import pct

META = {
    "id": "chg_5d",
    "name": "5日累计涨幅",
    "kind": "numeric",
    "unit": "%",
    "description": "今日收盘相对 5 个交易日前收盘的累计涨幅。",
    "source": "builtin",
    "direction": "range",
}
LOOKBACK = 5


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        closes = panel.closes(c)
        if len(closes) < LOOKBACK:
            continue
        v = pct(panel.close.get(c), closes[-LOOKBACK])
        if v is not None:
            out[c] = round(v, 3)
    return out
