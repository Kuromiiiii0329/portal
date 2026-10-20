# -*- coding: utf-8 -*-
"""
portal/premarket/screen.py — 用因子库跑战法筛选

这个文件只负责「组织」：构造 Panel → 跑因子 → 按战法组合判定 → 输出候选。
**规则都在 factors/ 里**：

    factors/f_*.py          单个因子的计算（一个文件一个因子）
    factors/playbooks.py    战法 = 因子的布尔组合（阈值改这里）
    factors/validate.py     因子统计检验（IC / 多空价差 / 覆盖率）

历史背景（为什么要有这一层）
--------------------------
第一版候选池只有涨停池/强势股池/龙虎榜，实测 40 只候选 **40 只已封涨停**，
而内核禁止打板 → 可买 0 只，账户永远是 100.00。那不是谨慎，是结构上不可能成交。

现在补上三类买点战法（首板回踩 / 平台突破 / 温和趋势），涨停股降级为
`buyable=False` 的观察标的，由内核拒绝买入。
"""
from __future__ import annotations

from typing import Any, Iterable

from .factors import build_panel, evaluate, load_factors
from .factors import playbooks as PB
from .factors.base import Panel

#: 历史不足这么多天，A/B/C 战法整体跳过（不拿残缺数据硬算形态）
MIN_HISTORY_DAYS = 25
#: 每个战法最多给多少只
PER_PLAYBOOK_CAP = 12
#: 涨停观察（不可买）最多几只
LIMIT_UP_CAP = 15
#: 至少要这么多只股票有足够历史，才认为因子库可用
MIN_DEEP_CODES = 500

PLAYBOOK_CN = {p["id"]: p["name"] for p in PB.PLAYBOOKS}
BUYABLE = tuple(PB.buyable_ids())

# 兼容旧引用（report/collect 里按这个判断超限）
TOL_LIMIT = 0.6


def _limit_pct(code: str) -> float:
    """涨跌停幅度：创业板/科创板 20%，其余 10%（ST 未单独处理，偏保守）。"""
    return 20.0 if code[:2] in ("68", "30") else 10.0


def _rows(panel: Panel, vals: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把 {factor: {code: v}} 转成 {code: {factor: v}}，便于按条件求值。"""
    rows: dict[str, dict[str, Any]] = {}
    for code in panel.codes():
        row = {}
        for fid, series in vals.items():
            if code in series:
                row[fid] = series[code]
        if len(row) >= max(1, len(vals) - 1):     # 允许缺 1 个因子（例如箱体）
            rows[code] = row
    return rows


def screen(conn, quotes: Iterable[dict], asof_iso: str,
           *, log=lambda *_: None) -> tuple[list[dict], str | None]:
    """跑全部买入战法。返回 (候选列表, 不可用原因)。

    每只候选带 `playbook` / `playbook_cn` / `buyable` / `setup`（逐条证据）。
    """
    quotes = list(quotes)
    panel = build_panel(conn, quotes, asof_iso)
    deep = sum(1 for c in panel.codes()
               if len(panel.closes(c)) >= MIN_HISTORY_DAYS)
    if deep < MIN_DEEP_CODES:
        return [], (f"历史K线不足（只有 {deep} 只股票有 ≥{MIN_HISTORY_DAYS} 日），"
                    f"A/B/C 战法需要 MA20/箱体/量比，已跳过")

    try:
        factors = load_factors()
        vals = evaluate(panel, factors)
        exprs = PB.compiled(factor_ids=set(factors))
    except Exception as exc:
        return [], f"因子库加载失败：{type(exc).__name__}: {exc}"

    rows = _rows(panel, vals)
    log(f"  因子库 {len(factors)} 个，可算截面 {len(rows)} 只，"
        f"历史充足的 {deep} 只")

    out: list[dict] = []
    for pid, conds in exprs.items():
        pb = PB.get(pid) or {}
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
        spec = PB.sort_spec(pb)
        for fid, desc in reversed(spec):
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


__all__ = ["screen", "group_by_playbook", "PLAYBOOK_CN", "BUYABLE",
           "PER_PLAYBOOK_CAP", "LIMIT_UP_CAP", "MIN_HISTORY_DAYS",
           "_limit_pct"]
