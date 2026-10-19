# -*- coding: utf-8 -*-
"""
portal/premarket/agent.py — LLM 层：只负责「提议」，不负责「定稿」

契约
----
LLM 返回**一段 JSON**，形状固定（见 `SCHEMA_HINT`）。它能做的是判断：
趋势、题材优先级、个股逻辑、四张表的增删改建议。
它**不能**做的是：写数字结果（仓位%/盈亏%/市值）、拼 HTML、绕过上限。

为什么这么严
-----------
源包 `科斯托兰尼/.workbuddy/memory/2026-10-08.md` 记过教训：预案里写
「放量有效跌破 3800」却没定义"有效"是盘中还是收盘，结果盘中根本无法判定。
所以这里每个字段都要么是数字、要么是枚举 —— 留白的形容词一律在校验阶段打回。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

# ─────────────────────────────────────────────────────────────
# 输出 schema（写进 prompt，也用于校验）
# ─────────────────────────────────────────────────────────────
SCHEMA_HINT = """{
  "market_view": {
    "trend": "强势|震荡|弱势",              // 三选一
    "position_range_pct": [下限, 上限],     // 总仓位区间，0-100 的两个数字
    "confidence": 0-100,                    // 你对这个判断的置信度
    "reasons": ["依据1", "依据2"]           // 必须引用给定数据里的具体数字
  },
  "sector_priority": [
    {"name":"题材名","logic":"为什么优先","strength":0-100,"evidence":"引用涨停家数/龙头"}
  ],
  "pool_actions": {
    "add":     [{"code":"600000","name":"某股","tier":"core|normal","logic":"入选逻辑",
                 "reason":"买入理由","trigger":"量化触发条件(必须含价格或量能数字)",
                 "stop_loss":10.5,"take_profit":13.0}],
    "update":  [{"code":"600000","reason":"改的原因","tier":"normal","trigger":"...",
                 "stop_loss":10.2,"take_profit":13.5,"status":"tracking|ready"}],
    "remove":  [{"code":"600000","reason":"移除原因"}]
  },
  "position_plan": {
    "buy":    [{"code":"600000","name":"某股","alloc_pct":15,"trigger":"什么价位/条件下买",
                "stop_loss":10.5,"take_profit":13.0,"logic":"买入逻辑"}],
    "sell":   [{"code":"600000","reason":"卖出原因","all":true}],
    "adjust": [{"code":"600000","reason":"调整原因","stop_loss":10.8,"take_profit":13.5}]
  },
  "info_actions": {
    "add":    [{"content":"不超过50字","scope":"大盘|行业|个股","scope_detail":"具体",
                "level":"高|中|低","expires_on":"YYYY-MM-DD"}],
    "remove": [{"id":"Ixxxxxxxx","reason":"失效原因"}]
  },
  "lesson_actions": {
    "add":    [{"lesson":"可验证的规律(必须含数字或明确条件)","scenario":"具体场景",
                "result":"成功经验|失败教训","evidence":"样本/数据"}],
    "remove": [{"id":"Lxxxxxxxx","reason":"被证伪"}]
  },
  "risk_notes": ["风险点1"],
  "execution_checklist": ["开盘要做的事1"]
}"""

SYSTEM_ROLE = """你是一名严谨的 A 股策略交易研究员，负责盘前预案。你的职责边界：

1. 你只输出 **JSON**，不要任何解释文字、不要 Markdown 代码块以外的内容。
2. 你**不写数字结果**（仓位百分比结果、盈亏、市值占比都由程序计算）。你只给区间与判断。
3. 你的每一个判断都必须能对应到给定数据里的具体数字，禁止「可能」「或许」「看情况」这类
   无法执行的表述。触发条件必须是可判定的（含价格或量能数字）。
4. 决策必须按四层优先级推导，禁止跳步：
   大盘环境定总仓位 → 题材方向定优先级 → 个股选择定标的 → 操作执行定时机。
5. 逆市加仓（大盘走弱还要提高仓位）必须有极强理由，并在 reasons 里写明。
6. 若无把握，就把 position_range_pct 给窄一点、confidence 给低一点，
   不要编造数据里没有的东西。"""


# ─────────────────────────────────────────────────────────────
# LLM 调用（复用 srv 的网关；不依赖 srv 时自行兜底）
# ─────────────────────────────────────────────────────────────
def default_llm(log: Callable[[str], None] = print) -> Callable[[str], str] | None:
    """返回 `call(prompt) -> str`。优先复用 portal 既有 LLM 网关。"""
    # 命令行跑的时候进程环境是空的，key 只在 .env 里 —— 必须先加载，
    # 否则 CLI 永远走兜底路径（实测踩过）。
    try:
        from .env import has_key, load_env
        applied = load_env()
        if applied:
            log("🔑 已从 .env 加载：" + "、".join(sorted(applied)))
        elif not has_key("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                         "LITELLM_MODEL", "HAI_API_KEY"):
            log("🔑 进程环境与 .env 都没有 LLM 配置")
    except Exception as exc:
        log(f"⚠️  .env 加载失败：{type(exc).__name__}: {exc}")

    try:
        from srv.llm_gateway import _make_llm_caller
        fn = _make_llm_caller(log)
        if fn:
            return fn
    except Exception:
        pass

    # 兜底：直接 litellm（与网关同样的环境变量优先级）
    try:
        import litellm
        litellm.suppress_debug_info = True
    except Exception:
        log("⚠️  litellm 未安装，无法调用 LLM")
        return None

    model = (os.environ.get("LITELLM_MODEL")
             or ("deepseek/deepseek-chat" if os.environ.get("DEEPSEEK_API_KEY") else None)
             or ("gpt-4o-mini" if os.environ.get("OPENAI_API_KEY") else None))
    if not model:
        log("⚠️  未配置 LLM（DEEPSEEK_API_KEY / OPENAI_API_KEY / LITELLM_MODEL）")
        return None
    log(f"🤖 LLM：{model}")

    def call(prompt: str) -> str:
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=8192, timeout=180)
        return resp.choices[0].message.content or ""

    return call


# ─────────────────────────────────────────────────────────────
# JSON 抽取（LLM 经常裹代码块或加前后话）
# ─────────────────────────────────────────────────────────────
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def extract_json(text: str) -> dict:
    """从 LLM 输出里取出 JSON 对象。失败抛 ValueError（附原文尾部便于排查）。"""
    if not text or not text.strip():
        raise ValueError("LLM 返回空字符串")
    raw = text.strip()

    m = _FENCE.search(raw)
    if m:
        raw = m.group(1).strip()

    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 退化：从第一个 { 到最后一个 } 之间截取
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法从 LLM 输出解析出 JSON。原始输出尾部：…{raw[-300:]}")


# ─────────────────────────────────────────────────────────────
# 结构校验
# ─────────────────────────────────────────────────────────────
class AgentOutputError(ValueError):
    """LLM 输出不符合 schema。消息要能直接告诉用户哪里不对。"""


TRENDS = ("强势", "震荡", "弱势")


def _need(obj: dict, key: str, where: str) -> Any:
    if key not in obj:
        raise AgentOutputError(f"{where} 缺少字段 `{key}`")
    return obj[key]


def _list(obj: dict, key: str, where: str) -> list:
    v = obj.get(key, [])
    if v is None:
        return []
    if not isinstance(v, list):
        raise AgentOutputError(f"{where}.{key} 必须是数组，得到 {type(v).__name__}")
    return v


def validate(payload: dict) -> dict:
    """校验并规范化。只保留已知字段，防止 LLM 灌垃圾键。"""
    if not isinstance(payload, dict):
        raise AgentOutputError("顶层必须是 JSON 对象")

    mv = _need(payload, "market_view", "顶层")
    if not isinstance(mv, dict):
        raise AgentOutputError("market_view 必须是对象")
    trend = str(mv.get("trend", "")).strip()
    if trend not in TRENDS:
        raise AgentOutputError(f"market_view.trend 只能是 {TRENDS}，得到 {trend!r}")

    pr = mv.get("position_range_pct")
    if not (isinstance(pr, (list, tuple)) and len(pr) == 2):
        raise AgentOutputError(
            "market_view.position_range_pct 必须是 [下限, 上限] 两个数字")
    try:
        lo, hi = float(pr[0]), float(pr[1])
    except (TypeError, ValueError):
        raise AgentOutputError(f"position_range_pct 必须是数字，得到 {pr!r}") from None
    if lo > hi:
        raise AgentOutputError(f"position_range_pct 下限 {lo} 大于上限 {hi}")
    if not (0 <= lo <= 100 and 0 <= hi <= 100):
        raise AgentOutputError(f"position_range_pct 必须在 0-100，得到 [{lo}, {hi}]")

    reasons = _list(mv, "reasons", "market_view")
    if not reasons:
        raise AgentOutputError("market_view.reasons 不能为空（要给出依据）")
    # 依据里必须出现数字，否则就是「感觉」
    if not any(any(c.isdigit() for c in str(r)) for r in reasons):
        raise AgentOutputError(
            "market_view.reasons 里至少要有一条含具体数字（引用涨跌幅/家数/成交额）")

    conf = mv.get("confidence", 50)
    try:
        conf = max(0.0, min(100.0, float(conf)))
    except (TypeError, ValueError):
        conf = 50.0

    out = {
        "market_view": {
            "trend": trend,
            "position_range_pct": [round(lo, 1), round(hi, 1)],
            "confidence": round(conf, 1),
            "reasons": [str(r).strip()[:120] for r in reasons][:6],
        },
        "sector_priority": [],
        "pool_actions": {"add": [], "update": [], "remove": []},
        "position_plan": {"buy": [], "sell": [], "adjust": []},
        "info_actions": {"add": [], "remove": []},
        "lesson_actions": {"add": [], "remove": []},
        "risk_notes": [str(x).strip()[:160] for x in _list(payload, "risk_notes", "顶层")][:8],
        "execution_checklist": [str(x).strip()[:160]
                                for x in _list(payload, "execution_checklist", "顶层")][:10],
    }

    for s in _list(payload, "sector_priority", "顶层")[:8]:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name", "")).strip()
        if not name:
            continue
        try:
            strength = max(0.0, min(100.0, float(s.get("strength", 50))))
        except (TypeError, ValueError):
            strength = 50.0
        out["sector_priority"].append({
            "name": name[:20],
            "logic": str(s.get("logic", "")).strip()[:160],
            "strength": round(strength, 1),
            "evidence": str(s.get("evidence", "")).strip()[:160],
        })

    pa = payload.get("pool_actions") or {}
    for key in ("add", "update", "remove"):
        out["pool_actions"][key] = [x for x in _list(pa, key, "pool_actions")
                                    if isinstance(x, dict)][:12]

    pp = payload.get("position_plan") or {}
    for key in ("buy", "sell", "adjust"):
        out["position_plan"][key] = [x for x in _list(pp, key, "position_plan")
                                     if isinstance(x, dict)][:10]

    ia = payload.get("info_actions") or {}
    for key in ("add", "remove"):
        out["info_actions"][key] = [x for x in _list(ia, key, "info_actions")
                                    if isinstance(x, dict)][:12]

    la = payload.get("lesson_actions") or {}
    for key in ("add", "remove"):
        out["lesson_actions"][key] = [x for x in _list(la, key, "lesson_actions")
                                      if isinstance(x, dict)][:10]
    return out


# ─────────────────────────────────────────────────────────────
# Prompt 构建
# ─────────────────────────────────────────────────────────────
def build_prompt(*, snapshot: dict, market: dict, account: dict,
                 plan_date: str, trade_date: str) -> str:
    """把四张表 + 采集摘要 + 账户，拼成给 LLM 的完整 prompt。

    刻意**不**把原始新闻正文全塞进去：只给标题与时间，避免模型被长文带跑。
    """
    pool = snapshot.get("pool") or []
    positions = snapshot.get("positions") or []
    infos = snapshot.get("infos") or []
    lessons = snapshot.get("lessons") or []

    def dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=1)

    pool_view = [{"code": r.get("code"), "name": r.get("name"), "tier": r.get("tier"),
                  "logic": r.get("logic"), "trigger": r.get("trigger"),
                  "stop_loss": r.get("stop_loss"), "take_profit": r.get("take_profit"),
                  "added_date": r.get("added_date"), "status": r.get("status")}
                 for r in pool if r.get("status") != "removed"]

    pos_view = [{"code": r.get("code"), "name": r.get("name"),
                 "alloc_pct": r.get("alloc_pct"), "entry_price": r.get("entry_price"),
                 "stop_loss": r.get("stop_loss"), "take_profit": r.get("take_profit"),
                 "entry_date": r.get("entry_date"), "entry_logic": r.get("entry_logic")}
                for r in positions]

    info_view = [{"id": r.get("_id"), "content": r.get("content"),
                  "scope": r.get("scope"), "level": r.get("level"),
                  "expires_on": r.get("expires_on")}
                 for r in infos]

    lesson_view = [{"id": r.get("_id"), "lesson": r.get("lesson"),
                    "scenario": r.get("scenario"), "result": r.get("result"),
                    "recorded_on": r.get("recorded_on")}
                   for r in lessons[:20]]

    acct_view = {k: account.get(k) for k in
                 ("total_pct", "cash_pct", "market_value_pct", "position_ratio_pct",
                  "cum_pnl_ratio_pct", "n_positions")}

    return f"""{SYSTEM_ROLE}

──────────────────────────────
【本次任务】
为 {trade_date}（下一个交易日）制定盘前预案。预案日期标记 {plan_date}。

【一、大盘与市场数据（程序采集，数字可直接引用）】
{dump({k: v for k, v in market.items() if k != "candidates"})}

【二、可交易候选池（选股**只能**从这里挑，价格直接用给定值）】
{dump(market.get("candidates") or []) if market.get("candidates") else "（无候选池数据，本次不要新增个股）"}
说明：{market.get("candidates_note") or ""}

【三、当前账户（程序计算，初始 100%）】
{dump(acct_view)}

【四、模拟持仓表（{len(pos_view)} 只）】
{dump(pos_view) if pos_view else "（空仓）"}

【五、选股观察表（{len(pool_view)} 只）】
{dump(pool_view) if pool_view else "（空表）"}

【六、重要信息表（{len(info_view)} 条，盘前必须先通读）】
{dump(info_view) if info_view else "（空表）"}

【七、交易经验表（最近 {len(lesson_view)} 条，历史教训必须遵守）】
{dump(lesson_view) if lesson_view else "（空表）"}

──────────────────────────────
【硬约束（违反会被程序拒绝）】
- **选股只能来自候选池**。不在候选池里的 code 会被拒绝并记为「疑似编造」，
  因为程序无法校验它的价格，你也无法给出可信的止损止盈。
- 观察表：当日新增 ≤ 8 只，核心观察（tier=core）合计 ≤ 6 只
- 观察表每条的 trigger 必须含具体价格或量能数字；stop_loss < take_profit
- **stop_loss / take_profit 可以留空（填 0 或不填）**：内核会按候选池里的
  收盘价补全（止损 = 收盘 × 0.95，止盈 = 收盘 × 1.10）。
  如果你有更好的判断就填写，但必须满足 止损 < 现价 < 止盈 且不远离现价。
- 已在持仓的标的不能再出现在观察表；买入成交后须从观察表移除
- 单只标的仓位 alloc_pct ≤ 40；总仓位 ≤ 100
- 重要信息 content ≤ 50 字；经验必须可验证（含数字或明确条件），禁止「要控制仓位」这类空话
- 移除任何一行都必须写 reason（用户会看到原因）
- position_range_pct 必须与 trend 自洽：trend=弱势 时上限不建议超过 50

【决策框架（按顺序推导，不许跳步）】
1. 大盘环境定总仓位：趋势/流动性/情绪 → position_range_pct
2. 题材方向定优先级：政策催化强 + 资金认可 + 有龙头效应 → sector_priority
3. 个股选择定标的：**从候选池里**挑题材龙头 + 量价健康 + 有席位验证的
4. 操作执行定时机：明确触发条件与止损止盈，不追高不抄底 → execution_checklist

【输出要求】
只输出一个 JSON 对象，结构如下（不要 Markdown 代码块以外的任何文字）：
{SCHEMA_HINT}

注意：
- 数组可以为空 []，但 market_view 必填
- 所有 code 必须是 6 位数字字符串，且必须出现在候选池里
- 若某个数据源在 caveats 里标注失败，不要基于它下结论
- 候选池为空时，pool_actions 与 position_plan.buy 必须为空数组，不要凭想象选股
"""


# ─────────────────────────────────────────────────────────────
# 提议
# ─────────────────────────────────────────────────────────────
def propose(*, snapshot: dict, market: dict, account: dict,
            plan_date: str, trade_date: str,
            llm_call: Callable[[str], str] | None = None,
            log: Callable[[str], None] = print,
            raw_sink: list[str] | None = None) -> dict:
    """调 LLM 并返回**已校验**的提议。失败抛异常，由 pipeline 决定如何降级。"""
    call = llm_call or default_llm(log)
    if call is None:
        raise RuntimeError(
            "没有可用的 LLM。请配置 DEEPSEEK_API_KEY / OPENAI_API_KEY / "
            "LITELLM_MODEL，或在设置页填好模型后再生成。")

    prompt = build_prompt(snapshot=snapshot, market=market, account=account,
                          plan_date=plan_date, trade_date=trade_date)
    log(f"🤖 调用 LLM（prompt {len(prompt)} 字符）…")
    text = call(prompt)
    if raw_sink is not None:
        raw_sink.append(text)
    log(f"   LLM 返回 {len(text)} 字符，解析 JSON…")

    payload = extract_json(text)
    clean = validate(payload)
    log(f"   校验通过：趋势={clean['market_view']['trend']} "
        f"仓位区间={clean['market_view']['position_range_pct']} "
        f"题材={len(clean['sector_priority'])} 个股计划={len(clean['position_plan']['buy'])}")
    return clean


__all__ = ["propose", "build_prompt", "validate", "extract_json",
           "default_llm", "AgentOutputError", "SCHEMA_HINT", "SYSTEM_ROLE", "TRENDS"]
