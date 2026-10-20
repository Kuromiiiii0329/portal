# -*- coding: utf-8 -*-
"""
portal/premarket/factors/catalog.py — 因子库总表（自动维护）

产出两份东西，都**由代码生成**，不手写：

    docs/factor-library.md              人读的总表（Markdown）
    data/premarket/factor_catalog.json  机器读的快照

每个因子记录：
    基本    id / 名称 / 类型 / 来源 / 假设
    战法    它参与哪些战法（把 conditions 里出现的因子名反查回去）
    回测    最近一次的 IC / IR / 覆盖率 / 多空价差 / 检验天数
    打分    score = |IC| × min(1,|IR|/0.3) × 覆盖率（与讨论循环一致）
    使用    在历史预案里出现过几次、被选进买入计划几次、覆盖几个交易日
    适用场景 由它支撑的战法说明自动汇总
    综合评价 由数字自动生成（不吹不黑，没回测就写没回测）
    状态    active / retired

更新时机（"跟随因子变化实时更新"）：
    - 每次报告生成候选池后（collect.candidates 末尾）
    - 每次讨论循环结束（入库或失败）
    - 每周阶段验证后
    - 手动 `python -m portal.premarket.factors catalog`
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

_DATA = Path(__file__).parents[2] / "data" / "premarket"
CATALOG_JSON = _DATA / "factor_catalog.json"
STATS_JSON = _DATA / "factor_stats.json"
DOC = Path(__file__).parents[1].parent / "docs" / "factor-library.md"
ADMISSION_DIR = Path(__file__).parents[1].parent / "docs" / "factors"
PLANS = _DATA / "plans"
REPORTS = _DATA / "reports"

#: 与讨论循环保持一致的评分口径
IR_FULL_MARK = 0.30
MIN_IC = 0.02
MIN_IR = 0.20
MIN_COVERAGE = 0.50


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def score(stats: dict | None) -> float:
    if not stats:
        return 0.0
    ic = abs(float(stats.get("ic_mean") or 0))
    ir = abs(float(stats.get("ic_ir") or 0))
    cov = float(stats.get("coverage") or 0)
    return round(ic * min(1.0, ir / IR_FULL_MARK) * cov, 6)


# ─────────────────────────────────────────────────────────────
# 回测数据缓存
# ─────────────────────────────────────────────────────────────
LEDGER_PATH = _DATA / "factors_ledger.jsonl"


def _stats_from_ledger() -> dict[str, dict]:
    """回测数字的第二来源：讨论循环把 gated 事件写进了 ledger。

    为什么需要：`factor_stats.json` 只在跑过 validate/weekly/入库时更新，
    而 discover 早期版本只写了 ledger。两者合并才不会把"已测过"显示成"未回测"。
    """
    out: dict[str, dict] = {}
    if not LEDGER_PATH.exists():
        return out
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "gated" and ev.get("stats") and ev.get("id"):
            out[str(ev["id"])] = {**ev["stats"],
                                  "measured_at": ev.get("at") or ""}
    return out


def load_stats() -> dict[str, dict]:
    base = _stats_from_ledger()
    base.update(_read_json(STATS_JSON) or {})   # 缓存优先（更新）
    return base


def save_stats(updates: dict[str, dict]) -> dict[str, dict]:
    cur = load_stats()
    for fid, st in (updates or {}).items():
        if st:
            cur[fid] = {**st, "measured_at": _now()}
    STATS_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATS_JSON.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return cur


# ─────────────────────────────────────────────────────────────
# 使用情况（从历史预案里统计）
# ─────────────────────────────────────────────────────────────
def collect_usage() -> dict[str, dict]:
    """扫历史预案，统计每个战法/因子被用到的次数。

    只看 `market_digest.candidates`：
      - 战法：出现过几个交易日、每天平均给几只候选
      - 因子：在候选的 setup（"因子=值"）里出现多少次
    """
    plays: dict[str, dict] = {}
    factors: dict[str, dict] = {}
    files = sorted(PLANS.glob("plan_*.json"))
    for p in files:
        d = _read_json(p) or {}
        cands = ((d.get("market_digest") or {}).get("candidates") or [])
        seen_plays = set()
        for c in cands:
            pid = c.get("playbook")
            if pid:
                row = plays.setdefault(pid, {"days": set(), "candidates": 0,
                                             "selected": 0})
                row["days"].add(d.get("trade_date") or d.get("plan_date"))
                row["candidates"] += 1
                seen_plays.add(pid)
            for s in (c.get("setup") or []):
                if "=" in str(s):
                    fid = str(s).split("=", 1)[0].strip()
                    row = factors.setdefault(fid, {"appearances": 0,
                                                   "days": set()})
                    row["appearances"] += 1
                    row["days"].add(d.get("trade_date") or d.get("plan_date"))
        # 被选进买入计划的（planned.buy 的 code 对应哪个战法）
        codes = {b.get("code") for b in ((d.get("planned") or {}).get("buy") or [])}
        for c in cands:
            if c.get("code") in codes and c.get("playbook") in plays:
                plays[c["playbook"]]["selected"] += 1
    for v in plays.values():
        v["day_count"] = len(v.pop("days"))
    for v in factors.values():
        v["day_count"] = len(v.pop("days"))
    return {"plays": plays, "factors": factors, "plan_files": len(files)}


# ─────────────────────────────────────────────────────────────
# 组装总表
# ─────────────────────────────────────────────────────────────
def _play_map() -> tuple[dict[str, list[dict]], dict[str, str]]:
    """反查：因子 id → 用到它的战法列表；战法 id → 中文名。"""
    from . import load_factors
    from . import playbooks as PB

    fids = set(load_factors())
    plays = PB.load_all()
    used: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for p in plays:
        names[p["id"]] = p.get("name") or p["id"]
        tokens: set[str] = set()
        for cond in p.get("conditions") or []:
            for fid in fids:
                if fid in cond:
                    tokens.add(fid)
        for fid in p.get("evidence") or []:
            tokens.add(fid)
        for fid in tokens:
            used.setdefault(fid, []).append({
                "id": p["id"], "name": p.get("name"),
                "buyable": bool(p.get("buyable")), "why": p.get("why")})
    return used, names


def _scenario_html(plays: list[dict]) -> str:
    if not plays:
        return "未被任何战法引用（观察中）"
    return "；".join(f"{p['name']}（{'可买' if p['buyable'] else '仅观察'}）"
                    for p in plays)


def _verdict(stats: dict | None, plays: list[dict], retired: bool,
             direction: str = "") -> str:
    """自动生成综合评价。

    ★ 必须区分 IC 的**符号**与因子的**方向**是否一致：
      direction="low"（越小越好）配负 IC 是一致的；
      direction="high" 配负 IC 则说明"方向反了"，得反转使用才有意义。
      只看 |IC| 会得出错误的"可用"结论。
    """
    if retired:
        return "已下线"
    if not stats:
        return "尚未回测 —— 无法评价"
    ic_raw = float(stats.get("ic_mean") or 0)
    ic = abs(ic_raw)
    ir = abs(float(stats.get("ic_ir") or 0))
    cov = float(stats.get("coverage") or 0)
    bits = []
    if ic >= MIN_IC and ir >= MIN_IR:
        bits.append(f"已验证有预测力且稳定（IC={stats.get('ic_mean')}、"
                    f"IR={stats.get('ic_ir')}）")
    elif ic >= MIN_IC:
        bits.append(f"有预测力但不稳定（IC={stats.get('ic_mean')}、"
                    f"IR={stats.get('ic_ir')} 低于 {MIN_IR}）")
    else:
        bits.append(f"预测力不足（|IC|={ic:.4f} < {MIN_IC}）")

    # 方向一致性
    aligned = True
    if direction == "low" and ic_raw > 0:
        aligned = False
    elif direction == "high" and ic_raw < 0:
        aligned = False
    if ic >= MIN_IC and not aligned:
        bits.append("IC 符号与因子声明方向相反 —— 需反转使用"
                    "（把条件改成反向，或把 direction 改成相反）")

    if cov < MIN_COVERAGE:
        bits.append(f"覆盖率仅 {cov:.0%}，只对少数标的有效")
    if not plays:
        bits.append("当前没有战法使用它")
    ok = (aligned and len(bits) == 1 and ic >= MIN_IC and ir >= MIN_IR
          and cov >= MIN_COVERAGE)
    return ("可用：" if ok else "谨慎：") + "；".join(bits)


def build_rows(*, retired_ids: set[str] | None = None) -> dict[str, Any]:
    from . import load_factors

    factors = load_factors(refresh=False)
    used, names = _play_map()
    stats = load_stats()
    usage = collect_usage()
    retired_ids = retired_ids or set()

    rows = []
    for fid, f in factors.items():
        m = f.meta
        st = stats.get(fid)
        plays = used.get(fid) or []
        u_f = usage["factors"].get(fid) or {}
        rows.append({
            "id": fid,
            "name": m.get("name") or fid,
            "kind": f.kind,
            "source": m.get("source") or "builtin",
            "module": m.get("module"),
            "hypothesis": m.get("hypothesis") or "",
            "description": m.get("description") or "",
            "direction": m.get("direction") or "",
            "plays": plays,
            "play_names": [p["name"] for p in plays],
            "stats": st,
            "score": score(st),
            "usage": {"appearances": u_f.get("appearances", 0),
                      "days": u_f.get("day_count", 0)},
            "scenario": _scenario_html(plays),
            "verdict": _verdict(st, plays, fid in retired_ids,
                                m.get("direction") or ""),
            "status": "retired" if fid in retired_ids else "active",
            "created_at": m.get("created_at") or "",
        })
    rows.sort(key=lambda r: (-r["score"], r["id"]))

    # 战法表
    from . import playbooks as PB
    plays_all = {p["id"]: p for p in PB.load_all()}
    play_rows = []
    for pid, name in names.items():
        pb = plays_all.get(pid) or {}
        u = usage["plays"].get(pid) or {}
        play_rows.append({
            "id": pid, "name": name,
            "buyable": bool(pb.get("buyable")),
            "why": pb.get("why") or "",
            "conditions": pb.get("conditions") or [],
            "evidence": pb.get("evidence") or [],
            "source": pb.get("source") or "内置 playbooks.py",
            "days_used": u.get("day_count", 0),
            "candidates": u.get("candidates", 0),
            "selected": u.get("selected", 0),
        })
    play_rows.sort(key=lambda r: (-r["days_used"], -r["candidates"], r["id"]))

    return {"generated_at": _now(),
            "n_factors": len(rows), "n_plays": len(play_rows),
            "plan_files": usage["plan_files"],
            "factors": rows, "plays": play_rows}


# ─────────────────────────────────────────────────────────────
# 渲染
# ─────────────────────────────────────────────────────────────
def _fmt(v, digits: int = 4, suffix: str = "") -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def render_markdown(cat: dict[str, Any]) -> str:
    out = [
        "# 因子库总表（自动生成）",
        "",
        f"> 生成时间：{cat['generated_at']}　因子 {cat['n_factors']} 个　"
        f"战法 {cat['n_plays']} 个　统计自 {cat['plan_files']} 份历史预案",
        ">",
        "> **本文件由 `python -m portal.premarket.factors catalog` 自动生成，请勿手改。**",
        "> 每次报告生成候选池、每次讨论入库、每周阶段验证后都会刷新。",
        "",
        "## 一、因子清单（按评分降序）",
        "",
        "| # | 因子 | 类型 | 来源 | 战法数 | IC | IR | 覆盖率 | 评分 | "
        "出现次数 | 综合评价 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(cat["factors"], 1):
        st = r["stats"] or {}
        out.append(
            f"| {i} | `{r['id']}`<br>{r['name']} | {r['kind']} | {r['source']} | "
            f"{len(r['plays'])} | {_fmt(st.get('ic_mean'))} | "
            f"{_fmt(st.get('ic_ir'), 3)} | "
            f"{_fmt(st.get('coverage'), 2)} | {r['score']:.4f} | "
            f"{r['usage']['appearances']} | {r['verdict']} |")

    out += ["", "## 二、逐因子详情", ""]
    for r in cat["factors"]:
        st = r["stats"] or {}
        out += [f"### `{r['id']}` · {r['name']}", "",
                f"- **类型 / 来源**：{r['kind']} / {r['source']}"
                + (f"（`{r['module']}`）" if r.get("module") else ""),
                f"- **说明**：{r['description'] or '—'}"]
        if r.get("hypothesis"):
            out.append(f"- **提出假设**：{r['hypothesis']}")
        out.append(f"- **参与战法**：{r['scenario']}")
        if st:
            out.append(
                f"- **最近回测**：IC={_fmt(st.get('ic_mean'))}、"
                f"IR={_fmt(st.get('ic_ir'), 3)}、覆盖率={_fmt(st.get('coverage'), 2)}、"
                f"多空价差={_fmt(st.get('long_short_pct'), 3)}pp、"
                f"检验 {st.get('n_days')} 个交易日（{st.get('measured_at')}）")
        else:
            out.append("- **最近回测**：尚未回测")
        out.append(f"- **评分**：{r['score']:.4f}"
                   f"　**使用**：{r['usage']['appearances']} 次 / "
                   f"{r['usage']['days']} 个交易日")
        out.append(f"- **综合评价**：{r['verdict']}")
        out.append("")

    out += ["## 三、战法清单", "",
            "| 战法 | 类型 | 用它的交易日 | 给过的候选 | 被选入买入 | 依赖因子 | 说明 |",
            "|---|---|---|---|---|---|---|"]
    for p in cat["plays"]:
        out.append(
            f"| `{p['id']}`<br>{p['name']} | "
            f"{'可买' if p['buyable'] else '仅观察'} | {p['days_used']} | "
            f"{p['candidates']} | {p['selected']} | "
            f"{'、'.join(p['evidence']) or '—'} | {p['why'][:40]} |")

    out += ["", "## 四、口径说明", "",
            "- **评分** = `|IC| × min(1, |IR| / 0.30) × 覆盖率`，"
            "与讨论循环的回溯判断同一口径。",
            "- **入库门槛**：`|IC| ≥ 0.02`、`|IR| ≥ 0.20`、`覆盖率 ≥ 50%`，"
            "且不得含涨停/追高语义、不得与已有因子重复。",
            "- **IC**：因子值与次日收益的截面秩相关；**IR**：IC 均值 / 标准差。",
            "- **出现次数**：该因子在各历史预案候选的 `setup` 里被记录的次数。",
            "- 想刷新回测列：`python -m portal.premarket.factors validate --days 20`。",
            ""]
    return "\n".join(out)


def write_catalog(*, quiet: bool = False) -> dict[str, Any]:
    """生成 JSON 快照 + Markdown 总表。任何异常都不向上抛，只返回 ok=False。"""
    try:
        retired_ids = set()
        ret = Path(__file__).parent / "discovered" / "_retired"
        if ret.is_dir():
            for p in ret.glob("f_*.py"):
                retired_ids.add(p.stem[2:])
        cat = build_rows(retired_ids=retired_ids)
        CATALOG_JSON.parent.mkdir(parents=True, exist_ok=True)
        CATALOG_JSON.write_text(json.dumps(cat, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        DOC.parent.mkdir(parents=True, exist_ok=True)
        DOC.write_text(render_markdown(cat), encoding="utf-8")
        return {"ok": True, "factors": cat["n_factors"], "plays": cat["n_plays"],
                "doc": str(DOC), "json": str(CATALOG_JSON)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ─────────────────────────────────────────────────────────────
# 入库档案（每次讨论通过都要留档）
# ─────────────────────────────────────────────────────────────
def write_admission_record(*, prop: dict, bt: dict, history: list[dict],
                           rounds_cap: int, path: Path | None = None) -> Path:
    """把这次讨论的**完整流程**写成一份档案。

    内容：讨论核心（假设）、逐轮的代码与回测数字、评审意见、最终结论、
    它支撑的战法、以及可复现的源码。
    """
    fid = str(prop.get("id"))
    ADMISSION_DIR.mkdir(parents=True, exist_ok=True)
    p = path or (ADMISSION_DIR / f"{fid}.md")
    st = bt.get("stats") or {}
    lines = [
        f"# 因子入库档案：`{fid}` · {prop.get('name') or fid}",
        "",
        f"> 入库时间：{_now()}　讨论轮次：{len(history)}/{rounds_cap}",
        "> 本文由讨论循环在因子通过内核回测后自动生成，记录**当轮讨论的完整过程**。",
        "",
        "## 一、讨论核心",
        "",
        f"- **假设**：{prop.get('hypothesis') or '—'}",
        f"- **说明**：{prop.get('description') or '—'}",
        f"- **类型**：{prop.get('kind') or 'numeric'}",
        "",
        "## 二、最终回测结果（内核执行，非模型声称）",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| IC（信息系数） | {st.get('ic_mean')} |",
        f"| IR（稳定性） | {st.get('ic_ir')} |",
        f"| 覆盖率 | {st.get('coverage')} |",
        f"| 多空价差 | {st.get('long_short_pct')} pp |",
        f"| IC 为正的日子占比 | {st.get('ic_pos_ratio')} |",
        f"| 检验天数 | {st.get('n_days')} |",
        f"| 评分 | {bt.get('score')} |",
        "",
        "## 三、讨论过程",
        "",
        "| 轮次 | 变体 | IC | IR | 覆盖 | 评分 | 评审 | 决策 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for h in history:
        lines.append(
            f"| {h.get('round')} | `{h.get('id')}` | {_fmt(h.get('ic'))} | "
            f"{_fmt(h.get('ir'), 3)} | {_fmt(h.get('coverage'), 2)} | "
            f"{h.get('score')} | {h.get('verdict')} | {h.get('decision')} |")
    lines += ["", "### 逐轮评审意见", ""]
    for h in history:
        if not (h.get("issues") or h.get("next_change")):
            continue
        lines.append(f"**第 {h.get('round')} 轮**")
        for x in h.get("issues") or []:
            lines.append(f"- 问题：{x}")
        if h.get("next_change"):
            lines.append(f"- 下一步：{h['next_change']}")
        lines.append("")

    lines += ["## 四、它支撑的战法", ""]
    for play in (prop.get("plays") or []):
        lines += [f"### `{play.get('id')}` · {play.get('name')}", "",
                  f"- 可买：{'是' if play.get('buyable') else '否（仅观察）'}",
                  f"- 逻辑：{play.get('why') or '—'}", "- 条件："]
        for c in play.get("conditions") or []:
            lines.append(f"  - `{c}`")
        lines.append("")
    if not (prop.get("plays")):
        lines.append("（本因子未附带战法，仅作为可选因子进入库中）")
        lines.append("")

    lines += ["## 五、源码（可复现）", "", "```python"]
    if prop.get("expr"):
        lines += [f"EXPR = {json.dumps(prop['expr'], ensure_ascii=False)}"]
    else:
        lines += [(prop.get("code") or "").strip()]
    lines += ["```", "",
              "## 六、如何复核", "",
              "```bash",
              f"python -m portal.premarket.factors eval        # 看当日取值分布",
              f"python -m portal.premarket.factors validate --id {fid} --days 20",
              f"python -m portal.premarket.factors check       # 确认已参与战法筛选",
              "```", ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


__all__ = ["write_catalog", "build_rows", "render_markdown", "collect_usage",
           "load_stats", "save_stats", "score", "write_admission_record",
           "CATALOG_JSON", "DOC", "ADMISSION_DIR", "STATS_JSON"]
