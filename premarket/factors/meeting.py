# -*- coding: utf-8 -*-
"""
portal/premarket/factors/meeting.py — 因子讨论循环（强制代码回测）

与上一版的区别
--------------
上一版是「提议 → 批评 → 回测」的一次性流程：批评者**看不到回测数字**，
所以它的意见全是主观判断。现在按用户要求改成代码驱动的讨论循环：

    提出推演  →  **强制代码回测**（内核执行，不许口头声称）
              →  把回测数据交给 agent review + feedback
              →  可行则 discuss 改善 → **二次回测**
              →  不理想则回溯到历史最佳变体，换方向再来
              →  直到 review 与反馈都确认「可尝试」（且内核数字达标）才结束

结论**只能**来自可复现的数字。agent 的评审是意见，内核的门槛是否决权。

可配置
------
`MAX_DISCUSS_ROUNDS`（本文件顶部，默认 8）—— 讨论轮次上限，写在代码里，
也可以用 `--max-rounds` 临时覆盖。到上限仍未通过就如实报告失败并清理落盘文件。

回溯（backtrack）语义
--------------------
每一轮都算一个 score：

    score = |IC| × min(1, |IR| / 0.30) × 覆盖率

即"有预测力、且稳定、且覆盖面够"。循环始终记住**历史最佳变体**；
当某轮改坏了（score 明显低于最佳，或评审要求回溯），下一轮就回到最佳变体，
而不是在一条越改越差的路上继续走。
"""
from __future__ import annotations

import datetime
import json
import pathlib
import pprint
import re
import shutil
from typing import Any, Callable

from . import memory, sandbox

_DIR = pathlib.Path(__file__).parent
DISCOVERED = _DIR / "discovered"
RETIRED = _DIR / "discovered" / "_retired"
LEDGER = pathlib.Path(__file__).parents[2] / "data" / "premarket" / "factors_ledger.jsonl"

# ─────────────────────────────────────────────────────────────
# ★ 讨论轮次上限（用户可控，写死在代码里；CLI 可临时覆盖）
# ─────────────────────────────────────────────────────────────
MAX_DISCUSS_ROUNDS = 8

#: 入库门槛（内核判据，不是 agent 说了算）
MIN_IC = 0.02
MIN_IR = 0.20
MIN_COVERAGE = 0.50
#: 评分里 IR 达到这个值即视作"足够稳定"，不再加分
IR_FULL_MARK = 0.30
#: 某轮 score 低于历史最佳的这个比例 → 视为改坏了，触发回溯
BACKTRACK_RATIO = 0.80

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
只允许的语法：算术、比较、for、if、字典/列表操作、方法
get/items/keys/values/append/split/strip/startswith/endswith/join 等。
不允许 while、lambda、try、class、import、任何双下划线。

也可以只写表达式（更安全）：给出 "expr"，用已有因子的 id 组合。

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

_ROLE_REVIEWER = """你是因子评审员。下面给你**已经跑完代码回测**的因子：源码、以及内核算出的真实数字。

你的职责不是猜，而是**读数字**：
- IC（信息系数，因子值与次日收益的秩相关）绝对值 > 0.02 才算有点预测力
- IR（IC 均值/标准差）> 0.20 才算稳定，不靠某几天
- 覆盖率 < 50% 说明因子只对少数股票有效，不可用
- 多空价差（前 10% 组减后 10% 组）为正才说明方向对

然后给出结论：
- accept   ：数字达标且逻辑站得住，建议入库试用
- improve  ：方向对但数字不够，指出**下一轮具体改哪里**（改参数/换窗口/加条件）
- backtrack：这次改坏了（比之前更差），建议回到之前的版本换方向
- give_up  ：这条路走不通，别浪费时间

注意：你不能决定入库，只有内核数字能。你只负责给出下一步方向。

只输出 JSON：
{"verdict": "accept|improve|backtrack|give_up",
 "issues": ["读数字看出的问题"],
 "suggestions": ["具体的改法"],
 "next_change": "一句话：下一轮改哪一处"}
不要输出任何解释文字。"""


# ─────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def ledger_append(event: dict, path: pathlib.Path | None = None) -> None:
    p = path or LEDGER
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"at": _now(), **event}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ledger_read(path: pathlib.Path | None = None) -> list[dict]:
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


def _json_obj(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text or "", re.S)
    body = m.group(1) if m else (text or "")
    i, j = body.find("{"), body.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("模型没有返回 JSON 对象")
    return json.loads(body[i:j + 1])


def _factor_source(prop: dict) -> str:
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
    lines = ["# -*- coding: utf-8 -*-",
             f'"""{meta["name"]} —— 由因子讨论循环产出，内核回测通过后入库。',
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


_CHASE_HINTS = ("涨停", "连板", "封板", "打板", "追高", "追涨", "limit_up",
                "boards", "seal", "封单")


def _looks_like_chase(prop: dict) -> str | None:
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


def score(stats: dict) -> float:
    """评分：有预测力 × 稳定 × 覆盖。用于回溯判断，不是入库判据。"""
    ic = abs(float(stats.get("ic_mean") or 0))
    ir = abs(float(stats.get("ic_ir") or 0))
    cov = float(stats.get("coverage") or 0)
    return round(ic * min(1.0, ir / IR_FULL_MARK) * cov, 6)


def _gate_ok(stats: dict) -> tuple[bool, str]:
    """内核最终判据。agent 说 accept 也不算数。"""
    if not stats:
        return False, "没有回测数据"
    if not stats.get("ok"):
        return False, f"统计检验未通过：{stats.get('reason')}"
    ic = abs(float(stats.get("ic_mean") or 0))
    ir = abs(float(stats.get("ic_ir") or 0))
    cov = float(stats.get("coverage") or 0)
    bad = []
    if ic < MIN_IC:
        bad.append(f"|IC|={ic:.4f}<{MIN_IC}")
    if ir < MIN_IR:
        bad.append(f"|IR|={ir:.3f}<{MIN_IR}")
    if cov < MIN_COVERAGE:
        bad.append(f"覆盖率{cov:.0%}<{MIN_COVERAGE:.0%}")
    return (not bad), ("；".join(bad) if bad else "达标")


def decide(bt: dict, verdict: str, best: dict | None) -> tuple[str, str]:
    """一轮的最终决策。返回 (decision, 说明)。

    规则（顺序即优先级）：
    1. 内核不达标 → **永远不能 accept**，评审说 accept 也改判 improve
    2. 内核达标 + 评审 accept → accept
    3. 内核达标 + 评审 improve → accept（先入库，意见留给后续迭代）
    4. 评审说 give_up → give_up
    5. 得分明显低于历史最佳 → backtrack
    6. 其余 → improve
    """
    ok = bool(bt.get("ok"))
    score_v = bt.get("score")
    if not ok and verdict == "accept":
        return "improve", "内核未达标，评审的 accept 无效"
    if ok and verdict in ("accept", "improve"):
        return "accept", ("数字达标且评审确认" if verdict == "accept"
                          else "数字达标，评审建议继续改进（先入库）")
    if verdict == "give_up":
        return "give_up", "评审判定走不通"
    if (best and score_v is not None
            and score_v < (best.get("score") or 0) * BACKTRACK_RATIO):
        return "backtrack", f"得分 {score_v} 低于最佳 {best.get('score')}"
    return "improve", "继续改进"


# ─────────────────────────────────────────────────────────────
# 强制回测（代码执行，不许口头声称）
# ─────────────────────────────────────────────────────────────
def backtest(conn, prop: dict, *, asof: str, days: int,
             sample_quotes: list[dict], log=print) -> dict[str, Any]:
    """把候选落盘 → 沙箱 → 子进程试跑 → 统计检验。返回真实数字。

    这一步是**强制的**：任何一轮都必须经过它才能进入评审。
    语法/运行失败也算一轮结果（stage=syntax/runtime），不是跳过。
    """
    fid = str(prop.get("id") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,30}", fid):
        return {"ok": False, "stage": "id", "reason": f"id 不合法：{fid!r}"}
    if not prop.get("expr") and not prop.get("code"):
        return {"ok": False, "stage": "empty", "reason": "既没有 expr 也没有 code"}
    chase = _looks_like_chase(prop)
    if chase:
        return {"ok": False, "stage": "chase", "reason": chase}
    try:
        from . import load_factors
        existing = set(load_factors())
    except Exception:
        existing = set()
    # 允许与"自己上一轮的版本"重名：本轮内同名覆盖属正常迭代，
    # 只有与库里其它因子冲突才算重复。
    dup = _duplicate(prop, existing - {fid})
    if dup:
        return {"ok": False, "stage": "duplicate", "reason": dup}

    DISCOVERED.mkdir(parents=True, exist_ok=True)
    path = DISCOVERED / f"f_{fid}.py"
    path.write_text(_factor_source(prop), encoding="utf-8")

    if prop.get("code"):
        try:
            sandbox.validate_code(prop["code"])
        except sandbox.SandboxError as exc:
            path.unlink(missing_ok=True)
            return {"ok": False, "stage": "syntax", "reason": str(exc)}
        r = sandbox.run_in_subprocess(str(path), root=str(_DIR.parents[2]),
                                      quotes=sample_quotes, asof=asof)
        if not r.get("ok"):
            path.unlink(missing_ok=True)
            return {"ok": False, "stage": "runtime",
                    "reason": str(r.get("error"))[:200]}
        if r.get("n", 0) < 100:
            path.unlink(missing_ok=True)
            return {"ok": False, "stage": "coverage0",
                    "reason": f"只算出 {r.get('n')} 只，覆盖太低"}

    try:
        from . import all_plays, load_factors
        from . import playbooks as PB
        load_factors(refresh=True)
        all_plays(refresh=True)
        PB.compiled(factor_ids=set(load_factors()), plays=PB.load_all(refresh=True))
    except Exception as exc:
        path.unlink(missing_ok=True)
        return {"ok": False, "stage": "load",
                "reason": f"加载失败：{type(exc).__name__}: {exc}"}

    from .validate import run_all
    try:
        res = run_all(days=days, conn=conn, ids=[fid], log=lambda *_: None)
    except Exception as exc:
        path.unlink(missing_ok=True)
        return {"ok": False, "stage": "stats",
                "reason": f"统计检验失败：{type(exc).__name__}: {exc}"}
    stats = res[0] if res else None
    if not stats:
        path.unlink(missing_ok=True)
        return {"ok": False, "stage": "stats", "reason": "没有检验结果"}
    ok, why = _gate_ok(stats)
    return {"ok": ok, "stage": "backtest", "reason": why,
            "stats": stats, "path": str(path), "score": score(stats)}


# ─────────────────────────────────────────────────────────────
# 讨论循环
# ─────────────────────────────────────────────────────────────
def _ctx(conn) -> dict:
    from . import factor_summary
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
        "lessons": memory.prompt_block(),
    }


def _context_prompt(ctx: dict) -> str:
    parts = [ctx.get("lessons") or "",
             f"【现有因子】{json.dumps(ctx['factors'], ensure_ascii=False)}",
             f"【现有战法】{json.dumps(ctx['plays'], ensure_ascii=False)}",
             f"【可用字段】{json.dumps(ctx['fields'], ensure_ascii=False)}"]
    return "\n".join(p for p in parts if p)


def propose(ctx: dict, llm: Callable[[str], str], *,
            history: list[dict] | None = None, base: dict | None = None,
            max_n: int = 1) -> list[dict]:
    """提议或修订。history 非空时进入「修订模式」。"""
    prompt = _ROLE_PROPOSER + "\n\n" + _context_prompt(ctx)
    if history:
        prompt += (
            "\n\n【修订模式】下面是你之前提过的版本与**真实回测结果**，"
            "以及评审意见。请**只改一处**并给出新版本（沿用或换 id 均可）：\n"
            + json.dumps({"history": history[-3:], "base": base},
                         ensure_ascii=False)[:6000])
    prompt += f"\n\n请提出最多 {max_n} 个候选。"
    data = _json_obj(llm(prompt))
    return [p for p in (data.get("proposals") or []) if isinstance(p, dict)][:max_n]


def review(ctx: dict, prop: dict, bt: dict, history: list[dict],
           llm: Callable[[str], str]) -> dict:
    """把**回测数字**交给评审员，要它给下一步方向。"""
    payload = {
        "factor": {k: prop.get(k) for k in
                   ("id", "name", "hypothesis", "description", "expr", "code")},
        "plays": prop.get("plays") or [],
        "backtest": bt.get("stats") or {},
        "gate": {"ok": bt.get("ok"), "stage": bt.get("stage"),
                 "reason": bt.get("reason"), "score": bt.get("score")},
        "history": [{k: h.get(k) for k in ("round", "id", "score", "ic", "ir",
                                           "coverage", "verdict", "decision")}
                    for h in history[-5:]],
    }
    text = llm(_ROLE_REVIEWER + "\n\n" + _context_prompt(ctx)
               + "\n\n【本轮待评审】" + json.dumps(payload, ensure_ascii=False)[:8000])
    data = _json_obj(text)
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in ("accept", "improve", "backtrack", "give_up"):
        verdict = "improve"
    return {"verdict": verdict,
            "issues": [str(x)[:160] for x in (data.get("issues") or [])][:6],
            "suggestions": [str(x)[:160] for x in (data.get("suggestions") or [])][:6],
            "next_change": str(data.get("next_change") or "")[:200]}


def discuss(*, conn=None, llm: Callable[[str], str] | None = None,
            max_rounds: int | None = None, days: int = 10,
            asof: str | None = None, sample: int = 600,
            dry_run: bool = False, log=print) -> dict[str, Any]:
    """带轮次上限的讨论循环。每轮都必须先跑代码回测。"""
    from portal.virtual import marketdb as M
    from .__main__ import _quotes

    rounds_cap = int(max_rounds or MAX_DISCUSS_ROUNDS)
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
        quotes = _quotes(conn, d8)[:sample] if not dry_run else []
        log(f"📚 现有 {len(ctx['factors'])} 因子 / {len(ctx['plays'])} 战法　"
            f"轮次上限 {rounds_cap}　回测 {days} 日")
        if ctx.get("lessons"):
            log(f"🧠 已注入历史经验 {len(memory.lessons())} 条")

        history: list[dict] = []
        best: dict | None = None
        accepted: dict | None = None
        base: dict | None = None

        for rnd in range(1, rounds_cap + 1):
            log(f"\n─── 第 {rnd}/{rounds_cap} 轮 ───")
            try:
                props = propose(ctx, llm, history=history if rnd > 1 else None,
                                base=base, max_n=1)
            except Exception as exc:
                log(f"   ✗ 提议失败：{type(exc).__name__}: {exc}")
                history.append({"round": rnd, "id": None,
                                "decision": "propose_error",
                                "reason": str(exc)[:120]})
                continue
            if not props:
                log("   ✗ 模型没有给出候选")
                history.append({"round": rnd, "id": None, "decision": "empty"})
                continue
            prop = props[0]
            log(f"   💡 {prop.get('id')}：{str(prop.get('hypothesis'))[:70]}")

            if dry_run:
                history.append({"round": rnd, "id": prop.get("id"),
                                "decision": "dry-run"})
                log("   (dry-run) 不落盘、不回测")
                base = prop
                continue

            # ★ 强制代码回测
            bt = backtest(conn, prop, asof=iso, days=days,
                          sample_quotes=quotes, log=log)
            st = bt.get("stats") or {}
            log(f"   📊 回测：IC={st.get('ic_mean')} IR={st.get('ic_ir')} "
                f"覆盖={st.get('coverage')} score={bt.get('score')} "
                f"→ {'达标' if bt.get('ok') else bt.get('reason')}")

            rv = review(ctx, prop, bt, history, llm)
            log(f"   🧐 评审：{rv['verdict']}"
                f"{'　' + rv['next_change'] if rv.get('next_change') else ''}")

            decision, why = decide(bt, rv["verdict"], best)
            if decision != rv["verdict"]:
                log(f"   ⚖ 内核改判：评审={rv['verdict']} → 决策={decision}（{why}）")
            elif decision in ("backtrack", "give_up"):
                log(f"   ↩ {decision}：{why}")

            history.append({
                "round": rnd, "id": prop.get("id"),
                "hypothesis": str(prop.get("hypothesis"))[:160],
                "score": bt.get("score"),
                "ic": st.get("ic_mean"), "ir": st.get("ic_ir"),
                "coverage": st.get("coverage"),
                "gate_ok": bt.get("ok"), "gate_reason": bt.get("reason"),
                "verdict": rv["verdict"], "issues": rv["issues"],
                "next_change": rv["next_change"],
                "decision": decision, "code": prop.get("code"),
                "expr": prop.get("expr"), "plays": prop.get("plays")})

            if bt.get("score") is not None and (
                    best is None or (bt["score"] or 0) > (best.get("score") or 0)):
                best = {"prop": prop, "score": bt["score"], "stats": st}

            if decision == "accept" and bt.get("ok"):
                accepted = {"prop": prop, "bt": bt, "round": rnd}
                log(f"   ✅ 第 {rnd} 轮通过：内核数字达标且评审确认可尝试")
                break
            if decision == "give_up":
                log("   ⛔ 评审判定这条路走不通，提前结束")
                break
            if decision == "backtrack" and best:
                base = best["prop"]
                log("   ↩ 以历史最佳变体为基准继续")
            else:
                base = prop
            ledger_append({"event": "round", "date": d8, "round": rnd,
                           "id": prop.get("id"), "score": bt.get("score"),
                           "gate_ok": bt.get("ok"), "decision": decision,
                           "verdict": rv["verdict"]})

        # ── 收尾：入库或清理 ──
        if accepted:
            log(f"\n🎉 入库 {accepted['prop'].get('id')}（第 {accepted['round']} 轮）")
            # ★ 两件事：把这次讨论的完整过程写成档案；把回测数字存进缓存
            try:
                from . import catalog
                rec = catalog.write_admission_record(
                    prop=accepted["prop"], bt=accepted["bt"],
                    history=history, rounds_cap=rounds_cap)
                catalog.save_stats({accepted["prop"]["id"]:
                                    accepted["bt"].get("stats") or {}})
                log(f"   📄 入库档案：{rec}")
            except Exception as exc:
                log(f"   ⚠️ 写入库档案失败：{type(exc).__name__}: {exc}")
        else:
            for h in history:
                if h.get("id"):
                    p = DISCOVERED / f"f_{h['id']}.py"
                    if p.exists():
                        RETIRED.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(p), str(RETIRED / p.name))
            log(f"\n⚠️ {rounds_cap} 轮内未通过，已清理落盘文件")

        meeting = {"ok": True, "date": iso, "rounds_cap": rounds_cap,
                   "rounds_used": len(history), "accepted": bool(accepted),
                   "accepted_id": (accepted["prop"].get("id") if accepted else None),
                   "best_score": best.get("score") if best else None,
                   "history": history}
        memory.save_transcript({"date": iso, "mode": "discuss",
                                "rounds_cap": rounds_cap,
                                "history": history,
                                "accepted": meeting["accepted_id"]})
        fresh = memory.distill(meeting)
        if llm is not None:
            try:
                fresh += memory.distill_by_llm(meeting, llm)
            except Exception as exc:
                log(f"   ⚠️ LLM 提炼经验失败：{type(exc).__name__}: {exc}")
        meeting["lessons_added"] = len(fresh)
        ledger_append({"event": "discussed", "date": d8,
                       "rounds": len(history),
                       "accepted": meeting["accepted_id"],
                       "lessons": len(fresh)})
        log(f"🧠 经验库：本次 +{len(fresh)} 条（当前 "
            f"{memory.stats()['lessons_active']} 条有效）")
        try:
            from . import catalog
            r = catalog.write_catalog(quiet=True)
            if r.get("ok"):
                log(f"📚 因子库总表已刷新（{r['factors']} 因子 / "
                    f"{r['plays']} 战法）")
        except Exception:
            pass
        return meeting
    finally:
        if own and conn is not None:
            conn.close()


def run_meeting(**kwargs) -> dict[str, Any]:
    """兼容旧入口：单轮等价于 discuss(max_rounds=1)。"""
    kwargs.setdefault("max_rounds", 1)
    return discuss(**kwargs)


def weekly(*, conn=None, days: int = 5, asof: str | None = None,
           log=print) -> dict[str, Any]:
    """阶段验证：复验已入库的发现型因子，退化的下线。"""
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
            ok, why = _gate_ok(r)
            if ok:
                log(f"   ✓ {r['id']} IC={r.get('ic_mean')} IR={r.get('ic_ir')}")
                continue
            path = DISCOVERED / f"f_{r['id']}.py"
            if path.exists():
                RETIRED.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(RETIRED / path.name))
                retired.append(r["id"])
                log(f"   ✗ {r['id']} 退化下线（{why}）")
            ledger_append({"event": "weekly_retire", "date": iso,
                           "id": r["id"], "reason": why, "stats": r})
        ledger_append({"event": "weekly", "date": iso,
                       "checked": len(res), "retired": retired})
        try:
            from . import catalog
            catalog.save_stats({r["id"]: r for r in res if r.get("id")})
            catalog.write_catalog(quiet=True)
        except Exception as exc:
            log(f"   ⚠️ 刷新因子库总表失败：{type(exc).__name__}: {exc}")
        return {"ok": True, "date": iso, "checked": len(res),
                "retired": retired, "results": res}
    finally:
        if own and conn is not None:
            conn.close()


__all__ = ["discuss", "run_meeting", "weekly", "backtest", "propose",
           "review", "score", "decide", "ledger_append", "ledger_read",
           "DISCOVERED", "RETIRED", "LEDGER",
           "MAX_DISCUSS_ROUNDS", "MIN_IC", "MIN_IR", "MIN_COVERAGE",
           "BACKTRACK_RATIO"]
