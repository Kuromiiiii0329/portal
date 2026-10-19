"""
portal/srv/state.py
全局任务状态注册表 —— task_id → 任务字典，及其保护锁。

所有后台任务函数（tasks.py）和 HTTP 层（http_handler.py）共享这两个对象，
故独立成模块避免循环依赖。语义与原 server.py 的 _tasks / _tasks_lock 完全一致。
"""
from __future__ import annotations

import threading

# task_id -> {"status": "running"|"done"|"error", "logs": [...], "report": str, "started_at": ...}
_tasks: dict = {}
_tasks_lock = threading.Lock()
