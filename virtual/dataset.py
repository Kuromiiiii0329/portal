# -*- coding: utf-8 -*-
"""
portal/virtual/dataset.py — 把「特征」和「结果」拼成演化器能吃的行

演化器（`evolve.py`）只吃一种东西：

    [{"zone_pos_120": 0.12, "date": "20261008", "ret_pct": 1.23, ...}, ...]

本模块负责把两段数据拼起来：

    特征（tagging 产出的 pre 字段）  +  可执行收益（backtest_core 的 T+1 口径）

**收益绝不在这里自己算** —— 一律调 `backtest_core.forward_return_after()`。
原因：口径分叉本身就是静默错误的来源，P0 刚为此踩过一次坑。

关键取舍：后视收益是「可以事后补」的，但**当时的市场环境补不了**。
所以特征必须先落盘（pre 阶段），收益事后回填 —— 顺序反过来就会泄漏未来。
"""
from __future__ import annotations

import datetime
from typing import Any, Callable, Iterable

import pandas as pd

from . import tagging

try:  # 口径唯一来源
    from portal import backtest_core as core
except ImportError:  # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import backtest_core as core  # type: ignore


def _default_kline_provider(code: str) -> pd.DataFrame | None:
    """默认从 portal 的本地缓存取日K；不可用就返回 None（不猜）。"""
    try:
        from portal.data_cache import StockDataCache
        df = StockDataCache().get_kline(code)
        return df if df is not None and len(df) else None
    except Exception:
        return None


def load_klines(codes: Iterable[str],
                provider: Callable[[str], pd.DataFrame | None] | None = None
                ) -> dict[str, pd.DataFrame]:
    provider = provider or _default_kline_provider
    out: dict[str, pd.DataFrame] = {}
    for code in codes:
        try:
            df = provider(str(code))
        except Exception:
            df = None
        if df is not None and len(df):
            out[str(code)] = df
    return out


def build_rows(
    docs: Iterable[dict],
    klines_by_code: dict[str, pd.DataFrame],
    *,
    horizon: int = 1,
    cost: float = core.COST_ROUND_TRIP,
    keep_excluded: bool = False,
) -> tuple[list[dict], dict[str, int]]:
    """把特征文档摊平成演化器输入行。

    Returns:
        (rows, stats)；stats 记录每种丢弃原因的数量（不许静默丢样本）。
    """
    rows: list[dict] = []
    stats = {"no_kline": 0, "no_return": 0, "limit_up_open": 0, "kept": 0}

    for doc in docs or []:
        date8 = str(doc.get("date") or "")
        for stock in doc.get("stocks") or []:
            code = str(stock.get("code") or "")
            klines = klines_by_code.get(code)
            if klines is None:
                stats["no_kline"] += 1
                continue
            fwd = core.forward_return_after(klines, date8, horizon, code=code, cost=cost)
            if fwd is None:
                stats["no_return"] += 1
                continue
            if fwd.get("excluded") == "limit_up_open":
                stats["limit_up_open"] += 1
                if not keep_excluded:
                    continue
            row = tagging.flatten_row(doc, stock)
            row["ret_pct"] = fwd["net_pct"]
            row["ret_gross_pct"] = fwd["gross_pct"]
            row["entry_date"] = fwd["entry_date"]
            row["exit_date"] = fwd["exit_date"]
            row["horizon"] = horizon
            rows.append(row)
            stats["kept"] += 1

    return rows, stats


def load_features(dates: Iterable[str] | None = None) -> list[dict]:
    """读已落盘的 pre 特征。`dates=None` 时读全部。"""
    if dates is None:
        days = tagging.list_days()
    else:
        days = [str(d).replace("-", "")[:8] for d in dates]
    out = []
    for d in days:
        doc = tagging.read_features(d)
        if doc is not None:
            out.append(doc)
    return out


def load_rows(
    dates: Iterable[str] | None = None,
    *,
    horizon: int = 1,
    provider: Callable[[str], pd.DataFrame | None] | None = None,
    cost: float = core.COST_ROUND_TRIP,
) -> tuple[list[dict], dict[str, int], dict[str, Any]]:
    """一步到位：读特征 → 取K线 → 拼行。返回 (rows, stats, meta)。"""
    docs = load_features(dates)
    codes = {str(s.get("code")) for d in docs for s in (d.get("stocks") or [])}
    klines = load_klines(codes, provider)
    rows, stats = build_rows(docs, klines, horizon=horizon, cost=cost)
    meta = {
        "feature_days": len(docs),
        "codes_in_features": len(codes),
        "codes_with_kline": len(klines),
        "horizon": horizon,
        "cost_pct": cost * 100,
        "caliber": "T+1 开盘买入，持有 horizon 个交易日后收盘卖出",
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return rows, stats, meta


__all__ = [
    "load_klines",
    "build_rows",
    "load_features",
    "load_rows",
]
