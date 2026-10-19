"""
portal/analyzers/fundamental.py
基本面分析器

复用 data_provider/fundamental_adapter.py（财报/分红）
和 data_provider 实时行情（PE/PB/市值）。
子模块：financials / growth / dividend / capital_flow / valuation / business
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path

# path setup handled by server.py startup

from .base import BaseAnalyzer, DimensionResult, Section

logger = logging.getLogger(__name__)


class FundamentalAnalyzer(BaseAnalyzer):
    name = "基本面"
    dimension = "fundamental"
    description = "财报/成长/分红/主力资金/估值/主营业务"

    MODULES = {
        "financials":    "核心财报（营收/净利/ROE/现金流）",
        "growth":        "成长能力（YoY增速）",
        "dividend":      "分红质量（股息率）",
        "capital_flow":  "主力资金流向",
        "valuation":     "估值水平（PE/PB/市值）",
        "business":      "主营业务（LLM + 搜索）",
        "northbound":    "北向资金（陆股通）",
        "holder_change": "大股东增减持",
    }
    DEFAULT_MODULES = ["financials", "growth", "valuation", "capital_flow"]

    def analyze(self, stock_code, stock_name, df, modules, llm_call, search):
        result = DimensionResult(dimension=self.dimension, name=self.name)
        sections = []

        # ── 获取基本面数据 ───────────────────────────────────
        fundamental_bundle = {}
        realtime_data = {}
        try:
            from data_provider.fundamental_adapter import AkshareFundamentalAdapter
            adapter = AkshareFundamentalAdapter()
            fundamental_bundle = adapter.get_fundamental_bundle(stock_code) or {}
        except Exception as e:
            logger.warning("fundamental_adapter error for %s: %s", stock_code, e)

        try:
            from data_provider import DataFetcherManager
            from src.config import get_config
            config = get_config()
            mgr = DataFetcherManager(config)
            realtime_data = mgr.get_realtime_quote(stock_code) or {}
        except Exception as e:
            logger.warning("realtime quote error for %s: %s", stock_code, e)

        # ── 各子模块分析 ─────────────────────────────────────
        try:
            earnings = fundamental_bundle.get("earnings", {})
            fin_report = earnings.get("financial_report", {})
            growth = fundamental_bundle.get("growth", {})

            if "financials" in modules:
                sections.append(self._analyze_financials(fin_report, growth))
            if "growth" in modules:
                sections.append(self._analyze_growth(growth))
            if "dividend" in modules:
                sections.append(self._analyze_dividend(earnings.get("dividend", {})))
            if "capital_flow" in modules:
                sections.append(self._analyze_capital_flow(fundamental_bundle))
            if "valuation" in modules:
                sections.append(self._analyze_valuation(realtime_data))
            if "business" in modules and llm_call:
                sections.append(self._analyze_business(stock_code, stock_name, llm_call, search))
            if "northbound" in modules:
                sections.append(self._analyze_northbound(stock_code))
            if "holder_change" in modules:
                sections.append(self._analyze_holder_change(stock_code))

        except Exception as e:
            logger.exception("FundamentalAnalyzer section error for %s: %s", stock_code, e)
            result.error = str(e)

        result.sections = sections
        scored = [s for s in sections if s.score != 50]
        result.score = int(sum(s.score for s in scored) / len(scored)) if scored else 50
        result.signal = self._score_to_signal(result.score)

        fin_sec = next((s for s in sections if s.key == "financials"), None)
        val_sec = next((s for s in sections if s.key == "valuation"), None)
        summary_parts = []
        if fin_sec:
            summary_parts.append(fin_sec.content.split("\n")[0])
        if val_sec:
            summary_parts.append(val_sec.content.split("\n")[0])
        result.summary = "；".join(summary_parts) or f"基本面评分 {result.score}/100"

        return result

    # ── 子模块 ───────────────────────────────────────────────
    def _analyze_financials(self, fin_report: dict, growth: dict) -> Section:
        if not fin_report:
            return Section(key="financials", title="核心财报",
                           content="暂无财报数据", score=50, signal="hold")

        rev     = fin_report.get("revenue")
        profit  = fin_report.get("net_profit_parent")
        ocf     = fin_report.get("operating_cash_flow")
        roe     = fin_report.get("roe") or growth.get("roe")
        period  = fin_report.get("report_date", "")
        margin  = growth.get("gross_margin")

        lines = [f"**报告期：{period}**"]
        score = 50

        if rev:
            lines.append(f"- 营业收入：{self._fmt_money(rev)}")
        if profit:
            lines.append(f"- 归母净利润：{self._fmt_money(profit)}")
            if rev and rev != 0:
                net_margin = profit / rev * 100
                lines.append(f"- 净利率：{net_margin:.1f}%")
        if ocf:
            lines.append(f"- 经营现金流：{self._fmt_money(ocf)}")
            # 现金流质量：经营现金流 > 净利润 为好信号
            if profit and ocf > profit:
                score = min(score + 8, 80)
                lines.append("  ✅ 经营现金流 > 净利润（盈利质量好）")
        if roe:
            lines.append(f"- ROE：{roe:.1f}%")
            if roe > 15:
                score = min(score + 10, 85)
            elif roe < 5:
                score = max(score - 10, 20)
        if margin:
            lines.append(f"- 毛利率：{margin:.1f}%")

        content = "\n".join(lines)
        return Section(key="financials", title="核心财报", content=content,
                       data=fin_report, score=score, signal=self._score_to_signal(score))

    def _analyze_growth(self, growth: dict) -> Section:
        if not growth:
            return Section(key="growth", title="成长能力",
                           content="暂无增长数据", score=50, signal="hold")

        rev_yoy    = growth.get("revenue_yoy")
        profit_yoy = growth.get("net_profit_yoy")

        score = 50
        lines = ["**同比增速（YoY）**"]
        if rev_yoy is not None:
            lines.append(f"- 营收同比：{rev_yoy:+.1f}%")
            if rev_yoy > 20:   score = min(score + 12, 85)
            elif rev_yoy > 10: score = min(score + 6, 75)
            elif rev_yoy < 0:  score = max(score - 10, 25)
        if profit_yoy is not None:
            lines.append(f"- 净利润同比：{profit_yoy:+.1f}%")
            if profit_yoy > 20:   score = min(score + 12, 88)
            elif profit_yoy > 10: score = min(score + 6, 78)
            elif profit_yoy < 0:  score = max(score - 10, 22)

        if score >= 70:
            lines.append("✅ 高成长，业绩驱动力强")
        elif score < 40:
            lines.append("⚠️ 增长承压，需关注基本面改善信号")

        content = "\n".join(lines)
        return Section(key="growth", title="成长能力", content=content,
                       data=growth, score=score, signal=self._score_to_signal(score))

    def _analyze_dividend(self, dividend: dict) -> Section:
        if not dividend:
            return Section(key="dividend", title="分红质量",
                           content="暂无分红数据", score=50, signal="hold")

        ttm_yield  = dividend.get("ttm_dividend_yield_pct")
        ttm_dps    = dividend.get("ttm_cash_dividend_per_share")
        ttm_count  = dividend.get("ttm_event_count", 0)

        score = 50
        lines = ["**近12个月分红情况**"]
        if ttm_dps:
            lines.append(f"- TTM每股现金分红：{ttm_dps:.4f} 元")
        if ttm_yield:
            lines.append(f"- TTM股息率：{ttm_yield:.2f}%")
            if ttm_yield > 4:   score = 72
            elif ttm_yield > 2: score = 60
            elif ttm_yield > 0: score = 52
        lines.append(f"- 近12月分红次数：{ttm_count}")
        if ttm_count == 0:
            lines.append("  ⚠️ 近期无分红")
            score = max(score - 5, 45)

        content = "\n".join(lines)
        return Section(key="dividend", title="分红质量", content=content,
                       data=dividend, score=score, signal=self._score_to_signal(score))

    def _analyze_capital_flow(self, bundle: dict) -> Section:
        cf = bundle.get("capital_flow", {})
        stock_flow = cf.get("stock_flow", {})
        if not stock_flow:
            return Section(key="capital_flow", title="主力资金",
                           content="暂无资金流向数据", score=50, signal="hold")

        main_in  = stock_flow.get("main_net_inflow", 0) or 0
        in_5d    = stock_flow.get("inflow_5d", 0) or 0
        in_10d   = stock_flow.get("inflow_10d", 0) or 0

        score = 50
        if main_in > 0:   score = min(score + 10, 72)
        elif main_in < 0: score = max(score - 10, 30)
        if in_5d > 0 and in_10d > 0:
            score = min(score + 8, 80)

        main_str = f"{main_in/1e8:+.2f}亿" if abs(main_in) > 1e6 else f"{main_in/1e4:+.2f}万"
        lines = [
            f"**主力净流入（今日）：{main_str}**",
            f"- 5日净流入：{in_5d/1e8:+.2f}亿" if abs(in_5d) > 1e6 else f"- 5日净流入：{in_5d/1e4:+.2f}万",
            f"- 10日净流入：{in_10d/1e8:+.2f}亿" if abs(in_10d) > 1e6 else f"- 10日净流入：{in_10d/1e4:+.2f}万",
        ]
        if main_in > 0:
            lines.append("✅ 主力持续流入，多头力量占优")
        else:
            lines.append("⚠️ 主力净流出，注意风险")

        # 板块资金
        rankings = cf.get("sector_rankings", {})
        top = rankings.get("top", [])[:3]
        if top:
            lines.append(f"- 资金流入板块前三：{', '.join(t.get('name','') for t in top)}")

        content = "\n".join(lines)
        return Section(key="capital_flow", title="主力资金", content=content,
                       data=stock_flow, score=score, signal=self._score_to_signal(score))

    def _analyze_valuation(self, realtime: dict) -> Section:
        if not realtime:
            return Section(key="valuation", title="估值水平",
                           content="暂无实时行情数据", score=50, signal="hold")

        pe  = realtime.get("pe_ratio") or realtime.get("pe")
        pb  = realtime.get("pb_ratio") or realtime.get("pb")
        mv  = realtime.get("total_mv") or realtime.get("market_cap")
        circ = realtime.get("circ_mv")

        score = 50
        lines = ["**估值指标**"]
        if pe:
            lines.append(f"- 市盈率（PE）：{pe:.1f}x")
            if pe < 15:    score = min(score + 10, 75)
            elif pe > 50:  score = max(score - 10, 30)
            elif pe < 0:   lines.append("  ⚠️ PE为负（亏损）")
        if pb:
            lines.append(f"- 市净率（PB）：{pb:.2f}x")
            if pb < 1:    lines.append("  ✅ PB<1，资产折价")
        if mv:
            mv_str = f"{mv/1e8:.0f}亿" if mv > 1e8 else f"{mv/1e4:.0f}万"
            lines.append(f"- 总市值：{mv_str}")
        if circ:
            circ_str = f"{circ/1e8:.0f}亿" if circ > 1e8 else f"{circ/1e4:.0f}万"
            lines.append(f"- 流通市值：{circ_str}")

        content = "\n".join(lines)
        return Section(key="valuation", title="估值水平", content=content,
                       data={"pe": pe, "pb": pb, "mv": mv},
                       score=score, signal=self._score_to_signal(score))

    def _analyze_business(self, stock_code, stock_name, llm_call, search) -> Section:
        search_results = ""
        if search:
            try:
                results = search(f"{stock_name} 主营业务 行业地位 竞争优势")
                if results:
                    snippets = [r.get("snippet", r.get("content", ""))[:200] for r in results[:3]]
                    search_results = "\n".join(f"- {s}" for s in snippets if s)
            except Exception as e:
                logger.warning("business search error: %s", e)

        prompt = f"""请简要分析 {stock_name}（{stock_code}）的主营业务情况：
{f"参考资讯：" + chr(10) + search_results if search_results else ""}

请输出（不超过150字）：
1. 核心主营业务（1-2句）
2. 行业地位（龙头/二线/细分领域）
3. 主要竞争优势或风险
4. 近期经营亮点或隐忧"""
        try:
            content = llm_call(prompt).strip()
        except Exception as e:
            content = f"主营业务分析失败：{e}"
        return Section(key="business", title="主营业务", content=content, score=50, signal="hold")

    def _analyze_northbound(self, stock_code: str) -> Section:
        """北向资金（陆股通）近20日净流入分析"""
        try:
            import akshare as ak
            df_nb = None
            # 依次尝试多个可能的 akshare 接口
            for fn_name, kwargs in [
                ("stock_hsgt_individual_em",   {"stock": stock_code}),
                ("stock_hsgt_hist_em",         {"symbol": stock_code}),
                ("stock_hsgt_north_individual_account_top_10_em", {"symbol": stock_code}),
            ]:
                fn = getattr(ak, fn_name, None)
                if fn is None:
                    continue
                try:
                    df_nb = fn(**kwargs)
                    if df_nb is not None and not df_nb.empty:
                        break
                except Exception:
                    df_nb = None

            if df_nb is None or df_nb.empty:
                raise ValueError("所有接口均无数据")

            # 统一找净流入列
            net_col = None
            for cand in ["净买入额", "净流入", "net_inflow", "净买入", "成交净买额"]:
                if cand in df_nb.columns:
                    net_col = cand
                    break
            if net_col is None:
                # 取最后一个数值列作为净流入
                num_cols = df_nb.select_dtypes("number").columns.tolist()
                if not num_cols:
                    raise ValueError("无法识别净流入列")
                net_col = num_cols[-1]

            series = df_nb[net_col].tail(20).fillna(0).astype(float)
            recent5  = series.tail(5)
            recent20 = series

            sum5  = recent5.sum()
            sum20 = recent20.sum()
            # 趋势：连续5日方向
            consec_in  = (recent5 > 0).all()
            consec_out = (recent5 < 0).all()

            if consec_in:
                score = 72
                trend = "连续5日净流入，北向持续增持"
            elif consec_out:
                score = 32
                trend = "连续5日净流出，北向持续减仓"
            else:
                score = 52
                trend = "近5日北向资金流向混合"

            def _fmt(v):
                if abs(v) >= 1e8:  return f"{v/1e8:+.2f}亿"
                if abs(v) >= 1e4:  return f"{v/1e4:+.2f}万"
                return f"{v:+.2f}"

            lines = [
                f"**北向资金（陆股通）**",
                f"- 近5日累计净流入：{_fmt(sum5)}",
                f"- 近20日累计净流入：{_fmt(sum20)}",
                f"- 趋势：{trend}",
            ]
            content = "\n".join(lines)
            return Section(key="northbound", title="北向资金", content=content,
                           score=score, signal=self._score_to_signal(score))

        except Exception as e:
            logger.warning("northbound analysis error for %s: %s", stock_code, e)
            return Section(key="northbound", title="北向资金",
                           content="北向资金数据暂不可用", score=50, signal="hold")

    def _analyze_holder_change(self, stock_code: str) -> Section:
        """大股东/董监高增减持分析（近90天）"""
        try:
            import akshare as ak
            import pandas as pd
            from datetime import datetime, timedelta

            df_ht = None
            for fn_name, kwargs in [
                ("stock_holdertrade_em",   {"symbol": stock_code}),
                ("stock_hold_trade_detail_em", {"symbol": stock_code}),
            ]:
                fn = getattr(ak, fn_name, None)
                if fn is None:
                    continue
                try:
                    df_ht = fn(**kwargs)
                    if df_ht is not None and not df_ht.empty:
                        break
                except Exception:
                    df_ht = None

            if df_ht is None or df_ht.empty:
                raise ValueError("无增减持数据")

            # 找日期列
            date_col = None
            for cand in ["变动截止日", "公告日期", "截止日期", "变动日期", "date"]:
                if cand in df_ht.columns:
                    date_col = cand
                    break
            cutoff = datetime.now() - timedelta(days=90)
            if date_col:
                df_ht[date_col] = pd.to_datetime(df_ht[date_col], errors="coerce")
                df_ht = df_ht[df_ht[date_col] >= cutoff]

            if df_ht.empty:
                return Section(key="holder_change", title="大股东增减持",
                               content="近90天无增减持公告", score=52, signal="hold")

            # 找变动类型列
            type_col = None
            for cand in ["变动类型", "增减", "类型", "type"]:
                if cand in df_ht.columns:
                    type_col = cand
                    break

            # 找金额列
            amt_col = None
            for cand in ["变动金额", "交易金额", "金额", "变动市值", "amount"]:
                if cand in df_ht.columns:
                    amt_col = cand
                    break

            # 找持有人列（判断是否为大股东/实际控制人/董监高）
            holder_col = None
            for cand in ["股东名称", "变动人", "持有人", "holder"]:
                if cand in df_ht.columns:
                    holder_col = cand
                    break

            KEY_ROLES = ("大股东", "实际控制人", "董事", "监事", "高级管理", "总经理", "董事长", "控股")

            def _is_key_role(name: str) -> bool:
                if not name:
                    return False
                return any(kw in str(name) for kw in KEY_ROLES)

            def _parse_type(val: str) -> str:
                v = str(val)
                if any(k in v for k in ("减持", "卖出", "减少")):
                    return "sell"
                if any(k in v for k in ("增持", "买入", "增加")):
                    return "buy"
                return "unknown"

            sell_cnt = buy_cnt = 0
            sell_amt = buy_amt = 0.0
            key_sell_amt = key_buy_amt = 0.0

            for _, row in df_ht.iterrows():
                t = _parse_type(row.get(type_col, "") if type_col else "")
                amt = 0.0
                if amt_col:
                    try:
                        amt = float(str(row[amt_col]).replace(",", "").replace("万", "")) or 0.0
                        # 如果单位不是元则换算（简单启发：数值很小时乘以10000）
                        if amt != 0 and abs(amt) < 100:
                            amt *= 1e4
                    except (ValueError, TypeError):
                        amt = 0.0
                is_key = _is_key_role(row.get(holder_col, "") if holder_col else "")
                if t == "sell":
                    sell_cnt += 1
                    sell_amt += amt
                    if is_key:
                        key_sell_amt += amt
                elif t == "buy":
                    buy_cnt += 1
                    buy_amt += amt
                    if is_key:
                        key_buy_amt += amt

            net = buy_amt - sell_amt
            WEIGHT = 1.5
            weighted_net = (key_buy_amt - key_sell_amt) * WEIGHT + (
                (buy_amt - key_buy_amt) - (sell_amt - key_sell_amt)
            )

            if weighted_net < -1e7:
                score = 28
                signal_text = "大股东/董监高净减持超千万，强卖出信号"
            elif weighted_net > 5e6:
                score = 70
                signal_text = "大股东/董监高净增持超五百万，强买入信号"
            elif sell_cnt == 0 and buy_cnt == 0:
                score = 52
                signal_text = "近90天无增减持动作"
            else:
                score = 52
                signal_text = "近90天增减持信号中性"

            def _fmt(v):
                if abs(v) >= 1e8:  return f"{v/1e8:.2f}亿"
                if abs(v) >= 1e4:  return f"{v/1e4:.2f}万"
                return f"{v:.2f}"

            lines = [
                "**大股东增减持（近90天）**",
                f"- 减持：{sell_cnt}笔，合计约 {_fmt(sell_amt)}",
                f"- 增持：{buy_cnt}笔，合计约 {_fmt(buy_amt)}",
                f"- 净增持：{_fmt(net)}",
                f"- 信号：{signal_text}",
            ]
            if key_sell_amt or key_buy_amt:
                lines.append(f"- 大股东/实控人/董监高净增持：{_fmt(key_buy_amt - key_sell_amt)}")

            content = "\n".join(lines)
            return Section(key="holder_change", title="大股东增减持", content=content,
                           score=score, signal=self._score_to_signal(score))

        except Exception as e:
            logger.warning("holder_change analysis error for %s: %s", stock_code, e)
            return Section(key="holder_change", title="大股东增减持",
                           content="增减持数据暂不可用", score=50, signal="hold")

    @staticmethod
    def _fmt_money(val) -> str:
        if val is None: return "N/A"
        if abs(val) >= 1e8:  return f"{val/1e8:.2f}亿"
        if abs(val) >= 1e4:  return f"{val/1e4:.2f}万"
        return f"{val:.2f}"

    @staticmethod
    def _score_to_signal(score: int) -> str:
        if score >= 70: return "buy"
        if score >= 55: return "watch"
        if score >= 40: return "hold"
        return "sell"
