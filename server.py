#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portal/server.py — 本地配置写入 + 分析触发服务（薄入口）

接口：
  GET  /health          健康检查
  POST /save            写入 config/watchlist.json
  POST /run             启动分析任务，返回 task_id
  GET  /run/status/<id> 查询任务状态 + 日志
  GET  /run/stream/<id> SSE 实时推送日志
  GET  /run/report/<id> 获取分析完成的报告内容

启动：
    cd C:\\Users\\I762120\\Desktop\\incident\\daily
    python portal/server.py

—— 模块化重构说明 ——
原单文件 server.py（1578行）已按职责拆分到 srv/ 子包（_config/state/prompts/
llm_gateway/data_access/tasks/http_handler）。本文件保留为薄入口以维持
`python portal/server.py` 启动方式不变，并 re-export 全部符号，任何
`from server import X` / `import server; server.X` 的历史用法仍然可用。
"""
from __future__ import annotations

# ⚠️ 必须最先导入 srv：其 _config 顶层完成 sys.path 注入（portal/lib 优先），
#    这是所有 analyzer/data_provider 裸导入的前提。
import srv
from srv import *          # noqa: F401,F403  re-export 全部公开符号（含 Handler/常量/任务函数）
from srv import Handler, PORT, CONFIG_PATH, logger

import threading
import webbrowser
from http.server import HTTPServer
from pathlib import Path


class _QuietHTTPServer(HTTPServer):
    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return  # 浏览器主动断开，静默忽略
        super().handle_error(request, client_address)


def main():
    server = _QuietHTTPServer(("127.0.0.1", PORT), Handler)
    logger.info("=" * 50)
    logger.info("Portal 本地服务已启动")
    logger.info("地址: http://127.0.0.1:%d", PORT)
    logger.info("配置文件: %s", CONFIG_PATH)
    logger.info("按 Ctrl+C 停止")
    logger.info("=" * 50)
    html = Path(__file__).parent / "index-standalone.html"
    threading.Timer(0.5, webbrowser.open, args=(html.as_uri(),)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务已停止")


if __name__ == "__main__":
    main()
