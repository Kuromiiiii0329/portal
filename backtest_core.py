# -*- coding: utf-8 -*-
"""
portal/backtest_core.py — 口径正确的信号回测内核

本模块替换 portal 旧 `backtester.py` 的收益计算口径。旧版有 6 处「不报错、只出错数」
的缺陷，全部来自《回测避坑手册》（A股量化回测工具包）与《净值口径说明》：

    1. 入场时点错：旧版用 close[T+d]/close[T]，把「T 日收盘 → T+1 开盘」的隔夜跳空
       也算了进去，而实盘是 T+1 开盘才买得到 —— 这段吃不到。
       实测同类错误曾把某策略算成 +21,248%（手册 §3.1）。
    2. 违反 T+1：旧版 d=1 等于「当日买当日卖」，A 股物理上不允许。
    3. 完全不扣成本：单次往返 0.10%（印花税 0.05% + 佣金双边 0.05%）。
       成本只在买卖两个时点扣一次，不是时间费用（手册 §3.3，错法实测差 24%）。
    4. 无基准、无超额：牛市里「赚 20%」可能是跑输（手册 §4.2）。
    5. 只有均值与胜率：均值正、中位负是最常见的骗局（手册 §4.1）。
    6. 无分年稳定性检验：只在一年成立的是行情特征，不是规律（手册 §4.5）。

另外两条本仓库特有的事实（已实测确认，见 UPGRADE_PLAN.md）：

    7. 绝不使用 pct_chg 字段。portal 的 kline 缓存里该字段在增量批次边界会被静默写成
       0（实测 6/3695 行：2026-08-25、2026-09-07 三天全部错），用它会静默算错。
       所有收益一律由 close/open 现算。
    8. 必须剔除「次日开盘即涨停」的样本并报告剔除数。这类样本物理上买不进，
       留在回测里会系统性高估（打板工具包实测：一字板次日 +6.93% 但根本买不到）。

公开 API：
    run_backtest(df, forward_days=(1,3,5,10,20), code=None) -> dict
        返回结构与旧版 **向后兼容**（signals/name/count/last_date/stats/win_rate/
        avg_return/valid/stock_days/total_days 全部保留），并**新增**中位数、t 值、
        超额、分年、剔除计数、口径声明、基准、数据体检。
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────
# 口径常量
# ─────────────────────────────────────────────────────────────
COST_ROUND_TRIP = 0.0010   # 单次往返：印花税 0.05% + 佣金双边 0.05%
MIN_SAMPLES = 30           # 低于此数不得下任何统计结论（手册 §4.4）
SMALL_SAMPLE = 100         # 低于此数标注「样本偏少」
DEFAULT_HORIZONS = (1, 3, 5, 10, 20)

# 单日移动超过「涨跌停幅 × 此倍数」→ 判定为复权断裂/脏数据
ANOMALY_MULTIPLIER = 1.5

CALIBER_VERSION = 2
"""口径版本号。任何会改变收益数字的口径改动都必须 +1。

为什么必须有：`data_cache.save_backtest/get_backtest` 会把回测结果落盘复用。
若没有版本号，口径修好之后旧缓存仍会被读出来继续喂给报告和 LLM —— 修了个寂寞。
调用方拿到缓存后必须先 `backtester.is_current_caliber()` 校验，不符就重算。
"""

CALIBER = {
    "entry": "T+1 开盘价（信号日收盘确认，次日开盘买入）",
    "exit": "T+1+h 日收盘价（h = 持有交易日数）",
    "t_plus_1_respected": True,
    "cost_round_trip_pct": COST_ROUND_TRIP * 100,
    "cost_application": "只在买入与卖出两个时点各扣一次，不在持有期内按日扣",
    "benchmark": "同一只票、同一持有期、同口径的无条件收益（零自由度基准）",
    "reported": "回报一律为扣除成本后的净值；同时给均值/中位数/胜率/t 值/分年",
    "excluded": "剔除「次日开盘即涨停」（买不进）与复权断裂样本，并报告剔除数",
    "pct_chg_used": False,
    "note": "旧版口径为 close[T+d]/close[T] 且不扣费，本版数字与旧版不可直接比较",
}


# ─────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────
def limit_pct_for_code(code: str | None) -> float:
    """按板块推断涨跌停幅度。

    主板 10% / 创业板(300,301) 与科创板(688) 20% / 北交所(8xx,4xx) 30%。
    ST 股为 5%，但本函数拿不到名称，故不处理 —— 依赖调用方传入非 ST 或接受偏差。
    """
    if not code:
        return 0.10
    c = str(code).strip()
    if c.startswith(("688", "689")):
        return 0.20
    if c.startswith(("300", "301")):
        return 0.20
    if c.startswith(("8", "4", "9")):
        return 0.30
    return 0.10


def _anomaly_threshold(code: str | None) -> float:
    """单日移动超过此比例即视为数据异常（复权断裂 / 单位错 / 脏数据）。"""
    return limit_pct_for_code(code) * ANOMALY_MULTIPLIER + 0.01


def prepare_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """规范输入：按日期升序、数值化、去重。返回 (df, warnings)。"""
    warnings: list[str] = []
    if df is None or len(df) == 0:
        raise ValueError("K线数据为空，无法回测")

    out = df.copy()
    if "date" not in out.columns:
        raise ValueError("K线缺少 date 列")

    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    before = len(out)
    out = out.dropna(subset=["date"])
    if len(out) != before:
        warnings.append(f"有 {before - len(out)} 行日期无法解析，已剔除")

    for col in ("open", "high", "low", "close", "volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    missing = [c for c in ("open", "high", "low", "close") if c not in out.columns]
    if missing:
        raise ValueError(f"K线缺少必需列：{missing}")

    before = len(out)
    out = out.dropna(subset=["open", "high", "low", "close"])
    if len(out) != before:
        warnings.append(f"有 {before - len(out)} 行 OHLC 为空，已剔除")

    out = out[out["close"] > 0]
    out = out[out["open"] > 0]
    out = out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return out, warnings


def _daily_moves(close: np.ndarray) -> np.ndarray:
    """逐日涨跌幅（小数）。首行显式置 0。

    不能用 `np.roll(close, 1)` 直接算：roll 会把最后一行卷到第一行，导致首行与
    「全序列最后一天的收盘价」相比，凭空造出一个巨大的假涨跌 —— 进而把首行误判成
    复权断裂，或误报一行 pct_chg 静默归零。这个 bug 实测踩到过（3 行 vs 2 行）。
    """
    move = np.zeros(len(close), dtype=float)
    if len(close) > 1:
        prev = close[:-1]
        cur = close[1:]
        with np.errstate(divide="ignore", invalid="ignore"):
            move[1:] = np.where(prev > 0, cur / prev - 1.0, 0.0)
    return np.nan_to_num(move)


def compute_anomaly_mask(df: pd.DataFrame, code: str | None) -> np.ndarray:
    """标记复权断裂/脏数据的行（True = 该行价格不可信）。

    **只看价格**，不看 pct_chg。理由：portal 缓存的 pct_chg 会在增量批次边界被静默写成 0，
    但那些行的 open/close 本身是好的。若让一个坏字段作废一整个样本，就会静默丢掉
    有效数据 —— 那是另一种「不报错只出错数」。pct_chg 的问题只在 data_quality() 里
    作为诊断计数报出来，不参与样本选择。
    """
    n = len(df)
    mask = np.zeros(n, dtype=bool)
    if n < 2:
        return mask

    close = df["close"].to_numpy(dtype=float)
    mask |= np.abs(_daily_moves(close)) > _anomaly_threshold(code)

    # 价格本身非正 / 非有限 → 不可信
    for col in ("open", "close"):
        arr = df[col].to_numpy(dtype=float)
        mask |= ~np.isfinite(arr) | (arr <= 0)
    return mask


def data_quality(df: pd.DataFrame, code: str | None = None) -> dict[str, Any]:
    """数据体检：把「不报错只出错数」的问题显式报出来。

    对输入宽容：date 可以是字符串、行序可以乱。内部**强制按日期排序**后再算相邻
    涨跌 —— 否则 np.roll 取到的「上一行」不是上一交易日，体检结果会随 CSV 行序漂移
    （这个坑实测踩到过：同一份数据行序不同，报出 6 行 vs 11 行）。
    """
    n = len(df)
    work = df
    if "date" in work.columns:
        sortable = pd.to_datetime(work["date"], errors="coerce")
        if sortable.notna().any():
            work = work.assign(_d=sortable).sort_values("_d").drop(columns="_d")
    work = work.reset_index(drop=True)

    anomalies = compute_anomaly_mask(work, code)
    dates = pd.to_datetime(work["date"], errors="coerce") if "date" in work.columns else None
    anomaly_dates = (
        [d.strftime("%Y-%m-%d") for d in dates[anomalies].dropna()[:20]]
        if dates is not None else []
    )

    pct_zero_bad = 0
    if "pct_chg" in work.columns and n > 1:
        rec = pd.to_numeric(work["pct_chg"], errors="coerce")
        close = pd.to_numeric(work["close"], errors="coerce").to_numpy(dtype=float)
        move = _daily_moves(close)
        pct_zero_bad = int(((rec.abs() < 1e-9) & (np.abs(move) > 0.0005)).sum())

    gaps = dates.diff().dt.days.dropna() if dates is not None else pd.Series(dtype=float)
    span = ""
    if dates is not None and dates.notna().any():
        span = f"{dates.min():%Y-%m-%d} ~ {dates.max():%Y-%m-%d}"

    return {
        "rows": n,
        "span": span,
        "anomaly_rows": int(anomalies.sum()),
        "anomaly_dates": anomaly_dates,
        "pct_chg_silently_zeroed": pct_zero_bad,
        "max_calendar_gap_days": float(gaps.max()) if len(gaps) else None,
        "sources": (
            {str(k): int(v) for k, v in work["fetch_source"].value_counts().items()}
            if "fetch_source" in work.columns else {}
        ),
    }


# ─────────────────────────────────────────────────────────────
# 指标（语义与旧 backtester 完全一致，只是改为向量化）
# ─────────────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"].astype(float)

    for n in (5, 10, 20):
        out[f"ma{n}"] = close.rolling(n).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["dif"] = ema12 - ema26
    out["dea"] = out["dif"].ewm(span=9, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(com=5, adjust=False).mean()
    loss = (-delta).clip(lower=0).ewm(com=5, adjust=False).mean()
    out["rsi6"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))

    low9 = out["low"].rolling(9).min()
    high9 = out["high"].rolling(9).max()
    rsv = (close - low9) / (high9 - low9 + 1e-9) * 100
    out["kdj_k"] = rsv.ewm(com=2, adjust=False).mean()
    out["kdj_d"] = out["kdj_k"].ewm(com=2, adjust=False).mean()

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    out["boll_lower"] = mid - 2 * std

    if "volume" in out.columns and out["volume"].notna().any():
        vol = out["volume"].astype(float)
        out["vol_ratio"] = vol / vol.rolling(5).mean().replace(0, 1e-9)
    else:
        # 缺量能数据时给常数 1.0 —— 量能类信号因此永不触发（诚实的降级，不猜）
        out["vol_ratio"] = 1.0

    out["close_chg"] = close.pct_change() * 100
    return out


def compute_signal_flags(df: pd.DataFrame) -> pd.DataFrame:
    """9 个信号布尔列。名称与旧版 SIGNALS 完全一致，供下游按名索引。"""
    f = pd.DataFrame(index=df.index)

    dif, dea = df["dif"], df["dea"]
    pdif, pdea = dif.shift(1), dea.shift(1)
    f["MACD金叉"] = (dif > dea) & (pdif <= pdea)
    f["MACD死叉"] = (dif < dea) & (pdif >= pdea)

    f["RSI超卖(<30)"] = df["rsi6"] < 30
    f["RSI超买(>70)"] = df["rsi6"] > 70

    k, d = df["kdj_k"], df["kdj_d"]
    f["KDJ金叉"] = (k > d) & (k.shift(1) <= d.shift(1))

    vol_ratio = df["vol_ratio"]
    chg = df["close_chg"]
    f["放量上涨"] = (vol_ratio > 2) & (chg > 0)
    f["放量下跌"] = (vol_ratio > 2) & (chg < 0)

    ma5, ma10, ma20 = df["ma5"], df["ma10"], df["ma20"]
    f["均线多头排列"] = (ma5 > ma10) & (ma10 > ma20) & (ma20 > 0)

    f["价格跌破布林下轨"] = df["close"] < df["boll_lower"]

    return f.fillna(False).astype(bool)


def signal_names() -> list[str]:
    """信号名单（顺序与旧版一致）。"""
    return [
        "MACD金叉", "MACD死叉", "RSI超卖(<30)", "RSI超买(>70)", "KDJ金叉",
        "放量上涨", "放量下跌", "均线多头排列", "价格跌破布林下轨",
    ]


# ─────────────────────────────────────────────────────────────
# 收益统计
# ─────────────────────────────────────────────────────────────
def _t_stat(values: np.ndarray) -> float | None:
    n = len(values)
    if n < 3:
        return None
    sd = float(np.std(values, ddof=1))
    if sd <= 0:
        return None
    return float(np.mean(values) / (sd / math.sqrt(n)))


def describe(values: Iterable[float]) -> dict[str, Any] | None:
    """一组净收益（小数）的完整描述。保留旧字段名，新增中位数/t 值/std。"""
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    n = len(arr)
    if n == 0:
        return None
    mean = float(np.mean(arr))
    median = float(np.median(arr))
    win = float((arr > 0).sum() / n * 100.0)
    return {
        # ── 旧字段（保持类型与单位不变，下游 prompts/patterns 直接可用）
        "win_rate": round(win, 1),
        "avg_return": round(mean * 100.0, 2),
        "valid": n,
        # ── 新增
        "median_return": round(median * 100.0, 2),
        "std_return": round(float(np.std(arr, ddof=1)) * 100.0, 2) if n > 1 else None,
        "t_stat": round(_t_stat(arr), 2) if _t_stat(arr) is not None else None,
        "mean_gt_median": mean > median,
        "insufficient": n < MIN_SAMPLES,
        "small_sample": n < SMALL_SAMPLE,
    }


def _harvest(
    df: pd.DataFrame,
    entries: np.ndarray,
    horizons: tuple[int, ...],
    code: str | None,
    anomaly: np.ndarray,
    cost: float = COST_ROUND_TRIP,
) -> tuple[dict[int, list[float]], dict[str, int], list[np.ndarray]]:
    """按「T+1 开盘买 → T+1+h 收盘卖」收割样本。

    返回 ({h: [净收益...]}, 剔除计数, [每条样本的入场索引]) —— 用于按年分组。
    """
    open_ = df["open"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)
    lim = limit_pct_for_code(code)
    rets: dict[int, list[float]] = {h: [] for h in horizons}
    excl = {"limit_up_entry": 0, "anomaly": 0, "no_next_day": 0, "no_exit_data": 0}
    kept_idx: list[np.ndarray] = []

    for i in np.flatnonzero(entries):
        j = int(i) + 1                      # T+1 —— 信号次日开盘买入
        if j >= n:
            excl["no_next_day"] += 1
            continue
        buy = open_[j]
        if not np.isfinite(buy) or buy <= 0:
            excl["no_next_day"] += 1
            continue

        prev_close = close[j - 1]
        if prev_close > 0 and buy >= prev_close * (1 + lim) - 0.005:
            # 次日开盘即涨停：物理上买不进，留在回测里就是系统性高估
            excl["limit_up_entry"] += 1
            continue

        usable = False
        n_anom = 0
        for h in horizons:
            k = j + h                       # T+1+h 收盘卖出（满足 T+1）
            if k >= n:
                continue
            if anomaly[j] or anomaly[k]:
                n_anom += 1
                continue
            sell = close[k]
            if not np.isfinite(sell) or sell <= 0:
                continue
            gross = sell / buy - 1.0
            rets[h].append(gross - cost)
            usable = True

        if usable:
            kept_idx.append(np.array([j]))
        else:
            if n_anom:
                excl["anomaly"] += 1
            elif j + min(horizons) >= n:
                excl["no_exit_data"] += 1

    return rets, excl, kept_idx


def _by_year(
    df: pd.DataFrame,
    rets_by_h: dict[int, list[float]],
    kept_idx: list[np.ndarray],
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    """分年稳定性：只在某一年成立的是行情特征，不是规律（手册 §4.5）。"""
    if not kept_idx:
        return {}
    years = [int(df["date"].iloc[int(k[0])].year) for k in kept_idx]
    out: dict[str, Any] = {}
    for h in horizons:
        vals = rets_by_h[h]
        if len(vals) != len(years):
            # 某些 h 因数据不足被跳过，无法与 years 对齐 → 按 h 重新对齐
            continue
        per: dict[str, list[float]] = {}
        for y, v in zip(years, vals):
            per.setdefault(str(y), []).append(v)
        out[str(h)] = {
            y: {
                "n": len(vs),
                "avg_return": round(float(np.mean(vs)) * 100.0, 2),
                "median_return": round(float(np.median(vs)) * 100.0, 2),
            }
            for y, vs in sorted(per.items())
            if len(vs) >= 10
        }
    return out


def _same_sign_years(by_year_h: dict[str, dict]) -> str | None:
    """分年方向一致性，例如 "3/4"。样本不足 2 年时不作判断。"""
    if not by_year_h or len(by_year_h) < 2:
        return None
    pos = sum(1 for v in by_year_h.values() if v["avg_return"] > 0)
    neg = sum(1 for v in by_year_h.values() if v["avg_return"] < 0)
    return f"{max(pos, neg)}/{len(by_year_h)}"


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────
def run_backtest(
    df: pd.DataFrame,
    forward_days: tuple[int, ...] | list[int] = DEFAULT_HORIZONS,
    code: str | None = None,
    cost: float = COST_ROUND_TRIP,
) -> dict[str, Any]:
    """口径正确的单股信号回测。

    Args:
        df: K线（至少含 date/open/high/low/close；有 volume 时量能信号才生效）
        forward_days: 持有交易日数，默认 (1,3,5,10,20)
        code: 股票代码，用于推断涨跌停幅度；缺省按主板 10% 处理
        cost: 单次往返成本（小数）。默认 0.10%；传 0 可得毛收益，用于成本口径自查。

    Returns:
        与旧版向后兼容的 dict；详细口径见模块 docstring 与返回值 `caliber`。
    """
    horizons: tuple[int, ...] = tuple(int(h) for h in forward_days)
    if not horizons:
        horizons = DEFAULT_HORIZONS

    try:
        prepared, warnings = prepare_frame(df)
    except ValueError as exc:
        return {"error": str(exc), "signals": {}, "total_days": 0, "stock_days": ""}

    n_total = len(prepared)
    if n_total < 60:
        return {
            "error": "数据不足60日，无法回测",
            "signals": {},
            "total_days": n_total,
            "stock_days": "",
            "warnings": warnings,
        }

    enriched = compute_indicators(prepared)
    flags = compute_signal_flags(enriched)
    anomaly = compute_anomaly_mask(enriched, code)
    dq = data_quality(enriched, code)

    if dq["anomaly_rows"]:
        warnings.append(
            f"检测到 {dq['anomaly_rows']} 行价格异常（复权断裂/脏数据），"
            f"相关样本已剔除：{', '.join(dq['anomaly_dates'][:5])}"
        )
    if dq["pct_chg_silently_zeroed"]:
        warnings.append(
            f"pct_chg 字段有 {dq['pct_chg_silently_zeroed']} 行被静默写成 0"
            f"（缓存增量批次边界所致）——本回测不使用该字段，收益全部由 close/open 现算"
        )

    # ── 基准：同口径无条件收益（零自由度）
    all_entries = np.ones(n_total, dtype=bool)
    base_rets, base_excl, _ = _harvest(enriched, all_entries, horizons, code, anomaly, cost)
    baseline = {
        str(h): describe(v) for h, v in base_rets.items()
    }

    # ── 各信号
    signals: dict[str, Any] = {}
    for name in signal_names():
        mask = flags[name].to_numpy()
        hit = int(mask.sum())
        if hit == 0:
            signals[name] = {"count": 0, "note": "历史未触发"}
            continue

        rets_by_h, excl, kept = _harvest(enriched, mask, horizons, code, anomaly, cost)
        stats: dict[str, Any] = {}
        for h in horizons:
            d = describe(rets_by_h[h])
            if d is None:
                continue
            b = baseline.get(str(h)) or {}
            base_avg = b.get("avg_return")
            d["excess_return"] = (
                round(d["avg_return"] - base_avg, 2) if base_avg is not None else None
            )
            base_med = b.get("median_return")
            d["excess_median"] = (
                round(d["median_return"] - base_med, 2) if base_med is not None else None
            )
            stats[str(h)] = d

        by_year = _by_year(enriched, rets_by_h, kept, horizons)
        last_signal_idx = int(np.flatnonzero(mask)[-1])
        signals[name] = {
            "count": hit,
            "count_used": len(kept),
            "excluded": excl,
            "last_date": enriched["date"].iloc[last_signal_idx].strftime("%Y-%m-%d"),
            "stats": stats,
            "by_year": by_year,
            "same_sign_years": {
                h: _same_sign_years(v) for h, v in by_year.items()
            } or None,
            "insufficient": len(kept) < MIN_SAMPLES,
        }

    return {
        # ── 旧字段（保持）
        "signals": signals,
        "total_days": n_total,
        "stock_days": f"{enriched['date'].iloc[0]:%Y-%m-%d} ~ {enriched['date'].iloc[-1]:%Y-%m-%d}",
        # ── 新增
        "caliber_version": CALIBER_VERSION,
        "caliber": CALIBER,
        "baseline": baseline,
        "baseline_excluded": base_excl,
        "data_quality": dq,
        "warnings": warnings,
        "code": code,
    }


# ─────────────────────────────────────────────────────────────
# 采样接口（供审计/结算/报告复用，避免各自重复实现口径）
# ─────────────────────────────────────────────────────────────
def collect_samples(
    df: pd.DataFrame,
    entries: np.ndarray | None = None,
    horizons: tuple[int, ...] | list[int] = DEFAULT_HORIZONS,
    code: str | None = None,
    cost: float = COST_ROUND_TRIP,
) -> tuple[pd.DataFrame, dict[int, list[float]], dict[str, int], list[np.ndarray]]:
    """按内核口径收割样本，返回 (enriched_df, {h: [净收益]}, 剔除计数, 入场索引)。

    `entries=None` 表示无条件（全样本基准）。审计、结算、报告都应走这里，
    不要在别处另写一套收益公式 —— 口径分叉本身就是静默错误的来源。
    """
    prepared, _ = prepare_frame(df)
    enriched = compute_indicators(prepared)
    anomaly = compute_anomaly_mask(enriched, code)
    ent = np.ones(len(enriched), dtype=bool) if entries is None else np.asarray(entries, dtype=bool)
    rets, excl, kept = _harvest(enriched, ent, tuple(int(h) for h in horizons), code, anomaly, cost)
    return enriched, rets, excl, kept


# ─────────────────────────────────────────────────────────────
# 单笔前瞻收益（供特征层 / 演化器取「可执行收益」，口径仍只有这里一份）
# ─────────────────────────────────────────────────────────────
def forward_return_after(
    df: pd.DataFrame,
    signal_date: str,
    horizon: int = 1,
    *,
    code: str | None = None,
    cost: float = COST_ROUND_TRIP,
) -> dict[str, Any] | None:
    """信号日之后的第一交易日开盘买入，持有 `horizon` 个交易日后收盘卖出。

    这是**唯一**该被用来给特征打「结果」标签的接口 —— 演化器、日报、结算都走它，
    避免任何地方再写一套收益公式（口径分叉本身就是静默错误的来源）。

    Returns:
        {"entry_date","entry_price","exit_date","exit_price",
         "gross_pct","net_pct","excluded"}；数据不足返回 None。
        `excluded` 非空表示这笔物理上不成立（如次日开盘即涨停买不进）。
    """
    if df is None or len(df) == 0:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date"]).sort_values("date").drop_duplicates("date", keep="last")
    d = d.reset_index(drop=True)
    target = pd.to_datetime(str(signal_date).replace("-", "")[:8],
                            format="%Y%m%d", errors="coerce")
    if pd.isna(target):
        return None

    after = d.index[d["date"] > target]
    if len(after) == 0:
        return None
    i = int(after[0])
    k = i + int(horizon)
    if k >= len(d):
        return None

    prev_close = float(d.loc[i - 1, "close"]) if i > 0 else None
    buy = float(d.loc[i, "open"])
    sell = float(d.loc[k, "close"])
    if not (np.isfinite(buy) and np.isfinite(sell)) or buy <= 0 or sell <= 0:
        return None

    lim = limit_pct_for_code(code)
    excluded = None
    if prev_close and buy >= prev_close * (1 + lim) - 0.005:
        excluded = "limit_up_open"          # 次日开盘即涨停：买不进

    gross = sell / buy - 1.0
    return {
        "entry_date": d.loc[i, "date"].strftime("%Y-%m-%d"),
        "entry_price": round(buy, 3),
        "exit_date": d.loc[k, "date"].strftime("%Y-%m-%d"),
        "exit_price": round(sell, 3),
        "gross_pct": round(gross * 100, 4),
        "net_pct": round((gross - cost) * 100, 4),
        "excluded": excluded,
    }


# ─────────────────────────────────────────────────────────────
# 摘要（供报告 / LLM prompt 使用）
# ─────────────────────────────────────────────────────────────
def summarize_for_prompt(result: dict, horizons: Iterable[int] = DEFAULT_HORIZONS) -> str:
    """把回测结果压成几行文本，**强制带上口径与样本量**。

    旧版直接把「胜率X%/均收益Y%」塞进 prompt，模型会把这些数字当成可靠结论复述；
    这里补上口径、成本、基准与样本量，让模型的叙述有边界。
    """
    if not result or result.get("error"):
        return ""

    lines: list[str] = []
    blocks = []
    for sig, v in (result.get("signals") or {}).items():
        if not isinstance(v, dict) or not v.get("stats"):
            continue
        parts = []
        for d in horizons:
            s = v["stats"].get(str(d))
            if not s or s.get("win_rate") is None:
                continue
            part = f"{d}日 胜率{s['win_rate']}%/均值{s['avg_return']}%"
            if s.get("median_return") is not None:
                part += f"/中位{s['median_return']}%"
            if s.get("excess_return") is not None:
                part += f"/超额{s['excess_return']:+.2f}pp"
            parts.append(part)
        if not parts:
            continue
        tag = "（样本极少，仅供参考）" if v.get("insufficient") else ""
        blocks.append(f"  {sig}（触发{v.get('count', 0)}次，参与统计{v.get('count_used', 0)}次）{tag}：{'  '.join(parts)}")

    if not blocks:
        return ""

    cal = result.get("caliber") or {}
    header = (
        f"历史信号回测（{result.get('stock_days', '')}，共{result.get('total_days', 0)}日）\n"
        f"  口径：{cal.get('entry', '')} → {cal.get('exit', '')}；"
        f"已扣往返成本 {cal.get('cost_round_trip_pct', 0)}%；"
        f"超额 = 相对「同一只票同持有期无条件收益」\n"
    )
    lines.append(header + "\n".join(blocks))

    warns = result.get("warnings") or []
    if warns:
        lines.append("  数据提示：" + "；".join(warns))
    return "\n".join(lines)
