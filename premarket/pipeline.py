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
             use_cache: bool = True, allow_fallback: bool = True,
             write: bool = True,
             log: Callable[[str], None] = print) -> dict[str, Any]:
    """生成盘前预案。返回 {ok, plan, report_path, plan_path, ...}。

    `latest_close` 可注入：生产用 market.db，测试给固定价格表。
    `market` 可注入：填了就跳过采集（测试用，避免联网与缓存干扰）。
    """
    paths.ensure_dirs()
    trade8 = (trade_date or latest_trade_date()).replace("-", "")[:8]
    trade_iso = f"{trade8[:4]}-{trade8[4:6]}-{trade8[6:8]}"
    plan8 = (plan_date or _next_trade_date(trade8)).replace("-", "")[:8]

    log(f"📋 生成盘前预案：数据日 {trade_iso} → 预案日 {plan8}")

    # 1) 四张表 + 账户
    tables = Tables()
    log("  读取四张表…")
    expired = tables.expire_infos()
    if expired:
        log(f"  自动清理过期重要信息 {len(expired)} 条")
    snapshot = tables.snapshot()
    held = list(tables.positions.alive())
    prices = held_prices(held)
    snap_prev = account_mod.load_snapshot()
    acct = account_mod.compute(tables, prices,
                               prev_total=(snap_prev or {}).get("total_pct"))

    # 2) 采集（允许注入，测试时跳过联网）
    if market is None:
        log("  采集盘前数据…")
        market = collect_mod.collect(trade8, use_cache=use_cache, progress=log)
    else:
        log("  使用注入的行情数据（跳过采集）")
    digest = collect_mod.summarize(market)

    # 3) 提议
    source = "llm"
    raw: list[str] = []
    try:
        proposal = agent_mod.propose(
            snapshot=snapshot, market=digest, account=acct,
            plan_date=plan8, trade_date=trade_iso,
            llm_call=llm_call, log=log, raw_sink=raw)
    except Exception as exc:
        if not allow_fallback:
            raise
        log(f"⚠️  LLM 不可用（{type(exc).__name__}: {str(exc)[:120]}），改用规则兜底")
        source = "rule_fallback"
        proposal = agent_mod.validate(rule_fallback_proposal(market))
        proposal["_fallback_reason"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    # 4) 裁决
    log("  内核裁决…")
    verdict = apply_mod.apply_proposal(
        proposal=proposal, tables=tables, plan_date=plan8,
        trade_date=trade_iso, prices=prices, latest_close=latest_close,
        candidates=market.get("candidates"), log=log)

    # 5) 组装预案
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
    }
    plan["anchor"] = {"sha256": anchor_of(plan), "prev": _prev_anchor(plan8),
                      "at": plan["generated_at"],
                      "note": "锚点 = 预案内容的 sha256，事后可验证未被修改"}

    if not write:
        return {"ok": True, "plan": plan, "written": False}

    # 6) 落盘：plan JSON + HTML
    ppath = paths.plan_path(plan8)
    ppath.parent.mkdir(parents=True, exist_ok=True)
    ppath.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    rpath = paths.report_path(plan8)
    r = render_mod.write_plan({
        "plan_date": plan8, "trade_date": trade_iso,
        "generated_at": plan["generated_at"],
        "market": market, "proposal": proposal,
        "applied": verdict["applied"], "rejected": verdict["rejected"],
        "planned": verdict["planned"], "snapshot": snapshot_after, "account": acct,
        "proposal_source": source, "anchor": plan["anchor"],
    }, rpath)

    account_mod.save_snapshot(acct)
    log(f"✅ 预案已生成：{rpath.name}（{r['bytes']} 字节）")
    return {"ok": True, "written": True, "plan": plan,
            "plan_path": str(ppath), "report_path": str(rpath),
            "report_bytes": r["bytes"], "proposal_source": source,
            "applied": verdict["applied"], "rejected": verdict["rejected"]}


# ─────────────────────────────────────────────────────────────
# 盘后复盘（更新持仓表 + 已实现盈亏）
# ─────────────────────────────────────────────────────────────
def review(*, date8: str | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """盘后复盘：按预案执行计划，用实际收盘价结算，更新持仓表与账户。

    只做**机械结算**：买/卖都按当日收盘价成交（虚拟盘口径），费用 0.10% 往返。
    真实成交价与滑点属于执行层，不在本函数内假设。
    """
    paths.ensure_dirs()
    d8 = (date8 or latest_trade_date()).replace("-", "")[:8]
    iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"
    tables = Tables()

    ppath = paths.plan_path(d8)
    if not ppath.exists():
        return {"ok": False, "error": f"找不到 {d8} 的预案 {ppath.name}"}
    plan = json.loads(ppath.read_text(encoding="utf-8"))
    planned = plan.get("planned") or {}

    applied, skipped = [], []
    codes = [b["code"] for b in planned.get("buy") or []] + \
            [s["code"] for s in planned.get("sell") or []]
    px = held_prices(sorted(set(codes)))

    for s in planned.get("sell") or []:
        code = s["code"]
        price = px.get(code)
        if price is None:
            skipped.append({"action": "sell", "code": code, "reason": "取不到收盘价"})
            continue
        rec = account_mod.close_position(
            tables, code=code, exit_price=price,
            reason=f"复盘按收盘价结算：{s.get('reason') or ''}",
            plan_date=d8, exit_date=iso)
        applied.append({"action": "sell", **rec})
        log(f"  ✅ 卖出 {code} @{price} 已实现 {rec['realized_pct']} 份")

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
                entry_logic=b.get("logic") or "", reason="复盘按收盘价成交",
                plan_date=d8, entry_date=iso)
            applied.append({"action": "buy", "code": code, "price": price,
                            "alloc_pct": b["alloc_pct"]})
            log(f"  ✅ 买入 {code} @{price} 仓位 {b['alloc_pct']}%")
        except Exception as exc:
            skipped.append({"action": "buy", "code": code, "reason": str(exc)[:120]})

    for a in planned.get("adjust") or []:
        try:
            account_mod.adjust_position(
                tables, code=a["code"], plan_date=d8,
                stop_loss=a.get("stop_loss"), take_profit=a.get("take_profit"),
                reason=f"复盘调整：{a.get('reason') or ''}")
            applied.append({"action": "adjust", "code": a["code"]})
        except Exception as exc:
            skipped.append({"action": "adjust", "code": a["code"], "reason": str(exc)[:120]})

    # 按最新价重算账户
    held = list(tables.positions.alive())
    prices = held_prices(held)
    prev = account_mod.load_snapshot()
    acct = account_mod.compute(tables, prices,
                               prev_total=(prev or {}).get("total_pct"))
    account_mod.save_snapshot(acct)

    out = {"ok": True, "date": iso, "applied": applied, "skipped": skipped,
           "account": acct, "plan_path": str(ppath)}
    rpath = paths.review_path(d8)
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ 复盘完成：执行 {len(applied)} 笔，跳过 {len(skipped)} 笔，"
        f"总资产 {acct['total_pct']}（累计 {acct['cum_pnl_ratio_pct']}%）")
    return out


# ─────────────────────────────────────────────────────────────
# 状态
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
