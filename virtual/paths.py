# -*- coding: utf-8 -*-
"""
portal/virtual/paths.py — 虚拟盘数据根的路径约定

设计原则（与 UPGRADE_PLAN.md §5 一致）：

    「经验」必须落盘成结构化载体，不能只活在会话上下文里。

目录分两类，git 待遇不同：

    **决策记录（必须提交）** —— 体积小、不可再生、是审计的证据链
        anchors.json     参数锚点链（防作弊）
        rules.jsonl      规则注册表（append-only 事件日志）
        rejected.jsonl   被闸门拒绝的假设（防止 Agent 反复提同样坏主意）
        predictions.jsonl 预测账本（每条预测 + 结果 + 归因）
        calibration.json 信号校准表（Brier / 命中率 / 衰减）
        ledger.md        人类可读账本
        incidents.md     事故志

    **派生数据（不提交，可再生产）**
        features/YYYYMMDD.json  pre/post 两阶段标签

环境变量 `PORTAL_VIRTUAL_ROOT` 可整体覆盖数据根（便于测试与多账户）。
"""
from __future__ import annotations

import os
from pathlib import Path

PORTAL_DIR = Path(__file__).resolve().parent.parent

VIRTUAL_ROOT = Path(
    os.environ.get("PORTAL_VIRTUAL_ROOT") or (PORTAL_DIR / "data" / "virtual")
).resolve()

# ── 决策记录 ──────────────────────────────────────────────
ANCHORS = VIRTUAL_ROOT / "anchors.json"
RULES = VIRTUAL_ROOT / "rules.jsonl"
REJECTED = VIRTUAL_ROOT / "rejected.jsonl"
PREDICTIONS = VIRTUAL_ROOT / "predictions.jsonl"
CALIBRATION = VIRTUAL_ROOT / "calibration.json"
LEDGER = VIRTUAL_ROOT / "ledger.md"
INCIDENTS = VIRTUAL_ROOT / "incidents.md"

# ── 派生数据 ──────────────────────────────────────────────
FEATURES_DIR = VIRTUAL_ROOT / "features"
PLANS_DIR = VIRTUAL_ROOT / "plans"
FILLS_DIR = VIRTUAL_ROOT / "fills"

_DECISION_FILES = (ANCHORS, RULES, REJECTED, PREDICTIONS, CALIBRATION)


def ensure_dirs() -> None:
    """建目录。幂等。"""
    for d in (VIRTUAL_ROOT, FEATURES_DIR, PLANS_DIR, FILLS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def feature_path(date8: str) -> Path:
    """`20261008` → features/20261008.json"""
    return FEATURES_DIR / f"{_norm_date(date8)}.json"


def plan_path(plan_id: str) -> Path:
    return PLANS_DIR / f"plan_{plan_id}.json"


def fills_path(plan_id: str) -> Path:
    return FILLS_DIR / f"{plan_id}.json"


def _norm_date(date8: str) -> str:
    """把 2026-10-08 / 2026/10/08 统一成 20261008。"""
    return str(date8).replace("-", "").replace("/", "").strip()[:8]


__all__ = [
    "PORTAL_DIR",
    "VIRTUAL_ROOT",
    "ANCHORS",
    "RULES",
    "REJECTED",
    "PREDICTIONS",
    "CALIBRATION",
    "LEDGER",
    "INCIDENTS",
    "FEATURES_DIR",
    "PLANS_DIR",
    "FILLS_DIR",
    "ensure_dirs",
    "feature_path",
    "plan_path",
    "fills_path",
]
