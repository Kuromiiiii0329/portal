# -*- coding: utf-8 -*-
"""
portal/virtual/toolbox.py — 暴露给 Agent 的四个工具

设计原则（UPGRADE_PLAN.md §L4）：**Agent 不计算任何数字。**
这四把工具把「能做的事」限死在可审计的动作上，其余一律走内核。

| 工具 | 作用 | 为什么必须走它 |
|---|---|---|
| `freeze_virtual_plan` | 校验 → 落盘 → 锚定 | 冻结后再改，结算会中止 |
| `settle_virtual_plan` | 机械回放 → fills → 账本 | 成交由算法判定，不许人工叙述 |
| `backtest_virtual_rule` | 单条规则在历史特征上取证据 + 过闸门 | 不经回测的规则是负期望的默认值 |
| `query_virtual_memory` | 查规则 / 特征 / 预测 / 事故 | 只读 |

**禁止清单**（既是文档也是实现约束）：

    禁止 1  自己计算任何收益率/胜率/净值 —— 必须调用内核
    禁止 2  修改 rules.jsonl 的 status —— 只有 gate.py / decay.py 能改
    禁止 3  在结算前「解释」未结算的结果
    禁止 4  用自然语言改写计划里的参数
    禁止 5  把同步指标写成预测结论

工具返回统一的 dict：`{"ok": bool, ...}`。失败也必须返回 `ok=False` + `error`，
而不是抛异常给 Agent 猜 —— 让失败可被叙述、可被审计。
"""
from __future__ import annotations

import json
from typing import Any, Callable

# ── 尽量复用 agent 框架的 schema 定义；拿不到就用本地的等价物 ──
try:  # pragma: no cover - 依赖 lib/src 是否在 sys.path 上
    from src.agent.tools.registry import ToolDefinition, ToolParameter  # type: ignore
    _HAS_FRAMEWORK = True
except Exception:  # pragma: no cover
    _HAS_FRAMEWORK = False

    class ToolParameter:  # type: ignore
        def __init__(self, name: str, type: str, description: str,
                     required: bool = True, enum: list[str] | None = None,
                     default: Any = None):
            self.name, self.type, self.description = name, type, description
            self.required, self.enum, self.default = required, enum, default

    class ToolDefinition:  # type: ignore
        def __init__(self, name: str, description: str,
                     parameters: list, handler: Callable, category: str = "data"):
            self.name, self.description = name, description
            self.parameters, self.handler, self.category = parameters, handler, category


CATEGORY = "action"


# ─────────────────────────────────────────────────────────────
# 1. 冻结计划
# ─────────────────────────────────────────────────────────────
def freeze_virtual_plan(plan_json: Any, note: str = "") -> dict[str, Any]:
    """校验并冻结一份计划（落盘 + 锚定）。参数写错会被拒，不会静默开始。"""
    from . import plan as plan_mod

    try:
        payload = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
        if not isinstance(payload, dict):
            return {"ok": False, "error": "plan_json 必须是 JSON 对象"}
        problems = plan_mod.validate(payload)
        if problems:
            return {"ok": False, "error": "计划非法", "problems": problems}
        frozen = plan_mod.freeze(payload, note=note)
        return {
            "ok": True,
            "plan_id": frozen["plan_id"],
            "params_hash": frozen["params_hash"],
            "params_frozen_at": frozen.get("params_frozen_at"),
            "plan_sha256": (frozen.get("_anchor") or {}).get("plan_sha256"),
            "next": f'结算：settle_virtual_plan(plan_id="{frozen["plan_id"]}")',
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ─────────────────────────────────────────────────────────────
# 2. 结算
# ─────────────────────────────────────────────────────────────
def settle_virtual_plan(plan_id: str, mode: str = "buy", dry: bool = False) -> dict[str, Any]:
    """机械回放结算。锚点不符 / 日期不符一律中止，且**不写账本**。"""
    from . import settle as settle_mod

    try:
        if mode == "exit":
            r = settle_mod.settle_exit(plan_id, dry=dry)
            return {
                "ok": True, "mode": "exit", "plan_id": plan_id,
                "realized": r["realized"], "realized_pct": r["realized_pct"],
                "close_total_asset": r["close_total_asset"],
                "rows": r["rows"],
            }
        r = settle_mod.settle_buy(plan_id, dry=dry)
        return {
            "ok": True, "mode": "buy", "plan_id": plan_id,
            "gate": r["gate"],
            "invested": r["position"]["invested"],
            "total_asset": r["total_asset"],
            "unrealized": r["unrealized"],
            "rulers": r["rulers"],
            "fills": r["fills"],
        }
    except Exception as exc:
        # SettleAbort 也要以 ok=False 返回：让 Agent 能叙述「为什么没结算」
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "written": False}


# ─────────────────────────────────────────────────────────────
# 3. 规则回测 + 闸门
# ─────────────────────────────────────────────────────────────
def backtest_virtual_rule(
    expr_json: Any,
    horizon: int = 1,
    dates: list[str] | None = None,
) -> dict[str, Any]:
    """在历史特征上评估一条规则，并跑 G1–G5 闸门。

    数据来源：`data/virtual/features/` + portal 的 K 线缓存。
    取不到足够样本时返回 `ok=False` 并说明缺什么 —— **不许拿空气当证据**。
    """
    from . import dataset, evolve, gate

    try:
        expr = json.loads(expr_json) if isinstance(expr_json, str) else expr_json
        from . import expr as expr_mod
        problems = expr_mod.validate(expr)
        if problems:
            return {"ok": False, "error": "表达式非法", "problems": problems}

        rows, stats, meta = dataset.load_rows(dates, horizon=horizon)
        meta["rows"] = len(rows)
        meta["drop_stats"] = stats
        if len(rows) < gate.MIN_SAMPLES:
            return {
                "ok": False,
                "error": (f"可用样本仅 {len(rows)} 条，不足 {gate.MIN_SAMPLES} 条，"
                          f"无法给出任何统计结论"),
                "meta": meta,
                "hint": ("先积累特征：python -m portal.virtual.tagging 或在日报流程里生成；"
                         "同时需要该股在本地 K 线缓存中"),
            }

        baseline = sum(float(r["ret_pct"]) for r in rows) / len(rows)
        # 单条规则的 p 值没有多重比较问题，但仍要走同一个证据口径
        ev = evolve.evaluate_condition(rows, expr, baseline_mean=baseline)
        p = ev.pop("_p", None)
        if p is not None:
            ev["bh_fdr_p"] = round(p, 6)   # 单条检验：BH 校正因子为 1
        verdict = gate.evaluate(ev)
        return {
            "ok": True,
            "expr": expr,
            "evidence": ev,
            "gate": verdict.to_dict(),
            "meta": meta,
            "note": "单条规则无多重比较；批量候选请走 evolve.run() 以做 BH 校正",
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ─────────────────────────────────────────────────────────────
# 4. 记忆查询
# ─────────────────────────────────────────────────────────────
def query_virtual_memory(kind: str = "rules", limit: int = 20,
                         status: str | None = None) -> dict[str, Any]:
    """只读查询。kind ∈ rules | rejected | predictions | features | anchors。"""
    from . import anchor, paths, rules as rules_mod

    try:
        limit = max(1, min(int(limit), 500))
        if kind == "rules":
            rows = rules_mod.summary_table()
            if status:
                rows = [r for r in rows if r["status"] == status]
            return {"ok": True, "kind": kind, "count": len(rows), "rows": rows[:limit]}
        if kind == "rejected":
            rows = rules_mod.rejected_records()
            return {"ok": True, "kind": kind, "count": len(rows),
                    "rows": [{k: v for k, v in r.items() if k != "evidence"}
                             for r in rows[-limit:]]}
        if kind == "predictions":
            rows: list[dict] = []
            if paths.PREDICTIONS.exists():
                for line in paths.PREDICTIONS.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            return {"ok": True, "kind": kind, "count": len(rows), "rows": rows[-limit:]}
        if kind == "features":
            from . import tagging
            days = tagging.list_days()
            return {"ok": True, "kind": kind, "count": len(days), "rows": days[-limit:]}
        if kind == "anchors":
            d = anchor.load()
            return {"ok": True, "kind": kind, "count": len(d.get("order", [])),
                    "head": d.get("head"),
                    "rows": [{"plan_id": p, "anchored_at": d["records"][p]["anchored_at"]}
                             for p in d.get("order", [])[-limit:]]}
        return {"ok": False, "error": f"未知 kind：{kind!r}"
                                      f"（可选 rules/rejected/predictions/features/anchors）"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ─────────────────────────────────────────────────────────────
# 工具清单
# ─────────────────────────────────────────────────────────────
FORBIDDEN = (
    "自己计算任何收益率/胜率/净值 —— 必须调用内核",
    "修改 rules.jsonl 的 status —— 只有 gate.py / decay.py 能改",
    "在结算前解释未结算的结果",
    "用自然语言改写计划里的参数",
    "把同步指标写成预测结论",
)


def build_tools() -> list:
    """构造四个 ToolDefinition。未装 agent 框架时用本文件的等价实现。"""
    return [
        ToolDefinition(
            name="freeze_virtual_plan",
            description=("校验并冻结一份虚拟盘计划（落盘 + 参数锚定）。"
                         "冻结后同一 plan_id 不允许再改；规则变更必须换新 plan_id。"),
            parameters=[
                ToolParameter("plan_json", "object",
                              "完整计划对象，字段见 portal/virtual/plan.py 的 validate()",
                              required=True),
                ToolParameter("note", "string", "冻结备注", required=False, default=""),
            ],
            handler=freeze_virtual_plan,
            category=CATEGORY,
        ),
        ToolDefinition(
            name="settle_virtual_plan",
            description=("用真实行情机械回放结算：判定成交、算当日盈亏、跑三把尺子。"
                         "锚点或日期不符会中止且不写账本。"),
            parameters=[
                ToolParameter("plan_id", "string", "计划 id（如 20261008）", required=True),
                ToolParameter("mode", "string", "buy=买入日 / exit=平仓日",
                              required=False, enum=["buy", "exit"], default="buy"),
                ToolParameter("dry", "boolean", "只打印判定过程，不写账本",
                              required=False, default=False),
            ],
            handler=settle_virtual_plan,
            category=CATEGORY,
        ),
        ToolDefinition(
            name="backtest_virtual_rule",
            description=("在历史特征上评估一条规则并跑 G1–G5 闸门。"
                         "返回的证据可直接用于 gate.promote()。"),
            parameters=[
                ToolParameter("expr_json", "object", "规则表达式（白名单语法树）", required=True),
                ToolParameter("horizon", "integer", "持有交易日数", required=False, default=1),
            ],
            handler=backtest_virtual_rule,
            category="analysis",
        ),
        ToolDefinition(
            name="query_virtual_memory",
            description="只读查询规则/拒绝记录/预测/特征日期/锚点链。",
            parameters=[
                ToolParameter("kind", "string", "查询对象",
                              required=False, default="rules",
                              enum=["rules", "rejected", "predictions", "features", "anchors"]),
                ToolParameter("limit", "integer", "返回条数上限", required=False, default=20),
                ToolParameter("status", "string", "按状态过滤（仅 kind=rules）",
                              required=False),
            ],
            handler=query_virtual_memory,
            category="data",
        ),
    ]


def register(registry: Any) -> dict[str, Any]:
    """把四个工具注册进 agent 框架的 ToolRegistry（若可用）。"""
    tools = build_tools()
    if registry is None or not hasattr(registry, "register"):
        return {"ok": False, "error": "registry 没有 register() 方法", "registered": 0}
    n = 0
    for t in tools:
        registry.register(t)
        n += 1
    return {"ok": True, "registered": n, "names": [t.name for t in tools],
            "framework": _HAS_FRAMEWORK}


def manifest() -> dict[str, Any]:
    """JSON 形式的工具清单（给不支持 ToolRegistry 的调用方用）。"""
    out = []
    for t in build_tools():
        out.append({
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "parameters": [
                {"name": p.name, "type": p.type, "description": p.description,
                 "required": p.required, "enum": getattr(p, "enum", None),
                 "default": getattr(p, "default", None)}
                for p in t.parameters
            ],
        })
    return {"tools": out, "forbidden": list(FORBIDDEN)}


__all__ = [
    "CATEGORY",
    "FORBIDDEN",
    "freeze_virtual_plan",
    "settle_virtual_plan",
    "backtest_virtual_rule",
    "query_virtual_memory",
    "build_tools",
    "register",
    "manifest",
]
