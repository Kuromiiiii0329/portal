"""
portal/analyzers/base.py
分析器基类 — 所有维度分析器的统一接口。
新增分析器只需继承此类并实现 analyze()，无需改动 server.py。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Section:
    """一个子模块的分析结果。"""
    key: str            # 唯一标识，如 "ma_system"
    title: str          # 显示标题，如 "均线系统"
    content: str        # Markdown 格式的文字分析
    data: dict = field(default_factory=dict)   # 原始结构化数据（供 UI 渲染图表用）
    signal: str = "hold"   # buy / hold / sell / watch
    score: int = 50        # 0-100


@dataclass
class DimensionResult:
    """一个分析维度（如技术面）的完整结果。"""
    dimension: str          # 维度唯一标识，如 "technical"
    name: str               # 显示名称，如 "技术面"
    sections: list[Section] = field(default_factory=list)
    summary: str = ""       # 该维度一句话结论
    score: int = 50         # 0-100
    signal: str = "hold"    # buy / hold / sell / watch
    error: str = ""         # 非空表示分析失败原因

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "name": self.name,
            "summary": self.summary,
            "score": self.score,
            "signal": self.signal,
            "error": self.error,
            "sections": [
                {
                    "key": s.key,
                    "title": s.title,
                    "content": s.content,
                    "data": s.data,
                    "signal": s.signal,
                    "score": s.score,
                }
                for s in self.sections
            ],
        }


class BaseAnalyzer:
    """
    所有分析器的基类。

    子类必须设置：
        name        — 显示名称（如 "技术面"）
        dimension   — 唯一标识（如 "technical"）
        description — 一行说明

    子类必须实现：
        analyze(stock_code, stock_name, df, modules, llm_call, search) -> DimensionResult
    """
    name: str = ""
    dimension: str = ""
    description: str = ""

    # 该分析器支持的子模块及其显示名称，供 UI 展示勾选框
    MODULES: dict[str, str] = {}
    # 默认启用的子模块
    DEFAULT_MODULES: list[str] = []

    def analyze(
        self,
        stock_code: str,
        stock_name: str,
        df,                  # pandas DataFrame：日线K线数据
        modules: list[str],  # 启用的子模块 key 列表
        llm_call,            # callable(prompt: str) -> str
        search,              # callable(query: str) -> list[dict]
    ) -> DimensionResult:
        raise NotImplementedError(f"{self.__class__.__name__} must implement analyze()")
