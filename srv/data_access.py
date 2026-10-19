"""
portal/srv/data_access.py
数据访问层 —— .env 加载 / K线获取（带缓存）/ 技术维度重算 / JSON 序列化兜底。

从原 server.py 的 _json_default / _recompute_tech_dimension / _load_dotenv /
_fetch_kline 逐字节搬迁。依赖 _config 的路径常量与 logger。
"""
from __future__ import annotations

import os

from ._config import logger, LIB_DIR, PROJECT_ROOT


def _json_default(o):
    """json.dumps 的兜底：把 numpy 标量（bool_/int64/float64）等转成原生类型。"""
    # numpy 标量都实现了 .item()
    if hasattr(o, "item"):
        try:
            return o.item()
        except Exception:
            pass
    # pandas/numpy 布尔、其他可布尔化对象
    if isinstance(o, (set,)):
        return list(o)
    try:
        import numpy as np
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
    except Exception:
        pass
    return str(o)


def _recompute_tech_dimension(tech_result, llm_call, log=None):
    """LLM 打分回写各 Section 后，重算技术面维度综合分与综合信号。

    - 维度综合分：各指标 LLM 分的均值（排除占位 50）。
    - 维度综合信号：交给 LLM 基于全部指标做一次总结性判断（零硬编码）。
      LLM 不可用/失败时降级为 hold（不做 score→signal 硬编码推导）。
    """
    def _log(m):
        if log: log(m)

    sections = [s for s in tech_result.sections if s is not None]
    scored = [s for s in sections if s.score != 50]
    tech_result.score = int(sum(s.score for s in scored) / len(scored)) if scored else 50

    # 维度综合信号交给 LLM
    tech_result.signal = "hold"
    if not llm_call or not sections:
        return
    try:
        import json as _json
        brief = "；".join(
            f"{s.title}(评分{s.score},{s.signal})" for s in sections
        )
        prompt = (
            f"你是A股技术分析师。下面是某股票技术面各指标的 LLM 评分与信号汇总：\n{brief}\n\n"
            f"技术面综合评分为 {tech_result.score}/100。请你综合全部指标，给出技术面维度的**综合信号**。\n"
            f"只返回严格 JSON（不要解释、不要markdown围栏）："
            f'{{"signal":"buy或watch或hold或sell"}}'
        )
        resp = (llm_call(prompt) or "").strip()
        obj = _json.loads(resp[resp.find("{"): resp.rfind("}") + 1]) if "{" in resp else {}
        sig = str(obj.get("signal", "hold")).strip().lower()
        if sig not in ("buy", "watch", "hold", "sell"):
            cn = {"买入": "buy", "关注": "watch", "观望": "watch",
                  "持有": "hold", "减仓": "hold", "卖出": "sell"}
            sig = cn.get(str(obj.get("signal", "")).strip(), "hold")
        tech_result.signal = sig
        _log(f"🧭 技术面维度综合信号（LLM）：{sig}")
    except Exception as e:
        logger.warning("技术面维度综合信号 LLM 判断失败: %s", e)


def _load_dotenv():
    """读取配置文件目录 .env，注入环境变量（不覆盖已有值）。"""
    # 优先 portal/lib/.env，其次项目根 .env
    for env_file in [LIB_DIR / ".env", PROJECT_ROOT / ".env"]:
        if not env_file.exists():
            continue
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as e:
            logger.warning("load_dotenv %s error: %s", env_file, e)
        break


def _fetch_kline(stock_code: str, log) -> object:
    """
    获取股票 K 线数据（带本地增量缓存）。

    流程：
      1. 检查 portal/data/stocks/{code}/kline.csv 是否存在
      2. 计算需要拉取的日期范围（full / incremental / up_to_date）
      3. 拉取新数据（失败自动重试最多 10 次）→ 合并到缓存 → 返回完整 DataFrame
      4. 全部重试失败时抛出异常，不降级使用过期缓存
    """
    import time as _time

    from portal.data_cache import StockDataCache
    cache = StockDataCache()

    start, end, mode = cache.calc_fetch_range(stock_code, days=250)

    if mode == "up_to_date":
        df = cache.get_kline(stock_code)
        if df is not None and not df.empty:
            log(f"📦 使用本地缓存（已是最新，{len(df)} 条）")
            return df
        log("⚠️  本地缓存读取失败，尝试网络拉取")
        mode = "full"
        start = None
        end   = None

    # 网络拉取：只用内网可用的两个数据源，volume 单位统一为"股（shares）"
    # AkshareFetcher: ak.stock_zh_a_hist() 直接返回股，_normalize_data 无乘法
    # BaostockFetcher: query_history_k_data_plus() 直接返回股，pd.to_numeric() 无乘法
    from data_provider import DataFetcherManager
    from data_provider.baostock_fetcher import BaostockFetcher

    MAX_RETRIES = 10
    RETRY_DELAY = 0.5  # 秒，每次失败后等待

    def _fetch_with_retry(fetch_fn, desc: str):
        """执行 fetch_fn()，失败后最多重试 MAX_RETRIES 次，全部失败则抛出异常。"""
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                mgr = DataFetcherManager(fetchers=[BaostockFetcher()])
                result = fetch_fn(mgr)
                df_r, src = result if isinstance(result, tuple) else (result, "unknown")
                if df_r is not None and not df_r.empty:
                    return df_r, src
                last_err = "返回空数据"
            except Exception as e:
                last_err = str(e)
            log(f"⚠️  {desc} 第{attempt}/{MAX_RETRIES}次失败：{last_err}"
                + (f"，{RETRY_DELAY}s 后重试…" if attempt < MAX_RETRIES else ""))
            if attempt < MAX_RETRIES:
                _time.sleep(RETRY_DELAY)
        raise RuntimeError(f"{stock_code} K线拉取失败（{MAX_RETRIES}次均失败，最后错误：{last_err}）")

    if mode == "incremental":
        log(f"📥 增量拉取 {start} ~ {end}")
        try:
            mgr = DataFetcherManager(fetchers=[BaostockFetcher()])
            result = mgr.get_daily_data(stock_code, start_date=start, end_date=end)
            new_df, source_name = result if isinstance(result, tuple) else (result, "unknown")
        except Exception as e:
            new_df, source_name = None, "unknown"
            log(f"⚠️  增量拉取失败（视为无新数据）：{e}")

        if new_df is not None and not new_df.empty:
            cache.merge_kline(stock_code, new_df, source_name)
            log(f"✅ 增量更新 {len(new_df)} 条，写入缓存")
        else:
            log("ℹ️  无新交易数据（非交易日或今日未收盘），使用现有缓存")

        df = cache.get_kline(stock_code)
        if df is not None and not df.empty:
            return df
        raise RuntimeError(f"{stock_code} 增量合并后缓存读取失败")

    else:  # full
        log(f"🌐 首次全量拉取（最近 250 日）")
        df, source_name = _fetch_with_retry(
            lambda mgr: mgr.get_daily_data(stock_code, days=250),
            "全量拉取"
        )
        cache.save_kline(stock_code, df, source_name)
        log(f"✅ 获取 {len(df)} 条K线数据，已写入缓存")
        return df
