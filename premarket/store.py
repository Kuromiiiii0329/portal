# -*- coding: utf-8 -*-
"""
portal/premarket/store.py — 四张表的存储与校验

设计：**append-only 事件流 + 重放得出状态**

为什么不直接存当前状态：
用户要求「改表内数据变动都需要写明原因」「可追溯」。如果存的是当前状态，
那"为什么删"就只能靠另开一个日志文件记 —— 迟早会与真实状态脱节。
把**每一次改动都记成事件**，「当前状态」是重放结果，于是：

- 任何一行都能回答「它是什么时候、以什么理由被加的/改的/删的」
- 事后无法悄悄改历史（改一行要追加一条事件）
- 状态可重放校验（`replay() == state()`）

四张表
------
| 表 | 键 | 作用 |
|---|---|---|
| `pool`      | code        | 选股观察表：分级跟踪，动态进出 |
| `positions` | code        | 模拟持仓表：开仓/平仓/调止损止盈 |
| `infos`     | 内容哈希    | 重要信息表：影响中期逻辑的关键信息 |
| `lessons`   | 内容哈希    | 交易经验表：可复用的规律与教训 |
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from . import paths

# ─────────────────────────────────────────────────────────────
# 常量：枚举与上限（用户 spec 里的硬约束）
# ─────────────────────────────────────────────────────────────
TIERS = ("core", "normal")                 # 核心观察 / 普通观察
POOL_STATUS = ("tracking", "ready", "removed")   # 跟踪中 / 待买入 / 已移除
SCOPES = ("大盘", "行业", "个股")
LEVELS = ("高", "中", "低")
RESULTS = ("成功经验", "失败教训")

#: 每日新增标的上限、核心观察上限（spec: 不超过 8 / 6）
MAX_NEW_PER_DAY = 8
MAX_CORE = 6
#: 重要信息内容长度上限（spec: ≤50 字）
MAX_INFO_LEN = 50
#: 单条经验/理由的长度上限（防止 LLM 灌长文）
MAX_TEXT = 200


class ValidationError(ValueError):
    """结构性校验失败。消息要能直接告诉用户「哪个字段、为什么」。"""


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.date.today().isoformat()


def _req(fields: dict, key: str, table: str) -> Any:
    if key not in fields or fields[key] in (None, ""):
        raise ValidationError(f"{table}: 缺少必填字段 `{key}`")
    return fields[key]


def _text(fields: dict, key: str, table: str, *, maxlen: int = MAX_TEXT,
          required: bool = True) -> str:
    if key not in fields or fields[key] in (None, ""):
        if required:
            raise ValidationError(f"{table}: 缺少必填字段 `{key}`")
        return ""
    v = str(fields[key]).strip()
    if len(v) > maxlen:
        raise ValidationError(
            f"{table}.{key} 超长（{len(v)} > {maxlen}）：{v[:40]}…")
    return v


def _num(fields: dict, key: str, table: str, *, positive: bool = True) -> float:
    if key not in fields or fields[key] in (None, ""):
        raise ValidationError(f"{table}: 缺少必填数值字段 `{key}`")
    try:
        v = float(fields[key])
    except (TypeError, ValueError):
        raise ValidationError(f"{table}.{key} 不是数字：{fields[key]!r}") from None
    if positive and v <= 0:
        raise ValidationError(f"{table}.{key} 必须为正数，得到 {v}")
    return round(v, 4)


def _enum(fields: dict, key: str, table: str, allowed: Iterable[str]) -> str:
    v = str(_req(fields, key, table)).strip()
    allowed = tuple(allowed)
    if v not in allowed:
        raise ValidationError(f"{table}.{key} 只能是 {allowed}，得到 {v!r}")
    return v


def _date8(value: Any, field: str) -> str:
    s = str(value).replace("-", "").strip()
    if len(s) != 8 or not s.isdigit():
        raise ValidationError(f"{field} 必须是 YYYYMMDD 或 YYYY-MM-DD，得到 {value!r}")
    return s


def _iso_date(value: Any, field: str) -> str:
    s = str(value).replace("-", "").strip()[:8]
    if len(s) != 8 or not s.isdigit():
        raise ValidationError(f"{field} 必须是日期，得到 {value!r}")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def stable_id(text: str, prefix: str = "") -> str:
    """内容哈希做 id：同样的内容重复添加会落到同一 id，天然幂等。"""
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}{h}" if prefix else h


# ─────────────────────────────────────────────────────────────
# 各表的字段校验
# ─────────────────────────────────────────────────────────────
def normalize_pool(fields: dict) -> dict:
    code = str(_req(fields, "code", "pool")).strip()
    if not (len(code) == 6 and code.isdigit()):
        raise ValidationError(f"pool.code 必须是 6 位数字，得到 {code!r}")
    # status 必须出现在规范化结果里，否则 `update(status=...)` 会被
    # 「没有可识别的字段」拒掉 —— 这是第一版就踩到的坑。
    status = str(fields.get("status") or "tracking").strip()
    if status not in POOL_STATUS:
        raise ValidationError(f"pool.status 只能是 {POOL_STATUS}，得到 {status!r}")
    out = {
        "code": code,
        "name": _text(fields, "name", "pool", maxlen=20),
        "tier": _enum(fields, "tier", "pool", TIERS),
        "logic": _text(fields, "logic", "pool"),          # 入选逻辑
        "reason": _text(fields, "reason", "pool"),        # 买入理由 1-2 句
        "trigger": _text(fields, "trigger", "pool"),      # 量化触发条件
        "stop_loss": _num(fields, "stop_loss", "pool"),
        "take_profit": _num(fields, "take_profit", "pool"),
        "added_date": _iso_date(fields.get("added_date") or _today(), "pool.added_date"),
        "status": status,
    }
    if out["stop_loss"] >= out["take_profit"]:
        raise ValidationError(
            f"pool[{code}] 止损 {out['stop_loss']} 必须低于止盈 {out['take_profit']}")
    # 触发条件必须是可判定的（含数字），否则就是"看情况"这类空话
    if not any(ch.isdigit() for ch in out["trigger"]):
        raise ValidationError(
            f"pool[{code}].trigger 必须含具体价格/量能数字，不能是「{out['trigger']}」")
    return out


def normalize_lesson(fields: dict) -> dict:
    lesson = _text(fields, "lesson", "lessons")
    # spec: 禁止空泛总结。「要控制仓位」无效，「弱势市场追高胜率低于 20%」有效
    if len(lesson) < 8:
        raise ValidationError(f"lessons.lesson 太短（{len(lesson)} 字），空泛总结不算经验")
    if not any(ch.isdigit() for ch in lesson) and "不" not in lesson:
        raise ValidationError(
            "lessons.lesson 需要可验证的说法（含数字或明确条件），"
            f"得到「{lesson}」")
    out = {
        "lesson": lesson,
        "scenario": _text(fields, "scenario", "lessons"),
        "result": _enum(fields, "result", "lessons", RESULTS),
        "recorded_on": _iso_date(fields.get("recorded_on") or _today(),
                                 "lessons.recorded_on"),
    }
    if fields.get("evidence"):
        out["evidence"] = _text(fields, "evidence", "lessons")
    return out


def normalize_info(fields: dict) -> dict:
    content = _text(fields, "content", "infos", maxlen=MAX_INFO_LEN)
    out = {
        "content": content,
        "scope": _enum(fields, "scope", "infos", SCOPES),
        "scope_detail": _text(fields, "scope_detail", "infos", maxlen=30,
                              required=False),
        "level": _enum(fields, "level", "infos", LEVELS),
        "expires_on": _iso_date(_req(fields, "expires_on", "infos"),
                                "infos.expires_on"),
        "recorded_on": _iso_date(fields.get("recorded_on") or _today(),
                                 "infos.recorded_on"),
    }
    if fields.get("source"):
        out["source"] = _text(fields, "source", "infos", maxlen=60)
    return out


def normalize_position(fields: dict) -> dict:
    code = str(_req(fields, "code", "positions")).strip()
    if not (len(code) == 6 and code.isdigit()):
        raise ValidationError(f"positions.code 必须是 6 位数字，得到 {code!r}")
    alloc = _num(fields, "alloc_pct", "positions")
    if not (0 < alloc <= 100):
        raise ValidationError(f"positions[{code}].alloc_pct 必须在 (0,100]，得到 {alloc}")
    out = {
        "code": code,
        "name": _text(fields, "name", "positions", maxlen=20),
        "alloc_pct": alloc,               # 开仓时占总资产 %
        "entry_price": _num(fields, "entry_price", "positions"),
        "stop_loss": _num(fields, "stop_loss", "positions"),
        "take_profit": _num(fields, "take_profit", "positions"),
        "entry_date": _iso_date(fields.get("entry_date") or _today(),
                                "positions.entry_date"),
        "entry_logic": _text(fields, "entry_logic", "positions"),
    }
    if out["stop_loss"] >= out["entry_price"]:
        raise ValidationError(
            f"positions[{code}] 止损 {out['stop_loss']} 必须低于成本 {out['entry_price']}")
    if out["take_profit"] <= out["entry_price"]:
        raise ValidationError(
            f"positions[{code}] 止盈 {out['take_profit']} 必须高于成本 {out['entry_price']}")
    return out


NORMALIZERS = {
    "pool": normalize_pool,
    "positions": normalize_position,
    "infos": normalize_info,
    "lessons": normalize_lesson,
}

#: 每张表的记录键（事件里用 `id`）
ID_FIELDS = {"pool": "code", "positions": "code",
             "infos": "content", "lessons": "lesson"}


def row_id(table: str, fields: dict) -> str:
    key = ID_FIELDS[table]
    raw = str(fields.get(key, "")).strip()
    if not raw:
        raise ValidationError(f"{table}: 缺少主键字段 `{key}`")
    if table in ("pool", "positions"):
        return raw
    return stable_id(raw, prefix="I" if table == "infos" else "L")


# ─────────────────────────────────────────────────────────────
# 事件存储
# ─────────────────────────────────────────────────────────────
class EventStore:
    """append-only 事件流 + 重放。

    事件格式：
        {"seq":1, "op":"add|update|remove", "id":"600519",
         "at":"2026-10-19T23:30:00+08:00", "plan_date":"20261020",
         "reason":"...", "fields":{...}}
    """

    def __init__(self, table: str, path: Path | None = None):
        if table not in paths.TABLES:
            raise ValueError(f"未知表 {table!r}，只能是 {paths.TABLES}")
        self.table = table
        self.path = path or paths.table_path(table)

    # ── 读写 ────────────────────────────────────────────
    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"{self.path.name} 第 {i} 行不是合法 JSON：{exc}") from None
        return out

    def _append(self, event: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def state(self) -> dict[str, dict]:
        """重放事件得到当前状态。"""
        cur: dict[str, dict] = {}
        for ev in self.events():
            rid, op = ev.get("id"), ev.get("op")
            if op == "add":
                cur[rid] = {**ev.get("fields", {}),
                            "_id": rid, "_added": ev.get("at"),
                            "_reason": ev.get("reason")}
            elif op == "update":
                if rid in cur:
                    cur[rid].update(ev.get("fields", {}))
                    cur[rid]["_updated"] = ev.get("at")
                    cur[rid]["_reason"] = ev.get("reason")
            elif op == "remove":
                cur.pop(rid, None)
            else:
                raise ValidationError(f"{self.table}: 未知 op {op!r}")
        return cur

    def alive(self) -> dict[str, dict]:
        """未删除的记录（池表额外排除 status=removed）。"""
        st = self.state()
        if self.table == "pool":
            return {k: v for k, v in st.items() if v.get("status") != "removed"}
        return st

    # ── 写入 ────────────────────────────────────────────
    def add(self, fields: dict, *, reason: str, plan_date: str,
            at: str | None = None) -> dict:
        norm = NORMALIZERS[self.table](fields)
        rid = row_id(self.table, norm)
        if rid in self.state():
            raise ValidationError(
                f"{self.table} 已存在 id={rid}。要改请用 update，并写明原因。")
        reason = _check_reason(reason, self.table)
        ev = self._append({"op": "add", "id": rid, "at": at or _now(),
                           "plan_date": str(plan_date).replace("-", "")[:8],
                           "reason": reason, "fields": norm})
        return ev

    def update(self, rid: str, fields: dict, *, reason: str, plan_date: str,
               at: str | None = None) -> dict:
        if rid not in self.state():
            raise ValidationError(f"{self.table} 不存在 id={rid}，无法 update")
        reason = _check_reason(reason, self.table)
        # 只允许已知字段，防止灌任意键
        allowed = set(NORMALIZERS[self.table]({**self.state()[rid], **fields}))
        merged = {k: v for k, v in fields.items() if k in allowed}
        if not merged:
            raise ValidationError(
                f"{self.table}[{rid}] update 没有可识别的字段：{list(fields)}")
        return self._append({"op": "update", "id": rid, "at": at or _now(),
                             "plan_date": str(plan_date).replace("-", "")[:8],
                             "reason": reason, "fields": merged})

    def remove(self, rid: str, *, reason: str, plan_date: str,
               at: str | None = None) -> dict:
        if rid not in self.state():
            raise ValidationError(f"{self.table} 不存在 id={rid}，无法 remove")
        reason = _check_reason(reason, self.table)
        return self._append({"op": "remove", "id": rid, "at": at or _now(),
                             "plan_date": str(plan_date).replace("-", "")[:8],
                             "reason": reason, "fields": {}})

    # ── 统计 ────────────────────────────────────────────
    def added_on(self, plan_date: str) -> list[str]:
        d8 = str(plan_date).replace("-", "")[:8]
        return [e["id"] for e in self.events()
                if e.get("op") == "add" and str(e.get("plan_date")) == d8]


def _check_reason(reason: str | None, table: str) -> str:
    r = (reason or "").strip()
    if not r:
        raise ValidationError(
            f"{table}: 改动必须写明 reason（用户 spec：改表要写移除/添加原因）")
    if len(r) > MAX_TEXT:
        raise ValidationError(f"{table}.reason 超长（{len(r)} > {MAX_TEXT}）")
    return r


# ─────────────────────────────────────────────────────────────
# 四张表的门面
# ─────────────────────────────────────────────────────────────
class Tables:
    """四张表的统一入口。所有经由它的写入都会做跨表约束检查。"""

    def __init__(self, root: Path | None = None):
        if root is not None:
            p = Path(root)
            self.pool = EventStore("pool", p / "pool.jsonl")
            self.positions = EventStore("positions", p / "positions.jsonl")
            self.infos = EventStore("infos", p / "infos.jsonl")
            self.lessons = EventStore("lessons", p / "lessons.jsonl")
        else:
            self.pool = EventStore("pool")
            self.positions = EventStore("positions")
            self.infos = EventStore("infos")
            self.lessons = EventStore("lessons")

    def get(self, name: str) -> EventStore:
        return {"pool": self.pool, "positions": self.positions,
                "infos": self.infos, "lessons": self.lessons}[name]

    # ── 跨表约束 ────────────────────────────────────────
    def check_caps(self, plan_date: str) -> None:
        """池子容量约束（用户 spec：每日新增 ≤8、核心 ≤6）。"""
        d8 = str(plan_date).replace("-", "")[:8]
        new_today = self.pool.added_on(d8)
        if len(new_today) > MAX_NEW_PER_DAY:
            raise ValidationError(
                f"当日新增 {len(new_today)} 只，超过上限 {MAX_NEW_PER_DAY}。"
                f"今天加的：{new_today}")
        core = [k for k, v in self.pool.alive().items() if v.get("tier") == "core"]
        if len(core) > MAX_CORE:
            raise ValidationError(
                f"核心观察 {len(core)} 只，超过上限 {MAX_CORE}：{core}")

    def check_pool_not_held(self) -> None:
        """已在持仓的标的必须从观察表移出（用户 spec 明确要求）。"""
        held = set(self.positions.alive())
        both = sorted(set(self.pool.alive()) & held)
        if both:
            raise ValidationError(
                f"这些标的同时在观察表和持仓表：{both}。"
                f"调仓后必须从观察表移除（status=removed 或直接 remove）。")

    def expire_infos(self, on_date: str | None = None) -> list[str]:
        """把过期的重要信息移除（用户 spec：超过有效期立即删除）。

        返回被移除的 id 列表。原因是自动生成的，但仍然写进事件流。
        """
        on = _iso_date(on_date or _today(), "on_date")
        gone = []
        for rid, row in self.infos.alive().items():
            if str(row.get("expires_on", "")) < on:
                self.infos.remove(
                    rid, reason=f"超过有效期 {row.get('expires_on')}（自动清理）",
                    plan_date=on.replace("-", ""))
                gone.append(rid)
        return gone

    def prune_expired_lessons(self) -> list[str]:
        """保留全部经验：证伪靠追加 `result=失败教训` 的新条目，不删历史。"""
        return []

    def snapshot(self) -> dict[str, Any]:
        """给渲染/LLM 用的只读快照。"""
        return {
            "pool": sorted(self.pool.state().values(),
                           key=lambda r: (r.get("tier") != "core",
                                          str(r.get("added_date", "")))),
            "positions": sorted(self.positions.alive().values(),
                                key=lambda r: str(r.get("code"))),
            "infos": sorted(self.infos.alive().values(),
                            key=lambda r: (r.get("level") != "高",
                                           str(r.get("expires_on", "")))),
            "lessons": sorted(self.lessons.alive().values(),
                              key=lambda r: str(r.get("recorded_on", "")),
                              reverse=True),
        }


__all__ = ["EventStore", "Tables", "ValidationError",
           "TIERS", "POOL_STATUS", "SCOPES", "LEVELS", "RESULTS",
           "MAX_NEW_PER_DAY", "MAX_CORE", "MAX_INFO_LEN", "MAX_TEXT",
           "stable_id", "row_id", "normalize_pool", "normalize_position",
           "normalize_info", "normalize_lesson"]
