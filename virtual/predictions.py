# -*- coding: utf-8 -*-
"""
portal/virtual/predictions.py — 预测账本（append-only）

这是「经验累积」的最后一块拼图：把**每一条预测**和**它的结果**配对存下来。

为什么不能只靠账本（ledger.md）
-------------------------------
账本记录的是「钱变成了多少」。但要知道「**哪个信号**准、**哪条规则**在退化」，
必须能按信号/规则/市场状态切片统计 —— 那需要每条预测的：

    当时看到了什么（特征快照）→ 下了什么注（计划）→ 结果如何（成交+盈亏+归因）

`ledger.md` 给人看；`predictions.jsonl` 给机器算。两者都不能省。

校准（calibration）
-------------------
`calibration()` 对每个信号算：
- 命中率（预测会赚 → 实际赚了吗）
- 平均净收益
- **Brier 分数**：衡量「概率判断」的准确性，`mean((p - outcome)^2)`，越小越好。
  没有概率字段时就退回命中率，不假装有 Brier。

追加语义
--------
只追加、不改写。同一条预测先写 `stage=predicted`，结算后追加 `stage=settled`
（带 `parent` 指回原记录），读取时用 `latest_by_id()` 折叠。
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Iterable

from . import paths


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _append(rec: dict) -> None:
    paths.ensure_dirs()
    with paths.PREDICTIONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=False) + "\n")


def _read() -> list[dict]:
    if not paths.PREDICTIONS.exists():
        return []
    out = []
    for line in paths.PREDICTIONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ─────────────────────────────────────────────────────────────
# 写
# ─────────────────────────────────────────────────────────────
def record_prediction(
    plan: dict,
    *,
    gate: dict | None = None,
    features: dict | None = None,
    rules_applied: Iterable[str] | None = None,
) -> dict[str, Any]:
    """盘前/买入时记录一条预测（还没结果）。

    Args:
        features: 特征快照（至少含 market 与 picks 的关键字段），用于事后按条件切片。
    """
    picks = plan.get("picks") or []
    rec = {
        "id": f"{plan.get('plan_id')}-buy",
        "stage": "predicted",
        "at": _now(),
        "plan_id": plan.get("plan_id"),
        "trade_date": plan.get("trade_date"),
        "data_date": plan.get("data_date"),
        "params_version": plan.get("params_version"),
        "params_hash": plan.get("params_hash"),
        "n_picks": len(picks),
        "codes": [p.get("code") for p in picks],
        # 主观概率：计划里若写了 confidence 就用，否则不给（不假造）
        "stated_confidence": plan.get("confidence"),
        "gate": gate,
        "rules_applied": list(rules_applied or []),
        "features": features or {},
        "entry_rule": (plan.get("fill_rule") or {}).get("fill_method"),
        "exit_rule": {
            k: (plan.get("exit_rule") or {}).get(k)
            for k in ("price_basis", "use_stop", "z1")
        },
        "prediction": "本次建仓在平仓后净收益为正",
    }
    _append(rec)
    return rec


def record_settlement(
    buy_fills: dict,
    *,
    exit_fills: dict | None = None,
) -> dict[str, Any]:
    """结算后追加结果。`exit_fills` 为空表示只记买入日（浮动盈亏）。"""
    plan_id = buy_fills.get("plan_id")
    filled = [f for f in (buy_fills.get("fills") or []) if f.get("status") == "filled"]
    rulers = buy_fills.get("rulers") or {}
    attr = rulers.get("attribution") or {}

    rec: dict[str, Any] = {
        "id": f"{plan_id}-settled",
        "parent": f"{plan_id}-buy",
        "stage": "settled",
        "at": _now(),
        "plan_id": plan_id,
        "trade_date": buy_fills.get("trade_date"),
        "filled_n": len(filled),
        "invested": (buy_fills.get("position") or {}).get("invested"),
        "gate": buy_fills.get("gate"),
        "unrealized": buy_fills.get("unrealized"),
        "total_asset": buy_fills.get("total_asset"),
        "rulers": {
            "actual_pct": attr.get("actual_pct"),
            "shadow_pct": attr.get("shadow_pct"),
            "baseline_pct": attr.get("baseline_pct"),
            "index_pct": (rulers.get("index") or {}).get("ret_pct"),
            "execution_pp": attr.get("execution_pp"),
            "selection_pp": attr.get("selection_pp"),
        },
    }

    if exit_fills:
        realized = exit_fills.get("realized")
        rec.update({
            "realized": realized,
            "realized_pct": exit_fills.get("realized_pct"),
            "statuses": {r.get("status"): None for r in (exit_fills.get("rows") or [])},
            "forced_hold": any(r.get("status") == "forced_hold"
                               for r in (exit_fills.get("rows") or [])),
            "z1_defer": any(r.get("status") == "z1_defer"
                            for r in (exit_fills.get("rows") or [])),
            # 二值结果，供命中率 / Brier 使用
            "outcome": 1 if (isinstance(realized, (int, float)) and realized > 0) else 0,
        })
    else:
        rec["outcome"] = None      # 还没平仓，不能算命中

    _append(rec)
    return rec


def record_signal_hits(
    *,
    plan_id: str,
    trade_date: str,
    signals: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """记录「当天触发了哪些信号、各自的历史胜率」，供事后校准。

    `signals` 形如 {信号名: {"count":.., "win_rate":.., "fired": bool, "excess_return":..}}。
    这是把「回测里的胜率」与「实盘结果」对齐的唯一办法。
    """
    rec = {
        "id": f"{plan_id}-signals",
        "stage": "signals",
        "at": _now(),
        "plan_id": plan_id,
        "trade_date": trade_date,
        "signals": signals,
    }
    _append(rec)
    return rec


# ─────────────────────────────────────────────────────────────
# 读 / 校准
# ─────────────────────────────────────────────────────────────
def read_all() -> list[dict]:
    return _read()


def latest_by_id() -> dict[str, dict]:
    """同一 id 取最后一条（append-only 折叠）。"""
    out: dict[str, dict] = {}
    for r in _read():
        rid = r.get("id")
        if isinstance(rid, str):
            out[rid] = r
    return out


def settled() -> list[dict]:
    return [r for r in latest_by_id().values()
            if r.get("stage") == "settled" and r.get("outcome") is not None]


def calibration() -> dict[str, Any]:
    """校准表：命中率 / 平均净收益 / Brier（有概率才给）。"""
    rows = settled()
    n = len(rows)
    if n == 0:
        return {"n": 0, "note": "还没有已平仓的预测样本；先跑一轮 plan → settle → exit"}

    hits = sum(int(r.get("outcome") or 0) for r in rows)
    pcts = [float(r["realized_pct"]) for r in rows
            if isinstance(r.get("realized_pct"), (int, float))]
    brier = None
    probs = [(float(r["stated_confidence"]), int(r["outcome"]))
             for r in _read()
             if r.get("stage") == "predicted" and isinstance(r.get("stated_confidence"), (int, float))
             and any(s.get("parent") == r.get("id") for s in rows)]
    if probs:
        brier = round(sum((p - o) ** 2 for p, o in probs) / len(probs), 4)

    return {
        "n": n,
        "hit_rate": round(hits / n * 100, 1),
        "avg_realized_pct": round(sum(pcts) / len(pcts), 3) if pcts else None,
        "median_realized_pct": round(sorted(pcts)[len(pcts) // 2], 3) if pcts else None,
        "brier": brier,
        "brier_note": ("有 stated_confidence 才算 Brier；没有就留空，不假装有"
                       if brier is None else "基于计划里声明的 confidence"),
        "forced_hold_n": sum(1 for r in rows if r.get("forced_hold")),
        "z1_defer_n": sum(1 for r in rows if r.get("z1_defer")),
        "sample_note": "n<30 不得下任何结论" if n < 30 else "样本量已达最低要求",
    }


def summary() -> dict[str, Any]:
    rows = _read()
    by_stage: dict[str, int] = {}
    for r in rows:
        by_stage[r.get("stage", "?")] = by_stage.get(r.get("stage", "?"), 0) + 1
    return {
        "records": len(rows),
        "by_stage": by_stage,
        "path": str(paths.PREDICTIONS),
        "exists": paths.PREDICTIONS.exists(),
        "calibration": calibration(),
    }


__all__ = [
    "record_prediction",
    "record_settlement",
    "record_signal_hits",
    "read_all",
    "latest_by_id",
    "settled",
    "calibration",
    "summary",
]
