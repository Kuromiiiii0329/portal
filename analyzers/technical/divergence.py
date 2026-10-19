"""
portal/analyzers/technical/divergence.py
背离检测引擎（专业量化实现）—— 最独立的技术子领域。

特性：
- 摆动点：分形(Fractal, 右侧k根确认防未来函数) + ATR幅度清洗
- 类型：常规背离(反转) + 隐藏背离(延续)，MACD-DIF 与 RSI12 双指标
- 成熟度状态机：无迹象 → 迹象浮现(EARLY) → 进行中(FORMING) → 已确认(CONFIRMED) → 失效
- 强度评分：多因子加权 × 成熟度系数

从原 TechnicalAnalyzer 的 _analyze_divergence 及其 8 个辅助方法逐字节搬迁。
⚠️ 原实现依赖 self._df 取日期，重构后改为显式传 df 参数（analyze_divergence 的入参
   即为 df，一路透传给需要日期的 _classify_pair/_emerging/_build_divergence_section）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Section


def analyze_divergence(df) -> Section:
    n = len(df)
    if n < 30:
        return None  # 数据不足（少于30日），不显示（不出假情报）

    close = df["close"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    dif   = df["dif"].values.astype(float)   if "dif"   in df.columns else None
    rsi   = df["rsi12"].values.astype(float) if "rsi12" in df.columns else None
    macd_bar = df["macd_bar"].values.astype(float) if "macd_bar" in df.columns else None
    vr    = df["vol_ratio"].values.astype(float) if "vol_ratio" in df.columns else None
    ma20  = df["close"].rolling(20).mean().values

    # ATR14（波动率归一化基准）
    atr = _atr(high, low, close, 14)
    now = n - 1
    K = 3               # 分形半宽（右侧确认根数）
    MIN_GAP, MAX_GAP = 5, 55
    RECENT_WIN = 90     # 只看近90交易日内形成的背离（更早的时效性差，且避免长历史刷屏）

    # 摆动点（价格高/低点，已右侧确认）+ ATR 幅度清洗
    pl = _fractal_idx(low,  K, kind="low")
    ph = _fractal_idx(high, K, kind="high")
    pl = _clean_pivots(pl, low,  atr, MIN_GAP)
    ph = _clean_pivots(ph, high, atr, MIN_GAP)

    signals = []

    # ── 已确认背离（遍历相邻同类摆动点，MACD + RSI 双指标）──
    for ind, ind_name, k_ind in [(dif, "MACD", "dif"), (rsi, "RSI", "rsi")]:
        if ind is None:
            continue
        # 底背离 / 隐藏底背离
        for a, b in zip(pl, pl[1:]):
            if b < now - RECENT_WIN:          # 第二个摆动点须在近期窗口内
                continue
            if not (MIN_GAP <= b - a <= MAX_GAP):
                continue
            sig = _classify_pair(
                df, kind="bottom", i1=a, i2=b, price=low, ind=ind, ind_name=ind_name,
                atr=atr, rsi=rsi, ma20=ma20, vr=vr, close=close)
            if sig:
                signals.append(sig)
        # 顶背离 / 隐藏顶背离
        for a, b in zip(ph, ph[1:]):
            if b < now - RECENT_WIN:
                continue
            if not (MIN_GAP <= b - a <= MAX_GAP):
                continue
            sig = _classify_pair(
                df, kind="top", i1=a, i2=b, price=high, ind=ind, ind_name=ind_name,
                atr=atr, rsi=rsi, ma20=ma20, vr=vr, close=close)
            if sig:
                signals.append(sig)

    # ── 进行时 / 早期背离（临时锚点 + 状态机，MACD 主判）──
    emerging = _emerging(
        df, close, low, high, dif, rsi, macd_bar, atr, ma20, pl, ph, now)
    signals.extend(emerging)

    # 去重：同类型同区间保留强度最高
    signals = _dedup(signals)

    return _build_divergence_section(df, signals)


# ── 背离引擎辅助方法 ──────────────────────────────────────
def _atr(high, low, close, n=14):
    tr = np.zeros(len(close))
    tr[0] = high[0] - low[0]
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i-1]),
                    abs(low[i]  - close[i-1]))
    atr = np.full(len(close), np.nan)
    if len(close) >= n:
        atr[n-1] = tr[:n].mean()
        for i in range(n, len(close)):
            atr[i] = (atr[i-1] * (n-1) + tr[i]) / n
    # 前段用累计均值兜底，避免 NaN
    for i in range(len(close)):
        if np.isnan(atr[i]):
            atr[i] = tr[:i+1].mean() if i > 0 else tr[0]
    return atr


def _fractal_idx(arr, k, kind="low"):
    """分形摆动点：严格不等号 + 右侧 k 根确认（防未来函数）。返回升序下标。"""
    out = []
    m = len(arr)
    for i in range(k, m - k):
        left  = arr[i-k:i]
        right = arr[i+1:i+k+1]
        if kind == "low"  and arr[i] < left.min() and arr[i] < right.min():
            out.append(i)
        elif kind == "high" and arr[i] > left.max() and arr[i] > right.max():
            out.append(i)
    return out


def _clean_pivots(pivots, prices, atr, min_gap, atr_mult=1.2):
    """清洗摆动点：间隔<min_gap 合并保留更极端者；幅度<atr_mult*ATR 视为同一波剔除。"""
    kept = []
    for idx in pivots:
        if not kept:
            kept.append(idx); continue
        prev = kept[-1]
        if idx - prev < min_gap:
            if abs(prices[idx]) and abs(prices[idx] - prices[prev]) > 0:
                # 保留更极端的（低点取更低、高点由调用侧保证方向一致，这里按价差方向）
                kept[-1] = idx if abs(prices[idx]) else prev
            continue
        if abs(prices[idx] - prices[prev]) < atr_mult * (atr[idx] or 1e-9):
            continue
        kept.append(idx)
    return kept


def _classify_pair(df, kind, i1, i2, price, ind, ind_name, atr, rsi, ma20, vr, close):
    """判定一对摆动点是否构成背离，返回信号 dict 或 None。"""
    p1, p2 = price[i1], price[i2]
    v1, v2 = ind[i1], ind[i2]

    dtype = None       # regular_bull/bear, hidden_bull/bear
    if kind == "bottom":
        if p2 < p1 and v2 > v1:
            dtype = "regular_bull"       # 价格新低+指标抬高 → 常规底背离(反转看涨)
        elif p2 > p1 and v2 < v1:
            dtype = "hidden_bull"        # 价格抬高+指标新低 → 隐藏底背离(延续看涨)
    else:  # top
        if p2 > p1 and v2 < v1:
            dtype = "regular_bear"       # 价格新高+指标走弱 → 常规顶背离(反转看跌)
        elif p2 < p1 and v2 > v1:
            dtype = "hidden_bear"        # 价格走低+指标抬高 → 隐藏顶背离(延续看跌)
    if dtype is None:
        return None

    # 过滤：常规背离要区域过滤（RSI），隐藏背离要趋势过滤
    if dtype.startswith("regular") and ind_name == "RSI" and rsi is not None:
        if kind == "bottom" and not (rsi[i1] < 35 or rsi[i2] < 35):
            return None
        if kind == "top" and not (rsi[i1] > 65 or rsi[i2] > 65):
            return None
    if dtype.startswith("hidden") and ma20 is not None and not pd.isna(ma20[i2]):
        if "bull" in dtype and not (close[i2] > ma20[i2]):
            return None
        if "bear" in dtype and not (close[i2] < ma20[i2]):
            return None

    # 成熟度：i2 已是确认摆动点（能进这函数就已右侧确认）→ CONFIRMED
    state = "CONFIRMED"
    score = _strength(dtype, kind, i1, i2, price, ind, ind_name, atr, rsi, vr)
    score = int(round(score * 1.0))     # confirm_mult=1.0

    # 携带日期和具体价格/指标值，供渲染时引用
    d1 = str(df.iloc[i1]["date"]) if df is not None and i1 < len(df) else str(i1)
    d2 = str(df.iloc[i2]["date"]) if df is not None and i2 < len(df) else str(i2)

    return {"type": dtype, "indicator": ind_name, "maturity": state,
            "score": score, "i1": i1, "i2": i2,
            "date1": d1, "date2": d2,
            "price1": round(float(p1), 3), "price2": round(float(p2), 3),
            "ind1": round(float(v1), 4),   "ind2": round(float(v2), 4)}


def _strength(dtype, kind, i1, i2, price, ind, ind_name, atr, rsi, vr):
    """八因子加权强度评分 0-100（简化版，保留核心因子）。"""
    a = atr[i2] or 1e-9
    # 价格位移
    price_f = min(abs(price[i2] - price[i1]) / a / 2.5, 1.0)
    # 指标反向差
    ind_diff = (ind[i2] - ind[i1]) if "bull" in dtype else (ind[i1] - ind[i2])
    k_ind = 15.0 if ind_name == "RSI" else max(a, abs(ind[i2]) + 1e-9)
    ind_f = min(max(ind_diff, 0) / k_ind, 1.0)
    align_f = min(price_f, ind_f)
    # 时间跨度梯形
    dt = i2 - i1
    if dt < 5 or dt > 80: time_f = 0.0
    elif 8 <= dt <= 40:   time_f = 1.0
    elif dt < 8:          time_f = (dt - 5) / 3.0
    else:                 time_f = (80 - dt) / 40.0
    # 极值区（仅常规背离，用 RSI）
    zone_f = 0.0
    if dtype.startswith("regular") and rsi is not None:
        zone_f = min(max((rsi[i2]-70)/20, 0), 1) if kind == "top" else min(max((30-rsi[i2])/20, 0), 1)
    # 量能（P2 缩量为正）
    vol_f = 0.0
    if vr is not None and vr[i1] > 0:
        vol_f = min(max((vr[i1] - vr[i2]) / vr[i1], 0), 1)
    raw = 100 * (0.22*zone_f + 0.20*ind_f + 0.18*align_f +
                 0.15*price_f + 0.13*time_f + 0.12*vol_f)
    if raw < 5:
        raw = 30 + 20 * align_f     # 无极值区/量能时给个保底（隐藏背离常见）
    return max(min(raw, 100), 0)


def _emerging(df, close, low, high, dif, rsi, macd_bar, atr, ma20, pl, ph, now):
    """临时锚点 + 状态机：检测 EARLY(迹象浮现) / FORMING(进行中) 背离。"""
    out = []
    if dif is None:
        return out
    recent_cut = now - 55

    for kind in ("bottom", "top"):
        pivots = pl if kind == "bottom" else ph
        confirmed = [i for i in pivots if i <= now - 3 and i >= recent_cut]
        if not confirmed:
            continue
        p1_idx = confirmed[-1]           # 最近一个已确认摆动点
        seg = slice(p1_idx + 1, now + 1)
        if p1_idx + 1 > now:
            continue

        if kind == "bottom":
            live_idx = p1_idx + 1 + int(np.argmin(low[seg]))
            P1, P2 = low[p1_idx], low[live_idx]
        else:
            live_idx = p1_idx + 1 + int(np.argmax(high[seg]))
            P1, P2 = high[p1_idx], high[live_idx]
        I1, I2 = dif[p1_idx], dif[live_idx]
        bb = max(live_idx - p1_idx, 1)
        a = atr[now] or 1e-9

        # 价格-指标滚动相关（脱钩预警）
        W = 14
        rho = np.nan
        if now >= W:
            ret = np.diff(close[now-W:now+1]) / (close[now-W:now] + 1e-9)
            dind = np.diff(dif[now-W:now+1])
            if ret.std() > 0 and dind.std() > 0:
                rho = float(np.corrcoef(ret, dind)[0, 1])

        state = None
        reason = ""
        if kind == "bottom":
            # FORMING：价格已破前低 + DIF 抬高
            if P2 < P1 and I2 > I1:
                state, reason = "FORMING", "价格已创新低但 MACD 抬高，底背离进行中"
            else:
                near = (P1 <= P2 <= P1 * 1.015) or (P2 - P1 <= 0.5 * a)
                rsi_strong = rsi is not None and (rsi[live_idx] - rsi[p1_idx] >= 3)
                if near and (I2 > I1 or rsi_strong):
                    state, reason = "EARLY", "价格逼近前低但指标已转强，底背离迹象浮现"
                elif not np.isnan(rho) and rho < -0.3 and close[now] < ma20[now]:
                    state, reason = "EARLY", "价格与 MACD 走势脱钩（负相关），潜在底背离酝酿"
        else:  # top
            if P2 > P1 and I2 < I1:
                state, reason = "FORMING", "价格已创新高但 MACD 走弱，顶背离进行中"
            else:
                near = (P1 * 0.985 <= P2 <= P1) or (P1 - P2 <= 0.5 * a)
                # 动能衰减：macd_bar 连续收缩
                decay = False
                if macd_bar is not None and now >= 3:
                    h = macd_bar[now-2:now+1]
                    decay = h[0] > h[1] > h[2] and h[0] > 0
                if near and (I2 < I1 or decay):
                    state, reason = "EARLY", "价格逼近前高但动能衰减，顶背离迹象浮现"
                elif decay and close[now] > ma20[now]:
                    state, reason = "EARLY", "MACD 柱连续收缩、量价动能脱钩，顶背离预警"

        if state:
            dtype = ("regular_bull" if kind == "bottom" else "regular_bear")
            base = _strength(dtype, kind, p1_idx, live_idx, low if kind=="bottom" else high,
                             dif, "MACD", atr, rsi, None)
            mult = 0.6 if state == "EARLY" else 0.85
            score = int(round(base * mult))
            # 早期信号给分不低于状态下限，便于展示
            score = max(score, 30 if state == "EARLY" else 42)

            d1 = str(df.iloc[p1_idx]["date"])  if df is not None and p1_idx < len(df) else str(p1_idx)
            d2 = str(df.iloc[live_idx]["date"]) if df is not None and live_idx < len(df) else str(live_idx)
            price_arr = low if kind == "bottom" else high
            out.append({"type": dtype + "_emerging", "indicator": "MACD",
                        "maturity": state, "score": score,
                        "i1": p1_idx, "i2": live_idx, "reason": reason,
                        "date1": d1, "date2": d2,
                        "price1": round(float(price_arr[p1_idx]), 3),
                        "price2": round(float(price_arr[live_idx]), 3),
                        "ind1": round(float(dif[p1_idx]), 4),
                        "ind2": round(float(dif[live_idx]), 4)})
    return out


def _dedup(signals):
    """
    同「基础类型 + 指标 + 是否早期」只保留强度最高的 1 条，避免长历史多对摆动点刷屏。
    例：多个 RSI 常规顶背离只留最强那条。
    """
    best = {}
    for s in signals:
        base_type = s["type"].replace("_emerging", "")
        is_emerging = "_emerging" in s["type"]
        key = (base_type, s.get("indicator", ""), is_emerging)
        if key not in best or s["score"] > best[key]["score"]:
            best[key] = s
    return sorted(best.values(), key=lambda x: -x["score"])


def _build_divergence_section(df, signals) -> Section:
    """把背离信号列表渲染成详细的 Section，包含具体 K 线引用、确认条件、操作建议。"""
    TYPE_LABEL = {
        "regular_bull": ("🟢 常规底背离", "反转看涨", "buy"),
        "regular_bear": ("🔴 常规顶背离", "反转看跌", "sell"),
        "hidden_bull":  ("🔵 隐藏底背离", "趋势延续（多）", "buy"),
        "hidden_bear":  ("🟠 隐藏顶背离", "趋势延续（空）", "sell"),
    }
    MAT_LABEL = {
        "EARLY":     ("迹象浮现",  "⚠️ 预警级"),
        "FORMING":   ("进行中",    "🔔 观察级"),
        "CONFIRMED": ("已确认",    "✅ 参考级"),
    }

    if not signals:
        return Section(key="divergence", title="背离检测",
                       content=(
                           "近期未检测到顶/底背离信号（含早期/进行中）。\n"
                           "- 检测方法：分形摆动点 + MACD(DIF)/RSI 双指标 + 成熟度状态机\n"
                           "- 背离生成需要近期存在可比较的相邻摆动高/低点，若行情单边运行则无法产生\n"
                       ),
                       data={"signals": []}, score=50, signal="hold")

    # 取当前价格辅助计算关键点位
    cur_price = float(df.iloc[-1]["close"]) if df is not None else 0.0
    ma5  = float(df.iloc[-1].get("ma5")  or 0) if df is not None else 0.0
    ma10 = float(df.iloc[-1].get("ma10") or 0) if df is not None else 0.0
    ma20 = float(df.iloc[-1].get("ma20") or 0) if df is not None else 0.0

    lines = []
    for s in signals[:5]:
        base_type = s["type"].replace("_emerging", "")
        is_emerging = "_emerging" in s["type"]
        label, meaning, _ = TYPE_LABEL.get(base_type, ("背离", "", "hold"))
        mat_short, mat_badge = MAT_LABEL.get(s["maturity"], (s["maturity"], ""))
        ind_name = s.get("indicator", "MACD")
        grade = "强" if s["score"] >= 70 else ("中" if s["score"] >= 45 else "弱")

        d1   = s.get("date1", "")
        d2   = s.get("date2", "")
        p1   = s.get("price1", 0.0)
        p2   = s.get("price2", 0.0)
        v1   = s.get("ind1",   0.0)
        v2   = s.get("ind2",   0.0)

        # 标题行
        lines.append(f"### {label}·{mat_short}  {mat_badge}（{ind_name}，强度 {s['score']}/{grade}）")

        # 具体 K 线引用
        if "bull" in base_type:
            lines.append(f"**K线区间**：{d1} 低点 **{p1}** → {d2} 低点 **{p2}**")
            if p2 < p1:
                lines.append(f"  价格：{d1}低({p1}) > {d2}低({p2})  ↓ 价格创新低 {abs(p2-p1)/p1*100:.1f}%")
            else:
                lines.append(f"  价格：{d1}低({p1}) < {d2}低({p2})  ↑ 价格回升 {(p2-p1)/p1*100:.1f}%")
            lines.append(f"  {ind_name}：第一低点 {v1:.4f} → 第二低点 {v2:.4f}  "
                         + ("↑ 指标抬升（背离确立）" if v2 > v1 else "↓ 指标同降（尚未背离）"))
        else:  # bear
            lines.append(f"**K线区间**：{d1} 高点 **{p1}** → {d2} 高点 **{p2}**")
            if p2 > p1:
                lines.append(f"  价格：{d1}高({p1}) < {d2}高({p2})  ↑ 价格创新高 {(p2-p1)/p1*100:.1f}%")
            else:
                lines.append(f"  价格：{d1}高({p1}) > {d2}高({p2})  ↓ 价格走低")
            lines.append(f"  {ind_name}：第一高点 {v1:.4f} → 第二高点 {v2:.4f}  "
                         + ("↓ 指标走弱（背离确立）" if v2 < v1 else "↑ 指标同升（尚未背离）"))

        # 进行时/早期信号的动态 reason
        if is_emerging and s.get("reason"):
            lines.append(f"**当前状态**：{s['reason']}")

        # 背离含义解释
        if base_type == "regular_bear":
            lines.append(f"**含义**：常规顶背离 = 价格创新高但动能衰减，是上涨趋势潜在反转的预警信号。")
        elif base_type == "regular_bull":
            lines.append(f"**含义**：常规底背离 = 价格创新低但动能反弹，是下跌趋势潜在反转的预警信号。")
        elif base_type == "hidden_bear":
            lines.append(f"**含义**：隐藏顶背离 = 价格走低但指标抬升，是下跌趋势延续的确认信号。")
        elif base_type == "hidden_bull":
            lines.append(f"**含义**：隐藏底背离 = 价格回升但指标仍弱，是上涨趋势延续的确认信号。")

        # 后市确认条件
        lines.append(f"**后市如何确认**：")
        if base_type == "regular_bear":
            confirm_price = round(p2 * 0.97, 2) if p2 else round(cur_price * 0.97, 2)
            ref_ma = round(min(ma5, ma10) if ma5 and ma10 else cur_price * 0.98, 2)
            lines.append(f"  1. 价格跌破 MA5（当前 {ma5:.2f}）或 MA10（{ma10:.2f}）**收盘确认**，顶背离大概率开始兑现")
            lines.append(f"  2. {ind_name} 出现死叉，且零轴向下运行，趋势转空确认")
            lines.append(f"  3. 成交量若再次放量上冲但价格无法超越 {d2} 高点 {p2}，形态固化")
            lines.append(f"  4. ⚠️ 若后续价格突破 {p2} 并站稳，顶背离信号失效")
        elif base_type == "regular_bull":
            lines.append(f"  1. 价格收复 MA5（当前 {ma5:.2f}）并站稳，底背离开始兑现")
            lines.append(f"  2. {ind_name} 出现金叉，柱线翻红并扩大，动能反转确认")
            lines.append(f"  3. 成交量在价格反弹时放大，价涨量增配合")
            lines.append(f"  4. ⚠️ 若后续价格跌破 {d2} 低点 {p2}，底背离信号失效")
        elif base_type == "hidden_bear":
            lines.append(f"  1. 当前价格若继续走低并跌破 MA20（{ma20:.2f}），延续趋势确认")
            lines.append(f"  2. {ind_name} 持续在零轴下方运行，空头趋势不变")
            lines.append(f"  ⚠️ 若价格强势反弹突破 {p1}，隐藏顶背离失效")
        elif base_type == "hidden_bull":
            lines.append(f"  1. 价格回调不破 {d2} 低点 {p2}，继续上行趋势确认")
            lines.append(f"  2. {ind_name} 金叉并在零轴上方，多头延续")
            lines.append(f"  ⚠️ 若价格跌破 {p2}，隐藏底背离失效")

        # 关键点位
        lines.append(f"**关键点位**：")
        if "bear" in base_type:
            stop_loss  = round(p2 * 1.02, 2) if p2 else round(cur_price * 1.03, 2)
            target1    = round(p2 * 0.95, 2) if p2 else round(cur_price * 0.95, 2)
            target2    = round(p2 * 0.90, 2) if p2 else round(cur_price * 0.90, 2)
            lines.append(f"  - 上方阻力：{p2}（{d2} 高点，即背离右侧顶点）")
            lines.append(f"  - 止损参考：{stop_loss}（若持空，跌破此价止损）")
            lines.append(f"  - 下方目标：初步 {target1}，若有效跌破 MA20 则看 {target2}")
            lines.append(f"  - 支撑参考：MA20={ma20:.2f}，若跌破将加速确认背离")
        else:
            stop_loss = round(p2 * 0.98, 2) if p2 else round(cur_price * 0.97, 2)
            target1   = round(p2 * 1.05, 2) if p2 else round(cur_price * 1.05, 2)
            target2   = round(p2 * 1.10, 2) if p2 else round(cur_price * 1.10, 2)
            lines.append(f"  - 下方支撑：{p2}（{d2} 低点，即背离右侧底点）")
            lines.append(f"  - 止损参考：{stop_loss}（若持多，跌破此价止损）")
            lines.append(f"  - 上方目标：初步 {target1}，若放量突破 MA20={ma20:.2f} 则看 {target2}")
            lines.append(f"  - 压力参考：MA10={ma10:.2f}，MA20={ma20:.2f}")

        # 操作建议
        lines.append(f"**操作建议**：")
        mat = s["maturity"]
        if base_type == "regular_bear":
            if mat == "CONFIRMED":
                lines.append(f"  - 已确认，建议轻仓者谨慎追多，持仓者逢反弹至压力位分批减仓")
                lines.append(f"  - 激进者可在价格跌破 MA5 且量能配合时尝试短空，止损设于 {p2}")
            elif mat == "FORMING":
                lines.append(f"  - 进行中，建议暂缓追涨，等待方向选择确认后再操作")
            else:
                lines.append(f"  - 迹象浮现（早期预警），建议观望为主，不追高，重点关注量能变化")
        elif base_type == "regular_bull":
            if mat == "CONFIRMED":
                lines.append(f"  - 已确认，可在价格收复 MA5（{ma5:.2f}）后分批轻仓试多")
                lines.append(f"  - 严格止损：跌破 {stop_loss} 立即止损，仓位不超过 3 成")
            elif mat == "FORMING":
                lines.append(f"  - 进行中，耐心等待价格企稳信号（MA5 收口/缩量回调），不抢底")
            else:
                lines.append(f"  - 迹象浮现（早期预警），观望为主，留意后续能否形成企稳形态")
        elif "bear" in base_type:
            lines.append(f"  - 隐藏顶背离为趋势延续信号，当前为空头市，反弹为卖出机会")
            lines.append(f"  - 建议持仓者坚持持仓，反弹至 {ma10:.2f}~{ma20:.2f} 区间减仓")
        else:
            lines.append(f"  - 隐藏底背离为趋势延续信号，当前为多头市，回调为买入机会")
            lines.append(f"  - 建议在价格回调至 MA5~MA10 区间（{ma5:.2f}~{ma10:.2f}）时分批买入")

        lines.append("")  # 空行分隔

    # 底部统一注释
    note = (
        "---\n"
        "**成熟度说明**：迹象浮现(预警) → 进行中(未确认) → 已确认(可参考)\n"
        "**背离类型**：常规背离=反转信号；隐藏背离=趋势延续信号\n"
        "**重要提示**：背离是预警信号而非充分条件，早期信号尤其须结合均线/成交量二次确认后操作\n"
    )

    # 综合评分：最强信号方向
    best = signals[0]
    base_type = best["type"].replace("_emerging", "")
    # 背离方向作为客观事实存入 data，打分交给 LLM
    best["direction"] = "bullish" if "bull" in base_type else "bearish"

    content = "\n".join(lines) + "\n" + note
    return Section(key="divergence", title="背离检测", content=content,
                   data={"signals": signals[:5], "best": best},
                   score=50, signal="hold")
