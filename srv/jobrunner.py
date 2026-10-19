# -*- coding: utf-8 -*-
"""
portal/srv/jobrunner.py — 通用的「后台子进程 + 进度轮询」执行器

为什么用子进程而不是线程
----------------------
两类任务都会踩到同一个坑，只是表现不同：

- `marketdb update` 会建 ProcessPoolExecutor，而 Windows 的 spawn 会重新 import
  `__main__`；在 HTTP 服务进程里混进程池，出问题时现象是「进程池被整体终止」。
- `premarket generate` 会调 LLM、写文件、可能跑几十秒到几分钟；直接用线程会把
  HTTP 请求挂死，而且崩了没法看。

子进程隔离后：`python -m portal.xxx` 自带 `__main__` 保护，stdout 天然是进度流，
退出码天然是成败。清理也简单（杀进程树）。

约定
----
一个名字对应一个槽位（`JobRunner("update")`）。同一个槽位同时只跑一个任务，
第二个启动请求直接拒绝 —— 并发写同一个数据文件是最容易产生「看起来正常」的脏数据。
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterable

#: 保留多少行日志
MAX_LINES = 400


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class JobRunner:
    """单槽位后台 CLI 任务。线程只负责读 stdout，不承担业务逻辑。"""

    def __init__(self, name: str, *, root: Path):
        self.name = name
        self.root = Path(root)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "running": False, "kind": None, "started_at": None,
            "finished_at": None, "lines": [], "exit": None,
            "stats": None, "error": None, "argv": None,
        }

    # ── 启动 ────────────────────────────────────────────
    def start(self, kind: str, args: Iterable[str], *,
              note: str = "") -> dict[str, Any]:
        """启动 `python -u -m <args...>`。已有任务在跑则拒绝。"""
        argv = [sys.executable, "-u", *args]
        with self._lock:
            if self._state["running"]:
                return {"ok": False,
                        "error": f"{self.name}: 已有任务在进行，请等它结束",
                        "hint": f"开始于 {self._state['started_at']}"}
            self._state.update({
                "running": True, "kind": kind, "started_at": _now(),
                "finished_at": None, "lines": [], "exit": None,
                "stats": None, "error": None, "argv": argv,
            })
            threading.Thread(target=self._run, args=(argv,), daemon=True).start()
            out = {"ok": True, "kind": kind,
                   "started_at": self._state["started_at"]}
        if note:
            out["note"] = note
        return out

    def _run(self, argv: list[str]) -> None:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            proc = subprocess.Popen(
                argv, cwd=str(self.root), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                with self._lock:
                    self._state["lines"].append(line)
                    if len(self._state["lines"]) > MAX_LINES:
                        del self._state["lines"][:-MAX_LINES]
            code = proc.wait()
            with self._lock:
                self._state["exit"] = code
                self._state["stats"] = self._last_json()
                if code != 0:
                    self._state["error"] = (
                        (self._state["stats"] or {}).get("error")
                        or f"退出码 {code}：" + " / ".join(self._state["lines"][-3:]))
        except Exception as exc:
            with self._lock:
                self._state["error"] = f"{type(exc).__name__}: {exc}"
                self._state["exit"] = -1
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["finished_at"] = _now()

    def _last_json(self) -> dict | None:
        for line in reversed(self._state["lines"]):
            s = line.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue
        return None

    # ── 查询 ────────────────────────────────────────────
    def status(self, *, tail: int = 40) -> dict[str, Any]:
        with self._lock:
            st = dict(self._state)
            st["lines"] = list(self._state["lines"])[-max(1, min(int(tail), MAX_LINES)):]
        st["ok"] = True
        st["name"] = self.name
        return st

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._state["running"])

    def reset(self) -> None:
        """仅用于测试：把状态清成空闲。"""
        with self._lock:
            self._state.update({"running": False, "kind": None, "lines": [],
                                "exit": None, "stats": None, "error": None})


__all__ = ["JobRunner", "MAX_LINES"]
