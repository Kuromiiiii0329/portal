# -*- coding: utf-8 -*-
"""日内反转强度 —— 由因子会议产出，内核校验后入库。

假设：在全市场层面，日内被砸出低点后又被买回（收盘位置高、下影长）说明有承接资金，短期存在修复性上涨；该信号与当日涨幅无关，不构成追高。
作者：factor-meeting　时间：2026-10-20T20:41:02+08:00
"""

META = {   'id': 'intraday_reversal_strength',
    'name': '日内反转强度',
    'kind': 'numeric',
    'description': '今日低点相对昨收的跌幅与收盘位置的组合，衡量低开/下探后被资金拉回的力度。',
    'source': 'discovered',
    'direction': 'high',
    'hypothesis': '在全市场层面，日内被砸出低点后又被买回（收盘位置高、下影长）说明有承接资金，短期存在修复性上涨；该信号与当日涨幅无关，不构成追高。',
    'created_by': 'factor-meeting',
    'created_at': '2026-10-20T20:41:02+08:00'}

def compute(panel):
    out = {}
    codes = panel.codes()
    for c in codes:
        close = panel.close.get(c)
        prev = panel.prev_close.get(c)
        low = panel.low_today.get(c)
        high = panel.high_today.get(c)
        if close is None or prev is None or low is None or high is None:
            continue
        if prev <= 0 or high <= low:
            continue
        down = (prev - low) / prev * 100.0
        pos = (close - low) / (high - low) * 100.0
        if down <= 0:
            out[c] = 0.0
        else:
            out[c] = down * pos / 100.0
    return out

PLAYS = [   {   'id': 'reversal_low_absorb',
        'name': '下探回升承接',
        'buyable': True,
        'conditions': [   'intraday_reversal_strength >= 1.5',
                          'close_pos >= 60.0',
                          'chg_today >= -3.0',
                          'chg_today < 5.0',
                          'above_ma20 == 1',
                          'amount_yi >= 1.5'],
        'evidence': ['intraday_reversal_strength', 'close_pos', 'above_ma20'],
        'why': '日内被砸后收复大半跌幅且站上MA20，属于有资金承接的回踩，非追高。'}]
