# -*- coding: utf-8 -*-
"""
portal/srv/virtual_api.py — 前端「虚拟盘」Tab 的后端接口（纯函数，便于测试）

设计原则：**每个接口都必须能在缺数据库 / 缺数据时优雅降级**，返回
`{"ok": false, "error": ..., "hint": ...}` 而不是抛 500 —— 前端的价值在于
让你看见「为什么现在不能跑」，而不是一个红叉。
"""
from __future__ import annotations

import datetime
import json
from typing import Any


def _err(msg: str, hint: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": msg}
    if hint:
        out["hint"] = hint
    return out


# ─────────────────────────────────────────────────────────────
# 状态总览
# ─────────────────────────────────────────────────────────────
def status() -> dict[str, Any]:
    """一屏看清：数据库 / 规则 / 校准 / 锚点 / 衰减健康度。"""
    out: dict[str, Any] = {"ok": True, "at": datetime.datetime.now().astimezone()
                           .isoformat(timespec="seconds")}

    # 数据库
    try:
        from portal.virtual import datafeed
        out["db"] = datafeed.db_summary()
    except Exception as exc:
        out["db"] = {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    # 规则
    try:
        from portal.virtual import rules
        rows = rules.summary_table()
        out["rules"] = {
            "count": len(rows),
            "by_status": _count_by(rows, "status"),
            "rows": rows[:50],
        }
    except Exception as exc:
        out["rules"] = {"count": 0, "error": f"{type(exc).__name__}: {exc}"}

    # 预测校准
    try:
        from portal.virtual import predictions
        out["predictions"] = predictions.summary()
    except Exception as exc:
        out["predictions"] = {"records": 0, "error": f"{type(exc).__name__}: {exc}"}

    # 衰减健康度
    try:
        from portal.virtual import decay
        out["decay"] = decay.health()
    except Exception as exc:
        out["decay"] = []
        out["decay_error"] = f"{type(exc).__name__}: {exc}"

    # 锚点
    try:
        from portal.virtual import anchor
        d = anchor.load()
        out["anchors"] = {
            "count": len(d.get("order", [])),
            "head": (d.get("head") or "")[:16],
            "chain_ok": not anchor.verify_chain(d),
            "recent": [
                {"plan_id": p, "anchored_at": d["records"][p].get("anchored_at")}
                for p in d.get("order", [])[-10:]
            ],
        }
    except Exception as exc:
        out["anchors"] = {"count": 0, "error": f"{type(exc).__name__}: {exc}"}

    # 特征覆盖
    try:
        from portal.virtual import tagging
        days = tagging.list_days()
        out["features"] = {"days": len(days), "latest": days[-5:], "earliest": days[:1]}
    except Exception as exc:
        out["features"] = {"days": 0, "error": f"{type(exc).__name__}: {exc}"}

    return out


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key))
        out[k] = out.get(k, 0) + 1
    return out


def rules_list(status: str | None = None) -> dict[str, Any]:
    try:
        from portal.virtual import rules
        rows = rules.summary_table()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return {"ok": True, "count": len(rows), "rows": rows}
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def predictions_info() -> dict[str, Any]:
    try:
        from portal.virtual import predictions
        return {"ok": True, **predictions.summary(),
                "recent": predictions.settled()[-10:]}
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def ledger_text() -> dict[str, Any]:
    try:
        from portal.virtual import paths
        if not paths.LEDGER.exists():
            return {"ok": True, "text": "", "note": "还没有账本（先跑一次买入日结算）"}
        return {"ok": True, "text": paths.LEDGER.read_text(encoding="utf-8")}
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────────────────────
# 动作
# ─────────────────────────────────────────────────────────────
def warmup(days: int = 20, *, write: bool = True) -> dict[str, Any]:
    """从数据库重建涨停池并生成特征（提升 zone_pos_120 覆盖）。"""
    try:
        from portal.virtual import datafeed, warmup as warm
        conn = datafeed.connect()
        if conn is None:
            return _err("找不到回测数据库",
                        "先跑：python -m portal.virtual.marketdb init && "
                        "python -m portal.virtual.marketdb build")
        end = str(conn.execute("SELECT MAX(date) FROM day").fetchone()[0]).replace("-", "")[:8]
        days_list = warm.trading_days(conn, "1990-01-01", end)[-max(1, min(int(days), 250)):]
        if not days_list:
            return _err("交易日历为空", "先跑 marketdb init")
        stats = warm.warm_range(conn, days_list[0], end, write=write, progress=lambda *_: None)
        return {"ok": True, **stats}
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def evolve(max_days: int = 40, *, commit: bool = False,
           horizon: int = 1) -> dict[str, Any]:
    """跑一轮演化（默认**不落库**，只返回提议，避免一键就改了规则表）。"""
    try:
        from portal.virtual import datafeed, evolve as evo, weights
        conn = datafeed.connect()
        if conn is None:
            return _err("找不到回测数据库", "先跑 marketdb build")
        end = str(conn.execute("SELECT MAX(date) FROM day").fetchone()[0]).replace("-", "")[:8]
        rows, meta = weights.build_panel(conn, "20250101", end, horizon=horizon,
                                         max_days=max(1, min(int(max_days), 250)))
        if not rows:
            return _err("面板行数为 0（数据库覆盖不足）", json.dumps(meta, ensure_ascii=False))
        # 面板只有 technical/yuzi 两维，演化器只认特征字段名，这里映射回特征名
        adapted = []
        for r in rows:
            rr = dict(r)
            rr["zone_pos_120"] = r.get("zone_pos_120")
            rr["lbc"] = r.get("boards")
            adapted.append(rr)
        res = evo.run(adapted, dims=("zone_pos_120", "lbc"), n_buckets=4)
        out = {
            "ok": True,
            "panel": meta,
            "tested": len(res.get("tested") or []),
            "passed": [e["rule_id"] for e in res.get("passed") or []],
            "rejected": len(res.get("rejected") or []),
            "details": [
                {"rule_id": e["rule_id"], "desc": e["desc"], "evidence": e["evidence"],
                 "gate": e.get("gate")}
                for e in (res.get("passed") or []) + (res.get("rejected") or [])[:5]
            ],
        }
        if commit:
            out["committed"] = evo.propose(res)
        else:
            out["note"] = "未落库（commit=false）。确认无误后再用 CLI 或 commit=true 写入。"
        return out
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def make_report(*, open_browser: bool = True) -> dict[str, Any]:
    """生成自包含复盘报告（含审计；审计不过则不落盘）。"""
    try:
        from portal.virtual import decay, predictions, report, rules
        payload = {
            "title": "虚拟盘复盘报告",
            "caliber": "所有收益均为 T+1 开盘买入口径，已扣往返成本 0.10%",
            "kpis": _kpis(),
            "rules": rules.summary_table(),
            "text": "",
            "claims": [],
            "numbers": [],
            "params_text": {},
            "params_machine": {},
            "backtest": None,
            "known_bias_declared": True,
            "notes": _notes(decay, predictions),
        }
        res = report.build_and_write(
            payload, filename=f"virtual_{datetime.date.today():%Y%m%d}.html")
        if res.get("written") and open_browser:
            _open(res["path"])
        return {"ok": bool(res.get("written")), **res}
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def _kpis() -> list[dict]:
    out: list[dict] = []
    try:
        from portal.virtual import paths
        import re
        if paths.LEDGER.exists():
            text = paths.LEDGER.read_text(encoding="utf-8")
            nums = re.findall(r"\|\s*([\d,]+\.\d{2})\s*\|\s*$", text, re.M)
            if nums:
                last = float(nums[-1].replace(",", ""))
                out.append({"label": "期末总资产", "value": last,
                            "display": f"{last:,.2f}"})
    except Exception:
        pass
    return out


def _notes(decay, predictions) -> list[str]:
    notes = [
        "成交与盈亏由确定性内核计算（T+1 开盘买入、扣费 0.10%），未经人工修饰。",
        "情绪类指标为同步指标，只用于仓位，不参与个股评分。",
    ]
    try:
        cal = predictions.calibration()
        if cal.get("n"):
            notes.append(f"预测校准：已平仓 {cal['n']} 笔，命中率 {cal.get('hit_rate')}%"
                         f"，平均 {cal.get('avg_realized_pct')}%")
    except Exception:
        pass
    try:
        h = decay.health()
        if h:
            failing = [x for x in h if x.get("consecutive_fail")]
            if failing:
                notes.append("衰减告警：" + "、".join(
                    f"{x['rule_id']}(连续{x['consecutive_fail']}次低于阈值)" for x in failing))
    except Exception:
        pass
    return notes


def _open(path: str) -> None:
    try:
        import webbrowser
        webbrowser.open(f"file:///{path.replace(chr(92), '/')}")
    except Exception:
        pass


__all__ = ["status", "rules_list", "predictions_info", "ledger_text",
           "warmup", "evolve", "make_report"]
