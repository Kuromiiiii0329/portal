"""
portal/analyzers/technical/  技术面分析器（包）

复用 src/stock_analyzer.py（StockTrendAnalyzer）+ pandas 计算 KDJ/布林带。
子模块：ma_system / macd / rsi / volume / kdj / bollinger / pattern / wave / chan

—— 模块化重构说明 ——
原单文件 technical.py（1513行）按职责拆分为子模块，本 __init__.py 保留 TechnicalAnalyzer
类作为对外唯一入口，各 _analyze_* 方法改为「薄委托」到子模块的纯函数，行为逐字节等价：
  - indicators.py       compute_indicators / normalize_volume_scale
  - sections_basic.py   ma/macd/rsi/kdj/bollinger/volume/overbought（纯量化）
  - sections_data.py    chip/turnover/margin（依赖 akshare，可返回 None）
  - sections_llm.py     pattern/wave/chan/llm_tech（LLM）
  - divergence.py       背离检测引擎
  - .._common           extract_llm_score / strip_score_json（跨 analyzer 共享）

⚠️ 契约：类的公开/私有方法名保持不变。market.py 直接调用本类的
   _compute_indicators/_analyze_ma/_analyze_macd/_analyze_rsi/_analyze_kdj/
   _analyze_bollinger/_analyze_overbought/_analyze_divergence/_analyze_volume，
   这些方法名与签名不可改。（market.py 另调 tech._score_to_signal，此方法本类从未
   定义——属既有缺陷，重构保持现状，不新增。）
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from ..base import BaseAnalyzer, DimensionResult, Section
from .._common import extract_llm_score, strip_score_json
from . import indicators as _indicators
from . import sections_basic as _basic
from . import sections_data as _data
from . import sections_llm as _llm
from . import divergence as _divergence

logger = logging.getLogger(__name__)


class TechnicalAnalyzer(BaseAnalyzer):
    name = "技术面"
    dimension = "technical"
    description = "均线/MACD/RSI/KDJ/布林带/量价/形态/波浪/缠论"

    MODULES = {
        "ma_system":   "均线系统（MA5/10/20/60）",
        "macd":        "MACD 指标",
        "rsi":         "RSI 超买超卖",
        "kdj":         "KDJ 随机指标",
        "bollinger":   "布林带",
        "overbought":  "超买超卖综合（RSI+KDJ+WR+布林）",
        "divergence":  "背离检测（顶背离/底背离）",
        "volume":      "量价关系",
        "llm_tech":    "技术指标综合精讲（LLM，基于所有量化指标）",
        "pattern":     "K线形态（LLM）",
        "wave":        "波浪理论（LLM）",
        "chan":         "缠论（LLM）",
        "chip":        "筹码分布（成本集中度）",
        "turnover":    "换手率趋势（近30日）",
        "margin":      "融资融券余额趋势",
    }
    DEFAULT_MODULES = ["ma_system", "macd", "rsi", "kdj", "bollinger",
                       "overbought", "divergence", "volume", "llm_tech"]

    def analyze(self, stock_code, stock_name, df, modules, llm_call, search, log=None):
        result = DimensionResult(dimension=self.dimension, name=self.name)

        if df is None or df.empty or len(df) < 20:
            result.error = "K线数据不足（少于20日），无法进行技术分析"
            result.score = 50
            result.signal = "hold"
            return result

        def _timed(name, fn):
            t0 = time.perf_counter()
            sec = fn()
            elapsed = time.perf_counter() - t0
            if log:
                log(f"  ⏱ [{name}] {elapsed*1000:.0f}ms")
            return sec

        try:
            df = df.copy().sort_values("date").reset_index(drop=True)
            self._df = df          # 保留引用，供背离渲染时取日期
            df = self._compute_indicators(df)
            sections = []

            if "ma_system" in modules:
                sections.append(_timed("ma_system",   lambda: self._analyze_ma(df, stock_code)))
            if "macd" in modules:
                sections.append(_timed("macd",        lambda: self._analyze_macd(df)))
            if "rsi" in modules:
                sections.append(_timed("rsi",         lambda: self._analyze_rsi(df)))
            if "kdj" in modules:
                sections.append(_timed("kdj",         lambda: self._analyze_kdj(df)))
            if "bollinger" in modules:
                sections.append(_timed("bollinger",   lambda: self._analyze_bollinger(df)))
            if "overbought" in modules:
                sections.append(_timed("overbought",  lambda: self._analyze_overbought(df)))
            if "divergence" in modules:
                sections.append(_timed("divergence",  lambda: self._analyze_divergence(df)))
            if "volume" in modules:
                sections.append(_timed("volume",      lambda: self._analyze_volume(df)))
            if "pattern" in modules and llm_call:
                sections.append(_timed("pattern",     lambda: self._analyze_pattern_llm(df, stock_name, llm_call)))
            if "wave" in modules and llm_call:
                sections.append(_timed("wave",        lambda: self._analyze_wave_llm(df, stock_name, llm_call)))
            if "chan" in modules and llm_call:
                sections.append(_timed("chan",        lambda: self._analyze_chan_llm(df, stock_name, llm_call)))
            if "chip" in modules:
                sections.append(_timed("chip",        lambda: self._analyze_chip(df, stock_code)))
            if "turnover" in modules:
                sections.append(_timed("turnover",    lambda: self._analyze_turnover(df)))
            if "margin" in modules:
                sections.append(_timed("margin",      lambda: self._analyze_margin(df, stock_code)))
            if "llm_tech" in modules and llm_call:
                sections.append(_timed("llm_tech",    lambda: self._analyze_llm_tech(df, stock_name, llm_call, sections)))

            # 过滤掉降级/无真实数据返回 None 的子模块（不出假情报）
            sections = [s for s in sections if s is not None]
            result.sections = sections

            # 注意：数值指标（ma/macd/rsi 等）的 score/signal 此处为占位（50/hold），
            # 由外层 server.py 统一调 LLM 打分并回写；维度综合分/信号也在外层回写后重算。
            # 这里先给一个临时聚合，保证 analyzer 单独运行（llm_call=None）时不为空。
            scored = [s for s in sections if s.score != 50]
            result.score = int(sum(s.score for s in scored) / len(scored)) if scored else 50
            result.signal = "hold"

            # 一句话摘要
            ma_sec = next((s for s in sections if s is not None and s.key == "ma_system"), None)
            result.summary = ma_sec.content.split("\n")[0] if ma_sec else f"技术分析（{len(sections)}项）"

        except Exception as e:
            logger.exception("TechnicalAnalyzer error for %s: %s", stock_code, e)
            result.error = str(e)

        return result

    # ── 指标计算（委托 indicators.py）────────────────────────
    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return _indicators.compute_indicators(df)

    @staticmethod
    def _normalize_volume_scale(volume: pd.Series) -> pd.Series:
        return _indicators.normalize_volume_scale(volume)

    # ── 纯量化子模块（委托 sections_basic.py）────────────────
    def _analyze_ma(self, df, stock_code) -> Section:
        return _basic.analyze_ma(df, stock_code)

    def _analyze_macd(self, df) -> Section:
        return _basic.analyze_macd(df)

    def _analyze_rsi(self, df) -> Section:
        return _basic.analyze_rsi(df)

    def _analyze_kdj(self, df) -> Section:
        return _basic.analyze_kdj(df)

    def _analyze_bollinger(self, df) -> Section:
        return _basic.analyze_bollinger(df)

    def _analyze_volume(self, df) -> Section:
        return _basic.analyze_volume(df)

    def _analyze_overbought(self, df) -> Section:
        return _basic.analyze_overbought(df)

    # ── 数据 IO 子模块（委托 sections_data.py）───────────────
    def _analyze_chip(self, df: pd.DataFrame, stock_code: str) -> Section:
        return _data.analyze_chip(df, stock_code)

    def _analyze_turnover(self, df: pd.DataFrame) -> Section:
        return _data.analyze_turnover(df)

    def _analyze_margin(self, df: pd.DataFrame, stock_code: str) -> Section:
        return _data.analyze_margin(df, stock_code)

    # ── LLM 子模块（委托 sections_llm.py）────────────────────
    def _analyze_pattern_llm(self, df, stock_name, llm_call) -> Section:
        return _llm.analyze_pattern_llm(df, stock_name, llm_call)

    def _analyze_wave_llm(self, df, stock_name, llm_call) -> Section:
        return _llm.analyze_wave_llm(df, stock_name, llm_call)

    def _analyze_chan_llm(self, df, stock_name, llm_call) -> Section:
        return _llm.analyze_chan_llm(df, stock_name, llm_call)

    def _analyze_llm_tech(self, df: pd.DataFrame, stock_name: str, llm_call, sections: list) -> Section:
        return _llm.analyze_llm_tech(df, stock_name, llm_call, sections)

    # ── 背离引擎（委托 divergence.py，df 显式传参替代 self._df）──
    def _analyze_divergence(self, df) -> Section:
        return _divergence.analyze_divergence(df)

    # ── LLM 分数解析（委托 _common，保持静态方法名兼容）──────
    @staticmethod
    def _extract_llm_score(content: str) -> tuple:
        return extract_llm_score(content)

    @staticmethod
    def _strip_score_json(content: str) -> str:
        return strip_score_json(content)


# ── 独立测试入口 ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys as _sys
    code = _sys.argv[1] if len(_sys.argv) > 1 else "600519"
    print(f"测试技术面分析：{code}")
    from data_provider import DataFetcherManager
    from src.config import get_config
    config = get_config()
    mgr = DataFetcherManager(config)
    df = mgr.get_stock_data(code, days=90)
    if df is not None and not df.empty:
        analyzer = TechnicalAnalyzer()
        result = analyzer.analyze(code, code, df,
                                  modules=list(TechnicalAnalyzer.DEFAULT_MODULES),
                                  llm_call=None, search=None)
        print(f"评分: {result.score}  信号: {result.signal}")
        for s in result.sections:
            print(f"\n[{s.title}]\n{s.content}")
    else:
        print("获取数据失败")
