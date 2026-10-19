"""
portal/analyzers/industry/chain.py
产业链分析子领域 —— LLM + 搜索。

子模块：chain_keywords（识别关键词）/ key_commodity（核心大宗/产品价格）/
        industry_chain（产业链地位）/ competitors（竞争格局）/ policy（政策风向）。

从原 IndustryAnalyzer 的关键词识别 + 各 LLM 子模块 + 工具方法逐字节搬迁，
改为模块级纯函数。含大宗商品期货映射、降级关键词模板两个常量。
"""
from __future__ import annotations

import json
import logging

from ..base import Section

logger = logging.getLogger(__name__)

# ── 大宗商品期货代码映射（akshare futures_main_sina 使用）────────
COMMODITY_FUTURES_MAP = {
    "碳酸锂": "LC",   # 碳酸锂期货
    "锂": "LC",
    "铜": "CU",
    "铝": "AL",
    "钢": "RB",     # 螺纹钢
    "煤": "ZC",     # 郑煤
    "原油": "SC",
    "石油": "SC",
    "黄金": "AU",
    "白银": "AG",
}

# ── 内置产业链关键词模板（LLM 生成失败时的降级方案）────────────
INDUSTRY_KEYWORDS_FALLBACK = {
    "锂": ["碳酸锂价格", "锂矿供需", "新能源电池产业链"],
    "钴": ["钴价格走势", "钴矿供需"],
    "铜": ["铜价格走势", "铜矿供需", "铜期货"],
    "铝": ["铝价格走势", "电解铝产能"],
    "钢": ["钢铁价格", "铁矿石价格", "螺纹钢期货"],
    "煤": ["动力煤价格", "焦煤价格", "能源供给"],
    "石油": ["原油价格", "炼化利润", "成品油价格"],
    "化工": ["化工原料价格", "石油化工链"],
    "白酒": ["白酒行业景气", "茅台价格", "高端消费"],
    "猪": ["猪肉价格", "猪周期", "饲料价格"],
    "光伏": ["硅料价格", "光伏装机量", "组件价格"],
    "半导体": ["芯片供需", "半导体景气", "晶圆代工"],
    "新能源": ["电池材料价格", "碳酸锂", "新能源汽车销量"],
    "医药": ["医保政策", "集采价格", "创新药研发"],
    "房地产": ["土地市场", "房价走势", "地产政策"],
    "银行": ["利率政策", "不良贷款率", "息差"],
}


# ── 核心：自动识别产业链关键词 ───────────────────────────────
def get_chain_keywords(stock_code: str, stock_name: str, llm_call) -> list[str]:
    prompt = f"""你是A股产业链分析专家。
请为 {stock_name}（{stock_code}）列出5-8个最重要的产业链搜索关键词，
用于搜索该公司相关的大宗商品价格、上下游行业动态、产品供需等信息。

要求：
- 每个关键词要具体、可搜索（如"碳酸锂价格走势"而非"锂"）
- 优先列出对公司盈利影响最直接的关键词
- 涵盖：核心产品/原材料价格、行业景气度、上下游需求

请直接返回 JSON 数组格式，例如：["碳酸锂价格", "锂矿供需", "新能源电池装机量"]
只返回JSON，不要其他文字。"""
    try:
        resp = llm_call(prompt).strip()
        # 提取 JSON 数组
        start = resp.find("[")
        end   = resp.rfind("]") + 1
        if start >= 0 and end > start:
            keywords = json.loads(resp[start:end])
            if isinstance(keywords, list):
                return [str(k) for k in keywords[:8]]
    except Exception as e:
        logger.warning("Keywords LLM error for %s: %s", stock_name, e)

    # 降级：根据股票名称关键词匹配
    for key, kws in INDUSTRY_KEYWORDS_FALLBACK.items():
        if key in stock_name:
            return kws
    return [f"{stock_name}行业景气", f"{stock_name}竞争格局", f"{stock_name}政策动向"]


# ── 各子模块 ─────────────────────────────────────────────
def get_commodity_price(keyword: str) -> str:
    """根据关键词查期货主力合约最新价，返回格式化字符串，失败返回空字符串。"""
    code = None
    for key, futures_code in COMMODITY_FUTURES_MAP.items():
        if key in keyword:
            code = futures_code
            break
    if not code:
        return ""
    try:
        import akshare as ak
        df = ak.futures_main_sina(symbol=code)
        if df is None or df.empty:
            return ""
        # 取最新一行
        latest = df.iloc[-1]
        price = float(latest.get("收盘价", latest.get("close", 0)))
        date  = str(latest.get("日期", latest.get("date", "")))[:10]
        if price <= 0:
            return ""
        return f"{keyword}：{price:.0f} 元/吨（{date}）"
    except Exception as e:
        logger.debug("get_commodity_price %s(%s) error: %s", keyword, code, e)
        return ""


def analyze_commodity(stock_name, keywords, search_context, llm_call) -> Section:
    price_lines = [get_commodity_price(kw) for kw in keywords[:3]]
    price_text  = "\n".join(l for l in price_lines if l)
    context_text = build_context(search_context, keywords[:3])
    prompt = f"""你是商品市场分析师。
请基于以下信息，分析与 {stock_name} 最相关的大宗商品/核心产品价格走势：
{"【实时期货价格】\n" + price_text + "\n\n" if price_text else ""}
{context_text if context_text else f"无实时搜索数据，请基于你的知识分析 {stock_name} 相关产品价格走势。"}

请输出（不超过200字）：
1. 核心商品/产品当前价格趋势（上涨/下跌/震荡）
2. 价格走势对公司利润的直接影响（利多/利空/中性）
3. 未来3-6个月预判

输出最后一行格式：【信号】利多/利空/中性"""
    content, score, signal = llm_with_signal(llm_call, prompt)
    return Section(key="key_commodity", title="核心大宗/产品价格",
                   content=content, score=score, signal=signal)


def analyze_chain_position(stock_code, stock_name, keywords, search_context, llm_call) -> Section:
    context_text = build_context(search_context, keywords)
    prompt = f"""你是产业链分析专家。
请分析 {stock_name}（{stock_code}）在产业链中的位置和竞争优势：

{context_text if context_text else "请基于你的知识进行分析。"}

请输出（不超过200字）：
1. 公司在产业链的位置（上游/中游/下游）
2. 对上下游的议价能力
3. 产业链中的核心竞争优势或薄弱环节
4. 当前产业链景气度

输出最后一行格式：【信号】利多/利空/中性"""
    content, score, signal = llm_with_signal(llm_call, prompt)
    return Section(key="industry_chain", title="产业链地位",
                   content=content, score=score, signal=signal)


def analyze_competitors(stock_code, stock_name, search_context, llm_call) -> Section:
    context_text = build_context(search_context, list(search_context.keys())[:2])
    prompt = f"""你是行业竞争分析专家。
请分析 {stock_name}（{stock_code}）的竞争格局：

{context_text if context_text else "请基于你的知识进行分析。"}

请输出（不超过150字）：
1. 主要竞争对手（2-3家）
2. 公司市场份额估计
3. 相对竞争优势/劣势
4. 行业集中度趋势（集中/分散/整合中）

输出最后一行格式：【信号】利多/利空/中性"""
    content, score, signal = llm_with_signal(llm_call, prompt)
    return Section(key="competitors", title="竞争格局",
                   content=content, score=score, signal=signal)


def analyze_policy(stock_name, keywords, search_context, llm_call) -> Section:
    policy_keywords = [k for k in keywords if any(p in k for p in ["政策", "监管", "补贴", "双碳", "限制"])]
    context_text = build_context(search_context, policy_keywords or keywords[:2])
    prompt = f"""你是政策研究专家。
请分析当前影响 {stock_name} 所在行业的政策风向：

{context_text if context_text else "请基于你的知识进行分析。"}

请输出（不超过150字）：
1. 近期最重要的相关政策（支持/限制/中性）
2. 政策对行业/公司的短期影响
3. 政策趋势研判

输出最后一行格式：【信号】利多/利空/中性"""
    content, score, signal = llm_with_signal(llm_call, prompt)
    return Section(key="policy", title="政策风向",
                   content=content, score=score, signal=signal)


# ── 工具方法 ─────────────────────────────────────────────
def build_context(search_context: dict, keywords: list) -> str:
    parts = []
    for kw in keywords:
        snippets = search_context.get(kw, [])
        if snippets:
            parts.append(f"【{kw}】\n" + "\n".join(f"  {s}" for s in snippets[:2]))
    return "\n\n".join(parts)


def llm_with_signal(llm_call, prompt) -> tuple[str, int, str]:
    """调用 LLM，从末尾提取【信号】标记，返回 (content, score, signal)。"""
    try:
        content = llm_call(prompt).strip()
    except Exception as e:
        return f"分析失败：{e}", 50, "hold"

    score = 50
    signal = "hold"
    signal_map = {"利多": ("buy", 68), "利空": ("sell", 32), "中性": ("hold", 50)}
    for label, (sig, sc) in signal_map.items():
        if f"【信号】{label}" in content or f"【信号】 {label}" in content:
            signal, score = sig, sc
            break

    return content, score, signal
