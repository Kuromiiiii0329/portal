# -*- coding: utf-8 -*-
"""
portal/virtual/tagging.py — 特征层（pre/post 两阶段标签）

为什么必须两阶段（原包 tagging.py 的核心洞察，原样保留）：

    价格数据事后能补，**「当时的市场环境」补不了**。
    30 天后会有几十笔裸数据，却分不清哪一笔赚钱是因为「低位」、哪一笔是因为
    「游资」——没有标签，系统永远停在「记账」，到不了「学习」。

两阶段写入（严格防时序泄漏）
----------------------------
    stage=pre   T 日盘后写入：**只允许用 T 日收盘及以前的信息**
    stage=post  T+1 盘后回填：成交结果、次日价格、是否封板

绝不允许把 T+1 的信息写进 pre 字段，否则就是「用未来预测过去」。

三条铁律（写进代码，不只写进文档）
----------------------------------
1. `pre` 只能看到 `date <= date8` 的 K 线；
2. `post` 只能由次日数据回填，且**只存价格事实，不存收益公式**；
3. 宁 `null` 不猜 —— 源挂了就标 null，不填假数据。

关于第 2 条：为什么 post 里不直接存「收益率」？
    收益口径只有一个权威实现（`backtest_core`）。若在这里再算一遍，就出现了
    第二套公式 —— 而口径分叉本身就是静默错误的来源。所以这里只存次日 OHLC
    事实，收益由上层用统一口径计算。
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Iterable

import numpy as np
import pandas as pd

from . import paths

SCHEMA_VERSION = 1

#: 参与特征计算的 K 线窗口
ZONE_WINDOWS = (60, 120)


# ─────────────────────────────────────────────────────────────
# 描述性指标（纯函数，只吃「截至某日」的历史）
# ─────────────────────────────────────────────────────────────
def asof(df: pd.DataFrame, date8: str, n: int) -> pd.DataFrame:
    """取截至 date8（含）的最近 n 行，按日期升序。

    **这是防时序泄漏的唯一入口**：任何特征计算都必须先过这里。
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close"])
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date"])
    target = pd.to_datetime(str(date8).replace("-", "")[:8], format="%Y%m%d", errors="coerce")
    if pd.isna(target):
        return d.iloc[0:0]
    d = d[d["date"] <= target].sort_values("date")
    return d.tail(n).reset_index(drop=True)


def zone_pos(seg: pd.DataFrame) -> float | None:
    """区间位：(收 − N日最低) / (N日最高 − N日最低)。0=最低，1=最高。

    原包的「位置」维度就是它 —— 也是唯一在小样本外仍站得住的单维度
    （SKILL §9.7.2：低位档扣费后 +0.050%，复合档 −0.018%）。
    """
    if seg is None or len(seg) == 0:
        return None
    hi = float(seg["high"].max())
    lo = float(seg["low"].min())
    close = float(seg["close"].iloc[-1])
    if not np.isfinite(hi) or not np.isfinite(lo) or hi <= lo:
        return None
    return round((close - lo) / (hi - lo), 4)


def ma_spread_pct(seg: pd.DataFrame) -> float | None:
    """MA5/10/20 极差 ÷ MA20。越小 = 三线越粘合。"""
    if seg is None or len(seg) < 20:
        return None
    c = seg["close"].astype(float)
    ma5, ma10, ma20 = c.tail(5).mean(), c.tail(10).mean(), c.tail(20).mean()
    if not np.isfinite(ma20) or ma20 == 0:
        return None
    return round((max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20 * 100, 3)


def chg_nd(seg: pd.DataFrame, n: int) -> float | None:
    """近 n 个交易日涨幅 %（含当日）。"""
    if seg is None or len(seg) <= n:
        return None
    a = float(seg["close"].iloc[-1 - n])
    b = float(seg["close"].iloc[-1])
    if not np.isfinite(a) or a == 0:
        return None
    return round((b / a - 1) * 100, 3)


def limit_pct_for(code: str) -> float:
    """板块涨跌停幅度（与 backtest_core 保持一致；ST 拿不到名称故不处理）。"""
    c = str(code)
    if c.startswith(("688", "689", "300", "301")):
        return 0.20
    if c.startswith(("8", "4", "9")):
        return 0.30
    return 0.10


def limit_up_price(prev_close: float | None, code: str) -> float | None:
    """涨停价 = 昨收 × (1+幅度)，四舍五入到 0.01。

    ★ 必须用 Decimal + ROUND_HALF_UP：Python 的 round 是银行家舍入（round-half-even），
      而交易所是四舍五入。凡落在 .xx5 边界的涨停价会被**静默算错**，
      然后「收盘价 == 涨停价」的判定失效 —— 不报错，只是永久漏判。

    实测（Python 3.14 / 主板 1.1 倍）：

        昨收 1.15 → round(1.15*1.1, 2) = 1.26，Decimal HALF_UP = 1.27  ← 差一分
        昨收 4.85 → round(4.85*1.1, 2) = 5.33，Decimal HALF_UP = 5.34  ← 差一分

    注：SKILL.md 举的例子是「round(6.75*1.1, 2) = 7.42，交易所给 7.43」，
    但该值在本机 Python 3.14 上算出来就是 7.43（因为 6.75*1.1 的浮点结果略大于
    7.425）。也就是说那个**具体数字**复现不了 —— 但银行家舍入这个问题本身是真的，
    上面两个值就是活证据。所以这里保留 Decimal 实现，只是把例子换成本机实测的。
    """
    from decimal import Decimal, ROUND_HALF_UP

    if prev_close is None or not np.isfinite(prev_close) or prev_close <= 0:
        return None
    raw = Decimal(str(float(prev_close))) * (Decimal(1) + Decimal(str(limit_pct_for(code))))
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ─────────────────────────────────────────────────────────────
# pre 标签
# ─────────────────────────────────────────────────────────────
def stock_pre(
    row: dict,
    *,
    klines: pd.DataFrame | None = None,
    date8: str,
    industry_chg_pct: float | None = None,
    industry_main_net_yi: float | None = None,
    hot_reason: str | None = None,
) -> dict[str, Any]:
    """一只票的 T 日收盘时点标签。只用 `row`（T 日池子字段）与 T 日及以前的 K 线。"""
    code = str(row.get("code") or "").strip()
    boards = row.get("boards")
    seal_ratio = row.get("seal_ratio_pct")

    kline_ok = klines is not None and len(klines) > 0
    seg120 = asof(klines, date8, 120) if kline_ok else pd.DataFrame()
    seg60 = asof(klines, date8, 60) if kline_ok else pd.DataFrame()

    return {
        # ── 连板与题材（来自 T 日池子）
        "lbc": int(boards) if boards is not None else None,
        "is_first_board": (int(boards) == 1) if boards is not None else None,
        "zbc": row.get("break_count"),
        "turnover_pct": row.get("turnover_pct"),
        # ── 可买性（T 日收盘时点的封单结构）
        "seal_fund_yi": round(row["seal_amount"] / 1e8, 3) if row.get("seal_amount") else None,
        "float_cap_yi": round(row["float_cap"] / 1e8, 1) if row.get("float_cap") else None,
        "seal_ratio_pct": seal_ratio,
        "first_seal_time": row.get("first_seal"),
        "last_seal_time": row.get("last_seal"),
        # ── 位置结构（来自 T 日及以前日K）
        "zone_pos_120": zone_pos(seg120) if len(seg120) else None,
        "zone_pos_60": zone_pos(seg60) if len(seg60) else None,
        "ma_spread_pct": ma_spread_pct(seg120) if len(seg120) else None,
        "chg_5d_pct": chg_nd(seg120, 5) if len(seg120) else None,
        "chg_10d_pct": chg_nd(seg120, 10) if len(seg120) else None,
        "chg_20d_pct": chg_nd(seg120, 20) if len(seg120) else None,
        # 自证：有几个 K 线样本参与计算（缺数据时一眼看得出）
        "kline_rows": int(len(seg120)),
        # ── 题材与行业
        "industry": row.get("industry"),
        "industry_chg_pct": industry_chg_pct,
        "industry_main_net_yi": industry_main_net_yi,
        "hot_reason": hot_reason,
        "prev_close": row.get("price"),
        "limit_up": limit_up_price(row.get("price"), code),
    }


def market_features(snapshot: dict) -> dict[str, Any]:
    """市场环境标签。直接透传 emotion_snapshot 的结果（含 brake_only 约束）。"""
    keys = (
        "zt_n", "dt_n", "zb_n", "zb_rate", "zt_dt_ratio", "max_lbc", "lbc_ge2_n",
        "seal_ratio_median_pct", "idx_chg_pct", "emotion_score", "emotion_formula",
        "usage", "usage_note",
    )
    return {k: snapshot.get(k) for k in keys}


def build_day(
    date8: str,
    *,
    snapshot: dict,
    stocks: Iterable[dict],
    klines_by_code: dict[str, pd.DataFrame] | None = None,
    industry_by_name: dict[str, dict] | None = None,
    hot_by_code: dict[str, str] | None = None,
) -> dict[str, Any]:
    """构造一整天的 pre 特征。不写盘（写盘走 write_features）。"""
    d8 = str(date8).replace("-", "").replace("/", "")[:8]
    klines_by_code = klines_by_code or {}
    industry_by_name = industry_by_name or {}
    hot_by_code = hot_by_code or {}

    rows: list[dict[str, Any]] = []
    for stock in stocks:
        code = str(stock.get("code") or "").strip()
        if not code:
            continue
        ind = industry_by_name.get(str(stock.get("industry") or ""), {}) or {}
        pre = stock_pre(
            stock,
            klines=klines_by_code.get(code),
            date8=d8,
            industry_chg_pct=ind.get("chg"),
            industry_main_net_yi=ind.get("main_net_yi"),
            hot_reason=hot_by_code.get(code),
        )
        rows.append({"code": code, "name": stock.get("name"), "pre": pre, "post": None})

    return {
        "schema_version": SCHEMA_VERSION,
        "date": d8,
        "stage": "pre",
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": ("stage=pre：只使用 T 日收盘及以前的信息。"
                 "严禁把 T+1 的价格/成交量写进 pre 字段（时序泄漏）。"),
        "market": market_features(snapshot),
        "stocks": rows,
    }


# ─────────────────────────────────────────────────────────────
# post 标签（回填）
# ─────────────────────────────────────────────────────────────
def build_post(
    code: str,
    *,
    klines: pd.DataFrame | None,
    date8: str,
    prev_close: float | None = None,
) -> dict[str, Any] | None:
    """回填 T+1 的价格事实。找不到下一交易日则返回 None（不猜）。"""
    if klines is None or len(klines) == 0:
        return None
    d = klines.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    target = pd.to_datetime(str(date8).replace("-", "")[:8], format="%Y%m%d", errors="coerce")
    if pd.isna(target):
        return None
    pos = d.index[d["date"] == target]
    if len(pos) == 0:
        return None
    i = int(pos[-1])
    if i + 1 >= len(d):
        return None                      # 还没有下一交易日 → 不猜

    nxt = d.iloc[i + 1]
    base_close = prev_close if prev_close else float(d.iloc[i]["close"])
    sealed = None
    lu = limit_up_price(base_close, code)
    if lu is not None and np.isfinite(nxt["close"]):
        sealed = bool(abs(float(nxt["close"]) - lu) < 0.005)

    return {
        "next_date": nxt["date"].strftime("%Y-%m-%d"),
        # 只存价格事实。收益必须由 backtest_core 用统一口径算，不在这里重算。
        "next_open": _f(nxt.get("open")),
        "next_close": _f(nxt.get("close")),
        "next_high": _f(nxt.get("high")),
        "next_low": _f(nxt.get("low")),
        "base_close": round(float(base_close), 3) if base_close else None,
        "limit_up_next": lu,
        "sealed_next_day": sealed,
        "note": "只存价格事实；收益请用 backtest_core 的统一口径计算",
    }


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return round(f, 3) if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def backfill_post(
    date8: str,
    klines_by_code: dict[str, pd.DataFrame],
    *,
    write: bool = True,
) -> dict[str, Any] | None:
    """给已有 pre 特征回填 post。返回更新后的文档。"""
    doc = read_features(date8)
    if doc is None:
        return None
    if doc.get("stage") == "post":
        return doc

    n_ok = 0
    for stock in doc.get("stocks", []):
        code = stock.get("code")
        prev_close = (stock.get("pre") or {}).get("prev_close")
        post = build_post(code, klines=klines_by_code.get(code), date8=doc["date"],
                          prev_close=prev_close)
        stock["post"] = post
        n_ok += 1 if post else 0

    doc["stage"] = "post"
    doc["post_filled_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    doc["post_filled_n"] = n_ok
    if write:
        write_features(doc)
    return doc


# ─────────────────────────────────────────────────────────────
# 落盘
# ─────────────────────────────────────────────────────────────
def write_features(doc: dict) -> Any:
    paths.ensure_dirs()
    p = paths.feature_path(doc["date"])
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def read_features(date8: str) -> dict | None:
    p = paths.feature_path(date8)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_days() -> list[str]:
    if not paths.FEATURES_DIR.exists():
        return []
    return sorted(p.stem for p in paths.FEATURES_DIR.glob("*.json"))


def feature_fields() -> list[str]:
    """pre 字段全集（用于检查规则引用的字段是否都有特征支撑）。"""
    sample = stock_pre({"code": "600000", "price": 10.0, "boards": 1}, date8="20260101")
    return sorted(sample.keys())


def flatten_row(doc: dict, stock: dict) -> dict[str, Any]:
    """把一天的 market + 一只票的 pre 摊平成规则求值用的单层字典。"""
    row: dict[str, Any] = {}
    for k, v in (doc.get("market") or {}).items():
        row[f"m_{k}"] = v            # 市场字段加 m_ 前缀，避免与个股字段撞名
    for k, v in (stock.get("pre") or {}).items():
        row[k] = v
    row["code"] = stock.get("code")
    row["name"] = stock.get("name")
    row["date"] = doc.get("date")
    return row


__all__ = [
    "SCHEMA_VERSION",
    "ZONE_WINDOWS",
    "asof",
    "zone_pos",
    "ma_spread_pct",
    "chg_nd",
    "limit_pct_for",
    "limit_up_price",
    "stock_pre",
    "market_features",
    "build_day",
    "build_post",
    "backfill_post",
    "write_features",
    "read_features",
    "list_days",
    "feature_fields",
    "flatten_row",
]
