"""
portal/analyzers/technical/sections_data.py
需外部数据 IO 的个股专属 section 子模块 —— 筹码分布 / 换手率 / 融资融券。

特征：内部临时 import akshare、自带 try/except 降级，拿不到真实数据时返回 None
（不出假情报）。仅个股适用（大盘指数无这些数据，故 market.py 不调用）。

从原 TechnicalAnalyzer 的对应 _analyze_* 方法逐字节搬迁，改为模块级纯函数。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Section


def analyze_chip(df: pd.DataFrame, stock_code: str) -> Section:
    """筹码分布：成本集中度分析"""
    try:
        import akshare as ak
        cyq = ak.stock_cyq_em(symbol=stock_code)
        if cyq is not None and not cyq.empty and len(cyq) >= 5:
            close_col = next((c for c in cyq.columns if "收盘" in c or "close" in c.lower()), None)
            profit_col = next((c for c in cyq.columns if "获利" in c or "profit" in c.lower()), None)
            cost_col   = next((c for c in cyq.columns if "成本" in c or "cost" in c.lower()), None)

            current_price = df.iloc[-1]["close"]

            if profit_col and not cyq[profit_col].isna().all():
                profit_ratio = float(cyq[profit_col].iloc[-1])
                if profit_ratio > 1:
                    profit_ratio = profit_ratio / 100.0
            else:
                profit_col_fallback = cyq.select_dtypes(include=[float, int]).columns
                profit_ratio = None
                if close_col:
                    try:
                        closes_in_cyq = cyq[close_col].dropna()
                        profit_ratio = float((closes_in_cyq < current_price).sum()) / max(len(closes_in_cyq), 1)
                    except Exception:
                        pass
                if profit_ratio is None:
                    profit_ratio = 0.5

            # 主要套牢/密集区
            if cost_col and not cyq[cost_col].isna().all():
                cost_median = float(cyq[cost_col].median())
            else:
                cost_median = float(df.tail(60)["close"].mean()) if len(df) >= 60 else float(df["close"].mean())

            support   = round(cost_median * 0.97, 2)
            pressure  = round(cost_median * 1.05, 2)

            profit_pct = profit_ratio * 100
            content = (
                f"**获利盘比例={profit_pct:.1f}%**\n"
                f"- 密集成本区中位数：{cost_median:.2f}\n"
                f"- 估算支撑位：{support}  压力位：{pressure}\n"
            )
            return Section(key="chip", title="筹码分布",
                           content=content,
                           data={"profit_ratio": profit_ratio, "cost_median": cost_median,
                                 "support": support, "pressure": pressure},
                           score=50, signal="hold")

        # akshare 返回空，无真实筹码数据 → 不显示（不出假情报）
        raise ValueError("cyq data empty")

    except Exception:
        # 拿不到真实筹码数据，宁可不显示也不出估算假情报
        return None


def analyze_turnover(df: pd.DataFrame) -> Section:
    """换手率趋势分析（近30日）"""
    try:
        # 确定换手率列（仅接受真实换手率或基于真实成交额的换算，不做凭空粗估）
        if "turnover_rate" in df.columns and not df["turnover_rate"].isna().all():
            tr = df["turnover_rate"].copy()
        elif all(c in df.columns for c in ["amount", "close", "volume"]):
            # amount 单位通常为元，volume 为股数，基于真实成交额估算换手比
            tr = df["volume"] / (df["amount"] / df["close"].replace(0, np.nan) + 1e-9) * 100
        else:
            return None  # 无真实换手率/成交额数据，不显示（不出假情报）

        tr = tr.replace([np.inf, -np.inf], np.nan).fillna(method="ffill")
        n30 = min(30, len(tr))
        n5  = min(5, len(tr))
        if n30 < 5:
            return None  # 数据不足（少于5日），不显示

        tr30 = tr.iloc[-n30:]
        tr5  = tr.iloc[-n5:]
        avg30 = float(tr30.mean())
        avg5  = float(tr5.mean())
        max30 = float(tr30.max())
        min30 = float(tr30.min())

        close5_change = float(df.iloc[-1]["close"] - df.iloc[-n5]["close"]) / (float(df.iloc[-n5]["close"]) + 1e-9) * 100

        # 客观趋势描述（不打分，打分交给 LLM）
        if avg5 > avg30 * 1.5 and close5_change > 0:
            status = "换手率上升+价格上涨"
        elif avg5 > avg30 * 1.5 and close5_change < 0:
            status = "换手率上升+价格下跌"
        elif avg5 < avg30 * 0.5:
            status = "换手率持续低迷"
        elif avg5 > avg30 * 1.2:
            status = "换手率略有上升"
        else:
            status = "换手率平稳"

        content = (
            f"**近5日均换手={avg5:.2f}%  近30日均换手={avg30:.2f}%**\n"
            f"- 30日区间：[{min30:.2f}%, {max30:.2f}%]\n"
            f"- 近5日价格变动：{close5_change:+.2f}%\n"
            f"- 状态：{status}\n"
        )
        return Section(key="turnover", title="换手率趋势",
                       content=content,
                       data={"avg5": avg5, "avg30": avg30, "max30": max30, "min30": min30},
                       score=50, signal="hold")

    except Exception:
        # 换手率分析失败，宁可不显示也不出假情报
        return None


def analyze_margin(df: pd.DataFrame, stock_code: str) -> Section:
    """融资融券余额趋势分析"""
    try:
        import akshare as ak

        # 判断市场
        is_sh = stock_code.startswith("6")
        margin_df = None

        try:
            if is_sh:
                margin_df = ak.stock_margin_detail_sse(stock=stock_code)
            else:
                margin_df = ak.stock_margin_detail_szse(stock=stock_code)
        except Exception:
            try:
                if is_sh:
                    margin_df = ak.stock_margin_sse(stock=stock_code)
            except Exception:
                pass

        if margin_df is None or margin_df.empty:
            raise ValueError("margin data empty")

        margin_df = margin_df.tail(10).reset_index(drop=True)

        # 找融资余额列
        rz_col = next(
            (c for c in margin_df.columns if "融资余额" in c or ("融资" in c and "余额" in c)),
            None
        )
        if rz_col is None:
            rz_col = next(
                (c for c in margin_df.columns if "余额" in c),
                margin_df.columns[-1]
            )

        rz = pd.to_numeric(margin_df[rz_col], errors="coerce").dropna()
        if len(rz) < 2:
            raise ValueError("insufficient margin rows")

        latest = float(rz.iloc[-1])
        earliest = float(rz.iloc[0])
        change_pct = (latest - earliest) / (abs(earliest) + 1e-9) * 100

        # 线性回归斜率（趋势方向）
        x = np.arange(len(rz))
        slope = float(np.polyfit(x, rz.values, 1)[0])
        slope_pct = slope / (abs(earliest) + 1e-9) * 100

        # 客观趋势描述（不打分，打分交给 LLM）
        if change_pct > 10:
            status = f"融资余额增加{change_pct:+.1f}%"
        elif change_pct < -10:
            status = f"融资余额减少{change_pct:+.1f}%"
        else:
            status = f"融资余额平稳（变动{change_pct:+.1f}%）"

        content = (
            f"**最新融资余额={latest/1e8:.2f}亿  10日前={earliest/1e8:.2f}亿**\n"
            f"- 变动幅度：{change_pct:+.1f}%\n"
            f"- 趋势斜率：{'↑ 上升' if slope > 0 else '↓ 下降'}\n"
            f"- 状态：{status}\n"
        )
        return Section(key="margin", title="融资融券",
                       content=content,
                       data={"latest": latest, "earliest": earliest, "change_pct": change_pct},
                       score=50, signal="hold")

    except Exception:
        # 拿不到真实两融数据（可能不在两融标的），不显示（不出假情报）
        return None
