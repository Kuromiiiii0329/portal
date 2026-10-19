# -*- coding: utf-8 -*-
"""
portal/virtual/minute_data.py — 分时数据（腾讯），带强制日期护栏

portal 原本**完全没有分时数据**，而结算回放离不开它（L1 止损层、Z1 涨停顺延、
14:50 平仓价都要读分时）。本模块补上，并且把原包里踩过的坑全部写成代码约束。

四个已知坑（全部来自 SKILL.md §7.4 / §9.5 实测）
------------------------------------------------
1. **分时里没有 `0925` 这一条**，分时从 `0930` 起。
   所以**竞价开盘价不要从分时里取** —— 要用实时行情的 `open` 字段。
   原实现取 `at_time(rows,"0925")` 恒返回 None，导致整个「前提 A」判定失效。
2. **`1530` 盘后数据会污染收盘价**：有的票分时里有 `1530` 一条，直接取「最后
   一行」会拿错。收盘价优先取 `1500`。
3. **必须有日期护栏**：拉完第一只票就比对 `分时日期 == 交易日`，不等则立即中止。
   否则行情还没生成时跑一次，会拿前一天的数据污染账本，**而且不报错**。
4. **指数分时到 `1500` 结束，个股可能到 `1530`**。

设计取舍
--------
- 只用标准库 `urllib`（不引 requests），避免与 data_provider 的重依赖耦合。
- 抓取与解析分离：`parse_payload()` 是纯函数，可离线单测。
- 失败不静默：网络/解析失败抛 `MinuteDataError`，日期不符抛 `StaleMinuteData`。
  「宁可报错，不要静默用错数」。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"

#: 指数前缀必须显式映射。纯按「6/9 开头才是 sh」会错到 sz —— 例如 000300/000688
#: 是**上海**指数，000852 也是上海。这个坑原包踩过。
INDEX_MAP = {
    "000001": "sh",   # 上证指数
    "399001": "sz",   # 深证成指
    "399006": "sz",   # 创业板指
    "000300": "sh",   # 沪深300
    "000688": "sh",   # 科创50
    "000852": "sh",   # 中证1000
    "000905": "sh",   # 中证500
    "899050": "bj",   # 北证50
    "399905": "sz",
}

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}

DEFAULT_TIMEOUT = 12


class MinuteDataError(RuntimeError):
    """分时抓取/解析失败。"""


class StaleMinuteData(MinuteDataError):
    """分时日期与目标交易日不一致 —— 必须中止，不许写账本。"""

    def __init__(self, expected: str, got: str, code: str):
        self.expected = expected
        self.got = got
        self.code = code
        super().__init__(
            f"{code} 的分时日期是 {got or '(空)'}，但目标交易日是 {expected}。"
            f"说明该日行情尚未生成（或接口滞后）。空跑会污染账本，故不做任何判定。"
        )


# ─────────────────────────────────────────────────────────────
# 代码 → 腾讯符号
# ─────────────────────────────────────────────────────────────
def tencent_symbol(code: str, *, is_index: bool = False) -> str:
    """`600032` → `sh600032`；指数走显式映射表。"""
    c = str(code).strip()
    if "." in c:                      # 已经是 sh600032 / 600032.SH 形式
        head, tail = (c.split(".", 1) if c[0].isalpha() else c.split(".", 1)[::-1])
        if head.lower() in ("sh", "sz", "bj"):
            return head.lower() + tail
        if tail.lower() in ("sh", "sz", "bj"):
            return tail.lower() + head
    if is_index:
        if c in INDEX_MAP:
            return INDEX_MAP[c] + c
        raise MinuteDataError(f"未知指数代码 {c}，请先补进 INDEX_MAP（不许猜前缀）")
    if c.startswith(("6", "9")):
        return "sh" + c
    if c.startswith(("8", "4")):
        return "bj" + c
    return "sz" + c


# ─────────────────────────────────────────────────────────────
# 抓取 + 解析
# ─────────────────────────────────────────────────────────────
def fetch_raw(code: str, *, is_index: bool = False, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """请求腾讯分时接口，返回解析后的原始 JSON。"""
    sym = tencent_symbol(code, is_index=is_index)
    url = f"{MINUTE_URL}?code={sym}"
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as exc:  # 网络错误统一包装，避免上层依赖具体异常类型
        raise MinuteDataError(f"分时请求失败 {sym}：{exc}") from exc

    for enc in ("utf-8", "gbk"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise MinuteDataError(f"分时响应无法解析为 JSON：{sym}")


def parse_payload(payload: dict, code: str, *, is_index: bool = False) -> dict[str, Any]:
    """把接口返回解析成 {'date', 'rows': [{time, price, volume, amount}], ...}。

    纯函数，便于离线单测。解析失败抛 MinuteDataError（不返回空壳）。
    """
    sym = tencent_symbol(code, is_index=is_index)
    node = (payload or {}).get("data", {}).get(sym)
    if not node:
        # 有些返回把 symbol 放在 data 下但键名大小写不同
        data = (payload or {}).get("data") or {}
        node = next((v for k, v in data.items() if str(k).lower() == sym.lower()), None)
    if not node:
        raise MinuteDataError(f"分时响应里没有 {sym} 节点（接口结构可能变了）")

    inner = node.get("data") or {}
    rows_raw = inner.get("data") or []
    date = str(inner.get("date") or "")

    rows: list[dict[str, Any]] = []
    for line in rows_raw:
        parts = str(line).split()
        if len(parts) < 2:
            continue
        try:
            rows.append({
                "time": parts[0],
                "price": float(parts[1]),
                "volume": float(parts[2]) if len(parts) > 2 else None,
                "amount": float(parts[3]) if len(parts) > 3 else None,
            })
        except (TypeError, ValueError):
            continue

    if not rows:
        raise MinuteDataError(f"{sym} 分时数据为空")
    rows.sort(key=lambda r: r["time"])
    return {"code": str(code), "symbol": sym, "date": date, "rows": rows}


def fetch_minute(
    code: str,
    *,
    is_index: bool = False,
    expect_date: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """抓分时；给了 `expect_date` 就强制校验，不符抛 StaleMinuteData。

    这是结算引擎唯一该用的入口 —— 日期护栏**必须**在取数层，不能寄希望于上层记得查。
    """
    parsed = parse_payload(fetch_raw(code, is_index=is_index, timeout=timeout),
                           code, is_index=is_index)
    if expect_date:
        want = str(expect_date).replace("-", "").replace("/", "")[:8]
        if parsed["date"] != want:
            raise StaleMinuteData(want, parsed["date"], code)
    return parsed


# ─────────────────────────────────────────────────────────────
# 行取用（口径统一在这里，别在上层各写一套）
# ─────────────────────────────────────────────────────────────
def at_time(rows: list[dict], hhmm: str) -> float | None:
    """取某一时刻的价格。找不到返回 None（**不要**退化成「最后一行」）。"""
    for r in rows:
        if r["time"] == hhmm:
            return r["price"]
    return None


def price_at_or_before(rows: list[dict], hhmm: str) -> float | None:
    """取 <= hhmm 的最后一个价格（用于数据缺失时的降级，语义明确）。"""
    best = None
    for r in rows:
        if r["time"] <= hhmm:
            best = r["price"]
        else:
            break
    return best


def close_price(rows: list[dict]) -> float | None:
    """收盘价：优先 `1500`，避免 `1530` 盘后数据污染。"""
    return at_time(rows, "1500") or price_at_or_before(rows, "1500")


def window_low(rows: list[dict], t0: str, t1: str) -> float | None:
    """时间窗内的最低价（闭区间）。"""
    vals = [r["price"] for r in rows if t0 <= r["time"] <= t1]
    return min(vals) if vals else None


def window_high(rows: list[dict], t0: str, t1: str) -> float | None:
    vals = [r["price"] for r in rows if t0 <= r["time"] <= t1]
    return max(vals) if vals else None


def auction_open_note() -> str:
    """把「分时里没有竞价价」这件事显式写进文档字符串与报告。"""
    return ("分时数据从 0930 起，没有 0925；竞价开盘价必须取实时行情的 open 字段。")


__all__ = [
    "MinuteDataError",
    "StaleMinuteData",
    "INDEX_MAP",
    "tencent_symbol",
    "fetch_raw",
    "parse_payload",
    "fetch_minute",
    "at_time",
    "price_at_or_before",
    "close_price",
    "window_low",
    "window_high",
    "auction_open_note",
]
