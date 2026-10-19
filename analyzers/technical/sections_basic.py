"""
portal/analyzers/technical/sections_basic.py
纯量化 section 子模块 —— 均线/MACD/RSI/KDJ/布林带/量价/超买超卖。

特征：只读 compute_indicators 产出的指标列，无外部 IO、无 LLM，score/signal 为占位
（50/hold），真实打分由外层 llm_notes 回写。

从原 TechnicalAnalyzer 的对应 _analyze_* 方法逐字节搬迁，改为模块级纯函数。
"""
from __future__ import annotations

import datetime
import pandas as pd

from ..base import Section


# ── 通用近 N 日历史序列辅助 ────────────────────────────────────────────────────

def _hist_window(df: pd.DataFrame, cols: list[str], n: int = 10) -> list[dict]:
    """从 df 尾部取最近 n 行，提取 date + 指定列，返回 list[dict]（供 LLM 消费）。
    缺失/NaN 值保留为 None，不用 0 填充，防止误导 LLM。
    """
    window = df.tail(n)
    rows = []
    for _, r in window.iterrows():
        row = {"date": str(r.get("date", ""))[:10]}
        for c in cols:
            v = r.get(c)
            row[c] = round(float(v), 4) if (v is not None and not pd.isna(v)) else None
        rows.append(row)
    return rows


def _slope(vals: list[float | None]) -> float:
    """最小二乘斜率，None 值跳过。正=上升，负=下降。"""
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    n = len(pts)
    if n < 2:
        return 0.0
    x_mean = sum(p[0] for p in pts) / n
    y_mean = sum(p[1] for p in pts) / n
    num = sum((p[0] - x_mean) * (p[1] - y_mean) for p in pts)
    den = sum((p[0] - x_mean) ** 2 for p in pts)
    return num / den if den else 0.0


def _trend_str(s: float) -> str:
    return "↗ 上升" if s > 0 else "↘ 下降"


def _is_intraday(df: pd.DataFrame) -> bool:
    """判断 df 最后一行是否为当日盘中未收盘数据。
    判断依据：最后一行日期 == 今天，且当前时间在 A 股收盘时间（15:00）之前。
    """
    try:
        last_date = str(df.iloc[-1].get("date", ""))[:10]
        today = datetime.date.today().isoformat()
        if last_date != today:
            return False
        now = datetime.datetime.now().time()
        market_close = datetime.time(15, 5)  # 留 5 分钟缓冲
        return now < market_close
    except Exception:
        return False


def analyze_ma(df, stock_code) -> Section:
    last = df.iloc[-1]
    close = last["close"]
    ma5   = last.get("ma5")
    ma10  = last.get("ma10")
    ma20  = last.get("ma20")
    ma30  = last.get("ma30")
    ma60  = last.get("ma60")
    ma120 = last.get("ma120")
    ma250 = last.get("ma250")

    def _v(x): return x if (x is not None and not pd.isna(x)) else None

    ma5, ma10, ma20, ma30, ma60, ma120, ma250 = (
        _v(ma5), _v(ma10), _v(ma20), _v(ma30), _v(ma60), _v(ma120), _v(ma250)
    )

    bias5  = (close - ma5)  / ma5  * 100 if ma5  else 0
    bias20 = (close - ma20) / ma20 * 100 if ma20 else 0
    bias30 = (close - ma30) / ma30 * 100 if ma30 else 0

    # 均线多空排列（短中期）— 仅客观描述形态，不打分（打分交给 LLM）
    alignment = ""
    if all(v is not None for v in [ma5, ma10, ma20]):
        if ma5 > ma10 > ma20:
            alignment = "多头排列（MA5>MA10>MA20）"
        elif ma5 < ma10 < ma20:
            alignment = "空头排列（MA5<MA10<MA20）"
        else:
            alignment = "均线缠绕（震荡）"

    # 中长期均线多空排列（30/60/120）
    mid_alignment = ""
    if all(v is not None for v in [ma30, ma60, ma120]):
        if ma30 > ma60 > ma120:
            mid_alignment = "中长期多头（MA30>MA60>MA120）"
        elif ma30 < ma60 < ma120:
            mid_alignment = "中长期空头（MA30<MA60<MA120）"

    # 价格与年线/120日线/60日线的位置（客观事实）
    above_ma250 = ma250 and close > ma250
    above_ma120 = ma120 and close > ma120
    above_ma60  = ma60  and close > ma60

    if abs(bias5) > 8:
        bias_warn = f"  ⚠️ 短期乖离率偏大（MA5乖离 {bias5:+.1f}%）\n"
    else:
        bias_warn = ""

    # 关键均线支撑/压力描述
    pos_lines = []
    if ma30:
        rel30 = "上方（支撑）" if close > ma30 else "下方（压力）"
        pos_lines.append(f"MA30={ma30:.2f}（月线，{rel30}）")
    if ma60:
        rel60 = "上方（支撑）" if close > ma60 else "下方（压力）"
        pos_lines.append(f"MA60={ma60:.2f}（季线，{rel60}）")
    if ma120:
        rel120 = "上方（支撑）" if close > ma120 else "下方（压力）"
        pos_lines.append(f"MA120={ma120:.2f}（半年线，{rel120}）")
    if ma250:
        rel250 = "上方（牛市格局）" if close > ma250 else "下方（年线压制，偏弱）"
        pos_lines.append(f"MA250={ma250:.2f}（年线，{rel250}）")

    # 组装内容
    ma_line = (
        f"- MA5={ma5:.2f}  MA10={ma10:.2f}  MA20={ma20:.2f}"
        if ma5 and ma10 and ma20 else "- 均线数据不足"
    )
    if ma30:  ma_line += f"  MA30={ma30:.2f}"
    if ma60:  ma_line += f"  MA60={ma60:.2f}"
    if ma120: ma_line += f"  MA120={ma120:.2f}"
    if ma250: ma_line += f"  MA250={ma250:.2f}"

    content = (
        f"**{alignment}**"
        + (f"  {mid_alignment}" if mid_alignment else "") + "\n"
        f"- 当前价: {close:.2f}\n"
        f"{ma_line}\n"
        f"- 乖离率 MA5:{bias5:+.1f}%  MA20:{bias20:+.1f}%"
        + (f"  MA30:{bias30:+.1f}%" if ma30 else "") + "\n"
        + bias_warn
        + ("\n".join(f"- {l}" for l in pos_lines) + "\n" if pos_lines else "")
    )
    signal = "hold"
    return Section(key="ma_system", title="均线系统", content=content,
                   data={"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma30": ma30,
                         "ma60": ma60, "ma120": ma120, "ma250": ma250,
                         "close": close, "bias5": bias5, "bias20": bias20, "bias30": bias30,
                         "above_ma250": above_ma250, "above_ma120": above_ma120,
                         "above_ma60": above_ma60},
                   score=50, signal=signal)


def analyze_macd(df) -> Section:
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    dif, dea, bar = last.get("dif", 0), last.get("dea", 0), last.get("macd_bar", 0)
    prev_bar = prev.get("macd_bar", 0)

    # 金叉/死叉检测（客观事件，不打分）
    golden = prev.get("dif", 0) < prev.get("dea", 0) and dif > dea
    death  = prev.get("dif", 0) > prev.get("dea", 0) and dif < dea

    cross    = "🔴 死叉" if death else ("🟢 金叉" if golden else "")
    zero_pos = "零轴上方（多头区域）" if dif > 0 else "零轴下方（空头区域）"
    bar_trend = "↑ 柱线扩大" if bar > prev_bar else "↓ 柱线收缩"

    # ── 近 10 日趋势分析 ─────────────────────────────────────────────────
    window = df.tail(10)
    dates  = [str(r.get("date", ""))[:10] for _, r in window.iterrows()]
    difs   = [round(float(r.get("dif", 0) or 0), 4) for _, r in window.iterrows()]
    deas   = [round(float(r.get("dea", 0) or 0), 4) for _, r in window.iterrows()]
    bars   = [round(float(r.get("macd_bar", 0) or 0), 4) for _, r in window.iterrows()]

    # DIF 斜率方向（线性趋势）
    n = len(difs)
    if n >= 3:
        slope_num = sum((i - (n-1)/2) * (difs[i] - sum(difs)/n) for i in range(n))
        dif_slope = slope_num  # 正=上升趋势, 负=下降趋势
    else:
        dif_slope = difs[-1] - difs[0] if len(difs) >= 2 else 0

    dif_trend = "↗ 上升" if dif_slope > 0 else "↘ 下降"

    # 柱线连续方向（最近 3 根）
    bar3 = bars[-3:] if len(bars) >= 3 else bars
    bars_expanding = all(bar3[i] > bar3[i-1] for i in range(1, len(bar3))) if len(bar3) >= 2 else None
    bars_shrinking = all(bar3[i] < bar3[i-1] for i in range(1, len(bar3))) if len(bar3) >= 2 else None
    bar3_desc = "连续扩大" if bars_expanding else ("连续收缩" if bars_shrinking else "震荡")

    # 零轴穿越次数（近 10 日 DIF 穿越 0 轴的次数，>2 次说明震荡剧烈）
    zero_crosses = sum(
        1 for i in range(1, len(difs)) if difs[i] * difs[i-1] < 0
    )

    # 近 10 日表格（给 LLM 看的结构化数据，每行：日期 DIF DEA bar）
    hist_rows = [
        {"date": dates[i], "dif": difs[i], "dea": deas[i], "bar": bars[i]}
        for i in range(len(dates))
    ]

    # 组装文字描述
    trend_line = (
        f"- 近10日DIF趋势：{dif_trend}（斜率{dif_slope:+.5f}），"
        f"柱线近3日：{bar3_desc}，零轴穿越{zero_crosses}次\n"
    )

    content = (
        f"**DIF={dif:.4f}  DEA={dea:.4f}  MACD柱={bar:.4f}**\n"
        f"- 位置：{zero_pos} {cross}\n"
        f"- 柱线趋势：{bar_trend}\n"
        + trend_line
    )
    return Section(key="macd", title="MACD指标", content=content,
                   data={"dif": dif, "dea": dea, "bar": bar,
                         "golden": golden, "death": death,
                         "dif_trend": dif_trend, "dif_slope": round(dif_slope, 6),
                         "bar3_desc": bar3_desc, "zero_crosses": zero_crosses,
                         "hist10": hist_rows},
                   score=50, signal="hold")


def analyze_rsi(df) -> Section:
    last = df.iloc[-1]
    r6, r12, r24 = (last.get(f"rsi{n}", 50) for n in [6, 12, 24])

    # 客观区间描述（不打分，打分交给 LLM）
    if r6 > 80:
        status = "严重超买区间（>80）"
    elif r6 > 70:
        status = "超买区间（>70）"
    elif r6 < 20:
        status = "严重超卖区间（<20）"
    elif r6 < 30:
        status = "超卖区间（<30）"
    else:
        status = "中性区间"

    # 近 10 日趋势
    hist10 = _hist_window(df, ["rsi6", "rsi12", "rsi24"])
    r6_vals = [r["rsi6"] for r in hist10]
    r6_slope = _slope(r6_vals)
    r6_trend = _trend_str(r6_slope)

    content = (
        f"**RSI(6)={r6:.1f}  RSI(12)={r12:.1f}  RSI(24)={r24:.1f}**\n"
        f"- 区间：{status}\n"
        f"- 近10日RSI(6)趋势：{r6_trend}（斜率{r6_slope:+.3f}）\n"
        f"- 参考：超买>70，超卖<30\n"
    )
    return Section(key="rsi", title="RSI超买超卖", content=content,
                   data={"rsi6": r6, "rsi12": r12, "rsi24": r24,
                         "rsi6_trend": r6_trend, "rsi6_slope": round(r6_slope, 4),
                         "hist10": hist10},
                   score=50, signal="hold")


def analyze_kdj(df) -> Section:
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    k, d, j = last.get("kdj_k", 50), last.get("kdj_d", 50), last.get("kdj_j", 50)
    pk, pd_ = prev.get("kdj_k", 50), prev.get("kdj_d", 50)

    golden = pk < pd_ and k > d
    death  = pk > pd_ and k < d

    cross      = "🟢 金叉" if golden else ("🔴 死叉" if death else "")
    overbought = " ⚠️ J值超买(>90)" if j > 90 else (" ✅ J值超卖(<10)" if j < 10 else "")

    # 近 10 日趋势
    hist10  = _hist_window(df, ["kdj_k", "kdj_d", "kdj_j"])
    j_vals  = [r["kdj_j"] for r in hist10]
    j_slope = _slope(j_vals)
    j_trend = _trend_str(j_slope)

    content = (
        f"**K={k:.1f}  D={d:.1f}  J={j:.1f}** {cross}{overbought}\n"
        f"- K>D（多头信号）{' ✓' if k > d else ' ✗'}\n"
        f"- 近10日J值趋势：{j_trend}（斜率{j_slope:+.3f}）\n"
    )
    return Section(key="kdj", title="KDJ随机指标", content=content,
                   data={"k": k, "d": d, "j": j, "golden": golden, "death": death,
                         "j_trend": j_trend, "j_slope": round(j_slope, 4),
                         "hist10": hist10},
                   score=50, signal="hold")


def analyze_bollinger(df) -> Section:
    last = df.iloc[-1]
    close = last["close"]
    upper = last.get("boll_upper")
    mid   = last.get("boll_mid")
    lower = last.get("boll_lower")
    width = last.get("boll_width", 0)

    if upper is None or pd.isna(upper):
        return None  # 无真实布林带数据，不显示

    pos_pct = (close - lower) / (upper - lower + 1e-9) * 100

    # 客观位置描述
    if close > upper:
        status = "价格突破上轨"
    elif close < lower:
        status = "价格跌破下轨"
    elif pos_pct > 75:
        status = "价格在布林带上半区"
    elif pos_pct < 25:
        status = "价格在布林带下半区"
    else:
        status = "价格在布林带中部"

    # 近 10 日趋势：带宽收敛/扩张是关键信号
    hist10      = _hist_window(df, ["boll_upper", "boll_mid", "boll_lower", "boll_width"])
    width_vals  = [r["boll_width"] for r in hist10]
    width_slope = _slope(width_vals)
    width_trend = "↗ 扩张（波动加剧）" if width_slope > 0 else "↘ 收敛（蓄势待发）"

    # 补充每日价格在布林带内的位置百分比
    close_vals = list(df["close"].tail(10))
    for i, r in enumerate(hist10):
        u, l = r["boll_upper"], r["boll_lower"]
        if u is not None and l is not None and i < len(close_vals):
            r["pos_pct"] = round((close_vals[i] - l) / (u - l + 1e-9) * 100, 1)
        else:
            r["pos_pct"] = None

    content = (
        f"**上轨={upper:.2f}  中轨={mid:.2f}  下轨={lower:.2f}**\n"
        f"- 带宽：{width:.1f}%  价格位置：{pos_pct:.0f}%\n"
        f"- 状态：{status}\n"
        f"- 近10日带宽趋势：{width_trend}（斜率{width_slope:+.3f}）\n"
    )
    return Section(key="bollinger", title="布林带", content=content,
                   data={"upper": upper, "mid": mid, "lower": lower,
                         "width": width, "pos_pct": pos_pct,
                         "width_trend": width_trend, "width_slope": round(width_slope, 4),
                         "hist10": hist10},
                   score=50, signal="hold")


def analyze_volume(df) -> Section:
    intraday = _is_intraday(df)

    if intraday and len(df) >= 2:
        # 盘中：价格用最新行（含当日涨跌），量比用前一个完整交易日避免失真
        last      = df.iloc[-1]
        prev      = df.iloc[-2]
        prev_prev = df.iloc[-3] if len(df) >= 3 else prev
        vol_ratio    = prev.get("vol_ratio", 1.0)   # 前一完整日量比
        close_change = (last["close"] - prev["close"]) / prev["close"] * 100
        intraday_note = f"⚠️ 今日盘中数据（成交量不完整），量比取前交易日({str(prev.get('date',''))[:10]})\n"
    else:
        last      = df.iloc[-1]
        prev      = df.iloc[-2] if len(df) >= 2 else last
        vol_ratio    = last.get("vol_ratio", 1.0)
        close_change = (last["close"] - prev["close"]) / prev["close"] * 100
        intraday_note = ""

    # 客观量价关系描述
    if vol_ratio > 2 and close_change > 0:
        status = "放量上涨"
    elif vol_ratio > 2 and close_change < 0:
        status = "放量下跌"
    elif vol_ratio < 0.5 and close_change > 0:
        status = "缩量上涨"
    elif vol_ratio < 0.5 and close_change < 0:
        status = "缩量回调"
    else:
        status = "量能正常"

    # 近 10 日趋势：排除当日盘中行，取最近 10 个完整交易日
    hist_df = df.iloc[:-1] if intraday else df
    hist10     = _hist_window(hist_df, ["vol_ratio"])
    vol_r_vals = [r["vol_ratio"] for r in hist10]
    vol_slope  = _slope(vol_r_vals)
    vol_trend  = "↗ 量能放大" if vol_slope > 0 else "↘ 量能萎缩"

    # 补充每日涨跌幅到 hist10
    closes = list(hist_df["close"].tail(11))
    for i, r in enumerate(hist10):
        if i + 1 < len(closes) and closes[i] and closes[i] > 0:
            r["chg_pct"] = round((closes[i + 1] - closes[i]) / closes[i] * 100, 2)
        else:
            r["chg_pct"] = None

    content = (
        intraday_note
        + f"**量比={vol_ratio:.2f}x  当日涨跌={close_change:+.2f}%**\n"
        f"- 状态：{status}\n"
        f"- 近10日量能趋势：{vol_trend}（斜率{vol_slope:+.3f}）\n"
        f"- 量比>2为放量，<0.5为缩量\n"
    )
    return Section(key="volume", title="量价关系", content=content,
                   data={"vol_ratio": vol_ratio, "close_change": close_change,
                         "vol_trend": vol_trend, "vol_slope": round(vol_slope, 4),
                         "intraday": intraday,
                         "hist10": hist10},
                   score=50, signal="hold")


def analyze_overbought(df) -> Section:
    """综合 RSI/KDJ/威廉WR/布林位置，给出统一的超买超卖结论。"""
    last = df.iloc[-1]
    r6  = float(last.get("rsi6", 50) or 50)
    j   = float(last.get("kdj_j", 50) or 50)
    wr  = float(last.get("wr14", -50) or -50)
    upper = last.get("boll_upper"); lower = last.get("boll_lower"); close = last["close"]
    boll_pos = None
    if upper is not None and not pd.isna(upper):
        boll_pos = (close - lower) / (upper - lower + 1e-9) * 100

    # 各指标投票：+1 超买、-1 超卖、0 中性
    ob_votes, os_votes, lines = 0, 0, []
    if r6 > 70:    ob_votes += 1; lines.append(f"RSI(6)={r6:.0f} 超买")
    elif r6 < 30:  os_votes += 1; lines.append(f"RSI(6)={r6:.0f} 超卖")
    else:          lines.append(f"RSI(6)={r6:.0f} 中性")
    if j > 90:     ob_votes += 1; lines.append(f"KDJ_J={j:.0f} 超买")
    elif j < 10:   os_votes += 1; lines.append(f"KDJ_J={j:.0f} 超卖")
    else:          lines.append(f"KDJ_J={j:.0f} 中性")
    if wr > -20:   ob_votes += 1; lines.append(f"WR(14)={wr:.0f} 超买")
    elif wr < -80: os_votes += 1; lines.append(f"WR(14)={wr:.0f} 超卖")
    else:          lines.append(f"WR(14)={wr:.0f} 中性")
    if boll_pos is not None:
        if boll_pos > 90:    ob_votes += 1; lines.append(f"布林位置={boll_pos:.0f}% 触上轨")
        elif boll_pos < 10:  os_votes += 1; lines.append(f"布林位置={boll_pos:.0f}% 触下轨")
        else:                lines.append(f"布林位置={boll_pos:.0f}% 中部")

    if ob_votes >= 3:
        status = f"🔴 强烈超买（{ob_votes}/4 指标）"
    elif os_votes >= 3:
        status = f"🟢 强烈超卖（{os_votes}/4 指标）"
    elif ob_votes == 2:
        status = f"🟠 偏超买（{ob_votes}/4）"
    elif os_votes == 2:
        status = f"🔵 偏超卖（{os_votes}/4）"
    else:
        status = "⚪ 中性区域，无明显超买超卖"

    # 近 10 日趋势：综合4项指标的投票方向演变
    hist10     = _hist_window(df, ["rsi6", "kdj_j", "wr14"])
    r6_slope   = _slope([r["rsi6"] for r in hist10])
    j_slope    = _slope([r["kdj_j"] for r in hist10])
    # WR 越大（靠近 0）越超买，越小（靠近 -100）越超卖，斜率正=趋向超买
    wr_slope   = _slope([r["wr14"] for r in hist10])

    # 计算每日 boll_pos 并写入 hist10
    close_vals = list(df["close"].tail(10))
    boll_hist  = _hist_window(df, ["boll_upper", "boll_lower"])
    for i, r in enumerate(hist10):
        bu = boll_hist[i]["boll_upper"]; bl = boll_hist[i]["boll_lower"]
        if bu is not None and bl is not None and i < len(close_vals):
            r["boll_pos"] = round((close_vals[i] - bl) / (bu - bl + 1e-9) * 100, 1)
        else:
            r["boll_pos"] = None

    # 综合趋势：判断整体热度是升温还是降温
    # RSI↑、J↑、WR↑（趋向超买方向）→ 热度升温；反之降温
    heat_up = sum(1 for s in [r6_slope, j_slope, wr_slope] if s > 0)
    heat_desc = (
        "🔺 多项指标同步升温（热度增加）" if heat_up >= 2
        else "🔻 多项指标同步降温（热度降低）" if heat_up <= 1
        else "↔ 指标分化"
    )

    content = (
        "**" + status + "**\n"
        + "\n".join(f"- {x}" for x in lines)
        + f"\n- 近10日综合热度：{heat_desc}\n"
        + "- 判据：多指标共振时信号更可靠（≥3 项同向为强信号）\n"
    )
    return Section(key="overbought", title="超买超卖综合", content=content,
                   data={"rsi6": r6, "kdj_j": j, "wr14": wr, "boll_pos": boll_pos,
                         "ob_votes": ob_votes, "os_votes": os_votes,
                         "heat_desc": heat_desc,
                         "rsi6_slope": round(r6_slope, 4),
                         "j_slope": round(j_slope, 4),
                         "wr_slope": round(wr_slope, 4),
                         "hist10": hist10},
                   score=50, signal="hold")
