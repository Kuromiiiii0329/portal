# -*- coding: utf-8 -*-
"""
portal/premarket/factors/discover.py — 因子自我迭代：开会 → 回测 → 入库

流程（每一步都可审计，写进 ledger）
----------------------------------
    1. 提议者：根据现有因子库与可用字段，提出若干候选因子（表达式或代码）
    2. 批评者：逐条挑毛病 —— 是否等于追高打板、是否有未来函数、是否过拟合、
               是否与已有因子重复、是否可计算
    3. 内核裁决（不是 LLM）：语法白名单 → 子进程试跑 → 覆盖率 → 统计检验（IC/IR）
    4. 入库或淘汰：通过写进 factors/discovered/，不通过写进 _retired/

为什么让批评者先说话
------------------
单让模型提因子，它会给出"涨幅越大越好"这种直接违反策略约束的东西。
用一个独立角色先审一遍，能把大部分垃圾挡在回测之前，省时间也省 token。

为什么最终裁决必须是内核
----------------------
LLM 的自我评价不可信。真正决定入库的是**可复现的数字**：IC、IR、覆盖率、
多空价差，以及"不得含涨停/追高条件"这条硬规则。
"""
from __future__ import annotations

import datetime
import json
import pprint
import re
import shutil
from pathlib import Path
from typing import Any, Callable

import pathlib

_DIR = pathlib.Path(__file__).parent
DISCOVERED = _DIR / "discovered"
RETIRED = _DIR / "discovered" / "_retired"
LEDGER = Path(__file__).parents[2] / "data" / "premarket" / "factors_ledger.jsonl"

#: 入库门槛（与 validate.py 保持一致，但这里更严一点：新因子必须真的有用）
MIN_IC = 0.02
MIN_IR = 0.20
MIN_COVERAGE = 0.50
MAX_CORR_WITH_EXISTING = 0.95      # 与已有因子相关性过高 → 视为重复

_ROLE_PROPOSER = """你是量化因子研究员。任务：提出**新的选股因子**。

硬约束（违反会被内核直接拒）：
- 禁止任何等价于「追高 / 打板」的因子（今日涨幅≥7%、涨停、连板数、封板资金等）。
- 不得使用未来数据（只能用当日收盘及以前的数据）。
- 因子必须对全市场可算，覆盖率要高。

可用数据（在 compute(panel) 里）：
  panel.codes()                 股票代码列表
  panel.close / prev_close / volume / amount_yi    当日报价（dict: code -> float）
  panel.open_today / high_today / low_today        当日开高低
  panel.chg                    当日涨跌幅(%)（dict）
  panel.closes(code) / highs(code) / lows(code) / vols(code)   历史序列（最近 40 日）
  panel.series_with_today(code)  历史收盘 + 今日收盘
  panel.hist_days(code)          历史天数
只允许 import 之外的白名单语法：算术、比较、for、if、字典/列表操作、
方法 get/items/keys/values/append/split/strip/startswith/endswith/join 等。
不允许 while、lambda、try、class、import、任何双下划线。

你也可以只写表达式（更安全）：给出 "expr"，用已有因子的 id 组合，例如
"vol_ratio * close_pos / 100"。

只输出 JSON：
{"proposals": [{
  "id": "snake_case_id", "name": "中文名", "kind": "numeric|bool",
  "description": "一句话说明", "hypothesis": "为什么它应该预测上涨",
  "expr": "（可选）表达式", 
  "code": "（可选）def compute(panel): ... 的完整源码",
  "plays": [{"id":"...","name":"...","buyable":true,
             "conditions":["my_factor >= 1.0", ...],
             "evidence":["my_factor"], "why":"..."}]
}]}
不要输出任何解释文字。"""

_ROLE_CRITIC = """你是因子审查员，负责在回测之前毙掉坏因子。

对每个候选逐条检查：
1. 是否等价于追高/打板（今日涨幅≥7%、涨停、连板、封板资金）→ 直接 verdict=reject
2. 是否使用未来数据（引用明天的收盘、后一天的开盘等）
3. 是否过拟合（条件太多、只对某几天有效、参数精确到小数点后三位）
4. 是否与已有因子高度重复（可在 expr 里直接用已有因子，没必要新写）
5. 是否真的可算（引用了不存在的字段、语法超出白名单）
6. 有没有明确的经济含义（说不出为什么的，不要）

只输出 JSON：
{"reviews": [{"id": "...", "verdict": "accept|revise|reject",
              "issues": ["..."], "suggestion": "..."}]}
不要输出任何解释文字。"""


# ─────────────────────────────────────────────────────────────
# ledger
# ─────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def ledger_append(event: dict, path: Path | None = None) -> None:
    p = path or LEDGER
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"at": _now(), **event}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ledger_read(path: Path | None = None) -> list[dict]:
    p = path or LEDGER
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ─────────────────────────────────────────────────────────────
# LLM 两段
# ─────────────────────────────────────────────────────────────
def _json_obj(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text or "", re.S)
    body = m.group(1) if m else (text or "")
    i, j = body.find("{"), body.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("模型没有返回 JSON 对象")
    return json.loads(body[i:j + 1])


def context_prompt(ctx: dict) -> str:
    return (f"【现有因子】{json.dumps(ctx['factors'], ensure_ascii=False)}\n"
            f"【现有战法】{json.dumps(ctx['plays'], ensure_ascii=False)}\n"
            f"【可用字段】{json.dumps(ctx['fields'], ensure_ascii=False)}")


def propose(ctx: dict, llm: Callable[[str], str], *, max_n: int = 3) -> list[dict]:
    text = llm(_ROLE_PROPOSER + "\n\n" + context_prompt(ctx)
               + f"\n\n请提出最多 {max_n} 个候选。")
    data = _json_obj(text)
    props = [p for p in (data.get("proposals") or []) if isinstance(p, dict)]
    return props[:max_n]


def critique(ctx: dict, props: list[dict],
             llm: Callable[[str], str]) -> dict[str, dict]:
    if not props:
        return {}
    text = llm(_ROLE_CRITIC + "\n\n" + context_prompt(ctx)
               + "\n\n【候选因子】" + json.dumps(props, ensure_ascii=False))
    data = _json_obj(text)
    out = {}
    for r in (data.get("reviews") or []):
        if isinstance(r, dict) and r.get("id"):
            out[str(r["id"])] = r
    return out


# ─────────────────────────────────────────────────────────────
# 内核闸门
# ─────────────────────────────────────────────────────────────
#: 禁止出现的关键词（追高/打板类）
_CHASE_HINTS = ("涨停", "连板", "封板", "打板", "追高", "追涨", "limit_up",
                "boards", "seal", "封单")


def _looks_like_chase(prop: dict) -> str | None:
    """内核侧的追高检查：不看你叫什么，看你算了什么。"""
    body = " ".join([str(prop.get("expr") or ""), str(prop.get("code") or "")])
    for play in prop.get("plays") or []:
        body += " " + " ".join(play.get("conditions") or [])
    low = body.lower()
    for hint in _CHASE_HINTS:
        if hint.lower() in low:
            return f"因子/战法里出现「{hint}」，属于追高打板类，不允许入库"
    m = re.search(r"chg_today\s*>\s*=?\s*(7|8|9|10)", body)
    if m:
        return f"条件里出现 chg_today >= {m.group(1)}，属于追高"
    return None


def _duplicate(prop: dict, existing: set[str]) -> str | None:
    """与现有因子重复 / 只是搬运原始字段 —— 这两种都没有信息增量。"""
    fid = str(prop.get("id") or "")
    if fid in existing:
        return f"因子 id {fid} 与现有因子重复"
    expr = str(prop.get("expr") or "").strip()
    if expr in existing:
        return f"表达式只有 {expr}，与已有因子完全等价（没有信息增量）"
    if expr in ("chg", "close", "volume", "prev_close", "limit_up",
                "open", "high", "low"):
        return f"表达式只是搬运原始字段 {expr}，没有加工"
    return None


def _factor_source(prop: dict) -> str:
    """把候选渲染成标准因子文件源码。"""
    meta = {
        "id": prop["id"], "name": prop.get("name") or prop["id"],
        "kind": prop.get("kind") or "numeric",
        "description": (prop.get("description") or "")[:120],
        "source": "discovered",
        "direction": prop.get("direction") or "high",
        "hypothesis": (prop.get("hypothesis") or "")[:200],
        "created_by": "factor-meeting",
        "created_at": _now(),
    }
    # ★ 必须用 Python 字面量而不是 JSON：json.dumps 会把 True 写成 true，
    #   生成的模块一 import 就 NameError（踩过）。
    # ★ 也不能写 `from ..base import safe`：子进程用 spec_from_file_location
    #   加载，没有包上下文，相对导入必然失败（也踩过）。
    lines = ["# -*- coding: utf-8 -*-",
             f'"""{meta["name"]} —— 由因子会议产出，内核校验后入库。',
             "",
             f"假设：{meta['hypothesis']}",
             f'作者：{meta["created_by"]}　时间：{meta["created_at"]}',
             '"""',
             "",
             f"META = {pprint.pformat(meta, indent=4, sort_dicts=False)}",
             ""]
    if prop.get("expr"):
        lines.append(f"EXPR = {json.dumps(prop['expr'], ensure_ascii=False)}")
        lines.append("")
    elif prop.get("code"):
        lines.append((prop["code"]).strip())
        lines.append("")
    if prop.get("plays"):
        lines.append(f"PLAYS = {pprint.pformat(prop['plays'], indent=4, sort_dicts=False)}")
        lines.append("")
    return "\n".join(lines)


def gate(conn, prop: dict, *, asof: str, sample_quotes: list[dict],
         days: int, log=print) -> dict[str, Any]:
    """内核裁决：语法 → 试跑 → 覆盖率 → 统计检验。返回 {ok, reason, stats}。"""
    from . import sandbox

    fid = str(prop.get("id") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,30}", fid):
        return {"ok": False, "reason": f"id 不合法（小写字母开头/下划线）：{fid!r}"}
    if not prop.get("expr") and not prop.get("code"):
        return {"ok": False, "reason": "既没有 expr 也没有 code"}

    chase = _looks_like_chase(prop)
    if chase:
        return {"ok": False, "reason": chase}
    try:
        from . import load_factors
        existing = set(load_factors())
    except Exception:
        existing = set()
    dup = _duplicate(prop, existing)
    if dup:
        return {"ok": False, "reason": dup}

    DISCOVERED.mkdir(parents=True, exist_ok=True)
    path = DISCOVERED / f"f_{fid}.py"
    src = _factor_source(prop)

    # 1) 语法白名单（代码因子）
    if prop.get("code"):
        try:
            sandbox.validate_code(prop["code"])
        except sandbox.SandboxError as exc:
            return {"ok": False, "reason": f"语法审查不通过：{exc}"}

    # 2) 落盘并试跑（代码因子走子进程，超时隔离）
    path.write_text(src, encoding="utf-8")
    if prop.get("code"):
        r = sandbox.run_in_subprocess(str(path), root=str(_DIR.parents[2]),
                                      quotes=sample_quotes, asof=asof)
        if not r.get("ok"):
            path.unlink(missing_ok=True)
            return {"ok": False, "reason": f"试跑失败：{r.get('error')}"}
        if r.get("n", 0) < 100:
            path.unlink(missing_ok=True)
            return {"ok": False, "reason": f"只算出 {r.get('n')} 只，覆盖太低"}

    # 3) 覆盖率 + 统计检验（复用 validate）
    try:
        from . import load_factors, all_plays
        from . import playbooks as PB
        load_factors(refresh=True)
        all_plays(refresh=True)
        plays = PB.load_all(refresh=True)
        PB.compiled(factor_ids=set(load_factors()), plays=plays)
    except Exception as exc:
        path.unlink(missing_ok=True)
        return {"ok": False, "reason": f"入库后加载失败：{type(exc).__name__}: {exc}"}

    from .validate import run_all
    try:
        res = run_all(days=days, conn=conn, ids=[fid], log=lambda *_: None)
    except Exception as exc:
        path.unlink(missing_ok=True)
        return {"ok": False, "reason": f"统计检验失败：{type(exc).__name__}: {exc}"}
    stats = res[0] if res else {"ok": False, "reason": "无检验结果"}

    ic = abs(stats.get("ic_mean") or 0)
    ir = abs(stats.get("ic_ir") or 0)
    cov = stats.get("coverage") or 0
    reasons = []
    if ic < MIN_IC:
        reasons.append(f"|IC|={ic:.4f} < {MIN_IC}")
    if ir < MIN_IR:
        reasons.append(f"|IR|={ir:.3f} < {MIN_IR}")
    if cov < MIN_COVERAGE:
        reasons.append(f"覆盖率 {cov:.0%} < {MIN_COVERAGE:.0%}")
    if not stats.get("ok"):
        reasons.append(f"检验未通过（{stats.get('reason')}）")
    if reasons:
        RETIRED.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(RETIRED / path.name))
        return {"ok": False, "reason": "；".join(reasons), "stats": stats}

    return {"ok": True, "reason": "通过", "stats": stats, "path": str(path)}


# ─────────────────────────────────────────────────────────────
# 主流程：开会
# ─────────────────────────────────────────────────────────────
def _ctx(conn) -> dict:
    from . import factor_summary, load_factors
    from . import playbooks as PB
    return {
        "factors": [{"id": r["id"], "name": r["name"], "kind": r["kind"],
                     "description": r["description"]}
                    for r in factor_summary()],
        "plays": [{"id": p["id"], "name": p["name"],
                   "buyable": p.get("buyable"),
                   "conditions": p.get("conditions")}
                  for p in PB.load_all()],
        "fields": ["close", "prev_close", "chg", "volume", "amount_yi",
                   "open_today", "high_today", "low_today",
                   "closes(code)", "highs(code)", "lows(code)", "vols(code)",
                   "series_with_today(code)", "hist_days(code)"],
    }


def run_meeting(*, conn=None, llm: Callable[[str], str] | None = None,
                max_n: int = 3, days: int = 10, dry_run: bool = False,
                asof: str | None = None, sample: int = 600,
                log=print) -> dict[str, Any]:
    """开一次因子会议：提议 → 批评 → 内核回测 → 入库/淘汰。"""
    from portal.virtual import marketdb as M
    from .__main__ import _quotes  # 复用离线行情回放

    own = conn is None
    if conn is None:
        conn = M.connect()
    if conn is None:
        return {"ok": False, "error": "market.db 不可用"}

    try:
        iso = asof or M.latest_trade_date(conn)
        d8 = (iso or "").replace("-", "")
        if not iso:
            return {"ok": False, "error": "库里没有交易日数据"}
        if llm is None:
            from ..agent import default_llm
            llm = default_llm(log)

        ctx = _ctx(conn)
        log(f"📚 现有 {len(ctx['factors'])} 个因子 / {len(ctx['plays'])} 个战法")

        props = propose(ctx, llm, max_n=max_n)
        log(f"💡 提议者提出 {len(props)} 个候选：{[p.get('id') for p in props]}")
        ledger_append({"event": "proposed", "date": d8,
                       "proposals": [{k: p.get(k) for k in
                                      ("id", "name", "hypothesis")} for p in props]})

        reviews = critique(ctx, props, llm)
        for p in props:
            rv = reviews.get(str(p.get("id"))) or {}
            log(f"   🧐 {p.get('id'):<20} {rv.get('verdict', '未评审')}"
                f" {'; '.join(rv.get('issues') or [])[:70]}")
        ledger_append({"event": "reviewed", "date": d8,
                       "reviews": reviews})

        quotes = _quotes(conn, d8)[:sample]
        results = []
        for p in props:
            rv = reviews.get(str(p.get("id"))) or {}
            if rv.get("verdict") == "reject":
                results.append({"id": p.get("id"), "ok": False,
                                "stage": "critic",
                                "reason": "批评者否决：" +
                                          "；".join((rv.get("issues") or [])[:2])})
                log(f"   ✗ {p.get('id')} 批评者否决")
                continue
            if dry_run:
                results.append({"id": p.get("id"), "ok": None,
                                "stage": "dry-run", "reason": "未落盘（dry-run）"})
                continue
            g = gate(conn, p, asof=iso, sample_quotes=quotes, days=days,
                     log=log)
            g["id"] = p.get("id")
            results.append(g)
            mark = "✓ 入库" if g.get("ok") else "✗ 淘汰"
            log(f"   {mark} {p.get('id')}：{g.get('reason')}")
            ledger_append({"event": "gated", "date": d8, "id": p.get("id"),
                           "ok": g.get("ok"), "reason": g.get("reason"),
                           "stats": g.get("stats")})

        accepted = [r for r in results if r.get("ok")]
        return {"ok": True, "date": iso, "proposed": len(props),
                "accepted": len(accepted), "results": results,
                "proposals": props, "reviews": reviews}
    finally:
        if own and conn is not None:
            conn.close()


def weekly(*, conn=None, days: int = 5, asof: str | None = None,
           log=print) -> dict[str, Any]:
    """阶段验证：用最近 days 个交易日复验已入库的发现型因子，退化的下线。"""
    from portal.virtual import marketdb as M
    from . import load_factors
    from .validate import run_all

    own = conn is None
    if conn is None:
        conn = M.connect()
    if conn is None:
        return {"ok": False, "error": "market.db 不可用"}
    try:
        iso = asof or M.latest_trade_date(conn)
        factors = load_factors(refresh=True)
        mine = [f for f in factors.values()
                if (f.meta.get("source") or "") == "discovered"]
        if not mine:
            return {"ok": True, "date": iso, "checked": 0, "retired": [],
                    "note": "发现型因子为空"}
        log(f"🔍 复验 {len(mine)} 个发现型因子（近 {days} 日）")
        res = run_all(days=days, conn=conn, ids=[f.id for f in mine],
                      log=lambda *_: None)
        retired = []
        for r in res:
            keep = (abs(r.get("ic_mean") or 0) >= MIN_IC
                    and abs(r.get("ic_ir") or 0) >= MIN_IR)
            if keep:
                log(f"   ✓ {r['id']} IC={r.get('ic_mean')} IR={r.get('ic_ir')}")
                continue
            # 发现型因子一定在 discovered/ 下，文件名与 id 一一对应
            path = DISCOVERED / f"f_{r['id']}.py"
            if path.exists():
                RETIRED.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(RETIRED / path.name))
                retired.append(r["id"])
                log(f"   ✗ {r['id']} 退化，已下线（IC={r.get('ic_mean')} "
                    f"IR={r.get('ic_ir')}）")
            ledger_append({"event": "weekly_retire", "date": iso,
                           "id": r["id"], "stats": r})
        ledger_append({"event": "weekly", "date": iso,
                       "checked": len(res), "retired": retired})
        return {"ok": True, "date": iso, "checked": len(res),
                "retired": retired, "results": res}
    finally:
        if own and conn is not None:
            conn.close()


__all__ = ["run_meeting", "weekly", "gate", "propose", "critique",
           "ledger_append", "ledger_read", "DISCOVERED", "RETIRED", "LEDGER",
           "MIN_IC", "MIN_IR", "MIN_COVERAGE"]
