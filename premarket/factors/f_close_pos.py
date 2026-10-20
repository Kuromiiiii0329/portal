# -*- coding: utf-8 -*-
"""
收盘位置 =（收盘 - 今日最低）/（今日最高 - 今日最低）× 100。

含义
----
100 表示收在当日最高（尾盘走强、买盘压制卖盘）；0 表示收在最低（冲高回落）。
它衡量的是**日内多空结果**，和涨跌幅是两件事：
涨 2% 但从 +6% 回落收盘，close_pos 会很低；涨 2% 且收在最高附近，close_pos 接近 100。

为什么这个因子不是"追高"
----------------------
它描述的是**收盘结构**，不是涨幅。战法里配合 chg_today < 7% 使用时，
选的是"温和上涨且收在强势位置"的票，而不是涨停板。
"""
from .base import safe

META = {
    "id": "close_pos",
    "name": "收盘位置",
    "kind": "numeric",
    "unit": "%",
    "description": "（收盘-最低）/（最高-最低）×100。100=收在最高，0=收在最低。",
    "source": "builtin",
    "direction": "high",
}

#: 本因子贡献的战法：强势收盘（尾盘走强）。
#: 注册表会自动合并，下一次午报/晚报就会用这个战法筛选候选股。
PLAYS = [
    {
        "id": "strong_close",
        "name": "强势收盘（尾盘走强）",
        "buyable": True,
        "why": "温和上涨且收在当日振幅上部，说明尾盘有承接；不在涨停板上接棒",
        "conditions": [
            "close_pos >= 80.0",
            "chg_today >= 1.0",
            "chg_today < 7.0",
            "vol_ratio >= 1.10",
            "above_ma20 == 1",
            "amount_yi >= 2.0",
        ],
        "evidence": ["close_pos", "chg_today", "vol_ratio", "above_ma20",
                     "amount_yi"],
        "sort": [["close_pos", "desc"], ["amount_yi", "desc"]],
    },
]


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        hi = safe(panel.high_today.get(c))
        lo = safe(panel.low_today.get(c))
        px = safe(panel.close.get(c))
        if not hi or not lo or not px or hi <= lo:
            continue
        out[c] = round((px - lo) / (hi - lo) * 100, 2)
    return out
