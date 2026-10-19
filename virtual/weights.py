# -*- coding: utf-8 -*-
"""
portal/virtual/weights.py — 维度权重回测（只回测得起的那部分）

诚实的边界，先说清楚
--------------------
`merger.DIMENSION_WEIGHTS` 现在是 技术 0.35 / 基本面 0.30 / 产业链 0.15 / 游资 0.20。
这四个数**从来没有被回测过** —— 它们来自方案的初始设定。

要严肃回测权重，需要一个「历史面板」：每个交易日 × 每只候选股 × 各维度评分 → 前瞻收益。
问题在于：

| 维度 | 历史评分能否复现 | 原因 |
|---|---|---|
| 技术面 | ✅ 能 | 只要日K，指标可重算 |
| 游资情绪 | ✅ 能（部分） | 涨停/连板/位置可重建；**封单结构不可** |
| 基本面 | ❌ 不能 | 需要历史财报快照，数据库里没有 |
| 产业链 | ❌ 不能 | 需要历史板块归属 + 政策文本 |

所以本模块只回测**技术 vs 游资**这一对的相对权重，并**明确报告**：
基本面与产业链的权重仍属未验证假设。这比编一个四维权重的最优解诚实得多。

技术面评分是「代理分」
----------------------
`technical_proxy_score()` 不是 `analyzers/technical` 的复刻，而是一个**显式声明的
简化加性模型**（均线排列 / MACD / RSI / 布林 / 量比各给固定分）。它只用于比较
「技术权重相对游资权重该高还是该低」，不能用来宣称技术面本身有多大 alpha。

判据（与闸门口径一致）
----------------------
- 主指标：**买入组**（综合分 ≥ 阈值）的平均净收益，减去同面板无条件均值
- 同时给中位数、胜率、t 值
- 训练/测试切分：前半段选权重，后半段验证（不拿全样本挑最优）
"""
from __future__ import annotations

import datetime
import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import pandas as pd

from . import datafeed, scan

try:
    from portal import backtest_core as core
except ImportError:  # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import backtest_core as core  # type: ignore

#: 权重网格（技术, 游资）。两者之和归一到 1，所以只需要扫一个比例。
TECH_WEIGHT_GRID = (0.0, 0.2, 0.3, 0.35, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0)
#: 综合分达到多少算「买入」
BUY_THRESHOLD = 70

#: 当前生效权重（与 merger 保持一致，用于对照）
CURRENT_WEIGHTS = {"technical": 0.35, "fundamental": 0.30,
                   "industry": 0.15, "yuzi": 0.20}


# ─────────────────────────────────────────────────────────────
# 代理评分
# ─────────────────────────────────────────────────────────────
def technical_proxy_score(df: pd.DataFrame, idx: int) -> float | None:
    """技术面代理分（0-100）。**显式声明为简化模型，不是 analyzer 的复刻。**

    只用 `df.iloc[:idx+1]`（防未来函数）。任一关键指标缺失返回 None（不猜）。
    """
    if df is None or idx < 20 or idx >= len(df):
        return None
    sub = df.iloc[: idx + 1]
    try:
        enriched = core.compute_indicators(core.prepare_frame(sub)[0])
    except Exception:
        return None
    if enriched.empty:
        return None
    row = enriched.iloc[-1]
    prev = enriched.iloc[-2] if len(enriched) > 1 else row

    need = ("ma5", "ma10", "ma20", "dif", "dea", "rsi6", "boll_lower", "_ret_close")
    if any(pd.isna(row.get(k)) for k in need):
        return None

    s = 50.0
    ma5, ma10, ma20 = float(row["ma5"]), float(row["ma10"]), float(row["ma20"])
    if ma5 > ma10 > ma20:
        s += 10
    elif ma5 < ma10 < ma20:
        s -= 10

    dif, dea = float(row["dif"]), float(row["dea"])
    pdif, pdea = float(prev["dif"]), float(prev["dea"])
    if dif > dea and pdif <= pdea:
        s += 8
    elif dif < dea and pdif >= pdea:
        s -= 8

    rsi = float(row["rsi6"])
    if rsi < 30:
        s += 6
    elif rsi > 70:
        s -= 6

    close = float(row["_ret_close"])
    if close < float(row["boll_lower"]):
        s += 6

    vr = row.get("vol_ratio")
    if pd.notna(vr):
        if float(vr) > 2 and float(row.get("close_chg") or 0) > 0:
            s += 5
        elif float(vr) > 2 and float(row.get("close_chg") or 0) < 0:
            s -= 5

    return float(max(0.0, min(100.0, s)))


def yuzi_proxy_score(*, boards: float | None, zone_pos_120: float | None,
                     sealed: bool) -> float | None:
    """游资/情绪代理分（0-100）。

    只用可重建的事实：连板数、120 日区间位、是否收盘封住。
    **不含封单/流通**（日K里没有），所以它不是完整度上的 `YuziAnalyzer`。
    """
    if not sealed or boards is None:
        return None
    s = 50.0
    if boards <= 1:
        s += 6
    elif boards >= 3:
        s -= 8 * min(boards - 2, 3)
    if zone_pos_120 is not None:
        if zone_pos_120 < 0.30:
            s += 10
        elif zone_pos_120 > 0.80:
            s -= 10
    return float(max(0.0, min(100.0, s)))


# ─────────────────────────────────────────────────────────────
# 面板构建
# ─────────────────────────────────────────────────────────────
def build_panel(
    conn,
    start: str,
    end: str,
    *,
    horizon: int = 1,
    max_days: int | None = None,
    progress=None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """构建「每日 × 涨停股」的面板：各维度代理分 + 前瞻净收益。"""
    scan_res = scan.scan_range(conn, start, end)
    days = sorted(scan_res)
    if max_days:
        days = days[-max_days:]
    if not days:
        return [], {"note": "区间内重建不出涨停股（数据库覆盖不足或区间无涨停）"}

    from . import warmup
    rows: list[dict[str, Any]] = []
    dropped = {"no_kline": 0, "no_tech": 0, "no_yuzi": 0, "no_return": 0,
               "limit_up_open": 0}
    for i, d in enumerate(days, 1):
        # ★ scan_range 的 key 是 ISO 日期（2026-01-06），这里必须先归一成 8 位，
        #   否则 f"{d[:4]}-{d[4:6]}-{d[6:8]}" 会拼出 "2026-0:1-" 这种垃圾，
        #   之后按日期匹配那一行永远失败 → 整个面板 0 行（实测踩到，120 天全 drop）。
        d8 = str(d).replace("-", "").replace("/", "")[:8]
        iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"
        zt = scan_res[d]["zt"]
        codes = [x["code"] for x in zt]
        klines = datafeed.load_many(codes, date8=d8, lookback=180)
        for x in zt:
            df = klines.get(x["code"])
            if df is None or len(df) == 0:
                dropped["no_kline"] += 1
                continue
            pos = df.index[df["date"].astype(str).str[:10] == iso]
            if len(pos) == 0:
                dropped["no_kline"] += 1
                continue
            idx = int(pos[-1])
            tech = technical_proxy_score(df, idx)
            if tech is None:
                dropped["no_tech"] += 1
                continue
            seg = df.iloc[max(0, idx - 119): idx + 1]
            zone = _zone_pos(seg)
            yz = yuzi_proxy_score(boards=float(x.get("boards") or 1),
                                  zone_pos_120=zone, sealed=bool(x.get("sealed")))
            if yz is None:
                dropped["no_yuzi"] += 1
                continue
            fwd = core.forward_return_after(df, d8, horizon, code=x["code"])
            if fwd is None:
                dropped["no_return"] += 1
                continue
            if fwd.get("excluded") == "limit_up_open":
                dropped["limit_up_open"] += 1
                continue
            rows.append({
                "date": iso, "code": x["code"], "boards": x.get("boards"),
                "zone_pos_120": zone,
                "technical_score": round(tech, 1),
                "yuzi_score": round(yz, 1),
                "ret_pct": fwd["net_pct"],
            })
        if progress and (i % 20 == 0 or i == len(days)):
            progress(f"  面板进度 [{i}/{len(days)}] 行数 {len(rows)}")

    meta = {
        "days": len(days), "rows": len(rows), "dropped": dropped,
        "horizon": horizon, "cost_pct": core.COST_ROUND_TRIP * 100,
        "dims": ["technical", "yuzi"],
        "not_backtestable": ["fundamental", "industry"],
        "note": ("只回测技术 vs 游资的相对权重。基本面/产业链权重仍属未验证假设 —— "
                 "因为数据库里没有历史财报快照与历史板块归属。"),
    }
    return rows, meta


def _zone_pos(seg: pd.DataFrame) -> float | None:
    if seg is None or len(seg) == 0:
        return None
    col = "aclose" if "aclose" in seg.columns else "close"
    hi_col = "ahigh" if "ahigh" in seg.columns else "high"
    lo_col = "alow" if "alow" in seg.columns else "low"
    hi, lo = float(seg[hi_col].max()), float(seg[lo_col].min())
    c = float(seg[col].iloc[-1])
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
        return None
    return round((c - lo) / (hi - lo), 4)


# ─────────────────────────────────────────────────────────────
# 评估
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class WeightEval:
    tech: float
    yuzi: float
    n: int
    mean_pct: float | None
    median_pct: float | None
    buy_n: int = 0
    buy_mean_pct: float | None = None
    buy_median_pct: float | None = None
    buy_winrate: float | None = None
    excess_pp: float | None = None
    t_stat: float | None = None
    ic: float | None = None
    cutoff: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": {"technical": self.tech, "yuzi": self.yuzi},
            "n": self.n,
            "all_mean_pct": self.mean_pct,
            "all_median_pct": self.median_pct,
            "buy_n": self.buy_n,
            "buy_mean_pct": self.buy_mean_pct,
            "buy_median_pct": self.buy_median_pct,
            "buy_winrate": self.buy_winrate,
            "cutoff": self.cutoff,
            "ic": self.ic,
            "excess_pp": self.excess_pp,
            "t_stat": self.t_stat,
            "note": self.note,
        }


def combined_score(rows: list[dict], tech_w: float, yuzi_w: float) -> np.ndarray:
    """按权重混合两维分数。权重和归一到 1（与 merger 的「只对有效维度加权」一致）。"""
    total = tech_w + yuzi_w
    if total <= 0:
        return np.full(len(rows), 50.0)
    t = np.array([float(r["technical_score"]) for r in rows])
    y = np.array([float(r["yuzi_score"]) for r in rows])
    return (t * tech_w + y * yuzi_w) / total


def _rank(x: np.ndarray) -> np.ndarray:
    """平均秩（处理并列），用于 Spearman。避免引入 scipy 依赖。"""
    order = x.argsort(kind="mergesort")
    ranks = np.empty(len(x), dtype=float)
    ranks[order] = np.arange(1, len(x) + 1, dtype=float)
    # 并列取平均秩
    uniq, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    if len(uniq) < len(x):
        sums = np.zeros(len(uniq))
        np.add.at(sums, inv, ranks)
        ranks = (sums / counts)[inv]
    return ranks


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 5 or len(b) != len(a):
        return None
    ra, rb = _rank(np.asarray(a, float)), _rank(np.asarray(b, float))
    sa, sb = ra.std(), rb.std()
    if sa <= 0 or sb <= 0:
        return None
    return float(((ra - ra.mean()) * (rb - rb.mean())).mean() / (sa * sb))


def evaluate(rows: list[dict], tech: float, yuzi: float,
             *, select: str = "quantile", quantile: float = 0.2,
             buy_threshold: float = BUY_THRESHOLD) -> WeightEval:
    """评估一个权重组合。

    为什么默认按**分位**选股而不是固定阈值 70：
    代理分的绝对水平没有校准到真实 analyzer 的分布上（技术代理分上限约 74，
    游资代理分上限约 66），固定 70 分几乎选不出样本 —— 那样测的就不是权重，
    而是阈值。按分位选「分数最高的 20%」测的是**排序能力**，这才是权重该负责的事。
    """
    if not rows:
        return WeightEval(tech, yuzi, 0, None, None, note="空面板")
    rets = np.array([float(r["ret_pct"]) for r in rows])
    score = combined_score(rows, tech, yuzi)
    all_mean = float(np.mean(rets))
    ev = WeightEval(
        tech=tech, yuzi=yuzi, n=len(rows),
        mean_pct=round(all_mean, 3),
        median_pct=round(float(np.median(rets)), 3),
        ic=round(spearman(score, rets), 4) if spearman(score, rets) is not None else None,
    )

    if select == "quantile":
        q = min(max(quantile, 0.01), 0.9)
        cutoff = float(np.quantile(score, 1.0 - q))
        mask = score >= cutoff
    else:
        cutoff = buy_threshold
        mask = score >= buy_threshold
    ev.cutoff = round(cutoff, 2)

    if int(mask.sum()) >= 3:
        sel = rets[mask]
        ev.buy_n = int(len(sel))
        ev.buy_mean_pct = round(float(np.mean(sel)), 3)
        ev.buy_median_pct = round(float(np.median(sel)), 3)
        ev.buy_winrate = round(float((sel > 0).sum() / len(sel) * 100), 1)
        ev.excess_pp = round(float(np.mean(sel) - all_mean), 3)
        sd = float(np.std(sel, ddof=1))
        ev.t_stat = round(float(np.mean(sel) / (sd / math.sqrt(len(sel)))), 2) if sd > 0 else None
    elif int(mask.sum()) > 0:
        ev.note = f"选中只有 {int(mask.sum())} 条，不足 3 条，不做统计"
    else:
        ev.note = f"cutoff {ev.cutoff} 下没有任何样本（权重对该面板没有区分度）"
    return ev


def grid_search(rows: list[dict], grid: Iterable[float] = TECH_WEIGHT_GRID,
                *, select: str = "quantile", quantile: float = 0.2,
                buy_threshold: float = BUY_THRESHOLD) -> list[WeightEval]:
    """扫权重网格。按「选中组超额」降序，同时给 IC —— 两者都要看。"""
    out = [evaluate(rows, t, round(1.0 - t, 4), select=select, quantile=quantile,
                    buy_threshold=buy_threshold) for t in grid]
    out.sort(key=lambda e: (e.excess_pp if e.excess_pp is not None else -9e9), reverse=True)
    return out


def walk_forward(rows: list[dict], *, split_ratio: float = 0.6,
                 select: str = "quantile", quantile: float = 0.2) -> dict[str, Any]:
    """训练/测试切分：前半段挑权重，后半段验证。

    这是防止「全样本挑最优」的唯一办法 —— 那等于抽卡。
    """
    if len(rows) < 60:
        return {"ok": False, "note": f"样本仅 {len(rows)} 条，不足以做切分验证"}
    rows = sorted(rows, key=lambda r: (r["date"], r["code"]))
    cut = int(len(rows) * split_ratio)
    train, test = rows[:cut], rows[cut:]

    train_res = grid_search(train, select=select, quantile=quantile)
    best = train_res[0]
    if best.excess_pp is None:
        return {"ok": False, "note": "训练集里没有任何权重组合能选出样本",
                "train_rows": len(train), "test_rows": len(test)}

    test_ev = evaluate(test, best.tech, best.yuzi, select=select, quantile=quantile)
    baseline = evaluate(test, 0.5, 0.5, select=select, quantile=quantile)

    return {
        "ok": True,
        "train_rows": len(train), "test_rows": len(test),
        "split_ratio": split_ratio,
        "selection": f"{select} {quantile:.0%}" if select == "quantile" else f"score>={quantile}",
        "best_on_train": best.to_dict(),
        "same_on_test": test_ev.to_dict(),
        "balanced_baseline_on_test": baseline.to_dict(),
        "verdict": _verdict(best, test_ev, baseline),
    }


def _verdict(best: WeightEval, test: WeightEval, baseline: WeightEval) -> str:
    if test.buy_n < 30:
        return (f"测试集选中组仅 {test.buy_n} 条（<30），不做任何结论 —— "
                f"样本量不够，权重之争没有答案")
    if test.excess_pp is None:
        return "测试集选不出样本，权重无法验证"
    if test.excess_pp > 0 and (test.t_stat or 0) > 2:
        return (f"训练集选出的权重在测试集上仍有 +{test.excess_pp}pp 超额"
                f"（t={test.t_stat}），可以作为候选，但仍需过 G1–G5")
    return (f"训练集选出的权重在测试集上没有复现（超额 {test.excess_pp}pp，"
            f"t={test.t_stat}）→ 属于过拟合，不应采用")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="portal.virtual.weights")
    ap.add_argument("--start", default="20260101")
    ap.add_argument("--end", default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--buy-threshold", type=float, default=BUY_THRESHOLD)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    conn = datafeed.connect()
    if conn is None:
        print("找不到回测数据库。先跑：python -m portal.virtual.marketdb build")
        return 2
    end = args.end or str(conn.execute("SELECT MAX(date) FROM day").fetchone()[0]).replace("-", "")[:8]

    rows, meta = build_panel(conn, args.start, end, horizon=args.horizon,
                             max_days=args.days, progress=print)
    if not rows:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 1

    full = [e.to_dict() for e in grid_search(rows, select="quantile", quantile=0.2)]
    wf = walk_forward(rows, select="quantile", quantile=0.2)
    out = {"meta": meta, "grid": full, "walk_forward": wf,
           "current_weights_for_reference": CURRENT_WEIGHTS}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        print(f"\n按分位选股（前 20%），比较各权重组合的排序能力：")
        print(f"{'技术':>6}{'游资':>6}{'选中n':>7}{'选中均值':>10}"
              f"{'中位':>8}{'胜率':>7}{'超额pp':>9}{'t':>7}{'IC':>8}")
        for e in full:
            print(f"{e['weights']['technical']:>6.2f}{e['weights']['yuzi']:>6.2f}"
                  f"{e['buy_n']:>7}{str(e['buy_mean_pct']):>10}"
                  f"{str(e['buy_median_pct']):>8}{str(e['buy_winrate']):>7}"
                  f"{str(e['excess_pp']):>9}{str(e['t_stat']):>7}{str(e['ic']):>8}")
        print("\n走查验证（训练集挑权重 → 测试集验证）：")
        print(json.dumps(wf, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
