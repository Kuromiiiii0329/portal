"""
portal/analyzers/market.py — 轻量大盘/指数分析器

分析上证指数(sh000001) + 创业板指(sz399006)：
  - ak.stock_zh_index_daily 拉指数历史日线（8000+条，含 MA250 年线所需）
  - 复用 TechnicalAnalyzer 的指标计算与各子模块（均线/MACD/RSI/KDJ/布林/超买超卖/背离/量价）
  - 额外补算 MA250 年线（牛熊分界）
  - 不判断交易日，以最新交易日数据为主
  - 不含 turnover/margin/chip（指数无 amount/换手率数据，属个股概念）
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

INDICES = [
    {"symbol": "sh000001", "name": "上证指数"},
    {"symbol": "sz399006", "name": "创业板指"},
]

# 大盘用的技术子模块（排除个股专属的 turnover/margin/chip）
MARKET_SECTIONS = ["ma_system", "ma250", "macd", "rsi", "kdj",
                   "bollinger", "overbought", "divergence", "volume"]


class MarketAnalyzer:
    """大盘指数分析器。"""

    def analyze_all(self, llm_call=None, log=None) -> list:
        def _log(m):
            if log: log(m)
        results = []
        for idx in INDICES:
            _log(f"📊 分析 {idx['name']}（{idx['symbol']}）...")
            try:
                r = self.analyze_index(idx["symbol"], idx["name"], llm_call, log)
                if r:
                    results.append(r)
                    _log(f"✅ {idx['name']} 完成，评分 {r['score']}，信号 {r['signal']}")
            except Exception as e:
                logger.exception("大盘指数分析失败 %s: %s", idx["symbol"], e)
                _log(f"❌ {idx['name']} 分析失败：{e}")
        return results

    def build_overall_summary(self, results: list, llm_call=None, log=None) -> str:
        """综合两个指数生成大盘整体研判（一段话）。无 LLM 时规则拼接。"""
        if not results:
            return "未获取到指数数据，无法研判。"

        # 组装各指数关键指标明细
        blocks = []
        for r in results:
            key_secs = []
            for s in r.get("sections", []):
                if s.get("key") in ("ma_system", "ma250", "macd", "rsi", "overbought", "divergence"):
                    first = (s.get("content") or "").split("\n")[0].replace("**", "")
                    key_secs.append(f"{s.get('title')}：{first}")
            blocks.append(f"【{r['name']}】评分{r['score']}/信号{r['signal']}\n" + "\n".join(key_secs))
        detail = "\n\n".join(blocks)

        if llm_call:
            prompt = f"""你是A股市场策略分析师。请基于上证指数和创业板指的技术指标，给出一段客观的大盘整体研判。

{detail}

请输出（150-220字，自然段落，关键处加粗）：
1. **整体基调**：一句话定性当前大盘（强势/偏多/震荡/偏空/弱势），引用两指数的关键指标数值。
2. **风格研判**：主板 vs 创业板谁更强，市场偏大盘蓝筹还是成长题材。
3. **操作建议**：仓位建议（重仓/半仓/轻仓/观望）+ 需要观察的关键点位或信号。
4. **风险提示**：一句话点明最需警惕的风险（如指数背离、跌破年线等）。
直接输出，不要标题不要分点编号。"""
            try:
                return llm_call(prompt).strip()
            except Exception as e:
                if log: log(f"⚠️ 大盘整体研判 LLM 失败：{e}")

        # 规则降级
        parts = [f"{r['name']}（{r['score']}/{r['signal']}）：{r.get('summary','')}" for r in results]
        avg = int(sum(r["score"] for r in results) / len(results))
        tone = "偏多" if avg >= 60 else "偏空" if avg <= 40 else "震荡"
        return f"大盘整体{tone}（均分{avg}）。" + "；".join(parts) + "。"

    def analyze_index(self, symbol: str, name: str, llm_call=None, log=None) -> Optional[dict]:
        import pandas as pd
        df = self._fetch_index_kline(symbol, log)
        if df is None or df.empty or len(df) < 30:
            if log: log(f"⚠️ {name} K线数据不足")
            return None

        from portal.analyzers.technical import TechnicalAnalyzer
        tech = TechnicalAnalyzer()
        df = df.sort_values("date").reset_index(drop=True)
        df = tech._compute_indicators(df)      # MA5/10/20/60 + macd/rsi/kdj/boll/wr/vol_ratio
        df = self._add_ma250(df)               # 年线

        # 逐子模块产出 Section（直接调实例方法，无副作用）
        section_objs = []
        for key in MARKET_SECTIONS:
            try:
                if key == "ma_system":   section_objs.append(tech._analyze_ma(df, symbol))
                elif key == "ma250":     section_objs.append(self._analyze_ma250(df, tech))
                elif key == "macd":      section_objs.append(tech._analyze_macd(df))
                elif key == "rsi":       section_objs.append(tech._analyze_rsi(df))
                elif key == "kdj":       section_objs.append(tech._analyze_kdj(df))
                elif key == "bollinger": section_objs.append(tech._analyze_bollinger(df))
                elif key == "overbought":section_objs.append(tech._analyze_overbought(df))
                elif key == "divergence":section_objs.append(tech._analyze_divergence(df))
                elif key == "volume":    section_objs.append(tech._analyze_volume(df))
            except Exception as e:
                logger.warning("大盘子模块 %s 失败 %s: %s", key, symbol, e)

        # Section dataclass → dict
        sections = [{"key": s.key, "title": s.title, "content": s.content,
                     "data": s.data, "signal": s.signal, "score": s.score}
                    for s in section_objs]

        # 综合评分：各 section 加权平均
        scored = [s for s in sections if s["score"] != 50]
        score = int(sum(s["score"] for s in scored) / len(scored)) if scored else 50
        signal = tech._score_to_signal(score)

        # 摘要
        ma_sec = next((s for s in sections if s["key"] == "ma250"), None) \
            or next((s for s in sections if s["key"] == "ma_system"), None)
        summary = ma_sec["content"].split("\n")[0].replace("**", "") if ma_sec else f"评分 {score}/100"

        # kline_data（含年线，供大盘蜡烛图）
        kline = self._build_kline_data(df)

        return {"symbol": symbol, "name": name, "score": score, "signal": signal,
                "summary": summary, "sections": sections, "kline_data": kline}

    # ── 指数 K 线获取 ─────────────────────────────────────────
    @staticmethod
    def _fetch_index_kline(symbol: str, log=None):
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol=symbol)  # date/open/high/low/close/volume
            if df is None or df.empty:
                return None
            df = df.copy()
            df["date"] = df["date"].astype(str)
            if log: log(f"📈 {symbol} 获取 {len(df)} 条指数日线，最新 {df.iloc[-1]['date']}")
            return df
        except Exception as e:
            logger.warning("获取指数K线失败 %s: %s", symbol, e)
            if log: log(f"⚠️ 指数K线获取失败：{e}")
            return None

    @staticmethod
    def _add_ma250(df):
        df["ma250"] = df["close"].rolling(250).mean()
        return df

    def _analyze_ma250(self, df, tech):
        """年线（MA250）多空分析：牛熊分界。"""
        from portal.analyzers.base import Section
        import pandas as pd
        last = df.iloc[-1]
        close = last["close"]
        ma250 = last.get("ma250")
        if ma250 is None or pd.isna(ma250):
            return Section(key="ma250", title="年线（MA250）",
                           content="数据不足250日，无法计算年线", score=50, signal="hold")
        bias = (close - ma250) / ma250 * 100
        # 年线斜率（近20日）
        slope = 0.0
        if len(df) >= 270 and not pd.isna(df.iloc[-21].get("ma250")):
            prev = df.iloc[-21]["ma250"]
            slope = (ma250 - prev) / prev * 100
        above = close > ma250
        if above and slope > 0:
            status, score = "站上年线且年线上行（牛市格局）", 72
        elif above:
            status, score = "站上年线但年线走平/下行（偏多但需确认）", 60
        elif slope < 0:
            status, score = "跌破年线且年线下行（熊市格局）", 28
        else:
            status, score = "跌破年线但年线走平（弱势整理）", 42
        content = (
            f"**年线 MA250={ma250:.2f}**\n"
            f"- 当前价 {close:.2f}，{'高于' if above else '低于'}年线 {bias:+.1f}%\n"
            f"- 年线斜率（近20日）：{slope:+.2f}%（{'上行' if slope>0 else '下行' if slope<0 else '走平'}）\n"
            f"- 状态：{status}\n"
        )
        return Section(key="ma250", title="年线（MA250）", content=content,
                       data={"ma250": float(ma250), "bias": float(bias), "slope": float(slope)},
                       score=score, signal=tech._score_to_signal(score))

    @staticmethod
    def _build_kline_data(df, tail=200):
        import pandas as pd
        keep = ["date", "open", "high", "low", "close", "ma5", "ma10", "ma20", "ma30", "ma60", "ma120", "ma250"]
        cols = [c for c in keep if c in df.columns]
        sub = df[cols].tail(tail)
        records = []
        for row in sub.to_dict(orient="records"):
            rec = {}
            for k, v in row.items():
                if v is None or (isinstance(v, float) and v != v):  # NaN
                    rec[k] = None
                elif k == "date":
                    rec[k] = str(v)
                else:
                    try: rec[k] = round(float(v), 2)
                    except Exception: rec[k] = None
            records.append(rec)
        return records
