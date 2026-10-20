# -*- coding: utf-8 -*-
"""
portal/premarket/factors/panel_iter.py — 高效面板迭代（消灭 N+1）

问题
----
第一版 `run_all` 对每个检验日、每只股票各查一次"前收盘"：
一个 5200 只的市场 × 60 天 = **31 万次 SQL**。所以 60 天要 1.5 分钟。

改法
----
1. **一次窗口查询**：用 SQL 的 `LAG(close) OVER (PARTITION BY code ORDER BY date)`
   直接算出前收，整段区间只查一次（分块，避免内存爆掉）。
2. **滚动窗口**：按日期升序推进，每只票维护最近 40 根 K 线；
   构造 Panel 时直接切片，不再回查历史。
3. **按日期主序**：一天只建一次 Panel，然后跑所有因子 ——
   这顺带解释了为什么"加因子几乎免费"。

内存
----
同一时刻只有 1 个 Panel + 每只票 40 根滚动缓存（约 30 MB），
所以跑到 10 年（~2400 个交易日）也不会涨。
"""
from __future__ import annotations

from collections import deque
from typing import Any, Iterator

from .base import Panel

#: 因子需要的历史长度：MA20 与 20 日箱体最多用到 21 根，取 25 留余量。
#: （原来 40 是浪费：每天要为每只票复制 4 条 40 长的序列，是主要开销）
HIST_LEN = 25
#: 每次查询覆盖多少个交易日（控制内存；一个块 ≈ 60 天 × 5200 只）
CHUNK_DAYS = 60


def _q(conn, sql: str, args=()) -> list[Any]:
    return conn.execute(sql, args).fetchall()


def trading_dates(conn, *, end: str | None = None, limit: int | None = None
                  ) -> list[str]:
    """升序返回交易日（可选只取最后 limit 个）。"""
    if limit:
        rows = _q(conn, """SELECT date FROM (SELECT DISTINCT date FROM day
                           WHERE (? IS NULL OR date <= ?)
                           ORDER BY date DESC LIMIT ?) ORDER BY date""",
                  (end, end, limit))
    else:
        rows = _q(conn, """SELECT DISTINCT date FROM day
                           WHERE (? IS NULL OR date <= ?) ORDER BY date""",
                  (end, end))
    return [r["date"] for r in rows]


def iter_panels(conn, dates: list[str], *, hist_len: int = HIST_LEN,
                log=None) -> Iterator[tuple[str, Panel, dict[str, float]]]:
    """按日期升序产出 (date, Panel, 当日收盘 map)。

    Panel 里已包含该日截面 + 该日之前 `hist_len` 根历史，可直接喂给因子。
    """
    if not dates:
        return
    # 滚动缓存：code -> 历史序列
    hist: dict[str, dict[str, deque]] = {}
    # 上一交易日的截面（用于前收）
    prev_close: dict[str, float] = {}

    def ensure(code: str) -> dict[str, deque]:
        rec = hist.get(code)
        if rec is None:
            rec = {"close": deque(maxlen=hist_len), "high": deque(maxlen=hist_len),
                   "low": deque(maxlen=hist_len), "vol": deque(maxlen=hist_len)}
            hist[code] = rec
        return rec

    dateset = set(dates)
    first, last = dates[0], dates[-1]
    # 块按"最后一个检验日"推进；每块额外带上块首之前的历史
    idx = 0
    while idx < len(dates):
        chunk = dates[idx:idx + CHUNK_DAYS]
        idx += CHUNK_DAYS
        c_first, c_last = chunk[0], chunk[-1]
        # 历史起点：块首之前再多取 hist_len 个交易日
        lead = _q(conn, """SELECT date FROM (SELECT DISTINCT date FROM day
                           WHERE date < ? ORDER BY date DESC LIMIT ?)
                           ORDER BY date""", (c_first, hist_len))
        start = lead[0]["date"] if lead else c_first
        # ★ 单次查询：整段区间的日K + SQL 自算前收
        rows = _q(conn, """
            SELECT code, date, open, high, low, close, volume, amount,
                   LAG(close) OVER (PARTITION BY code ORDER BY date) AS prev_close
            FROM day
            WHERE date >= ? AND date <= ?
            ORDER BY date, code
        """, (start, c_last))
        by_date: dict[str, list[Any]] = {}
        for r in rows:
            by_date.setdefault(r["date"], []).append(r)

        for d in dates_in(by_date):
            # ★ 关键：块首之前的日期只用来喂滚动历史，**绝不能 yield**。
            #   第一版只判了 `d not in dateset`，但分块（第 2 块起）的 lead-in
            #   日期本身就在 dateset 里，于是被当成检验日重复结算 ——
            #   days=60 却报出 100 个有效日（踩过）。
            if d < c_first:
                for r in by_date.get(d, []):
                    if r["close"]:
                        rec = ensure(r["code"])
                        rec["close"].append(r["close"])
                        rec["high"].append(r["high"])
                        rec["low"].append(r["low"])
                        rec["vol"].append(r["volume"])
                prev_close = {r["code"]: r["close"]
                              for r in by_date.get(d, []) if r["close"]}
                continue
            if d > c_last:
                continue
            bars = by_date.get(d) or []
            p = Panel(asof=d)
            for r in bars:
                code = r["code"]
                c = r["close"]
                if not c:
                    continue
                p.close[code] = c
                pc = r["prev_close"] if r["prev_close"] else prev_close.get(code)
                if pc:
                    p.prev_close[code] = pc
                    p.chg[code] = (c / pc - 1) * 100
                if r["volume"]:
                    p.volume[code] = r["volume"]
                if r["amount"]:
                    p.amount_yi[code] = r["amount"] / 1e8
                if r["open"]:
                    p.open_today[code] = r["open"]
                if r["high"]:
                    p.high_today[code] = r["high"]
                if r["low"]:
                    p.low_today[code] = r["low"]
                h = hist.get(code)
                if h and h["close"]:
                    p.hist[code] = {"close": list(h["close"]),
                                    "high": list(h["high"]),
                                    "low": list(h["low"]),
                                    "vol": list(h["vol"])}
                p.limit_pct[code] = 20.0 if code[:2] in ("68", "30") else 10.0
            # 把当日并入滚动历史，供后续日期使用
            for r in bars:
                if not r["close"]:
                    continue
                rec = ensure(r["code"])
                rec["close"].append(r["close"])
                rec["high"].append(r["high"])
                rec["low"].append(r["low"])
                rec["vol"].append(r["volume"])
            yield d, p, {r["code"]: r["close"] for r in bars if r["close"]}
            prev_close = {r["code"]: r["close"] for r in bars if r["close"]}
            if log and dateset and d == c_last:
                log(f"    …已处理到 {d}")


def dates_in(by_date: dict[str, list]) -> list[str]:
    return sorted(by_date)


__all__ = ["iter_panels", "trading_dates", "HIST_LEN", "CHUNK_DAYS"]
