# -*- coding: utf-8 -*-
"""成交额（亿元）。流动性门槛 —— 太小的票买不进也卖不出。"""

META = {
    "id": "amount_yi",
    "name": "成交额",
    "kind": "numeric",
    "unit": "亿",
    "description": "今日成交额（亿元）。作为流动性门槛使用。",
    "source": "builtin",
    "direction": "high",
}


def compute(panel) -> dict:
    return {c: v for c, v in panel.amount_yi.items() if v is not None}
