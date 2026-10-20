# -*- coding: utf-8 -*-
"""收盘是否站上 20 日箱体上沿（1/0）。平台突破战法的核心条件。"""

META = {
    "id": "box_break",
    "name": "站上箱体上沿",
    "kind": "bool",
    "description": "收盘 ≥ 近 20 日（不含今日）最高价 × 0.995 → 1。"
                  "（作为门槛使用：突破与否的门槛：==1 才考虑，不用于排序）",
    "source": "builtin",
    "direction": "filter",
}
BOX_DAYS = 20
RATIO = 0.995


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        hi = panel.highs(c)[-BOX_DAYS:]
        px = panel.close.get(c)
        if len(hi) < BOX_DAYS or not px:
            continue
        top = max(hi)
        if top > 0:
            out[c] = 1 if px >= top * RATIO else 0
    return out
