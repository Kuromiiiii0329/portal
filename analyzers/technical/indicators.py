"""
portal/analyzers/technical/indicators.py
指标计算层 —— 一次性算出全部技术指标列，供各 section 子模块读取。

从原 TechnicalAnalyzer._compute_indicators / _normalize_volume_scale 逐字节搬迁，
改为模块级纯函数（不依赖实例状态）。
"""
from __future__ import annotations

import pandas as pd


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    volume = df.get("volume", pd.Series(dtype=float))

    # MA
    for n in [5, 10, 20, 30, 60, 120, 250]:
        df[f"ma{n}"] = close.rolling(n).mean()

    # MACD (12/26/9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd_bar"] = (df["dif"] - df["dea"]) * 2

    # RSI
    for n in [6, 12, 24]:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(n).mean()
        loss = (-delta).clip(lower=0).rolling(n).mean()
        rs = gain / loss.replace(0, 1e-9)
        df[f"rsi{n}"] = 100 - 100 / (1 + rs)

    # KDJ (9日)
    low_9  = df["low"].rolling(9).min()
    high_9 = df["high"].rolling(9).max()
    rsv = (close - low_9) / (high_9 - low_9 + 1e-9) * 100
    df["kdj_k"] = rsv.ewm(com=2, adjust=False).mean()
    df["kdj_d"] = df["kdj_k"].ewm(com=2, adjust=False).mean()
    df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]

    # Bollinger Bands (20日, 2σ)
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    df["boll_mid"]   = mid
    df["boll_upper"] = mid + 2 * std
    df["boll_lower"] = mid - 2 * std
    df["boll_width"] = (df["boll_upper"] - df["boll_lower"]) / mid * 100

    # 威廉指标 WR(14)：(最高-收盘)/(最高-最低)*(-100)，[-100,0]，
    # <-80 超卖、>-20 超买（注意方向与 RSI 相反）
    hh14 = df["high"].rolling(14).max()
    ll14 = df["low"].rolling(14).min()
    df["wr14"] = (hh14 - close) / (hh14 - ll14 + 1e-9) * -100

    # 成交量均线
    if not volume.empty:
        # ── 单位一致性归一（防御性）──────────────────────────
        # 历史 kline.csv 可能由多个数据源增量拼接而成，而各源成交量
        # 单位不一致（Tushare/Baostock 落"股"，Pytdx/Efinance/Akshare
        # 落"手"，1 手 = 100 股，差 100 倍）。若不归一，最新一行较历史
        # 出现约 100 倍量级跳变，会算出"量比=0.01x"这类明显错误的值。
        # 这里以历史中位数为基准，把偏离基准约 100 倍量级的行拉回同一量级，
        # 既修正当日异常，也能修复已被污染的历史缓存。
        volume = normalize_volume_scale(volume)
        df["volume"] = volume
        df["vol_ma5"]  = volume.rolling(5).mean()
        df["vol_ratio"] = volume / df["vol_ma5"].replace(0, 1e-9)

    return df


def normalize_volume_scale(volume: pd.Series) -> pd.Series:
    """把同一序列内因数据源混用产生的 100 倍量级跳变归一到统一量级。

    思路：取所有正值成交量的中位数作为基准量级；对每一行，若其与基准的
    比值接近 100 或 1/100（在 [30, 300] 或 [1/300, 1/30] 区间内），判定为
    单位错位（手 vs 股），乘/除 100 拉回基准量级。阈值取宽区间以容忍正常
    的放量/缩量波动（通常在 10 倍以内），只捕捉"手↔股"这种整两个数量级的跳变。
    """
    try:
        vol = pd.to_numeric(volume, errors="coerce")
        positive = vol[vol > 0]
        if len(positive) < 5:
            return volume  # 数据太少，无法稳健判定基准，原样返回
        base = float(positive.median())
        if base <= 0:
            return volume

        def _fix(v: float) -> float:
            if v is None or not (v > 0):
                return v
            ratio = v / base
            if 30 <= ratio <= 300:       # 该行比基准大约 100 倍 → 多乘了 100（股 vs 手）
                return v / 100.0
            if 1 / 300 <= ratio <= 1 / 30:  # 该行比基准小约 100 倍 → 少乘 100（手 vs 股）
                return v * 100.0
            return v

        fixed = vol.map(_fix)
        # 保留原 index，NaN（无法转数值的行）回填原值
        fixed = fixed.where(fixed.notna(), volume)
        return fixed
    except Exception:
        # 归一属防御性增强，任何异常都不应影响主流程，退回原始数据
        return volume
