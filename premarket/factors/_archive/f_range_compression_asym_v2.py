# -*- coding: utf-8 -*-
"""波动压缩不对称度(纯压缩版) —— 由因子讨论循环产出，内核回测通过后入库。

假设：当个股波动率在长窗口内显著压缩（横盘蓄势），说明多空分歧收敛、筹码沉淀，属于突破前的能量积累；压缩越极致，后续方向性释放的弹性越大。失效条件：单边下跌趋势中压缩常伴随阴跌而非蓄势，故仅在 above_ma20==1 时使用；同时压缩本身不含方向，需配合趋势门槛与流动性门槛过滤。
作者：factor-meeting　时间：2026-10-20T21:49:03+08:00
"""

META = {   'id': 'range_compression_asym_v2',
    'name': '波动压缩不对称度(纯压缩版)',
    'kind': 'numeric',
    'description': '近10日振幅均值相对近30日振幅均值的压缩比，仅保留压缩维度，剔除与close_pos语义重叠的上行占比项。',
    'source': 'discovered',
    'direction': 'high',
    'hypothesis': '当个股波动率在长窗口内显著压缩（横盘蓄势），说明多空分歧收敛、筹码沉淀，属于突破前的能量积累；压缩越极致，后续方向性释放的弹性越大。失效条件：单边下跌趋势中压缩常伴随阴跌而非蓄势，故仅在 '
                  'above_ma20==1 时使用；同时压缩本身不含方向，需配合趋势门槛与流动性门槛过滤。',
    'created_by': 'factor-meeting',
    'created_at': '2026-10-20T21:49:03+08:00'}

def compute(panel):
    out = {}
    for code in panel.codes():
        hs = panel.highs(code)
        ls = panel.lows(code)
        n = len(hs)
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
        out[code] = (1.0 - comp) * 100.0
    return out

PLAYS = [   {   'id': 'compression_break_watch_v2',
        'name': '压缩蓄势观察',
        'buyable': True,
        'conditions': [   'range_compression_asym_v2 >= 15.0',
                          'above_ma20 == 1',
                          'chg_today >= -2.0',
                          'chg_today < 6.0',
                          'amount_yi >= 1.5'],
        'evidence': ['range_compression_asym_v2', 'box_amp', 'vol_ratio'],
        'why': '波动压缩代表分歧收敛与筹码沉淀，配合趋势未破与流动性门槛，捕捉蓄势后向上释放的标的；阈值下调至15以扩大样本、避免选股池过小导致统计不可检验。'}]
