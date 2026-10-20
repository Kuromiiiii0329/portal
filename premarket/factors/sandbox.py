# -*- coding: utf-8 -*-
"""
portal/premarket/factors/sandbox.py — 因子代码的沙箱

两级安全
--------
**A 类（表达式因子）**：只写 `EXPR`，由 `base.compile_expr` 白名单求值。
   能引用的是「已有因子的值 + 当日行情标量」，天生无副作用。

**B 类（代码因子）**：写 `def compute(panel)`。这里加两道：

1. **语法白名单**（`validate_code`）—— 不是黑名单，只放行算术/比较/循环/条件/返回、
   字典与列表操作、少量白名单方法（`get/items/keys/values/append/...`）。
   禁止：`import`、`def` 之外的定义、`class`、`global/nonlocal`、`with`、`try`、
   `lambda`、`yield`、`async`、任何以 `_` 开头的属性或名字（挡掉 `__class__`
   这类逃逸入口）、以及 `eval/exec/open/getattr/setattr/vars/globals/compile`
   等危险内建。
2. **子进程 + 超时**（`run_in_subprocess`）—— 死循环、巨量内存、崩溃都只影响子进程。
   子进程自己也只拿到 800 只股票的样本，跑不出全网级的开销。

为什么允许 B 类：表达式表达力太弱，写不出"过去 20 日里收盘在箱体上沿的次数"这种
需要循环的逻辑。但代价是必须**先审后跑**，所以两道闸缺一不可。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

#: 允许的语句节点
_ALLOWED_STMT = (
    ast.Module, ast.FunctionDef, ast.Return, ast.Assign, ast.AugAssign,
    ast.AnnAssign, ast.For, ast.If, ast.While, ast.Break, ast.Continue, ast.Pass,
    ast.Expr, ast.Global,  # Global 在下面单独再禁掉
)
#: 允许的表达式节点
_ALLOWED_EXPR = (
    ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Pow, ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.IfExp, ast.Name, ast.Load,
    ast.Store, ast.Constant, ast.Call, ast.Attribute, ast.Subscript, ast.Tuple,
    ast.List, ast.Dict, ast.Set, ast.Slice, ast.comprehension, ast.ListComp,
    ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.Starred,
    # 函数签名本身要用到的节点（少了这些，任何 def 都会被误判）
    ast.arguments, ast.arg,
)
#: 允许调用的名字（内建）
_ALLOWED_CALLS = {
    "abs", "min", "max", "round", "int", "float", "bool", "len", "sum",
    "sorted", "enumerate", "range", "zip", "list", "dict", "set", "tuple",
    "any", "all", "str",
}
#: 允许的方法名（挡住所有以 _ 开头的）
_ALLOWED_METHODS = {
    "get", "items", "keys", "values", "append", "extend", "sort", "split",
    "strip", "lstrip", "rstrip", "lower", "upper", "startswith", "endswith",
    "join", "count", "index", "copy", "pop", "add", "update", "reverse",
    # Panel 的只读方法（因子代码里必须能用）
    "codes", "closes", "highs", "lows", "vols", "hist_days",
    "series_with_today",
}
#: Panel 的只读字段（允许以属性方式读取，例如 panel.close）
_PANEL_FIELDS = {
    "close", "prev_close", "chg", "volume", "amount_yi", "limit_up",
    "open_today", "high_today", "low_today", "names", "hist", "limit_pct",
    "asof",
}
#: 允许出现的属性名 = 白名单方法 + Panel 字段
_ALLOWED_ATTRS = _ALLOWED_METHODS | _PANEL_FIELDS

#: 明确禁止的内建（即使出现在 _ALLOWED_CALLS 之外也要给出可读错误）
_BANNED_NAMES = {
    "eval", "exec", "compile", "open", "input", "getattr", "setattr",
    "delattr", "vars", "globals", "locals", "__import__", "breakpoint",
    "memoryview", "object", "type", "super", "help", "exit", "quit",
}


class SandboxError(ValueError):
    """因子代码未通过语法审查。"""


def validate_code(src: str, *, require_compute: bool = True) -> dict[str, Any]:
    """审查因子代码。返回摘要（函数名、行数）；不通过抛 SandboxError。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise SandboxError(f"语法错误：{exc.msg}（第 {exc.lineno} 行）") from exc

    def bad(node, why: str):
        raise SandboxError(f"第 {getattr(node, 'lineno', '?')} 行：{why}")

    has_compute = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bad(node, "不允许 global / nonlocal")
        if isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom):
            bad(node, "不允许 import")
        if isinstance(node, ast.ClassDef):
            bad(node, "不允许定义 class")
        if isinstance(node, (ast.With, ast.AsyncWith, ast.Try, ast.Raise,
                            ast.Lambda, ast.Yield, ast.YieldFrom, ast.Await,
                            ast.Delete, ast.Assert, ast.While)):
            bad(node, f"不允许 {type(node).__name__}")
        if isinstance(node, (ast.AsyncFunctionDef, ast.AsyncFor)):
            bad(node, "不允许 async")
        if isinstance(node, ast.FunctionDef):
            if node.name.startswith("_"):
                bad(node, "函数名不能以 _ 开头")
            if node.name == "compute":
                has_compute = True
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                bad(node, f"属性 {node.attr} 以 _ 开头")
            if node.attr not in _ALLOWED_ATTRS:
                bad(node, f"属性 {node.attr} 不在白名单（允许："
                         f"{', '.join(sorted(_ALLOWED_METHODS)[:8])}…）")
        if isinstance(node, ast.Name):
            if node.id.startswith("__"):
                bad(node, f"名字 {node.id} 是双下划线")
            if node.id in _BANNED_NAMES:
                bad(node, f"禁止使用 {node.id}")
            if isinstance(node.ctx, ast.Store) and node.id in _BANNED_NAMES:
                bad(node, f"禁止覆盖 {node.id}")
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                if fn.id not in _ALLOWED_CALLS:
                    bad(node, f"不允许调用 {fn.id}")
            elif isinstance(fn, ast.Attribute):
                if fn.attr not in _ALLOWED_METHODS:
                    bad(node, f"不允许调用方法 {fn.attr}")
            else:
                bad(node, "只允许调用具名函数或白名单方法")
        if not isinstance(node, _ALLOWED_STMT + _ALLOWED_EXPR + (ast.operator,
                                                                 ast.boolop,
                                                                 ast.cmpop,
                                                                 ast.unaryop,
                                                                 ast.expr_context)):
            bad(node, f"不允许的语法 {type(node).__name__}")
    if require_compute and not has_compute:
        raise SandboxError("缺少 compute(panel) 函数")
    return {"ok": True, "lines": len(src.splitlines()),
            "functions": [n.name for n in ast.walk(tree)
                          if isinstance(n, ast.FunctionDef)]}


# ─────────────────────────────────────────────────────────────
# 子进程执行（超时隔离）
# ─────────────────────────────────────────────────────────────
_RUNNER = r'''
import json, sys, importlib.util
payload = json.loads(sys.stdin.read())
sys.path.insert(0, payload["root"])
spec = importlib.util.spec_from_file_location("cand", payload["src_path"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from portal.premarket.factors import build_panel
from portal.virtual import marketdb as M
conn = M.connect()
quotes = payload["quotes"]
panel = build_panel(conn, quotes, payload["asof"], history_days=40)
out = mod.compute(panel)
clean = {}
for k, v in (out or {}).items():
    if isinstance(v, bool) or isinstance(v, (int, float)):
        clean[str(k)] = v
print(json.dumps({"ok": True, "n": len(clean), "values": clean}))
'''


def run_in_subprocess(src_path: str, *, root: str, quotes: list[dict],
                      asof: str, timeout: float = 45.0) -> dict[str, Any]:
    """在子进程里跑候选因子的 compute()。超时/崩溃都返回 ok=False，不抛出。"""
    payload = json.dumps({"root": root, "src_path": src_path,
                          "quotes": quotes, "asof": asof})
    with tempfile.TemporaryDirectory() as td:
        runner = Path(td) / "_runner.py"
        runner.write_text(_RUNNER, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(runner)], input=payload,
                capture_output=True, text=True, timeout=timeout,
                cwd=root)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"执行超时（>{timeout:.0f}s），已终止"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return {"ok": False, "error": "子进程失败：" + " | ".join(tail)[:400]}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"ok": False, "error": f"输出无法解析：{exc}"}


__all__ = ["validate_code", "run_in_subprocess", "SandboxError",
           "_ALLOWED_CALLS", "_ALLOWED_METHODS"]
