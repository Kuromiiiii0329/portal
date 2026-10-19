"""
portal/srv/llm_gateway.py
LLM 网关层 —— 构建 LLM 调用/搜索闭包，agent 环境映射，报告摘要。

从原 server.py 的 _make_llm_caller / _apply_agent_env / _summarize_report_for_agent /
_make_search_fn 逐字节搬迁。logger 从 _config 复用（等价原模块级 logger）。
"""
from __future__ import annotations

import os

from ._config import logger


def _make_llm_caller(log):
    """构建 LLM 调用函数，复用 litellm。

    优先级：
      1. Hai Proxy（SAP 内网 OpenAI 兼容网关）—— HAI_BASE_URL + HAI_API_KEY + HAI_MODEL
         用于内网直连外部 LLM（DeepSeek/OpenAI）被封锁的场景。
      2. LITELLM_MODEL / GEMINI / DEEPSEEK / OPENAI（公网直连）
    """
    try:
        import litellm
        # 关掉 litellm 的调试提示（含 "Provider List: https://..." 这类无用行）
        try:
            litellm.suppress_debug_info = True
            litellm.set_verbose = False
        except Exception:
            pass

        # ── 优先：Hai Proxy（内网 OpenAI 兼容网关）──────────────
        hai_base = os.environ.get("HAI_BASE_URL")
        hai_key  = os.environ.get("HAI_API_KEY")
        if hai_base and hai_key:
            hai_model = os.environ.get("HAI_MODEL", "gpt-4.1")
            # litellm 用 openai/ 前缀走 OpenAI 兼容协议 + 自定义 api_base
            model = f"openai/{hai_model}"
            log(f"🤖 LLM：Hai Proxy（{hai_model} @ {hai_base}）")

            # GPT-5 系列不支持自定义 temperature（只接受默认值），且有 reasoning 开销
            is_gpt5 = "gpt-5" in hai_model.lower()

            def call_hai(prompt: str) -> str:
                kwargs = dict(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    api_base=hai_base,
                    api_key=hai_key,
                    max_tokens=8192,   # litellm 自动转 max_completion_tokens；调大以容纳
                                       # K线形态/波浪/缠论等长结构化输出，并为 GPT-5 系列的
                                       # reasoning 开销预留预算，避免返回空内容（content=""）
                    timeout=90,
                )
                if not is_gpt5:
                    kwargs["temperature"] = 0.3   # GPT-5 只允许默认 temperature，故省略
                resp = litellm.completion(**kwargs)
                return resp.choices[0].message.content or ""

            return call_hai

        # ── 回退：公网直连模型 ──────────────────────────────────
        model = (
            os.environ.get("LITELLM_MODEL")
            or os.environ.get("GEMINI_MODEL")
            or ("gemini/gemini-2.0-flash" if os.environ.get("GEMINI_API_KEY") else None)
            or ("deepseek/deepseek-chat" if os.environ.get("DEEPSEEK_API_KEY") else None)
            or ("gpt-4o-mini" if os.environ.get("OPENAI_API_KEY") else None)
        )

        if not model:
            log("⚠️  未配置 LLM（Hai Proxy 或 API Key），LLM 相关子模块将跳过")
            return None

        log(f"🤖 LLM 模型：{model}")

        def call(prompt: str) -> str:
            resp = litellm.completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=8192,   # 调大以容纳长结构化输出（K线形态/波浪/缠论），避免返回空内容
                timeout=60,
            )
            return resp.choices[0].message.content or ""

        return call

    except ImportError:
        log("⚠️  litellm 未安装，LLM 子模块跳过")
        return None
    except Exception as e:
        log(f"⚠️  LLM 初始化失败：{e}")
        return None


def _apply_agent_env():
    """把 portal 现有的 HAI_* 网关配置映射为 agent 框架（get_config）认识的 OPENAI_*，
    并设定 multi-agent 编排默认参数。用 setdefault，不覆盖用户显式设置。

    必须在构建 agent（build_agent_executor）之前调用。
    """
    hai_key = os.environ.get("HAI_API_KEY")
    hai_base = os.environ.get("HAI_BASE_URL")
    if hai_key and hai_base:
        os.environ.setdefault("OPENAI_API_KEY", hai_key)
        os.environ.setdefault("OPENAI_BASE_URL", hai_base)
        _m = os.environ.get("HAI_MODEL", "gpt-4.1")
        os.environ.setdefault("LITELLM_MODEL", f"openai/{_m}")
    os.environ.setdefault("AGENT_ARCH", "multi")                  # multi → AgentOrchestrator
    os.environ.setdefault("AGENT_ORCHESTRATOR_MODE", "standard")  # technical→intel→decision
    os.environ.setdefault("AGENT_MAX_STEPS", "10")


def _summarize_report_for_agent(final_report: dict) -> str:
    """把已算好的 final_report 抽取成一段结构化中文摘要，供 Agent 综合研判（选项B）。

    只喂"已算好的结论与数据"，不让 LLM 重新取数。
    """
    lines = []
    name = final_report.get("stock_name", "")
    code = final_report.get("stock_code", "")
    lines.append(f"标的：{name}（{code}）")
    lines.append(
        f"系统加权综合评分：{final_report.get('overall_score', '?')}/100，"
        f"综合信号：{final_report.get('overall_signal_label', final_report.get('overall_signal', '?'))}"
    )

    for dim in final_report.get("dimensions", []):
        if not isinstance(dim, dict):
            continue
        dname = dim.get("name") or dim.get("dimension", "")
        if dim.get("error"):
            lines.append(f"\n### {dname}（分析失败：{dim.get('error')}）")
            continue
        lines.append(
            f"\n### {dname}（维度评分 {dim.get('score', '?')}/100，信号：{dim.get('signal', '?')}）"
        )
        for sec in dim.get("sections", []):
            if not isinstance(sec, dict):
                continue
            title = sec.get("title", "")
            content = (sec.get("content") or "").strip()
            first_line = content.split("\n")[0].replace("**", "") if content else ""
            sig = sec.get("signal", "")
            score = sec.get("score", "")
            lines.append(f"- {title}（{sig}/{score}）：{first_line}")

    notes = final_report.get("llm_notes") or {}
    if isinstance(notes, dict) and notes:
        lines.append("\n### 逐指标点评（偏多/偏空）")
        for key, note in notes.items():
            if isinstance(note, dict):
                lines.append(f"- {key}：{note.get('stance', '')} — {note.get('reason', '')}")

    kline = final_report.get("kline_data") or []
    if isinstance(kline, list) and kline:
        lines.append("\n### 最近K线（date/close/ma5/ma20）")
        for r in kline[-5:]:
            if isinstance(r, dict):
                lines.append(
                    f"- {r.get('date')}: close={r.get('close')} ma5={r.get('ma5')} ma20={r.get('ma20')}"
                )

    return "\n".join(lines)


def _make_search_fn(log):
    """
    构建关键词搜索函数，供产业链 LLM 子模块检索资讯。

    注：src/search_service.py 的 SearchService 只提供面向个股的
    search_stock_news(code, name) 等接口，没有通用的 search(keyword)。
    产业链子模块需要的是"关键词 → 资讯片段"，接口不匹配，故此处
    暂不接入（返回 None）。industry.py 已对 search=None 做降级：
    改用 LLM 自身知识分析，不影响板块子模块（板块用 efinance 真实数据）。

    未来若要接入关键词搜索，可在此封装 SearchService.search_stock_news
    或直接调用某个搜索 API，返回 [{"snippet": "..."}] 列表。
    """
    log("ℹ️  关键词搜索未接入，产业链 LLM 子模块将用模型知识分析（板块子模块不受影响）")
    return None
