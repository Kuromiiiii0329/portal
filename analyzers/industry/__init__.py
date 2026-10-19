"""
portal/analyzers/industry/  产业链分析器（包）

功能：
1. LLM 自动识别该股票的核心产业链关键词（如天齐锂业 → 碳酸锂价格）
2. 搜索关键词最新资讯
3. LLM 综合分析：产业链地位/上下游/大宗商品/竞争格局/政策风向
4. 板块分析（真实数据，efinance/同花顺）：所属板块/景气/资金轮动

—— 模块化重构说明 ——
原单文件 industry.py（531行）按子领域拆分：
  - sectors.py   板块分析（真实数据，不依赖 LLM）
  - chain.py     产业链分析（LLM + 搜索）+ 大宗商品映射/降级关键词模板
本 __init__.py 保留 IndustryAnalyzer 类作为对外唯一入口，analyze() 编排逻辑不变，
各子模块方法委托到 sectors/chain。行为逐字节等价。

契约：IndustryAnalyzer.analyze 签名、MODULES/DEFAULT_MODULES、各 section.key 不变。
      _score_to_signal 阈值（65/52/38）保持 industry 自有，不与其他 analyzer 合并。
"""
from __future__ import annotations

import logging

from ..base import BaseAnalyzer, DimensionResult, Section
from . import sectors as _sectors
from . import chain as _chain

logger = logging.getLogger(__name__)


class IndustryAnalyzer(BaseAnalyzer):
    name = "产业链"
    dimension = "industry"
    description = "产业链地位/大宗商品/竞争格局/政策风向（LLM + 搜索）"

    MODULES = {
        # ── 板块子模块（真实数据，efinance/同花顺，不依赖 LLM）──
        "sector_membership": "所属板块（真实数据）",
        "sector_momentum":   "板块景气/相对强弱",
        "sector_fund_flow":  "板块资金/轮动",
        # ── 产业链子模块（LLM + 搜索）──
        "chain_keywords": "识别产业链关键词",
        "key_commodity":  "核心大宗/产品价格走势",
        "industry_chain": "产业链地位（上中下游）",
        "competitors":    "竞争格局",
        "policy":         "政策风向",
    }
    DEFAULT_MODULES = ["sector_membership", "sector_momentum", "sector_fund_flow",
                       "key_commodity", "industry_chain", "competitors", "policy"]

    def analyze(self, stock_code, stock_name, df, modules, llm_call, search):
        result = DimensionResult(dimension=self.dimension, name=self.name)
        sections = []

        # ── 板块子模块（真实数据，不依赖 LLM，优先执行）────────
        sector_keys = [k for k in modules if k in ("sector_membership", "sector_momentum", "sector_fund_flow")]
        if sector_keys:
            sections.extend(self._analyze_sectors(stock_code, stock_name, df, sector_keys))

        # ── 产业链 LLM 子模块（需要 API Key）──────────────────
        llm_keys = [k for k in modules if k not in ("sector_membership", "sector_momentum", "sector_fund_flow")]
        if not llm_call:
            if sections:
                # 有板块数据 → 仅缺 LLM 部分，整体仍可用
                result.sections = sections
                self._finalize_score(result, sections)
                result.summary = self._sector_summary(sections) or f"产业链评分 {result.score}/100（LLM 未配置，仅板块数据）"
                return result
            result.error = "产业链分析需要 LLM（未配置 API Key）"
            result.score = 50
            return result

        # Step 1：识别产业链关键词（优先读 meta 缓存里已有的关键词）
        keywords = self._get_chain_keywords(stock_code, stock_name, llm_call)
        if "chain_keywords" in modules:
            kw_content = "**自动识别的产业链关键词：**\n" + "\n".join(f"- {k}" for k in keywords)
            sections.append(Section(key="chain_keywords", title="产业链关键词",
                                    content=kw_content, data={"keywords": keywords},
                                    score=50, signal="hold"))

        # Step 2：搜索各关键词最新资讯（带本地缓存）
        search_context = {}
        try:
            from portal.data_cache import StockDataCache
            cache = StockDataCache()
        except Exception:
            cache = None

        if keywords:
            for kw in keywords[:4]:
                # 先查本地缓存
                if cache:
                    cached = cache.get_commodity(stock_code, kw)
                    if cached is not None:
                        search_context[kw] = cached
                        logger.info("Industry cache hit '%s': %d snippets", kw, len(cached))
                        continue

                # 缓存未命中 → 网络搜索
                if search:
                    try:
                        results = search(kw)
                        snippets = [r.get("snippet", r.get("content", ""))[:300]
                                    for r in (results or [])[:3] if r]
                        search_context[kw] = snippets
                        logger.info("Industry search '%s': %d results", kw, len(snippets))
                        # 写入本地缓存
                        if cache and snippets:
                            cache.save_commodity(stock_code, kw, snippets)
                    except Exception as e:
                        logger.warning("Search error for '%s': %s", kw, e)

        # Step 3：各子模块 LLM 分析
        if "key_commodity" in modules:
            sections.append(self._analyze_commodity(
                stock_name, keywords, search_context, llm_call))

        if "industry_chain" in modules:
            sections.append(self._analyze_chain_position(
                stock_code, stock_name, keywords, search_context, llm_call))

        if "competitors" in modules:
            sections.append(self._analyze_competitors(
                stock_code, stock_name, search_context, llm_call))

        if "policy" in modules:
            sections.append(self._analyze_policy(
                stock_name, keywords, search_context, llm_call))

        result.sections = sections
        self._finalize_score(result, sections)

        # 摘要：优先板块相对强弱，其次大宗/产业链
        sector_sum = self._sector_summary(sections)
        commodity_sec = next((s for s in sections if s.key == "key_commodity"), None)
        chain_sec     = next((s for s in sections if s.key == "industry_chain"), None)
        if sector_sum:
            result.summary = sector_sum
        elif commodity_sec:
            result.summary = commodity_sec.content.split("\n")[0]
        elif chain_sec:
            result.summary = chain_sec.content.split("\n")[0]
        else:
            result.summary = f"产业链评分 {result.score}/100"

        return result

    # ── 统一评分 + 板块摘要（编排辅助，留类内）───────────────
    @staticmethod
    def _finalize_score(result, sections):
        scored = [s for s in sections if s.score != 50]
        result.score = int(sum(s.score for s in scored) / len(scored)) if scored else 50
        result.signal = IndustryAnalyzer._score_to_signal(result.score)

    @staticmethod
    def _sector_summary(sections) -> str:
        """从板块子模块提取一句话摘要（相对强弱优先）。"""
        mom = next((s for s in sections if s.key == "sector_momentum"), None)
        if mom and mom.content:
            return mom.content.split("\n")[0]
        mem = next((s for s in sections if s.key == "sector_membership"), None)
        if mem and mem.data.get("primary"):
            names = "、".join(mem.data["primary"][:3])
            return f"所属核心题材板块：{names}"
        return ""

    @staticmethod
    def _score_to_signal(score: int) -> str:
        if score >= 65: return "buy"
        if score >= 52: return "watch"
        if score >= 38: return "hold"
        return "sell"

    # ── 板块子模块（委托 sectors.py）─────────────────────────
    def _analyze_sectors(self, stock_code, stock_name, df, sector_keys) -> list:
        return _sectors.analyze_sectors(stock_code, stock_name, df, sector_keys)

    # ── 产业链子模块（委托 chain.py）─────────────────────────
    def _get_chain_keywords(self, stock_code, stock_name, llm_call) -> list:
        return _chain.get_chain_keywords(stock_code, stock_name, llm_call)

    def _analyze_commodity(self, stock_name, keywords, search_context, llm_call) -> Section:
        return _chain.analyze_commodity(stock_name, keywords, search_context, llm_call)

    def _analyze_chain_position(self, stock_code, stock_name, keywords, search_context, llm_call) -> Section:
        return _chain.analyze_chain_position(stock_code, stock_name, keywords, search_context, llm_call)

    def _analyze_competitors(self, stock_code, stock_name, search_context, llm_call) -> Section:
        return _chain.analyze_competitors(stock_code, stock_name, search_context, llm_call)

    def _analyze_policy(self, stock_name, keywords, search_context, llm_call) -> Section:
        return _chain.analyze_policy(stock_name, keywords, search_context, llm_call)
