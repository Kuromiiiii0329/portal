# -*- coding: utf-8 -*-
"""量价背离度 —— 由因子会议产出，内核校验后入库。

假设：温和缩量上行（价格涨但量能未过度放大）代表筹码稳定、抛压小，后续更易延续；放量滞涨则风险高。该因子用历史序列计算，不含未来数据。
作者：factor-meeting　时间：2026-10-20T20:41:17+08:00
"""

META = {   'id': 'volume_price_divergence',
    'name': '量价背离度',
    'kind': 'numeric',
    'description': '近5日价格累计涨幅与近5日量能相对20日量能变化的背离程度，衡量缩量上涨/放量滞涨。',
    'source': 'discovered',
    'direction': 'high',
    'hypothesis': '温和缩量上行（价格涨但量能未过度放大）代表筹码稳定、抛压小，后续更易延续；放量滞涨则风险高。该因子用历史序列计算，不含未来数据。',
    'created_by': 'factor-meeting',
    'created_at': '2026-10-20T20:41:17+08:00'}

def compute(panel):
    out = {}
    codes = panel.codes()
    for c in codes:
        vols = panel.vols(c)
        closes = panel.closes(c)
        if vols is None or closes is None:
            continue
        n = len(vols)
        m = len(closes)
        if n < 10 or m < 6:
            continue
        v5 = 0.0
        for i in range(n - 5, n):
            v5 = v5 + vols[i]
        v5 = v5 / 5.0
        v20 = 0.0
        cnt = 0
        start = n - 20
        if start < 0:
            start = 0
        for i in range(start, n):
            v20 = v20 + vols[i]
            cnt = cnt + 1
        if cnt <= 0:
            continue
        v20 = v20 / cnt
        if v20 <= 0:
            continue
        vr = v5 / v20
        c_now = closes[m - 1]
        c_5 = closes[m - 6]
        if c_5 <= 0:
            continue
        r5 = (c_now / c_5 - 1.0) * 100.0
        out[c] = r5 - (vr - 1.0) * 10.0
    return out

PLAYS = [   {   'id': 'quiet_uptrend',
        'name': '缩量温和上行',
        'buyable': True,
        'conditions': [   'volume_price_divergence >= 1.0',
                          'volume_price_divergence <= 12.0',
                          'chg_today >= 0.0',
                          'chg_today < 5.0',
                          'ma_bull == 1',
                          'amount_yi >= 1.5'],
        'evidence': ['volume_price_divergence', 'ma_bull'],
        'why': '均线多头且价格温和上行、量能未过度放大，筹码稳定，回撤风险较低。'}]
