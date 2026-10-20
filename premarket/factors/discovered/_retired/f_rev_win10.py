# -*- coding: utf-8 -*-
"""十日反转 —— 由因子讨论循环产出，内核回测通过后入库。

假设：十日反转更稳
作者：factor-meeting　时间：2026-10-20T20:51:34+08:00
"""

META = {   'id': 'rev_win10',
    'name': '十日反转',
    'kind': 'numeric',
    'description': '拉长窗口',
    'source': 'discovered',
    'direction': 'high',
    'hypothesis': '十日反转更稳',
    'created_by': 'factor-meeting',
    'created_at': '2026-10-20T20:51:34+08:00'}

def compute(panel):
    out = {}
    for c in panel.codes():
        closes = panel.closes(c)
        if len(closes) < 10:
            continue
        px = panel.close.get(c)
        base = closes[-10]
        if not px or not base:
            continue
        out[c] = round((px / base - 1) * -1, 4)
    return out
