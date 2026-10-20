# -*- coding: utf-8 -*-
"""上影线回吐承接度 —— 由因子讨论循环产出，内核回测通过后入库。

假设：前期多次冲高回落（上影线长）说明有抛压，但若今日收盘位置高、且当日未大涨，说明抛压已被消化、资金在低位重新承接，后续更易延续上行；相比单纯反转，加入上影线历史可过滤噪声。
作者：factor-meeting　时间：2026-10-20T21:22:44+08:00
"""

META = {   'id': 'upper_shadow_absorb',
    'name': '上影线回吐承接度',
    'kind': 'numeric',
    'description': '近5日累计上影线幅度与今日收盘位置的组合，衡量前期冲高回落后今日是否被资金重新承接。',
    'source': 'discovered',
    'direction': 'high',
    'hypothesis': '前期多次冲高回落（上影线长）说明有抛压，但若今日收盘位置高、且当日未大涨，说明抛压已被消化、资金在低位重新承接，后续更易延续上行；相比单纯反转，加入上影线历史可过滤噪声。',
    'created_by': 'factor-meeting',
    'created_at': '2026-10-20T21:22:44+08:00'}

def compute(panel):
    out = {}
    for code in panel.codes():
        hs = panel.highs(code)
        cs = panel.closes(code)
        os_ = panel.open_today.get(code)
        hi = panel.high_today.get(code)
        lo = panel.low_today.get(code)
        cl = panel.close.get(code)
        if os_ is None or hi is None or lo is None or cl is None:
            continue
        if len(hs) < 6 or len(cs) < 6:
            continue
        shadow_sum = 0.0
        cnt = 0
        n = len(hs)
        for i in range(n - 5, n):
            h = hs[i]
            c = cs[i]
            if h is None or c is None or c <= 0:
                continue
            shadow_sum += (h - c) / c * 100.0
            cnt += 1
        if cnt < 3:
            continue
        avg_shadow = shadow_sum / cnt
        rng = hi - lo
        if rng <= 0:
            pos = 50.0
        else:
            pos = (cl - lo) / rng * 100.0
        out[code] = avg_shadow * (pos / 100.0)
    return out

PLAYS = [   {   'id': 'shadow_absorb_pullback',
        'name': '上影回吐后承接低吸',
        'buyable': True,
        'conditions': [   'upper_shadow_absorb >= 1.0',
                          'upper_shadow_absorb <= 8.0',
                          'close_pos >= 55.0',
                          'chg_today >= -2.0',
                          'chg_today < 5.0',
                          'above_ma20 == 1',
                          'amount_yi >= 1.5'],
        'evidence': ['upper_shadow_absorb', 'close_pos', 'chg_today'],
        'why': '前期有冲高回落抛压，但今日收在中上部且未追高，说明抛压被消化、资金承接，符合低吸而非追高逻辑。'}]
