# -*- coding: utf-8 -*-
"""
portal/premarket/factors/validate.py — 因子统计检验

「高胜率稳定因子」不能靠描述说服人，得有数。这里对每个因子跑**滚动截面**检验：

对最近 `days` 个交易日中的每一天 d：
  1. 用 d 及之前的数据构造 Panel（只用 d 收盘前的信息）
  2. 算因子值 → 该票 d 日的取值
  3. 取 d+1 日的真实收益（未来函数只在这里用，且只用于检验）
  4. 计算截面**秩相关（Spearman IC）**：因子值 vs 次日收益

汇总：
  - `ic_mean` 平均信息系数（|IC|>0.02 才算有点用，>0.05 算不错）
  - `ic_ir` = 均值/标准差（稳定性，>0.3 说明不靠某几天）
  - `ic_pos_ratio` 方向为正的日子占比
  - `long_short_pct` 因子前 10% 组 minus 后 10% 组 的次日平均收益差
  - `coverage` 覆盖率（有因子值的股票数 / 全市场）

注意：这是**离线工具**，不进每日流程（跑一次约几十秒到几分钟）。
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Callable

from .base import Factor, Panel

#: 检验门槛（写死在这里，避免"看起来不错"就放行）
MIN_IC_ABS = 0.015
MIN_IR_ABS = 0.15
MIN_COVERAGE = 0.35
MIN_DAYS = 10


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 30:
        return None
    rx, ry = _rank(x), _rank(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def validate_factor(factor: Factor, dates: list[str],
                    panel_factory: Callable[[str], Panel],
                    fwd: dict[str, dict[str, float]],
                    *, log: Callable[[str], None] = print) -> dict[str, Any]:
    """对单个因子跑滚动截面检验。

    `panel_factory(date)` 返回该日的 Panel（无未来函数）。
    `fwd[date][code]` 是该日收盘买入、下一交易日收盘的收益率（%）。
    """
    ics: list[float] = []
    ls_spreads: list[float] = []
    covers: list[float] = []
    for d in dates:
        p = panel_factory(d)
        if p is None or not p.close:
            continue
        try:
            vals = factor.run(p)
        except Exception as exc:
            log(f"  {factor.id} 在 {d} 计算失败：{type(exc).__name__}: {exc}")
            continue
        fw = fwd.get(d) or {}
        pairs = [(float(v), fw[c]) for c, v in vals.items()
                 if c in fw and isinstance(v, (int, float))]
        if len(pairs) < 100:
            continue
        covers.append(len(pairs) / max(1, len(p.close)))
        xs = [a for a, _ in pairs]
        ys = [b for _, b in pairs]
        ic = spearman(xs, ys)
        if ic is not None:
            ics.append(ic)
        # 多空价差：按因子排序取前/后 10%
        if len(pairs) >= 200:
            srt = sorted(pairs, key=lambda t: t[0])
            k = max(10, len(srt) // 10)
            lo = statistics.fmean([t[1] for t in srt[:k]])
            hi = statistics.fmean([t[1] for t in srt[-k:]])
            ls_spreads.append(hi - lo)

    if not ics:
        return {"id": factor.id, "ok": False, "reason": "样本不足，无法检验",
                "n_days": 0}

    mean = statistics.fmean(ics)
    sd = statistics.pstdev(ics) if len(ics) > 1 else 0.0
    ir = (mean / sd) if sd > 1e-9 else 0.0
    pos = sum(1 for x in ics if x > 0) / len(ics)
    cover = statistics.fmean(covers) if covers else 0.0
    ls = statistics.fmean(ls_spreads) if ls_spreads else None

    reasons = []
    if len(ics) < MIN_DAYS:
        reasons.append(f"有效检验日只有 {len(ics)} 天（<{MIN_DAYS}）")
    if abs(mean) < MIN_IC_ABS:
        reasons.append(f"|IC|={abs(mean):.4f} < {MIN_IC_ABS}")
    if abs(ir) < MIN_IR_ABS:
        reasons.append(f"|IR|={abs(ir):.3f} < {MIN_IR_ABS}")
    if cover < MIN_COVERAGE:
        reasons.append(f"覆盖率 {cover:.0%} < {MIN_COVERAGE:.0%}")
    return {
        "id": factor.id, "name": factor.meta.get("name"),
        "ok": not reasons,
        "reason": "；".join(reasons) if reasons else "通过",
        "n_days": len(ics), "ic_mean": round(mean, 4),
        "ic_ir": round(ir, 3), "ic_pos_ratio": round(pos, 3),
        "coverage": round(cover, 3),
        "long_short_pct": round(ls, 3) if ls is not None else None,
    }


def forward_returns(conn, dates: list[str]) -> dict[str, dict[str, float]]:
    """每个检验日的「次日收益」映射：{date: {code: pct}}。"""
    if not dates:
        return {}
    lo, hi = min(dates), max(dates)
    rows = conn.execute(
        """SELECT code, date, close FROM day
           WHERE date >= date(?, '-5 day') AND date <= date(?, '+10 day')
           ORDER BY code, date""", (lo, hi)).fetchall()
    by_code: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        if r["close"]:
            by_code.setdefault(r["code"], []).append((r["date"], r["close"]))
    want = set(dates)
    out: dict[str, dict[str, float]] = {d: {} for d in dates}
    for code, seq in by_code.items():
        for i in range(len(seq) - 1):
            d, px = seq[i]
            if d in want and px:
                nxt = seq[i + 1][1]
                if nxt:
                    out[d][code] = (nxt / px - 1) * 100
    return out


def run_all(*, days: int = 20, horizon: int = 1, conn=None,
            log: Callable[[str], None] = print,
            ids: list[str] | None = None) -> list[dict[str, Any]]:
    """跑全库检验。返回每个因子的结论列表（按 |IC| 降序）。"""
    from .. import screen
    from . import build_panel, load_factors
    from portal.virtual import marketdb as M

    own = conn is None
    if conn is None:
        conn = M.connect()
    try:
        # 取最近 days+1 个交易日作为检验日（最后一天没有次日收益）
        rows = conn.execute(
            "SELECT DISTINCT date FROM day ORDER BY date DESC LIMIT ?",
            (days + 1,)).fetchall()
        alldates = sorted(r["date"] for r in rows)
        dates = alldates[:-1]
        if not dates:
            return []
        log(f"检验 {len(dates)} 个交易日：{dates[0]} ~ {dates[-1]}")

        fwd = forward_returns(conn, dates)
        cache: dict[str, Panel] = {}

        def panel_factory(d: str) -> Panel | None:
            if d in cache:
                return cache[d]
            # 用该日的历史行情充当"今日截面"（离线回放，不看未来）
            # ★ 必须带 open/high/low：close_pos、gap_pct 这类因子只靠它们算，
            #   少取一列就会让整个因子"覆盖率为 0"（踩过）。
            bars = conn.execute(
                """SELECT code, open, high, low, close, volume, amount
                   FROM day WHERE date = ?""",
                (d,)).fetchall()
            quotes = []
            for b in bars:
                prev = conn.execute(
                    """SELECT close FROM day WHERE code = ? AND date < ?
                       ORDER BY date DESC LIMIT 1""", (b["code"], d)).fetchone()
                if not prev or not prev["close"]:
                    continue
                quotes.append({"code": b["code"], "open": b["open"],
                               "high": b["high"], "low": b["low"],
                               "close": b["close"], "prev_close": prev["close"],
                               "volume": b["volume"], "amount": b["amount"]})
            p = build_panel(conn, quotes, d, history_days=40)
            cache[d] = p
            return p

        factors = load_factors()
        want = ids or [f.id for f in factors.values()]
        results = []
        for fid in want:
            f = factors.get(fid)
            if not f:
                continue
            r = validate_factor(f, dates, panel_factory, fwd, log=log)
            results.append(r)
            log(f"  {fid:<20} IC={r.get('ic_mean')}  IR={r.get('ic_ir')}  "
                f"覆盖={r.get('coverage')}  {'✓' if r.get('ok') else '✗ ' + r.get('reason', '')}")
        results.sort(key=lambda x: -abs(x.get("ic_mean") or 0))
        # ★ 回写缓存，让因子库总表能显示真实回测数字
        try:
            from .catalog import save_stats
            save_stats({r["id"]: r for r in results if r.get("id")})
            from .catalog import write_catalog
            write_catalog(quiet=True)
        except Exception:
            pass
        return results
    finally:
        if own and conn is not None:
            conn.close()


__all__ = ["validate_factor", "run_all", "forward_returns", "spearman",
           "MIN_IC_ABS", "MIN_IR_ABS", "MIN_COVERAGE"]
