"""
portal/llm_notes.py — 技术指标 LLM 打分（score + signal + 说明）+ 交易日缓存

设计原则：**零硬编码打分**。代码只算指标的客观数值事实（存于 Section.data），
把这些事实喂给 LLM，由 LLM 直接给出 score(0-100)、signal(buy/watch/hold/sell)、
依据(reason)、影响(impact)。代码不做任何评级判断，不从 score 推导 signal。

双模式（由前端 setting「技术指标 LLM 打分」的 llm_mode 决定）：
  - batch:         所有指标打包一次调 LLM（快，默认）
  - per_indicator: 每个指标单独调 LLM（慢，更精细）

缓存：portal/data/stocks/{code}/llm_notes/{trade_date}.json
  同股票+同指标+同交易日不重复调 LLM，天然复用。

输出结构（每个 section）：
  {"score": int(0-100), "signal": "buy|watch|hold|sell", "reason": "...", "impact": "..."}
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 只对"数值型技术指标"打分，跳过本身就是 LLM 文本、已自打分的段
# （pattern/wave/chan/llm_tech 在 technical.py 内部自己调 LLM 出 score+signal）
NOTE_KEYS = {
    "ma_system", "macd", "rsi", "kdj", "bollinger", "overbought",
    "divergence", "volume", "chip", "turnover", "margin", "ma250",
}

VALID_SIGNALS = ("buy", "watch", "hold", "sell")


def _notes_dir(code: str) -> Path:
    d = Path(__file__).parent / "data" / "stocks" / code / "llm_notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _notes_path(code: str, trade_date: str) -> Path:
    safe_date = str(trade_date).replace("-", "")
    return _notes_dir(code) / f"{safe_date}.json"


def _load_notes(code: str, trade_date: str) -> dict:
    p = _notes_path(code, trade_date)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("notes", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_notes(code: str, trade_date: str, notes: dict):
    try:
        _notes_path(code, trade_date).write_text(
            json.dumps({"trade_date": trade_date, "notes": notes}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        logger.warning("[llm_notes] 保存失败 %s: %s", code, e)


def _parse_json(text: str) -> Optional[dict]:
    """从 LLM 回复里抠出首个 JSON 对象，容错 markdown 围栏/多余文字。"""
    if not text:
        return None
    # 去 ```json 围栏
    text = re.sub(r"```(?:json)?", "", text)
    # 抠第一个 {...}（贪婪到最后一个 }）
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end+1])
        except Exception:
            pass
    return None


def _strip_trailing_json(text: str) -> str:
    """从 LLM 回复里剥离 score/signal 的 JSON 片段，返回纯分析正文（供展示）。"""
    if not text:
        return ""
    # 去 markdown 围栏
    cleaned = re.sub(r"```(?:json)?", "", text)
    # 去掉含 score/signal 的 JSON 对象
    cleaned = re.sub(r'\{[^{}]*"s(?:core|ignal)"[^{}]*\}', "", cleaned)
    return cleaned.strip()


def _norm_score(obj) -> dict:
    """规范化单条打分为 {score, signal, reason, impact}。

    零硬编码原则：signal 由 LLM 直接给，非法时降级 hold（**不从 score 推导**）。
    """
    if not isinstance(obj, dict):
        return {"score": 50, "signal": "hold", "reason": str(obj), "impact": ""}

    # score：转 int，clamp [0,100]，非法 → 50
    try:
        score = int(round(float(obj.get("score", 50))))
    except Exception:
        score = 50
    score = max(0, min(100, score))

    # signal：白名单 + 中文兼容；非法直接降级 hold（禁止从 score 推导）
    raw_sig = str(obj.get("signal", "")).strip().lower()
    if raw_sig not in VALID_SIGNALS:
        cn_map = {
            "买入": "buy", "看多": "buy", "偏多": "buy",
            "关注": "watch", "观望": "watch",
            "持有": "hold", "减仓": "hold", "中性": "hold",
            "卖出": "sell", "看空": "sell", "偏空": "sell",
        }
        raw_sig = cn_map.get(str(obj.get("signal", "")).strip(), "")
    signal = raw_sig if raw_sig in VALID_SIGNALS else "hold"

    return {
        "score": score,
        "signal": signal,
        "reason": str(obj.get("reason", "")),
        "impact": str(obj.get("impact", "")),
    }


def _section_brief(s: dict) -> str:
    """指标客观数据摘要（喂给 LLM）。不含评分（代码不再算分，避免误导 LLM）。"""
    content = (s.get("content") or "").replace("\n", " ")[:200]
    data = s.get("data") or {}
    # data 可能含 numpy bool/int/float，default=str 兜底
    data_str = json.dumps(data, ensure_ascii=False, default=str)[:200] if data else ""
    return f"【{s.get('title')}】{content} 数据:{data_str}"


def _build_single_prompt(stock_name: str, s: dict) -> str:
    return f"""你是A股技术分析师。下面是 {stock_name} 的「{s.get('title')}」指标的**客观数值（由系统计算，不可篡改）**。请你**基于这些真实数值**做详细分析，并给出评分与信号。

{_section_brief(s)}

【要求】
1. 详细分析（分 2-4 点，每点结合上面的具体数值展开，说明该指标当前反映的多空含义、力度、需要关注的信号或风险）。
2. 分析写完后，最后必须**单独追加一行严格 JSON**给出评分与信号（基于你的分析）：
{{"score":整数0到100,"signal":"buy或watch或hold或sell","reason":"核心依据","impact":"对后市影响"}}

评分标准：score 越高越偏多（>65 偏多、35-65 中性震荡、<35 偏空）；signal 只能是 buy/watch/hold/sell。
注意：先输出分析正文，JSON 只放在最后一行，不要用 markdown 围栏包裹 JSON。"""


def _build_batch_prompt(stock_name: str, sections: list) -> str:
    items = "\n".join(f"[{s.get('key')}] {_section_brief(s)}" for s in sections)
    keys = ", ".join(f'"{s.get("key")}"' for s in sections)
    first_key = sections[0].get("key") if sections else "key"
    return f"""你是A股技术分析师。下面是 {stock_name} 的多个技术指标的**客观数值（由系统计算，不可篡改）**。请你**基于每个指标的真实数值**逐个给出评分与信号。

{items}

评分标准：score 为 0-100 整数，越高越偏多（>65 偏多、35-65 中性震荡、<35 偏空）。
信号 signal 只能是：buy / watch / hold / sell。

只返回严格 JSON 对象（不要解释、不要markdown围栏），键为指标标识，值为该指标的打分：
{{"{first_key}":{{"score":整数0到100,"signal":"buy或watch或hold或sell","reason":"依据,引用数值","impact":"影响"}}, ...其余指标同理}}
必须包含全部这些键：{keys}"""


def score_sections(code: str, stock_name: str, trade_date: str,
                   sections: list, llm_call, mode: str = "batch", log=None) -> dict:
    """对 sections（Section 对象或 dict）做逐指标 LLM 打分。

    返回 {section_key: {score, signal, reason, impact}}。
    带交易日缓存，只对未命中的指标调 LLM。调用方负责把 score/signal 回写到 Section。
    """
    def _log(m):
        if log: log(m)

    def _get(s, k, default=None):
        # 兼容 Section 对象与 dict
        if isinstance(s, dict):
            return s.get(k, default)
        return getattr(s, k, default)

    def _as_dict(s) -> dict:
        if isinstance(s, dict):
            return s
        return {"key": _get(s, "key"), "title": _get(s, "title"),
                "content": _get(s, "content"), "data": _get(s, "data") or {}}

    # 只对数值指标打分，跳过已标记 error 的段
    target = [s for s in sections if _get(s, "key") in NOTE_KEYS and not _get(s, "error")]
    if not target:
        return {}

    cached = _load_notes(code, trade_date)

    def _cache_valid(key: str) -> bool:
        # 旧格式（无 score/signal 字段）视为未命中，重新打分
        note = cached.get(key)
        return isinstance(note, dict) and "score" in note and "signal" in note

    todo = [s for s in target if not _cache_valid(_get(s, "key"))]

    if not todo:
        _log(f"📝 逐指标打分全部命中缓存（{len(target)} 项）")
        return {_get(s, "key"): cached[_get(s, "key")] for s in target}

    if not llm_call:
        _log("⚠️ 未配置 LLM，跳过逐指标打分")
        return {_get(s, "key"): cached.get(_get(s, "key"), {"score": 50, "signal": "hold", "reason": "", "impact": ""})
                for s in target}

    result = {k: v for k, v in cached.items() if _cache_valid(k)}

    if mode == "per_indicator":
        _log(f"📝 逐指标打分（单独调用，含详细分析）：{len(todo)} 项待分析（缓存命中 {len(result)}）")
        for i, s in enumerate(todo, 1):
            key = _get(s, "key")
            _log(f"  🤖 分析+打分 {i}/{len(todo)}：{_get(s, 'title')}")
            try:
                resp = llm_call(_build_single_prompt(stock_name, _as_dict(s)))
                obj = _parse_json(resp)
                note = _norm_score(obj) if obj else {"score": 50, "signal": "hold", "reason": (resp or ""), "impact": ""}
                # per_indicator 额外保留详细分析正文（去掉末尾 JSON），供 Section.content 展示
                detail = _strip_trailing_json(resp)
                if detail:
                    note["detail"] = detail
                result[key] = note
            except Exception as e:
                logger.warning("[llm_notes] 单项分析打分失败 %s/%s: %s", code, key, e)
                result[key] = {"score": 50, "signal": "hold", "reason": "分析失败", "impact": ""}
    else:  # batch
        _log(f"📝 批量打分（打包一次）：{len(todo)} 项分析（缓存命中 {len(result)}）")
        try:
            resp = llm_call(_build_batch_prompt(stock_name, [_as_dict(s) for s in todo]))
            obj = _parse_json(resp) or {}
            for s in todo:
                key = _get(s, "key")
                result[key] = _norm_score(obj.get(key)) if obj.get(key) else {"score": 50, "signal": "hold", "reason": "", "impact": ""}
        except Exception as e:
            logger.warning("[llm_notes] 批量打分失败 %s: %s", code, e)
            for s in todo:
                result[_get(s, "key")] = {"score": 50, "signal": "hold", "reason": "分析失败", "impact": ""}

    _save_notes(code, trade_date, result)
    # 只返回本次 target 涉及的
    return {_get(s, "key"): result[_get(s, "key")] for s in target if _get(s, "key") in result}


# 向后兼容别名（旧调用点）
generate_notes = score_sections
