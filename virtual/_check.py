# -*- coding: utf-8 -*-
"""
portal/virtual/_check.py — 自检断言工具（P0 的 backtest_audit 与 P1/P2 的自检共用）

一个判据如果「永远不会失败」，它就不是判据。
所以 `Check.raises()` 与 `Check.expect()` 的每一次使用，都必须能塞一个已知错误进去
并确认它真的会报错（对应《回测避坑手册》§5.2 / §5.6 的两条：对照验证、探针不能是坏的）。
"""
from __future__ import annotations

from typing import Any


class Check:
    """一条检查。`passed` 为真才算过；detail 无论成败都记录。"""

    def __init__(self, cid: str, title: str):
        self.cid = cid
        self.title = title
        self.passed = True
        self.lines: list[str] = []

    def expect(self, cond: bool, msg: str) -> "Check":
        if not cond:
            self.passed = False
            self.lines.append(f"   ✗ {msg}")
        else:
            self.lines.append(f"   ✓ {msg}")
        return self

    def note(self, msg: str) -> "Check":
        self.lines.append(f"   · {msg}")
        return self

    def raises(self, exc_types, fn, msg: str) -> "Check":
        """断言某个调用必须抛指定异常（证明校验真的在拦）。"""
        try:
            fn()
        except exc_types:
            return self.expect(True, msg)
        except Exception as exc:  # 抛了别的异常也算失败：说明拦错了地方
            return self.expect(False, f"{msg}（抛的是 {type(exc).__name__}: {exc}）")
        return self.expect(False, f"{msg}（没有抛异常，校验形同虚设）")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.cid, "title": self.title,
                "passed": self.passed, "detail": self.lines}


def close_enough(a: float | None, b: float | None, tol: float = 1e-9) -> bool:
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= tol


__all__ = ["Check", "close_enough"]
