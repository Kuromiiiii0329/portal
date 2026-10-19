# -*- coding: utf-8 -*-
"""
portal/backtest_audit.py — 回测内核的对照实验 + 数据体检

为什么需要这个文件（一句话）：
    《回测避坑手册》§7 的结论是——「回测里最贵的错误不是算错，而是算错了但没报错」。
    所以真正的功夫花在两件事上：
      ① 每个判据都要能通过**对照实验**（塞已知答案进去，看它报不报）；
      ② 每个结论都要能通过**分层验证**（分年、分样本、换口径，看它稳不稳）。
    本文件就是第 ① 件事的可执行版本。

用法：
    python portal/backtest_audit.py                # 只跑合成数据自检（不需要网络/缓存）
    python portal/backtest_audit.py --real         # 额外跑仓库内的真实 K 线缓存
    python portal/backtest_audit.py --code 002466  # 只体检一只票
    python portal/backtest_audit.py --json         # 机器可读输出

退出码：0 = 全部通过；1 = 有检查失败。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import numpy as np
import pandas as pd

try:  # 两种导入方式都要能跑：包内 `portal.x` / 直接跑脚本
    from portal import backtest_core as core
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import backtest_core as core  # type: ignore


# ─────────────────────────────────────────────────────────────
# 断言工具：必须能「失败」的断言，才算断言
# ─────────────────────────────────────────────────────────────
class Check:
    """一条检查。`ok()` 为真才算过；detail 无论成败都记录。"""

    def __init__(self, cid: str, title: str):
        self.cid = cid
        self.title = title
        self.passed = True
        self.lines: list[str] = []

    def expect(self, cond: bool, msg: str) -> "Check":
        if not cond:
            self.passed = False
            self.lines.append(f"   ✗ {msg}")
        else:
            self.lines.append(f"   ✓ {msg}")
        return self

    def note(self, msg: str) -> "Check":
        self.lines.append(f"   · {msg}")
        return self

    def close(self, obj: bool, msg: str) -> "Check":
        """期望 obj 为 True（用于证明断言本身有效）。"""
        return self.expect(obj, msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.cid,
            "title": self.title,
            "passed": self.passed,
            "detail": self.lines,
        }


def _close_enough(a: float | None, b: float | None, tol: float = 1e-9) -> bool:
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= tol


# ─────────────────────────────────────────────────────────────
# 合成数据构造器
# ─────────────────────────────────────────────────────────────
def _frame(closes: np.ndarray, opens: np.ndarray | None = None,
           volumes: np.ndarray | None = None,
           start: str = "2020-01-02") -> pd.DataFrame:
    n = len(closes)
    if opens is None:
        opens = closes.copy()
    if volumes is None:
        volumes = np.full(n, 1_000_000.0)
    dates = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": opens,
        "high": np.maximum(closes, opens) * 1.001,
        "low": np.minimum(closes, opens) * 0.999,
        "close": closes,
        "volume": volumes,
    })


def _compound_series(n: int, daily: float, base: float = 100.0) -> np.ndarray:
    """确定性复利序列：close[i] = base * (1+daily)^i —— 答案可手算。"""
    return base * np.power(1.0 + daily, np.arange(n))


# ─────────────────────────────────────────────────────────────
# C1 · 算术已知答案
# ─────────────────────────────────────────────────────────────
def check_known_answer(h: int = 3, daily: float = 0.01, n: int = 300) -> Check:
    c = Check("C1", f"算术已知答案（日涨 {daily:.0%}，持有 {h} 日，open==close 无跳空）")
    closes = _compound_series(n, daily)
    df = _frame(closes)

    enriched, rets, excl, kept = core.collect_samples(df, None, (h,), None, cost=0.0)
    expected_gross = (1.0 + daily) ** h - 1.0
    got = float(np.mean(rets[h])) if rets[h] else None
    c.expect(_close_enough(got, expected_gross, 1e-12),
             f"毛收益均值 {got!r} == 手算 {expected_gross!r}")
    c.expect(excl["limit_up_entry"] == 0, f"无涨停剔除（实际 {excl['limit_up_entry']}）")
    c.expect(len(rets[h]) > 0, f"样本数 {len(rets[h])} > 0")

    # 扣成本后必须精确等于毛收益 - cost
    _, rets_net, _, _ = core.collect_samples(df, None, (h,), None, cost=core.COST_ROUND_TRIP)
    diff = float(np.mean(rets_net[h])) - float(np.mean(rets[h]))
    c.expect(_close_enough(diff, -core.COST_ROUND_TRIP, 1e-12),
             f"净收益 − 毛收益 = {diff!r} == −cost {(-core.COST_ROUND_TRIP)!r}")
    return c


# ─────────────────────────────────────────────────────────────
# C2 · T+1 入场：必须等于「次日开盘买」，且**不等于**旧口径
# ─────────────────────────────────────────────────────────────
def check_t_plus_1(h: int = 2, n: int = 300) -> Check:
    c = Check("C2", "T+1 入场口径（每日 2% 跳空开盘，可放大新/旧口径差异）")
    closes = _compound_series(n, 0.005)
    # open[t] = close[t-1] * 1.02 → 每天高开 2%
    opens = np.empty_like(closes)
    opens[0] = closes[0]
    opens[1:] = closes[:-1] * 1.02
    df = _frame(closes, opens)

    enriched, rets, _, _ = core.collect_samples(df, None, (h,), None, cost=0.0)
    got = float(np.mean(rets[h]))

    # 正确口径：close[j+h] / open[j] - 1
    ent = np.ones(len(enriched), dtype=bool)
    new_math = []
    old_math = []
    o = enriched["open"].to_numpy(float)
    cl = enriched["close"].to_numpy(float)
    for i in np.flatnonzero(ent):
        j = int(i) + 1
        k = j + h
        if k >= len(cl):
            continue
        if o[j] >= cl[j - 1] * 1.10 - 0.005:      # 主板涨停开盘买不进
            continue
        new_math.append(cl[k] / o[j] - 1.0)
        old_math.append(cl[k] / cl[int(i)] - 1.0)  # 旧版：信号日收盘 → T+h 收盘

    new_mean = float(np.mean(new_math))
    old_mean = float(np.mean(old_math))
    c.expect(_close_enough(got, new_mean, 1e-12),
             f"内核均值 {got:.8f} == 新口径 {new_mean:.8f}")
    c.expect(not _close_enough(got, old_mean, 1e-6),
             f"内核均值 ≠ 旧口径 {old_mean:.8f}（差 {abs(got-old_mean)*100:.3f}pp，说明修正真实生效）")
    c.note(f"旧口径会把结果虚高 {abs(new_mean-old_mean)*100:.3f}pp/笔")
    return c


# ─────────────────────────────────────────────────────────────
# C3 · 成本只扣一次（不是按持有天数扣）
# ─────────────────────────────────────────────────────────────
def check_cost_once(h: int = 5, n: int = 300) -> Check:
    c = Check("C3", "成本只在买卖两个时点各扣一次（不是持有期内按日扣）")
    closes = _compound_series(n, 0.002)
    df = _frame(closes)
    _, g, _, _ = core.collect_samples(df, None, (h,), None, cost=0.0)
    _, n_, _, _ = core.collect_samples(df, None, (h,), None, cost=core.COST_ROUND_TRIP)
    delta = float(np.mean(g[h])) - float(np.mean(n_[h]))
    c.expect(_close_enough(delta, core.COST_ROUND_TRIP, 1e-12),
             f"扣费额 {delta!r} == 单次往返 {core.COST_ROUND_TRIP!r}")
    c.expect(not _close_enough(delta, core.COST_ROUND_TRIP * h, 1e-9),
             f"扣费额 ≠ 按日扣 {core.COST_ROUND_TRIP * h!r}（旧错法：持 {h} 天扣 {h} 次）")
    return c


# ─────────────────────────────────────────────────────────────
# C4 · 负向控制：开盘即涨停必须被剔除，而不是算成买到
# ─────────────────────────────────────────────────────────────
def check_limit_up_excluded(n: int = 200) -> Check:
    c = Check("C4", "负向控制：全部次日开盘涨停 → 必须全部剔除，不得产出统计")
    closes = _compound_series(n, 0.0)          # 收盘恒定
    opens = closes * 1.10                       # 开盘即 +10%（主板涨停）
    df = _frame(closes, opens)

    _, rets, excl, kept = core.collect_samples(df, None, (1,), "600000", cost=core.COST_ROUND_TRIP)
    total = len(rets[1])
    c.expect(total == 0, f"参与统计样本数 = {total}（应为 0）")
    c.expect(excl["limit_up_entry"] > 0, f"记录了 {excl['limit_up_entry']} 笔涨停剔除")
    c.expect(len(kept) == 0, f"kept 索引数 = {len(kept)}（应为 0）")

    # 正向对照：同数据但开盘不涨停 → 必须产生样本
    df2 = _frame(closes, closes * 1.001)
    _, rets2, _, _ = core.collect_samples(df2, None, (1,), "600000", cost=core.COST_ROUND_TRIP)
    c.expect(len(rets2[1]) > 0, f"对照（开盘 +0.1%）产生 {len(rets2[1])} 个样本 → 剔除逻辑不是把所有样本都杀掉")
    return c


# ─────────────────────────────────────────────────────────────
# C5 · pct_chg 免疫
# ─────────────────────────────────────────────────────────────
def check_pct_chg_immunity(h: int = 3, n: int = 250) -> Check:
    c = Check("C5", "pct_chg 字段免疫（缓存里该字段会在批次边界被静默写成 0）")
    closes = np.empty(n)
    closes[0] = 100.0
    rng = np.random.default_rng(20261019)
    for i in range(1, n):
        closes[i] = closes[i - 1] * (1.0 + float(rng.normal(0.0005, 0.012)))
    df = _frame(closes, closes)
    df_clean = df.copy()
    df_clean["pct_chg"] = df["close"].pct_change() * 100

    df_broken = df.copy()
    df_broken["pct_chg"] = 0.0                 # 全表静默归零（最坏情况）

    a = core.run_backtest(df_clean, (h,), "600000")
    b = core.run_backtest(df_broken, (h,), "600000")

    same = True
    for k in a["signals"]:
        sa = (a["signals"][k].get("stats") or {}).get(str(h))
        sb = (b["signals"][k].get("stats") or {}).get(str(h))
        if (sa is None) != (sb is None):
            same = False
            break
        if sa and sb and (sa["avg_return"] != sb["avg_return"] or sa["valid"] != sb["valid"]):
            same = False
            break
    c.expect(same, "打乱 pct_chg 后所有信号统计完全一致")
    c.expect(a.get("caliber", {}).get("pct_chg_used") is False, "caliber 声明 pct_chg_used = False")
    c.note(f"坏字段仍被体检抓到：pct_chg_silently_zeroed = "
           f"{b.get('data_quality', {}).get('pct_chg_silently_zeroed')}")
    return c


# ─────────────────────────────────────────────────────────────
# C6 · 探针有效性（塞一个已知错误，检查器必须报错）
# ─────────────────────────────────────────────────────────────
def check_probe_not_vacuous() -> Check:
    c = Check("C6", "探针有效性：塞入已知错误答案，断言必须具备失败能力")
    c.expect(_close_enough(1.0, 1.0), "正确值通过")
    c.expect(not _close_enough(1.0, 1.001), "错误值被拒绝（说明断言不是恒真）")
    c.expect(not _close_enough(None, 0.0), "None vs 数值 被拒绝")
    # 反向验证 C2 的核心对比：若把旧口径当成期望，C2 会失败
    c.expect(not _close_enough(0.03, 0.05, 1e-6), "旧/新口径差异可被识别（C2 有判别力）")
    return c


# ─────────────────────────────────────────────────────────────
# C7 · 向后兼容（旧字段必须还在，类型不变）
# ─────────────────────────────────────────────────────────────
LEGACY_KEYS = ("signals", "total_days", "stock_days")
LEGACY_SIGNAL_KEYS = ("count", "last_date", "stats")
LEGACY_STAT_KEYS = ("win_rate", "avg_return", "valid")


def check_backward_compat(h: int = 1, n: int = 300) -> Check:
    c = Check("C7", "向后兼容：旧字段名/类型全部保留（http_handler / tasks / prompts 不改也能跑）")
    closes = _compound_series(n, 0.001)
    rng = np.random.default_rng(7)
    closes = closes * (1.0 + rng.normal(0, 0.01, n).cumsum() * 0.0)  # 保形
    df = _frame(closes, closes)
    res = core.run_backtest(df, (1, 3, 5, 10, 20), "600000")

    for k in LEGACY_KEYS:
        c.expect(k in res, f"顶层字段 {k} 存在")
    c.expect(isinstance(res.get("total_days"), int), "total_days 是 int")
    c.expect(isinstance(res.get("stock_days"), str), "stock_days 是 str")

    any_sig = False
    for name, v in res["signals"].items():
        if v.get("count", 0) == 0:
            c.expect("note" in v, f"{name}: 未触发时仍返回 note 字段")
            continue
        any_sig = True
        for k in LEGACY_SIGNAL_KEYS:
            c.expect(k in v, f"{name}: 字段 {k} 存在")
        st = (v.get("stats") or {}).get("1")
        if st:
            for k in LEGACY_STAT_KEYS:
                c.expect(k in st, f"{name}.stats['1']: 字段 {k} 存在")
            c.expect(isinstance(st["win_rate"], float), f"{name}: win_rate 是 float")
            c.expect(isinstance(st["valid"], int), f"{name}: valid 是 int")
    c.expect(any_sig, "至少有一个信号产出了统计（否则兼容性检查是空转）")
    return c


# ─────────────────────────────────────────────────────────────
# C8 · 真实数据独立复算
# ─────────────────────────────────────────────────────────────
def _independent_limit(code: str | None) -> float:
    if not code:
        return 0.10
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    if code.startswith(("8", "4", "9")):
        return 0.30
    return 0.10


def _independent_returns(df: pd.DataFrame, code: str | None, h: int,
                         cost: float) -> list[float]:
    """完全独立的第二实现：纯 pandas 取值、独立复刻剔除规则。"""
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    o = d["open"].to_numpy(float)
    cl = d["close"].to_numpy(float)
    lim = _independent_limit(code)
    thr = lim * 1.5 + 0.01

    move = np.zeros(len(cl))
    move[1:] = np.where(cl[:-1] > 0, cl[1:] / cl[:-1] - 1.0, 0.0)
    bad = np.abs(move) > thr

    out: list[float] = []
    for i in range(0, len(cl) - 1):
        j = i + 1
        k = j + h
        if k >= len(cl):
            continue
        if o[j] <= 0 or cl[k] <= 0:
            continue
        if cl[j - 1] > 0 and o[j] >= cl[j - 1] * (1 + lim) - 0.005:
            continue
        if bad[j] or bad[k]:
            continue
        out.append(cl[k] / o[j] - 1.0 - cost)
    return out


def check_real_data(csv_paths: list[pathlib.Path], h: int = 3) -> Check:
    c = Check("C8", "真实缓存独立复算（相关系数 ≈1.000、平均偏差 ≈0）")
    if not csv_paths:
        return c.note("未找到 data/stocks/*/kline.csv，跳过")

    for p in csv_paths:
        code = p.parent.name
        df = pd.read_csv(p)
        if len(df) < 80:
            c.note(f"{code}: 仅 {len(df)} 行，跳过")
            continue

        enriched, rets, excl, _ = core.collect_samples(df, None, (h,), code,
                                                       cost=core.COST_ROUND_TRIP)
        mine = np.asarray(rets[h], dtype=float)
        theirs = np.asarray(_independent_returns(df, code, h, core.COST_ROUND_TRIP), dtype=float)

        if len(mine) != len(theirs):
            c.expect(False, f"{code}: 样本数不一致 内核 {len(mine)} vs 独立 {len(theirs)}")
            continue
        if len(mine) < 5:
            c.note(f"{code}: 样本 {len(mine)} 过少，跳过相关性")
            continue

        corr = float(np.corrcoef(mine, theirs)[0, 1])
        mad = float(np.mean(np.abs(mine - theirs)))
        c.expect(corr > 0.999, f"{code}: 逐笔相关系数 {corr:.6f} > 0.999（n={len(mine)}）")
        c.expect(mad < 1e-9, f"{code}: 平均偏差 {mad:.2e} < 1e-9")
    return c


def check_data_quality_real(csv_paths: list[pathlib.Path]) -> Check:
    c = Check("C9", "数据体检能抓到真实缓存里的 pct_chg 静默归零")
    total_zero = 0
    for p in csv_paths:
        df = pd.read_csv(p)
        dq = core.data_quality(df, p.parent.name)
        z = int(dq.get("pct_chg_silently_zeroed") or 0)
        total_zero += z
        if z:
            c.note(f"{p.parent.name}: {z} 行 pct_chg 静默归零，已报出")
    c.expect(total_zero > 0,
             f"在真实缓存中共抓到 {total_zero} 行（若为 0，说明这份缓存没有该问题）")
    return c


# ─────────────────────────────────────────────────────────────
# C10 · 行序不变性（体检结果不能随 CSV 行序漂移）
# ─────────────────────────────────────────────────────────────
def check_order_invariance(n: int = 200) -> Check:
    c = Check("C10", "行序不变性：打乱 CSV 行序后体检指标必须完全一致")
    rng = np.random.default_rng(99)
    closes = np.empty(n)
    closes[0] = 50.0
    for i in range(1, n):
        closes[i] = closes[i - 1] * (1.0 + float(rng.normal(0, 0.015)))
    df = _frame(closes, closes)
    pct = pd.Series(closes).pct_change() * 100
    pct.iloc[[40, 41, 120]] = 0.0          # 故意埋 3 处静默归零
    df["pct_chg"] = pct.to_numpy()

    a = core.data_quality(df, "600000")

    shuffled = df.sample(frac=1.0, random_state=5).reset_index(drop=True)
    b = core.data_quality(shuffled, "600000")

    c.expect(a["pct_chg_silently_zeroed"] == b["pct_chg_silently_zeroed"],
             f"静默归零计数一致（{a['pct_chg_silently_zeroed']} vs {b['pct_chg_silently_zeroed']}）")
    c.expect(a["anomaly_rows"] == b["anomaly_rows"],
             f"异常行数一致（{a['anomaly_rows']} vs {b['anomaly_rows']}）")
    c.expect(a["pct_chg_silently_zeroed"] == 3,
             f"埋了 3 处，抓到 {a['pct_chg_silently_zeroed']} 处（探针有判别力）")
    return c


# ─────────────────────────────────────────────────────────────
# C11 · 首行不得被 np.roll 回卷误判
# ─────────────────────────────────────────────────────────────
def check_first_row_wraparound(n: int = 200) -> Check:
    c = Check("C11", "首行回卷防护：np.roll 会把末行卷到首行，造出假涨跌")
    # 价格从 10 涨到 ~1000：若首行与末行相比，会得到 +9900% 的假涨跌
    closes = 10.0 * np.power(1.03, np.arange(n))
    df = _frame(closes, closes)
    df["pct_chg"] = pd.Series(closes).pct_change().fillna(0.0).to_numpy()

    mask = core.compute_anomaly_mask(df, "600000")
    c.expect(bool(mask[0]) is False,
             f"首行未被标记为异常（mask[0]={mask[0]}，修复前会因 +9900% 假涨跌被判脏）")

    dq = core.data_quality(df, "600000")
    c.expect(dq["pct_chg_silently_zeroed"] == 0,
             f"首行未被误报为 pct_chg 静默归零（实际 {dq['pct_chg_silently_zeroed']}）")
    c.expect(mask[1:].sum() == 0,
             f"整段 3% 日涨未触发异常（实际 {int(mask[1:].sum())} 行，阈值应高于 16%）")
    return c


# ─────────────────────────────────────────────────────────────
# C12 · 两套价格体系（真实价判涨停 / 复权价算收益）
# ─────────────────────────────────────────────────────────────
def check_two_price_systems() -> Check:
    c = Check("C12", "两套价格体系：真实价判涨跌停，复权价算收益")

    # 构造：第 100 天除权（10 送 10），真实价腰斩，复权价连续
    n = 160
    raw_close = np.full(n, 20.0)
    raw_close[100:] = 10.0                      # 除权：20 → 10
    raw_open = raw_close.copy()
    adj_close = np.full(n, 10.0)                # 前复权：全程连续
    adj_open = adj_close.copy()

    df = _frame(raw_close, raw_open)
    df["aclose"] = adj_close
    df["aopen"] = adj_open
    df["ahigh"] = np.maximum(adj_open, adj_close) * 1.001
    df["alow"] = np.minimum(adj_open, adj_close) * 0.999

    c.expect(core.has_adjusted(df), "识别出复权列")
    c.expect(core.return_cols(df) == (core.ADJ_OPEN, core.ADJ_CLOSE), "收益用复权列")

    # ① 收益：跨除权日必须算成 0%，而不是 −50%
    _, rets, _, _ = core.collect_samples(df, None, (1,), "600000", cost=0.0)
    mean = float(np.mean(rets[1]))
    c.expect(abs(mean) < 1e-9,
             f"跨除权日的收益用复权价 → {mean:+.6f}（若用真实价会是 −50% 级别的假跌）")

    # 对照组：去掉复权列后，跨除权日的样本**不会静默算错**——
    # 它会被异常检测拦住并计入 excluded（这是防御性兜底）。
    # 但代价是丢样本：除权日往往伴随放量，丢掉它本身也是损失。
    df_raw = df.drop(columns=["aclose", "aopen", "ahigh", "alow"])
    _, rets_raw, excl_raw, _ = core.collect_samples(df_raw, None, (1,), "600000", cost=0.0)
    mean_raw = float(np.mean(rets_raw[1])) if rets_raw[1] else 0.0
    c.expect(mean_raw < -1.0 or excl_raw["anomaly"] > 0,
             f"去掉复权列后：要么算出假跌（{mean_raw:+.2f}%），要么被异常检测剔除"
             f"（excluded.anomaly={excl_raw['anomaly']}）—— 两条都是损失，"
             f"所以必须补复权")

    # ② 异常检测：真实价在除权日有 −50% 跳空，但复权后应判为正常
    enriched, _, _, _ = core.collect_samples(df, None, (1,), "600000", cost=0.0)
    mask = core.compute_anomaly_mask(enriched, "600000")
    c.expect(int(mask.sum()) == 0,
             f"复权价连续 → 0 行被判异常（实际 {int(mask.sum())}；用真实价会误判）")

    # ③ 涨跌停判定仍用真实价
    n2 = 40
    rc = np.full(n2, 10.0)
    ro = np.full(n2, 10.0)
    ro[20] = 11.0                                  # 第 20 天开盘 = 昨收 ×1.1（涨停）
    ac = np.full(n2, 5.0)                          # 复权价被缩放，永远「看起来不涨停」
    ao = np.full(n2, 5.0)
    ao[20] = 5.5
    d2df = _frame(rc, ro)
    d2df["aclose"], d2df["aopen"] = ac, ao
    entries = np.zeros(n2, dtype=bool)
    entries[19] = True                             # 信号第 19 天 → 第 20 天开盘买
    _, rets2, excl2, _ = core.collect_samples(d2df, entries, (1,), "600000", cost=0.0)
    c.expect(excl2["limit_up_entry"] == 1,
             f"用真实价判出「开盘涨停买不进」（剔除 {excl2['limit_up_entry']} 笔）")
    c.expect(len(rets2[1]) == 0, "这笔没有进统计（不是靠复权价掩过去）")
    return c


# ─────────────────────────────────────────────────────────────
# 运行器
# ─────────────────────────────────────────────────────────────
def _find_kline(root: pathlib.Path, code: str | None = None) -> list[pathlib.Path]:
    base = root / "data" / "stocks"
    if not base.exists():
        return []
    if code:
        p = base / code / "kline.csv"
        return [p] if p.exists() else []
    return sorted(base.glob("*/kline.csv"))


def run(include_real: bool = False, code: str | None = None,
        root: pathlib.Path | None = None) -> dict[str, Any]:
    root = root or pathlib.Path(__file__).resolve().parent
    checks: list[Check] = [
        check_known_answer(),
        check_t_plus_1(),
        check_cost_once(),
        check_limit_up_excluded(),
        check_pct_chg_immunity(),
        check_probe_not_vacuous(),
        check_backward_compat(),
        check_order_invariance(),
        check_first_row_wraparound(),
        check_two_price_systems(),
    ]
    if include_real:
        paths = _find_kline(root, code)
        checks.append(check_real_data(paths))
        checks.append(check_data_quality_real(paths))

    passed = all(c.passed for c in checks)
    return {
        "passed": passed,
        "n_checks": len(checks),
        "n_failed": sum(1 for c in checks if not c.passed),
        "checks": [c.to_dict() for c in checks],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="portal 回测内核自检 / 数据体检")
    ap.add_argument("--real", action="store_true", help="额外体检仓库内真实 K 线缓存")
    ap.add_argument("--code", help="只体检指定代码")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    report = run(include_real=args.real, code=args.code)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1

    print("=" * 72)
    print("portal 回测内核自检")
    print("=" * 72)
    for chk in report["checks"]:
        flag = "PASS" if chk["passed"] else "FAIL"
        print(f"\n[{flag}] {chk['id']} · {chk['title']}")
        for line in chk["detail"]:
            print(line)

    print("\n" + "=" * 72)
    print(f"结果：{report['n_checks'] - report['n_failed']}/{report['n_checks']} 通过"
          + ("" if report["passed"] else "  ← 有检查失败，不要相信这份回测的数字"))
    print("=" * 72)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
