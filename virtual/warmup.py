# -*- coding: utf-8 -*-
"""
portal/virtual/warmup.py — 全市场预热：批量生成特征（提升 zone_pos_120 覆盖）

问题
----
`tagging` 的 `zone_pos_120` 依赖 K 线。以前 K 线只来自 `data_cache`（你手动分析过
哪些票就有哪些票），所以在真实涨停池上跑特征时覆盖率经常是 0 —— 位置类规则无法求值。

解法
----
回测数据库建好之后，全市场日K都在，于是：

    任意历史日期 → 从日K重建涨停池（scan）→ 用数据库 K 线算位置 → 落盘特征

这样 `zone_pos_120` 的覆盖率从「取决于你点过哪些票」变成「取决于数据库覆盖」，
通常接近 100%（新上市不足 120 日的票除外，那种情况如实留空）。

还有两个诚实边界
----------------
1. **封单额 / 流通市值 无法从日K重建** → `seal_ratio_pct` 必然为 null。
   可买性判据在历史回补上是**不可得**的，只能用于当日实时（`pools.fetch_pools`）。
2. **题材归因（同花顺）只有近期** → 历史日的 `hot_reason` 为 null。
"""
from __future__ import annotations

import datetime
from typing import Any, Callable, Iterable

from . import datafeed, paths, scan, tagging


def trading_days(conn, start: str, end: str) -> list[str]:
    rows = conn.execute(
        "SELECT date FROM calendar WHERE date>=? AND date<=? AND is_open=1 ORDER BY date",
        (scan._iso(start), scan._iso(end))).fetchall()
    return [str(r[0]).replace("-", "")[:8] for r in rows]


def _prev_trading_day(conn, date8: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) FROM day WHERE date < ?", (scan._iso(date8),)).fetchone()
    return str(row[0]).replace("-", "")[:8] if row and row[0] else None


def warm_day(
    conn,
    date8: str,
    *,
    write: bool = True,
    lookback: int = 160,
    max_stocks: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """生成某一天的 pre 特征。池子来自日K重建（任意历史日期都行）。"""
    d8 = str(date8).replace("-", "")[:8]
    scan_res = scan.scan_day(conn, d8)
    pool = scan.to_pool_doc(scan_res, d8)
    zt = pool["zt"]
    if max_stocks:
        zt = zt[:max_stocks]

    codes = [x["code"] for x in zt]
    klines = datafeed.load_many(codes, date8=d8, lookback=lookback)

    # 市场情绪快照：pool 已含 zd/dt，但缺封单 → 情绪分仍可算（用计数）
    from . import pools as pools_mod
    snapshot = pools_mod.emotion_snapshot(pool["zt"], pool["zb"], pool["dt"])

    doc = tagging.build_day(d8, snapshot=snapshot, stocks=zt, klines_by_code=klines)
    doc["rebuilt_pool"] = True
    doc["rebuilt_note"] = pool.get("rebuilt_note")
    doc["kline_source"] = "marketdb"
    doc["kline_hit"] = len(klines)
    doc["stocks_n"] = len(zt)

    n_zone = sum(1 for s in doc["stocks"] if s["pre"].get("zone_pos_120") is not None)
    doc["zone_pos_120_cover"] = f"{n_zone}/{len(doc['stocks'])}"

    if write:
        p = tagging.write_features(doc)
        doc["path"] = str(p)
    if progress:
        progress(f"  {d8}: 涨停 {len(zt)} 只，K线命中 {len(klines)}，"
                 f"区间位覆盖 {n_zone}/{len(doc['stocks'])}")
    return doc


def warm_range(
    conn,
    start: str,
    end: str,
    *,
    write: bool = True,
    limit_days: int | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    """批量预热一段日期。返回汇总统计。"""
    days = trading_days(conn, start, end)
    if limit_days:
        days = days[-limit_days:]
    if not days:
        return {"days": 0, "note": "区间内没有交易日（先跑 marketdb init 灌交易日历）"}

    t0 = datetime.datetime.now()
    ok = empty = 0
    total_zt = cover_num = cover_den = 0
    for i, d in enumerate(days, 1):
        try:
            doc = warm_day(conn, d, write=write, progress=None)
        except Exception as exc:
            empty += 1
            progress(f"  [{i}/{len(days)}] {d} 失败：{type(exc).__name__}: {exc}")
            continue
        n = doc.get("stocks_n", 0)
        if n == 0:
            empty += 1
            continue
        ok += 1
        total_zt += n
        c = doc.get("zone_pos_120_cover", "0/0").split("/")
        cover_num += int(c[0])
        cover_den += int(c[-1])
        if i % 20 == 0 or i == len(days):
            el = (datetime.datetime.now() - t0).total_seconds()
            progress(f"  [{i}/{len(days)}] 已生成 {ok} 天，涨停累计 {total_zt} 只，"
                     f"{el:.0f}s")

    return {
        "days_requested": len(days),
        "days_written": ok,
        "days_empty": empty,
        "limit_up_rows": total_zt,
        "zone_pos_120_coverage": (f"{cover_num}/{cover_den} "
                                  f"({cover_num / cover_den * 100:.1f}%)"
                                  if cover_den else "n/a"),
        "elapsed_s": round((datetime.datetime.now() - t0).total_seconds(), 1),
    }


def rebuild_official_check(conn, dates: Iterable[str]) -> dict[str, Any]:
    """把重建的涨停池与**官方接口**对照（地基自检）。

    原包的判据是「漏报 0」。这里同样断言漏报，只对官方仍可取的日期有效
    （接口只保留约 14 个交易日）。
    """
    from . import pools as pools_mod

    official: dict[str, set[str]] = {}
    for d in dates:
        try:
            doc = pools_mod.fetch_pools(str(d), kinds=("zt",), strict=True)
            codes = {x["code"] for x in doc.get("zt") or []}
            if codes:
                official[str(d).replace("-", "")[:8]] = codes
        except Exception:
            continue
    return scan.coverage_check(conn, list(official.keys()), official=official)


def main(argv: list[str] | None = None) -> int:
    """python -m portal.virtual.warmup [--start 20260901] [--end 20261016] [--days 20]"""
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="portal.virtual.warmup")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--days", type=int, default=None, help="只预热最近 N 个交易日")
    ap.add_argument("--check", action="store_true", help="与官方涨停池对照（地基自检）")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    conn = datafeed.connect()
    if conn is None:
        print("找不到回测数据库。先跑：python -m portal.virtual.marketdb init && "
              "python -m portal.virtual.marketdb build")
        return 2

    end = args.end or marketdb_latest(conn)
    start = args.start or end
    if args.days:
        days = trading_days(conn, "1990-01-01", end)[-args.days:]
        start = days[0] if days else end

    if args.check:
        recent = trading_days(conn, "1990-01-01", end)[-10:]
        print(json.dumps(rebuild_official_check(conn, recent), ensure_ascii=False, indent=2))
        return 0

    stats = warm_range(conn, start, end, write=not args.no_write)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def marketdb_latest(conn) -> str:
    row = conn.execute("SELECT MAX(date) FROM day").fetchone()
    return str(row[0]).replace("-", "")[:8] if row and row[0] else \
        datetime.date.today().strftime("%Y%m%d")


if __name__ == "__main__":
    raise SystemExit(main())
