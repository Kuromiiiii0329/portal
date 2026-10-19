# -*- coding: utf-8 -*-
"""
portal/premarket/paths.py — 盘前预案的数据根与文件布局

约定（与 `virtual/paths.py` 一致）
------------------------------
- **决策记录提交 git**：四张表、预案 JSON、报告 HTML 都是审计留痕
- **派生/缓存不提交**：采集到的原始快照（新闻正文、行情快照）放 `cache/`
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

#: 允许用环境变量把数据根指到别处（测试/CI 用）
_ROOT_ENV = "PORTAL_PREMARKET_ROOT"

PORTAL_DIR = Path(__file__).resolve().parent.parent


def _root() -> Path:
    env = os.environ.get(_ROOT_ENV)
    if env:
        return Path(env)
    return PORTAL_DIR / "data" / "premarket"


ROOT = _root()

# ── 四张表（append-only 事件流，提交 git）──────────────────
POOL = ROOT / "pool.jsonl"          # a. 选股观察表
POSITIONS = ROOT / "positions.jsonl"  # b. 模拟持仓表（事件流）
TRADES = ROOT / "trades.jsonl"      # b. 已平仓流水（持仓行被移除后，已实现盈亏必须另存）
ACCOUNT = ROOT / "account.json"     # b. 账户快照（派生，可重算）
INFOS = ROOT / "infos.jsonl"        # c. 重要信息表
LESSONS = ROOT / "lessons.jsonl"    # d. 交易经验表

# ── 产物（提交 git）─────────────────────────────────────
PLANS = ROOT / "plans"              # 预案 JSON（含锚点）
REPORTS = ROOT / "reports"          # HTML 预案/复盘
REVIEWS = ROOT / "reviews"          # 盘后复盘 JSON

# ── 采集缓存（不提交）───────────────────────────────────
CACHE = ROOT / "cache"

#: 四张表的名字（用于错误信息与渲染）
TABLES = ("pool", "positions", "infos", "lessons")


def ensure_dirs() -> None:
    for p in (ROOT, PLANS, REPORTS, REVIEWS, CACHE):
        p.mkdir(parents=True, exist_ok=True)


def cache_dir(date8: str | None = None) -> Path:
    d = CACHE / (date8 or datetime.date.today().strftime("%Y%m%d"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def plan_path(plan_date: str) -> Path:
    """plan_date: YYYYMMDD 或 YYYY-MM-DD。"""
    d8 = str(plan_date).replace("-", "")[:8]
    return PLANS / f"plan_{d8}.json"


def report_path(plan_date: str) -> Path:
    d8 = str(plan_date).replace("-", "")[:8]
    return REPORTS / f"{d8}_premarket.html"


def review_path(date8: str) -> Path:
    d8 = str(date8).replace("-", "")[:8]
    return REVIEWS / f"review_{d8}.json"


def table_path(name: str) -> Path:
    return {"pool": POOL, "positions": POSITIONS,
            "infos": INFOS, "lessons": LESSONS}[name]


__all__ = ["ROOT", "POOL", "POSITIONS", "TRADES", "ACCOUNT", "INFOS", "LESSONS",
           "PLANS", "REPORTS", "REVIEWS", "CACHE", "TABLES",
           "PORTAL_DIR", "ensure_dirs", "cache_dir",
           "plan_path", "report_path", "review_path", "table_path"]
