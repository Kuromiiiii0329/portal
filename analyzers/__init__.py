"""
portal/analyzers/__init__.py
分析器包入口，注册所有可用分析器。
"""
from .technical import TechnicalAnalyzer
from .fundamental import FundamentalAnalyzer
from .industry import IndustryAnalyzer
from .merger import merge_results

ANALYZER_REGISTRY = {
    "technical": TechnicalAnalyzer,
    "fundamental": FundamentalAnalyzer,
    "industry": IndustryAnalyzer,
}

__all__ = [
    "TechnicalAnalyzer",
    "FundamentalAnalyzer",
    "IndustryAnalyzer",
    "ANALYZER_REGISTRY",
    "merge_results",
]
