# -*- coding: utf-8 -*-
"""
portal/premarket/env.py — 加载 .env（与 portal 的 `/env` 端点同源）

为什么需要它
-----------
盘前预案既可以从 HTTP 服务里跑（子进程继承服务进程环境），也可以直接从命令行跑。
命令行跑的时候进程环境是空的，而 key 只存在于 `.env` 文件里 —— 所以必须自己加载，
否则 CLI 永远走「没有 LLM」的兜底路径。

查找顺序（**先找到先用，不覆盖已有的进程环境变量**）
--------------------------------------------------
1. 进程环境变量（最高优先，CI 里用 secrets 注入走这条）
2. `PORTAL_DIR.parent / ".env"`  ← portal 的 `/env` 端点写的就是这里
3. `PORTAL_DIR / ".env"`         ← 仓库内的本地副本（已被 .gitignore 忽略）

安全约定
--------
本模块**只读取、不打印**任何值。日志里最多出现 key 的**名字**，
一旦有人不小心把值打进日志，就等于泄漏。
"""
from __future__ import annotations

import os
from pathlib import Path

PORTAL_DIR = Path(__file__).resolve().parent.parent


def env_files() -> list[Path]:
    """按优先级返回候选 .env 路径。"""
    return [PORTAL_DIR.parent / ".env", PORTAL_DIR / ".env"]


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE。跳过注释与空行，去掉成对引号。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load_env(*, override: bool = False) -> dict[str, str]:
    """加载所有候选 .env 到 `os.environ`，返回**实际被采用**的键值对。

    默认不覆盖已存在的进程环境变量（CI 的 secrets 优先）。
    """
    applied: dict[str, str] = {}
    for p in env_files():
        if not p.exists():
            continue
        for k, v in parse_env_file(p).items():
            if not override and os.environ.get(k):
                continue
            os.environ[k] = v
            applied[k] = v
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    return applied


def has_key(*names: str) -> bool:
    """检查任一 key 是否存在（只看存在性，不返回值）。"""
    return any(bool(os.environ.get(n)) for n in names)


def mask(value: str | None) -> str:
    """给日志用的脱敏显示：只留前 6 位与长度。"""
    if not value:
        return "(未设置)"
    return f"{value[:6]}…（长度 {len(value)}）"


__all__ = ["load_env", "env_files", "parse_env_file", "has_key", "mask", "PORTAL_DIR"]
