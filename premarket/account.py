# -*- coding: utf-8 -*-
"""
portal/premarket/account.py — 百分比制模拟账户核算

口径（用户 spec：「不算具体资金，只按照百分比，初始总资金为 100%」）
-------------------------------------------------------------------
内部把总资产记成 **100.00 个份额**，对外**只显示百分比**。

    qty        = alloc_pct% × 100 / entry_price       （分数股，不取整）
    cash      += -qty × entry_price（买入）/ +qty × price（卖出）
    total      = cash + Σ(qty × last_price)

**不计费用**（用户明确要求）。买卖都不打折：买入占用多少份额就扣多少。

为什么用「分数股」而不是凑整 100 股
--------------------------------
真实交易有 100 股整手约束，但本账户是**百分比账**：凑整会让「买 10%」变成
「买 9.7%」，仓位就与预案写的数不一致，之后所有归因都对不上。
所以这里保留分数股，把「整手」当成执行层的待办，而不是账本的假设。

执行时点
--------
调仓**默认按当日收盘价直接成交**（用户要求：持仓不是"买入计划"，而是当天完成调仓）。
由 `execute_changes()` 把买卖决定当场落到持仓表。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from . import paths
from .store import Tables, ValidationError

#: 初始总资产（份额）
INITIAL = 100.0
#: 单只标的最大仓位（防止 LLM 说「梭哈」）
MAX_SINGLE_PCT = 40.0
#: 总仓位上限（防止满仓穿越黑天鹅）
MAX_TOTAL_PCT = 100.0


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.date.today().isoformat()


# ─────────────────────────────────────────────────────────────
# 流水（已平仓）
# ─────────────────────────────────────────────────────────────
def load_trades(path: Path | None = None) -> list[dict]:
    p = path or paths.TRADES
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def append_trade(rec: dict, path: Path | None = None) -> dict:
    p = path or paths.TRADES
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


# ─────────────────────────────────────────────────────────────
# 核算
# ─────────────────────────────────────────────────────────────
def compute(tables: Tables, prices: dict[str, float],
            *, prev_total: float | None = None,
            trades_path: Path | None = None) -> dict[str, Any]:
    """按最新价重算账户。`prices` 缺失的持仓用成本价兜底并标注 `stale`。

    返回的每个数字都是**百分比或份额**，不含任何「元」。
    """
    positions = tables.positions.alive()
    rows: list[dict] = []
    realized = sum(float(t.get("realized_pct") or 0)
                   for t in load_trades(trades_path))
    invested_cost = 0.0

    for code, pos in positions.items():
        entry = float(pos["entry_price"])
        alloc = float(pos["alloc_pct"])
        qty = alloc * INITIAL / 100.0 / entry          # 分数股
        px = prices.get(code)
        stale = px is None
        last = float(px) if px is not None else entry
        value = qty * last
        cost = qty * entry
        invested_cost += cost

        rows.append({
            "code": code,
            "name": pos.get("name", ""),
            "qty": round(qty, 4),
            "entry_price": round(entry, 3),
            "last_price": round(last, 3),
            "cost_pct": round(alloc, 3),                    # 开仓时占总资产
            "value_pct": round(value, 4),                   # 现在值多少份额
            "weight_pct": 0.0,                              # 占总资产，稍后填
            "unrealized_pct": round((last / entry - 1) * 100, 3) if not stale else 0.0,
            "pnl_pct": round(value - cost, 4),              # 对总资产的贡献（份额）
            "entry_date": pos.get("entry_date"),
            "entry_logic": pos.get("entry_logic", ""),
            "stop_loss": pos.get("stop_loss"),
            "take_profit": pos.get("take_profit"),
            "stale": stale,
        })

    cash = INITIAL - invested_cost + realized
    market_value = sum(r["value_pct"] for r in rows)
    total = cash + market_value

    for r in rows:
        r["weight_pct"] = round(r["value_pct"] / total * 100, 2) if total else 0.0

    day_pnl = (total - prev_total) if prev_total is not None else 0.0
    return {
        "as_of": _now(),
        "date": _today(),
        "initial": INITIAL,
        "total_pct": round(total, 4),                 # 总资产（份额，初始 100）
        "cash_pct": round(cash, 4),                   # 可用（份额）
        "market_value_pct": round(market_value, 4),   # 持仓市值（份额）
        "position_ratio_pct": round(market_value / total * 100, 2) if total else 0.0,
        "day_pnl_pct": round(day_pnl, 4),
        "day_pnl_ratio_pct": round(day_pnl / prev_total * 100, 3) if prev_total else 0.0,
        "cum_pnl_pct": round(total - INITIAL, 4),
        "cum_pnl_ratio_pct": round((total / INITIAL - 1) * 100, 3),
        "realized_pct": round(realized, 4),
        "invested_cost_pct": round(invested_cost, 4),
        "n_positions": len(rows),
        "positions": rows,
        "stale_codes": [r["code"] for r in rows if r["stale"]],
    }


# ─────────────────────────────────────────────────────────────
# 开仓 / 平仓（带跨表约束）
# ─────────────────────────────────────────────────────────────
def open_position(tables: Tables, *, code: str, name: str, alloc_pct: float,
                  entry_price: float, stop_loss: float, take_profit: float,
                  entry_logic: str, reason: str, plan_date: str,
                  entry_date: str | None = None) -> dict:
    """建仓。会自动把该标的从观察表移出（spec：调入持仓后移出观察表）。"""
    snap = compute(tables, {})
    if snap["position_ratio_pct"] + alloc_pct > MAX_TOTAL_PCT + 1e-9:
        raise ValidationError(
            f"总仓位将达 {snap['position_ratio_pct'] + alloc_pct:.1f}%，"
            f"超过上限 {MAX_TOTAL_PCT}%")
    if alloc_pct > MAX_SINGLE_PCT:
        raise ValidationError(
            f"单只 {code} 仓位 {alloc_pct}%，超过上限 {MAX_SINGLE_PCT}%")

    ev = tables.positions.add({
        "code": code, "name": name, "alloc_pct": alloc_pct,
        "entry_price": entry_price, "stop_loss": stop_loss,
        "take_profit": take_profit, "entry_logic": entry_logic,
        "entry_date": entry_date or _today(),
    }, reason=reason, plan_date=plan_date)

    if code in tables.pool.alive():
        tables.pool.update(code, {"status": "removed"},
                           reason=f"已调入持仓：{reason}",
                           plan_date=plan_date)
    return ev


def close_position(tables: Tables, *, code: str, exit_price: float,
                   reason: str, plan_date: str,
                   exit_date: str | None = None) -> dict:
    """平仓并落一条已实现流水。持仓行随后删除，但盈亏留在 trades.jsonl。"""
    pos = tables.positions.alive().get(code)
    if not pos:
        raise ValidationError(f"没有持仓 {code}，无法平仓")

    alloc = float(pos["alloc_pct"])
    entry = float(pos["entry_price"])
    qty = alloc * INITIAL / 100.0 / entry
    buy_cost = qty * entry
    sell_net = qty * float(exit_price)
    realized = sell_net - buy_cost
    realized_pct = realized / INITIAL * 100          # 对初始总资产的贡献

    rec = {
        "code": code,
        "name": pos.get("name", ""),
        "qty": round(qty, 4),
        "alloc_pct": round(alloc, 4),
        "entry_price": round(entry, 3),
        "exit_price": round(float(exit_price), 3),
        "entry_date": pos.get("entry_date"),
        "exit_date": exit_date or _today(),
        "buy_cost_pct": round(buy_cost, 4),
        "sell_net_pct": round(sell_net, 4),
        "realized_pct": round(realized_pct, 4),      # 占总资产 %
        "trade_return_pct": round((sell_net / buy_cost - 1) * 100, 3),
        "reason": reason,
        "plan_date": str(plan_date).replace("-", "")[:8],
        "at": _now(),
    }
    append_trade(rec)
    tables.positions.remove(code, reason=reason, plan_date=plan_date)
    return rec


def adjust_position(tables: Tables, *, code: str, reason: str, plan_date: str,
                    stop_loss: float | None = None,
                    take_profit: float | None = None) -> dict:
    """只调止损/止盈（spec 允许动态调整）。"""
    fields: dict[str, Any] = {}
    if stop_loss is not None:
        fields["stop_loss"] = stop_loss
    if take_profit is not None:
        fields["take_profit"] = take_profit
    if not fields:
        raise ValidationError("adjust_position 至少要给 stop_loss 或 take_profit")
    return tables.positions.update(code, fields, reason=reason, plan_date=plan_date)


# ─────────────────────────────────────────────────────────────
# 调仓执行（用户要求：默认按收盘价直接完成调仓）
# ─────────────────────────────────────────────────────────────
def execute_changes(tables: Tables, planned: dict, prices: dict[str, float],
                    *, plan_date: str, iso_date: str,
                    log=lambda *_: None) -> dict[str, Any]:
    """把 agent 的买卖决定**当场按收盘价**执行完。

    为什么不再"挂计划等次日"：用户明确要求「自己的持仓不是买入计划，默认直接以
    收盘价买入完成调仓」。所以这里直接改持仓表，报告展示的是**已执行的调仓**。

    `prices` 必须包含要交易的代码；缺价则跳过并记录原因（不猜价）。
    先卖后买：卖出释放的仓位才能给买入用。
    """
    executed: list[dict] = []
    skipped: list[dict] = []

    for s in planned.get("sell") or []:
        code = s.get("code")
        px = prices.get(code)
        if px is None:
            skipped.append({"action": "sell", "code": code, "reason": "取不到收盘价"})
            continue
        try:
            rec = close_position(tables, code=code, exit_price=px,
                                 reason=f"调仓卖出：{s.get('reason') or ''}",
                                 plan_date=plan_date, exit_date=iso_date)
            executed.append({"action": "sell", **rec})
            log(f"  ✅ 卖出 {code} @{px}（已实现 {rec['realized_pct']} 份额）")
        except ValidationError as exc:
            skipped.append({"action": "sell", "code": code, "reason": str(exc)})

    for b in planned.get("buy") or []:
        code = b.get("code")
        px = prices.get(code)
        if px is None:
            skipped.append({"action": "buy", "code": code, "reason": "取不到收盘价"})
            continue
        try:
            open_position(
                tables, code=code, name=b.get("name") or code,
                alloc_pct=float(b["alloc_pct"]), entry_price=px,
                stop_loss=float(b["stop_loss"]), take_profit=float(b["take_profit"]),
                entry_logic=b.get("logic") or "", reason="调仓买入（收盘价成交）",
                plan_date=plan_date, entry_date=iso_date)
            executed.append({"action": "buy", "code": code, "name": b.get("name"),
                             "price": px, "alloc_pct": b["alloc_pct"],
                             "reason": b.get("trigger") or ""})
            log(f"  ✅ 买入 {code} @{px} 仓位 {b['alloc_pct']}%")
        except ValidationError as exc:
            skipped.append({"action": "buy", "code": code, "reason": str(exc)})

    for a in planned.get("adjust") or []:
        try:
            adjust_position(tables, code=a["code"], plan_date=plan_date,
                            stop_loss=a.get("stop_loss"),
                            take_profit=a.get("take_profit"),
                            reason=f"调仓调整：{a.get('reason') or ''}")
            executed.append({"action": "adjust", "code": a["code"],
                             "stop_loss": a.get("stop_loss"),
                             "take_profit": a.get("take_profit"),
                             "reason": a.get("reason")})
        except ValidationError as exc:
            skipped.append({"action": "adjust", "code": a["code"],
                            "reason": str(exc)})
    return {"executed": executed, "skipped": skipped}


# ─────────────────────────────────────────────────────────────
# 快照文件（派生数据，可随时重算）
# ─────────────────────────────────────────────────────────────
def save_snapshot(snap: dict, path: Path | None = None) -> Path:
    p = path or paths.ACCOUNT
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_snapshot(path: Path | None = None) -> dict | None:
    p = path or paths.ACCOUNT
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


__all__ = ["INITIAL", "MAX_SINGLE_PCT", "MAX_TOTAL_PCT",
           "compute", "open_position", "close_position", "adjust_position",
           "execute_changes", "load_trades", "append_trade",
           "save_snapshot", "load_snapshot"]
