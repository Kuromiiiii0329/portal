"""
portal/srv/http_handler.py
HTTP 传输/路由层 —— Handler 类（do_GET/do_POST 路由 + 各 _handle_*）。

从原 server.py 的 Handler 类逐字节搬迁。_handle_* 内部对后台任务函数、
配置常量、全局状态的裸名引用，改为从对应子模块导入（state/_config/tasks/data_access），
行为与原实现等价。13 个 HTTP 端点契约完全保持。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler

from ._config import logger, TZ_CN, PORT, CONFIG_PATH, ALLOWED_ENV_KEYS, PROJECT_ROOT, LIB_DIR
from .state import _tasks, _tasks_lock
from .data_access import _load_dotenv
from .tasks import (
    _run_analysis_task, _run_deep_analysis_task, _run_chat_task, _run_market_review_task,
)


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        logger.info("%s %s", self.address_string(), format % args)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, code: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass  # 客户端已断开（Windows 常见，属正常现象）

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/health", "/"):
            self._send_json(200, {"ok": True, "config_path": str(CONFIG_PATH)})

        elif path == "/env":
            self._handle_get_env()

        elif path == "/quote":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            self._handle_quote(qs)

        elif path == "/open_report":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            code = (qs.get("code", [""])[0]).strip()
            self._handle_open_report(code)

        elif path == "/backtest":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            code = (qs.get("code", [""])[0]).strip().upper()
            self._handle_backtest(code)

        elif path.startswith("/run/stream/"):
            task_id = path.split("/run/stream/")[-1]
            self._handle_sse(task_id)

        elif path.startswith("/run/status/"):
            task_id = path.split("/run/status/")[-1]
            with _tasks_lock:
                task = _tasks.get(task_id)
            if not task:
                self._send_json(404, {"ok": False, "error": "task not found"})
                return
            self._send_json(200, {
                "ok": True,
                "task_id": task_id,
                "status": task["status"],
                "log_count": len(task["logs"]),
                "has_report": bool(task.get("report")),
            })

        elif path.startswith("/run/report/"):
            task_id = path.split("/run/report/")[-1]
            with _tasks_lock:
                task = _tasks.get(task_id)
            if not task:
                self._send_json(404, {"ok": False, "error": "task not found"})
                return
            self._send_json(200, {
                "ok": True,
                "status": task["status"],
                "report": task.get("report", ""),
                "logs": task.get("logs", []),
            })

        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"invalid JSON: {e}"})
            return

        if path == "/save":
            self._handle_save(payload)
        elif path == "/run":
            self._handle_run(payload)
        elif path == "/analyze":
            self._handle_analyze(payload)
        elif path == "/chat":
            self._handle_chat(payload)
        elif path == "/market_review":
            self._handle_market_review(payload)
        elif path == "/env":
            self._handle_set_env(payload)
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def _handle_save(self, payload: dict):
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("✅ 写入 %s  股票: %s", CONFIG_PATH, payload.get("stock_list", []))
            self._send_json(200, {"ok": True, "path": str(CONFIG_PATH)})
        except Exception as e:
            logger.error("写入失败: %s", e)
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_run(self, payload: dict):
        """
        payload 字段：
          mode: "stock" | "market" | "full"
          stocks: ["600519", "300750"]  (mode=stock 时使用)
          dry_run: bool
          force_run: bool
          report_type: "simple" | "full" | "brief"
          no_notify: bool  (默认 true)
        """
        mode = payload.get("mode", "stock")
        stocks = payload.get("stocks", [])
        dry_run = payload.get("dry_run", False)
        force_run = payload.get("force_run", False)
        report_type = payload.get("report_type", "simple")
        no_notify = payload.get("no_notify", True)

        # 构建命令
        cmd = [sys.executable, "main.py"]

        if mode == "market":
            cmd.append("--market-review")
        elif mode == "stock" and stocks:
            cmd += ["--stocks", ",".join(stocks), "--no-market-review"]
        # mode == "full": 不加任何模式参数，使用 config/watchlist.json 或 STOCK_LIST

        if dry_run:
            cmd.append("--dry-run")
        if force_run:
            cmd.append("--force-run")
        if no_notify:
            cmd.append("--no-notify")

        # 构建环境变量（继承当前进程 + 读取 .env 文件）
        env = os.environ.copy()
        env["REPORT_TYPE"] = report_type
        env["PYTHONUNBUFFERED"] = "1"  # 确保实时输出日志
        # Windows 子进程默认 cp1252，写中文日志会 UnicodeEncodeError；强制 UTF-8
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # 如果 mode=stock 且有 stocks，也通过环境变量注入
        if mode == "stock" and stocks:
            env["STOCK_LIST"] = ",".join(stocks)

        # 读取项目 .env 文件（如果存在）
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in env:  # 不覆盖已有环境变量
                            env[k] = v
            except Exception:
                pass

        task_id = uuid.uuid4().hex[:12]
        with _tasks_lock:
            _tasks[task_id] = {
                "status": "pending",
                "logs": [],
                "report": "",
                "pid": None,
                "created_at": datetime.now(TZ_CN).isoformat(),
                "mode": mode,
                "stocks": stocks,
            }

        thread = threading.Thread(
            target=_run_analysis_task,
            args=(task_id, cmd, env),
            daemon=True,
        )
        thread.start()
        logger.info("任务 %s 已启动: mode=%s stocks=%s", task_id, mode, stocks)
        self._send_json(200, {"ok": True, "task_id": task_id})

    def _handle_sse(self, task_id: str):
        """Server-Sent Events：实时推送日志行。"""
        with _tasks_lock:
            if task_id not in _tasks:
                self._send_json(404, {"ok": False, "error": "task not found"})
                return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()

        sent = 0
        try:
            while True:
                with _tasks_lock:
                    task = _tasks.get(task_id, {})
                    logs = task.get("logs", [])
                    status = task.get("status", "pending")

                # 推送新日志行
                while sent < len(logs):
                    line = logs[sent].replace("\n", " ")
                    data = f"data: {json.dumps({'log': line, 'status': status}, ensure_ascii=False)}\n\n"
                    self.wfile.write(data.encode("utf-8"))
                    self.wfile.flush()
                    sent += 1

                # 任务结束则推送 done 事件并断开
                if status in ("done", "error"):
                    with _tasks_lock:
                        report = _tasks[task_id].get("report", "")
                        report_type = _tasks[task_id].get("report_type", "markdown")
                    done_data = json.dumps({
                        "status": status,
                        "has_report": bool(report),
                        "report_type": report_type,
                    }, ensure_ascii=False)
                    self.wfile.write(f"event: done\ndata: {done_data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break

                time.sleep(0.3)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端断开，正常退出

    def _handle_analyze(self, payload: dict):
        """
        深度双维度分析接口。

        payload:
          stock_code:  str
          stock_name:  str (可选，自动查询)
          dimensions:  list[str]  默认 ["technical","fundamental","industry"]
          modules:     dict       各维度启用的子模块
        """
        stock_code = payload.get("stock_code", "").strip().upper()
        if not stock_code:
            self._send_json(400, {"ok": False, "error": "stock_code 不能为空"})
            return

        stock_name  = payload.get("stock_name", stock_code)
        dimensions  = payload.get("dimensions", ["technical", "fundamental", "industry"])
        modules_map = payload.get("modules", {})
        llm_mode    = payload.get("llm_mode", "batch")       # batch | per_indicator
        open_report = payload.get("open_report", True)
        agent_review = bool(payload.get("agent_review", False))  # 🤖 Agent 综合研判（选项B）

        task_id = uuid.uuid4().hex[:12]
        with _tasks_lock:
            _tasks[task_id] = {
                "status":     "pending",
                "logs":       [],
                "report":     "",
                "report_type": "structured",   # 区别于 /run 的 markdown 报告
                "pid":        None,
                "created_at": datetime.now(TZ_CN).isoformat(),
                "stock_code": stock_code,
                "stock_name": stock_name,
                "task_kind":  "analyze",
            }

        thread = threading.Thread(
            target=_run_deep_analysis_task,
            args=(task_id, stock_code, stock_name, dimensions, modules_map, llm_mode, open_report, agent_review),
            daemon=True,
        )
        thread.start()
        logger.info("深度分析任务 %s 已启动: %s (%s)", task_id, stock_name, stock_code)
        self._send_json(200, {"ok": True, "task_id": task_id})

    def _handle_chat(self, payload: dict):
        """POST /chat — 交互式 AI 对话（选项A：走 AgentOrchestrator 四子 agent 多轮）。

        payload:
          message:     str   用户提问（必填）
          session_id:  str   前端生成并持久化（localStorage），用于多轮上下文
          stock_code:  str   可选，带上则注入股票上下文
          stock_name:  str   可选
        """
        message = (payload.get("message") or "").strip()
        if not message:
            self._send_json(400, {"ok": False, "error": "message 不能为空"})
            return

        session_id = (payload.get("session_id") or "").strip() or ("sess_" + uuid.uuid4().hex[:12])
        stock_code = (payload.get("stock_code") or "").strip().upper()
        stock_name = payload.get("stock_name", stock_code)

        task_id = uuid.uuid4().hex[:12]
        with _tasks_lock:
            _tasks[task_id] = {
                "status":      "pending",
                "logs":        [],
                "report":      "",
                "report_type": "chat",
                "pid":         None,
                "created_at":  datetime.now(TZ_CN).isoformat(),
                "session_id":  session_id,
                "task_kind":   "chat",
            }

        thread = threading.Thread(
            target=_run_chat_task,
            args=(task_id, message, session_id, stock_code, stock_name),
            daemon=True,
        )
        thread.start()
        logger.info("AI 对话任务 %s 已启动: session=%s", task_id, session_id)
        self._send_json(200, {"ok": True, "task_id": task_id, "session_id": session_id})

    def _handle_market_review(self, payload: dict):
        """POST /market_review — 大盘复盘（上证+创业板），生成一份 HTML 并打开。"""
        open_report = payload.get("open_report", True)
        task_id = uuid.uuid4().hex[:12]
        with _tasks_lock:
            _tasks[task_id] = {
                "status":      "pending",
                "logs":        [],
                "report":      "",
                "report_type": "market",
                "pid":         None,
                "created_at":  datetime.now(TZ_CN).isoformat(),
                "task_kind":   "market",
            }
        thread = threading.Thread(
            target=_run_market_review_task,
            args=(task_id, open_report),
            daemon=True,
        )
        thread.start()
        logger.info("大盘复盘任务 %s 已启动", task_id)
        self._send_json(200, {"ok": True, "task_id": task_id})

    def _handle_open_report(self, code: str):
        """GET /open_report?code=xxx — 按 code 找最新 HTML 报告并用浏览器打开。"""
        try:
            from portal.report_html import find_latest_stock_html, find_latest_market_html, open_in_browser
            if code == "__market__" or not code:
                path = find_latest_market_html()
            else:
                path = find_latest_stock_html(code.upper())
            if not path or not path.exists():
                self._send_json(404, {"ok": False, "error": "未找到该报告的 HTML，请重新分析生成"})
                return
            opened = open_in_browser(path)
            self._send_json(200, {"ok": True, "path": str(path), "opened": opened})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_get_env(self):
        """GET /env — 返回 .env 中已配置的白名单 key，value 用星号掩码（前3后3）。"""
        env_file = PROJECT_ROOT / ".env"
        configured = {}
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k in ALLOWED_ENV_KEYS and v:
                            if len(v) <= 6:
                                masked = "*" * len(v)
                            else:
                                masked = v[:3] + "*" * (len(v) - 6) + v[-3:]
                            configured[k] = masked
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
                return
        self._send_json(200, {"ok": True, "env": configured})

    def _handle_set_env(self, payload: dict):
        """POST /env — 写入白名单内的 key 到 .env 文件，保留注释，立即生效。"""
        updates = payload.get("env", {})
        if not isinstance(updates, dict):
            self._send_json(400, {"ok": False, "error": "env 字段必须是对象"})
            return

        # 只保留白名单内的 key
        safe_updates = {k: v for k, v in updates.items() if k in ALLOWED_ENV_KEYS}
        if not safe_updates:
            self._send_json(200, {"ok": True, "updated": []})
            return

        env_file = PROJECT_ROOT / ".env"
        # 读取现有内容
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()
        else:
            lines = []

        # 对每个要更新的 key，在已有行中替换，记录哪些已更新
        updated_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _, _ = stripped.partition("=")
                k = k.strip()
                if k in safe_updates:
                    new_lines.append(f'{k}={safe_updates[k]}')
                    updated_keys.add(k)
                    continue
            new_lines.append(line)

        # 追加尚未出现的 key
        for k, v in safe_updates.items():
            if k not in updated_keys:
                new_lines.append(f'{k}={v}')

        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        # 立即生效：更新当前进程环境变量
        _load_dotenv()
        # 强制覆盖（_load_dotenv 不覆盖已有值，新写入的要强制更新）
        for k, v in safe_updates.items():
            os.environ[k] = v

        logger.info("✅ .env 已更新，keys: %s", list(safe_updates.keys()))
        self._send_json(200, {"ok": True, "updated": list(safe_updates.keys())})

    def _handle_quote(self, params: dict):
        """GET /quote?codes=600519,300750 — 返回股票实时行情（名称、现价、涨跌幅）。"""
        codes_raw = params.get("codes", [""])[0]
        codes = [c.strip() for c in codes_raw.split(",") if c.strip()]
        if not codes:
            self._send_json(400, {"ok": False, "error": "缺少 codes 参数"})
            return

        result = []

        # ── 优先：efinance get_base_info（内网稳定，返回名称+行业）──
        # 东财 spot（akshare stock_zh_a_spot_em）在部分内网返回乱码/不通，
        # efinance 个股基本信息接口更稳，用于可靠拿到股票名称。
        try:
            import efinance as ef
            ef_ok = False
            for code in codes:
                name = None; price = None; pct = None
                try:
                    info = ef.stock.get_base_info(code)
                    # 单只返回 Series，可取"股票名称"
                    if info is not None and hasattr(info, "get"):
                        nm = info.get("股票名称")
                        if nm and str(nm) not in ("nan", "-", ""):
                            name = str(nm); ef_ok = True
                except Exception:
                    pass
                result.append({"code": code, "name": name, "price": price,
                               "pct_chg": pct, "volume": None})
            if ef_ok:
                self._send_json(200, {"ok": True, "quotes": result})
                return
            result = []  # efinance 全空，落到 akshare 分支
        except Exception as e:
            logger.info("efinance 查名不可用，降级 akshare: %s", str(e)[:60])
            result = []

        # ── 次选：akshare 全市场实时行情（东财 spot）────────────
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            # 列名示例：代码 名称 最新价 涨跌幅 成交量 ...
            col_map = {
                "代码":   "code",
                "名称":   "name",
                "最新价": "price",
                "涨跌幅": "pct_chg",
                "成交量": "volume",
            }
            df = df.rename(columns=col_map)
            df["code"] = df["code"].astype(str).str.strip()
            df_idx = df.set_index("code")

            for code in codes:
                if code in df_idx.index:
                    row = df_idx.loc[code]
                    def _safe(v):
                        try:
                            f = float(v)
                            return None if (f != f) else f  # NaN → None
                        except Exception:
                            return None
                    result.append({
                        "code":    code,
                        "name":    str(row.get("name", code)),
                        "price":   _safe(row.get("price")),
                        "pct_chg": _safe(row.get("pct_chg")),
                        "volume":  _safe(row.get("volume")),
                    })
                else:
                    result.append({"code": code, "name": None, "price": None, "pct_chg": None, "volume": None})

            self._send_json(200, {"ok": True, "quotes": result})
            return

        except Exception as e:
            logger.warning("akshare 获取行情失败，降级读取缓存: %s", e)

        # ── 降级：从 data_cache meta.json 读取 name ───────────
        try:
            from portal.data_cache import StockDataCache
            cache = StockDataCache()
        except Exception:
            cache = None

        for code in codes:
            name = None
            if cache:
                try:
                    meta = cache.get_meta(code)
                    if meta:
                        name = meta.get("name")
                except Exception:
                    pass
            result.append({"code": code, "name": name, "price": None, "pct_chg": None, "volume": None})

        self._send_json(200, {"ok": True, "quotes": result})

    def _handle_backtest(self, code: str):
        """GET /backtest?code=xxx — 对指定股票的K线缓存运行回测，返回信号胜率统计。"""
        if not code:
            self._send_json(400, {"ok": False, "error": "缺少 code 参数"})
            return
        try:
            from portal.data_cache import StockDataCache
            df = StockDataCache().get_kline(code)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"读取缓存失败：{e}"})
            return

        if df is None or df.empty:
            self._send_json(404, {
                "ok": False,
                "error": "请先对该股票执行一次深度分析以建立K线缓存"
            })
            return

        try:
            from portal.backtester import run_backtest
            result = run_backtest(df)
        except Exception as e:
            logger.exception("backtest error for %s: %s", code, e)
            self._send_json(500, {"ok": False, "error": f"回测执行失败：{e}"})
            return

        # 保存到缓存，供走势预测等模块复用
        try:
            from portal.data_cache import StockDataCache
            StockDataCache().save_backtest(code, result)
        except Exception as e:
            logger.warning("保存回测缓存失败 %s: %s", code, e)

        self._send_json(200, {"ok": True, "code": code, "backtest": result})
