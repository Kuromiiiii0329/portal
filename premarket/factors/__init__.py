# -*- coding: utf-8 -*-
"""
portal/premarket/factors/__init__.py — 因子注册表（自动发现）

加一个因子 = 往这个目录放一个 `f_xxx.py`，里面写 `META` 与 `compute(panel)`。
不用改任何注册代码；删掉文件即下线。

目录约定
--------
    factors/
        base.py          Panel / Factor / 表达式白名单
        f_*.py           内置因子（可信代码）
        discovered/      agent 发现并通过验证的因子（只允许表达式）
        playbooks.py     战法 = 因子的布尔组合
        validate.py      因子统计检验（IC / 多空价差 / 覆盖率）
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Iterable

from .base import Factor, FactorError, Panel, make_factor  # noqa: F401

__all__ = ["Panel", "Factor", "FactorError", "make_factor",
           "load_factors", "build_panel", "evaluate", "discover",
           "factor_summary", "validate_all", "discover_plays",
           "all_plays"]

import pathlib

_DIR = pathlib.Path(__file__).parent


def discover(*, include_discovered: bool = True) -> dict[str, Factor]:
    """扫描目录，返回 {factor_id: Factor}。同名后者覆盖前者（discovered 优先）。"""
    found: dict[str, Factor] = {}
    prefixes = ["portal.premarket.factors"]
    mods: list[str] = []
    for info in pkgutil.iter_modules([str(_DIR)]):
        if info.name.startswith("f_"):
            mods.append(f"portal.premarket.factors.{info.name}")
    if include_discovered:
        disc = _DIR / "discovered"
        if disc.is_dir():
            for info in pkgutil.iter_modules([str(disc)]):
                if info.name.startswith("f_"):
                    mods.append(f"portal.premarket.factors.discovered.{info.name}")
    for name in mods:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:          # 单个因子写坏了不该拖垮全库
            raise FactorError(f"{name} 导入失败：{type(exc).__name__}: {exc}") from exc
        meta = getattr(mod, "META", None)
        compute = getattr(mod, "compute", None)
        expr = getattr(mod, "EXPR", None)
        if not meta:
            raise FactorError(f"{name} 没有 META")
        f = make_factor(meta, compute=compute, expr=expr)
        if meta.get("source") is None:
            f.meta["source"] = ("discovered" if "discovered" in name else "builtin")
        f.meta["module"] = name
        found[f.id] = f
    return found


_CACHE: dict[str, Factor] | None = None
_PLAYS_CACHE: list[dict] | None = None


def discover_plays(*, include_discovered: bool = True) -> list[dict]:
    """收集因子模块里 `PLAYS` 声明的新战法。

    这是「新建一个因子文件，下次报告就会用它选股」的关键：
    因子文件除了 `META` + `compute`，还可以写

        PLAYS = [{"id": "my_setup", "name": "我的买点", "buyable": True,
                  "why": "...", "conditions": ["my_factor >= 0.5", ...],
                  "evidence": ["my_factor", "vol_ratio"],
                  "sort": [["my_factor", "desc"]]}]

    注册表会把它们和内置战法合并，`screen()` 一视同仁地跑。
    """
    plays: list[dict] = []
    prefixes: list[str] = []
    for info in pkgutil.iter_modules([str(_DIR)]):
        if info.name.startswith("f_"):
            prefixes.append(f"portal.premarket.factors.{info.name}")
    if include_discovered:
        disc = _DIR / "discovered"
        if disc.is_dir():
            for info in pkgutil.iter_modules([str(disc)]):
                if info.name.startswith("f_"):
                    prefixes.append(
                        f"portal.premarket.factors.discovered.{info.name}")
    for name in prefixes:
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        for play in getattr(mod, "PLAYS", None) or []:
            if not isinstance(play, dict) or not play.get("id"):
                continue
            p = dict(play)
            p["source"] = name
            p.setdefault("buyable", False)
            plays.append(p)
    return plays


def all_plays(*, refresh: bool = False) -> list[dict]:
    """内置战法 + 因子模块贡献的战法（去重，后者覆盖同 id）。"""
    global _PLAYS_CACHE
    if _PLAYS_CACHE is None or refresh:
        from . import playbooks as PB
        merged: dict[str, dict] = {}
        for p in PB.PLAYBOOKS:
            merged[p["id"]] = dict(p)
        for p in discover_plays():
            merged[p["id"]] = p          # 因子贡献的可以覆盖内置（便于迭代）
        _PLAYS_CACHE = list(merged.values())
    return _PLAYS_CACHE


def load_factors(*, refresh: bool = False) -> dict[str, Factor]:
    global _CACHE
    if _CACHE is None or refresh:
        _CACHE = discover()
    return _CACHE


# ─────────────────────────────────────────────────────────────
# 构造 Panel
# ─────────────────────────────────────────────────────────────
def build_panel(conn, quotes: Iterable[dict], asof_iso: str, *,
                history_days: int = 40) -> Panel:
    """把「批量行情 + 本地历史」合成一个 Panel。"""
    from ..screen import _limit_pct

    p = Panel(asof=asof_iso)
    for q in quotes:
        code = str(q.get("code") or "")
        if len(code) != 6:
            continue
        close = q.get("close")
        prev = q.get("prev_close")
        if not close or not prev:
            continue
        p.close[code] = float(close)
        p.prev_close[code] = float(prev)
        if q.get("name"):
            p.names[code] = q["name"]
        if q.get("volume"):
            p.volume[code] = float(q["volume"])
        for key, dst in (("open", p.open_today), ("high", p.high_today),
                         ("low", p.low_today)):
            v = q.get(key)
            if v:
                dst[code] = float(v)
        amt = q.get("amount")
        if amt:
            p.amount_yi[code] = float(amt) / 1e8
        if q.get("limit_up"):
            p.limit_up[code] = float(q["limit_up"])
        p.chg[code] = (p.close[code] / p.prev_close[code] - 1) * 100
        p.limit_pct[code] = _limit_pct(code)

    if conn is not None:
        rows = conn.execute(
            """WITH d AS (
                   SELECT DISTINCT date FROM day
                   WHERE date < ? ORDER BY date DESC LIMIT ?
               )
               SELECT code, date, close, high, low, volume
               FROM day WHERE date IN (SELECT date FROM d)
               ORDER BY code, date""", (asof_iso, history_days)).fetchall()
        for r in rows:
            rec = p.hist.setdefault(r["code"], {"close": [], "high": [],
                                                "low": [], "vol": []})
            rec["close"].append(r["close"])
            rec["high"].append(r["high"])
            rec["low"].append(r["low"])
            rec["vol"].append(r["volume"])
    return p


def evaluate(panel: Panel, factors: dict[str, Factor] | None = None,
             *, ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """跑一批因子，返回 {factor_id: {code: value}}。"""
    factors = factors or load_factors()
    want = list(ids) if ids else list(factors)
    out: dict[str, dict[str, Any]] = {}
    for fid in want:
        f = factors.get(fid)
        if not f:
            raise FactorError(f"没有因子 {fid}")
        out[fid] = f.run(panel)
    return out


def factor_summary() -> list[dict[str, Any]]:
    """给 CLI / 报告用的因子清单。"""
    factors = load_factors()
    return [
        {"id": f.id, "name": f.meta.get("name"), "kind": f.kind,
         "unit": f.meta.get("unit") or "",
         "direction": f.meta.get("direction") or "",
         "source": f.meta.get("source"),
         "description": f.meta.get("description")}
        for f in sorted(factors.values(), key=lambda x: x.id)
    ]


def validate_all(*, days: int = 20, horizon: int = 1,
                 conn=None, log=print) -> list[dict[str, Any]]:
    """批量统计检验（见 validate.py）。"""
    from .validate import run_all
    return run_all(days=days, horizon=horizon, conn=conn, log=log)
