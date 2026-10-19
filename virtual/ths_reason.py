# -*- coding: utf-8 -*-
"""
portal/virtual/ths_reason.py — 同花顺题材归因（"为什么涨"）

为什么要单独接这个源（SKILL.md §4 的实测坑）：

    东财涨停池的 `hybk` 字段**会骗人** —— 它按主营业务分类，与市场实际炒的题材
    经常完全不重叠。实测一批「家居用品」涨停股，真实逻辑全是半导体 / PCB / 鸿蒙。

所以题材判断一律以同花顺人工运营的归因 tags 为准。这是题材复盘的第一依据。

接口（免费，无 key）：
    http://zx.10jqka.com.cn/event/api/getharden/date/{YYYY-MM-DD}/orderby/date/orderway/desc/charset/GBK/

注意编码是 **GBK**（路径里 charset/GBK 也说明了这点），按 UTF-8 解会全是乱码，
然后很容易被误判成「页面没内容」。

设计：抓取（fetch）与解析（parse_payload）分离，解析是纯函数、可离线单测。
字段名做**多别名容错** —— 这个接口的字段名历史上变过，写死一个键名会在某天静默返回空。
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

BASE = ("http://zx.10jqka.com.cn/event/api/getharden/date/"
        "{date}/orderby/date/orderway/desc/charset/GBK/")

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "http://data.10jqka.com.cn/",
}
DEFAULT_TIMEOUT = 15

# 字段别名：真实返回里这些键名都出现过
_CODE_KEYS = ("股票代码", "代码", "code", "stock_code", "symbol")
_NAME_KEYS = ("股票简称", "名称", "name", "stock_name")
_REASON_KEYS = ("涨停原因", "题材", "原因", "reason", "reason_type", "hyzq")
_DAYS_KEYS = ("连续涨停天数", "连板数", "lbc", "continuous_day")
_DATE_KEYS = ("日期", "date", "trade_date")


class ThsReasonError(RuntimeError):
    """题材归因抓取/解析失败。"""


def _norm_date(date8: str) -> str:
    """`20261008` / `2026-10-08` → `2026-10-08`（接口要带横杠）。

    必须校验月/日范围：只做字符串切片的话，`2026-13-99` 会被原样放行，
    然后发一个注定返回空的请求 —— 这就成了「静默拿不到数据」。
    """
    d = str(date8).replace("-", "").replace("/", "").strip()[:8]
    if len(d) != 8 or not d.isdigit():
        raise ThsReasonError(f"日期格式不对：{date8!r}（应为 YYYYMMDD）")
    month, day = int(d[4:6]), int(d[6:8])
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        raise ThsReasonError(f"日期不合法：{date8!r}（月 {month} 日 {day}）")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _pick(item: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in item and item[k] not in (None, ""):
            return item[k]
    return None


def _split_reason(value: Any) -> list[str]:
    """把「半导体+PCB」这类串拆成标签列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        tags = [str(v).strip() for v in value if str(v).strip()]
    else:
        text = str(value)
        tags = [t.strip() for t in re.split(r"[+＋、,，;；/\s]+", text) if t.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def parse_payload(payload: Any) -> dict[str, dict[str, Any]]:
    """解析成 `{code: {"name","reason","tags","boards"}}`。

    容忍多种外层结构（`data.list` / `data` 为列表 / 顶层 list）。
    解析不出任何条目时抛错 —— 静默返回空字典会让下游把它当成「今天没有归因」。
    """
    items: list[Any] | None = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for k in ("list", "data", "items", "result"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break
        if items is None:
            for k in ("list", "items", "result", "data_list"):
                if isinstance(payload.get(k), list):
                    items = payload[k]
                    break
    if items is None:
        raise ThsReasonError("题材归因响应结构无法识别（接口可能改版）")

    out: dict[str, dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        code = _pick(it, _CODE_KEYS)
        if code is None:
            continue
        code = str(code).strip()
        digits = re.sub(r"\D", "", code)
        if len(digits) != 6:
            continue
        reason = _pick(it, _REASON_KEYS)
        out[digits] = {
            "name": str(_pick(it, _NAME_KEYS) or "").strip(),
            "reason": str(reason).strip() if reason is not None else "",
            "tags": _split_reason(reason),
            "boards": _pick(it, _DAYS_KEYS),
        }

    if not out:
        raise ThsReasonError("题材归因解析出 0 条（字段名可能又变了）")
    return out


def fetch_reason(date8: str, *, timeout: int = DEFAULT_TIMEOUT) -> dict[str, dict[str, Any]]:
    """抓取指定交易日的同花顺题材归因。"""
    url = BASE.format(date=_norm_date(date8))
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as exc:
        raise ThsReasonError(f"题材归因请求失败：{exc}") from exc

    # 接口声明 GBK；但个别网关会返回 UTF-8，两种都试
    last_err: Exception | None = None
    for enc in ("gbk", "utf-8", "gb18030"):
        try:
            payload = json.loads(raw.decode(enc))
            return parse_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_err = exc
            continue
    raise ThsReasonError(f"题材归因响应无法解析：{last_err}")


def reason_for(pool: dict[str, dict], code: str) -> str | None:
    """从归因表里取某只票的题材串；没有就返回 None（不猜）。"""
    node = pool.get(str(code).strip())
    return node.get("reason") if node else None


__all__ = [
    "ThsReasonError",
    "parse_payload",
    "fetch_reason",
    "reason_for",
]
