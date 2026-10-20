# -*- coding: utf-8 -*-
"""三日反转 —— 由因子讨论循环产出，内核回测通过后入库。

假设：三日涨幅越低越好
作者：factor-meeting　时间：2026-10-20T20:51:40+08:00
"""

META = {   'id': 'rev_win3',
    'name': '三日反转',
    'kind': 'numeric',
    'description': '短期反转',
    'source': 'discovered',
    'direction': 'high',
    'hypothesis': '三日涨幅越低越好',
    'created_by': 'factor-meeting',
    'created_at': '2026-10-20T20:51:40+08:00'}

def compute(panel):
    out = {}
    for c in panel.codes():
        closes = panel.closes(c)
        if len(closes) < 3:
            continue
        px = panel.close.get(c)
        base = closes[-3]
        if not px or not base:
            continue
        out[c] = round((px / base - 1) * -1, 4)
    return out
