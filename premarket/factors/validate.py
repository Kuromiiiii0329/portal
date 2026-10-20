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
#: 默认回测窗口 = 近 10 年
DEFAULT_YEARS = 10
TRADING_DAYS_PER_YEAR = 243
DEFAULT_DAYS = DEFAULT_YEARS * TRADING_DAYS_PER_YEAR  # ≈ 2430


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


def run_all(*, days: int | None = None, horizon: int = 1, conn=None,
            log: Callable[[str], None] = print,
            ids: list[str] | None = None) -> list[dict[str, Any]]:
    """跑全库检验（按日期主序，默认近 10 年）。

    性能设计
    --------
    - **单次窗口查询**：整段区间的日K 一次取出，前收由 SQL 的 LAG 算出，
      不再逐票逐日回查（旧实现 60 天要 31 万次 SQL）。
    - **按日期主序**：一天只建一次 Panel，所有因子共用 —— 加因子几乎免费。
    - **滚动窗口**：每只票维护最近 40 根 K 线，构造 Panel 时直接切片。
    """
    from . import load_factors
    from .panel_iter import iter_panels, trading_dates
    from portal.virtual import marketdb as M

    n_days = int(days or DEFAULT_DAYS)
    own = conn is None
    if conn is None:
        conn = M.connect()
    if conn is None:
        return []

    try:
        alldates = trading_dates(conn, limit=n_days + 1)
        if len(alldates) < 3:
            log("  交易日不足，无法检验")
            return []
        test_dates = alldates[:-1]          # 最后一天只提供"次日收益"
        test_set = set(test_dates)
        log(f"检验 {len(test_dates)} 个交易日：{test_dates[0]} ~ "
            f"{test_dates[-1]}（约 {len(test_dates)/TRADING_DAYS_PER_YEAR:.1f} 年）")

        factors = load_factors()
        want = [fid for fid in (ids or list(factors)) if fid in factors]
        compute_ids = [fid for fid in want if factors[fid].compute]
        expr_ids = [fid for fid in want if not factors[fid].compute]

        acc: dict[str, dict[str, list[float]]] = {
            fid: {"ics": [], "ls": [], "cov": []} for fid in want}

        def settle(day: str, vals: dict[str, dict], rets: dict[str, float],
                   universe: int) -> None:
            if not rets or universe <= 0:
                return
            for fid, series in vals.items():
                items = [(c, float(v)) for c, v in series.items() if c in rets]
                # ★ 覆盖率无条件记录：因子太挑剔时（例如全市场只有 0.3% 满足）
                #   IC 根本算不出来，但"覆盖率多少"正是下一轮最该知道的信息。
                #   以前这行放在 <100 检查之后，导致失败原因只写"样本不足"，
                #   agent 无法判断该放宽多少（踩过）。
                acc[fid]["cov"].append(len(items) / max(1, universe))
                if len(items) < 100:
                    continue
                xs = [v for _, v in items]
                ys = [rets[c] for c, _ in items]
                ic = spearman(xs, ys)
                if ic is not None:
                    acc[fid]["ics"].append(ic)
                if len(items) >= 200:
                    order = sorted(range(len(items)), key=lambda i: items[i][1])
                    k = max(10, len(order) // 10)
                    lo = statistics.fmean(ys[i] for i in order[:k])
                    hi = statistics.fmean(ys[i] for i in order[-k:])
                    acc[fid]["ls"].append(hi - lo)

        prev_day: str | None = None
        prev_vals: dict[str, dict] = {}
        prev_closes: dict[str, float] = {}
        prev_universe = 0
        n_settled = 0

        for d, panel, close_map in iter_panels(conn, alldates, log=None):
            # 1) 先用今天的收盘结算"昨天"的因子
            if prev_day in test_set and prev_closes:
                rets = {}
                for code, px in close_map.items():
                    base = prev_closes.get(code)
                    if base:
                        rets[code] = (px / base - 1) * 100
                settle(prev_day, prev_vals, rets, prev_universe)
                n_settled += 1
                if n_settled % 200 == 0:
                    log(f"    …已结算 {n_settled} 天（当前 {d}）")

            # 2) 再算今天的因子值（两轮：代码因子 → 表达式因子）
            if d in test_set:
                vals: dict[str, dict] = {}
                for fid in compute_ids:
                    try:
                        vals[fid] = factors[fid].run(panel)
                    except Exception:
                        vals[fid] = {}
                for fid in expr_ids:
                    try:
                        vals[fid] = factors[fid].run(panel, env=dict(vals))
                    except Exception:
                        vals[fid] = {}
                prev_vals = vals
                prev_universe = len(panel.close)
            else:
                prev_vals, prev_universe = {}, 0

            prev_day = d
            prev_closes = close_map

        results = []
        for fid in want:
            ic = acc[fid]["ics"]
            f = factors[fid]
            if not ic:
                cov0 = (statistics.fmean(acc[fid]["cov"])
                        if acc[fid]["cov"] else 0.0)
                why = ("每日满足条件的股票数不足 100，IC 无法计算"
                       if cov0 < MIN_COVERAGE else "有效样本日不足")
                results.append({
                    "id": fid, "name": f.meta.get("name"), "ok": False,
                    "n_days": 0, "coverage": round(cov0, 4),
                    "reason": f"样本不足，无法检验（{why}；平均覆盖率 {cov0:.2%}）"})
                log(f"  {fid:<28} 样本不足：平均覆盖率 {cov0:.2%} "
                    f"（条件过严，需放宽阈值）")
                continue
            mean = statistics.fmean(ic)
            sd = statistics.pstdev(ic) if len(ic) > 1 else 0.0
            ir = (mean / sd) if sd > 1e-9 else 0.0
            pos = sum(1 for x in ic if x > 0) / len(ic)
            cov = statistics.fmean(acc[fid]["cov"]) if acc[fid]["cov"] else 0.0
            ls = (statistics.fmean(acc[fid]["ls"]) if acc[fid]["ls"] else None)
            reasons = []
            if len(ic) < MIN_DAYS:
                reasons.append(f"有效检验日只有 {len(ic)} 天（<{MIN_DAYS}）")
            if abs(mean) < MIN_IC_ABS:
                reasons.append(f"|IC|={abs(mean):.4f} < {MIN_IC_ABS}")
            if abs(ir) < MIN_IR_ABS:
                reasons.append(f"|IR|={abs(ir):.3f} < {MIN_IR_ABS}")
            if cov < MIN_COVERAGE:
                reasons.append(f"覆盖率 {cov:.0%} < {MIN_COVERAGE:.0%}")
            row = {"id": fid, "name": f.meta.get("name"),
                   "ok": not reasons,
                   "reason": "；".join(reasons) if reasons else "通过",
                   "n_days": len(ic), "ic_mean": round(mean, 4),
                   "ic_ir": round(ir, 3), "ic_pos_ratio": round(pos, 3),
                   "coverage": round(cov, 3),
                   "long_short_pct": round(ls, 3) if ls is not None else None}
            results.append(row)
            log(f"  {fid:<28} IC={row['ic_mean']:>9} IR={row['ic_ir']:>8} "
                f"覆盖={row['coverage']:<6} "
                f"{'✓' if row['ok'] else '✗ ' + row['reason'][:44]}")
        results.sort(key=lambda x: -abs(x.get("ic_mean") or 0))
        try:
            from .catalog import save_stats, write_catalog
            save_stats({r["id"]: r for r in results if r.get("id")})
            write_catalog(quiet=True)
        except Exception:
            pass
        return results
    finally:
        if own and conn is not None:
            conn.close()


__all__ = ["validate_factor", "run_all", "forward_returns", "spearman",
           "DEFAULT_YEARS", "DEFAULT_DAYS", "TRADING_DAYS_PER_YEAR",
           "MIN_IC_ABS", "MIN_IR_ABS", "MIN_COVERAGE"]
