# -*- coding: utf-8 -*-
"""
portal/premarket/factors/base.py — 因子库基础设施

设计原则
--------
1. **一个因子一个文件**（`f_*.py`），加/删因子只动那个文件，不改别处。
2. **因子只算数，不做判断**：`compute(panel) -> {code: value}`。
   买点判断属于「战法」（`playbooks.py`），战法是因子的布尔组合。
3. **发现型因子只能写表达式，不能写代码**：`EXPR` 由受限 AST 求值器执行，
   没有 import / open / eval / 属性访问 —— 因为未来这些因子由 LLM 生成。
4. **能被验证才有价值**：`validate.py` 用滚动截面的信息系数（IC）检验因子，
   而不是"看着有道理"。

Panel
-----
一次采集 → 一次构造成 `Panel` → 所有因子共享，避免每个因子各自查库。
"""
from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from typing import Any, Callable

#: 战法/因子条件里允许出现的比较与算术节点（白名单，其余一律拒绝）
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.Mod, ast.Pow, ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Eq, ast.NotEq, ast.Name, ast.Load, ast.Constant, ast.IfExp,
    ast.Call,   # 只放行下面 _SAFE_FUNCS 里的函数
)


class FactorError(ValueError):
    """因子定义非法（元数据缺失、表达式越界等）。"""


@dataclass
class Panel:
    """因子计算的输入：今日截面 + 近 N 日历史。"""

    asof: str                                   # 今日 ISO 日期
    close: dict[str, float] = field(default_factory=dict)
    prev_close: dict[str, float] = field(default_factory=dict)
    chg: dict[str, float] = field(default_factory=dict)
    volume: dict[str, float] = field(default_factory=dict)
    amount_yi: dict[str, float] = field(default_factory=dict)
    #: 当日开/高/低（来自实时行情）。收盘位置、跳空这类因子要用。
    open_today: dict[str, float] = field(default_factory=dict)
    high_today: dict[str, float] = field(default_factory=dict)
    low_today: dict[str, float] = field(default_factory=dict)
    limit_up: dict[str, float] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    hist: dict[str, dict] = field(default_factory=dict)
    limit_pct: dict[str, float] = field(default_factory=dict)

    def codes(self) -> list[str]:
        return [c for c in self.close if c in self.prev_close]

    def closes(self, code: str) -> list[float]:
        return [x for x in (self.hist.get(code) or {}).get("close", []) if x]

    def highs(self, code: str) -> list[float]:
        return [x for x in (self.hist.get(code) or {}).get("high", []) if x]

    def lows(self, code: str) -> list[float]:
        return [x for x in (self.hist.get(code) or {}).get("low", []) if x]

    def vols(self, code: str) -> list[float]:
        return [x for x in (self.hist.get(code) or {}).get("vol", []) if x]

    def hist_days(self, code: str) -> int:
        return len(self.closes(code))

    def series_with_today(self, code: str) -> list[float]:
        """历史收盘 + 今日收盘（算均线用，含今日）。"""
        c = self.closes(code)
        px = self.close.get(code)
        return c + ([px] if px else [])


# ── 通用计算助手（因子文件里复用）─────────────────────────────
def ma(px: list[float], n: int) -> float | None:
    if len(px) < n:
        return None
    return sum(px[-n:]) / n


def pct(a: float | None, b: float | None) -> float | None:
    if not a or not b or b == 0:
        return None
    return (a / b - 1) * 100


def safe(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


_SAFE_FUNCS: dict[str, Callable] = {
    "abs": abs, "min": min, "max": max, "round": round,
    "int": int, "float": float, "bool": bool,
}


class _Guard(ast.NodeVisitor):
    """拒绝白名单之外的语法（属性访问、下标、推导式、lambda 等）。

    注意：必须重写 `generic_visit` 而不是 `visit` —— `NodeVisitor.visit` 会按
    节点类型再派发回 `visit_XXX`，如果在 `visit_XXX` 里调 `super().visit()` 就会
    无限递归（第一版就是这么炸的，RecursionError）。
    """

    def generic_visit(self, node):
        if not isinstance(node, _ALLOWED_NODES):
            raise FactorError(f"表达式里不允许 {type(node).__name__}")
        super().generic_visit(node)

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCS:
            raise FactorError(f"不允许调用 {ast.dump(node.func)[:40]}")
        if node.keywords:
            raise FactorError("不允许关键字参数")
        self.generic_visit(node)


def compile_expr(expr: str, *, allowed_names: set[str]) -> ast.Expression:
    """把条件/因子表达式编译成 AST，并校验名字与语法。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FactorError(f"表达式语法错误：{exc.msg}（{expr!r}）") from exc
    _Guard().visit(tree)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    unknown = names - allowed_names - set(_SAFE_FUNCS)
    if unknown:
        raise FactorError(f"表达式引用了未知字段：{sorted(unknown)}")
    return tree


def eval_expr(tree: ast.Expression, env: dict[str, Any]) -> Any:
    """求值（env 已经过滤过名字，且 AST 已过白名单）。"""
    return eval(compile(tree, "<factor>", "eval"), {"__builtins__": {}}, env)


# ── 因子定义 ──────────────────────────────────────────────────
REQUIRED_META = ("id", "name", "kind", "description")


@dataclass
class Factor:
    """一个因子。`compute` 为内置因子；`expr` 为发现型因子（沙箱求值）。"""

    meta: dict[str, Any]
    compute: Callable[[Panel], dict[str, Any]] | None = None
    expr: str | None = None

    def __post_init__(self) -> None:
        for k in REQUIRED_META:
            if not self.meta.get(k):
                raise FactorError(f"因子缺少元数据 {k}: {self.meta}")
        if self.meta["kind"] not in ("numeric", "bool"):
            raise FactorError(f"kind 只能是 numeric/bool：{self.meta}")
        if not self.compute and not self.expr:
            raise FactorError(f"因子 {self.meta['id']} 既没有 compute 也没有 expr")

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def kind(self) -> str:
        return self.meta["kind"]

    def run(self, panel: Panel, *, env: dict[str, dict[str, Any]] | None = None
            ) -> dict[str, Any]:
        """算一个因子。

        - 代码因子：直接调 `compute(panel)`
        - 表达式因子（A 类）：逐只股票求值，可用名字 = 其它因子的值（env）+
          当日标量（close / prev_close / chg_today / volume / amount_yi /
          limit_up / open / high / low）
        """
        if self.compute:
            return self.compute(panel)
        if not self.expr:
            raise FactorError(f"因子 {self.id} 既没有 compute 也没有 expr")

        scalars = {
            "close": panel.close, "prev_close": panel.prev_close,
            "chg_today": panel.chg, "volume": panel.volume,
            "amount_yi": panel.amount_yi, "limit_up": panel.limit_up,
            "open": panel.open_today, "high": panel.high_today,
            "low": panel.low_today,
        }
        env = env or {}
        tree = compile_expr(self.expr, allowed_names=set(env) | set(scalars))

        out: dict[str, Any] = {}
        for code in panel.codes():
            row: dict[str, Any] = {}
            for k, series in scalars.items():
                if code in series:
                    row[k] = series[code]
            for k, series in env.items():
                if code in series:
                    row[k] = series[code]
            if "close" not in row:
                continue
            try:
                v = eval_expr(tree, row)
            except Exception:
                continue
            v = safe(v)
            if v is not None:
                out[code] = round(v, 4)
        return out


def make_factor(meta: dict[str, Any], compute=None, expr: str | None = None) -> Factor:
    return Factor(meta=dict(meta), compute=compute, expr=expr)


__all__ = ["Panel", "Factor", "FactorError", "make_factor",
           "ma", "pct", "safe", "compile_expr", "eval_expr",
           "REQUIRED_META"]
