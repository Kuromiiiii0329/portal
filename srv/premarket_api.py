# -*- coding: utf-8 -*-
"""
portal/srv/premarket_api.py — 盘前预案的后端接口（前端「一键生成」用）

只做三件事：
    1) 启动/查询「生成预案」后台任务（LLM 可能跑几分钟，不能挂 HTTP）
    2) 读四张表与账户状态
    3) 打开最新报告

所有数字都来自 `portal.premarket`，这里不做任何计算。
"""
from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

from .jobrunner import JobRunner


def _project_root() -> Path:
    from portal.premarket import paths
    return paths.PORTAL_DIR.parent


#: 单槽位：同一时刻只允许一个预案任务（避免两次采集/两次写表互相踩）
JOB = JobRunner("premarket", root=_project_root())


def _err(msg: str, hint: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": msg}
    if hint:
        out["hint"] = hint
    return out


def start_daily(*, date8: str | None = None, plan_date: str | None = None,
                use_cache: bool = True) -> dict[str, Any]:
    """一条龙：结算昨日计划 → 采集 → 次日计划 → 复盘 → 一份报告。

    用户要求：盘后复盘与盘前预案本质上都在收盘后做，合并成一条流程。
    """
    args = ["-m", "portal.premarket", "daily"]
    if date8:
        args += ["--date", str(date8).replace("-", "")[:8]]
    if plan_date:
        args += ["--plan-date", str(plan_date).replace("-", "")[:8]]
    if not use_cache:
        args.append("--no-cache")
    args.append("--json")
    return JOB.start("daily", args,
                     note="含当日结算 + 次日计划 + 复盘；LLM 通常 40–150 秒。"
                          "未配置 LLM 时降级为规则兜底，报告顶部会标注。")


def start_midday() -> dict[str, Any]:
    """午报：半日行情 + 预案核对 + 午后判断（12:00 前后运行）。

    时段护栏在 `midday.session_guard`：不在午休窗口、或官方快照不是今天 11:30，
    都会直接失败 —— 宁可报错，也不许拿旧数据假装午报。
    """
    return JOB.start("midday", ["-m", "portal.premarket", "midday", "--json"],
                     note="仅在午休（11:25–13:15）可用；LLM 通常 30–90 秒。")


def start_generate(*, plan_date: str | None = None,
                   trade_date: str | None = None,
                   use_cache: bool = True) -> dict[str, Any]:
    """兼容旧路由：等同 start_daily。"""
    return start_daily(date8=trade_date, plan_date=plan_date, use_cache=use_cache)


def start_review(*, date8: str | None = None) -> dict[str, Any]:
    args = ["-m", "portal.premarket", "review"]
    if date8:
        args += ["--date", str(date8).replace("-", "")[:8]]
    return JOB.start("review", args, note="按预案买卖计划、以当日收盘价机械结算。")


def job_status(*, tail: int = 40) -> dict[str, Any]:
    return JOB.status(tail=tail)


def status() -> dict[str, Any]:
    """四张表 + 账户 + 最近预案。缺数据时也能返回（用于前端首屏）。"""
    try:
        from portal.premarket import pipeline
        st = pipeline.status()
        st["running"] = JOB.running
        return st
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def latest_report() -> dict[str, Any]:
    """返回最新一份报告（晚间或午间，按修改时间取最新的）。"""
    try:
        from portal.premarket import paths
        files = list(paths.REPORTS.glob("*_premarket.html")) + \
            list(paths.REPORTS.glob("*_midday.html"))
        if not files:
            return _err("还没有生成过报告")
        p = max(files, key=lambda f: f.stat().st_mtime)
        return {"ok": True, "path": str(p), "name": p.name,
                "kind": "midday" if p.name.endswith("_midday.html") else "evening",
                "bytes": p.stat().st_size,
                "mtime": p.stat().st_mtime}
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def open_report() -> dict[str, Any]:
    r = latest_report()
    if not r.get("ok"):
        return r
    try:
        import webbrowser
        webbrowser.open(Path(r["path"]).as_uri())
        return {"ok": True, "opened": r["path"]}
    except Exception as exc:
        return _err(f"打开失败：{exc}", r["path"])


def tables() -> dict[str, Any]:
    try:
        from portal.premarket.store import Tables
        from portal.premarket import account as acc
        t = Tables()
        snap = t.snapshot()
        acct = acc.load_snapshot() or acc.compute(t, {})
        return {"ok": True, **snap, "account": acct}
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


__all__ = ["JOB", "start_daily", "start_midday", "start_generate", "start_review", "job_status", "status",
           "latest_report", "open_report", "tables"]
