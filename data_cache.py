"""
portal/data_cache.py — 股票数据本地缓存管理器

结构：
  portal/data/stocks/{code}/
    kline.csv          — 日线 K 线（增量更新）
    meta.json          — 元信息（名称、关键词、最后更新）
    commodities/
      {keyword}.csv    — 相关商品/产品价格搜索缓存

使用：
  from portal.data_cache import StockDataCache
  cache = StockDataCache()
  mode, start, end = cache.calc_fetch_range("002466")
"""
from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TZ_CN  = timezone(timedelta(hours=8))
_KLINE_COLS = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "fetch_source"]

# 商品搜索缓存 TTL（秒）
_TTL_INTRADAY  = 2 * 3600    # 盘中 2h
_TTL_POSTCLOSE = 6 * 3600    # 盘后 6h
_TTL_HOLIDAY   = 24 * 3600   # 非交易日 24h


def _now_cn() -> datetime:
    return datetime.now(TZ_CN)


def _is_trading_hours() -> bool:
    """粗判：是否处于 A 股交易时段（周一至周五 09:30-15:00）。"""
    now = _now_cn()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= t <= 15 * 60


def _commodity_ttl() -> int:
    return _TTL_INTRADAY if _is_trading_hours() else _TTL_POSTCLOSE


def _safe_filename(keyword: str) -> str:
    """把关键词转成安全文件名，保留中文字符。"""
    safe = re.sub(r'[\\/:*?"<>|]', "_", keyword)
    return safe[:60]  # 最长 60 字符


def _last_trading_date() -> str:
    """返回最近一个已收盘的 A 股交易日（字符串 'YYYY-MM-DD'）。
    逻辑：从今天往前找，跳过周六/周日，最多回溯 7 天。
    15:00 之后认为当日已收盘，15:00 之前认为昨日是最后收盘日。
    """
    now = _now_cn()
    # 盘后（15:00+）才把今天算入
    candidate = now.date() if (now.hour > 15 or (now.hour == 15 and now.minute >= 0)) else (now - timedelta(days=1)).date()
    for _ in range(7):
        if candidate.weekday() < 5:   # 周一~周五，暂不检查节假日
            return candidate.strftime("%Y-%m-%d")
        candidate -= timedelta(days=1)
    return (now - timedelta(days=3)).strftime("%Y-%m-%d")  # fallback


class StockDataCache:
    """股票数据本地缓存管理器。"""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.root = cache_dir or Path(__file__).parent / "data" / "stocks"
        self.root.mkdir(parents=True, exist_ok=True)

    # ── 路径辅助 ─────────────────────────────────────────────
    def _stock_dir(self, code: str) -> Path:
        d = self.root / code
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _kline_path(self, code: str) -> Path:
        return self._stock_dir(code) / "kline.csv"

    def _meta_path(self, code: str) -> Path:
        return self._stock_dir(code) / "meta.json"

    def _commodity_dir(self, code: str) -> Path:
        d = self._stock_dir(code) / "commodities"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _commodity_path(self, code: str, keyword: str) -> Path:
        return self._commodity_dir(code) / f"{_safe_filename(keyword)}.csv"

    # ── K 线缓存 ─────────────────────────────────────────────
    def get_kline_last_date(self, code: str) -> Optional[str]:
        """返回本地缓存最新交易日（如 '2026-08-14'），无缓存返回 None。"""
        path = self._kline_path(code)
        if not path.exists():
            return None
        try:
            last_date = None
            with open(path, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    d = row.get("date", "")
                    if d and (last_date is None or d > last_date):
                        last_date = d
            return last_date
        except Exception as e:
            logger.warning("[cache] 读取 kline last_date 失败 %s: %s", code, e)
            return None

    def calc_fetch_range(self, code: str, days: int = 120) -> tuple[Optional[str], Optional[str], str]:
        """
        计算需要拉取的日期范围。

        Returns:
            (start_date, end_date, mode)
            mode: "full" | "incremental" | "up_to_date"
        """
        today_str        = _now_cn().strftime("%Y-%m-%d")
        last_trade_str   = _last_trading_date()
        last             = self.get_kline_last_date(code)

        if last is None:
            start = (_now_cn() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
            return start, today_str, "full"

        # 缓存已覆盖到最近交易日（含今天）→ 无需拉取
        if last >= last_trade_str:
            return None, None, "up_to_date"

        # 增量：从 last + 1 到今天
        last_dt  = datetime.strptime(last, "%Y-%m-%d")
        start_dt = last_dt + timedelta(days=1)
        return start_dt.strftime("%Y-%m-%d"), today_str, "incremental"

    def get_kline(self, code: str):
        """读取本地 K 线缓存，返回 pandas DataFrame 或 None。"""
        path = self._kline_path(code)
        if not path.exists():
            return None
        try:
            import pandas as pd
            df = pd.read_csv(path, dtype={"date": str})
            df = df.sort_values("date").reset_index(drop=True)
            logger.info("[cache] 读取 %s K线缓存: %d 条", code, len(df))
            return df
        except Exception as e:
            logger.warning("[cache] 读取 kline 失败 %s: %s", code, e)
            return None

    def save_kline(self, code: str, df, source: str):
        """首次保存全量 K 线数据。"""
        if df is None or (hasattr(df, "empty") and df.empty):
            return
        try:
            import pandas as pd
            out = df.copy()
            out["fetch_source"] = source
            # 只保留标准列，缺失列填 0
            for col in _KLINE_COLS:
                if col not in out.columns:
                    out[col] = "" if col == "fetch_source" else 0.0
            out = out[_KLINE_COLS].sort_values("date").drop_duplicates("date")
            out.to_csv(self._kline_path(code), index=False, encoding="utf-8")
            logger.info("[cache] 保存 %s K线: %d 条 (来源: %s)", code, len(out), source)
        except Exception as e:
            logger.error("[cache] 保存 kline 失败 %s: %s", code, e)

    def merge_kline(self, code: str, new_df, source: str):
        """将增量新数据合并到已有缓存（去重、排序）。"""
        if new_df is None or (hasattr(new_df, "empty") and new_df.empty):
            logger.info("[cache] 增量数据为空，无需合并 %s", code)
            return
        try:
            import pandas as pd
            existing = self.get_kline(code)
            new_df = new_df.copy()
            new_df["fetch_source"] = source
            for col in _KLINE_COLS:
                if col not in new_df.columns:
                    new_df[col] = "" if col == "fetch_source" else 0.0
            new_df = new_df[_KLINE_COLS]

            if existing is not None and not existing.empty:
                for col in _KLINE_COLS:
                    if col not in existing.columns:
                        existing[col] = "" if col == "fetch_source" else 0.0
                existing = existing[_KLINE_COLS]
                merged = pd.concat([existing, new_df], ignore_index=True)
            else:
                merged = new_df

            # 统一 date 列为字符串（避免 Timestamp vs str 比较报错）
            merged["date"] = merged["date"].astype(str).str.slice(0, 10)
            # 新数据在后，keep="last" 让增量数据覆盖同日旧缓存（修复盘中快照被保留的问题）
            merged = merged.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
            merged.to_csv(self._kline_path(code), index=False, encoding="utf-8")
            logger.info("[cache] 合并 %s K线: 新增 %d 条，合计 %d 条", code, len(new_df), len(merged))
        except Exception as e:
            logger.error("[cache] 合并 kline 失败 %s: %s", code, e)

    # ── 元信息 ────────────────────────────────────────────────
    def get_meta(self, code: str) -> dict:
        path = self._meta_path(code)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_meta(self, code: str, name: str = "", source: str = "",
                  keywords: Optional[list] = None):
        """保存/更新股票元信息。"""
        existing = self.get_meta(code)
        meta = {
            "code":               code,
            "name":               name or existing.get("name", ""),
            "last_date":          self.get_kline_last_date(code) or "",
            "fetch_source":       source or existing.get("fetch_source", ""),
            "updated_at":         _now_cn().isoformat(),
            "commodity_keywords": keywords if keywords is not None
                                  else existing.get("commodity_keywords", []),
        }
        try:
            self._meta_path(code).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("[cache] 保存 meta 失败 %s: %s", code, e)

    def update_commodity_keywords(self, code: str, keywords: list):
        """更新关键词列表（不清空其他 meta 字段）。"""
        meta = self.get_meta(code)
        meta["commodity_keywords"] = keywords
        meta["updated_at"] = _now_cn().isoformat()
        try:
            self._meta_path(code).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("[cache] 更新 commodity_keywords 失败 %s: %s", code, e)

    # ── 商品/产品价格搜索缓存 ─────────────────────────────────
    def get_commodity(self, code: str, keyword: str) -> Optional[list]:
        """
        读取商品关键词缓存。
        返回 snippet 列表，或 None（无缓存 / 已过期）。
        """
        path = self._commodity_path(code, keyword)
        if not path.exists():
            return None
        try:
            ttl = _commodity_ttl()
            rows = []
            with open(path, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
            if not rows:
                return None
            # 检查最后一条记录的时间
            last_ts_str = rows[-1].get("fetched_at", "")
            if last_ts_str:
                try:
                    last_ts = datetime.fromisoformat(last_ts_str)
                    age = (_now_cn() - last_ts).total_seconds()
                    if age > ttl:
                        logger.info("[cache] 商品缓存已过期 %s/%s (age=%.0fs)", code, keyword, age)
                        return None
                except ValueError:
                    pass
            snippets = [r.get("snippet", "") for r in rows if r.get("snippet")]
            return snippets if snippets else None
        except Exception as e:
            logger.warning("[cache] 读取商品缓存失败 %s/%s: %s", code, keyword, e)
            return None

    def save_commodity(self, code: str, keyword: str, snippets: list):
        """保存商品关键词搜索结果到 CSV。"""
        if not snippets:
            return
        path = self._commodity_path(code, keyword)
        try:
            now_str = _now_cn().isoformat()
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["fetched_at", "query", "snippet"])
                writer.writeheader()
                for snippet in snippets:
                    writer.writerow({
                        "fetched_at": now_str,
                        "query":      keyword,
                        "snippet":    str(snippet)[:500],
                    })
            logger.info("[cache] 保存商品缓存 %s/%s: %d 条", code, keyword, len(snippets))
        except Exception as e:
            logger.warning("[cache] 保存商品缓存失败 %s/%s: %s", code, keyword, e)

    # ── 回测缓存 ──────────────────────────────────────────────
    def _backtest_path(self, code: str) -> Path:
        return self._stock_dir(code) / "backtest.json"

    def save_backtest(self, code: str, result: dict):
        """保存回测结果到 backtest.json（每次运行覆盖）。"""
        try:
            data = {"code": code, "updated_at": _now_cn().isoformat(), "result": result}
            self._backtest_path(code).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("[cache] 保存回测结果 %s", code)
        except Exception as e:
            logger.warning("[cache] 保存回测失败 %s: %s", code, e)

    def get_backtest(self, code: str) -> Optional[dict]:
        """读取回测缓存，返回 result 字典，无缓存返回 None。"""
        path = self._backtest_path(code)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("result")
        except Exception as e:
            logger.warning("[cache] 读取回测失败 %s: %s", code, e)
            return None

    # ── 统计 ──────────────────────────────────────────────────
    def list_cached_stocks(self) -> list[dict]:
        """列出所有已缓存的股票及基本信息。"""
        result = []
        if not self.root.exists():
            return result
        for d in sorted(self.root.iterdir()):
            if d.is_dir():
                meta = self.get_meta(d.name)
                kline_path = d / "kline.csv"
                result.append({
                    "code":        d.name,
                    "name":        meta.get("name", ""),
                    "last_date":   meta.get("last_date", ""),
                    "has_kline":   kline_path.exists(),
                    "commodities": [
                        p.stem for p in (d / "commodities").glob("*.csv")
                    ] if (d / "commodities").exists() else [],
                })
        return result
