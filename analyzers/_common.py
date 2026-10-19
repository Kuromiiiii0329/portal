"""
portal/analyzers/_common.py
分析器共享工具 —— 从各 analyzer 中抽出的通用纯函数，行为与原实现逐字节等价。

⚠️ 注意：本模块只做「物理归位」，不统一各处不同的阈值/规则。
   例如 fundamental/industry/merger 各自的 _score_to_signal 阈值不一致，
   保持各自定义，不在此合并（合并属于改行为）。
"""
from __future__ import annotations

import re as _re
import json as _json


def extract_llm_score(content: str) -> tuple:
    """从 LLM 输出末尾解析 {"score":..,"signal":..}，打分完全由 LLM 决定。

    解析失败时降级 (50, "hold")，不做任何硬编码信号推导。

    （原 TechnicalAnalyzer._extract_llm_score，逐字节等价搬迁。）
    """
    if not content:
        return 50, "hold"
    # 抠最后一个 {...} JSON 对象
    matches = _re.findall(r"\{[^{}]*\}", content)
    for raw in reversed(matches):
        try:
            obj = _json.loads(raw)
        except Exception:
            continue
        if "score" in obj or "signal" in obj:
            try:
                score = int(round(float(obj.get("score", 50))))
            except Exception:
                score = 50
            score = max(0, min(100, score))
            sig = str(obj.get("signal", "hold")).strip().lower()
            if sig not in ("buy", "watch", "hold", "sell"):
                cn = {"买入": "buy", "关注": "watch", "观望": "watch",
                      "持有": "hold", "减仓": "hold", "卖出": "sell"}
                sig = cn.get(str(obj.get("signal", "")).strip(), "hold")
            return score, sig
    return 50, "hold"


def strip_score_json(content: str) -> str:
    """从展示文本里移除末尾的 score/signal JSON 行（用户不需要看到原始 JSON）。

    （原 TechnicalAnalyzer._strip_score_json，逐字节等价搬迁。）
    """
    if not content:
        return content
    # 去掉包含 "score" 和 "signal" 的 JSON 片段及其所在行
    cleaned = _re.sub(r'\{[^{}]*"s(core|ignal)"[^{}]*\}', "", content)
    # 清理因删除产生的空行
    lines = [ln for ln in cleaned.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()
