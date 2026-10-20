# -*- coding: utf-8 -*-
"""量比 = 今日成交量 / 前 5 日均量。

放量突破要 >1.2~1.5，缩量回踩要 <0.9。**不含今日**算均量，否则自比较无意义。
"""

META = {
    "id": "vol_ratio",
    "name": "量比",
    "kind": "numeric",
    "description": "今日成交量 / 前 5 日均量（不含今日）。>1.5 显著放量，<0.9 缩量。",
    "source": "builtin",
    "direction": "high",
}
LOOKBACK = 5


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        vols = panel.vols(c)
        vol = panel.volume.get(c)
        if not vol or len(vols) < LOOKBACK:
            continue
        base = sum(vols[-LOOKBACK:]) / LOOKBACK
        if base > 0:
            out[c] = round(vol / base, 3)
    return out
