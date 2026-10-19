# -*- coding: utf-8 -*-
"""
portal/virtual/datafeed.py — 行情数据统一入口（数据库优先，缓存兜底）

为什么要有这一层
----------------
portal 原本的行情来自 `data_cache`（按股票分文件夹的 csv，只有你手动分析过的票）。
回测数据库 `marketdb` 建好之后，全市场都有数据了，但两条路不能各写一套取数逻辑 ——
否则「用哪份数据」本身就会成为一个静默变量。

优先级：

    1. `marketdb`（全市场、带复权列、可回测）
    2. `data_cache`（历史遗留、只有分析过的票、无复权列）

两者返回的 DataFrame 都满足 `backtest_core` 的输入契约；差别只在有没有
`aopen/aclose` 列（有就按两套价格体系走）。

复权口径的诚实说明
------------------
marketdb 存的是**原始价 + 因子事件**，`load_kline` 现算前复权。
前复权的基准是「序列最后一天」，所以同一只票今天取和历史某天取的**数值不同**——
这在做「当时的价格」回放时必须注意：回放只关心比例，不关心绝对价位，因此安全。
但如果你要把历史价格当绝对数值展示，就必须声明复权基准日。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from . import marketdb


def db_path() -> Path:
    return Path(os.environ.get("PORTAL_MARKET_DB") or marketdb.DEFAULT_PATH)


def connect(*, create: bool = False):
    """连数据库。默认不建库 —— 没有库时应**明确降级**，而不是悄悄建个空库。"""
    p = db_path()
    if not p.exists() and not create:
        return None
    conn = marketdb.connect(p)
    if create:
        marketdb.init_schema(conn)
    return conn


def db_ready() -> bool:
    """数据库是否可用且有数据（快速判断，用于决定要不要预热）。"""
    conn = connect()
    if conn is None:
        return False
    try:
        n = conn.execute("SELECT COUNT(*) FROM day").fetchone()[0]
        return int(n or 0) > 0
    except Exception:
        return False
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# 取数
# ─────────────────────────────────────────────────────────────
def kline_from_db(code: str, *, end: str | None = None,
                  lookback: int | None = None, conn=None) -> pd.DataFrame | None:
    """从数据库取一只票的日K（带复权列）。`lookback` 只取最近 N 根。"""
    own = conn is None
    conn = conn or connect()
    if conn is None:
        return None
    try:
        start = None
        if lookback and end:
            # 多取一些自然日再截尾，避免节假日造成不足
            d = pd.to_datetime(str(end)[:8], format="%Y%m%d", errors="coerce")
            if not pd.isna(d):
                start = (d - pd.Timedelta(days=int(lookback * 1.9))).strftime("%Y-%m-%d")
        df = marketdb.load_kline(conn, code, start=start, end=end)
        if df is not None and lookback and len(df) > lookback:
            df = df.tail(lookback).reset_index(drop=True)
        return df if df is not None and len(df) else None
    except Exception:
        return None
    finally:
        if own:
            conn.close()


def kline_from_cache(code: str) -> pd.DataFrame | None:
    """历史遗留路径：portal 的本地 csv 缓存。无复权列。"""
    try:
        from portal.data_cache import StockDataCache
        df = StockDataCache().get_kline(code)
        return df if df is not None and len(df) else None
    except Exception:
        return None


def kline(code: str, *, prefer: str = "db", **kw) -> pd.DataFrame | None:
    """统一取数：数据库优先，缓存兜底。"""
    if prefer == "db":
        df = kline_from_db(code, **kw)
        if df is not None:
            return df
        return kline_from_cache(code)
    df = kline_from_cache(code)
    return df if df is not None else kline_from_db(code, **kw)


def provider(*, prefer: str = "db") -> Callable[[str], pd.DataFrame | None]:
    """给 `dataset.load_rows` 用的 provider 闭包（避免每只票重复开关连接）。"""
    conn = connect()

    def _get(code: str):
        try:
            if prefer == "db" and conn is not None:
                df = kline_from_db(str(code), conn=conn)
                if df is not None:
                    return df
            return kline_from_cache(str(code))
        except Exception:
            return None

    _get.conn = conn  # 便于调用方关闭
    return _get


def load_many(codes: Iterable[str], *, date8: str | None = None,
              lookback: int = 160) -> dict[str, pd.DataFrame]:
    """批量取数（给特征层用）。一次开连接，逐只查。"""
    conn = connect()
    out: dict[str, pd.DataFrame] = {}
    try:
        for c in codes:
            df = (kline_from_db(str(c), end=date8, lookback=lookback, conn=conn)
                  if conn is not None else None)
            if df is None:
                df = kline_from_cache(str(c))
            if df is not None:
                out[str(c)] = df
    finally:
        if conn is not None:
            conn.close()
    return out


# ─────────────────────────────────────────────────────────────
# 横截面能力（只有数据库才提供）
# ─────────────────────────────────────────────────────────────
def daily_limit_up(date8: str, *, conn=None) -> list[dict[str, Any]]:
    """从日K**重建**当日涨停名单（不依赖涨停池接口那 14 天窗口）。

    涨停判据：`收 == 昨收 × (1+幅度)`，四舍五入到分。
    这是《回测避坑手册》§1.3 的口径；幅度用 `marketdb.limit_pct_at`（日期感知）。
    """
    own = conn is None
    conn = conn or connect()
    if conn is None:
        return []
    d = str(date8).replace("-", "")[:8]
    iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    try:
        rows = conn.execute(
            """SELECT d.code, s.name, s.is_st, d.close, d.high
               FROM day d JOIN stock s ON s.code=d.code
               WHERE d.date = ?""", (iso,)).fetchall()
    except Exception:
        return []
    try:
        out = []
        for r in rows:
            prev = conn.execute(
                "SELECT close FROM day WHERE code=? AND date<? ORDER BY date DESC LIMIT 1",
                (r["code"], iso)).fetchone()
            if not prev or not prev["close"]:
                continue
            lim = marketdb.limit_pct_at(r["code"], d, is_st=bool(r["is_st"]))
            up = round(float(prev["close"]) * (1 + lim), 2)
            up_touch = round(float(prev["close"]) * (1 + lim) + 0.0001, 2)
            if abs(float(r["close"]) - up) < 0.005:
                out.append({"code": r["code"], "name": r["name"], "close": float(r["close"]),
                            "prev_close": float(prev["close"]), "limit_up": up,
                            "touch_only": False})
            elif r["high"] and abs(float(r["high"]) - up_touch) < 0.02:
                out.append({"code": r["code"], "name": r["name"], "close": float(r["close"]),
                            "prev_close": float(prev["close"]), "limit_up": up,
                            "touch_only": True})
        return out
    finally:
        if own:
            conn.close()


def db_summary() -> dict[str, Any]:
    """一句话说清「数据库能不能用」，给报告与 CLI 用。"""
    conn = connect()
    if conn is None:
        return {"available": False, "reason": f"找不到 {db_path()}",
                "hint": "python -m portal.virtual.marketdb init && "
                        "python -m portal.virtual.marketdb build"}
    try:
        cov = marketdb.coverage(conn)
        cov["available"] = cov.get("codes_with_data", 0) > 0
        cov["path"] = str(db_path())
        return cov
    finally:
        conn.close()


__all__ = [
    "db_path",
    "connect",
    "db_ready",
    "kline_from_db",
    "kline_from_cache",
    "kline",
    "provider",
    "load_many",
    "daily_limit_up",
    "db_summary",
]
