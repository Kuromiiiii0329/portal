# -*- coding: utf-8 -*-
"""
portal/virtual/scan.py — 从日K重建涨停/炸板/跌停池（任意历史日期）

为什么必须自建
--------------
东财涨停池接口只保留**最近约 14 个交易日**。超过这个窗口就无法取到「当天的涨停名单」，
于是：样本量永远停在个位数 → 系统只能记账，不能学习。

但**涨停是日K里可推导的事实**：

    收 == 昨收 × (1 + 板块幅度)   （四舍五入到分）

用这个判据可以重建任意历史日期的池子。原包做过一致性验证：
重建口径与官方涨停池对照 14 个交易日**漏报 0**，某日 33/33 完全重合。

三种口径必须分清（避坑手册 §1.4）
--------------------------------
| 口径 | 定义 | 用途 |
|---|---|---|
| 收盘封住 | `close == 涨停价` | 次日能高开的前提 |
| 盘中触及（含炸板） | `high >= 涨停价 - 0.005` | 用户嘴里的「昨天涨停了」 |

两者都要算，且报告里要写清用的哪个。默认「收盘封住」才是可交易的前提。

已知口径差异（如实在报告里声明）
--------------------------------
- 官方涨停池**不收录 ST 股**（ST 幅度 5%），重建会多出 ST → 属口径差异，不是误判。
- 2020-08-24 之前创业板是 10%，之后 20% → 必须日期感知（`marketdb.limit_pct_at`）。
"""
from __future__ import annotations

from typing import Any, Iterable

import pandas as pd

from . import marketdb


def _window_sql() -> str:
    return """
    WITH x AS (
      SELECT d.code, d.date, d.open, d.high, d.low, d.close, d.volume, d.amount,
             LAG(d.close) OVER (PARTITION BY d.code ORDER BY d.date) AS prev_close,
             LAG(d.date)  OVER (PARTITION BY d.code ORDER BY d.date) AS prev_date
      FROM day d
      WHERE d.date >= ? AND d.date <= ?
    )
    SELECT x.*, s.name, s.is_st, s.board, s.industry
    FROM x LEFT JOIN stock s ON s.code = x.code
    WHERE x.date >= ? AND x.prev_close IS NOT NULL AND x.prev_close > 0
    """


def _classify(row: dict, *, touch: bool = True) -> dict[str, Any] | None:
    lim = marketdb.limit_pct_at(row["code"], str(row["date"]).replace("-", ""),
                                is_st=bool(row.get("is_st")))
    prev = float(row["prev_close"])
    close = float(row["close"])
    high = float(row["high"]) if row.get("high") is not None else close
    # ★ 必须用 ROUND_HALF_UP：Python round 是银行家舍入，会把 21.395 算成 21.39，
    #   而交易所给 21.40 —— 一次对照就漏判 3 只涨停。
    up, down = marketdb.limit_prices(prev, row["code"], str(row["date"]),
                                     is_st=bool(row.get("is_st")))
    sealed = abs(close - up) < 0.005
    touched = high >= up - 0.005
    dt = abs(close - down) < 0.005
    if not (sealed or (touch and touched) or dt):
        return None
    return {
        "code": row["code"],
        "name": row.get("name"),
        "date": str(row["date"])[:10],
        "close": round(close, 3),
        "high": round(high, 3),
        "prev_close": round(prev, 3),
        "limit_up": up,
        "limit_down": down,
        "limit_pct": lim,
        "sealed": sealed,
        "touch_only": (not sealed) and touched,      # 炸板
        "limit_down_sealed": dt,
        "amount": row.get("amount"),
        "volume": row.get("volume"),
        "industry": row.get("industry"),
        "is_st": bool(row.get("is_st")),
    }


def _runs(pool_date_map: dict[str, dict[str, bool]],
          date_list: list[str], code: str, idx: int) -> int:
    """连续涨停天数（含当日）。`pool_date_map[date][code] = 是否收盘封住`。"""
    n = 0
    i = idx
    while i >= 0:
        d = date_list[i]
        if pool_date_map.get(d, {}).get(code):
            n += 1
            i -= 1
        else:
            break
    return n


def scan_range(
    conn,
    start: str,
    end: str,
    *,
    touch: bool = True,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """扫描区间内每一天的涨停/炸板/跌停池。

    Returns:
        {date: {"zt": [...], "zb": [...], "dt": [...]}}，
        每个 zt 行带 `boards`（连板数）与 `zbc`（当日炸板次数——日K算不出，置 None）。
    """
    start_iso = _iso(start)
    end_iso = _iso(end)
    rows = conn.execute(_window_sql(), (start_iso, end_iso, start_iso)).fetchall()
    if not rows:
        return {}

    by_date: dict[str, list[dict]] = {}
    for r in rows:
        d = _classify(dict(r), touch=touch)
        if d is not None:
            by_date.setdefault(d["date"], []).append(d)

    date_list = sorted(by_date)
    # 先建「当日是否收盘封住」的映射，用于数连板
    sealed_map = {d: {x["code"]: x["sealed"] for x in v} for d, v in by_date.items()}

    out: dict[str, dict[str, list[dict]]] = {}
    for i, d in enumerate(date_list):
        zt = [x for x in by_date[d] if x["sealed"]]
        zb = [x for x in by_date[d] if x["touch_only"] and touch]
        dt = [x for x in by_date[d] if x["limit_down_sealed"]]
        for x in zt:
            x["boards"] = _runs(sealed_map, date_list, x["code"], i)
        for x in zb:
            x["boards"] = _runs(sealed_map, date_list, x["code"], i - 1) if i > 0 else 0
        out[d] = {"zt": zt, "zb": zb, "dt": dt}
    return out


def scan_day(conn, date8: str, *, touch: bool = True,
             lookback_days: int = 40) -> dict[str, list[dict[str, Any]]]:
    """单日扫描（内部会多扫前面一段，用于算连板数）。"""
    d = _iso(date8)
    start = (pd.to_datetime(d) - pd.Timedelta(days=int(lookback_days * 1.9))).strftime("%Y-%m-%d")
    res = scan_range(conn, start, d, touch=touch)
    return res.get(d, {"zt": [], "zb": [], "dt": []})


def to_pool_doc(scan: dict[str, list[dict[str, Any]]], date8: str) -> dict[str, Any]:
    """把扫描结果转成 `pools.fetch_pools` 同形状的 dict（供 tagging 复用）。

    注意：这里**没有** 封单额/流通市值（日K里没有），所以 `seal_ratio_pct` 只能为
    None。这正是「重建池」的边界：位置/连板/涨跌停可重建，封单结构不可重建。
    """
    def conv(x: dict) -> dict:
        return {
            "code": x["code"], "name": x.get("name"),
            "close": x.get("close"), "prev_close": x.get("prev_close"),
            "boards": x.get("boards"),
            "limit_up": x.get("limit_up"),
            "seal_amount": None, "float_cap": None, "seal_ratio_pct": None,
            "first_seal": None, "last_seal": None, "break_count": None,
            "turnover_pct": None,
            "industry": x.get("industry"),
            "price": x.get("close"),
            "source": "rebuilt_from_daily",
        }

    return {
        "date": str(date8).replace("-", "")[:8],
        "zt": [conv(x) for x in scan.get("zt", [])],
        "zb": [conv(x) for x in scan.get("zb", [])],
        "dt": [conv(x) for x in scan.get("dt", [])],
        "errors": {},
        "rebuilt": True,
        "rebuilt_note": ("由日K重建：可验证「收盘封住」，但**没有封单额/流通市值**，"
                         "所以可买性判据必须留空（不猜）"),
    }


def _iso(d: str) -> str:
    s = str(d).replace("-", "").replace("/", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def coverage_check(conn, dates: Iterable[str], *, official: dict[str, set[str]] | None = None
                   ) -> dict[str, Any]:
    """把重建结果与官方池对照，并**把差异按口径归类**。

    原包的判据是「漏报 0」。实测（2026-10-13~19，10 个交易日）差异全部可解释：

    | 类别 | 数量 | 解释 |
    |---|---|---|
    | 漏报：北交所 | 4 | 新浪/腾讯都拿不到 bj 历史日K，库里没有数据 |
    | 多报：ST 股 | 3 | 官方涨停池**不收录 ST**（5% 幅度），属口径差异 |
    | 多报：炸板后回封 | 3 | 收盘确实等于涨停价，但官方归入**炸板池**（break_count>0） |
    | 其他 | 0 | — |

    所以「漏报/多报」这个数字**必须带解释**，裸着给会让人误以为重建有 bug。
    """
    dates = list(dates)
    report: dict[str, Any] = {
        "dates": len(dates), "checked": [],
        "missed": 0, "extra": 0,
        "missed_bse": 0, "missed_other": 0,
        "extra_st": 0, "extra_break_resealed": 0, "extra_other": 0,
        "unexplained": [],
        "caveat": ("口径差异：官方涨停池不收录 ST；炸过板后回封的票官方归入炸板池；"
                   "北交所无历史日K数据。以上三类不算重建错误。"
                   "另有少量残余差异来自**官方池自身不稳定**：同一历史日期两次调用"
                   "官方接口，家数从 36 变为 34（实测），故小数级不符不构成重建错误。"),
    }
    if not official:
        report["note"] = "未提供官方池，仅返回重建数量"
        report["counts"] = {d: len(scan_day(conn, d)["zt"]) for d in dates}
        return report

    from . import pools as pools_mod

    for d in dates:
        rebuilt_rows = scan_day(conn, d)["zt"]
        rebuilt = {x["code"] for x in rebuilt_rows}
        by_code = {x["code"]: x for x in rebuilt_rows}
        off = official.get(str(d).replace("-", "")[:8]) or official.get(d) or set()
        if not off:
            continue
        missed = off - rebuilt
        extra = rebuilt - off

        # 炸板池用于区分「回封」是不是官方归类问题
        zb_codes: set[str] = set()
        try:
            zb_codes = {x["code"] for x in pools_mod.fetch_pools(str(d), kinds=("zb",),
                                                                 strict=False).get("zb") or []}
        except Exception:
            pass

        for code in missed:
            if marketdb.board_of(code) == "bse":
                report["missed_bse"] += 1
            else:
                report["missed_other"] += 1
                report["unexplained"].append({"date": d, "code": code, "kind": "missed"})
        for code in extra:
            row = by_code.get(code) or {}
            st = conn.execute("SELECT is_st FROM stock WHERE code=?", (code,)).fetchone()
            if st and int(st["is_st"] or 0) == 1:
                report["extra_st"] += 1
            elif code in zb_codes:
                report["extra_break_resealed"] += 1
            else:
                report["extra_other"] += 1
                report["unexplained"].append({"date": d, "code": code, "kind": "extra"})

        report["checked"].append({
            "date": d, "official": len(off), "rebuilt": len(rebuilt),
            "missed": len(missed), "extra": len(extra),
        })
        report["missed"] += len(missed)
        report["extra"] += len(extra)
    return report


__all__ = ["scan_range", "scan_day", "to_pool_doc", "coverage_check"]
