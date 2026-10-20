# -*- coding: utf-8 -*-
"""
portal/premarket/factors/profile.py — 因子前向收益画像（3/5/10/20/30 日）

IC 回答的是"排序有没有相关性"，是个抽象数字。这个模块回答人真正关心的问题：

    因子发出信号后，买入并持有 3/5/10/20/30 个交易日，
    **胜率多少、平均涨幅多少、比全市场同期好多少**？

口径（写死，可审计）
------------------
- **信号（firing）**：按因子方向取当日截面分位
    direction="high"   → 前 `TOP_PCT`（默认 20%）
    direction="low"    → 后 `TOP_PCT`
    direction="filter" → bool 取 ==1；数值取战法里实际用的阈值（找不到则前 20%）
- **买入价**：信号日收盘（这是"因子值可观测"的那一刻，最保守也最干净）
- **前向收益**：`close[t+h] / close[t] - 1`，h ∈ {3,5,10,20,30}
- **胜率**：前向收益 > 0 的比例
- **基准**：同日全市场等权持有同样天数的平均收益（用来算超额）
- **不计成本**：这是因子画像，不是策略回测；策略回测另有一套（见 portfolio.py）

为什么不用"次日"而用 3~30 日
---------------------------
A 股 T+1，且单日噪声极大。3~30 日的窗口才看得出因子的实际持有价值。
"""
from __future__ import annotations

import statistics
from collections import deque
from typing import Any, Callable

HORIZONS = (3, 5, 10, 20, 30)
#: 取截面的前/后百分之多少作为"信号"
TOP_PCT = 0.20
#: 判定 bool 因子"成立"的取值
BOOL_TRUE = 1


def _pct(v: float | None) -> float | None:
    return None if v is None else v * 100.0


def _threshold_from_plays(fid: str) -> float | None:
    """从战法条件里找出该因子实际使用的阈值（用于 filter 类）。"""
    import re
    from . import playbooks as PB
    pat = re.compile(rf"{re.escape(fid)}\s*(>=|>|<=|<)\s*(-?\d+(?:\.\d+)?)")
    for p in PB.load_all():
        for cond in p.get("conditions") or []:
            m = pat.search(cond)
            if m:
                return float(m.group(2)) * (1 if m.group(1).startswith(">") else -1)
    return None


def _pick(series: dict[str, float], direction: str, *,
          top_pct: float = TOP_PCT) -> dict[str, float]:
    """按方向取当日信号集合。返回 {code: 因子值}。"""
    if not series:
        return {}
    if direction == "filter":
        vals = [v for v in series.values() if v]
        if not vals:
            return {}
        # 全是 0/1 → bool 语义
        if set(series.values()) <= {0, 1}:
            return {c: v for c, v in series.items() if v == BOOL_TRUE}
        # 否则退化为前 20%
        direction = "high"
    n = max(1, int(len(series) * top_pct))
    order = sorted(series.items(), key=lambda kv: kv[1],
                   reverse=(direction != "low"))
    return dict(order[:n])


def forward_profile(conn=None, *, horizons: list[int] | None = None,
                    factor_ids: list[str] | None = None,
                    top_pct: float = TOP_PCT,
                    max_days: int | None = None,
                    log: Callable[[str], None] = print) -> dict[str, Any]:
    """主函数：算每个因子在多个持有期下的胜率与涨幅。

    单遍扫描（日期主序），同一时刻只保留最近 max(horizons) 天的信号，
    内存与窗口长度无关（10 年也只是同一个环形缓冲）。
    """
    from . import load_factors
    from .panel_iter import iter_panels, trading_dates
    from portal.virtual import marketdb as M

    hs = sorted(horizons or HORIZONS)
    hmax = max(hs)
    own = conn is None
    if conn is None:
        conn = M.connect()
    if conn is None:
        return {"ok": False, "error": "market.db 不可用"}

    try:
        factors = load_factors()
        want = [fid for fid in (factor_ids or list(factors)) if fid in factors]
        dates = trading_dates(conn, limit=(max_days + hmax + 1) if max_days else None)
        if len(dates) <= hmax + 1:
            return {"ok": False, "error": "交易日太少"}
        log(f"前向画像：{len(want)} 因子 × 持有期 {hs} × {len(dates)} 个交易日")

        # 累积器：acc[fid][h] = [count, wins, sum, sumsq]
        acc: dict[str, dict[int, list[float]]] = {
            fid: {h: [0, 0, 0.0, 0.0] for h in hs} for fid in want}
        # 基准累积：base[h] = [count, sum, wins]
        base: dict[int, list[float]] = {h: [0, 0.0, 0] for h in hs}

        ring: deque = deque(maxlen=hmax + 1)      # 每项 (date, close_map, fire)
        n_seen = 0

        for d, panel, close_map in iter_panels(conn, dates, log=None):
            n_seen += 1
            # 1) 今天的截面 → 各因子的信号集合
            fire: dict[str, list[tuple[str, float]]] = {}
            for fid in want:
                f = factors[fid]
                try:
                    vals = (f.run(panel) if f.compute
                            else f.run(panel, env={}))
                except Exception:
                    vals = {}
                picked = _pick(vals, f.meta.get("direction") or "high",
                              top_pct=top_pct)
                fire[fid] = [(c, close_map[c]) for c in picked
                             if c in close_map and close_map[c]]
            ring.append((d, close_map, fire))

            # 2) 结算 hmax 天前的信号
            if len(ring) > hmax:
                old_date, old_close, old_fire = ring[0]
                for h in hs:
                    # ★ ring[0] 是 hmax 天前；它之后第 h 天就是 ring[h]。
                    #   第一版写成 ring[len-1-h]，于是 30 日变成"自己比自己"(0.0%)、
                    #   3 日变成 27 日、20 日变成 10 日 —— 持有期全部错位。
                    tgt_idx = h
                    if tgt_idx >= len(ring):
                        continue
                    tgt_close = ring[tgt_idx][1]
                    for fid in want:
                        for code, base_px in old_fire.get(fid) or []:
                            px = tgt_close.get(code)
                            if not px or not base_px:
                                continue
                            r = px / base_px - 1
                            a = acc[fid][h]
                            a[0] += 1
                            a[1] += 1 if r > 0 else 0
                            a[2] += r
                            a[3] += r * r
                    # 基准：全市场等权
                    pairs = [(old_close.get(c), tgt_close.get(c))
                             for c in old_close]
                    rets = [b / a0 - 1 for a0, b in pairs if a0 and b]
                    if rets:
                        bb = base[h]
                        bb[0] += len(rets)
                        bb[1] += sum(rets)
                        bb[2] += sum(1 for x in rets if x > 0)

        # 3) 汇总
        base_mean = {h: (base[h][1] / base[h][0]) if base[h][0] else None
                     for h in hs}
        base_win = {h: (base[h][2] / base[h][0]) if base[h][0] else None
                    for h in hs}
        rows = []
        for fid in want:
            f = factors[fid]
            entry = {"id": fid, "name": f.meta.get("name"),
                     "direction": f.meta.get("direction") or "",
                     "by_h": {}}
            for h in hs:
                a = acc[fid][h]
                if a[0] < 100:
                    entry["by_h"][h] = {"n": int(a[0]), "win_rate": None,
                                        "avg_pct": None, "median_pct": None,
                                        "excess_pct": None}
                    continue
                mean = a[2] / a[0]
                entry["by_h"][h] = {
                    "n": int(a[0]),
                    "win_rate": round(a[1] / a[0], 4),
                    "avg_pct": round(mean * 100, 3),
                    "std_pct": round(((a[3] / a[0]) - mean ** 2) ** 0.5 * 100, 3),
                    "excess_pct": (round((mean - base_mean[h]) * 100, 3)
                                   if base_mean[h] is not None else None),
                }
            # 用 20 日（或最长有数据的）做排序键
            key_h = 20 if 20 in hs else hs[-1]
            entry["_sort"] = (entry["by_h"].get(key_h, {}).get("excess_pct")
                              or -999)
            rows.append(entry)
        rows.sort(key=lambda x: -x["_sort"])
        for r in rows:
            r.pop("_sort", None)
        return {"ok": True, "horizons": hs, "days": n_seen,
                "top_pct": top_pct,
                "baseline": {h: {"avg_pct": (round(base_mean[h] * 100, 3)
                                             if base_mean[h] is not None else None),
                                 "win_rate": (round(base_win[h], 4)
                                              if base_win[h] is not None else None),
                                 "n": int(base[h][0])}
                             for h in hs},
                "factors": rows}
    finally:
        if own and conn is not None:
            conn.close()


def render_markdown(prof: dict[str, Any]) -> str:
    hs = prof["horizons"]
    out = ["# 因子前向收益画像（信号后持有 N 个交易日）", "",
           f"> 信号口径：按因子方向取当日截面前 {prof['top_pct']:.0%}"
           f"（filter 类取 ==1 或战法阈值）", 
           f"> 样本 {prof['days']} 个交易日　不计交易成本", ""]
    base = prof["baseline"]
    out += ["## 基准（全市场等权持有同样天数）", "",
            "| 持有 | 平均涨幅 | 胜率 | 样本 |", "|---|---|---|---|"]
    for h in hs:
        b = base[h]
        out.append(f"| {h} 日 | {b['avg_pct']}% | {b['win_rate']} | {b['n']} |")
    out += ["", "## 各因子前向表现", "",
            "| 因子 | 方向 | 持有 | 样本 | 胜率 | 平均涨幅 | 超额(vs 基准) |",
            "|---|---|---|---|---|---|---|"]
    for r in prof["factors"]:
        for h in hs:
            d = r["by_h"][h]
            if d["win_rate"] is None:
                out.append(f"| `{r['id']}` | {r['direction']} | {h} 日 | "
                           f"{d['n']} | — | — | 样本不足 |")
                continue
            out.append(f"| `{r['id']}` | {r['direction']} | {h} 日 | {d['n']} | "
                       f"{d['win_rate']:.1%} | {d['avg_pct']:+.2f}% | "
                       f"{d['excess_pct']:+.2f}% |")
    out.append("")
    return "\n".join(out)


__all__ = ["forward_profile", "render_markdown", "HORIZONS", "TOP_PCT"]
