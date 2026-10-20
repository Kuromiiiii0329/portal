# -*- coding: utf-8 -*-
"""
portal/premarket/apply.py — 内核裁决：LLM 提议 → 写进表或被拒绝

职责边界
--------
LLM 说「加这三只、减那只、仓位 30-50%」。这里逐条裁决：

- 能算的（上限、区间、价格合法性）由程序判断，**不与 LLM 商量**
- 每条提议都会得到一个明确结果：`applied` 或 `rejected` + 原因
- 被拒绝的**不静默丢弃** —— 会原样写进预案，让人看到「代理想做什么、为什么没让做」

为什么要有这一层
----------------
如果直接把 LLM 的 JSON 写进表，那么"当日新增 ≤ 8"这种约束就只是 prompt 里的一句话，
模型换个说法就破了。约束必须写在代码里。
"""
from __future__ import annotations

from typing import Any, Callable

from . import account as account_mod
from . import store
from .store import Tables, ValidationError


def _latest_close(code: str) -> float | None:
    """从本地 market.db 取最新收盘（价格合法性校验用）。取不到返回 None。"""
    try:
        from portal.virtual import marketdb as M
        conn = M.connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT close FROM day WHERE code=? ORDER BY date DESC LIMIT 1",
                (code,)).fetchone()
            return float(row["close"]) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _price_sanity(code: str, stop_loss: Any, take_profit: Any,
                  ref_price: float | None) -> str | None:
    """返回错误信息或 None。价格必须相对最新收盘合理，否则说明模型在编。"""
    try:
        sl, tp = float(stop_loss), float(take_profit)
    except (TypeError, ValueError):
        return f"{code}: stop_loss/take_profit 必须是数字"
    if sl >= tp:
        return f"{code}: 止损 {sl} 必须低于止盈 {tp}"
    if ref_price and ref_price > 0:
        if not (sl < ref_price < tp):
            return (f"{code}: 最新收盘 {ref_price} 不在止损 {sl} 与止盈 {tp} 之间，"
                    f"价格明显不合理")
        if abs(sl / ref_price - 1) > 0.35 or abs(tp / ref_price - 1) > 0.60:
            return (f"{code}: 止损/止盈偏离最新收盘过远"
                    f"（{sl}/{tp} vs {ref_price}），疑似编造")
    return None


class Rejections:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, kind: str, target: str, reason: str) -> None:
        self.items.append({"kind": kind, "target": target, "reason": reason})

    def __len__(self) -> int:
        return len(self.items)


#: 缺止损止盈时的补全规则（相对候选池收盘价）。
#: 为什么要有：实测 LLM 把 stop/target 都填 0（它没有价格参照），
#: 于是四条计划全被拒 —— 预案每天都没有个股计划。补全 + 标注来源是更好的平衡。
AUTO_STOP_RATIO = 0.95
AUTO_TARGET_RATIO = 1.10

#: ★ 禁止追高打板（用户要求，写在代码里而不是只写在 prompt 里）。
#: 打板 = 今日收盘已是涨停价；追高 = 今日涨幅达到该阈值。
#: 靠提示词约束模型是不可靠的，必须由内核拦。
MAX_CHASE_PCT = 7.0

#: 午报专用额度（半日信息不足以支撑大决策，所以比晚间更紧）
MIDDAY_MAX_SINGLE_PCT = 10.0
MIDDAY_MAX_TOTAL_BUY_PCT = 20.0
MIDDAY_MAX_POOL_ADDS = 3


def _chase_guard(code: str, cand: dict | None) -> str | None:
    """返回拒绝原因或 None。只拦「买入」，不影响观察表收录。"""
    if not cand:
        return None
    close, up, chg = cand.get("close"), cand.get("limit_up"), cand.get("chg_pct")
    try:
        if close is not None and up is not None and float(close) >= float(up) - 0.005:
            return (f"{code} 今日收盘 {close} 已达涨停价 {up} —— "
                    f"禁止打板买入（策略约束）")
        if chg is not None and float(chg) >= MAX_CHASE_PCT:
            return (f"{code} 今日涨幅 {chg}% ≥ {MAX_CHASE_PCT}% —— "
                    f"禁止追高买入（策略约束）")
    except (TypeError, ValueError):
        return None
    return None


def _fill_prices(item: dict, cand: dict | None) -> tuple[float, float, str]:
    """返回 (stop_loss, take_profit, price_source)。

    price_source: "llm"（模型给的合理值）| "auto"（内核按收盘价补全）| "none"
    """
    try:
        sl = float(item.get("stop_loss") or 0)
        tp = float(item.get("take_profit") or 0)
    except (TypeError, ValueError):
        sl = tp = 0.0
    if sl > 0 and tp > sl:
        return round(sl, 2), round(tp, 2), "llm"
    close = float((cand or {}).get("close") or 0)
    if close <= 0:
        return sl, tp, "none"
    return (round(close * AUTO_STOP_RATIO, 2),
            round(close * AUTO_TARGET_RATIO, 2), "auto")


def apply_proposal(*, proposal: dict, tables: Tables, plan_date: str,
                   trade_date: str, prices: dict[str, float] | None = None,
                   latest_close: Callable[[str], float | None] | None = None,
                   candidates: list[dict] | None = None,
                   mode: str = "evening",
                   log: Callable[[str], None] = print) -> dict[str, Any]:
    """把提议落到四张表。返回 {applied, rejected, planned}。

    `latest_close` 可注入：测试里给一个固定价格表，避免依赖真实 market.db。
    `candidates` 是当日候选池（带价格）。给了就启用「选股必须在池内」的校验 ——
    这条挡的是 LLM 编造代码（实测它编过一个 603197 保隆科技）。
    `mode`：`evening`（默认，全额度）| `midday`（更紧的额度，且禁止写经验表）——
    半天数据不足以支撑大决策，这条同样由内核强制。
    """
    prices = prices or {}
    ref = latest_close or _latest_close
    universe = {str(c.get("code")): c
                for c in (candidates or []) if c.get("code")}
    max_pool_adds = (MIDDAY_MAX_POOL_ADDS if mode == "midday"
                     else store.MAX_NEW_PER_DAY)
    rej = Rejections()
    applied: dict[str, list] = {
        "pool_add": [], "pool_update": [], "pool_remove": [],
        "info_add": [], "info_remove": [],
        "lesson_add": [], "lesson_remove": [],
    }
    p8 = str(plan_date).replace("-", "")[:8]

    pa = proposal.get("pool_actions") or {}

    # ── 观察表：新增 ────────────────────────────────────
    for item in pa.get("add") or []:
        code = str(item.get("code", "")).strip()
        try:
            if len(applied["pool_add"]) >= max_pool_adds:
                raise ValidationError(
                    f"{mode} 模式下观察表当日最多新增 {max_pool_adds} 只，已达上限")
            if universe and code not in universe:
                raise ValidationError(
                    f"{code} 不在当日候选池（池内 {len(universe)} 只）。"
                    f"程序无法校验它的价格，疑似编造，已拒绝。")
            cand = universe.get(code)
            sl, tp, psrc = _fill_prices(item, cand)
            fields = {k: item.get(k) for k in
                      ("code", "name", "tier", "logic", "reason", "trigger")}
            fields["stop_loss"], fields["take_profit"] = sl, tp
            fields["added_date"] = trade_date
            bad = _price_sanity(code, sl, tp,
                                ref(code) or (cand or {}).get("close")
                                or prices.get(code))
            if bad:
                raise ValidationError(bad)
            tables.pool.add(fields, reason=str(item.get("reason") or ""),
                            plan_date=p8)
            tables.check_caps(p8)          # 每加一只都重新检查容量
            applied["pool_add"].append({"code": code, "name": item.get("name"),
                                        "tier": item.get("tier"),
                                        "reason": item.get("reason"),
                                        "stop_loss": sl, "take_profit": tp,
                                        "price_source": psrc})
            log(f"   ✅ 观察表新增 {code} {item.get('name')}"
                + (f"（止损止盈由内核按收盘价补全：{sl}/{tp}）"
                   if psrc == "auto" else ""))
        except ValidationError as exc:
            rej.add("pool_add", code or "?", str(exc))
            log(f"   ❌ 观察表新增 {code} 被拒：{str(exc)[:80]}")

    # ── 观察表：更新 ────────────────────────────────────
    for item in pa.get("update") or []:
        code = str(item.get("code", "")).strip()
        try:
            fields = {k: item[k] for k in
                      ("tier", "trigger", "stop_loss", "take_profit", "status")
                      if item.get(k) is not None}
            if not fields:
                raise ValidationError("update 没有给出任何要改的字段")
            tables.pool.update(code, fields, reason=str(item.get("reason") or ""),
                               plan_date=p8)
            tables.check_caps(p8)
            applied["pool_update"].append({"code": code, "fields": fields,
                                           "reason": item.get("reason")})
            log(f"   ✅ 观察表更新 {code}：{list(fields)}")
        except ValidationError as exc:
            rej.add("pool_update", code or "?", str(exc))
            log(f"   ❌ 观察表更新 {code} 被拒：{str(exc)[:80]}")

    # ── 观察表：移除 ────────────────────────────────────
    for item in pa.get("remove") or []:
        code = str(item.get("code", "")).strip()
        try:
            tables.pool.remove(code, reason=str(item.get("reason") or ""),
                               plan_date=p8)
            applied["pool_remove"].append({"code": code, "reason": item.get("reason")})
            log(f"   ✅ 观察表移除 {code}")
        except ValidationError as exc:
            rej.add("pool_remove", code or "?", str(exc))
            log(f"   ❌ 观察表移除 {code} 被拒：{str(exc)[:80]}")

    # ── 重要信息 ────────────────────────────────────────
    ia = proposal.get("info_actions") or {}
    for item in ia.get("add") or []:
        try:
            fields = {k: item.get(k) for k in
                      ("content", "scope", "scope_detail", "level", "expires_on")}
            fields["recorded_on"] = trade_date
            rid = None
            ev = tables.infos.add(fields, reason="盘前信息采集",
                                  plan_date=p8)
            rid = ev["id"]
            applied["info_add"].append({"id": rid, "content": item.get("content"),
                                        "level": item.get("level")})
            log(f"   ✅ 重要信息 +1（{item.get('level')}）")
        except ValidationError as exc:
            rej.add("info_add", str(item.get("content"))[:16], str(exc))
            log(f"   ❌ 重要信息被拒：{str(exc)[:80]}")

    for item in ia.get("remove") or []:
        rid = str(item.get("id", "")).strip()
        try:
            tables.infos.remove(rid, reason=str(item.get("reason") or ""), plan_date=p8)
            applied["info_remove"].append({"id": rid, "reason": item.get("reason")})
        except ValidationError as exc:
            rej.add("info_remove", rid or "?", str(exc))

    # ── 经验 ────────────────────────────────────────────
    la = proposal.get("lesson_actions") or {}
    if mode == "midday" and ((la.get("add") or []) or (la.get("remove") or [])):
        rej.add("lesson_add", "-",
                "午报不写交易经验：半天数据不足以沉淀规律，留到晚间复盘（策略约束）")
    for item in ([] if mode == "midday" else (la.get("add") or [])):
        try:
            fields = {k: item.get(k) for k in
                      ("lesson", "scenario", "result", "evidence")}
            fields["recorded_on"] = trade_date
            ev = tables.lessons.add(fields, reason="盘后复盘沉淀", plan_date=p8)
            applied["lesson_add"].append({"id": ev["id"], "lesson": item.get("lesson"),
                                          "result": item.get("result")})
            log(f"   ✅ 经验 +1：{str(item.get('lesson'))[:30]}…")
        except ValidationError as exc:
            rej.add("lesson_add", str(item.get("lesson"))[:16], str(exc))
            log(f"   ❌ 经验被拒：{str(exc)[:80]}")

    for item in la.get("remove") or []:
        rid = str(item.get("id", "")).strip()
        try:
            tables.lessons.remove(rid, reason=str(item.get("reason") or ""),
                                  plan_date=p8)
            applied["lesson_remove"].append({"id": rid, "reason": item.get("reason")})
        except ValidationError as exc:
            rej.add("lesson_remove", rid or "?", str(exc))

    # ── 持仓计划（只校验，不成交；成交由 pipeline/execute_changes 负责）──
    planned = _plan_positions(proposal.get("position_plan") or {}, tables, rej,
                              ref, universe, mode)

    # ── 全局一致性 ──────────────────────────────────────
    try:
        tables.check_pool_not_held()
    except ValidationError as exc:
        rej.add("consistency", "pool/positions", str(exc))
        log(f"   ⚠️ 一致性告警：{str(exc)[:90]}")

    mv = proposal["market_view"]
    lo, hi = mv["position_range_pct"]
    if mv["trend"] == "弱势" and hi > 50:
        rej.add("market_view", "position_range_pct",
                f"趋势=弱势 但仓位上限 {hi}% > 50%，偏激进，已在预案中标注")
        log(f"   ⚠️ 弱势市场仓位上限 {hi}% 偏高，已标注")

    if planned["planned_total_pct"] > hi + 1e-9:
        rej.add("position_plan", "total",
                f"计划投入 {planned['planned_total_pct']}% 超过仓位区间上限 {hi}%")
        log(f"   ⚠️ 计划投入 {planned['planned_total_pct']}% > 区间上限 {hi}%")

    return {"applied": applied, "rejected": rej.items, "planned": planned}


def _plan_positions(pp: dict, tables: Tables, rej: Rejections,
                    ref: Callable[[str], float | None],
                    universe: dict[str, dict] | None = None,
                    mode: str = "evening") -> dict[str, Any]:
    """校验买卖计划（只校验、不成交）。"""
    held = tables.positions.alive()
    buys, sells, adjusts = [], [], []
    total = 0.0
    max_single = (MIDDAY_MAX_SINGLE_PCT if mode == "midday"
                  else account_mod.MAX_SINGLE_PCT)
    max_total = (MIDDAY_MAX_TOTAL_BUY_PCT if mode == "midday"
                 else account_mod.MAX_TOTAL_PCT)

    for item in pp.get("buy") or []:
        code = str(item.get("code", "")).strip()
        try:
            alloc = float(item.get("alloc_pct"))
        except (TypeError, ValueError):
            rej.add("buy", code or "?", "alloc_pct 必须是数字")
            continue
        if not (0 < alloc <= max_single):
            rej.add("buy", code,
                    f"仓位 {alloc}% 超出 (0, {max_single}]（{mode} 额度）")
            continue
        if code in held:
            rej.add("buy", code, "已在持仓中，不能重复买入")
            continue
        if universe and code not in universe:
            rej.add("buy", code,
                    f"不在当日候选池（池内 {len(universe)} 只），疑似编造，已拒绝")
            continue
        cand = universe.get(code)
        # ★ 只观察的标的（涨停家族）不能买。
        #   这条让「池子里只有涨停股」时内核能说清原因，而不是让模型反复提议、
        #   再被 chase_guard 以价格形式拦掉 —— 拒绝原因要指向战法本身。
        if cand is not None and cand.get("buyable") is False:
            rej.add("buy", code,
                    f"{code} 属「{cand.get('playbook_cn') or '仅观察'}」战法："
                    f"只能进观察表等回调，不能买入（策略约束）")
            continue
        chase = _chase_guard(code, cand)          # ★ 禁止追高打板
        if chase:
            rej.add("buy", code, chase)
            continue
        sl, tp, psrc = _fill_prices(item, cand)
        bad = _price_sanity(code, sl, tp,
                            ref(code) or (cand or {}).get("close"))
        if bad:
            rej.add("buy", code, bad)
            continue
        if total + alloc > max_total + 1e-9:
            rej.add("buy", code,
                    f"当日买入合计将达 {total + alloc:.1f}%，"
                    f"超过 {mode} 上限 {max_total}%")
            continue
        total += alloc
        buys.append({"code": code, "name": item.get("name"), "alloc_pct": alloc,
                     "trigger": item.get("trigger"), "stop_loss": sl,
                     "take_profit": tp, "price_source": psrc,
                     "ref_close": (cand or {}).get("close"),
                     "logic": item.get("logic")})

    for item in pp.get("sell") or []:
        code = str(item.get("code", "")).strip()
        if code not in held:
            rej.add("sell", code or "?", "没有这只持仓，无法卖出")
            continue
        sells.append({"code": code, "name": held[code].get("name"),
                      "all": bool(item.get("all", True)),
                      "reason": item.get("reason")})

    for item in pp.get("adjust") or []:
        code = str(item.get("code", "")).strip()
        if code not in held:
            rej.add("adjust", code or "?", "没有这只持仓，无法调整")
            continue
        adjusts.append({"code": code, "stop_loss": item.get("stop_loss"),
                        "take_profit": item.get("take_profit"),
                        "reason": item.get("reason")})

    return {"buy": buys, "sell": sells, "adjust": adjusts,
            "planned_total_pct": round(total, 2)}


__all__ = ["apply_proposal", "Rejections"]
