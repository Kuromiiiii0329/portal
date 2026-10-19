"""
portal/analyzers/merger.py
合并所有维度结果，生成最终结构化报告。
权重：技术面 40% + 基本面 40% + 产业链 20%
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any

from .base import DimensionResult

TZ_CN = timezone(timedelta(hours=8))

DIMENSION_WEIGHTS = {
    "technical": 0.40,
    "fundamental": 0.40,
    "industry": 0.20,
}

SIGNAL_PRIORITY = {
    "buy": 4,
    "watch": 3,
    "hold": 2,
    "sell": 1,
}

SIGNAL_LABELS = {
    "buy": "买入",
    "watch": "关注",
    "hold": "持有",
    "sell": "卖出",
}

SCORE_TO_SIGNAL = [
    (70, "buy"),
    (55, "watch"),
    (40, "hold"),
    (0,  "sell"),
]


def _score_to_signal(score: int) -> str:
    for threshold, sig in SCORE_TO_SIGNAL:
        if score >= threshold:
            return sig
    return "sell"


def merge_results(
    stock_code: str,
    stock_name: str,
    results: list[DimensionResult],
    llm_call=None,
) -> dict[str, Any]:
    """
    将各维度分析结果合并为最终报告。

    Args:
        stock_code: 股票代码
        stock_name: 股票名称
        results:    各维度 DimensionResult 列表
        llm_call:   可选，用于生成综合结论的 LLM 调用函数

    Returns:
        {
          "stock_code": str,
          "stock_name": str,
          "overall_score": int,
          "overall_signal": str,
          "overall_signal_label": str,
          "conclusion": str,
          "dimensions": [...],
          "generated_at": str,
        }
    """
    result_map = {r.dimension: r for r in results}

    # ── 加权综合评分 ────────────────────────────────────────
    total_weight = 0.0
    weighted_score = 0.0
    for dim_result in results:
        if dim_result.error:
            continue
        w = DIMENSION_WEIGHTS.get(dim_result.dimension, 0.1)
        weighted_score += dim_result.score * w
        total_weight += w

    overall_score = int(weighted_score / total_weight) if total_weight > 0 else 50
    overall_signal = _score_to_signal(overall_score)

    # ── 生成综合结论 ─────────────────────────────────────────
    conclusion = _build_conclusion(stock_code, stock_name, results, overall_score, overall_signal, llm_call)

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "overall_score": overall_score,
        "overall_signal": overall_signal,
        "overall_signal_label": SIGNAL_LABELS.get(overall_signal, overall_signal),
        "conclusion": conclusion,
        "tech_summary": _build_tech_summary(result_map.get("technical")),
        "dimensions": [r.to_dict() for r in results],
        "generated_at": datetime.now(TZ_CN).isoformat(),
    }


def _build_conclusion(
    stock_code: str,
    stock_name: str,
    results: list[DimensionResult],
    overall_score: int,
    overall_signal: str,
    llm_call,
) -> str:
    """用 LLM 生成一段话综合结论，无 LLM 时用规则拼接。

    喂给 LLM 的不只是各维度一句话摘要，而是【每个子模块的指标明细】
    （标题+信号+评分+首行结论），让 LLM 有据可依，给出客观具体的结论。
    """
    if llm_call:
        # 组装各维度 + 子模块明细上下文
        blocks = []
        for r in results:
            if r.error:
                blocks.append(f"### {r.name}（分析失败：{r.error}）")
                continue
            lines = [f"### {r.name}（维度评分 {r.score}/100，信号：{SIGNAL_LABELS.get(r.signal, r.signal)}）"]
            for s in r.sections:
                # 取每个子模块的首行结论 + 信号 + 评分
                first = (s.content or "").strip().split("\n")[0].replace("**", "")
                sig_label = SIGNAL_LABELS.get(s.signal, s.signal)
                lines.append(f"- {s.title}（{sig_label}/{s.score}）：{first}")
            blocks.append("\n".join(lines))
        detail = "\n\n".join(blocks)

        prompt = f"""你是一位严谨、务实的 A 股分析师。请基于下面 {stock_name}（{stock_code}）各维度、各指标的**具体数据**，给出一段客观的综合投资结论。

各维度指标明细：
{detail}

系统加权综合评分：{overall_score}/100，综合信号：{SIGNAL_LABELS.get(overall_signal, overall_signal)}

请严格按以下要求输出（总计 180-280 字）：
1. **核心判断**：一句话给出当前定性（强势/偏多/震荡/偏空/弱势），必须引用上面 2-3 个最关键的具体指标数值作为依据（如"MACD金叉+RSI 68 偏高""底背离迹象浮现""个股跑输所属板块 α=-1.3%"）。
2. **多空依据**：分别列出「看多理由」和「看空理由」各 1-2 条，引用具体指标，客观呈现分歧，不要只报喜或只报忧。
3. **操作建议**：给出明确、可执行的建议——包括：观望还是参与、若参与建议的关注/买入价位区间或触发条件、止损参考位、仓位建议（轻仓/半仓等）。避免"仅供参考"式空话。
4. **风险提示**：一句话点明当前最需警惕的风险。

要求：客观中立、数据驱动、拒绝套话。直接输出结论，不要加标题、不要分点编号，用自然段落+ 关键处加粗。"""
        try:
            return llm_call(prompt).strip()
        except Exception:
            pass

    # 规则降级：拼接各维度摘要
    parts = [f"{r.name}：{r.summary}" for r in results if r.summary and not r.error]
    signal_label = SIGNAL_LABELS.get(overall_signal, overall_signal)
    return f"综合评分 {overall_score}/100，信号：{signal_label}。" + "；".join(parts) + "。"


def _build_tech_summary(tech: DimensionResult | None) -> str:
    """
    生成技术面一句话 AI summary，格式：
      🤖 偏多 — RSI底背离确认，MACD金叉（影响：短线买入信号，关注量能配合）
    纯规则生成，无需 LLM，保证每次都有输出。
    """
    if tech is None or tech.error:
        return ""

    sec_map = {s.key: s for s in tech.sections}
    parts_pos, parts_neg, parts_note = [], [], []

    # 背离
    div = sec_map.get("divergence")
    if div and div.content:
        first = div.content.strip().split("\n")[0]
        if "顶背离" in first:
            parts_neg.append("顶背离" + ("·已确认" if "已确认" in first else "·迹象浮现" if "迹象" in first else ""))
        if "底背离" in first:
            parts_pos.append("底背离" + ("·已确认" if "已确认" in first else "·迹象浮现" if "迹象" in first else ""))

    # MACD
    macd = sec_map.get("macd")
    if macd:
        d = macd.data or {}
        if d.get("golden"):  parts_pos.append("MACD金叉")
        elif d.get("death"): parts_neg.append("MACD死叉")
        elif d.get("dif", 0) > 0: parts_pos.append("MACD零轴上方")
        else: parts_neg.append("MACD零轴下方")

    # RSI
    rsi = sec_map.get("rsi")
    if rsi:
        r6 = (rsi.data or {}).get("rsi6", 50)
        if r6 > 70:   parts_neg.append(f"RSI超买({r6:.0f})")
        elif r6 < 30: parts_pos.append(f"RSI超卖({r6:.0f})")

    # KDJ
    kdj = sec_map.get("kdj")
    if kdj:
        d = kdj.data or {}
        if d.get("golden"):  parts_pos.append("KDJ金叉")
        elif d.get("death"): parts_neg.append("KDJ死叉")

    # 布林带
    boll = sec_map.get("bollinger")
    if boll:
        pos = (boll.data or {}).get("pos_pct", 50)
        if pos > 85:  parts_neg.append("触布林上轨")
        elif pos < 15: parts_pos.append("触布林下轨")

    # 均线系统
    ma = sec_map.get("ma_system")
    if ma:
        content = ma.content or ""
        if "多头排列" in content: parts_pos.append("均线多头")
        elif "空头排列" in content: parts_neg.append("均线空头")
        d = ma.data or {}
        if d.get("above_ma250") is False and d.get("ma250"):
            parts_neg.append("年线压制")
        elif d.get("above_ma250"):
            parts_pos.append("站上年线")

    # 量价
    vol = sec_map.get("volume")
    if vol and vol.score != 50:
        if vol.score >= 65: parts_pos.append("量价配合")
        elif vol.score <= 35: parts_neg.append("放量下跌")

    # 整体信号
    score = tech.score
    if score >= 70:   tone, emoji = "偏多", "🟢"
    elif score >= 58: tone, emoji = "中性偏多", "🔵"
    elif score >= 42: tone, emoji = "中性", "⚪"
    elif score >= 30: tone, emoji = "中性偏空", "🟠"
    else:             tone, emoji = "偏空", "🔴"

    # 拼接核心信号（最多3条）
    signals = parts_pos[:2] + parts_neg[:2]
    signals = signals[:3]
    sig_str = "，".join(signals) if signals else "无明确信号"

    # 影响描述
    if parts_pos and parts_neg:
        impact = "多空信号混杂，需后续确认"
    elif len(parts_pos) >= 2:
        impact = "多头信号共振，短线偏强"
    elif len(parts_neg) >= 2:
        impact = "空头信号共振，注意回调风险"
    else:
        impact = "信号中性，建议观望"

    return f"🤖 {tone} — {sig_str}（影响：{impact}）"
