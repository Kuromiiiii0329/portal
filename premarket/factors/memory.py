# -*- coding: utf-8 -*-
"""
portal/premarket/factors/memory.py — 因子会议的记忆与经验进化

三层存储，职责分开
------------------
1. `factors_ledger.jsonl`   事件流水（轻量）：提议 / 评审 / 入库 / 淘汰 / 周检
2. `factors_memory.jsonl`   **完整会议记录**：候选的源码、批评者逐条意见、内核数字
3. `factor_lessons.jsonl`   **提炼出的经验**（append-only 事件流，状态靠重放）

为什么必须分开
--------------
只记"入库/淘汰"这类结论，下次开会学不到任何东西 —— 你只知道某次失败了，
不知道为什么、也没法把它变成下一次的约束。所以要把**讨论内容**留下，
再从中提炼成可复用的规律，并且在下次开会时**注入到提示词里**，形成闭环：

    开会 → 记录全过程 → 提炼经验 → 注入下次提示词 → 行为改变 → 再记录

经验条目带 `evidence` 计数：同一个坑踩得越多，权重越高，越靠前注入。
LLM 也会参与提炼（`source="llm"`），但**内核规则永远优先且不可被 LLM 删除**。
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

_DATA = Path(__file__).parents[2] / "data" / "premarket"
MEMORY = _DATA / "factors_memory.jsonl"
LESSONS = _DATA / "factor_lessons.jsonl"
LEDGER = _DATA / "factors_ledger.jsonl"

#: 内核规则：拒绝原因关键词 → (经验 key, 经验正文)
#: 这就是"从失败里提炼规律"的确定性部分，不依赖 LLM。
KERNEL_RULES: list[tuple[str, str, str]] = [
    ("与已有因子完全等价", "no_duplicate_expr",
     "不要提出与已有因子等价的表达式：直接写 chg_today / vol_ratio 这类已有因子名，"
     "或只搬运 close/volume 等原始字段，都会被判重复。要提就必须是新的组合或加工。"),
    ("与现有因子重复", "no_duplicate_id",
     "因子 id 不能与现有因子重名；先看清单再起名。"),
    ("搬运原始字段", "no_raw_field_passthrough",
     "只返回 close/volume/prev_close 这类原始字段没有信息增量，必须做加工或比较。"),
    ("属于追高打板类", "no_chase_definition",
     "因子与战法都不得含涨停/连板/封板/追涨语义，也不得用 chg_today >= 7 作买入条件。"
     "这是硬约束，会被内核直接拒，不要浪费一次提议。"),
    ("属于追高", "no_chase_threshold",
     "买入条件里的涨幅阈值必须 < 7%，7% 以上属追高。"),
    ("语法审查不通过", "sandbox_syntax",
     "代码因子只能写算术、for、if、字典/列表操作；禁止 import/class/while/lambda/try/"
     "with/global，禁止任何以 _ 开头的属性，禁止 while 死循环。"),
    ("只算出", "need_coverage",
     "因子必须能对全市场大多数股票算出值（覆盖率 ≥ 50%）。"
     "依赖当日开高低价的因子，要处理缺失（有的停牌股没有 high/low）。"),
    ("|IC|=", "need_backtest_support",
     "入库要求 |IC| ≥ 0.02 且 |IR| ≥ 0.20。系数、阈值都要能被回测支持 —— "
     "拍脑袋的权重（例如把量比乘以 10）通常过不了 IR，宁可简单。"),
    ("检验未通过", "need_enough_days",
     "回测天数不足时无法判定，不要因为天数少而调低标准；换更长的窗口再测。"),
]

#: 通过侧的观察（正向经验）
ACCEPT_KEY = "accepted_pattern"
ACCEPT_TEXT = ("通过内核检验的因子通常：单一经济含义、条件数 ≤ 6、"
               "参数是整数或简单分位、且能被一句话解释为什么预测上涨。")


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _slug(text: str) -> str:
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"L{h}"


# ─────────────────────────────────────────────────────────────
# 1. 完整会议记录
# ─────────────────────────────────────────────────────────────
def save_transcript(rec: dict) -> dict:
    """保存一次会议的完整内容（候选源码 + 批评意见 + 内核数字）。"""
    body = {"at": _now(), **rec}
    _append(MEMORY, body)
    return body


def transcripts(tail: int = 5) -> list[dict]:
    return _read(MEMORY)[-tail:]


# ─────────────────────────────────────────────────────────────
# 2. 经验库（事件流：add / evidence / retire）
# ─────────────────────────────────────────────────────────────
def add_lesson(key: str, text: str, *, kind: str = "rule",
               source: str = "kernel", tags: Iterable[str] = (),
               note: str = "") -> dict:
    """新增或累加一条经验。同 key 不会重复建条目，只会 evidence+1。"""
    for cur in lessons():
        if cur["key"] == key:
            return record_evidence(cur["id"], note=note)
    lid = _slug(key)
    ev = {"op": "add", "id": lid, "key": key, "at": _now(),
          "kind": kind, "text": text, "source": source,
          "tags": list(tags), "note": note[:200]}
    _append(LESSONS, ev)
    return ev


def record_evidence(lesson_id: str, *, delta: int = 1, note: str = "") -> dict:
    ev = {"op": "evidence", "id": lesson_id, "at": _now(),
          "delta": delta, "note": note[:200]}
    _append(LESSONS, ev)
    return ev


def retire_lesson(lesson_id: str, *, reason: str) -> dict:
    ev = {"op": "retire", "id": lesson_id, "at": _now(), "reason": reason[:200]}
    _append(LESSONS, ev)
    return ev


def lessons(*, include_retired: bool = False) -> list[dict]:
    """重放事件流，得到当前经验库（按 evidence 降序）。"""
    state: dict[str, dict] = {}
    for ev in _read(LESSONS):
        op = ev.get("op")
        if op == "add":
            state[ev["id"]] = {"id": ev["id"], "key": ev.get("key"),
                               "kind": ev.get("kind") or "rule",
                               "text": ev.get("text") or "",
                               "source": ev.get("source") or "kernel",
                               "tags": ev.get("tags") or [],
                               # 首次出现就算踩过 1 次；否则「被拒 N 次」会少算一次
                               "evidence": 1, "retired": False,
                               "created_at": ev.get("at"),
                               "notes": []}
        elif op == "evidence" and ev["id"] in state:
            state[ev["id"]]["evidence"] += int(ev.get("delta") or 1)
            if ev.get("note"):
                state[ev["id"]]["notes"].append(ev["note"][:120])
        elif op == "retire" and ev["id"] in state:
            state[ev["id"]]["retired"] = True
            state[ev["id"]]["retire_reason"] = ev.get("reason")
    out = [v for v in state.values() if include_retired or not v["retired"]]
    out.sort(key=lambda x: (-x["evidence"], x["created_at"] or ""))
    return out


def prompt_block(*, max_n: int = 12) -> str:
    """把经验渲染成提示词片段。这是"自我迭代"真正生效的地方。"""
    items = lessons()[:max_n]
    if not items:
        return ""
    rules = [x for x in items if x["kind"] == "rule"]
    obs = [x for x in items if x["kind"] != "rule"]
    lines = ["【历史经验（来自既往因子会议）】",
             "下面是以前踩过的坑与验证过的规律，**提因子时必须避开**："]
    for x in rules:
        hit = f"（被拒 {x['evidence']} 次）" if x["evidence"] else ""
        lines.append(f"- {x['text']}{hit}")
    if obs:
        lines.append("【正面观察】")
        for x in obs[:5]:
            lines.append(f"- {x['text']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 3. 从一次会议里提炼经验
# ─────────────────────────────────────────────────────────────
def distill(meeting: dict) -> list[dict]:
    """把会议结果翻译成经验条目（确定性规则，不花 token）。"""
    fresh: list[dict] = []
    results = meeting.get("results") or []
    for r in results:
        reason = str(r.get("reason") or "")
        if r.get("ok"):
            ev = add_lesson(ACCEPT_KEY, ACCEPT_TEXT, kind="observation",
                            source="kernel", tags=["accepted"],
                            note=str(r.get("id")))
            fresh.append(ev)
            continue
        if r.get("stage") == "critic":
            # 批评者否决但内核没跑到：记为观察，权重低
            ev = add_lesson("critic_veto",
                            "批评者明确否决的候选不要再原样提交；"
                            "先按它给的建议改一版再说。",
                            kind="rule", source="kernel",
                            tags=["critic"], note=str(r.get("id")))
            fresh.append(ev)
            continue
        matched = False
        for kw, key, text in KERNEL_RULES:
            if kw in reason:
                ev = add_lesson(key, text, kind="rule", source="kernel",
                                tags=["rejected"], note=str(r.get("id")))
                fresh.append(ev)
                matched = True
                break
        if not matched:
            ev = add_lesson("other_rejection",
                            "被拒但原因不在已知分类里，需要人工看一眼会议记录。",
                            kind="observation", source="kernel",
                            tags=["unknown"], note=reason[:120])
            fresh.append(ev)
    return fresh


def distill_by_llm(meeting: dict, llm, *, max_n: int = 3) -> list[dict]:
    """让模型从会议记录里提炼可复用规律（补充确定性规则的不足）。"""
    props = meeting.get("proposals") or []
    reviews = meeting.get("reviews") or {}
    results = meeting.get("results") or []
    payload = {
        "proposals": [{k: p.get(k) for k in ("id", "name", "hypothesis", "expr")}
                      for p in props],
        "reviews": reviews,
        "results": [{k: r.get(k) for k in ("id", "ok", "reason")} for r in results],
    }
    prompt = (
        "你是因子研究的方法论审阅者。下面是一次因子会议的记录（提议、批评、"
        "内核回测结果）。请提炼**可复用到下一次会议的规律**，而不是复述这次发生了什么。\n"
        "要求：每条一句话、可执行、能指导「下次该写什么 / 不该写什么」；"
        "不要写「这次某因子失败了」这种一次性结论。\n"
        "只输出 JSON：{\"lessons\": [\"...\", \"...\"]}\n\n"
        + json.dumps(payload, ensure_ascii=False)[:8000])
    try:
        text = llm(prompt) or ""
        i, j = text.find("{"), text.rfind("}")
        data = json.loads(text[i:j + 1]) if i >= 0 and j > i else {}
    except Exception:
        return []
    out = []
    for t in (data.get("lessons") or [])[:max_n]:
        t = str(t).strip()
        if len(t) < 8:
            continue
        out.append(add_lesson(f"llm:{_slug(t)}", t[:300], kind="rule",
                              source="llm", tags=["distilled"]))
    return out


def stats() -> dict[str, Any]:
    ls = lessons()
    return {
        "transcripts": len(_read(MEMORY)),
        "lessons_total": len(lessons(include_retired=True)),
        "lessons_active": len(ls),
        "rules": sum(1 for x in ls if x["kind"] == "rule"),
        "observations": sum(1 for x in ls if x["kind"] != "rule"),
        "by_source": {s: sum(1 for x in ls if x["source"] == s)
                      for s in {x["source"] for x in ls}} if ls else {},
        "top": [{"key": x["key"], "evidence": x["evidence"],
                 "text": x["text"][:60]} for x in ls[:5]],
    }


__all__ = ["MEMORY", "LESSONS", "LEDGER", "KERNEL_RULES",
           "save_transcript", "transcripts", "add_lesson", "record_evidence",
           "retire_lesson", "lessons", "prompt_block", "distill",
           "distill_by_llm", "stats"]
