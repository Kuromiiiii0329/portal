# -*- coding: utf-8 -*-
"""
portal/virtual/expr.py — 规则表达式求值器（安全子集，禁止 eval）

为什么不用 eval：`rules.jsonl` 里会有 LLM 起草、人工批准的规则。任何能执行任意
代码的求值器都是一个「提权接口」—— 一条规则就能把整个进程读写权限交出去。
所以这里只支持一棵**白名单语法树**。

为什么规则必须是结构化表达式而不是散文（来自 SKILL.md §9.7.3 的实测事故）：

    10/8 冻结的 plan 里 `exit_rule` 只写了人类可读的 `layers` 文本，漏写了机器
    可读开关 `z1` / `price_basis` / `use_stop` → 引擎全走默认值，结果
    「文本说 Z1 适用、引擎 Z1 关闭」，二者方向相反。

所以：**文本与代码冲突时，以这里能求值的 `expr` 为准。** `desc_human` 只是给人看的。

语法
----
分组：
    {"all": [子表达式, ...]}     全部成立
    {"any": [子表达式, ...]}     任一成立
    {"not": 子表达式}           取反

叶子：
    {"field": "zone_pos_120", "op": "<",   "value": 0.30}
    {"field": "lbc",          "op": "between", "value": [1, 2]}   # 闭区间
    {"field": "hot_reason",   "op": "is_null"}                    # 不需要 value

算子白名单：
    数值  <  <=  >  >=  ==  !=
    集合  in  not_in          （value 可以是标量或列表）
    区间  between             （value = [lo, hi]，闭区间）
    空值  is_null  not_null

缺失字段语义（"宁 null 不猜"）
------------------------------
字段缺失 **不会** 被当成 0 或 False 蒙混过关：默认判为「不满足」，
并在 `evaluate_traced()` 的 reasons 里明确写出 `missing:<字段名>`。
要让线上规则在缺数据时大声失败，用 `evaluate(..., missing="raise")`。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

MAX_DEPTH = 12
MAX_LEAVES = 200

GROUP_KEYS = ("all", "any", "not")
OPS = {
    "<", "<=", ">", ">=", "==", "!=",
    "in", "not_in", "between", "is_null", "not_null",
}
_NULL_OPS = {"is_null", "not_null"}


class ExprError(ValueError):
    """表达式非法（结构错误 / 未知算子 / 超限）。"""


# ─────────────────────────────────────────────────────────────
# 校验
# ─────────────────────────────────────────────────────────────
def validate(expr: Any, *, path: str = "expr") -> list[str]:
    """返回问题清单；空列表 = 合法。递归校验，带路径便于定位。"""
    problems: list[str] = []
    _validate(expr, path, 0, problems, [0])
    return problems


def _validate(expr: Any, path: str, depth: int, problems: list[str], leaves: list[int]) -> None:
    if depth > MAX_DEPTH:
        problems.append(f"{path}: 嵌套深度超过 {MAX_DEPTH}")
        return
    if not isinstance(expr, Mapping):
        problems.append(f"{path}: 必须是对象，实际是 {type(expr).__name__}")
        return

    keys = set(expr.keys())
    groups = keys & set(GROUP_KEYS)

    if groups:
        if len(groups) > 1:
            problems.append(f"{path}: 同时出现多个分组键 {sorted(groups)}")
            return
        g = next(iter(groups))
        extra = keys - {g}
        if extra:
            problems.append(f"{path}: 分组 {g} 下不允许多余键 {sorted(extra)}")
        if g == "not":
            if "not" not in expr:
                problems.append(f"{path}: not 缺少子表达式")
            else:
                _validate(expr["not"], f"{path}.not", depth + 1, problems, leaves)
        else:
            items = expr[g]
            if not isinstance(items, list) or not items:
                problems.append(f"{path}.{g}: 必须是非空列表")
                return
            for i, sub in enumerate(items):
                _validate(sub, f"{path}.{g}[{i}]", depth + 1, problems, leaves)
        return

    # ── 叶子
    if "field" not in keys:
        problems.append(f"{path}: 叶子缺少 field（也不是分组节点）")
        return
    unknown = keys - {"field", "op", "value"}
    if unknown:
        problems.append(f"{path}: 叶子不允许多余键 {sorted(unknown)}")
    field = expr.get("field")
    if not isinstance(field, str) or not field.strip():
        problems.append(f"{path}.field: 必须是非空字符串")
    op = expr.get("op")
    if op not in OPS:
        problems.append(f"{path}.op: 未知算子 {op!r}（允许 {sorted(OPS)}）")
        return
    if op in _NULL_OPS:
        if "value" in keys:
            problems.append(f"{path}: 算子 {op} 不接受 value")
    else:
        if "value" not in keys:
            problems.append(f"{path}: 算子 {op} 需要 value")
        elif op == "between":
            v = expr.get("value")
            if (not isinstance(v, (list, tuple)) or len(v) != 2
                    or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
                problems.append(f"{path}.value: between 需要 [lo, hi] 两个数字")

    leaves[0] += 1
    if leaves[0] > MAX_LEAVES:
        problems.append(f"{path}: 叶子数超过 {MAX_LEAVES}")


def is_valid(expr: Any) -> bool:
    return not validate(expr)


# ─────────────────────────────────────────────────────────────
# 求值
# ─────────────────────────────────────────────────────────────
def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _cmp(op: str, left: Any, right: Any) -> bool | None:
    """返回 True/False；类型不可比时返回 None（调用方决定如何处置）。"""
    if op in ("<", "<=", ">", ">="):
        if not (_num(left) and _num(right)):
            return None
        return {"<": left < right, "<=": left <= right,
                ">": left > right, ">=": left >= right}[op]
    if op in ("==", "!="):
        if _num(left) and _num(right):
            eq = float(left) == float(right)
        elif isinstance(left, str) and isinstance(right, str):
            eq = left == right
        elif isinstance(left, bool) and isinstance(right, bool):
            eq = left is right
        elif left is None or right is None:
            eq = left is right
        else:
            return None
        return eq if op == "==" else not eq
    if op in ("in", "not_in"):
        if isinstance(right, (list, tuple, set)):
            hit = left in right
        else:
            hit = left == right
        return hit if op == "in" else not hit
    if op == "between":
        if not (_num(left) and isinstance(right, (list, tuple)) and len(right) == 2):
            return None
        return right[0] <= left <= right[1]
    return None


def evaluate_traced(expr: Any, row: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """求值并返回 (是否满足, 原因列表)。原因只在不满足时非空。"""
    problems = validate(expr)
    if problems:
        raise ExprError("表达式非法：" + "；".join(problems))
    reasons: list[str] = []
    ok = _eval(expr, row, reasons)
    return ok, ([] if ok else reasons)


def evaluate(expr: Any, row: Mapping[str, Any], *, missing: str = "false") -> bool:
    """求值。`missing="raise"` 时，字段缺失直接抛错而不是静默判 False。"""
    ok, reasons = evaluate_traced(expr, row)
    if missing == "raise":
        miss = [r for r in reasons if r.startswith("missing:")]
        if miss:
            raise ExprError("字段缺失：" + ", ".join(m[8:] for m in miss))
    return ok


def _eval(expr: Any, row: Mapping[str, Any], reasons: list[str]) -> bool:
    if "all" in expr:
        results = [_eval(sub, row, reasons) for sub in expr["all"]]
        if not all(results):
            failed = [s for s, r in zip(expr["all"], results) if not r]
            if failed:
                reasons.append(f"all 中 {len(failed)}/{len(results)} 条不满足")
        return all(results)

    if "any" in expr:
        results = [_eval(sub, row, reasons) for sub in expr["any"]]
        if not any(results):
            reasons.append(f"any 中 0/{len(results)} 条满足")
        return any(results)

    if "not" in expr:
        before = len(reasons)
        inner = _eval(expr["not"], row, reasons)
        if not inner:
            # not 成功时，内层失败原因不该出现在结果里
            del reasons[before:]
        return not inner

    field = expr["field"]
    op = expr["op"]

    if op in _NULL_OPS:
        value = row.get(field, None)
        present = (field in row) and (value is not None)
        if op == "is_null":
            return value is None
        return present

    if field not in row:
        reasons.append(f"missing:{field}")
        return False
    left = row.get(field)
    if left is None:
        reasons.append(f"missing:{field}")
        return False

    result = _cmp(op, left, expr.get("value"))
    if result is None:
        reasons.append(f"type_mismatch:{field}")
        return False
    if not result:
        reasons.append(f"failed:{field}{op}")
    return bool(result)


# ─────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────
def fields_used(expr: Any) -> list[str]:
    """收集表达式引用到的字段名（用于检查特征库是否覆盖该规则）。"""
    out: set[str] = set()
    _collect(expr, out)
    return sorted(out)


def _collect(expr: Any, out: set[str]) -> None:
    if not isinstance(expr, Mapping):
        return
    if "field" in expr and isinstance(expr.get("field"), str):
        out.add(expr["field"])
    for g in GROUP_KEYS:
        if g in expr:
            v = expr[g]
            if g == "not":
                _collect(v, out)
            elif isinstance(v, list):
                for sub in v:
                    _collect(sub, out)


def flatten(expr: Any) -> list[dict[str, Any]]:
    """把所有叶子摊平成列表，便于生成「人类可读摘要」。"""
    out: list[dict[str, Any]] = []
    _flatten(expr, out)
    return out


def _flatten(expr: Any, out: list[dict[str, Any]]) -> None:
    if not isinstance(expr, Mapping):
        return
    if "field" in expr:
        out.append(dict(expr))
        return
    for g in GROUP_KEYS:
        if g in expr:
            v = expr[g]
            if g == "not":
                _flatten(v, out)
            elif isinstance(v, list):
                for sub in v:
                    _flatten(sub, out)


__all__ = [
    "ExprError",
    "OPS",
    "validate",
    "is_valid",
    "evaluate",
    "evaluate_traced",
    "fields_used",
    "flatten",
    "MAX_DEPTH",
    "MAX_LEAVES",
]
