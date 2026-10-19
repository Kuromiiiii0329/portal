"""
portal/analyzers/technical/sections_llm.py
LLM 类 section 子模块 —— K线形态 / 波浪理论 / 缠论 / 技术综合精讲。

模式统一：构造指标快照 → 组 prompt → llm_call → extract_llm_score + strip_score_json。
从原 TechnicalAnalyzer 的对应 _analyze_*_llm / _analyze_llm_tech 逐字节搬迁，
score/signal 解析改用 _common 的共享函数（等价于原 self._extract_llm_score）。
"""
from __future__ import annotations

import logging

from ..base import Section
from .._common import extract_llm_score, strip_score_json

logger = logging.getLogger(__name__)


def analyze_pattern_llm(df, stock_name, llm_call) -> Section:
    tail20 = df.tail(20)
    recent_str = tail20[["date", "open", "high", "low", "close", "volume"]].to_string(index=False)
    last = df.iloc[-1]
    ma5  = round(float(last.get("ma5")  or 0), 2)
    ma10 = round(float(last.get("ma10") or 0), 2)
    ma20 = round(float(last.get("ma20") or 0), 2)
    ma60 = round(float(last.get("ma60") or 0), 2)
    dif  = round(float(last.get("dif")  or 0), 4)
    dea  = round(float(last.get("dea")  or 0), 4)
    rsi6 = round(float(last.get("rsi6") or 50), 1)
    vol_ratio = round(float(last.get("vol_ratio") or 1.0), 2)

    prompt = f"""你是专业技术分析师，请对 {stock_name} 最近20日K线形态进行深度分析。

【近20日K线数据】
{recent_str}

【当前指标快照】
- 均线：MA5={ma5}  MA10={ma10}  MA20={ma20}  MA60={ma60}
- MACD：DIF={dif}  DEA={dea}
- RSI(6)={rsi6}  量比={vol_ratio}x

【要求】请输出以下内容，每点都要结合实际数据（日期、价格）说明：

1. **识别的形态**
   - 形态名称（如：双底/头肩顶/三角收敛/旗形/楔形/W底等）
   - 具体引用：形态的关键价格点（注明日期和价格，如"X月X日高点XX，X月X日回落至XX形成左肩"）
   - 可靠性：高/中/低，并说明理由（成交量配合/对称性/时间周期）

2. **形态暗示走势**
   - 突破方向及目标价位（给出具体数字，如"若有效突破XX，目标看XX-XX"）
   - 突破确认条件（收盘价/成交量要求）

3. **关键支撑与阻力**
   - 近期支撑位：XX（对应X月X日低点/均线）
   - 近期阻力位：XX（对应X月X日高点/均线压力）

4. **操作建议**
   - 当前位置建议（持有/减仓/加仓/观望）
   - 入场条件：满足【具体价格或信号】时可考虑操作
   - 止损设置：XX价格以下止损（基于形态失效判断）

输出最后必须追加一行严格 JSON（基于你上面的分析给出评分与信号）：{{"score":整数0到100,"signal":"buy或watch或hold或sell"}}
（score 越高越偏多：>65偏多、35-65中性、<35偏空；signal 只能是 buy/watch/hold/sell）"""
    try:
        content = llm_call(prompt).strip()
        if not content:
            content = "⚠️ LLM 未返回内容（可能受 token 上限或模型行为影响），请重试。"
    except Exception as e:
        content = f"K线形态分析失败：{e}"
    # 从 LLM 输出末尾解析 score+signal（打分完全交给 LLM）
    score, signal = extract_llm_score(content)
    content = strip_score_json(content)
    return Section(key="pattern", title="K线形态", content=content, score=score, signal=signal)


def analyze_wave_llm(df, stock_name, llm_call) -> Section:
    tail = df.tail(60)
    closes = tail["close"].round(2).tolist()
    highs  = tail["high"].round(2).tolist()
    lows   = tail["low"].round(2).tolist()
    dates  = tail["date"].tolist()
    # 近期高低点
    max_idx = int(tail["high"].idxmax() - tail.index[0])
    min_idx = int(tail["low"].idxmin()  - tail.index[0])
    recent_high_date  = dates[max_idx] if max_idx < len(dates) else ""
    recent_high_price = highs[max_idx]  if max_idx < len(highs) else ""
    recent_low_date   = dates[min_idx]  if min_idx < len(dates) else ""
    recent_low_price  = lows[min_idx]   if min_idx < len(lows)  else ""
    last = df.iloc[-1]
    cur_price = round(float(last["close"]), 2)
    ma20  = round(float(last.get("ma20")  or 0), 2)
    ma60  = round(float(last.get("ma60")  or 0), 2)
    ma120 = round(float(last.get("ma120") or 0), 2)
    ma250 = round(float(last.get("ma250") or 0), 2)

    prompt = f"""你是波浪理论专家，请对 {stock_name} 进行专业波浪分析。

【近60日收盘价序列】（时间从早到晚）
{closes}

【区间高低点参考】
- 近60日高点：{recent_high_price}（{recent_high_date}）
- 近60日低点：{recent_low_price}（{recent_low_date}）
- 当前价格：{cur_price}  MA20={ma20}  MA60={ma60}  MA120={ma120}（半年线）  MA250={ma250}（年线）

【要求】请进行专业波浪分析，必须包含以下内容：

1. **波浪计数**
   - 判断大级别趋势背景（上升趋势/下降趋势/整理）
   - 当前处于第几浪（1/2/3/4/5浪，或A/B/C调整浪），给出理由
   - 关键：引用具体价格点说明浪的起止（如"从X价格启动第X浪，运行至X价格"）

2. **当前浪的状态**
   - 该浪是否已完成或进行中
   - 浪内部结构（如正处于5浪中的iii子浪）

3. **下一步预判**
   - 最可能的走势及目标价位区间（给出具体数字）
   - 次要情景（如主情景失效则转为）
   - 关键变盘时间窗口（如有周期规律则指出）

4. **关键点位**
   - 主要支撑：XX（若跌破则波浪计数修正）
   - 主要阻力：XX（突破后下一目标XX）
   - 浪的失效位：XX（跌破/突破此位则当前计数作废）

5. **操作建议**
   - 当前适合的操作策略（分批买/持有/减仓等）
   - 入场价位：XX附近，止损：XX，目标：XX

输出最后必须追加一行严格 JSON（基于你上面的分析给出评分与信号）：{{"score":整数0到100,"signal":"buy或watch或hold或sell"}}
（score 越高越偏多：>65偏多、35-65中性、<35偏空；signal 只能是 buy/watch/hold/sell）"""
    try:
        content = llm_call(prompt).strip()
        if not content:
            content = "⚠️ LLM 未返回内容（可能受 token 上限或模型行为影响），请重试。"
    except Exception as e:
        content = f"波浪分析失败：{e}"
    score, signal = extract_llm_score(content)
    content = strip_score_json(content)
    return Section(key="wave", title="波浪理论", content=content, score=score, signal=signal)


def analyze_chan_llm(df, stock_name, llm_call) -> Section:
    tail = df.tail(60)
    closes = tail["close"].round(2).tolist()
    highs  = tail["high"].round(2).tolist()
    lows   = tail["low"].round(2).tolist()
    dates  = tail["date"].tolist()
    last = df.iloc[-1]
    cur_price = round(float(last["close"]), 2)
    dif  = round(float(last.get("dif")  or 0), 4)
    dea  = round(float(last.get("dea")  or 0), 4)
    rsi6 = round(float(last.get("rsi6") or 50), 1)
    ma5   = round(float(last.get("ma5")   or 0), 2)
    ma20  = round(float(last.get("ma20")  or 0), 2)
    ma60  = round(float(last.get("ma60")  or 0), 2)
    ma120 = round(float(last.get("ma120") or 0), 2)
    ma250 = round(float(last.get("ma250") or 0), 2)
    vol_ratio = round(float(last.get("vol_ratio") or 1.0), 2)
    # 日期标注的序列（每10个标注一次）
    labeled = []
    for i, (d, c, h, lo) in enumerate(zip(dates, closes, highs, lows)):
        if i % 10 == 0 or i == len(dates) - 1:
            labeled.append(f"{d}: 收{c} 高{h} 低{lo}")
    labeled_str = "\n".join(labeled)

    prompt = f"""你是缠论专家，请对 {stock_name} 进行专业缠论分析。

【近60日价格序列（关键节点标注）】
{labeled_str}

【当前指标】
当前价={cur_price}  MA5={ma5}  MA20={ma20}  MA60={ma60}  MA120={ma120}（半年线）  MA250={ma250}（年线）  DIF={dif}  DEA={dea}  RSI(6)={rsi6}  量比={vol_ratio}x

【近60日完整收盘价】
{closes}

【要求】请进行严格的缠论分析，必须包含：

1. **笔和线段结构**
   - 识别近期形成的笔（给出起止价格及日期）
   - 是否已形成线段或中枢，中枢区间是多少

2. **中枢分析**
   - 当前是否在中枢内/中枢上方/中枢下方运行
   - 中枢区间：XX ~ XX（对应具体价格区间）
   - 是否有中枢突破或中枢扩张迹象

3. **背驰判断**（这是核心，必须给出具体分析）
   - 是否出现顶背驰：如是，说明哪几笔的MACD（柱面积/DIF绝对值）在减小，对应价格反而新高
   - 是否出现底背驰：如是，说明哪几笔的MACD在减小，对应价格反而新低
   - 背驰强度（弱背驰/背驰）及判断依据

4. **买卖点判断**
   - 当前最近形成的买卖点类型（一买/二买/三买/一卖/二卖/三卖）
   - 给出具体的点位价格及判断依据

5. **后市预判**
   - 主要走势预判及目标位（给出价格区间）
   - 关键确认信号（满足什么条件则确认方向）

6. **操作建议**
   - 建议操作：买入/持有/减仓/卖出（附条件）
   - 参考止损位：XX（基于缠论笔段失效判断）
   - 目标位：XX ~ XX

输出最后必须追加一行严格 JSON（基于你上面的分析给出评分与信号）：{{"score":整数0到100,"signal":"buy或watch或hold或sell"}}
（score 越高越偏多：>65偏多、35-65中性、<35偏空；signal 只能是 buy/watch/hold/sell）"""
    try:
        content = llm_call(prompt).strip()
        if not content:
            content = "⚠️ LLM 未返回内容（可能受 token 上限或模型行为影响），请重试。"
    except Exception as e:
        content = f"缠论分析失败：{e}"
    score, signal = extract_llm_score(content)
    content = strip_score_json(content)
    return Section(key="chan", title="缠论分析", content=content, score=score, signal=signal)


def analyze_llm_tech(df, stock_name: str, llm_call, sections: list) -> Section:
    """基于所有已计算的量化指标，让 LLM 做综合技术精讲，输出具体点位和操作建议。"""
    last  = df.iloc[-1]
    prev5 = df.tail(6).iloc[0]  # 5日前
    close  = round(float(last["close"]), 2)
    close5 = round(float(prev5["close"]), 2)
    chg5   = round((close - close5) / close5 * 100, 2)

    # 组装量化快照
    ma5   = round(float(last.get("ma5")   or 0), 2)
    ma10  = round(float(last.get("ma10")  or 0), 2)
    ma20  = round(float(last.get("ma20")  or 0), 2)
    ma60  = round(float(last.get("ma60")  or 0), 2)
    ma120 = round(float(last.get("ma120") or 0), 2)
    ma250 = round(float(last.get("ma250") or 0), 2)
    dif  = round(float(last.get("dif")  or 0), 4)
    dea  = round(float(last.get("dea")  or 0), 4)
    bar  = round(float(last.get("macd_bar") or 0), 4)
    rsi6 = round(float(last.get("rsi6")  or 50), 1)
    r12  = round(float(last.get("rsi12") or 50), 1)
    k    = round(float(last.get("kdj_k") or 50), 1)
    d_   = round(float(last.get("kdj_d") or 50), 1)
    j    = round(float(last.get("kdj_j") or 50), 1)
    boll_u = round(float(last.get("boll_upper") or 0), 2)
    boll_m = round(float(last.get("boll_mid")   or 0), 2)
    boll_l = round(float(last.get("boll_lower") or 0), 2)
    wr14   = round(float(last.get("wr14") or -50), 1)
    vol_r  = round(float(last.get("vol_ratio") or 1.0), 2)

    # 近5日涨跌幅序列
    tail5 = df.tail(5)
    daily_chg = []
    for i in range(len(tail5)):
        row = tail5.iloc[i]
        c = round(float(row["close"]), 2)
        if i > 0:
            prev_c = round(float(tail5.iloc[i-1]["close"]), 2)
            pct = round((c - prev_c) / prev_c * 100, 2)
            daily_chg.append(f"{row['date']}({pct:+.2f}%)")
        else:
            daily_chg.append(f"{row['date']}(基准)")

    # 从已计算 sections 中提取背离摘要
    div_summary = ""
    for s in sections:
        if s is not None and s.key == "divergence" and s.content:
            first_line = s.content.strip().split("\n")[0]
            div_summary = f"背离检测：{first_line}"
            break

    # 近5日最高/最低
    tail5_high = round(float(df.tail(5)["high"].max()), 2)
    tail5_low  = round(float(df.tail(5)["low"].min()), 2)

    prompt = f"""你是顶尖A股技术分析师，请对 {stock_name}（当前价{close}）进行技术面精讲分析。

【指标快照】
均线：MA5={ma5}  MA10={ma10}  MA20={ma20}  MA60={ma60}  MA120={ma120}  MA250={ma250}
MACD：DIF={dif}  DEA={dea}  柱={bar}（{'零轴上方' if dif > 0 else '零轴下方'}）
RSI(6)={rsi6}  RSI(12)={r12}  KDJ K={k} D={d_} J={j}
布林带：上{boll_u}  中{boll_m}  下{boll_l}  WR(14)={wr14}
量比={vol_r}x  近5日涨跌={chg5:+.2f}%  近5日区间=[{tail5_low},{tail5_high}]
{div_summary}

请按以下6个方面分析，每项结合具体数值展开：

## 1. 趋势判断
均线排列（多头/空头/缠绕）+ 价格与MA60/MA120/MA250的位置关系 → 大/中/短三级趋势结论

## 2. 动能解读
MACD零轴位置+柱线扩缩含义、RSI区间判断、KDJ J值状态、多指标是否共振

## 3. 关键价位
- 阻力：列2个具体价格（注明来源）
- 支撑：列2个具体价格（注明来源）
- 变盘位：突破/跌破哪个价改变趋势

## 4. 量价与风险
量比+近期价格的量价关系解读；当前最主要的1个技术风险

## 5. 操作建议
短线（1-5日）：入场区间+止损+目标（具体数字）
中线（1-4周）：建仓条件+止损+目标（具体数字）

## 6. 仓位建议
当前看多/看空强度，建议仓位比例

输出最后必须追加一行严格 JSON（基于你上面的分析给出评分与信号）：{{"score":整数0到100,"signal":"buy或watch或hold或sell"}}
（score 越高越偏多：>65偏多、35-65中性、<35偏空；signal 只能是 buy/watch/hold/sell）"""

    try:
        content = llm_call(prompt).strip()
    except Exception as e:
        logger.warning("[llm_tech] LLM 调用失败 %s: %s", stock_name, e)
        content = f"技术综合分析失败：{e}"

    # LLM 返回空时用规则降级
    if not content:
        logger.warning("[llm_tech] LLM 返回空内容，降级为规则摘要")
        ma_line = f"MA5={ma5} MA10={ma10} MA20={ma20} MA60={ma60}"
        content = (
            f"**技术指标快照（LLM 未返回）**\n"
            f"- 均线：{ma_line}\n"
            f"- MACD：DIF={dif} DEA={dea} 柱={bar}（{'零轴上方' if dif > 0 else '零轴下方'}）\n"
            f"- RSI(6)={rsi6}  KDJ J={j}\n"
            f"- 布林带：上{boll_u} 中{boll_m} 下{boll_l}\n"
            f"- 量比={vol_r}x\n"
            f"\n（LLM 综合精讲暂不可用，请检查 LLM API 配置）"
        )

    score, signal = extract_llm_score(content)
    content = strip_score_json(content)
    return Section(key="llm_tech", title="技术综合精讲（LLM）",
                   content=content, score=score, signal=signal)
