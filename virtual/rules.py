# -*- coding: utf-8 -*-
"""
portal/virtual/rules.py — 规则注册表（四态生命周期）

规则不是散文，是一条**可求值、可回测、可回滚**的记录。

存储形态：`data/virtual/rules.jsonl`，**append-only 事件日志**。
每个 `rule_id` 可以有多条记录（版本演进），`current()` 取最后一条为当前状态。
这样「规则改过几次、每次为什么改、证据是什么」全在文件里，可精确回放。

生命周期（UPGRADE_PLAN.md §6.1）：

    hypothesis ──gate──> testing ──gate/人工──> active
         │                  │                   │
         └──> retired       └──> retired        └──decay_monitor──> decaying
                                                                    │
                                                          human/gate └──> active | retired

**权限边界是不可越的**：
- Agent 只能创建 `hypothesis`；
- `→ testing` 只有 `gate` 能做；
- `→ active` 在 P1 阶段**必须人工批准**（`human:*`），这是渐进授权的第一档；
- `→ decaying` 只有 `decay_monitor` 能做（机械规则，不靠人发现）。

被拒绝的假设写进 `rejected.jsonl`。**这一步不能省** —— 不记录拒绝理由，Agent 会
一遍又一遍提同样的坏主意（SKILL.md §9.4：不许「试 50 个条件挑最好的」）。
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Any, Iterable

from . import expr as expr_mod
from . import paths

SCHEMA_VERSION = 1

STATUSES = ("hypothesis", "testing", "active", "decaying", "retired")

#: 允许的状态迁移
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "hypothesis": {"testing", "retired"},
    "testing": {"active", "retired"},
    "active": {"decaying", "retired"},
    "decaying": {"active", "retired"},
    "retired": set(),
}

#: 允许执行该迁移的角色（actor 的前缀）
REQUIRED_ROLE: dict[tuple[str, str], set[str]] = {
    ("hypothesis", "testing"): {"gate"},
    ("testing", "active"): {"human", "gate"},
    ("active", "decaying"): {"decay_monitor", "gate"},
    ("decaying", "active"): {"human", "gate"},
}

SCOPES = ("candidate_filter", "entry", "exit", "sizing", "brake")

INDICATORS = {
    "market_emotion", "candidate_strength", "auction", "execution", "exit",
    "position", "data_quality",
}

_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")


class RuleError(ValueError):
    """规则记录非法（schema / 状态迁移 / 权限）。"""


# ─────────────────────────────────────────────────────────────
# 基础 IO
# ─────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _read_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RuleError(f"{path.name} 第 {i} 行不是合法 JSON：{exc}") from exc
    return out


def _append_jsonl(path, record: dict) -> None:
    paths.ensure_dirs()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")


def _role(actor: str) -> str:
    """`human:kuromi` → `human`；`gate` → `gate`。"""
    return str(actor or "").split(":", 1)[0].strip().lower()


# ─────────────────────────────────────────────────────────────
# 校验
# ─────────────────────────────────────────────────────────────
def validate(rule: dict) -> list[str]:
    """返回问题清单；空 = 合法。"""
    problems: list[str] = []
    if not isinstance(rule, dict):
        return ["规则必须是对象"]

    rid = rule.get("rule_id")
    if not isinstance(rid, str) or not _RULE_ID_RE.match(rid):
        problems.append(f"rule_id 非法：{rid!r}（3-64 位，字母数字与 _.-）")

    ver = rule.get("version")
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        problems.append(f"version 必须是 >=1 的整数，实际 {ver!r}")

    scope = rule.get("scope")
    if scope not in SCOPES:
        problems.append(f"scope 必须是 {SCOPES} 之一，实际 {scope!r}")

    if not isinstance(rule.get("desc_human"), str) or not rule["desc_human"].strip():
        problems.append("desc_human 必须是非空字符串（给人看的说明）")

    status = rule.get("status")
    if status not in STATUSES:
        problems.append(f"status 必须是 {STATUSES} 之一，实际 {status!r}")

    # ★ 机器可读表达式是必填项，不是可选项 —— 见 SKILL.md §9.7.3 的事故
    if "expr" not in rule:
        problems.append("缺少 expr（机器可读表达式）。只有 desc_human 的规则不许入库")
    else:
        problems.extend(f"expr: {p}" for p in expr_mod.validate(rule["expr"]))

    ev = rule.get("evidence")
    if ev is None:
        problems.append("缺少 evidence（哪怕是空的 {} 也要显式给）")
    elif not isinstance(ev, dict):
        problems.append("evidence 必须是对象")

    ind = rule.get("indicator")
    if ind is not None and ind not in INDICATORS:
        problems.append(f"indicator 必须是 {sorted(INDICATORS)} 之一，实际 {ind!r}")

    if not rule.get("author"):
        problems.append("缺少 author（谁提的：agent:propose / human:xxx）")

    return problems


def _require_valid(rule: dict) -> None:
    problems = validate(rule)
    if problems:
        raise RuleError("规则非法：" + "；".join(problems))


# ─────────────────────────────────────────────────────────────
# 读
# ─────────────────────────────────────────────────────────────
def all_records() -> list[dict]:
    return _read_jsonl(paths.RULES)


def current() -> dict[str, dict]:
    """每个 rule_id 的最新一条记录（按文件顺序，后写覆盖先写）。"""
    out: dict[str, dict] = {}
    for rec in all_records():
        rid = rec.get("rule_id")
        if isinstance(rid, str):
            out[rid] = rec
    return out


def get(rule_id: str) -> dict | None:
    return current().get(rule_id)


def history(rule_id: str) -> list[dict]:
    return [r for r in all_records() if r.get("rule_id") == rule_id]


def by_status(*statuses: str) -> list[dict]:
    want = set(statuses) if statuses else set(STATUSES)
    return [r for r in current().values() if r.get("status") in want]


def active() -> list[dict]:
    return by_status("active")


def by_scope(scope: str) -> list[dict]:
    return [r for r in current().values() if r.get("scope") == scope]


def next_version(rule_id: str) -> int:
    hist = history(rule_id)
    if not hist:
        return 1
    return max(int(r.get("version", 0)) for r in hist) + 1


# ─────────────────────────────────────────────────────────────
# 写
# ─────────────────────────────────────────────────────────────
def append(
    *,
    rule_id: str,
    scope: str,
    desc_human: str,
    expr: dict,
    author: str,
    evidence: dict | None = None,
    indicator: str | None = None,
    status: str = "hypothesis",
    status_reason: str = "",
    decay_monitor: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """追加一条规则记录（自动算 version / supersedes / created_at）。

    新规则只允许以 `hypothesis` 入库：**提案与激活必须分离**。
    """
    if status != "hypothesis":
        raise RuleError(
            f"新规则只能以 hypothesis 入库（收到 {status!r}）。"
            "激活必须走 gate/human，见 transition()。"
        )

    rid = str(rule_id)
    ver = next_version(rid)
    rec: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": rid,
        "version": ver,
        "created_at": _now(),
        "author": str(author),
        "scope": scope,
        "desc_human": desc_human,
        "expr": expr,
        "evidence": dict(evidence or {}),
        "status": status,
        "status_reason": status_reason,
        "decay_monitor": decay_monitor or {
            "rolling_n": 30, "threshold_pct": 0.0, "consecutive_fail": 0,
        },
        "supersedes": f"{rid}@{ver - 1}" if ver > 1 else None,
        "anchor_sha256": None,
    }
    if indicator:
        rec["indicator"] = indicator
    if extra:
        rec.update({k: v for k, v in extra.items() if k not in rec})

    _require_valid(rec)
    _append_jsonl(paths.RULES, rec)
    return rec


def transition(
    rule_id: str,
    to_status: str,
    *,
    actor: str,
    reason: str = "",
    evidence_patch: dict | None = None,
    decay_monitor: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """状态迁移。追加一条新版本记录，不修改历史行。"""
    cur = get(rule_id)
    if cur is None:
        raise RuleError(f"规则 {rule_id} 不存在")

    frm = cur.get("status", "")
    if to_status not in STATUSES:
        raise RuleError(f"未知目标状态 {to_status!r}")
    if to_status == frm:
        raise RuleError(f"{rule_id} 已经是 {frm}，无需迁移")
    if to_status not in ALLOWED_TRANSITIONS.get(frm, set()):
        raise RuleError(
            f"{rule_id} 不允许 {frm} → {to_status}（允许：{sorted(ALLOWED_TRANSITIONS.get(frm, set()))}）"
        )

    role = _role(actor)
    allowed_roles = REQUIRED_ROLE.get((frm, to_status))
    if allowed_roles is not None and role not in allowed_roles:
        raise RuleError(
            f"{rule_id} 的 {frm} → {to_status} 只能由 {sorted(allowed_roles)} 执行，"
            f"当前 actor={actor!r}（角色 {role!r}）"
        )

    new = dict(cur)
    new["version"] = next_version(rule_id)
    new["created_at"] = _now()
    new["author"] = str(actor)
    new["status"] = to_status
    new["status_reason"] = reason or cur.get("status_reason", "")
    new["supersedes"] = f"{rule_id}@{cur.get('version')}"
    new["expr"] = cur.get("expr")
    new["evidence"] = dict(cur.get("evidence") or {})
    if evidence_patch:
        new["evidence"].update(evidence_patch)
    if decay_monitor:
        new["decay_monitor"] = dict(decay_monitor)
    if extra:
        new.update(extra)

    _require_valid(new)
    _append_jsonl(paths.RULES, new)
    return new


def revise(
    rule_id: str,
    *,
    actor: str,
    evidence_patch: dict | None = None,
    decay_monitor: dict | None = None,
    status_reason: str | None = None,
    extra: dict | None = None,
) -> dict:
    """**不改状态**地追加一个新版本（用于衰减计数、证据补录）。

    与 `transition()` 的分工：
        transition  改变状态（hypothesis → testing → active …），受角色矩阵约束
        revise      状态不变，只更新元数据（`consecutive_fail` 计数、evidence）

    为什么需要它：衰减监控要在**还没降级**的时候累加「连续 N 次低于阈值」，
    那不是状态迁移。但又必须留痕（append-only），所以走这里。

    权限：只有 `decay_monitor` / `gate` / `human` 能改元数据。
    `agent:propose` 不许动已入库规则的证据。
    """
    cur = get(rule_id)
    if cur is None:
        raise RuleError(f"规则 {rule_id} 不存在")

    role = _role(actor)
    if role not in ("decay_monitor", "gate", "human"):
        raise RuleError(
            f"{rule_id} 的元数据修订只能由 decay_monitor/gate/human 执行，"
            f"当前 actor={actor!r}（角色 {role!r}）"
        )

    new = dict(cur)
    new["version"] = next_version(rule_id)
    new["created_at"] = _now()
    new["author"] = str(actor)
    new["supersedes"] = f"{rule_id}@{cur.get('version')}"
    new["expr"] = cur.get("expr")
    new["evidence"] = dict(cur.get("evidence") or {})
    if evidence_patch:
        new["evidence"].update(evidence_patch)
    if decay_monitor:
        new["decay_monitor"] = dict(decay_monitor)
    if status_reason is not None:
        new["status_reason"] = status_reason
    if extra:
        new.update(extra)

    _require_valid(new)
    _append_jsonl(paths.RULES, new)
    return new


def record_rejection(
    *,
    rule_id: str,
    expr: dict,
    reason: str,
    evidence: dict | None = None,
    actor: str = "gate",
) -> dict:
    """把被闸门拒绝的假设归档。**必须带 rejected 理由**，否则会重复提议。"""
    rec = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": str(rule_id),
        "created_at": _now(),
        "actor": str(actor),
        "reason": str(reason),
        "expr": expr,
        "evidence": dict(evidence or {}),
        "rejected": True,
    }
    _append_jsonl(paths.REJECTED, rec)
    return rec


def rejected_keys() -> set[str]:
    """已拒绝的 rule_id 集合 —— 演化器据此跳过重复提议。"""
    return {str(r.get("rule_id")) for r in _read_jsonl(paths.REJECTED)}


def rejected_records() -> list[dict]:
    return _read_jsonl(paths.REJECTED)


# ─────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────
def evidence_flags(ev: dict) -> list[str]:
    """**只做提示，不做裁决**：把证据里最容易被误读的地方标出来。

    正式闸门（G1–G5）在 P3 的 gate.py 里，这里只负责让「均值正、中位负」这类
    结构性问题在写库时就被看见（避坑手册 §4.1）。

    Args:
        ev: 证据字段，键名与 rules.jsonl 的 evidence 一致（*_pct 单位为百分数）

    """
    if not isinstance(ev, dict):
        return []
    flags: list[str] = []
    mean = ev.get("mean_pct")
    median = ev.get("median_pct")
    if isinstance(mean, (int, float)) and isinstance(median, (int, float)):
        if mean > 0 >= median:
            flags.append("mean_positive_median_negative:多数交易在亏钱，少数大赢撑起均值")
    n = ev.get("n")
    if isinstance(n, int) and n < 30:
        flags.append(f"sample_too_small:n={n}（<30 不得下任何结论）")
    yrs = ev.get("same_sign_years")
    if isinstance(yrs, str) and "/" in yrs:
        try:
            a, b = yrs.split("/")
            if int(b) >= 2 and int(a) != int(b):
                flags.append(f"not_uniform_across_years:{yrs}")
        except ValueError:
            pass
    after = ev.get("after_cost_pct")
    if isinstance(after, (int, float)) and after <= 0:
        flags.append(f"non_positive_after_cost:{after}%")
    drop = ev.get("drop_top_window_mean_pct")
    if isinstance(drop, (int, float)) and isinstance(mean, (int, float)):
        if mean > 0 >= drop:
            flags.append("collapses_when_dropping_largest_window:疑似单一窗口撑起（SKILL §9.7.2）")
    return flags


def summary_table() -> list[dict]:
    """当前规则一览（给报告/前端用）。"""
    rows = []
    for rid, rec in sorted(current().items()):
        ev = rec.get("evidence") or {}
        rows.append({
            "rule_id": rid,
            "version": rec.get("version"),
            "scope": rec.get("scope"),
            "status": rec.get("status"),
            "desc": rec.get("desc_human"),
            "n": ev.get("n"),
            "mean_pct": ev.get("mean_pct"),
            "median_pct": ev.get("median_pct"),
            "after_cost_pct": ev.get("after_cost_pct"),
            "flags": evidence_flags(ev),
        })
    return rows


def bootstrap_rules() -> list[dict]:
    """把「无名游资」包已验证过、且有明确证据的规则作为种子写入（幂等）。

    只写**原文明确给过的数字**。原文没给的字段一律留空并注明 —— 编造一个看起来
    合理的中位数或 t 值，正是本系统要防的事（避坑手册：最贵的错误是算错了没报错，
    而凭空填数连"算"都算不上）。

    来源：SKILL.md §9.3（档位统计）、§9.7.2（样本外验证 / 剔除最大窗口 / 扣费后）。

    已被原文自己推翻的「低位×回调」复合档**不写**（§9.7.2：剔除 2024-02 单月后转负）。
    """
    seeds = [
        {
            "rule_id": "POS-120-LOW",
            "scope": "candidate_filter",
            "indicator": "candidate_strength",
            "desc_human": "候选须满足 120 日区间位 < 0.30（低位）",
            "expr": {"all": [{"field": "zone_pos_120", "op": "<", "value": 0.30}]},
            "evidence": {
                "n": 5093,
                "mean_pct": 0.580,
                "winrate": 53.1,
                "same_sign_years": "4/4",
                "by_year": {"2023": 0.41, "2024": 0.98, "2025": 0.32, "2026": 0.19},
                "drop_top_window_mean_pct": 0.152,
                "after_cost_pct": 0.050,
                "vs_baseline_pp": 0.693,
                "median_pct": None,
                "t_stat": None,
                "missing_fields_note": "原文未给中位数与 t 值，故留空（不编造）",
                "source": "无名游资 SKILL.md §9.3 / §9.7.2（样本外验证）",
            },
        },
        {
            "rule_id": "CHG5D-NEG",
            "scope": "candidate_filter",
            "indicator": "candidate_strength",
            "desc_human": "候选须满足前 5 日涨幅 < 0（前期回调）",
            "expr": {"all": [{"field": "chg_5d_pct", "op": "<", "value": 0.0}]},
            "evidence": {
                "n": 3501,
                "mean_pct": 0.719,
                "winrate": 57.0,
                "same_sign_years": "4/4",
                "drop_top_window_mean_pct": 0.145,
                "after_cost_pct": 0.043,
                "vs_baseline_pp": 0.832,
                "median_pct": None,
                "t_stat": None,
                "missing_fields_note": "原文未给中位数与 t 值，故留空（不编造）",
                "source": "无名游资 SKILL.md §9.3 / §9.7.2",
            },
        },
        {
            "rule_id": "SEAL-RATIO-BUYABLE",
            "scope": "entry",
            "indicator": "auction",
            "desc_human": "封单额/流通市值 < 1% 才判「可买」；> 10% 直接剔除（买不到）",
            "expr": {"all": [{"field": "seal_ratio_pct", "op": "<", "value": 1.0}]},
            "evidence": {
                "n": None,
                "note": (
                    "判据来自实盘成交回放，不是大样本统计：涨停时卖盘稀少，"
                    "要成交必须有卖单落在我前面；封单/流通是唯一有效的量化预判。"
                    "分档：<1% 可买 / 1-3% 需竞价判断 / 3-10% 基本买不到 / >10% 完全买不到。"
                ),
                "missing_fields_note": "无统计样本量，属机制性判据而非统计结论",
                "source": "无名游资 SKILL.md §3.5 + 打板工具包分层实测",
            },
        },
    ]

    created = []
    existing = current()
    for s in seeds:
        if s["rule_id"] in existing:
            continue
        rec = append(
            rule_id=s["rule_id"],
            scope=s["scope"],
            desc_human=s["desc_human"],
            expr=s["expr"],
            author="human:bootstrap",
            evidence=s["evidence"],
            indicator=s.get("indicator"),
            status_reason="从已验证存档导入；证据来自原包样本外回测，状态仍为 hypothesis",
        )
        created.append(rec)
    return created


def reset_for_tests() -> None:
    """仅供自检使用：清空规则与拒绝记录。"""
    for p in (paths.RULES, paths.REJECTED):
        if p.exists():
            p.unlink()


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    """python -m portal.virtual.rules [bootstrap|list|history|flags|rejected]"""
    import sys

    args = [a for a in (argv if argv is not None else sys.argv[1:]) if a]
    cmd = args[0] if args else "list"

    if cmd == "bootstrap":
        created = bootstrap_rules()
        if not created:
            print("种子规则已存在，未改动。")
            return 0
        for r in created:
            print(f"  写入 {r['rule_id']} v{r['version']} ({r['scope']}) — {r['desc_human']}")
        print(f"共导入 {len(created)} 条。状态均为 hypothesis，需过闸门才能激活。")
        return 0

    if cmd == "list":
        rows = summary_table()
        if not rows:
            print("规则库为空。先跑：python -m portal.virtual.rules bootstrap")
            return 0
        print(f"{'rule_id':22} {'v':>2} {'status':10} {'scope':16} "
              f"{'n':>6} {'mean%':>7} {'med%':>7} {'扣费%':>7}  说明")
        for r in rows:
            print(f"{r['rule_id']:22} {str(r['version']):>2} {str(r['status']):10} "
                  f"{str(r['scope']):16} {str(r['n']):>6} {str(r['mean_pct']):>7} "
                  f"{str(r['median_pct']):>7} {str(r['after_cost_pct']):>7}  {r['desc']}")
            for f in r["flags"]:
                print(f"{'':22}   ⚠ {f}")
        return 0

    if cmd == "history":
        if len(args) < 2:
            print("用法：python -m portal.virtual.rules history <rule_id>")
            return 2
        hist = history(args[1])
        if not hist:
            print(f"{args[1]} 没有历史记录")
            return 1
        for r in hist:
            print(f"  v{r['version']:>2} {r['status']:10} by {r.get('author')} "
                  f"at {r.get('created_at')} — {r.get('status_reason') or '(无理由记录)'}")
        return 0

    if cmd == "flags":
        bad = 0
        for rid, rec in sorted(current().items()):
            fl = evidence_flags(rec.get("evidence") or {})
            if fl:
                bad += 1
                print(f"{rid}:")
                for f in fl:
                    print(f"  ⚠ {f}")
        if not bad:
            print("所有规则的证据均未触发提示标记。")
        return 0

    if cmd == "rejected":
        recs = rejected_records()
        if not recs:
            print("没有被拒绝的假设记录。")
            return 0
        for r in recs:
            print(f"  {r.get('rule_id')}  at {r.get('created_at')} by {r.get('actor')}")
            print(f"    理由：{r.get('reason')}")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SCHEMA_VERSION",
    "STATUSES",
    "SCOPES",
    "INDICATORS",
    "ALLOWED_TRANSITIONS",
    "REQUIRED_ROLE",
    "RuleError",
    "validate",
    "all_records",
    "current",
    "get",
    "history",
    "by_status",
    "active",
    "by_scope",
    "next_version",
    "append",
    "transition",
    "revise",
    "record_rejection",
    "rejected_keys",
    "rejected_records",
    "evidence_flags",
    "summary_table",
    "bootstrap_rules",
    "reset_for_tests",
]
