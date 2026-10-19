# -*- coding: utf-8 -*-
"""
portal/premarket/pipeline.py — 编排：采集 → 提议 → 裁决 → 渲染 → 存档

一条链路只做一次，全部落盘留痕：
    collect（数据） → agent（判断，LLM 或规则兜底） → apply（内核裁决）
    → render（代码生成 HTML） → plans/plan_YYYYMMDD.json（含锚点）

关于「没有 LLM 怎么办」
--------------------
直接报错会让「一键生成预案」在没配 key 的机器上完全不可用，但那台机器上
数据采集、四张表、账户核算是**能跑**的。所以这里降级为**规则兜底**：

    proposal_source = "llm" | "rule_fallback"

`rule_fallback` 只做大盘趋势→仓位区间的机械映射，外加题材排序；
不新增个股、不做买卖计划（那些需要判断）。报告里会显著标注「本次为规则兜底，
非 agent 判断」，绝不冒充。这样"能跑"与"是谁判断的"两件事都清楚。
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from . import account as account_mod
from . import agent as agent_mod
from . import apply as apply_mod
from . import collect as collect_mod
from . import paths
from . import render as render_mod
from .store import Tables


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _today8() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _next_trade_date(trade_date: str) -> str:
    """粗算下一个工作日（不含节假日日历；仅用于预案日期标记）。"""
    d = datetime.datetime.strptime(trade_date.replace("-", "")[:8], "%Y%m%d").date()
    for _ in range(10):
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            return d.isoformat()
    return d.isoformat()


def latest_trade_date() -> str:
    """优先用本地库的最新交易日，取不到就用今天。"""
    try:
        from portal.virtual import marketdb as M
        conn = M.connect()
        if conn is not None:
            try:
                d = M.latest_trade_date(conn)
                if d:
                    return d.replace("-", "")[:8]
            finally:
                conn.close()
    except Exception:
        pass
    return _today8()


def held_prices(codes: list[str]) -> dict[str, float]:
    """从本地库取持仓最新收盘价。取不到的留空（account 会用成本价并标 stale）。"""
    if not codes:
        return {}
    try:
        from portal.virtual import marketdb as M
        conn = M.connect()
        if conn is None:
            return {}
        try:
            out = {}
            for c in codes:
                row = conn.execute(
                    "SELECT close FROM day WHERE code=? ORDER BY date DESC LIMIT 1",
                    (c,)).fetchone()
                if row:
                    out[c] = float(row["close"])
            return out
        finally:
            conn.close()
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────
# 规则兜底提议（无 LLM 时用）
# ─────────────────────────────────────────────────────────────
def rule_fallback_proposal(market: dict) -> dict:
    """只用采集到的数字做机械映射。不新增标的、不做买卖计划。"""
    idx = market.get("indices") or []
    weak = sum(1 for i in idx if i.get("trend") == "走弱")
    strong = sum(1 for i in idx if i.get("trend") == "强势")
    n = max(1, len(idx))
    if weak >= n * 0.6:
        trend, rng = "弱势", [10, 30]
    elif strong >= n * 0.5:
        trend, rng = "强势", [40, 70]
    else:
        trend, rng = "震荡", [20, 50]

    b = market.get("breadth") or {}
    reasons = []
    if idx:
        first = idx[0]
        reasons.append(
            f"{first.get('name')} {first.get('chg_pct'):+.2f}%，"
            f"5日 {first.get('chg_5d_pct'):+.2f}%，成交额 {first.get('amount_yi')}亿")
    if b.get("up") is not None:
        reasons.append(f"上涨 {b.get('up')} 家 / 下跌 {b.get('down')} 家，"
                       f"上涨占比 {b.get('up_ratio_pct')}%")
    if b.get("break_rate_pct") is not None:
        reasons.append(f"炸板率 {b.get('break_rate_pct')}%，最高连板 "
                       f"{b.get('max_boards')} 板")
    if not reasons:
        reasons = ["数据不足，按最保守假设给仓位 10-30%"]

    sectors = [
        {"name": t["name"], "logic": f"涨停 {t['limit_up_n']} 家、最高 {t['max_boards']} 板",
         "strength": min(100.0, t["limit_up_n"] * 8.0),
         "evidence": "涨停池行业聚合"}
        for t in (market.get("themes") or [])[:5]
    ]

    risk = list(market.get("caveats") or [])
    if b.get("break_rate_pct") and b["break_rate_pct"] > 40:
        risk.append(f"炸板率 {b['break_rate_pct']}% 偏高，接力风险大")
    risk.append("本次为规则兜底（未接入 LLM），只做仓位与题材排序，未生成个股计划")

    return {
        "market_view": {"trend": trend, "position_range_pct": rng,
                        "confidence": 30.0, "reasons": reasons[:5]},
        "sector_priority": sectors,
        "pool_actions": {"add": [], "update": [], "remove": []},
        "position_plan": {"buy": [], "sell": [], "adjust": []},
        "info_actions": {"add": [], "remove": []},
        "lesson_actions": {"add": [], "remove": []},
        "risk_notes": risk[:6],
        "execution_checklist": [
            "开盘 30 分钟先看量能与涨跌家数，不追高",
            f"总仓位控制在 {rng[0]}-{rng[1]}%",
            "无 LLM 时按规则兜底，个股决策请人工复核",
        ],
    }


# ─────────────────────────────────────────────────────────────
# 锚点（预案冻结，事后可验证未被改）
# ─────────────────────────────────────────────────────────────
def anchor_of(plan: dict) -> str:
    body = {k: plan.get(k) for k in
            ("plan_date", "trade_date", "market_digest", "proposal", "planned")}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _prev_anchor(plan_date: str) -> str | None:
    d = datetime.datetime.strptime(str(plan_date)[:8], "%Y%m%d").date()
    for _ in range(30):
        d -= datetime.timedelta(days=1)
        p = paths.plan_path(d.strftime("%Y%m%d"))
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")).get("anchor", {}).get("sha256")
            except json.JSONDecodeError:
                return None
    return None


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def generate(*, plan_date: str | None = None, trade_date: str | None = None,
             llm_call: Callable[[str], str] | None = None,
             latest_close: Callable[[str], float | None] | None = None,
             market: dict[str, Any] | None = None,
             settle: bool = True,
             use_cache: bool = True, allow_fallback: bool = True,
             write: bool = True,
             log: Callable[[str], None] = print) -> dict[str, Any]:
    """盘后一条龙：结算当日 → 采集 → agent（次日计划 + 复盘）→ 裁决 → 一份报告。

    顺序按用户要求：**先收集信息 → 再总结当日持仓涨跌 → 再定次日计划 → 最后复盘**。
    注意实现上结算必须排在 agent 之前（agent 要看到当日盈亏才能复盘），
    但报告里的章节顺序仍按用户要求排列（消息/大盘/情绪/题材 → 持仓 → 计划 → 复盘）。

    `latest_close` 可注入：生产用 market.db，测试给固定价格表。
    `market` 可注入：填了就跳过采集（测试用，避免联网与缓存干扰）。
    `settle=False` 可跳过结算（只出预案，不执行昨日计划）。
    """
    paths.ensure_dirs()
    trade8 = (trade_date or latest_trade_date()).replace("-", "")[:8]
    trade_iso = f"{trade8[:4]}-{trade8[4:6]}-{trade8[6:8]}"
    plan8 = (plan_date or _next_trade_date(trade8)).replace("-", "")[:8]

    log(f"📋 生成盘前预案：数据日 {trade_iso} → 预案日 {plan8}")

    # 1) 四张表
    tables = Tables()
    log("  读取四张表…")
    expired = tables.expire_infos()
    if expired:
        log(f"  自动清理过期重要信息 {len(expired)} 条")

    # 1.5) ★ 先结算「昨天为今天制定的计划」，得到今日成交与已实现盈亏。
    #      必须放在账户核算与 agent 之前 —— 否则 agent 看不到今天的盈亏，
    #      复盘就成了凭空猜测。
    settle_info = {"applied": [], "skipped": [], "had_plan": False}
    if settle:
        settle_info = _settle_plan(tables, trade8, trade_iso, log)
        if settle_info["had_plan"]:
            log(f"  今日按预案结算：成交 {len(settle_info['applied'])} 笔，"
                f"跳过 {len(settle_info['skipped'])} 笔")

    # 2) 账户（含当日盈亏）
    snapshot = tables.snapshot()
    held = list(tables.positions.alive())
    px_ctx = _price_ctx(held, trade_iso)
    prices = {c: v["last"] for c, v in px_ctx.items() if v.get("last") is not None}
    snap_prev = account_mod.load_snapshot()
    acct = account_mod.compute(tables, prices,
                               prev_total=(snap_prev or {}).get("total_pct"),
                               trades_path=paths.TRADES)

    # 3) 采集（允许注入，测试时跳过联网）
    if market is None:
        log("  采集盘前数据…")
        market = collect_mod.collect(trade8, use_cache=use_cache, progress=log)
    else:
        log("  使用注入的行情数据（跳过采集）")
    digest = collect_mod.summarize(market)

    # 4) 复盘素材（给 agent 的「已发生事实」）
    review_ctx = {
        "date": trade_iso,
        "account": {k: acct.get(k) for k in
                    ("total_pct", "cash_pct", "market_value_pct",
                     "position_ratio_pct", "day_pnl_pct", "day_pnl_ratio_pct",
                     "cum_pnl_ratio_pct", "n_positions")},
        "positions": [
            {**{k: p.get(k) for k in
                ("code", "name", "entry_price", "last_price", "unrealized_pct",
                 "stop_loss", "take_profit", "entry_logic", "weight_pct")},
             "today_chg_pct": (px_ctx.get(p["code"]) or {}).get("chg_pct")}
            for p in (acct.get("positions") or [])
        ],
        "executed": settle_info["applied"],
        "skipped": settle_info["skipped"],
    }

    # 5) 提议
    source = "llm"
    raw: list[str] = []
    try:
        proposal = agent_mod.propose(
            snapshot=snapshot, market=digest, account=acct,
            plan_date=plan8, trade_date=trade_iso, review_context=review_ctx,
            llm_call=llm_call, log=log, raw_sink=raw)
    except Exception as exc:
        if not allow_fallback:
            raise
        log(f"⚠️  LLM 不可用（{type(exc).__name__}: {str(exc)[:120]}），改用规则兜底")
        source = "rule_fallback"
        proposal = agent_mod.validate(rule_fallback_proposal(market))
        proposal["_fallback_reason"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    # 6) 裁决
    log("  内核裁决…")
    verdict = apply_mod.apply_proposal(
        proposal=proposal, tables=tables, plan_date=plan8,
        trade_date=trade_iso, prices=prices, latest_close=latest_close,
        candidates=market.get("candidates"), log=log)

    # 6.5) ★ 当场按收盘价完成调仓（用户要求：持仓不是"买入计划"）
    #      先卖后买由 execute_changes 保证；缺价就跳过并记原因，绝不猜价。
    need = {c for c in
            ([b.get("code") for b in verdict["planned"].get("buy") or []]
             + [s.get("code") for s in verdict["planned"].get("sell") or []]) if c}
    exec_prices = {c: prices[c] for c in need if c in prices}
    missing = [c for c in need if c not in exec_prices]
    if missing:
        for c, v in _price_ctx(missing, trade_iso).items():
            if v.get("last") is not None:
                exec_prices[c] = v["last"]
    if need:
        log(f"  执行调仓（{len(exec_prices)}/{len(need)} 只可取价）…")
    exec_result = account_mod.execute_changes(
        tables, verdict["planned"], exec_prices,
        plan_date=plan8, iso_date=trade_iso, log=log)

    # 调仓后的账户：报告「持仓」用这个，而「当日盈亏」用调仓前的
    acct_after = account_mod.compute(tables, prices,
                                     prev_total=(snap_prev or {}).get("total_pct"),
                                     trades_path=paths.TRADES)

    # 7) 组装预案
    snapshot_after = tables.snapshot()
    plan: dict[str, Any] = {
        "plan_date": plan8,
        "trade_date": trade_iso,
        "data_date": market.get("date"),
        "generated_at": _now(),
        "proposal_source": source,
        "proposal": proposal,
        "market_digest": digest,
        "planned": verdict["planned"],
        "applied": verdict["applied"],
        "rejected": verdict["rejected"],
        "account_at_gen": {k: acct[k] for k in
                           ("total_pct", "cash_pct", "market_value_pct",
                            "position_ratio_pct", "cum_pnl_ratio_pct", "n_positions")},
        "tables_after": {"pool": len([r for r in snapshot_after["pool"]
                                      if r.get("status") != "removed"]),
                         "positions": len(snapshot_after["positions"]),
                         "infos": len(snapshot_after["infos"]),
                         "lessons": len(snapshot_after["lessons"])},
        "caveats": market.get("caveats") or [],
        "errors": market.get("errors") or [],
        "settlement": {"had_plan": settle_info["had_plan"],
                       "executed": len(settle_info["applied"]),
                       "skipped": len(settle_info["skipped"])},
        "executed_now": exec_result["executed"],
        "exec_skipped": exec_result["skipped"],
        "account_after": {k: acct_after[k] for k in
                          ("total_pct", "cash_pct", "market_value_pct",
                           "position_ratio_pct", "cum_pnl_ratio_pct", "n_positions")},
        "review": proposal.get("review"),
    }
    plan["anchor"] = {"sha256": anchor_of(plan), "prev": _prev_anchor(plan8),
                      "at": plan["generated_at"],
                      "note": "锚点 = 预案内容的 sha256，事后可验证未被修改"}

    if not write:
        return {"ok": True, "plan": plan, "written": False}

    # 8) 落盘：plan JSON + HTML
    ppath = paths.plan_path(plan8)
    ppath.parent.mkdir(parents=True, exist_ok=True)
    ppath.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    rpath = paths.report_path(plan8)
    r = render_mod.write_plan({
        "plan_date": plan8, "trade_date": trade_iso,
        "plan_date_iso": f"{plan8[:4]}-{plan8[4:6]}-{plan8[6:8]}",
        "generated_at": plan["generated_at"],
        "market": market, "proposal": proposal,
        "applied": verdict["applied"], "rejected": verdict["rejected"],
        "planned": verdict["planned"], "snapshot": snapshot_after,
        "account": acct_after,
        "review_context": {**review_ctx,
                           "executed": exec_result["executed"],
                           "skipped": exec_result["skipped"]},
        "proposal_source": source, "anchor": plan["anchor"],
    }, rpath)

    account_mod.save_snapshot(acct_after)
    log(f"✅ 预案已生成：{rpath.name}（{r['bytes']} 字节）")
    if exec_result["executed"]:
        log(f"   本次调仓已执行 {len(exec_result['executed'])} 笔，"
            f"跳过 {len(exec_result['skipped'])} 笔")
    return {"ok": True, "written": True, "plan": plan,
            "plan_path": str(ppath), "report_path": str(rpath),
            "report_bytes": r["bytes"], "proposal_source": source,
            "applied": verdict["applied"], "rejected": verdict["rejected"],
            "executed": exec_result["executed"],
            "exec_skipped": exec_result["skipped"]}


# ─────────────────────────────────────────────────────────────
# 盘后复盘（更新持仓表 + 已实现盈亏）
# ─────────────────────────────────────────────────────────────
# 结算：把「为今天制定的计划」按今日收盘价机械执行
# ─────────────────────────────────────────────────────────────
def _price_ctx(codes: list[str], iso: str) -> dict[str, dict]:
    """取每只票的最新收盘与前收 → 当日涨跌幅（用于持仓当日表现）。"""
    if not codes:
        return {}
    out: dict[str, dict] = {}
    try:
        from portal.virtual import marketdb as M
        conn = M.connect()
        if conn is None:
            return {}
        try:
            for c in codes:
                rows = conn.execute(
                    "SELECT date, close FROM day WHERE code=? AND date<=? "
                    "ORDER BY date DESC LIMIT 2", (c, iso)).fetchall()
                if not rows:
                    continue
                last = float(rows[0]["close"])
                prev = float(rows[1]["close"]) if len(rows) > 1 else None
                out[c] = {"last": last, "prev": prev,
                          "chg_pct": round((last / prev - 1) * 100, 3) if prev else None}
        finally:
            conn.close()
    except Exception:
        return {}
    return out


def _settle_plan(tables: Tables, d8: str, iso: str,
                 log: Callable[[str], None] = print) -> dict[str, Any]:
    """执行 `plan_<d8>.json` 里的买卖计划（按 d8 当日收盘价）。

    返回 {had_plan, applied, skipped}。没有预案就返回 had_plan=False（不报错）——
    首次运行、或用户某天没生成预案，都是正常情况。
    """
    ppath = paths.plan_path(d8)
    if not ppath.exists():
        return {"had_plan": False, "applied": [], "skipped": []}
    try:
        plan = json.loads(ppath.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"had_plan": False, "applied": [], "skipped": []}

    planned = plan.get("planned") or {}
    applied, skipped = [], []
    codes = [b["code"] for b in planned.get("buy") or []] + \
            [s["code"] for s in planned.get("sell") or []]
    px = held_prices(sorted(set(codes)))
    if not px:
        ctx = _price_ctx(sorted(set(codes)), iso)
        px = {c: v["last"] for c, v in ctx.items() if v.get("last") is not None}

    for s in planned.get("sell") or []:
        code = s["code"]
        price = px.get(code)
        if price is None:
            skipped.append({"action": "sell", "code": code, "reason": "取不到收盘价"})
            continue
        try:
            rec = account_mod.close_position(
                tables, code=code, exit_price=price,
                reason=f"按收盘价结算：{s.get('reason') or ''}",
                plan_date=d8, exit_date=iso)
            applied.append({"action": "sell", **rec})
            log(f"  ✅ 卖出 {code} @{price} 已实现 {rec['realized_pct']} 份")
        except Exception as exc:
            skipped.append({"action": "sell", "code": code, "reason": str(exc)[:120]})

    for b in planned.get("buy") or []:
        code = b["code"]
        price = px.get(code)
        if price is None:
            skipped.append({"action": "buy", "code": code, "reason": "取不到收盘价"})
            continue
        try:
            account_mod.open_position(
                tables, code=code, name=b.get("name") or code,
                alloc_pct=float(b["alloc_pct"]), entry_price=price,
                stop_loss=float(b["stop_loss"]), take_profit=float(b["take_profit"]),
                entry_logic=b.get("logic") or "", reason="按收盘价成交",
                plan_date=d8, entry_date=iso)
            applied.append({"action": "buy", "code": code, "name": b.get("name"),
                            "price": price, "alloc_pct": b["alloc_pct"],
                            "reason": b.get("trigger") or ""})
            log(f"  ✅ 买入 {code} @{price} 仓位 {b['alloc_pct']}%")
        except Exception as exc:
            skipped.append({"action": "buy", "code": code, "reason": str(exc)[:120]})

    for a in planned.get("adjust") or []:
        try:
            account_mod.adjust_position(
                tables, code=a["code"], plan_date=d8,
                stop_loss=a.get("stop_loss"), take_profit=a.get("take_profit"),
                reason=f"调整：{a.get('reason') or ''}")
            applied.append({"action": "adjust", "code": a["code"],
                            "reason": a.get("reason")})
        except Exception as exc:
            skipped.append({"action": "adjust", "code": a["code"],
                            "reason": str(exc)[:120]})
    return {"had_plan": True, "applied": applied, "skipped": skipped}


# ─────────────────────────────────────────────────────────────
def review(*, date8: str | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """仅做结算（不生成新报告）。保留给需要单独复盘结算的场景。

    现在完整的「盘后复盘 + 盘前预案」走 `daily()` / `generate()`。
    """
    paths.ensure_dirs()
    d8 = (date8 or latest_trade_date()).replace("-", "")[:8]
    iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"
    tables = Tables()

    ppath = paths.plan_path(d8)
    if not ppath.exists():
        return {"ok": False, "error": f"找不到 {d8} 的预案 {ppath.name}"}

    info = _settle_plan(tables, d8, iso, log)
    held = list(tables.positions.alive())
    prices = held_prices(held)
    prev = account_mod.load_snapshot()
    acct = account_mod.compute(tables, prices,
                               prev_total=(prev or {}).get("total_pct"),
                               trades_path=paths.TRADES)
    account_mod.save_snapshot(acct)

    out = {"ok": True, "date": iso, "applied": info["applied"],
           "skipped": info["skipped"], "account": acct, "plan_path": str(ppath)}
    rpath = paths.review_path(d8)
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ 复盘完成：执行 {len(info['applied'])} 笔，跳过 {len(info['skipped'])} 笔，"
        f"总资产 {acct['total_pct']}（累计 {acct['cum_pnl_ratio_pct']}%）")
    return out


# ─────────────────────────────────────────────────────────────
# 状态
# ─────────────────────────────────────────────────────────────
def daily(*, date8: str | None = None, plan_date: str | None = None,
          **kwargs) -> dict[str, Any]:
    """「盘后复盘 + 盘前预案」合并入口（用户要求的一条流程）。

    正常用法就是每天收盘后跑一次：它会
      1. 按今日收盘价结算昨天为今天制定的计划（成交 / 已实现盈亏）
      2. 采集当日信息（指数 / 情绪 / 题材 / 消息 / 候选池）
      3. 让 agent 看到**当日真实盈亏**后出次日计划与复盘
      4. 内核裁决四张表与买卖计划
      5. 输出一份合并 HTML

    `date8` 是「数据日」（今天），预案自动落到下一个工作日。
    """
    # 调仓已改为「当场按收盘价成交」，所以不需要再结算昨日计划。
    return generate(trade_date=date8, plan_date=plan_date, settle=False, **kwargs)


# ─────────────────────────────────────────────────────────────
def status() -> dict[str, Any]:
    tables = Tables()
    snap = tables.snapshot()
    acct = account_mod.load_snapshot() or account_mod.compute(tables, {})
    plans = sorted(paths.PLANS.glob("plan_*.json")) if paths.PLANS.exists() else []
    reports = sorted(paths.REPORTS.glob("*_premarket.html")) if paths.REPORTS.exists() else []
    return {
        "ok": True,
        "root": str(paths.ROOT),
        "counts": {"pool": len([r for r in snap["pool"] if r.get("status") != "removed"]),
                   "positions": len(snap["positions"]),
                   "infos": len(snap["infos"]),
                   "lessons": len(snap["lessons"])},
        "account": {k: acct.get(k) for k in
                    ("total_pct", "cash_pct", "position_ratio_pct",
                     "cum_pnl_ratio_pct", "n_positions")},
        "plans": [{"name": p.name, "at": p.stat().st_mtime} for p in plans[-5:]],
        "n_plans": len(plans),
        "n_reports": len(reports),
        "latest_report": str(reports[-1]) if reports else None,
    }
