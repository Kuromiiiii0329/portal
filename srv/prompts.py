"""
portal/srv/prompts.py
Prompt 模板层 —— Agent 综合研判 / 走势预测 prompt 构造。

从原 server.py 的 build_review_prompt / build_forecast_prompt 逐字节搬迁（纯字符串构造，无外部依赖）。
"""
from __future__ import annotations


def build_review_prompt(stock_name: str, stock_code: str, summary_text: str) -> str:
    """构造 Agent 综合研判 prompt（选项B）。数据全在 prompt 里，LLM 只做综合，不取数。"""
    return f"""你是一位严谨、务实的 A 股资深投研分析师。以下是系统已完成的量化分析结论与数据，\
请**基于这些【已算好的】结果**做综合研判，**不要重复取数、不要臆造未提供的数据**。

标的：{stock_name}（{stock_code}）

系统量化分析结论与数据：
{summary_text}

请严格按以下结构输出（总计 250-400 字，客观中立、数据驱动、拒绝套话）：
1. **核心判断**：一句话定性（强势/偏多/震荡/偏空/弱势），必须引用上面 2-3 个最关键的具体指标数值作为依据。
2. **多空依据**：分别列「看多理由」「看空理由」各 1-2 条，引用具体指标，客观呈现分歧。
3. **操作建议**：明确可执行——观望还是参与、关注/买入价位区间或触发条件、止损参考位、仓位建议（轻仓/半仓等）。
4. **风险提示**：一句话点明当前最需警惕的风险。

直接输出研判，用自然段落 + 关键处加粗，不要加大标题。"""


def build_forecast_prompt(stock_name: str, stock_code: str, summary_text: str,
                          kline_tail: list, backtest_result: dict = None,
                          active_patterns: list = None) -> str:
    """构造走势预测 prompt，要求 LLM 输出结构化 JSON。

    kline_tail:       最近若干条 {date, open, close, low, high} 记录，供 LLM 感知价格区间。
    backtest_result:  run_backtest() 的返回值，作为历史信号胜率参考。
    active_patterns:  当前已触发的形态及其回测胜率，由 _extract_active_patterns 提取。
    """
    kline_str = "\n".join(
        f"  {r.get('date')}: O={r.get('open')} H={r.get('high')} L={r.get('low')} C={r.get('close')}"
        for r in kline_tail if isinstance(r, dict)
    )

    # ── 当前触发形态（优先展示，LLM 应重点参考）──────────────
    active_str = ""
    if active_patterns:
        triggered = [p for p in active_patterns if p.get("triggered")]
        if triggered:
            lines = ["当前触发形态及历史回测胜率（⚡ 表示今日已触发，应重点参考）："]
            for p in triggered:
                stats = p.get("stats") or {}
                parts = []
                for d in ("1", "3", "5", "10", "20"):
                    s = stats.get(d)
                    if s and s.get("win_rate") is not None:
                        parts.append(f"{d}日胜率{s['win_rate']}%/均收益{s['avg_return']}%")
                stat_str = "  ".join(parts) if parts else "无统计"
                lines.append(f"  ⚡ {p['name']}（历史触发{p.get('count',0)}次，最近:{p.get('last_date','—')}）：{stat_str}")
            active_str = "\n" + "\n".join(lines) + "\n"

    # ── 全量回测摘要（所有信号，作为背景参考）────────────────
    backtest_str = ""
    if backtest_result and isinstance(backtest_result, dict):
        signals = backtest_result.get("signals") or {}
        lines = []
        for sig, v in signals.items():
            if not isinstance(v, dict) or not v.get("stats"):
                continue
            stats = v["stats"]
            parts = []
            for d in ("1", "3", "5", "10", "20"):
                s = stats.get(d)
                if s and s.get("win_rate") is not None:
                    parts.append(f"{d}日胜率{s['win_rate']}%/均收益{s['avg_return']}%")
            if parts:
                lines.append(f"  {sig}（触发{v.get('count',0)}次）：{'  '.join(parts)}")
        if lines:
            period = backtest_result.get("stock_days", "")
            backtest_str = f"\n历史信号回测（{period}，共{backtest_result.get('total_days',0)}日）：\n" + "\n".join(lines)

    return f"""你是一位 A 股量化技术分析师。以下是 {stock_name}（{stock_code}）的量化分析摘要和近期 K 线数据。
请基于这些信息，给出**走势预测**（这是模型推理的情景模拟，仅供研究参考，不构成投资建议）。

量化分析摘要：
{summary_text}
{active_str}{backtest_str}

最近 K 线（OHLC）：
{kline_str}

请**只返回严格 JSON**（不要解释、不要 markdown 围栏），格式如下：
{{
  "next_day": {{
    "high": 数字,
    "low": 数字,
    "trend": "上涨|震荡上行|震荡|震荡下行|下跌",
    "reason": "简短依据（1-2句）"
  }},
  "week_forecast": [
    {{"day": 1, "open": 数字, "high": 数字, "low": 数字, "close": 数字, "note": "简短说明"}},
    {{"day": 2, "open": 数字, "high": 数字, "low": 数字, "close": 数字, "note": ""}},
    {{"day": 3, "open": 数字, "high": 数字, "low": 数字, "close": 数字, "note": ""}},
    {{"day": 4, "open": 数字, "high": 数字, "low": 数字, "close": 数字, "note": ""}},
    {{"day": 5, "open": 数字, "high": 数字, "low": 数字, "close": 数字, "note": ""}},
    {{"day": 6, "open": 数字, "high": 数字, "low": 数字, "close": 数字, "note": ""}},
    {{"day": 7, "open": 数字, "high": 数字, "low": 数字, "close": 数字, "note": ""}}
  ]
}}

注意：
- 所有价格数字必须是合理的浮点数，基于最近收盘价 {kline_tail[-1].get('close') if kline_tail else '?'} 附近，幅度不超过 ±10%。
- 当前触发形态（⚡标注）的回测胜率是最重要的参考依据，请优先基于这些信号制定预测。
- next_day 的 high/low 要比 week_forecast[0] 的 high/low 更精确（日内区间更窄）。
- week_forecast 每日 open 应等于或接近前一日 close（连续性）。
- 只输出 JSON，不要任何其他文字。"""
