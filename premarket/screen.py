# -*- coding: utf-8 -*-
"""
portal/premarket/screen.py — 用因子库跑战法筛选（午报/晚报共用）

这个文件只负责「组织」：构造 Panel → 跑因子 → 按战法组合判定 → 输出候选。
**规则都在 factors/ 里**：

    factors/f_*.py          单个因子的计算（一个文件一个因子）
    factors/playbooks.py    战法 = 因子的布尔组合（阈值改这里）
    factors/validate.py     因子统计检验（IC / 多空价差 / 覆盖率）

★ 关键契约（任务要求）
---------------------
**新增一个 `factors/f_xxx.py`，下一次报告就会用它筛选候选股。**

实现方式：因子文件除了 `META` + `compute(panel)`，还可以带 `PLAYS`
（该因子参与的战法定义）。注册表把内置战法与这些 PLAYS 合并，
`screen()` 由午报和晚报共同调用 —— 所以新因子只落一个文件就生效。

历史背景（为什么要有这一层）
--------------------------
第一版候选池只有涨停池/强势股池/龙虎榜，实测 40 只候选 **40 只已封涨停**，
而内核禁止打板 → 可买 0 只，账户永远是 100.00。那不是谨慎，是结构上不可能成交。

现在补上多类买点战法，涨停股降级为 `buyable=False` 的观察标的，由内核拒绝买入。
"""
from __future__ import annotations

from typing import Any, Iterable

from .factors import build_panel, evaluate, load_factors
from .factors import playbooks as PB
from .factors.base import Panel

#: 历史不足这么多天，因子战法整体跳过（不拿残缺数据硬算形态）
MIN_HISTORY_DAYS = 25
#: 每个战法最多给多少只
PER_PLAYBOOK_CAP = 12
#: 涨停观察（不可买）最多几只
LIMIT_UP_CAP = 15
#: 至少要这么多只股票有足够历史，才认为因子库可用
MIN_DEEP_CODES = 500

#: 兼容旧引用
TOL_LIMIT = 0.6


def _limit_pct(code: str) -> float:
    """涨跌停幅度：创业板/科创板 20%，其余 10%（ST 未单独处理，偏保守）。"""
    return 20.0 if code[:2] in ("68", "30") else 10.0


def playbook_names() -> dict[str, str]:
    """{playbook_id: 中文名}（含因子文件贡献的战法）。"""
    return {p["id"]: p.get("name") or p["id"] for p in PB.load_all()}


def buyable_playbooks() -> tuple[str, ...]:
    return tuple(PB.buyable_ids(PB.load_all()))


def observe_pool(rows: Iterable[dict], *, cap: int = LIMIT_UP_CAP,
                 name: str = "涨停/连板（仅观察）",
                 why: str = "当日已封涨停：只进观察表等回调，内核禁止打板买入",
                 playbook: str = "limit_up_chase") -> list[dict]:
    """把原始池子（涨停/强势/龙虎榜）标成「仅观察」候选。午报晚报共用。"""
    out = []
    for r in rows:
        if not r.get("code"):
            continue
        c = dict(r)
        c.update({"playbook": playbook, "playbook_cn": name,
                  "buyable": False, "why": why,
                  "setup": [], "metrics": {}})
        out.append(c)
    return out[:cap]


def _rows(panel: Panel, vals: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把 {factor: {code: v}} 转成 {code: {factor: v}}，便于按条件求值。

    门槛按**真正产出过值的因子**算，不是按注册表总数算 ——
    否则某个因子在某种数据下必然为空（例如没有当日开高低时的 close_pos），
    会把所有股票一起判成"因子不全"而全部丢掉（踩过：截面直接变 0 只）。
    """
    active = [fid for fid, series in vals.items() if series]
    need = max(1, len(active) - 1)          # 允许缺 1 个（例如箱体不足 20 日）
    rows: dict[str, dict[str, Any]] = {}
    for code in panel.codes():
        row = {}
        for fid in active:
            series = vals[fid]
            if code in series:
                row[fid] = series[code]
        if len(row) >= need:
            rows[code] = row
    return rows


def screen(conn, quotes: Iterable[dict], asof_iso: str,
           *, log=lambda *_: None) -> tuple[list[dict], str | None]:
    """跑全部买入战法（内置 + 因子文件贡献的）。返回 (候选列表, 不可用原因)。"""
    quotes = list(quotes)
    panel = build_panel(conn, quotes, asof_iso)
    deep = sum(1 for c in panel.codes()
               if len(panel.closes(c)) >= MIN_HISTORY_DAYS)
    if deep < MIN_DEEP_CODES:
        return [], (f"历史K线不足（只有 {deep} 只股票有 ≥{MIN_HISTORY_DAYS} 日），"
                    f"因子战法需要 MA20/箱体/量比，已跳过")

    try:
        factors = load_factors()
        plays = PB.load_all()
        vals = evaluate(panel, factors)
        exprs = PB.compiled(factor_ids=set(factors), plays=plays)
    except Exception as exc:
        return [], f"因子库加载失败：{type(exc).__name__}: {exc}"

    rows = _rows(panel, vals)
    log(f"  因子库 {len(factors)} 个因子 / {len(plays)} 个战法，"
        f"可算截面 {len(rows)} 只，历史充足 {deep} 只")

    out: list[dict] = []
    for pid, conds in exprs.items():
        pb = PB.get(pid, plays) or {"id": pid, "name": pid, "buyable": False}
        try:
            PB.check_no_chase(pb)
        except Exception as exc:
            log(f"  ⚠️ 战法 {pid} 被拒：{exc}")
            continue
        hits = []
        for code, row in rows.items():
            # 涨停/追高的票一律不进买入池（与内核禁令一致，双保险）
            chg = panel.chg.get(code)
            up = panel.limit_up.get(code)
            px = panel.close.get(code)
            if up and px and px >= up - 0.005:
                continue
            if chg is not None and chg >= 7.0:
                continue
            ok, evidence = PB.match(pid, row, conds)
            if not ok:
                continue
            metrics = {k: row.get(k) for k in (pb.get("evidence") or [])}
            # 证据要带**实际值**，否则报告里只能看到"条件成立"，看不出有多成立
            setup = [f"{k}={PB.fmt_value(metrics.get(k))}"
                     for k in (pb.get("evidence") or [])]
            hits.append({
                "code": code,
                "name": panel.names.get(code),
                "close": round(px, 2) if px else None,
                "prev_close": round(panel.prev_close.get(code), 2)
                if panel.prev_close.get(code) else None,
                "chg_pct": round(chg, 2) if chg is not None else None,
                "limit_up": up,
                "playbook": pid,
                "playbook_cn": pb.get("name"),
                "buyable": bool(pb.get("buyable")),
                "sources": [pb.get("name")],
                "why": pb.get("why"),
                "setup": setup,
                "conditions": evidence,
                "metrics": metrics,
                "boards": None, "industry": None, "seal_yi": None,
                "amount_yi": (round(row["amount_yi"], 2)
                              if row.get("amount_yi") is not None else None),
            })
        # 排序方向由战法自己声明（回踩=越缩量越好，突破=越放量越好）
        for fid, desc in reversed(PB.sort_spec(pb)):
            hits.sort(key=lambda x, f=fid: (
                x["metrics"].get(f) is None,
                -(x["metrics"].get(f) or 0) if desc
                else (x["metrics"].get(f) if x["metrics"].get(f) is not None
                      else float("inf"))))
        log(f"  {pb.get('name')}：命中 {len(hits)} 只"
            f"（取前 {min(len(hits), PER_PLAYBOOK_CAP)}）")
        out.extend(hits[:PER_PLAYBOOK_CAP])
    return out, None


def group_by_playbook(cands: list[dict]) -> dict[str, list[dict]]:
    """按战法分组，报告里分区展示用。"""
    out: dict[str, list[dict]] = {}
    for c in cands or []:
        out.setdefault(c.get("playbook") or "other", []).append(c)
    return out


__all__ = ["screen", "observe_pool", "group_by_playbook", "playbook_names",
           "buyable_playbooks", "PER_PLAYBOOK_CAP", "LIMIT_UP_CAP",
           "MIN_HISTORY_DAYS", "_limit_pct", "TOL_LIMIT"]
