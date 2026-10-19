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


def start_generate(*, plan_date: str | None = None,
                   trade_date: str | None = None,
                   use_cache: bool = True) -> dict[str, Any]:
    args = ["-m", "portal.premarket", "generate"]
    if plan_date:
        args += ["--plan-date", str(plan_date).replace("-", "")[:8]]
    if trade_date:
        args += ["--trade-date", str(trade_date).replace("-", "")[:8]]
    if not use_cache:
        args.append("--no-cache")
    args.append("--json")
    return JOB.start("generate", args,
                     note="LLM 通常 30–120 秒；未配置 LLM 时会降级为规则兜底并在报告顶部标注。")


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
    """返回最新一份预案 HTML 的路径与 mtime。"""
    try:
        from portal.premarket import paths
        files = sorted(glob.glob(str(paths.REPORTS / "*_premarket.html")))
        if not files:
            return _err("还没有生成过预案")
        p = Path(files[-1])
        return {"ok": True, "path": str(p), "name": p.name,
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


__all__ = ["JOB", "start_generate", "start_review", "job_status", "status",
           "latest_report", "open_report", "tables"]
