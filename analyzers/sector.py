"""
portal/analyzers/sector.py — 板块数据层（efinance 东财 BK 口径）

职责：封装 efinance 板块取数 + 本地缓存 + 降级，供 industry.py 调用。
所有板块数据全市场共享，放 portal/data/sectors/（非按股票分目录）。

核心接口：
  get_stock_boards(code)      个股所属全部板块 [{bk,name,pct}]        （TTL 24h）
  get_sector_kline(bk, name)  板块日线 DataFrame（增量缓存）
  get_market_snapshot()       全市场概念板块行情 DataFrame           （TTL 自适应）
  pick_primary_boards(boards) 过滤宽泛/指数类板块，选真正题材概念

设计原则：
  - 纯 efinance，全东财 BK 代码口径，无跨源名称匹配问题
  - 每次网络调用 try/except + sleep 限速，任一失败降级返回空，不抛异常
  - 缓存 CSV + meta.json，复用 data_cache 的 _now_cn / _commodity_ttl / _safe_filename
"""
from __future__ import annotations

import csv
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 复用 data_cache 的时间/TTL/文件名工具（保持行为一致）
try:
    from portal.data_cache import _now_cn, _commodity_ttl, _safe_filename, TZ_CN, _KLINE_COLS
except Exception:  # pragma: no cover - 独立运行兜底
    from datetime import timezone, timedelta
    TZ_CN = timezone(timedelta(hours=8))
    _KLINE_COLS = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "fetch_source"]

    def _now_cn() -> datetime:
        return datetime.now(TZ_CN)

    def _commodity_ttl() -> int:
        now = _now_cn()
        if now.weekday() >= 5:
            return 24 * 3600
        t = now.hour * 60 + now.minute
        return 2 * 3600 if (9 * 60 + 30 <= t <= 15 * 60) else 6 * 3600

    def _safe_filename(keyword: str) -> str:
        import re
        return re.sub(r'[\\/:*?"<>|]', "_", str(keyword))[:60]


_BOARDS_TTL = 24 * 3600     # 个股所属板块：一天更新一次足够

# efinance 每次调用间限速（东财爬虫，防封）
_EF_SLEEP = 0.3

# 单个板块 K 线抓取的总超时（秒）——同花顺历史端点在部分网络下会挂起，
# 用超时保护避免拖死整个深度分析。拿不到 K 线不影响板块归属/涨幅分析。
_KLINE_TIMEOUT = 12


def _call_with_timeout(fn, timeout: int):
    """在独立线程里调用 fn，超时返回 None（不阻塞主分析流程）。"""
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(fn).result(timeout=timeout)
    except FuturesTimeout:
        logger.info("[sector] 调用超时(%ds)，跳过", timeout)
        return None
    except Exception as e:
        logger.info("[sector] 调用异常: %s", str(e)[:60])
        return None

# 宽泛 / 指数类板块黑名单（默认，可被 _blacklist.json 覆盖）
# 这些不是题材概念，对个股分析无参考价值，从"主板块"里剔除
_DEFAULT_BLACKLIST = [
    "融资融券", "深股通", "沪股通", "标准普尔", "富时罗素", "MSCI中国", "MSCI概念",
    "HS300_", "HS300", "上证180_", "上证380", "深证100R", "深成500", "深证100",
    "央视50_", "央视50", "大盘股", "中盘股", "小盘股", "微盘股",
    "股权分散", "破净股", "破增发价股", "预盈预增", "预亏预减", "AB股",
    "AH股", "含B股", "含H股", "参股新股", "国企改革", "举牌",
    "创业成份", "创业板综", "转债标的", "机构重仓", "基金重仓", "QFII重仓",
    "社保重仓", "券商重仓", "北交所概念", "注册制次新股", "次新股", "标普道琼斯A",
]


class SectorData:
    """板块数据管理器（efinance + 本地缓存 + 降级）。"""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.root = cache_dir or Path(__file__).parent.parent / "data" / "sectors"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "boards").mkdir(parents=True, exist_ok=True)
        (self.root / "kline").mkdir(parents=True, exist_ok=True)
        self._ef = None            # 懒加载 efinance
        self._blacklist = None

    # ── efinance 懒加载 ──────────────────────────────────────
    def _efinance(self):
        if self._ef is None:
            try:
                import efinance as ef
                self._ef = ef
            except Exception as e:
                logger.warning("[sector] efinance 未安装/加载失败: %s", e)
                self._ef = False
        return self._ef or None

    # ── 黑名单 ────────────────────────────────────────────────
    def _load_blacklist(self) -> set:
        if self._blacklist is not None:
            return self._blacklist
        path = self.root / "_blacklist.json"
        names = set(_DEFAULT_BLACKLIST)
        if path.exists():
            try:
                extra = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(extra, list):
                    names |= {str(x) for x in extra}
            except Exception:
                pass
        else:
            # 首次落地一份默认黑名单，方便用户编辑
            try:
                path.write_text(json.dumps(_DEFAULT_BLACKLIST, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            except Exception:
                pass
        self._blacklist = names
        return names

    # ── 1. 个股所属板块（TTL 24h）────────────────────────────
    def get_stock_boards(self, code: str) -> list[dict]:
        """
        返回个股所属全部板块 [{bk, name, pct}]。
        pct = 该板块当日涨跌幅（来自 efinance）。
        """
        path = self.root / "boards" / f"{code}.csv"
        cached = self._read_boards_cache(path)
        if cached is not None:
            logger.info("[sector] 板块归属缓存命中 %s: %d 个", code, len(cached))
            return cached

        ef = self._efinance()
        if not ef:
            return cached or []

        try:
            time.sleep(_EF_SLEEP)
            df = ef.stock.get_belong_board(code)
        except Exception as e:
            logger.warning("[sector] get_belong_board 失败 %s: %s", code, e)
            return self._read_boards_cache(path, ignore_ttl=True) or []

        if df is None or df.empty:
            return self._read_boards_cache(path, ignore_ttl=True) or []

        boards = []
        for _, row in df.iterrows():
            bk   = str(row.get("板块代码", "")).strip()
            name = str(row.get("板块名称", "")).strip()
            pct  = row.get("板块涨幅", None)
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                pct = None
            if bk and name:
                boards.append({"bk": bk, "name": name, "pct": pct})

        self._write_boards_cache(path, boards)
        logger.info("[sector] 拉取板块归属 %s: %d 个，写缓存", code, len(boards))
        return boards

    def _read_boards_cache(self, path: Path, ignore_ttl: bool = False) -> Optional[list]:
        if not path.exists():
            return None
        try:
            rows = []
            fetched_at = None
            with open(path, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    fetched_at = row.get("fetched_at") or fetched_at
                    pct = row.get("pct", "")
                    try:
                        pct = float(pct)
                    except (TypeError, ValueError):
                        pct = None
                    rows.append({"bk": row.get("bk", ""), "name": row.get("name", ""), "pct": pct})
            if not rows:
                return None
            if not ignore_ttl and fetched_at:
                try:
                    age = (_now_cn() - datetime.fromisoformat(fetched_at)).total_seconds()
                    if age > _BOARDS_TTL:
                        return None
                except ValueError:
                    pass
            return rows
        except Exception as e:
            logger.warning("[sector] 读板块缓存失败 %s: %s", path.name, e)
            return None

    def _write_boards_cache(self, path: Path, boards: list):
        try:
            now = _now_cn().isoformat()
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["fetched_at", "bk", "name", "pct"])
                w.writeheader()
                for b in boards:
                    w.writerow({"fetched_at": now, "bk": b["bk"], "name": b["name"],
                                "pct": "" if b["pct"] is None else b["pct"]})
        except Exception as e:
            logger.warning("[sector] 写板块缓存失败: %s", e)

    # ── 2. 板块日线 K 线（增量缓存 + 双源降级）──────────────
    def get_sector_kline(self, bk: str, name: str = "", days: int = 120):
        """
        返回板块日线 DataFrame（date/open/close/.../pct_chg），带增量缓存。

        双源策略（东财板块历史端点 push2his 常被内网拦截）：
          1. 优先 efinance BK 代码（东财 push2his）
          2. 失败降级 akshare 同花顺 stock_board_concept_index_ths(name)（10jqka，稳定）
        以 bk 作为缓存键（跨源统一）。
        """
        try:
            import pandas as pd
        except Exception:
            return None

        path = self.root / "kline" / f"{_safe_filename(bk)}.csv"
        today = _now_cn().strftime("%Y-%m-%d")

        existing = self._read_kline_csv(path)
        last_date = existing["date"].max() if existing is not None and not existing.empty else None

        if last_date is not None and last_date >= today:
            logger.info("[sector] 板块K线缓存最新 %s: %d 条", bk, len(existing))
            return existing

        norm = _call_with_timeout(lambda: self._fetch_kline_efinance(bk, last_date), _KLINE_TIMEOUT)
        if norm is None or (hasattr(norm, "empty") and norm.empty):
            norm = _call_with_timeout(lambda: self._fetch_kline_ths(name, last_date), _KLINE_TIMEOUT)

        if norm is None or norm.empty:
            return existing

        merged = self._merge_kline(existing, norm)
        self._write_kline_csv(path, merged)
        if last_date is None and len(merged) > days:
            return merged.tail(days).reset_index(drop=True)
        return merged

    def _fetch_kline_efinance(self, bk: str, last_date):
        """efinance 板块 K 线（东财 push2his，可能被拦截）。"""
        ef = self._efinance()
        if not ef:
            return None
        beg = "19000101"
        if last_date:
            try:
                beg = datetime.strptime(last_date, "%Y-%m-%d").strftime("%Y%m%d")
            except ValueError:
                pass
        try:
            time.sleep(_EF_SLEEP)
            df = ef.stock.get_quote_history(bk, beg=beg, klt=101)
            if df is not None and not df.empty:
                return self._normalize_ef_kline(df)
        except Exception as e:
            logger.info("[sector] efinance板块K线失败 %s（降级同花顺）: %s", bk, str(e)[:60])
        return None

    def _fetch_kline_ths(self, name: str, last_date):
        """akshare 同花顺板块指数 K 线（10jqka，稳定降级源）。"""
        if not name:
            return None
        try:
            import akshare as ak
            import pandas as pd
        except Exception:
            return None
        beg = "20200101"
        if last_date:
            try:
                beg = datetime.strptime(last_date, "%Y-%m-%d").strftime("%Y%m%d")
            except ValueError:
                pass
        end = _now_cn().strftime("%Y%m%d")
        try:
            time.sleep(_EF_SLEEP)
            df = ak.stock_board_concept_index_ths(symbol=name, start_date=beg, end_date=end)
            if df is None or df.empty:
                return None
            col_map = {"日期": "date", "开盘价": "open", "收盘价": "close",
                       "最高价": "high", "最低价": "low", "成交量": "volume", "成交额": "amount"}
            out = df.rename(columns=col_map)
            keep = [c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in out.columns]
            out = out[keep].copy()
            out["date"] = out["date"].astype(str)
            # 同花顺不返回 pct_chg，用收盘价现算
            if "close" in out.columns:
                out["pct_chg"] = (out["close"].astype(float).pct_change() * 100).round(2)
            out["fetch_source"] = "ths"
            logger.info("[sector] 同花顺板块K线 %s: %d 条", name, len(out))
            return out
        except Exception as e:
            logger.info("[sector] 同花顺板块K线失败 %s: %s", name, str(e)[:60])
            return None

    @staticmethod
    def _normalize_ef_kline(df):
        """efinance K线列名 → 标准列 date/open/high/low/close/volume/amount/pct_chg。"""
        import pandas as pd
        col_map = {
            "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
            "最低": "low", "成交量": "volume", "成交额": "amount", "涨跌幅": "pct_chg",
        }
        out = df.rename(columns=col_map)
        keep = [c for c in ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"] if c in out.columns]
        out = out[keep].copy()
        out["date"] = out["date"].astype(str)
        out["fetch_source"] = "efinance"
        return out

    def _read_kline_csv(self, path: Path):
        if not path.exists():
            return None
        try:
            import pandas as pd
            df = pd.read_csv(path, dtype={"date": str})
            return df.sort_values("date").reset_index(drop=True)
        except Exception:
            return None

    def _merge_kline(self, existing, new_df):
        import pandas as pd
        if existing is None or existing.empty:
            merged = new_df
        else:
            merged = pd.concat([existing, new_df], ignore_index=True)
        return merged.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)

    def _write_kline_csv(self, path: Path, df):
        try:
            df.to_csv(path, index=False, encoding="utf-8")
        except Exception as e:
            logger.warning("[sector] 写板块K线失败 %s: %s", path.name, e)

    # ── 3. 全市场板块行情快照（TTL 自适应）──────────────────
    def get_market_snapshot(self):
        """
        全市场概念板块实时行情 DataFrame。
        列（标准化）：bk, name, pct_chg, price, turnover, volume_ratio, total_mv
        """
        path = self.root / "concept_snapshot.csv"
        cached = self._read_snapshot_cache(path)
        if cached is not None:
            logger.info("[sector] 板块快照缓存命中: %d 个", len(cached))
            return cached

        ef = self._efinance()
        if not ef:
            return self._read_snapshot_cache(path, ignore_ttl=True)

        try:
            snap_raw = _call_with_timeout(
                lambda: ef.stock.get_realtime_quotes(["概念板块"]), _KLINE_TIMEOUT)
            df = snap_raw
        except Exception as e:
            logger.warning("[sector] 板块快照拉取失败: %s", e)
            return self._read_snapshot_cache(path, ignore_ttl=True)

        if df is None or df.empty:
            return self._read_snapshot_cache(path, ignore_ttl=True)

        snap = self._normalize_snapshot(df)
        self._write_snapshot_cache(path, snap)
        logger.info("[sector] 拉取板块快照: %d 个，写缓存", len(snap))
        return snap

    @staticmethod
    def _normalize_snapshot(df):
        import pandas as pd
        col_map = {
            "股票代码": "bk", "股票名称": "name", "涨跌幅": "pct_chg", "最新价": "price",
            "换手率": "turnover", "量比": "volume_ratio", "总市值": "total_mv",
            "流通市值": "float_mv", "成交额": "amount",
        }
        out = df.rename(columns=col_map)
        keep = [c for c in ["bk", "name", "pct_chg", "price", "turnover", "volume_ratio", "total_mv", "float_mv", "amount"] if c in out.columns]
        out = out[keep].copy()
        for c in ["pct_chg", "price", "turnover", "volume_ratio", "total_mv", "float_mv", "amount"]:
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        return out

    def _read_snapshot_cache(self, path: Path, ignore_ttl: bool = False):
        if not path.exists():
            return None
        try:
            import pandas as pd
            df = pd.read_csv(path)
            if df.empty:
                return None
            if not ignore_ttl and "_fetched_at" in df.columns:
                fetched = str(df["_fetched_at"].iloc[0])
                try:
                    age = (_now_cn() - datetime.fromisoformat(fetched)).total_seconds()
                    if age > _commodity_ttl():
                        return None
                except ValueError:
                    pass
            return df.drop(columns=[c for c in ["_fetched_at"] if c in df.columns])
        except Exception:
            return None

    def _write_snapshot_cache(self, path: Path, df):
        try:
            out = df.copy()
            out["_fetched_at"] = _now_cn().isoformat()
            out.to_csv(path, index=False, encoding="utf-8")
        except Exception as e:
            logger.warning("[sector] 写板块快照失败: %s", e)

    # ── 4. 筛选主题材板块（剔除宽泛/指数类）──────────────────
    # ── 题材概念集（同花顺概念全集，用于识别"题材 vs 行业"）──
    def _concept_universe(self) -> set:
        """
        返回同花顺概念板块名全集，缓存到 sectors/concept_universe.csv（TTL 7天）。
        用于在 pick_primary_boards 里区分"题材概念"（可拿板块K线）与"行业分类"。
        拉取失败返回空集（不影响主流程，只是排序退化）。
        """
        path = self.root / "concept_universe.csv"
        # 读缓存（TTL 7 天）
        if path.exists():
            try:
                import pandas as pd
                df = pd.read_csv(path)
                if not df.empty and "_fetched_at" in df.columns:
                    fetched = str(df["_fetched_at"].iloc[0])
                    age = (_now_cn() - datetime.fromisoformat(fetched)).total_seconds()
                    if age <= 7 * 24 * 3600:
                        return set(df["name"].astype(str).tolist())
            except Exception:
                pass
        # 拉取
        try:
            import akshare as ak
            import pandas as pd
            time.sleep(_EF_SLEEP)
            df = ak.stock_board_concept_name_ths()
            names = set(df["name"].astype(str).tolist())
            out = pd.DataFrame({"name": list(names)})
            out["_fetched_at"] = _now_cn().isoformat()
            out.to_csv(path, index=False, encoding="utf-8")
            logger.info("[sector] 同花顺概念全集: %d 个，写缓存", len(names))
            return names
        except Exception as e:
            logger.info("[sector] 概念全集拉取失败: %s", str(e)[:60])
            return set()

    # 风格/宽基后缀（非题材，剔除）
    _STYLE_KW = ("成份", "成分", "指数", "板块指数", "风格", "大盘成长", "大盘价值",
                 "中盘成长", "中盘价值", "小盘成长", "小盘价值", "地板块", "板块")

    def pick_primary_boards(self, boards: list[dict], top: int = 6) -> list[dict]:
        """
        从个股所属板块里剔除黑名单（指数/风格/宽基类），保留题材概念 + 行业。
        **题材概念优先**：能在同花顺概念全集里匹配到的（可拿板块K线、更有分析价值）
        排在行业分类前面；同组内保留 efinance 原始顺序（越靠前越贴近个股定位）。
        """
        bl = self._load_blacklist()
        filtered = []
        for b in boards:
            name = b["name"]
            if name in bl:
                continue
            if name.endswith(("_", "R")):        # HS300_ / 深证100R 之类
                continue
            if any(kw in name for kw in self._STYLE_KW):
                continue
            filtered.append(b)

        universe = self._concept_universe()
        if universe:
            themed  = [b for b in filtered if b["name"] in universe]
            others  = [b for b in filtered if b["name"] not in universe]
            ordered = themed + others          # 题材优先，稳定保序
        else:
            ordered = filtered
        return ordered[:top] if ordered else boards[:top]
