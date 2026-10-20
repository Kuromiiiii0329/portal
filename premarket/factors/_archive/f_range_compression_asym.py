# -*- coding: utf-8 -*-
"""波动压缩不对称度 —— 由因子讨论循环产出，内核回测通过后入库。

假设：当个股波动率在长窗口内显著压缩（横盘蓄势），且压缩期内收盘更常靠近当日区间上沿时，说明抛压衰竭而承接盘持续存在，属于突破前的能量积累；这类结构在无消息驱动的存量博弈市场中更易在随后数日向上释放。失效条件：单边下跌趋势中（MA20 下行）压缩不对称度会因反弹日噪声而虚高，故仅在 above_ma20==1 时使用。
作者：factor-meeting　时间：2026-10-20T21:45:55+08:00
"""

META = {   'id': 'range_compression_asym',
    'name': '波动压缩不对称度',
    'kind': 'numeric',
    'description': '近10日振幅均值相对近30日振幅均值的压缩比，减去近10日上行振幅占比，衡量缩量收敛中多头仍占优的程度。',
    'source': 'discovered',
    'direction': 'high',
    'hypothesis': '当个股波动率在长窗口内显著压缩（横盘蓄势），且压缩期内收盘更常靠近当日区间上沿时，说明抛压衰竭而承接盘持续存在，属于突破前的能量积累；这类结构在无消息驱动的存量博弈市场中更易在随后数日向上释放。失效条件：单边下跌趋势中（MA20 '
                  '下行）压缩不对称度会因反弹日噪声而虚高，故仅在 above_ma20==1 时使用。',
    'created_by': 'factor-meeting',
    'created_at': '2026-10-20T21:45:55+08:00'}

def compute(panel):
    out = {}
    for code in panel.codes():
        hs = panel.highs(code)
        ls = panel.lows(code)
        cs = panel.closes(code)
        n = len(cs)
        if n < 31:
            out[code] = 0.0
            continue
        amp10 = 0.0
        for i in range(n - 10, n):
            if ls[i] > 0:
                amp10 += (hs[i] - ls[i]) / ls[i]
        amp10 = amp10 / 10.0
        amp30 = 0.0
        for i in range(n - 30, n):
            if ls[i] > 0:
                amp30 += (hs[i] - ls[i]) / ls[i]
        amp30 = amp30 / 30.0
        if amp30 <= 0:
            out[code] = 0.0
            continue
        comp = amp10 / amp30
        up_share = 0.0
        for i in range(n - 10, n):
            rng = hs[i] - ls[i]
            if rng > 0:
                up_share += (cs[i] - ls[i]) / rng
        up_share = up_share / 10.0
        out[code] = (1.0 - comp) * 100.0 + up_share * 10.0
    return out

PLAYS = [   {   'id': 'compression_break_watch',
        'name': '压缩不对称蓄势',
        'buyable': True,
        'conditions': [   'range_compression_asym >= 25.0',
                          'above_ma20 == 1',
                          'chg_today >= -2.0',
                          'chg_today < 6.0',
                          'amount_yi >= 1.5'],
        'evidence': ['range_compression_asym', 'box_amp', 'vol_ratio'],
        'why': '波动压缩且区间内收盘偏上沿，代表抛压衰竭与承接持续，配合趋势未破与流动性门槛，捕捉蓄势后向上释放的标的，同时避开单边下跌中的假压缩。'}]
