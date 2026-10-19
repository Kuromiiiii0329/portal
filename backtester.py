"""
portal/backtester.py — 简单信号回测引擎
统计各技术信号触发后 1/3/5/10/20 日的平均收益率和胜率
"""
from __future__ import annotations
import logging
from pathlib import Path
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

SIGNALS = [
    ("MACD金叉",        lambda r: r.get("dif",0) > r.get("dea",0) and r.get("prev_dif",0) <= r.get("prev_dea",0)),
    ("MACD死叉",        lambda r: r.get("dif",0) < r.get("dea",0) and r.get("prev_dif",0) >= r.get("prev_dea",0)),
    ("RSI超卖(<30)",    lambda r: r.get("rsi6",50) < 30),
    ("RSI超买(>70)",    lambda r: r.get("rsi6",50) > 70),
    ("KDJ金叉",         lambda r: r.get("k",50) > r.get("d",50) and r.get("prev_k",50) <= r.get("prev_d",50)),
    ("放量上涨",         lambda r: r.get("vol_ratio",1) > 2 and r.get("close_chg",0) > 0),
    ("放量下跌",         lambda r: r.get("vol_ratio",1) > 2 and r.get("close_chg",0) < 0),
    ("均线多头排列",     lambda r: r.get("ma5",0) > r.get("ma10",0) > r.get("ma20",0) > 0),
    ("价格跌破布林下轨", lambda r: r.get("close",0) < r.get("boll_lower",0) > 0),
]

def run_backtest(df: pd.DataFrame, forward_days=(1, 3, 5, 10, 20)) -> dict:
    """对历史K线数据运行所有信号回测，返回胜率统计。"""
    if df is None or len(df) < 60:
        return {"error": "数据不足60日，无法回测"}

    df = df.copy().sort_values("date").reset_index(drop=True)
    df = _compute_indicators(df)
    closes = df["close"].values
    n = len(df)
    results = {}

    for sig_name, sig_fn in SIGNALS:
        win_counts = {d: 0 for d in forward_days}
        ret_sums   = {d: 0.0 for d in forward_days}
        valid_counts = {d: 0 for d in forward_days}  # 持有期内有足够后续数据的触发次数
        total = 0
        last_date = None
        for i in range(1, n):
            row = df.iloc[i].to_dict()
            row["prev_dif"] = df.iloc[i-1].get("dif", 0)
            row["prev_dea"] = df.iloc[i-1].get("dea", 0)
            row["prev_k"]   = df.iloc[i-1].get("kdj_k", 50)
            row["prev_d"]   = df.iloc[i-1].get("kdj_d", 50)
            row["close_chg"] = (closes[i] - closes[i-1]) / closes[i-1] * 100 if closes[i-1] else 0
            try:
                triggered = sig_fn(row)
            except Exception:
                triggered = False
            if not triggered:
                continue
            total += 1
            last_date = str(df.iloc[i]["date"])
            for d in forward_days:
                if i + d < n:
                    ret = (closes[i + d] - closes[i]) / closes[i] * 100
                    ret_sums[d] += ret
                    valid_counts[d] += 1
                    if ret > 0:
                        win_counts[d] += 1
        if total == 0:
            results[sig_name] = {"count": 0, "note": "历史未触发"}
            continue
        results[sig_name] = {
            "count": total,
            "last_date": last_date,
            "stats": {
                str(d): {
                    "win_rate":   round(win_counts[d] / valid_counts[d] * 100, 1) if valid_counts[d] else None,
                    "avg_return": round(ret_sums[d] / valid_counts[d], 2) if valid_counts[d] else None,
                    "valid":      valid_counts[d],
                }
                for d in forward_days
            }
        }

    return {"signals": results, "total_days": n, "stock_days": str(df["date"].iloc[0]) + " ~ " + str(df["date"].iloc[-1])}


def _compute_indicators(df):
    close = df["close"]
    volume = df.get("volume", pd.Series(dtype=float))
    for n in [5, 10, 20]: df[f"ma{n}"] = close.rolling(n).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(com=5, adjust=False).mean()
    loss = (-delta).clip(lower=0).ewm(com=5, adjust=False).mean()
    df["rsi6"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))
    low9 = df["low"].rolling(9).min()
    high9 = df["high"].rolling(9).max()
    rsv = (close - low9) / (high9 - low9 + 1e-9) * 100
    df["kdj_k"] = rsv.ewm(com=2, adjust=False).mean()
    df["kdj_d"] = df["kdj_k"].ewm(com=2, adjust=False).mean()
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    df["boll_lower"] = mid - 2 * std
    if not volume.empty:
        df["vol_ratio"] = volume / volume.rolling(5).mean().replace(0, 1e-9)
    return df
