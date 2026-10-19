# -*- coding: utf-8 -*-
"""
portal/virtual/plan.py — 盘前计划（规则冻结的唯一载体）

计划不是「明天买什么」的清单，而是**规则被冻结下来的合同**。它必须在开盘前写完，
开盘后一个字都不许动；次日由 `settle.py` 机械执行，不许人工叙述。

为什么这么设计（原包的机制约束，逐条保留）：

1. **可以不买**。空仓是合法结果，不是失败。计划里必须有明确空仓出口（`open_gate`）。
2. **最多 3 只**。且三只分属不同逻辑链。
3. **用户不操作模拟盘**。所有成交由引擎用次日真实行情**机械回放**判定。
4. 复盘必须产出：当时会不会买入、当日盈亏、当前总资产。

`params_hash` 的定位
--------------------
它只做**自我一致性**校验（plan 里参数变了 hash 就变），**不是防作弊**。
防作弊靠外置哈希链（`anchor.py`）——plan 内部的哈希谁改谁重算，等于没有锁。
这一点必须写在 schema 里，避免有人误以为有了 params_hash 就安全了。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from . import anchor, paths

PLAN_SCHEMA_VERSION = 1

#: 参与 params_hash 的字段（顺序无关，内部会排序）。
#: 规则就是这些；改其中任何一个都必须重算 hash 并**换新 plan_id**。
PARAM_FIELDS = ("open_gate", "picks", "backup", "fill_rule", "exit_rule", "position_sizing")

REQUIRED_TOP = ("plan_id", "trade_date", "data_date", "initial_asset", "picks")


class PlanError(ValueError):
    """计划不合法（schema / 前后矛盾）。"""


# ─────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────
def round_price(value: float | None) -> float | None:
    """价格四舍五入到 0.01，**必须** ROUND_HALF_UP（交易所口径）。"""
    if value is None:
        return None
    return float(Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_pct_for(code: str) -> float:
    c = str(code)
    if c.startswith(("688", "689", "300", "301")):
        return 0.20
    if c.startswith(("8", "4", "9")):
        return 0.30
    return 0.10


def limit_prices(prev_close: float, code: str) -> tuple[float, float]:
    """(涨停价, 跌停价)。半进位修正，避免 .xx5 边界静默漏判。"""
    lim = limit_pct_for(code)
    up = round_price(float(prev_close) * (1 + lim))
    down = round_price(float(prev_close) * (1 - lim))
    return float(up), float(down)


def _canonical(obj: Any) -> Any:
    """把结构规范化成可稳定 hash 的形式（排序键、去 float 噪声）。"""
    if isinstance(obj, dict):
        return {k: _canonical(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return round(obj, 6)
    return obj


def params_hash(plan: dict) -> str:
    """对规则子集做 sha256（16 位前缀）。

    注意：这是**自我认证**，不是防作弊 —— 见模块 docstring。
    """
    subset = {k: _canonical(plan.get(k)) for k in PARAM_FIELDS}
    blob = json.dumps(subset, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def plan_sha256(plan: dict) -> str:
    """整份 plan 的 sha256（与 anchor.freeze 写进链里的值同源）。"""
    blob = json.dumps(_canonical(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────
# 校验
# ─────────────────────────────────────────────────────────────
def validate(plan: dict) -> list[str]:
    """返回问题清单；空 = 合法。"""
    p: list[str] = []
    if not isinstance(plan, dict):
        return ["plan 必须是对象"]

    for k in REQUIRED_TOP:
        if k not in plan:
            p.append(f"缺少必填字段 {k}")

    picks = plan.get("picks") or []
    if not isinstance(picks, list):
        p.append("picks 必须是列表")
        picks = []
    if len(picks) > 3:
        p.append(f"picks 最多 3 只，实际 {len(picks)}")

    for i, pick in enumerate(picks):
        tag = f"picks[{i}]"
        if not isinstance(pick, dict):
            p.append(f"{tag} 必须是对象")
            continue
        for k in ("code", "prev_close", "target_amount"):
            if k not in pick:
                p.append(f"{tag} 缺少 {k}")
        pc = pick.get("prev_close")
        lu = pick.get("limit_up")
        if pc is not None and lu is not None:
            code = str(pick.get("code") or "")
            want_up, want_down = limit_prices(pc, code)
            if lu is not None and abs(float(lu) - want_up) > 0.005:
                p.append(f"{tag} limit_up={lu} 与 昨收×幅度={want_up} 不符（会导致止盈档失效）")
            ld = pick.get("limit_down")
            if ld is not None and abs(float(ld) - want_down) > 0.005:
                p.append(f"{tag} limit_down={ld} 与 昨收×幅度={want_down} 不符")

        # 止盈档必须 <= 次日涨停价：超过涨停价的止盈单当天根本不可能成交
        for tp in ("tp1", "tp2"):
            v = pick.get(tp)
            if v is not None and lu is not None and float(v) > float(lu) + 0.005:
                p.append(f"{tag} {tp}={v} 高于涨停价 {lu}，当天不可能成交")

        # 买入价必须 > 止损位，否则买入即触发止损、规则自相矛盾
        stop = pick.get("stop")
        if stop is not None and pc is not None and float(stop) >= float(pc):
            p.append(f"{tag} stop={stop} 不低于昨收 {pc}，买入即触发止损")

    gate = plan.get("open_gate") or {}
    if not isinstance(gate, dict) or not gate.get("conditions"):
        p.append("open_gate.conditions 不能为空（必须显式写空仓出口）")
    else:
        for j, cond in enumerate(gate["conditions"]):
            if cond.get("type") not in ("index_auction", "pick_auction_band", "manual"):
                p.append(f"open_gate.conditions[{j}].type 未知：{cond.get('type')!r}")
            if cond.get("type") == "manual" and "default" not in cond:
                p.append(f"open_gate.conditions[{j}] 是人工项但未声明 default")

    fr = plan.get("fill_rule") or {}
    for k in ("fill_method", "fee", "max_chase_pct"):
        if k not in fr:
            p.append(f"fill_rule 缺少 {k}")
    if isinstance(fr.get("fee"), dict):
        for k in ("commission_rate", "commission_min", "stamp_rate", "transfer_rate"):
            if k not in fr["fee"]:
                p.append(f"fill_rule.fee 缺少 {k}")

    er = plan.get("exit_rule") or {}
    if er:
        # ★ 不许依赖默认值：所有开关位必须显式写全（SKILL §9.7.3 的事故）
        for k in ("price_basis", "use_stop", "z1"):
            if k not in er:
                p.append(f"exit_rule 缺少显式开关 {k}（默认值不是中性选项，它是隐形的规则）")

    if not plan.get("params_version"):
        p.append("缺少 params_version")

    return p


def require_valid(plan: dict) -> None:
    problems = validate(plan)
    if problems:
        raise PlanError("计划非法：" + "；".join(problems))


# ─────────────────────────────────────────────────────────────
# 构造
# ─────────────────────────────────────────────────────────────
def build_plan(
    *,
    plan_id: str,
    trade_date: str,
    data_date: str,
    initial_asset: float,
    picks: list[dict],
    open_gate: dict,
    fill_rule: dict,
    exit_rule: dict,
    position_sizing: dict | None = None,
    backup: list[dict] | None = None,
    excluded: list[dict] | None = None,
    baseline: dict | None = None,
    shadow: dict | None = None,
    known_bias: list[str] | None = None,
    params_version: str = "v1.0",
    note: str = "",
) -> dict:
    """组装一份 plan（不落盘、不锚定）。"""
    plan: dict[str, Any] = {
        "plan_id": str(plan_id),
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "trade_date": str(trade_date),
        "data_date": str(data_date),
        "initial_asset": float(initial_asset),
        "open_gate": open_gate,
        "picks": picks,
        "backup": backup or [],
        "fill_rule": fill_rule,
        "exit_rule": exit_rule,
        "position_sizing": position_sizing or {},
        "excluded": excluded or [],
        "baseline": baseline or {},
        "shadow": shadow or {},
        "known_bias": known_bias or [],
        "params_version": str(params_version),
        "params_frozen_at": None,
        "params_hash": None,
        "note": note,
    }
    plan["params_hash"] = params_hash(plan)
    require_valid(plan)
    return plan


def freeze(plan: dict, *, frozen_at: str | None = None, note: str = "") -> dict:
    """把 plan 写到 `data/virtual/plans/`，然后锚定。

    顺序很重要：**先落盘，再锚定**。锚点校验的是磁盘上那份文件的哈希。
    """
    import datetime

    require_valid(plan)
    frozen_at = frozen_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    plan = dict(plan)
    plan["params_frozen_at"] = frozen_at
    plan["params_hash"] = params_hash(plan)      # 冻结时刻是元数据，不进 hash，但重算一次保持一致
    require_valid(plan)

    paths.ensure_dirs()
    p = paths.plan_path(plan["plan_id"])
    if p.exists():
        existing = json.loads(p.read_text(encoding="utf-8"))
        if existing.get("params_hash") != plan["params_hash"]:
            raise PlanError(
                f"plan {plan['plan_id']} 已存在且参数不同。"
                f"规则变更必须用新 plan_id 重开一轮，不许原地改。"
            )
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    rec = anchor.freeze(plan["plan_id"], note=note)
    plan["_anchor"] = {
        "plan_sha256": rec["plan_sha256"],
        "chain_sha256": rec["chain_sha256"],
        "anchored_at": rec["anchored_at"],
    }
    return plan


# ─────────────────────────────────────────────────────────────
# 读
# ─────────────────────────────────────────────────────────────
def load(plan_id: str) -> dict | None:
    p = paths.plan_path(plan_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_plans() -> list[str]:
    if not paths.PLANS_DIR.exists():
        return []
    return sorted(p.stem.replace("plan_", "") for p in paths.PLANS_DIR.glob("plan_*.json"))


def verify(plan_id: str) -> tuple[bool, str]:
    """锚点校验（结算前必调）。"""
    plan = load(plan_id)
    if plan is None:
        return False, f"找不到 plan {plan_id}"
    return anchor.verify(plan_id, plan)


# ─────────────────────────────────────────────────────────────
# 仓位分档
# ─────────────────────────────────────────────────────────────
def position_for_gate(plan: dict, gate_passed: int, gate_total: int) -> tuple[int, float]:
    """按 gate 得分给出 (每只金额, 总仓位百分比)。

    分档来自 plan 里冻结的表，**不许运行时另算** —— 那等于事后调参。
    未配置分档时返回 (0, 0.0)：宁可空仓，也不猜一个仓位。
    """
    tiers = (plan.get("position_sizing") or {}).get("tiers") or []
    if not tiers:
        return 0, 0.0

    def _ratio(spec: str) -> float:
        try:
            a, b = str(spec).split("/")
            return float(a) / float(b) if float(b) else 0.0
        except (ValueError, ZeroDivisionError):
            return -1.0

    target = gate_passed / gate_total if gate_total else 0.0
    best: tuple[int, float] | None = None
    for tier in tiers:
        spec = str(tier.get("gate_pass", ""))
        if "/" in spec and spec.strip()[0].isdigit():
            need = _ratio(spec)
        elif spec.startswith("<="):
            need = _ratio(spec[2:].strip())
        else:
            continue
        if need < 0:
            continue
        if target >= need - 1e-9:
            cand = (int(tier.get("per_pick") or 0), float(tier.get("total_pct") or 0.0))
            if best is None or cand[1] > best[1]:
                best = cand
    return best if best else (0, 0.0)


__all__ = [
    "PLAN_SCHEMA_VERSION",
    "PARAM_FIELDS",
    "PlanError",
    "round_price",
    "limit_pct_for",
    "limit_prices",
    "params_hash",
    "plan_sha256",
    "validate",
    "require_valid",
    "build_plan",
    "freeze",
    "load",
    "list_plans",
    "verify",
    "position_for_gate",
]
