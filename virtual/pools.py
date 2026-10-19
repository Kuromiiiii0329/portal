# -*- coding: utf-8 -*-
"""
portal/virtual/pools.py — 涨停 / 炸板 / 跌停池 + 情绪快照

数据源：akshare 对东财 push2ex 的封装（`stock_zt_pool_em` / `stock_zt_pool_zbgc_em`
/ `stock_zt_pool_dtgc_em`）。**优先用 akshare，不要自己拼原始 URL。**

理由（已实测核对源码）：SKILL.md 记过一个坑 ——「跌停池必须 `sort=fund:asc`，
用 `fbt:asc` 会返回空数组，且不报错」。实测 akshare 三个接口的 sort 参数分别是
`fbt:asc` / `fbt:asc` / **`fund:asc`** —— 这个坑它已经替我们填好了。
自己拼 URL 等于把踩过的坑重踩一遍。

另一个已核实的事实：三个池子的原始表**都带「流通市值 / 总市值」列**。
「封单额 ÷ 流通市值」是判断次日能否买到的唯一有效量化预判，缺了它可买性无从下手。

情绪快照的用途约束（UPGRADE_PLAN.md §L1.5）
-------------------------------------------
涨停家数 / 连板高度 / 炸板率这些是**同步指标**，没有领先性（华泰金工对 21 个情绪
指标的统一回测结论）。所以：

    ✅ 可以：仓位档位、决定「今天做不做」
    ❌ 禁止：进入个股评分、当作买入信号、写成「明天会涨」

`emotion_snapshot()` 返回的每个快照里都带 `"usage": "brake_only"`，
让任何读到它的下游（包括 LLM）都能看见这条约束。
"""
from __future__ import annotations

from typing import Any, Iterable

#: 字段别名 → 统一键。akshare 三个池子的列名并不一致：
#: 涨停池用「封板资金 / 连板数」，跌停池用「封单资金 / 连续跌停」。
_ALIASES: dict[str, tuple[str, ...]] = {
    "code": ("代码", "股票代码"),
    "name": ("名称", "股票简称"),
    "price": ("最新价",),
    "change_pct": ("涨跌幅",),
    "amount": ("成交额",),
    "float_cap": ("流通市值",),
    "total_cap": ("总市值",),
    "turnover_pct": ("换手率",),
    "seal_amount": ("封板资金", "封单资金"),
    "first_seal": ("首次封板时间",),
    "last_seal": ("最后封板时间",),
    "break_count": ("炸板次数", "开板次数"),
    "boards": ("连板数", "连续跌停"),
    "industry": ("所属行业",),
    "limit_stat": ("涨停统计",),
    "amplitude": ("振幅",),
}

USAGE_NOTE = "brake_only"


class PoolError(RuntimeError):
    """池子抓取失败。"""


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "" or v == "-":
            return None
        f = float(v)
        return f if f == f else None          # 过滤 NaN
    except (TypeError, ValueError):
        return None


def _time_str(v: Any) -> str | None:
    """封板时间统一成 6 位 HHMMSS 字符串。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(6)[:6]


def normalize_row(row: dict) -> dict[str, Any] | None:
    """把 akshare 的一行（中文列名）规范成统一结构。缺代码的行返回 None。"""
    out: dict[str, Any] = {}
    for key, names in _ALIASES.items():
        val = None
        for n in names:
            if n in row and row[n] not in (None, ""):
                val = row[n]
                break
        out[key] = val

    code = out.get("code")
    if code is None:
        return None
    code = str(code).strip()
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) != 6:
        return None

    seal = _num(out["seal_amount"])
    float_cap = _num(out["float_cap"])
    return {
        "code": digits,
        "name": str(out["name"] or "").strip(),
        "price": _num(out["price"]),
        "change_pct": _num(out["change_pct"]),
        "amount": _num(out["amount"]),
        "float_cap": float_cap,
        "total_cap": _num(out["total_cap"]),
        "turnover_pct": _num(out["turnover_pct"]),
        "seal_amount": seal,
        # 可买性核心指标。封单/流通 <1% 可买、1-3% 需竞价判断、>10% 买不到。
        "seal_ratio_pct": (
            round(seal / float_cap * 100, 3) if seal is not None and float_cap else None
        ),
        "first_seal": _time_str(out["first_seal"]),
        "last_seal": _time_str(out["last_seal"]),
        "break_count": _num(out["break_count"]),
        "boards": _num(out["boards"]),
        "industry": str(out["industry"] or "").strip(),
        "limit_stat": str(out["limit_stat"] or "").strip(),
        "amplitude": _num(out["amplitude"]),
    }


def _fetch_one(kind: str, date8: str) -> list[dict[str, Any]]:
    """按种类取一个池子。失败抛 PoolError（不静默返回空列表）。"""
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover
        raise PoolError("未安装 akshare，无法获取池子数据") from exc

    fn = {
        "zt": getattr(ak, "stock_zt_pool_em", None),
        "zb": getattr(ak, "stock_zt_pool_zbgc_em", None),
        "dt": getattr(ak, "stock_zt_pool_dtgc_em", None),
    }.get(kind)
    if fn is None:
        raise PoolError(f"当前 akshare 版本没有 {kind} 池接口，请升级 akshare")

    d = str(date8).replace("-", "").replace("/", "").strip()[:8]
    try:
        df = fn(date=d)
    except Exception as exc:
        raise PoolError(f"{kind} 池请求失败（{d}）：{exc}") from exc

    if df is None or len(df) == 0:
        return []

    rows: list[dict[str, Any]] = []
    for raw in df.to_dict("records"):
        norm = normalize_row(raw)
        if norm is not None:
            rows.append(norm)
    return rows


def fetch_pools(
    date8: str,
    *,
    kinds: Iterable[str] = ("zt", "zb", "dt"),
    strict: bool = False,
) -> dict[str, Any]:
    """一次取多个池子。

    Args:
        kinds: 要取的池子（zt 涨停 / zb 炸板 / dt 跌停）
        strict: True 时任一池失败就抛错；False 时把错误记进 `errors` 并继续
                （盘前流程里「炸板池挂了」不该阻断整个链路，但**必须被看见**）

    Returns:
        {"date": ..., "zt": [...], "zb": [...], "dt": [...], "errors": {...}}
    """
    d = str(date8).replace("-", "").replace("/", "").strip()[:8]
    out: dict[str, Any] = {"date": d, "errors": {}}
    for kind in kinds:
        try:
            out[kind] = _fetch_one(kind, d)
        except PoolError as exc:
            if strict:
                raise
            out[kind] = []
            out["errors"][kind] = str(exc)
    return out


# ─────────────────────────────────────────────────────────────
# 情绪快照（刹车用，不是方向盘）
# ─────────────────────────────────────────────────────────────
def emotion_snapshot(
    zt: list[dict],
    zb: list[dict] | None = None,
    dt: list[dict] | None = None,
    *,
    idx_chg_pct: float | None = None,
) -> dict[str, Any]:
    """由池子计数算出情绪读数。

    `emotion_score` 是**自定义合成分，标注「待校准」** —— 它是假设，不是事实。
    在 P3 的闸门通过之前，不允许用它做任何裁决。

    注意炸板率算法：`炸板 ÷ (涨停 + 炸板)`。
    用「涨停 ÷ (涨停 + 跌停)」会差 15 个百分点，结论可能完全相反（SKILL §4）。
    """
    zt = zt or []
    zb = zb or []
    dt = dt or []

    zt_n, zb_n, dt_n = len(zt), len(zb), len(dt)
    max_lbc = max((int(x.get("boards") or 0) for x in zt), default=0)
    lbc_ge2 = sum(1 for x in zt if (x.get("boards") or 0) >= 2)
    zb_rate = round(zb_n / (zt_n + zb_n), 4) if (zt_n + zb_n) else None

    ratios = [x.get("seal_ratio_pct") for x in zt if x.get("seal_ratio_pct") is not None]
    return {
        "zt_n": zt_n,
        "dt_n": dt_n,
        "zb_n": zb_n,
        "zb_rate": zb_rate,
        "zt_dt_ratio": round(zt_n / dt_n, 3) if dt_n else None,
        "max_lbc": max_lbc,
        "lbc_ge2_n": lbc_ge2,
        "seal_ratio_median_pct": round(float(sorted(ratios)[len(ratios) // 2]), 3) if ratios else None,
        "idx_chg_pct": idx_chg_pct,
        # 合成分：负值越大越冰点。**待校准**，P3 之前不得用于裁决。
        "emotion_score": round(zt_n - 1.5 * dt_n + 3 * max_lbc, 2),
        "emotion_formula": "zt_n - 1.5*dt_n + 3*max_lbc（自定义合成分，待校准）",
        "usage": USAGE_NOTE,
        "usage_note": (
            "同步指标，只可用于仓位档位与「今天做不做」；"
            "禁止进入个股评分、禁止当作买入信号、禁止写成『明天会涨』"
        ),
    }


def buyability_bucket(seal_ratio_pct: float | None) -> str:
    """按封单/流通比给出可买性分档（SKILL §3.5 的四档）。"""
    if seal_ratio_pct is None:
        return "unknown"
    r = float(seal_ratio_pct)
    if r < 1.0:
        return "buyable"            # <1% 可买
    if r < 3.0:
        return "auction_decide"     # 1-3% 需 9:20-9:25 竞价窗口判断
    if r < 10.0:
        return "likely_sealed"      # 3-10% 大概率一字，基本买不到
    return "unbuyable"              # >10% 完全买不到


__all__ = [
    "PoolError",
    "USAGE_NOTE",
    "normalize_row",
    "fetch_pools",
    "emotion_snapshot",
    "buyability_bucket",
]
