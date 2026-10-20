# -*- coding: utf-8 -*-
"""近 10 个交易日出现过的涨停次数（不含今日）。

用途：判断这只票"做过龙头没有"。首板回踩战法要求 ≥1。
注意：这是**历史属性**，不是"今天涨停"，因此不与「禁止追高打板」冲突。
"""

from .base import pct

META = {
    "id": "limit_up_10d",
    "name": "近10日涨停次数",
    "kind": "numeric",
    "unit": "次",
    "description": "近 10 个交易日（不含今日）收盘涨幅达到涨停的交易日数。"
                   "衡量是否曾为板块龙头，与当日是否涨停无关。",
    "source": "builtin",
    "direction": "high",
}
LOOKBACK = 10
#: 判定「当日涨停」的容差：20cm 板与 10cm 板用各自的涨停幅度减 0.6
TOL = 0.6


def _limit_pct(panel, code: str) -> float:
    return panel.limit_pct.get(code) or 10.0


def compute(panel) -> dict:
    out = {}
    for c in panel.codes():
        closes = panel.closes(c)
        if len(closes) < LOOKBACK + 1:
            continue
        lp = _limit_pct(panel, c)
        window = closes[-(LOOKBACK + 1):]
        hits = 0
        for i in range(1, len(window)):
            v = pct(window[i], window[i - 1])
            if v is not None and v >= lp - TOL:
                hits += 1
        out[c] = hits
    return out
