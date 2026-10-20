# -*- coding: utf-8 -*-
"""20 日箱体振幅（%）=（箱顶 - 箱底）/ 箱底 × 100，只用历史（不含今日）。

振幅越小说明横盘越充分，突破才有意义。
"""

META = {
    "id": "box_amp",
    "name": "20日箱体振幅",
    "kind": "numeric",
    "unit": "%",
    "description": "近 20 个交易日（不含今日）最高/最低构成箱体的振幅。越小越是横盘平台。",
    "source": "builtin",
    "direction": "low",
}
BOX_DAYS = 20


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        hi = panel.highs(c)[-BOX_DAYS:]
        lo = panel.lows(c)[-BOX_DAYS:]
        if len(hi) < BOX_DAYS or len(lo) < BOX_DAYS:
            continue
        top, bot = max(hi), min(lo)
        if bot > 0:
            out[c] = round((top - bot) / bot * 100, 3)
    return out
