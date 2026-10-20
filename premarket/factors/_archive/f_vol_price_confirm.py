# -*- coding: utf-8 -*-
"""量价确认 —— 由因子会议产出，内核校验后入库。

假设：放量+强势收盘说明有真实买盘承接
作者：factor-meeting　时间：2026-10-20T20:40:40+08:00
"""

META = {   'id': 'vol_price_confirm',
    'name': '量价确认',
    'kind': 'numeric',
    'description': '放量且收盘靠当日高位',
    'source': 'discovered',
    'direction': 'high',
    'hypothesis': '放量+强势收盘说明有真实买盘承接',
    'created_by': 'factor-meeting',
    'created_at': '2026-10-20T20:40:40+08:00'}

def compute(panel):
    out = {}
    for c in panel.codes():
        px = panel.close.get(c)
        hi = panel.high_today.get(c)
        lo = panel.low_today.get(c)
        prev = panel.prev_close.get(c)
        if not px or not hi or not lo or not prev or hi <= lo:
            continue
        pos = (px - lo) / (hi - lo)
        chg = (px / prev - 1) * 100
        out[c] = round(pos * (1 + chg / 100), 4)
    return out

PLAYS = [   {   'id': 'vol_price_go',
        'name': '量价确认启动',
        'buyable': True,
        'why': '放量且收在日内高位',
        'conditions': [   'vol_price_confirm >= 0.9',
                          'chg_today >= 1.0',
                          'chg_today < 7.0',
                          'amount_yi >= 2.0'],
        'evidence': ['vol_price_confirm', 'chg_today', 'amount_yi'],
        'sort': [['vol_price_confirm', 'desc']]}]
