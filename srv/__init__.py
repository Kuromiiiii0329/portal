"""
portal/srv/  服务实现包（从原 server.py 拆分）

原单文件 server.py（1578行）按职责分层拆分为本包，server.py 保留为薄入口
（路径注入 + main() + re-export），启动方式 `python portal/server.py` 不变。

分层（依赖自上而下，无环）：
  _config.py       常量 / 路径注入 / 日志设置（最先导入）
  state.py         _tasks / _tasks_lock 全局任务状态
  prompts.py       build_review_prompt / build_forecast_prompt
  llm_gateway.py   _make_llm_caller / _apply_agent_env / _make_search_fn / _summarize_report_for_agent
  data_access.py   _load_dotenv / _fetch_kline / _recompute_tech_dimension / _json_default
  tasks.py         _run_analysis_task / _run_deep_analysis_task / _run_chat_task / _run_market_review_task
  http_handler.py  Handler（HTTP 路由 + _handle_*）

本 __init__ re-export 常用符号，供 server.py 薄入口与外部（如测试）统一引用。
"""
from __future__ import annotations

from ._config import (
    PORT, ALLOWED_ENV_KEYS, PORTAL_DIR, PROJECT_ROOT, LIB_DIR, CONFIG_PATH,
    TZ_CN, logger,
)
from .state import _tasks, _tasks_lock
from .prompts import build_review_prompt, build_forecast_prompt
from .llm_gateway import (
    _make_llm_caller, _apply_agent_env, _make_search_fn, _summarize_report_for_agent,
)
from .data_access import (
    _json_default, _recompute_tech_dimension, _load_dotenv, _fetch_kline,
)
from .tasks import (
    _run_analysis_task, _run_deep_analysis_task, _run_chat_task, _run_market_review_task,
)
from .http_handler import Handler

__all__ = [
    "PORT", "ALLOWED_ENV_KEYS", "PORTAL_DIR", "PROJECT_ROOT", "LIB_DIR",
    "CONFIG_PATH", "TZ_CN", "logger",
    "_tasks", "_tasks_lock",
    "build_review_prompt", "build_forecast_prompt",
    "_make_llm_caller", "_apply_agent_env", "_make_search_fn", "_summarize_report_for_agent",
    "_json_default", "_recompute_tech_dimension", "_load_dotenv", "_fetch_kline",
    "_run_analysis_task", "_run_deep_analysis_task", "_run_chat_task", "_run_market_review_task",
    "Handler",
]
