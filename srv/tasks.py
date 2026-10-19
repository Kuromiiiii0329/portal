"""
portal/srv/tasks.py
后台任务执行层 —— 4 个 _run_*_task 后台线程体。

从原 server.py 逐字节搬迁：
  _run_analysis_task        子进程模式（跑 main.py）
  _run_deep_analysis_task   进程内深度双维度分析（编排枢纽）
  _run_chat_task            AI 对话（AgentOrchestrator）
  _run_market_review_task   大盘复盘

依赖：state（_tasks/_tasks_lock）、_config（常量/logger）、
      data_access / llm_gateway / prompts 的模块级函数。
这些函数原为 server.py 模块级裸名调用，此处改为从对应子模块导入，行为等价。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime

from ._config import logger, TZ_CN, PROJECT_ROOT, LIB_DIR
from .state import _tasks, _tasks_lock
from .data_access import (
    _json_default, _recompute_tech_dimension, _load_dotenv, _fetch_kline,
)
from .llm_gateway import (
    _make_llm_caller, _make_search_fn, _apply_agent_env, _summarize_report_for_agent,
)
from .prompts import build_review_prompt, build_forecast_prompt


def _run_analysis_task(task_id: str, cmd: list, env: dict):
    """在后台线程中运行分析命令，收集日志。"""
    with _tasks_lock:
        _tasks[task_id]["status"] = "running"
        _tasks[task_id]["started_at"] = datetime.now(TZ_CN).isoformat()

    logs = _tasks[task_id]["logs"]
    logs.append(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] 🚀 启动分析...")
    logs.append(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] 命令: {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(PROJECT_ROOT),
        )
        with _tasks_lock:
            _tasks[task_id]["pid"] = proc.pid

        for line in proc.stdout:
            line = line.rstrip()
            if line:
                logs.append(line)

        proc.wait()
        exit_code = proc.returncode

        # 分析完成后读取报告文件
        date_str = datetime.now(TZ_CN).strftime("%Y%m%d")
        report_parts = []
        for fname in [f"market_review_{date_str}.md", f"report_{date_str}.md"]:
            fpath = PROJECT_ROOT / "reports" / fname
            if fpath.exists():
                report_parts.append(fpath.read_text(encoding="utf-8"))

        report = "\n\n---\n\n".join(report_parts) if report_parts else ""

        with _tasks_lock:
            _tasks[task_id]["status"] = "done" if exit_code == 0 else "error"
            _tasks[task_id]["exit_code"] = exit_code
            _tasks[task_id]["report"] = report
            _tasks[task_id]["finished_at"] = datetime.now(TZ_CN).isoformat()

        emoji = "✅" if exit_code == 0 else "❌"
        logs.append(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] {emoji} 分析完成，退出码: {exit_code}")
        if report:
            logs.append(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] 📄 报告已生成")
        else:
            logs.append(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] ⚠️  未找到报告文件（可能是非交易日）")

    except Exception as e:
        logger.exception("任务 %s 执行异常: %s", task_id, e)
        logs.append(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] ❌ 异常: {e}")
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["finished_at"] = datetime.now(TZ_CN).isoformat()



def _run_deep_analysis_task(
    task_id: str,
    stock_code: str,
    stock_name: str,
    dimensions: list,
    modules_map: dict,
    llm_mode: str = "batch",
    open_report: bool = True,
    agent_review: bool = False,
):
    """在后台线程中运行双维度深度分析。"""
    sys.path.insert(0, str(PROJECT_ROOT))

    logs = _tasks[task_id]["logs"]

    def log(msg):
        ts = datetime.now(TZ_CN).strftime("%H:%M:%S")
        logs.append(f"[{ts}] {msg}")
        logger.info("[task %s] %s", task_id, msg)

    with _tasks_lock:
        _tasks[task_id]["status"] = "running"
        _tasks[task_id]["started_at"] = datetime.now(TZ_CN).isoformat()

    try:
        log(f"🚀 开始深度分析：{stock_name}（{stock_code}）")
        log(f"📊 分析维度：{dimensions}")

        # ── 加载环境变量 (.env) ──────────────────────────────
        _load_dotenv()

        # ── 构建 LLM 调用函数 ────────────────────────────────
        llm_call = _make_llm_caller(log)

        # ── 构建搜索函数 ─────────────────────────────────────
        search_fn = _make_search_fn(log)

        # ── 获取 K线数据（各分析器共用）────────────────────
        log("📈 获取K线数据...")
        df = _fetch_kline(stock_code, log)

        # ── 执行各维度分析 ────────────────────────────────────
        from portal.analyzers import ANALYZER_REGISTRY
        results = []

        for dim in dimensions:
            cls = ANALYZER_REGISTRY.get(dim)
            if not cls:
                log(f"⚠️  未知分析维度：{dim}，跳过")
                continue

            analyzer = cls()
            active_modules = modules_map.get(dim) or cls.DEFAULT_MODULES
            log(f"🔍 [{analyzer.name}] 开始分析，子模块：{active_modules}")

            try:
                kwargs = dict(
                    stock_code=stock_code,
                    stock_name=stock_name,
                    df=df,
                    modules=active_modules,
                    llm_call=llm_call,
                    search=search_fn,
                )
                import inspect
                if 'log' in inspect.signature(analyzer.analyze).parameters:
                    kwargs['log'] = log
                dim_result = analyzer.analyze(**kwargs)
                if dim_result.error:
                    log(f"⚠️  [{analyzer.name}] 分析异常：{dim_result.error}")
                else:
                    log(f"✅ [{analyzer.name}] 完成，评分={dim_result.score}，信号={dim_result.signal}")
                results.append(dim_result)
            except Exception as e:
                log(f"❌ [{analyzer.name}] 执行失败：{e}")
                logger.exception("Analyzer %s failed for %s", dim, stock_code)

        # ── 技术面逐指标 LLM 打分（零硬编码：score+signal 全由 LLM 出）──
        #    必须在 merge 之前回写 Section，merge 才能用 LLM 分聚合综合分。
        tech_llm_notes = {}
        try:
            from portal.llm_notes import score_sections
            from portal.data_cache import _last_trading_date
            trade_date = _last_trading_date()
            tech_result = next((r for r in results if r.dimension == "technical"), None)
            if tech_result and tech_result.sections:
                scores = score_sections(stock_code, stock_name, trade_date,
                                        tech_result.sections, llm_call, mode=llm_mode, log=log)
                # 回写到 Section 对象（网页端/HTML 两端同源，自动显示 LLM 分）
                for s in tech_result.sections:
                    if s.key in scores:
                        s.score = scores[s.key]["score"]
                        s.signal = scores[s.key]["signal"]
                        # per_indicator 模式下 LLM 产出了详细分析，追加到客观描述之后展示
                        detail = scores[s.key].get("detail")
                        if detail:
                            s.content = (s.content or "").rstrip() + "\n\n**📊 LLM 分析**\n" + detail
                tech_llm_notes = scores
                # 重算技术面维度综合分 + 维度综合信号（信号交给 LLM 做总结判断）
                _recompute_tech_dimension(tech_result, llm_call, log)
        except Exception as e:
            logger.warning("技术面 LLM 打分失败: %s", e)
            log(f"⚠️ 技术面 LLM 打分失败：{e}")

        # ── 合并报告 ──────────────────────────────────────────
        log("📝 合并各维度结果...")
        from portal.analyzers.merger import merge_results
        final_report = merge_results(stock_code, stock_name, results, llm_call)
        final_report["llm_notes"] = tech_llm_notes
        log(f"🎯 综合评分={final_report['overall_score']}，信号={final_report['overall_signal_label']}")

        # ── 注入 K线数据（最近60条 date/open/high/low/close/ma5/ma20）──
        if df is not None and not df.empty:
            try:
                kline_df = df.copy()
                # 统一列名
                col_map = {}
                for c in kline_df.columns:
                    lc = c.lower()
                    if lc in ('trade_date', 'tradedate', 'date'):
                        col_map[c] = 'date'
                    elif lc == 'open':
                        col_map[c] = 'open'
                    elif lc == 'high':
                        col_map[c] = 'high'
                    elif lc == 'low':
                        col_map[c] = 'low'
                    elif lc == 'close':
                        col_map[c] = 'close'
                    elif lc == 'ma5':
                        col_map[c] = 'ma5'
                    elif lc == 'ma10':
                        col_map[c] = 'ma10'
                    elif lc == 'ma20':
                        col_map[c] = 'ma20'
                    elif lc == 'ma30':
                        col_map[c] = 'ma30'
                    elif lc == 'ma60':
                        col_map[c] = 'ma60'
                    elif lc == 'ma120':
                        col_map[c] = 'ma120'
                    elif lc == 'ma250':
                        col_map[c] = 'ma250'
                kline_df = kline_df.rename(columns=col_map)

                # 如果缺少均线，现场计算
                if 'close' in kline_df.columns:
                    for period, min_p in [(5,1),(10,1),(20,1),(30,1),(60,1),(120,60),(250,200)]:
                        col = f'ma{period}'
                        if col not in kline_df.columns:
                            kline_df[col] = kline_df['close'].rolling(period, min_periods=min_p).mean().round(2)

                keep_cols = [c for c in ('date', 'open', 'high', 'low', 'close',
                                         'ma5', 'ma10', 'ma20', 'ma30', 'ma60', 'ma120', 'ma250')
                             if c in kline_df.columns]
                kline_df = kline_df[keep_cols].tail(250)

                # 序列化为干净的 list[dict]，NaN → None
                kline_records = []
                for row in kline_df.to_dict(orient='records'):
                    cleaned = {}
                    for k, v in row.items():
                        if v is None:
                            cleaned[k] = None
                        elif isinstance(v, float) and v != v:  # NaN
                            cleaned[k] = None
                        elif hasattr(v, 'item'):  # numpy scalar
                            raw = v.item()
                            cleaned[k] = str(raw) if k == 'date' else round(float(raw), 2)
                        elif isinstance(v, float):
                            cleaned[k] = round(v, 2)
                        else:
                            cleaned[k] = str(v) if k == 'date' else v
                    kline_records.append(cleaned)

                final_report['kline_data'] = kline_records
                log(f"📊 K线数据已注入报告（{len(kline_records)} 条）")
            except Exception as e:
                logger.warning("注入 kline_data 失败: %s", e)
                final_report['kline_data'] = []
        else:
            final_report['kline_data'] = []

        # ── 保存元信息到缓存 ──────────────────────────────────
        try:
            from portal.data_cache import StockDataCache
            cache = StockDataCache()
            # 从产业链维度结果里提取关键词
            industry_result = next(
                (r for r in results if r.dimension == "industry"), None
            )
            keywords = []
            if industry_result:
                for sec in industry_result.sections:
                    if sec.key == "chain_keywords":
                        keywords = sec.data.get("keywords", [])
                        break
            cache.save_meta(stock_code, name=stock_name, keywords=keywords)
            log(f"💾 元信息已保存（关键词: {len(keywords)} 个）")
        except Exception as e:
            logger.warning("保存 meta 失败: %s", e)

        # 注：技术面逐指标 LLM 打分已在 merge 之前完成并回写（见上），此处不再重复。

        # ── 🤖 Agent 综合研判（选项B：单次 LLM 复用已算数据，零重复取数）──
        if agent_review:
            summary_text = _summarize_report_for_agent(final_report)
            try:
                log("🤖 Agent 综合研判：基于已算好的全部指标做深度研判...")
                if llm_call:
                    review_text = llm_call(build_review_prompt(stock_name, stock_code, summary_text)).strip()
                    final_report["agent_review"] = review_text
                    log(f"✅ Agent 综合研判完成（{len(review_text)} 字）")
                else:
                    final_report["agent_review"] = ""
                    log("⚠️ 未配置 LLM，跳过 Agent 综合研判")
            except Exception as e:
                logger.warning("Agent 综合研判失败: %s", e)
                log(f"⚠️ Agent 综合研判失败：{e}")
                final_report["agent_review"] = ""

            # ── 📈 走势预测（7 交易日模拟 K 线 + 次日高低点）──
            try:
                log("📈 走势预测：预测未来 7 交易日走势及次日高低点...")
                kline_tail = [
                    r for r in (final_report.get("kline_data") or [])
                    if isinstance(r, dict) and r.get("close")
                ][-15:]  # 喂最近 15 条（含 OHLC）

                # 读取或现场生成回测数据（供预测 prompt 参考）
                backtest_result = None
                try:
                    from portal.data_cache import StockDataCache
                    from portal.backtester import run_backtest
                    _cache = StockDataCache()
                    backtest_result = _cache.get_backtest(stock_code)
                    if backtest_result is None and df is not None and not df.empty:
                        log("📊 回测数据缺失，现场生成...")
                        backtest_result = run_backtest(df)
                        _cache.save_backtest(stock_code, backtest_result)
                        log("✅ 回测数据已生成并缓存")
                except Exception as _be:
                    logger.warning("走势预测阶段获取回测数据失败: %s", _be)

                # ── 提取当前触发的形态，关联回测胜率 ──────────────
                active_patterns = _extract_active_patterns(results, backtest_result, kline_tail)
                if active_patterns:
                    log(f"🔍 当前触发形态：{[p['name'] for p in active_patterns]}")

                if llm_call and kline_tail:
                    raw = llm_call(build_forecast_prompt(
                        stock_name, stock_code, summary_text, kline_tail,
                        backtest_result, active_patterns,
                    )).strip()
                    # 抠 JSON
                    s, e_ = raw.find("{"), raw.rfind("}")
                    forecast_obj = json.loads(raw[s:e_+1]) if s >= 0 and e_ > s else {}
                    forecast_obj["active_patterns"] = active_patterns
                    final_report["price_forecast"] = forecast_obj
                    log("✅ 走势预测完成")
                else:
                    final_report["price_forecast"] = {"active_patterns": active_patterns}
                    if not llm_call:
                        log("⚠️ 未配置 LLM，跳过走势预测")
                    else:
                        log("⚠️ K线数据不足，跳过走势预测")
            except Exception as e:
                logger.warning("走势预测失败: %s", e)
                log(f"⚠️ 走势预测失败：{e}")
                final_report["price_forecast"] = {}

        # ── 生成独立 HTML 报告 + 自动打开 ─────────────────────
        html_path = None
        try:
            from portal.report_html import render_stock_report, open_in_browser
            html_path = render_stock_report(final_report, final_report.get("llm_notes"))
            log(f"📄 HTML 报告已生成：{html_path}")
            if open_report:
                open_in_browser(html_path, log)
        except Exception as e:
            logger.warning("生成 HTML 报告失败: %s", e)
            log(f"⚠️ HTML 报告生成失败：{e}")

        with _tasks_lock:
            _tasks[task_id]["status"] = "done"
            if html_path:
                final_report["html_path"] = str(html_path)
                _tasks[task_id]["html_path"] = str(html_path)
            _tasks[task_id]["report"] = json.dumps(final_report, ensure_ascii=False, default=_json_default)
            _tasks[task_id]["finished_at"] = datetime.now(TZ_CN).isoformat()

        log("✅ 深度分析完成")

    except Exception as e:
        logger.exception("Deep analysis task %s failed: %s", task_id, e)
        logs.append(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] ❌ 分析失败：{e}")
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["finished_at"] = datetime.now(TZ_CN).isoformat()


def _run_chat_task(task_id: str, message: str, session_id: str,
                   stock_code: str = "", stock_name: str = ""):
    """后台线程：交互式 AI 对话（选项A）→ AgentOrchestrator.chat 多轮。

    进度经 progress_callback 转成日志行走现有 SSE（/run/stream/<task_id>）。
    最终答案（result.content）存入 _tasks[task_id]["report"]，report_type="chat"。
    """
    sys.path.insert(0, str(LIB_DIR))
    sys.path.insert(0, str(PROJECT_ROOT))
    logs = _tasks[task_id]["logs"]

    def log(msg):
        ts = datetime.now(TZ_CN).strftime("%H:%M:%S")
        logs.append(f"[{ts}] {msg}")
        logger.info("[chat %s] %s", task_id, msg)

    with _tasks_lock:
        _tasks[task_id]["status"] = "running"
        _tasks[task_id]["started_at"] = datetime.now(TZ_CN).isoformat()

    try:
        log(f"💬 收到提问：{message[:60]}")
        _load_dotenv()
        _apply_agent_env()

        from src.config import get_config
        from src.agent.factory import build_agent_executor

        log("🤖 构建 multi-agent 编排器（Technical→Intel→Decision）...")
        executor = build_agent_executor(get_config())

        def progress_cb(ev):
            try:
                t = (ev or {}).get("type")
                if t == "stage_start":
                    log(f"🔹 {ev.get('stage')} 开始…")
                elif t == "stage_done":
                    dur = ev.get("duration")
                    dur_s = f"{dur:.1f}s" if isinstance(dur, (int, float)) else ""
                    log(f"✅ {ev.get('stage')} 完成（{ev.get('status', '')} {dur_s}）")
                elif t == "pipeline_timeout":
                    log(f"⏱ {ev.get('stage')} 超时（{ev.get('elapsed')}s/{ev.get('timeout')}s）")
            except Exception:
                pass

        ctx = {"stock_code": stock_code, "stock_name": stock_name} if stock_code else None
        result = executor.chat(
            message=message,
            session_id=session_id,
            progress_callback=progress_cb,
            context=ctx,
        )

        content = getattr(result, "content", "") or ""
        success = getattr(result, "success", True)
        with _tasks_lock:
            _tasks[task_id]["report"] = content
            _tasks[task_id]["report_type"] = "chat"
            _tasks[task_id]["status"] = "done" if success else "error"
            _tasks[task_id]["finished_at"] = datetime.now(TZ_CN).isoformat()
        if success:
            log(f"✅ 回答完成（{len(content)} 字）")
        else:
            err = getattr(result, "error", "") or "未知错误"
            log(f"⚠️ 回答异常：{err}")

    except Exception as e:
        logger.exception("Chat task %s failed: %s", task_id, e)
        logs.append(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] ❌ 对话失败：{e}")
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["report"] = f"[对话失败] {e}"
            _tasks[task_id]["finished_at"] = datetime.now(TZ_CN).isoformat()


def _run_market_review_task(task_id: str, open_report: bool = True):
    """后台线程：大盘复盘（上证+创业板）→ 一份 HTML → 打开。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    logs = _tasks[task_id]["logs"]

    def log(msg):
        ts = datetime.now(TZ_CN).strftime("%H:%M:%S")
        logs.append(f"[{ts}] {msg}")
        logger.info("[market %s] %s", task_id, msg)

    with _tasks_lock:
        _tasks[task_id]["status"] = "running"
        _tasks[task_id]["started_at"] = datetime.now(TZ_CN).isoformat()

    try:
        log("🌐 开始大盘复盘（上证指数 + 创业板指）")
        _load_dotenv()
        llm_call = _make_llm_caller(log)

        from portal.analyzers.market import MarketAnalyzer
        mkt = MarketAnalyzer()
        results = mkt.analyze_all(llm_call, log)
        if not results:
            raise RuntimeError("未获取到任何指数数据")

        log("📝 生成大盘整体研判...")
        overall = mkt.build_overall_summary(results, llm_call, log)

        from portal.report_html import render_market_report, open_in_browser
        html_path = render_market_report(results, overall)
        log(f"📄 大盘 HTML 报告已生成：{html_path}")
        if open_report:
            open_in_browser(html_path, log)

        with _tasks_lock:
            _tasks[task_id]["status"] = "done"
            _tasks[task_id]["html_path"] = str(html_path)
            _tasks[task_id]["report"] = json.dumps(
                {"kind": "market", "indices": results, "overall_summary": overall,
                 "html_path": str(html_path)},
                ensure_ascii=False, default=_json_default)
            _tasks[task_id]["finished_at"] = datetime.now(TZ_CN).isoformat()
        log("✅ 大盘复盘完成")

    except Exception as e:
        logger.exception("Market review task %s failed: %s", task_id, e)
        logs.append(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] ❌ 大盘复盘失败：{e}")
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["finished_at"] = datetime.now(TZ_CN).isoformat()


def _extract_active_patterns(results: list, backtest_result: dict | None, kline_tail: list) -> list:
    """从技术面各 section.data 提取当前触发的形态，关联回测胜率数据。

    返回 list[dict]，每条：
      name       信号名（与 backtester SIGNALS 一致）
      triggered  True 表示当前 K 线末尾已触发
      last_date  该信号最近历史触发日期（来自 backtest）
      count      历史触发次数
      stats      {1,3,5,10,20} 日胜率/均收益（来自 backtest）
    """
    last_kline = kline_tail[-1] if kline_tail else {}
    close = last_kline.get("close", 0) or 0

    tech_result = next((r for r in (results or []) if getattr(r, "dimension", "") == "technical"), None)

    def _sec_data(key):
        if not tech_result:
            return {}
        sec = next((s for s in (tech_result.sections or []) if s.key == key), None)
        return sec.data if sec else {}

    macd = _sec_data("macd")
    kdj  = _sec_data("kdj")
    rsi  = _sec_data("rsi")
    vol  = _sec_data("volume")
    boll = _sec_data("bollinger")
    ma   = _sec_data("ma_system")

    triggered_map = {
        "MACD金叉":        bool(macd.get("golden")),
        "MACD死叉":        bool(macd.get("death")),
        "RSI超卖(<30)":    (rsi.get("rsi6", 50) or 50) < 30,
        "RSI超买(>70)":    (rsi.get("rsi6", 50) or 50) > 70,
        "KDJ金叉":         bool(kdj.get("golden")),
        "放量上涨":         (vol.get("vol_ratio", 0) or 0) > 2 and (vol.get("close_change", 0) or 0) > 0,
        "放量下跌":         (vol.get("vol_ratio", 0) or 0) > 2 and (vol.get("close_change", 0) or 0) < 0,
        "均线多头排列":     bool(
            ma.get("ma5") and ma.get("ma10") and ma.get("ma20")
            and (ma.get("ma5") or 0) > (ma.get("ma10") or 0) > (ma.get("ma20") or 0) > 0
        ),
        "价格跌破布林下轨": close > 0 and (boll.get("lower", 0) or 0) > 0 and close < (boll.get("lower") or 0),
    }

    bt_signals = (backtest_result or {}).get("signals") or {}
    patterns = []
    for sig_name, is_triggered in triggered_map.items():
        bt = bt_signals.get(sig_name)
        if not isinstance(bt, dict) or bt.get("count", 0) == 0:
            stats = None
            last_date = None
        else:
            stats = bt.get("stats") or {}
            last_date = bt.get("last_date")
        patterns.append({
            "name":      sig_name,
            "triggered": is_triggered,
            "last_date": last_date,
            "count":     (bt or {}).get("count", 0),
            "stats":     stats,
        })

    patterns.sort(key=lambda p: (not p["triggered"], -(p["count"] or 0)))
    return patterns
