# -*- coding: utf-8 -*-
"""
portal/virtual/marketdb.py — A 股回测数据库（一次性全量 + 每日增量）

为什么必须自建
--------------
涨停池接口只保留最近约 14 个交易日，分时只给当天 → 样本量永远停在个位数，
系统只能「记账」不能「学习」。而**涨停是日K里可推导的事实**，所以日K面板是地基。

实测结论（本机 2026-10，见 docs/data-layer.md）
----------------------------------------------
| 源 | 结果 |
|---|---|
| 东财 `push2his`（`ak.stock_zh_a_hist` / efinance） | ❌ ProxyError，**不可用** |
| 新浪 `ak.stock_zh_a_daily(adjust="")` | ✅ 352ms/只，字段全（含 outstanding_share/turnover） |
| 新浪 `ak.stock_zh_a_daily(adjust="hfq-factor")` | ✅ 50ms/只，33 个事件（多年） |
| `ak.tool_trade_date_hist_sina` | ✅ 8797 个交易日 |
| 8 进程并发 | ✅ 108ms/只 → 全市场约 10 分钟 |

复权方案（已验证，最大绝对误差 0.005 元 = 分位四舍五入）
--------------------------------------------------------
    hfq_close(t) = close(t) × factor(t)     factor 按事件日向前填充
    qfq_close(t) = close(t) × factor(t) / factor(末值)

所以**只存原始价 + 因子事件**，复权价按需推导。这样同时得到两套价格体系：
- 真实价（不复权）→ 判涨跌停、价格笼子、成交可行性
- 复权价 → 收益、均线、净值

`SKILL.md` 明确警告过：用错会怎样 —— 复权后的价格永远「不涨停」（被缩放了）；
不复权的均线是坏的（送转跳空污染之后 60 个交易日）。

存原始价的好处是不可逆操作最少：因子可以随时重算，复权价可以随时重建。
"""
from __future__ import annotations

import datetime
import json
import os
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 1
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "market.db"

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS calendar (
  date TEXT PRIMARY KEY,
  is_open INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stock (
  code TEXT PRIMARY KEY,
  name TEXT,
  market TEXT,              -- sh / sz / bj
  board TEXT,               -- main / gem / star / bse
  limit_pct REAL,           -- 当前制度下的涨跌停幅度
  is_st INTEGER DEFAULT 0,
  listed_date TEXT,
  total_share REAL,
  float_share REAL,
  industry TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS day (
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL,
  outstanding_share REAL, turnover REAL,
  PRIMARY KEY (code, date)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_day_date ON day(date);

CREATE TABLE IF NOT EXISTS adj_event (
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  hfq_factor REAL NOT NULL,
  PRIMARY KEY (code, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ingest_log (
  code TEXT PRIMARY KEY,
  last_date TEXT,
  rows INTEGER DEFAULT 0,
  factor_rows INTEGER DEFAULT 0,
  status TEXT,              -- ok / empty / error
  err TEXT,
  updated_at TEXT
);
"""


# ─────────────────────────────────────────────────────────────
# 基础
# ─────────────────────────────────────────────────────────────
def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("PORTAL_MARKET_DB") or DEFAULT_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for t in ("stock", "day", "adj_event", "calendar", "ingest_log"):
        try:
            out[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        except sqlite3.Error:
            out[t] = -1
    return out


# ─────────────────────────────────────────────────────────────
# 板块与涨跌停
# ─────────────────────────────────────────────────────────────
def board_of(code: str) -> str:
    c = str(code)
    if c.startswith(("688", "689")):
        return "star"          # 科创板 20%
    if c.startswith(("300", "301")):
        return "gem"           # 创业板（2020-08-24 起 20%）
    if c.startswith(("4", "8", "9")):
        return "bse"           # 北交所 30%
    return "main"              # 主板 10%


def limit_pct_at(code: str, date8: str | None = None, *, is_st: bool = False) -> float:
    """**日期感知**的涨跌停幅度。

    创业板 300/301 在 2020-08-24 之前是 10%，之后才是 20%。
    不处理这个分界点，2020 年前后的创业板信号会全错（避坑手册 §1.3）。
    """
    if is_st:
        return 0.05
    b = board_of(code)
    if b == "star":
        return 0.20
    if b == "bse":
        return 0.30
    if b == "gem":
        d = str(date8 or "")[:8]
        if d and d < "20200824":
            return 0.10
        return 0.20
    return 0.10


def is_st_name(name: str | None) -> bool:
    n = str(name or "").upper()
    return "ST" in n or "*ST" in n or "退" in n


# ─────────────────────────────────────────────────────────────
# 股票列表
# ─────────────────────────────────────────────────────────────
def fetch_stock_list() -> list[dict[str, Any]]:
    """沪 + 深 + 北 三张表合成一份列表（带上市日期/股本/行业）。

    `stock_info_a_code_name()` 也能用（5575 只，7.4 秒），但它没有上市日期/行业，
    且历史上因北交所失败过。所以分市场取，失败的市场单独降级。
    """
    import akshare as ak

    rows: dict[str, dict[str, Any]] = {}

    try:
        sh = ak.stock_info_sh_name_code(symbol="主板A股")
        for r in sh.to_dict("records"):
            code = str(r.get("证券代码") or "").strip()
            if len(code) == 6:
                rows[code] = {"code": code, "name": str(r.get("证券简称") or "").strip(),
                              "market": "sh", "listed_date": str(r.get("上市日期") or "")[:10],
                              "total_share": None, "float_share": None, "industry": None}
    except Exception:
        pass
    try:
        sh2 = ak.stock_info_sh_name_code(symbol="科创板")
        for r in sh2.to_dict("records"):
            code = str(r.get("证券代码") or "").strip()
            if len(code) == 6:
                rows.setdefault(code, {})
                rows[code].update({"code": code,
                                   "name": str(r.get("证券简称") or "").strip(),
                                   "market": "sh",
                                   "listed_date": str(r.get("上市日期") or "")[:10],
                                   "total_share": None, "float_share": None,
                                   "industry": None})
    except Exception:
        pass
    try:
        sz = ak.stock_info_sz_name_code(symbol="A股列表")
        for r in sz.to_dict("records"):
            code = str(r.get("A股代码") or "").strip()
            if len(code) == 6:
                rows[code] = {
                    "code": code, "name": str(r.get("A股简称") or "").strip(),
                    "market": "sz",
                    "listed_date": str(r.get("A股上市日期") or "")[:10],
                    "total_share": _f(r.get("A股总股本")),
                    "float_share": _f(r.get("A股流通股本")),
                    "industry": str(r.get("所属行业") or "").strip() or None,
                }
    except Exception:
        pass
    try:
        bj = ak.stock_info_bj_name_code()
        for r in bj.to_dict("records"):
            code = str(r.get("证券代码") or "").strip()
            if len(code) == 6:
                rows[code] = {"code": code, "name": str(r.get("证券简称") or "").strip(),
                              "market": "bj",
                              "listed_date": str(r.get("上市日期") or "")[:10],
                              "total_share": _f(r.get("总股本")),
                              "float_share": _f(r.get("流通股本")),
                              "industry": str(r.get("所属行业") or "").strip() or None}
    except Exception:
        pass

    if not rows:
        # 最后兜底：只有代码和名称，没有其他字段
        for r in ak.stock_info_a_code_name().to_dict("records"):
            code = str(r.get("code") or "").strip()
            if len(code) == 6:
                rows[code] = {"code": code, "name": str(r.get("name") or "").strip(),
                              "market": "sh" if code[0] in "69" else ("bj" if code[0] in "48" else "sz"),
                              "listed_date": None, "total_share": None,
                              "float_share": None, "industry": None}

    out = []
    for code, r in rows.items():
        if not r.get("market"):
            r["market"] = "sh" if code[0] in "69" else ("bj" if code[0] in "48" else "sz")
        r["board"] = board_of(code)
        r["is_st"] = 1 if is_st_name(r.get("name")) else 0
        r["limit_pct"] = limit_pct_at(code, None, is_st=bool(r["is_st"]))
        out.append(r)
    return sorted(out, key=lambda x: x["code"])


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def refresh_stock_list(conn: sqlite3.Connection) -> int:
    rows = fetch_stock_list()
    now = _now()
    with conn:
        for r in rows:
            conn.execute(
                """INSERT INTO stock(code,name,market,board,limit_pct,is_st,
                                     listed_date,total_share,float_share,industry,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name, market=excluded.market, board=excluded.board,
                     limit_pct=excluded.limit_pct, is_st=excluded.is_st,
                     listed_date=COALESCE(excluded.listed_date, stock.listed_date),
                     total_share=COALESCE(excluded.total_share, stock.total_share),
                     float_share=COALESCE(excluded.float_share, stock.float_share),
                     industry=COALESCE(excluded.industry, stock.industry),
                     updated_at=excluded.updated_at""",
                (r["code"], r["name"], r["market"], r["board"], r["limit_pct"],
                 r["is_st"], r.get("listed_date"), r.get("total_share"),
                 r.get("float_share"), r.get("industry"), now))
    return len(rows)


def refresh_calendar(conn: sqlite3.Connection) -> int:
    import akshare as ak
    df = ak.tool_trade_date_hist_sina()
    dates = [str(d)[:10] for d in df["trade_date"].tolist()]
    with conn:
        conn.executemany("INSERT OR REPLACE INTO calendar(date, is_open) VALUES(?, 1)",
                         [(d,) for d in dates])
    return len(dates)


def universe(conn: sqlite3.Connection, *, markets: Iterable[str] | None = None) -> list[str]:
    q = "SELECT code FROM stock"
    args: list[Any] = []
    if markets:
        ms = list(markets)
        q += " WHERE market IN (" + ",".join("?" * len(ms)) + ")"
        args = ms
    return [r[0] for r in conn.execute(q + " ORDER BY code", args)]


# ─────────────────────────────────────────────────────────────
# 单只抓取（在子进程里跑）
# ─────────────────────────────────────────────────────────────
def _sina_symbol(code: str) -> str:
    c = str(code)
    return ("sh" if c[0] in "69" else ("bj" if c[0] in "48" else "sz")) + c


def fetch_one(code: str, *, sleep: float = 0.0) -> dict[str, Any]:
    """抓一只票的原始日K + 复权因子。**只返回数据，不碰数据库**
    （多进程写 SQLite 会锁争用，写统一放主进程）。"""
    out: dict[str, Any] = {"code": code, "status": "ok", "err": None,
                           "bars": [], "factors": []}
    try:
        if sleep:
            time.sleep(sleep + random.random() * sleep)
        import akshare as ak
        sym = _sina_symbol(code)

        df = ak.stock_zh_a_daily(symbol=sym, adjust="")
        if df is None or len(df) == 0:
            out["status"] = "empty"
            return out
        bars = []
        for r in df.to_dict("records"):
            bars.append((
                str(r.get("date"))[:10],
                _f(r.get("open")), _f(r.get("high")), _f(r.get("low")), _f(r.get("close")),
                _f(r.get("volume")), _f(r.get("amount")),
                _f(r.get("outstanding_share")), _f(r.get("turnover")),
            ))
        out["bars"] = bars

        try:
            fac = ak.stock_zh_a_daily(symbol=sym, adjust="hfq-factor")
            if fac is not None and len(fac):
                out["factors"] = [(str(r.get("date"))[:10], _f(r.get("hfq_factor")))
                                  for r in fac.to_dict("records") if _f(r.get("hfq_factor"))]
        except Exception as exc:
            out["factor_err"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        return out
    except Exception as exc:
        out["status"] = "error"
        out["err"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return out


def _upsert(conn: sqlite3.Connection, res: dict[str, Any]) -> None:
    code = res["code"]
    now = _now()
    with conn:
        if res["bars"]:
            conn.executemany(
                """INSERT INTO day(code,date,open,high,low,close,volume,amount,
                                   outstanding_share,turnover)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(code,date) DO UPDATE SET
                     open=excluded.open, high=excluded.high, low=excluded.low,
                     close=excluded.close, volume=excluded.volume, amount=excluded.amount,
                     outstanding_share=excluded.outstanding_share,
                     turnover=excluded.turnover""",
                [(code, *b) for b in res["bars"]])
        if res["factors"]:
            conn.executemany(
                """INSERT OR REPLACE INTO adj_event(code,date,hfq_factor) VALUES(?,?,?)""",
                [(code, d, f) for d, f in res["factors"]])
        last = res["bars"][-1][0] if res["bars"] else None
        conn.execute(
            """INSERT INTO ingest_log(code,last_date,rows,factor_rows,status,err,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 last_date=excluded.last_date, rows=excluded.rows,
                 factor_rows=excluded.factor_rows, status=excluded.status,
                 err=excluded.err, updated_at=excluded.updated_at""",
            (code, last, len(res["bars"]), len(res["factors"]),
             res["status"], res.get("err"), now))


# ─────────────────────────────────────────────────────────────
# 全量构建 / 增量更新
# ─────────────────────────────────────────────────────────────
def build(
    conn: sqlite3.Connection,
    *,
    workers: int = 8,
    limit: int | None = None,
    resume: bool = True,
    sleep: float = 0.0,
    progress: Callable[[str], None] = print,
    codes: Iterable[str] | None = None,
) -> dict[str, Any]:
    """一次性全量构建（可断点续传）。

    ⚠️ **Windows 上调用方必须有 `if __name__ == "__main__":` 保护。**
    multiprocessing 在 Windows 用 spawn：子进程会重新 import `__main__`，
    如果调用脚本的顶层没有保护，每个子进程都会再建一个进程池 → 全部
    "terminated abruptly"。实测踩过（30 只全灭）。最省事的做法是用
    `python -m portal.virtual.marketdb build`，它自带保护。

    `resume=True` 时跳过 `ingest_log.status='ok'` 且 `last_date` 不早于最近交易日的票。
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from concurrent.futures.process import BrokenProcessPool

    todo = list(codes) if codes is not None else universe(conn)
    if resume:
        last_td = latest_trade_date(conn)
        done = {r[0] for r in conn.execute(
            "SELECT code FROM ingest_log WHERE status IN ('ok','empty') AND last_date >= ?",
            (last_td or "",))}
        todo = [c for c in todo if c not in done]
    if limit:
        todo = todo[:limit]

    stats = {"requested": len(todo), "ok": 0, "empty": 0, "error": 0,
             "bars": 0, "factors": 0}
    if not todo:
        progress("没有待处理的代码（已是最新）")
        return stats

    t0 = time.perf_counter()
    progress(f"开始抓取 {len(todo)} 只（{workers} 进程）…")
    try:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_one, c, sleep=sleep): c for c in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    res = fut.result()
                except BrokenProcessPool:
                    raise
                except Exception as exc:
                    stats["error"] += 1
                    progress(f"  [{i}/{len(todo)}] 子进程异常 {futs[fut]}: {exc}")
                    continue
                _upsert(conn, res)
                stats[res["status"]] = stats.get(res["status"], 0) + 1
                stats["bars"] += len(res["bars"])
                stats["factors"] += len(res["factors"])
                if i % 200 == 0 or i == len(todo):
                    el = time.perf_counter() - t0
                    rate = i / el if el else 0
                    eta = (len(todo) - i) / rate / 60 if rate else 0
                    progress(f"  [{i}/{len(todo)}] ok={stats['ok']} empty={stats['empty']} "
                             f"err={stats['error']} 条数={stats['bars']} "
                             f"{rate:.1f}/s 剩余≈{eta:.1f}分钟")
    except BrokenProcessPool as exc:
        raise RuntimeError(
            "进程池被整体终止。最常见的两个原因：\n"
            "  1) 调用脚本顶层没有 `if __name__ == '__main__':` 保护"
            "（Windows spawn 会重新 import __main__）；\n"
            "  2) 子进程里初始化了不能 fork 的原生库（如 V8/py_mini_racer 的极早期初始化）。\n"
            "建议改用：python -m portal.virtual.marketdb build"
        ) from exc
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_build_at', ?)", (_now(),))
    conn.commit()
    stats["elapsed_s"] = round(time.perf_counter() - t0, 1)
    progress(f"完成：{stats}")
    return stats


def update(
    conn: sqlite3.Connection,
    *,
    workers: int = 8,
    lookback_days: int = 5,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    """每日增量更新。

    对每只票从 `last_date - lookback_days` 重新抓（不是从 last_date+1）：
    财报复权修正会改写历史，多抓几天才能覆盖。重叠写入用 UPSERT，幂等。
    """
    codes = universe(conn)
    stats = {"codes": len(codes), "ok": 0, "empty": 0, "error": 0, "bars": 0}
    if not codes:
        return stats
    progress(f"增量更新 {len(codes)} 只（每只回看 {lookback_days} 天）…")
    from concurrent.futures import ProcessPoolExecutor, as_completed

    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                res = fut.result()
            except Exception:
                stats["error"] += 1
                continue
            # 增量只写最近的 bar（全量返回也没关系：UPSERT 幂等，只是慢一点）
            _upsert(conn, res)
            stats[res["status"]] = stats.get(res["status"], 0) + 1
            stats["bars"] += len(res["bars"])
            if i % 500 == 0 or i == len(codes):
                progress(f"  [{i}/{len(codes)}] ok={stats['ok']} err={stats['error']}")
    stats["elapsed_s"] = round(time.perf_counter() - t0, 1)
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_update_at', ?)", (_now(),))
    conn.commit()
    return stats


def latest_trade_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) FROM day").fetchone()
    return row[0] if row and row[0] else None


# ─────────────────────────────────────────────────────────────
# 读取（给回测/特征层用）
# ─────────────────────────────────────────────────────────────
def load_kline(conn: sqlite3.Connection, code: str, *,
               start: str | None = None, end: str | None = None,
               with_adjusted: bool = True):
    """返回一只票的日K。

    - `open/high/low/close` 是**真实价**（不复权）→ 判涨跌停用
    - `aopen/ahigh/alow/aclose` 是**前复权价** → 收益/均线用
    - `pct_chg` 用**复权价**算（真实价在除权日会假跌）
    """
    import pandas as pd

    q = ("SELECT date,open,high,low,close,volume,amount,outstanding_share,turnover "
         "FROM day WHERE code=?")
    args: list[Any] = [code]
    if start:
        q += " AND date>=?"
        args.append(start)
    if end:
        q += " AND date<=?"
        args.append(end)
    q += " ORDER BY date"
    rows = conn.execute(q, args).fetchall()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df.attrs["code"] = code
    if not with_adjusted:
        return df

    factors = conn.execute(
        "SELECT date, hfq_factor FROM adj_event WHERE code=? ORDER BY date",
        (code,)).fetchall()
    if not factors:
        # 没有因子事件 = 从未除权除息/送转，前复权价等于真实价
        for c in ("open", "high", "low", "close"):
            df["a" + c] = df[c]
    else:
        fac = pd.DataFrame([dict(r) for r in factors])
        fac["date"] = pd.to_datetime(fac["date"])
        d = df.copy()
        d["_dt"] = pd.to_datetime(d["date"])
        merged = pd.merge_asof(d.sort_values("_dt"), fac.sort_values("date"),
                               left_on="_dt", right_on="date",
                               direction="backward", suffixes=("", "_fac"))
        merged = merged.sort_values("_dt").reset_index(drop=True)
        f = merged["hfq_factor"].ffill().bfill().astype(float)
        last = float(f.iloc[-1]) if len(f) and f.iloc[-1] else 1.0
        ratio = f / (last or 1.0)
        for c in ("open", "high", "low", "close"):
            df["a" + c] = (df[c].astype(float) * ratio).round(4)
        df.attrs["factor_last"] = last

    df["pct_chg"] = (df["aclose"].astype(float).pct_change() * 100).fillna(0.0).round(4)
    return df


def coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    """数据覆盖度体检 —— 用于判断「能不能做横截面统计」。"""
    counts = table_counts(conn)
    n_codes = counts.get("stock", 0)
    with_data = conn.execute("SELECT COUNT(DISTINCT code) FROM day").fetchone()[0]
    rng = conn.execute("SELECT MIN(date), MAX(date) FROM day").fetchone()
    per_code = conn.execute(
        "SELECT AVG(c), MIN(c), MAX(c) FROM (SELECT COUNT(*) c FROM day GROUP BY code)"
    ).fetchone()
    stale = conn.execute(
        "SELECT COUNT(*) FROM ingest_log WHERE status!='ok'").fetchone()[0]
    return {
        "stocks": n_codes,
        "codes_with_data": int(with_data or 0),
        "date_min": rng[0] if rng else None,
        "date_max": rng[1] if rng else None,
        "rows": counts.get("day", 0),
        "avg_bars_per_code": round(float(per_code[0] or 0), 1),
        "min_bars": int(per_code[1] or 0),
        "max_bars": int(per_code[2] or 0),
        "adj_event_rows": counts.get("adj_event", 0),
        "not_ok_codes": int(stale or 0),
        "meta": {r[0]: r[1] for r in conn.execute("SELECT k,v FROM meta")},
    }


def spot_check(conn: sqlite3.Connection, code: str = "600519") -> dict[str, Any]:
    """抽一只票做人工可核对的对照（复权价 vs 真实价、涨停价是否算得对）。"""
    df = load_kline(conn, code)
    if df.empty:
        return {"code": code, "error": "无数据"}
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    st = conn.execute("SELECT name, board, limit_pct, is_st FROM stock WHERE code=?",
                      (code,)).fetchone()
    lim = limit_pct_at(code, str(last["date"]), is_st=bool(st["is_st"]) if st else False)
    return {
        "code": code,
        "name": st["name"] if st else None,
        "board": st["board"] if st else None,
        "last_date": str(last["date"]),
        "raw_close": round(float(last["close"]), 4),
        "adj_close": round(float(last["aclose"]), 4),
        "factor_last": df.attrs.get("factor_last"),
        "prev_raw_close": round(float(prev["close"]), 4),
        "limit_up_price": round(float(prev["close"]) * (1 + lim), 2),
        "limit_pct": lim,
        "bars": len(df),
        "first_date": str(df.iloc[0]["date"]),
    }


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_PATH",
    "connect",
    "init_schema",
    "table_counts",
    "board_of",
    "limit_pct_at",
    "is_st_name",
    "fetch_stock_list",
    "refresh_stock_list",
    "refresh_calendar",
    "universe",
    "fetch_one",
    "build",
    "update",
    "latest_trade_date",
    "load_kline",
    "coverage",
    "spot_check",
    "main",
]


# ─────────────────────────────────────────────────────────────
# CLI（自带 `if __name__ == "__main__"` 保护，规避 Windows spawn 陷阱）
# ─────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    """python -m portal.virtual.marketdb [init|build|update|status|spot|list]"""
    import argparse

    ap = argparse.ArgumentParser(prog="portal.virtual.marketdb",
                                description="A 股回测数据库：全量构建 / 每日增量")
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["init", "build", "update", "status", "spot", "list"])
    ap.add_argument("--db", default=None, help="数据库路径（默认 portal/data/market.db）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只（冒烟用）")
    ap.add_argument("--no-resume", action="store_true", help="不跳过已完成项")
    ap.add_argument("--sleep", type=float, default=0.0, help="每只额外随机等待秒数")
    ap.add_argument("--code", default="600519")
    ap.add_argument("--market", default=None, help="只处理市场：sh/sz/bj")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    init_schema(conn)

    if args.cmd == "init":
        n = refresh_stock_list(conn)
        c = refresh_calendar(conn)
        print(f"股票 {n} 只，交易日历 {c} 天")
        return 0

    if args.cmd == "build":
        if not table_counts(conn)["stock"]:
            print("先灌股票列表…")
            refresh_stock_list(conn)
            refresh_calendar(conn)
        markets = [args.market] if args.market else None
        codes = universe(conn, markets=markets) if markets else None
        stats = build(conn, workers=args.workers, limit=args.limit,
                      resume=not args.no_resume, sleep=args.sleep, codes=codes)
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    if args.cmd == "update":
        stats = update(conn, workers=args.workers)
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    if args.cmd == "spot":
        print(json.dumps(spot_check(conn, args.code), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "list":
        rows = conn.execute(
            "SELECT market, COUNT(*) FROM stock GROUP BY market ORDER BY market").fetchall()
        for m, n in rows:
            print(f"  {m}: {n}")
        print(f"  合计 {sum(n for _, n in rows)}")
        return 0

    print(json.dumps(coverage(conn), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
